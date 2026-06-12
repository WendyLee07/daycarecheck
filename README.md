# Background-Check Your Daycare

A free, sourced background-check tool for U.S. families choosing a daycare. Each report runs through the [Agentic Commons](https://agentic-commons.org) network and emerges with a verifiable contribution ID.

## What it does

- Search any U.S. daycare or daycare chain by name
- Get the real owner / parent company / private-equity backers
- Every claim backed by a clickable public source URL
- Federal investigation status (e.g. Sen. Merkley 2026 probe)
- All reports CC0, resolvable on agentic-commons.org

## How it works

1. User submits a daycare name on the site
2. Backend creates a public-good task on [ClawForce](https://clawgrid.ai)
3. A Lobster (AI agent) claims the task and researches via web search
4. Lobster submits an artifact with an ACG marker
5. ClawForce QA verifier independently fact-checks (catches hallucinations)
6. Verified report streamed back to the user

## Files

| File | Purpose |
|------|---------|
| `index.html` | Static frontend with live search + SSE progress |
| `backend.py` | FastAPI bridge (frontend ↔ ClawForce) with JWT signing + artifact normalizer |
| `daycare_chains.json` | Pre-loaded cache of 6 well-known PE-owned chains |
| `brief_daycare_ownership.md` | Lobster instruction template (v0.1, ownership only) |
| `.agentic-commons` | ACG protocol opt-in manifest |
| `.well-known/agentic-commons.json` | Service manifest |

## License

Reports: [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Code: see LICENSE.

## Status

v0 prototype — see https://agentic-commons.org/c/AC-T-RK94DF for an example completed contribution.
