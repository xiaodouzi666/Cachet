"""Cut idle intervals from genuine recordings and place their marked screenshots on the output timeline."""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

FPS = 25
# Appendix B's scene timings, in seconds. Idle waits are cut; readable evidence frames fill the cut.
SCENE_SECONDS = dict(
    zip((f"S{i}" for i in range(1, 9)), (14, 18, 23, 25, 25, 20, 25, 20))
)
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
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
        return digest.hexdigest()


def video_info(path):
    data = json.loads(
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
    if data["streams"] != [{"width": 1920, "height": 1080}]:
        raise ValueError(f"Expected a 1920×1080 recording: {path.name}")
    return float(data["format"]["duration"])


def sources_for(scene):
    if scene == "S4":
        return ["regular", "decision"]
    if scene == "S7":
        return ["soak"]
    return ["regular", "soak"] if scene in ("S5", "S6") else ["regular"]


def event_groups(manifest, scene):
    groups = []
    for kind in sources_for(scene):
        source_scene = "S5" if kind == "decision" else scene
        source_kind = "regular" if kind == "decision" else kind
        record = manifest["segments"][f"{source_scene}-{source_kind}"]
        if record["status"] != "captured" or not record["events"]:
            raise ValueError(f"Incomplete source {scene}-{kind}")
        prefix = source_scene if source_kind == "regular" else source_scene + "-soak"
        chosen = record["events"]
        if source_scene == "S5" and source_kind == "regular":
            chosen = [
                p
                for p in chosen
                if (p["label"] == "live-buy-decision") == (kind == "decision")
            ]
        if not chosen:
            raise ValueError(f"Missing required visual evidence in {prefix}")
        groups.append((kind, prefix, record, chosen))
    return groups


def compose(raw, dest, manifest, scene, tmp):
    chunks, marks, segments, files = [], [], {}, {}
    frames = 0
    common = [
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
        "29",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
    ]
    groups = event_groups(manifest, scene)
    count = sum(len(events) for _, _, _, events in groups)
    per_event, extra = divmod(SCENE_SECONDS[scene] * FPS, count)
    if per_event < 5:
        raise ValueError("Too many events for this scene's editing duration")
    for kind, prefix, record, chosen in groups:
        source_dir = (raw / record.get("sourceDir", ".")).resolve()
        if not source_dir.is_relative_to(raw):
            raise ValueError("Evidence directory escapes the sealed take")
        source = source_dir / (prefix + ".webm")
        duration = video_info(source)
        source_frames = max(1, math.floor(duration * FPS))
        source_id = sha(source)
        segments[kind] = {
            "source_video_sha256": source_id,
            "source_duration_ms": round(duration * 1000),
            "evidence": record.get("evidence", {}),
        }
        last_ms = -1
        previous_frame = 0
        for point in chosen:
            label = point["label"]
            if (
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", label)
                or point["ms"] < last_ms
            ):
                raise ValueError(f"Invalid evidence mark in {prefix}")
            last_ms = point["ms"]
            still = source_dir / (prefix + "-" + label + ".png")
            canonical = "soak-" + label if kind == "soak" else label
            name = scene + "-" + canonical + ".png"
            if name in files:
                raise ValueError(f"Duplicate mark {name}")
            end = max(0, min(round(last_ms * FPS / 1000), source_frames))
            # A synchronized request has a real common UTC time, but no Playwright still;
            # capture the corresponding frame of this camera, never the other angle.
            if not still.exists() and point.get("actionKey") and point.get("utc"):
                still = tmp / (canonical + "-source.png")
                encode(
                    [
                        "-ss",
                        f"{min(end, max(source_frames-2,0))/FPS:.3f}",
                        "-i",
                        source,
                        "-frames:v",
                        "1",
                        still,
                    ]
                )
            if not still.exists():
                raise ValueError(f"Missing genuine screenshot for {prefix}/{label}")
            source_still = sha(still)
            normalized = tmp / name
            encode(
                [
                    "-i",
                    still,
                    "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-frames:v",
                    "1",
                    normalized,
                ]
            )
            still = normalized
            files[name] = still
            allotment = per_event + (len(marks) < extra)
            popup = label == "sandbox-wallet-open"
            maximum_motion = (
                min(36, allotment // 2) if previous_frame and not popup else 0
            )
            start = max(previous_frame, end - maximum_motion)
            motion = end - start
            if motion:
                out = tmp / f"{len(chunks):03d}.webm"
                encode(
                    [
                        "-ss",
                        f"{start/FPS:.3f}",
                        "-i",
                        source,
                        "-vf",
                        "fps=25,tpad=stop_mode=clone:stop_duration=1",
                        "-frames:v",
                        motion,
                        *common,
                        out,
                    ]
                )
                chunks.append(out)
                frames += motion
            previous_frame = end
            hold = allotment - motion
            p = tmp / f"{len(chunks):03d}.webm"
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
                    hold,
                    *common,
                    p,
                ]
            )
            chunks.append(p)
            mark = {
                "label": canonical,
                # First stable frame of this recorded event after the preceding motion.
                "ms": round((frames + 1) * 1000 / FPS),
                "source": kind,
                "raw_ms": last_ms,
                "motion_ms": round(motion * 1000 / FPS),
                "hold_ms": round(hold * 1000 / FPS),
                "screenshot_sha256": sha(still),
                "source_screenshot_sha256": source_still,
            }
            for key in ("utc", "actionKey"):
                if key in point:
                    mark[key] = point[key]
            marks.append(mark)
            frames += hold
    playlist = tmp / "clips.ffconcat"
    playlist.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{c.name}'\n" for c in chunks)
    )
    out = tmp / (scene + ".webm")
    encode(["-f", "concat", "-safe", "1", "-i", playlist, "-c", "copy", out])
    final_ms = round(video_info(out) * 1000)
    if abs(final_ms - frames * 1000 / FPS) > 100 or marks[-1]["ms"] >= final_ms:
        raise ValueError(f"Encoded media clock differs from {scene} timeline")
    for name, still in files.items():
        shutil.copyfile(still, dest / name)
    out.replace(dest / out.name)
    return {
        "status": "captured",
        "duration_ms": final_ms,
        "events": marks,
        "segments": segments,
        "video_sha256": sha(dest / out.name),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--scenes", default=",".join(SCENES))
    args = parser.parse_args()
    raw, dest = args.raw.resolve(), args.destination.resolve()
    if raw == dest:
        parser.error(
            "Keep sealed raw evidence and edited material in different directories"
        )
    manifest = json.loads((raw / "segments.json").read_text())
    dest.mkdir(parents=True, exist_ok=True)
    result = {k: manifest[k] for k in ("origin", "rehearsal", "session", "camera")}
    result["regular_wallet_cleanup"] = manifest.get("cleanup", {})
    result["scenes"] = {}
    for scene in args.scenes.split(","):
        if scene not in SCENES:
            parser.error("Unknown scene")
        if not all(
            manifest["segments"]
            .get("S5-regular" if kind == "decision" else f"{scene}-{kind}", {})
            .get("status")
            == "captured"
            for kind in sources_for(scene)
        ):
            result["scenes"][scene] = {"status": "incomplete", "events": []}
            continue
        with TemporaryDirectory(dir=dest) as d:
            result["scenes"][scene] = compose(raw, dest, manifest, scene, Path(d))
    tmp = dest / "timeline.json.tmp"
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    tmp.replace(dest / "timeline.json")


if __name__ == "__main__":
    main()
