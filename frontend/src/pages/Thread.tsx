import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { ArrowDown, Bot, ChevronLeft, Copy, Hash, Info, RotateCw, SendHorizontal, ShieldCheck, Star, UserRound, X } from "lucide-react";
import { api, ApiError, type Conversation, type Message, type MessagePage, type Status } from "../lib/api";
import {
  cx,
  formatDateTime,
  formatDayHeading,
  formatTime,
  loadDraft,
  newClientId,
  sameDay,
  saveDraft,
  stateLabel,
  stateTone,
  utf8Bytes,
  canCopy,
} from "../lib/util";
import { useConversations } from "../components/ConversationList";
import { Badge, IconButton } from "../components/ui";
import { useMessageActions } from "../components/MessageActions";
import { useContextTrigger } from "../components/useContextTrigger";
import { EmojiButton } from "../components/EmojiPicker";
import {
  ContactCard,
  contactLink,
  SignatureDetails,
  SignatureTick,
  SignedMessagesDialog,
} from "../components/SignedMessages";

const PAGE = 50;
// How long the "New" line stays once you are watching the thread (window focused, at the bottom).
const NEW_LINE_SEEN_MS = 4000;
const BOTTOM_SLACK = 80;

export function Thread({ status }: { status?: Status }) {
  const { id = "" } = useParams();
  const { data: conversations } = useConversations();
  const conv = conversations?.find((c) => c.id === id);
  if (conversations && !conv) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-sm text-muted">
        Conversation not found.
        <Link to="/" className="text-accent underline">
          Back to chats
        </Link>
      </div>
    );
  }
  if (!conv) return <div className="p-6 text-sm text-muted">Loading…</div>;
  // Key by id so per-conversation state (scroll, unread boundary, draft) resets cleanly.
  return <ThreadView key={conv.id} conv={conv} status={status} />;
}

function ThreadView({ conv, status }: { conv: Conversation; status?: Status }) {
  const qc = useQueryClient();
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const restore = useRef<{ height: number; top: number } | null>(null);
  const didInitialScroll = useRef(false);
  const [showJump, setShowJump] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [signing, setSigning] = useState(false);
  const messageActions = useMessageActions(conv);
  // The "New" line sits above the first message that was unread when the thread opened (or that
  // arrived while you weren't looking). It doesn't move as messages are marked read; it goes away
  // once you've been at the bottom of the thread, in a focused window, for a few seconds.
  const [boundary, setBoundary] = useState(conv.read_position);

  const messages = useInfiniteQuery({
    queryKey: ["messages", conv.id],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      api<MessagePage>(`/api/conversations/${conv.id}/messages?limit=${PAGE}${pageParam ? `&before=${pageParam}` : ""}`),
    getNextPageParam: (last) => (last.has_more && last.messages.length ? last.messages[0].position : null),
  });

  const items: Message[] = useMemo(
    () => (messages.data ? [...messages.data.pages].reverse().flatMap((p) => p.messages) : []),
    [messages.data],
  );
  const lastPosition = items.length ? items[items.length - 1].position : 0;
  const firstUnreadCandidate = items.find((m) => m.direction === "in" && m.position > boundary);
  // A divider above the very first message of the whole history adds nothing.
  const firstUnread =
    firstUnreadCandidate && !(firstUnreadCandidate.id === items[0]?.id && !messages.hasNextPage)
      ? firstUnreadCandidate
      : undefined;

  const markRead = useMutation({
    mutationFn: (position: number) =>
      api(`/api/conversations/${conv.id}/read-position`, { method: "PUT", json: { position } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["conversations"] }),
  });

  const maybeMarkRead = useCallback(() => {
    if (!lastPosition || document.visibilityState !== "visible" || !atBottom.current) return;
    if (lastPosition > conv.read_position && !markRead.isPending) markRead.mutate(lastPosition);
  }, [lastPosition, conv.read_position, markRead]);

  // Watching the thread: a focused, visible window, scrolled to the bottom.
  const watching = () => document.visibilityState === "visible" && document.hasFocus() && atBottom.current;
  const seenTimer = useRef<number | undefined>(undefined);
  const settleNewLine = useCallback(() => {
    window.clearTimeout(seenTimer.current);
    if (!lastPosition || lastPosition <= boundary || !watching()) return;
    seenTimer.current = window.setTimeout(() => {
      if (watching()) setBoundary(lastPosition);
    }, NEW_LINE_SEEN_MS);
  }, [lastPosition, boundary]);
  // A message arriving while you're watching gets no "New" line at all.
  const prevLast = useRef(lastPosition);
  useEffect(() => {
    const before = prevLast.current;
    prevLast.current = lastPosition;
    if (before && lastPosition > before && boundary >= before && watching()) setBoundary(lastPosition);
    else settleNewLine();
  }, [lastPosition, boundary, settleNewLine]);
  useEffect(() => {
    window.addEventListener("focus", settleNewLine);
    window.addEventListener("blur", settleNewLine);
    document.addEventListener("visibilitychange", settleNewLine);
    return () => {
      window.removeEventListener("focus", settleNewLine);
      window.removeEventListener("blur", settleNewLine);
      document.removeEventListener("visibilitychange", settleNewLine);
      window.clearTimeout(seenTimer.current);
    };
  }, [settleNewLine]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const wasAtBottom = atBottom.current;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_SLACK;
    if (atBottom.current !== wasAtBottom) settleNewLine();
    if (atBottom.current) {
      setShowJump(false);
      maybeMarkRead();
    }
    if (el.scrollTop < 120 && messages.hasNextPage && !messages.isFetchingNextPage) loadOlder();
  };

  const loadOlder = () => {
    const el = scrollRef.current;
    if (el) restore.current = { height: el.scrollHeight, top: el.scrollTop };
    messages.fetchNextPage();
  };

  const scrollToBottom = (smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    atBottom.current = true;
    setShowJump(false);
  };

  // Keep the reader's place: after prepending older history, offset by the added height;
  // after new messages, follow only if they were already at the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !items.length) return;
    if (restore.current) {
      el.scrollTop = el.scrollHeight - restore.current.height + restore.current.top;
      restore.current = null;
      return;
    }
    if (!didInitialScroll.current) {
      didInitialScroll.current = true;
      const marker = el.querySelector<HTMLElement>("[data-unread-marker]");
      if (marker && marker.offsetTop > el.clientHeight / 2) {
        el.scrollTop = marker.offsetTop - 80;
        atBottom.current = false;
        setShowJump(true);
      } else scrollToBottom();
      return;
    }
    if (atBottom.current) scrollToBottom();
    else setShowJump(true);
  }, [items]);

  useEffect(() => {
    maybeMarkRead();
    const onVis = () => maybeMarkRead();
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [maybeMarkRead]);

  const favorite = useMutation({
    mutationFn: () => api(`/api/conversations/${conv.id}`, { method: "PATCH", json: { favorite: !conv.favorite } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["conversations"] }),
  });

  const online = status?.radio.state === "connected";
  const Icon = conv.kind === "channel" ? Hash : UserRound;

  return (
    <div className="flex h-full">
      <section className="relative flex h-full min-w-0 flex-1 flex-col" aria-label={`Conversation: ${conv.title}`}>
        <header className="pwa-top flex items-center gap-1 border-b border-line bg-surface px-1.5 py-1.5 md:px-3">
          <Link
            to="/"
            className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2 md:hidden"
            aria-label="Back to conversations"
          >
            <ChevronLeft className="size-6" />
          </Link>
          <Icon className="ml-1 hidden size-5 shrink-0 text-muted md:block" aria-hidden />
          <div className="min-w-0 flex-1 px-1">
            <h2 className="truncate text-base font-semibold">{conv.title}</h2>
            <p className="flex items-center gap-1.5 truncate text-xs text-muted">
              {conv.kind === "channel" ? `Channel · slot ${conv.channel_slot}` : "Direct message"}
              {conv.is_simulated && <Badge tone="warn">Simulated</Badge>}
              {conv.archived && <Badge>Archived</Badge>}
            </p>
          </div>
          <IconButton
            label={conv.favorite ? "Remove from favorites" : "Add to favorites"}
            aria-pressed={conv.favorite}
            onClick={() => favorite.mutate()}
          >
            <Star className={cx("size-5", conv.favorite && "fill-current text-warn")} />
          </IconButton>
          {conv.kind === "channel" && !conv.archived && (
            <IconButton
              label={conv.sign_messages ? "Signed messages (signing on)" : "Signed messages"}
              onClick={() => setSigning(true)}
            >
              <ShieldCheck className={cx("size-5", conv.sign_messages && "text-accent")} />
            </IconButton>
          )}
          <IconButton label="Conversation details" aria-pressed={showDetails} onClick={() => setShowDetails((v) => !v)}>
            <Info className="size-5" />
          </IconButton>
        </header>

        {favorite.error != null && (
          <div className="flex items-center gap-2 border-b border-line bg-danger/5 px-4 py-2 text-xs text-danger" role="alert">
            <span className="flex-1">{(favorite.error as Error).message}</span>
            <button className="underline" onClick={() => favorite.reset()}>
              Dismiss
            </button>
          </div>
        )}
        {!online && (
          <div className="border-b border-line bg-warn/10 px-4 py-2 text-xs text-ink" role="status">
            Radio is not connected — history is available, sending is disabled.{" "}
            <Link to="/settings/radio" className="text-accent underline">
              Radio settings
            </Link>
          </div>
        )}

        <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-3 py-4 md:px-6">
          <div className="mx-auto max-w-3xl">
            {messages.hasNextPage && (
              <div className="mb-3 text-center">
                <button
                  onClick={loadOlder}
                  disabled={messages.isFetchingNextPage}
                  className="min-h-9 rounded-full border border-line bg-surface px-4 text-xs text-muted hover:text-ink"
                >
                  {messages.isFetchingNextPage ? "Loading…" : "Load earlier messages"}
                </button>
              </div>
            )}
            {messages.isPending && <p className="py-8 text-center text-sm text-muted">Loading messages…</p>}
            {messages.data && items.length === 0 && (
              <p className="py-12 text-center text-sm text-muted">
                No messages yet.{" "}
                {conv.kind === "channel" ? "Messages heard on this channel will appear here." : "Say hello."}
              </p>
            )}
            {/* One section per day, each with its own sticky date: the next day's date pushes the
                previous one away instead of stacking on top of it. */}
            <ol>
              {days(items).map((day) => (
                <li key={day.key} aria-label={day.label}>
                  <div className="sticky top-0 z-10 flex justify-center py-2" aria-hidden>
                    <span className="rounded-full border border-line bg-surface px-3 py-0.5 text-[11px] font-medium text-muted shadow-sm">
                      {day.label}
                    </span>
                  </div>
                  <ol className="space-y-1">
                    {day.indexes.map((i) => {
                      const m = items[i];
                      const prev = i > day.indexes[0] ? items[i - 1] : undefined;
                      const grouped =
                        !!prev &&
                        prev.direction === m.direction &&
                        prev.sender_label === m.sender_label &&
                        new Date(m.created_at).getTime() - new Date(prev.created_at).getTime() < 5 * 60_000;
                      return (
                        <Fragment key={m.id}>
                          {firstUnread?.id === m.id && (
                            <li data-unread-marker className="flex items-center gap-3 py-2" aria-label="New messages">
                              <span className="h-px flex-1 bg-accent/50" />
                              <span className="text-[11px] font-semibold uppercase tracking-wide text-accent">New</span>
                              <span className="h-px flex-1 bg-accent/50" />
                            </li>
                          )}
                          <Bubble
                            m={m}
                            conv={conv}
                            grouped={grouped}
                            online={online}
                            onMenu={(x, y) => messageActions.openMenu(m, x, y)}
                          />
                        </Fragment>
                      );
                    })}
                  </ol>
                </li>
              ))}
            </ol>
          </div>
        </div>

        {showJump && (
          <button
            onClick={() => scrollToBottom(true)}
            className="absolute bottom-24 left-1/2 inline-flex min-h-9 -translate-x-1/2 items-center gap-1.5 rounded-full bg-accent px-4 text-xs font-medium text-accent-fg shadow-lg"
          >
            <ArrowDown className="size-4" aria-hidden /> New messages
          </button>
        )}

        <Composer conv={conv} online={online} onSent={() => scrollToBottom()} />
      </section>
      {messageActions.element}
      {showDetails && <Details conv={conv} onClose={() => setShowDetails(false)} />}
      {signing && <SignedMessagesDialog conv={conv} onClose={() => setSigning(false)} />}
    </div>
  );
}

/** Messages split into calendar days (local time), as positions in the list. */
function days(items: Message[]) {
  const out: { key: string; label: string; indexes: number[] }[] = [];
  items.forEach((m, i) => {
    const prev = items[i - 1];
    if (!prev || !sameDay(new Date(prev.created_at), new Date(m.created_at))) {
      out.push({ key: m.id, label: formatDayHeading(m.created_at), indexes: [] });
    }
    out[out.length - 1].indexes.push(i);
  });
  return out;
}

function Bubble({
  m,
  conv,
  grouped,
  online,
  onMenu,
}: {
  m: Message;
  conv: Conversation;
  grouped: boolean;
  online: boolean;
  onMenu: (x: number, y: number) => void;
}) {
  const qc = useQueryClient();
  // Right-click, long-press or the keyboard menu key: message actions.
  const trigger = useContextTrigger()(onMenu);
  const [open, setOpen] = useState(false);
  const out = m.direction === "out";
  const retryable = out && ["failed", "uncertain", "no_ack", "expired"].includes(m.state);
  const retry = useMutation({
    mutationFn: (confirm: boolean) =>
      api(`/api/messages/${m.id}/retry`, { json: { confirm_possible_duplicate: confirm } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["messages", conv.id] }),
  });
  const onRetry = () => {
    const risky = m.state === "uncertain" || m.state === "no_ack";
    if (risky && !confirm("This message may already have been delivered. Send it again? It could arrive twice.")) return;
    retry.mutate(risky);
  };
  const tone = stateTone(m.state);
  const card = conv.kind === "channel" ? contactLink(m.body) : null;

  return (
    <li className={cx("flex flex-col", out ? "items-end" : "items-start", !grouped && "pt-2")}>
      {!out && conv.kind === "channel" && !grouped && (
        <span className="mb-0.5 px-3 text-xs font-medium text-muted">{m.sender_label ?? "Unknown sender"}</span>
      )}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        {...trigger}
        className={cx(
          "max-w-[85%] rounded-2xl px-3.5 py-2 text-left text-[15px] leading-snug sm:max-w-[75%]",
          out ? "bg-bubble-out text-bubble-out-fg" : "border border-line bg-bubble-in text-ink",
          out ? (grouped ? "rounded-tr-md" : "") : grouped ? "rounded-tl-md" : "",
        )}
      >
        {/* Remote text is rendered as text, never as HTML. */}
        <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{m.body}</span>
        <span className={cx("mt-0.5 flex items-center justify-end gap-1.5 text-[11px]", out ? "opacity-80" : "text-muted")}>
          {m.is_simulated && <span className="uppercase tracking-wide">sim</span>}
          {typeof m.meta?.bot === "string" && (
            <span className="inline-flex items-center gap-0.5" title={`Automatic reply to /${m.meta.bot}`}>
              <Bot className="size-3" aria-hidden /> bot
            </span>
          )}
          {m.duplicate_count > 0 && <span>heard ×{m.duplicate_count + 1}</span>}
          {m.signature && <SignatureTick sig={m.signature} />}
          <time dateTime={m.created_at}>{formatTime(m.created_at)}</time>
        </span>
      </button>
      {card && <ContactCard uri={card.uri} name={card.name} contactKey={card.key} own={out} />}
      {out && m.state !== "received" && (
        <span
          className={cx(
            "mt-0.5 px-2 text-[11px]",
            tone === "ok" && "text-ok",
            tone === "warn" && "text-warn",
            tone === "danger" && "text-danger",
            tone === "muted" && "text-muted",
          )}
        >
          {stateLabel(m.state, conv.kind)}
          {retryable && online && (
            <button onClick={onRetry} disabled={retry.isPending} className="ml-2 inline-flex items-center gap-1 underline">
              <RotateCw className="size-3" aria-hidden /> Retry
            </button>
          )}
        </span>
      )}
      {retry.error && <span className="px-2 text-[11px] text-danger">{(retry.error as Error).message}</span>}
      {open && (
        <dl className="mt-1 grid max-w-[85%] grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded-lg bg-surface-2 px-3 py-2 text-[11px] text-muted sm:max-w-[75%]">
          <dt>{out ? "Requested" : "Received"}</dt>
          <dd>{formatDateTime(m.created_at)}</dd>
          {m.sender_timestamp ? (
            <>
              <dt>Sender clock</dt>
              <dd>{formatDateTime(m.sender_timestamp)}</dd>
            </>
          ) : null}
          {m.sender_key_prefix && (
            <>
              <dt>Sender key</dt>
              <dd className="font-mono">{m.sender_key_prefix}…</dd>
            </>
          )}
          {m.signature && <SignatureDetails sig={m.signature} />}
          {Object.entries(m.meta ?? {})
            .filter(([k]) => k !== "signed" && k !== "unsigned")
            .map(([k, v]) => (
            <Fragment key={k}>
              <dt>{k}</dt>
              <dd>{String(v)}</dd>
            </Fragment>
          ))}
          {m.error && (
            <>
              <dt>Detail</dt>
              <dd className="text-warn">{m.error}</dd>
            </>
          )}
        </dl>
      )}
    </li>
  );
}

function Composer({ conv, online, onSent }: { conv: Conversation; online: boolean; onSent: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState(() => loadDraft(conv.id));
  const clientId = useRef<string>(newClientId());
  const taRef = useRef<HTMLTextAreaElement>(null);
  const bytes = utf8Bytes(text);
  const over = bytes > conv.max_bytes;
  const canReply = conv.kind === "channel" || !!conv.contact_id;

  const send = useMutation({
    mutationFn: (body: string) =>
      api<Message>(`/api/conversations/${conv.id}/messages`, { json: { client_message_id: clientId.current, body } }),
    onSuccess: () => {
      setText("");
      saveDraft(conv.id, "");
      clientId.current = newClientId();
      qc.invalidateQueries({ queryKey: ["messages", conv.id] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onSent();
    },
  });

  useEffect(() => saveDraft(conv.id, text), [conv.id, text]);
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  }, [text]);

  const insertEmoji = (emoji: string) => {
    const ta = taRef.current;
    const start = ta?.selectionStart ?? text.length;
    const end = ta?.selectionEnd ?? text.length;
    setText(text.slice(0, start) + emoji + text.slice(end));
    clientId.current = newClientId();
    requestAnimationFrame(() => {
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(start + emoji.length, start + emoji.length);
    });
  };

  const submit = () => {
    if (!text.trim() || over || !online || send.isPending) return;
    send.mutate(text);
  };

  const disabledReason = !canReply
    ? "This sender isn't a known contact, so replies can't be addressed."
    : !online
      ? "Sending is unavailable while the radio is offline. Your draft is kept."
      : conv.archived
        ? "This conversation is archived on the radio."
        : null;

  return (
    <div className="safe-bottom border-t border-line bg-surface px-3 pt-2 md:px-6">
      <div className="mx-auto max-w-3xl">
        {send.error && (
          <p className="mb-1.5 text-xs text-danger" role="alert">
            {send.error instanceof ApiError ? send.error.message : "Could not send. Your text has been kept."}
          </p>
        )}
        {disabledReason && <p className="mb-1.5 text-xs text-muted">{disabledReason}</p>}
        <div className="flex items-end gap-1.5 sm:gap-2">
          <EmojiButton onPick={insertEmoji} disabled={!canReply} />
          <label htmlFor="composer" className="sr-only">
            Message
          </label>
          <textarea
            id="composer"
            ref={taRef}
            rows={1}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              clientId.current = newClientId();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={conv.kind === "channel" ? `Message ${conv.title}` : "Message"}
            aria-describedby="byte-count"
            className="max-h-40 min-h-11 flex-1 resize-none rounded-xl border border-line bg-surface-2 px-3.5 py-2.5 text-base leading-snug placeholder:text-muted focus:border-accent focus:outline-none"
          />
          <IconButton
            label="Send"
            onClick={submit}
            disabled={!text.trim() || over || !online || !canReply || send.isPending}
            className="bg-bubble-out !text-white hover:bg-bubble-out hover:brightness-110 disabled:bg-surface-2 disabled:!text-muted"
          >
            <SendHorizontal className="size-5" />
          </IconButton>
        </div>
        <p
          id="byte-count"
          className={cx("mt-1 flex justify-between text-[11px]", over ? "text-danger" : "text-muted")}
          aria-live="polite"
        >
          <span className="hidden sm:inline">Enter to send · Shift+Enter for a new line</span>
          <span className="ml-auto">
            {bytes}/{conv.max_bytes} bytes{over ? " — too long, please shorten" : ""}
          </span>
        </p>
      </div>
    </div>
  );
}

function Details({ conv, onClose }: { conv: Conversation; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async (v: string) => {
    try {
      await navigator.clipboard.writeText(v);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (e.g. plain http) */
    }
  };
  return (
    <aside
      className="fixed inset-0 z-30 flex flex-col bg-surface lg:static lg:z-auto lg:w-80 lg:border-l lg:border-line"
      aria-label="Conversation details"
    >
      <header className="pwa-top flex items-center justify-between border-b border-line px-4 py-2">
        <h3 className="text-sm font-semibold">Details</h3>
        <IconButton label="Close details" onClick={onClose}>
          <X className="size-5" />
        </IconButton>
      </header>
      <dl className="space-y-3 p-4 text-sm">
        <div>
          <dt className="text-xs text-muted">Type</dt>
          <dd>{conv.kind === "channel" ? `Channel (radio slot ${conv.channel_slot})` : "Direct message"}</dd>
        </div>
        {conv.contact_public_key && (
          <div>
            <dt className="text-xs text-muted">Public key</dt>
            <dd className="flex items-start gap-2">
              <code className="break-all font-mono text-xs">{conv.contact_public_key}</code>
              {canCopy() && (
                <IconButton label={copied ? "Copied" : "Copy public key"} onClick={() => copy(conv.contact_public_key!)}>
                  <Copy className="size-4" />
                </IconButton>
              )}
            </dd>
          </div>
        )}
        {conv.peer_prefix !== null && !conv.contact_id && (
          <div>
            <dt className="text-xs text-muted">Sender key prefix</dt>
            <dd className="font-mono text-xs">{conv.peer_prefix || "unknown"}</dd>
            <dd className="mt-1 text-xs text-muted">
              Not matched to exactly one known contact, so it is kept separate rather than guessed by name.
            </dd>
          </div>
        )}
        <div>
          <dt className="text-xs text-muted">Message size limit</dt>
          <dd>{conv.max_bytes} UTF-8 bytes</dd>
        </div>
        {conv.kind === "channel" && (
          <p className="text-xs text-muted">
            Channel sender names are labels chosen by each sender, not verified identities. Channel messages have no
            recipient acknowledgement — "Sent by radio" means the radio accepted it for transmission.
          </p>
        )}
        {conv.kind === "dm" && (
          <p className="text-xs text-muted">
            "Delivered" means the recipient's radio confirmed it received the message (an ACK). It is not a
            read receipt.
          </p>
        )}
      </dl>
    </aside>
  );
}
