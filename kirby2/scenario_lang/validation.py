"""Deterministic static scenario validation and artifact finalization."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .capabilities import (
    SCENARIO_TARGET_CAPABILITIES_V1,
    evaluate_scenario_capabilities,
)
from .identity import canonical_semantic_plan_bytes, compiled_artifact_digest
from .models import (
    DEFINITION_TYPE_BY_SECTION_V1,
    SCENARIO_COMPILATION_PHASES_V1,
    SCENARIO_EXECUTION_ELIGIBLE_REASON_V1,
    SCENARIO_FINALIZED_COMPILATION_PHASES_V1,
    SCENARIO_VALIDATION_FAMILIES_V1,
    SCENARIO_VALIDATION_POLICY_VERSION,
    SCENARIO_VALIDATION_REPORT_SCHEMA_VERSION,
    CompiledScenarioArtifactV1,
    ScenarioCapabilityDecisionV1,
    ScenarioTargetKindV1,
    ScenarioValidationFindingV1,
    ScenarioValidationReportV1,
    ScenarioValidationSeverityV1,
)


_RECORD_TOKEN_SPLIT = re.compile(r"[_.:/-]+")
_ALLOWED_SESSION_TRANSITIONS = frozenset(
    {
        ("CLOSED", "PREOPEN"),
        ("PREOPEN", "OPENING_AUCTION"),
        ("PREOPEN", "CLOSED"),
        ("OPENING_AUCTION", "CONTINUOUS"),
        ("OPENING_AUCTION", "HALTED"),
        ("OPENING_AUCTION", "CLOSED"),
        ("CONTINUOUS", "HALTED"),
        ("CONTINUOUS", "CLOSING_AUCTION"),
        ("CONTINUOUS", "POSTCLOSE"),
        ("HALTED", "REOPENING_AUCTION"),
        ("HALTED", "CLOSED"),
        ("HALTED", "POSTCLOSE"),
        ("REOPENING_AUCTION", "CONTINUOUS"),
        ("REOPENING_AUCTION", "HALTED"),
        ("REOPENING_AUCTION", "CLOSED"),
        ("CLOSING_AUCTION", "POSTCLOSE"),
        ("CLOSING_AUCTION", "HALTED"),
        ("POSTCLOSE", "CLOSED"),
        ("POSTCLOSE", "PREOPEN"),
    }
)
_AUCTION_STATES = frozenset(
    {"OPENING_AUCTION", "CLOSING_AUCTION", "REOPENING_AUCTION"}
)
_HIDDEN_FEATURES = frozenset(
    {
        "FUTURE_EVENTS",
        "FUTURE_TRADES",
        "GROUND_TRUTH",
        "HIDDEN_RESERVE",
        "PERFECT_FUTURE_PATH",
        "TRUE_QUEUE_POSITION",
    }
)
_SOURCE_CAPABILITY_RANK = {
    "BARS_ONLY": 0,
    "TRADES": 1,
    "TRADES_AND_QUOTES": 2,
    "LEVEL2_SNAPSHOTS": 3,
    "LEVEL2_DELTAS": 4,
    "MARKET_BY_ORDER": 5,
}
_RESOURCE_LIMITS = {
    "agent_count": 100_000,
    "maximum_duration_us": 604_800_000_000,
    "maximum_events": 10_000_000,
    "maximum_events_per_agent": 1_000_000,
    "maximum_orders": 1_000_000,
    "maximum_orders_per_agent": 1_000_000,
    "maximum_position_shares": 1_000_000_000,
}


class ScenarioValidationRefused(ValueError):
    def __init__(self, report: ScenarioValidationReportV1) -> None:
        if type(report) is not ScenarioValidationReportV1:
            raise TypeError("scenario validation refusal requires a V1 report")
        if report.passed:
            raise ValueError("passing validation report cannot be refused")
        self.report = report
        codes = ",".join(item.code for item in report.findings if item.blocks_execution)
        super().__init__(f"scenario validation refused: {codes or 'CAPABILITY_REFUSAL'}")


@dataclass(frozen=True, slots=True)
class _RecordView:
    section: str
    logical_name: str
    record_type: str
    fields: Mapping[str, object]
    location: str

    def value(self, *names: str, default: object = None) -> object:
        for name in names:
            if name in self.fields:
                return self.fields[name]
        return default

    def has_token(self, token: str) -> bool:
        return token in _RECORD_TOKEN_SPLIT.split(self.record_type.upper())


def validate_compiled_scenario(
    artifact: CompiledScenarioArtifactV1,
    *,
    target_registry: object | None = None,
) -> ScenarioValidationReportV1:
    """Return one canonical complete report without mutating or executing a plan."""

    if type(artifact) is not CompiledScenarioArtifactV1:
        raise TypeError("scenario validation requires a compiled V1 artifact")
    if artifact.execution_eligible:
        report = artifact.validation_report
        if report is None:
            raise ValueError("eligible scenario artifact omitted its validation report")
        return report

    if tuple(artifact.as_dict()["completed_phases"]) != SCENARIO_COMPILATION_PHASES_V1:
        raise ValueError("scenario validation requires the completed WO32-C phases")

    from .compiler import DEFAULT_SCENARIO_TARGET_REGISTRY, ScenarioTargetRegistry

    registry = (
        DEFAULT_SCENARIO_TARGET_REGISTRY
        if target_registry is None
        else target_registry
    )
    if type(registry) is not ScenarioTargetRegistry:
        raise TypeError("scenario validation requires ScenarioTargetRegistry")

    validators: tuple[
        tuple[
            str,
            Callable[
                [CompiledScenarioArtifactV1],
                tuple[ScenarioValidationFindingV1, ...],
            ],
        ],
        ...,
    ] = (
        ("SESSION_AUCTION_HALT", _validate_session_auction_halt),
        ("STATE_GRAPH_REACHABILITY", _validate_state_graph),
        ("TRANSITION_NUMERICS", _validate_transition_numerics),
        ("HAWKES_STABILITY", _validate_hawkes_stability),
        ("VENUE_INSTRUMENT_COMPATIBILITY", _validate_venue_instrument),
        ("LATENCY_REPLAY_COMPATIBILITY", _validate_latency_replay),
        ("FEATURE_OBSERVABILITY", _validate_feature_observability),
        ("STRATEGY_NO_LOOKAHEAD", _validate_strategy_no_lookahead),
        ("RESOURCE_LIMITS", _validate_resource_limits),
        ("CHECKPOINT_ADAPTERS", _validate_checkpoint_adapters),
        ("HISTORICAL_CAPABILITY", _validate_historical_capability),
    )
    findings: list[ScenarioValidationFindingV1] = []
    completed: list[str] = []
    for family, validator in validators:
        try:
            family_findings = validator(artifact)
        except (KeyError, TypeError, ValueError) as error:
            family_findings = (
                _finding(
                    family,
                    ScenarioValidationSeverityV1.ERROR,
                    "MALFORMED_VALIDATION_INPUT",
                    f"materialized_plan.{family.lower()}",
                    f"The materialized input for {family} is malformed: {error}",
                    "Recompile from a canonical source bundle before validation.",
                ),
            )
        findings.extend(family_findings)
        completed.append(family)

    decisions, capability_findings = evaluate_scenario_capabilities(artifact)
    findings.extend(capability_findings)
    findings.extend(_validate_target_contract(artifact, registry))
    completed.append("TARGET_CAPABILITY_CONTRACT")
    if tuple(completed) != SCENARIO_VALIDATION_FAMILIES_V1:
        raise AssertionError("scenario validation family execution order changed")

    canonical_findings = _canonical_findings(findings)
    error_count = sum(
        item.severity is ScenarioValidationSeverityV1.ERROR
        for item in canonical_findings
    )
    warning_count = sum(
        item.severity is ScenarioValidationSeverityV1.WARNING
        for item in canonical_findings
    )
    not_provable_count = sum(
        item.severity is ScenarioValidationSeverityV1.NOT_PROVABLE_STATICALLY
        for item in canonical_findings
    )
    blocking_not_provable_count = sum(
        item.required
        and item.severity is ScenarioValidationSeverityV1.NOT_PROVABLE_STATICALLY
        for item in canonical_findings
    )
    passed = not any(item.blocks_execution for item in canonical_findings) and not any(
        item.blocks_execution for item in decisions
    )
    return ScenarioValidationReportV1(
        schema_version=SCENARIO_VALIDATION_REPORT_SCHEMA_VERSION,
        policy_version=SCENARIO_VALIDATION_POLICY_VERSION,
        subject_artifact_digest=artifact.compiled_artifact_digest,
        source_bundle_digest=artifact.source_bundle_digest,
        semantic_plan_digest=artifact.semantic_plan_digest,
        native_plan_digest=artifact.native_plan_digest,
        run_identity_digest=artifact.run_identity_digest,
        target_kind=artifact.target_kind,
        target_version=artifact.target_version,
        adapter_id=artifact.adapter_id,
        adapter_version=artifact.adapter_version,
        completed_families=SCENARIO_VALIDATION_FAMILIES_V1,
        findings=canonical_findings,
        capability_decisions=decisions,
        error_count=error_count,
        warning_count=warning_count,
        not_provable_count=not_provable_count,
        blocking_not_provable_count=blocking_not_provable_count,
        passed=passed,
    )


def finalize_compiled_scenario(
    artifact: CompiledScenarioArtifactV1,
    report: ScenarioValidationReportV1 | None = None,
    *,
    target_registry: object | None = None,
) -> CompiledScenarioArtifactV1:
    """Bind a passing complete report into a new execution-eligible artifact."""

    if type(artifact) is not CompiledScenarioArtifactV1:
        raise TypeError("scenario finalization requires a compiled V1 artifact")
    if artifact.execution_eligible:
        raise ValueError("scenario artifact is already validation-finalized")
    computed_report = validate_compiled_scenario(
        artifact,
        target_registry=target_registry,
    )
    selected_report = computed_report if report is None else report
    if type(selected_report) is not ScenarioValidationReportV1:
        raise TypeError("scenario finalization requires a V1 validation report")
    if selected_report.canonical_bytes() != computed_report.canonical_bytes():
        raise ValueError("supplied scenario validation report was not validator-issued")
    if (
        selected_report.subject_artifact_digest != artifact.compiled_artifact_digest
        or selected_report.source_bundle_digest != artifact.source_bundle_digest
        or selected_report.semantic_plan_digest != artifact.semantic_plan_digest
        or selected_report.native_plan_digest != artifact.native_plan_digest
        or selected_report.run_identity_digest != artifact.run_identity_digest
        or selected_report.target_kind is not artifact.target_kind
        or selected_report.target_version != artifact.target_version
        or selected_report.adapter_id != artifact.adapter_id
        or selected_report.adapter_version != artifact.adapter_version
    ):
        raise ValueError("scenario validation report belongs to another artifact")
    if not selected_report.passed:
        raise ScenarioValidationRefused(selected_report)

    payload = artifact.as_dict()
    provenance = payload["provenance"]
    if not isinstance(provenance, Mapping):
        raise TypeError("compiled scenario provenance must be an object")
    payload["capability_decisions"] = [
        item.as_dict() for item in selected_report.capability_decisions
    ]
    payload["completed_phases"] = list(SCENARIO_FINALIZED_COMPILATION_PHASES_V1)
    payload["execution_eligible"] = True
    payload["execution_reason_code"] = SCENARIO_EXECUTION_ELIGIBLE_REASON_V1
    payload["pending_phases"] = []
    payload["validation_report_digest"] = (
        selected_report.validation_report_digest
    )
    payload["validation_report_json"] = selected_report.canonical_bytes().decode(
        "utf-8"
    )
    identity_body = dict(payload)
    del identity_body["compiled_artifact_digest"]
    del identity_body["provenance"]
    payload["compiled_artifact_digest"] = compiled_artifact_digest(
        canonical_semantic_plan_bytes(identity_body),
        provenance,
    )
    return CompiledScenarioArtifactV1(canonical_semantic_plan_bytes(payload))


def _validate_session_auction_halt(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    findings: list[ScenarioValidationFindingV1] = []
    records = (
        *_records(artifact, "session_schedule"),
        *_records(artifact, "scheduled_events"),
        *_records(artifact, "day_local_states"),
    )
    ranges: list[tuple[_RecordView, int, int, str | None]] = []
    for record in records:
        start = record.value("start_us", "start_time_us")
        end = record.value("end_us", "end_time_us")
        range_contract = start is not None or end is not None
        if range_contract:
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
            ):
                findings.append(
                    _finding(
                        "SESSION_AUCTION_HALT",
                        ScenarioValidationSeverityV1.ERROR,
                        "INVALID_TIME_RANGE",
                        record.location,
                        "A declared time range must satisfy 0 <= start_us < end_us.",
                        "Use a nonnegative half-open interval with a positive duration.",
                    )
                )
            else:
                group = record.value("exclusive_group")
                ranges.append(
                    (record, start, end, group if type(group) is str else None)
                )

        from_state = record.value("from_state", "from_session_state")
        to_state = record.value("to_state", "to_session_state")
        if from_state is not None or to_state is not None:
            transition = (from_state, to_state)
            if (
                type(from_state) is not str
                or type(to_state) is not str
                or transition not in _ALLOWED_SESSION_TRANSITIONS
            ):
                findings.append(
                    _finding(
                        "SESSION_AUCTION_HALT",
                        ScenarioValidationSeverityV1.ERROR,
                        "INVALID_SESSION_TRANSITION",
                        record.location,
                        f"Session transition {from_state!r} -> {to_state!r} is not allowed.",
                        "Use a transition in the closed PREOPEN through CLOSED lifecycle.",
                    )
                )

        if record.has_token("AUCTION"):
            state = record.value("auction_state", "session_state")
            uncross = record.value("uncross_enabled", default=True)
            indicative = record.value("indicative_price_enabled", default=True)
            if (
                (state is not None and state not in _AUCTION_STATES)
                or uncross is not True
                or indicative is not True
            ):
                findings.append(
                    _finding(
                        "SESSION_AUCTION_HALT",
                        ScenarioValidationSeverityV1.ERROR,
                        "IMPOSSIBLE_AUCTION_CONFIGURATION",
                        record.location,
                        "Auction configuration cannot produce a valid indicative uncross.",
                        "Select an auction session state and enable indicative "
                        "pricing and uncross.",
                    )
                )

    by_group: dict[str, list[tuple[_RecordView, int, int]]] = defaultdict(list)
    for record, start, end, group in ranges:
        if group is not None:
            by_group[group].append((record, start, end))
    for group, grouped in sorted(by_group.items()):
        ordered = sorted(grouped, key=lambda item: (item[1], item[2], item[0].logical_name))
        for previous, current in zip(ordered, ordered[1:]):
            if current[1] < previous[2]:
                findings.append(
                    _finding(
                        "SESSION_AUCTION_HALT",
                        ScenarioValidationSeverityV1.ERROR,
                        "OVERLAPPING_EXCLUSIVE_STATES",
                        current[0].location,
                        f"Exclusive state group {group} overlaps another active interval.",
                        "Make exclusive state intervals disjoint and half-open.",
                    )
                )
    return tuple(findings)


def _validate_state_graph(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    states = _records(artifact, "day_local_states")
    transitions = _records(artifact, "transition_rules")
    strategies = _records(artifact, "strategy")
    if not states and not transitions:
        return ()
    findings: list[ScenarioValidationFindingV1] = []
    state_names = {record.logical_name for record in states}
    initial = tuple(
        record.logical_name for record in states if record.value("initial") is True
    )
    if states and len(initial) != 1:
        findings.append(
            _finding(
                "STATE_GRAPH_REACHABILITY",
                ScenarioValidationSeverityV1.ERROR,
                "INVALID_TRANSITION_GRAPH",
                "root_source.day_local_states",
                "A finite state graph requires exactly one declared initial state.",
                "Mark exactly one day-local state with initial=true.",
            )
        )
    edges: list[tuple[str, str, _RecordView]] = []
    for record in transitions:
        source = record.value("from_state")
        target = record.value("to_state")
        if type(source) is not str or type(target) is not str:
            findings.append(
                _finding(
                    "STATE_GRAPH_REACHABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "MISSING_STATE_REFERENCE",
                    record.location,
                    "Transition must declare identifier-valued from_state and to_state.",
                    "Reference two declared day-local state logical names.",
                )
            )
            continue
        if source not in state_names or target not in state_names:
            findings.append(
                _finding(
                    "STATE_GRAPH_REACHABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "MISSING_STATE_REFERENCE",
                    record.location,
                    f"Transition references unknown state {source!r} or {target!r}.",
                    "Declare both endpoint states before referencing them.",
                )
            )
            continue
        edges.append((source, target, record))
    edge_pairs = tuple((source, target) for source, target, _ in edges)
    if len(edge_pairs) != len(set(edge_pairs)):
        findings.append(
            _finding(
                "STATE_GRAPH_REACHABILITY",
                ScenarioValidationSeverityV1.ERROR,
                "INVALID_TRANSITION_GRAPH",
                "root_source.transition_rules",
                "The finite transition graph contains duplicate directed edges.",
                "Keep one canonical transition declaration per directed edge.",
            )
        )
    reachable: set[str] = set()
    if len(initial) == 1:
        adjacency: dict[str, set[str]] = defaultdict(set)
        for source, target, _ in edges:
            adjacency[source].add(target)
        queue: deque[str] = deque(initial)
        while queue:
            state = queue.popleft()
            if state in reachable:
                continue
            reachable.add(state)
            queue.extend(sorted(adjacency[state].difference(reachable)))
        for state in sorted(state_names.difference(reachable)):
            record = next(item for item in states if item.logical_name == state)
            findings.append(
                _finding(
                    "STATE_GRAPH_REACHABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "UNREACHABLE_STRATEGY_STATE",
                    record.location,
                    f"Declared state {state!r} is unreachable from the initial state.",
                    "Add a bounded incoming transition or remove the unreachable state.",
                )
            )
    for strategy in strategies:
        referenced = _string_values(
            strategy.value("allowed_states", "strategy_states", default=())
        )
        initial_state = strategy.value("initial_state")
        if type(initial_state) is str:
            referenced = (*referenced, initial_state)
        for state in sorted(set(referenced)):
            if state not in state_names:
                findings.append(
                    _finding(
                        "STATE_GRAPH_REACHABILITY",
                        ScenarioValidationSeverityV1.ERROR,
                        "MISSING_STATE_REFERENCE",
                        strategy.location,
                        f"Strategy references undeclared state {state!r}.",
                        "Reference a declared finite day-local state.",
                    )
                )
            elif reachable and state not in reachable:
                findings.append(
                    _finding(
                        "STATE_GRAPH_REACHABILITY",
                        ScenarioValidationSeverityV1.ERROR,
                        "UNREACHABLE_STRATEGY_STATE",
                        strategy.location,
                        f"Strategy state {state!r} cannot be reached from the initial state.",
                        "Connect the strategy state through a bounded transition path.",
                    )
                )
    return tuple(findings)


def _validate_transition_numerics(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    transitions = _records(artifact, "transition_rules")
    findings: list[ScenarioValidationFindingV1] = []
    zero_edges: list[tuple[str, str]] = []
    for record in transitions:
        weight = record.value("probability_weight", "weight")
        if weight is not None and (type(weight) is not int or weight <= 0):
            findings.append(
                _finding(
                    "TRANSITION_NUMERICS",
                    ScenarioValidationSeverityV1.ERROR,
                    "INVALID_TRANSITION_WEIGHT",
                    record.location,
                    "Transition probability weight must be a positive integer.",
                    "Use an explicit positive probability_weight value.",
                )
            )
        duration = record.value("duration_us", "minimum_duration_us")
        maximum = record.value("maximum_duration_us")
        if duration is not None and (type(duration) is not int or duration < 0):
            findings.append(
                _finding(
                    "TRANSITION_NUMERICS",
                    ScenarioValidationSeverityV1.ERROR,
                    "INVALID_TRANSITION_DURATION",
                    record.location,
                    "Transition duration must be a nonnegative integer number of microseconds.",
                    "Use a bounded nonnegative duration.",
                )
            )
        if maximum is not None and (
            type(maximum) is not int
            or maximum < 0
            or (type(duration) is int and maximum < duration)
        ):
            findings.append(
                _finding(
                    "TRANSITION_NUMERICS",
                    ScenarioValidationSeverityV1.ERROR,
                    "INVALID_TRANSITION_DURATION",
                    record.location,
                    "Transition maximum duration is invalid or below its minimum.",
                    "Set maximum_duration_us greater than or equal to the minimum.",
                )
            )
        if duration == 0:
            source = record.value("from_state")
            target = record.value("to_state")
            if type(source) is str and type(target) is str:
                zero_edges.append((source, target))
    if _directed_cycle(zero_edges):
        findings.append(
            _finding(
                "TRANSITION_NUMERICS",
                ScenarioValidationSeverityV1.ERROR,
                "INVALID_TRANSITION_GRAPH",
                "root_source.transition_rules",
                "Zero-duration transitions contain a directed cycle.",
                "Break the cycle or give at least one edge a positive bounded duration.",
            )
        )
    return tuple(findings)


def _validate_hawkes_stability(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    hawkes_records = tuple(
        record
        for record in _records(artifact, "flow_model")
        if record.has_token("HAWKES") or record.value("model") == "HAWKES"
    )
    if not hawkes_records:
        return ()
    from kirby2.simulation.flow_models import load_accepted_hawkes_configs

    accepted = load_accepted_hawkes_configs()
    findings: list[ScenarioValidationFindingV1] = []
    for record in hawkes_records:
        profile_id = record.value("accepted_profile", "profile_id")
        if type(profile_id) is not str or profile_id not in accepted:
            findings.append(
                _finding(
                    "HAWKES_STABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "HAWKES_PROFILE_UNKNOWN",
                    record.location,
                    "Hawkes flow must bind one accepted, stability-certified profile.",
                    "Use an ID from the packaged accepted Hawkes profile registry.",
                )
            )
            continue
        certification = accepted[profile_id].stability_certification
        if certification.classification == "WARNING_NEAR_CRITICAL":
            findings.append(
                _finding(
                    "HAWKES_STABILITY",
                    ScenarioValidationSeverityV1.WARNING,
                    "HAWKES_STABILITY_NEAR_CRITICAL",
                    record.location,
                    f"Hawkes profile {profile_id!r} is accepted but near critical.",
                    "Preserve the accepted profile limits and monitor its stability margin.",
                    required=False,
                )
            )
        elif certification.classification != "PASS_SUBCRITICAL":
            findings.append(
                _finding(
                    "HAWKES_STABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "HAWKES_STABILITY_REJECTED",
                    record.location,
                    f"Hawkes profile {profile_id!r} is not certified subcritical.",
                    "Select a profile with PASS_SUBCRITICAL certification.",
                )
            )
    return tuple(findings)


def _validate_venue_instrument(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    instruments = _records(artifact, "instrument")
    venues = _records(artifact, "venues")
    findings: list[ScenarioValidationFindingV1] = []
    instrument_ids = {
        str(record.value("symbol", "instrument_id", default=record.logical_name))
        for record in instruments
    }
    contract = SCENARIO_TARGET_CAPABILITIES_V1[artifact.target_kind]
    supported_orders = set(contract.supported_order_instructions)
    venue_ids: list[str] = []
    for record in (*instruments, *venues):
        instructions = _string_values(
            record.value(
                "supported_order_instructions",
                "order_instructions",
                default=(),
            )
        )
        unsupported = sorted(set(instructions).difference(supported_orders))
        if unsupported:
            findings.append(
                _finding(
                    "VENUE_INSTRUMENT_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "UNSUPPORTED_ORDER_INSTRUCTION",
                    record.location,
                    f"Order instructions are unsupported by the target: {unsupported}.",
                    "Use only instructions declared by the selected target contract.",
                )
            )
    for venue in venues:
        venue_id = str(venue.value("venue_id", default=venue.logical_name))
        venue_ids.append(venue_id)
        referenced = set(
            _string_values(venue.value("instruments", "symbols", default=()))
        )
        if instrument_ids and referenced.difference(instrument_ids):
            findings.append(
                _finding(
                    "VENUE_INSTRUMENT_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "INSTRUMENT_NOT_SUPPORTED_BY_VENUE",
                    venue.location,
                    "Venue references an instrument absent from the declared inventory.",
                    "Declare the instrument or remove the venue reference.",
                )
            )
        bid = venue.value("starting_best_bid_ticks", "best_bid_ticks")
        ask = venue.value("starting_best_ask_ticks", "best_ask_ticks")
        if type(bid) is int and type(ask) is int and bid >= ask:
            findings.append(
                _finding(
                    "VENUE_INSTRUMENT_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "CROSSED_STARTING_BOOK",
                    venue.location,
                    f"Starting best bid {bid} is not below starting best ask {ask}.",
                    "Use a strictly non-crossed starting book.",
                )
            )
    if len(venue_ids) != len(set(venue_ids)):
        findings.append(
            _finding(
                "VENUE_INSTRUMENT_COMPATIBILITY",
                ScenarioValidationSeverityV1.ERROR,
                "INVALID_VENUE_COMBINATION",
                "root_source.venues",
                "Two venue declarations resolve to the same venue ID.",
                "Give every venue a unique stable venue_id.",
            )
        )
    if venues and artifact.target_kind is ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1:
        if len(venues) < 2:
            findings.append(
                _finding(
                    "VENUE_INSTRUMENT_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "INVALID_VENUE_COMBINATION",
                    "root_source.venues",
                    "A declared multivenue scenario requires at least two venues.",
                    "Declare at least two compatible venues.",
                )
            )
    elif len(venues) > 1 and artifact.target_kind in {
        ScenarioTargetKindV1.MARKET_SCENARIO_V1,
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
    }:
        findings.append(
            _finding(
                "VENUE_INSTRUMENT_COMPATIBILITY",
                ScenarioValidationSeverityV1.ERROR,
                "INVALID_VENUE_COMBINATION",
                "root_source.venues",
                "The selected target is single-venue but multiple venues were declared.",
                "Select MULTIVENUE_RECORDING_V1 or declare one venue.",
            )
        )
    return tuple(findings)


def _validate_latency_replay(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    findings: list[ScenarioValidationFindingV1] = []
    records = (
        *_records(artifact, "latency"),
        *_records(artifact, "historical_constraints"),
    )
    for record in records:
        replay_mode = record.value("replay_mode")
        source_capability = record.value("source_capability")
        required_capability = record.value(
            "required_source_capability",
            default="TRADES_AND_QUOTES",
        )
        exact = replay_mode == "EXACT_REPLAY" or record.value("exact_replay") is True
        if exact:
            if (
                type(source_capability) is not str
                or source_capability not in _SOURCE_CAPABILITY_RANK
                or type(required_capability) is not str
                or required_capability not in _SOURCE_CAPABILITY_RANK
                or _SOURCE_CAPABILITY_RANK[source_capability]
                < _SOURCE_CAPABILITY_RANK[required_capability]
            ):
                findings.append(
                    _finding(
                        "LATENCY_REPLAY_COMPATIBILITY",
                        ScenarioValidationSeverityV1.ERROR,
                        "EXACT_REPLAY_REQUIRES_STRONGER_DATA",
                        record.location,
                        "Exact replay was requested from a weaker source capability.",
                        "Supply the required source capability or declare reconstruction.",
                    )
                )
        latency_mode = record.value("latency_mode", "mode")
        if latency_mode == "RECORDED" and (
            type(source_capability) is not str
            or _SOURCE_CAPABILITY_RANK.get(source_capability, -1)
            < _SOURCE_CAPABILITY_RANK["TRADES_AND_QUOTES"]
        ):
            findings.append(
                _finding(
                    "LATENCY_REPLAY_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "LATENCY_REPLAY_INCOMPATIBLE",
                    record.location,
                    "Recorded latency requires timestamped quote-or-better source data.",
                    "Use synthetic latency or provide timestamped quote/depth data.",
                )
            )
        if replay_mode == "RECONSTRUCTION" and record.value("exact_replay") is True:
            findings.append(
                _finding(
                    "LATENCY_REPLAY_COMPATIBILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "RECONSTRUCTION_CANNOT_CLAIM_EXACT_REPLAY",
                    record.location,
                    "Reconstruction and exact replay are distinct capabilities.",
                    "Set exact_replay=false and label the run as reconstruction.",
                )
            )
    return tuple(findings)


def _validate_feature_observability(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    reveal_records = _records(artifact, "reveal_policy")
    strategies = _records(artifact, "strategy")
    target_observable = set(
        SCENARIO_TARGET_CAPABILITIES_V1[artifact.target_kind].observable_features
    )
    declared_observable: set[str] = set()
    declared_hidden: set[str] = set()
    findings: list[ScenarioValidationFindingV1] = []
    for record in reveal_records:
        declared_observable.update(
            _string_values(record.value("observable_features", default=()))
        )
        declared_hidden.update(
            _string_values(record.value("hidden_features", default=()))
        )
    observable = declared_observable or target_observable
    overlap = sorted(observable.intersection(declared_hidden | _HIDDEN_FEATURES))
    if overlap:
        findings.append(
            _finding(
                "FEATURE_OBSERVABILITY",
                ScenarioValidationSeverityV1.ERROR,
                "HIDDEN_TRUTH_EXPOSED",
                "root_source.reveal_policy",
                f"Features are simultaneously hidden and observable: {overlap}.",
                "Remove ground-truth features from the observable projection.",
            )
        )
    for strategy in strategies:
        required = set(
            _string_values(strategy.value("required_features", default=()))
        )
        hidden_required = sorted(required.intersection(declared_hidden | _HIDDEN_FEATURES))
        if hidden_required:
            findings.append(
                _finding(
                    "FEATURE_OBSERVABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "HIDDEN_TRUTH_EXPOSED",
                    strategy.location,
                    f"Strategy requests hidden or ground-truth features: {hidden_required}.",
                    "Restrict strategy inputs to decision-time observable features.",
                )
            )
        missing = sorted(
            required.difference(observable).union(
                required.difference(target_observable)
            )
        )
        if missing:
            findings.append(
                _finding(
                    "FEATURE_OBSERVABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "REQUIRED_FEATURE_NOT_OBSERVABLE",
                    strategy.location,
                    f"Required strategy features are not observable: {missing}.",
                    "Declare and target only features available at the information cutoff.",
                )
            )
    return tuple(findings)


def _validate_strategy_no_lookahead(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    findings: list[ScenarioValidationFindingV1] = []
    for strategy in _records(artifact, "strategy"):
        cutoff = strategy.value("information_cutoff", default="DECISION_TIME")
        future_offset = strategy.value("future_offset_us", default=0)
        time_reference = strategy.value("feature_time_reference", default="AT_OR_BEFORE_CUTOFF")
        uses_future = strategy.value("uses_future_information", default=False)
        if (
            cutoff not in {"DECISION_TIME", "OBSERVATION_TIME"}
            or type(future_offset) is not int
            or future_offset != 0
            or uses_future is not False
            or time_reference in {"FUTURE", "NEXT_EVENT", "PERFECT_HINDSIGHT"}
        ):
            findings.append(
                _finding(
                    "STRATEGY_NO_LOOKAHEAD",
                    ScenarioValidationSeverityV1.ERROR,
                    "FUTURE_INFORMATION_EXPOSED",
                    strategy.location,
                    "Strategy requests information after its declared decision cutoff.",
                    "Use only observations at or before DECISION_TIME.",
                )
            )
        if strategy.value("requires_general_proof") is True:
            findings.append(
                _finding(
                    "STRATEGY_NO_LOOKAHEAD",
                    ScenarioValidationSeverityV1.NOT_PROVABLE_STATICALLY,
                    "GENERAL_STRATEGY_PROOF_NOT_PROVABLE_STATICALLY",
                    strategy.location,
                    "A general behavioral proof cannot be established from the "
                    "finite declaration graph.",
                    "Replace the requirement with bounded declarative checks and "
                    "runtime cutoff enforcement.",
                    required=True,
                )
            )
        findings.append(
            _finding(
                "STRATEGY_NO_LOOKAHEAD",
                ScenarioValidationSeverityV1.WARNING,
                "RUNTIME_CUTOFF_ENFORCEMENT_REQUIRED",
                strategy.location,
                "Static no-lookahead checks supplement but do not replace runtime "
                "cutoff enforcement.",
                "Keep the runtime information-cutoff guard enabled.",
                required=False,
            )
        )
    return tuple(findings)


def _validate_resource_limits(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    findings: list[ScenarioValidationFindingV1] = []
    populations = _records(artifact, "agent_populations")
    for record in populations:
        bounded = record.value("bounded", default=True)
        budget_mode = record.value("budget_mode")
        bound_names = (
            "maximum_events_per_agent",
            "maximum_orders_per_agent",
            "maximum_position_shares",
            "budget_shares",
        )
        has_bound = any(record.value(name) is not None for name in bound_names)
        if bounded is not True or budget_mode == "UNBOUNDED" or not has_bound:
            findings.append(
                _finding(
                    "RESOURCE_LIMITS",
                    ScenarioValidationSeverityV1.ERROR,
                    "AGENT_BUDGET_UNBOUNDED",
                    record.location,
                    "Agent population lacks an explicit finite execution budget.",
                    "Declare finite per-agent order/event/position limits.",
                )
            )
    for section in _root_sections(artifact):
        for record in _records(artifact, section):
            for name, value in record.fields.items():
                normalized = name.lower()
                if name in _RESOURCE_LIMITS:
                    limit = _RESOURCE_LIMITS[name]
                    if type(value) is not int or value < 0 or value > limit:
                        findings.append(
                            _finding(
                                "RESOURCE_LIMITS",
                                ScenarioValidationSeverityV1.ERROR,
                                "RESOURCE_LIMIT_EXCEEDED",
                                f"{record.location}.fields[{name}]",
                                f"Resource {name} must lie in [0, {limit}].",
                                "Reduce the declared deterministic resource limit.",
                            )
                        )
                if (
                    type(value) is int
                    and value < 0
                    and any(
                        token in normalized
                        for token in ("budget", "count", "quantity", "shares")
                    )
                ):
                    findings.append(
                        _finding(
                            "RESOURCE_LIMITS",
                            ScenarioValidationSeverityV1.ERROR,
                            "NEGATIVE_QUANTITY",
                            f"{record.location}.fields[{name}]",
                            "Quantity and resource values cannot be negative.",
                            "Use a nonnegative exact integer quantity.",
                        )
                    )
    return tuple(findings)


def _validate_checkpoint_adapters(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    supported = set(
        SCENARIO_TARGET_CAPABILITIES_V1[artifact.target_kind].checkpoint_adapters
    )
    findings: list[ScenarioValidationFindingV1] = []
    for record in _records(artifact, "checkpoint_policy"):
        required = set(
            _string_values(record.value("required_adapters", default=()))
        )
        unsupported = sorted(required.difference(supported))
        if unsupported:
            findings.append(
                _finding(
                    "CHECKPOINT_ADAPTERS",
                    ScenarioValidationSeverityV1.ERROR,
                    "CHECKPOINT_ADAPTER_UNSUPPORTED",
                    record.location,
                    f"Checkpoint adapters are unsupported by the target: {unsupported}.",
                    "Request only adapters in the selected target capability contract.",
                )
            )
        if record.value("restore_required") is True and not supported:
            findings.append(
                _finding(
                    "CHECKPOINT_ADAPTERS",
                    ScenarioValidationSeverityV1.ERROR,
                    "CHECKPOINT_RESTORE_UNSUPPORTED",
                    record.location,
                    "The selected target has no declared restore adapter.",
                    "Disable restore or select a restorable target.",
                )
            )
    return tuple(findings)


def _validate_historical_capability(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    findings: list[ScenarioValidationFindingV1] = []
    records = _records(artifact, "historical_constraints")
    for record in records:
        source_capability = record.value("source_capability")
        replay_mode = record.value("replay_mode")
        requires_mbo = record.value("requires_market_by_order") is True or (
            record.value("required_source_capability") == "MARKET_BY_ORDER"
        )
        if requires_mbo and source_capability != "MARKET_BY_ORDER":
            findings.append(
                _finding(
                    "HISTORICAL_CAPABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "HISTORICAL_MBO_REQUIRES_MARKET_BY_ORDER",
                    record.location,
                    "Historical market-by-order behavior cannot be proven from weaker data.",
                    "Supply MARKET_BY_ORDER data or explicitly redesign as "
                    "reconstruction without the MBO claim.",
                )
            )
        if replay_mode == "RECONSTRUCTION" and source_capability != "MARKET_BY_ORDER":
            findings.append(
                _finding(
                    "HISTORICAL_CAPABILITY",
                    ScenarioValidationSeverityV1.WARNING,
                    "HISTORICAL_RECONSTRUCTION_EXPLICIT",
                    record.location,
                    "The historical plan is an explicit reconstruction, not an "
                    "exact-data upgrade.",
                    "Preserve reconstruction labels and known limitations at runtime.",
                    required=False,
                )
            )
        if (
            records
            and artifact.target_kind
            is not ScenarioTargetKindV1.HISTORICAL_LESSON_V1
            and replay_mode in {"EXACT_REPLAY", "RECONSTRUCTION"}
        ):
            findings.append(
                _finding(
                    "HISTORICAL_CAPABILITY",
                    ScenarioValidationSeverityV1.ERROR,
                    "HISTORICAL_TARGET_REQUIRED",
                    record.location,
                    "Historical replay declarations require HISTORICAL_LESSON_V1 "
                    "and cannot coerce another target.",
                    "Select the historical target explicitly.",
                )
            )
    if artifact.target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        from kirby2.historical.models import HistoricalDataMode

        native = artifact.plan_envelope.payload
        native_mode = getattr(native, "mode", None)
        for record in records:
            replay_mode = record.value("replay_mode")
            if (
                replay_mode == "EXACT_REPLAY"
                and native_mode is not HistoricalDataMode.EXACT_REPLAY
            ) or (
                replay_mode == "RECONSTRUCTION"
                and native_mode is not HistoricalDataMode.RECONSTRUCTION
            ):
                findings.append(
                    _finding(
                        "HISTORICAL_CAPABILITY",
                        ScenarioValidationSeverityV1.ERROR,
                        "HISTORICAL_NATIVE_MODE_MISMATCH",
                        record.location,
                        "Source historical mode differs from the tagged native lesson mode.",
                        "Bind a native lesson with the same explicit historical mode.",
                    )
                )
    return tuple(findings)


def _validate_target_contract(
    artifact: CompiledScenarioArtifactV1,
    registry: object,
) -> tuple[ScenarioValidationFindingV1, ...]:
    from .compiler import ScenarioTargetRegistry

    if type(registry) is not ScenarioTargetRegistry:
        raise TypeError("target validation requires ScenarioTargetRegistry")
    findings: list[ScenarioValidationFindingV1] = []
    try:
        registry.assert_closed_v1()
        adapter = registry.adapter(artifact.target_kind)
    except (KeyError, TypeError, ValueError) as error:
        return (
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_ADAPTER_MISSING",
                "native_plan_envelope",
                f"The selected target adapter is unavailable: {error}",
                "Use the closed five-target V1 registry.",
            ),
        )
    metadata = artifact.materialized_plan.get("root_source", {})
    if isinstance(metadata, Mapping):
        metadata = metadata.get("metadata", {})
    if not isinstance(metadata, Mapping) or (
        metadata.get("target_kind") != artifact.target_kind.value
        or metadata.get("target_version") != artifact.target_version
        or metadata.get("adapter_id") != artifact.adapter_id
        or metadata.get("adapter_version") != artifact.adapter_version
    ):
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_CONTRACT_MISMATCH",
                "root_source.metadata",
                "Materialized source target metadata differs from the native envelope.",
                "Compile with the exact declared target tag and adapter version.",
            )
        )
    if (
        adapter.adapter_id != artifact.adapter_id
        or adapter.adapter_version != artifact.adapter_version
        or adapter.target_version != artifact.target_version
    ):
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_ADAPTER_MISMATCH",
                "native_plan_envelope",
                "Registered target adapter differs from the compiled artifact contract.",
                "Use the exact closed adapter registered for the target tag.",
            )
        )
    findings.extend(_validate_native_run_contract(artifact))
    try:
        native = artifact.plan_envelope.payload
        validated = adapter.validate(native)
        persisted = adapter.persist(native)
        replayed = adapter.replay(persisted)
        replayed_bytes = adapter.persist(replayed)
        if validated != persisted or replayed_bytes != persisted:
            raise ValueError("target validate/persist/replay bytes differ")
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_PERSIST_REPLAY_UNSUPPORTED",
                "native_plan_envelope",
                f"Target persistence or replay validation failed: {error}",
                "Bind a canonical native payload with supported persist/replay adapters.",
            )
        )
    return tuple(findings)


def _validate_native_run_contract(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioValidationFindingV1, ...]:
    if artifact.target_kind is not ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        return ()

    from kirby2.full_day.composition import (
        INITIAL_PROFILE_ID,
        executable_agent_mechanics_composition_matrix,
    )

    native = artifact.plan_envelope.payload
    matrix = executable_agent_mechanics_composition_matrix()
    profile = native.composition_profile
    findings: list[ScenarioValidationFindingV1] = []
    if (
        profile.reference_id != INITIAL_PROFILE_ID
        or profile.version != 2
        or profile.sha256 != matrix.sha256
    ):
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_RUNTIME_PROFILE_UNSUPPORTED",
                "native_plan_envelope.payload.composition_profile",
                "The plan-only full-day adapter cannot materialize this composition profile.",
                "Use the executable mechanics-only WO31 profile or select the "
                "separately tagged native runtime.",
            )
        )
    scheduler_required = bool(native.participant_schedule) or any(
        item.initially_active for item in native.participant_definitions
    )
    if scheduler_required:
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_RUNTIME_DEPENDENCY_UNRESOLVED",
                "native_plan_envelope.payload.participant_schedule",
                "The full-day plan requires an injected agent scheduler that is "
                "not embedded in the native plan.",
                "Use an inactive mechanics-only plan or compile through a future "
                "adapter with resolved scheduler ownership.",
            )
        )
    if native.unscheduled_shock_policy.enabled:
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_RUNTIME_DEPENDENCY_UNRESOLVED",
                "native_plan_envelope.payload.unscheduled_shock_policy",
                "The full-day plan requires an injected shock distribution that "
                "is not embedded in the native plan.",
                "Disable unscheduled shocks or use an adapter that resolves the "
                "bound shock distribution.",
            )
        )
    if not scheduler_required and any(
        item.event_type.value == "LARGE_SCHEDULED_METAORDER"
        for item in native.scheduled_events
    ):
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_RUNTIME_DEPENDENCY_UNRESOLVED",
                "native_plan_envelope.payload.scheduled_events",
                "A scheduled metaorder requires the absent agent scheduler owner.",
                "Remove the metaorder event or provide a scheduler-backed target adapter.",
            )
        )
    if native.seed_policy.root_seed != artifact.seed_policy.selected_root_seed:
        findings.append(
            _finding(
                "TARGET_CAPABILITY_CONTRACT",
                ScenarioValidationSeverityV1.ERROR,
                "TARGET_SEED_POLICY_MISMATCH",
                "root_source.seed_policy",
                "The compiled seed differs from the immutable full-day plan root seed.",
                "Set the scenario root seed to the native full-day plan root seed.",
            )
        )
    return tuple(findings)


def _records(
    artifact: CompiledScenarioArtifactV1,
    section_name: str,
) -> tuple[_RecordView, ...]:
    root = artifact.materialized_plan.get("root_source")
    if not isinstance(root, Mapping):
        raise TypeError("materialized root_source must be an object")
    section = root.get(section_name)
    if not isinstance(section, Mapping):
        raise TypeError(f"materialized section {section_name} must be an object")
    raw_records = section.get("records")
    if type(raw_records) is not list:
        raise TypeError(f"materialized section {section_name} records must be an array")
    entries: list[tuple[Mapping[str, object], str | None]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise TypeError(f"materialized {section_name} record must be an object")
        entries.append((raw_record, None))
    definition_type = DEFINITION_TYPE_BY_SECTION_V1.get(section_name)
    if definition_type is not None:
        resolved = artifact.materialized_plan.get("resolved_definitions")
        if type(resolved) is not list:
            raise TypeError("materialized resolved_definitions must be an array")
        for definition in resolved:
            if not isinstance(definition, Mapping):
                raise TypeError("materialized resolved definition must be an object")
            if definition.get("definition_type") != definition_type.value:
                continue
            raw_record = definition.get("record")
            qualified_name = definition.get("qualified_name")
            if not isinstance(raw_record, Mapping) or type(qualified_name) is not str:
                raise TypeError("materialized resolved definition is malformed")
            entries.append(
                (
                    raw_record,
                    f"resolved_definitions[{qualified_name}].record",
                )
            )

    result: list[_RecordView] = []
    for raw_record, explicit_location in entries:
        logical_name = raw_record.get("logical_name")
        record_type = raw_record.get("record_type")
        raw_fields = raw_record.get("fields")
        if (
            type(logical_name) is not str
            or type(record_type) is not str
            or type(raw_fields) is not list
        ):
            raise TypeError(f"materialized {section_name} record is malformed")
        fields: dict[str, object] = {}
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping) or len(raw_field) != 2:
                raise TypeError(f"materialized {section_name} field is malformed")
            name = raw_field.get("name")
            if type(name) is not str:
                raise TypeError(f"materialized {section_name} field name is invalid")
            value_keys = tuple(key for key in raw_field if key != "name")
            if len(value_keys) != 1 or name in fields:
                raise ValueError(f"materialized {section_name} fields are not exact")
            fields[name] = raw_field[value_keys[0]]
        result.append(
            _RecordView(
                section=section_name,
                logical_name=logical_name,
                record_type=record_type,
                fields=fields,
                location=(
                    explicit_location
                    if explicit_location is not None
                    else f"root_source.{section_name}.records[{logical_name}]"
                ),
            )
        )
    return tuple(result)


def _root_sections(artifact: CompiledScenarioArtifactV1) -> tuple[str, ...]:
    root = artifact.materialized_plan.get("root_source")
    if not isinstance(root, Mapping):
        raise TypeError("materialized root_source must be an object")
    return tuple(sorted(key for key in root if key not in {"metadata", "schema_version"}))


def _string_values(value: object) -> tuple[str, ...]:
    if type(value) is str:
        return (value,)
    if type(value) in {tuple, list} and all(type(item) is str for item in value):
        return tuple(value)
    return ()


def _directed_cycle(edges: list[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for source, target in edges:
        adjacency[source].add(target)
        nodes.update((source, target))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(target) for target in sorted(adjacency[node])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in sorted(nodes) if node not in visited)


def _canonical_findings(
    findings: list[ScenarioValidationFindingV1],
) -> tuple[ScenarioValidationFindingV1, ...]:
    by_key: dict[tuple[str, str, str], ScenarioValidationFindingV1] = {}
    for finding in findings:
        if type(finding) is not ScenarioValidationFindingV1:
            raise TypeError("scenario validator emitted an untyped finding")
        key = (finding.family, finding.code, finding.source_location)
        previous = by_key.get(key)
        if previous is not None and previous != finding:
            raise ValueError("scenario validator emitted conflicting stable diagnostics")
        by_key[key] = finding
    return tuple(sorted(by_key.values(), key=lambda item: item.sort_key()))


def _finding(
    family: str,
    severity: ScenarioValidationSeverityV1,
    code: str,
    source_location: str,
    message: str,
    suggested_correction: str,
    *,
    required: bool = True,
) -> ScenarioValidationFindingV1:
    return ScenarioValidationFindingV1(
        family=family,
        severity=severity,
        code=code,
        source_location=source_location,
        message=message,
        suggested_correction=suggested_correction,
        required=required,
    )


__all__ = [
    "ScenarioValidationRefused",
    "finalize_compiled_scenario",
    "validate_compiled_scenario",
]
