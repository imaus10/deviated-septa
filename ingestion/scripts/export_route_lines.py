"""
One-shot script: export one stop-to-stop polyline per bus/trolley route
as a JSON file consumed by the frontend.

Run from ingestion/:
    source ../.env && uv run python -m scripts.export_route_lines

Re-run whenever SEPTA publishes a new GTFS static feed (every few months).
"""

import json
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

OUTPUT = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "frontend", "public", "route-lines.json",
)

SQL = """
WITH ranked AS (
    SELECT DISTINCT ON (t.route_id)
        t.route_id,
        t.trip_id,
        r.route_short_name,
        COUNT(*) AS stop_count
    FROM trips t
    JOIN routes r ON r.route_id = t.route_id
    JOIN stop_times st ON st.trip_id = t.trip_id
    WHERE r.route_type IN (0, 3)
    GROUP BY t.route_id, t.trip_id, r.route_short_name
    ORDER BY t.route_id, stop_count DESC
)
SELECT
    r.route_id,
    r.route_short_name,
    json_agg(
        json_build_array(s.stop_lat, s.stop_lon)
        ORDER BY st.stop_sequence
    ) FILTER (WHERE s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL) AS coordinates
FROM ranked r
JOIN stop_times st ON st.trip_id = r.trip_id
JOIN stops s ON s.stop_id = st.stop_id
GROUP BY r.route_id, r.route_short_name
ORDER BY r.route_id;
"""


def main():
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(SQL)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    features = []
    for route_id, name, coords in rows:
        if not coords or len(coords) < 2:
            continue
        features.append({
            "route_id": route_id,
            "route_name": name,
            "coordinates": coords,
        })

    with open(OUTPUT, "w") as f:
        json.dump(features, f)

    print(f"Wrote {len(features)} route lines to {OUTPUT}")


if __name__ == "__main__":
    main()
