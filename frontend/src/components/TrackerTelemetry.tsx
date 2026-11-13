import { useEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { BatteryFull, BatteryLow, BatteryMedium, Droplets, PlugZap, Signal, Thermometer } from "lucide-react";
import { api, type TelemetryPoint, type TrackedDevice } from "../lib/api";
import type { unitsFor } from "../lib/units";
import { cx } from "../lib/util";
import { Dialog } from "./Dialog";

type U = ReturnType<typeof unitsFor>;

function ago(epoch: number, now = Date.now() / 1000): string {
  const s = Math.max(0, Math.round(now - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function duration(s: number): string {
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.round((s % 86400) / 3600)} h`;
}

const toEpoch = (iso: string) => Date.parse(iso) / 1000;

/** Its readings, latest first from the telemetry, then from its last position's sensors. */
function readings(dev: TrackedDevice) {
  const t = dev.telemetry;
  const env = t?.environment;
  const s = dev.sensors;
  const envAt = env?.at ?? (s ? toEpoch(s.at) : null);
  return {
    batteryPct: dev.battery_pct,
    volts: dev.battery_v ?? t?.device?.voltage ?? null,
    batteryAt: t?.device?.at ?? (dev.last_reply_at ? toEpoch(dev.last_reply_at) : null),
    external: dev.external_power,
    temp: env?.temp_c ?? s?.temp_c ?? null,
    humidity: env?.humidity ?? s?.humidity ?? null,
    pressure: env?.pressure_hpa ?? s?.pressure_hpa ?? null,
    lux: env?.lux ?? s?.lux ?? null,
    baroAlt: env?.baro_alt_m ?? s?.baro_alt_m ?? null,
    envAt,
    channelUtil: t?.device?.channel_util ?? null,
    airUtil: t?.device?.air_util_tx ?? null,
    uptime: t?.device?.uptime_s ?? null,
  };
}

function hopsText(dev: TrackedDevice): string | null {
  const l = dev.link;
  if (!l) return null;
  if (l.hops.length === 0) return "heard directly";
  const last = l.hops[l.hops.length - 1];
  const name = last.name ?? (last.hash === "?" ? null : last.hash.toUpperCase());
  return `${l.hops.length} hop${l.hops.length === 1 ? "" : "s"}${name ? `, last via ${name}` : ""}`;
}

// ---- the chips on each tracker --------------------------------------------------------------

function Chip({ icon, children, tone, title }: { icon: ReactNode; children: ReactNode; tone?: "danger"; title?: string }) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs tabular-nums",
        tone === "danger" ? "border-danger/40 font-semibold text-danger" : "border-line text-ink",
      )}
    >
      {icon}
      {children}
    </span>
  );
}

/** Battery, temperature, humidity and signal at a glance (the Telemetry button shows the rest). */
export function TelemetryChips({ dev, u }: { dev: TrackedDevice; u: U }) {
  const r = readings(dev);
  const chips: ReactNode[] = [];
  if (r.external) {
    chips.push(
      <Chip key="bat" icon={<PlugZap className="size-3.5" aria-hidden />} title="On external power">
        Plugged in
      </Chip>,
    );
  } else if (r.batteryPct !== null || r.volts !== null) {
    const Icon = dev.battery_low ? BatteryLow : (r.batteryPct ?? 0) >= 80 ? BatteryFull : BatteryMedium;
    chips.push(
      <Chip
        key="bat"
        icon={<Icon className="size-3.5" aria-hidden />}
        tone={dev.battery_low ? "danger" : undefined}
        title={`Battery${r.volts !== null ? ` ${r.volts.toFixed(2)} V` : ""}${r.batteryAt ? `, ${ago(r.batteryAt)}` : ""}`}
      >
        <span className="sr-only">Battery </span>
        {r.batteryPct !== null ? `${r.batteryPct}%` : `${r.volts!.toFixed(2)} V`}
      </Chip>,
    );
  }
  if (r.temp !== null) {
    chips.push(
      <Chip key="temp" icon={<Thermometer className="size-3.5" aria-hidden />} title="Temperature inside the tracker">
        <span className="sr-only">Temperature </span>
        {u.temp(r.temp).toFixed(u.imperial ? 0 : 1)}
        {u.tempUnit}
      </Chip>,
    );
  }
  if (r.humidity !== null) {
    chips.push(
      <Chip key="hum" icon={<Droplets className="size-3.5" aria-hidden />} title="Relative humidity">
        <span className="sr-only">Humidity </span>
        {Math.round(r.humidity)}%
      </Chip>,
    );
  }
  if (dev.link?.snr !== null && dev.link?.snr !== undefined) {
    chips.push(
      <Chip key="sig" icon={<Signal className="size-3.5" aria-hidden />} title={`Signal of its last position${hopsText(dev) ? `: ${hopsText(dev)}` : ""}`}>
        <span className="sr-only">Signal </span>
        {dev.link.snr.toFixed(dev.link.snr % 1 ? 1 : 0)} dB
      </Chip>,
    );
  }
  if (chips.length === 0) return null;
  return <span className="mt-1 flex flex-wrap gap-1">{chips}</span>;
}

// ---- the Telemetry dialog --------------------------------------------------------------------

function Reading({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      {sub && <div className="text-xs text-muted">{sub}</div>}
    </div>
  );
}

function Group({ title, when, children }: { title: string; when?: number | null; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 flex items-baseline justify-between gap-2 text-sm font-semibold">
        {title}
        {when ? <span className="text-xs font-normal text-muted">reported {ago(when)}</span> : null}
      </h3>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">{children}</div>
    </section>
  );
}

const RANGES = [
  { hours: 6, label: "6 h" },
  { hours: 24, label: "24 h" },
  { hours: 72, label: "3 days" },
];

export function TelemetryDialog({
  dev,
  u,
  name,
  onClose,
}: {
  dev: TrackedDevice;
  u: U;
  name: string;
  onClose: () => void;
}) {
  const [hours, setHours] = useState(24);
  const history = useQuery({
    queryKey: ["tracking-telemetry", dev.id, hours],
    queryFn: () => api<{ hours: number; points: TelemetryPoint[] }>(`/api/tracking/devices/${dev.id}/telemetry?hours=${hours}`),
    refetchInterval: 60_000,
  });
  const r = readings(dev);
  const mt = dev.source === "meshtastic";
  const points = history.data?.points ?? [];
  const hasBattery = r.batteryPct !== null || r.volts !== null || r.external;
  const hasEnv = r.temp !== null || r.humidity !== null || r.pressure !== null || r.lux !== null || r.baroAlt !== null;
  const hasDevice = r.channelUtil !== null || r.airUtil !== null || r.uptime !== null;

  const batterySeries = useMemo(() => {
    const pct = points.filter((p) => p.battery_pct !== null);
    if (pct.length >= 2) return { pts: pct.map((p) => ({ t: toEpoch(p.at), v: p.battery_pct! })), unit: "%", fixed: [0, 100] as [number, number], digits: 0 };
    const v = points.filter((p) => p.battery_v !== null);
    return { pts: v.map((p) => ({ t: toEpoch(p.at), v: p.battery_v! })), unit: " V", fixed: undefined, digits: 2 };
  }, [points]);
  const tempSeries = useMemo(
    () => points.filter((p) => p.temp_c !== null).map((p) => ({ t: toEpoch(p.at), v: u.temp(p.temp_c!) })),
    [points, u],
  );
  const now = Date.now() / 1000;

  return (
    <Dialog title={`Telemetry · ${name}`} onClose={onClose} size="lg">
      <div className="space-y-5">
        <p className="text-sm text-muted">
          {dev.last_reply_at ? `Last heard ${ago(toEpoch(dev.last_reply_at))}. ` : "Not heard yet. "}
          {mt
            ? "Meshtastic trackers send their battery and sensor readings on their own schedule, separately from their position."
            : "Each answer to MeshHome's request carries its battery and, when it shares them, its sensor readings."}
        </p>

        {hasBattery && (
          <Group title="Battery" when={r.batteryAt}>
            <Reading
              label="Charge"
              value={r.external ? "Plugged in" : r.batteryPct !== null ? `${r.batteryPct}%` : "—"}
              sub={r.external ? "on external power" : dev.battery_low ? <span className="font-semibold text-danger">low</span> : undefined}
            />
            {r.volts !== null && <Reading label="Voltage" value={`${r.volts.toFixed(2)} V`} />}
          </Group>
        )}

        {hasEnv && (
          <Group title="Sensors" when={r.envAt}>
            {r.temp !== null && (
              <Reading label="Temperature" value={`${u.temp(r.temp).toFixed(u.imperial ? 0 : 1)}${u.tempUnit}`} sub="inside the tracker" />
            )}
            {r.humidity !== null && <Reading label="Humidity" value={`${Math.round(r.humidity)}%`} />}
            {r.pressure !== null && <Reading label="Pressure" value={`${u.pressure(r.pressure).toFixed(u.pressureDigits)} ${u.pressureUnit}`} />}
            {r.lux !== null && <Reading label="Light" value={`${Math.round(r.lux).toLocaleString()} lux`} />}
            {r.baroAlt !== null && <Reading label="Altitude (barometer)" value={`${Math.round(u.alt(r.baroAlt))} ${u.altUnit}`} />}
          </Group>
        )}

        {dev.link && (
          <Group title="Signal" when={toEpoch(dev.link.at)}>
            {dev.link.snr !== null && <Reading label="SNR" value={`${dev.link.snr.toFixed(1)} dB`} sub="above 0 is good" />}
            {dev.link.rssi !== null && <Reading label="RSSI" value={`${Math.round(dev.link.rssi)} dBm`} />}
            <Reading label="Route" value={<span className="text-base">{hopsText(dev)}</span>} />
          </Group>
        )}

        {hasDevice && (
          <Group title="Device" when={r.batteryAt}>
            {r.uptime !== null && <Reading label="Up for" value={<span className="text-base">{duration(r.uptime)}</span>} />}
            {r.channelUtil !== null && <Reading label="Channel busy" value={`${r.channelUtil.toFixed(1)}%`} sub="airtime it hears in use" />}
            {r.airUtil !== null && <Reading label="Its own airtime" value={`${r.airUtil.toFixed(2)}%`} sub="last hour" />}
          </Group>
        )}

        <section>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">History</h3>
            <div role="tablist" aria-label="Time range" className="inline-flex rounded-lg border border-line p-0.5">
              {RANGES.map((x) => (
                <button
                  key={x.hours}
                  type="button"
                  role="tab"
                  aria-selected={hours === x.hours}
                  onClick={() => setHours(x.hours)}
                  className={cx("min-h-8 rounded-md px-2.5 text-xs font-medium", hours === x.hours ? "bg-accent-soft text-accent" : "text-muted hover:text-ink")}
                >
                  {x.label}
                </button>
              ))}
            </div>
          </div>
          {history.isLoading && <p className="text-sm text-muted">Loading…</p>}
          {!history.isLoading && batterySeries.pts.length < 2 && tempSeries.length < 2 && (
            <p className="rounded-lg border border-dashed border-line p-3 text-center text-sm text-muted">
              Not enough readings in the last {RANGES.find((x) => x.hours === hours)?.label} to draw.
            </p>
          )}
          <div className="space-y-3">
            {batterySeries.pts.length >= 2 && (
              <LineChart
                title={batterySeries.unit === "%" ? "Battery (%)" : "Battery (V)"}
                points={batterySeries.pts}
                domain={batterySeries.fixed}
                format={(v) => `${v.toFixed(batterySeries.digits)}${batterySeries.unit}`}
                from={now - hours * 3600}
                to={now}
              />
            )}
            {tempSeries.length >= 2 && (
              <LineChart
                title={`Temperature (${u.tempUnit})`}
                points={tempSeries}
                format={(v) => `${v.toFixed(u.imperial ? 0 : 1)}${u.tempUnit}`}
                from={now - hours * 3600}
                to={now}
              />
            )}
          </div>
        </section>

        <details className="rounded-lg border border-line px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium">Something missing?</summary>
          {mt ? (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
              <li>
                <strong className="text-ink">Battery</strong> comes from the tracker's device telemetry, every 30 minutes by default (in the
                Meshtastic app on the tracker: Settings → Telemetry → Device metrics update interval).
              </li>
              <li>
                <strong className="text-ink">Temperature, humidity and light</strong> need its environment telemetry turned on (Settings →
                Telemetry → Environment metrics), on trackers with those sensors.
              </li>
              <li>Readings travel on the tracker's primary channel, the same one as its positions.</li>
            </ul>
          ) : (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
              <li>
                <strong className="text-ink">Battery</strong> comes with each answer.
              </li>
              <li>
                <strong className="text-ink">Sensor readings</strong> need the tracker to share them with this node: in the MeshCore app on
                the tracker, Telemetry → Environment.
              </li>
            </ul>
          )}
        </details>
      </div>
    </Dialog>
  );
}

// ---- a small line chart, with a readout under the pointer -------------------------------------

function clock(t: number, withDay: boolean): string {
  const d = new Date(t * 1000);
  return withDay
    ? d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function LineChart({
  title,
  points,
  domain,
  format,
  from: rangeFrom,
  to,
}: {
  title: string;
  points: { t: number; v: number }[];
  domain?: [number, number];
  format: (v: number) => string;
  from: number;
  to: number;
}) {
  // Drawn at its real width, so the text keeps its shape on a phone.
  const wrap = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(600);
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const H = 112;
  const PAD = { l: 44, r: 8, t: 8, b: 22 };
  // Start where its readings start, when that's later than the range (a tracker added recently).
  const from = Math.max(rangeFrom, points[0].t - Math.max(60, (to - points[0].t) * 0.05));
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const vs = points.map((p) => p.v);
  let [lo, hi] = domain ?? [Math.min(...vs), Math.max(...vs)];
  if (!domain) {
    const pad = Math.max((hi - lo) * 0.15, Math.abs(hi) * 0.01, 0.5);
    lo -= pad;
    hi += pad;
  }
  const x = (t: number) => PAD.l + ((t - from) / Math.max(1, to - from)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - (v - lo) / Math.max(1e-9, hi - lo)) * (H - PAD.t - PAD.b);
  // A gap of more than an hour (or a tenth of the range) breaks the line: no readings then.
  const gap = Math.max(3600, (to - from) / 10);
  let d = "";
  points.forEach((p, i) => {
    const move = i === 0 || p.t - points[i - 1].t > gap;
    d += `${move ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
  });
  const withDay = to - from > 86400;
  const latest = points[points.length - 1];
  const min = Math.min(...vs);
  const max = Math.max(...vs);
  const h = hover !== null ? points[hover] : null;

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    for (let i = 1; i < points.length; i++) if (Math.abs(x(points[i].t) - px) < Math.abs(x(points[best].t) - px)) best = i;
    setHover(best);
  };

  return (
    <figure className="rounded-lg border border-line bg-surface p-2">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-x-3 px-1 text-xs">
        <span className="font-medium text-ink">{title}</span>
        <span className="text-muted tabular-nums">
          now {format(latest.v)} · low {format(min)} · high {format(max)}
        </span>
      </figcaption>
      <div ref={wrap} className="relative">
        <svg
          ref={ref}
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="mt-1 block touch-none"
          role="img"
          aria-label={`${title}: from ${format(points[0].v)} to ${format(latest.v)}, low ${format(min)}, high ${format(max)}`}
          onPointerMove={onMove}
          onPointerDown={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {[lo, (lo + hi) / 2, hi].map((g) => (
            <g key={g}>
              <line x1={PAD.l} x2={W - PAD.r} y1={y(g)} y2={y(g)} stroke="var(--border)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
              <text x={PAD.l - 6} y={y(g)} dy="0.35em" textAnchor="end" fontSize={11} fill="var(--muted)">
                {format(g)}
              </text>
            </g>
          ))}
          <text x={PAD.l} y={H - 6} fontSize={11} fill="var(--muted)">
            {clock(from, withDay)}
          </text>
          <text x={W - PAD.r} y={H - 6} fontSize={11} textAnchor="end" fill="var(--muted)">
            now
          </text>
          <path d={d} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          {h && (
            <>
              <line x1={x(h.t)} x2={x(h.t)} y1={PAD.t} y2={H - PAD.b} stroke="var(--muted)" strokeWidth={1} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
              <circle cx={x(h.t)} cy={y(h.v)} r={4} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
            </>
          )}
        </svg>
        {h && (
          <div
            className="pointer-events-none absolute top-0 rounded-md border border-line bg-surface px-2 py-1 text-xs shadow-sm"
            style={{
              left: `${(x(h.t) / W) * 100}%`,
              transform: `translateX(${x(h.t) / W > 0.6 ? "-105%" : "8px"})`,
            }}
          >
            <div className="font-semibold tabular-nums">{format(h.v)}</div>
            <div className="text-muted">{clock(h.t, withDay)}</div>
          </div>
        )}
      </div>
    </figure>
  );
}

