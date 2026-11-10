import http from "node:http";
import net from "node:net";
import type { Socket } from "node:net";

/** Disconnect real browser sockets once. Application requests and responses are forwarded unchanged. */
export async function browserTransport(origin: URL) {
  const sockets = new Set<Socket>();
  const cursors: { path: string; id: string }[] = [];
  let cutAt = 0,
    forwarded = 0;
  const port = Number(origin.port || (origin.protocol === "https:" ? 443 : 80));
  const track = (socket: Socket) => {
    if (sockets.has(socket)) return;
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  };
  const proxy = http.createServer((incoming, outgoing) => {
    let url: URL;
    try {
      url = new URL(incoming.url ?? "", origin);
    } catch {
      outgoing.writeHead(400).end();
      return;
    }
    if (url.origin !== origin.origin || url.protocol !== "http:") {
      outgoing.writeHead(403).end();
      return;
    }
    if (Date.now() < cutAt) {
      incoming.socket.destroy();
      return;
    }
    forwarded++;
    if (incoming.headers["last-event-id"])
      cursors.push({
        path: url.pathname,
        id: String(incoming.headers["last-event-id"]),
      });
    track(incoming.socket);
    const request = http.request(
      url,
      { method: incoming.method, headers: incoming.headers },
      (response) => {
        outgoing.writeHead(response.statusCode ?? 502, response.headers);
        response.on("error", () => outgoing.destroy());
        response.pipe(outgoing);
      },
    );
    request.on("error", () => outgoing.destroy());
    outgoing.on("close", () => request.destroy());
    incoming.pipe(request);
  });
  proxy.on("connect", (req, down, head) => {
    if (req.url !== origin.hostname + ":" + port || Date.now() < cutAt) {
      down.destroy();
      return;
    }
    const upstream = net.connect(port, origin.hostname, () => {
      forwarded++;
      down.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      down.pipe(upstream);
      upstream.pipe(down);
    });
    track(down as Socket);
    track(upstream);
    down.on("error", () => upstream.destroy());
    upstream.on("error", () => down.destroy());
    down.on("close", () => upstream.destroy());
    upstream.on("close", () => down.destroy());
  });
  await new Promise<void>((resolve, reject) => {
    proxy.once("error", reject);
    proxy.listen(0, "127.0.0.1", resolve);
  });
  const address = proxy.address();
  if (!address || typeof address === "string")
    throw new Error("Missing proxy port");
  return {
    url: "http://127.0.0.1:" + address.port,
    cursors,
    get forwarded() {
      return forwarded;
    },
    cut() {
      const n = sockets.size;
      if (!n) throw new Error("No real browser sockets are connected");
      cutAt = Date.now() + 1250;
      for (const s of sockets) s.destroy();
      return n;
    },
    async close() {
      for (const s of sockets) s.destroy();
      await new Promise<void>((resolve) => proxy.close(() => resolve()));
    },
  };
}
