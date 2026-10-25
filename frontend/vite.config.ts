import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 后端 API（guard/api.py，8100）经 /guard 代理，避免 CORS；
// Speculos（5001）由浏览器直接访问：它自己放开了 CORS，和 Ledger 官方 workshop 的做法一致。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/guard": { target: "http://127.0.0.1:8100", rewrite: (path) => path.replace(/^\/guard/, "") },
    },
  },
});
