"""The bounded initial Kirby2 execution curriculum."""

from __future__ import annotations

from kirby2.scenarios import get_scenario_definition
from kirby2.session.objectives import ObjectiveType
from kirby2.simulation import LiquidityPreset as L
from kirby2.simulation import VolumePreset as V

from .models import (
    CurriculumDrill,
    CurriculumLesson,
    CurriculumMode,
    LessonObjectiveTemplate,
)


def _objective(
    objective_type: ObjectiveType,
    targets: tuple[int, ...],
    slippage: int,
) -> LessonObjectiveTemplate:
    return LessonObjectiveTemplate(objective_type, targets, slippage)


LESSONS = (
    CurriculumLesson(
        "01",
        "Balanced book",
        "Establish a baseline for two-sided flow, spread behavior, and neutral queue risk.",
        "balanced",
        (V.X0_50, V.X1_00, V.X2_00),
        (L.NORMAL, L.DEEP),
        (101, 113, 127, 139, 151, 163),
        (45, 60),
        _objective(ObjectiveType.ROUND_TRIP, (500, 1_000), 2),
        "Balanced flow is not motionless. Compare your timing with shifts in displayed depth and avoid inventing directional conviction from ordinary queue noise.",
    ),
    CurriculumLesson(
        "02",
        "Strong bid imbalance",
        "Recognize persistent bid-side depth and aggressive buying without treating one snapshot as proof.",
        "buy_pressure",
        (V.X0_50, V.X1_00, V.X2_00),
        (L.NORMAL, L.DEEP),
        (202, 211, 223, 227, 239, 251),
        (45, 60),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 2),
        "The useful signal was persistence across updates: bid replenishment, trade direction, and ask depletion together. A single large bid could still have been cancelled.",
    ),
    CurriculumLesson(
        "03",
        "Strong ask imbalance",
        "Recognize persistent ask-side depth and aggressive selling while separating pressure from a static wall.",
        "sell_pressure",
        (V.X0_50, V.X1_00, V.X2_00),
        (L.NORMAL, L.DEEP),
        (303, 307, 311, 317, 331, 347),
        (45, 60),
        _objective(ObjectiveType.LIQUIDATE, (500, 1_000), 2),
        "Ask imbalance mattered only when it persisted alongside selling and bid depletion. Review whether you waited for repeated evidence or reacted to one displayed queue.",
    ),
    CurriculumLesson(
        "04",
        "Queue depletion",
        "Track queue loss and cancellation pressure before the touch moves.",
        "high_cancellation",
        (V.X1_00, V.X2_00, V.X5_00),
        (L.THIN, L.NORMAL),
        (401, 409, 419, 421, 431, 433),
        (45, 60),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 2),
        "Queue depletion changes both fill probability and price risk. Distinguish traded depletion from cancelled liquidity; both remove depth, but they imply different urgency.",
    ),
    CurriculumLesson(
        "05",
        "Passive fill management",
        "Manage genuine FIFO queue position and decide when patience no longer compensates for adverse-selection risk.",
        "balanced",
        (V.X0_50, V.X1_00),
        (L.DEEP, L.VERY_DEEP),
        (503, 509, 521, 523, 541, 547),
        (60, 75),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 0),
        "Passive execution saved spread only when the queue filled before the market moved away. Use queue-ahead and post-fill midpoint behavior to judge whether waiting was actually cheap.",
    ),
    CurriculumLesson(
        "06",
        "Crossing the spread",
        "Use aggressive execution deliberately when completion risk outweighs the displayed spread cost.",
        "balanced",
        (V.X1_00, V.X2_00),
        (L.NORMAL, L.DEEP),
        (601, 607, 613, 617, 619, 631),
        (30, 45),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 2),
        "Crossing bought certainty at an explicit spread cost. Compare the saved time and available liquidity with slippage and shortfall; urgency should be a reason, not a reflex.",
    ),
    CurriculumLesson(
        "07",
        "Bid absorption",
        "Identify repeated aggressive selling that fails to dislodge replenishing bid liquidity.",
        "absorption_bid",
        (V.X1_00, V.X2_00, V.X5_00),
        (L.DEEP, L.VERY_DEEP),
        (701, 709, 719, 727, 733, 739),
        (45, 60),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 1),
        "Absorption was the relationship between sell flow and limited downside progress, not merely a large bid. Review replenishment and the price response after repeated hits.",
    ),
    CurriculumLesson(
        "08",
        "Ask absorption",
        "Identify repeated aggressive buying that fails to lift replenishing ask liquidity.",
        "absorption_ask",
        (V.X1_00, V.X2_00, V.X5_00),
        (L.DEEP, L.VERY_DEEP),
        (809, 811, 821, 823, 827, 829),
        (45, 60),
        _objective(ObjectiveType.LIQUIDATE, (500, 1_000), 1),
        "Ask absorption was visible when buy aggression produced little upside progress while offers replenished. A large displayed offer alone was not sufficient evidence.",
    ),
    CurriculumLesson(
        "09",
        "Failed breakout",
        "Recognize directional extension that loses follow-through and returns toward prior value.",
        "mean_reversion",
        (V.X0_50, V.X1_00, V.X2_00),
        (L.THIN, L.NORMAL),
        (907, 911, 919, 929, 937, 941),
        (45, 60),
        _objective(ObjectiveType.ROUND_TRIP, (500, 1_000), 2),
        "The failure was confirmed by lost follow-through and returning flow, not by guessing the turning tick. Review whether your round trip responded to evidence or anticipated reversal.",
    ),
    CurriculumLesson(
        "10",
        "Liquidity withdrawal",
        "Observe how cancellation and weak replenishment widen execution risk before price movement becomes orderly.",
        "liquidity_vacuum",
        (V.X0_50, V.X1_00, V.X2_00),
        (L.VERY_THIN, L.THIN),
        (1009, 1013, 1019, 1021, 1031, 1033),
        (30, 45),
        _objective(ObjectiveType.OBSERVE_ONLY, (0,), 0),
        "Withdrawal is an availability problem: queues disappear, spreads become unstable, and market orders may find little to execute against. Observation was the disciplined action here.",
    ),
    CurriculumLesson(
        "11",
        "Momentum burst",
        "Distinguish a short-lived directional burst from ordinary imbalance and manage entry urgency.",
        "momentum_up",
        (V.X1_00, V.X2_00, V.X5_00),
        (L.THIN, L.NORMAL),
        (1103, 1109, 1117, 1123, 1129, 1151),
        (30, 45),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 3),
        "Momentum combined aggressive buying, ask depletion, and price follow-through. Check whether your urgency increased only after those observable elements aligned.",
    ),
    CurriculumLesson(
        "12",
        "High-volume momentum",
        "Execute during sustained directional flow when event rate and order size both rise.",
        "momentum_up",
        (V.X5_00, V.X10_00),
        (L.NORMAL, L.DEEP),
        (1201, 1213, 1217, 1223, 1229, 1231),
        (30, 45),
        _objective(ObjectiveType.ACQUIRE, (1_000, 2_000), 4),
        "High activity did not remove execution tradeoffs. Review whether you confused abundant prints with abundant displayed liquidity, and whether crossing improved completion enough to justify cost.",
    ),
    CurriculumLesson(
        "13",
        "Thin-market momentum",
        "Manage momentum when shallow queues amplify spread, slippage, and overshoot risk.",
        "momentum_up",
        (V.X1_00, V.X2_00),
        (L.VERY_THIN, L.THIN),
        (1301, 1303, 1307, 1319, 1321, 1327),
        (30, 45),
        _objective(ObjectiveType.ACQUIRE, (500, 1_000), 5),
        "Thin momentum can move quickly while offering little executable depth. Compare requested size with the actual touch and note where aggressive orders expired or swept multiple levels.",
    ),
    CurriculumLesson(
        "14",
        "Panic / disorderly flow",
        "Prioritize controlled risk reduction when selling, cancellations, and liquidity loss become disorderly.",
        "panic",
        (V.X5_00, V.X10_00),
        (L.VERY_THIN, L.THIN),
        (1409, 1423, 1427, 1429, 1433, 1439),
        (30, 45),
        _objective(ObjectiveType.LIQUIDATE, (1_000, 2_000), 6),
        "Panic rewards risk control, not precision theater. Review completion, expired quantity, spread cost, and whether repeated replacement exposed you to worsening liquidity.",
    ),
)


def load_curriculum() -> dict[str, CurriculumLesson]:
    lessons = {lesson.lesson_id: lesson for lesson in LESSONS}
    expected = tuple(f"{index:02d}" for index in range(1, 15))
    if tuple(lessons) != expected:
        raise RuntimeError("curriculum must define lessons 01 through 14 in order")
    for lesson in lessons.values():
        get_scenario_definition(lesson.scenario_name)
    return lessons


def get_lesson(lesson_id: str) -> CurriculumLesson:
    normalized = str(lesson_id).zfill(2)
    lessons = load_curriculum()
    if normalized not in lessons:
        raise ValueError(f"unknown curriculum lesson: {lesson_id}")
    return lessons[normalized]


def prepare_lesson(
    lesson_id: str,
    mode: CurriculumMode,
    variation_seed: int,
) -> CurriculumDrill:
    return get_lesson(lesson_id).prepare(mode, variation_seed)

