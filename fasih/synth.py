"""Read a diacritized Arabic text with Fasih-TTS-V1 (XTTS v2) and write one MP3.

usage: python synth.py <fasih repo dir> <weights dir> <text file> <out.wav>

XTTS reads at most ~160 characters at a time, so the text is cut into pieces and the audio is
joined. How it is cut decides how the reading sounds:
  * every line is its own paragraph; a line without final punctuation (a heading, or a line such
    as "...إلا على الظالمين") gets a full stop, so it is not run into the next line;
  * pieces end at sentence ends (. ؟ ! ؛ :) whenever possible, and short sentences are packed
    together so the model sets their pauses itself;
  * only a sentence longer than the limit is cut inside, at the last comma, else before a word
    starting with و, else at a space, and never leaving a tiny tail;
  * each piece's own leading and trailing silence is trimmed, then a pause is put back that
    matches the boundary: almost none inside a sentence, a short one between sentences and a
    longer one between paragraphs.
"""
import os
import re
import resource
import sys
import time

import numpy as np
import soundfile as sf
import torch

REPO, WEIGHTS, TEXT, OUT = sys.argv[1:5]
sys.path.insert(0, os.path.join(REPO, "src"))
os.chdir(REPO)  # the text front-end loads its lexicon from a relative path

from tts.text.chunk import chunk_text  # noqa: E402
from tts.text.pipeline import TextPipeline  # noqa: E402
from TTS.tts.configs.xtts_config import XttsConfig  # noqa: E402
from TTS.tts.models.xtts import Xtts  # noqa: E402

LIMIT = 150  # characters, diacritics included; XTTS's hard limit for Arabic is 166
RATE = 24000
# XTTS on CPU grows by roughly 90 MB per piece and never gives it back, so a long episode runs a
# 16 GB machine out of memory. The process makes at most this many new pieces, then exits with
# code 3; the caller starts it again and it resumes from the pieces already on disk.
BATCH = int(os.environ.get("FASIH_BATCH", "40"))
PAUSE = {"mid": 0.03, "comma": 0.16, "sentence": 0.38, "paragraph": 0.75}
SENTENCE_END = re.compile(r"(?<=[.؟!؛:])\s+")
FINAL_PUNCT = re.compile(r"[.؟!؛:،]$")


def split_long(sentence):
    """Cut one over-long sentence into pieces of at most LIMIT characters."""
    pieces, rest = [], sentence.strip()
    while len(rest) > LIMIT:
        window = rest[: LIMIT + 1]
        lowest = int(LIMIT * 0.4)
        cut = None
        for pattern in (r"[،,]\s", r"\sو", r"\s"):
            spots = [m.start() + (1 if pattern != r"\sو" else 0) for m in re.finditer(pattern, window)]
            spots = [p for p in spots if p >= lowest and len(rest) - p >= 25]
            if spots:
                cut = spots[-1]
                break
        if cut is None:
            cut = window.rfind(" ")
            cut = cut if cut > 0 else LIMIT
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def segments(text):
    """[(piece, boundary after it)] where boundary is mid, sentence or paragraph."""
    out = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if not FINAL_PUNCT.search(line):
            line += "."
        units = []  # (text, boundary)
        for sentence in SENTENCE_END.split(line):
            if len(sentence) <= LIMIT:
                units.append((sentence, "sentence"))
            else:
                parts = split_long(sentence)
                units += [(p, "mid") for p in parts[:-1]] + [(parts[-1], "sentence")]
        packed = []
        for piece, boundary in units:
            if packed and packed[-1][1] == "sentence" and len(packed[-1][0]) + 1 + len(piece) <= LIMIT:
                packed[-1] = (f"{packed[-1][0]} {piece}", boundary)
            else:
                packed.append((piece, boundary))
        packed[-1] = (packed[-1][0], "paragraph")
        # a cut after a comma keeps a reader's short comma pause; any other cut joins seamlessly
        out += [(p, "comma" if b == "mid" and p.endswith(("،", ",")) else b) for p, b in packed]
    return out


def trim(wav, keep=0.03):
    """Drop the silence XTTS leaves at both ends, keeping a few milliseconds."""
    if not len(wav):
        return wav
    level = np.abs(wav)
    loud = np.where(level > max(level.max() * 0.03, 1e-4))[0]
    if not len(loud):
        return wav
    pad = int(RATE * keep)
    return wav[max(0, loud[0] - pad): min(len(wav), loud[-1] + pad)]


def main():
    torch.set_num_threads(os.cpu_count())
    text = open(TEXT, encoding="utf-8").read()
    pipe = TextPipeline(diacritizer=None)
    plan = []
    for piece, boundary in segments(text):
        try:
            ready = pipe.prepare_inference_text(piece, diac_threshold=0.0)
        except RuntimeError:
            ready = piece
        parts = chunk_text(ready, 160) if len(ready) > 160 else [ready]
        plan += [(p, "mid") for p in parts[:-1]] + [(parts[-1], boundary)]
    print(f"{len(text)} chars, {len(plan)} pieces, {os.cpu_count()} cpus", flush=True)

    cfg = XttsConfig()
    cfg.load_json(f"{WEIGHTS}/config.json")
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_path=f"{WEIGHTS}/model.pth", vocab_path=f"{WEIGHTS}/vocab.json",
                          use_deepspeed=False)
    model.eval()
    latents = torch.load(f"{WEIGHTS}/speaker_latents.pt", map_location="cpu")
    cond, speaker = latents["gpt_cond_latent"], latents["speaker_embedding"]

    # every piece is kept on disk as it is made, so a run that gets killed resumes where it stopped
    parts_dir = OUT + ".parts"
    os.makedirs(parts_dir, exist_ok=True)
    audio, start, made = [], time.time(), 0
    with torch.inference_mode():
        for n, (piece, boundary) in enumerate(plan, 1):
            saved = os.path.join(parts_dir, f"{n:04d}.npy")
            if os.path.exists(saved):
                wav = np.load(saved)
            elif made >= BATCH:
                print(f"{n - 1}/{len(plan)} pieces done, restarting to free memory", flush=True)
                sys.exit(3)
            else:
                made += 1
                out = model.inference(piece, "ar", cond, speaker, temperature=0.65, repetition_penalty=2.0,
                                      enable_text_splitting=False)
                wav = trim(np.asarray(out["wav"], np.float32))
                np.save(saved + ".tmp.npy", wav)
                os.replace(saved + ".tmp.npy", saved)
            audio.append(wav)
            audio.append(np.zeros(int(RATE * PAUSE[boundary]), np.float32))
            if n % 10 == 0 or n == len(plan):
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
                print(f"{n}/{len(plan)} pieces, {(time.time() - start) / 60:.1f} min, peak {rss} MB", flush=True)
    wav = np.concatenate(audio)
    spent = time.time() - start
    print(f"RESULT: {len(wav) / RATE / 60:.1f} min of audio in {spent / 60:.1f} min", flush=True)
    sf.write(OUT, wav, RATE)


if __name__ == "__main__":
    main()
