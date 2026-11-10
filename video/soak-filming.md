# Recording the shared mandate

`video/soak_scenes.cjs` exports `recordSoakSegment({scene, browser, baseURL, journalPath, outputDirs, rehearsal, rehearsalActions})`. Supply two different camera directories and explicitly choose the rehearsal mode. Each camera gets an independent browser context, a separate 1920 × 1080 WebM and PNGs. The result is a two-element array of timeline events and sanitized evidence. Milliseconds are relative to the start of each camera's own page. A submitted close or payout carries the same action key and UTC request time in both timelines.

Set `PUBLIC_WEB_URL`, `PUBLIC_API_URL` and `CACHET_SOAK_JOURNAL` on the recording machine. The journal must be owned by the operator, mode 0600, with the original API and web origins. Both browsers read the existing workspace through its cookie. Provider amounts, fees, refunds and identities are compared with independent PayPal Sandbox GETs. Keep the private journal, its cookie and the action checkpoints outside the video package.

- `--rehearsal` observes the configured workspace. It does not submit a close or payout.
- `--isolated-rehearsal-actions` exercises the actual UI in a dedicated rehearsal deployment. Set `CACHET_REHEARSAL_WEB_URL`, `CACHET_REHEARSAL_API_URL` and `CACHET_SOAK_REHEARSAL_JOURNAL`. Both origins must differ from production, and the rehearsal workspace and mandate must differ from the production registration. The production journal path may be reserved before the production measurement starts.
- `--formal` enables the original production action at D1. S5 records the two threshold captures. S6 verifies the original fault refund, then one camera closes the original ACTIVE mandate in the agent UI. S7 submits one payout from the settlement UI and records both verified recipients, attribution and the workspace Copilot answer.

Example invocation:

```sh
node video/soak_scenes.cjs S6 video/out/camera-a video/out/camera-b --isolated-rehearsal-actions
```

An atomic checkpoint beside the private journal is written before each money action. Subsequent takes only observe that same close or batch. A missing or ambiguous provider receipt after an interruption requires inspecting the saved action. Never remove the checkpoint to repeat a payment. Each camera returns a read-only rehearsal label or the observed receipt and distinguishes recovered results from a newly submitted action.
