# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Cairo rendering of the drawing model."""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from .model import DrawingItem, Stroke, TextItem


class Renderer:
    """Own the backing cairo surface and paint items into it."""

    def __init__(self, cairo, width: int, height: int) -> None:
        self.cairo = cairo
        self.width = width
        self.height = height
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        self.cr = cairo.Context(self.surface)
        self.clear_surface()

    # -- surface ----------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        old_surface = self.surface
        self.width, self.height = width, height
        self.surface = self.cairo.ImageSurface(
            self.cairo.FORMAT_ARGB32, width, height
        )
        self.cr = self.cairo.Context(self.surface)
        self.clear_surface()
        cr = self.cr
        cr.save()
        try:
            cr.set_source_surface(old_surface, 0, 0)
            cr.paint()
        finally:
            cr.restore()

    def clear_surface(self) -> None:
        cr = self.cr
        cr.save()
        cr.set_operator(self.cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()

    # -- geometry ---------------------------------------------------------

    def clamp_bbox(
        self, bbox: tuple[float, float, float, float]
    ) -> Optional[tuple[float, float, float, float]]:
        x0, y0, x1, y1 = bbox
        if not all(math.isfinite(value) for value in bbox):
            return None
        x0 = max(0.0, min(float(self.width), x0))
        y0 = max(0.0, min(float(self.height), y0))
        x1 = max(0.0, min(float(self.width), x1))
        y1 = max(0.0, min(float(self.height), y1))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1, y1)

    @staticmethod
    def segment_bbox(
        first: tuple[float, float],
        second: tuple[float, float],
        width: float,
    ) -> tuple[float, float, float, float]:
        radius = width / 2.0 + 2.0
        return (
            min(first[0], second[0]) - radius,
            min(first[1], second[1]) - radius,
            max(first[0], second[0]) + radius,
            max(first[1], second[1]) + radius,
        )

    # -- text -------------------------------------------------------------

    def font_style(self, item: TextItem):
        cairo = self.cairo
        slant = cairo.FONT_SLANT_ITALIC if item.italic else cairo.FONT_SLANT_NORMAL
        weight = cairo.FONT_WEIGHT_BOLD if item.bold else cairo.FONT_WEIGHT_NORMAL
        return slant, weight

    def text_layout(self, item: TextItem) -> dict[str, Any]:
        cr = self.cr
        cr.save()
        try:
            slant, weight = self.font_style(item)
            cr.select_font_face(item.font, slant, weight)
            cr.set_font_size(item.size)
            extents = cr.font_extents()
            # pycairo builds differ here: some return a struct, some a tuple.
            if hasattr(extents, "ascent"):
                ascent = float(extents.ascent)
                descent = float(extents.descent)
            else:
                ascent = float(extents[0])
                descent = float(extents[1])
            lines = item.text.split("\n")
            widths = []
            for line in lines:
                line_extents = cr.text_extents(line)
                if hasattr(line_extents, "x_advance"):
                    widths.append(float(line_extents.x_advance))
                else:
                    widths.append(float(line_extents[4]))
        finally:
            cr.restore()

        line_height = item.size * 1.25
        width = max(widths) if widths else 0.0
        height = (len(lines) - 1) * line_height + ascent + descent if lines else 0.0
        return {
            "lines": lines,
            "widths": widths,
            "line_height": line_height,
            "ascent": ascent,
            "descent": descent,
            "bbox": (item.x, item.y, item.x + width, item.y + height),
        }

    def make_text_item(
        self,
        x: float,
        y: float,
        text: str,
        colour: tuple[float, float, float, float],
        size: float,
        font: str,
        bold: bool,
        italic: bool,
    ) -> TextItem:
        item = TextItem(x, y, text, colour, size, font, bold, italic)
        item.bbox = self.text_layout(item)["bbox"]
        return item

    def render_text(
        self, cr, item: TextItem, layout: Optional[dict[str, Any]] = None
    ) -> None:
        if not item.text:
            return
        layout = layout or self.text_layout(item)
        cr.save()
        try:
            slant, weight = self.font_style(item)
            cr.select_font_face(item.font, slant, weight)
            cr.set_font_size(item.size)
            cr.set_source_rgba(*item.color)
            for index, line in enumerate(layout["lines"]):
                baseline = item.y + layout["ascent"] + index * layout["line_height"]
                cr.move_to(item.x, baseline)
                cr.show_text(line)
        finally:
            cr.restore()

    # -- strokes and items ------------------------------------------------

    def render_stroke(self, cr, stroke: Stroke) -> None:
        if not stroke.points:
            return
        cr.save()
        try:
            if stroke.eraser:
                cr.set_operator(self.cairo.OPERATOR_CLEAR)
            else:
                cr.set_operator(self.cairo.OPERATOR_OVER)
                cr.set_source_rgba(*stroke.color)
            cr.set_line_width(max(0.1, stroke.width))
            cr.set_line_cap(self.cairo.LINE_CAP_ROUND)
            cr.set_line_join(self.cairo.LINE_JOIN_ROUND)

            if len(stroke.points) == 1:
                x, y = stroke.points[0]
                cr.arc(x, y, max(0.1, stroke.width / 2.0), 0, 2 * math.pi)
                cr.fill()
            else:
                x, y = stroke.points[0]
                cr.move_to(x, y)
                for x, y in stroke.points[1:]:
                    cr.line_to(x, y)
                cr.stroke()
        finally:
            cr.restore()

    def render_item(self, cr, item: DrawingItem) -> None:
        if isinstance(item, Stroke):
            self.render_stroke(cr, item)
        else:
            self.render_text(cr, item)

    def repaint(
        self,
        items: Iterable[DrawingItem],
        bbox: tuple[float, float, float, float],
    ) -> Optional[tuple[float, float, float, float]]:
        """Clear a region and replay every item that intersects it.

        Replaying a whole item inside the clip region (instead of
        incrementally adding segments) keeps translucent strokes uniform and
        makes eraser strokes behave like real erasers.
        """
        clamped = self.clamp_bbox(bbox)
        if clamped is None:
            return None
        x0, y0, x1, y1 = clamped
        cr = self.cr
        cr.save()
        try:
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.clip()
            cr.set_operator(self.cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(self.cairo.OPERATOR_OVER)
            for item in items:
                item_bbox = item.bbox
                if item_bbox is None:
                    continue
                if (
                    item_bbox[2] < x0
                    or item_bbox[0] > x1
                    or item_bbox[3] < y0
                    or item_bbox[1] > y1
                ):
                    continue
                self.render_item(cr, item)
        finally:
            cr.restore()
        return clamped

    def full_repaint(self, items: Iterable[DrawingItem]) -> None:
        self.clear_surface()
        for item in items:
            self.render_item(self.cr, item)

    # -- interactive decorations -----------------------------------------

    def draw_text_entry(self, cr, item: TextItem, caret_visible: bool) -> None:
        layout = self.text_layout(item)
        if item.text:
            self.render_text(cr, item, layout)

        if not caret_visible:
            return
        lines = layout["lines"] if item.text else [""]
        widths = layout["widths"] if item.text else [0.0]
        last = len(lines) - 1
        caret_x = item.x + widths[last]
        caret_top = item.y + last * layout["line_height"]
        caret_bottom = caret_top + layout["ascent"] + layout["descent"]
        cairo = self.cairo
        cr.save()
        try:
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_source_rgba(*item.color)
            width = max(1.0, min(3.0, item.size / 18.0))
            cr.rectangle(
                caret_x,
                caret_top,
                width,
                max(1.0, caret_bottom - caret_top),
            )
            cr.fill()
        finally:
            cr.restore()

    def draw_eraser_ring(self, cr, x: float, y: float, radius: float) -> None:
        cairo = self.cairo
        radius = max(1.0, radius)
        cr.save()
        try:
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_line_width(3.0)
            cr.set_source_rgba(0.05, 0.05, 0.05, 0.85)
            cr.arc(x, y, radius, 0, 2 * math.pi)
            cr.stroke()
            cr.set_line_width(1.0)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
            cr.arc(x, y, radius, 0, 2 * math.pi)
            cr.stroke()
        finally:
            cr.restore()
