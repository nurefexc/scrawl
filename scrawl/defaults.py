# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Built-in defaults and configuration schema constants.

The aliases below are the compiled-in fallback. The project also tracks a
``config/`` directory with the same aliases, which ``scrawl config --init``
uses when it creates the user's runtime files.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PEN_COLOR = "#ff3b30"
DEFAULT_PEN_SIZE = 4.0
DEFAULT_ERASER_SIZE = 30.0
DEFAULT_TEXT_COLOR = "#ff3b30"
DEFAULT_TEXT_SIZE = 32.0
DEFAULT_FONT = "sans"

DEFAULT_CONFIG: dict[str, Any] = {
    "colors": {
        "red": "#ff3b30",
        "orange": "#ff9500",
        "yellow": "#ffcc00",
        "green": "#34c759",
        "neon": "#00ff9d",
        "blue": "#0a84ff",
        "indigo": "#5e5ce6",
        "purple": "#bf5af2",
        "pink": "#ff2d55",
        "white": "#ffffff",
        "black": "#000000",
        "tokyo-text": "#c0caf5",
        "tokyo-blue": "#7aa2f7",
        "tokyo-red": "#f7768e",
        "tokyo-green": "#9ece6a",
        "tokyo-yellow": "#e0af68",
        "tokyo-cyan": "#7dcfff",
        "tokyo-purple": "#bb9af7",
    },
    "sizes": {
        "hairline": 2,
        "thin": 4,
        "normal": 8,
        "bold": 16,
        "marker": 24,
        "eraser": 30,
        "eraser-large": 60,
        "eraser-xl": 100,
        "text-small": 20,
        "text": 32,
        "text-large": 48,
        "text-xl": 72,
    },
    "groups": {
        "pen": ["hairline", "thin", "normal", "bold", "marker"],
        "eraser": ["eraser", "eraser-large", "eraser-xl"],
        "text": ["text-small", "text", "text-large", "text-xl"],
    },
    "defaults": {
        "pen": {"color": "red", "size": "thin"},
        "eraser": {"size": "eraser"},
        "text": {"color": "red", "size": "text", "font": "sans"},
    },
}

SET_FIELDS = {
    "color": ("pen", "color"),
    "size": ("pen", "size"),
    "pen-color": ("pen", "color"),
    "pen-size": ("pen", "size"),
    "eraser-size": ("eraser", "size"),
    "text-color": ("text", "color"),
    "text-size": ("text", "size"),
    "text-font": ("text", "font"),
    "text-bold": ("text", "bold"),
    "text-italic": ("text", "italic"),
}

CONFIG_SECTIONS = ("colors", "sizes", "groups")
CONFIG_FILENAMES = {
    "colors": "colors.json",
    "sizes": "sizes.json",
    "groups": "groups.json",
}
LEGACY_CONFIG_FILENAME = "config.json"
