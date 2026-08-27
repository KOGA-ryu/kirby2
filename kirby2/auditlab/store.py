"""Content-addressed immutable evidence packets and append-only ledger."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .models import AcceptanceRecord, canonical_json, canonical_sha256


DEFAULT_AUDIT_LAB_STORE = Path(".kirby2") / "research" / "audit_lab"
_ACCEPTANCE_ID = re.compile(r"^acceptance-[A-Za-z0-9_-]{1,96}$")


@dataclass(frozen=True, slots=True)
class PacketRecord:
    packet_id: str
    directory: Path
    manifest_sha256: str
    artifact_count: int
    verification_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_count": self.artifact_count,
            "directory": str(self.directory),
            "manifest_sha256": self.manifest_sha256,
            "packet_id": self.packet_id,
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
        packet_id = f"audit-{canonical_sha256(identity)[:24]}"
        target = self.packets / packet_id
        if target.exists():
            record = self.verify(packet_id)
            if record.verification_status != "PASS":
                raise RuntimeError("existing immutable audit packet failed verification")
            return record
        with tempfile.TemporaryDirectory(
            dir=self.staging,
            prefix=f"{packet_id}-",
        ) as temporary:
            directory = Path(temporary) / packet_id
            directory.mkdir()
            references: dict[str, dict[str, object]] = {}
            for name, content in sorted(artifacts.items()):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                references[name] = {
                    "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
            manifest = {
                "artifacts": references,
                "identity": identity,
                "packet_id": packet_id,
                "record_type": "IMMUTABLE_KIRBY2_MODEL_RISK_PACKET",
                "schema_version": 1,
            }
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            directory.replace(target)
        record = self.verify(packet_id)
        if record.verification_status != "PASS":
            raise RuntimeError("new audit packet failed immutable verification")
        self._append_ledger(record, identity)
        return record

    def verify(self, packet_id: str) -> PacketRecord:
        directory = self.packets / packet_id
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"audit packet manifest is unavailable: {error}") from error
        failures = []
        if manifest.get("packet_id") != packet_id:
            failures.append("packet identity mismatch")
        if manifest.get("schema_version") != 1:
            failures.append("unsupported packet schema")
        identity = manifest.get("identity")
        if not isinstance(identity, dict) or packet_id != f"audit-{canonical_sha256(identity)[:24]}":
            failures.append("content-derived packet identity mismatch")
        references = manifest.get("artifacts")
        if not isinstance(references, dict):
            failures.append("artifact inventory is missing")
            references = {}
        for name, reference in references.items():
            path = directory / name
            if not isinstance(reference, dict) or not path.is_file():
                failures.append(f"missing artifact {name}")
                continue
            if _file_sha256(path) != reference.get("sha256"):
                failures.append(f"artifact digest mismatch {name}")
            if path.stat().st_size != reference.get("bytes"):
                failures.append(f"artifact byte-count mismatch {name}")
        actual_artifacts = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path != manifest_path
        }
        if actual_artifacts != set(references):
            failures.append("packet file inventory differs from immutable manifest")
        return PacketRecord(
            packet_id,
            directory.resolve(),
            _file_sha256(manifest_path),
            len(references),
            "PASS" if not failures else "FAIL: " + "; ".join(failures),
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
            if record.manifest_sha256 != entry.get("manifest_sha256"):
                failures.append(f"packet ledger digest mismatch {packet_id}")
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
                "manifest_sha256": record.manifest_sha256,
                "packet_id": record.packet_id,
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
