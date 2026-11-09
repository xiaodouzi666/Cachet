# Cachet pricing and settlement

A workspace authorizes a maximum USD budget in PayPal Sandbox. Each inference turn accrues an integer number of microdollars (µUSD). One cent equals 10,000 µUSD. PayPal receives aggregated amounts in whole cents.

| Metered usage | Default USD per million tokens |
|---|---:|
| Private input, cache miss | 1.00 |
| Private input, cache hit | 0.10 |
| System or shared publisher input, cache hit | 0.15 |
| System or shared publisher input, cache miss | 0 |
| Generated output | 2.00 |
| Publisher royalty pool per shared publisher hit, included in the shared price | 0.04 |

The rates in the middle column also give µUSD per token. The engine reports the cached prefix length. Cachet intersects that prefix with each token span and rounds the combined charge to the nearest µUSD, half up. Deterministic largest-remainder allocation preserves the total. The two-layer publisher pool uses marginal contributions from the measured prefill model. System hits earn no publisher royalty. Shares attributable to the buyer's own linked publisher account stay with the platform.

For example, 25,900 publisher hits accrue 3,885 µUSD in shared charges and a 1,036 µUSD royalty pool. Ten such turns generate $0.01036 in total royalties across both publishers before reversals. This calculation illustrates the default rates. The catalog, live call events and ledger supply the actual hit counts and amounts for a particular session.

The Buy quote prices shared prefix hits at the shared rate. The Compute quote combines a specified cold fraction at the private miss rate with the remainder at the private hit rate. A quarter cold gives 0.325 µUSD per token. Both quotes estimate prefix costs. The invoice includes measured private turns and generated output.

## Collecting, refunding and paying

The standard capture threshold is $2.00. A judge workspace uses $0.02. Closing a mandate collects the remaining eligible balance within its authorization, absorbs sub-cent residuals in the rounding account and verifies that the authorization has closed or been voided. A provider-confirmed partial-capture capability permits periodic collection. A final-only configuration defers collection until close. The wallet probe records whether the sandbox account releases unused funds after the last partial capture. [PayPal Payments API](https://developer.paypal.com/docs/api/payments/v2/)

A shared hit breaches its SLA when its delivered time to first token exceeds the configured factor (default 2) times the measured warm median for its prompt-length band. An audit reverses eligible outstanding shared charges, refunds the captured portions through PayPal and reverses the associated royalties. The ledger separately accounts for whole-cent rounding and reported fee reversals. Repeated audits reuse confirmed refunds.

Publisher payables accrue across sessions within a workspace. Settlement pays whole cents after an individual publisher reaches $1.00, or $0.01 in judge mode. Every sandbox payout uses a stable sender batch ID and distinct sender item IDs. PayPal reports each item's transaction status independently. A successful batch alone does not establish delivery to both recipients. [PayPal Payouts integration](https://developer.paypal.com/api/payouts/customize)

Cachet reads each capture's fee from the confirmed seller receivable breakdown and each successful payout item's fee from its GET response. These values feed the PayPal clearing account. No flat-fee estimate is used. [PayPal capture breakdown](https://developer.paypal.com/sdk/payment/v2/definitions/capture/)

## Sandbox measurements for this account

On 9 November 2026, PayPal GET responses confirmed three $5.00 wallet authorizations for the configured US sandbox merchant. All amounts below are USD. Each fee is the amount PayPal reported for this account and transaction.

| Wallet path | Completed captures | Fee per capture, in order | Final authorization status |
|---|---|---|---|
| Two partial captures, then a final capture | $1.00, $0.50, $0.30 | $0.52, $0.50, $0.30 | `CAPTURED` after the final $0.30. The remaining $3.20 was uncaptured and no explicit void was needed. |
| One partial capture, then void | $0.10 | $0.10 | `VOIDED` with $4.90 uncaptured. |
| Void before capture | None | — | `VOIDED` with the full $5.00 uncaptured. |

Replaying the first $1.00 capture with its original request ID returned the same capture. PayPal completed a $0.20 refund against the $0.50 capture, and a subsequent GET reported that capture as `PARTIALLY_REFUNDED`.

The two-recipient payout batch reached `SUCCESS`. Separate item GETs confirmed a $0.10 payout with a $0.25 fee and a $0.20 payout with a $0.25 fee. Both items reported `SUCCESS`. The batch fee was $0.50.

## Measuring this sandbox app

Run the commands from the checkout with the configured sandbox app, sandbox buyer, two funded/receivable sandbox accounts, and Playwright Chromium. Keep the private journals on the operator's machine. Rerun with the same paths to resume pending provider operations.

```sh
.venv/bin/python scripts/sandbox_smoke.py wallet --journal logs/wallet.json
.venv/bin/python scripts/sandbox_smoke.py payout --journal logs/payout.json
.venv/bin/python scripts/register_webhooks.py --check logs/wallet.json logs/payout.json --wait 180
```

The first journal records GET-verified authorization outcomes, partial captures, a request-ID replay, release behavior, refund and fee breakdowns. The second records two successful item GETs and their individual fees. After registering the public webhook URL and setting its ID, the third command correlates verified event rows with those exact resources. [PayPal webhook events](https://developer.paypal.com/api/rest/webhooks/event-names/)

`make e2e` uses a dedicated sandbox judge workspace and live GPU. It first accumulates actual unpaid royalties across metered preparation sessions so both publishers can receive a whole cent. It then requires three acceptance sessions, each with three delayed turns, linked refunds and a separate reconciled two-recipient payout. `scripts/seed_demo.py` accumulates at least 30 other real operator test sessions and $0.05 outstanding per publisher. Both drivers reuse private state in `logs/demo-workspace.json` and respect the workspace's three-run hourly limit. Their separate evidence report omits login details and the workspace cookie.

To exercise webhook redelivery on that workspace, run `.venv/bin/python scripts/register_webhooks.py --redeliver logs/demo-workspace-evidence.json --wait 180` on the receiver host. PayPal signs and sends each original event again. The verifier observes a new delivery count for the completed refund, its earlier capture and the original order approval. It checks that the closed mandate and journal count remain stable, then reconciles through PayPal GET. A missing original delivery or unconfirmed resend fails the probe.

`.venv/bin/python scripts/reconcile_check.py logs/demo-workspace-evidence.json` takes a read-only SQLite backup into a private temporary directory. On the copy it reprocesses the verified events twice in reverse provider creation order, confirms that the transaction IDs and reconciliation agree, then flips a capture's final flag and requires PayPal GET to identify the edited field. The live database stays online throughout.

The seed driver reserves each dedicated mandate via the operator API before starting a run. After closure, the server confirms the named live run, its committed turns and a linked capture. The judge state returns the confirmed run IDs and count for the current workspace. Manually created runs in that same workspace keep their own provenance.
