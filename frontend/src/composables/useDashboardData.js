import { ref, onMounted, onUnmounted } from "vue";
import { sql } from "../lib/neon.js";

const POLL_INTERVAL = 60_000;

export function useDashboardData() {
  const snapshot = ref([]);
  const loading = ref(true);
  const error = ref(null);

  let timer = null;

  async function fetchSnapshot() {
    try {
      const rows = await sql`SELECT * FROM latest_snapshot ORDER BY route_id`;
      snapshot.value = rows;
      loading.value = false;
      error.value = null;
    } catch (e) {
      error.value = e.message;
    }
  }

  onMounted(() => {
    fetchSnapshot();
    timer = setInterval(fetchSnapshot, POLL_INTERVAL);
  });

  onUnmounted(() => {
    if (timer) clearInterval(timer);
  });

  return { snapshot, loading, error };
}
