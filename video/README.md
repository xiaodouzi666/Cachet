# Record and package the demo

The recorder makes two 1920×1080 takes. Each ordinary walkthrough publishes its own Starlette → FastAPI chain in a fresh judge workspace, authorizes one $5 PayPal sandbox wallet, and keeps that workspace through S5’s ten live turns, the S1 Prefix Stack, the S6 cold call and the three fault refunds. It releases that original wallet and checks the order, authorization, captures and refunds against PayPal. S5 also shows the marked recording mandate’s threshold captures. S6 and S7 use two simultaneous pages observing one soak authorization and one payout batch. [Soak camera and receipt contract](soak-filming.md)

Supply the origin, provider readback interpreter, and existing private journal through the operator environment. The FFmpeg/FFprobe on PATH must support VP9. On the recording server the installed binaries are under `~/anaconda3/envs/devpost/bin`. Set `PLAYWRIGHT_CHROMIUM_CHANNEL=chromium-headless-shell` for its installed Playwright browser.

## C7 rehearsal

Set `PUBLIC_WEB_URL`, `PUBLIC_API_URL`, and `CACHET_SOAK_JOURNAL` to the production bindings. Set `CACHET_REHEARSAL_WEB_URL`, `CACHET_REHEARSAL_API_URL`, and `CACHET_SOAK_REHEARSAL_JOURNAL` to the separate rehearsal deployment and its funded recording mandate. The journal is a private mode-0600 file. A complete rehearsal needs both real threshold captures and the linked refund. An isolated rehearsal includes real sandbox close and settlement on its own authorized mandate.

```sh
export CACHET_WEB_URL="$CACHET_REHEARSAL_WEB_URL"
node video/record_scenes.ts --rehearsal --isolated-rehearsal-actions --session video/out/rehearsals/c7
python video/tts.py --models .cache/kokoro --from-take video/out/rehearsals/c7/take-1 --output video/out/rehearsals/c7/voice
python video/package.py video/out/rehearsals/c7/take-1 video/out/rehearsals/c7/take-2 --rehearsal --narration video/out/rehearsals/c7/voice --output video/out/rehearsals/c7/handoff
```

Run TTS using the existing Python environment with Kokoro ONNX and soundfile. It emits eight mono 24 kHz WAVs, the spoken texts, hashes and durations. The measured variant reads the recorded probe ratio, Buy quote, actual capture fees and authorization release. The duration limit is 110% of each Appendix B scene target. Omitting `--from-take` generates the generic rehearsal narration.

For a read-only subset, use e.g. `node video/record_scenes.ts S2,S8 --rehearsal --session video/out/rehearsals/visuals`. S1 also retains the `--archive-gallery` rehearsal option.

## D1 recording and D2 handoff

```sh
export CACHET_WEB_URL="$PUBLIC_WEB_URL"
node video/record_scenes.ts --session video/out/takes/d1
python video/tts.py --models .cache/kokoro --from-take video/out/takes/d1/take-1 --output video/out/takes/d1/voice
python video/package.py video/out/takes/d1/take-1 video/out/takes/d1/take-2 --narration video/out/takes/d1/voice --output video/out/video_raw_1112
```

Each paid ordinary take holds one lease on the recording server’s shared `.cache/night-gpu.lock` through publication, sponsor preparation, the ordinary agent runs, the S6 flush, and wallet release. The second take acquires its own lease. S6 honors the service’s cooldown inside that lease. For worktrees the default lock is next to the primary checkout’s Git directory. Run the recorder directly to allow it to acquire and release this lock. A non-default installation can set `CACHET_GPU_LOCK`.

The original source videos, stills, timestamps, cookie state, camera manifests and paired receipts stay under the session’s private directory. Keep the separate soak journal and its action sidecars on the operator host. Rerun with the **same session** to skip complete segments. For a shared GPU reservation, `--regular-take 1` or `--regular-take 2` captures just that ordinary wallet walkthrough and its verified release; the full command later reads those files before filming the one soak close and payout. If a wallet scene is interrupted, its available camera footage is retained and the recorder attempts release of that workspace’s original authorization. `--cleanup --session ...` retries only the regular wallets’ release and provider readback. Keep the original production/rehearsal flag. An interrupted, unfilmed payment is reported as incomplete. A resumed readback may document its outcome.

`--compose-only --session ...` rebuilds the edited takes entirely from the saved material. FFmpeg retains continuous source motion, including clicks, scrolling, loading transitions and receipt updates. It trims the interior of documented long waits and pixel-identical freezes, protecting the lead-in and reaction around every milestone. The real redacted PayPal popup appears for two seconds in S4. S5/S6 each retain their own camera’s soak segment. Edited durations can differ between cameras. Milestone PNGs are extracted from the corresponding edited WebM, with both the source timestamp and the retained source intervals preserved in the timeline. Set `CACHET_VIDEO_CRF` between 20 and 45 to tune VP9 size while retaining the action. Each event retains its source time and screenshot hash. The packager decodes the frame at each milestone and checks it against the PNG, verifies two independent wallet sessions and both original recorded soak submissions, and enforces the 300 MB uncompressed limit. The directory and zip outputs include the two takes, 1080p PNGs, voice WAVs, timeline, benchmark plots and measured contrast. Use `--contrast PATH` when the genuine contrast report resides in a separate benchmark checkout.

For an explicitly incomplete handoff, use `--allow-partial` while recording and `--partial` while packaging. Recording errors still return a failure exit status. The top-level timeline lists missing scenes by camera and any failed pair validation. A complete D1/D2 handoff requires the fully validated package. Copy **only the verified handoff directory** to the editing Mac at `~/Documents/Codex/video_raw_1112/`.

The optional first-cut editor accepts `python video/edit.py TAKE --output video/out/cachet.mp4`; use `--preview` for a rehearsal.


An interrupted original soak payout can be resumed with the same recording session and journal. Run the recorder with `S7 --soak-only` and the original environment flags. The cameras record the existing batch and both provider item readbacks. The editor joins the original browser submission with the later observation only when both camera journals match the original request and UTC, and retains their source hashes in the timeline. The cameras measure the time spent polling the submitted PayPal batch, including interrupted status checks. The editor cuts that interval with padding around the request and readback. A local C7 rehearsal can be exported with `--rehearsal --partial` once one complete ordinary take and the shared original S5/S6/S7 views have been verified. The package records which camera completed all eight scenes.
