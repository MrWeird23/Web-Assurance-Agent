"""Periodic fast/deep page checks: jittered intervals, bounded concurrency."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from triage_agent.manifests import ManifestRegistry, PageManifest

logger = logging.getLogger(__name__)

CheckRunner = Callable[[PageManifest], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]


class CheckScheduler:
    def __init__(
        self,
        *,
        manifest_registry: ManifestRegistry,
        run_fast_check: CheckRunner,
        run_deep_check: CheckRunner,
        global_concurrency: int = 2,
        site_concurrency: int = 1,
        jitter_seconds: float = 20.0,
        sleeper: Sleeper = asyncio.sleep,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be at least 1")
        if site_concurrency < 1:
            raise ValueError("site_concurrency must be at least 1")
        if jitter_seconds < 0:
            raise ValueError("jitter_seconds cannot be negative")
        self._manifest_registry = manifest_registry
        self._run_fast_check = run_fast_check
        self._run_deep_check = run_deep_check
        self._jitter_seconds = jitter_seconds
        self._sleeper = sleeper
        self._random_fraction = random_fraction
        self._global_semaphore = asyncio.Semaphore(global_concurrency)
        self._site_semaphores = {
            site.id: asyncio.Semaphore(site_concurrency)
            for site in manifest_registry.manifest.sites
        }

    async def run_forever(self) -> None:
        """Run every configured page's fast/deep loop until cancelled."""
        loops = []
        for page in self._manifest_registry.pages():
            if page.fast_check_interval_seconds is not None:
                loops.append(
                    self._loop(page, page.fast_check_interval_seconds, self._run_fast_check)
                )
            if page.deep_check_interval_seconds is not None:
                loops.append(
                    self._loop(page, page.deep_check_interval_seconds, self._run_deep_check)
                )
        if not loops:
            return
        await asyncio.gather(*loops)

    async def run_immediate_deep_check(self, page: PageManifest) -> None:
        """Run a deep check now, still bounded by the same concurrency budgets."""
        await self._execute(page, self._run_deep_check)

    async def _loop(self, page: PageManifest, interval_seconds: int, run: CheckRunner) -> None:
        while True:
            jitter = self._random_fraction() * self._jitter_seconds
            await self._sleeper(interval_seconds + jitter)
            await self._execute(page, run)

    async def _execute(self, page: PageManifest, run: CheckRunner) -> None:
        site_id = self._manifest_registry.site_id(page.id)
        async with self._global_semaphore, self._site_semaphores[site_id]:
            try:
                await run(page)
            except Exception:
                logger.exception("scheduled_check_failed page_id=%s", page.id)
