"""Shared rich/questionary rendering and prompting, so the guided flow and the individual
subcommands present prices, diffs, and confirmations identically."""

from __future__ import annotations

import pandas as pd
import questionary
from questionary import Style
from rich.console import Console
from rich.table import Table

console = Console()

PROMPT_STYLE = Style([
    ("qmark", "fg:#00afff bold"),
    ("question", "bold"),
    ("answer", "fg:#00af5f bold"),
    ("pointer", "fg:#00afff bold"),
    ("highlighted", "fg:#00afff bold"),
    ("selected", "fg:#00af5f"),
])


def _answered(result):
    """questionary returns None when the prompt is interrupted (Ctrl-C) or stdin isn't a
    TTY. Callers immediately do things like float(text(...)), so letting that None through
    surfaces as a TypeError traceback instead of a clean abort - and mid-flow, right before
    a store write, an ambiguous traceback is the worst possible thing to show."""
    if result is None:
        raise SystemExit(130)
    return result


def text(message: str, default: str = "", validate=None) -> str:
    return _answered(questionary.text(message, default=default, validate=validate, style=PROMPT_STYLE).ask())


def confirm(message: str, default: bool = False) -> bool:
    return bool(_answered(questionary.confirm(message, default=default, style=PROMPT_STYLE).ask()))


def select(message: str, choices: list[str], default: "str | None" = None) -> str:
    return _answered(questionary.select(message, choices=choices, default=default, style=PROMPT_STYLE).ask())


def checkbox(message: str, choices: list[str], allow_empty: bool = False) -> list[str]:
    # questionary's own default is a permissive `lambda a: True`, not None - passing
    # validate=None outright raises "validate must be callable" rather than being treated
    # as "no validation", so the allow_empty=True case needs its own permissive callable.
    validate = (lambda selected: True) if allow_empty else (
        lambda selected: True if selected else "Select at least one (space to toggle, enter to confirm)"
    )
    return _answered(questionary.checkbox(message, choices=choices, style=PROMPT_STYLE, validate=validate).ask())


def price_preview_table(df: pd.DataFrame, limit: int = 25) -> Table:
    table = Table(title=f"Price preview - {len(df)} countries", show_lines=False)
    table.add_column("Country")
    table.add_column("Local price", justify="right")
    table.add_column("≈ USD", justify="right")
    table.add_column("VAT", justify="right")
    table.add_column("Factor", justify="right")
    table.add_column("Basis")

    for _, row in df.head(limit).iterrows():
        table.add_row(
            row["Country"],
            f"{row['Currency_Code']} {row['Smart_Price_Native']:.2f}",
            f"${row['Smart_Price_USD']:.2f}",
            f"{row['Tax_Rate'] * 100:.0f}%",
            f"{row['Scaling_Factor']:.2f}",
            row.get("Pricing_Basis", "ppp"),
        )
    if len(df) > limit:
        table.caption = f"... and {len(df) - limit} more (see price_scaled.xlsx for the full list)"
    return table


def price_summary(df: pd.DataFrame) -> str:
    usd = df["Smart_Price_USD"]
    return (
        f"{len(df)} countries · median ${usd.median():.2f} · "
        f"min ${usd.min():.2f} ({df.loc[usd.idxmin(), 'Country']}) · "
        f"max ${usd.max():.2f} ({df.loc[usd.idxmax(), 'Country']})"
    )


def diff_table(
    platform: str,
    live: dict[str, dict],
    new_prices: dict,
    currencies: dict,
    currency_mismatch_blocks: bool = False,
    usd_rates: "dict | None" = None,
    country_prices_usd: "dict | None" = None,
) -> "tuple[Table, int, int]":
    """live: {country: {"price": float, "currency": str | None}}.
    Returns (table, applyable_count, locked_count).

    The live currency can legitimately differ from the new price's currency: Apple prices
    some territories in USD instead of their local currency but resolves per-territory
    regardless of what was previously live, so a difference there doesn't block anything.
    Google is stricter - it rejects any price update where the submitted currency doesn't
    match the region's existing currency outright (no in-place currency change via that
    endpoint), *unless* the price can be auto-converted into whatever currency Google
    already has on file (see google._update_region()/convert_to_currency()).

    `currency_mismatch_blocks=True` (pass for Google) means a currency difference needs
    that conversion check: pass `usd_rates` (from scaler.fetch_all_usd_rates()) and
    `country_prices_usd` (InputData.country_prices_usd) so a row can be predicted as
    "fx convert" (applyable) when a rate is available, or "locked" (excluded from the
    applyable count) only when it genuinely isn't - rather than treating every currency
    difference as a guaranteed failure, which buried the small number of real price changes
    under a wall of identical-looking rows for a subscription with years of flat-USD-per-
    region pricing, most of which now actually succeed via conversion.
    """
    table = Table(title=f"{platform}: live → new", show_lines=False)
    table.add_column("Country")
    table.add_column("Live", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Status", justify="right")

    applyable = 0
    locked = 0
    unchanged = 0
    locked_rows = []
    for country, new_price in new_prices.items():
        new_currency = currencies.get(country, "")
        live_entry = live.get(country)
        if live_entry is None:
            table.add_row(country, "—", f"{new_currency} {new_price:.2f}", "[green]new[/green]")
            applyable += 1
            continue

        live_price = live_entry["price"]
        live_currency = live_entry.get("currency") or new_currency
        currency_differs = live_currency != new_currency

        if not currency_differs and abs(live_price - float(new_price)) < 0.01:
            unchanged += 1
            continue

        live_cell = f"{live_currency} {live_price:.2f}"
        new_cell = f"{new_currency} {new_price:.2f}"

        if currency_differs and currency_mismatch_blocks:
            can_convert = (
                usd_rates is not None and live_currency in usd_rates
                and country_prices_usd is not None and country in country_prices_usd
            )
            if can_convert:
                table.add_row(country, live_cell, new_cell, "[yellow]fx convert[/yellow]")
                applyable += 1
            else:
                locked += 1
                locked_rows.append((country, live_cell, new_cell))
            continue
        elif currency_differs:
            status = "[yellow]fx change[/yellow]"
        else:
            delta_pct = ((float(new_price) - live_price) / live_price * 100) if live_price else 0
            status = f"{delta_pct:+.0f}%"

        table.add_row(country, live_cell, new_cell, status)
        applyable += 1

    # Locked rows are appended after the applyable ones and visually marked, rather than
    # interleaved - they read as a distinct "these will fail" block, not scattered noise.
    for country, live_cell, new_cell in locked_rows:
        table.add_row(country, live_cell, new_cell, "[red]locked[/red]")

    caption_bits = []
    if unchanged:
        caption_bits.append(f"{unchanged} unchanged (not shown)")
    if locked:
        caption_bits.append(
            f"{locked} currency-locked - no exchange rate available to auto-convert; will "
            f"be rejected until fixed manually in Play Console (see the failure report)"
        )
    if caption_bits:
        table.caption = " · ".join(caption_bits)

    return table, applyable, locked


_FAILURE_CATEGORIES = [
    # Listed before the "No matching Apple price point" entry on purpose: a throttled or
    # timed-out request is transient and worth re-running, whereas a territory Apple has no
    # price point for is not. Collapsing the two reads as "these countries are unpriceable"
    # when the real answer is "try again".
    ("Apple API error", "Apple API error - rate limit or timeout (transient, re-run)"),
    ("Currency mismatch", "Currency mismatch (no exchange rate to auto-convert)"),
    ("Region not found", "Region not configured on this base plan / purchase option"),
    ("Base plan", "Base plan not found in subscription"),
    ("not found in one-time product", "Purchase option not found in one-time product"),
    ("No price points available", "No matching Apple price point"),
    ("Price difference too large", "Closest Apple price point too far off"),
    ("clamped to Google Play minimum", "Price clamped to Google's minimum"),
    ("Removed from", "Removed from base plan / purchase option (repeated clamp failure)"),
    ("Could not fetch subscription", "Couldn't reach the store (subscription fetch failed)"),
    ("Could not fetch one-time product", "Couldn't reach the store (one-time product fetch failed)"),
    ("Could not find in-app purchase", "Couldn't reach the store (in-app purchase not found)"),
    ("can't be changed in place via currency_options", "Locked to Stripe's base Price currency"),
    ("Rejected by Stripe", "Currency rejected by Stripe"),
    ("Missing country/currency code mapping", "Missing country/currency code mapping"),
    ("Unhandled exception", "Unhandled exception"),
]


def _categorize_failure(reason: str) -> str:
    for needle, label in _FAILURE_CATEGORIES:
        if needle in reason:
            return label
    return reason.split(":")[0] if ":" in reason else reason


def failure_summary_table(failures: list[dict]) -> Table:
    """Groups failures by platform + category instead of listing every country as its own
    row - a subscription with a lot of legacy pricing debt can produce 100+ failures that
    are mostly the same handful of underlying reasons, and a flat list of that size reads
    as noise rather than something actionable."""
    groups: "dict[tuple[str, str], list[str]]" = {}
    for f in failures:
        key = (f["platform"], _categorize_failure(f["reason"]))
        groups.setdefault(key, []).append(f"{f['country']} ({f['country_code']})")

    table = Table(title=f"{len(failures)} failures, {len(groups)} distinct reason(s)", show_lines=True)
    table.add_column("Platform")
    table.add_column("Reason")
    table.add_column("Countries")

    for (platform, reason), countries in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        shown = countries[:8]
        countries_str = ", ".join(shown)
        if len(countries) > len(shown):
            countries_str += f", +{len(countries) - len(shown)} more"
        table.add_row(platform, f"{reason} ({len(countries)})", countries_str)

    return table
