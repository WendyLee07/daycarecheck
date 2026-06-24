#!/usr/bin/env python3
"""Daycarecheck backend — bridges frontend to clawgrid task pipeline.

Endpoints:
    GET  /                          → static daycare site (index.html, etc.)
    POST /api/diligence             → create task on clawgrid, return task_id + ac_id
    GET  /api/diligence/{id}/stream → SSE: live status updates from clawgrid
    GET  /api/diligence/{id}        → one-shot final result with artifact

Auth model:
    The frontend never sees the clawgrid JWT. Backend keeps the JWT in memory
    (auto-renewed from the local Ed25519 private key) and signs all calls.

Run:
    cd /home/dev/workspace/who-owns-nyc-vets/daycare
    python3 backend.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("daycarecheck")

# ── Config ────────────────────────────────────────────────────────────────────

CLAWFORCE_BASE = os.environ.get("CLAWFORCE_BASE", "https://clawgrid.ai")
PROJECT_ID     = os.environ.get("DAYCARE_PROJECT_ID", "22d6f178-62d1-4c7d-83d3-e4a2297eee40")
CLIENT_AGENT   = os.environ.get("CLAWFORCE_CLIENT_AGENT_ID", "4792107f-bc3c-4018-96c7-92e7809ae776")
JWT_USER_ID    = os.environ.get("CLAWFORCE_USER_ID", "56efe8ce-7278-4c99-9f2e-dffdc7222ef7")
JWT_USER_EMAIL = os.environ.get("CLAWFORCE_USER_EMAIL", "wendy.li@heydora.ai")

# Routing: every daycare-diligence task goes into the ClawGrid `tag_pool` so
# the platform picks an idle, qualified Manus agent (queue when empty, auto
# failover on agent death). We DO NOT pin a single agent — that was the old
# DAYCARE_DIRECT_AGENT_ID antipattern. Agents qualify by carrying the
# `manus_qualified` EntityTag in the `agent_capability` category.
# Capability keys an agent must hold to enter the daycarecheck dispatch
# pool. Selection runs with match_mode=all on the ClawGrid side, so an
# agent must hold BOTH keys (rows in agent_capabilities) to be eligible.
# Dot-namespaced format aligns with the platform's existing capability
# convention (browser.patchright, network.residential, etc.) and is
# decoupled from any specific runtime brand.
CAPABILITY_KEYS = ["browser.playwright", "llm.advanced"]

# Two URLs serve very different audiences:
#
#  - DAYCARE_WEBHOOK_URL  →  ClawGrid → daycarecheck callback path. Must
#    point at the Cloud Run service directly (no proxy hop) so internal
#    task.assigned / task.completed / task.cancelled webhooks are fast.
#
#  - DAYCARE_PUBLIC_URL   →  user-facing URLs (report links in emails,
#    share buttons, etc.). Goes through the agentic-commons FastAPI
#    reverse proxy so the user sees the friendly subdomain.
#
# Both default sensibly; in prod they're overridden via Cloud Run env vars.
DAYCARE_WEBHOOK_URL = (
    os.environ.get("DAYCARE_WEBHOOK_URL")
    or "https://daycarecheck-969911375916.us-central1.run.app"
)
DAYCARE_PUBLIC_URL = (
    os.environ.get("DAYCARE_PUBLIC_URL")
    or "https://daycarecheck.agentic-commons.org"
)

# Report permalink template — used both for the report.html endpoint
# response body links and as the URL inserted into "your report is ready"
# emails to the requester.
REPORT_URL_TEMPLATE = DAYCARE_PUBLIC_URL.rstrip("/") + "/api/diligence/{task_id}/report.html"

# MailerSend (transactional email). Standard envs mirrored from clawforce.
# Cloud Run picks these up from Secret Manager. If MAILERSEND_API_KEY is
# unset (e.g. local dev), emails are skipped with a structured log line
# rather than crashing the request.
MAILERSEND_API_KEY      = os.environ.get("MAILERSEND_API_KEY") or None
MAILERSEND_FROM_EMAIL   = os.environ.get("MAILERSEND_FROM_EMAIL", "noreply@agentic-commons.org")
MAILERSEND_FROM_NAME    = os.environ.get("MAILERSEND_FROM_NAME", "Daycare Check")

# Display-name overrides for the Manus-qualified pool. ClawGrid's
# `agents.name` column is just the agent name (e.g. "Manus"), but users
# expect the "owner-runtime" form (e.g. "wendy-Manus") in the live log so
# they can tell which physical operator's lobster is doing the work.
#
# v1 hardcoded mapping keyed by agent UUID — pool is tiny (2 agents),
# replacing with a clawgrid API lookup is a follow-up once the pool grows
# past ~5 agents or we have multiple lobsters per owner.
AGENT_DISPLAY_NAMES: dict[str, str] = {
    "20f522ac-8ab2-4b30-82ab-bc594cf4faab": "wendy-Manus",
    "3e993962-68e8-4254-bd95-176cdfb86cf4": "david-zCrab",
}


def _extract_city_state(
    spec: dict | None,
    task: dict | None,
) -> tuple[str | None, str | None]:
    """Best-effort city + state extraction from a task's structured_spec
    and (optionally) the original ClawGrid task dict.

    Order of preference:
      1. ``spec.location_hint`` — Photon-resolved string we ourselves sent
         when the user picked a suggestion. Format like "123 Main St,
         Brooklyn, New York" — last two comma parts are usually city + state.
      2. Anything in the artifact's practical_info (not always available
         when this runs — webhook may fire before normalization).
    Returns (city, state); either may be None.
    """
    spec = spec or {}
    raw = (spec.get("location_hint") or "").strip()
    if raw:
        # Split on commas, strip each piece. We expect "[street,] city, state[, country]".
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        # Strip a "United States" / "USA" trailing token if present.
        if parts and parts[-1].lower() in ("united states", "usa", "us"):
            parts = parts[:-1]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        if len(parts) == 1:
            return parts[0], None
    return None, None


def _assignee_display(task: dict | None) -> str | None:
    """Build the user-friendly assignee label from a clawgrid task dict.

    Prefers the owner-runtime mapping above; falls back to ClawGrid's raw
    `assignee_name`; finally None when the task is unassigned.
    """
    if not task:
        return None
    aid = task.get("assignee_id")
    if aid and aid in AGENT_DISPLAY_NAMES:
        return AGENT_DISPLAY_NAMES[aid]
    return task.get("assignee_name")

CONFIG_DIR = Path.home() / ".config" / "clawforce"
PRIV_KEY_PATH = Path(os.environ.get("CLAWFORCE_JWT_PRIVATE_KEY_PATH", CONFIG_DIR / "jwt_private_key.pem"))
KID_PATH      = Path(os.environ.get("CLAWFORCE_JWT_KID_PATH",         CONFIG_DIR / "jwt_kid"))

STATIC_ROOT = Path(__file__).parent

# ── JWT signer with auto-renew ────────────────────────────────────────────────

class JWTManager:
    """Keeps a fresh prod JWT in memory, re-signs when within 5 min of expiry."""

    def __init__(self) -> None:
        self._priv_pem = PRIV_KEY_PATH.read_text()
        self._kid = KID_PATH.read_text().strip()
        self._token: str | None = None
        self._exp: datetime | None = None

    def get(self) -> str:
        now = datetime.now(timezone.utc)
        if not self._token or not self._exp or self._exp - now < timedelta(minutes=5):
            self._sign()
        return self._token  # type: ignore[return-value]

    def _sign(self) -> None:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=24)
        claims = {
            "sub": JWT_USER_ID,
            "email": JWT_USER_EMAIL,
            "type": "access",
            "iat": now,
            "exp": exp,
            "jti": str(uuid.uuid4()),
        }
        self._token = jwt.encode(claims, self._priv_pem, algorithm="EdDSA", headers={"kid": self._kid})
        self._exp = exp
        log.info("JWT (re)signed, expires %s", exp.isoformat())


jwt_mgr = JWTManager()

# ── Clawgrid HTTP wrapper (async) ─────────────────────────────────────────────

import httpx  # noqa: E402

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=20)
    return _http


async def cf_request(method: str, path: str, **kw: Any) -> httpx.Response:
    headers = kw.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {jwt_mgr.get()}"
    headers.setdefault("Content-Type", "application/json")
    return await _client().request(method, f"{CLAWFORCE_BASE}{path}", headers=headers, **kw)


# ── Artifact normalizer ──────────────────────────────────────────────────────
# Different lobsters use different ad-hoc schemas. Map them all to one canonical
# shape so the frontend doesn't need to care which lobster produced the result.

def _first_truthy(d: dict, *keys):
    """Return the first non-empty value among keys."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _normalize_item(item: dict) -> dict:
    """Map any lobster's schema to a canonical daycare-ownership shape."""

    headline = _first_truthy(
        item,
        "direct_parent_company",   # Scott rich schema
        "parent_company",          # KinderCare / Cadence flat schema
        "ownership_entity",        # pony-ai schema
        "entity_name",             # Endeavor lobster schema
        "ultimate_owner",          # fallback
    ) or "Unknown"

    summary = _first_truthy(
        item,
        "ownership_summary",
        "parent_company_description",
        "description",
        "ultimate_owner_description",
    )

    summary_note = _first_truthy(
        item,
        "direct_parent_note",
        "verification_notes",
        "notes",
    )

    # Prior parent (Scott schema only — for now)
    prior = None
    if item.get("prior_parent_company"):
        prior = {
            "name": item["prior_parent_company"],
            "acquired_at": item.get("prior_parent_acquisition_date"),
            "bankrupted_at": item.get("prior_parent_bankruptcy_date"),
            "current_status": item.get("prior_parent_current_status"),
        }

    # Chain — Scott / Endeavor / hand-rolled schemas
    chain = []
    for level in (item.get("ownership_chain") or []):
        chain.append({
            "level": level.get("level"),
            "entity": level.get("entity") or level.get("entity_name"),
            "role": level.get("role"),
            "source_url": level.get("source_url"),
            "source_note": level.get("source_note"),
        })

    # Sources — aggregate all URL-shaped fields, dedupe
    src_set = []
    def _add(u):
        if isinstance(u, str) and u.startswith("http") and u not in src_set:
            src_set.append(u)

    _add(item.get("source_url"))
    for u in (item.get("source_urls") or []):
        _add(u)
    for level in (item.get("ownership_chain") or []):
        _add(level.get("source_url"))

    return {
        "daycare_name": item.get("daycare_name") or item.get("entity") or item.get("entity_name"),
        "headline_owner": headline,
        "ownership_type": _first_truthy(item, "ownership_type", "owner_type"),
        "ticker": item.get("ticker"),
        "acquisition_date": item.get("acquisition_date"),
        "ownership_summary": summary,
        "summary_note": summary_note,
        "prior_parent": prior,
        "chain": chain,
        "sources": src_set,
        "note": item.get("note"),  # transparency / QA corrections
    }


def _looks_like_chain_items(items_raw: list[dict]) -> bool:
    """Detect the 'chain-as-items' schema used by some lobsters.

    Each item is one level of an ownership chain (queried entity → parent →
    ultimate owner → prior owner) instead of a separate daycare report.
    Heuristic: 2+ items where at least one has a chain-relationship field.
    """
    if len(items_raw) < 2:
        return False
    chain_fields = ("relationship", "ultimate_owner", "ultimate_owner_note", "key_person")
    hits = sum(1 for it in items_raw if any(k in it for k in chain_fields))
    return hits >= 1


def _merge_chain_items(items_raw: list[dict], top_summary: str | None) -> dict:
    """Collapse a chain-style items[] into a single canonical primary."""
    src_set: list[str] = []

    def _add_url(u):
        if isinstance(u, str) and u.startswith("http") and u not in src_set:
            src_set.append(u)

    chain = []
    headline_owner = None
    ultimate_owner = None
    prior = None
    daycare_name = None

    for idx, it in enumerate(items_raw):
        entity = it.get("entity") or it.get("entity_name")
        rel = (it.get("relationship") or "").lower()
        if idx == 0:
            daycare_name = entity
            # Direct parent for the queried entity
            headline_owner = it.get("parent_company") or it.get("direct_parent_company")
        if "ultimate" in rel or it.get("ultimate_owner"):
            ultimate_owner = it.get("ultimate_owner") or entity
        if "prior" in rel or "former" in rel:
            prior = {
                "name": entity,
                "current_status": it.get("note") or it.get("relationship"),
            }

        chain.append({
            "level": idx + 1,
            "entity": entity,
            "role": it.get("relationship"),
            "source_url": it.get("source_url"),
            "source_note": _first_truthy(it, "parent_company_note", "ultimate_owner_note", "note"),
        })

        _add_url(it.get("source_url"))
        for u in (it.get("source_urls") or []):
            _add_url(u)

    if not headline_owner and len(chain) >= 2:
        headline_owner = chain[1]["entity"]
    if not ultimate_owner and chain:
        # Last entity that isn't "prior" — best-effort.
        for c in reversed(chain):
            r = (c.get("role") or "").lower()
            if "prior" not in r and "former" not in r:
                ultimate_owner = c["entity"]
                break

    return {
        "daycare_name": daycare_name,
        "headline_owner": headline_owner or "Unknown",
        "ultimate_owner": ultimate_owner,
        "ownership_type": None,
        "ticker": None,
        "acquisition_date": None,
        "ownership_summary": top_summary,
        "summary_note": None,
        "prior_parent": prior,
        "chain": chain,
        "sources": src_set,
        "note": None,
    }


_FULL_REPORT_SECTION_KEYS = {
    "licensing", "inspections", "violations", "incidents",
    "ownership", "staff_signals", "executive_summary",
}


def _looks_like_full_report(items_raw: list[dict]) -> bool:
    """Detect the new 6-section background-check schema.

    Heuristic: first item has at least 2 of the canonical section keys
    AND `ownership` is itself a dict (in legacy schemas ownership_chain
    was a top-level field, ownership wasn't a nested object).
    """
    if not items_raw:
        return False
    item = items_raw[0]
    hits = sum(1 for k in _FULL_REPORT_SECTION_KEYS if k in item)
    if hits < 2:
        return False
    ownership = item.get("ownership")
    return isinstance(ownership, dict)


def _normalize_full_report_item(item: dict) -> dict:
    """Normalize the new 6-section background-check schema.

    Preserves all sections as-is for the formal report renderer, and also
    derives the legacy fields (`headline_owner`, `chain`, `sources`) so
    the existing frontend in-page card keeps rendering during transition.
    """
    ownership = item.get("ownership") or {}

    # Aggregate every URL across all sections, dedup, preserve order
    sources: list[str] = []

    def _add(u):
        if isinstance(u, str) and u.startswith("http") and u not in sources:
            sources.append(u)

    for sec_key in ("licensing", "inspections", "violations", "ownership", "staff_signals"):
        sec = item.get(sec_key)
        if isinstance(sec, dict):
            _add(sec.get("source_url"))
    for inc in (item.get("incidents") or []):
        if isinstance(inc, dict):
            _add(inc.get("source_url"))
    for level in (ownership.get("ownership_chain") or []):
        if isinstance(level, dict):
            _add(level.get("source_url"))
    licensing = item.get("licensing") or {}
    for action in (licensing.get("disciplinary_actions") or []):
        if isinstance(action, dict):
            _add(action.get("source_url"))
    staff = item.get("staff_signals") or {}
    _add(staff.get("glassdoor_url"))
    _add(staff.get("naeyc_source_url"))
    pi = item.get("practical_info") or {}
    pricing = pi.get("pricing") if isinstance(pi.get("pricing"), dict) else {}
    _add(pricing.get("source_url") if isinstance(pricing, dict) else None)
    sentiment = pi.get("recent_parent_sentiment") if isinstance(pi.get("recent_parent_sentiment"), dict) else {}
    for u in (sentiment.get("source_urls") or []) if isinstance(sentiment, dict) else []:
        _add(u)

    # Legacy-compatible chain
    chain = []
    for level in (ownership.get("ownership_chain") or []):
        if not isinstance(level, dict):
            continue
        chain.append({
            "level": level.get("level"),
            "entity": level.get("entity"),
            "role": level.get("role"),
            "source_url": level.get("source_url"),
            "source_note": level.get("source_note"),
        })

    return {
        # Full-report sections (new — used by formal report template)
        "executive_summary": item.get("executive_summary"),
        "licensing": item.get("licensing"),
        "inspections": item.get("inspections"),
        "violations": item.get("violations"),
        "incidents": item.get("incidents") or [],
        "ownership_full": ownership,
        "staff_signals": item.get("staff_signals"),
        "practical_info": item.get("practical_info"),

        # Legacy fields (kept so existing frontend doesn't break)
        "daycare_name": item.get("daycare_name"),
        "headline_owner": ownership.get("ultimate_parent") or ownership.get("direct_owner") or "Unknown",
        "ownership_type": ownership.get("owner_type"),
        "ticker": None,
        "acquisition_date": ownership.get("pe_acquisition_year"),
        "ownership_summary": item.get("executive_summary"),
        "summary_note": None,
        "prior_parent": None,
        "chain": chain,
        "sources": sources,
        "note": None,

        "_schema": "full_report_v1",
    }


def _normalize_artifact(artifact: dict | None) -> dict | None:
    if not artifact:
        return None

    data = artifact.get("data") or {}
    items_raw = data.get("items") or []
    top_summary = data.get("summary")

    if _looks_like_full_report(items_raw):
        items = [_normalize_full_report_item(it) for it in items_raw]
    elif _looks_like_chain_items(items_raw):
        primary = _merge_chain_items(items_raw, top_summary)
        items = [primary]
    else:
        items = [_normalize_item(it) for it in items_raw]

    return {
        "submission_marker": artifact.get("submission_marker"),
        "qa_score": artifact.get("qa_score"),
        "qa_final": artifact.get("qa_final"),
        "items": items,
        "primary": items[0] if items else None,
        "raw": artifact,  # frontend can ignore but keep for debugging
    }


# ── Formal HTML report renderer ───────────────────────────────────────────────
# Government-document style: white background, serif typeface, sectioned with
# rule lines, every claim followed by a numbered citation, provenance footer.
# Self-contained (CSS inline) so the downloaded file opens anywhere.

from html import escape as _esc  # noqa: E402


def _fmt_date(s: Any) -> str:
    if not s:
        return "—"
    return _esc(str(s))


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or url
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return url


def _cite(url: str | None, citations: list[str]) -> str:
    """Append url to citation list and return a superscript [n] link."""
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return ""
    if url in citations:
        n = citations.index(url) + 1
    else:
        citations.append(url)
        n = len(citations)
    return f' <a href="#cite-{n}" class="cite">[{n}]</a>'


def _section_licensing(lic: dict | None, citations: list[str]) -> str:
    if not lic or lic.get("data_available") is False:
        return _empty_section("No licensing records found in public databases.")
    status = (lic.get("status") or "unknown").lower()
    badge_cls = {
        "active": "ok",
        "expired": "warn",
        "probation": "warn",
        "revoked": "bad",
    }.get(status, "neutral")
    rows = [
        ("License status", f'<span class="badge {badge_cls}">{_esc(status.upper())}</span>{_cite(lic.get("source_url"), citations)}'),
        ("License number", _esc(lic.get("license_number") or "—")),
        ("Issuing agency", _esc(lic.get("state_agency") or "—")),
        ("Expiration date", _fmt_date(lic.get("expires_date"))),
    ]
    body = _field_table(rows)
    actions = lic.get("disciplinary_actions") or []
    if actions:
        # Newest first — same time-axis convention as incidents / inspections.
        actions = sorted(
            (a for a in actions if isinstance(a, dict)),
            key=lambda a: a.get("date") or "",
            reverse=True,
        )
        body += '<h3 class="subsection">Disciplinary actions on record</h3><ul class="action-list">'
        for a in actions:
            body += (
                f'<li><strong>{_fmt_date(a.get("date"))} — {_esc(a.get("type") or "Action")}.</strong> '
                f'{_esc(a.get("summary") or "")}{_cite(a.get("source_url"), citations)}</li>'
            )
        body += "</ul>"
    return body


def _section_inspections(ins: dict | None, citations: list[str]) -> str:
    if not ins or ins.get("data_available") is False:
        return _empty_section("No public inspection records found.")
    outcome = (ins.get("outcome") or "unknown").lower()
    badge_cls = {
        "clean": "ok",
        "corrections": "warn",
        "violations": "bad",
    }.get(outcome, "neutral")
    rows = [
        ("Most recent inspection", _fmt_date(ins.get("last_inspection_date")) + _cite(ins.get("source_url"), citations)),
        ("Outcome", f'<span class="badge {badge_cls}">{_esc(outcome.upper())}</span>'),
        ("Inspection frequency (24 mo)", _esc(str(ins.get("frequency_months_24mo") or "—")) + " months between visits"),
    ]
    body = _field_table(rows)
    summary = ins.get("violations_summary_plain_english")
    if summary:
        body += f'<p class="paragraph">{_esc(summary)}</p>'
    return body


def _section_violations(v: dict | None, citations: list[str]) -> str:
    if not v or v.get("data_available") is False:
        return _empty_section("No substantiated violations on file.")
    total = v.get("total_substantiated_36mo") or 0
    body = (
        f'<p class="paragraph"><strong>{_esc(str(total))}</strong> substantiated '
        f'violation(s) in the past 36 months{_cite(v.get("source_url"), citations)}.</p>'
    )
    cats = v.get("by_category") or {}
    if cats:
        body += '<table class="cat-table"><thead><tr><th>Category</th><th>Count</th></tr></thead><tbody>'
        labels = {
            "ratio": "Staff-to-child ratio",
            "supervision": "Supervision",
            "health_sanitation": "Health & sanitation",
            "background_checks": "Staff background checks",
            "facility_safety": "Facility safety",
        }
        for key, label in labels.items():
            n = cats.get(key, 0) or 0
            cls = "bad" if n >= 3 else ("warn" if n >= 1 else "ok")
            body += f'<tr><td>{_esc(label)}</td><td class="{cls}">{_esc(str(n))}</td></tr>'
        body += "</tbody></table>"
    pattern = v.get("repeat_pattern_note")
    if pattern:
        body += f'<p class="paragraph"><em>Pattern note:</em> {_esc(pattern)}</p>'
    return body


def _section_incidents(incidents: list[dict] | None, citations: list[str]) -> str:
    if not incidents:
        return _empty_section("No documented child-safety incidents in court or news records.")
    # Newest first — parents care most about recent events. Sort by ISO-ish
    # date string descending (works for "2024-08", "2024-08-12", and full ISO).
    incidents = sorted(
        (i for i in incidents if isinstance(i, dict)),
        key=lambda i: i.get("date") or "",
        reverse=True,
    )
    body = ""
    for inc in incidents:
        sev = (inc.get("severity") or "medium").lower()
        body += (
            f'<div class="incident-block sev-{_esc(sev)}">'
            f'<div class="incident-meta">{_fmt_date(inc.get("date"))} · '
            f'{_esc(inc.get("location") or "—")} · '
            f'<span class="badge sev-{_esc(sev)}-badge">{_esc(sev.upper())}</span></div>'
            f'<p class="paragraph">{_esc(inc.get("summary") or "")}{_cite(inc.get("source_url"), citations)}</p>'
            f"</div>"
        )
    return body


def _section_ownership(own: dict | None, citations: list[str]) -> str:
    if not own:
        return _empty_section("Ownership information could not be determined.")
    rows = [
        ("Direct legal owner", _esc(own.get("direct_owner") or "—") + _cite(own.get("source_url"), citations)),
        ("Ultimate parent", _esc(own.get("ultimate_parent") or "—")),
        ("Owner type", _esc((own.get("owner_type") or "—").replace("_", " ").title())),
    ]
    if own.get("franchise_of"):
        rows.append(("Franchise of", _esc(own["franchise_of"])))
    if own.get("pe_acquisition_year"):
        rows.append(("PE acquisition year", _esc(str(own["pe_acquisition_year"]))))
    body = _field_table(rows)
    chain = own.get("ownership_chain") or []
    if chain:
        body += '<h3 class="subsection">Ownership chain</h3><ol class="chain-list">'
        for level in chain:
            if not isinstance(level, dict):
                continue
            body += (
                f'<li><strong>{_esc(level.get("entity") or "—")}</strong>'
                f'{" — " + _esc(level.get("role")) if level.get("role") else ""}'
                f"{_cite(level.get('source_url'), citations)}</li>"
            )
        body += "</ol>"
    return body


def _build_fact_sheet(pi: dict, citations: list[str]) -> str:
    """Top-of-report fact sheet: photo + ages/capacity/hours/pricing/parent
    rating + website + Google Maps. Pulls from practical_info.

    Returns "" if practical_info has nothing useful — letterhead stands alone.
    """
    if not pi:
        return ""

    photo_url = pi.get("photo_url")
    website_url = pi.get("website_url")
    maps_url = pi.get("google_maps_url")

    rows: list[str] = []
    if pi.get("ages_served"):
        rows.append(f'<div class="fs-row"><span class="fs-label">Ages served</span><span class="fs-value">{_esc(pi["ages_served"])}</span></div>')
    if pi.get("capacity") is not None:
        rows.append(f'<div class="fs-row"><span class="fs-label">Licensed capacity</span><span class="fs-value">{_esc(str(pi["capacity"]))} children</span></div>')
    if pi.get("hours_of_operation"):
        rows.append(f'<div class="fs-row"><span class="fs-label">Hours</span><span class="fs-value">{_esc(pi["hours_of_operation"])}</span></div>')

    pricing = pi.get("pricing") or {}
    if isinstance(pricing, dict) and pricing.get("estimate"):
        conf = (pricing.get("confidence") or "").lower()
        conf_note = ""
        if pricing.get("source"):
            conf_note = f' <span class="fs-hint">— {_esc(pricing["source"])}</span>'
        elif conf in ("high", "medium", "low"):
            conf_note = f' <span class="fs-hint">({_esc(conf)} confidence estimate)</span>'
        cite_html = _cite(pricing.get("source_url"), citations) if pricing.get("source_url") else ""
        rows.append(f'<div class="fs-row"><span class="fs-label">Pricing</span><span class="fs-value">{_esc(pricing["estimate"])}{conf_note}{cite_html}</span></div>')

    sent = pi.get("recent_parent_sentiment") or {}
    if isinstance(sent, dict) and (sent.get("rating_avg") is not None or sent.get("summary")):
        bits = []
        if sent.get("rating_avg") is not None:
            bits.append(f'<strong>{_esc(str(sent["rating_avg"]))} / 5</strong>')
        if sent.get("sample_size"):
            bits.append(f'{_esc(str(sent["sample_size"]))} reviews')
        if sent.get("review_period"):
            bits.append(f'<span class="fs-hint">{_esc(sent["review_period"])}</span>')
        rating_line = " · ".join(bits)
        # Inline parent-summary one-liner under rating
        summary_html = ""
        if sent.get("summary"):
            cite_for_summary = ""
            for u in (sent.get("source_urls") or [])[:2]:
                cite_for_summary += _cite(u, citations)
            summary_html = f'<div class="fs-sentiment">{_esc(sent["summary"])}{cite_for_summary}</div>'
        rows.append(f'<div class="fs-row"><span class="fs-label">Parent rating</span><span class="fs-value">{rating_line}{summary_html}</span></div>')

    if not rows and not photo_url and not website_url and not maps_url:
        return ""

    photo_html = ""
    if photo_url and isinstance(photo_url, str) and photo_url.startswith("http"):
        photo_html = f'<div class="fs-photo"><img src="{_esc(photo_url)}" alt="" loading="lazy"></div>'

    links: list[str] = []
    if website_url and isinstance(website_url, str) and website_url.startswith("http"):
        try:
            from urllib.parse import urlparse
            host = urlparse(website_url).hostname or website_url
            host = host[4:] if host.startswith("www.") else host
        except Exception:
            host = website_url
        links.append(f'<a href="{_esc(website_url)}" target="_blank" rel="noopener">🌐 {_esc(host)}</a>')
    if maps_url and isinstance(maps_url, str) and maps_url.startswith("http"):
        links.append(f'<a href="{_esc(maps_url)}" target="_blank" rel="noopener">📍 Google Maps</a>')
    links_html = f'<div class="fs-links">{"".join(links)}</div>' if links else ""

    rows_html = f'<div class="fs-rows">{"".join(rows)}</div>' if rows else ""

    return f'<aside class="fact-sheet">{photo_html}<div class="fs-content">{rows_html}{links_html}</div></aside>'


def _section_practical_info(pi: dict | None, citations: list[str]) -> str:
    """Section 7 — operator-claimed / user-review tier. Visually marked as
    a different trust tier from sections 1-6."""
    if not pi or pi.get("data_available") is False:
        return _empty_section("Practical operating info (ages / hours / pricing) is not currently available for this daycare.")

    rows: list[tuple[str, str]] = []
    if pi.get("ages_served"):
        rows.append(("Ages served", _esc(pi["ages_served"])))
    if pi.get("capacity") is not None:
        rows.append(("Licensed capacity", _esc(str(pi["capacity"])) + " children"))
    if pi.get("hours_of_operation"):
        rows.append(("Hours of operation", _esc(pi["hours_of_operation"])))

    pricing = pi.get("pricing") or {}
    if isinstance(pricing, dict) and pricing.get("estimate"):
        conf = (pricing.get("confidence") or "unknown").lower()
        conf_cls = {"high": "ok", "medium": "warn", "low": "bad"}.get(conf, "neutral")
        pricing_html = f'<strong>{_esc(pricing["estimate"])}</strong>'
        if pricing.get("source"):
            pricing_html += f' <span class="hint">— {_esc(pricing["source"])}</span>'
        if conf in ("high", "medium", "low"):
            pricing_html += f' <span class="badge {conf_cls}">{_esc(conf.upper())} CONFIDENCE</span>'
        if pricing.get("source_url"):
            pricing_html += _cite(pricing["source_url"], citations)
        rows.append(("Pricing estimate", pricing_html))

    body = _field_table(rows) if rows else ""

    sent = pi.get("recent_parent_sentiment") or {}
    if isinstance(sent, dict) and (sent.get("summary") or sent.get("rating_avg") is not None):
        body += '<h3 class="subsection">Recent parent sentiment</h3>'
        meta_bits = []
        if sent.get("rating_avg") is not None:
            meta_bits.append(f'<strong>{_esc(str(sent["rating_avg"]))} / 5</strong> avg')
        if sent.get("sample_size"):
            meta_bits.append(f'{_esc(str(sent["sample_size"]))} reviews')
        if sent.get("review_period"):
            meta_bits.append(f'{_esc(sent["review_period"])}')
        if meta_bits:
            body += f'<p class="meta">{" · ".join(meta_bits)}</p>'
        if sent.get("summary"):
            cite_for_summary = ""
            for u in (sent.get("source_urls") or [])[:3]:
                cite_for_summary += _cite(u, citations)
            body += f'<p class="paragraph">{_esc(sent["summary"])}{cite_for_summary}</p>'

    if not body:
        return _empty_section("Practical operating info (ages / hours / pricing) is not currently available for this daycare.")

    return body


def _section_staff(staff: dict | None, citations: list[str]) -> str:
    if not staff or staff.get("data_available") is False:
        return _empty_section("No public staff or accreditation signals available.")
    rows = []
    if staff.get("glassdoor_rating") is not None:
        rows.append((
            "Glassdoor rating",
            f'{_esc(str(staff["glassdoor_rating"]))} / 5'
            f'{_cite(staff.get("glassdoor_url"), citations)}',
        ))
    if staff.get("estimated_turnover_pct") is not None:
        rows.append(("Estimated staff turnover", f'{_esc(str(staff["estimated_turnover_pct"]))}% annually'))
    if staff.get("naeyc_accredited") is not None:
        v = "Yes" if staff["naeyc_accredited"] else "No"
        rows.append(("NAEYC accreditation", v + _cite(staff.get("naeyc_source_url"), citations)))
    other = staff.get("other_accreditations") or []
    if other:
        rows.append(("Other accreditations", _esc(", ".join(str(o) for o in other))))
    if not rows:
        return _empty_section("No public staff or accreditation signals available.")
    return _field_table(rows)


def _empty_section(msg: str) -> str:
    return f'<p class="paragraph empty">{_esc(msg)}</p>'


def _field_table(rows: list[tuple[str, str]]) -> str:
    out = '<table class="field-table"><tbody>'
    for label, val in rows:
        out += f"<tr><th>{_esc(label)}</th><td>{val}</td></tr>"
    out += "</tbody></table>"
    return out


def _citations_block(citations: list[str]) -> str:
    if not citations:
        return ""
    out = '<section class="footnotes"><h2 class="section">Sources cited</h2><ol>'
    for i, url in enumerate(citations, 1):
        out += (
            f'<li id="cite-{i}"><a href="{_esc(url)}" target="_blank" rel="noopener">'
            f'{_esc(_host(url))}</a> &mdash; <span class="cite-url">{_esc(url)}</span></li>'
        )
    out += "</ol></section>"
    return out


REPORT_CSS = """
@page { size: letter; margin: 0.75in; }
* { box-sizing: border-box; }
body {
  font-family: 'Charter', 'Source Serif Pro', Georgia, 'Times New Roman', serif;
  font-size: 11pt;
  line-height: 1.55;
  color: #1a1a1a;
  background: white;
  max-width: 7.5in;
  margin: 40px auto;
  padding: 0 32px;
}
.letterhead {
  border-bottom: 2.5px solid #1a1a1a;
  padding-bottom: 16px;
  margin-bottom: 28px;
}
.brand {
  font-size: 9pt;
  letter-spacing: 3px;
  text-transform: uppercase;
  color: #555;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
h1.doctitle {
  font-size: 26pt;
  font-weight: 600;
  line-height: 1.15;
  margin: 10px 0 6px;
  font-family: 'Charter', 'Source Serif Pro', Georgia, serif;
}
.loc-line {
  font-size: 10pt;
  color: #555;
  font-style: italic;
}
.summary-box {
  background: #f5f1eb;
  border-left: 3px solid #1a1a1a;
  padding: 16px 20px;
  margin: 24px 0 32px;
  font-size: 11.5pt;
}
.summary-box .label {
  font-size: 9pt;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: #555;
  margin-bottom: 6px;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
h2.section {
  font-size: 14pt;
  font-weight: 600;
  border-bottom: 1px solid #888;
  padding-bottom: 4px;
  margin: 28px 0 14px;
}
h3.subsection {
  font-size: 11pt;
  font-weight: 600;
  margin: 18px 0 8px;
  color: #333;
}
table.field-table {
  width: 100%;
  border-collapse: collapse;
  margin: 4px 0 8px;
}
table.field-table th {
  text-align: left;
  width: 35%;
  padding: 5px 12px 5px 0;
  vertical-align: top;
  font-weight: 500;
  color: #555;
  font-size: 10.5pt;
}
table.field-table td {
  padding: 5px 0;
  vertical-align: top;
}
table.cat-table {
  width: 60%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 10.5pt;
}
table.cat-table th, table.cat-table td {
  text-align: left;
  padding: 5px 10px;
  border-bottom: 1px solid #ddd;
}
table.cat-table td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
table.cat-table .bad { color: #b91c1c; font-weight: 600; }
table.cat-table .warn { color: #c2410c; font-weight: 600; }
table.cat-table .ok { color: #1d6b3a; }
.badge {
  display: inline-block;
  padding: 1px 8px;
  font-size: 9pt;
  font-weight: 600;
  letter-spacing: 1px;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
.badge.ok { background: #1d6b3a; color: white; }
.badge.warn { background: #c2410c; color: white; }
.badge.bad { background: #b91c1c; color: white; }
.badge.neutral { background: #555; color: white; }
.badge.sev-critical-badge { background: #b91c1c; color: white; }
.badge.sev-high-badge { background: #c2410c; color: white; }
.badge.sev-medium-badge { background: #fbc02d; color: #000; }
.incident-block {
  margin: 12px 0;
  padding: 12px 16px;
  border-left: 3px solid #ccc;
  background: #fafaf6;
}
.incident-block.sev-critical { border-left-color: #b91c1c; }
.incident-block.sev-high { border-left-color: #c2410c; }
.incident-block.sev-medium { border-left-color: #fbc02d; }
.incident-meta {
  font-size: 9.5pt;
  color: #666;
  margin-bottom: 6px;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
.paragraph { margin: 8px 0; }
.paragraph.empty { color: #888; font-style: italic; }
.action-list, .chain-list { margin: 8px 0 8px 24px; }
.action-list li, .chain-list li { margin: 6px 0; }
a.cite {
  font-size: 8pt;
  vertical-align: super;
  color: #1976d2;
  text-decoration: none;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
section.footnotes {
  margin-top: 36px;
  page-break-before: always;
}
section.footnotes ol {
  font-size: 9.5pt;
  margin: 12px 0 0 24px;
}
section.footnotes li {
  margin: 8px 0;
  line-height: 1.4;
}
section.footnotes .cite-url {
  color: #888;
  font-size: 8.5pt;
  word-break: break-all;
}
section.footnotes a { color: #1a1a1a; }

/* === Top-of-report fact sheet (photo + ages/hours/pricing/parent rating/links) === */
.fact-sheet {
  display: flex;
  gap: 22px;
  margin: 0 0 28px;
  padding: 18px;
  background: #fafaf6;
  border: 1px solid #e2ddd0;
}
.fact-sheet .fs-photo {
  flex: 0 0 180px;
}
.fact-sheet .fs-photo img {
  width: 180px;
  height: 135px;
  object-fit: cover;
  border-radius: 2px;
  background: #ece8e0;
}
.fact-sheet .fs-content {
  flex: 1;
  min-width: 0;
}
.fact-sheet .fs-rows {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
  font-size: 10pt;
  line-height: 1.5;
}
.fact-sheet .fs-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
}
.fact-sheet .fs-label {
  flex: 0 0 130px;
  font-size: 9pt;
  font-family: 'Helvetica Neue', Arial, sans-serif;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 1px;
  padding-top: 1px;
}
.fact-sheet .fs-value {
  flex: 1;
  color: #1a1a1a;
}
.fact-sheet .fs-hint {
  font-family: 'Helvetica Neue', Arial, sans-serif;
  font-size: 9pt;
  color: #888;
  font-weight: 400;
}
.fact-sheet .fs-sentiment {
  font-size: 9.5pt;
  color: #555;
  margin-top: 4px;
  font-style: italic;
  line-height: 1.5;
}
.fact-sheet .fs-links {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #e2ddd0;
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 9.5pt;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
.fact-sheet .fs-links a {
  color: #1a1a1a;
  text-decoration: underline;
  text-underline-offset: 2px;
}
/* When no photo (or photo failed), let content fill width */
.fact-sheet .fs-content:only-child { flex: 1 1 100%; }
@media print {
  .fact-sheet { page-break-inside: avoid; }
  .fact-sheet .fs-photo img { width: 160px; height: 120px; }
}
section.report-notes {
  margin-top: 36px;
  padding-top: 18px;
  border-top: 1px solid #888;
  font-size: 9.5pt;
  color: #444;
  line-height: 1.6;
}
section.report-notes h2.section {
  font-size: 11pt;
  font-weight: 600;
  border-bottom: none;
  margin: 0 0 12px 0;
  padding-bottom: 0;
  color: #1a1a1a;
}
section.report-notes h3 {
  font-size: 10pt;
  font-weight: 600;
  color: #1a1a1a;
  margin: 14px 0 4px;
  font-family: 'Charter', 'Source Serif Pro', Georgia, serif;
}
section.report-notes p,
section.report-notes ol,
section.report-notes ul {
  font-size: 9.5pt;
  line-height: 1.55;
  margin: 4px 0;
  color: #444;
}
section.report-notes ol,
section.report-notes ul {
  padding-left: 20px;
}
section.report-notes li {
  margin: 3px 0;
}
section.report-notes a {
  color: #1a1a1a;
  text-decoration: underline;
}
.provenance {
  margin-top: 40px;
  padding-top: 16px;
  border-top: 1px solid #888;
  font-size: 8.5pt;
  color: #555;
  line-height: 1.6;
  font-family: 'Helvetica Neue', Arial, sans-serif;
}
.provenance strong { color: #1a1a1a; }
.provenance a { color: #1a1a1a; }
@media print {
  body { margin: 0; padding: 0; max-width: none; }
  a { color: inherit; }
}
"""


def render_report_html(
    normalized_artifact: dict,
    *,
    task_id: str,
    alias: str | None,
    qa_score: int | None,
    slug: str | None = None,
) -> str:
    """Render a formal HTML background-check report from the normalized artifact."""
    primary = normalized_artifact.get("primary") or {}
    is_full = primary.get("_schema") == "full_report_v1"

    daycare_name = primary.get("daycare_name") or "Unknown daycare"
    location = primary.get("location") if is_full else None
    exec_summary = primary.get("executive_summary") or primary.get("ownership_summary")
    marker = normalized_artifact.get("submission_marker") or "—"
    gen_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    citations: list[str] = []

    if is_full:
        sec1 = _section_licensing(primary.get("licensing"), citations)
        sec2 = _section_inspections(primary.get("inspections"), citations)
        sec3 = _section_violations(primary.get("violations"), citations)
        sec4 = _section_incidents(primary.get("incidents"), citations)
        sec5 = _section_ownership(primary.get("ownership_full"), citations)
        sec6 = _section_staff(primary.get("staff_signals"), citations)
    else:
        # Legacy ownership-only artifact — only render ownership + sources
        legacy_own = {
            "direct_owner": primary.get("headline_owner"),
            "owner_type": primary.get("ownership_type"),
            "pe_acquisition_year": primary.get("acquisition_date"),
            "source_url": (primary.get("sources") or [None])[0],
            "ownership_chain": primary.get("chain") or [],
        }
        sec1 = _empty_section("This report was generated under the legacy ownership-only schema. Licensing data not available.")
        sec2 = _empty_section("Inspection data not available in legacy schema.")
        sec3 = _empty_section("Violation data not available in legacy schema.")
        sec4 = _empty_section("Incident data not available in legacy schema.")
        sec5 = _section_ownership(legacy_own, citations)
        sec6 = _empty_section("Staff signal data not available in legacy schema.")

    exec_html = (
        f'<div class="summary-box"><div class="label">Executive summary</div>'
        f"{_esc(exec_summary)}</div>"
        if exec_summary else ""
    )

    fact_sheet_html = _build_fact_sheet(primary.get("practical_info") or {}, citations) if is_full else ""

    loc_html = f'<div class="loc-line">{_esc(location)}</div>' if location else ""

    ac_alias_link = (
        f'<a href="https://agentic-commons.org/c/{_esc(alias)}" target="_blank">'
        f"agentic-commons.org/c/{_esc(alias)}</a>"
        if alias else "—"
    )

    # ── SEO head ─────────────────────────────────────────────────────
    # Title + meta description: what Google shows in search results.
    # Keep title under 60 chars where possible (Google truncates beyond);
    # description under 160. Both must include the daycare name + city
    # for keyword match on local searches.
    seo_title = f"{daycare_name}"
    if location:
        seo_title += f" — {location}"
    seo_title += " · Background Check Report"
    seo_title = seo_title[:120]

    # Pull a short summary fact-set for the meta description. Order of
    # preference: explicit summary → ownership → inspection note → fallback.
    desc_seed = (
        exec_summary
        or primary.get("ownership_summary")
        or primary.get("summary_note")
        or ""
    )
    seo_desc = (
        f"Public-record background check for {daycare_name}"
        f"{f' in {location}' if location else ''}. "
        f"Licensing, inspection violations, ownership chain, "
        f"and full source citations."
    )
    if desc_seed:
        # Use the artifact's own one-liner if it's tighter than the boilerplate.
        snip = " ".join(desc_seed.split())[:200]
        if snip:
            seo_desc = snip
    seo_desc = seo_desc.replace('"', "'").strip()[:300]

    # Canonical URL — points back at the slug route so all share / search
    # traffic consolidates on one URL even when someone hits the legacy
    # /api/diligence/{id}/report.html form.
    public_base = (DAYCARE_PUBLIC_URL or "https://daycarecheck.agentic-commons.org").rstrip("/")
    canonical_url = (
        f"{public_base}/r/{slug}" if slug
        else f"{public_base}/api/diligence/{task_id}/report.html"
    )

    # JSON-LD ChildCare schema. Each property only included when the
    # underlying data is present; missing fields are dropped rather than
    # filled with defaults that could mislead a search engine.
    pi = primary.get("practical_info") or {}
    ld = {
        "@context": "https://schema.org",
        "@type": "ChildCare",
        "name": daycare_name,
        "url": canonical_url,
    }
    if location:
        ld["address"] = location
    operator_name = (primary.get("ownership_full") or {}).get("direct_owner") or primary.get("headline_owner")
    if operator_name:
        ld["parentOrganization"] = {"@type": "Organization", "name": operator_name}
    if (pi or {}).get("website_url"):
        ld["sameAs"] = [pi["website_url"]]
    import json as _json
    ld_json = _json.dumps(ld, ensure_ascii=False, separators=(",", ":"))

    seo_head = (
        f'<title>{_esc(seo_title)}</title>\n'
        f'<meta name="description" content="{_esc(seo_desc)}">\n'
        f'<link rel="canonical" href="{_esc(canonical_url)}">\n'
        f'<meta name="robots" content="index, follow">\n'
        # Open Graph — controls preview cards on FB / LinkedIn / Slack / iMessage.
        f'<meta property="og:type" content="article">\n'
        f'<meta property="og:title" content="{_esc(seo_title)}">\n'
        f'<meta property="og:description" content="{_esc(seo_desc)}">\n'
        f'<meta property="og:url" content="{_esc(canonical_url)}">\n'
        f'<meta property="og:site_name" content="Daycare Check">\n'
        # Twitter card — same metadata, separate vocabulary.
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{_esc(seo_title)}">\n'
        f'<meta name="twitter:description" content="{_esc(seo_desc)}">\n'
        f'<script type="application/ld+json">{ld_json}</script>\n'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{seo_head}
<style>{REPORT_CSS}</style>
</head>
<body>

<header class="letterhead">
  <div class="brand">Daycare Background Check &middot; Agentic Commons</div>
  <h1 class="doctitle">{_esc(daycare_name)}</h1>
  {loc_html}
  <div class="loc-line">Report generated {_esc(gen_date)}</div>
</header>

{fact_sheet_html}

{exec_html}

<h2 class="section">1. Licensing &amp; Compliance</h2>
{sec1}

<h2 class="section">2. State Inspections</h2>
{sec2}

<h2 class="section">3. Violations &amp; Complaints</h2>
{sec3}

<h2 class="section">4. Documented Incidents</h2>
{sec4}

<h2 class="section">5. Ownership &amp; Corporate Structure</h2>
{sec5}

<h2 class="section">6. Staff &amp; Accreditation Signals</h2>
{sec6}

{_citations_block(citations)}

<section class="report-notes">
  <h2 class="section">About this report</h2>

  <h3>How this report was made</h3>
  <ol>
    <li>You searched this daycare on Background-Check Your Daycare.</li>
    <li>An AI agent on the Agentic Commons network pulled from public
      sources — state licensing portals, court records, mainstream
      press, NAEYC, SEC filings, and Glassdoor — the same data a
      determined parent could find with hours of digging.</li>
    <li>Every claim above is required to link to its original public
      source. Submissions missing per-claim citations are rejected by
      automated QA before reaching you.</li>
  </ol>

  <h3>What this report does NOT cover</h3>
  <ul>
    <li>Sealed family-court records or ongoing investigations not yet public.</li>
    <li>Individual teacher backgrounds — only chain-level or location-level signals.</li>
    <li>Educational quality, curriculum fit, or your child's specific needs.</li>
    <li>A substitute for your own visit, conversation with the director,
      or direct check with your state licensing agency.</li>
  </ul>

  <h3>Disclaimer</h3>
  <p>
    This report combines public records with AI-assisted research,
    generated {_esc(gen_date)}. It may contain errors or omit information
    added after that date. Always verify directly with the daycare and
    your state licensing agency before any childcare decision. This is
    not legal advice.
  </p>

  <h3>No commercial interest</h3>
  <p>
    We accept no ads, operator partnerships, referrals, or fees from
    any daycare, chain, or trade body. All reports are released under
    CC0 1.0 (public domain).
  </p>

  <h3>Errors &amp; inaccuracies</h3>
  <p>
    If you're a daycare operator who believes this report is inaccurate,
    or a parent who spotted something wrong, email
    <a href="mailto:support@agentic-commons.org?subject=Report%20error%3A%20{_esc(alias or task_id)}">support@agentic-commons.org</a>.
    We respond within 5 business days and update or withdraw inaccurate
    claims.
  </p>
</section>

<footer class="provenance">
  <strong>Provenance.</strong> This report was generated by Agentic Commons,
  a public-good agent network. Every claim above is backed by a publicly
  verifiable source cited earlier.<br>
  <strong>Task ID:</strong> {_esc(task_id)}<br>
  <strong>Alias:</strong> {_esc(alias or "—")}<br>
  <strong>Submission marker:</strong> {_esc(marker)}<br>
  <strong>QA score:</strong> {_esc(str(qa_score) if qa_score is not None else "—")}<br>
  <strong>Generated at:</strong> {_esc(gen_date)}<br>
  <strong>Verifiable at:</strong> {ac_alias_link}
</footer>

</body>
</html>
"""


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Daycarecheck Backend", version="0.1")


# Some user networks have a transparent proxy that rewrites client GET
# requests into absolute-URI request-target form (RFC 7230 §5.3.2 — the form
# normally only proxies use). Browsers / Starlette then put the entire
# `http://host:port` prefix into the path, so `/api/diligence/{id}/stream`
# never matches its route. Strip that prefix before routing.
import re as _re  # noqa: E402

_ABS_URI_RE = _re.compile(r"^https?://[^/]+", _re.IGNORECASE)


@app.middleware("http")
async def strip_absolute_uri_prefix(request, call_next):
    raw_path = request.scope.get("raw_path") or b""
    path = request.scope.get("path") or ""
    # Path may have been URL-decoded by Starlette before reaching us; check
    # both the decoded path and the raw bytes for an absolute-URI prefix.
    new_path = None
    m = _ABS_URI_RE.match(path)
    if m:
        new_path = path[m.end():] or "/"
    else:
        try:
            raw_str = raw_path.decode("ascii")
            m2 = _ABS_URI_RE.match(raw_str)
            if m2:
                new_path = raw_str[m2.end():] or "/"
        except UnicodeDecodeError:
            pass
    if new_path is not None:
        request.scope["path"] = new_path
        request.scope["raw_path"] = new_path.encode("ascii")
        log.info("path-rewrite: stripped abs-URI prefix → %s", new_path)
    return await call_next(request)


# CORS: Daycarecheck UI is served by agentic-commons on its dedicated
# subdomain (production) and by the AC vite dev server (local). All other
# origins must hit the same-origin static frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://daycarecheck.agentic-commons.org",
        "http://localhost:5180",
        "http://daycarecheck.localhost:5180",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Idempotency-Key"],
)


class DiligenceRequest(BaseModel):
    daycare_name: str = Field(..., min_length=2, max_length=200)
    location_hint: str | None = Field(None, max_length=200)
    # Required: where to send the finished report. We do not maintain a side
    # store — the email goes into structured_spec.requester_email on the
    # ClawGrid task itself so the webhook handler can read it back when the
    # task finishes (any time in the next 24h).
    email: str = Field(..., min_length=5, max_length=200)
    # Photon-resolved coordinates for the specific location. Required for
    # cache lookup — we only cache reports keyed on (name, lat, lng), so a
    # search without coords (user typed name + Enter) always runs live.
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    # User explicitly clicked "Refresh now" on a cached report. Skip the
    # cache lookup, run a fresh diligence, replace the cached row when
    # this run completes.
    force_refresh: bool = False


class DiligenceCreatedResponse(BaseModel):
    task_id: str
    ac_id: str | None
    alias: str | None
    status: str
    # New: surface whether the task was dispatched immediately (assigned) or
    # is sitting in the pool queue waiting for an idle Manus agent. Lets the
    # frontend show different copy ("Researching now" vs "Waiting for an
    # agent — we'll email you when it starts").
    queued: bool = False
    # Cache hit: when true, this response is referencing a previously-run
    # report (within the 14-day TTL) instead of a freshly-created task.
    # The frontend skips the live SSE / email flow entirely and renders the
    # report immediately, with a "Compiled X days ago · Refresh" badge.
    cached: bool = False
    # ISO timestamp of the cached report's original completion time.
    # Drives the "compiled X days ago" copy. None on cache miss.
    cached_at: str | None = None


@app.post("/api/diligence", response_model=DiligenceCreatedResponse)
async def create_diligence(req: DiligenceRequest):
    """Create a daycare due diligence task on clawgrid. Returns task id + AC alias.

    Before kicking off a new ClawGrid task, look up the per-location
    cache (14-day TTL, GCS-backed). On a fresh hit, reference the
    cached task directly so the frontend can render the report from
    the existing artifact — no agent dispatch, no email, no waiting.
    Misses + force_refresh fall through to the live flow as before.
    """
    # ── Cache lookup ─────────────────────────────────────────────────
    # Cache requires precise lat/lng (the Photon-resolved location). A
    # search without coordinates (user typed name + Enter) is exploratory
    # and always runs live — we don't want to cache "KinderCare" the
    # brand under whatever ambiguous location the agent ended up at.
    if req.lat is not None and req.lng is not None and not req.force_refresh:
        try:
            from cache_service import get_cached
            cached = get_cached(name=req.daycare_name, lat=req.lat, lng=req.lng)
        except Exception:
            log.warning("cache_lookup_failed", exc_info=True)
            cached = None
        if cached:
            cached_task_id = cached.get("task_id")
            # Defence-in-depth: verify the upstream task still exists. A
            # cache row pointing at a deleted ClawGrid task is worse than
            # a miss, so fall through to live in that edge case.
            if cached_task_id:
                check = await cf_request("GET", f"/api/tasks/{cached_task_id}")
                if check.status_code == 200:
                    t = check.json()
                    log.info(
                        "cache_hit task_id=%s age_seconds=%s name=%r lat=%s lng=%s",
                        cached_task_id, cached.get("_cache_age_seconds"),
                        req.daycare_name, req.lat, req.lng,
                    )
                    return DiligenceCreatedResponse(
                        task_id=cached_task_id,
                        ac_id=t.get("ac_id"),
                        alias=t.get("alias"),
                        status=t.get("status") or "completed",
                        queued=False,
                        cached=True,
                        cached_at=cached.get("completed_at"),
                    )
                log.warning(
                    "cache_hit_but_upstream_gone task_id=%s status=%s",
                    cached_task_id, check.status_code,
                )

    loc_clause = f" near {req.location_hint}" if req.location_hint else ""
    brief = f"""TASK: Produce a formal background-check report for the U.S. daycare \
"{req.daycare_name}"{loc_clause}.

OUTPUT LANGUAGE: English only.
OUTPUT FORMAT: A single JSON artifact matching the schema in structured_spec.
Every factual claim (date, name, count, quote, status) MUST include at least
one clickable source URL. Reports without per-claim sources will be rejected
by QA.

REQUIRED SECTIONS:

1. LICENSING — current license status (active / expired / probation /
   revoked), license number, issuing state agency, expiration date,
   any disciplinary actions. Source: state child-care licensing portal
   (e.g., NY OCFS, CA CCLD, TX HHS Child Care Search).

2. STATE INSPECTIONS — date and outcome of the most recent inspection,
   inspection frequency over the past 24 months, plain-English summary
   of any violations cited. Source: state inspection record URL.

3. VIOLATIONS & COMPLAINTS — substantiated violations in the past 36
   months, grouped by category (staff-to-child ratio / supervision /
   health & sanitation / staff background checks / facility safety).
   Note any repeating pattern. Source: state regulator records.

4. DOCUMENTED INCIDENTS — abuse, neglect, injury, runaway, poisoning,
   or fatality incidents on public record, each with date, city/state,
   severity (critical / high / medium), and a one-paragraph plain-
   English summary. Source: news article URL or court docket URL.

5. OWNERSHIP — direct legal owner (LLC) and ultimate parent company,
   owner type (independent / franchise / corporate / private equity /
   publicly traded), franchise affiliation, PE acquisition year if
   applicable. Full ownership chain with one source per level.
   Source: state business registry, SEC EDGAR, press releases.

6. STAFF & ACCREDITATION SIGNALS — Glassdoor / Indeed rating (chain or
   location), estimated turnover or average tenure, NAEYC accreditation
   status, other accreditations (NECPA, state QRIS).
   Source: Glassdoor URL, NAEYC public search.

7. PRACTICAL INFO (rendered as a "fact sheet" at the top of the report,
   NOT as a separate section; this is the "what is this place" identity
   info parents use to first orient themselves):
   - Ages served (range accepted, e.g. "6 weeks to 5 years")
   - Capacity (max children allowed per license; from state portal)
   - Hours of operation (days + open/close times)
   - Pricing estimate (monthly tuition range; note source — operator
     website, Care.com, or user reviews; note confidence — high/medium/low)
   - Recent parent sentiment (1-2 sentences summarizing top themes in
     the last 12 months of Yelp/Google/Reddit reviews, with review
     period, sample size, and average rating)
   - Operator's main website URL
   - The operator website's og:image (or its main hero photo URL) — used
     to show a small thumbnail at the top of the report. Must be a
     direct image URL (https://, ending in .jpg / .png / .webp), not
     an image search results page.
   - Google Maps URL for this specific location (search-style URL is
     fine, e.g. https://www.google.com/maps/place/...)
   Source: operator's own website, Care.com, ChildCareAware, Google
   Maps, Yelp, Google reviews, state licensing portal capacity field.

EXECUTIVE SUMMARY: At the top, include a 2-3 sentence plain-English
summary focused on the single most important thing a parent should
know about this daycare.

SOURCE QUALITY: For sections 1-6 use Tier 1-3 sources only (state
regulators, court records, mainstream news, SEC filings, official
trade-body lookups). For section 7 (Practical Info), operator-claimed
and user-review sources are acceptable but must be clearly labeled with
the source and a confidence level. Avoid forums and parent blogs as
primary evidence for sections 1-6.

MISSING DATA: If a section truly has no public data (e.g., a brand-new
center with no inspection history), state "No public records found"
explicitly and set the section's `data_available` to false. Do NOT
fabricate or extrapolate.

SUBMISSION MARKER: Include the assigned submission marker in the
artifact as a bare token (format: sm_xxxxxxxx). Do not wrap it in
a URL.
"""

    body = {
        "title": f"Daycare background check: {req.daycare_name}",
        "natural_language_desc": brief,
        "task_type": "custom",
        "task_kind": "public_good",
        "public_good_project_id": PROJECT_ID,
        "budget_max": "0",
        "publisher_agent_id": CLIENT_AGENT,
        # tag_pool routing: ClawGrid selects from agents carrying
        # manus_qualified EntityTag, queues if pool empty (24h TTL), and
        # auto-failovers if the assigned agent goes offline before working.
        "routing_mode": "tag_pool",
        "required_capability_keys": CAPABILITY_KEYS,
    }
    # Tell ClawGrid where to POST status-change webhooks (task.assigned,
    # task.completed, task.cancelled+reason). Only set when we know our
    # public URL — local dev / port-forward scenarios get no webhooks and
    # rely on the SSE stream for status.
    # Webhook target must skip the agentic-commons proxy hop — ClawGrid
    # internal callbacks go straight to the Cloud Run backend.
    body["webhook_url"] = f"{DAYCARE_WEBHOOK_URL.rstrip('/')}/api/webhook/task-status"
    body.update({
        "structured_spec": {
            "internal_subtype": "daycare_background_check",
            "daycare_name": req.daycare_name,
            "location_hint": req.location_hint,
            # Stash the requester's email on the task so the webhook handler
            # can email them when the task finishes (or expires after 24h).
            # No side database needed — the task IS our persistence.
            "requester_email": req.email,
            # Photon-resolved coordinates, persisted so the task.completed
            # webhook handler can compute the cache key and write the row
            # without recomputing or re-fetching anything.
            "requester_lat": req.lat,
            "requester_lng": req.lng,
            "report_dimensions": [
                "licensing", "inspections", "violations",
                "incidents", "ownership", "staff_signals",
                "practical_info",
            ],
            "required_sources_per_claim": 1,
            "required_source_tier": "T1-T3",
            "output_language": "en",
            "output_format": "formal_json_report",
            "output_schema": {
                "daycare_name": "string",
                "location": "string|null",
                "executive_summary": "string (2-3 sentences, plain English)",
                "licensing": {
                    "status": "active|expired|probation|revoked|unknown",
                    "license_number": "string|null",
                    "state_agency": "string|null",
                    "expires_date": "ISO date|null",
                    "disciplinary_actions": [
                        {"date": "string", "type": "string",
                         "summary": "string", "source_url": "string"}
                    ],
                    "source_url": "string",
                    "data_available": "boolean",
                },
                "inspections": {
                    "last_inspection_date": "ISO date|null",
                    "frequency_months_24mo": "number|null",
                    "outcome": "clean|corrections|violations|unknown",
                    "violations_summary_plain_english": "string",
                    "source_url": "string",
                    "data_available": "boolean",
                },
                "violations": {
                    "total_substantiated_36mo": "number",
                    "by_category": {
                        "ratio": "number", "supervision": "number",
                        "health_sanitation": "number",
                        "background_checks": "number",
                        "facility_safety": "number",
                    },
                    "repeat_pattern_note": "string|null",
                    "source_url": "string",
                    "data_available": "boolean",
                },
                "incidents": [
                    {"date": "string", "location": "string",
                     "severity": "critical|high|medium",
                     "summary": "string", "source_url": "string"}
                ],
                "ownership": {
                    "direct_owner": "string",
                    "ultimate_parent": "string|null",
                    "owner_type": "independent|franchise|corporate|private_equity|publicly_traded",
                    "franchise_of": "string|null",
                    "pe_acquisition_year": "number|null",
                    "ownership_chain": [
                        {"level": "number", "entity": "string",
                         "role": "string", "source_url": "string"}
                    ],
                    "source_url": "string",
                },
                "staff_signals": {
                    "glassdoor_rating": "number|null",
                    "glassdoor_url": "string|null",
                    "estimated_turnover_pct": "number|null",
                    "naeyc_accredited": "boolean|null",
                    "naeyc_source_url": "string|null",
                    "other_accreditations": ["string"],
                    "data_available": "boolean",
                },
                "practical_info": {
                    "ages_served": "string|null (e.g. '6 weeks to 5 years')",
                    "capacity": "number|null",
                    "hours_of_operation": "string|null (e.g. 'Mon-Fri 7:00am-6:00pm')",
                    "pricing": {
                        "estimate": "string|null (e.g. '$2,200-2,800/month' or 'not publicly listed')",
                        "source": "string|null (e.g. 'operator website' / 'Care.com listing' / 'estimate from 5 Yelp/Google reviews 2024-2025')",
                        "confidence": "high|medium|low|unknown",
                        "source_url": "string|null",
                    },
                    "recent_parent_sentiment": {
                        "summary": "string (1-2 sentence theme summary from last 12 months of reviews)",
                        "review_period": "string|null (e.g. '2024-01 to 2025-06')",
                        "sample_size": "number|null",
                        "rating_avg": "number|null",
                        "source_urls": ["string"],
                    },
                    "website_url": "string|null (operator main website URL)",
                    "photo_url": "string|null (direct image URL — og:image or hero photo from operator website)",
                    "google_maps_url": "string|null (Google Maps URL for this specific location)",
                    "data_available": "boolean",
                },
            },
        },
    })

    # Idempotency key must be ASCII-safe (HTTP header constraint).
    # Use SHA1 of the normalized name → hex; daycare names with non-ASCII chars
    # (e.g. "Crème de la Crème") would otherwise crash before reaching clawgrid.
    import hashlib
    normalized = req.daycare_name.strip().lower()
    name_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    idem = f"daycare-{name_hash}-{int(time.time() // 3600)}"
    headers = {"X-Idempotency-Key": idem}

    r = await cf_request("POST", "/api/tasks", json=body, headers=headers)
    if r.status_code != 201:
        log.error("clawgrid task create failed %s: %s", r.status_code, r.text[:400])
        raise HTTPException(r.status_code, detail=r.text[:400])

    j = r.json()
    # tag_pool tasks land in either `assigned` (idle agent found at create
    # time) or `queued` (pool empty — sweep worker will retry every minute,
    # 24h TTL). Surface this so the frontend can show different copy.
    _status = j.get("status") or ""
    return DiligenceCreatedResponse(
        task_id=j["id"],
        ac_id=j.get("ac_id"),
        alias=j.get("alias"),
        status=_status,
        queued=(_status == "queued"),
    )


async def _auto_approve_if_ready(task_id: str) -> dict | None:
    """If task is in pending_acceptance with full QA, approve it."""
    r = await cf_request("GET", f"/api/tasks/{task_id}")
    if r.status_code != 200:
        return None
    t = r.json()
    if t.get("status") == "pending_acceptance" and t.get("qa_final") and (t.get("quality_score") or 0) >= 80:
        log.info("auto-approve task=%s score=%s", task_id, t.get("quality_score"))
        rr = await cf_request("POST", f"/api/tasks/{task_id}/review",
                              json={"action": "approve", "reason": "auto-accept by daycarecheck backend (QA score >= 80)"})
        if rr.status_code == 200:
            return rr.json()
    return t


@app.get("/api/diligence/{task_id}/stream")
async def stream_diligence(task_id: str):
    """Server-Sent Events: poll clawgrid every 3s, push status changes."""

    async def gen():
        last_status = ""
        consecutive_lookup_failures = 0
        # Heartbeat keep-alive: also send empty comment events every iteration
        # so EventSource stays open even when backend is silently polling.
        for _ in range(600):  # 30 min cap (was 120 = 6 min, too short for busy lobsters)
            try:
                t = await _auto_approve_if_ready(task_id)
                if not t:
                    # Tolerate transient upstream blips (5xx during clawgrid
                    # rollouts, brief network hiccups). Only surface error after
                    # ~30s of consecutive failures.
                    consecutive_lookup_failures += 1
                    if consecutive_lookup_failures >= 10:
                        yield f"data: {json.dumps({'error': 'task lookup failed'})}\n\n"
                        return
                    yield ": lookup-retry\n\n"
                    await asyncio.sleep(3)
                    continue
                consecutive_lookup_failures = 0

                payload = {
                    "task_id": task_id,
                    "status": t.get("status"),
                    "qa_final": t.get("qa_final"),
                    "score": t.get("quality_score"),
                    "alias": t.get("alias"),
                    "ac_id": t.get("ac_id"),
                    # ClawGrid populates assignee name on the task itself
                    # when an agent is chosen (tag_pool dispatch); no need
                    # to lookup against a hardcoded display map anymore.
                    "assignee_display": _assignee_display(t),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }

                if payload["status"] != last_status:
                    yield f"event: status\ndata: {json.dumps(payload)}\n\n"
                    last_status = payload["status"]
                else:
                    # SSE comment as heartbeat — keeps connection alive in proxies
                    yield ": ping\n\n"

                if payload["status"] in ("completed", "failed", "cancelled", "partial_completed"):
                    # final: include normalized artifact
                    art_r = await cf_request("GET", f"/api/tasks/{task_id}/artifacts")
                    raw_art = art_r.json()[0] if art_r.status_code == 200 and art_r.json() else None
                    final = {**payload, "artifact": _normalize_artifact(raw_art)}
                    yield f"event: final\ndata: {json.dumps(final)}\n\n"
                    return

                await asyncio.sleep(3)
            except Exception as e:
                log.exception("stream error")
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                return
        # timeout
        yield f"event: timeout\ndata: {json.dumps({'task_id': task_id})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/diligence/{task_id}")
async def get_diligence(task_id: str):
    """One-shot fetch: current task state + artifact if available."""
    t = await _auto_approve_if_ready(task_id)
    if not t:
        raise HTTPException(404, detail="task not found")

    artifact = None
    if t.get("status") in ("completed", "pending_acceptance"):
        art_r = await cf_request("GET", f"/api/tasks/{task_id}/artifacts")
        if art_r.status_code == 200 and art_r.json():
            artifact = _normalize_artifact(art_r.json()[0])

    return {
        "task_id": task_id,
        "status": t.get("status"),
        "score": t.get("quality_score"),
        "alias": t.get("alias"),
        "ac_id": t.get("ac_id"),
        "assignee_display": _assignee_display(t),
        "artifact": artifact,
    }


async def _fetch_report_html(task_id: str) -> tuple[str, str]:
    """Fetch task + artifact, render formal HTML report.

    Returns (html_string, safe_filename_slug).
    Raises HTTPException if the task or artifact isn't ready.
    """
    t = await _auto_approve_if_ready(task_id)
    if not t:
        raise HTTPException(404, detail="task not found")
    if t.get("status") not in ("completed", "pending_acceptance"):
        raise HTTPException(409, detail=f"report not ready (status={t.get('status')})")

    art_r = await cf_request("GET", f"/api/tasks/{task_id}/artifacts")
    if art_r.status_code != 200 or not art_r.json():
        raise HTTPException(404, detail="artifact not found")

    normalized = _normalize_artifact(art_r.json()[0])
    if not normalized:
        raise HTTPException(500, detail="artifact normalization failed")

    # Find the slug for this task by looking up the cache key derived from
    # name+coords (we cached it on completion). Best-effort — if it's not
    # in the cache (e.g. legacy task without lat/lng) the report falls
    # back to the UUID URL as canonical.
    spec = t.get("structured_spec") or {}
    slug_for_canonical: str | None = None
    try:
        from cache_service import get_cached
        lat = spec.get("requester_lat")
        lng = spec.get("requester_lng")
        primary_for_lookup = normalized.get("primary") or {}
        cache_name = primary_for_lookup.get("daycare_name") or spec.get("daycare_name")
        if cache_name and isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            row = get_cached(name=cache_name, lat=float(lat), lng=float(lng))
            if row:
                slug_for_canonical = row.get("slug")
    except Exception:
        log.warning(
            "slug_lookup_for_canonical_failed task_id=%s", task_id, exc_info=True,
        )

    html = render_report_html(
        normalized,
        task_id=task_id,
        alias=t.get("alias"),
        qa_score=t.get("quality_score"),
        slug=slug_for_canonical,
    )

    # Build safe filename slug from daycare name
    primary = normalized.get("primary") or {}
    name = (primary.get("daycare_name") or "report").strip().lower()
    slug = "".join(c if c.isalnum() else "-" for c in name).strip("-")[:60] or "report"
    return html, slug


@app.get("/api/diligence/{task_id}/report.html", response_class=HTMLResponse)
async def view_report(task_id: str):
    """Inline HTML report — opens in browser tab."""
    html, _ = await _fetch_report_html(task_id)
    return HTMLResponse(content=html)


@app.get("/api/diligence/{task_id}/download")
async def download_report(task_id: str):
    """Forced-download PDF report (rendered server-side from HTML)."""
    from fastapi.responses import Response
    html, slug = await _fetch_report_html(task_id)
    # WeasyPrint converts our self-contained HTML+CSS template to PDF.
    # Heavy import — keep it lazy so test/dev paths that don't hit /download
    # don't pay the load cost.
    from weasyprint import HTML as _WeasyHTML  # noqa: WPS433
    pdf_bytes = _WeasyHTML(string=html).write_pdf()
    filename = f"daycare-background-check-{slug}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ── ClawGrid task-status webhook ────────────────────────────────────────────
# ClawGrid POSTs here when one of our submitted tasks changes status. Payload
# shape (see clawforce _fire_webhook):
#   {"task_id": "...", "status": "completed", "event": "task.completed",
#    "reason": "tag_pool_queue_expired"  # optional, present on cancel/expire
#   }
#
# We turn the two interesting transitions into transactional emails to the
# requester (whose address is stashed on structured_spec.requester_email):
#   - task.completed             → "your report is ready"
#   - task.cancelled + reason=tag_pool_queue_expired → "no agent available"
#
# All other status changes are ignored. We always return 200 so ClawGrid
# doesn't retry — failed email is logged but not surfaced as a webhook
# failure (the user can still hit the permalink directly).


@app.post("/api/webhook/task-status")
async def task_status_webhook(req: Request):
    """Receive a ClawGrid task status webhook and dispatch the right email."""
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    task_id = payload.get("task_id")
    event   = payload.get("event") or ""
    reason  = payload.get("reason") or ""
    if not task_id or not event:
        return JSONResponse({"ok": False, "error": "missing_fields"}, status_code=400)

    # We only care about two events. Anything else (task.assigned, etc.) is
    # a no-op for the email pipeline but still returns 200.
    is_completed   = event == "task.completed"
    is_expired     = event == "task.cancelled" and reason == "tag_pool_queue_expired"
    if not (is_completed or is_expired):
        return JSONResponse({"ok": True, "ignored": event})

    # Fetch the task to read requester_email + daycare_name from
    # structured_spec. The webhook payload deliberately doesn't include
    # them — keeping ClawGrid free of project-specific fields.
    try:
        r = await cf_request("GET", f"/api/tasks/{task_id}")
        if r.status_code != 200:
            log.warning(
                "webhook_task_fetch_failed task_id=%s status=%s",
                task_id, r.status_code,
            )
            return JSONResponse({"ok": True, "skipped": "task_fetch_failed"})
        t = r.json()
    except Exception:
        log.exception("webhook_task_fetch_exception task_id=%s", task_id)
        return JSONResponse({"ok": True, "skipped": "task_fetch_exception"})

    spec = t.get("structured_spec") or {}
    email = (spec.get("requester_email") or "").strip()
    daycare_name = (spec.get("daycare_name") or t.get("title") or "your daycare").strip()
    if not email:
        log.info("webhook_no_email_on_task task_id=%s event=%s", task_id, event)
        return JSONResponse({"ok": True, "skipped": "no_email"})

    from mail_service import (
        send_no_agent_email,
        send_report_ready_email,
    )

    if is_completed:
        # Cache write — runs before the email so a slow MailerSend send
        # never delays the cache being populated. Only write if the spec
        # has Photon coordinates (otherwise we don't have a stable key).
        # Also derives city/state from either the location_hint string or
        # the artifact's practical_info; persisted so /r/{slug} pages can
        # render rich meta tags without a second ClawGrid lookup.
        lat = spec.get("requester_lat")
        lng = spec.get("requester_lng")
        slug_for_email: str | None = None
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            try:
                # Best-effort city/state extraction. location_hint comes from
                # Photon ("123 Main St, Brooklyn, New York") so the last two
                # comma-separated parts are usually city + state. If the
                # artifact carries a structured location string we prefer
                # that. Failures are non-fatal — slug still works without.
                city, state = _extract_city_state(spec, t)
                from cache_service import put_cached
                slug_for_email = put_cached(
                    name=daycare_name,
                    lat=float(lat), lng=float(lng),
                    task_id=task_id,
                    completed_at_iso=(
                        t.get("completed_at")
                        or t.get("updated_at")
                        or datetime.now(timezone.utc).isoformat()
                    ),
                    city=city,
                    state=state,
                )
            except Exception:
                log.warning(
                    "cache_write_failed task_id=%s", task_id, exc_info=True,
                )

        # Prefer the slug URL in the email so users sharing the link in
        # text / chat / social get something readable instead of a UUID.
        # Fallback to the legacy task_id URL if slug unavailable.
        if slug_for_email:
            report_url = (
                DAYCARE_PUBLIC_URL.rstrip("/") + f"/r/{slug_for_email}"
            )
        else:
            report_url = REPORT_URL_TEMPLATE.format(task_id=task_id)
        result = await send_report_ready_email(
            to_email=email, daycare_name=daycare_name, report_url=report_url,
        )
        log.info(
            "webhook_email_sent_completed task_id=%s to=%s success=%s skip=%s",
            task_id, email, result.success, result.skipped_reason,
        )
    else:  # is_expired
        retry_url = DAYCARE_PUBLIC_URL.rstrip("/") + "/"
        result = await send_no_agent_email(
            to_email=email, daycare_name=daycare_name, retry_url=retry_url,
        )
        log.info(
            "webhook_email_sent_expired task_id=%s to=%s success=%s skip=%s",
            task_id, email, result.success, result.skipped_reason,
        )

    return JSONResponse({"ok": True})


# ── Pageview / UV telemetry ────────────────────────────────────────────────
# Lightweight self-instrumented analytics. Frontend Daycarecheck.tsx fires
# one POST /api/pv on mount + on SPA route changes.
#
# What we record (structured log line, no DB row):
#   event=pageview
#   visitor_hash   16-char sha256 of (ip, user-agent, UTC-date) — same
#                  visitor on the same day hashes to the same value, so
#                  COUNT(DISTINCT visitor_hash) per day = UV
#   path           the SPA path string (e.g. "/", "/legal")
#   referrer       document.referrer (clamped to 200 chars)
#   country        from Cloudflare/GCLB header if present, else null
#
# What we DON'T record: raw IP, raw UA, email, daycare name, anything
# that links a pageview back to an individual. GDPR-friendly.
#
# Querying later (Cloud Logging):
#   gcloud run services logs read daycarecheck --region us-central1 \
#     --format json | jq '.[]|select(.textPayload|contains("pageview_recorded"))'
#   For UV count: parse visitor_hash, sort -u | wc -l.
#   Long-term: set up a Cloud Logging sink → BigQuery for SQL.


class PageViewRequest(BaseModel):
    path: str = Field(..., max_length=200)
    referrer: str | None = Field(None, max_length=200)


@app.post("/api/pv")
async def page_view(req: Request, body: PageViewRequest):
    """Record one pageview into Cloud Logging.

    Always returns 200 quickly — telemetry must never block UX. Errors
    are swallowed and logged as warnings; the user's session keeps
    flowing regardless.
    """
    try:
        import hashlib
        ip = (req.client.host if req.client else "") or ""
        ua = req.headers.get("user-agent") or ""
        # Daily UV bucket — same visitor visiting twice in the same UTC
        # day hashes the same. Different day → different hash.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        visitor_hash = hashlib.sha256(f"{ip}|{ua}|{day}".encode("utf-8")).hexdigest()[:16]

        # Optional country header set by Cloudflare / GCLB. We're behind
        # GCLB so today these are usually absent — left here for future
        # geo-aware reporting if we ever proxy through Cloudflare.
        country = (
            req.headers.get("cf-ipcountry")
            or req.headers.get("x-appengine-country")
            or None
        )

        # Structured log line so a Cloud Logging filter can pick it out:
        #   resource.type=cloud_run_revision AND textPayload:"pageview_recorded"
        log.info(
            "pageview_recorded path=%r referrer=%r visitor_hash=%s country=%s ua_short=%r day=%s",
            body.path[:200],
            (body.referrer or "")[:200],
            visitor_hash,
            country or "-",
            ua[:80],
            day,
        )
    except Exception:
        # Telemetry should never break a page load. Swallow + warn.
        log.warning("pageview_record_failed", exc_info=True)
    return JSONResponse({"ok": True})


# ── SEO: slug-based public report URLs ─────────────────────────────────────
# /r/{slug} is the canonical public URL for a completed report (used in
# emails + social shares). Friendly slugs like
# /r/cadence-academy-preschool-portland-or-a1b2 → looked up in the GCS
# slug index → resolves to the underlying task_id → renders the same HTML
# as /api/diligence/{task_id}/report.html.
#
# Why a separate route instead of redirecting to the legacy URL:
#   - Search engines treat the slug URL as the canonical (it's what's in
#     the sitemap + the meta canonical tag); a 302 to a UUID URL leaks
#     ranking value to the UUID form.
#   - Sharing on Reddit / FB / Slack shows the slug URL preview directly,
#     which carries semantic info ("oh that's the Cadence Academy report").


@app.get("/r/{slug}", response_class=HTMLResponse)
async def view_report_by_slug(slug: str):
    """SEO-friendly public report URL. Looks up slug in cache index,
    renders the same HTML as the legacy /api/diligence/{id}/report.html
    endpoint with a canonical link back to itself."""
    try:
        from cache_service import get_by_slug
        row = get_by_slug(slug)
    except Exception:
        log.warning("slug_lookup_failed slug=%s", slug, exc_info=True)
        row = None
    if not row or not row.get("task_id"):
        raise HTTPException(404, detail="report not found")
    html, _ = await _fetch_report_html(row["task_id"])
    return HTMLResponse(content=html)


@app.get("/sitemap.xml")
async def sitemap_xml():
    """sitemap.xml — homepage + every cached report. Updates automatically
    as the GCS cache evolves; stale entries (>14 days) are filtered out
    of the listing inside ``list_all_slugs``."""
    base = DAYCARE_PUBLIC_URL.rstrip("/")
    try:
        from cache_service import list_all_slugs
        rows = list_all_slugs()
    except Exception:
        log.warning("sitemap_list_failed", exc_info=True)
        rows = []

    # Plain ASCII generation — sitemap.xml goes to robots so we keep it
    # tiny and predictable (no f-string template escaping pitfalls).
    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts.append(
        f"<url><loc>{base}/</loc><lastmod>{today}</lastmod>"
        "<changefreq>daily</changefreq><priority>1.0</priority></url>"
    )
    parts.append(
        f"<url><loc>{base}/legal.html</loc><changefreq>monthly</changefreq>"
        "<priority>0.3</priority></url>"
    )
    for r in rows:
        slug = r.get("slug") or ""
        if not slug:
            continue
        last = (r.get("completed_at") or "")[:10] or today
        parts.append(
            f"<url><loc>{base}/r/{slug}</loc>"
            f"<lastmod>{last}</lastmod>"
            "<changefreq>weekly</changefreq><priority>0.8</priority></url>"
        )
    parts.append("</urlset>")
    return Response(
        content="\n".join(parts),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/llms.txt", response_class=HTMLResponse)
async def llms_txt():
    """LLM-friendly markdown index per the emerging llms.txt convention.

    Tells LLM crawlers (GPTBot, ClaudeBot, PerplexityBot, etc.) what this
    site is, what its key URLs look like, and lists currently-cached
    reports so they can pull samples for grounding. Plain markdown so any
    LLM can parse it without rendering JS.

    Refreshed dynamically: the report-list section reflects whatever's in
    the cache RIGHT NOW (14-day TTL), so newly-completed reports show up
    on the next crawl without us touching anything.
    """
    base = DAYCARE_PUBLIC_URL.rstrip("/")
    try:
        from cache_service import list_all_slugs
        rows = list_all_slugs()
    except Exception:
        log.warning("llms_list_failed", exc_info=True)
        rows = []

    lines = [
        "# Daycare Check",
        "",
        "> Free public-record background checks on any U.S. daycare. "
        "Pulls licensing, inspection violations, ownership chain, and "
        "incident history from authoritative public sources (state "
        "regulators, federal court records, SEC EDGAR, NAEYC, mainstream "
        "press) into a single cited report. CC0 public domain.",
        "",
        "## What this site does",
        "",
        "- A user enters a U.S. daycare name + their email.",
        "- An AI agent on the Agentic Commons public-good network pulls "
        "every public record on that daycare.",
        "- A formal HTML report is generated with every claim linked to "
        "its original source.",
        "- The report is cached for 14 days and emailed to the user.",
        "",
        "## Key URLs",
        "",
        f"- {base}/ — homepage + search form",
        f"- {base}/r/{{slug}} — permalink to a completed report",
        f"- {base}/sitemap.xml — full URL index of cached reports",
        f"- {base}/robots.txt — crawler policy (all major LLM bots allowed)",
        "",
        "## Data sources we pull from",
        "",
        "- 50 U.S. state child-care licensing portals",
        "- Federal court records (PACER, DoJ press releases)",
        "- SEC EDGAR (for publicly-held daycare operators)",
        "- NAEYC accreditation database",
        "- Library of Congress / Congressional Research Service",
        "- U.S. Senate investigations (Sen. Merkley's daycare probes)",
        "- Mainstream press archives",
        "",
        "## How agents should use this site",
        "",
        "Each report is a self-contained, cited document. Citations point "
        "to primary sources; treat those sources as the authoritative "
        "data, not our summary. To check whether a specific daycare has "
        "been reported on, scan the sitemap or the report list below.",
        "",
        "## Recent reports",
        "",
    ]
    if rows:
        for r in rows[:50]:
            slug = r.get("slug")
            name = r.get("daycare_name") or slug
            if slug:
                lines.append(f"- [{name}]({base}/r/{slug})")
    else:
        lines.append("(no cached reports right now — submit a daycare on "
                     "the homepage to generate one)")
    lines.append("")
    lines.append("## Project")
    lines.append("")
    lines.append(
        "Daycare Check is a public-good prototype on the Agentic Commons "
        "network (https://agentic-commons.org). The agents that produce "
        "these reports volunteer their compute time; nothing is paid, no "
        "data is sold, no user accounts exist."
    )
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/robots.txt", response_class=HTMLResponse)
async def robots_txt():
    """robots.txt — allow crawlers everywhere except /api/*, point at the
    sitemap. Mounting at the FastAPI route layer (not StaticFiles) so it
    can use DAYCARE_PUBLIC_URL even when behind a proxy."""
    base = DAYCARE_PUBLIC_URL.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /r/\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
        # llms.txt is the emerging convention for LLM-friendly site indexes.
        # Not part of the original robots.txt RFC but supported by some
        # bots and useful as a discoverability hint.
        f"# LLM index: {base}/llms.txt\n"
    )
    return Response(
        content=body,
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Static frontend ──────────────────────────────────────────────────────────
# Mount last so /api/* takes precedence

app.mount("/", StaticFiles(directory=str(STATIC_ROOT), html=True), name="static")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8766"))
    log.info("starting daycarecheck backend on :%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
