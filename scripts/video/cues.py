"""Word timings for each narration file, so the picture can follow the voice.

    <tts-venv>/python scripts/video/cues.py

Run after narrate.py, in the same environment (Whisper runs on the CPU).
Whisper's word timestamps are close enough (a tenth of a second or so) to start
a click on "send" or a cut on "board". Sentence boundaries are taken from the
silence narrate.py inserts between sentences: those samples are exact zeros.
"""
import json

import torch
import torchaudio as ta

from narrate import GAP, NARRATION, SCENES, scene_file
from timing import sentence_spans


def main():
    from faster_whisper import WhisperModel
    asr = WhisperModel("small.en", device="cpu", compute_type="int8")
    result = {}
    for idx, (name, sentences) in enumerate(SCENES, 1):
        wav, sr = ta.load(str(scene_file(idx, name)))
        x = wav[0].numpy()
        heard16k = ta.functional.resample(torch.from_numpy(x), sr, 16000).numpy()
        segments, _ = asr.transcribe(heard16k, language="en", beam_size=5, word_timestamps=True)
        words = [{"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)}
                 for s in segments for w in s.words]
        spans = sentence_spans(x.tolist(), sr, len(sentences), GAP)
        result[name] = {"seconds": round(len(x) / sr, 3), "words": words,
                        "sentences": [{"text": t, "start": a, "end": b} for t, (a, b) in zip(sentences, spans)]}
        print(f"{idx:02d} {name:10s} {len(words):3d} words  " + "  ".join(f"{a:.2f}-{b:.2f}" for a, b in spans))
    (NARRATION / "words.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
