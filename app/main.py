"""Route optimizer API + static pages.

Run:  .venv\\Scripts\\python -m uvicorn app.main:app --port 8000
"""
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import routing

ROOT = Path(__file__).resolve().parent.parent
# In Azure, DB_PATH points at the mounted Azure Files share so data survives restarts/redeploys.
DB = Path(os.environ.get("DB_PATH", ROOT / "data" / "app.db"))
DB.parent.mkdir(parents=True, exist_ok=True)
WAVE_COLORS = ["#e6b800", "#22a822", "#e0147a", "#3b82f6", "#f97316"]

app = FastAPI(title="Route optimizer")


# ---------- storage ----------

@contextmanager
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS locations (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, plus_code TEXT,
                lat REAL NOT NULL, lon REAL NOT NULL, is_depot INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, driver TEXT NOT NULL,
                sort INTEGER NOT NULL DEFAULT 0, trips TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS geometry_cache (seq TEXT PRIMARY KEY, coords TEXT NOT NULL);
        """)
        if con.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
            seed(con)


def seed(con):
    """First run: import the sample route + geocode cache from the prototype."""
    sample = json.loads((ROOT / "data" / "route_sample.json").read_text("utf-8"))
    cache_file = ROOT / "data" / "geocode_cache.json"
    cache = json.loads(cache_file.read_text("utf-8")) if cache_file.exists() else {}
    for key, loc in sample["locations"].items():
        lat, lon = cache.get(key) or routing.geocode(loc.get("address"), loc.get("plus_code"))
        con.execute("INSERT INTO locations VALUES (?,?,?,?,?,?,?)",
                    (key, loc["name"], loc.get("address"), loc.get("plus_code"), lat, lon,
                     int(key == sample["depot"])))
    trips = [{"label": t["label"], "color": t["color"], "stops": t["stops"], "links": t.get("links", [])}
             for t in sample["trips"]]
    con.execute("INSERT INTO routes (driver, sort, trips) VALUES (?,?,?)",
                (sample.get("driver", "Driver 1"), 0, json.dumps(trips, ensure_ascii=False)))


def slugify(name):
    s = unicodedata.normalize("NFKD", name.replace("ð", "d").replace("Ð", "D").replace("þ", "th").replace("Þ", "Th")
                              .replace("æ", "ae").replace("Æ", "Ae"))
    return re.sub(r"[^a-z0-9]+", "_", s.encode("ascii", "ignore").decode().lower()).strip("_") or "loc"


def all_locations(con):
    return {r["key"]: dict(r) for r in con.execute("SELECT * FROM locations ORDER BY name")}


def depot_key(locs):
    return next((k for k, l in locs.items() if l["is_depot"]), None)


def route_row(r):
    return {"id": r["id"], "driver": r["driver"], "sort": r["sort"], "trips": json.loads(r["trips"])}


# ---------- locations ----------

class LocationIn(BaseModel):
    name: str
    address: str | None = None
    plus_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    is_depot: bool = False


def _resolve_coords(body: LocationIn):
    if body.lat is not None and body.lon is not None:
        return body.lat, body.lon
    hit = routing.geocode(body.address, body.plus_code)
    if not hit:
        raise HTTPException(422, "Could not find that address/plus code — set the position on the map instead.")
    return hit


@app.get("/api/locations")
def list_locations():
    with db() as con:
        return list(all_locations(con).values())


@app.post("/api/locations")
def create_location(body: LocationIn):
    lat, lon = _resolve_coords(body)
    with db() as con:
        key, base, n = slugify(body.name), slugify(body.name), 2
        while con.execute("SELECT 1 FROM locations WHERE key=?", (key,)).fetchone():
            key, n = f"{base}_{n}", n + 1
        if body.is_depot:
            con.execute("UPDATE locations SET is_depot=0")
        con.execute("INSERT INTO locations VALUES (?,?,?,?,?,?,?)",
                    (key, body.name, body.address, body.plus_code, lat, lon, int(body.is_depot)))
        return all_locations(con)[key]


@app.put("/api/locations/{key}")
def update_location(key: str, body: LocationIn):
    with db() as con:
        old = con.execute("SELECT * FROM locations WHERE key=?", (key,)).fetchone()
        if not old:
            raise HTTPException(404, "Location not found")
        moved = (body.address or None) != old["address"] or (body.plus_code or None) != old["plus_code"]
        if body.lat is not None and body.lon is not None:
            lat, lon = body.lat, body.lon
        elif moved:
            lat, lon = _resolve_coords(body)
        else:
            lat, lon = old["lat"], old["lon"]
        if body.is_depot:
            con.execute("UPDATE locations SET is_depot=0")
        con.execute("UPDATE locations SET name=?, address=?, plus_code=?, lat=?, lon=?, is_depot=? WHERE key=?",
                    (body.name, body.address or None, body.plus_code or None, lat, lon, int(body.is_depot), key))
        return all_locations(con)[key]


@app.delete("/api/locations/{key}")
def delete_location(key: str):
    with db() as con:
        used = [r["driver"] for r in con.execute("SELECT driver, trips FROM routes")
                if any(s["loc"] == key for t in json.loads(r["trips"]) for s in t["stops"])]
        if used:
            raise HTTPException(409, f"Location is used on: {', '.join(used)}")
        con.execute("DELETE FROM locations WHERE key=?", (key,))
    return {"ok": True}


# ---------- routes ----------

class Stop(BaseModel):
    loc: str
    order: str | None = None


class Link(BaseModel):
    pickup: str
    drop: str


class Trip(BaseModel):
    label: str
    color: str | None = None
    stops: list[Stop] = []
    links: list[Link] = []


class RouteIn(BaseModel):
    driver: str
    trips: list[Trip] = []


def _validate(body: RouteIn, locs, check_order=True):
    """check_order=False for optimizing: a pickup after its drop is fine there, the optimizer fixes it."""
    errors = []
    names = {k: l["name"] for k, l in locs.items()}
    for t in body.trips:
        keys = [s.loc for s in t.stops]
        if unknown := [k for k in keys if k not in locs]:
            errors.append(f"{t.label}: unknown location(s) {', '.join(unknown)}")
        if len(keys) != len(set(keys)):
            errors.append(f"{t.label}: a location can only appear once per wave")
        problems = routing.link_problems([s.model_dump() for s in t.stops], [l.model_dump() for l in t.links], names)
        errors += [f"{t.label}: {p}" for p in problems if check_order or "before its pickup" not in p]
    if errors:
        raise HTTPException(422, "; ".join(errors))


def _trips_json(body: RouteIn):
    trips = []
    for i, t in enumerate(body.trips):
        d = t.model_dump(exclude_none=True)
        d.setdefault("color", WAVE_COLORS[i % len(WAVE_COLORS)])
        trips.append(d)
    return json.dumps(trips, ensure_ascii=False)


@app.get("/api/routes")
def list_routes():
    with db() as con:
        return [route_row(r) for r in con.execute("SELECT * FROM routes ORDER BY sort, id")]


@app.post("/api/routes")
def create_route(body: RouteIn):
    with db() as con:
        _validate(body, all_locations(con))
        sort = con.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM routes").fetchone()[0]
        cur = con.execute("INSERT INTO routes (driver, sort, trips) VALUES (?,?,?)",
                          (body.driver, sort, _trips_json(body)))
        return route_row(con.execute("SELECT * FROM routes WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/routes/{rid}")
def update_route(rid: int, body: RouteIn):
    with db() as con:
        _validate(body, all_locations(con))
        if con.execute("UPDATE routes SET driver=?, trips=? WHERE id=?",
                       (body.driver, _trips_json(body), rid)).rowcount == 0:
            raise HTTPException(404, "Route not found")
        return route_row(con.execute("SELECT * FROM routes WHERE id=?", (rid,)).fetchone())


@app.delete("/api/routes/{rid}")
def delete_route(rid: int):
    with db() as con:
        con.execute("DELETE FROM routes WHERE id=?", (rid,))
    return {"ok": True}


@app.post("/api/optimize")
def optimize(body: RouteIn):
    """Optimize an (unsaved) route's stop order per wave. Returns the reordered route, not saved."""
    with db() as con:
        locs = all_locations(con)
    _validate(body, locs, check_order=False)
    depot = depot_key(locs)
    if not depot:
        raise HTTPException(422, "Mark one location as the depot (HQ) first.")
    keys = list({depot, *(s.loc for t in body.trips for s in t.stops)})
    idx = {k: i for i, k in enumerate(keys)}
    dur, dist = routing.matrix([(locs[k]["lat"], locs[k]["lon"]) for k in keys])
    out = body.model_dump()
    for t in out["trips"]:
        t["stops"] = routing.optimize_wave(depot, t["stops"], t["links"], idx, dur)
    return out


# ---------- plan (map page) ----------

def _geometry(con, seq, locs):
    key = ">".join(seq)
    hit = con.execute("SELECT coords FROM geometry_cache WHERE seq=?", (key,)).fetchone()
    if hit:
        return json.loads(hit[0])
    coords = routing.geometry([(locs[k]["lat"], locs[k]["lon"]) for k in seq])
    con.execute("INSERT OR REPLACE INTO geometry_cache VALUES (?,?)", (key, json.dumps(coords)))
    return coords


@app.get("/api/plan")
def plan():
    """Every driver's route as saved ('current') and optimized, with road geometry and km/min."""
    with db() as con:
        locs = all_locations(con)
        routes = [route_row(r) for r in con.execute("SELECT * FROM routes ORDER BY sort, id")]
        depot = depot_key(locs)
        if not depot:
            raise HTTPException(422, "Mark one location as the depot (HQ) first.")
        keys = list({depot, *(s["loc"] for r in routes for t in r["trips"] for s in t["stops"])})
        idx = {k: i for i, k in enumerate(keys)}
        dur, dist = routing.matrix([(locs[k]["lat"], locs[k]["lon"]) for k in keys])
        drivers = []
        for r in routes:
            plans = {}
            for name in ("current", "optimized"):
                trips, tot_t, tot_d = [], 0.0, 0.0
                for t in r["trips"]:
                    stops = t["stops"] if name == "current" else routing.optimize_wave(depot, t["stops"], t["links"], idx, dur)
                    seq = [depot] + [s["loc"] for s in stops] + [depot]
                    secs, meters = routing.seq_cost(seq, idx, dur, dist)
                    tot_t, tot_d = tot_t + secs, tot_d + meters
                    trips.append({"label": t["label"], "color": t["color"], "seq": seq, "links": t["links"],
                                  "minutes": round(secs / 60, 1), "km": round(meters / 1000, 1),
                                  "geometry": _geometry(con, seq, locs) if stops else []})
                plans[name] = {"trips": trips, "minutes": round(tot_t / 60, 1), "km": round(tot_d / 1000, 1)}
            drivers.append({"id": r["id"], "driver": r["driver"], "plans": plans})
        return {"depot": depot, "locations": locs, "drivers": drivers}


init_db()
app.get("/")(lambda: RedirectResponse("/map.html"))
app.mount("/", StaticFiles(directory=ROOT / "web"), name="web")
