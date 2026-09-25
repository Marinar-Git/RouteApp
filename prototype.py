"""Phase-1 prototype: geocode a route, build a road matrix, optimize stop order, render a map.

Usage:  .venv\\Scripts\\python prototype.py data\\route_sample.json
"""
import json
import sys
import time
from pathlib import Path

import requests
from openlocationcode import openlocationcode as olc
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

UA = {"User-Agent": "marinar-route-optimizer-prototype/0.1"}
OSRM = "https://router.project-osrm.org"  # public demo server; self-host OSRM (Iceland extract) for production
CACHE = Path("data/geocode_cache.json")


def geocode(locs):
    cache = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {}
    for key, loc in locs.items():
        if key in cache:
            loc["lat"], loc["lon"] = cache[key]
            continue
        if "plus_code" in loc:
            full = olc.recoverNearest(loc["plus_code"], *loc["plus_code_ref"])
            area = olc.decode(full)
            loc["lat"], loc["lon"] = area.latitudeCenter, area.longitudeCenter
        else:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": loc["address"], "format": "json", "limit": 1}, headers=UA, timeout=20)
            hits = r.json()
            if not hits:
                sys.exit(f"Could not geocode {key}: {loc['address']}")
            loc["lat"], loc["lon"] = float(hits[0]["lat"]), float(hits[0]["lon"])
            time.sleep(1.1)  # Nominatim usage policy
        cache[key] = [loc["lat"], loc["lon"]]
    CACHE.write_text(json.dumps(cache, indent=2), "utf-8")


def osrm_matrix(keys, locs):
    coords = ";".join(f"{locs[k]['lon']},{locs[k]['lat']}" for k in keys)
    r = requests.get(f"{OSRM}/table/v1/driving/{coords}", params={"annotations": "duration,distance"},
                     headers=UA, timeout=30).json()
    return r["durations"], r["distances"]


def osrm_geometry(seq, locs):
    coords = ";".join(f"{locs[k]['lon']},{locs[k]['lat']}" for k in seq)
    r = requests.get(f"{OSRM}/route/v1/driving/{coords}", params={"overview": "full", "geometries": "geojson"},
                     headers=UA, timeout=30).json()
    return [[lat, lon] for lon, lat in r["routes"][0]["geometry"]["coordinates"]]


def trip_cost(seq, idx, dur, dist):
    d = t = 0.0
    for a, b in zip(seq, seq[1:]):
        t += dur[idx[a]][idx[b]]
        d += dist[idx[a]][idx[b]]
    return t, d


def optimize_trip(depot, stops, links, idx, dur):
    """Best order for one trip: depot -> stops -> depot, each linked pickup visited before its drop."""
    nodes = [depot] + [s["loc"] for s in stops]
    n = len(nodes)
    manager = pywrapcp.RoutingIndexManager(n, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def cost(i, j):
        return int(dur[idx[nodes[manager.IndexToNode(i)]]][idx[nodes[manager.IndexToNode(j)]]])

    cb = routing.RegisterTransitCallback(cost)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)
    routing.AddDimension(cb, 0, 24 * 3600, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    # One pickup may feed several drops (e.g. Tokyo Nýbýlavegur -> Hringbraut + Hallveigarstígur).
    node_of = {s["loc"]: n for n, s in enumerate(stops, start=1)}
    for link in links:
        pi, di = manager.NodeToIndex(node_of[link["pickup"]]), manager.NodeToIndex(node_of[link["drop"]])
        routing.solver().Add(time_dim.CumulVar(pi) < time_dim.CumulVar(di))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 2
    # Warm-start from the current order (already feasible); precedence-only side constraints
    # can otherwise leave the first-solution heuristics with nothing to start from.
    routing.CloseModelWithParameters(params)
    initial = routing.ReadAssignmentFromRoutes([list(range(1, n))], True)
    sol = routing.SolveFromAssignmentWithParameters(initial, params)
    if sol is None:
        return stops

    order, i = [], routing.Start(0)
    while not routing.IsEnd(i):
        node = manager.IndexToNode(i)
        if node:
            order.append(stops[node - 1])
        i = sol.Value(routing.NextVar(i))
    return order


def main(path):
    route = json.loads(Path(path).read_text("utf-8"))
    locs, depot = route["locations"], route["depot"]
    geocode(locs)
    keys = list(locs)
    idx = {k: i for i, k in enumerate(keys)}
    dur, dist = osrm_matrix(keys, locs)

    result = {"name": route["name"], "driver": route.get("driver", "Driver"), "locations": locs, "depot": depot, "plans": {}}
    for plan in ("current", "optimized"):
        trips, tot_t, tot_d = [], 0.0, 0.0
        for trip in route["trips"]:
            stops = trip["stops"] if plan == "current" else optimize_trip(depot, trip["stops"], trip.get("links", []), idx, dur)
            seq = [depot] + [s["loc"] for s in stops] + [depot]
            t, d = trip_cost(seq, idx, dur, dist)
            tot_t += t
            tot_d += d
            trips.append({"label": trip["label"], "color": trip["color"], "seq": seq,
                          "orders": [s["order"] for s in stops], "links": trip.get("links", []),
                          "minutes": round(t / 60, 1), "km": round(d / 1000, 1),
                          "geometry": osrm_geometry(seq, locs)})
        result["plans"][plan] = {"trips": trips, "minutes": round(tot_t / 60, 1), "km": round(tot_d / 1000, 1)}

    for plan, p in result["plans"].items():
        print(f"\n== {plan.upper()}: {p['km']} km, {p['minutes']} min driving")
        for t in p["trips"]:
            print(f"  {t['label']}: {t['km']} km / {t['minutes']} min  ->  " + " > ".join(locs[k]["name"] for k in t["seq"]))

    html = Path("map_template.html").read_text("utf-8").replace("__DATA__", json.dumps(result, ensure_ascii=False))
    Path("route_map.html").write_text(html, "utf-8")
    print("\nMap written to route_map.html")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/route_sample.json")
