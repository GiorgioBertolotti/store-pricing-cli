import pandas as pd
import pytest

from store_pricing.config import PricingConfig
from store_pricing.scaler import apply_smart_pricing, calculate_scaling_factors
from store_pricing.data import MEAL_COLUMN


# --- apply_smart_pricing boundaries ---

@pytest.mark.parametrize("price,expected", [
    (0.0, 0.0),
    (0.001, 0.09),
    (0.10, 0.19),
    (0.20, 0.29),
    (0.30, 0.39),
    (0.40, 0.49),
    (0.50, 0.59),
    (0.60, 0.69),
    (0.70, 0.79),
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 0.99),
    (0.999, 0.99),
])
def test_smart_pricing_sub_dollar_tiers(price, expected):
    assert apply_smart_pricing(price) == expected


@pytest.mark.parametrize("price,expected", [
    (1.00, 0.99),   # cents=0 < 50 -> rounds down to the previous dollar's .99
    (1.49, 0.99),
    (1.50, 1.99),   # cents=50 pivot -> rounds up
    (9.99, 9.99),
    (10.49, 9.99),
    (10.50, 10.99),
    (99.50, 99.99),
])
def test_smart_pricing_dollar_range_pivot(price, expected):
    assert apply_smart_pricing(price) == pytest.approx(expected)


@pytest.mark.parametrize("price,expected", [
    (100.0, 100),
    (102.4, 100),   # round to nearest 5 under $1000
    (102.6, 105),
    (999.99, 1000),
    (1000.0, 1000),  # round to nearest 10 at/above $1000
    (1004.0, 1000),
    (1006.0, 1010),
])
def test_smart_pricing_high_tiers(price, expected):
    assert apply_smart_pricing(price) == pytest.approx(expected)


def test_smart_pricing_none_rounding_is_a_noop():
    assert apply_smart_pricing(12.3456, rounding="none") == 12.3456


# --- scaling factor cap ---

def _cost_of_living_df():
    return pd.DataFrame([
        {"CountryName": "United States", "CurrencyCode": "USD", MEAL_COLUMN: 80.0},
        {"CountryName": "Switzerland", "CurrencyCode": "CHF", MEAL_COLUMN: 100.0},  # pricier than anchor
        {"CountryName": "India", "CurrencyCode": "INR", MEAL_COLUMN: 800.0},  # cheaper than anchor
    ])


def test_scaling_factor_capped_at_one_for_expensive_countries(monkeypatch):
    monkeypatch.setattr(
        "store_pricing.scaler.fetch_exchange_rates",
        lambda currencies: {"USD": 1.0, "CHF": 0.9, "INR": 83.0},
    )
    factors = calculate_scaling_factors(_cost_of_living_df(), PricingConfig())

    assert factors["United States"]["scaling_factor"] == pytest.approx(1.0)
    # Switzerland's meal is pricier than the US anchor -> factor capped at 1.0, not >1.
    assert factors["Switzerland"]["scaling_factor"] == pytest.approx(1.0)
    # India is cheaper -> factor strictly below 1.0.
    assert 0 < factors["India"]["scaling_factor"] < 1.0


def test_scaling_cap_respects_config_override(monkeypatch):
    monkeypatch.setattr(
        "store_pricing.scaler.fetch_exchange_rates",
        lambda currencies: {"USD": 1.0, "CHF": 0.9, "INR": 83.0},
    )
    factors = calculate_scaling_factors(_cost_of_living_df(), PricingConfig(scaling_cap=0.5))
    assert factors["United States"]["scaling_factor"] == pytest.approx(0.5)
    assert factors["Switzerland"]["scaling_factor"] == pytest.approx(0.5)


def test_missing_anchor_country_is_a_hard_error(monkeypatch):
    monkeypatch.setattr("store_pricing.scaler.fetch_exchange_rates", lambda currencies: {"USD": 1.0})
    df = pd.DataFrame([{"CountryName": "India", "CurrencyCode": "INR", MEAL_COLUMN: 800.0}])
    with pytest.raises(ValueError, match="Anchor country"):
        calculate_scaling_factors(df, PricingConfig())


# --- VAT lookup ---

def test_vat_rate_known_country():
    assert PricingConfig().vat_rate("Germany") == pytest.approx(0.19)


def test_vat_rate_default_for_unknown_country():
    assert PricingConfig(default_vat=0.15).vat_rate("Atlantis") == pytest.approx(0.15)


def test_vat_rate_override_wins_over_builtin_table():
    config = PricingConfig(vat_overrides={"Germany": 0.05})
    assert config.vat_rate("Germany") == pytest.approx(0.05)


# --- market ratios ---

def _factor(exchange_rate: float, scaling_factor: float, currency: str) -> dict:
    return {
        "scaling_factor": scaling_factor,
        "meal_price_native": 1.0,
        "meal_price_usd": 1.0,
        "exchange_rate": exchange_rate,
        "purchasing_power_ratio": 1.0 / scaling_factor,
        "currency_code": currency,
    }


def test_market_ratio_prices_off_the_anchors_gross_price_without_adding_vat_again():
    from store_pricing.scaler import scale_price

    config = PricingConfig(rounding="none", market_ratios={"annual": {"Italy": 1.10}})
    factors = {"United States": _factor(1.0, 1.0, "USD"), "Italy": _factor(0.9, 0.7, "EUR")}

    df = scale_price(20.0, factors, config, plan="annual").set_index("Country")

    assert df.loc["Italy", "Smart_Price_Native"] == pytest.approx(20.0 * 1.10 * 0.9)
    assert df.loc["Italy", "Pricing_Basis"] == "market"
    assert df.loc["Italy", "Scaled_Price_Native"] == pytest.approx(20.0 * 1.10 * 0.9 / 1.22)
    assert df.loc["United States", "Pricing_Basis"] == "ppp"


def test_without_a_plan_market_ratios_are_ignored_and_ppp_applies():
    from store_pricing.scaler import scale_price

    config = PricingConfig(rounding="none", market_ratios={"annual": {"Italy": 1.10}})
    factors = {"Italy": _factor(0.9, 0.7, "EUR")}

    df = scale_price(20.0, factors, config).set_index("Country")

    assert df.loc["Italy", "Smart_Price_Native"] == pytest.approx(20.0 * 0.7 * 0.9 * 1.22)
    assert df.loc["Italy", "Pricing_Basis"] == "ppp"


def test_a_country_missing_from_the_plans_ratios_falls_back_to_ppp():
    from store_pricing.scaler import scale_price

    config = PricingConfig(rounding="none", market_ratios={"monthly": {"Italy": 0.85}})
    factors = {"Italy": _factor(0.9, 0.7, "EUR")}

    df = scale_price(20.0, factors, config, plan="annual").set_index("Country")

    assert df.loc["Italy", "Pricing_Basis"] == "ppp"


def test_unknown_market_ratio_countries_are_reported():
    from store_pricing.scaler import unknown_market_ratio_countries

    config = PricingConfig(market_ratios={"annual": {"Turkey": 0.8, "Italy": 1.05}})

    assert unknown_market_ratio_countries(config, "annual", {"Italy", "Turkiye"}) == ["Turkey"]
    assert unknown_market_ratio_countries(config, None, {"Italy"}) == []


def test_vat_table_uses_world_bank_country_names():
    import json
    from pathlib import Path

    from store_pricing.config import DEFAULT_VAT_RATES

    known = set(json.loads(Path("country_codes.json").read_text()))
    # Present in the VAT table but absent from the World Bank PPP dataset entirely.
    no_ppp_data = {"Argentina", "Liechtenstein", "Taiwan"}
    assert sorted(set(DEFAULT_VAT_RATES) - known - no_ppp_data) == []


# --- psychological-half rounding ---

@pytest.mark.parametrize("price,expected", [
    (2.20, 1.99),
    (2.30, 2.49),
    (2.70, 2.49),
    (2.80, 2.99),
    (18.60, 18.49),
    (0.50, 0.59),     # sub-unit tiers are unchanged
    (247.0, 245),     # 100+ steps are unchanged
])
def test_half_rounding_picks_the_nearest_49_or_99_ending(price, expected):
    assert apply_smart_pricing(price, rounding="psychological-half") == pytest.approx(expected)


def test_plan_rounding_overrides_the_default_only_for_that_plan():
    from store_pricing.scaler import scale_price

    config = PricingConfig(plan_rounding={"monthly": "psychological-half"})
    factors = {"United States": _factor(1.0, 1.0, "USD")}

    monthly = scale_price(2.40, factors, config, plan="monthly").set_index("Country")
    annual = scale_price(2.40, factors, config, plan="annual").set_index("Country")

    assert monthly.loc["United States", "Smart_Price_Native"] == pytest.approx(2.49)
    assert monthly.loc["United States", "Rounding"] == "psychological-half"
    assert annual.loc["United States", "Smart_Price_Native"] == pytest.approx(1.99)
