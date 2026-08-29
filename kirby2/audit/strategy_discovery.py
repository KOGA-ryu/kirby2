"""Executable strategy-discovery audits for Work Orders 35-A through 35-E."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.immutable import thaw_json
from kirby2.discovery.ast import (
    StrategyAstV1,
    parse_strategy_ast,
    render_canonical_strategy_ast,
    strategy_ast_round_trip,
)
from kirby2.discovery.identity import (
    STRATEGY_CANONICALIZATION_POLICY_SHA256_V1,
    STRATEGY_IDENTITY_MIGRATION_ID_V1,
    StrategyImportOriginV1,
    canonical_identity_bytes,
    canonical_render_identity,
    strategy_semantic_sha256,
)
from kirby2.discovery.lineage import (
    StrategyRngSubstreamV1,
    build_strategy_lineage_node,
    semantic_strategy_diff,
)
from kirby2.discovery.diffs import (
    STRATEGY_COMPLEXITY_SCHEMA_ID_V1,
    StrategyComplexityV1,
    StrategyComplexityDeltaV1,
    strategy_complexity,
)
from kirby2.discovery.generation import (
    STRATEGY_MUTATION_GENERATION_ORDER_V1,
    STRATEGY_MUTATION_SUBSTREAM_LABEL_V1,
    MutationGenerationContextV1,
    generate_mutation_batch,
    labeled_substream_uint64,
)
from kirby2.discovery.mutations import (
    REQUIRED_MUTATION_OPERATORS_V1,
    MutationOperationIdV1,
    MutationRejectionReasonV1,
    MutationRequestV1,
    MutationResourceLimitsV1,
    MutationStatusV1,
    StrategyMutationResultV1,
    apply_strategy_mutation,
)
from kirby2.discovery.access import (
    PartitionAccessDecisionV1,
    PartitionAccessPurposeV1,
    PartitionAccessReasonV1,
    request_partition_access,
)
from kirby2.discovery.experiment import (
    ExperimentPhaseV1,
    StrategyDiscoveryExperimentV1,
    TerminalEvaluationOutcomeV1,
    close_terminal_evaluation,
    freeze_candidate_set,
    start_strategy_experiment,
    start_successor_experiment,
)
from kirby2.discovery.partitions import (
    NOT_APPLICABLE_V1,
    HistoricalPeriodV1,
    PartitionManifestV1,
    PartitionMemberV1,
    StrategyPartitionV1,
    ValidationScheduleV1,
    partition_manifest_round_trip,
)
from kirby2.discovery.evaluation import (
    CandidatePartitionEvidenceV1,
    ComponentDeltaV1,
    DevelopmentSyntheticScoreOracleV1,
    EvaluationAccessError,
    RootDeltaV1,
    SyntheticOracleModeV1,
    require_compatible_evidence,
)
from kirby2.discovery.objectives import (
    ALL_OBJECTIVE_SPECS_V1,
    EvidenceCompatibilityKeyV1,
    ObjectiveApplicabilityV1,
    ObjectiveValueV1,
    REQUIRED_OBJECTIVE_SPECS_V1,
    StrategyObjectiveIdV1,
    balanced_classification_utility,
    common_tie_digest,
    completion_utility,
    complexity_points,
    cross_cell_stability_utility,
    discipline_compatibility_utility,
    execution_opportunity_utility,
    false_green_utility,
    materially_equivalent,
    median_and_mad,
    missed_opportunity_utility,
    multiplicity_penalty,
    objective_protocol_projection,
    root_composite,
    signed_cost_utility,
    turnover_utility,
)
from kirby2.discovery.search import (
    CONTROLLED_BASE_SOURCE_SHA256_V1,
    STRATEGY_VECTOR_ORDER_V1,
    SearchOutcomeV1,
    SearchPolicyV1,
    StrategySearchManifestV1,
    load_search_manifest,
    run_development_search,
)
from kirby2.discovery.observability import (
    ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1,
    FORBIDDEN_REFERENCE_FIELDS_V1,
    OBSERVABLE_FEATURE_NAMES_V1,
    TERMINAL_ROOT_ORDER_V1,
    CandidateDecisionProjectionV1,
    CandidatePermissionV1,
    CandidateSignalV1,
    DisciplineEligibilityV1,
    DisciplineEvidenceStatusV1,
    DisciplineReasonV1,
    EndogenousDivergenceRecordV1,
    MissingReferenceLabelError,
    ObservableDecisionInputV1,
    ObservationStatusV1,
    ObservationUnavailableError,
    RevealProtocolError,
    RevealStageV1,
    ScientificConclusionV1,
    TerminalRevealControllerV1,
    bind_reference_decision_label,
    project_candidate_decision,
    scientific_conclusion,
    score_candidate_decision,
    seal_terminal_material,
    summarize_discipline,
)
from kirby2.discovery.overfit import (
    POST_REVEAL_ADDITIONS_V1,
    PRE_REVEAL_APPLICABILITY_V1,
    PRE_REVEAL_SEALED_V1,
    OverfitCellV1,
    OverfitLabelV1,
    OverfitPartitionEvidenceV1,
    ThresholdSensitivityEvidenceV1,
    ThresholdSettingMedianV1,
    assess_post_reveal_overfit,
    build_development_overfit_fixture,
    excessive_trade_frequency,
    one_scenario_dependence,
    one_seed_dependence,
    threshold_evidence_from_robustness,
    threshold_sensitivity,
    trade_suppression,
)
from kirby2.discovery.robustness import (
    MANDATORY_ROBUSTNESS_FAMILIES_V1,
    ROBUSTNESS_EXPECTED_CELL_COUNT_V1,
    ROBUSTNESS_ROOTS_V1,
    ROBUSTNESS_SETTINGS_V1,
    PerturbationStatusV1,
    RegimeProbabilityRowV1,
    RegimeWeightV1,
    RobustnessFamilyV1,
    RobustnessOutcomeV1,
    SyntheticRobustnessModeV1,
    apply_robustness_setting,
    build_robustness_probes,
    build_synthetic_robustness_evidence,
    controlled_robustness_environment,
    derive_execution_timing,
    qualify_adversarial,
    qualify_holdout,
    qualify_robustness,
)
from kirby2.experiments.models import (
    ExperimentManifest,
    ExperimentMode,
    StrategyVariant,
)
from kirby2.research.models import ArtifactType
from kirby2.research.store import RunStore
from kirby2.strategy.language import (
    ComparisonOperator,
    FeatureName,
    RuleSyntaxError,
    parse_strategy_semantic_ast,
    render_canonical_strategy_source,
)
from kirby2.strategy.state_machine import PositionFeature


WO35A_AUDIT_CASE_COUNT = 5
WO35A_CANONICALIZATION_POLICY_SHA256 = (
    "fa2df709734780a866d9c8f9554004ebaad510869be961d5c655a1a19c52c2b8"
)
WO35A_FIXTURE_SHA256 = (
    "84733a622bc99a389823f97c47c57f9d093801b9e27cfd089741f534530d14c5"
)
WO35A_LINEAGE_FIXTURE_SHA256 = (
    "f331b6624aa2fb6c60705d292d25d1c6a3ae0938555ddd78026282b94c2b7cd6"
)
WO35B_AUDIT_CASE_COUNT = 5
WO35B_FIXTURE_SHA256 = (
    "402f5ad2c9806825ce62668e35f25fa5d3a33aff426269cc36b64a848729b89b"
)
WO35B_ACCESS_POLICY_SHA256 = (
    "8e7ef0a44e5780502b8b1d453b19cdafa757aa3de39f997a608e0754ec31d635"
)
WO35C_AUDIT_CASE_COUNT = 5
WO35C_OPERATOR_REGISTRY_SHA256 = (
    "40f0e73d38c3d8e4256abb8b320ff953a8538621e990a38ef82b35e46bf5d31c"
)
WO35C_FIXTURE_SHA256 = (
    "efc7fbd7d1d2ce7b6fe9d3134002efbab2b4dd82b43a76639c0f339412fe36c5"
)
WO35C_BATCH_SHA256 = (
    "d07b5df16b6a11972d5ed4e24b13040965f750e07ce533641a88bf4d222e44a0"
)
WO35C_ACCOUNTING_SHA256 = (
    "cb62a448fc87401c68c318eab155233bd66a7f1aa470881ffb06cbdc70c3ca5a"
)
WO35D_AUDIT_CASE_COUNT = 5
WO35D_MANIFEST_FIXTURE_SHA256 = (
    "3965babf238cf8a1fc57ddf96cd4e25a8eba137197da74ff765409eb159ee127"
)
WO35D_POLICY_FIXTURE_SHA256 = (
    "b6d2fd6e83e5064a588f4c3f0e62c4f07c7e3388aa6bf8f6f8aaae3ec95147d3"
)
WO35D_OBJECTIVE_FIXTURE_SHA256 = (
    "80e118a3bb25da94d74cec4584258ee52d78a006d9e97d773ee3ef38fe84520d"
)
WO35D_ACCESS_FIXTURE_SHA256 = (
    "9f70f10834b939ff4f063109d61b882bbce113f3cee3c169bfcd96be7ae432ed"
)
WO35D_NO_WINNER_RUN_SHA256 = (
    "bed290d4e8bc95388f25677f15a6b65af0369a0c4b2958aec87bed43837dac91"
)
WO35E_AUDIT_CASE_COUNT = 5
WO35E_PERTURBATION_FIXTURE_SHA256 = (
    "5826e162b1b0876d49c02a8306712845ca0f014eaef3a5d65dd7ed5afcdc6471"
)
WO35E_ROBUSTNESS_FIXTURE_SHA256 = (
    "48986e4fe0118cec5d6e48c41719a5a316f327043a1fa76c6a77b1385f0f97e7"
)
WO35E_OBSERVABILITY_FIXTURE_SHA256 = (
    "e8c16437614a1bdb9a716ada713e2ba54d78d8611df4554fd297d9c99679a0e3"
)
WO35E_OVERFIT_FIXTURE_SHA256 = (
    "94adb078dbe9256b2e4e6e191e9b794e772a30cfce54fba64410081a1bcff0ec"
)
WO35E_REVEAL_FIXTURE_SHA256 = (
    "5907a21418b16db9476c2e96bc46dd6f202973abc3a5bce7fa9d39310de141e2"
)

_TRAFFIC_A = """\
setup semantic_probe
# The 5s window and REFUSE policy are both defaults.
GREEN when
    spread_ticks <= 2.00
    book_imbalance >= 0.200
WAIT when
    spread_ticks <= 4.0
RED otherwise
"""

_TRAFFIC_B = """\
SeTuP semantic_probe
WINDOW 5000ms
UNAVAILABLE refuse
green WHEN
    book_imbalance >= 2e-1
    spread_ticks <= 2
    spread_ticks <= 2.000
wait when
    spread_ticks <= 4.000
red otherwise
"""

_TRAFFIC_CHILD = """\
setup semantic_probe
window 5s
unavailable REFUSE
GREEN when
    book_imbalance >= 0.3
    spread_ticks <= 2
WAIT when
    spread_ticks <= 4
RED otherwise
"""

_MACHINE_A = """\
machine priority_probe
window 1s
initial IDLE
state IDLE signal WAIT entry DENY exit ALLOW
state ARMED signal GREEN entry ALLOW exit ALLOW
state HALT signal RED entry DENY exit ALLOW cooldown 1s
transition IDLE -> ARMED when
    spread_ticks <= 2.00
    book_imbalance >= 0.0
transition IDLE -> HALT when
    spread_ticks <= 2
"""

_MACHINE_B = """\
MACHINE priority_probe
WINDOW 1000ms
UNAVAILABLE REFUSE
INITIAL IDLE
STATE HALT SIGNAL RED ENTRY DENY EXIT ALLOW COOLDOWN 1000ms
STATE IDLE SIGNAL WAIT ENTRY DENY EXIT ALLOW
STATE ARMED SIGNAL GREEN ENTRY ALLOW EXIT ALLOW
TRANSITION IDLE -> ARMED WHEN
    book_imbalance >= -0.000
    spread_ticks <= 2
TRANSITION IDLE -> HALT WHEN
    spread_ticks <= 2.000
"""

_MACHINE_PRIORITY_SWAPPED = """\
machine priority_probe
window 1s
unavailable REFUSE
initial IDLE
state ARMED signal GREEN entry ALLOW exit ALLOW
state HALT signal RED entry DENY exit ALLOW cooldown 1s
state IDLE signal WAIT entry DENY exit ALLOW
transition IDLE -> HALT when
    spread_ticks <= 2
transition IDLE -> ARMED when
    spread_ticks <= 2
    book_imbalance >= 0
"""

_MUTATION_MACHINE = """\
machine mutation_probe
window 1s
unavailable REFUSE
initial IDLE
state IDLE signal WAIT entry DENY exit ALLOW
state ARMED signal GREEN entry ALLOW exit ALLOW
state HALT signal RED entry DENY exit ALLOW cooldown 1s
transition IDLE -> ARMED when for 500ms
    spread_ticks <= 2
    book_imbalance >= 0
transition ARMED -> IDLE when events 2 within 1s
    aggressive_sell_volume >= 1
transition ARMED -> HALT when
    spread_ticks > 5
transition HALT -> IDLE after entry
"""

_ACTUAL_FIXTURE_SHA256 = hashlib.sha256(
    canonical_identity_bytes(
        {
            "machine_a": _MACHINE_A,
            "machine_b": _MACHINE_B,
            "machine_priority_swapped": _MACHINE_PRIORITY_SWAPPED,
            "traffic_a": _TRAFFIC_A,
            "traffic_b": _TRAFFIC_B,
            "traffic_child": _TRAFFIC_CHILD,
        }
    )
).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyDiscoveryAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    required: bool = True


def _parse_render_parse_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic = parse_strategy_ast(_TRAFFIC_A)
    machine = parse_strategy_ast(_MACHINE_A)
    traffic_rendered = render_canonical_strategy_ast(traffic)
    machine_rendered = render_canonical_strategy_ast(machine)
    if strategy_ast_round_trip(traffic) != traffic:
        failures.append("traffic-light parse/render/parse changed semantic AST")
    if strategy_ast_round_trip(machine) != machine:
        failures.append("state-machine parse/render/parse changed semantic AST")
    if render_canonical_strategy_ast(parse_strategy_ast(traffic_rendered)) != traffic_rendered:
        failures.append("traffic-light canonical rendering is not a fixed point")
    if render_canonical_strategy_ast(parse_strategy_ast(machine_rendered)) != machine_rendered:
        failures.append("state-machine canonical rendering is not a fixed point")
    if (
        parse_strategy_semantic_ast(_TRAFFIC_A) != traffic
        or render_canonical_strategy_source(_MACHINE_A) != machine_rendered
    ):
        failures.append("legacy strategy-language semantic adapters are inconsistent")
    if _ACTUAL_FIXTURE_SHA256 != WO35A_FIXTURE_SHA256:
        failures.append("WO35-A source fixtures differ from their frozen digest")
    return StrategyDiscoveryAuditCase(
        "a_parse_render_parse_is_semantically_stable_for_both_grammars",
        (
            f"traffic_sha256={strategy_semantic_sha256(traffic)} "
            f"machine_sha256={strategy_semantic_sha256(machine)} "
            "round_trips=2 canonical_fixed_points=2"
        ),
        tuple(failures),
    )


def _equivalence_and_priority_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic_a = parse_strategy_ast(_TRAFFIC_A)
    traffic_b = parse_strategy_ast(_TRAFFIC_B)
    machine_a = parse_strategy_ast(_MACHINE_A)
    machine_b = parse_strategy_ast(_MACHINE_B)
    swapped = parse_strategy_ast(_MACHINE_PRIORITY_SWAPPED)
    if traffic_a != traffic_b or strategy_semantic_sha256(
        traffic_a
    ) != strategy_semantic_sha256(traffic_b):
        failures.append(
            "format, unit, decimal, duplicate, or conjunction ordering did not collapse"
        )
    if render_canonical_strategy_ast(traffic_a) != render_canonical_strategy_ast(
        traffic_b
    ):
        failures.append("equivalent traffic strategies rendered differently")
    if machine_a != machine_b or strategy_semantic_sha256(
        machine_a
    ) != strategy_semantic_sha256(machine_b):
        failures.append("commutative state-machine representation did not collapse")
    if strategy_semantic_sha256(machine_a) == strategy_semantic_sha256(swapped):
        failures.append("transition priority was incorrectly treated as commutative")
    if (
        STRATEGY_CANONICALIZATION_POLICY_SHA256_V1
        != WO35A_CANONICALIZATION_POLICY_SHA256
    ):
        failures.append("canonicalization policy differs from its frozen V1 digest")
    return StrategyDiscoveryAuditCase(
        "a_supported_equivalences_collapse_but_transition_priority_remains_semantic",
        (
            f"policy_sha256={STRATEGY_CANONICALIZATION_POLICY_SHA256_V1} "
            "traffic_duplicates=COLLAPSED machine_formatting=COLLAPSED "
            "transition_priority=PRESERVED"
        ),
        tuple(failures),
    )


def _dual_identity_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    inline_a = StrategyVariant("inline_a", _TRAFFIC_A)
    inline_b = StrategyVariant("inline_b", _TRAFFIC_B)
    file_variant = StrategyVariant(
        "file_variant",
        _TRAFFIC_A,
        "strategies/semantic_probe.k2",
    )
    identity_a = inline_a.strategy_identity
    identity_b = inline_b.strategy_identity
    file_identity = file_variant.strategy_identity
    expected_legacy = hashlib.sha256(_TRAFFIC_A.encode("utf-8")).hexdigest()
    if inline_a.source_sha256 != expected_legacy:
        failures.append("experiment legacy source-byte digest changed")
    if inline_a.as_dict() != {
        "name": "inline_a",
        "source": _TRAFFIC_A,
        "source_path": None,
        "source_sha256": expected_legacy,
    }:
        failures.append("legacy experiment serialization was silently replaced")
    if (
        identity_a.legacy_source_sha256 == identity_b.legacy_source_sha256
        or identity_a.semantic_ast_sha256 != identity_b.semantic_ast_sha256
    ):
        failures.append("dual identity did not distinguish source bytes from semantics")
    if inline_a.discovery_identity_dict() != identity_a.as_dict():
        failures.append("experiment discovery identity sidecar is not reproducible")
    if (
        identity_a.provenance.import_origin
        is not StrategyImportOriginV1.EXPERIMENT_INLINE_SOURCE
        or file_identity.provenance.import_origin
        is not StrategyImportOriginV1.EXPERIMENT_RULE_FILE
        or identity_a.provenance.as_dict()["migration_id"]
        != STRATEGY_IDENTITY_MIGRATION_ID_V1
    ):
        failures.append("strategy migration/import provenance is incomplete")
    rendered_identity = canonical_render_identity(parse_strategy_ast(_TRAFFIC_A))
    if (
        rendered_identity.semantic_ast_sha256 != identity_a.semantic_ast_sha256
        or rendered_identity.provenance.import_origin
        is not StrategyImportOriginV1.CANONICAL_AST_RENDER
    ):
        failures.append("canonical render identity lacks explicit provenance")
    return StrategyDiscoveryAuditCase(
        "a_legacy_source_and_semantic_ast_identities_remain_separate_and_inspectable",
        (
            f"legacy_source_sha256={identity_a.legacy_source_sha256} "
            f"semantic_ast_sha256={identity_a.semantic_ast_sha256} "
            "legacy_projection=PRESERVED origins=INLINE,RULE_FILE,CANONICAL_RENDER"
        ),
        tuple(failures),
    )


def _lineage_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    parent = parse_strategy_ast(_TRAFFIC_A)
    child = parse_strategy_ast(_TRAFFIC_CHILD)
    mutable_parameters: dict[str, object] = {
        "path": "/green_conditions/0/threshold",
        "threshold": {"coefficient": 3, "scale": 1},
    }
    substream = StrategyRngSubstreamV1(35_000_001, "mutation/threshold/0")
    node = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters=mutable_parameters,
        rng_substream=substream,
        valid=True,
    )
    repeated = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters=mutable_parameters,
        rng_substream=substream,
        valid=True,
    )
    mutable_parameters["threshold"] = {"coefficient": 999, "scale": 0}
    diff_paths = tuple(entry.path for entry in node.semantic_diff)
    if diff_paths != ("/green_conditions/0/threshold/coefficient",):
        failures.append("lineage semantic diff is not the exact canonical field change")
    if node.parent_semantic_sha256 != strategy_semantic_sha256(parent):
        failures.append("lineage parent semantic digest is not bound")
    if node.child_semantic_sha256 != strategy_semantic_sha256(child):
        failures.append("lineage child semantic digest is not bound")
    if node.lineage_sha256 != repeated.lineage_sha256:
        failures.append("identical lineage inputs did not reproduce identity")
    if node.lineage_sha256 != WO35A_LINEAGE_FIXTURE_SHA256:
        failures.append("lineage fixture differs from its frozen V1 identity")
    if thaw_json(node.parameters)["threshold"] != {
        "coefficient": 3,
        "scale": 1,
    }:
        failures.append("lineage parameters retained mutable caller ownership")
    no_op = build_strategy_lineage_node(
        parent,
        parent,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters={"path": "/green_conditions/0/threshold"},
        rng_substream=substream,
        valid=False,
    )
    if no_op.valid or no_op.semantic_diff:
        failures.append("rejected no-op lineage did not retain explicit invalidity")
    if not _raises(
        lambda: build_strategy_lineage_node(
            parent,
            child,
            operation_id="THRESHOLD",
            operation_version=1,
            parameters={"binary_float": 0.3},
            rng_substream=substream,
            valid=True,
        ),
        TypeError,
    ):
        failures.append("lineage accepted a binary-float identity parameter")
    changed_rng = build_strategy_lineage_node(
        parent,
        child,
        operation_id="THRESHOLD",
        operation_version=1,
        parameters={
            "path": "/green_conditions/0/threshold",
            "threshold": {"coefficient": 3, "scale": 1},
        },
        rng_substream=StrategyRngSubstreamV1(
            35_000_001,
            "mutation/threshold/1",
        ),
        valid=True,
    )
    if changed_rng.lineage_sha256 == node.lineage_sha256:
        failures.append("lineage identity omitted the RNG substream")
    return StrategyDiscoveryAuditCase(
        "a_lineage_binds_parent_operation_parameters_rng_child_validity_and_diff",
        (
            f"lineage_sha256={node.lineage_sha256} diff_paths={','.join(diff_paths)} "
            f"rng_sha256={substream.sha256} no_op_valid={str(no_op.valid).lower()}"
        ),
        tuple(failures),
    )


def _unsupported_before_mutation_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    unsupported_sources = (
        _TRAFFIC_A.replace(
            "spread_ticks <= 2.00",
            "spread_ticks <= 2 OR book_imbalance >= 0",
        ),
        _TRAFFIC_A.replace("spread_ticks <= 2.00", "spread_ticks <= 1e999999"),
    )
    mutation_attempts: list[str] = []
    errors: list[str] = []
    for unsupported in unsupported_sources:
        for _ in range(2):
            try:
                parse_strategy_ast(unsupported)
                mutation_attempts.append("MUTATION_STARTED")
            except (RuleSyntaxError, ValueError) as error:
                errors.append(f"{type(error).__name__}:{error}")
    if mutation_attempts:
        failures.append("unsupported grammar reached the mutation boundary")
    if (
        len(errors) != 4
        or errors[0] != errors[1]
        or errors[2] != errors[3]
    ):
        failures.append("unsupported grammar refusal was not deterministic")
    child = parse_strategy_ast(_TRAFFIC_CHILD)
    if not _raises(
        lambda: build_strategy_lineage_node(  # type: ignore[arg-type]
            _TRAFFIC_A,
            child,
            operation_id="THRESHOLD",
            operation_version=1,
            parameters={},
            rng_substream=StrategyRngSubstreamV1(1, "invalid/raw-parent"),
            valid=True,
        ),
        TypeError,
    ):
        failures.append("lineage builder accepted unparsed source text")
    return StrategyDiscoveryAuditCase(
        "a_unsupported_grammar_fails_deterministically_before_mutation",
        (
            f"parse_refusals={len(errors)}/4 mutation_attempts={len(mutation_attempts)} "
            "raw_lineage_parent=REFUSED"
        ),
        tuple(failures),
    )


def _raises(operation, expected: type[BaseException]) -> bool:
    try:
        operation()
    except expected:
        return True
    return False


def audit_wo35a_strategy_discovery() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _parse_render_parse_case(),
        _equivalence_and_priority_case(),
        _dual_identity_case(),
        _lineage_case(),
        _unsupported_before_mutation_case(),
    )


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _partition_fixture(
    *,
    version: int = 1,
    namespace: str = "primary",
) -> PartitionManifestV1:
    specs = (
        (
            "train-a",
            StrategyPartitionV1.TRAIN,
            3_501_000,
            "2025-01-06",
            "QUIET_RANGE_PRESSURE",
        ),
        (
            "validation-a",
            StrategyPartitionV1.VALIDATION,
            3_502_000,
            "2025-02-03",
            "TREND_PRESSURE",
        ),
        (
            "holdout-a",
            StrategyPartitionV1.HOLDOUT,
            3_503_000,
            "2025-03-03",
            "EVENT_SHOCK_PRESSURE",
        ),
        (
            "adversarial-a",
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
            3_504_000,
            "2025-04-07",
            "DISORDERLY_OPEN_STABILIZATION_PRESSURE",
        ),
        (
            "robustness-a",
            StrategyPartitionV1.ROBUSTNESS,
            3_505_000,
            NOT_APPLICABLE_V1,
            "QUIET_RANGE_PRESSURE",
        ),
    )
    version_offset = (version - 1) * 100
    members = tuple(
        PartitionMemberV1(
            member_id=member_id,
            partition=partition,
            source_day=source_day,
            scenario_family=scenario_family,
            historical_period=(
                HistoricalPeriodV1.not_applicable()
                if source_day == NOT_APPLICABLE_V1
                else HistoricalPeriodV1.interval(
                    1_736_121_600_000_000_000 + version_offset + index * 10_000,
                    1_736_121_600_000_005_000 + version_offset + index * 10_000,
                )
            ),
            seed=seed + version_offset,
            dataset_sha256=_digest(f"{namespace}/dataset/{member_id}"),
            independence_group_sha256=_digest(
                f"{namespace}/independence/{member_id}"
            ),
            extracted_window_ancestry_sha256=(
                _digest(f"{namespace}/window-root/{member_id}"),
                _digest(f"{namespace}/window/{member_id}"),
            ),
            branch_ancestry_sha256=(
                _digest(f"{namespace}/branch-root/{member_id}"),
                _digest(f"{namespace}/branch/{member_id}"),
            ),
        )
        for index, (
            member_id,
            partition,
            seed,
            source_day,
            scenario_family,
        ) in enumerate(specs)
    )
    return PartitionManifestV1(
        experiment_id="sealed-strategy-probe",
        experiment_version=version,
        members=members,
        validation_schedule=(
            ValidationScheduleV1(
                schedule_id="validation-pass-1",
                member_ids=("validation-a",),
                release_after_train_access_count=1,
                max_access_count=1,
            ),
        ),
    )


def _candidate_freeze(
    experiment: StrategyDiscoveryExperimentV1,
) -> StrategyDiscoveryExperimentV1:
    candidate_a = _digest("candidate/semantic/a")
    candidate_b = _digest("candidate/semantic/b")
    return freeze_candidate_set(
        experiment,
        candidate_semantic_sha256=(candidate_b, candidate_a),
        selected_candidate_semantic_sha256=candidate_a,
        selection_record_sha256=_digest("candidate/selection-record"),
    )


def _partition_manifest_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    manifest = _partition_fixture()
    round_trip = partition_manifest_round_trip(manifest)
    view = manifest.search_view()
    legacy_manifest = ExperimentManifest(
        experiment_id=manifest.experiment_id,
        mode=ExperimentMode.PASSIVE_OBSERVER,
        scenario_names=("quiet", "shock"),
        seeds=(101, 102),
        strategies=(
            StrategyVariant("baseline", _TRAFFIC_A),
            StrategyVariant("candidate", _TRAFFIC_CHILD),
        ),
        duration_us=1_000_000,
        fork_time_us=0,
        decision_interval_us=100_000,
        quantity=1,
    )
    binding = legacy_manifest.bind_discovery_partitions(manifest)
    if round_trip != manifest or round_trip.manifest_sha256 != manifest.manifest_sha256:
        failures.append("canonical partition manifest round trip changed identity")
    if manifest.manifest_sha256 != WO35B_FIXTURE_SHA256:
        failures.append("WO35-B partition fixture differs from its frozen digest")
    if (
        binding.experiment_manifest_sha256 != legacy_manifest.sha256
        or binding.partition_manifest_sha256 != manifest.manifest_sha256
        or len(binding.strategy_semantic_sha256) != 2
    ):
        failures.append("legacy experiment identity was not bound to sealed partitions")
    sealed = view["sealed_partitions"]
    if not isinstance(sealed, dict) or any(
        not isinstance(value, dict)
        or set(value) != {"member_count", "members_sha256"}
        for value in sealed.values()
    ):
        failures.append("search view exposed sealed partition member details")
    if "holdout-a" in manifest.search_view().__repr__():
        failures.append("search view leaked a sealed holdout member ID")
    train = manifest.partition_members(StrategyPartitionV1.TRAIN)[0]
    holdout = manifest.partition_members(StrategyPartitionV1.HOLDOUT)[0]
    leaked_members = tuple(
        replace(
            member,
            independence_group_sha256=train.independence_group_sha256,
        )
        if member is holdout
        else member
        for member in manifest.members
    )
    if not _raises(
        lambda: replace(manifest, members=leaked_members),
        ValueError,
    ):
        failures.append("partition manifest accepted cross-partition ancestry")
    duplicate_seed_members = tuple(
        replace(member, seed=train.seed)
        if member is holdout
        else member
        for member in manifest.members
    )
    if not _raises(
        lambda: replace(manifest, members=duplicate_seed_members),
        ValueError,
    ):
        failures.append("partition manifest accepted a reused partition seed")
    if not _raises(
        lambda: PartitionManifestV1.from_json_bytes(
            manifest.canonical_bytes() + b"\n"
        ),
        ValueError,
    ):
        failures.append("partition manifest accepted noncanonical mutated bytes")
    return StrategyDiscoveryAuditCase(
        "b_partition_manifest_is_canonical_sealed_and_ancestry_disjoint",
        (
            f"manifest_sha256={manifest.manifest_sha256} members={len(manifest.members)} "
            "source_days=BOUND periods=BOUND ancestry=CROSS_PARTITION_REFUSED "
            "legacy_experiment=BOUND sealed_search_projection=COUNT_AND_DIGEST_ONLY"
        ),
        tuple(failures),
    )


def _search_schedule_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    manifest = _partition_fixture()
    state = start_strategy_experiment(manifest)
    validation_ids = manifest.schedule("validation-pass-1").member_ids
    early_validation = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.VALIDATION,
        purpose=PartitionAccessPurposeV1.SEARCH_VALIDATION,
        member_ids=validation_ids,
        validation_schedule_id="validation-pass-1",
    )
    state = early_validation.experiment
    train = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.TRAIN,
        purpose=PartitionAccessPurposeV1.SEARCH_TRAIN,
    )
    state = train.experiment
    validation = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.VALIDATION,
        purpose=PartitionAccessPurposeV1.SEARCH_VALIDATION,
        member_ids=validation_ids,
        validation_schedule_id="validation-pass-1",
    )
    state = validation.experiment
    exhausted = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.VALIDATION,
        purpose=PartitionAccessPurposeV1.SEARCH_VALIDATION,
        member_ids=validation_ids,
        validation_schedule_id="validation-pass-1",
    )
    state = exhausted.experiment
    early_holdout = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
    )
    state = early_holdout.experiment
    if (
        early_validation.record.reason
        is not PartitionAccessReasonV1.VALIDATION_NOT_RELEASED
        or early_validation.members
        or train.record.decision is not PartitionAccessDecisionV1.GRANTED
        or validation.record.decision is not PartitionAccessDecisionV1.GRANTED
        or exhausted.record.reason
        is not PartitionAccessReasonV1.VALIDATION_BUDGET_EXHAUSTED
        or early_holdout.record.reason
        is not PartitionAccessReasonV1.CANDIDATES_NOT_FROZEN
        or early_holdout.members
    ):
        failures.append("search partition access did not enforce schedule and sealing")
    access_digests = state.access_record_sha256
    records = (
        early_validation.record,
        train.record,
        validation.record,
        exhausted.record,
        early_holdout.record,
    )
    if access_digests != tuple(item.access_sha256 for item in records):
        failures.append("granted and refused search accesses are not fully recorded")
    if any(
        record.previous_access_sha256
        != (None if index == 0 else records[index - 1].access_sha256)
        for index, record in enumerate(records)
    ):
        failures.append("search access records are not hash chained")
    actual_policy_sha256 = hashlib.sha256(
        canonical_identity_bytes(
            [
                {
                    "decision": item.decision.value,
                    "reason": item.reason.value,
                }
                for item in records
            ]
        )
    ).hexdigest()
    if actual_policy_sha256 != WO35B_ACCESS_POLICY_SHA256:
        failures.append("WO35-B access decision policy differs from its frozen digest")
    return StrategyDiscoveryAuditCase(
        "b_search_access_obeys_predeclared_validation_schedule",
        (
            f"access_policy_sha256={actual_policy_sha256} access_records={len(records)} "
            "early_validation=REFUSED train=GRANTED validation=GRANTED "
            "validation_reuse=REFUSED early_holdout=REFUSED"
        ),
        tuple(failures),
    )


def _terminal_reveal_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    manifest = _partition_fixture()
    frozen = _candidate_freeze(start_strategy_experiment(manifest))
    repeated_frozen = _candidate_freeze(start_strategy_experiment(manifest))
    reveal = request_partition_access(
        manifest,
        frozen,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
    )
    repeated_reveal = request_partition_access(
        manifest,
        repeated_frozen,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
    )
    state = reveal.experiment
    second_reveal = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
    )
    state = second_reveal.experiment
    post_reveal_search = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.TRAIN,
        purpose=PartitionAccessPurposeV1.SEARCH_TRAIN,
    )
    state = post_reveal_search.experiment
    adversarial = request_partition_access(
        manifest,
        state,
        partition=StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        purpose=PartitionAccessPurposeV1.TERMINAL_EVALUATION,
    )
    failed = close_terminal_evaluation(
        adversarial.experiment,
        TerminalEvaluationOutcomeV1.FAILED,
    )
    if (
        reveal.record.decision is not PartitionAccessDecisionV1.GRANTED
        or reveal.experiment.phase is not ExperimentPhaseV1.TERMINAL_EVALUATION
        or not reveal.members
        or reveal.record.access_sha256 != repeated_reveal.record.access_sha256
    ):
        failures.append("valid one-shot holdout reveal is not deterministic")
    if (
        second_reveal.record.reason
        is not PartitionAccessReasonV1.REVEAL_ALREADY_CONSUMED
        or second_reveal.members
        or post_reveal_search.record.reason
        is not PartitionAccessReasonV1.SEARCH_TERMINATED
        or post_reveal_search.members
        or adversarial.record.decision is not PartitionAccessDecisionV1.GRANTED
    ):
        failures.append("terminal evaluation allowed reveal reuse or later search")
    if not _raises(
        lambda: _candidate_freeze(frozen),
        ValueError,
    ):
        failures.append("frozen candidate selection was mutable")
    if not _raises(
        lambda: close_terminal_evaluation(
            failed,
            TerminalEvaluationOutcomeV1.PASSED,
        ),
        ValueError,
    ):
        failures.append("failed terminal evaluation was reopened or rewritten")
    return StrategyDiscoveryAuditCase(
        "b_candidate_freeze_and_one_shot_reveal_are_terminal",
        (
            f"candidate_freeze_sha256={frozen.candidate_freeze_sha256} "
            f"reveal_sha256={reveal.record.access_sha256} reveal_reuse=REFUSED "
            "post_reveal_search=REFUSED adversarial=GRANTED outcome=FAILED_TERMINAL"
        ),
        tuple(failures),
    )


def _successor_partition_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    previous_manifest = _partition_fixture()
    frozen = _candidate_freeze(start_strategy_experiment(previous_manifest))
    revealed = request_partition_access(
        previous_manifest,
        frozen,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
    ).experiment
    previous = close_terminal_evaluation(
        revealed,
        TerminalEvaluationOutcomeV1.FAILED,
    )
    successor_manifest = _partition_fixture(version=2, namespace="successor")
    successor = start_successor_experiment(
        previous,
        previous_manifest,
        successor_manifest,
    )
    old_holdout = previous_manifest.partition_members(StrategyPartitionV1.HOLDOUT)[0]
    new_holdout = successor_manifest.partition_members(StrategyPartitionV1.HOLDOUT)[0]
    ancestry_reuse = replace(
        successor_manifest,
        members=tuple(
            replace(
                item,
                independence_group_sha256=old_holdout.independence_group_sha256,
            )
            if item is new_holdout
            else item
            for item in successor_manifest.members
        ),
    )
    dataset_reuse = replace(
        successor_manifest,
        members=tuple(
            replace(item, dataset_sha256=old_holdout.dataset_sha256)
            if item is new_holdout
            else item
            for item in successor_manifest.members
        ),
    )
    if successor.phase is not ExperimentPhaseV1.SEARCH_OPEN:
        failures.append("valid untouched successor did not open a new search")
    if not _raises(
        lambda: start_successor_experiment(
            previous,
            previous_manifest,
            ancestry_reuse,
        ),
        ValueError,
    ):
        failures.append("successor reused prior terminal ancestry")
    if not _raises(
        lambda: start_successor_experiment(
            previous,
            previous_manifest,
            dataset_reuse,
        ),
        ValueError,
    ):
        failures.append("successor reused a prior terminal dataset")
    return StrategyDiscoveryAuditCase(
        "b_successor_requires_new_untouched_terminal_partitions",
        (
            f"previous_version={previous.experiment_version} "
            f"successor_version={successor.experiment_version} "
            "reused_ancestry=REFUSED reused_dataset=REFUSED untouched=SEARCH_OPEN"
        ),
        tuple(failures),
    )


def _immutable_store_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        store = RunStore(Path(directory))
        manifest = _partition_fixture()
        manifest_reference = store.record_strategy_partition_manifest(manifest)
        train_member = manifest.partition_members(StrategyPartitionV1.TRAIN)[0]
        mutated_manifest = replace(
            manifest,
            members=tuple(
                replace(
                    item,
                    dataset_sha256=_digest("mutated/train-dataset"),
                )
                if item is train_member
                else item
                for item in manifest.members
            ),
        )
        if not _raises(
            lambda: store.record_strategy_partition_manifest(mutated_manifest),
            RuntimeError,
        ):
            failures.append("research store accepted manifest mutation in place")
        state = start_strategy_experiment(manifest)
        initial_reference = store.record_strategy_experiment_state(state)
        early = request_partition_access(
            manifest,
            state,
            partition=StrategyPartitionV1.HOLDOUT,
            purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
        )
        early_reference = store.record_strategy_access_record(early.record)
        store.record_strategy_experiment_state(early.experiment)
        train = request_partition_access(
            manifest,
            early.experiment,
            partition=StrategyPartitionV1.TRAIN,
            purpose=PartitionAccessPurposeV1.SEARCH_TRAIN,
        )
        store.record_strategy_access_record(train.record)
        store.record_strategy_experiment_state(train.experiment)
        frozen = _candidate_freeze(train.experiment)
        store.record_strategy_experiment_state(frozen)
        closed_search = request_partition_access(
            manifest,
            frozen,
            partition=StrategyPartitionV1.TRAIN,
            purpose=PartitionAccessPurposeV1.SEARCH_TRAIN,
        )
        forged_grant = replace(
            closed_search.record,
            decision=PartitionAccessDecisionV1.GRANTED,
            reason=PartitionAccessReasonV1.GRANTED,
            metrics_visible=True,
            granted_member_ids=("train-a",),
        )
        if not _raises(
            lambda: store.record_strategy_access_record(forged_grant),
            RuntimeError,
        ):
            failures.append("research store persisted a fabricated access grant")
        reveal = request_partition_access(
            manifest,
            frozen,
            partition=StrategyPartitionV1.HOLDOUT,
            purpose=PartitionAccessPurposeV1.HOLDOUT_REVEAL,
        )
        reveal_reference = store.record_strategy_access_record(reveal.record)
        terminal_reference = store.record_strategy_experiment_state(reveal.experiment)
        records = store.query_strategy_access_records(
            manifest.experiment_id,
            manifest.experiment_version,
        )
        if (
            store.load_strategy_partition_manifest(manifest.manifest_sha256) != manifest
            or store.load_strategy_experiment_state(reveal.experiment.state_sha256)
            != reveal.experiment
            or tuple(item.access_sha256 for item in records)
            != reveal.experiment.access_record_sha256
        ):
            failures.append("research store did not reproduce partition access lineage")
        references = (
            manifest_reference,
            initial_reference,
            early_reference,
            reveal_reference,
            terminal_reference,
        )
        expected_types = {
            ArtifactType.STRATEGY_PARTITION_MANIFEST,
            ArtifactType.STRATEGY_EXPERIMENT_STATE,
            ArtifactType.STRATEGY_ACCESS_RECORD,
        }
        if {item.artifact_type for item in references} != expected_types:
            failures.append("research store omitted typed strategy artifacts")
        access_path = Path(directory) / early_reference.relative_path
        tampered = access_path.read_bytes() + b"\n"
        access_path.write_bytes(tampered)
        if not _raises(
            lambda: store.load_strategy_access_record(early.record.access_sha256),
            ValueError,
        ):
            failures.append("research store accepted mutated access bytes")
        if not _raises(
            lambda: store.record_strategy_access_record(early.record),
            RuntimeError,
        ):
            failures.append("research store overwrote a mutated immutable artifact")
        if access_path.read_bytes() != tampered:
            failures.append("failed immutable write changed existing evidence")
        decision_inventory = tuple(item.decision.value for item in records)
    return StrategyDiscoveryAuditCase(
        "b_research_store_persists_immutable_audit_visible_access",
        (
            f"records={len(decision_inventory)} decisions={','.join(decision_inventory)} "
            "artifact_types=PARTITION,STATE,ACCESS manifest_mutation=REFUSED "
            "forged_grant=REFUSED access_mutation=DETECTED overwrite=REFUSED"
        ),
        tuple(failures),
    )


def audit_wo35b_strategy_partitions() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _partition_manifest_case(),
        _search_schedule_case(),
        _terminal_reveal_case(),
        _successor_partition_case(),
        _immutable_store_case(),
    )


def _exact_decimal(coefficient: int, scale: int = 0) -> dict[str, int]:
    return {"coefficient": coefficient, "scale": scale}


def _comparison(
    feature: str,
    operator: ComparisonOperator,
    coefficient: int,
    scale: int = 0,
) -> dict[str, object]:
    return {
        "feature": feature,
        "operator": operator.value,
        "threshold": _exact_decimal(coefficient, scale),
    }


def _mutation_available_features() -> tuple[str, ...]:
    return tuple(
        sorted(
            {item.value for item in FeatureName}
            | {item.value for item in PositionFeature}
        )
    )


def _mutation_valid_fixtures() -> tuple[
    tuple[StrategyAstV1, MutationRequestV1],
    ...,
]:
    traffic = parse_strategy_ast(_TRAFFIC_A)
    machine = parse_strategy_ast(_MUTATION_MACHINE)
    return (
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.THRESHOLD,
                1,
                {
                    "path": "/green_conditions/0",
                    "threshold": _exact_decimal(25, 2),
                },
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.ROLLING_WINDOW,
                1,
                {"window_us": 6_000_000},
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.REQUIRED_DURATION,
                1,
                {"duration_us": 600_000, "transition_index": 0},
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.ADD_CONDITION,
                1,
                {
                    "collection_path": "/green_conditions",
                    "condition": _comparison(
                        FeatureName.RELATIVE_VOLUME.value,
                        ComparisonOperator.GREATER_EQUAL,
                        1,
                    ),
                },
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.REMOVE_CONDITION,
                1,
                {"path": "/green_conditions/0"},
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.FEATURE_REPLACEMENT,
                1,
                {
                    "feature": FeatureName.RELATIVE_VOLUME.value,
                    "path": "/green_conditions/0",
                },
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.LOGICAL_OPERATOR,
                1,
                {
                    "operator": ComparisonOperator.GREATER.value,
                    "path": "/green_conditions/0",
                },
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.TRANSITION_CONDITION,
                1,
                {
                    "condition": _comparison(
                        FeatureName.AGGRESSIVE_BUY_VOLUME.value,
                        ComparisonOperator.GREATER_EQUAL,
                        1,
                    ),
                    "path": "/transitions/0/conditions/0",
                },
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.COOLDOWN,
                1,
                {"cooldown_us": 500_000, "state_name": "IDLE"},
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.STATE_TIMEOUT,
                1,
                {"timeout_us": 2_000_000, "transition_index": 3},
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.CONFIRMATION_COUNT,
                1,
                {"event_count": 3, "transition_index": 1},
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.INVALIDATION_RULE,
                1,
                {
                    "condition": _comparison(
                        FeatureName.BOOK_IMBALANCE.value,
                        ComparisonOperator.LESS_EQUAL,
                        -5,
                        1,
                    ),
                    "transition_index": 2,
                },
            ),
        ),
        (
            machine,
            MutationRequestV1(
                MutationOperationIdV1.POSITION_CONSTRAINT,
                1,
                {
                    "feature": PositionFeature.POSITION.value,
                    "operator": ComparisonOperator.LESS_EQUAL.value,
                    "threshold": _exact_decimal(10),
                    "transition_index": 0,
                },
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.SPREAD_LIMIT,
                1,
                {
                    "collection_path": "/wait_conditions",
                    "max_spread_ticks": _exact_decimal(3),
                },
            ),
        ),
        (
            traffic,
            MutationRequestV1(
                MutationOperationIdV1.VOLUME_REQUIREMENT,
                1,
                {
                    "collection_path": "/green_conditions",
                    "feature": FeatureName.AGGRESSIVE_BUY_VOLUME.value,
                    "minimum": _exact_decimal(10),
                },
            ),
        ),
    )


def _mutation_fixture_sha256() -> str:
    return hashlib.sha256(
        canonical_identity_bytes(
            [
                {
                    "parent_semantic_sha256": strategy_semantic_sha256(parent),
                    "request": request.as_dict(),
                }
                for parent, request in _mutation_valid_fixtures()
            ]
        )
    ).hexdigest()


def _apply_audit_mutation(
    parent: StrategyAstV1,
    request: MutationRequestV1,
    label: str,
) -> StrategyMutationResultV1:
    return apply_strategy_mutation(
        parent,
        request,
        rng_substream=StrategyRngSubstreamV1(35_000_003, label),
        available_features=_mutation_available_features(),
    )


def _mutation_registry_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    operation_ids = tuple(item.operation_id for item in REQUIRED_MUTATION_OPERATORS_V1)
    registry_sha256 = hashlib.sha256(
        canonical_identity_bytes([item.as_dict() for item in REQUIRED_MUTATION_OPERATORS_V1])
    ).hexdigest()
    if operation_ids != tuple(MutationOperationIdV1):
        failures.append("required mutation operator inventory is incomplete or reordered")
    if len(REQUIRED_MUTATION_OPERATORS_V1) != 15:
        failures.append("required mutation operator inventory does not contain 15 IDs")
    if any(
        not item.input_node_kinds
        or not item.parameter_domain
        or not item.semantic_validation
        or not item.machine_reason
        or not item.human_reason
        or not item.inverse_description
        or not item.diff_description
        or item.complexity_delta_rule != "EXACT_CHILD_MINUS_PARENT_V1"
        for item in REQUIRED_MUTATION_OPERATORS_V1
    ):
        failures.append("an operator omitted a bounded declaration or explanation")
    if registry_sha256 != WO35C_OPERATOR_REGISTRY_SHA256:
        failures.append("WO35-C operator registry differs from its frozen digest")
    return StrategyDiscoveryAuditCase(
        "c_required_operator_registry_is_complete_declared_and_bounded",
        (
            f"operators={len(operation_ids)} registry_sha256={registry_sha256} "
            "domains=BOUNDED observability=DECLARED semantic_validation=DECLARED "
            "inverse=DECLARED "
            "complexity_rule=EXACT_CHILD_MINUS_PARENT_V1"
        ),
        tuple(failures),
    )


def _mutation_fixture_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    accepted_ids: list[str] = []
    rejected_ids: list[str] = []
    for parent, request in _mutation_valid_fixtures():
        label = f"audit/wo35c/{request.operation_id.value.lower()}"
        first = _apply_audit_mutation(parent, request, label)
        repeated = _apply_audit_mutation(parent, request, label)
        if (
            first.record.status is not MutationStatusV1.ACCEPTED
            or not first.record.evaluation_eligible
        ):
            failures.append(f"{request.operation_id.value} valid fixture was rejected")
        elif first.record.canonical_bytes() != repeated.record.canonical_bytes():
            failures.append(f"{request.operation_id.value} valid fixture was unstable")
        else:
            accepted_ids.append(request.operation_id.value)
        invalid_parameters = thaw_json(request.parameters)
        invalid_parameters["unexpected_parameter"] = True
        invalid_request = MutationRequestV1(
            request.operation_id,
            request.operation_version,
            invalid_parameters,
        )
        invalid = _apply_audit_mutation(parent, invalid_request, label + "/invalid")
        invalid_repeated = _apply_audit_mutation(
            parent,
            invalid_request,
            label + "/invalid",
        )
        if (
            invalid.record.status is not MutationStatusV1.REJECTED
            or invalid.record.evaluation_eligible
            or invalid.record.canonical_bytes()
            != invalid_repeated.record.canonical_bytes()
        ):
            failures.append(f"{request.operation_id.value} invalid fixture was unstable")
        else:
            rejected_ids.append(request.operation_id.value)
    fixture_sha256 = _mutation_fixture_sha256()
    if fixture_sha256 != WO35C_FIXTURE_SHA256:
        failures.append("WO35-C mutation fixtures differ from their frozen digest")
    return StrategyDiscoveryAuditCase(
        "c_every_operator_has_deterministic_valid_and_invalid_fixtures",
        (
            f"valid={len(accepted_ids)}/15 invalid={len(rejected_ids)}/15 "
            f"fixture_sha256={fixture_sha256} typed_children=ONLY"
        ),
        tuple(failures),
    )


def _mutation_accounting_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    record_digests: list[str] = []
    for parent, request in _mutation_valid_fixtures():
        result = _apply_audit_mutation(
            parent,
            request,
            f"audit/wo35c/accounting/{request.operation_id.value.lower()}",
        )
        record = result.record
        expected_delta = StrategyComplexityDeltaV1.between(
            strategy_complexity(parent),
            strategy_complexity(result.child),
        )
        if (
            record.mutation_diff.semantic_diff
            != semantic_strategy_diff(parent, result.child)
            or record.lineage.semantic_diff != record.mutation_diff.semantic_diff
            or record.complexity_before != strategy_complexity(parent)
            or record.complexity_after != strategy_complexity(result.child)
            or record.complexity_delta != expected_delta
            or record.parent_semantic_sha256 != strategy_semantic_sha256(parent)
            or record.child_semantic_sha256
            != strategy_semantic_sha256(result.child)
            or strategy_ast_round_trip(result.child) != result.child
        ):
            failures.append(f"{request.operation_id.value} accounting disagrees")
        record_digests.append(record.record_sha256)
    accounting_sha256 = hashlib.sha256(
        canonical_identity_bytes(record_digests)
    ).hexdigest()
    if accounting_sha256 != WO35C_ACCOUNTING_SHA256:
        failures.append("WO35-C mutation accounting differs from its frozen digest")
    return StrategyDiscoveryAuditCase(
        "c_semantic_diff_complexity_and_lineage_agree_exactly",
        (
            f"records={len(record_digests)} accounting_sha256={accounting_sha256} "
            f"complexity_schema={STRATEGY_COMPLEXITY_SCHEMA_ID_V1} "
            "round_trip=15/15 lineage_diff=15/15 exact_delta=15/15"
        ),
        tuple(failures),
    )


def _mutation_generation_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic = parse_strategy_ast(_TRAFFIC_A)
    fixtures = tuple(
        request
        for parent, request in _mutation_valid_fixtures()
        if parent.kind == traffic.kind
        and request.operation_id
        in {
            MutationOperationIdV1.THRESHOLD,
            MutationOperationIdV1.ROLLING_WINDOW,
            MutationOperationIdV1.ADD_CONDITION,
        }
    )
    requests = (*fixtures, fixtures[0])
    context = MutationGenerationContextV1(
        root_seed=35_000_003,
        available_features=_mutation_available_features(),
    )
    batch = generate_mutation_batch(traffic, requests, context=context)
    reversed_batch = generate_mutation_batch(
        traffic,
        tuple(reversed(requests)),
        context=context,
    )
    reordered_context_batch = generate_mutation_batch(
        traffic,
        requests,
        context=MutationGenerationContextV1(
            root_seed=35_000_003,
            available_features=tuple(reversed(_mutation_available_features())),
        ),
    )
    statuses = tuple(item.record.status for item in batch.results)
    reasons = tuple(item.record.rejection_reason for item in batch.results)
    draws = tuple(
        labeled_substream_uint64(item.record.rng_substream)
        for item in batch.results
    )
    if (
        batch.canonical_bytes() != reversed_batch.canonical_bytes()
        or batch.canonical_bytes() != reordered_context_batch.canonical_bytes()
    ):
        failures.append("input permutation changed the canonical mutation batch")
    if statuses.count(MutationStatusV1.ACCEPTED) != 3 or reasons.count(
        MutationRejectionReasonV1.DUPLICATE
    ) != 1:
        failures.append("duplicate generation did not accept once and reject once")
    if len(set(draws)) != len(draws):
        failures.append("labeled mutation substreams were not independent")
    if batch.batch_sha256 != WO35C_BATCH_SHA256:
        failures.append("WO35-C generated batch differs from its frozen digest")
    return StrategyDiscoveryAuditCase(
        "c_generation_order_substreams_and_duplicates_are_deterministic",
        (
            f"batch_sha256={batch.batch_sha256} accepted={len(batch.accepted)} "
            f"rejected={len(batch.rejected)} ordering={STRATEGY_MUTATION_GENERATION_ORDER_V1} "
            f"substreams={STRATEGY_MUTATION_SUBSTREAM_LABEL_V1} "
            "request_and_feature_permutation=STABLE"
        ),
        tuple(failures),
    )


def _permission_projection(ast: StrategyAstV1) -> object:
    if hasattr(ast, "states"):
        return tuple(
            (state.name, state.entry_permission.value, state.exit_permission.value)
            for state in ast.states
        )
    return ast.unavailable_policy.value


def _mutation_refusal_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    traffic = parse_strategy_ast(_TRAFFIC_A)
    base_feature_request = MutationRequestV1(
        MutationOperationIdV1.FEATURE_REPLACEMENT,
        1,
        {"feature": FeatureName.RELATIVE_VOLUME.value, "path": "/green_conditions/0"},
    )
    future_request = MutationRequestV1(
        MutationOperationIdV1.FEATURE_REPLACEMENT,
        1,
        {"feature": "future_midprice", "path": "/green_conditions/0"},
    )
    no_op_request = MutationRequestV1(
        MutationOperationIdV1.THRESHOLD,
        1,
        {
            "path": "/green_conditions/0",
            "threshold": _exact_decimal(2, 1),
        },
    )
    invalid_child_request = MutationRequestV1(
        MutationOperationIdV1.REMOVE_CONDITION,
        1,
        {"path": "/wait_conditions/0"},
    )
    resource_request = MutationRequestV1(
        MutationOperationIdV1.ADD_CONDITION,
        1,
        {
            "collection_path": "/green_conditions",
            "condition": _comparison(
                FeatureName.RELATIVE_VOLUME.value,
                ComparisonOperator.GREATER_EQUAL,
                1,
            ),
        },
    )
    arbitrary_request = MutationRequestV1(
        MutationOperationIdV1.THRESHOLD,
        1,
        {
            "callable": "__import__('os').system('false')",
            "path": "/green_conditions/0",
            "threshold": _exact_decimal(25, 2),
        },
    )
    common = {
        "parent": traffic,
        "rng_substream": StrategyRngSubstreamV1(35_000_003, "audit/wo35c/refusal"),
        "known_semantic_sha256": (),
    }
    refusals = (
        apply_strategy_mutation(
            request=future_request,
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
        apply_strategy_mutation(
            request=base_feature_request,
            available_features=(
                FeatureName.BOOK_IMBALANCE.value,
                FeatureName.SPREAD_TICKS.value,
            ),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
        apply_strategy_mutation(
            request=resource_request,
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(max_conditions=3),
            **common,
        ),
        apply_strategy_mutation(
            request=no_op_request,
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
        apply_strategy_mutation(
            request=invalid_child_request,
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
        apply_strategy_mutation(
            request=replace(base_feature_request, operation_version=99),
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
        apply_strategy_mutation(
            request=arbitrary_request,
            available_features=_mutation_available_features(),
            resource_limits=MutationResourceLimitsV1(),
            **common,
        ),
    )
    expected_reasons = (
        MutationRejectionReasonV1.FUTURE_DEPENDENT,
        MutationRejectionReasonV1.UNAVAILABLE_FEATURE,
        MutationRejectionReasonV1.RESOURCE_EXCESSIVE,
        MutationRejectionReasonV1.NO_OP,
        MutationRejectionReasonV1.INVALID_CHILD,
        MutationRejectionReasonV1.UNSUPPORTED_OPERATION_VERSION,
        MutationRejectionReasonV1.INVALID_PARAMETER,
    )
    if tuple(item.record.rejection_reason for item in refusals) != expected_reasons:
        failures.append("mutation refusal reasons differ from the fail-closed contract")
    if any(
        item.record.status is not MutationStatusV1.REJECTED
        or item.record.evaluation_eligible
        or item.record.lineage.valid
        or _permission_projection(item.child) != _permission_projection(traffic)
        for item in refusals
    ):
        failures.append("a refused child became eligible or widened permissions")
    accepted_permissions = tuple(
        (
            _permission_projection(parent),
            _permission_projection(
                _apply_audit_mutation(
                    parent,
                    request,
                    f"audit/wo35c/permission/{request.operation_id.value.lower()}",
                ).child
            ),
        )
        for parent, request in _mutation_valid_fixtures()
    )
    if any(before != after for before, after in accepted_permissions):
        failures.append("a supported mutation changed the permission projection")
    return StrategyDiscoveryAuditCase(
        "c_lookahead_observability_permissions_and_resources_fail_closed",
        (
            "future=REFUSED unavailable=REFUSED excessive=REFUSED no_op=REFUSED "
            "invalid_child=REFUSED unsupported_version=REFUSED arbitrary_code=REFUSED "
            "permissions=PRESERVED evaluation_eligible=0/7"
        ),
        tuple(failures),
    )


def audit_wo35c_strategy_mutations() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _mutation_registry_case(),
        _mutation_fixture_case(),
        _mutation_accounting_case(),
        _mutation_generation_case(),
        _mutation_refusal_case(),
    )


def _search_example_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "discovery" / "examples" / name


def _load_search_examples() -> tuple[StrategySearchManifestV1, StrategySearchManifestV1]:
    return (
        load_search_manifest(_search_example_path("bounded_search.toml")),
        load_search_manifest(_search_example_path("no_winner.toml")),
    )


def _normalized_manifest_payload(payload: object) -> dict[str, object]:
    normalized = thaw_json(payload)
    if type(normalized) is not dict:
        raise TypeError("search manifest payload must thaw to an object")
    normalized["experiment_id"] = "FIXTURE_ID"
    normalized["oracle_id"] = "FIXTURE_ORACLE"
    normalized["expected_outcome"] = "FIXTURE_OUTCOME"
    return normalized


def _search_manifest_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    bounded, no_winner = _load_search_examples()
    bounded_raw = _search_example_path("bounded_search.toml").read_bytes()
    space = bounded.search_space
    universe = space.universe()
    raw_vector_count = 1
    for parameter in space.parameters:
        raw_vector_count *= len(parameter.domain)
    if raw_vector_count != 108 or len(universe) != 95:
        failures.append("controlled grid did not retain 95 unique non-base semantics")
    if len({item.semantic_sha256 for item in universe}) != len(universe):
        failures.append("controlled universe retained a semantic duplicate")
    if len({item.vector.canonical_bytes for item in universe}) != len(universe):
        failures.append("controlled universe retained a duplicate canonical vector")
    if any(
        item.vector.canonical_bytes.endswith(b"\n")
        or not item.vector.canonical_bytes.startswith(b"[[\"")
        for item in universe
    ):
        failures.append("canonical vectors are not exact no-LF compact JSON arrays")
    if _normalized_manifest_payload(bounded.payload) != _normalized_manifest_payload(
        no_winner.payload
    ):
        failures.append("no-winner fixture changed a preregistered search threshold")
    if bounded.search_space.parameters != no_winner.search_space.parameters:
        failures.append("example manifests do not bind the same controlled search space")
    if bounded.policy is not SearchPolicyV1.GRID or no_winner.policy is not SearchPolicyV1.GRID:
        failures.append("committed examples do not each select exactly GRID")
    if bounded.budget != 64 or no_winner.budget != 64:
        failures.append("committed examples do not bind budget 64")
    hostile_manifests = (
        bounded_raw.replace(b"budget = 64", b"budget = 65", 1),
        bounded_raw.replace(b"statistic_min = 30000", b"statistic_min = 29999", 1),
        bounded_raw.replace(
            b"schema_version = 1\n",
            b"schema_version = 1\nunexpected = true\n",
            1,
        ),
    )
    hostile_refusals = 0
    for raw in hostile_manifests:
        try:
            StrategySearchManifestV1.from_toml_bytes(raw)
        except (TypeError, ValueError):
            hostile_refusals += 1
    try:
        bounded.payload["budget"] = 1  # type: ignore[index]
    except TypeError:
        hostile_refusals += 1
    if hostile_refusals != 4:
        failures.append("search manifest mutation or threshold tampering was admitted")
    fixture_projection = {
        "bounded_manifest_sha256": bounded.manifest_sha256,
        "base_source_sha256": CONTROLLED_BASE_SOURCE_SHA256_V1,
        "no_winner_manifest_sha256": no_winner.manifest_sha256,
        "parameter_paths": [item.path for item in space.parameters],
        "raw_vectors": raw_vector_count,
        "retained_non_base": len(universe),
        "hostile_refusals": hostile_refusals,
        "vector_order": STRATEGY_VECTOR_ORDER_V1,
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(fixture_projection)
    ).hexdigest()
    if fixture_sha256 != WO35D_MANIFEST_FIXTURE_SHA256:
        failures.append("WO35-D manifests differ from their frozen fixture digest")
    return StrategyDiscoveryAuditCase(
        "d_manifests_preregister_the_exact_bounded_protocol",
        (
            f"fixture_sha256={fixture_sha256} bounded_sha256={bounded.manifest_sha256} "
            f"no_winner_sha256={no_winner.manifest_sha256} raw_vectors={raw_vector_count} "
            f"valid_including_base={len(universe) + 1} non_base={len(universe)} "
            f"hostile_refusals={hostile_refusals}/4 policy=GRID budget=64 "
            "real_partition_execution=FORBIDDEN"
        ),
        tuple(failures),
    )


def _search_policy_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    bounded, _ = _load_search_examples()
    rows: list[dict[str, object]] = []
    for policy in SearchPolicyV1:
        first = run_development_search(bounded, policy=policy)
        repeated = run_development_search(bounded, policy=policy)
        if first.run_sha256 != repeated.run_sha256 or first.as_dict() != repeated.as_dict():
            failures.append(f"{policy.value} changed across identical repeated searches")
        identities = tuple(item.candidate.semantic_sha256 for item in first.evaluated)
        if len(identities) != len(set(identities)):
            failures.append(f"{policy.value} evaluated a semantic duplicate")
        if not 1 <= len(first.evaluated) <= first.effective_budget <= 64:
            failures.append(f"{policy.value} violated its effective budget")
        if first.outcome is not SearchOutcomeV1.CANDIDATE_SELECTED:
            failures.append(f"{policy.value} did not complete controlled selection")
        bounded_seven = run_development_search(
            bounded,
            policy=policy,
            cli_budget=7,
        )
        if bounded_seven.effective_budget != 7 or len(bounded_seven.evaluated) > 7:
            failures.append(f"{policy.value} ignored the smaller CLI budget")
        rows.append(
            {
                "evaluated": len(first.evaluated),
                "outcome": first.outcome.value,
                "policy": policy.value,
                "run_sha256": first.run_sha256,
                "selected": first.selected_semantic_sha256,
                "stop_reason": first.stop_reason.value,
                "trace": list(first.policy_trace),
            }
        )
    fixture_sha256 = hashlib.sha256(canonical_identity_bytes(rows)).hexdigest()
    if fixture_sha256 != WO35D_POLICY_FIXTURE_SHA256:
        failures.append("WO35-D policy results differ from the frozen fixture digest")
    counts = ",".join(f"{item['policy']}={item['evaluated']}" for item in rows)
    return StrategyDiscoveryAuditCase(
        "d_all_five_policies_are_repeatable_unique_and_budget_bounded",
        (
            f"policy_fixture_sha256={fixture_sha256} policies={len(rows)} "
            f"evaluations={counts} repeated=IDENTICAL cli_budget_7=ENFORCED "
            "combined_meta_search=ABSENT"
        ),
        tuple(failures),
    )


def _search_objective_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    required_ids = tuple(item.objective_id for item in REQUIRED_OBJECTIVE_SPECS_V1)
    all_ids = tuple(item.objective_id for item in ALL_OBJECTIVE_SPECS_V1)
    weight_sum = sum(item.weight for item in REQUIRED_OBJECTIVE_SPECS_V1)
    complexity = StrategyComplexityV1(1, 2, 3, 4, 5, 6)
    median, mad = median_and_mad((1, 2, 100, 101))
    objective_values = tuple(
        ObjectiveValueV1(
            item.objective_id,
            ObjectiveApplicabilityV1.APPLICABLE,
            500_000 if item.objective_id is not StrategyObjectiveIdV1.PNL else 1_000_000,
        )
        for item in ALL_OBJECTIVE_SPECS_V1
    )
    composite = root_composite(objective_values)
    tie_a = common_tie_digest(
        context_id="WO35/TRAINING_FINALISTS",
        semantic_sha256="01" * 32,
    )
    tie_b = common_tie_digest(
        context_id="WO35/TRAINING_FINALISTS",
        semantic_sha256="01" * 32,
    )
    utility_fixture = {
        "balanced_classification": balanced_classification_utility(
            correct_green=4,
            reference_green=4,
            correct_wait=3,
            reference_wait=3,
            correct_red=2,
            reference_red=2,
        ),
        "completion": completion_utility(
            completed_shares=50,
            objective_shares=100,
        ),
        "discipline": discipline_compatibility_utility(
            violations=1,
            eligible=4,
        ),
        "false_green": false_green_utility(false_green=1, non_green=4),
        "missed_opportunity": missed_opportunity_utility(
            missed=1,
            true_opportunities=4,
        ),
        "opportunity": execution_opportunity_utility(
            true_positive=1,
            predicted_green_allow=2,
            true_opportunities=4,
        ),
        "signed_cost": signed_cost_utility(2_500),
        "stability": cross_cell_stability_utility((400_000, 600_000)),
        "turnover_complete": turnover_utility(
            traded_shares=200,
            objective_shares=100,
        ),
        "turnover_cap": turnover_utility(
            traded_shares=2_000,
            objective_shares=100,
        ),
    }
    if len(required_ids) != 11 or len(all_ids) != 12 or len(set(all_ids)) != 12:
        failures.append("objective inventory is incomplete or duplicated")
    if weight_sum != 1_000_000 or ALL_OBJECTIVE_SPECS_V1[-1].weight != 0:
        failures.append("non-P&L weights do not sum to S or P&L is weighted")
    if complexity_points(complexity) != 74:
        failures.append("six-dimensional complexity coefficients changed")
    if (median, mad) != (2, 1):
        failures.append("nearest-rank P50 or MAD arithmetic changed")
    if multiplicity_penalty(64) != 35_000:
        failures.append("budget-64 multiplicity penalty is not 35000")
    if composite != 500_000:
        failures.append("zero-weight P&L changed the weighted root composite")
    if tie_a != tie_b or len(tie_a) != 32:
        failures.append("common tie digest is not stable SHA-256 bytes")
    if not materially_equivalent(100_000, 129_999) or materially_equivalent(
        100_000,
        130_000,
    ):
        failures.append("minimum practical-effect boundary changed")
    if utility_fixture != {
        "balanced_classification": 1_000_000,
        "completion": 500_000,
        "discipline": 750_000,
        "false_green": 750_000,
        "missed_opportunity": 750_000,
        "opportunity": 333_333,
        "signed_cost": 500_000,
        "stability": 800_000,
        "turnover_complete": 1_000_000,
        "turnover_cap": 0,
    }:
        failures.append("an exact section-5.7.6 utility formula changed")
    projection = {
        "all_ids": [item.value for item in all_ids],
        "complexity_fixture": complexity.as_dict(),
        "complexity_points": complexity_points(complexity),
        "material_equivalence": [True, False],
        "median": median,
        "mad": mad,
        "multiplicity_64": multiplicity_penalty(64),
        "objective_protocol": objective_protocol_projection(),
        "root_composite": composite,
        "tie_digest": tie_a.hex(),
        "utility_fixture": utility_fixture,
        "weight_sum": weight_sum,
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(projection)
    ).hexdigest()
    if fixture_sha256 != WO35D_OBJECTIVE_FIXTURE_SHA256:
        failures.append("WO35-D objective arithmetic differs from its frozen fixture")
    return StrategyDiscoveryAuditCase(
        "d_objectives_uncertainty_multiplicity_and_complexity_are_exact",
        (
            f"objective_fixture_sha256={fixture_sha256} required={len(required_ids)} "
            f"optional_pnl_weight=0 weight_sum={weight_sum} median={median} mad={mad} "
            f"multiplicity_n64={multiplicity_penalty(64)} complexity_points=74 "
            "utility_formulas=10/10 material_equivalence_prefers=SIMPLER"
        ),
        tuple(failures),
    )


def _search_access_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    bounded, _ = _load_search_examples()
    candidates = bounded.search_space.universe()[:2]
    oracle = DevelopmentSyntheticScoreOracleV1(
        mode=SyntheticOracleModeV1.CONTROLLED,
        compatibility=bounded.compatibility,
        train_budget=1,
    )
    refusal_codes: list[str] = []

    def refuse(call: object) -> None:
        try:
            call()  # type: ignore[operator]
        except EvaluationAccessError as error:
            refusal_codes.append(error.code)
        else:
            failures.append("a forbidden synthetic-oracle access was admitted")

    first, second = candidates
    common_first = {
        "candidate_id": first.candidate_id,
        "semantic_sha256": first.semantic_sha256,
        "vector_values": first.oracle_values,
        "complexity_points": first.complexity_points,
    }
    common_second = {
        "candidate_id": second.candidate_id,
        "semantic_sha256": second.semantic_sha256,
        "vector_values": second.oracle_values,
        "complexity_points": second.complexity_points,
    }
    refuse(
        lambda: oracle.evaluate(
            **common_first,
            partition=StrategyPartitionV1.VALIDATION,
        )
    )
    oracle.evaluate(**common_first, partition=StrategyPartitionV1.TRAIN)
    refuse(
        lambda: oracle.evaluate(
            **common_second,
            partition=StrategyPartitionV1.TRAIN,
        )
    )
    oracle.freeze_validation((first.semantic_sha256,))
    refuse(
        lambda: oracle.evaluate(
            **common_first,
            partition=StrategyPartitionV1.HOLDOUT,
        )
    )
    refuse(
        lambda: oracle.evaluate(
            **common_first,
            partition=StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        )
    )
    refuse(
        lambda: oracle.evaluate(
            **common_first,
            partition=StrategyPartitionV1.ROBUSTNESS,
        )
    )
    refuse(
        lambda: oracle.evaluate(
            **common_second,
            partition=StrategyPartitionV1.VALIDATION,
        )
    )
    refuse(
        lambda: oracle.evaluate(
            **common_second,
            partition=StrategyPartitionV1.TRAIN,
        )
    )
    validation = oracle.evaluate(
        **common_first,
        partition=StrategyPartitionV1.VALIDATION,
    )
    refuse(lambda: oracle.freeze_validation((first.semantic_sha256,)))
    incompatible = replace(
        validation,
        compatibility=EvidenceCompatibilityKeyV1(
            "OTHER_SCENARIO_GROUP_V1",
            validation.compatibility.objective_group_id,
            validation.compatibility.evidence_group_id,
        ),
    )
    try:
        require_compatible_evidence((validation, incompatible))
    except ValueError:
        comparison_refused = True
    else:
        comparison_refused = False
        failures.append("incompatible scenario/objective/evidence groups were compared")
    expected_codes = (
        "VALIDATION_BEFORE_FINALIST_FREEZE",
        "TRAIN_BUDGET_EXHAUSTED",
        "REAL_PARTITION_FORBIDDEN",
        "REAL_PARTITION_FORBIDDEN",
        "REAL_PARTITION_FORBIDDEN",
        "VALIDATION_NON_FINALIST",
        "TRAINING_AFTER_FINALIST_FREEZE",
        "VALIDATION_ALREADY_FROZEN",
    )
    if tuple(refusal_codes) != expected_codes:
        failures.append("search access refusal codes or ordering changed")
    if (
        oracle.train_evaluation_count != 1
        or oracle.validation_evaluation_count != 1
        or oracle.real_partition_access_count != 0
    ):
        failures.append("synthetic oracle access accounting changed")
    projection = {
        "access_log": [list(item) for item in oracle.access_log],
        "comparison_refused": comparison_refused,
        "real_partition_access_count": oracle.real_partition_access_count,
        "refusal_codes": refusal_codes,
        "train_count": oracle.train_evaluation_count,
        "validation_count": oracle.validation_evaluation_count,
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(projection)
    ).hexdigest()
    if fixture_sha256 != WO35D_ACCESS_FIXTURE_SHA256:
        failures.append("WO35-D access boundary differs from its frozen fixture")
    return StrategyDiscoveryAuditCase(
        "d_budget_validation_and_real_partition_access_fail_closed",
        (
            f"access_fixture_sha256={fixture_sha256} refusals={len(refusal_codes)} "
            "train_first_time=1/1 validation=AFTER_FREEZE_ONLY "
            "incompatible_comparison=REFUSED real_partition_access_count=0"
        ),
        tuple(failures),
    )


def _search_no_winner_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    bounded, no_winner = _load_search_examples()
    first = run_development_search(no_winner)
    repeated = run_development_search(no_winner)
    controlled = run_development_search(bounded)
    if first.run_sha256 != repeated.run_sha256:
        failures.append("no-winner search changed across identical repetitions")
    if (
        first.outcome is not SearchOutcomeV1.NO_CANDIDATE_MET_CRITERIA
        or first.selected_semantic_sha256 is not None
        or any(item.qualification.qualified for item in first.finalists)
    ):
        failures.append("no-winner fixture selected or qualified a candidate")
    if first.run_sha256 != WO35D_NO_WINNER_RUN_SHA256:
        failures.append("no-winner run differs from its frozen result digest")
    if _normalized_manifest_payload(bounded.payload) != _normalized_manifest_payload(
        no_winner.payload
    ):
        failures.append("no-winner completion changed a threshold or protocol field")
    if (
        controlled.training_star_semantic_sha256
        == controlled.selected_semantic_sha256
        or controlled.finalists[0].qualification.qualified
    ):
        failures.append("controlled training star did not remain distinct and fail validation")
    if (
        len(first.evaluated) != 64
        or len(first.finalists) != 8
        or first.real_partition_access_count != 0
    ):
        failures.append("no-winner fixture did not close at the declared budget/access bounds")
    return StrategyDiscoveryAuditCase(
        "d_no_candidate_is_a_terminal_success_without_threshold_relaxation",
        (
            f"run_sha256={first.run_sha256} evaluated={len(first.evaluated)}/64 "
            f"finalists={len(first.finalists)} qualified=0 outcome={first.outcome.value} "
            "threshold_changes=0 training_star_validation=FAILED "
            "real_partition_access_count=0"
        ),
        tuple(failures),
    )


def audit_wo35d_strategy_search() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _search_manifest_case(),
        _search_policy_case(),
        _search_objective_case(),
        _search_access_case(),
        _search_no_winner_case(),
    )


def _robustness_perturbation_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    candidate_environment = controlled_robustness_environment(candidate=True)
    base_environment = controlled_robustness_environment(candidate=False)
    candidate = build_robustness_probes(candidate_environment)
    base = build_robustness_probes(base_environment)
    family_counts = tuple(
        (
            family.value,
            sum(item.family is family for item in ROBUSTNESS_SETTINGS_V1),
        )
        for family in RobustnessFamilyV1
    )
    if len(candidate) != 17 or len(base) != 17:
        failures.append("robustness registry does not contain 16 settings plus venue N/A")
    for candidate_probe, base_probe in zip(candidate, base, strict=True):
        if (
            candidate_probe.setting != base_probe.setting
            or candidate_probe.status is not base_probe.status
            or candidate_probe.changed_paths != base_probe.changed_paths
        ):
            failures.append("candidate and base did not receive the same one-factor probe")
    if tuple(count for _, count in family_counts) != (4, 2, 2, 2, 2, 2, 2, 1):
        failures.append("robustness family setting counts changed")
    if any(
        item.status is not PerturbationStatusV1.APPLIED
        for item in candidate[:-1]
    ) or candidate[-1].status is not PerturbationStatusV1.NOT_APPLICABLE:
        failures.append("mandatory families or single-venue declaration changed status")
    threshold_paths = (
        "/green/0/threshold_ticks",
        "/green/1/threshold_ppm",
        "/wait/0/threshold_ticks",
    )
    if any(item.changed_paths != threshold_paths for item in candidate[:4]):
        failures.append("threshold robustness changed a duration or omitted a condition")
    if any(item.changed_paths != ("/window_us",) for item in candidate[4:6]):
        failures.append("rolling-window robustness changed more than the window")
    latency_values = tuple(
        (
            item.environment.decision_latency_us,
            item.environment.routing_latency_us,
        )
        for item in candidate[6:8]
        if item.environment is not None
    )
    if latency_values != ((251, 0), (1001, 0)):
        failures.append("latency robustness did not preserve zero routing latency")
    latency_timings = tuple(
        derive_execution_timing(
            decision_time_us=10_000_000,
            decision_latency_us=item.environment.decision_latency_us,
            routing_latency_us=item.environment.routing_latency_us,
            filled_entry_quantity=100,
        )
        for item in candidate[6:8]
        if item.environment is not None
    )
    if tuple(
        (item.entry_arrival_us, item.cancellation_us, item.exit_arrival_us)
        for item in latency_timings
    ) != (
        (10_000_251, 12_000_251, 12_000_252),
        (10_001_001, 12_001_001, 12_001_002),
    ) or derive_execution_timing(
        decision_time_us=10_000_000,
        decision_latency_us=1,
        routing_latency_us=0,
        filled_entry_quantity=0,
    ).exit_arrival_us is not None:
        failures.append("latency robustness did not derive exact entry/cancel/exit times")
    rebate_environment = replace(
        base_environment,
        maker_fee_milliticks_per_share=-500,
        taker_fee_milliticks_per_share=100,
    )
    fee_plus_250 = next(
        item
        for item in ROBUSTNESS_SETTINGS_V1
        if item.setting_id == "FEES_PLUS_250"
    )
    rebate_probe = apply_robustness_setting(rebate_environment, fee_plus_250)
    if (
        rebate_probe.environment is None
        or rebate_probe.environment.maker_fee_milliticks_per_share != -250
        or rebate_probe.environment.taker_fee_milliticks_per_share != 350
    ):
        failures.append("fee robustness did not make a rebate less favorable")
    if any(
        item.environment is None
        or item.environment.liquidity != candidate_environment.liquidity
        for item in candidate[10:12]
    ):
        failures.append("volume robustness changed the liquidity vector")
    if any(
        item.environment is None
        or item.environment.volume != candidate_environment.volume
        or item.environment.liquidity.cancellation_rate_ppm
        != candidate_environment.liquidity.cancellation_rate_ppm
        or item.environment.liquidity.placement_depth_offset_ticks
        != candidate_environment.liquidity.placement_depth_offset_ticks
        for item in candidate[12:14]
    ):
        failures.append("liquidity robustness changed an excluded vector field")
    weak_regime = replace(
        base_environment,
        regime_rows=(
            RegimeProbabilityRowV1(
                "WEAK",
                (
                    RegimeWeightV1("DONOR", 200_000),
                    RegimeWeightV1("RECEIVER", 800_000),
                ),
            ),
        ),
    )
    min_to_max = next(
        item
        for item in ROBUSTNESS_SETTINGS_V1
        if item.setting_id == "REGIME_MIN_TO_MAX"
    )
    donor_refusal = apply_robustness_setting(weak_regime, min_to_max)
    if (
        donor_refusal.status is not PerturbationStatusV1.INSUFFICIENT_EVIDENCE
        or donor_refusal.environment is not None
    ):
        failures.append("at-most-200000 regime donor did not fail the whole setting")
    threshold_110 = next(
        item
        for item in ROBUSTNESS_SETTINGS_V1
        if item.setting_id == "THRESHOLD_1100000"
    )
    window_120 = next(
        item
        for item in ROBUSTNESS_SETTINGS_V1
        if item.setting_id == "ROLLING_WINDOW_1200000"
    )
    threshold_bound_refusal = apply_robustness_setting(
        replace(
            base_environment,
            green_spread_ticks=5,
            green_imbalance_ppm=500_000,
            wait_spread_ticks=10,
        ),
        threshold_110,
    )
    window_bound_refusal = apply_robustness_setting(
        replace(base_environment, window_us=20_000_000),
        window_120,
    )
    if any(
        item.status is not PerturbationStatusV1.INVALID
        or item.environment is not None
        for item in (threshold_bound_refusal, window_bound_refusal)
    ):
        failures.append("out-of-bound threshold/window robustness was clamped")
    venue = candidate[-1]
    if venue.reason != "SINGLE_VENUE_CONTROLLED_SOURCE_V1" or venue.changed_paths:
        failures.append("venue N/A was not capability-declared without a synthetic probe")
    projection = {
        "base": [item.as_dict() for item in base],
        "candidate": [item.as_dict() for item in candidate],
        "donor_refusal": donor_refusal.as_dict(),
        "family_counts": [list(item) for item in family_counts],
        "latency_timings": [item.as_dict() for item in latency_timings],
        "latency_values": [list(item) for item in latency_values],
        "rebate_probe": rebate_probe.as_dict(),
        "threshold_bound_refusal": threshold_bound_refusal.as_dict(),
        "window_bound_refusal": window_bound_refusal.as_dict(),
    }
    fixture_sha256 = hashlib.sha256(canonical_identity_bytes(projection)).hexdigest()
    if fixture_sha256 != WO35E_PERTURBATION_FIXTURE_SHA256:
        failures.append("WO35-E one-factor perturbations differ from their frozen fixture")
    return StrategyDiscoveryAuditCase(
        "e_one_factor_perturbations_and_single_venue_capability_are_exact",
        (
            f"fixture_sha256={fixture_sha256} settings=16 roots=4 expected_cells=64 "
            f"families={','.join(f'{name}:{count}' for name, count in family_counts)} "
            "paired_paths=IDENTICAL latency=251,1001 routing_zero=PRESERVED "
            "entry_cancel_exit=EXACT rebate=LESS_FAVORABLE "
            "bounds=REFUSED_WITHOUT_CLAMP "
            "regime_donor_at_200000=INSUFFICIENT_EVIDENCE venue_mix=NOT_APPLICABLE"
        ),
        tuple(failures),
    )


def _robustness_qualification_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    modes = tuple(SyntheticRobustnessModeV1)
    evidence = tuple(build_synthetic_robustness_evidence(item) for item in modes)
    decisions = tuple(qualify_robustness(item) for item in evidence)
    expected_outcomes = (
        RobustnessOutcomeV1.PASSED,
        RobustnessOutcomeV1.INSUFFICIENT_EVIDENCE,
        RobustnessOutcomeV1.INSUFFICIENT_EVIDENCE,
        RobustnessOutcomeV1.EXPERIMENT_INVALID,
    )
    if tuple(item.outcome for item in decisions) != expected_outcomes:
        failures.append("robustness scientific and invalid outcomes changed")
    passing = evidence[0]
    passing_decision = decisions[0]
    if (
        tuple(item.family for item in passing.families)
        != MANDATORY_ROBUSTNESS_FAMILIES_V1
        or sum(len(item.cells) for item in passing.families)
        != ROBUSTNESS_EXPECTED_CELL_COUNT_V1
        or tuple(
            (cell.root_seed, cell.setting_id)
            for family in passing.families
            for cell in family.cells
        )
        != tuple(
            (root, setting.setting_id)
            for family in MANDATORY_ROBUSTNESS_FAMILIES_V1
            for root in ROBUSTNESS_ROOTS_V1
            for setting in ROBUSTNESS_SETTINGS_V1
            if setting.family is family
        )
    ):
        failures.append("robustness evidence is not seven families and 64 ordered cells")
    for family in passing.families:
        medians = family.component_medians()
        if (
            medians[StrategyObjectiveIdV1.BALANCED_CLASSIFICATION] != 10_000
            or medians[StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY] != 10_000
            or StrategyObjectiveIdV1.CROSS_CELL_STABILITY in medians
            or len(medians) != 10
        ):
            failures.append("robustness family pooling or component inventory changed")
    first_family = passing.families[0]
    pooling_tamper_refused = _raises(
        lambda: replace(
            first_family,
            cells=(
                replace(
                    first_family.cells[0],
                    composite_delta=first_family.cells[0].composite_delta + 1,
                ),
                *first_family.cells[1:],
            ),
        ),
        ValueError,
    )
    if not pooling_tamper_refused:
        failures.append("robustness admitted a composite that bypassed pooled utilities")
    if (
        passing_decision.nonnegative_family_count != 7
        or passing_decision.minimum_cell < -75_000
    ):
        failures.append("passing robustness reduction changed")
    if not any("UNAVAILABLE_OBSERVATION" in item for item in decisions[2].reasons):
        failures.append("unavailable robustness observation was not insufficient evidence")
    if not any("REPLAY_INVALID" in item for item in decisions[3].reasons):
        failures.append("replay-invalid robustness cell was not experiment-invalid")
    projection = {
        "decisions": [item.as_dict() for item in decisions],
        "evidence_sha256": [item.evidence_sha256 for item in evidence],
        "family_component_ids": [
            sorted(item.value for item in family.component_medians())
            for family in passing.families
        ],
        "mode_order": [item.value for item in modes],
        "pooling_tamper_refused": pooling_tamper_refused,
    }
    fixture_sha256 = hashlib.sha256(canonical_identity_bytes(projection)).hexdigest()
    if fixture_sha256 != WO35E_ROBUSTNESS_FIXTURE_SHA256:
        failures.append("WO35-E robustness qualification differs from its frozen fixture")
    return StrategyDiscoveryAuditCase(
        "e_robustness_pools_exactly_and_fails_closed_by_failure_class",
        (
            f"fixture_sha256={fixture_sha256} families=7 cells=64 "
            "classification_opportunity=FAMILY_POOLED pooling_bypass=REFUSED "
            "stability=OMITTED "
            "pass=PASSED brittle=INSUFFICIENT_EVIDENCE "
            "unavailable=INSUFFICIENT_EVIDENCE replay=EXPERIMENT_INVALID"
        ),
        tuple(failures),
    )


def _observability_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    observation = ObservableDecisionInputV1(
        "wo35e-observable-decision",
        3_505_000,
        1_000,
        _digest("wo35e/observable-cut"),
        ObservationStatusV1.AVAILABLE,
        2,
        200_000,
        1_000_000,
        8,
        3,
        0,
        0,
    )
    label = bind_reference_decision_label(
        label_id=observation.decision_id,
        root_seed=observation.root_seed,
        decision_time_us=observation.decision_time_us,
        reference_state=CandidateSignalV1.GREEN,
        opportunity=True,
        source_event_ids=("source-event-0001", "source-event-0002"),
        oracle_sha256=_digest("wo35e/reference-oracle"),
    )
    projection = project_candidate_decision(
        observation,
        label,
        candidate_state=CandidateSignalV1.GREEN,
        permission=CandidatePermissionV1.ALLOW,
    )
    scored = score_candidate_decision(projection, label)

    def make_observation(
        decision_id: str,
        root_seed: int,
        decision_time_us: int,
    ) -> ObservableDecisionInputV1:
        return ObservableDecisionInputV1(
            decision_id,
            root_seed,
            decision_time_us,
            _digest(f"wo35e/{decision_id}/cut"),
            ObservationStatusV1.AVAILABLE,
            3,
            0,
            900_000,
            3,
            4,
            0,
            0,
        )

    wait_observation = make_observation("wo35e-wait-decision", 3_505_001, 2_000)
    wait_label = bind_reference_decision_label(
        label_id=wait_observation.decision_id,
        root_seed=wait_observation.root_seed,
        decision_time_us=wait_observation.decision_time_us,
        reference_state=CandidateSignalV1.WAIT,
        opportunity=False,
        source_event_ids=("source-event-0010",),
        oracle_sha256=_digest("wo35e/reference-oracle"),
    )
    wait_projection = project_candidate_decision(
        wait_observation,
        wait_label,
        candidate_state=CandidateSignalV1.WAIT,
        permission=CandidatePermissionV1.DENY,
    )
    red_observation = make_observation("wo35e-red-decision", 3_505_002, 3_000)
    red_label = bind_reference_decision_label(
        label_id=red_observation.decision_id,
        root_seed=red_observation.root_seed,
        decision_time_us=red_observation.decision_time_us,
        reference_state=CandidateSignalV1.RED,
        opportunity=False,
        source_event_ids=("source-event-0020",),
        oracle_sha256=_digest("wo35e/reference-oracle"),
    )
    red_projection = project_candidate_decision(
        red_observation,
        red_label,
        candidate_state=CandidateSignalV1.GREEN,
        permission=CandidatePermissionV1.ALLOW,
    )
    discipline = summarize_discipline(
        (projection, wait_projection, red_projection)
    )
    zero_eligible = summarize_discipline((projection,))

    def recursive_keys(value: object) -> frozenset[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                keys.add(str(key))
                keys.update(recursive_keys(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                keys.update(recursive_keys(child))
        return frozenset(keys)

    leaked = recursive_keys(projection.as_dict()).intersection(
        FORBIDDEN_REFERENCE_FIELDS_V1
    )
    if leaked or tuple(name for name, _ in observation.feature_values) != OBSERVABLE_FEATURE_NAMES_V1:
        failures.append("candidate projection contains truth or differs from its whitelist")
    if not (
        scored.classification_correct
        and not scored.false_green
        and not scored.missed_opportunity
    ):
        failures.append("post-projection reference scoring changed")
    if (
        wait_projection.discipline_eligibility is not DisciplineEligibilityV1.ELIGIBLE
        or wait_projection.discipline_violation
        or wait_projection.discipline_reason is not DisciplineReasonV1.NONE
        or red_projection.discipline_eligibility is not DisciplineEligibilityV1.ELIGIBLE
        or not red_projection.discipline_violation
        or red_projection.discipline_reason is not DisciplineReasonV1.ACTED_DURING_RED
        or discipline.status is not DisciplineEvidenceStatusV1.MEASURED
        or discipline.eligible_decisions != 2
        or discipline.violations != 1
        or discipline.utility != 500_000
        or zero_eligible.status
        is not DisciplineEvidenceStatusV1.INSUFFICIENT_EVIDENCE
        or zero_eligible.utility is not None
    ):
        failures.append("typed discipline eligibility, violation, or zero-denominator rule changed")
    unavailable = ObservableDecisionInputV1(
        "wo35e-unavailable-decision",
        3_505_003,
        2_000,
        _digest("wo35e/unavailable-cut"),
        ObservationStatusV1.UNAVAILABLE,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    unavailable_refused = _raises(
        lambda: project_candidate_decision(
            unavailable,
            label,
            candidate_state=CandidateSignalV1.WAIT,
            permission=CandidatePermissionV1.DENY,
        ),
        ObservationUnavailableError,
    )
    missing_label_refused = _raises(
        lambda: project_candidate_decision(
            observation,
            None,
            candidate_state=CandidateSignalV1.GREEN,
            permission=CandidatePermissionV1.ALLOW,
        ),
        MissingReferenceLabelError,
    )
    hidden_injection_refused = _raises(
        lambda: CandidateDecisionProjectionV1(
            decision_id=projection.decision_id,
            label_id=projection.label_id,
            root_seed=projection.root_seed,
            decision_time_us=projection.decision_time_us,
            observable_cut_sha256=projection.observable_cut_sha256,
            candidate_state=projection.candidate_state,
            permission=projection.permission,
            discipline_eligibility=projection.discipline_eligibility,
            discipline_violation=projection.discipline_violation,
            discipline_reason=projection.discipline_reason,
            frozen_at_us=projection.frozen_at_us,
            reference_state=CandidateSignalV1.GREEN,  # type: ignore[call-arg]
        ),
        TypeError,
    )
    label_tamper_refused = _raises(
        lambda: replace(label, label_sha256="0" * 64),
        ValueError,
    )
    divergence = EndogenousDivergenceRecordV1(
        3_505_000,
        _digest("wo35e/base-execution"),
        _digest("wo35e/candidate-execution"),
        observation.observable_cut_sha256,
        15_000,
    )
    superiority_refused = _raises(
        lambda: replace(divergence, real_market_superiority=True),
        ValueError,
    )
    if not unavailable_refused:
        failures.append("unavailable decision observation did not fail closed")
    if not missing_label_refused:
        failures.append("missing immutable reference label did not fail insufficient")
    if not hidden_injection_refused or not label_tamper_refused:
        failures.append("truth-only field injection reached the candidate projection")
    if (
        not superiority_refused
        or divergence.claim_scope != ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1
        or divergence.real_market_superiority
    ):
        failures.append("simulator divergence was allowed to overstate real-market evidence")
    fixture_projection = {
        "divergence": divergence.as_dict(),
        "forbidden_fields": sorted(FORBIDDEN_REFERENCE_FIELDS_V1),
        "hidden_injection_refused": hidden_injection_refused,
        "label": label.as_dict(),
        "label_tamper_refused": label_tamper_refused,
        "missing_label_refused": missing_label_refused,
        "projection": projection.as_dict(),
        "projection_sha256": projection.projection_sha256,
        "scored": {
            "classification_correct": scored.classification_correct,
            "false_green": scored.false_green,
            "missed_opportunity": scored.missed_opportunity,
        },
        "discipline": {
            "eligible_decisions": discipline.eligible_decisions,
            "status": discipline.status.value,
            "utility": discipline.utility,
            "violations": discipline.violations,
            "zero_eligible_status": zero_eligible.status.value,
        },
        "unavailable_refused": unavailable_refused,
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(fixture_projection)
    ).hexdigest()
    if fixture_sha256 != WO35E_OBSERVABILITY_FIXTURE_SHA256:
        failures.append("WO35-E observability boundary differs from its frozen fixture")
    return StrategyDiscoveryAuditCase(
        "e_decision_projection_excludes_truth_and_unavailable_inputs_fail_closed",
        (
            f"fixture_sha256={fixture_sha256} observable_features={len(observation.feature_values)} "
            f"forbidden_fields={len(FORBIDDEN_REFERENCE_FIELDS_V1)} leaks={len(leaked)} "
            "unavailable=REFUSED missing_label=INSUFFICIENT_EVIDENCE "
            "discipline=2_ELIGIBLE_1_VIOLATION zero_eligible=INSUFFICIENT_EVIDENCE "
            "hidden_injection=REFUSED label_tamper=REFUSED "
            "endogenous_claim=SIMULATOR_COUNTERFACTUAL_ONLY"
        ),
        tuple(failures),
    )


def _overfit_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    fixture = build_development_overfit_fixture()
    derived_threshold = threshold_evidence_from_robustness(
        build_synthetic_robustness_evidence(SyntheticRobustnessModeV1.PASS)
    )
    expected_fixture_labels = (
        OverfitLabelV1.TRAIN_VALIDATION_DIVERGENCE,
        OverfitLabelV1.ONE_SEED_DEPENDENCE,
        OverfitLabelV1.ONE_SCENARIO_DEPENDENCE,
    )
    if (
        fixture.train.deltas != (100_000, 100_000, 100_000, 100_000)
        or fixture.validation.deltas != (600_000, -20_000, -20_000, -20_000)
        or fixture.assessment.labels != expected_fixture_labels
        or not fixture.assessment.rejected
    ):
        failures.append("development training-star fixture was not labeled and rejected")
    if tuple(item.setting_id for item in derived_threshold.settings) != tuple(
        item.setting_id
        for item in ROBUSTNESS_SETTINGS_V1
        if item.family is RobustnessFamilyV1.THRESHOLD
    ):
        failures.append("overfit threshold medians did not derive from robustness cells")
    threshold_ids = tuple(
        item.setting_id
        for item in ROBUSTNESS_SETTINGS_V1
        if item.family is RobustnessFamilyV1.THRESHOLD
    )
    signed_threshold = ThresholdSensitivityEvidenceV1(
        fixture.train.candidate_semantic_sha256,
        tuple(
            ThresholdSettingMedianV1(setting_id, value)
            for setting_id, value in zip(
                threshold_ids,
                (-1, 0, 0, 1),
                strict=True,
            )
        ),
    )
    ranged_threshold = ThresholdSensitivityEvidenceV1(
        fixture.train.candidate_semantic_sha256,
        tuple(
            ThresholdSettingMedianV1(setting_id, value)
            for setting_id, value in zip(
                threshold_ids,
                (1, 2, 3, 100_002),
                strict=True,
            )
        ),
    )
    zero_positive = OverfitPartitionEvidenceV1(
        fixture.train.candidate_semantic_sha256,
        StrategyPartitionV1.VALIDATION,
        (
            OverfitCellV1(1, "A", 0),
            OverfitCellV1(2, "B", -1),
            OverfitCellV1(3, "B", -2),
        ),
        40,
        40,
        0,
    )
    if one_seed_dependence(zero_positive) or one_scenario_dependence(zero_positive):
        failures.append("zero positive denominator fabricated dependence")
    candidate = fixture.train.candidate_semantic_sha256

    def partition_evidence(
        partition: StrategyPartitionV1,
        *,
        root_start: int,
        candidate_trades: int,
        base_trades: int,
        false_green_delta: int,
    ) -> OverfitPartitionEvidenceV1:
        deltas = (600_000, -20_000, -20_000, -20_000, -20_000, -20_000, -20_000, -20_000)
        families = ("SOLE_POSITIVE_FAMILY",) + ("CONTROL_FAMILY",) * 7
        return OverfitPartitionEvidenceV1(
            candidate,
            partition,
            tuple(
                OverfitCellV1(root_start + index, family, delta)
                for index, (family, delta) in enumerate(
                    zip(families, deltas, strict=True)
                )
            ),
            candidate_trades,
            base_trades,
            false_green_delta,
        )

    holdout_suppressed = partition_evidence(
        StrategyPartitionV1.HOLDOUT,
        root_start=3_503_000,
        candidate_trades=20,
        base_trades=40,
        false_green_delta=0,
    )
    holdout_excessive = partition_evidence(
        StrategyPartitionV1.HOLDOUT,
        root_start=3_503_000,
        candidate_trades=81,
        base_trades=50,
        false_green_delta=-20_001,
    )
    adversarial_suppressed = partition_evidence(
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        root_start=3_504_000,
        candidate_trades=20,
        base_trades=40,
        false_green_delta=0,
    )
    adversarial_excessive = partition_evidence(
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        root_start=3_504_000,
        candidate_trades=81,
        base_trades=50,
        false_green_delta=-20_001,
    )
    first_post = assess_post_reveal_overfit(
        fixture.assessment,
        holdout_suppressed,
        adversarial_excessive,
        candidate_complexity_points=40,
        base_complexity_points=10,
    )
    second_post = assess_post_reveal_overfit(
        fixture.assessment,
        holdout_excessive,
        adversarial_suppressed,
        candidate_complexity_points=40,
        base_complexity_points=10,
    )
    post_union = tuple(
        item
        for item in POST_REVEAL_ADDITIONS_V1
        if item in set(first_post.labels).union(second_post.labels)
    )
    if post_union != POST_REVEAL_ADDITIONS_V1:
        failures.append("post-reveal suffix and complexity predicates are incomplete")
    if (
        first_post.labels[: len(fixture.assessment.labels)]
        != fixture.assessment.labels
        or first_post.preserved_pre_reveal_sha256
        != fixture.assessment.assessment_sha256
        or first_post.evaluated_labels
        != PRE_REVEAL_APPLICABILITY_V1 + POST_REVEAL_ADDITIONS_V1
        or fixture.assessment.sealed_not_evaluated != PRE_REVEAL_SEALED_V1
    ):
        failures.append("post-reveal assessment recalculated or replaced pre-reveal labels")
    if not threshold_sensitivity(signed_threshold) or not threshold_sensitivity(
        ranged_threshold
    ):
        failures.append("threshold both-sign or greater-than-100000 range rule changed")
    if (
        not trade_suppression(holdout_suppressed)
        or not excessive_trade_frequency(adversarial_excessive)
        or excessive_trade_frequency(holdout_suppressed)
    ):
        failures.append("trade suppression or excessive-frequency predicate changed")
    fixture_projection = {
        "development_fixture": fixture.as_dict(),
        "development_fixture_sha256": fixture.fixture_sha256,
        "derived_threshold": derived_threshold.as_dict(),
        "first_post": first_post.as_dict(),
        "post_union": [item.value for item in post_union],
        "second_post": second_post.as_dict(),
        "threshold_range": ranged_threshold.as_dict(),
        "threshold_sign": signed_threshold.as_dict(),
        "zero_positive_seed": one_seed_dependence(zero_positive),
        "zero_positive_scenario": one_scenario_dependence(zero_positive),
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(fixture_projection)
    ).hexdigest()
    if fixture_sha256 != WO35E_OVERFIT_FIXTURE_SHA256:
        failures.append("WO35-E overfit predicates differ from their frozen fixture")
    return StrategyDiscoveryAuditCase(
        "e_all_overfit_predicates_apply_once_and_the_training_star_is_rejected",
        (
            f"fixture_sha256={fixture_sha256} development_sha256={fixture.fixture_sha256} "
            f"pre_labels={','.join(item.value for item in fixture.assessment.labels)} "
            f"post_additions={len(post_union)}/9 pre_preserved=YES "
            "zero_positive_denominator=FALSE_WITHOUT_MISSING threshold_rules=2/2 "
            "fixture_outcome=REJECTED"
        ),
        tuple(failures),
    )


def _terminal_evidence(
    *,
    candidate_semantic_sha256: str,
    partition: StrategyPartitionV1,
    roots: tuple[int, ...],
    delta: int,
    compatibility: EvidenceCompatibilityKeyV1,
) -> CandidatePartitionEvidenceV1:
    return CandidatePartitionEvidenceV1(
        candidate_id="wo35e-terminal-candidate",
        semantic_sha256=candidate_semantic_sha256,
        partition=partition,
        compatibility=compatibility,
        root_deltas=tuple(RootDeltaV1(root, delta) for root in roots),
        component_deltas=tuple(
            ComponentDeltaV1(
                item.objective_id,
                (
                    60_000
                    if item.objective_id
                    is StrategyObjectiveIdV1.BALANCED_CLASSIFICATION
                    else 10_000
                ),
            )
            for item in REQUIRED_OBJECTIVE_SPECS_V1
        ),
        candidate_trades=40,
        base_trades=40,
        complexity_points=30,
        oracle_id="WO35E_SYNTHETIC_TERMINAL_ORACLE_V1",
    )


def _reveal_and_terminal_case() -> StrategyDiscoveryAuditCase:
    failures: list[str] = []
    robustness = build_synthetic_robustness_evidence(SyntheticRobustnessModeV1.PASS)
    robustness_decision = qualify_robustness(robustness)
    material = seal_terminal_material(
        candidate_semantic_sha256=robustness.candidate_semantic_sha256,
        holdout_manifest_sha256=_digest("wo35e/holdout-manifest"),
        holdout_member_inventory_sha256=_digest("wo35e/holdout-members"),
        adversarial_manifest_sha256=_digest("wo35e/adversarial-manifest"),
        adversarial_member_inventory_sha256=_digest("wo35e/adversarial-members"),
        reveal_token="wo35e-one-time-token",
    )

    def controller() -> TerminalRevealControllerV1:
        return TerminalRevealControllerV1(
            candidate_semantic_sha256=robustness.candidate_semantic_sha256,
            sealed_material_commitment_sha256=material.commitment_sha256,
        )

    main = controller()
    pre_reveal_clean = (
        main.stage is RevealStageV1.CANDIDATE_FROZEN
        and not main.access_records
        and not main.token_consumed
        and not any(
            slot in {"_holdout", "_adversarial", "_material"}
            for slot in TerminalRevealControllerV1.__slots__
        )
    )
    main.record_robustness(robustness, robustness_decision)
    result = main.reveal(material, reveal_token="wo35e-one-time-token")
    if (
        not pre_reveal_clean
        or main.stage is not RevealStageV1.TERMINAL_REVEALED
        or not main.token_consumed
        or len(main.access_records) != 1
        or result.execution_order != TERMINAL_ROOT_ORDER_V1
        or not result.access_recorded_before_exposure
        or result.access_record.partitions
        != (
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        )
    ):
        failures.append("valid atomic terminal reveal ordering or accounting changed")

    early = controller()
    try:
        early.reveal(material, reveal_token="wo35e-one-time-token")
    except RevealProtocolError as error:
        early_code = error.code
    else:
        early_code = "GRANTED"
        failures.append("terminal material was exposed before robustness")
    failed_robustness = build_synthetic_robustness_evidence(
        SyntheticRobustnessModeV1.BRITTLE
    )
    failed_decision = qualify_robustness(failed_robustness)
    failed = controller()
    failed.record_robustness(failed_robustness, failed_decision)
    if (
        failed.stage is not RevealStageV1.CLOSED_INSUFFICIENT_EVIDENCE
        or failed.access_records
        or failed.token_consumed
    ):
        failures.append("scientific robustness miss did not close without reveal")
    forged_pass = controller()
    try:
        forged_pass.record_robustness(
            failed_robustness,
            replace(
                failed_decision,
                outcome=RobustnessOutcomeV1.PASSED,
                reasons=(),
            ),
        )
    except RevealProtocolError as error:
        forged_pass_code = error.code
    else:
        forged_pass_code = "GRANTED"
        failures.append("forged passing robustness decision opened the reveal gate")
    invalid_robustness = build_synthetic_robustness_evidence(
        SyntheticRobustnessModeV1.REPLAY_INVALID
    )
    invalid = controller()
    invalid.record_robustness(
        invalid_robustness,
        qualify_robustness(invalid_robustness),
    )
    if invalid.stage is not RevealStageV1.EXPERIMENT_INVALID:
        failures.append("replay-invalid robustness evidence was not experiment-invalid")
    wrong_token = controller()
    wrong_token.record_robustness(robustness, robustness_decision)
    try:
        wrong_token.reveal(material, reveal_token="wrong-token")
    except RevealProtocolError as error:
        wrong_token_code = error.code
    else:
        wrong_token_code = "GRANTED"
        failures.append("wrong reveal token exposed terminal material")
    repeated = controller()
    repeated.record_robustness(robustness, robustness_decision)
    repeated.reveal(material, reveal_token="wo35e-one-time-token")
    try:
        repeated.reveal(material, reveal_token="wo35e-one-time-token")
    except RevealProtocolError as error:
        repeat_code = error.code
    else:
        repeat_code = "GRANTED"
        failures.append("terminal reveal token was reusable")
    rerun = controller()
    rerun.record_robustness(robustness, robustness_decision)
    try:
        rerun.record_robustness(robustness, robustness_decision)
    except RevealProtocolError as error:
        rerun_code = error.code
    else:
        rerun_code = "GRANTED"
        failures.append("robustness could run twice after candidate freeze")
    if (
        early_code != "ROBUSTNESS_NOT_PASSED"
        or early.stage is not RevealStageV1.EXPERIMENT_INVALID
        or wrong_token_code != "REVEAL_TOKEN_MISMATCH"
        or wrong_token.stage is not RevealStageV1.EXPERIMENT_INVALID
        or wrong_token.access_records
        or repeat_code != "REVEAL_ALREADY_CONSUMED"
        or repeated.stage is not RevealStageV1.EXPERIMENT_INVALID
        or rerun_code != "ROBUSTNESS_ALREADY_RECORDED"
        or rerun.robustness_record_count != 1
        or forged_pass_code != "ROBUSTNESS_BINDING_MISMATCH"
        or forged_pass.stage is not RevealStageV1.EXPERIMENT_INVALID
    ):
        failures.append("reveal/access/rerun protocol violations did not fail invalid")

    compatibility = EvidenceCompatibilityKeyV1(
        "WO35E_TERMINAL_SCENARIOS_V1",
        "WO35_OBJECTIVES_V1",
        "WO35E_TERMINAL_EVIDENCE_V1",
    )
    validation = _terminal_evidence(
        candidate_semantic_sha256=robustness.candidate_semantic_sha256,
        partition=StrategyPartitionV1.VALIDATION,
        roots=tuple(range(3_502_000, 3_502_008)),
        delta=60_000,
        compatibility=compatibility,
    )
    holdout = _terminal_evidence(
        candidate_semantic_sha256=robustness.candidate_semantic_sha256,
        partition=StrategyPartitionV1.HOLDOUT,
        roots=tuple(range(3_503_000, 3_503_008)),
        delta=40_000,
        compatibility=compatibility,
    )
    adversarial = _terminal_evidence(
        candidate_semantic_sha256=robustness.candidate_semantic_sha256,
        partition=StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        roots=tuple(range(3_504_000, 3_504_008)),
        delta=80_000,
        compatibility=compatibility,
    )
    holdout_decision = qualify_holdout(holdout, validation)
    adversarial_decision = qualify_adversarial(
        adversarial,
        trained_candidate_count=64,
    )
    confirmed = scientific_conclusion(
        candidate_selected=True,
        validation_qualified=True,
        robustness_qualified=robustness_decision.qualified,
        holdout_qualified=holdout_decision.qualified,
        adversarial_qualified=adversarial_decision.qualified,
        reveal_stage=main.stage,
    )
    no_winner = scientific_conclusion(
        candidate_selected=False,
        validation_qualified=False,
        robustness_qualified=False,
        holdout_qualified=False,
        adversarial_qualified=False,
        reveal_stage=RevealStageV1.CANDIDATE_FROZEN,
    )
    insufficient = scientific_conclusion(
        candidate_selected=True,
        validation_qualified=True,
        robustness_qualified=False,
        holdout_qualified=False,
        adversarial_qualified=False,
        reveal_stage=RevealStageV1.CLOSED_INSUFFICIENT_EVIDENCE,
    )
    if (
        not holdout_decision.qualified
        or not adversarial_decision.qualified
        or confirmed is not ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
        or no_winner is not ScientificConclusionV1.NO_CANDIDATE_MET_CRITERIA
        or insufficient is not ScientificConclusionV1.INSUFFICIENT_EVIDENCE
    ):
        failures.append("terminal partition or named-scope qualification changed")
    fixture_projection = {
        "access": result.access_record.as_dict(),
        "access_sha256": result.access_record.access_sha256,
        "adversarial": adversarial_decision.as_dict(),
        "conclusions": [confirmed.value, no_winner.value, insufficient.value],
        "early_code": early_code,
        "execution_order": list(result.execution_order),
        "forged_pass_code": forged_pass_code,
        "holdout": holdout_decision.as_dict(),
        "material_commitment_sha256": material.commitment_sha256,
        "repeat_code": repeat_code,
        "rerun_code": rerun_code,
        "wrong_token_code": wrong_token_code,
    }
    fixture_sha256 = hashlib.sha256(
        canonical_identity_bytes(fixture_projection)
    ).hexdigest()
    if fixture_sha256 != WO35E_REVEAL_FIXTURE_SHA256:
        failures.append("WO35-E reveal/terminal qualification differs from its fixture")
    return StrategyDiscoveryAuditCase(
        "e_robustness_precedes_one_atomic_reveal_and_terminal_claims_stay_named",
        (
            f"fixture_sha256={fixture_sha256} access_sha256={result.access_record.access_sha256} "
            "pre_reveal_material=SEALED access_before_exposure=YES token_consumed=YES "
            "root_order=HOLDOUT_THEN_ADVERSARIAL repeat=REFUSED rerun=REFUSED "
            "robustness_miss=INSUFFICIENT_EVIDENCE protocol_violation=EXPERIMENT_INVALID "
            "forged_robustness_pass=REFUSED "
            "no_winner=VALID confirmed=CONFIRMED_WITHIN_DECLARED_SCOPE"
        ),
        tuple(failures),
    )


def audit_wo35e_strategy_robustness() -> tuple[StrategyDiscoveryAuditCase, ...]:
    return (
        _robustness_perturbation_case(),
        _robustness_qualification_case(),
        _observability_case(),
        _overfit_case(),
        _reveal_and_terminal_case(),
    )


__all__ = [
    "WO35A_AUDIT_CASE_COUNT",
    "WO35A_CANONICALIZATION_POLICY_SHA256",
    "WO35A_FIXTURE_SHA256",
    "WO35A_LINEAGE_FIXTURE_SHA256",
    "WO35B_ACCESS_POLICY_SHA256",
    "WO35B_AUDIT_CASE_COUNT",
    "WO35B_FIXTURE_SHA256",
    "WO35C_AUDIT_CASE_COUNT",
    "WO35C_ACCOUNTING_SHA256",
    "WO35C_BATCH_SHA256",
    "WO35C_FIXTURE_SHA256",
    "WO35C_OPERATOR_REGISTRY_SHA256",
    "WO35D_ACCESS_FIXTURE_SHA256",
    "WO35D_AUDIT_CASE_COUNT",
    "WO35D_MANIFEST_FIXTURE_SHA256",
    "WO35D_NO_WINNER_RUN_SHA256",
    "WO35D_OBJECTIVE_FIXTURE_SHA256",
    "WO35D_POLICY_FIXTURE_SHA256",
    "WO35E_AUDIT_CASE_COUNT",
    "WO35E_OBSERVABILITY_FIXTURE_SHA256",
    "WO35E_OVERFIT_FIXTURE_SHA256",
    "WO35E_PERTURBATION_FIXTURE_SHA256",
    "WO35E_REVEAL_FIXTURE_SHA256",
    "WO35E_ROBUSTNESS_FIXTURE_SHA256",
    "StrategyDiscoveryAuditCase",
    "audit_wo35a_strategy_discovery",
    "audit_wo35b_strategy_partitions",
    "audit_wo35c_strategy_mutations",
    "audit_wo35d_strategy_search",
    "audit_wo35e_strategy_robustness",
]
