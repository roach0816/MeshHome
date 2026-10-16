import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useMatch } from "react-router";
import { BellOff, Hash, MoreHorizontal, Plus, Search, Star, UserRound, X } from "lucide-react";
import { api, type Conversation, type SearchHit } from "../lib/api";
import { cx, formatListTime } from "../lib/util";
import { useConversationActions } from "./ConversationActions";
import { useContextTrigger } from "./useContextTrigger";
import { AddChannelDialog } from "./AddChannel";
import { IconButton } from "./ui";

type Filter = "all" | "unread" | "favorites";

export function useConversations() {
  return useQuery({ queryKey: ["conversations"], queryFn: () => api<Conversation[]>("/api/conversations") });
}

function Avatar({ c }: { c: Conversation }) {
  const Icon = c.kind === "channel" ? Hash : UserRound;
  return (
    <span
      aria-hidden
      className={cx(
        "flex size-10 shrink-0 items-center justify-center rounded-full",
        c.kind === "channel" ? "bg-accent-soft text-accent" : "bg-surface-2 text-muted",
      )}
    >
      <Icon className="size-5" />
    </span>
  );
}

function previewText(c: Conversation): string {
  if (!c.preview) return c.kind === "channel" ? "No messages yet" : "No messages yet";
  const p = c.preview;
  const who = p.direction === "out" ? "You: " : c.kind === "channel" && p.sender_label ? `${p.sender_label}: ` : "";
  return who + p.body.replace(/\s+/g, " ");
}

export function ConversationList() {
  const { data, isPending, error } = useConversations();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const match = useMatch("/c/:id");
  const activeId = match?.params.id;
  const actions = useConversationActions();
  const trigger = useContextTrigger();
  const [adding, setAdding] = useState(false);

  const q = query.trim().toLowerCase();
  const list = useMemo(() => {
    let items = data ?? [];
    if (filter === "unread") items = items.filter((c) => c.unread > 0);
    if (filter === "favorites") items = items.filter((c) => c.favorite);
    if (q) items = items.filter((c) => c.title.toLowerCase().includes(q));
    // Favorites first, then most recent activity (server order).
    return [...items].sort((a, b) => Number(b.favorite) - Number(a.favorite));
  }, [data, filter, q]);

  const search = useQuery({
    queryKey: ["search", q],
    queryFn: () => api<SearchHit[]>(`/api/search?q=${encodeURIComponent(q)}&limit=30`),
    enabled: q.length >= 2,
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-2 px-3 pb-2 pt-3">
        <div className="flex items-center gap-1">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search conversations and messages"
            aria-label="Search conversations and messages"
            className="min-h-10 w-full rounded-lg border border-line bg-surface-2 pl-9 pr-9 text-base placeholder:text-muted focus:border-accent focus:outline-none sm:text-sm"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              aria-label="Clear search"
              className="absolute right-1 top-1/2 inline-flex size-8 -translate-y-1/2 items-center justify-center rounded-md text-muted hover:text-ink"
            >
              <X className="size-4" />
            </button>
          )}
        </div>
        <IconButton label="Add channel" onClick={() => setAdding(true)} className="size-10">
          <Plus className="size-5" />
        </IconButton>
        </div>
        {adding && <AddChannelDialog onClose={() => setAdding(false)} />}
        <div className="flex gap-1.5" role="tablist" aria-label="Filter conversations">
          {(["all", "unread", "favorites"] as Filter[]).map((f) => (
            <button
              key={f}
              role="tab"
              aria-selected={filter === f}
              onClick={() => setFilter(f)}
              className={cx(
                "min-h-8 rounded-full px-3 text-xs font-medium capitalize transition-colors",
                filter === f ? "bg-ink text-bg" : "bg-surface-2 text-muted hover:text-ink",
              )}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {actions.element}
      <div className="relative min-h-0 flex-1 overflow-y-auto">
        {isPending && <p className="px-4 py-6 text-sm text-muted">Loading conversations…</p>}
        {error && <p className="px-4 py-6 text-sm text-danger">Could not load conversations.</p>}
        {data && list.length === 0 && !q && (
          <div className="px-5 py-10 text-center text-sm text-muted">
            {filter === "all" ? (
              <>
                No conversations yet. Channels appear once a radio connects; direct messages appear when someone
                writes, or start one from <Link to="/contacts" className="text-accent underline">Contacts</Link>.
              </>
            ) : (
              `No ${filter} conversations.`
            )}
          </div>
        )}
        <ul>
          {list.map((c) => (
            <li key={c.id} className="group relative">
              <Link
                to={`/c/${c.id}`}
                aria-current={activeId === c.id ? "page" : undefined}
                aria-haspopup="menu"
{...trigger((x, y) => actions.openMenu(c, x, y))}
                className={cx(
                  "flex select-none items-center gap-3 px-3 py-2.5 transition-colors [-webkit-touch-callout:none]",
                  activeId === c.id ? "bg-accent-soft/60" : "hover:bg-surface-2",
                )}
              >
                <Avatar c={c} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline gap-2">
                    <span className={cx("truncate text-sm", c.unread ? "font-semibold" : "font-medium")}>{c.title}</span>
                    {c.favorite && <Star className="size-3 shrink-0 fill-current text-warn" aria-label="Favorite" />}
                    {c.sound === "off" && <BellOff className="size-3 shrink-0 text-muted" aria-label="Muted" />}
                    <span className="ml-auto shrink-0 text-xs text-muted">{formatListTime(c.last_message_at)}</span>
                  </span>
                  <span className="mt-0.5 flex items-center gap-2">
                    <span className={cx("truncate text-xs", c.unread ? "text-ink" : "text-muted")}>{previewText(c)}</span>
                    <span className="ml-auto flex shrink-0 items-center gap-1">
                      {c.is_simulated && <span className="text-[10px] uppercase tracking-wide text-warn">sim</span>}
                      {c.archived && <span className="text-[10px] uppercase tracking-wide text-muted">archived</span>}
                      {c.unread > 0 && (
                        <span
                          className="min-w-5 rounded-full bg-accent px-1.5 text-center text-[11px] font-semibold leading-5 text-accent-fg"
                          aria-label={`${c.unread} unread`}
                        >
                          {c.unread > 99 ? "99+" : c.unread}
                        </span>
                      )}
                    </span>
                  </span>
                </span>
              </Link>
              <button
                type="button"
                aria-label={`More actions for ${c.title}`}
                aria-haspopup="menu"
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  actions.openMenu(c, r.left, r.bottom + 4);
                }}
                className="absolute right-2 top-1/2 hidden size-8 -translate-y-1/2 items-center justify-center rounded-md border border-line bg-surface text-muted shadow-sm hover:text-ink focus-visible:flex group-hover:flex [@media(hover:none)]:!hidden"
              >
                <MoreHorizontal className="size-4" />
              </button>
            </li>
          ))}
        </ul>

        {q.length >= 2 && (
          <section aria-label="Message search results" className="border-t border-line">
            <h2 className="px-4 pb-1 pt-3 text-xs font-semibold uppercase tracking-wide text-muted">Messages</h2>
            {search.isPending && <p className="px-4 py-2 text-sm text-muted">Searching…</p>}
            {search.data?.length === 0 && <p className="px-4 py-2 text-sm text-muted">No messages match.</p>}
            <ul>
              {search.data?.map((m) => (
                <li key={m.id}>
                  <Link to={`/c/${m.conversation_id}`} className="block px-4 py-2 hover:bg-surface-2">
                    <span className="flex justify-between gap-2 text-xs text-muted">
                      <span className="truncate font-medium text-ink">{m.conversation_title}</span>
                      <span className="shrink-0">{formatListTime(m.created_at)}</span>
                    </span>
                    <span className="line-clamp-2 text-sm">
                      {m.direction === "out" ? "You: " : m.sender_label ? `${m.sender_label}: ` : ""}
                      {m.body}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
