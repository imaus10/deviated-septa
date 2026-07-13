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

const sorted = computed(() =>
  [...props.routes].sort((a, b) => (a.on_time_percentage ?? 0) - (b.on_time_percentage ?? 0)),
);

function otpColor(pct) {
  if (pct == null) return "#555";
  if (pct >= 85) return "#4caf50";
  if (pct >= 70) return "#ff9800";
  return "#f44336";
}
</script>

<template>
  <table class="route-table">
    <thead>
      <tr>
        <th>Route</th>
        <th>Name</th>
        <th>Obs</th>
        <th>On-Time</th>
        <th>Early</th>
        <th>Late</th>
        <th>OTP%</th>
        <th>Avg Delay</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="r in sorted" :key="r.route_id">
        <td class="route-id">{{ r.route_id }}</td>
        <td class="route-name">{{ r.route_name ?? "—" }}</td>
        <td>{{ (r.total_observations ?? 0).toLocaleString() }}</td>
        <td>{{ (r.on_time_count ?? 0).toLocaleString() }}</td>
        <td>{{ (r.early_count ?? 0).toLocaleString() }}</td>
        <td>{{ (r.late_count ?? 0).toLocaleString() }}</td>
        <td>
          <div class="otp-bar-container">
            <div
              class="otp-bar"
              :style="{ width: (r.on_time_percentage ?? 0) + '%', background: otpColor(r.on_time_percentage) }"
            ></div>
            <span class="otp-text">{{ r.on_time_percentage != null ? r.on_time_percentage + '%' : '—' }}</span>
          </div>
        </td>
        <td>{{ formatDelay(r.avg_delay_seconds) }}</td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.route-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
th {
  text-align: left;
  padding: 0.5rem;
  color: #888;
  border-bottom: 1px solid #333;
  font-weight: 600;
}
td {
  padding: 0.5rem;
  border-bottom: 1px solid #222;
}
.route-id {
  font-weight: 700;
  color: #e0e0e0;
}
.route-name {
  color: #aaa;
}
.otp-bar-container {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.otp-bar {
  height: 8px;
  border-radius: 4px;
  min-width: 4px;
  transition: width 0.3s;
}
.otp-text {
  font-size: 0.8rem;
  white-space: nowrap;
}
</style>
