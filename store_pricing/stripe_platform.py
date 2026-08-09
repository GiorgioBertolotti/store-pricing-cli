"""Stripe regional prices - subscriptions and one-time purchases alike.

Unlike Apple/Google, a Stripe Price has no per-country pricing - only per-currency, via
`currency_options` on a single Price object. The Price's own default currency/unit_amount
is immutable once created (Stripe Prices in general are immutable); `currency_options` is
the one field on an existing Price the API still lets you patch, so that's what this
module writes to. This mechanism works identically whether the Price is recurring
(subscription) or `type=one_time` (one-time purchase) - Stripe doesn't distinguish between
the two for currency_options at all, so update_prices()/update_onetime_prices() and
fetch_live_prices()/fetch_live_onetime_prices() share one implementation
(_update_prices_for()/_fetch_live_prices_for()) parameterized only by which fetch_*_price()
function validates the Price's recurring-vs-one-time-ness and which platform label its
failures get tagged with.

Pricing is therefore resolved by currency, not by country: every country sharing a
currency gets that currency's one Stripe amount. Where PPP scaling would want different
prices for countries sharing a currency (e.g. the eurozone), one has to win - the
anchor country's price if the anchor is in that currency group (so the currency the
anchor itself is priced in is never discounted), otherwise the group's PPP-scaled
prices are averaged.

Countries priced in the base Price's own currency can't be represented via
currency_options at all - they come back from resolve_currency_options() as "locked",
the same concept apply_flow already shows for Google's currency-locked regions.

Promotional offers (offers.py) stay subscription-only: a Stripe Coupon's
duration_in_months needs a recurring billing cadence to discount across, which a one-time
purchase doesn't have.
"""

from __future__ import annotations

import re
import time
from statistics import mean

import stripe

from store_pricing.config import PricingConfig, StripeCreds, StripeOneTimeCreds
from store_pricing.inputs import InputData
from store_pricing.report import make_failure

# Currencies Stripe bills in whole units instead of the usual /100 minor unit.
# https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL_CURRENCIES = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
    "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

# Stripe coupon duration_in_months must be a whole number of months - a weekly billing
# period (P1W) has no exact month equivalent, so it can't be expressed as a Stripe coupon.
PERIOD_MONTHS = {"P1M": 1, "P2M": 2, "P3M": 3, "P6M": 6, "P1Y": 12}


def months_for_period(google_duration: str) -> "int | None":
    return PERIOD_MONTHS.get(google_duration)


def _make_failure(country, country_code, price, currency, reason) -> dict:
    return make_failure("Stripe", country, country_code, price, currency, reason)


def _make_onetime_failure(country, country_code, price, currency, reason) -> dict:
    # A distinct platform label from _make_failure()'s "Stripe" - otherwise a run that
    # applies both the subscription and the one-time Price in the same invocation merges
    # their failures into one indistinguishable "Stripe" bucket in the failure report and
    # the console summary table (same fix as apple._make_iap_failure()/
    # google._make_onetime_failure()).
    return make_failure("Stripe (One-time)", country, country_code, price, currency, reason)


def build_client(creds: "StripeCreds | StripeOneTimeCreds") -> stripe.StripeClient:
    return stripe.StripeClient(api_key=creds.secret_key)


def to_smallest_unit(amount: float, currency: str) -> int:
    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return round(amount)
    return round(amount * 100)


def from_smallest_unit(amount: int, currency: str) -> float:
    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return float(amount)
    return amount / 100


def _fetch_price(creds, *, require_recurring: bool) -> "tuple":
    """Retrieve the configured Price. Returns (price, error) - error is None on success.

    `expand=["currency_options"]` is required - Stripe omits that field from the response
    by default, which would otherwise make fetch_live_prices() see every non-base currency
    as unset.
    """
    client = build_client(creds)
    try:
        price = client.v1.prices.retrieve(creds.price_id, {"expand": ["currency_options"]})
    except stripe.error.StripeError as e:
        return None, f"Could not fetch Stripe Price '{creds.price_id}': {e}"

    # stripe-python's StripeObject no longer subclasses dict (as of the v8 SDK rewrite),
    # so it has no .get() - only attribute/item access (__getattr__ proxies to __getitem__
    # and raises AttributeError, which is what makes getattr()'s default work here).
    is_recurring = bool(getattr(price, "recurring", None))
    if require_recurring and not is_recurring:
        return None, f"Stripe Price '{creds.price_id}' is not a recurring price - a subscription needs a recurring Price"
    if not require_recurring and is_recurring:
        return None, f"Stripe Price '{creds.price_id}' is a recurring price - a one-time purchase needs a one-time Price"

    return price, None


def fetch_price(creds: StripeCreds) -> "tuple":
    return _fetch_price(creds, require_recurring=True)


def fetch_onetime_price(creds: StripeOneTimeCreds) -> "tuple":
    return _fetch_price(creds, require_recurring=False)


def group_by_currency(data: InputData) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for country in data.country_prices:
        currency = data.country_currencies.get(country)
        if not currency:
            continue
        groups.setdefault(currency.upper(), []).append(country)
    return groups


def _resolve_group_price(countries: list[str], data: InputData, anchor_country: str) -> float:
    if len(countries) == 1:
        return data.country_prices[countries[0]]
    if anchor_country in countries:
        return data.country_prices[anchor_country]
    return mean(data.country_prices[c] for c in countries)


def resolve_currency_options(
    data: InputData, pricing: PricingConfig, base_currency: str, price_id_var: str = "STRIPE_PRICE_ID",
) -> "tuple[dict, dict]":
    """Build the currency_options patch plus the country -> reason map of countries that
    can't be represented (those priced in the base Price's own currency).

    Returns ({currency_lower: {"unit_amount": int}}, {country: reason}).

    `price_id_var` only changes the locked-country message's suggested fix - pass
    "STRIPE_ONETIME_PRICE_ID" for the one-time Price path so the advice points at the right
    .env variable, since both share this function's currency-collision logic verbatim.
    """
    base_currency = base_currency.upper()
    groups = group_by_currency(data)

    currency_options: dict[str, dict] = {}
    locked: dict[str, str] = {}

    for currency, countries in groups.items():
        if currency == base_currency:
            reason = (
                f"Stripe's base Price currency ({base_currency}) can't be changed in place via "
                f"currency_options - create a new Price to change it, then update {price_id_var}"
            )
            for country in countries:
                locked[country] = reason
            continue

        price = _resolve_group_price(countries, data, pricing.anchor_country)
        currency_options[currency.lower()] = {"unit_amount": to_smallest_unit(price, currency)}

    return currency_options, locked


def _fetch_live_prices_for(creds, fetch_fn, data: InputData) -> dict[str, dict]:
    """{country: {"price": float, "currency": str}} for every country we can read back -
    the base Price's own currency/unit_amount for countries priced in it, its
    currency_options for everyone else. Used for the apply-time diff. Shared by
    fetch_live_prices() (subscription) and fetch_live_onetime_prices() (one-time) -
    currency_options works identically on both kinds of Price."""
    price, error = fetch_fn(creds)
    if error:
        return {}

    base_currency = price["currency"].upper()
    options = getattr(price, "currency_options", None) or {}

    live = {}
    for country, currency in data.country_currencies.items():
        if not currency:
            continue
        currency_upper = currency.upper()
        if currency_upper == base_currency:
            live[country] = {"price": from_smallest_unit(price["unit_amount"], base_currency), "currency": base_currency}
            continue
        entry = getattr(options, currency.lower(), None)
        if entry is not None:
            live[country] = {"price": from_smallest_unit(entry["unit_amount"], currency_upper), "currency": currency_upper}

    return live


def fetch_live_prices(creds: StripeCreds, data: InputData) -> dict[str, dict]:
    return _fetch_live_prices_for(creds, fetch_price, data)


def fetch_live_onetime_prices(creds: StripeOneTimeCreds, data: InputData) -> dict[str, dict]:
    return _fetch_live_prices_for(creds, fetch_onetime_price, data)


_INVALID_CURRENCY_RE = re.compile(r"Invalid currency:\s*(\w+)")


def _dropped_currency(e: "stripe.error.StripeError") -> "str | None":
    """Stripe names the offending parameter as e.g. "currency_options[eur][unit_amount]"
    on `.param` - pull the currency code out of that rather than pattern-matching the
    human-readable message, which Stripe doesn't document the wording of.

    A currency Stripe doesn't support at all (e.g. IQD - not in Stripe's supported-currency
    list as of this writing) comes back as a top-level "Invalid currency: iqd" error instead,
    with `.param` unset or not in the `currency_options[xxx]` shape - so that case needs the
    message-text fallback below, or one bad currency aborts the whole batch instead of just
    that one currency being dropped.
    """
    param = getattr(e, "param", None)
    if param and param.startswith("currency_options["):
        return param.split("[", 2)[1].split("]", 1)[0]
    match = _INVALID_CURRENCY_RE.search(str(e))
    return match.group(1).lower() if match else None


def _update_prices_for(
    creds, fetch_fn, make_failure_fn, price_id_var: str,
    config: PricingConfig, data: InputData, dry_run: bool = False,
) -> list[dict]:
    """Shared by update_prices() (subscription) and update_onetime_prices() (one-time) -
    the currency_options patch-and-retry-dropping-rejected-currencies ladder doesn't care
    whether the underlying Price is recurring or one-time."""
    price, error = fetch_fn(creds)
    if error:
        print(error)
        return [make_failure_fn("N/A", "N/A", None, None, error)]

    base_currency = price["currency"].upper()
    groups = group_by_currency(data)
    currency_options, locked = resolve_currency_options(data, config, base_currency, price_id_var)

    failures = [
        make_failure_fn(country, data.country_currencies.get(country), data.country_prices.get(country), base_currency, reason)
        for country, reason in locked.items()
    ]

    if not currency_options:
        print("No currencies to update on Stripe (every country shares the base Price's currency).")
        return failures

    country_count = sum(len(c) for cur, c in groups.items() if cur != base_currency)
    verb = "Would update" if dry_run else "Updating"
    print(f"{verb} {len(currency_options)} currencies on Stripe Price '{creds.price_id}' (covering {country_count} countries)")

    if dry_run:
        return failures

    client = build_client(creds)
    remaining = dict(currency_options)
    currency_to_countries = {c.lower(): countries for c, countries in groups.items()}

    for attempt in range(len(remaining) + 1):
        try:
            client.v1.prices.update(creds.price_id, {"currency_options": remaining})
            print(f"Updated {len(remaining)} currencies on Stripe Price '{creds.price_id}'")
            return failures
        except stripe.error.StripeError as e:
            dropped = _dropped_currency(e)
            if dropped is None or dropped not in remaining:
                reason = f"API error: {e}"
                print(reason)
                return failures + [make_failure_fn("N/A", "N/A", None, None, reason)]

            del remaining[dropped]
            affected = currency_to_countries.get(dropped, [])
            print(f"   Stripe rejected currency {dropped.upper()} - dropping it and retrying (attempt {attempt + 1})")
            failures += [
                make_failure_fn(country, dropped.upper(), data.country_prices.get(country), dropped.upper(), f"Rejected by Stripe: {e}")
                for country in affected
            ]
            if not remaining:
                return failures

    return failures + [make_failure_fn("N/A", "N/A", None, None, "Could not resolve all API errors - too many currencies rejected")]


def update_prices(creds: StripeCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    return _update_prices_for(creds, fetch_price, _make_failure, "STRIPE_PRICE_ID", config, data, dry_run)


def update_onetime_prices(creds: StripeOneTimeCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    return _update_prices_for(creds, fetch_onetime_price, _make_onetime_failure, "STRIPE_ONETIME_PRICE_ID", config, data, dry_run)
