"""Frozen declarative playbooks over delivered C4 observations.

The language/condition evaluator and semantic AST are existing strategy owners.
This adapter advertises only top-of-book features whose availability it can prove.
All consumers call evaluate_playbook; widgets never implement a second rule engine.
"""
from __future__ import annotations

import copy
from decimal import Decimal

from kirby2.full_day.models import canonical_sha256
from kirby2.strategy.features import FeatureSnapshot
from kirby2.strategy.language import FeatureName, StrategyDefinition, UnavailableValuePolicy, parse_strategy, parse_strategy_semantic_ast
from kirby2.strategy.runtime import TrafficLightRuntime
from .execution_commitments import integer
from .execution_recipes import RECIPES, READY_US, STOP_US, PRICE_MIN, PRICE_MAX
from .practice_library import _bytes, _decode

SCHEMA = 'KIRBY2_PLAYBOOK_V1'
SUPPORTED = ('spread_ticks', 'best_bid_size', 'best_ask_size', 'book_imbalance', 'microprice')
RULE = 'setup Entry\nwindow 1s\nunavailable REFUSE\nGREEN when\n spread_ticks <= 2\nWAIT when\n spread_ticks <= 4\nRED otherwise\n'


def exact(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(label + ' fields are not exact; unsupported fields are never discarded')
    return value


def template(price=9998):
    return dict(schema_id=SCHEMA, rule_source=RULE,
                execution=dict(quantity=1000, price_ticks=price, decision_delay_us=0,
                               lifetime_us=15000, partial_policy='HOLD_UNTIL_CANCEL',
                               invalidation='CANCEL_WHEN_NOT_GREEN'),
                evaluation=dict(markout_horizon_us=3000, fee_milliticks_per_share=0))


def _parse(source):
    if type(source) is not str or not 1 <= len(source.encode()) <= 16_384:
        raise ValueError('playbook source must be 1–16384 UTF-8 bytes')
    document = _decode(source.encode())
    exact(document, ('schema_id', 'rule_source', 'execution', 'evaluation'), 'playbook')
    if document['schema_id'] != SCHEMA: raise ValueError('unsupported playbook version')
    if type(document['rule_source']) is not str: raise ValueError('rule source must be text')
    definition = parse_strategy(document['rule_source'])
    if not isinstance(definition, StrategyDefinition):
        raise ValueError('advanced state-machine source is retained but unsupported by this V1 observation adapter')
    if definition.unavailable_policy is not UnavailableValuePolicy.REFUSE:
        raise ValueError('new playbooks require unavailable REFUSE; legacy policies are not reinterpreted')
    used = {c.feature.value for c in definition.green_conditions + definition.wait_conditions}
    if not used <= set(SUPPORTED):
        raise ValueError('unsupported observation capabilities: ' + ', '.join(sorted(used-set(SUPPORTED))))
    execution = exact(document['execution'], ('quantity', 'price_ticks', 'decision_delay_us', 'lifetime_us', 'partial_policy', 'invalidation'), 'execution')
    integer(execution['quantity'], 'quantity', 1, 1000)
    integer(execution['price_ticks'], 'price', PRICE_MIN, PRICE_MAX)
    integer(execution['decision_delay_us'], 'decision delay', 0, 10000)
    integer(execution['lifetime_us'], 'lifetime', 1, STOP_US-READY_US)
    if execution['partial_policy'] != 'HOLD_UNTIL_CANCEL' or execution['invalidation'] != 'CANCEL_WHEN_NOT_GREEN':
        raise ValueError('unsupported partial-fill or invalidation policy')
    evaluation = exact(document['evaluation'], ('markout_horizon_us', 'fee_milliticks_per_share'), 'evaluation')
    integer(evaluation['markout_horizon_us'], 'markout horizon', 1000, 10000)
    integer(evaluation['fee_milliticks_per_share'], 'fee', 0, 10000)
    semantic = dict(schema_id=SCHEMA, rule=parse_strategy_semantic_ast(document['rule_source']).semantic_projection(),
                    execution=execution, evaluation=evaluation)
    return document, definition, 'playbook-'+canonical_sha256(semantic)


def validate_playbook(source):
    """Validation never rewrites the user's source, including unsupported text."""
    try:
        document, definition, identifier = _parse(source)
        return dict(status='VALID', source=source, playbook_id=identifier, document=copy.deepcopy(document),
                    required_features=sorted({c.feature.value for c in definition.green_conditions+definition.wait_conditions}),
                    error=None)
    except (ValueError, TypeError, KeyError) as error:
        return dict(status='REFUSED', source=source, playbook_id=None, document=None, required_features=[], error=str(error))


def edit_playbook_form(source, values):
    """Only execution/cost fields are form-editable; exact rule text is retained."""
    document, _, _ = _parse(source)
    exact(values, ('execution', 'evaluation'), 'form')
    document.update(copy.deepcopy(values))
    result = _bytes(document).decode()
    _parse(result)
    return result


def observation(frame):
    """Take only the delivered market cut. Private labels/account/future are absent."""
    knowledge = frame['knowledge']
    result = dict(schema_id='KIRBY2_RULE_OBSERVATION_V1', simulation_time_us=frame['simulation_time_us'],
                  market=copy.deepcopy(knowledge['market']))
    result['observation_id'] = 'observation-'+canonical_sha256(result)
    return result


def evaluate_playbook(source, cut, eligible_since_us=None):
    document, definition, identifier = _parse(source)
    exact(cut, ('schema_id','simulation_time_us','market','observation_id'), 'observation')
    if cut['schema_id'] != 'KIRBY2_RULE_OBSERVATION_V1' or cut['observation_id'] != 'observation-'+canonical_sha256({k:v for k,v in cut.items() if k!='observation_id'}):
        raise ValueError('observation identity differs')
    now = integer(cut['simulation_time_us'], 'observation time', 0, STOP_US)
    values = {feature: None for feature in FeatureName}
    market = cut['market']; unavailable = None
    if market is None: unavailable = 'MISSING_MARKET'
    else:
        exact(market, ('ask_levels','bid_levels','best_ask_ticks','best_bid_ticks','last_trade_price_ticks','session_state','simulation_time_us'), 'market')
        source_time = integer(market['simulation_time_us'], 'source time', 0, now)
        if now-source_time > 500000: unavailable = 'STALE_MARKET'
        elif market['session_state'] != 'CONTINUOUS': unavailable = 'MARKET_NOT_CONTINUOUS'
        else:
            tops = {}
            for side in ('bid', 'ask'):
                levels = market[side+'_levels']
                if type(levels) is not list or len(levels)>256: raise ValueError('invalid delivered depth')
                previous = None
                for level in levels:
                    exact(level, ('price_ticks','quantity'), 'depth level')
                    p = integer(level['price_ticks'], 'depth price', 1, 1000000)
                    integer(level['quantity'], 'depth shares', 1, 1000000)
                    if previous is not None and ((side=='bid' and p>=previous) or (side=='ask' and p<=previous)):
                        raise ValueError('unordered delivered depth')
                    previous = p
                top = levels[0] if levels else None
                if market['best_'+side+'_ticks'] != (None if top is None else top['price_ticks']): raise ValueError('top/depth mismatch')
                tops[side] = top
                if top: values[FeatureName('best_'+side+'_size')] = Decimal(top['quantity'])
            bid, ask = tops['bid'], tops['ask']
            if bid and ask:
                spread = ask['price_ticks']-bid['price_ticks']
                if spread<0: raise ValueError('crossed delivered book')
                total = Decimal(bid['quantity']+ask['quantity'])
                values[FeatureName.SPREAD_TICKS] = Decimal(spread)
                values[FeatureName.BOOK_IMBALANCE] = Decimal(bid['quantity']-ask['quantity'])/total
                values[FeatureName.MICROPRICE] = Decimal(ask['price_ticks']*bid['quantity']+bid['price_ticks']*ask['quantity'])/total
    required = {c.feature for c in definition.green_conditions+definition.wait_conditions}
    missing = sorted(feature.value for feature in required if values[feature] is None)
    if unavailable or missing: unavailable = unavailable or 'MISSING_REQUIRED_FEATURES'
    features = FeatureSnapshot(now, definition.window_us, values)
    # Existing deterministic condition evaluation, without synthesizing a rolling
    # window or substituting configured relative volume for observed volume.
    evaluated = TrafficLightRuntime(definition, Decimal(0))._evaluate(features).as_dict()
    if eligible_since_us is not None: integer(eligible_since_us, 'eligibility time', 0, now)
    expired = eligible_since_us is not None and now >= eligible_since_us+document['execution']['lifetime_us']
    status = 'UNAVAILABLE' if unavailable else 'EXPIRED' if expired else 'ELIGIBLE' if evaluated['state']=='GREEN' else 'NOT_ELIGIBLE'
    return dict(playbook_id=identifier, observation_id=cut['observation_id'], simulation_time_us=now,
                status=status, unavailable_reason=unavailable, missing_features=missing, evaluation=evaluated,
                expires_us=None if eligible_since_us is None else eligible_since_us+document['execution']['lifetime_us'],
                rank=None, success_probability=None)


def preview_playbook(source, recipe_id='execution.cancel-race.v1', seed=11):
    from .execution_practice import start_execution_practice, close_execution_practice
    handle, frame = start_execution_practice(recipe_id, seed)
    try:
        cut = observation(frame)
        return dict(observation=cut, result=evaluate_playbook(source, cut))
    finally: close_execution_practice(handle)


def evaluate_practice_playbook(handle, source):
    from .execution_practice import refresh_execution_practice
    cut = observation(refresh_execution_practice(handle))
    return dict(observation=cut, result=evaluate_playbook(source, cut))


def playbook_catalog():
    return dict(schema_id=SCHEMA, supported_features=list(SUPPORTED), maximum_cells=16,
                worker_concurrency=1, recipes=[dict(recipe_id=k,title=v[0]) for k,v in RECIPES.items()],
                templates=[dict(title='Passive queue',source=_bytes(template(9998)).decode()),
                           dict(title='Marketable limit',source=_bytes(template(10000)).decode())],
                notice='Synthetic rules and conditional measurements; no estimated edge, calibrated probability or automatic winner.')
