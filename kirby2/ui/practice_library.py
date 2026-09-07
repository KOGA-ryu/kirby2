"""Local immutable practice bundles, verified Replay, and separate user notes.

Public callers exchange detached records and opaque verified Replay sources.
Only this backend module resolves storage or sees canonical artifact bytes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from collections.abc import Mapping

from kirby2.research.paths import DataPaths, DataAreaId
from kirby2.release.platform_paths import platform_data_paths
from kirby2.curriculum.practice_episodes import get_practice_episode_v1
from .simulation_artifact_contract import ReplayArtifactRefV1
from .simulation_artifact_store import _read_simulation_replay_artifact
from .simulation_replay_facade import _verify_replay_artifact_bytes
from .simulation_practice_contract import (
    PracticeResultV1, PracticeActionRequestV1, build_practice_attempt_request,
)
from .simulation_practice_passage_contract import PracticeObservationPassageResultV1

SCHEMA = "KIRBY2_PRACTICE_BUNDLE_V1"
_ID = re.compile(r"practice-evidence-[0-9a-f]{64}\Z")
_MAX_BYTES = 64 * 1024 * 1024


def _bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decode(raw: bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate library JSON field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON library scalar: " + value)

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def _paths(root: str | None) -> DataPaths:
    return platform_data_paths() if root is None else DataPaths(Path(root))


def _child(parent: Path, name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("invalid storage child")
    path = parent / name
    if path.is_symlink():
        raise ValueError("library symlinks are not supported")
    return path


def _directory(paths: DataPaths, area: DataAreaId, *, create: bool = False) -> Path:
    paths.validate(area)
    if create:
        paths.ensure(area)
    parent = _child(paths.area(area), "practice-library")
    if create:
        parent.mkdir(exist_ok=True)
    if parent.exists() and not parent.is_dir():
        raise ValueError("library area is not a directory")
    return parent


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise ValueError("library object is not a bounded regular file")
        raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("library object exceeds supported size")
        return raw


def _write(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identifier(value: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError("invalid practice evidence ID")
    return value


def _completed(last: Mapping[str, object]) -> bool:
    attempt, episode = last["attempt"], last["episode"]
    if last["assessment"] is None or last["hold_id"] is not None:
        return False
    return episode["family"] == "F2_READING" or attempt["step_index"] >= attempt["step_count"]


def _validate_dependencies(deps: object, reference: ReplayArtifactRefV1) -> dict:
    if type(deps) is not dict or set(deps) != {"history", "requests", "passage", "ancestors"}:
        raise ValueError("unsupported practice dependencies")
    history = deps["history"]
    if type(history) is not list or not history or len(history) > 10000:
        raise ValueError("practice history is unavailable")
    first = history[0]
    identifiers = set()
    for item in history:
        PracticeResultV1.from_dict(item)
        if item["status"] != "AVAILABLE" or item["source_run_id"] != reference.source_run_id:
            raise ValueError("practice history source mismatch")
        if item["episode"] != first["episode"] or item["attempt"]["attempt_id"] != first["attempt"]["attempt_id"]:
            raise ValueError("practice history identity mismatch")
        if item["result_id"] in identifiers:
            raise ValueError("duplicate practice result")
        identifiers.add(item["result_id"])
    if type(deps["requests"]) is not dict:
        raise ValueError("practice requests unavailable")
    for key, request in deps["requests"].items():
        parsed = PracticeActionRequestV1.from_dict(request)
        if key != parsed.request_id or parsed.source_run_id != reference.source_run_id:
            raise ValueError("practice request identity mismatch")
    for item in history[1:]:
        if item["operation"] in {"STAGE", "CONTINUE", "UNASSISTED"} and item["request_id"] not in deps["requests"]:
            raise ValueError("practice response dependency missing")
    passage = deps["passage"]
    if passage is not None:
        PracticeObservationPassageResultV1.from_dict(passage)
        if passage["source_run_id"] != reference.source_run_id or passage["episode_recipe_sha256"] != first["episode"]["recipe_sha256"]:
            raise ValueError("practice passage identity mismatch")
    if type(deps["ancestors"]) is not list:
        raise ValueError("practice ancestry unavailable")
    for ancestor in deps["ancestors"]:
        PracticeResultV1.from_dict(ancestor)
    return deps


def _summary(bundle_id: str, manifest: dict, deps: dict) -> dict:
    first, last = deps["history"][0], deps["history"][-1]
    episode, attempt = first["episode"], first["attempt"]
    repeat = False
    try:
        repeat = get_practice_episode_v1(episode["episode_id"]).recipe_sha256 == episode["recipe_sha256"]
    except (KeyError, ValueError):
        pass
    return {
        "evidence_id": bundle_id, "attempt_id": attempt["attempt_id"],
        "episode_id": episode["episode_id"], "title": episode["title"],
        "family": episode["family"], "skill_id": episode["primary_skill_id"],
        "mode": attempt["mode"], "operation": attempt["operation"],
        "prior_attempt_id": attempt["prior_attempt_id"],
        "exercise_status": "FINISHED" if _completed(last) else "PARTIAL",
        "simulation_status": manifest["simulation_status"],
        "outcome": None if last["assessment"] is None else last["assessment"]["outcome"],
        "can_repeat": repeat, "status": "VERIFIED",
        "artifact_sha256": manifest["reference"]["artifact_sha256"],
        "assistance_count": sum(len(row["assistance"]) for row in deps["history"]),
        "pace": "UNTIMED" if episode["family"] == "F1_CONTROL" else (
            str(attempt["pace_multiplier_ppm"] / 1000000) + "x" if deps["passage"] else "UNAVAILABLE"
        ),
    }


def _load(paths: DataPaths, bundle_id: str, *, staging: Path | None = None) -> tuple[object, dict, dict, dict]:
    directory = _child(_directory(paths, DataAreaId.EVIDENCE), _identifier(bundle_id)) if staging is None else staging
    manifest = _decode(_read(_child(directory, "manifest.json")))
    fields = {"schema_id", "schema_version", "reference", "dependencies_sha256", "simulation_status"}
    if type(manifest) is not dict or set(manifest) != fields or manifest["schema_id"] != SCHEMA or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported bundle schema")
    if "practice-evidence-" + _sha(_bytes(manifest)) != bundle_id:
        raise ValueError("bundle manifest identity mismatch")
    reference = ReplayArtifactRefV1.from_dict(manifest["reference"])
    raw_deps = _read(_child(directory, "practice.json"))
    if _sha(raw_deps) != manifest["dependencies_sha256"]:
        raise ValueError("practice dependency digest mismatch")
    deps = _validate_dependencies(_decode(raw_deps), reference)
    raw = _read(_child(directory, "replay.json"))
    source, receipt = _verify_replay_artifact_bytes(reference, raw)
    if source is None or receipt["status"] != "AVAILABLE":
        raise ValueError("Replay verification failed: " + str(receipt.get("unavailable_reason")))
    artifact = json.loads(raw)
    if manifest["simulation_status"] != artifact["terminal_status"]:
        raise ValueError("simulation status mismatch")
    # The last practice cut may precede a subsequently completed simulation.
    final_time = artifact["final_frame"]["cursor"]["simulation_time_us"]
    if any(row["current_frame"]["cursor"]["simulation_time_us"] > final_time for row in deps["history"]):
        raise ValueError("practice history exceeds recording")
    return source, receipt, manifest, deps


def save_practice_evidence(reference_payload: Mapping[str, object], attempt_id: str,
                           root: str | None = None) -> dict:
    """Save a finalized artifact and backend-authored attempt history atomically."""
    from .simulation_practice_facade import _ATTEMPTS, _PERSISTED_HISTORY
    reference = ReplayArtifactRefV1.from_dict(reference_payload)
    state = _ATTEMPTS.get(attempt_id)
    if state is None or not state.history or state.record["source_run_id"] != reference.source_run_id:
        raise ValueError("practice history is unavailable for this artifact")
    raw = _read_simulation_replay_artifact(reference.store_id, reference.object_key)
    if raw is None:
        raise ValueError("original Replay bytes unavailable")
    source, receipt = _verify_replay_artifact_bytes(reference, raw)
    if source is None or receipt["status"] != "AVAILABLE":
        raise ValueError("Replay must verify before Save")
    ancestors, seen = [], {attempt_id}
    parent = state.record["prior_attempt_id"]
    while parent in _ATTEMPTS and parent not in seen:
        seen.add(parent)
        prior = _ATTEMPTS[parent]
        ancestors.extend(copy.deepcopy(prior.history))
        parent = prior.record["prior_attempt_id"]
    if parent in _PERSISTED_HISTORY and parent not in seen:
        ancestors.extend(copy.deepcopy(_PERSISTED_HISTORY[parent]))
    deps = {"history": copy.deepcopy(state.history), "requests": copy.deepcopy(state.requests),
            "passage": copy.deepcopy(state.passage), "ancestors": ancestors}
    _validate_dependencies(deps, reference)
    deps_raw = _bytes(deps)
    manifest = {"schema_id": SCHEMA, "schema_version": 1,
                "reference": reference.as_dict(), "dependencies_sha256": _sha(deps_raw),
                "simulation_status": json.loads(raw)["terminal_status"]}
    identifier = "practice-evidence-" + _sha(_bytes(manifest))
    paths = _paths(root)
    parent_dir = _directory(paths, DataAreaId.EVIDENCE, create=True)
    target = _child(parent_dir, identifier)
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent_dir))
        try:
            _write(staging / "replay.json", raw)
            _write(staging / "practice.json", deps_raw)
            _write(staging / "manifest.json", _bytes(manifest))
            _sync_directory(staging)
            _load(paths, identifier, staging=staging)
            paths.validate(DataAreaId.EVIDENCE)
            _child(parent_dir, identifier)
            try:
                staging.rename(target)
            except OSError:
                if not target.exists():
                    raise
            _sync_directory(parent_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    _, _, stored, dependencies = _load(paths, identifier)
    if stored != manifest or dependencies != deps:
        raise ValueError("existing evidence conflicts with immutable Save")
    return _summary(identifier, stored, dependencies)


def open_practice_evidence(evidence_id: str, root: str | None = None) -> tuple[object, dict]:
    paths = _paths(root)
    source, receipt, manifest, deps = _load(paths, evidence_id)
    return source, {"summary": _summary(evidence_id, manifest, deps),
                    "verification": receipt, "reference": manifest["reference"],
                    "dependencies": copy.deepcopy(deps)}


def list_practice_evidence(root: str | None = None) -> dict:
    """Rebuild inventory from authoritative bundles; cached index is disposable."""
    paths = _paths(root)
    parent = _directory(paths, DataAreaId.EVIDENCE)
    entries, rejected = [], []
    for item in sorted(parent.iterdir()) if parent.exists() else ():
        if item.name.startswith(".pending-"):
            continue
        try:
            _, _, manifest, deps = _load(paths, item.name)
            entries.append(_summary(item.name, manifest, deps))
        except (ValueError, OSError, TypeError, KeyError) as error:
            rejected.append({"name": item.name, "reason": str(error)})
    skills = {}
    for entry in entries:
        skills[entry["skill_id"]] = skills.get(entry["skill_id"], 0) + 1
    return {"schema_id": "KIRBY2_PRACTICE_LIBRARY_V1", "schema_version": 1,
            "entries": entries, "rejected": rejected, "skill_counts": skills,
            "storage_root": str(paths.root)}


def read_practice_note(evidence_id: str, root: str | None = None) -> dict:
    paths = _paths(root)
    _load(paths, evidence_id)
    path = _child(_directory(paths, DataAreaId.CONFIG), evidence_id + ".json")
    if not path.exists():
        return {"text": "", "revision": _sha(b"")}
    raw = _read(path)
    data = _decode(raw)
    if type(data) is not dict or set(data) != {"text"} or type(data["text"]) is not str:
        raise ValueError("invalid note")
    return {"text": data["text"], "revision": _sha(raw)}


def write_practice_note(evidence_id: str, text: str, expected_revision: str,
                        root: str | None = None) -> dict:
    if type(text) is not str or len(text) > 100000:
        raise ValueError("note exceeds supported length")
    paths = _paths(root)
    parent = _directory(paths, DataAreaId.CONFIG, create=True)
    # OS locks release on process exit; no stale lock directory after a crash.
    import fcntl
    lock = _child(parent, ".notes.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        current = read_practice_note(evidence_id, root)
        if current["revision"] != expected_revision:
            raise ValueError("note changed; reload before saving")
        target = _child(parent, evidence_id + ".json")
        temp = parent / (".note-" + os.urandom(12).hex())
        try:
            _write(temp, _bytes({"text": text}))
            paths.validate(DataAreaId.CONFIG)
            _child(parent, target.name)
            os.replace(temp, target)
            _sync_directory(parent)
        finally:
            temp.unlink(missing_ok=True)
    return read_practice_note(evidence_id, root)


def build_saved_practice_repeat_request(evidence_id: str, root: str | None = None) -> dict:
    """Authorize a new exact repeat from a verified saved parent, never resume it."""
    from .simulation_practice_facade import _PERSISTED_PARENTS, _PERSISTED_HISTORY
    _, record = open_practice_evidence(evidence_id, root)
    if not record["summary"]["can_repeat"]:
        raise ValueError("saved recipe is unavailable; Review only")
    first = record["dependencies"]["history"][0]
    attempt = first["attempt"]
    _PERSISTED_PARENTS[attempt["attempt_id"]] = first["episode"]["episode_id"]
    deps = record["dependencies"]
    _PERSISTED_HISTORY[attempt["attempt_id"]] = copy.deepcopy(deps["history"] + deps["ancestors"])
    return build_practice_attempt_request(
        episode_id=first["episode"]["episode_id"], operation="EXACT_REPEAT",
        prior_attempt_id=attempt["attempt_id"], mode=attempt["mode"],
        pace_multiplier_ppm=attempt["pace_multiplier_ppm"],
    )
