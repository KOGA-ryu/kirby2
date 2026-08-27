"""Content-addressed immutable evidence packets and append-only ledger."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .models import (
    AUDIT_PACKET_SCHEMA_VERSION,
    LEGACY_AUDIT_PACKET_SCHEMA_VERSION,
    AcceptanceRecord,
    canonical_json,
    canonical_sha256,
)


DEFAULT_AUDIT_LAB_STORE = Path(".kirby2") / "research" / "audit_lab"
_ACCEPTANCE_ID = re.compile(r"^acceptance-[A-Za-z0-9_-]{1,96}$")
_PACKET_ID = re.compile(r"^audit-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_ARTIFACT_NAMES = frozenset({"manifest.json"})
_IDENTITY_AND_ARTIFACTS = "IDENTITY_AND_ARTIFACTS"
_IDENTITY_ONLY_LEGACY = "IDENTITY_ONLY_LEGACY"
_UNSUPPORTED_IDENTITY_SCOPE = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class PacketRecord:
    packet_id: str
    directory: Path
    manifest_sha256: str
    artifact_count: int
    schema_version: int
    identity_scope: str
    verification_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_count": self.artifact_count,
            "directory": str(self.directory),
            "identity_scope": self.identity_scope,
            "manifest_sha256": self.manifest_sha256,
            "packet_id": self.packet_id,
            "schema_version": self.schema_version,
            "verification_status": self.verification_status,
        }


class AuditLabStore:
    def __init__(self, root: Path = DEFAULT_AUDIT_LAB_STORE) -> None:
        self.root = root
        self.packets = root / "packets"
        self.staging = root / ".staging"
        self.ledger = root / "ledger.jsonl"
        self.acceptance_records = root / "acceptance_records"
        self.acceptance_ledger = root / "acceptance_ledger.jsonl"
        self.packets.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.acceptance_records.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        identity: dict[str, object],
        artifacts: dict[str, str],
    ) -> PacketRecord:
        identity_snapshot = _json_object_snapshot(identity, "packet identity")
        encoded_artifacts, references = _prepare_artifacts(artifacts)
        packet_id = _v2_packet_id(identity_snapshot, references)
        manifest = {
            "artifacts": references,
            "identity": identity_snapshot,
            "packet_id": packet_id,
            "record_type": "IMMUTABLE_KIRBY2_MODEL_RISK_PACKET",
            "schema_version": AUDIT_PACKET_SCHEMA_VERSION,
        }
        target = self.packets / packet_id
        if target.is_symlink():
            raise RuntimeError("immutable audit packet target must not be a symlink")
        if target.exists():
            record = self.verify(packet_id)
            if record.verification_status != "PASS":
                raise RuntimeError("existing immutable audit packet failed verification")
            existing_manifest = _read_manifest(target / "manifest.json")
            if existing_manifest != manifest:
                raise RuntimeError(
                    "immutable audit packet ID collision with different manifest"
                )
            return record
        with tempfile.TemporaryDirectory(
            dir=self.staging,
            prefix=f"{packet_id}-",
        ) as temporary:
            directory = Path(temporary) / packet_id
            directory.mkdir()
            for name, content in encoded_artifacts.items():
                path = _contained_artifact_path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path = _contained_artifact_path(directory, name)
                path.write_bytes(content)
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            directory.replace(target)
        record = self.verify(packet_id)
        if record.verification_status != "PASS":
            raise RuntimeError("new audit packet failed immutable verification")
        self._append_ledger(record, identity_snapshot)
        return record

    def verify(self, packet_id: str) -> PacketRecord:
        _validate_packet_id(packet_id)
        directory = self.packets / packet_id
        if directory.is_symlink():
            raise RuntimeError("audit packet directory must not be a symlink")
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            raise RuntimeError("audit packet manifest must not be a symlink")
        manifest = _read_manifest(manifest_path)
        failures = []
        if manifest.get("packet_id") != packet_id:
            failures.append("packet identity mismatch")
        raw_schema_version = manifest.get("schema_version")
        schema_version = (
            raw_schema_version if type(raw_schema_version) is int else 0
        )
        if schema_version == AUDIT_PACKET_SCHEMA_VERSION:
            identity_scope = _IDENTITY_AND_ARTIFACTS
        elif schema_version == LEGACY_AUDIT_PACKET_SCHEMA_VERSION:
            identity_scope = _IDENTITY_ONLY_LEGACY
        else:
            identity_scope = _UNSUPPORTED_IDENTITY_SCOPE
            failures.append("unsupported packet schema")
        identity = manifest.get("identity")
        references = manifest.get("artifacts")
        if not isinstance(references, dict):
            failures.append("artifact inventory is missing")
            references = {}
        if not isinstance(identity, dict):
            failures.append("content-derived packet identity mismatch")
        elif schema_version == AUDIT_PACKET_SCHEMA_VERSION:
            if packet_id != _v2_packet_id(identity, references):
                failures.append("content-derived packet identity mismatch")
        elif schema_version == LEGACY_AUDIT_PACKET_SCHEMA_VERSION:
            if packet_id != _legacy_packet_id(identity):
                failures.append("content-derived packet identity mismatch")
        validated_references: list[tuple[str, object]] = []
        for name, reference in references.items():
            try:
                validated_name = _validate_artifact_name(name)
            except (TypeError, ValueError) as error:
                failures.append(f"invalid artifact name {name!r}: {error}")
                continue
            if not _valid_artifact_reference(reference):
                failures.append(f"invalid artifact reference {name!r}")
                continue
            validated_references.append((validated_name, reference))

        packet_entries = tuple(directory.rglob("*"))
        symlinks = tuple(
            path.relative_to(directory).as_posix()
            for path in packet_entries
            if path.is_symlink()
        )
        if symlinks:
            failures.append(f"packet contains symlinks: {sorted(symlinks)!r}")

        for name, reference in validated_references:
            try:
                path = _contained_artifact_path(directory, name)
            except ValueError as error:
                failures.append(f"unsafe artifact path {name!r}: {error}")
                continue
            if not isinstance(reference, dict) or not path.is_file():
                failures.append(f"missing artifact {name}")
                continue
            if path.is_symlink():
                failures.append(f"artifact must not be a symlink {name}")
                continue
            if _file_sha256(path) != reference.get("sha256"):
                failures.append(f"artifact digest mismatch {name}")
            if path.stat().st_size != reference.get("bytes"):
                failures.append(f"artifact byte-count mismatch {name}")
        actual_artifacts = {
            path.relative_to(directory).as_posix()
            for path in packet_entries
            if path.is_file() and not path.is_symlink() and path != manifest_path
        }
        if actual_artifacts != set(references):
            failures.append("packet file inventory differs from immutable manifest")
        return PacketRecord(
            packet_id=packet_id,
            directory=directory.resolve(),
            manifest_sha256=_file_sha256(manifest_path),
            artifact_count=len(references),
            schema_version=schema_version,
            identity_scope=identity_scope,
            verification_status=(
                "PASS" if not failures else "FAIL: " + "; ".join(failures)
            ),
        )

    def record_acceptance(self, record: AcceptanceRecord) -> Path:
        """Persist a pending or human decision without rewriting earlier records."""

        if not _ACCEPTANCE_ID.fullmatch(record.record_id):
            raise ValueError("acceptance record ID is invalid")
        if record.supersedes_record_id is not None:
            superseded = self.acceptance_records / f"{record.supersedes_record_id}.json"
            if not superseded.is_file():
                raise ValueError("superseded acceptance record does not exist")
        payload = canonical_json(record.as_dict()) + "\n"
        target = self.acceptance_records / f"{record.record_id}.json"
        if target.exists():
            if target.read_text(encoding="utf-8") != payload:
                raise RuntimeError("immutable acceptance record cannot be overwritten")
            return target.resolve()
        target.write_text(payload, encoding="utf-8")
        entry = canonical_json(
            {
                "record_id": record.record_id,
                "record_sha256": _file_sha256(target),
                "reviewer_decision": record.reviewer_decision,
                "supersedes_record_id": record.supersedes_record_id,
            }
        )
        with self.acceptance_ledger.open("a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
        return target.resolve()

    def verify_ledgers(self) -> dict[str, object]:
        failures: list[str] = []
        packet_entries = self._load_ledger(self.ledger, "packet", failures)
        packet_ids = []
        for entry in packet_entries:
            packet_id = entry.get("packet_id")
            if not isinstance(packet_id, str):
                failures.append("packet ledger entry lacks an ID")
                continue
            packet_ids.append(packet_id)
            try:
                record = self.verify(packet_id)
            except RuntimeError as error:
                failures.append(str(error))
                continue
            if record.verification_status != "PASS":
                failures.append(f"packet verification failed {packet_id}")
            if record.manifest_sha256 != entry.get("manifest_sha256"):
                failures.append(f"packet ledger digest mismatch {packet_id}")
            entry_schema = entry.get("packet_schema_version")
            if entry_schema is None:
                if record.schema_version != LEGACY_AUDIT_PACKET_SCHEMA_VERSION:
                    failures.append(f"packet ledger schema missing {packet_id}")
            elif entry_schema != record.schema_version:
                failures.append(f"packet ledger schema mismatch {packet_id}")
            entry_scope = entry.get("identity_scope")
            if entry_scope is None:
                if record.identity_scope != _IDENTITY_ONLY_LEGACY:
                    failures.append(f"packet ledger identity scope missing {packet_id}")
            elif entry_scope != record.identity_scope:
                failures.append(f"packet ledger identity scope mismatch {packet_id}")
        directory_packet_ids = {
            path.name for path in self.packets.iterdir() if path.is_dir()
        }
        if set(packet_ids) != directory_packet_ids or len(packet_ids) != len(set(packet_ids)):
            failures.append("packet ledger and immutable packet inventory differ")

        acceptance_entries = self._load_ledger(
            self.acceptance_ledger,
            "acceptance",
            failures,
        )
        acceptance_ids = []
        seen_acceptances: set[str] = set()
        for entry in acceptance_entries:
            record_id = entry.get("record_id")
            if not isinstance(record_id, str):
                failures.append("acceptance ledger entry lacks an ID")
                continue
            acceptance_ids.append(record_id)
            path = self.acceptance_records / f"{record_id}.json"
            if not path.is_file() or _file_sha256(path) != entry.get("record_sha256"):
                failures.append(f"acceptance record digest mismatch {record_id}")
            supersedes = entry.get("supersedes_record_id")
            if supersedes is not None and supersedes not in seen_acceptances:
                failures.append(f"acceptance record supersedes an unknown future record {record_id}")
            seen_acceptances.add(record_id)
        directory_acceptance_ids = {
            path.stem for path in self.acceptance_records.glob("*.json")
        }
        if (
            set(acceptance_ids) != directory_acceptance_ids
            or len(acceptance_ids) != len(set(acceptance_ids))
        ):
            failures.append("acceptance ledger and immutable record inventory differ")
        return {
            "acceptance_record_count": len(acceptance_ids),
            "failures": failures,
            "packet_count": len(packet_ids),
            "status": "PASS" if not failures else "FAIL",
        }

    @staticmethod
    def _load_ledger(path: Path, label: str, failures: list[str]) -> list[dict[str, object]]:
        if not path.is_file():
            failures.append(f"{label} ledger is missing")
            return []
        entries = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                failures.append(f"{label} ledger line {line_number} is invalid JSON")
                continue
            if not isinstance(payload, dict):
                failures.append(f"{label} ledger line {line_number} is not an object")
                continue
            entries.append(payload)
        return entries

    def _append_ledger(self, record: PacketRecord, identity: dict[str, object]) -> None:
        entry = canonical_json(
            {
                "budget": identity["budget"],
                "identity_scope": record.identity_scope,
                "manifest_sha256": record.manifest_sha256,
                "packet_id": record.packet_id,
                "packet_schema_version": record.schema_version,
                "seed": identity["seed"],
            }
        )
        existing = set()
        if self.ledger.is_file():
            existing = set(self.ledger.read_text(encoding="utf-8").splitlines())
        if entry not in existing:
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(entry + "\n")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_artifacts(
    artifacts: dict[str, str],
) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
    """Validate and encode the complete inventory before staging any writes."""

    names = tuple(_validate_artifact_name(name) for name in artifacts)
    if len(names) != len(set(names)):
        raise ValueError("artifact names must be unique after canonicalization")
    encoded: dict[str, bytes] = {}
    references: dict[str, dict[str, object]] = {}
    for name in sorted(names):
        content = artifacts[name]
        if not isinstance(content, str):
            raise TypeError(f"artifact content must be text: {name}")
        data = content.encode("utf-8")
        encoded[name] = data
        references[name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return encoded, references


def _validate_artifact_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError("artifact name must be text")
    if not name:
        raise ValueError("artifact name must not be empty")
    if "\x00" in name:
        raise ValueError("artifact name must not contain NUL")
    if "\\" in name:
        raise ValueError("artifact name must use POSIX separators")
    if name in _RESERVED_ARTIFACT_NAMES:
        raise ValueError("artifact name is reserved by the packet format")

    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact name contains an empty, dot, or parent segment")
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise ValueError("artifact name must be packet-relative")
    if posix.as_posix() != name:
        raise ValueError("artifact name is not canonical POSIX text")
    return name


def _contained_artifact_path(directory: Path, name: object) -> Path:
    validated = _validate_artifact_name(name)
    root = directory.resolve()
    candidate = directory.joinpath(*PurePosixPath(validated).parts)
    if not candidate.resolve(strict=False).is_relative_to(root):
        raise ValueError("artifact resolves outside its packet directory")
    return candidate


def _validate_packet_id(packet_id: object) -> str:
    if not isinstance(packet_id, str) or _PACKET_ID.fullmatch(packet_id) is None:
        raise ValueError("audit packet ID is invalid")
    return packet_id


def _json_object_snapshot(value: object, label: str) -> dict[str, object]:
    try:
        snapshot = json.loads(canonical_json(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical JSON: {error}") from error
    if not isinstance(snapshot, dict):
        raise TypeError(f"{label} must be an object")
    return snapshot


def _v2_packet_id(
    identity: dict[str, object],
    references: dict[str, object],
) -> str:
    material = {
        "artifacts": references,
        "identity": identity,
        "schema_version": AUDIT_PACKET_SCHEMA_VERSION,
    }
    return f"audit-{canonical_sha256(material)[:24]}"


def _legacy_packet_id(identity: dict[str, object]) -> str:
    return f"audit-{canonical_sha256(identity)[:24]}"


def _valid_artifact_reference(reference: object) -> bool:
    return (
        isinstance(reference, dict)
        and set(reference) == {"bytes", "sha256"}
        and type(reference.get("bytes")) is int
        and int(reference["bytes"]) >= 0
        and isinstance(reference.get("sha256"), str)
        and _SHA256.fullmatch(str(reference["sha256"])) is not None
    )


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"audit packet manifest is unavailable: {error}") from error
    if not isinstance(manifest, dict):
        raise RuntimeError("audit packet manifest root must be an object")
    return manifest
