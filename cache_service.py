"""Per-location report cache (GCS-backed, 14-day TTL) + slug-to-task index.

Two related caches in one bucket, distinguished by object key prefix:

  {key}.json          — primary (name + lat/lng → task_id) cache row
  by-slug/{slug}.json — secondary index from URL slug → primary key

Why a slug index: SEO-friendly URLs like
``/r/cadence-academy-preschool-portland-or`` need a fast lookup. The
slug isn't part of the primary cache key (which is name + coords), so
we maintain a separate small JSON pointer per slug. Same 14-day TTL.

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
  slug                 str   — SEO-friendly URL slug, also the secondary
                                index key (``by-slug/{slug}.json``).
  city / state         str   — last-known city/state strings, used for
                                meta tags on the slug-route page.

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


def build_slug(*, name: str, city: str | None, state: str | None) -> str:
    """SEO-friendly slug from daycare name + city + state.

    Output looks like ``cadence-academy-preschool-portland-or``. Lowercase,
    alphanumerics + hyphens only, hyphen-separated, with a 4-char hash
    suffix appended when there's a chance of collision (multiple identical
    name/city pairs across the country — surprisingly common with chain
    locations, e.g. "KinderCare" / "Bright Horizons").

    The hash is derived from the original name+city+state (pre-slugify)
    so the same physical place always gets the same suffix.
    """
    def _slugify(s: str) -> str:
        # ASCII-only, alphanumerics → hyphens. Decompose accented Latin
        # chars (NFKD) and DROP the combining marks before scanning, so
        # "crème" becomes "creme", not "cre-me" (the combining grave was
        # being treated as a separator otherwise).
        import unicodedata
        decomposed = unicodedata.normalize("NFKD", s or "")
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        out: list[str] = []
        prev_hyphen = True
        for ch in stripped.lower():
            if ch.isascii() and ch.isalnum():
                out.append(ch)
                prev_hyphen = False
            elif not prev_hyphen:
                out.append("-")
                prev_hyphen = True
        return "".join(out).strip("-")

    parts = [_slugify(name)]
    if city:
        parts.append(_slugify(city))
    if state:
        parts.append(_slugify(state))
    base = "-".join(p for p in parts if p) or "report"
    base = base[:80]  # keep URL reasonably short

    # 4-char hash of the full input — disambiguates same-name/same-city
    # entries (e.g. two KinderCares in the same Portland zip). Same input
    # → same suffix, so a slug is stable across writes.
    raw = f"{(name or '').lower()}|{(city or '').lower()}|{(state or '').lower()}"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:4]
    return f"{base}-{suffix}"


def put_cached(
    *,
    name: str,
    lat: float,
    lng: float,
    task_id: str,
    completed_at_iso: str,
    city: str | None = None,
    state: str | None = None,
    slug: str | None = None,
) -> str:
    """Write a cache row for a freshly-completed task. Idempotent —
    repeated writes for the same key replace the previous row's
    contents (last-write-wins is fine here).

    Returns the slug (computed if not passed).
    """
    key = cache_key(name=name, lat=lat, lng=lng)
    final_slug = slug or build_slug(name=name, city=city, state=state)
    payload = {
        "task_id": task_id,
        "completed_at": completed_at_iso,
        "daycare_name": name,
        "lat": lat,
        "lng": lng,
        "slug": final_slug,
        "city": city,
        "state": state,
    }
    try:
        client = _client()
        bucket = client.bucket(CACHE_BUCKET)

        # Primary row keyed by name+coords hash.
        blob = bucket.blob(f"{key}.json")
        blob.upload_from_string(
            json.dumps(payload),
            content_type="application/json",
        )

        # Secondary slug index — small pointer doc that maps the URL
        # slug back to the primary cache key. Lets the public /r/{slug}
        # route resolve in one read.
        slug_blob = bucket.blob(f"by-slug/{final_slug}.json")
        slug_blob.upload_from_string(
            json.dumps({"key": key, "task_id": task_id}),
            content_type="application/json",
        )

        log.info(
            "daycare_cache_put key=%s slug=%s task_id=%s",
            key, final_slug, task_id,
        )
    except Exception:
        log.warning(
            "daycare_cache_put_failed key=%s slug=%s task_id=%s",
            key, final_slug, task_id, exc_info=True,
        )
    return final_slug


def get_by_slug(slug: str) -> dict | None:
    """Resolve an SEO slug to the cached primary row.

    Two reads (slug index → primary key → primary row) but each is a
    sub-100-byte JSON object — well under any latency concern. Returns
    the same shape as ``get_cached`` (with _cache_age_seconds), or None
    on miss / stale.
    """
    if not slug or "/" in slug or ".." in slug:
        return None
    try:
        client = _client()
        bucket = client.bucket(CACHE_BUCKET)
        idx_blob = bucket.blob(f"by-slug/{slug}.json")
        if not idx_blob.exists():
            return None
        idx_raw = idx_blob.download_as_text()
        idx = json.loads(idx_raw)
        primary_key = idx.get("key")
        if not primary_key:
            return None

        primary_blob = bucket.blob(f"{primary_key}.json")
        if not primary_blob.exists():
            return None
        row = json.loads(primary_blob.download_as_text())
    except Exception:
        log.warning("daycare_cache_get_by_slug_failed slug=%s", slug, exc_info=True)
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
        return None
    row["_cache_age_seconds"] = int(age.total_seconds())
    return row


def list_all_slugs() -> list[dict]:
    """List every fresh slug for sitemap.xml generation.

    Returns a list of {slug, completed_at, daycare_name} dicts, freshest
    first. Stale (>14 day) rows are filtered out; lifecycle eventually
    deletes them on its own schedule.
    """
    out: list[dict] = []
    try:
        client = _client()
        bucket = client.bucket(CACHE_BUCKET)
        # Iterate primary rows only — by-slug/* are pointer docs that
        # don't carry the freshness data we need for <lastmod>.
        for blob in bucket.list_blobs(prefix=""):
            if blob.name.startswith("by-slug/"):
                continue
            if not blob.name.endswith(".json"):
                continue
            try:
                row = json.loads(blob.download_as_text())
            except Exception:
                continue
            slug = row.get("slug")
            completed_at = row.get("completed_at")
            if not slug or not completed_at:
                continue
            try:
                completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if datetime.now(timezone.utc) - completed_dt > timedelta(days=CACHE_TTL_DAYS):
                continue
            out.append({
                "slug": slug,
                "completed_at": completed_at,
                "daycare_name": row.get("daycare_name"),
            })
    except Exception:
        log.warning("daycare_cache_list_failed", exc_info=True)
    out.sort(key=lambda r: r["completed_at"], reverse=True)
    return out
