"""Canonical normalization and descriptive market-calibration measurements."""

from .comparison import (
    MAJOR_MEASUREMENT_PATHS,
    CalibrationComparison,
    CalibrationDifference,
    compare_reports,
)
from .measurements import MINIMUM_INFERENCE_SAMPLE_COUNT, measure_stream
from .fitting import (
    DEFAULT_OBJECTIVE,
    PARAMETER_SPECS,
    CalibrationConfig,
    calibrate_market,
    objective_loss,
    selected_parameter_specs,
)
from .models import (
    CALIBRATION_REPORT_SCHEMA_VERSION,
    NORMALIZED_MARKET_SCHEMA_VERSION,
    BookLevel,
    CalibrationMetric,
    CalibrationReport,
    NormalizedEventType,
    NormalizedMarketEvent,
    NormalizedMarketStream,
    ObservationCapability,
)
from .normalization import (
    normalize_exact_fixture,
    normalize_kirby_replay,
    normalize_reconstruction_fixture,
    normalize_simulation,
)
from .sources import resolve_measurement_source
from .profiles import (
    CALIBRATION_SOFTWARE_VERSION,
    CalibrationEvaluation,
    CalibrationObjective,
    CalibrationRun,
    CalibrationStageOutcome,
    DistanceKind,
    MarketProfile,
    ObjectiveTerm,
    ParameterSpec,
)
from .runtime import run_market_profile, run_parameterized_market

__all__ = [
    "CALIBRATION_REPORT_SCHEMA_VERSION",
    "CALIBRATION_SOFTWARE_VERSION",
    "DEFAULT_OBJECTIVE",
    "MINIMUM_INFERENCE_SAMPLE_COUNT",
    "MAJOR_MEASUREMENT_PATHS",
    "NORMALIZED_MARKET_SCHEMA_VERSION",
    "PARAMETER_SPECS",
    "BookLevel",
    "CalibrationComparison",
    "CalibrationConfig",
    "CalibrationDifference",
    "CalibrationEvaluation",
    "CalibrationMetric",
    "CalibrationReport",
    "CalibrationObjective",
    "CalibrationRun",
    "CalibrationStageOutcome",
    "DistanceKind",
    "MarketProfile",
    "NormalizedEventType",
    "NormalizedMarketEvent",
    "NormalizedMarketStream",
    "ObservationCapability",
    "ObjectiveTerm",
    "ParameterSpec",
    "calibrate_market",
    "compare_reports",
    "measure_stream",
    "objective_loss",
    "normalize_exact_fixture",
    "normalize_kirby_replay",
    "normalize_reconstruction_fixture",
    "normalize_simulation",
    "resolve_measurement_source",
    "run_market_profile",
    "run_parameterized_market",
    "selected_parameter_specs",
]
