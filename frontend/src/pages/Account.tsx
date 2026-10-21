import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, Monitor, QrCode } from "lucide-react";
import { api, type Me, type SignedInDevice } from "../lib/api";
import { formatDateTime } from "../lib/util";
import { Badge, Button, ErrorText, Field, IconButton, Input } from "../components/ui";
import { Dialog } from "../components/Dialog";
import { MyContactDialog } from "../components/SignedMessages";

function Section({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <section className="border-t border-line pt-4">
      <h3 className="text-sm font-semibold">{title}</h3>
      {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  );
}

/** Account settings, opened from the username at the bottom of the sidebar. */
export function AccountDialog({ me, onClose }: { me: Me; onClose: () => void }) {
  const qc = useQueryClient();
  const [showContact, setShowContact] = useState(false);
  const logout = useMutation({
    mutationFn: () => api("/api/auth/logout", { method: "POST" }),
    onSettled: () => {
      qc.clear();
      location.assign("/login");
    },
  });

  return (
    <Dialog title="Account" onClose={onClose}>
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <span
            aria-hidden
            className="flex size-12 shrink-0 items-center justify-center rounded-full bg-accent-soft text-lg font-semibold uppercase text-accent"
          >
            {me.username.slice(0, 1)}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate font-semibold">{me.username}</p>
            <p className="truncate text-sm text-muted">Owner of {me.home_name}</p>
          </div>
          <IconButton label="My contact: QR code, name and key" onClick={() => setShowContact(true)}>
            <QrCode className="size-5" />
          </IconButton>
          <Button variant="ghost" onClick={() => logout.mutate()} disabled={logout.isPending}>
            <LogOut className="size-4" aria-hidden /> Sign out
          </Button>
        </div>
        <UsernameSection me={me} />
        <PasswordSection />
        <DevicesSection />
      </div>
      {showContact && <MyContactDialog onClose={() => setShowContact(false)} />}
    </Dialog>
  );
}

function UsernameSection({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [username, setUsername] = useState(me.username);
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const valid = /^[A-Za-z0-9_.@-]+$/.test(username) && username !== me.username;
  const save = useMutation({
    mutationFn: () => api<Me>("/api/auth/username", { method: "PUT", json: { username, current_password: password } }),
    onSuccess: (next) => {
      qc.setQueryData(["me"], next);
      setPassword("");
      setDone(true);
    },
  });
  return (
    <Section title="Username" description="Used to sign in. Your current password is required to change it.">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setDone(false);
          if (valid && password) save.mutate();
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="New username"
            htmlFor="acct-username"
            error={username && !/^[A-Za-z0-9_.@-]+$/.test(username) ? "Letters, numbers and . _ @ - only" : null}
          >
            <Input
              id="acct-username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={64}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </Field>
          <Field label="Current password" htmlFor="acct-username-pw">
            <Input
              id="acct-username-pw"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
        </div>
        <ErrorText error={save.error} />
        {done && (
          <p className="text-sm text-ok" role="status">
            Username changed. Use it the next time you sign in.
          </p>
        )}
        <Button type="submit" variant="primary" disabled={!valid || !password || save.isPending}>
          Change username
        </Button>
      </form>
    </Section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const mismatch = confirm.length > 0 && confirm !== next;
  const change = useMutation({
    mutationFn: () => api("/api/auth/password", { json: { current_password: current, new_password: next } }),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    },
  });
  return (
    <Section title="Password" description="At least 10 characters. Changing it signs out every other browser.">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setDone(false);
          change.mutate();
        }}
      >
        <Field label="Current password" htmlFor="acct-cur-pw">
          <Input
            id="acct-cur-pw"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="New password"
            htmlFor="acct-new-pw"
            error={next.length > 0 && next.length < 10 ? "Use at least 10 characters" : null}
          >
            <Input
              id="acct-new-pw"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          </Field>
          <Field label="Confirm new password" htmlFor="acct-new-pw2" error={mismatch ? "Passwords do not match" : null}>
            <Input
              id="acct-new-pw2"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </Field>
        </div>
        <ErrorText error={change.error} />
        {done && (
          <p className="text-sm text-ok" role="status">
            Password changed. Other browsers have been signed out.
          </p>
        )}
        <Button
          type="submit"
          variant="primary"
          disabled={!current || next.length < 10 || next !== confirm || change.isPending}
        >
          Change password
        </Button>
      </form>
    </Section>
  );
}

/** "Safari on iPhone" from a browser's user agent; good enough to recognise your own devices. */
function describeBrowser(ua: string | null): string {
  if (!ua) return "Browser";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\/|FxiOS/.test(ua)
      ? "Firefox"
      : /Chrome\/|CriOS/.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : "Browser";
  const os = /iPhone/.test(ua)
    ? "iPhone"
    : /iPad/.test(ua)
      ? "iPad"
      : /Android/.test(ua)
        ? "Android"
        : /CrOS/.test(ua)
          ? "ChromeOS"
          : /Mac OS X/.test(ua)
            ? "macOS"
            : /Windows/.test(ua)
              ? "Windows"
              : /Linux/.test(ua)
                ? "Linux"
                : null;
  return os ? `${browser} on ${os}` : browser;
}

function DevicesSection() {
  const qc = useQueryClient();
  const devices = useQuery({
    queryKey: ["signed-in-devices"],
    queryFn: () => api<SignedInDevice[]>("/api/auth/sessions"),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["signed-in-devices"] });
  const signOut = useMutation({
    mutationFn: (id: string) => api<void>(`/api/auth/sessions/${id}`, { method: "DELETE" }),
    onSettled: () => void refresh(),
  });
  const signOutOthers = useMutation({
    mutationFn: () => api<void>("/api/auth/sessions", { method: "DELETE" }),
    onSettled: () => void refresh(),
  });
  const others = devices.data?.filter((d) => !d.current).length ?? 0;
  return (
    <Section
      title="Signed-in devices"
      description="Browsers signed in to your account. Sign out any you don't recognise or no longer use."
    >
      <ErrorText error={devices.error ?? signOut.error ?? signOutOthers.error} />
      {devices.data && (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {devices.data.map((d) => (
            <li key={d.id} className="flex items-center gap-3 py-1 pl-3 pr-1">
              <Monitor className="size-4 shrink-0 text-muted" aria-hidden />
              <div className="min-w-0 flex-1 py-1.5">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="min-w-0 break-words text-sm font-medium">{describeBrowser(d.user_agent)}</span>
                  {d.current && <Badge tone="accent">This browser</Badge>}
                </p>
                <p className="mt-0.5 break-words text-xs text-muted">
                  Last active {formatDateTime(d.last_seen_at)} · signed in {formatDateTime(d.created_at)}
                </p>
              </div>
              {!d.current && (
                <Button
                  variant="ghost"
                  className="shrink-0 px-3"
                  aria-label={`Sign out ${describeBrowser(d.user_agent)}`}
                  disabled={signOut.isPending}
                  onClick={() => signOut.mutate(d.id)}
                >
                  <LogOut className="size-4" aria-hidden /> Sign out
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {others > 1 && (
        <div className="flex flex-wrap gap-2">
          <Button variant="ghost" disabled={signOutOthers.isPending} onClick={() => signOutOthers.mutate()}>
            <LogOut className="size-4" aria-hidden /> Sign out all other devices
          </Button>
        </div>
      )}
    </Section>
  );
}
