"""Geocoding, road matrix/geometry (OSRM) and per-wave stop-order optimization (OR-Tools)."""
import time

import requests
from openlocationcode import openlocationcode as olc
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

UA = {"User-Agent": "marinar-route-optimizer/0.1"}
OSRM = "https://router.project-osrm.org"  # public demo server; self-host OSRM (Iceland extract) for production
REYKJAVIK = (64.1466, -21.9426)  # reference point for short plus codes like "43QF+PQ7"

_last_nominatim = 0.0


def geocode(address=None, plus_code=None):
    """Return (lat, lon) for an address or plus code, or None if not found."""
    global _last_nominatim
    if plus_code:
        code = plus_code.strip().split()[0]
        if not olc.isValid(code):
            return None
        full = olc.recoverNearest(code, *REYKJAVIK) if olc.isShort(code) else code
        area = olc.decode(full)
        return area.latitudeCenter, area.longitudeCenter
    if not address:
        return None
    wait = 1.1 - (time.time() - _last_nominatim)  # Nominatim usage policy: max 1 req/s
    if wait > 0:
        time.sleep(wait)
    _last_nominatim = time.time()
    q = address if "iceland" in address.lower() else f"{address}, Iceland"
    hits = requests.get("https://nominatim.openstreetmap.org/search",
                        params={"q": q, "format": "json", "limit": 1}, headers=UA, timeout=20).json()
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def matrix(points):
    """points: list of (lat, lon). Returns (durations_s, distances_m) square matrices."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(f"{OSRM}/table/v1/driving/{coords}", params={"annotations": "duration,distance"},
                     headers=UA, timeout=30).json()
    return r["durations"], r["distances"]


def geometry(points):
    """Road polyline [[lat, lon], ...] through the given points in order."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(f"{OSRM}/route/v1/driving/{coords}", params={"overview": "full", "geometries": "geojson"},
                     headers=UA, timeout=30).json()
    return [[lat, lon] for lon, lat in r["routes"][0]["geometry"]["coordinates"]]


def seq_cost(seq, idx, dur, dist):
    t = d = 0.0
    for a, b in zip(seq, seq[1:]):
        t += dur[idx[a]][idx[b]]
        d += dist[idx[a]][idx[b]]
    return t, d


def link_problems(stops, links, names=None):
    """Human-readable problems with a wave's pickup links (missing stops, drop before pickup)."""
    pos = {s["loc"]: i for i, s in enumerate(stops)}
    nm = lambda k: (names or {}).get(k, k)
    out = []
    for l in links:
        if l["pickup"] not in pos or l["drop"] not in pos:
            out.append(f"link {nm(l['pickup'])} → {nm(l['drop'])} refers to a stop not in this wave")
        elif pos[l["pickup"]] > pos[l["drop"]]:
            out.append(f"{nm(l['drop'])} is visited before its pickup {nm(l['pickup'])}")
    return out


def _feasible_order(stops, links):
    """Move pickups in front of their drops until every link is satisfied (keeps order otherwise)."""
    order = list(stops)
    for _ in range(len(order) * len(links) + 1):
        pos = {s["loc"]: i for i, s in enumerate(order)}
        bad = next((l for l in links if pos[l["pickup"]] > pos[l["drop"]]), None)
        if bad is None:
            return order
        p = order.pop(pos[bad["pickup"]])
        order.insert(pos[bad["drop"]], p)
    raise ValueError("pickup links form a cycle")


def optimize_wave(depot, stops, links, idx, dur):
    """Best order for one wave: depot -> stops -> depot, each linked pickup visited before its drop."""
    if len(stops) < 2:
        return list(stops)
    start = _feasible_order(stops, links)
    nodes = [depot] + [s["loc"] for s in start]
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
    node_of = {loc: k for k, loc in enumerate(nodes) if k}
    for link in links:
        pi, di = manager.NodeToIndex(node_of[link["pickup"]]), manager.NodeToIndex(node_of[link["drop"]])
        routing.solver().Add(time_dim.CumulVar(pi) < time_dim.CumulVar(di))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 1
    # Warm-start from a feasible order; precedence-only side constraints can otherwise leave
    # the first-solution heuristics with nothing to start from.
    routing.CloseModelWithParameters(params)
    initial = routing.ReadAssignmentFromRoutes([list(range(1, n))], True)
    sol = routing.SolveFromAssignmentWithParameters(initial, params)
    if sol is None:
        return start

    order, i = [], routing.Start(0)
    while not routing.IsEnd(i):
        node = manager.IndexToNode(i)
        if node:
            order.append(start[node - 1])
        i = sol.Value(routing.NextVar(i))
    return order
