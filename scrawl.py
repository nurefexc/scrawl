#!/usr/bin/env python3
# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""
scrawl - command-line screen annotation overlay for X11.

The daemon owns a full-screen GTK3 override-redirect window. It is input
transparent unless a pen, eraser or text mode is active. The `scrawl` command
is a thin client that talks to the daemon over a Unix socket.

Author: nurefexc
License: MIT
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

__author__ = "nurefexc"
__copyright__ = "Copyright (c) 2026 nurefexc"
__license__ = "MIT"

# GTK 3.24 deprecates a few APIs that are still the cleanest way to build a
# full-screen ARGB overlay. Keep the daemon log readable.
warnings.filterwarnings("ignore", category=DeprecationWarning)

APP_NAME = "scrawl"
VERSION = "1.2.1"

DEFAULT_PEN_COLOR = "#ff3b30"
DEFAULT_PEN_SIZE = 4.0
DEFAULT_ERASER_SIZE = 30.0
DEFAULT_TEXT_COLOR = "#ff3b30"
DEFAULT_TEXT_SIZE = 32.0
DEFAULT_FONT = "sans"

MAX_COMMAND_BYTES = 1024 * 1024
SOCKET_START_TIMEOUT = 4.0

# Built-in aliases and starting values. These are intentionally kept in the
# program rather than in a config file: the user files only describe which
# aliases are available, while the current selection lives in settings.json.
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


# ---------------------------------------------------------------------------
# Runtime paths
# ---------------------------------------------------------------------------


def _runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        return os.path.join(base, APP_NAME)
    return os.path.join("/tmp", f"{APP_NAME}-{os.getuid()}")


def socket_path() -> str:
    display = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.environ.get("DISPLAY", "nodisplay"))
    return os.path.join(_runtime_dir(), f"daemon{display}.sock")


def lock_path() -> str:
    return socket_path() + ".lock"


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, APP_NAME)


def log_path() -> str:
    return os.path.join(state_dir(), "daemon.log")


def _mkdir_private(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _rotate_log_if_needed(path: str) -> None:
    try:
        if os.path.getsize(path) > 1024 * 1024:
            os.replace(path, path + ".old")
    except OSError:
        pass


def _log_tail(path: str, lines: int = 25) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# CLI <-> daemon transport
# ---------------------------------------------------------------------------


def _can_connect(timeout: float = 0.2) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path())
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _exchange(command: dict[str, Any], start_daemon: bool = True) -> dict[str, Any]:
    """Send one command to the daemon and wait for its final reply."""
    if not _can_connect():
        if not start_daemon:
            raise RuntimeError("the scrawl daemon is not running")
        _spawn_daemon()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(None)
    try:
        sock.connect(socket_path())
        payload = json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n"
        sock.sendall(payload)
        reader = sock.makefile("rb")
        line = reader.readline(MAX_COMMAND_BYTES + 1)
        if not line:
            raise RuntimeError("the daemon closed the connection")
        if not line.endswith(b"\n"):
            raise RuntimeError("invalid reply from the daemon")
        response = json.loads(line.decode("utf-8"))
        if not isinstance(response, dict):
            raise RuntimeError("invalid reply from the daemon")
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("error", "unknown daemon error")))
        return response
    finally:
        sock.close()


def _spawn_daemon() -> None:
    """Start the daemon in the background and wait until its socket is ready."""
    _mkdir_private(_runtime_dir())
    _mkdir_private(state_dir())
    log = log_path()
    _rotate_log_if_needed(log)

    script = os.path.realpath(__file__)
    with open(log, "ab") as logfile:
        logfile.write(
            f"\n--- {APP_NAME} {VERSION} starting {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"DISPLAY={os.environ.get('DISPLAY', '-')} ---\n".encode("utf-8")
        )
        logfile.flush()
        subprocess.Popen(
            [sys.executable, script, "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=logfile,
            start_new_session=True,
            close_fds=True,
            cwd=os.path.dirname(script) or None,
        )

    deadline = time.monotonic() + SOCKET_START_TIMEOUT
    while time.monotonic() < deadline:
        if _can_connect(timeout=0.2):
            return
        time.sleep(0.05)

    tail = _log_tail(log)
    message = "failed to start the scrawl daemon"
    if tail:
        message += "\n\nLast lines of the daemon log:\n" + tail
    message += f"\nLog file: {log}"
    raise RuntimeError(message)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_color(text: str) -> tuple[float, float, float, float]:
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
    return (float(rgba.red), float(rgba.green), float(rgba.blue), float(rgba.alpha))


def _parse_point(text: str) -> tuple[float, float]:
    parts = [part for part in re.split(r"[\s,;]+", text.strip()) if part]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"invalid coordinate: {text!r} (expected X,Y)"
        )
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid coordinate: {text!r} (expected X,Y)"
        )


def _parse_size(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("size must be a positive number")
    return value


def _color_to_list(color: tuple[float, float, float, float]) -> list[float]:
    return [float(channel) for channel in color]


def _rgba_to_hex(color: tuple[float, float, float, float]) -> str:
    red, green, blue, alpha = (max(0.0, min(1.0, float(value))) for value in color)
    r = int(round(red * 255))
    g = int(round(green * 255))
    b = int(round(blue * 255))
    if alpha < 0.999:
        return f"#{r:02x}{g:02x}{b:02x}{int(round(alpha * 255)):02x}"
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Configuration and current tool settings
# ---------------------------------------------------------------------------


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def config_path(section: str) -> str:
    try:
        filename = CONFIG_FILENAMES[section]
    except KeyError:
        raise ValueError(f"unknown config section: {section!r}")
    return os.path.join(config_dir(), filename)


def legacy_config_path() -> str:
    return os.path.join(config_dir(), LEGACY_CONFIG_FILENAME)


def settings_path() -> str:
    return os.path.join(state_dir(), "settings.json")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _write_json(path: str, value: Any) -> None:
    _mkdir_private(os.path.dirname(path))
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json_object(path: str, description: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"could not read {description} {path}: {exc}")
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid {description} {path}: the root must be an object")
    return data


def _merge_config_section(
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


def load_config() -> dict[str, Any]:
    """Load split config files over the built-in defaults.

    Only colours, sizes and groups are read from disk. Starting values are
    built into the program, and the current selection lives in settings.json.
    """
    config = _json_copy(DEFAULT_CONFIG)

    legacy = legacy_config_path()
    if os.path.exists(legacy):
        data = _read_json_object(legacy, "config")
        for section in CONFIG_SECTIONS:
            value = data.get(section)
            if value is not None:
                if not isinstance(value, dict):
                    raise RuntimeError(
                        f"invalid config {legacy}: {section!r} must be an object"
                    )
                _merge_config_section(config, section, value, legacy)

    for section in CONFIG_SECTIONS:
        path = config_path(section)
        if os.path.exists(path):
            data = _read_json_object(path, f"{section} config")
            _merge_config_section(config, section, data, path)

    return config


def init_config() -> list[str]:
    """Create the split config files, migrating a legacy config.json if needed."""
    messages: list[str] = []
    legacy = legacy_config_path()

    if os.path.exists(legacy):
        effective = load_config()
        for section in CONFIG_SECTIONS:
            path = config_path(section)
            _write_json(path, effective[section])
            messages.append(f"wrote {path}")

        backup = f"{legacy}.legacy"
        if os.path.exists(backup):
            backup = f"{backup}.{int(time.time())}"
        os.replace(legacy, backup)
        messages.append(f"moved {legacy} to {backup}")
        return messages

    for section in CONFIG_SECTIONS:
        path = config_path(section)
        if os.path.exists(path):
            messages.append(f"kept {path}")
        else:
            _write_json(path, DEFAULT_CONFIG[section])
            messages.append(f"wrote {path}")
    return messages


def _default_settings() -> dict[str, dict[str, Any]]:
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


def load_settings(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    settings = _default_settings()
    path = settings_path()
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
            for key in settings[tool]:
                if key in stored and isinstance(stored[key], (str, int, float, bool)):
                    settings[tool][key] = stored[key]
    return settings


def save_settings(settings: dict[str, dict[str, Any]]) -> None:
    _write_json(settings_path(), settings)


def _resolve_color(
    value: Any, config: dict[str, Any]
) -> tuple[tuple[float, float, float, float], str]:
    text = str(value).strip()
    colors = config.get("colors", {})
    if text in colors:
        return _parse_color(str(colors[text])), text
    return _parse_color(text), text


def _resolve_size(value: Any, config: dict[str, Any]) -> tuple[float, str]:
    text = str(value).strip()
    sizes = config.get("sizes", {})
    if text in sizes:
        return _parse_size(str(sizes[text])), text
    return _parse_size(text), text


def _concrete_tool(
    tool: str, config: dict[str, Any], settings: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    values = settings.get(tool, {})
    size_default = {"pen": "thin", "eraser": "eraser", "text": "text"}[tool]
    size, size_name = _resolve_size(values.get("size", size_default), config)

    if tool == "eraser":
        return {
            "size": size,
            "size_name": size_name,
            "size_px": size,
        }

    color, color_name = _resolve_color(values.get("color", "red"), config)
    result: dict[str, Any] = {
        "color": color,
        "color_name": color_name,
        "color_hex": _rgba_to_hex(color),
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


def _query_current_desktop() -> Optional[int]:
    """Return the EWMH current desktop, or None when it cannot be queried."""
    commands = (
        (["xdotool", "get_desktop"], "plain"),
        (["xprop", "-root", "-notype", "_NET_CURRENT_DESKTOP"], "xprop"),
    )
    for command, mode in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=0.4
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        output = result.stdout.strip()
        if mode == "xprop":
            match = re.search(r"=\s*(\d+)", output)
            if match:
                return int(match.group(1))
        else:
            try:
                return int(output)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Drawing model
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class ScrawlDaemon:
    def __init__(self) -> None:
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

        self.screen = Gdk.Screen.get_default()
        self.width = int(self.screen.get_width())
        self.height = int(self.screen.get_height())
        if self.width <= 0 or self.height <= 0:
            raise RuntimeError("invalid screen size")

        self.items: list[Stroke | TextItem] = []
        self.mode: Optional[str] = None
        self.params: dict[str, Any] = {}
        self.client: Optional[socket.socket] = None
        self.client_watch: Optional[int] = None
        self.current_stroke: Optional[Stroke] = None
        self.eraser_pos: Optional[tuple[float, float]] = None
        self.ring_bbox: Optional[tuple[float, float, float, float]] = None
        self.text_edit: Optional[dict[str, Any]] = None
        self.caret_visible = True
        self.caret_timer: Optional[int] = None
        self.grabbed = False
        self.prev_focus = None
        self.visible = True
        self.shutting_down = False
        self.running = True
        self.server: Optional[socket.socket] = None
        self.server_thread: Optional[threading.Thread] = None
        self.lock_fd: Optional[int] = None
        self.top_timer: Optional[int] = None
        self.desktop_timer: Optional[int] = None
        self.desktop_proc: Optional[subprocess.Popen[str]] = None
        self.desktop_watch: Optional[int] = None
        self.desktop: Optional[int] = _query_current_desktop()
        self.current_desktop: Optional[int] = self.desktop
        self.workspace_hidden = False

        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.width, self.height)
        self.cr = cairo.Context(self.surface)
        self._clear_surface()

        # Claim the lock before creating the overlay, so a second daemon exits
        # without flashing a full-screen window.
        self._start_server()
        self._build_window()
        # An override-redirect window can be covered by windows mapped later.
        # Re-raising on a slow timer keeps annotations on top without ever
        # taking input focus.
        self.top_timer = self.GLib.timeout_add_seconds(2, self._keep_on_top)
        # Override-redirect windows are not managed by the window manager, so
        # they are visible on every xmonad workspace. Watch _NET_CURRENT_DESKTOP
        # and unmap the overlay when the user switches away.
        self._start_desktop_watch()
        self._install_signal_handlers()
        atexit.register(self._cleanup_files)
        self.screen.connect("size-changed", self._on_screen_size_changed)
        self._log(
            f"daemon started (pid={os.getpid()}, screen={self.width}x{self.height}, "
            f"desktop={self.desktop}, socket={socket_path()})"
        )

    # -- logging ----------------------------------------------------------

    def _log(self, message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)

    # -- window -----------------------------------------------------------

    def _build_window(self) -> None:
        Gtk = self.Gtk
        Gdk = self.Gdk

        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("scrawl-overlay")
        win.set_role("scrawl-overlay")
        win.set_wmclass("scrawl", "Scrawl")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        win.set_accept_focus(False)
        win.set_can_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        win.set_app_paintable(True)

        visual = self.screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)

        win.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
        )
        win.connect("draw", self._on_draw)
        win.connect("key-press-event", self._on_key_press)
        win.connect("button-press-event", self._on_button_press)
        win.connect("button-release-event", self._on_button_release)
        win.connect("motion-notify-event", self._on_motion)
        win.connect("leave-notify-event", self._on_leave)
        win.connect("destroy", self._on_window_destroy)

        win.set_default_size(self.width, self.height)
        win.move(0, 0)
        win.realize()

        gdkwin = win.get_window()
        self.win = win
        self.gdkwin = gdkwin

        if gdkwin is not None:
            # Bypass the window manager: never tiled, never decorated, always
            # above normal windows.
            gdkwin.set_override_redirect(True)
            gdkwin.set_keep_above(True)

        # Install the empty input shape before mapping, so the overlay is
        # click-through from its very first frame.
        self._set_clickthrough(True)
        win.show()
        if gdkwin is not None:
            gdkwin.raise_()

    def _set_clickthrough(self, enabled: bool) -> None:
        if self.gdkwin is None:
            return
        try:
            if enabled:
                region = self.cairo.Region()
            else:
                region = self.cairo.Region(
                    self.cairo.RectangleInt(0, 0, self.width, self.height)
                )
            self.gdkwin.input_shape_combine_region(region, 0, 0)
        except Exception:
            self._log(
                "warning: could not update the X11 input shape\n"
                + traceback.format_exc()
            )

    def _adopt_current_desktop(self) -> None:
        """Bind the overlay to the workspace the user is looking at now."""
        desktop = self.current_desktop
        if desktop is None:
            desktop = _query_current_desktop()
        if desktop is None:
            return
        self.desktop = desktop
        self.current_desktop = desktop

    def _start_desktop_watch(self) -> None:
        """Watch _NET_CURRENT_DESKTOP without blocking the GTK main loop.

        `xprop -spy` prints the current value once and then again on every
        change, which is cheaper and more responsive than spawning a subprocess
        from a timeout. The timeout is only used when xprop is unavailable.
        """
        try:
            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_CURRENT_DESKTOP"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError:
            proc = None

        if proc is not None and proc.stdout is not None:
            self.desktop_proc = proc
            try:
                self.desktop_watch = self.GLib.io_add_watch(
                    proc.stdout.fileno(),
                    self.GLib.PRIORITY_DEFAULT,
                    self.GLib.IOCondition.IN | self.GLib.IOCondition.HUP,
                    self._on_desktop_output,
                )
                return
            except Exception:
                self._stop_desktop_watch()

        self.desktop_timer = self.GLib.timeout_add(600, self._check_desktop)

    def _stop_desktop_watch(self) -> None:
        if self.desktop_watch is not None:
            try:
                self.GLib.source_remove(self.desktop_watch)
            except Exception:
                pass
            self.desktop_watch = None
        if self.desktop_proc is not None:
            try:
                self.desktop_proc.terminate()
                self.desktop_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.desktop_proc.kill()
                except Exception:
                    pass
            self.desktop_proc = None

    def _on_desktop_output(self, _fd: int, _condition) -> bool:
        proc = self.desktop_proc
        if proc is None or proc.stdout is None:
            self.desktop_watch = None
            return False
        line = proc.stdout.readline()
        if not line:
            # xprop exited; the source is removed by returning False. Fall
            # back to polling so workspace tracking keeps working.
            self.desktop_watch = None
            if not self.shutting_down and self.desktop_timer is None:
                self.desktop_timer = self.GLib.timeout_add(600, self._check_desktop)
            return False
        match = re.search(r"=\s*(\d+)", line)
        if match:
            self._apply_desktop(int(match.group(1)))
        return True

    def _check_desktop(self) -> bool:
        if self.shutting_down:
            return False
        current = _query_current_desktop()
        if current is None:
            return True
        self._apply_desktop(current)
        return True

    def _apply_desktop(self, current: int) -> None:
        if self.shutting_down:
            return
        self.current_desktop = current

        if self.desktop is None:
            self.desktop = current
            return

        if current != self.desktop:
            if not self.workspace_hidden:
                if self.mode is not None:
                    self._end_mode("workspace-changed")
                if self.visible and self.gdkwin is not None:
                    self.win.hide()
                self.workspace_hidden = True
                self._log(
                    f"workspace {self.desktop} left (now {current}); overlay hidden"
                )
        elif self.workspace_hidden:
            self.workspace_hidden = False
            if self.visible and self.gdkwin is not None:
                self.win.show()
                self._set_clickthrough(self.mode is None)
                self.win.queue_draw()
                try:
                    self.gdkwin.raise_()
                except Exception:
                    pass
            self._log(f"workspace {self.desktop} active; overlay shown")

    def _ensure_visible(self) -> None:
        self._adopt_current_desktop()
        if not self.visible:
            self.visible = True
            self.workspace_hidden = False
            self.win.show()
            self._set_clickthrough(self.mode is None)
        elif self.workspace_hidden:
            self.workspace_hidden = False
            self.win.show()
            self._set_clickthrough(self.mode is None)
        if self.gdkwin is not None:
            self.gdkwin.raise_()

    def _keep_on_top(self) -> bool:
        if self.shutting_down:
            return False
        # An interactive mode has just raised the window and holds the pointer
        # grab, so re-raising it during drawing would only disturb frames.
        if (
            self.mode is None
            and self.visible
            and not self.workspace_hidden
            and self.gdkwin is not None
        ):
            try:
                self.gdkwin.raise_()
            except Exception:
                return False
        return True

    def _hide_overlay(self) -> None:
        self._end_mode("hidden")
        self.visible = False
        self.win.hide()

    def _show_overlay(self) -> None:
        self._ensure_visible()
        self.win.queue_draw()

    # -- entering/leaving interactive modes -------------------------------

    def _cursor(self, cursor_type: str):
        Gdk = self.Gdk
        ctype = getattr(Gdk.CursorType, cursor_type)
        try:
            return Gdk.Cursor.new_for_display(self.display, ctype)
        except Exception:
            return Gdk.Cursor.new_from_name(self.display, "default")

    def _enter_interactive(self, cursor_type: str) -> None:
        Gdk = self.Gdk
        self._ensure_visible()
        self.win.set_accept_focus(True)
        self.win.set_can_focus(True)
        self._set_clickthrough(False)
        try:
            self.display.flush()
        except Exception:
            pass

        cursor = self._cursor(cursor_type)
        if self.gdkwin is not None:
            self.gdkwin.set_cursor(cursor)
            self.gdkwin.raise_()

        self.grabbed = False
        self.prev_focus = None
        try:
            status = self.display.get_default_seat().grab(
                self.gdkwin,
                Gdk.SeatCapabilities.POINTER | Gdk.SeatCapabilities.KEYBOARD,
                True,
                cursor,
                None,
                None,
                None,
            )
            self.grabbed = status == Gdk.GrabStatus.SUCCESS
        except Exception:
            self._log(
                "warning: could not grab pointer/keyboard\n" + traceback.format_exc()
            )

        if not self.grabbed:
            # Fallback for environments where the grab is refused: at least
            # take keyboard focus so Esc still exits the mode.
            try:
                self.prev_focus = self.screen.get_active_window()
                if self.gdkwin is not None:
                    self.gdkwin.focus(Gdk.CURRENT_TIME)
            except Exception:
                self.prev_focus = None
        self.win.queue_draw()

    def _leave_interactive(self) -> None:
        Gdk = self.Gdk
        if self.grabbed:
            try:
                self.display.get_default_seat().ungrab()
            except Exception:
                pass
            self.grabbed = False
        if self.gdkwin is not None:
            try:
                self.gdkwin.set_cursor(None)
            except Exception:
                pass
        self._set_clickthrough(True)
        self.win.set_accept_focus(False)
        self.win.set_can_focus(False)
        if self.prev_focus is not None:
            try:
                if self.prev_focus != self.gdkwin:
                    self.prev_focus.focus(Gdk.CURRENT_TIME)
            except Exception:
                pass
            self.prev_focus = None

    # -- mode lifecycle ---------------------------------------------------

    def _set_mode(self, mode: str, params: dict[str, Any], client: socket.socket) -> None:
        if self.mode is not None:
            self._end_mode("replaced", reply=True)
        self.mode = mode
        self.params = dict(params)
        self.client = client
        self._watch_client(client)
        self.text_edit = None
        self.current_stroke = None
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
        self.current_stroke = None
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
        self.win.queue_draw()

    # -- client liveness watch -------------------------------------------

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

    def _clear_surface(self) -> None:
        cr = self.cr
        cr.save()
        cr.set_operator(self.cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.restore()

    def _font_style(self, item: TextItem):
        cairo = self.cairo
        slant = cairo.FONT_SLANT_ITALIC if item.italic else cairo.FONT_SLANT_NORMAL
        weight = cairo.FONT_WEIGHT_BOLD if item.bold else cairo.FONT_WEIGHT_NORMAL
        return slant, weight

    def _text_layout(self, item: TextItem) -> dict[str, Any]:
        cr = self.cr
        cr.save()
        try:
            slant, weight = self._font_style(item)
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

    def _make_text_item(
        self,
        x: float,
        y: float,
        text: str,
        color: tuple[float, float, float, float],
        size: float,
        font: str,
        bold: bool,
        italic: bool,
    ) -> TextItem:
        item = TextItem(x, y, text, color, size, font, bold, italic)
        item.bbox = self._text_layout(item)["bbox"]
        return item

    def _render_text(
        self, cr, item: TextItem, layout: Optional[dict[str, Any]] = None
    ) -> None:
        if not item.text:
            return
        layout = layout or self._text_layout(item)
        cr.save()
        try:
            slant, weight = self._font_style(item)
            cr.select_font_face(item.font, slant, weight)
            cr.set_font_size(item.size)
            cr.set_source_rgba(*item.color)
            for index, line in enumerate(layout["lines"]):
                baseline = item.y + layout["ascent"] + index * layout["line_height"]
                cr.move_to(item.x, baseline)
                cr.show_text(line)
        finally:
            cr.restore()

    def _render_stroke(self, cr, stroke: Stroke) -> None:
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

    def _render_item(self, cr, item: Stroke | TextItem) -> None:
        if isinstance(item, Stroke):
            self._render_stroke(cr, item)
        else:
            self._render_text(cr, item)

    def _clamp_bbox(
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

    def _queue_region(self, bbox: tuple[float, float, float, float]) -> None:
        clamped = self._clamp_bbox(bbox)
        if clamped is None:
            return
        x0, y0, x1, y1 = clamped
        self.win.queue_draw_area(
            int(math.floor(x0)),
            int(math.floor(y0)),
            max(1, int(math.ceil(x1 - x0))),
            max(1, int(math.ceil(y1 - y0))),
        )

    def _repaint_region(self, bbox: tuple[float, float, float, float]) -> None:
        """Clear a region of the backing surface and replay all items into it.

        Replaying a whole item inside the clip region (instead of incrementally
        adding segments) keeps translucent strokes uniform and makes eraser
        strokes behave like real erasers. It also keeps undo simple: the model
        is an ordered list of pen/eraser/text items.
        """
        clamped = self._clamp_bbox(bbox)
        if clamped is None:
            return
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
            for item in self.items:
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
                self._render_item(cr, item)
        finally:
            cr.restore()
        self._queue_region(clamped)

    def _schedule_region(self, bbox: tuple[float, float, float, float]) -> None:
        """Paint a dirty region immediately.

        Repainting here instead of from an idle callback keeps the stroke
        under the cursor even when pointer events arrive faster than the main
        loop can drain them. Cairo and GTK merge the queued draw areas, so a
        burst of motion events still composites at the display refresh rate.
        """
        self._repaint_region(bbox)

    def _full_repaint(self) -> None:
        self._clear_surface()
        for item in self.items:
            self._render_item(self.cr, item)
        self.win.queue_draw()

    def _segment_bbox(
        self, first: tuple[float, float], second: tuple[float, float], width: float
    ) -> tuple[float, float, float, float]:
        radius = width / 2.0 + 2.0
        return (
            min(first[0], second[0]) - radius,
            min(first[1], second[1]) - radius,
            max(first[0], second[0]) + radius,
            max(first[1], second[1]) + radius,
        )

    # -- painting ---------------------------------------------------------

    def _on_draw(self, _widget, cr) -> bool:
        cairo = self.cairo
        cr.save()
        try:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_source_surface(self.surface, 0, 0)
            cr.paint()

            if self.text_edit is not None:
                self._draw_text_edit(cr)
            if self.mode == "eraser" and self.eraser_pos is not None:
                self._draw_eraser_ring(cr)
        finally:
            cr.restore()
        return False

    def _draw_eraser_ring(self, cr) -> None:
        cairo = self.cairo
        x, y = self.eraser_pos
        radius = max(1.0, float(self.params.get("size", DEFAULT_ERASER_SIZE)) / 2.0)
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

    def _ring_bbox_for(self, x: float, y: float) -> tuple[float, float, float, float]:
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
        return self._make_text_item(
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
        layout = self._text_layout(item)
        x0, y0, x1, y1 = layout["bbox"]
        return (x0 - 4, y0 - 4, x1 + 12, y1 + 6)

    def _draw_text_edit(self, cr) -> None:
        if self.text_edit is None:
            return
        item = self._text_edit_item(self.text_edit["buffer"])
        layout = self._text_layout(item)
        if item.text:
            self._render_text(cr, item, layout)

        if self.caret_visible:
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
        self.items.append(item)
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
            self.current_stroke = stroke
            self.items.append(stroke)
            self._schedule_region(stroke.bbox or (x, y, x, y))
            if self.mode == "eraser":
                self._update_ring(x, y)
            return True

        if self.mode == "text":
            if self.text_edit is not None:
                return True
            pending_text = self.params.get("text")
            if pending_text is not None:
                item = self._make_text_item(
                    x,
                    y,
                    str(pending_text),
                    tuple(self.params.get("color", (0.0, 0.0, 0.0, 1.0))),
                    float(self.params.get("size", DEFAULT_TEXT_SIZE)),
                    str(self.params.get("font", DEFAULT_FONT)),
                    bool(self.params.get("bold", False)),
                    bool(self.params.get("italic", False)),
                )
                self.items.append(item)
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
        if self.current_stroke is not None:
            self.current_stroke = None
            return True
        return False

    def _on_motion(self, _widget, event) -> bool:
        if self.mode is None:
            return False
        x, y = float(event.x), float(event.y)

        if self.mode == "eraser":
            self._update_ring(x, y)

        stroke = self.current_stroke
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
            self.win.queue_draw()
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

        if keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._end_mode("key")
            return True
        return False

    def _on_window_destroy(self, _widget) -> None:
        if not self.shutting_down:
            self._shutdown("window-destroyed")

    # -- commands ---------------------------------------------------------

    def _reply(self, client: socket.socket, payload: dict[str, Any]) -> None:
        try:
            client.sendall(
                json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

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
        item = self._make_text_item(x, y, text, color, size, font, bold, italic)
        self.items.append(item)
        if item.bbox is not None:
            # Immediate placement: paint synchronously so the caller can rely
            # on the text being in the backing surface when the command exits.
            self._repaint_region(item.bbox)
        self._ensure_visible()

    def _undo(self) -> None:
        if not self.items:
            return
        # If a stroke is currently under the mouse, do not let later motion
        # events keep mutating an item that is no longer in the model.
        self.current_stroke = None
        self.items.pop()
        self._full_repaint()

    def _clear(self) -> None:
        self.items.clear()
        self.current_stroke = None
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
            "color": tuple(float(value) for value in cmd.get("color", [1, 0, 0, 1])),
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
                        "items": len(self.items),
                        "screen": [self.width, self.height],
                        "pid": os.getpid(),
                        "version": VERSION,
                        "socket": socket_path(),
                        "log": log_path(),
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

    # -- socket server ----------------------------------------------------

    def _start_server(self) -> None:
        _mkdir_private(_runtime_dir())
        path = socket_path()

        self.lock_fd = os.open(lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            self._log("another scrawl daemon is already running on this display")
            raise SystemExit(0)

        try:
            os.unlink(path)
        except OSError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(8)
        except Exception:
            server.close()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            raise

        self.server = server
        self.server_thread = threading.Thread(
            target=self._accept_loop, name="scrawl-socket", daemon=True
        )
        self.server_thread.start()

        try:
            with open(path + ".pid", "w", encoding="ascii") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while self.running:
            try:
                if self.server is None:
                    break
                client, _addr = self.server.accept()
            except OSError:
                break
            try:
                client.settimeout(5.0)
                chunks = bytearray()
                while b"\n" not in chunks:
                    chunk = client.recv(65536)
                    if not chunk:
                        raise ValueError("empty request")
                    chunks.extend(chunk)
                    if len(chunks) > MAX_COMMAND_BYTES:
                        raise ValueError("request too large")
                line = bytes(chunks).split(b"\n", 1)[0]
                cmd = json.loads(line.decode("utf-8"))
                if not isinstance(cmd, dict):
                    raise ValueError("request is not a JSON object")
                client.settimeout(None)
            except Exception as exc:
                try:
                    self._reply(client, {"ok": False, "error": str(exc)})
                except Exception:
                    pass
                continue
            self.GLib.idle_add(self._on_command, cmd, client)

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
        self.running = False
        if self.top_timer is not None:
            try:
                self.GLib.source_remove(self.top_timer)
            except Exception:
                pass
            self.top_timer = None
        if self.desktop_timer is not None:
            try:
                self.GLib.source_remove(self.desktop_timer)
            except Exception:
                pass
            self.desktop_timer = None
        self._stop_desktop_watch()
        try:
            self._end_mode(reason)
        except Exception:
            self._log("error while closing the active mode\n" + traceback.format_exc())
        if self.server is not None:
            try:
                self.server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        self._cleanup_files()
        try:
            if self.gdkwin is not None:
                self.win.destroy()
        except Exception:
            pass
        self._log(f"daemon stopped ({reason})")
        try:
            self.loop.quit()
        except Exception:
            pass

    def _cleanup_files(self) -> None:
        for path in (socket_path(), socket_path() + ".pid"):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None

    # -- main loop --------------------------------------------------------

    def _on_screen_size_changed(self, _screen) -> None:
        width = int(self.screen.get_width())
        height = int(self.screen.get_height())
        if width == self.width and height == self.height:
            return
        if self.mode is not None:
            self._end_mode("screen-resized")
        old_surface = self.surface
        self.width, self.height = width, height
        self.surface = self.cairo.ImageSurface(
            self.cairo.FORMAT_ARGB32, self.width, self.height
        )
        self.cr = self.cairo.Context(self.surface)
        self._clear_surface()
        cr = self.cr
        cr.save()
        try:
            cr.set_source_surface(old_surface, 0, 0)
            cr.paint()
        finally:
            cr.restore()
        self.win.resize(self.width, self.height)
        self.win.move(0, 0)
        if self.gdkwin is not None:
            self.gdkwin.raise_()
        self.win.queue_draw()

    def run(self) -> int:
        self.loop = self.GLib.MainLoop()
        try:
            self.loop.run()
        finally:
            self._cleanup_files()
        return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def _cmd_daemon(_args: argparse.Namespace) -> int:
    try:
        daemon = ScrawlDaemon()
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"{APP_NAME}: daemon failed to start: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    return daemon.run()


def _current_data(
    config: dict[str, Any], settings: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    for tool in ("pen", "eraser", "text"):
        values = _concrete_tool(tool, config, settings)
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


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    settings = load_settings(config)
    current = _current_data(config, settings)

    if not _can_connect(timeout=0.3):
        response: dict[str, Any] = {
            "ok": True,
            "running": False,
            "visible": False,
            "effective_visible": False,
            "workspace_hidden": False,
            "clickthrough": True,
            "desktop": None,
            "current_desktop": None,
            "mode": None,
            "items": 0,
        }
    else:
        try:
            response = _exchange({"cmd": "status"}, start_daemon=False)
        except Exception as exc:
            print(f"{APP_NAME}: {exc}", file=sys.stderr)
            return 1

    response["config_dir"] = config_dir()
    response["config_files"] = {
        section: config_path(section) for section in CONFIG_SECTIONS
    }
    response["settings"] = settings_path()
    response["current"] = current

    if getattr(args, "json", False):
        print(json.dumps(response, ensure_ascii=False))
        return 0

    if not response.get("running"):
        print(f"{APP_NAME}: not running")
    else:
        print(
            f"{APP_NAME}: running (pid {response['pid']}, "
            f"version {response['version']})"
        )
        print(f"screen: {response['screen'][0]}x{response['screen'][1]}")
        workspace = "hidden" if response.get("workspace_hidden") else "visible"
        print(
            f"desktop: {response.get('desktop')} "
            f"(current {response.get('current_desktop')}, {workspace})"
        )
        print(f"visible: {'yes' if response['visible'] else 'no'}")
        print(f"click-through: {'yes' if response.get('clickthrough') else 'no'}")
        print(f"mode: {response['mode'] or 'idle'}")
        print(f"items: {response['items']}")

    pen = current["pen"]
    eraser = current["eraser"]
    text = current["text"]
    print(f"pen: {pen['color']} ({pen['color_hex']}), {pen['size']} ({pen['size_px']:g} px)")
    print(f"eraser: {eraser['size']} ({eraser['size_px']:g} px)")
    print(
        f"text: {text['color']} ({text['color_hex']}), "
        f"{text['size']} ({text['size_px']:g} px), {text['font']}"
    )
    print(f"config dir: {config_dir()}")
    print(f"settings: {settings_path()}")
    if response.get("running"):
        print(f"socket: {response['socket']}")
        print(f"log: {response['log']}")
    return 0


def _cmd_current(args: argparse.Namespace) -> int:
    config = load_config()
    settings = load_settings(config)
    current = _current_data(config, settings)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "config_dir": config_dir(),
                    "config_files": {
                        section: config_path(section) for section in CONFIG_SECTIONS
                    },
                    "settings": settings_path(),
                    **current,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if getattr(args, "tsv", False):
        for tool in ("pen", "eraser", "text"):
            item = current[tool]
            color = item["color"] if item["color"] is not None else "-"
            hex_value = item["color_hex"] if item["color_hex"] is not None else "-"
            print(
                f"{tool}\t{color}\t{hex_value}\t"
                f"{item['size']}\t{item['size_px']:g}"
            )
        return 0

    for tool in ("pen", "eraser", "text"):
        item = current[tool]
        if item["color"] is None:
            print(f"{tool}: {item['size']} ({item['size_px']:g} px)")
        else:
            suffix = ""
            if tool == "text":
                suffix = f", {item['font']}"
                if item.get("bold"):
                    suffix += ", bold"
                if item.get("italic"):
                    suffix += ", italic"
            print(
                f"{tool}: {item['color']} ({item['color_hex']}), "
                f"{item['size']} ({item['size_px']:g} px){suffix}"
            )
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    config = load_config()
    settings = load_settings(config)
    tool, field = SET_FIELDS[args.what]
    value = args.value

    if field == "color":
        _resolve_color(value, config)
    elif field == "size":
        _resolve_size(value, config)
    elif field in ("bold", "italic"):
        normalized = str(value).strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            value = True
        elif normalized in ("0", "false", "no", "off"):
            value = False
        else:
            raise RuntimeError(f"expected a boolean for {args.what}, got {value!r}")
    elif field == "font":
        if not str(value).strip():
            raise RuntimeError("font must not be empty")

    settings[tool][field] = value
    save_settings(settings)
    print(f"{args.what}: {value}")
    return 0


def _cmd_colors(args: argparse.Namespace) -> int:
    config = load_config()
    colors: dict[str, str] = {}
    for alias, value in config.get("colors", {}).items():
        if not isinstance(value, str):
            continue
        try:
            rgba, _ = _resolve_color(value, config)
        except argparse.ArgumentTypeError:
            continue
        colors[str(alias)] = _rgba_to_hex(rgba)

    if getattr(args, "json", False):
        print(json.dumps(colors, ensure_ascii=False))
        return 0

    if getattr(args, "tsv", False):
        for alias, hex_value in colors.items():
            print(f"{alias}\t{hex_value}")
        return 0

    width = max((len(alias) for alias in colors), default=0)
    for alias, hex_value in colors.items():
        print(f"{alias:<{width}}  {hex_value}")
    return 0


def _cmd_sizes(args: argparse.Namespace) -> int:
    config = load_config()
    sizes_config = config.get("sizes", {})
    aliases: list[str]
    if args.group:
        groups = config.get("groups", {})
        group = groups.get(args.group, [])
        aliases = [str(alias) for alias in group if str(alias) in sizes_config]
    else:
        aliases = [str(alias) for alias in sizes_config]

    sizes: dict[str, float] = {}
    for alias in aliases:
        try:
            value, _ = _resolve_size(alias, config)
        except argparse.ArgumentTypeError:
            continue
        sizes[alias] = value

    if getattr(args, "json", False):
        print(json.dumps(sizes, ensure_ascii=False))
        return 0

    if getattr(args, "tsv", False):
        for alias, value in sizes.items():
            print(f"{alias}\t{value:g}")
        return 0

    width = max((len(alias) for alias in sizes), default=0)
    for alias, value in sizes.items():
        print(f"{alias:<{width}}  {value:g} px")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if getattr(args, "init", False):
        for message in init_config():
            print(f"{APP_NAME}: {message}")

    config = load_config()
    if getattr(args, "json", False):
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    defaults = DEFAULT_CONFIG["defaults"]
    pen = defaults["pen"]
    eraser = defaults["eraser"]
    text = defaults["text"]
    print(f"config dir: {config_dir()}")
    for section in CONFIG_SECTIONS:
        path = config_path(section)
        origin = "file" if os.path.exists(path) else "built-in defaults"
        print(f"{section}: {len(config[section])} entries ({origin}: {path})")
    print(f"default pen: {pen.get('color')} / {pen.get('size')} (built in)")
    print(f"default eraser: {eraser.get('size')} (built in)")
    print(f"default text: {text.get('color')} / {text.get('size')} (built in)")
    print(f"current settings: {settings_path()}")
    return 0


def _cmd_clear(_args: argparse.Namespace) -> int:
    if not _can_connect():
        return 0
    _exchange({"cmd": "clear"}, start_daemon=False)
    return 0


def _cmd_undo(_args: argparse.Namespace) -> int:
    if not _can_connect():
        return 0
    _exchange({"cmd": "undo"}, start_daemon=False)
    return 0


def _cmd_visibility(action: str):
    def run(_args: argparse.Namespace) -> int:
        if not _can_connect():
            # Starting a daemon only to immediately hide it would be silly,
            # so a toggle with no daemon means "show a fresh overlay".
            if action in ("show", "toggle"):
                _exchange({"cmd": "show"}, start_daemon=True)
            return 0
        _exchange({"cmd": action}, start_daemon=False)
        return 0

    return run


def _cmd_quit(_args: argparse.Namespace) -> int:
    if not _can_connect():
        return 0
    _exchange({"cmd": "quit"}, start_daemon=False)
    return 0


def _cmd_pen(args: argparse.Namespace, eraser: bool = False) -> int:
    config = load_config()
    settings = load_settings(config)
    tool = "eraser" if eraser else "pen"

    if args.size is not None:
        settings[tool]["size"] = args.size
    if not eraser and args.color is not None:
        settings["pen"]["color"] = args.color

    values = _concrete_tool(tool, config, settings)
    save_settings(settings)

    command: dict[str, Any] = {
        "cmd": tool,
        "size": values["size"],
    }
    if not eraser:
        command["color"] = _color_to_list(values["color"])

    if sys.stderr.isatty():
        if eraser:
            print(
                f"{APP_NAME}: erasing with {values['size_name']} "
                f"({values['size_px']:g} px) - drag with the left mouse button; "
                "exit: Esc / Enter / right-click",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"{APP_NAME}: drawing with {values['color_name']} "
                f"({values['color_hex']}) at {values['size_name']} "
                f"({values['size_px']:g} px) - drag with the left mouse button; "
                "exit: Esc / Enter / right-click",
                file=sys.stderr,
                flush=True,
            )
    _exchange(command)
    return 0


def _cmd_eraser(args: argparse.Namespace) -> int:
    return _cmd_pen(args, eraser=True)


def _cmd_text(args: argparse.Namespace) -> int:
    config = load_config()
    settings = load_settings(config)

    if args.color is not None:
        settings["text"]["color"] = args.color
    if args.size is not None:
        settings["text"]["size"] = args.size
    if args.font is not None:
        settings["text"]["font"] = args.font
    if args.bold:
        settings["text"]["bold"] = True
    if args.italic:
        settings["text"]["italic"] = True

    values = _concrete_tool("text", config, settings)
    save_settings(settings)

    text = " ".join(args.text) if args.text else None
    if text == "":
        text = None
    at = args.at
    command: dict[str, Any] = {
        "cmd": "text",
        "text": text,
        "color": _color_to_list(values["color"]),
        "size": values["size"],
        "font": values["font"],
        "bold": values["bold"],
        "italic": values["italic"],
        "at": list(at) if at is not None else None,
        "exit_after_text": bool(at is not None and text is None),
    }
    if sys.stderr.isatty():
        if text is not None and at is not None:
            pass
        elif text is not None:
            print(
                f"{APP_NAME}: click where the text should go "
                f"({values['color_name']} {values['size_name']}); "
                "exit: Esc / right-click",
                file=sys.stderr,
                flush=True,
            )
        elif at is not None:
            print(
                f"{APP_NAME}: type the text ({values['color_name']} "
                f"{values['size_name']}); Enter = done, Esc = cancel",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"{APP_NAME}: click a position, then type "
                f"({values['color_name']} {values['size_name']}); "
                "Enter = done, Shift+Enter = new line, Esc = exit",
                file=sys.stderr,
                flush=True,
            )
    _exchange(command)
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            "Click-through screen annotation overlay controlled entirely from "
            "the command line. Interactive modes exit with Esc, Enter or a "
            "right-click."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  scrawl pen                       # current colour and size\n"
            "  scrawl pen --color neon --size marker\n"
            "  scrawl pen --color '#00ff9d' --size 12\n"
            "  scrawl set pen-color blue        # switch and remember\n"
            "  scrawl set pen-size normal\n"
            "  scrawl eraser                    # current eraser size\n"
            "  scrawl text --at 100,100 --size text-large 'Hello world'\n"
            "  scrawl current --tsv\n"
            "  scrawl config --init\n"
            "  scrawl clear\n"
            "  scrawl quit\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pen = sub.add_parser(
        "pen",
        aliases=["draw"],
        help="interactive pen mode (drag with the left button)",
        description=(
            "Interactive pen mode. Drag with the left mouse button to draw. "
            "Esc, Enter or right-click exits."
        ),
    )
    pen.add_argument(
        "--color",
        "-c",
        default=None,
        metavar="COLOR",
        help="colour alias from config or any GTK colour (default: current setting)",
    )
    pen.add_argument(
        "--size",
        "-s",
        default=None,
        metavar="PX",
        help="size alias from config or pixels (default: current setting)",
    )
    pen.set_defaults(func=_cmd_pen)

    eraser = sub.add_parser(
        "eraser",
        aliases=["erase"],
        help="interactive eraser mode (drag with the left button)",
        description=(
            "Interactive eraser mode. The ring under the cursor shows the "
            "eraser diameter. Esc, Enter or right-click exits."
        ),
    )
    eraser.add_argument(
        "--size",
        "-s",
        default=None,
        metavar="PX",
        help="size alias from config or pixels (default: current setting)",
    )
    eraser.add_argument(
        "--color",
        "-c",
        default=None,
        help=argparse.SUPPRESS,
    )
    eraser.set_defaults(func=_cmd_eraser)

    text = sub.add_parser(
        "text",
        aliases=["write"],
        help="put text on the screen",
        description=(
            "Place text on the screen. With both TEXT and --at X,Y it is "
            "placed immediately. With only TEXT, click to place it (repeatable). "
            "With no TEXT, click a position and type directly on the overlay."
        ),
    )
    text.add_argument("text", nargs="*", help="text to write (optional)")
    text.add_argument(
        "--at",
        "-a",
        type=_parse_point,
        default=None,
        metavar="X,Y",
        help="place immediately at this screen coordinate",
    )
    text.add_argument(
        "--color",
        "-c",
        default=None,
        metavar="COLOR",
        help="colour alias from config or any GTK colour (default: current setting)",
    )
    text.add_argument(
        "--size",
        "-s",
        default=None,
        metavar="PX",
        help="size alias from config or pixels (default: current setting)",
    )
    text.add_argument(
        "--font",
        "-f",
        default=None,
        metavar="NAME",
        help="font family (default: current setting)",
    )
    text.add_argument("--bold", action="store_true", help="bold")
    text.add_argument("--italic", action="store_true", help="italic")
    text.set_defaults(func=_cmd_text)

    clear = sub.add_parser(
        "clear",
        aliases=["all-clear", "allclear"],
        help="remove every annotation",
        description="Remove all pen strokes, eraser marks and text.",
    )
    clear.set_defaults(func=_cmd_clear)

    undo = sub.add_parser("undo", help="remove the most recent item")
    undo.set_defaults(func=_cmd_undo)

    hide = sub.add_parser("hide", help="hide the overlay (annotations are kept)")
    hide.set_defaults(func=_cmd_visibility("hide"))

    show = sub.add_parser("show", help="show the overlay")
    show.set_defaults(func=_cmd_visibility("show"))

    toggle = sub.add_parser("toggle", help="toggle overlay visibility")
    toggle.set_defaults(func=_cmd_visibility("toggle"))

    status = sub.add_parser("status", help="show daemon status")
    status.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable JSON object",
    )
    status.set_defaults(func=_cmd_status)

    current = sub.add_parser(
        "current",
        help="show the current pen, eraser and text settings",
    )
    current.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable JSON object",
    )
    current.add_argument(
        "--tsv",
        action="store_true",
        help="print tab-separated rows for the fzf picker",
    )
    current.set_defaults(func=_cmd_current)

    set_parser = sub.add_parser(
        "set",
        help="switch a current setting (persists between runs)",
        description=(
            "Switch a setting and remember it. Values may be aliases from the "
            "JSON config or raw values: colours accept #rrggbb / GTK names, "
            "sizes accept pixels."
        ),
    )
    set_parser.add_argument(
        "what",
        choices=sorted(SET_FIELDS),
        metavar="WHAT",
        help=(
            "one of: "
            + ", ".join(sorted(SET_FIELDS))
        ),
    )
    set_parser.add_argument("value", metavar="VALUE")
    set_parser.set_defaults(func=_cmd_set)

    colors = sub.add_parser(
        "colors",
        help="list colour aliases from the config",
    )
    colors.add_argument("--json", action="store_true", help="print JSON")
    colors.add_argument(
        "--tsv", action="store_true", help="print alias<TAB>hex rows"
    )
    colors.set_defaults(func=_cmd_colors)

    sizes = sub.add_parser(
        "sizes",
        help="list size aliases from the config",
    )
    sizes.add_argument(
        "group",
        nargs="?",
        choices=["pen", "eraser", "text"],
        default=None,
        help="show only the group used by this tool",
    )
    sizes.add_argument("--json", action="store_true", help="print JSON")
    sizes.add_argument(
        "--tsv", action="store_true", help="print alias<TAB>pixels rows"
    )
    sizes.set_defaults(func=_cmd_sizes)

    config = sub.add_parser(
        "config",
        help="show the split config files, or create them",
    )
    config.add_argument(
        "--json",
        action="store_true",
        help="print the effective config as JSON (built-in defaults included)",
    )
    config.add_argument(
        "--init",
        action="store_true",
        help="create missing config files and migrate a legacy config.json",
    )
    config.set_defaults(func=_cmd_config)

    daemon = sub.add_parser(
        "daemon",
        help="run the daemon in the foreground (mainly for debugging)",
    )
    daemon.set_defaults(func=_cmd_daemon)

    quit_parser = sub.add_parser(
        "quit",
        help="stop the daemon (annotations are lost)",
    )
    quit_parser.set_defaults(func=_cmd_quit)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _ensure_runtime(argv: list[str]) -> bool:
    """Re-exec with the project venv when pycairo is missing."""
    try:
        import cairo  # noqa: F401
    except ImportError:
        project_dir = os.path.dirname(os.path.realpath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python")
        if (
            os.path.isfile(venv_python)
            and os.environ.get("SCRAWL_REEXEC") != "1"
            and os.path.realpath(sys.executable) != os.path.realpath(venv_python)
        ):
            env = dict(os.environ)
            env["SCRAWL_REEXEC"] = "1"
            os.execve(
                venv_python,
                [venv_python, os.path.realpath(__file__), *argv],
                env,
            )
        print(
            f"{APP_NAME}: pycairo is required. Run ./install.sh or install it "
            "with: python -m pip install pycairo",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not _ensure_runtime(arguments):
        return 1

    parser = _build_parser()
    args = parser.parse_args(arguments)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 1
    except RuntimeError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
