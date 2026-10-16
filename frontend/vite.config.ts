import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In development the Vite server proxies API and WebSocket traffic to the FastAPI backend,
// keeping everything same-origin exactly as it is in production.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8080";

/**
 * `virtual:emoji-data`: the Unicode emoji list compacted at build time to
 * [[group, "emoji\tname\temoji\tname…"], …]. Emoji newer than 15.0 are left out because many
 * operating systems (and older Pi images) cannot draw them yet. Imported lazily by the picker.
 */
function emojiData(): Plugin {
  const id = "virtual:emoji-data";
  return {
    name: "emoji-data",
    resolveId: (source) => (source === id ? "\0" + id : null),
    load(resolved) {
      if (resolved !== "\0" + id) return null;
      const file = createRequire(import.meta.url).resolve("unicode-emoji-json/data-by-group.json");
      const groups = JSON.parse(readFileSync(file, "utf8")) as {
        name: string;
        emojis: { emoji: string; name: string; emoji_version: string }[];
      }[];
      const compact = groups.map((g) => [
        g.name,
        g.emojis
          .filter((e) => parseFloat(e.emoji_version) <= 15.0)
          .map((e) => `${e.emoji}\t${e.name}`)
          .join("\t"),
      ]);
      return `export default ${JSON.stringify(compact)};`;
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), emojiData()],
  server: {
    proxy: {
      "/api": { target: backend },
      "/health": { target: backend },
      "/ws": { target: backend, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
