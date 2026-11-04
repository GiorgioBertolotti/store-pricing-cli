# store-pricing-cli

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![App Store Connect API](https://img.shields.io/badge/App%20Store%20Connect-API-000000?logo=apple&logoColor=white)](https://developer.apple.com/documentation/appstoreconnectapi)
[![Google Play Developer API](https://img.shields.io/badge/Google%20Play-Developer%20API-3DDC84?logo=googleplay&logoColor=white)](https://developers.google.com/android-publisher)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#license)

**Purchasing-power-parity (PPP) subscription pricing automation** for App Store Connect and Google Play Console. Scale a single USD price into 150+ localized, tax-inclusive, psychologically-rounded prices — anchored to real cost-of-living data, not flat exchange rates — and push them live to both stores via their official APIs. One command, no spreadsheet juggling, no manual price entry per territory.

## Table of contents

- [Table of contents](#table-of-contents)
- [Why this exists](#why-this-exists)
- [Features](#features)
- [Quick start](#quick-start)
- [Commands](#commands)
  - [`pricing.py` — the guided flow](#pricingpy--the-guided-flow)
  - [`setup` / `doctor` — credentials](#setup--doctor--credentials)
  - [`scale` — compute prices only](#scale--compute-prices-only)
  - [`apply` — push prices live](#apply--push-prices-live)
  - [`offer` — promotional/discount offers](#offer--promotionaldiscount-offers)
  - [`refresh-data` — update the cost-of-living cache](#refresh-data--update-the-cost-of-living-cache)
- [Configuration (`pricing.toml`)](#configuration-pricingtoml)
- [Country code mapping](#country-code-mapping)
- [Pricing algorithm](#pricing-algorithm)
- [FAQ](#faq)
- [How this compares to store-native regional pricing](#how-this-compares-to-store-native-regional-pricing)
- [Requirements](#requirements)
- [License](#license)

## Why this exists

Flat USD prices are a poor fit for a global audience. A $19.99/year subscription is affordable in the US but prohibitively expensive in India, Brazil, or Southeast Asia — and a straight exchange-rate conversion still overprices it relative to local income almost everywhere outside North America and Western Europe.

Most regional-pricing tools stop at producing a spreadsheet you then re-key by hand into App Store Connect and Google Play Console. This pipeline goes further and pushes the result live, from a single guided command:

- **Anchors prices to real purchasing power** using World Bank PPP conversion factors (not arbitrary regional discounts)
- **Applies live exchange rates** so prices stay accurate as currencies shift
- **Accounts for local VAT/GST** — the price the user sees already includes tax
- **Rounds to psychological price points** (`.99` endings, platform-native tiers)
- **Shows a diff against what's currently live** before anything is pushed, and asks for confirmation
- **Pushes directly to both stores** via their official APIs — App Store Connect and Google Play Console
- **Automates promotional/discount offers** on both platforms from the same pricing data

The result is a regionally fair price ladder that maximizes conversions across markets without manual effort on every price change.

## Features

| Capability                                     | App Store Connect          | Google Play Console        |
| ---------------------------------------------- | -------------------------- | -------------------------- |
| PPP-adjusted base pricing                      | ✅                         | ✅                         |
| Live exchange-rate updates                     | ✅                         | ✅                         |
| VAT/GST-inclusive pricing                      | ✅                         | ✅                         |
| Psychological price rounding (`.99`)           | ✅                         | ✅                         |
| Direct API price push                          | ✅ (App Store Connect API) | ✅ (Android Publisher API) |
| Live-price diff before applying                | ✅                         | ✅                         |
| `--dry-run` (resolve everything, push nothing) | ✅                         | ✅                         |
| Regional promotional/discount offers           | ✅                         | ✅                         |
| Automatic retry on recoverable API errors      | ✅                         | ✅                         |
| Failure report per run                         | ✅                         | ✅                         |
| Guided credential setup with live verification | ✅                         | ✅                         |

## Quick start

```bash
git clone https://github.com/GiorgioBertolotti/store-pricing-cli.git
cd store-pricing-cli

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

python pricing.py
```

That's it — one command. On a clean checkout it will:

1. Fetch the cost-of-living cache in the background (no action needed)
2. Walk you through a guided credential setup if `.env` isn't configured yet, verifying each key against the real API as you enter it
3. Ask for the USD price you want to scale
4. Show you the resulting price table and summary stats
5. Ask which store(s) to push to, show a diff against what's currently live, and ask for confirmation before changing anything

## Commands

```
pricing.py                # guided flow (see above)
pricing.py setup          # credential wizard
pricing.py doctor         # live-verify configured credentials
pricing.py scale          # compute prices -> price_scaled.xlsx only
pricing.py apply          # push price_scaled.xlsx live
pricing.py offer          # create a promotional/discount offer
pricing.py refresh-data   # refresh the cost-of-living cache and country codes
pricing.py backup-google  # snapshot the Google Play products (read-only)
pricing.py restore-google --backup backups/<file>.json  # put a snapshot's prices back
```

Every subcommand accepts flags for unattended/CI use and falls back to a prompt for anything you omit — `pricing.py apply --yes` skips confirmation, `pricing.py scale --price 9.99` skips the prompt.

### `pricing.py` — the guided flow

Detects what's missing and only asks what it can't infer: refreshes the cost-of-living cache silently if it's stale, runs the setup wizard if no store is configured, prompts for the base USD price, previews the computed prices, then — if you choose to push — fetches what's currently live on each store, shows only what would change, and requires an explicit confirmation before writing anything.

### `setup` / `doctor` — credentials

```bash
python pricing.py setup            # configure both stores, or pick which
python pricing.py setup --apple
python pricing.py setup --google
python pricing.py doctor           # live-check whatever's already configured
```

The wizard asks for one credential at a time with the exact console click-path, validates its shape locally, then makes a real API call to confirm it works before moving on — so a bad key fails immediately with an actionable message instead of 200 lines into a price push. `doctor` re-runs that same live check at any time without touching `.env`.

**Google Play** needs a service account with the **"Manage store presence"** permission (Play Console → Setup → API access → Create service account, then invite it under Users & permissions — this invite is a separate step from creating the account, and can take a few minutes to propagate).

**App Store Connect** needs an API key with the **"Finance"** role (App Store Connect → Users and Access → Integrations → Keys). The `.p8` file downloads exactly once — the wizard accepts either a path to it or pasted contents.

The wizard writes `.env` with `0600` permissions, since it holds the Apple private key verbatim. **On CI**, skip the file entirely: `.env` wins for any variable it defines, but everything it omits falls through to the process environment, so the same variable names work as injected secrets. See [`.env.example`](.env.example) for the full list.

### `scale` — compute prices only

```bash
python pricing.py scale --price 19.99
```

Computes the full price ladder and writes `price_scaled.xlsx`. Never touches a store — this is also what the guided flow runs before asking whether to push.

### `apply` — push prices live

```bash
python pricing.py apply                 # both configured stores, with confirmation
python pricing.py apply --apple         # Apple only
python pricing.py apply --dry-run       # resolve everything, push nothing
python pricing.py apply --yes           # skip confirmation (CI)
```

Reads `price_scaled.xlsx` and `country_codes.json`, fetches what's currently live on each targeted store, and shows a diff before pushing anything:

- **Google Play**: fetches the subscription object, saves a copy of it to `backups/`, patches all regional configs in a single API call, auto-retries on recoverable 400 errors (stale currency, non-billable region, price out of range), then re-reads it and flags any price that changed outside the base plan being priced. `pricing.py restore-google --backup <file>` puts a saved copy back (`--dry-run` lists what would change first)
- **App Store Connect**: generates a JWT, resolves each territory to the closest available price point in parallel, and submits the price update

`--dry-run` runs the full resolution path — auth, price-point matching, payload construction — and reports exactly what would happen without submitting anything, so it's safe to try against a live account. Any country that could not be updated (or would fail under `--dry-run`) is logged to a timestamped `price_update_failures_YYYYMMDD_HHMMSS.txt` report.

### `offer` — promotional/discount offers

```bash
python pricing.py offer --discount 30 --cycles 3 --period P1M \
  --code SUMMER30 --google-offer-id summer-30-off
```

Creates a discounted, time-limited offer on top of whatever price is currently live, reusing `price_scaled.xlsx`:

- **Google Play**: a `relativeDiscount` phase (percentage off the live price in each region) — no price computation needed
- **App Store Connect**: computes the discounted price per territory from the scaled price data, resolves it to the closest available price point, and creates a `subscriptionPromotionalOffers` resource

The two stores have incompatible id rules — Apple offer codes are conventionally uppercase, Google Play offer ids accept only lowercase letters, numbers and hyphens — so `--google-offer-id` sets Google's independently. Omit it and `--code` is used for both, which only works if the code is already Google-shaped; anything else is rejected up front rather than failing partway through.

Apple's promotional offer API only supports recreating an offer, not editing one in place: re-running with the same offer code deletes the existing offer and creates a new one, with a brief window where it doesn't exist — `pricing.py offer` warns and asks for confirmation before doing this. Failures are logged to the same `price_update_failures_*.txt` format as `apply`.

### `refresh-data` — update the cost-of-living cache

```bash
python pricing.py refresh-data
```

Pulls **World Bank PPP conversion factors** (indicator `PA.NUS.PRVT.PP`, walking back through prior years for any country missing the latest one) and resolves **currency codes** offline via `pycountry` + Babel's CLDR territory data, then regenerates `country_codes.json` (World Bank name → ISO alpha-2/alpha-3). No scraping, no browser automation, no API key. The guided flow and `scale` run this automatically whenever the cache is missing or older than 30 days — you only need it directly to force an early refresh.

## Configuration (`pricing.toml`)

Generated with commented defaults on first run. Covers the anchor country, the scaling-factor cap, VAT overrides, Apple's price-point tolerance, and worker concurrency — see [`pricing.toml.example`](./pricing.toml.example) for the full set with inline explanations of each value.

## Country code mapping

`country_codes.json` maps World Bank country names to ISO alpha-2 (Google Play) and alpha-3 (App Store Connect) codes, resolved offline via `pycountry`. The file is committed and covers every country in the World Bank PPP dataset; `pricing.py refresh-data` regenerates it (merge-only — existing entries are never dropped, only added to or updated).

## Pricing algorithm

```
scaling_factor = min(us_meal_usd / country_meal_usd, 1.0)
scaled_native  = usd_price × scaling_factor × exchange_rate × (1 + vat_rate)
final_price    = smart_round(scaled_native)   # → .99 endings
```

- Countries with a higher cost of living than the anchor country (default: United States) receive the full USD price (factor capped at 1.0, configurable in `pricing.toml`)
- `min_scaling_factor` sets a floor (e.g. `0.25` = never below 25% of the anchor price), which also guards against bad PPP data points
- VAT/GST rates are embedded for ~50 countries; unlisted countries default to 15% (both overridable)
- Smart rounding snaps prices to platform-native tiers (e.g. `$9.99`, `$4.99`)

**Market ratios (optional)**: PPP is a poor fit where people pay close to US prices for subscriptions despite lower purchasing power (Southern Europe, Japan). List per-plan ratios in `pricing.toml` (`[pricing.market_ratio.annual]`, `Italy = 1.05`) and run `pricing.py scale --plan annual`: listed countries are priced at that ratio of the anchor's tax-inclusive price, everyone else stays on PPP. `market_ratio_cap = 1.25` under `[pricing]` keeps any market-priced country at or below 125% of the anchor's price.

**Per-plan rounding (optional)**: on a cheap plan a whole-unit `.99` step is a large share of the price. `[pricing.plan_rounding]` with `monthly = "psychological-half"` lets that plan end in `.49` as well as `.99`.

**Apple price matching**: the App Store enforces a fixed set of price points per territory. The script finds the closest available point and skips the update if the difference exceeds a configurable tolerance (default 10% or 2 units of local currency). Skipped territories appear in the failure report.

**Google Play**: the entire subscription object is patched in one call. The script retries automatically on recoverable 400 errors: stale currency codes, non-billable regions, and prices outside the allowed range.

## FAQ

**What is purchasing power parity (PPP) pricing for apps?**
PPP pricing sets a subscription's local price based on what an equivalent basket of goods costs in that country, rather than a flat currency conversion. It's why a $9.99 subscription might localize to $3.99-equivalent in a lower-income market instead of a mechanical exchange-rate conversion of $9.99 — the local price reflects local purchasing power, not just the currency rate.

**How do I set different subscription prices per country on the App Store or Google Play?**
Both platforms let you set prices manually per territory in their respective consoles, but doing that for 150+ countries by hand — and keeping it in sync as exchange rates move — doesn't scale. `python pricing.py` computes the per-country price and pushes it via the App Store Connect API and Android Publisher API in one guided run.

**Does Google Play support PPP-based regional pricing automatically?**
Google Play's own "price templates" convert by exchange rate, not purchasing power, and Apple's App Store Connect has no built-in PPP concept at all. Neither platform anchors to cost-of-living data out of the box — this pipeline computes that anchor from World Bank data and applies it to both stores identically.

**Can I automate App Store Connect price updates via API instead of the dashboard?**
Yes — `pricing.py apply` authenticates with a JWT signed by an App Store Connect API key, resolves each territory to the closest available price point, and submits the update programmatically. No manual per-territory entry in the dashboard.

**How do I create a regional promotional/discount offer on Google Play and App Store Connect?**
Run `python pricing.py offer` after the base prices are live. It creates a Google Play `relativeDiscount` offer and an App Store Connect `subscriptionPromotionalOffers` resource from the same underlying pricing data, so the discount is consistent across both stores.

**Why not just use App Store Connect's "equalize prices" or Google Play's currency conversion tool?**
See [How this compares to store-native regional pricing](#how-this-compares-to-store-native-regional-pricing) below.

## How this compares to store-native regional pricing

Apple and Google both offer semi-automatic regional price tools — App Store Connect's price equalization and Google Play's currency-based price templates. Both convert by **exchange rate only**: a $9.99 subscription becomes exchange-rate-equivalent in Vietnam and in Switzerland, even though average purchasing power between those markets differs by an order of magnitude. Neither platform factors in local VAT/GST inclusivity, cost-of-living, or psychological price rounding, and neither exposes a way to push a **PPP-adjusted** price ladder programmatically.

This pipeline anchors every price to World Bank PPP conversion factors instead of raw FX rates, so the local price reflects what people can actually afford to pay — then handles tax inclusivity, `.99` rounding, and the API push to both stores in one guided run.

## Requirements

- Python 3.9+
- A Google Play Console service account with Manage store presence permission
- An App Store Connect API key with Finance role

## License

MIT
