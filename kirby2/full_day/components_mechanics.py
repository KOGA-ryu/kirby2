"""Component adapter for the sole full-day market-mechanics engine."""

from __future__ import annotations

from collections.abc import Mapping

from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
from kirby2.simulation.clock import SimulationClock

from .components import ComponentSnapshotV1, FullDayComponentAdapterV1, _plain_mapping
from .composition import FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT
from .models import FullDayPlanV1, canonical_json_bytes


MECHANICS_COMPONENT_SCHEMA_VERSION = 1
MECHANICS_COMPONENT_IMPLEMENTATION_VERSION = 2


class MarketMechanicsComponentAdapterV1(FullDayComponentAdapterV1):
    """Restore/validate the engine while retaining runtime ownership.

    The adapter owns checkpoint interpretation, not another engine, book, clock,
    or allocator.  When the runtime clock already exists, restore validates the
    serialized clock and attaches that exact object to the detached engine.
    """

    component_id = MECHANICS_COMPONENT
    component_schema_version = MECHANICS_COMPONENT_SCHEMA_VERSION
    implementation_version = MECHANICS_COMPONENT_IMPLEMENTATION_VERSION
    active_predicate = "ALWAYS"
    dependencies = (FULL_DAY_RUNTIME_COMPONENT,)
    owned_resource_ids = ()
    borrowed_resource_ids = tuple(
        sorted({"AUCTION_BOOK", "MARKET_MECHANICS_ENGINE", "ORDER_BOOK"})
    )
    owned_state_ids = tuple(
        sorted(
            {
                "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
            }
        )
    )

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("mechanics adapter requires FullDayPlanV1")
        return True

    @staticmethod
    def _validate_engine(engine: object, plan: FullDayPlanV1 | None = None) -> MarketMechanicsEngine:
        if type(engine) is not MarketMechanicsEngine:
            raise TypeError("mechanics adapter owner must be MarketMechanicsEngine")
        if type(engine.clock) is not SimulationClock:
            raise ValueError("mechanics engine has a noncanonical clock owner")
        if engine.rules.session_schedule.transitions:
            raise ValueError(
                "full-day mechanics engine must have an empty native session schedule"
            )
        if plan is not None:
            if type(plan) is not FullDayPlanV1:
                raise TypeError("mechanics validation plan must be FullDayPlanV1")
            expected_rules = plan.instrument_profile.mechanics_rules.to_instrument_rules()
            if engine.rules.as_dict() != expected_rules.as_dict():
                raise ValueError("mechanics engine rules differ from the semantic plan")
        engine.assert_invariants()
        return engine

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        engine = self._validate_engine(owner)
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=engine.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if plan is not None and type(plan) is not FullDayPlanV1:
            raise TypeError("mechanics adapter plan context must be FullDayPlanV1")
        clock = context.get("clock")
        if clock is not None:
            if type(clock) is not SimulationClock:
                raise TypeError("mechanics adapter clock context must be SimulationClock")
        existing = context.get("existing_engine")
        if existing is not None:
            self._validate_engine(existing, plan)
            if clock is not None and existing.clock is not clock:
                raise ValueError("existing mechanics engine does not borrow the runtime clock")
            if existing.canonical_state_bytes() != canonical_json_bytes(snapshot.state):
                raise ValueError("runtime engine differs from mechanics component state")
            return

        # Detached validation has no pre-existing engine.  Construct exactly one
        # candidate and return after checking its canonical fixed point.  The
        # restore path does not call this branch and then construct it again.
        restored = MarketMechanicsEngine.from_checkpoint_state(
            _plain_mapping(snapshot.state)
        )
        self._validate_engine(restored, plan)
        if clock is not None and restored.clock.checkpoint_state() != clock.checkpoint_state():
            raise ValueError("mechanics checkpoint clock differs from runtime clock")
        if restored.canonical_state_bytes() != canonical_json_bytes(snapshot.state):
            raise ValueError("mechanics component state is not a canonical fixed point")

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if plan is not None and type(plan) is not FullDayPlanV1:
            raise TypeError("mechanics adapter plan context must be FullDayPlanV1")
        clock = context.get("clock")
        if clock is not None and type(clock) is not SimulationClock:
            raise TypeError("mechanics adapter clock context must be SimulationClock")
        existing = context.get("existing_engine")
        if existing is not None:
            self.validate(snapshot, **context)
            return existing
        restored = MarketMechanicsEngine.from_checkpoint_state(
            _plain_mapping(snapshot.state)
        )
        self._validate_engine(restored, plan)
        if restored.canonical_state_bytes() != canonical_json_bytes(snapshot.state):
            raise ValueError("mechanics component state is not a canonical fixed point")
        clock = context.get("clock")
        if clock is not None:
            if restored.clock.checkpoint_state() != clock.checkpoint_state():
                raise ValueError("mechanics checkpoint clock differs from runtime clock")
            # Rebind to the owner shell's sole authoritative clock.
            restored.clock = clock  # type: ignore[assignment]
            restored.assert_invariants()
        return restored


def mechanics_checkpoint_projection(
    engine: MarketMechanicsEngine,
) -> Mapping[str, Mapping[str, object]]:
    """Project the strict engine state into the three frozen inventory owners.

    The complete native book/auction states are retained inside their exact
    order-record fields so a fresh process can reconstruct allocator and history
    semantics, while the sibling fields remain independently auditable.
    """

    MarketMechanicsComponentAdapterV1._validate_engine(engine)
    state = engine.checkpoint_state()
    book = state["book"]
    auction = state["auction"]
    allocators = state["allocators"]
    if not isinstance(book, Mapping) or not isinstance(auction, Mapping) or not isinstance(
        allocators, Mapping
    ):
        raise RuntimeError("validated mechanics checkpoint has malformed nested state")
    player_position = book["player_position"]
    if not isinstance(player_position, Mapping):
        raise RuntimeError("validated book checkpoint has malformed player state")
    auction_orders = auction["orders"]
    if type(auction_orders) is not list:
        raise RuntimeError("validated auction checkpoint has malformed order rows")
    auction_ids = sorted(
        row["request"]["order_id"]
        for row in auction_orders
        if isinstance(row, Mapping) and isinstance(row.get("request"), Mapping)
    )
    projection: dict[str, Mapping[str, object]] = {
        "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1": {
            "auction.imbalance_state": engine.auction_indication().as_dict(),
            "auction.order_priority_allocator_state": max(
                (
                    int(row["resting_sequence"])
                    for row in auction_orders
                    if isinstance(row, Mapping)
                    and type(row.get("resting_sequence")) is int
                ),
                default=0,
            ),
            "auction.order_records": auction,
            "auction.reference_price_ticks": (
                engine.last_trade_price_ticks or engine.rules.reference_price_ticks
            ),
            "auction.seen_order_ids": auction_ids,
            "auction.trade_allocator_state": auction["trade_sequence"],
            "venue_truth.auction.player_fill_history": [
                event.as_dict()
                for event in engine.events
                if event.event_type.value == "AUCTION_FILL"
            ],
            "venue_truth.auction.player_position": state["auction_player_position"],
        },
        "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1": {
            "continuous.active_order_index": sorted(
                order_id
                for side in (book["bid_levels"], book["ask_levels"])
                if type(side) is list
                for level in side
                if isinstance(level, Mapping)
                for order_id in level.get("order_ids", [])
            ),
            "continuous.resting_priority_allocator_state": book["resting_sequence"],
            "continuous.seen_order_ids": book["seen_order_ids"],
            "venue_truth.continuous.event_journal": book["journal"],
            "venue_truth.continuous.fill_records": book["fills"],
            "venue_truth.continuous.fifo_price_levels": {
                "asks": book["ask_levels"],
                "bids": book["bid_levels"],
            },
            "venue_truth.continuous.order_history": book,
            "venue_truth.continuous.player_fill_history": player_position["fills"],
            "venue_truth.continuous.player_position": {
                "bought_quantity": player_position["bought_quantity"],
                "position": player_position["position"],
                "sold_quantity": player_position["sold_quantity"],
            },
            "venue_truth.continuous.trade_records": book["trades"],
        },
        "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1": {
            "mechanics.arrival_allocator_state": allocators["arrival_sequence"],
            "mechanics.command_allocator_state": allocators["command_sequence"],
            "mechanics.event_allocator_state": len(state["events"]) + 1,
            "mechanics.event_prefix": state["events"],
            "mechanics.instrument_rules": state["rules"],
            "mechanics.last_trade": state["last_trade_price_ticks"],
            "mechanics.managed_order_records": state["managed_orders"],
            "mechanics.session_schedule_cursor": state["schedule_index"],
            "mechanics.session_state": state["session_state"],
        },
    }
    return projection


__all__ = [
    "MECHANICS_COMPONENT_IMPLEMENTATION_VERSION",
    "MECHANICS_COMPONENT_SCHEMA_VERSION",
    "MarketMechanicsComponentAdapterV1",
    "mechanics_checkpoint_projection",
]
