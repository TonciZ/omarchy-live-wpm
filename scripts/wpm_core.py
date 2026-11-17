"""Pure WPM math for io.github.tonciz.live-wpm. No I/O, no key identities.

An event stream is a list of encoded ints, one per recorded keystroke:
  gap >= 0                  a typed character, `gap` ms after the previous event
  -(gap + 1 + 65536 * n)    a deletion of n characters (Backspace writes n=1,
                            a held Backspace its repeats, Ctrl+Backspace the
                            length of the word it removed; n=0 is read as 1,
                            which is how older files stored Backspace)
`gap` is capped at BREAK_MS; BREAK_MS also marks "no usable previous event"
(first key of the day, after a pause, after an ignored app).

Rules (net WPM, deleted letters don't count):
- A burst is a run of events whose gaps are all <= pause_cutoff_ms.
- A burst counts only if it has >= min_burst_keys typed characters; else it
  is dropped whole.
- Fencepost: each event pays for the gap before it, so the first event of a
  burst adds no time, and if it is a character it adds no character either.
- Deletions take characters back. In a counted burst they subtract where they
  happen. A dropped burst only passes on the deletions beyond its own
  characters (fixing an earlier sentence after a pause still costs).
- End trim: if one of the last end_trim_keys events of a burst came after a
  gap >= end_hesitation_ms (e.g. reading it over, then Enter), that gap and
  everything after it don't count. Deletions in the trimmed tail still take
  their characters back. 0 keys turns it off.
- Key mashing: a burst where MASH_SHARE or more of the gaps are <= MASH_GAP_MS
  (keys landing together: a game, a keyboard test, a cat) is not typing and
  is dropped whole, like a short burst. Real typing has a few percent of such
  gaps; mashed runs have 40-100 % and read as 190+ WPM.
- Held Backspace: the desktop repeats a held key, so holding Backspace removes
  many letters with one press. held_repeats() estimates how many from how long
  it was held; the reader writes them as one more deletion at key-up.
- Ctrl+Backspace removes a whole word, which is then typed again. WordTracker
  counts the letters of the recent words (lengths only) so the reader can take
  the whole word back. With nothing to go on it is one letter.
- Totals never go below zero.
- WPM = chars / 5 / active minutes = chars * 12000 / active_ms.
"""

from __future__ import annotations

BREAK_MS = 60_000

# name: (minimum, maximum, default)
# The popup has a slider for the first three only. The ranges are kept to what
# gives a sensible number: on 14 days of real typing a 0.5 s cutoff already
# reads a third too high and beyond 2 s thinking counts as typing.
# The rest are fixed for normal use; settings.json or `wpm-reader.py --set`
# can still change them.
SETTINGS = {
    "pause_cutoff_ms": (500, 2000, 1000),
    "end_trim_keys": (0, 3, 1),
    "live_window_s": (10, 60, 30),
    "min_burst_keys": (1, 50, 10),
    "peak_window_s": (10, 120, 30),
    "peak_min_chars": (10, 300, 50),
    "end_hesitation_ms": (250, 1500, 400),
}
SLIDERS = ("pause_cutoff_ms", "end_trim_keys", "live_window_s")
# Regexes, matched (whole, case-insensitive) against the focused window's app
# class and its title (a title match catches a game streamed in a browser tab).
# Empty by default: add apps from the popup ("Ignore current app").
DEFAULT_IGNORE_APPS: list = []
MASH_GAP_MS = 30  # keys this close together arrived (nearly) at once
MASH_SHARE = 0.4  # a burst with this share of such gaps is mashing, not typing
LIVE_MIN_ACTIVE_MS = 2000  # live gauge shows nothing until this much typing
STAT_MIN_ACTIVE_MS = 10_000  # session/day/... WPM shows nothing until this much


def default_settings() -> dict:
    out = {name: spec[2] for name, spec in SETTINGS.items()}
    out["ignore_apps"] = list(DEFAULT_IGNORE_APPS)
    return out


def clamp_settings(raw: dict | None) -> dict:
    out = default_settings()
    if not isinstance(raw, dict):
        return out
    for name, (low, high, _default) in SETTINGS.items():
        try:
            out[name] = max(low, min(high, int(round(float(raw[name])))))
        except (KeyError, TypeError, ValueError):
            pass
    apps = raw.get("ignore_apps")
    if isinstance(apps, list):
        out["ignore_apps"] = [str(a)[:200] for a in apps if str(a).strip()][:50]
    return out


def math_signature(s: dict) -> str:
    """Settings that change saved-history results."""
    return ("c{pause_cutoff_ms}-m{min_burst_keys}-w{peak_window_s}-p{peak_min_chars}"
            "-e{end_hesitation_ms}-k{end_trim_keys}").format(**s) + \
        f"-g{MASH_GAP_MS}-s{round(MASH_SHARE * 100)}"


def encode(gap_ms: int | None, delta: int | bool = 1) -> int:
    """delta: +1 (or True) = typed char, False = Backspace, -n = delete n."""
    gap = BREAK_MS if gap_ms is None else max(0, min(BREAK_MS, int(gap_ms)))
    if delta is True or (delta is not False and delta > 0):
        return gap
    n = 1 if delta is False else max(1, min(255, -int(delta)))
    return -(gap + 1 + 65536 * n)


def decode(value: int) -> tuple[int, int]:
    """Return (gap_ms, delta): delta is +1 for a char, -n for a deletion."""
    if value >= 0:
        return value, 1
    raw = -value - 1
    return raw % 65536, -max(1, raw // 65536)


def held_repeats(hold_ms: int, delay_ms: int, rate_hz: int) -> int:
    """Extra key repeats the desktop made while a key was held for hold_ms."""
    if delay_ms <= 0 or rate_hz <= 0 or hold_ms < delay_ms:
        return 0
    return min(255, 1 + (hold_ms - delay_ms) * rate_hz // 1000)


class WordTracker:
    """Lengths of the words typed lately, to size a Ctrl+Backspace.

    Holds counts only: letters and following spaces of the word being typed,
    and of up to MAX_WORDS before it. Never which keys. The reader resets it
    whenever the cursor may have moved (another key, a shortcut, Enter, a new
    window, a long pause).
    """

    MAX_WORDS = 64

    def __init__(self) -> None:
        self.letters = 0
        self.spaces = 0
        self.earlier: list[tuple[int, int]] = []

    def reset(self) -> None:
        self.letters = self.spaces = 0
        self.earlier.clear()

    def typed(self, space: bool) -> None:
        if space:
            self.spaces += 1
            return
        if self.spaces:  # a new word starts
            self.earlier.append((self.letters, self.spaces))
            del self.earlier[:-self.MAX_WORDS]
            self.letters = self.spaces = 0
        self.letters += 1

    def erased(self, count: int = 1) -> None:
        """Backspace took `count` characters off the end."""
        for _ in range(count):
            if not (self.letters or self.spaces):
                if not self.earlier:
                    return
                self.letters, self.spaces = self.earlier.pop()
            if self.spaces:
                self.spaces -= 1
            else:
                self.letters -= 1

    def word_delete(self) -> int:
        """Ctrl+Backspace: the spaces before the cursor and the word before them."""
        if not (self.letters or self.spaces) and self.earlier:
            self.letters, self.spaces = self.earlier.pop()
        count = self.letters + self.spaces
        self.letters = self.spaces = 0
        return max(1, min(255, count))


def wpm(chars: int, active_ms: int) -> float | None:
    if active_ms <= 0:
        return None
    return chars * 12000.0 / active_ms


def trim_end(gaps: list, deltas: list, end_ms: int, end_keys: int):
    """Apply end trim; return (gaps, deltas, typed chars removed)."""
    if end_keys <= 0:
        return gaps, deltas, 0
    for i in range(max(0, len(gaps) - end_keys), len(gaps)):
        if gaps[i] >= end_ms:
            tail = deltas[i:]
            kept = [x for x in tail if x < 0]
            removed = sum(1 for x in tail if x > 0)
            return gaps[:i] + [0] * len(kept), deltas[:i] + kept, removed
    return gaps, deltas, 0


def counted_pieces(events, cutoff_ms: int, min_keys: int,
                   end_ms: int = 10**9, end_keys: int = 0):
    """Return (gaps, deltas, bursts, stray) for counted bursts.

    gaps[i]/deltas[i] describe one event inside a counted burst: the time it
    took and its char delta (+1 typed, -n deleted). `stray` is how many
    deletions dropped bursts pass on (beyond their own typed chars).
    """
    gaps: list[int] = []
    deltas: list[int] = []
    bursts = 0
    stray = 0
    cur_gaps: list[int] = []
    cur_deltas: list[int] = []
    cur_typed = 0
    cur_deleted = 0
    cur_inside = 0  # events after the burst's first one (each has a real gap)
    cur_fast = 0    # how many of those gaps are <= MASH_GAP_MS

    def flush() -> None:
        nonlocal bursts, stray
        g, d, removed = trim_end(cur_gaps, cur_deltas, end_ms, end_keys)
        mashed = cur_inside > 0 and cur_fast >= MASH_SHARE * cur_inside
        if cur_typed - removed >= min_keys and not mashed:
            gaps.extend(g)
            deltas.extend(d)
            bursts += 1
        else:
            stray += max(0, cur_deleted - cur_typed)

    started = False
    for value in events:
        gap, delta = decode(value)
        if not started or gap > cutoff_ms:
            if started:
                flush()
            cur_gaps, cur_deltas, cur_typed, cur_deleted = [], [], 0, 0
            cur_inside = cur_fast = 0
            started = True
            if delta < 0:  # a deletion opening a burst still costs chars
                cur_gaps.append(0)
                cur_deltas.append(delta)
        else:
            cur_gaps.append(gap)
            cur_deltas.append(delta)
            cur_inside += 1
            if gap <= MASH_GAP_MS:
                cur_fast += 1
        if delta > 0:
            cur_typed += 1
        else:
            cur_deleted -= delta
    if started:
        flush()
    return gaps, deltas, bursts, stray


def best_window(gaps, chars, window_ms: int, min_chars: int) -> float | None:
    """Best WPM over any stretch of >= window_ms active time."""
    best = None
    left = 0
    t = 0
    c = 0
    for right in range(len(gaps)):
        t += gaps[right]
        c += chars[right]
        while left < right and t - gaps[left] >= window_ms:
            t -= gaps[left]
            c -= chars[left]
            left += 1
        if t >= window_ms and c >= min_chars:
            value = c * 12000.0 / t
            if best is None or value > best:
                best = value
    return best


def burst_args(s: dict):
    return (s["pause_cutoff_ms"], s["min_burst_keys"], s["end_hesitation_ms"], s["end_trim_keys"])


def summarize(events, s: dict) -> dict:
    gaps, chars, bursts, stray = counted_pieces(events, *burst_args(s))
    total_chars = max(0, sum(chars) - stray)
    active_ms = sum(gaps)
    return {
        "chars": total_chars,
        "active_ms": active_ms,
        "bursts": bursts,
        "keys": len(events),
        "wpm": wpm(total_chars, active_ms),
        "peak": best_window(gaps, chars, s["peak_window_s"] * 1000, s["peak_min_chars"]),
    }


def live(events, s: dict) -> float | None:
    """WPM over the most recent live_window_s of counted active time."""
    tail = events[-4000:]
    gaps, chars, _, _ = counted_pieces(tail, *burst_args(s))
    window = s["live_window_s"] * 1000
    t = 0
    c = 0
    for i in range(len(gaps) - 1, -1, -1):
        t += gaps[i]
        c += chars[i]
        if t >= window:
            break
    if t < min(LIVE_MIN_ACTIVE_MS, window):
        return None
    return max(0, c) * 12000.0 / t


def combine(summaries) -> dict:
    """Aggregate day summaries: sum chars and time, max of peaks."""
    chars = sum(d["chars"] for d in summaries)
    active = sum(d["active_ms"] for d in summaries)
    peaks = [d["peak"] for d in summaries if d.get("peak") is not None]
    return {
        "chars": chars,
        "active_ms": active,
        "days": sum(1 for d in summaries if d["active_ms"] > 0),
        "wpm": wpm(chars, active),
        "peak": max(peaks) if peaks else None,
    }
