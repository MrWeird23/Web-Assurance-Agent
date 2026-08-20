# Pilot site inventory

ROADMAP Milestone 6 requires a per-site inventory before any fleet expansion.
Fill in one row per pilot page before it goes on a check schedule. Nothing in
this repository reads this file — it is an operational record, kept next to
the manifest it describes.

| Site | Page | Critical plugins | Key workflows | Owner | Severity | Maintenance window |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

- **Site / Page** — must match the `id` fields in the site manifest
  (`config/sites.example.yaml`) so an alert can be traced back to this row.
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
