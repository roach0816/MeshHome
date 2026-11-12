import { lazy, Suspense, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Check,
  ChevronLeft,
  Copy,
  Crosshair,
  ExternalLink,
  Link2,
  Loader2,
  MapPin,
  Pencil,
  Plus,
  QrCode as QrIcon,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { api, type MtChannel, type MtConfig, type MtLinkPreview } from "../lib/api";
import { useStatus } from "../lib/queries";
import { canCopy, canLocate, cx } from "../lib/util";
import { browserLocation, fmtCoord } from "../lib/geo";
import { Dialog } from "./Dialog";
import { QrCode } from "./QrCode";
import { QrScanner } from "./QrScanner";
import { Section, Select } from "./SettingsKit";
import { Badge, Button, Card, ErrorText, Field, IconButton, Input } from "./ui";

// Leaflet is only downloaded when the map picker opens.
const LocationPicker = lazy(() => import("./LocationPicker").then((m) => ({ default: m.LocationPicker })));

const utf8Len = (s: string) => new TextEncoder().encode(s).length;

/** "LONG_FAST" → "Long Fast", "EU_868" → "EU 868". */
export function nice(name: string): string {
  return name
    .split("_")
    .map((w) => (/^[A-Z]{2,3}$/.test(w) && !["LONG", "FAST", "SLOW"].includes(w) ? w : w[0] + w.slice(1).toLowerCase()))
    .join(" ");
}

const ROLE_HINT: Record<string, string> = {
  CLIENT: "A normal node: sends and receives, and relays when needed. Right for most base stations.",
  CLIENT_MUTE: "Sends and receives, but never relays other nodes' packets.",
  CLIENT_BASE: "A base station that reliably relays its favourite nodes' packets (mark your trackers as favourites).",
  CLIENT_HIDDEN: "Transmits only when it has to.",
  ROUTER: "Always relays. Only for a node with a high, clear position; it costs everyone airtime.",
  ROUTER_LATE: "Relays after the other nodes, to fill gaps in coverage.",
  TRACKER: "For trackers: positions first.",
  SENSOR: "For sensors: telemetry first.",
};

const PRECISION: { value: number; label: string }[] = [
  { value: 32, label: "Exact" },
  { value: 13, label: "Approximate (Meshtastic's default)" },
  { value: 0, label: "Not sent" },
];

function precisionLabel(bits: number): string {
  if (bits >= 32) return "Exact positions";
  if (bits === 0) return "No positions";
  return "Approximate positions";
}

function useMtConfig() {
  const status = useStatus();
  const connected = status.data?.radio.state === "connected" && status.data.radio.tech === "meshtastic";
  const q = useQuery({
    queryKey: ["meshtastic-node"],
    queryFn: () => api<MtConfig>("/api/node/meshtastic"),
    enabled: connected,
    retry: false,
  });
  return { ...q, connected };
}

function useSave<T>(path: string) {
  const qc = useQueryClient();
  const [saved, setSaved] = useState(false);
  const m = useMutation({
    mutationFn: (body: T) => api<unknown>(path, { method: "PUT", json: body }),
    onMutate: () => setSaved(false),
    onSuccess: () => {
      setSaved(true);
      qc.invalidateQueries({ queryKey: ["meshtastic-node"] });
      qc.invalidateQueries({ queryKey: ["device"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  return { ...m, saved };
}

function SaveRow({
  dirty,
  pending,
  onSave,
  error,
  saved,
}: {
  dirty: boolean;
  pending: boolean;
  onSave: () => void;
  error: unknown;
  saved: boolean;
}) {
  return (
    <div className="space-y-2">
      <ErrorText error={error} />
      <div className="flex items-center gap-3">
        <Button variant="primary" onClick={onSave} disabled={!dirty || pending}>
          {pending ? "Sending to radio…" : "Save to radio"}
        </Button>
        {saved && !dirty && (
          <span className="text-sm text-ok" role="status">
            Saved on the radio.
          </span>
        )}
      </div>
    </div>
  );
}

/** Settings → Radio → Configure node settings, while MeshHome runs Meshtastic. */
export function MeshtasticNodeSettings() {
  const cfg = useMtConfig();
  const c = cfg.data;
  return (
    <div className="flex h-full flex-col">
      <header className="pwa-top flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
        <Link
          to="/settings/radio"
          className="inline-flex size-11 items-center justify-center rounded-lg text-muted hover:bg-surface-2"
          aria-label="Back to settings"
        >
          <ChevronLeft className="size-6" />
        </Link>
        <div className="min-w-0 flex-1 px-1">
          <h2 className="truncate text-base font-semibold">Node settings</h2>
          <p className="truncate text-xs text-muted">
            {c
              ? `${c.node.long_name || c.node.node_id} · Meshtastic ${c.node.firmware ?? ""}`
              : "Settings stored on the Meshtastic radio"}
          </p>
        </div>
        <Button
          onClick={() => cfg.refetch()}
          disabled={!cfg.connected || cfg.isFetching}
          title="Read settings from the radio again"
        >
          <RefreshCw className={cx("size-4", cfg.isFetching && "animate-spin")} aria-hidden />
          <span className="hidden sm:inline">Reload</span>
        </Button>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {!cfg.connected && (
            <Card className="p-5 text-sm text-muted">
              Node settings can only be read and changed while the radio is connected.{" "}
              <Link to="/settings/radio" className="text-accent underline">
                Check the radio connection
              </Link>
              .
            </Card>
          )}
          {cfg.connected && cfg.isPending && <p className="text-sm text-muted">Reading settings from the radio…</p>}
          <ErrorText error={cfg.error} />
          {c && (
            <>
              <OwnerSection c={c} />
              <LoraSection c={c} />
              <RoleSection c={c} />
              <PositionSection c={c} />
              <ChannelsSection c={c} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function OwnerSection({ c }: { c: MtConfig }) {
  const [long, setLong] = useState(c.node.long_name);
  const [short, setShort] = useState(c.node.short_name);
  useEffect(() => {
    setLong(c.node.long_name);
    setShort(c.node.short_name);
  }, [c.node.long_name, c.node.short_name]);
  const save = useSave<{ long_name: string; short_name: string }>("/api/node/meshtastic/owner");
  const longBytes = utf8Len(long.trim());
  const ok = !!long.trim() && longBytes <= 24 && !!short.trim() && [...short.trim()].length <= 4;
  return (
    <Section title="Name" description={`What other nodes see. Node ${c.node.node_id}.`}>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
        <Field label="Long name" htmlFor="mt-long" hint={`${longBytes} of 24 bytes`}>
          <Input id="mt-long" value={long} onChange={(e) => setLong(e.target.value)} />
        </Field>
        <Field label="Short name" htmlFor="mt-short" hint="Up to 4 characters">
          <Input id="mt-short" value={short} onChange={(e) => setShort(e.target.value)} />
        </Field>
      </div>
      <SaveRow
        dirty={ok && (long.trim() !== c.node.long_name || short.trim() !== c.node.short_name)}
        pending={save.isPending}
        saved={save.saved}
        error={save.error}
        onSave={() => save.mutate({ long_name: long.trim(), short_name: short.trim() })}
      />
    </Section>
  );
}

function LoraSection({ c }: { c: MtConfig }) {
  const qc = useQueryClient();
  const [region, setRegion] = useState(c.lora.region);
  const [preset, setPreset] = useState(c.lora.preset);
  const [hops, setHops] = useState(c.lora.hop_limit);
  const [power, setPower] = useState(String(c.lora.tx_power_dbm));
  const [confirm, setConfirm] = useState(false);
  const [restarting, setRestarting] = useState(false);
  useEffect(() => {
    setRegion(c.lora.region);
    setPreset(c.lora.preset);
    setHops(c.lora.hop_limit);
    setPower(String(c.lora.tx_power_dbm));
  }, [c.lora]);
  const save = useMutation({
    mutationFn: () =>
      api<{ restarts: boolean }>("/api/node/meshtastic/lora", {
        method: "PUT",
        json: {
          region: region === "UNSET" ? null : region,
          preset: preset === "CUSTOM" ? null : preset,
          hop_limit: hops,
          tx_power_dbm: Number(power) || 0,
        },
      }),
    onSuccess: (r) => {
      setConfirm(false);
      setRestarting(r.restarts);
      qc.invalidateQueries({ queryKey: ["meshtastic-node"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  const restarts = region !== c.lora.region || preset !== c.lora.preset;
  const dirty = restarts || hops !== c.lora.hop_limit || Number(power) !== c.lora.tx_power_dbm;
  return (
    <Section
      title="LoRa"
      description="Every node on the mesh, trackers included, needs the same region and modem preset."
      info={
        <>
          <p>
            <strong>Region</strong> sets the frequencies the radio may use. It doesn't transmit until a region is set.
          </p>
          <p>
            <strong>Modem preset</strong> trades range for speed: Long Fast is Meshtastic's default; Long Moderate and Long Slow
            reach further but carry less.
          </p>
          <p>
            <strong>Hops</strong>: how many times a packet may be relayed (3 is the default).
          </p>
        </>
      }
    >
      {c.lora.region === "UNSET" && (
        <p className="rounded-md bg-warn/10 px-3 py-2 text-sm">Choose your region: the radio doesn't transmit until it's set.</p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Region" htmlFor="mt-region">
          <Select
            id="mt-region"
            value={region}
            options={[
              ...(region === "UNSET" ? [{ value: "UNSET", label: "Not set" }] : []),
              ...c.regions.map((r) => ({ value: r, label: nice(r) })),
            ]}
            onChange={setRegion}
          />
        </Field>
        <Field label="Modem preset" htmlFor="mt-preset">
          <Select
            id="mt-preset"
            value={preset}
            options={[
              ...(preset === "CUSTOM" ? [{ value: "CUSTOM", label: "Custom" }] : []),
              ...c.presets.map((p) => ({ value: p, label: nice(p) })),
            ]}
            onChange={setPreset}
          />
        </Field>
        <Field label="Hops" htmlFor="mt-hops">
          <Select
            id="mt-hops"
            value={hops}
            options={[1, 2, 3, 4, 5, 6, 7].map((n) => ({
              value: n,
              label: String(n),
            }))}
            onChange={setHops}
          />
        </Field>
        <Field label="Transmit power (dBm)" htmlFor="mt-power" hint="0: the region's maximum">
          <Input id="mt-power" inputMode="numeric" value={power} onChange={(e) => setPower(e.target.value)} />
        </Field>
      </div>
      {restarting && !dirty && (
        <p className="text-sm text-muted">The radio restarts to apply it, and reconnects in a few seconds.</p>
      )}
      <SaveRow
        dirty={dirty}
        pending={save.isPending}
        saved={save.isSuccess}
        error={save.error}
        onSave={() => (restarts ? setConfirm(true) : save.mutate())}
      />
      {confirm && (
        <Dialog
          size="sm"
          title="Change the region or preset?"
          onClose={() => setConfirm(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(false)} autoFocus>
                Cancel
              </Button>
              <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
                Change it
              </Button>
            </>
          }
        >
          <p className="text-sm">
            The radio restarts to apply it. Nodes still on the old settings can't hear it until they're changed too: the channel
            QR codes below carry the new settings.
          </p>
        </Dialog>
      )}
    </Section>
  );
}

function RoleSection({ c }: { c: MtConfig }) {
  const [role, setRole] = useState(c.node.role);
  useEffect(() => setRole(c.node.role), [c.node.role]);
  const save = useSave<{ role: string }>("/api/node/meshtastic/role");
  return (
    <Section title="Role" description="How this node behaves on the mesh.">
      <Field label="Role" htmlFor="mt-role" hint={ROLE_HINT[role]}>
        <Select
          id="mt-role"
          value={role}
          options={[
            ...(c.roles.includes(role) ? [] : [{ value: role, label: nice(role) }]),
            ...c.roles.map((r) => ({ value: r, label: nice(r) })),
          ]}
          onChange={setRole}
        />
      </Field>
      <SaveRow
        dirty={role !== c.node.role}
        pending={save.isPending}
        saved={save.saved}
        error={save.error}
        onSave={() => save.mutate({ role })}
      />
    </Section>
  );
}

function PositionSection({ c }: { c: MtConfig }) {
  const [lat, setLat] = useState(c.node.lat !== null ? String(c.node.lat) : "");
  const [lon, setLon] = useState(c.node.lon !== null ? String(c.node.lon) : "");
  const [picking, setPicking] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [locError, setLocError] = useState<unknown>(null);
  useEffect(() => {
    setLat(c.node.lat !== null ? String(c.node.lat) : "");
    setLon(c.node.lon !== null ? String(c.node.lon) : "");
  }, [c.node.lat, c.node.lon]);
  const save = useSave<{ lat: number | null; lon: number | null }>("/api/node/meshtastic/position");
  const latN = lat.trim() === "" ? null : Number(lat);
  const lonN = lon.trim() === "" ? null : Number(lon);
  const valid =
    (latN === null && lonN === null) ||
    (latN !== null &&
      lonN !== null &&
      Number.isFinite(latN) &&
      Number.isFinite(lonN) &&
      Math.abs(latN) <= 90 &&
      Math.abs(lonN) <= 180);
  const dirty = latN !== c.node.lat || lonN !== c.node.lon;
  const setPoint = (la: number, lo: number) => {
    setLat(fmtCoord(la));
    setLon(fmtCoord(lo));
  };
  return (
    <Section
      title="Position"
      description="Where this radio is: shown to other nodes, and as the base on MeshHome's maps."
      info="A fixed position, for a radio that doesn't move. Leave both empty for none."
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Latitude" htmlFor="mt-lat">
          <Input id="mt-lat" inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} />
        </Field>
        <Field label="Longitude" htmlFor="mt-lon">
          <Input id="mt-lon" inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => setPicking(true)}>
          <MapPin className="size-4" aria-hidden /> Pick on the map
        </Button>
        {canLocate() && (
          <Button
            onClick={() =>
              browserLocation().then(
                (p) => {
                  setPoint(p.lat, p.lon);
                  setNote(`This device's location (within ${Math.round(p.accuracy)} m). Save to apply it.`);
                  setLocError(null);
                },
                (e) => setLocError(e),
              )
            }
          >
            <Crosshair className="size-4" aria-hidden /> Use this device's location
          </Button>
        )}
        {(lat || lon) && (
          <Button
            variant="ghost"
            onClick={() => {
              setLat("");
              setLon("");
            }}
          >
            Clear
          </Button>
        )}
      </div>
      {note && <p className="text-xs text-muted">{note}</p>}
      <ErrorText error={locError} />
      {!valid && <p className="text-xs text-danger">Enter both latitude and longitude (decimal degrees), or neither.</p>}
      <SaveRow
        dirty={dirty && valid}
        pending={save.isPending}
        saved={save.saved}
        error={save.error}
        onSave={() => save.mutate({ lat: latN, lon: lonN })}
      />
      {picking && (
        <Suspense fallback={null}>
          <LocationPicker
            initial={latN !== null && lonN !== null && valid ? { lat: latN, lon: lonN } : null}
            onClose={() => setPicking(false)}
            onPick={(p) => {
              setPoint(p.lat, p.lon);
              setNote("Picked on the map. Save to apply it.");
              setPicking(false);
            }}
          />
        </Suspense>
      )}
    </Section>
  );
}

// ---- channels -------------------------------------------------------------------------------

function ChannelsSection({ c }: { c: MtConfig }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<MtChannel | "new" | null>(null);
  const [sharing, setSharing] = useState<MtChannel[] | null>(null);
  const [joining, setJoining] = useState(false);
  const [removing, setRemoving] = useState<MtChannel | null>(null);
  const remove = useMutation({
    mutationFn: (ch: MtChannel) => api(`/api/node/meshtastic/channels/${ch.index}`, { method: "DELETE" }),
    onSuccess: () => {
      setRemoving(null);
      qc.invalidateQueries({ queryKey: ["meshtastic-node"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  const full = c.channels.length >= 8;
  const publicPrimary = c.channels.find((ch) => ch.primary && ch.key !== "private");
  return (
    <Section
      title="Channels"
      description="Messages and positions go out on these. Share a channel's QR code to put it on trackers and other nodes."
      info={
        <>
          <p>
            The <strong>primary</strong> channel (slot 0) carries positions and node info. A channel with the{" "}
            <strong>default key</strong> can be read by anyone running Meshtastic nearby; a <strong>private</strong> key only by
            those you share it with.
          </p>
          <p>Channel keys never leave MeshHome's server except inside the share links and QR codes you open.</p>
        </>
      }
    >
      {publicPrimary && (
        <p className="rounded-md bg-warn/10 px-3 py-2 text-sm">
          The primary channel uses Meshtastic's default key: anyone nearby can follow positions sent on it. For trackers, give it
          a private key with exact positions (Edit), then scan its QR code into each tracker.
        </p>
      )}
      <ul className="divide-y divide-line rounded-lg border border-line">
        {c.channels.map((ch) => (
          <li key={ch.index} className="flex flex-wrap items-center gap-2 px-3 py-2">
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2 text-sm font-medium">
                <span className="truncate">{ch.display_name}</span>
                {ch.primary && <Badge>Primary</Badge>}
              </span>
              <span className="block text-xs text-muted">
                Slot {ch.index} ·{" "}
                {ch.key === "private" ? "Private key" : ch.key === "default" ? "Default key (public)" : "No encryption"} ·{" "}
                {precisionLabel(ch.position_precision)}
              </span>
            </span>
            <IconButton label={`QR code for ${ch.display_name}`} onClick={() => setSharing([ch])}>
              <QrIcon className="size-5" aria-hidden />
            </IconButton>
            <IconButton label={`Edit ${ch.display_name}`} onClick={() => setEditing(ch)}>
              <Pencil className="size-5" aria-hidden />
            </IconButton>
            {!ch.primary && (
              <IconButton label={`Remove ${ch.display_name}`} onClick={() => setRemoving(ch)}>
                <Trash2 className="size-5" aria-hidden />
              </IconButton>
            )}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => setEditing("new")} disabled={full}>
          <Plus className="size-4" aria-hidden /> Add a channel
        </Button>
        <Button onClick={() => setJoining(true)}>
          <Link2 className="size-4" aria-hidden /> Join from a link or QR code
        </Button>
        {c.channels.length > 1 && (
          <Button onClick={() => setSharing(c.channels)}>
            <QrIcon className="size-4" aria-hidden /> Share all channels
          </Button>
        )}
      </div>
      {editing && <ChannelDialog ch={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
      {sharing && <ShareDialog channels={sharing} onClose={() => setSharing(null)} />}
      {joining && <JoinDialog onClose={() => setJoining(false)} />}
      {removing && (
        <Dialog
          size="sm"
          title={`Remove ${removing.display_name}?`}
          onClose={() => setRemoving(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setRemoving(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="danger" disabled={remove.isPending} onClick={() => remove.mutate(removing)}>
                Remove
              </Button>
            </>
          }
        >
          <p className="text-sm">The radio leaves the channel. Its messages stay in MeshHome's archive.</p>
          <ErrorText error={remove.error} />
        </Dialog>
      )}
    </Section>
  );
}

function ChannelDialog({ ch, onClose }: { ch: MtChannel | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(ch?.name ?? "");
  const [key, setKey] = useState<"keep" | "random" | "default">(ch ? "keep" : "random");
  const [precision, setPrecision] = useState(ch ? (ch.position_precision >= 32 ? 32 : ch.position_precision === 0 ? 0 : 13) : 32);
  const save = useMutation({
    mutationFn: () =>
      api(ch ? `/api/node/meshtastic/channels/${ch.index}` : "/api/node/meshtastic/channels", {
        method: ch ? "PUT" : "POST",
        json: {
          name: name.trim(),
          key: key === "keep" ? null : key,
          position_precision: precision,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["meshtastic-node"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
    },
  });
  const nameOk = /^[A-Za-z0-9_-]{0,11}$/.test(name.trim()) && (!!ch?.primary || name.trim().length > 0);
  return (
    <Dialog
      title={ch ? `Edit ${ch.display_name}` : "Add a channel"}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!nameOk || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save to radio"}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-sm">
        <Field
          label="Name"
          htmlFor="mt-ch-name"
          hint={
            ch?.primary
              ? "Up to 11 letters, digits, - or _. Empty: named after the preset, as Meshtastic does."
              : "Up to 11 letters, digits, - or _"
          }
        >
          <Input id="mt-ch-name" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Key" htmlFor="mt-ch-key">
          <Select
            id="mt-ch-key"
            value={key}
            options={[
              ...(ch ? [{ value: "keep" as const, label: "Keep its key" }] : []),
              {
                value: "random" as const,
                label: ch ? "A new private key" : "Private (a random key)",
              },
              {
                value: "default" as const,
                label: "Meshtastic's default key (anyone can read it)",
              },
            ]}
            onChange={setKey}
          />
        </Field>
        {ch && key === "random" && (
          <p className="text-xs text-warn">Every node on the channel needs its new QR code: until then they can't read it.</p>
        )}
        <Field label="Positions on this channel" htmlFor="mt-ch-prec">
          <Select id="mt-ch-prec" value={precision} options={PRECISION} onChange={setPrecision} />
        </Field>
        <ErrorText error={save.error} />
      </div>
    </Dialog>
  );
}

function ShareDialog({ channels, onClose }: { channels: MtChannel[]; onClose: () => void }) {
  const indexes = channels.map((c) => c.index).join(",");
  const url = useQuery({
    queryKey: ["meshtastic-share", indexes],
    queryFn: () => api<{ url: string }>(`/api/node/meshtastic/share-url?channels=${indexes}`),
    retry: 2,
  });
  const [copied, setCopied] = useState(false);
  const copy = (text: string) =>
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  const title = channels.length === 1 ? `Channel ${channels[0].display_name}` : "All channels";
  return (
    <Dialog title={title} onClose={onClose} footer={<Button onClick={onClose}>Done</Button>}>
      <div className="space-y-3 text-sm">
        {url.data ? (
          <>
            <QrCode value={url.data.url} label={`QR code for ${title}`} className="mx-auto size-64 rounded-lg" />
            <div className="flex flex-wrap justify-center gap-2">
              <a
                href={url.data.url}
                className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium hover:bg-surface-2"
              >
                <ExternalLink className="size-4" aria-hidden /> Open in Meshtastic
              </a>
              {canCopy() && (
                <Button onClick={() => copy(url.data.url)}>
                  {copied ? <Check className="size-4 text-ok" aria-hidden /> : <Copy className="size-4" aria-hidden />}
                  {copied ? "Copied" : "Copy link"}
                </Button>
              )}
            </div>
          </>
        ) : url.isPending ? (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Reading the channel from the radio…
          </p>
        ) : (
          <ErrorText error={url.error} />
        )}
        <p className="text-xs text-warn">The code holds the channel's key: anyone who scans it can read the channel.</p>
        <ol className="list-decimal space-y-1.5 pl-5">
          <li>
            Connect the Meshtastic app on your phone to the tracker (or other node) by Bluetooth. Then:
            <ul className="mt-1 list-disc space-y-1 pl-5">
              <li>
                <strong>iPhone:</strong> scan this code with the <strong>Camera</strong> app and tap the meshtastic.org link.
              </li>
              <li>
                <strong>Android:</strong> in the Meshtastic app, <strong>Settings → Channels</strong> → scan this code.
              </li>
              <li>
                <strong>On the phone that's showing this page:</strong> select <strong>Open in Meshtastic</strong>.
              </li>
            </ul>
            <p className="mt-1">
              <strong>Replace</strong> makes these its channels and matches its LoRa settings to this radio's: right for your own
              trackers. <strong>Add</strong> keeps its other channels.
            </p>
          </li>
          <li>
            On a tracker: <strong>Position</strong> → GPS on, smart position on, broadcast interval 60–120 s. For a battery
            reading every few minutes, set <strong>Telemetry</strong> → device metrics interval to 5–15 min.
          </li>
          <li>
            A relay at your car: <strong>Device</strong> → role <strong>Router</strong>.
          </li>
        </ol>
      </div>
    </Dialog>
  );
}

function JoinDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [scanning, setScanning] = useState(false);
  const [replace, setReplace] = useState(false);
  const preview = useMutation({
    mutationFn: (u: string) =>
      api<MtLinkPreview>("/api/node/meshtastic/link-preview", {
        json: { url: u },
      }),
  });
  const apply = useMutation({
    mutationFn: () =>
      api<{ added: number; restarts: boolean }>("/api/node/meshtastic/import", {
        json: { url: url.trim(), replace },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["meshtastic-node"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  const look = (u: string) => {
    setUrl(u);
    if (u.includes("meshtastic.org/e/")) preview.mutate(u.trim());
    else preview.reset();
  };
  const p = preview.data;
  return (
    <Dialog
      title="Join channels"
      onClose={onClose}
      footer={
        apply.isSuccess ? (
          <Button onClick={onClose}>Done</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" disabled={!p || apply.isPending} onClick={() => apply.mutate()}>
              {replace ? "Replace the channels" : "Add the channels"}
            </Button>
          </>
        )
      }
    >
      <div className="space-y-3 text-sm">
        {apply.isSuccess ? (
          <p role="status">
            {replace
              ? "The radio's channels are replaced."
              : `Added ${apply.data.added} channel${apply.data.added === 1 ? "" : "s"}.`}
            {apply.data.restarts && " The radio restarts for the new LoRa settings, and reconnects in a few seconds."}
          </p>
        ) : (
          <>
            <Field
              label="Meshtastic channel link"
              htmlFor="mt-join-url"
              hint="https://meshtastic.org/e/#…, as another node or app shares it"
            >
              <Input
                id="mt-join-url"
                value={url}
                onChange={(e) => look(e.target.value)}
                placeholder="https://meshtastic.org/e/#…"
              />
            </Field>
            {scanning ? (
              <QrScanner
                onResult={(t) => {
                  setScanning(false);
                  look(t);
                }}
              />
            ) : (
              <Button onClick={() => setScanning(true)}>
                <QrIcon className="size-4" aria-hidden /> Scan a QR code
              </Button>
            )}
            <ErrorText error={preview.error} />
            {p && (
              <div className="space-y-2 rounded-lg border border-line p-3">
                <p className="font-medium">The link has:</p>
                <ul className="list-disc pl-5">
                  {p.channels.map((ch, i) => (
                    <li key={i}>
                      {ch.name || "an unnamed primary channel"} (
                      {ch.key === "private" ? "private key" : ch.key === "default" ? "default key" : "no encryption"})
                    </li>
                  ))}
                </ul>
                {p.region && (
                  <p className="text-muted">
                    LoRa: {nice(p.region)} · {p.preset ? nice(p.preset) : "?"}
                  </p>
                )}
                <label className="flex cursor-pointer items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5 size-5 accent-[var(--accent)]"
                    checked={replace}
                    onChange={(e) => setReplace(e.target.checked)}
                  />
                  <span>
                    <span className="block font-medium">Replace this radio's channels</span>
                    <span className="block text-xs text-muted">
                      Its channels become the link's, with its LoRa settings (the radio restarts). Otherwise they're added in free
                      slots.
                    </span>
                  </span>
                </label>
              </div>
            )}
            <ErrorText error={apply.error} />
          </>
        )}
      </div>
    </Dialog>
  );
}

/** The conversation list's "Add channel" while MeshHome runs Meshtastic: a new channel on the radio,
 * or another group's channels from their share link or QR code. */
export function MeshtasticAddChannelDialog({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<"choose" | "new" | "join">("choose");
  if (step === "new") return <ChannelDialog ch={null} onClose={onClose} />;
  if (step === "join") return <JoinDialog onClose={onClose} />;
  return (
    <Dialog size="sm" title="Add a channel" onClose={onClose} footer={<Button onClick={onClose}>Cancel</Button>}>
      <div className="grid gap-2">
        <Button onClick={() => setStep("new")}>
          <Plus className="size-4" aria-hidden /> A new channel
        </Button>
        <Button onClick={() => setStep("join")}>
          <Link2 className="size-4" aria-hidden /> Join from a link or QR code
        </Button>
        <p className="text-xs text-muted">
          A Meshtastic radio has up to 8 channels. Manage them, and share their QR codes, in{" "}
          <Link to="/settings/node" className="text-accent underline" onClick={onClose}>
            Node settings
          </Link>
          .
        </p>
      </div>
    </Dialog>
  );
}
