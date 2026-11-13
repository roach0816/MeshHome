import { useEffect, useRef, useState, type ComponentType } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  ChevronDown,
  CircleCheck,
  CircleMinus,
  CircleX,
  ClipboardCopy,
  FileDown,
  Info,
  Radio as RadioIcon,
  RefreshCw,
  Search,
  TriangleAlert,
} from "lucide-react";
import { api, type HealthItem, type HealthStatus, type LogLine, type LogPage } from "../lib/api";
import { useHealth } from "../lib/queries";
import { canCopy, cx } from "../lib/util";
import { Section, Select } from "./SettingsKit";
import { Badge, Button, ErrorText, Input } from "./ui";

// ---- shared -----------------------------------------------------------------------------------

const STATUS_ICON: Record<HealthStatus, { icon: ComponentType<{ className?: string }>; className: string; label: string }> = {
  ok: { icon: CircleCheck, className: "text-ok", label: "Working" },
  info: { icon: Info, className: "text-accent", label: "Note" },
  warn: { icon: TriangleAlert, className: "text-warn", label: "Needs attention" },
  bad: { icon: CircleX, className: "text-danger", label: "Not working" },
  off: { icon: CircleMinus, className: "text-muted", label: "Off" },
};

function StatusIcon({ status, className }: { status: HealthStatus; className?: string }) {
  const s = STATUS_ICON[status];
  return <s.icon aria-label={s.label} className={cx("size-5 shrink-0", s.className, className)} />;
}

/** "06:28:12" today, "Nov 12 06:28" otherwise. */
export function logTime(epoch: number, seconds = true): string {
  const d = new Date(epoch * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined });
  }
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function ago(epoch: number, now = Date.now() / 1000): string {
  const s = Math.max(0, Math.round(now - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// ---- the page ---------------------------------------------------------------------------------

/** Settings → Health & logs. */
export function HealthPage() {
  const [component, setComponent] = useState<string>("");
  const [problemsOnly, setProblemsOnly] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const showLog = (c: string, problems = false) => {
    setComponent(c);
    setProblemsOnly(problems);
    logRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  return (
    <>
      <HealthSection onShowLog={showLog} />
      <div ref={logRef} className="scroll-mt-4">
        <LogSection component={component} setComponent={setComponent} problemsOnly={problemsOnly} setProblemsOnly={setProblemsOnly} />
      </div>
    </>
  );
}

// ---- health -----------------------------------------------------------------------------------

function HealthSection({ onShowLog }: { onShowLog: (component: string, problems?: boolean) => void }) {
  const health = useHealth();
  const qc = useQueryClient();
  const r = health.data;
  const attention = r?.items.filter((i) => i.status === "bad" || i.status === "warn") ?? [];
  return (
    <Section
      title="Health"
      description="What's working, and what isn't. Checked every 30 seconds."
      info={
        <>
          <p>
            Each line is one part of MeshHome. Something that isn't working says what it means and what to do, and{" "}
            <strong>Show in log</strong> shows its lines in the log below.
          </p>
          <p>
            <strong>Copy report</strong> copies this page and the recent problems as text, to paste when asking for help. It
            contains no passwords or keys, but does include network addresses and names.
          </p>
        </>
      }
      actions={
        <>
          <ReportButtons />
          <Button
            variant="ghost"
            className="px-3"
            aria-label="Check again"
            title="Check again"
            onClick={() => {
              qc.invalidateQueries({ queryKey: ["health"] });
              qc.invalidateQueries({ queryKey: ["logs"] });
            }}
          >
            <RefreshCw className={cx("size-4", health.isFetching && "animate-spin")} />
          </Button>
        </>
      }
    >
      {health.error && <ErrorText error={health.error} />}
      {!r && !health.error && <p className="text-sm text-muted">Checking…</p>}
      {r && (
        <>
          <div
            className={cx(
              "flex items-center gap-3 rounded-lg border p-3",
              r.overall === "ok" && "border-ok/40 bg-ok/5",
              r.overall === "warn" && "border-warn/40 bg-warn/5",
              r.overall === "bad" && "border-danger/40 bg-danger/5",
            )}
            role="status"
          >
            <StatusIcon status={r.overall} className="size-6" />
            <div className="min-w-0 flex-1">
              <p className="font-medium">{r.summary}</p>
              {attention.length > 0 && <p className="text-sm text-muted">{attention.map((i) => i.title).join(" · ")}</p>}
            </div>
          </div>
          <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line">
            {r.items.map((item) => (
              <HealthRow key={item.id} item={item} onShowLog={onShowLog} />
            ))}
          </ul>
          <ProblemSummary problems={r.problems} onShowLog={onShowLog} />
          <p className="text-xs text-muted">
            MeshHome {r.version} · running for {ago(r.started_at).replace(" ago", "").replace("just now", "a moment")} · checked{" "}
            {logTime(r.checked_at)}
          </p>
        </>
      )}
    </Section>
  );
}

function HealthRow({ item, onShowLog }: { item: HealthItem; onShowLog: (c: string, problems?: boolean) => void }) {
  const problem = item.status === "bad" || item.status === "warn";
  const [open, setOpen] = useState(problem);
  // Open by itself when it goes wrong; stays as the user left it otherwise.
  const wasProblem = useRef(problem);
  useEffect(() => {
    if (problem && !wasProblem.current) setOpen(true);
    wasProblem.current = problem;
  }, [problem]);
  const hasMore = !!item.detail || item.fix.length > 0 || item.facts.length > 0 || !!item.link || !!item.logs;
  return (
    <li className={cx(problem && "bg-surface-2/40")}>
      <button
        type="button"
        className="flex min-h-14 w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-surface-2 disabled:hover:bg-transparent"
        aria-expanded={hasMore ? open : undefined}
        disabled={!hasMore}
        onClick={() => setOpen(!open)}
      >
        <StatusIcon status={item.status} className="mt-0.5" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium">{item.title}</span>
          <span className={cx("block text-sm", problem ? "text-ink" : "text-muted")}>{item.summary}</span>
        </span>
        {hasMore && <ChevronDown className={cx("mt-1 size-4 shrink-0 text-muted transition-transform", open && "rotate-180")} aria-hidden />}
      </button>
      {open && hasMore && (
        <div className="space-y-3 px-3 pb-3 pl-11 text-sm">
          {item.detail && <p className="text-muted">{item.detail}</p>}
          {item.fix.length > 0 && problem && (
            <div>
              <p className="font-medium">What to do</p>
              <ol className="mt-1 list-decimal space-y-1 pl-5">
                {item.fix.map((f) => (
                  <li key={f}>
                    <FixText text={f} />
                  </li>
                ))}
              </ol>
            </div>
          )}
          {item.fix.length > 0 && !problem && (
            <ul className="list-disc space-y-1 pl-5 text-muted">
              {item.fix.map((f) => (
                <li key={f}>
                  <FixText text={f} />
                </li>
              ))}
            </ul>
          )}
          {item.facts.length > 0 && (
            <dl className="divide-y divide-line rounded-lg border border-line bg-surface px-3">
              {item.facts.map((f, i) => (
                <div key={`${f.label}-${i}`} className="flex justify-between gap-4 py-1.5">
                  <dt className="shrink-0 text-muted">{f.label}</dt>
                  <dd className="min-w-0 break-words text-right">{f.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {(item.link || item.logs) && (
            <div className="flex flex-wrap gap-2">
              {item.link && (
                <Link
                  to={item.link}
                  className="inline-flex min-h-9 items-center rounded-lg border border-line bg-surface px-3 text-sm font-medium hover:bg-surface-2"
                >
                  {item.link_label ?? "Open settings"}
                </Link>
              )}
              {item.logs && (
                <button
                  type="button"
                  className="inline-flex min-h-9 items-center rounded-lg px-3 text-sm font-medium text-accent hover:bg-surface-2"
                  onClick={() => onShowLog(item.logs!)}
                >
                  Show in log
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/** Commands (e.g. "sudo meshhome radio-hat logs") in monospace, so they're easy to copy. */
function FixText({ text }: { text: string }) {
  const m = text.match(/^(.*?)(sudo [^,;]+|meshhome [a-z-]+[^,;]*)(.*)$/);
  if (!m) return <>{text}</>;
  return (
    <>
      {m[1]}
      <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.85em]">{m[2].trim()}</code>
      {m[3]}
    </>
  );
}

function ProblemSummary({
  problems,
  onShowLog,
}: {
  problems: { component: string; label: string; count: number; last_at: number; latest: string; level: string }[];
  onShowLog: (c: string, problems?: boolean) => void;
}) {
  if (problems.length === 0) {
    return <p className="text-sm text-muted">No warnings or errors in the log in the last 24 hours.</p>;
  }
  return (
    <div>
      <p className="text-sm font-medium">Warnings and errors in the last 24 hours</p>
      <ul className="mt-1.5 space-y-1">
        {problems.map((p) => (
          <li key={p.component}>
            <button
              type="button"
              onClick={() => onShowLog(p.component, true)}
              className="flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-surface-2"
            >
              <Badge tone={p.level === "WARNING" ? "warn" : "danger"}>{p.count}</Badge>
              <span className="min-w-0 flex-1">
                <span className="font-medium">{p.label}</span>
                <span className="text-muted"> · latest {ago(p.last_at)}: </span>
                <span className="break-words">{p.latest}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ReportButtons() {
  const [state, setState] = useState<"" | "busy" | "copied" | "failed">("");
  const fetchReport = async () => {
    const res = await fetch("/api/system/health/report", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  };
  const copy = async () => {
    setState("busy");
    try {
      await navigator.clipboard.writeText(await fetchReport());
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState(""), 2500);
  };
  const download = async () => {
    setState("busy");
    try {
      const blob = new Blob([await fetchReport()], { type: "text/plain" });
      const a = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
      a.href = URL.createObjectURL(blob);
      a.download = `meshhome-report-${stamp}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
      setState("");
    } catch {
      setState("failed");
      window.setTimeout(() => setState(""), 2500);
    }
  };
  return canCopy() ? (
    <Button onClick={copy} disabled={state === "busy"} className="px-3">
      <ClipboardCopy className="size-4" />
      {state === "copied" ? "Copied" : state === "failed" ? "Couldn't copy" : "Copy report"}
    </Button>
  ) : (
    <Button onClick={download} disabled={state === "busy"} className="px-3">
      <FileDown className="size-4" />
      {state === "failed" ? "Couldn't download" : "Download report"}
    </Button>
  );
}

// ---- the log ----------------------------------------------------------------------------------

type View = "history" | "recent";

function LogSection({
  component,
  setComponent,
  problemsOnly,
  setProblemsOnly,
}: {
  component: string;
  setComponent: (c: string) => void;
  problemsOnly: boolean;
  setProblemsOnly: (v: boolean) => void;
}) {
  const [view, setView] = useState<View>("history");
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQ(search.trim()), 300);
    return () => window.clearTimeout(t);
  }, [search]);

  const PAGE = 50;
  const logs = useInfiniteQuery({
    queryKey: ["logs", view, problemsOnly, component, q],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ view, level: problemsOnly ? "problems" : "all", limit: String(PAGE) });
      if (component) p.set("component", component);
      if (q) p.set("q", q);
      if (pageParam !== null) p.set(view === "history" ? "offset" : "before", String(pageParam));
      return api<LogPage>(`/api/system/logs?${p}`);
    },
    getNextPageParam: (last, pages) => {
      if (!last.more) return undefined;
      if (view === "history") return pages.reduce((n, pg) => n + pg.items.filter((i) => i.saved !== false).length, 0);
      return last.items[last.items.length - 1]?.seq ?? undefined;
    },
    // "Since start" has no realtime event for INFO lines: refresh it while it's shown.
    refetchInterval: view === "recent" ? 10_000 : false,
  });
  const pages = logs.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items);
  const components = pages[0]?.components ?? {};
  const first = pages[0];

  return (
    <Section
      title="Log"
      description={
        view === "history"
          ? "Problems and events (the radio connecting and going offline, MeshHome starting, settings saved), kept for 30 days."
          : "Everything MeshHome has logged since it started, newest first."
      }
      info={
        <>
          <p>
            <strong>History</strong> keeps warnings, errors and events for 30 days. A line that repeats (such as the radio
            retrying all night) is shown once, with how many times and since when.
          </p>
          <p>
            <strong>Since start</strong> shows every line since MeshHome last started, including routine ones, up to the
            last 2,000.
          </p>
          <p>
            On a Raspberry Pi, <code>sudo meshhome logs</code> shows the same log in a terminal, and{" "}
            <code>sudo meshhome radio-hat logs</code> the radio HAT software's own log.
          </p>
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label="Which log" className="inline-flex rounded-lg border border-line p-0.5">
          {(
            [
              ["history", "History"],
              ["recent", "Since start"],
            ] as const
          ).map(([v, label]) => (
            <button
              key={v}
              type="button"
              role="tab"
              aria-selected={view === v}
              onClick={() => setView(v)}
              className={cx(
                "min-h-9 rounded-md px-3 text-sm font-medium",
                view === v ? "bg-accent-soft text-accent" : "text-muted hover:text-ink",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="inline-flex min-h-9 items-center gap-2 text-sm">
          <input type="checkbox" checked={problemsOnly} onChange={(e) => setProblemsOnly(e.target.checked)} className="size-4" />
          Problems only
        </label>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <Select
          id="log-component"
          value={component}
          onChange={setComponent}
          options={[{ value: "", label: "All parts of MeshHome" }, ...Object.entries(components).map(([value, label]) => ({ value, label }))]}
        />
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            type="search"
            aria-label="Search the log"
            placeholder="Search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>

      {first?.error && <p className="rounded-lg border border-warn/40 bg-warn/5 p-2 text-sm">{first.error}</p>}
      {logs.error && <ErrorText error={logs.error} />}
      {logs.isLoading && <p className="text-sm text-muted">Loading…</p>}
      {!logs.isLoading && items.length === 0 && !logs.error && (
        <p className="rounded-lg border border-dashed border-line p-4 text-center text-sm text-muted">
          {q || component || problemsOnly ? "Nothing matches." : view === "history" ? "Nothing logged yet." : "Nothing yet."}
        </p>
      )}
      {items.length > 0 && (
        <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line">
          {items.map((e) => (
            <LogRow key={e.id !== null ? `h${e.id}` : `s${e.seq ?? ""}-${e.first_at}-${e.message}`} e={e} components={components} />
          ))}
        </ul>
      )}
      <div className="flex items-center justify-between gap-2 text-xs text-muted">
        <span>
          {view === "recent" && first?.since ? `Since MeshHome started at ${logTime(first.since, false)}` : ""}
        </span>
        {logs.hasNextPage && (
          <Button onClick={() => logs.fetchNextPage()} disabled={logs.isFetchingNextPage}>
            {logs.isFetchingNextPage ? "Loading…" : "Older lines"}
          </Button>
        )}
      </div>
    </Section>
  );
}

const LEVEL: Record<string, { tone: "muted" | "warn" | "danger"; label: string }> = {
  INFO: { tone: "muted", label: "Info" },
  WARNING: { tone: "warn", label: "Warning" },
  ERROR: { tone: "danger", label: "Error" },
  CRITICAL: { tone: "danger", label: "Critical" },
};

function LogRow({ e, components }: { e: LogLine; components: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const lvl = LEVEL[e.level] ?? LEVEL.INFO;
  const radioEvent = e.event === "radio";
  const repeated = e.count > 1;
  return (
    <li className={cx("px-3 py-2 text-sm", e.level !== "INFO" && "bg-surface-2/30")}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <time className="shrink-0 font-mono text-xs tabular-nums text-muted" dateTime={new Date(e.last_at * 1000).toISOString()} title={new Date(e.last_at * 1000).toLocaleString()}>
          {logTime(e.last_at)}
        </time>
        {e.level !== "INFO" && <Badge tone={lvl.tone}>{lvl.label}</Badge>}
        <span className="text-xs text-muted">{components[e.component] ?? e.component}</span>
        {e.saved === false && <span className="text-xs text-warn">not saved yet</span>}
      </div>
      <p className="mt-0.5 flex items-start gap-1.5 break-words">
        {radioEvent && <RadioIcon className="mt-0.5 size-4 shrink-0 text-muted" aria-label="Radio connection" />}
        <span className="min-w-0">{e.message}</span>
      </p>
      {repeated && (
        <p className="mt-0.5 text-xs text-muted">
          {e.count} times, from {logTime(e.first_at, false)} to {logTime(e.last_at, false)}
        </p>
      )}
      {e.detail && (
        <>
          <button type="button" className="mt-1 text-xs font-medium text-accent" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "Hide details" : "Details"}
          </button>
          {open && (
            <pre className="mt-1 max-h-72 overflow-auto rounded-lg bg-surface-2 p-2 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words">
              {e.detail}
            </pre>
          )}
        </>
      )}
    </li>
  );
}
