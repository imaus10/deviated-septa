import { neon, neonConfig } from "@neondatabase/serverless";

if (import.meta.env.DEV && import.meta.env.VITE_NEON_FETCH_ENDPOINT) {
  neonConfig.fetchEndpoint = import.meta.env.VITE_NEON_FETCH_ENDPOINT;
}

export const sql = neon(import.meta.env.VITE_NEON_URL);
