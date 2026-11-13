import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {
  Activity,
  BatteryFull,
  BatteryLow,
  BatteryMedium,
  ChevronLeft,
  Download,
  Eye,
  EyeOff,
  List,
  Maximize,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  api,
  type ContactPage,
  type Course,
  type MeshNode,
  type MtConfig,
  type TrackedDevice,
  type TrackingData,
  type Units,
} from "../lib/api";
import { baseLayer, baseLayerKey, usesOffline } from "../lib/tiles";
import { longText, paceText, unitsFor } from "../lib/units";
import { cx, formatDateTime } from "../lib/util";
import { Badge, Button, ErrorText, Field, IconButton, Input } from "../components/ui";
import { Select } from "../components/SettingsKit";
import { Dialog } from "../components/Dialog";
import { TelemetryChips, TelemetryDialog } from "../components/TrackerTelemetry";
import { GATEWAY_COLOR, markerIcon } from "./NodeMap";

// ---- time and formatting ----------------------------------------------------------------------

function useNow(ms = 5000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** "40 s ago", "12 min ago", "3 h ago": seconds matter here. */
export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

type U = ReturnType<typeof unitsFor>;

function distance(u: U, m: number): string {
  return longText(u, m);
}

function intervalLabel(s: number): string {
  return s < 60 ? `${s} s` : `${s / 60} min`;
}

const displayName = (d: TrackedDevice) => d.label || d.name;


function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** "Red Loop · 3.2 of 6.5 mi · 49%", or why there's no progress. Null: no course. */
function progressText(u: U, d: TrackedDevice): { text: string; tone: "ok" | "warn" | "muted" } | null {
  const p = d.progress;
  if (!p) return null;
  if (p.finished) return { text: `${p.course_name} · finished`, tone: "ok" };
  if (p.along_m === null) {
    const off = p.off_course_m !== null ? ` (${Math.round(u.dist(p.off_course_m))} ${u.distUnit} away)` : "";
    return { text: `Off ${p.course_name}${off}`, tone: "warn" };
  }
  return {
    text: `${p.course_name} · ${longText(u, p.along_m, false)} of ${longText(u, p.total_m)} · ${Math.round(p.pct ?? 0)}%`,
    tone: "muted",
  };
}

/** "9:45 /mi · ETA 3:42 PM (24 min)", "Stopped", or null (too few positions yet). */
function paceLine(u: U, d: TrackedDevice, now: number): string | null {
  const p = d.pace;
  if (!p) return null;
  if (!p.moving) return "Not moving";
  let out = paceText(u, p.speed_mps);
  if (p.basis === "straight") out += " (straight line)";
  if (p.eta_at) {
    const min = Math.round((Date.parse(p.eta_at) - now) / 60000);
    out += ` · ETA ${clock(p.eta_at)}${min > 0 ? ` (${min} min)` : min < -1 ? " (overdue)" : " (now)"}`;
  }
  return out;
}

function batteryText(d: TrackedDevice): string | null {
  if (d.battery_pct !== null) return `${d.battery_pct}%`;
  return d.battery_v !== null ? `${d.battery_v.toFixed(2)} V` : null;
}

function BatteryIcon({ d }: { d: TrackedDevice }) {
  const Icon = d.battery_low ? BatteryLow : (d.battery_pct ?? 0) >= 80 ? BatteryFull : BatteryMedium;
  return <Icon className="size-3.5" aria-hidden />;
}

const hopName = (h: { hash: string; name: string | null }) => h.name ?? (h.hash === "?" ? null : h.hash.toUpperCase());

/** "via Hilltop · SNR 6 dB": how its latest answer reached this radio. */
function linkText(d: TrackedDevice): string | null {
  const l = d.link;
  if (!l) return null;
  if (l.route === "mesh") {
    // Meshtastic: the hop count, and the last relay when the packet names it.
    const n = l.hops.length;
    const last = n ? hopName(l.hops[n - 1]) : null;
    const via = n === 0 ? "heard directly" : last ? `via ${last}${n > 1 ? ` (${n} hops)` : ""}` : `${n} hop${n === 1 ? "" : "s"}`;
    return via;
  }
  const near = l.hops.length ? l.hops[l.hops.length - 1] : null;
  return near ? `via ${near.name ?? near.hash.toUpperCase()}${l.hops.length > 1 ? ` (+${l.hops.length - 1})` : ""}` : "heard directly";
}

function routeText(d: TrackedDevice): string | null {
  const l = d.link;
  if (!l) return null;
  if (l.route === "mesh") {
    const names = l.hops.map((h) => hopName(h) ?? "a relay");
    const rssi = l.rssi !== null ? ` (RSSI ${Math.round(l.rssi)} dBm)` : "";
    return `${[displayName(d), ...names, "this radio"].join(" → ")}${rssi}`;
  }
  const names = l.hops.map((h) => h.name ?? h.hash.toUpperCase());
  const route = [displayName(d), ...names, "this radio"].join(" → ");
  return `${route} (${l.route === "flood" ? "by flood" : "on its route"}${l.rssi !== null ? `, RSSI ${Math.round(l.rssi)} dBm` : ""})`;
}

function sensorText(u: U, d: TrackedDevice): string | null {
  const s = d.sensors;
  if (!s) return null;
  const parts: string[] = [];
  if (s.temp_c !== null) parts.push(`${u.temp(s.temp_c).toFixed(u.imperial ? 0 : 1)}${u.tempUnit} inside the tracker`);
  if (s.baro_alt_m !== null) parts.push(`${Math.round(u.alt(s.baro_alt_m))} ${u.altUnit} by barometer`);
  if (s.pressure_hpa !== null) parts.push(`${u.pressure(s.pressure_hpa).toFixed(u.pressureDigits)} ${u.pressureUnit}`);
  if (s.humidity !== null) parts.push(`${Math.round(s.humidity)}% humidity`);
  if (s.lux !== null) parts.push(`${Math.round(s.lux).toLocaleString()} lux`);
  return parts.length ? parts.join(" · ") : null;
}

/** Older than three intervals (and at least 5 minutes): shown faded. */
function isStale(d: TrackedDevice, now: number): boolean {
  if (!d.last_position) return true;
  return now - Date.parse(d.last_position.at) > Math.max(3 * d.interval_s, 300) * 1000;
}

function statusLine(d: TrackedDevice, now: number): { text: string; tone: "ok" | "warn" | "danger" | "muted" } {
  const pos = d.last_position ? `position ${ago(d.last_position.at, now)}` : "no position yet";
  if (d.source === "meshtastic") {
    if (!d.current_radio) return { text: `Meshtastic not connected · ${pos}`, tone: "muted" };
    if (d.asking) return { text: `Asking… · ${pos}`, tone: "muted" };
    if (!d.last_position) return { text: "Waiting for its first broadcast", tone: "muted" };
    if (d.status === "no_answer") return { text: `No answer to the request · ${pos}`, tone: "warn" };
    if (isStale(d, now)) return { text: `Not heard lately · ${pos}`, tone: "warn" };
    return { text: `Position ${ago(d.last_position.at, now)}`, tone: "ok" };
  }
  if (!d.current_radio) return { text: `On another radio · ${pos}`, tone: "muted" };
  if (d.asking) return { text: `Asking… · ${pos}`, tone: "muted" };
  if (!d.polling) return { text: `Paused · ${pos}`, tone: "muted" };
  switch (d.status) {
    case "ok":
      return { text: `Position ${d.last_position ? ago(d.last_position.at, now) : ""}`.trim(), tone: "ok" };
    case "no_answer":
      return { text: `No answer${d.failures > 1 ? ` (${d.failures}×)` : ""} · ${pos}`, tone: "warn" };
    case "no_position":
      return { text: `Answered without a position · ${pos}`, tone: "warn" };
    case "error":
      return { text: `Couldn't ask · ${pos}`, tone: "danger" };
    default:
      return { text: "Waiting for the first answer", tone: "muted" };
  }
}

const TONE_CLASS = { ok: "text-ok", warn: "text-warn", danger: "text-danger", muted: "text-muted" } as const;

function glyph(name: string): string {
  const ch = [...name.trim()][0] ?? "?";
  return /[\p{L}\p{N}]/u.test(ch) ? ch.toUpperCase() : "•";
}

// ---- the page --------------------------------------------------------------------------------

export function Tracking() {
  const qc = useQueryClient();
  const now = useNow();
  const data = useQuery({
    queryKey: ["tracking"],
    queryFn: () => api<TrackingData>("/api/tracking"),
    // Live updates arrive by WebSocket; this is the fallback.
    refetchInterval: 30_000,
  });
  const courses = useQuery({
    queryKey: ["tracking-courses"],
    queryFn: () => api<Course[]>("/api/tracking/courses"),
    staleTime: 5 * 60_000,
  });
  const [panel, setPanel] = useState(false); // phones: the list over the map
  const [tab, setTab] = useState<"trackers" | "courses">("trackers");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<TrackedDevice | null>(null);
  const [telemetryFor, setTelemetryFor] = useState<TrackedDevice | null>(null);

  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const tiles = useRef<L.TileLayer | null>(null);
  const courseLayer = useRef<L.LayerGroup | null>(null);
  const trailLayer = useRef<L.LayerGroup | null>(null);
  const markerLayer = useRef<L.LayerGroup | null>(null);
  const fitted = useRef(false);

  const d = data.data;
  const units: Units = d?.units ?? "metric";
  const u = useMemo(() => unitsFor(units), [units]);
  const devices = d?.devices ?? [];
  const shown = useMemo(() => devices.filter((x) => x.show), [devices]);
  const visibleCourses = useMemo(() => (courses.data ?? []).filter((c) => c.visible), [courses.data]);

  // Create the map once.
  useEffect(() => {
    if (!container.current || map.current) return;
    const m = L.map(container.current, { zoomControl: true }).setView([39.5, -98.35], 4);
    courseLayer.current = L.layerGroup().addTo(m);
    trailLayer.current = L.layerGroup().addTo(m);
    markerLayer.current = L.layerGroup().addTo(m);
    map.current = m;
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(container.current);
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
      tiles.current = null;
    };
  }, []);

  const tileKey = baseLayerKey(d?.tiles, d?.offline);
  useEffect(() => {
    const m = map.current;
    if (!m || !d) return;
    tiles.current?.remove();
    tiles.current = baseLayer(d.tiles, d.offline).addTo(m);
  }, [tileKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Courses: a pale casing under the line keeps it readable on any map.
  useEffect(() => {
    const g = courseLayer.current;
    if (!g) return;
    g.clearLayers();
    for (const c of visibleCourses) {
      for (const seg of c.segments) {
        L.polyline(seg, { color: "#ffffff", weight: 8, opacity: 0.55, interactive: false }).addTo(g);
        const line = L.polyline(seg, { color: c.color, weight: 4, opacity: 0.9 });
        const tip = document.createElement("span");
        tip.textContent = `${c.name} · ${distance(u, c.distance_m)}`;
        line.bindTooltip(tip, { sticky: true }).addTo(g);
      }
      for (const w of c.waypoints) {
        const mk = L.circleMarker([w.lat, w.lon], { radius: 6, color: c.color, weight: 3, fillColor: "#ffffff", fillOpacity: 1 });
        if (w.name) {
          const tip = document.createElement("span");
          tip.textContent = w.name;
          mk.bindTooltip(tip);
        }
        mk.addTo(g);
      }
    }
  }, [visibleCourses, u]);

  // Trails and current positions.
  useEffect(() => {
    const trails = trailLayer.current;
    const markers = markerLayer.current;
    if (!trails || !markers || !d) return;
    trails.clearLayers();
    markers.clearLayers();
    if (d.base) {
      const mk = L.marker([d.base.lat, d.base.lon], {
        icon: markerIcon(GATEWAY_COLOR, "⌂", { gateway: true }),
        title: d.base.name,
        zIndexOffset: 500,
        keyboard: false,
      });
      const tip = document.createElement("span");
      tip.textContent = `${d.base.name} (base)`;
      mk.bindTooltip(tip).addTo(markers);
    }
    for (const dev of shown) {
      const pts = dev.trail.map((p) => [p.lat, p.lon] as L.LatLngTuple);
      if (pts.length > 1) L.polyline(pts, { color: dev.color, weight: 3, opacity: 0.6, dashArray: "6 6" }).addTo(trails);
      for (const p of dev.trail.slice(0, -1)) {
        const dot = L.circleMarker([p.lat, p.lon], { radius: 3, color: dev.color, weight: 1, fillOpacity: 0.7 });
        const tip = document.createElement("span");
        tip.textContent = `${displayName(dev)} · ${formatDateTime(p.at)}`;
        dot.bindTooltip(tip).addTo(trails);
      }
      const last = dev.last_position;
      if (!last) continue;
      const mk = L.marker([last.lat, last.lon], {
        icon: markerIcon(dev.color, glyph(displayName(dev)), { stale: isStale(dev, now) }),
        title: displayName(dev),
        alt: `${displayName(dev)}: position ${ago(last.at, now)}`,
        zIndexOffset: 1000,
        keyboard: true,
      });
      // Names come from the radio: DOM text only, never HTML.
      const tip = document.createElement("span");
      tip.className = "font-medium";
      tip.textContent = `${displayName(dev)} · ${ago(last.at, now)}`;
      mk.bindTooltip(tip, { permanent: true, direction: "top", offset: [0, -12] });
      const pop = document.createElement("div");
      pop.className = "space-y-0.5 text-sm";
      const line = (text: string, cls = "text-xs text-[var(--muted)]") => {
        const el = document.createElement("div");
        el.className = cls;
        el.textContent = text;
        pop.append(el);
      };
      line(displayName(dev), "font-semibold");
      if (dev.label) line(dev.name);
      line(`${formatDateTime(last.at)} (${last.source === "advert" ? "advert" : last.source === "meshtastic" ? "Meshtastic" : "asked"})`);
      line(`${last.lat.toFixed(5)}, ${last.lon.toFixed(5)}${last.alt_m !== null ? ` · ${Math.round(u.alt(last.alt_m))} ${u.altUnit}` : ""}`);
      const prog = progressText(u, dev);
      if (prog) line(prog.text);
      const pl = paceLine(u, dev, now);
      if (pl) line(pl);
      if (dev.battery_v !== null)
        line(`Battery ${dev.battery_pct !== null ? `about ${dev.battery_pct}% (${dev.battery_v.toFixed(2)} V)` : `${dev.battery_v.toFixed(2)} V`}`);
      const rt = routeText(dev);
      if (rt) line(rt);
      const sn = sensorText(u, dev);
      if (sn) line(sn);
      const more = document.createElement("button");
      more.type = "button";
      more.className = "mt-1 text-xs font-medium text-[var(--accent)] underline";
      more.textContent = "Telemetry and history";
      more.onclick = () => setTelemetryFor(dev);
      pop.append(more);
      mk.bindPopup(pop).addTo(markers);
    }
  }, [d, shown, now]);

  const fitAll = () => {
    const m = map.current;
    if (!m) return;
    m.invalidateSize(); // the list beside the map may have just taken its space
    const pts: L.LatLngTuple[] = [];
    for (const dev of shown) if (dev.last_position) pts.push([dev.last_position.lat, dev.last_position.lon]);
    for (const c of visibleCourses) for (const seg of c.segments) for (const p of seg) pts.push(p);
    if (d?.base) pts.push([d.base.lat, d.base.lon]);
    if (pts.length === 1) m.setView(pts[0], 15);
    else if (pts.length > 1) m.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 16 });
  };

  useEffect(() => {
    if (fitted.current || !d || !courses.data) return;
    if (shown.some((x) => x.last_position) || visibleCourses.length || d.base) {
      fitted.current = true;
      fitAll();
    }
  }, [d, courses.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const focus = (dev: TrackedDevice) => {
    const p = dev.last_position;
    if (p && map.current) map.current.setView([p.lat, p.lon], Math.max(map.current.getZoom(), 15));
    setPanel(false);
  };

  const save = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<TrackedDevice> }) =>
      api<TrackedDevice>(`/api/tracking/devices/${id}`, { method: "PATCH", json: body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tracking"] }),
  });
  const poll = useMutation({
    mutationFn: (id: string) => api(`/api/tracking/devices/${id}/poll`, { method: "POST" }),
  });

  const enabled = d?.config.enabled;
  const offlineMissing = d && d.tiles.offline && !usesOffline(d.tiles, d.offline);
  // Meshtastic: trackers broadcast on their own, over a channel shared with the radio.
  const mt = d?.tech === "meshtastic";
  const node = useQuery({
    queryKey: ["meshtastic-node"],
    queryFn: () => api<MtConfig>("/api/node/meshtastic"),
    enabled: mt && !!d?.radio_connected,
    retry: false,
  });
  const publicPrimary = node.data?.channels.find((c) => c.primary && c.key !== "private");

  const listPanel = (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5" role="tablist" aria-label="Tracking lists">
        {(["trackers", "courses"] as const).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cx(
              "min-h-9 flex-1 rounded-lg px-3 text-sm font-medium",
              tab === t ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-2",
            )}
          >
            {t === "trackers" ? `Trackers${devices.length ? ` (${devices.length})` : ""}` : `Courses${courses.data?.length ? ` (${courses.data.length})` : ""}`}
          </button>
        ))}
        <IconButton label="Close the list" className="lg:hidden" onClick={() => setPanel(false)}>
          <X className="size-5" />
        </IconButton>
      </div>
      {/* Bottom padding on phones: the map's attribution sits over the list's last line. */}
      <div className="min-h-0 flex-1 overflow-y-auto pb-6 lg:pb-0">
        {tab === "trackers" ? (
          <TrackerList
            devices={devices}
            now={now}
            u={u}
            onFocus={focus}
            onEdit={setEditing}
            onTelemetry={setTelemetryFor}
            onToggle={(dev) => save.mutate({ id: dev.id, body: { show: !dev.show } })}
            onPoll={(dev) => poll.mutate(dev.id)}
            onAdd={() => setAdding(true)}
            radioConnected={!!d?.radio_connected}
            meshtastic={mt}
            error={save.error ?? poll.error}
          />
        ) : (
          <CourseList courses={courses.data ?? []} error={courses.error} u={u} />
        )}
      </div>
    </div>
  );

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
        <div className="min-w-0 flex-1 px-1">
          <h2 className="text-base font-semibold">Tracking</h2>
          <p className="truncate text-xs text-muted">
            {!d
              ? "Loading…"
              : `${shown.length} of ${devices.length} tracker${devices.length === 1 ? "" : "s"} shown · ${
                  d.radio_connected ? "radio connected" : "radio not connected"
                }${usesOffline(d.tiles, d.offline) ? " · offline map" : ""}`}
          </p>
        </div>
        <Button onClick={fitAll} disabled={!d} title="Fit everything in view">
          <Maximize className="size-4" aria-hidden />
          <span className="hidden sm:inline">Fit</span>
        </Button>
        <Button className="lg:hidden" onClick={() => setPanel((p) => !p)} aria-expanded={panel} disabled={!enabled}>
          <List className="size-4" aria-hidden />
          <span className="hidden sm:inline">List</span>
        </Button>
      </header>

      {d && !enabled && (
        <p className="border-b border-line bg-surface px-4 py-3 text-sm text-muted">
          Tracking is off.{" "}
          <Link to="/settings/tracking" className="text-accent underline">
            Turn it on in Settings → Tracking
          </Link>{" "}
          to add trackers.
        </p>
      )}
      {d && enabled && !d.radio_connected && (
        <p role="status" className="border-b border-line bg-warn/10 px-4 py-2 text-sm text-warn">
          {mt
            ? "The radio isn't connected, so trackers' positions aren't being heard."
            : "The radio isn't connected, so trackers aren't being asked for their position."}
        </p>
      )}
      {d && enabled && mt && publicPrimary && (
        <p role="status" className="border-b border-line bg-warn/10 px-4 py-2 text-sm text-warn">
          The radio's primary channel uses Meshtastic's default key: positions arrive approximate, and anyone nearby can follow
          them.{" "}
          <Link to="/settings/node" className="underline">
            Give it a private key
          </Link>
        </p>
      )}
      {offlineMissing && (
        <p role="status" className="border-b border-line bg-warn/10 px-4 py-2 text-sm text-warn">
          The offline map is turned on but none is saved.{" "}
          <Link to="/settings/map" className="underline">
            Download one in Settings → Map
          </Link>
          .
        </p>
      )}

      <div className="relative flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          <div ref={container} className="absolute inset-0" role="region" aria-label="Map of tracked devices" />
          {/* At a glance: every shown tracker and how fresh its position is. Tap to centre it. */}
          {shown.length > 0 && !panel && (
            <div className="pointer-events-none absolute inset-x-0 bottom-0 z-[500] flex flex-wrap justify-center gap-2 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
              {shown.map((dev) => {
                const st = statusLine(dev, now);
                const prog = progressText(u, dev);
                const pl = paceLine(u, dev, now);
                const bat = batteryText(dev);
                return (
                  <div
                    key={dev.id}
                    className="pointer-events-auto flex min-h-12 max-w-full items-stretch rounded-xl border border-line bg-surface/95 shadow-lg backdrop-blur"
                  >
                  <button
                    type="button"
                    onClick={() => focus(dev)}
                    className="flex min-w-0 items-center gap-2.5 rounded-l-xl px-3 py-1.5 text-left hover:bg-surface-2/60"
                    title="Centre the map on it"
                  >
                    <span aria-hidden className={cx("size-3 shrink-0 rounded-full", isStale(dev, now) && "opacity-50")} style={{ background: dev.color }} />
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-semibold">{displayName(dev)}</span>
                      <span className={cx("block truncate text-xs", TONE_CLASS[st.tone])}>{st.text}</span>
                      {prog && <span className={cx("block truncate text-xs", TONE_CLASS[prog.tone])}>{prog.text}</span>}
                      {pl && <span className="block truncate text-xs text-muted">{pl}</span>}
                    </span>
                  </button>
                  {/* Its telemetry, one tap away (the list is hidden on phones). */}
                  <button
                    type="button"
                    onClick={() => setTelemetryFor(dev)}
                    className="flex shrink-0 flex-col items-center justify-center gap-0.5 rounded-r-xl border-l border-line px-2.5 text-xs hover:bg-surface-2/60"
                    aria-label={`Telemetry for ${displayName(dev)}${bat ? `, battery ${bat}` : ""}`}
                    title="Battery, sensors, signal and their history"
                  >
                    {bat ? (
                      <span className={cx("flex items-center gap-0.5", dev.battery_low ? "font-semibold text-danger" : "text-muted")}>
                        <BatteryIcon d={dev} />
                        {bat}
                      </span>
                    ) : (
                      <Activity className="size-4 text-muted" aria-hidden />
                    )}
                    <span className="font-medium text-accent">Telemetry</span>
                  </button>
                  </div>
                );
              })}
            </div>
          )}
          {d && enabled && devices.length === 0 && !panel && (
            <div className="pointer-events-none absolute inset-x-0 top-6 z-[500] flex justify-center px-4">
              <div className="pointer-events-auto max-w-md rounded-xl border border-line bg-surface px-4 py-3 text-center text-sm text-muted shadow-lg">
                <p>
                  {mt
                    ? "No trackers yet. Add a Meshtastic tracker the radio has heard: every position it broadcasts is recorded."
                    : "No trackers yet. Add the contact of a MeshCore GPS tracker: MeshHome asks it for its position on a schedule."}
                </p>
                <Button variant="primary" className="mt-3" onClick={() => setAdding(true)}>
                  <Plus className="size-4" aria-hidden /> Add a tracker
                </Button>
              </div>
            </div>
          )}
        </div>
        {/* The list: beside the map on wide screens, over its lower part on phones. */}
        {enabled && (
          <aside
            aria-label="Trackers and courses"
            className={cx(
              "z-[600] flex-col border-line bg-surface",
              "lg:static lg:flex lg:w-80 lg:border-l",
              panel ? "absolute inset-x-0 bottom-0 flex max-h-[65%] rounded-t-2xl border-t shadow-2xl" : "hidden",
            )}
          >
            {listPanel}
          </aside>
        )}
      </div>

      {adding && d && <AddTrackerDialog data={d} onClose={() => setAdding(false)} />}
      {telemetryFor && (
        <TelemetryDialog
          dev={devices.find((x) => x.id === telemetryFor.id) ?? telemetryFor}
          u={u}
          name={displayName(telemetryFor)}
          onClose={() => setTelemetryFor(null)}
        />
      )}
      {editing && d && (
        <EditTrackerDialog
          device={devices.find((x) => x.id === editing.id) ?? editing}
          intervals={d.intervals}
          courses={courses.data ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

// ---- trackers --------------------------------------------------------------------------------

function TrackerList({
  devices,
  now,
  u,
  onFocus,
  onEdit,
  onTelemetry,
  onToggle,
  onPoll,
  onAdd,
  radioConnected,
  meshtastic,
  error,
}: {
  devices: TrackedDevice[];
  now: number;
  u: U;
  onFocus: (d: TrackedDevice) => void;
  onEdit: (d: TrackedDevice) => void;
  onTelemetry: (d: TrackedDevice) => void;
  onToggle: (d: TrackedDevice) => void;
  onPoll: (d: TrackedDevice) => void;
  onAdd: () => void;
  radioConnected: boolean;
  meshtastic: boolean;
  error: unknown;
}) {
  return (
    <div className="space-y-3 p-3">
      <ErrorText error={error} />
      {devices.length === 0 && <p className="text-sm text-muted">No trackers yet.</p>}
      <ul className="space-y-2">
        {devices.map((dev) => {
          const st = statusLine(dev, now);
          return (
            <li key={dev.id} className="rounded-xl border border-line p-2.5">
              <div className="flex items-start gap-2">
                <button
                  type="button"
                  onClick={() => onFocus(dev)}
                  disabled={!dev.last_position}
                  className="flex min-w-0 flex-1 items-start gap-2.5 rounded-lg text-left disabled:cursor-default"
                  title={dev.last_position ? "Centre the map on it" : undefined}
                >
                  <span aria-hidden className="mt-1 size-3 shrink-0 rounded-full" style={{ background: dev.color }} />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold">{displayName(dev)}</span>
                    {dev.label && <span className="block truncate text-xs text-muted">{dev.name}</span>}
                    <span className={cx("block text-xs", TONE_CLASS[st.tone])}>{st.text}</span>
                    {progressText(u, dev) && (
                      <span className={cx("block text-xs", TONE_CLASS[progressText(u, dev)!.tone])}>{progressText(u, dev)!.text}</span>
                    )}
                    {paceLine(u, dev, now) && <span className="block text-xs text-muted">{paceLine(u, dev, now)}</span>}
                    {linkText(dev) && <span className="block text-xs text-muted" title={routeText(dev) ?? undefined}>{linkText(dev)}</span>}
                    <TelemetryChips dev={dev} u={u} />
                    <span className="mt-1 block text-xs text-muted">
                      {dev.source === "meshtastic" ? `Meshtastic ${dev.node_id} · broadcasts about every ${intervalLabel(dev.interval_s)}` : `Every ${intervalLabel(dev.interval_s)}`}
                      {dev.is_simulated && " · simulated"}
                    </span>
                  </span>
                </button>
                <IconButton label={dev.show ? "Hide from the map" : "Show on the map"} onClick={() => onToggle(dev)} aria-pressed={dev.show}>
                  {dev.show ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
                </IconButton>
              </div>
              {(dev.status === "no_answer" || dev.status === "no_position" || dev.status === "error") && dev.status_detail && (
                <p className="mt-1.5 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs text-muted">{dev.status_detail}</p>
              )}
              <div className="mt-2 grid grid-cols-3 gap-1.5">
                <Button
                  className="min-h-9 gap-1.5 whitespace-nowrap px-2"
                  onClick={() => onPoll(dev)}
                  disabled={(dev.source === "meshcore" && !radioConnected) || dev.asking || !dev.current_radio}
                  title={dev.source === "meshtastic" ? "Ask for its position now (a Meshtastic tracker answers at most every 3 minutes)" : "Ask for its position now"}
                >
                  <RefreshCw className={cx("size-4", dev.asking && "animate-spin")} aria-hidden /> Ask now
                </Button>
                <Button className="min-h-9 gap-1.5 whitespace-nowrap px-2" onClick={() => onTelemetry(dev)} title="Battery, sensors, signal and their history">
                  <Activity className="size-4" aria-hidden /> Telemetry
                </Button>
                <Button className="min-h-9 gap-1.5 whitespace-nowrap px-2" onClick={() => onEdit(dev)}>
                  <Pencil className="size-4" aria-hidden /> Edit
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
      <Button variant="primary" className="w-full" onClick={onAdd}>
        <Plus className="size-4" aria-hidden /> Add a tracker
      </Button>
      {meshtastic ? (
        <p className="text-xs text-muted">
          Trackers need to be on the same channel as this radio: scan the channel's QR code (Settings → Radio → Configure node
          settings → Channels) into each one.
        </p>
      ) : (
        <p className="text-xs text-muted">
          Trackers must share their location with this node: in the MeshCore app on the tracker, turn GPS on and set Telemetry →
          Location to allow this node (or everyone). It also needs this node in its contacts.
        </p>
      )}
    </div>
  );
}

function AddTrackerDialog({ data, onClose }: { data: TrackingData; onClose: () => void }) {
  const qc = useQueryClient();
  const kind = data.tech;
  const [q, setQ] = useState("");
  const [label, setLabel] = useState("");
  const [interval, setIntervalS] = useState(60);
  const contacts = useQuery({
    queryKey: ["contacts", "tracking-candidates", q],
    queryFn: () => api<ContactPage>(`/api/contacts?kind=1&page_size=25&sort=last_heard&q=${encodeURIComponent(q)}`),
    enabled: kind === "meshcore",
  });
  const tracked = new Set(data.devices.map((d) => d.contact_id));
  const add = useMutation({
    mutationFn: (body: { contact_id: string } | { node_num: number }) =>
      api<TrackedDevice>("/api/tracking/devices", { json: { ...body, label: label.trim() || null, interval_s: interval } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tracking"] });
      onClose();
    },
  });
  const items = (contacts.data?.items ?? []).filter((c) => !tracked.has(c.id));
  return (
    <Dialog title="Add a tracker" onClose={onClose} footer={<Button onClick={onClose}>Cancel</Button>}>
      <div className="space-y-4">
        <div className={cx("grid gap-3", kind === "meshcore" && "sm:grid-cols-[1fr_9rem]")}>
          <Field label="Label (optional)" htmlFor="trk-label" hint="Shown instead of its name, e.g. who's carrying it.">
            <Input id="trk-label" value={label} maxLength={64} placeholder="Leg 1 – Sam" onChange={(e) => setLabel(e.target.value)} />
          </Field>
          {kind === "meshcore" && (
            <Field label="Ask every" htmlFor="trk-interval">
              <Select
                id="trk-interval"
                value={interval}
                options={data.intervals.map((s) => ({ value: s, label: intervalLabel(s) }))}
                onChange={setIntervalS}
              />
            </Field>
          )}
        </div>
        {kind === "meshtastic" ? (
          <>
            <ErrorText error={add.error} />
            <MeshNodePicker onPick={(n) => add.mutate({ node_num: n.num })} busy={add.isPending} />
          </>
        ) : (
          <>
        <Field label="Contact" htmlFor="trk-search" hint="Companions on this radio, most recently heard first.">
          <Input id="trk-search" type="search" value={q} placeholder="Search by name" onChange={(e) => setQ(e.target.value)} />
        </Field>
        <ErrorText error={add.error ?? contacts.error} />
        <ul className="max-h-72 divide-y divide-line overflow-y-auto rounded-xl border border-line">
          {items.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                disabled={add.isPending}
                onClick={() => add.mutate({ contact_id: c.id })}
                className="flex min-h-12 w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface-2 disabled:opacity-50"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{c.alias || c.name || c.public_key.slice(0, 8)}</span>
                  <span className="block truncate text-xs text-muted">
                    {c.last_advert_at ? `Heard ${ago(c.last_advert_at, Date.now())}` : "Never heard"}
                    {c.is_simulated && " · simulated"}
                  </span>
                </span>
                <Plus className="size-4 shrink-0 text-muted" aria-hidden />
              </button>
            </li>
          ))}
          {contacts.data && items.length === 0 && <li className="px-3 py-3 text-sm text-muted">No companions to add.</li>}
        </ul>
          </>
        )}
      </div>
    </Dialog>
  );
}

/** Meshtastic nodes the Meshtastic radio has heard, trackers first, to add one. */
function MeshNodePicker({ onPick, busy }: { onPick: (n: MeshNode) => void; busy: boolean }) {
  const [q, setQ] = useState("");
  const nodes = useQuery({
    queryKey: ["meshtastic-nodes"],
    queryFn: () => api<MeshNode[]>("/api/tracking/meshtastic/nodes"),
  });
  const needle = q.trim().toLowerCase();
  const items = (nodes.data ?? [])
    .filter((n) => !n.tracked && !n.is_self)
    .filter((n) => !needle || [n.long_name, n.short_name, n.node_id].some((v) => v?.toLowerCase().includes(needle)))
    .sort((a, b) => Number(b.role === "TRACKER") - Number(a.role === "TRACKER"));
  return (
    <>
      <Field label="Meshtastic node" htmlFor="trk-mt-search" hint="Heard by the Meshtastic radio, most recently heard first.">
        <Input id="trk-mt-search" type="search" value={q} placeholder="Search by name or !id" onChange={(e) => setQ(e.target.value)} />
      </Field>
      <ErrorText error={nodes.error} />
      <ul className="max-h-72 divide-y divide-line overflow-y-auto rounded-xl border border-line">
        {items.map((n) => (
          <li key={n.num}>
            <button
              type="button"
              disabled={busy}
              onClick={() => onPick(n)}
              className="flex min-h-12 w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface-2 disabled:opacity-50"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{n.long_name || n.short_name || n.node_id}</span>
                <span className="block truncate text-xs text-muted">
                  {n.node_id}
                  {n.role && n.role !== "CLIENT" ? ` · ${n.role.toLowerCase().replace(/_/g, " ")}` : ""}
                  {` · ${n.last_heard_at ? `heard ${ago(n.last_heard_at, Date.now())}` : "never heard"}`}
                  {n.has_position ? " · has a position" : ""}
                  {n.simulated && " · simulated"}
                </span>
              </span>
              <Plus className="size-4 shrink-0 text-muted" aria-hidden />
            </button>
          </li>
        ))}
        {nodes.data && items.length === 0 && (
          <li className="px-3 py-3 text-sm text-muted">
            No nodes to add yet. A tracker shows up here once the Meshtastic radio hears it (on the same channel).
          </li>
        )}
      </ul>
    </>
  );
}

function EditTrackerDialog({
  device,
  intervals,
  courses,
  onClose,
}: {
  device: TrackedDevice;
  intervals: number[];
  courses: Course[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [label, setLabel] = useState(device.label ?? "");
  const [color, setColor] = useState(device.color);
  const [interval, setIntervalS] = useState(device.interval_s);
  const [polling, setPolling] = useState(device.polling);
  const [courseId, setCourseId] = useState(device.course_id ?? "");
  const [confirm, setConfirm] = useState<"clear" | "remove" | null>(null);
  const done = () => qc.invalidateQueries({ queryKey: ["tracking"] });
  const save = useMutation({
    mutationFn: () =>
      api(`/api/tracking/devices/${device.id}`, {
        method: "PATCH",
        json:
          device.source === "meshcore"
            ? { label: label.trim() || null, color, interval_s: interval, polling, course_id: courseId || null }
            : { label: label.trim() || null, color, course_id: courseId || null },
      }),
    onSuccess: () => {
      done();
      onClose();
    },
  });
  const clear = useMutation({
    mutationFn: () => api(`/api/tracking/devices/${device.id}/positions`, { method: "DELETE" }),
    onSuccess: () => {
      done();
      setConfirm(null);
    },
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/tracking/devices/${device.id}`, { method: "DELETE" }),
    onSuccess: () => {
      done();
      onClose();
    },
  });
  return (
    <Dialog
      title={displayName(device)}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-[1fr_5rem]">
          <Field label="Label" htmlFor="edit-label" hint={`Empty shows its name, ${device.name}.`}>
            <Input id="edit-label" value={label} maxLength={64} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field label="Colour" htmlFor="edit-color">
            <input
              id="edit-color"
              type="color"
              value={color}
              onChange={(e) => setColor(e.target.value)}
              className="h-11 w-full cursor-pointer rounded-lg border border-line bg-surface p-1"
            />
          </Field>
        </div>
        <Field
          label="Course"
          htmlFor="edit-course"
          hint="For progress and ETA. At a handoff, choose the loop the next runner is on. Automatic picks the course its position is on, which can be wrong where loops share trail."
        >
          <Select
            id="edit-course"
            value={courseId}
            options={[{ value: "", label: "Automatic" }, ...courses.map((c) => ({ value: c.id, label: c.name }))]}
            onChange={setCourseId}
          />
        </Field>
        {device.source === "meshcore" ? (
          <>
            <Field label="Ask for its position every" htmlFor="edit-interval">
              <Select id="edit-interval" value={interval} options={intervals.map((s) => ({ value: s, label: intervalLabel(s) }))} onChange={setIntervalS} />
            </Field>
            <label className="flex cursor-pointer items-start gap-3">
              <input type="checkbox" checked={polling} onChange={(e) => setPolling(e.target.checked)} className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]" />
              <span>
                <span className="block text-sm font-medium">Ask for its position</span>
                <span className="block text-xs text-muted">Off: no requests are sent; positions from its adverts are still recorded.</span>
              </span>
            </label>
          </>
        ) : (
          <p className="text-xs text-muted">
            Meshtastic node {device.node_id}. It broadcasts its position on its own (set how often on the tracker, in the
            Meshtastic app: Position → broadcast interval and smart position).
          </p>
        )}
        <ErrorText error={save.error ?? clear.error ?? remove.error} />
        <div className="flex flex-wrap gap-2 border-t border-line pt-4">
          <a
            href={`/api/tracking/devices/${device.id}/track.gpx?hours=48`}
            download
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-line px-4 text-sm font-medium hover:bg-surface-2"
          >
            <Download className="size-4" aria-hidden /> Track (GPX, 48 h)
          </a>
          {confirm === "clear" ? (
            <Button variant="danger" onClick={() => clear.mutate()} disabled={clear.isPending}>
              Delete its history?
            </Button>
          ) : (
            <Button onClick={() => setConfirm("clear")}>Clear history</Button>
          )}
          {confirm === "remove" ? (
            <Button variant="danger" onClick={() => remove.mutate()} disabled={remove.isPending}>
              <Trash2 className="size-4" aria-hidden /> Stop tracking and delete?
            </Button>
          ) : (
            <Button variant="danger" onClick={() => setConfirm("remove")}>
              <Trash2 className="size-4" aria-hidden /> Remove
            </Button>
          )}
        </div>
        {device.last_reply_at && <p className="text-xs text-muted">Last answered {formatDateTime(device.last_reply_at)}.</p>}
      </div>
    </Dialog>
  );
}

// ---- courses ---------------------------------------------------------------------------------

function CourseList({ courses, error, u }: { courses: Course[]; error: unknown; u: U }) {
  const qc = useQueryClient();
  const file = useRef<HTMLInputElement>(null);
  const [renaming, setRenaming] = useState<Course | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["tracking-courses"] });
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      for (const f of files) {
        await api<Course>(`/api/tracking/courses?filename=${encodeURIComponent(f.name)}`, {
          method: "POST",
          body: f,
          headers: { "Content-Type": "application/gpx+xml" },
        });
      }
    },
    onSettled: refresh,
  });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Course> }) =>
      api<Course>(`/api/tracking/courses/${id}`, { method: "PATCH", json: body }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/tracking/courses/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setRemoving(null);
      refresh();
    },
  });
  return (
    <div className="space-y-3 p-3">
      <ErrorText error={error ?? upload.error ?? patch.error ?? remove.error} />
      {courses.length === 0 && <p className="text-sm text-muted">No courses yet. Upload the course's GPX files to draw them on the map.</p>}
      <ul className="space-y-2">
        {courses.map((c) => (
          <li key={c.id} className="flex items-center gap-2 rounded-xl border border-line p-2">
            <input
              type="color"
              value={c.color}
              aria-label={`Colour of ${c.name}`}
              onChange={(e) => patch.mutate({ id: c.id, body: { color: e.target.value } })}
              className="size-9 shrink-0 cursor-pointer rounded-md border border-line bg-surface p-0.5"
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-semibold">{c.name}</span>
              <span className="block truncate text-xs text-muted">
                {distance(u, c.distance_m)}
                {c.waypoints.length > 0 && ` · ${c.waypoints.length} point${c.waypoints.length === 1 ? "" : "s"}`}
              </span>
            </span>
            <IconButton
              label={c.visible ? "Hide from the map" : "Show on the map"}
              aria-pressed={c.visible}
              onClick={() => patch.mutate({ id: c.id, body: { visible: !c.visible } })}
            >
              {c.visible ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
            </IconButton>
            <IconButton label="Rename" onClick={() => setRenaming(c)}>
              <Pencil className="size-4" />
            </IconButton>
            {removing === c.id ? (
              <Button variant="danger" className="min-h-9 px-2" onClick={() => remove.mutate(c.id)} disabled={remove.isPending}>
                Delete?
              </Button>
            ) : (
              <IconButton label="Delete" onClick={() => setRemoving(c.id)}>
                <Trash2 className="size-4" />
              </IconButton>
            )}
          </li>
        ))}
      </ul>
      <input
        ref={file}
        type="file"
        accept=".gpx,application/gpx+xml"
        multiple
        className="sr-only"
        tabIndex={-1}
        onChange={(e) => {
          const files = [...(e.target.files ?? [])];
          e.target.value = "";
          if (files.length) upload.mutate(files);
        }}
      />
      <Button variant="primary" className="w-full" onClick={() => file.current?.click()} disabled={upload.isPending}>
        <Upload className="size-4" aria-hidden /> {upload.isPending ? "Uploading…" : "Upload GPX files"}
      </Button>
      <p className="text-xs text-muted">
        Tracks, routes and waypoints are drawn. A colour in the name ("Green loop") is used for the line. The first visible course
        is also what the simulated tracker runs.
      </p>
      {renaming && (
        <RenameCourseDialog
          course={renaming}
          u={u}
          onClose={() => setRenaming(null)}
          onSave={(name) => patch.mutate({ id: renaming.id, body: { name } }, { onSuccess: () => setRenaming(null) })}
          saving={patch.isPending}
        />
      )}
    </div>
  );
}

function RenameCourseDialog({
  course,
  u,
  onClose,
  onSave,
  saving,
}: {
  course: Course;
  u: U;
  onClose: () => void;
  onSave: (name: string) => void;
  saving: boolean;
}) {
  const [name, setName] = useState(course.name);
  return (
    <Dialog
      title="Rename course"
      size="sm"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!name.trim() || saving} onClick={() => onSave(name.trim())}>
            Save
          </Button>
        </>
      }
    >
      <Field label="Name" htmlFor="course-name">
        <Input id="course-name" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
      </Field>
      <p className="mt-2">
        <Badge>{distance(u, course.distance_m)}</Badge>
      </p>
    </Dialog>
  );
}
