import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArchiveRestore, CheckCircle2, Download, FileUp, Loader2, Lock, Radio, Trash2, XCircle } from "lucide-react";
import { api, ApiError, errorDetail, REQUESTED_WITH, writeHeaders } from "../lib/api";
import { cx, formatDateTime } from "../lib/util";
import { Dialog } from "./Dialog";
import { Badge, Button, Card, ErrorText, Field, IconButton, Input } from "./ui";
import { InfoTip } from "./InfoTip";

const MIN_PASSPHRASE = 10;

type BackupFile = { name: string; size: number; created_at: string };
type BackupList = { persistent: boolean; native: boolean; backups: BackupFile[] };
/** A radio snapshot in a backup (the identity key itself is never sent to the browser). */
type RadioSnapshot = {
  name: string;
  public_key: string;
  captured_at: string;
  firmware: string | null;
  model: string | null;
  /** included; disabled: the firmware doesn't allow exporting it; unavailable: it couldn't be read. */
  key: "included" | "disabled" | "unavailable";
  channels: string[];
  contacts: number | null;
  radio: { freq_mhz: number | null; bw_khz: number | null; sf: number | null; cr: number | null; tx_power_dbm: number | null };
};
type Created = {
  name: string;
  size: number;
  system_included: boolean;
  warning: string | null;
  radio: RadioSnapshot | null;
  radio_note: string | null;
};
export type RestoreSummary = {
  created_at: string;
  app_version: string;
  install_kind: string;
  home_name: string;
  counts: Record<string, number>;
  can_restore: boolean;
  errors: string[];
  restored: string[];
  not_restored: string[];
  notes: string[];
  system_restore: boolean;
  radio: RadioSnapshot | null;
};
type Applied = { restored: boolean; system: "none" | "applying"; safety_backup: string | null; summary: RestoreSummary };

const size = (n: number) => (n < 1_048_576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1_048_576).toFixed(1)} MB`);

/** Settings → Backup & restore. */
export function BackupSection() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["backups"], queryFn: () => api<BackupList>("/api/backups") });
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [restoring, setRestoring] = useState<null | "backup" | "radio">(null);
  const create = useMutation({
    mutationFn: () => api<Created>("/api/backups", { json: { passphrase: pass } }),
    onSuccess: (r) => {
      setPass("");
      setPass2("");
      qc.invalidateQueries({ queryKey: ["backups"] });
      // Containers don't keep backups: hand it over straight away.
      if (!list.data?.persistent) window.location.assign(`/api/backups/${encodeURIComponent(r.name)}`);
    },
  });
  const remove = useMutation({
    mutationFn: (name: string) => api(`/api/backups/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups"] }),
  });
  const d = list.data;
  const short = pass.length > 0 && pass.length < MIN_PASSPHRASE;
  const mismatch = pass2.length > 0 && pass2 !== pass;

  return (
    <Card className="space-y-5 p-4 sm:p-5">
      <div>
        <h3 className="flex items-center gap-1 text-base font-semibold">
          Backup &amp; restore
          <InfoTip label="What a backup holds">
            Your account, API keys, all settings and the whole message archive
            {d?.native ? ", plus this Pi's network and HTTPS settings, certificate, DNS credentials and the radio HAT's identity" : ""}
            . A connected TCP radio is saved too: its identity key, settings, channels and contacts. Sign-ins aren't included.
          </InfoTip>
        </h3>
        <p className="mt-0.5 text-sm text-muted">Everything, in one encrypted file.</p>
      </div>

      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Passphrase" htmlFor="bk-pass" error={short ? `Use at least ${MIN_PASSPHRASE} characters` : null}>
            <Input id="bk-pass" type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} />
          </Field>
          <Field label="Repeat it" htmlFor="bk-pass2" error={mismatch ? "The passphrases don't match" : null}>
            <Input id="bk-pass2" type="password" autoComplete="new-password" value={pass2} onChange={(e) => setPass2(e.target.value)} />
          </Field>
        </div>
        <p className="flex items-start gap-2 text-xs text-muted">
          <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          Keep the passphrase safe: MeshHome doesn't store it, and the backup can't be opened without it.
        </p>
        <ErrorText error={create.error} />
        {create.data?.warning && <p className="rounded-lg bg-warn/10 px-3 py-2 text-sm">{create.data.warning} The backup holds the app data only.</p>}
        {create.data && <RadioCaptured c={create.data} />}
        <Button type="submit" variant="primary" disabled={create.isPending || pass.length < MIN_PASSPHRASE || pass !== pass2}>
          {create.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Download className="size-4" aria-hidden />}
          {create.isPending ? "Creating the backup…" : d?.persistent ? "Create backup" : "Create and download backup"}
        </Button>
      </form>

      {d?.persistent && (
        <div className="space-y-2">
          <h4 className="text-sm font-semibold">Backups on this Pi</h4>
          {d.backups.length === 0 ? (
            <p className="text-sm text-muted">None yet. Download the backups you make and keep a copy off this Pi too.</p>
          ) : (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {d.backups.map((b) => (
                <li key={b.name} className="flex flex-wrap items-center gap-2 px-3 py-2">
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm">
                      {formatDateTime(b.created_at)} {b.name.includes("before-restore") && <Badge>before a restore</Badge>}
                    </span>
                    <span className="block truncate text-xs text-muted">
                      {b.name} · {size(b.size)}
                    </span>
                  </span>
                  <a
                    href={`/api/backups/${encodeURIComponent(b.name)}`}
                    download={b.name}
                    className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-line px-3 text-sm font-medium hover:bg-surface-2"
                  >
                    <Download className="size-4" aria-hidden /> Download
                  </a>
                  <IconButton label={`Delete ${b.name}`} onClick={() => remove.mutate(b.name)} disabled={remove.isPending}>
                    <Trash2 className="size-5" aria-hidden />
                  </IconButton>
                </li>
              ))}
            </ul>
          )}
          <ErrorText error={remove.error} />
        </div>
      )}

      <div className="border-t border-line pt-4">
        {restoring === "backup" ? (
          <RestoreFlow onCancel={() => setRestoring(null)} />
        ) : restoring === "radio" ? (
          <RadioRestoreFlow onCancel={() => setRestoring(null)} />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => setRestoring("backup")}>
              <ArchiveRestore className="size-4" aria-hidden /> Restore a backup…
            </Button>
            <Button onClick={() => setRestoring("radio")}>
              <Radio className="size-4" aria-hidden /> Restore a radio from a backup…
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

/** After a backup: what it saved from the radio, and what it couldn't. */
function RadioCaptured({ c }: { c: Created }) {
  if (!c.radio && !c.radio_note) return null;
  return (
    <div className="space-y-1 rounded-lg border border-line px-3 py-2 text-sm" role="status">
      {c.radio && (
        <p className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden />
          <span>
            Saved the radio <strong>{c.radio.name}</strong> ({c.radio.public_key.slice(0, 8)}…):{" "}
            {[
              c.radio.key === "included" ? "identity key" : null,
              "settings",
              `${c.radio.channels.length} channel${c.radio.channels.length === 1 ? "" : "s"}`,
              c.radio.contacts === null ? null : `${c.radio.contacts} contact${c.radio.contacts === 1 ? "" : "s"}`,
            ]
              .filter(Boolean)
              .join(", ")}
            .
          </span>
        </p>
      )}
      {c.radio_note && (
        <p className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          <span>{c.radio_note}</span>
        </p>
      )}
    </div>
  );
}

/** Upload a backup and send it with progress (raw body, so large archives are fine). */
function uploadFile(file: File, headers: Record<string, string>, onProgress: (f: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/restore/upload");
    xhr.withCredentials = true;
    for (const [k, v] of Object.entries({ ...headers, "Content-Type": "application/octet-stream" })) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve((body as { upload_id: string }).upload_id);
      else reject(new ApiError(xhr.status, errorDetail(body)));
    };
    xhr.onerror = () => reject(new Error("The upload failed; check the connection and try again."));
    xhr.send(file);
  });
}

/**
 * Choose a backup file → passphrase → check it (nothing changes yet) → see what will and won't be
 * restored here → restore. Used in Settings and by the setup wizard (with the setup token).
 */
export function RestoreFlow({ setupToken, onCancel, onDone }: { setupToken?: string; onCancel?: () => void; onDone?: (a: Applied) => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [pass, setPass] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [agree, setAgree] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const headers = setupToken ? { "X-Requested-With": REQUESTED_WITH, "X-Setup-Token": setupToken } : writeHeaders();

  const check = useMutation({
    mutationFn: async () => {
      let id = uploadId;
      if (!id) {
        setProgress(0);
        id = await uploadFile(file!, headers, setProgress);
        setUploadId(id);
      }
      return api<RestoreSummary>(`/api/restore/${id}/inspect`, { json: { passphrase: pass }, headers });
    },
    onSettled: () => setProgress(null),
  });
  const apply = useMutation({
    mutationFn: () => api<Applied>(`/api/restore/${uploadId}/apply`, { json: { passphrase: pass }, headers }),
    onSuccess: (r) => {
      setConfirming(false);
      onDone?.(r);
    },
    onError: () => setConfirming(false),
  });
  const s = check.data;

  if (apply.data) return <Restored a={apply.data} wizard={!!setupToken} />;

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold">Restore a backup</h4>
        <p className="mt-0.5 text-sm text-muted">
          The backup is checked first, and you see what will be restored before anything changes.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept=".mhb,.mchb,application/octet-stream"
          className="sr-only"
          id="restore-file"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setUploadId(null);
            check.reset();
          }}
        />
        <Button onClick={() => fileRef.current?.click()}>
          <FileUp className="size-4" aria-hidden /> {file ? "Choose another file" : "Choose backup file"}
        </Button>
        {file && (
          <span className="min-w-0 truncate text-sm text-muted">
            {file.name} · {size(file.size)}
          </span>
        )}
      </div>
      {file && (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            check.mutate();
          }}
        >
          <Field label="Backup passphrase" htmlFor="restore-pass">
            <Input
              id="restore-pass"
              type="password"
              autoComplete="off"
              value={pass}
              onChange={(e) => {
                setPass(e.target.value);
                check.reset();
              }}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={check.isPending || pass.length < MIN_PASSPHRASE}>
              {check.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <CheckCircle2 className="size-4" aria-hidden />}
              {progress !== null ? `Uploading… ${Math.round(progress * 100)}%` : check.isPending ? "Checking the backup…" : "Check backup"}
            </Button>
            {onCancel && (
              <Button variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
            )}
          </div>
          <ErrorText error={check.error} />
        </form>
      )}

      {s && <Summary s={s} />}

      {s?.can_restore && (
        <div className="space-y-3 rounded-lg border border-warn/40 bg-warn/5 p-3">
          <label className="flex cursor-pointer items-start gap-2 text-sm">
            <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="mt-0.5 size-4 accent-[var(--accent)]" />
            <span>
              {setupToken
                ? "Set up this installation from the backup."
                : "Replace everything in this installation with the backup. Everyone is signed out and signs in with the backup's account."}
            </span>
          </label>
          <Button variant={setupToken ? "primary" : "danger"} disabled={!agree || apply.isPending} onClick={() => setConfirming(true)}>
            <ArchiveRestore className="size-4" aria-hidden /> Restore
          </Button>
          <ErrorText error={apply.error} />
        </div>
      )}

      {confirming && s && (
        <Dialog
          size="sm"
          title="Restore this backup?"
          onClose={() => !apply.isPending && setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} disabled={apply.isPending} autoFocus>
                Cancel
              </Button>
              <Button variant={setupToken ? "primary" : "danger"} onClick={() => apply.mutate()} disabled={apply.isPending}>
                {apply.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                {apply.isPending ? "Restoring…" : "Restore"}
              </Button>
            </>
          }
        >
          <p className="text-sm">
            {setupToken
              ? `This installation is set up from the backup of “${s.home_name}” made ${formatDateTime(s.created_at)}.`
              : `All current data is replaced by the backup of “${s.home_name}” made ${formatDateTime(s.created_at)}. On this Pi, a copy of the current data is saved first (with the same passphrase).`}
          </p>
        </Dialog>
      )}
    </div>
  );
}

function Summary({ s }: { s: RestoreSummary }) {
  return (
    <div className="space-y-3 rounded-lg border border-line p-3 text-sm">
      <p>
        Backup of <strong>{s.home_name}</strong> made {formatDateTime(s.created_at)} by MeshHome v{s.app_version} (
        {s.install_kind === "native" ? "Raspberry Pi / Debian install" : "container"}).
      </p>
      {s.errors.length > 0 && <List tone="danger" items={s.errors} />}
      {s.restored.length > 0 && (
        <div>
          <h5 className="mb-1 font-medium">Will be restored</h5>
          <List tone="ok" items={s.restored} />
        </div>
      )}
      {s.not_restored.length > 0 && (
        <div>
          <h5 className="mb-1 font-medium">Won't be restored here</h5>
          <List tone="muted" items={s.not_restored} />
        </div>
      )}
      {s.notes.length > 0 && (
        <div>
          <h5 className="mb-1 font-medium">Afterwards</h5>
          <List tone="warn" items={s.notes} />
        </div>
      )}
    </div>
  );
}

function List({ items, tone }: { items: string[]; tone: "ok" | "muted" | "warn" | "danger" }) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "danger" ? XCircle : tone === "warn" ? AlertTriangle : XCircle;
  return (
    <ul className="space-y-1">
      {items.map((t) => (
        <li key={t} className="flex items-start gap-2">
          <Icon
            className={cx(
              "mt-0.5 size-4 shrink-0",
              tone === "ok" && "text-ok",
              tone === "danger" && "text-danger",
              tone === "warn" && "text-warn",
              tone === "muted" && "text-muted",
            )}
            aria-hidden
          />
          <span>{t}</span>
        </li>
      ))}
    </ul>
  );
}

function Restored({ a, wizard }: { a: Applied; wizard: boolean }) {
  return (
    <div className="space-y-3 rounded-lg border border-ok/40 bg-ok/5 p-4 text-sm" role="status">
      <p className="flex items-center gap-2 font-medium text-ok">
        <CheckCircle2 className="size-5" aria-hidden /> Backup restored
      </p>
      {a.system === "applying" && (
        <p>
          This Pi is now applying the restored network and HTTPS settings. The app may restart and move to the restored address
          {a.summary.restored.some((r) => r.startsWith("HTTPS for")) ? " (see above)" : ""}; reload the page in a minute if it doesn't
          reconnect.
        </p>
      )}
      {a.safety_backup && (
        <p className="text-muted">The data that was here before is saved as {a.safety_backup} under Backup &amp; restore.</p>
      )}
      <p>{wizard ? "Sign in with the account from the backup." : "Everyone has been signed out. Sign in with the account from the backup."}</p>
      <Button variant="primary" onClick={() => window.location.assign("/login")}>
        Sign in
      </Button>
    </div>
  );
}

type RadioPart = "identity" | "settings" | "channels" | "contacts";
type RadioCheck = {
  snapshot: RadioSnapshot;
  radio: { name: string; public_key: string; simulated: boolean } | null;
  same_identity: boolean;
  can_restore: boolean;
  problem: string | null;
};
type RadioStep = { part: RadioPart; ok: boolean; detail: string };

const PART_LABELS: Record<RadioPart, string> = {
  identity: "Identity key (the radio becomes this node again)",
  settings: "Name, location, radio (LoRa) and other settings",
  channels: "Channels and their keys (the radio's channels are replaced)",
  contacts: "Contacts the radio doesn't have",
};

/**
 * Put a backup's radio snapshot back on the connected radio, e.g. after reflashing its firmware.
 * Nothing else changes: MeshHome's own data stays as it is.
 */
function RadioRestoreFlow({ onCancel }: { onCancel: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [pass, setPass] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [parts, setParts] = useState<RadioPart[]>([]);
  const [confirming, setConfirming] = useState(false);
  const headers = writeHeaders();

  const check = useMutation({
    mutationFn: async () => {
      let id = uploadId;
      if (!id) {
        setProgress(0);
        id = await uploadFile(file!, headers, setProgress);
        setUploadId(id);
      }
      return api<RadioCheck>(`/api/radio-restore/${id}/inspect`, { json: { passphrase: pass }, headers });
    },
    onSuccess: (r) => {
      // Everything the backup has; the identity only when the radio is someone else now.
      const all: RadioPart[] = ["identity", "settings", "channels", "contacts"];
      setParts(
        all.filter(
          (p) =>
            !(p === "identity" && (r.same_identity || r.snapshot.key !== "included")) && !(p === "contacts" && r.snapshot.contacts === null),
        ),
      );
    },
    onSettled: () => setProgress(null),
  });
  const apply = useMutation({
    mutationFn: () => api<{ steps: RadioStep[] }>(`/api/radio-restore/${uploadId}/apply`, { json: { passphrase: pass, parts }, headers }),
    onSettled: () => setConfirming(false),
  });
  const c = check.data;

  if (apply.data) {
    const failed = apply.data.steps.filter((s) => !s.ok);
    return (
      <div className="space-y-3 rounded-lg border border-line p-4 text-sm" role="status">
        <p className={cx("flex items-center gap-2 font-medium", failed.length ? "text-warn" : "text-ok")}>
          {failed.length ? <AlertTriangle className="size-5" aria-hidden /> : <CheckCircle2 className="size-5" aria-hidden />}
          {failed.length ? "The radio was partly restored" : "Radio restored"}
        </p>
        <ul className="space-y-1">
          {apply.data.steps.map((s) => (
            <li key={s.part} className="flex items-start gap-2">
              {s.ok ? (
                <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden />
              ) : (
                <XCircle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
              )}
              <span>
                <span className="font-medium">{PART_LABELS[s.part].split(" (")[0]}:</span> {s.detail}
              </span>
            </li>
          ))}
        </ul>
        <p className="text-muted">MeshHome is reconnecting to the radio to read it again.</p>
        <Button onClick={onCancel}>Done</Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold">Restore a radio from a backup</h4>
        <p className="mt-0.5 text-sm text-muted">
          Puts a backup&apos;s radio identity, settings, channels and contacts back on the connected radio, for example after
          reflashing its firmware. MeshHome&apos;s own data doesn&apos;t change.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept=".mhb,.mchb,application/octet-stream"
          className="sr-only"
          id="radio-restore-file"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setUploadId(null);
            check.reset();
          }}
        />
        <Button onClick={() => fileRef.current?.click()}>
          <FileUp className="size-4" aria-hidden /> {file ? "Choose another file" : "Choose backup file"}
        </Button>
        {file && (
          <span className="min-w-0 truncate text-sm text-muted">
            {file.name} · {size(file.size)}
          </span>
        )}
      </div>
      {file && (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            check.mutate();
          }}
        >
          <Field label="Backup passphrase" htmlFor="radio-restore-pass">
            <Input
              id="radio-restore-pass"
              type="password"
              autoComplete="off"
              value={pass}
              onChange={(e) => {
                setPass(e.target.value);
                check.reset();
              }}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={check.isPending || pass.length < MIN_PASSPHRASE}>
              {check.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <CheckCircle2 className="size-4" aria-hidden />}
              {progress !== null ? `Uploading… ${Math.round(progress * 100)}%` : check.isPending ? "Checking the backup…" : "Check backup"}
            </Button>
            <Button variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
          </div>
          <ErrorText error={check.error} />
        </form>
      )}

      {c && (
        <div className="space-y-3 rounded-lg border border-line p-3 text-sm">
          <p>
            Radio <strong>{c.snapshot.name}</strong> ({c.snapshot.public_key.slice(0, 8)}…), saved {formatDateTime(c.snapshot.captured_at)}
            {c.snapshot.model ? ` from a ${c.snapshot.model}` : ""}
            {c.snapshot.firmware ? ` running ${c.snapshot.firmware}` : ""}.
          </p>
          <p className="text-muted">
            {c.snapshot.channels.length} channel{c.snapshot.channels.length === 1 ? "" : "s"}
            {c.snapshot.channels.length ? ` (${c.snapshot.channels.join(", ")})` : ""}
            {c.snapshot.contacts !== null ? ` · ${c.snapshot.contacts} contacts` : ""}
            {c.snapshot.radio.freq_mhz ? ` · ${c.snapshot.radio.freq_mhz} MHz SF${c.snapshot.radio.sf} BW${c.snapshot.radio.bw_khz}` : ""}
          </p>
          {c.snapshot.key !== "included" && (
            <p className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
              {c.snapshot.key === "disabled"
                ? "The identity key isn't in this backup: the radio's firmware didn't allow exporting it."
                : "The identity key isn't in this backup: it couldn't be read from the radio."}
            </p>
          )}
          {c.problem ? (
            <p className="flex items-start gap-2 text-danger">
              <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden /> {c.problem}
            </p>
          ) : (
            c.radio && (
              <p>
                Connected now: <strong>{c.radio.name}</strong> ({c.radio.public_key.slice(0, 8)}…)
                {c.same_identity ? ", the same identity as in the backup." : ", a different identity from the backup."}
              </p>
            )
          )}
        </div>
      )}

      {c?.can_restore && (
        <div className="space-y-3 rounded-lg border border-warn/40 bg-warn/5 p-3">
          <fieldset className="space-y-2">
            <legend className="mb-1 text-sm font-medium">Restore</legend>
            {(Object.keys(PART_LABELS) as RadioPart[]).map((p) => {
              const unavailable =
                (p === "identity" && (c.snapshot.key !== "included" || c.same_identity)) || (p === "contacts" && c.snapshot.contacts === null);
              return (
                <label key={p} className={cx("flex items-start gap-2 text-sm", unavailable ? "text-muted" : "cursor-pointer")}>
                  <input
                    type="checkbox"
                    disabled={unavailable}
                    checked={parts.includes(p)}
                    onChange={(e) => setParts(e.target.checked ? [...parts, p] : parts.filter((x) => x !== p))}
                    className="mt-0.5 size-4 accent-[var(--accent)]"
                  />
                  <span>
                    {PART_LABELS[p]}
                    {p === "identity" && c.same_identity ? " — the radio already has it" : ""}
                  </span>
                </label>
              );
            })}
          </fieldset>
          {parts.includes("identity") && (
            <p className="flex items-start gap-2 text-sm">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
              The connected radio takes over the backup&apos;s identity. Don&apos;t do this while another radio with that identity
              is still on the mesh.
            </p>
          )}
          <Button variant="danger" disabled={parts.length === 0 || apply.isPending} onClick={() => setConfirming(true)}>
            <Radio className="size-4" aria-hidden /> Restore the radio
          </Button>
          <ErrorText error={apply.error} />
        </div>
      )}

      {confirming && c && (
        <Dialog
          size="sm"
          title="Restore the radio?"
          onClose={() => !apply.isPending && setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} disabled={apply.isPending} autoFocus>
                Cancel
              </Button>
              <Button variant="danger" onClick={() => apply.mutate()} disabled={apply.isPending}>
                {apply.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                {apply.isPending ? "Restoring…" : "Restore"}
              </Button>
            </>
          }
        >
          <p className="text-sm">
            {c.radio?.name ?? "The radio"} gets {parts.map((p) => PART_LABELS[p].split(" (")[0].toLowerCase()).join(", ")} from the backup
            of {c.snapshot.name}. It may take a minute; MeshHome reconnects to the radio afterwards.
          </p>
        </Dialog>
      )}
    </div>
  );
}
