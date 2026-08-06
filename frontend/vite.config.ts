import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 演示模式（--mode demo）构建输出到 dist-demo，避免覆盖真实构建的 dist。
// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    port: 5173,
    host: "localhost",
  },
  build: {
    outDir: mode === "demo" ? "dist-demo" : "dist",
    sourcemap: false,
  },
}));
