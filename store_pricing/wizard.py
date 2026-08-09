"""Guided credential setup (`pricing.py setup`) and live credential verification
(`pricing.py doctor`).

This is the part of the redesign the original request called out specifically: the README
gave a static "where to find each value" table and then went silent - first real feedback
was an opaque API error mid-run. The wizard instead asks for one value at a time with the
exact console click-path, validates its shape locally, and then makes one real API call to
confirm it actually works before moving on.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import stripe
from dotenv import dotenv_values
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from store_pricing import apple as apple_api
from store_pricing import google as google_api
from store_pricing import stripe_platform
from store_pricing.config import (
    ENV_PATH,
    AppleCreds,
    AppleOneTimeCreds,
    GoogleCreds,
    GoogleOneTimeCreds,
    StripeCreds,
    StripeOneTimeCreds,
    load_settings,
)
from store_pricing.ui import confirm, console, checkbox, select, text

GOOGLE_KEYS = [
    "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_PACKAGE_NAME",
    "GOOGLE_SUBSCRIPTION_ID", "GOOGLE_BASEPLAN_ID",
    "GOOGLE_ONETIME_PRODUCT_ID", "GOOGLE_ONETIME_PURCHASE_OPTION_ID",
]
APPLE_KEYS = [
    "APPLE_ISSUER_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY", "APPLE_APP_ID",
    "APPLE_SUBSCRIPTION_PRODUCT_ID", "APPLE_IAP_PRODUCT_ID",
]
STRIPE_KEYS = ["STRIPE_SECRET_KEY", "STRIPE_PRICE_ID", "STRIPE_ONETIME_PRICE_ID"]

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{20,40}$")


def _read_env(path: Path = ENV_PATH) -> dict:
    return {k: v for k, v in dotenv_values(path).items() if v is not None} if path.is_file() else {}


def _escape(value: str) -> str:
    return value.replace("\n", "\\n")


def _write_env(values: dict, path: Path = ENV_PATH) -> None:
    """Write .env at 0600.

    The wizard is now what creates this file, and it holds the Apple .p8 private key
    verbatim - the same secret the key.p8 file itself holds, which nobody would leave
    world-readable. Path.write_text() alone would create it 0644.
    """
    lines = []
    if any(k in values for k in GOOGLE_KEYS):
        lines.append("# Google Play Console Configuration")
        lines += [f"{k}={_escape(values[k])}" for k in GOOGLE_KEYS if k in values]
        lines.append("")
    if any(k in values for k in APPLE_KEYS):
        lines.append("# Apple App Store Connect Configuration")
        lines += [f"{k}={_escape(values[k])}" for k in APPLE_KEYS if k in values]
        lines.append("")
    if any(k in values for k in STRIPE_KEYS):
        lines.append("# Stripe Configuration")
        lines += [f"{k}={_escape(values[k])}" for k in STRIPE_KEYS if k in values]
        lines.append("")
    for k, v in values.items():
        if k not in GOOGLE_KEYS and k not in APPLE_KEYS and k not in STRIPE_KEYS:
            lines.append(f"{k}={_escape(v)}")
    path.write_text("\n".join(lines) + "\n")
    # chmod after writing so an already-existing 0644 .env gets tightened too, not just
    # newly created ones.
    os.chmod(path, 0o600)


def _set(values: dict, key: str, value: str) -> None:
    """Write `key` into `values`, asking before clobbering an existing non-empty value."""
    if values.get(key) and values[key] != value:
        if not confirm(f"{key} is already set to '{values[key][:40]}...' - overwrite?", default=False):
            return
    values[key] = value


def _read_p8(raw: str) -> "str | None":
    """Accept either a path to a .p8 file or pasted key contents."""
    candidate = Path(raw.strip())
    if candidate.is_file():
        return candidate.read_text().strip()
    if "BEGIN PRIVATE KEY" in raw:
        return raw.strip()
    return None


def _setup_google(values: dict) -> None:
    console.print("\n[bold]Google Play Console[/bold]")
    console.print("Play Console → Setup → API access → Create service account → download the JSON, "
                   "then invite that service account under Users & permissions with the "
                   "[bold]Manage store presence[/bold] permission (this is a separate step from "
                   "creating the account, and can take a few minutes to propagate).")

    while True:
        path_str = text("Path to the downloaded service-account JSON:")
        path = Path(path_str.strip()).expanduser()
        if path.is_file():
            break
        console.print(f"[red]No file at {path}[/red]")

    _set(values, "GOOGLE_SERVICE_ACCOUNT_FILE", str(path))
    _set(values, "GOOGLE_PACKAGE_NAME", text("App package name (e.g. com.example.app):"))

    product_types = checkbox(
        "Which product type(s) do you want to price on Google Play?",
        ["Subscription", "One-time purchase"],
    )

    if "Subscription" in product_types:
        _set(values, "GOOGLE_SUBSCRIPTION_ID", text("Subscription product ID (Play Console → Monetize → Subscriptions):"))
        _set(values, "GOOGLE_BASEPLAN_ID", text("Base plan ID (inside that subscription → Base plans):"))

        creds = GoogleCreds(
            service_account_file=values["GOOGLE_SERVICE_ACCOUNT_FILE"],
            package_name=values["GOOGLE_PACKAGE_NAME"],
            subscription_id=values["GOOGLE_SUBSCRIPTION_ID"],
            baseplan_id=values["GOOGLE_BASEPLAN_ID"],
        )
        ok, message = verify_google(creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")

    if "One-time purchase" in product_types:
        console.print("Play Console → Monetize → Products → One-time products - the product ID is at the "
                       "top, the purchase option ID is inside it under Purchase options.")
        _set(values, "GOOGLE_ONETIME_PRODUCT_ID", text("One-time product ID:"))
        _set(values, "GOOGLE_ONETIME_PURCHASE_OPTION_ID", text("Purchase option ID:"))

        onetime_creds = GoogleOneTimeCreds(
            service_account_file=values["GOOGLE_SERVICE_ACCOUNT_FILE"],
            package_name=values["GOOGLE_PACKAGE_NAME"],
            product_id=values["GOOGLE_ONETIME_PRODUCT_ID"],
            purchase_option_id=values["GOOGLE_ONETIME_PURCHASE_OPTION_ID"],
        )
        ok, message = verify_google_onetime(onetime_creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")


def _setup_apple(values: dict) -> None:
    console.print("\n[bold]App Store Connect[/bold]")
    console.print("App Store Connect → Users and Access → Integrations → Keys → "
                   "create a key with the [bold]Finance[/bold] role. The .p8 file downloads "
                   "exactly once, so save it somewhere safe.")

    _set(values, "APPLE_ISSUER_ID", text("Issuer ID (top of the Keys page):",
                                          validate=lambda v: True if _UUID_RE.match(v.strip()) else "Doesn't look like a UUID"))
    _set(values, "APPLE_KEY_ID", text("Key ID (next to the key you just created):"))

    while True:
        raw = text("Path to the .p8 file (or paste its contents):")
        key = _read_p8(raw)
        if key:
            break
        console.print("[red]Couldn't read a private key from that - check the path or paste the full PEM block[/red]")
    _set(values, "APPLE_PRIVATE_KEY", key)

    _set(values, "APPLE_APP_ID", text("Numeric App ID (App Store Connect → your app → General → App Information):",
                                       validate=lambda v: True if v.strip().isdigit() else "Should be numeric, not the bundle ID"))

    product_types = checkbox(
        "Which product type(s) do you want to price on the App Store?",
        ["Subscription", "One-time purchase"],
    )

    if "Subscription" in product_types:
        _set(values, "APPLE_SUBSCRIPTION_PRODUCT_ID", text("Subscription product ID (your app → Subscriptions):"))

        creds = AppleCreds(
            issuer_id=values["APPLE_ISSUER_ID"], key_id=values["APPLE_KEY_ID"],
            private_key=values["APPLE_PRIVATE_KEY"], app_id=values["APPLE_APP_ID"],
            subscription_product_id=values["APPLE_SUBSCRIPTION_PRODUCT_ID"],
        )
        ok, message = verify_apple(creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")

    if "One-time purchase" in product_types:
        _set(values, "APPLE_IAP_PRODUCT_ID", text("In-app purchase product ID (your app → In-App Purchases):"))

        onetime_creds = AppleOneTimeCreds(
            issuer_id=values["APPLE_ISSUER_ID"], key_id=values["APPLE_KEY_ID"],
            private_key=values["APPLE_PRIVATE_KEY"], app_id=values["APPLE_APP_ID"],
            iap_product_id=values["APPLE_IAP_PRODUCT_ID"],
        )
        ok, message = verify_apple_onetime(onetime_creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")


def _setup_stripe(values: dict) -> None:
    console.print("\n[bold]Stripe[/bold]")
    console.print("Dashboard → Developers → API keys for the secret key (a restricted key with "
                   "write access to Prices/Coupons/Promotion codes also works).")

    _set(values, "STRIPE_SECRET_KEY", text("Secret (or restricted) key (sk_... or rk_...):",
                                            validate=lambda v: True if v.strip().startswith(("sk_", "rk_")) else "Should start with sk_ or rk_"))

    product_types = checkbox(
        "Which product type(s) do you want to price on Stripe?",
        ["Subscription", "One-time purchase"],
    )

    if "Subscription" in product_types:
        _set(values, "STRIPE_PRICE_ID", text("Recurring Price ID (Product catalog → your subscription's recurring Price) (price_...):",
                                              validate=lambda v: True if v.strip().startswith("price_") else "Should start with price_"))

        creds = StripeCreds(secret_key=values["STRIPE_SECRET_KEY"], price_id=values["STRIPE_PRICE_ID"])
        ok, message = verify_stripe(creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")

    if "One-time purchase" in product_types:
        _set(values, "STRIPE_ONETIME_PRICE_ID", text("One-time Price ID (Product catalog → your product's one-time Price) (price_...):",
                                                       validate=lambda v: True if v.strip().startswith("price_") else "Should start with price_"))

        onetime_creds = StripeOneTimeCreds(secret_key=values["STRIPE_SECRET_KEY"], price_id=values["STRIPE_ONETIME_PRICE_ID"])
        ok, message = verify_stripe_onetime(onetime_creds)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")


def run_setup(platforms: "set[str] | None" = None) -> None:
    if platforms is None:
        chosen = checkbox("Which store(s) do you want to configure?", ["Apple", "Google", "Stripe"])
        platforms = {c.lower() for c in chosen}

    values = _read_env()

    if "google" in platforms:
        _setup_google(values)
    if "apple" in platforms:
        _setup_apple(values)
    if "stripe" in platforms:
        _setup_stripe(values)

    _write_env(values)
    console.print(f"\n[bold green]Saved to {ENV_PATH}[/bold green] — run: [bold]python pricing.py[/bold]")


def verify_google(creds: GoogleCreds) -> tuple:
    try:
        credentials = service_account.Credentials.from_service_account_file(
            creds.service_account_file, scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        service = build("androidpublisher", "v3", credentials=credentials)
        response = service.monetization().subscriptions().get(
            packageName=creds.package_name, productId=creds.subscription_id,
        ).execute()
        base_plans = {bp.get("basePlanId") for bp in response.get("basePlans", [])}
        if creds.baseplan_id not in base_plans:
            return False, f"Connected, but base plan '{creds.baseplan_id}' not found (have: {', '.join(base_plans) or 'none'})"
        return True, f"Connected - subscription '{creds.subscription_id}' has {len(base_plans)} base plan(s)"
    except HttpError as e:
        if e.resp.status == 403:
            return False, "Permission denied - check the service account has 'Manage store presence' in Play Console → Users and permissions"
        if e.resp.status == 404:
            return False, "Not found - check GOOGLE_PACKAGE_NAME and GOOGLE_SUBSCRIPTION_ID"
        return False, f"API error {e.resp.status}: {e}"
    except Exception as e:
        return False, f"Could not connect: {e}"


def verify_google_onetime(creds: GoogleOneTimeCreds) -> tuple:
    try:
        service = google_api.build_service(creds)
        response = service.monetization().onetimeproducts().get(
            packageName=creds.package_name, productId=creds.product_id,
        ).execute()
        purchase_options = {po.get("purchaseOptionId") for po in response.get("purchaseOptions", [])}
        if creds.purchase_option_id not in purchase_options:
            return False, f"Connected, but purchase option '{creds.purchase_option_id}' not found (have: {', '.join(purchase_options) or 'none'})"
        return True, f"Connected - one-time product '{creds.product_id}' has {len(purchase_options)} purchase option(s)"
    except HttpError as e:
        if e.resp.status == 403:
            return False, "Permission denied - check the service account has 'Manage store presence' in Play Console → Users and permissions"
        if e.resp.status == 404:
            return False, "Not found - check GOOGLE_PACKAGE_NAME and GOOGLE_ONETIME_PRODUCT_ID"
        return False, f"API error {e.resp.status}: {e}"
    except Exception as e:
        return False, f"Could not connect: {e}"


def verify_stripe(creds: StripeCreds) -> tuple:
    price, error = stripe_platform.fetch_price(creds)
    if error:
        return False, error
    recurring = getattr(price, "recurring", None)
    interval = getattr(recurring, "interval", "?") if recurring else "?"
    return True, f"Connected - Price {creds.price_id} is {price['currency'].upper()} recurring every {interval}"


def verify_stripe_onetime(creds: StripeOneTimeCreds) -> tuple:
    price, error = stripe_platform.fetch_onetime_price(creds)
    if error:
        return False, error
    return True, f"Connected - Price {creds.price_id} is a {price['currency'].upper()} one-time Price"


def verify_apple(creds: AppleCreds) -> tuple:
    try:
        token = apple_api.get_jwt(creds)
    except Exception as e:
        return False, f"Could not sign a JWT with this key: {e}"

    subscription_id = apple_api.get_subscription_id(token, creds)
    if subscription_id is None:
        return False, "Key accepted, but couldn't find the subscription - check APPLE_APP_ID, APPLE_SUBSCRIPTION_PRODUCT_ID, and that the key has the Finance role"
    return True, f"Connected - resolved subscription {subscription_id}"


def verify_apple_onetime(creds: AppleOneTimeCreds) -> tuple:
    try:
        token = apple_api.get_jwt(creds)
    except Exception as e:
        return False, f"Could not sign a JWT with this key: {e}"

    iap_id = apple_api.get_iap_id(token, creds)
    if iap_id is None:
        return False, "Key accepted, but couldn't find the in-app purchase - check APPLE_APP_ID, APPLE_IAP_PRODUCT_ID, and that the key has the Finance role"
    return True, f"Connected - resolved in-app purchase {iap_id}"


def run_doctor() -> bool:
    """Live-verify configured credentials. Returns True if every configured platform is healthy."""
    settings = load_settings()
    all_ok = True

    console.print("[bold]Google Play (Subscription)[/bold]")
    if settings.google is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.google_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --google[/bold]")
    else:
        ok, message = verify_google(settings.google)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    console.print("\n[bold]Google Play (One-time purchase)[/bold]")
    if settings.google_onetime is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.google_onetime_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --google[/bold]")
    else:
        ok, message = verify_google_onetime(settings.google_onetime)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    console.print("\n[bold]App Store Connect (Subscription)[/bold]")
    if settings.apple is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.apple_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --apple[/bold]")
    else:
        ok, message = verify_apple(settings.apple)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    console.print("\n[bold]App Store Connect (One-time purchase)[/bold]")
    if settings.apple_onetime is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.apple_onetime_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --apple[/bold]")
    else:
        ok, message = verify_apple_onetime(settings.apple_onetime)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    console.print("\n[bold]Stripe (Subscription)[/bold]")
    if settings.stripe is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.stripe_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --stripe[/bold]")
    else:
        ok, message = verify_stripe(settings.stripe)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    console.print("\n[bold]Stripe (One-time purchase)[/bold]")
    if settings.stripe_onetime is None:
        console.print("[yellow]✗ Not configured[/yellow]")
        for err in settings.stripe_onetime_errors:
            console.print(f"    {err}")
        console.print("  Run: [bold]python pricing.py setup --stripe[/bold]")
    else:
        ok, message = verify_stripe_onetime(settings.stripe_onetime)
        console.print(f"[green]✓ {message}[/green]" if ok else f"[red]✗ {message}[/red]")
        all_ok = all_ok and ok

    return all_ok
