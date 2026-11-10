# Recording mandate

The operator driver runs a live, bounded PayPal Sandbox workload against one dedicated $5 card authorization and the published FastAPI-on-Starlette context. Its private journal contains the workspace cookie needed to resume the run and record the close. The adjacent evidence file contains the measured counts and provider IDs.

The operator machine and the API require an independent `CACHET_SOAK_TOKEN` and the normal `ADMIN_TOKEN`. Local setup generates distinct tokens. Configure the soak token only on the backend and operator machine. The Next.js proxy has no operator routes. Ordinary workspaces retain their public request quotas. The dedicated runner shares the normal GPU semaphore, can schedule one live built-in task at a time, and is limited to 240 starts per hour and 600 per registered mandate.

From the server checkout, run the driver in a tmux session. The default API origin is `PUBLIC_API_URL`. Use different private journal paths for a loopback fallback and production.

```sh
.venv/bin/python scripts/soak_mandate.py --state logs/soak-production.json
.venv/bin/python scripts/soak_mandate.py --state logs/soak-production.json --status
.venv/bin/python scripts/soak_mandate.py --state logs/soak-production.json --continue-if-needed
```

The initial window lasts up to three hours or $4.20 of captured and pending charges. Six delayed live turns are scheduled near a normal threshold capture. The driver requests an ordinary SLA audit after the capture includes delayed shared charges. The final report contains the provider authorization disposition, capture fees, refund IDs and reconciliation. The mandate remains open for the recording. If the C2 targets are still outstanding, an explicit continuation opens another bounded window and retains $0.25 of authorization headroom. The recording operator closes and settles through the normal judge controls.
