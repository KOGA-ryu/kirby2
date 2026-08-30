"""Deterministic local instructor-console demonstration command.

The demo is intentionally synthetic and offline.  It proves immutable workflow and
pseudonymous profile isolation only.  Equal synthetic scores are used throughout;
the artifact makes no learner difference, cohort difference, educational-effect,
or causal claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.research.paths import DataAreaId, DataPaths

from .assignments import (
    AssignmentLocksV1,
    AssignmentModeV1,
    AssignmentRevisionV1,
    AssignmentSpecV1,
    AssignmentTargetKindV1,
    AssignmentTargetV1,
    AssignmentAttemptManifestV1,
    AttemptRuntimeParametersV1,
    FeedbackTimingV1,
    HiddenStateRevealPolicyV1,
    HotkeyLayoutBindingV1,
    LessonReferenceV1,
    ResearchConsentEvidenceV1,
    ResearchConsentRequirementV1,
    RubricBindingV1,
    SeedPolicyKindV1,
    SeedPolicyV1,
    StrategyPolicyV1,
    bind_assignment_attempt,
    create_assignment,
)
from .cohorts import (
    CohortAssignmentBindingV1,
    CohortDefinitionV1,
    CohortRevisionV1,
    CohortSourceAttemptV1,
    CohortSummaryV1,
    build_cohort_summary,
    create_cohort,
)
from .consent import (
    ConsentDecisionStatusV1,
    ConsentRecordV1,
    ConsentScopeV1,
    ConsentStateV1,
    EvidenceExportPermissionV1,
    EvidenceRetentionPolicyV1,
    WithdrawalPolicyV1,
    create_consent_record,
    revise_consent_record,
)
from .deletion import (
    RetainedEvidenceKindV1,
    create_retained_evidence_reference,
    execute_profile_deletion,
    plan_profile_deletion,
)
from .export import (
    ExportOmissionReasonV1,
    ExportOmissionV1,
    build_export_bundle,
    create_selected_causal_trace,
    import_export_directory,
    verify_export_directory,
    write_export_directory,
)
from .identity import (
    DirectIdentifierV1,
    DirectIdentityV1,
    create_identity_mapping,
)
from .console import (
    NOT_APPLICABLE_VERSION,
    ConsoleArtifactKindV1,
    ConsoleArtifactReferenceV1,
    ConsoleCapabilityV1,
    ConsoleSourceIdentityV1,
    InstructorConsoleLedgerV1,
    create_console_artifact_reference,
    create_console_ledger,
    create_console_source_identity,
    record_assignment,
    record_attempt,
    record_cohort,
    record_comparison,
    record_profile,
    record_review,
    record_rubric,
    record_study,
)
from .models import (
    InstructorProfile,
    LearnerProfile,
    create_instructor_profile,
    create_learner_profile,
)
from .query import (
    ComparisonExecutionModeV1,
    ComparisonSourceV1,
    ComparisonViewKindV1,
    ComparisonViewV1,
    InstructorQueryScopeKindV1,
    InstructorQueryScopeV1,
    build_comparison_view,
)
from .redaction import (
    PORTABLE_EVIDENCE_ALLOWLIST_V1,
    create_portable_evidence_redaction_policy,
)
from .reviews import (
    AttemptReviewBindingV1,
    EvidenceEventReferenceV1,
    ReviewRevisionV1,
    RubricItemReferenceV1,
    RubricResultBindingV1,
    TimelineAnnotationV1,
    annotate_timeline,
    attach_rubric_result,
    create_review,
    inspect_causal_trace,
    mark_complete,
    open_attempt,
    replay_attempt,
)
from .rubrics import (
    RubricContentV1,
    RubricItemScoreV1,
    RubricItemV1,
    RubricRevisionV1,
    RubricScoreSidecarV1,
    create_rubric,
    score_attempt,
)
from .statistics import (
    AnalysisCapabilityV1,
    CompatibilityActionV1,
    MetricObservationV1,
    VersionSignatureV1,
)
from .studies import (
    AllocationMethodV1,
    AllocationRandomizationV1,
    AnalysisPlanV1,
    BlindingRevealV1,
    ContentLockV1,
    DesignCapabilityV1,
    MetricDeclarationV1,
    OutcomeDeclarationV1,
    ParameterLockV1,
    StudyAssignmentBindingV1,
    StudyConsentPolicyV1,
    StudyDataExportPolicyV1,
    StudyDesignKindV1,
    StudyDesignV1,
    StudyExecutionLedgerV1,
    StudyManifestV1,
    StudyRetentionPolicyV1,
    StudyRevisionV1,
    StudyStatusV1,
    create_study,
    create_study_execution_ledger,
    include_study_attempt,
)


INSTRUCTOR_DEMO_SCHEMA_ID = "KIRBY2_INSTRUCTOR_DEMO_V1"
INSTRUCTOR_DEMO_SCHEMA_VERSION = 1
INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_ID = "KIRBY2_INSTRUCTOR_DEMO_REVIEW_BUNDLE_V1"
INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_VERSION = 1
INSTRUCTOR_DEMO_CLAIM_SCOPE = "WORKFLOW_AND_PSEUDONYMOUS_PROFILE_ISOLATION_ONLY"
INSTRUCTOR_DEMO_EXTERNAL_SERVICE_POLICY = "LOCAL_ONLY_NO_EXTERNAL_SERVICES_V1"
INSTRUCTOR_DEMO_LEARNER_COUNT = 2
INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER = 3
INSTRUCTOR_DEMO_ATTEMPT_COUNT = 6
INSTRUCTOR_DEMO_METRIC_ID = "hidden_liquidity_review_score"
PRIVACY_EXPORT_FIXTURE_SCHEMA_ID = "KIRBY2_INSTRUCTOR_PRIVACY_EXPORT_FIXTURE_V1"
PRIVACY_EXPORT_FIXTURE_SCHEMA_VERSION = 1
INSTRUCTOR_EXPORT_DEMO_SCHEMA_ID = "KIRBY2_INSTRUCTOR_EXPORT_DEMO_V1"
INSTRUCTOR_EXPORT_DEMO_SCHEMA_VERSION = 1
PRIVACY_EXPORT_OMITTED_CATEGORIES_V1 = (
    "direct_identity",
    "identity_mapping",
    "local_paths",
    "secrets",
    "unauthorized_hidden_or_reveal_data",
    "unselected_causal_traces",
)
PRIVACY_EXPORT_COMPATIBILITY_COMPONENTS_V1 = (
    "attempt_manifest",
    "review",
    "rubric_score",
)
_MAX_SEED = (1 << 63) - 1


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return value


def _fields(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} fields differ")
    return value


def _seed(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SEED:
        raise ValueError(f"demo seed must be an integer from 0 through {_MAX_SEED}")
    return value


def _seed_argument(value: str) -> int:
    try:
        return _seed(int(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _fixture_section(
    value: object,
    expected: set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} fields differ")
    return value


def _fixture_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if any(character in value for character in "\x00\r\n"):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _fixture_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _fixture_text_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact TOML array")
    result = tuple(_fixture_text(item, label) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{label} cannot be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class PrivacyExportFixtureV1:
    """Strict, data-only configuration for the WO37-E production demo."""

    learner_ordinal: int
    attempt_number: int
    selected_causal_trace_ordinal: int
    recorded_at_utc: str
    decision_time_utc: str
    deletion_time_utc: str
    required_scope: ConsentScopeV1
    scopes: tuple[ConsentScopeV1, ...]
    retention_policy: EvidenceRetentionPolicyV1
    retain_after_profile_deletion: bool
    export_permission: EvidenceExportPermissionV1
    withdrawal_policy: WithdrawalPolicyV1
    redaction_policy_id: str
    redaction_policy_version: str
    allowlisted_paths: tuple[str, ...]
    authorized_hidden_paths: tuple[str, ...]
    directory_name: str
    software_version: str
    limitations: tuple[str, ...]
    omitted_categories: tuple[str, ...]
    compatibility_versions: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _fixture_positive_int(self.learner_ordinal, "fixture learner ordinal")
        _fixture_positive_int(self.attempt_number, "fixture attempt number")
        _fixture_positive_int(
            self.selected_causal_trace_ordinal,
            "fixture selected causal-trace ordinal",
        )
        if self.learner_ordinal > INSTRUCTOR_DEMO_LEARNER_COUNT:
            raise ValueError("fixture learner ordinal exceeds the demo learner count")
        if self.attempt_number > INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER:
            raise ValueError("fixture attempt number exceeds the per-learner limit")
        expected_trace_ordinal = (
            (self.learner_ordinal - 1) * INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER
            + self.attempt_number
        )
        if self.selected_causal_trace_ordinal != expected_trace_ordinal:
            raise ValueError(
                "fixture causal-trace ordinal differs from the selected attempt"
            )
        for label, value in (
            ("fixture consent recorded time", self.recorded_at_utc),
            ("fixture consent decision time", self.decision_time_utc),
            ("fixture deletion time", self.deletion_time_utc),
            ("fixture redaction policy ID", self.redaction_policy_id),
            ("fixture redaction policy version", self.redaction_policy_version),
            ("fixture export directory name", self.directory_name),
            ("fixture software version", self.software_version),
        ):
            _fixture_text(value, label)
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("fixture required scope is invalid")
        if type(self.scopes) is not tuple or not self.scopes or any(
            type(item) is not ConsentScopeV1 for item in self.scopes
        ):
            raise TypeError("fixture consent scopes are invalid")
        if tuple(sorted(set(self.scopes), key=lambda item: item.value)) != self.scopes:
            raise ValueError("fixture consent scopes must be unique and canonical")
        if self.scopes != tuple(ConsentScopeV1):
            raise ValueError(
                "privacy-export demo requires all evidence, review, cohort, and study scopes"
            )
        if self.required_scope not in self.scopes:
            raise ValueError("fixture required scope is absent from granted scopes")
        if self.required_scope is not ConsentScopeV1.INSTRUCTIONAL_EVIDENCE:
            raise ValueError(
                "privacy-export demo primary scope must be instructional evidence"
            )
        if type(self.retention_policy) is not EvidenceRetentionPolicyV1:
            raise TypeError("fixture retention policy is invalid")
        if (
            self.retention_policy
            is not EvidenceRetentionPolicyV1.RETAIN_WITHOUT_FIXED_END
        ):
            raise ValueError(
                "privacy-export demo requires retention without a fixed end"
            )
        if self.retain_after_profile_deletion is not True:
            raise ValueError("privacy-export fixture must authorize retained evidence")
        if (
            self.export_permission
            is not EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY
        ):
            raise ValueError("privacy-export fixture must grant redacted export only")
        if type(self.withdrawal_policy) is not WithdrawalPolicyV1:
            raise TypeError("fixture withdrawal policy is invalid")
        for value, label in (
            (self.allowlisted_paths, "fixture allowlisted paths"),
            (self.limitations, "fixture limitations"),
            (self.omitted_categories, "fixture omitted categories"),
        ):
            if type(value) is not tuple or not value or any(
                type(item) is not str or not item for item in value
            ):
                raise TypeError(f"{label} must be a nonempty text tuple")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("fixture limitations must be unique and canonical")
        if type(self.authorized_hidden_paths) is not tuple or any(
            type(item) is not str or not item for item in self.authorized_hidden_paths
        ):
            raise TypeError("fixture authorized hidden paths must be a text tuple")
        if self.allowlisted_paths != PORTABLE_EVIDENCE_ALLOWLIST_V1:
            raise ValueError(
                "privacy-export demo requires the exact portable-evidence allowlist"
            )
        create_portable_evidence_redaction_policy(
            policy_id=self.redaction_policy_id,
            policy_version=self.redaction_policy_version,
            allowlisted_paths=self.allowlisted_paths,
            authorized_hidden_paths=self.authorized_hidden_paths,
        )
        if self.omitted_categories != PRIVACY_EXPORT_OMITTED_CATEGORIES_V1:
            raise ValueError(
                "privacy-export demo requires the exact prohibited/omitted inventory"
            )
        if (
            type(self.compatibility_versions) is not tuple
            or not self.compatibility_versions
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not int
                or item[1] <= 0
                for item in self.compatibility_versions
            )
        ):
            raise TypeError("fixture compatibility versions are invalid")
        if tuple(sorted(self.compatibility_versions)) != self.compatibility_versions:
            raise ValueError("fixture compatibility versions must be canonical")
        if tuple(item[0] for item in self.compatibility_versions) != (
            PRIVACY_EXPORT_COMPATIBILITY_COMPONENTS_V1
        ):
            raise ValueError(
                "privacy-export fixture compatibility components differ"
            )
        safe_name_characters = frozenset(
            "abcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        if not (
            self.directory_name[0]
            in frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
            and all(
                character in safe_name_characters
                for character in self.directory_name
            )
        ):
            raise ValueError("fixture directory name must be one lowercase safe name")

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> PrivacyExportFixtureV1:
        if type(raw) is not bytes or not raw or len(raw) > 64 * 1024:
            raise ValueError("privacy-export fixture byte length is invalid")
        try:
            root = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("privacy-export fixture must be strict UTF-8 TOML") from error
        payload = _fixture_section(
            root,
            {"consent", "export", "redaction", "schema_id", "schema_version", "selection"},
            "privacy-export fixture",
        )
        if (
            payload["schema_id"] != PRIVACY_EXPORT_FIXTURE_SCHEMA_ID
            or payload["schema_version"] != PRIVACY_EXPORT_FIXTURE_SCHEMA_VERSION
        ):
            raise ValueError("privacy-export fixture schema differs")
        selection = _fixture_section(
            payload["selection"],
            {"attempt_number", "learner_ordinal", "selected_causal_trace_ordinal"},
            "privacy-export fixture selection",
        )
        consent = _fixture_section(
            payload["consent"],
            {
                "decision_time_utc",
                "deletion_time_utc",
                "export_permission",
                "recorded_at_utc",
                "required_scope",
                "retain_pseudonymous_evidence_after_profile_deletion",
                "retention_policy",
                "scopes",
                "withdrawal_policy",
            },
            "privacy-export fixture consent",
        )
        redaction = _fixture_section(
            payload["redaction"],
            {
                "allowlisted_paths",
                "authorized_hidden_paths",
                "policy_id",
                "policy_version",
            },
            "privacy-export fixture redaction",
        )
        export = _fixture_section(
            payload["export"],
            {
                "compatibility_versions",
                "directory_name",
                "limitations",
                "omitted_categories",
                "software_version",
            },
            "privacy-export fixture export",
        )
        raw_compatibility = export["compatibility_versions"]
        if type(raw_compatibility) is not dict or not raw_compatibility:
            raise TypeError("fixture compatibility versions must be a TOML table")
        compatibility_versions = tuple(
            sorted(
                (
                    _fixture_text(key, "fixture compatibility component"),
                    _fixture_positive_int(
                        value,
                        f"fixture compatibility version for {key}",
                    ),
                )
                for key, value in raw_compatibility.items()
            )
        )
        return cls(
            learner_ordinal=_fixture_positive_int(
                selection["learner_ordinal"], "fixture learner ordinal"
            ),
            attempt_number=_fixture_positive_int(
                selection["attempt_number"], "fixture attempt number"
            ),
            selected_causal_trace_ordinal=_fixture_positive_int(
                selection["selected_causal_trace_ordinal"],
                "fixture selected causal-trace ordinal",
            ),
            recorded_at_utc=_fixture_text(
                consent["recorded_at_utc"], "fixture consent recorded time"
            ),
            decision_time_utc=_fixture_text(
                consent["decision_time_utc"], "fixture consent decision time"
            ),
            deletion_time_utc=_fixture_text(
                consent["deletion_time_utc"], "fixture deletion time"
            ),
            required_scope=ConsentScopeV1(consent["required_scope"]),
            scopes=tuple(
                sorted(
                    (ConsentScopeV1(item) for item in _fixture_text_tuple(
                        consent["scopes"], "fixture consent scopes"
                    )),
                    key=lambda item: item.value,
                )
            ),
            retention_policy=EvidenceRetentionPolicyV1(consent["retention_policy"]),
            retain_after_profile_deletion=(
                consent["retain_pseudonymous_evidence_after_profile_deletion"]
            ),
            export_permission=EvidenceExportPermissionV1(consent["export_permission"]),
            withdrawal_policy=WithdrawalPolicyV1(consent["withdrawal_policy"]),
            redaction_policy_id=_fixture_text(
                redaction["policy_id"], "fixture redaction policy ID"
            ),
            redaction_policy_version=_fixture_text(
                redaction["policy_version"], "fixture redaction policy version"
            ),
            allowlisted_paths=_fixture_text_tuple(
                redaction["allowlisted_paths"], "fixture allowlisted paths"
            ),
            authorized_hidden_paths=_fixture_text_tuple(
                redaction["authorized_hidden_paths"],
                "fixture authorized hidden paths",
                allow_empty=True,
            ),
            directory_name=_fixture_text(
                export["directory_name"], "fixture export directory name"
            ),
            software_version=_fixture_text(
                export["software_version"], "fixture software version"
            ),
            limitations=_fixture_text_tuple(
                export["limitations"], "fixture limitations"
            ),
            omitted_categories=tuple(sorted(_fixture_text_tuple(
                export["omitted_categories"], "fixture omitted categories"
            ))),
            compatibility_versions=compatibility_versions,
        )


def load_privacy_export_fixture(path: Path) -> PrivacyExportFixtureV1:
    if type(path) is not Path:
        raise TypeError("privacy-export fixture path must be pathlib.Path")
    if path.is_symlink() or not path.is_file():
        raise ValueError("privacy-export fixture must be one regular non-symlink file")
    return PrivacyExportFixtureV1.from_toml_bytes(path.read_bytes())


def _demo_digest(seed: int, label: str) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "domain": "KIRBY2_INSTRUCTOR_DEMO_DERIVATION_V1",
                "label": label,
                "seed": _seed(seed),
            }
        )
    ).hexdigest()


def _demo_entropy(seed: int, label: str) -> bytes:
    return bytes.fromhex(_demo_digest(seed, f"profile:{label}"))


def _profile_from_dict(value: object, *, learner: bool):
    label = "learner profile" if learner else "instructor profile"
    payload = _fields(
        value,
        {"profile_id", "profile_kind", "schema_id", "schema_version"},
        label,
    )
    profile = (
        LearnerProfile(profile_id=payload["profile_id"])
        if learner
        else InstructorProfile(profile_id=payload["profile_id"])
    )
    if profile.as_dict() != payload:
        raise ValueError(f"{label} differs from its canonical identity")
    return profile


@dataclass(frozen=True, slots=True)
class InstructorDemoReviewBundleV1:
    """One immutable bundle containing every score and completed review."""

    reviewer_profile_id: str
    rubric_id: str
    rubric_sha256: str
    scores: tuple[RubricScoreSidecarV1, ...]
    reviews: tuple[ReviewRevisionV1, ...]
    schema_id: str = INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_ID
    schema_version: int = INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_VERSION
    review_bundle_id: str = field(init=False)

    def __post_init__(self) -> None:
        InstructorProfile(profile_id=self.reviewer_profile_id)
        if type(self.rubric_id) is not str or not self.rubric_id.startswith("rubric-"):
            raise ValueError("demo review bundle rubric ID is invalid")
        if (
            type(self.rubric_sha256) is not str
            or len(self.rubric_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.rubric_sha256)
        ):
            raise ValueError("demo review bundle rubric digest is invalid")
        if type(self.scores) is not tuple or len(self.scores) != INSTRUCTOR_DEMO_ATTEMPT_COUNT:
            raise ValueError("demo review bundle requires exactly six scores")
        if any(type(item) is not RubricScoreSidecarV1 for item in self.scores):
            raise TypeError("demo review bundle scores use the wrong type")
        if type(self.reviews) is not tuple or len(self.reviews) != INSTRUCTOR_DEMO_ATTEMPT_COUNT:
            raise ValueError("demo review bundle requires exactly six reviews")
        if any(type(item) is not ReviewRevisionV1 for item in self.reviews):
            raise TypeError("demo review bundle reviews use the wrong type")
        score_attempt_ids = tuple(item.assignment_attempt_id for item in self.scores)
        review_attempt_ids = tuple(item.attempt_id for item in self.reviews)
        if score_attempt_ids != tuple(sorted(score_attempt_ids)):
            raise ValueError("demo scores must be in canonical attempt-ID order")
        if review_attempt_ids != tuple(sorted(review_attempt_ids)):
            raise ValueError("demo reviews must be in canonical attempt-ID order")
        if score_attempt_ids != review_attempt_ids or len(set(score_attempt_ids)) != 6:
            raise ValueError("demo scores and reviews must cover the same six attempts")
        if any(
            not item.sidecar.completed or not item.sidecar.timeline_annotations
            for item in self.reviews
        ):
            raise ValueError("every demo review must be complete and annotated")
        if any(item.reviewer_profile_id != self.reviewer_profile_id for item in self.reviews):
            raise ValueError("demo review bundle changed reviewer profile")
        if any(
            item.rubric_record_id != self.rubric_id
            or item.rubric_record_sha256 != self.rubric_sha256
            for item in self.scores
        ):
            raise ValueError("demo review scores changed rubric binding")
        if (
            self.schema_id != INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_ID
            or self.schema_version != INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_VERSION
        ):
            raise ValueError("demo review bundle schema differs")
        object.__setattr__(
            self,
            "review_bundle_id",
            "instructor-review-bundle-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "reviewer_profile_id": self.reviewer_profile_id,
            "reviews": [item.as_dict() for item in self.reviews],
            "rubric_id": self.rubric_id,
            "rubric_sha256": self.rubric_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scores": [item.as_dict() for item in self.scores],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "review_bundle_id": self.review_bundle_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> InstructorDemoReviewBundleV1:
        payload = _fields(
            value,
            {
                "review_bundle_id",
                "reviewer_profile_id",
                "reviews",
                "rubric_id",
                "rubric_sha256",
                "schema_id",
                "schema_version",
                "scores",
            },
            "instructor demo review bundle",
        )
        raw_scores = payload["scores"]
        raw_reviews = payload["reviews"]
        if type(raw_scores) is not list or type(raw_reviews) is not list:
            raise TypeError("demo review bundle scores and reviews must be arrays")
        bundle = cls(
            reviewer_profile_id=payload["reviewer_profile_id"],
            rubric_id=payload["rubric_id"],
            rubric_sha256=payload["rubric_sha256"],
            scores=tuple(RubricScoreSidecarV1.from_dict(item) for item in raw_scores),
            reviews=tuple(ReviewRevisionV1.from_dict(item) for item in raw_reviews),
            schema_id=payload["schema_id"],
            schema_version=payload["schema_version"],
        )
        if bundle.as_dict() != payload:
            raise ValueError("instructor demo review bundle did not round-trip")
        return bundle

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> InstructorDemoReviewBundleV1:
        bundle = cls.from_dict(_canonical_object(raw, "instructor demo review bundle"))
        if bundle.canonical_bytes() != raw:
            raise ValueError("instructor demo review bundle changed during restoration")
        return bundle


@dataclass(frozen=True, slots=True)
class InstructorDemoV1:
    """Durable exact artifact produced by :func:`build_instructor_demo`."""

    seed: int
    reviewer_profile: InstructorProfile
    learner_profiles: tuple[LearnerProfile, ...]
    consents: tuple[ConsentRecordV1, ...]
    assignment: AssignmentRevisionV1
    attempts: tuple[AssignmentAttemptManifestV1, ...]
    rubric: RubricRevisionV1
    review_bundle: InstructorDemoReviewBundleV1
    study_revision: StudyRevisionV1
    study_ledger: StudyExecutionLedgerV1
    cohort: CohortRevisionV1
    cohort_summary: CohortSummaryV1
    cohort_comparison: ComparisonViewV1
    console_ledger: InstructorConsoleLedgerV1
    schema_id: str = INSTRUCTOR_DEMO_SCHEMA_ID
    schema_version: int = INSTRUCTOR_DEMO_SCHEMA_VERSION
    demo_id: str = field(init=False)

    claim_scope: ClassVar[str] = INSTRUCTOR_DEMO_CLAIM_SCOPE
    external_service_policy: ClassVar[str] = INSTRUCTOR_DEMO_EXTERNAL_SERVICE_POLICY
    learner_difference_claim: ClassVar[bool] = False
    cohort_difference_claim: ClassVar[bool] = False
    causal_claim: ClassVar[bool] = False

    def __post_init__(self) -> None:
        _seed(self.seed)
        if type(self.reviewer_profile) is not InstructorProfile:
            raise TypeError("demo reviewer must be InstructorProfile")
        if type(self.learner_profiles) is not tuple or len(self.learner_profiles) != 2:
            raise ValueError("instructor demo requires exactly two learner profiles")
        if any(type(item) is not LearnerProfile for item in self.learner_profiles):
            raise TypeError("demo learner profiles use the wrong type")
        learner_ids = tuple(item.profile_id for item in self.learner_profiles)
        if learner_ids != tuple(sorted(learner_ids)) or len(set(learner_ids)) != 2:
            raise ValueError("demo learner profiles must be unique and canonically ordered")
        if type(self.consents) is not tuple or len(self.consents) != 2:
            raise ValueError("instructor demo requires one consent per learner")
        if tuple(item.pseudonymous_profile_id for item in self.consents) != learner_ids:
            raise ValueError("demo consents do not map one-to-one to learner profiles")
        if type(self.assignment) is not AssignmentRevisionV1:
            raise TypeError("demo assignment must be AssignmentRevisionV1")
        if "hidden" not in self.assignment.spec.target.lessons[0].lesson_id.lower():
            raise ValueError("demo assignment must be the hidden-liquidity assignment")
        if type(self.attempts) is not tuple or len(self.attempts) != 6:
            raise ValueError("instructor demo requires exactly six attempts")
        if any(type(item) is not AssignmentAttemptManifestV1 for item in self.attempts):
            raise TypeError("demo attempts use the wrong type")
        attempt_keys = tuple(
            (item.learner_profile_id, item.attempt_number) for item in self.attempts
        )
        expected_attempt_keys = tuple(
            (learner_id, ordinal)
            for learner_id in learner_ids
            for ordinal in range(1, INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER + 1)
        )
        if attempt_keys != expected_attempt_keys:
            raise ValueError("each demo learner must have exactly attempts one through three")
        if any(item.assignment_revision.sha256 != self.assignment.sha256 for item in self.attempts):
            raise ValueError("demo attempt changed the one exact assignment")
        if type(self.rubric) is not RubricRevisionV1:
            raise TypeError("demo rubric must be RubricRevisionV1")
        if type(self.review_bundle) is not InstructorDemoReviewBundleV1:
            raise TypeError("demo review bundle uses the wrong type")
        if self.review_bundle.reviewer_profile_id != self.reviewer_profile.profile_id:
            raise ValueError("demo review bundle changed reviewer profile")
        if self.review_bundle.rubric_id != self.rubric.rubric_id:
            raise ValueError("demo review bundle changed the one rubric")
        if {item.attempt_id for item in self.attempts} != {
            item.attempt_id for item in self.review_bundle.reviews
        }:
            raise ValueError("demo review bundle does not cover every attempt")
        if type(self.study_revision) is not StudyRevisionV1:
            raise TypeError("demo study must be StudyRevisionV1")
        if type(self.study_ledger) is not StudyExecutionLedgerV1:
            raise TypeError("demo study ledger must be StudyExecutionLedgerV1")
        if self.study_ledger.study_revision.sha256 != self.study_revision.sha256:
            raise ValueError("demo study ledger changed study revision")
        if len(self.study_ledger.included_attempts) != 6:
            raise ValueError("demo study ledger must include all six attempts")
        if type(self.cohort) is not CohortRevisionV1:
            raise TypeError("demo cohort must be CohortRevisionV1")
        if tuple(self.cohort.definition.member_profile_ids) != learner_ids:
            raise ValueError("demo cohort changed learner membership")
        if type(self.cohort_summary) is not CohortSummaryV1:
            raise TypeError("demo cohort summary must be CohortSummaryV1")
        if (
            self.cohort_summary.cohort_id != self.cohort.cohort_id
            or self.cohort_summary.member_count != 2
            or self.cohort_summary.eligible_denominator != 6
            or self.cohort_summary.requested_capability
            is not AnalysisCapabilityV1.DESCRIPTIVE
        ):
            raise ValueError("demo cohort summary exceeds its exact descriptive scope")
        if type(self.cohort_comparison) is not ComparisonViewV1:
            raise TypeError("demo cohort comparison must be ComparisonViewV1")
        if (
            self.cohort_comparison.view_kind
            is not ComparisonViewKindV1.SAME_LESSON_ACROSS_LEARNERS
            or self.cohort_comparison.scope.scope_kind
            is not InstructorQueryScopeKindV1.COHORT_RESEARCH
            or self.cohort_comparison.scope.principal_profile_id
            != self.reviewer_profile.profile_id
            or self.cohort_comparison.scope.cohort_id != self.cohort.cohort_id
            or self.cohort_comparison.scope.study_id != self.study_revision.study_id
            or self.cohort_comparison.sample_count != 6
            or self.cohort_comparison.capability
            is not ConsoleCapabilityV1.DESCRIPTIVE
            or not self.cohort_comparison.consent_eligible
            or self.cohort_comparison.export_eligible
        ):
            raise ValueError("demo comparison exceeds its exact descriptive query scope")
        exact_attempts = {(item.attempt_id, item.sha256) for item in self.attempts}
        if {
            (item.attempt_id, item.attempt_sha256)
            for item in self.cohort_comparison.sources
        } != exact_attempts:
            raise ValueError("demo comparison does not bind all six exact attempts")
        if type(self.console_ledger) is not InstructorConsoleLedgerV1:
            raise TypeError("demo console ledger must be InstructorConsoleLedgerV1")
        if self.cohort_comparison.as_of_sequence != self.console_ledger.head_sequence - 1:
            raise ValueError("demo comparison must use the pre-comparison console point")
        comparison_snapshot = self.console_ledger.as_of(
            self.cohort_comparison.as_of_sequence
        )
        if comparison_snapshot.head_sha256 != self.cohort_comparison.ledger_sha256:
            raise ValueError("demo comparison changed its exact console ledger point")
        rebuilt_comparison = build_comparison_view(
            comparison_snapshot,
            view_kind=self.cohort_comparison.view_kind,
            scope=self.cohort_comparison.scope,
            sources=self.cohort_comparison.sources,
            as_of=comparison_snapshot.head_sequence,
        )
        if rebuilt_comparison != self.cohort_comparison:
            raise ValueError("demo comparison differs from its exact console query")
        ledger_kinds = tuple(
            item.artifact_reference.artifact_kind for item in self.console_ledger.entries
        )
        expected_kind_counts = {
            ConsoleArtifactKindV1.PROFILE: 3,
            ConsoleArtifactKindV1.ASSIGNMENT: 1,
            ConsoleArtifactKindV1.ASSIGNMENT_ATTEMPT: 6,
            ConsoleArtifactKindV1.RUBRIC: 1,
            ConsoleArtifactKindV1.REVIEW: 1,
            ConsoleArtifactKindV1.COHORT: 1,
            ConsoleArtifactKindV1.STUDY: 1,
            ConsoleArtifactKindV1.COMPARISON: 1,
        }
        if len(ledger_kinds) != sum(expected_kind_counts.values()) or any(
            ledger_kinds.count(kind) != count
            for kind, count in expected_kind_counts.items()
        ):
            raise ValueError("demo console ledger changed its exact artifact counts")
        comparison_reference = self.console_ledger.entries[-1].artifact_reference
        if (
            comparison_reference.artifact_kind
            is not ConsoleArtifactKindV1.COMPARISON
            or comparison_reference.artifact_id
            != self.cohort_comparison.comparison_id
            or comparison_reference.artifact_sha256
            != self.cohort_comparison.comparison_sha256
        ):
            raise ValueError("demo console ledger changed the one comparison artifact")
        if self.schema_id != INSTRUCTOR_DEMO_SCHEMA_ID or self.schema_version != 1:
            raise ValueError("instructor demo schema differs")
        object.__setattr__(
            self,
            "demo_id",
            "instructor-demo-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    @property
    def counts(self) -> dict[str, int]:
        return {
            "annotated_reviews": sum(
                bool(item.sidecar.timeline_annotations)
                for item in self.review_bundle.reviews
            ),
            "assignments": 1,
            "attempts": len(self.attempts),
            "attempts_per_learner": INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER,
            "cohort_comparisons": 1,
            "cohorts": 1,
            "completed_reviews": sum(
                item.sidecar.completed for item in self.review_bundle.reviews
            ),
            "console_entries": self.console_ledger.head_sequence,
            "learner_profiles": len(self.learner_profiles),
            "review_bundles": 1,
            "reviewer_profiles": 1,
            "rubrics": 1,
            "studies": 1,
        }

    def identity_dict(self) -> dict[str, object]:
        return {
            "assignment": self.assignment.as_dict(),
            "attempts": [item.as_dict() for item in self.attempts],
            "causal_claim": self.causal_claim,
            "claim_scope": self.claim_scope,
            "cohort": self.cohort.as_dict(),
            "cohort_comparison": self.cohort_comparison.as_dict(),
            "cohort_difference_claim": self.cohort_difference_claim,
            "cohort_summary": self.cohort_summary.as_dict(),
            "consents": [item.as_dict() for item in self.consents],
            "console_ledger": self.console_ledger.as_dict(),
            "counts": self.counts,
            "external_service_policy": self.external_service_policy,
            "learner_difference_claim": self.learner_difference_claim,
            "learner_profiles": [item.as_dict() for item in self.learner_profiles],
            "review_bundle": self.review_bundle.as_dict(),
            "reviewer_profile": self.reviewer_profile.as_dict(),
            "rubric": self.rubric.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "study_ledger": self.study_ledger.as_dict(),
            "study_revision": self.study_revision.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "demo_id": self.demo_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> InstructorDemoV1:
        expected = {
            "assignment",
            "attempts",
            "causal_claim",
            "claim_scope",
            "cohort",
            "cohort_comparison",
            "cohort_difference_claim",
            "cohort_summary",
            "consents",
            "console_ledger",
            "counts",
            "demo_id",
            "external_service_policy",
            "learner_difference_claim",
            "learner_profiles",
            "review_bundle",
            "reviewer_profile",
            "rubric",
            "schema_id",
            "schema_version",
            "seed",
            "study_ledger",
            "study_revision",
        }
        payload = _fields(value, expected, "instructor demo")
        raw_learners = payload["learner_profiles"]
        raw_consents = payload["consents"]
        raw_attempts = payload["attempts"]
        if not all(type(item) is list for item in (raw_learners, raw_consents, raw_attempts)):
            raise TypeError("instructor demo repeated records must be arrays")
        demo = cls(
            seed=_seed(payload["seed"]),
            reviewer_profile=_profile_from_dict(payload["reviewer_profile"], learner=False),
            learner_profiles=tuple(
                _profile_from_dict(item, learner=True) for item in raw_learners
            ),
            consents=tuple(ConsentRecordV1.from_dict(item) for item in raw_consents),
            assignment=AssignmentRevisionV1.from_dict(payload["assignment"]),
            attempts=tuple(
                AssignmentAttemptManifestV1.from_dict(item) for item in raw_attempts
            ),
            rubric=RubricRevisionV1.from_dict(payload["rubric"]),
            review_bundle=InstructorDemoReviewBundleV1.from_dict(
                payload["review_bundle"]
            ),
            study_revision=StudyRevisionV1.from_dict(payload["study_revision"]),
            study_ledger=StudyExecutionLedgerV1.from_dict(payload["study_ledger"]),
            cohort=CohortRevisionV1.from_dict(payload["cohort"]),
            cohort_summary=CohortSummaryV1.from_dict(payload["cohort_summary"]),
            cohort_comparison=ComparisonViewV1.from_dict(payload["cohort_comparison"]),
            console_ledger=InstructorConsoleLedgerV1.from_dict(
                payload["console_ledger"]
            ),
            schema_id=payload["schema_id"],
            schema_version=payload["schema_version"],
        )
        if demo.as_dict() != payload:
            raise ValueError("instructor demo did not round-trip exactly")
        return demo

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> InstructorDemoV1:
        demo = cls.from_dict(_canonical_object(raw, "instructor demo"))
        if demo.canonical_bytes() != raw:
            raise ValueError("instructor demo changed during restoration")
        return demo

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> InstructorDemoV1:
        return cls.from_json_bytes(raw)


def _build_rubric() -> RubricRevisionV1:
    return create_rubric(
        RubricContentV1(
            title="Hidden-liquidity workflow rubric",
            description="Synthetic workflow evidence only; not a learner ranking.",
            score_unit="points",
            scoring_version=1,
            items=(
                RubricItemV1(
                    item_id="execution-discipline",
                    label="Execution discipline",
                    description="Uses the locked execution controls.",
                    maximum_score=5,
                    evidence_required=True,
                ),
                RubricItemV1(
                    item_id="hidden-liquidity-identification",
                    label="Hidden-liquidity identification",
                    description="Identifies the synthetic hidden-liquidity cue.",
                    maximum_score=5,
                    evidence_required=True,
                ),
            ),
            passing_score=None,
        )
    )


def _build_assignment(seed: int, rubric: RubricRevisionV1) -> AssignmentRevisionV1:
    seed_policy = SeedPolicyV1(
        kind=SeedPolicyKindV1.ASSIGNMENT_ATTEMPT_DERIVED,
        fixed_seed=None,
        derivation_namespace=f"kirby2.instructor-demo.{seed}",
    )
    lesson = LessonReferenceV1(
        lesson_id="lesson.hidden-liquidity.local-v1",
        lesson_sha256=_demo_digest(seed, "hidden-liquidity-lesson"),
    )
    return create_assignment(
        AssignmentSpecV1(
            target=AssignmentTargetV1(
                kind=AssignmentTargetKindV1.LESSON,
                lessons=(lesson,),
            ),
            curriculum_sha256=_demo_digest(seed, "curriculum"),
            scenario_sha256=_demo_digest(seed, "hidden-liquidity-scenario"),
            pack_sha256=_demo_digest(seed, "lesson-pack"),
            allowed_scenario_variations=(
                "hidden_liquidity_baseline",
                "hidden_liquidity_fragmented",
            ),
            locks=AssignmentLocksV1(
                latency_sha256=_demo_digest(seed, "latency-lock"),
                volume_sha256=_demo_digest(seed, "volume-lock"),
                liquidity_sha256=_demo_digest(seed, "liquidity-lock"),
                strategy_sha256=_demo_digest(seed, "strategy-lock"),
                objective="Detect synthetic hidden liquidity without revealing hidden state.",
                venue_count=2,
                hidden_state_reveal_policy=(
                    HiddenStateRevealPolicyV1.AFTER_REVIEW_COMPLETION
                ),
                seed_policy=seed_policy,
            ),
            mode=AssignmentModeV1.RESEARCH,
            strategy_policy=StrategyPolicyV1.HIDDEN_UNTIL_REVIEW,
            hotkey_layout=HotkeyLayoutBindingV1(
                layout_name="instructor-demo-v1",
                layout_sha256=_demo_digest(seed, "hotkey-layout"),
            ),
            attempt_limit=3,
            deadline=None,
            feedback_timing=FeedbackTimingV1.AFTER_REVIEW,
            scoring_version="1",
            rubric=RubricBindingV1(
                rubric_record_id=rubric.rubric_id,
                rubric_sha256=rubric.record_sha256,
                rubric_version=rubric.revision,
            ),
            research_consent=ResearchConsentRequirementV1(
                authorization_policy_id="KIRBY2_LOCAL_INSTRUCTOR_DEMO_CONSENT_V1",
                evidence_purpose="Offline synthetic instructor workflow demonstration.",
                required_scopes=(
                    ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,
                    ConsentScopeV1.INSTRUCTOR_REVIEW,
                    ConsentScopeV1.LOCAL_COHORT_ANALYSIS,
                    ConsentScopeV1.LOCAL_RESEARCH_STUDY,
                ),
            ),
        )
    )


def _build_consent(
    seed: int,
    learner: LearnerProfile,
    ordinal: int,
) -> tuple[ConsentRecordV1, ResearchConsentEvidenceV1]:
    scopes = (
        ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,
        ConsentScopeV1.INSTRUCTOR_REVIEW,
        ConsentScopeV1.LOCAL_COHORT_ANALYSIS,
        ConsentScopeV1.LOCAL_RESEARCH_STUDY,
    )
    consent = create_consent_record(
        pseudonymous_profile_id=learner.profile_id,
        scopes=scopes,
        recorded_at_utc=f"2026-01-01T00:00:0{ordinal}Z",
        retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
        retention_until_utc=None,
        retain_pseudonymous_evidence_after_profile_deletion=False,
        export_permission=EvidenceExportPermissionV1.DENIED,
        withdrawal_policy=WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT,
    )
    decision_sha256 = _demo_digest(seed, f"consent-authorization:{learner.profile_id}")
    evidence = ResearchConsentEvidenceV1(
        learner_profile_id=learner.profile_id,
        consent_id=consent.consent_id,
        consent_sha256=consent.consent_sha256,
        consent_revision=consent.revision,
        consent_state=ConsentStateV1.GRANTED,
        granted_scopes=consent.scopes,
        authorization_policy_id="KIRBY2_LOCAL_INSTRUCTOR_DEMO_CONSENT_V1",
        authorization_decision_id="consent-decision-" + decision_sha256[:24],
        authorization_decision_sha256=decision_sha256,
        authorization_status=ConsentDecisionStatusV1.AUTHORIZED,
    )
    return consent, evidence


def _build_attempts(
    assignment: AssignmentRevisionV1,
    learners: tuple[LearnerProfile, ...],
    consent_evidence: dict[str, ResearchConsentEvidenceV1],
) -> tuple[AssignmentAttemptManifestV1, ...]:
    attempts: list[AssignmentAttemptManifestV1] = []
    for learner_index, learner in enumerate(learners, start=1):
        for attempt_number in range(1, INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER + 1):
            attempt_seed = assignment.spec.locks.seed_policy.expected_seed(
                assignment_lineage_id=assignment.assignment.lineage_id,
                assignment_revision=assignment.assignment.revision,
                learner_profile_id=learner.profile_id,
                attempt_number=attempt_number,
            )
            if attempt_seed is None:
                raise ValueError("demo assignment seed policy failed to derive a seed")
            attempts.append(
                bind_assignment_attempt(
                    assignment_revision=assignment,
                    learner_profile_id=learner.profile_id,
                    attempt_number=attempt_number,
                    run_id=f"run.instructor-demo.{learner_index}.{attempt_number}",
                    selected_lesson=assignment.spec.target.lessons[0],
                    selected_scenario_variation=(
                        "hidden_liquidity_baseline"
                        if attempt_number % 2
                        else "hidden_liquidity_fragmented"
                    ),
                    runtime_parameters=AttemptRuntimeParametersV1(
                        latency_sha256=assignment.spec.locks.latency_sha256,
                        volume_sha256=assignment.spec.locks.volume_sha256,
                        liquidity_sha256=assignment.spec.locks.liquidity_sha256,
                        strategy_sha256=assignment.spec.locks.strategy_sha256,
                        objective=assignment.spec.locks.objective,
                        venue_count=assignment.spec.locks.venue_count,
                        hidden_state_reveal_policy=(
                            assignment.spec.locks.hidden_state_reveal_policy
                        ),
                        seed_policy=assignment.spec.locks.seed_policy,
                        seed=attempt_seed,
                    ),
                    consent_evidence=consent_evidence[learner.profile_id],
                    recorded_at_utc=(
                        f"2026-01-01T01:{learner_index}{attempt_number}:00Z"
                    ),
                )
            )
    return tuple(attempts)


def _build_review_bundle(
    seed: int,
    reviewer: InstructorProfile,
    rubric: RubricRevisionV1,
    attempts: tuple[AssignmentAttemptManifestV1, ...],
) -> InstructorDemoReviewBundleV1:
    rubric_references = tuple(
        sorted(
            (
                RubricItemReferenceV1(
                    rubric_id=rubric.rubric_id,
                    rubric_sha256=rubric.record_sha256,
                    item_id=item.item_id,
                )
                for item in rubric.content.items
            ),
            key=lambda item: item.canonical_bytes(),
        )
    )
    completed: list[tuple[RubricScoreSidecarV1, ReviewRevisionV1]] = []
    for index, attempt in enumerate(attempts, start=1):
        evidence_id = f"evidence.instructor-demo.{index}"
        event_ids = (
            f"event.instructor-demo.{index}.execution",
            f"event.instructor-demo.{index}.hidden-liquidity",
        )
        evidence = EvidenceEventReferenceV1(
            evidence_id=evidence_id,
            evidence_sha256=_demo_digest(seed, evidence_id),
            event_ids=event_ids,
        )
        score = score_attempt(
            assignment_attempt_id=attempt.attempt_id,
            assignment_attempt_sha256=attempt.sha256,
            rubric=rubric,
            item_scores=tuple(
                RubricItemScoreV1(
                    item_id=item.item_id,
                    awarded_score=4,
                    maximum_score=item.maximum_score,
                    evidence_event_ids=(event_ids[item_index],),
                )
                for item_index, item in enumerate(rubric.content.items)
            ),
        )
        binding = AttemptReviewBindingV1(
            attempt_id=attempt.attempt_id,
            attempt_sha256=attempt.sha256,
            replay_id=f"replay.instructor-demo.{index}",
            replay_sha256=_demo_digest(seed, f"replay:{index}"),
            causal_trace_id=f"trace.instructor-demo.{index}",
            causal_trace_sha256=_demo_digest(seed, f"trace:{index}"),
        )
        review = inspect_causal_trace(replay_attempt(open_attempt(create_review(
            reviewer_profile_id=reviewer.profile_id,
            attempt=binding,
        ))))
        review = annotate_timeline(
            review,
            TimelineAnnotationV1(
                replay_time_us=index * 1_000,
                body="Synthetic workflow annotation; no learner-difference inference.",
                rubric_items=rubric_references,
                evidence=(evidence,),
            ),
        )
        review = attach_rubric_result(
            review,
            RubricResultBindingV1(
                result_id=score.score_id,
                result_sha256=score.sha256,
                assignment_attempt_id=attempt.attempt_id,
                assignment_attempt_sha256=attempt.sha256,
                rubric_id=rubric.rubric_id,
                rubric_sha256=rubric.record_sha256,
                rubric_items=rubric_references,
            ),
        )
        completed.append((score, mark_complete(review)))
    completed.sort(key=lambda item: item[0].assignment_attempt_id)
    return InstructorDemoReviewBundleV1(
        reviewer_profile_id=reviewer.profile_id,
        rubric_id=rubric.rubric_id,
        rubric_sha256=rubric.record_sha256,
        scores=tuple(item[0] for item in completed),
        reviews=tuple(item[1] for item in completed),
    )


def _build_study(
    seed: int,
    assignment: AssignmentRevisionV1,
    rubric: RubricRevisionV1,
    *,
    retention_policy: StudyRetentionPolicyV1 | None = None,
    data_export_policy: StudyDataExportPolicyV1 | None = None,
) -> StudyRevisionV1:
    selected_retention_policy = (
        StudyRetentionPolicyV1(
            policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
            retention_until_utc=None,
            retain_after_profile_deletion=False,
        )
        if retention_policy is None
        else retention_policy
    )
    selected_data_export_policy = (
        StudyDataExportPolicyV1(
            permission=EvidenceExportPermissionV1.DENIED,
            redaction_policy_sha256=None,
        )
        if data_export_policy is None
        else data_export_policy
    )
    if type(selected_retention_policy) is not StudyRetentionPolicyV1:
        raise TypeError("demo study retention policy is invalid")
    if type(selected_data_export_policy) is not StudyDataExportPolicyV1:
        raise TypeError("demo study data-export policy is invalid")
    manifest = StudyManifestV1(
        question="Can the complete local instructor workflow be exercised reproducibly?",
        hypothesis="Six synthetic attempts can be isolated, reviewed, and summarized.",
        assignment_set=(
            StudyAssignmentBindingV1(
                assignment_id=assignment.assignment_id,
                assignment_sha256=assignment.sha256,
            ),
        ),
        study_status=StudyStatusV1.EXPLORATORY,
        preregistration_sha256=_demo_digest(seed, "study-preregistration"),
        preregistered_at_utc="2026-01-01T00:05:00Z",
        population="Two synthetic pseudonymous learner profiles in one offline fixture.",
        design=StudyDesignV1(
            capability=DesignCapabilityV1.DESCRIPTIVE,
            design_kind=StudyDesignKindV1.OBSERVATIONAL,
            intervention=None,
            comparator=None,
            causal_estimand=None,
            randomization_evidence_sha256=None,
            identifying_assumptions=(),
            confounding_adjustment_sha256=None,
        ),
        allocation_randomization=AllocationRandomizationV1(
            method=AllocationMethodV1.OBSERVED_NO_ALLOCATION,
            allocation_unit="pseudonymous learner profile",
            arm_ids=("all_fixture_profiles",),
            allocation_ratio=(),
            allocation_policy_sha256=_demo_digest(seed, "allocation-policy"),
            randomization_sha256=None,
        ),
        blinding_reveal=BlindingRevealV1(
            participants_blinded=False,
            instructors_blinded=False,
            outcome_assessors_blinded=False,
            analysts_blinded=False,
            reveal_policy_id="INSTRUCTOR_DEMO_REVIEW_THEN_REVEAL_V1",
            reveal_policy_sha256=_demo_digest(seed, "reveal-policy"),
            reveal_timing="After each immutable review is complete.",
        ),
        content_locks=(
            ContentLockV1(
                lock_name="assignment_revision",
                content_id=assignment.assignment_id,
                content_sha256=assignment.sha256,
            ),
            ContentLockV1(
                lock_name="rubric_revision",
                content_id=rubric.rubric_id,
                content_sha256=rubric.sha256,
            ),
        ),
        parameter_locks=(
            ParameterLockV1(
                parameter_path="hidden_liquidity_policy",
                value_sha256=assignment.spec.locks.liquidity_sha256,
            ),
            ParameterLockV1(
                parameter_path="seed_policy",
                value_sha256=assignment.spec.locks.seed_policy.sha256,
            ),
        ),
        declared_metrics=(
            MetricDeclarationV1(
                metric_id=INSTRUCTOR_DEMO_METRIC_ID,
                metric_version="1",
                definition_sha256=_demo_digest(seed, "review-score-metric"),
                unit="points",
            ),
        ),
        primary_outcomes=(
            OutcomeDeclarationV1(
                outcome_id="workflow_completion_score",
                metric_id=INSTRUCTOR_DEMO_METRIC_ID,
                estimand="Descriptive mean synthetic rubric score.",
                analysis_population="All six fixture attempts.",
                time_window="At completed instructor review.",
            ),
        ),
        secondary_outcomes=(),
        planned_sample_size=6,
        sample_rationale="Exactly three attempts for each of two fixture profiles.",
        stopping_rule="Stop after all six preregistered synthetic attempts are reviewed.",
        missing_data_policy="Retain every eligible attempt and report missingness explicitly.",
        multiplicity_policy="One descriptive metric; no inferential multiplicity claim.",
        inclusion_criteria=("Attempt belongs to one of the two exact fixture profiles.",),
        exclusion_criteria=("Attempt differs from the locked assignment revision.",),
        analysis_plan=AnalysisPlanV1(
            version="1",
            plan_sha256=_demo_digest(seed, "analysis-plan"),
            code_sha256=_demo_digest(seed, "analysis-code"),
            capability=DesignCapabilityV1.DESCRIPTIVE,
        ),
        seed_policy=assignment.spec.locks.seed_policy,
        software_version="kirby2-wo37d-instructor-demo-v1",
        consent_policy=StudyConsentPolicyV1(
            authorization_policy_id="KIRBY2_LOCAL_INSTRUCTOR_DEMO_CONSENT_V1",
            required_scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
            require_current_grant_at_inclusion=True,
        ),
        retention_policy=selected_retention_policy,
        data_export_policy=selected_data_export_policy,
    )
    return create_study(manifest)


def _build_study_ledger(
    study: StudyRevisionV1,
    assignment: AssignmentRevisionV1,
    attempts: tuple[AssignmentAttemptManifestV1, ...],
) -> StudyExecutionLedgerV1:
    ledger = create_study_execution_ledger(
        study,
        locked_at_utc="2026-01-01T00:10:00Z",
    )
    for attempt in attempts:
        ledger = include_study_attempt(
            ledger,
            assignment_id=assignment.assignment_id,
            assignment_sha256=assignment.sha256,
            attempt_id=attempt.attempt_id,
            attempt_sha256=attempt.sha256,
            observed_at_utc=attempt.recorded_at_utc,
            included_at_utc=attempt.recorded_at_utc,
        )
    return ledger


def _build_cohort_summary(
    seed: int,
    learners: tuple[LearnerProfile, ...],
    assignment: AssignmentRevisionV1,
    attempts: tuple[AssignmentAttemptManifestV1, ...],
    rubric: RubricRevisionV1,
    study: StudyRevisionV1,
    study_ledger: StudyExecutionLedgerV1,
) -> tuple[CohortRevisionV1, CohortSummaryV1]:
    cohort = create_cohort(
        CohortDefinitionV1(
            study_id=study.study_id,
            study_sha256=study.sha256,
            protocol_lock_sha256=study_ledger.protocol_lock.sha256,
            population=study.manifest.population,
            inclusion_criteria=tuple(sorted(study.manifest.inclusion_criteria)),
            exclusion_criteria=tuple(sorted(study.manifest.exclusion_criteria)),
            member_profile_ids=tuple(item.profile_id for item in learners),
            membership_policy=None,
            assignment_bindings=(
                CohortAssignmentBindingV1(
                    assignment_id=assignment.assignment_id,
                    assignment_sha256=assignment.sha256,
                ),
            ),
            metric_ids=(INSTRUCTOR_DEMO_METRIC_ID,),
        )
    )
    signature = VersionSignatureV1(
        score_version=1,
        score_sha256=rubric.content.sha256,
        model_version=1,
        model_sha256=_demo_digest(seed, "descriptive-model"),
        analysis_version=1,
        analysis_sha256=study.manifest.analysis_plan.plan_sha256,
    )
    ordered_attempts = tuple(sorted(attempts, key=lambda item: item.attempt_id))
    observations = tuple(
        MetricObservationV1(
            metric_id=INSTRUCTOR_DEMO_METRIC_ID,
            observation_id=attempt.attempt_id,
            version_signature=signature,
            present=True,
            value=8,
            scale=1,
            denominator=1,
        )
        for attempt in ordered_attempts
    )
    sources = tuple(
        CohortSourceAttemptV1(
            learner_profile_id=attempt.learner_profile_id,
            assignment_id=assignment.assignment_id,
            assignment_sha256=assignment.sha256,
            attempt_id=attempt.attempt_id,
            attempt_sha256=attempt.sha256,
        )
        for attempt in ordered_attempts
    )
    summary = build_cohort_summary(
        cohort,
        INSTRUCTOR_DEMO_METRIC_ID,
        observations,
        sources,
        compatibility_action=CompatibilityActionV1.REFUSE,
        requested_capability=AnalysisCapabilityV1.DESCRIPTIVE,
        analysis_capability=AnalysisCapabilityV1.DESCRIPTIVE,
        design_capability=study.manifest,
    )
    return cohort, summary


def _console_source(
    source_kind: str,
    source_id: str,
    source_bytes: bytes,
) -> ConsoleSourceIdentityV1:
    return create_console_source_identity(
        source_kind=source_kind,
        source_id=source_id,
        source_bytes=source_bytes,
    )


def _console_reference(
    *,
    artifact_kind: ConsoleArtifactKindV1,
    artifact_id: str,
    artifact_bytes: bytes,
    source_identities: tuple[ConsoleSourceIdentityV1, ...],
    content_version: str,
    scoring_version: str = NOT_APPLICABLE_VERSION,
    model_version: str = NOT_APPLICABLE_VERSION,
    analysis_version: str = NOT_APPLICABLE_VERSION,
    sample_count: int = 0,
    uncertainty_sha256: str | None = None,
    capability: ConsoleCapabilityV1 = ConsoleCapabilityV1.NOT_APPLICABLE,
    consent_eligible: bool = True,
) -> ConsoleArtifactReferenceV1:
    return create_console_artifact_reference(
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_bytes=artifact_bytes,
        source_identities=source_identities,
        content_version=content_version,
        scoring_version=scoring_version,
        model_version=model_version,
        analysis_version=analysis_version,
        sample_count=sample_count,
        uncertainty_sha256=uncertainty_sha256,
        capability=capability,
        consent_eligible=consent_eligible,
        export_eligible=False,
    )


def _build_console_and_comparison(
    seed: int,
    reviewer: InstructorProfile,
    learners: tuple[LearnerProfile, ...],
    assignment: AssignmentRevisionV1,
    attempts: tuple[AssignmentAttemptManifestV1, ...],
    rubric: RubricRevisionV1,
    review_bundle: InstructorDemoReviewBundleV1,
    study: StudyRevisionV1,
    cohort: CohortRevisionV1,
    cohort_summary: CohortSummaryV1,
) -> tuple[InstructorConsoleLedgerV1, ComparisonViewV1]:
    """Index exact artifacts, query one descriptive view, then index that view."""

    ledger = create_console_ledger(
        ledger_id="instructor-demo-console-" + _demo_digest(seed, "console-ledger")[:32]
    )
    profile_sources: dict[str, ConsoleSourceIdentityV1] = {}
    indexed_profiles = (("INSTRUCTOR_PROFILE", reviewer),) + tuple(
        ("LEARNER_PROFILE", item) for item in learners
    )
    for profile_kind, profile in indexed_profiles:
        profile_bytes = profile.canonical_bytes()
        source = _console_source(profile_kind, profile.profile_id, profile_bytes)
        profile_sources[profile.profile_id] = source
        ledger = record_profile(
            ledger,
            _console_reference(
                artifact_kind=ConsoleArtifactKindV1.PROFILE,
                artifact_id=profile.profile_id,
                artifact_bytes=profile_bytes,
                source_identities=(source,),
                content_version="1",
            ),
        )

    rubric_bytes = rubric.canonical_bytes()
    rubric_source = _console_source("RUBRIC_REVISION", rubric.rubric_id, rubric_bytes)
    ledger = record_rubric(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.RUBRIC,
            artifact_id=rubric.rubric_id,
            artifact_bytes=rubric_bytes,
            source_identities=(rubric_source,),
            content_version=str(rubric.revision),
            scoring_version=str(rubric.scoring_version),
        ),
    )

    assignment_bytes = assignment.canonical_bytes()
    assignment_source = _console_source(
        "ASSIGNMENT_REVISION",
        assignment.assignment_id,
        assignment_bytes,
    )
    ledger = record_assignment(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.ASSIGNMENT,
            artifact_id=assignment.assignment_id,
            artifact_bytes=assignment_bytes,
            source_identities=(assignment_source, rubric_source),
            content_version=str(assignment.assignment.revision),
            scoring_version=assignment.spec.scoring_version,
        ),
    )

    uncertainty_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {
                "cohort_summary_sha256": cohort_summary.sha256,
                "uncertainty": [
                    None if item is None else item.as_dict()
                    for item in cohort_summary.uncertainty
                ],
            }
        )
    ).hexdigest()
    cohort_summary_source = _console_source(
        "COHORT_SUMMARY",
        cohort.cohort_id,
        cohort_summary.canonical_bytes(),
    )
    attempt_sources: dict[str, ConsoleSourceIdentityV1] = {}
    attempt_references: dict[str, ConsoleArtifactReferenceV1] = {}
    for attempt in sorted(attempts, key=lambda item: item.attempt_id):
        attempt_bytes = attempt.canonical_bytes()
        attempt_source = _console_source(
            "ASSIGNMENT_ATTEMPT",
            attempt.attempt_id,
            attempt_bytes,
        )
        attempt_sources[attempt.attempt_id] = attempt_source
        reference = _console_reference(
            artifact_kind=ConsoleArtifactKindV1.ASSIGNMENT_ATTEMPT,
            artifact_id=attempt.attempt_id,
            artifact_bytes=attempt_bytes,
            source_identities=(
                assignment_source,
                attempt_source,
                cohort_summary_source,
                profile_sources[attempt.learner_profile_id],
            ),
            content_version=str(attempt.attempt_revision.revision),
            scoring_version=assignment.spec.scoring_version,
            model_version="1",
            analysis_version=study.manifest.analysis_plan.version,
            sample_count=1,
            uncertainty_sha256=uncertainty_sha256,
            capability=ConsoleCapabilityV1.DESCRIPTIVE,
        )
        attempt_references[attempt.attempt_id] = reference
        ledger = record_attempt(ledger, reference)

    review_bytes = review_bundle.canonical_bytes()
    review_source = _console_source(
        "REVIEW_BUNDLE",
        review_bundle.review_bundle_id,
        review_bytes,
    )
    ledger = record_review(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.REVIEW,
            artifact_id=review_bundle.review_bundle_id,
            artifact_bytes=review_bytes,
            source_identities=(
                review_source,
                rubric_source,
                cohort_summary_source,
                profile_sources[reviewer.profile_id],
                *attempt_sources.values(),
            ),
            content_version="1",
            scoring_version=str(rubric.scoring_version),
            model_version="1",
            analysis_version=study.manifest.analysis_plan.version,
            sample_count=len(review_bundle.reviews),
            uncertainty_sha256=uncertainty_sha256,
            capability=ConsoleCapabilityV1.DESCRIPTIVE,
        ),
    )

    study_bytes = study.canonical_bytes()
    study_source = _console_source("STUDY_REVISION", study.study_id, study_bytes)
    ledger = record_study(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.STUDY,
            artifact_id=study.study_id,
            artifact_bytes=study_bytes,
            source_identities=(study_source, assignment_source, rubric_source),
            content_version=str(study.study.revision),
            scoring_version=assignment.spec.scoring_version,
            model_version="1",
            analysis_version=study.manifest.analysis_plan.version,
            sample_count=0,
            capability=ConsoleCapabilityV1.DESCRIPTIVE,
        ),
    )

    cohort_bytes = cohort.canonical_bytes()
    cohort_source = _console_source("COHORT_REVISION", cohort.cohort_id, cohort_bytes)
    ledger = record_cohort(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.COHORT,
            artifact_id=cohort.cohort_id,
            artifact_bytes=cohort_bytes,
            source_identities=(
                cohort_source,
                cohort_summary_source,
                study_source,
                *attempt_sources.values(),
            ),
            content_version=str(cohort.revision),
            scoring_version=assignment.spec.scoring_version,
            model_version="1",
            analysis_version=study.manifest.analysis_plan.version,
            sample_count=cohort_summary.eligible_denominator,
            uncertainty_sha256=uncertainty_sha256,
            capability=ConsoleCapabilityV1.DESCRIPTIVE,
        ),
    )

    attempts_by_id = {item.attempt_id: item for item in attempts}
    sources = tuple(
        sorted(
            (
                ComparisonSourceV1(
                    reference=reference,
                    learner_profile_id=attempts_by_id[attempt_id].learner_profile_id,
                    lesson_id=attempts_by_id[attempt_id].selected_lesson.lesson_id,
                    skill_id="skill.hidden-liquidity",
                    scenario_id=(
                        attempts_by_id[attempt_id].selected_scenario_variation
                    ),
                    hotkey_layout_id=assignment.spec.hotkey_layout.layout_name,
                    session_id=attempts_by_id[attempt_id].run_id,
                    strategy_id="strategy.hidden-liquidity-manual-v1",
                    volume_regime_id=(
                        attempts_by_id[attempt_id].selected_scenario_variation
                    ),
                    execution_mode=ComparisonExecutionModeV1.MANUAL,
                )
                for attempt_id, reference in attempt_references.items()
            ),
            key=lambda item: (
                item.attempt_id,
                item.attempt_sha256,
                item.source_sha256,
            ),
        )
    )
    comparison = build_comparison_view(
        ledger,
        view_kind=ComparisonViewKindV1.SAME_LESSON_ACROSS_LEARNERS,
        scope=InstructorQueryScopeV1(
            scope_kind=InstructorQueryScopeKindV1.COHORT_RESEARCH,
            principal_profile_id=reviewer.profile_id,
            cohort_id=cohort.cohort_id,
            study_id=study.study_id,
        ),
        sources=sources,
        as_of=ledger.head_sequence,
    )
    comparison_bytes = comparison.canonical_bytes()
    comparison_source = _console_source(
        "COMPARISON_VIEW",
        comparison.comparison_id,
        comparison_bytes,
    )
    ledger = record_comparison(
        ledger,
        _console_reference(
            artifact_kind=ConsoleArtifactKindV1.COMPARISON,
            artifact_id=comparison.comparison_id,
            artifact_bytes=comparison_bytes,
            source_identities=(
                comparison_source,
                cohort_source,
                cohort_summary_source,
                study_source,
                *attempt_sources.values(),
            ),
            content_version="1",
            scoring_version=assignment.spec.scoring_version,
            model_version="1",
            analysis_version=study.manifest.analysis_plan.version,
            sample_count=comparison.sample_count,
            uncertainty_sha256=uncertainty_sha256,
            capability=ConsoleCapabilityV1.DESCRIPTIVE,
        ),
    )
    return ledger, comparison


def build_instructor_demo(
    seed: int = 42,
    *,
    study_retention_policy: StudyRetentionPolicyV1 | None = None,
    study_data_export_policy: StudyDataExportPolicyV1 | None = None,
) -> InstructorDemoV1:
    """Build the exact deterministic offline WO37-D demonstration artifact."""

    selected_seed = _seed(seed)
    reviewer = create_instructor_profile(_demo_entropy(selected_seed, "reviewer"))
    learners = tuple(
        sorted(
            (
                create_learner_profile(_demo_entropy(selected_seed, f"learner:{index}"))
                for index in range(1, INSTRUCTOR_DEMO_LEARNER_COUNT + 1)
            ),
            key=lambda item: item.profile_id,
        )
    )
    consent_pairs = tuple(
        _build_consent(selected_seed, learner, index)
        for index, learner in enumerate(learners, start=1)
    )
    consents = tuple(item[0] for item in consent_pairs)
    consent_evidence = {
        learner.profile_id: pair[1]
        for learner, pair in zip(learners, consent_pairs, strict=True)
    }
    rubric = _build_rubric()
    assignment = _build_assignment(selected_seed, rubric)
    attempts = _build_attempts(assignment, learners, consent_evidence)
    review_bundle = _build_review_bundle(
        selected_seed,
        reviewer,
        rubric,
        attempts,
    )
    study = _build_study(
        selected_seed,
        assignment,
        rubric,
        retention_policy=study_retention_policy,
        data_export_policy=study_data_export_policy,
    )
    study_ledger = _build_study_ledger(study, assignment, attempts)
    cohort, cohort_summary = _build_cohort_summary(
        selected_seed,
        learners,
        assignment,
        attempts,
        rubric,
        study,
        study_ledger,
    )
    console_ledger, cohort_comparison = _build_console_and_comparison(
        selected_seed,
        reviewer,
        learners,
        assignment,
        attempts,
        rubric,
        review_bundle,
        study,
        cohort,
        cohort_summary,
    )
    return InstructorDemoV1(
        seed=selected_seed,
        reviewer_profile=reviewer,
        learner_profiles=learners,
        consents=consents,
        assignment=assignment,
        attempts=attempts,
        rubric=rubric,
        review_bundle=review_bundle,
        study_revision=study,
        study_ledger=study_ledger,
        cohort=cohort,
        cohort_summary=cohort_summary,
        cohort_comparison=cohort_comparison,
        console_ledger=console_ledger,
    )


def _selected_privacy_export_records(
    demo: InstructorDemoV1,
    fixture: PrivacyExportFixtureV1,
) -> tuple[
    LearnerProfile,
    AssignmentAttemptManifestV1,
    RubricScoreSidecarV1,
    ReviewRevisionV1,
]:
    learner = demo.learner_profiles[fixture.learner_ordinal - 1]
    matches = tuple(
        item
        for item in demo.attempts
        if item.learner_profile_id == learner.profile_id
        and item.attempt_number == fixture.attempt_number
    )
    if len(matches) != 1:
        raise RuntimeError("privacy-export fixture did not select one exact attempt")
    attempt = matches[0]
    scores = tuple(
        item
        for item in demo.review_bundle.scores
        if item.assignment_attempt_id == attempt.attempt_id
    )
    reviews = tuple(
        item
        for item in demo.review_bundle.reviews
        if item.attempt_id == attempt.attempt_id
    )
    if len(scores) != 1 or len(reviews) != 1:
        raise RuntimeError("privacy-export attempt lacks one exact score and review")
    return learner, attempt, scores[0], reviews[0]


def _privacy_export_profile_entropy(
    seed: int,
    learner: LearnerProfile,
) -> bytes:
    for ordinal in range(1, INSTRUCTOR_DEMO_LEARNER_COUNT + 1):
        entropy = _demo_entropy(seed, f"learner:{ordinal}")
        if create_learner_profile(entropy).profile_id == learner.profile_id:
            return entropy
    raise RuntimeError("selected demo learner has no deterministic local entropy")


def _privacy_export_omissions(
    fixture: PrivacyExportFixtureV1,
) -> tuple[ExportOmissionV1, ...]:
    specifications = {
        "direct_identity": (
            ExportOmissionReasonV1.PROHIBITED_DIRECT_IDENTITY,
            "Direct identity is separately erasable local data and is never portable evidence.",
        ),
        "identity_mapping": (
            ExportOmissionReasonV1.PROHIBITED_IDENTITY_MAPPING,
            "The local pseudonym-to-identity mapping is categorically excluded.",
        ),
        "local_paths": (
            ExportOmissionReasonV1.PROHIBITED_LOCAL_PATH,
            "Machine-local filesystem paths are not portable evidence.",
        ),
        "secrets": (
            ExportOmissionReasonV1.PROHIBITED_SECRET,
            "Secrets and credentials are categorically excluded.",
        ),
        "unauthorized_hidden_or_reveal_data": (
            ExportOmissionReasonV1.PROHIBITED_HIDDEN_REVEAL_DATA,
            "Hidden or reveal data outside an explicit selected-trace authorization is excluded.",
        ),
        "unselected_causal_traces": (
            ExportOmissionReasonV1.NOT_SELECTED,
            "Only the causal trace bound to the selected attempt is exported.",
        ),
    }
    if tuple(sorted(specifications)) != fixture.omitted_categories:
        raise ValueError("privacy-export omission inventory differs from the fixture")
    return tuple(
        sorted(
            (
                ExportOmissionV1(
                    item_kind=category,
                    item_id=f"omitted.{category}",
                    reason=specifications[category][0],
                    detail=specifications[category][1],
                )
                for category in fixture.omitted_categories
            ),
            key=lambda item: item.canonical_bytes(),
        )
    )


def run_instructor_export_demo(
    fixture: PrivacyExportFixtureV1,
    *,
    seed: int = 42,
) -> dict[str, object]:
    """Exercise the complete local WO37-E export/import/deletion workflow."""

    if type(fixture) is not PrivacyExportFixtureV1:
        raise TypeError("instructor export demo requires PrivacyExportFixtureV1")
    selected_seed = _seed(seed)
    redaction_policy = create_portable_evidence_redaction_policy(
        policy_id=fixture.redaction_policy_id,
        policy_version=fixture.redaction_policy_version,
        allowlisted_paths=fixture.allowlisted_paths,
        authorized_hidden_paths=fixture.authorized_hidden_paths,
    )
    demo = build_instructor_demo(
        selected_seed,
        study_retention_policy=StudyRetentionPolicyV1(
            policy=fixture.retention_policy,
            retention_until_utc=None,
            retain_after_profile_deletion=fixture.retain_after_profile_deletion,
        ),
        study_data_export_policy=StudyDataExportPolicyV1(
            permission=fixture.export_permission,
            redaction_policy_sha256=redaction_policy.sha256,
        ),
    )
    learner, attempt, score, review = _selected_privacy_export_records(
        demo,
        fixture,
    )
    base_consents = tuple(
        item
        for item in demo.consents
        if item.pseudonymous_profile_id == learner.profile_id
    )
    if len(base_consents) != 1:
        raise RuntimeError("selected learner does not have one exact consent head")
    consent = revise_consent_record(
        base_consents[0],
        scopes=fixture.scopes,
        recorded_at_utc=fixture.recorded_at_utc,
        retention_policy=fixture.retention_policy,
        retention_until_utc=None,
        retain_pseudonymous_evidence_after_profile_deletion=(
            fixture.retain_after_profile_deletion
        ),
        export_permission=fixture.export_permission,
        withdrawal_policy=fixture.withdrawal_policy,
    )
    trace_source_bytes = _canonical_json_bytes(
        {
            "domain": "KIRBY2_INSTRUCTOR_DEMO_DERIVATION_V1",
            "label": f"trace:{fixture.selected_causal_trace_ordinal}",
            "seed": selected_seed,
        }
    )
    selected_trace = create_selected_causal_trace(
        review.sidecar.attempt.causal_trace_id,
        trace_source_bytes,
    )
    if selected_trace.trace_sha256 != review.sidecar.attempt.causal_trace_sha256:
        raise RuntimeError("selected causal-trace bytes differ from the review binding")
    versions = dict(fixture.compatibility_versions)
    compatibility = VersionSignatureV1(
        score_version=versions["rubric_score"],
        score_sha256=score.sha256,
        model_version=versions["attempt_manifest"],
        model_sha256=attempt.sha256,
        analysis_version=versions["review"],
        analysis_sha256=review.sha256,
    )
    bundle = build_export_bundle(
        current_consent=consent,
        required_scope=fixture.required_scope,
        decision_time_utc=fixture.decision_time_utc,
        assignment=demo.assignment,
        attempt_manifest=attempt,
        study_revision=demo.study_revision,
        study_ledger=demo.study_ledger,
        scores=(score,),
        reviews=(review,),
        selected_causal_traces=(selected_trace,),
        compatibility_versions=(compatibility,),
        software_version=fixture.software_version,
        limitations=fixture.limitations,
        redaction_policy=redaction_policy,
        omissions=_privacy_export_omissions(fixture),
    )

    with tempfile.TemporaryDirectory(prefix="kirby2-instructor-export-") as raw_root:
        root = Path(raw_root).resolve()
        source_paths = DataPaths((root / "source").resolve())
        clean_import_paths = DataPaths((root / "clean-import").resolve())
        create_identity_mapping(
            source_paths,
            learner,
            DirectIdentityV1(
                display_name="Local synthetic demo learner",
                direct_identifiers=(
                    DirectIdentifierV1(
                        identifier_kind="demo.local_id",
                        identifier_value=f"offline-fixture-{selected_seed}",
                    ),
                ),
            ),
            opaque_entropy=_privacy_export_profile_entropy(selected_seed, learner),
        )
        source_paths.ensure((DataAreaId.EVIDENCE,))
        destination = (source_paths.evidence / fixture.directory_name).resolve()
        written_directory = write_export_directory(bundle, destination)
        verified_before_deletion = verify_export_directory(written_directory)
        if verified_before_deletion.files != bundle.files:
            raise RuntimeError("written export bytes differ from the built bundle")
        imported = import_export_directory(
            written_directory,
            paths=clean_import_paths,
        )
        retained_reference = create_retained_evidence_reference(
            evidence_kind=RetainedEvidenceKindV1.EVIDENCE,
            evidence_id=bundle.export_id,
            evidence_sha256=bundle.manifest_sha256,
            pseudonymous_profile_id=learner.profile_id,
        )
        deletion_plan = plan_profile_deletion(
            consent,
            required_scope=fixture.required_scope,
            requested_pseudonymous_evidence_retention=True,
            retained_evidence_references=(retained_reference,),
            decision_time_utc=fixture.decision_time_utc,
            planned_deletion_time_utc=fixture.deletion_time_utc,
        )
        deletion_result = execute_profile_deletion(
            source_paths,
            consent,
            deletion_plan,
            deletion_time_utc=fixture.deletion_time_utc,
        )
        verified_after_deletion = verify_export_directory(written_directory)
        retained_export_unchanged = (
            verified_after_deletion.files == verified_before_deletion.files
            and verified_after_deletion.manifest_sha256
            == verified_before_deletion.manifest_sha256
            and verified_after_deletion.inventory_sha256
            == verified_before_deletion.inventory_sha256
        )
        if not retained_export_unchanged:
            raise RuntimeError("profile deletion mutated retained export evidence")
        return {
            "consent_decision": bundle.consent_decision.as_dict(),
            "deletion_result": deletion_result.as_dict(),
            "export_manifest": bundle.manifest.as_dict(),
            "import_receipt": imported.as_dict(),
            "inventory": bundle.inventory.as_dict(),
            "redaction_manifest": bundle.redaction_manifest.as_dict(),
            "retained_export_unchanged": retained_export_unchanged,
            "schema_id": INSTRUCTOR_EXPORT_DEMO_SCHEMA_ID,
            "schema_version": INSTRUCTOR_EXPORT_DEMO_SCHEMA_VERSION,
            "seed": selected_seed,
        }


def _configure_instructor_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seed",
        type=_seed_argument,
        default=42,
        help="explicit deterministic fixture seed (default: 42)",
    )


def _handle_instructor_demo(args: argparse.Namespace) -> int:
    print(build_instructor_demo(args.seed).canonical_bytes().decode("ascii"))
    return 0


def _configure_instructor_export_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="strict local WO37-E privacy/export TOML fixture",
    )
    parser.add_argument(
        "--seed",
        type=_seed_argument,
        default=42,
        help="explicit deterministic fixture seed (default: 42)",
    )


def _handle_instructor_export_demo(args: argparse.Namespace) -> int:
    fixture = load_privacy_export_fixture(args.fixture)
    result = run_instructor_export_demo(fixture, seed=args.seed)
    print(_canonical_json_bytes(result).decode("ascii"))
    return 0


INSTRUCTOR_CONSOLE_COMMAND_MODULE = CommandModule(
    module_id="INSTRUCTOR_RESEARCH_CONSOLE",
    commands=(
        CommandSpec(
            command_id="INSTRUCTOR_DEMO",
            name="instructor-demo",
            help="build the exact offline pseudonymous instructor workflow fixture",
            handler=_handle_instructor_demo,
            configure=_configure_instructor_demo,
        ),
        CommandSpec(
            command_id="INSTRUCTOR_EXPORT_DEMO",
            name="instructor-export-demo",
            help="exercise authorized redacted export, clean import, and profile deletion",
            handler=_handle_instructor_export_demo,
            configure=_configure_instructor_export_demo,
        ),
    ),
)


__all__ = [
    "INSTRUCTOR_CONSOLE_COMMAND_MODULE",
    "INSTRUCTOR_DEMO_ATTEMPT_COUNT",
    "INSTRUCTOR_DEMO_ATTEMPTS_PER_LEARNER",
    "INSTRUCTOR_DEMO_CLAIM_SCOPE",
    "INSTRUCTOR_DEMO_EXTERNAL_SERVICE_POLICY",
    "INSTRUCTOR_DEMO_LEARNER_COUNT",
    "INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_ID",
    "INSTRUCTOR_DEMO_REVIEW_BUNDLE_SCHEMA_VERSION",
    "INSTRUCTOR_DEMO_SCHEMA_ID",
    "INSTRUCTOR_DEMO_SCHEMA_VERSION",
    "INSTRUCTOR_EXPORT_DEMO_SCHEMA_ID",
    "INSTRUCTOR_EXPORT_DEMO_SCHEMA_VERSION",
    "PRIVACY_EXPORT_COMPATIBILITY_COMPONENTS_V1",
    "PRIVACY_EXPORT_FIXTURE_SCHEMA_ID",
    "PRIVACY_EXPORT_FIXTURE_SCHEMA_VERSION",
    "PRIVACY_EXPORT_OMITTED_CATEGORIES_V1",
    "InstructorDemoReviewBundleV1",
    "InstructorDemoV1",
    "PrivacyExportFixtureV1",
    "build_instructor_demo",
    "load_privacy_export_fixture",
    "run_instructor_export_demo",
]
