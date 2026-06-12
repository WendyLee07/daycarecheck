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
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
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

CONFIG_DIR = Path.home() / ".config" / "clawforce"
PRIV_KEY_PATH = CONFIG_DIR / "jwt_private_key.pem"
KID_PATH      = CONFIG_DIR / "jwt_kid"

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


def _normalize_artifact(artifact: dict | None) -> dict | None:
    if not artifact:
        return None

    items_raw = (artifact.get("data") or {}).get("items") or []
    items = [_normalize_item(it) for it in items_raw]

    return {
        "submission_marker": artifact.get("submission_marker"),
        "qa_score": artifact.get("qa_score"),
        "qa_final": artifact.get("qa_final"),
        "items": items,
        "primary": items[0] if items else None,
        "raw": artifact,  # frontend can ignore but keep for debugging
    }

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Daycarecheck Backend", version="0.1")


class DiligenceRequest(BaseModel):
    daycare_name: str = Field(..., min_length=2, max_length=200)
    location_hint: str | None = Field(None, max_length=200)


class DiligenceCreatedResponse(BaseModel):
    task_id: str
    ac_id: str | None
    alias: str | None
    status: str


@app.post("/api/diligence", response_model=DiligenceCreatedResponse)
async def create_diligence(req: DiligenceRequest):
    """Create a daycare due diligence task on clawgrid. Returns task id + AC alias."""
    body = {
        "title": f"Daycare due diligence: {req.daycare_name}",
        "natural_language_desc": (
            f"Identify the parent company and ultimate owner of the U.S. daycare "
            f"'{req.daycare_name}'"
            f"{f' near {req.location_hint}' if req.location_hint else ''}. "
            "Provide at least one publicly verifiable URL source for each ownership claim."
        ),
        "task_type": "custom",
        "task_kind": "public_good",
        "public_good_project_id": PROJECT_ID,
        "budget_max": "0",
        "publisher_agent_id": CLIENT_AGENT,
        "structured_spec": {
            "internal_subtype": "daycare_due_diligence",
            "daycare_name": req.daycare_name,
            "location_hint": req.location_hint,
            "report_dimensions": ["ownership"],
            "required_sources_min": 1,
            "required_source_tier": "T1-T3",
        },
    }

    idem = f"daycare-{req.daycare_name.lower().replace(' ', '-')[:60]}-{int(time.time() // 3600)}"
    headers = {"X-Idempotency-Key": idem}

    r = await cf_request("POST", "/api/tasks", json=body, headers=headers)
    if r.status_code != 201:
        log.error("clawgrid task create failed %s: %s", r.status_code, r.text[:400])
        raise HTTPException(r.status_code, detail=r.text[:400])

    j = r.json()
    return DiligenceCreatedResponse(
        task_id=j["id"], ac_id=j.get("ac_id"), alias=j.get("alias"), status=j["status"]
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
        # Heartbeat keep-alive: also send empty comment events every iteration
        # so EventSource stays open even when backend is silently polling.
        for _ in range(600):  # 30 min cap (was 120 = 6 min, too short for busy lobsters)
            try:
                t = await _auto_approve_if_ready(task_id)
                if not t:
                    yield f"data: {json.dumps({'error': 'task lookup failed'})}\n\n"
                    return

                payload = {
                    "task_id": task_id,
                    "status": t.get("status"),
                    "qa_final": t.get("qa_final"),
                    "score": t.get("quality_score"),
                    "alias": t.get("alias"),
                    "ac_id": t.get("ac_id"),
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
        "artifact": artifact,
    }


# ── Static frontend ──────────────────────────────────────────────────────────
# Mount last so /api/* takes precedence

app.mount("/", StaticFiles(directory=str(STATIC_ROOT), html=True), name="static")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8766"))
    log.info("starting daycarecheck backend on :%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
