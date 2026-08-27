"""Executable audit for causal historical features and evidence provenance."""

from __future__ import annotations

from dataclasses import dataclass, replace

from kirby2.features import FeatureKey
from kirby2.historical import (
    FeatureAvailability,
    HistoricalEvidenceScope,
    HistoricalFeatureProvenance,
    HistoricalStrategyEvidenceError,
    evaluate_historical_strategy,
    historical_feature_provenance_summary,
    load_historical_fixtures,
    load_historical_lessons,
    render_historical_report,
    replay_historical_features,
    require_historical_strategy_evidence,
    run_historical_fixture,
    run_historical_lesson,
)
from kirby2.strategy import parse_strategy


@dataclass(frozen=True, slots=True)
class HistoricalFeatureAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_historical_features() -> tuple[HistoricalFeatureAuditCase, ...]:
    fixtures = load_historical_fixtures()
    exact = run_historical_fixture(fixtures["exact_demo"])
    reconstruction = run_historical_fixture(fixtures["reconstruction_demo"])
    return (
        _exact_trade_case(exact),
        _source_capability_case(reconstruction),
        _reconstruction_provenance_case(reconstruction),
        _timestamps_and_expiry_case(exact),
        _aggressor_capability_case(exact),
        _strategy_refusal_case(reconstruction),
        _replay_lessons_and_report_case(exact, reconstruction),
    )


def _exact_trade_case(run) -> HistoricalFeatureAuditCase:
    replay = replay_historical_features(
        run,
        frame_times_us=(0, 1_000_000, 2_000_000, run.duration_us),
        windows_us=(1_000_000, 5_000_000),
    )
    at_two_seconds = replay.frames[2]
    velocity = at_two_seconds.value(FeatureKey.TRADE_VELOCITY, 1_000_000)
    aggressive_buy = at_two_seconds.value(
        FeatureKey.AGGRESSIVE_BUY_VOLUME,
        1_000_000,
    )
    failures: list[str] = []
    if velocity.availability is not FeatureAvailability.AVAILABLE or not velocity.value:
        failures.append("exact trade replay produced an empty rolling trade velocity")
    if aggressive_buy.availability is not FeatureAvailability.AVAILABLE:
        failures.append("exact aggressor-side volume was not available")
    if velocity.provenance is not HistoricalFeatureProvenance.DERIVED_FROM_SOURCE:
        failures.append("computed exact trade feature was not source-derived")
    if replay.replay_json_lines() != replay_historical_features(
        run,
        frame_times_us=(0, 1_000_000, 2_000_000, run.duration_us),
        windows_us=(1_000_000, 5_000_000),
    ).replay_json_lines():
        failures.append("exact historical feature replay was nondeterministic")
    return HistoricalFeatureAuditCase(
        "exact_ordered_trade_features",
        {
            "aggressive_buy_volume_1s": aggressive_buy.as_dict(),
            "frame_times_us": [frame.simulation_time_us for frame in replay.frames],
            "replay_sha256": replay.replay_sha256(),
            "trade_velocity_1s": velocity.as_dict(),
        },
        tuple(failures),
    )


def _source_capability_case(run) -> HistoricalFeatureAuditCase:
    frame = replay_historical_features(
        run,
        windows_us=(1_000_000,),
    ).terminal_frame
    queue_keys = (
        FeatureKey.BEST_BID_SIZE,
        FeatureKey.BEST_ASK_SIZE,
        FeatureKey.TOP_LEVEL_IMBALANCE,
        FeatureKey.WEIGHTED_DEPTH_BID,
        FeatureKey.WEIGHTED_DEPTH_ASK,
    )
    queue_values = tuple(frame.value(key) for key in queue_keys)
    failures: list[str] = []
    if any(
        value.availability is not FeatureAvailability.UNAVAILABLE
        or value.value is not None
        or value.provenance is not HistoricalFeatureProvenance.UNAVAILABLE
        for value in queue_values
    ):
        failures.append("source-only queue feature was represented as measured zero")
    spread = frame.value(FeatureKey.SPREAD_TICKS)
    if (
        spread.availability is not FeatureAvailability.AVAILABLE
        or spread.provenance is not HistoricalFeatureProvenance.OBSERVED
    ):
        failures.append("direct terminal spread observation was not preserved")
    return HistoricalFeatureAuditCase(
        "source_capability_unavailable_not_zero",
        {
            "queue_values": {
                value.field_name: value.as_dict() for value in queue_values
            },
            "spread": spread.as_dict(),
            "summary": historical_feature_provenance_summary(frame),
        },
        tuple(failures),
    )


def _reconstruction_provenance_case(run) -> HistoricalFeatureAuditCase:
    replay = replay_historical_features(
        run,
        frame_times_us=(0, 10_000_000, 20_000_000, run.duration_us),
        windows_us=(1_000_000,),
        evidence_scope=HistoricalEvidenceScope.INCLUDE_RECONSTRUCTION,
    )
    frame = replay.terminal_frame
    queue_values = (
        frame.value(FeatureKey.BEST_BID_SIZE),
        frame.value(FeatureKey.MULTI_LEVEL_IMBALANCE),
        frame.value(FeatureKey.QUEUE_REPLENISHMENT_BID, 1_000_000),
    )
    failures: list[str] = []
    if any(
        value.provenance is not HistoricalFeatureProvenance.SYNTHETIC_RECONSTRUCTION
        for value in queue_values
    ):
        failures.append("reconstructed queue evidence was mislabeled as observed")
    if any(value.availability is FeatureAvailability.UNAVAILABLE for value in queue_values):
        failures.append("opt-in reconstruction did not expose its modeled queue values")
    for frame_item in replay.frames:
        for value in frame_item.values.values():
            if (
                value.provenance is HistoricalFeatureProvenance.OBSERVED
                and value.key is not FeatureKey.SPREAD_TICKS
            ):
                failures.append("synthetic reconstruction claimed non-spread observation")
    return HistoricalFeatureAuditCase(
        "synthetic_reconstruction_queue_labels",
        {
            "queue_values": {
                value.field_name: value.as_dict() for value in queue_values
            },
            "replay_sha256": replay.replay_sha256(),
            "summary": historical_feature_provenance_summary(frame),
        },
        tuple(failures),
    )


def _timestamps_and_expiry_case(run) -> HistoricalFeatureAuditCase:
    times = (0, 1_250_000, 1_500_000, 4_500_001, 5_000_000)
    first = replay_historical_features(
        run,
        frame_times_us=times,
        windows_us=(500_000,),
    )
    second = replay_historical_features(
        run,
        frame_times_us=times,
        windows_us=(500_000,),
    )
    time_zero = first.frames[0]
    gap = first.frames[1]
    final = first.terminal_frame
    failures: list[str] = []
    if first.replay_json_lines() != second.replay_json_lines():
        failures.append("timestamp gap/tie replay was nondeterministic")
    if time_zero.value(FeatureKey.BEST_BID_SIZE).value != 500:
        failures.append("identical timestamp source order was not sequence-stable")
    if gap.value(FeatureKey.TRADE_VELOCITY, 500_000).value != 2:
        failures.append("quiet timestamp gap did not retain the boundary trade")
    if final.value(FeatureKey.TRADE_VELOCITY, 500_000).value != 0:
        failures.append("terminal quiet window did not expire trade activity")
    return HistoricalFeatureAuditCase(
        "timestamp_gaps_ties_and_quiet_expiry",
        {
            "frame_sha256": [frame.sha256() for frame in first.frames],
            "gap_trade_velocity": gap.value(
                FeatureKey.TRADE_VELOCITY,
                500_000,
            ).as_dict(),
            "terminal_trade_velocity": final.value(
                FeatureKey.TRADE_VELOCITY,
                500_000,
            ).as_dict(),
            "time_zero_best_bid_size": time_zero.value(
                FeatureKey.BEST_BID_SIZE
            ).as_dict(),
            "times_us": list(times),
        },
        tuple(failures),
    )


def _aggressor_capability_case(run) -> HistoricalFeatureAuditCase:
    provenance = replace(run.provenance, provides_trade_aggressor_side=False)
    partial = replace(run, provenance=provenance)
    frame = replay_historical_features(
        partial,
        frame_times_us=(2_000_000,),
        windows_us=(1_000_000,),
    ).terminal_frame
    velocity = frame.value(FeatureKey.TRADE_VELOCITY, 1_000_000)
    aggressive = frame.value(FeatureKey.AGGRESSIVE_BUY_VOLUME, 1_000_000)
    failures: list[str] = []
    if velocity.availability is not FeatureAvailability.AVAILABLE:
        failures.append("trade count was hidden when only aggressor side was missing")
    if aggressive.availability is not FeatureAvailability.UNAVAILABLE:
        failures.append("missing aggressor side was represented as zero volume")
    return HistoricalFeatureAuditCase(
        "unsupported_trade_aggressor_side",
        {
            "aggressive_buy": aggressive.as_dict(),
            "trade_velocity": velocity.as_dict(),
        },
        tuple(failures),
    )


def _strategy_refusal_case(run) -> HistoricalFeatureAuditCase:
    frame = replay_historical_features(
        run,
        windows_us=(1_000_000,),
    ).terminal_frame
    refusing = parse_strategy(
        """\
setup historical_queue
window 1s
GREEN when
    best_bid_size > 0
WAIT when
    spread_ticks > 0
RED otherwise
"""
    )
    explicit = parse_strategy(
        """\
setup historical_queue
window 1s
unavailable AS_FALSE
GREEN when
    best_bid_size > 0
WAIT when
    spread_ticks > 0
RED otherwise
"""
    )
    refused = False
    refusal_fields: tuple[str, ...] = ()
    try:
        evaluate_historical_strategy(refusing, frame)
    except HistoricalStrategyEvidenceError as error:
        refused = True
        refusal_fields = error.unavailable_fields
    evaluation = evaluate_historical_strategy(explicit, frame)
    failures: list[str] = []
    if not refused or refusal_fields != ("best_bid_size",):
        failures.append("default historical strategy did not fail closed")
    if evaluation.state != "WAIT":
        failures.append("explicit AS_FALSE policy did not produce declared evaluation")
    if require_historical_strategy_evidence(explicit, frame) != ("best_bid_size",):
        failures.append("explicit policy did not retain unavailable evidence inventory")
    return HistoricalFeatureAuditCase(
        "strategy_unavailable_evidence_policy",
        {
            "explicit_evaluation": evaluation.as_dict(),
            "refusal_fields": list(refusal_fields),
            "refused": refused,
        },
        tuple(failures),
    )


def _replay_lessons_and_report_case(exact, reconstruction) -> HistoricalFeatureAuditCase:
    exact_report = render_historical_report(exact)
    reconstruction_report = render_historical_report(reconstruction)
    lessons = load_historical_lessons()
    sessions = tuple(run_historical_lesson(lessons[name]) for name in sorted(lessons))
    failures: list[str] = []
    for report in (exact_report, reconstruction_report):
        if "SOURCE_FEATURE_PROVENANCE" not in report:
            failures.append("historical report omitted source feature provenance")
        if "HISTORICAL_FEATURE_REPLAY_SHA256" not in report:
            failures.append("historical report omitted feature replay digest")
        if "RUNTIME_INVARIANTS PASS" not in report:
            failures.append("historical report lost invariant status")
    if "RECONSTRUCTION_FEATURE_PROVENANCE" not in reconstruction_report:
        failures.append("reconstruction report omitted synthetic feature disclosure")
    if any(not session.complete for session in sessions):
        failures.append("packaged R04 lesson source replay did not complete")
    if any(not session.run.replay_sha256() for session in sessions):
        failures.append("historical lesson source replay omitted digest")
    return HistoricalFeatureAuditCase(
        "historical_replay_lessons_and_provenance_output",
        {
            "exact_replay_sha256": exact.replay_sha256(),
            "lesson_count": len(sessions),
            "lesson_replay_sha256": [session.run.replay_sha256() for session in sessions],
            "reconstruction_replay_sha256": reconstruction.replay_sha256(),
            "report_feature_provenance": True,
        },
        tuple(failures),
    )
