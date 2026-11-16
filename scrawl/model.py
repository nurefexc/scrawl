# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Drawing model: strokes, text items and the ordered annotation list."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class Stroke:
    color: tuple[float, float, float, float]
    width: float
    eraser: bool = False
    points: list[tuple[float, float]] = field(default_factory=list)
    bbox: Optional[tuple[float, float, float, float]] = None

    def add_point(self, x: float, y: float) -> None:
        self.points.append((x, y))
        radius = self.width / 2.0 + 2.0
        if self.bbox is None:
            self.bbox = (x - radius, y - radius, x + radius, y + radius)
        else:
            x0, y0, x1, y1 = self.bbox
            self.bbox = (
                min(x0, x - radius),
                min(y0, y - radius),
                max(x1, x + radius),
                max(y1, y + radius),
            )


@dataclass
class TextItem:
    x: float
    y: float
    text: str
    color: tuple[float, float, float, float]
    size: float
    font: str
    bold: bool = False
    italic: bool = False
    bbox: Optional[tuple[float, float, float, float]] = None


DrawingItem = Stroke | TextItem


class Drawing:
    """Ordered annotation model.

    The list order is the paint order. Undo removes the most recent item, and
    eraser strokes are ordinary items painted with cairo's clear operator.
    """

    def __init__(self) -> None:
        self.items: list[DrawingItem] = []
        self.current_stroke: Optional[Stroke] = None

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[DrawingItem]:
        return iter(self.items)

    def add(self, item: DrawingItem) -> None:
        self.items.append(item)

    def undo(self) -> bool:
        """Remove the most recent item; return whether anything was removed."""
        self.current_stroke = None
        if not self.items:
            return False
        self.items.pop()
        return True

    def clear(self) -> None:
        self.items.clear()
        self.current_stroke = None
