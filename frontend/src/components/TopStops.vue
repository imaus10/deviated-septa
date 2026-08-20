<script setup>
defineProps({
  best: { type: Array, default: () => [] },
  worst: { type: Array, default: () => [] },
});

const emit = defineEmits(["selectStop"]);

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
  <template v-if="best.length || worst.length">
    <section class="stops-section">
      <h3 class="section-title best-title">Best Stops</h3>
      <table class="top-table">
        <tbody>
          <tr v-for="(s, i) in best" :key="'b' + s.entity_id" class="clickable" @click="emit('selectStop', s)">
            <td class="rank">{{ i + 1 }}</td>
            <td class="name-cell" :title="s.stop_id">{{ s.stop_name || s.stop_id }}</td>
            <td class="otp best-otp">{{ s.on_time_percentage + '%' }}</td>
            <td class="delay">{{ formatDelay(s.avg_delay_seconds) }}</td>
            <td class="obs">{{ (s.total_observations ?? 0).toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="stops-section">
      <h3 class="section-title worst-title">Worst Stops</h3>
      <table class="top-table">
        <tbody>
          <tr v-for="(s, i) in worst" :key="'w' + s.entity_id" class="clickable" @click="emit('selectStop', s)">
            <td class="rank">{{ i + 1 }}</td>
            <td class="name-cell" :title="s.stop_id">{{ s.stop_name || s.stop_id }}</td>
            <td class="otp worst-otp">{{ s.on_time_percentage + '%' }}</td>
            <td class="delay">{{ formatDelay(s.avg_delay_seconds) }}</td>
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
}
.best-otp {
  color: #4caf50;
}
.worst-otp {
  color: #f44336;
}
.delay {
  color: #aaa;
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
