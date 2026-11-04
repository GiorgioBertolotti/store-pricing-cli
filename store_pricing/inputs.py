"""Loads the two files every apply/offer step reads: price_scaled.xlsx (from scaler.py)
and country_codes.json (from data.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

SCALED_PRICES_FILE = Path("price_scaled.xlsx")
COUNTRY_CODES_FILE = Path("country_codes.json")


@dataclass(frozen=True)
class InputData:
    country_prices: dict
    country_prices_usd: dict
    country_currencies: dict
    country_code_mapping: dict
    # The rounding the prices were scaled with, so a currency conversion at apply time rounds
    # the same way. None for spreadsheets written before the column existed.
    rounding: "str | None" = None


def load_input_data(
    prices_path: Path = SCALED_PRICES_FILE,
    country_codes_path: Path = COUNTRY_CODES_FILE,
) -> InputData:
    try:
        prices_df = pd.read_excel(prices_path)
    except FileNotFoundError:
        raise SystemExit(f"'{prices_path}' not found. Run `pricing.py scale` first.")
    except Exception as e:
        raise SystemExit(f"Failed to read {prices_path}: {e}")

    try:
        with country_codes_path.open() as f:
            country_code_mapping = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"'{country_codes_path}' not found. Run `pricing.py refresh-data` first.")
    except Exception as e:
        raise SystemExit(f"Failed to read {country_codes_path}: {e}")

    return InputData(
        country_prices=dict(zip(prices_df["Country"], prices_df["Smart_Price_Native"])),
        country_prices_usd=dict(zip(prices_df["Country"], prices_df["Smart_Price_USD"])),
        country_currencies=dict(zip(prices_df["Country"], prices_df["Currency_Code"])),
        country_code_mapping=country_code_mapping,
        rounding=prices_df["Rounding"].iloc[0] if "Rounding" in prices_df.columns and len(prices_df) else None,
    )


def get_alpha_2_country_code(country_name: str, country_code_mapping: dict) -> "str | None":
    entry = country_code_mapping.get(country_name)
    return entry.get("alpha2") if entry else None


def get_alpha_3_country_code(country_name: str, country_code_mapping: dict) -> "str | None":
    entry = country_code_mapping.get(country_name)
    return entry.get("alpha3") if entry else None
