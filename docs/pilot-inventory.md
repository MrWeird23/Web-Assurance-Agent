# Pilot site inventory

ROADMAP Milestone 6 requires a per-site inventory before any fleet expansion.
Fill in one row per pilot page before it goes on a check schedule. Nothing in
this repository reads this file — it is an operational record, kept next to
the manifest it describes.

| Site | Page | Critical plugins | Key workflows | Owner | Severity | Maintenance window |
| --- | --- | --- | --- | --- | --- | --- |
| `techx` | `techx-home` | Public WordPress/Elementor page rendering | Load homepage; expose customer-area and contact navigation | TECH X development | low | None for this test pilot |
| `larurros` | `larurros-home` | Public WordPress page rendering | Load homepage; expose institution, social-response, and contact navigation | TECH X development | low | None for this test pilot |
| `scmmd` | `scmmd-reporting-home` | Angular application shell and reporting form | Load reporting portal; render reporting guidance and initial form | TECH X development | medium | None for this test pilot |

- **Site / Page** — must match the `id` fields in the site manifest
  (`config/sites.yaml`) so an alert can be traced back to this row.
- **Critical plugins** — anything whose failure should page someone (forms,
  checkout, membership/login).
- **Key workflows** — the 1-3 user actions this page must support (e.g.
  "submit contact form", "add to cart").
- **Owner** — who gets paged and who approves visual baseline changes for
  this page.
- **Severity** — how urgently a confirmed outage on this page needs a human
  (e.g. `critical` / `high` / `low`).
- **Maintenance window** — recurring deploy/backup times when checks are
  expected to be noisy; use this to judge whether an alert during that
  window is signal or scheduled noise (the scheduler has no maintenance-mode
  awareness yet — see "Known gaps" in `docs/pilot-rollout.md`).

Expand this table only after the steps in `docs/pilot-rollout.md` have been
run for every page already listed.

## Open production connections

- Configure the Discord incident webhook in `TRIAGE_DISCORD_WEBHOOK_URL`.
- Configure a durable `TRIAGE_STATE_DATABASE_PATH` before production rollout.
- Confirm the Uptime Kuma monitor IDs for all three pilot pages and add each as
  `monitor_id` in `config/sites.yaml` before enabling event correlation.
- Keep `/admin/sites` behind a trusted private network or an authenticated
  reverse proxy. The console uses the webhook bearer token for API calls, but
  the HTML shell itself is intentionally not an identity provider.
- Confirm the exact production URL for `larurros-home`; the provisional
  `https://larurros.pt/` target did not resolve during the 2026-08-21 calibration.

## Calibration notes

- `techx-home`: the public page loaded successfully in an ordinary Chromium
  session on 2026-08-21. The hardened triage runner did not complete the page
  within its current browser deadline, so this target requires a runner-level
  compatibility investigation before scheduled browser checks are considered
  production-ready.
- `larurros-home`: DNS resolution failed during calibration; no live browser
  assertion could be verified.
- `scmmd-reporting-home`: the target returned HTTP 502 during calibration; no
  live browser assertion could be verified.
