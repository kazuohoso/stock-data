import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// devポートは既存アプリと衝突しない 5185 を strictPort で固定（ERP標準）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5185, strictPort: true },
});
