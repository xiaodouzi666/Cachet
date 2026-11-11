"""Offline encoder/decoder regression. No application, provider, model, or financial journal."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from compose_take import encode, register_frame
from package import inspect_take, verify_pair


class EditedMilestones(unittest.TestCase):
    def test_joined_observation_registers_its_real_frame_after_a_clock_shift(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stream = root / "source.webm"
            figure = root / "observed.png"
            encode([
                "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6",
                "-c:v", "libvpx", "-deadline", "realtime", "-threads", "2", stream,
            ])
            encode(["-ss", "4.40", "-i", stream, "-frames:v", "1", figure])
            frame = register_frame(stream, 1900, figure, 150, earliest=80, joined=True)
            self.assertLessEqual(abs(frame - 110), 1)

    def test_long_wait_popup_camera_action_and_tamper(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "private"
            dest = root / "cut"
            raw.mkdir()
            # Genuine continuous camera movement on either side of a long documented frozen scene.
            encode(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=1920x1080:r=25:d=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=navy:s=1920x1080:r=25:d=20",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=1920x1080:r=25:d=7",
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
                    "-map",
                    "[out]",
                    "-an",
                    "-threads",
                    "3",
                    "-c:v",
                    "libvpx",
                    "-deadline",
                    "realtime",
                    "-cpu-used",
                    "8",
                    "-b:v",
                    "650k",
                    raw / "S4.webm",
                ]
            )
            for f in ("S5", "S5-soak"):
                shutil.copyfile(raw / "S4.webm", raw / (f + ".webm"))
            from compose_take import shot

            # Every non-popup proof comes from its actual original video frame. Only the
            # popup is an external tab, represented by a separate marked screenshot.
            encode(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=1000x700",
                    "-frames:v",
                    "1",
                    raw / "S4-sandbox-wallet-open.png",
                ]
            )
            for source, label, ms in (
                ("S4", "sandbox-authorized", 27800),
                ("S5", "live-buy-decision", 2000),
                ("S5", "first-metered-turn", 6100),
                ("S5-soak", "table", 7700),
            ):
                shot(
                    raw / (source + ".webm"), ms, raw / (source + "-" + label + ".png")
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
                            {"label": "sandbox-authorized", "ms": 27800},
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
            data["segments"]["S4-regular"]["waits"] = [
                {"start_ms": 3000, "end_ms": 23000, "reason": "server-retry-after"}
            ]
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
            regular = s4["segments"]["regular"]
            self.assertEqual(regular["inserted_popup_ms"], 2000)
            self.assertTrue(regular["removed_waits"])
            self.assertTrue(
                any(a == 0 and b >= 2800 for a, b in regular["kept_source_ranges_ms"])
            )
            self.assertTrue(
                any(
                    a <= 24000 and b >= 29500
                    for a, b in regular["kept_source_ranges_ms"]
                )
            )
            self.assertLess(
                sum(b - a for a, b in regular["kept_source_ranges_ms"]), 18000
            )
            reflected = (
                sum(
                    b - a
                    for src in s4["segments"].values()
                    for a, b in src["kept_source_ranges_ms"]
                )
                + 2000
            )
            self.assertLessEqual(abs(s4["duration_ms"] - reflected), 120)
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
