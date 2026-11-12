import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeftRight, CheckCircle2, Cpu, Loader2, Power, RotateCw, Trash2 } from "lucide-react";
import { api, type Device, type HatFirmware, type RadioHatInfo } from "../lib/api";
import { Dialog } from "./Dialog";
import { Select } from "./SettingsKit";
import { Button, ErrorText, Field } from "./ui";

const BUSY = new Set(["checking", "installing", "rebooting"]);
const EU_FREQ = 869.618;

export function useRadioHat(enabled = true) {
  return useQuery({
    queryKey: ["radio-hat"],
    queryFn: () => api<RadioHatInfo>("/api/system/radio-hat"),
    enabled,
    // Follow the root helper closely while it works; otherwise an occasional refresh is enough.
    refetchInterval: (q) => (q.state.data && (BUSY.has(q.state.data.phase) || q.state.data.request_pending) ? 2000 : 30000),
  });
}

function boardName(b: RadioHatInfo["board"]) {
  return b === "pi5" ? "Raspberry Pi 5" : b === "pi4" ? "Raspberry Pi 4" : "this computer";
}

type InstallBody = { action: "install"; firmware: HatFirmware; slot?: 1 | 2; module?: "13300" | "13302" };

const FIRMWARE_NAME: Record<HatFirmware, string> = { meshcore: "MeshCore (ZephCore)", meshtastic: "Meshtastic (meshtasticd)" };

/** Which RAK module, in which IO slot: meshtasticd needs to be told (ZephCore uses slot 1). */
function SlotFields({
  slot,
  module,
  onSlot,
  onModule,
}: {
  slot: 1 | 2;
  module: "13300" | "13302";
  onSlot: (v: 1 | 2) => void;
  onModule: (v: "13300" | "13302") => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Radio module" htmlFor="hat-module">
        <Select
          id="hat-module"
          value={module}
          options={[
            { value: "13300" as const, label: "RAK13300" },
            { value: "13302" as const, label: "RAK13302 (1 W)" },
          ]}
          onChange={onModule}
        />
      </Field>
      <Field label="In IO slot" htmlFor="hat-slot">
        <Select
          id="hat-slot"
          value={slot}
          options={[
            { value: 1 as const, label: "Slot 1" },
            { value: 2 as const, label: "Slot 2" },
          ]}
          onChange={onSlot}
        />
      </Field>
    </div>
  );
}

/**
 * Settings → Radio connection → "Radio HAT on this Pi": set up, follow, restart, switch or remove
 * the radio software that drives a RAK6421 HAT: ZephCore (MeshCore, MeshHome's radio) or
 * meshtasticd (Meshtastic, for Meshtastic trackers). System changes are made by the installer's
 * root helper; this panel only sends requests and shows its progress.
 */
export function RadioHatPanel({
  connected,
  beforeReboot,
}: {
  connected: boolean;
  // Saves "Radio HAT on this Pi" as the radio mode when it is chosen but not saved yet, so the
  // choice survives the restart.
  beforeReboot?: () => Promise<unknown>;
}) {
  const qc = useQueryClient();
  const hat = useRadioHat();
  const device = useQuery({ queryKey: ["device"], queryFn: () => api<Device>("/api/device"), enabled: connected });
  const [confirm, setConfirm] = useState<null | "reboot" | "remove" | "switch">(null);
  const [choice, setChoice] = useState<HatFirmware>("meshcore");
  const [slot, setSlot] = useState<1 | 2>(1);
  const [module, setModule] = useState<"13300" | "13302">("13300");
  const act = useMutation({
    mutationFn: async (action: "remove" | "restart" | "reboot" | InstallBody) => {
      if (action === "reboot") await beforeReboot?.();
      return api("/api/system/radio-hat", { method: "POST", json: typeof action === "string" ? { action } : action });
    },
    onSuccess: () => {
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["radio-hat"] });
      qc.invalidateQueries({ queryKey: ["radio-settings"] });
      qc.invalidateQueries({ queryKey: ["meshtastic-settings"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  const install = (firmware: HatFirmware) =>
    act.mutate(firmware === "meshtastic" ? { action: "install", firmware, slot, module } : { action: "install", firmware });

  const h = hat.data;
  if (hat.isPending) return <p className="text-sm text-muted">Checking for a radio HAT…</p>;
  if (!h) return <ErrorText error={hat.error} />;

  const pending = h.request_pending || act.isPending;
  const failed = h.last_state === "failed" && !BUSY.has(h.phase);
  const freq = Number(device.data?.radio?.rf?.freq_mhz ?? NaN);
  const fw = h.firmware ?? "meshcore";
  const other: HatFirmware = fw === "meshtastic" ? "meshcore" : "meshtastic";
  const version = fw === "meshtastic" ? h.meshtastic_version?.replace(/~.*/, "") : h.installed_version;

  return (
    <div className="space-y-3 rounded-lg border border-line p-3">
      <p className="flex items-start gap-2 text-sm">
        <Cpu className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
        <span>
          <span className="font-medium">{h.model ?? boardName(h.board)}</span>
          {h.hat_product ? <> · HAT: {h.hat_product}</> : h.board ? <span className="text-muted"> · no HAT reported</span> : null}
        </span>
      </p>

      {!h.available && h.phase !== "absent" && (
        <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-sm text-ink">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          {h.unavailable_reason}
        </p>
      )}

      {h.phase === "absent" && (
        <div className="space-y-3 text-sm">
          <div className="flex gap-1 rounded-lg bg-surface-2 p-1" role="radiogroup" aria-label="What the radio HAT runs">
            {(["meshcore", "meshtastic"] as const).map((k) => (
              <button
                key={k}
                type="button"
                role="radio"
                aria-checked={choice === k}
                onClick={() => setChoice(k)}
                className={
                  "min-h-9 flex-1 rounded-md px-3 text-sm font-medium " + (choice === k ? "bg-surface shadow-sm" : "text-muted")
                }
              >
                {k === "meshcore" ? "MeshCore" : "Meshtastic"}
              </button>
            ))}
          </div>
          {!h.available_firmware?.[choice] ? (
            <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-ink">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
              {h.unavailable_reasons?.[choice] ?? h.unavailable_reason}
            </p>
          ) : choice === "meshcore" ? (
            <>
              <p>
                MeshHome installs <strong>ZephCore</strong>, the MeshCore firmware ported to Linux, to drive the RAK6421 HAT as
                MeshHome's radio. Nothing else to install by hand:
              </p>
              <ul className="list-disc space-y-1 pl-5 text-muted">
                <li>
                  Downloads ZephCore {h.pinned_version ?? ""} for the {boardName(h.board)} from GitHub (about 5 MB, checksum
                  verified)
                </li>
                <li>Turns on SPI if needed: the Pi then needs one restart</li>
                <li>Runs it as its own service that only this Pi can connect to (port 5000)</li>
              </ul>
            </>
          ) : (
            <>
              <p>
                MeshHome installs <strong>meshtasticd</strong>, Meshtastic for Linux, to drive the HAT for{" "}
                <strong>Meshtastic trackers</strong> (Settings → Tracking). The HAT is then not a MeshCore radio: MeshHome's
                messages need another MeshCore radio meanwhile.
              </p>
              <ul className="list-disc space-y-1 pl-5 text-muted">
                <li>Adds Meshtastic's package repository (its signing key is checked) and installs meshtasticd with apt, which keeps it up to date</li>
                <li>Turns on SPI if needed: the Pi then needs one restart</li>
                <li>Runs it as its own service that only this Pi can connect to (port 4403)</li>
              </ul>
              <SlotFields slot={slot} module={module} onSlot={setSlot} onModule={setModule} />
            </>
          )}
          {!h.hat_product && (
            <p className="text-xs text-warn">
              The Pi did not report a HAT. Check that the RAK6421 is seated, with the radio module in its IO slot and the
              antenna attached, before you continue.
            </p>
          )}
          <Button variant="primary" onClick={() => install(choice)} disabled={pending || !h.available_firmware?.[choice]}>
            <Cpu className="size-4" aria-hidden /> Set up the radio HAT
          </Button>
        </div>
      )}

      {BUSY.has(h.phase) && (
        <p className="flex items-center gap-2 text-sm" role="status" aria-live="polite">
          <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
          {h.phase === "rebooting" ? "Restarting the Pi… this page reconnects when it is back." : h.last_message || "Working…"}
        </p>
      )}

      {h.phase === "needs_reboot" && (
        <div className="space-y-2 text-sm">
          <p className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
            Restart the Pi to finish: SPI was just turned on. The radio starts by itself afterwards.
          </p>
          <Button onClick={() => setConfirm("reboot")} disabled={pending}>
            <Power className="size-4" aria-hidden /> Restart the Pi
          </Button>
        </div>
      )}

      {(h.phase === "ready" || h.phase === "stopped") && (
        <div className="space-y-2 text-sm">
          <p className="flex items-start gap-2">
            {h.phase === "ready" ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden />
            ) : (
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
            )}
            <span>
              {h.phase === "ready" ? "Radio software running" : `Radio software stopped (${h.service.state ?? "?"})`} ·{" "}
              {fw === "meshtastic" ? "Meshtastic (meshtasticd)" : "ZephCore"} {version ?? "?"}
              {fw === "meshtastic" && h.meshtastic_module ? ` · RAK${h.meshtastic_module} in slot ${h.meshtastic_slot ?? 1}` : ""} ·{" "}
              {h.host}:{h.port} (this Pi only)
              {h.service.restarts ? <span className="text-muted"> · {h.service.restarts} restarts</span> : null}
            </span>
          </p>
          {fw === "meshtastic" && (
            <p className="text-muted">
              The HAT runs Meshtastic, for Meshtastic trackers (Settings → Tracking → Meshtastic), so it isn't MeshHome's MeshCore
              radio now. Switch it back to MeshCore to use it as that again.
            </p>
          )}
          {fw === "meshcore" && connected && Number.isFinite(freq) && Math.abs(freq - EU_FREQ) < 0.01 && (
            <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
              <span>
                The radio is on {EU_FREQ} MHz, ZephCore's EU/UK default. Outside Europe, set your region's frequency in{" "}
                <strong>Configure node settings → LoRa radio</strong> before sending.
              </span>
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => act.mutate("restart")} disabled={pending}>
              <RotateCw className="size-4" aria-hidden /> Restart radio
            </Button>
            <Button onClick={() => setConfirm("switch")} disabled={pending || !h.available_firmware?.[other]}>
              <ArrowLeftRight className="size-4" aria-hidden /> Switch to {other === "meshtastic" ? "Meshtastic" : "MeshCore"}
            </Button>
            <Button variant="danger" onClick={() => setConfirm("remove")} disabled={pending}>
              <Trash2 className="size-4" aria-hidden /> Remove radio software
            </Button>
          </div>
        </div>
      )}

      {failed && h.last_message && (
        <p role="alert" className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">
          {h.last_message}
        </p>
      )}
      <ErrorText error={act.error} />

      {confirm === "reboot" && (
        <Dialog
          size="sm"
          title="Restart the Pi?"
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="primary" disabled={act.isPending} onClick={() => act.mutate("reboot")}>
                Restart now
              </Button>
            </>
          }
        >
          <p className="text-sm">
            MeshHome and the radio are unavailable for about a minute while the Pi restarts. Messages sent to you
            meanwhile may be missed.
          </p>
        </Dialog>
      )}
      {confirm === "switch" && (
        <Dialog
          title={`Switch the HAT to ${FIRMWARE_NAME[other]}?`}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="primary" disabled={act.isPending} onClick={() => install(other)}>
                Switch
              </Button>
            </>
          }
        >
          <div className="space-y-3 text-sm">
            {other === "meshtastic" ? (
              <>
                <p>
                  The HAT stops being MeshHome's MeshCore radio and runs Meshtastic, so MeshHome can follow Meshtastic trackers
                  through it. Until you switch back, MeshHome sends and receives no MeshCore messages (unless you connect
                  another MeshCore radio).
                </p>
                <p className="text-muted">
                  ZephCore's identity, contacts and channels are kept: switching back restores them. Meshtastic is installed with
                  apt from its own repository the first time (a few minutes).
                </p>
                <SlotFields slot={slot} module={module} onSlot={setSlot} onModule={setModule} />
              </>
            ) : (
              <p>
                The HAT runs ZephCore again and becomes MeshHome's MeshCore radio, with its identity, contacts and channels as
                before. Meshtastic trackers are no longer heard through it; Meshtastic's channels are kept for switching back.
              </p>
            )}
            <ErrorText error={act.error} />
          </div>
        </Dialog>
      )}
      {confirm === "remove" && (
        <Dialog
          size="sm"
          title="Remove the radio software?"
          onClose={() => setConfirm(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)} autoFocus>
                Cancel
              </Button>
              <Button variant="danger" disabled={act.isPending} onClick={() => act.mutate("remove")}>
                Remove
              </Button>
            </>
          }
        >
          <p className="text-sm">
            {fw === "meshtastic" ? "meshtasticd" : "ZephCore"} and its service are removed
            {fw === "meshcore" ? ", and MeshHome loses its radio until you choose another one" : ""}. The radio's identity and
            channels are kept, so setting it up again restores them.
          </p>
        </Dialog>
      )}
    </div>
  );
}
