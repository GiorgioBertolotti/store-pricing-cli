"""Feeds Google Play's recorded 400-error strings through the recovery ladder and asserts
the right auto-fix fires. These regexes are the only thing standing between a stale
currency code / non-billable region / clamp-needed price and a hard failure of the whole
patch (Google validates the entire basePlans object together), so a wording change on
Google's side needs to show up here as a test failure, not a silent no-op."""

import pytest

from store_pricing.google import _handle_400_error


def _subscription_update():
    return {
        "basePlans": [{
            "basePlanId": "monthly",
            "regionalConfigs": [
                {"regionCode": "TR", "price": {"currencyCode": "USD", "units": "5", "nanos": 0}},
                {"regionCode": "AR", "price": {"currencyCode": "ARS", "units": "1000", "nanos": 0}},
            ],
        }],
    }


def test_currency_mismatch_is_fixed_in_place():
    update = _subscription_update()
    error = "region code TR is invalid: Expected TRY but got USD"
    handled = _handle_400_error(
        update, error, attempt=0, extra_failures=[], auto_fixed_regions=(fixed := set()), clamped_pairs=set(),
        usd_rates={"USD": 1.0, "TRY": 40.0},
    )

    assert handled is True
    assert "TR" in fixed
    tr_config = update["basePlans"][0]["regionalConfigs"][0]
    assert tr_config["price"]["currencyCode"] == "TRY"
    # The amount is converted, not just relabelled: USD 5 -> TRY 200.
    assert (int(tr_config["price"]["units"]), int(tr_config["price"]["nanos"])) == (200, 0)


def test_currency_mismatch_without_exchange_rates_fails_instead_of_relabelling():
    update = _subscription_update()
    error = "region code TR is invalid: Expected TRY but got USD"
    with pytest.raises(RuntimeError, match="exchange rate"):
        _handle_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())


def test_not_billable_region_is_dropped():
    update = _subscription_update()
    error = "Region code AR is not billable for this app"
    handled = _handle_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())

    assert handled is True
    region_codes = {c["regionCode"] for c in update["basePlans"][0]["regionalConfigs"]}
    assert "AR" not in region_codes


def test_price_out_of_range_is_clamped_then_removed_on_repeat():
    update = _subscription_update()
    error = "monthly: Price for TR must be between 1.00 and 999.00. Details: out of range"
    extra_failures = []
    clamped_pairs = set()

    # First occurrence: clamp to the minimum and remember the (base_plan, region) pair.
    handled = _handle_400_error(update, error, attempt=0, extra_failures=extra_failures, auto_fixed_regions=set(), clamped_pairs=clamped_pairs)
    assert handled is True
    assert ("monthly", "TR") in clamped_pairs
    tr_config = update["basePlans"][0]["regionalConfigs"][0]
    assert tr_config["price"]["units"] == "1"

    # Second occurrence for the same pair: the clamp didn't stick, so the region is
    # dropped from that base plan instead of retrying forever.
    handled_again = _handle_400_error(update, error, attempt=1, extra_failures=extra_failures, auto_fixed_regions=set(), clamped_pairs=clamped_pairs)
    assert handled_again is True
    region_codes = {c["regionCode"] for c in update["basePlans"][0]["regionalConfigs"]}
    assert "TR" not in region_codes
    assert len(extra_failures) == 2


def test_region_removal_rejected_raises_actionable_error():
    update = _subscription_update()
    error = "monthly: Regional configs were removed from the base plan: TR"
    with pytest.raises(RuntimeError, match="manual fix in Play Console"):
        _handle_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())


def test_unrecognized_error_is_not_handled():
    update = _subscription_update()
    error = "Something Google has never said before"
    handled = _handle_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())
    assert handled is False
