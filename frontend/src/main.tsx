import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import { App } from "./App";
import { applyTheme, getThemePref, migrateLocalStorage } from "./lib/util";
import { ApiError } from "./lib/api";
import { installAudioUnlock } from "./lib/sound";
import "./index.css";

migrateLocalStorage();
// Tells tabs for different MeshHome servers apart.
if (location.hostname) document.title = `MeshHome - ${location.hostname}`;
applyTheme(getThemePref());
installAudioUnlock();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(getThemePref()));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
