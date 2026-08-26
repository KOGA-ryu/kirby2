"""Named, editable hotkey-layout persistence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .bindings import BindingMap, SessionCommand


LAYOUT_SCHEMA_VERSION = 1
DEFAULT_LAYOUT_DIRECTORY = Path(".kirby2") / "layouts"
_VALID_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class HotkeyLayout:
    name: str
    bindings: BindingMap

    def __post_init__(self) -> None:
        if not _VALID_NAME.fullmatch(self.name):
            raise ValueError(
                "layout name must start with a lowercase letter and contain only "
                "lowercase letters, digits, underscores, or hyphens"
            )

    @classmethod
    def default(cls) -> HotkeyLayout:
        return cls("layout_default", BindingMap.default())

    def as_dict(self) -> dict[str, object]:
        return {
            "bindings": self.bindings.as_dict(),
            "name": self.name,
            "schema_version": LAYOUT_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> HotkeyLayout:
        if payload.get("schema_version") != LAYOUT_SCHEMA_VERSION:
            raise ValueError("unsupported hotkey layout schema version")
        raw_bindings = payload.get("bindings")
        if not isinstance(raw_bindings, dict):
            raise ValueError("layout bindings must be an object")
        bindings: dict[str, SessionCommand] = {}
        for key, command in raw_bindings.items():
            if not isinstance(key, str) or not isinstance(command, str):
                raise ValueError("layout bindings must map string keys to commands")
            bindings[key] = SessionCommand(command)
        return cls(str(payload.get("name", "")), BindingMap(bindings))


class LayoutStore:
    def __init__(self, directory: Path = DEFAULT_LAYOUT_DIRECTORY) -> None:
        self.directory = directory

    def save(self, layout: HotkeyLayout) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(layout.name)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(layout.as_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def load(self, name: str) -> HotkeyLayout:
        path = self._path(name)
        if not path.is_file():
            raise ValueError(f"unknown hotkey layout: {name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hotkey layout must contain a JSON object")
        layout = HotkeyLayout.from_dict(payload)
        if layout.name != name:
            raise ValueError("hotkey layout name does not match its filename")
        return layout

    def list_names(self) -> tuple[str, ...]:
        if not self.directory.is_dir():
            return ()
        names: list[str] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                layout = self.load(path.stem)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            names.append(layout.name)
        return tuple(names)

    def _path(self, name: str) -> Path:
        if not _VALID_NAME.fullmatch(name):
            raise ValueError("invalid hotkey layout name")
        return self.directory / f"{name}.json"
