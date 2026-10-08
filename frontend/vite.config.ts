import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In development the two backends are proxied, so the browser talks to one
// origin and CORS never gets in the way:
//   /journey-api/*  -> journey agent  (uvicorn journey_agent.api:app --port 8100)
//   /analyst-api/*  -> data analyst   (uvicorn analyst.api:app --port 8200)
// xfwd passes the visitor's address on, so the analyst can tell whether the Settings page
// is being used from this computer (allowed) or from another one (needs ANALYST_ADMIN_TOKEN).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const journey = env.JOURNEY_BACKEND ?? "http://localhost:8100";
  const analyst = env.ANALYST_BACKEND ?? "http://localhost:8200";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/journey-api": { target: journey, changeOrigin: true, rewrite: (p) => p.replace(/^\/journey-api/, "") },
        "/analyst-api": { target: analyst, changeOrigin: true, xfwd: true, rewrite: (p) => p.replace(/^\/analyst-api/, "") },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        "/journey-api": { target: journey, changeOrigin: true, rewrite: (p) => p.replace(/^\/journey-api/, "") },
        "/analyst-api": { target: analyst, changeOrigin: true, xfwd: true, rewrite: (p) => p.replace(/^\/analyst-api/, "") },
      },
    },
  };
});
