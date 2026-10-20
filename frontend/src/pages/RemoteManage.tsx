import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  Cpu,
  Fingerprint,
  Globe,
  Info,
  KeyRound,
  ListTree,
  Loader2,
  LogOut,
  MapPin,
  Megaphone,
  Network,
  Power,
  Radio,
  RefreshCw,
  Repeat,
  Save,
  Send,
  ShieldCheck,
  Thermometer,
  Timer,
  Trash2,
  User,
  Users,
} from "lucide-react";
import { api, type PresetList, type RemoteKind, type RemoteState } from "../lib/api";
import { Badge, Button, Card, ErrorText, Field, IconButton, Input } from "../components/ui";
import { Dialog } from "../components/Dialog";
import { NodeDetails } from "../components/NodeDetails";
import { Section, Select } from "../components/SettingsKit";
import { BANDWIDTHS, CUSTOM, matchPreset } from "../lib/presets";
import { canCopy, cx } from "../lib/util";

// Leaflet is only downloaded when the map picker opens.
const LocationPicker = lazy(() => import("../components/LocationPicker").then((m) => ({ default: m.LocationPicker })));

const KIND: Record<number, string> = { 2: "Repeater", 3: "Room server" };
const PERMS = [
  { value: 0, label: "Guest" },
  { value: 1, label: "Read only" },
  { value: 2, label: "Read / write" },
  { value: 3, label: "Admin" },
];
type Tab = "status" | "cli" | "settings";

// ---- helpers ----------------------------------------------------------------------------

function useNow(ms = 30_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

function ago(iso: string | undefined, now: number): string {
  if (!iso) return "Not requested yet";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return "Fetched just now";
  if (s < 3600) return `Fetched ${Math.round(s / 60)} min ago`;
  if (s < 86400) return `Fetched ${Math.round(s / 3600)} h ago`;
  return `Fetched ${Math.round(s / 86400)} d ago`;
}

/** Estimated charge of a Li-ion cell from its voltage (mV): linear from 3.0 V (0%) to 4.2 V (100%),
 * rounded down, which matches the MeshCore app (4.10 V shows as 91%). */
export function batteryPercent(mV: number): number {
  return Math.max(0, Math.min(100, Math.floor(((mV - 3000) / 1200) * 100)));
}

function duration(secs: number): string {
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${secs % 60}s`;
}

/** The value in a "get" reply ("> value"), or null for anything else (an error message). */
const getValue = (reply: string | null | undefined) => (reply && reply.startsWith("> ") ? reply.slice(2).trim() : null);
const isOk = (reply: string | null | undefined) => !!reply && /^ok\b/i.test(reply.trim());

function useRemote(id: string) {
  return useQuery({
    queryKey: ["remote", id],
    queryFn: () => api<RemoteState>(`/api/remote/${id}`),
  });
}

/** Every mesh request shares one key: only one runs at a time, and all buttons wait for it. */
function useBusy(id: string) {
  return useIsMutating({ mutationKey: ["remote-op", id] }) > 0;
}

function useCli(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ["remote-op", id],
    mutationFn: (command: string) =>
      api<{ reply: string | null }>(`/api/remote/${id}/cli`, { method: "POST", json: { command } }).then((r) => r.reply),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
}

function useRequest(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ["remote-op", id],
    mutationFn: (args: { kind: RemoteKind; offset?: number }) =>
      api(`/api/remote/${id}/request`, { method: "POST", json: args }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
}

function RefreshIcon({ onClick, pending, disabled, label }: { onClick: () => void; pending: boolean; disabled: boolean; label: string }) {
  return (
    <IconButton label={label} onClick={onClick} disabled={disabled}>
      <RefreshCw className={cx("size-5", pending && "animate-spin text-accent")} aria-hidden />
    </IconButton>
  );
}

function SaveIcon({ onClick, pending, disabled, label = "Save to node" }: { onClick: () => void; pending: boolean; disabled: boolean; label?: string }) {
  return (
    <IconButton label={label} onClick={onClick} disabled={disabled} className={cx(!disabled && "text-accent")}>
      {pending ? <Loader2 className="size-5 animate-spin" aria-hidden /> : <Save className="size-5" aria-hidden />}
    </IconButton>
  );
}

/** A reply that is not a plain "OK" or a value: show it, the node is telling us something. */
function Reply({ reply }: { reply: string | null | undefined }) {
  if (reply === undefined) return null;
  if (reply === null) return <p className="text-xs text-muted">Sent. The node does not reply to this command.</p>;
  if (isOk(reply)) {
    return (
      <p className="flex items-center gap-1 text-xs text-ok" role="status">
        <Check className="size-3.5" aria-hidden /> {reply}
      </p>
    );
  }
  if (getValue(reply) !== null) return null;
  return (
    <p className="rounded-md bg-warn/10 px-2 py-1 font-mono text-xs text-ink" role="status">
      {reply}
    </p>
  );
}

function SectionHead({ title, at, now, children }: { title: string; at?: string; now: number; children?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-0 flex-1">
        <h3 className="text-base font-semibold">{title}</h3>
        <p className="text-xs text-muted">{ago(at, now)}</p>
      </div>
      {children}
    </div>
  );
}

/** Keep an unexpected current value selectable, and show "—" until the value has been read. */
function selectOptions(options: { value: string; label: string }[], value: string) {
  if (!value) return [{ value: "", label: "— tap refresh to read" }, ...options];
  return options.some((o) => o.value === value) ? options : [{ value, label: value }, ...options];
}

// ---- a node setting read with "get <key>" and written with "set <key> <value>" ------------------

type SettingProps = {
  id: string;
  state: RemoteState;
  setting: string;
  label: string;
  hint?: ReactNode;
  options?: { value: string; label: string }[];
  inputMode?: "text" | "decimal" | "numeric";
  secret?: boolean;
  maxLength?: number;
  validate?: (v: string) => string | null;
};

function CliSetting({ id, state, setting, label, hint, options, inputMode = "text", secret, maxLength = 64, validate }: SettingProps) {
  const now = useNow();
  const busy = useBusy(id);
  const cached = state.values[setting];
  // What the node last said: the server's cache, or (for secrets, which it does not cache) the
  // reply to our own read. The user's edit is kept separately so a refetch never overwrites it.
  const [local, setLocal] = useState<{ value: string; at: string } | null>(null);
  const known = local && (!cached || local.at > cached.at) ? local : cached;
  const [draft, setDraft] = useState<string | null>(null);
  const value = draft ?? known?.value ?? "";
  const setValue = (v: string) => setDraft(v);
  const fetchedAt = known?.at;
  const read = useCli(id);
  const write = useCli(id);
  const error = value && validate ? validate(value) : null;
  const dirty = value.trim() !== "" && value.trim() !== (known?.value ?? "");
  const htmlId = `rs-${setting.replace(/\W/g, "-")}`;

  const refresh = () => {
    const before = draft;
    read.mutate(`get ${setting}`, {
      onSuccess: (reply) => {
        const v = getValue(reply);
        if (v !== null) {
          setLocal({ value: v, at: new Date().toISOString() });
          setDraft((d) => (d === before ? null : d)); // keep anything typed while waiting
        }
      },
    });
  };
  const save = () => {
    const v = value.trim();
    write.mutate(`set ${setting} ${v}`, {
      onSuccess: (reply) => {
        if (isOk(reply)) {
          setLocal({ value: v, at: new Date().toISOString() });
          setDraft(null);
        }
      },
    });
  };
  const unsupported = read.data?.startsWith("??") || read.data?.toLowerCase().startsWith("unknown");

  return (
    <Field label={label} htmlFor={htmlId} hint={hint} error={error}>
      <div className="flex items-center gap-1">
        <div className="min-w-0 flex-1">
          {options ? (
            <Select id={htmlId} value={value} options={selectOptions(options, value)} onChange={setValue} />
          ) : (
            <Input
              id={htmlId}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              inputMode={inputMode}
              maxLength={maxLength}
              placeholder={fetchedAt ? "" : "Tap refresh to read"}
              type={secret ? "text" : undefined}
              autoComplete="off"
              spellCheck={false}
            />
          )}
        </div>
        <RefreshIcon label={`Read ${label.toLowerCase()} from the node`} onClick={refresh} pending={read.isPending} disabled={busy} />
        <SaveIcon onClick={save} pending={write.isPending} disabled={busy || !dirty || !!error} />
      </div>
      <p className="text-xs text-muted">{ago(fetchedAt, now)}</p>
      {unsupported ? <p className="text-xs text-warn">This firmware does not know “{setting}”.</p> : <Reply reply={read.data && !getValue(read.data) ? read.data : undefined} />}
      <Reply reply={write.data} />
      <ErrorText error={read.error || write.error} />
    </Field>
  );
}

// ---- page -------------------------------------------------------------------------------

export function RemoteManage() {
  const { id = "" } = useParams();
  const remote = useRemote(id);
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("status");
  const busy = useBusy(id);
  const logout = useMutation({
    mutationKey: ["remote-op", id],
    mutationFn: () => api(`/api/remote/${id}/logout`, { method: "POST" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
  const s = remote.data;

  return (
    <div className="flex h-full flex-col">
      <header className="pwa-top flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
        <Link to="/contacts" className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2" aria-label="Back to contacts">
          <ChevronLeft className="size-6" />
        </Link>
        <div className="min-w-0 flex-1 px-1">
          <h2 className="truncate text-base font-semibold">{s?.contact.name ?? "Remote manage"}</h2>
          <p className="truncate text-xs text-muted">
            {s ? `${KIND[s.contact.kind] ?? "Node"} · Remote manage` : "Remote administration over the mesh"}
          </p>
        </div>
        {s?.session && (
          <>
            <Badge tone={s.session.admin ? "accent" : "muted"} className="hidden sm:inline-flex">
              {s.session.admin ? "Admin" : "Guest"}
            </Badge>
            <Button variant="ghost" onClick={() => logout.mutate()} disabled={busy} title="Log out of this node">
              <LogOut className="size-4" aria-hidden />
              <span className="hidden sm:inline">Log out</span>
            </Button>
          </>
        )}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {remote.isPending && <p className="text-sm text-muted">Loading…</p>}
          <ErrorText error={remote.error || logout.error} />
          {s && (
            <>
              {!s.radio_connected && (
                <div className="rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm" role="status">
                  Your radio is not connected, so nothing can be sent to this node right now. Showing what was last fetched.
                </div>
              )}
              <div className="flex items-start gap-2 rounded-xl border border-accent/30 bg-accent-soft px-4 py-3 text-sm">
                <Info className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
                <span>
                  To keep mesh traffic low, nothing is requested automatically. Use the{" "}
                  <RefreshCw className="inline size-3.5 align-[-2px]" aria-label="refresh" /> icons to fetch only what you need. Each
                  request is sent over the air and can take several seconds.
                  {s.simulated && (
                    <>
                      {" "}
                      <Badge tone="warn">Simulated node</Badge>
                    </>
                  )}
                </span>
              </div>
              {!s.session ? (
                <LoginCard id={id} state={s} />
              ) : (
                <>
                  <div role="tablist" aria-label="Remote manage" className="grid grid-cols-3 gap-1 rounded-xl border border-line bg-surface p-1">
                    {(
                      [
                        ["status", "Status"],
                        ["cli", "Command line"],
                        ["settings", "Settings"],
                      ] as const
                    ).map(([t, label]) => (
                      <button
                        key={t}
                        role="tab"
                        aria-selected={tab === t}
                        onClick={() => setTab(t)}
                        className={cx(
                          "min-h-10 rounded-lg px-2 text-sm font-medium transition-colors",
                          tab === t ? "bg-accent text-accent-fg" : "text-muted hover:bg-surface-2 hover:text-ink",
                        )}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <fieldset disabled={!s.radio_connected} className="m-0 min-w-0 space-y-4 border-0 p-0">
                    {tab === "status" && <StatusTab id={id} state={s} />}
                    {tab !== "status" && !s.session.admin && (
                      <Card className="p-5 text-sm text-muted">
                        You are logged in as a guest. Log out and log in with the admin password to use the command line and settings.
                      </Card>
                    )}
                    {tab === "cli" && s.session.admin && <CliTab id={id} state={s} />}
                    {tab === "settings" && s.session.admin && <SettingsTab id={id} state={s} />}
                  </fieldset>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---- login -------------------------------------------------------------------------------

function LoginCard({ id, state }: { id: string; state: RemoteState }) {
  const qc = useQueryClient();
  const busy = useBusy(id);
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationKey: ["remote-op", id],
    mutationFn: () => api(`/api/remote/${id}/login`, { method: "POST", json: { password } }),
    onSuccess: () => setPassword(""),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
  return (
    <Section
      title={`Log in to ${state.contact.name}`}
      description="The admin password gives full control; a guest password (if the node has one) allows reading its status."
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <Field
          label="Password"
          htmlFor="remote-password"
          hint="Sent to the node encrypted, over the mesh. MeshHome does not store it. New repeaters use the admin password “password” until it is changed."
        >
          <Input
            id="remote-password"
            type="password"
            autoComplete="off"
            value={password}
            maxLength={64}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <ErrorText error={login.error} />
        <Button variant="primary" type="submit" disabled={busy || !state.radio_connected}>
          {login.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <KeyRound className="size-4" aria-hidden />}
          {login.isPending ? "Waiting for the node…" : "Log in"}
        </Button>
      </form>
    </Section>
  );
}

// ---- status ------------------------------------------------------------------------------

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg bg-surface-2 px-3 py-2">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="text-sm font-medium tabular-nums">{value}</dd>
    </div>
  );
}

function StatusTab({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const req = useRequest(id);
  const st = state.sections.status;
  const d = st?.data;
  return (
    <Card className="space-y-4 p-4 sm:p-5">
      <SectionHead title="Status" at={st?.at} now={now}>
        <RefreshIcon label="Request status" onClick={() => req.mutate({ kind: "status" })} pending={req.isPending} disabled={busy} />
      </SectionHead>
      <ErrorText error={req.error} />
      {d ? (
        <>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Metric label="Battery" value={`${batteryPercent(d.bat)}% / ${(d.bat / 1000).toFixed(2)} V`} />
            <Metric label="Uptime" value={duration(d.uptime)} />
            <Metric label="Queue" value={d.tx_queue_len} />
            <Metric label="Last RSSI" value={`${d.last_rssi} dBm`} />
            <Metric label="Last SNR" value={`${d.last_snr} dB`} />
            <Metric label="Noise floor" value={`${d.noise_floor} dBm`} />
          </dl>
          <h4 className="text-sm font-semibold">Packets</h4>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Metric label="Received" value={d.nb_recv.toLocaleString()} />
            <Metric label="Sent" value={d.nb_sent.toLocaleString()} />
            <Metric label="Receive errors" value={d.recv_errors.toLocaleString()} />
            <Metric label="Flood sent / received" value={`${d.sent_flood.toLocaleString()} / ${d.recv_flood.toLocaleString()}`} />
            <Metric label="Direct sent / received" value={`${d.sent_direct.toLocaleString()} / ${d.recv_direct.toLocaleString()}`} />
            <Metric label="Duplicates (flood / direct)" value={`${d.flood_dups.toLocaleString()} / ${d.direct_dups.toLocaleString()}`} />
            <Metric label="Airtime TX" value={duration(d.airtime)} />
            <Metric label="Airtime RX" value={duration(d.rx_airtime)} />
            <Metric label="Queue full events" value={d.full_evts} />
          </dl>
        </>
      ) : (
        <p className="text-sm text-muted">Tap refresh to request the node's status (one short exchange over the air).</p>
      )}
    </Card>
  );
}

// ---- command line ----------------------------------------------------------------------------

const QUICK = ["ver", "clock", "get radio", "get repeat", "neighbors", "stats-core"];

function CliTab({ id, state }: { id: string; state: RemoteState }) {
  const qc = useQueryClient();
  const busy = useBusy(id);
  const cli = useCli(id);
  const [cmd, setCmd] = useState("");
  const history = useRef<string[]>([]);
  const pos = useRef(-1);
  const end = useRef<HTMLDivElement>(null);
  const clear = useMutation({
    mutationFn: () => api(`/api/remote/${id}/console`, { method: "DELETE" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [state.console.length, cli.isPending]);
  const send = (c: string) => {
    const command = c.trim();
    if (!command) return;
    history.current = [command, ...history.current.filter((h) => h !== command)].slice(0, 30);
    pos.current = -1;
    setCmd("");
    cli.mutate(command);
  };
  return (
    <Card className="space-y-3 p-4 sm:p-5">
      <div className="flex items-center gap-2">
        <h3 className="flex-1 text-base font-semibold">Command line</h3>
        <Button variant="ghost" onClick={() => clear.mutate()} disabled={!state.console.length}>
          <Trash2 className="size-4" aria-hidden /> Clear
        </Button>
      </div>
      <div
        className="h-80 overflow-y-auto rounded-lg border border-line bg-surface-2 p-3 font-mono text-xs leading-5"
        role="log"
        aria-live="polite"
        aria-label="Console"
      >
        {state.console.length === 0 && !cli.isPending && (
          <p className="text-muted">Commands you send and the node's replies appear here. Passwords and keys are hidden.</p>
        )}
        {state.console.map((l, i) => (
          <div
            key={i}
            className={cx("whitespace-pre-wrap break-words", l.dir === "out" && "text-accent", l.dir === "note" && "italic text-muted")}
          >
            {l.dir === "out" ? `> ${l.text}` : l.text}
          </div>
        ))}
        {cli.isPending && (
          <div className="flex items-center gap-1 text-muted">
            <Loader2 className="size-3 animate-spin" aria-hidden /> waiting for the node…
          </div>
        )}
        <div ref={end} />
      </div>
      <ErrorText error={cli.error} />
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(cmd);
        }}
      >
        <Input
          aria-label="Command"
          value={cmd}
          maxLength={160}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          className="font-mono"
          placeholder="e.g. get advert.interval"
          onChange={(e) => setCmd(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowUp" || e.key === "ArrowDown") {
              const h = history.current;
              if (!h.length) return;
              e.preventDefault();
              pos.current = Math.max(-1, Math.min(h.length - 1, pos.current + (e.key === "ArrowUp" ? 1 : -1)));
              setCmd(pos.current < 0 ? "" : h[pos.current]);
            }
          }}
        />
        <Button variant="primary" type="submit" disabled={busy || !cmd.trim()}>
          <Send className="size-4" aria-hidden />
          <span className="hidden sm:inline">Send</span>
        </Button>
      </form>
      <div className="flex flex-wrap gap-1.5">
        {QUICK.map((q) => (
          <button
            key={q}
            type="button"
            disabled={busy}
            onClick={() => send(q)}
            className="rounded-full border border-line px-3 py-1 font-mono text-xs text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-50"
          >
            {q}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted">
        One command per send. See MeshCore's{" "}
        <a className="text-accent underline" href="https://github.com/meshcore-dev/MeshCore/blob/main/docs/cli_commands.md" target="_blank" rel="noreferrer">
          CLI reference
        </a>
        .
      </p>
    </Card>
  );
}

// ---- settings ------------------------------------------------------------------------------

function SettingsTab({ id, state }: { id: string; state: RemoteState }) {
  const [copied, setCopied] = useState(false);
  return (
    <>
      <Section title="Public info">
        <CliSetting id={id} state={state} setting="name" label="Name" maxLength={31} />
        <Field label="Public key" htmlFor="rs-pubkey">
          <div className="flex items-center gap-1">
            <Input id="rs-pubkey" readOnly value={state.contact.public_key} className="font-mono text-xs" />
            {canCopy() && (
            <IconButton
              label={copied ? "Copied" : "Copy public key"}
              onClick={() => {
                navigator.clipboard?.writeText(state.contact.public_key);
                setCopied(true);
              }}
            >
              {copied ? <Check className="size-5 text-ok" aria-hidden /> : <Copy className="size-5" aria-hidden />}
            </IconButton>
            )}
          </div>
        </Field>
      </Section>
      <RadioSettings id={id} state={state} />
      <Card className="p-4 sm:p-5">
        <h3 className="text-base font-semibold">Extra tools</h3>
        <p className="mt-0.5 text-sm text-muted">Open a tool, then use its refresh icon to read the current values.</p>
        <div className="mt-3 divide-y divide-line">
          <Tool icon={<User />} title="Owner info" summary="Contact details shown to anyone who asks">
            <OwnerInfo id={id} state={state} />
          </Tool>
          <Tool icon={<Megaphone />} title="Advert" summary="Announce the node to the mesh now">
            <AdvertTool id={id} />
          </Tool>
          <Tool icon={<Timer />} title="Advert intervals" summary="How often the node announces itself">
            <CliSetting id={id} state={state} setting="advert.interval" label="Zero-hop advert interval (minutes)" inputMode="numeric" hint="60–240 minutes, or 0 to turn off." validate={range(0, 240)} />
            <CliSetting id={id} state={state} setting="flood.advert.interval" label="Flood advert interval (hours)" inputMode="numeric" hint="3–168 hours, or 0 to turn off. Flood adverts reach the whole mesh: keep this long." validate={range(0, 168)} />
          </Tool>
          <Tool icon={<MapPin />} title="Position" summary="Where the node says it is">
            <PositionTool id={id} state={state} />
          </Tool>
          <Tool icon={<Clock />} title="Sync clock" summary="Set the node's clock from this device">
            <ClockTool id={id} state={state} />
          </Tool>
          <Tool icon={<ShieldCheck />} title="Access control" summary="Who may log in, and with what rights">
            <AclTool id={id} state={state} />
          </Tool>
          <Tool icon={<KeyRound />} title="Admin password" summary="Change the password for full control">
            <AdminPassword id={id} />
          </Tool>
          <Tool icon={<Users />} title="Guest password" summary="Read-only access for others">
            <CliSetting id={id} state={state} setting="guest.password" label="Guest password" secret hint="Leave as it is if you don't want guests. Read and saved values are not stored by MeshHome." />
          </Tool>
          <Tool icon={<Fingerprint />} title="Change identity key" summary="Give the node a new key, with a prefix you choose">
            <IdentityTool id={id} />
          </Tool>
          <Tool icon={<Globe />} title="Manage regions" summary="Regions this node repeats floods for">
            <RegionsTool id={id} state={state} />
          </Tool>
          <Tool icon={<Network />} title="Neighbours" summary="Repeaters this node hears directly">
            <NeighboursTool id={id} state={state} />
          </Tool>
          <Tool icon={<ListTree />} title="Network settings" summary="Routing and timing">
            <CliSetting id={id} state={state} setting="path.hash.mode" label="Path hash size" options={[{ value: "0", label: "1 byte" }, { value: "1", label: "2 bytes" }, { value: "2", label: "3 bytes" }]} hint="Must match the rest of your mesh." />
            <CliSetting id={id} state={state} setting="txdelay" label="Flood transmit delay factor" inputMode="decimal" />
            <CliSetting id={id} state={state} setting="direct.txdelay" label="Direct transmit delay factor" inputMode="decimal" />
            <CliSetting id={id} state={state} setting="af" label="Airtime factor" inputMode="decimal" hint="Higher values make the node wait longer between transmissions." />
            <CliSetting id={id} state={state} setting="loop.detect" label="Loop detection" options={[{ value: "off", label: "Off" }, { value: "minimal", label: "Minimal" }, { value: "moderate", label: "Moderate" }, { value: "strict", label: "Strict" }]} />
            <CliSetting id={id} state={state} setting="multi.acks" label="Extra ACKs" options={[{ value: "0", label: "Off" }, { value: "1", label: "On" }]} />
            <CliSetting id={id} state={state} setting="int.thresh" label="Interference threshold" inputMode="numeric" />
          </Tool>
          <Tool icon={<Repeat />} title="Repeat settings" summary="Whether and how far the node repeats">
            <CliSetting id={id} state={state} setting="repeat" label="Repeat packets" options={[{ value: "on", label: "On" }, { value: "off", label: "Off" }]} />
            <CliSetting id={id} state={state} setting="flood.max" label="Maximum flood hops" inputMode="numeric" validate={range(0, 64)} />
            <CliSetting id={id} state={state} setting="flood.max.unscoped" label="Maximum hops for floods without a region" inputMode="numeric" validate={range(0, 64)} />
            <CliSetting id={id} state={state} setting="flood.max.advert" label="Maximum hops for flood adverts" inputMode="numeric" validate={range(0, 64)} />
          </Tool>
          <Tool icon={<Thermometer />} title="Telemetry" summary="Battery, sensors and position">
            <TelemetryTool id={id} state={state} />
          </Tool>
          <Tool icon={<Power />} title="Reboot" summary="Restart the node">
            <RebootTool id={id} />
          </Tool>
          <Tool icon={<Cpu />} title="Version" summary="Firmware and board">
            <VersionTool id={id} state={state} />
          </Tool>
        </div>
      </Card>
    </>
  );
}

const range = (min: number, max: number) => (v: string) => {
  const n = Number(v);
  return Number.isInteger(n) && n >= min && n <= max ? null : `Enter a whole number from ${min} to ${max}.`;
};

function Tool({ icon, title, summary, children }: { icon: ReactNode; title: string; summary: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex min-h-14 w-full items-center gap-3 py-2 text-left"
      >
        <span className="text-muted [&>svg]:size-5" aria-hidden>
          {icon}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium">{title}</span>
          <span className="block truncate text-xs text-muted">{summary}</span>
        </span>
        <ChevronDown className={cx("size-5 text-muted transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && <div className="space-y-4 pb-4 pl-8">{children}</div>}
    </div>
  );
}

// ---- radio settings -------------------------------------------------------------------------

function RadioSettings({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const read = useCli(id);
  const write = useCli(id);
  const presets = useQuery({ queryKey: ["radio-presets"], queryFn: () => api<PresetList>("/api/radio/presets"), staleTime: 3600_000 });
  const list = presets.data?.presets ?? [];
  const cached = state.values["radio"];
  const [f, setF] = useState("");
  const [bw, setBw] = useState(62.5);
  const [sf, setSf] = useState(7);
  const [cr, setCr] = useState(5);
  const [presetId, setPresetId] = useState(CUSTOM);
  const [confirming, setConfirming] = useState(false);
  const [touched, setTouched] = useState(false); // keep the user's edits when fresh data arrives
  useEffect(() => {
    const parts = cached?.value.split(",").map(Number) ?? [];
    if (!touched && parts.length === 4 && parts.every(Number.isFinite)) {
      setF(String(parts[0]));
      setBw(parts[1]);
      setSf(parts[2]);
      setCr(parts[3]);
      setPresetId(matchPreset(list, parts[0], parts[1], parts[2], parts[3], null)?.id ?? CUSTOM);
    }
  }, [cached?.value, presets.data, touched]); // eslint-disable-line react-hooks/exhaustive-deps
  const preset = list.find((p) => p.id === presetId);
  const choose = (pid: string) => {
    setTouched(true);
    setPresetId(pid);
    const p = list.find((x) => x.id === pid);
    if (p) {
      setF(String(p.freq_mhz));
      setBw(p.bw_khz);
      setSf(p.sf);
      setCr(p.cr);
    }
  };
  const fN = Number(f);
  const valid = f !== "" && Number.isFinite(fN) && fN >= 150 && fN <= 2500;
  const value = `${fN},${bw},${sf},${cr}`;
  const dirty = valid && value !== cached?.value;
  const bwOptions = (BANDWIDTHS.includes(bw) ? BANDWIDTHS : [...BANDWIDTHS, bw].sort((a, b) => a - b)).map((b) => ({ value: b, label: `${b} kHz` }));

  return (
    <Card className="space-y-4 p-4 sm:p-5">
      <SectionHead title="Radio settings" at={cached?.at} now={now}>
        <RefreshIcon label="Read radio settings from the node" onClick={() => read.mutate("get radio", { onSuccess: () => setTouched(false) })} pending={read.isPending} disabled={busy} />
        <SaveIcon onClick={() => setConfirming(true)} pending={write.isPending} disabled={busy || !dirty} />
      </SectionHead>
      {!cached ? (
        <p className="text-sm text-muted">Tap refresh to read the node's frequency, bandwidth, spreading factor and coding rate.</p>
      ) : (
        <>
          <Field label="Preset" htmlFor="rr-preset">
            <select
              id="rr-preset"
              value={presetId}
              onChange={(e) => choose(e.target.value)}
              className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink sm:text-sm"
            >
              {list.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
              <option value={CUSTOM}>Custom</option>
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Frequency (MHz)" htmlFor="rr-f">
              <Input id="rr-f" inputMode="decimal" value={f} disabled={!!preset} onChange={(e) => { setTouched(true); setF(e.target.value); }} />
            </Field>
            <Field label="Bandwidth" htmlFor="rr-bw">
              <Select id="rr-bw" value={bw} options={bwOptions} onChange={(v) => { setTouched(true); setBw(v); }} disabled={!!preset} />
            </Field>
            <Field label="Spreading factor" htmlFor="rr-sf">
              <Select id="rr-sf" value={sf} options={[5, 6, 7, 8, 9, 10, 11, 12].map((v) => ({ value: v, label: `SF${v}` }))} onChange={(v) => { setTouched(true); setSf(v); }} disabled={!!preset} />
            </Field>
            <Field label="Coding rate" htmlFor="rr-cr">
              <Select id="rr-cr" value={cr} options={[5, 6, 7, 8].map((v) => ({ value: v, label: `4/${v}` }))} onChange={(v) => { setTouched(true); setCr(v); }} disabled={!!preset} />
            </Field>
          </div>
        </>
      )}
      <Reply reply={write.data} />
      <ErrorText error={read.error || write.error} />
      {cached && <CliSetting id={id} state={state} setting="tx" label="Transmit power (dBm)" inputMode="numeric" validate={range(1, 30)} />}
      {confirming && (
        <Dialog
          size="sm"
          title="Change the node's radio settings?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirming(false);
                  write.mutate(`set radio ${value}`, { onSuccess: (r) => isOk(r) && setTouched(false) });
                }}
              >
                Change
              </Button>
            </>
          }
        >
          <p className="text-sm">
            The node switches to {fN} MHz, {bw} kHz, SF{sf}, CR 4/{cr} after it restarts. If these don't match your radio and the rest of the
            mesh, you will lose contact with it and need physical access to fix it.
          </p>
        </Dialog>
      )}
    </Card>
  );
}

// ---- extra tools -------------------------------------------------------------------------------

function OwnerInfo({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const read = useCli(id);
  const write = useCli(id);
  const cached = state.values["owner.info"];
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft ?? cached?.value.replace(/\|/g, "\n") ?? "";
  const encoded = text.trim().replace(/\r?\n/g, "|");
  const dirty = encoded !== (cached?.value ?? "") && encoded !== "";
  return (
    <div className="space-y-1.5">
      <div className="flex items-start gap-1">
        <label htmlFor="rs-owner" className="sr-only">
          Owner info
        </label>
        <textarea
          id="rs-owner"
          rows={3}
          maxLength={119}
          value={text}
          placeholder={cached ? "" : "Tap refresh to read"}
          onChange={(e) => setDraft(e.target.value)}
          className="min-h-11 w-full flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-base text-ink placeholder:text-muted focus:border-accent focus:outline-none sm:text-sm"
        />
        <RefreshIcon label="Read owner info" onClick={() => read.mutate("get owner.info", { onSuccess: () => setDraft(null) })} pending={read.isPending} disabled={busy} />
        <SaveIcon onClick={() => write.mutate(`set owner.info ${encoded}`, { onSuccess: (r) => isOk(r) && setDraft(null) })} pending={write.isPending} disabled={busy || !dirty} />
      </div>
      <p className="text-xs text-muted">{ago(cached?.at, now)} · Anyone can request this, without a password.</p>
      <Reply reply={write.data} />
      <ErrorText error={read.error || write.error} />
    </div>
  );
}

function AdvertTool({ id }: { id: string }) {
  const busy = useBusy(id);
  const cli = useCli(id);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => cli.mutate("advert.zerohop")} disabled={busy}>
          <Megaphone className="size-4" aria-hidden /> Zero-hop advert
        </Button>
        <Button onClick={() => cli.mutate("advert")} disabled={busy}>
          <Radio className="size-4" aria-hidden /> Flood advert
        </Button>
      </div>
      <p className="text-xs text-muted">A zero-hop advert reaches only nodes in direct range. A flood advert is repeated across the whole mesh: use it sparingly.</p>
      <Reply reply={cli.data} />
      <ErrorText error={cli.error} />
    </div>
  );
}

function PositionTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const read = useCli(id);
  const write = useCli(id);
  const [picking, setPicking] = useState(false);
  const cLat = state.values["lat"];
  const cLon = state.values["lon"];
  const [draft, setDraft] = useState<{ lat: string; lon: string } | null>(null);
  const lat = draft?.lat ?? cLat?.value ?? "";
  const lon = draft?.lon ?? cLon?.value ?? "";
  const setLat = (v: string) => setDraft({ lat: v, lon });
  const setLon = (v: string) => setDraft({ lat, lon: v });
  const la = Number(lat);
  const lo = Number(lon);
  const valid = lat !== "" && lon !== "" && Math.abs(la) <= 90 && Math.abs(lo) <= 180;
  const dirty = valid && (lat !== cLat?.value || lon !== cLon?.value);
  const refresh = async () => {
    await read.mutateAsync("get lat").catch(() => undefined);
    await read.mutateAsync("get lon").catch(() => undefined);
    setDraft(null);
  };
  const save = async () => {
    await write.mutateAsync(`set lat ${la}`);
    await write.mutateAsync(`set lon ${lo}`);
    setDraft(null);
  };
  const initial =
    valid && (la || lo) ? { lat: la, lon: lo } : state.contact.lat !== null && state.contact.lon !== null ? { lat: state.contact.lat, lon: state.contact.lon } : null;
  return (
    <div className="space-y-2">
      <div className="flex items-end gap-1">
        <div className="grid flex-1 grid-cols-2 gap-2">
          <Field label="Latitude" htmlFor="rs-lat">
            <Input id="rs-lat" inputMode="decimal" value={lat} placeholder={cLat ? "" : "Tap refresh"} onChange={(e) => setLat(e.target.value)} />
          </Field>
          <Field label="Longitude" htmlFor="rs-lon">
            <Input id="rs-lon" inputMode="decimal" value={lon} placeholder={cLon ? "" : "Tap refresh"} onChange={(e) => setLon(e.target.value)} />
          </Field>
        </div>
        <RefreshIcon label="Read position" onClick={refresh} pending={read.isPending} disabled={busy} />
        <SaveIcon onClick={() => save().catch(() => undefined)} pending={write.isPending} disabled={busy || !dirty} />
      </div>
      <p className="text-xs text-muted">{ago(cLat?.at, now)} · Two short requests (latitude, then longitude).</p>
      <Button variant="ghost" onClick={() => setPicking(true)}>
        <MapPin className="size-4" aria-hidden /> Pick on map
      </Button>
      <Reply reply={write.data} />
      <ErrorText error={read.error || write.error} />
      {picking && (
        <Suspense fallback={null}>
          <LocationPicker
            initial={initial}
            onClose={() => setPicking(false)}
            onPick={(p) => {
              setDraft({ lat: p.lat.toFixed(6), lon: p.lon.toFixed(6) });
              setPicking(false);
            }}
          />
        </Suspense>
      )}
    </div>
  );
}

function ClockTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const read = useCli(id);
  const write = useCli(id);
  const cached = state.values["clock"];
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1">
        <p className="min-w-0 flex-1 text-sm">
          <span className="text-muted">Node clock: </span>
          <span className="font-mono">{cached?.value ?? "—"}</span>
        </p>
        <RefreshIcon label="Read the node's clock" onClick={() => read.mutate("clock")} pending={read.isPending} disabled={busy} />
      </div>
      <p className="text-xs text-muted">{ago(cached?.at, now)}</p>
      <Button onClick={() => write.mutate(`time ${Math.floor(Date.now() / 1000)}`)} disabled={busy}>
        <Clock className="size-4" aria-hidden /> Set to this device's time
      </Button>
      <p className="text-xs text-muted">The firmware refuses to move its clock backwards.</p>
      <Reply reply={write.data} />
      <ErrorText error={read.error || write.error} />
    </div>
  );
}

function AclTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const req = useRequest(id);
  const cli = useCli(id);
  const [key, setKey] = useState("");
  const [perm, setPerm] = useState(1);
  const acl = state.sections.acl;
  const keyOk = /^[0-9a-fA-F]{12,64}$/.test(key.trim());
  return (
    <div className="space-y-3">
      <SectionHead title="Access list" at={acl?.at} now={now}>
        <RefreshIcon label="Request the access list" onClick={() => req.mutate({ kind: "acl" })} pending={req.isPending} disabled={busy} />
      </SectionHead>
      {acl && acl.data.acl.length === 0 && <p className="text-sm text-muted">No entries.</p>}
      {acl && acl.data.acl.length > 0 && (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {acl.data.acl.map((a) => (
            <li key={a.key} className="flex flex-wrap items-center gap-2 px-3 py-2">
              <span className="min-w-40 flex-1">
                <span className="block truncate text-sm">{state.names[a.key.toLowerCase()] ?? "Unknown node"}</span>
                <span className="block font-mono text-xs text-muted">{a.key}</span>
              </span>
              <div className="w-36">
                <Select
                  id={`acl-${a.key}`}
                  value={a.perm & 3}
                  options={PERMS}
                  disabled={busy}
                  onChange={(v) => cli.mutate(`setperm ${a.key} ${v}`)}
                />
              </div>
              <IconButton label="Remove access" disabled={busy} onClick={() => cli.mutate(`setperm ${a.key}`)}>
                <Trash2 className="size-5" aria-hidden />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          cli.mutate(`setperm ${key.trim().toLowerCase()} ${perm}`, { onSuccess: () => setKey("") });
        }}
      >
        <Field label="Add or change access" htmlFor="acl-key" hint="The node's public key (at least the first 12 hex characters). Refresh the list afterwards to check.">
          <div className="flex flex-wrap gap-2">
            <Input id="acl-key" value={key} onChange={(e) => setKey(e.target.value)} className="min-w-0 basis-full font-mono sm:basis-0 sm:flex-1" spellCheck={false} autoComplete="off" />
            <div className="min-w-36 flex-1 sm:flex-none">
              <Select id="acl-perm" value={perm} options={PERMS} onChange={setPerm} />
            </div>
            <Button type="submit" disabled={busy || !keyOk}>
              Apply
            </Button>
          </div>
        </Field>
      </form>
      <Reply reply={cli.data} />
      <ErrorText error={req.error || cli.error} />
    </div>
  );
}

function AdminPassword({ id }: { id: string }) {
  const busy = useBusy(id);
  const cli = useCli(id);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const mismatch = b !== "" && a !== b;
  const invalid = /\s/.test(a);
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        cli.mutate(`password ${a}`, {
          onSuccess: () => {
            setA("");
            setB("");
          },
        });
      }}
    >
      <Field label="New admin password" htmlFor="rs-pw1" error={invalid ? "Spaces are not allowed." : null} hint="Up to 15 characters. Keep it safe: without it, the node can only be reset in person.">
        <Input id="rs-pw1" type="password" autoComplete="new-password" maxLength={15} value={a} onChange={(e) => setA(e.target.value)} />
      </Field>
      <Field label="Repeat it" htmlFor="rs-pw2" error={mismatch ? "The passwords don't match." : null}>
        <Input id="rs-pw2" type="password" autoComplete="new-password" maxLength={15} value={b} onChange={(e) => setB(e.target.value)} />
      </Field>
      <Button type="submit" variant="primary" disabled={busy || !a || a !== b || invalid}>
        {cli.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <KeyRound className="size-4" aria-hidden />} Change password
      </Button>
      {cli.data !== undefined && !cli.isPending &&
        (cli.data?.toLowerCase().startsWith("password now") ? (
          <p className="flex items-center gap-1 text-xs text-ok" role="status">
            <Check className="size-3.5" aria-hidden /> The node accepted the new password.
          </p>
        ) : (
          <Reply reply={cli.data} />
        ))}
      <ErrorText error={cli.error} />
    </form>
  );
}

function IdentityTool({ id }: { id: string }) {
  const qc = useQueryClient();
  const busy = useBusy(id);
  const [prefix, setPrefix] = useState("");
  const [confirming, setConfirming] = useState(false);
  const change = useMutation({
    mutationKey: ["remote-op", id],
    mutationFn: () => api<{ reply: string | null; new_public_key: string }>(`/api/remote/${id}/identity`, { method: "POST", json: { prefix } }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["remote", id] }),
  });
  const p = prefix.trim().toLowerCase();
  const error = !/^[0-9a-f]{0,4}$/.test(p) ? "Use up to 4 hex characters (0–9, a–f)." : ["00", "ff"].includes(p.slice(0, 2)) ? "Keys starting with 00 or ff are reserved." : null;
  return (
    <div className="space-y-3">
      <p className="text-sm">
        Generates a new identity for the node whose public key starts with the prefix you choose, so it is easy to tell apart from other
        repeaters in paths. The new private key is created here, sent to the node encrypted, and not kept.
      </p>
      <Field label="Prefix (optional)" htmlFor="rs-prefix" error={error} hint="1–2 characters are instant; 4 can take up to a minute. Leave empty for a random key.">
        <Input id="rs-prefix" value={prefix} maxLength={4} className="font-mono" spellCheck={false} autoComplete="off" onChange={(e) => setPrefix(e.target.value)} />
      </Field>
      <Button variant="danger" disabled={busy || !!error} onClick={() => setConfirming(true)}>
        {change.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Fingerprint className="size-4" aria-hidden />}
        {change.isPending ? "Generating and sending…" : "Change identity key"}
      </Button>
      {change.data && (
        <div className="rounded-lg border border-ok/40 bg-ok/5 px-3 py-2 text-sm" role="status">
          <p>
            New public key: <span className="break-all font-mono text-xs">{change.data.new_public_key}</span>
          </p>
          <p className="mt-1 text-xs text-muted">
            Reboot the node to switch to it. Afterwards it appears as a new contact when its next advert arrives; remove the old one. Its
            neighbours and companions will need to re-learn it too.
          </p>
          <Reply reply={change.data.reply} />
        </div>
      )}
      <ErrorText error={change.error} />
      {confirming && (
        <Dialog
          size="sm"
          title="Give this node a new identity?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirming(false);
                  change.mutate();
                }}
              >
                Change key
              </Button>
            </>
          }
        >
          <p className="text-sm">
            After its next restart the node has a different public key. Contacts, saved paths and access lists elsewhere that refer to the
            old key stop matching it. This cannot be undone: the old key is not saved anywhere.
          </p>
        </Dialog>
      )}
    </div>
  );
}

function RegionsTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const dump = useCli(id);
  const act = useCli(id);
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [lastDump, setLastDump] = useState<{ text: string; at: string } | null>(null);
  const n = name.trim();
  const validName = /^[#*]?[\w.-]{1,30}$/.test(n);
  const run = (cmd: string) => act.mutate(cmd);
  return (
    <div className="space-y-3">
      <SectionHead title="Regions" at={lastDump?.at} now={now}>
        <RefreshIcon
          label="Read the region table"
          onClick={() => dump.mutate("region", { onSuccess: (r) => r !== null && setLastDump({ text: r, at: new Date().toISOString() }) })}
          pending={dump.isPending}
          disabled={busy}
        />
      </SectionHead>
      {lastDump ? (
        <pre className="max-h-48 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-xs">{lastDump.text}</pre>
      ) : state.sections.regions ? (
        <p className="text-sm">
          <span className="text-muted">Regions: </span>
          {state.sections.regions.data.text || "none"}
        </p>
      ) : (
        <p className="text-sm text-muted">Tap refresh to read the node's region table.</p>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Region" htmlFor="rg-name">
          <Input id="rg-name" value={name} maxLength={30} spellCheck={false} autoComplete="off" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Parent (optional)" htmlFor="rg-parent">
          <Input id="rg-parent" value={parent} maxLength={30} spellCheck={false} autoComplete="off" onChange={(e) => setParent(e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy || !validName} onClick={() => run(`region put ${n}${parent.trim() ? ` ${parent.trim()}` : ""}`)}>
          Add
        </Button>
        <Button disabled={busy || !validName} onClick={() => run(`region allowf ${n}`)}>
          Allow floods
        </Button>
        <Button disabled={busy || !validName} onClick={() => run(`region denyf ${n}`)}>
          Deny floods
        </Button>
        <Button disabled={busy || !validName} onClick={() => run(`region home ${n}`)}>
          Set as home
        </Button>
        <Button variant="danger" disabled={busy || !validName} onClick={() => run(`region remove ${n}`)}>
          Remove
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={busy} onClick={() => run("region save")}>
          <Save className="size-4" aria-hidden /> Save regions
        </Button>
        <span className="text-xs text-muted">Changes apply at once but are lost on restart until saved.</span>
      </div>
      <Reply reply={act.data} />
      <ErrorText error={dump.error || act.error} />
    </div>
  );
}

function NeighboursTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const req = useRequest(id);
  const nb = state.sections.neighbours;
  const list = nb?.data.neighbours ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  return (
    <div className="space-y-3">
      <SectionHead title="Neighbours" at={nb?.at} now={now}>
        <RefreshIcon label="Request neighbours" onClick={() => req.mutate({ kind: "neighbours" })} pending={req.isPending} disabled={busy} />
      </SectionHead>
      {nb && list.length === 0 && <p className="text-sm text-muted">The node has not heard any other repeaters directly.</p>}
      {list.length > 0 && (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {list.map((x) => (
            <li key={x.pubkey}>
              <button
                type="button"
                className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none"
                onClick={() => setPicked(x.pubkey)}
                aria-label={`Details for ${state.names[x.pubkey.toLowerCase()] ?? "unknown node"} (${x.pubkey})`}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">{state.names[x.pubkey.toLowerCase()] ?? "Unknown node"}</span>
                  <span className="block font-mono text-xs text-muted">{x.pubkey}</span>
                </span>
                <span className="text-right text-xs tabular-nums">
                  <span className={cx("block font-medium", x.snr >= 0 ? "text-ok" : x.snr > -7 ? "text-ink" : "text-warn")}>SNR {x.snr} dB</span>
                  <span className="block text-muted">heard {duration(x.secs_ago).split(" ").slice(0, 2).join(" ")} ago</span>
                </span>
                <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
      {nb && nb.data.neighbours_count > nb.data.results_count && (
        <p className="text-xs text-muted">
          Showing {nb.data.results_count} of {nb.data.neighbours_count}.
        </p>
      )}
      <ErrorText error={req.error} />
      {picked && (
        <Dialog
          title="Neighbour"
          onClose={() => setPicked(null)}
          footer={
            <Button variant="primary" onClick={() => setPicked(null)}>
              Done
            </Button>
          }
        >
          <NodeDetails prefix={picked} onNavigate={() => setPicked(null)} />
        </Dialog>
      )}
    </div>
  );
}

function lppValue(v: unknown): string {
  if (typeof v === "number") return String(Math.round(v * 1000) / 1000);
  if (v && typeof v === "object") {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k} ${typeof x === "number" ? Math.round(x * 1e5) / 1e5 : String(x)}`)
      .join(", ");
  }
  return String(v);
}

function TelemetryTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const req = useRequest(id);
  const t = state.sections.telemetry;
  return (
    <div className="space-y-3">
      <SectionHead title="Telemetry" at={t?.at} now={now}>
        <RefreshIcon label="Request telemetry" onClick={() => req.mutate({ kind: "telemetry" })} pending={req.isPending} disabled={busy} />
      </SectionHead>
      {t && t.data.lpp.length === 0 && <p className="text-sm text-muted">The node sent no readings.</p>}
      {t && t.data.lpp.length > 0 && (
        <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {t.data.lpp.map((r, i) => (
            <div key={i} className="rounded-lg bg-surface-2 px-3 py-2">
              <dt className="text-xs capitalize text-muted">
                {r.type.replace(/_/g, " ")} · channel {r.channel}
              </dt>
              <dd className="text-sm font-medium tabular-nums">{lppValue(r.value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <ErrorText error={req.error} />
    </div>
  );
}

function RebootTool({ id }: { id: string }) {
  const busy = useBusy(id);
  const cli = useCli(id);
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="space-y-2">
      <Button variant="danger" onClick={() => setConfirming(true)} disabled={busy}>
        <Power className="size-4" aria-hidden /> Reboot node
      </Button>
      <p className="text-xs text-muted">The node stops repeating for a few seconds and forgets logins: you will need to log in again.</p>
      <ErrorText error={cli.error} />
      {confirming && (
        <Dialog
          size="sm"
          title="Reboot this node?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirming(false);
                  cli.mutate("reboot");
                }}
              >
                Reboot
              </Button>
            </>
          }
        >
          <p className="text-sm">Messages relayed through it are dropped while it restarts.</p>
        </Dialog>
      )}
    </div>
  );
}

function VersionTool({ id, state }: { id: string; state: RemoteState }) {
  const now = useNow();
  const busy = useBusy(id);
  const cli = useCli(id);
  const ver = state.values["ver"];
  const board = state.values["board"];
  const refresh = async () => {
    await cli.mutateAsync("ver").catch(() => undefined);
    await cli.mutateAsync("board").catch(() => undefined);
  };
  return (
    <div className="space-y-2">
      <div className="flex items-start gap-1">
        <dl className="min-w-0 flex-1 space-y-1 text-sm">
          <div>
            <dt className="inline text-muted">Firmware: </dt>
            <dd className="inline font-mono">{ver?.value ?? "—"}</dd>
          </div>
          <div>
            <dt className="inline text-muted">Board: </dt>
            <dd className="inline font-mono">{board?.value ?? "—"}</dd>
          </div>
        </dl>
        <RefreshIcon label="Read version" onClick={refresh} pending={cli.isPending} disabled={busy} />
      </div>
      <p className="text-xs text-muted">{ago(ver?.at, now)}</p>
      <ErrorText error={cli.error} />
    </div>
  );
}
