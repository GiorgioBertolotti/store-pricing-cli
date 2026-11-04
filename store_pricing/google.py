"""Google Play price updates via the Android Publisher API.

Prices are written by patching the full subscription object in-place (there is no
per-region endpoint), which means every patch resubmits *all* base plans, not just the one
being targeted - a pre-existing data problem on a base plan you have no intention of
touching (e.g. a stale price left over from a currency changeover) can still block the
whole patch. The 400-error recovery ladder below (_handle_400_error and friends) exists
because of that: it auto-fixes stale currency codes, drops non-billable regions, and
clamps out-of-range prices to Google's minimum, retrying until the patch validates or a
genuine data lock is hit.

Ported from subscription_price_applier.py with one structural change: every function here
takes GoogleCreds explicitly instead of reading module-level globals frozen at import time,
so credentials can be constructed by a wizard and used in the same process.

One-time (managed) product pricing (update_onetime_prices() and friends, below the
subscription functions) uses Google's newer `monetization.onetimeproducts` resource rather
than the legacy `inappproducts` one - it mirrors the subscription shape closely enough to
reuse most of the same patch/retry machinery: a OneTimeProduct's `purchaseOptions` array
plays exactly the role `basePlans` does for a Subscription, and each purchase option's
`regionalPricingAndAvailabilityConfigs` is the same per-region `price: Money` shape as a
base plan's `regionalConfigs`. The 400-error recovery ladder is reused where Google's error
wording is product-agnostic (currency mismatch, non-billable region, price out of range);
only the "regional configs were removed" message names "purchase option" instead of "base
plan" for this product type, so it gets its own regex - unverified against a live 400
(if Google's actual wording differs, the ladder falls through and re-raises rather than
mishandling it, same as an unrecognised error today).
"""

from __future__ import annotations

import copy
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from store_pricing import backup
from store_pricing.config import GoogleCreds, GoogleOneTimeCreds, PricingConfig
from store_pricing.inputs import InputData, get_alpha_2_country_code
from store_pricing.report import make_failure
from store_pricing.scaler import convert_to_currency, fetch_all_usd_rates

# These parse Google's human-readable 400 error strings to identify recoverable errors.
# If Google changes the wording the regex won't match, and the HttpError re-raises unchanged - safe fallback.
# The first three are product-type-agnostic (no "base plan"/"purchase option" wording) and
# are reused as-is by the one-time-product ladder further down; only the regional-configs-
# removal message is product-specific, so it has a onetimeproducts variant below.
_CURRENCY_MISMATCH_RE = re.compile(r"region code (\w+).*?Expected (\w+) but got (\w+)")
_NOT_BILLABLE_RE = re.compile(r"Region code (\w+) is not billable")
_PRICE_OUT_OF_RANGE_RE = re.compile(r"([\w-]+): Price for (\w+) must be between [^\d]*([\d.]+)")
_REGION_REMOVAL_REJECTED_RE = re.compile(r"([\w-]+): Regional configs were removed from the base plan: (\w+)")


def _price_to_units_nanos(price: float) -> tuple:
    # Round to whole cents first via divmod so a carry (e.g. 99.5 cents rounding
    # up to 100) rolls into the unit instead of producing an out-of-range nanos
    # value - Google's Money type requires 0 <= nanos <= 999_999_999.
    total_cents = round(price * 100)
    units, cents = divmod(total_cents, 100)
    nanos = cents * int(1e7)
    return str(units), nanos


def _make_failure(country, country_code, price, currency, reason) -> dict:
    return make_failure("Google Play", country, country_code, price, currency, reason)


def build_service(creds: "GoogleCreds | GoogleOneTimeCreds"):
    credentials = service_account.Credentials.from_service_account_file(
        creds.service_account_file,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    # static_discovery=False forces a live fetch of Google's discovery document instead of
    # the copy bundled inside the installed google-api-python-client package, which is only
    # as fresh as whatever version happened to be pinned - the `onetimeproducts` resource
    # (used by update_onetime_prices() and friends) was added to Android Publisher after
    # google-api-python-client 2.149.0 was released, so the bundled copy 404s on it with a
    # confusing "'Resource' object has no attribute 'onetimeproducts'" rather than a clear
    # version error. subscriptions() has been in the discovery doc long enough that this
    # wasn't visible before one-time-product support needed a newer resource.
    return build("androidpublisher", "v3", credentials=credentials, static_discovery=False)


def _build_service_safe(creds: "GoogleCreds | GoogleOneTimeCreds"):
    """build_service(), but catching connection/auth errors instead of letting them crash
    the whole run - static_discovery=False (see build_service()'s docstring) added a live
    network fetch of Google's discovery document to every call here, so a network hiccup or
    a bad service-account file now surfaces the same way credentials.Credentials.from_...()
    already could, and every caller needs to handle it the same graceful way the rest of
    this module handles a failed API call rather than an unhandled exception."""
    try:
        return build_service(creds)
    except Exception as e:
        print(f"Error connecting to Google Play: {e}")
        return None


def fetch_subscription(service, creds: GoogleCreds) -> "tuple | None":
    """Fetch subscription from Play Console. Returns (subscription_response, regions_version) or None."""
    try:
        response = (
            service.monetization()
            .subscriptions()
            .get(packageName=creds.package_name, productId=creds.subscription_id)
            .execute()
        )
        regions_version = response.get("regionsVersion", "2025/01")
        if regions_version == "1":
            regions_version = "2025/01"
        return response, regions_version
    except Exception as e:
        print(f"Error getting subscription details: {e}")
        return None


def fetch_live_prices(creds: GoogleCreds, data: InputData, base_plan_id: "str | None" = None) -> dict[str, dict]:
    """Return {country_name: {"price": float, "currency": str}} for the current live
    regional configs of the given base plan (defaults to creds.baseplan_id) - used for the
    apply-time diff against what's about to be pushed.

    Google's regional configs are keyed by ISO alpha-2 region code, but everything else in
    this pipeline (InputData.country_prices, the diff table) is keyed by country name - so
    this reverses country_code_mapping to translate as it reads, rather than pushing that
    mismatch onto every caller.

    The reverse map is built only from `data.country_prices` (the countries we're actually
    pricing), not from every entry in country_code_mapping. country_codes.json accumulates
    naming variants over time since its refresh is merge-only and never deletes an old
    entry - e.g. both "United States" and "United States of America" map to alpha2 "US",
    "Korea, Rep." and "South Korea" both map to "KR". Reversing the *whole* file would pick
    whichever variant happens to sort last, which often isn't the exact name
    price_scaled.xlsx uses - silently losing that country's live price (it would show as
    "new" in the diff even though it's actually live).
    """
    service = _build_service_safe(creds)
    if service is None:
        return {}
    result = fetch_subscription(service, creds)
    if result is None:
        return {}
    subscription, _ = result
    base_plan_id = base_plan_id or creds.baseplan_id

    alpha2_to_name = {}
    for country in data.country_prices:
        code = get_alpha_2_country_code(country, data.country_code_mapping)
        if code:
            alpha2_to_name[code] = country

    live = {}
    for bp in subscription.get("basePlans", []):
        if bp.get("basePlanId") != base_plan_id:
            continue
        for config in bp.get("regionalConfigs", []):
            country_name = alpha2_to_name.get(config.get("regionCode"))
            if not country_name:
                continue
            price = config.get("price", {})
            units = float(price.get("units", 0))
            nanos = price.get("nanos", 0)
            live[country_name] = {
                "price": units + nanos / 1e9,
                "currency": price.get("currencyCode"),
            }
    return live


def _update_region(
    subscription_update, creds: GoogleCreds, country, country_code, currency_code, price,
    usd_price: "float | None" = None, usd_rates: "dict | None" = None, rounding: str = "psychological",
) -> "dict | None":
    price_units, price_nanos = _price_to_units_nanos(float(price))

    for base_plan in subscription_update.get("basePlans", []):
        if base_plan.get("basePlanId") != creds.baseplan_id:
            continue

        for config in base_plan.get("regionalConfigs", []):
            if config.get("regionCode") != country_code:
                continue

            existing_currency = config.get("price", {}).get("currencyCode")
            if existing_currency != currency_code:
                # Google rejects any price update whose currency doesn't match what's
                # already configured for that region (no in-place currency change via this
                # endpoint) - usually a leftover from an earlier flat-price rollout that
                # never localized this region. Rather than fail outright, convert our
                # computed price into whatever currency Google already has on file, the
                # same way Apple's resolver retries in USD for territories it prices
                # differently (see apple.resolve_price_point()).
                converted = convert_to_currency(usd_price, existing_currency, usd_rates or {}, rounding)
                if converted is None:
                    reason = (
                        f"Currency mismatch: Google expects {existing_currency}, we have {currency_code} "
                        f"(couldn't auto-convert - no USD price or exchange rate available)"
                    )
                    print(f"   Skipping {country_code} - {reason}")
                    return _make_failure(country, country_code, price, currency_code, reason)

                print(f"   Currency mismatch for {country_code}: Google expects {existing_currency} - "
                      f"converting {price} {currency_code} -> {converted} {existing_currency}")
                conv_units, conv_nanos = _price_to_units_nanos(converted)
                config["price"] = {"currencyCode": existing_currency, "units": conv_units, "nanos": conv_nanos}
                return None

            config["price"] = {"currencyCode": currency_code, "units": price_units, "nanos": price_nanos}
            return None

        reason = "Region not found in Google Play configuration"
        print(f"   Skipping {country_code} - {reason}")
        return _make_failure(country, country_code, price, currency_code, reason)

    return _make_failure(
        country, country_code, price, currency_code,
        f"Base plan {creds.baseplan_id} not found in subscription",
    )


def _fix_currency_mismatch(subscription_update, region_code, expected_currency, got_currency) -> None:
    for bp in subscription_update.get("basePlans", []):
        for config in bp.get("regionalConfigs", []):
            if config.get("regionCode") == region_code:
                if config.get("price", {}).get("currencyCode") == got_currency:
                    config["price"]["currencyCode"] = expected_currency


def _fix_not_billable(subscription_update, region_code) -> None:
    for bp in subscription_update.get("basePlans", []):
        bp["regionalConfigs"] = [c for c in bp.get("regionalConfigs", []) if c.get("regionCode") != region_code]


def _remove_region_from_base_plan(subscription_update, base_plan_id, region_code) -> None:
    for bp in subscription_update.get("basePlans", []):
        if bp.get("basePlanId") == base_plan_id:
            bp["regionalConfigs"] = [c for c in bp.get("regionalConfigs", []) if c.get("regionCode") != region_code]


def _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str) -> tuple:
    """Clamp region price to the API's minimum. Returns (min_price, failure_dict)."""
    try:
        min_price = float(min_price_str)
    except ValueError as e:
        raise RuntimeError(f"Could not parse minimum price '{min_price_str}' from API error") from e

    price_units, price_nanos = _price_to_units_nanos(min_price)
    for bp in subscription_update.get("basePlans", []):
        if bp.get("basePlanId") == base_plan_id:
            for config in bp.get("regionalConfigs", []):
                if config.get("regionCode") == region_code:
                    config["price"]["units"] = price_units
                    config["price"]["nanos"] = price_nanos

    failure = _make_failure(region_code, region_code, min_price, None, f"Price clamped to Google Play minimum ({min_price})")
    return min_price, failure


def _handle_400_error(subscription_update, error_str, attempt, extra_failures, auto_fixed_regions, clamped_pairs) -> bool:
    """Handle a recoverable Play Console 400 error. Returns True if handled, False to re-raise."""
    currency_match = _CURRENCY_MISMATCH_RE.search(error_str)
    if currency_match:
        region_code, expected_currency, got_currency = currency_match.groups()
        print(f"   Auto-fixing stale currency for {region_code}: {got_currency} → {expected_currency} (attempt {attempt + 1})")
        _fix_currency_mismatch(subscription_update, region_code, expected_currency, got_currency)
        auto_fixed_regions.add(region_code)
        return True

    billable_match = _NOT_BILLABLE_RE.search(error_str)
    if billable_match:
        region_code = billable_match.group(1)
        print(f"   Removing non-billable region {region_code} (attempt {attempt + 1})")
        _fix_not_billable(subscription_update, region_code)
        return True

    removal_rejected_match = _REGION_REMOVAL_REJECTED_RE.search(error_str)
    if removal_rejected_match:
        base_plan_id, region_code = removal_rejected_match.groups()
        raise RuntimeError(
            f"Google Play won't accept a fix for {region_code} on base plan '{base_plan_id}' via this API: "
            f"its price fails Google's own validation, but the API also refuses to let us remove that "
            f"region's config to work around it. This needs a manual fix in Play Console "
            f"(Monetize > Products > your subscription > {base_plan_id} > {region_code} pricing) before "
            f"any price updates can go through - Google requires the whole basePlans object to validate "
            f"together, so this one broken entry blocks every base plan's price update, not just its own."
        )

    price_range_match = _PRICE_OUT_OF_RANGE_RE.search(error_str)
    if price_range_match:
        base_plan_id, region_code, min_price_str = price_range_match.groups()
        detail = error_str.split("Details:")[0].strip()
        pair = (base_plan_id, region_code)

        if pair in clamped_pairs:
            # We already clamped this exact (base plan, region) once and Google is reporting
            # the identical violation again - the clamp isn't converging (likely a pre-existing
            # data inconsistency on that base plan, unrelated to what this run is updating).
            # Drop the region from just that base plan so the rest of the patch can go through.
            print(f"   Clamp for {region_code} in {base_plan_id} didn't stick - removing that region from {base_plan_id} instead (attempt {attempt + 1}): {detail}")
            _remove_region_from_base_plan(subscription_update, base_plan_id, region_code)
            extra_failures.append(_make_failure(
                region_code, region_code, None, None,
                f"Removed from {base_plan_id}: price kept failing validation after clamping ({detail})",
            ))
            return True

        print(f"   Clamping {region_code} in {base_plan_id} to minimum price (attempt {attempt + 1}): {detail}")
        min_price, failure = _fix_price_out_of_range(subscription_update, base_plan_id, region_code, min_price_str)
        clamped_pairs.add(pair)
        failure["reason"] = f"Price clamped to Google Play minimum ({min_price}): {detail}"
        extra_failures.append(failure)
        return True

    return False


def patch_with_currency_fixes(service, creds: GoogleCreds, subscription_update, regions_version, max_retries=15) -> tuple:
    """Submit the Play Console subscription patch, retrying on recoverable 400 errors.

    Returns (auto_fixed_regions, extra_failures).
    """
    auto_fixed_regions = set()
    extra_failures = []
    clamped_pairs = set()

    for attempt in range(max_retries):
        request = (
            service.monetization()
            .subscriptions()
            .patch(
                packageName=creds.package_name,
                productId=creds.subscription_id,
                allowMissing=False,
                updateMask="basePlans",
                body=subscription_update,
            )
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"

        try:
            request.execute()
            return auto_fixed_regions, extra_failures
        except HttpError as e:
            if e.resp.status != 400:
                raise
            if not _handle_400_error(subscription_update, str(e), attempt, extra_failures, auto_fixed_regions, clamped_pairs):
                raise

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def update_prices(creds: GoogleCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    """Push data.country_prices to Google Play. In dry-run mode, builds the same patch
    body and reports what would change but never calls .patch()."""
    service = _build_service_safe(creds)
    if service is None:
        return [_make_failure(
            "N/A", "N/A", None, None,
            "Could not connect to Google Play - no prices were updated (see console output above)",
        )]

    result = fetch_subscription(service, creds)
    if result is None:
        return [_make_failure(
            "N/A", "N/A", None, None,
            "Could not fetch subscription from Google Play - no prices were updated (see console output above)",
        )]
    subscription_response, regions_version = result
    subscription_update = copy.deepcopy(subscription_response)

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - currency-mismatched regions will fail instead of auto-converting")
        usd_rates = {}

    failures = []
    for country, price in data.country_prices.items():
        country_code = None
        try:
            country_code = get_alpha_2_country_code(country, data.country_code_mapping)
            currency_code = data.country_currencies.get(country, "USD")
            print(f"{'[dry-run] Would update' if dry_run else 'Updating'} Google Play price for {country} ({country_code}): {price} {currency_code}")
            failure = _update_region(
                subscription_update, creds, country, country_code, currency_code, price,
                usd_price=data.country_prices_usd.get(country), usd_rates=usd_rates, rounding=data.rounding or config.rounding,
            )
            if failure:
                failures.append(failure)
        except Exception as e:
            print(f"Error updating {country}: {e}")
            failures.append(_make_failure(
                country, country_code or "Unknown", price,
                data.country_currencies.get(country, "Unknown"), f"Exception: {e}",
            ))

    if dry_run:
        print(f"[dry-run] Would patch base plan '{creds.baseplan_id}' - no request sent")
        return failures

    try:
        backup_path = backup.write_backup(backup.SUBSCRIPTION, creds.subscription_id, subscription_response)
    except Exception as e:
        return failures + [_make_failure(
            "N/A", "N/A", None, None,
            f"Could not back up the subscription before patching ({e}) - no prices were updated",
        )]
    print(f"Backed up subscription '{creds.subscription_id}' to {backup_path}")

    auto_fixed, patch_failures = patch_with_currency_fixes(service, creds, subscription_update, regions_version)
    if auto_fixed:
        failures = [f for f in failures if f.get("country_code") not in auto_fixed]
    failures.extend(patch_failures)
    print("Successfully updated Google Play prices")

    after = fetch_subscription(service, creds)
    failures.extend(_verification_failures(
        _make_failure, subscription_response, after[0] if after else None,
        backup.SUBSCRIPTION, creds.baseplan_id, backup_path,
    ))
    return failures


def _verification_failures(make, before: dict, after: "dict | None", shape, target_id: str, backup_path) -> list[dict]:
    """Re-read after the patch and report anything outside the priced container that moved."""
    restore_hint = f"restore with `pricing.py restore-google --backup {backup_path}`"
    if after is None:
        return [make("N/A", "N/A", None, None, f"Could not re-read the product to verify the patch - {restore_hint} if needed")]
    issues = backup.compare(before, after, shape, target_id)
    if issues:
        print(f"WARNING: the patch changed prices outside '{target_id}' - {restore_hint}")
    return [make("N/A", "N/A", None, None, f"Unexpected change after patch: {issue} - {restore_hint}") for issue in issues]


# --- ONE-TIME (MANAGED) PRODUCTS ---

_PURCHASE_OPTION_REMOVAL_REJECTED_RE = re.compile(
    r"([\w-]+): Regional configs were removed from the purchase option: (\w+)"
)


def _make_onetime_failure(country, country_code, price, currency, reason) -> dict:
    # A distinct platform label from _make_failure()'s "Google Play" - otherwise a run that
    # applies both the subscription and the one-time product in the same invocation merges
    # their failures into one indistinguishable "Google Play" bucket in the failure report
    # and the console summary table (report.write_failure_report() and
    # ui.failure_summary_table() both group by this exact string).
    return make_failure("Google Play (One-time)", country, country_code, price, currency, reason)


def fetch_onetime_product(service, creds: GoogleOneTimeCreds) -> "tuple | None":
    """Fetch a one-time product from Play Console. Returns (product_response,
    regions_version) or None. Unlike Subscription (regionsVersion is a bare string on the
    resource), OneTimeProduct's regionsVersion is the RegionsVersion object {"version":
    "..."} per Google's schema - read the nested field rather than the subscription
    shortcut of using the field's value directly."""
    try:
        response = (
            service.monetization()
            .onetimeproducts()
            .get(packageName=creds.package_name, productId=creds.product_id)
            .execute()
        )
        regions_version = response.get("regionsVersion", {}).get("version", "2025/01")
        return response, regions_version
    except Exception as e:
        print(f"Error getting one-time product details: {e}")
        return None


def fetch_live_onetime_prices(creds: GoogleOneTimeCreds, data: InputData, purchase_option_id: "str | None" = None) -> dict[str, dict]:
    """One-time-product equivalent of fetch_live_prices() - same alpha2-to-name reversal,
    walking `purchaseOptions`/`regionalPricingAndAvailabilityConfigs` instead of
    `basePlans`/`regionalConfigs`."""
    service = _build_service_safe(creds)
    if service is None:
        return {}
    result = fetch_onetime_product(service, creds)
    if result is None:
        return {}
    product, _ = result
    purchase_option_id = purchase_option_id or creds.purchase_option_id

    alpha2_to_name = {}
    for country in data.country_prices:
        code = get_alpha_2_country_code(country, data.country_code_mapping)
        if code:
            alpha2_to_name[code] = country

    live = {}
    for po in product.get("purchaseOptions", []):
        if po.get("purchaseOptionId") != purchase_option_id:
            continue
        for config in po.get("regionalPricingAndAvailabilityConfigs", []):
            country_name = alpha2_to_name.get(config.get("regionCode"))
            if not country_name:
                continue
            price = config.get("price", {})
            units = float(price.get("units", 0))
            nanos = price.get("nanos", 0)
            live[country_name] = {
                "price": units + nanos / 1e9,
                "currency": price.get("currencyCode"),
            }
    return live


def _update_onetime_region(
    product_update, creds: GoogleOneTimeCreds, country, country_code, currency_code, price,
    usd_price: "float | None" = None, usd_rates: "dict | None" = None, rounding: str = "psychological",
) -> "dict | None":
    """One-time-product equivalent of _update_region() - see that function for the
    currency-mismatch auto-conversion rationale, which applies identically here."""
    price_units, price_nanos = _price_to_units_nanos(float(price))

    for po in product_update.get("purchaseOptions", []):
        if po.get("purchaseOptionId") != creds.purchase_option_id:
            continue

        for config in po.get("regionalPricingAndAvailabilityConfigs", []):
            if config.get("regionCode") != country_code:
                continue

            existing_currency = config.get("price", {}).get("currencyCode")
            if existing_currency != currency_code:
                converted = convert_to_currency(usd_price, existing_currency, usd_rates or {}, rounding)
                if converted is None:
                    reason = (
                        f"Currency mismatch: Google expects {existing_currency}, we have {currency_code} "
                        f"(couldn't auto-convert - no USD price or exchange rate available)"
                    )
                    print(f"   Skipping {country_code} - {reason}")
                    return _make_onetime_failure(country, country_code, price, currency_code, reason)

                print(f"   Currency mismatch for {country_code}: Google expects {existing_currency} - "
                      f"converting {price} {currency_code} -> {converted} {existing_currency}")
                conv_units, conv_nanos = _price_to_units_nanos(converted)
                config["price"] = {"currencyCode": existing_currency, "units": conv_units, "nanos": conv_nanos}
                return None

            config["price"] = {"currencyCode": currency_code, "units": price_units, "nanos": price_nanos}
            return None

        reason = "Region not found in Google Play configuration"
        print(f"   Skipping {country_code} - {reason}")
        return _make_onetime_failure(country, country_code, price, currency_code, reason)

    return _make_onetime_failure(
        country, country_code, price, currency_code,
        f"Purchase option {creds.purchase_option_id} not found in one-time product",
    )


def _fix_onetime_currency_mismatch(product_update, region_code, expected_currency, got_currency) -> None:
    for po in product_update.get("purchaseOptions", []):
        for config in po.get("regionalPricingAndAvailabilityConfigs", []):
            if config.get("regionCode") == region_code:
                if config.get("price", {}).get("currencyCode") == got_currency:
                    config["price"]["currencyCode"] = expected_currency


def _fix_onetime_not_billable(product_update, region_code) -> None:
    for po in product_update.get("purchaseOptions", []):
        po["regionalPricingAndAvailabilityConfigs"] = [
            c for c in po.get("regionalPricingAndAvailabilityConfigs", []) if c.get("regionCode") != region_code
        ]


def _remove_region_from_purchase_option(product_update, purchase_option_id, region_code) -> None:
    for po in product_update.get("purchaseOptions", []):
        if po.get("purchaseOptionId") == purchase_option_id:
            po["regionalPricingAndAvailabilityConfigs"] = [
                c for c in po.get("regionalPricingAndAvailabilityConfigs", []) if c.get("regionCode") != region_code
            ]


def _fix_onetime_price_out_of_range(product_update, purchase_option_id, region_code, min_price_str) -> tuple:
    """Clamp region price to the API's minimum. Returns (min_price, failure_dict)."""
    try:
        min_price = float(min_price_str)
    except ValueError as e:
        raise RuntimeError(f"Could not parse minimum price '{min_price_str}' from API error") from e

    price_units, price_nanos = _price_to_units_nanos(min_price)
    for po in product_update.get("purchaseOptions", []):
        if po.get("purchaseOptionId") == purchase_option_id:
            for config in po.get("regionalPricingAndAvailabilityConfigs", []):
                if config.get("regionCode") == region_code:
                    config["price"]["units"] = price_units
                    config["price"]["nanos"] = price_nanos

    failure = _make_onetime_failure(region_code, region_code, min_price, None, f"Price clamped to Google Play minimum ({min_price})")
    return min_price, failure


def _handle_onetime_400_error(product_update, error_str, attempt, extra_failures, auto_fixed_regions, clamped_pairs) -> bool:
    """One-time-product analogue of _handle_400_error() - see that function and the module
    docstring for which regexes are shared verbatim vs product-specific."""
    currency_match = _CURRENCY_MISMATCH_RE.search(error_str)
    if currency_match:
        region_code, expected_currency, got_currency = currency_match.groups()
        print(f"   Auto-fixing stale currency for {region_code}: {got_currency} → {expected_currency} (attempt {attempt + 1})")
        _fix_onetime_currency_mismatch(product_update, region_code, expected_currency, got_currency)
        auto_fixed_regions.add(region_code)
        return True

    billable_match = _NOT_BILLABLE_RE.search(error_str)
    if billable_match:
        region_code = billable_match.group(1)
        print(f"   Removing non-billable region {region_code} (attempt {attempt + 1})")
        _fix_onetime_not_billable(product_update, region_code)
        return True

    removal_rejected_match = _PURCHASE_OPTION_REMOVAL_REJECTED_RE.search(error_str)
    if removal_rejected_match:
        purchase_option_id, region_code = removal_rejected_match.groups()
        raise RuntimeError(
            f"Google Play won't accept a fix for {region_code} on purchase option '{purchase_option_id}' via this API: "
            f"its price fails Google's own validation, but the API also refuses to let us remove that "
            f"region's config to work around it. This needs a manual fix in Play Console "
            f"(Monetize > Products > your one-time product > {purchase_option_id} > {region_code} pricing) before "
            f"any price updates can go through - Google requires the whole purchaseOptions object to validate "
            f"together, so this one broken entry blocks every purchase option's price update, not just its own."
        )

    price_range_match = _PRICE_OUT_OF_RANGE_RE.search(error_str)
    if price_range_match:
        purchase_option_id, region_code, min_price_str = price_range_match.groups()
        detail = error_str.split("Details:")[0].strip()
        pair = (purchase_option_id, region_code)

        if pair in clamped_pairs:
            print(f"   Clamp for {region_code} in {purchase_option_id} didn't stick - removing that region from {purchase_option_id} instead (attempt {attempt + 1}): {detail}")
            _remove_region_from_purchase_option(product_update, purchase_option_id, region_code)
            extra_failures.append(_make_onetime_failure(
                region_code, region_code, None, None,
                f"Removed from {purchase_option_id}: price kept failing validation after clamping ({detail})",
            ))
            return True

        print(f"   Clamping {region_code} in {purchase_option_id} to minimum price (attempt {attempt + 1}): {detail}")
        min_price, failure = _fix_onetime_price_out_of_range(product_update, purchase_option_id, region_code, min_price_str)
        clamped_pairs.add(pair)
        failure["reason"] = f"Price clamped to Google Play minimum ({min_price}): {detail}"
        extra_failures.append(failure)
        return True

    return False


def patch_onetime_with_currency_fixes(service, creds: GoogleOneTimeCreds, product_update, regions_version, max_retries=15) -> tuple:
    """Submit the Play Console one-time-product patch, retrying on recoverable 400 errors.

    Returns (auto_fixed_regions, extra_failures).
    """
    auto_fixed_regions = set()
    extra_failures = []
    clamped_pairs = set()

    for attempt in range(max_retries):
        request = (
            service.monetization()
            .onetimeproducts()
            .patch(
                packageName=creds.package_name,
                productId=creds.product_id,
                allowMissing=False,
                updateMask="purchaseOptions",
                body=product_update,
            )
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"

        try:
            request.execute()
            return auto_fixed_regions, extra_failures
        except HttpError as e:
            if e.resp.status != 400:
                raise
            if not _handle_onetime_400_error(product_update, str(e), attempt, extra_failures, auto_fixed_regions, clamped_pairs):
                raise

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def update_onetime_prices(creds: GoogleOneTimeCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    """Push data.country_prices to a Google Play one-time product. In dry-run mode, builds
    the same patch body and reports what would change but never calls .patch()."""
    service = _build_service_safe(creds)
    if service is None:
        return [_make_onetime_failure(
            "N/A", "N/A", None, None,
            "Could not connect to Google Play - no prices were updated (see console output above)",
        )]

    result = fetch_onetime_product(service, creds)
    if result is None:
        return [_make_onetime_failure(
            "N/A", "N/A", None, None,
            "Could not fetch one-time product from Google Play - no prices were updated (see console output above)",
        )]
    product_response, regions_version = result
    product_update = copy.deepcopy(product_response)

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - currency-mismatched regions will fail instead of auto-converting")
        usd_rates = {}

    failures = []
    for country, price in data.country_prices.items():
        country_code = None
        try:
            country_code = get_alpha_2_country_code(country, data.country_code_mapping)
            currency_code = data.country_currencies.get(country, "USD")
            print(f"{'[dry-run] Would update' if dry_run else 'Updating'} Google Play one-time price for {country} ({country_code}): {price} {currency_code}")
            failure = _update_onetime_region(
                product_update, creds, country, country_code, currency_code, price,
                usd_price=data.country_prices_usd.get(country), usd_rates=usd_rates, rounding=data.rounding or config.rounding,
            )
            if failure:
                failures.append(failure)
        except Exception as e:
            print(f"Error updating {country}: {e}")
            failures.append(_make_onetime_failure(
                country, country_code or "Unknown", price,
                data.country_currencies.get(country, "Unknown"), f"Exception: {e}",
            ))

    if dry_run:
        print(f"[dry-run] Would patch purchase option '{creds.purchase_option_id}' - no request sent")
        return failures

    try:
        backup_path = backup.write_backup(backup.ONETIME_PRODUCT, creds.product_id, product_response)
    except Exception as e:
        return failures + [_make_onetime_failure(
            "N/A", "N/A", None, None,
            f"Could not back up the one-time product before patching ({e}) - no prices were updated",
        )]
    print(f"Backed up one-time product '{creds.product_id}' to {backup_path}")

    auto_fixed, patch_failures = patch_onetime_with_currency_fixes(service, creds, product_update, regions_version)
    if auto_fixed:
        failures = [f for f in failures if f.get("country_code") not in auto_fixed]
    failures.extend(patch_failures)
    print("Successfully updated Google Play one-time product prices")

    after = fetch_onetime_product(service, creds)
    failures.extend(_verification_failures(
        _make_onetime_failure, product_response, after[0] if after else None,
        backup.ONETIME_PRODUCT, creds.purchase_option_id, backup_path,
    ))
    return failures


# --- RESTORE FROM BACKUP ---

def backup_live(creds: "GoogleCreds | GoogleOneTimeCreds"):
    """Snapshot the live product without changing anything. Returns the backup path."""
    is_subscription = isinstance(creds, GoogleCreds)
    shape = backup.SUBSCRIPTION if is_subscription else backup.ONETIME_PRODUCT
    service = build_service(creds)
    result = (fetch_subscription if is_subscription else fetch_onetime_product)(service, creds)
    if result is None:
        raise RuntimeError("Could not fetch the live product from Google Play")
    return backup.write_backup(shape, creds.subscription_id if is_subscription else creds.product_id, result[0])


def _patch_resource(service, shape, creds, body: dict, regions_version: str) -> None:
    if shape is backup.SUBSCRIPTION:
        request = service.monetization().subscriptions().patch(
            packageName=creds.package_name, productId=creds.subscription_id,
            allowMissing=False, updateMask="basePlans", body=body,
        )
    else:
        request = service.monetization().onetimeproducts().patch(
            packageName=creds.package_name, productId=creds.product_id,
            allowMissing=False, updateMask="purchaseOptions", body=body,
        )
    sep = "&" if "?" in request.uri else "?"
    request.uri += f"{sep}regionsVersion.version={regions_version}"
    request.execute()


def restore_from_backup(
    creds: "GoogleCreds | GoogleOneTimeCreds", payload: dict, dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """Put a backup's regional prices back. Returns (changes, remaining): what the restore
    changes relative to what is live now, and what still differs from the backup afterwards
    (empty on success). Snapshots the live state first, so a restore can itself be undone.
    No 400-error recovery here: a restore must land exactly as backed up or not at all."""
    shape = backup.SHAPES[payload["kind"]]
    live_product_id = creds.subscription_id if shape is backup.SUBSCRIPTION else creds.product_id
    if payload["product_id"] != live_product_id:
        raise ValueError(
            f"Backup is for '{payload['product_id']}' but the configured product is '{live_product_id}' - "
            "point .env at the product the backup was taken from"
        )

    service = build_service(creds)
    fetch = fetch_subscription if shape is backup.SUBSCRIPTION else fetch_onetime_product
    result = fetch(service, creds)
    if result is None:
        raise RuntimeError("Could not fetch the live product from Google Play")
    live, regions_version = result
    wanted = payload["resource"]

    changes = backup.compare(live, wanted, shape, None)
    if dry_run or not changes:
        return changes, []

    snapshot = backup.write_backup(shape, live_product_id, live)
    print(f"Backed up the current state to {snapshot} before restoring")

    body = copy.deepcopy(live)
    body[shape.containers_key] = copy.deepcopy(wanted.get(shape.containers_key, []))
    _patch_resource(service, shape, creds, body, regions_version)

    after = fetch(service, creds)
    if after is None:
        return changes, ["Could not re-read the product to verify the restore"]
    return changes, backup.compare(wanted, after[0], shape, None)
