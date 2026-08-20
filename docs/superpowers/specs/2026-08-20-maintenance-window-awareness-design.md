# Maintenance-window awareness — design

Closes the first "Known gaps" item in `docs/pilot-rollout.md`: a
deploy/backup window recorded in `docs/pilot-inventory.md` today is not
enforced anywhere, so a scheduled check during that window still alerts.

## Scope

Suppresses alerts from the **background scheduler only** (fast-check and
deep-check loops in `src/triage_agent/scheduler.py` / `runtime.py`). Out of
scope, by design:

- Incoming Uptime Kuma webhook events (`POST /webhooks/uptime-kuma`) —
  Kuma's own maintenance handling, if configured, already covers this path.
- Manual on-demand checks (`POST /checks/pages/{page_id}`) — an operator
  explicitly asking for a check result gets one, window or not.

The check still runs and its evidence is still recorded during a window;
only the outbound Discord alert is dropped. This avoids a blind spot when
the window ends — if the page is still down, the very next scheduled check
alerts normally.

## Manifest schema

New optional per-page field in the site manifest (`config/sites.yaml`),
alongside the existing scheduling fields:

```yaml
maintenance_windows:
  - day_of_week: tue
    start_time: "02:00"
    end_time: "04:00"
```

- `day_of_week`: one of `mon`..`sun`.
- `start_time` / `end_time`: `"HH:MM"`, 24-hour, UTC. `start_time` must be
  strictly before `end_time` — **no overnight wraparound in v1** (a window
  can't cross midnight). Known limitation, not a blocker for the pilot's
  deploy/backup windows; documented in the runbook.
- Up to 10 windows per page (`Field(max_length=10)`), matching the existing
  bounded-collection convention in `manifests.py`.
- Defaults to `()` — no behavior change for pages that don't set it.

Validated the same way other manifest fields are: a Pydantic model
(`MaintenanceWindow`) under the existing `StrictManifestModel`, plus a
`parse_site_manifest` pass rejecting `start_time >= end_time` (same place the
existing interaction/viewport/plugin-assertion cross-field checks live).

## Window evaluation

A pure function next to `PageManifest` in `manifests.py`:

```python
def is_within_maintenance_window(
    windows: Sequence[MaintenanceWindow], now: datetime
) -> bool:
```

Compares `now.weekday()` (mapped to the `mon`..`sun` name) and
`now.strftime("%H:%M")` against each window; zero-padded 24h strings compare
correctly with plain `<=`/`<`. `now` is always `datetime.now(UTC)`, matching
every other timestamp already in the codebase (e.g. `KumaEvent.observed_at`
in `runtime.py`).

## Suppression mechanism

Two different call sites need two different mechanisms, because the two
publish paths are shaped differently:

**Fast check** (`run_fast_check` in `runtime.py`) goes through the shared
`TriageEngine`, which is also used by the webhook path — the publisher
itself can't be swapped per call. Add an explicit
`suppress_publish: bool = False` parameter to `TriageEngine.handle_event` /
`_handle_event`. When the reservation would publish and `suppress_publish`
is set: skip `self._publish` and the `on_publish` hook, log
`maintenance_window_suppressed_publish`, and call
`self._registry.complete(reservation, delivered=False)` — the same
"not delivered" bookkeeping already used on a real publish failure, so the
existing retry-on-next-event logic re-fires once the window ends.

**Deep check** (`run_deep_check` in `runtime.py`) and the WordPress-health
alert inside `run_page_check` (`api.py`) already take their publisher as a
plain optional parameter, and `run_page_check` is shared with the manual
endpoint. No new parameter needed there: `run_deep_check` computes
`in_window` once and passes `wordpress_alert_publisher=None if in_window
else publisher`; the classification-based browser-check alert (also in
`run_deep_check`, after `run_page_check` returns) is wrapped in the same
`if not in_window:` check, with a log line on the suppressed branch.

## Testing

- `test_manifests.py`: valid window parses; `start_time >= end_time`
  rejected; bad `HH:MM` pattern rejected; `is_within_maintenance_window`
  true/false including exact boundary minutes and wrong day.
- `test_engine.py`: `suppress_publish=True` skips `self._publish` and the
  `on_publish` hook, and leaves the reservation `delivered=False` (a
  follow-up event still publishes).
- `runtime.py`'s `run_fast_check`/`run_deep_check` wiring itself is
  implementation-only, matching existing coverage: they're closures inside
  `build_app` with no test seam today (`CheckScheduler` is tested with fake
  callables in `test_scheduler.py`, but the real closures never are — adding
  one just for this feature, without also covering everything else those
  closures already do, would be inconsistent). Verified instead by the full
  suite + `mypy`/`ruff` plus manual review of the diff.

## Non-goals

- No UI/API to manage windows — YAML manifest only, matching how every other
  per-page setting works today.
- No timezone other than UTC — matches every other timestamp in the system.
- No overnight (cross-midnight) windows — noted above.
