#!/usr/bin/env python3
"""Synthetic typing tests: Monkeytype's score against this plugin's score.

Nothing here touches a keyboard or the running reader. A simulated typist
"takes" a Monkeytype test; the same key presses are scored twice:

  Monkeytype  its own formula, ported from its source (November 2026):
              frontend/src/ts/utils/strings.ts countChars() and
              test/test-logic.ts: wpm = correctWord / 5 / minutes,
              raw = (allCorrect + incorrect + extra) / 5 / minutes.
              It sees which keys were pressed and the final text.
  Widget      wpm_core.summarize() on what wpm-reader.py would record: key-down
              times, +1 per character key, -1 per Backspace press.

The typist's gaps between keys are drawn from the real recorded gaps in
~/.local/share/omarchy-live-wpm/days (timings only), scaled to a target speed, so
the rhythm is the user's own. Without that data a log-normal stand-in is used.

  python3 tests/sim_monkeytype.py                 10 batches of 10 tests
  python3 tests/sim_monkeytype.py --set end_trim_keys=0 --set pause_cutoff_ms=2000
  python3 tests/sim_monkeytype.py --fix oldctrl           Ctrl+Backspace = 1 letter (the older rule)
  python3 tests/sim_monkeytype.py --fix oldhold           held Backspace = 1 letter (the older rule)
  python3 tests/sim_monkeytype.py --round 2       a fresh set of 100 tests
  python3 tests/sim_monkeytype.py --tune          search the settings
  python3 tests/sim_monkeytype.py --quiet         batch summary lines only
"""

from __future__ import annotations

import argparse
import array
import glob
import gzip
import itertools
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import wpm_core as core  # noqa: E402

REPEAT_DELAY_MS = 250   # Hyprland input:repeat_delay used for the simulated holds
REPEAT_PERIOD_MS = 25   # input:repeat_rate 40

# Each batch: (name, mode, length, typist habits). mode "time" = seconds,
# "words" = word count. Habits:
#   err     chance a key is the wrong letter
#   leave   chance a mistake is never fixed
#   ctrl    share of fixes done with one Ctrl+Backspace (whole word)
#   hold    share of fixes of 3+ letters done by holding Backspace
#   pause   chance per word of losing your place for 1-3 s
BATCHES = [
    ("time 30, careful (2% mistakes)", "time", 30, dict(err=.02, leave=.0, ctrl=0, hold=0, pause=0)),
    ("time 30, typical (5% mistakes)", "time", 30, dict(err=.05, leave=.05, ctrl=0, hold=0, pause=0)),
    ("time 60, typical", "time", 60, dict(err=.05, leave=.05, ctrl=0, hold=0, pause=0)),
    ("time 15, typical", "time", 15, dict(err=.05, leave=.05, ctrl=0, hold=0, pause=0)),
    ("words 50, typical", "words", 50, dict(err=.05, leave=.05, ctrl=0, hold=0, pause=0)),
    ("time 30, fixes with Ctrl+Backspace", "time", 30, dict(err=.05, leave=.05, ctrl=.7, hold=0, pause=0)),
    ("time 30, fixes by holding Backspace", "time", 30, dict(err=.05, leave=.05, ctrl=0, hold=.7, pause=0)),
    ("time 30, sloppy (8%, 30% left in)", "time", 30, dict(err=.08, leave=.30, ctrl=0, hold=0, pause=0)),
    ("time 30, loses place (1-3 s stops)", "time", 30, dict(err=.05, leave=.05, ctrl=0, hold=0, pause=.06)),
    ("time 60, everything mixed", "time", 60, dict(err=.06, leave=.10, ctrl=.3, hold=.2, pause=.03)),
]

LETTERS = "etaoinshrdlucmfwypvbgkqjxz"
WORD_LENGTHS = [1, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 6, 6, 6, 7, 7, 8, 9, 10]


def load_gap_pools():
    """Real gaps (ms) before a character and before a deletion, inside bursts."""
    chars, dels = [], []
    data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")) + "/omarchy-live-wpm/days"
    for path in sorted(glob.glob(data + "/*.gaps.gz")):
        events = array.array("i")
        try:
            with gzip.open(path, "rb") as f:
                events.frombytes(f.read())
        except (OSError, ValueError, EOFError):
            continue
        for value in events:
            gap, delta = core.decode(value)
            if gap <= 1000:
                (chars if delta > 0 else dels).append(gap)
    if len(chars) < 5000 or len(dels) < 500:
        rng = random.Random(1)
        chars = [min(1000, round(rng.lognormvariate(4.75, 0.75))) for _ in range(50000)]
        dels = [min(1000, round(rng.lognormvariate(5.6, 0.6))) for _ in range(5000)]
    return chars, dels


def mt_count_chars(inp: str, target: str, credit_partial: bool, committed: bool):
    """Port of Monkeytype's countChars (utils/strings.ts)."""
    all_correct = correct_word = incorrect = extra = missed = 0
    word_correct = inp == target
    partially = target.startswith(inp)
    for i in range(max(len(inp), len(target))):
        a = inp[i] if i < len(inp) else None
        b = target[i] if i < len(target) else None
        if a == b:
            if b == " " and not word_correct:
                if committed and i == len(inp) - 1:
                    incorrect += 1
                else:
                    extra += 1
            else:
                all_correct += 1
            if word_correct or (credit_partial and partially):
                correct_word += 1
        elif a is None:
            if not credit_partial:
                missed += 1
        elif b is None or (b == " " and a != " " and " " not in inp):
            extra += 1
        else:
            incorrect += 1
    return all_correct, correct_word, incorrect, extra, missed


def hold_ms(letters: int) -> int:
    """How long Backspace is held to remove this many letters (1 + repeats)."""
    return REPEAT_DELAY_MS + (letters - 2) * REPEAT_PERIOD_MS + REPEAT_PERIOD_MS // 2


def run_test(seed: int, mode: str, length: int, habits: dict, pools, speed: float):
    """One simulated test. Returns (mt_wpm, mt_raw, presses).

    presses: (ms, kind, n) with kind "char" / "space" / "back" / "ctrlback" / "holdback";
    n = letters that key really removed (1 for a char or a single Backspace).
    """
    rng = random.Random(seed)
    char_pool, del_pool = pools
    n_words = length if mode == "words" else 400
    words = ["".join(rng.choice(LETTERS[:18] if rng.random() < .8 else LETTERS)
                     for _ in range(rng.choice(WORD_LENGTHS))) for _ in range(n_words)]
    targets = [w + " " for w in words]
    if mode == "words":
        targets[-1] = words[-1]
    limit_ms = length * 1000 if mode == "time" else None

    presses: list = []
    inputs: list[str] = []      # committed words, as typed
    cur = ""                    # text of the word being typed
    now = 0.0
    first = True
    notice_in = None            # keys until the typist notices a mistake
    extra_wait = 0.0

    def press(kind: str, n: int = 1, pool=None) -> bool:
        nonlocal now, first, extra_wait
        if first:
            first = False
        else:
            now += rng.choice(pool or char_pool) * speed + extra_wait
            extra_wait = 0.0
        if limit_ms is not None and now >= limit_ms:
            return False
        presses.append((round(now), kind, n))
        return True

    done = False
    while not done and len(inputs) < len(targets):
        target = targets[len(inputs)]
        wrong_at = next((i for i, c in enumerate(cur) if i >= len(target) or c != target[i]), None)
        if wrong_at is not None and notice_in is not None and notice_in <= 0:
            k = len(cur) - wrong_at
            roll = rng.random()
            if roll < habits["ctrl"]:
                if not press("ctrlback", len(cur), del_pool):
                    break
                cur = ""
            elif roll < habits["ctrl"] + habits["hold"] and k >= 3:
                if not press("holdback", k, del_pool):
                    break
                cur = cur[:wrong_at]
                extra_wait = hold_ms(k)  # the key stays down
            else:
                for _ in range(k):
                    if not press("back", 1, del_pool):
                        done = True
                        break
                    cur = cur[:-1]
            notice_in = None
            continue
        if len(cur) >= len(target) or target[len(cur)] == " ":
            # word finished: a mistake still in it is either fixed now or left
            if wrong_at is not None and notice_in is not None:
                notice_in = 0
                continue
            if target.endswith(" "):
                if rng.random() < habits["pause"]:
                    extra_wait += rng.uniform(1000, 3000)
                if not press("space"):
                    break
                inputs.append(cur + " ")
            else:
                inputs.append(cur)  # last word of a words test: no space
            cur = ""
            notice_in = None
            continue
        want = target[len(cur)]
        if rng.random() < habits["err"]:
            typed = rng.choice([c for c in LETTERS if c != want])
            if wrong_at is None:
                notice_in = None if rng.random() < habits["leave"] else rng.choice((0, 0, 1, 1, 2, 3))
        else:
            typed = want
            if notice_in is not None:
                notice_in -= 1
        if not press("char"):
            break
        cur += typed

    if mode == "time":
        seconds = float(length)
    else:
        seconds = round(presses[-1][0] / 1000, 2)
    all_correct = correct_word = incorrect = extra = 0
    typed_words = inputs + ([cur] if cur else [])
    for i, inp in enumerate(typed_words):
        last = i == len(typed_words) - 1 and bool(cur)
        a, c, w, e, _ = mt_count_chars(inp, targets[i], last and mode == "time", inp.endswith(" "))
        all_correct += a
        correct_word += c
        incorrect += w
        extra += e
    minutes = seconds / 60
    return correct_word / 5 / minutes, (all_correct + incorrect + extra) / 5 / minutes, presses


def widget_wpm(presses, settings: dict, fixes: set):
    """What the widget would show for these key presses."""
    events = []
    prev = None
    words = core.WordTracker()  # used the way wpm-reader.py uses it
    for ms, kind, n in presses:
        if kind in ("char", "space"):
            words.typed(kind == "space")
            delta = 1
        elif kind == "ctrlback":
            delta = -words.word_delete()
            if "oldctrl" in fixes:
                delta = -1
        else:
            words.erased(1)
            delta = -1
        events.append(core.encode(None if prev is None else ms - prev, delta))
        prev = ms
        if kind == "holdback" and "oldhold" not in fixes:
            # as wpm-reader.py does: at key-up, the repeats estimated from the hold
            held = hold_ms(n)
            extra = core.held_repeats(held, REPEAT_DELAY_MS, 1000 // REPEAT_PERIOD_MS)
            if extra:
                words.erased(extra)
                events.append(core.encode(held, -extra))
                prev = ms + held
    r = core.summarize(events, settings)
    return r["wpm"] or 0.0


def make_tests(pools, round_no: int = 1):
    tests = []
    for b, (name, mode, length, habits) in enumerate(BATCHES):
        for i in range(10):
            seed = (round_no - 1) * 10000 + b * 100 + i
            speed = random.Random(seed + 7).uniform(0.6, 1.15)   # about 60 to 110 raw wpm
            tests.append((b, i, run_test(seed, mode, length, habits, pools, speed)))
    return tests


def score(tests, settings, fixes):
    """Mean of (widget - Monkeytype) and of its absolute value, over all tests."""
    diffs = [widget_wpm(p, settings, fixes) - mt for _, _, (mt, _raw, p) in tests]
    return sum(diffs) / len(diffs), sum(abs(d) for d in diffs) / len(diffs)


def report(tests, settings, fixes, quiet: bool) -> None:
    for b, (name, _mode, _length, _habits) in enumerate(BATCHES):
        rows = [(i, mt, raw, widget_wpm(p, settings, fixes)) for bb, i, (mt, raw, p) in tests if bb == b]
        diffs = [w - mt for _, mt, _, w in rows]
        if not quiet:
            print(f"\nBatch {b + 1}: {name}")
            print("   #   monkeytype     raw    widget    diff")
            for i, mt, raw, w in rows:
                print(f"  {i + 1:2d}     {mt:7.1f}  {raw:6.1f}   {w:7.1f}  {w - mt:+6.1f}")
        mean = sum(diffs) / len(diffs)
        mean_abs = sum(abs(d) for d in diffs) / len(diffs)
        mt_mean = sum(r[1] for r in rows) / len(rows)
        print(f"{'  ' if not quiet else ''}batch {b + 1:2d}  {name:36s} monkeytype {mt_mean:5.1f}  "
              f"widget {mt_mean + mean:5.1f}  off by {mean:+5.1f}  (typical miss {mean_abs:4.1f})")
    mean, mean_abs = score(tests, settings, fixes)
    print(f"\nall {len(tests)} tests: widget is {mean:+.2f} wpm from Monkeytype on average, "
          f"typical miss {mean_abs:.2f} wpm")


def tune(tests, settings, fixes) -> None:
    grid = {
        "pause_cutoff_ms": (500, 750, 1000, 1500, 2000, 3000),
        "end_trim_keys": (0, 1, 3),
        "end_hesitation_ms": (400, 800),
        "min_burst_keys": (1, 10),
    }
    results = []
    for combo in itertools.product(*grid.values()):
        s = {**settings, **dict(zip(grid, combo))}
        if s["end_trim_keys"] == 0 and s["end_hesitation_ms"] != 400:
            continue
        mean, mean_abs = score(tests, s, fixes)
        results.append((mean_abs, mean, combo))
    results.sort()
    print("typical miss / average offset / " + ", ".join(grid))
    for mean_abs, mean, combo in results[:8] + results[-3:]:
        print(f"  {mean_abs:5.2f}   {mean:+6.2f}   {combo}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--fix", default="", help="older behaviour to compare with: oldctrl, oldhold")
    ap.add_argument("--current", action="store_true", help="start from the live settings.json, not the defaults")
    ap.add_argument("--round", type=int, default=1, metavar="N",
                    help="which set of 100 tests (1 = the first set, 2 = a fresh one, ...)")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    raw = {}
    if args.current:
        path = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")) + "/omarchy-live-wpm/settings.json"
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            pass
    for item in args.set:
        key, _, value = item.partition("=")
        raw[key.strip()] = value
    settings = core.clamp_settings({**core.default_settings(), **raw})
    fixes = {x.strip() for x in args.fix.split(",") if x.strip()}
    tests = make_tests(load_gap_pools(), args.round)
    print("settings: " + ", ".join(f"{k}={settings[k]}" for k in core.SETTINGS) +
          (f"   fixes: {', '.join(sorted(fixes))}" if fixes else ""))
    if args.tune:
        tune(tests, settings, fixes)
    else:
        report(tests, settings, fixes, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
