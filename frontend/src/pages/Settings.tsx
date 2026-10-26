import { useEffect, useState, useSyncExternalStore, type ComponentType, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, NavLink, useParams } from "react-router";
import {
  Archive,
  Bell,
  BellRing,
  Bot,
  ChevronLeft,
  ChevronRight,
  Download,
  Globe,
  History,
  KeyRound,
  Map as MapIcon,
  Palette,
  Pause,
  Play,
  PlugZap,
  Radio as RadioIcon,
  SlidersHorizontal,
  Smartphone,
  Sparkles,
  Trash2,
  Volume2,
  Thermometer,
} from "lucide-react";
import { playChime, type SoundSetting } from "../lib/sound";
import { useUpdateInfo } from "../lib/queries";
import { BotAppsSection } from "../components/BotApps";
import { SensorsSection } from "../components/SensorsSection";
import { NetworkSection } from "../components/NetworkSettings";
import { ApiKeysSection } from "../components/ApiKeys";
import { BackupSection } from "../components/BackupRestore";
import { RadioHatPanel, useRadioHat } from "../components/RadioHat";
import { FirmwareBadge, useRadioFirmware } from "../components/RadioFirmware";
import { api, type AppInstall, type Device, type MapConfig, type RadioConfig, type RadioMode, type SensorsState } from "../lib/api";
import {
  disablePush,
  enablePush,
  httpsPage,
  isIOS,
  pushSupported,
  runningInstalled,
  type WebPushStatus,
} from "../lib/pwa";
import { Badge, Button, ErrorText, Field, Input } from "../components/ui";
import { InfoTip } from "../components/InfoTip";
import { Section } from "../components/SettingsKit";
import { Dialog } from "../components/Dialog";
import { radioSummary } from "../components/StatusPill";
import { useStatus } from "./Shell";
import { applyTheme, cx, formatDateTime, getThemePref, relativeSeconds, type ThemePref } from "../lib/util";

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm">
      <dt className="shrink-0 text-muted">{k}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  );
}

// ---- layout: categories in a sidebar (wide screens) or a menu to tap into (phones) ------------

type CategoryId = "radio" | "sensors" | "notifications" | "app" | "map" | "bot" | "network" | "software" | "api" | "data";

interface Category {
  id: CategoryId;
  label: string;
  sub: string;
  icon: ComponentType<{ className?: string }>;
  /** A page of its own (instead of /settings/:id). */
  to?: string;
}

const CATEGORIES: Category[] = [
  { id: "radio", label: "Radio", sub: "Connection and device", icon: RadioIcon },
  { id: "sensors", label: "Sensors", sub: "Environment and GPS", icon: Thermometer },
  { id: "notifications", label: "Notifications", sub: "Sounds and alerts", icon: Bell },
  { id: "app", label: "App & appearance", sub: "Theme and installing", icon: Palette },
  { id: "map", label: "Map", sub: "Tiles and place names", icon: MapIcon },
  { id: "bot", label: "Bot", sub: "Commands and apps", icon: Bot },
  { id: "network", label: "Network & HTTPS", sub: "Ports and certificate", icon: Globe },
  { id: "software", label: "Software", sub: "Updates and firmware", icon: Download, to: "/settings/updates" },
  { id: "api", label: "API keys", sub: "For scripts and services", icon: KeyRound },
  { id: "data", label: "Backup & data", sub: "Backups and export", icon: Archive },
];

const WIDE = "(min-width: 1024px)"; // room for the conversation list, this menu and a page

function useWide(): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = matchMedia(WIDE);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => matchMedia(WIDE).matches,
  );
}

function categoryPath(c: Category) {
  return c.to ?? `/settings/${c.id}`;
}

function CategoryBadge({ id }: { id: CategoryId }) {
  const u = useUpdateInfo();
  if (id === "software" && u.data?.update_available) return <Badge tone="accent">Update</Badge>;
  return null;
}

/** Settings pages: the category menu beside the page on wide screens; the page alone on phones. */
export function SettingsLayout({ active, children }: { active: CategoryId; children: ReactNode }) {
  return (
    <div className="flex h-full">
      <nav aria-label="Settings" className="pwa-top hidden w-56 shrink-0 overflow-y-auto border-r border-line bg-surface lg:block">
        <h2 className="px-4 pb-2 pt-4 text-base font-semibold">Settings</h2>
        <ul className="space-y-0.5 px-2 pb-4">
          {CATEGORIES.map((c) => (
            <li key={c.id}>
              <NavLink
                to={categoryPath(c)}
                className={cx(
                  "flex min-h-10 items-center gap-2.5 rounded-lg px-3 text-sm",
                  active === c.id ? "bg-accent-soft/60 font-medium text-ink" : "text-muted hover:bg-surface-2 hover:text-ink",
                )}
              >
                <c.icon className="size-4 shrink-0" />
                <span className="min-w-0 flex-1 truncate">{c.label}</span>
                <CategoryBadge id={c.id} />
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/** A settings page: its title bar (with the way back on phones) and its sections. */
function SettingsPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex h-full flex-col">
      <header className="pwa-top flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 lg:px-4">
        <Link
          to="/settings"
          className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2 lg:hidden"
          aria-label="All settings"
        >
          <ChevronLeft className="size-6" />
        </Link>
        <h2 className="flex-1 px-1 py-2 text-base font-semibold">{title}</h2>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-3xl space-y-4">{children}</div>
      </div>
    </div>
  );
}

/** Phones: the list of categories. */
function SettingsMenu() {
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
        <h2 className="flex-1 px-1 py-2 text-base font-semibold">Settings</h2>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <ul className="mx-auto max-w-3xl divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
          {CATEGORIES.map((c) => (
            <li key={c.id}>
              <Link to={categoryPath(c)} className="flex min-h-14 items-center gap-3 px-4 py-2 hover:bg-surface-2">
                <c.icon className="size-5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{c.label}</span>
                  <span className="block truncate text-xs text-muted">{c.sub}</span>
                </span>
                <CategoryBadge id={c.id} />
                <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function CategoryContent({ id }: { id: CategoryId }) {
  switch (id) {
    case "radio":
      return (
        <>
          <RadioSection />
          <DeviceSection />
        </>
      );
    case "notifications":
      return (
        <>
          <NotificationsSection />
          {/* Notifications on a device need HTTPS (they work through an installed app). */}
          {httpsPage() && <DeviceNotificationsSection />}
        </>
      );
    case "app":
      return (
        <>
          <AppearanceSection />
          {/* Browsers only install apps from HTTPS pages. */}
          {httpsPage() && <AppInstallSection />}
        </>
      );
    case "sensors":
      return <SensorsSection />;
    case "map":
      return <MapSection />;
    case "bot":
      return (
        <>
          <BotSection />
          <BotAppsSection />
        </>
      );
    case "network":
      return <NetworkSection />;
    case "api":
      return <ApiKeysSection />;
    case "data":
      return (
        <>
          <BackupSection />
          <DataSection />
        </>
      );
    default:
      return null;
  }
}

export function Settings() {
  const { category } = useParams();
  const wide = useWide();
  if (!category) return wide ? <Navigate to="/settings/radio" replace /> : <SettingsMenu />;
  const c = CATEGORIES.find((x) => x.id === category);
  if (!c) return <Navigate to="/settings" replace />;
  if (c.to) return <Navigate to={c.to} replace />;
  return (
    <SettingsLayout active={c.id}>
      <SettingsPage title={c.label}>
        <CategoryContent id={c.id} />
      </SettingsPage>
    </SettingsLayout>
  );
}

function RadioSection() {
  const qc = useQueryClient();
  const status = useStatus();
  const cfg = useQuery({ queryKey: ["radio-settings"], queryFn: () => api<RadioConfig>("/api/settings/radio") });
  const hat = useRadioHat();
  const [mode, setMode] = useState<RadioMode>("none");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("5000");
  const [interval, setIntervalS] = useState("60");

  useEffect(() => {
    if (!cfg.data) return;
    setMode(cfg.data.mode);
    setHost(cfg.data.host);
    setPort(String(cfg.data.port));
    setIntervalS(String(cfg.data.sim_interval_seconds));
  }, [cfg.data]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["radio-settings"] });
    qc.invalidateQueries({ queryKey: ["status"] });
  };
  const save = useMutation({
    mutationFn: () =>
      api<RadioConfig>("/api/settings/radio", {
        method: "PUT",
        json: { mode, host: host.trim(), port: Number(port), sim_interval_seconds: Number(interval) },
      }),
    onSuccess: invalidate,
  });
  const test = useMutation({
    mutationFn: () =>
      api<{ reachable: boolean; detail: string }>("/api/radio/test-connection", {
        json: { host: host.trim(), port: Number(port) },
      }),
  });
  const pause = useMutation({
    mutationFn: (p: boolean) => api(`/api/radio/${p ? "pause" : "resume"}`, { method: "POST" }),
    onSuccess: invalidate,
  });
  const simulate = useMutation({ mutationFn: () => api("/api/radio/simulate-incoming", { method: "POST" }) });

  const r = status.data?.radio;
  const gaps = status.data?.gaps ?? [];
  const [showGaps, setShowGaps] = useState(false);
  const summary = radioSummary(status.data);
  const dirty =
    cfg.data &&
    (mode !== cfg.data.mode ||
      host.trim() !== cfg.data.host ||
      Number(port) !== cfg.data.port ||
      Number(interval) !== cfg.data.sim_interval_seconds);
  // The HAT option is offered on a Raspberry Pi 4/5 native install (or when it is already chosen).
  const offerHat = !!hat.data?.board || cfg.data?.mode === "hat";
  const hatUsable = hat.data && ["ready", "stopped", "needs_reboot"].includes(hat.data.phase);

  return (
    <Section
      title="Radio connection"
      description="How this server reaches your radio."
      info={
        <>
          <p>The server keeps one connection to your radio and collects messages even when no browser is open.</p>
          <p>
            <strong>Pause for maintenance</strong> lets go of the radio (for example to use it with the MeshCore app);
            the time paused shows under Collection gaps.
          </p>
        </>
      }
    >
      <div className="rounded-lg border border-line bg-surface-2/50 p-3">
        <p className="flex items-center gap-2 text-sm font-medium">
          <Badge tone={summary.tone === "muted" ? "muted" : summary.tone}>{summary.label}</Badge>
        </p>
        {r && (
          <dl className="mt-2 divide-y divide-line">
            <Row k="Detail">{r.detail || "—"}</Row>
            <Row k="Last radio interaction">{relativeSeconds(r.last_interaction_at, status.data?.server_time)}</Row>
            <Row k="Received / sent (this run)">
              {r.received} / {r.sent}
            </Row>
            <Row k="Reconnect attempts">{r.reconnects}</Row>
            {r.next_retry_at && <Row k="Next retry">{relativeSeconds(r.next_retry_at, status.data?.server_time).replace(" ago", "")}</Row>}
            {r.last_error && <Row k="Last error">{r.last_error}</Row>}
            {r.storage_warning && <Row k="Storage">{r.storage_warning}</Row>}
            {status.data && !status.data.app.radio_enabled_env && (
              <Row k="Deployment">RADIO_ENABLED=false overrides these settings</Row>
            )}
          </dl>
        )}
      </div>

      <fieldset className="space-y-2">
        <legend className="mb-1 text-sm font-medium">Mode</legend>
        <div className={cx("grid gap-2", offerHat ? "sm:grid-cols-2" : "sm:grid-cols-3")}>
          {(
            [
              ["simulated", "Simulated", "Sample traffic, no hardware"],
              ["tcp", "MeshCore TCP", "Ethernet companion radio"],
              ...(offerHat ? [["hat", "Radio HAT on this Pi", "RAK6421 LoRa HAT, run by MeshHome"]] : []),
              ["none", "None", "No radio connection"],
            ] as [RadioMode, string, string][]
          ).map(([m, title, sub]) => (
            <label
              key={m}
              className={cx(
                "flex cursor-pointer items-start gap-2 rounded-lg border p-3",
                mode === m ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
              )}
            >
              <input type="radio" name="radio-mode" checked={mode === m} onChange={() => setMode(m)} className="mt-1 accent-[var(--accent)]" />
              <span>
                <span className="block text-sm font-medium">{title}</span>
                <span className="block text-xs text-muted">{sub}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {mode === "tcp" && (
        <div className="space-y-3">
          <div className="grid grid-cols-[1fr_6.5rem] gap-3">
            <Field
              label="Radio address"
              htmlFor="radio-host"
              info="The radio's IP address or hostname. Give the radio a fixed address (a DHCP reservation in your router) so it doesn't change."
            >
              <Input
                id="radio-host"
                placeholder="192.168.1.50"
                autoCapitalize="none"
                spellCheck={false}
                value={host}
                onChange={(e) => setHost(e.target.value)}
              />
            </Field>
            <Field label="TCP port" htmlFor="radio-port">
              <Input id="radio-port" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} />
            </Field>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => test.mutate()} disabled={!host.trim() || test.isPending}>
              <PlugZap className="size-4" aria-hidden /> {test.isPending ? "Testing…" : "Test reachability"}
            </Button>
            <InfoTip label="About the reachability test">
              It only checks that the TCP port is open. The MeshCore TCP adapter hasn't been verified against real hardware
              yet, so check the status above after saving.
            </InfoTip>
            {test.data && (
              <span className={cx("text-sm", test.data.reachable ? "text-ok" : "text-danger")} role="status">
                {test.data.reachable ? "Reachable" : "Not reachable"} — {test.data.detail}
              </span>
            )}
          </div>
        </div>
      )}

      {mode === "hat" && (
        <RadioHatPanel
          connected={r?.state === "connected"}
          beforeReboot={cfg.data?.mode !== "hat" ? () => save.mutateAsync() : undefined}
        />
      )}
      {mode === "hat" && cfg.data?.mode !== "hat" && hat.data && !hatUsable && (
        <p className="text-xs text-muted">Set up the radio HAT first, then select Save and reconnect.</p>
      )}

      {mode === "simulated" && (
        <Field label="Seconds between simulated messages" htmlFor="sim-interval" hint="On average; 0 turns automatic traffic off.">
          <Input
            id="sim-interval"
            inputMode="numeric"
            className="max-w-32"
            value={interval}
            onChange={(e) => setIntervalS(e.target.value)}
          />
        </Field>
      )}

      <ErrorText error={save.error ?? pause.error ?? simulate.error ?? test.error} />
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          onClick={() => save.mutate()}
          disabled={!dirty || save.isPending || (mode === "hat" && cfg.data?.mode !== "hat" && !hatUsable)}
        >
          {save.isPending ? "Saving…" : "Save and reconnect"}
        </Button>
        {cfg.data && cfg.data.mode !== "none" && (
          <Button onClick={() => pause.mutate(!cfg.data!.paused)} disabled={pause.isPending}>
            {cfg.data.paused ? <Play className="size-4" aria-hidden /> : <Pause className="size-4" aria-hidden />}
            {cfg.data.paused ? "Resume radio" : "Pause for maintenance"}
          </Button>
        )}
        {r?.state === "connected" && (
          <Link
            to="/settings/node"
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium hover:bg-surface-2"
          >
            <SlidersHorizontal className="size-4" aria-hidden /> Configure node settings
          </Link>
        )}
        <Button onClick={() => setShowGaps(true)}>
          <History className="size-4" aria-hidden /> Collection gaps
          {gaps.length > 0 && <Badge tone={gaps.some((g) => g.open) ? "warn" : "muted"}>{gaps.some((g) => g.open) ? "ongoing" : gaps.length}</Badge>}
        </Button>
        {r?.state === "connected" && r.is_simulated && (
          <Button onClick={() => simulate.mutate()} disabled={simulate.isPending}>
            <Sparkles className="size-4" aria-hidden /> Simulate incoming message
          </Button>
        )}
      </div>
      {showGaps && <GapsDialog onClose={() => setShowGaps(false)} />}
    </Section>
  );
}

function DeviceSection() {
  const device = useQuery({ queryKey: ["device"], queryFn: () => api<Device>("/api/device") });
  const firmware = useRadioFirmware();
  const d = device.data;
  if (!d?.radio)
    return (
      <Section title="Device">
        <p className="text-sm text-muted">No radio has connected yet.</p>
      </Section>
    );
  const rf = d.radio.rf ?? {};
  return (
    <Section title="Device" description="Reported by the radio.">
      <dl className="divide-y divide-line">
        <Row k="Name">
          {d.radio.name} {d.radio.is_simulated && <Badge tone="warn">Simulated</Badge>}
        </Row>
        <Row k="Public key">
          <code className="break-all font-mono text-xs">{d.radio.public_key}</code>
        </Row>
        <Row k="Model">{String(d.radio.device_info.model ?? "—")}</Row>
        <Row k="Firmware">
          <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
            {String(d.radio.device_info.firmware ?? "—")}
            <Link to="/settings/updates" title="Radio firmware">
              <FirmwareBadge s={firmware.data} />
            </Link>
          </span>
        </Row>
        <Row k="RF">
          {rf.freq_mhz ? `${rf.freq_mhz} MHz · BW ${rf.bw_khz} kHz · SF${rf.sf} · CR${rf.cr} · ${rf.tx_power_dbm} dBm` : "—"}
        </Row>
        <Row k="Last connected">{formatDateTime(d.radio.last_connected_at)}</Row>
        <Row k="Contacts known">{d.contacts}</Row>
      </dl>
      <div>
        <h4 className="mb-1 flex items-center gap-1 text-sm font-medium">
          Channels
          <InfoTip label="About channels">Channel keys are never stored or shown by MeshHome.</InfoTip>
        </h4>
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {d.channels.map((c) => (
            <li key={`${c.slot}-${c.generation}`} className="flex items-center justify-between gap-2 px-3 py-2">
              <span>
                <span className="text-muted">Slot {c.slot}</span> · {c.name}
              </span>
              <span className="flex gap-1">
                {c.generation > 1 && <Badge>gen {c.generation}</Badge>}
                {!c.active && <Badge>archived</Badge>}
              </span>
            </li>
          ))}
          {d.channels.length === 0 && <li className="px-3 py-2 text-muted">No channels.</li>}
        </ul>
      </div>
    </Section>
  );
}

function GapsDialog({ onClose }: { onClose: () => void }) {
  const status = useStatus();
  const gaps = status.data?.gaps ?? [];
  return (
    <Dialog title="Collection gaps" onClose={onClose} footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
      <p className="mb-3 text-sm text-muted">
        When the archive wasn't collecting. Messages from these times may be missing: the radio's own buffer is small.
      </p>
      {gaps.length === 0 ? (
        <p className="text-sm text-muted">No gaps recorded.</p>
      ) : (
        <ul className="divide-y divide-line text-sm">
          {gaps.map((g, i) => (
            <li key={i} className="flex flex-wrap justify-between gap-x-4 gap-y-0.5 py-2">
              <span>{g.reason}</span>
              <span className="text-muted">
                {formatDateTime(g.started_at)} → {g.open ? <Badge tone="warn">ongoing</Badge> : formatDateTime(g.ended_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Dialog>
  );
}

const OSM_DEFAULT: MapConfig = {
  tile_url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  attribution: "© OpenStreetMap contributors",
  max_zoom: 19,
  place_names: true,
};

function MapSection() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["map-settings"], queryFn: () => api<MapConfig>("/api/settings/map") });
  const [form, setForm] = useState<MapConfig>(OSM_DEFAULT);
  useEffect(() => {
    if (cfg.data) setForm(cfg.data);
  }, [cfg.data]);
  const save = useMutation({
    mutationFn: (body: MapConfig) => api<MapConfig>("/api/settings/map", { method: "PUT", json: body }),
    onSuccess: (saved) => {
      setForm(saved);
      qc.invalidateQueries({ queryKey: ["map-settings"] });
      qc.invalidateQueries({ queryKey: ["map"] });
    },
  });
  const dirty = cfg.data && JSON.stringify(cfg.data) !== JSON.stringify(form);
  return (
    <Section
      title="Map"
      description="Where map tiles come from, and place names."
      info={
        <>
          <p>
            Your browser loads map tiles from this tile server, which sees which areas you view. The default is{" "}
            <a href="https://www.openstreetmap.org" target="_blank" rel="noopener">
              OpenStreetMap
            </a>{" "}
            (free, open data, for light personal use).
          </p>
          <p>For full privacy, point it at a self-hosted XYZ tile server.</p>
        </>
      }
    >
      <Field label="Tile URL template" htmlFor="tile-url" hint="https:// with {z}, {x} and {y} placeholders.">
        <Input
          id="tile-url"
          spellCheck={false}
          autoCapitalize="none"
          className="font-mono text-xs sm:text-xs"
          value={form.tile_url}
          onChange={(e) => setForm({ ...form, tile_url: e.target.value })}
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
        <Field label="Attribution" htmlFor="tile-attr" info="Shown on the map. Most tile providers require it.">
          <Input id="tile-attr" value={form.attribution} onChange={(e) => setForm({ ...form, attribution: e.target.value })} />
        </Field>
        <Field label="Max zoom" htmlFor="tile-zoom">
          <Input
            id="tile-zoom"
            inputMode="numeric"
            value={String(form.max_zoom)}
            onChange={(e) => setForm({ ...form, max_zoom: Number(e.target.value) || 0 })}
          />
        </Field>
      </div>
      <label className="flex cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          checked={form.place_names}
          onChange={(e) => setForm({ ...form, place_names: e.target.checked })}
          className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
        />
        <span>
          <span className="flex items-center gap-1 text-sm font-medium">
            Place names
            <InfoTip label="About place names">
              This server looks the town up from{" "}
              <a href="https://nominatim.org" target="_blank" rel="noopener">
                OpenStreetMap Nominatim
              </a>
              , sending only the node's advertised coordinates. Each spot is looked up once and remembered.
            </InfoTip>
          </span>
          <span className="block text-xs text-muted">The town at a node's position, in its details.</span>
        </span>
      </label>
      <ErrorText error={save.error} />
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => save.mutate(form)} disabled={!dirty || save.isPending}>
          Save map settings
        </Button>
        <Button onClick={() => save.mutate({ ...OSM_DEFAULT, place_names: form.place_names })} disabled={save.isPending}>
          Reset to OpenStreetMap
        </Button>
      </div>
    </Section>
  );
}

function AppInstallSection() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["app-install"], queryFn: () => api<AppInstall>("/api/settings/app-install") });
  const save = useMutation({
    mutationFn: (enabled: boolean) => api<AppInstall>("/api/settings/app-install", { method: "PUT", json: { enabled } }),
    onSuccess: (v) => qc.setQueryData(["app-install"], v),
  });
  const c = cfg.data;
  const secure = httpsPage();
  // HTTPS in the browser, but a proxy in front didn't tell the server (X-Forwarded-Proto).
  const proxyMismatch = secure && c && !c.https;
  const canEnable = secure && !!c?.https;
  return (
    <Section
      title="Install as an app"
      description="On phones and computers, from the browser. Needs HTTPS."
      info={
        <>
          <p>
            The installed app opens full-screen with its own icon, and shows a clear message when the server can't be
            reached. It's also what notifications on a device need.
          </p>
          <p>
            Only the app's own files are kept on the device, never messages or settings. Browsers only install apps from
            secure (HTTPS) pages.
          </p>
        </>
      }
    >
      {!c ? (
        <ErrorText error={cfg.error} />
      ) : (
        <>
          <label className={cx("flex items-start gap-3", canEnable || c.enabled ? "cursor-pointer" : "cursor-not-allowed opacity-60")}>
            <input
              type="checkbox"
              checked={c.enabled}
              disabled={save.isPending || (!c.enabled && !canEnable)}
              onChange={(e) => save.mutate(e.target.checked)}
              className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
            />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-medium">
                <Smartphone className="size-4 text-muted" aria-hidden /> Allow installing MeshHome
                {c.enabled && runningInstalled() && <Badge tone="ok">You're using the installed app</Badge>}
              </span>
            </span>
          </label>
          {!secure && (
            <p className="rounded-lg bg-warn/10 p-3 text-sm">
              Open MeshHome at its https:// address to turn this on.{" "}
              <Link to="/settings/network" className="text-accent underline">
                Network &amp; HTTPS
              </Link>
            </p>
          )}
          {proxyMismatch && (
            <p className="rounded-lg bg-warn/10 p-3 text-sm">
              The server thinks this is plain HTTP. Make the proxy in front of MeshHome send{" "}
              <code className="font-mono text-xs">X-Forwarded-Proto: https</code>.
            </p>
          )}
          {c.enabled && secure && (
            <div className="space-y-1 rounded-lg bg-surface-2 p-3 text-sm">
              <p className="flex items-center gap-1 font-medium">
                To install it
                <InfoTip label="About installing">
                  Each address is a separate app, so install it from the address you use day to day ({location.host}).
                </InfoTip>
              </p>
              <ul className="space-y-0.5 text-muted">
                <li>
                  <span className="text-ink">iPhone, iPad:</span> Safari → Share → Add to Home Screen
                </li>
                <li>
                  <span className="text-ink">Android:</span> Chrome → ⋮ → Install app
                </li>
                <li>
                  <span className="text-ink">Computer:</span> the install icon in Chrome's or Edge's address bar
                </li>
              </ul>
            </div>
          )}
          <ErrorText error={save.error} />
        </>
      )}
    </Section>
  );
}

function DeviceNotificationsSection() {
  const qc = useQueryClient();
  const install = useQuery({ queryKey: ["app-install"], queryFn: () => api<AppInstall>("/api/settings/app-install") });
  const on = !!install.data?.enabled && httpsPage();
  const st = useQuery({ queryKey: ["web-push"], queryFn: () => api<WebPushStatus>("/api/web-push"), enabled: on });
  const refresh = () => qc.invalidateQueries({ queryKey: ["web-push"] });
  const change = useMutation({
    mutationFn: async (next: { dms: boolean; channels: boolean } | null) => {
      if (next === null) await disablePush();
      else await enablePush(st.data!.public_key!, next);
    },
    onSettled: refresh,
  });
  const test = useMutation({
    mutationFn: () => api<{ ok: boolean; error: string | null }>("/api/web-push/test", { method: "POST" }),
    onSettled: refresh,
  });
  if (!on) {
    return (
      <Section title="This device" description="Notifications when MeshHome is closed.">
        <p className="text-sm">
          Turn on{" "}
          <Link to="/settings/app" className="text-accent underline">
            Install as an app
          </Link>{" "}
          first{httpsPage() ? "" : ", from MeshHome's https:// address"}.
        </p>
      </Section>
    );
  }
  const d = st.data;
  const sub = d?.subscription ?? null;
  const iosNotInstalled = isIOS() && !runningInstalled();
  const blocked = pushSupported() && Notification.permission === "denied";
  const prefs = { dms: sub?.dms ?? true, channels: sub?.channels ?? false };
  return (
    <Section
      title="This device"
      description="Notifications when MeshHome is closed."
      info={
        <>
          <p>
            Your MeshHome server sends them through this browser's push service (Google's, Apple's or Mozilla's), encrypted
            so the push service only sees the time and size.
          </p>
          <p>A conversation's own sound setting (on or off) overrides the choices here.</p>
        </>
      }
    >
      {!d ? (
        <ErrorText error={st.error} />
      ) : iosNotInstalled ? (
        <p className="rounded-lg bg-surface-2 p-3 text-sm">
          On iPhone and iPad, notifications need the Home Screen app (iOS 16.4 or later): Safari → Share → Add to Home
          Screen, then turn them on there.
        </p>
      ) : !pushSupported() ? (
        <p className="rounded-lg bg-warn/10 p-3 text-sm">This browser doesn't support web push notifications.</p>
      ) : (
        <>
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={!!sub}
              disabled={change.isPending}
              onChange={(e) => change.mutate(e.target.checked ? prefs : null)}
              className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
            />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-medium">
                <BellRing className="size-4 text-muted" aria-hidden /> Notify me on this device
              </span>
              <span className="block text-xs text-muted">
                {d.devices === 0 ? "No devices yet" : `${d.devices} device${d.devices === 1 ? "" : "s"} with notifications on`}
              </span>
            </span>
          </label>
          {sub && (
            <div className="space-y-2 pl-8">
              <div className="flex flex-wrap gap-x-5 gap-y-2">
              {(
                [
                  ["dms", "Direct messages"],
                  ["channels", "Channels"],
                ] as const
              ).map(([k, label]) => (
                <label key={k} className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={sub[k]}
                    disabled={change.isPending}
                    onChange={(e) => change.mutate({ ...prefs, [k]: e.target.checked })}
                    className="size-4 accent-[var(--accent)]"
                  />
                  {label}
                </label>
              ))}
              </div>
              <div className="flex flex-wrap items-center gap-3 pt-1">
                <Button onClick={() => test.mutate()} disabled={test.isPending}>
                  {test.isPending ? "Sending…" : "Send a test notification"}
                </Button>
                {test.data?.ok && <span className="text-sm text-ok">Sent</span>}
              </div>
              {sub.last_error && <p className="text-sm text-danger">Last attempt failed: {sub.last_error}</p>}
              {sub.last_sent_at && !sub.last_error && (
                <p className="text-xs text-muted">Last sent {formatDateTime(sub.last_sent_at)}</p>
              )}
            </div>
          )}
          {blocked && (
            <p className="rounded-lg bg-warn/10 p-3 text-sm">
              Notifications are blocked for MeshHome. Allow them in the browser's site settings (or the phone's), then try
              again.
            </p>
          )}
          <ErrorText error={change.error ?? test.error ?? (test.data && !test.data.ok ? new Error(test.data.error ?? "Failed") : null)} />
        </>
      )}
    </Section>
  );
}

function NotificationsSection() {
  const qc = useQueryClient();
  const cfg = useQuery({
    queryKey: ["notification-settings"],
    queryFn: () => api<{ sound: SoundSetting }>("/api/settings/notifications"),
  });
  const save = useMutation({
    mutationFn: (sound: SoundSetting) =>
      api<{ sound: SoundSetting }>("/api/settings/notifications", { method: "PUT", json: { sound } }),
    onSuccess: (v) => qc.setQueryData(["notification-settings"], v),
  });
  const current = cfg.data?.sound;
  const options: [SoundSetting, string, string][] = [
    ["all", "All messages", "Channels and direct messages"],
    ["dms", "Direct messages", "Channels stay quiet"],
    ["off", "Off", "Only conversations you turn on"],
  ];
  return (
    <Section
      title="Sounds"
      description="A chime for new messages while MeshHome is open."
      info={
        <>
          <p>Shared by all your browsers. Right-click (or long-press) a conversation to turn its sound on or off.</p>
          <p>Phones may silence web pages that are in the background.</p>
        </>
      }
    >
      <fieldset className="grid gap-2 sm:grid-cols-3">
        <legend className="sr-only">Play a sound for</legend>
        {options.map(([value, title, sub]) => (
          <label
            key={value}
            className={cx(
              "flex cursor-pointer items-start gap-2 rounded-lg border p-3",
              current === value ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
            )}
          >
            <input
              type="radio"
              name="sound"
              checked={current === value}
              disabled={save.isPending}
              onChange={() => save.mutate(value)}
              className="mt-1 accent-[var(--accent)]"
            />
            <span>
              <span className="block text-sm font-medium">{title}</span>
              <span className="block text-xs text-muted">{sub}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <ErrorText error={save.error} />
      <div>
        <Button onClick={playChime}>
          <Volume2 className="size-4" aria-hidden /> Test sound
        </Button>
      </div>
    </Section>
  );
}

type BotConfig = { enabled: boolean; allow: "favorites" | "everyone"; env: boolean };

const BOT_COMMANDS: [string, string][] = [
  ["/info", "Node name, version, uptime, radio settings, contacts"],
  ["/ping", "\u201cpong\u201d with signal (SNR, RSSI) and hops"],
  ["/help", "The list of commands"],
];

function BotSection() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["bot-settings"], queryFn: () => api<BotConfig>("/api/settings/bot") });
  // The change shows at once and stays until the server answers (a refetch cannot undo it).
  const [pending, setPending] = useState<BotConfig | null>(null);
  const save = useMutation({
    mutationFn: (next: BotConfig) => api<BotConfig>("/api/settings/bot", { method: "PUT", json: next }),
    onMutate: (next) => setPending(next),
    onSuccess: (v) => qc.setQueryData(["bot-settings"], v),
    onSettled: () => setPending(null),
  });
  const c = pending ?? cfg.data;
  // /env answers from the environment sensor on this Pi (Settings → Sensors), when there is one.
  const sensors = useQuery({ queryKey: ["sensors"], queryFn: () => api<SensorsState>("/api/sensors") });
  const envSensor = sensors.data?.environment.status;
  return (
    <Section
      title="Bot"
      description="Replies to commands like /info sent to this node by direct message."
      info={
        <>
          <p>Channels are never answered, and blocked contacts are always ignored.</p>
          <p>
            To save airtime it replies to each contact at most once every 10 seconds and 6 times a minute in total (quick
            follow-ups are answered in turn), and ignores commands more than 15 minutes old. Replies appear in the
            conversation like your own messages.
          </p>
        </>
      }
    >
      {!c ? (
        <ErrorText error={cfg.error} />
      ) : (
        <>
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={c.enabled}
              onChange={(e) => save.mutate({ ...c, enabled: e.target.checked })}
              className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
            />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-medium">
                <Bot className="size-4 text-muted" aria-hidden /> Answer commands
              </span>
              <span className="block text-xs text-muted">Direct messages that start with “/”</span>
            </span>
          </label>
          <fieldset className="grid gap-2 sm:grid-cols-2" disabled={!c.enabled}>
            <legend className="mb-2 text-sm font-medium">Who can use it</legend>
            {(
              [
                ["favorites", "Favourite contacts", "The ones you've starred"],
                ["everyone", "Every contact", "Anyone in your radio's contacts"],
              ] as const
            ).map(([value, title, sub]) => (
              <label
                key={value}
                className={cx(
                  "flex cursor-pointer items-start gap-2 rounded-lg border p-3",
                  !c.enabled && "cursor-not-allowed opacity-60",
                  c.allow === value ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
                )}
              >
                <input
                  type="radio"
                  name="bot-allow"
                  checked={c.allow === value}
                  onChange={() => save.mutate({ ...c, allow: value })}
                  className="mt-1 accent-[var(--accent)]"
                />
                <span>
                  <span className="block text-sm font-medium">{title}</span>
                  <span className="block text-xs text-muted">{sub}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <dl className="space-y-1.5 rounded-lg bg-surface-2 p-3 text-sm">
            {BOT_COMMANDS.map(([cmd, what]) => (
              <div key={cmd} className="flex gap-3">
                <dt className="w-20 shrink-0 font-mono font-medium">{cmd}</dt>
                <dd className="text-muted">{what}</dd>
              </div>
            ))}
            <div className="flex gap-3 border-t border-line pt-1.5">
              <dt className="w-20 shrink-0 font-mono font-medium">/env</dt>
              <dd className="min-w-0 flex-1">
                <label className={cx("flex cursor-pointer items-start gap-2", !c.enabled && "cursor-not-allowed opacity-60")}>
                  <input
                    type="checkbox"
                    disabled={!c.enabled}
                    checked={c.env}
                    onChange={(e) => save.mutate({ ...c, env: e.target.checked })}
                    className="mt-0.5 size-4 shrink-0 accent-[var(--accent)]"
                  />
                  <span>
                    <span className="block text-ink">Answer /env</span>
                    <span className="block text-xs text-muted">
                      {envSensor === "ok"
                        ? "Temperature, humidity, pressure and air quality from this Pi's environment sensor, for the contacts allowed above."
                        : "No environment sensor is working on this Pi (Settings → Sensors), so /env isn't answered."}
                    </span>
                  </span>
                </label>
              </dd>
            </div>
          </dl>
          <p className="text-xs text-muted">Apps below add more commands.</p>
          <ErrorText error={save.error} />
        </>
      )}
    </Section>
  );
}

function AppearanceSection() {
  const [pref, setPref] = useState<ThemePref>(getThemePref());
  return (
    <Section title="Appearance">
      <div className="flex gap-2" role="radiogroup" aria-label="Theme">
        {(["system", "light", "dark"] as ThemePref[]).map((t) => (
          <button
            key={t}
            role="radio"
            aria-checked={pref === t}
            onClick={() => {
              setPref(t);
              applyTheme(t);
            }}
            className={cx(
              "min-h-11 flex-1 rounded-lg border text-sm capitalize",
              pref === t ? "border-accent bg-accent-soft/40 font-medium" : "border-line hover:bg-surface-2",
            )}
          >
            {t}
          </button>
        ))}
      </div>
    </Section>
  );
}

function DataSection() {
  const qc = useQueryClient();
  const status = useStatus();
  const del = useMutation({
    mutationFn: () => api<{ deleted_radios: number }>("/api/simulated-data", { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const simActive = status.data?.radio_config.mode === "simulated";
  return (
    <Section
      title="Data"
      description={`${status.data?.database.messages ?? 0} messages archived`}
      info="Export the archive as JSON. Deleting simulated data needs the radio switched away from Simulated first; real radio data isn't affected."
    >
      <div className="flex flex-wrap gap-2">
        <a
          href="/api/export"
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium hover:bg-surface-2"
        >
          <Download className="size-4" aria-hidden /> Export archive (JSON)
        </a>
        <Button
          variant="danger"
          disabled={simActive || del.isPending}
          title={simActive ? "Switch away from the simulated radio first" : undefined}
          onClick={() => {
            if (confirm("Delete all simulated conversations, contacts and messages? Real radio data is not affected.")) del.mutate();
          }}
        >
          <Trash2 className="size-4" aria-hidden /> Delete simulated data
        </Button>
      </div>
      {del.data && <p className="text-sm text-ok">Simulated data deleted.</p>}
      <ErrorText error={del.error} />
    </Section>
  );
}
