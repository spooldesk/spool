"""English narration for the walkthrough, one file per scene, checked by ear-proxy.

    <tts-venv>/python scripts/video/narrate.py            # every scene
    <tts-venv>/python scripts/video/narrate.py --only agent

Needs the GPU environment described in docs/VIDEO.md (Chatterbox, Whisper,
HF_HOME for the model cache). Run it only when the script changes: the result
is committed, so a re-render does not need it.

Chatterbox (MIT) with the "lively" delivery the user picked. Each sentence is
generated separately — long inputs are where the model drifts — trimmed of the
silence it pads with, and joined with even pauses, so the rhythm is set here
rather than by chance.

Nobody on this side can listen to the result, so every scene is transcribed
back with Whisper and compared with the script. A scene that does not say what
it should is regenerated with another seed, up to a limit, and the best take is
kept and reported. Loudness is normalised per scene so the level does not jump
between cuts.

Scenes are stored as 24-bit FLAC. Not 16: at 16 bits the quietest ends of
words round to zero and run into the silence cues.py measures sentences by.
"""
import difflib
import json
import re
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
import torch
import torchaudio as ta

NARRATION = Path(__file__).resolve().parent / "narration"

DELIVERY = dict(exaggeration=0.7, cfg_weight=0.3)
GAP = 0.32            # seconds between sentences
LUFS = -18.0          # web voice-over level
TAKES = 4             # seeds tried per scene before settling
GOOD = 0.95           # transcript similarity accepted without retrying

# Spoken forms: what the voice should say, not how the page spells it.
SCENES = [
    ("intro", ["Your customers write from everywhere — email, your website, Slack, Telegram.",
               "Spooldesk brings all of it into one place, on a server you control.",
               "Let me show you how it works."]),
    ("queue", ["This is the ticket queue.",
               "Every conversation lands here, whatever channel it came from.",
               "Filter by team, category or operator — or just search for what you remember."]),
    ("reply", ["Open a ticket and you get the whole story: who's asking, what they wrote, and where it came from.",
               "Type your reply right here.",
               "The moment you send it, the ticket moves to waiting for the customer — nobody has to track that by hand."]),
    ("note", ["Need a colleague's help? Leave an internal note.",
              "Your team sees it. The customer never does."]),
    ("board", ["The board shows the same work by stage — new, in progress, waiting.",
               "One look, and you know what needs you next."]),
    ("templates", ["The answers you give every day become templates.",
                   "One click, instead of typing the same thing for the fortieth time."]),
    ("help", ["And when an answer deserves to last, put it in the help centre.",
              "Customers find it on their own, and every article has a link you can send."]),
    ("channels", ["Channels are switched on in Integrations.",
                  "Connect only what your team actually uses — each one comes with a checklist, so you know it really works."]),
    ("assistant", ["Then there's the AI assistant.",
                   "Ask it in plain words — which tickets still need a reply? — and you get the answer in seconds.",
                   "It can also suggest a change, like moving a ticket forward.",
                   "But nothing happens until you confirm."]),
    ("agent", ["You can also let AI answer customers directly.",
               "Choose a single reply or a whole conversation, and set the limits.",
               "It steps aside the moment a person takes over, or the customer asks for one."]),
    ("reports", ["Reports show how you're doing: response times, resolution, customer satisfaction — by team and by operator."]),
    ("outro", ["Spooldesk runs on your own server, so your data stays under your control.",
               "Try the live demo at spooldesk dot net — and when you're ready, install it yourself."]),
]


def scene_file(idx, name):
    return NARRATION / f"{idx:02d}-{name}.flac"


def trim(wav, sr, floor_db=-45.0, pad=0.04):
    """Drop the silence the model leaves at either end of a sentence."""
    x = wav[0].numpy()
    frame = int(sr * 0.01)
    if len(x) < frame * 3:
        return wav
    energy = np.array([np.sqrt(np.mean(x[i:i + frame] ** 2) + 1e-12)
                       for i in range(0, len(x) - frame, frame)])
    loud = np.where(20 * np.log10(energy) > floor_db)[0]
    if not len(loud):
        return wav
    a = max(0, loud[0] * frame - int(sr * pad))
    b = min(len(x), (loud[-1] + 1) * frame + int(sr * pad))
    return wav[:, a:b]


def words(s):
    s = s.lower().replace("spooldesk dot net", "spooldesk.net")
    return re.findall(r"[a-z0-9']+", s)


def similarity(expected, heard):
    return difflib.SequenceMatcher(None, words(expected), words(heard)).ratio()


def main(only=(), takes=TAKES, good=GOOD, first_take=0):
    from chatterbox.tts import ChatterboxTTS
    from faster_whisper import WhisperModel

    tts = ChatterboxTTS.from_pretrained(device="cuda")
    asr = WhisperModel("small.en", device="cpu", compute_type="int8")
    meter = pyln.Meter(tts.sr)
    NARRATION.mkdir(exist_ok=True)
    report_path = NARRATION / "report.json"
    kept = {r["name"]: r for r in json.loads(report_path.read_text(encoding="utf-8"))} if only else {}
    report = []

    for idx, (name, sentences) in enumerate(SCENES, 1):
        if only and name not in only:
            report.append(kept[name])
            continue
        expected = " ".join(sentences)
        best = None
        for take in range(first_take, first_take + takes):
            parts = []
            for n, sentence in enumerate(sentences):
                torch.manual_seed(1000 * idx + 37 * take + n)
                parts.append(trim(tts.generate(sentence, **DELIVERY), tts.sr))
                parts.append(torch.zeros(1, int(tts.sr * GAP)))
            wav = torch.cat(parts[:-1], dim=1)

            x = wav[0].numpy().astype(np.float64)
            x = pyln.normalize.loudness(x, meter.integrated_loudness(x), LUFS)
            x = np.clip(x, -0.99, 0.99).astype(np.float32)
            sf.write(scene_file(idx, name), x, tts.sr, subtype="PCM_24")

            # As an array, not a path: faster-whisper's file reader passes an
            # argument the installed PyAV no longer accepts.
            heard16k = ta.functional.resample(torch.from_numpy(x), tts.sr, 16000).numpy()
            segments, _ = asr.transcribe(heard16k, language="en", beam_size=5)
            heard = " ".join(s.text.strip() for s in segments)
            score = similarity(expected, heard)
            if best is None or score > best["score"]:
                best = {"take": take, "score": score, "heard": heard,
                        "seconds": round(len(x) / tts.sr, 2), "audio": x}
            print(f"{idx:02d} {name:10s} take {take}: {score:.3f}  {len(x)/tts.sr:5.1f}s")
            if score >= good:
                break

        sf.write(scene_file(idx, name), best.pop("audio"), tts.sr, subtype="PCM_24")
        report.append({"scene": idx, "name": name, "expected": expected, **best})

    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\nscene  score  seconds")
    for r in report:
        flag = "" if r["score"] >= GOOD else "   <-- check"
        print(f"{r['scene']:02d} {r['name']:10s} {r['score']:.3f}  {r['seconds']:5.1f}{flag}")
    print("total", round(sum(r["seconds"] for r in report), 1), "s of speech")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=(), help="regenerate just these scenes")
    ap.add_argument("--takes", type=int, default=TAKES)
    ap.add_argument("--good", type=float, default=GOOD)
    ap.add_argument("--first-take", type=int, default=0, help="start at this seed offset")
    a = ap.parse_args()
    main(tuple(a.only), a.takes, a.good, a.first_take)
