"""Every live Google Play patch rewrites the whole product, so update_prices() snapshots it
first, refuses to patch without that snapshot, re-reads it afterwards to flag anything that
moved outside the priced container, and `restore-google` can put the snapshot back."""

import copy
import json
from datetime import datetime, timezone

import pytest

from store_pricing import backup, google
from store_pricing.config import GoogleCreds, GoogleOneTimeCreds, PricingConfig
from store_pricing.inputs import InputData


def _price(units: int, nanos: int = 990000000, currency: str = "EUR") -> dict:
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


def _subscription() -> dict:
    return {
        "productId": "sub",
        "basePlans": [
            {"basePlanId": "annual", "regionalConfigs": [{"regionCode": "IT", "price": _price(14)}]},
            {"basePlanId": "monthly", "regionalConfigs": [
                {"regionCode": "IT", "price": _price(1)},
                {"regionCode": "FR", "price": _price(2)},
            ]},
        ],
    }


class _Request:
    def __init__(self, service, action, kwargs):
        self.service, self.action, self.kwargs = service, action, kwargs
        self.uri = "https://example.test/patch"

    def execute(self):
        if self.action == "get":
            return copy.deepcopy(self.service.state)
        self.service.patches.append(copy.deepcopy(self.kwargs["body"]))
        self.service.state = copy.deepcopy(self.service.after_patch(self.kwargs["body"]))
        return self.service.state


class FakeService:
    """monetization().subscriptions()/onetimeproducts().get/patch over an in-memory resource.
    `after_patch` decides what Google ends up storing, to simulate a patch touching more than it should."""

    def __init__(self, state, after_patch=None):
        self.state = copy.deepcopy(state)
        self.patches = []
        self.after_patch = after_patch or (lambda body: body)

    def monetization(self):
        return self

    def subscriptions(self):
        return self

    def onetimeproducts(self):
        return self

    def get(self, **kwargs):
        return _Request(self, "get", kwargs)

    def patch(self, **kwargs):
        return _Request(self, "patch", kwargs)


def _creds() -> GoogleCreds:
    return GoogleCreds(service_account_file="unused.json", package_name="com.example.app",
                        subscription_id="sub", baseplan_id="annual")


def _data() -> InputData:
    return InputData(
        country_prices={"Italy": 18.99},
        country_prices_usd={"Italy": 21.0},
        country_currencies={"Italy": "EUR"},
        country_code_mapping={"Italy": {"alpha2": "IT", "alpha3": "ITA"}},
    )


@pytest.fixture
def in_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("store_pricing.google.fetch_all_usd_rates", lambda: {"EUR": 0.9, "USD": 1.0})
    return tmp_path


def _use(monkeypatch, service):
    monkeypatch.setattr("store_pricing.google.build_service", lambda creds: service)


# --- backup files ---

def test_backup_round_trips_the_resource_unchanged(tmp_path):
    when = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    path = backup.write_backup(backup.SUBSCRIPTION, "sub", _subscription(), backup_dir=tmp_path, now=when)

    payload = backup.load_backup(path)

    assert path.name.startswith("google-subscription_sub_20261104T120000")
    assert payload["resource"] == _subscription()
    assert payload["product_id"] == "sub"


def test_a_json_file_that_is_not_a_backup_is_rejected(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError):
        backup.load_backup(path)


# --- compare ---

def test_compare_ignores_price_changes_inside_the_target_but_not_elsewhere():
    before = _subscription()
    after = copy.deepcopy(before)
    after["basePlans"][0]["regionalConfigs"][0]["price"] = _price(18)
    after["basePlans"][1]["regionalConfigs"][1]["price"] = _price(1, 490000000)

    issues = backup.compare(before, after, backup.SUBSCRIPTION, "annual")

    assert issues == ["monthly: region FR changed 2.99 EUR -> 1.49 EUR"]


def test_compare_reports_regions_removed_from_the_target_and_removed_containers():
    before = _subscription()
    after = copy.deepcopy(before)
    after["basePlans"][0]["regionalConfigs"] = []
    del after["basePlans"][1]

    issues = backup.compare(before, after, backup.SUBSCRIPTION, "annual")

    assert "monthly: removed" in issues
    assert "annual: region IT removed (was 14.99 EUR)" in issues


# --- update_prices ---

def test_a_live_patch_writes_a_backup_of_the_pre_patch_state_first(monkeypatch, in_tmp):
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    failures = google.update_prices(_creds(), PricingConfig(), _data())

    backups = list((in_tmp / "backups").glob("google-subscription_sub_*.json"))
    assert len(backups) == 1
    assert backup.load_backup(backups[0])["resource"] == _subscription()
    assert len(service.patches) == 1
    assert failures == []


def test_dry_run_neither_backs_up_nor_patches(monkeypatch, in_tmp):
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    google.update_prices(_creds(), PricingConfig(), _data(), dry_run=True)

    assert not (in_tmp / "backups").exists()
    assert service.patches == []


def test_no_patch_is_sent_when_the_backup_cannot_be_written(monkeypatch, in_tmp):
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("store_pricing.backup.write_backup", fail)

    failures = google.update_prices(_creds(), PricingConfig(), _data())

    assert service.patches == []
    assert any("Could not back up" in f["reason"] for f in failures)


def test_a_patch_that_moves_another_base_plan_is_reported_with_a_restore_hint(monkeypatch, in_tmp):
    def drops_france_from_monthly(body):
        stored = copy.deepcopy(body)
        stored["basePlans"][1]["regionalConfigs"] = stored["basePlans"][1]["regionalConfigs"][:1]
        return stored

    service = FakeService(_subscription(), after_patch=drops_france_from_monthly)
    _use(monkeypatch, service)

    failures = google.update_prices(_creds(), PricingConfig(), _data())

    reasons = [f["reason"] for f in failures]
    assert any("monthly: region FR removed" in r and "restore-google --backup" in r for r in reasons)


# --- restore ---

def _payload_for(resource: dict, product_id: str = "sub") -> dict:
    return {"kind": backup.SUBSCRIPTION.kind, "product_id": product_id, "taken_at": "t", "resource": resource}


def test_restore_puts_the_backed_up_prices_back_and_snapshots_the_live_state_first(monkeypatch, in_tmp):
    damaged = _subscription()
    damaged["basePlans"][1]["regionalConfigs"] = damaged["basePlans"][1]["regionalConfigs"][:1]
    service = FakeService(damaged)
    _use(monkeypatch, service)

    changes, remaining = google.restore_from_backup(_creds(), _payload_for(_subscription()))

    assert changes == ["monthly: region FR added (2.99 EUR)"]
    assert remaining == []
    assert service.state["basePlans"] == _subscription()["basePlans"]
    snapshots = list((in_tmp / "backups").glob("*.json"))
    assert [backup.load_backup(p)["resource"] for p in snapshots] == [damaged]


def test_restore_dry_run_lists_changes_without_patching(monkeypatch, in_tmp):
    damaged = _subscription()
    damaged["basePlans"][0]["regionalConfigs"][0]["price"] = _price(30)
    service = FakeService(damaged)
    _use(monkeypatch, service)

    changes, _ = google.restore_from_backup(_creds(), _payload_for(_subscription()), dry_run=True)

    assert changes == ["annual: region IT changed 30.99 EUR -> 14.99 EUR"]
    assert service.patches == []


def test_restore_refuses_a_backup_of_a_different_product(monkeypatch, in_tmp):
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    with pytest.raises(ValueError, match="other"):
        google.restore_from_backup(_creds(), _payload_for(_subscription(), product_id="other"))
    assert service.patches == []


def test_restore_works_for_a_one_time_product(monkeypatch, in_tmp):
    original = {"productId": "ltd", "purchaseOptions": [
        {"purchaseOptionId": "buy", "regionalPricingAndAvailabilityConfigs": [{"regionCode": "IT", "price": _price(39)}]},
    ]}
    damaged = copy.deepcopy(original)
    damaged["purchaseOptions"][0]["regionalPricingAndAvailabilityConfigs"][0]["price"] = _price(1)
    service = FakeService({**damaged, "regionsVersion": {"version": "2025/01"}})
    _use(monkeypatch, service)
    creds = GoogleOneTimeCreds(service_account_file="unused.json", package_name="com.example.app",
                               product_id="ltd", purchase_option_id="buy")
    payload = {"kind": backup.ONETIME_PRODUCT.kind, "product_id": "ltd", "taken_at": "t", "resource": original}

    _, remaining = google.restore_from_backup(creds, payload)

    assert remaining == []
    assert service.state["purchaseOptions"] == original["purchaseOptions"]


def test_backup_live_snapshots_without_patching(monkeypatch, in_tmp):
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    path = google.backup_live(_creds())

    assert backup.load_backup(path)["resource"] == _subscription()
    assert service.patches == []


# --- regions version ---

def test_fetch_defaults_to_the_latest_regions_version_when_google_omits_it():
    service = FakeService(_subscription())
    _, version = google.fetch_subscription(service, _creds())
    assert version == google.LATEST_REGIONS_VERSION


def test_an_outdated_regions_version_is_replaced_by_the_one_google_names(monkeypatch, in_tmp):
    from googleapiclient.errors import HttpError

    class _Resp(dict):
        status = 400
        reason = "Bad Request"

    sent_versions = []
    original_execute = _Request.execute

    def execute(self):
        if self.action == "patch":
            version = self.uri.split("regionsVersion.version=")[-1]
            sent_versions.append(version)
            if version != "2099/01":
                raise HttpError(_Resp(), b'"Invalid regions version x, latest value is ExternalRegionLaunchVersionId{versionId=2099/01}"')
        return original_execute(self)

    monkeypatch.setattr(_Request, "execute", execute)
    service = FakeService(_subscription())
    _use(monkeypatch, service)

    google.update_prices(_creds(), PricingConfig(), _data())

    assert sent_versions == [google.LATEST_REGIONS_VERSION, "2099/01"]
    assert len(service.patches) == 1
