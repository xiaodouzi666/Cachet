import type { ContextRow } from "./types";

type RegisteredLayer = Pick<ContextRow, "id" | "slug" | "parent_id" | "status">;

export function hasRunnableLayers(rows: RegisteredLayer[], mode?: string) {
  if (mode !== "live" && mode !== "hosted" && mode !== "replay") return false;
  const usable =
    mode !== "replay" ? rows.filter((layer) => layer.status === "hot") : rows;
  const roots = new Set(
    usable
      .filter((layer) => layer.slug === "starlette" && !layer.parent_id)
      .map((layer) => layer.id),
  );
  return usable.some(
    (layer) =>
      layer.slug === "fastapi" &&
      layer.parent_id != null &&
      roots.has(layer.parent_id),
  );
}
