"""Per-location report cache (GCS-backed, 14-day TTL).

When a user submits a search precise to a daycare location (i.e. they
picked a Photon suggestion that gave us lat/lng), we look the location up
here. A hit means we already ran a diligence on this place recently and
the artifact is still valid — we serve the existing report immediately
instead of spinning up a new ClawGrid task.

Why a separate file:
  - The cache contract is intentionally tiny (one read, one write) so
    daycarecheck's hot path keeps a single import surface.
  - Bucket lifecycle (14-day auto-delete) handles eviction so we don't
    need a sweep job; consumers only verify the per-row freshness header
    out of caution.

Cache key:  sha256(lower(name) | round(lat,4) | round(lng,4)) — first
16 hex chars. Round(lat,4) ≈ 11 m precision; comfortably tighter than
Photon's typical address-level accuracy. Same key always derives the
same object name, so cache writes from any pod stomp into the same row.

Cache value (JSON):
  task_id              str   — ClawGrid task UUID, used to fetch the
                                artifact + render report.html on hit.
  completed_at         str   — ISO 8601, drives the "compiled X days ago"
                                UI badge.
  daycare_name         str   — for audit + log readability only.
  lat / lng            float — verbatim, in case the cache key collides.

Writes happen exactly once per task: when ClawGrid fires task.completed
into the webhook handler, we write the row. Nothing else writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


CACHE_BUCKET   = os.environ.get("DAYCARE_CACHE_BUCKET", "daycare-cache")
CACHE_TTL_DAYS = int(os.environ.get("DAYCARE_CACHE_TTL_DAYS", "14"))


def cache_key(*, name: str, lat: float, lng: float) -> str:
    """Deterministic 16-char hex key for a (name, lat, lng) triple.

    Rounding lat/lng to 4 decimals gives ~11 m tolerance — far tighter
    than the Photon address resolution ever gives us, but loose enough
    that minor coord drift between repeated Photon calls hashes the same.
    Names are lowercased + whitespace-collapsed so "KinderCare" and
    "kindercare " hash the same.
    """
    norm_name = " ".join(name.lower().split())
    payload = f"{norm_name}|{round(lat, 4)}|{round(lng, 4)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _client():
    """Lazy-import google-cloud-storage so unit-test imports don't
    require the package on the host."""
    from google.cloud import storage
    return storage.Client()


def get_cached(*, name: str, lat: float, lng: float) -> dict | None:
    """Look up the cache row for a place. Returns None on miss / stale.

    A row is "stale" when its ``completed_at`` is older than
    DAYCARE_CACHE_TTL_DAYS. Bucket lifecycle deletes old objects on its
    own schedule (typically within a day of expiry), but we re-check the
    timestamp on read so a row in its grace window never gets served.
    """
    key = cache_key(name=name, lat=lat, lng=lng)
    try:
        client = _client()
        bucket = client.bucket(CACHE_BUCKET)
        blob = bucket.blob(f"{key}.json")
        if not blob.exists():
            return None
        raw = blob.download_as_text()
        row = json.loads(raw)
    except Exception:
        log.warning("daycare_cache_get_failed key=%s", key, exc_info=True)
        return None

    completed_at = row.get("completed_at")
    if not completed_at:
        return None
    try:
        completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    age = datetime.now(timezone.utc) - completed_dt
    if age > timedelta(days=CACHE_TTL_DAYS):
        log.info("daycare_cache_stale key=%s age_days=%s", key, age.days)
        return None
    row["_cache_key"] = key
    row["_cache_age_seconds"] = int(age.total_seconds())
    return row


def put_cached(
    *,
    name: str,
    lat: float,
    lng: float,
    task_id: str,
    completed_at_iso: str,
) -> None:
    """Write a cache row for a freshly-completed task. Idempotent —
    repeated writes for the same key replace the previous row's
    contents (last-write-wins is fine here)."""
    key = cache_key(name=name, lat=lat, lng=lng)
    payload = {
        "task_id": task_id,
        "completed_at": completed_at_iso,
        "daycare_name": name,
        "lat": lat,
        "lng": lng,
    }
    try:
        client = _client()
        bucket = client.bucket(CACHE_BUCKET)
        blob = bucket.blob(f"{key}.json")
        blob.upload_from_string(
            json.dumps(payload),
            content_type="application/json",
        )
        log.info("daycare_cache_put key=%s task_id=%s", key, task_id)
    except Exception:
        log.warning(
            "daycare_cache_put_failed key=%s task_id=%s",
            key, task_id, exc_info=True,
        )
