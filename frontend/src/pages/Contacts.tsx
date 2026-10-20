import { useEffect, useState, type ReactNode } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import {
  IdCard,
  ArrowDown,
  ArrowUp,
  Ban,
  ChevronLeft,
  ChevronRight,
  Copy,
  Info,
  MessageSquare,
  Route,
  RouteOff,
  Search,
  Share2,
  SlidersHorizontal,
  Star,
  StarOff,
  Trash2,
  X,
} from "lucide-react";
import { api, type Contact, type ContactDetail, type ContactPage } from "../lib/api";
import { Badge, Button, ErrorText, Field, IconButton, Input } from "../components/ui";
import { ContextMenu, type MenuItem } from "../components/ContextMenu";
import { Dialog } from "../components/Dialog";
import { useContextTrigger } from "../components/useContextTrigger";
import { MyContactDialog } from "../components/SignedMessages";
import { canCopy, cx, formatDateTime, sameDay } from "../lib/util";

// MeshCore advert types.
const KIND: Record<number, string> = { 1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor" };
const kindLabel = (k: number) => KIND[k] ?? `Type ${k}`;
const PAGE_SIZES = [10, 25, 50] as const;
type Show = "all" | "favorites" | "blocked" | "removed";
type SortKey = "name" | "kind" | "last_heard";
type Dir = "asc" | "desc";
// The direction a column starts in when first clicked: newest first for "last heard", A→Z otherwise.
const FIRST_DIR: Record<SortKey, Dir> = { name: "asc", kind: "asc", last_heard: "desc" };
const PREFS_KEY = "mh.contacts.sort";

type SortPrefs = { sort: SortKey; dir: Dir; favoritesFirst: boolean };
function loadSortPrefs(): SortPrefs {
  const fallback: SortPrefs = { sort: "last_heard", dir: "desc", favoritesFirst: true };
  try {
    const v = JSON.parse(localStorage.getItem(PREFS_KEY) ?? "null");
    if (v && v.sort in FIRST_DIR && (v.dir === "asc" || v.dir === "desc")) return { ...fallback, ...v, favoritesFirst: v.favoritesFirst !== false };
  } catch {
    // unavailable or unreadable storage: use the defaults
  }
  return fallback;
}
const SHOW_LABEL: Record<Show, string> = {
  all: "All",
  favorites: "Favorites",
  blocked: "Blocked",
  removed: "Removed from radio",
};
// Shorter labels for phones and tablets, where the full ones do not fit on one line.
const SHOW_SHORT: Partial<Record<Show, string>> = { favorites: "Favs", removed: "Removed" };

/** Time if heard today, otherwise the date. */
function lastHeard(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  if (sameDay(d, now)) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  // Short enough for the phone column: "Oct 6" this year, "7/12/25" (locale order) before that.
  // The full date and time are in the tooltip.
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  return d.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" });
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function Contacts() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>("");
  const [show, setShow] = useState<Show>("all");
  const [prefs, setPrefs] = useState<SortPrefs>(loadSortPrefs);
  const { sort, dir, favoritesFirst } = prefs;
  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      // not remembered; fine
    }
  }, [prefs]);
  // Clicking the active column flips its direction; another column starts in its natural one.
  const sortBy = (key: SortKey) =>
    setPrefs((p) => ({ ...p, sort: key, dir: p.sort === key ? (p.dir === "asc" ? "desc" : "asc") : FIRST_DIR[key] }));
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(25);
  const [page, setPage] = useState(1);
  const query = useDebounced(q.trim());

  // Any filter change returns to the first page.
  useEffect(() => setPage(1), [query, kind, show, sort, dir, favoritesFirst, pageSize]);

  const params = new URLSearchParams({
    show,
    sort,
    order: dir,
    favorites_first: String(favoritesFirst && show !== "favorites"),
    page: String(page),
    page_size: String(pageSize),
  });
  if (query) params.set("q", query);
  if (kind) params.set("kind", kind);
  const list = useQuery({
    queryKey: ["contacts", params.toString()],
    queryFn: () => api<ContactPage>(`/api/contacts?${params}`),
    placeholderData: keepPreviousData,
  });
  const data = list.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;
  useEffect(() => {
    if (data && page > pages) setPage(pages);
  }, [data, page, pages]);

  const [showMine, setShowMine] = useState(false);
  const actions = useContactActions();
  const trigger = useContextTrigger();
  const from = data && data.total ? (page - 1) * pageSize + 1 : 0;
  const to = data ? Math.min(page * pageSize, data.total) : 0;

  return (
    <div className="flex h-full flex-col">
      <header className="pwa-top flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
        <Link
          to="/"
          className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2 md:hidden"
          aria-label="Back to conversations"
        >
          <ChevronLeft className="size-6" />
        </Link>
        {/* No refresh button: the list follows the radio's adverts (and re-reads every 30 minutes). */}
        <h2 className="flex-1 px-1 py-2 text-base font-semibold">Contacts</h2>
        <Button onClick={() => setShowMine(true)}>
          <IdCard className="size-4" aria-hidden /> My contact
        </Button>
      </header>
      {showMine && <MyContactDialog onClose={() => setShowMine(false)} />}

      {/* Toolbar: search, filters, sort */}
      <div className="space-y-2 border-b border-line bg-surface px-3 py-2.5 md:px-4">
        <div className="flex flex-wrap gap-2">
          <div className="relative min-w-48 flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search name, alias or key"
              aria-label="Search contacts"
              className="min-h-10 w-full rounded-lg border border-line bg-surface-2 pl-9 pr-9 text-base placeholder:text-muted focus:border-accent focus:outline-none sm:text-sm"
            />
            {q && (
              <button
                onClick={() => setQ("")}
                aria-label="Clear search"
                className="absolute right-1 top-1/2 inline-flex size-8 -translate-y-1/2 items-center justify-center rounded-md text-muted hover:text-ink"
              >
                <X className="size-4" />
              </button>
            )}
          </div>
          <select
            aria-label="Device type"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className="min-h-10 rounded-lg border border-line bg-surface px-2 text-sm"
          >
            <option value="">All types</option>
            {Object.entries(KIND).map(([k, v]) => (
              <option key={k} value={k}>
                {v}s
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto" role="tablist" aria-label="Show contacts">
          {(["all", "favorites", "blocked", "removed"] as Show[]).map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={show === s}
              aria-label={SHOW_LABEL[s]}
              onClick={() => setShow(s)}
              className={cx(
                "min-h-8 shrink-0 rounded-full px-3 text-xs font-medium transition-colors",
                show === s ? "bg-ink text-bg" : "bg-surface-2 text-muted hover:text-ink",
              )}
            >
              {SHOW_SHORT[s] ? (
                <>
                  <span className="lg:hidden">{SHOW_SHORT[s]}</span>
                  <span className="hidden lg:inline">{SHOW_LABEL[s]}</span>
                </>
              ) : (
                SHOW_LABEL[s]
              )}
            </button>
          ))}
        </div>
          <label className="flex min-h-8 shrink-0 cursor-pointer items-center gap-1.5 text-xs text-muted hover:text-ink">
            <input
              type="checkbox"
              checked={favoritesFirst}
              onChange={(e) => setPrefs((p) => ({ ...p, favoritesFirst: e.target.checked }))}
              className="size-4 accent-[var(--accent)]"
            />
            <Star className="size-3.5 fill-current text-warn" aria-hidden />
            <span>
              <span className="sm:hidden">First</span>
              <span className="hidden sm:inline">Favorites first</span>
            </span>
          </label>
        </div>
      </div>

      {/* Table */}
      <div className="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
        <ErrorText error={list.error} />
        <table className="w-full table-fixed border-collapse text-sm">
          <colgroup>
            <col />
            <col className="w-[5.75rem] sm:w-36" />
            <col className="w-[4.5rem] sm:w-32" />
          </colgroup>
          <thead className="sticky top-0 z-10 bg-bg/95 text-left text-xs text-muted backdrop-blur">
            <tr className="border-b border-line">
              <SortHeader label="Name" active={sort === "name"} dir={dir} onClick={() => sortBy("name")} />
              <SortHeader
                label={
                  <>
                    <span className="sm:hidden">Type</span>
                    <span className="hidden sm:inline">Device type</span>
                  </>
                }
                active={sort === "kind"}
                dir={dir}
                onClick={() => sortBy("kind")}
              />
              <SortHeader label="Last heard" active={sort === "last_heard"} dir={dir} onClick={() => sortBy("last_heard")} />
            </tr>
          </thead>
          <tbody className={cx(list.isFetching && list.isPlaceholderData && "opacity-60")}>
            {data?.items.map((c) => {
              const t = trigger((x, y) => actions.openMenu(c, x, y));
              return (
              <tr
                key={c.id}
                tabIndex={0}
                aria-haspopup="menu"
                onClick={() => actions.primary(c, navigate)}
                title={`${c.kind === 1 ? "Open conversation" : c.kind === 2 || c.kind === 3 ? "Remote manage" : "Details"}. Right-click, or press and hold, for more.`}
                {...t}
                onKeyDown={(e) => {
                  if (e.key === "Enter") actions.primary(c, navigate);
                  else t.onKeyDown(e);
                }}
                className="group cursor-pointer select-none border-b border-line [-webkit-touch-callout:none] hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none"
              >
                <td className="overflow-hidden px-3 py-2.5 md:px-4">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className={cx("truncate font-medium", c.blocked && "text-muted line-through")}>
                      {c.alias || c.name || "Unnamed"}
                    </span>
                    {c.favorite && <Star className="size-3.5 shrink-0 fill-current text-warn" aria-label="Favorite" />}
                    {c.blocked && <Badge tone="danger">Blocked</Badge>}
                    {c.is_simulated && (
                      <span className="hidden shrink-0 text-[10px] uppercase tracking-wide text-warn sm:inline">sim</span>
                    )}
                  </span>
                </td>
                <td className="truncate px-2 py-2.5 text-muted">
                  <span className="sm:hidden">{kindLabel(c.kind).replace("Room server", "Room")}</span>
                  <span className="hidden sm:inline">{kindLabel(c.kind)}</span>
                </td>
                <td className="truncate px-2 py-2.5 text-muted">
                  <time dateTime={c.last_advert_at ?? undefined} title={formatDateTime(c.last_advert_at)}>
                    {lastHeard(c.last_advert_at)}
                  </time>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        {list.isPending && <p className="px-4 py-6 text-sm text-muted">Loading contacts…</p>}
        {data && data.total === 0 && (
          <p className="px-4 py-10 text-center text-sm text-muted">
            {query || kind || show !== "all" ? "No contacts match these filters." : "No contacts on the radio yet."}
          </p>
        )}
      </div>

      {/* Pagination */}
      <nav
        aria-label="Contact pages"
        className="safe-bottom flex flex-wrap items-center justify-between gap-2 border-t border-line bg-surface px-3 pt-2 text-sm md:px-4"
      >
        <label className="flex items-center gap-2 text-muted">
          Rows per page
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number])}
            className="min-h-9 rounded-md border border-line bg-surface px-2 text-ink"
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-center gap-1">
          <span className="mr-2 text-muted" aria-live="polite">
            {data ? `${from}–${to} of ${data.total}` : "…"}
          </span>
          <IconButton label="Previous page" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            <ChevronLeft className="size-5" />
          </IconButton>
          <span className="min-w-16 text-center text-muted">
            {page} / {pages}
          </span>
          <IconButton label="Next page" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            <ChevronRight className="size-5" />
          </IconButton>
        </div>
      </nav>
      {actions.element(navigate)}
    </div>
  );
}

function SortHeader({ label, active, onClick, dir }: { label: ReactNode; active: boolean; onClick: () => void; dir: Dir }) {
  const Icon = dir === "desc" ? ArrowDown : ArrowUp;
  return (
    <th scope="col" aria-sort={active ? (dir === "desc" ? "descending" : "ascending") : "none"} className="px-2 py-1 font-medium first:pl-3 md:first:pl-4">
      <button
        onClick={onClick}
        title={active ? "Reverse the order" : "Sort by this column"}
        className={cx("inline-flex min-h-8 items-center gap-1 rounded text-left hover:text-ink", active && "text-ink")}
      >
        {label}
        {active && <Icon className="size-3" aria-hidden />}
      </button>
    </th>
  );
}

// ---- actions --------------------------------------------------------------------------

type DialogKind = "details" | "share" | "path" | "remove" | "block";

function useContactActions() {
  const qc = useQueryClient();
  const [menu, setMenu] = useState<{ c: Contact; x: number; y: number } | null>(null);
  const [dialog, setDialog] = useState<{ kind: DialogKind; c: Contact } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["contacts"] });
    qc.invalidateQueries({ queryKey: ["contact"] });
  };
  const favorite = useMutation({
    mutationFn: (c: Contact) => api(`/api/contacts/${c.id}/favorite`, { json: { favorite: !c.favorite } }),
    onSuccess: invalidate,
    onError: setError,
  });
  const resetPath = useMutation({
    mutationFn: (c: Contact) => api(`/api/contacts/${c.id}/reset-path`, { method: "POST" }),
    onSuccess: invalidate,
    onError: setError,
  });
  const unblock = useMutation({
    mutationFn: (c: Contact) => api(`/api/contacts/${c.id}`, { method: "PATCH", json: { blocked: false } }),
    onSuccess: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
    onError: setError,
  });

  const open = (kind: DialogKind, c: Contact) => {
    setError(null);
    setDialog({ kind, c });
  };

  const message = async (c: Contact, navigate: (to: string) => void) => {
    setError(null);
    try {
      const r = await api<{ conversation_id: string }>(`/api/contacts/${c.id}/conversation`, { method: "POST" });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      navigate(`/c/${r.conversation_id}`);
    } catch (e) {
      setError(e);
    }
  };

  /** Clicking a contact: message a person, manage a repeater or room server, otherwise details. */
  const primary = (c: Contact, navigate: (to: string) => void) => {
    if (c.kind === 1) void message(c, navigate);
    else if (c.kind === 2 || c.kind === 3) navigate(`/contacts/${c.id}/manage`);
    else open("details", c);
  };

  const items = (c: Contact, navigate: (to: string) => void): MenuItem[] => {
    const onRadio = c.on_radio;
    return [
      { label: "Details", icon: <Info className="size-4" />, onSelect: () => open("details", c) },
      ...(c.kind === 1
        ? [
            {
              label: "Send message",
              icon: <MessageSquare className="size-4" />,
              onSelect: () => message(c, navigate),
            } satisfies MenuItem,
          ]
        : []),
      ...(c.kind === 2 || c.kind === 3
        ? [
            {
              label: "Remote manage",
              icon: <SlidersHorizontal className="size-4" />,
              onSelect: () => navigate(`/contacts/${c.id}/manage`),
            } satisfies MenuItem,
          ]
        : []),
      { label: "Share…", icon: <Share2 className="size-4" />, disabled: !onRadio, onSelect: () => open("share", c) },
      { label: "Set path…", icon: <Route className="size-4" />, disabled: !onRadio, onSelect: () => open("path", c) },
      { label: "Reset path", icon: <RouteOff className="size-4" />, disabled: !onRadio, onSelect: () => resetPath.mutate(c) },
      {
        label: c.favorite ? "Remove favorite" : "Favorite",
        icon: c.favorite ? <StarOff className="size-4" /> : <Star className="size-4" />,
        disabled: !onRadio,
        onSelect: () => favorite.mutate(c),
      },
      {
        label: c.blocked ? "Unblock" : "Block…",
        icon: <Ban className="size-4" />,
        onSelect: () => (c.blocked ? unblock.mutate(c) : open("block", c)),
      },
      "separator",
      { label: "Remove contact…", icon: <Trash2 className="size-4" />, danger: true, disabled: !onRadio, onSelect: () => open("remove", c) },
    ];
  };

  const element = (navigate: (to: string) => void) => (
    <>
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          label={`Actions for ${menu.c.alias || menu.c.name}`}
          items={items(menu.c, navigate)}
          onClose={() => setMenu(null)}
        />
      )}
      {error != null && (
        <div className="fixed bottom-4 left-1/2 z-40 w-[min(28rem,calc(100%-2rem))] -translate-x-1/2" role="alert">
          <div className="flex items-start gap-2 rounded-xl border border-danger/40 bg-surface p-3 text-sm text-danger shadow-xl">
            <span className="flex-1">{error instanceof Error ? error.message : String(error)}</span>
            <IconButton label="Dismiss" className="-m-2 size-9" onClick={() => setError(null)}>
              <X className="size-4" />
            </IconButton>
          </div>
        </div>
      )}
      {dialog?.kind === "details" && <DetailsDialog c={dialog.c} onClose={() => setDialog(null)} />}
      {dialog?.kind === "share" && <ShareDialog c={dialog.c} onClose={() => setDialog(null)} />}
      {dialog?.kind === "path" && <PathDialog c={dialog.c} onClose={() => setDialog(null)} />}
      {dialog?.kind === "remove" && <RemoveDialog c={dialog.c} onClose={() => setDialog(null)} />}
      {dialog?.kind === "block" && <BlockDialog c={dialog.c} onClose={() => setDialog(null)} />}
    </>
  );

  return { openMenu: (c: Contact, x: number, y: number) => setMenu({ c, x, y }), open, primary, element };
}

function useContactDetail(id: string) {
  return useQuery({ queryKey: ["contact", id], queryFn: () => api<ContactDetail>(`/api/contacts/${id}`) });
}

function CopyRow({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span className="flex items-start gap-1">
      <code className="min-w-0 flex-1 break-all rounded-md bg-surface-2 px-2 py-1 font-mono text-xs">{value}</code>
      {canCopy() && (
      <IconButton
        label={copied ? "Copied" : label}
        className="size-9"
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
        <Copy className="size-4" />
      </IconButton>
      )}
    </span>
  );
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-3 py-2 text-sm">
      <dt className="text-muted">{k}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function pathText(d: ContactDetail) {
  if (d.path_len < 0) return "Flood (no learned path yet)";
  if (d.path_len === 0) return "Direct (no repeaters)";
  return `${d.path_len} hop${d.path_len === 1 ? "" : "s"}: ${d.path_hops.join(" → ")}`;
}

function DetailsDialog({ c, onClose }: { c: Contact; onClose: () => void }) {
  const qc = useQueryClient();
  const d = useContactDetail(c.id);
  const [alias, setAlias] = useState(c.alias ?? "");
  const save = useMutation({
    mutationFn: () => api(`/api/contacts/${c.id}`, { method: "PATCH", json: { alias } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["contact", c.id] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  const x = d.data;
  return (
    <Dialog title={c.alias || c.name || "Contact"} onClose={onClose} footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
      <div className="mb-2 flex flex-wrap gap-1.5">
        <Badge tone="accent">{kindLabel(c.kind)}</Badge>
        {c.favorite && <Badge>Favorite</Badge>}
        {c.blocked && <Badge tone="danger">Blocked</Badge>}
        {!c.on_radio && <Badge>Removed from radio</Badge>}
        {c.is_simulated && <Badge tone="warn">Simulated</Badge>}
      </div>
      {d.isPending && <p className="py-4 text-sm text-muted">Loading…</p>}
      <ErrorText error={d.error} />
      {x && (
        <dl className="divide-y divide-line">
          <Row k="Advertised name">{x.name || "—"}</Row>
          <Row k="Public key">
            <CopyRow value={x.public_key} label="Copy public key" />
          </Row>
          <Row k="Last heard">{formatDateTime(x.last_advert_at)}</Row>
          <Row k="Location">
            {x.lat !== null && x.lon !== null ? (
              <Link to="/map" className="text-accent underline" onClick={onClose}>
                {x.lat.toFixed(5)}, {x.lon.toFixed(5)}
              </Link>
            ) : (
              "Not shared"
            )}
          </Row>
          <Row k="Route">{pathText(x)}</Row>
          {x.kind === 1 && (
            <Row k="Messages">
              {x.messages_received} received · {x.messages_sent} sent
            </Row>
          )}
        </dl>
      )}
      <form
        className="mt-3 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Local alias" htmlFor="ct-alias" hint="Only shown in this app.">
          <Input id="ct-alias" maxLength={64} value={alias} onChange={(e) => setAlias(e.target.value)} placeholder={c.name} />
        </Field>
        <Button type="submit" disabled={save.isPending || alias === (c.alias ?? "")}>
          Save
        </Button>
      </form>
      <ErrorText error={save.error} />
    </Dialog>
  );
}

function ShareDialog({ c, onClose }: { c: Contact; onClose: () => void }) {
  const card = useQuery({
    queryKey: ["contact-card", c.id],
    queryFn: () => api<{ uri: string }>(`/api/contacts/${c.id}/export`),
    retry: false,
  });
  const share = useMutation({ mutationFn: () => api(`/api/contacts/${c.id}/share`, { method: "POST" }) });
  return (
    <Dialog title={`Share ${c.alias || c.name}`} onClose={onClose} footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
      <div className="space-y-5 text-sm">
        <section className="space-y-2">
          <h3 className="font-medium">Contact link</h3>
          <p className="text-muted">Paste into another MeshCore app (or a QR generator) to add this contact there.</p>
          {card.isPending && <p className="text-muted">Asking the radio…</p>}
          <ErrorText error={card.error} />
          {card.data && <CopyRow value={card.data.uri} label="Copy contact link" />}
        </section>
        <section className="space-y-2">
          <h3 className="font-medium">Share to nearby nodes</h3>
          <p className="text-muted">The radio re-broadcasts this contact's advert once, to nodes in direct range (zero hop).</p>
          <Button onClick={() => share.mutate()} disabled={share.isPending || share.isSuccess}>
            <Share2 className="size-4" aria-hidden /> {share.isSuccess ? "Shared" : "Share over the mesh"}
          </Button>
          <ErrorText error={share.error} />
        </section>
      </div>
    </Dialog>
  );
}

function PathDialog({ c, onClose }: { c: Contact; onClose: () => void }) {
  const qc = useQueryClient();
  const d = useContactDetail(c.id);
  const repeaters = useQuery({
    queryKey: ["contacts", "repeaters-for-path"],
    queryFn: async () => {
      const [r, s] = await Promise.all([
        api<ContactPage>("/api/contacts?kind=2&page_size=50&sort=name"),
        api<ContactPage>("/api/contacts?kind=3&page_size=50&sort=name"),
      ]);
      return [...r.items, ...s.items];
    },
  });
  const [hops, setHops] = useState<string[]>([]);
  const [pick, setPick] = useState("");
  const [manual, setManual] = useState("");
  useEffect(() => {
    if (d.data) setHops(d.data.path_hops);
  }, [d.data]);
  const size = d.data?.path_hash_size ?? 1;
  const save = useMutation({
    mutationFn: () => api(`/api/contacts/${c.id}/path`, { method: "PUT", json: { hops } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contact", c.id] });
      onClose();
    },
  });
  const nameFor = (hop: string) => repeaters.data?.find((r) => r.public_key.startsWith(hop))?.name;
  const addHop = (value: string) => {
    const v = value.trim().toLowerCase();
    if (/^[0-9a-f]+$/.test(v) && v.length >= size * 2) setHops([...hops, v.slice(0, size * 2)]);
  };
  return (
    <Dialog
      title={`Route to ${c.alias || c.name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending || !d.data}>
            {hops.length ? `Save ${hops.length}-hop path` : "Save as direct"}
          </Button>
        </>
      }
    >
      <div className="space-y-4 text-sm">
        <p className="text-muted">
          Messages to this contact go out along this path of repeaters. Leave it empty for a direct (in-range) route, or use{" "}
          <strong>Reset path</strong> from the menu to go back to flooding. Each hop is the first {size} byte{size > 1 ? "s" : ""} of
          a repeater's key.
        </p>
        {d.data && <p>Current: {pathText(d.data)}</p>}
        <ol className="space-y-1.5">
          {hops.map((h, i) => (
            <li key={`${h}-${i}`} className="flex items-center gap-2 rounded-lg border border-line px-3 py-1.5">
              <span className="w-6 text-muted">{i + 1}.</span>
              <code className="font-mono text-xs">{h}</code>
              <span className="min-w-0 flex-1 truncate text-muted">{nameFor(h) ?? ""}</span>
              <IconButton label="Move up" className="size-8" disabled={i === 0} onClick={() => setHops(hops.map((x, j) => (j === i - 1 ? hops[i] : j === i ? hops[i - 1] : x)))}>
                <ArrowUp className="size-4" />
              </IconButton>
              <IconButton label="Remove hop" className="size-8" onClick={() => setHops(hops.filter((_, j) => j !== i))}>
                <X className="size-4" />
              </IconButton>
            </li>
          ))}
          {hops.length === 0 && <li className="rounded-lg border border-dashed border-line px-3 py-2 text-muted">Direct — no repeaters</li>}
        </ol>
        <div className="flex gap-2">
          <select
            aria-label="Repeater to add"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            className="min-h-11 min-w-0 flex-1 rounded-lg border border-line bg-surface px-2"
          >
            <option value="">Add a known repeater…</option>
            {repeaters.data?.map((r) => (
              <option key={r.id} value={r.public_key}>
                {r.alias || r.name} ({r.public_key.slice(0, size * 2)})
              </option>
            ))}
          </select>
          <Button
            disabled={!pick}
            onClick={() => {
              addHop(pick);
              setPick("");
            }}
          >
            Add
          </Button>
        </div>
        <div className="flex gap-2">
          <Input
            aria-label="Hop hash (hex)"
            placeholder={`Or enter a hop hash (${size * 2} hex characters)`}
            className="font-mono"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
          />
          <Button
            disabled={!/^[0-9a-fA-F]+$/.test(manual.trim()) || manual.trim().length < size * 2}
            onClick={() => {
              addHop(manual);
              setManual("");
            }}
          >
            Add
          </Button>
        </div>
        <ErrorText error={save.error ?? d.error} />
      </div>
    </Dialog>
  );
}

function RemoveDialog({ c, onClose }: { c: Contact; onClose: () => void }) {
  const qc = useQueryClient();
  const del = useMutation({
    mutationFn: () => api(`/api/contacts/${c.id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      onClose();
    },
  });
  return (
    <Dialog
      size="sm"
      title={`Remove ${c.alias || c.name}?`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} autoFocus>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => del.mutate()} disabled={del.isPending}>
            Remove from radio
          </Button>
        </>
      }
    >
      <div className="space-y-2 text-sm">
        <p>The contact is deleted from the radio's contact list, freeing a slot. You won't be able to DM them until they're added again (for example when they next advert, if auto-add is on).</p>
        <p className="text-muted">This app keeps the archived conversation; the contact moves to "Removed from radio".</p>
        <ErrorText error={del.error} />
      </div>
    </Dialog>
  );
}

function BlockDialog({ c, onClose }: { c: Contact; onClose: () => void }) {
  const qc = useQueryClient();
  const block = useMutation({
    mutationFn: () => api(`/api/contacts/${c.id}`, { method: "PATCH", json: { blocked: true } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contacts"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["messages"] });
      onClose();
    },
  });
  return (
    <Dialog
      size="sm"
      title={`Block ${c.alias || c.name}?`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} autoFocus>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => block.mutate()} disabled={block.isPending}>
            Block
          </Button>
        </>
      }
    >
      <div className="space-y-2 text-sm">
        <p>
          Their direct messages, and channel messages sent under the name “{c.name}”, are hidden: no unread badges, no
          sounds, and not shown in conversations or search. Their DM conversation is hidden from your list.
        </p>
        <p className="text-muted">
          MeshCore radios can't block traffic, so messages are still received and kept in the archive. Unblocking shows them
          again. Channel messages carry only a self-chosen name, so someone could still post under a different name.
        </p>
        <ErrorText error={block.error} />
      </div>
    </Dialog>
  );
}
