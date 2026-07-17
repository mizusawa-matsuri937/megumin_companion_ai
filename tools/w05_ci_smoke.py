"""Build-evidence and isolated installed-wheel smoke for the W05 CI gate.

This module intentionally uses only the Python standard library.  The CI job
can therefore inspect a freshly built wheel before synchronising the project
environment or importing any project package from the source checkout.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path

PROJECT_NAME = "megumin-companion-ai"
EXPECTED_REQUIRES_PYTHON = frozenset({">=3.11", "<3.12"})
MAX_WHEEL_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024

_DIST_INFO = re.compile(r"^megumin_companion_ai-[^/]+\.dist-info$")
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_FROZEN_DEPENDENCY = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".git",
        "__pycache__",
        "assets",
        "audio",
        "data",
        "images",
        "logs",
        "model",
        "models",
        "tests",
        "user_config",
    }
)
_FORBIDDEN_SUFFIXES = (
    ".aac",
    ".bin",
    ".bmp",
    ".ckpt",
    ".db",
    ".dotenv",
    ".exp3.json",
    ".flac",
    ".ggml",
    ".gguf",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".key",
    ".log",
    ".m4a",
    ".moc3",
    ".model3.json",
    ".motion3.json",
    ".mp3",
    ".onnx",
    ".opus",
    ".ort",
    ".p12",
    ".pem",
    ".pfx",
    ".physics3.json",
    ".png",
    ".pt",
    ".pth",
    ".safetensors",
    ".shm",
    ".sqlite",
    ".sqlite3",
    ".tflite",
    ".tiff",
    ".wal",
    ".wav",
    ".webp",
)
_FORBIDDEN_CONFIG_NAMES = frozenset(
    {
        "config.json",
        "config.yaml",
        "config.yml",
        "settings.json",
        "settings.yaml",
        "settings.yml",
    }
)
_REQUIRED_PACKAGE_MEMBERS = frozenset(
    {
        "app/__init__.py",
        "app/main.py",
        "app/resources/default_config.yaml",
        "desktop_client/__init__.py",
        "desktop_client/entrypoint.py",
    }
)


class ArtifactPolicyError(RuntimeError):
    """Raised when the wheel violates the W05 package-content contract."""


class InstalledSmokeError(RuntimeError):
    """Raised when a clean installed-wheel subprocess violates its contract."""


@dataclass(frozen=True, slots=True)
class WheelInspection:
    """Path-free evidence derived from one inspected wheel."""

    filename: str
    version: str
    sha256: str
    size_bytes: int
    member_count: int
    uncompressed_bytes: int
    manifest_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "version": self.version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "member_count": self.member_count,
            "uncompressed_bytes": self.uncompressed_bytes,
            "manifest_sha256": self.manifest_sha256,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_single_wheel(wheel_directory: Path) -> Path:
    wheels = sorted(path for path in wheel_directory.glob("*.whl") if path.is_file())
    if len(wheels) != 1:
        raise ArtifactPolicyError(
            f"expected exactly one wheel in the build directory, found {len(wheels)}"
        )
    return wheels[0]


def _validate_member_name(raw_name: str) -> tuple[str, tuple[str, ...]]:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name or raw_name.startswith("/"):
        raise ArtifactPolicyError(f"unsafe wheel member path: {raw_name!r}")
    stripped = raw_name[:-1] if raw_name.endswith("/") else raw_name
    parts = tuple(stripped.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactPolicyError(f"unsafe wheel member path: {raw_name!r}")
    normalized = "/".join(parts)
    if len(normalized) > 512:
        raise ArtifactPolicyError("wheel member path exceeds 512 characters")
    return normalized, parts


def _check_forbidden_member(normalized: str, parts: tuple[str, ...]) -> None:
    folded_parts = tuple(part.casefold() for part in parts)
    basename = folded_parts[-1]
    if any(part in _FORBIDDEN_COMPONENTS for part in folded_parts):
        raise ArtifactPolicyError(f"forbidden private/runtime directory in wheel: {normalized}")
    if basename == ".env" or basename.startswith(".env."):
        raise ArtifactPolicyError(f"environment file is forbidden in wheel: {normalized}")
    if basename in _FORBIDDEN_CONFIG_NAMES:
        raise ArtifactPolicyError(f"user configuration is forbidden in wheel: {normalized}")
    if basename.endswith((".yaml", ".yml")) and normalized != ("app/resources/default_config.yaml"):
        raise ArtifactPolicyError(f"unexpected configuration resource in wheel: {normalized}")
    if basename.endswith(_FORBIDDEN_SUFFIXES):
        raise ArtifactPolicyError(f"forbidden asset/private-data type in wheel: {normalized}")


def inspect_wheel(wheel_path: Path) -> WheelInspection:
    """Validate wheel paths, package allowlist, metadata, and sensitive file exclusions."""

    wheel_path = wheel_path.resolve()
    if not wheel_path.is_file() or wheel_path.suffix.casefold() != ".whl":
        raise ArtifactPolicyError("wheel path must name one existing .whl file")
    wheel_size = wheel_path.stat().st_size
    if wheel_size <= 0 or wheel_size > MAX_WHEEL_BYTES:
        raise ArtifactPolicyError(f"wheel size is outside the W05 limit: {wheel_size} bytes")

    with zipfile.ZipFile(wheel_path) as archive:
        infos = archive.infolist()
        if not infos:
            raise ArtifactPolicyError("wheel archive is empty")

        normalized_infos: list[tuple[str, zipfile.ZipInfo]] = []
        casefolded_names: set[str] = set()
        dist_info_roots: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            normalized, parts = _validate_member_name(info.filename)
            folded = normalized.casefold()
            if folded in casefolded_names:
                raise ArtifactPolicyError(f"duplicate or case-colliding wheel member: {normalized}")
            casefolded_names.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise ArtifactPolicyError(f"symbolic links are forbidden in wheel: {normalized}")
            if info.is_dir():
                continue
            if info.file_size < 0:
                raise ArtifactPolicyError(f"invalid member size in wheel: {normalized}")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ArtifactPolicyError("wheel uncompressed size exceeds the W05 limit")

            top_level = parts[0]
            if top_level not in {"app", "desktop_client"}:
                if not _DIST_INFO.fullmatch(top_level):
                    raise ArtifactPolicyError(f"unexpected top-level wheel member: {normalized}")
                dist_info_roots.add(top_level)
            _check_forbidden_member(normalized, parts)
            if (
                top_level in {"app", "desktop_client"}
                and normalized != "app/resources/default_config.yaml"
                and not normalized.casefold().endswith(".py")
            ):
                raise ArtifactPolicyError(
                    f"unexpected non-code package member in wheel: {normalized}"
                )
            normalized_infos.append((normalized, info))

        if len(dist_info_roots) != 1:
            raise ArtifactPolicyError(
                f"expected exactly one project dist-info directory, found {len(dist_info_roots)}"
            )
        dist_info = next(iter(dist_info_roots))
        names = {name for name, _ in normalized_infos}
        missing_package = sorted(_REQUIRED_PACKAGE_MEMBERS - names)
        if missing_package:
            raise ArtifactPolicyError(
                "wheel is missing required package members: " + ", ".join(missing_package)
            )
        required_dist_info = {
            f"{dist_info}/METADATA",
            f"{dist_info}/RECORD",
            f"{dist_info}/WHEEL",
            f"{dist_info}/entry_points.txt",
        }
        missing_metadata = sorted(required_dist_info - names)
        if missing_metadata:
            raise ArtifactPolicyError(
                "wheel is missing required dist-info members: " + ", ".join(missing_metadata)
            )

        metadata_bytes = archive.read(f"{dist_info}/METADATA")
        metadata = BytesParser(policy=email_policy).parsebytes(metadata_bytes)
        project_name = metadata.get("Name")
        version = metadata.get("Version")
        requires_python = metadata.get("Requires-Python")
        if project_name != PROJECT_NAME or not version:
            raise ArtifactPolicyError("wheel project name/version metadata is unexpected")
        normalized_requires_python = frozenset(
            item.strip() for item in (requires_python or "").split(",") if item.strip()
        )
        if normalized_requires_python != EXPECTED_REQUIRES_PYTHON:
            raise ArtifactPolicyError(
                f"wheel Requires-Python changed unexpectedly: {requires_python!r}"
            )

        entry_points = configparser.ConfigParser(interpolation=None)
        try:
            entry_points.read_string(archive.read(f"{dist_info}/entry_points.txt").decode("utf-8"))
        except (UnicodeDecodeError, configparser.Error) as exc:
            raise ArtifactPolicyError("wheel entry point metadata is invalid") from exc
        required_entry_points = {
            ("console_scripts", "megumin-companion-api"): "app.cli:main",
            ("gui_scripts", "megumin-companion-desktop"): "desktop_client.entrypoint:main",
        }
        for (group, name), target in required_entry_points.items():
            if entry_points.get(group, name, fallback=None) != target:
                raise ArtifactPolicyError(f"wheel entry point is missing or changed: {name}")

        manifest = hashlib.sha256()
        for normalized, info in sorted(normalized_infos, key=lambda item: item[0].casefold()):
            member_bytes = archive.read(info)
            member_digest = hashlib.sha256(member_bytes).hexdigest()
            manifest.update(normalized.encode("utf-8"))
            manifest.update(b"\x00")
            manifest.update(str(len(member_bytes)).encode("ascii"))
            manifest.update(b"\x00")
            manifest.update(member_digest.encode("ascii"))
            manifest.update(b"\n")

    return WheelInspection(
        filename=wheel_path.name,
        version=version,
        sha256=sha256_file(wheel_path),
        size_bytes=wheel_size,
        member_count=len(normalized_infos),
        uncompressed_bytes=total_uncompressed,
        manifest_sha256=manifest.hexdigest(),
    )


def isolated_environment(local_app_data: Path, uv_cache: Path) -> dict[str, str]:
    """Return a subprocess environment without project overrides or Python path fallback."""

    environment = dict(os.environ)
    for name in tuple(environment):
        folded = name.upper()
        if folded.startswith(("MEGUMIN_", "COMPANION_")) or folded in {
            "PYTHONHOME",
            "PYTHONPATH",
            "UV_DEFAULT_INDEX",
            "UV_INDEX",
            "UV_INDEX_URL",
            "UV_EXTRA_INDEX_URL",
            "UV_INSECURE_HOST",
            "UV_KEYRING_PROVIDER",
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "PIP_TRUSTED_HOST",
            "VIRTUAL_ENV",
        }:
            environment.pop(name, None)
    environment.update(
        {
            "LOCALAPPDATA": str(local_app_data),
            "XDG_DATA_HOME": str(local_app_data),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "UV_CACHE_DIR": str(uv_cache),
            "UV_NO_PROGRESS": "1",
            "NO_COLOR": "1",
        }
    )
    return environment


@contextmanager
def hidden_source_packages(source_root: Path) -> Iterator[None]:
    """Temporarily hide product package directories and always restore them.

    The quarantine is constrained to ``<source>/dist`` on the same filesystem,
    so each operation is an atomic directory rename rather than a recursive
    copy, delete, or cross-volume move.
    """

    source_root = source_root.resolve()
    dist = source_root / "dist"
    dist.mkdir(exist_ok=True)
    resolved_dist = dist.resolve()
    if dist.is_symlink() or resolved_dist.parent != source_root:
        raise InstalledSmokeError("source quarantine dist directory escaped the source root")
    quarantine = resolved_dist / "w05-source-quarantine"
    if quarantine.exists() or quarantine.is_symlink():
        raise InstalledSmokeError("source quarantine path already exists")

    sources: list[tuple[Path, Path]] = []
    for package_name in ("app", "desktop_client"):
        source = source_root / package_name
        if source.is_symlink() or not source.is_dir() or source.resolve().parent != source_root:
            raise InstalledSmokeError(f"source package directory is unsafe: {package_name}")
        sources.append((source, quarantine / package_name))

    quarantine.mkdir()
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in sources:
            source.rename(destination)
            moved.append((source, destination))
        yield
    finally:
        for source, destination in reversed(moved):
            if source.exists() or source.is_symlink() or not destination.is_dir():
                raise InstalledSmokeError(
                    f"cannot safely restore hidden source package: {source.name}"
                )
            destination.rename(source)
        quarantine.rmdir()


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    label: str,
    timeout_seconds: float = 180.0,
) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        stdout = completed.stdout[-4000:].strip()
        stderr = completed.stderr[-4000:].strip()
        raise InstalledSmokeError(
            f"{label} failed with exit {completed.returncode}; stdout={stdout!r}; stderr={stderr!r}"
        )
    return completed.stdout.strip()


def _venv_python(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not candidate.is_file():
        raise InstalledSmokeError("uv did not create the expected virtual-environment Python")
    return candidate


def parse_frozen_dependencies(output: str, *, project_version: str) -> list[dict[str, str]]:
    """Normalize ``uv pip freeze`` without retaining a local wheel file URL."""

    dependencies: list[dict[str, str]] = []
    names_seen: set[str] = set()
    project_seen = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _FROZEN_DEPENDENCY.fullmatch(line)
        if match:
            name, version = match.groups()
        elif " @ " in line:
            name, _location = line.split(" @ ", 1)
            if name.casefold().replace("_", "-") != PROJECT_NAME:
                raise InstalledSmokeError(f"unexpected path-based installed dependency: {name}")
            version = project_version
        else:
            raise InstalledSmokeError(f"unexpected installed dependency record: {line!r}")
        normalized_name = name.casefold().replace("_", "-")
        if normalized_name in names_seen:
            raise InstalledSmokeError(f"duplicate installed dependency record: {normalized_name}")
        names_seen.add(normalized_name)
        if normalized_name == PROJECT_NAME:
            project_seen = True
            version = project_version
        dependencies.append({"name": normalized_name, "version": version})
    if not project_seen:
        raise InstalledSmokeError("installed dependency inventory omitted the project wheel")
    dependencies.sort(key=lambda item: item["name"])
    return dependencies


def _parse_last_json_line(output: str, *, label: str) -> dict[str, object]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise InstalledSmokeError(f"{label} produced no JSON evidence")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise InstalledSmokeError(f"{label} did not end with valid JSON") from exc
    if not isinstance(value, dict):
        raise InstalledSmokeError(f"{label} JSON evidence must be an object")
    return {str(key): item for key, item in value.items()}


def _write_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_step_summary(evidence: Mapping[str, object]) -> None:
    summary_name = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_name:
        return
    wheel = evidence["wheel"]
    lock = evidence["lock"]
    python_evidence = evidence["python"]
    if not isinstance(wheel, Mapping) or not isinstance(lock, Mapping):
        raise InstalledSmokeError("internal provenance shape is invalid")
    if not isinstance(python_evidence, Mapping):
        raise InstalledSmokeError("internal Python provenance shape is invalid")
    with Path(summary_name).open("a", encoding="utf-8") as handle:
        handle.write("### W05 installed-wheel evidence\n\n")
        handle.write(f"- Wheel SHA-256: `{wheel['sha256']}`\n")
        handle.write(f"- Wheel manifest SHA-256: `{wheel['manifest_sha256']}`\n")
        handle.write(f"- uv.lock SHA-256: `{lock['sha256']}`\n")
        handle.write(f"- Build Python: `{python_evidence['build']}`\n")
        handle.write(f"- Installed Python: `{python_evidence['installed']}`\n")
        handle.write("- Uploaded artifact is provenance JSON only; the wheel is not published.\n")


def run_installed_smoke(
    *,
    source_root: Path,
    wheel_path: Path,
    lock_file: Path,
    installed_smoke_script: Path,
) -> dict[str, object]:
    """Install one inspected wheel into a new venv and run it outside the repository."""

    source_root = source_root.resolve()
    wheel_path = wheel_path.resolve()
    lock_file = lock_file.resolve()
    installed_smoke_script = installed_smoke_script.resolve()
    if not (source_root / "pyproject.toml").is_file():
        raise InstalledSmokeError("source root does not contain pyproject.toml")
    if not lock_file.is_file() or not lock_file.is_relative_to(source_root):
        raise InstalledSmokeError("lock file must be an existing file inside the source root")
    if not installed_smoke_script.is_file() or not installed_smoke_script.is_relative_to(
        source_root
    ):
        raise InstalledSmokeError("installed smoke script must be inside the source root")

    inspection = inspect_wheel(wheel_path)
    uv = shutil.which("uv")
    if uv is None:
        raise InstalledSmokeError("uv executable is required for the isolated installation smoke")

    with tempfile.TemporaryDirectory(prefix="megumin-W05-仓库外 空格-") as temporary_name:
        temporary_root = Path(temporary_name).resolve()
        arbitrary_cwd = temporary_root / "任意 CWD with spaces"
        local_app_data = temporary_root / "Local App Data 隔离"
        uv_cache = temporary_root / "uv-cache-empty"
        venv = temporary_root / "fresh-wheel-venv"
        arbitrary_cwd.mkdir(parents=True)
        local_app_data.mkdir(parents=True)
        uv_cache.mkdir(parents=True)
        environment = isolated_environment(local_app_data, uv_cache)

        _run_checked(
            [uv, "venv", "--python", "3.11", str(venv)],
            cwd=arbitrary_cwd,
            environment=environment,
            label="fresh virtual environment creation",
        )
        python = _venv_python(venv)
        _run_checked(
            [uv, "pip", "install", "--python", str(python), str(wheel_path)],
            cwd=arbitrary_cwd,
            environment=environment,
            label="wheel installation",
            timeout_seconds=300.0,
        )
        _run_checked(
            [uv, "pip", "check", "--python", str(python)],
            cwd=arbitrary_cwd,
            environment=environment,
            label="installed dependency compatibility check",
        )
        frozen = _run_checked(
            [uv, "pip", "freeze", "--python", str(python)],
            cwd=arbitrary_cwd,
            environment=environment,
            label="installed dependency inventory",
        )
        dependencies = parse_frozen_dependencies(frozen, project_version=inspection.version)

        copied_smoke = arbitrary_cwd / "installed_wheel_smoke.py"
        shutil.copyfile(installed_smoke_script, copied_smoke)
        sentinel = arbitrary_cwd / "cwd-sentinel.txt"
        sentinel.write_text("W05 synthetic sentinel; runtime must not modify this file.\n", "utf-8")
        with hidden_source_packages(source_root):
            smoke_output = _run_checked(
                [
                    str(python),
                    "-I",
                    "-B",
                    str(copied_smoke),
                    "--forbid-root",
                    str(source_root),
                    "--local-app-data",
                    str(local_app_data),
                ],
                cwd=arbitrary_cwd,
                environment=environment,
                label="installed CLI/ASGI/arbitrary-CWD smoke",
            )
        smoke = _parse_last_json_line(smoke_output, label="installed wheel smoke")
        if smoke.get("status") != "ok":
            raise InstalledSmokeError("installed wheel smoke did not report status=ok")

        python_output = _run_checked(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import json,platform;"
                    "print(json.dumps({'implementation':platform.python_implementation(),"
                    "'version':platform.python_version()}))"
                ),
            ],
            cwd=arbitrary_cwd,
            environment=environment,
            label="installed Python provenance",
        )
        installed_python = _parse_last_json_line(python_output, label="installed Python provenance")
        uv_version = _run_checked(
            [uv, "--version"],
            cwd=arbitrary_cwd,
            environment=environment,
            label="uv provenance",
        )

    revision = os.environ.get("GITHUB_SHA", "")
    if not _COMMIT_SHA.fullmatch(revision):
        revision = "local-unrecorded"
    return {
        "schema_version": 1,
        "artifact_kind": "w05-ci-provenance-only",
        "release_artifact": False,
        "source_revision": revision,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "python": {
            "build": platform.python_version(),
            "installed": installed_python.get("version"),
            "implementation": installed_python.get("implementation"),
        },
        "uv": uv_version,
        "lock": {
            "filename": lock_file.name,
            "sha256": sha256_file(lock_file),
        },
        "wheel": inspection.as_dict(),
        "dependencies": dependencies,
        "installed_smoke": smoke,
        "source_checkout": {"product_packages_hidden_during_smoke": True},
        "cache": {
            "restored_by_workflow": False,
            "promoted_as_artifact": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        source_root = args.source_root.resolve()
        wheel = find_single_wheel(args.wheel_dir.resolve())
        evidence = run_installed_smoke(
            source_root=source_root,
            wheel_path=wheel,
            lock_file=args.lock_file,
            installed_smoke_script=source_root / "tools" / "installed_wheel_smoke.py",
        )
        _write_evidence(args.evidence.resolve(), evidence)
        _append_step_summary(evidence)
    except (
        ArtifactPolicyError,
        InstalledSmokeError,
        OSError,
        ValueError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"w05_ci_smoke_failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
