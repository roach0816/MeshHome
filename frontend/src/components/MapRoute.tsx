import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Activity, Eraser, Undo2 } from "lucide-react";
import { api, type MapData, type MapNode } from "../lib/api";
import { AGE_LABELS, AGE_SECONDS, GATEWAY_COLOR, KINDS, STALE_SECONDS, heardAgo, markerIcon, type Age } from "../pages/NodeMap";
import { Button, ErrorText } from "./ui";
import { Dialog } from "./Dialog";
import { cx } from "../lib/util";

// "Set path → On the map": like the MeshCore app's Trace Path (Using Map). Tap the repeaters in the
// order a message should take, test the route with a trace probe (each repeater reports the signal
// it heard, out and back), and use it as the contact's path.

interface TraceResult {
  hops: { hash: string; snr: number | null; names: string[] }[];
  final_snr: number | null;
  elapsed_ms: number;
}

const PICKABLE = new Set([2, 3]); // repeaters and room servers relay messages

function snrTone(snr: number | null) {
  if (snr == null) return "text-muted";
  if (snr >= 5) return "text-ok";
  if (snr >= -5) return "text-warn";
  return "text-danger";
}

function numberedIcon(n: number) {
  return L.divIcon({
    className: "",
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    html: `<span class="mc-marker" style="--c:var(--accent)">${n}</span>`,
  });
}

export function MapRoute({
  targetKey,
  targetName,
  onUse,
  onClose,
}: {
  targetKey: string;
  targetName: string;
  /** The chosen repeaters' full public keys, in order. */
  onUse: (keys: string[]) => void;
  onClose: () => void;
}) {
  const data = useQuery({ queryKey: ["map"], queryFn: () => api<MapData>("/api/map") });
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const fitted = useRef(false);
  const [route, setRoute] = useState<MapNode[]>([]);
  // Repeaters that have gone quiet are unlikely to relay, so older ones are hidden by default.
  const [age, setAge] = useState<Age>("7d");
  const trace = useMutation({
    mutationFn: (keys: string[]) => api<TraceResult>("/api/radio/trace", { json: { hops: keys } }),
  });

  const allRelays = useMemo(() => (data.data?.nodes ?? []).filter((n) => PICKABLE.has(n.kind)), [data.data]);
  // Hops already on the route stay on the map, so the route line never points at a hidden marker.
  const relays = useMemo(
    () =>
      allRelays.filter(
        (n) => age === "all" || heardAgo(n.last_advert_at) <= AGE_SECONDS[age] || route.some((r) => r.id === n.id),
      ),
    [allRelays, age, route],
  );
  const tooOld = allRelays.length - relays.length;
  const target = data.data?.nodes.find((n) => n.public_key === targetKey);
  const home = data.data?.gateways[0];

  const add = (n: MapNode) => {
    trace.reset();
    setRoute((r) => (r.length && r[r.length - 1].id === n.id ? r : [...r, n]));
  };

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
    };
  }, []);

  const cfg = data.data?.tiles;
  useEffect(() => {
    const m = map.current;
    if (!m || !cfg) return;
    const t = L.tileLayer(cfg.tile_url, {
      maxZoom: cfg.max_zoom,
      attribution: cfg.tile_url.includes("openstreetmap.org") ? "&copy; OpenStreetMap contributors" : "",
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(m);
    return () => {
      t.remove();
    };
  }, [cfg?.tile_url, cfg?.max_zoom]);

  // Markers and the route line.
  useEffect(() => {
    const g = layer.current;
    const m = map.current;
    const d = data.data;
    if (!g || !m || !d) return;
    g.clearLayers();
    const order = new Map(route.map((n, i) => [n.id, i + 1]));
    // SVG attributes don't resolve CSS variables, so the theme's accent is read here.
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0f766e";
    const pts: L.LatLngTuple[] = [];
    if (home) {
      L.marker([home.lat, home.lon], { icon: markerIcon(GATEWAY_COLOR, "⌂", { gateway: true }), title: `${home.name} (you)`, zIndexOffset: 1000 }).addTo(g);
      pts.push([home.lat, home.lon]);
    }
    for (const n of relays) {
      const k = KINDS[n.kind];
      const pos = order.get(n.id);
      L.marker([n.lat, n.lon], {
        icon: pos ? numberedIcon(pos) : markerIcon(k.color, k.glyph, { stale: heardAgo(n.last_advert_at) > STALE_SECONDS }),
        title: `${n.alias || n.name}: tap to add as the next hop`,
        alt: `${k.label.replace(/s$/, "")}: ${n.alias || n.name}`,
        keyboard: true,
        zIndexOffset: pos ? 500 : 0,
      })
        .on("click", () => add(n))
        .addTo(g);
      pts.push([n.lat, n.lon]);
    }
    if (target && !PICKABLE.has(target.kind)) {
      const k = KINDS[target.kind] ?? KINDS[1];
      L.marker([target.lat, target.lon], { icon: markerIcon(k.color, k.glyph), title: `${targetName} (destination)` }).addTo(g);
    }
    const line: L.LatLngTuple[] = [
      ...(home ? [[home.lat, home.lon] as L.LatLngTuple] : []),
      ...route.map((n) => [n.lat, n.lon] as L.LatLngTuple),
    ];
    if (line.length > 1) L.polyline(line, { color: accent, weight: 4, opacity: 0.85 }).addTo(g);
    if (target && route.length) {
      const last = route[route.length - 1];
      L.polyline(
        [
          [last.lat, last.lon],
          [target.lat, target.lon],
        ],
        { color: accent, weight: 3, opacity: 0.7, dashArray: "6 8" },
      ).addTo(g);
    }
    if (!fitted.current && pts.length) {
      fitted.current = true;
      if (pts.length === 1) m.setView(pts[0], 13);
      else m.fitBounds(L.latLngBounds(pts), { padding: [30, 30], maxZoom: 15 });
    }
  }, [data.data, relays, route, home, target]);

  const keys = route.map((n) => n.public_key);
  const hidden = (data.data?.without_location ?? 0) > 0;

  return (
    <Dialog
      title={`Route to ${targetName} on the map`}
      size="lg"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!route.length} onClick={() => onUse(keys)}>
            Use this route
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <p className="text-muted">
          Tap the repeaters in the order messages should take, starting with the one your radio reaches. Then test the route:
          a trace goes out along it and back, and each repeater reports the signal it heard.
        </p>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <label className="flex items-center gap-2">
            Heard within
            <select
              value={age}
              onChange={(e) => setAge(e.target.value as Age)}
              className="min-h-8 rounded-md border border-line bg-surface px-2 text-xs text-ink"
            >
              {Object.entries(AGE_LABELS).map(([v, label]) => (
                <option key={v} value={v}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {tooOld > 0 && (
            <span>
              {tooOld} older {tooOld === 1 ? "repeater" : "repeaters"} hidden
            </span>
          )}
        </div>
        <div ref={container} className="h-[min(50vh,26rem)] w-full overflow-hidden rounded-xl border border-line" aria-label="Map of repeaters" />
        {hidden && (
          <p className="text-xs text-muted">Repeaters that don't share their position aren't on the map; add those from the list instead.</p>
        )}
        <div className="space-y-2">
          <ol className="flex min-w-0 flex-wrap items-center gap-1.5" aria-label="Route">
            <li className="rounded-full bg-surface-2 px-2.5 py-1 text-xs">{home?.name ?? "Your radio"}</li>
            {route.map((n, i) => (
              <li key={`${n.id}-${i}`} className="flex items-center gap-1.5 text-xs">
                <span className="text-muted">→</span>
                <span className="rounded-full bg-accent-soft px-2.5 py-1 text-accent">
                  {i + 1}. {n.alias || n.name}
                </span>
              </li>
            ))}
            <li className="flex items-center gap-1.5 text-xs">
              <span className="text-muted">{route.length ? "⇢" : "→"}</span>
              <span className="rounded-full bg-surface-2 px-2.5 py-1">{targetName}</span>
            </li>
          </ol>
          <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" disabled={!route.length} onClick={() => (trace.reset(), setRoute((r) => r.slice(0, -1)))}>
            <Undo2 className="size-4" aria-hidden /> Undo
          </Button>
          <Button variant="ghost" disabled={!route.length} onClick={() => (trace.reset(), setRoute([]))}>
            <Eraser className="size-4" aria-hidden /> Clear
          </Button>
          <Button disabled={!route.length || trace.isPending} onClick={() => trace.mutate(keys)}>
            <Activity className="size-4" aria-hidden /> {trace.isPending ? "Tracing…" : "Test route"}
          </Button>
          </div>
        </div>
        <ErrorText error={trace.error ?? data.error} />
        {trace.data && (
          <div className="rounded-xl border border-line p-3">
            <p className="mb-2 font-medium">
              The trace came back in {(trace.data.elapsed_ms / 1000).toFixed(1)} s. Signal (SNR) each hop heard:
            </p>
            <ol className="space-y-1 text-xs">
              {trace.data.hops.map((h, i) => (
                <li key={i} className="flex items-center gap-2">
                  <span className="w-5 text-muted">{i + 1}.</span>
                  <span className="min-w-0 flex-1 truncate">
                    {h.names[0] ?? <code className="font-mono">{h.hash}</code>}
                    {i >= route.length && <span className="text-muted"> (back)</span>}
                  </span>
                  <span className={cx("font-mono tabular-nums", snrTone(h.snr))}>
                    {h.snr == null ? "—" : `${h.snr > 0 ? "+" : ""}${h.snr.toFixed(2)} dB`}
                  </span>
                </li>
              ))}
              <li className="flex items-center gap-2">
                <span className="w-5 text-muted">↩</span>
                <span className="min-w-0 flex-1 truncate">{home?.name ?? "Your radio"}</span>
                <span className={cx("font-mono tabular-nums", snrTone(trace.data.final_snr))}>
                  {trace.data.final_snr == null ? "—" : `${trace.data.final_snr > 0 ? "+" : ""}${trace.data.final_snr.toFixed(2)} dB`}
                </span>
              </li>
            </ol>
            <p className="mt-2 text-xs text-muted">
              The trace tests the repeaters, not {targetName} itself: the last leg depends on {targetName} being in range of
              the last repeater.
            </p>
          </div>
        )}
      </div>
    </Dialog>
  );
}
