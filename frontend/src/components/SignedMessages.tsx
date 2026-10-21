import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, IdCard, ShieldCheck, ShieldQuestion, ShieldX, UserPlus } from "lucide-react";
import { api, type Contact, type Conversation, type MessageSignature, type MyContact } from "../lib/api";
import { canCopy, cx, newClientId } from "../lib/util";
import { Dialog } from "./Dialog";
import { QrCode } from "./QrCode";
import { QrScanner } from "./QrScanner";
import { Button, ErrorText, Input } from "./ui";

// Signed channel messages (backend: services/signed_messages.py). A signature is checked against
// the signer's key; "trust" is whether that key is one of the radio's contacts, which you add by
// exchanging contact links and checking the start of the key with the person.

/** "B3F8 CE5B 40D1 A9E2": the start of a public key, in groups, for reading out and comparing. */
export function keyStart(hex: string, bytes = 8): string {
  return (hex.slice(0, bytes * 2).toUpperCase().match(/.{1,4}/g) ?? []).join(" ");
}

const STATE: Record<MessageSignature["state"], { label: (s: MessageSignature) => string; tone: string; Icon: typeof ShieldCheck }> = {
  self: { label: () => "Signed by your radio", tone: "", Icon: ShieldCheck },
  contact: { label: (s) => `Signed by ${s.signer_name ?? "a contact"}, one of your contacts`, tone: "text-ok", Icon: ShieldCheck },
  known: {
    label: (s) => `Signed by ${s.signer_name ?? "this sender"}'s key, but they aren't one of your contacts`,
    tone: "text-muted",
    Icon: ShieldCheck,
  },
  unknown: {
    label: (s) => `Signed by a key your radio hasn't heard yet (${keyStart(s.key_id)}), so it can't be checked`,
    tone: "text-muted",
    Icon: ShieldQuestion,
  },
  invalid: {
    label: () => "The signature doesn't match: the text was changed, or it isn't really from that sender",
    tone: "text-danger",
    Icon: ShieldX,
  },
};

export function signatureLabel(sig: MessageSignature): string {
  return STATE[sig.state].label(sig);
}

/** The tick beside a signed message's time. */
export function SignatureTick({ sig }: { sig: MessageSignature }) {
  const { Icon, tone } = STATE[sig.state];
  return (
    <span className={cx("inline-flex", tone)} title={signatureLabel(sig)}>
      <Icon className="size-3.5" aria-hidden />
      <span className="sr-only">{signatureLabel(sig)}</span>
    </span>
  );
}

/** The signer, in a message's expanded details. */
export function SignatureDetails({ sig }: { sig: MessageSignature }) {
  return (
    <>
      <dt>Signature</dt>
      <dd>
        {signatureLabel(sig)}
        {sig.signer_key && sig.state !== "self" && (
          <span className="block font-mono text-[10px]">key {keyStart(sig.signer_key)} …</span>
        )}
      </dd>
    </>
  );
}

function Switch({
  id,
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label htmlFor={id} className={cx("flex items-start gap-3", disabled && "opacity-60")}>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium">{label}</span>
        <span className="mt-0.5 block text-xs text-muted">{hint}</span>
      </span>
      <span className="relative mt-0.5 inline-flex shrink-0">
        <input
          id={id}
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="peer sr-only"
        />
        <span
          aria-hidden
          className="h-6 w-11 rounded-full bg-line transition-colors peer-checked:bg-accent peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[var(--accent)]"
        />
        <span
          aria-hidden
          className="absolute left-0.5 top-0.5 size-5 rounded-full bg-surface shadow transition-transform peer-checked:translate-x-5"
        />
      </span>
    </label>
  );
}

function useMyContact() {
  return useQuery({ queryKey: ["my-contact"], queryFn: () => api<MyContact>("/api/contacts/me"), staleTime: 60_000 });
}

/** A channel's signed-message settings (the conversation menu and header). */
export function SignedMessagesDialog({ conv, onClose }: { conv: Conversation; onClose: () => void }) {
  const qc = useQueryClient();
  const me = useMyContact();
  const [shared, setShared] = useState(false);
  const save = useMutation({
    mutationFn: (patch: { sign_messages?: boolean; contacts_only?: boolean }) =>
      api(`/api/conversations/${conv.id}`, { method: "PATCH", json: patch }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["messages", conv.id] });
    },
  });
  const share = useMutation({
    mutationFn: () =>
      api(`/api/conversations/${conv.id}/messages`, {
        json: { client_message_id: newClientId(), body: me.data!.uri, sign: false },
      }),
    onSuccess: () => {
      setShared(true);
      qc.invalidateQueries({ queryKey: ["messages", conv.id] });
    },
  });
  const signing = !!conv.sign_messages;
  const unsignedBytes = conv.max_bytes + (signing ? 75 : 0);

  return (
    <Dialog
      title="Signed messages"
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="space-y-5 text-sm">
        <p className="text-muted">
          A signed message carries a signature after the text, made by your radio with its own key. MeshHome
          and MeshOS check it against the sender's key; other MeshCore apps and radios show the text as usual. Signed messages
          are shorter, as the signature takes up part of the packet.
        </p>
        <Switch
          id="sign-messages"
          checked={signing}
          disabled={save.isPending}
          onChange={(v) => save.mutate({ sign_messages: v })}
          label="Sign my messages"
          hint={`In ${conv.title}. Messages can be up to ${unsignedBytes - 75} bytes instead of ${unsignedBytes}.`}
        />
        <Switch
          id="contacts-only"
          checked={!!conv.contacts_only}
          disabled={save.isPending}
          onChange={(v) => save.mutate({ contacts_only: v })}
          label="Only messages from my contacts"
          hint="Each message here has to be signed by the person who sent it, and they have to be one of your radio's contacts. Anything else is hidden, and comes back if you add them as a contact or turn this off."
        />
        <ErrorText error={save.error} />

        <div className="space-y-2 rounded-xl bg-surface-2 p-3">
          <p className="font-medium">Ticks beside the time</p>
          <ul className="space-y-1 text-xs text-muted">
            <li className="flex gap-2">
              <ShieldCheck className="size-4 shrink-0 text-ok" aria-hidden /> Signed by one of your contacts
            </li>
            <li className="flex gap-2">
              <ShieldCheck className="size-4 shrink-0" aria-hidden /> Signature checks out, but the sender isn't a contact
            </li>
            <li className="flex gap-2">
              <ShieldQuestion className="size-4 shrink-0" aria-hidden /> Signed by a key your radio hasn't heard yet
            </li>
            <li className="flex gap-2">
              <ShieldX className="size-4 shrink-0 text-danger" aria-hidden /> Doesn't match: altered, or not from that sender
            </li>
          </ul>
          <p className="text-xs text-muted">No tick: not signed. Tap a message to see who signed it.</p>
        </div>

        <div className="space-y-2">
          <p className="font-medium">Share my contact</p>
          <p className="text-xs text-muted">
            Posts your contact card in {conv.title}. Others can add you to their radio's contacts with one tap, to message you
            and to count your signed messages as a contact's. It goes out as a MeshCore contact link, so other MeshCore apps
            can use it too. Contact cards are too long to carry a signature.
          </p>
          {me.data && (
            <p className="text-xs text-muted">
              Your key starts <span className="font-mono text-ink">{keyStart(me.data.public_key)}</span>: read it out so they
              can check it before adding you.
            </p>
          )}
          <Button onClick={() => share.mutate()} disabled={!me.data || share.isPending || shared}>
            <IdCard className="size-4" aria-hidden /> {shared ? "Shared" : "Share my contact"}
          </Button>
          <ErrorText error={share.error ?? me.error} />
        </div>
      </div>
    </Dialog>
  );
}

const CONTACT_LINK = /^meshcore:\/\/contact\/add\?\S+$/;

/** The name, key and node type in a MeshCore contact link, if a message (or QR code) is one. */
export function contactLink(body: string): { uri: string; name: string; key: string; kind: number } | null {
  const uri = body.trim();
  if (!CONTACT_LINK.test(uri)) return null;
  try {
    const q = new URL(uri.replace(/^meshcore:\/\//, "https://x/")).searchParams;
    const key = (q.get("public_key") ?? "").toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(key)) return null;
    const kind = Number(q.get("type") ?? "1");
    return { uri, name: q.get("name") ?? "", key, kind: [1, 2, 3, 4].includes(kind) ? kind : 1 };
  } catch {
    return null;
  }
}

/** Under a message that is a contact card: who it is, and adding them (after checking the key). */
export function ContactCard({ uri, name, contactKey, own }: { uri: string; name: string; contactKey: string; own: boolean }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const add = useMutation({
    mutationFn: () => api<{ added: boolean; contact: Contact }>("/api/contacts/import", { json: { uri } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["messages"] });
    },
  });
  const done = add.data != null;
  return (
    <div className="mt-1 flex max-w-[85%] items-center gap-3 rounded-xl border border-line bg-surface px-3 py-2 text-sm sm:max-w-[75%]">
      <IdCard className="size-5 shrink-0 text-muted" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{own ? "Your contact card" : name || "MeshCore contact"}</p>
        <p className="font-mono text-xs text-muted">{keyStart(contactKey)} …</p>
      </div>
      {!own &&
        (done ? (
          <span className="text-xs text-ok">{add.data!.added ? "Added" : "Already a contact"}</span>
        ) : (
          <Button onClick={() => setConfirming(true)}>
            <UserPlus className="size-4" aria-hidden /> Add
          </Button>
        ))}
      {confirming && (
        <Dialog
          title={`Add ${name || "this contact"}?`}
          size="sm"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                disabled={add.isPending}
                onClick={() => add.mutate(undefined, { onSuccess: () => setConfirming(false) })}
              >
                Add to contacts
              </Button>
            </>
          }
        >
          <div className="space-y-3 text-sm">
            <p>
              Their key starts <span className="font-mono font-semibold">{keyStart(contactKey)}</span>.
            </p>
            <p className="text-muted">
              Anyone can post a card with any name. Check the start of the key with {name || "them"}, in person or on a
              call, before you trust their signed messages. Adding puts them in your radio's contacts.
            </p>
            <ErrorText error={add.error} />
          </div>
        </Dialog>
      )}
    </div>
  );
}

/** Your own contact for sharing: QR code, link and key. */
export function MyContactDialog({ onClose }: { onClose: () => void }) {
  const me = useMyContact();
  return (
    <Dialog
      title="My contact"
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="space-y-5 text-sm">
        {me.data ? (
          <div className="flex flex-col items-center gap-3 text-center">
            <QrCode value={me.data.uri} label={`QR code for ${me.data.name}`} className="size-48 rounded-lg" />
            <div>
              <p className="font-medium">{me.data.name}</p>
              <p className="break-words font-mono text-xs text-muted">{keyStart(me.data.public_key, 32)}</p>
            </div>
            <p className="text-xs text-muted">
              Others scan this in a MeshCore app, or add your link, to message you and to count your signed messages as a
              contact's. Your key starts <span className="font-mono text-ink">{keyStart(me.data.public_key)}</span>: read it
              out so they can check it.
            </p>
            {canCopy() && (
              <Button onClick={() => void navigator.clipboard?.writeText(me.data!.uri).catch(() => {})}>
                <Copy className="size-4" aria-hidden /> Copy link
              </Button>
            )}
          </div>
        ) : (
          <ErrorText error={me.error} />
        )}

      </div>
    </Dialog>
  );
}

const KIND: Record<number, string> = { 1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor" };

/** Add a contact from their QR code (camera, or a photo on plain-HTTP pages) or their link. */
export function AddContactDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"scan" | "link">("scan");
  const [link, setLink] = useState("");
  const [scanned, setScanned] = useState<ReturnType<typeof contactLink>>(null);
  const [scanError, setScanError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const typed = contactLink(link);
  const candidate = mode === "scan" ? scanned : typed;
  const add = useMutation({
    mutationFn: (uri: string) => api<{ added: boolean; contact: Contact }>("/api/contacts/import", { json: { uri } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["messages"] });
    },
  });
  const reset = () => {
    setScanned(null);
    setScanError("");
    setLink("");
    add.reset();
    setAttempt((n) => n + 1);
  };

  if (add.data) {
    const c = add.data.contact;
    return (
      <Dialog
        title={add.data.added ? "Contact added" : "Already a contact"}
        size="sm"
        onClose={onClose}
        footer={
          <>
            <Button variant="ghost" onClick={reset}>
              Add another
            </Button>
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </>
        }
      >
        <p className="text-sm">
          {add.data.added
            ? `${c.name} is now in your radio's contacts. Their position and route arrive with their next advert.`
            : `${c.name} is already one of your radio's contacts.`}
        </p>
      </Dialog>
    );
  }

  return (
    <Dialog
      title="Add contact"
      onClose={onClose}
      footer={
        candidate ? (
          <>
            <Button variant="ghost" onClick={reset}>
              Back
            </Button>
            <Button variant="primary" onClick={() => add.mutate(candidate.uri)} disabled={add.isPending}>
              <UserPlus className="size-4" aria-hidden /> Add to contacts
            </Button>
          </>
        ) : (
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        )
      }
    >
      {candidate ? (
        <div className="space-y-3 text-sm">
          <div className="rounded-xl border border-line p-3">
            <p className="font-medium">{candidate.name || "Unnamed contact"}</p>
            <p className="text-xs text-muted">{KIND[candidate.kind]}</p>
            <p className="mt-2 break-words font-mono text-xs text-muted">{keyStart(candidate.key, 32)}</p>
          </div>
          <p>
            Their key starts <span className="font-mono font-semibold">{keyStart(candidate.key)}</span>.
          </p>
          <p className="text-muted">
            Anyone can make a code with any name. Check the start of the key with {candidate.name || "them"}, in person or
            on a call, before you trust their signed messages. Adding puts them in your radio's contacts.
          </p>
          <ErrorText error={add.error} />
        </div>
      ) : (
        <div className="space-y-4 text-sm">
          <div role="tablist" aria-label="How to add" className="grid grid-cols-2 gap-1 rounded-xl bg-surface-2 p-1">
            {(
              [
                ["scan", "Scan QR code"],
                ["link", "Paste link"],
              ] as const
            ).map(([m, label]) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => setMode(m)}
                className={cx(
                  "min-h-10 rounded-lg px-3 text-sm font-medium",
                  mode === m ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          {mode === "scan" ? (
            <div className="space-y-2">
              <p className="text-muted">
                Scan the contact QR code from their MeshCore app, MeshOS or MeshHome (Contacts → My contact).
              </p>
              <QrScanner
                key={attempt}
                onResult={(text) => {
                  const c = contactLink(text);
                  if (c) {
                    setScanned(c);
                    setScanError("");
                  } else {
                    setScanError("That QR code isn't a MeshCore contact. Scan the code under their \"Share my contact\".");
                    setAttempt((n) => n + 1);
                  }
                }}
              />
              {scanError && <p className="text-xs text-danger">{scanError}</p>}
            </div>
          ) : (
            <div className="space-y-2">
              <label htmlFor="contact-link" className="block font-medium">
                Their contact link
              </label>
              <Input
                id="contact-link"
                value={link}
                onChange={(e) => setLink(e.target.value)}
                placeholder="meshcore://contact/add?name=…&public_key=…"
                autoComplete="off"
                spellCheck={false}
                autoFocus
              />
              <p className="text-xs text-muted">
                {link.trim() && !typed
                  ? "That isn't a MeshCore contact link. It starts with meshcore://contact/add?"
                  : "The link someone shared from their MeshCore app, MeshOS or MeshHome."}
              </p>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
