# Record and edit the demo

The recorder captures the running Cachet UI at 1920×1080. Each scene waits for observable browser or API events and writes a WebM, evidence PNGs and a timestamped timeline under `video/out/`. Production recordings require the HTTPS judge site and working sandbox accounts. A rehearsal can use the loopback deployment.

From the repository root:

```sh
npm ci --prefix frontend
(cd frontend && npx playwright install chromium)
CACHET_WEB_URL=http://127.0.0.1:3000 node video/record_scenes.ts S1,S2,S8 --rehearsal
```

S1 identifies the historical GPU trace used in its Prefix Stack. S2 shows the measured five-band benchmark. S8 shows the architecture and provider APIs. S3–S7 require actual receiver configuration, buyer authorization, real metering and provider readbacks. The fresh publishing workspace and the accumulated payout workspace serve different scenes. Set `CACHET_S7_SEED_JOURNAL` to the operator's private seed journal under `logs/` for the funded payout scene. Use the recorder's screenshots and marked event times to inspect the result before editing.

## English voiceover

Install `kokoro-onnx==0.5.0` and `soundfile` in a separate Python 3.11 environment. Download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the upstream release into `.cache/kokoro/`. The checked recording used voice `af_heart` and the following SHA-256 hashes. [ONNX release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) · [Kokoro-82M model and license](https://huggingface.co/hexgrad/Kokoro-82M)

| File | SHA-256 |
|---|---|
| kokoro-v1.0.onnx | `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5` |
| voices-v1.0.bin | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

Run `python video/tts.py` inside that environment. The program writes eight mono 24 kHz WAVs and `durations.json` containing the source hashes, waveform hashes and measured durations. Regenerate audio after changing the corresponding narration text.

## Production takes and handoff

Export `CACHET_WEB_URL` with the real HTTPS deployment origin. Run `node video/record_scenes.ts` twice. Each run prints its take directory. With FFmpeg and FFprobe on the path, run `python video/package.py FIRST_TAKE SECOND_TAKE --output video/out/video_raw_1112`, substituting the two actual directories. The output directory contains both 1080p takes, key frames, a combined timeline, 24 kHz narration, the benchmark plot, and the GPU buy/compute comparison. The 300 MB limit applies to the uncompressed files.

Use `python video/edit.py TAKE --output video/out/cachet.mp4` for a first cut from a completed production take, or add `--preview` for a rehearsal. The edit carries selectable English captions, AAC audio and a 1080p H.264 stream. The final video and the public upload still need review against the live product and the event rules.
