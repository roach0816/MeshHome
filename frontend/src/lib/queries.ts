import { useQuery } from "@tanstack/react-query";
import { api, type RadioTech, type SetupStatus, type Status, type UpdateInfo } from "./api";

export function useSetupStatus() {
  return useQuery({
    queryKey: ["setup-status"],
    queryFn: () => api<SetupStatus>("/api/setup/status"),
    staleTime: Infinity,
  });
}

export function useUpdateInfo() {
  return useQuery({
    queryKey: ["update-info"],
    queryFn: () => api<UpdateInfo>("/api/system/update"),
    staleTime: 10 * 60_000,
    refetchInterval: 30 * 60_000,
    retry: false,
  });
}

export function useStatus() {
  return useQuery({ queryKey: ["status"], queryFn: () => api<Status>("/api/status"), refetchInterval: 15000 });
}

/** The mesh technology MeshHome runs ("meshcore" until the status has loaded). Features of the
 * other technology are hidden. */
export function useTech(): RadioTech {
  return useStatus().data?.radio_config.tech ?? "meshcore";
}

/** Shown in place of a MeshCore feature while Meshtastic is in use (and the other way round). */
export const TECH_NAME: Record<RadioTech, string> = { meshcore: "MeshCore", meshtastic: "Meshtastic" };
