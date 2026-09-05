"""No-Qt public-boundary audit for the Chapter 1 decision repeater."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from kirby2.curriculum.practice_assessment import assess_response, classify_public_reading
from kirby2.curriculum.practice_episodes import (
    PRACTICE_EPISODES_V1,
    PracticeEpisodeDefinitionV1,
    get_practice_episode_v1,
    list_practice_episodes_v1,
)
from kirby2.ui import (
    PracticeCatalogV1,
    PracticeActionRequestV1,
    PracticeResultV1,
    advance_simulation_run,
    begin_simulation_practice_attempt,
    build_practice_action_request,
    build_practice_attempt_request,
    build_simulation_episode_preparation_request,
    dispatch_simulation_command,
    list_simulation_practice_episodes,
    list_simulation_profiles,
    list_simulation_training_resources,
    release_simulation_episode,
    resolve_simulation_profile,
    submit_simulation_practice_action,
)


_CASES: list[dict[str, object]] = []


def _action(result: dict[str, object], operation: str, kind: str, action: str | None = None) -> dict[str, object]:
    frame = result["current_frame"]
    cursor = frame["cursor"]
    return build_practice_action_request(
        attempt_id=result["attempt"]["attempt_id"], hold_id=result["hold_id"],
        source_run_id=frame["source_run_id"], origin_frame_id=frame["frame_id"],
        origin_cursor_id=cursor["cursor_id"], operation=operation, response_kind=kind,
        semantic_action_id=action,
    )


def _command(frame: dict[str, object], action: str) -> dict[str, object]:
    from kirby2.ui.simulation_contract import canonical_digest

    cursor = frame["cursor"]
    basis = {
        "schema_id": "KIRBY2_SIMULATION_COMMAND_REQUEST_V1", "schema_version": 1,
        "source_run_id": frame["source_run_id"], "origin_frame_id": frame["frame_id"],
        "origin_cursor_id": cursor["cursor_id"], "semantic_action_id": action, "parameters": {},
    }
    return {**basis, "command_id": f"simulation-command-{canonical_digest(basis)[:24]}"}


def _reidentify_practice_result(record: dict[str, object]) -> None:
    from kirby2.ui.simulation_contract import canonical_digest

    basis = {key: value for key, value in record.items() if key != "result_id"}
    record["result_id"] = f"practice-result-{canonical_digest(basis)[:24]}"


def _reidentify_recipe(record: dict[str, object]) -> None:
    from kirby2.ui.simulation_contract import canonical_digest

    basis = {key: value for key, value in record.items() if key != "recipe_sha256"}
    record["recipe_sha256"] = canonical_digest(basis)


def _reidentify_catalog(record: dict[str, object]) -> None:
    from kirby2.ui.simulation_contract import canonical_digest

    basis = {"schema_id": record["schema_id"], "schema_version": record["schema_version"], "episodes": record["episodes"]}
    record["catalog_id"] = f"practice-catalog-{canonical_digest(basis)[:24]}"


def _temporary_public_cut(profile_id: str) -> tuple[object, dict[str, object]]:
    profile = next(row for row in list_simulation_profiles()["profiles"] if row["profile_ref"]["profile_id"] == profile_id)
    defaults = profile["defaults"]
    controls = {row["control_id"]: row["default_value"] for row in profile["controls"]}
    resolution = resolve_simulation_profile({
        "schema_id": "KIRBY2_SIMULATION_PROFILE_SELECTION_V1", "schema_version": 1,
        "profile_ref": profile["profile_ref"], "seed": defaults["seed"],
        "duration_us": defaults["duration_us"], "control_values": controls,
    })
    resources = list_simulation_training_resources()["defaults"]
    training = {
        "schema_id": "KIRBY2_SIMULATION_TRAINING_OPTIONS_V1", "schema_version": 1,
        "quantity_options": resources["quantity_options"], "initial_quantity": resources["initial_quantity"],
        "layout_ref": resources["layout_ref"], "strategy_ref": None, "objective": None,
        "curriculum_drill_ref": None, "initial_run_state": "READY",
        "observation_policy_ref": resources["observation_policy_ref"],
    }
    request = build_simulation_episode_preparation_request(
        episode_id="audit.public-reading-branch.v1", episode_version=1,
        resolution_payload=resolution, training_options_payload=training,
        prefix_actions=("SIMULATION_PLAY",), anchor_time_us=1_000_000,
    )
    handle, result = __import__("kirby2.ui", fromlist=["prepare_simulation_episode"]).prepare_simulation_episode(request)
    if handle is None or result["status"] != "AVAILABLE":
        raise AssertionError("temporary public rule cut did not prepare")
    return handle, result["current_frame"]


class SimulationPracticeAudit(unittest.TestCase):
    maxDiff = None

    def test_catalog_is_strict_detached_and_has_six_recipes(self) -> None:
        catalog = list_simulation_practice_episodes()
        self.assertEqual(len(catalog["episodes"]), 6)
        self.assertEqual(PracticeCatalogV1.from_dict(catalog).as_dict(), catalog)
        hostile = copy.deepcopy(catalog)
        hostile["schema_version"] = True
        with self.assertRaises(ValueError):
            PracticeCatalogV1.from_dict(hostile)
        hostile = copy.deepcopy(catalog)
        hostile["episodes"][0]["unknown"] = "hostile"
        with self.assertRaises(ValueError):
            PracticeCatalogV1.from_dict(hostile)
        catalog["episodes"][0]["title"] = "caller mutation"
        self.assertNotEqual(list_simulation_practice_episodes()["episodes"][0]["title"], "caller mutation")
        episode = get_practice_episode_v1("practice.f1.place-and-cancel.v1")
        self.assertIs(episode, PRACTICE_EPISODES_V1[0])
        self.assertIs(episode, list_practice_episodes_v1()[0])
        with self.assertRaises(TypeError):
            episode.profile_ref["profile_id"] = "hostile"
        with self.assertRaises(TypeError):
            PRACTICE_EPISODES_V1[0].control_values["relative_volume"] = "hostile"
        detached_recipe = episode.as_dict()
        detached_recipe["control_values"]["relative_volume"] = "caller-mutated"
        detached_recipe["observation_rule"]["quantity"] = 999
        self.assertEqual(episode.control_values["relative_volume"], "1.00x")
        self.assertEqual(episode.observation_rule["quantity"], 200)
        profile_ref = dict(episode.profile_ref)
        controls = dict(episode.control_values)
        preparation_actions = ["SIMULATION_PLAY"]
        expected_actions = ["PLAYER_INCREASE_QUANTITY", "PLAYER_BUY_BID", "PLAYER_CANCEL_NEAREST"]
        rule = dict(episode.observation_rule)
        constructed = PracticeEpisodeDefinitionV1(
            "practice.audit-immutable.v1", "F1_CONTROL", "Audit immutable recipe", None,
            profile_ref, 101, controls, 90_000_000, preparation_actions, 1,
            "CANCEL_TIMING", "EXACT_MECHANICAL", expected_actions, rule,
        )
        profile_ref["profile_id"] = "caller-mutated"
        controls["relative_volume"] = "caller-mutated"
        preparation_actions[0] = "PLAYER_BUY_BID"
        expected_actions[0] = "PLAYER_NOT_A_REAL_ACTION"
        rule["quantity"] = 999
        self.assertEqual(constructed.profile_ref["profile_id"], "accepted.balanced.simple")
        self.assertEqual(constructed.control_values["relative_volume"], "1.00x")
        self.assertEqual(constructed.preparation_actions, ("SIMULATION_PLAY",))
        self.assertEqual(constructed.expected_actions[0], "PLAYER_INCREASE_QUANTITY")
        self.assertEqual(constructed.observation_rule["quantity"], 200)
        hostile = copy.deepcopy(list_simulation_practice_episodes())
        hostile["episodes"][0]["expected_actions"] = ["PLAYER_NOT_A_REAL_ACTION"]
        _reidentify_recipe(hostile["episodes"][0])
        _reidentify_catalog(hostile)
        with self.assertRaises(ValueError):
            PracticeCatalogV1.from_dict(hostile)
        _CASES.append({"case_id": "B01_STRICT_DETACHED_SIX_RECIPE_CATALOG", "catalog_id": list_simulation_practice_episodes()["catalog_id"]})

    def test_measured_public_cuts_and_f2_declared_rule_branches(self) -> None:
        catalog = {row["episode_id"]: row for row in list_simulation_practice_episodes()["episodes"]}
        handles: list[object] = []
        try:
            results: dict[str, dict[str, object]] = {}
            for episode_id in catalog:
                handle, result = begin_simulation_practice_attempt(build_practice_attempt_request(
                    episode_id=episode_id, mode="UNASSISTED", pace_multiplier_ppm=500_000,
                ))
                self.assertIsNotNone(handle)
                self.assertEqual(result["status"], "AVAILABLE")
                handles.append(handle)
                results[episode_id] = result
            pressure = results["practice.f2.public-pressure.v1"]
            replenish = results["practice.f2.replenishment.v1"]
            self.assertEqual(classify_public_reading(get_practice_episode_v1(pressure["episode"]["episode_id"]), pressure["current_frame"])["classification"], "PRESSURE_PRESENT")
            self.assertEqual(classify_public_reading(get_practice_episode_v1(replenish["episode"]["episode_id"]), replenish["current_frame"])["classification"], "REPLENISHMENT_BLOCKS")
            partial = results["practice.f3.cancel-partial-residual.v1"]["current_frame"]
            self.assertGreater(partial["account"]["position"], 0)
            self.assertEqual(len(partial["working_orders"]), 1)
            self.assertGreater(partial["working_orders"][0]["filled_quantity"], 0)
            self.assertGreater(partial["working_orders"][0]["remaining_quantity"], 0)
        finally:
            for handle in handles:
                self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")

        recipe = get_practice_episode_v1("practice.f2.public-pressure.v1")
        no_handle, no_frame = _temporary_public_cut("accepted.thin_liquidity.simple")
        insufficient_handle, insufficient_frame = _temporary_public_cut("accepted.sell_pressure.simple")
        try:
            self.assertEqual(classify_public_reading(recipe, no_frame)["classification"], "NO_OPPORTUNITY")
            self.assertEqual(assess_response(recipe, no_frame, response_kind="WAIT", semantic_action_id=None, action_index=0)["outcome"], "NO_OPPORTUNITY")
            self.assertEqual(classify_public_reading(recipe, insufficient_frame)["classification"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(assess_response(recipe, insufficient_frame, response_kind="INSUFFICIENT_EVIDENCE", semantic_action_id=None, action_index=0)["outcome"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(assess_response(recipe, insufficient_frame, response_kind="WAIT", semantic_action_id=None, action_index=0)["outcome"], "FAIL")
        finally:
            self.assertEqual(release_simulation_episode(no_handle)["status"], "CLOSED")
            self.assertEqual(release_simulation_episode(insufficient_handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B02_MEASURED_F2_AND_SINGLE_ORDER_F3", "f3_order": partial["working_orders"][0]["order_id"], "f3_remaining": partial["working_orders"][0]["remaining_quantity"]})

    def test_guided_hold_stages_then_releases_exactly_once(self) -> None:
        handle, begun = begin_simulation_practice_attempt(build_practice_attempt_request(episode_id="practice.f1.place-and-cancel.v1"))
        self.assertIsNotNone(handle)
        try:
            frame = begun["current_frame"]
            self.assertEqual(dispatch_simulation_command(handle, _command(frame, "PLAYER_INCREASE_QUANTITY"))["unavailable_reason"], "GUIDED_HOLD_ACTIVE")
            self.assertEqual(advance_simulation_run(handle, frame["source_run_id"], frame["frame_id"], frame["cursor"]["cursor_id"], 2)["unavailable_reason"], "GUIDED_HOLD_ACTIVE")
            wrong = submit_simulation_practice_action(handle, _action(begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_CANCEL_NEAREST"))
            self.assertEqual(wrong["assessment"]["outcome"], "FAIL")
            self.assertEqual(wrong["current_frame"]["frame_id"], frame["frame_id"])
            staged = submit_simulation_practice_action(handle, _action(begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY"))
            self.assertEqual(staged["assessment"]["outcome"], "PASS")
            released = submit_simulation_practice_action(handle, _action(staged, "CONTINUE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY"))
            self.assertEqual(released["assessment"]["outcome"], "PASS")
            self.assertEqual(released["current_frame"]["account"]["selected_quantity"], 200)
            twice = submit_simulation_practice_action(handle, _action(staged, "CONTINUE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY"))
            self.assertEqual(twice["status"], "UNAVAILABLE")
            hostile = copy.deepcopy(released)
            hostile["assistance"][0]["unknown"] = True
            with self.assertRaises(ValueError):
                PracticeResultV1.from_dict(hostile)
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B03_GUIDED_HOLD_AND_EXACTLY_ONCE_RELEASE", "attempt_id": begun["attempt"]["attempt_id"]})

    def test_retry_is_fenced_and_all_nested_records_are_strict_detached(self) -> None:
        request = build_practice_attempt_request(
            episode_id="practice.f3.cancel-partial-residual.v1", pace_multiplier_ppm=500_000,
        )
        handle, begun = begin_simulation_practice_attempt(request)
        self.assertIsNotNone(handle)
        try:
            retry_handle, retry = begin_simulation_practice_attempt(request)
            self.assertIs(retry_handle, handle)
            self.assertEqual(retry["attempt"]["attempt_id"], begun["attempt"]["attempt_id"])
            self.assertEqual(retry["source_run_id"], begun["source_run_id"])
            stage = submit_simulation_practice_action(handle, _action(begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_CANCEL_NEAREST"))
            released = submit_simulation_practice_action(handle, _action(stage, "CONTINUE", "SEMANTIC_ACTION", "PLAYER_CANCEL_NEAREST"))
            self.assertEqual(released["assessment"]["outcome"], "PASS")
            self.assertEqual(released["debrief"]["evidence"]["order_ids"], ["PLAYER-O-000001"])
            progressed_handle, progressed = begin_simulation_practice_attempt(request)
            self.assertIs(progressed_handle, handle)
            self.assertEqual(progressed["status"], "DUPLICATE")
            self.assertEqual(progressed["operation"], "DUPLICATE")
            self.assertEqual(progressed["unavailable_reason"], "DUPLICATE_REQUEST_ALREADY_PROGRESSED")
            self.assertEqual(progressed["current_frame"]["frame_id"], released["current_frame"]["frame_id"])
            self.assertEqual(progressed["attempt"]["step_index"], released["attempt"]["step_index"])

            parsed = PracticeResultV1.from_dict(released)
            detached = parsed.as_dict()
            detached["assessment"]["evidence"]["order_ids"].append("caller-mutated")
            self.assertEqual(parsed.as_dict()["assessment"]["evidence"]["order_ids"], ["PLAYER-O-000001"])
            for path in ("attempt", "assistance", "assessment", "debrief"):
                hostile = copy.deepcopy(released)
                if path == "attempt":
                    hostile[path]["unknown"] = "hostile"
                elif path == "assistance":
                    hostile[path][0]["unknown"] = "hostile"
                elif path == "assessment":
                    hostile[path]["evidence"]["unknown"] = "hostile"
                else:
                    hostile[path]["unknown"] = "hostile"
                _reidentify_practice_result(hostile)
                with self.assertRaises(ValueError):
                    PracticeResultV1.from_dict(hostile)
            hostile = copy.deepcopy(released)
            hostile["episode"]["episode_id"] = "practice.f1.place-and-cancel.v1"
            _reidentify_practice_result(hostile)
            with self.assertRaises(Exception):
                PracticeResultV1.from_dict(hostile)
            hostile = copy.deepcopy(released)
            prepared_identity = hostile["attempt"]["prepared_identity"]
            prepared_identity["anchor_time_us"] = 2
            from kirby2.ui.simulation_contract import canonical_digest
            prepared_identity["recipe_sha256"] = canonical_digest({
                key: value for key, value in prepared_identity.items() if key != "recipe_sha256"
            })
            _reidentify_practice_result(hostile)
            with self.assertRaises(Exception):
                PracticeResultV1.from_dict(hostile)
            hostile = copy.deepcopy(released)
            hostile["schema_version"] = True
            with self.assertRaises(ValueError):
                PracticeResultV1.from_dict(hostile)
            hostile = copy.deepcopy(released)
            hostile["hold_id"] = "practice-guided-hold-000000000000000000000000"
            _reidentify_practice_result(hostile)
            with self.assertRaises(ValueError):
                PracticeResultV1.from_dict(hostile)
            invalid_wall = _action(stage, "CONTINUE", "SEMANTIC_ACTION", "PLAYER_CANCEL_NEAREST")
            invalid_wall["wall_time"] = {"source": "MONOTONIC_CALLER", "resolution_us": True, "elapsed_wall_time_us": 0}
            action_basis = {key: value for key, value in invalid_wall.items() if key != "request_id"}
            invalid_wall["request_id"] = f"practice-action-request-{canonical_digest(action_basis)[:24]}"
            with self.assertRaises(ValueError):
                PracticeActionRequestV1.from_dict(invalid_wall)
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B04_RETRY_FENCE_CAUSAL_DEBRIEF_AND_NESTED_HOSTILES", "attempt_id": begun["attempt"]["attempt_id"]})

    def test_settled_duplicate_is_tombstoned_without_new_source(self) -> None:
        from kirby2.ui.simulation_run_facade import _ISSUED_SOURCE_RUN_IDS

        request = build_practice_attempt_request(
            episode_id="practice.f1.place-and-replace.v1", pace_multiplier_ppm=500_000,
        )
        handle, begun = begin_simulation_practice_attempt(request)
        self.assertIsNotNone(handle)
        source_run_id = begun["source_run_id"]
        try:
            staged = submit_simulation_practice_action(handle, _action(
                begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_DECREASE_QUANTITY",
            ))
            self.assertEqual(staged["assessment"]["outcome"], "PASS")
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        count_before = len(_ISSUED_SOURCE_RUN_IDS)
        duplicate_handle, duplicate = begin_simulation_practice_attempt(request)
        self.assertIsNone(duplicate_handle)
        self.assertEqual(duplicate["request_id"], request["request_id"])
        self.assertEqual(duplicate["status"], "REFUSED")
        self.assertEqual(duplicate["unavailable_reason"], "DUPLICATE_REQUEST_SETTLED")
        self.assertIsNone(duplicate["attempt"])
        self.assertIsNone(duplicate["current_frame"])
        self.assertEqual(len(_ISSUED_SOURCE_RUN_IDS), count_before)
        fresh_begin_request = build_practice_attempt_request(
            episode_id="practice.f1.place-and-replace.v1", pace_multiplier_ppm=500_000,
        )
        self.assertNotEqual(fresh_begin_request["request_id"], request["request_id"])
        self.assertNotEqual(fresh_begin_request["operation_id"], request["operation_id"])
        fresh_handle, fresh = begin_simulation_practice_attempt(fresh_begin_request)
        try:
            self.assertIsNotNone(fresh_handle)
            self.assertEqual(fresh["status"], "AVAILABLE")
            self.assertNotEqual(fresh["source_run_id"], source_run_id)
        finally:
            self.assertEqual(release_simulation_episode(fresh_handle)["status"], "CLOSED")
        repeat_handle, repeat = begin_simulation_practice_attempt(build_practice_attempt_request(
            episode_id="practice.f1.place-and-replace.v1", operation="EXACT_REPEAT",
            prior_attempt_id=begun["attempt"]["attempt_id"], pace_multiplier_ppm=500_000,
        ))
        try:
            self.assertIsNotNone(repeat_handle)
            self.assertEqual(repeat["status"], "AVAILABLE")
            self.assertNotEqual(repeat["source_run_id"], source_run_id)
        finally:
            self.assertEqual(release_simulation_episode(repeat_handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B07_SETTLED_DUPLICATE_TOMBSTONE", "settled_source": source_run_id})

    def test_result_request_correlations_are_explicit_and_integral(self) -> None:
        refused_request = build_practice_attempt_request(
            episode_id="practice.f1.place-and-cancel.v1",
            operation="EXACT_REPEAT",
            prior_attempt_id="practice-attempt-000000000000000000000000",
        )
        refused_handle, refused = begin_simulation_practice_attempt(refused_request)
        self.assertIsNone(refused_handle)
        self.assertEqual(refused["status"], "REFUSED")
        self.assertEqual(refused["request_id"], refused_request["request_id"])
        self.assertEqual(PracticeResultV1.from_dict(refused).as_dict(), refused)

        begin_request = build_practice_attempt_request(
            episode_id="practice.f1.place-and-cancel.v1",
        )
        handle, begun = begin_simulation_practice_attempt(begin_request)
        self.assertIsNotNone(handle)
        try:
            self.assertEqual(begun["request_id"], begin_request["request_id"])
            hostile_begin = copy.deepcopy(begun)
            hostile_begin["request_id"] = refused_request["request_id"]
            _reidentify_practice_result(hostile_begin)
            with self.assertRaises(Exception):
                PracticeResultV1.from_dict(hostile_begin)

            stage_request = _action(
                begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY"
            )
            staged = submit_simulation_practice_action(handle, stage_request)
            self.assertEqual(staged["request_id"], stage_request["request_id"])
            hostile_action = copy.deepcopy(staged)
            hostile_action["request_id"] = (
                "practice-action-request-000000000000000000000000"
            )
            _reidentify_practice_result(hostile_action)
            with self.assertRaises(Exception):
                PracticeResultV1.from_dict(hostile_action)

            unavailable_request = copy.deepcopy(stage_request)
            unavailable_request["attempt_id"] = (
                "practice-attempt-000000000000000000000000"
            )
            from kirby2.ui.simulation_contract import canonical_digest

            unavailable_basis = {
                key: value
                for key, value in unavailable_request.items()
                if key != "request_id"
            }
            unavailable_request["request_id"] = (
                f"practice-action-request-{canonical_digest(unavailable_basis)[:24]}"
            )
            unavailable = submit_simulation_practice_action(
                handle, unavailable_request
            )
            self.assertEqual(unavailable["status"], "UNAVAILABLE")
            self.assertEqual(
                unavailable["request_id"], unavailable_request["request_id"]
            )
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append(
            {
                "case_id": "B09_RESULT_REQUEST_CORRELATION",
                "refused_request_id": refused_request["request_id"],
                "begin_request_id": begin_request["request_id"],
            }
        )

    def test_f2_staged_duplicate_retains_its_active_hold(self) -> None:
        request = build_practice_attempt_request(
            episode_id="practice.f2.public-pressure.v1", pace_multiplier_ppm=500_000,
        )
        handle, begun = begin_simulation_practice_attempt(request)
        self.assertIsNotNone(handle)
        try:
            staged = submit_simulation_practice_action(handle, _action(
                begun, "STAGE", "PRESSURE_PRESENT",
            ))
            self.assertEqual(staged["assessment"]["outcome"], "PASS")
            duplicate_handle, duplicate = begin_simulation_practice_attempt(request)
            self.assertIs(duplicate_handle, handle)
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(duplicate["hold_id"], staged["hold_id"])
            self.assertEqual(duplicate["current_frame"]["frame_id"], staged["current_frame"]["frame_id"])
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B08_F2_STAGED_DUPLICATE_ACTIVE_HOLD", "attempt_id": begun["attempt"]["attempt_id"]})

    def test_guided_dispatch_failure_reholds_without_consuming_stage(self) -> None:
        handle, begun = begin_simulation_practice_attempt(build_practice_attempt_request(
            episode_id="practice.f1.place-and-cancel.v1", pace_multiplier_ppm=500_000,
        ))
        self.assertIsNotNone(handle)
        try:
            staged = submit_simulation_practice_action(handle, _action(begun, "STAGE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY"))
            continue_request = _action(staged, "CONTINUE", "SEMANTIC_ACTION", "PLAYER_INCREASE_QUANTITY")
            with patch("kirby2.ui.simulation_practice_facade.dispatch_simulation_command", side_effect=RuntimeError("audit dispatch failure")):
                failure = submit_simulation_practice_action(handle, continue_request)
            self.assertEqual(failure["assessment"]["outcome"], "SYSTEM_FAILURE")
            self.assertEqual(failure["hold_id"], staged["hold_id"])
            self.assertEqual(failure["current_frame"]["frame_id"], staged["current_frame"]["frame_id"])
            recovered = submit_simulation_practice_action(handle, continue_request)
            self.assertEqual(recovered["assessment"]["outcome"], "PASS")
            self.assertEqual(recovered["current_frame"]["account"]["selected_quantity"], 200)
        finally:
            self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B05_GUIDED_DISPATCH_FAILURE_REHOLDS", "attempt_id": begun["attempt"]["attempt_id"]})

    def test_unassisted_dispatch_and_repeat_lineage_are_fresh(self) -> None:
        primary_handle, primary = begin_simulation_practice_attempt(build_practice_attempt_request(episode_id="practice.f3.cancel-partial-residual.v1", mode="UNASSISTED"))
        self.assertIsNotNone(primary_handle)
        handles = [primary_handle]
        try:
            unassisted = submit_simulation_practice_action(primary_handle, _action(primary, "UNASSISTED", "SEMANTIC_ACTION", "PLAYER_CANCEL_NEAREST"))
            self.assertEqual(unassisted["assessment"]["outcome"], "PASS")
            repeated_handle, repeated = begin_simulation_practice_attempt(build_practice_attempt_request(episode_id="practice.f3.cancel-partial-residual.v1", operation="EXACT_REPEAT", prior_attempt_id=primary["attempt"]["attempt_id"]))
            variation_handle, variation = begin_simulation_practice_attempt(build_practice_attempt_request(episode_id="practice.f3.cancel-volume-variation.v1", operation="VARIATION", prior_attempt_id=primary["attempt"]["attempt_id"]))
            handles.extend([repeated_handle, variation_handle])
            self.assertNotEqual(primary["source_run_id"], repeated["source_run_id"])
            self.assertEqual(primary["episode"]["recipe_sha256"], repeated["episode"]["recipe_sha256"])
            self.assertNotEqual(primary["episode"]["recipe_sha256"], variation["episode"]["recipe_sha256"])
            self.assertEqual(primary["current_frame"]["cursor"]["simulation_time_us"], repeated["current_frame"]["cursor"]["simulation_time_us"])
        finally:
            for handle in handles:
                self.assertIsNotNone(handle)
                self.assertEqual(release_simulation_episode(handle)["status"], "CLOSED")
        _CASES.append({"case_id": "B06_UNASSISTED_AND_FRESH_REPEAT_LINEAGE", "primary_source": primary["source_run_id"]})


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SimulationPracticeAudit))
    print(json.dumps({"schema_id": "KIRBY2_SIMULATION_PRACTICE_AUDIT_V1", "schema_version": 1, "status": "PASS" if result.wasSuccessful() else "FAIL", "test_count": result.testsRun, "cases": _CASES}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
