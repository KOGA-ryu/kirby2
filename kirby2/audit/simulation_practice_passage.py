"""No-Qt proof for immutable Chapter 1 observation passages."""

from __future__ import annotations

import copy
import json
import unittest
from collections.abc import Mapping
from unittest.mock import patch

from kirby2.ui import (
    PracticeObservationPassageRequestV1,
    PracticeObservationPassageResultV1,
    SimulationFrameV1,
    acquire_simulation_practice_observation_passage,
    begin_simulation_practice_attempt,
    build_practice_action_request,
    build_practice_attempt_request,
    build_simulation_practice_observation_passage_request,
    finalize_simulation_run,
    list_simulation_practice_passage_capabilities,
    release_simulation_episode,
    read_current_simulation_frame,
    resolve_replay_artifact,
    submit_simulation_practice_action,
)
from kirby2.ui.simulation_contract import canonical_digest
from kirby2.ui.simulation_episode_contract import episode_prefix_projection
from kirby2.ui.simulation_practice_passage_contract import (
    build_practice_passage_observation,
)
from kirby2.ui.simulation_run_facade import _prepared_episode_model_sha256


_CASES: list[dict[str, object]] = []


def _begin(
    episode_id: str,
    *,
    pace_multiplier_ppm: int = 1_000_000,
    mode: str = "GUIDED",
) -> tuple[object, dict[str, object]]:
    handle, result = begin_simulation_practice_attempt(
        build_practice_attempt_request(
            episode_id=episode_id,
            mode=mode,
            pace_multiplier_ppm=pace_multiplier_ppm,
        )
    )
    if handle is None or result["status"] != "AVAILABLE":
        raise AssertionError(f"practice attempt unavailable: {result}")
    return handle, result


def _acquire(
    handle: object,
    result: Mapping[str, object],
) -> tuple[object | None, dict[str, object], dict[str, object]]:
    request = build_simulation_practice_observation_passage_request(result)
    cleanup, passage = acquire_simulation_practice_observation_passage(handle, request)
    return cleanup, passage, request


def _reidentify(record: dict[str, object]) -> None:
    basis = {key: value for key, value in record.items() if key != "passage_id"}
    record["passage_id"] = f"practice-passage-{canonical_digest(basis)[:24]}"


def _reidentify_request(record: dict[str, object]) -> None:
    basis = {key: value for key, value in record.items() if key != "request_id"}
    record["request_id"] = f"practice-passage-request-{canonical_digest(basis)[:24]}"


def _retime_frame(frame: Mapping[str, object], simulation_time_us: int) -> dict[str, object]:
    result = copy.deepcopy(dict(frame))
    cursor = dict(result["cursor"])
    cursor["simulation_time_us"] = simulation_time_us
    cursor_basis = {key: value for key, value in cursor.items() if key != "cursor_id"}
    cursor["cursor_id"] = f"simulation-cursor-{canonical_digest(cursor_basis)[:24]}"
    result["cursor"] = cursor
    seconds, micros = divmod(simulation_time_us, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    result["clock"] = {
        **result["clock"],
        "cursor_label": f"T+{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}",
    }
    basis = {key: value for key, value in result.items() if key != "frame_id"}
    result["frame_id"] = f"simulation-frame-{canonical_digest(basis)[:24]}"
    return result


def _action(result: Mapping[str, object], semantic_action_id: str) -> dict[str, object]:
    frame = result["current_frame"]
    attempt = result["attempt"]
    if not isinstance(frame, Mapping) or not isinstance(attempt, Mapping):
        raise AssertionError("practice result lost its action origin")
    cursor = frame["cursor"]
    return build_practice_action_request(
        attempt_id=str(attempt["attempt_id"]),
        hold_id=result["hold_id"],
        source_run_id=str(frame["source_run_id"]),
        origin_frame_id=str(frame["frame_id"]),
        origin_cursor_id=str(cursor["cursor_id"]),
        operation="UNASSISTED",
        response_kind="SEMANTIC_ACTION",
        semantic_action_id=semantic_action_id,
    )


def _assessment_semantics(value: Mapping[str, object]) -> dict[str, object]:
    result = copy.deepcopy(dict(value))
    evidence = dict(result["evidence"])
    evidence.pop("frame_id")
    evidence.pop("cursor_id")
    result["evidence"] = evidence
    return result


class SimulationPracticePassageAudit(unittest.TestCase):
    maxDiff = None

    def test_real_passages_bind_changing_observations_and_exact_support(self) -> None:
        capabilities = list_simulation_practice_passage_capabilities()
        support = {
            item["episode_id"]: (
                item["episode_recipe_sha256"],
                item["support"],
                item["passage_duration_us"],
                item["unavailable_reason"],
            )
            for item in capabilities["episodes"]
        }
        self.assertEqual(
            support,
            {
                "practice.f1.place-and-cancel.v1": (
                    "e5245133976ab5e0766f3cc164a5279ee4787c218ee3d7353c4b2bbd10500586",
                    "UNSUPPORTED",
                    None,
                    "ANCHOR_TOO_SHORT_FOR_MEANINGFUL_PACE",
                ),
                "practice.f1.place-and-replace.v1": (
                    "656a2515109fb42e23297a1801dc1199725f9f2bcac7aa5923c019296e29fee4",
                    "UNSUPPORTED",
                    None,
                    "ANCHOR_TOO_SHORT_FOR_MEANINGFUL_PACE",
                ),
                "practice.f2.public-pressure.v1": (
                    "62ef9ec704b92bd45b9aaf09a61a7d21703da09b7c960c465b86e6693e228122",
                    "AVAILABLE",
                    1_000_000,
                    None,
                ),
                "practice.f2.replenishment.v1": (
                    "8b8b748e2156ef187dc5e490ccb09e8823e5e8aaac668ab0c26e3085cc7a3d21",
                    "AVAILABLE",
                    1_000_000,
                    None,
                ),
                "practice.f3.cancel-partial-residual.v1": (
                    "a29b223fb7ad3342a1142a70132d41a46e4bc3dc80ffa6824995f4424fdb7474",
                    "AVAILABLE",
                    1_000_000,
                    None,
                ),
                "practice.f3.cancel-volume-variation.v1": (
                    "bbea819721cd659794c94672b6649e941d46629b972c42e2fbabbdc31827b9e6",
                    "AVAILABLE",
                    1_000_000,
                    None,
                ),
            },
        )
        summaries: list[dict[str, object]] = []
        for episode_id in (
            "practice.f2.public-pressure.v1",
            "practice.f2.replenishment.v1",
            "practice.f3.cancel-partial-residual.v1",
            "practice.f3.cancel-volume-variation.v1",
        ):
            handle, begun = _begin(episode_id)
            try:
                live_before = copy.deepcopy(begun["current_frame"])
                model_before = _prepared_episode_model_sha256(handle)
                cleanup, passage, request = _acquire(handle, begun)
                self.assertIsNone(cleanup)
                parsed = PracticeObservationPassageResultV1.from_dict(
                    passage,
                    request=PracticeObservationPassageRequestV1.from_dict(request),
                )
                self.assertEqual(parsed.status, "AVAILABLE")
                times = [item["simulation_time_us"] for item in passage["observations"]]
                self.assertEqual(times, [0, 250_000, 500_000, 750_000, 1_000_000])
                public_states = {
                    canonical_digest(
                        {
                            "book": item["frame"]["book"],
                            "recent_trades": item["frame"]["recent_trades"],
                            "working_orders": item["frame"]["working_orders"],
                            "account": item["frame"]["account"],
                        }
                    )
                    for item in passage["observations"]
                }
                self.assertGreater(len(public_states), 1)
                self.assertEqual(
                    passage["final_cut"]["terminal_frame_projection_sha256"],
                    canonical_digest(
                        episode_prefix_projection(
                            SimulationFrameV1.from_dict(
                                passage["observations"][-1]["frame"]
                            )
                        )
                    ),
                )
                self.assertEqual(model_before, _prepared_episode_model_sha256(handle))
                current = read_current_simulation_frame(
                    handle, begun["source_run_id"]
                )
                self.assertEqual(current["current_frame"], live_before)
                summaries.append(
                    {
                        "episode_id": episode_id,
                        "passage_id": passage["passage_id"],
                        "times_us": times,
                        "public_state_count": len(public_states),
                        "model_sha256": model_before,
                    }
                )
            finally:
                self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        f1_handle, f1 = _begin("practice.f1.place-and-cancel.v1")
        try:
            cleanup, unavailable, _ = _acquire(f1_handle, f1)
            self.assertIsNone(cleanup)
            self.assertEqual(unavailable["status"], "UNAVAILABLE")
            self.assertEqual(
                unavailable["unavailable_reason"], "EPISODE_PASSAGE_UNSUPPORTED"
            )
        finally:
            self.assertEqual(release_simulation_episode(f1_handle)["status"], "CLOSED")
        _CASES.append(
            {
                "case_id": "P01_REAL_PUBLIC_PASSAGE",
                "capability_catalog_id": capabilities["catalog_id"],
                "passages": summaries,
                "f1_resolution": "USER_APPROVAL_REQUIRED_FOR_UNTIMED_CONTROL_EXCEPTION",
            }
        )

    def test_strict_boundary_rejects_mismatch_order_post_cut_and_incomplete_data(self) -> None:
        handle, begun = _begin("practice.f2.public-pressure.v1")
        try:
            cleanup, passage, request = _acquire(handle, begun)
            self.assertIsNone(cleanup)
            self.assertEqual(passage["status"], "AVAILABLE")

            hostile = copy.deepcopy(request)
            hostile["episode_recipe_sha256"] = "0" * 64
            _reidentify_request(hostile)
            no_cleanup, mismatch = acquire_simulation_practice_observation_passage(
                handle, hostile
            )
            self.assertIsNone(no_cleanup)
            self.assertEqual(mismatch["unavailable_reason"], "ATTEMPT_IDENTITY_MISMATCH")

            reversed_passage = copy.deepcopy(passage)
            reversed_passage["observations"].reverse()
            _reidentify(reversed_passage)
            with self.assertRaises(ValueError):
                PracticeObservationPassageResultV1.from_dict(reversed_passage)

            duplicate = copy.deepcopy(passage)
            duplicate["observations"].append(
                copy.deepcopy(duplicate["observations"][-1])
            )
            _reidentify(duplicate)
            with self.assertRaises(ValueError):
                PracticeObservationPassageResultV1.from_dict(duplicate)

            incomplete = copy.deepcopy(passage)
            incomplete["observations"].pop()
            _reidentify(incomplete)
            with self.assertRaises(Exception):
                PracticeObservationPassageResultV1.from_dict(incomplete)

            post_cut = copy.deepcopy(passage)
            later_frame = _retime_frame(
                post_cut["observations"][-1]["frame"], 1_250_000
            )
            post_cut["observations"].append(
                build_practice_passage_observation(
                    later_frame,
                    observation_sequence=len(post_cut["observations"]) + 1,
                    tie_breaker=0,
                )
            )
            _reidentify(post_cut)
            with self.assertRaises(Exception):
                PracticeObservationPassageResultV1.from_dict(post_cut)

            wrong_terminal = copy.deepcopy(passage)
            wrong_terminal["final_cut"]["terminal_frame_projection_sha256"] = "0" * 64
            _reidentify(wrong_terminal)
            with self.assertRaises(Exception):
                PracticeObservationPassageResultV1.from_dict(wrong_terminal)

            substituted_live_cut = copy.deepcopy(passage)
            substituted_live_cut["final_cut"]["frame_id"] = passage["observations"][-1][
                "frame"
            ]["frame_id"]
            substituted_live_cut["final_cut"]["cursor_id"] = passage["observations"][-1][
                "frame"
            ]["cursor"]["cursor_id"]
            _reidentify(substituted_live_cut)
            with self.assertRaises(Exception):
                PracticeObservationPassageResultV1.from_dict(
                    substituted_live_cut,
                    request=PracticeObservationPassageRequestV1.from_dict(request),
                )

            wrong_schema = copy.deepcopy(request)
            wrong_schema["schema_version"] = True
            with self.assertRaises(ValueError):
                PracticeObservationPassageRequestV1.from_dict(wrong_schema)
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        _CASES.append(
            {
                "case_id": "P01_STRICT_ORDER_CUT_AND_BINDINGS",
                "rejections": [
                    "RECIPE_MISMATCH",
                    "REVERSED",
                    "DUPLICATE_PAIR",
                    "INCOMPLETE",
                    "POST_CUT",
                    "TERMINAL_MISMATCH",
                    "SUBSTITUTED_LIVE_CUT_AUTHORITY",
                    "BOOLEAN_SCHEMA_VERSION",
                ],
            }
        )

    def test_failures_settle_transient_or_return_exact_cleanup_owner(self) -> None:
        from kirby2.ui import simulation_practice_passage_facade as facade

        scenarios: list[dict[str, object]] = []
        for name, patcher in (
            (
                "ACQUISITION",
                patch.object(facade, "start_simulation_run", side_effect=RuntimeError("start")),
            ),
            (
                "PARTIAL_CONSTRUCTION",
                patch.object(
                    facade,
                    "build_practice_passage_observation",
                    side_effect=[
                        RuntimeError("projection")
                    ],
                ),
            ),
        ):
            handle, begun = _begin("practice.f2.public-pressure.v1")
            live = copy.deepcopy(begun["current_frame"])
            try:
                request = build_simulation_practice_observation_passage_request(begun)
                with patcher:
                    cleanup, result = acquire_simulation_practice_observation_passage(
                        handle, request
                    )
                self.assertIsNone(cleanup)
                self.assertEqual(result["unavailable_reason"], "RECONSTRUCTION_FAILED")
                if name == "PARTIAL_CONSTRUCTION":
                    self.assertEqual(
                        result["cleanup_disposition"],
                        "TRANSIENT_RECONSTRUCTION_CLOSED",
                    )
                current = read_current_simulation_frame(
                    handle, begun["source_run_id"]
                )
                self.assertEqual(current["current_frame"], live)
                scenarios.append(
                    {
                        "scenario": name,
                        "reason": result["unavailable_reason"],
                        "cleanup": result["cleanup_disposition"],
                    }
                )
            finally:
                self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        handle, begun = _begin("practice.f2.public-pressure.v1")
        try:
            request = build_simulation_practice_observation_passage_request(begun)
            original_builder = facade.build_practice_passage_result

            def publication_failure(**fields: object) -> dict[str, object]:
                if fields["status"] == "AVAILABLE":
                    raise RuntimeError("publication")
                return original_builder(**fields)

            with patch.object(
                facade, "build_practice_passage_result", side_effect=publication_failure
            ):
                cleanup, result = acquire_simulation_practice_observation_passage(
                    handle, request
                )
            self.assertIsNone(cleanup)
            self.assertEqual(result["unavailable_reason"], "RESULT_PUBLICATION_FAILED")
            self.assertEqual(
                result["cleanup_disposition"], "TRANSIENT_RECONSTRUCTION_CLOSED"
            )
            scenarios.append(
                {
                    "scenario": "PUBLICATION",
                    "reason": result["unavailable_reason"],
                    "cleanup": result["cleanup_disposition"],
                }
            )
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        handle, begun = _begin("practice.f2.public-pressure.v1")
        try:
            request = build_simulation_practice_observation_passage_request(begun)
            with patch.object(
                facade,
                "episode_prefix_projection_sha256",
                side_effect=["0" * 64, "1" * 64],
            ):
                cleanup, result = acquire_simulation_practice_observation_passage(
                    handle, request
                )
            self.assertIsNone(cleanup)
            self.assertEqual(
                result["unavailable_reason"], "TERMINAL_PROJECTION_MISMATCH"
            )
            self.assertEqual(
                result["cleanup_disposition"], "TRANSIENT_RECONSTRUCTION_CLOSED"
            )
            scenarios.append(
                {
                    "scenario": "PROJECTION_REFUSAL",
                    "reason": result["unavailable_reason"],
                    "cleanup": result["cleanup_disposition"],
                }
            )
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        handle, begun = _begin("practice.f2.public-pressure.v1")
        cleanup_owner: object | None = None
        try:
            request = build_simulation_practice_observation_passage_request(begun)
            with patch.object(
                facade,
                "close_simulation_run",
                side_effect=RuntimeError("cleanup not confirmed"),
            ):
                cleanup_owner, result = acquire_simulation_practice_observation_passage(
                    handle, request
                )
            self.assertIsNotNone(cleanup_owner)
            self.assertEqual(
                result["unavailable_reason"], "TRANSIENT_CLEANUP_UNCONFIRMED"
            )
            self.assertEqual(result["resource_ownership"], "CALLER_OWNS_CLEANUP_HANDLE")
            self.assertEqual(release_simulation_episode(cleanup_owner)["status"], "CLOSED")
            cleanup_owner = None
            scenarios.append(
                {
                    "scenario": "CLEANUP_UNCONFIRMED",
                    "reason": result["unavailable_reason"],
                    "cleanup": "CALLER_SETTLED_EXACT_RETURNED_HANDLE",
                }
            )
        finally:
            if cleanup_owner is not None:
                release_simulation_episode(cleanup_owner)
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append(
            {
                "case_id": "P02_RESOURCE_AND_LIVE_MODEL_CONSERVATION",
                "scenarios": scenarios,
            }
        )

    def test_pace_metadata_does_not_change_semantics_or_verified_replay(self) -> None:
        proofs: list[dict[str, object]] = []
        for pace in (1_000_000, 500_000):
            handle, begun = _begin(
                "practice.f3.cancel-partial-residual.v1",
                pace_multiplier_ppm=pace,
                mode="UNASSISTED",
            )
            cleanup, passage, _ = _acquire(handle, begun)
            self.assertIsNone(cleanup)
            self.assertEqual(passage["status"], "AVAILABLE")
            acted = submit_simulation_practice_action(
                handle, _action(begun, "PLAYER_CANCEL_NEAREST")
            )
            self.assertEqual(acted["assessment"]["outcome"], "PASS")
            frame = acted["current_frame"]
            model_sha256 = _prepared_episode_model_sha256(handle)
            public_sha256 = canonical_digest(
                episode_prefix_projection(SimulationFrameV1.from_dict(frame))
            )
            finalized = finalize_simulation_run(
                handle,
                frame["source_run_id"],
                frame["frame_id"],
                frame["cursor"]["cursor_id"],
                "ALLOW_PARTIAL",
            )
            self.assertEqual(finalized["status"], "AVAILABLE")
            _, receipt = resolve_replay_artifact(
                finalized["run_result"]["replay_artifact"]
            )
            self.assertEqual(receipt["status"], "AVAILABLE")
            proofs.append(
                {
                    "pace_multiplier_ppm": pace,
                    "public_sha256": public_sha256,
                    "full_model_sha256": model_sha256,
                    "assessment": _assessment_semantics(acted["assessment"]),
                    "account": frame["account"],
                    "working_orders": frame["working_orders"],
                    "replay_receipt_status": receipt["status"],
                }
            )
        for field in (
            "public_sha256",
            "full_model_sha256",
            "assessment",
            "account",
            "working_orders",
            "replay_receipt_status",
        ):
            self.assertEqual(proofs[0][field], proofs[1][field])
        _CASES.append(
            {
                "case_id": "P06_PACED_ATTEMPT_SEMANTIC_REPLAY_INVARIANCE",
                "proofs": proofs,
                "scope": "ONE_FROZEN_F3_CUT_AND_ONE_POST_CUT_ACTION",
            }
        )


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            SimulationPracticePassageAudit
        )
    )
    print(
        json.dumps(
            {
                "schema_id": "KIRBY2_SIMULATION_PRACTICE_PASSAGE_AUDIT_V1",
                "schema_version": 1,
                "status": "PASS" if result.wasSuccessful() else "FAIL",
                "test_count": result.testsRun,
                "cases": _CASES,
            },
            sort_keys=True,
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
