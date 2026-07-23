from __future__ import annotations

import re
import stat
import tomllib
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from tools.installed_wheel_smoke import _snapshot_directory
from tools.w05_ci_smoke import (
    ArtifactPolicyError,
    InstalledSmokeError,
    _source_identity,
    _validate_member_name,
    find_single_wheel,
    hidden_source_packages,
    inspect_wheel,
    isolated_environment,
    parse_frozen_dependencies,
)

ROOT = Path(__file__).resolve().parents[2]
DIST_INFO = "megumin_companion_ai-0.1.0.dist-info"


def _valid_members() -> dict[str, bytes]:
    return {
        "app/__init__.py": b'__version__ = "0.1.0"\n',
        "app/main.py": b"def create_app(): ...\n",
        "app/resources/default_config.yaml": b"schema_version: 1\n",
        "desktop_client/__init__.py": b"",
        "desktop_client/entrypoint.py": b"def main(): return 3\n",
        f"{DIST_INFO}/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Name: megumin-companion-ai\n"
            b"Version: 0.1.0\n"
            b"Requires-Python: >=3.11,<3.12\n\n"
        ),
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: hatchling 1.31.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        f"{DIST_INFO}/entry_points.txt": (
            b"[console_scripts]\nmegumin-companion-api = app.cli:main\n"
            b"[gui_scripts]\n"
            b"megumin-companion-desktop = desktop_client.entrypoint:main\n"
        ),
        f"{DIST_INFO}/RECORD": b"",
    }


def _write_wheel(
    directory: Path,
    *,
    extra: Mapping[str, bytes] | None = None,
    omit: set[str] | None = None,
) -> Path:
    members = _valid_members()
    for name in omit or set():
        members.pop(name, None)
    members.update(extra or {})
    wheel = directory / "megumin_companion_ai-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _load_yaml(path: Path) -> dict[str, object]:
    value: object = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return _mapping(value)


def _steps(job: Mapping[str, object]) -> list[dict[str, object]]:
    return [_mapping(item) for item in _sequence(job["steps"])]


def test_inspect_wheel_returns_path_free_hash_evidence(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path, extra={"app/models.py": b"class Model: ...\n"})

    inspection = inspect_wheel(wheel)

    assert inspection.filename == wheel.name
    assert inspection.version == "0.1.0"
    assert len(inspection.sha256) == 64
    assert len(inspection.manifest_sha256) == 64
    assert inspection.generator == "hatchling 1.31.0"
    assert inspection.zip_create_systems in {(0,), (3,)}
    assert inspection.member_count == len(_valid_members()) + 1
    assert inspection.uncompressed_bytes > 0
    assert str(tmp_path) not in str(inspection.as_dict())


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "app/.env",
        "app/.env.production",
        "app/data/history.sqlite",
        "app/assets/avatar.png",
        "app/models/voice.onnx",
        "app/whisper-cli.exe",
        "app/whisper.dll",
        "app/resources/whisper-bin-x64.zip",
        "app/audio/reference.wav",
        "app/logs/app.log",
        "app/resources/settings.yaml",
        "app/resources/extra.yml",
        "desktop_client/private/key.pem",
        "desktop_client/private/secret.bin",
        "app/resources/private.txt",
        "tests/test_private.py",
        "tools/release.py",
    ],
)
def test_inspect_wheel_rejects_private_runtime_and_asset_members(
    tmp_path: Path, forbidden_name: str
) -> None:
    wheel = _write_wheel(tmp_path, extra={forbidden_name: b"synthetic-private-sentinel"})

    with pytest.raises(ArtifactPolicyError):
        inspect_wheel(wheel)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "app/../private.txt",
        "/app/private.txt",
        "app//private.txt",
    ],
)
def test_inspect_wheel_rejects_unsafe_archive_paths(tmp_path: Path, unsafe_name: str) -> None:
    wheel = _write_wheel(tmp_path, extra={unsafe_name: b"sentinel"})

    with pytest.raises(ArtifactPolicyError, match="unsafe wheel member path"):
        inspect_wheel(wheel)


def test_member_policy_rejects_windows_archive_separator() -> None:
    with pytest.raises(ArtifactPolicyError, match="unsafe wheel member path"):
        _validate_member_name("app\\private.txt")


def test_inspect_wheel_rejects_case_collisions(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path, extra={"APP/__init__.py": b"collision"})

    with pytest.raises(ArtifactPolicyError, match="case-colliding"):
        inspect_wheel(wheel)


def test_inspect_wheel_rejects_symbolic_links(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        link = zipfile.ZipInfo("app/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target.py")

    with pytest.raises(ArtifactPolicyError, match="Symbolic links|symbolic links"):
        inspect_wheel(wheel)


def test_inspect_wheel_requires_all_runtime_and_metadata_members(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path, omit={"app/resources/default_config.yaml"})

    with pytest.raises(ArtifactPolicyError, match="missing required package members"):
        inspect_wheel(wheel)


def test_inspect_wheel_rejects_changed_python_contract(tmp_path: Path) -> None:
    metadata_name = f"{DIST_INFO}/METADATA"
    wheel = _write_wheel(
        tmp_path,
        extra={
            metadata_name: (
                b"Metadata-Version: 2.4\n"
                b"Name: megumin-companion-ai\n"
                b"Version: 0.1.0\n"
                b"Requires-Python: >=3.12\n\n"
            )
        },
    )

    with pytest.raises(ArtifactPolicyError, match="Requires-Python"):
        inspect_wheel(wheel)


def test_inspect_wheel_enforces_compressed_and_uncompressed_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _write_wheel(tmp_path)
    monkeypatch.setattr("tools.w05_ci_smoke.MAX_WHEEL_BYTES", 1)
    with pytest.raises(ArtifactPolicyError, match="wheel size"):
        inspect_wheel(wheel)

    monkeypatch.setattr("tools.w05_ci_smoke.MAX_WHEEL_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr("tools.w05_ci_smoke.MAX_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(ArtifactPolicyError, match="uncompressed size"):
        inspect_wheel(wheel)


def test_inspect_wheel_requires_exact_entry_point_targets(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path,
        extra={
            f"{DIST_INFO}/entry_points.txt": (
                b"[console_scripts]\nmegumin-companion-api = app.cli:not_main\n"
                b"[gui_scripts]\n"
                b"megumin-companion-desktop = desktop_client.entrypoint:main\n"
            )
        },
    )

    with pytest.raises(ArtifactPolicyError, match="missing or changed"):
        inspect_wheel(wheel)


def test_inspect_wheel_requires_exact_build_generator(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path,
        extra={
            f"{DIST_INFO}/WHEEL": (
                b"Wheel-Version: 1.0\n"
                b"Generator: hatchling 9.9.9\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n"
            )
        },
    )

    with pytest.raises(ArtifactPolicyError, match="generator"):
        inspect_wheel(wheel)


def test_find_single_wheel_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ArtifactPolicyError, match="found 0"):
        find_single_wheel(tmp_path)
    first = _write_wheel(tmp_path)
    assert find_single_wheel(tmp_path) == first
    (tmp_path / "second.whl").write_bytes(first.read_bytes())
    with pytest.raises(ArtifactPolicyError, match="found 2"):
        find_single_wheel(tmp_path)


def test_parse_frozen_dependencies_removes_local_wheel_url() -> None:
    dependencies = parse_frozen_dependencies(
        "fastapi==0.120.0\nmegumin-companion-ai @ file:///private/runner/path.whl\n",
        project_version="0.1.0",
    )

    assert dependencies == [
        {"name": "fastapi", "version": "0.120.0"},
        {"name": "megumin-companion-ai", "version": "0.1.0"},
    ]
    assert "private" not in str(dependencies)


def test_parse_frozen_dependencies_rejects_other_path_install() -> None:
    with pytest.raises(InstalledSmokeError, match="path-based"):
        parse_frozen_dependencies(
            "fastapi @ file:///unreviewed/fastapi.whl\nmegumin-companion-ai==0.1.0\n",
            project_version="0.1.0",
        )


def test_parse_frozen_dependencies_rejects_duplicates() -> None:
    with pytest.raises(InstalledSmokeError, match="duplicate"):
        parse_frozen_dependencies(
            "fastapi==0.120.0\nFASTAPI==0.121.0\nmegumin-companion-ai==0.1.0\n",
            project_version="0.1.0",
        )


def test_isolated_environment_scrubs_project_and_index_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEGUMIN_LLM_PROVIDER", "real-provider")
    monkeypatch.setenv("COMPANION_LLM_API_KEY", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://token.invalid/simple")
    monkeypatch.setenv("UV_INDEX_URL", "https://token.invalid/simple")
    monkeypatch.setenv("PIP_INDEX_URL", "https://token.invalid/simple")
    monkeypatch.setenv("W05_CHANGE_REVISION", "a" * 40)

    environment = isolated_environment(tmp_path / "local", tmp_path / "cache")

    assert "MEGUMIN_LLM_PROVIDER" not in environment
    assert "COMPANION_LLM_API_KEY" not in environment
    assert "PYTHONPATH" not in environment
    assert "UV_DEFAULT_INDEX" not in environment
    assert "UV_INDEX_URL" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "W05_CHANGE_REVISION" not in environment
    assert environment["LOCALAPPDATA"] == str(tmp_path / "local")
    assert environment["UV_CACHE_DIR"] == str(tmp_path / "cache")


def test_source_identity_distinguishes_pr_merge_from_change_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("W05_CHANGE_REVISION", "b" * 40)

    assert _source_identity() == {
        "source_revision": "a" * 40,
        "change_revision": "b" * 40,
        "source_revision_kind": "pull-request-merge",
    }


def test_source_identity_reports_local_without_ci_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GITHUB_EVENT_NAME", "GITHUB_SHA", "W05_CHANGE_REVISION"):
        monkeypatch.delenv(name, raising=False)

    assert _source_identity() == {
        "source_revision": "local-unrecorded",
        "change_revision": "local-unrecorded",
        "source_revision_kind": "local",
    }


def test_source_identity_accepts_matching_push_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "c" * 40
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_SHA", revision)
    monkeypatch.setenv("W05_CHANGE_REVISION", revision)

    assert _source_identity() == {
        "source_revision": revision,
        "change_revision": revision,
        "source_revision_kind": "branch-head",
    }


def test_source_identity_rejects_mismatched_push_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("W05_CHANGE_REVISION", "b" * 40)

    with pytest.raises(InstalledSmokeError, match="do not match"):
        _source_identity()


def test_directory_snapshot_detects_cwd_mutation(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("before", encoding="utf-8")
    before = _snapshot_directory(tmp_path)
    sentinel.write_text("after", encoding="utf-8")

    assert _snapshot_directory(tmp_path) != before


def test_source_packages_are_hidden_and_restored_even_after_failure(tmp_path: Path) -> None:
    app_file = tmp_path / "app" / "sentinel.py"
    desktop_file = tmp_path / "desktop_client" / "sentinel.py"
    app_file.parent.mkdir()
    desktop_file.parent.mkdir()
    app_file.write_text("app sentinel", encoding="utf-8")
    desktop_file.write_text("desktop sentinel", encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        hidden_source_packages(tmp_path),
    ):
        assert not (tmp_path / "app").exists()
        assert not (tmp_path / "desktop_client").exists()
        quarantine = tmp_path / "dist" / "w05-source-quarantine"
        assert (quarantine / "app" / "sentinel.py").is_file()
        assert (quarantine / "desktop_client" / "sentinel.py").is_file()
        raise RuntimeError("synthetic failure")

    assert app_file.read_text(encoding="utf-8") == "app sentinel"
    assert desktop_file.read_text(encoding="utf-8") == "desktop sentinel"
    assert not (tmp_path / "dist" / "w05-source-quarantine").exists()


def test_workflow_preserves_source_gate_and_adds_dual_os_installed_gate() -> None:
    workflow = _load_yaml(ROOT / ".github" / "workflows" / "ci.yml")
    assert workflow["permissions"] == {"contents": "read"}

    triggers = _mapping(workflow["on"])
    assert "pull_request" in triggers
    push = _mapping(triggers["push"])
    branches = set(str(item) for item in _sequence(push["branches"]))
    assert branches == {"main", "agent/windows-development-baseline", "codex/**"}

    jobs = _mapping(workflow["jobs"])
    assert set(jobs) == {"quality", "installed-wheel"}
    for job_name in ("quality", "installed-wheel"):
        job = _mapping(jobs[job_name])
        matrix = _mapping(_mapping(job["strategy"])["matrix"])
        operating_systems = {str(item) for item in _sequence(matrix["os"])}
        assert operating_systems == {"macos-latest", "windows-latest"}
    installed_environment = _mapping(_mapping(jobs["installed-wheel"])["env"])
    assert installed_environment["W05_CHANGE_REVISION"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert _mapping(project["build-system"])["requires"] == ["hatchling==1.31.0"]


def test_workflow_job_commands_cache_and_provenance_policy() -> None:
    workflow = _load_yaml(ROOT / ".github" / "workflows" / "ci.yml")
    jobs = _mapping(workflow["jobs"])
    quality_steps = _steps(_mapping(jobs["quality"]))
    installed_steps = _steps(_mapping(jobs["installed-wheel"]))

    quality_commands = {str(step["run"]) for step in quality_steps if "run" in step}
    assert {
        "uv sync --frozen --all-groups --all-extras",
        "uv run pytest",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy",
    } <= quality_commands

    all_steps = quality_steps + installed_steps
    action_references = [str(step["uses"]) for step in all_steps if "uses" in step]
    assert action_references
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", ref)
        for ref in action_references
    )
    assert {reference.split("@", 1)[0] for reference in action_references} == {
        "actions/checkout",
        "actions/upload-artifact",
        "astral-sh/setup-uv",
    }

    checkout_steps = [
        step for step in all_steps if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkout_steps
    assert all(_mapping(step["with"])["persist-credentials"] == "false" for step in checkout_steps)

    quality_setup = next(
        step
        for step in quality_steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    installed_setup = next(
        step
        for step in installed_steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    assert _mapping(quality_setup["with"])["enable-cache"] == "true"
    assert _mapping(installed_setup["with"])["enable-cache"] == "false"
    assert _mapping(quality_setup["with"])["version"] == "0.11.28"
    assert _mapping(installed_setup["with"])["version"] == "0.11.28"

    installed_commands = "\n".join(str(step["run"]) for step in installed_steps if "run" in step)
    assert "uv build --wheel --out-dir dist/w05-wheel" in installed_commands
    assert "tools/w05_ci_smoke.py" in installed_commands
    assert "--no-project --python 3.11" in installed_commands

    upload = next(
        step
        for step in installed_steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    upload_inputs = _mapping(upload["with"])
    assert upload_inputs["path"] == "dist/w05-evidence/provenance.json"
    assert upload_inputs["retention-days"] == "7"
    assert "w05-wheel" not in str(upload_inputs["path"])


def test_dependabot_updates_only_actions_on_the_development_branch() -> None:
    config = _load_yaml(ROOT / ".github" / "dependabot.yml")
    updates = [_mapping(item) for item in _sequence(config["updates"])]

    assert len(updates) == 1
    update = updates[0]
    assert update["package-ecosystem"] == "github-actions"
    assert update["directory"] == "/"
    assert update["target-branch"] == "agent/windows-development-baseline"
    assert _mapping(update["schedule"])["interval"] == "weekly"
