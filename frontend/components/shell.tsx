"use client";
import {
  createContext,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import type { ReactNode } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  Bot,
  Boxes,
  ChevronRight,
  Compass,
  Layers3,
  Menu,
  PanelTop,
  SquareChartGantt,
  Upload,
  X,
} from "lucide-react";
import { api, describe, useResource, workspace } from "../lib/client";
import type { Health, State } from "../lib/types";
import { Failure, Skeleton } from "./ui";

const nav = [
  { href: "/", label: "Overview", icon: PanelTop },
  { href: "/catalog", label: "Context catalog", icon: Boxes },
  { href: "/publish", label: "Publish context", icon: Upload },
  { href: "/agent", label: "Agent console", icon: Bot },
  { href: "/settlement", label: "Settlement", icon: SquareChartGantt },
  { href: "/judge", label: "Judge mode", icon: Compass },
];
const Session = createContext<{
  state: State | null;
  mode?: string;
  error: string;
  loading: boolean;
  start: (kind: "judge" | "user", switchBack?: boolean) => Promise<void>;
  refresh: () => Promise<void>;
}>({
  state: null,
  error: "",
  loading: true,
  start: async () => {},
  refresh: async () => {},
});
export const useSession = () => useContext(Session);
export function WorkspaceGate({ children }: { children: ReactNode }) {
  const s = useSession();
  if (!s.state)
    return s.error ? (
      <Failure error={s.error} retry={() => void s.start("user")} />
    ) : (
      <Skeleton label="Opening workspace" />
    );
  return <>{children}</>;
}
function Mode({ health, error }: { health: Health | null; error: string }) {
  const name = health?.mode;
  const label =
    name === "live"
      ? "Live · self-hosted GPU"
      : name === "hosted"
        ? "Live · hosted cache"
        : name === "replay"
          ? "Recorded GPU replay"
          : name === "unavailable" || error
            ? "Inference offline"
            : "Checking inference";
  return (
    <span
      className={`mode-dot ${name === "live" || name === "hosted" ? "online" : ""}`}
      title={health?.gpu || health?.model || error || label}
    >
      {label}
    </span>
  );
}
export function RecordingMode() {
  const rec = useSearchParams().get("rec") === "1";
  useEffect(() => {
    if (!rec) return;
    document.documentElement.classList.add("recording");
    const cursor = document.createElement("span");
    cursor.className = "recording-cursor";
    cursor.hidden = true;
    document.body.appendChild(cursor);
    const move = (e: PointerEvent) => {
      cursor.hidden = false;
      cursor.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
    };
    const click = (e: PointerEvent) => {
      const pulse = document.createElement("span");
      pulse.className = "recording-pulse";
      pulse.style.left = e.clientX + "px";
      pulse.style.top = e.clientY + "px";
      document.body.appendChild(pulse);
      pulse.addEventListener("animationend", () => pulse.remove(), {
        once: true,
      });
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerdown", click);
    return () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerdown", click);
      cursor.remove();
      document.querySelectorAll(".recording-pulse").forEach((x) => x.remove());
      document.documentElement.classList.remove("recording");
    };
  }, [rec]);
  return null;
}
export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname(),
    health = useResource<Health>("health", 25000);
  const [open, setOpen] = useState(false),
    [state, setState] = useState<State | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false);
  const refresh = useCallback(async () => {
    setState(await api<State>("judge/state"));
    setError("");
  }, []);
  const start = useCallback(
    async (kind: "judge" | "user", switchBack = false) => {
      setLoading(true);
      setError("");
      try {
        await workspace(kind, switchBack);
        await refresh();
      } catch (e) {
        setError(describe(e));
      } finally {
        setLoading(false);
      }
    },
    [refresh],
  );
  useEffect(() => {
    setOpen(false);
    if (
      !state &&
      pathname !== "/" &&
      !pathname.startsWith("/dev/") &&
      pathname !== "/judge"
    )
      void start("user");
    if (!state && pathname === "/judge") void refresh().catch(() => {});
  }, [pathname, Boolean(state), start, refresh]); // Only initialize once per workspace.
  return (
    <Session.Provider
      value={{ state, mode: health.data?.mode, error, loading, start, refresh }}
    >
      <Suspense>
        <RecordingMode />
      </Suspense>
      <a href="#content" className="skip">
        Skip to content
      </a>
      <div className="frame">
        {open && (
          <button
            className="nav-scrim"
            aria-label="Close navigation"
            onClick={() => setOpen(false)}
          />
        )}
        <aside className={`sidebar ${open ? "open" : ""}`}>
          <Link href="/" className="wordmark" aria-label="Cachet home">
            <i className="brand-icon">
              <Layers3 size={23} strokeWidth={2.3} />
            </i>
            Cachet
          </Link>
          <nav aria-label="Main navigation">
            {nav.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className={pathname === href ? "current" : ""}
                aria-current={pathname === href ? "page" : undefined}
              >
                <Icon size={18} />
                <span>{label}</span>
                {pathname === href && (
                  <ChevronRight size={15} className="nav-chevron" />
                )}
              </Link>
            ))}
          </nav>
          <div className="side-foot">
            <BookOpen size={16} />
            <Link href="/judge">
              Testing instructions <ArrowUpRight size={13} />
            </Link>
          </div>
        </aside>
        <div className="workspace">
          <header className="topbar">
            <button
              onClick={() => setOpen(!open)}
              className="menu-toggle"
              aria-label="Toggle navigation"
            >
              {open ? <X /> : <Menu />}
            </button>
            <span className="breadcrumb">
              <Layers3 size={14} /> Workspace <span>/</span>{" "}
              <b>
                {nav.find((x) => x.href === pathname)?.label ||
                  "Developer tools"}
              </b>
            </span>
            <div className="top-status">
              <Mode health={health.data} error={health.error} />
              <span className="sandbox-label">
                <Activity size={13} />
                Sandbox
              </span>
            </div>
          </header>
          <main id="content">
            {state?.operator_seeded &&
              Number(state.operator_seed_run_count) > 0 && (
                <aside className="seed-notice" role="note">
                  <Activity size={16} />
                  <span>
                    This workspace includes {state.operator_seed_run_count}{" "}
                    operator-generated test sessions.
                  </span>
                </aside>
              )}
            {children}
          </main>
        </div>
      </div>
    </Session.Provider>
  );
}
