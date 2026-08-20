<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const props = defineProps({
  routes: { type: Array, default: () => [] },
  stops: { type: Array, default: () => [] },
  geometries: { type: Array, default: () => [] },
  highlightStops: { type: Array, default: () => [] },
  selectedStop: { type: Object, default: null },
});

const mapContainer = ref(null);
const stopsVisible = ref(false);
const STOPS_MIN_ZOOM = 14;
let map = null;
let lineLayer = null;
let stopLayer = null;
let highlightLayer = null;
let highlightMarkerMap = new Map();
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

function otpHaloColor(otp) {
  if (otp == null) return "rgba(144,164,174,0.5)";
  const t = Math.max(0, Math.min(100, otp)) / 100;
  const hue = t * 120;
  return `hsla(${hue}, 85%, 55%, 0.5)`;
}

function otpEdgeColor(otp) {
  if (otp == null) return "#90a4ae";
  const t = Math.max(0, Math.min(100, otp)) / 100;
  const hue = t * 120;
  return `hsl(${hue}, 85%, 35%)`;
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

function stopPopupContent(stop) {
  const otp = stop.on_time_percentage;
  const pct = otp != null ? otp.toFixed(1) + "%" : "—";
  const delay = formatDelay(stop.avg_delay_seconds);
  const total = stop.total_observations || 0;
  const early = stop.early_count || 0;
  const onTime = stop.on_time_count || 0;
  const late = stop.late_count || 0;
  const earlyPct = total ? (early / total * 100).toFixed(0) : 0;
  const onTimePct = total ? (onTime / total * 100).toFixed(0) : 0;
  const latePct = total ? (late / total * 100).toFixed(0) : 0;
  return `
    <div class="popup-body">
      <div class="popup-name">${stop.stop_name || stop.stop_id}</div>
      <div class="popup-otp" style="color:${otpColor(otp)}">${pct}</div>
      <div class="popup-row"><span class="popup-label">Avg delay</span><span>${delay}</span></div>
      <div class="popup-row"><span class="popup-label">Observations</span><span>${total.toLocaleString()}</span></div>
      <div class="popup-breakdown">
        <span style="color:#f44336">${earlyPct}% early</span>
        <span style="color:#4caf50">${onTimePct}% on time</span>
        <span style="color:#ff9800">${latePct}% late</span>
      </div>
    </div>`;
}

function drawLines() {
  if (!map) return;
  if (lineLayer) { map.removeLayer(lineLayer); lineLayer = null; }
  lineLayer = L.layerGroup().addTo(map);
  for (const r of props.geometries) {
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

function drawStops() {
  if (!map) return;
  if (stopLayer) { map.removeLayer(stopLayer); stopLayer = null; }
  if (map.getZoom() < STOPS_MIN_ZOOM) {
    stopsVisible.value = false;
    return;
  }
  stopLayer = L.layerGroup().addTo(map);
  stopsVisible.value = true;

  for (const stop of props.stops) {
    const lat = Number(stop.stop_lat);
    const lon = Number(stop.stop_lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    const marker = L.circleMarker([lat, lon], {
      radius: 4,
      color: "#111827",
      weight: 1,
      fillColor: otpColor(stop.on_time_percentage),
      fillOpacity: 0.92,
      opacity: 0.9,
    }).addTo(stopLayer);

    marker.bindTooltip(stop.stop_name || stop.stop_id, { sticky: true, className: "route-tooltip" });
    marker.bindPopup(stopPopupContent(stop), { className: "route-popup", closeButton: false });
  }
  bringHighlightsToFront();
}

function drawHighlights() {
  if (!map) return;
  if (highlightLayer) { map.removeLayer(highlightLayer); highlightLayer = null; }
  highlightMarkerMap = new Map();
  if (!props.highlightStops.length) return;

  highlightLayer = L.layerGroup().addTo(map);
  for (const stop of props.highlightStops) {
    const lat = Number(stop.stop_lat);
    const lon = Number(stop.stop_lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

    const halo = L.circleMarker([lat, lon], {
      radius: 9,
      color: otpHaloColor(stop.on_time_percentage),
      weight: 2,
      fill: false,
      interactive: false,
      pane: "highlightPane",
    }).addTo(highlightLayer);

    const marker = L.circleMarker([lat, lon], {
      radius: 6,
      color: otpEdgeColor(stop.on_time_percentage),
      weight: 1.5,
      fillColor: otpColor(stop.on_time_percentage),
      fillOpacity: 0.8,
      pane: "highlightPane",
    }).addTo(highlightLayer);

    marker.bindTooltip(stop.stop_name || stop.stop_id, { sticky: true, className: "route-tooltip" });
    marker.bindPopup(stopPopupContent(stop), { className: "route-popup", closeButton: false });
    highlightMarkerMap.set(stop.entity_id, marker);
  }
  bringHighlightsToFront();
}

function bringHighlightsToFront() {
  if (!map || !highlightLayer) return;
  map.removeLayer(highlightLayer);
  highlightLayer.addTo(map);
}

function updateStopsVisibility() {
  if (!map) return;
  if (map.getZoom() >= STOPS_MIN_ZOOM) {
    if (!stopLayer) drawStops();
    else if (!map.hasLayer(stopLayer)) {
      stopLayer.addTo(map);
      stopsVisible.value = true;
    }
  } else if (stopLayer) {
    map.removeLayer(stopLayer);
    stopLayer = null;
    stopsVisible.value = false;
  }
}

watch(() => props.routes, drawLines, { deep: false });
watch(() => props.stops, drawStops, { deep: false });
watch(() => props.geometries, drawLines, { deep: false });
watch(() => props.highlightStops, drawHighlights, { deep: false });

watch(() => props.selectedStop, (stop) => {
  if (!map || !stop) return;
  const marker = highlightMarkerMap.get(stop.entity_id);
  if (!marker) return;
  const latlng = marker.getLatLng();

  if (!map.getBounds().contains(latlng)) {
    map.once("moveend", () => marker.openPopup());
    map.panTo(latlng);
  } else {
    marker.openPopup();
  }
});

onMounted(() => {
  map = L.map(mapContainer.value, {
    center: [39.95, -75.16],
    zoom: 11,
    zoomControl: false,
  });

  const highlightPane = map.createPane("highlightPane");
  highlightPane.style.zIndex = 640;

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OSM</a> &copy; <a href=\"https://carto.com/\">CARTO</a>",
    maxZoom: 18,
  }).addTo(map);
  map.on("zoomend", updateStopsVisibility);
  drawStops();

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

  drawLines();
  drawHighlights();
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
  <div class="map-wrap">
    <div ref="mapContainer" class="map-container"></div>
    <div class="map-corner">
      <div v-if="!stopsVisible" class="zoom-hint">
        <span class="zoom-hint-icon">+</span>
        <div class="zoom-hint-text">
          <span class="zoom-hint-title">Zoom in to view stop-level data</span>
        </div>
      </div>
      <div class="zoom-stack">
        <button class="zoom-btn" title="Zoom in" aria-label="Zoom in" @click="map?.zoomIn()">+</button>
        <button class="zoom-btn" title="Zoom out" aria-label="Zoom out" @click="map?.zoomOut()">−</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.map-wrap {
  width: 100%;
  height: 100%;
  position: relative;
}

.map-container {
  width: 100%;
  height: 100%;
}

.map-corner {
  position: absolute;
  right: 0.625rem;
  bottom: 1.7rem;
  z-index: 1100;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  pointer-events: none;
}

.map-corner > * {
  pointer-events: auto;
}

.zoom-stack {
  display: flex;
  flex-direction: column;
  border: 1px solid #333;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(26, 26, 46, 0.85);
  backdrop-filter: blur(4px);
}

.zoom-btn {
  width: 2rem;
  height: 2rem;
  border: none;
  background: transparent;
  color: #ccc;
  font-size: 1.1rem;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
}

.zoom-btn + .zoom-btn {
  border-top: 1px solid #2a2a3e;
}

.zoom-btn:hover {
  background: #2a2a3e;
  color: #e0e0e0;
}

.zoom-hint {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  background: rgba(22, 22, 42, 0.92);
  border: 1px solid #333;
  border-radius: 10px;
  padding: 0.6rem 0.9rem;
  backdrop-filter: blur(4px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  pointer-events: none;
  animation: zoom-hint-in 0.25s ease-out;
}

.zoom-hint-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 50%;
  background: #3a3a5e;
  color: #e0e0e0;
  font-size: 1.1rem;
  font-weight: 700;
  flex-shrink: 0;
}

.zoom-hint-text {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.zoom-hint-title {
  color: #e0e0e0;
  font-size: 0.8rem;
  font-weight: 700;
}

@keyframes zoom-hint-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
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
.leaflet-container svg path {
  outline: none;
}

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
