"""Deterministic volume-by-liquidity scenario matrix inspection."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.simulation import LiquidityPreset, VolumePreset

from .market import ScenarioDefinition, ScenarioRun, run_market_scenario


@dataclass(slots=True)
class MatrixCell:
    volume: VolumePreset
    liquidity: LiquidityPreset
    run: ScenarioRun

    def row(self) -> dict[str, object]:
        metrics = self.run.metrics()
        return {
            "average_depth": metrics["average_depth"],
            "average_spread_ticks": metrics["average_spread_ticks"],
            "cancellation_count": metrics["cancellation_count"],
            "invariant_status": metrics["invariant_status"],
            "liquidity": self.liquidity.value,
            "price_displacement_ticks": metrics["price_displacement_ticks"],
            "trade_count": metrics["total_trades"],
            "traded_volume": metrics["total_volume"],
            "volume": self.volume.value,
        }


@dataclass(slots=True)
class ScenarioMatrix:
    definition: ScenarioDefinition
    seed: int
    seconds: int
    cells: tuple[MatrixCell, ...]

    def render(self) -> str:
        headers = (
            "VOLUME",
            "LIQUIDITY",
            "TRADES",
            "TRADED_VOL",
            "PRICE_D",
            "AVG_SPREAD",
            "AVG_DEPTH",
            "CANCELS",
            "STATUS",
        )
        rows: list[tuple[str, ...]] = []
        for cell in self.cells:
            data = cell.row()
            rows.append(
                (
                    str(data["volume"]),
                    str(data["liquidity"]),
                    str(data["trade_count"]),
                    str(data["traded_volume"]),
                    str(data["price_displacement_ticks"]),
                    self._format_metric(data["average_spread_ticks"]),
                    self._format_metric(data["average_depth"]),
                    str(data["cancellation_count"]),
                    str(data["invariant_status"]),
                )
            )
        widths = [
            max(len(headers[index]), *(len(row[index]) for row in rows))
            for index in range(len(headers))
        ]
        lines = [self._render_row(headers, widths)]
        lines.append(self._render_row(tuple("-" * width for width in widths), widths))
        lines.extend(self._render_row(row, widths) for row in rows)
        return "\n".join(lines)

    @staticmethod
    def _format_metric(value: object) -> str:
        if value is None:
            return "NA"
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    @staticmethod
    def _render_row(row: tuple[str, ...], widths: list[int]) -> str:
        return "  ".join(value.rjust(widths[index]) for index, value in enumerate(row))


def run_scenario_matrix(
    definition: ScenarioDefinition,
    seed: int | None = None,
    seconds: int = 30,
) -> ScenarioMatrix:
    actual_seed = definition.seed if seed is None else seed
    cells: list[MatrixCell] = []
    for volume in VolumePreset:
        for liquidity in LiquidityPreset:
            run = run_market_scenario(
                definition,
                seed=actual_seed,
                seconds=seconds,
                relative_volume=volume,
                liquidity=liquidity,
            )
            cells.append(MatrixCell(volume, liquidity, run))
    return ScenarioMatrix(definition, actual_seed, seconds, tuple(cells))

