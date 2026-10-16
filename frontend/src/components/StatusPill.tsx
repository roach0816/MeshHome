import type { Status } from "../lib/api";
import type { SocketState } from "../lib/realtime";
import { cx } from "../lib/util";

export function radioSummary(status?: Status): { label: string; tone: "ok" | "warn" | "danger" | "muted" } {
  const r = status?.radio;
  if (!r) return { label: "Checking…", tone: "muted" };
  switch (r.state) {
    case "connected":
      return r.is_simulated ? { label: "Simulated radio", tone: "warn" } : { label: "Radio connected", tone: "ok" };
    case "connecting":
    case "starting":
      return { label: "Connecting…", tone: "muted" };
    case "backoff":
      return { label: "Radio offline · retrying", tone: "danger" };
    case "paused":
      return { label: "Paused for maintenance", tone: "warn" };
    case "not_configured":
      return { label: "No radio configured", tone: "muted" };
    case "disabled":
      return { label: "Radio disabled", tone: "muted" };
    case "lock_unavailable":
      return { label: "Read-only (another instance owns radio)", tone: "warn" };
  }
}

export function StatusPill({ status, socket }: { status?: Status; socket: SocketState }) {
  const s = radioSummary(status);
  return (
    <p className="flex items-center gap-1.5 text-xs text-muted" role="status" aria-live="polite">
      <span
        aria-hidden
        className={cx(
          "inline-block size-2 rounded-full",
          s.tone === "ok" && "bg-ok",
          s.tone === "warn" && "bg-warn",
          s.tone === "danger" && "bg-danger",
          s.tone === "muted" && "bg-muted/50",
        )}
      />
      <span className="truncate">{s.label}</span>
      {socket !== "open" && <span className="truncate">· {socket === "connecting" ? "syncing…" : "live updates paused"}</span>}
    </p>
  );
}
