import asyncio

import pytest

from triage_agent.manifests import ManifestRegistry, PageManifest, parse_site_manifest
from triage_agent.scheduler import CheckScheduler

MANIFEST = """
version: 1
sites:
  - id: site-a
    allowed_hosts:
      - example.com
    pages:
      - id: home
        url: https://example.com/
        fast_check_interval_seconds: 60
        deep_check_interval_seconds: 900
        kuma_monitor_id: 1
        viewports:
          - id: desktop
            width: 1440
            height: 900
            device_scale_factor: 1.0
      - id: about
        url: https://example.com/about
        deep_check_interval_seconds: 900
        viewports:
          - id: desktop
            width: 1440
            height: 900
            device_scale_factor: 1.0
"""


SINGLE_LOOP_MANIFEST = """
version: 1
sites:
  - id: site-a
    allowed_hosts:
      - example.com
    pages:
      - id: home
        url: https://example.com/
        fast_check_interval_seconds: 60
        kuma_monitor_id: 1
        viewports:
          - id: desktop
            width: 1440
            height: 900
            device_scale_factor: 1.0
"""


def _load_registry(text: str = MANIFEST) -> ManifestRegistry:
    return parse_site_manifest(text)


async def test_loop_sleeps_interval_plus_jitter_before_each_run() -> None:
    registry = _load_registry(SINGLE_LOOP_MANIFEST)
    sleeps: list[float] = []
    runs = 0

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def run_fast_check(page: PageManifest) -> None:
        nonlocal runs
        runs += 1

    async def run_deep_check(page: PageManifest) -> None:
        pass

    scheduler = CheckScheduler(
        manifest_registry=registry,
        run_fast_check=run_fast_check,
        run_deep_check=run_deep_check,
        jitter_seconds=20.0,
        sleeper=sleeper,
        random_fraction=lambda: 0.5,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_forever()

    # Only the fast-check loop's single page has fast_check_interval_seconds set.
    assert sleeps[0] == 60 + 0.5 * 20.0
    assert runs == 1


async def test_run_immediate_deep_check_is_bounded_by_global_concurrency() -> None:
    registry = _load_registry()
    active = 0
    max_active = 0

    async def run_fast_check(page: PageManifest) -> None:
        pass

    async def run_deep_check(page: PageManifest) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    scheduler = CheckScheduler(
        manifest_registry=registry,
        run_fast_check=run_fast_check,
        run_deep_check=run_deep_check,
        global_concurrency=1,
        site_concurrency=2,
    )

    home = registry.page("home")
    about = registry.page("about")
    await asyncio.gather(
        scheduler.run_immediate_deep_check(home),
        scheduler.run_immediate_deep_check(about),
    )

    assert max_active == 1


async def test_run_immediate_deep_check_is_bounded_by_site_concurrency() -> None:
    registry = _load_registry()
    active = 0
    max_active = 0

    async def run_fast_check(page: PageManifest) -> None:
        pass

    async def run_deep_check(page: PageManifest) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    scheduler = CheckScheduler(
        manifest_registry=registry,
        run_fast_check=run_fast_check,
        run_deep_check=run_deep_check,
        global_concurrency=5,
        site_concurrency=1,
    )

    home = registry.page("home")
    about = registry.page("about")
    await asyncio.gather(
        scheduler.run_immediate_deep_check(home),
        scheduler.run_immediate_deep_check(about),
    )

    # Both pages belong to the same site, so site_concurrency=1 still serializes them.
    assert max_active == 1


async def test_execute_swallows_check_runner_exceptions() -> None:
    registry = _load_registry()

    async def run_fast_check(page: PageManifest) -> None:
        pass

    async def run_deep_check(page: PageManifest) -> None:
        raise RuntimeError("check exploded")

    scheduler = CheckScheduler(
        manifest_registry=registry,
        run_fast_check=run_fast_check,
        run_deep_check=run_deep_check,
    )

    await scheduler.run_immediate_deep_check(registry.page("home"))  # must not raise


def test_rejects_non_positive_concurrency_and_negative_jitter() -> None:
    registry = _load_registry()

    async def noop(page: PageManifest) -> None:
        pass

    with pytest.raises(ValueError, match="global_concurrency"):
        CheckScheduler(
            manifest_registry=registry,
            run_fast_check=noop,
            run_deep_check=noop,
            global_concurrency=0,
        )
    with pytest.raises(ValueError, match="site_concurrency"):
        CheckScheduler(
            manifest_registry=registry,
            run_fast_check=noop,
            run_deep_check=noop,
            site_concurrency=0,
        )
    with pytest.raises(ValueError, match="jitter_seconds"):
        CheckScheduler(
            manifest_registry=registry,
            run_fast_check=noop,
            run_deep_check=noop,
            jitter_seconds=-1,
        )
