"""Public-boundary audit for Packet A prepared simulation episodes.

Run with ``python3 -m kirby2.audit.simulation_episode``. The audit uses public
episode operations and one backend-private opaque commitment helper for final-state
comparison; it never reads an opaque run handle's session, event tape, or
Replay-store internals.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.ui import (
    SimulationEpisodeIdentityV1,
    SimulationEpisodePreparationRequestV1,
    SimulationEpisodePreparedResultV1,
    SimulationEpisodeRefusalV1,
    SimulationEpisodeVerificationV1,
    SimulationFrameV1,
    SimulationStartResultV1,
    build_simulation_episode_preparation_request,
    dispatch_simulation_command,
    episode_prefix_projection_sha256,
    finalize_simulation_run,
    prepare_simulation_episode,
    release_simulation_episode,
    resolve_replay_artifact,
    resolve_simulation_profile,
    start_simulation_run,
    verify_prepared_simulation_episode,
)
from kirby2.ui.simulation_run_facade import _prepared_episode_model_sha256


_FIXTURE_ROOT = Path(__file__).parents[1] / "ui" / "fixtures" / "simulation_contract_v1"
_BASE_ACTIONS = ("SIMULATION_PLAY", "PLAYER_BUY_MARKET", "PLAYER_BUY_BID")
_CASE_RECORDS: list[dict[str, object]] = []


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


def _fixture(name: str) -> dict[str, object]:
    payload = json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"fixture {name} is not an object")
    return payload


def _request(*, actions: tuple[str, ...] = _BASE_ACTIONS) -> dict[str, object]:
    training = _fixture("simulation_training_options.json")
    training["initial_run_state"] = "READY"
    return build_simulation_episode_preparation_request(
        episode_id="practice.control-residual.v1",
        episode_version=1,
        resolution_payload=resolve_simulation_profile(_fixture("profile_selection.json")),
        training_options_payload=training,
        prefix_actions=actions,
        anchor_time_us=1,
    )


def _cursor(frame: dict[str, object]) -> dict[str, object]:
    cursor = frame["cursor"]
    if not isinstance(cursor, dict):
        raise AssertionError("public frame cursor is not an object")
    return cursor


def _command(frame: dict[str, object], action: str) -> dict[str, object]:
    cursor = _cursor(frame)
    basis = {
        "schema_id": "KIRBY2_SIMULATION_COMMAND_REQUEST_V1",
        "schema_version": 1,
        "source_run_id": frame["source_run_id"],
        "origin_frame_id": frame["frame_id"],
        "origin_cursor_id": cursor["cursor_id"],
        "semantic_action_id": action,
        "parameters": {},
    }
    return {**basis, "command_id": f"simulation-command-{_canonical_digest(basis)[:24]}"}


def _nonzero_ready_frame(frame: dict[str, object]) -> dict[str, object]:
    """Make the lifecycle-hostile frame canonical except for READY at T=1."""

    hostile = copy.deepcopy(frame)
    cursor = _cursor(hostile)
    cursor["simulation_time_us"] = 1
    cursor_basis = {key: value for key, value in cursor.items() if key != "cursor_id"}
    cursor["cursor_id"] = f"simulation-cursor-{_canonical_digest(cursor_basis)[:24]}"
    frame_basis = {key: value for key, value in hostile.items() if key != "frame_id"}
    hostile["frame_id"] = f"simulation-frame-{_canonical_digest(frame_basis)[:24]}"
    return hostile


def _recalculate_identity(identity: dict[str, object]) -> None:
    basis = {key: value for key, value in identity.items() if key != "recipe_sha256"}
    identity["recipe_sha256"] = _canonical_digest(basis)


def _recalculate_prepared_result(prepared: dict[str, object]) -> None:
    basis = {key: value for key, value in prepared.items() if key != "result_id"}
    prepared["result_id"] = (
        f"simulation-episode-prepared-result-{_canonical_digest(basis)[:24]}"
    )


def _rebound_prepared_result(
    prepared: dict[str, object],
    kind: str,
) -> dict[str, object]:
    forged = copy.deepcopy(prepared)
    identity = forged["identity"]
    if not isinstance(identity, dict):
        raise AssertionError("available prepared result has no identity")
    if kind == "episode_id":
        identity["episode_id"] = "forged.other.episode"
        _recalculate_identity(identity)
    elif kind == "episode_version":
        identity["episode_version"] = 2
        _recalculate_identity(identity)
    elif kind == "request_id":
        forged["request_id"] = f"simulation-episode-request-{_canonical_digest({'forged': kind})[:24]}"
    elif kind == "timing_policy":
        identity["prefix_timing_policy"] = "FORGED_TIMING_POLICY_V1"
        _recalculate_identity(identity)
    elif kind == "prefix_identity":
        actions = ["SIMULATION_PLAY", "PLAYER_BUY_BID", "PLAYER_BUY_MARKET"]
        forged["prefix_actions"] = actions
        identity["prefix_sha256"] = _canonical_digest(actions)
        _recalculate_identity(identity)
        current = forged["current_frame"]
        if not isinstance(current, dict):
            raise AssertionError("available prepared result has no current frame")
        forged["prefix_projection_sha256"] = episode_prefix_projection_sha256(
            SimulationFrameV1.from_dict(current), actions
        )
    else:
        raise AssertionError(f"unknown forged binding kind: {kind}")
    _recalculate_prepared_result(forged)
    return forged


def _destination(command_result: dict[str, object]) -> dict[str, object]:
    if command_result.get("status") != "AVAILABLE":
        raise AssertionError(f"command unavailable: {command_result}")
    outcome = command_result.get("outcome")
    destination = command_result.get("destination_frame")
    if not isinstance(outcome, dict) or outcome.get("accepted") is not True:
        raise AssertionError(f"command rejected: {command_result}")
    if not isinstance(destination, dict):
        raise AssertionError("available command has no destination")
    return destination


def _finalize_and_verify(handle: object, frame: dict[str, object]) -> dict[str, object]:
    cursor = _cursor(frame)
    finalized = finalize_simulation_run(
        handle,
        str(frame["source_run_id"]),
        str(frame["frame_id"]),
        str(cursor["cursor_id"]),
        "ALLOW_PARTIAL",
    )
    if finalized.get("status") != "AVAILABLE":
        raise AssertionError(f"partial finalization failed: {finalized}")
    run_result = finalized.get("run_result")
    if not isinstance(run_result, dict) or not isinstance(run_result.get("replay_artifact"), dict):
        raise AssertionError("available finalization has no Replay artifact")
    _, receipt = resolve_replay_artifact(run_result["replay_artifact"])
    if receipt.get("status") != "AVAILABLE":
        raise AssertionError(f"Replay artifact did not verify: {receipt}")
    return {
        "finalize_result_id": finalized["result_id"],
        "run_result_id": run_result["result_id"],
        "verified_replay_artifact_sha256": receipt["verified_artifact_sha256"],
    }


class SimulationEpisodeAudit(unittest.TestCase):
    maxDiff = None

    def _prepare(self) -> tuple[object, dict[str, object]]:
        handle, result = prepare_simulation_episode(_request())
        self.assertIsNotNone(handle)
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(verify_prepared_simulation_episode(handle, result)["status"], "MATCH")
        return handle, result

    def test_control_residual_prepares_independent_equivalent_cuts(self) -> None:
        left_handle, left = self._prepare()
        right_handle, right = self._prepare()
        try:
            self.assertNotEqual(
                left["current_frame"]["source_run_id"], right["current_frame"]["source_run_id"]
            )
            self.assertEqual(left["prefix_projection_sha256"], right["prefix_projection_sha256"])
            self.assertEqual(left["full_model_prefix_sha256"], right["full_model_prefix_sha256"])
            for result in (left, right):
                frame = result["current_frame"]
                self.assertEqual(frame["cursor"]["run_state"], "PAUSED")
                self.assertEqual(frame["cursor"]["simulation_time_us"], 1)
                self.assertEqual(frame["account"]["position"], 100)
                self.assertEqual(frame["account"]["working_order_count"], 1)
                self.assertEqual(len(frame["working_orders"]), 1)
            _CASE_RECORDS.append(
                {
                    "case_id": "A01_CONTROL_RESIDUAL_PREPARE",
                    "recipe_id": left["identity"]["recipe_sha256"],
                    "left_result_id": left["result_id"],
                    "right_result_id": right["result_id"],
                    "prefix_projection_sha256": left["prefix_projection_sha256"],
                    "full_model_prefix_sha256": left["full_model_prefix_sha256"],
                    "cleanup": "USER_ABANDONED",
                }
            )
        finally:
            self.assertEqual(release_simulation_episode(left_handle)["status"], "CLOSED")
            self.assertEqual(release_simulation_episode(right_handle)["status"], "CLOSED")

    def test_continuation_finalizes_and_verifies_from_each_independent_cut(self) -> None:
        proofs: list[dict[str, object]] = []
        for label in ("left", "right"):
            handle, prepared = self._prepare()
            command_result = dispatch_simulation_command(
                handle, _command(prepared["current_frame"], "PLAYER_CANCEL_NEAREST")
            )
            frame = _destination(command_result)
            outcome = command_result["outcome"]
            self.assertIsInstance(outcome, dict)
            final_full_model_sha256 = _prepared_episode_model_sha256(handle)
            proof = _finalize_and_verify(handle, frame)
            proofs.append(
                {
                    "label": label,
                    "semantic_outcome": outcome,
                    "final_full_model_sha256": final_full_model_sha256,
                    **proof,
                }
            )
        self.assertNotEqual(proofs[0]["run_result_id"], proofs[1]["run_result_id"])
        self.assertEqual(proofs[0]["semantic_outcome"], proofs[1]["semantic_outcome"])
        self.assertEqual(
            proofs[0]["final_full_model_sha256"], proofs[1]["final_full_model_sha256"]
        )
        _CASE_RECORDS.append(
            {
                "case_id": "A02_CONTINUATION_REPLAY_VERIFICATION",
                "continuations": proofs,
                "cleanup": "FINALIZED_TO_REPLAY",
            }
        )

    def test_prepared_identity_rebinding_and_boolean_schema_versions_are_refused(self) -> None:
        handle, prepared = self._prepare()
        try:
            hostile_results: list[dict[str, object]] = []
            for kind in (
                "episode_id",
                "episode_version",
                "request_id",
                "timing_policy",
                "prefix_identity",
            ):
                verification = verify_prepared_simulation_episode(
                    handle, _rebound_prepared_result(prepared, kind)
                )
                self.assertEqual(verification["status"], "MISMATCH")
                expected = (
                    "INVALID_PREPARED_RESULT"
                    if kind == "timing_policy"
                    else "PREPARED_IDENTITY_MISMATCH"
                )
                self.assertEqual(verification["reason"], expected)
                hostile_results.append(
                    {
                        "kind": kind,
                        "verification_id": verification["verification_id"],
                        "reason": verification["reason"],
                    }
                )

            request = _request()
            refusal = SimulationEpisodeRefusalV1(
                "INVALID_REQUEST", "hostile schema-version coverage"
            ).as_dict()
            verification = verify_prepared_simulation_episode(handle, prepared)
            records_and_decoders = (
                (prepared["identity"], SimulationEpisodeIdentityV1),
                (request, SimulationEpisodePreparationRequestV1),
                (refusal, SimulationEpisodeRefusalV1),
                (prepared, SimulationEpisodePreparedResultV1),
                (verification, SimulationEpisodeVerificationV1),
            )
            for record, decoder in records_and_decoders:
                hostile = copy.deepcopy(record)
                hostile["schema_version"] = True
                with self.assertRaisesRegex(ValueError, "schema is unsupported"):
                    decoder.from_dict(hostile)
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        request = _request()
        invalid_start_handle, _ = start_simulation_run(
            request["resolution"], request["training_options"]
        )
        self.assertIsNotNone(invalid_start_handle)
        with patch(
            "kirby2.ui.simulation_episode_facade.start_simulation_run",
            return_value=(invalid_start_handle, {}),
        ):
            invalid_cleanup_handle, invalid_cleanup_refusal = prepare_simulation_episode(request)
        self.assertIsNone(invalid_cleanup_handle)
        self.assertEqual(invalid_cleanup_refusal["status"], "REFUSED")
        self.assertEqual(invalid_cleanup_refusal["refusal"]["reason_code"], "START_REFUSED")
        self.assertEqual(invalid_cleanup_refusal["resource_ownership"], "NO_RESOURCE")
        self.assertEqual(invalid_cleanup_refusal["cleanup_disposition"], "USER_ABANDONED")

        refused_start_handle, _ = start_simulation_run(
            request["resolution"], request["training_options"]
        )
        self.assertIsNotNone(refused_start_handle)
        with patch(
            "kirby2.ui.simulation_episode_facade.start_simulation_run",
            return_value=(refused_start_handle, _fixture("simulation_start_refused.json")),
        ):
            refused_cleanup_handle, refused_cleanup = prepare_simulation_episode(request)
        self.assertIsNone(refused_cleanup_handle)
        self.assertEqual(refused_cleanup["status"], "REFUSED")
        self.assertEqual(refused_cleanup["refusal"]["reason_code"], "START_REFUSED")
        self.assertEqual(refused_cleanup["resource_ownership"], "NO_RESOURCE")
        self.assertEqual(refused_cleanup["cleanup_disposition"], "USER_ABANDONED")

        malformed_close_handle, _ = start_simulation_run(
            request["resolution"], request["training_options"]
        )
        self.assertIsNotNone(malformed_close_handle)
        with (
            patch(
                "kirby2.ui.simulation_episode_facade.start_simulation_run",
                return_value=(malformed_close_handle, {}),
            ),
            patch(
                "kirby2.ui.simulation_episode_facade.close_simulation_run",
                return_value={"status": "CLOSED"},
            ),
        ):
            retained_handle, cleanup_unconfirmed = prepare_simulation_episode(request)
        self.assertIs(retained_handle, malformed_close_handle)
        self.assertEqual(cleanup_unconfirmed["status"], "REFUSED")
        self.assertEqual(
            cleanup_unconfirmed["refusal"]["reason_code"], "CLEANUP_UNCONFIRMED"
        )
        self.assertEqual(
            cleanup_unconfirmed["resource_ownership"], "CALLER_OWNS_CLEANUP_HANDLE"
        )
        self.assertEqual(
            release_simulation_episode(retained_handle)["status"], "CLOSED"
        )
        _CASE_RECORDS.append(
            {
                "case_id": "A04_IDENTITY_REBIND_SCHEMA_AND_START_CLEANUP",
                "identity_rebinds": hostile_results,
                "boolean_schema_version_rejections": 5,
                "invalid_start_cleanup_result_id": invalid_cleanup_refusal["result_id"],
                "nonavailable_start_cleanup_result_id": refused_cleanup["result_id"],
                "malformed_close_retained_handle_result_id": cleanup_unconfirmed["result_id"],
                "cleanup": "USER_ABANDONED",
            }
        )

    def test_bad_requests_cleanup_and_full_model_dependency_mismatch(self) -> None:
        malformed = copy.deepcopy(_request())
        malformed["identity"]["anchor_time_us"] = 2
        handle, refused = prepare_simulation_episode(malformed)
        self.assertIsNone(handle)
        self.assertEqual(refused["status"], "REFUSED")
        self.assertEqual(refused["refusal"]["reason_code"], "INVALID_REQUEST")
        self.assertEqual(refused["resource_ownership"], "NO_RESOURCE")
        self.assertIsNone(refused["cleanup_disposition"])

        handle, rejected = prepare_simulation_episode(
            _request(actions=("SIMULATION_PLAY", "SIMULATION_PAUSE"))
        )
        self.assertIsNone(handle)
        self.assertEqual(rejected["status"], "REFUSED")
        self.assertEqual(rejected["refusal"]["reason_code"], "ANCHOR_INVALID")
        self.assertEqual(rejected["resource_ownership"], "NO_RESOURCE")
        self.assertEqual(rejected["cleanup_disposition"], "USER_ABANDONED")

        handle, prepared = self._prepare()
        try:
            changed = _destination(
                dispatch_simulation_command(handle, _command(prepared["current_frame"], "PLAYER_INCREASE_QUANTITY"))
            )
            changed = _destination(
                dispatch_simulation_command(handle, _command(changed, "PLAYER_DECREASE_QUANTITY"))
            )
            self.assertEqual(changed["book"], prepared["current_frame"]["book"])
            self.assertEqual(changed["account"], prepared["current_frame"]["account"])
            self.assertEqual(changed["working_orders"], prepared["current_frame"]["working_orders"])

            adversarial = copy.deepcopy(prepared)
            adversarial["current_frame"] = changed
            adversarial["prefix_projection_sha256"] = episode_prefix_projection_sha256(
                SimulationFrameV1.from_dict(changed), adversarial["prefix_actions"]
            )
            adversarial_basis = {key: value for key, value in adversarial.items() if key != "result_id"}
            adversarial["result_id"] = (
                f"simulation-episode-prepared-result-{_canonical_digest(adversarial_basis)[:24]}"
            )
            verification = verify_prepared_simulation_episode(handle, adversarial)
            self.assertEqual(verification["status"], "MISMATCH")
            self.assertEqual(verification["reason"], "FULL_MODEL_PREFIX_MISMATCH")
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        start_fixture = _fixture("simulation_start_available.json")
        parsed_start = SimulationStartResultV1.from_dict(start_fixture)
        self.assertEqual(parsed_start.initial_frame.as_dict()["cursor"]["simulation_time_us"], 0)
        self.assertEqual(parsed_start.initial_frame.as_dict()["cursor"]["run_state"], "READY")
        hostile_frame = _nonzero_ready_frame(start_fixture["initial_frame"])
        with self.assertRaisesRegex(ValueError, "state and time disagree"):
            SimulationFrameV1.from_dict(hostile_frame)
        hostile_start = copy.deepcopy(start_fixture)
        hostile_start["initial_frame"] = hostile_frame
        hostile_start_basis = {key: value for key, value in hostile_start.items() if key != "result_id"}
        hostile_start["result_id"] = (
            f"simulation-start-result-{_canonical_digest(hostile_start_basis)[:24]}"
        )
        with self.assertRaisesRegex(ValueError, "state and time disagree"):
            SimulationStartResultV1.from_dict(hostile_start)
        _CASE_RECORDS.append(
            {
                "case_id": "A03_BAD_REQUEST_CLEANUP_AND_HIDDEN_DEPENDENCY",
                "invalid_request_result_id": refused["result_id"],
                "post_allocation_cleanup_result_id": rejected["result_id"],
                "dependency_verification_id": verification["verification_id"],
                "dependency_reason": verification["reason"],
                "v1_start_fixture_result_id": parsed_start.result_id,
                "v1_nonzero_ready_rejection": "READY_TIME_INVARIANT",
                "cleanup": "REFUSED_NO_RESOURCE_OR_USER_ABANDONED",
            }
        )


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SimulationEpisodeAudit)
    )
    print(
        json.dumps(
            {
                "schema_id": "KIRBY2_SIMULATION_EPISODE_AUDIT_V1",
                "schema_version": 1,
                "status": "PASS" if result.wasSuccessful() else "FAIL",
                "test_count": result.testsRun,
                "cases": _CASE_RECORDS,
            },
            sort_keys=True,
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
