import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const reactRoot = fileURLToPath(new URL("./react/", import.meta.url));

export default defineConfig({
  root: reactRoot,
  base: "/",
  server: {
    proxy: { "/api": "http://127.0.0.1:8080" }
  },
  build: {
    outDir: fileURLToPath(new URL("./dist/", import.meta.url)),
    emptyOutDir: true
  },
  resolve: {
    alias: { "@frontend": frontendRoot }
  }
});
