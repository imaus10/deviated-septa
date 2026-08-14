import { ref, computed, onMounted, onUnmounted } from "vue";
import { sql } from "../lib/neon.js";

const POLL_INTERVAL = 60_000;
const DEFAULT_PERIOD = "hourly";

export function useDashboardData() {
  const snapshot = ref([]);
  const routeGeometries = ref([]);
  const period = ref(DEFAULT_PERIOD);
  const loading = ref(true);
  const error = ref(null);

  const rows = computed(() =>
    snapshot.value.filter((r) => r.period === period.value),
  );

  let timer = null;

  async function fetchSnapshot() {
    try {
      const [snapRows, geoRows] = await Promise.all([
        sql`
          SELECT *
          FROM latest_snapshot
          ORDER BY entity_type, entity_id
        `,
        sql`
          SELECT *
          FROM route_geometries
          ORDER BY route_id
        `,
      ]);
      snapshot.value = snapRows;
      routeGeometries.value = geoRows;
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

  return { snapshot, rows, routeGeometries, period, loading, error };
}
