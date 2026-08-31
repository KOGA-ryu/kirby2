"""Platform selection for the single governed :class:`DataPaths` provider."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kirby2.research.paths import DataPaths


class ReleasePathModeV1(str, Enum):
    PLATFORM = "PLATFORM"
    PORTABLE = "PORTABLE"
    DEVELOPMENT = "DEVELOPMENT"


@dataclass(frozen=True, slots=True)
class ReleasePathSelectionV1:
    """One inspectable path decision containing the sole semantic path map."""

    mode: ReleasePathModeV1
    platform_id: str
    source: str
    paths: DataPaths

    def __post_init__(self) -> None:
        if type(self.mode) is not ReleasePathModeV1:
            raise TypeError("release path mode is invalid")
        if type(self.platform_id) is not str or not self.platform_id:
            raise ValueError("release platform ID is required")
        if type(self.source) is not str or not self.source:
            raise ValueError("release path source is required")
        if type(self.paths) is not DataPaths:
            raise TypeError("release path selection must contain DataPaths")

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "platform_id": self.platform_id,
            "source": self.source,
            "data_paths": self.paths.as_dict(),
        }


def select_release_paths(
    mode: ReleasePathModeV1 = ReleasePathModeV1.PLATFORM,
    *,
    explicit_root: Path | None = None,
    checkout_root: Path | None = None,
    platform_id: str | None = None,
    home_directory: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> ReleasePathSelectionV1:
    """Select release, portable, or explicit development paths without writing.

    ``PLATFORM`` follows OS conventions unless ``explicit_root`` is supplied.
    ``PORTABLE`` requires that explicit root.  ``DEVELOPMENT`` requires an explicit
    checkout root and preserves the historical ``.kirby2/research`` location.
    """

    if type(mode) is not ReleasePathModeV1:
        raise TypeError("release path selection requires ReleasePathModeV1")
    selected_platform = _platform_id(platform_id)
    if mode is ReleasePathModeV1.DEVELOPMENT:
        if explicit_root is not None or checkout_root is None:
            raise ValueError(
                "development mode requires checkout_root and forbids explicit_root"
            )
        root = _resolved(checkout_root, "development checkout root")
        data_root = (root / ".kirby2" / "research").resolve(strict=False)
        source = "EXPLICIT_DEVELOPMENT_CHECKOUT"
    elif mode is ReleasePathModeV1.PORTABLE:
        if explicit_root is None or checkout_root is not None:
            raise ValueError(
                "portable mode requires explicit_root and forbids checkout_root"
            )
        data_root = _resolved(explicit_root, "portable data root")
        source = "EXPLICIT_PORTABLE_ROOT"
    else:
        if checkout_root is not None:
            raise ValueError("platform mode does not accept a checkout root")
        if explicit_root is not None:
            data_root = _resolved(explicit_root, "release data-root override")
            source = "EXPLICIT_RELEASE_OVERRIDE"
        else:
            data_root, source = _platform_default_root(
                selected_platform,
                home_directory=home_directory,
                environment=environment,
            )
    return ReleasePathSelectionV1(
        mode=mode,
        platform_id=selected_platform,
        source=source,
        paths=DataPaths(data_root),
    )


def platform_data_paths(
    *,
    explicit_root: Path | None = None,
) -> DataPaths:
    """Return the single platform-mode provider used by release entrypoints."""

    return select_release_paths(
        ReleasePathModeV1.PLATFORM,
        explicit_root=explicit_root,
    ).paths


def _platform_default_root(
    platform_id: str,
    *,
    home_directory: Path | None,
    environment: Mapping[str, str] | None,
) -> tuple[Path, str]:
    env = os.environ if environment is None else environment
    if not isinstance(env, Mapping) or any(
        type(key) is not str or type(value) is not str
        for key, value in env.items()
    ):
        raise TypeError("release path environment must be a text mapping")
    home = _resolved(
        Path.home() if home_directory is None else home_directory,
        "platform home directory",
    )
    if platform_id == "darwin":
        return (
            (home / "Library" / "Application Support" / "Kirby2").resolve(
                strict=False
            ),
            "MACOS_APPLICATION_SUPPORT",
        )
    if platform_id == "linux":
        xdg = env.get("XDG_DATA_HOME")
        if xdg is not None:
            base = _resolved(Path(xdg), "XDG_DATA_HOME")
            source = "XDG_DATA_HOME"
        else:
            base = (home / ".local" / "share").resolve(strict=False)
            source = "XDG_DATA_HOME_FALLBACK"
        return (base / "kirby2").resolve(strict=False), source
    if platform_id == "win32":
        local_app_data = env.get("LOCALAPPDATA")
        if local_app_data is not None:
            base = _resolved(Path(local_app_data), "LOCALAPPDATA")
            source = "WINDOWS_LOCALAPPDATA"
        else:
            base = (home / "AppData" / "Local").resolve(strict=False)
            source = "WINDOWS_LOCALAPPDATA_FALLBACK"
        return (base / "Kirby2").resolve(strict=False), source
    raise ValueError(f"unsupported release platform: {platform_id}")


def _platform_id(value: str | None) -> str:
    candidate = sys.platform if value is None else value
    if type(candidate) is not str or not candidate:
        raise ValueError("release platform ID is required")
    normalized = candidate.casefold()
    if normalized.startswith("linux"):
        return "linux"
    if normalized in {"darwin", "macos"}:
        return "darwin"
    if normalized in {"win32", "cygwin", "windows"}:
        return "win32"
    return normalized


def _resolved(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a pathlib.Path")
    if not path.is_absolute():
        raise ValueError(f"{label} must be explicit and absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} cannot be resolved safely") from error
    if resolved == Path(resolved.anchor):
        raise ValueError(f"{label} cannot be the filesystem anchor")
    return resolved


__all__ = [
    "ReleasePathModeV1",
    "ReleasePathSelectionV1",
    "platform_data_paths",
    "select_release_paths",
]
