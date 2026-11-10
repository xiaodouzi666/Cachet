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


def pixels(file, position=None):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "2"]
    if position is not None:
        args.extend(["-ss", f"{position/1000:.3f}"])
    return subprocess.check_output(
        [
            *args,
            "-i",
            str(file),
            "-vf",
            "scale=160:90",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
    )


def inspect_take(directory, *, rehearsal=False, partial=False):
    from compose_take import sha, video_info

    timeline_path = directory / "timeline.json"
    data = json.loads(timeline_path.read_text())
    origin = urlparse(data["origin"])
    if (
        data.get("rehearsal") is not rehearsal
        or not origin.hostname
        or (
            not rehearsal
            and (
                origin.scheme != "https"
                or origin.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError(
            "The timeline origin or rehearsal label differs from the requested handoff"
        )
    contents = {"timeline.json": timeline_path}
    for scene in SCENES:
        entry = data["scenes"].get(scene, {})
        if entry.get("status") != "captured" or not entry.get("events"):
            if partial:
                continue
            raise ValueError(f"Missing complete {scene} in {directory}")
        video = directory / (scene + ".webm")
        duration = round(video_info(video) * 1000)
        if "video_sha256" in entry and (
            sha(video) != entry["video_sha256"]
            or abs(duration - entry["duration_ms"]) > 50
        ):
            raise ValueError(f"{scene} differs from its edited timeline")
        contents[video.name] = video
        previous = -1
        for event in entry["events"]:
            name = f"{scene}-{event['label']}.png"
            frame = directory / name
            if (
                Path(name).name != name
                or not frame.is_file()
                or not previous <= event["ms"] < duration
            ):
                raise ValueError(f"Invalid timeline evidence for {scene}")
            previous = event["ms"]
            if "video_sha256" in entry:
                with frame.open("rb") as png:
                    header = png.read(24)
                if (
                    header[:8] != b"\x89PNG\r\n\x1a\n"
                    or struct.unpack(">II", header[16:24]) != (1920, 1080)
                    or sha(frame) != event["screenshot_sha256"]
                ):
                    raise ValueError(f"Invalid 1080p evidence PNG {name}")
                shot, decoded = pixels(frame), pixels(video, event["ms"])
                if (
                    len(shot) != 160 * 90 * 3
                    or len(decoded) != len(shot)
                    or sum(abs(a - b) for a, b in zip(shot, decoded)) / len(shot) > 6
                ):
                    raise ValueError(
                        f"Decoded video differs from its screenshot at {scene}/{event['ms']} ms"
                    )
            contents[name] = frame
    return data["origin"], contents


def verify_pair(takes):
    a, b = takes
    if (
        a.get("camera") != 1
        or b.get("camera") != 2
        or not a.get("session")
        or a["session"] != b.get("session")
    ):
        raise ValueError(
            "Expected the two original cameras of a single recording session"
        )
    for scene in SCENES:
        each = [t["scenes"][scene] for t in takes]
        if any(s.get("status") != "captured" for s in each):
            raise ValueError(f"Missing scene {scene} in the camera pair")
        if (
            scene in {"S4", "S5", "S6", "S7"}
            and each[0]["video_sha256"] == each[1]["video_sha256"]
        ):
            raise ValueError(f"Duplicate financial footage for {scene}")
    cash = []
    for t in takes:
        components = t["scenes"]

        def evidence(scene, part):
            return components[scene]["segments"][part]["evidence"]

        publish = evidence("S3", "regular")
        approval = evidence("S4", "regular")
        task = evidence("S5", "regular")
        ordinary = evidence("S6", "regular")
        cleanup = t["regular_wallet_cleanup"]
        if not (
            publish["workspaceId"] == task["workspaceId"] == ordinary["workspaceId"]
            and approval["mandateId"] == task["mandateId"] == ordinary["mandateId"]
            and task["runId"] == evidence("S4", "decision")["runId"]
            and len(set(task["calls"])) == 10
            and cleanup["verified"]
            and approval["authorizationId"] == cleanup["receipt"]["authorization_id"]
            and approval["orderId"] == cleanup["receipt"]["order_id"]
        ):
            raise ValueError(
                "The regular camera did not follow and release its original authorized wallet"
            )
        parts = [evidence(scene, "soak") for scene in ("S5", "S6", "S7")]
        if (
            len({v["workspaceId"] for v in parts}) != 1
            or len({v["mandateId"] for v in parts}) != 1
            or any(v["camera"] != t["camera"] for v in parts)
        ):
            raise ValueError(
                "The soak segments do not share their original workspace and mandate"
            )
        if (
            parts[0]["workspaceId"] == publish["workspaceId"]
            or parts[0]["mandateId"] == approval["mandateId"]
        ):
            raise ValueError("The regular wallet and soak must use separate mandates")
        if (
            len({c["id"] for c in parts[0]["thresholdCaptures"]}) < 2
            or len(parts[2]["batch"]["items"]) != 2
        ):
            raise ValueError("The two captures and original payout are required")
        signed = []
        for scene, label in (
            ("S6", "soak-original-close-submitted"),
            ("S7", "soak-original-payout-submitted"),
        ):
            events = [ev for ev in components[scene]["events"] if ev["label"] == label]
            if (
                len(events) != 1
                or not events[0].get("actionKey")
                or not events[0].get("utc")
            ):
                raise ValueError(
                    f"No filmed original transaction submission in {scene}"
                )
            signed.append((events[0]["actionKey"], events[0]["utc"]))
        cash.append(
            (
                publish["workspaceId"],
                approval["authorizationId"],
                parts[0]["mandateId"],
                parts[2]["batch"]["id"],
                signed,
            )
        )
    if (
        cash[0][0] == cash[1][0]
        or cash[0][1] == cash[1][1]
        or cash[0][2:] != cash[1][2:]
    ):
        raise ValueError(
            "Regular sessions must be independent and both soak cameras must observe the same two actions"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("takes", type=Path, nargs=2)
    parser.add_argument("--narration", type=Path, default=ROOT / "video/out/narration")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="Package explicitly labelled rehearsal media.",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Create an explicitly incomplete handoff with its missing-scene list.",
    )
    parser.add_argument(
        "--contrast", type=Path, default=ROOT / "bench/results/contrast.json"
    )
    args = parser.parse_args()
    if args.takes[0].resolve() == args.takes[1].resolve():
        parser.error("Two separate takes are required")
    source = None
    timelines = {}
    files = {}
    full_takes = []
    for index, directory in enumerate(args.takes, start=1):
        origin, contents = inspect_take(
            directory, rehearsal=args.rehearsal, partial=args.partial
        )
        full_takes.append(json.loads(contents["timeline.json"].read_text()))
        if source and source != origin:
            raise ValueError("Both takes must show the same site")
        source = origin
        timelines[f"take-{index}"] = json.loads(contents["timeline.json"].read_text())[
            "scenes"
        ]
        files.update({f"take-{index}/{k}": v for k, v in contents.items()})
    pair_error = None
    try:
        verify_pair(full_takes)
    except (KeyError, ValueError) as exc:
        if not args.partial:
            raise
        pair_error = str(exc)
    durations = args.narration / "durations.json"
    files["narration/durations.json"] = durations
    voice = json.loads(durations.read_text())
    if not args.partial and not args.rehearsal:
        accepted_timelines = {
            hashlib.sha256((directory / "timeline.json").read_bytes()).hexdigest()
            for directory in args.takes
        }
        if voice.get("take_timeline_sha256") not in accepted_timelines:
            raise ValueError(
                "Generate the final voiceover from the measured figures of one of these takes"
            )
    rows = voice["scenes"]
    if [r["scene"] for r in rows] != SCENES:
        raise ValueError("Expected one timed narration for every scene")
    for row in rows:
        name = row["scene"]
        wav = args.narration / f"{name}.wav"
        script = args.narration / f"{name}.txt"
        if not script.is_file():
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
    if header[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", header[16:24]) != (
        1920,
        1080,
    ):
        raise ValueError("Expected a 1920 by 1080 benchmark PNG")
    for figure in sorted(chart.parent.glob("*.png")):
        files[f"bench/plots/{figure.name}"] = figure
    contrast = args.contrast
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
        manifest.write_text(
            json.dumps(
                {
                    "origin": source,
                    "rehearsal": args.rehearsal,
                    "status": "partial" if args.partial else "complete",
                    "pair_check": pair_error or "verified",
                    "missing": {
                        k: [
                            n
                            for n in SCENES
                            if v.get(n, {}).get("status") != "captured"
                        ]
                        for k, v in timelines.items()
                    },
                    "takes": timelines,
                },
                indent=2,
            )
            + "\n"
        )
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
