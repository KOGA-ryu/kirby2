"""Executable strategy-discovery audits for Work Orders 35-A through 35-C."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

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
from kirby2.experiments.models import (
    ExperimentManifest,
    ExperimentMode,
    StrategyVariant,
)
from kirby2.immutable import thaw_json
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
    "StrategyDiscoveryAuditCase",
    "audit_wo35a_strategy_discovery",
    "audit_wo35b_strategy_partitions",
    "audit_wo35c_strategy_mutations",
]
