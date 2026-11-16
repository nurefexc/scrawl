# Copyright (c) 2026 nurefexc
# SPDX-License-Identifier: MIT
"""Command-line client for the scrawl daemon."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from typing import Any, Callable, Optional

from . import APP_NAME, VERSION
from .config import Config, Settings
from .defaults import CONFIG_SECTIONS, DEFAULT_CONFIG, SET_FIELDS
from .ipc import ScrawlClient
from .paths import RuntimePaths


def parse_point(text: str) -> tuple[float, float]:
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


class Cli:
    def __init__(self, paths: Optional[RuntimePaths] = None) -> None:
        self.paths = paths or RuntimePaths()
        self.client = ScrawlClient(self.paths)

    # -- shared helpers ---------------------------------------------------

    def _load(self) -> tuple[Config, Settings]:
        config = Config(self.paths)
        config.load()
        settings = Settings.load(self.paths)
        return config, settings

    # -- commands ---------------------------------------------------------

    def cmd_daemon(self, _args: argparse.Namespace) -> int:
        # Imported lazily so config/CLI commands do not need GTK/cairo.
        from .daemon import ScrawlDaemon

        try:
            daemon = ScrawlDaemon(self.paths)
        except SystemExit as exc:
            return int(exc.code or 0)
        except Exception as exc:
            print(f"{APP_NAME}: daemon failed to start: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 1
        return daemon.run()

    def cmd_status(self, args: argparse.Namespace) -> int:
        config, settings = self._load()
        current = config.current(settings)

        if not self.client.can_connect(timeout=0.3):
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
                response = self.client.exchange(
                    {"cmd": "status"}, start_daemon=False
                )
            except Exception as exc:
                print(f"{APP_NAME}: {exc}", file=sys.stderr)
                return 1

        response["config_dir"] = self.paths.config_dir()
        response["config_files"] = {
            section: self.paths.config_path(section)
            for section in CONFIG_SECTIONS
        }
        response["settings"] = self.paths.settings_path()
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
            workspace = (
                "hidden" if response.get("workspace_hidden") else "visible"
            )
            print(
                f"desktop: {response.get('desktop')} "
                f"(current {response.get('current_desktop')}, {workspace})"
            )
            print(f"visible: {'yes' if response['visible'] else 'no'}")
            print(
                f"click-through: "
                f"{'yes' if response.get('clickthrough') else 'no'}"
            )
            print(f"mode: {response['mode'] or 'idle'}")
            print(f"items: {response['items']}")

        pen = current["pen"]
        eraser = current["eraser"]
        text = current["text"]
        print(
            f"pen: {pen['color']} ({pen['color_hex']}), "
            f"{pen['size']} ({pen['size_px']:g} px)"
        )
        print(f"eraser: {eraser['size']} ({eraser['size_px']:g} px)")
        print(
            f"text: {text['color']} ({text['color_hex']}), "
            f"{text['size']} ({text['size_px']:g} px), {text['font']}"
        )
        print(f"config dir: {self.paths.config_dir()}")
        print(f"settings: {self.paths.settings_path()}")
        if response.get("running"):
            print(f"socket: {response['socket']}")
            print(f"log: {response['log']}")
        return 0

    def cmd_current(self, args: argparse.Namespace) -> int:
        config, settings = self._load()
        current = config.current(settings)

        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "ok": True,
                        "config_dir": self.paths.config_dir(),
                        "config_files": {
                            section: self.paths.config_path(section)
                            for section in CONFIG_SECTIONS
                        },
                        "settings": self.paths.settings_path(),
                        **current,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if getattr(args, "tsv", False):
            for tool in ("pen", "eraser", "text"):
                item = current[tool]
                color = (
                    item["color"] if item["color"] is not None else "-"
                )
                hex_value = (
                    item["color_hex"]
                    if item["color_hex"] is not None
                    else "-"
                )
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

    def cmd_set(self, args: argparse.Namespace) -> int:
        config, settings = self._load()
        tool, field = SET_FIELDS[args.what]
        value = args.value

        if field == "color":
            config.resolve_colour(value)
        elif field == "size":
            config.resolve_size(value)
        elif field in ("bold", "italic"):
            normalized = str(value).strip().lower()
            if normalized in ("1", "true", "yes", "on"):
                value = True
            elif normalized in ("0", "false", "no", "off"):
                value = False
            else:
                raise RuntimeError(
                    f"expected a boolean for {args.what}, got {value!r}"
                )
        elif field == "font":
            if not str(value).strip():
                raise RuntimeError("font must not be empty")

        settings.set(tool, field, value)
        settings.save()
        print(f"{args.what}: {value}")
        return 0

    def cmd_colors(self, args: argparse.Namespace) -> int:
        config, _settings = self._load()
        colors: dict[str, str] = {}
        for alias, value in config.colors.items():
            if not isinstance(value, str):
                continue
            try:
                colour = config.resolve_colour(value)
            except argparse.ArgumentTypeError:
                continue
            colors[str(alias)] = colour.hex

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

    def cmd_sizes(self, args: argparse.Namespace) -> int:
        config, _settings = self._load()
        sizes_config = config.sizes
        aliases: list[str]
        if args.group:
            group = config.groups.get(args.group, [])
            aliases = [
                str(alias) for alias in group if str(alias) in sizes_config
            ]
        else:
            aliases = [str(alias) for alias in sizes_config]

        sizes: dict[str, float] = {}
        for alias in aliases:
            try:
                value, _name = config.resolve_size(alias)
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

    def cmd_config(self, args: argparse.Namespace) -> int:
        config = Config(self.paths)
        if getattr(args, "init", False):
            for message in config.init():
                print(f"{APP_NAME}: {message}")

        config.load()
        if getattr(args, "json", False):
            print(json.dumps(config.data, ensure_ascii=False, indent=2))
            return 0

        defaults = DEFAULT_CONFIG["defaults"]
        pen = defaults["pen"]
        eraser = defaults["eraser"]
        text = defaults["text"]
        print(f"config dir: {self.paths.config_dir()}")
        for section in CONFIG_SECTIONS:
            path = self.paths.config_path(section)
            origin = "file" if os.path.exists(path) else "built-in defaults"
            print(
                f"{section}: {len(config.data[section])} entries "
                f"({origin}: {path})"
            )
        print(f"default pen: {pen.get('color')} / {pen.get('size')} (built in)")
        print(f"default eraser: {eraser.get('size')} (built in)")
        print(f"default text: {text.get('color')} / {text.get('size')} (built in)")
        print(f"current settings: {self.paths.settings_path()}")
        return 0

    def cmd_clear(self, _args: argparse.Namespace) -> int:
        if not self.client.can_connect():
            return 0
        self.client.exchange({"cmd": "clear"}, start_daemon=False)
        return 0

    def cmd_undo(self, _args: argparse.Namespace) -> int:
        if not self.client.can_connect():
            return 0
        self.client.exchange({"cmd": "undo"}, start_daemon=False)
        return 0

    def cmd_visibility(self, action: str) -> Callable[[argparse.Namespace], int]:
        def run(_args: argparse.Namespace) -> int:
            if not self.client.can_connect():
                # Starting a daemon only to immediately hide it would be
                # silly, so a toggle with no daemon means "show a fresh
                # overlay".
                if action in ("show", "toggle"):
                    self.client.exchange({"cmd": "show"}, start_daemon=True)
                return 0
            self.client.exchange({"cmd": action}, start_daemon=False)
            return 0

        return run

    def cmd_quit(self, _args: argparse.Namespace) -> int:
        if not self.client.can_connect():
            return 0
        self.client.exchange({"cmd": "quit"}, start_daemon=False)
        return 0

    def cmd_pen(self, args: argparse.Namespace, eraser: bool = False) -> int:
        config, settings = self._load()
        tool = "eraser" if eraser else "pen"

        if args.size is not None:
            settings.set(tool, "size", args.size)
        if not eraser and args.color is not None:
            settings.set("pen", "color", args.color)

        values = config.tool_values(tool, settings)
        settings.save()

        command: dict[str, Any] = {
            "cmd": tool,
            "size": values["size"],
        }
        if not eraser:
            command["color"] = [
                float(channel) for channel in values["color"]
            ]

        if sys.stderr.isatty():
            if eraser:
                print(
                    f"{APP_NAME}: erasing with {values['size_name']} "
                    f"({values['size_px']:g} px) - drag with the left mouse "
                    "button; exit: Esc / Enter / right-click",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"{APP_NAME}: drawing with {values['color_name']} "
                    f"({values['color_hex']}) at {values['size_name']} "
                    f"({values['size_px']:g} px) - drag with the left mouse "
                    "button; exit: Esc / Enter / right-click",
                    file=sys.stderr,
                    flush=True,
                )
        self.client.exchange(command)
        return 0

    def cmd_eraser(self, args: argparse.Namespace) -> int:
        return self.cmd_pen(args, eraser=True)

    def cmd_text(self, args: argparse.Namespace) -> int:
        config, settings = self._load()

        if args.color is not None:
            settings.set("text", "color", args.color)
        if args.size is not None:
            settings.set("text", "size", args.size)
        if args.font is not None:
            settings.set("text", "font", args.font)
        if args.bold:
            settings.set("text", "bold", True)
        if args.italic:
            settings.set("text", "italic", True)

        values = config.tool_values("text", settings)
        settings.save()

        text = " ".join(args.text) if args.text else None
        if text == "":
            text = None
        at = args.at
        command: dict[str, Any] = {
            "cmd": "text",
            "text": text,
            "color": [float(channel) for channel in values["color"]],
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
        self.client.exchange(command)
        return 0

    # -- parser -----------------------------------------------------------

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=APP_NAME,
            description=(
                "Click-through screen annotation overlay controlled entirely "
                "from the command line. Interactive modes exit with Esc, Enter "
                "or a right-click."
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
        parser.add_argument(
            "--version", action="version", version=f"{APP_NAME} {VERSION}"
        )
        sub = parser.add_subparsers(dest="cmd", required=True)

        pen = sub.add_parser(
            "pen",
            aliases=["draw"],
            help="interactive pen mode (drag with the left button)",
            description=(
                "Interactive pen mode. Drag with the left mouse button to "
                "draw. Esc, Enter or right-click exits."
            ),
        )
        pen.add_argument(
            "--color",
            "-c",
            default=None,
            metavar="COLOR",
            help=(
                "colour alias from config or any GTK colour "
                "(default: current setting)"
            ),
        )
        pen.add_argument(
            "--size",
            "-s",
            default=None,
            metavar="PX",
            help="size alias from config or pixels (default: current setting)",
        )
        pen.set_defaults(func=self.cmd_pen)

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
        eraser.set_defaults(func=self.cmd_eraser)

        text = sub.add_parser(
            "text",
            aliases=["write"],
            help="put text on the screen",
            description=(
                "Place text on the screen. With both TEXT and --at X,Y it is "
                "placed immediately. With only TEXT, click to place it "
                "(repeatable). With no TEXT, click a position and type "
                "directly on the overlay."
            ),
        )
        text.add_argument("text", nargs="*", help="text to write (optional)")
        text.add_argument(
            "--at",
            "-a",
            type=parse_point,
            default=None,
            metavar="X,Y",
            help="place immediately at this screen coordinate",
        )
        text.add_argument(
            "--color",
            "-c",
            default=None,
            metavar="COLOR",
            help=(
                "colour alias from config or any GTK colour "
                "(default: current setting)"
            ),
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
        text.set_defaults(func=self.cmd_text)

        clear = sub.add_parser(
            "clear",
            aliases=["all-clear", "allclear"],
            help="remove every annotation",
            description="Remove all pen strokes, eraser marks and text.",
        )
        clear.set_defaults(func=self.cmd_clear)

        undo = sub.add_parser("undo", help="remove the most recent item")
        undo.set_defaults(func=self.cmd_undo)

        hide = sub.add_parser(
            "hide", help="hide the overlay (annotations are kept)"
        )
        hide.set_defaults(func=self.cmd_visibility("hide"))

        show = sub.add_parser("show", help="show the overlay")
        show.set_defaults(func=self.cmd_visibility("show"))

        toggle = sub.add_parser("toggle", help="toggle overlay visibility")
        toggle.set_defaults(func=self.cmd_visibility("toggle"))

        status = sub.add_parser("status", help="show daemon status")
        status.add_argument(
            "--json",
            action="store_true",
            help="print a machine-readable JSON object",
        )
        status.set_defaults(func=self.cmd_status)

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
        current.set_defaults(func=self.cmd_current)

        set_parser = sub.add_parser(
            "set",
            help="switch a current setting (persists between runs)",
            description=(
                "Switch a setting and remember it. Values may be aliases from "
                "the JSON config or raw values: colours accept #rrggbb / GTK "
                "names, sizes accept pixels."
            ),
        )
        set_parser.add_argument(
            "what",
            choices=sorted(SET_FIELDS),
            metavar="WHAT",
            help="one of: " + ", ".join(sorted(SET_FIELDS)),
        )
        set_parser.add_argument("value", metavar="VALUE")
        set_parser.set_defaults(func=self.cmd_set)

        colors = sub.add_parser(
            "colors",
            help="list colour aliases from the config",
        )
        colors.add_argument("--json", action="store_true", help="print JSON")
        colors.add_argument(
            "--tsv", action="store_true", help="print alias<TAB>hex rows"
        )
        colors.set_defaults(func=self.cmd_colors)

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
        sizes.set_defaults(func=self.cmd_sizes)

        config = sub.add_parser(
            "config",
            help="show the split config files, or create them",
        )
        config.add_argument(
            "--json",
            action="store_true",
            help=(
                "print the effective config as JSON "
                "(built-in defaults included)"
            ),
        )
        config.add_argument(
            "--init",
            action="store_true",
            help=(
                "create missing config files and migrate a legacy config.json"
            ),
        )
        config.set_defaults(func=self.cmd_config)

        daemon = sub.add_parser(
            "daemon",
            help="run the daemon in the foreground (mainly for debugging)",
        )
        daemon.set_defaults(func=self.cmd_daemon)

        quit_parser = sub.add_parser(
            "quit",
            help="stop the daemon (annotations are lost)",
        )
        quit_parser.set_defaults(func=self.cmd_quit)

        return parser


def main(argv: Optional[list[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    cli = Cli()
    parser = cli.build_parser()
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
