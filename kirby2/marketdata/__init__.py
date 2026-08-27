"""Capability-honest local market-data normalization and storage."""

from .adapters import ADAPTERS, RawDataset, load_raw_dataset
from .models import (
    CAPABILITY_RECORD_TYPES,
    DATA_QUALITY_SCHEMA_VERSION,
    DATASET_MANIFEST_SCHEMA_VERSION,
    DATASET_SCHEMA_VERSIONS,
    MARKET_DATA_SCHEMA_VERSION,
    DataGap,
    DataQualityIssue,
    DataQualityReport,
    DatasetManifest,
    NormalizedDataset,
    NormalizedMarketRecord,
    QualitySeverity,
    RecordType,
    ReplayCapabilityDecision,
    ReplayMode,
    SourceCapability,
    TimestampPrecision,
)
from .normalization import normalize_raw_dataset, normalize_timestamp, replay_capability
from .store import DatasetVerificationReport, MarketDataStore

__all__ = [
    "ADAPTERS",
    "CAPABILITY_RECORD_TYPES",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "DATASET_SCHEMA_VERSIONS",
    "DATA_QUALITY_SCHEMA_VERSION",
    "MARKET_DATA_SCHEMA_VERSION",
    "DataGap",
    "DataQualityIssue",
    "DataQualityReport",
    "DatasetManifest",
    "DatasetVerificationReport",
    "MarketDataStore",
    "NormalizedDataset",
    "NormalizedMarketRecord",
    "QualitySeverity",
    "RawDataset",
    "RecordType",
    "ReplayCapabilityDecision",
    "ReplayMode",
    "SourceCapability",
    "TimestampPrecision",
    "load_raw_dataset",
    "normalize_raw_dataset",
    "normalize_timestamp",
    "replay_capability",
]
