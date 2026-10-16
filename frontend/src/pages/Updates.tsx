import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { CheckCircle2, ChevronLeft, CircleAlert, Download, ExternalLink, RefreshCw, RotateCcw } from "lucide-react";
import { api, type UpdateInfo, type UpdateState, type UpdateStatus } from "../lib/api";
import { useUpdateInfo } from "../lib/queries";
import { Badge, Button, Card, ErrorText } from "../components/ui";
import { Dialog } from "../components/Dialog";
import { MarkdownLite } from "../components/MarkdownLite";
import { RadioFirmwareSection } from "../components/RadioFirmware";
import { cx, formatDateTime, relativeSeconds } from "../lib/util";

const ACTIVE: UpdateState[] = ["queued", "downloading", "installing", "migrating", "restarting"];
const STAGES: { state: UpdateState; label: string }[] = [
  { state: "queued", label: "Queued" },
  { state: "downloading", label: "Download & verify" },
  { state: "installing", label: "Back up & install" },
  { state: "restarting", label: "Restart" },
  { state: "done", label: "Done" },
];

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-base font-semibold">{title}</h3>
        {aside}
      </div>
      <div className="mt-3 space-y-3">{children}</div>
    </Card>
  );
}

export function Updates() {
  const qc = useQueryClient();
  const info = useUpdateInfo();
  const [confirming, setConfirming] = useState(false);
  const [watching, setWatching] = useState(false);
  // True when this page started the update: an older status (e.g. a previous failure) must not
  // be shown as this attempt's result.
  const [startedHere, setStartedHere] = useState(false);
  const d = info.data;

  const check = useMutation({
    mutationFn: () => api<UpdateInfo>("/api/system/update?refresh=1"),
    onSuccess: (v) => qc.setQueryData(["update-info"], v),
  });
  const install = useMutation({
    mutationFn: (version: string) => api("/api/system/update", { json: { version } }),
    onSuccess: () => {
      setStartedHere(true);
      setWatching(true);
    },
  });

  // Resume showing progress if an update is already running (e.g. after a page reload).
  useEffect(() => {
    if (d?.status && ACTIVE.includes(d.status.state) && Date.now() / 1000 - d.status.updated_at < 1800) setWatching(true);
  }, [d?.status]);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
        <Link
          to="/settings"
          className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2 lg:hidden"
          aria-label="Back to settings"
        >
          <ChevronLeft className="size-6" />
        </Link>
        <h2 className="flex-1 px-1 py-2 text-base font-semibold">Software updates</h2>
        <Button onClick={() => check.mutate()} disabled={check.isPending || watching}>
          <RefreshCw className={cx("size-4", check.isPending && "animate-spin")} aria-hidden />
          <span className="hidden sm:inline">Check now</span>
        </Button>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-3xl space-y-4">
          <ErrorText error={info.error ?? check.error} />
          {d && (
            <Section title="This installation">
              <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-sm">
                <dt className="text-muted">Installed version</dt>
                <dd className="font-medium">v{d.current_version}</dd>
                <dt className="text-muted">Installed as</dt>
                <dd>
                  {d.install_kind === "native" ? "Native (Debian / Raspberry Pi)" : "Container (Docker / Kubernetes)"}
                  {d.legacy_layout && (
                    <span className="block text-xs text-muted">
                      Still uses the names from before the rename to MeshHome. To move it to the new ones, run{" "}
                      <code className="font-mono">sudo meshcore-home migrate</code> on the device.
                    </span>
                  )}
                </dd>
                <dt className="text-muted">Last checked</dt>
                <dd>
                  {d.checked_at ? relativeSeconds(d.checked_at) : d.checks_enabled ? "Not yet" : "Update checks are disabled"}
                  {d.error && <span className="block text-xs text-warn">Couldn't reach GitHub: {d.error}</span>}
                </dd>
              </dl>
            </Section>
          )}

          {watching && d && (
            <Progress
              initial={startedHere ? null : d.status}
              target={install.variables ?? d.status?.version ?? d.latest?.version ?? ""}
              command={d.command}
            />
          )}

          {d && !watching && d.update_available && d.latest && (
            <Section
              title={`Version ${d.latest.version} is available`}
              aside={<Badge tone="accent">New</Badge>}
            >
              <p className="text-sm text-muted">
                Released {d.latest.published_at ? formatDateTime(d.latest.published_at) : "recently"} ·{" "}
                <a href={d.latest.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent underline">
                  Full release notes <ExternalLink className="size-3.5" aria-hidden />
                </a>
              </p>
              <ReleaseNotes text={d.latest.notes} />
              {d.can_install ? (
                <div className="flex flex-wrap items-center gap-3 pt-1">
                  <Button variant="primary" onClick={() => setConfirming(true)} disabled={install.isPending}>
                    <Download className="size-4" aria-hidden /> Install v{d.latest.version}
                  </Button>
                  <span className="text-xs text-muted">Takes a few minutes. The app restarts once.</span>
                </div>
              ) : d.install_kind === "native" ? (
                <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
                  This release doesn't have a Raspberry Pi / Debian package yet. Check again in a few minutes, or upgrade
                  from a terminal: <code className="font-mono">sudo {d.command ?? "meshhome"} update</code>
                </p>
              ) : (
                <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
                  This installation runs in a container, so it's updated by redeploying rather than from here. With
                  Rancher Continuous Delivery that happens automatically once the new image is pinned; with Docker
                  Compose, pull the new version and run <code className="font-mono">docker compose up -d --build</code>.
                </p>
              )}
              <ErrorText error={install.error} />
            </Section>
          )}

          {d && !watching && !d.update_available && d.checked_at && !d.error && (
            <Card className="flex items-start gap-3 p-4 sm:p-5">
              <CheckCircle2 className="mt-0.5 size-6 shrink-0 text-ok" aria-hidden />
              <div className="space-y-1 text-sm">
                <p>You're up to date. v{d.current_version} is the latest release.</p>
                <p className="text-muted">
                  {d.install_kind === "native"
                    ? "When a new version is released, an Install button appears here — this installation can update itself."
                    : "This installation runs in a container (Docker / Kubernetes), so new versions arrive by redeploying — with Rancher Continuous Delivery that happens automatically."}
                </p>
              </div>
            </Card>
          )}

          {d?.installed && !watching && (
            <Section
              title={`What's new in v${d.installed.version}`}
              aside={<Badge>{d.update_available ? "Installed" : "Current"}</Badge>}
            >
              <p className="text-sm text-muted">
                Released {d.installed.published_at ? formatDateTime(d.installed.published_at) : "—"} ·{" "}
                <a href={d.installed.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent underline">
                  View on GitHub <ExternalLink className="size-3.5" aria-hidden />
                </a>
              </p>
              <ReleaseNotes text={d.installed.notes} />
            </Section>
          )}

          {d?.status && !watching && (d.status.state === "failed" || d.status.state === "rolled_back") && (
            <Section title="Last update attempt">
              <StatusDetail st={d.status} command={d.command} />
            </Section>
          )}

          <RadioFirmwareSection />
        </div>
      </div>

      {confirming && d?.latest && (
        <Dialog
          size="sm"
          title={`Install v${d.latest.version}?`}
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  setConfirming(false);
                  install.mutate(d.latest!.version);
                }}
              >
                Install update
              </Button>
            </>
          }
        >
          <ol className="list-decimal space-y-1.5 pl-5 text-sm">
            <li>Downloads v{d.latest.version} from GitHub and verifies its checksum.</li>
            <li>Backs up the database.</li>
            <li>Installs it alongside the current version, then restarts the app (a minute or so offline).</li>
            <li>If the new version doesn't start, the current version is restored automatically.</li>
          </ol>
          <p className="mt-3 text-xs text-muted">
            Messages the radio receives during the restart wait on the radio and are collected afterwards.
          </p>
        </Dialog>
      )}
    </div>
  );
}

function ReleaseNotes({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const notes = text.trim();
  if (!notes) return null;
  const long = notes.split("\n").length > 14 || notes.length > 1200;
  return (
    <div>
      {/* Release notes: lightweight Markdown built as React elements, never interpreted as HTML. */}
      <div
        className={cx(
          "break-words rounded-lg bg-surface-2 px-4 py-3",
          long && !open && "max-h-72 overflow-hidden [mask-image:linear-gradient(to_bottom,black_75%,transparent)]",
        )}
      >
        <MarkdownLite text={notes} />
      </div>
      {long && (
        <button onClick={() => setOpen(!open)} className="mt-1 text-xs text-accent underline">
          {open ? "Show less" : "Show all release notes"}
        </button>
      )}
    </div>
  );
}

function StatusDetail({ st, command = "meshhome" }: { st: UpdateStatus; command?: string }) {
  const bad = st.state === "failed" || st.state === "rolled_back";
  return (
    <div className="space-y-2 text-sm">
      <p className={cx("flex items-start gap-2", bad ? "text-danger" : "text-ink")}>
        {st.state === "rolled_back" ? (
          <RotateCcw className="mt-0.5 size-4 shrink-0" aria-hidden />
        ) : bad ? (
          <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
        ) : null}
        {st.message}
      </p>
      {st.log_tail && st.log_tail.length > 0 && (
        <details className="rounded-lg bg-surface-2 px-3 py-2 text-xs">
          <summary className="cursor-pointer text-muted">Installer log</summary>
          <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug">
            {st.log_tail.join("\n")}
          </pre>
        </details>
      )}
      {bad && (
        <p className="text-xs text-muted">
          Full details: <code className="font-mono">{command} logs</code> and{" "}
          <code className="font-mono">/var/log/{command}-install.log</code> on the device.
        </p>
      )}
    </div>
  );
}

/** Polls the updater's status file through the API, riding through the app's own restart. */
function Progress({ initial, target, command }: { initial: UpdateStatus | null; target: string; command?: string }) {
  const qc = useQueryClient();
  const [offline, setOffline] = useState(false);
  const started = useRef(Date.now() / 1000);
  const q = useQuery({
    queryKey: ["update-status"],
    queryFn: async () => {
      try {
        const r = await api<{ current_version: string; status: UpdateStatus | null }>("/api/system/update/status");
        setOffline(false);
        return r;
      } catch (e) {
        setOffline(true); // expected while the app restarts
        throw e;
      }
    },
    refetchInterval: 2000,
    retry: false,
  });
  // Ignore a status left over from an earlier update until the new request is picked up.
  const fresh = q.data?.status && q.data.status.updated_at >= started.current - 5 ? q.data.status : null;
  const st = fresh ?? initial;
  const finished = st?.state === "done" && q.data?.current_version === target;
  const failed = st?.state === "failed" || st?.state === "rolled_back";

  useEffect(() => {
    if (!finished) return;
    // The new version may ship a new interface: load it.
    const t = setTimeout(() => location.reload(), 2500);
    qc.invalidateQueries();
    return () => clearTimeout(t);
  }, [finished, qc]);

  const stageIndex = finished ? STAGES.length - 1 : Math.max(0, STAGES.findIndex((s) => s.state === (st?.state === "migrating" ? "installing" : st?.state)));
  return (
    <Section title={finished ? `Updated to v${target}` : failed ? "Update did not complete" : `Updating to v${target}…`}>
      <ol className="flex flex-wrap gap-x-2 gap-y-2" aria-label="Update progress">
        {STAGES.map((s, i) => {
          const done = finished || (!failed && i < stageIndex);
          const current = !finished && !failed && i === stageIndex;
          return (
            <li key={s.state} className="flex items-center gap-2 text-xs">
              <span
                className={cx(
                  "flex size-6 items-center justify-center rounded-full border text-[11px] font-semibold",
                  done && "border-accent bg-accent text-accent-fg",
                  current && "border-accent text-accent",
                  !done && !current && "border-line text-muted",
                )}
                aria-current={current ? "step" : undefined}
              >
                {done ? "✓" : current ? <RefreshCw className="size-3 animate-spin" aria-hidden /> : i + 1}
              </span>
              <span className={cx(current ? "font-medium text-ink" : "text-muted")}>{s.label}</span>
              {i < STAGES.length - 1 && <span className="hidden h-px w-4 bg-line sm:block" aria-hidden />}
            </li>
          );
        })}
      </ol>
      {finished ? (
        <p className="flex items-center gap-2 text-sm text-ok" role="status">
          <CheckCircle2 className="size-4" aria-hidden /> Done. Reloading the new version…
        </p>
      ) : failed && st ? (
        <StatusDetail st={st} command={command} />
      ) : (
        <p className="text-sm text-muted" role="status" aria-live="polite">
          {offline ? "The app is restarting — reconnecting…" : st?.message ?? "Waiting for the updater to start…"}
        </p>
      )}
    </Section>
  );
}
