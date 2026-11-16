# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Configuration files, persisted settings and tool value resolution."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from typing import Any, Optional

from .colour import Colour
from .defaults import (
    CONFIG_FILENAMES,
    CONFIG_SECTIONS,
    DEFAULT_CONFIG,
    DEFAULT_FONT,
)
from .paths import RuntimePaths, bundled_config_dir


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def write_json(path: str, value: Any) -> None:
    RuntimePaths.ensure_private_dir(os.path.dirname(path))
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json_object(path: str, description: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"could not read {description} {path}: {exc}")
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"invalid {description} {path}: the root must be an object"
        )
    return data


def merge_config_section(
    config: dict[str, Any], section: str, data: dict[str, Any], path: str
) -> None:
    if section == "colors":
        for alias, value in data.items():
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(
                    f"invalid colour in {path}: {alias!r} must map to a string"
                )
            config[section][str(alias)] = value
        return

    if section == "sizes":
        for alias, value in data.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise RuntimeError(
                    f"invalid size in {path}: {alias!r} must map to a positive number"
                )
            config[section][str(alias)] = value
        return

    if section == "groups":
        for group, aliases in data.items():
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) for alias in aliases
            ):
                raise RuntimeError(
                    f"invalid group in {path}: {group!r} must be a list of aliases"
                )
            config[section][str(group)] = list(aliases)
        return

    raise RuntimeError(f"unknown config section: {section!r}")


def parse_size(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("size must be a positive number")
    return value


class Config:
    """The effective colour/size/group configuration."""

    def __init__(self, paths: Optional[RuntimePaths] = None) -> None:
        self.paths = paths or RuntimePaths()
        self.data: dict[str, Any] = json_copy(DEFAULT_CONFIG)

    @property
    def colors(self) -> dict[str, Any]:
        return self.data.get("colors", {})

    @property
    def sizes(self) -> dict[str, Any]:
        return self.data.get("sizes", {})

    @property
    def groups(self) -> dict[str, Any]:
        return self.data.get("groups", {})

    def load(self) -> dict[str, Any]:
        """Load split config files over the built-in defaults.

        Only colours, sizes and groups are read from disk. Starting values are
        built into the program, and the current selection lives in
        settings.json.
        """
        config = json_copy(DEFAULT_CONFIG)

        legacy = self.paths.legacy_config_path()
        if os.path.exists(legacy):
            data = read_json_object(legacy, "config")
            for section in CONFIG_SECTIONS:
                value = data.get(section)
                if value is not None:
                    if not isinstance(value, dict):
                        raise RuntimeError(
                            f"invalid config {legacy}: {section!r} must be an object"
                        )
                    merge_config_section(config, section, value, legacy)

        for section in CONFIG_SECTIONS:
            path = self.paths.config_path(section)
            if os.path.exists(path):
                data = read_json_object(path, f"{section} config")
                merge_config_section(config, section, data, path)

        self.data = config
        return config

    def init(self) -> list[str]:
        """Create the split config files, migrating a legacy config.json."""
        messages: list[str] = []
        legacy = self.paths.legacy_config_path()

        if os.path.exists(legacy):
            effective = self.load()
            for section in CONFIG_SECTIONS:
                path = self.paths.config_path(section)
                write_json(path, effective[section])
                messages.append(f"wrote {path}")

            backup = f"{legacy}.legacy"
            if os.path.exists(backup):
                backup = f"{backup}.{int(time.time())}"
            os.replace(legacy, backup)
            messages.append(f"moved {legacy} to {backup}")
            return messages

        for section in CONFIG_SECTIONS:
            path = self.paths.config_path(section)
            if os.path.exists(path):
                messages.append(f"kept {path}")
            else:
                write_json(path, self.bundled_section(section))
                messages.append(f"wrote {path}")
        return messages

    @staticmethod
    def _empty_section(section: str) -> dict[str, Any]:
        return {}

    def bundled_section(self, section: str) -> Any:
        """Read a tracked config file, falling back to the built-in defaults."""
        path = os.path.join(bundled_config_dir(), CONFIG_FILENAMES[section])
        if os.path.exists(path):
            data = read_json_object(path, f"{section} config")
            probe = {section: self._empty_section(section)}
            merge_config_section(probe, section, data, path)
            return probe[section]
        return json_copy(DEFAULT_CONFIG[section])

    def resolve_colour(self, value: object) -> Colour:
        return Colour.resolve(value, self.colors)

    def resolve_size(self, value: object) -> tuple[float, str]:
        text = str(value).strip()
        sizes = self.sizes
        if text in sizes:
            return parse_size(str(sizes[text])), text
        return parse_size(text), text

    def tool_values(self, tool: str, settings: "Settings") -> dict[str, Any]:
        values = settings.data.get(tool, {})
        size_default = {"pen": "thin", "eraser": "eraser", "text": "text"}[tool]
        size, size_name = self.resolve_size(values.get("size", size_default))

        if tool == "eraser":
            return {
                "size": size,
                "size_name": size_name,
                "size_px": size,
            }

        colour = self.resolve_colour(values.get("color", "red"))
        result: dict[str, Any] = {
            "color": colour.rgba,
            "color_name": colour.name,
            "color_hex": colour.hex,
            "size": size,
            "size_name": size_name,
            "size_px": size,
        }
        if tool == "text":
            result.update(
                {
                    "font": str(values.get("font", DEFAULT_FONT)),
                    "bold": bool(values.get("bold", False)),
                    "italic": bool(values.get("italic", False)),
                }
            )
        return result

    def current(self, settings: "Settings") -> dict[str, dict[str, Any]]:
        data: dict[str, dict[str, Any]] = {}
        for tool in ("pen", "eraser", "text"):
            values = self.tool_values(tool, settings)
            item: dict[str, Any] = {
                "color": values.get("color_name"),
                "color_hex": values.get("color_hex"),
                "size": values["size_name"],
                "size_px": values["size_px"],
            }
            if tool == "text":
                item.update(
                    {
                        "font": values.get("font", DEFAULT_FONT),
                        "bold": values.get("bold", False),
                        "italic": values.get("italic", False),
                    }
                )
            data[tool] = item
        return data


def default_settings() -> dict[str, dict[str, Any]]:
    defaults = DEFAULT_CONFIG["defaults"]
    pen = defaults.get("pen", {})
    eraser = defaults.get("eraser", {})
    text = defaults.get("text", {})
    return {
        "pen": {
            "color": pen.get("color", "red"),
            "size": pen.get("size", "thin"),
        },
        "eraser": {
            "size": eraser.get("size", "eraser"),
        },
        "text": {
            "color": text.get("color", "red"),
            "size": text.get("size", "text"),
            "font": text.get("font", DEFAULT_FONT),
            "bold": bool(text.get("bold", False)),
            "italic": bool(text.get("italic", False)),
        },
    }


class Settings:
    """Current selections, persisted separately from the alias config."""

    def __init__(self, paths: Optional[RuntimePaths] = None) -> None:
        self.paths = paths or RuntimePaths()
        self.data = default_settings()

    @classmethod
    def load(cls, paths: Optional[RuntimePaths] = None) -> "Settings":
        settings = cls(paths)
        path = settings.paths.settings_path()
        if not os.path.exists(path):
            return settings
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return settings
        if not isinstance(data, dict):
            return settings
        for tool in ("pen", "eraser", "text"):
            stored = data.get(tool)
            if isinstance(stored, dict):
                for key in settings.data[tool]:
                    if key in stored and isinstance(
                        stored[key], (str, int, float, bool)
                    ):
                        settings.data[tool][key] = stored[key]
        return settings

    def save(self) -> None:
        write_json(self.paths.settings_path(), self.data)

    def set(self, tool: str, field: str, value: Any) -> None:
        self.data.setdefault(tool, {})[field] = value


__all__ = [
    "Config",
    "Settings",
    "default_settings",
    "json_copy",
    "merge_config_section",
    "parse_size",
    "read_json_object",
    "write_json",
]
