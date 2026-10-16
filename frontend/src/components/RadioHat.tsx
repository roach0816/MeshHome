import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Cpu, Loader2, Power, RotateCw, Trash2 } from "lucide-react";
import { api, type Device, type RadioHatInfo } from "../lib/api";
import { Dialog } from "./Dialog";
import { Button, ErrorText } from "./ui";

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

/**
 * Settings → Radio connection → "Radio HAT on this Pi": set up, follow, restart or remove the
 * ZephCore radio software that drives a RAK6421 HAT. System changes are made by the installer's
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
  const [confirm, setConfirm] = useState<null | "reboot" | "remove">(null);
  const act = useMutation({
    mutationFn: async (action: "install" | "remove" | "restart" | "reboot") => {
      if (action === "reboot") await beforeReboot?.();
      return api("/api/system/radio-hat", { method: "POST", json: { action } });
    },
    onSuccess: () => {
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["radio-hat"] });
    },
  });

  const h = hat.data;
  if (hat.isPending) return <p className="text-sm text-muted">Checking for a radio HAT…</p>;
  if (!h) return <ErrorText error={hat.error} />;

  const pending = h.request_pending || act.isPending;
  const failed = h.last_state === "failed" && !BUSY.has(h.phase);
  const freq = Number(device.data?.radio?.rf?.freq_mhz ?? NaN);

  return (
    <div className="space-y-3 rounded-lg border border-line p-3">
      <p className="flex items-start gap-2 text-sm">
        <Cpu className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
        <span>
          <span className="font-medium">{h.model ?? boardName(h.board)}</span>
          {h.hat_product ? <> · HAT: {h.hat_product}</> : h.board ? <span className="text-muted"> · no HAT reported</span> : null}
        </span>
      </p>

      {!h.available && (
        <p className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-sm text-ink">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
          {h.unavailable_reason}
        </p>
      )}

      {h.available && h.phase === "absent" && (
        <div className="space-y-2 text-sm">
          <p>
            MeshHome installs <strong>ZephCore</strong>, the MeshCore firmware ported to Linux, to drive the
            RAK6421 HAT. Nothing else to install by hand:
          </p>
          <ul className="list-disc space-y-1 pl-5 text-muted">
            <li>
              Downloads ZephCore {h.pinned_version ?? ""} for the {boardName(h.board)} from GitHub (about 5 MB, checksum
              verified)
            </li>
            <li>Turns on SPI if needed: the Pi then needs one restart</li>
            <li>Runs it as its own service that only this Pi can connect to (port {h.port})</li>
          </ul>
          {!h.hat_product && (
            <p className="text-xs text-warn">
              The Pi did not report a HAT. Check that the RAK6421 is seated, with the radio module in IO slot 1 and the
              antenna attached, before you continue.
            </p>
          )}
          <Button variant="primary" onClick={() => act.mutate("install")} disabled={pending}>
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
              {h.phase === "ready" ? "Radio software running" : `Radio software stopped (${h.service.state ?? "?"})`} · ZephCore{" "}
              {h.installed_version ?? "?"} · {h.host}:{h.port} (this Pi only)
              {h.service.restarts ? <span className="text-muted"> · {h.service.restarts} restarts</span> : null}
            </span>
          </p>
          {connected && Number.isFinite(freq) && Math.abs(freq - EU_FREQ) < 0.01 && (
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
            ZephCore and its service are removed, and MeshHome loses its radio until you choose another one. The
            radio's identity, contacts and channels are kept, so setting it up again restores them.
          </p>
        </Dialog>
      )}
    </div>
  );
}
