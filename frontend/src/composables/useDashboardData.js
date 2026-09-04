import { ref, computed, onMounted, onUnmounted } from "vue";
import { buildSnapshot } from "../lib/current.js";

const POLL_INTERVAL = 60_000;
const DEFAULT_PERIOD = "hourly";
const BASE_URL =
  import.meta.env.VITE_PUBLIC_URL || "https://deviated-septa-prod.s3.amazonaws.com/public";

export function useDashboardData() {
  const snapshot = ref([]);
  const routeGeometries = ref([]);
  const dataRange = ref(null);
  const period = ref(DEFAULT_PERIOD);
  const loading = ref(true);
  const error = ref(null);

  const rows = computed(() =>
    snapshot.value.filter((r) => r.period === period.value),
  );

  let timer = null;

  async function fetchJson(path) {
    const resp = await fetch(`${BASE_URL}${path}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${path}`);
    return resp.json();
  }

  async function fetchSnapshot() {
    try {
      const [current, geometries] = await Promise.all([
        fetchJson("/current.json"),
        fetchJson("/geometries.json"),
      ]);
      snapshot.value = buildSnapshot(current);
      routeGeometries.value = geometries;
      dataRange.value = current.data_range || null;
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

  return { snapshot, rows, routeGeometries, dataRange, period, loading, error };
}