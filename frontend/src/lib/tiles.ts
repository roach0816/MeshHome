import L from "leaflet";
import type { MapConfig, OfflineSaved } from "./api";

export function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** True when the maps use the tiles saved on this server (Settings → Map → Offline map). */
export function usesOffline(cfg: MapConfig, saved: OfflineSaved | null | undefined): saved is OfflineSaved {
  return cfg.offline && !!saved && saved.tiles > 0 && saved.max_zoom !== null;
}

/** The map's base layer: the saved offline map, or the configured tile server. */
export function baseLayer(cfg: MapConfig, saved: OfflineSaved | null | undefined): L.TileLayer {
  if (usesOffline(cfg, saved)) {
    const b = saved.bounds;
    return L.tileLayer("/api/tiles/{z}/{x}/{y}", {
      // Zoom in past the saved levels by enlarging the closest saved tiles.
      maxNativeZoom: saved.max_zoom ?? undefined,
      minNativeZoom: saved.min_zoom ?? undefined,
      maxZoom: 19,
      // Don't ask for tiles outside the saved area.
      bounds: b ? L.latLngBounds([b.south, b.west], [b.north, b.east]) : undefined,
      attribution: `${escapeHtml(saved.attribution || saved.source_name || "Saved map")} · saved on this server`,
    });
  }
  const isOsm = cfg.tile_url.includes("openstreetmap.org");
  return L.tileLayer(cfg.tile_url, {
    maxZoom: cfg.max_zoom,
    attribution: isOsm
      ? '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
      : escapeHtml(cfg.attribution),
    // The app sends no Referer by default; tile servers (OSM's policy) expect one, so send the origin only.
    referrerPolicy: "strict-origin-when-cross-origin",
  });
}

/** A stable key for re-creating the base layer only when its source changes. */
export function baseLayerKey(cfg: MapConfig | undefined, saved: OfflineSaved | null | undefined): string {
  if (!cfg) return "";
  return usesOffline(cfg, saved)
    ? `offline:${saved.source}:${saved.min_zoom}:${saved.max_zoom}:${saved.downloaded_at}:${JSON.stringify(saved.bounds)}`
    : `online:${cfg.tile_url}:${cfg.attribution}:${cfg.max_zoom}`;
}
