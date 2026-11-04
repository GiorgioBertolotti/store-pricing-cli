"""Price scaling: cost-of-living data + live exchange rates -> a per-country price ladder.

Uses "Meal for 2 People, Mid-range Restaurant, Three-course" as the PPP anchor metric
(see data.py for why the constant behind it doesn't actually matter).

The original PriceScaler class did file-read + HTTP + computation inside __init__, so it
couldn't be constructed without hitting the network, and its two genuinely pure helpers
(smart-pricing rounding, VAT lookup) were instance methods that never touched `self`. Both
are now plain functions - `apply_smart_pricing` here, VAT resolution as
`PricingConfig.vat_rate` in config.py - so they're directly unit-testable, and the pipeline
stages (load / fetch rates / compute) are explicit steps the caller controls and can show
progress for, rather than one eager constructor.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

from store_pricing.config import PricingConfig
from store_pricing.data import MEAL_COLUMN

EXCHANGE_RATE_URL = "https://api.exchangerate-api.com/v4/latest/USD"
COST_OF_LIVING_FILE = Path("cost_of_living_data.xlsx")
SCALED_PRICES_FILE = Path("price_scaled.xlsx")

# Currencies pegged 1:1 to USD not available in the free exchangerate-api tier.
USD_PEGGED = {"BSD", "PAB"}


def load_cost_of_living(path: Path = COST_OF_LIVING_FILE) -> pd.DataFrame:
    try:
        df = pd.read_excel(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"'{path}' not found. Run `pricing.py refresh-data` first.")

    if MEAL_COLUMN not in df.columns:
        raise ValueError(f"Column '{MEAL_COLUMN}' not found in {path}")
    if "CurrencyCode" not in df.columns:
        raise ValueError(f"CurrencyCode column not found in {path}. Refresh it with `pricing.py refresh-data`.")

    valid = df.dropna(subset=[MEAL_COLUMN]).reset_index(drop=True)
    dropped = len(df) - len(valid)
    if dropped:
        print(f"Dropped {dropped} countries with invalid meal-price data")
    return valid


def fetch_all_usd_rates() -> dict[str, float]:
    """Fetch the full USD exchange rate table - every currency the API knows about, not
    filtered to any particular country list. Used both by `fetch_exchange_rates` below and
    by google.py to convert a price into whatever currency a Google Play region actually
    expects (see google._update_region())."""
    # Short retry/backoff: this is an unauthenticated public API, same class of
    # occasional transient failure as World Bank's (see data.py's _get_with_retries).
    last_error = None
    response = None
    for attempt in range(3):
        try:
            response = requests.get(EXCHANGE_RATE_URL, timeout=10)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_error = e
            response = None
            if attempt < 2:
                time.sleep(2 ** attempt)
    if response is None:
        raise last_error

    rates_raw = response.json().get("rates", {})
    if not rates_raw:
        raise ValueError("Exchange rate API returned empty or missing rates - unexpected response format")

    for curr in USD_PEGGED:
        rates_raw.setdefault(curr, 1.0)

    return rates_raw


def fetch_exchange_rates(currencies: list[str]) -> dict[str, float]:
    """Fetch current USD exchange rates for the given currency codes."""
    currencies = list(currencies)
    if "USD" not in currencies:
        currencies.append("USD")

    all_rates = fetch_all_usd_rates()
    rates = {c: all_rates[c] for c in currencies if c in all_rates}

    missing = [c for c in currencies if c not in rates]
    if missing:
        raise RuntimeError(f"Could not fetch exchange rates for currencies: {missing}")

    return rates


def calculate_scaling_factors(df: pd.DataFrame, config: PricingConfig) -> dict[str, dict]:
    """Compute each country's scaling factor relative to `config.anchor_country`.

    scaling_factor = min(ppp_factor_relative_to_anchor, config.scaling_cap) - a country
    pricier than the anchor pays the full price; a cheaper one gets a proportional
    discount, capped so nobody pays more than the anchor's price.
    """
    exchange_rates = fetch_exchange_rates(df["CurrencyCode"].unique().tolist())

    anchor_mask = df["CountryName"] == config.anchor_country
    if not anchor_mask.any():
        raise ValueError(
            f"Anchor country '{config.anchor_country}' not found in the cost-of-living data. "
            "Set [pricing].anchor_country in pricing.toml to a country present in "
            "cost_of_living_data.xlsx, or refresh the data."
        )

    anchor_meal_native = df.loc[anchor_mask, MEAL_COLUMN].iloc[0]
    anchor_currency = df.loc[anchor_mask, "CurrencyCode"].iloc[0]
    anchor_meal_usd = anchor_meal_native / exchange_rates.get(anchor_currency, 1.0)

    factors: dict[str, dict] = {}
    for _, row in df.iterrows():
        country = row["CountryName"]
        currency_code = row["CurrencyCode"]
        meal_price_native = row[MEAL_COLUMN]

        if pd.isna(meal_price_native) or meal_price_native <= 0:
            continue

        exchange_rate = exchange_rates.get(currency_code, 1.0)
        if exchange_rate <= 0:
            print(f"Skipping {country} - invalid exchange rate ({exchange_rate})")
            continue

        meal_price_usd = meal_price_native / exchange_rate
        purchasing_power_ratio = max(anchor_meal_usd / meal_price_usd, 1.0)
        scaling_factor = min(1.0 / purchasing_power_ratio, config.scaling_cap)

        factors[country] = {
            "scaling_factor": scaling_factor,
            "meal_price_native": meal_price_native,
            "meal_price_usd": meal_price_usd,
            "exchange_rate": exchange_rate,
            "purchasing_power_ratio": purchasing_power_ratio,
            "currency_code": currency_code,
        }

    if not factors:
        raise RuntimeError("No countries produced a valid scaling factor - nothing to price")

    return factors


def apply_smart_pricing(price: float, rounding: str = "psychological") -> float:
    """Round a taxed price to a psychologically appealing price point.

    rounding="none" returns the price unrounded, for anyone who'd rather set exact
    values (e.g. testing, or a store that already enforces its own price tiers).
    """
    if rounding == "none" or price <= 0:
        return price

    # For prices under $1, snap up to nearest standard price tier ending in .x9
    if price < 1.0:
        cents = int(price * 100)
        tiers = [(95, 0.99), (90, 0.95), (85, 0.90), (80, 0.85), (70, 0.79),
                 (60, 0.69), (50, 0.59), (40, 0.49), (30, 0.39), (20, 0.29), (10, 0.19)]
        for threshold, value in tiers:
            if cents >= threshold:
                return value
        return 0.09

    # For prices $1-$100, use a .99 ending, rounding down under the half-dollar mark
    if price < 100.0:
        dollars = int(price)
        cents = int((price - dollars) * 100)
        return dollars + 0.99 if cents >= 50 else (dollars - 1) + 0.99

    # For prices $100+, round to the nearest 5 (under $1000) or 10 (above)
    if price < 1000:
        return round(price / 5) * 5
    return round(price / 10) * 10


def convert_to_currency(usd_price: "float | None", target_currency: str, usd_rates: dict, rounding: str = "psychological") -> "float | None":
    """Convert a USD price into `target_currency` using a fetched USD-rate table, applying
    the same smart-pricing rounding used everywhere else. Returns None if conversion isn't
    possible (no USD price on hand, or the rate table doesn't know this currency).

    Shared by google.py (converting into whatever currency a Google Play region is
    currently locked to) and apple.py (converting into whatever currency Apple actually
    prices a territory in, when it differs from ours - not just USD).
    """
    if usd_price is None:
        return None
    rate = usd_rates.get(target_currency)
    if rate is None:
        return None
    return apply_smart_pricing(usd_price * rate, rounding)


def scale_price(
    usd_amount: float, scaling_factors: dict[str, dict], config: PricingConfig, plan: "str | None" = None
) -> pd.DataFrame:
    """Scale a USD amount to every country's local, tax-inclusive, smart-rounded price.

    With a `plan`, a country that has a `[pricing.market_ratio.<plan>]` entry is priced at
    that ratio of the anchor's tax-inclusive price instead of by PPP; its ratio already
    includes local tax, so VAT is not added again.
    """
    anchor_gross_usd = usd_amount * (1 + config.vat_rate(config.anchor_country))
    results = []
    for country, factor_data in scaling_factors.items():
        exchange_rate = factor_data["exchange_rate"]
        tax_rate = config.vat_rate(country)
        market_ratio = config.market_ratio(plan, country)

        if market_ratio is not None:
            taxed_price_native = anchor_gross_usd * market_ratio * exchange_rate
            scaled_price_native = taxed_price_native / (1 + tax_rate)
            scaled_price_usd = scaled_price_native / exchange_rate
            scaling_factor = scaled_price_usd / usd_amount
            basis = "market"
        else:
            scaling_factor = factor_data["scaling_factor"]
            scaled_price_usd = usd_amount * scaling_factor
            scaled_price_native = scaled_price_usd * exchange_rate
            taxed_price_native = scaled_price_native * (1 + tax_rate)
            basis = "ppp"

        smart_price_native = apply_smart_pricing(taxed_price_native, config.rounding)
        smart_price_usd = smart_price_native / exchange_rate

        results.append({
            "Country": country,
            "Currency_Code": factor_data["currency_code"],
            "Original_USD_Amount": usd_amount,
            "Scaled_Price_USD": scaled_price_usd,
            "Scaled_Price_Native": scaled_price_native,
            "Taxed_Price_Native": taxed_price_native,
            "Tax_Rate": tax_rate,
            "Smart_Price_Native": smart_price_native,
            "Smart_Price_USD": smart_price_usd,
            "Scaling_Factor": scaling_factor,
            "Pricing_Basis": basis,
            "Meal_Price_Native": factor_data["meal_price_native"],
            "Meal_Price_USD": factor_data["meal_price_usd"],
            "Exchange_Rate": exchange_rate,
            "Purchasing_Power_Ratio": factor_data["purchasing_power_ratio"],
        })

    return pd.DataFrame(results).sort_values("Scaled_Price_USD", ascending=False)


def unknown_market_ratio_countries(config: PricingConfig, plan: "str | None", known: "set[str] | list[str]") -> list[str]:
    """Countries listed under the plan's market ratios that the cost-of-living data doesn't
    know, almost always a non-World-Bank spelling (e.g. "Turkey" for "Turkiye")."""
    if plan is None:
        return []
    known = set(known)
    return sorted(c for c in config.market_ratios.get(plan, {}) if c not in known)


def save_results(df: pd.DataFrame, path: Path = SCALED_PRICES_FILE) -> Path:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Price_Scaling_Results", index=False)
    return path


def run(
    usd_amount: float,
    config: PricingConfig,
    cost_of_living_path: Path = COST_OF_LIVING_FILE,
    plan: "str | None" = None,
) -> pd.DataFrame:
    """Full pipeline: load cached cost-of-living data, fetch rates, scale, and return the
    result DataFrame (caller decides whether/where to save it)."""
    df = load_cost_of_living(cost_of_living_path)
    factors = calculate_scaling_factors(df, config)
    unknown = unknown_market_ratio_countries(config, plan, factors.keys())
    if unknown:
        print(f"Ignoring market ratios for countries not in the cost-of-living data: {', '.join(unknown)}")
    return scale_price(usd_amount, factors, config, plan)
