import datetime as dt
import importlib.util
import os
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, SCRIPTS)
import wpm_core as core  # noqa: E402

GAME_PATTERNS = [r"com\.nvidia\.geforcenow", r".*GeForce NOW.*"]  # an app class and a window title
GAP_70 = 12000 / 70 / 1  # ms per char at 70 wpm (= 171.4)


def stream(n_chars, gap_ms, first_gap=None):
    """n_chars counted keys at a steady gap; first key after a break."""
    out = [core.encode(first_gap, True)]
    out += [core.encode(round(gap_ms), True) for _ in range(n_chars - 1)]
    return out


class CoreMath(unittest.TestCase):
    def setUp(self):
        self.s = core.default_settings()

    def test_steady_70_with_thinking_pauses(self):
        events = []
        for _ in range(10):
            events += stream(60, GAP_70, first_gap=5000)
        r = core.summarize(events, self.s)
        self.assertAlmostEqual(r["wpm"], 70, delta=0.5)
        self.assertEqual(r["bursts"], 10)
        self.assertEqual(r["chars"], 590)  # fencepost: 59 per burst

    def test_end_trim_drops_read_over_then_enter(self):
        events = stream(40, 200) + [core.encode(800, True)]   # read over 0.8 s, Enter
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 39)
        self.assertEqual(r["active_ms"], 39 * 200)
        self.assertAlmostEqual(r["wpm"], 60, delta=0.01)

    def test_end_trim_default_is_the_last_key_only(self):
        self.assertEqual(self.s["end_trim_keys"], 1)
        tail = [core.encode(600, True), core.encode(200, True)]     # hesitation, then 2 more keys
        r = core.summarize(stream(40, 200) + tail, self.s)
        self.assertEqual(r["chars"], 41)
        self.assertEqual(r["active_ms"], 39 * 200 + 600 + 200)

    def test_end_trim_takes_up_to_three_keys(self):
        self.s["end_trim_keys"] = 3
        tail = [core.encode(600, True), core.encode(200, True), core.encode(200, True)]
        r = core.summarize(stream(40, 200) + tail, self.s)
        self.assertEqual(r["chars"], 39)
        r = core.summarize(stream(40, 200) + tail + [core.encode(200, True)], self.s)  # 4 keys: mid-burst
        self.assertEqual(r["chars"], 43)

    def test_end_trim_mid_hesitation_still_counts(self):
        events = stream(20, 200) + [core.encode(800, True)] + [core.encode(200, True)] * 19
        self.assertEqual(core.summarize(events, self.s)["active_ms"], 19 * 200 + 800 + 19 * 200)

    def test_end_trim_keeps_deletion_penalty(self):
        events = stream(40, 200) + [core.encode(700, False)]  # pause, then a fix
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 38)
        self.assertEqual(r["active_ms"], 39 * 200)

    def test_end_trim_off(self):
        self.s["end_trim_keys"] = 0
        r = core.summarize(stream(40, 200) + [core.encode(800, True)], self.s)
        self.assertEqual(r["chars"], 40)

    def test_live_does_not_drop_on_final_enter(self):
        events = stream(200, 200)
        before = core.live(events, self.s)
        self.assertAlmostEqual(core.live(events + [core.encode(900, True)], self.s), before)

    def test_short_burst_ignored(self):
        events = stream(3, 80)  # "Yes" in 160 ms = 225 wpm naive
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 0)
        self.assertIsNone(r["wpm"])

    def test_short_burst_counts_with_low_minimum(self):
        self.s["min_burst_keys"] = 3
        events = stream(4, 500 / 3)  # Y e s Space, 3 gaps in 0.5 s
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 3)
        self.assertAlmostEqual(r["wpm"], 72, delta=0.5)

    def test_backspace_takes_a_letter_back(self):
        events = stream(20, 200)
        events.insert(10, core.encode(200, False))
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 18)  # 19 counted typed - 1 deleted
        self.assertEqual(r["active_ms"], 20 * 200)

    def test_word_delete_takes_word_back(self):
        events = stream(30, 200) + [core.encode(300, -6)]
        self.assertEqual(core.summarize(events, self.s)["chars"], 29 - 6)

    def test_fix_after_pause_still_costs(self):
        events = stream(30, 200) + [core.encode(4000, -6)]  # pause, then Ctrl+Backspace
        self.assertEqual(core.summarize(events, self.s)["chars"], 29 - 6)
        events = stream(30, 200) + [core.encode(4000, False)] * 3
        self.assertEqual(core.summarize(events, self.s)["chars"], 29 - 3)

    def test_short_burst_fixing_itself_costs_nothing_extra(self):
        events = stream(30, 200) + stream(3, 200, first_gap=4000) + [core.encode(200, False)] * 2
        self.assertEqual(core.summarize(events, self.s)["chars"], 29)

    def test_net_never_below_zero(self):
        events = stream(12, 200) + [core.encode(200, -50)]
        r = core.summarize(events, self.s)
        self.assertEqual(r["chars"], 0)
        self.assertEqual(r["wpm"], 0)

    def test_old_backspace_encoding_reads_as_one(self):
        self.assertEqual(core.decode(-(170 + 1)), (170, -1))

    def test_backspace_does_not_count_toward_minimum(self):
        events = stream(5, 200) + [core.encode(200, False)] * 10
        self.assertEqual(core.summarize(events, self.s)["chars"], 0)

    def test_mashed_burst_is_dropped(self):
        mash = [core.encode(5000, True)] + [core.encode(g, True) for g in (0, 22, 45, 22, 0) * 20]
        r = core.summarize(mash, self.s)   # 100 keys in 1.8 s = 670 wpm naive
        self.assertEqual((r["chars"], r["active_ms"], r["bursts"]), (0, 0, 0))
        self.assertIsNone(r["peak"])

    def test_mashing_does_not_set_the_peak(self):
        typing = stream(400, 150)          # 80 wpm for a minute
        mash = [core.encode(5000, True)] + [core.encode(g, True) for g in (0, 22, 45, 22, 0) * 400]
        r = core.summarize(typing + mash + stream(400, 150, first_gap=5000), self.s)
        self.assertAlmostEqual(r["peak"], 80, delta=0.5)
        self.assertAlmostEqual(r["wpm"], 80, delta=0.5)
        self.assertEqual(r["bursts"], 2)

    def test_fast_typing_with_rollover_is_not_mashing(self):
        # 150 wpm with every third key rolled over (33 % of gaps under 30 ms)
        events = [core.encode(5000, True)] + [core.encode(g, True) for g in (20, 110, 110) * 30]
        r = core.summarize(events, self.s)
        self.assertEqual(r["bursts"], 1)
        self.assertEqual(r["chars"], 90)
        self.assertAlmostEqual(r["wpm"], 150, delta=0.1)

    def test_mashed_burst_passes_on_only_extra_deletions(self):
        mash = [core.encode(5000, True)] + [core.encode(22, True)] * 19
        events = stream(30, 200) + mash + [core.encode(22, False)] * 25
        self.assertEqual(core.summarize(events, self.s)["chars"], 29 - 5)

    def test_live_ignores_mashing(self):
        events = stream(200, 200)
        before = core.live(events, self.s)
        mash = [core.encode(5000, True)] + [core.encode(22, True)] * 300
        self.assertAlmostEqual(core.live(events + mash, self.s), before)

    def test_mash_rule_is_part_of_the_history_signature(self):
        self.assertIn(f"-g{core.MASH_GAP_MS}-s40", core.math_signature(self.s))

    def test_word_tracker(self):
        w = core.WordTracker()
        self.assertEqual(w.word_delete(), 1)            # nothing known: one letter
        for word in ("hello", "big", "world"):
            for _ in word:
                w.typed(False)
            w.typed(True)
        self.assertEqual(w.word_delete(), 6)            # "world "
        self.assertEqual(w.word_delete(), 4)            # "big "
        w.typed(False); w.typed(False)                  # "hello ab"
        w.erased(1)                                     # "hello a"
        self.assertEqual(w.word_delete(), 1)            # "a"
        w.erased(2)                                     # "hell"
        self.assertEqual(w.word_delete(), 4)
        self.assertEqual(w.word_delete(), 1)            # empty again
        w.erased(5)                                     # nothing to erase: no error
        w.typed(False); w.reset()
        self.assertEqual(w.word_delete(), 1)

    def test_word_tracker_caps(self):
        w = core.WordTracker()
        for _ in range(500):
            w.typed(False)
        self.assertEqual(w.word_delete(), 255)
        for _ in range(200):
            w.typed(False); w.typed(True)
        self.assertEqual(len(w.earlier), core.WordTracker.MAX_WORDS)

    def test_held_repeats(self):
        self.assertEqual(core.held_repeats(249, 250, 40), 0)    # a tap, or a slow press
        self.assertEqual(core.held_repeats(250, 250, 40), 1)
        self.assertEqual(core.held_repeats(274, 250, 40), 1)
        self.assertEqual(core.held_repeats(275, 250, 40), 2)
        self.assertEqual(core.held_repeats(1250, 250, 40), 41)
        self.assertEqual(core.held_repeats(60000, 250, 40), 255)
        self.assertEqual(core.held_repeats(900, 0, 0), 0)       # repeat turned off

    def test_pause_below_cutoff_counts_as_active(self):
        events = stream(20, 200) + stream(20, 200, first_gap=900)
        r = core.summarize(events, self.s)
        self.assertEqual(r["bursts"], 1)
        self.s["pause_cutoff_ms"] = 800
        r2 = core.summarize(events, self.s)
        self.assertEqual(r2["bursts"], 2)
        self.assertGreater(r2["wpm"], r["wpm"])

    def test_peak_needs_full_window(self):
        events = stream(60, 100)  # 120 wpm but only ~6 s
        self.assertIsNone(core.summarize(events, self.s)["peak"])
        events = stream(400, 100) + stream(400, 200, first_gap=5000)
        peak = core.summarize(events, self.s)["peak"]
        self.assertAlmostEqual(peak, 120, delta=0.5)

    def test_combine_weights_by_time_not_days(self):
        a = core.summarize(stream(21, 120), {**self.s, "min_burst_keys": 1})   # 100 wpm, short
        b = core.summarize(stream(1001, 200), self.s)                           # 60 wpm, long
        c = core.combine([a, b])
        self.assertAlmostEqual(c["wpm"], (1020 * 12000) / (20 * 120 + 1000 * 200), places=6)
        self.assertLess(c["wpm"], 62)

    def test_live_uses_recent_window(self):
        events = stream(400, 300) + stream(400, 150, first_gap=5000)
        self.assertAlmostEqual(core.live(events, self.s), 80, delta=0.5)

    def test_live_none_until_two_seconds(self):
        self.assertIsNone(core.live(stream(12, 100), self.s))

    def test_clamp_settings(self):
        s = core.clamp_settings({"pause_cutoff_ms": 5, "min_burst_keys": "999", "ignore_apps": ["x"]})
        self.assertEqual(s["pause_cutoff_ms"], 500)
        self.assertEqual(s["min_burst_keys"], 50)
        top = core.clamp_settings({"pause_cutoff_ms": 9000, "end_trim_keys": 5, "live_window_s": 300})
        self.assertEqual((top["pause_cutoff_ms"], top["end_trim_keys"], top["live_window_s"]), (2000, 3, 60))
        self.assertTrue(set(core.SLIDERS) <= set(core.SETTINGS))
        self.assertEqual(s["ignore_apps"], ["x"])
        self.assertEqual(s["live_window_s"], 30)

    def test_encode_roundtrip(self):
        for gap, delta in ((0, 1), (0, -1), (170, 1), (None, -1), (10**9, 1), (250, -7), (None, -255)):
            g, d = core.decode(core.encode(gap, delta))
            self.assertEqual(d, delta)
            self.assertEqual(g, core.BREAK_MS if gap is None or gap > core.BREAK_MS else gap)


class ReaderBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_DATA_HOME"] = self.tmp.name + "/data"
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name + "/config"
        spec = importlib.util.spec_from_file_location("wpm_reader", os.path.join(SCRIPTS, "wpm-reader.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.r = self.mod.Reader(print_mode=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_classify(self):
        c = self.mod.classify
        self.assertEqual(c(30, set()), "char")          # A
        self.assertEqual(c(30, {42}), "char")           # Shift+A
        self.assertEqual(c(30, {100}), "char")          # AltGr
        self.assertEqual(c(57, set()), "char")          # Space
        self.assertIsNone(c(46, {29}))                  # Ctrl+C
        self.assertIsNone(c(30, {125}))                 # Super+A
        self.assertIsNone(c(103, set()))                # Up arrow
        self.assertEqual(c(14, set()), "backspace")
        self.assertEqual(c(14, {29}), "backspace")      # Ctrl+Backspace (sized by the word tracker)
        self.assertIsNone(c(14, {125}))                 # Super+Backspace

    def feed(self, rows):
        """Push raw kernel input events (ms, code, value) through read_device."""
        rfd, wfd = os.pipe()
        os.set_blocking(rfd, False)
        os.write(wfd, b"".join(self.mod.EVENT.pack(ms // 1000, ms % 1000 * 1000, self.mod.EV_KEY, code, value)
                               for ms, code, value in rows))
        self.r.read_device("pipe", rfd)
        os.close(rfd)
        os.close(wfd)

    def test_held_key_counts_once(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 5000
        rows = [(now, 23, 1)] + [(now + 250 + 25 * i, 23, 2) for i in range(40)] + [(now + 1300, 23, 0)]
        self.feed(rows)                                 # "i" stuck for 1.3 s: 40 auto-repeats
        self.assertEqual(len(self.r.today), 1)
        self.assertEqual(core.decode(self.r.today[0])[1], 1)

    def test_held_key_inside_a_word_adds_no_chars(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 9000
        rows = []
        for i in range(20):                             # 20 keys, 150 ms apart
            rows += [(now + 150 * i, 30 + i % 9, 1), (now + 150 * i + 60, 30 + i % 9, 0)]
        rows += [(now + 150 * 9 + 70 + 10 * i, 30, 2) for i in range(6)]   # repeats in between
        rows.sort()
        self.feed(rows)
        r = core.summarize(self.r.today, self.r.settings)
        self.assertEqual(len(self.r.today), 20)
        self.assertEqual((r["chars"], r["active_ms"]), (19, 19 * 150))

    def type_keys(self, start, codes, step=150, held=()):
        """Rows for tapping `codes` one after another; `held` = modifier codes down throughout."""
        rows = [(start - 5, m, 1) for m in held]
        for i, code in enumerate(codes):
            rows += [(start + step * i, code, 1), (start + step * i + 60, code, 0)]
        end = start + step * len(codes)
        return rows + [(end - 50, m, 0) for m in held], end

    def deltas(self):
        return [core.decode(v)[1] for v in self.r.today]

    def test_ctrl_backspace_takes_the_word_back(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 9000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows, t = self.type_keys(now, [35, 18, 38, 38, 24, 57, 17, 24, 19])     # "hello wor"
        more, t = self.type_keys(t, [14], held=(29,))                           # Ctrl+Backspace
        self.feed(rows + more)
        self.assertEqual(self.deltas(), [1] * 9 + [-3])
        more, t = self.type_keys(t, [14], held=(97,))                           # again, right Ctrl
        self.feed(more)
        self.assertEqual(self.deltas()[-1], -6)                                 # "hello "
        more, t = self.type_keys(t, [14], held=(29,))
        self.feed(more)
        self.assertEqual(self.deltas()[-1], -1)                                 # nothing known

    def test_ctrl_backspace_after_backspaces_takes_what_is_left(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 9000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows, t = self.type_keys(now, [30, 31, 32, 33, 34, 14, 14])             # 5 letters, 2 back
        more, t = self.type_keys(t, [14], held=(29,))
        self.feed(rows + more)
        self.assertEqual(self.deltas(), [1] * 5 + [-1, -1, -3])

    def test_ctrl_backspace_is_one_letter_when_the_cursor_may_have_moved(self):
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        cases = {"arrow key": [105], "shortcut": None, "Enter": [28], "Tab": [15]}
        now = int(dt.datetime.now().timestamp() * 1000) - 60000
        for name, codes in cases.items():
            self.r.today = self.mod.array.array("i")
            rows, t = self.type_keys(now, [30, 31, 32, 33, 34])
            if codes is None:
                more, t = self.type_keys(t, [30], held=(29,))                   # Ctrl+A
            else:
                more, t = self.type_keys(t, codes)
            last, t = self.type_keys(t, [14], held=(29,))
            self.feed(rows + more + last)
            self.assertEqual(self.deltas()[-1], -1, name)
            now = t + 500
        self.r.today = self.mod.array.array("i")
        rows, t = self.type_keys(now, [30, 31, 32, 33, 34])
        last, t = self.type_keys(t + 11000, [14], held=(29,))                   # 11 s later
        self.feed(rows + last)
        self.assertEqual(self.deltas()[-1], -1, "long pause")
        self.r.today = self.mod.array.array("i")
        rows, t = self.type_keys(t + 500, [30, 31, 32, 33, 34])
        self.feed(rows)
        self.r.words.reset()                                                    # as a window switch does
        last, t = self.type_keys(t, [14], held=(29,))
        self.feed(last)
        self.assertEqual(self.deltas()[-1], -1, "window switch")

    def test_shift_does_not_break_a_word(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 9000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows, t = self.type_keys(now, [30], held=(42,))                         # capital letter
        more, t = self.type_keys(t, [31, 32, 33])
        last, t = self.type_keys(t, [14], held=(29,))
        self.feed(rows + more + last)
        self.assertEqual(self.deltas(), [1, 1, 1, 1, -4])

    def test_ctrl_backspace_costs_the_word_in_a_burst(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 20000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows, t = self.type_keys(now, ([30] * 5 + [57]) * 5 + [30] * 5)         # 5 words + "aaaaa"
        more, t = self.type_keys(t, [14], held=(29,))
        again, t = self.type_keys(t, [30] * 5 + [57])                           # retype it
        self.feed(rows + more + again)
        r = core.summarize(self.r.today, self.r.settings)
        self.assertEqual(r["chars"], 34 - 5 + 6)        # net text: 6 words of 5 + 5 spaces, less the first key

    def test_held_ctrl_backspace_takes_a_word_per_repeat(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 20000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows, t = self.type_keys(now, ([30] * 4 + [57]) * 4)                    # 4 words of 4 + space
        rows += [(t - 5, 29, 1), (t, 14, 1), (t + 290, 14, 0), (t + 300, 29, 0)]   # held: 2 repeats
        self.feed(rows)
        self.assertEqual(self.deltas()[-2:], [-5, -10])

    def test_tapped_backspace_takes_one_letter(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 5000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        self.feed([(now, 14, 1), (now + 90, 14, 0), (now + 200, 14, 1), (now + 290, 14, 0)])
        self.assertEqual([core.decode(v) for v in self.r.today], [(core.BREAK_MS, -1), (200, -1)])

    def test_held_backspace_takes_its_repeats_too(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 5000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        # held 0.75 s: the desktop repeats at 250, 275, ... 750 ms = 21 more letters.
        # The kernel's own repeat events (value 2) are not what is counted.
        rows = [(now, 14, 1)] + [(now + 250 + 33 * i, 14, 2) for i in range(15)] + [(now + 750, 14, 0)]
        self.feed(rows)
        self.assertEqual([core.decode(v) for v in self.r.today], [(core.BREAK_MS, -1), (750, -21)])

    def test_held_backspace_costs_chars_and_time_in_a_burst(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 9000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        rows = [(now + 150 * i, 30, 1) for i in range(30)]           # 30 letters
        t = now + 150 * 30
        rows += [(t, 14, 1), (t + 500, 14, 0)]                       # hold: 1 + 11 letters back
        rows += [(t + 600 + 150 * i, 30, 1) for i in range(12)]      # retype 12
        self.feed(rows)
        r = core.summarize(self.r.today, self.r.settings)
        self.assertEqual(r["chars"], 29 - 12 + 12)
        self.assertEqual(r["active_ms"], 150 * 30 + 500 + 100 + 150 * 11)

    def test_backspace_hold_is_dropped_when_another_key_came_between(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 5000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        self.feed([(now, 14, 1), (now + 100, 30, 1), (now + 900, 14, 0)])
        self.assertEqual([core.decode(v)[1] for v in self.r.today], [-1, 1])

    def test_backspace_released_while_paused_records_nothing(self):
        now = int(dt.datetime.now().timestamp() * 1000) - 5000
        self.r.repeat_delay_ms, self.r.repeat_rate = 250, 40
        self.feed([(now, 14, 1)])
        self.r.set_paused(True)
        self.feed([(now + 900, 14, 0)])
        self.assertEqual(len(self.r.today), 1)

    def test_midnight_splits_days(self):
        midnight = dt.datetime(2026, 11, 4, 0, 0, 0)
        start = int((midnight - dt.timedelta(seconds=2)).timestamp() * 1000)
        self.r.day = "2026-11-03"
        self.r.today = self.mod.array.array("i")
        for i in range(30):
            self.r.record(start + i * 150, True)  # 4.5 s of typing over midnight
        self.assertEqual(self.r.day, "2026-11-04")
        self.assertIn("2026-11-03", self.r.day_cache)
        before = self.r.day_cache["2026-11-03"]["keys"]
        self.assertEqual(before + len(self.r.today), 30)
        self.assertEqual(core.decode(self.r.today[0])[0], core.BREAK_MS)

    def test_paused_and_ignored_record_nothing(self):
        self.r.set_paused(True)
        self.r.record(1_000_000, True)
        self.assertEqual(len(self.r.session), 0)
        self.r.set_paused(False)
        self.r.apply_settings({**self.r.settings, "ignore_apps": GAME_PATTERNS}, persist=False)
        self.r.app = "com.nvidia.geforcenow"
        self.r.update_ignored()
        self.r.record(1_000_100, True)
        self.assertEqual(len(self.r.session), 0)

    def test_ignore_matches_class_or_title(self):
        self.r.apply_settings({**self.r.settings, "ignore_apps": GAME_PATTERNS}, persist=False)
        self.r.app, self.r.title = "brave-browser", "Fortnite - GeForce NOW - Brave"
        self.r.update_ignored()
        self.assertTrue(self.r.ignored)
        self.r.app, self.r.title = "com.nvidia.geforcenow", "x"
        self.r.update_ignored()
        self.assertTrue(self.r.ignored)
        self.r.app, self.r.title = "brave-browser", "Gmail"
        self.r.update_ignored()
        self.assertFalse(self.r.ignored)

    def test_ignore_current_and_remove(self):
        self.r.app, self.r.title = "org.some.Game", "Level 1"
        self.r.command({"cmd": "ignore-current"})
        self.assertIn(r"org\.some\.Game", self.r.settings["ignore_apps"])
        self.assertTrue(self.r.ignored)
        self.r.command({"cmd": "defaults"})        # sliders only, list kept
        self.assertIn(r"org\.some\.Game", self.r.settings["ignore_apps"])
        self.r.command({"cmd": "ignore-remove", "pattern": r"org\.some\.Game"})
        self.assertFalse(self.r.ignored)

    def test_settings_change_recomputes_history(self):
        day = "2020-01-01"
        events = self.mod.array.array("i", stream(20, 200) + stream(20, 200, first_gap=900))
        self.mod.save_day(day, events)
        self.r.load_day_cache()
        one = self.r.day_cache[day]["bursts"]
        self.r.apply_settings({**self.r.settings, "pause_cutoff_ms": 800})
        self.assertEqual(one, 1)
        self.assertEqual(self.r.day_cache[day]["bursts"], 2)
        self.r.command({"cmd": "defaults"})
        self.assertEqual(self.r.settings, core.default_settings())  # list was default anyway
        self.assertEqual(self.r.day_cache[day]["bursts"], 1)

    def test_week_is_sum_of_chars_over_time(self):
        for day, gap in (("2026-11-02", 200), ("2026-11-03", 100)):  # Mon, Tue
            self.mod.save_day(day, self.mod.array.array("i", stream(101, gap)))
        self.r.day = "2026-11-04"
        self.r.today = self.mod.array.array("i")
        self.r.load_day_cache()
        st = self.r.stats()
        self.assertAlmostEqual(st["week"]["wpm"], 200 * 12000 / (100 * 200 + 100 * 100), delta=0.1)


if __name__ == "__main__":
    unittest.main()
