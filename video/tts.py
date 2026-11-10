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


def measured_text(take):
    data = json.loads((take / "timeline.json").read_text())

    def segment(scene, kind):
        return data["scenes"][scene]["segments"][kind]["evidence"]

    band = segment("S2", "regular")["band"]
    if not (band["prefix_len"] == 26000 and 0 < band["warm_ttft_ms_p50"] < band["cold_ttft_ms_p50"]):
        raise ValueError("The spoken benchmark needs the observed 26,000-token warm and cold figure.")
    ratio = segment("S3", "regular")["minimumProbeRatio"]
    q = segment("S4", "regular")["quote"]
    if (
        not 0.95 <= ratio <= 1
        or q["decision"] != "buy"
        or not 0 < q["buy_micros"] < q["compute_micros"]
    ):
        raise ValueError(
            "The narration needs this take's verified probes and Buy quote."
        )
    caps = segment("S5", "soak")["thresholdCaptures"]
    end = segment("S6", "soak")
    if (
        len(caps) < 2
        or len({c["id"] for c in caps}) < 2
        or any(c["amount_cents"] < 200 for c in caps)
    ):
        raise ValueError(
            "The narration needs two distinct measured threshold captures."
        )
    if (
        end.get("action") in {"read-only rehearsal", "observed closed mandate"}
        or end["releasedMicros"] < 0
    ):
        raise ValueError("The measured release is missing.")
    text = {}

    def source(n):
        return (ROOT / f"video/narration/S{n}.txt").read_text().strip()

    text[2] = (
        "On this deployment a warm twenty-six-thousand-token prefix starts answering in "
        f"{round(band['warm_ttft_ms_p50'])} milliseconds. Cold, it takes "
        f"{band['cold_ttft_ms_p50']/1000:.1f} seconds. Cachet lets developers buy "
        "that warm context and pays the maintainers through PayPal."
    )
    text[3] = source(3).replace(
        "The live probe verifies the cache hits.",
        f"The live probe verifies a hit rate of at least {int(ratio*1000)/10:g} percent.",
    )
    saving = 100 * (q["compute_micros"] - q["buy_micros"]) / q["compute_micros"]
    text[4] = source(4).replace(
        "It chooses Buy and explains the price difference before the coding work begins.",
        f"It chooses Buy, about {round(saving)} percent cheaper at the quoted cold fraction.",
    )
    fees = [int(c["fee_cents"]) for c in caps[:2]]
    clause = (
        f"{fees[0]} cents each"
        if fees[0] == fees[1]
        else f"{fees[0]} and {fees[1]} cents respectively"
    )
    text[5] = (
        "Each turn shows its measured cache hits, first-token latency and charge. Small charges on this long-running mandate accumulate into two-dollar captures. PayPal charged "
        + clause
        + " in fees on these two captures."
    )
    if end["releasedMicros"] % 10000:
        raise ValueError("Release did not resolve to whole provider cents.")
    cents = end["releasedMicros"] // 10000
    amount = (
        f"{cents} cents"
        if cents < 100
        else f"{cents//100} dollars and {cents%100} cents"
    )
    last = (
        "writes off the sub-dollar remainder"
        if end["forgivenMicros"] > 0
        else "finalizes the metered balance"
    )
    text[6] = (
        f"Cold shared tokens cost nothing. The judge injects delays and audits the warm calls. PayPal confirms the refund. Closing the long-running mandate {last} and releases {amount}."
    )
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=ROOT / ".cache/kokoro")
    parser.add_argument("--output", type=Path, default=ROOT / "video/out/narration")
    parser.add_argument(
        "--from-take",
        type=Path,
        help="Bind spoken probe percentage, savings, fees and release to edited camera evidence.",
    )
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
    replacements = measured_text(args.from_take) if args.from_take else {}
    for n, target in enumerate(TARGETS, start=1):
        source = ROOT / f"video/narration/S{n}.txt"
        text = (replacements.get(n) or source.read_text().strip()) + "\n"
        spoken = args.output / f"S{n}.txt"
        spoken.write_text(text)
        samples, rate = synthesizer.create(
            text.strip(), voice="af_heart", speed=args.speed, lang="en-us"
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
                text_sha256=digest(spoken),
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
        "take_timeline_sha256": (
            digest(args.from_take / "timeline.json") if args.from_take else None
        ),
        "scenes": rows,
    }
    (args.output / "durations.json").write_text(json.dumps(report, indent=2) + "\n")
    if any(r["seconds"] > r["target_seconds"] * 1.1 for r in rows):
        raise SystemExit(
            "Shorten or re-time the scenes exceeding 110% of their target duration"
        )


if __name__ == "__main__":
    main()
