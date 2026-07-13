import { ref, onMounted, onUnmounted } from "vue";
import { supabase } from "../lib/supabase.js";

export function useDashboardData() {
  const snapshot = ref([]);
  const loading = ref(true);
  const error = ref(null);

  let subscription = null;

  async function fetchSnapshot() {
    const { data, error: err } = await supabase
      .from("latest_snapshot")
      .select("*")
      .order("route_id");

    if (err) {
      error.value = err.message;
      return;
    }
    snapshot.value = data;
    loading.value = false;
  }

  function subscribeToChanges() {
    subscription = supabase
      .channel("latest_snapshot_changes")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "latest_snapshot" },
        () => fetchSnapshot(),
      )
      .subscribe();
  }

  onMounted(() => {
    fetchSnapshot();
    subscribeToChanges();
  });

  onUnmounted(() => {
    if (subscription) supabase.removeChannel(subscription);
  });

  return { snapshot, loading, error };
}
