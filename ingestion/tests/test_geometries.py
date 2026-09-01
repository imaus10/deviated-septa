from poller.route_geometries import build_geometries, _spider_order


def _data():
    return {
        "routes": {
            "42": {"route_name": "42", "route_type": 3},
            "10": {"route_name": "10", "route_type": 0},
        },
        "stops": {
            "A": {"stop_name": "A", "stop_lat": 39.95, "stop_lon": -75.16},
            "B": {"stop_name": "B", "stop_lat": 39.96, "stop_lon": -75.17},
            "C": {"stop_name": "C", "stop_lat": 39.97, "stop_lon": -75.18},
            "NOLAT": {"stop_name": "NoLat", "stop_lat": None, "stop_lon": -75.16},
        },
        "trips": {
            "t1": {"route_id": "42", "service_id": "wk", "direction_id": 0},
            "t2": {"route_id": "10", "service_id": "wk", "direction_id": 0},
        },
        "stop_times": {
            ("t1", 1): {"arrival_time": "08:00:00", "stop_id": "A"},
            ("t1", 2): {"arrival_time": "08:05:00", "stop_id": "B"},
            ("t1", 3): {"arrival_time": "08:10:00", "stop_id": "C"},
            ("t2", 1): {"arrival_time": "08:00:00", "stop_id": "A"},
            ("t2", 2): {"arrival_time": "08:03:00", "stop_id": "B"},
        },
        "calendar": {},
    }


class TestBuildGeometries:
    def test_emits_route_with_ordered_coordinates(self):
        geo = build_geometries(_data())
        by_id = {g["route_id"]: g for g in geo}
        assert "42" in by_id
        assert by_id["42"]["route_name"] == "42"
        # in stop_sequence order A->B->C
        assert by_id["42"]["coordinates"] == [
            [39.95, -75.16],
            [39.96, -75.17],
            [39.97, -75.18],
        ]

    def test_sorted_by_route_id(self):
        geo = build_geometries(_data())
        assert [g["route_id"] for g in geo] == sorted(g["route_id"] for g in geo)

    def test_skips_route_with_fewer_than_two_coords(self):
        data = _data()
        # give "10" only a single stop (A) -> no polyline
        data["stop_times"] = {k: v for k, v in data["stop_times"].items() if k != ("t2", 2)}
        geo = build_geometries(data)
        assert all(g["route_id"] != "10" for g in geo)

    def test_skips_missing_coords_and_missing_stoptime(self):
        data = _data()
        # "42"'s only stops: NOLAT (no coords) + GHOST (unknown) -> no polyline
        data["stop_times"] = {
            ("t1", 1): {"arrival_time": "08:10:00", "stop_id": "NOLAT"},
            ("t1", 2): {"arrival_time": "08:15:00", "stop_id": "GHOST"},
        }
        geo = build_geometries(data)
        assert all(g["route_id"] != "42" for g in geo)


class TestSpiderOrder:
    def test_orders_branch_so_all_stops_on_line(self):
        graph = {
            "A": {"B"},
            "B": {"A", "C", "D"},
            "C": {"B"},
            "D": {"B"},
        }
        trips = [["A", "B", "C"]]
        coords = {
            "A": (39.95, -75.16),
            "B": (39.96, -75.17),
            "C": (39.97, -75.18),
            "D": (39.965, -75.175),
        }
        order = _spider_order(graph, trips, coords)
        assert set(order) == {"A", "B", "C", "D"}
