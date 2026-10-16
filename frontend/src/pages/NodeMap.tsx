import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { ChevronLeft, Maximize } from "lucide-react";
import { api, type MapData, type MapNode } from "../lib/api";
import { Button } from "../components/ui";
import { cx, formatDateTime } from "../lib/util";

// MeshCore advert types. Colours are chosen to stay distinguishable on light and dark tiles.
const KINDS: Record<number, { label: string; color: string; glyph: string }> = {
  1: { label: "Companions", color: "#2563eb", glyph: "C" },
  2: { label: "Repeaters", color: "#d97706", glyph: "R" },
  3: { label: "Room servers", color: "#7c3aed", glyph: "S" },
  4: { label: "Sensors", color: "#0891b2", glyph: "W" },
};
const OTHER = { label: "Other", color: "#64748b", glyph: "?" };
const GATEWAY_COLOR = "#0f766e";
const kindOf = (k: number) => KINDS[k] ?? OTHER;

type Age = "all" | "1d" | "7d" | "30d";
const AGE_SECONDS: Record<Age, number> = { all: Infinity, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
const STALE_SECONDS = 7 * 86400;

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function markerIcon(color: string, glyph: string, opts: { gateway?: boolean; stale?: boolean } = {}) {
  const size = opts.gateway ? 30 : 24;
  // Only constant values are interpolated here; node names never enter HTML strings.
  return L.divIcon({
    className: "",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -size / 2],
    html: `<span class="mc-marker${opts.gateway ? " is-gateway" : ""}${opts.stale ? " is-stale" : ""}" style="--c:${color}">${glyph}</span>`,
  });
}

/** Popup content built with DOM APIs: names and keys come from the radio and are untrusted text. */
function nodePopup(n: MapNode, onMessage: (n: MapNode) => void): HTMLElement {
  const el = document.createElement("div");
  el.className = "space-y-1 text-sm min-w-48";
  const title = document.createElement("div");
  title.className = "font-semibold";
  title.textContent = n.alias || n.name || "Unnamed";
  el.append(title);
  const line = (text: string, cls = "text-xs text-[var(--muted)]") => {
    const d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    el.append(d);
  };
  line([kindOf(n.kind).label.replace(/s$/, ""), n.is_simulated ? "simulated" : "", n.on_radio ? "" : "not on radio"].filter(Boolean).join(" · "));
  if (n.alias && n.name) line(`Advertised as ${n.name}`);
  line(`Last advert: ${formatDateTime(n.last_advert_at)}`);
  line(`${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}`);
  line(`Key ${n.public_key.slice(0, 12)}…`, "font-mono text-[11px] text-[var(--muted)]");
  if (n.kind === 1) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "mt-2 inline-flex min-h-9 items-center rounded-lg bg-[var(--accent)] px-3 text-xs font-medium text-[var(--accent-fg)]";
    b.textContent = "Message";
    b.addEventListener("click", () => onMessage(n));
    el.append(b);
  }
  return el;
}

export function NodeMap() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const data = useQuery({ queryKey: ["map"], queryFn: () => api<MapData>("/api/map"), refetchInterval: 60_000 });
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const tiles = useRef<L.TileLayer | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const fitted = useRef(false);
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const [age, setAge] = useState<Age>("all");

  const now = Date.now() / 1000;
  const visible = useMemo(
    () =>
      (data.data?.nodes ?? []).filter((n) => {
        if (hidden.has(n.kind in KINDS ? n.kind : -1)) return false;
        const t = n.last_advert_at ? new Date(n.last_advert_at).getTime() / 1000 : 0;
        return age === "all" || now - t <= AGE_SECONDS[age];
      }),
    [data.data, hidden, age],
  );

  const openDm = async (n: MapNode) => {
    let id = n.conversation_id;
    if (!id) {
      id = (await api<{ conversation_id: string }>(`/api/contacts/${n.id}/conversation`, { method: "POST" })).conversation_id;
      await qc.invalidateQueries({ queryKey: ["conversations"] });
    }
    navigate(`/c/${id}`);
  };

  // Create the map once.
  useEffect(() => {
    if (!container.current || map.current) return;
    const m = L.map(container.current, { zoomControl: true, worldCopyJump: true }).setView([20, 0], 2);
    layer.current = L.layerGroup().addTo(m);
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

  // Tile source (configurable in Settings → Map).
  const cfg = data.data?.tiles;
  useEffect(() => {
    const m = map.current;
    if (!m || !cfg) return;
    tiles.current?.remove();
    const isOsm = cfg.tile_url.includes("openstreetmap.org");
    tiles.current = L.tileLayer(cfg.tile_url, {
      maxZoom: cfg.max_zoom,
      attribution: isOsm
        ? '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
        : escapeHtml(cfg.attribution),
      // The app sends no Referer by default; tile servers (OSM's policy) expect one, so send the origin only.
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(m);
  }, [cfg?.tile_url, cfg?.attribution, cfg?.max_zoom]);

  const fitAll = () => {
    const m = map.current;
    const d = data.data;
    if (!m || !d) return;
    const pts: L.LatLngTuple[] = [...visible.map((n) => [n.lat, n.lon] as L.LatLngTuple), ...d.gateways.map((g) => [g.lat, g.lon] as L.LatLngTuple)];
    if (pts.length === 1) m.setView(pts[0], 13);
    else if (pts.length > 1) m.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 15 });
  };

  // Markers.
  useEffect(() => {
    const g = layer.current;
    const d = data.data;
    if (!g || !d) return;
    g.clearLayers();
    for (const gw of d.gateways) {
      const mk = L.marker([gw.lat, gw.lon], {
        icon: markerIcon(GATEWAY_COLOR, "⌂", { gateway: true }),
        title: gw.name,
        zIndexOffset: 1000,
        keyboard: true,
      });
      const el = document.createElement("div");
      el.className = "text-sm";
      const t = document.createElement("div");
      t.className = "font-semibold";
      t.textContent = gw.name;
      const s = document.createElement("div");
      s.className = "text-xs text-[var(--muted)]";
      s.textContent = `This app's gateway${gw.is_simulated ? " (simulated)" : ""}${gw.live ? " · connected" : ""}`;
      el.append(t, s);
      mk.bindPopup(el).addTo(g);
    }
    for (const n of visible) {
      const k = kindOf(n.kind);
      const t = n.last_advert_at ? new Date(n.last_advert_at).getTime() / 1000 : 0;
      L.marker([n.lat, n.lon], {
        icon: markerIcon(k.color, k.glyph, { stale: Date.now() / 1000 - t > STALE_SECONDS }),
        title: n.alias || n.name,
        alt: `${k.label.replace(/s$/, "")}: ${n.alias || n.name}`,
        keyboard: true,
      })
        .bindPopup(nodePopup(n, openDm))
        .addTo(g);
    }
    if (!fitted.current && (visible.length || d.gateways.length)) {
      fitted.current = true;
      fitAll();
    }
  }, [visible, data.data]);

  const counts = useMemo(() => {
    const c: Record<number, number> = {};
    for (const n of data.data?.nodes ?? []) c[n.kind in KINDS ? n.kind : -1] = (c[n.kind in KINDS ? n.kind : -1] ?? 0) + 1;
    return c;
  }, [data.data]);

  const toggle = (k: number) =>
    setHidden((h) => {
      const next = new Set(h);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const d = data.data;
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
        <div className="min-w-0 flex-1 px-1">
          <h2 className="text-base font-semibold">Node map</h2>
          <p className="truncate text-xs text-muted">
            {d
              ? `${visible.length} of ${d.nodes.length} located node${d.nodes.length === 1 ? "" : "s"} shown${
                  d.without_location ? ` · ${d.without_location} share no location` : ""
                }`
              : "Loading…"}
          </p>
        </div>
        <Button onClick={fitAll} disabled={!d} title="Fit all nodes in view">
          <Maximize className="size-4" aria-hidden />
          <span className="hidden sm:inline">Fit all</span>
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-1.5 border-b border-line bg-surface px-3 py-2" role="group" aria-label="Filter nodes">
        {[...Object.entries(KINDS).map(([k, v]) => [Number(k), v] as const), [-1, OTHER] as const]
          .filter(([k]) => counts[k])
          .map(([k, v]) => (
            <button
              key={k}
              type="button"
              aria-pressed={!hidden.has(k)}
              onClick={() => toggle(k)}
              className={cx(
                "inline-flex min-h-8 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors",
                hidden.has(k) ? "border-line text-muted line-through" : "border-line bg-surface-2 text-ink",
              )}
            >
              <span aria-hidden className="size-2.5 rounded-full" style={{ background: v.color }} />
              {v.label} <span className="text-muted">{counts[k]}</span>
            </button>
          ))}
        <span className="inline-flex items-center gap-1.5 px-2 text-xs text-muted">
          <span aria-hidden className="size-2.5 rounded-sm" style={{ background: GATEWAY_COLOR }} /> Gateway
        </span>
        <label className="ml-auto flex items-center gap-2 text-xs text-muted">
          Heard within
          <select
            value={age}
            onChange={(e) => setAge(e.target.value as Age)}
            className="min-h-8 rounded-md border border-line bg-surface px-2 text-xs text-ink"
          >
            <option value="all">Any time</option>
            <option value="1d">24 hours</option>
            <option value="7d">7 days</option>
            <option value="30d">30 days</option>
          </select>
        </label>
      </div>

      <div className="relative min-h-0 flex-1">
        <div ref={container} className="absolute inset-0" role="region" aria-label="Map of mesh nodes" />
        {d && d.nodes.length === 0 && d.gateways.length === 0 && (
          <div className="pointer-events-none absolute inset-x-0 top-6 z-[500] flex justify-center px-4">
            <p className="pointer-events-auto max-w-md rounded-xl border border-line bg-surface px-4 py-3 text-center text-sm text-muted shadow-lg">
              No positions yet. Nodes appear here when their adverts include a location, which is optional in
              MeshCore.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
