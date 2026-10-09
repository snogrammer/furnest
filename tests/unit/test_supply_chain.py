"""The CI/publish pipeline stays pinned and Renovate keeps the pins fresh."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")


def test_workflows_exist() -> None:
    assert {w.name for w in WORKFLOWS} >= {"ci.yml", "publish.yml"}


def test_every_action_is_pinned_to_a_sha_with_tag_comment() -> None:
    unpinned = []
    for workflow in WORKFLOWS:
        for line in workflow.read_text().splitlines():
            m = USES.match(line)
            if not m:
                continue
            ref, rest = m.groups()
            if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref) or not re.match(r"\s*#\s*v\d", rest):
                unpinned.append(f"{workflow.name}: {line.strip()}")
    assert not unpinned


def test_trivy_image_is_digest_pinned_and_consistent() -> None:
    values = {
        m.group(1) for w in WORKFLOWS for m in re.finditer(r"TRIVY_IMAGE: (\S+)", w.read_text())
    }
    assert len(values) == 1
    assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", values.pop())


def test_publish_signs_and_attaches_sbom_and_provenance() -> None:
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "cosign sign" in text
    assert "--type cyclonedx" in text
    assert "--type slsaprovenance" in text
    assert "--format cyclonedx" in text


def test_renovate_covers_every_dependency_surface() -> None:
    config = json.loads((ROOT / "renovate.json").read_text())
    assert "helpers:pinGitHubActionDigests" in config["extends"]
    assert {"github-actions", "dockerfile", "pep621"} <= set(config["enabledManagers"])
    assert config["lockFileMaintenance"]["enabled"] is True
    trivy = next(m for m in config["customManagers"] if "TRIVY_IMAGE" in m["matchStrings"][0])
    pattern = re.sub(r"\(\?<(\w+)>", r"(?P<\1>", trivy["matchStrings"][0])  # JS -> Python groups
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        for m in re.finditer(r"TRIVY_IMAGE: .*", text):
            assert re.search(pattern, m.group(0))
