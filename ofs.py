"""ofs.py — cover a whole transit line with however many current models it takes.

THE PROBLEM THIS SOLVES. A NOAA Operational Forecast System is a REGIONAL model with a
hard domain edge, and a transit does not care where that edge is. The supplied Lewes
line runs 10.4 NM past DBOFS's southern boundary at 37.82 N, so the last stretch had
no current at all. CBOFS — the Chesapeake model — reaches down to 36.2 N and out
across the Delmarva shelf, and its nearest water node to that gap is 0.6 km away
against DBOFS's 20.6 km. One extra fetch closes the hole.

So: probe which models actually reach the line, fetch each one scoped to the part it
is needed for, and serve them through a single source that tries the FINEST model
first and falls through. Every sample records which model answered.

FINEST FIRST, AND WHY. Where two models overlap they disagree slightly — different
grids, different bathymetry, different assimilation. The regional model with the
smaller cell is the better answer inshore, so it wins. But a hard switch at the
boundary puts a step discontinuity in the middle of a leg, which shows up as a
kink in the ETA. Inside `BLEND_KM` of the finer model's edge the two are blended by
distance, so the handover is smooth and the transit does not jump.

WHAT THIS DOES NOT DO: invent coverage. If no model reaches, the answer is still
None, still counted as a gap, still reported. Chaining widens the honest answer; it
does not replace it.
"""

import json
import math
import os
import threading
import time

import currents as cmod
import geo

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DOMAIN_CACHE = os.path.join(APP_DIR, 'ofs_cache', 'domains.json')

# Occupancy-raster cell size, degrees. ~5.5 km at these latitudes: fine enough to
# resolve a model boundary to well inside a leg, coarse enough that a domain stores
# as a few thousand short strings.
CELL = 0.05

# Blend width at a model handover, km. Roughly a few grid cells: wide enough to kill
# the step, narrow enough that the finer model still owns its interior.
BLEND_KM = 15.0

# The regional OFS models on the CO-OPS THREDDS, coarse-listed with a nominal
# bounding box so we can rule most of them out without a network call. The nominal
# box is a SCREEN ONLY — the true domain is irregular and is probed and cached the
# first time a model is actually considered. `rank` is finest-first for overlaps.
REGISTRY = [
    {'ofs': 'dbofs', 'name': 'Delaware Bay',      'rank': 10, 'nominal': (37.8, -75.9, 40.3, -73.2)},
    {'ofs': 'cbofs', 'name': 'Chesapeake Bay',    'rank': 20, 'nominal': (36.1, -77.4, 39.7, -74.5)},
    {'ofs': 'nyofs', 'name': 'NY / NJ Harbor',    'rank': 10, 'nominal': (39.8, -74.6, 41.5, -72.6)},
    {'ofs': 'sfbofs', 'name': 'San Francisco Bay', 'rank': 10, 'nominal': (36.8, -123.2, 38.5, -121.3)},
    {'ofs': 'ngofs2', 'name': 'Northern Gulf',    'rank': 20, 'nominal': (27.0, -98.5, 31.0, -87.5)},
    {'ofs': 'tbofs', 'name': 'Tampa Bay',         'rank': 10, 'nominal': (27.0, -83.5, 28.2, -82.3)},
    {'ofs': 'leofs', 'name': 'Lake Erie',         'rank': 10, 'nominal': (41.3, -83.6, 43.0, -78.8)},
    {'ofs': 'lmhofs', 'name': 'Lake Michigan/Huron', 'rank': 10, 'nominal': (41.5, -88.2, 46.5, -79.9)},
    {'ofs': 'loofs', 'name': 'Lake Ontario',      'rank': 10, 'nominal': (43.1, -79.9, 44.4, -75.9)},
    {'ofs': 'lsofs', 'name': 'Lake Superior',     'rank': 10, 'nominal': (46.4, -92.2, 49.1, -84.3)},
]

_domains = None
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  Domain discovery                                                            #
# --------------------------------------------------------------------------- #
def _load_domains():
    global _domains
    with _lock:
        if _domains is not None:
            return _domains
        try:
            with open(DOMAIN_CACHE, 'r', encoding='utf-8') as f:
                _domains = json.load(f)
        except (OSError, ValueError):
            _domains = {}
        return _domains


def _save_domains():
    os.makedirs(os.path.dirname(DOMAIN_CACHE), exist_ok=True)
    tmp = DOMAIN_CACHE + '.part'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_domains, f, indent=1)
    os.replace(tmp, DOMAIN_CACHE)


def probe_domain(ofs, stride=6, timeout_ok=True):
    """The model's real WATER extent, subsampled, cached to disk.

    Not the array corners: a `regulargrid` file is a rectangle with FILL where the
    irregular model domain does not reach, and its corners read back as lat/lon 2.0
    or 89.0. Reading the corners tells you nothing — this walks the grid with a
    stride, keeps only unmasked water nodes, and returns their hull.
    """
    d = _load_domains()
    if ofs in d:
        return d[ofs]
    try:
        av = cmod.available_cycles(ofs, days_back=2)
        if not av:
            return None
        datestr, cycle, hours = av[0]
        url = cmod._file_url(ofs, datestr, cycle, hours[0])
        dds = cmod._get(url + '.dds').decode('utf8', 'replace')
        import re
        m = re.search(r'Latitude\[ny\s*=\s*(\d+)\]\[nx\s*=\s*(\d+)\]', dds)
        if not m:
            return None
        ny, nx = int(m.group(1)), int(m.group(2))
        proj = (f'Latitude[0:{stride}:{ny - 1}][0:{stride}:{nx - 1}],'
                f'Longitude[0:{stride}:{ny - 1}][0:{stride}:{nx - 1}],'
                f'mask[0:{stride}:{ny - 1}][0:{stride}:{nx - 1}]')
        got = cmod.dap_fetch(url, proj)
        lat, lon, mask = (_flat(got['Latitude']), _flat(got['Longitude']), _flat(got['mask']))
        pts = [(a, o) for a, o, k in zip(lat, lon, mask)
               if k > 0.5 and -90 <= a <= 90 and -180 <= o <= 180]
        # Fill leaks through as exactly 2.0 / 89.0 / 85.0 pairs; a real node never has
        # lat == lon to the bit, so that pairing is a safe discriminator.
        pts = [(a, o) for a, o in pts if abs(a - o) > 1e-9]
        if not pts:
            return None
        # AN OCCUPANCY RASTER, NOT A LIST OF NODES. The first version of this kept a
        # thinned sample of water nodes and asked "how far to the nearest one",
        # which is wrong twice over: the thinning is row-major so the retained
        # nodes are not evenly spread, and the answer then depends on how many were
        # kept rather than on the model. It let DBOFS claim a point 20 km beyond its
        # own southern boundary. A coarse lat/lon occupancy grid asks the right
        # question — "does this model have water in this cell" — at a fixed,
        # stated resolution, and stores compactly.
        occ = sorted({f'{int(math.floor(a / CELL))},{int(math.floor(o / CELL))}'
                      for a, o in pts})
        info = {'ofs': ofs, 'ny': ny, 'nx': nx, 'stride': stride, 'cell_deg': CELL,
                'lat0': min(p[0] for p in pts), 'lat1': max(p[0] for p in pts),
                'lon0': min(p[1] for p in pts), 'lon1': max(p[1] for p in pts),
                'occupied': occ,
                'probed': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        with _lock:
            _domains[ofs] = info
            _save_domains()
        return info
    except Exception:
        return None


def _flat(v):
    out = []

    def rec(x):
        if isinstance(x, (list, tuple)):
            for y in x:
                rec(y)
        else:
            out.append(float(x))
    rec(v)
    return out


def covers(info, lat, lon, rings=1):
    """Does this model have water at (lat, lon)?

    A cell lookup, plus `rings` of neighbours so a point just off a cell corner is
    not refused on a rounding accident. rings=1 gives a tolerance of one CELL
    (~5.5 km) — comfortably finer than the ~20 km error the old nearest-node test
    made, and it is a STATED tolerance rather than an artifact of subsampling.

    A bounding box is not enough on its own: it says yes for a point in the middle
    of a bay the model does not resolve, and for the fill corners of the rectangle.
    """
    occ = info.get('_occset')
    if occ is None:
        occ = set(info.get('occupied') or ())
        info['_occset'] = occ
    ci, cj = int(math.floor(lat / CELL)), int(math.floor(lon / CELL))
    for di in range(-rings, rings + 1):
        for dj in range(-rings, rings + 1):
            if f'{ci + di},{cj + dj}' in occ:
                return True
    return False


# --------------------------------------------------------------------------- #
#  Which models does this line need?                                           #
# --------------------------------------------------------------------------- #
def plan_coverage(points, rings=1, probe=True):
    """Which OFS models are needed to cover `points`, and what each contributes.

    Returns {'models': [...], 'uncovered': [(lat,lon), ...], 'covered_fraction': f}.
    Each model entry carries the count of sample points it is the FINEST available
    model for, so a model that adds nothing is not fetched.
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    box = (min(lats), min(lons), max(lats), max(lons))

    cands = [r for r in REGISTRY if _boxes_touch(box, r['nominal'], pad=0.75)]
    infos = []
    for r in cands:
        info = probe_domain(r['ofs']) if probe else _load_domains().get(r['ofs'])
        if info:
            infos.append((r, info))
    infos.sort(key=lambda t: t[0]['rank'])

    # Sample along the line, not just at vertices — a leg can leave and re-enter.
    samples = _sample_line(points, step_km=5.0)
    claims = {r['ofs']: 0 for r, _ in infos}
    uncovered = []
    for lat, lon in samples:
        winner = None
        for r, info in infos:
            if covers(info, lat, lon, rings=rings):
                winner = r['ofs']
                break
        if winner:
            claims[winner] += 1
        else:
            uncovered.append((lat, lon))

    models = []
    for r, info in infos:
        if claims[r['ofs']] == 0:
            continue
        models.append({'ofs': r['ofs'], 'name': r['name'], 'rank': r['rank'],
                       'claims': claims[r['ofs']],
                       'share': claims[r['ofs']] / max(1, len(samples)),
                       'lat0': info['lat0'], 'lat1': info['lat1'],
                       'lon0': info['lon0'], 'lon1': info['lon1']})
    return {'models': models, 'uncovered': uncovered,
            'samples': len(samples),
            'covered_fraction': 1.0 - len(uncovered) / max(1, len(samples))}


def _boxes_touch(a, b, pad=0.0):
    return not (a[2] + pad < b[0] or a[0] - pad > b[2] or
                a[3] + pad < b[1] or a[1] - pad > b[3])


def _sample_line(points, step_km=5.0):
    out = []
    for i in range(len(points) - 1):
        d, brg, _ = geo.inverse(*points[i], *points[i + 1])
        n = max(1, int(math.ceil((d / 1000.0) / step_km)))
        for k in range(n):
            out.append(geo.direct(points[i][0], points[i][1], brg, d * k / n))
    out.append(tuple(points[-1]))
    return out


# --------------------------------------------------------------------------- #
#  A source that spans several cycles                                          #
# --------------------------------------------------------------------------- #
class MultiCurrents:
    """Several cached OFS cycles behind one `query(lat, lon, when)`.

    Tries them finest-first. Near the edge of whichever model answered, blends with
    the next one that also has a value, weighted by distance into the overlap, so a
    handover does not put a step in the middle of a leg.

    The return matches `transit.CurrentSource`: (set_deg, drift_kt, quality). The
    model that answered is recorded in `.last_source` and tallied in `.tally`, so the
    result can say "DBOFS for 87%, CBOFS for the rest" instead of just "currents".
    """

    def __init__(self, cycles, allow_projection=True, blend_km=BLEND_KM):
        # cycles: [(rank, ofs, Currents, domain_info_or_None)] — sorted finest first
        self.cycles = sorted(cycles, key=lambda t: t[0])
        self.allow_projection = allow_projection
        self.blend_km = blend_km
        self.tally = {}
        self.last_source = None
        self.tag = '+'.join(c[2].tag for c in self.cycles) if self.cycles else None

    def _one(self, cyc, lat, lon, when):
        try:
            r = cyc.at(lat, lon, when)
            if r is not None:
                return r[1], r[0], 'measured'
        except ValueError:
            pass
        except Exception:
            return None
        if not self.allow_projection:
            return None
        try:
            best = cyc.at_best(lat, lon, when)
        except Exception:
            return None
        if not best or best[0] is None:
            return None
        vals, shift = best[0], best[1]
        return vals[1], vals[0], ('measured' if abs(shift or 0) < 1e-9 else 'projected')

    def query(self, lat, lon, when):
        hits = []
        for rank, ofs, cyc, info in self.cycles:
            got = self._one(cyc, lat, lon, when)
            if got is not None:
                edge = _edge_distance_km(cyc, lat, lon)
                hits.append((rank, ofs, got, edge))
        if not hits:
            self.last_source = None
            return None, None, None

        primary = hits[0]
        self.last_source = primary[1]
        self.tally[primary[1]] = self.tally.get(primary[1], 0) + 1
        if len(hits) == 1 or primary[3] >= self.blend_km:
            return primary[2]

        # Inside the blend band: mix the primary with the next model that answered.
        other = hits[1]
        w = 0.5 + 0.5 * (primary[3] / self.blend_km)      # 0.5 at the edge -> 1.0 inside
        return _blend(primary[2], other[2], w)

    def sources(self):
        return [{'ofs': ofs, 'tag': cyc.tag, 'rank': rank,
                 'samples': self.tally.get(ofs, 0)} for rank, ofs, cyc, _ in self.cycles]


def _edge_distance_km(cyc, lat, lon):
    """How far inside its own rectangular grid the point sits. Cheap and adequate:
    the blend only needs to know 'near a boundary', not which boundary."""
    lat1 = cyc.lat0 + (cyc.ny - 1) * cyc.dlat
    lon1 = cyc.lon0 + (cyc.nx - 1) * cyc.dlon
    cosl = math.cos(math.radians(lat))
    return min((lat - cyc.lat0) * 111.32, (lat1 - lat) * 111.32,
               (lon - cyc.lon0) * 111.32 * cosl, (lon1 - lon) * 111.32 * cosl)


def _blend(a, b, w):
    """Weighted mean of two (set, drift, quality) answers — as VECTORS.

    Averaging two directions as angles gives 000 for 350 and 010 wrongly as 180, and
    a current is a vector anyway: the blend of a 1 kt flood and a 1 kt ebb is slack,
    not a 1 kt current pointing sideways.
    """
    (sa, da, qa), (sb, db, qb) = a, b
    ax, ay = da * math.sin(math.radians(sa)), da * math.cos(math.radians(sa))
    bx, by = db * math.sin(math.radians(sb)), db * math.cos(math.radians(sb))
    x, y = ax * w + bx * (1 - w), ay * w + by * (1 - w)
    drift = math.hypot(x, y)
    s = math.degrees(math.atan2(x, y)) % 360.0 if drift > 0 else sa
    quality = 'projected' if 'projected' in (qa, qb) else 'measured'
    return s, drift, quality
