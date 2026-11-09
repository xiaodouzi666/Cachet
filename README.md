# Cachet

Cachet lets coding agents buy warm open-source context. The engine counts reused tokens, the ledger attributes royalties to publishers, and PayPal Sandbox handles the money.

![Cachet showing cache hits for each source layer on the first turn of a recorded FastAPI task](docs/images/measured-prefix-segments.png)

On this turn the engine reused 25,913 of 26,260 input tokens. The inspector attributes 11,992 hits to Starlette and 13,908 to FastAPI. The remaining 13 hits belong to the system prefix. [Recorded turn and token ranges](backend/app/replay/traces/rate-limit-buy.json)

A team running coding agents may repeatedly process the same framework across unrelated sessions. A hot prefix avoids much of this prefill work. Cache lifetimes depend on the provider and cache configuration. Cachet uses observed reuse to price each turn and credit the maintainers who supplied the shared code. [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## Try it locally

Use Python 3.11, Node.js 22, npm, Make and tmux. A fresh checkout can serve the UI, SQLite ledger, measured benchmarks and recorded workloads without a GPU.

```sh
make setup
make dev
```

Open [the judge guide](http://127.0.0.1:3000/judge). Choose **Start guided demo** to create an isolated workspace. The catalog and settlement pages show the records in the current workspace. Keys belong in the private project `.env`. Setup generates a gateway token, keeps existing configuration and creates the local database on first startup. The API is available at [loopback health](http://127.0.0.1:8000/api/health).

For sandbox transactions, configure a PayPal sandbox app with Payouts access, a demo sandbox buyer, and two distinct sandbox receiver accounts. Set the `PAYPAL_*`, `JUDGE_BUYER_*` and `PUBLISHER_*_EMAIL` fields in `.env`. Judge mode then displays the configured sandbox buyer during checkout. Only set the optional fixed test-card fields for a supported sandbox card. Ordinary workspaces supply their own receivers. The two sandbox publishers in the walkthrough are test roles using licensed framework excerpts. A PayPal login, mandate ID or payment receipt is never generated locally.

For live inference on Linux, use a compatible NVIDIA GPU, CUDA toolkit and `uv`. Generate a separate random `SGLANG_API_KEY` in the project `.env`, install the pinned model and start the authenticated tmux service:

```sh
scripts/start_sglang.sh --install
scripts/start_sglang.sh
```

For Ops Copilot's PayPal read tools, run `.venv/bin/python scripts/toolkit_probe.py install`. The toolkit runs in a separate Python environment. Copilot uses the local model, or the configured OpenAI-compatible endpoint. Transaction history is visible only for provider IDs in the current workspace. Sandbox Reporting can lag behind the Payments API. Sandbox Copilot exposes order lookup and transaction search. The Toolkit merchant-insights endpoint requires a supported merchant environment.

## From a prompt to a payout

1. A publisher selects pinned Starlette or FastAPI source. The gateway checks its hash, tokenizes it, warms the prefix, and probes again. The catalog shows the measured hit ratio.
2. The buyer sets a budget and approves a sandbox order. The agent compares the cost of sharing the hot prefix with a private compute baseline before its ten-turn task.
3. SGLang returns cumulative cached input tokens. Intersecting that prefix with each source span gives the exact hits attributed to each layer. A tenant salt separates private turns.
4. Each turn posts integer microdollars to a double-entry ledger. Captures batch whole cents. An SLA audit reverses uncaptured charges or requests a PayPal refund for captured charges. Payouts group royalties by recipient. Reconciliation reads back the corresponding PayPal objects.

```mermaid
flowchart LR
  Browser[Next.js judge and settlement UI] --> Gateway[Trusted application gateway]
  Gateway --> API[FastAPI]
  API --> GPU[SGLang and Qwen]
  API --> Tracks[Checked GPU recordings]
  API --> DB[(SQLite ledger)]
  API --> PP[PayPal Sandbox]
  PP -->|Signed webhook| API
  API --> Ops[Ops Copilot and scoped toolkit]
```

```mermaid
flowchart LR
  Auth[Buyer authorization] -->|Metered whole cents| Cap[Capture]
  Cap --> Clearing[PayPal clearing]
  Cap -->|Confirmed funding| Eligible[Publisher royalties eligible for payout]
  Eligible --> Batch[Recipient payout batch]
  Clearing -->|Captured public charge under SLA| Refund[Buyer refund]
  Auth -->|Close| Released[Unused authorization released]
```

The normal mandate path is `CREATED → APPROVED → AUTHORIZED → ACTIVE`. A partial capture moves through `CAPTURING`. Closing performs the final whole-cent capture when due, checks the provider authorization, releases the remainder and journals rounding dust. The state machine also handles void, expiry and failure. [State machine](backend/app/settlement/mandate_fsm.py) · [Settlement service](backend/app/settlement/service.py)

## Pricing and performance

At the default tariff, a private input miss costs $1.00 per million tokens and a private hit costs $0.10. A shared hit costs $0.15, including $0.04 for the publisher pool on publisher-owned tokens. A missed shared token is free. Generated output costs $2.00 per million tokens. The quote's private price per million is `f × 1.00 + (1 − f) × 0.10`, where the user sets the expected cold fraction `f`. At 25% the private estimate is $0.325 per million. The quote assumes the selected shared prefix will hit. Actual billing uses the metered result of each turn. [Pricing, attribution and verification](docs/pricing.md)

The benchmark used Qwen2.5-7B-Instruct-AWQ, SGLang 0.5.23 and an NVIDIA GeForce RTX 3090 Ti with 24 GiB of VRAM. Each prefix length has five cold/warm pairs. Time to first text includes the local HTTP transport and detokenization.

![Measured cold and warm time to first text across five prefix lengths](bench/plots/prefix-latency.png)

| Prefix tokens | Cold p50 | Warm p50 |
|---:|---:|---:|
| 2,000 | 384 ms | 28 ms |
| 4,000 | 783 ms | 28 ms |
| 8,000 | 1,743 ms | 29 ms |
| 16,000 | 4,160 ms | 47 ms |
| 26,000 | 8,432 ms | 69 ms |

A separate ten-turn FastAPI rate-limiting task produced its first text in 186 ms using the shared prefix and 8,277 ms computing privately. Pricing each independent conversation's actual token counts at the default tariff gives $0.045621 and $0.056174 respectively, before any SLA adjustments. Two of the ten shared calls met the 138.152 ms warm SLA. Those two calls accounted for 1,390 microdollars of Starlette royalties and 682 microdollars of FastAPI royalties at the default tariff. The two conversations produced their own outputs. These amounts describe metered usage. The shared FastAPI answer included prose around its working module.

All four final generated modules passed the isolated HTTP checks. For both Starlette strategies, `/ping` returned JSON with `X-Route-Class: custom`, an unknown URL returned 404, and a second endpoint inherited the same header from the route subclass. Both FastAPI modules enforced independent client quotas and restored access after 60 seconds. [Paired benchmark and fit](bench/results/calibration.json) · [Session comparison](bench/results/sessions.json)

## AI components

| Component | Model or service | Role |
|---|---|---|
| Coding agent | Qwen2.5-7B-Instruct-AWQ | Uses its previous outputs to investigate a framework task over ten turns. [Agent](backend/app/agents/inference.py) |
| Inference gateway | SGLang | Streams text and reports cached input tokens for tenant-scoped prefixes. [Gateway](backend/app/gateway/sglang_client.py) |
| Recorded inference | Archived Qwen conversations | Checks hashes, source revisions and the original quote before replaying a conversation. [Recorder](scripts/record_sessions.py) |
| Ops Copilot | Local model or configured OpenAI-compatible endpoint | Uses workspace-scoped tools to answer payment and royalty questions. A human confirms refund proposals. [Copilot](backend/app/agents/copilot.py) |

The mode badge reflects the reachable engine. With the GPU offline, recorded mode supports the two built-in tasks and their original quote inputs (25% cold for buy and 0% for compute). Every replay turn carries its mode. Free-text tasks need live inference. Payments in either mode require the configured PayPal sandbox.

The quote determines whether the agent buys shared context or computes privately. Before the first metered turn, the serving model explains the choice in a separate, unbilled request. Cachet checks both prices and the selected strategy before publishing the explanation. The archive pairs each original quote with a separately recorded explanation and preserves the original coding responses. With SGLang online, run `.venv/bin/python -m scripts.record_quote_reasons --verify-only` to reconstruct the four input token sequences. [GPU quote recordings](backend/app/replay/quote_reasons.json)

## PayPal APIs

| API | Where Cachet uses it | Implementation |
|---|---|---|
| PayPal JS SDK and Orders v2 | Browser approval and an AUTHORIZE order for the budget | [Wallet](frontend/components/wallet.tsx) · [PayPal client](backend/app/paypal/client.py) |
| Payments v2 | Capture authorized spend, release the unused budget and refund eligible captures | [Settlement service](backend/app/settlement/service.py) |
| Payouts v1 | Group recipient items into a durable payout batch and read back fees and statuses | [Payout job](backend/app/settlement/payout_job.py) |
| Webhooks and Reporting | Verify signatures, process events and inspect transaction history with the PayPal Agent Toolkit | [Webhook handler](backend/app/api/webhooks.py) · [Toolkit](backend/app/paypal/toolkit.py) |

The AG Studio settlement dashboard shows the current workspace's journal, royalty tree and Ops Copilot. [Dashboard](frontend/studio/settlement.tsx)

## Verification

```sh
make test
make verify
make verify-full
make e2e
```

`make verify` runs unit and non-PayPal integration tests. With a configured GPU it includes real generation, cache flushes and tenant isolation. Run it with exclusive access to that inference server. `make verify-full` also requires sandbox credentials and provisioned accounts.

`make e2e` uses one judge workspace and real sandbox payments. If either publisher is below the payout minimum, the driver first runs paid ten-turn framework questions on the GPU to earn eligible royalties. These preparation sessions follow the same SLA, refunds and hourly limits as any other run. Their charges and balances are recorded separately; only the three built-in coding sessions with fault injection, refunds, two payouts and successful reconciliation count toward acceptance. See [the sandbox sequence and acceptance instructions](docs/pricing.md).

To reproduce the five-band benchmark use `.venv-sglang/bin/python -m bench.calibrate`. Local charts and checked-in traces do not require GPU installation to inspect.

With Docker running, verify the actual final modules over loopback HTTP inside network-isolated containers:

```sh
.venv/bin/python -m scripts.check_generated_apps --build --require-passed
```

## Prior work and scope

| Project or study | What it measures or allocates | What the result is used for |
|---|---|---|
| thanks.dev | Dependencies declared in a sponsor’s repositories | Divides a donation among the upstream projects the sponsor uses. [Canonical’s funding example](https://canonical.com/blog/canonical-thanks-dev-giving-back-to-open-source-developers) |
| Langfuse | Per-call token usage, including provider-reported cached tokens, with a price for each usage type | Reports application spend by model, user or tag. [Usage and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking) |
| *Can I Buy Your KV Cache?* | The compute cost of reusing a document’s KV cache versus rebuilding it | Proposes provider-hosted reuse and identifies payment between publishers and consumers as an open problem. [Zhang, 2026](https://arxiv.org/abs/2606.13361) |
| Cachet | The intersection of an engine-reported cache hit with each publisher’s token range on a specific turn | Calculates each publisher’s royalty and records it in the ledger. The settlement implementation batches funded royalties for PayPal Sandbox payout. [Attribution and settlement](docs/pricing.md) |

The checked-in evidence covers the GPU benchmark, forty recorded turns, local ledger checks and browser integration. Sandbox receipt totals, payout throughput and publicly hosted uptime require a provisioned deployment. Fees come from actual provider responses. The Render configuration uses a single API process and a persistent SQLite disk. [Deployment](docs/deployment.md)

Original Cachet code is released under the MIT license. Redistributed source excerpts and the model retain their respective licenses. [MIT](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
