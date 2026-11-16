# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Colour parsing and value objects."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Mapping

RGBA = tuple[float, float, float, float]


def parse_rgba(text: str) -> RGBA:
    """Parse a GTK/CSS colour string into a normalised RGBA tuple."""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
    except Exception as exc:  # pragma: no cover - dependency failure path
        raise argparse.ArgumentTypeError(f"GTK3/Gdk is not available: {exc}")

    rgba = Gdk.RGBA()
    if not rgba.parse(text):
        raise argparse.ArgumentTypeError(
            f"invalid color: {text!r} (try red, #ff0000 or rgb(255,0,0))"
        )
    return (
        float(rgba.red),
        float(rgba.green),
        float(rgba.blue),
        float(rgba.alpha),
    )


def rgba_to_hex(colour: RGBA) -> str:
    red, green, blue, alpha = (
        max(0.0, min(1.0, float(value))) for value in colour
    )
    r = int(round(red * 255))
    g = int(round(green * 255))
    b = int(round(blue * 255))
    if alpha < 0.999:
        return f"#{r:02x}{g:02x}{b:02x}{int(round(alpha * 255)):02x}"
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass(frozen=True)
class Colour:
    """A resolved colour together with the name or alias the user chose."""

    rgba: RGBA
    name: str

    @classmethod
    def parse(cls, text: str) -> "Colour":
        return cls(parse_rgba(text), text)

    @classmethod
    def resolve(cls, value: object, palette: Mapping[str, object]) -> "Colour":
        text = str(value).strip()
        if text in palette:
            return cls(parse_rgba(str(palette[text])), text)
        return cls(parse_rgba(text), text)

    @property
    def hex(self) -> str:
        return rgba_to_hex(self.rgba)

    def as_list(self) -> list[float]:
        return [float(channel) for channel in self.rgba]
