# Pilot rollout runbook

Operationalizes ROADMAP Milestone 6 ("Controlled pilot and gradual rollout")
against the mechanisms this repository actually has today. Run these steps
once per pilot page; don't add a next page until the current batch has been
noise-checked.

## 1. Select a small approved set of public pages

Pick 1-3 non-critical public pages first. Record each one in
`docs/pilot-inventory.md` before continuing — owner and severity must be
known before an alert can be actioned.

## 2. Define desktop and mobile manifests

Add one entry per page to your site manifest, modeled on
`config/sites.example.yaml` (already has a `desktop` + `mobile` viewport
pair). Leave `fast_check_interval_seconds` / `deep_check_interval_seconds` /
`kuma_monitor_id` commented out for now — the scheduler stays off until
step 3 has run clean.

Validate the manifest loads:

```bash
uv run python -c "from triage_agent.manifests import load_site_manifest; from pathlib import Path; load_site_manifest(Path('config/sites.yaml'))"
```

## 3. Run in dry-run mode

Leave `TRIAGE_DISCORD_WEBHOOK_URL` unset. `build_app()` then wires a
dry-run publisher that logs every would-be Discord payload
(`dry_run_discord_payload=...`) instead of sending it — see
`src/triage_agent/runtime.py`. Set `TRIAGE_SITE_MANIFEST_PATH` to your
manifest and start the service; poke each pilot page manually:

```bash
curl -X POST https://<host>/checks/pages/<page-id> -H "X-Triage-Token: <token>"
```

Read the `classification`, `confidence`, and `next_action` fields in the
response (see README "Browser check classification" / "Extended check
reports").

## 4. Review false positives for several days

Grep the service logs for `dry_run_discord_payload` and for
`deep_check_alert_delivery_failed` / `incident_publish_hook_failed`. A
`classification` other than `healthy`/`baseline_pending` on a page you know
is fine is a false positive — fix the manifest (required text/selectors,
`ignored_resource_patterns`, `critical_resource_patterns`) before moving on.

## 5. Approve visual baselines manually

The first capture on any viewport is always `baseline_pending` — it is never
auto-approved. Set `TRIAGE_VISUAL_BASELINE_DIRECTORY`, let a check run once
to produce a pending capture, review the screenshot yourself, then:

```bash
uv run python scripts/approve_baseline.py --baseline-dir "$TRIAGE_VISUAL_BASELINE_DIRECTORY" --list
uv run python scripts/approve_baseline.py --baseline-dir "$TRIAGE_VISUAL_BASELINE_DIRECTORY" \
    --page-id <page-id> --viewport-id <viewport-id> --operator "<your name>" --yes
```

Every approval is appended to `<baseline-dir>/audit.jsonl` and never rewritten.

## 6. Configure a dedicated reporting destination

Create a pilot-only Discord channel + webhook so pilot noise never mixes
with production incident channels. Set `TRIAGE_DISCORD_WEBHOOK_URL` to it
once dry-run review (step 4) is clean.

## 7. Add the assurance webhook alongside existing providers

Follow README "Additive Uptime Kuma integration" — add the Kuma webhook
notification without removing any existing provider. Only then uncomment
`fast_check_interval_seconds` / `kuma_monitor_id` for the pilot pages so the
scheduler starts probing them.

## 8. Expand in small batches only after noise and accuracy are acceptable

Add the next batch of pages to `docs/pilot-inventory.md`, repeat from step 2.
Bump `TRIAGE_SCHEDULER_GLOBAL_CONCURRENCY` / `TRIAGE_SCHEDULER_SITE_CONCURRENCY`
only if check latency, not alert noise, is the bottleneck.

## Known gaps

- Maintenance windows (`maintenance_windows` in the site manifest, see README
  "Check scheduling") only cover scheduled fast/deep checks, are UTC and
  recurring-weekly only, and only support a same-day start/end (no windows
  spanning midnight). A deploy/backup window from the inventory's
  "Maintenance window" column that doesn't fit those constraints still needs
  to be tracked manually.
- `scripts/approve_baseline.py` has no `--reject`; discard a bad pending
  capture by deleting its files under `<baseline-dir>/pending/` and letting
  the next check re-capture.
