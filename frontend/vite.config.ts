import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend URL is baked in at build time from VITE_API_BASE. In development
// the proxy below forwards /api to the local FastAPI instead, so `npm run dev`
// needs no CORS configuration and no separate token handling - the dev server
// and the API look like one origin to the browser.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_API ?? "https://iscale-whatsapp-chatbot.onrender.com",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // The console is behind a login and used by a handful of staff; a single
    // small bundle beats code-splitting a two-screen app.
    chunkSizeWarningLimit: 700,
  },
});
