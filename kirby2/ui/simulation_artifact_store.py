"""Process-local immutable storage for finalized simulation Replay artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .simulation_contract import SimulationContractIntegrityError


SIMULATION_REPLAY_STORE_ID = "kirby2-in-process-replay-store-v1"
_OBJECT_PREFIX = "simulation-replay-artifact-"


@dataclass(slots=True)
class _SimulationReplayArtifactStore:
    store_id: str = SIMULATION_REPLAY_STORE_ID
    _objects: dict[str, bytes] = field(default_factory=dict)

    def put(self, artifact_bytes: bytes) -> tuple[str, str]:
        if type(artifact_bytes) is not bytes or not artifact_bytes:
            raise TypeError("simulation Replay artifact bytes must be nonempty bytes")
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        object_key = f"{_OBJECT_PREFIX}{artifact_sha256}"
        existing = self._objects.get(object_key)
        if existing is not None and existing != artifact_bytes:
            raise SimulationContractIntegrityError(
                "immutable simulation Replay object key already has different bytes"
            )
        self._objects.setdefault(object_key, artifact_bytes)
        return object_key, artifact_sha256

    def get(self, object_key: str) -> bytes | None:
        if type(object_key) is not str:
            raise TypeError("simulation Replay object key must be text")
        value = self._objects.get(object_key)
        return None if value is None else bytes(value)


_STORE = _SimulationReplayArtifactStore()


def _store_simulation_replay_artifact(artifact_bytes: bytes) -> tuple[str, str, str]:
    object_key, artifact_sha256 = _STORE.put(artifact_bytes)
    return _STORE.store_id, object_key, artifact_sha256


def _read_simulation_replay_artifact(store_id: str, object_key: str) -> bytes | None:
    if store_id != _STORE.store_id:
        return None
    return _STORE.get(object_key)


__all__ = ["SIMULATION_REPLAY_STORE_ID"]
