"""Validated, atomic persistence for the site manifest."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from triage_agent.manifests import MAX_MANIFEST_BYTES, ManifestRegistry, parse_site_manifest


class ManifestStore:
    def __init__(self, path: Path, *, permitted_hosts: set[str] | frozenset[str]) -> None:
        self.path = path
        self.permitted_hosts = frozenset(host.lower() for host in permitted_hosts)
        self.registry = parse_site_manifest(path.read_text(encoding="utf-8"))
        self._validate_runtime_hosts(self.registry)

    def read(self) -> dict[str, Any]:
        return self.registry.manifest.model_dump(mode="json")

    def replace(self, payload: object) -> ManifestRegistry:
        try:
            rendered = yaml.safe_dump(
                payload,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        except yaml.YAMLError as exc:
            raise ValueError("Invalid site manifest") from exc
        if len(rendered.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("Site manifest is too large")
        registry = parse_site_manifest(rendered)
        self._validate_runtime_hosts(registry)
        self._atomic_write(rendered)
        self.registry = registry
        return registry

    def _validate_runtime_hosts(self, registry: ManifestRegistry) -> None:
        configured = {
            host.lower()
            for site in registry.manifest.sites
            for host in site.allowed_hosts
        }
        unexpected = sorted(configured - self.permitted_hosts)
        if unexpected:
            raise ValueError(
                "Site hosts must first be approved in TRIAGE_ALLOWED_HOSTS: "
                + ", ".join(unexpected)
            )

    def _atomic_write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
