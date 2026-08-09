"""Stripe prices by currency, not by country (only currency_options on a Price can be
patched after creation) - resolve_currency_options()'s collision rule (anchor wins, else
average) and the base-currency lock are the load-bearing logic here, plus the retry
ladder that drops a currency Stripe rejects rather than failing the whole patch."""

import stripe

from store_pricing.config import PricingConfig, StripeCreds, StripeOneTimeCreds
from store_pricing.inputs import InputData
from store_pricing.offers import OfferConfig, create_stripe_offer
from store_pricing.stripe_platform import (
    _dropped_currency,
    fetch_onetime_price,
    fetch_price,
    from_smallest_unit,
    months_for_period,
    resolve_currency_options,
    to_smallest_unit,
    update_onetime_prices,
    update_prices,
)


def _stripe_error(param=None, code=None, message="error"):
    return stripe.error.InvalidRequestError(message, param, code)


# --- smallest-unit conversion ---

def test_normal_currency_converts_to_cents():
    assert to_smallest_unit(19.99, "USD") == 1999
    assert from_smallest_unit(1999, "USD") == 19.99


def test_zero_decimal_currency_is_not_multiplied():
    assert to_smallest_unit(1500, "JPY") == 1500
    assert from_smallest_unit(1500, "JPY") == 1500.0
    assert to_smallest_unit(1500, "jpy") == 1500  # case-insensitive


# --- currency collision resolution ---

def _data(prices: dict, currencies: dict) -> InputData:
    return InputData(
        country_prices=prices,
        country_prices_usd={c: p / 1.0 for c, p in prices.items()},
        country_currencies=currencies,
        country_code_mapping={},
    )


def test_single_country_currency_uses_its_own_price():
    data = _data({"Norway": 99.0}, {"Norway": "NOK"})
    options, locked = resolve_currency_options(data, PricingConfig(), base_currency="USD")

    assert options["nok"]["unit_amount"] == 9900
    assert locked == {}


def test_shared_currency_prefers_the_anchor_countrys_price():
    data = _data(
        {"Germany": 8.99, "France": 7.99, "United States": 9.99},
        {"Germany": "EUR", "France": "EUR", "United States": "USD"},
    )
    config = PricingConfig(anchor_country="Germany")

    options, locked = resolve_currency_options(data, config, base_currency="USD")

    # Germany is the anchor and shares EUR with France - Germany's price wins over an average.
    assert options["eur"]["unit_amount"] == to_smallest_unit(8.99, "EUR")


def test_shared_currency_without_the_anchor_averages():
    data = _data(
        {"Ireland": 8.99, "Portugal": 6.99},
        {"Ireland": "EUR", "Portugal": "EUR"},
    )
    config = PricingConfig(anchor_country="United States")  # not in this EUR group

    options, locked = resolve_currency_options(data, config, base_currency="USD")

    assert options["eur"]["unit_amount"] == to_smallest_unit((8.99 + 6.99) / 2, "EUR")


def test_countries_priced_in_the_base_currency_are_locked_not_pushed():
    data = _data(
        {"United States": 9.99, "Ecuador": 9.99, "Norway": 99.0},
        {"United States": "USD", "Ecuador": "USD", "Norway": "NOK"},
    )
    options, locked = resolve_currency_options(data, PricingConfig(), base_currency="USD")

    assert "usd" not in options
    assert set(locked) == {"United States", "Ecuador"}
    assert "currency_options" in locked["United States"]


# --- period -> Stripe coupon months ---

def test_month_and_year_periods_map_to_whole_months():
    assert months_for_period("P1M") == 1
    assert months_for_period("P3M") == 3
    assert months_for_period("P1Y") == 12


def test_weekly_period_has_no_month_equivalent():
    assert months_for_period("P1W") is None


# --- dropped-currency parsing ---

def test_dropped_currency_reads_the_structured_param_field():
    error = _stripe_error(param="currency_options[eur][unit_amount]", message="Invalid currency")
    assert _dropped_currency(error) == "eur"


def test_dropped_currency_is_none_for_unrelated_params():
    error = _stripe_error(param="unit_amount", message="Invalid amount")
    assert _dropped_currency(error) is None


def test_dropped_currency_falls_back_to_message_for_unsupported_currency():
    # A currency Stripe doesn't support at all (e.g. IQD) comes back with no
    # currency_options[xxx]-shaped param - only the message names the currency.
    error = _stripe_error(
        param=None,
        message="Invalid currency: iqd. Stripe currently supports these currencies: usd, eur, ...",
    )
    assert _dropped_currency(error) == "iqd"


# --- update_prices retry ladder ---

class _FakePrices:
    def __init__(self, price, fail_currency=None):
        self._price = price
        self._fail_currency = fail_currency
        self.update_calls = []

    def retrieve(self, price_id):
        return self._price

    def update(self, price_id, params):
        # Snapshot currency_options at call time - resolve_currency_options()'s dict is
        # mutated in place between retries, so recording the reference would make every
        # recorded call retroactively show the final (post-retry) state instead of what
        # was actually sent on the wire for that attempt.
        self.update_calls.append({"currency_options": dict(params["currency_options"])})
        if self._fail_currency and self._fail_currency in params["currency_options"]:
            raise stripe.error.InvalidRequestError(
                f"Invalid currency: {self._fail_currency}",
                param=f"currency_options[{self._fail_currency}][unit_amount]",
            )


class _FakeV1:
    def __init__(self, prices):
        self.prices = prices


class _FakeClient:
    def __init__(self, prices):
        self.v1 = _FakeV1(prices)


class _StripeObjectStub(dict):
    """Minimal stand-in for stripe-python's StripeObject: item access (price["currency"])
    *and* attribute access (getattr(price, "recurring", None)) both read the same keys.

    A plain dict only gives you the former - getattr() on a plain dict never finds a
    "recurring" attribute regardless of what's in the dict, silently defaulting to None
    every time. _fetch_price() relies on attribute access specifically (see its comment),
    so a plain-dict stub would make every price look non-recurring no matter what "recurring"
    was set to - this class is what makes these stubs actually exercise that code path.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _price_stub(currency="USD"):
    return _StripeObjectStub(id="price_123", currency=currency, recurring={"interval": "month"}, currency_options={})


def test_update_prices_drops_the_rejected_currency_and_retries(monkeypatch):
    from store_pricing import config as config_mod
    creds = config_mod.StripeCreds(secret_key="sk_test_x", price_id="price_123")

    data = _data(
        {"Norway": 99.0, "Germany": 8.99},
        {"Norway": "NOK", "Germany": "EUR"},
    )

    fake_prices = _FakePrices(_price_stub(), fail_currency="nok")
    monkeypatch.setattr("store_pricing.stripe_platform.fetch_price", lambda c: (_price_stub(), None))
    monkeypatch.setattr("store_pricing.stripe_platform.build_client", lambda c: _FakeClient(fake_prices))

    failures = update_prices(creds, PricingConfig(), data, dry_run=False)

    # First attempt included both currencies and failed; second attempt dropped NOK and succeeded.
    assert len(fake_prices.update_calls) == 2
    assert "nok" in fake_prices.update_calls[0]["currency_options"]
    assert "nok" not in fake_prices.update_calls[1]["currency_options"]
    assert any(f["country"] == "Norway" and "Rejected by Stripe" in f["reason"] for f in failures)


def test_update_prices_reports_locked_base_currency_countries(monkeypatch):
    from store_pricing import config as config_mod
    creds = config_mod.StripeCreds(secret_key="sk_test_x", price_id="price_123")

    data = _data({"United States": 9.99}, {"United States": "USD"})

    monkeypatch.setattr("store_pricing.stripe_platform.fetch_price", lambda c: (_price_stub("USD"), None))

    failures = update_prices(creds, PricingConfig(), data, dry_run=True)

    assert len(failures) == 1
    assert failures[0]["country"] == "United States"
    assert "currency_options" in failures[0]["reason"]


# --- fetch_price()/fetch_onetime_price(): recurring-vs-one-time validation ---

class _FakeRetrievePrices:
    def __init__(self, price):
        self._price = price

    def retrieve(self, price_id, params=None):
        return self._price


def _onetime_price_stub(currency="USD"):
    return _StripeObjectStub(id="price_456", currency=currency, recurring=None, currency_options={})


def test_fetch_price_rejects_a_one_time_price(monkeypatch):
    creds = StripeCreds(secret_key="sk_test_x", price_id="price_456")
    monkeypatch.setattr(
        "store_pricing.stripe_platform.build_client",
        lambda c: _FakeClient(_FakeRetrievePrices(_onetime_price_stub())),
    )

    price, error = fetch_price(creds)

    assert price is None
    assert "not a recurring price" in error


def test_fetch_onetime_price_rejects_a_recurring_price(monkeypatch):
    creds = StripeOneTimeCreds(secret_key="sk_test_x", price_id="price_123")
    monkeypatch.setattr(
        "store_pricing.stripe_platform.build_client",
        lambda c: _FakeClient(_FakeRetrievePrices(_price_stub())),
    )

    price, error = fetch_onetime_price(creds)

    assert price is None
    assert "recurring price" in error


def test_fetch_onetime_price_accepts_a_one_time_price(monkeypatch):
    creds = StripeOneTimeCreds(secret_key="sk_test_x", price_id="price_456")
    monkeypatch.setattr(
        "store_pricing.stripe_platform.build_client",
        lambda c: _FakeClient(_FakeRetrievePrices(_onetime_price_stub())),
    )

    price, error = fetch_onetime_price(creds)

    assert error is None
    assert price["id"] == "price_456"


# --- update_onetime_prices(): same retry ladder, tagged with a distinct platform label ---

def test_update_onetime_prices_drops_the_rejected_currency_and_retries(monkeypatch):
    creds = StripeOneTimeCreds(secret_key="sk_test_x", price_id="price_456")

    data = _data(
        {"Norway": 99.0, "Germany": 8.99},
        {"Norway": "NOK", "Germany": "EUR"},
    )

    fake_prices = _FakePrices(_onetime_price_stub(), fail_currency="nok")
    monkeypatch.setattr("store_pricing.stripe_platform.fetch_onetime_price", lambda c: (_onetime_price_stub(), None))
    monkeypatch.setattr("store_pricing.stripe_platform.build_client", lambda c: _FakeClient(fake_prices))

    failures = update_onetime_prices(creds, PricingConfig(), data, dry_run=False)

    assert len(fake_prices.update_calls) == 2
    assert "nok" in fake_prices.update_calls[0]["currency_options"]
    assert "nok" not in fake_prices.update_calls[1]["currency_options"]
    assert any(f["country"] == "Norway" and "Rejected by Stripe" in f["reason"] for f in failures)
    # Distinct platform label so this doesn't collapse into the subscription's "Stripe"
    # bucket in the failure report when both are pushed in the same run.
    assert all(f["platform"] == "Stripe (One-time)" for f in failures)


def test_update_onetime_prices_locked_country_message_points_at_the_onetime_env_var(monkeypatch):
    creds = StripeOneTimeCreds(secret_key="sk_test_x", price_id="price_456")
    data = _data({"United States": 9.99}, {"United States": "USD"})

    monkeypatch.setattr("store_pricing.stripe_platform.fetch_onetime_price", lambda c: (_onetime_price_stub("USD"), None))

    failures = update_onetime_prices(creds, PricingConfig(), data, dry_run=True)

    assert len(failures) == 1
    assert failures[0]["platform"] == "Stripe (One-time)"
    assert "STRIPE_ONETIME_PRICE_ID" in failures[0]["reason"]


# --- create_stripe_offer ---

class _FakeCoupons:
    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.create_calls = []

    def create(self, params):
        self.create_calls.append(dict(params))
        if self.errors:
            raise self.errors.pop(0)
        return {"id": params["id"]}


class _FakePromotionCodes:
    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.create_calls = []

    def create(self, params):
        self.create_calls.append(dict(params))
        if self.errors:
            raise self.errors.pop(0)
        return {"id": params["code"]}


class _FakeOfferV1:
    def __init__(self, coupons, promotion_codes):
        self.coupons = coupons
        self.promotion_codes = promotion_codes


class _FakeOfferClient:
    def __init__(self, coupons, promotion_codes):
        self.v1 = _FakeOfferV1(coupons, promotion_codes)


def _offer_config(**overrides):
    defaults = dict(
        platforms={"stripe"},
        discount_percent=30,
        num_periods=3,
        apple_duration="ONE_MONTH",
        google_duration="P1M",
        offer_name="Summer sale",
        offer_code="SUMMER30",
    )
    defaults.update(overrides)
    return OfferConfig(**defaults)


def _patch_offer_client(monkeypatch, coupons, promotion_codes):
    monkeypatch.setattr(
        "store_pricing.stripe_platform.build_client",
        lambda creds: _FakeOfferClient(coupons, promotion_codes),
    )
    monkeypatch.setattr("store_pricing.offers.time.sleep", lambda seconds: None)


def test_create_stripe_offer_succeeds_on_the_first_try(monkeypatch):
    coupons, promotion_codes = _FakeCoupons(), _FakePromotionCodes()
    _patch_offer_client(monkeypatch, coupons, promotion_codes)

    failures = create_stripe_offer(StripeCreds("sk_test_x", "price_123"), _offer_config())

    assert failures == []
    assert coupons.create_calls[0]["id"] == "SUMMER30"
    assert coupons.create_calls[0]["duration_in_months"] == 3  # P1M x 3 cycles
    assert promotion_codes.create_calls[0]["code"] == "SUMMER30"


def test_create_stripe_offer_retries_a_duplicate_coupon_id_with_a_suffix(monkeypatch):
    dup = stripe.error.InvalidRequestError("Coupon already exists", param=None, code="resource_already_exists")
    coupons, promotion_codes = _FakeCoupons(errors=[dup]), _FakePromotionCodes()
    _patch_offer_client(monkeypatch, coupons, promotion_codes)

    failures = create_stripe_offer(StripeCreds("sk_test_x", "price_123"), _offer_config())

    assert failures == []
    assert len(coupons.create_calls) == 2
    assert coupons.create_calls[0]["id"] == "SUMMER30"
    assert coupons.create_calls[1]["id"] != "SUMMER30"
    assert coupons.create_calls[1]["id"].startswith("SUMMER30-")


def test_create_stripe_offer_reports_a_non_duplicate_coupon_error_instead_of_raising(monkeypatch):
    # Regression test: an unhandled StripeError subtype used to propagate out of
    # create_stripe_offer and crash the CLI instead of coming back as a failure report -
    # AuthenticationError/APIConnectionError/RateLimitError don't subclass InvalidRequestError.
    down = stripe.error.APIConnectionError("network is down")
    coupons, promotion_codes = _FakeCoupons(errors=[down]), _FakePromotionCodes()
    _patch_offer_client(monkeypatch, coupons, promotion_codes)

    failures = create_stripe_offer(StripeCreds("sk_test_x", "price_123"), _offer_config())

    assert len(failures) == 1
    assert "Could not create coupon" in failures[0]["reason"]
    assert promotion_codes.create_calls == []


def test_create_stripe_offer_reports_a_non_duplicate_promotion_code_error_instead_of_raising(monkeypatch):
    down = stripe.error.APIConnectionError("network is down")
    coupons, promotion_codes = _FakeCoupons(), _FakePromotionCodes(errors=[down])
    _patch_offer_client(monkeypatch, coupons, promotion_codes)

    failures = create_stripe_offer(StripeCreds("sk_test_x", "price_123"), _offer_config())

    assert len(failures) == 1
    assert "promotion code failed" in failures[0]["reason"]


def test_create_stripe_offer_dry_run_sends_no_request(monkeypatch):
    coupons, promotion_codes = _FakeCoupons(), _FakePromotionCodes()
    _patch_offer_client(monkeypatch, coupons, promotion_codes)

    failures = create_stripe_offer(StripeCreds("sk_test_x", "price_123"), _offer_config(), dry_run=True)

    assert failures == []
    assert coupons.create_calls == []
    assert promotion_codes.create_calls == []
