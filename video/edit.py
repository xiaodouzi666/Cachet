"""Edit real captured scenes to timed voiceovers, with selectable English subtitles."""

import argparse
import json
import math
import re
import subprocess
import tempfile
import wave
from pathlib import Path

from package import ROOT, SCENES, inspect_take


def command(args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        check=True,
    )


def timestamp(seconds):
    ms = round(seconds * 1000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{minutes // 60:02}:{minutes % 60:02}:{secs:02},{ms:03}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("take", type=Path)
    parser.add_argument("--narration", type=Path, default=ROOT / "video/out/narration")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Allow an explicitly labeled partial rehearsal edit.",
    )
    args = parser.parse_args()
    timeline = json.loads((args.take / "timeline.json").read_text())
    if not args.preview:
        inspect_take(args.take)
    names = [
        s for s in SCENES if timeline["scenes"].get(s, {}).get("status") == "captured"
    ]
    if not names or (not args.preview and names != SCENES):
        raise SystemExit("All eight captured scenes are required for a production cut")
    title = (
        "Cachet | Local rehearsal"
        if args.preview
        else "Cachet | Warm context, fairly paid"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as d:
        tmp = Path(d)
        clips = []
        captions = []
        offset = 0
        # The official release uses all eight corresponding production screen captures.
        for name in names:
            wav = args.narration / f"{name}.wav"
            with wave.open(str(wav)) as f:
                duration = f.getnframes() / f.getframerate()
            video = args.take / f"{name}.webm"
            clip_duration = float(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=nw=1:nk=1",
                        str(video),
                    ],
                    text=True,
                )
            )
            events = timeline["scenes"][name]["events"]
            if not events:
                raise ValueError("A scene has no real recorded events")
            # Trim initial browser navigation; retain every marked event in original order.
            start = max(0, min(events[0]["ms"] / 1000 - 1.8, clip_duration - 0.6))
            end = min(clip_duration, events[-1]["ms"] / 1000 + 0.6)
            scale = min(1.0, (duration - 0.2) / max(0.05, end - start))
            frames = math.ceil((duration + 0.28) * 30)
            filters = (
                f"trim=start={start}:end={end},setpts=(PTS-STARTPTS)*{scale},"
                f"fps=30,tpad=stop_mode=clone:stop_duration={duration + 1},trim=end_frame={frames},"
                "scale=1920:1080,setsar=1,format=yuv420p"
            )
            clip = tmp / f"{name}.mp4"
            command(
                [
                    "-i",
                    str(video),
                    "-i",
                    str(wav),
                    "-vf",
                    filters,
                    "-af",
                    "apad",
                    "-t",
                    str(frames / 30),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-b:a",
                    "160k",
                    str(clip),
                ]
            )
            clips.append(clip)
            sentences = re.split(
                r"(?<=[.!?])\s+",
                (ROOT / f"video/narration/{name}.txt").read_text().strip(),
            )
            weights = [len(t.split()) for t in sentences]
            current = offset
            for sentence, w in zip(sentences, weights):
                stop = current + duration * w / sum(weights)
                captions.append(
                    f"{len(captions) + 1}\n{timestamp(current)} --> {timestamp(stop)}\n{sentence}\n"
                )
                current = stop
            offset += frames / 30
        if offset > 175:
            raise ValueError("Cut exceeds the 2:55 edit limit")
        listing = tmp / "clips.txt"
        listing.write_text("".join(f"file '{f.name}'\n" for f in clips))
        srt = tmp / "English.srt"
        srt.write_text("\n".join(captions) + "\n")
        output = tmp / "finished.mp4"
        command(
            [
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listing),
                "-i",
                str(srt),
                "-map",
                "0:v",
                "-map",
                "0:a",
                "-map",
                "1:0",
                "-c:v",
                "copy",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=7",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-b:a",
                "160k",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=eng",
                "-disposition:s:0",
                "default",
                "-metadata",
                "title=" + title,
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        output.replace(args.output)
        print(f"{title}: {', '.join(names)}; {offset:.2f} s")


if __name__ == "__main__":
    main()
