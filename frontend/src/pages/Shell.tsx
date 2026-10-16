import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, Map as MapIcon, MessagesSquare, Settings as SettingsIcon, Users } from "lucide-react";
import { Link, NavLink, Route, Routes, useLocation, useNavigate } from "react-router";
import { api, type AppInstall, type Me, type Status } from "../lib/api";
import { useSetupStatus, useUpdateInfo } from "../lib/queries";
import type { SoundSetting } from "../lib/sound";
import { httpsPage, resyncPush, syncAppInstall, syncUnread, type WebPushStatus } from "../lib/pwa";
import { useRealtime } from "../lib/realtime";
import { cx } from "../lib/util";
import { ConversationList, useConversations } from "../components/ConversationList";
import { StatusPill } from "../components/StatusPill";
import { Thread } from "./Thread";
import { Contacts } from "./Contacts";
import { SharePage } from "./Share";
import { Settings, SettingsLayout } from "./Settings";

// Leaflet is only downloaded when the map is opened.
const NodeMap = lazy(() => import("./NodeMap").then((m) => ({ default: m.NodeMap })));
import { IconButton } from "../components/ui";
import { PageErrorBoundary } from "../components/PageErrorBoundary";
import { AccountDialog } from "./Account";
import { NodeSettings } from "./NodeSettings";
import { RemoteManage } from "./RemoteManage";
import { Updates } from "./Updates";

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: () => api<Status>("/api/status"), refetchInterval: 15000 });
}

function NavItem({ to, label, icon: Icon }: { to: string; label: string; icon: typeof Users }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      aria-label={label}
      title={label}
      className={({ isActive }) =>
        cx(
          "inline-flex size-11 items-center justify-center rounded-lg transition-colors",
          isActive ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-2 hover:text-ink",
        )
      }
    >
      <Icon className="size-5" aria-hidden />
    </NavLink>
  );
}

function EmptyPane() {
  return (
    <div className="hidden h-full flex-col items-center justify-center gap-2 p-8 text-center md:flex">
      <MessagesSquare className="size-10 text-muted" aria-hidden />
      <p className="text-sm text-muted">Select a conversation</p>
    </div>
  );
}

export function Shell({ me }: { me: Me }) {
  const socket = useRealtime(true);
  useQuery({
    queryKey: ["notification-settings"],
    queryFn: () => api<{ sound: SoundSetting }>("/api/settings/notifications"),
    staleTime: Infinity,
  });
  const appInstall = useQuery({
    queryKey: ["app-install"],
    queryFn: () => api<AppInstall>("/api/settings/app-install"),
    staleTime: Infinity,
  });
  const installEnabled = appInstall.data?.enabled;
  useEffect(() => {
    if (installEnabled !== undefined) void syncAppInstall(installEnabled);
  }, [installEnabled]);
  // Notifications on this device: re-register if the browser replaced its push subscription.
  const webPush = useQuery({
    queryKey: ["web-push"],
    queryFn: () => api<WebPushStatus>("/api/web-push"),
    enabled: !!installEnabled && httpsPage(),
    staleTime: Infinity,
  });
  useEffect(() => {
    if (webPush.data) resyncPush(webPush.data).catch(() => {});
  }, [webPush.data]);
  // The installed app's icon shows the unread count, and notifications of conversations that are
  // now read are cleared. Applied on every refresh, not just when the count changes: a
  // notification's service worker may have set the badge meanwhile (a message read at once in
  // the open conversation never changes the count this page sees).
  const conversations = useConversations();
  useEffect(() => {
    if (!installEnabled || !conversations.data) return;
    syncUnread(conversations.data);
  }, [installEnabled, conversations.data, conversations.dataUpdatedAt]);
  // A tapped notification, when the service worker couldn't open the conversation itself.
  const navigate = useNavigate();
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (e: MessageEvent) => {
      const url = e.data?.type === "open" && typeof e.data.url === "string" ? new URL(e.data.url, location.origin) : null;
      if (url && url.origin === location.origin) navigate(url.pathname);
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, [navigate]);
  const status = useStatus();
  const { pathname } = useLocation();
  const atRoot = pathname === "/";
  // Animate the sidebar back in on phones only when returning from a page, not on first load.
  const prevPath = useRef(pathname);
  const returning = atRoot && prevPath.current !== "/";
  useEffect(() => {
    prevPath.current = pathname;
  }, [pathname]);

  return (
    <div className="app-height flex overflow-hidden bg-bg">
      <aside
        className={cx(
          "flex h-full w-full flex-col border-line bg-surface md:w-80 md:border-r lg:w-96",
          !atRoot && "hidden md:flex",
          returning && "mc-back-enter",
        )}
        aria-label="Conversations"
      >
        <header className="flex items-center gap-2 border-b border-line px-3 py-2">
          <div className="min-w-0 flex-1 pl-1">
            <h1 className="truncate text-base font-semibold">{me.home_name}</h1>
            <StatusPill status={status.data} socket={socket} />
          </div>
          <nav className="flex items-center gap-1" aria-label="Main">
            <NavItem to="/" label="Chats" icon={MessagesSquare} />
            <NavItem to="/contacts" label="Contacts" icon={Users} />
            <NavItem to="/map" label="Map" icon={MapIcon} />
            <NavItem to="/settings" label="Settings" icon={SettingsIcon} />
          </nav>
        </header>
        <ConversationList />
        <UserPanel me={me} />
      </aside>
      <main className={cx("h-full min-w-0 flex-1 overflow-hidden", atRoot && "hidden md:block")}>
        {/* Keyed by path so each page plays a short entrance animation. */}
        {/* `relative` keeps absolutely positioned descendants (e.g. sr-only labels) inside the
            page instead of stretching the document and adding a second scrollbar. */}
        <div key={pathname} className="mc-page-enter relative h-full">
        <PageErrorBoundary>
        <Routes>
          <Route index element={<EmptyPane />} />
          <Route path="c/:id" element={<Thread status={status.data} />} />
          <Route path="contacts" element={<Contacts />} />
          <Route path="share" element={<SharePage />} />
          <Route path="contacts/:id/manage" element={<RemoteManage />} />
          <Route path="settings" element={<Settings />} />
          <Route
            path="settings/node"
            element={
              <SettingsLayout active="radio">
                <NodeSettings />
              </SettingsLayout>
            }
          />
          <Route
            path="settings/updates"
            element={
              <SettingsLayout active="software">
                <Updates />
              </SettingsLayout>
            }
          />
          <Route path="settings/:category" element={<Settings />} />
          <Route path="account" element={<EmptyPane />} />
          <Route
            path="map"
            element={
              <Suspense fallback={<p className="p-6 text-sm text-muted">Loading map…</p>}>
                <NodeMap />
              </Suspense>
            }
          />
          <Route path="*" element={<EmptyPane />} />
        </Routes>
        </PageErrorBoundary>
        </div>
      </main>
    </div>
  );
}

function UserPanel({ me }: { me: Me }) {
  const qc = useQueryClient();
  const meta = useSetupStatus();
  const update = useUpdateInfo();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [accountOpen, setAccountOpen] = useState(false);
  // Old links and bookmarks to /account open the dialog.
  useEffect(() => {
    if (pathname === "/account") {
      setAccountOpen(true);
      navigate("/", { replace: true });
    }
  }, [pathname, navigate]);
  const logout = useMutation({
    mutationFn: () => api("/api/auth/logout", { method: "POST" }),
    onSettled: () => {
      qc.clear();
      location.assign("/login");
    },
  });
  const version = meta.data?.version;
  const active = accountOpen;
  return (
    <footer className="safe-bottom flex items-center gap-2 border-t border-line px-2 pt-2">
      <div
        className={cx(
          "flex min-w-0 flex-1 items-center gap-3 rounded-lg px-1.5 py-1 transition-colors",
          active ? "bg-accent-soft/60" : "hover:bg-surface-2",
        )}
      >
        {/* Decorative duplicate of the username button, kept out of the tab order. */}
        <button type="button" onClick={() => setAccountOpen(true)} tabIndex={-1} aria-hidden className="shrink-0">
          <span className="flex size-9 items-center justify-center rounded-full bg-accent-soft text-sm font-semibold uppercase text-accent">
            {me.username.slice(0, 1)}
          </span>
        </button>
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => setAccountOpen(true)}
            aria-haspopup="dialog"
            title="Account settings"
            className="block max-w-full truncate text-left text-sm font-medium hover:underline"
          >
            {me.username}
          </button>
          {version &&
            (update.data?.update_available && update.data.latest ? (
              <Link
                to="/settings/updates"
                title={`Version ${update.data.latest.version} is available`}
                className="flex items-center gap-1.5 text-xs font-medium text-accent hover:underline"
              >
                <span className="relative flex size-2" aria-hidden>
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-60 motion-reduce:hidden" />
                  <span className="relative inline-flex size-2 rounded-full bg-accent" />
                </span>
                v{version} · Update to v{update.data.latest.version}
              </Link>
            ) : (
              <Link
                to="/settings/updates"
                title="Software updates and release notes"
                className="text-xs text-muted underline-offset-2 hover:text-accent hover:underline"
              >
                MeshHome v{version}
              </Link>
            ))}
        </div>
      </div>
      <IconButton label="Sign out" onClick={() => logout.mutate()} disabled={logout.isPending}>
        <LogOut className="size-5" />
      </IconButton>
      {accountOpen && <AccountDialog me={me} onClose={() => setAccountOpen(false)} />}
    </footer>
  );
}
