import type { MessageState } from "./api";

const encoder = new TextEncoder();
export const utf8Bytes = (s: string) => encoder.encode(s).length;

/** Copying to the clipboard: browsers only allow it on HTTPS pages (and localhost). */
export function canCopy(): boolean {
  return window.isSecureContext && !!navigator.clipboard;
}

/** The device's location ("Use my location"): HTTPS pages only, too. */
export function canLocate(): boolean {
  return window.isSecureContext && "geolocation" in navigator;
}

export function newClientId(): string {
  // Not crypto.randomUUID(): browsers only offer it on HTTPS pages, and MeshHome is often opened
  // over plain HTTP on the home network. getRandomValues works everywhere.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function sameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export function formatListTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (sameDay(d, now)) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const diffDays = (now.getTime() - d.getTime()) / 86400000;
  if (diffDays < 6) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function formatDayHeading(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (sameDay(d, now)) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return "Yesterday";
  return d.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: d.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}

export function formatDateTime(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function relativeSeconds(epoch: number | null | undefined, now = Date.now() / 1000): string {
  if (!epoch) return "never";
  const s = Math.max(0, Math.round(now - epoch));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Labels never claim more than the protocol proves (a channel send has no recipient ACK). */
export function stateLabel(state: MessageState, kind: "dm" | "channel"): string {
  switch (state) {
    case "queued":
      return "Queued";
    case "sending":
      return "Sending…";
    case "accepted":
      return kind === "dm" ? "Sent · awaiting delivery" : "Sent by radio";
    case "acknowledged":
      return "Delivered";
    case "no_ack":
      return "Not confirmed delivered";
    case "uncertain":
      return "Outcome uncertain";
    case "failed":
      return "Failed";
    case "expired":
      return "Expired · not sent";
    default:
      return "";
  }
}

/** Automatic retries of a direct message (meta.delivery): the latest attempt, of how many, and its route. */
export type Delivery = { attempt: number; of: number; route: "direct" | "flood" };

export function deliveryOf(meta: Record<string, unknown> | undefined): Delivery | null {
  const d = meta?.delivery as Delivery | undefined;
  return d && typeof d.attempt === "number" && typeof d.of === "number" ? d : null;
}

/** The delivery line under one of your messages, with its retries when there were any. */
export function deliveryLabel(state: MessageState, kind: "dm" | "channel", d: Delivery | null): string {
  if (!d || kind !== "dm") return stateLabel(state, kind);
  const by = d.route === "flood" ? " by flood" : "";
  switch (state) {
    case "queued": // waiting for its next attempt
      return d.attempt < d.of ? `Not confirmed yet · retrying (${d.attempt + 1} of ${d.of})` : stateLabel(state, kind);
    case "sending":
      return d.attempt < d.of ? `Sending attempt ${d.attempt + 1} of ${d.of}…` : stateLabel(state, kind);
    case "accepted":
      return d.attempt > 1 ? `Sent${by} · awaiting delivery (attempt ${d.attempt} of ${d.of})` : stateLabel(state, kind);
    case "acknowledged":
      return d.attempt > 1 ? `Delivered on attempt ${d.attempt}${by}` : stateLabel(state, kind);
    case "no_ack":
      return d.attempt > 1 ? `Not confirmed delivered after ${d.attempt} attempts` : stateLabel(state, kind);
    default:
      return stateLabel(state, kind);
  }
}

export function stateTone(state: MessageState): "muted" | "ok" | "warn" | "danger" {
  if (state === "acknowledged") return "ok";
  if (state === "no_ack" || state === "uncertain" || state === "expired") return "warn";
  if (state === "failed") return "danger";
  return "muted";
}

// ---- this browser's storage -----------------------------------------------------------------

/** Move settings stored before the rename to MeshHome ("mch.*") to their new keys ("mh.*"). */
export function migrateLocalStorage() {
  try {
    const old = Object.keys(localStorage).filter((k) => k.startsWith("mch."));
    for (const k of old) {
      const next = "mh." + k.slice(4);
      const v = localStorage.getItem(k);
      if (v !== null && localStorage.getItem(next) === null) localStorage.setItem(next, v);
      localStorage.removeItem(k);
    }
  } catch {
    /* storage unavailable */
  }
}

// ---- drafts (local to this browser, kept separate from authoritative history) -------------

const DRAFT_PREFIX = "mh.draft.";

export function loadDraft(convId: string): string {
  try {
    return localStorage.getItem(DRAFT_PREFIX + convId) ?? "";
  } catch {
    return "";
  }
}

export function saveDraft(convId: string, text: string) {
  try {
    if (text) localStorage.setItem(DRAFT_PREFIX + convId, text);
    else localStorage.removeItem(DRAFT_PREFIX + convId);
  } catch {
    /* storage unavailable */
  }
}

// ---- theme ------------------------------------------------------------------------------

export type ThemePref = "system" | "light" | "dark";

export function getThemePref(): ThemePref {
  try {
    const v = localStorage.getItem("mh.theme");
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(pref: ThemePref) {
  const dark = pref === "dark" || (pref === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {
    if (pref === "system") localStorage.removeItem("mh.theme");
    else localStorage.setItem("mh.theme", pref);
  } catch {
    /* ignore */
  }
}

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}
