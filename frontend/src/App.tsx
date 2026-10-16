import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router";
import { api, ApiError, type Me } from "./lib/api";
import { SetupWizard } from "./pages/SetupWizard";
import { Login } from "./pages/Login";
import { Shell } from "./pages/Shell";

function Splash() {
  return (
    <div className="app-height flex items-center justify-center text-sm text-muted" role="status">
      Loading…
    </div>
  );
}

export function App() {
  const setup = useQuery({
    queryKey: ["setup-status"],
    queryFn: () => api<{ needs_setup: boolean; version: string }>("/api/setup/status"),
  });
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<Me>("/api/auth/me"),
    enabled: setup.data?.needs_setup === false,
    retry: false,
  });

  if (setup.isPending) return <Splash />;
  if (setup.isError)
    return (
      <div className="app-height flex items-center justify-center p-6 text-center text-sm text-muted">
        Cannot reach the MeshHome server. Check that it is running, then reload.
      </div>
    );
  if (setup.data.needs_setup) {
    return (
      <Routes>
        <Route path="/setup" element={<SetupWizard />} />
        <Route path="*" element={<Navigate to="/setup" replace />} />
      </Routes>
    );
  }
  if (me.isPending) return <Splash />;
  const signedOut = me.isError && me.error instanceof ApiError && me.error.status === 401;
  if (signedOut || me.isError) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }
  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="/setup" element={<Navigate to="/" replace />} />
      <Route path="/*" element={<Shell me={me.data} />} />
    </Routes>
  );
}
