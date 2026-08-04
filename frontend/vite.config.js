import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import pg from "pg";

const LOCAL_DB_URL =
  process.env.LOCAL_DB_URL ?? "postgresql://localhost/deviated_septa_dev";

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function send(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

function neonLocalShim() {
  let pool = null;

  function getPool() {
    if (!pool) {
      pool = new pg.Pool({
        connectionString: LOCAL_DB_URL,
        types: { getTypeParser: () => (value) => value },
      });
    }
    return pool;
  }

  async function runQuery({ query, params }) {
    const result = await getPool().query({
      text: query,
      values: params ?? [],
      rowMode: "array",
    });
    return {
      fields: result.fields.map((f) => ({
        name: f.name,
        dataTypeID: f.dataTypeID,
      })),
      rows: result.rows,
    };
  }

  return {
    name: "neon-local-shim",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (req.method !== "POST" || !req.url.startsWith("/sql")) {
          return next();
        }
        try {
          const body = await readJson(req);
          if (Array.isArray(body.queries)) {
            const results = [];
            for (const q of body.queries) {
              results.push(await runQuery(q));
            }
            return send(res, 200, { results });
          }
          send(res, 200, await runQuery(body));
        } catch (e) {
          send(res, 400, { message: String((e && e.message) || e) });
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [vue(), neonLocalShim()],
  base: "/deviated-septa/",
  envDir: "..",
});
