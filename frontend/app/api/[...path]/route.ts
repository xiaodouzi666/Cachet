import { isIP } from "node:net";
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const id = "[A-Za-z0-9_-]+";
const reads = new RegExp(
  `^(health|reference-turn|sources|benchmarks|tasks|contexts(/${id})?|mandates/${id}|agent/runs/${id}/(events|application-check)|judge/(state|capture-preview|walkthrough|preparation|settle)|ledger|captures|refunds|payouts|paypal-events|royalties(/system)?|reconcile)$`,
);
const writes = new RegExp(
  `^(publishers|contexts|contexts/${id}/warm|quote|mandates|mandates/card|mandates/${id}/(authorize|close)|agent/runs|copilot(/confirm)?|judge/(workspace|prepare|task|finish|fault-run|fault-audit|fault|flush-cache|capture-now|fault-capture|audit|settle))$`,
);
const cookieName = "cachet_workspace";

async function forward(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const path = (await params).path.join("/");
  const post = req.method === "POST";
  if (!(post ? writes : reads).test(path)) {
    return NextResponse.json({ detail: "Unknown API route." }, { status: 404 });
  }
  let body: string | undefined;
  if (post) {
    const origin = req.headers.get("origin");
    // Next normalizes loopback hosts to "localhost", while browsers retain the
    // port and hostname they actually navigated to (including SSH forwards).
    const allowedOrigin = process.env.PUBLIC_WEB_URL
      ? new URL(process.env.PUBLIC_WEB_URL).origin
      : req.nextUrl.protocol + "//" + req.headers.get("host");
    if (
      req.headers.get("sec-fetch-site") === "cross-site" ||
      (origin && origin !== allowedOrigin)
    ) {
      return NextResponse.json(
        { detail: "Cross-site request refused." },
        { status: 403 },
      );
    }
    if (req.headers.get("content-type")?.split(";")[0] !== "application/json") {
      return NextResponse.json(
        { detail: "Send JSON to this endpoint." },
        { status: 415 },
      );
    }
    if (Number(req.headers.get("content-length") || 0) > 32768) {
      return NextResponse.json(
        { detail: "Request exceeds the input limit." },
        { status: 413 },
      );
    }
    const reader = req.body?.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    if (reader) {
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          total += chunk.value.length;
          if (total > 32768) {
            await reader.cancel();
            return NextResponse.json(
              { detail: "Request exceeds the input limit." },
              { status: 413 },
            );
          }
          chunks.push(chunk.value);
        }
      } finally {
        reader.releaseLock();
      }
    }
    body = Buffer.concat(chunks).toString("utf8");
  }
  const api =
    process.env.CACHET_API_URL ||
    process.env.PUBLIC_API_URL ||
    "http://127.0.0.1:8000";
  const url = new URL(`/api/${path}`, api);
  url.search = req.nextUrl.search;
  const headers = new Headers({
    accept: req.headers.get("accept") || "application/json",
  });
  // Enable only for a header that the deployment's ingress unconditionally overwrites.
  const trustedHeader = process.env.CACHET_TRUSTED_CLIENT_IP_HEADER;
  const address = trustedHeader && req.headers.get(trustedHeader);
  if (address && isIP(address)) headers.set("x-cachet-client-ip", address);
  if (post) headers.set("content-type", "application/json");
  const cookieNames =
    path === "judge/workspace"
      ? [cookieName, "cachet_user_workspace"]
      : [cookieName];
  const cookies = cookieNames.flatMap((name) => {
    const value = req.cookies.get(name)?.value;
    return value ? [`${name}=${encodeURIComponent(value)}`] : [];
  });
  if (cookies.length) headers.set("cookie", cookies.join("; "));
  const cursor = req.headers.get("last-event-id");
  if (cursor) headers.set("last-event-id", cursor);
  if (process.env.BFF_TOKEN) {
    headers.set("x-gateway-token", process.env.BFF_TOKEN);
  }
  try {
    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      signal: req.signal,
      cache: "no-store",
      redirect: "error",
    });
    const response = new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type":
          upstream.headers.get("content-type") || "application/json",
        "cache-control": "private, no-store, no-transform",
        "x-accel-buffering": "no",
      },
    });
    for (const name of ["paypal-debug-id", "retry-after"]) {
      const value = upstream.headers.get(name);
      if (value) response.headers.set(name, value);
    }
    if (path === "judge/workspace" && upstream.ok) {
      const lines =
        upstream.headers.getSetCookie?.() ||
        (upstream.headers.get("set-cookie") || "").split(
          /,\s*(?=cachet_(?:user_)?workspace=)/i,
        );
      for (const line of lines) {
        const parsed = line.match(
          /^(cachet_workspace|cachet_user_workspace)=([^;,]*)/,
        );
        if (!parsed) continue;
        const name = parsed[1];
        const maxAge = line.match(/;\s*Max-Age=(\d+)/i)?.[1];
        response.cookies.set(name, parsed[2], {
          httpOnly: true,
          sameSite: "lax",
          path: name === cookieName ? "/" : "/api/judge/workspace",
          secure: (process.env.PUBLIC_WEB_URL || req.nextUrl.origin).startsWith(
            "https://",
          ),
          ...(maxAge ? { maxAge: Number(maxAge) } : {}),
        });
      }
    }
    return response;
  } catch {
    return NextResponse.json(
      {
        detail:
          "The Cachet API is unreachable. Check the connection and retry.",
      },
      { status: 503 },
    );
  }
}
export { forward as GET, forward as POST };
