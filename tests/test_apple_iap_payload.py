"""Asserts the shape of POST /v1/inAppPurchasePriceSchedules, the endpoint that replaces a
one-time in-app purchase's entire price schedule in a single request (there is no
per-territory PATCH the way subscriptionPrices has - see apple.py's module docstring). This
shape was cross-checked against Apple's published OpenAPI spec rather than reverse-engineered
from a live 409 the way offers.py's subscription payload was - this test locks it down so a
mismatch shows up locally instead of as an opaque 4xx against the real API.

Also covers the all-or-nothing failure semantics: since one POST covers every territory in
the batch, a rejected submission has to report a failure for every territory that was in it,
not just a single aggregate error - update_prices()'s per-country failures don't have this
problem because each territory is its own independent request.
"""

from store_pricing import apple as apple_api
from store_pricing.config import AppleOneTimeCreds, PricingConfig
from store_pricing.inputs import InputData


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _apple_onetime_creds() -> AppleOneTimeCreds:
    return AppleOneTimeCreds(
        issuer_id="11111111-1111-1111-1111-111111111111",
        key_id="ABC123",
        private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        app_id="123456789",
        iap_product_id="lifetime_unlock",
    )


def _input_data() -> InputData:
    return InputData(
        country_prices={"Norway": 99.0, "Japan": 1500.0},
        country_prices_usd={"Norway": 9.99, "Japan": 9.99},
        country_currencies={"Norway": "NOK", "Japan": "JPY"},
        country_code_mapping={
            "Norway": {"alpha2": "NO", "alpha3": "NOR"},
            "Japan": {"alpha2": "JP", "alpha3": "JPN"},
            "United States": {"alpha2": "US", "alpha3": "USA"},
        },
    )


def _fake_resolve_iap_price_point(price_points):
    """price_points: {country_code: (point_id, customer_price)}"""

    def fake_resolve(token, iap_id, country, country_code, price, currency_code, usd_prices, usd_rates=None, rounding="psychological"):
        point_id, customer_price = price_points[country_code]
        return {"id": point_id, "attributes": {"customerPrice": str(customer_price)}}, price, currency_code, currency_code, None

    return fake_resolve


def test_iap_price_schedule_payload_shape(monkeypatch):
    captured = {}

    monkeypatch.setattr(apple_api, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(apple_api, "get_iap_id", lambda token, creds: "iap_123")
    monkeypatch.setattr(apple_api, "fetch_all_usd_rates", lambda: {"NOK": 10.5, "JPY": 150.0})
    monkeypatch.setattr(
        apple_api, "resolve_iap_price_point",
        _fake_resolve_iap_price_point({"NOR": ("pp_nor", 99.0), "JPN": ("pp_jpn", 1500.0)}),
    )

    def fake_post(url, headers, json, max_retries=5):
        captured["url"] = url
        captured["body"] = json
        return _FakeResponse(201)

    monkeypatch.setattr(apple_api, "_post_with_rate_limit", fake_post)

    failures = apple_api.update_iap_prices(
        _apple_onetime_creds(), PricingConfig(anchor_country="United States"), _input_data(),
    )

    assert failures == []
    assert captured["url"].endswith("/inAppPurchasePriceSchedules")

    body = captured["body"]
    data = body["data"]
    assert data["type"] == "inAppPurchasePriceSchedules"
    assert data["relationships"]["inAppPurchase"]["data"] == {"type": "inAppPurchases", "id": "iap_123"}
    # baseTerritory anchors the schedule to config.anchor_country's territory - required by
    # Apple, and there's no other natural source of truth for it in this pipeline.
    assert data["relationships"]["baseTerritory"]["data"] == {"type": "territories", "id": "USA"}

    manual_prices = data["relationships"]["manualPrices"]["data"]
    assert {p["id"] for p in manual_prices} == {"${NOR}", "${JPN}"}
    assert all(p["type"] == "inAppPurchasePrices" for p in manual_prices)

    included = {i["id"]: i for i in body["included"]}
    assert set(included) == {"${NOR}", "${JPN}"}
    nor = included["${NOR}"]
    assert nor["relationships"]["inAppPurchaseV2"]["data"] == {"type": "inAppPurchases", "id": "iap_123"}
    assert nor["relationships"]["inAppPurchasePricePoint"]["data"] == {"type": "inAppPurchasePricePoints", "id": "pp_nor"}
    assert nor["attributes"]["startDate"] is None


def test_iap_price_schedule_rejection_fails_every_territory_in_the_batch(monkeypatch):
    """A rejected schedule POST is all-or-nothing - unlike per-country subscriptionPrices
    submissions, every territory that was included in this batch has to be reported as
    failed, not just a single opaque error."""
    monkeypatch.setattr(apple_api, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(apple_api, "get_iap_id", lambda token, creds: "iap_123")
    monkeypatch.setattr(apple_api, "fetch_all_usd_rates", lambda: {"NOK": 10.5, "JPY": 150.0})
    monkeypatch.setattr(
        apple_api, "resolve_iap_price_point",
        _fake_resolve_iap_price_point({"NOR": ("pp_nor", 99.0), "JPN": ("pp_jpn", 1500.0)}),
    )
    monkeypatch.setattr(apple_api, "_post_with_rate_limit", lambda url, headers, json, max_retries=5: _FakeResponse(409, "CONFLICT"))

    failures = apple_api.update_iap_prices(
        _apple_onetime_creds(), PricingConfig(anchor_country="United States"), _input_data(),
    )

    assert {f["country"] for f in failures} == {"Norway", "Japan"}
    assert all("409" in f["reason"] for f in failures)


def test_iap_dry_run_never_submits(monkeypatch):
    monkeypatch.setattr(apple_api, "get_jwt", lambda creds: "token")
    monkeypatch.setattr(apple_api, "get_iap_id", lambda token, creds: "iap_123")
    monkeypatch.setattr(apple_api, "fetch_all_usd_rates", lambda: {"NOK": 10.5, "JPY": 150.0})
    monkeypatch.setattr(
        apple_api, "resolve_iap_price_point",
        _fake_resolve_iap_price_point({"NOR": ("pp_nor", 99.0), "JPN": ("pp_jpn", 1500.0)}),
    )

    def fail_if_called(*a, **k):
        raise AssertionError("dry-run must never POST")

    monkeypatch.setattr(apple_api, "_post_with_rate_limit", fail_if_called)

    failures = apple_api.update_iap_prices(
        _apple_onetime_creds(), PricingConfig(anchor_country="United States"), _input_data(), dry_run=True,
    )

    assert failures == []
