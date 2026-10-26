import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Satellite, Thermometer } from "lucide-react";
import { api, type SensorHistory, type SensorsConfig, type SensorsState } from "../lib/api";
import { cx, relativeSeconds } from "../lib/util";
import { Section, Select } from "./SettingsKit";
import { Badge, Button, ErrorText, Field, Input } from "./ui";

// Sensors attached to the Pi MeshHome runs on (backend: services/sensors.py): an environment sensor
// (BME680) on I2C and a GPS on a serial port, as on the RAK6421 HAT.

const RANGES = [
  { hours: 24, label: "24 hours" },
  { hours: 168, label: "7 days" },
  { hours: 720, label: "30 days" },
];

function useSensors() {
  return useQuery({ queryKey: ["sensors"], queryFn: () => api<SensorsState>("/api/sensors"), refetchInterval: 15_000 });
}

const fahrenheit = (c: number) => (c * 9) / 5 + 32;
const INHG = 0.02953;
const FEET = 3.28084;

type Units = "metric" | "imperial";
/** Readings in the chosen units (stored values are metric). */
function unitsFor(u: Units) {
  const imp = u === "imperial";
  return {
    temp: (c: number) => (imp ? fahrenheit(c) : c),
    tempUnit: imp ? "°F" : "°C",
    pressure: (h: number) => (imp ? h * INHG : h),
    pressureUnit: imp ? "inHg" : "hPa",
    pressureDigits: imp ? 2 : 1,
    alt: (m: number) => (imp ? m * FEET : m),
    altUnit: imp ? "ft" : "m",
    dist: (m: number) => (imp ? m * FEET : m),
    distUnit: imp ? "ft" : "m",
  };
}

function Status({ tone, children }: { tone: "ok" | "warn" | "muted" | "danger"; children: ReactNode }) {
  return <Badge tone={tone === "ok" ? "accent" : tone === "danger" ? "danger" : tone === "warn" ? "warn" : undefined}>{children}</Badge>;
}

function Reading({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-surface-2 px-3 py-2.5">
      <p className="text-xs text-muted">{label}</p>
      <p className="text-lg font-semibold tabular-nums">{value}</p>
      {sub && <p className="text-xs text-muted tabular-nums">{sub}</p>}
    </div>
  );
}

/** A small line chart, drawn to scale between the series' own minimum and maximum. */
function LineChart({ label, unit, values, times, digits = 1 }: { label: string; unit: string; values: (number | null)[]; times: string[]; digits?: number }) {
  const pts = values.map((v, i) => (v == null ? null : { i, v }));
  const nums = values.filter((v): v is number => v != null);
  if (nums.length < 2) return null;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const W = 300;
  const H = 70;
  const x = (i: number) => (i / Math.max(1, values.length - 1)) * W;
  const y = (v: number) => H - 6 - ((v - min) / span) * (H - 12);
  let d = "";
  let pen = false;
  for (const p of pts) {
    if (!p) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x(p.i).toFixed(1)} ${y(p.v).toFixed(1)}`;
    pen = true;
  }
  const last = nums[nums.length - 1];
  const fmt = (t: string) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return (
    <figure className="min-w-0 space-y-1">
      <figcaption className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted">
          now {last.toFixed(digits)} {unit} · {min.toFixed(digits)}–{max.toFixed(digits)}
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-20 w-full overflow-visible" preserveAspectRatio="none" role="img" aria-label={`${label}, from ${min.toFixed(digits)} to ${max.toFixed(digits)} ${unit}`}>
        <line x1="0" x2={W} y1={y(max)} y2={y(max)} className="stroke-line" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
        <line x1="0" x2={W} y1={y(min)} y2={y(min)} className="stroke-line" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
        <path d={d} fill="none" className="stroke-accent" strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      </svg>
      <div className="flex justify-between text-[10px] text-muted">
        <span>{fmt(times[0])}</span>
        <span>{fmt(times[times.length - 1])}</span>
      </div>
    </figure>
  );
}

function History({ units }: { units: Units }) {
  const u = unitsFor(units);
  const [hours, setHours] = useState(24);
  const h = useQuery({
    queryKey: ["sensor-history", hours],
    queryFn: () => api<SensorHistory>(`/api/sensors/history?hours=${hours}`),
    refetchInterval: 300_000,
  });
  const pts = h.data?.points ?? [];
  const times = pts.map((p) => p.at);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">History</span>
        <div className="flex gap-1" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r.hours}
              onClick={() => setHours(r.hours)}
              aria-pressed={hours === r.hours}
              className={cx(
                "min-h-8 rounded-full px-3 text-xs",
                hours === r.hours ? "bg-ink text-surface" : "bg-surface-2 text-muted hover:text-ink",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {pts.length < 2 ? (
        <p className="text-xs text-muted">A reading is stored every 5 minutes; charts appear once there are a few.</p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <LineChart label="Temperature" unit={u.tempUnit} values={pts.map((p) => (p.temperature_c == null ? null : u.temp(p.temperature_c)))} times={times} />
          <LineChart label="Humidity" unit="%" values={pts.map((p) => p.humidity)} times={times} digits={0} />
          <LineChart
            label="Pressure"
            unit={u.pressureUnit}
            values={pts.map((p) => (p.pressure_hpa == null ? null : u.pressure(p.pressure_hpa)))}
            times={times}
            digits={u.pressureDigits}
          />
          <LineChart label="Air (gas resistance)" unit="kΩ" values={pts.map((p) => (p.gas_ohms == null ? null : p.gas_ohms / 1000))} times={times} digits={0} />
        </div>
      )}
      <ErrorText error={h.error} />
    </div>
  );
}

const ENV_HINT: Record<string, ReactNode> = {
  not_found: (
    <>
      No BME680 on I²C bus 1 (address 0x76 or 0x77). Fit it in a sensor slot (on a RAK6421: B, C or D), and check I²C is on:{" "}
      <code>sudo raspi-config</code> → Interface Options → I2C.
    </>
  ),
  error: "The sensor stopped answering. MeshHome looks for it again every minute; check it's pressed fully into its slot.",
};

const GNSS_HINT = (device: string): Record<string, ReactNode> => ({
  not_found: <>There's no serial port at {device}. On a Raspberry Pi, turn the serial port on: <code>sudo raspi-config</code> → Interface Options → Serial Port (login shell: No, hardware: Yes).</>,
  silent: (
    <>
      The GPS isn't sending anything on {device}. Turn off the serial login console (<code>sudo raspi-config</code> → Interface Options →
      Serial Port: login shell No, hardware Yes) and check the module is snapped fully into slot A.
    </>
  ),
  no_fix: "The GPS is working but hasn't found its position yet. Give the antenna a view of the sky; the first fix can take a few minutes.",
  error: <>MeshHome can't read {device}. If MeshHome was just updated, restart it (<code>sudo systemctl restart meshhome</code>) so it can open the port.</>,
});

export function SensorsSection() {
  const qc = useQueryClient();
  const st = useSensors();
  const [form, setForm] = useState<SensorsConfig | null>(null);
  // The distance as typed, in the chosen unit (converting each keystroke would fight the typing).
  const [moveText, setMoveText] = useState("");
  const moveUnits = form?.units;
  useEffect(() => {
    if (form) setMoveText(String(form.units === "imperial" ? Math.round(form.location_min_move_m * FEET) : form.location_min_move_m));
    // Only when loaded or the units change, not on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [moveUnits, form === null]);
  useEffect(() => {
    if (st.data && !form) setForm(st.data.config);
  }, [st.data, form]);
  const save = useMutation({
    mutationFn: (body: SensorsConfig) => api<SensorsConfig>("/api/settings/sensors", { method: "PUT", json: body }),
    onSuccess: (saved) => {
      setForm(saved);
      qc.invalidateQueries({ queryKey: ["sensors"] });
    },
  });
  const dirty = !!(st.data && form && JSON.stringify(st.data.config) !== JSON.stringify(form));
  const env = st.data?.environment;
  const gnss = st.data?.gnss;
  const r = env?.reading;
  const g = gnss?.reading;
  const envTone = env?.status === "ok" ? "ok" : env?.status === "error" ? "danger" : "muted";
  const gTone = gnss?.status === "fix" ? "ok" : gnss?.status === "no_fix" ? "warn" : gnss?.status === "error" || gnss?.status === "silent" ? "danger" : "muted";
  const label: Record<string, string> = {
    off: "Off",
    not_found: "Not found",
    ok: "Working",
    error: "Not answering",
    silent: "Not sending",
    no_fix: "Searching",
    fix: "Position fix",
  };
  const units: Units = st.data?.config.units ?? "metric";
  const u = unitsFor(units);
  const imperial = units === "imperial";
  const fixAge = useMemo(() => (g?.fix_at ? relativeSeconds(g.fix_at) : null), [g?.fix_at]);

  return (
    <>
      <Section
        title="Environment sensor"
        description="Temperature, humidity, pressure and air quality from a BME680 on this Pi."
        info={
          <>
            <p>
              MeshHome reads a BME680 (such as a RAK1906 in a RAK6421 sensor slot) on I²C once a minute, stores a reading every 5 minutes for
              30 days, and answers <code>/env</code> from the bot with the latest values.
            </p>
            <p>Sensors work on Raspberry Pi installs, where MeshHome runs on the Pi they're attached to.</p>
          </>
        }
        actions={env && <Status tone={envTone}>{label[env.status]}</Status>}
      >
        {env && ENV_HINT[env.status] && <p className="text-sm text-muted">{ENV_HINT[env.status]}</p>}
        {r && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Reading
              label="Temperature"
              value={`${u.temp(r.temperature_c).toFixed(1)} ${u.tempUnit}`}
              sub={imperial ? `${r.temperature_c.toFixed(1)} °C` : `${fahrenheit(r.temperature_c).toFixed(1)} °F`}
            />
            {r.humidity != null && <Reading label="Humidity" value={`${r.humidity.toFixed(0)} %`} />}
            {r.pressure_hpa != null && (
              <Reading
                label="Pressure"
                value={`${u.pressure(r.pressure_hpa).toFixed(u.pressureDigits)} ${u.pressureUnit}`}
                sub={imperial ? `${r.pressure_hpa.toFixed(1)} hPa` : `${(r.pressure_hpa * INHG).toFixed(2)} inHg`}
              />
            )}
            <Reading label="Air (gas resistance)" value={r.gas_ohms ? `${(r.gas_ohms / 1000).toFixed(0)} kΩ` : "Warming up"} sub="Higher is cleaner air" />
          </div>
        )}
        {r && <p className="text-xs text-muted">Read {relativeSeconds(r.at)}{env?.address != null ? ` · I²C address 0x${env.address.toString(16)}` : ""}</p>}
        {env?.status !== "off" && <History units={units} />}
      </Section>

      <Section
        title="GPS"
        description="Position from a GPS receiver on this Pi's serial port."
        info={
          <>
            <p>
              MeshHome reads a GPS that sends standard NMEA (such as a RAK12501 in a RAK6421's slot A, on <code>/dev/serial0</code>). It can keep your
              node's advertised location up to date, only when you've moved further than the distance you choose, and at most every 10 minutes.
            </p>
          </>
        }
        actions={gnss && <Status tone={gTone}>{label[gnss.status]}</Status>}
      >
        {gnss && GNSS_HINT(gnss.device ?? "/dev/serial0")[gnss.status] && <p className="text-sm text-muted">{GNSS_HINT(gnss.device ?? "/dev/serial0")[gnss.status]}</p>}
        {g && gnss?.status !== "off" && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Reading label="Satellites" value={g.satellites != null ? String(g.satellites) : "—"} sub={g.hdop != null ? `HDOP ${g.hdop}` : undefined} />
            {g.fix && g.lat != null && g.lon != null ? (
              <>
                <Reading label="Position" value={`${g.lat.toFixed(5)}`} sub={`${g.lon.toFixed(5)}`} />
                <Reading label="Altitude" value={g.alt_m != null ? `${u.alt(g.alt_m).toFixed(0)} ${u.altUnit}` : "—"} />
                <Reading label="Fix" value={fixAge ?? "—"} sub={g.utc ? new Date(g.utc).toLocaleTimeString() : undefined} />
              </>
            ) : (
              <Reading label="Position" value="No fix yet" />
            )}
          </div>
        )}
        {gnss?.location_update && (
          <p className="text-xs text-muted">
            Your node's location was last set from the GPS {relativeSeconds(gnss.location_update.at)}
            {gnss.location_update.moved_m != null ? ` (moved ${Math.round(u.dist(gnss.location_update.moved_m))} ${u.distUnit})` : ""}.
          </p>
        )}
      </Section>

      {form && (
        <Section title="Sensor settings" description="Which sensors MeshHome reads, and what it does with the GPS.">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Units" htmlFor="sensor-units" hint="For readings here and the /env bot reply.">
              <Select
                id="sensor-units"
                value={form.units}
                options={[
                  { value: "metric", label: "Metric (°C, hPa, metres)" },
                  { value: "imperial", label: "Imperial (°F, inHg, feet)" },
                ]}
                onChange={(v) => setForm({ ...form, units: v })}
              />
            </Field>
          </div>
          <label className="flex cursor-pointer items-start gap-3">
            <input type="checkbox" checked={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.checked })} className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]" />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-medium">
                <Thermometer className="size-4 text-muted" aria-hidden /> Read the environment sensor
              </span>
              <span className="block text-xs text-muted">A BME680 on I²C bus 1, found automatically.</span>
            </span>
          </label>
          <label className="flex cursor-pointer items-start gap-3">
            <input type="checkbox" checked={form.gnss} onChange={(e) => setForm({ ...form, gnss: e.target.checked })} className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]" />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-medium">
                <Satellite className="size-4 text-muted" aria-hidden /> Read the GPS
              </span>
              <span className="block text-xs text-muted">NMEA at 9600 baud.</span>
            </span>
          </label>
          {form.gnss && (
            <div className="grid gap-3 pl-8 sm:grid-cols-2">
              <Field label="GPS serial port" htmlFor="gnss-device" hint="/dev/serial0 on a Raspberry Pi (MeshHome uses /dev/ttyAMA0 on a Pi 5).">
                <Input id="gnss-device" className="font-mono" value={form.gnss_device} onChange={(e) => setForm({ ...form, gnss_device: e.target.value })} spellCheck={false} />
              </Field>
            </div>
          )}
          <label className={cx("flex cursor-pointer items-start gap-3", !form.gnss && "opacity-50")}>
            <input type="checkbox" disabled={!form.gnss} checked={form.update_location} onChange={(e) => setForm({ ...form, update_location: e.target.checked })} className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]" />
            <span>
              <span className="block text-sm font-medium">Keep my node's location from the GPS</span>
              <span className="block text-xs text-muted">
                Sets the radio's location when you've moved. Whether adverts share it is still your choice in the radio's settings.
              </span>
            </span>
          </label>
          {form.gnss && form.update_location && (
            <div className="grid gap-3 pl-8 sm:grid-cols-2">
              <Field
                label={`When it has moved at least (${form.units === "imperial" ? "feet" : "metres"})`}
                htmlFor="gnss-move"
                hint={form.units === "imperial" ? "70 to 32,800." : "20 to 10,000."}
              >
                <Input
                  id="gnss-move"
                  inputMode="numeric"
                  value={moveText}
                  onChange={(e) => {
                    setMoveText(e.target.value);
                    const n = Number(e.target.value) || 0;
                    setForm({ ...form, location_min_move_m: form.units === "imperial" ? Math.round(n / FEET) : n });
                  }}
                />
              </Field>
            </div>
          )}
          <div className="flex items-center gap-2">
            <Button variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate(form)}>
              Save
            </Button>
            {save.isSuccess && !dirty && <span className="text-xs text-ok">Saved</span>}
          </div>
          <ErrorText error={save.error ?? st.error} />
        </Section>
      )}
    </>
  );
}
