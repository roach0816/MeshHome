import { useQuery } from "@tanstack/react-query";
import { api, type SetupStatus, type UpdateInfo } from "./api";

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
