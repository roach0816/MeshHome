import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Check, ChevronLeft, ChevronRight, Copy, Globe, Hash, KeyRound, Lock, ScanQrCode } from "lucide-react";
import { api, type NodeConfig } from "../lib/api";
import {
  channelLink,
  normaliseScope,
  parseLink,
  PUBLIC_KEY_HEX,
  scopeError,
  type ChannelLink,
} from "../lib/channelLink";
import { canCopy, cx, utf8Bytes } from "../lib/util";
import { Dialog } from "./Dialog";
import { QrCode } from "./QrCode";
import { QrScanner } from "./QrScanner";
import { Button, ErrorText, Field, IconButton, Input } from "./ui";

type KeyMode = "hashtag" | "public" | "random" | "custom";
type AddResult = {
  slot: number;
  name: string;
  conversation_id: string | null;
  config: NodeConfig;
  share?: { hex: string; base64: string };
};
type Step =
  | { kind: "menu" }
  | { kind: "create" }
  | { kind: "private"; prefill?: ChannelLink }
  | { kind: "public"; prefill?: ChannelLink }
  | { kind: "hashtag"; prefill?: ChannelLink }
  | { kind: "scan" }
  | { kind: "created"; result: AddResult; scope: string };

const TITLES: Record<Step["kind"], string> = {
  menu: "Add channel",
  create: "Create a private channel",
  private: "Join a private channel",
  public: "Join the Public channel",
  hashtag: "Join a hashtag channel",
  scan: "Scan a channel QR code",
  created: "Channel created",
};

/**
 * The MeshCore app's "Add channel" flows: create or join a private channel, join Public, join a
 * hashtag channel, or scan a channel QR code. The server picks the first free slot on the radio.
 */
export function AddChannelDialog({ onClose, openOnAdd = true }: { onClose: () => void; openOnAdd?: boolean }) {
  const [step, setStep] = useState<Step>({ kind: "menu" });
  const qc = useQueryClient();
  const navigate = useNavigate();

  const add = useMutation({
    mutationFn: (v: { name: string; key_mode: KeyMode; key?: string; flood_scope: string }) =>
      api<AddResult>("/api/radio/channels", { method: "POST", json: v }),
    onSuccess: (r, v) => {
      qc.setQueryData(["node-config"], r.config);
      qc.invalidateQueries({ queryKey: ["conversations"] });
      if (v.key_mode === "random") {
        setStep({ kind: "created", result: r, scope: v.flood_scope });
      } else {
        onClose();
        if (openOnAdd && r.conversation_id) navigate(`/c/${r.conversation_id}`);
      }
    },
  });
  const go = (s: Step) => {
    add.reset();
    setStep(s);
  };

  const onScanned = (text: string) => {
    const parsed = parseLink(text);
    if (parsed.kind !== "channel") return parsed.kind === "contact" ? "contact" : parsed.reason;
    const link = parsed.link;
    if (link.secret === PUBLIC_KEY_HEX) go({ kind: "public", prefill: link });
    else if (link.name.startsWith("#")) go({ kind: "hashtag", prefill: link });
    else go({ kind: "private", prefill: link });
    return null;
  };

  const back =
    step.kind !== "menu" && step.kind !== "created" ? (
      <Button variant="ghost" className="mr-auto" onClick={() => go({ kind: "menu" })}>
        <ChevronLeft className="size-4" aria-hidden /> Back
      </Button>
    ) : null;

  return (
    <Dialog title={TITLES[step.kind]} onClose={onClose}>
      {step.kind === "menu" && <Menu onPick={(kind) => go({ kind } as Step)} />}
      {step.kind === "create" && <CreateForm add={add} back={back} />}
      {step.kind === "private" && <JoinPrivateForm add={add} back={back} prefill={step.prefill} />}
      {step.kind === "public" && <JoinPublicForm add={add} back={back} prefill={step.prefill} />}
      {step.kind === "hashtag" && <HashtagForm add={add} back={back} prefill={step.prefill} />}
      {step.kind === "scan" && <ScanStep back={back} onScanned={onScanned} />}
      {step.kind === "created" && (
        <div className="space-y-4">
          <p className="text-sm">
            <strong>{step.result.name}</strong> is on the radio. Share it with the people you want in the channel.
            <strong> The key is shown only this once</strong>: this app does not keep a copy it can show again.
          </p>
          {step.result.share && (
            <SharePanel link={{ name: step.result.name, secret: step.result.share.hex, scope: step.scope }} />
          )}
          <Footer>
            <Button
              variant="primary"
              onClick={() => {
                onClose();
                if (openOnAdd && step.result.conversation_id) navigate(`/c/${step.result.conversation_id}`);
              }}
            >
              {openOnAdd ? "Open channel" : "Done"}
            </Button>
          </Footer>
        </div>
      )}
    </Dialog>
  );
}

type AddMutation = ReturnType<
  typeof useMutation<AddResult, Error, { name: string; key_mode: KeyMode; key?: string; flood_scope: string }>
>;

function Menu({ onPick }: { onPick: (k: "create" | "private" | "public" | "hashtag" | "scan") => void }) {
  const items = [
    { k: "create", icon: Lock, title: "Create a private channel", text: "Generate a new secret key to share with the people you invite." },
    { k: "private", icon: KeyRound, title: "Join a private channel", text: "Enter the name and secret key someone shared with you." },
    { k: "public", icon: Globe, title: "Join the Public channel", text: "The open channel every MeshCore radio starts with." },
    { k: "hashtag", icon: Hash, title: "Join a hashtag channel", text: "Anyone who knows the name can join, because the key comes from the name." },
    { k: "scan", icon: ScanQrCode, title: "Scan a QR code", text: "Join a channel shared from the MeshCore app or another MeshHome." },
  ] as const;
  return (
    <ul className="-mx-2 space-y-1">
      {items.map(({ k, icon: Icon, title, text }) => (
        <li key={k}>
          <button
            type="button"
            onClick={() => onPick(k)}
            className="flex w-full items-center gap-3 rounded-lg px-2 py-2.5 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none"
          >
            <span aria-hidden className="flex size-10 shrink-0 items-center justify-center rounded-full bg-accent-soft text-accent">
              <Icon className="size-5" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">{title}</span>
              <span className="block text-xs text-muted">{text}</span>
            </span>
            <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );
}

function Footer({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-3">{children}</div>;
}

function ScopeField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Field
      label="Region scope (optional)"
      htmlFor="ch-scope"
      error={scopeError(value)}
      hint="Limits how far this channel's messages flood to repeaters that serve this region, for example #boulder. Leave empty to use the radio's default."
    >
      <Input
        id="ch-scope"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="#region"
        autoCapitalize="none"
        spellCheck={false}
      />
    </Field>
  );
}

function nameError(name: string, hashtag: boolean): string | null {
  const n = name.trim();
  if (utf8Bytes(n) > 31) return "At most 31 bytes";
  if (!hashtag && n.startsWith("#")) return 'Names starting with "#" are hashtag channels. Use "Join a hashtag channel".';
  if (hashtag && /\s/.test(n)) return "Hashtag names cannot contain spaces";
  return null;
}

function CreateForm({ add, back }: { add: AddMutation; back: ReactNode }) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState("");
  const err = nameError(name, false);
  const valid = name.trim() && !err && !scopeError(scope);
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) add.mutate({ name: name.trim(), key_mode: "random", flood_scope: normaliseScope(scope) });
      }}
    >
      <Field label="Channel name" htmlFor="ch-name" error={err} hint="Shown in the channel list. Members can use any name for it.">
        <Input id="ch-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Family" />
      </Field>
      <ScopeField value={scope} onChange={setScope} />
      <p className="text-xs text-muted">A new random 128-bit key is generated on the server and saved to the radio.</p>
      <ErrorText error={add.error} />
      <Footer>
        {back}
        <Button type="submit" variant="primary" disabled={!valid || add.isPending}>
          {add.isPending ? "Creating…" : "Create channel"}
        </Button>
      </Footer>
    </form>
  );
}

function JoinPrivateForm({ add, back, prefill }: { add: AddMutation; back: ReactNode; prefill?: ChannelLink }) {
  const [name, setName] = useState(prefill?.name ?? "");
  const [key, setKey] = useState(prefill?.secret ?? "");
  const [scope, setScope] = useState(prefill?.scope ?? "");
  const err = nameError(name, false);
  const k = key.trim();
  const keyOk = /^[0-9a-fA-F]{32}$/.test(k) || /^[A-Za-z0-9+/]{22}==$/.test(k);
  const valid = name.trim() && !err && keyOk && !scopeError(scope);
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) add.mutate({ name: name.trim(), key_mode: "custom", key: k, flood_scope: normaliseScope(scope) });
      }}
    >
      {prefill && <p className="rounded-lg bg-accent-soft/50 px-3 py-2 text-sm">Read from the QR code. Check the details, then join.</p>}
      <Field label="Channel name" htmlFor="ch-name" error={err}>
        <Input id="ch-name" autoFocus={!prefill} value={name} onChange={(e) => setName(e.target.value)} placeholder="Family" />
      </Field>
      <Field
        label="Secret key"
        htmlFor="ch-key"
        error={k && !keyOk ? "Enter 32 hex characters or a 24-character base64 key" : null}
        hint="32 hex characters (as the MeshCore app shows it) or base64."
      >
        <Input
          id="ch-key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          className="font-mono"
          spellCheck={false}
          autoCapitalize="none"
          autoComplete="off"
          placeholder="e.g. 8b3387e9c5cdea6ac9e5edbaa115cd72"
        />
      </Field>
      <ScopeField value={scope} onChange={setScope} />
      <ErrorText error={add.error} />
      <Footer>
        {back}
        <Button type="submit" variant="primary" disabled={!valid || add.isPending}>
          {add.isPending ? "Joining…" : "Join channel"}
        </Button>
      </Footer>
    </form>
  );
}

function JoinPublicForm({ add, back, prefill }: { add: AddMutation; back: ReactNode; prefill?: ChannelLink }) {
  const [scope, setScope] = useState(prefill?.scope ?? "");
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate({ name: "Public", key_mode: "public", flood_scope: normaliseScope(scope) });
      }}
    >
      <p className="text-sm">
        <strong>Public</strong> is the open channel that every MeshCore radio ships with. Anyone nearby can read it, so
        don't share anything private there.
      </p>
      <ScopeField value={scope} onChange={setScope} />
      <ErrorText error={add.error} />
      <Footer>
        {back}
        <Button type="submit" variant="primary" disabled={!!scopeError(scope) || add.isPending}>
          {add.isPending ? "Joining…" : "Join Public"}
        </Button>
      </Footer>
    </form>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function HashtagForm({ add, back, prefill }: { add: AddMutation; back: ReactNode; prefill?: ChannelLink }) {
  const [name, setName] = useState(prefill?.name.replace(/^#/, "") ?? "");
  const [scope, setScope] = useState(prefill?.scope ?? "");
  const full = `#${name.trim().replace(/^#+/, "")}`;
  const err = nameError(full, true);
  const ready = full.length > 1 && !err;
  const debounced = useDebounced(ready ? full : "", 250);
  // The key is derived from the name (SHA-256), so it is not secret; the server computes it.
  const key = useQuery({
    queryKey: ["hashtag-key", debounced],
    queryFn: () => api<{ hex: string }>(`/api/radio/channels/hashtag-key?name=${encodeURIComponent(debounced)}`),
    enabled: !!debounced,
    staleTime: Infinity,
  });
  const keyHex = debounced === full ? key.data?.hex : undefined;
  const mismatch = prefill && keyHex && prefill.secret !== keyHex && full === prefill.name;
  const valid = ready && !scopeError(scope);
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) add.mutate({ name: full, key_mode: "hashtag", flood_scope: normaliseScope(scope) });
      }}
    >
      <Field
        label="Hashtag"
        htmlFor="ch-tag"
        error={err}
        hint="Names are case-sensitive: #Hikers and #hikers are different channels."
      >
        <div className="relative">
          <Hash className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            id="ch-tag"
            autoFocus={!prefill}
            value={name}
            onChange={(e) => setName(e.target.value.replace(/^#+/, ""))}
            className="pl-8"
            placeholder="hikers"
            autoCapitalize="none"
            spellCheck={false}
          />
        </div>
      </Field>
      <ScopeField value={scope} onChange={setScope} />
      {ready && (
        <div className="rounded-xl border border-line p-3">
          {keyHex ? (
            <SharePanel link={{ name: full, secret: keyHex, scope: normaliseScope(scope) }} compact />
          ) : (
            <p className="py-6 text-center text-sm text-muted">{key.isError ? "Could not compute the key." : "Computing key…"}</p>
          )}
        </div>
      )}
      {mismatch && (
        <p role="alert" className="text-sm text-warn">
          The scanned key doesn't match the key for {full}. Joining uses the key derived from the name.
        </p>
      )}
      <ErrorText error={add.error} />
      <Footer>
        {back}
        <Button type="submit" variant="primary" disabled={!valid || add.isPending}>
          {add.isPending ? "Joining…" : `Join ${ready ? full : "channel"}`}
        </Button>
      </Footer>
    </form>
  );
}

function ScanStep({ back, onScanned }: { back: ReactNode; onScanned: (text: string) => string | null }) {
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">Scan a channel QR code from the MeshCore app or from another MeshHome.</p>
      {error ? (
        <div className="space-y-3">
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">
            {error === "contact" ? "That QR code is for a contact, not a channel." : error}
          </p>
          <Button
            className="w-full"
            onClick={() => {
              setError("");
              setAttempt((a) => a + 1);
            }}
          >
            Scan again
          </Button>
        </div>
      ) : (
        <QrScanner key={attempt} onResult={(text) => setError(onScanned(text) ?? "")} />
      )}
      <Footer>{back}</Footer>
    </div>
  );
}

function CopyRow({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-muted">{label}</p>
      <div className="flex items-center gap-1">
        <code
          className={cx(
            "min-w-0 flex-1 select-all break-all rounded-md bg-surface-2 px-2 py-1.5 text-xs",
            mono ? "font-mono" : "",
          )}
        >
          {value}
        </code>
        {canCopy() && (
        <IconButton
          type="button"
          label={copied ? "Copied" : `Copy ${label.toLowerCase()}`}
          onClick={() =>
            navigator.clipboard
              ?.writeText(value)
              .then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              })
              .catch(() => {})
          }
        >
          {copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
        </IconButton>
        )}
      </div>
    </div>
  );
}

/** QR code plus the secret key and the meshcore:// link, for sharing a channel. */
export function SharePanel({ link, compact = false }: { link: ChannelLink; compact?: boolean }) {
  const url = channelLink(link);
  return (
    <div className={cx("flex flex-col items-center gap-3", !compact && "sm:flex-row sm:items-start")}>
      <QrCode
        value={url}
        label={`QR code for ${link.name}`}
        className={cx("shrink-0 rounded-lg border border-line", compact ? "size-40" : "size-44")}
      />
      <div className="w-full min-w-0 space-y-2">
        <CopyRow label="Secret key" value={link.secret} />
        <CopyRow label="Share link" value={url} />
        {link.scope && <p className="text-xs text-muted">Region scope #{link.scope} is included in the link.</p>}
      </div>
    </div>
  );
}
