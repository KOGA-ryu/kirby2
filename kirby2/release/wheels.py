"""Closed, byte-oriented wheel verification for the offline release builder.

The project wheel is not trusted merely because the pinned frontend produced a
``.whl`` file.  This module verifies its ZIP representation, normalized metadata,
RECORD ledger, and complete candidate-derived payload.  Locked third-party wheels
are read once through no-follow descriptors, matched to the committed lock, and
structurally checked before their exact captured bytes enter an archive plan.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import stat
import struct
import tomllib
import unicodedata
import zipfile
from dataclasses import InitVar, dataclass
from email.parser import BytesParser
from email.policy import compat32
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Mapping

from kirby2.packs.formats import canonical_json_bytes, require_sha256

from .licenses import (
    LockedReleaseWheelV1,
    ReleaseRequirementsLockV1,
    extract_locked_license,
)
from .packaging import normalize_release_path


PROJECT_WHEEL_FILENAME_V1 = "kirby2-0.1.0-py3-none-any.whl"
PROJECT_DIST_INFO_ROOT_V1 = "kirby2-0.1.0.dist-info"
PROJECT_WHEEL_MAX_MEMBERS_V1 = 4096
PROJECT_WHEEL_MAX_EXPANDED_BYTES_V1 = 128 * 1024 * 1024
DEPENDENCY_WHEEL_MAX_MEMBERS_V1 = 16384
DEPENDENCY_WHEEL_MAX_EXPANDED_BYTES_V1 = 2 * 1024 * 1024 * 1024

_DIST_INFO_MEMBERS_V1 = (
    f"{PROJECT_DIST_INFO_ROOT_V1}/METADATA",
    f"{PROJECT_DIST_INFO_ROOT_V1}/WHEEL",
    f"{PROJECT_DIST_INFO_ROOT_V1}/entry_points.txt",
    f"{PROJECT_DIST_INFO_ROOT_V1}/top_level.txt",
    f"{PROJECT_DIST_INFO_ROOT_V1}/RECORD",
)
_PROJECT_METADATA_V1 = (
    b"Metadata-Version: 2.4\n"
    b"Name: kirby2\n"
    b"Version: 0.1.0\n"
    b"Summary: Deterministic market-execution training sandbox\n"
    b"Requires-Python: >=3.11\n"
    b"Requires-Dist: duckdb<2,>=1.5\n"
)
_PROJECT_WHEEL_V1 = (
    b"Wheel-Version: 1.0\n"
    b"Generator: setuptools (80.9.0)\n"
    b"Root-Is-Purelib: true\n"
    b"Tag: py3-none-any\n"
    b"\n"
)
_PROJECT_ENTRY_POINTS_V1 = (
    b"[console_scripts]\n"
    b"kirby2 = kirby2.__main__:main\n"
    b"kirby2-desktop = kirby2.release.desktop:main\n"
    b"kirby2-headless = kirby2.release.headless:main\n"
)
_PROJECT_TOP_LEVEL_V1 = b"kirby2\n"
_EOCD_SIGNATURE = b"PK\x05\x06"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_VERIFIED_WHEEL_CONSTRUCTION_TOKEN_V1 = object()


@dataclass(frozen=True, slots=True)
class WheelMemberIdentityV1:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _wheel_member_path(self.path)
        if type(self.size) is not int or self.size < 0:
            raise ValueError("wheel member size must be nonnegative")
        require_sha256(self.sha256, "wheel member digest")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class VerifiedWheelV1:
    filename: str
    wheel_bytes: bytes
    name: str
    version: str
    tags: tuple[str, ...]
    size: int
    sha256: str
    members: tuple[WheelMemberIdentityV1, ...]
    record_sha256: str
    verification_token: InitVar[object] = None

    def __post_init__(self, verification_token: object) -> None:
        if verification_token is not _VERIFIED_WHEEL_CONSTRUCTION_TOKEN_V1:
            raise ValueError("verified wheels can only be constructed by a verifier")
        if (
            type(self.filename) is not str
            or not self.filename.endswith(".whl")
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ValueError("verified wheel filename is invalid")
        if type(self.wheel_bytes) is not bytes or not self.wheel_bytes:
            raise ValueError("verified wheel transport bytes are empty")
        if type(self.name) is not str or not self.name:
            raise ValueError("verified wheel name is invalid")
        if type(self.version) is not str or not self.version:
            raise ValueError("verified wheel version is invalid")
        if (
            type(self.tags) is not tuple
            or not self.tags
            or any(type(item) is not str or not item for item in self.tags)
            or len(self.tags) != len(set(self.tags))
        ):
            raise ValueError("verified wheel requires at least one tag")
        if type(self.size) is not int or self.size <= 0:
            raise ValueError("verified wheel size must be positive")
        require_sha256(self.sha256, "verified wheel digest")
        require_sha256(self.record_sha256, "wheel RECORD digest")
        if (
            type(self.members) is not tuple
            or not self.members
            or any(type(item) is not WheelMemberIdentityV1 for item in self.members)
            or len({item.path for item in self.members}) != len(self.members)
        ):
            raise ValueError("verified wheel member inventory is empty")
        if len(self.wheel_bytes) != self.size or hashlib.sha256(
            self.wheel_bytes
        ).hexdigest() != self.sha256:
            raise ValueError("verified wheel transport identity differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "members": [item.as_dict() for item in self.members],
            "name": self.name,
            "record_sha256": self.record_sha256,
            "sha256": self.sha256,
            "size": self.size,
            "tags": list(self.tags),
            "version": self.version,
        }

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class LockedWheelPayloadV1:
    locked: LockedReleaseWheelV1
    verified: VerifiedWheelV1
    wheel_bytes: bytes
    license_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.locked) is not LockedReleaseWheelV1:
            raise TypeError("locked wheel payload requires a lock row")
        if type(self.verified) is not VerifiedWheelV1:
            raise TypeError("locked wheel payload requires a verified wheel")
        if type(self.wheel_bytes) is not bytes or type(self.license_bytes) is not bytes:
            raise TypeError("locked wheel payloads must be exact bytes")
        if (
            self.verified.filename != self.locked.filename
            or self.verified.sha256 != self.locked.sha256
            or self.wheel_bytes != self.verified.wheel_bytes
            or hashlib.sha256(self.wheel_bytes).hexdigest() != self.locked.sha256
            or len(self.license_bytes) != self.locked.license_size
            or hashlib.sha256(self.license_bytes).hexdigest()
            != self.locked.license_sha256
        ):
            raise ValueError("locked wheel payload identities differ")

    @property
    def target(self) -> str:
        return self.locked.target

    @property
    def filename(self) -> str:
        return self.locked.filename


def _wheel_member_path(path: object) -> str:
    if type(path) is not str or not path or path.endswith("/"):
        raise ValueError("wheel members must be nonempty regular-file paths")
    normalize_release_path(path, label="wheel member path")
    if unicodedata.normalize("NFC", path) != path:
        raise ValueError("wheel member path is not NFC")
    return path


def _zip_eocd(raw: bytes) -> tuple[int, int, int]:
    if len(raw) < 22 or raw[-22:-18] != _EOCD_SIGNATURE:
        raise ValueError("wheel ZIP must end with one comment-free EOCD record")
    (
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack("<HHHHIIH", raw[-18:])
    if (
        disk_number != 0
        or central_disk != 0
        or disk_entries != total_entries
        or comment_size != 0
        or central_offset + central_size != len(raw) - 22
        or b"PK\x06\x06" in raw
        or b"PK\x06\x07" in raw
    ):
        raise ValueError("wheel ZIP uses a split, ZIP64, prefixed, or trailed container")
    return total_entries, central_offset, central_size


def _read_zip_payloads(
    raw: bytes,
    *,
    maximum_members: int,
    maximum_expanded_bytes: int,
    exact_project_representation: bool,
    source_date_epoch: int | None = None,
    allow_directories: bool = False,
) -> tuple[tuple[zipfile.ZipInfo, ...], dict[str, bytes]]:
    if type(raw) is not bytes or not raw.startswith(_LOCAL_SIGNATURE):
        raise ValueError("wheel is not an exact ZIP stream starting at byte zero")
    total_entries, central_offset, _ = _zip_eocd(raw)
    if total_entries <= 0 or total_entries > maximum_members:
        raise ValueError("wheel ZIP member count is outside the release bound")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), mode="r")
    except zipfile.BadZipFile as error:
        raise ValueError("wheel is not a valid ZIP container") from error
    with archive:
        if archive.comment or archive.start_dir != central_offset:
            raise ValueError("wheel ZIP central directory is noncanonical")
        infos = tuple(archive.infolist())
        if len(infos) != total_entries:
            raise ValueError("wheel ZIP directory entry count differs")
        names: list[str] = []
        folded: set[str] = set()
        total_size = 0
        expected_timestamp = None
        if source_date_epoch is not None:
            import datetime as _datetime

            timestamp = _datetime.datetime.fromtimestamp(
                source_date_epoch, tz=_datetime.timezone.utc
            )
            expected_timestamp = (
                timestamp.year,
                timestamp.month,
                timestamp.day,
                timestamp.hour,
                timestamp.minute,
                timestamp.second - (timestamp.second % 2),
            )
        for info in infos:
            if (
                info.orig_filename != info.filename
                or "\x00" in info.orig_filename
            ):
                raise ValueError("wheel ZIP member name was altered during decoding")
            is_directory = info.is_dir()
            if is_directory:
                if not allow_directories or not info.filename.endswith("/"):
                    raise ValueError("wheel ZIP contains a directory member")
                path = normalize_release_path(
                    info.filename[:-1], label="wheel directory path"
                ) + "/"
            else:
                path = _wheel_member_path(info.filename)
            folded_path = path.rstrip("/").casefold()
            if path in names or folded_path in folded:
                raise ValueError("wheel ZIP member paths are not portable-unique")
            names.append(path)
            folded.add(folded_path)
            if info.flag_bits & 0x1:
                raise ValueError("wheel ZIP contains an encrypted member")
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError("wheel ZIP member has a negative size")
            total_size += info.file_size
            if total_size > maximum_expanded_bytes:
                raise ValueError("wheel ZIP expanded size exceeds the release bound")
            if info.file_size > 0 and info.compress_size == 0:
                raise ValueError("wheel ZIP member has an invalid compression ratio")
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            allowed_types = {0, stat.S_IFREG} | (
                {stat.S_IFDIR} if allow_directories else set()
            )
            if file_type not in allowed_types:
                raise ValueError("wheel ZIP contains a non-regular member")
            if is_directory and (
                file_type != stat.S_IFDIR
                or info.file_size != 0
                or info.compress_size != 0
            ):
                raise ValueError("wheel ZIP directory representation differs")
            if exact_project_representation:
                if info.compress_type != zipfile.ZIP_DEFLATED:
                    raise ValueError("project wheel member compression differs")
                if info.flag_bits not in {0, 0x800}:
                    raise ValueError("project wheel member flags differ")
                if info.extra or info.comment or info.create_system != 3:
                    raise ValueError("project wheel member metadata differs")
                if expected_timestamp is None or info.date_time != expected_timestamp:
                    raise ValueError("project wheel member timestamp differs")
                permissions = stat.S_IMODE(mode)
                expected_permissions = (
                    0o664 if path.endswith(".dist-info/RECORD") else 0o644
                )
                if file_type != stat.S_IFREG or permissions != expected_permissions:
                    raise ValueError("project wheel member mode differs")

        ordered_by_offset = tuple(sorted(infos, key=lambda item: item.header_offset))
        if ordered_by_offset != infos:
            raise ValueError("wheel local-header and central-directory orders differ")
        offset = 0
        for info in ordered_by_offset:
            if info.header_offset != offset or raw[offset : offset + 4] != _LOCAL_SIGNATURE:
                raise ValueError("wheel ZIP has a prefix, gap, or overlapping local entry")
            if offset + 30 > central_offset:
                raise ValueError("wheel ZIP local header is truncated")
            (
                local_version,
                flags,
                compression,
                local_time,
                local_date,
                crc,
                compressed_size,
                file_size,
                name_size,
                extra_size,
            ) = struct.unpack("<HHHHHIIIHH", raw[offset + 4 : offset + 30])
            name_start = offset + 30
            name_end = name_start + name_size
            data_start = name_end + extra_size
            data_end = data_start + compressed_size
            year, month, day, hour, minute, second = info.date_time
            central_time = (hour << 11) | (minute << 5) | (second // 2)
            central_date = ((year - 1980) << 9) | (month << 5) | day
            if (
                raw[name_start:name_end] != info.orig_filename.encode("utf-8")
                or local_version != info.extract_version
                or flags != info.flag_bits
                or compression != info.compress_type
                or local_time != central_time
                or local_date != central_date
                or crc != info.CRC
                or compressed_size != info.compress_size
                or file_size != info.file_size
                or data_end > central_offset
            ):
                raise ValueError("wheel ZIP local and central records differ")
            if exact_project_representation and extra_size != 0:
                raise ValueError("project wheel local header contains extra data")
            offset = data_end
        if offset != central_offset:
            raise ValueError("wheel ZIP has unclaimed bytes before its central directory")

        payloads: dict[str, bytes] = {}
        try:
            for info in infos:
                payload = archive.read(info)
                if len(payload) != info.file_size:
                    raise ValueError("wheel member restored size differs")
                payloads[info.filename] = payload
        except (RuntimeError, zipfile.BadZipFile) as error:
            raise ValueError("wheel member CRC or compression verification failed") from error
    return infos, payloads


def _verify_record(
    payloads: Mapping[str, bytes],
    *,
    record_path: str,
    require_zip_order: tuple[str, ...] | None = None,
) -> str:
    if record_path not in payloads:
        raise ValueError("wheel omits its RECORD ledger")
    raw = payloads[record_path]
    try:
        text = raw.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("wheel RECORD is not strict UTF-8 CSV") from error
    if not raw.endswith(b"\n") or any(len(row) != 3 for row in rows):
        raise ValueError("wheel RECORD row encoding differs")
    paths = tuple(row[0] for row in rows)
    if len(paths) != len(set(paths)) or set(paths) != set(payloads):
        raise ValueError("wheel RECORD does not name every member exactly once")
    if require_zip_order is not None and paths != require_zip_order:
        raise ValueError("project wheel RECORD order differs from ZIP order")
    for path, digest, size in rows:
        _wheel_member_path(path)
        if path == record_path:
            if digest or size:
                raise ValueError("wheel RECORD self-row must omit digest and size")
            continue
        payload = payloads[path]
        expected_digest = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(payload).digest()
        ).rstrip(b"=").decode("ascii")
        if digest != expected_digest or size != str(len(payload)):
            raise ValueError("wheel RECORD digest or size differs")
    return hashlib.sha256(raw).hexdigest()


def _project_payload_paths(
    candidate_files: Mapping[str, bytes], pyproject_bytes: bytes
) -> tuple[str, ...]:
    try:
        document = tomllib.loads(pyproject_bytes.decode("utf-8"))
        setuptools = document["tool"]["setuptools"]
        package_data = setuptools["package-data"]
        include = setuptools["packages"]["find"]["include"]
        project = document["project"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise ValueError("candidate pyproject packaging projection is invalid") from error
    if (
        project.get("name") != "kirby2"
        or project.get("version") != "0.1.0"
        or project.get("description")
        != "Deterministic market-execution training sandbox"
        or project.get("requires-python") != ">=3.11"
        or project.get("dependencies") != ["duckdb>=1.5,<2"]
        or include != ["kirby2*"]
        or type(package_data) is not dict
    ):
        raise ValueError("candidate pyproject release metadata differs")
    expected = {
        path
        for path in candidate_files
        if path.startswith("kirby2/") and path.endswith(".py")
    }
    for package_name, patterns in package_data.items():
        if type(package_name) is not str or type(patterns) is not list or any(
            type(pattern) is not str for pattern in patterns
        ):
            raise TypeError("candidate package-data projection is invalid")
        package_root = package_name.replace(".", "/") + "/"
        for path in candidate_files:
            if not path.startswith(package_root):
                continue
            relative = path[len(package_root) :]
            if any(fnmatchcase(relative, pattern) for pattern in patterns):
                expected.add(path)
    return tuple(sorted(expected, key=lambda item: item.encode("utf-8")))


def verify_project_wheel(
    wheel_bytes: bytes,
    *,
    filename: str,
    candidate_files: Mapping[str, bytes],
    pyproject_bytes: bytes,
    source_date_epoch: int,
    backend_version: str,
    requirements_lock: ReleaseRequirementsLockV1,
) -> VerifiedWheelV1:
    """Require one exact setuptools wheel derived only from candidate bytes."""

    if filename != PROJECT_WHEEL_FILENAME_V1:
        raise ValueError("project wheel filename differs")
    if type(candidate_files) is not dict or any(
        type(path) is not str or type(payload) is not bytes
        for path, payload in candidate_files.items()
    ):
        raise TypeError("candidate wheel inputs must map paths to exact bytes")
    if type(pyproject_bytes) is not bytes:
        raise TypeError("candidate pyproject input must be exact bytes")
    if backend_version != "80.9.0":
        raise ValueError("project wheel backend version differs")
    if (
        type(requirements_lock) is not ReleaseRequirementsLockV1
        or requirements_lock.project_requirement != "duckdb>=1.5,<2"
    ):
        raise ValueError("project wheel dependency lock differs")
    infos, payloads = _read_zip_payloads(
        wheel_bytes,
        maximum_members=PROJECT_WHEEL_MAX_MEMBERS_V1,
        maximum_expanded_bytes=PROJECT_WHEEL_MAX_EXPANDED_BYTES_V1,
        exact_project_representation=True,
        source_date_epoch=source_date_epoch,
    )
    zip_order = tuple(info.filename for info in infos)
    expected_payloads = _project_payload_paths(candidate_files, pyproject_bytes)
    expected_members = expected_payloads + _DIST_INFO_MEMBERS_V1
    if (
        set(zip_order) != set(expected_members)
        or len(zip_order) != len(expected_members)
        or zip_order[-len(_DIST_INFO_MEMBERS_V1) :] != _DIST_INFO_MEMBERS_V1
    ):
        missing = tuple(path for path in expected_members if path not in zip_order)
        unexpected = tuple(path for path in zip_order if path not in expected_members)
        first_difference = next(
            (
                [index, expected, observed]
                for index, (expected, observed) in enumerate(
                    zip(expected_members, zip_order, strict=False)
                )
                if expected != observed
            ),
            None,
        )
        raise ValueError(
            "project wheel member inventory or order differs: "
            f"expected={len(expected_members)} observed={len(zip_order)} "
            f"missing={list(missing[:3])!r} unexpected={list(unexpected[:3])!r} "
            f"first_difference={first_difference!r}"
        )
    for path in expected_payloads:
        if payloads[path] != candidate_files[path]:
            raise ValueError("project wheel package payload differs from candidate blob")
    expected_generated = {
        _DIST_INFO_MEMBERS_V1[0]: _PROJECT_METADATA_V1,
        _DIST_INFO_MEMBERS_V1[1]: _PROJECT_WHEEL_V1,
        _DIST_INFO_MEMBERS_V1[2]: _PROJECT_ENTRY_POINTS_V1,
        _DIST_INFO_MEMBERS_V1[3]: _PROJECT_TOP_LEVEL_V1,
    }
    differing_generated = tuple(
        path for path, raw in expected_generated.items() if payloads[path] != raw
    )
    if differing_generated:
        observed_metadata = (
            payloads[_DIST_INFO_MEMBERS_V1[0]][:1024]
            if _DIST_INFO_MEMBERS_V1[0] in differing_generated
            else b""
        )
        raise ValueError(
            "project wheel generated metadata bytes differ: "
            + ",".join(differing_generated)
            + f" observed_metadata={observed_metadata!r}"
        )
    record_sha256 = _verify_record(
        payloads,
        record_path=_DIST_INFO_MEMBERS_V1[-1],
        require_zip_order=zip_order,
    )
    return VerifiedWheelV1(
        filename=filename,
        wheel_bytes=wheel_bytes,
        name="kirby2",
        version="0.1.0",
        tags=("py3-none-any",),
        size=len(wheel_bytes),
        sha256=hashlib.sha256(wheel_bytes).hexdigest(),
        members=tuple(
            WheelMemberIdentityV1(
                path=path,
                size=len(payloads[path]),
                sha256=hashlib.sha256(payloads[path]).hexdigest(),
            )
            for path in zip_order
        ),
        record_sha256=record_sha256,
        verification_token=_VERIFIED_WHEEL_CONSTRUCTION_TOKEN_V1,
    )


def _stable_regular_file(
    path: Path | str,
    *,
    maximum_bytes: int,
    directory_fd: int | None = None,
) -> bytes:
    if directory_fd is None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path.resolve(strict=False) != path
        ):
            raise ValueError("wheel path must be supplied as an absolute resolved Path")
    elif (
        type(path) is not str
        or not path
        or "/" in path
        or "\\" in path
        or path in {".", ".."}
    ):
        raise ValueError("descriptor-relative wheel name is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("platform lacks O_NOFOLLOW wheel capture support")
    flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ValueError("wheel input is not one bounded single-link regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("wheel input was truncated during capture")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("wheel input grew during capture")
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ValueError("wheel input identity changed during capture")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _dependency_metadata(
    payloads: Mapping[str, bytes], locked: LockedReleaseWheelV1
) -> tuple[str, ...]:
    expected_filename = {
        "linux-x86_64": (
            "duckdb-1.5.5-cp314-cp314-"
            "manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl"
        ),
        "macos-arm64": "duckdb-1.5.5-cp314-cp314-macosx_11_0_arm64.whl",
    }[locked.target]
    if locked.filename != expected_filename:
        raise ValueError("locked dependency wheel filename tags differ")
    dist_info = f"{locked.name}-{locked.version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    wheel_path = f"{dist_info}/WHEEL"
    record_path = f"{dist_info}/RECORD"
    if any(path not in payloads for path in (metadata_path, wheel_path, record_path)):
        raise ValueError("locked dependency wheel metadata inventory differs")
    if any(
        path.endswith((".dist-info/METADATA", ".dist-info/WHEEL", ".dist-info/RECORD"))
        and not path.startswith(dist_info + "/")
        for path in payloads
    ):
        raise ValueError("locked dependency wheel contains another metadata root")
    metadata = BytesParser(policy=compat32).parsebytes(payloads[metadata_path])
    names = tuple(metadata.get_all("Name", []))
    versions = tuple(metadata.get_all("Version", []))
    if len(names) != 1 or names[0].casefold().replace("_", "-") != locked.name:
        raise ValueError("locked dependency wheel Name differs")
    if versions != (locked.version,):
        raise ValueError("locked dependency wheel Version differs")
    wheel = BytesParser(policy=compat32).parsebytes(payloads[wheel_path])
    tags = tuple(wheel.get_all("Tag", []))
    expected_tags = (
        {
            "cp314-cp314-manylinux_2_26_x86_64",
            "cp314-cp314-manylinux_2_28_x86_64",
        }
        if locked.target == "linux-x86_64"
        else {"cp314-cp314-macosx_11_0_arm64"}
    )
    if (
        tuple(wheel.get_all("Wheel-Version", [])) != ("1.0",)
        or tuple(wheel.get_all("Root-Is-Purelib", [])) != ("false",)
        or len(tags) != len(expected_tags)
        or set(tags) != expected_tags
    ):
        raise ValueError("locked dependency wheel compatibility tag differs")
    _verify_record(payloads, record_path=record_path)
    return tags


def load_locked_dependency_wheel(
    wheelhouse_root: Path,
    locked: LockedReleaseWheelV1,
) -> LockedWheelPayloadV1:
    """Capture and verify one exact dependency wheel without following links."""

    if type(locked) is not LockedReleaseWheelV1:
        raise TypeError("locked dependency resolver requires one lock row")
    if (
        not isinstance(wheelhouse_root, Path)
        or not wheelhouse_root.is_absolute()
        or wheelhouse_root.resolve(strict=False) != wheelhouse_root
    ):
        raise ValueError("wheelhouse root must be an absolute resolved Path")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("platform lacks O_NOFOLLOW wheelhouse support")
    directory_flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    directory_identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_gid,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    root_descriptor = os.open(wheelhouse_root, directory_flags)
    try:
        root_before = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_before.st_mode):
            raise ValueError("wheelhouse root is not a real directory")
        target_descriptor = os.open(
            locked.target,
            directory_flags,
            dir_fd=root_descriptor,
        )
        try:
            target_before = os.fstat(target_descriptor)
            if not stat.S_ISDIR(target_before.st_mode):
                raise ValueError("wheelhouse target is not a real directory")
            entries = tuple(sorted(os.listdir(target_descriptor)))
            if entries != (locked.filename,):
                raise ValueError(
                    "wheelhouse target inventory differs from its one lock row"
                )
            raw = _stable_regular_file(
                locked.filename,
                maximum_bytes=512 * 1024 * 1024,
                directory_fd=target_descriptor,
            )
            if (
                tuple(sorted(os.listdir(target_descriptor))) != entries
                or directory_identity(os.fstat(target_descriptor))
                != directory_identity(target_before)
            ):
                raise ValueError("wheelhouse target changed during capture")
        finally:
            os.close(target_descriptor)
        if directory_identity(os.fstat(root_descriptor)) != directory_identity(
            root_before
        ):
            raise ValueError("wheelhouse root changed during capture")
    finally:
        os.close(root_descriptor)
    if hashlib.sha256(raw).hexdigest() != locked.sha256:
        raise ValueError("locked dependency wheel digest differs")
    infos, payloads = _read_zip_payloads(
        raw,
        maximum_members=DEPENDENCY_WHEEL_MAX_MEMBERS_V1,
        maximum_expanded_bytes=DEPENDENCY_WHEEL_MAX_EXPANDED_BYTES_V1,
        exact_project_representation=False,
        allow_directories=True,
    )
    regular_payloads = {
        path: payload for path, payload in payloads.items() if not path.endswith("/")
    }
    tags = _dependency_metadata(regular_payloads, locked)
    license_bytes = extract_locked_license(raw, locked)
    record_path = next(
        path for path in regular_payloads if path.endswith(".dist-info/RECORD")
    )
    verified = VerifiedWheelV1(
        filename=locked.filename,
        wheel_bytes=raw,
        name=locked.name,
        version=locked.version,
        tags=tags,
        size=len(raw),
        sha256=locked.sha256,
        members=tuple(
            WheelMemberIdentityV1(
                path=info.filename,
                size=len(regular_payloads[info.filename]),
                sha256=hashlib.sha256(regular_payloads[info.filename]).hexdigest(),
            )
            for info in infos
            if not info.is_dir()
        ),
        record_sha256=hashlib.sha256(regular_payloads[record_path]).hexdigest(),
        verification_token=_VERIFIED_WHEEL_CONSTRUCTION_TOKEN_V1,
    )
    return LockedWheelPayloadV1(
        locked=locked,
        verified=verified,
        wheel_bytes=raw,
        license_bytes=license_bytes,
    )


def resolve_offline_wheelhouse(
    wheelhouse_root: Path,
    lock: ReleaseRequirementsLockV1,
) -> tuple[LockedWheelPayloadV1, ...]:
    if type(lock) is not ReleaseRequirementsLockV1:
        raise TypeError("offline wheelhouse resolver requires the exact lock")
    return tuple(
        load_locked_dependency_wheel(wheelhouse_root, locked)
        for locked in lock.wheels
    )


__all__ = [
    "DEPENDENCY_WHEEL_MAX_EXPANDED_BYTES_V1",
    "DEPENDENCY_WHEEL_MAX_MEMBERS_V1",
    "LockedWheelPayloadV1",
    "PROJECT_DIST_INFO_ROOT_V1",
    "PROJECT_WHEEL_FILENAME_V1",
    "PROJECT_WHEEL_MAX_EXPANDED_BYTES_V1",
    "PROJECT_WHEEL_MAX_MEMBERS_V1",
    "VerifiedWheelV1",
    "WheelMemberIdentityV1",
    "load_locked_dependency_wheel",
    "resolve_offline_wheelhouse",
    "verify_project_wheel",
]
