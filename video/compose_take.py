"""Cut documented waiting or identical frozen frames out of genuine browser footage."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

FPS = 25
SCENES = tuple(f"S{i}" for i in range(1, 9))


def encode(args):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-filter_threads",
            "2",
            *map(str, args),
        ],
        check=True,
    )


def sha(path):
    with path.open("rb") as handle:
        value = hashlib.sha256()
        for block in iter(lambda: handle.read(1 << 20), b""):
            value.update(block)
        return value.hexdigest()


def video_info(path):
    result = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    if result["streams"] != [{"width": 1920, "height": 1080}]:
        raise ValueError(f"Expected 1920×1080 source: {path.name}")
    return float(result["format"]["duration"])


def sources_for(scene):
    if scene == "S4":
        return ["regular", "decision"]
    if scene == "S7":
        return ["soak"]
    return ["regular", "soak"] if scene in ("S5", "S6") else ["regular"]


def subtract(spans, cuts):
    """Subtract half-open frame intervals, preserving every other frame."""
    for left, right in sorted(cuts):
        retained = []
        for a, b in spans:
            if b <= left or a >= right:
                retained.append((a, b))
            else:
                if a < left:
                    retained.append((a, left))
                if b > right:
                    retained.append((right, b))
        spans = retained
    return spans


def frozen(path, frames):
    # Zero noise tolerance is deliberate. Moving spinners, cursor movement and small numeric
    # updates remain footage unless the recorder explicitly identified a wait.
    process = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-i",
            str(path),
            "-an",
            "-vf",
            "freezedetect=n=0:d=5",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    start = None
    windows = []
    for which, seconds in re.findall(
        r"lavfi\.freezedetect\.freeze_(start|end): ([0-9.eE+-]+)",
        process.stderr.decode(),
    ):
        frame = round(float(seconds) * FPS)
        if which == "start":
            start = frame
        elif start is not None:
            windows.append((start, frame, "identical-frames"))
            start = None
    if start is not None:
        windows.append((start, frames, "identical-frames"))
    return windows


def shot(raw, at_ms, path):
    encode(["-ss", f"{at_ms/1000:.4f}", "-i", raw, "-frames:v", "1", path])
    if not path.is_file():
        raise ValueError("No camera frame at the marked time")


def register_frame(source, stamp, still, frames):
    """Locate the original browser screenshot on the camera's media clock."""
    predicted = min(frames - 1, max(0, round(stamp * FPS / 1000)))
    begin = max(0, predicted - FPS)
    end = min(frames, predicted + FPS + 1)
    filter_ = "scale=96:54,format=gray"
    setup = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "2",
        "-filter_threads",
        "1",
    ]
    target = subprocess.check_output(
        [
            *setup,
            "-i",
            str(still),
            "-vf",
            filter_,
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    raw = subprocess.check_output(
        [
            *setup,
            "-ss",
            f"{begin/FPS:.4f}",
            "-i",
            str(source),
            "-vf",
            "fps=25," + filter_,
            "-frames:v",
            str(end - begin),
            "-f",
            "rawvideo",
            "-",
        ]
    )
    size = 96 * 54
    if len(target) != size or len(raw) < size:
        raise ValueError(
            "Cannot align a marked browser screenshot with the original video"
        )

    def deviation(index):
        frame = raw[index * size : (index + 1) * size]
        return (
            sum(abs(x - y) for x, y in zip(target, frame)),
            abs(begin + index - predicted),
        )

    index = min(range(len(raw) // size), key=deviation)
    if deviation(index)[0] / size > 20:
        raise ValueError(
            f"The recorded screenshot {still.name} could not be located on its original camera"
        )
    return begin + index


def join_motion(source, ranges, parts, options, tmp):
    if not ranges:
        return 0
    first, last = ranges[0][0], ranges[-1][1]
    expression = "+".join(f"between(n,{a-first},{b-first-1})" for a, b in ranges)
    length = sum(b - a for a, b in ranges)
    clip = tmp / f"{len(parts):03d}.webm"
    encode(
        [
            "-ss",
            f"{first/FPS:.4f}",
            "-t",
            f"{(last-first)/FPS:.4f}",
            "-i",
            source,
            "-vf",
            f"fps=25,select='{expression}',setpts=N/(25*TB)",
            "-frames:v",
            length,
            *options,
            clip,
        ]
    )
    count = int(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_entries",
                "stream=nb_read_packets",
                "-of",
                "csv=p=0",
                str(clip),
            ]
        )
    )
    if abs(count - length) > 1:
        raise ValueError(
            f"Retained frame count differs from edit map ({count} vs {length})"
        )
    parts.append(clip)
    return count


def compose(raw, dest, manifest, scene, tmp, crf, kinds):
    options = [
        "-an",
        "-c:v",
        "libvpx-vp9",
        "-row-mt",
        "1",
        "-threads",
        "3",
        "-deadline",
        "realtime",
        "-cpu-used",
        "6",
        "-b:v",
        "0",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
    ]
    parts = []
    marks = []
    sources = {}
    output_frame = 0
    for kind in kinds:
        source_scene = "S5" if kind == "decision" else scene
        source_kind = "regular" if kind == "decision" else kind
        entry = manifest["segments"][source_scene + "-" + source_kind]
        source_dir = (raw / entry.get("sourceDir", ".")).resolve()
        if not source_dir.is_relative_to(raw):
            raise ValueError("Evidence directory escapes the take")
        prefix = source_scene + ("-soak" if source_kind == "soak" else "")
        source = source_dir / (prefix + ".webm")
        duration = video_info(source)
        frames = max(1, round(duration * FPS))
        points = []
        previous_ms = -1
        for event in entry["events"]:
            name = event["label"]
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name)
                or event["ms"] < previous_ms
            ):
                raise ValueError(f"Invalid mark in {prefix}")
            previous_ms = event["ms"]
            still = source_dir / (prefix + "-" + name + ".png")
            popup = name == "sandbox-wallet-open"
            if (not still.exists() and not event.get("actionKey")) or event[
                "ms"
            ] > duration * 1000 + 1500:
                raise ValueError(f"Unverifiable source mark {name}")
            frame = min(frames - 1, max(0, round(event["ms"] * FPS / 1000)))
            if still.exists() and not popup:
                frame = register_frame(source, event["ms"], still, frames)
            points.append((event, frame, still, popup))
        # Browser monotonic time starts just before this page's actual screencast.
        # Anchor unscreenshoted request markers to the closest photographed DOM event.
        # Keep the original UTC and raw_ms for correlating the same request on both cameras.
        anchors = [
            (event["ms"], frame - round(event["ms"] * FPS / 1000), event.get("fragment"))
            for event, frame, still, popup in points
            if still.exists() and not popup
        ]
        if anchors:
            corrected = []
            for event, frame, still, popup in points:
                matching = [a for a in anchors if a[2] == event.get("fragment")]
                if matching and (popup or not still.exists()):
                    offset = min(
                        matching, key=lambda anchor: abs(anchor[0] - event["ms"])
                    )[1]
                    frame = max(
                        0, min(frames - 1, round(event["ms"] * FPS / 1000) + offset)
                    )
                corrected.append((event, frame, still, popup))
            points = corrected
        all_frames = [(0, frames)]
        if source_scene == "S5" and source_kind == "regular":
            decision = next(
                (f for e, f, _, _ in points if e["label"] == "live-buy-decision"), None
            )
            if decision is None:
                raise ValueError(
                    "The S4 decision needs the same wallet's filmed S5 explanation"
                )
            later = [
                f
                for e, f, _, _ in points
                if e["label"] != "live-buy-decision" and f > decision
            ]
            window = (
                max(0, decision - 2 * FPS),
                min(frames, decision + 3 * FPS, min(later) if later else frames),
            )
            if kind == "decision":
                all_frames = [window]
                points = [p for p in points if p[0]["label"] == "live-buy-decision"]
            else:
                # Keep the full start-to-finish regular S5 action for the editor. The
                # short decision excerpt is also available in the previous quote scene.
                points = [p for p in points if p[0]["label"] != "live-buy-decision"]
        if not points:
            raise ValueError(f"No photographed events for {scene}/{kind}")
        # Ensure that each key moment and its lead-in/reaction survive the cut. Never use the
        # time between two screenshots as evidence that a browser was idle.
        protected = [
            (max(0, f - FPS), min(frames, f + FPS + 1)) for _, f, _, _ in points
        ]
        waits = frozen(source, frames)
        waits.extend(
            (
                math.ceil(w["start_ms"] * FPS / 1000),
                math.floor(w["end_ms"] * FPS / 1000),
                w["reason"],
            )
            for w in entry.get("waits", [])
            if w["reason"]
            in {
                "server-retry-after",
                "document-navigation",
                "separate-sandbox-approval-window",
            }
        )
        cuts = []
        removed = []
        for a, b, reason in waits:
            if b - a < 5 * FPS:
                continue
            # Retain entry into and exit from the wait. Display-only scenes get longer bookends.
            pad = 2 * FPS if scene in {"S1", "S2", "S8"} else FPS
            for available_start, available_end in subtract(
                all_frames, protected + cuts
            ):
                left = max(available_start, a + pad)
                right = min(available_end, b - pad)
                if left < right:
                    cuts.append((left, right))
                    removed.append(
                        {"start_ms": left * 40, "end_ms": right * 40, "reason": reason}
                    )
        ranges = subtract(all_frames, cuts)
        popup = next((p for p in points if p[3]), None)
        extra = 0
        if popup:
            _, at, still, _ = popup
            actual_before = join_motion(
                source,
                [(a, min(b, at)) for a, b in ranges if a < at],
                parts,
                options,
                tmp,
            )
            clip = tmp / f"{len(parts):03d}.webm"
            extra = 2 * FPS
            encode(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    FPS,
                    "-i",
                    still,
                    "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-frames:v",
                    extra,
                    *options,
                    clip,
                ]
            )
            parts.append(clip)
            actual_after = join_motion(
                source,
                [(max(a, at), b) for a, b in ranges if b > at],
                parts,
                options,
                tmp,
            )
            length = actual_before + extra + actual_after
        else:
            length = join_motion(source, ranges, parts, options, tmp)
        if not length:
            raise ValueError(f"No usable motion for {scene}/{kind}")
        for event, frame, still, is_popup in points:
            if not any(a <= frame < b for a, b in ranges):
                raise ValueError(f"Cut would remove {event['label']}")
            index = sum(max(0, min(frame, b) - a) for a, b in ranges if a < frame)
            if is_popup:
                index = actual_before + extra // 2
            elif popup and frame >= popup[1]:
                index += extra
            mark = {
                "label": ("soak-" if kind == "soak" else "") + event["label"],
                "ms": 40 * (output_frame + min(index, length - 1)),
                "raw_ms": event["ms"],
                "source_frame_ms": 40 * frame,
                "source": kind,
            }
            if still.exists():
                mark["source_screenshot_sha256"] = sha(still)
            for field in ("utc", "actionKey", "fragment", "fragment_ms", "surface"):
                if field in event:
                    mark[field] = event[field]
            marks.append(mark)
        sources[kind] = {
            "source_video_sha256": sha(source),
            "source_duration_ms": round(duration * 1000),
            "kept_source_ranges_ms": [[a * 40, b * 40] for a, b in ranges],
            "removed_waits": removed,
            "inserted_popup_ms": extra * 40,
            "evidence": entry.get("evidence", {}),
        }
        output_frame += length
    playlist = tmp / "clips.ffconcat"
    playlist.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{p.name}'\n" for p in parts)
    )
    out = tmp / (scene + ".webm")
    encode(["-f", "concat", "-safe", "1", "-i", playlist, "-c", "copy", out])
    ms = round(video_info(out) * 1000)
    if abs(ms - output_frame * 40) > 120:
        raise ValueError("Encoded media clock differs from kept original footage")
    marks.sort(key=lambda e: e["ms"])
    seen = set()
    for mark in marks:
        if mark["label"] in seen or mark["ms"] >= ms:
            raise ValueError("Invalid or duplicated edited milestone")
        seen.add(mark["label"])
        name = scene + "-" + mark["label"] + ".png"
        shot(out, mark["ms"], tmp / name)
        mark["screenshot_sha256"] = sha(tmp / name)
        (tmp / name).replace(dest / name)
    out.replace(dest / out.name)
    return {
        "status": "captured",
        "duration_ms": ms,
        "events": marks,
        "segments": sources,
        "video_sha256": sha(dest / out.name),
    }


def input_digest(raw, manifest, scene, crf, kinds):
    inputs = [sha(Path(__file__)), crf]
    for kind in kinds:
        target = "S5" if kind == "decision" else scene
        component = "regular" if kind == "decision" else kind
        entry = manifest["segments"][target + "-" + component]
        directory = (raw / entry.get("sourceDir", ".")).resolve()
        if not directory.is_relative_to(raw):
            raise ValueError("Source lies outside the sealed camera directory")
        prefix = target + ("-soak" if component == "soak" else "")
        hashes = [sha(directory / (prefix + ".webm"))]
        for mark in entry["events"]:
            file = directory / (prefix + "-" + mark["label"] + ".png")
            if file.is_file():
                hashes.append(sha(file))
        inputs.append([entry, hashes])
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def intact(directory, scene, entry):
    files = [(directory / (scene + ".webm"), entry["video_sha256"])]
    files.extend(
        (directory / (scene + "-" + e["label"] + ".png"), e["screenshot_sha256"])
        for e in entry["events"]
    )
    return all(p.is_file() and sha(p) == digest for p, digest in files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--scenes", default=",".join(SCENES))
    parser.add_argument(
        "--crf",
        type=int,
        default=int(os.environ.get("CACHET_VIDEO_CRF", "31")),
        choices=range(20, 46),
    )
    args = parser.parse_args()
    raw, dest = args.raw.resolve(), args.destination.resolve()
    if raw == dest:
        parser.error(
            "Keep the original source directory separate from the edited handoff"
        )
    manifest = json.loads((raw / "segments.json").read_bytes())
    dest.mkdir(parents=True, exist_ok=True)
    result = {k: manifest[k] for k in ("origin", "rehearsal", "session", "camera")}
    timeline = dest / "timeline.json"
    prior = json.loads(timeline.read_text()) if timeline.exists() else {}
    if prior and any(prior.get(k) != v for k, v in result.items()):
        raise ValueError("Destination already belongs to another recording camera")
    result["regular_wallet_cleanup"] = manifest.get("cleanup", {})
    result["scenes"] = dict(prior.get("scenes", {}))
    for scene in args.scenes.split(","):
        if scene not in SCENES:
            parser.error("Unknown scene")
        expected = sources_for(scene)
        present = [
            kind
            for kind in expected
            if manifest["segments"]
            .get("S5-regular" if kind == "decision" else f"{scene}-{kind}", {})
            .get("status")
            == "captured"
        ]
        if not present:
            result["scenes"][scene] = {
                "status": "incomplete",
                "missing_sources": expected,
                "events": [],
            }
            continue
        signature = input_digest(raw, manifest, scene, args.crf, present)
        previous = result["scenes"].get(scene, {})
        if previous.get("input_digest") == signature and intact(dest, scene, previous):
            continue
        with TemporaryDirectory(dir=dest) as d:
            result["scenes"][scene] = compose(
                raw, dest, manifest, scene, Path(d), args.crf, present
            )
        if present != expected:
            result["scenes"][scene].update(
                status="partial",
                missing_sources=[k for k in expected if k not in present],
            )
        result["scenes"][scene]["input_digest"] = signature
    tmp = dest / "timeline.json.tmp"
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    tmp.replace(dest / "timeline.json")


if __name__ == "__main__":
    main()
