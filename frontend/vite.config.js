import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,          // Listen on 0.0.0.0 so tunnels can reach us
    // Let Onshape / trycloudflare tunnel iframes load the panel
    allowedHosts: [
      "localhost",
      ".trycloudflare.com",
      ".ngrok-free.app",
      ".ngrok.io",
    ],
    proxy: {
      "/api": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
    },
  },
});
