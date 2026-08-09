"""The single entry point: `python pricing.py [command]`.

No-args invokes the guided flow (data refresh -> credentials -> price -> preview -> diff
-> confirm -> push). Every subcommand also accepts flags so the same code path works
unattended in CI (`pricing.py apply --yes`, `pricing.py scale --price 9.99`).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from store_pricing import apple as apple_api
from store_pricing import data as data_mod
from store_pricing import google as google_api
from store_pricing import offers as offers_mod
from store_pricing import scaler
from store_pricing import stripe_platform as stripe_api
from store_pricing import wizard
from store_pricing.config import Settings, ensure_toml_exists, load_settings
from store_pricing.inputs import load_input_data
from store_pricing.report import write_failure_report
from store_pricing.ui import checkbox, confirm, console, diff_table, failure_summary_table, price_preview_table, price_summary, select, text


# Google Play offer ids accept lowercase letters, digits and hyphens only.
_GOOGLE_OFFER_ID_RE = re.compile(r"^[a-z0-9-]+$")


def _validate_google_offer_id(v: str):
    return True if _GOOGLE_OFFER_ID_RE.match(v.strip()) else "Lowercase letters, numbers and hyphens only"


def _validate_price(v: str):
    try:
        f = float(v)
    except ValueError:
        return "Enter a number"
    return True if f > 0 else "Must be positive"


def _validate_percent(v: str):
    try:
        f = float(v)
    except ValueError:
        return "Enter a number"
    return True if 0 < f < 100 else "Must be between 0 and 100"


def _validate_positive_int(v: str):
    try:
        i = int(v)
    except ValueError:
        return "Enter a whole number"
    return True if i >= 1 else "Must be at least 1"


def _validate_nonempty(v: str):
    return True if v.strip() else "Can't be empty"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pricing.py",
        description="PPP-adjusted pricing (subscriptions and one-time purchases) for App Store Connect and Google Play. "
                     "Run with no arguments for the guided flow.",
    )
    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Guided credential setup")
    p_setup.add_argument("--apple", action="store_true")
    p_setup.add_argument("--google", action="store_true")
    p_setup.add_argument("--stripe", action="store_true")

    sub.add_parser("doctor", help="Live-validate configured credentials against every API")

    p_scale = sub.add_parser("scale", help="Compute prices -> price_scaled.xlsx (never touches a store)")
    p_scale.add_argument("--price", type=float, help="Base USD price; prompted if omitted")
    p_scale.add_argument("--out", default="price_scaled.xlsx")

    p_apply = sub.add_parser("apply", help="Push price_scaled.xlsx live to the configured store(s)")
    p_apply.add_argument("--apple", action="store_true", help="Apply to Apple (subscription) only")
    p_apply.add_argument("--google", action="store_true", help="Apply to Google (subscription) only")
    p_apply.add_argument("--stripe", action="store_true", help="Apply to Stripe (subscription) only")
    p_apply.add_argument("--apple-iap", action="store_true", help="Apply to Apple's one-time in-app purchase only")
    p_apply.add_argument("--google-iap", action="store_true", help="Apply to Google's one-time product only")
    p_apply.add_argument("--stripe-onetime", action="store_true", help="Apply to Stripe's one-time Price only")
    p_apply.add_argument("--dry-run", action="store_true", help="Resolve everything but never submit a change")
    p_apply.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (for CI)")

    p_offer = sub.add_parser("offer", help="Create a time-limited promotional/discount offer")
    p_offer.add_argument("--apple", action="store_true")
    p_offer.add_argument("--google", action="store_true")
    p_offer.add_argument("--stripe", action="store_true")
    p_offer.add_argument("--discount", type=float, help="Discount percentage, e.g. 30")
    p_offer.add_argument("--cycles", type=int, help="Number of billing cycles the discount applies to")
    p_offer.add_argument("--period", help="Billing period, e.g. P1M (see `pricing.py offer --help`)")
    p_offer.add_argument("--code", help="Apple offerCode (also used as the Google offer id unless --google-offer-id is given, and as the Stripe coupon id / promotion code)")
    p_offer.add_argument("--google-offer-id", help="Google Play offer id (lowercase letters, numbers, hyphens)")
    p_offer.add_argument("--name", help="Apple offer reference name (human-readable)")
    p_offer.add_argument("--mode", choices=offers_mod.OFFER_MODES, default="PAY_AS_YOU_GO")
    p_offer.add_argument("--dry-run", action="store_true")
    p_offer.add_argument("--yes", action="store_true", help="Skip the delete-and-recreate confirmation")

    # No --force: asking for the refresh explicitly always refreshes. The staleness gate
    # only exists for the *automatic* refresh in ensure_data_fresh().
    sub.add_parser("refresh-data", help="Refresh cost_of_living_data.xlsx and country_codes.json")

    return parser


def ensure_data_fresh(settings: Settings, force: bool = False) -> None:
    if force or data_mod.is_cost_of_living_stale() or not Path("country_codes.json").is_file():
        console.print("[dim]Refreshing cost-of-living data (this only happens periodically)...[/dim]")
        data_mod.refresh_all(settings.pricing.ppp_year)


def _available_platform_names(settings: Settings) -> list[str]:
    """Configured platforms, in a stable display order - shared by the guided flow's push
    prompt and the apply command's platform resolution so both offer the same set of
    choices. A store's subscription label only gets a "(Subscription)" qualifier when its
    one-time-purchase counterpart is *also* configured - the common single-product case
    reads exactly as it did before one-time purchase support existed."""
    names = []
    if settings.apple_configured:
        names.append("Apple (Subscription)" if settings.apple_onetime_configured else "Apple")
    if settings.apple_onetime_configured:
        names.append("Apple (One-time)")
    if settings.google_configured:
        names.append("Google (Subscription)" if settings.google_onetime_configured else "Google")
    if settings.google_onetime_configured:
        names.append("Google (One-time)")
    if settings.stripe_configured:
        names.append("Stripe (Subscription)" if settings.stripe_onetime_configured else "Stripe")
    if settings.stripe_onetime_configured:
        names.append("Stripe (One-time)")
    return names


def _available_offer_platform_names(settings: Settings) -> list[str]:
    """Subscription-only subset of _available_platform_names() for `pricing.py offer` -
    a percentage-off-for-N-cycles promotional offer has no direct analogue for a one-time
    purchase, so those targets are never offered here."""
    names = []
    if settings.apple_configured:
        names.append("Apple")
    if settings.google_configured:
        names.append("Google")
    if settings.stripe_configured:
        names.append("Stripe")
    return names


def _platform_key(name: str) -> str:
    """Map a display name from _available_platform_names() back to the internal key used
    everywhere else (apple/apple_onetime/google/google_onetime/stripe)."""
    base = name.split(" (")[0].lower()
    return f"{base}_onetime" if name.endswith("(One-time)") else base


def _resolve_platforms(
    settings: Settings, want_apple: bool, want_google: bool, want_stripe: bool = False,
    want_apple_iap: bool = False, want_google_iap: bool = False, want_stripe_onetime: bool = False,
) -> set[str]:
    if want_apple or want_google or want_stripe or want_apple_iap or want_google_iap or want_stripe_onetime:
        platforms = set()
        if want_apple:
            if settings.apple_configured:
                platforms.add("apple")
            else:
                console.print("[red]--apple requested but Apple isn't configured.[/red]")
        if want_google:
            if settings.google_configured:
                platforms.add("google")
            else:
                console.print("[red]--google requested but Google isn't configured.[/red]")
        if want_stripe:
            if settings.stripe_configured:
                platforms.add("stripe")
            else:
                console.print("[red]--stripe requested but Stripe isn't configured.[/red]")
        if want_apple_iap:
            if settings.apple_onetime_configured:
                platforms.add("apple_onetime")
            else:
                console.print("[red]--apple-iap requested but Apple one-time purchase isn't configured.[/red]")
        if want_google_iap:
            if settings.google_onetime_configured:
                platforms.add("google_onetime")
            else:
                console.print("[red]--google-iap requested but Google one-time purchase isn't configured.[/red]")
        if want_stripe_onetime:
            if settings.stripe_onetime_configured:
                platforms.add("stripe_onetime")
            else:
                console.print("[red]--stripe-onetime requested but Stripe one-time purchase isn't configured.[/red]")
        return platforms

    return {_platform_key(name) for name in _available_platform_names(settings)}


def _report(failures: list[dict], dry_run: bool = False) -> int:
    if not failures:
        console.print(
            "[bold green]No problems found - nothing was sent (dry run).[/bold green]" if dry_run
            else "[bold green]No failures - all countries updated successfully.[/bold green]"
        )
        return 0
    console.print(failure_summary_table(failures))
    path = write_failure_report(failures)
    console.print(f"Failure report saved to {path}")
    return 1


def _apply_flow(settings: Settings, platforms: set[str], dry_run: bool = False, skip_confirm: bool = False) -> int:
    data = load_input_data()

    total_applyable = 0
    total_locked = 0
    if "google" in platforms:
        console.print("Fetching live Google Play prices for comparison...")
        live = google_api.fetch_live_prices(settings.google, data)
        try:
            usd_rates = scaler.fetch_all_usd_rates()
        except Exception as e:
            console.print(f"[dim]Warning: could not fetch exchange rates ({e}) - currency mismatches will show as locked.[/dim]")
            usd_rates = {}
        table, applyable, locked = diff_table(
            "Google Play", live, data.country_prices, data.country_currencies,
            currency_mismatch_blocks=True, usd_rates=usd_rates, country_prices_usd=data.country_prices_usd,
        )
        console.print(table)
        total_applyable += applyable
        total_locked += locked
    if "apple" in platforms:
        console.print("Fetching live App Store prices for comparison...")
        live = apple_api.fetch_live_prices(settings.apple, data, settings.pricing)
        table, applyable, locked = diff_table("Apple App Store", live, data.country_prices, data.country_currencies)
        console.print(table)
        total_applyable += applyable
        total_locked += locked
    if "google_onetime" in platforms:
        console.print("Fetching live Google Play one-time prices for comparison...")
        live = google_api.fetch_live_onetime_prices(settings.google_onetime, data)
        try:
            usd_rates = scaler.fetch_all_usd_rates()
        except Exception as e:
            console.print(f"[dim]Warning: could not fetch exchange rates ({e}) - currency mismatches will show as locked.[/dim]")
            usd_rates = {}
        table, applyable, locked = diff_table(
            "Google Play (One-time)", live, data.country_prices, data.country_currencies,
            currency_mismatch_blocks=True, usd_rates=usd_rates, country_prices_usd=data.country_prices_usd,
        )
        console.print(table)
        total_applyable += applyable
        total_locked += locked
    if "apple_onetime" in platforms:
        console.print("Fetching live App Store in-app purchase prices for comparison...")
        live = apple_api.fetch_live_iap_prices(settings.apple_onetime, data, settings.pricing)
        table, applyable, locked = diff_table("Apple App Store (One-time)", live, data.country_prices, data.country_currencies)
        console.print(table)
        total_applyable += applyable
        total_locked += locked
    if "stripe" in platforms:
        console.print("Fetching live Stripe prices for comparison...")
        price, error = stripe_api.fetch_price(settings.stripe)
        if error:
            console.print(f"[red]{error}[/red]")
        else:
            live = stripe_api.fetch_live_prices(settings.stripe, data)
            _, locked_countries = stripe_api.resolve_currency_options(data, settings.pricing, price["currency"])
            pushable_prices = {c: p for c, p in data.country_prices.items() if c not in locked_countries}
            table, applyable, _ = diff_table("Stripe", live, pushable_prices, data.country_currencies)
            console.print(table)
            total_applyable += applyable
            if locked_countries:
                console.print(
                    f"[dim]{len(locked_countries)} countries share Stripe's base Price currency "
                    f"({price['currency'].upper()}) and can't be changed via the API - create a new "
                    f"Price to update it.[/dim]"
                )
    if "stripe_onetime" in platforms:
        console.print("Fetching live Stripe one-time prices for comparison...")
        price, error = stripe_api.fetch_onetime_price(settings.stripe_onetime)
        if error:
            console.print(f"[red]{error}[/red]")
        else:
            live = stripe_api.fetch_live_onetime_prices(settings.stripe_onetime, data)
            _, locked_countries = stripe_api.resolve_currency_options(
                data, settings.pricing, price["currency"], price_id_var="STRIPE_ONETIME_PRICE_ID",
            )
            pushable_prices = {c: p for c, p in data.country_prices.items() if c not in locked_countries}
            table, applyable, _ = diff_table("Stripe (One-time)", live, pushable_prices, data.country_currencies)
            console.print(table)
            total_applyable += applyable
            if locked_countries:
                console.print(
                    f"[dim]{len(locked_countries)} countries share Stripe's base Price currency "
                    f"({price['currency'].upper()}) and can't be changed via the API - create a new "
                    f"Price to update it.[/dim]"
                )

    if total_applyable == 0 and total_locked == 0:
        console.print("No price changes to apply.")
        return 0

    locked_note = f" (plus {total_locked} currency-locked on Google, expected to fail regardless)" if total_locked else ""

    if dry_run:
        console.print(f"[dim][dry-run] {total_applyable} changes would be applied{locked_note} - nothing was sent.[/dim]")
    elif total_applyable == 0:
        # Only currency-locked rows remain: every one is expected to be rejected, so
        # there's nothing to confirm and nothing worth sending.
        console.print(f"No applyable price changes{locked_note}. Nothing to do.")
        return 0
    elif not skip_confirm:
        if not confirm(f"Apply {total_applyable} price change(s) across {len(platforms)} store(s)?{locked_note}", default=False):
            console.print("Aborted - nothing was changed.")
            return 0

    failures = []
    if "google" in platforms:
        failures += google_api.update_prices(settings.google, settings.pricing, data, dry_run=dry_run)
    if "apple" in platforms:
        failures += apple_api.update_prices(settings.apple, settings.pricing, data, dry_run=dry_run)
    if "google_onetime" in platforms:
        failures += google_api.update_onetime_prices(settings.google_onetime, settings.pricing, data, dry_run=dry_run)
    if "apple_onetime" in platforms:
        failures += apple_api.update_iap_prices(settings.apple_onetime, settings.pricing, data, dry_run=dry_run)
    if "stripe" in platforms:
        failures += stripe_api.update_prices(settings.stripe, settings.pricing, data, dry_run=dry_run)
    if "stripe_onetime" in platforms:
        failures += stripe_api.update_onetime_prices(settings.stripe_onetime, settings.pricing, data, dry_run=dry_run)

    return _report(failures, dry_run=dry_run)


def guided_flow() -> int:
    settings = load_settings()
    ensure_data_fresh(settings)

    if not _available_platform_names(settings):
        console.print("No store credentials configured yet - let's set them up.")
        wizard.run_setup(None)
        settings = load_settings()
        if not _available_platform_names(settings):
            console.print("[red]Still not configured - run `pricing.py setup` when you're ready.[/red]")
            return 1
    else:
        configured = _available_platform_names(settings)
        console.print(f"Using credentials for: {', '.join(configured)} (`pricing.py doctor` to re-check, `pricing.py setup` to add more)")

    price = float(text("Base price in USD (what you charge in the US):", default="9.99", validate=_validate_price))

    df = scaler.run(price, settings.pricing)
    path = scaler.save_results(df)
    console.print(price_preview_table(df))
    console.print(price_summary(df))
    console.print(f"Saved to {path}")

    available = _available_platform_names(settings)
    if len(available) == 1:
        if not confirm(f"Push these prices live to {available[0]}?", default=False):
            return 0
        platforms = {_platform_key(available[0])}
    else:
        chosen = checkbox("Push these prices live? Select target(s) (leave empty for just the spreadsheet):", available, allow_empty=True)
        if not chosen:
            return 0
        platforms = {_platform_key(c) for c in chosen}

    mode = select("How?", [
        "Preview the diff, then ask for confirmation before pushing",
        "Dry-run only - resolve everything, push nothing",
    ])
    dry_run = mode.startswith("Dry-run")

    return _apply_flow(settings, platforms, dry_run=dry_run)


def cmd_scale(args) -> int:
    settings = load_settings()
    ensure_data_fresh(settings)

    price = args.price if args.price is not None else float(text("Base price in USD:", default="9.99", validate=_validate_price))
    df = scaler.run(price, settings.pricing)
    path = scaler.save_results(df, Path(args.out))

    console.print(price_preview_table(df))
    console.print(price_summary(df))
    console.print(f"Saved to {path}")
    return 0


def cmd_apply(args) -> int:
    settings = load_settings()
    platforms = _resolve_platforms(
        settings, args.apple, args.google, args.stripe,
        want_apple_iap=args.apple_iap, want_google_iap=args.google_iap, want_stripe_onetime=args.stripe_onetime,
    )
    if not platforms:
        console.print("[red]No configured platform available. Run `pricing.py setup`.[/red]")
        return 1
    return _apply_flow(settings, platforms, dry_run=args.dry_run, skip_confirm=args.yes)


def _select_offer_platforms(settings: Settings, args) -> set[str]:
    # Promotional offers are subscription-only (see _available_offer_platform_names) - a
    # one-time purchase has no percentage-off-for-N-cycles concept to map this onto.
    if args.apple or args.google or args.stripe:
        return _resolve_platforms(settings, args.apple, args.google, args.stripe)

    available = _available_offer_platform_names(settings)
    if not available:
        return set()
    if len(available) == 1:
        return {available[0].lower()}
    chosen = checkbox("Which store(s) do you want to create this offer on?", available, allow_empty=True)
    return {c.lower() for c in chosen}


def cmd_offer(args) -> int:
    settings = load_settings()
    platforms = _select_offer_platforms(settings, args)
    if not platforms:
        console.print("[red]No configured platform selected. Run `pricing.py setup`.[/red]")
        return 1

    data = load_input_data()

    discount = args.discount
    if discount is None:
        discount = float(text("Discount percentage off the current price (e.g. 30):", validate=_validate_percent))
    elif not 0 < discount < 100:
        console.print("[red]--discount must be between 0 and 100.[/red]")
        return 1

    cycles = args.cycles
    if cycles is None:
        cycles = int(text("Number of billing cycles the discount applies to:", default="1", validate=_validate_positive_int))
    elif cycles < 1:
        console.print("[red]--cycles must be at least 1.[/red]")
        return 1

    if args.period:
        match = next((pair for label, *pair in offers_mod.DURATIONS if args.period in pair), None)
        if match is None:
            console.print(f"[red]Unknown --period '{args.period}'. Choices: {', '.join(d[2] for d in offers_mod.DURATIONS)}[/red]")
            return 1
        apple_duration, google_duration = match
    else:
        label = select("Billing period of the subscription this offer applies to:", [d[0] for d in offers_mod.DURATIONS])
        apple_duration, google_duration = offers_mod.duration_by_label(label)

    if "stripe" in platforms and stripe_api.months_for_period(google_duration) is None:
        console.print(
            f"[red]Stripe coupons need a whole number of months - '{google_duration}' has no month "
            f"equivalent. Pick a monthly/yearly --period, or drop --stripe.[/red]"
        )
        return 1

    # --code doubles as the Google offer id when --google-offer-id isn't given, and as the
    # Stripe coupon id / promotion code. Google rejects anything outside [a-z0-9-], so an
    # Apple-style code like "SUMMER30" needs the explicit flag (or the prompt below); warn
    # rather than silently sending a 400.
    offer_name, offer_code = args.name, args.code
    google_offer_id = args.google_offer_id or args.code
    if "google" in platforms and google_offer_id and not _GOOGLE_OFFER_ID_RE.match(google_offer_id):
        console.print(
            f"[red]'{google_offer_id}' isn't a valid Google Play offer id (lowercase letters, "
            f"numbers and hyphens only). Pass --google-offer-id to set it separately from the "
            f"Apple offer code.[/red]"
        )
        return 1

    if "apple" in platforms or "stripe" in platforms:
        if not offer_name:
            offer_name = text("Offer reference name (human-readable, e.g. 'Summer sale 30% off'):", validate=_validate_nonempty)
        if not offer_code:
            offer_code = text("Offer code (unique alphanumeric identifier, e.g. SUMMER30):", validate=_validate_nonempty)

    if "apple" in platforms:
        existing = offers_mod.find_existing_apple_offer(settings.apple, offer_code)
        if existing and not args.dry_run:
            console.print(f"[yellow]An Apple offer with code '{offer_code}' already exists. Apple's API only supports "
                           f"recreating it: the existing offer will be deleted, then a new one created - there's a "
                           f"brief window where no offer is live.[/yellow]")
            if not args.yes and not confirm(f"Delete and recreate offer '{offer_code}'?", default=False):
                console.print("Aborted - nothing was changed.")
                return 0

    if "google" in platforms and not google_offer_id:
        google_offer_id = text("Google Play offer id (lowercase letters/numbers/hyphens, e.g. summer-30-off):", validate=_validate_google_offer_id)

    config = offers_mod.OfferConfig(
        platforms=platforms, discount_percent=discount, num_periods=cycles,
        apple_duration=apple_duration, google_duration=google_duration,
        offer_name=offer_name, offer_code=offer_code, google_offer_id=google_offer_id,
        offer_mode=args.mode,
    )

    console.print(f"\nDiscount: {discount}% for {cycles} cycle(s) of {apple_duration}")

    failures = []
    if "google" in platforms:
        failures += offers_mod.create_google_offer(settings.google, data, config, dry_run=args.dry_run)
    if "apple" in platforms:
        failures += offers_mod.create_apple_offer(settings.apple, settings.pricing, data, config, dry_run=args.dry_run)
    if "stripe" in platforms:
        failures += offers_mod.create_stripe_offer(settings.stripe, config, dry_run=args.dry_run)

    return _report(failures, dry_run=args.dry_run)


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_toml_exists()

    if args.command is None:
        return guided_flow()
    if args.command == "setup":
        platforms = set()
        if args.apple:
            platforms.add("apple")
        if args.google:
            platforms.add("google")
        if args.stripe:
            platforms.add("stripe")
        wizard.run_setup(platforms or None)
        return 0
    if args.command == "doctor":
        return 0 if wizard.run_doctor() else 1
    if args.command == "scale":
        return cmd_scale(args)
    if args.command == "apply":
        return cmd_apply(args)
    if args.command == "offer":
        return cmd_offer(args)
    if args.command == "refresh-data":
        # Asking for the refresh explicitly always refreshes; the
        # staleness gate only applies to the automatic refresh in ensure_data_fresh().
        # What was missing here is pricing.toml's [data] ppp_year - calling refresh_all()
        # with no arguments silently pinned it back to "auto", so the one command whose
        # whole job is this refresh was the one place the setting didn't apply.
        data_mod.refresh_all(load_settings().pricing.ppp_year)
        return 0

    parser.print_help()
    return 1
