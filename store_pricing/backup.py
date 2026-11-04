"""Snapshots of a Google Play resource taken right before a price patch, and the check that
compares the resource after the patch against that snapshot.

Google Play has no per-region price endpoint: every price update resubmits the whole
subscription (every base plan) or one-time product (every purchase option), and the 400-error
recovery ladder in google.py may clamp or drop regions on containers other than the one being
priced to get a patch through. A snapshot on disk is the only way back from that, so
google.update_prices() refuses to patch without one, and `pricing.py restore-google` puts a
snapshot's prices back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path("backups")


@dataclass(frozen=True)
class ResourceShape:
    """Where a Google Play resource keeps its per-container regional prices."""

    kind: str
    containers_key: str
    container_id_key: str
    regions_key: str


SUBSCRIPTION = ResourceShape("google-subscription", "basePlans", "basePlanId", "regionalConfigs")
ONETIME_PRODUCT = ResourceShape(
    "google-onetime-product", "purchaseOptions", "purchaseOptionId", "regionalPricingAndAvailabilityConfigs"
)
SHAPES = {shape.kind: shape for shape in (SUBSCRIPTION, ONETIME_PRODUCT)}


def write_backup(
    shape: ResourceShape,
    product_id: str,
    resource: dict,
    backup_dir: Path = BACKUP_DIR,
    now: "datetime | None" = None,
) -> Path:
    """Write `resource` exactly as Google returned it. Raises on any I/O error: callers must
    not patch when this fails."""
    now = now or datetime.now(timezone.utc)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{shape.kind}_{product_id}_{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    payload = {"kind": shape.kind, "product_id": product_id, "taken_at": now.isoformat(), "resource": resource}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def load_backup(path: Path) -> dict:
    payload = json.loads(Path(path).read_text())
    if payload.get("kind") not in SHAPES or "resource" not in payload or "product_id" not in payload:
        raise ValueError(f"{path} is not a backup written by pricing.py")
    return payload


def _regional_prices(container: dict, shape: ResourceShape) -> dict:
    prices = {}
    for config in container.get(shape.regions_key, []) or []:
        price = config.get("price") or {}
        prices[config.get("regionCode")] = (
            price.get("currencyCode"),
            int(price.get("units", 0) or 0),
            int(price.get("nanos", 0) or 0),
        )
    return prices


def _containers(resource: dict, shape: ResourceShape) -> dict:
    return {c.get(shape.container_id_key): c for c in resource.get(shape.containers_key, []) or []}


def _format_price(price: tuple) -> str:
    currency, units, nanos = price
    return f"{units + nanos / 1e9:.2f} {currency}"


def compare(before: dict, after: dict, shape: ResourceShape, target_id: "str | None") -> list[str]:
    """Describe every regional price difference between two copies of the resource.

    With a `target_id`, price changes inside that container are expected and skipped; only
    regions that disappeared from it are reported. Every difference anywhere else is reported.
    With no target, every difference is reported.
    """
    issues = []
    before_containers = _containers(before, shape)
    after_containers = _containers(after, shape)

    for container_id in sorted(set(before_containers) - set(after_containers), key=str):
        issues.append(f"{container_id}: removed")
    for container_id in sorted(set(after_containers) - set(before_containers), key=str):
        issues.append(f"{container_id}: added")

    for container_id in sorted(set(before_containers) & set(after_containers), key=str):
        old = _regional_prices(before_containers[container_id], shape)
        new = _regional_prices(after_containers[container_id], shape)
        is_target = container_id == target_id

        for region in sorted(set(old) - set(new), key=str):
            issues.append(f"{container_id}: region {region} removed (was {_format_price(old[region])})")
        if is_target:
            continue
        for region in sorted(set(new) - set(old), key=str):
            issues.append(f"{container_id}: region {region} added ({_format_price(new[region])})")
        for region in sorted(set(old) & set(new), key=str):
            if old[region] != new[region]:
                issues.append(
                    f"{container_id}: region {region} changed {_format_price(old[region])} -> {_format_price(new[region])}"
                )
    return issues
