<script setup>
import { computed } from "vue";

const props = defineProps({
  routes: { type: Array, required: true },
});

function formatDelay(seconds) {
  if (seconds == null) return "—";
  const abs = Math.abs(seconds);
  const m = Math.floor(abs / 60);
  const s = Math.round(abs % 60);
  const sign = seconds < 0 ? "-" : "+";
  if (m === 0) return `${sign}${s}s`;
  return `${sign}${m}m ${s}s`;
}

const systemOtp = computed(() => {
  const total = props.routes.reduce((s, r) => s + (r.total_observations || 0), 0);
  const onTime = props.routes.reduce((s, r) => s + (r.on_time_count || 0), 0);
  if (total === 0) return null;
  return ((onTime / total) * 100).toFixed(1);
});

const totalObs = computed(() =>
  props.routes.reduce((s, r) => s + (r.total_observations || 0), 0),
);

const formattedAvgDelay = computed(() => {
  const withDelay = props.routes.filter((r) => r.avg_delay_seconds != null);
  if (withDelay.length === 0) return null;
  const mean = withDelay.reduce((s, r) => s + r.avg_delay_seconds, 0) / withDelay.length;
  return formatDelay(mean);
});

const lastUpdated = computed(() => {
  const ts = props.routes
    .map((r) => r.updated_at)
    .filter(Boolean)
    .sort()
    .reverse()[0];
  if (!ts) return null;
  return new Date(ts).toLocaleString();
});
</script>

<template>
  <div class="kpi-grid">
    <div class="kpi-card">
      <span class="kpi-value">{{ systemOtp ?? "—" }}%</span>
      <span class="kpi-label">System On-Time</span>
    </div>
    <div class="kpi-card">
      <span class="kpi-value">{{ props.routes.length }}</span>
      <span class="kpi-label">Routes Tracked</span>
    </div>
    <div class="kpi-card">
      <span class="kpi-value">{{ totalObs.toLocaleString() }}</span>
      <span class="kpi-label">Stop Observations Today</span>
    </div>
    <div class="kpi-card">
      <span class="kpi-value">{{ formattedAvgDelay ?? "—" }}</span>
      <span class="kpi-label">Avg Delay</span>
    </div>
  </div>
  <p v-if="lastUpdated" class="updated-at">
    Last updated: {{ lastUpdated }}
  </p>
</template>

<style scoped>
.kpi-grid {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.kpi-card {
  background: rgba(26, 26, 46, 0.92);
  border-radius: 6px;
  padding: 0.4rem 0.75rem;
  text-align: center;
  backdrop-filter: blur(4px);
  min-width: 120px;
}
.kpi-value {
  display: block;
  font-size: 1.2rem;
  font-weight: 700;
  color: #e0e0e0;
}
.kpi-label {
  display: block;
  font-size: 0.65rem;
  color: #888;
  margin-top: 0.1rem;
}
.updated-at {
  font-size: 0.7rem;
  color: #666;
  margin-top: 0.35rem;
  text-align: left;
}
</style>
