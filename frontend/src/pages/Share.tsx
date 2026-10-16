import { useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { ChevronLeft, Hash, Share2, UserRound } from "lucide-react";
import { useConversations } from "../components/ConversationList";
import { ErrorText } from "../components/ui";
import { loadDraft, saveDraft } from "../lib/util";

/**
 * Share target of the installed app (Android): text or a link shared from another app arrives here
 * (/share?title=&text=&url=). Pick a conversation; the text becomes its draft, to edit and send.
 */
export function SharePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const conversations = useConversations();
  const shared = useMemo(() => {
    const parts = [params.get("title"), params.get("text"), params.get("url")].map((p) => p?.trim()).filter(Boolean);
    // Apps often repeat the link inside the text; keep it once.
    return [...new Set(parts)].join(" ").slice(0, 1000);
  }, [params]);
  const list = useMemo(
    () =>
      [...(conversations.data ?? [])]
        .filter((c) => !c.archived)
        .sort((a, b) => (b.last_message_at ?? "").localeCompare(a.last_message_at ?? "")),
    [conversations.data],
  );

  const pick = (id: string) => {
    const draft = loadDraft(id);
    saveDraft(id, draft ? `${draft} ${shared}` : shared);
    navigate(`/c/${id}`, { replace: true });
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
        <Link
          to="/"
          className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2 md:hidden"
          aria-label="Back to conversations"
        >
          <ChevronLeft className="size-6" />
        </Link>
        <h2 className="flex-1 px-1 py-2 text-base font-semibold">Share to MeshHome</h2>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-2xl space-y-4">
          {shared ? (
            <div className="rounded-lg border border-line bg-surface p-3">
              <p className="mb-1 flex items-center gap-1.5 text-xs text-muted">
                <Share2 className="size-3.5" aria-hidden /> Shared text
              </p>
              <p className="whitespace-pre-wrap break-words text-sm">{shared}</p>
            </div>
          ) : (
            <p className="text-sm text-muted">Nothing was shared. Share text or a link to MeshHome from another app.</p>
          )}
          {shared && (
            <>
              <p className="text-sm">
                Choose a conversation. The text is added to its message box so you can edit it before sending; MeshCore
                messages are short, so long text may need trimming.
              </p>
              <ErrorText error={conversations.error} />
              <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
                {list.map((c) => {
                  const Icon = c.kind === "channel" ? Hash : UserRound;
                  return (
                    <li key={c.id}>
                      <button
                        type="button"
                        className="flex min-h-12 w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface-2"
                        onClick={() => pick(c.id)}
                      >
                        <Icon className="size-4 shrink-0 text-muted" aria-hidden />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">{c.title}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
