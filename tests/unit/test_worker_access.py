"""Approved root/handle and canonicalization boundary tests."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import app.workers.access as worker_access
import pytest
from app.workers.access import (
    ApprovedResourcePolicy,
    ResourceAccessError,
    ResourceReference,
)


def test_approved_relative_file_and_inherited_handle_use_only_symbolic_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    resource = root / "input.bin"
    resource.write_bytes(b"content-never-on-wire")
    policy = ApprovedResourcePolicy(roots={"input": root}, inherited_handles={"pcm": 42})

    path_reference = ResourceReference(
        resource_id="source",
        root_id="input",
        relative_path="input.bin",
    )
    handle_reference = ResourceReference(resource_id="buffer", handle_id="pcm")

    path_authority = policy.authorize(path_reference)
    try:
        assert path_authority.owns_descriptor
        assert not os.get_inheritable(path_authority.value)
        assert os.read(path_authority.value, 64) == b"content-never-on-wire"
    finally:
        path_authority.close()
    handle_authority = policy.authorize(handle_reference)
    assert handle_authority.value == 42 and not handle_authority.owns_descriptor
    assert policy.wire_reference(path_reference) == {
        "resource_id": "source",
        "root_id": "input",
        "relative_path": "input.bin",
    }
    assert policy.wire_reference(handle_reference) == {
        "resource_id": "buffer",
        "handle_id": "pcm",
    }
    assert policy.inherited_handle_values == (42,)
    wire_path = policy.authorize_wire(policy.wire_reference(path_reference))
    try:
        assert wire_path.owns_descriptor and os.read(wire_path.value, 64) == (
            b"content-never-on-wire"
        )
    finally:
        wire_path.close()
    wire_handle = policy.authorize_wire(policy.wire_reference(handle_reference))
    assert wire_handle.value == 42 and not wire_handle.owns_descriptor


@pytest.mark.parametrize(
    "relative",
    ("../outside.bin", "sub/../../outside.bin", "/absolute.bin", "", "a\x00b"),
)
def test_path_escape_and_invalid_relative_forms_are_rejected(
    tmp_path: Path,
    relative: str,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    policy = ApprovedResourcePolicy(roots={"input": root})
    reference = ResourceReference(resource_id="source", root_id="input", relative_path=relative)

    with pytest.raises(ResourceAccessError):
        policy.authorize(reference)


def test_unapproved_root_and_handle_are_rejected_without_reflecting_values(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    policy = ApprovedResourcePolicy(roots={"input": root}, inherited_handles={"known": 7})
    references = (
        ResourceReference(resource_id="source", root_id="private-root", relative_path="x"),
        ResourceReference(resource_id="source", handle_id="private-handle"),
    )
    for reference in references:
        with pytest.raises(ResourceAccessError) as raised:
            policy.authorize(reference)
        assert "private" not in str(raised.value)


def test_symlink_or_junction_escape_is_rejected_before_file_access(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"sentinel")
    link = root / "alias"
    try:
        if os.name == "nt":
            os.symlink(outside, link, target_is_directory=True)
        else:
            link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("current user cannot create a directory symlink")
    policy = ApprovedResourcePolicy(roots={"input": root})
    reference = ResourceReference(
        resource_id="source",
        root_id="input",
        relative_path="alias/secret.bin",
    )

    with pytest.raises(ResourceAccessError, match="escape_or_reparse"):
        policy.authorize(reference)


def test_resource_reference_requires_exactly_one_authority() -> None:
    with pytest.raises(ResourceAccessError, match="reference_invalid"):
        ResourceReference(resource_id="source")
    with pytest.raises(ResourceAccessError, match="reference_invalid"):
        ResourceReference(
            resource_id="source",
            root_id="input",
            relative_path="x",
            handle_id="handle",
        )


@pytest.mark.skipif(os.name != "nt", reason="junction swap is a Windows security regression")
def test_authorized_resource_handle_survives_junction_swap_without_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    original = root / "original"
    selected = root / "selected"
    outside = tmp_path / "outside"
    root.mkdir()
    selected.mkdir()
    outside.mkdir()
    (selected / "input.bin").write_bytes(b"SAFE")
    (outside / "input.bin").write_bytes(b"OUTSIDE")
    policy = ApprovedResourcePolicy(roots={"input": root})
    authorized = policy.authorize(
        ResourceReference(
            resource_id="source",
            root_id="input",
            relative_path="selected/input.bin",
        )
    )
    try:
        selected.rename(original)
    except PermissionError:
        try:
            assert os.read(authorized.value, 16) == b"SAFE"
        finally:
            authorized.close()
        return
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(selected), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        original.rename(selected)
        pytest.skip("current user cannot create a directory junction")
    try:
        observed = os.read(authorized.value, 16)
        assert observed == b"SAFE"
    finally:
        close = getattr(authorized, "close", None)
        if close is not None:
            close()
        selected.rmdir()
        original.rename(selected)


@pytest.mark.parametrize(
    "raw",
    (
        {"resource_id": "x", "handle_id": "known", "extra": "forbidden"},
        {"resource_id": "x", "root_id": "input"},
        {"resource_id": "x", "root_id": "input", "relative_path": 3},
        {"resource_id": 1, "handle_id": "known"},
        ["not", "an", "object"],
    ),
)
def test_worker_side_wire_authorization_rejects_schema_confusion(
    tmp_path: Path,
    raw: object,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    policy = ApprovedResourcePolicy(roots={"input": root}, inherited_handles={"known": 7})
    with pytest.raises(ResourceAccessError, match="reference_invalid"):
        policy.authorize_wire(raw)


def test_policy_rejects_invalid_roots_handles_missing_and_non_regular_resources(
    tmp_path: Path,
) -> None:
    relative = tmp_path / "relative"
    relative.mkdir()
    with pytest.raises(ResourceAccessError, match="root_invalid"):
        ApprovedResourcePolicy(roots={"input": relative.relative_to(tmp_path)})
    with pytest.raises(ResourceAccessError, match="handle_invalid"):
        ApprovedResourcePolicy(inherited_handles={"handle": -1})

    root = tmp_path / "approved"
    root.mkdir()
    (root / "directory").mkdir()
    policy = ApprovedResourcePolicy(roots={"input": root})
    for relative_path, code in (
        ("missing.bin", "escape_or_reparse"),
        ("directory", "not_regular_file"),
        ("x" * 1025, "relative_path_invalid"),
    ):
        reference = ResourceReference(
            resource_id="source",
            root_id="input",
            relative_path=relative_path,
        )
        with pytest.raises(ResourceAccessError, match=code):
            policy.authorize(reference)


def test_authorized_resource_context_is_idempotent_and_reference_ids_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "input.bin"
    source.write_bytes(b"SAFE")
    policy = ApprovedResourcePolicy(roots={"input": root})
    with policy.authorize(
        ResourceReference(resource_id="source", root_id="input", relative_path="input.bin")
    ) as authorized:
        assert os.read(authorized.value, 16) == b"SAFE"
    assert authorized.closed
    authorized.close()

    with pytest.raises(ResourceAccessError, match="resource_id_invalid"):
        ResourceReference(resource_id="INVALID SPACE", handle_id="known")
    with pytest.raises(ResourceAccessError, match="authority_invalid"):
        ResourceReference(resource_id="source", handle_id="INVALID SPACE")


def test_policy_rejects_missing_or_file_roots(tmp_path: Path) -> None:
    missing = (tmp_path / "missing").resolve()
    with pytest.raises(ResourceAccessError, match="root_invalid"):
        ApprovedResourcePolicy(roots={"input": missing})
    regular = tmp_path / "regular.bin"
    regular.write_bytes(b"SAFE")
    with pytest.raises(ResourceAccessError, match="root_invalid"):
        ApprovedResourcePolicy(roots={"input": regular})


def test_open_and_final_handle_validation_failures_close_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    source = root / "input.bin"
    source.write_bytes(b"SAFE")
    reference = ResourceReference(
        resource_id="source",
        root_id="input",
        relative_path="input.bin",
    )
    policy = ApprovedResourcePolicy(roots={"input": root})

    real_open = os.open

    def fail_open(_path: object, _flags: int) -> int:
        raise OSError("synthetic open failure")

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(ResourceAccessError, match="escape_or_reparse"):
        policy.authorize(reference)
    monkeypatch.setattr(os, "open", real_open)

    monkeypatch.setattr(stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ResourceAccessError, match="not_regular_file"):
        policy.authorize(reference)
    monkeypatch.undo()

    monkeypatch.setattr(worker_access, "_final_path_from_descriptor", lambda _fd: outside)
    with pytest.raises(ResourceAccessError, match="escape_or_reparse"):
        policy.authorize(reference)
    monkeypatch.undo()

    def fail_final_path(_fd: int) -> Path:
        raise OSError("synthetic final path failure")

    monkeypatch.setattr(worker_access, "_final_path_from_descriptor", fail_final_path)
    with pytest.raises(ResourceAccessError, match="escape_or_reparse"):
        policy.authorize(reference)


def test_darwin_final_path_uses_f_getpath_maxpathlen_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []
    fake_fcntl = ModuleType("fcntl")

    def resolve(descriptor: int, operation: int, buffer: bytes) -> bytes:
        calls.append((descriptor, operation, len(buffer)))
        resolved = b"/approved/input.bin\0"
        return resolved + (b"\0" * (len(buffer) - len(resolved)))

    fake_fcntl.fcntl = resolve  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    assert worker_access._darwin_final_path_from_descriptor(7) == Path("/approved/input.bin")
    assert calls == [(7, 50, 1024)]


@pytest.mark.parametrize("maximum_active_jobs", (0, 65, True))
def test_helper_capacity_configuration_is_bounded(maximum_active_jobs: object) -> None:
    from app.workers.helper import HelperRuntime

    class Handler:
        async def run_job(self, *_args: object) -> dict[str, object]:
            return {}

        async def close(self) -> None:
            return None

    with pytest.raises(ValueError, match="configuration invalid"):
        HelperRuntime(
            role="media",
            handler=Handler(),
            maximum_active_jobs=maximum_active_jobs,  # type: ignore[arg-type]
        )
