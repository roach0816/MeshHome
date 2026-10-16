// MeshHome service worker: registered only when the owner has turned on "Install as an app"
// (Settings, HTTPS only; see src/lib/pwa.ts).
//
// It keeps the app's own files so an installed app opens quickly, and shows offline.html instead
// of the browser's error page when the server can't be reached. It never stores what the server
// knows: /api, /ws and /health requests pass straight through, and pages always come from the
// network first. Version-independent, so it doesn't change between releases.
//
// With notifications turned on for this device (Settings), it also shows web push notifications
// from your MeshHome server and keeps the app icon's unread badge current.

const SHELL = "meshhome-shell-v1";
const ASSETS = "meshhome-assets-v1";
const KEEP = [SHELL, ASSETS];
const SHELL_FILES = ["/offline.html", "/icon-192.png", "/favicon.svg"];
const MAX_ASSETS = 80; // hashed files from recent releases; older ones are dropped

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((c) => c.addAll(SHELL_FILES))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n.startsWith("meshhome-") && !KEEP.includes(n)).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

async function trim(cache) {
  const keys = await cache.keys();
  for (const req of keys.slice(0, Math.max(0, keys.length - MAX_ASSETS))) await cache.delete(req);
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // map tiles and the like
  if (/^\/(api|ws|health)(\/|$)/.test(url.pathname)) return;

  // Pages: always the server's current version; the offline page when it can't be reached.
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline.html", { cacheName: SHELL })));
    return;
  }

  // The offline page's own files: the network copy when there is one, else the cached one.
  if (SHELL_FILES.includes(url.pathname)) {
    event.respondWith(fetch(req).catch(() => caches.match(url.pathname, { cacheName: SHELL })));
    return;
  }

  // Built files have a content hash in their name, so a cached copy never goes stale.
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.open(ASSETS).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) {
          await cache.put(req, res.clone());
          trim(cache);
        }
        return res;
      }),
    );
  }
});

// ---- notifications (web push from your MeshHome server; app/services/web_push.py) -------------

function describe(n) {
  if (n.k === "test") return { title: n.t || "MeshHome", body: n.b };
  if (n.k === "channel") return { title: n.t, body: n.s ? `${n.s}: ${n.b}` : n.b };
  return { title: n.s || n.t || "Direct message", body: n.b };
}

self.addEventListener("push", (event) => {
  let n = {};
  try {
    n = event.data ? event.data.json() : {};
  } catch {
    n = {};
  }
  const { title, body } = describe(n);
  const show = self.registration.showNotification(title || "MeshHome", {
    body: body || "New message",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: n.c ? `conv-${n.c}` : "meshhome",
    renotify: Boolean(n.c),
    timestamp: Date.now(),
    data: { url: n.c ? `/c/${n.c}` : "/" },
  });
  // The badge: from the count sent with the notification, unless MeshHome is open and visible.
  // Then the page keeps it current (and may already have marked this message read).
  const badge = self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    if (typeof n.n !== "number" || !("setAppBadge" in self.navigator)) return;
    if (windows.some((w) => w.visibilityState === "visible")) return;
    return (n.n > 0 ? self.navigator.setAppBadge(n.n) : self.navigator.clearAppBadge()).catch(() => {});
  });
  event.waitUntil(Promise.all([show, badge]));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (windows) => {
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (open) {
        await open.focus();
        return open.navigate ? open.navigate(url).catch(() => open.postMessage({ type: "open", url })) : undefined;
      }
      return self.clients.openWindow(url);
    }),
  );
});
