"""The walkthrough video pipeline's pure parts, and the narration it ships with.

The rest of scripts/video needs the voice models, a browser or ffmpeg; where
the sentences fall and how the subtitles read needs none of them. The
narration is committed so a re-render needs no GPU, which only holds while it
still matches the script.
"""

import ast
import json
from pathlib import Path

import pytest

from scripts.video import timing

VIDEO = Path(__file__).resolve().parent.parent / "scripts" / "video"


def test_sentence_spans_are_read_from_the_silence_between_sentences():
    sr, gap = 100, 0.32
    voice = [0.5] * 120 + [0.0] * 32 + [0.25] * 80 + [0.0] * 32 + [-0.1] * 50
    assert timing.sentence_spans(voice, sr, 3, gap) == [(0.0, 1.2), (1.52, 2.32), (2.64, 3.14)]


def test_a_short_silence_inside_a_sentence_is_not_a_boundary():
    sr, gap = 100, 0.32
    voice = [0.5] * 50 + [0.0] * 10 + [0.5] * 50 + [0.0] * 32 + [0.5] * 40
    assert timing.sentence_spans(voice, sr, 2, gap) == [(0.0, 1.1), (1.42, 1.82)]


def test_sentence_spans_refuse_audio_with_the_wrong_number_of_gaps():
    voice = [0.5] * 100 + [0.0] * 32 + [0.5] * 100
    with pytest.raises(ValueError, match="expected 2 gaps, found 1"):
        timing.sentence_spans(voice, 100, 3, 0.32)


def test_a_short_sentence_is_one_cue():
    line = "Filter by team, category or operator — or just search for what you remember."
    assert timing.split_cue(line) == [line]


def test_a_long_sentence_splits_at_the_dash_and_keeps_it():
    line = ("Connect only what your team actually uses — "
            "each one comes with a checklist, so you know it really works.")
    assert timing.split_cue(line) == ["Connect only what your team actually uses —",
                                      "each one comes with a checklist, so you know it really works."]


def test_a_very_long_sentence_splits_until_every_cue_fits():
    line = ("Sure — Spooldesk brings every conversation from email, chat, Slack and Telegram into "
            "one shared queue, and it runs on a server you control, so the data stays yours.")
    cues = timing.split_cue(line)
    assert len(cues) == 3 and " ".join(cues) == line
    assert all(len(cue) <= timing.MAX_CUE for cue in cues)
    # The dash five characters in is too close to the edge to cut at.
    assert not cues[0].endswith("—")


def test_a_short_line_is_not_broken():
    assert timing.two_lines("This is the ticket queue.") == "This is the ticket queue."


def test_the_second_line_never_starts_with_a_dash():
    line = "Your customers write from everywhere — email, your website, Slack, Telegram."
    assert timing.two_lines(line) == "Your customers write from everywhere — email,\nyour website, Slack, Telegram."


def test_the_first_line_never_ends_on_a_one_or_two_letter_word():
    line = "Reports show how you are doing at a glance, by team and by operator."
    assert timing.two_lines(line) == "Reports show how you are doing\nat a glance, by team and by operator."


def test_subtitle_files_are_srt_for_upload_and_vtt_for_the_landing():
    cues = [(0.9, 5.1, "Your customers write from everywhere — email, your website, Slack, Telegram."),
            (3725.25, 3727.0, "Short line.")]
    assert timing.srt(cues) == (
        "1\n00:00:00,900 --> 00:00:05,100\n"
        "Your customers write from everywhere — email,\nyour website, Slack, Telegram.\n\n"
        "2\n01:02:05,250 --> 01:02:07,000\nShort line.\n")
    assert timing.vtt(cues) == (
        "WEBVTT\n\n00:00.900 --> 00:05.100\n"
        "Your customers write from everywhere — email,\nyour website, Slack, Telegram.\n\n"
        "01:02:05.250 --> 01:02:07.000\nShort line.\n")


def _literal(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name)
    return ast.literal_eval(node.value)


def test_committed_narration_matches_the_script():
    """Edit a line in narrate.py without regenerating, and the video would say
    the old one; render.py would also be timing cuts to words nobody speaks."""
    scenes = _literal(VIDEO / "narrate.py", "SCENES")
    russian = _literal(VIDEO / "render.py", "RU")
    words = json.loads((VIDEO / "narration" / "words.json").read_text(encoding="utf-8"))
    report = json.loads((VIDEO / "narration" / "report.json").read_text(encoding="utf-8"))
    assert list(words) == [name for name, _ in scenes] == [r["name"] for r in report]
    for idx, ((name, sentences), take) in enumerate(zip(scenes, report), 1):
        assert [s["text"] for s in words[name]["sentences"]] == sentences, name
        assert take["expected"] == " ".join(sentences), name
        assert take["score"] >= 0.95, name
        assert len(russian[name]) == len(sentences), name
        assert (VIDEO / "narration" / f"{idx:02d}-{name}.flac").is_file(), name
