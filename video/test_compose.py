"""Offline encoder/decoder regression. No application, provider, model, or financial journal."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compose_take import encode
from package import inspect_take, verify_pair


class EditedMilestones(unittest.TestCase):
    def test_long_wait_popup_camera_action_and_tamper(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "private"
            dest = root / "cut"
            raw.mkdir()
            # Deliberately blank neutral source for checking the editing clock; the popup marker is a red test card.
            encode(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=navy:s=1920x1080:r=25:d=12",
                    "-an",
                    "-threads",
                    "2",
                    "-c:v",
                    "libvpx-vp9",
                    "-deadline",
                    "realtime",
                    "-cpu-used",
                    "8",
                    raw / "S4.webm",
                ]
            )
            for f in ("S5", "S5-soak"):
                shutil.copyfile(raw / "S4.webm", raw / (f + ".webm"))
            for image, color, size in (
                ("S4-sandbox-wallet-open", "red", "1000x700"),
                ("S4-sandbox-authorized", "teal", "1920x1080"),
                ("S5-live-buy-decision", "lime", "1920x1080"),
                ("S5-first-metered-turn", "orange", "1920x1080"),
                ("S5-soak-table", "purple", "1920x1080"),
            ):
                encode(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=" + color + ":s=" + size,
                        "-frames:v",
                        "1",
                        raw / (image + ".png"),
                    ]
                )

            def segment(points):
                return {
                    "status": "captured",
                    "events": points,
                    "evidence": {"source": "offline media fixture"},
                }

            data = {
                "camera": 1,
                "session": "offline-geometry",
                "origin": "http://localhost",
                "rehearsal": True,
                "segments": {
                    "S4-regular": segment(
                        [
                            {"label": "sandbox-wallet-open", "ms": 1200},
                            {"label": "sandbox-authorized", "ms": 10800},
                        ]
                    ),
                    "S5-regular": segment(
                        [
                            {"label": "live-buy-decision", "ms": 2000},
                            {"label": "first-metered-turn", "ms": 6100},
                        ]
                    ),
                    "S5-soak": segment(
                        [
                            {
                                "label": "offline-camera-event",
                                "ms": 3500,
                                "utc": "fixture",
                                "actionKey": "fixture",
                            },
                            {"label": "table", "ms": 7700},
                        ]
                    ),
                },
            }
            (raw / "segments.json").write_text(json.dumps(data))
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("compose_take.py")),
                    str(raw),
                    str(dest),
                    "--scenes",
                    "S4,S5",
                ],
                check=True,
            )
            out = json.loads((dest / "timeline.json").read_bytes())
            s4 = out["scenes"]["S4"]
            self.assertLess(s4["duration_ms"], 9000)
            self.assertEqual(
                [p["label"] for p in s4["events"]],
                ["sandbox-wallet-open", "sandbox-authorized", "live-buy-decision"],
            )
            s5 = out["scenes"]["S5"]
            self.assertEqual(
                [p["label"] for p in s5["events"]],
                ["first-metered-turn", "soak-offline-camera-event", "soak-table"],
            )
            self.assertEqual(s5["events"][1]["actionKey"], "fixture")
            self.assertEqual(s5["events"][1]["raw_ms"], 3500)
            inspect_take(dest, rehearsal=True, partial=True)
            with self.assertRaises(ValueError):
                inspect_take(dest)
            with self.assertRaises(ValueError):
                verify_pair([out, out])
            s4["events"][0]["ms"] = s4["events"][1]["ms"]
            (dest / "timeline.json").write_text(json.dumps(out))
            with self.assertRaisesRegex(ValueError, "Decoded video differs"):
                inspect_take(dest, rehearsal=True, partial=True)


if __name__ == "__main__":
    unittest.main()
