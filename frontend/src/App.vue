<script setup>
import { ref, computed } from "vue";
import { useDashboardData } from "./composables/useDashboardData.js";
import KpiHeader from "./components/KpiHeader.vue";
import RouteTable from "./components/RouteTable.vue";
import RouteMap from "./components/RouteMap.vue";

const { rows, period, loading, error } = useDashboardData();

const allRoutes = computed(() =>
  rows.value.filter((r) => r.route_type === 0 || r.route_type === 3),
);

const periods = [
  { value: "hourly", label: "Last Hour", title: "Stop arrivals refreshed in the last 60 minutes" },
  { value: "daily", label: "Today", title: "Today's service date" },
  { value: "weekly", label: "Last 7 Days", title: "Past 7 service dates including today" },
  { value: "all", label: "All Time", title: "All available observations since tracking began" },
];

const showList = ref(false);
</script>

<template>
  <div class="app">
    <div v-if="error" class="error">Error: {{ error }}</div>
    <div v-else-if="loading" class="loading">Loading...</div>
    <template v-else>
      <div class="map-layer">
        <RouteMap :routes="rows" />
      </div>

      <div class="title-bar"><span class="title-red">deviated</span> <span class="title-green">SEPTA</span></div>
      <div class="period-control">
        <button
          v-for="g in periods"
          :key="g.value"
          class="period-btn"
          :class="{ active: period === g.value }"
          :title="g.title"
          @click="period = g.value"
        >
          {{ g.label }}
        </button>
      </div>
      <div class="kpi-rail">
        <KpiHeader :routes="rows" />
      </div>

      <button class="toggle-btn" :class="{ hidden: showList }" @click="showList = true">
        ☰ Route Ranking
      </button>

      <div class="list-pane" :class="{ open: showList }">
        <div class="pane-header">
          <h2>Route Ranking</h2>
          <button class="close-btn" @click="showList = false">✕</button>
        </div>
        <RouteTable :routes="allRoutes" />
      </div>
    </template>
  </div>
</template>

<style>
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
body {
  background: #0f0f1a;
  color: #ccc;
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  overflow: hidden;
}
.app {
  width: 100vw;
  height: 100vh;
  position: relative;
}
.map-layer {
  position: absolute;
  inset: 0;
  z-index: 1;
}
.title-bar {
  position: absolute;
  top: 0.75rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 10;
  font-size: 1.8rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  pointer-events: none;
  background: rgba(26, 26, 46, 0.85);
  padding: 0.5rem 1.6rem;
  border-radius: 6px;
  backdrop-filter: blur(4px);
}
.title-red { color: #f44336; }
.title-green { color: #4caf50; }
.period-control {
  position: absolute;
  top: 4rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 10;
  display: flex;
  gap: 0.25rem;
  background: rgba(26, 26, 46, 0.85);
  padding: 0.25rem;
  border-radius: 8px;
  backdrop-filter: blur(4px);
}
.period-btn {
  background: none;
  border: none;
  color: #888;
  font-size: 0.8rem;
  font-weight: 600;
  padding: 0.35rem 0.9rem;
  border-radius: 5px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.period-btn:hover {
  color: #ccc;
}
.period-btn.active {
  background: #3a3a5e;
  color: #e0e0e0;
}
.kpi-rail {
  position: absolute;
  top: 4rem;
  left: 0.75rem;
  z-index: 10;
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.kpi-rail > * {
  pointer-events: auto;
}
.toggle-btn {
  position: absolute;
  top: 0.75rem;
  right: 0.75rem;
  z-index: 30;
  background: rgba(26, 26, 46, 0.85);
  border: 1px solid #333;
  color: #ccc;
  font-size: 0.9rem;
  font-weight: 600;
  padding: 0.5rem 0.9rem;
  border-radius: 6px;
  cursor: pointer;
  backdrop-filter: blur(4px);
}
.toggle-btn:hover {
  background: #2a2a3e;
}
.toggle-btn.hidden {
  opacity: 0;
  pointer-events: none;
}
.list-pane {
  position: absolute;
  top: 0;
  right: 0;
  width: 560px;
  height: 100vh;
  background: rgba(22, 22, 42, 0.95);
  border-left: 1px solid #333;
  padding: 1rem;
  overflow-y: auto;
  transform: translateX(100%);
  transition: transform 0.25s ease;
  z-index: 20;
}
.list-pane.open {
  transform: translateX(0);
}
.pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #2a2a3e;
  padding-bottom: 0.4rem;
  margin-bottom: 0.75rem;
}
.pane-header h2 {
  font-size: 1.1rem;
  color: #aaa;
}
.close-btn {
  background: none;
  border: none;
  color: #666;
  font-size: 1rem;
  cursor: pointer;
  padding: 0.2rem 0.4rem;
  border-radius: 4px;
}
.close-btn:hover {
  color: #ccc;
  background: #2a2a3e;
}
.loading,
.error {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #888;
}
.error {
  color: #f44336;
}
</style>
