# Maintenance-window awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A page can declare recurring weekly UTC maintenance windows in the site manifest; the background scheduler (fast + deep checks) still runs and records evidence during a window but suppresses the Discord alert, and re-alerts automatically once the window ends if the page is still failing.

**Architecture:** A new `MaintenanceWindow` Pydantic model + `maintenance_windows` field on `PageManifest` (`manifests.py`), plus a pure `is_within_maintenance_window(windows, now)` predicate next to it. `TriageEngine.handle_event` gains a `suppress_publish` flag so the fast-check path can skip the Discord send while still doing normal dedup bookkeeping (`delivered=False`, so it retries later). The deep-check path in `runtime.py` reuses the existing optional-publisher parameters — no engine changes needed there — by passing `None` for the publisher when the page is in-window.

**Tech Stack:** Python, Pydantic (existing `StrictManifestModel` base), pytest.

---

### Task 1: `MaintenanceWindow` model, `PageManifest` field, and manifest validation

**Files:**
- Modify: `src/triage_agent/manifests.py`
- Test: `tests/test_manifests.py`

Read `src/triage_agent/manifests.py` in full before starting — you need the exact base classes (`StrictManifestModel`, `Identifier`, `NonEmptyText`), the existing `Field(...)` bound-collection style, and the exact spot in `parse_site_manifest` where cross-field checks for other manifest features already live (next to the `interactions` fill/click check).

- [ ] **Step 1: Write the failing tests for parsing and validation**

Add to `tests/test_manifests.py` (the module already imports `parse_site_manifest`; add `pytest` if not already imported — check the top of the file first):

```python
def test_manifest_accepts_maintenance_windows() -> None:
    manifest_text = VALID_MANIFEST.replace(
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health",
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health\n"
        "        maintenance_windows:\n"
        "          - day_of_week: tue\n"
        '            start_time: "02:00"\n'
        '            end_time: "04:00"',
    )
    registry = parse_site_manifest(manifest_text)
    page = registry.page("home")
    assert len(page.maintenance_windows) == 1
    assert page.maintenance_windows[0].day_of_week == "tue"


def test_manifest_rejects_maintenance_window_where_start_is_not_before_end() -> None:
    manifest_text = VALID_MANIFEST.replace(
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health",
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health\n"
        "        maintenance_windows:\n"
        "          - day_of_week: tue\n"
        '            start_time: "04:00"\n'
        '            end_time: "02:00"',
    )
    with pytest.raises(ValueError, match="maintenance window"):
        parse_site_manifest(manifest_text)


def test_manifest_rejects_malformed_maintenance_window_time() -> None:
    manifest_text = VALID_MANIFEST.replace(
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health",
        "        wordpress_health:\n"
        "          - id: site-health\n"
        "            endpoint: https://example.com/wp-json/techx-monitor/v1/health\n"
        "        maintenance_windows:\n"
        "          - day_of_week: tue\n"
        '            start_time: "2:00"\n'
        '            end_time: "04:00"',
    )
    with pytest.raises(ValueError):
        parse_site_manifest(manifest_text)
```

Check the top of `tests/test_manifests.py` for `import pytest` — add it if missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_manifests.py -k maintenance_window -v`
Expected: FAIL — `PageManifest` has no field `maintenance_windows` (Pydantic will silently ignore unknown fields unless the model is set to forbid extras; check `StrictManifestModel`'s config — if it forbids extras, the first test fails with a validation error instead, which is also an acceptable "fails for the right reason" result. Either way, `maintenance_windows` must not yet exist on the returned page.)

- [ ] **Step 3: Add the `MaintenanceWindow` model and the `PageManifest` field**

In `src/triage_agent/manifests.py`, add the `MaintenanceWindow` model directly above `PageManifest` (find `class PageManifest(StrictManifestModel):`):

```python
_TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class MaintenanceWindow(StrictManifestModel):
    day_of_week: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    start_time: str = Field(pattern=_TIME_PATTERN)
    end_time: str = Field(pattern=_TIME_PATTERN)
```

If `Literal` isn't already imported at the top of the file, add it to the existing `typing` import line.

Then add the field to `PageManifest`, next to the other scheduling fields (`fast_check_interval_seconds` / `deep_check_interval_seconds` / `kuma_monitor_id` — find that block):

```python
    maintenance_windows: tuple[MaintenanceWindow, ...] = Field(default=(), max_length=10)
```

- [ ] **Step 4: Add the start-before-end validation in `parse_site_manifest`**

Find the existing interaction validation block in `parse_site_manifest`:

```python
    for page in page_list:
        for interaction in page.interactions:
            if interaction.action == "fill" and interaction.value is None:
                raise ValueError("Invalid site manifest: fill interaction requires a value")
            if interaction.action == "click" and interaction.value is not None:
                raise ValueError(
                    "Invalid site manifest: click interaction must not declare a value"
                )
```

Add directly after it:

```python
    for page in page_list:
        for window in page.maintenance_windows:
            if window.start_time >= window.end_time:
                raise ValueError(
                    "Invalid site manifest: maintenance window start_time must be before end_time"
                )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_manifests.py -k maintenance_window -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Add and test `is_within_maintenance_window`**

Add this function in `src/triage_agent/manifests.py`, near the bottom of the file (after `ManifestRegistry` and its methods, before `load_site_manifest` is a good spot). It needs `datetime` and `Sequence` — check the top-of-file imports first and add `from collections.abc import Sequence` and `from datetime import datetime` if not already present (the file may already import `Sequence`-like collections for other fields — check before adding a duplicate import):

```python
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def is_within_maintenance_window(
    windows: Sequence[MaintenanceWindow], now: datetime
) -> bool:
    """UTC, same-day windows only — a window cannot cross midnight."""
    current_day = _WEEKDAY_NAMES[now.weekday()]
    current_time = now.strftime("%H:%M")
    return any(
        window.day_of_week == current_day and window.start_time <= current_time < window.end_time
        for window in windows
    )
```

Add to `tests/test_manifests.py`:

```python
from datetime import datetime

from triage_agent.manifests import MaintenanceWindow, is_within_maintenance_window


def test_is_within_maintenance_window_matches_day_and_time_range() -> None:
    windows = [MaintenanceWindow(day_of_week="tue", start_time="02:00", end_time="04:00")]
    assert is_within_maintenance_window(windows, datetime(2026, 8, 18, 3, 0)) is True  # Tuesday


def test_is_within_maintenance_window_rejects_wrong_day() -> None:
    windows = [MaintenanceWindow(day_of_week="tue", start_time="02:00", end_time="04:00")]
    assert is_within_maintenance_window(windows, datetime(2026, 8, 19, 3, 0)) is False  # Wednesday


def test_is_within_maintenance_window_boundaries_are_start_inclusive_end_exclusive() -> None:
    windows = [MaintenanceWindow(day_of_week="tue", start_time="02:00", end_time="04:00")]
    assert is_within_maintenance_window(windows, datetime(2026, 8, 18, 2, 0)) is True
    assert is_within_maintenance_window(windows, datetime(2026, 8, 18, 4, 0)) is False


def test_is_within_maintenance_window_empty_list_is_never_in_window() -> None:
    assert is_within_maintenance_window([], datetime(2026, 8, 18, 3, 0)) is False
```

(2026-08-18 is a Tuesday, 2026-08-19 is a Wednesday — `datetime.weekday()` returns `0` for Monday, matching index 0 of `_WEEKDAY_NAMES`.)

- [ ] **Step 7: Run all manifest tests**

Run: `uv run pytest tests/test_manifests.py -v`
Expected: all pass, no regressions.

- [ ] **Step 8: Commit**

```bash
git add src/triage_agent/manifests.py tests/test_manifests.py
git commit -m "feat: add maintenance_windows to page manifests

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `suppress_publish` on `TriageEngine`

**Files:**
- Modify: `src/triage_agent/engine.py`
- Test: `tests/test_engine.py`

Read `src/triage_agent/engine.py` in full first — the exact reservation/publish/hook flow in `_handle_event` matters for where the new branch goes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engine.py`:

```python
async def test_engine_suppresses_publish_when_requested_but_still_classifies() -> None:
    published: list[dict[str, Any]] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(payload: dict[str, Any]) -> None:
        published.append(payload)

    engine = TriageEngine(probe=probe, publish=publish, confirmation_attempts=1)
    outcome = await engine.handle_event(
        _kuma_event(monitor_id=12, url="https://example.com/", down=True),
        suppress_publish=True,
    )

    assert outcome.incident.kind is IncidentKind.CONFIRMED_OUTAGE
    assert published == []


async def test_engine_retries_suppressed_incident_on_next_event() -> None:
    published: list[dict[str, Any]] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(payload: dict[str, Any]) -> None:
        published.append(payload)

    engine = TriageEngine(probe=probe, publish=publish, confirmation_attempts=1)
    event = _kuma_event(monitor_id=12, url="https://example.com/", down=True)

    await engine.handle_event(event, suppress_publish=True)
    await engine.handle_event(event)  # maintenance window over: same incident, no suppression

    assert len(published) == 1


async def test_engine_does_not_call_on_publish_hook_when_suppressed() -> None:
    hook_calls: list[Any] = []

    async def probe(url: str) -> ProbeResult:
        return ProbeResult(False, 503, 100, url, "HTTP 503", "cloudflare")

    async def publish(_payload: dict[str, Any]) -> None:
        return None

    async def on_publish(event: Any, incident: Any) -> None:
        hook_calls.append(incident.kind)

    engine = TriageEngine(
        probe=probe, publish=publish, confirmation_attempts=1, on_publish=on_publish
    )
    await engine.handle_event(
        _kuma_event(monitor_id=12, url="https://example.com/", down=True),
        suppress_publish=True,
    )

    assert hook_calls == []
```

These tests need a `_kuma_event` helper — add it near the top of `tests/test_engine.py`, after the imports, since `handle_event` (unlike `handle`) takes a `KumaEvent` directly rather than a raw webhook payload:

```python
from triage_agent.events import EventState, KumaEvent


def _kuma_event(*, monitor_id: int, url: str, down: bool) -> KumaEvent:
    return KumaEvent(
        monitor_id=monitor_id,
        monitor_name="Example",
        url=url,
        state=EventState.DOWN if down else EventState.UP,
        error="HTTP 503" if down else "",
        observed_at="2026-08-04T13:20:00+00:00",
    )
```

Check `src/triage_agent/events.py` for the real `KumaEvent` field names/types before writing this helper — match them exactly (this plan's guess at field names must be verified against the actual dataclass).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_engine.py -k suppress -v`
Expected: FAIL — `handle_event() got an unexpected keyword argument 'suppress_publish'`

- [ ] **Step 3: Add `suppress_publish` to `handle_event` / `_handle_event`**

In `src/triage_agent/engine.py`, change:

```python
    async def handle_event(self, event: KumaEvent) -> TriageOutcome:
        monitor_lock = self._monitor_locks.setdefault(event.monitor_id, asyncio.Lock())
        async with monitor_lock:
            return await self._handle_event(event)
```

to:

```python
    async def handle_event(
        self, event: KumaEvent, *, suppress_publish: bool = False
    ) -> TriageOutcome:
        monitor_lock = self._monitor_locks.setdefault(event.monitor_id, asyncio.Lock())
        async with monitor_lock:
            return await self._handle_event(event, suppress_publish=suppress_publish)
```

Then change `_handle_event`'s signature and publish branch. From:

```python
    async def _handle_event(self, event: KumaEvent) -> TriageOutcome:
        probes = []
        if event.state is EventState.DOWN:
            for attempt in range(self._confirmation_attempts):
                if attempt:
                    await self._sleeper(self._confirmation_delay_seconds)
                probes.append(await self._probe(event.url))
        incident = classify_incident(event, probes)
        reservation = self._registry.reserve(event, incident)
        discord_payload = render_discord_payload(
            event,
            incident,
            probes,
            recovery_duration_seconds=reservation.recovery_duration_seconds,
        )
        if reservation.decision is PublicationDecision.PUBLISH:
            try:
                await self._publish(discord_payload)
            except Exception:
                self._registry.complete(reservation, delivered=False)
                raise
            self._registry.complete(reservation, delivered=True)
            if self._on_publish is not None:
                try:
                    await self._on_publish(event, incident)
                except Exception:
                    logger.exception(
                        "incident_publish_hook_failed monitor_id=%s", event.monitor_id
                    )
        return TriageOutcome(
            event=event,
            incident=incident,
            probes=probes,
            discord_payload=discord_payload,
        )
```

to:

```python
    async def _handle_event(
        self, event: KumaEvent, *, suppress_publish: bool = False
    ) -> TriageOutcome:
        probes = []
        if event.state is EventState.DOWN:
            for attempt in range(self._confirmation_attempts):
                if attempt:
                    await self._sleeper(self._confirmation_delay_seconds)
                probes.append(await self._probe(event.url))
        incident = classify_incident(event, probes)
        reservation = self._registry.reserve(event, incident)
        discord_payload = render_discord_payload(
            event,
            incident,
            probes,
            recovery_duration_seconds=reservation.recovery_duration_seconds,
        )
        if reservation.decision is PublicationDecision.PUBLISH:
            if suppress_publish:
                logger.info(
                    "maintenance_window_suppressed_publish monitor_id=%s", event.monitor_id
                )
                self._registry.complete(reservation, delivered=False)
            else:
                try:
                    await self._publish(discord_payload)
                except Exception:
                    self._registry.complete(reservation, delivered=False)
                    raise
                self._registry.complete(reservation, delivered=True)
                if self._on_publish is not None:
                    try:
                        await self._on_publish(event, incident)
                    except Exception:
                        logger.exception(
                            "incident_publish_hook_failed monitor_id=%s", event.monitor_id
                        )
        return TriageOutcome(
            event=event,
            incident=incident,
            probes=probes,
            discord_payload=discord_payload,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_engine.py -v`
Expected: all pass, including the 3 new tests and all pre-existing ones (no regressions to the default `suppress_publish=False` path).

- [ ] **Step 5: Commit**

```bash
git add src/triage_agent/engine.py tests/test_engine.py
git commit -m "feat: add suppress_publish to TriageEngine for maintenance windows

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire maintenance windows into the scheduler's fast/deep checks

**Files:**
- Modify: `src/triage_agent/runtime.py`

`run_fast_check` and `run_deep_check` are closures inside `build_app` with no existing unit tests (verified: `tests/test_scheduler.py` only exercises `CheckScheduler` with fake callables, never these real closures) — this task is implementation-only, verified by the full test suite plus manual review, matching the existing test-depth convention for this file.

- [ ] **Step 1: Import `is_within_maintenance_window`**

In `src/triage_agent/runtime.py`, find:

```python
from triage_agent.manifests import ManifestRegistry, PageManifest, load_site_manifest
```

Change to:

```python
from triage_agent.manifests import (
    ManifestRegistry,
    PageManifest,
    is_within_maintenance_window,
    load_site_manifest,
)
```

- [ ] **Step 2: Suppress the fast-check publish during a maintenance window**

Find `run_fast_check`:

```python
        async def run_fast_check(page: PageManifest) -> None:
            assert page.kuma_monitor_id is not None  # enforced by manifest validation
            probe_result = await probe(page.url)
            event = KumaEvent(
                monitor_id=page.kuma_monitor_id,
                monitor_name=page.id,
                url=page.url,
                state=EventState.UP if probe_result.ok else EventState.DOWN,
                error=probe_result.error or "",
                observed_at=datetime.now(UTC).isoformat(),
            )
            await engine.handle_event(event)
```

Change the last two lines to:

```python
            now = datetime.now(UTC)
            event = KumaEvent(
                monitor_id=page.kuma_monitor_id,
                monitor_name=page.id,
                url=page.url,
                state=EventState.UP if probe_result.ok else EventState.DOWN,
                error=probe_result.error or "",
                observed_at=now.isoformat(),
            )
            await engine.handle_event(
                event, suppress_publish=is_within_maintenance_window(page.maintenance_windows, now)
            )
```

- [ ] **Step 3: Suppress the deep-check alerts during a maintenance window**

Find `run_deep_check`:

```python
        async def run_deep_check(page: PageManifest) -> None:
            assert manifest_registry is not None
            assert page_checker is not None
            result = await run_page_check(
                page=page,
                allowed_hosts=set(manifest_registry.allowed_hosts(page.id)),
                site_id=manifest_registry.site_id(page.id),
                page_checker=page_checker,
                wordpress_health_checker=wordpress_health_checker,
                wordpress_alert_publisher=publisher,
            )
            if result["classification"] not in ("healthy", "baseline_pending"):
                try:
                    await publisher(
                        render_browser_check_discord_payload(
                            page_id=page.id,
                            failed_viewports=result["evidence"]["failed_viewports"],
                            failure_codes=result["evidence"]["failure_codes"],
                            failed_plugin_assertions=result["evidence"]["failed_plugin_assertions"],
                        )
                    )
                except DiscordPublishError:
                    logger.exception("deep_check_alert_delivery_failed page_id=%s", page.id)
```

Replace with:

```python
        async def run_deep_check(page: PageManifest) -> None:
            assert manifest_registry is not None
            assert page_checker is not None
            in_maintenance_window = is_within_maintenance_window(
                page.maintenance_windows, datetime.now(UTC)
            )
            result = await run_page_check(
                page=page,
                allowed_hosts=set(manifest_registry.allowed_hosts(page.id)),
                site_id=manifest_registry.site_id(page.id),
                page_checker=page_checker,
                wordpress_health_checker=wordpress_health_checker,
                wordpress_alert_publisher=None if in_maintenance_window else publisher,
            )
            if result["classification"] not in ("healthy", "baseline_pending"):
                if in_maintenance_window:
                    logger.info("maintenance_window_suppressed_alert page_id=%s", page.id)
                else:
                    try:
                        await publisher(
                            render_browser_check_discord_payload(
                                page_id=page.id,
                                failed_viewports=result["evidence"]["failed_viewports"],
                                failure_codes=result["evidence"]["failure_codes"],
                                failed_plugin_assertions=result["evidence"][
                                    "failed_plugin_assertions"
                                ],
                            )
                        )
                    except DiscordPublishError:
                        logger.exception("deep_check_alert_delivery_failed page_id=%s", page.id)
```

- [ ] **Step 4: Run the full test suite and static checks**

Run: `uv run pytest -q`
Expected: all pass, no regressions.

Run: `uv run ruff check .`
Expected: clean.

Run: `uv run mypy src tests`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/triage_agent/runtime.py
git commit -m "feat: suppress scheduler alerts during page maintenance windows

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Documentation

**Files:**
- Modify: `config/sites.example.yaml`
- Modify: `README.md`
- Modify: `docs/pilot-rollout.md`

- [ ] **Step 1: Document the field in the example manifest**

In `config/sites.example.yaml`, find the scheduling comment block:

```yaml
        # Uncomment to put this page on a background check schedule (see README
        # "Check scheduling"), in addition to the on-demand /checks/pages endpoint.
        # fast_check_interval_seconds: 120   # 60-300; re-probes and triages like a Kuma webhook
        # deep_check_interval_seconds: 1800  # 900-3600; re-runs the full browser/WP health check
        # kuma_monitor_id: 1                 # required by fast_check_interval_seconds
```

Add directly after it:

```yaml
        # Optional: recurring weekly UTC windows during which scheduled fast/deep
        # checks still run and record evidence, but don't alert (see README
        # "Check scheduling"). Manual /checks/pages calls always alert regardless.
        # maintenance_windows:
        #   - day_of_week: tue      # mon-sun
        #     start_time: "02:00"   # UTC, HH:MM, 24h
        #     end_time: "04:00"     # must be later than start_time same day
```

- [ ] **Step 2: Document the behavior in README**

In `README.md`, find the "Check scheduling" section (search for `### Check scheduling`). Add a new paragraph after the existing bullet list and before the "Scheduled checks share the same global/per-site concurrency budgets" sentence:

```markdown
A page can also declare `maintenance_windows` (recurring weekly UTC windows, see
`config/sites.example.yaml`). During a window, scheduled fast/deep checks still run and
record evidence as normal, but the Discord alert is suppressed; if the page is still
failing once the window ends, the very next scheduled check alerts normally. This only
covers the background scheduler — a manual `/checks/pages/{page_id}` call and incoming
Uptime Kuma webhook events always alert regardless of any window.
```

- [ ] **Step 3: Update the pilot runbook's known gaps**

In `docs/pilot-rollout.md`, find:

```markdown
## Known gaps

- No maintenance-mode awareness: a deploy/backup window in the inventory's
  "Maintenance window" column is not enforced anywhere — a check during that
  window still alerts. Track recurring false positives there manually until
  this is built.
- `scripts/approve_baseline.py` has no `--reject`; discard a bad pending
  capture by deleting its files under `<baseline-dir>/pending/` and letting
  the next check re-capture.
```

Replace with:

```markdown
## Known gaps

- Maintenance windows are UTC, recurring weekly, and same-day only (no
  overnight wraparound past midnight); set the inventory's "Maintenance
  window" column to match a page's `maintenance_windows` manifest entries
  (see README "Check scheduling") so the two stay in sync.
- `scripts/approve_baseline.py` has no `--reject`; discard a bad pending
  capture by deleting its files under `<baseline-dir>/pending/` and letting
  the next check re-capture.
```

- [ ] **Step 4: Commit**

```bash
git add config/sites.example.yaml README.md docs/pilot-rollout.md
git commit -m "docs: document maintenance_windows manifest field

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Final verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full global verification gate from ROADMAP.md**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src tests
TRIAGE_WEBHOOK_TOKEN='configured-test-value' \
TRIAGE_ALLOWED_HOSTS='example.com' \
docker compose config --quiet
```

Expected: all clean, no failures.

- [ ] **Step 2: Confirm nothing left uncommitted**

Run: `git status`
Expected: clean working tree.
