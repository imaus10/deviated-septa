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
let animFrame = null;
const routeLookup = computed(() => {
  const m = new Map();
  for (const r of props.routes) {
    m.set(r.route_id, r);
  }
  return m;
});

function otpColor(otp) {
  if (otp == null) return "#90a4ae";
  const t = Math.max(0, Math.min(100, otp)) / 100;
  const hue = t * 120;
  return `hsl(${hue}, 85%, 50%)`;
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

function popupContent(r) {
  if (!r) return '<div class="popup-body">No data</div>';
  const otp = r.on_time_percentage;
  const pct = otp != null ? otp.toFixed(1) + "%" : "—";
  const delay = formatDelay(r.avg_delay_seconds);
  const total = r.total_observations || 0;
  const early = r.early_count || 0;
  const onTime = r.on_time_count || 0;
  const late = r.late_count || 0;
  const earlyPct = total ? (early / total * 100).toFixed(0) : 0;
  const onTimePct = total ? (onTime / total * 100).toFixed(0) : 0;
  const latePct = total ? (late / total * 100).toFixed(0) : 0;
  return `
    <div class="popup-body">
      <div class="popup-name">Route ${r.route_name || r.route_id}</div>
      <div class="popup-otp" style="color:${otpColor(otp)}">${pct}</div>
      <div class="popup-row"><span class="popup-label">Avg delay</span><span>${delay}</span></div>
      <div class="popup-breakdown">
        <span style="color:#f44336">${earlyPct}% early</span>
        <span style="color:#4caf50">${onTimePct}% on time</span>
        <span style="color:#ff9800">${latePct}% late</span>
      </div>
    </div>`;
}

function drawLines() {
  if (!map || !routeLines) return;
  if (lineLayer) { map.removeLayer(lineLayer); lineLayer = null; }
  lineLayer = L.layerGroup().addTo(map);
  for (const r of routeLines) {
    const routeData = routeLookup.value.get(r.route_id);
    const color = otpColor(routeData ? routeData.on_time_percentage : null);

    const hit = L.polyline(r.coordinates, {
      color: "transparent", weight: 12, opacity: 1, interactive: true,
    }).addTo(lineLayer);

    const vis = L.polyline(r.coordinates, {
      color, weight: 3, opacity: 0.8, interactive: false,
    }).addTo(lineLayer);
    vis._path.style.pointerEvents = "none";

    hit.bindTooltip(r.route_name || r.route_id, { sticky: true, className: "route-tooltip" });
    hit.bindPopup("", { className: "route-popup", closeButton: false });

    hit.on("mouseover", () => {
      vis.setStyle({ weight: 7 });
      vis.bringToFront();
    });
    hit.on("mouseout", () => vis.setStyle({ weight: 3 }));
    hit.on("click", (e) => {
      hit.setPopupContent(popupContent(routeData));
      hit.openPopup(e.latlng);
    });
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

      let t = 0;
      function pulse() {
        t += 0.025;
        const phase = Math.sin(t) * 0.5 + 0.5;
        const opacity = 0.5 + 0.5 * phase;
        core.eachLayer((p) => p.setStyle({ opacity }));
        animFrame = requestAnimationFrame(pulse);
      }
      pulse();
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
  if (animFrame) cancelAnimationFrame(animFrame);
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

:deep(.route-popup .leaflet-popup-content-wrapper) {
  background: rgba(22, 22, 42, 0.96);
  color: #ccc;
  border: 1px solid #333;
  border-radius: 8px;
  padding: 0;
}
:deep(.route-popup .leaflet-popup-content) {
  margin: 0;
  padding: 0.75rem 1rem;
  font-size: 0.85rem;
  min-width: 160px;
}
:deep(.route-popup .leaflet-popup-tip) {
  background: rgba(22, 22, 42, 0.96);
  border: 1px solid #333;
  border-top: none;
  border-left: none;
}

</style>

<style>
.boundary-path {
  filter: drop-shadow(0 0 14px rgba(255, 255, 255, 0.4))
          drop-shadow(0 0 6px rgba(255, 255, 255, 0.6));
}

.route-tooltip {
  background: rgba(22, 22, 42, 0.92) !important;
  border: 1px solid #333 !important;
  color: #e0e0e0 !important;
  font-size: 0.85rem !important;
  font-weight: 700 !important;
  padding: 0.25rem 0.6rem !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}
.route-tooltip::before {
  border-top-color: #333 !important;
}

.popup-body { line-height: 1.5; }
.popup-name { font-size: 1rem; font-weight: 700; color: #e0e0e0; margin-bottom: 0.25rem; }
.popup-otp { font-size: 1.6rem; font-weight: 800; margin-bottom: 0.3rem; }
.popup-row { display: flex; justify-content: space-between; gap: 1rem; color: #aaa; }
.popup-label { color: #888; }
.popup-breakdown { display: flex; gap: 1rem; font-size: 0.75rem; margin-top: 0.3rem; }
</style>
