import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Ban, ChevronLeft, ChevronRight, Copy, Info, MessageSquare, Route, Trash2, UserRound } from "lucide-react";
import { api, type Conversation, type Message, type MessageInfo, type MessagePath } from "../lib/api";
import { canCopy, formatDateTime, stateLabel } from "../lib/util";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Dialog } from "./Dialog";
import { NodeDetails } from "./NodeDetails";
import { Badge, Button, ErrorText } from "./ui";

type View = "details" | "sender" | "paths" | "block" | "delete";
const KIND: Record<number, string> = { 1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor" };

/** Right-click / long-press menu for one message, and the dialogs it opens. */
export function useMessageActions(conv: Conversation) {
  const [menu, setMenu] = useState<{ m: Message; x: number; y: number } | null>(null);
  const [open, setOpen] = useState<{ m: Message; view: View } | null>(null);
  const [copied, setCopied] = useState(false);

  const items = (m: Message): MenuItem[] => {
    const incoming = m.direction === "in";
    const show = (view: View) => () => setOpen({ m, view });
    return [
      { label: "Message details", icon: <Info className="size-4" />, onSelect: show("details") },
      ...(incoming
        ? ([
            { label: "Sender info", icon: <UserRound className="size-4" />, onSelect: show("sender") },
            { label: "View message paths", icon: <Route className="size-4" />, onSelect: show("paths") },
          ] satisfies MenuItem[])
        : []),
      ...(canCopy()
        ? ([
            {
              label: "Copy text",
              icon: <Copy className="size-4" />,
              onSelect: () =>
                navigator.clipboard
                  .writeText(m.body)
                  .then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  })
                  .catch(() => {}),
            },
          ] satisfies MenuItem[])
        : []),
      "separator",
      ...(incoming ? ([{ label: "Block sender…", icon: <Ban className="size-4" />, onSelect: show("block") }] satisfies MenuItem[]) : []),
      {
        label: "Delete…",
        icon: <Trash2 className="size-4" />,
        danger: true,
        disabled: m.direction === "out" && (m.state === "queued" || m.state === "sending"),
        onSelect: show("delete"),
      },
    ];
  };

  const element = (
    <>
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} label="Message actions" items={items(menu.m)} onClose={() => setMenu(null)} />
      )}
      {open && <MessageDialog m={open.m} conv={conv} view={open.view} setView={(view) => setOpen({ m: open.m, view })} onClose={() => setOpen(null)} />}
      {copied && (
        <div className="fixed bottom-4 left-1/2 z-40 -translate-x-1/2 rounded-full bg-ink px-4 py-2 text-sm text-bg shadow-lg" role="status">
          Copied
        </div>
      )}
    </>
  );
  return { openMenu: (m: Message, x: number, y: number) => setMenu({ m, x, y }), element };
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-muted">{k}</dt>
      <dd className="min-w-0 break-words">{children ?? "—"}</dd>
    </div>
  );
}

const TITLES: Record<View, string> = {
  details: "Message details",
  sender: "Sender",
  paths: "Message paths",
  block: "Block sender",
  delete: "Delete message",
};

function MessageDialog({
  m,
  conv,
  view,
  setView,
  onClose,
}: {
  m: Message;
  conv: Conversation;
  view: View;
  setView: (v: View) => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  // A hop picked in the paths view: who that repeater is.
  const [hop, setHop] = useState<string | null>(null);
  const info = useQuery({ queryKey: ["message-info", m.id], queryFn: () => api<MessageInfo>(`/api/messages/${m.id}/info`) });
  const del = useMutation({
    mutationFn: () => api(`/api/messages/${m.id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["messages", conv.id] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
    },
  });
  const contact = info.data?.sender.contact ?? null;
  const block = useMutation({
    mutationFn: () => api(`/api/contacts/${contact!.id}`, { method: "PATCH", json: { blocked: !contact!.blocked } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["messages"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["contacts"] });
      onClose();
    },
  });

  const footer =
    view === "delete" ? (
      <>
        <Button variant="ghost" onClick={onClose} autoFocus>
          Cancel
        </Button>
        <Button variant="danger" onClick={() => del.mutate()} disabled={del.isPending}>
          Delete
        </Button>
      </>
    ) : view === "block" && contact ? (
      <>
        <Button variant="ghost" onClick={onClose} autoFocus>
          Cancel
        </Button>
        <Button variant={contact.blocked ? "primary" : "danger"} onClick={() => block.mutate()} disabled={block.isPending}>
          {contact.blocked ? "Unblock" : "Block"}
        </Button>
      </>
    ) : (
      <Button variant="primary" onClick={onClose}>
        Done
      </Button>
    );

  return (
    <Dialog title={view === "paths" && hop ? "Path hop" : TITLES[view]} onClose={onClose} footer={footer}>
      {view !== "delete" && <ErrorText error={info.error} />}
      {view === "details" && <Details m={m} conv={conv} info={info.data} setView={setView} />}
      {view === "sender" && info.data && <Sender info={info.data} />}
      {view === "paths" && info.data && !hop && <Paths paths={info.data.paths} onHop={setHop} />}
      {view === "paths" && hop && (
        <div className="space-y-3">
          <button className="inline-flex items-center gap-1 text-sm text-accent underline" onClick={() => setHop(null)}>
            <ChevronLeft className="size-4" aria-hidden /> Back to paths
          </button>
          <NodeDetails prefix={hop} onNavigate={onClose} />
        </div>
      )}
      {view === "block" && info.data && <BlockText info={info.data} />}
      {view === "delete" && (
        <p className="text-sm">
          Delete this message from MeshHome's archive? It is not removed from anyone's radio, and others still have their
          copies.
        </p>
      )}
      {(view === "sender" || view === "paths" || view === "block") && info.isPending && <p className="text-sm text-muted">Loading…</p>}
      <ErrorText error={del.error || block.error} />
    </Dialog>
  );
}

function Details({ m, conv, info, setView }: { m: Message; conv: Conversation; info?: MessageInfo; setView: (v: View) => void }) {
  const r = info?.received;
  const incoming = m.direction === "in";
  const hops = !r ? undefined : r.route === "direct" ? "Direct route" : r.hops === null ? undefined : r.hops === 0 ? "0 (heard directly)" : String(r.hops);
  return (
    <div className="space-y-3">
      <dl className="divide-y divide-line">
        {incoming ? (
          <>
            <Row k="Sent">{m.sender_timestamp ? `${formatDateTime(m.sender_timestamp)} (sender's clock)` : undefined}</Row>
            <Row k="Received">{formatDateTime(m.created_at)}</Row>
            <Row k="Hops">{hops}</Row>
            <Row k="Path hash size">{r?.path_hash_size ? `${r.path_hash_size} byte${r.path_hash_size > 1 ? "s" : ""}` : undefined}</Row>
            <Row k="SNR">{r?.snr != null ? `${r.snr} dB` : undefined}</Row>
            <Row k="RSSI">{r?.rssi != null ? `${r.rssi} dBm` : undefined}</Row>
            <Row k="Heard">{info ? `${info.paths.length || 1} time${(info.paths.length || 1) > 1 ? "s" : ""}` : undefined}</Row>
          </>
        ) : (
          <>
            <Row k="Sent">{formatDateTime(m.created_at)}</Row>
            <Row k="Status">{stateLabel(m.state, conv.kind)}</Row>
            {m.error && <Row k="Error">{m.error}</Row>}
          </>
        )}
      </dl>
      {incoming && info && info.paths.length > 0 && (
        <button className="inline-flex items-center gap-1 text-sm text-accent underline" onClick={() => setView("paths")}>
          View {info.paths.length} path{info.paths.length > 1 ? "s" : ""} <ChevronRight className="size-4" aria-hidden />
        </button>
      )}
    </div>
  );
}

function Sender({ info }: { info: MessageInfo }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const c = info.sender.contact;
  const message = useMutation({
    mutationFn: () => api<{ conversation_id: string }>(`/api/contacts/${c!.id}/conversation`, { method: "POST" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      navigate(`/c/${r.conversation_id}`);
    },
  });
  if (!c) {
    return (
      <div className="space-y-2 text-sm">
        <p>
          <strong>{info.sender.label ?? "Unknown sender"}</strong>
        </p>
        <p className="text-muted">
          {info.conversation_kind === "channel"
            ? "No contact on your radio uses this name (or several do). Channel senders are identified only by the name they choose, which isn't verified."
            : "This sender is not one of your radio's contacts."}
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <dl className="divide-y divide-line">
        <Row k="Name">
          {c.alias ?? c.name}
          {c.alias && <span className="text-muted"> ({c.name})</span>}{" "}
          {c.favorite && <Badge tone="warn">Favourite</Badge>} {c.blocked && <Badge tone="danger">Blocked</Badge>}
        </Row>
        <Row k="Type">{KIND[c.kind] ?? `Type ${c.kind}`}</Row>
        <Row k="Last heard">{c.last_advert_at ? formatDateTime(c.last_advert_at) : undefined}</Row>
        <Row k="Public key">
          <code className="break-all font-mono text-xs">{c.public_key}</code>
        </Row>
      </dl>
      {info.sender.match === "name" && (
        <p className="text-xs text-muted">Matched by name: channel senders choose their own names, which aren't verified.</p>
      )}
      {c.kind === 1 && info.conversation_kind === "channel" && (
        <Button onClick={() => message.mutate()} disabled={message.isPending}>
          <MessageSquare className="size-4" aria-hidden /> Send a direct message
        </Button>
      )}
      <ErrorText error={message.error} />
    </div>
  );
}

function hopLabel(h: MessagePath["hops"][number]) {
  if (h.names.length === 1) return h.names[0];
  if (h.names.length > 1) return `${h.names[0]} or ${h.names.length - 1} other${h.names.length > 2 ? "s" : ""}`;
  return "Unknown repeater";
}

function Paths({ paths, onHop }: { paths: MessagePath[]; onHop: (hash: string) => void }) {
  if (paths.length === 0) {
    return (
      <p className="text-sm text-muted">
        No paths were recorded for this message. Paths are captured as the radio hears a message, so they are missing for messages
        received while MeshHome wasn't connected (or before this version).
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        The routes this message took to reach your radio, one for each copy it heard. Repeaters are named from your contacts by the
        start of their key, so a name can occasionally be ambiguous. Select a repeater for its details.
      </p>
      <ol className="space-y-2">
        {paths.map((p, i) => (
          <li key={i} className="rounded-lg border border-line p-3">
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span className="font-medium text-ink">Path {i + 1}</span>
              <span>{p.route === "direct" ? "Direct route" : p.hops.length === 0 ? "Heard directly (no repeaters)" : `${p.hops.length} hop${p.hops.length > 1 ? "s" : ""}`}</span>
              {p.snr != null && <span>SNR {p.snr} dB</span>}
              {p.rssi != null && <span>RSSI {p.rssi} dBm</span>}
            </div>
            <ol className="flex flex-wrap items-center gap-1 text-sm">
              <li className="text-muted">Sender</li>
              {p.hops.map((h, j) => (
                <li key={j} className="flex items-center gap-1">
                  <ChevronRight className="size-3.5 text-muted" aria-hidden />
                  <button
                    type="button"
                    className="rounded-md bg-surface-2 px-2 py-0.5 text-left hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-accent"
                    title={h.names.join(", ") || undefined}
                    aria-label={`Details for ${hopLabel(h)} (${h.hash})`}
                    onClick={() => onHop(h.hash)}
                  >
                    {hopLabel(h)} <span className="font-mono text-[11px] text-muted">{h.hash}</span>
                  </button>
                </li>
              ))}
              <li className="flex items-center gap-1">
                <ChevronRight className="size-3.5 text-muted" aria-hidden />
                <span className="text-muted">Your radio</span>
              </li>
            </ol>
          </li>
        ))}
      </ol>
    </div>
  );
}

function BlockText({ info }: { info: MessageInfo }) {
  const c = info.sender.contact;
  if (!c) {
    return (
      <p className="text-sm">
        <strong>{info.sender.label ?? "This sender"}</strong> isn't one of your radio's contacts, so it can't be blocked. MeshCore
        radios can't block traffic; MeshHome blocks contacts, hiding their direct messages and channel messages under their
        name.
      </p>
    );
  }
  return c.blocked ? (
    <p className="text-sm">
      Unblock <strong>{c.alias ?? c.name}</strong>? Their hidden messages appear again.
    </p>
  ) : (
    <p className="text-sm">
      Block <strong>{c.alias ?? c.name}</strong>? Their direct messages, and channel messages under their name, are still archived
      but hidden, never unread, and silent. You can unblock them in Contacts.
    </p>
  );
}
