#!/usr/bin/env python3
"""Global typing-speed reader for io.github.tonciz.live-wpm.

Reads keyboards from /dev/input (needs the `input` group), turns key-downs
into timing-only events (see wpm_core.py) and prints one JSON stats line per
second. Key codes are used only inside classify() to decide what kind of
key it was; no key identity is ever printed or written. Net WPM: Backspace
takes back one character. A held Backspace takes back one more per repeat the
desktop made (estimated from how long it was held and Hyprland's repeat
delay/rate). Ctrl+Backspace takes back the word it removed: the reader keeps
the lengths of the recent words in memory (core.WordTracker, counts only) and
writes that length; with no word to go on it is one character.

stdin commands (one JSON object per line):
  {"cmd":"settings", ...partial settings}   {"cmd":"defaults"}
  {"cmd":"reset"}  (session)   {"cmd":"pause"}  {"cmd":"resume"}  {"cmd":"toggle-pause"}
  {"cmd":"report"}  (rebuild report.html now, then print {"type":"report"})
  {"cmd":"ignore-current"}  {"cmd":"ignore-add","pattern":"re"}  {"cmd":"ignore-remove","pattern":"re"}
"defaults" resets the sliders only; the ignore list is kept.
Ignore patterns match the focused window's class or title. Titles are only
compared in memory, never printed or saved.
SIGUSR1 toggles pause. SIGTERM/SIGINT flush and exit.

Data:   ~/.local/share/omarchy-live-wpm/days/YYYY-MM-DD.gaps.gz, summary.json
Config: ~/.config/omarchy-live-wpm/settings.json
"""

from __future__ import annotations

import argparse
import array
import datetime as dt
import errno
import fcntl
import glob
import gzip
import json
import os
import re
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wpm_core as core  # noqa: E402

EV_SYN, EV_KEY = 0x00, 0x01
SYN_DROPPED = 3
EVENT = struct.Struct("llHHI")
KEY_BITMAP_BYTES = 96
KEY_BACKSPACE = 14
KEY_SPACE = 57
ENTER_CODES = {28, 96}  # the text is sent or the line ends: earlier words are out of reach
WORD_MEMORY_MS = 10_000  # after a pause this long the cursor may be somewhere else
# Characters: number row, letter rows, Enter, Space, keypad, the ISO <> key.
CHAR_CODES = (
    set(range(2, 14)) | set(range(16, 29)) | set(range(30, 42)) | {43}
    | set(range(44, 54)) | {55, 57} | set(range(71, 84)) | {86, 96, 98}
)
CTRL_MODS = {29, 97}
# Held modifiers that turn a key into a shortcut. Shift and AltGr (100) are fine.
SHORTCUT_MODS = {29, 97, 56, 125, 126}

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("XDG_DATA_HOME", HOME + "/.local/share") + "/omarchy-live-wpm"
DAYS_DIR = DATA_DIR + "/days"
SUMMARY_PATH = DATA_DIR + "/summary.json"
CONFIG_DIR = os.environ.get("XDG_CONFIG_HOME", HOME + "/.config") + "/omarchy-live-wpm"
SETTINGS_PATH = CONFIG_DIR + "/settings.json"
REPORT_PATH = DATA_DIR + "/report.html"
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "report", "template.html")
FLUSH_EVERY_S = 60
REPEAT_DEFAULT = (600, 25)  # Hyprland's own defaults: delay ms, repeats per second


def classify(code: int, held_mods: set) -> str | None:
    """char | backspace | None (ignored). Ctrl+Backspace is a backspace too;
    read_device sizes it with the word tracker."""
    mods = held_mods & SHORTCUT_MODS
    if code == KEY_BACKSPACE:
        return "backspace" if mods <= CTRL_MODS else None
    if mods:
        return None
    if code in CHAR_CODES:
        return "char"
    return None


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return core.clamp_settings(json.load(f))
    except (OSError, ValueError):
        return core.default_settings()


def save_settings(s: dict) -> None:
    atomic_write(SETTINGS_PATH, (json.dumps(s, indent=2) + "\n").encode())


def settings_mtime() -> float:
    try:
        return os.stat(SETTINGS_PATH).st_mtime
    except OSError:
        return 0.0


def day_path(day: str) -> str:
    return f"{DAYS_DIR}/{day}.gaps.gz"


def load_day(day: str) -> array.array:
    events = array.array("i")
    try:
        with gzip.open(day_path(day), "rb") as f:
            events.frombytes(f.read())
    except (OSError, ValueError, EOFError):
        pass
    return events


def save_day(day: str, events: array.array) -> None:
    atomic_write(day_path(day), gzip.compress(events.tobytes()))


def local_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).date().isoformat()


def compile_ignore(patterns):
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            pass
    return out


def round_stat(d: dict | None) -> dict | None:
    if d is None:
        return None
    out = dict(d)
    if out.get("active_ms", 0) < core.STAT_MIN_ACTIVE_MS:
        out["wpm"] = None  # one quick burst is not an average yet
    for k in ("wpm", "peak"):
        if out.get(k) is not None:
            out[k] = round(out[k], 1)
    out["active_s"] = round(out.pop("active_ms", 0) / 1000)
    return out


class _Ioctl:
    @staticmethod
    def read(number: int, size: int) -> int:
        return (2 << 30) | (size << 16) | (ord("E") << 8) | number

    @staticmethod
    def bits(fd: int, event_type: int, size: int) -> bytes:
        buf = bytearray(size)
        fcntl.ioctl(fd, _Ioctl.read(0x20 + event_type, size), buf, True)
        return bytes(buf)


def _bit(bits: bytes, n: int) -> bool:
    return n // 8 < len(bits) and bool(bits[n // 8] & (1 << (n % 8)))


def is_keyboard(fd: int) -> bool:
    try:
        if not _bit(_Ioctl.bits(fd, 0, 16), EV_KEY):
            return False
        keys = _Ioctl.bits(fd, EV_KEY, KEY_BITMAP_BYTES)
        return all(_bit(keys, c) for c in (28, 30, 44, 57))  # Enter A Z Space
    except OSError:
        return False


class Reader:
    def __init__(self, print_mode: bool) -> None:
        self.print_mode = print_mode
        self.sel = selectors.DefaultSelector()
        self.not_keyboard: set = set()
        self.last_out = None
        self.last_out_at = 0.0
        self.devices: dict[str, int] = {}
        self.denied = 0
        self.held: set[int] = set()
        self.back_down_ms: int | None = None  # a recorded Backspace is being held
        self.back_ctrl = False                # ...and it was Ctrl+Backspace
        self.words = core.WordTracker()
        self.repeat_delay_ms, self.repeat_rate = REPEAT_DEFAULT
        self.settings = load_settings()
        self.settings_mtime = settings_mtime()
        self.ignore_re = compile_ignore(self.settings["ignore_apps"])
        self.paused = False
        self.app = ""
        self.title = ""
        self.address = ""
        self.ignored = False
        self.hypr: socket.socket | None = None
        self.hypr_buf = b""
        self.stdin_buf = b""
        self.stdin_open = True

        self.day = local_day(int(time.time() * 1000))
        self.today = load_day(self.day)
        self.session = array.array("i")
        self.last_ms: int | None = None
        self.dirty = False
        self.last_flush = time.monotonic()
        self.today_summary = None
        self.session_summary = None
        self.summary_dirty = True
        self.day_cache: dict[str, dict] = {}
        self.report_dirty = True
        self.load_day_cache()

    # ---------- history ----------
    def load_day_cache(self) -> None:
        sig = core.math_signature(self.settings)
        cache = {}
        try:
            with open(SUMMARY_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("sig") == sig:
                cache = raw.get("days", {})
        except (OSError, ValueError, AttributeError):
            pass
        changed = False
        for path in sorted(glob.glob(DAYS_DIR + "/*.gaps.gz")):
            day = os.path.basename(path)[:10]
            if day == self.day or day in cache:
                continue
            s = core.summarize(load_day(day), self.settings)
            cache[day] = {k: s[k] for k in ("chars", "active_ms", "peak", "bursts", "keys")}
            changed = True
        self.day_cache = {d: v for d, v in cache.items() if d != self.day}
        if changed or not os.path.exists(SUMMARY_PATH):
            self.save_day_cache()

    def save_day_cache(self) -> None:
        payload = {"sig": core.math_signature(self.settings), "days": self.day_cache}
        atomic_write(SUMMARY_PATH, json.dumps(payload, separators=(",", ":")).encode())

    def flush(self) -> None:
        if self.dirty:
            save_day(self.day, self.today)
            self.dirty = False
            self.report_dirty = True
        if self.report_dirty:
            self.write_report()
        self.last_flush = time.monotonic()

    def write_report(self) -> None:
        """Rebuild report.html: the template with every day's totals inside."""
        try:
            with open(TEMPLATE_PATH, encoding="utf-8") as f:
                template = f.read()
            days = dict(self.day_cache)
            if len(self.today):
                t = core.summarize(self.today, self.settings)
                days[self.day] = {k: t[k] for k in ("chars", "active_ms", "peak", "bursts", "keys")}
            rows = [{"d": d, "chars": v["chars"], "ms": v["active_ms"],
                     "peak": None if v.get("peak") is None else round(v["peak"], 1)}
                    for d, v in sorted(days.items()) if v.get("keys")]
            data = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "today": self.day,
                    "settings": self.settings, "days": rows}
            blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
            head, rest = template.split("/*DATA*/", 1)
            _, tail = rest.split("/*END*/", 1)
            atomic_write(REPORT_PATH, (head + blob + tail).encode())
            self.report_dirty = False
        except (OSError, ValueError) as error:
            print(f"report: {error}", file=sys.stderr, flush=True)

    def roll_day(self, new_day: str) -> None:
        self.flush()
        s = core.summarize(self.today, self.settings)
        if s["keys"]:
            self.day_cache[self.day] = {k: s[k] for k in ("chars", "active_ms", "peak", "bursts", "keys")}
            self.save_day_cache()
        self.day = new_day
        self.today = load_day(new_day)
        self.last_ms = None
        self.summary_dirty = True
        self.write_report()

    # ---------- settings ----------
    def apply_settings(self, new: dict, persist: bool = True) -> None:
        new = core.clamp_settings(new)
        old_sig = core.math_signature(self.settings)
        self.settings = new
        self.ignore_re = compile_ignore(new["ignore_apps"])
        self.update_ignored()
        if persist:
            save_settings(new)
            self.settings_mtime = settings_mtime()
        if core.math_signature(new) != old_sig:
            self.day_cache = {}
            self.load_day_cache()
        self.summary_dirty = True
        self.report_dirty = True

    def check_settings_file(self) -> None:
        m = settings_mtime()
        if m != self.settings_mtime:
            self.settings_mtime = m
            self.apply_settings(load_settings(), persist=False)

    # ---------- recording ----------
    def record(self, ms: int, delta: int) -> None:
        if self.paused or self.ignored:
            return
        day = local_day(ms)
        if day != self.day:
            self.roll_day(day)
        gap = None if self.last_ms is None else ms - self.last_ms
        if gap is not None and gap < 0:
            gap = None
        value = core.encode(gap, delta)
        self.today.append(value)
        self.session.append(value)
        self.last_ms = ms
        self.dirty = True
        self.summary_dirty = True

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.last_ms = None
        self.back_down_ms = None
        self.words.reset()

    def backspace_up(self, ms: int) -> None:
        """Backspace released: add the letters its auto-repeat removed."""
        down, self.back_down_ms = self.back_down_ms, None
        if down is None or self.last_ms != down:  # another key came in between
            return
        extra = core.held_repeats(ms - down, self.repeat_delay_ms, self.repeat_rate)
        if not extra:
            return
        if self.back_ctrl:  # each repeat removed one more word
            extra = min(255, sum(self.words.word_delete() for _ in range(extra)))
        else:
            self.words.erased(extra)
        self.record(ms, -extra)

    def update_ignored(self) -> None:
        ignored = any(r.fullmatch(self.app) or r.fullmatch(self.title)
                      for r in self.ignore_re if self.app or self.title)
        if ignored != self.ignored:
            self.ignored = ignored
            self.last_ms = None
            self.back_down_ms = None
            self.words.reset()

    # ---------- devices ----------
    def rescan(self) -> None:
        paths = set(glob.glob("/dev/input/event*"))
        # Nodes already seen not to be keyboards are not probed again; a path
        # that disappears is forgotten so a re-plugged node is checked afresh.
        self.not_keyboard &= paths
        for path in list(self.devices):
            if path not in paths:
                self.close_device(path)
        self.denied = 0
        for path in sorted(paths - set(self.devices) - self.not_keyboard):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except PermissionError:
                self.denied += 1
                continue
            except OSError:
                continue
            if not is_keyboard(fd):
                os.close(fd)
                self.not_keyboard.add(path)
                continue
            self.devices[path] = fd
            self.sel.register(fd, selectors.EVENT_READ, ("dev", path))

    def close_device(self, path: str) -> None:
        fd = self.devices.pop(path, None)
        if fd is None:
            return
        try:
            self.sel.unregister(fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def read_device(self, path: str, fd: int) -> None:
        try:
            for _ in range(16):
                data = os.read(fd, EVENT.size * 64)
                if not data:
                    self.close_device(path)
                    return
                usable = len(data) - len(data) % EVENT.size
                for sec, usec, etype, code, value in EVENT.iter_unpack(data[:usable]):
                    if etype == EV_SYN and code == SYN_DROPPED:
                        self.held.clear()
                        self.last_ms = None
                        self.back_down_ms = None
                        self.words.reset()
                        continue
                    if etype != EV_KEY:
                        continue
                    if code in SHORTCUT_MODS or code in (42, 54, 100):
                        if value == 1:
                            self.held.add(code)
                        elif value == 0:
                            self.held.discard(code)
                        continue
                    ms = sec * 1000 + usec // 1000
                    if value == 0 and code == KEY_BACKSPACE:
                        self.backspace_up(ms)
                    if value != 1:  # 0 = release, 2 = auto-repeat
                        continue
                    kind = classify(code, self.held)
                    self.back_down_ms = None
                    if kind is None or self.paused or self.ignored:
                        self.words.reset()  # arrows, shortcuts, Tab...: the cursor moved
                        continue
                    if self.last_ms is None or ms - self.last_ms > WORD_MEMORY_MS:
                        self.words.reset()
                    if kind == "char":
                        if code in ENTER_CODES:
                            self.words.reset()
                        else:
                            self.words.typed(code == KEY_SPACE)
                        self.record(ms, 1)
                        continue
                    self.back_ctrl = bool(self.held & CTRL_MODS)
                    if self.back_ctrl:
                        self.record(ms, -self.words.word_delete())
                    else:
                        self.words.erased(1)
                        self.record(ms, -1)
                    self.back_down_ms = ms
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self.close_device(path)

    # ---------- hyprland ----------
    def connect_hypr(self) -> None:
        if self.hypr is not None:
            return
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if not sig:
            return
        try:
            out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True,
                                 text=True, timeout=2).stdout
            info = json.loads(out or "{}")
            self.app = str(info.get("class", "") or "")
            self.title = str(info.get("title", "") or "")
            self.address = str(info.get("address", "") or "").removeprefix("0x")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self.read_repeat_settings()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(f"{runtime}/hypr/{sig}/.socket2.sock")
            s.setblocking(False)
        except OSError:
            return
        self.hypr = s
        self.sel.register(s, selectors.EVENT_READ, ("hypr", None))
        self.update_ignored()

    def read_repeat_settings(self) -> None:
        """Hyprland's key repeat delay and rate, for held_repeats()."""
        found = []
        for option in ("input:repeat_delay", "input:repeat_rate"):
            try:
                out = subprocess.run(["hyprctl", "getoption", option, "-j"], capture_output=True,
                                     text=True, timeout=2).stdout
                found.append(int(json.loads(out or "{}")["int"]))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                return
        if found[0] > 0 and found[1] > 0:
            self.repeat_delay_ms, self.repeat_rate = found

    def read_hypr(self) -> None:
        try:
            data = self.hypr.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.sel.unregister(self.hypr)
            self.hypr.close()
            self.hypr = None
            return
        self.hypr_buf += data
        *lines, self.hypr_buf = self.hypr_buf.split(b"\n")
        for line in lines:
            text = line.decode("utf-8", "replace")
            if text.startswith("activewindow>>"):
                self.app, _, self.title = text[14:].partition(",")
                self.words.reset()  # another window: another text
                self.update_ignored()
            elif text.startswith("activewindowv2>>"):
                self.address = text[16:].strip().removeprefix("0x")
            elif text.startswith("windowtitlev2>>"):
                addr, _, title = text[15:].partition(",")
                if addr.removeprefix("0x") == self.address and self.address:
                    self.title = title
                    self.update_ignored()

    # ---------- stdin ----------
    def read_stdin(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.sel.unregister(sys.stdin.fileno())
            self.stdin_open = False
            return
        self.stdin_buf += data
        *lines, self.stdin_buf = self.stdin_buf.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.command(msg)

    def command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "settings":
            merged = dict(self.settings)
            merged.update({k: v for k, v in msg.items() if k != "cmd"})
            self.apply_settings(merged)
        elif cmd == "defaults":
            fresh = core.default_settings()
            fresh["ignore_apps"] = list(self.settings["ignore_apps"])
            self.apply_settings(fresh)
        elif cmd in ("ignore-current", "ignore-add", "ignore-remove"):
            apps = list(self.settings["ignore_apps"])
            if cmd == "ignore-current":
                pattern = re.escape(self.app) if self.app else ""
            else:
                pattern = str(msg.get("pattern", ""))[:200]
            if cmd == "ignore-remove":
                apps = [a for a in apps if a != pattern]
            elif pattern and pattern not in apps:
                apps.append(pattern)
            self.apply_settings({**self.settings, "ignore_apps": apps})
        elif cmd == "reset":
            self.session = array.array("i")
            self.summary_dirty = True
        elif cmd == "pause":
            self.set_paused(True)
        elif cmd == "resume":
            self.set_paused(False)
        elif cmd == "toggle-pause":
            self.set_paused(not self.paused)
        elif cmd == "report":
            self.flush()
            self.write_report()
            print(json.dumps({"type": "report", "path": REPORT_PATH}), flush=True)
        self.emit()

    # ---------- output ----------
    def stats(self) -> dict:
        s = self.settings
        if self.summary_dirty:
            self.today_summary = core.summarize(self.today, s)
            self.session_summary = core.summarize(self.session, s)
            self.summary_dirty = False
        today = self.today_summary
        now = dt.date.fromisoformat(self.day)
        iso = now.isocalendar()[:2]
        past = self.day_cache

        def group(pred):
            days = [v for d, v in past.items() if pred(dt.date.fromisoformat(d))]
            return core.combine(days + [today])

        now_ms = int(time.time() * 1000)
        idle = self.last_ms is None or now_ms - self.last_ms > s["pause_cutoff_ms"]
        if self.devices:
            status = "ready"
        elif self.denied:
            status = "permission-denied"
        else:
            status = "no-keyboard"
        return {
            "type": "stats",
            "status": status,
            "devices": len(self.devices),
            "live": None if (v := core.live(self.session, s)) is None else round(v, 1),
            "idle": idle,
            "paused": self.paused,
            "ignored": self.ignored,
            "app": self.app[:80],
            "session": round_stat(self.session_summary),
            "today": round_stat(core.combine([today])),
            "week": round_stat(group(lambda d: d.isocalendar()[:2] == iso)),
            "month": round_stat(group(lambda d: (d.year, d.month) == (now.year, now.month))),
            "year": round_stat(group(lambda d: d.year == now.year)),
            "all": round_stat(group(lambda d: True)),
            "settings": s,
            "ranges": {k: list(v) for k, v in core.SETTINGS.items()},
        }

    def emit(self) -> None:
        st = self.stats()
        if self.print_mode:
            def f(x):
                return "  -" if x is None else f"{x:5.1f}"
            flags = " PAUSED" if st["paused"] else (" IGNORED" if st["ignored"] else "")
            line = (f"live {f(st['live'])}{' (idle)' if st['idle'] else '       '} | "
                    f"session {f(st['session']['wpm'])} peak {f(st['session']['peak'])} | "
                    f"today {f(st['today']['wpm'])} | all {f(st['all']['wpm'])} | "
                    f"{st['devices']} kbd{flags}")
        else:
            line = json.dumps(st, separators=(",", ":"))
        # Idle output repeats itself: print only on change, plus a heartbeat
        # every 10 s so a consumer can tell the reader is alive.
        now = time.monotonic()
        if line == self.last_out and now - self.last_out_at < 10.0:
            return
        self.last_out, self.last_out_at = line, now
        print(line, flush=True)

    # ---------- main loop ----------
    def run(self) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: self.set_paused(not self.paused))
        if self.stdin_open and not self.print_mode:
            try:
                os.set_blocking(sys.stdin.fileno(), False)
                self.sel.register(sys.stdin.fileno(), selectors.EVENT_READ, ("stdin", None))
            except (OSError, ValueError):
                self.stdin_open = False
        last_scan = 0.0
        last_emit = 0.0
        while True:
            now = time.monotonic()
            if now - last_scan >= 3.0:
                self.rescan()
                self.connect_hypr()
                self.check_settings_file()
                last_scan = now
            for key, _ in self.sel.select(timeout=0.25):
                kind, path = key.data
                if kind == "dev":
                    self.read_device(path, key.fd)
                elif kind == "hypr":
                    self.read_hypr()
                elif kind == "stdin":
                    self.read_stdin()
            now = time.monotonic()
            today = local_day(int(time.time() * 1000))
            if today != self.day:
                self.roll_day(today)
            if now - self.last_flush >= FLUSH_EVERY_S:
                self.flush()
            if now - last_emit >= 1.0:
                self.emit()
                last_emit = now


def main() -> int:
    ap = argparse.ArgumentParser(description="Live WPM reader (timings only)")
    ap.add_argument("--print", dest="print_mode", action="store_true",
                    help="human-readable line per second instead of JSON")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="change a setting in settings.json and exit")
    ap.add_argument("--defaults", action="store_true", help="reset settings to defaults and exit")
    ap.add_argument("--report", action="store_true",
                    help="rebuild report.html from saved data, print its path and exit")
    args = ap.parse_args()

    if args.report:
        Reader(print_mode=True).write_report()
        print(REPORT_PATH)
        return 0

    if args.defaults or args.set:
        s = core.default_settings() if args.defaults else load_settings()
        for item in args.set:
            key, _, value = item.partition("=")
            s[key.strip()] = [v.strip() for v in value.split(",") if v.strip()] \
                if key.strip() == "ignore_apps" else value
        s = core.clamp_settings(s)
        save_settings(s)
        print(json.dumps(s, indent=2))
        return 0

    reader = Reader(args.print_mode)

    def stop(*_):
        reader.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        reader.run()
    except BrokenPipeError:
        reader.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
