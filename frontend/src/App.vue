<script setup>
import { ref, computed, onMounted, watch } from "vue";
import { useDashboardData } from "./composables/useDashboardData.js";
import KpiHeader from "./components/KpiHeader.vue";
import RouteTable from "./components/RouteTable.vue";
import RouteMap from "./components/RouteMap.vue";
import TopStops from "./components/TopStops.vue";

const { rows, routeGeometries, dataRange, period, loading, error } = useDashboardData();

const routes = computed(() =>
  rows.value.filter((r) => r.entity_type === "route" && [0, 3, 11].includes(r.route_type)),
);

const stops = computed(() =>
  rows.value.filter((r) => r.entity_type === "stop" && r.stop_lat != null && r.stop_lon != null),
);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parseRangeDate(raw) {
  if (!raw) return null;
  if (raw instanceof Date) return raw;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(raw));
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function fmtRangeDate(d) {
  const base = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === new Date().getFullYear() ? base : `${base}, ${d.getFullYear()}`;
}

const rangeCaption = computed(() => {
  const min = parseRangeDate(dataRange.value?.min);
  const max = parseRangeDate(dataRange.value?.max);
  if (!min || !max) return null;
  const days = Math.round((max - min) / 86400000) + 1;
  const start = fmtRangeDate(min);
  return days <= 1 ? `tracking since ${start}` : `tracking since ${start} · ${days} days`;
});

const periods = computed(() => [
  { value: "hourly", label: "Last Hour", title: "Stop arrivals refreshed in the last 60 minutes" },
  { value: "daily", label: "Today", title: "Today's service date" },
  { value: "weekly", label: "Last 7 Days", title: "Past 7 service dates including today" },
  { value: "all", label: "All Time", title: "All available observations since tracking began" },
]);

const showList = ref(false);
const showTopStops = ref(false);
const selectedStop = ref(null);

const toggleGroup = ref(null);

function layoutClosedStack() {
  const group = toggleGroup.value;
  if (!group) return;
  const btns = Array.from(group.querySelectorAll(".toggle-btn"));
  const gap = parseFloat(getComputedStyle(group).gap) || 8;
  const ws = btns.map((b) => b.offsetWidth);
  const hs = btns.map((b) => b.offsetHeight);
  btns.forEach((b, i) => {
    let x = 0;
    let y = 0;
    for (let j = i + 1; j < btns.length; j++) x += ws[j] + gap;
    for (let j = 0; j < i; j++) y += hs[j] + gap;
    b.style.setProperty("--x", x + "px");
    b.style.setProperty("--y", y + "px");
  });
}

const paneOpen = computed(() => showList.value || showTopStops.value);
let startRects = [];
let flipAnims = [];

watch(
  paneOpen,
  () => {
    const group = toggleGroup.value;
    if (!group) return;
    startRects = Array.from(group.querySelectorAll(".toggle-btn")).map((b) =>
      b.getBoundingClientRect(),
    );
  },
  { flush: "pre" },
);

const OPEN_BTN_DELAY = 190;

watch(
  paneOpen,
  (open) => {
    const group = toggleGroup.value;
    if (!group) return;
    const btns = Array.from(group.querySelectorAll(".toggle-btn"));
    flipAnims.forEach((a) => a.cancel());
    const endRects = btns.map((b) => b.getBoundingClientRect());
    const endTransforms = btns.map((b) => {
      const m = new DOMMatrixReadOnly(getComputedStyle(b).transform);
      return { x: m.e, y: m.f };
    });
    flipAnims = btns.map((b, i) => {
      const s = startRects[i];
      const e = endRects[i];
      const t = endTransforms[i];
      const layoutLeft = e.left - t.x;
      const layoutTop = e.top - t.y;
      const fromX = s ? s.left - layoutLeft : t.x;
      const fromY = s ? s.top - layoutTop : t.y;
      return b.animate(
        [
          { transform: `translate(${fromX}px, ${fromY}px)` },
          { transform: `translate(${t.x}px, ${t.y}px)` },
        ],
        {
          duration: 500,
          delay: open ? OPEN_BTN_DELAY : 0,
          easing: "ease-out",
          fill: "backwards",
        },
      );
    });
  },
  { flush: "post" },
);

onMounted(layoutClosedStack);
watch(
  loading,
  (l) => {
    if (!l) layoutClosedStack();
  },
  { flush: "post" },
);

watch(showTopStops, (v) => {
  if (!v) selectedStop.value = null;
});

watch(period, () => {
  selectedStop.value = null;
});

const TOP_STOPS_N = 20;
const MIN_OBS = 10;

const qualifiedStops = computed(() =>
  stops.value.filter(
    (s) => (s.total_observations ?? 0) >= MIN_OBS && s.avg_delay_seconds != null,
  ),
);

const highlightedStops = ref([]);

function toggleList() {
  showList.value = !showList.value;
  if (showList.value) showTopStops.value = false;
}

function toggleTopStops() {
  showTopStops.value = !showTopStops.value;
  if (showTopStops.value) showList.value = false;
}
</script>

<template>
  <div class="app" :class="{ 'pane-open': showList || showTopStops }">
    <div v-if="error" class="error">Error: {{ error }}</div>
    <div v-else-if="loading" class="loading">Loading...</div>
    <template v-else>
      <div class="map-layer">
        <RouteMap
          :routes="routes"
          :stops="stops"
          :geometries="routeGeometries"
          :highlight-stops="showTopStops ? highlightedStops : []"
          :selected-stop="selectedStop"
        />
      </div>

      <div class="title-bar"><span class="title-red">deviated</span> <span class="title-green">SEPTA</span></div>
      <div class="period-stack">
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
        <div v-if="rangeCaption && period === 'all'" class="period-caption">{{ rangeCaption }}</div>
      </div>
      <div class="kpi-rail">
        <KpiHeader :routes="routes" />
      </div>

      <div ref="toggleGroup" class="toggle-group">
        <button class="toggle-btn" :class="{ active: showList }" @click="toggleList">
          <svg class="btn-icon" viewBox="0 0 16 16" aria-hidden="true">
            <rect x="1" y="3" width="14" height="2" rx="1" fill="currentColor" />
            <rect x="1" y="7" width="14" height="2" rx="1" fill="currentColor" />
            <rect x="1" y="11" width="14" height="2" rx="1" fill="currentColor" />
          </svg>
          Route Ranking
        </button>
        <button class="toggle-btn" :class="{ active: showTopStops }" @click="toggleTopStops">
          ◎ Top Stops
        </button>
      </div>

      <div class="list-pane" :class="{ open: showList || showTopStops }">
        <div class="pane-header">
          <button class="close-btn" @click="showList = false; showTopStops = false">✕</button>
        </div>
        <RouteTable v-if="showList" :rows="routes" />
        <TopStops v-else-if="showTopStops" :stops="qualifiedStops" :n="TOP_STOPS_N" @highlight="highlightedStops = $event" @selectStop="selectedStop = $event" />
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
.period-stack {
  position: absolute;
  top: 4.25rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.35rem;
}
.period-control {
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
.period-caption {
  font-size: 0.68rem;
  font-weight: 500;
  color: #999;
  background: rgba(26, 26, 46, 0.85);
  padding: 0.18rem 0.7rem;
  border-radius: 6px;
  backdrop-filter: blur(4px);
  pointer-events: none;
  white-space: nowrap;
  animation: period-caption-in 0.25s ease-out;
}
@keyframes period-caption-in {
  from {
    transform: translateY(-100%);
    opacity: 0;
  }
  to {
    transform: translateY(0);
    opacity: 1;
  }
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
.toggle-group {
  position: absolute;
  top: 0.75rem;
  right: 0.75rem;
  z-index: 30;
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 0.5rem;
  transform: translateX(0);
}
.pane-open .toggle-group {
  right: calc(560px - 1rem);
  transform: translateX(100%);
}
.toggle-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  background: rgba(26, 26, 46, 0.85);
  border: 1px solid #333;
  color: #ccc;
  font-size: 0.9rem;
  font-weight: 600;
  padding: 0.5rem 0.9rem;
  border-radius: 6px;
  cursor: pointer;
  backdrop-filter: blur(4px);
  transform: translate(var(--x, 0), var(--y, 0));
  transition: background 0.15s, color 0.15s;
}
.pane-open .toggle-btn {
  transform: translate(0, 0);
}
.btn-icon {
  width: 1em;
  height: 1em;
  display: block;
  flex: none;
}
.toggle-btn:hover {
  background: #2a2a3e;
}
.toggle-btn.active {
  background: #3a3a5e;
  border-color: #4a4a6e;
  color: #e0e0e0;
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
  clip-path: inset(0 0 0 100%);
  transition: clip-path 0.5s ease-out;
  z-index: 20;
}
.list-pane.open {
  clip-path: inset(0 0 0 0);
}
.pane-header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  border-bottom: 1px solid #2a2a3e;
  padding-bottom: 1rem;
  margin-bottom: 0.75rem;
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
