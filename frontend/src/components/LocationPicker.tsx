import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { Crosshair, Loader2 } from "lucide-react";
import { api, type MapData } from "../lib/api";
import { browserLocation, fmtCoord } from "../lib/geo";
import { canLocate } from "../lib/util";
import { Dialog } from "./Dialog";
import { Button, ErrorText } from "./ui";

type Point = { lat: number; lon: number };

const pin = L.divIcon({
  className: "",
  html: '<span style="display:block;width:22px;height:22px;border-radius:9999px;background:#0f766e;border:3px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.45)"></span>',
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/**
 * Pick the node's position on a map: click (or drag the pin), or use the browser's location.
 * Uses the same tiles as the Map page (Settings → Map). Loaded only when opened.
 */
export function LocationPicker({ initial, onPick, onClose }: { initial: Point | null; onPick: (p: Point) => void; onClose: () => void }) {
  const data = useQuery({ queryKey: ["map"], queryFn: () => api<MapData>("/api/map") });
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const marker = useRef<L.Marker | null>(null);
  const [point, setPoint] = useState<Point | null>(initial);
  const [locating, setLocating] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const place = (p: Point, zoomTo?: number) => {
    setPoint(p);
    const m = map.current;
    if (!m) return;
    if (marker.current) marker.current.setLatLng([p.lat, p.lon]);
    else {
      marker.current = L.marker([p.lat, p.lon], { icon: pin, draggable: true, keyboard: false }).addTo(m);
      marker.current.on("dragend", () => {
        const ll = marker.current!.getLatLng();
        setPoint({ lat: ll.lat, lon: L.Util.wrapNum(ll.lng, [-180, 180], true) });
      });
    }
    if (zoomTo) m.setView([p.lat, p.lon], zoomTo);
  };

  // Create the map once the dialog is open, then center it on the best known position.
  useEffect(() => {
    if (!container.current || map.current || !data.data) return;
    const m = L.map(container.current, { worldCopyJump: true }).setView([20, 0], 2);
    map.current = m;
    const cfg = data.data.tiles;
    L.tileLayer(cfg.tile_url, {
      maxZoom: cfg.max_zoom,
      attribution: cfg.tile_url.includes("openstreetmap.org")
        ? '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
        : escapeHtml(cfg.attribution),
      referrerPolicy: "strict-origin-when-cross-origin",
    }).addTo(m);
    m.on("click", (e: L.LeafletMouseEvent) => place({ lat: e.latlng.lat, lon: L.Util.wrapNum(e.latlng.lng, [-180, 180], true) }));
    const gw = data.data.gateways.find((g) => g.live) ?? data.data.gateways[0];
    const known = data.data.nodes.filter((n) => n.lat !== null && n.lon !== null);
    if (initial) place(initial, 14);
    else if (gw) m.setView([gw.lat, gw.lon], 12);
    else if (known.length) m.fitBounds(L.latLngBounds(known.map((n) => [n.lat!, n.lon!])), { maxZoom: 12, padding: [24, 24] });
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(container.current);
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
      marker.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.data]);

  const locate = async () => {
    setError(null);
    setLocating(true);
    try {
      const p = await browserLocation();
      place({ lat: p.lat, lon: p.lon }, 16);
    } catch (e) {
      setError(e);
    } finally {
      setLocating(false);
    }
  };

  return (
    <Dialog
      size="lg"
      title="Choose the node's location"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!point} onClick={() => point && onPick(point)}>
            Use this location
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted">Click the map to place the pin, or drag it to adjust.</p>
          {canLocate() && (
            <Button onClick={locate} disabled={locating}>
              {locating ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Crosshair className="size-4" aria-hidden />} Use my location
            </Button>
          )}
        </div>
        <div
          ref={container}
          className="h-[min(55dvh,26rem)] w-full overflow-hidden rounded-lg border border-line bg-surface-2"
          role="application"
          aria-label="Map: click to set the node's location"
        />
        <p className="text-sm" aria-live="polite">
          {point ? (
            <>
              Selected: <span className="font-mono">{fmtCoord(point.lat)}, {fmtCoord(point.lon)}</span>
            </>
          ) : (
            <span className="text-muted">No location selected yet.</span>
          )}
        </p>
        {data.isPending && <p className="text-sm text-muted">Loading map…</p>}
        <ErrorText error={error ?? data.error} />
      </div>
    </Dialog>
  );
}
