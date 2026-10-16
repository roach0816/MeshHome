import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpCircle,
  Check,
  Copy,
  Globe,
  House,
  KeyRound,
  Package,
  Play,
  Radio,
  RefreshCw,
  Settings2,
  Store,
  Trash2,
} from "lucide-react";
import {
  api,
  type BotAppCatalogEntry,
  type BotAppInfo,
  type BotAppInstalled,
  type BotAppSetting,
  type BotAppsState,
} from "../lib/api";
import { canCopy, cx, relativeSeconds } from "../lib/util";
import { Dialog } from "./Dialog";
import { InfoTip } from "./InfoTip";
import { Section } from "./SettingsKit";
import { Badge, Button, ErrorText, Field, IconButton, Input } from "./ui";

const KEY = ["bot-apps"];

function useBotApps() {
  return useQuery({
    queryKey: KEY,
    queryFn: () => api<BotAppsState>("/api/bot/apps"),
    // Apps a device sends data to show when the last reading arrived: keep that current.
    refetchInterval: (q) => (q.state.data?.installed.some((a) => a.push) ? 10_000 : false),
  });
}

/** Commands an app adds, as "/weather — local weather" (or "/ha <group>" for one that reads a group). */
function Commands({ app }: { app: BotAppInfo }) {
  return (
    <p className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
      {app.commands.map((c) => (
        <span key={c.name}>
          <code className="font-mono font-medium">
            /{c.name}
            {c.group ? " <group>" : ""}
          </code>{" "}
          <span className="text-muted">{c.help}</span>
        </span>
      ))}
    </p>
  );
}

function Contacts({ app }: { app: BotAppInfo }) {
  return (
    <ul className="space-y-0.5 text-xs text-muted">
      {app.contacts.map((c) => (
        <li key={c} className="flex items-start gap-1.5">
          {c.startsWith("signs in") ? (
            <KeyRound className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          ) : c.includes("your local network") ? (
            <House className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          ) : (
            <Globe className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          )}
          {c.charAt(0).toUpperCase() + c.slice(1)}
        </li>
      ))}
    </ul>
  );
}

/** Where a device sends its readings. Devices can't do HTTPS: over HTTPS, nginx passes /push/ on
 *  plain HTTP port 80 through to MeshHome; over plain HTTP it's the address you're on. */
function uploadAddress(path: string) {
  const host = location.hostname;
  const port = location.protocol === "https:" ? "80" : location.port || "80";
  return { host, port, path, url: `http://${host}${port === "80" ? "" : `:${port}`}${path}` };
}

function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="grid grid-cols-[4.5rem_1fr_auto] items-center gap-2">
      <span className="text-xs text-muted">{label}</span>
      <code className="min-w-0 select-all break-all rounded-md bg-surface-2 px-2 py-1 font-mono text-xs">{value}</code>
      {canCopy() ? (
        <IconButton
          label={copied ? "Copied" : `Copy ${label.toLowerCase()}`}
          className="size-9"
          onClick={() =>
            navigator.clipboard
              .writeText(value)
              .then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              })
              .catch(() => {})
          }
        >
          {copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
        </IconButton>
      ) : (
        <span />
      )}
    </div>
  );
}

/** For an app a device sends data to: the upload address, the device's instructions, and when the
 *  last reading arrived (the quickest way to tell whether the device is set up). */
function PushPanel({ app }: { app: BotAppInstalled }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const rotate = useMutation({
    mutationFn: () => api<BotAppsState>(`/api/bot/apps/${app.id}/push-token`, { method: "POST" }),
    onSuccess: (d) => {
      qc.setQueryData(KEY, d);
      setConfirm(false);
    },
  });
  const push = app.push!;
  const a = uploadAddress(push.path);
  const at = push.received_at ? new Date(push.received_at).getTime() / 1000 : null;
  const fresh = at !== null && Date.now() / 1000 - at <= push.max_age;
  return (
    <div className="space-y-2 rounded-lg bg-surface-2/60 p-3">
      <p className="flex items-center gap-1 text-sm font-medium">
        <Radio className="size-4 text-muted" aria-hidden /> Upload address
        <InfoTip label="About the upload address">
          <p>Your device sends its readings here, over plain HTTP. Only devices on your local network are accepted.</p>
          <p>New address makes a new one; the old one stops working, so update the device too.</p>
        </InfoTip>
      </p>
      {push.help && <p className="text-xs text-muted">{push.help}</p>}
      <div className="space-y-1.5">
        <CopyValue label="Server" value={a.host} />
        <CopyValue label="Port" value={a.port} />
        <CopyValue label="Path" value={a.path} />
        <CopyValue label="Full URL" value={a.url} />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className={cx("text-sm", fresh ? "text-ok" : "text-muted")} role="status">
          {at === null ? "Nothing received yet" : `Last reading ${relativeSeconds(at)}`}
        </p>
        <Button variant="ghost" onClick={() => setConfirm(true)}>
          <RefreshCw className="size-4" aria-hidden /> New address
        </Button>
      </div>
      {confirm && (
        <Dialog
          size="sm"
          title="Make a new upload address?"
          onClose={() => setConfirm(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(false)} autoFocus>
                Cancel
              </Button>
              <Button variant="primary" disabled={rotate.isPending} onClick={() => rotate.mutate()}>
                New address
              </Button>
            </>
          }
        >
          <p className="text-sm">The current address stops working at once. Enter the new path on your device afterwards.</p>
          <ErrorText error={rotate.error} />
        </Dialog>
      )}
    </div>
  );
}

/** Group names in a groups setting ("garage: cover.left, cover.right" per line). */
function groupNames(text: string | undefined) {
  const names = (text ?? "")
    .split("\n")
    .filter((line) => line.includes(":"))
    .map((line) => line.split(":")[0].trim().toLowerCase())
    .filter((n) => /^[a-z0-9]{1,20}$/.test(n));
  return [...new Set(names)];
}

const fieldClass =
  "w-full rounded-lg border border-line bg-surface px-3 text-base text-ink placeholder:text-muted sm:text-sm focus:border-accent focus:outline-none disabled:bg-surface-2 disabled:text-muted";

function SettingInput({
  id,
  setting: s,
  settings,
  values,
  secretSaved,
  onChange,
}: {
  id: string;
  setting: BotAppSetting;
  settings: BotAppSetting[];
  values: Record<string, string>;
  secretSaved: boolean;
  onChange: (value: string) => void;
}) {
  const value = values[s.key] ?? "";
  if (s.type === "select") {
    // A dependent select lists the choices for its parent's value, and waits for the parent.
    const parent = s.depends_on ? settings.find((p) => p.key === s.depends_on) : undefined;
    const parentValue = s.depends_on ? (values[s.depends_on] ?? "") : null;
    const options = s.options ?? (parentValue ? (s.options_by?.[parentValue] ?? []) : []);
    const waiting = parentValue === "";
    return (
      <select
        id={id}
        value={options.some((o) => o.value === value) ? value : ""}
        disabled={waiting}
        onChange={(e) => onChange(e.target.value)}
        className={cx(fieldClass, "min-h-11")}
      >
        <option value="">{waiting ? `Choose ${parent?.label.toLowerCase() ?? "the setting above"} first` : "Choose…"}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if (s.type === "groups") {
    return (
      <textarea
        id={id}
        rows={Math.min(10, Math.max(3, value.split("\n").length + 1))}
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        placeholder={s.placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cx(fieldClass, "py-2 font-mono")}
      />
    );
  }
  return (
    <Input
      id={id}
      type={s.type === "secret" ? "password" : "text"}
      autoComplete="off"
      autoCapitalize="none"
      spellCheck={false}
      placeholder={s.type === "secret" && secretSaved ? "Saved (leave empty to keep)" : s.placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** The app's settings, plus a try-it button that runs its first command with them. */
function SettingsForm({
  app,
  initial,
  secretsSet = [],
  submitLabel,
  onSubmit,
  pending,
  error,
}: {
  app: BotAppInfo;
  initial: Record<string, string>;
  secretsSet?: string[];
  submitLabel: string;
  onSubmit: (values: Record<string, string>) => void;
  pending: boolean;
  error: unknown;
}) {
  // Selects with a default start on it (it's what the app uses while nothing is saved).
  const [values, setValues] = useState<Record<string, string>>(() => {
    const out = { ...initial };
    for (const s of app.settings) if (s.type === "select" && s.default && !out[s.key]) out[s.key] = s.default;
    return out;
  });
  const [command, setCommand] = useState(app.commands[0]?.name ?? "");
  const [group, setGroup] = useState("");
  const groupKey = app.commands.find((c) => c.name === command)?.group;
  const groups = groupKey ? groupNames(values[groupKey]) : [];
  const arg = groupKey && groups.includes(group) ? group : "";
  const test = useMutation({
    mutationFn: () =>
      api<{ command: string; reply: string }>(`/api/bot/apps/${app.id}/test`, {
        json: { command, settings: values, arg: arg || null },
      }),
  });
  const change = (key: string, value: string) => {
    const next = { ...values, [key]: value };
    // A select that depends on this one starts over.
    for (const s of app.settings) if (s.depends_on === key) next[s.key] = "";
    setValues(next);
    test.reset();
  };
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(values);
      }}
    >
      {app.settings.map((s) => (
        <Field key={s.key} label={s.label} htmlFor={`app-${app.id}-${s.key}`} info={s.help || undefined}>
          <SettingInput
            id={`app-${app.id}-${s.key}`}
            setting={s}
            settings={app.settings}
            values={values}
            secretSaved={secretsSet.includes(s.key)}
            onChange={(v) => change(s.key, v)}
          />
        </Field>
      ))}
      <div className="flex flex-wrap items-center gap-2">
        {app.commands.length > 1 && (
          <select
            aria-label="Command to test"
            value={command}
            onChange={(e) => {
              setCommand(e.target.value);
              test.reset();
            }}
            className="min-h-11 rounded-lg border border-line bg-surface px-3 text-sm"
          >
            {app.commands.map((c) => (
              <option key={c.name} value={c.name}>
                /{c.name}
              </option>
            ))}
          </select>
        )}
        {groupKey && (
          <select
            aria-label="Group to test"
            value={arg}
            onChange={(e) => {
              setGroup(e.target.value);
              test.reset();
            }}
            className="min-h-11 rounded-lg border border-line bg-surface px-3 text-sm"
          >
            <option value="">No group (lists them)</option>
            {groups.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        )}
        <Button type="button" onClick={() => test.mutate()} disabled={test.isPending}>
          <Play className="size-4" aria-hidden /> {test.isPending ? "Trying…" : `Try /${command}${arg ? ` ${arg}` : ""}`}
        </Button>
        <Button type="submit" variant="primary" disabled={pending}>
          {submitLabel}
        </Button>
      </div>
      {test.data && (
        <div className="rounded-lg border border-ok/40 bg-ok/5 px-3 py-2 text-sm" role="status">
          <p className="text-xs text-muted">/{test.data.command} would reply:</p>
          <p className="mt-0.5 font-medium">{test.data.reply}</p>
        </div>
      )}
      <ErrorText error={test.error ?? error} />
    </form>
  );
}

function InstalledApp({ app }: { app: BotAppInstalled }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<null | "remove" | "update">(null);
  const set = (data: BotAppsState) => qc.setQueryData(KEY, data);
  const configure = useMutation({
    mutationFn: (body: { settings?: Record<string, string>; enabled?: boolean }) =>
      api<BotAppsState>(`/api/bot/apps/${app.id}`, { method: "PATCH", json: body }),
    onSuccess: (d) => {
      set(d);
      setEditing(false);
    },
  });
  const remove = useMutation({
    mutationFn: () => api<BotAppsState>(`/api/bot/apps/${app.id}`, { method: "DELETE" }),
    onSuccess: set,
  });
  const update = useMutation({
    mutationFn: () => api<BotAppsState>(`/api/bot/apps/${app.id}/update`, { json: {} }),
    onSuccess: (d) => {
      set(d);
      setConfirm(null);
    },
  });
  return (
    <li className="space-y-2 p-3">
      <div className="flex items-start gap-3">
        <Package className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{app.name}</span>
            <span className="text-xs text-muted">v{app.version}</span>
            {!app.enabled && <Badge>Off</Badge>}
            {app.problem && <Badge tone="warn">Needs settings</Badge>}
            {app.update && <Badge tone="accent">Update: v{app.update.version}</Badge>}
          </p>
          <Commands app={app} />
          <Contacts app={app} />
        </div>
        <label className="flex shrink-0 cursor-pointer items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={app.enabled}
            disabled={configure.isPending}
            onChange={(e) => configure.mutate({ enabled: e.target.checked })}
            className="size-5 accent-[var(--accent)]"
            aria-label={`${app.name} on`}
          />
          <span className="hidden sm:inline">On</span>
        </label>
      </div>
      <div className="flex flex-wrap gap-2 pl-8">
        <Button onClick={() => setEditing((v) => !v)} aria-expanded={editing}>
          <Settings2 className="size-4" aria-hidden /> Settings
        </Button>
        {app.update && (
          <Button onClick={() => setConfirm("update")}>
            <ArrowUpCircle className="size-4" aria-hidden /> Update
          </Button>
        )}
        <Button variant="ghost" onClick={() => setConfirm("remove")}>
          <Trash2 className="size-4" aria-hidden /> Remove
        </Button>
      </div>
      {app.problem && !editing && (
        <p className="pl-8 text-sm text-warn" role="status">
          {app.problem}. Open Settings to choose again.
        </p>
      )}
      {app.push && (
        <div className="pl-8">
          <PushPanel app={app} />
        </div>
      )}
      {editing && (
        <div className="pl-8">
          <SettingsForm
            app={app}
            initial={app.values}
            secretsSet={app.secrets_set}
            submitLabel="Save"
            pending={configure.isPending}
            error={configure.error}
            onSubmit={(settings) => configure.mutate({ settings })}
          />
        </div>
      )}
      <ErrorText error={editing ? null : (configure.error ?? remove.error)} />
      {confirm === "remove" && (
        <Dialog
          size="sm"
          title={`Remove ${app.name}?`}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
                Remove
              </Button>
            </>
          }
        >
          <p className="text-sm">
            Its commands ({app.commands.map((c) => `/${c.name}`).join(", ")}) stop working, and its settings are deleted.
          </p>
        </Dialog>
      )}
      {confirm === "update" && app.update && (
        <Dialog
          size="sm"
          title={`Update ${app.name} to v${app.update.version}?`}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="primary" disabled={update.isPending} onClick={() => update.mutate()}>
                Update
              </Button>
            </>
          }
        >
          {app.update.changes.length ? (
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {app.update.changes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm">Improvements only: no new commands, settings or hosts. Your settings are kept.</p>
          )}
          <ErrorText error={update.error} />
        </Dialog>
      )}
    </li>
  );
}

function CatalogDialog({ state, onClose }: { state: BotAppsState; onClose: () => void }) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<BotAppCatalogEntry | null>(null);
  const [category, setCategory] = useState<string | null>(null); // null: all
  const shown = category ? state.catalog.filter((a) => a.category === category) : state.catalog;
  const refresh = useMutation({
    mutationFn: () => api<BotAppsState>("/api/bot/apps/refresh", { method: "POST" }),
    onSuccess: (d) => qc.setQueryData(KEY, d),
  });
  const install = useMutation({
    mutationFn: ({ id, settings }: { id: string; settings: Record<string, string> }) =>
      api<BotAppsState>(`/api/bot/apps/${id}`, { json: { settings } }),
    onSuccess: (d) => {
      qc.setQueryData(KEY, d);
      onClose();
    },
  });
  if (picked) {
    return (
      <Dialog title={`Install ${picked.name}`} onClose={onClose}>
        <div className="space-y-3">
          <button className="text-sm text-accent underline" onClick={() => setPicked(null)}>
            Back to the catalog
          </button>
          <p className="text-sm">{picked.description || picked.summary}</p>
          <Commands app={picked} />
          <Contacts app={picked} />
          <SettingsForm
            app={picked}
            initial={{}}
            submitLabel="Install"
            pending={install.isPending}
            error={install.error}
            onSubmit={(settings) => install.mutate({ id: picked.id, settings })}
          />
        </div>
      </Dialog>
    );
  }
  return (
    <Dialog
      title="App catalog"
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted">
            {state.catalog_source === "online" ? "From the MeshHome app catalog." : "Apps included with MeshHome."}
          </p>
          <Button variant="ghost" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshCw className={cx("size-4", refresh.isPending && "animate-spin")} aria-hidden /> Refresh
          </Button>
        </div>
        {state.catalog_error && <p className="rounded-lg bg-warn/10 px-3 py-2 text-sm">{state.catalog_error}</p>}
        {state.categories.length > 1 && (
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Categories">
            {[null, ...state.categories].map((c) => {
              const count = c ? state.catalog.filter((a) => a.category === c).length : state.catalog.length;
              return (
                <button
                  key={c ?? "all"}
                  type="button"
                  aria-pressed={category === c}
                  onClick={() => setCategory(c)}
                  className={cx(
                    "min-h-9 rounded-full border px-3 text-sm",
                    category === c ? "border-accent bg-accent-soft/60 font-medium" : "border-line hover:bg-surface-2",
                  )}
                >
                  {c ?? "All"} <span className="text-muted">{count}</span>
                </button>
              );
            })}
          </div>
        )}
        <ul className="divide-y divide-line rounded-lg border border-line">
          {shown.map((a) => (
            <li key={a.id} className="flex items-start gap-3 p-3">
              <Package className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden />
              <div className="min-w-0 flex-1 space-y-1">
                <p className="flex flex-wrap items-center gap-x-2">
                  <span className="font-medium">{a.name}</span>
                  <span className="text-xs text-muted">v{a.version}</span>
                  <span className="text-xs text-muted">· {a.category}</span>
                </p>
                <p className="text-sm text-muted">{a.summary}</p>
                <Commands app={a} />
                <Contacts app={a} />
              </div>
              {a.installed ? (
                <Badge tone="ok">Installed</Badge>
              ) : a.compatible ? (
                <Button onClick={() => setPicked(a)}>Install…</Button>
              ) : (
                <Badge>Needs MeshHome {a.min_meshhome}</Badge>
              )}
            </li>
          ))}
          {shown.length === 0 && <li className="p-3 text-sm text-muted">No apps available.</li>}
        </ul>
        <ErrorText error={refresh.error} />
      </div>
    </Dialog>
  );
}

/** Settings → Bot → Apps: commands added from the app catalog. */
export function BotAppsSection() {
  const apps = useBotApps();
  const [browsing, setBrowsing] = useState(false);
  const d = apps.data;
  return (
    <Section
      title="Apps"
      description="Add commands to the bot from the app catalog."
      info={
        <>
          <p>
            An app is a recipe: the settings it needs, the address it reads from, and how to word the reply. It can't
            run code, and only contacts what's listed under it.
          </p>
          <p>When a new version is published, you see what changes and decide when to update.</p>
        </>
      }
      actions={
        <Button onClick={() => setBrowsing(true)} disabled={!d}>
          <Store className="size-4" aria-hidden /> Browse apps
        </Button>
      }
    >
      <ErrorText error={apps.error} />
      {d && d.installed.length === 0 && (
        <p className="text-sm text-muted">
          No apps yet. <InfoTip label="About apps">For example, the Ecowitt app answers /weather from your weather station.</InfoTip>
        </p>
      )}
      {d && d.installed.length > 0 && (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {d.installed.map((a) => (
            <InstalledApp key={a.id} app={a} />
          ))}
        </ul>
      )}
      {browsing && d && <CatalogDialog state={d} onClose={() => setBrowsing(false)} />}
    </Section>
  );
}
