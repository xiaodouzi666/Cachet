import { check } from "./client";

export async function* copilotEvents(message: string, signal?: AbortSignal) {
  const response = await check(
    await fetch("/api/copilot", {
      method: "POST",
      credentials: "same-origin",
      signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ message }),
    }),
  );
  if (!response.body) throw new Error("Copilot returned an empty response.");
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let cut = buffer.search(/\r?\n\r?\n/);
      while (cut >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut).replace(/^\r?\n\r?\n/, "");
        const lines = block.split(/\r?\n/),
          event =
            lines
              .find((l) => l.startsWith("event:"))
              ?.slice(6)
              .trim() || "message";
        const data = lines
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trimStart())
          .join("\n");
        if (data && data !== "[DONE]") {
          const payload = JSON.parse(data);
          yield { event, eventData: payload };
        }
        cut = buffer.search(/\r?\n\r?\n/);
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
