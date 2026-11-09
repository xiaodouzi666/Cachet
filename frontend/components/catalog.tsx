"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { AgGridReact } from "ag-grid-react";
import {
  AllCommunityModule,
  themeQuartz,
  type ColDef,
} from "ag-grid-community";
import { Search, RotateCw } from "lucide-react";
import { Empty, Failure, Skeleton, Tag } from "./ui";
import { useSession } from "./shell";
import { count, usd, useResource } from "../lib/client";
import type { ContextRow } from "../lib/types";

const modules = [AllCommunityModule];
const gridTheme = themeQuartz.withParams({
  accentColor: "#0f766e",
  fontFamily: "Inter Variable, sans-serif",
  borderColor: "#dce2df",
  headerBackgroundColor: "#f3f6f2",
  rowHoverColor: "#eef7f1",
  wrapperBorderRadius: 8,
});
export function Catalog() {
  const replay = useSession().mode === "replay";
  const {
    data: rows,
    loading,
    error,
    reload,
  } = useResource<ContextRow[]>("contexts", 6000);
  const [filter, setFilter] = useState("");
  const columns = useMemo<ColDef<ContextRow>[]>(
    () => [
      {
        field: "title",
        headerName: "Context",
        minWidth: 160,
        pinned: "left",
        cellRenderer: ({ data }: { data: ContextRow }) => (
          <Link
            className="text-button"
            href={`/agent?context=${encodeURIComponent(data.id)}`}
          >
            {data.title}
          </Link>
        ),
      },
      {
        field: "parent_id",
        headerName: "Parent",
        valueGetter: ({ data }) =>
          rows?.find((r) => r.id === data?.parent_id)?.title || "Root",
      },
      { field: "publisher_name", headerName: "Publisher" },
      {
        field: "status",
        headerName: "State",
        cellRenderer: ({ value }: { value: string }) => (
          <Tag tone={replay ? "neutral" : value === "hot" ? "good" : "neutral"}>
            {replay ? "Recorded workload" : value}
          </Tag>
        ),
      },
      {
        field: "token_len",
        headerName: "Layer tokens",
        type: "numericColumn",
        valueFormatter: ({ value }) => count(value),
      },
      {
        field: "price_shared",
        headerName: "USD / 1M hits",
        type: "numericColumn",
        valueFormatter: ({ value }) =>
          value == null ? "—" : Number(value).toFixed(2),
      },
      {
        field: "total_hits",
        headerName: "Metered hits",
        type: "numericColumn",
        valueFormatter: ({ value }) => count(value),
      },
      {
        field: "royalty_micros",
        headerName: "Royalties",
        type: "numericColumn",
        valueFormatter: ({ value }) => usd(value),
      },
    ],
    [rows, replay],
  );
  return (
    <section className="panel">
      <div className="toolbar">
        <label className="search-field">
          <Search size={16} />
          <input
            aria-label="Filter contexts"
            value={filter}
            placeholder="Find a context or publisher"
            onChange={(e) => setFilter(e.target.value)}
          />
        </label>
        <button className="text-button" onClick={reload}>
          <RotateCw size={15} />
          Refresh
        </button>
      </div>
      <Failure error={error} retry={reload} />
      {loading ? (
        <Skeleton />
      ) : rows?.length ? (
        <div className="catalog-grid">
          <AgGridReact<ContextRow>
            modules={modules}
            theme={gridTheme}
            rowData={rows}
            columnDefs={columns}
            defaultColDef={{
              minWidth: 108,
              flex: 1,
              sortable: true,
              resizable: true,
            }}
            quickFilterText={filter}
            getRowId={(p) => p.data.id}
            domLayout="autoHeight"
          />
        </div>
      ) : (
        !error && (
          <Empty title="No published contexts yet">
            <Link className="text-button" href="/publish">
              Publish the first context
            </Link>
          </Empty>
        )
      )}
    </section>
  );
}
