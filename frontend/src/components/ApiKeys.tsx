import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { api, type ApiKey, type ApiKeyCreated, type ApiKeyScope } from "../lib/api";
import { canCopy, cx, formatDateTime } from "../lib/util";
import { Dialog } from "./Dialog";
import { Badge, Button, Card, ErrorText, Field, IconButton, Input } from "./ui";
import { InfoTip } from "./InfoTip";

const SCOPES: { value: ApiKeyScope; label: string; hint: string }[] = [
  { value: "read", label: "Read only", hint: "Read conversations, messages, contacts, status and the map." },
  {
    value: "write",
    label: "Read & write",
    hint: "Also send messages, mark conversations read, and act on conversations and contacts.",
  },
];
const EXPIRY = [
  { value: 0, label: "Never" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 365, label: "1 year" },
];

function useApiKeys() {
  return useQuery({ queryKey: ["api-keys"], queryFn: () => api<ApiKey[]>("/api/api-keys") });
}

/** Settings → API keys: keys that let other services and scripts use this MeshHome. */
export function ApiKeysSection() {
  const keys = useApiKeys();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  return (
    <Card className="p-4 sm:p-5">
      <h3 className="flex items-center gap-1 text-base font-semibold">
        API keys
        <InfoTip label="About API keys">
          <p>
            Each request carries the key in an <code className="font-mono">Authorization: Bearer</code> header.
          </p>
          <p>Account, network, software and radio settings stay limited to this web interface.</p>
        </InfoTip>
      </h3>
      <p className="mt-0.5 text-sm text-muted">
        For scripts and services that read and send messages.{" "}
        <a href="/api/docs" target="_blank" rel="noopener" className="text-accent underline">
          API reference
        </a>
      </p>
      <div className="mt-4 space-y-4">
        <ErrorText error={keys.error} />
        {keys.data && keys.data.length > 0 && (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {keys.data.map((k) => (
              <li key={k.id} className="flex items-center gap-3 px-3 py-2.5">
                <KeyRound className="size-4 shrink-0 text-muted" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="truncate text-sm font-medium">{k.name}</span>
                    <Badge tone={k.scope === "write" ? "accent" : "muted"}>{k.scope === "write" ? "Read & write" : "Read only"}</Badge>
                    {k.expired && <Badge tone="danger">Expired</Badge>}
                  </p>
                  <p className="mt-0.5 break-words text-xs text-muted">
                    <span className="font-mono">{k.prefix}…</span> · created {formatDateTime(k.created_at)} · last used{" "}
                    {k.last_used_at ? formatDateTime(k.last_used_at) : "never"}
                    {k.expires_at && !k.expired && <> · expires {formatDateTime(k.expires_at)}</>}
                  </p>
                </div>
                <IconButton label={`Revoke ${k.name}`} onClick={() => setRevoking(k)}>
                  <Trash2 className="size-4" />
                </IconButton>
              </li>
            ))}
          </ul>
        )}
        {keys.data?.length === 0 && <p className="text-sm text-muted">No API keys yet.</p>}
        <Button onClick={() => setCreating(true)}>
          <Plus className="size-4" aria-hidden /> Create API key
        </Button>
        {creating && <CreateKeyDialog onClose={() => setCreating(false)} />}
        {revoking && <RevokeDialog k={revoking} onClose={() => setRevoking(null)} />}
      </div>
    </Card>
  );
}

function CreateKeyDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<ApiKeyScope>("read");
  const [expiry, setExpiry] = useState(0);
  const create = useMutation({
    mutationFn: () =>
      api<ApiKeyCreated>("/api/api-keys", {
        method: "POST",
        json: { name: name.trim(), scope, expires_in_days: expiry || null },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });

  if (create.data) return <NewKeyDialog created={create.data} onClose={onClose} />;

  const valid = name.trim().length > 0 && name.trim().length <= 64;
  return (
    <Dialog
      title="Create API key"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? "Creating…" : "Create key"}
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) create.mutate();
        }}
      >
        <Field label="Name" htmlFor="key-name" hint="What will use this key, so you can tell keys apart later.">
          <Input id="key-name" autoFocus maxLength={64} value={name} onChange={(e) => setName(e.target.value)} placeholder="Home Assistant" />
        </Field>
        <fieldset className="space-y-2">
          <legend className="mb-1 text-sm font-medium">Permission</legend>
          {SCOPES.map((s) => (
            <label
              key={s.value}
              className={cx(
                "flex cursor-pointer gap-3 rounded-lg border p-3",
                scope === s.value ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
              )}
            >
              <input
                type="radio"
                name="key-scope"
                checked={scope === s.value}
                onChange={() => setScope(s.value)}
                className="mt-1 accent-[var(--accent)]"
              />
              <span>
                <span className="block text-sm font-medium">{s.label}</span>
                <span className="block text-xs text-muted">{s.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <Field label="Expires" htmlFor="key-expiry">
          <select
            id="key-expiry"
            value={expiry}
            onChange={(e) => setExpiry(Number(e.target.value))}
            className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base sm:text-sm"
          >
            {EXPIRY.map((x) => (
              <option key={x.value} value={x.value}>
                {x.label}
              </option>
            ))}
          </select>
        </Field>
        <ErrorText error={create.error} />
      </form>
    </Dialog>
  );
}

function NewKeyDialog({ created, onClose }: { created: ApiKeyCreated; onClose: () => void }) {
  const [copied, setCopied] = useState<string | null>(null);
  const example = `curl -H "Authorization: Bearer ${created.key}" ${window.location.origin}/api/conversations`;
  const copy = (what: string, text: string) =>
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(what);
        setTimeout(() => setCopied(null), 1500);
      })
      .catch(() => {});
  return (
    <Dialog
      title={`API key “${created.api_key.name}”`}
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          I've saved it
        </Button>
      }
    >
      <div className="space-y-4 text-sm">
        <p>
          Copy this key now and store it somewhere safe, such as the other service's secret settings.{" "}
          <strong>It is shown only once</strong>: MeshHome keeps only a fingerprint of it.
        </p>
        <div className="flex items-center gap-1">
          <code className="min-w-0 flex-1 select-all break-all rounded-md bg-surface-2 px-2 py-2 font-mono text-xs">{created.key}</code>
          {canCopy() && (
            <IconButton label={copied === "key" ? "Copied" : "Copy key"} onClick={() => copy("key", created.key)}>
              {copied === "key" ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
            </IconButton>
          )}
        </div>
        <div>
          <p className="mb-1 text-xs font-medium text-muted">Try it</p>
          <div className="flex items-center gap-1">
            <code className="min-w-0 flex-1 select-all break-all rounded-md bg-surface-2 px-2 py-2 font-mono text-xs">{example}</code>
            {canCopy() && (
              <IconButton label={copied === "curl" ? "Copied" : "Copy example"} onClick={() => copy("curl", example)}>
                {copied === "curl" ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
              </IconButton>
            )}
          </div>
        </div>
        <p className="text-xs text-muted">
          {created.api_key.scope === "write" ? "Read & write" : "Read only"}
          {created.api_key.expires_at ? ` · expires ${formatDateTime(created.api_key.expires_at)}` : " · never expires"}.
          Anyone with this key can use it, so revoke it here if it leaks.
        </p>
      </div>
    </Dialog>
  );
}

function RevokeDialog({ k, onClose }: { k: ApiKey; onClose: () => void }) {
  const qc = useQueryClient();
  const revoke = useMutation({
    mutationFn: () => api<void>(`/api/api-keys/${k.id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      onClose();
    },
  });
  return (
    <Dialog
      size="sm"
      title={`Revoke “${k.name}”?`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} autoFocus>
            Cancel
          </Button>
          <Button variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate()}>
            Revoke key
          </Button>
        </>
      }
    >
      <p className="text-sm">Anything using this key stops working immediately. This can't be undone.</p>
      <ErrorText error={revoke.error} />
    </Dialog>
  );
}
