"""Join the two real observations of one submitted transaction during recorder recovery."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from compose_take import sha, video_info


def source_events(directory, scene):
    path = directory / f"{scene}-soak-source-events.json"
    return json.loads(path.read_bytes())


def original_observation(camera, scene, label, key_digest):
    candidates = []
    for path in sorted(camera.glob(f"{scene}-attempt-*/{scene}-soak-source-events.json")):
        directory = path.parent
        record = source_events(directory, scene)
        if record["status"] != "incomplete":
            continue
        matched = [
            event for event in record["events"]
            if event["label"] == label and event.get("utc") and event.get("actionKey")
            and hashlib.sha256(event["actionKey"].encode()).hexdigest() == key_digest
        ]
        if matched:
            journal = directory / f"{scene}-soak-action-events.jsonl"
            observed = [json.loads(line) for line in journal.read_text().splitlines()]
            if len(matched) != 1 or matched[0] not in observed:
                raise ValueError("Submission differs from the durable browser request observation")
            candidates.append((directory, record))
    if len(candidates) != 1:
        raise ValueError("Expected one original browser submission for this camera and transaction")
    return candidates[0]


def join(camera, scene, latest, output, key_digest):
    label = "original-close-submitted" if scene == "S6" else "original-payout-submitted"
    finished = source_events(latest, scene)
    if finished["status"] != "captured" or any(event["label"] == label for event in finished["events"]):
        raise ValueError("Expected a later readback without a second submission")
    original, first = original_observation(camera, scene, label, key_digest)
    files = [original / f"{scene}-soak-incomplete.webm", latest / f"{scene}-soak.webm"]
    durations = [round(video_info(path) * 1000) for path in files]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    joined = output / f"{scene}-soak.webm"
    with TemporaryDirectory(dir=output) as temporary:
        scratch = Path(temporary)
        for index, source in enumerate(files):
            (scratch / f"part-{index}.webm").symlink_to(source)
        manifest = scratch / "list.ffconcat"
        manifest.write_text("ffconcat version 1.0\nfile 'part-0.webm'\nfile 'part-1.webm'\n")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "1",
                        "-i", manifest, "-c", "copy", joined], check=True)
    if abs(round(video_info(joined) * 1000) - sum(durations)) > 150:
        raise ValueError("Joined stream timing differs from the source clips")
    events = []
    later_labels = {event["label"] for event in finished["events"]}
    for index, (directory, record) in enumerate(((original, first), (latest, finished))):
        for recorded in record["events"]:
            source_label = recorded["label"]
            name = "original-" + source_label if index == 0 and source_label in later_labels else source_label
            event = {**recorded, "label": name, "ms": recorded["ms"] + (durations[0] if index else 0),
                     "fragment": index + 1, "fragment_ms": recorded["ms"]}
            image = directory / f"{scene}-soak-{source_label}.png"
            if image.is_file():
                capture = output / f"{scene}-soak-{name}.png"
                shutil.copyfile(image, capture)
                event["screenshot"] = capture.name
            elif not event.get("actionKey"):
                raise ValueError("Missing screenshot for original recorded milestone")
            events.append(event)
    waits = [
        {**wait, "start_ms": wait["start_ms"] + offset, "end_ms": wait["end_ms"] + offset}
        for record, offset in ((first, 0), (finished, durations[0]))
        for wait in record.get("waits", [])
    ]
    proof = {
        "events": events,
        "waits": waits,
        "observationParts": [
            {"phase": phase, "source_sha256": sha(source), "duration_ms": duration}
            for phase, source, duration in zip(("original-submission", "subsequent-readback"), files, durations)
        ],
    }
    (output / f"{scene}-soak-source-events.json").write_text(json.dumps({"status": "captured", **proof}, indent=2) + "\n")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("camera", type=Path)
    parser.add_argument("scene", choices=["S6", "S7"])
    parser.add_argument("latest")
    parser.add_argument("output")
    parser.add_argument("key_digest")
    args = parser.parse_args()
    camera = args.camera.resolve()
    latest = (camera / args.latest).resolve()
    output = (camera / args.output).resolve()
    if latest.parent != camera or output.parent != camera:
        parser.error("Use private directories inside the same camera's source folder")
    print(json.dumps(join(camera, args.scene, latest, output, args.key_digest)))
