"""Explicit, contained writable-area paths for Kirby2 data.

``DataPaths`` is deliberately a path provider rather than a store.  Constructing or
validating one never creates filesystem entries.  Call :meth:`DataPaths.ensure` at
the write boundary for the exact areas a caller owns, and validate again immediately
before activating an artifact.  A returned :class:`~pathlib.Path` is only a location
label, not a pinned filesystem entry, so callers must revalidate at every later write
boundary.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType


DATA_PATHS_SCHEMA_VERSION = 2

_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())
_MKDIR_SUPPORTS_DIR_FD = os.mkdir in getattr(os, "supports_dir_fd", ())
_SCANDIR_SUPPORTS_FD = os.scandir in getattr(os, "supports_fd", ())


class DataAreaId(str, Enum):
    """Stable semantic identifiers for governed Kirby2 writable areas."""

    RUNS = "runs"
    EVIDENCE = "evidence"
    CHECKPOINTS = "checkpoints"
    PACKS = "packs"
    IDENTITY_MAPPINGS = "identity_mappings"
    CONFIG = "config"
    CACHE = "cache"
    STAGING = "staging"
    BACKUPS = "backups"
    DIAGNOSTICS = "diagnostics"
    RELEASE = "release"
    DATASETS = "datasets"
    LOGS = "logs"
    CRASH_REPORTS = "crash_reports"
    TEMPORARY = "temporary"
    EXPORTS = "exports"

    # Descriptive source-code aliases keep the semantic meaning visible without
    # changing the short, stable on-disk IDs used by WO31-40.
    INSTALLED_PACKS = "packs"
    CONFIGURATION = "config"
    RELEASE_ARTIFACTS = "release"
    USER_EXPORTS = "exports"


PACK_INSTALLATION_AREA_IDS = (DataAreaId.PACKS, DataAreaId.STAGING)
IMMUTABLE_EVIDENCE_AREA_IDS = (DataAreaId.RUNS, DataAreaId.EVIDENCE)
ERASABLE_IDENTITY_AREA_IDS = (DataAreaId.IDENTITY_MAPPINGS,)


_DEFAULT_AREA_CHILDREN: Mapping[DataAreaId, str] = MappingProxyType(
    {area_id: area_id.value for area_id in DataAreaId}
)


class DataPaths:
    """A versioned, non-aliasing map rooted at one explicit resolved directory.

    ``runs`` and ``evidence`` are reserved for immutable artifacts, ``packs`` for
    installed packs, and ``identity_mappings`` for separately erasable direct-
    identity material.  The remaining names describe their governed mutable or
    derived area.  Child overrides exist so platform adapters and hostile audits can
    construct the same semantic map without defining a second path provider.

    The supplied root must already be absolute and resolved, although it need not
    exist.  Existing symlinks in the root or any area chain are refused on every
    validation; callers therefore cannot silently rebind an area to another location
    between construction and a later write-boundary validation.
    """

    __slots__ = ("_area_children", "_areas", "_root")

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        area_children: Mapping[
            DataAreaId | str, str | os.PathLike[str]
        ]
        | None = None,
    ) -> None:
        self._root = _resolved_root(root)
        children = dict(_DEFAULT_AREA_CHILDREN)
        if area_children is not None:
            seen: set[DataAreaId] = set()
            for raw_area_id, raw_child in area_children.items():
                area_id = _coerce_area_id(raw_area_id)
                if area_id in seen:
                    raise ValueError(f"duplicate data area override: {area_id.value}")
                seen.add(area_id)
                children[area_id] = _validate_child(raw_child, area_id=area_id)

        # Validate defaults as well as overrides so future additions cannot weaken
        # the declaration contract accidentally.
        for area_id, child in tuple(children.items()):
            children[area_id] = _validate_child(child, area_id=area_id)
        _validate_non_aliasing(children)

        areas = {
            area_id: self._root.joinpath(*PurePosixPath(child).parts)
            for area_id, child in children.items()
        }
        self._area_children = MappingProxyType(children)
        self._areas = MappingProxyType(areas)
        self.validate()

    @property
    def root(self) -> Path:
        """The absolute, normalized root stored by this provider."""

        return self._root

    @property
    def area_ids(self) -> tuple[DataAreaId, ...]:
        """All governed area identifiers in stable declaration order."""

        return tuple(DataAreaId)

    @property
    def area_children(self) -> Mapping[DataAreaId, str]:
        """The immutable semantic-area to relative-child declaration."""

        return self._area_children

    @property
    def areas(self) -> Mapping[DataAreaId, Path]:
        """The immutable semantic-area to absolute-path map."""

        return self._areas

    @property
    def runs(self) -> Path:
        return self.area(DataAreaId.RUNS)

    @property
    def evidence(self) -> Path:
        return self.area(DataAreaId.EVIDENCE)

    @property
    def checkpoints(self) -> Path:
        return self.area(DataAreaId.CHECKPOINTS)

    @property
    def packs(self) -> Path:
        return self.area(DataAreaId.PACKS)

    @property
    def installed_packs(self) -> Path:
        return self.packs

    @property
    def identity_mappings(self) -> Path:
        return self.area(DataAreaId.IDENTITY_MAPPINGS)

    @property
    def config(self) -> Path:
        return self.area(DataAreaId.CONFIG)

    @property
    def configuration(self) -> Path:
        return self.config

    @property
    def cache(self) -> Path:
        return self.area(DataAreaId.CACHE)

    @property
    def staging(self) -> Path:
        return self.area(DataAreaId.STAGING)

    @property
    def backups(self) -> Path:
        return self.area(DataAreaId.BACKUPS)

    @property
    def diagnostics(self) -> Path:
        return self.area(DataAreaId.DIAGNOSTICS)

    @property
    def release(self) -> Path:
        return self.area(DataAreaId.RELEASE)

    @property
    def release_artifacts(self) -> Path:
        return self.release

    @property
    def datasets(self) -> Path:
        return self.area(DataAreaId.DATASETS)

    @property
    def logs(self) -> Path:
        return self.area(DataAreaId.LOGS)

    @property
    def crash_reports(self) -> Path:
        return self.area(DataAreaId.CRASH_REPORTS)

    @property
    def temporary(self) -> Path:
        return self.area(DataAreaId.TEMPORARY)

    @property
    def exports(self) -> Path:
        return self.area(DataAreaId.EXPORTS)

    @property
    def user_exports(self) -> Path:
        return self.exports

    def area(self, area_id: DataAreaId | str) -> Path:
        """Return one declared area path without touching the filesystem."""

        return self._areas[_coerce_area_id(area_id)]

    def validate(
        self,
        area_ids: Iterable[DataAreaId | str] | DataAreaId | str | None = None,
    ) -> None:
        """Refuse escaped, rebound, aliased, or non-directory area paths.

        Missing directories are valid because validation is non-writing.  Existing
        nodes from the filesystem anchor through each selected area must all be real
        directories rather than symlinks or files.
        """

        selected = self.area_ids if area_ids is None else _ordered_area_ids(area_ids)
        _validate_non_aliasing(self._area_children)
        _validate_directory_chain(self._root)
        for area_id in selected:
            area_path = self._areas[area_id]
            try:
                resolved = area_path.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise ValueError(
                    f"data area cannot be resolved safely: {area_id.value}"
                ) from error
            if not _is_within(resolved, self._root):
                raise ValueError(f"data area escapes its root: {area_id.value}")
            _validate_directory_chain(area_path)

    def ensure(
        self,
        area_ids: Iterable[DataAreaId | str] | DataAreaId | str,
    ) -> tuple[Path, ...]:
        """Create only the requested areas and their required parent directories.

        The creation walk is descriptor-relative from the filesystem anchor and
        refuses to follow symlinks at every component.  The return value follows
        stable :class:`DataAreaId` declaration order, even if the caller supplied a
        set or another unordered iterable.  An empty request writes nothing,
        including the root.

        Directory descriptors are closed before return.  Consequently, returned
        :class:`~pathlib.Path` values do not pin the validated entries; a caller must
        call :meth:`validate` again immediately before every later write or artifact
        activation boundary.
        """

        selected = _ordered_area_ids(area_ids)
        self.validate()
        if not selected:
            return ()

        open_flags = _safe_directory_open_flags()
        for area_id in selected:
            _ensure_directory_chain_fd(self._areas[area_id], open_flags=open_flags)

        # Detect a rebind of a pathname after its pinned creation walk before
        # returning any unpinned write target.
        self.validate()
        return tuple(self._areas[area_id] for area_id in selected)

    def ensure_pack_installation_areas(self) -> tuple[Path, Path]:
        """Create the one installed-pack/staging pair owned by WO39-C."""

        installed, staging = self.ensure(PACK_INSTALLATION_AREA_IDS)
        return installed, staging

    def as_dict(self) -> dict[str, object]:
        """Return a deterministic inspection payload; paths are display metadata."""

        return {
            "areas": {
                area_id.value: str(self._areas[area_id]) for area_id in DataAreaId
            },
            "root": str(self._root),
            "schema_version": DATA_PATHS_SCHEMA_VERSION,
        }


def _resolved_root(root: str | os.PathLike[str]) -> Path:
    try:
        path = Path(root)
    except TypeError as error:
        raise TypeError("data root must be a string or path-like value") from error
    if not path.is_absolute():
        raise ValueError("data root must be explicit and absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("data root cannot be resolved safely") from error
    if path != resolved:
        raise ValueError("data root must be supplied in already-resolved form")
    if resolved == Path(resolved.anchor):
        raise ValueError("filesystem anchor cannot be used as the data root")
    return resolved


def _coerce_area_id(area_id: DataAreaId | str) -> DataAreaId:
    if isinstance(area_id, DataAreaId):
        return area_id
    if type(area_id) is not str:
        raise TypeError("data area ID must be a DataAreaId or string")
    try:
        return DataAreaId(area_id)
    except ValueError as error:
        raise ValueError(f"unknown data area ID: {area_id!r}") from error


def _ordered_area_ids(
    area_ids: Iterable[DataAreaId | str] | DataAreaId | str,
) -> tuple[DataAreaId, ...]:
    if isinstance(area_ids, (DataAreaId, str)):
        raw_ids: Iterable[DataAreaId | str] = (area_ids,)
    else:
        try:
            raw_ids = iter(area_ids)
        except TypeError as error:
            raise TypeError("data area IDs must be iterable") from error
    selected: set[DataAreaId] = set()
    for raw_area_id in raw_ids:
        area_id = _coerce_area_id(raw_area_id)
        if area_id in selected:
            raise ValueError(f"duplicate data area ID: {area_id.value}")
        selected.add(area_id)
    return tuple(area_id for area_id in DataAreaId if area_id in selected)


def _validate_child(
    child: str | os.PathLike[str],
    *,
    area_id: DataAreaId,
) -> str:
    try:
        raw = os.fspath(child)
    except TypeError as error:
        raise TypeError(
            f"data area child must be a string or path-like value: {area_id.value}"
        ) from error
    if type(raw) is not str:
        raise TypeError(f"data area child must be text: {area_id.value}")
    if not raw or "\x00" in raw:
        raise ValueError(f"data area child is empty or invalid: {area_id.value}")

    if "\\" in raw:
        raise ValueError(
            f"data area child must use canonical POSIX separators: {area_id.value}"
        )
    raw_parts = raw.split("/")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
    ):
        raise ValueError(f"data area child must be relative: {area_id.value}")
    if "" in raw_parts or "." in raw_parts or ".." in raw_parts:
        raise ValueError(
            f"data area child must be canonical and traversal-free: {area_id.value}"
        )
    if posix == PurePosixPath("."):
        raise ValueError(f"data area child must name a directory: {area_id.value}")
    return posix.as_posix()


def _validate_non_aliasing(children: Mapping[DataAreaId, str]) -> None:
    if set(children) != set(DataAreaId):
        raise ValueError("data area declaration must contain every stable area ID")
    declarations = tuple(
        (
            area_id,
            PurePosixPath(children[area_id]).parts,
            _portable_parts(PurePosixPath(children[area_id]).parts),
        )
        for area_id in DataAreaId
    )
    for index, (left_id, left_parts, left_portable) in enumerate(declarations):
        for right_id, right_parts, right_portable in declarations[index + 1 :]:
            if _is_prefix(left_parts, right_parts) or _is_prefix(
                right_parts, left_parts
            ):
                raise ValueError(
                    "data areas must not alias or contain one another: "
                    f"{left_id.value}, {right_id.value}"
                )
            if _is_prefix(left_portable, right_portable) or _is_prefix(
                right_portable, left_portable
            ):
                raise ValueError(
                    "data areas collide under case or Unicode normalization: "
                    f"{left_id.value}, {right_id.value}"
                )


def _portable_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize(
            "NFC", unicodedata.normalize("NFC", part).casefold()
        )
        for part in parts
    )


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_directory_open_flags() -> int:
    """Return required no-follow directory flags or fail before any creation."""

    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise RuntimeError(
            "safe data-directory creation requires O_DIRECTORY and O_NOFOLLOW"
        )
    if (
        not _OPEN_SUPPORTS_DIR_FD
        or not _MKDIR_SUPPORTS_DIR_FD
        or not _SCANDIR_SUPPORTS_FD
    ):
        raise RuntimeError(
            "safe data-directory creation requires fd-relative open/mkdir/scandir"
        )

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _ensure_directory_chain_fd(path: Path, *, open_flags: int) -> None:
    """Create/open one absolute directory chain without following rebound names."""

    if not path.is_absolute():  # pragma: no cover - guarded by DataPaths
        raise ValueError("governed data paths must be absolute")
    anchor = Path(path.anchor)
    try:
        current_fd = os.open(anchor, open_flags)
    except (OSError, TypeError, NotImplementedError) as error:
        raise ValueError(
            f"filesystem anchor cannot be opened safely: {anchor}"
        ) from error

    current_path = anchor
    try:
        for part in path.parts[1:]:
            _reject_portable_sibling_alias_fd(
                current_fd,
                part,
                parent_path=current_path,
            )
            next_path = current_path / part
            next_fd = _open_or_create_directory_at(
                current_fd,
                part,
                open_flags=open_flags,
                display_path=next_path,
            )
            try:
                # A case-sensitive filesystem may admit a portable alias during the
                # mkdir/open race.  Check again through the still-pinned parent.
                _reject_portable_sibling_alias_fd(
                    current_fd,
                    part,
                    parent_path=current_path,
                )
            except BaseException:
                os.close(next_fd)
                raise
            previous_fd = current_fd
            current_fd = next_fd
            os.close(previous_fd)
            current_path = next_path
    finally:
        os.close(current_fd)


def _open_or_create_directory_at(
    parent_fd: int,
    name: str,
    *,
    open_flags: int,
    display_path: Path,
) -> int:
    """Open one child directory by descriptor, creating only that exact name."""

    try:
        return os.open(name, open_flags, dir_fd=parent_fd)
    except FileNotFoundError:
        created = False
        try:
            os.mkdir(name, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            # A concurrent creator won the race.  The no-follow open below decides
            # whether the resulting entry is the required real directory.
            pass
        except (OSError, TypeError, NotImplementedError) as error:
            raise ValueError(
                "directory cannot be created safely at governed data path: "
                f"{display_path}"
            ) from error
        if created:
            try:
                os.fsync(parent_fd)
            except OSError as error:
                raise ValueError(
                    "directory creation cannot be made durable at governed data "
                    f"path: {display_path}"
                ) from error
        try:
            return os.open(name, open_flags, dir_fd=parent_fd)
        except (OSError, TypeError, NotImplementedError) as error:
            raise ValueError(
                f"directory required at governed data path: {display_path}"
            ) from error
    except (OSError, TypeError, NotImplementedError) as error:
        raise ValueError(
            f"directory required at governed data path: {display_path}"
        ) from error


def _reject_portable_sibling_alias_fd(
    parent_fd: int,
    intended_name: str,
    *,
    parent_path: Path,
) -> None:
    """Check portable aliases through an already-open, pinned parent directory."""

    intended_key = _portable_name(intended_name)
    try:
        with os.scandir(parent_fd) as entries:
            matches = tuple(
                entry.name
                for entry in entries
                if _portable_name(entry.name) == intended_key
            )
    except (OSError, TypeError, NotImplementedError) as error:
        raise ValueError(
            f"governed data directory cannot be inspected safely: {parent_path}"
        ) from error
    if not matches:
        return
    if matches != (intended_name,):
        rendered = ", ".join(sorted(repr(name) for name in matches))
        raise ValueError(
            "governed data path collides with an existing case/Unicode alias: "
            f"intended={intended_name!r} existing={rendered}"
        )


def _validate_directory_chain(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("governed data paths must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        _reject_portable_sibling_alias(current, part)
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # A descendant cannot exist beneath a missing component.  Continuing is
            # unnecessary and avoids falsely requiring construction during validate.
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"governed data path must not contain symlinks: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"directory required at governed data path: {current}")


def _reject_portable_sibling_alias(parent: Path, intended_name: str) -> None:
    intended_key = _portable_name(intended_name)
    try:
        with os.scandir(parent) as entries:
            sibling_names = tuple(entry.name for entry in entries)
    except FileNotFoundError:
        return
    matches = tuple(
        name for name in sibling_names if _portable_name(name) == intended_key
    )
    if not matches:
        return
    if matches != (intended_name,):
        rendered = ", ".join(sorted(repr(name) for name in matches))
        raise ValueError(
            "governed data path collides with an existing case/Unicode alias: "
            f"intended={intended_name!r} existing={rendered}"
        )


def _portable_name(name: str) -> str:
    return unicodedata.normalize(
        "NFC", unicodedata.normalize("NFC", name).casefold()
    )


__all__ = [
    "DATA_PATHS_SCHEMA_VERSION",
    "ERASABLE_IDENTITY_AREA_IDS",
    "IMMUTABLE_EVIDENCE_AREA_IDS",
    "PACK_INSTALLATION_AREA_IDS",
    "DataAreaId",
    "DataPaths",
]
