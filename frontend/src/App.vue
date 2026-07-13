<script setup>
import { ref, computed } from "vue";
import { useDashboardData } from "./composables/useDashboardData.js";
import KpiHeader from "./components/KpiHeader.vue";
import RouteTable from "./components/RouteTable.vue";
import RouteMap from "./components/RouteMap.vue";

const { snapshot, loading, error } = useDashboardData();

const allRoutes = computed(() =>
  snapshot.value.filter((r) => r.route_type === 0 || r.route_type === 3),
);

const showList = ref(true);
</script>

<template>
  <div class="app">
    <div v-if="error" class="error">Error: {{ error }}</div>
    <div v-else-if="loading" class="loading">Loading...</div>
    <template v-else>
      <div class="map-layer">
        <RouteMap :routes="snapshot" />
      </div>

      <div class="title-bar"><span class="title-red">deviated</span> <span class="title-green">SEPTA</span></div>
      <div class="kpi-rail">
        <KpiHeader :routes="snapshot" />
      </div>

      <button class="toggle-btn" :class="{ hidden: showList }" @click="showList = true">
        ☰
      </button>

      <div class="list-pane" :class="{ open: showList }">
        <div class="pane-header">
          <h2>Routes</h2>
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
  font-size: 1.2rem;
  width: 2.2rem;
  height: 2.2rem;
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
  width: 480px;
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
