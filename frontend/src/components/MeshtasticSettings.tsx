import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, QrCode as QrIcon, Radio } from "lucide-react";
import { api, type MeshtasticConfig, type MeshtasticInfo, type MeshtasticSource } from "../lib/api";
import { useRadioHat } from "./RadioHat";
import { Dialog } from "./Dialog";
import { QrCode } from "./QrCode";
import { Select } from "./SettingsKit";
import { Button, ErrorText, Field, Input } from "./ui";

const SOURCES: { value: MeshtasticSource; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "hat", label: "Radio HAT on this Pi (meshtasticd)" },
  { value: "network", label: "A Meshtastic node on the network" },
  { value: "simulated", label: "Simulated (to rehearse)" },
];

/** "LONG_FAST" → "Long Fast", "EU_868" → "EU 868". */
function nice(name: string): string {
  return name
    .split("_")
    .map((w) => (/^[A-Z]{2,3}$/.test(w) && !["LONG", "FAST", "SLOW"].includes(w) ? w : w[0] + w.slice(1).toLowerCase()))
    .join(" ");
}

/**
 * Settings → Tracking → Meshtastic: where Meshtastic trackers are heard, and the private team
 * channel they share with that node (shown as a QR code to scan into each tracker).
 */
export function MeshtasticSection() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["meshtastic-settings"], queryFn: () => api<MeshtasticConfig>("/api/settings/meshtastic") });
  const info = useQuery({
    queryKey: ["meshtastic"],
    queryFn: () => api<MeshtasticInfo>("/api/tracking/meshtastic"),
    refetchInterval: (q) => (q.state.data?.status && q.state.data.status.state !== "connected" ? 3000 : 30000),
  });
  const hat = useRadioHat();
  const [host, setHost] = useState<string | null>(null);
  const [port, setPort] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: MeshtasticConfig) => api<MeshtasticConfig>("/api/settings/meshtastic", { method: "PUT", json: body }),
    onSuccess: (v) => {
      qc.setQueryData(["meshtastic-settings"], v);
      setHost(null);
      setPort(null);
      qc.invalidateQueries({ queryKey: ["meshtastic"] });
      qc.invalidateQueries({ queryKey: ["tracking"] });
    },
  });
  const c = save.isPending && save.variables ? save.variables : cfg.data;
  if (!c) return <ErrorText error={cfg.error} />;
  const st = info.data?.status ?? null;
  const hatIsMeshtastic = hat.data?.firmware === "meshtastic" && hat.data.phase !== "absent";
  const hostValue = host ?? c.host;
  const portValue = port ?? String(c.port);

  return (
    <div className="space-y-3 border-t border-line pt-4">
      <div>
        <h3 className="text-sm font-semibold">Meshtastic trackers</h3>
        <p className="text-xs text-muted">
          Trackers running Meshtastic, such as a SenseCAP X1, broadcast their position on their own. MeshHome hears them through a
          Meshtastic radio and records each position, with its battery and sensor readings.
        </p>
      </div>
      <Field label="Hear Meshtastic trackers through" htmlFor="mt-source">
        <Select
          id="mt-source"
          value={c.source}
          disabled={save.isPending}
          options={SOURCES}
          onChange={(v) =>
            // A network node needs its address first (saved with Connect).
            v === "network" && !c.host ? qc.setQueryData(["meshtastic-settings"], { ...c, source: v }) : save.mutate({ ...c, source: v })
          }
        />
      </Field>
      {c.source === "hat" && !hatIsMeshtastic && (
        <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          <span>
            The radio HAT doesn't run Meshtastic. Switch it in Settings → Radio connection → Radio HAT on this Pi (it then stops
            being MeshHome's MeshCore radio), or use a Meshtastic node on the network.
          </span>
        </p>
      )}
      {c.source === "network" && (
        <div className="grid gap-3 sm:grid-cols-[1fr_7rem_auto] sm:items-end">
          <Field label="Address" htmlFor="mt-host" hint="Its IP address or name. The node needs Wi-Fi or Ethernet turned on.">
            <Input id="mt-host" value={hostValue} placeholder="192.168.1.50" onChange={(e) => setHost(e.target.value)} />
          </Field>
          <Field label="Port" htmlFor="mt-port">
            <Input id="mt-port" inputMode="numeric" value={portValue} onChange={(e) => setPort(e.target.value)} />
          </Field>
          <Button
            variant="primary"
            disabled={save.isPending || !hostValue.trim()}
            onClick={() => save.mutate({ ...c, source: "network", host: hostValue.trim(), port: Number(portValue) || 4403 })}
          >
            Connect
          </Button>
        </div>
      )}
      <ErrorText error={save.error ?? info.error} />
      {st && <MeshtasticStatusCard info={info.data!} />}
    </div>
  );
}

function MeshtasticStatusCard({ info }: { info: MeshtasticInfo }) {
  const st = info.status!;
  const [setup, setSetup] = useState(false);
  const [qr, setQr] = useState(false);
  if (st.state !== "connected") {
    return (
      <p className="flex items-start gap-2 rounded-lg border border-line px-3 py-2 text-sm" role="status">
        {st.state === "error" ? (
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
        ) : (
          <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-accent" aria-hidden />
        )}
        <span>{st.detail ?? "Connecting…"}</span>
      </p>
    );
  }
  const ch = st.channel;
  return (
    <div className="space-y-2 rounded-lg border border-line p-3 text-sm">
      <p className="flex items-start gap-2">
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden />
        <span>
          Connected to <strong>{st.node?.long_name ?? st.node?.node_id}</strong> ({st.node?.node_id}
          {st.node?.firmware ? `, Meshtastic ${st.node.firmware}` : ""}) · {st.region === "UNSET" ? "no region set" : nice(st.region ?? "")} ·{" "}
          {nice(st.preset ?? "")}
        </span>
      </p>
      {ch?.private ? (
        <p className="flex items-start gap-2">
          <Radio className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
          <span>
            Team channel <strong>{ch.name || "(no name)"}</strong>: private
            {ch.precise ? ", positions at full precision" : ", but positions are blurred on it (set precise location in the Meshtastic app)"}.
          </span>
        </p>
      ) : (
        <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          <span>
            It's on Meshtastic's default public channel: positions arrive blurred to about 1.5 km, and anyone nearby can follow them.
            Set up a private team channel and scan it into the trackers.
          </span>
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {ch?.private && (
          <Button variant="primary" onClick={() => setQr(true)}>
            <QrIcon className="size-4" aria-hidden /> Show the team channel QR code
          </Button>
        )}
        <Button variant={ch?.private ? "secondary" : "primary"} onClick={() => setSetup(true)}>
          {ch?.private ? "Make a new team channel" : "Set up a team channel"}
        </Button>
      </div>
      {setup && <TeamChannelDialog info={info} onClose={() => setSetup(false)} onDone={() => setQr(true)} />}
      {qr && <ChannelQrDialog name={ch?.name ?? ""} onClose={() => setQr(false)} />}
    </div>
  );
}

function TeamChannelDialog({ info, onClose, onDone }: { info: MeshtasticInfo; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const st = info.status!;
  const regionNow = info.regions.find((r) => r.name === st.region)?.value;
  const presetNow = info.presets.find((p) => p.name === st.preset)?.value;
  const [name, setName] = useState(st.channel?.private && st.channel.name ? st.channel.name : "MeshHome");
  const [region, setRegion] = useState<number>(regionNow ?? 1);
  const [preset, setPreset] = useState<number>(presetNow ?? 0);
  const valid = /^[A-Za-z0-9_-]{1,11}$/.test(name);
  const go = useMutation({
    mutationFn: () => api("/api/tracking/meshtastic/channel", { json: { name, region, preset } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["meshtastic"] });
      onClose();
      onDone();
    },
  });
  const restarts = region !== regionNow || preset !== presetNow;
  return (
    <Dialog
      title={st.channel?.private ? "Make a new team channel" : "Set up a team channel"}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid || go.isPending} onClick={() => go.mutate()}>
            {go.isPending ? "Setting it up…" : "Set up"}
          </Button>
        </>
      }
    >
      <div className="space-y-4 text-sm">
        <p>
          The Meshtastic radio gets a new private channel with a random key, as its primary channel, with positions at full
          precision. Then scan its QR code into each tracker and the relay at your car: they join it and use the same radio
          settings.
        </p>
        {st.channel?.private && (
          <p className="rounded-md bg-warn/10 px-3 py-2">
            Trackers on the current team channel stop being heard until you scan the new QR code into them.
          </p>
        )}
        <Field label="Channel name" htmlFor="tc-name" hint="Up to 11 letters, digits, - or _.">
          <Input id="tc-name" value={name} maxLength={11} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Region" htmlFor="tc-region" hint="Where you are: the radio only transmits once it's set.">
            <Select id="tc-region" value={region} options={info.regions.map((r) => ({ value: r.value, label: nice(r.name) }))} onChange={setRegion} />
          </Field>
          <Field label="Modem preset" htmlFor="tc-preset" hint="Long Fast is Meshtastic's default; every radio must match.">
            <Select id="tc-preset" value={preset} options={info.presets.map((p) => ({ value: p.value, label: nice(p.name) }))} onChange={setPreset} />
          </Field>
        </div>
        {restarts && <p className="text-xs text-muted">Changing the region or preset restarts the Meshtastic radio (a few seconds).</p>}
        <ErrorText error={go.error} />
      </div>
    </Dialog>
  );
}

function ChannelQrDialog({ name, onClose }: { name: string; onClose: () => void }) {
  const url = useQuery({
    queryKey: ["meshtastic-channel-url"],
    queryFn: () => api<{ url: string }>("/api/tracking/meshtastic/channel-url"),
    retry: 3,
    retryDelay: 2000,
  });
  return (
    <Dialog title={`Team channel ${name}`} onClose={onClose} footer={<Button onClick={onClose}>Done</Button>}>
      <div className="space-y-3 text-sm">
        {url.data ? (
          <QrCode value={url.data.url} label={`QR code for the team channel ${name}`} className="mx-auto size-64 rounded-lg" />
        ) : url.isPending ? (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Reading the channel from the radio…
          </p>
        ) : (
          <ErrorText error={url.error} />
        )}
        <p className="text-xs text-warn">The code holds the channel's key: anyone who scans it can follow your trackers.</p>
        <ol className="list-decimal space-y-1.5 pl-5">
          <li>
            Connect the Meshtastic app on your phone to each tracker (and to the relay at your car) by Bluetooth, open
            <strong> Channels</strong>, scan this code and choose <strong>Replace</strong>.
          </li>
          <li>
            On each tracker: <strong>Position</strong> → GPS on, smart position on, broadcast interval 60–120 s. For a battery
            reading every few minutes, set <strong>Telemetry</strong> → device metrics interval to 5–15 min.
          </li>
          <li>
            The relay at your car: <strong>Device</strong> → role <strong>Router</strong>.
          </li>
        </ol>
      </div>
    </Dialog>
  );
}
