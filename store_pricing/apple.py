"""App Store Connect price updates via the official REST API.

The App Store only allows prices from a fixed set of price points per territory;
get_closest_price_point() finds the nearest available one and the caller skips the update
if the difference exceeds `config.apple_tolerance_pct` or `config.apple_tolerance_units`.
Price-point resolution is one HTTP round-trip per country (pure I/O wait), so it runs
through a ThreadPoolExecutor sized by `config.apple_max_workers` instead of a sequential
loop - console output from concurrent countries will interleave since prints happen inside
worker threads.

Price updates need a future startDate; "tomorrow" (UTC) is sometimes rejected as still too
soon (undocumented minimum lead time) - _submit_price() reads Apple's own "must be on or
after YYYY-MM-DD" out of the 409 body and retries with that date instead of guessing a
fixed offset.

Ported from subscription_price_applier.py with credentials now passed explicitly as
AppleCreds rather than read from module globals, plus one bug fix: update_prices() used to
return an empty (non-failing) list when the subscription couldn't be found, which made a
misconfigured APPLE_SUBSCRIPTION_PRODUCT_ID look like "all succeeded". It now returns an
explicit failure record, matching the Google path.

One-time in-app purchase pricing (update_iap_prices() and friends, below the subscription
functions) uses a structurally different Apple API: price-point resolution mirrors the
subscription path almost exactly (same request/response shape, just under
/v2/inAppPurchases/.../pricePoints instead of /v1/subscriptions/.../pricePoints), but there
is no per-territory PATCH equivalent to subscriptionPrices - an in-app purchase has exactly
one InAppPurchasePriceSchedule, and the only documented way to change it is POST
/v1/inAppPurchasePriceSchedules with a manualPrices entry for *every* territory being
priced in one request. That makes update_iap_prices() all-or-nothing at the HTTP level:
if Apple rejects the schedule, none of the territories in that batch were updated, not
just the one that triggered the error - see the comment in update_iap_prices() for how
failures are reported for that case. This shape was confirmed against Apple's official
OpenAPI spec (developer.apple.com's published spec, cross-checked via
github.com/EvanBacon/App-Store-Connect-OpenAPI-Spec), but - unlike the subscription
offers payload in offers.py - has not been exercised against a live schedule that already
has prices, so submitting a second schedule after the first one hasn't been confirmed live.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import jwt
import requests

from store_pricing.config import AppleCreds, AppleOneTimeCreds, PricingConfig
from store_pricing.inputs import InputData, get_alpha_3_country_code
from store_pricing.report import make_failure
from store_pricing.scaler import convert_to_currency, fetch_all_usd_rates

BASE_URL = "https://api.appstoreconnect.apple.com/v1"
# One-time in-app purchase pricing (pricePoints, iapPriceSchedule) only exists under v2 -
# subscriptions still live entirely under v1.
BASE_URL_V2 = "https://api.appstoreconnect.apple.com/v2"

# Apple sometimes requires startDate to be further out than "tomorrow" (e.g. price changes
# already scheduled, or a stricter minimum lead time than documented); it tells us the
# earliest acceptable date in the 409 body, so we just retry with that instead of guessing.
_STARTDATE_TOO_SOON_RE = re.compile(r"must be on or after (\d{4}-\d{2}-\d{2})")


class _RateLimiter:
    """Coordinates request pacing across all worker threads hitting Apple's API.

    APPLE_MAX_WORKERS bounds how many requests can be *in flight* at once (for I/O
    overlap); this bounds how often a new one is *issued*, regardless of worker count.
    Apple's rate limit is undocumented, so a naive per-request retry isn't enough: with N
    threads all retrying independently on their own schedule, one thread's 429 usually
    means the others are seconds away from tripping the same limit too, and each retry just
    re-triggers it (observed in practice - dozens of 429s across a single resolution run
    even with a 3-attempt/2s-backoff retry). Sharing one limiter means a 429 anywhere backs
    off every thread, not just the one that hit it.
    """

    def __init__(self, min_interval: float):
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            start_at = max(self._next_allowed, now)
            self._next_allowed = start_at + self._min_interval
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)

    def backoff(self, seconds: float) -> None:
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.time() + seconds)


# ~5 requests/second aggregate across every worker thread combined.
_rate_limiter = _RateLimiter(min_interval=0.2)


def _retry_after_seconds(resp, attempt: int) -> float:
    value = resp.headers.get("Retry-After")
    if value is not None:
        try:
            return float(value)
        except ValueError:
            pass
    return float(2 ** (attempt + 1))


class _FailedRequest:
    """Stand-in for a requests.Response so callers' `resp.status_code`/`resp.text` checks
    keep working even when every retry raised a network-level exception rather than
    returning an HTTP response at all - without this, a single timeout deep inside a
    ThreadPoolExecutor worker propagates up and crashes the entire run instead of being
    recorded as one country's failure."""

    def __init__(self, text: str):
        self.status_code = 599
        self.text = text


def _request_with_rate_limit(method: str, url: str, headers: dict, json: "dict | None" = None, max_retries: int = 5):
    """GET/POST through the shared rate limiter, backing off every thread on a 429 and
    retrying instead of giving up after one shot. Also retries on network-level errors
    (timeouts, connection resets) - Apple's API got flaky under sustained load in practice
    (observed: ReadTimeout after a burst of 429s from heavy testing), not just 429s.
    """
    last_error = None
    for attempt in range(max_retries):
        _rate_limiter.wait()
        try:
            resp = requests.request(method, url, headers=headers, json=json, timeout=30)
        except requests.exceptions.RequestException as e:
            last_error = e
            _rate_limiter.backoff(2 ** attempt)
            continue

        if resp.status_code == 429:
            _rate_limiter.backoff(_retry_after_seconds(resp, attempt))
            if attempt < max_retries - 1:
                continue

        return resp

    return _FailedRequest(f"Request failed after {max_retries} attempts: {last_error}")


def _get_with_rate_limit(url: str, headers: dict, max_retries: int = 5):
    return _request_with_rate_limit("GET", url, headers, max_retries=max_retries)


def _post_with_rate_limit(url: str, headers: dict, json: dict, max_retries: int = 5):
    return _request_with_rate_limit("POST", url, headers, json=json, max_retries=max_retries)


def _make_failure(country, country_code, price, currency, reason) -> dict:
    return make_failure("Apple App Store", country, country_code, price, currency, reason)


def get_jwt(creds: "AppleCreds | AppleOneTimeCreds") -> str:
    headers = {"alg": "ES256", "kid": creds.key_id, "typ": "JWT"}
    payload = {"iss": creds.issuer_id, "exp": int(time.time()) + 1200, "aud": "appstoreconnect-v1"}
    private_key = creds.private_key.replace("\\n", "\n")
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def get_subscription_id(token: str, creds: AppleCreds) -> "str | None":
    headers = {"Authorization": f"Bearer {token}"}

    resp = _get_with_rate_limit(f"{BASE_URL}/apps/{creds.app_id}/subscriptionGroups", headers)
    if resp.status_code != 200:
        print(f"Error fetching subscription groups for {creds.app_id}: {resp.text}")
        return None

    for group in resp.json()["data"]:
        resp2 = _get_with_rate_limit(f"{BASE_URL}/subscriptionGroups/{group['id']}/subscriptions", headers)
        if resp2.status_code != 200:
            print(f"Error fetching subscriptions for {creds.app_id}: {resp2.text}")
            return None
        for sub in resp2.json()["data"]:
            if sub["attributes"]["productId"] == creds.subscription_product_id:
                return sub["id"]

    return None


def get_iap_id(token: str, creds: AppleOneTimeCreds) -> "str | None":
    """Find the in-app purchase's resource id by its productId. Unlike subscriptions
    (which need a subscriptionGroups -> subscriptions walk since productId isn't directly
    filterable there), inAppPurchasesV2 supports filter[productId] directly."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{BASE_URL}/apps/{creds.app_id}/inAppPurchasesV2?filter[productId]={creds.iap_product_id}&limit=1"
    resp = _get_with_rate_limit(url, headers)
    if resp.status_code != 200:
        print(f"Error fetching in-app purchases for {creds.app_id}: {resp.text}")
        return None

    data = resp.json().get("data", [])
    return data[0]["id"] if data else None


def get_closest_price_point(token, subscription_id, country_code, target_price, expected_currency=None):
    """Get the closest available price point for a territory.

    Returns (closest_point, actual_currency, api_error):
      - (point, currency, None)  a match was found
      - (None, currency, None)   territory currency differs from expected_currency
      - (None, None, None)       Apple genuinely lists no price points for this territory
      - (None, None, "...")      the request itself failed (rate limit, timeout, auth)

    That last case is why `api_error` exists as a separate return value rather than
    collapsing into the empty one. The shared rate limiter narrows the window but can't
    close it: after `max_retries` exhausted 429s (or a persistent ReadTimeout, which comes
    back as _FailedRequest) the call still comes back empty-handed, and reporting that as
    "no matching price point" tells the operator the territory is unpriceable when the
    real answer is "try again". They need to know which countries to re-run.
    """
    url = (
        f"{BASE_URL}/subscriptions/{subscription_id}/pricePoints"
        f"?include=territory&filter[territory]={country_code}&limit=1000"
    )
    return _closest_price_point_for_token(token, url, country_code, float(target_price), expected_currency)


def get_closest_iap_price_point(token, iap_id, country_code, target_price, expected_currency=None):
    """In-app purchase equivalent of get_closest_price_point() - same return contract, but
    against /v2/inAppPurchases/{id}/pricePoints (v2; subscriptions are v1)."""
    url = (
        f"{BASE_URL_V2}/inAppPurchases/{iap_id}/pricePoints"
        f"?include=territory&filter[territory]={country_code}&limit=1000"
    )
    return _closest_price_point_for_token(token, url, country_code, float(target_price), expected_currency)


def _closest_price_point_for_token(token: str, url: str, country_code: str, target_price: float, expected_currency: "str | None") -> tuple:
    headers = {"Authorization": f"Bearer {token}"}
    resp = _get_with_rate_limit(url, headers)

    if resp.status_code != 200:
        print(f"Error fetching price points for {country_code}: {resp.text}")
        return None, None, f"Apple API error {resp.status_code}: {resp.text[:200]}"

    response_data = resp.json()
    price_points = response_data["data"]
    territories = response_data.get("included", [])

    if not price_points or not territories:
        return None, None, None

    actual_currency = territories[0]["attributes"]["currency"]
    if expected_currency and actual_currency != expected_currency:
        return None, actual_currency, None

    closest_point, min_difference = None, float("inf")
    for point in sorted(price_points, key=lambda p: float(p["attributes"]["customerPrice"])):
        difference = abs(float(point["attributes"]["customerPrice"]) - target_price)
        if difference < min_difference:
            min_difference = difference
            closest_point = point
        elif closest_point is not None:
            break

    return closest_point, actual_currency, None


def _resolve_point(
    get_point_fn, token, entity_id, country, country_code, price, currency_code, country_prices_usd,
    usd_rates: "dict | None", rounding: str,
):
    """Shared by resolve_price_point (subscriptions) and resolve_iap_price_point (one-time
    in-app purchases): find the best price point, retrying in whatever currency Apple
    actually uses for this territory when it differs from ours. Apple doesn't always fall
    back to USD - it also prices several territories in EUR (observed: Serbia, Bosnia,
    Bulgaria) - so this converts our USD-equivalent price into *whichever* currency Apple
    reports via convert_to_currency() (the same conversion google.py uses for
    currency-locked Google regions), rather than only ever retrying in USD and giving up on
    anything else.

    Returns (closest_point, final_price, final_currency, apple_territory_currency,
    api_error) - see get_closest_price_point() for why the transport-level error is carried
    separately from "no match found".
    """
    closest_point, apple_currency, api_error = get_point_fn(token, entity_id, country_code, float(price), currency_code)
    if api_error:
        return None, price, currency_code, apple_currency, api_error

    if closest_point is None and apple_currency and apple_currency != currency_code:
        usd_price = country_prices_usd.get(country)
        converted_price = convert_to_currency(usd_price, apple_currency, usd_rates or {}, rounding)
        if converted_price is not None:
            print(f"   Apple uses {apple_currency} for {country_code}, retrying with {converted_price:.2f} {apple_currency}")
            closest_point, _, api_error = get_point_fn(token, entity_id, country_code, converted_price, apple_currency)
            if api_error:
                return None, converted_price, apple_currency, apple_currency, api_error
            if closest_point is not None:
                return closest_point, converted_price, apple_currency, apple_currency, None
        else:
            print(f"   Apple uses {apple_currency} for {country_code} (we have {currency_code}) — no rate available, skipping")

    return closest_point, price, currency_code, apple_currency, None


def resolve_price_point(
    token, subscription_id, country, country_code, price, currency_code, country_prices_usd,
    usd_rates: "dict | None" = None, rounding: str = "psychological",
):
    return _resolve_point(
        get_closest_price_point, token, subscription_id, country, country_code, price, currency_code,
        country_prices_usd, usd_rates, rounding,
    )


def resolve_iap_price_point(
    token, iap_id, country, country_code, price, currency_code, country_prices_usd,
    usd_rates: "dict | None" = None, rounding: str = "psychological",
):
    """In-app purchase equivalent of resolve_price_point() - same retry-in-Apple's-currency
    logic, against get_closest_iap_price_point()."""
    return _resolve_point(
        get_closest_iap_price_point, token, iap_id, country, country_code, price, currency_code,
        country_prices_usd, usd_rates, rounding,
    )


def _build_price_payload(subscription_id: str, price_point_id: str, start_date: "str | None") -> dict:
    attributes: dict = {"preserveCurrentPrice": True}
    if start_date is not None:
        attributes["startDate"] = start_date
    return {
        "data": {
            "type": "subscriptionPrices",
            "attributes": attributes,
            "relationships": {
                "subscription": {"data": {"id": subscription_id, "type": "subscriptions"}},
                "subscriptionPricePoint": {"data": {"id": price_point_id, "type": "subscriptionPricePoints"}},
            },
        }
    }


def _submit_price(headers, subscription_id, price_point_id, max_retries=5):
    """POST a price update, retrying without startDate if the territory has no existing
    price, or with Apple's own minimum startDate if "tomorrow" is rejected as too soon."""
    start_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    for _ in range(max_retries):
        resp = _post_with_rate_limit(
            f"{BASE_URL}/subscriptionPrices",
            headers, _build_price_payload(subscription_id, price_point_id, start_date),
        )
        if resp.status_code != 409:
            return resp

        if "Create a starting price" in resp.text:
            return _post_with_rate_limit(
                f"{BASE_URL}/subscriptionPrices",
                headers, _build_price_payload(subscription_id, price_point_id, None),
            )

        match = _STARTDATE_TOO_SOON_RE.search(resp.text)
        if not match:
            return resp
        start_date = match.group(1)

    return resp


def _update_country(
    token, subscription_id, headers, country, price, data: InputData, config: PricingConfig, dry_run: bool,
    usd_rates: "dict | None" = None,
) -> tuple:
    """Process one country's price update. Returns (succeeded: bool, failure: dict | None)."""
    try:
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
    except Exception as e:
        reason = f"Error getting country code: {e}"
        return False, _make_failure(country, "Unknown", price, data.country_currencies.get(country, "Unknown"), reason)

    currency_code = data.country_currencies.get(country)
    if not currency_code:
        return False, _make_failure(country, country_code, price, "Unknown", "Error getting currency code")

    closest_point, final_price, final_currency, apple_currency, api_error = resolve_price_point(
        token, subscription_id, country, country_code, price, currency_code, data.country_prices_usd,
        usd_rates=usd_rates, rounding=data.rounding or config.rounding,
    )

    if api_error:
        return False, _make_failure(country, country_code, price, currency_code, api_error)

    if not closest_point:
        reason = (
            f"No price points available with currency {currency_code}"
            if not apple_currency
            else f"No price points available (Apple uses {apple_currency}, we have {currency_code})"
        )
        return False, _make_failure(country, country_code, price, currency_code, reason)

    closest_price = float(closest_point["attributes"]["customerPrice"])
    price_difference = abs(closest_price - float(final_price))
    tolerance = max(float(final_price) * config.apple_tolerance_pct, config.apple_tolerance_units)

    if price_difference > tolerance:
        reason = f"Price difference too large: {price_difference:.2f} (max allowed: {tolerance:.2f})"
        return False, _make_failure(country, country_code, price, currency_code, reason)

    if dry_run:
        print(f"[dry-run] Would set {country} ({country_code}) to {closest_price} {final_currency}")
        return True, None

    resp = _submit_price(headers, subscription_id, closest_point["id"])
    if resp.status_code == 201:
        print(f"Updated price for {country}")
        return True, None

    reason = f"API Error {resp.status_code}: {resp.text}"
    return False, _make_failure(country, country_code, price, currency_code, reason)


def update_prices(creds: AppleCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}

    subscription_id = get_subscription_id(token, creds)
    if not subscription_id:
        reason = f"Could not find subscription for app {creds.app_id} - check APPLE_SUBSCRIPTION_PRODUCT_ID"
        print(f"{reason}")
        return [_make_failure("N/A", "N/A", None, None, reason)]

    print(f"{'[dry-run] Resolving' if dry_run else 'Updating'} App Store prices for {len(data.country_prices)} countries...")

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - territories Apple prices in a different currency than ours won't be retried")
        usd_rates = {}

    updated_count = api_error_count = not_found_count = 0
    failures = []

    with ThreadPoolExecutor(max_workers=config.apple_max_workers) as executor:
        results = executor.map(
            lambda item: _update_country(token, subscription_id, headers, item[0], item[1], data, config, dry_run, usd_rates),
            data.country_prices.items(),
        )
        for succeeded, failure in results:
            if succeeded:
                updated_count += 1
            elif failure:
                failures.append(failure)
                if "No price points" in failure["reason"] or "Price difference" in failure["reason"]:
                    not_found_count += 1
                else:
                    api_error_count += 1

    verb = "would update" if dry_run else "updated"
    print(f"App Store: {updated_count} {verb}, {api_error_count} API errors, {not_found_count} no suitable price point")

    # Throttling and timeouts are transient, unlike "this territory has no matching price
    # point" - worth calling out so the operator re-runs instead of assuming those
    # countries are permanently unpriceable.
    retryable = sum(1 for f in failures if "Apple API error" in f["reason"])
    if retryable:
        print(f"   {retryable} of those were transport errors (rate limit / timeout) - re-running should pick them up")

    return failures


def fetch_live_prices(creds: AppleCreds, data: InputData, config: "PricingConfig | None" = None) -> dict[str, dict]:
    """Return {country: {"price": float, "currency": str}} for each territory's current
    live price - used for the apply-time diff against what's about to be pushed.

    One HTTP round-trip per country, same as price-point resolution, so it runs through
    the same ThreadPoolExecutor sizing instead of a sequential loop - a sequential version
    of this against ~150+ territories is slow enough to look hung.
    """
    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}
    subscription_id = get_subscription_id(token, creds)
    if not subscription_id:
        return {}

    max_workers = config.apple_max_workers if config else 8

    def _fetch_one(country: str) -> "tuple[str, dict | None]":
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
        if not country_code:
            return country, None
        url = (
            f"{BASE_URL}/subscriptions/{subscription_id}/prices"
            f"?filter[territory]={country_code}&include=subscriptionPricePoint,territory&limit=1"
        )
        resp = _get_with_rate_limit(url, headers)
        if resp.status_code != 200:
            return country, None
        included = resp.json().get("included", [])
        # subscriptionPricePoint attributes carry the price; currency lives on the
        # territory relationship (same split as get_closest_price_point()) - Apple
        # sometimes prices a territory in USD instead of its local currency, so this
        # can legitimately differ from data.country_currencies for that country.
        price_point = next((i for i in included if i.get("type") == "subscriptionPricePoints"), None)
        territory = next((i for i in included if i.get("type") == "territories"), None)
        if not price_point:
            return country, None
        currency = territory["attributes"]["currency"] if territory else None
        return country, {"price": float(price_point["attributes"]["customerPrice"]), "currency": currency}

    live = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for country, entry in executor.map(_fetch_one, data.country_prices):
            if entry is not None:
                live[country] = entry
    return live


# --- ONE-TIME IN-APP PURCHASES ---
#
# See the module docstring for why this section can't mirror update_prices()'s per-country
# submission model: Apple has no per-territory PATCH for a one-time IAP's prices, only a
# single POST that replaces the whole price schedule at once.

def _make_iap_failure(country, country_code, price, currency, reason) -> dict:
    # A distinct platform label from _make_failure()'s "Apple App Store" - otherwise a run
    # that applies both the subscription and the one-time purchase in the same invocation
    # merges their failures into one indistinguishable "Apple App Store" bucket in the
    # failure report and the console summary table (report.write_failure_report() and
    # ui.failure_summary_table() both group by this exact string).
    return make_failure("Apple App Store (One-time)", country, country_code, price, currency, reason)


def _build_iap_price_schedule_payload(iap_id: str, base_territory: str, resolved: "list[tuple[str, str, str]]") -> dict:
    """Build the POST /v1/inAppPurchasePriceSchedules body. `resolved` is a list of
    (country, territory_code, price_point_id) for every territory to include - the id
    wrapping ("${territory_code}") mirrors offers.py's subscriptionPromotionalOfferPrices
    payload, which Apple's own error responses dictated (a plain id is rejected with
    ENTITY_ERROR.INCLUDED.INVALID_ID); territory_code is unique per territory so it doubles
    as the local id linking each relationship entry to its "included" definition."""
    manual_prices, included = [], []
    for _, territory_code, price_point_id in resolved:
        local_id = f"${{{territory_code}}}"
        manual_prices.append({"type": "inAppPurchasePrices", "id": local_id})
        included.append({
            "type": "inAppPurchasePrices",
            "id": local_id,
            "attributes": {"startDate": None},
            "relationships": {
                "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}},
                "inAppPurchasePricePoint": {"data": {"type": "inAppPurchasePricePoints", "id": price_point_id}},
            },
        })

    return {
        "data": {
            "type": "inAppPurchasePriceSchedules",
            "relationships": {
                "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                "baseTerritory": {"data": {"type": "territories", "id": base_territory}},
                "manualPrices": {"data": manual_prices},
            },
        },
        "included": included,
    }


def _resolve_iap_country(token, iap_id, country, price, data: InputData, config: PricingConfig, usd_rates) -> tuple:
    """Resolve one country's price to an Apple price point. Returns
    (country, territory_code, price_point_id, currency_code, failure) - price_point_id is
    None when resolution failed, in which case `failure` carries the reason."""
    try:
        country_code = get_alpha_3_country_code(country, data.country_code_mapping)
    except Exception as e:
        reason = f"Error getting country code: {e}"
        return country, None, None, None, _make_iap_failure(country, "Unknown", price, data.country_currencies.get(country, "Unknown"), reason)

    currency_code = data.country_currencies.get(country)
    if not currency_code:
        return country, country_code, None, None, _make_iap_failure(country, country_code, price, "Unknown", "Error getting currency code")

    closest_point, final_price, final_currency, apple_currency, api_error = resolve_iap_price_point(
        token, iap_id, country, country_code, price, currency_code, data.country_prices_usd,
        usd_rates=usd_rates, rounding=data.rounding or config.rounding,
    )

    if api_error:
        return country, country_code, None, currency_code, _make_iap_failure(country, country_code, price, currency_code, api_error)

    if not closest_point:
        reason = (
            f"No price points available with currency {currency_code}"
            if not apple_currency
            else f"No price points available (Apple uses {apple_currency}, we have {currency_code})"
        )
        return country, country_code, None, currency_code, _make_iap_failure(country, country_code, price, currency_code, reason)

    closest_price = float(closest_point["attributes"]["customerPrice"])
    price_difference = abs(closest_price - float(final_price))
    tolerance = max(float(final_price) * config.apple_tolerance_pct, config.apple_tolerance_units)

    if price_difference > tolerance:
        reason = f"Price difference too large: {price_difference:.2f} (max allowed: {tolerance:.2f})"
        return country, country_code, None, currency_code, _make_iap_failure(country, country_code, price, currency_code, reason)

    return country, country_code, closest_point["id"], currency_code, None


def update_iap_prices(creds: AppleOneTimeCreds, config: PricingConfig, data: InputData, dry_run: bool = False) -> list[dict]:
    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}

    iap_id = get_iap_id(token, creds)
    if not iap_id:
        reason = f"Could not find in-app purchase for app {creds.app_id} - check APPLE_IAP_PRODUCT_ID"
        print(reason)
        return [_make_iap_failure("N/A", "N/A", None, None, reason)]

    # A price schedule's baseTerritory is required by Apple and has no independent meaning
    # here beyond "which territory anchors this schedule" - config.anchor_country already
    # plays that role for PPP scaling, so it's the natural choice.
    base_territory = get_alpha_3_country_code(config.anchor_country, data.country_code_mapping)
    if not base_territory:
        reason = f"Could not resolve a territory code for anchor country '{config.anchor_country}' - check country_codes.json"
        print(reason)
        return [_make_iap_failure("N/A", "N/A", None, None, reason)]

    print(f"{'[dry-run] Resolving' if dry_run else 'Updating'} in-app purchase prices for {len(data.country_prices)} countries...")

    try:
        usd_rates = fetch_all_usd_rates()
    except Exception as e:
        print(f"Warning: could not fetch exchange rates ({e}) - territories Apple prices in a different currency than ours won't be retried")
        usd_rates = {}

    resolved: list = []  # (country, territory_code, price_point_id, currency_code)
    failures: list = []
    with ThreadPoolExecutor(max_workers=config.apple_max_workers) as executor:
        results = executor.map(
            lambda item: _resolve_iap_country(token, iap_id, item[0], item[1], data, config, usd_rates),
            data.country_prices.items(),
        )
        for country, territory_code, price_point_id, currency_code, failure in results:
            if price_point_id:
                resolved.append((country, territory_code, price_point_id, currency_code))
            elif failure:
                failures.append(failure)

    if not resolved:
        print("No territories resolved to a price point - not submitting a price schedule")
        return failures

    if dry_run:
        print(f"[dry-run] Would submit one price schedule covering {len(resolved)} territories (base territory {base_territory}) - no request sent")
        return failures

    payload = _build_iap_price_schedule_payload(iap_id, base_territory, [(c, tc, pid) for c, tc, pid, _ in resolved])
    resp = _post_with_rate_limit(f"{BASE_URL}/inAppPurchasePriceSchedules", headers, payload)
    if resp.status_code == 201:
        print(f"In-app purchase: submitted one price schedule covering {len(resolved)} territories, {len(failures)} could not be resolved")
        return failures

    # Unlike subscriptionPrices (one POST per territory), a one-time IAP's whole price
    # schedule is replaced by a single POST - a rejection here means none of the batched
    # territories were actually updated, not just the one that triggered the error, so
    # every one of them gets its own failure record rather than a single aggregate error.
    reason = f"API Error {resp.status_code}: {resp.text}"
    print(f"Failed to submit price schedule covering {len(resolved)} territories: {reason}")
    failures += [
        _make_iap_failure(country, territory_code, data.country_prices.get(country), currency_code, reason)
        for country, territory_code, _, currency_code in resolved
    ]
    return failures


def fetch_live_iap_prices(creds: AppleOneTimeCreds, data: InputData, config: "PricingConfig | None" = None) -> dict[str, dict]:
    """Return {country: {"price": float, "currency": str}} for each territory's current
    live in-app purchase price - used for the apply-time diff against what's about to be
    pushed.

    Unlike subscriptions (one .../prices GET per territory), a one-time IAP's live prices
    all live on its single price schedule: fetched once via iapPriceSchedule, then its
    manualPrices relationship pages through every territory's current price - no
    ThreadPoolExecutor needed here, this is a handful of paginated requests total rather
    than one per country.
    """
    token = get_jwt(creds)
    headers = {"Authorization": f"Bearer {token}"}
    iap_id = get_iap_id(token, creds)
    if not iap_id:
        return {}

    resp = _get_with_rate_limit(f"{BASE_URL_V2}/inAppPurchases/{iap_id}/iapPriceSchedule", headers)
    if resp.status_code != 200:
        # A brand-new in-app purchase with no price schedule yet reads as "not found" here -
        # same as fetch_live_prices() returning {} when the subscription itself isn't found.
        return {}
    schedule_id = resp.json().get("data", {}).get("id")
    if not schedule_id:
        return {}

    territory_to_country = {}
    for country in data.country_prices:
        code = get_alpha_3_country_code(country, data.country_code_mapping)
        if code:
            territory_to_country[code] = country

    live = {}
    url = (
        f"{BASE_URL}/inAppPurchasePriceSchedules/{schedule_id}/manualPrices"
        f"?include=inAppPurchasePricePoint,territory&limit=200"
    )
    while url:
        resp = _get_with_rate_limit(url, headers)
        if resp.status_code != 200:
            break
        body = resp.json()
        included = body.get("included", [])
        price_points = {i["id"]: i for i in included if i.get("type") == "inAppPurchasePricePoints"}
        territories = {i["id"]: i for i in included if i.get("type") == "territories"}

        for price in body.get("data", []):
            point_ref = price.get("relationships", {}).get("inAppPurchasePricePoint", {}).get("data")
            territory_ref = price.get("relationships", {}).get("territory", {}).get("data")
            if not point_ref or not territory_ref:
                continue
            point = price_points.get(point_ref["id"])
            territory = territories.get(territory_ref["id"])
            country_name = territory_to_country.get(territory_ref["id"])
            if not point or not country_name:
                continue
            live[country_name] = {
                "price": float(point["attributes"]["customerPrice"]),
                "currency": territory["attributes"]["currency"] if territory else None,
            }

        url = body.get("links", {}).get("next")

    return live
