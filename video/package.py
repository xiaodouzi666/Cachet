"""Package two verified production takes, final voiceovers and the measured chart for editing."""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SCENES = [f"S{n}" for n in range(1, 9)]


def inspect_take(directory):
    timeline_path = directory / "timeline.json"
    data = json.loads(timeline_path.read_text())
    origin = urlparse(data["origin"])
    if (
        data.get("rehearsal") is not False
        or origin.scheme != "https"
        or not origin.hostname
        or origin.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Final packaging requires the production HTTPS recording")
    contents = {"timeline.json": timeline_path}
    for scene in SCENES:
        entry = data["scenes"].get(scene, {})
        if entry.get("status") != "captured" or not entry.get("events"):
            raise ValueError(
                f"Missing completed production scene {scene} in {directory}"
            )
        video = directory / f"{scene}.webm"
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if json.loads(probe.stdout)["streams"] != [{"width": 1920, "height": 1080}]:
            raise ValueError(f"Expected 1920 by 1080 video for {scene}")
        contents[video.name] = video
        previous = 0
        for event in entry["events"]:
            name = f"{scene}-{event['label']}.png"
            if (
                Path(name).name != name
                or not (directory / name).is_file()
                or event["ms"] < previous
            ):
                raise ValueError(f"Invalid timeline evidence for {scene}")
            previous = event["ms"]
            contents[name] = directory / name
    return data["origin"], contents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("takes", type=Path, nargs=2)
    parser.add_argument("--narration", type=Path, default=ROOT / "video/out/narration")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.takes[0].resolve() == args.takes[1].resolve():
        parser.error("Two separate takes are required")
    source = None
    timelines = {}
    files = {}
    for index, directory in enumerate(args.takes, start=1):
        origin, contents = inspect_take(directory)
        if source and source != origin:
            raise ValueError("Both takes must show the same site")
        source = origin
        timelines[f"take-{index}"] = json.loads(contents["timeline.json"].read_text())["scenes"]
        files.update({f"take-{index}/{k}": v for k, v in contents.items()})
    durations = args.narration / "durations.json"
    files["narration/durations.json"] = durations
    rows = json.loads(durations.read_text())["scenes"]
    if [r["scene"] for r in rows] != SCENES:
        raise ValueError("Expected one timed narration for every scene")
    for row in rows:
        name = row["scene"]
        wav = args.narration / f"{name}.wav"
        script = ROOT / f"video/narration/{name}.txt"
        if (
            hashlib.sha256(wav.read_bytes()).hexdigest() != row["wav_sha256"]
            or hashlib.sha256(script.read_bytes()).hexdigest() != row["text_sha256"]
        ):
            raise ValueError("Regenerate the voiceover after editing its script")
        with wave.open(str(wav)) as stream:
            if (
                stream.getframerate() != 24000
                or stream.getnchannels() != 1
                or stream.getnframes() / 24000 > row["target_seconds"] * 1.1
            ):
                raise ValueError(f"Voiceover outside the target for {name}")
        files[f"narration/{wav.name}"] = wav
        files[f"narration/{script.name}"] = script
    chart = ROOT / "bench/plots/prefix-latency.png"
    with chart.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", header[16:24]) != (1920, 1080):
        raise ValueError("Expected a 1920 by 1080 benchmark PNG")
    files["bench/plots/prefix-latency.png"] = chart
    contrast = ROOT / "bench/results/contrast.json"
    runs = json.loads(contrast.read_text())["runs"]
    if {run["decision"] for run in runs} != {"buy", "compute"} or any(
        not run["turns"] for run in runs
    ):
        raise ValueError("Expected measured buy and compute runs")
    files["contrast.json"] = contrast
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as tmp:
        stage = Path(tmp) / "footage"
        stage.mkdir()
        manifest = stage / "timeline.json"
        manifest.write_text(json.dumps({"origin": source, "takes": timelines}, indent=2) + "\n")
        inputs = {**files, "timeline.json": manifest}
        size = sum(path.stat().st_size for path in inputs.values())
        if size >= 300_000_000:
            raise ValueError(f"Footage totals {size:,} bytes. The limit is 300 MB.")
        if args.output.suffix == ".zip":
            archive = Path(tmp) / "raw.zip"
            with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=4) as output:
                for name, path in sorted(inputs.items()):
                    output.write(path, name)
            with ZipFile(archive) as check:
                if check.testzip():
                    raise ValueError("Archive failed CRC verification")
            archive.replace(args.output)
        else:
            for name, path in files.items():
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
            stage.rename(args.output)
    print(f"Verified {len(inputs)} files, {size:,} bytes in {args.output}")


if __name__ == "__main__":
    main()
