<script setup>
import { computed } from "vue";
import { useDashboardData } from "./composables/useDashboardData.js";
import KpiHeader from "./components/KpiHeader.vue";
import RouteTable from "./components/RouteTable.vue";

const { snapshot, loading, error } = useDashboardData();

const busRoutes = computed(() => snapshot.value.filter((r) => r.route_type === 3));
const trolleyRoutes = computed(() => snapshot.value.filter((r) => r.route_type === 0));
</script>

<template>
  <div class="app">
    <header class="header">
      <h1>SEPTA Reliability</h1>
      <span class="subtitle">Real-time on-time performance for SEPTA Bus &amp; Trolley</span>
    </header>

    <div v-if="error" class="error">Error: {{ error }}</div>
    <div v-else-if="loading" class="loading">Loading...</div>
    <template v-else>
      <KpiHeader :routes="snapshot" />

      <section>
        <h2>Bus Routes</h2>
        <RouteTable :routes="busRoutes" />
      </section>

      <section>
        <h2>Trolley Routes</h2>
        <RouteTable :routes="trolleyRoutes" />
      </section>
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
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.app {
  max-width: 1100px;
  margin: 0 auto;
  padding: 1.5rem;
}
.header {
  margin-bottom: 1.5rem;
}
.header h1 {
  font-size: 1.6rem;
  color: #e0e0e0;
}
.subtitle {
  color: #666;
  font-size: 0.85rem;
}
section {
  margin-top: 2rem;
}
section h2 {
  font-size: 1.1rem;
  color: #aaa;
  margin-bottom: 0.75rem;
  border-bottom: 1px solid #2a2a3e;
  padding-bottom: 0.4rem;
}
.loading,
.error {
  text-align: center;
  padding: 3rem;
  color: #888;
}
.error {
  color: #f44336;
}
</style>
