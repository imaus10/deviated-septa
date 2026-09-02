// Transform the poller's public/current.json into the flat, period-tagged rows
// the dashboard components consume (the shape latest_snapshot used to provide).
// Pure functions — no I/O, no fetch. The composable feeds them `current.json`.

const PERIOD_MAP = { hour: "hourly", day: "daily", week: "weekly", all: "all" };

export function deriveTotals(t) {
  const total = t.total_observations || 0;
  return {
    ...t,
    on_time_percentage: total ? Math.round(((t.on_time_count || 0) / total * 100) * 10) / 10 : null,
    avg_delay_seconds: total ? t.delay_sum / total : null,
  };
}

export function buildSnapshot(current) {
  const meta = current.metadata || {};
  const rows = [];
  for (const [periodKey, periodData] of Object.entries(current.periods || {})) {
    const period = PERIOD_MAP[periodKey];
    if (!period) continue;

    for (const [routeId, totals] of Object.entries(periodData.routes || {})) {
      const m = meta.routes?.[routeId] || {};
      rows.push({
        period,
        entity_type: "route",
        entity_id: routeId,
        route_id: routeId,
        route_name: m.route_name ?? routeId,
        route_type: m.route_type ?? null,
        updated_at: current.updated_at,
        ...deriveTotals(totals),
      });
    }

    for (const [stopId, totals] of Object.entries(periodData.stops || {})) {
      const m = meta.stops?.[stopId] || {};
      rows.push({
        period,
        entity_type: "stop",
        entity_id: stopId,
        stop_id: stopId,
        stop_name: m.stop_name ?? stopId,
        stop_lat: m.stop_lat ?? null,
        stop_lon: m.stop_lon ?? null,
        updated_at: current.updated_at,
        ...deriveTotals(totals),
      });
    }
  }
  return rows;
}