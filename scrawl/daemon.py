# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""The scrawl daemon: window, drawing state, commands and shutdown."""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import math
import os
import signal
import socket
import sys
import time
import traceback
import warnings
from typing import Any, Optional

from . import VERSION
from .defaults import (
    DEFAULT_ERASER_SIZE,
    DEFAULT_FONT,
    DEFAULT_PEN_SIZE,
    DEFAULT_TEXT_SIZE,
)
from .ipc import DaemonServer
from .model import Drawing, Stroke, TextItem
from .overlay import OverlayWindow
from .paths import RuntimePaths
from .renderer import Renderer
from .workspace import WorkspaceWatcher

# GTK 3.24 deprecates a few APIs that are still the cleanest way to build a
# full-screen ARGB overlay. Keep the daemon log readable.
warnings.filterwarnings("ignore", category=DeprecationWarning)

# X11 modifier masks and keysyms used when forwarding a GTK shortcut through
# XTest. GTK masks and X11 masks are different namespaces, so the translation
# lives next to the forwarding code.
X11_CONTROL_MASK = 0x0004
X11_MOD1_MASK = 0x0008
X11_MOD4_MASK = 0x0040
X11_KEY_CONTROL_L = 0xFFE3
X11_KEY_ALT_L = 0xFFE9
X11_KEY_SUPER_L = 0xFFEB


class ScrawlDaemon:
    def __init__(self, paths: Optional[RuntimePaths] = None) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gdk, GdkX11, GLib, Gtk

        self.Gdk = Gdk
        self.GLib = GLib
        self.Gtk = Gtk

        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("no X display found (is DISPLAY set?)")
        if not isinstance(display, GdkX11.X11Display):
            raise RuntimeError("scrawl currently supports X11 only")
        self.display = display

        import cairo

        self.cairo = cairo
        self.paths = paths or RuntimePaths()

        self.screen = Gdk.Screen.get_default()
        self.width = int(self.screen.get_width())
        self.height = int(self.screen.get_height())
        if self.width <= 0 or self.height <= 0:
            raise RuntimeError("invalid screen size")

        self.renderer = Renderer(cairo, self.width, self.height)
        self.drawing = Drawing()

        self.mode: Optional[str] = None
        self.params: dict[str, Any] = {}
        self.client: Optional[socket.socket] = None
        self.client_watch: Optional[int] = None
        self.eraser_pos: Optional[tuple[float, float]] = None
        self.ring_bbox: Optional[tuple[float, float, float, float]] = None
        self.text_edit: Optional[dict[str, Any]] = None
        self.caret_visible = True
        self.caret_timer: Optional[int] = None
        self.visible = True
        self.shutting_down = False
        self.top_timer: Optional[int] = None
        self.overlay: Optional[OverlayWindow] = None
        self.workspace: Optional[WorkspaceWatcher] = None
        self.loop = None

        # Modifier combinations that usually belong to the window manager or
        # to application shortcuts. Any-key exit replays these after releasing
        # the grab so the shortcut is not lost. Keep in sync with GDK masks.
        self.system_modifier_mask = (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.MOD1_MASK
            | Gdk.ModifierType.MOD4_MASK
            | Gdk.ModifierType.SUPER_MASK
            | Gdk.ModifierType.META_MASK
        )
        self._x11 = None
        self._xtst = None
        self._xtest_display = None
        self._xtest_failed = False

        # Claim the lock before creating the overlay, so a second daemon exits
        # without flashing a full-screen window.
        self.server = DaemonServer(
            self.paths, self.GLib, self._on_command, self._log
        )
        self.server.start()
        atexit.register(self._cleanup_files)

        try:
            self.overlay = OverlayWindow(
                gdk=Gdk,
                gtk=Gtk,
                cairo=cairo,
                display=display,
                screen=self.screen,
                log=self._log,
                on_draw=self._on_draw,
                on_key_press=self._on_key_press,
                on_button_press=self._on_button_press,
                on_button_release=self._on_button_release,
                on_motion=self._on_motion,
                on_leave=self._on_leave,
                on_destroy=self._on_window_destroy,
            )
        except Exception:
            self._cleanup_files()
            raise

        self.workspace = WorkspaceWatcher(
            self.GLib,
            on_hide=self._workspace_hide,
            on_show=self._workspace_show,
            log=self._log,
        )
        # An override-redirect window can be covered by windows mapped later.
        # Re-raising on a slow timer keeps annotations on top without ever
        # taking input focus.
        self.top_timer = self.GLib.timeout_add_seconds(2, self._keep_on_top)
        self.workspace.start()
        self._install_signal_handlers()
        self.screen.connect("size-changed", self._on_screen_size_changed)
        self._log(
            f"daemon started (pid={os.getpid()}, "
            f"screen={self.width}x{self.height}, "
            f"desktop={self.desktop}, socket={self.paths.socket_path()})"
        )

    # -- convenience properties ------------------------------------------

    @property
    def desktop(self) -> Optional[int]:
        return self.workspace.bound if self.workspace is not None else None

    @property
    def current_desktop(self) -> Optional[int]:
        return self.workspace.current if self.workspace is not None else None

    @property
    def workspace_hidden(self) -> bool:
        return bool(self.workspace.hidden) if self.workspace is not None else False

    # -- logging ----------------------------------------------------------

    def _log(self, message: str) -> None:
        print(
            f"[{time.strftime('%H:%M:%S')}] {message}",
            file=sys.stderr,
            flush=True,
        )

    # -- window state -----------------------------------------------------

    def _workspace_hide(self, bound: int, current: int) -> None:
        if self.mode is not None:
            self._end_mode("workspace-changed")
        if self.visible and self.overlay is not None and self.overlay.gdkwin is not None:
            self.overlay.hide()
        self._log(f"workspace {bound} left (now {current}); overlay hidden")

    def _workspace_show(self, bound: int, _current: int) -> None:
        if (
            self.visible
            and self.overlay is not None
            and self.overlay.gdkwin is not None
        ):
            self.overlay.show()
            self.overlay.set_clickthrough(self.mode is None)
            self.overlay.queue_draw()
            self.overlay.raise_()
        self._log(f"workspace {bound} active; overlay shown")

    def _ensure_visible(self) -> None:
        if self.workspace is None or self.overlay is None:
            return
        self.workspace.adopt()
        if not self.visible:
            self.visible = True
            self.workspace.clear_hidden()
            self.overlay.show()
            self.overlay.set_clickthrough(self.mode is None)
        elif self.workspace.hidden:
            self.workspace.clear_hidden()
            self.overlay.show()
            self.overlay.set_clickthrough(self.mode is None)
        self.overlay.raise_()

    def _keep_on_top(self) -> bool:
        if self.shutting_down:
            return False
        # An interactive mode has just raised the window and holds the pointer
        # grab, so re-raising it during drawing would only disturb frames.
        if (
            self.mode is None
            and self.visible
            and not self.workspace_hidden
            and self.overlay is not None
            and self.overlay.gdkwin is not None
        ):
            try:
                self.overlay.gdkwin.raise_()
            except Exception:
                return False
        return True

    def _hide_overlay(self) -> None:
        self._end_mode("hidden")
        self.visible = False
        if self.overlay is not None:
            self.overlay.hide()

    def _show_overlay(self) -> None:
        self._ensure_visible()
        if self.overlay is not None:
            self.overlay.queue_draw()

    # -- entering/leaving interactive modes -------------------------------

    def _enter_interactive(self, cursor_type: str) -> None:
        self._ensure_visible()
        if self.overlay is not None:
            self.overlay.enter_interactive(cursor_type)

    def _leave_interactive(self) -> None:
        if self.overlay is not None:
            self.overlay.leave_interactive()

    # -- mode lifecycle ---------------------------------------------------

    def _set_mode(
        self, mode: str, params: dict[str, Any], client: socket.socket
    ) -> None:
        if self.mode is not None:
            self._end_mode("replaced", reply=True)
        self.mode = mode
        self.params = dict(params)
        self.client = client
        self._watch_client(client)
        self.text_edit = None
        self.drawing.current_stroke = None
        self._clear_ring()
        self._ensure_visible()

        if mode == "pen":
            self._enter_interactive("CROSSHAIR")
        elif mode == "eraser":
            self._enter_interactive("BLANK_CURSOR")
        elif mode == "text":
            self._enter_interactive("XTERM")
            at = self.params.get("at")
            if at is not None:
                self._begin_text_entry(float(at[0]), float(at[1]))
        else:
            raise ValueError(f"unknown mode: {mode}")

        self._log(f"mode={mode} size={self.params.get('size', '-')}")

    def _end_mode(self, reason: str, reply: bool = True) -> None:
        if self.mode is None and self.client is None:
            return
        self.drawing.current_stroke = None
        if self.text_edit is not None:
            self._cancel_text_entry()
        self._clear_ring()
        self.mode = None
        self.params = {}
        self._leave_interactive()

        client = self.client
        self.client = None
        self._unwatch_client()
        if reply and client is not None:
            self._reply(client, {"ok": True, "event": "done", "reason": reason})
        if self.overlay is not None:
            self.overlay.queue_draw()

    # -- client liveness watch --------------------------------------------

    def _watch_client(self, client: socket.socket) -> None:
        self._unwatch_client()
        try:
            self.client_watch = self.GLib.io_add_watch(
                client.fileno(),
                self.GLib.PRIORITY_DEFAULT,
                self.GLib.IOCondition.HUP
                | self.GLib.IOCondition.ERR
                | self.GLib.IOCondition.IN,
                self._on_client_event,
            )
        except Exception:
            self.client_watch = None

    def _unwatch_client(self) -> None:
        if self.client_watch is not None:
            try:
                self.GLib.source_remove(self.client_watch)
            except Exception:
                pass
            self.client_watch = None

    def _on_client_event(self, _fd: int, condition) -> bool:
        GLib = self.GLib
        client = self.client
        if client is None:
            return False
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self._end_mode("client-disconnected", reply=False)
            return False
        if condition & GLib.IOCondition.IN:
            try:
                data = client.recv(1, socket.MSG_PEEK)
            except OSError:
                data = b""
            if not data:
                self._end_mode("client-disconnected", reply=False)
                return False
        return True

    # -- drawing surface --------------------------------------------------

    def _queue_region(self, bbox: tuple[float, float, float, float]) -> None:
        if self.overlay is None:
            return
        clamped = self.renderer.clamp_bbox(bbox)
        if clamped is not None:
            self.overlay.queue_draw_area(clamped)

    def _repaint_region(self, bbox: tuple[float, float, float, float]) -> None:
        """Clear a region of the backing surface and replay intersecting items."""
        clamped = self.renderer.repaint(self.drawing.items, bbox)
        if clamped is not None and self.overlay is not None:
            self.overlay.queue_draw_area(clamped)

    def _schedule_region(self, bbox: tuple[float, float, float, float]) -> None:
        """Paint a dirty region immediately.

        Repainting here instead of from an idle callback keeps the stroke
        under the cursor even when pointer events arrive faster than the main
        loop can drain them. Cairo and GTK merge the queued draw areas, so a
        burst of motion events still composites at the display refresh rate.
        """
        self._repaint_region(bbox)

    def _full_repaint(self) -> None:
        self.renderer.full_repaint(self.drawing.items)
        if self.overlay is not None:
            self.overlay.queue_draw()

    def _segment_bbox(
        self,
        first: tuple[float, float],
        second: tuple[float, float],
        width: float,
    ) -> tuple[float, float, float, float]:
        return self.renderer.segment_bbox(first, second, width)

    # -- painting ---------------------------------------------------------

    def _on_draw(self, _widget, cr) -> bool:
        cairo = self.cairo
        cr.save()
        try:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_source_surface(self.renderer.surface, 0, 0)
            cr.paint()

            if self.text_edit is not None:
                self._draw_text_edit(cr)
            if self.mode == "eraser" and self.eraser_pos is not None:
                self._draw_eraser_ring(cr)
        finally:
            cr.restore()
        return False

    def _draw_eraser_ring(self, cr) -> None:
        if self.eraser_pos is None:
            return
        x, y = self.eraser_pos
        radius = max(1.0, float(self.params.get("size", DEFAULT_ERASER_SIZE)) / 2.0)
        self.renderer.draw_eraser_ring(cr, x, y, radius)

    def _ring_bbox_for(
        self, x: float, y: float
    ) -> tuple[float, float, float, float]:
        radius = max(1.0, float(self.params.get("size", DEFAULT_ERASER_SIZE)) / 2.0)
        return (x - radius - 3, y - radius - 3, x + radius + 3, y + radius + 3)

    def _clear_ring(self) -> None:
        if self.ring_bbox is not None:
            self._queue_region(self.ring_bbox)
            self.ring_bbox = None
        self.eraser_pos = None

    def _update_ring(self, x: float, y: float) -> None:
        new_bbox = self._ring_bbox_for(x, y)
        if self.ring_bbox is not None:
            self._queue_region(self.ring_bbox)
        self.eraser_pos = (x, y)
        self.ring_bbox = new_bbox
        self._queue_region(new_bbox)

    # -- interactive text editing ----------------------------------------

    def _begin_text_entry(self, x: float, y: float) -> None:
        if self.text_edit is not None:
            self._cancel_text_entry()
        self.text_edit = {"x": x, "y": y, "buffer": ""}
        self.caret_visible = True
        self._reset_caret_timer()
        self._queue_region(self._text_edit_region())

    def _text_edit_item(self, text: str) -> TextItem:
        if self.text_edit is None:
            raise RuntimeError("no active text entry")
        params = self.params
        return self.renderer.make_text_item(
            float(self.text_edit["x"]),
            float(self.text_edit["y"]),
            text,
            tuple(params.get("color", (0.0, 0.0, 0.0, 1.0))),
            float(params.get("size", DEFAULT_TEXT_SIZE)),
            str(params.get("font", DEFAULT_FONT)),
            bool(params.get("bold", False)),
            bool(params.get("italic", False)),
        )

    def _text_edit_region(self) -> tuple[float, float, float, float]:
        if self.text_edit is None:
            return (0, 0, 0, 0)
        # A sample glyph gives a usable box even while the buffer is empty.
        item = self._text_edit_item(self.text_edit["buffer"] or "M")
        layout = self.renderer.text_layout(item)
        x0, y0, x1, y1 = layout["bbox"]
        return (x0 - 4, y0 - 4, x1 + 12, y1 + 6)

    def _draw_text_edit(self, cr) -> None:
        if self.text_edit is None:
            return
        item = self._text_edit_item(self.text_edit["buffer"])
        self.renderer.draw_text_entry(cr, item, self.caret_visible)

    def _reset_caret_timer(self) -> None:
        if self.caret_timer is not None:
            try:
                self.GLib.source_remove(self.caret_timer)
            except Exception:
                pass
        self.caret_timer = self.GLib.timeout_add(530, self._blink_caret)

    def _blink_caret(self) -> bool:
        if self.text_edit is None:
            self.caret_timer = None
            return False
        self.caret_visible = not self.caret_visible
        self._queue_region(self._text_edit_region())
        return True

    def _cancel_text_entry(self) -> None:
        if self.text_edit is None:
            return
        region = self._text_edit_region()
        self.text_edit = None
        if self.caret_timer is not None:
            try:
                self.GLib.source_remove(self.caret_timer)
            except Exception:
                pass
            self.caret_timer = None
        self._queue_region(region)

    def _commit_text_entry(self) -> None:
        if self.text_edit is None:
            return
        region = self._text_edit_region()
        item = self._text_edit_item(self.text_edit["buffer"])
        self.text_edit = None
        if self.caret_timer is not None:
            try:
                self.GLib.source_remove(self.caret_timer)
            except Exception:
                pass
            self.caret_timer = None
        self._queue_region(region)

        if not item.text:
            return
        self.drawing.add(item)
        if item.bbox is not None:
            self._schedule_region(item.bbox)

        if self.params.get("exit_after_text"):
            self._end_mode("committed")

    def _refresh_text(self, old_region: tuple[float, float, float, float]) -> None:
        new_region = self._text_edit_region()
        self._queue_region(
            (
                min(old_region[0], new_region[0]),
                min(old_region[1], new_region[1]),
                max(old_region[2], new_region[2]),
                max(old_region[3], new_region[3]),
            )
        )
        self.caret_visible = True
        self._reset_caret_timer()

    def _insert_text(self, text: str) -> None:
        if self.text_edit is None or not text:
            return
        old = self._text_edit_region()
        self.text_edit["buffer"] += text
        self._refresh_text(old)

    def _delete_text_char(self) -> None:
        if self.text_edit is None or not self.text_edit["buffer"]:
            return
        old = self._text_edit_region()
        self.text_edit["buffer"] = self.text_edit["buffer"][:-1]
        self._refresh_text(old)

    # -- mouse and keyboard ----------------------------------------------

    def _on_button_press(self, _widget, event) -> bool:
        if self.mode is None:
            return False
        if event.button == 3:
            self._end_mode("right-click")
            return True
        if event.button != 1:
            return False

        x, y = float(event.x), float(event.y)

        if self.mode in ("pen", "eraser"):
            stroke = Stroke(
                color=tuple(self.params.get("color", (0.0, 0.0, 0.0, 1.0))),
                width=float(
                    self.params.get(
                        "size",
                        DEFAULT_PEN_SIZE
                        if self.mode == "pen"
                        else DEFAULT_ERASER_SIZE,
                    )
                ),
                eraser=self.mode == "eraser",
            )
            stroke.add_point(x, y)
            self.drawing.current_stroke = stroke
            self.drawing.add(stroke)
            self._schedule_region(stroke.bbox or (x, y, x, y))
            if self.mode == "eraser":
                self._update_ring(x, y)
            return True

        if self.mode == "text":
            if self.text_edit is not None:
                return True
            pending_text = self.params.get("text")
            if pending_text is not None:
                item = self.renderer.make_text_item(
                    x,
                    y,
                    str(pending_text),
                    tuple(self.params.get("color", (0.0, 0.0, 0.0, 1.0))),
                    float(self.params.get("size", DEFAULT_TEXT_SIZE)),
                    str(self.params.get("font", DEFAULT_FONT)),
                    bool(self.params.get("bold", False)),
                    bool(self.params.get("italic", False)),
                )
                self.drawing.add(item)
                if item.bbox is not None:
                    self._schedule_region(item.bbox)
                if self.params.get("exit_after_text"):
                    self._end_mode("placed")
            else:
                self._begin_text_entry(x, y)
            return True

        return False

    def _on_button_release(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        if self.drawing.current_stroke is not None:
            self.drawing.current_stroke = None
            return True
        return False

    def _on_motion(self, _widget, event) -> bool:
        if self.mode is None:
            return False
        x, y = float(event.x), float(event.y)

        if self.mode == "eraser":
            self._update_ring(x, y)

        stroke = self.drawing.current_stroke
        if stroke is not None:
            if stroke.points:
                last_x, last_y = stroke.points[-1]
                if math.hypot(x - last_x, y - last_y) < 0.5:
                    return True
            else:
                last_x, last_y = x, y
            stroke.add_point(x, y)
            # Repaint only the area covered by the new segment. Replaying the
            # whole stroke inside the clip keeps translucent strokes even and
            # keeps the dirty region small, so long strokes do not get slower
            # as more points accumulate.
            self._schedule_region(
                self._segment_bbox((last_x, last_y), (x, y), stroke.width)
            )
            return True

        return False

    def _on_leave(self, _widget, _event) -> bool:
        if self.mode == "eraser":
            self._clear_ring()
            if self.overlay is not None:
                self.overlay.queue_draw()
        return False

    def _text_from_key(self, event) -> str:
        Gdk = self.Gdk
        text = event.string
        if not text:
            codepoint = Gdk.keyval_to_unicode(event.keyval)
            if codepoint:
                text = chr(codepoint)
        if not text:
            return ""
        # Drop control characters; Enter/Backspace/Escape are handled above.
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            return ""
        return text

    def _on_key_press(self, _widget, event) -> bool:
        Gdk = self.Gdk
        keyval = event.keyval
        state = event.state

        # Ctrl+Z: undo the last item at any time.
        if (state & Gdk.ModifierType.CONTROL_MASK) and keyval in (
            Gdk.KEY_z,
            Gdk.KEY_Z,
        ):
            self._undo()
            return True

        if self.text_edit is not None:
            if keyval == Gdk.KEY_Escape:
                self._cancel_text_entry()
                self._end_mode("escape")
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if state & Gdk.ModifierType.SHIFT_MASK:
                    self._insert_text("\n")
                else:
                    self._commit_text_entry()
                return True
            if keyval == Gdk.KEY_BackSpace:
                self._delete_text_char()
                return True
            # Do not feed modifier shortcuts into the text buffer.
            if state & (
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.MOD1_MASK
                | Gdk.ModifierType.SUPER_MASK
            ):
                return True
            text = self._text_from_key(event)
            if text:
                self._insert_text(text)
            return True

        if (
            self.mode in ("pen", "eraser")
            and self.params.get("exit_on_any_key", True)
            and not self._is_modifier_key(keyval)
        ):
            forward = bool(state & self.system_modifier_mask)
            self._end_mode("key")
            if forward:
                self._forward_key(event.hardware_keycode, state)
            return True

        if keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._end_mode("key")
            return True
        return False

    def _forward_key(self, keycode: int, state: int) -> None:
        """Replay an exit shortcut to the focused window.

        The keyboard grab is needed to notice the exit key, but it also hides
        window manager shortcuts. Once the grab is released, synthesise the
        key press again so the shortcut reaches the focused application or the
        window manager instead of being swallowed by the exit.

        The synthetic events mirror what is still physically held: a key that
        is down is released and pressed again so the window manager sees a
        fresh shortcut instead of auto-repeat, and the complete combination is
        released afterwards. Nothing can remain stuck if a physical release
        races with the forwarded press.
        """
        if not keycode:
            return
        connection = self._xtest_connection()
        if connection is None:
            return
        x11, xtst, display = connection
        try:
            # The daemon's ungrab must reach the server before the fake event,
            # otherwise the grab would take the forwarded key as well.
            self.display.sync()
            modifiers = self._modifier_keys(display, state)
            current = self._pressed_modifiers(display)
            missing = [
                (mask, mod_keycode)
                for mask, mod_keycode in modifiers
                if not current & mask
            ]
            key_down = self._key_is_down(display, keycode)

            for _mask, mod_keycode in missing:
                xtst.XTestFakeKeyEvent(display, mod_keycode, True, 0)
            if key_down:
                # A press would look like auto-repeat while the physical key is
                # still held. Release first, then press again so the window
                # manager sees a fresh shortcut press.
                xtst.XTestFakeKeyEvent(display, keycode, False, 0)
            xtst.XTestFakeKeyEvent(display, keycode, True, 0)
            xtst.XTestFakeKeyEvent(display, keycode, False, 0)
            for _mask, mod_keycode in reversed(missing):
                xtst.XTestFakeKeyEvent(display, mod_keycode, False, 0)
            x11.XFlush(display)
        except Exception:
            if not self._xtest_failed:
                self._xtest_failed = True
                self._log(
                    "warning: could not forward the exit key\n"
                    + traceback.format_exc()
                )

    def _modifier_keys(self, display, state: int) -> list[tuple[int, int]]:
        """Map the pressed GDK modifiers to X masks and keycodes."""
        Gdk = self.Gdk
        wanted = (
            (
                Gdk.ModifierType.CONTROL_MASK,
                X11_CONTROL_MASK,
                X11_KEY_CONTROL_L,
            ),
            (Gdk.ModifierType.MOD1_MASK, X11_MOD1_MASK, X11_KEY_ALT_L),
            (Gdk.ModifierType.META_MASK, X11_MOD1_MASK, X11_KEY_ALT_L),
            (Gdk.ModifierType.MOD4_MASK, X11_MOD4_MASK, X11_KEY_SUPER_L),
            (Gdk.ModifierType.SUPER_MASK, X11_MOD4_MASK, X11_KEY_SUPER_L),
        )
        result: list[tuple[int, int]] = []
        seen: set[int] = set()
        for gdk_mask, x_mask, keysym in wanted:
            if not state & gdk_mask or x_mask in seen:
                continue
            seen.add(x_mask)
            keycode = self._x11.XKeysymToKeycode(display, keysym)
            if keycode:
                result.append((x_mask, int(keycode)))
        return result

    def _pressed_modifiers(self, display) -> int:
        x11 = self._x11
        root = x11.XDefaultRootWindow(display)
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask = ctypes.c_uint()
        x11.XQueryPointer(
            display,
            root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(win_x),
            ctypes.byref(win_y),
            ctypes.byref(mask),
        )
        return int(mask.value)

    def _key_is_down(self, display, keycode: int) -> bool:
        keys = (ctypes.c_ubyte * 32)()
        self._x11.XQueryKeymap(display, keys)
        return bool(keys[keycode >> 3] & (1 << (keycode & 7)))

    def _xtest_connection(self):
        """Open a separate X connection for XTest key injection.

        A separate connection keeps the GDK connection untouched. Replaying on
        a new connection is safe because the caller syncs the GDK display
        first, so the keyboard grab is already released on the server.
        """
        if self._xtest_display is not None:
            return self._x11, self._xtst, self._xtest_display
        if self._xtest_failed:
            return None
        try:
            x11 = ctypes.CDLL(
                ctypes.util.find_library("X11") or "libX11.so.6"
            )
            xtst = ctypes.CDLL(
                ctypes.util.find_library("Xtst") or "libXtst.so.6"
            )
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XFlush.argtypes = [ctypes.c_void_p]
            x11.XFlush.restype = ctypes.c_int
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XQueryPointer.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint),
            ]
            x11.XQueryPointer.restype = ctypes.c_int
            x11.XQueryKeymap.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            x11.XQueryKeymap.restype = ctypes.c_int
            x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x11.XKeysymToKeycode.restype = ctypes.c_ubyte
            xtst.XTestFakeKeyEvent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            xtst.XTestFakeKeyEvent.restype = ctypes.c_int
            display = x11.XOpenDisplay(None)
            if not display:
                raise RuntimeError("XOpenDisplay failed")
        except Exception:
            self._xtest_failed = True
            self._log(
                "warning: exit shortcuts will not be forwarded; "
                "libXtst is unavailable\n" + traceback.format_exc()
            )
            return None
        self._x11 = x11
        self._xtst = xtst
        self._xtest_display = display
        return x11, xtst, display

    def _is_modifier_key(self, keyval: int) -> bool:
        """Return True for modifier and lock keys.

        Any-key exit must wait for the non-modifier key, otherwise the
        Control_L press of Ctrl+Z would end the mode before the undo shortcut
        can be recognised.
        """
        Gdk = self.Gdk
        return keyval in (
            Gdk.KEY_Shift_L,
            Gdk.KEY_Shift_R,
            Gdk.KEY_Control_L,
            Gdk.KEY_Control_R,
            Gdk.KEY_Alt_L,
            Gdk.KEY_Alt_R,
            Gdk.KEY_Super_L,
            Gdk.KEY_Super_R,
            Gdk.KEY_Meta_L,
            Gdk.KEY_Meta_R,
            Gdk.KEY_Hyper_L,
            Gdk.KEY_Hyper_R,
            Gdk.KEY_Caps_Lock,
            Gdk.KEY_Shift_Lock,
            Gdk.KEY_Num_Lock,
        )

    def _on_window_destroy(self, _widget) -> None:
        if not self.shutting_down:
            self._shutdown("window-destroyed")

    # -- commands ---------------------------------------------------------

    def _reply(self, client: socket.socket, payload: dict[str, Any]) -> None:
        DaemonServer.reply(client, payload)

    def _add_text_immediate(
        self,
        x: float,
        y: float,
        text: str,
        color: tuple[float, float, float, float],
        size: float,
        font: str,
        bold: bool,
        italic: bool,
    ) -> None:
        item = self.renderer.make_text_item(
            x, y, text, color, size, font, bold, italic
        )
        self.drawing.add(item)
        if item.bbox is not None:
            # Immediate placement: paint synchronously so the caller can rely
            # on the text being in the backing surface when the command exits.
            self._repaint_region(item.bbox)
        self._ensure_visible()

    def _undo(self) -> None:
        if self.drawing.undo():
            self._full_repaint()

    def _clear(self) -> None:
        self.drawing.clear()
        self.text_edit = None
        if self.caret_timer is not None:
            try:
                self.GLib.source_remove(self.caret_timer)
            except Exception:
                pass
            self.caret_timer = None
        self._full_repaint()

    def _text_params(self, cmd: dict[str, Any]) -> dict[str, Any]:
        return {
            "color": tuple(
                float(value) for value in cmd.get("color", [1, 0, 0, 1])
            ),
            "size": float(cmd.get("size", DEFAULT_TEXT_SIZE)),
            "font": str(cmd.get("font", DEFAULT_FONT)),
            "bold": bool(cmd.get("bold", False)),
            "italic": bool(cmd.get("italic", False)),
            "text": cmd.get("text"),
            "at": cmd.get("at"),
            "exit_after_text": bool(cmd.get("exit_after_text", False)),
        }

    def _on_command(self, cmd: dict[str, Any], client: socket.socket) -> bool:
        try:
            action = cmd.get("cmd")

            if action in ("pen", "eraser"):
                params = {
                    "color": tuple(
                        float(value) for value in cmd.get("color", [1, 0, 0, 1])
                    ),
                    "size": float(cmd.get("size", DEFAULT_PEN_SIZE)),
                    "exit_on_any_key": bool(
                        cmd.get("exit_on_any_key", True)
                    ),
                }
                self._set_mode(action, params, client)

            elif action == "text":
                params = self._text_params(cmd)
                if params["text"] is not None and params["at"] is not None:
                    if self.mode is not None:
                        self._end_mode("replaced", reply=True)
                    self._add_text_immediate(
                        float(params["at"][0]),
                        float(params["at"][1]),
                        str(params["text"]),
                        params["color"],
                        params["size"],
                        params["font"],
                        params["bold"],
                        params["italic"],
                    )
                    self._reply(
                        client, {"ok": True, "event": "done", "reason": "placed"}
                    )
                else:
                    self._set_mode("text", params, client)

            elif action == "clear":
                if self.mode is not None:
                    self._end_mode("clear", reply=True)
                self._clear()
                self._reply(client, {"ok": True})

            elif action == "undo":
                if self.mode is not None:
                    self._end_mode("undo", reply=True)
                self._undo()
                self._reply(client, {"ok": True})

            elif action == "hide":
                self._hide_overlay()
                self._reply(client, {"ok": True})

            elif action == "show":
                self._show_overlay()
                self._reply(client, {"ok": True})

            elif action == "toggle":
                if self.visible:
                    self._hide_overlay()
                else:
                    self._show_overlay()
                self._reply(client, {"ok": True, "visible": self.visible})

            elif action == "status":
                self._reply(
                    client,
                    {
                        "ok": True,
                        "running": True,
                        "visible": self.visible,
                        "effective_visible": self.visible
                        and not self.workspace_hidden,
                        "workspace_hidden": self.workspace_hidden,
                        "clickthrough": self.mode is None,
                        "desktop": self.desktop,
                        "current_desktop": self.current_desktop,
                        "mode": self.mode,
                        "items": len(self.drawing),
                        "screen": [self.width, self.height],
                        "pid": os.getpid(),
                        "version": VERSION,
                        "socket": self.paths.socket_path(),
                        "log": self.paths.log_path(),
                    },
                )

            elif action == "quit":
                self._reply(client, {"ok": True})
                self._shutdown("quit")

            else:
                self._reply(
                    client,
                    {"ok": False, "error": f"unknown command: {action!r}"},
                )

        except Exception as exc:
            self._log("command failed\n" + traceback.format_exc())
            self._reply(client, {"ok": False, "error": str(exc)})
        return False

    # -- shutdown ---------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        GLib = self.GLib
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
            except Exception:
                pass

    def _on_signal(self) -> bool:
        self._shutdown("signal")
        return False

    def _shutdown(self, reason: str) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        if self.top_timer is not None:
            try:
                self.GLib.source_remove(self.top_timer)
            except Exception:
                pass
            self.top_timer = None
        if self.workspace is not None:
            self.workspace.stop()
        try:
            self._end_mode(reason)
        except Exception:
            self._log(
                "error while closing the active mode\n" + traceback.format_exc()
            )
        self.server.stop()
        if self.overlay is not None:
            self.overlay.destroy()
        self._log(f"daemon stopped ({reason})")
        try:
            if self.loop is not None:
                self.loop.quit()
        except Exception:
            pass

    def _cleanup_files(self) -> None:
        server = getattr(self, "server", None)
        if server is not None:
            server.cleanup()
        if self._xtest_display is not None and self._x11 is not None:
            try:
                self._x11.XCloseDisplay(self._xtest_display)
            except Exception:
                pass
            self._xtest_display = None

    # -- main loop --------------------------------------------------------

    def _on_screen_size_changed(self, _screen) -> None:
        width = int(self.screen.get_width())
        height = int(self.screen.get_height())
        if width == self.width and height == self.height:
            return
        if self.mode is not None:
            self._end_mode("screen-resized")
        self.renderer.resize(width, height)
        self.width, self.height = width, height
        if self.overlay is not None:
            self.overlay.resize(width, height)
            self.overlay.raise_()
            self.overlay.queue_draw()

    def run(self) -> int:
        self.loop = self.GLib.MainLoop()
        try:
            self.loop.run()
        finally:
            self._cleanup_files()
        return 0
