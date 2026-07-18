import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// dev_specs localhost_port_map: 新アプリは次の空き番号。erp5173/beacon5174/scdb5175/
// scdb-chat5176/beacon-chat5177 → ffsim=5178。strictPort でドリフト禁止（allowlist漏れ事故防止）。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5178, strictPort: true },
});
