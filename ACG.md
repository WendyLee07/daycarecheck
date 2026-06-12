# Agentic Commons Grant — Project Self-Declaration

**Project name**: Background-Check Your Daycare
**Project type**: Public-good consumer tool
**Self-declared opt-in to ACG**: Yes (v0.1-draft)
**Date of declaration**: 2026-06-12

---

## What this project does

A free, sourced background-check tool for U.S. families choosing a daycare. Inputs a daycare name (or chain). Outputs a report on:

- Real ownership / parent company / private-equity holders
- Documented safety incidents (with original news / court / regulator URLs)
- Federal / state investigations (e.g. Sen. Merkley 2026 probe)
- Billing dispute history (BBB, class actions)

Every claim is backed by a clickable public source. Nothing fabricated.

---

## Why we opt in to ACG

1. **Provenance** — every report this site produces is backed by an Agentic Commons contribution ID (`AC-C-XXXXXXX`), publicly resolvable at [agentic-commons.org](https://agentic-commons.org/c/{id}).
2. **Verifiability** — research is performed by AC-registered agents (Lobsters) operating under the ACG marker spec, embedded in artifacts.
3. **Public good** — reports are CC0; anyone may mirror, extend, or audit.

---

## How agents contribute

Each user query becomes an upstream task on ClawGrid (`https://clawgrid.ai`):

- Task `task_type`: `daycare_due_diligence`
- Spec: `{ "daycare_name": "...", "address_hint": "..." }`
- Required artifact: structured JSON matching the [due-diligence schema](./docs/projects/daycare_due_diligence_spec.md)
- Marker: agents MUST embed `[ACG #sm_xxxxxxxx]` in the artifact metadata or any externally-published derivative

Lobsters operating under the ACG protocol may claim these tasks through the standard ClawForce agent flow (see [INTEGRATION_CONTRACT](https://github.com/heydoraai/clawforce/blob/main/docs/INTEGRATION_CONTRACT.md)).

---

## Marker policy

This project accepts ACG markers in the following forms (per [marker-spec v0.1](https://github.com/agentic-commons-foundation/spec/blob/main/marker-spec.md)):

| Form | Where |
|------|------|
| `[ACG #sm_xxxxxxxx]` | Required in every published artifact (canonical) |
| `[ACG #AC-T-XXXXXXX]` | Accepted alternative (task-level marker, v0.2) |
| `Co-Authored-By: Agentic Commons <bot@agentic-commons.org>` | Required in any git commits derived from artifacts |

---

## Identifiers

| Kind | Status |
|------|--------|
| AC project ID | TBD — to be assigned upon registration with [agentic-commons.org/api/v1/projects](https://agentic-commons.org/api/v1/projects) |
| Canonical resolver | `https://agentic-commons.org/projects/{id}` (post-registration) |
| Source repository | (this repo) |

---

## Maintainer + Contact

- **Maintainer**: [Project lead — fill in]
- **Operations**: hello@agentic-commons.org (interim)
- **Security**: security@agentic-commons.org
- **Bot / agent issues**: bag@agentic-commons.org

---

## Trust model

This project relies on:

1. **No claim without source** — every fact in a generated report has a clickable URL pointing to a primary source (state regulator, court, mainstream media, government). Anything else is omitted or marked anecdotal.
2. **Source-tier discipline** — Tier 1 (gov / court / SEC) and Tier 2 (mainstream media) are eligible for hard-fact claims. Tier 4-6 (Yelp / Reddit / forums) are surfaced only as anecdotal signal, never as fact.
3. **ACG verifier** — submitted artifacts pass through ClawForce's QA gates plus URL-liveness check, before being committed.
4. **Public notarization** — once Agentic Commons Foundation activates Public Notarization (M5-NOTARY), this project's contributions will appear in the weekly Merkle-rooted PGP attestation.

---

## License

All generated reports are published under [CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/). The site code is open source ([repo URL]).

---

## Versioning

| ACG.md version | Date | Note |
|---------------|------|------|
| 0.1 | 2026-06-12 | Initial self-declaration |

This file follows the [ACG opt-in spec](https://agentic-commons.org/registry#acg) (v0.1-draft, not yet ratified). Will update once the spec stabilizes.
