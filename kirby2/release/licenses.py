"""Frozen dependency-wheel and license inventory for offline release builds."""

from __future__ import annotations

import hashlib
import io
import re
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from kirby2.packs.formats import (
    canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)


RELEASE_REQUIREMENTS_LOCK_SCHEMA_ID_V1 = "KIRBY2_RELEASE_REQUIREMENTS_LOCK_V1"
RELEASE_LICENSE_INVENTORY_SCHEMA_ID_V1 = "KIRBY2_RELEASE_LICENSE_INVENTORY_V1"
RELEASE_NOTICES_ENCODER_ID_V1 = "KIRBY2_RELEASE_NOTICES_TEXT_V1"
RELEASE_REQUIREMENTS_LOCK_SCHEMA_VERSION_V1 = 1
RELEASE_LICENSE_INVENTORY_SCHEMA_VERSION_V1 = 1
RELEASE_LOCK_TARGETS_V1 = ("macos-arm64", "linux-x86_64")
RELEASE_PROJECT_NOTICE_V1 = (
    "Kirby2 project materials are distributed only under terms supplied by the "
    "project owner. This generated inventory does not grant an additional license."
)

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 contract")
    return value


@dataclass(frozen=True, slots=True)
class LockedReleaseWheelV1:
    name: str
    version: str
    target: str
    filename: str
    sha256: str
    license_id: str
    license_member: str
    license_sha256: str
    license_size: int

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("locked wheel name must be normalized")
        _text(self.version, "locked wheel version", 128)
        if self.target not in RELEASE_LOCK_TARGETS_V1:
            raise ValueError("locked wheel target is unsupported")
        _text(self.filename, "locked wheel filename", 255)
        if not self.filename.endswith(".whl") or "/" in self.filename or "\\" in self.filename:
            raise ValueError("locked wheel filename is invalid")
        require_sha256(self.sha256, "locked wheel digest")
        _text(self.license_id, "locked wheel license ID", 128)
        _text(self.license_member, "locked wheel license member", 512)
        if not self.license_member.startswith(f"{self.name}-{self.version}.dist-info/licenses/"):
            raise ValueError("locked wheel license member is outside dist-info/licenses")
        require_sha256(self.license_sha256, "locked wheel license digest")
        if type(self.license_size) is not int or self.license_size <= 0:
            raise ValueError("locked wheel license size must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "license_id": self.license_id,
            "license_member": self.license_member,
            "license_sha256": self.license_sha256,
            "license_size": self.license_size,
            "name": self.name,
            "sha256": self.sha256,
            "target": self.target,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "LockedReleaseWheelV1":
        fields = {
            "name",
            "version",
            "target",
            "filename",
            "sha256",
            "license_id",
            "license_member",
            "license_sha256",
            "license_size",
        }
        row = _exact(value, fields, "locked wheel")
        return cls(
            name=_text(row["name"], "locked wheel name"),
            version=_text(row["version"], "locked wheel version"),
            target=_text(row["target"], "locked wheel target"),
            filename=_text(row["filename"], "locked wheel filename"),
            sha256=_text(row["sha256"], "locked wheel digest"),
            license_id=_text(row["license_id"], "locked wheel license ID"),
            license_member=_text(
                row["license_member"], "locked wheel license member"
            ),
            license_sha256=_text(
                row["license_sha256"], "locked wheel license digest"
            ),
            license_size=row["license_size"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReleaseRequirementsLockV1:
    lock_id: str
    python_implementation: str
    python_version: str
    project_requirement: str
    no_index: bool
    wheels: tuple[LockedReleaseWheelV1, ...]

    schema_version: ClassVar[int] = RELEASE_REQUIREMENTS_LOCK_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.lock_id, "requirements lock ID", 128)
        _text(self.python_implementation, "locked Python implementation", 64)
        _text(self.python_version, "locked Python version", 32)
        _text(self.project_requirement, "project dependency requirement", 256)
        if self.no_index is not True:
            raise ValueError("release dependency lock must require no-index builds")
        if (
            self.lock_id != "KIRBY2_RELEASE_REQUIREMENTS_V1"
            or self.python_implementation != "CPython"
            or self.python_version != "3.14"
            or self.project_requirement != "duckdb>=1.5,<2"
        ):
            raise ValueError("release dependency lock identity differs")
        if type(self.wheels) is not tuple or len(self.wheels) != 2:
            raise ValueError("release lock must contain exactly two target wheels")
        expected = tuple(
            sorted(
                self.wheels,
                key=lambda item: (item.name, item.version, item.target),
            )
        )
        if self.wheels != expected:
            raise ValueError("locked wheels must be in normalized tuple order")
        if tuple(item.target for item in self.wheels) != (
            "linux-x86_64",
            "macos-arm64",
        ):
            raise ValueError("release lock must cover Linux then macOS")
        if len({(item.name, item.version) for item in self.wheels}) != 1:
            raise ValueError("target wheels must lock the same dependency version")
        if any(
            item.name != "duckdb"
            or item.version != "1.5.5"
            or item.license_id != "MIT"
            for item in self.wheels
        ):
            raise ValueError("release dependency version or license differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "lock_id": self.lock_id,
            "no_index": self.no_index,
            "project_requirement": self.project_requirement,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "schema_version": self.schema_version,
            "wheels": [item.as_dict() for item in self.wheels],
        }

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    def for_target(self, target: str) -> tuple[LockedReleaseWheelV1, ...]:
        if target not in RELEASE_LOCK_TARGETS_V1:
            raise ValueError("release dependency target is unsupported")
        return tuple(item for item in self.wheels if item.target == target)

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseRequirementsLockV1":
        fields = {
            "schema_version",
            "lock_id",
            "python_implementation",
            "python_version",
            "project_requirement",
            "no_index",
            "wheels",
        }
        row = _exact(value, fields, "release requirements lock")
        if row["schema_version"] != cls.schema_version:
            raise ValueError("release requirements lock schema version differs")
        wheels = row["wheels"]
        if type(wheels) is not list:
            raise TypeError("locked wheels must be an array")
        return cls(
            lock_id=_text(row["lock_id"], "requirements lock ID"),
            python_implementation=_text(
                row["python_implementation"], "locked Python implementation"
            ),
            python_version=_text(row["python_version"], "locked Python version"),
            project_requirement=_text(
                row["project_requirement"], "project dependency requirement"
            ),
            no_index=row["no_index"],  # type: ignore[arg-type]
            wheels=tuple(LockedReleaseWheelV1.from_dict(item) for item in wheels),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseRequirementsLockV1":
        if type(raw) is not bytes:
            raise TypeError("requirements lock must be exact bytes")
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("release requirements lock is not valid UTF-8 TOML") from error
        return cls.from_dict(value)

    @classmethod
    def from_path(cls, path: Path) -> "ReleaseRequirementsLockV1":
        if not isinstance(path, Path):
            raise TypeError("requirements lock path must be a Path")
        return cls.from_bytes(path.read_bytes())


def extract_locked_license(wheel_bytes: bytes, locked: LockedReleaseWheelV1) -> bytes:
    """Extract exactly one bounded license member from a preverified wheel."""

    if type(wheel_bytes) is not bytes:
        raise TypeError("wheel license extraction requires exact bytes")
    if hashlib.sha256(wheel_bytes).hexdigest() != locked.sha256:
        raise ValueError("wheel bytes differ from the dependency lock")
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes), "r") as archive:
            names = archive.namelist()
            if names.count(locked.license_member) != 1:
                raise ValueError("wheel does not contain exactly one locked license member")
            info = archive.getinfo(locked.license_member)
            if info.is_dir() or info.file_size != locked.license_size:
                raise ValueError("wheel license member size differs")
            if info.file_size > 1024 * 1024:
                raise ValueError("wheel license member exceeds the release bound")
            raw = archive.read(info)
    except zipfile.BadZipFile as error:
        raise ValueError("locked dependency wheel is not a valid ZIP container") from error
    if len(raw) != locked.license_size:
        raise ValueError("extracted license size differs")
    if hashlib.sha256(raw).hexdigest() != locked.license_sha256:
        raise ValueError("extracted license digest differs")
    return raw


def release_license_inventory(
    lock: ReleaseRequirementsLockV1,
) -> dict[str, object]:
    """Return the target-independent, content-addressed license projection."""

    unique: dict[tuple[str, str, str], LockedReleaseWheelV1] = {}
    for wheel in lock.wheels:
        unique[(wheel.name, wheel.version, wheel.license_sha256)] = wheel
    dependency_rows = [
        {
            "license_id": wheel.license_id,
            "license_member": wheel.license_member,
            "license_sha256": wheel.license_sha256,
            "license_size": wheel.license_size,
            "name": wheel.name,
            "version": wheel.version,
        }
        for wheel in sorted(unique.values(), key=lambda item: item.name)
    ]
    return {
        "dependencies": dependency_rows,
        "project": {
            "license_id": "LicenseRef-Kirby2-Project",
            "notice": RELEASE_PROJECT_NOTICE_V1,
        },
        "schema_id": RELEASE_LICENSE_INVENTORY_SCHEMA_ID_V1,
        "schema_version": RELEASE_LICENSE_INVENTORY_SCHEMA_VERSION_V1,
    }


def release_license_inventory_bytes(lock: ReleaseRequirementsLockV1) -> bytes:
    return canonical_json_bytes(release_license_inventory(lock))


def release_notices_bytes(
    lock: ReleaseRequirementsLockV1,
    license_payloads: dict[str, bytes],
) -> bytes:
    """Encode deterministic project and third-party notices as UTF-8 text."""

    if type(license_payloads) is not dict or any(
        type(key) is not str or type(value) is not bytes
        for key, value in license_payloads.items()
    ):
        raise TypeError("license payloads must map digest strings to exact bytes")
    unique: dict[str, LockedReleaseWheelV1] = {}
    for wheel in lock.wheels:
        unique[wheel.license_sha256] = wheel
    expected = set(unique)
    if set(license_payloads) != expected:
        raise ValueError("license payload inventory differs from the lock")
    sections = [
        "Kirby2 Release Notices\n",
        "Project notice\n--------------\n" + RELEASE_PROJECT_NOTICE_V1 + "\n",
    ]
    for digest, wheel in sorted(unique.items(), key=lambda item: item[1].name):
        raw = license_payloads[digest]
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("provided license payload digest differs")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("V1 release notices require UTF-8 license text") from error
        sections.append(
            f"Third party: {wheel.name} {wheel.version} ({wheel.license_id})\n"
            + "-" * (14 + len(wheel.name) + len(wheel.version) + len(wheel.license_id))
            + "\n"
            + text.rstrip("\n")
            + "\n"
        )
    return ("\n".join(sections).rstrip("\n") + "\n").encode("utf-8")


__all__ = [
    "RELEASE_LICENSE_INVENTORY_SCHEMA_ID_V1",
    "RELEASE_LICENSE_INVENTORY_SCHEMA_VERSION_V1",
    "RELEASE_LOCK_TARGETS_V1",
    "RELEASE_NOTICES_ENCODER_ID_V1",
    "RELEASE_PROJECT_NOTICE_V1",
    "RELEASE_REQUIREMENTS_LOCK_SCHEMA_ID_V1",
    "RELEASE_REQUIREMENTS_LOCK_SCHEMA_VERSION_V1",
    "LockedReleaseWheelV1",
    "ReleaseRequirementsLockV1",
    "extract_locked_license",
    "release_license_inventory",
    "release_license_inventory_bytes",
    "release_notices_bytes",
]
