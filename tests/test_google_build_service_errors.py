"""build_service() forces a live fetch of Google's discovery document (static_discovery=False,
see its docstring) - a new network dependency shared by every function in google.py, including
plain subscription-only runs. Without _build_service_safe(), a network hiccup there (or a bad
service-account file) crashes the whole run with an unhandled exception instead of the graceful
failure every other error path in this module produces. These tests pin down that
fetch_live_prices()/update_prices()/fetch_live_onetime_prices()/update_onetime_prices() all
survive build_service() raising.
"""

from store_pricing.config import GoogleCreds, GoogleOneTimeCreds, PricingConfig
from store_pricing.google import (
    fetch_live_onetime_prices,
    fetch_live_prices,
    update_onetime_prices,
    update_prices,
)
from store_pricing.inputs import InputData


def _sub_creds() -> GoogleCreds:
    return GoogleCreds(service_account_file="unused.json", package_name="com.example.app",
                        subscription_id="sub", baseplan_id="annual")


def _onetime_creds() -> GoogleOneTimeCreds:
    return GoogleOneTimeCreds(service_account_file="unused.json", package_name="com.example.app",
                               product_id="lifetime_unlock", purchase_option_id="buy")


def _data() -> InputData:
    return InputData(
        country_prices={"United States": 19.99},
        country_prices_usd={"United States": 19.99},
        country_currencies={"United States": "USD"},
        country_code_mapping={"United States": {"alpha2": "US", "alpha3": "USA"}},
    )


def _raise_connection_error(creds):
    raise ConnectionError("discovery.googleapis.com unreachable")


def test_fetch_live_prices_survives_build_service_failure(monkeypatch):
    monkeypatch.setattr("store_pricing.google.build_service", _raise_connection_error)
    assert fetch_live_prices(_sub_creds(), _data()) == {}


def test_update_prices_reports_a_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr("store_pricing.google.build_service", _raise_connection_error)
    failures = update_prices(_sub_creds(), PricingConfig(), _data(), dry_run=True)
    assert len(failures) == 1
    assert failures[0]["platform"] == "Google Play"
    assert "Could not connect to Google Play" in failures[0]["reason"]


def test_fetch_live_onetime_prices_survives_build_service_failure(monkeypatch):
    monkeypatch.setattr("store_pricing.google.build_service", _raise_connection_error)
    assert fetch_live_onetime_prices(_onetime_creds(), _data()) == {}


def test_update_onetime_prices_reports_a_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr("store_pricing.google.build_service", _raise_connection_error)
    failures = update_onetime_prices(_onetime_creds(), PricingConfig(), _data(), dry_run=True)
    assert len(failures) == 1
    assert failures[0]["platform"] == "Google Play (One-time)"
    assert "Could not connect to Google Play" in failures[0]["reason"]
