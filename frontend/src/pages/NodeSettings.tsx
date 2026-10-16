import { lazy, Suspense, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { ChevronLeft, Clock, Copy, Crosshair, Loader2, MapPin, Megaphone, Pencil, Plus, Power, RefreshCw, Trash2 } from "lucide-react";
import { api, type ChannelKeyKind, type NodeConfig, type PresetList, type TelemetryMode } from "../lib/api";
import { BANDWIDTHS, CUSTOM, matchPreset } from "../lib/presets";
import { Badge, Button, Card, ErrorText, Field, IconButton, Input } from "../components/ui";
import { Section, Select } from "../components/SettingsKit";
import { Dialog } from "../components/Dialog";
import { AddChannelDialog } from "../components/AddChannel";
import { normaliseScope, scopeError } from "../lib/channelLink";
import { useStatus } from "./Shell";
import { canCopy, canLocate, cx } from "../lib/util";
import { browserLocation, fmtCoord } from "../lib/geo";

// Leaflet is only downloaded when the map picker opens.
const LocationPicker = lazy(() => import("../components/LocationPicker").then((m) => ({ default: m.LocationPicker })));

const TELEMETRY: { value: TelemetryMode; label: string }[] = [
  { value: 0, label: "Nobody" },
  { value: 1, label: "Contacts I allow" },
  { value: 2, label: "Anyone" },
];
const KEY_LABEL: Record<ChannelKeyKind, string> = {
  none: "No key",
  public: "Public key",
  hashtag: "Hashtag key",
  private: "Private key",
};
const utf8Len = (s: string) => new TextEncoder().encode(s).length;

function Toggle({
  id,
  checked,
  onChange,
  label,
  hint,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <label htmlFor={id} className="flex cursor-pointer items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
      />
      <span>
        <span className="block text-sm font-medium">{label}</span>
        {hint && <span className="block text-xs text-muted">{hint}</span>}
      </span>
    </label>
  );
}

function SaveRow({ dirty, pending, onSave, error, saved }: { dirty: boolean; pending: boolean; onSave: () => void; error: unknown; saved: boolean }) {
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

/** Mutation that PUTs a section, replaces the cached config with the radio's response. */
function useSection<T>(path: string) {
  const qc = useQueryClient();
  const [saved, setSaved] = useState(false);
  const m = useMutation({
    mutationFn: (body: T) => api<NodeConfig>(path, { method: "PUT", json: body }),
    onMutate: () => setSaved(false),
    onSuccess: (cfg) => {
      qc.setQueryData(["node-config"], cfg);
      qc.invalidateQueries({ queryKey: ["device"] });
      setSaved(true);
    },
  });
  return { ...m, saved };
}

export function NodeSettings() {
  const status = useStatus();
  const connected = status.data?.radio.state === "connected";
  const cfg = useQuery({
    queryKey: ["node-config"],
    queryFn: () => api<NodeConfig>("/api/radio/config"),
    enabled: connected,
    staleTime: Infinity,
    retry: false,
  });
  const c = cfg.data;
  // Re-read from the radio whenever the connection (re)establishes, e.g. after a reboot.
  const refetch = cfg.refetch;
  useEffect(() => {
    if (connected) refetch();
  }, [connected, refetch]);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-line bg-surface px-1.5 py-1.5 md:px-4">
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
            {c ? `${c.identity.name} · ${c.firmware.model ?? "MeshCore companion"}` : "Settings stored on the radio"}
          </p>
        </div>
        <Button onClick={() => cfg.refetch()} disabled={!connected || cfg.isFetching} title="Read settings from the radio again">
          <RefreshCw className={cx("size-4", cfg.isFetching && "animate-spin")} aria-hidden />
          <span className="hidden sm:inline">Reload</span>
        </Button>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-3 md:p-6">
        <div className="mx-auto max-w-3xl space-y-4">
          {!connected && c && (
            <div className="rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm" role="status">
              The radio is not connected right now{status.data?.radio.state === "backoff" || status.data?.radio.state === "connecting" ? " — reconnecting…" : "."} Showing
              the last settings read from it; saving is disabled until it's back.
            </div>
          )}
          {!connected && !c && (
            <Card className="p-5 text-sm text-muted">
              Node settings can only be read and changed while the radio is connected.{" "}
              <Link to="/settings/radio" className="text-accent underline">
                Check the radio connection
              </Link>
              .
            </Card>
          )}
          {connected && cfg.isPending && <p className="text-sm text-muted">Reading settings from the radio…</p>}
          <ErrorText error={cfg.error} />
          {c && (
            <fieldset disabled={!connected} className="m-0 min-w-0 space-y-4 border-0 p-0 disabled:opacity-60">
              <div className="rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm">
                Changes are written to the radio immediately when you save a section.{" "}
                {c.simulated ? (
                  <Badge tone="warn">Simulated radio</Badge>
                ) : (
                  <span className="text-muted">
                    Remote configuration has not yet been verified on every firmware build. Settings are read back
                    after each save so you can confirm what the radio accepted.
                  </span>
                )}
              </div>
              <IdentitySection c={c} />
              <RadioSection c={c} />
              <ChannelsSection c={c} />
              <BehaviorSection c={c} />
              <TelemetrySection c={c} />
              {c.custom_vars !== null && <CustomVarsSection c={c} />}
              {c.tuning && <TuningSection c={c} />}
              <MaintenanceSection c={c} />
            </fieldset>
          )}
        </div>
      </div>
    </div>
  );
}

// ---- identity -------------------------------------------------------------------------

function IdentitySection({ c }: { c: NodeConfig }) {
  const [name, setName] = useState(c.identity.name);
  const [lat, setLat] = useState(c.identity.lat?.toString() ?? "");
  const [lon, setLon] = useState(c.identity.lon?.toString() ?? "");
  const [share, setShare] = useState(c.identity.share_location);
  useEffect(() => {
    setName(c.identity.name);
    setLat(c.identity.lat?.toString() ?? "");
    setLon(c.identity.lon?.toString() ?? "");
    setShare(c.identity.share_location);
  }, [c.identity]);
  const save = useSection<object>("/api/radio/config/identity");
  const nameBytes = utf8Len(name.trim());
  const latN = lat.trim() === "" ? null : Number(lat);
  const lonN = lon.trim() === "" ? null : Number(lon);
  const coordsOk =
    (latN === null) === (lonN === null) &&
    (latN === null || (Number.isFinite(latN) && latN >= -90 && latN <= 90)) &&
    (lonN === null || (Number.isFinite(lonN) && lonN >= -180 && lonN <= 180));
  const valid = nameBytes > 0 && nameBytes <= 31 && coordsOk;
  const dirty =
    name.trim() !== c.identity.name || latN !== c.identity.lat || lonN !== c.identity.lon || share !== c.identity.share_location;
  const [locating, setLocating] = useState(false);
  const [locError, setLocError] = useState<unknown>(null);
  const [locNote, setLocNote] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const setPoint = (la: number, lo: number) => {
    setLat(fmtCoord(la));
    setLon(fmtCoord(lo));
  };
  const useBrowserLocation = async () => {
    setLocError(null);
    setLocNote(null);
    setLocating(true);
    try {
      const p = await browserLocation();
      setPoint(p.lat, p.lon);
      setLocNote(`From this device, accurate to about ${Math.max(1, Math.round(p.accuracy))} m. Save to apply it.`);
    } catch (e) {
      setLocError(e);
    } finally {
      setLocating(false);
    }
  };
  return (
    <Section title="Identity" description="The name and position this node advertises to the mesh.">
      <Field label="Node name" htmlFor="node-name" error={nameBytes > 31 ? "At most 31 bytes" : null} hint={`${nameBytes}/31 bytes. Shown to others in adverts and channel messages.`}>
        <Input id="node-name" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Latitude" htmlFor="node-lat" error={!coordsOk ? "Enter both, within range, or leave both empty" : null}>
          <Input id="node-lat" inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} />
        </Field>
        <Field label="Longitude" htmlFor="node-lon">
          <Input id="node-lon" inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-wrap gap-2">
        {canLocate() && (
          <Button type="button" onClick={useBrowserLocation} disabled={locating}>
            {locating ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Crosshair className="size-4" aria-hidden />}
            Use this device's location
          </Button>
        )}
        <Button type="button" onClick={() => setPicking(true)}>
          <MapPin className="size-4" aria-hidden /> Pick on map
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            setLat("");
            setLon("");
            setLocNote(null);
          }}
        >
          Clear location
        </Button>
      </div>
      {locNote && (
        <p className="text-xs text-muted" role="status">
          {locNote}
        </p>
      )}
      <ErrorText error={locError} />
      {picking && (
        <Suspense fallback={null}>
          <LocationPicker
            initial={latN !== null && lonN !== null && coordsOk ? { lat: latN, lon: lonN } : null}
            onClose={() => setPicking(false)}
            onPick={(p) => {
              setPoint(p.lat, p.lon);
              setLocNote("Picked on the map. Save to apply it.");
              setLocError(null);
              setPicking(false);
            }}
          />
        </Suspense>
      )}
      <Toggle
        id="node-share"
        checked={share}
        onChange={setShare}
        label="Include location in adverts"
        hint="Other nodes (and maps) will see this position. Leave off to keep it private."
      />
      <SaveRow
        dirty={dirty && valid}
        pending={save.isPending}
        error={save.error}
        saved={save.saved}
        onSave={() => save.mutate({ name: name.trim(), lat: latN, lon: lonN, share_location: share })}
      />
    </Section>
  );
}

// ---- LoRa radio -----------------------------------------------------------------------

function RadioSection({ c }: { c: NodeConfig }) {
  const r = c.radio;
  const qc = useQueryClient();
  const presets = useQuery({
    queryKey: ["radio-presets"],
    queryFn: () => api<PresetList>("/api/radio/presets"),
    staleTime: 3600_000,
  });
  const list = presets.data?.presets ?? [];
  const hashMode = c.behavior.path_hash_mode; // null: firmware without path hash sizes
  const currentHash = hashMode === null ? null : hashMode + 1;
  const [presetId, setPresetId] = useState<string>(CUSTOM);
  const [freq, setFreq] = useState(String(r.freq_mhz));
  const [bw, setBw] = useState(r.bw_khz);
  const [sf, setSf] = useState(r.sf);
  const [cr, setCr] = useState(r.cr);
  const [tx, setTx] = useState(String(r.tx_power_dbm));
  const [repeat, setRepeat] = useState(!!r.repeat);
  const [confirming, setConfirming] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setFreq(String(r.freq_mhz));
    setBw(r.bw_khz);
    setSf(r.sf);
    setCr(r.cr);
    setTx(String(r.tx_power_dbm));
    setRepeat(!!r.repeat);
    setPresetId(matchPreset(list, r.freq_mhz, r.bw_khz, r.sf, r.cr, currentHash)?.id ?? CUSTOM);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [r, presets.data, currentHash]);

  const preset = list.find((p) => p.id === presetId);
  const custom = !preset;
  const choose = (id: string) => {
    setPresetId(id);
    const p = list.find((x) => x.id === id);
    if (p) {
      setFreq(String(p.freq_mhz));
      setBw(p.bw_khz);
      setSf(p.sf);
      setCr(p.cr);
    }
  };
  // A preset may also define the mesh's path hash size (Settings → Contacts & routing).
  const targetHash = preset?.path_hash_size ?? null;
  const hashChange = targetHash !== null && hashMode !== null && targetHash !== currentHash;

  const maxTx = r.max_tx_power_dbm ?? 30;
  const freqN = Number(freq);
  const txN = Number(tx);
  const valid = Number.isFinite(freqN) && freqN >= 150 && freqN <= 2500 && Number.isInteger(txN) && txN >= 1 && txN <= maxTx;
  const rfChanged = freqN !== r.freq_mhz || bw !== r.bw_khz || sf !== r.sf || cr !== r.cr;
  const dirty = rfChanged || hashChange || txN !== r.tx_power_dbm || (r.repeat !== null && repeat !== r.repeat);
  const body = { freq_mhz: freqN, bw_khz: bw, sf, cr, tx_power_dbm: txN, repeat: r.repeat === null ? null : repeat };
  const save = useMutation({
    mutationFn: async () => {
      let cfg = await api<NodeConfig>("/api/radio/config/radio", { method: "PUT", json: body });
      if (hashChange && targetHash !== null) {
        const b = cfg.behavior;
        cfg = await api<NodeConfig>("/api/radio/config/behavior", {
          method: "PUT",
          json: { auto_add_contacts: b.auto_add_contacts, multi_acks: b.multi_acks, path_hash_mode: targetHash - 1, default_flood_scope: null },
        });
      }
      return cfg;
    },
    onMutate: () => setSaved(false),
    onSuccess: (cfg) => {
      qc.setQueryData(["node-config"], cfg);
      qc.invalidateQueries({ queryKey: ["device"] });
      setSaved(true);
    },
  });
  // Keep the current value selectable even if it isn't a standard option.
  const bwOptions = (BANDWIDTHS.includes(bw) ? BANDWIDTHS : [...BANDWIDTHS, bw].sort((a, b2) => a - b2)).map((b2) => ({
    value: b2,
    label: `${b2} kHz`,
  }));
  return (
    <Section
      title="LoRa radio"
      description="Must match the rest of your mesh."
      info="If these differ from the other nodes in your mesh, this node stops hearing them. Changes apply immediately."
    >
      <Field
        label="Preset"
        htmlFor="rf-preset"
        hint={
          presets.isPending
            ? "Loading MeshCore's presets…"
            : `${presets.data?.info_message ?? "Suggested by the MeshCore community."} ${
                presets.data?.source === "live" ? "Up to date from MeshCore." : `Built-in list${presets.data?.updated ? ` (${presets.data.updated})` : ""}.`
              } Choose Custom to set your own.`
        }
      >
        <select
          id="rf-preset"
          value={presetId}
          onChange={(e) => choose(e.target.value)}
          className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink sm:text-sm"
        >
          {list.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title} · {p.freq_mhz} MHz · {p.bw_khz} kHz · SF{p.sf} · CR{p.cr}
              {p.path_hash_size ? ` · ${p.path_hash_size}-byte paths` : ""}
            </option>
          ))}
          <option value={CUSTOM}>Custom</option>
        </select>
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Frequency (MHz)"
          htmlFor="rf-freq"
          hint={custom ? "Use a frequency that is legal where you are." : undefined}
          error={freq && !(freqN >= 150 && freqN <= 2500) ? "150–2500 MHz" : null}
        >
          <Input id="rf-freq" inputMode="decimal" disabled={!custom} value={freq} onChange={(e) => setFreq(e.target.value)} />
        </Field>
        <Field label="Bandwidth" htmlFor="rf-bw">
          <Select id="rf-bw" value={bw} options={bwOptions} onChange={setBw} disabled={!custom} />
        </Field>
        <Field label="Spreading factor" htmlFor="rf-sf" hint={custom ? "Higher = longer range, slower." : undefined}>
          <Select id="rf-sf" value={sf} options={[5, 6, 7, 8, 9, 10, 11, 12].map((v) => ({ value: v, label: `SF${v}` }))} onChange={setSf} disabled={!custom} />
        </Field>
        <Field label="Coding rate" htmlFor="rf-cr">
          <Select id="rf-cr" value={cr} options={[5, 6, 7, 8].map((v) => ({ value: v, label: `4/${v}` }))} onChange={setCr} disabled={!custom} />
        </Field>
        <Field label={`TX power (dBm, max ${maxTx})`} htmlFor="rf-tx" error={tx && !(txN >= 1 && txN <= maxTx) ? `1–${maxTx} dBm` : null}>
          <Input id="rf-tx" inputMode="numeric" value={tx} onChange={(e) => setTx(e.target.value)} />
        </Field>
      </div>
      {preset && hashChange && (
        <p className="text-xs text-muted">
          This preset also sets the mesh's path hash size to {targetHash} bytes (Contacts & routing).
        </p>
      )}
      {r.repeat !== null && (
        <Toggle
          id="rf-repeat"
          checked={repeat}
          onChange={setRepeat}
          label="Client repeat"
          hint="This companion also repeats packets (only on frequencies the firmware allows)."
        />
      )}
      <SaveRow
        dirty={dirty && valid}
        pending={save.isPending}
        error={save.error}
        saved={saved}
        onSave={() => (rfChanged || hashChange ? setConfirming(true) : save.mutate())}
      />
      {confirming && (
        <Dialog
          size="sm"
          title="Change LoRa parameters?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirming(false);
                  save.mutate();
                }}
              >
                Apply to radio
              </Button>
            </>
          }
        >
          <div className="space-y-2 text-sm">
            <p>
              {preset ? <strong>{preset.title}: </strong> : null}
              {freqN} MHz · {bw} kHz · SF{sf} · CR 4/{cr}
              {hashChange ? ` · ${targetHash}-byte path hashes` : ""}
            </p>
            <p className="text-muted">
              If these don't match your mesh, this node will go silent to everyone else until you change them back.
              The app's connection to the radio is over the network, so you can always return here to fix it.
            </p>
          </div>
        </Dialog>
      )}
    </Section>
  );
}

// ---- channels -------------------------------------------------------------------------

type ChannelEdit = { slot: number; name: string; key: ChannelKeyKind; scope: string };

function ChannelsSection({ c }: { c: NodeConfig }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<ChannelEdit | null>(null);
  const [adding, setAdding] = useState(false);
  const [clearing, setClearing] = useState<{ slot: number; name: string } | null>(null);
  const [newKey, setNewKey] = useState<{ name: string; hex: string; base64: string } | null>(null);
  const bySlot = new Map(c.channels.map((ch) => [ch.slot, ch]));
  const slots = Array.from(
    { length: c.max_channels },
    (_, i) => bySlot.get(i) ?? { slot: i, name: "", key: "none" as ChannelKeyKind, flood_scope: null },
  );
  const used = slots.filter((s) => s.name);
  const firstFree = slots.find((s) => !s.name);
  const clear = useMutation({
    mutationFn: (slot: number) => api<NodeConfig>(`/api/radio/channels/${slot}`, { method: "DELETE" }),
    onSuccess: (cfg) => {
      qc.setQueryData(["node-config"], cfg);
      qc.invalidateQueries({ queryKey: ["conversations"] });
      setClearing(null);
    },
  });
  return (
    <Section
      title="Channels"
      description={`${used.length} of ${c.max_channels} slots used. Keys are write-only: this app never displays an existing channel key.`}
    >
      <ul className="divide-y divide-line rounded-lg border border-line">
        {used.map((ch) => (
          <li key={ch.slot} className="flex items-center gap-3 px-3 py-2">
            <span className="w-14 shrink-0 text-xs text-muted">Slot {ch.slot}</span>
            <span className="min-w-16 flex-1">
              <span className="block truncate text-sm font-medium">{ch.name}</span>
              {ch.flood_scope && <span className="block truncate text-xs text-muted">Region #{ch.flood_scope}</span>}
            </span>
            <Badge tone={ch.key === "private" ? "accent" : "muted"} className="hidden whitespace-nowrap sm:inline-flex">
              {KEY_LABEL[ch.key]}
            </Badge>
            <IconButton
              label={`Edit ${ch.name}`}
              onClick={() => setEditing({ slot: ch.slot, name: ch.name, key: ch.key, scope: ch.flood_scope ?? "" })}
            >
              <Pencil className="size-4" />
            </IconButton>
            <IconButton label={`Remove ${ch.name}`} onClick={() => setClearing({ slot: ch.slot, name: ch.name })}>
              <Trash2 className="size-4" />
            </IconButton>
          </li>
        ))}
        {used.length === 0 && <li className="px-3 py-3 text-sm text-muted">No channels configured.</li>}
      </ul>
      <Button disabled={!firstFree} onClick={() => setAdding(true)}>
        <Plus className="size-4" aria-hidden /> Add channel
      </Button>
      {adding && <AddChannelDialog openOnAdd={false} onClose={() => setAdding(false)} />}
      <ErrorText error={clear.error} />
      {editing && (
        <ChannelDialog
          edit={editing}
          onClose={() => setEditing(null)}
          onSaved={(key, name) => {
            setEditing(null);
            if (key) setNewKey({ name, ...key });
          }}
        />
      )}
      {clearing && (
        <Dialog
          size="sm"
          title={`Remove ${clearing.name} from the radio?`}
          onClose={() => setClearing(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setClearing(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="danger" disabled={clear.isPending} onClick={() => clear.mutate(clearing.slot)}>
                Remove channel
              </Button>
            </>
          }
        >
          <p className="text-sm">
            The radio stops receiving and sending on this channel. Messages already archived in this app are kept
            (the conversation is marked archived). Re-adding it later needs the channel key again.
          </p>
        </Dialog>
      )}
      {newKey && (
        <Dialog size="sm" title={`Key for ${newKey.name}`} onClose={() => setNewKey(null)} footer={<Button variant="primary" onClick={() => setNewKey(null)}>I've saved it</Button>}>
          <div className="space-y-3 text-sm">
            <p>
              Share this key with the people who should join this channel. <strong>It is shown only once</strong> — the
              app does not store it.
            </p>
            {(["hex", "base64"] as const).map((fmt) => (
              <div key={fmt}>
                <p className="mb-1 text-xs uppercase tracking-wide text-muted">{fmt}</p>
                <div className="flex items-center gap-1">
                  <code className="min-w-0 flex-1 break-all rounded-md bg-surface-2 px-2 py-1.5 font-mono text-xs">{newKey[fmt]}</code>
                  {canCopy() && (
                  <IconButton label={`Copy ${fmt} key`} onClick={() => navigator.clipboard?.writeText(newKey[fmt]).catch(() => {})}>
                    <Copy className="size-4" />
                  </IconButton>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Dialog>
      )}
    </Section>
  );
}

type KeyMode = "keep" | "hashtag" | "public" | "random" | "custom";

function ChannelDialog({
  edit,
  onClose,
  onSaved,
}: {
  edit: ChannelEdit;
  onClose: () => void;
  onSaved: (key: { hex: string; base64: string } | null, name: string) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(edit.name);
  const [mode, setMode] = useState<KeyMode>(edit.key === "hashtag" ? "hashtag" : "keep");
  const [key, setKey] = useState("");
  const [scope, setScope] = useState(edit.scope);
  const isHashtag = name.trim().startsWith("#");
  // Hashtag names always derive their key from the name; keep the mode consistent with the name.
  const effectiveMode: KeyMode = isHashtag ? "hashtag" : mode === "hashtag" ? "keep" : mode;
  const save = useMutation({
    mutationFn: () =>
      api<{ config: NodeConfig; new_key?: { hex: string; base64: string } }>(`/api/radio/channels/${edit.slot}`, {
        method: "PUT",
        json: {
          name: name.trim(),
          key_mode: effectiveMode,
          key: effectiveMode === "custom" ? key.trim() : null,
          flood_scope: normaliseScope(scope),
        },
      }),
    onSuccess: (r) => {
      qc.setQueryData(["node-config"], r.config);
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onSaved(r.new_key ?? null, name.trim());
    },
  });
  const bytes = utf8Len(name.trim());
  const valid =
    bytes > 0 && bytes <= 31 && (effectiveMode !== "custom" || key.trim().length > 0) && !scopeError(scope);
  const modes: { value: KeyMode; label: string; hint: string; show: boolean }[] = [
    { value: "keep", label: "Keep the current key", hint: "Rename only.", show: !isHashtag },
    { value: "random", label: "Generate a new private key", hint: "Shown once so you can share it.", show: !isHashtag },
    { value: "custom", label: "Enter a key", hint: "Join an existing private channel (32 hex characters or base64).", show: !isHashtag },
    { value: "public", label: "MeshCore Public key", hint: "The well-known key every companion ships with.", show: !isHashtag },
  ];
  return (
    <Dialog
      title={`Edit ${edit.name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save to radio"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field
          label="Channel name"
          htmlFor="ch-name"
          hint={'Start with "#" for a hashtag channel: anyone who knows the name can join, because the key comes from the name.'}
          error={bytes > 31 ? "At most 31 bytes" : null}
        >
          <Input id="ch-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="#hiking or Family" />
        </Field>
        {isHashtag ? (
          <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">Key: derived from the name “{name.trim()}”.</p>
        ) : (
          <fieldset className="space-y-2">
            <legend className="mb-1 text-sm font-medium">Key</legend>
            {modes
              .filter((m) => m.show)
              .map((m) => (
                <label
                  key={m.value}
                  className={cx(
                    "flex cursor-pointer gap-3 rounded-lg border p-3",
                    effectiveMode === m.value ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
                  )}
                >
                  <input
                    type="radio"
                    name="key-mode"
                    checked={effectiveMode === m.value}
                    onChange={() => setMode(m.value)}
                    className="mt-1 accent-[var(--accent)]"
                  />
                  <span>
                    <span className="block text-sm font-medium">{m.label}</span>
                    <span className="block text-xs text-muted">{m.hint}</span>
                  </span>
                </label>
              ))}
            {effectiveMode === "custom" && (
              <Input
                aria-label="Channel key"
                spellCheck={false}
                autoCapitalize="none"
                className="font-mono"
                placeholder="32 hex characters or base64"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            )}
          </fieldset>
        )}
        <Field
          label="Region scope"
          htmlFor="ch-scope"
          error={scopeError(scope)}
          hint="Empty uses the radio's default."
          info="Messages you send on this channel only flood through repeaters serving this region."
        >
          <Input id="ch-scope" placeholder="#region" value={scope} onChange={(e) => setScope(e.target.value)} />
        </Field>
        <ErrorText error={save.error} />
      </div>
    </Dialog>
  );
}

// ---- behaviour ------------------------------------------------------------------------

function BehaviorSection({ c }: { c: NodeConfig }) {
  const b = c.behavior;
  const [autoAdd, setAutoAdd] = useState(b.auto_add_contacts);
  const [multi, setMulti] = useState(b.multi_acks > 0);
  const [hash, setHash] = useState(b.path_hash_mode ?? 0);
  const [scope, setScope] = useState(b.default_flood_scope ?? "");
  useEffect(() => {
    setAutoAdd(b.auto_add_contacts);
    setMulti(b.multi_acks > 0);
    setHash(b.path_hash_mode ?? 0);
    setScope(b.default_flood_scope ?? "");
  }, [b]);
  const save = useSection<object>("/api/radio/config/behavior");
  const dirty =
    autoAdd !== b.auto_add_contacts ||
    multi !== b.multi_acks > 0 ||
    (b.path_hash_mode !== null && hash !== b.path_hash_mode) ||
    (b.default_flood_scope !== null && scope.trim().replace(/^#/, "") !== b.default_flood_scope);
  return (
    <Section title="Contacts & routing">
      <Toggle
        id="bh-autoadd"
        checked={autoAdd}
        onChange={setAutoAdd}
        label="Automatically add contacts from adverts"
        hint="Off keeps the radio's small contact list tidy: new nodes wait for you."
      />
      <Toggle
        id="bh-multiack"
        checked={multi}
        onChange={setMulti}
        label="Send extra acknowledgements"
        hint="Improves DM delivery confirmation on lossy links, at the cost of a little more airtime."
      />
      {b.path_hash_mode !== null && (
        <Field label="Path hash size" htmlFor="bh-hash" hint="Must be supported by the repeaters your messages pass through.">
          <Select
            id="bh-hash"
            value={hash}
            options={[
              { value: 0, label: "1 byte (default)" },
              { value: 1, label: "2 bytes" },
              { value: 2, label: "3 bytes" },
            ]}
            onChange={setHash}
          />
        </Field>
      )}
      {b.default_flood_scope !== null && (
        <Field label="Default flood scope" htmlFor="bh-scope" hint="Limit flooded messages to repeaters in this region scope. Leave empty for no scope.">
          <Input id="bh-scope" placeholder="e.g. #boulder" value={scope} onChange={(e) => setScope(e.target.value)} />
        </Field>
      )}
      <SaveRow
        dirty={dirty}
        pending={save.isPending}
        error={save.error}
        saved={save.saved}
        onSave={() =>
          save.mutate({
            auto_add_contacts: autoAdd,
            multi_acks: multi ? 1 : 0,
            path_hash_mode: b.path_hash_mode === null ? null : hash,
            default_flood_scope: b.default_flood_scope === null ? null : scope.trim(),
          })
        }
      />
    </Section>
  );
}

// ---- telemetry ------------------------------------------------------------------------

function TelemetrySection({ c }: { c: NodeConfig }) {
  const t = c.telemetry;
  const [v, setV] = useState(t);
  useEffect(() => setV(t), [t]);
  const save = useSection<object>("/api/radio/config/telemetry");
  const dirty = v.base !== t.base || v.location !== t.location || v.environment !== t.environment;
  const row = (key: keyof typeof v, label: string, hint: string) => (
    <Field label={label} htmlFor={`tm-${key}`} hint={hint}>
      <Select id={`tm-${key}`} value={v[key]} options={TELEMETRY} onChange={(x) => setV({ ...v, [key]: x })} />
    </Field>
  );
  return (
    <Section title="Telemetry sharing" description="Who may request telemetry from this node over the mesh.">
      <div className="grid gap-3 sm:grid-cols-3">
        {row("base", "Basic", "Battery and similar.")}
        {row("location", "Location", "GPS position, if fitted.")}
        {row("environment", "Environment", "Attached sensors.")}
      </div>
      <SaveRow dirty={dirty} pending={save.isPending} error={save.error} saved={save.saved} onSave={() => save.mutate(v)} />
    </Section>
  );
}

// ---- custom variables -----------------------------------------------------------------

function CustomVarsSection({ c }: { c: NodeConfig }) {
  const qc = useQueryClient();
  const vars = c.custom_vars ?? {};
  const [drafts, setDrafts] = useState<Record<string, string>>(vars);
  useEffect(() => setDrafts(c.custom_vars ?? {}), [c.custom_vars]);
  const save = useMutation({
    mutationFn: (kv: { key: string; value: string }) => api<NodeConfig>("/api/radio/custom-vars", { method: "PUT", json: kv }),
    onSuccess: (cfg) => qc.setQueryData(["node-config"], cfg),
  });
  const keys = Object.keys(vars);
  return (
    <Section title="Firmware variables" description="Board-specific settings exposed by this firmware build (for example GPS).">
      {keys.length === 0 && <p className="text-sm text-muted">This firmware exposes no variables.</p>}
      <div className="space-y-2">
        {keys.map((k) => (
          <div key={k} className="flex items-end gap-2">
            <Field label={k} htmlFor={`cv-${k}`}>
              <Input id={`cv-${k}`} value={drafts[k] ?? ""} onChange={(e) => setDrafts({ ...drafts, [k]: e.target.value })} />
            </Field>
            <Button disabled={drafts[k] === vars[k] || save.isPending} onClick={() => save.mutate({ key: k, value: drafts[k] ?? "" })}>
              Save
            </Button>
          </div>
        ))}
      </div>
      <ErrorText error={save.error} />
    </Section>
  );
}

// ---- tuning ---------------------------------------------------------------------------

function TuningSection({ c }: { c: NodeConfig }) {
  const t = c.tuning!;
  const [rx, setRx] = useState(String(t.rx_delay));
  const [af, setAf] = useState(String(t.airtime_factor));
  useEffect(() => {
    setRx(String(t.rx_delay));
    setAf(String(t.airtime_factor));
  }, [t]);
  const save = useSection<object>("/api/radio/config/tuning");
  const rxN = Number(rx);
  const afN = Number(af);
  const valid = rxN >= 0 && rxN <= 20 && afN >= 0 && afN <= 9;
  return (
    <Section title="Advanced timing" description="Leave these at their defaults unless you know your mesh needs otherwise.">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="RX delay base" htmlFor="tn-rx" hint="0–20. 0 disables the receive delay." error={rx && !(rxN >= 0 && rxN <= 20) ? "0–20" : null}>
          <Input id="tn-rx" inputMode="decimal" value={rx} onChange={(e) => setRx(e.target.value)} />
        </Field>
        <Field label="Airtime factor" htmlFor="tn-af" hint="0–9. Default 1.0." error={af && !(afN >= 0 && afN <= 9) ? "0–9" : null}>
          <Input id="tn-af" inputMode="decimal" value={af} onChange={(e) => setAf(e.target.value)} />
        </Field>
      </div>
      <SaveRow
        dirty={valid && (rxN !== t.rx_delay || afN !== t.airtime_factor)}
        pending={save.isPending}
        error={save.error}
        saved={save.saved}
        onSave={() => save.mutate({ rx_delay: rxN, airtime_factor: afN })}
      />
    </Section>
  );
}

// ---- maintenance ----------------------------------------------------------------------

function MaintenanceSection({ c }: { c: NodeConfig }) {
  const qc = useQueryClient();
  const [confirmReboot, setConfirmReboot] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: object; done: string }) =>
      api(`/api/radio/actions/${path}`, { method: "POST", json: body ?? {} }),
    onSuccess: (_r, v) => {
      setNote(v.done);
      if (v.path === "reboot") qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  return (
    <Section title="Actions">
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => action.mutate({ path: "advert", body: { flood: false }, done: "Advert sent to nearby nodes." })}>
          <Megaphone className="size-4" aria-hidden /> Send advert (nearby)
        </Button>
        <Button onClick={() => action.mutate({ path: "advert", body: { flood: true }, done: "Advert flooded across the mesh." })}>
          <Megaphone className="size-4" aria-hidden /> Send flood advert
        </Button>
        <Button onClick={() => action.mutate({ path: "sync-clock", done: "Radio clock set to this server's time." })}>
          <Clock className="size-4" aria-hidden /> Sync clock
        </Button>
        <Button variant="danger" onClick={() => setConfirmReboot(true)}>
          <Power className="size-4" aria-hidden /> Reboot radio
        </Button>
      </div>
      {note && (
        <p className="text-sm text-ok" role="status">
          {note}
        </p>
      )}
      <ErrorText error={action.error} />
      <p className="text-xs text-muted">
        Factory reset and private-key export/import are intentionally not available here; use the official MeshCore
        tools for those so the node's identity is never handled by this app.
      </p>
      {confirmReboot && (
        <Dialog
          size="sm"
          title={`Reboot ${c.identity.name}?`}
          onClose={() => setConfirmReboot(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirmReboot(false)} autoFocus>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirmReboot(false);
                  action.mutate({ path: "reboot", done: "Rebooting. The app will reconnect automatically." });
                }}
              >
                Reboot
              </Button>
            </>
          }
        >
          <p className="text-sm">
            The radio restarts and the connection drops for a short while. Messages received during the restart wait
            on the radio until the app reconnects.
          </p>
        </Dialog>
      )}
    </Section>
  );
}
