import { useCallback, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMatch, useNavigate } from "react-router";
import { Bell, BellOff, CheckCheck, Copy, Eraser, Info, MessageSquare, ShieldCheck, Star, StarOff, Trash2, X } from "lucide-react";
import { soundDefaultFor, soundEnabledFor, type SoundSetting } from "../lib/sound";
import { api, type Conversation, type ConversationInfo } from "../lib/api";
import { canCopy, formatDateTime } from "../lib/util";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Dialog } from "./Dialog";
import { SignedMessagesDialog } from "./SignedMessages";
import { Badge, Button, ErrorText, IconButton } from "./ui";

const CONTACT_KIND: Record<number, string> = { 1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor" };

type Menu = { conv: Conversation; x: number; y: number };

/** Menu actions only change this archive — nothing is transmitted and the radio is not reconfigured. */
function deleteLabel(c: Conversation) {
  return c.kind === "channel" && !c.archived ? "Clear history…" : "Delete conversation…";
}

export function useConversationActions() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const activeId = useMatch("/c/:id")?.params.id;
  const [menu, setMenu] = useState<Menu | null>(null);
  const [infoFor, setInfoFor] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [signingFor, setSigningFor] = useState<Conversation | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["conversations"] });

  // A DM's star is the contact's favourite on the radio, so it can fail (e.g. radio offline).
  const favorite = useMutation({
    mutationFn: (c: Conversation) => api(`/api/conversations/${c.id}`, { method: "PATCH", json: { favorite: !c.favorite } }),
    onSuccess: refresh,
  });
  const markRead = useMutation({
    mutationFn: (c: Conversation) =>
      api(`/api/conversations/${c.id}/read-position`, { method: "PUT", json: { position: c.last_position } }),
    onSuccess: refresh,
  });

  const sound = useMutation({
    mutationFn: ({ c, value }: { c: Conversation; value: "on" | "off" | "default" }) =>
      api(`/api/conversations/${c.id}`, { method: "PATCH", json: { sound: value } }),
    onSuccess: refresh,
  });
  const globalSound = qc.getQueryData<{ sound: SoundSetting }>(["notification-settings"])?.sound ?? "dms";

  const openMenu = useCallback((conv: Conversation, x: number, y: number) => setMenu({ conv, x, y }), []);

  const items = (c: Conversation): MenuItem[] => [
    { label: "Open", icon: <MessageSquare className="size-4" />, onSelect: () => navigate(`/c/${c.id}`) },
    { label: "Info", icon: <Info className="size-4" />, onSelect: () => setInfoFor(c) },
    {
      label: "Mark as read",
      icon: <CheckCheck className="size-4" />,
      disabled: c.unread === 0,
      onSelect: () => markRead.mutate(c),
    },
    {
      label: c.favorite ? "Remove from favorites" : "Add to favorites",
      icon: c.favorite ? <StarOff className="size-4" /> : <Star className="size-4" />,
      onSelect: () => favorite.mutate(c),
    },
    (() => {
      const on = soundEnabledFor(globalSound, c, c.kind);
      // Toggling back to what the global setting would do clears the override instead of pinning it.
      const target = !on;
      const value = target === soundDefaultFor(globalSound, c.kind) ? "default" : target ? "on" : "off";
      return {
        label: on ? "Mute notifications" : "Unmute notifications",
        icon: on ? <BellOff className="size-4" /> : <Bell className="size-4" />,
        onSelect: () => sound.mutate({ c, value }),
      } satisfies MenuItem;
    })(),
    ...(c.kind === "channel" && !c.archived
      ? [{ label: "Signed messages…", icon: <ShieldCheck className="size-4" />, onSelect: () => setSigningFor(c) }]
      : []),
    ...(c.contact_public_key && canCopy()
      ? [
          {
            label: "Copy public key",
            icon: <Copy className="size-4" />,
            onSelect: () => void navigator.clipboard?.writeText(c.contact_public_key!).catch(() => {}),
          },
        ]
      : []),
    "separator",
    {
      label: deleteLabel(c),
      icon: c.kind === "channel" && !c.archived ? <Eraser className="size-4" /> : <Trash2 className="size-4" />,
      danger: true,
      onSelect: () => setDeleting(c),
    },
  ];

  const element = (
    <>
      {favorite.error != null && (
        <div className="fixed bottom-4 left-1/2 z-40 w-[min(28rem,calc(100%-2rem))] -translate-x-1/2" role="alert">
          <div className="flex items-start gap-2 rounded-xl border border-danger/40 bg-surface p-3 text-sm text-danger shadow-xl">
            <span className="flex-1">{(favorite.error as Error).message}</span>
            <IconButton label="Dismiss" className="-m-2 size-9" onClick={() => favorite.reset()}>
              <X className="size-4" />
            </IconButton>
          </div>
        </div>
      )}
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          label={`Actions for ${menu.conv.title}`}
          items={items(menu.conv)}
          onClose={() => setMenu(null)}
        />
      )}
      {infoFor && (
        <InfoDialog
          conv={infoFor}
          onClose={() => setInfoFor(null)}
          onDelete={() => {
            setDeleting(infoFor);
            setInfoFor(null);
          }}
        />
      )}
      {signingFor && <SignedMessagesDialog conv={signingFor} onClose={() => setSigningFor(null)} />}
      {deleting && (
        <DeleteDialog
          conv={deleting}
          onClose={() => setDeleting(null)}
          onDone={(action) => {
            qc.invalidateQueries({ queryKey: ["messages", deleting.id] });
            refresh();
            if (action === "deleted" && activeId === deleting.id) navigate("/", { replace: true });
            setDeleting(null);
          }}
        />
      )}
    </>
  );

  return { openMenu, element };
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-3 py-2 text-sm">
      <dt className="text-muted">{k}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function InfoDialog({ conv, onClose, onDelete }: { conv: Conversation; onClose: () => void; onDelete: () => void }) {
  const info = useQuery({
    queryKey: ["conversation-info", conv.id],
    queryFn: () => api<ConversationInfo>(`/api/conversations/${conv.id}/info`),
  });
  const [copied, setCopied] = useState(false);
  const d = info.data;
  return (
    <Dialog
      title={conv.title}
      onClose={onClose}
      footer={
        <>
          <Button variant="danger" onClick={onDelete}>
            {deleteLabel(conv)}
          </Button>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </>
      }
    >
      <div className="mb-2 flex flex-wrap gap-1.5">
        <Badge tone="accent">{conv.kind === "channel" ? "Channel" : "Direct message"}</Badge>
        {conv.is_simulated && <Badge tone="warn">Simulated</Badge>}
        {conv.archived && <Badge>Archived</Badge>}
        {conv.favorite && <Badge>Favorite</Badge>}
      </div>
      {info.isPending && <p className="py-4 text-sm text-muted">Loading…</p>}
      <ErrorText error={info.error} />
      {d && (
        <dl className="divide-y divide-line">
          {d.channel && (
            <>
              <Row k="Channel">{d.channel.name}</Row>
              <Row k="Radio slot">
                {d.channel.slot}
                {d.channel.generation > 1 && <span className="text-muted"> · generation {d.channel.generation}</span>}
                {!d.channel.active && <span className="text-muted"> · no longer on the radio</span>}
              </Row>
              <Row k="Region scope">
                {d.channel.flood_scope ? `#${d.channel.flood_scope}` : <span className="text-muted">Radio default</span>}
              </Row>
            </>
          )}
          {d.contact && (
            <>
              <Row k="Name">
                {d.contact.alias ? (
                  <>
                    {d.contact.alias} <span className="text-muted">({d.contact.name})</span>
                  </>
                ) : (
                  d.contact.name || "Unnamed"
                )}
              </Row>
              <Row k="Type">{CONTACT_KIND[d.contact.kind] ?? `Type ${d.contact.kind}`}</Row>
              <Row k="Public key">
                <span className="flex items-start gap-1">
                  <code className="min-w-0 flex-1 break-all font-mono text-xs">{d.contact.public_key}</code>
                  {canCopy() && (
                  <IconButton
                    label={copied ? "Copied" : "Copy public key"}
                    className="-my-2 size-9"
                    onClick={() =>
                      navigator.clipboard
                        ?.writeText(d.contact!.public_key)
                        .then(() => {
                          setCopied(true);
                          setTimeout(() => setCopied(false), 1500);
                        })
                        .catch(() => {})
                    }
                  >
                    <Copy className="size-4" />
                  </IconButton>
                  )}
                </span>
              </Row>
              <Row k="Last advert">{formatDateTime(d.contact.last_advert_at)}</Row>
              <Row k="On radio">{d.contact.on_radio ? "Yes" : "No — kept in the archive only"}</Row>
            </>
          )}
          {!d.contact && conv.kind === "dm" && (
            <Row k="Sender">
              Unknown key prefix <code className="font-mono text-xs">{conv.peer_prefix || "—"}</code>
              <span className="block text-xs text-muted">Not matched to exactly one known contact.</span>
            </Row>
          )}
          <Row k="Messages">
            {d.stats.total} <span className="text-muted">({d.stats.incoming} received · {d.stats.outgoing} sent)</span>
          </Row>
          <Row k="Unread">{conv.unread}</Row>
          <Row k="First message">{formatDateTime(d.stats.first_message_at)}</Row>
          <Row k="Last message">{formatDateTime(d.stats.last_message_at)}</Row>
          <Row k="Size limit">{conv.max_bytes} UTF-8 bytes per message</Row>
          <Row k="Via radio">{d.radio_name}</Row>
          <Row k="In archive since">{formatDateTime(d.created_at)}</Row>
        </dl>
      )}
    </Dialog>
  );
}

function DeleteDialog({
  conv,
  onClose,
  onDone,
}: {
  conv: Conversation;
  onClose: () => void;
  onDone: (action: "deleted" | "cleared") => void;
}) {
  const clear = conv.kind === "channel" && !conv.archived;
  const del = useMutation({
    mutationFn: () => api<{ action: "deleted" | "cleared"; messages_removed: number }>(`/api/conversations/${conv.id}`, { method: "DELETE" }),
    onSuccess: (r) => onDone(r.action),
  });
  return (
    <Dialog
      size="sm"
      title={clear ? `Clear history of ${conv.title}?` : `Delete ${conv.title}?`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} autoFocus>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => del.mutate()} disabled={del.isPending}>
            {del.isPending ? "Working…" : clear ? "Clear history" : "Delete"}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p>
          {clear
            ? "All archived messages in this channel will be permanently removed. The channel stays in your list because it is still configured on the radio, and new messages will keep arriving."
            : conv.kind === "dm"
              ? "This conversation and all its archived messages will be permanently removed. If this contact writes again, a new conversation will start."
              : "This retired channel and all its archived messages will be permanently removed."}
        </p>
        <p className="text-muted">
          This only affects this app's archive. Nothing is sent over the mesh, and the radio's contacts and channels are
          not changed. Export the archive first (Settings → Data) if you might want it later.
        </p>
        <ErrorText error={del.error} />
      </div>
    </Dialog>
  );
}
