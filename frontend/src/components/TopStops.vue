<script setup>
import { ref, computed, watch } from "vue";

const props = defineProps({
  stops: { type: Array, default: () => [] },
  n: { type: Number, default: 20 },
});

const emit = defineEmits(["selectStop", "highlight"]);

const bestSortKey = ref("abs_delay");
const worstSortKey = ref("abs_delay");

const sortDirs = {
  abs_delay: { best: 1, worst: -1 },
  otp: { best: -1, worst: 1 },
};

function setSort(section, key) {
  if (section === "best") bestSortKey.value = key;
  else worstSortKey.value = key;
}

function sortVal(r, key) {
  if (key === "abs_delay") return Math.abs(r.avg_delay_seconds ?? 0);
  if (key === "otp") return r.on_time_percentage ?? 0;
  return 0;
}

function topN(sortKey, section) {
  const key = sortKey;
  const dir = sortDirs[key][section];
  return [...props.stops]
    .sort((a, b) => {
      const va = sortVal(a, key);
      const vb = sortVal(b, key);
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    })
    .slice(0, props.n);
}

const sortedBest = computed(() => topN(bestSortKey.value, "best"));
const sortedWorst = computed(() => topN(worstSortKey.value, "worst"));

watch([sortedBest, sortedWorst], ([best, worst]) => {
  emit("highlight", [...best, ...worst]);
}, { immediate: true });

function formatDelay(seconds) {
  if (seconds == null) return "—";
  const abs = Math.abs(seconds);
  const m = Math.floor(abs / 60);
  const s = Math.round(abs % 60);
  const sign = seconds < 0 ? "-" : "+";
  if (m === 0) return `${sign}${s}s`;
  return `${sign}${m}m ${s}s`;
}
</script>

<template>
  <template v-if="stops.length">
    <section class="stops-section">
      <h3 class="section-title best-title">Best Stops</h3>
      <table class="top-table">
        <thead>
          <tr>
            <th class="rank-col">#</th>
            <th class="name-col">Stop</th>
            <th class="sortable" :class="{ active: bestSortKey === 'abs_delay' }" @click="setSort('best', 'abs_delay')">Avg Delay</th>
            <th class="sortable" :class="{ active: bestSortKey === 'otp' }" @click="setSort('best', 'otp')">OTP</th>
            <th class="obs-col">Obs</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in sortedBest" :key="'b' + s.entity_id" class="clickable" @click="emit('selectStop', s)">
            <td class="rank">{{ i + 1 }}</td>
            <td class="name-cell" :title="s.stop_id">{{ s.stop_name || s.stop_id }}</td>
            <td class="delay">{{ formatDelay(s.avg_delay_seconds) }}</td>
            <td class="otp">{{ s.on_time_percentage != null ? s.on_time_percentage + '%' : '—' }}</td>
            <td class="obs">{{ (s.total_observations ?? 0).toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="stops-section">
      <h3 class="section-title worst-title">Worst Stops</h3>
      <table class="top-table">
        <thead>
          <tr>
            <th class="rank-col">#</th>
            <th class="name-col">Stop</th>
            <th class="sortable" :class="{ active: worstSortKey === 'abs_delay' }" @click="setSort('worst', 'abs_delay')">Avg Delay</th>
            <th class="sortable" :class="{ active: worstSortKey === 'otp' }" @click="setSort('worst', 'otp')">OTP</th>
            <th class="obs-col">Obs</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in sortedWorst" :key="'w' + s.entity_id" class="clickable" @click="emit('selectStop', s)">
            <td class="rank">{{ i + 1 }}</td>
            <td class="name-cell" :title="s.stop_id">{{ s.stop_name || s.stop_id }}</td>
            <td class="delay">{{ formatDelay(s.avg_delay_seconds) }}</td>
            <td class="otp">{{ s.on_time_percentage != null ? s.on_time_percentage + '%' : '—' }}</td>
            <td class="obs">{{ (s.total_observations ?? 0).toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </template>
  <p v-else class="empty">No stop data for this period yet.</p>
</template>

<style scoped>
.stops-section + .stops-section {
  margin-top: 1.1rem;
}
.section-title {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  margin-bottom: 0.35rem;
}
.best-title {
  color: #4caf50;
}
.worst-title {
  color: #f44336;
}
.top-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.72rem;
}
.top-table th {
  text-align: left;
  padding: 0.2rem 0.3rem;
  color: #666;
  border-bottom: 1px solid #333;
  font-weight: 600;
  white-space: nowrap;
}
.top-table th.rank-col {
  width: 1.5rem;
  text-align: right;
}
.top-table th.name-col {
  width: auto;
}
.top-table th.obs-col {
  width: 3rem;
  text-align: right;
}
.top-table th.sortable {
  cursor: pointer;
  user-select: none;
  text-align: right;
}
.top-table th.sortable:hover {
  color: #ccc;
}
.top-table th.sortable.active {
  color: #e0e0e0;
  border-bottom-color: #888;
}
.top-table td {
  padding: 0.3rem 0.3rem;
  border-bottom: 1px solid #222;
  white-space: nowrap;
}
.rank {
  color: #666;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.name-cell {
  display: block;
  max-width: 15rem;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #e0e0e0;
  font-weight: 600;
}
.otp {
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: #4caf50;
}
.delay {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.obs {
  color: #666;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.clickable {
  cursor: pointer;
}
.clickable:hover td {
  background: #2a2a3e;
}
.empty {
  color: #888;
  font-size: 0.85rem;
  text-align: center;
  margin-top: 2rem;
}
</style>
