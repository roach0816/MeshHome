/**
 * "Install as an app" (Settings): a web app manifest and a service worker (public/sw.js), so
 * browsers offer to install MeshHome and an installed app shows its own offline page.
 *
 * Only when the owner turned it on and this page is on HTTPS (browsers require a secure page for
 * both). Otherwise any earlier manifest link, service worker and caches are removed again.
 */

import { api } from "./api";

const MANIFEST_ID = "meshhome-manifest";

export function httpsPage(): boolean {
  return location.protocol === "https:" && window.isSecureContext;
}

/** Opened from the home screen or as an installed app, rather than in a browser tab. */
export function runningInstalled(): boolean {
  return (
    matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

export async function syncAppInstall(enabled: boolean): Promise<void> {
  const on = enabled && httpsPage() && "serviceWorker" in navigator;
  const link = document.getElementById(MANIFEST_ID);
  if (on) {
    if (!link) {
      const el = document.createElement("link");
      el.id = MANIFEST_ID;
      el.rel = "manifest";
      el.href = "/manifest.webmanifest";
      document.head.appendChild(el);
    }
    try {
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    } catch {
      // A browser that refuses (private mode, policy) just stays a plain web page.
    }
    return;
  }
  link?.remove();
  if ("serviceWorker" in navigator) {
    for (const reg of await navigator.serviceWorker.getRegistrations().catch(() => [])) await reg.unregister();
  }
  if ("caches" in window) {
    for (const name of await caches.keys().catch(() => [] as string[])) {
      if (name.startsWith("meshhome-")) await caches.delete(name);
    }
  }
}

// ---- notifications on this device (web push; needs "Install as an app") ------------------------

export interface WebPushStatus {
  available: boolean;
  public_key: string | null;
  subscription: {
    endpoint: string;
    dms: boolean;
    channels: boolean;
    last_sent_at: string | null;
    last_error: string | null;
  } | null;
  devices: number;
}

export interface PushPrefs {
  dms: boolean;
  channels: boolean;
}

/** iPhone or iPad: web push only works from the home-screen app there (iOS 16.4 and later). */
export function isIOS(): boolean {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.userAgent.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(b64url: string): Uint8Array<ArrayBuffer> {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64url.length % 4)) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

/** Ask for permission, subscribe with this server's key, and register the subscription. */
export async function enablePush(publicKey: string, prefs: PushPrefs): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "Notifications are blocked for MeshHome. Allow them in this browser's or phone's settings, then try again."
        : "Notifications weren't allowed.",
    );
  }
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  // A subscription made with another key (e.g. before a restore) can't be used: start again.
  const key = keyBytes(publicKey);
  const current = sub?.options.applicationServerKey;
  if (sub && current && !sameBytes(new Uint8Array(current), key)) {
    await sub.unsubscribe();
    sub = null;
  }
  try {
    sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  } catch (e) {
    throw new Error(await subscribeProblem(e));
  }
  await api("/api/web-push/subscription", { method: "PUT", json: { ...sub.toJSON(), ...prefs } });
}

/** Brave hides itself in the user agent but says so here. */
async function isBrave(): Promise<boolean> {
  const brave = (navigator as Navigator & { brave?: { isBrave?: () => Promise<boolean> } }).brave;
  return !!brave?.isBrave && (await brave.isBrave().catch(() => false));
}

/** What to do when the browser's own push service refused (before MeshHome is involved). */
async function subscribeProblem(e: unknown): Promise<string> {
  const msg = e instanceof Error ? e.message : String(e);
  if (await isBrave()) {
    return "Brave turns web push off by default. Open brave://settings/privacy, turn on “Use Google services for push messaging”, restart Brave, then try again.";
  }
  if (/push service/i.test(msg)) {
    return `This browser couldn't reach its push service (${msg}). A VPN, firewall or privacy extension may be blocking it; try again, or another network or browser.`;
  }
  return msg;
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

export async function disablePush(): Promise<void> {
  await api("/api/web-push/subscription", { method: "DELETE" });
  await (await currentSubscription())?.unsubscribe();
}

/**
 * Browsers sometimes replace a push subscription on their own. When this one has notifications on
 * but the browser's subscription differs from the server's, register the new one again.
 */
export async function resyncPush(status: WebPushStatus): Promise<void> {
  if (!status.available || !status.subscription || !status.public_key || !pushSupported()) return;
  if (Notification.permission !== "granted") return;
  const sub = await currentSubscription();
  if (sub && sub.endpoint === status.subscription.endpoint) return;
  await enablePush(status.public_key, { dms: status.subscription.dms, channels: status.subscription.channels });
}

/** The unread count on the installed app's icon (where the browser supports app badges). */
export function setAppBadge(unread: number): void {
  const nav = navigator as Navigator & { setAppBadge?: (n: number) => Promise<void>; clearAppBadge?: () => Promise<void> };
  if (!nav.setAppBadge || !runningInstalled()) return;
  (unread > 0 ? nav.setAppBadge(unread) : nav.clearAppBadge!()).catch(() => {});
}

/**
 * Bring the icon badge and the notification list in line with what's unread: the badge shows the
 * total, and notifications of conversations with nothing unread are closed (they were read here
 * or in another browser).
 */
export function syncUnread(conversations: { id: string; unread: number }[]): void {
  setAppBadge(conversations.reduce((n, c) => n + c.unread, 0));
  if (!("serviceWorker" in navigator)) return;
  const read = new Set(conversations.filter((c) => c.unread === 0).map((c) => `conv-${c.id}`));
  navigator.serviceWorker
    .getRegistration("/")
    .then((reg) => reg?.getNotifications())
    .then((list) => list?.forEach((n) => read.has(n.tag) && n.close()))
    .catch(() => {});
}
