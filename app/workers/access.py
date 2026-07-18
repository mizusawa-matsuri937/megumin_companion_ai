"""Approved-root and inherited-handle validation for helper job resources."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import MappingProxyType

from app.windows_security import ReparsePointError, assert_no_reparse_points

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_RELATIVE_CHARS = 1024


class ResourceAccessError(RuntimeError):
    """A content-free resource authorization failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResourceReference:
    resource_id: str
    root_id: str | None = None
    relative_path: str | None = None
    handle_id: str | None = None

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.resource_id):
            raise ResourceAccessError("worker_resource_id_invalid")
        path_selected = self.root_id is not None or self.relative_path is not None
        handle_selected = self.handle_id is not None
        if path_selected == handle_selected or (
            path_selected and None in (self.root_id, self.relative_path)
        ):
            raise ResourceAccessError("worker_resource_reference_invalid")
        for value in (self.root_id, self.handle_id):
            if value is not None and not _SAFE_ID.fullmatch(value):
                raise ResourceAccessError("worker_resource_authority_invalid")


@dataclass(frozen=True, slots=True)
class AuthorizedResource:
    resource_id: str
    value: Path | int


class ApprovedResourcePolicy:
    """Immutable authorities selected by the trusted parent process."""

    def __init__(
        self,
        *,
        roots: Mapping[str, Path] = MappingProxyType({}),
        inherited_handles: Mapping[str, int] = MappingProxyType({}),
    ) -> None:
        canonical_roots: dict[str, Path] = {}
        for root_id, root in roots.items():
            if not _SAFE_ID.fullmatch(root_id) or not root.is_absolute():
                raise ResourceAccessError("worker_approved_root_invalid")
            try:
                canonical = root.resolve(strict=True)
                if not canonical.is_dir():
                    raise ResourceAccessError("worker_approved_root_invalid")
                assert_no_reparse_points(canonical, canonical)
            except (OSError, ReparsePointError) as exc:
                raise ResourceAccessError("worker_approved_root_invalid") from exc
            canonical_roots[root_id] = canonical
        handles: dict[str, int] = {}
        for handle_id, handle in inherited_handles.items():
            if not _SAFE_ID.fullmatch(handle_id) or isinstance(handle, bool) or handle < 0:
                raise ResourceAccessError("worker_approved_handle_invalid")
            handles[handle_id] = handle
        self._roots = MappingProxyType(canonical_roots)
        self._handles = MappingProxyType(handles)

    @property
    def inherited_handle_values(self) -> tuple[int, ...]:
        return tuple(self._handles.values())

    def authorize(self, reference: ResourceReference) -> Path | int:
        if reference.handle_id is not None:
            try:
                return self._handles[reference.handle_id]
            except KeyError as exc:
                raise ResourceAccessError("worker_handle_not_approved") from exc
        assert reference.root_id is not None and reference.relative_path is not None
        try:
            root = self._roots[reference.root_id]
        except KeyError as exc:
            raise ResourceAccessError("worker_root_not_approved") from exc
        raw = reference.relative_path
        candidate_relative = PurePath(raw)
        if (
            not raw
            or len(raw) > _MAX_RELATIVE_CHARS
            or "\x00" in raw
            or candidate_relative.is_absolute()
            or ".." in candidate_relative.parts
        ):
            raise ResourceAccessError("worker_relative_path_invalid")
        lexical = root.joinpath(*candidate_relative.parts)
        try:
            assert_no_reparse_points(root, lexical)
            canonical = lexical.resolve(strict=True)
            canonical.relative_to(root)
            assert_no_reparse_points(root, canonical)
        except (OSError, ValueError, ReparsePointError) as exc:
            raise ResourceAccessError("worker_path_escape_or_reparse") from exc
        if not canonical.is_file():
            raise ResourceAccessError("worker_resource_not_regular_file")
        return canonical

    def wire_reference(self, reference: ResourceReference) -> dict[str, object]:
        """Validate then return a path-free/root-relative wire description."""

        authorized = self.authorize(reference)
        if reference.handle_id is not None:
            del authorized
            return {
                "resource_id": reference.resource_id,
                "handle_id": reference.handle_id,
            }
        assert reference.root_id is not None and reference.relative_path is not None
        del authorized
        return {
            "resource_id": reference.resource_id,
            "root_id": reference.root_id,
            "relative_path": reference.relative_path,
        }

    def authorize_wire(self, raw: object) -> AuthorizedResource:
        """Revalidate an untrusted helper-protocol resource inside the worker."""

        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ResourceAccessError("worker_resource_reference_invalid")
        if set(raw) == {"resource_id", "handle_id"}:
            resource_id = raw["resource_id"]
            handle_id = raw["handle_id"]
            if not isinstance(resource_id, str) or not isinstance(handle_id, str):
                raise ResourceAccessError("worker_resource_reference_invalid")
            reference = ResourceReference(resource_id=resource_id, handle_id=handle_id)
        elif set(raw) == {"resource_id", "root_id", "relative_path"}:
            resource_id = raw["resource_id"]
            root_id = raw["root_id"]
            relative_path = raw["relative_path"]
            if not all(isinstance(value, str) for value in (resource_id, root_id, relative_path)):
                raise ResourceAccessError("worker_resource_reference_invalid")
            reference = ResourceReference(
                resource_id=resource_id,
                root_id=root_id,
                relative_path=relative_path,
            )
        else:
            raise ResourceAccessError("worker_resource_reference_invalid")
        return AuthorizedResource(
            resource_id=reference.resource_id, value=self.authorize(reference)
        )
