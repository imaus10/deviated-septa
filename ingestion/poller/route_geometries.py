"""Route geometry generation for the SEPTA dashboard.

Builds one full-coverage polyline per bus/trolley route (every stop the route
serves lies on its line) using a "spider" walk over the stop graph derived from
the GTFS static tables.

Rebuild route_geometries from ingestion/:
    source ../.env && uv run python -m poller.route_geometries

Idempotent — safe to re-run any time. The poller also regenerates automatically
after every GTFS static-feed import (gtfs_static.run_and_record_freshness).
"""

import math
from collections import defaultdict
from datetime import datetime, timezone

from psycopg2.extras import Json

from poller.db import get_connection, upsert_table

# Bus + trolley scope, matching 007's agg-function filter. Type 11 (trolleybus)
# covers SEPTA routes 59/66/75, which carry real real-time data.
ROUTE_SCOPE_TYPES = (0, 3, 11)

# Consecutive stops farther apart than this are deadhead/skip movements (e.g. a
# trip that jumps straight from the southern terminus to Doylestown), not real
# routing. Excluding them keeps the spider line on actual stop-to-stop edges.
_MAX_EDGE_M = 4000.0
_M_PER_DEG = 111_000.0


def _edge_ok(a, b, stop_coords):
    lat1, lon1 = stop_coords[a]
    lat2, lon2 = stop_coords[b]
    dlat = (lat1 - lat2) * _M_PER_DEG
    dlon = (lon1 - lon2) * _M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return dlat * dlat + dlon * dlon <= _MAX_EDGE_M * _MAX_EDGE_M


def _dist2(a, b, stop_coords):
    lat1, lon1 = stop_coords[a]
    lat2, lon2 = stop_coords[b]
    dlat = (lat1 - lat2) * _M_PER_DEG
    dlon = (lon1 - lon2) * _M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return dlat * dlat + dlon * dlon


def _components(nodes, graph):
    seen = set()
    comps = []
    for start in nodes:
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in graph.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        comps.append(comp)
    return comps


def _component_chain(comp, graph, start):
    chain = []
    seen = set()
    stack = [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        chain.append(n)
        for m in sorted(graph.get(n, ())):
            if m in comp and m not in seen:
                stack.append(m)
    return chain


def _load_route_geometry_inputs(db):
    """Return per-route trip stop-sequences + stop graph from the static tables."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT route_id, route_short_name
            FROM routes
            WHERE route_type IN %(types)s
            """,
            {"types": ROUTE_SCOPE_TYPES},
        )
        route_names = dict(cur.fetchall())

        cur.execute(
            "SELECT stop_id, stop_lat, stop_lon FROM stops "
            "WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL"
        )
        stop_coords = {row[0]: (float(row[1]), float(row[2])) for row in cur.fetchall()}

        cur.execute(
            """
            SELECT t.route_id, t.trip_id, st.stop_sequence, st.stop_id
            FROM stop_times st
            JOIN trips t ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            WHERE r.route_type IN %(types)s
            ORDER BY t.trip_id, st.stop_sequence
            """,
            {"types": ROUTE_SCOPE_TYPES},
        )
        trips_by_route = defaultdict(dict)
        for route_id, trip_id, _seq, stop_id in cur:
            if stop_id not in stop_coords:
                continue
            trips_by_route[route_id].setdefault(trip_id, []).append(stop_id)

    route_graphs = {}
    for route_id, trips in trips_by_route.items():
        graph = defaultdict(set)
        for seq in trips.values():
            for a, b in zip(seq, seq[1:]):
                if not _edge_ok(a, b, stop_coords):
                    continue
                graph[a].add(b)
                graph[b].add(a)
        route_graphs[route_id] = graph

    return route_names, stop_coords, trips_by_route, route_graphs


def _longest_clean_run(trips, graph):
    best = []
    for seq in trips:
        run = []
        for s in seq:
            if run and s in graph.get(run[-1], ()):
                run.append(s)
            else:
                run = [s]
            if len(run) > len(best):
                best = run
    return best


def _spider_order(graph, trips, stop_coords):
    """Order every stop on a route into one polyline.

    Walks the longest run of consecutive stops joined by real (post-pruning)
    edges as the spine, detouring out every off-spine branch (out-and-back DFS)
    so branch stops lie on the line too. Any component the main walk never
    reaches (isolated by skip-edge pruning) is walked as a coherent chain and
    spliced in right after the visited stop nearest to it, so chords are as
    short as the data allows.
    """
    all_stops = {s for seq in trips for s in seq}
    comps = _components(all_stops, graph)
    spine = _longest_clean_run(trips, graph)
    if len(spine) < 2 and trips:
        spine = max(trips, key=len)
    spine_set = set(spine)
    visited = set()
    order = []

    def detour(start):
        stack = [(start, iter(sorted(graph.get(start, ()))))]
        visited.add(start)
        order.append(start)
        while stack:
            node, children = stack[-1]
            entered = False
            for m in children:
                if m not in visited and m not in spine_set:
                    visited.add(m)
                    order.append(m)
                    stack.append((m, iter(sorted(graph.get(m, ())))))
                    entered = True
                    break
            if not entered:
                order.append(node)
                stack.pop()

    for s in spine:
        if s in visited:
            continue
        visited.add(s)
        order.append(s)
        for m in sorted(graph.get(s, ())):
            if m not in visited and m not in spine_set:
                detour(m)

    remaining = [c for c in comps if not set(c) & visited]
    for comp in remaining:
        anchor, attach = min(
            ((n, v) for n in comp for v in order),
            key=lambda nv: _dist2(nv[0], nv[1], stop_coords),
        )
        chain = _component_chain(comp, graph, anchor)
        idx = order.index(attach)
        order[idx + 1 : idx + 1] = chain
        visited.update(chain)

    return order


def regenerate_route_geometries(db):
    route_names, stop_coords, trips_by_route, route_graphs = _load_route_geometry_inputs(db)

    now_utc = datetime.now(timezone.utc)
    rows = []
    for route_id in sorted(route_names):
        trips = list(trips_by_route.get(route_id, {}).values())
        if not trips:
            continue
        graph = route_graphs.get(route_id, {})
        coords = [
            list(stop_coords[stop_id])
            for stop_id in _spider_order(graph, trips, stop_coords)
            if stop_id in stop_coords
        ]
        if len(coords) < 2:
            continue
        rows.append(
            {
                "route_id": route_id,
                "route_short_name": route_names[route_id],
                "coordinates": Json(coords),
                "updated_at": now_utc,
            }
        )

    if rows:
        upsert_table(db, "route_geometries", rows, pk_cols=["route_id"])

    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM route_geometries
            WHERE coordinates IS NULL
               OR jsonb_array_length(coordinates) < 2
               OR route_id NOT IN (
                   SELECT route_id FROM routes WHERE route_type IN %(types)s
               )
            """,
            {"types": ROUTE_SCOPE_TYPES},
        )
    db.commit()


def main():
    conn = get_connection()
    try:
        regenerate_route_geometries(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(coordinates) FROM route_geometries")
            total, with_coords = cur.fetchone()
    finally:
        conn.close()
    print(f"route_geometries: {total} routes, {with_coords} with coordinates")


if __name__ == "__main__":
    main()
