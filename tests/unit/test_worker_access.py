"""Approved root/handle and canonicalization boundary tests."""

from __future__ import annotations

import os
from pathlib import Path

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

    assert policy.authorize(path_reference) == resource.resolve()
    assert policy.authorize(handle_reference) == 42
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
    assert policy.authorize_wire(policy.wire_reference(path_reference)).value == resource.resolve()
    assert policy.authorize_wire(policy.wire_reference(handle_reference)).value == 42


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


@pytest.mark.parametrize(
    "raw",
    (
        {"resource_id": "x", "handle_id": "known", "extra": "forbidden"},
        {"resource_id": "x", "root_id": "input"},
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
