<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const props = defineProps({
  routes: { type: Array, default: () => [] },
});

const mapContainer = ref(null);
let map = null;
let routeLines = null;
let lineLayer = null;
const routeMap = computed(() => {
  const m = new Map();
  for (const r of props.routes) {
    m.set(r.route_id, r.on_time_percentage);
  }
  return m;
});

function otpColor(otp) {
  if (otp == null) return "#90a4ae";
  const t = Math.max(0, Math.min(100, otp)) / 100;
  const hue = t * 120;
  return `hsl(${hue}, 85%, 50%)`;
}

function drawLines() {
  if (!map || !routeLines) return;
  if (lineLayer) lineLayer.clearLayers();
  else lineLayer = L.layerGroup().addTo(map);
  for (const r of routeLines) {
    const otp = routeMap.value.get(r.route_id);
    L.polyline(r.coordinates, {
      color: otpColor(otp),
      weight: 2,
      opacity: 0.8,
    }).addTo(lineLayer);
  }
}

watch(() => props.routes, drawLines, { deep: false });

onMounted(() => {
  map = L.map(mapContainer.value, {
    center: [39.95, -75.16],
    zoom: 11,
    zoomControl: false,
  });
  L.control.zoom({ position: "bottomright" }).addTo(map);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OSM</a> &copy; <a href=\"https://carto.com/\">CARTO</a>",
    maxZoom: 18,
  }).addTo(map);

  fetch(import.meta.env.BASE_URL + "philly-boundary.json")
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then((geojson) => {
      const core = L.geoJSON(geojson, {
        style: { color: "#ffffff", fill: false, weight: 4, opacity: 1, className: "boundary-path" },
      }).addTo(map);
      map.fitBounds(core.getBounds());
    })
    .catch((e) => console.error("Boundary fetch failed:", e));

  fetch(import.meta.env.BASE_URL + "route-lines.json")
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then((lines) => {
      routeLines = lines;
      drawLines();
    })
    .catch((e) => console.error("Route lines fetch failed:", e));
});

onUnmounted(() => {
  if (map) {
    map.remove();
    map = null;
  }
});
</script>

<template>
  <div ref="mapContainer" class="map-container"></div>
</template>

<style scoped>
.map-container {
  width: 100%;
  height: 100%;
}

:deep(.leaflet-overlay-pane svg) {
  overflow: visible;
}

:deep(.boundary-path) {
  filter: drop-shadow(0 0 14px rgba(255, 255, 255, 0.4))
          drop-shadow(0 0 6px rgba(255, 255, 255, 0.6));
  animation: boundary-pulse 2.5s ease-in-out infinite;
}

@keyframes boundary-pulse {
  0%, 100% { opacity: 0.6; }
  50%      { opacity: 1;   }
}
</style>
