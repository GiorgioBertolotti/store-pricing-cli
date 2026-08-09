"""One-time-product analogue of test_google_errors.py: feeds the same class of Google Play
400-error strings through _handle_onetime_400_error() and asserts the right auto-fix fires
against purchaseOptions/regionalPricingAndAvailabilityConfigs instead of
basePlans/regionalConfigs. The currency-mismatch, not-billable and price-out-of-range
regexes are shared verbatim with the subscription ladder (see google.py's module docstring
for why); only the regional-configs-removal wording is product-specific, so it gets its own
test using "purchase option" phrasing."""

import pytest

from store_pricing.google import _handle_onetime_400_error


def _onetime_product_update():
    return {
        "purchaseOptions": [{
            "purchaseOptionId": "lifetime",
            "regionalPricingAndAvailabilityConfigs": [
                {"regionCode": "TR", "price": {"currencyCode": "USD", "units": "5", "nanos": 0}},
                {"regionCode": "AR", "price": {"currencyCode": "ARS", "units": "1000", "nanos": 0}},
            ],
        }],
    }


def test_currency_mismatch_is_fixed_in_place():
    update = _onetime_product_update()
    error = "region code TR is invalid: Expected TRY but got USD"
    handled = _handle_onetime_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=(fixed := set()), clamped_pairs=set())

    assert handled is True
    assert "TR" in fixed
    tr_config = update["purchaseOptions"][0]["regionalPricingAndAvailabilityConfigs"][0]
    assert tr_config["price"]["currencyCode"] == "TRY"


def test_not_billable_region_is_dropped():
    update = _onetime_product_update()
    error = "Region code AR is not billable for this app"
    handled = _handle_onetime_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())

    assert handled is True
    region_codes = {c["regionCode"] for c in update["purchaseOptions"][0]["regionalPricingAndAvailabilityConfigs"]}
    assert "AR" not in region_codes


def test_price_out_of_range_is_clamped_then_removed_on_repeat():
    update = _onetime_product_update()
    error = "lifetime: Price for TR must be between 1.00 and 999.00. Details: out of range"
    extra_failures = []
    clamped_pairs = set()

    # First occurrence: clamp to the minimum and remember the (purchase_option, region) pair.
    handled = _handle_onetime_400_error(update, error, attempt=0, extra_failures=extra_failures, auto_fixed_regions=set(), clamped_pairs=clamped_pairs)
    assert handled is True
    assert ("lifetime", "TR") in clamped_pairs
    tr_config = update["purchaseOptions"][0]["regionalPricingAndAvailabilityConfigs"][0]
    assert tr_config["price"]["units"] == "1"

    # Second occurrence for the same pair: the clamp didn't stick, so the region is dropped
    # from that purchase option instead of retrying forever.
    handled_again = _handle_onetime_400_error(update, error, attempt=1, extra_failures=extra_failures, auto_fixed_regions=set(), clamped_pairs=clamped_pairs)
    assert handled_again is True
    region_codes = {c["regionCode"] for c in update["purchaseOptions"][0]["regionalPricingAndAvailabilityConfigs"]}
    assert "TR" not in region_codes
    assert len(extra_failures) == 2


def test_region_removal_rejected_raises_actionable_error():
    update = _onetime_product_update()
    error = "lifetime: Regional configs were removed from the purchase option: TR"
    with pytest.raises(RuntimeError, match="manual fix in Play Console"):
        _handle_onetime_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())


def test_unrecognized_error_is_not_handled():
    update = _onetime_product_update()
    error = "Something Google has never said before"
    handled = _handle_onetime_400_error(update, error, attempt=0, extra_failures=[], auto_fixed_regions=set(), clamped_pairs=set())
    assert handled is False
