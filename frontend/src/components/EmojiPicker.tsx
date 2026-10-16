import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Search, Smile } from "lucide-react";
import { cx } from "../lib/util";
import { IconButton } from "./ui";

/*
 * Browsers cannot open the operating system's emoji picker from a button, so this is a small
 * in-app picker. Emoji are drawn with the OS's own emoji font, so they look native, and the
 * footer points to the OS picker's keyboard shortcut where there is one.
 */

type Emoji = { e: string; n: string };
type Group = { name: string; icon: string; emojis: Emoji[] };

const COLS = 8;
const RECENT_KEY = "mc-emoji-recent";
const RECENT_MAX = 24;

let groupsPromise: Promise<Group[]> | null = null;
function loadGroups(): Promise<Group[]> {
  groupsPromise ??= import("virtual:emoji-data").then(({ default: raw }) =>
    raw.map(([name, packed]) => {
      const parts = packed.split("\t");
      const emojis: Emoji[] = [];
      for (let i = 0; i + 1 < parts.length; i += 2) emojis.push({ e: parts[i], n: parts[i + 1] });
      return { name, icon: emojis[0]?.e ?? "", emojis };
    }),
  );
  return groupsPromise;
}

function readRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

function rememberRecent(e: string) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify([e, ...readRecent().filter((x) => x !== e)].slice(0, RECENT_MAX)));
  } catch {
    /* storage unavailable */
  }
}

function osShortcut(): string {
  const p = `${navigator.platform} ${navigator.userAgent}`;
  if (/iPhone|iPad|Android/i.test(p)) return "Your keyboard's emoji key works too.";
  if (/Mac/i.test(p)) return "Tip: Control-Command-Space opens the macOS emoji picker.";
  if (/Win/i.test(p)) return "Tip: Windows key + . opens the Windows emoji picker.";
  return "";
}

export function EmojiButton({ onPick, disabled }: { onPick: (emoji: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer, true);
    return () => document.removeEventListener("pointerdown", onPointer, true);
  }, [open]);

  return (
    <div ref={wrap} className="relative shrink-0">
      <IconButton
        label="Insert emoji"
        aria-expanded={open}
        aria-haspopup="dialog"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className={cx(open && "bg-surface-2 text-ink")}
      >
        <Smile className="size-5" />
      </IconButton>
      {open && (
        <EmojiPanel
          onPick={(e) => {
            rememberRecent(e);
            onPick(e);
            setOpen(false);
          }}
          onClose={() => {
            setOpen(false);
            wrap.current?.querySelector("button")?.focus();
          }}
        />
      )}
    </div>
  );
}

function EmojiPanel({ onPick, onClose }: { onPick: (e: string) => void; onClose: () => void }) {
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [recent] = useState(readRecent);
  const grid = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let live = true;
    loadGroups().then(
      (g) => live && setGroups(g),
      () => live && setFailed(true),
    );
    // On touch screens focusing the search would raise the keyboard over the picker.
    if (!matchMedia("(pointer: coarse)").matches) searchRef.current?.focus({ preventScroll: true });
    return () => {
      live = false;
    };
  }, []);

  const q = query.trim().toLowerCase();
  const results = useMemo(() => {
    if (!groups || !q) return null;
    const words = q.split(/\s+/);
    const out: Emoji[] = [];
    for (const g of groups)
      for (const em of g.emojis) if (words.every((w) => em.n.includes(w))) out.push(em);
    return out.slice(0, 160);
  }, [groups, q]);

  const sections = useMemo(() => {
    if (!groups) return [];
    if (results) return [{ name: "Results", icon: "", emojis: results }];
    const byChar = new Map(groups.flatMap((g) => g.emojis.map((em) => [em.e, em] as const)));
    const rec = recent.map((e) => byChar.get(e) ?? { e, n: "" });
    return rec.length ? [{ name: "Recently used", icon: "🕘", emojis: rec }, ...groups] : groups;
  }, [groups, results, recent]);

  // Arrow keys move between emoji; only one emoji is in the tab order.
  const onGridKey = (ev: KeyboardEvent<HTMLDivElement>) => {
    const buttons = Array.from(grid.current?.querySelectorAll<HTMLButtonElement>("button[data-emoji]") ?? []);
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (i < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: COLS, ArrowUp: -COLS } as Record<string, number>;
    if (ev.key in step) {
      ev.preventDefault();
      const next = i + step[ev.key];
      if (next < 0) searchRef.current?.focus();
      else buttons[Math.min(next, buttons.length - 1)]?.focus();
    } else if (ev.key === "Home" || ev.key === "End") {
      ev.preventDefault();
      buttons[ev.key === "Home" ? 0 : buttons.length - 1]?.focus();
    }
  };

  const first = sections[0]?.emojis[0]?.e;
  const hint = osShortcut();

  return (
    <div
      role="dialog"
      aria-label="Emoji"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
      className="mc-pop absolute bottom-full left-0 z-30 mb-2 flex h-[min(24rem,60dvh)] w-[min(20.5rem,calc(100vw-1.5rem))] flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-2xl"
    >
      <div className="relative border-b border-line p-2">
        <Search className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && first) {
              e.preventDefault();
              onPick(first);
            } else if (e.key === "ArrowDown") {
              e.preventDefault();
              grid.current?.querySelector<HTMLButtonElement>("button[data-emoji]")?.focus();
            }
          }}
          placeholder="Search emoji"
          aria-label="Search emoji"
          className="min-h-9 w-full rounded-lg border border-line bg-surface-2 pl-8 pr-2 text-base placeholder:text-muted focus:border-accent focus:outline-none sm:text-sm"
        />
      </div>
      {!results && groups && (
        <nav aria-label="Emoji categories" className="flex justify-between border-b border-line px-1">
          {sections.map((s) => (
            <button
              key={s.name}
              type="button"
              title={s.name}
              aria-label={s.name}
              tabIndex={-1}
              onClick={() =>
                grid.current
                  ?.querySelector(`[data-section="${CSS.escape(s.name)}"]`)
                  ?.scrollIntoView({ block: "start" })
              }
              className="emoji-glyph flex size-8 items-center justify-center rounded-md text-lg opacity-70 hover:bg-surface-2 hover:opacity-100"
            >
              {s.icon}
            </button>
          ))}
        </nav>
      )}
      <div ref={grid} onKeyDown={onGridKey} className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {failed && <p className="p-3 text-sm text-danger">Could not load emoji.</p>}
        {!groups && !failed && <p className="p-3 text-sm text-muted">Loading…</p>}
        {results?.length === 0 && <p className="p-3 text-sm text-muted">No emoji match “{query}”.</p>}
        {sections.map((s) => (
          <section key={s.name} data-section={s.name} aria-label={s.name}>
            <h3 className="sticky top-0 z-10 bg-surface pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {s.name}
            </h3>
            <div className="grid grid-cols-8">
              {s.emojis.map((em) => (
                <button
                  key={em.e}
                  type="button"
                  data-emoji
                  tabIndex={em.e === first ? 0 : -1}
                  title={em.n}
                  aria-label={em.n || em.e}
                  onClick={() => onPick(em.e)}
                  className="emoji-glyph flex aspect-square items-center justify-center rounded-md text-[1.375rem] leading-none hover:bg-surface-2 focus-visible:bg-accent-soft focus-visible:outline-none"
                >
                  {em.e}
                </button>
              ))}
            </div>
          </section>
        ))}
      </div>
      {hint && <p className="border-t border-line px-3 py-1.5 text-[11px] text-muted">{hint}</p>}
    </div>
  );
}
