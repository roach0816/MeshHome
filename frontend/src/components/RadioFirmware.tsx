import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import { Badge, Button, Card, ErrorText } from "./ui";
import { cx, formatDateTime } from "../lib/util";

export type FirmwareStatus =
  | { available: false; reason: string }
  | {
      available: true;
      checked_at: number | null;
      error: string | null;
      model: string | null;
      current_version: string | null;
      latest: { version: string; published_at: string | null; notes_url: string | null } | null;
      up_to_date: boolean | null;
    };

export function useRadioFirmware(enabled = true) {
  return useQuery({
    queryKey: ["radio-firmware"],
    queryFn: () => api<FirmwareStatus>("/api/radio/firmware"),
    enabled,
    staleTime: 10 * 60_000,
  });
}

/** "Up to date" / "vX available" next to the radio's firmware version. */
export function FirmwareBadge({ s }: { s: FirmwareStatus | undefined }) {
  if (!s?.available || s.up_to_date === null) return null;
  return s.up_to_date ? (
    <Badge tone="ok">Up to date</Badge>
  ) : (
    <Badge tone="accent">v{s.latest?.version} available</Badge>
  );
}

/** Updates → Radio firmware: the radio's MeshCore version compared with MeshCore's latest release. */
export function RadioFirmwareSection() {
  const qc = useQueryClient();
  const fw = useRadioFirmware();
  const check = useMutation({
    mutationFn: () => api<FirmwareStatus>("/api/radio/firmware/check", { method: "POST" }),
    onSuccess: (v) => qc.setQueryData(["radio-firmware"], v),
  });
  const s = fw.data;
  return (
    <Card className="space-y-3 p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold">Radio firmware</h3>
          <p className="mt-0.5 text-sm text-muted">The MeshCore firmware on your radio, compared with MeshCore's latest release.</p>
        </div>
        {s?.available && (
          <Button onClick={() => check.mutate()} disabled={check.isPending} title="Check GitHub for a newer release">
            <RefreshCw className={cx("size-4", check.isPending && "animate-spin")} aria-hidden />
            <span className="hidden sm:inline">Check</span>
          </Button>
        )}
      </div>
      {fw.isPending && <p className="text-sm text-muted">Checking…</p>}
      <ErrorText error={fw.error || check.error} />
      {s && !s.available && <p className="text-sm text-muted">{s.reason}</p>}
      {s?.available && (
        <>
          <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1.5 text-sm">
            <dt className="text-muted">Radio</dt>
            <dd className="min-w-0 break-words">{s.model ?? "Unknown"}</dd>
            <dt className="text-muted">Installed</dt>
            <dd className="flex flex-wrap items-center gap-2">
              {s.current_version ? `v${s.current_version}` : "Unknown"} <FirmwareBadge s={s} />
            </dd>
            <dt className="text-muted">Latest</dt>
            <dd className="min-w-0">
              {s.latest ? (
                <>
                  v{s.latest.version}
                  {s.latest.published_at && <span className="text-muted"> · {formatDateTime(s.latest.published_at)}</span>}
                  {s.latest.notes_url && (
                    <a
                      href={s.latest.notes_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="ml-2 inline-flex items-center gap-1 text-accent underline"
                    >
                      Release notes <ExternalLink className="size-3.5" aria-hidden />
                    </a>
                  )}
                </>
              ) : (
                "—"
              )}
            </dd>
          </dl>
          {s.error && <p className="text-sm text-warn">{s.error}</p>}
          {s.checked_at && <p className="text-xs text-muted">Checked {formatDateTime(s.checked_at)}. MeshCore releases are checked every few hours.</p>}
        </>
      )}
    </Card>
  );
}
