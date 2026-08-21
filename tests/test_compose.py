from pathlib import Path

import yaml


def test_visual_baseline_volume_is_writable_for_admin_uploads() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    volumes = compose["services"]["web-assurance-agent"]["volumes"]
    baseline = next(
        volume
        for volume in volumes
        if volume.get("target") == "/var/lib/triage/baselines"
    )

    assert baseline.get("read_only", False) is False
