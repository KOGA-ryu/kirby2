"""Deterministic DEV-0007 audit for learner-run pseudonym boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.curriculum.evidence import LearnerEvidenceLedgerV1
from kirby2.curriculum.learner import build_learner_projection_v1
from kirby2.pseudonyms import (
    derive_instructor_profile_id,
    derive_learner_profile_id,
    require_instructor_profile_id,
    require_learner_profile_id,
)
from kirby2.research import LearnerArtifactStore, RunStore


DEV0007_AUDIT_CASE_COUNT = 2
DEV0007_DIRECT_IDENTITY_MARKER = "ada.learner@example.invalid"

_AUDIT_ENTROPY = hashlib.sha256(
    b"KIRBY2_DEV_0007_INDEPENDENT_AUDIT_ENTROPY_V1\x00"
).digest()


@dataclass(frozen=True, slots=True)
class PseudonymousLearnerAuditCase:
    name: str
    detail: str
    evidence: dict[str, object]
    failures: tuple[str, ...]
    required: bool = True

    @property
    def passed(self) -> bool:
        return not self.failures


def _raises(operation) -> bool:
    try:
        operation()
    except (OSError, RuntimeError, TypeError, ValueError):
        return True
    return False


def _refuses_invalid_learner_id(operation) -> bool:
    try:
        operation()
    except ValueError as error:
        return str(error) == "learner profile ID is invalid"
    except (OSError, RuntimeError, TypeError):
        return False
    return False


def audit_dev0007_pseudonymous_learner_runs() -> tuple[
    PseudonymousLearnerAuditCase,
    ...,
]:
    """Run the fixed strict-ID and real artifact-store boundary checks."""

    cases = (
        _strict_role_scoped_derivation_case(),
        _learner_artifact_boundary_case(),
    )
    expected_names = (
        "opaque_profile_derivation_is_role_scoped_and_strict",
        "learner_store_refuses_direct_identity_and_round_trips_pseudonym",
    )
    if (
        len(cases) != DEV0007_AUDIT_CASE_COUNT
        or tuple(item.name for item in cases) != expected_names
    ):
        raise RuntimeError("DEV-0007 audit case inventory changed")
    return cases


def _strict_role_scoped_derivation_case() -> PseudonymousLearnerAuditCase:
    learner_id = derive_learner_profile_id(_AUDIT_ENTROPY)
    instructor_id = derive_instructor_profile_id(_AUDIT_ENTROPY)
    failures: list[str] = []
    derivation_stable = (
        learner_id == derive_learner_profile_id(_AUDIT_ENTROPY)
        and require_learner_profile_id(learner_id) == learner_id
        and require_instructor_profile_id(instructor_id) == instructor_id
    )
    if not derivation_stable:
        failures.append("opaque profile derivation or strict validation is unstable")
    role_domains_distinct = (
        learner_id.removeprefix("learner-profile-")
        != instructor_id.removeprefix("instructor-profile-")
    )
    if not role_domains_distinct:
        failures.append("instructor and learner derivations are not domain separated")
    refusal_checks = (
        _raises(lambda: require_learner_profile_id(DEV0007_DIRECT_IDENTITY_MARKER)),
        _raises(lambda: require_learner_profile_id(instructor_id)),
        _raises(lambda: require_instructor_profile_id(learner_id)),
        _raises(lambda: require_learner_profile_id(learner_id.upper())),
        _raises(lambda: derive_learner_profile_id(b"short")),
        _raises(lambda: derive_learner_profile_id(bytearray(_AUDIT_ENTROPY))),
    )
    if not all(refusal_checks):
        failures.append("malformed, cross-role, or non-bytes pseudonyms were accepted")
    return PseudonymousLearnerAuditCase(
        "opaque_profile_derivation_is_role_scoped_and_strict",
        (
            f"same_entropy_repeat={str(derivation_stable).lower()} "
            f"role_domains_distinct={str(role_domains_distinct).lower()} "
            f"refusals={sum(refusal_checks)}"
        ),
        {
            "derivation_repeat_count": 2,
            "direct_identity_inputs": 0,
            "refusal_count": sum(refusal_checks),
            "role_domain_count": 2,
        },
        tuple(failures),
    )


def _learner_artifact_boundary_case() -> PseudonymousLearnerAuditCase:
    valid_id = derive_learner_profile_id(_AUDIT_ENTROPY)
    valid_ledger = LearnerEvidenceLedgerV1(valid_id, ())
    valid_projection = build_learner_projection_v1(
        valid_ledger,
        as_of_attempt_ordinal=0,
    )
    direct_ledger = LearnerEvidenceLedgerV1(
        DEV0007_DIRECT_IDENTITY_MARKER,
        (),
    )
    direct_projection = build_learner_projection_v1(
        direct_ledger,
        as_of_attempt_ordinal=0,
    )
    failures: list[str] = []
    invalid_write_refused = False
    load_tamper_refused = False
    valid_round_trip = False
    verification_passed = False
    direct_marker_absent = False
    persisted_file_count = 0
    with TemporaryDirectory(prefix="kirby2-dev0007-") as directory:
        root = Path(directory) / "research"
        store = LearnerArtifactStore(root)
        invalid_write_refused = _refuses_invalid_learner_id(
            lambda: store.record_update(
                direct_ledger,
                direct_projection,
                seed=7,
                repository=Path(__file__).resolve().parents[2],
            )
        )
        if not invalid_write_refused:
            failures.append("email-like learner ID reached the persistence boundary")
        if tuple(store.runs_directory.iterdir()) or tuple(
            store.staging_directory.iterdir()
        ):
            failures.append("refused direct identity left a staged or persisted run")

        manifest = store.record_update(
            valid_ledger,
            valid_projection,
            seed=7,
            repository=Path(__file__).resolve().parents[2],
        )
        loaded_ledger, loaded_projection = store.load_update(manifest.run_id)
        local_verification = store.verify_run(manifest.run_id)
        dispatch_verification = RunStore(root).verify_run(manifest.run_id)
        run_directory = store.run_directory(manifest.run_id)
        persisted_files = tuple(
            path
            for path in sorted(run_directory.rglob("*"))
            if path.is_file()
        )
        persisted_file_count = len(persisted_files)
        persisted_bytes = b"".join(path.read_bytes() for path in persisted_files)
        valid_round_trip = (
            loaded_ledger.canonical_bytes() == valid_ledger.canonical_bytes()
            and loaded_projection.canonical_bytes()
            == valid_projection.canonical_bytes()
        )
        verification_passed = (
            local_verification.passed and dispatch_verification.passed
        )
        if not valid_round_trip or not verification_passed:
            failures.append("opaque learner update did not write, load, and verify")
        direct_marker_absent = (
            DEV0007_DIRECT_IDENTITY_MARKER.encode("ascii") not in persisted_bytes
        )
        if not direct_marker_absent:
            failures.append("direct identity marker entered valid learner-run bytes")

        (run_directory / "learner-update.json").write_bytes(
            direct_ledger.canonical_bytes()
        )
        (run_directory / "learner-projection.json").write_bytes(
            direct_projection.canonical_bytes()
        )
        load_tamper_refused = _refuses_invalid_learner_id(
            lambda: store.load_update(manifest.run_id)
        )
        if not load_tamper_refused:
            failures.append("load boundary accepted email-like learner artifact IDs")

    return PseudonymousLearnerAuditCase(
        "learner_store_refuses_direct_identity_and_round_trips_pseudonym",
        (
            f"pre_persistence_refusal={str(invalid_write_refused).lower()} "
            f"valid_round_trip={str(valid_round_trip).lower()} "
            f"verification={'PASS' if verification_passed else 'FAIL'} "
            f"direct_marker_absent={str(direct_marker_absent).lower()} "
            f"load_tamper_refused={str(load_tamper_refused).lower()}"
        ),
        {
            "direct_marker_absent": direct_marker_absent,
            "invalid_write_refused": invalid_write_refused,
            "load_tamper_refused": load_tamper_refused,
            "persisted_file_count": persisted_file_count,
        },
        tuple(failures),
    )


__all__ = [
    "DEV0007_AUDIT_CASE_COUNT",
    "DEV0007_DIRECT_IDENTITY_MARKER",
    "PseudonymousLearnerAuditCase",
    "audit_dev0007_pseudonymous_learner_runs",
]
