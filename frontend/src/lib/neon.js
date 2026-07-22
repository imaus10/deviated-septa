import { neon } from "@neondatabase/serverless";

export const sql = neon(import.meta.env.VITE_NEON_URL);
