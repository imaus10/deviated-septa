<script setup>
import { ref, computed } from "vue";

const props = defineProps({
  routes: { type: Array, required: true },
});

const sortKey = ref("on_time_percentage");
const sortDir = ref("asc");

function setSort(key) {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === "asc" ? "desc" : "asc";
  } else {
    sortKey.value = key;
    sortDir.value = "asc";
  }
}

function sortVal(r, key) {
  const v = r[key];
  if (key === "route_id") return v ?? "";
  if (key === "route_name") return (v ?? "").toLowerCase();
  return v ?? 0;
}

const sorted = computed(() =>
  [...props.routes].sort((a, b) => {
    const va = sortVal(a, sortKey.value);
    const vb = sortVal(b, sortKey.value);
    return (va < vb ? -1 : va > vb ? 1 : 0) * (sortDir.value === "asc" ? 1 : -1);
  }),
);

function sortIcon(key) {
  if (sortKey.value !== key) return "";
  return sortDir.value === "asc" ? " ▲" : " ▼";
}

function formatDelay(seconds) {
  if (seconds == null) return "—";
  const abs = Math.abs(seconds);
  const m = Math.floor(abs / 60);
  const s = Math.round(abs % 60);
  const sign = seconds < 0 ? "-" : "+";
  if (m === 0) return `${sign}${s}s`;
  return `${sign}${m}m ${s}s`;
}

function typeLabel(route_type) {
  if (route_type === 0) return "T";
  if (route_type === 3) return "B";
  return "—";
}

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
        <th class="sortable" @click="setSort('route_type')">#{{ sortIcon('route_type') }}</th>
        <th class="sortable" @click="setSort('route_id')">Route{{ sortIcon('route_id') }}</th>
        <th class="sortable" @click="setSort('route_name')">Name{{ sortIcon('route_name') }}</th>
        <th class="sortable" @click="setSort('total_observations')">Obs{{ sortIcon('total_observations') }}</th>
        <th class="sortable" @click="setSort('on_time_count')">OT{{ sortIcon('on_time_count') }}</th>
        <th class="sortable" @click="setSort('early_count')">Early{{ sortIcon('early_count') }}</th>
        <th class="sortable" @click="setSort('late_count')">Late{{ sortIcon('late_count') }}</th>
        <th class="sortable" @click="setSort('on_time_percentage')">OTP%{{ sortIcon('on_time_percentage') }}</th>
        <th class="sortable" @click="setSort('avg_delay_seconds')">Delay{{ sortIcon('avg_delay_seconds') }}</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="r in sorted" :key="r.route_id">
        <td class="type-cell">{{ typeLabel(r.route_type) }}</td>
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
  font-size: 0.78rem;
}
th {
  text-align: left;
  padding: 0.35rem 0.4rem;
  color: #888;
  border-bottom: 1px solid #333;
  font-weight: 600;
  white-space: nowrap;
}
th.sortable {
  cursor: pointer;
  user-select: none;
}
th.sortable:hover {
  color: #ccc;
}
td {
  padding: 0.3rem 0.4rem;
  border-bottom: 1px solid #222;
}
.route-id {
  font-weight: 700;
  color: #e0e0e0;
}
.route-name {
  color: #aaa;
}
.type-cell {
  color: #666;
  font-size: 0.7rem;
  text-align: center;
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
