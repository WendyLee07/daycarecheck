#!/usr/bin/env python3
"""
Minimal example: create a daycare-due-diligence task on ClawForce via API.

Replaces the fetcher pattern (auto-injection from a data source) with
on-demand task creation triggered by user search.

Usage:
    export CLAWFORCE_BASE=https://clawgrid.ai
    export CLAWFORCE_JWT=<admin or user JWT>
    export CLAWFORCE_CLIENT_AGENT_ID=<client agent uuid>

    python3 create_task.py "Cadence Education" --location "Park Slope, Brooklyn"
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid
from typing import Any


BASE = os.environ.get("CLAWFORCE_BASE", "https://clawgrid.ai")
JWT = os.environ.get("CLAWFORCE_JWT", "")
CLIENT_AGENT_ID = os.environ.get("CLAWFORCE_CLIENT_AGENT_ID", "")

# Task type registered in clawforce.task_type_configs.
# If not yet registered, run the SQL in setup_clawforce_side.md first.
TASK_TYPE = "daycare_due_diligence"


def _http(method: str, path: str, body: dict | None = None, extra_headers: dict | None = None) -> dict:
    """Tiny urllib wrapper that talks JSON to ClawForce."""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None

    headers = {
        "Authorization": f"Bearer {JWT}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"!! HTTP {e.code} from {method} {path}", file=sys.stderr)
        print(f"   body: {body_text[:500]}", file=sys.stderr)
        raise


def register_client_agent_once(name: str = "daycarecheck-frontend") -> str:
    """One-time setup: register a client agent if you don't have one yet.

    Returns agent id. Subsequent task creation needs this id in X-Agent-Id.
    Idempotent: if you already have one, just set CLAWFORCE_CLIENT_AGENT_ID env var.
    """
    payload = {"name": name, "agent_type": "client"}
    resp = _http("POST", "/api/agents", body=payload)
    print(f"✓ registered client agent: {resp.get('id')} (name={name})")
    return resp["id"]


def create_task(daycare_name: str, location_hint: str | None = None) -> dict[str, Any]:
    """The core thing this script demonstrates: POST /api/tasks.

    This is the replacement for fetcher-style auto-injection. Every user
    search on the daycare site triggers exactly one of these.
    """
    if not CLIENT_AGENT_ID:
        sys.exit("CLAWFORCE_CLIENT_AGENT_ID env var is required. Run register_client_agent_once first.")

    # Idempotency: same daycare+location+date should not create duplicate tasks.
    # Adjust the salt depending on how often you want re-runs (per-day / per-week / never).
    idempotency_key = f"daycare-{daycare_name}-{location_hint or 'none'}-{time.strftime('%Y%m%d')}"

    payload = {
        "title": f"Daycare due diligence: {daycare_name}",
        "natural_language_desc": (
            f"Research the U.S. daycare '{daycare_name}'"
            f"{f' near {location_hint}' if location_hint else ''}. "
            "Identify the operating brand, parent company, and ultimate owner. "
            "Provide at least one publicly verifiable URL source for each claim."
        ),
        "task_type": TASK_TYPE,
        "budget_max": "0.05",  # USD; tweak per real lobster pricing
        "publisher_agent_id": CLIENT_AGENT_ID,  # in body, not header (per impl in tasks.py:462)
        "structured_spec": {
            "daycare_name": daycare_name,
            "location_hint": location_hint,
            "report_dimensions": ["ownership"],  # MVP-tiny: ownership only
            "required_sources_min": 1,
            "required_source_tier": "T1-T3",  # gov / mainstream media / industry watchdog
            "marker_required": True,
        },
        "tool_constraints": {
            "allowed_tools": ["web_fetch", "web_search"],
            "denied_tools": [],
            "total_tool_calls_limit": 10,
            "enforcement": "strict",
            "on_insufficient": "abandon",
        },
    }

    headers = {
        "X-Idempotency-Key": idempotency_key,
    }

    resp = _http("POST", "/api/tasks", body=payload, extra_headers=headers)
    return resp


def main():
    parser = argparse.ArgumentParser(description="Create a daycare-due-diligence task on ClawForce")
    parser.add_argument("name", help="Daycare or chain name, e.g. 'Cadence Education'")
    parser.add_argument("--location", default=None, help="Optional location hint, e.g. 'Park Slope, Brooklyn'")
    parser.add_argument("--register-agent", action="store_true", help="One-time client-agent registration helper")
    args = parser.parse_args()

    if not JWT:
        sys.exit("CLAWFORCE_JWT env var is required.")

    if args.register_agent:
        agent_id = register_client_agent_once()
        print(f"\nNow set:  export CLAWFORCE_CLIENT_AGENT_ID={agent_id}")
        return

    task = create_task(args.name, location_hint=args.location)

    print("\n=== task created ===")
    print(json.dumps(task, indent=2))
    print()
    print(f"task_id : {task.get('id')}")
    print(f"ac_id   : {task.get('ac_id')}  (alias: {task.get('alias') or 'n/a'})")
    print(f"status  : {task.get('status')}  (likely 'draft' — needs further state-machine pushes)")
    print()
    print("Next manual step (per INTEGRATION_CONTRACT.md): submit_for_review → escrow → queued → assigned.")
    print("Or use /api/tasks/{id}/auto-publish if your task_type_config has auto-publish enabled.")


if __name__ == "__main__":
    main()
