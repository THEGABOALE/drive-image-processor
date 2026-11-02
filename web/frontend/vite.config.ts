import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// En desarrollo la API se alcanza por el proxy: para el navegador es el mismo
// origen, así que no hace falta CORS. Las credenciales nunca se compilan:
// el backend entrega los valores públicos del entorno en ejecución.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  test: { environment: "jsdom", restoreMocks: true },
});
