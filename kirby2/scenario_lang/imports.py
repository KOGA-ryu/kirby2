"""Confined, deterministic source-import loading for scenario language V1."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kirby2.research.toml_codec import canonical_toml

from .identity import SourceBundleEntryV1, source_bundle_digest
from .models import (
    ScenarioImportBundleV1,
    ScenarioImportEdgeV1,
    ScenarioImportV1,
    ScenarioSourceDocumentV1,
    ScenarioSourceOriginV1,
    ScenarioSourceV1,
)
from .schema import parse_scenario_source


DEFAULT_SCENARIO_IMPORT_MAX_DEPTH = 16
DEFAULT_SCENARIO_IMPORT_MAX_DOCUMENTS = 128
DEFAULT_SCENARIO_IMPORT_MAX_EXPANDED_BYTES = 8 * 1024 * 1024

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_PACK_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


@dataclass(frozen=True, slots=True)
class ScenarioImportLimitsV1:
    maximum_depth: int = DEFAULT_SCENARIO_IMPORT_MAX_DEPTH
    maximum_documents: int = DEFAULT_SCENARIO_IMPORT_MAX_DOCUMENTS
    maximum_expanded_bytes: int = DEFAULT_SCENARIO_IMPORT_MAX_EXPANDED_BYTES

    def __post_init__(self) -> None:
        for name in (
            "maximum_depth",
            "maximum_documents",
            "maximum_expanded_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"scenario import {name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class _ImportAuthority:
    origin: ScenarioSourceOriginV1
    namespace: str | None
    root: Path


@dataclass(frozen=True, slots=True)
class _DocumentLocation:
    authority: _ImportAuthority
    canonical_path: Path
    logical_path: str


def validate_scenario_import_path(path: str) -> PurePosixPath:
    """Validate one lexical relative TOML path without touching the filesystem."""

    if type(path) is not str or not path:
        raise ValueError("scenario import path must be a nonempty string")
    if "\x00" in path:
        raise ValueError("scenario import path must not contain NUL")
    if unicodedata.normalize("NFC", path) != path:
        raise ValueError("scenario import path must be NFC-normalized")
    if "\\" in path:
        raise ValueError("scenario import path must use POSIX separators")
    if path.startswith("//") or _WINDOWS_DRIVE.match(path):
        raise ValueError("Windows drive and UNC scenario imports are forbidden")
    if _URI_SCHEME.match(path):
        raise ValueError("URL and URI scenario imports are forbidden")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError("absolute scenario import paths are forbidden")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("scenario import path must not traverse or contain empty parts")
    if pure.as_posix() != path:
        raise ValueError("scenario import path must be canonical POSIX text")
    if pure.suffix != ".toml":
        raise ValueError("scenario imports must reference lowercase .toml files")
    return pure


def parse_scenario_source_document(
    raw: bytes,
) -> tuple[ScenarioSourceV1, tuple[ScenarioImportV1, ...]]:
    """Extract strict root import declarations, then parse the WO32-A source body."""

    if type(raw) is not bytes:
        raise TypeError("scenario source document must be exact bytes")
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("scenario source document must be strict UTF-8 TOML") from error
    if not isinstance(payload, dict):
        raise ValueError("scenario source document root must be a table")
    raw_imports = payload.pop("imports", [])
    if type(raw_imports) is not list:
        raise TypeError("scenario source imports must be an array")
    imports = tuple(_parse_import(item) for item in raw_imports)
    source = parse_scenario_source(canonical_toml(payload).encode("utf-8"))
    return source, imports


def resolve_scenario_import_bundle(
    source_root: Path,
    entry_path: str,
    *,
    activated_pack_namespaces: Mapping[str, Path] | None = None,
    limits: ScenarioImportLimitsV1 = ScenarioImportLimitsV1(),
) -> ScenarioImportBundleV1:
    """Resolve one complete source graph beneath explicitly activated authorities."""

    resolver = ScenarioImportResolver(
        source_root,
        activated_pack_namespaces=activated_pack_namespaces,
        limits=limits,
    )
    return resolver.resolve(entry_path)


class ScenarioImportResolver:
    """Read-only DFS resolver with closed roots and deterministic traversal order."""

    def __init__(
        self,
        source_root: Path,
        *,
        activated_pack_namespaces: Mapping[str, Path] | None = None,
        limits: ScenarioImportLimitsV1 = ScenarioImportLimitsV1(),
    ) -> None:
        if type(limits) is not ScenarioImportLimitsV1:
            raise TypeError("scenario import limits use the wrong V1 contract")
        self._limits = limits
        self._source_authority = _activate_authority(
            ScenarioSourceOriginV1.SOURCE_ROOT,
            None,
            source_root,
        )
        packs = activated_pack_namespaces or {}
        if not isinstance(packs, Mapping):
            raise TypeError("activated scenario packs must be a mapping")
        normalized_names: dict[str, str] = {}
        authorities: dict[str, _ImportAuthority] = {}
        for namespace, root in sorted(packs.items()):
            _validate_pack_namespace(namespace)
            collision_key = _logical_collision_key(namespace)
            previous = normalized_names.get(collision_key)
            if previous is not None and previous != namespace:
                raise ValueError("activated pack namespaces have a case/Unicode collision")
            normalized_names[collision_key] = namespace
            authorities[namespace] = _activate_authority(
                ScenarioSourceOriginV1.PACK_NAMESPACE,
                namespace,
                root,
            )
        self._pack_authorities = authorities

    def resolve(self, entry_path: str) -> ScenarioImportBundleV1:
        entry = validate_scenario_import_path(entry_path)
        root_location = _locate(
            self._source_authority,
            self._source_authority.root,
            entry,
        )
        documents: list[ScenarioSourceDocumentV1] = []
        edges: list[ScenarioImportEdgeV1] = []
        raw_members: list[SourceBundleEntryV1] = []
        seen_canonical_paths: set[Path] = set()
        active_canonical_paths: set[Path] = set()
        logical_collision_paths: dict[str, str] = {}
        expanded_bytes = 0

        def visit(location: _DocumentLocation, depth: int) -> None:
            nonlocal expanded_bytes
            if depth > self._limits.maximum_depth:
                raise ValueError("scenario import graph exceeds maximum depth")
            canonical_path = location.canonical_path
            if canonical_path in active_canonical_paths:
                raise ValueError("scenario import graph contains a cycle")
            if canonical_path in seen_canonical_paths:
                raise ValueError("scenario import graph repeats a canonical path")
            collision_key = _logical_collision_key(location.logical_path)
            previous = logical_collision_paths.get(collision_key)
            if previous is not None and previous != location.logical_path:
                raise ValueError("scenario import paths have a case/Unicode collision")
            if len(documents) >= self._limits.maximum_documents:
                raise ValueError("scenario import graph exceeds maximum document count")
            remaining = self._limits.maximum_expanded_bytes - expanded_bytes
            raw = _read_bounded(canonical_path, remaining)
            expanded_bytes += len(raw)
            source, imports = parse_scenario_source_document(raw)
            document = ScenarioSourceDocumentV1(
                logical_path=location.logical_path,
                origin=location.authority.origin,
                pack_namespace=location.authority.namespace,
                source=source,
                imports=imports,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                raw_byte_count=len(raw),
            )
            documents.append(document)
            raw_members.append(SourceBundleEntryV1(location.logical_path, raw))
            seen_canonical_paths.add(canonical_path)
            active_canonical_paths.add(canonical_path)
            logical_collision_paths[collision_key] = location.logical_path
            try:
                for ordinal, requested in enumerate(imports):
                    imported = self._resolve_import(location, requested)
                    edges.append(
                        ScenarioImportEdgeV1(
                            location.logical_path,
                            imported.logical_path,
                            ordinal,
                        )
                    )
                    visit(imported, depth + 1)
            finally:
                active_canonical_paths.remove(canonical_path)

        visit(root_location, 0)
        return ScenarioImportBundleV1(
            root_logical_path=root_location.logical_path,
            documents=tuple(documents),
            edges=tuple(edges),
            source_bundle_digest=source_bundle_digest(tuple(raw_members)),
            expanded_byte_count=expanded_bytes,
        )

    def _resolve_import(
        self,
        importer: _DocumentLocation,
        requested: ScenarioImportV1,
    ) -> _DocumentLocation:
        relative = validate_scenario_import_path(requested.path)
        if requested.pack_namespace is None:
            authority = importer.authority
            base = importer.canonical_path.parent
        else:
            try:
                authority = self._pack_authorities[requested.pack_namespace]
            except KeyError as error:
                raise ValueError(
                    "scenario import references an unactivated pack namespace"
                ) from error
            base = authority.root
        return _locate(authority, base, relative)


def _parse_import(value: object) -> ScenarioImportV1:
    if not isinstance(value, Mapping):
        raise TypeError("scenario import entry must be an inline table")
    actual = set(value)
    allowed = {"pack_namespace", "path"}
    if "path" not in actual or not actual.issubset(allowed):
        raise ValueError(
            "scenario import fields are not exact: "
            f"missing={sorted({'path'} - actual)} "
            f"unknown={sorted(actual - allowed)}"
        )
    path = value["path"]
    namespace = value.get("pack_namespace")
    if type(path) is not str:
        raise TypeError("scenario import path must be a string")
    if namespace is not None and type(namespace) is not str:
        raise TypeError("scenario pack namespace must be a string")
    validate_scenario_import_path(path)
    if namespace is not None:
        _validate_pack_namespace(namespace)
    return ScenarioImportV1(path, namespace)


def _activate_authority(
    origin: ScenarioSourceOriginV1,
    namespace: str | None,
    root: Path,
) -> _ImportAuthority:
    if not isinstance(root, Path):
        raise TypeError("scenario source roots must be explicit Path objects")
    if root.is_symlink():
        raise ValueError("scenario source authority root must not be a symlink")
    try:
        canonical = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ValueError("scenario source authority root does not exist") from error
    if not canonical.is_dir():
        raise ValueError("scenario source authority root must be a directory")
    return _ImportAuthority(origin, namespace, canonical)


def _locate(
    authority: _ImportAuthority,
    base: Path,
    relative: PurePosixPath,
) -> _DocumentLocation:
    candidate = base.joinpath(*relative.parts)
    try:
        canonical = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ValueError("scenario import target does not exist") from error
    if not canonical.is_relative_to(authority.root):
        raise ValueError("scenario import symlink escapes its activated root")
    if not canonical.is_file():
        raise ValueError("scenario import target must be a regular file")
    relative_canonical = canonical.relative_to(authority.root).as_posix()
    if authority.origin is ScenarioSourceOriginV1.SOURCE_ROOT:
        logical_path = f"source-root:{relative_canonical}"
    else:
        logical_path = f"pack:{authority.namespace}:{relative_canonical}"
    return _DocumentLocation(authority, canonical, logical_path)


def _read_bounded(path: Path, remaining_bytes: int) -> bytes:
    if remaining_bytes <= 0:
        raise ValueError("scenario import graph exceeds maximum expanded bytes")
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("scenario import target must be a regular file")
            raw = stream.read(remaining_bytes + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise ValueError("scenario import target could not be read") from error
    if len(raw) > remaining_bytes:
        raise ValueError("scenario import graph exceeds maximum expanded bytes")
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or after.st_size != len(raw):
        raise ValueError("scenario import target changed while it was read")
    return raw


def _validate_pack_namespace(namespace: object) -> str:
    if type(namespace) is not str or _PACK_NAMESPACE.fullmatch(namespace) is None:
        raise ValueError("scenario pack namespace is not a canonical identifier")
    if unicodedata.normalize("NFC", namespace) != namespace:
        raise ValueError("scenario pack namespace must be NFC-normalized")
    return namespace


def _logical_collision_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


__all__ = [
    "DEFAULT_SCENARIO_IMPORT_MAX_DEPTH",
    "DEFAULT_SCENARIO_IMPORT_MAX_DOCUMENTS",
    "DEFAULT_SCENARIO_IMPORT_MAX_EXPANDED_BYTES",
    "ScenarioImportLimitsV1",
    "ScenarioImportResolver",
    "parse_scenario_source_document",
    "resolve_scenario_import_bundle",
    "validate_scenario_import_path",
]
