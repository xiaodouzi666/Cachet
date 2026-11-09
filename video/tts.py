"""Generate and time all eight English voiceover tracks with Kokoro v1.0."""

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [14, 18, 23, 25, 25, 20, 25, 20]
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0"


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=ROOT / ".cache/kokoro")
    parser.add_argument("--output", type=Path, default=ROOT / "video/out/narration")
    parser.add_argument("--speed", type=float, default=1.03)
    args = parser.parse_args()
    if not 0.8 <= args.speed <= 1.3:
        parser.error("Choose a narration speed from 0.8 to 1.3")
    from kokoro_onnx import Kokoro
    import numpy as np
    import onnxruntime as ort
    import soundfile as sf

    model = args.models / "kokoro-v1.0.onnx"
    voices = args.models / "voices-v1.0.bin"
    if not all(f.is_file() for f in (model, voices)):
        parser.error(f"Download kokoro-v1.0.onnx and voices-v1.0.bin from {RELEASE}")
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(4, os.cpu_count() or 1)
    session = ort.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"]
    )
    synthesizer = Kokoro.from_session(session, str(voices))
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, target in enumerate(TARGETS, start=1):
        source = ROOT / f"video/narration/S{n}.txt"
        samples, rate = synthesizer.create(
            source.read_text().strip(), voice="af_heart", speed=args.speed, lang="en-us"
        )
        if rate != 24000 or len(samples) == 0 or np.max(np.abs(samples)) < 0.001:
            raise ValueError(f"Scene {n} produced no valid 24 kHz audio")
        path = args.output / f"S{n}.wav"
        sf.write(path, samples, rate, subtype="PCM_16")
        info = sf.info(path)
        if info.channels != 1 or info.samplerate != rate:
            raise ValueError(f"Unexpected channel count or sample rate in {path}")
        rows.append(
            dict(
                scene=f"S{n}",
                seconds=round(info.duration, 3),
                target_seconds=target,
                wav_sha256=digest(path),
                text_sha256=digest(source),
            )
        )
        print(f"S{n}: {info.duration:.2f} s / {target} s", flush=True)
    report = {
        "model_release": RELEASE,
        "model_sha256": digest(model),
        "voices_sha256": digest(voices),
        "voice": "af_heart",
        "sample_rate": 24000,
        "speed": args.speed,
        "scenes": rows,
    }
    (args.output / "durations.json").write_text(json.dumps(report, indent=2) + "\n")
    if any(r["seconds"] > r["target_seconds"] * 1.1 for r in rows):
        raise SystemExit(
            "Shorten or re-time the scenes exceeding 110% of their target duration"
        )


if __name__ == "__main__":
    main()
