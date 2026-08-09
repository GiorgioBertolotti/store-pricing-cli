"""Settings: everything the rest of the package needs, loaded once and passed as a
parameter - never read from module globals.

Historically (subscription_price_applier.py) credentials were read via os.getenv() at
*import time* and every helper function read those module-level globals directly. That
meant a single process could only ever target one app/subscription, and a setup wizard
that writes .env couldn't run in the same process as code that reads it. Settings fixes
both: it's built explicitly from .env + pricing.toml and threaded through as an argument.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from dotenv import dotenv_values

ENV_PATH = Path(".env")
TOML_PATH = Path("pricing.toml")

# Built-in VAT/GST rates by country (as of 2024), overridable per-country via
# pricing.toml's [pricing.vat] table. Countries not listed fall back to `default_vat`.
DEFAULT_VAT_RATES: dict[str, float] = {
    # European Union countries (VAT)
    "Austria": 0.20, "Belgium": 0.21, "Bulgaria": 0.20, "Croatia": 0.25,
    "Cyprus": 0.19, "Czech Republic": 0.21, "Denmark": 0.25, "Estonia": 0.20,
    "Finland": 0.24, "France": 0.20, "Germany": 0.19, "Greece": 0.24,
    "Hungary": 0.27, "Ireland": 0.23, "Italy": 0.22, "Latvia": 0.21,
    "Lithuania": 0.21, "Luxembourg": 0.17, "Malta": 0.18, "Netherlands": 0.21,
    "Poland": 0.23, "Portugal": 0.23, "Romania": 0.19, "Slovakia": 0.20,
    "Slovenia": 0.22, "Spain": 0.21, "Sweden": 0.25,
    # Other European countries
    "United Kingdom": 0.20, "Switzerland": 0.077, "Norway": 0.25,
    "Iceland": 0.24, "Liechtenstein": 0.077,
    # North America
    "United States": 0.0, "Canada": 0.05, "Mexico": 0.16,
    # Asia-Pacific
    "Australia": 0.10, "New Zealand": 0.15, "Japan": 0.10, "South Korea": 0.10,
    "Singapore": 0.07, "Hong Kong": 0.0, "Taiwan": 0.05, "Thailand": 0.07,
    "Malaysia": 0.06, "Indonesia": 0.11, "Philippines": 0.12, "Vietnam": 0.10,
    "India": 0.18, "China": 0.13, "Pakistan": 0.17,
    # Middle East & Africa
    "United Arab Emirates": 0.05, "Saudi Arabia": 0.15, "Qatar": 0.0,
    "Kuwait": 0.0, "Bahrain": 0.10, "Oman": 0.05, "Israel": 0.17,
    "Turkey": 0.20, "Egypt": 0.14, "South Africa": 0.15, "Nigeria": 0.075,
    "Kenya": 0.16,
    # South America
    "Brazil": 0.17, "Argentina": 0.21, "Chile": 0.19, "Colombia": 0.19,
    "Peru": 0.18, "Uruguay": 0.22,
    # Other major countries
    "Russia": 0.20, "Ukraine": 0.20, "Belarus": 0.20, "Kazakhstan": 0.12,
    "Uzbekistan": 0.12,
}

TOML_TEMPLATE = '''\
# store-pricing-cli configuration.
# Uncomment/edit any value below to override the built-in default.

[pricing]
# Country the scaling factor is anchored to - it always pays the full USD price.
anchor_country = "United States"
# Scaling factor cap: countries pricier than the anchor pay full price, never a premium.
scaling_cap = 1.0
# VAT/GST rate applied to countries not listed in [pricing.vat] below.
default_vat = 0.15
# "psychological" (round to .99-style endings) or "none" (raw computed price).
rounding = "psychological"

[pricing.vat]
# Per-country overrides of the built-in VAT/GST table, e.g.:
# Germany = 0.19

[apple]
# get_closest_price_point() skips a territory if the nearest available price point
# differs from the target by more than this fraction of the target price...
price_point_tolerance_pct = 0.10
# ...or by more than this many units of local currency, whichever is larger.
price_point_tolerance_units = 2.0
# Concurrent HTTP round-trips for price-point resolution. Kept modest against
# Apple's undocumented rate limits.
max_workers = 8

[data]
# "auto" walks back from the current year until the World Bank has PPP data.
# Set to a specific year (e.g. 2024) to pin it.
ppp_year = "auto"
'''


@dataclass(frozen=True)
class GoogleCreds:
    service_account_file: str
    package_name: str
    subscription_id: str
    baseplan_id: str


@dataclass(frozen=True)
class GoogleOneTimeCreds:
    """Same Play Console app/service-account as GoogleCreds, but a one-time (managed)
    product instead of a subscription: `onetimeproducts` has a `purchaseOptions` array
    playing the same role subscriptions' `basePlans` does, so `purchase_option_id` is the
    direct analogue of GoogleCreds.baseplan_id."""

    service_account_file: str
    package_name: str
    product_id: str
    purchase_option_id: str


@dataclass(frozen=True)
class AppleCreds:
    issuer_id: str
    key_id: str
    private_key: str
    app_id: str
    subscription_product_id: str


@dataclass(frozen=True)
class AppleOneTimeCreds:
    """Same App Store Connect key/app as AppleCreds, but an in-app purchase (one-time
    product) instead of a subscription."""

    issuer_id: str
    key_id: str
    private_key: str
    app_id: str
    iap_product_id: str


@dataclass(frozen=True)
class StripeCreds:
    secret_key: str
    price_id: str


@dataclass(frozen=True)
class StripeOneTimeCreds:
    """Same Stripe account/secret key as StripeCreds, but a one-time (`type=one_time`)
    Price instead of a recurring one - currency_options works identically on both, so
    stripe_platform.py's per-currency logic is shared; only fetch_price()'s
    recurring-vs-one-time validation differs."""

    secret_key: str
    price_id: str


@dataclass(frozen=True)
class PricingConfig:
    anchor_country: str = "United States"
    scaling_cap: float = 1.0
    default_vat: float = 0.15
    rounding: str = "psychological"
    vat_overrides: dict = field(default_factory=dict)
    apple_tolerance_pct: float = 0.10
    apple_tolerance_units: float = 2.0
    apple_max_workers: int = 8
    ppp_year: str = "auto"

    def vat_rate(self, country: str) -> float:
        if country in self.vat_overrides:
            return self.vat_overrides[country]
        return DEFAULT_VAT_RATES.get(country, self.default_vat)


@dataclass
class Settings:
    google: Optional[GoogleCreds]
    apple: Optional[AppleCreds]
    pricing: PricingConfig
    stripe: Optional[StripeCreds] = None
    google_onetime: Optional[GoogleOneTimeCreds] = None
    apple_onetime: Optional[AppleOneTimeCreds] = None
    stripe_onetime: Optional[StripeOneTimeCreds] = None
    google_errors: list[str] = field(default_factory=list)
    apple_errors: list[str] = field(default_factory=list)
    stripe_errors: list[str] = field(default_factory=list)
    google_onetime_errors: list[str] = field(default_factory=list)
    apple_onetime_errors: list[str] = field(default_factory=list)
    stripe_onetime_errors: list[str] = field(default_factory=list)

    @property
    def google_configured(self) -> bool:
        return self.google is not None

    @property
    def apple_configured(self) -> bool:
        return self.apple is not None

    @property
    def stripe_configured(self) -> bool:
        return self.stripe is not None

    @property
    def google_onetime_configured(self) -> bool:
        return self.google_onetime is not None

    @property
    def stripe_onetime_configured(self) -> bool:
        return self.stripe_onetime is not None

    @property
    def apple_onetime_configured(self) -> bool:
        return self.apple_onetime is not None


def _clean(value: "str | None") -> "str | None":
    value = (value or "").strip()
    return value or None


def _is_placeholder(value: str) -> bool:
    """True for the literal placeholder values .env.example ships.

    The pre-package `_validate_env()` rejected these explicitly (per variable); without the
    check, copying .env.example reads as fully configured and the first real feedback is an
    opaque API error mid-run. Only the unambiguous `your_*` markers are matched - a value
    that could plausibly be someone's real setting (e.g. a base plan genuinely named
    "base-plan") is left alone.
    """
    return value.strip().lower().startswith("your_")


def _collect(env: dict, fields: dict) -> "tuple[dict, list[str]]":
    """Read every field, reporting *all* problems rather than stopping at the first."""
    values, errors = {}, []
    for attr, (var, hint) in fields.items():
        value = _clean(env.get(var))
        if value is None:
            errors.append(f"{var} is not set ({hint})")
        elif _is_placeholder(value):
            errors.append(f"{var} is still the placeholder value '{value}' ({hint})")
            value = None
        values[attr] = value
    return values, errors


def parse_google_creds(env: dict) -> tuple[Optional[GoogleCreds], list[str]]:
    fields = {
        "service_account_file": ("GOOGLE_SERVICE_ACCOUNT_FILE", "path to the Play Console service-account JSON"),
        "package_name": ("GOOGLE_PACKAGE_NAME", "app package name"),
        "subscription_id": ("GOOGLE_SUBSCRIPTION_ID", "subscription product ID"),
        "baseplan_id": ("GOOGLE_BASEPLAN_ID", "base plan ID"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    account_file = Path(values["service_account_file"])
    if not account_file.is_file():
        errors.append(f"GOOGLE_SERVICE_ACCOUNT_FILE points to '{account_file}', which doesn't exist")
        return None, errors

    return GoogleCreds(**values), []


def parse_google_onetime_creds(env: dict) -> tuple[Optional[GoogleOneTimeCreds], list[str]]:
    fields = {
        "service_account_file": ("GOOGLE_SERVICE_ACCOUNT_FILE", "path to the Play Console service-account JSON"),
        "package_name": ("GOOGLE_PACKAGE_NAME", "app package name"),
        "product_id": ("GOOGLE_ONETIME_PRODUCT_ID", "one-time product ID"),
        "purchase_option_id": ("GOOGLE_ONETIME_PURCHASE_OPTION_ID", "purchase option ID within that product"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    account_file = Path(values["service_account_file"])
    if not account_file.is_file():
        errors.append(f"GOOGLE_SERVICE_ACCOUNT_FILE points to '{account_file}', which doesn't exist")
        return None, errors

    return GoogleOneTimeCreds(**values), []


def parse_apple_creds(env: dict) -> tuple[Optional[AppleCreds], list[str]]:
    fields = {
        "issuer_id": ("APPLE_ISSUER_ID", "App Store Connect → Users & Access → Keys → Issuer ID"),
        "key_id": ("APPLE_KEY_ID", "the API key's Key ID"),
        "private_key": ("APPLE_PRIVATE_KEY", "the .p8 private key contents"),
        "app_id": ("APPLE_APP_ID", "numeric App ID"),
        "subscription_product_id": ("APPLE_SUBSCRIPTION_PRODUCT_ID", "subscription product ID"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    if values["app_id"] and not values["app_id"].isdigit():
        errors.append("APPLE_APP_ID should be the numeric App ID, not the bundle ID or subscription group ID")

    key_text = values["private_key"].replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" not in key_text:
        errors.append("APPLE_PRIVATE_KEY doesn't look like a PEM private key (missing 'BEGIN PRIVATE KEY')")

    if errors:
        return None, errors

    return AppleCreds(**values), []


def parse_apple_onetime_creds(env: dict) -> tuple[Optional[AppleOneTimeCreds], list[str]]:
    fields = {
        "issuer_id": ("APPLE_ISSUER_ID", "App Store Connect → Users & Access → Keys → Issuer ID"),
        "key_id": ("APPLE_KEY_ID", "the API key's Key ID"),
        "private_key": ("APPLE_PRIVATE_KEY", "the .p8 private key contents"),
        "app_id": ("APPLE_APP_ID", "numeric App ID"),
        "iap_product_id": ("APPLE_IAP_PRODUCT_ID", "in-app purchase product ID"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    if values["app_id"] and not values["app_id"].isdigit():
        errors.append("APPLE_APP_ID should be the numeric App ID, not the bundle ID or subscription group ID")

    key_text = values["private_key"].replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" not in key_text:
        errors.append("APPLE_PRIVATE_KEY doesn't look like a PEM private key (missing 'BEGIN PRIVATE KEY')")

    if errors:
        return None, errors

    return AppleOneTimeCreds(**values), []


def parse_stripe_creds(env: dict) -> tuple[Optional[StripeCreds], list[str]]:
    fields = {
        "secret_key": ("STRIPE_SECRET_KEY", "Dashboard -> Developers -> API keys"),
        "price_id": ("STRIPE_PRICE_ID", "recurring Price ID for this subscription (Dashboard -> Product catalog)"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    if not values["secret_key"].startswith(("sk_", "rk_")):
        errors.append("STRIPE_SECRET_KEY doesn't look like a Stripe secret/restricted key (should start with 'sk_' or 'rk_')")
    if not values["price_id"].startswith("price_"):
        errors.append("STRIPE_PRICE_ID doesn't look like a Stripe Price ID (should start with 'price_')")

    if errors:
        return None, errors

    return StripeCreds(**values), []


def parse_stripe_onetime_creds(env: dict) -> tuple[Optional[StripeOneTimeCreds], list[str]]:
    fields = {
        "secret_key": ("STRIPE_SECRET_KEY", "Dashboard -> Developers -> API keys"),
        "price_id": ("STRIPE_ONETIME_PRICE_ID", "one-time Price ID for this product (Dashboard -> Product catalog)"),
    }
    values, errors = _collect(env, fields)

    if errors:
        return None, errors

    if not values["secret_key"].startswith(("sk_", "rk_")):
        errors.append("STRIPE_SECRET_KEY doesn't look like a Stripe secret/restricted key (should start with 'sk_' or 'rk_')")
    if not values["price_id"].startswith("price_"):
        errors.append("STRIPE_ONETIME_PRICE_ID doesn't look like a Stripe Price ID (should start with 'price_')")

    if errors:
        return None, errors

    return StripeOneTimeCreds(**values), []


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


ROUNDING_MODES = ("psychological", "none")


class ConfigError(ValueError):
    """A value in pricing.toml is unusable. Raised at load time with the offending key
    named, rather than surfacing later as a crash mid-run or - worse - as a silently
    wrong price (an unrecognised `rounding` used to fall through to psychological
    rounding, so a typo looked like it worked)."""


def _check_number(raw, key: str, where: str, *, minimum: float, cast=float):
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"pricing.toml: [{where}] {key} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"pricing.toml: [{where}] {key} must be >= {minimum}, got {value}")
    return value


def _number(section: dict, key: str, default, where: str, *, minimum: float, cast=float):
    return _check_number(section.get(key, default), key, where, minimum=minimum, cast=cast)


def load_pricing_config(toml_path: Path = TOML_PATH) -> PricingConfig:
    data = _load_toml(toml_path)
    pricing = data.get("pricing", {})
    apple = data.get("apple", {})
    dataconf = data.get("data", {})

    rounding = str(pricing.get("rounding", "psychological")).strip().lower()
    if rounding not in ROUNDING_MODES:
        raise ConfigError(
            f"pricing.toml: [pricing] rounding must be one of {', '.join(ROUNDING_MODES)}, got {rounding!r}"
        )

    vat_overrides = {
        country: _check_number(rate, country, "pricing.vat", minimum=0.0)
        for country, rate in pricing.get("vat", {}).items()
    }

    return PricingConfig(
        anchor_country=pricing.get("anchor_country", "United States"),
        # A cap below 1.0 discounts everyone including the anchor; above 1.0 lets pricier
        # countries pay a premium. Both are legitimate, so only non-positive is rejected.
        scaling_cap=_number(pricing, "scaling_cap", 1.0, "pricing", minimum=1e-9),
        default_vat=_number(pricing, "default_vat", 0.15, "pricing", minimum=0.0),
        rounding=rounding,
        vat_overrides=vat_overrides,
        apple_tolerance_pct=_number(apple, "price_point_tolerance_pct", 0.10, "apple", minimum=0.0),
        apple_tolerance_units=_number(apple, "price_point_tolerance_units", 2.0, "apple", minimum=0.0),
        # 0 would raise inside ThreadPoolExecutor rather than here, mid-run.
        apple_max_workers=_number(apple, "max_workers", 8, "apple", minimum=1, cast=int),
        ppp_year=str(dataconf.get("ppp_year", "auto")),
    )


def ensure_toml_exists(toml_path: Path = TOML_PATH) -> bool:
    """Write pricing.toml with commented defaults if it doesn't exist yet. Returns True if created."""
    if toml_path.is_file():
        return False
    toml_path.write_text(TOML_TEMPLATE)
    return True


def load_settings(env_path: Path = ENV_PATH, toml_path: Path = TOML_PATH) -> Settings:
    """Load credentials and pricing config.

    Missing/invalid *credentials* never raise - they show up as `google`/`apple` being None
    plus the errors list, so callers decide what to do (guided flow -> wizard, doctor ->
    per-var report, apply --apple -> hard fail). A malformed *pricing.toml* does raise
    (ConfigError): unlike a missing credential there's no sensible degraded mode, and
    guessing would mean pushing a wrong price.

    `.env` wins for every variable it defines, but anything it leaves out falls through to
    the process environment: CI injects these as secrets rather than committing a .env
    file, and `pricing.py apply --yes` is documented as the unattended entry point. (The
    pre-package scripts used load_dotenv() + os.getenv(), which read both.)
    """
    file_env = dotenv_values(env_path) if env_path.is_file() else {}
    env = {**os.environ, **{k: v for k, v in file_env.items() if v is not None}}

    google, google_errors = parse_google_creds(env)
    apple, apple_errors = parse_apple_creds(env)
    stripe, stripe_errors = parse_stripe_creds(env)
    google_onetime, google_onetime_errors = parse_google_onetime_creds(env)
    apple_onetime, apple_onetime_errors = parse_apple_onetime_creds(env)
    stripe_onetime, stripe_onetime_errors = parse_stripe_onetime_creds(env)
    pricing = load_pricing_config(toml_path)

    return Settings(
        google=google,
        apple=apple,
        pricing=pricing,
        stripe=stripe,
        google_onetime=google_onetime,
        apple_onetime=apple_onetime,
        stripe_onetime=stripe_onetime,
        google_errors=google_errors,
        apple_errors=apple_errors,
        stripe_errors=stripe_errors,
        google_onetime_errors=google_onetime_errors,
        apple_onetime_errors=apple_onetime_errors,
        stripe_onetime_errors=stripe_onetime_errors,
    )
