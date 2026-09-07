"""Public bounded synthetic experiments, durable reports and verified Replay.

Batch simulation is backend-owned. No handle escapes this module. Every preview
is durably marked exposed before it can be returned to the caller.
"""
from __future__ import annotations
import copy
import os
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from kirby2.features.market_observations import observe_market, WINDOW_US
from kirby2.research.paths import DataAreaId
from .market_profiles import (DURATION_US, EVALUATION_SEEDS, PRIOR_SEEDS, DIAGNOSTICS,
                              list_market_workbench_profiles)
from .simulation_facade import resolve_simulation_profile, list_simulation_training_resources
from .simulation_run_facade import start_simulation_run, advance_simulation_run, close_simulation_run, _materialize_session
from .simulation_finalize_facade import finalize_simulation_run
from .simulation_replay_facade import resolve_replay_artifact, _verify_replay_artifact_bytes
from .simulation_artifact_contract import ReplayArtifactRefV1
from .simulation_contract import canonical_digest
# Shared backend-only bounded, no-follow, fsynced storage primitives. No practice
# attempt registries or practice-bundle policy are used here.
from .practice_library import _bytes, _decode, _paths, _child, _read, _write, _sync_directory

class MarketCleanupError(RuntimeError):
    """A batch must stop if authority cleanup cannot be confirmed."""


_SCHEMA = "KIRBY2_MARKET_REPORT_V1"
_ID = re.compile(r"market-report-[0-9a-f]{64}\Z")


def market_start_configuration(profile_id, seed=11):
    """Resolve one supported composition; no simulation or disk mutation."""
    if type(profile_id) is not str or type(seed) is not int or not 0 <= seed <= 2**31-1:
        raise ValueError("select a market profile and an integer seed in [0,2147483647]")
    catalog = list_market_workbench_profiles()["catalog"]
    row = next((row for row in catalog["profiles"] if row["profile_ref"]["profile_id"] == profile_id),None)
    if row is None: raise ValueError("unsupported market composition")
    selection = {"schema_id":"KIRBY2_SIMULATION_PROFILE_SELECTION_V1","schema_version":1,
                 "profile_ref":row["profile_ref"],"seed":seed,"duration_us":DURATION_US,"control_values":{}}
    resolution = resolve_simulation_profile(selection)
    if resolution["status"] != "AVAILABLE": raise ValueError(str(resolution["refusal"]))
    training = list_simulation_training_resources()
    defaults = training["defaults"]
    options = {"schema_id":"KIRBY2_SIMULATION_TRAINING_OPTIONS_V1","schema_version":1,
               "quantity_options":defaults["quantity_options"],"initial_quantity":defaults["initial_quantity"],
               "layout_ref":defaults["layout_ref"],"observation_policy_ref":defaults["observation_policy_ref"],
               "strategy_ref":None,"curriculum_drill_ref":None,"objective":None,"initial_run_state":"READY"}
    return {"catalog":catalog,"resolution":resolution,"training_catalog":training,"training_options":options}


def _book(session):
    book = session.engine.book
    return {"time_us":session.simulation_time_us,"bid_ticks":book.best_bid,"ask_ticks":book.best_ask,
            "bid_shares":sum(level.total_quantity for level in book.bids.values()),
            "ask_shares":sum(level.total_quantity for level in book.asks.values())}


def _events(session):
    clock_by_sequence = {}
    for flow in session.engine.flow_events:
        if flow.exchange_event_start is not None:
            for sequence in range(flow.exchange_event_start,flow.exchange_event_end+1):
                clock_by_sequence[sequence] = flow.simulation_time_us
    rows = []
    for event in session.engine.book.journal.events:
        data, kind = event.data, event.event_type.value
        event_time = clock_by_sequence.get(event.sequence,0)
        economic = {"TRADE":("TRADE","taker_side","quantity"),
                    "ORDER_CANCELLED":("CANCEL","side","cancelled_quantity"),
                    "ORDER_ADDED":("ADD","side","remaining_quantity")}.get(kind)
        row = {"sequence":event.sequence,"time_us":event_time,"kind":"OTHER","side":None,"price_ticks":0,"quantity":0}
        if economic:
            kind, side, quantity = economic
            row.update(kind=kind,side=data[side].upper(),price_ticks=data["price_ticks"],quantity=data[quantity])
        rows.append(row)
    return rows


def _observations(session, books):
    return observe_market(_events(session), books, cut_us=session.simulation_time_us)


def _diagnostics(session, observations):
    values = [item["values"] for item in observations]
    events = len(session.engine.flow_events)
    executed = sum(v["executed_buy_shares"]+v["executed_sell_shares"] for v in values)
    # Envelopes cover every emitted top-of-book update, not only display samples.
    bid = ask = None
    maximum_spread = 0
    origin_mid = None
    maximum_move = 0
    boundaries = {flow.exchange_event_end for flow in session.engine.flow_events if flow.exchange_event_end is not None}
    starts = [flow.exchange_event_start for flow in session.engine.flow_events if flow.exchange_event_start is not None]
    boundaries.add(min(starts)-1 if starts else len(session.engine.book.journal.events))
    for event in session.engine.book.journal.events:
        if event.event_type.value == "BEST_BID_CHANGED": bid = event.data.get("new_price_ticks")
        if event.event_type.value == "BEST_ASK_CHANGED": ask = event.data.get("new_price_ticks")
        if event.sequence in boundaries and bid is not None and ask is not None:
            if origin_mid is None: origin_mid = bid+ask
            maximum_spread = max(maximum_spread, ask-bid)
            maximum_move = max(maximum_move, abs(bid+ask-origin_mid))
    resting = {"BUY":0,"SELL":0}
    for row in _events(session):
        if row["kind"] == "ADD": resting[row["side"]] += row["quantity"]
        elif row["kind"] == "CANCEL": resting[row["side"]] -= row["quantity"]
        elif row["kind"] == "TRADE": resting["SELL" if row["side"] == "BUY" else "BUY"] -= row["quantity"]
    final_book = _book(session)
    conserved = resting == {"BUY":final_book["bid_shares"],"SELL":final_book["ask_shares"]}
    checks = {"resting_quantity_conservation":conserved,"event_count": 0 < events <= DIAGNOSTICS["maximum_events"],
              "executed_shares":executed >= DIAGNOSTICS["minimum_executed_shares"],
              "spread": maximum_spread <= DIAGNOSTICS["maximum_spread_ticks"],
              "price_movement": maximum_move <= 2*DIAGNOSTICS["maximum_absolute_mid_change_ticks"],
              "observations":all(item["status"] == "AVAILABLE" for item in observations)}
    failures = [key for key,value in checks.items() if not value]
    session.engine.book.assert_invariants()
    return {"status":"PASS" if not failures else "FAIL", "failures":failures,
            "flow_events":events,"executed_shares":executed,"maximum_spread_ticks":maximum_spread,
            "maximum_mid_change_half_ticks":maximum_move,"book_invariants":"PASS","resting_quantity_conservation":"PASS" if conserved else "FAIL"}


def _run(profile_id, seed):
    config = market_start_configuration(profile_id,seed)
    config["training_options"]["initial_run_state"] = "RUNNING"
    handle, start = start_simulation_run(config["resolution"],config["training_options"])
    if handle is None: raise ValueError("governed Start refused: " + str(start))
    try:
        frame, books, observations = start["initial_frame"], [_book(handle.session)], []
        for _ in range(DURATION_US//WINDOW_US):
            result = advance_simulation_run(handle,frame["source_run_id"],frame["frame_id"],frame["cursor"]["cursor_id"],frame["cursor"]["simulation_time_us"]+WINDOW_US)
            if result["status"] != "AVAILABLE": raise ValueError("governed advance refused: "+str(result))
            frame = result["destination_frame"]
            books.append(_book(handle.session)); observations.append(_observations(handle.session,books))
        diagnostics = _diagnostics(handle.session,observations)
        final = finalize_simulation_run(handle,frame["source_run_id"],frame["frame_id"],frame["cursor"]["cursor_id"],"COMPLETE_ONLY")
        if final["status"] != "AVAILABLE": raise ValueError("governed finalization refused: "+str(final))
        ref = final["run_result"]["replay_artifact"]
        source, receipt = resolve_replay_artifact(ref)
        if source is None: raise ValueError("Replay verification failed: "+str(receipt))
        cell = {"profile_id":profile_id,"seed":seed,"status":"AVAILABLE", "resolution":config["resolution"],
                "artifact_ref":ref,"observations":observations,"diagnostics":diagnostics}
        return cell, source.artifact_bytes
    finally:
        if handle.lifecycle_disposition != "FINALIZED":
            closed = close_simulation_run(handle,"USER_ABANDONED")
            if closed["status"] != "CLOSED": raise MarketCleanupError("market experiment cleanup could not be confirmed")


def _recompute(source):
    state = source.reconstruction
    session, _, _, _ = _materialize_session(state.resolution,state.training_options)
    books, observations = [_book(session)], []
    for _ in range(DURATION_US//WINDOW_US):
        session.advance_by(WINDOW_US)
        books.append(_book(session)); observations.append(_observations(session,books))
    if session.state_sha256() != source.recording.expected_state_sha256:
        raise ValueError("market report is not a passive 30-second experiment")
    return observations, _diagnostics(session,observations)


def _priors(cells):
    if len(cells) != len(PRIOR_SEEDS) or any(cell["status"] != "AVAILABLE" for cell in cells): return []
    return [{"schema_id":"KIRBY2_SYNTHETIC_PRIOR_V1", "profile_sha256":cells[0]["resolution"]["selection"]["profile_ref"]["profile_sha256"],
             "seeds":list(PRIOR_SEEDS),"cut_us":cut,"window_us":WINDOW_US,
             "notional_sum":sum(cell["observations"][index]["values"]["executed_notional_tick_shares"] for cell in cells),
             "sample_count":len(cells)} for index,cut in enumerate(range(WINDOW_US,DURATION_US+1,WINDOW_US))]


def _with_priors(cells, priors):
    result = copy.deepcopy(cells)
    for cell in result:
        if cell["status"] != "AVAILABLE": continue
        for index, observation in enumerate(cell["observations"]):
            if not priors: continue
            prior = priors[index]; total = prior["notional_sum"]
            observation["relative_activity"] = {
                "status":"AVAILABLE" if total else "ZERO_BASELINE",
                "ratio":None if not total else str((Decimal(observation["values"]["executed_notional_tick_shares"])*prior["sample_count"]/total).quantize(Decimal('.000001'))),
                "unit":"observed_notional / independent_synthetic_prior_notional", "baseline_id":canonical_digest(prior)}
    return result


def _summary(cells):
    output = []
    for profile_id in dict.fromkeys(cell["profile_id"] for cell in cells):
        group = [cell for cell in cells if cell["profile_id"] == profile_id]
        metrics = {}
        for field in ("executed_buy_shares","executed_sell_shares","cancelled_ask_shares","added_ask_shares","replenished_ask_shares","mid_change_half_ticks"):
            totals = []
            for cell in group:
                values = ([] if cell["status"] != "AVAILABLE" else [o["values"][field] for o in cell["observations"]])
                totals.append(sum(values) if values and all(value is not None for value in values) else None)
            ordered = sorted(value for value in totals if value is not None)
            metrics[field] = {"values_in_seed_order":totals,"minimum":ordered[0] if ordered else None,
                              "maximum":ordered[-1] if ordered else None,
                              "median":str(Decimal(ordered[(len(ordered)-1)//2]+ordered[len(ordered)//2])/2) if ordered else None,
                              "sample_count":len(ordered),"unavailable_count":len(group)-len(ordered)}
        output.append({"profile_id":profile_id,"executed":sum(cell["status"] == "AVAILABLE" for cell in group),
                       "failed":sum(cell["status"] != "AVAILABLE" or cell["diagnostics"]["status"] != "PASS" for cell in group),
                       "metrics":metrics})
    return output


def _directory(root, create=False):
    paths = _paths(root); paths.validate(DataAreaId.EVIDENCE)
    if create: paths.ensure(DataAreaId.EVIDENCE)
    directory = _child(paths.area(DataAreaId.EVIDENCE),"market-workbench")
    if create: directory.mkdir(exist_ok=True)
    if directory.exists() and not directory.is_dir(): raise ValueError("market evidence directory is not a directory")
    return directory


def _immutable_record(directory, name, record):
    path = _child(directory,name)
    raw = _bytes(record)
    pending = Path(tempfile.mkdtemp(prefix=".pending-",dir=directory))
    try:
        candidate = pending/"record.json"
        _write(candidate,raw)
        try: os.link(candidate,path)
        except FileExistsError:
            if _read(path) != raw: raise ValueError("immutable market record identity collision")
        _sync_directory(directory)
    finally:
        shutil.rmtree(pending)


def _mark_exposed(report, root):
    directory = _child(_directory(root,True),"exposures"); directory.mkdir(exist_ok=True)
    for cell in report["cells"]+report["priors"]:
        if cell["status"] != "AVAILABLE": continue
        selection = cell["resolution"]["selection"]
        record = {"schema_id":"KIRBY2_MARKET_EXPOSURE_V1","selection":selection,"familiarity":"EXPOSED_BY_PREVIEW"}
        _immutable_record(directory,canonical_digest(selection)+".json",record)


def run_market_comparison(comparison_profile_id="market.replenishment.v1", root=None):
    """Run warming versus one named recipe on every predeclared seed, then save.

    No UI events, user order submission or presentation pacing participate here.
    A failed cell is kept in the report. An I/O failure never returns a preview.
    """
    market_start_configuration(comparison_profile_id)
    directory = _directory(root,True)
    if comparison_profile_id == "market.warming.v1": raise ValueError("choose a distinct comparison")
    cells, priors, artifacts = [], [], {}
    for target, population, collection in [("market.quiet.v1",PRIOR_SEEDS,priors),
                                          ("market.warming.v1",EVALUATION_SEEDS,cells),
                                          (comparison_profile_id,EVALUATION_SEEDS,cells)]:
        for seed in population:
            try:
                cell, raw = _run(target,seed)
                artifacts[cell["artifact_ref"]["artifact_sha256"]] = raw
            except MarketCleanupError:
                raise
            except Exception as error:
                cell = {"profile_id":target,"seed":seed,"status":"FAILED","error":str(error)}
            collection.append(cell)
    baselines = _priors(priors)
    cells = _with_priors(cells,baselines)
    report = {"schema_id":_SCHEMA,"schema_version":1,"comparison_profile_id":comparison_profile_id,
              "evaluation_seeds":list(EVALUATION_SEEDS),"prior_seeds":list(PRIOR_SEEDS),
              "coordinate":"SYNTHETIC_ELAPSED_UTC","window_us":WINDOW_US,"duration_us":DURATION_US,
              "diagnostic_envelopes":dict(DIAGNOSTICS), "priors":priors,"baselines":baselines,"cells":cells,
              "summary":_summary(cells),"representative_seed":EVALUATION_SEEDS[0],
              "selection_method":"FIRST_PREDECLARED_SEED; all cells retained",
              "familiarity":"EXPOSED_BY_PREVIEW", "release_qualification":False,
              "notice":"Synthetic experiments; overlapping results and failed diagnostics are retained. No expected direction or real-market claim."}
    evidence_id = "market-report-"+canonical_digest(report)
    report = {**report,"report_id":evidence_id}
    directory = _directory(root,True)
    pending = Path(tempfile.mkdtemp(prefix=".pending-",dir=directory))
    try:
        for digest, raw in artifacts.items(): _write(pending/(digest+".json"),raw)
        _write(pending/"report.json",_bytes(report)); _sync_directory(pending)
        os.rename(pending,_child(directory,evidence_id)); _sync_directory(directory)
    finally:
        if pending.exists(): shutil.rmtree(pending)
    _mark_exposed(report,root)
    return report


def open_market_report(report_id, root=None):
    """Read immutable evidence, deep-verify every available cell and recompute observations."""
    if type(report_id) is not str or _ID.fullmatch(report_id) is None: raise ValueError("invalid market report ID")
    directory = _child(_directory(root),report_id)
    report = _decode(_read(_child(directory,"report.json")))
    fields = {"schema_id","schema_version","report_id","comparison_profile_id","evaluation_seeds","prior_seeds","coordinate","window_us","duration_us","diagnostic_envelopes","priors","baselines","cells","summary","representative_seed","selection_method","familiarity","release_qualification","notice"}
    if (type(report) is not dict or set(report) != fields or report.get("schema_id") != _SCHEMA
            or type(report.get("schema_version")) is not int or report["schema_version"] != 1 or report.get("report_id") != report_id):
        raise ValueError("unsupported market report")
    if "market-report-"+canonical_digest({key:value for key,value in report.items() if key != "report_id"}) != report_id:
        raise ValueError("market report digest mismatch")
    comparison = report["comparison_profile_id"]; market_start_configuration(comparison)
    if (report["evaluation_seeds"] != list(EVALUATION_SEEDS) or report["prior_seeds"] != list(PRIOR_SEEDS)
            or report["diagnostic_envelopes"] != DIAGNOSTICS or report["familiarity"] != "EXPOSED_BY_PREVIEW"
            or report["coordinate"] != "SYNTHETIC_ELAPSED_UTC" or report["window_us"] != WINDOW_US or report["duration_us"] != DURATION_US
            or report["release_qualification"] is not False or report["representative_seed"] != EVALUATION_SEEDS[0]
            or report["selection_method"] != "FIRST_PREDECLARED_SEED; all cells retained" or comparison == "market.warming.v1"):
        raise ValueError("market experiment policy differs")
    expected = [("market.quiet.v1",seed) for seed in PRIOR_SEEDS] + [(profile,seed) for profile in ("market.warming.v1",comparison) for seed in EVALUATION_SEEDS]
    all_cells = report["priors"]+report["cells"]
    if [(c["profile_id"],c["seed"]) for c in all_cells] != expected: raise ValueError("market population differs")
    recomputed = []
    for cell in all_cells:
        if cell["status"] == "FAILED":
            if set(cell) != {"profile_id","seed","status","error"} or type(cell["error"]) is not str: raise ValueError("invalid failed cell")
            recomputed.append(cell); continue
        if cell["status"] != "AVAILABLE" or set(cell) != {"profile_id","seed","status","resolution","artifact_ref","observations","diagnostics"}:
            raise ValueError("invalid market cell")
        reference = ReplayArtifactRefV1.from_dict(cell["artifact_ref"])
        raw = _read(_child(directory,reference.artifact_sha256+".json"))
        source, receipt = _verify_replay_artifact_bytes(reference,raw)
        if source is None: raise ValueError("saved experiment Replay failed: "+str(receipt))
        if source.reconstruction.resolution.as_dict() != cell["resolution"] or cell["resolution"] != market_start_configuration(cell["profile_id"],cell["seed"])["resolution"]:
            raise ValueError("market recipe identity differs")
        observations, diagnostics = _recompute(source)
        recomputed.append({**cell,"observations":observations,"diagnostics":diagnostics})
    prior_cells = recomputed[:len(PRIOR_SEEDS)]
    baselines = _priors(prior_cells)
    cells = _with_priors(recomputed[len(PRIOR_SEEDS):],baselines)
    if prior_cells != report["priors"] or cells != report["cells"] or baselines != report["baselines"] or _summary(cells) != report["summary"]:
        raise ValueError("market report measurements differ from verified source")
    _mark_exposed(report,root)
    return report


def list_market_reports(root=None):
    """Lightweight inventory. Opening performs deep verification before display."""
    directory = _directory(root)
    entries, rejected = [], []
    if directory.exists():
        for path in sorted(directory.iterdir()):
            if path.name.startswith('.pending-') or path.name in {'configurations','exposures'}: continue
            if path.is_symlink() or _ID.fullmatch(path.name) is None:
                rejected.append(path.name); continue
            entries.append(path.name)
    return {"schema_id":"KIRBY2_MARKET_REPORT_INDEX_V1","entries":entries,"rejected":rejected}


def save_market_configuration(profile_id, seed=11, root=None):
    """Persist the resolved recipe before a UI practice Start; never claim blindness."""
    config = market_start_configuration(profile_id,seed)
    directory = _directory(root,True)
    familiarity = "RECIPE_KNOWN; workbench practice is not a blind assessment"
    exposures = _child(directory,"exposures")
    selection = config["resolution"]["selection"]
    if exposures.exists():
        exposure = _child(exposures,canonical_digest(selection)+".json")
        if exposure.exists():
            expected = {"schema_id":"KIRBY2_MARKET_EXPOSURE_V1","selection":selection,"familiarity":"EXPOSED_BY_PREVIEW"}
            if _decode(_read(exposure)) != expected: raise ValueError("market exposure record differs")
            familiarity = "EXPOSED_BY_PREVIEW; workbench practice is not a blind assessment"
    record = {"schema_id":"KIRBY2_MARKET_CONFIGURATION_V1","configuration":config,"familiarity":familiarity}
    config_id = "market-config-"+canonical_digest(record)
    configurations = _child(directory,"configurations"); configurations.mkdir(exist_ok=True)
    _immutable_record(configurations,config_id+".json",record)
    return {**config,"saved_configuration_id":config_id,"familiarity":record["familiarity"]}


def open_market_replay(report_id, profile_id, seed, root=None):
    report = open_market_report(report_id,root)
    cell = next((cell for cell in report["cells"] if cell["profile_id"] == profile_id and cell["seed"] == seed),None)
    if cell is None or cell["status"] != "AVAILABLE": raise ValueError("selected market Replay is unavailable")
    reference = ReplayArtifactRefV1.from_dict(cell["artifact_ref"])
    directory = _child(_directory(root),report_id)
    source, receipt = _verify_replay_artifact_bytes(reference,_read(_child(directory,reference.artifact_sha256+".json")))
    if source is None: raise ValueError("selected market Replay did not verify")
    return source, {"reference":reference.as_dict(),"verification":receipt,"familiarity":"EXPOSED_BY_PREVIEW"}
