"use client";
import Link from "next/link";
import { ArrowRight, BookOpenText, Coins, Cpu, Timer } from "lucide-react";
import { ActionLink, Failure, Skeleton } from "../components/ui";
import { count, latency, useResource } from "../lib/client";
import type { Bench } from "../lib/types";

export default function Home() {
  const bench = useResource<Bench[]>("benchmarks", 60000);
  const band = bench.data?.reduce<Bench | undefined>(
    (best, item) => (!best || best.prefix_len < item.prefix_len ? item : best),
    undefined,
  );
  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <div className="hero-kicker">
            <span />
            Context marketplace
          </div>
          <h1>
            Warm context,
            <br />
            <em>fairly paid.</em>
          </h1>
          <p>
            Coding agents answer faster by reusing code already loaded on a GPU.
            Maintainers earn a PayPal royalty when their code is reused.
          </p>
          <div className="actions">
            <ActionLink href="/judge">Start guided demo</ActionLink>
            <Link className="hero-link" href="/catalog">
              Browse catalog <ArrowRight size={15} />
            </Link>
          </div>
        </div>
        <div
          className="hero-art"
          role="img"
          aria-label="Shared foundation, framework and private tenant context form a layered prefix"
        >
          <div className="iso-grid" />
          <div className="iso-tile base" />
          <div className="iso-tile middle" />
          <div className="iso-tile top" />
          <div className="art-label label-a">Private turn</div>
          <div className="art-label label-b">Framework</div>
          <div className="art-label label-c">Shared prefix</div>
        </div>
      </section>
      <section className="section">
        <div className="section-heading">
          <h2>How the market works</h2>
        </div>
        <div className="flow">
          {[
            {
              icon: BookOpenText,
              title: "Maintainers earn royalties",
              detail: "Publish licensed code and earn a share every time an agent reuses its cached tokens.",
              href: "/publish",
            },
            {
              icon: Cpu,
              title: "Agents spend less time waiting",
              detail: "Compare the cost, approve one budget and pay only for the shared tokens the GPU reuses.",
              href: "/agent",
            },
            {
              icon: Coins,
              title: "Operators verify payments",
              detail: "Trace each charge to measured usage, refund missed guarantees and check receipts against PayPal.",
              href: "/settlement",
            },
          ].map(({ icon: Icon, title, detail, href }) => (
            <Link href={href} className="flow-card" key={title}>
              <span className="icon-tile">
                <Icon size={21} />
              </span>
              <h3>
                {title}
                <ArrowRight size={16} />
              </h3>
              <p>{detail}</p>
            </Link>
          ))}
        </div>
      </section>
      <section className="section bench-panel">
        <div className="bench-title">
          <span className="icon-tile">
            <Timer size={21} />
          </span>
          <div>
            <h2>Inference benchmark</h2>
            <p>
              Median time to first text{band?.gpu ? ` on the ${band.gpu}` : ""}
              {band?.mode === "hosted"
                ? " · independently calibrated hosted fallback"
                : ""}
              .
            </p>
          </div>
        </div>
        {bench.error ? (
          <Failure error={bench.error} retry={bench.reload} />
        ) : bench.loading ? (
          <Skeleton />
        ) : band ? (
          <div className="bench-grid">
            <div>
              <small>Context length</small>
              <strong>
                {count(band.prefix_len)} <span>tokens</span>
              </strong>
            </div>
            <div>
              <small>Cold start</small>
              <strong>{latency(band.cold_ttft_ms_p50)}</strong>
            </div>
            <div>
              <small>Warm start</small>
              <strong className="teal">{latency(band.warm_ttft_ms_p50)}</strong>
            </div>
          </div>
        ) : (
          <p>No recorded benchmark is available yet.</p>
        )}
        {bench.data && bench.data.length > 0 && (
          <details className="bench-detail">
            <summary>Compare measured context lengths</summary>
            <table>
              <thead>
                <tr>
                  <th>Input tokens</th>
                  <th>Cold TTFT</th>
                  <th>Warm TTFT</th>
                </tr>
              </thead>
              <tbody>
                {bench.data.map((row) => (
                  <tr key={row.prefix_len}>
                    <td>{count(row.prefix_len)}</td>
                    {[row.cold_ttft_ms_p50, row.warm_ttft_ms_p50].map(
                      (ms, i) => (
                        <td key={i}>
                          <span
                            className={"bench-bar " + (i ? "warm" : "cold")}
                            style={{
                              width: `${(100 * ms) / Math.max(...bench.data!.map((b) => b.cold_ttft_ms_p50))}%`,
                            }}
                          />
                          {latency(ms)}
                        </td>
                      ),
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </section>
    </>
  );
}
