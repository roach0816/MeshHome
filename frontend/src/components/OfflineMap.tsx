import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Trash2, X } from "lucide-react";
import { api, type Bounds, type Course, type MapConfig, type MapData, type OfflineMapState } from "../lib/api";
import { formatDateTime } from "../lib/util";
import { Badge, Button, ErrorText, Field, Input } from "./ui";
import { Section, Select } from "./SettingsKit";

// Same tile maths as the server (services/offline_map.py), for an instant estimate.
const MAX_LAT = 85.05112878;
function tileXY(lat: number, lon: number, z: number): [number, number] {
  const n = 2 ** z;
  const r = (Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * Math.PI) / 180;
  const x = Math.floor(((lon + 180) / 360) * n);
  const y = Math.floor(((1 - Math.asinh(Math.tan(r)) / Math.PI) / 2) * n);
  return [Math.min(Math.max(x, 0), n - 1), Math.min(Math.max(y, 0), n - 1)];
}
function countTiles(b: Bounds, zmin: number, zmax: number): number {
  let n = 0;
  for (let z = zmin; z <= zmax; z++) {
    const [x0, y0] = tileXY(b.north, b.west, z);
    const [x1, y1] = tileXY(b.south, b.east, z);
    n += (x1 - x0 + 1) * (y1 - y0 + 1);
  }
  return n;
}

/** A box `km` beyond the given points on every side. */
function around(points: [number, number][], km: number): Bounds | null {
  if (!points.length) return null;
  const lats = points.map((p) => p[0]);
  const lons = points.map((p) => p[1]);
  const mid = (Math.min(...lats) + Math.max(...lats)) / 2;
  const dLat = km / 111.32;
  const dLon = km / (111.32 * Math.max(0.01, Math.cos((mid * Math.PI) / 180)));
  const clamp = (v: number, a: number) => Math.max(-a, Math.min(a, v));
  return {
    south: clamp(Math.min(...lats) - dLat, 90),
    north: clamp(Math.max(...lats) + dLat, 90),
    west: clamp(Math.min(...lons) - dLon, 180),
    east: clamp(Math.max(...lons) + dLon, 180),
  };
}

function size(bytes: number) {
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1e3)} kB`;
}

type Area = "courses" | "base" | "custom";

export function OfflineMapSection() {
  const qc = useQueryClient();
  const state = useQuery({ queryKey: ["offline-map"], queryFn: () => api<OfflineMapState>("/api/offline-map") });
  const mapCfg = useQuery({ queryKey: ["map-settings"], queryFn: () => api<MapConfig>("/api/settings/map") });
  const courses = useQuery({ queryKey: ["tracking-courses"], queryFn: () => api<Course[]>("/api/tracking/courses") });
  const mapData = useQuery({ queryKey: ["map"], queryFn: () => api<MapData>("/api/map") });

  const [source, setSource] = useState("usgs-topo");
  const [area, setArea] = useState<Area | null>(null);
  const [margin, setMargin] = useState(1);
  const [radius, setRadius] = useState(5);
  const [custom, setCustom] = useState({ south: "", west: "", north: "", east: "" });
  const [minZoom, setMinZoom] = useState(10);
  const [maxZoom, setMaxZoom] = useState(16);
  const [url, setUrl] = useState("");
  const [attribution, setAttribution] = useState("");
  const [permitted, setPermitted] = useState(false);

  const coursePoints = useMemo(
    () => (courses.data ?? []).flatMap((c) => [...c.segments.flat(), ...c.waypoints.map((w) => [w.lat, w.lon] as [number, number])]),
    [courses.data],
  );
  const base = mapData.data?.gateways.find((g) => g.live) ?? mapData.data?.gateways[0];
  const chosen: Area = area ?? (coursePoints.length ? "courses" : base ? "base" : "custom");

  const bounds: Bounds | null = useMemo(() => {
    if (chosen === "courses") return around(coursePoints, margin);
    if (chosen === "base") return base ? around([[base.lat, base.lon]], radius) : null;
    const v = { south: Number(custom.south), west: Number(custom.west), north: Number(custom.north), east: Number(custom.east) };
    const ok =
      Object.values(custom).every((x) => x.trim() !== "" && Number.isFinite(Number(x))) &&
      v.south < v.north &&
      v.west < v.east &&
      v.south >= -90 &&
      v.north <= 90 &&
      v.west >= -180 &&
      v.east <= 180;
    return ok ? v : null;
  }, [chosen, coursePoints, margin, base, radius, custom]);

  const src = state.data?.sources.find((s) => s.id === source);
  const top = src?.max_zoom ?? 19;
  const zmax = Math.min(maxZoom, top);
  const zmin = Math.min(minZoom, zmax);
  const tiles = bounds ? countTiles(bounds, zmin, zmax) : 0;
  const limit = state.data?.max_tiles ?? 20000;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["offline-map"] });
    qc.invalidateQueries({ queryKey: ["map-settings"] });
    qc.invalidateQueries({ queryKey: ["map"] });
    qc.invalidateQueries({ queryKey: ["tracking"] });
  };
  const download = useMutation({
    mutationFn: () =>
      api("/api/offline-map/download", {
        json: {
          source,
          bounds,
          min_zoom: zmin,
          max_zoom: zmax,
          ...(source === "custom" ? { url: url.trim(), attribution: attribution.trim(), permitted } : {}),
        },
      }),
    onSettled: refresh,
  });
  const cancel = useMutation({ mutationFn: () => api("/api/offline-map/cancel", { method: "POST" }), onSettled: refresh });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const clear = useMutation({
    mutationFn: () => api("/api/offline-map", { method: "DELETE" }),
    onSettled: () => {
      setConfirmDelete(false);
      refresh();
    },
  });
  const useIt = useMutation({
    mutationFn: (offline: boolean) => api<MapConfig>("/api/settings/map", { method: "PUT", json: { ...mapCfg.data!, offline } }),
    onSuccess: refresh,
  });

  const job = state.data?.job;
  const saved = state.data?.saved;
  const running = job?.state === "running";
  const pct = job && job.total ? Math.round((100 * (job.done + job.missing + job.failed)) / job.total) : 0;
  const zooms = Array.from({ length: top + 1 }, (_, z) => z).filter((z) => z >= 4);

  return (
    <Section
      title="Offline map"
      description="Save the map of an area on this server, for places without internet."
      info={
        <>
          <p>
            At an event without internet your phone can still reach MeshHome over its Wi-Fi, but not a tile server. Download the
            area beforehand, while this server has internet; the maps then use the saved tiles.
          </p>
          <p>
            USGS maps are public domain and cover the United States. Another tile server can be used when its terms allow saving
            tiles; OpenStreetMap's own servers don't allow it, so they're refused. Saved tiles aren't included in backups.
          </p>
        </>
      }
    >
      {saved && saved.tiles > 0 ? (
        <div className="space-y-3 rounded-xl border border-line p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{saved.source_name}</span>
            <Badge>
              {saved.tiles.toLocaleString()} tiles · {size(saved.bytes)}
            </Badge>
            <Badge>
              zoom {saved.min_zoom}–{saved.max_zoom}
            </Badge>
          </div>
          {saved.downloaded_at && <p className="text-xs text-muted">Downloaded {formatDateTime(saved.downloaded_at)}</p>}
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={!!mapCfg.data?.offline}
              disabled={!mapCfg.data || useIt.isPending}
              onChange={(e) => useIt.mutate(e.target.checked)}
              className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
            />
            <span>
              <span className="block text-sm font-medium">Use the downloaded map</span>
              <span className="block text-xs text-muted">On both maps. Outside the saved area there's no map.</span>
            </span>
          </label>
          {confirmDelete ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="danger" onClick={() => clear.mutate()} disabled={clear.isPending || running}>
                <Trash2 className="size-4" aria-hidden /> Delete the saved map
              </Button>
              <Button onClick={() => setConfirmDelete(false)}>Keep it</Button>
            </div>
          ) : (
            <Button variant="danger" onClick={() => setConfirmDelete(true)} disabled={running}>
              <Trash2 className="size-4" aria-hidden /> Delete
            </Button>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted">No map saved yet.</p>
      )}

      {running && job ? (
        <div className="space-y-2" role="status">
          <div className="flex justify-between text-sm">
            <span>
              Downloading {job.done.toLocaleString()} of {job.total.toLocaleString()} tiles
            </span>
            <span className="text-muted">{pct}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-2">
            <div className="h-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <Button onClick={() => cancel.mutate()} disabled={cancel.isPending}>
            <X className="size-4" aria-hidden /> Cancel
          </Button>
        </div>
      ) : (
        <>
          {job && job.state !== "idle" && (
            <p
              role="status"
              className={
                job.state === "error"
                  ? "rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger"
                  : "text-sm text-muted"
              }
            >
              {job.state === "done" && `Downloaded: ${(job.done - job.skipped).toLocaleString()} new tiles`}
              {job.state === "cancelled" && `Cancelled after ${job.done.toLocaleString()} tiles (kept)`}
              {job.state === "error" && `Stopped: ${job.error}`}
              {job.skipped > 0 && `, ${job.skipped.toLocaleString()} already saved`}
              {job.missing > 0 && `; the source has no map for ${job.missing.toLocaleString()}`}
              {job.failed > 0 && `; ${job.failed.toLocaleString()} couldn't be fetched (download again to retry)`}.
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Map" htmlFor="off-source" hint={src?.note ?? "Only one saved map at a time: another source replaces it."}>
              <Select
                id="off-source"
                value={source}
                options={[
                  ...(state.data?.sources ?? []).map((s) => ({ value: s.id, label: s.name })),
                  { value: "custom", label: "Another tile server…" },
                ]}
                onChange={(v) => {
                  setSource(v);
                  setPermitted(false);
                }}
              />
            </Field>
            <Field label="Area" htmlFor="off-area">
              <Select
                id="off-area"
                value={chosen}
                options={[
                  ...(coursePoints.length ? [{ value: "courses" as Area, label: "Around the courses" }] : []),
                  ...(base ? [{ value: "base" as Area, label: `Around ${base.name}` }] : []),
                  { value: "custom" as Area, label: "Coordinates…" },
                ]}
                onChange={setArea}
              />
            </Field>
          </div>
          {source === "custom" && (
            <div className="space-y-3">
              <Field label="Tile URL template" htmlFor="off-url" hint="https:// with {z}, {x} and {y}.">
                <Input
                  id="off-url"
                  value={url}
                  spellCheck={false}
                  autoCapitalize="none"
                  className="font-mono text-xs sm:text-xs"
                  onChange={(e) => setUrl(e.target.value)}
                />
              </Field>
              <Field label="Attribution" htmlFor="off-attr">
                <Input id="off-attr" value={attribution} onChange={(e) => setAttribution(e.target.value)} />
              </Field>
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={permitted}
                  onChange={(e) => setPermitted(e.target.checked)}
                  className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
                />
                <span className="text-sm">This tile server's terms allow saving its tiles for offline use.</span>
              </label>
            </div>
          )}
          {chosen === "courses" && (
            <Field label="Margin around the courses" htmlFor="off-margin">
              <Select
                id="off-margin"
                value={margin}
                options={[0.5, 1, 2, 5].map((km) => ({ value: km, label: `${km} km (${(km * 0.621).toFixed(1)} mi)` }))}
                onChange={setMargin}
              />
            </Field>
          )}
          {chosen === "base" && (
            <Field label="Distance around it" htmlFor="off-radius">
              <Select
                id="off-radius"
                value={radius}
                options={[2, 5, 10, 20].map((km) => ({ value: km, label: `${km} km (${(km * 0.621).toFixed(1)} mi)` }))}
                onChange={setRadius}
              />
            </Field>
          )}
          {chosen === "custom" && (
            <div className="grid grid-cols-2 gap-3">
              {(["north", "south", "west", "east"] as const).map((k) => (
                <Field key={k} label={k[0].toUpperCase() + k.slice(1)} htmlFor={`off-${k}`}>
                  <Input
                    id={`off-${k}`}
                    inputMode="decimal"
                    placeholder={k === "north" || k === "south" ? "latitude" : "longitude"}
                    value={custom[k]}
                    onChange={(e) => setCustom({ ...custom, [k]: e.target.value })}
                  />
                </Field>
              ))}
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label="From zoom" htmlFor="off-zmin" hint="10 shows the region.">
              <Select id="off-zmin" value={zmin} options={zooms.filter((z) => z <= zmax).map((z) => ({ value: z, label: String(z) }))} onChange={setMinZoom} />
            </Field>
            <Field label="To zoom" htmlFor="off-zmax" hint={`16 shows trails and paths${top < 19 ? `; ${top} is this map's closest` : ""}.`}>
              <Select id="off-zmax" value={zmax} options={zooms.map((z) => ({ value: z, label: String(z) }))} onChange={setMaxZoom} />
            </Field>
          </div>
          <p className="text-sm">
            {bounds ? (
              <>
                About <strong>{tiles.toLocaleString()}</strong> tiles (roughly {size(tiles * 20000)})
                {tiles > limit && <span className="text-danger"> — over the limit of {limit.toLocaleString()}; choose a smaller area or zoom</span>}
              </>
            ) : (
              <span className="text-muted">
                {chosen === "courses" ? "Upload courses on the tracking map first." : "Enter the area's edges in decimal degrees."}
              </span>
            )}
          </p>
          <ErrorText error={download.error ?? cancel.error ?? clear.error ?? useIt.error ?? state.error} />
          <Button
            variant="primary"
            onClick={() => download.mutate()}
            disabled={!bounds || tiles === 0 || tiles > limit || download.isPending || (source === "custom" && (!url.trim() || !permitted))}
          >
            <Download className="size-4" aria-hidden /> Download
          </Button>
        </>
      )}
    </Section>
  );
}
