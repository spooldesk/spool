"""Where each sentence falls, and how its subtitle is cut.

The pure part of the walkthrough pipeline, shared by cues.py and render.py.
Standard library only, so the test suite can check it without the voice
models, a browser or ffmpeg.
"""

MAX_CUE = 84        # characters on screen at once, over two lines
LINE = 46           # characters per subtitle line


def sentence_spans(samples, sr, count, gap):
    """Start and end of each sentence, in seconds, found from the silence
    narrate.py puts between sentences: a run of exact zeros at least most of
    `gap` seconds long. Shorter runs are pauses inside a sentence."""
    runs, start = [], None
    for i, v in enumerate(samples):
        if v == 0:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(samples)))
    runs = [(a, b) for a, b in runs if b - a >= int(sr * gap * 0.9)]
    if len(runs) != count - 1:
        raise ValueError(f"expected {count - 1} gaps, found {len(runs)}")
    bounds = [0] + [v for run in runs for v in run] + [len(samples)]
    return [(round(bounds[i] / sr, 3), round(bounds[i + 1] / sr, 3)) for i in range(0, len(bounds), 2)]


def split_cue(s):
    """Split a long sentence at a dash or comma near its middle."""
    if len(s) <= MAX_CUE:
        return [s]
    best = None
    for sep in (" — ", ", ", ": "):
        i = -1
        while (i := s.find(sep, i + 1)) != -1:
            cut = i + (len(sep) if sep != " — " else 2)
            score = abs(cut - len(s) / 2) + (0 if sep == " — " else 6)
            if 12 < cut < len(s) - 12 and (best is None or score < best[0]):
                best = (score, cut)
    if best is None:
        return [s]
    a, b = s[:best[1]].strip(), s[best[1]:].strip()
    return split_cue(a) + split_cue(b)


def two_lines(s, width=LINE):
    """Break near the middle, but never before a dash (in subtitles a leading
    dash marks a new speaker) or after a one- or two-letter word."""
    spaces = [k for k, ch in enumerate(s) if ch == " "]
    if len(s) <= width or not spaces:
        return s

    def cost(k):
        before = s[:k].rsplit(" ", 1)[-1]
        return abs(k - len(s) / 2) + (100 if s[k + 1] in "—–" else 0) + (30 if len(before) <= 2 else 0)
    i = min(spaces, key=cost)
    return s[:i] + "\n" + s[i + 1:]


def _clock(t, sep, hours=True):
    ms = int(round(t * 1000))
    h, m, s, ms = ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000
    return (f"{h:02d}:" if hours or h else "") + f"{m:02d}:{s:02d}{sep}{ms:03d}"


def srt(cues):
    """SubRip, for upload to YouTube and the like. cues: [(start, end, text)]."""
    return "\n".join(f"{n}\n{_clock(a, ',')} --> {_clock(b, ',')}\n{two_lines(t)}\n"
                     for n, (a, b, t) in enumerate(cues, 1))


def vtt(cues):
    """WebVTT, for <track> on the landing; laid out as ffmpeg converts the SRT."""
    return "WEBVTT\n\n" + "\n".join(f"{_clock(a, '.', False)} --> {_clock(b, '.', False)}\n{two_lines(t)}\n"
                                    for a, b, t in cues)
