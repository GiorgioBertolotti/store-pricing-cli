"""Promotional/discount offers: a time-limited discounted offer on top of the already-live
base price (price_scaled.xlsx), on Google Play and/or App Store Connect.

Google Play: a monetization.subscriptions.basePlans.offers resource with a relativeDiscount
phase (percentage off whatever the base plan charges in each region), then activated. No
price computation needed - Google computes the resulting absolute price internally.

App Store Connect: Apple has no percentage-off concept, so for each country we take
Smart_Price_Native from price_scaled.xlsx, apply the discount, and resolve it to the
closest available subscriptionPricePoint (retrying in USD when Apple prices that territory
in USD, via the same resolve_price_point() apply.py's base price update uses). Per-territory
prices are sent as a JSON:API compound document: the "prices" relationship references
client-chosen ids wrapped as "${local-id}" (Apple's own 409 error dictates this exact
format - a plain id like "NOR" is rejected with ENTITY_ERROR.INCLUDED.INVALID_ID), and a
top-level "included" array supplies, for each id, a subscriptionPricePoint + territory
relationship. This shape was reverse-engineered from Apple's live error responses; it
isn't in Apple's published OpenAPI schema.

Apple's PATCH endpoint for this resource rejects the same payload shape its POST endpoint
accepts (also undocumented) - since only POST is proven to work, re-running with the same
offerCode deletes the existing offer and fully recreates it via POST, at the cost of a
brief window where the offer doesn't exist. find_existing_apple_offer() is exposed
separately so the caller can warn about this *before* committing to it. Apple also never
frees up a name/offerCode once used, even after deletion (undocumented) - a duplicate-
attribute 409 on the recreate POST is auto-retried with a "-<unix timestamp>" suffix
appended to both, rather than failing outright.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
import stripe
from googleapiclient.errors import HttpError

from store_pricing import stripe_platform
from store_pricing.apple import (
    BASE_URL,
    _request_with_rate_limit,
    get_jwt,
    get_subscription_id,
    resolve_price_point,
)
from store_pricing.config import AppleCreds, GoogleCreds, PricingConfig, StripeCreds
from store_pricing.google import (
    _NOT_BILLABLE_RE,
    build_service,
    fetch_subscription,
)
from store_pricing.inputs import InputData, get_alpha_2_country_code, get_alpha_3_country_code
from store_pricing.report import make_failure
from store_pricing.scaler import fetch_all_usd_rates

# duration key -> (human label, Apple SubscriptionOfferDuration enum, Google ISO 8601 period)
DURATIONS: list[tuple[str, str, str]] = [
    ("1 week", "ONE_WEEK", "P1W"),
    ("1 month", "ONE_MONTH", "P1M"),
    ("2 months", "TWO_MONTHS", "P2M"),
    ("3 months", "THREE_MONTHS", "P3M"),
    ("6 months", "SIX_MONTHS", "P6M"),
    ("1 year", "ONE_YEAR", "P1Y"),
]

# Apple offer payment modes. PAY_AS_YOU_GO was the only one the original script ever sent;
# the other two are now reachable but untested against a live discount offer.
OFFER_MODES = ["PAY_AS_YOU_GO", "PAY_UP_FRONT", "FREE_TRIAL"]


@dataclass(frozen=True)
class OfferConfig:
    platforms: set[str]
    discount_percent: float
    num_periods: int
    apple_duration: str
    google_duration: str
    offer_name: "str | None" = None
    offer_code: "str | None" = None
    google_offer_id: "str | None" = None
    offer_mode: str = "PAY_AS_YOU_GO"


def duration_by_label(label: str) -> "tuple[str, str] | None":
    for human, apple_d, google_d in DURATIONS:
        if human == label:
            return apple_d, google_d
    return None


# --- GOOGLE PLAY ---

# A relativeDiscount phase is a percentage off whatever price is currently live in that
# region, so Google computes the resulting absolute price internally at creation time - for
# a low-priced region a large discount can compute below Google's regional minimum. Unlike
# _NOT_BILLABLE_RE this isn't about the region being sellable at all, just that this
# specific discounted price isn't, so the fix is the same as a non-billable region: drop it
# from the offer and retry.
_OFFER_PRICE_OUT_OF_RANGE_RE = re.compile(
    r"Phase \d+ specified a price override in the region (\w+) that is out of the allowed price range"
)


def _remove_region(offer_body: dict, region_code: str) -> None:
    offer_body["regionalConfigs"] = [c for c in offer_body["regionalConfigs"] if c["regionCode"] != region_code]
    for phase in offer_body["phases"]:
        phase["regionalConfigs"] = [c for c in phase["regionalConfigs"] if c["regionCode"] != region_code]


def _create_offer_with_retries(service, creds: GoogleCreds, offer_body: dict, regions_version, max_retries: int = 50) -> list:
    """Create the offer, dropping regions Google reports as not billable or out of price
    range and retrying. Returns the list of region codes that were dropped."""
    removed_regions = []
    for attempt in range(max_retries):
        request = (
            service.monetization()
            .subscriptions()
            .basePlans()
            .offers()
            .create(
                packageName=creds.package_name,
                productId=creds.subscription_id,
                basePlanId=creds.baseplan_id,
                offerId=offer_body["offerId"],
                body=offer_body,
            )
        )
        sep = "&" if "?" in request.uri else "?"
        request.uri += f"{sep}regionsVersion.version={regions_version}"

        try:
            request.execute()
            return removed_regions
        except HttpError as e:
            if e.resp.status != 400:
                raise

            for pattern in (_NOT_BILLABLE_RE, _OFFER_PRICE_OUT_OF_RANGE_RE):
                match = pattern.search(str(e))
                if match:
                    region_code = match.group(1)
                    print(f"   Dropping region {region_code} from offer (attempt {attempt + 1})")
                    _remove_region(offer_body, region_code)
                    removed_regions.append(region_code)
                    break
            else:
                raise

    raise RuntimeError(f"Could not resolve all API errors after {max_retries} retries")


def create_google_offer(creds: GoogleCreds, data: InputData, config: OfferConfig, dry_run: bool = False) -> list:
    print("\nGoogle Play Promotional Offer")
    print("=" * 50)

    service = build_service(creds)
    result = fetch_subscription(service, creds)
    if result is None:
        return [make_failure("Google Play", "N/A", "N/A", None, None, "Could not fetch subscription")]
    _, regions_version = result

    region_codes = sorted({
        get_alpha_2_country_code(country, data.country_code_mapping)
        for country in data.country_prices
        if get_alpha_2_country_code(country, data.country_code_mapping)
    })

    offer_body = {
        "packageName": creds.package_name,
        "productId": creds.subscription_id,
        "basePlanId": creds.baseplan_id,
        "offerId": config.google_offer_id,
        "regionalConfigs": [{"regionCode": cc, "newSubscriberAvailability": True} for cc in region_codes],
        "phases": [{
            "recurrenceCount": config.num_periods,
            "duration": config.google_duration,
            "regionalConfigs": [
                {"regionCode": cc, "relativeDiscount": config.discount_percent / 100}
                for cc in region_codes
            ],
        }],
    }

    if dry_run:
        print(f"[dry-run] Would create offer '{config.google_offer_id}' covering {len(region_codes)} regions - no request sent")
        return []

    failures = []
    try:
        removed_regions = _create_offer_with_retries(service, creds, offer_body, regions_version)
        kept = len(region_codes) - len(removed_regions)
        print(f"Created offer '{config.google_offer_id}' in DRAFT state for {kept} regions")
        if removed_regions:
            print(f"   Excluded {len(removed_regions)} regions: {', '.join(removed_regions)}")
    except (HttpError, RuntimeError) as e:
        reason = f"Failed to create offer: {e}"
        print(reason)
        return [make_failure("Google Play", "N/A", "N/A", None, None, reason)]

    try:
        service.monetization().subscriptions().basePlans().offers().activate(
            packageName=creds.package_name, productId=creds.subscription_id,
            basePlanId=creds.baseplan_id, offerId=config.google_offer_id,
        ).execute()
        print(f"Activated offer '{config.google_offer_id}'")
    except HttpError as e:
        reason = f"Offer created but activation failed: {e}"
        print(reason)
        failures.append(make_failure("Google Play", "N/A", "N/A", None, None, reason))

    return failures


# --- APP STORE ---

# Every Apple request in this module goes through apple.py's shared rate limiter, the same
# one price-point resolution uses. These offer calls bracket a ~200-request resolution
# burst, so they hit the API exactly when it's most likely to be throttling; a bare
# requests.* call there fails outright on a 429 with no retry. Thin named wrappers rather
# than inline calls so tests can substitute them.

def _get_apple(url: str, headers: dict):
    return _request_with_rate_limit("GET", url, headers)


def _post_apple(url: str, headers: dict, body: dict):
    return _request_with_rate_limit("POST", url, headers, json=body)


def _delete_apple(url: str, headers: dict):
    return _request_with_rate_limit("DELETE", url, headers)


def find_existing_apple_offer(creds: AppleCreds, offer_code: str) -> "str | None":
    """Look up an existing promotional offer by offerCode. Exposed separately from
    create_apple_offer so callers can warn about the delete-and-recreate behaviour before
    committing to it."""
    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}
    subscription_id = get_subscription_id(token, creds)
    if not subscription_id:
        return None
    return _find_apple_offer_id(BASE_URL, headers, subscription_id, offer_code)


def _find_apple_offer_id(base_url: str, headers: dict, subscription_id: str, offer_code: str) -> "str | None":
    url = f"{base_url}/subscriptions/{subscription_id}/promotionalOffers?limit=200"
    while url:
        # Rate-limited like every other Apple call: create_apple_offer() fires this
        # immediately before (or after) resolving ~200 price points, so it lands right
        # where Apple's undocumented limit is most likely to be tripped. A bare
        # requests.get() here would 429 with no retry at all.
        resp = _get_apple(url, headers)
        if resp.status_code != 200:
            raise requests.RequestException(f"API Error {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        for offer in body.get("data", []):
            if offer.get("attributes", {}).get("offerCode") == offer_code:
                return offer["id"]
        url = body.get("links", {}).get("next")
    return None


def create_apple_offer(creds: AppleCreds, pricing: PricingConfig, data: InputData, config: OfferConfig, dry_run: bool = False) -> list:
    print("\nApple App Store Promotional Offer")
    print("=" * 50)

    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}
    subscription_id = get_subscription_id(token, creds)
    if not subscription_id:
        reason = f"Could not find subscription for app {creds.app_id}"
        print(reason)
        return [make_failure("Apple App Store", "N/A", "N/A", None, None, reason)]

    existing_offer_id = None
    try:
        existing_offer_id = _find_apple_offer_id(BASE_URL, headers, subscription_id, config.offer_code)
    except requests.RequestException as e:
        reason = f"Could not check for an existing offer with this code: {e}"
        print(reason)
        return [make_failure("Apple App Store", "N/A", "N/A", None, None, reason)]

    discount_factor = 1 - config.discount_percent / 100
    discounted_usd_prices = {c: v * discount_factor for c, v in data.country_prices_usd.items()}

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - territories Apple prices in a different currency than ours won't be retried")
        usd_rates = {}

    price_refs, included, failures = _resolve_offer_prices(
        token, subscription_id, pricing, data, discount_factor, discounted_usd_prices, usd_rates, dry_run
    )

    if not price_refs:
        print("No territories resolved to a price point - not creating the offer")
        return failures

    if dry_run:
        print(f"[dry-run] Would create offer '{config.offer_name}' ({config.offer_code}) covering {len(price_refs)} territories"
              + (" - would first delete the existing offer with this code" if existing_offer_id else "")
              + " - no request sent")
        return failures

    if existing_offer_id:
        print(f"Offer code '{config.offer_code}' already exists - deleting it to recreate from scratch")
        del_resp = _delete_apple(f"{BASE_URL}/subscriptionPromotionalOffers/{existing_offer_id}", headers)
        if del_resp.status_code != 204:
            reason = f"Could not delete existing offer before recreating it: API Error {del_resp.status_code}: {del_resp.text}"
            print(reason)
            return failures + [make_failure("Apple App Store", "N/A", "N/A", None, None, reason)]

    return failures + _post_offer_with_uniquify(headers, subscription_id, config, price_refs, included)


def _resolve_offer_prices(
    token, subscription_id, pricing: PricingConfig, data: InputData,
    discount_factor: float, discounted_usd_prices: dict, usd_rates: dict, dry_run: bool,
) -> "tuple[list, list, list]":
    """Resolve every country's discounted price to an Apple price point, concurrently.

    Returns (price_refs, included, failures) - the two halves of the JSON:API compound
    document plus one failure record per territory that couldn't be resolved.
    """
    resolvable, failures = [], []
    for country, price in data.country_prices.items():
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
        currency_code = data.country_currencies.get(country)
        if not country_code or not currency_code:
            failures.append(make_failure("Apple App Store", country, country_code or "Unknown", price, currency_code, "Missing country/currency code mapping"))
            continue
        resolvable.append((country, country_code, currency_code, float(price) * discount_factor))

    def _resolve_one(item):
        country, country_code, currency_code, target_price = item
        closest_point, _, _, apple_currency, api_error = resolve_price_point(
            token, subscription_id, country, country_code, target_price, currency_code, discounted_usd_prices,
            usd_rates=usd_rates, rounding=data.rounding or pricing.rounding,
        )
        return country, country_code, currency_code, target_price, closest_point, apple_currency, api_error

    price_refs, included = [], []
    with ThreadPoolExecutor(max_workers=pricing.apple_max_workers) as executor:
        for country, country_code, currency_code, target_price, closest_point, apple_currency, api_error in executor.map(_resolve_one, resolvable):
            verb = "Would set" if dry_run else "Resolving"
            print(f"{verb} promo price for {country} ({country_code}): target {target_price:.2f} {currency_code}")
            if not closest_point:
                # A transport error means "unknown", not "unpriceable" - keep them distinct
                # in the report so the operator knows which territories to re-run.
                reason = api_error or (
                    f"No price points available with currency {currency_code}"
                    if not apple_currency
                    else f"No price points available (Apple uses {apple_currency}, we have {currency_code})"
                )
                print(reason)
                failures.append(make_failure("Apple App Store", country, country_code, target_price, currency_code, reason))
                continue

            # Apple requires included-entity ids wrapped as "${local-id}" (client-scoped for
            # this request only). country_code (alpha-3) is unique per territory, so it
            # doubles as the local id linking this relationship entry to its "included" def.
            local_id = f"${{{country_code}}}"
            price_refs.append({"type": "subscriptionPromotionalOfferPrices", "id": local_id})
            included.append({
                "type": "subscriptionPromotionalOfferPrices",
                "id": local_id,
                "relationships": {
                    "subscriptionPricePoint": {"data": {"type": "subscriptionPricePoints", "id": closest_point["id"]}},
                    "territory": {"data": {"type": "territories", "id": country_code}},
                },
            })

    return price_refs, included, failures


def _post_offer_with_uniquify(headers, subscription_id, config: OfferConfig, price_refs: list, included: list, max_attempts: int = 5) -> list:
    """POST the offer, uniquifying name/offerCode on a duplicate-attribute 409.

    Apple never frees up a name/offerCode once used, even after the offer that used it is
    deleted. If we just deleted an offer with this exact name/offerCode, recreating with the
    same values is guaranteed to collide - on that specific 409, retry with a timestamp
    suffix instead of giving up.
    """
    offer_name, offer_code = config.offer_name, config.offer_code

    attempt = 0
    for attempt in range(max_attempts):
        body = {
            "data": {
                "type": "subscriptionPromotionalOffers",
                "attributes": {
                    "name": offer_name,
                    "offerCode": offer_code,
                    "duration": config.apple_duration,
                    "offerMode": config.offer_mode,
                    "numberOfPeriods": config.num_periods,
                },
                "relationships": {
                    "subscription": {"data": {"type": "subscriptions", "id": subscription_id}},
                    "prices": {"data": price_refs},
                },
            },
            "included": included,
        }
        resp = _post_apple(f"{BASE_URL}/subscriptionPromotionalOffers", headers, body)
        if resp.status_code == 201:
            print(f"Created promotional offer '{offer_name}' (offer code '{offer_code}') covering {len(price_refs)} territories")
            return []
        if resp.status_code == 409 and "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE" in resp.text:
            suffix = f"-{int(time.time())}"
            offer_name, offer_code = f"{config.offer_name}{suffix}", f"{config.offer_code}{suffix}"
            print(f"   Name/offer code already used (even though deleted) - retrying as '{offer_code}' (attempt {attempt + 1})")
            time.sleep(1)
            continue
        reason = f"API Error {resp.status_code}: {resp.text}"
        print(f"Failed to create offer: {reason}")
        return [make_failure("Apple App Store", "N/A", "N/A", None, None, reason)]

    reason = f"Could not find a free name/offer code after {attempt + 1} attempts"
    print(reason)
    return [make_failure("Apple App Store", "N/A", "N/A", None, None, reason)]


# --- STRIPE ---

# Stripe has no percentage-off-of-current-price concept scoped to a subscription the way
# Google's relativeDiscount is - a Coupon's percent_off applies uniformly regardless of
# currency, so (unlike Apple) no per-country price resolution is needed here at all.
#
# Coupons are immutable once created and Stripe has no per-region concept for them, so
# re-running with the same --code can't reuse or patch anything in place. Rather than
# guess at Stripe's delete-then-recreate semantics for a Coupon still referenced by a
# Promotion Code (undocumented, and unlike Apple's promotional offers there's no live-gap
# risk to a *new* coupon coexisting with an old one), a duplicate id/code is treated the
# same as Apple's duplicate-attribute 409: retried with a "-<unix timestamp>" suffix.


def _is_duplicate_error(e: "stripe.error.StripeError") -> bool:
    return e.code == "resource_already_exists" or "already exists" in str(e).lower()


def create_stripe_offer(creds: StripeCreds, config: OfferConfig, dry_run: bool = False) -> list:
    print("\nStripe Promotional Offer")
    print("=" * 50)

    months_per_cycle = stripe_platform.months_for_period(config.google_duration)
    if months_per_cycle is None:
        reason = (
            f"Stripe coupons need a whole number of months - '{config.google_duration}' has no "
            f"month equivalent (pick a monthly/yearly billing period for a Stripe offer)"
        )
        print(reason)
        return [make_failure("Stripe", "N/A", "N/A", None, None, reason)]

    total_months = months_per_cycle * config.num_periods
    coupon_id = config.offer_code
    promo_code = config.offer_code

    if dry_run:
        print(
            f"[dry-run] Would create coupon '{coupon_id}' ({config.discount_percent}% off for "
            f"{total_months} month(s)) and promotion code '{promo_code}' - no request sent"
        )
        return []

    client = stripe_platform.build_client(creds)

    coupon_params = {
        "id": coupon_id,
        "percent_off": config.discount_percent,
        "duration": "repeating",
        "duration_in_months": total_months,
    }
    if config.offer_name:
        coupon_params["name"] = config.offer_name

    coupon = None
    for attempt in range(5):
        try:
            coupon = client.v1.coupons.create(coupon_params)
            break
        except stripe.error.StripeError as e:
            if not _is_duplicate_error(e):
                reason = f"Could not create coupon: {e}"
                print(reason)
                return [make_failure("Stripe", "N/A", "N/A", None, None, reason)]
            suffix = f"-{int(time.time())}"
            coupon_params["id"] = f"{config.offer_code}{suffix}"
            promo_code = f"{config.offer_code}{suffix}"
            print(f"   Coupon id '{coupon_id}' already exists - retrying as '{coupon_params['id']}' (attempt {attempt + 1})")
            coupon_id = coupon_params["id"]
            time.sleep(1)

    if coupon is None:
        reason = f"Could not find a free coupon id after 5 attempts"
        print(reason)
        return [make_failure("Stripe", "N/A", "N/A", None, None, reason)]

    for attempt in range(5):
        try:
            client.v1.promotion_codes.create({
                "code": promo_code,
                "promotion": {"type": "coupon", "coupon": coupon["id"]},
            })
            print(
                f"Created Stripe coupon '{coupon['id']}' and promotion code '{promo_code}' "
                f"({config.discount_percent}% off for {total_months} month(s))"
            )
            return []
        except stripe.error.StripeError as e:
            if not _is_duplicate_error(e):
                reason = f"Coupon '{coupon['id']}' created but promotion code failed: {e}"
                print(reason)
                return [make_failure("Stripe", "N/A", "N/A", None, None, reason)]
            suffix = f"-{int(time.time())}"
            promo_code = f"{config.offer_code}{suffix}"
            print(f"   Promotion code already in use - retrying as '{promo_code}' (attempt {attempt + 1})")
            time.sleep(1)

    reason = f"Coupon '{coupon['id']}' created but could not find a free promotion code after 5 attempts"
    print(reason)
    return [make_failure("Stripe", "N/A", "N/A", None, None, reason)]
