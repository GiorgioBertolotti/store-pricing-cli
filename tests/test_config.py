"""Credential resolution and pricing.toml validation.

Two classes of regression are covered here:

  - `load_settings()` reading *only* .env. The pre-package scripts used load_dotenv() +
    os.getenv(), so credentials injected as environment variables worked; CI is documented
    as the reason `apply --yes` exists, and CI supplies secrets as env vars rather than a
    committed .env file.
  - pricing.toml values that are wrong in ways that don't announce themselves. An
    unrecognised `rounding` used to fall through to psychological rounding, so a typo
    silently produced different prices than asked for; max_workers = 0 blew up inside
    ThreadPoolExecutor mid-run rather than at load time.
"""

import pytest

from store_pricing.config import (
    ConfigError,
    load_pricing_config,
    load_settings,
    parse_apple_onetime_creds,
    parse_google_onetime_creds,
    parse_stripe_creds,
)

GOOGLE_ENV = {
    "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json",
    "GOOGLE_PACKAGE_NAME": "com.acme.app",
    "GOOGLE_SUBSCRIPTION_ID": "premium",
    "GOOGLE_BASEPLAN_ID": "monthly",
}


@pytest.fixture
def clean_env(monkeypatch):
    """Start from an environment with none of our variables set, so a developer's real
    shell exports can't make these tests pass (or fail) spuriously."""
    for key in list(GOOGLE_ENV) + [
        "APPLE_ISSUER_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY",
        "APPLE_APP_ID", "APPLE_SUBSCRIPTION_PRODUCT_ID",
        "GOOGLE_ONETIME_PRODUCT_ID", "GOOGLE_ONETIME_PURCHASE_OPTION_ID",
        "APPLE_IAP_PRODUCT_ID",
    ]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _service_account(tmp_path):
    path = tmp_path / "service-account.json"
    path.write_text("{}")
    return path


# --- credentials: environment fallback ---

def test_credentials_can_come_from_the_environment_without_a_dotenv_file(clean_env, tmp_path):
    account = _service_account(tmp_path)
    for key, value in {**GOOGLE_ENV, "GOOGLE_SERVICE_ACCOUNT_FILE": str(account)}.items():
        clean_env.setenv(key, value)

    settings = load_settings(env_path=tmp_path / "nonexistent.env", toml_path=tmp_path / "nonexistent.toml")

    assert settings.google_configured
    assert settings.google.package_name == "com.acme.app"
    assert settings.google_errors == []


def test_dotenv_wins_over_the_environment_for_variables_it_defines(clean_env, tmp_path):
    account = _service_account(tmp_path)
    for key, value in {**GOOGLE_ENV, "GOOGLE_SERVICE_ACCOUNT_FILE": str(account)}.items():
        clean_env.setenv(key, value)

    env_file = tmp_path / ".env"
    env_file.write_text(f"GOOGLE_SERVICE_ACCOUNT_FILE={account}\nGOOGLE_PACKAGE_NAME=com.from.dotenv\n")

    settings = load_settings(env_path=env_file, toml_path=tmp_path / "nonexistent.toml")

    # .env overrides the exported value...
    assert settings.google.package_name == "com.from.dotenv"
    # ...and variables it doesn't mention still fall through to the environment.
    assert settings.google.subscription_id == "premium"


def test_missing_everywhere_is_still_reported_as_unconfigured(clean_env, tmp_path):
    settings = load_settings(env_path=tmp_path / "nonexistent.env", toml_path=tmp_path / "nonexistent.toml")

    assert not settings.google_configured
    assert len(settings.google_errors) == 4


# --- credentials: placeholder rejection ---

def test_env_example_placeholders_are_not_treated_as_configured(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_SERVICE_ACCOUNT_FILE=your_path_to_the_service_account_json\n"
        "GOOGLE_PACKAGE_NAME=your_app_package_name\n"
        "GOOGLE_SUBSCRIPTION_ID=your_subscription_id\n"
        "GOOGLE_BASEPLAN_ID=your_base_plan_id\n"
    )

    settings = load_settings(env_path=env_file, toml_path=tmp_path / "nonexistent.toml")

    assert not settings.google_configured
    assert len(settings.google_errors) == 4
    assert all("placeholder" in err for err in settings.google_errors)


def test_a_plausible_real_value_is_not_mistaken_for_a_placeholder(clean_env, tmp_path):
    """"base-plan" is a perfectly ordinary base plan id, even though .env.example once
    shipped it - only the unambiguous `your_*` markers are rejected."""
    account = _service_account(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GOOGLE_SERVICE_ACCOUNT_FILE={account}\n"
        "GOOGLE_PACKAGE_NAME=com.acme.app\n"
        "GOOGLE_SUBSCRIPTION_ID=premium\n"
        "GOOGLE_BASEPLAN_ID=base-plan\n"
    )

    settings = load_settings(env_path=env_file, toml_path=tmp_path / "nonexistent.toml")

    assert settings.google_configured
    assert settings.google.baseplan_id == "base-plan"


# --- credentials: Stripe ---

def test_stripe_creds_need_a_recognisable_secret_key_prefix():
    creds, errors = parse_stripe_creds({"STRIPE_SECRET_KEY": "not-a-key", "STRIPE_PRICE_ID": "price_123"})
    assert creds is None
    assert any("STRIPE_SECRET_KEY" in e for e in errors)


def test_stripe_creds_need_a_recognisable_price_id_prefix():
    creds, errors = parse_stripe_creds({"STRIPE_SECRET_KEY": "sk_test_abc", "STRIPE_PRICE_ID": "prod_123"})
    assert creds is None
    assert any("STRIPE_PRICE_ID" in e for e in errors)


def test_a_restricted_key_is_accepted_like_a_secret_key():
    creds, errors = parse_stripe_creds({"STRIPE_SECRET_KEY": "rk_test_abc", "STRIPE_PRICE_ID": "price_123"})
    assert errors == []
    assert creds.secret_key == "rk_test_abc"


# --- pricing.toml validation ---

def test_defaults_apply_when_the_toml_is_absent(tmp_path):
    config = load_pricing_config(tmp_path / "nonexistent.toml")
    assert config.rounding == "psychological"
    assert config.scaling_cap == 1.0
    assert config.apple_max_workers == 8


def test_unrecognised_rounding_mode_is_rejected_rather_than_silently_ignored(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text('[pricing]\nrounding = "psycological"\n')  # typo

    with pytest.raises(ConfigError, match="rounding"):
        load_pricing_config(path)


def test_rounding_mode_is_case_and_whitespace_insensitive(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text('[pricing]\nrounding = "  None  "\n')
    assert load_pricing_config(path).rounding == "none"


def test_zero_max_workers_is_rejected_at_load_time(tmp_path):
    """ThreadPoolExecutor(max_workers=0) raises - but only once a run is already underway."""
    path = tmp_path / "pricing.toml"
    path.write_text("[apple]\nmax_workers = 0\n")

    with pytest.raises(ConfigError, match="max_workers"):
        load_pricing_config(path)


def test_non_positive_scaling_cap_is_rejected(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text("[pricing]\nscaling_cap = 0\n")

    with pytest.raises(ConfigError, match="scaling_cap"):
        load_pricing_config(path)


def test_negative_vat_override_is_rejected_and_names_the_country(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text("[pricing.vat]\nGermany = -0.19\n")

    with pytest.raises(ConfigError, match="Germany"):
        load_pricing_config(path)


# --- credentials: one-time products (independent of subscription config) ---

def test_google_onetime_creds_do_not_require_subscription_vars(clean_env, tmp_path):
    account = _service_account(tmp_path)
    creds, errors = parse_google_onetime_creds({
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(account),
        "GOOGLE_PACKAGE_NAME": "com.acme.app",
        "GOOGLE_ONETIME_PRODUCT_ID": "lifetime_unlock",
        "GOOGLE_ONETIME_PURCHASE_OPTION_ID": "buy",
    })

    assert errors == []
    assert creds.product_id == "lifetime_unlock"
    assert creds.purchase_option_id == "buy"


def test_apple_onetime_creds_do_not_require_subscription_vars(clean_env):
    creds, errors = parse_apple_onetime_creds({
        "APPLE_ISSUER_ID": "11111111-1111-1111-1111-111111111111",
        "APPLE_KEY_ID": "ABC123",
        "APPLE_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        "APPLE_APP_ID": "123456789",
        "APPLE_IAP_PRODUCT_ID": "lifetime_unlock",
    })

    assert errors == []
    assert creds.iap_product_id == "lifetime_unlock"


def test_settings_can_have_both_subscription_and_onetime_configured_for_the_same_store(clean_env, tmp_path):
    account = _service_account(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GOOGLE_SERVICE_ACCOUNT_FILE={account}\n"
        "GOOGLE_PACKAGE_NAME=com.acme.app\n"
        "GOOGLE_SUBSCRIPTION_ID=premium\n"
        "GOOGLE_BASEPLAN_ID=monthly\n"
        "GOOGLE_ONETIME_PRODUCT_ID=lifetime_unlock\n"
        "GOOGLE_ONETIME_PURCHASE_OPTION_ID=buy\n"
    )

    settings = load_settings(env_path=env_file, toml_path=tmp_path / "nonexistent.toml")

    assert settings.google_configured
    assert settings.google_onetime_configured
    assert settings.google.subscription_id == "premium"
    assert settings.google_onetime.product_id == "lifetime_unlock"


def test_onetime_not_configured_does_not_block_subscription(clean_env, tmp_path):
    account = _service_account(tmp_path)
    for key, value in {**GOOGLE_ENV, "GOOGLE_SERVICE_ACCOUNT_FILE": str(account)}.items():
        clean_env.setenv(key, value)

    settings = load_settings(env_path=tmp_path / "nonexistent.env", toml_path=tmp_path / "nonexistent.toml")

    assert settings.google_configured
    assert not settings.google_onetime_configured
    assert len(settings.google_onetime_errors) == 2  # only the two one-time-specific vars


def test_valid_overrides_are_parsed(tmp_path):
    path = tmp_path / "pricing.toml"
    path.write_text(
        '[pricing]\nanchor_country = "Germany"\nscaling_cap = 1.5\nrounding = "none"\n'
        "[pricing.vat]\nJapan = 0.10\n"
        "[apple]\nmax_workers = 4\n"
        '[data]\nppp_year = "2024"\n'
    )

    config = load_pricing_config(path)

    assert config.anchor_country == "Germany"
    assert config.scaling_cap == 1.5
    assert config.rounding == "none"
    assert config.vat_rate("Japan") == 0.10
    assert config.apple_max_workers == 4
    assert config.ppp_year == "2024"


def test_market_ratios_are_parsed_per_plan(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.market_ratio.annual]\nItaly = 1.05\n\n[pricing.market_ratio.monthly]\nItaly = 0.85\n')
    config = load_pricing_config(toml)
    assert config.market_ratio("annual", "Italy") == 1.05
    assert config.market_ratio("monthly", "Italy") == 0.85
    assert config.market_ratio("weekly", "Italy") is None
    assert config.market_ratio(None, "Italy") is None


def test_unknown_market_ratio_plan_is_rejected(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.market_ratio.yearly]\nItaly = 1.05\n')
    with pytest.raises(ConfigError, match="market_ratio.yearly"):
        load_pricing_config(toml)


def test_non_positive_market_ratio_is_rejected_and_names_the_country(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.market_ratio.annual]\nItaly = 0\n')
    with pytest.raises(ConfigError, match="Italy"):
        load_pricing_config(toml)


def test_plan_rounding_is_parsed_and_falls_back_to_the_default(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.plan_rounding]\nmonthly = "Psychological-Half"\n')
    config = load_pricing_config(toml)
    assert config.rounding_for("monthly") == "psychological-half"
    assert config.rounding_for("annual") == "psychological"
    assert config.rounding_for(None) == "psychological"


def test_unknown_plan_rounding_mode_is_rejected(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.plan_rounding]\nmonthly = "half"\n')
    with pytest.raises(ConfigError, match="monthly"):
        load_pricing_config(toml)


def test_market_ratio_cap_clamps_only_ratios_above_it(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text(
        '[pricing]\nmarket_ratio_cap = 1.25\n\n'
        '[pricing.market_ratio.monthly]\nNetherlands = 1.62\nItaly = 0.85\n'
    )
    config = load_pricing_config(toml)
    assert config.market_ratio("monthly", "Netherlands") == 1.25
    assert config.market_ratio("monthly", "Italy") == 0.85


def test_market_ratio_cap_defaults_to_none(tmp_path):
    toml = tmp_path / "pricing.toml"
    toml.write_text('[pricing.market_ratio.monthly]\nNetherlands = 1.62\n')
    assert load_pricing_config(toml).market_ratio("monthly", "Netherlands") == 1.62
