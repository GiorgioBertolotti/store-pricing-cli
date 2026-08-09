"""Subscription and one-time-product failures must not collapse into the same report
bucket when both are pushed in the same `pricing.py apply` run.

Regression: apple.update_iap_prices()/google.update_onetime_prices() originally reused the
same _make_failure() helper as their subscription counterparts, tagging every failure with
the bare platform string ("Apple App Store" / "Google Play"). report.write_failure_report()
and ui.failure_summary_table() both group failures by that exact string, so a combined run
merged subscription and one-time failures into one indistinguishable bucket - there was no
way to tell, from the report, which product a given failure belonged to.
"""

from store_pricing import apple as apple_api
from store_pricing import google as google_api
from store_pricing import stripe_platform as stripe_api
from store_pricing.report import write_failure_report


def test_apple_iap_failures_use_a_distinct_platform_label():
    failure = apple_api._make_iap_failure("Norway", "NOR", 9.99, "NOK", "some reason")
    assert failure["platform"] == "Apple App Store (One-time)"
    assert failure["platform"] != "Apple App Store"


def test_google_onetime_failures_use_a_distinct_platform_label():
    failure = google_api._make_onetime_failure("Norway", "NO", 9.99, "NOK", "some reason")
    assert failure["platform"] == "Google Play (One-time)"
    assert failure["platform"] != "Google Play"


def test_stripe_onetime_failures_use_a_distinct_platform_label():
    failure = stripe_api._make_onetime_failure("Norway", "NOK", 9.99, "NOK", "some reason")
    assert failure["platform"] == "Stripe (One-time)"
    assert failure["platform"] != "Stripe"


def test_combined_run_keeps_subscription_and_onetime_failures_separate_in_the_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    failures = [
        apple_api._make_failure("Iran", "IRN", 9.99, "IRR", "No price points available with currency IRR"),
        apple_api._make_iap_failure("Iran", "IRN", 9.99, "IRR", "No price points available with currency IRR"),
        google_api._make_failure("China", "CN", 9.99, "USD", "Region not found in Google Play configuration"),
        google_api._make_onetime_failure("China", "CN", 9.99, "USD", "Region not found in Google Play configuration"),
        stripe_api._make_failure("Iceland", "ISK", 9.99, "ISK", "Rejected by Stripe: some error"),
        stripe_api._make_onetime_failure("Iceland", "ISK", 9.99, "ISK", "Rejected by Stripe: some error"),
    ]

    path = write_failure_report(failures)
    text = path.read_text()

    assert "APPLE APP STORE FAILURES (SUBSCRIPTION)" in text
    assert "APPLE APP STORE FAILURES (ONE-TIME)" in text
    assert "GOOGLE PLAY FAILURES (SUBSCRIPTION)" in text
    assert "GOOGLE PLAY FAILURES (ONE-TIME)" in text
    assert "STRIPE FAILURES (SUBSCRIPTION)" in text
    assert "STRIPE FAILURES (ONE-TIME)" in text
    # Every failure that contributed to the total must actually show up in some bucket -
    # this is exactly the "silently dropped" failure mode the missing buckets caused.
    assert text.count("Iran") == 2
    assert text.count("China") == 2
    assert text.count("Iceland") == 2


def test_subscription_only_run_keeps_the_original_unsuffixed_label(tmp_path, monkeypatch):
    """No one-time failures in the batch -> labels stay exactly as they were before
    one-time-purchase support existed, so an existing subscription-only setup sees no
    change in its report."""
    monkeypatch.chdir(tmp_path)

    failures = [apple_api._make_failure("Iran", "IRN", 9.99, "IRR", "No price points available with currency IRR")]
    path = write_failure_report(failures)
    text = path.read_text()

    assert "APPLE APP STORE FAILURES:" in text
    assert "(SUBSCRIPTION)" not in text
    assert "(ONE-TIME)" not in text
