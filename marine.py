"""marine.py — wind and wave over the WHOLE line, from whatever covers it best.

THREE SOURCES, BECAUSE NO ONE SOURCE DOES THE JOB.

  NDBC buoys      real measurements, but scattered and PARTIAL. This is not a
                  theoretical caveat: on the supplied Lewes line, 44009 reports wind
                  with its wave fields blank, and 44084 reports waves with its wind
                  fields blank. Neither buoy alone answers the question.
  NWS gridpoints  api.weather.gov, ~2.5 km, wind + gust + wave height, and it works
                  offshore (verified 22 NM out). Forecast, US waters only.
  WAVEWATCH III   global 0.5 deg wave model via ERDDAP. Coarse — ~55 km — so it is a
                  BACKSTOP that guarantees an answer anywhere, not a precision source.

THEREFORE THE INTERPOLATION IS PER-VARIABLE, NOT PER-STATION. Each field is built
from whichever samples actually carry it. A station that reports wind and not waves
contributes to the wind field and is simply absent from the wave field. Treating a
station as a unit — or worse, reading a blank as zero — is how a calm-looking
forecast gets manufactured out of a broken sensor.

IDW, AND WHAT THE EXPONENT MEANS. Inverse distance weighting with w = q / (d^p + e).
`p` (default 2) sets how fast influence falls off; `q` is a source-quality factor so a
real buoy measurement outweighs a coarse model cell at equal distance. This is an
interpolator, not a model: it cannot invent structure between samples, and a value
50 km from anything is a smooth guess. Every interpolated value therefore carries the
sources that made it, their distances, and the age of the oldest observation, so a
reader can see how much to trust it.

DIRECTIONS ARE BLENDED AS VECTORS. The mean of 350 and 010 is 000, not 180. Wind
direction is averaged as a unit vector; wave direction is weighted by wave height,
because the direction of a 0.1 m ripple should not pull the mean away from a 2 m
swell.
"""

import datetime as dt
import json
import math
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import geo

UA = {'User-Agent': 'Transit-Calculator/1.0 (+https://github.com/AndyMcLeod/Transit-Calculator-Public)'}
MS_TO_KT = 1.9438444924406046
KMH_TO_KT = 0.5399568034557235

NDBC_STATIONS = 'https://www.ndbc.noaa.gov/activestations.xml'
NDBC_OBS = 'https://www.ndbc.noaa.gov/data/realtime2/%s.txt'
NWS_POINTS = 'https://api.weather.gov/points/%.4f,%.4f'
# The CoastWatch id redirects here; hitting it directly avoids a redirect whose cert
# chain Windows cannot always verify (see _ssl_context).
WW3 = 'https://pae-paha.pacioos.hawaii.edu/erddap/griddap/ww3_global.json'

# Source quality, used as the IDW numerator. An observation beats a fine forecast
# beats a coarse one; the ratios are judgement, not measurement, and are stated here
# rather than buried so they can be argued with.
QUALITY = {'ndbc': 3.0, 'nws': 1.5, 'ww3': 1.0}

_ctx = None
_stations = None
_lock = threading.Lock()
_cache = {}
_CACHE_TTL = 1800.0


def _ssl_context():
    """A verified TLS context, using certifi when it is installed.

    NOT a convenience. The ERDDAP host serving WAVEWATCH III presents a chain the
    Windows store cannot always complete, so the stdlib default fails there while
    succeeding everywhere else. certifi is an OPTIONAL dependency: without it this
    still runs and every other source works, and WW3 simply reports itself
    unavailable. Verification is never disabled — an unverified fetch of forecast
    data is not worth the habit it builds.
    """
    global _ctx
    if _ctx is None:
        try:
            import certifi
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
    return _ctx


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read()


def _cached(key, ttl, fn):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    with _lock:
        _cache[key] = (now, val)
    return val


# --------------------------------------------------------------------------- #
#  NDBC — observations                                                         #
# --------------------------------------------------------------------------- #
def stations():
    """Active NDBC stations: [{id, lat, lon, name, type, met}]."""
    global _stations
    with _lock:
        if _stations is not None:
            return _stations

    def load():
        xml = _get(NDBC_STATIONS, timeout=60).decode('utf-8', 'replace')
        out = []
        for tag in re.findall(r'<station\b[^>]*/>', xml):
            d = dict(re.findall(r'(\w+)="([^"]*)"', tag))
            try:
                out.append({'id': d['id'], 'lat': float(d['lat']), 'lon': float(d['lon']),
                            'name': d.get('name', ''), 'type': d.get('type', ''),
                            'met': d.get('met', 'n')})
            except (KeyError, ValueError):
                continue
        return out
    got = _cached('ndbc:stations', 6 * 3600, load) or []
    with _lock:
        _stations = got
    return got


# realtime2 column -> (our name, converter). Columns not listed are ignored.
_NDBC_COLS = {
    'WDIR': ('wind_from_deg', lambda v: v),
    'WSPD': ('wind_kt', lambda v: v * MS_TO_KT),
    'GST': ('gust_kt', lambda v: v * MS_TO_KT),
    'WVHT': ('wave_m', lambda v: v),
    'DPD': ('wave_period_s', lambda v: v),
    'APD': ('wave_avg_period_s', lambda v: v),
    'MWD': ('wave_from_deg', lambda v: v),
}


def buoy_observation(station_id):
    """Latest standard-met observation for a station, or None.

    'MM' IS MISSING, NOT ZERO. Each field is emitted only if that column carries a
    real number in the most recent row that has one. A buoy whose anemometer is down
    still contributes its wave height and must not contribute a 0 kt wind.
    """
    def load():
        txt = _get(NDBC_OBS % station_id, timeout=40).decode('utf-8', 'replace')
        lines = [l for l in txt.splitlines() if l.strip()]
        if len(lines) < 3:
            return None
        head = lines[0].lstrip('#').split()
        rows = [l.split() for l in lines[2:] if not l.startswith('#')]
        if not rows:
            return None
        vals, stamp = {}, None
        # Walk newest-first and take the first real value PER FIELD: a buoy often
        # reports wind every 10 min and waves every 30, so the newest row alone has
        # holes that older rows can fill.
        for row in rows[:12]:
            if len(row) < len(head):
                continue
            rec = dict(zip(head, row))
            if stamp is None:
                try:
                    stamp = dt.datetime(int(rec['YY']), int(rec['MM']), int(rec['DD']),
                                        int(rec['hh']), int(rec['mm']),
                                        tzinfo=dt.timezone.utc)
                except (KeyError, ValueError):
                    pass
            for col, (name, conv) in _NDBC_COLS.items():
                if name in vals:
                    continue
                raw = rec.get(col)
                if raw is None or raw == 'MM':
                    continue
                try:
                    vals[name] = conv(float(raw))
                except ValueError:
                    continue
        if not vals:
            return None
        return {'values': vals, 'time': stamp}
    return _cached('ndbc:%s' % station_id, 900, load)


def buoy_samples(points, radius_km=120.0, limit=10):
    """Observations from the buoys nearest the line, as IDW samples."""
    line = _thin(points, 12)
    out = []
    cands = []
    for s in stations():
        d = min(_km(s['lat'], s['lon'], a, b) for a, b in line)
        if d <= radius_km:
            cands.append((d, s))
    cands.sort(key=lambda t: t[0])
    now = dt.datetime.now(dt.timezone.utc)
    for d, s in cands[:limit]:
        obs = buoy_observation(s['id'])
        if not obs:
            continue
        age = (now - obs['time']).total_seconds() if obs['time'] else None
        # A day-old observation is not an observation of now. Keep it out rather
        # than let a stale reading dominate the field by being close.
        if age is not None and age > 6 * 3600:
            continue
        out.append({'lat': s['lat'], 'lon': s['lon'], 'source': 'ndbc',
                    'id': s['id'], 'name': s['name'], 'values': obs['values'],
                    'age_s': age, 'dist_km': d})
    return out


# --------------------------------------------------------------------------- #
#  NWS gridpoints — forecast, US waters                                        #
# --------------------------------------------------------------------------- #
def nws_sample(lat, lon, when=None):
    """Wind (+gust, +wave height) from api.weather.gov at a point, or None."""
    def load():
        p = json.loads(_get(NWS_POINTS % (lat, lon), timeout=40).decode())
        grid = p['properties']['forecastGridData']
        return json.loads(_get(grid, timeout=50).decode())['properties']
    g = _cached('nws:%.2f,%.2f' % (lat, lon), _CACHE_TTL, load)
    if not g:
        return None
    vals = {}
    for key, name, conv in (('windSpeed', 'wind_kt', lambda v: v * KMH_TO_KT),
                            ('windGust', 'gust_kt', lambda v: v * KMH_TO_KT),
                            ('windDirection', 'wind_from_deg', lambda v: v),
                            ('waveHeight', 'wave_m', lambda v: v),
                            ('wavePeriod', 'wave_period_s', lambda v: v),
                            ('waveDirection', 'wave_from_deg', lambda v: v)):
        v = _nws_at(g.get(key), when)
        if v is not None:
            vals[name] = conv(v)
    if not vals:
        return None
    return {'lat': lat, 'lon': lon, 'source': 'nws', 'id': 'nws', 'values': vals,
            'age_s': None, 'dist_km': 0.0}


def _nws_at(block, when):
    """Value from an NWS time-series for `when`. Each entry is an ISO interval
    'start/PTnH', so the right entry is the one whose window contains the time."""
    if not block or not block.get('values'):
        return None
    vals = block['values']
    if when is None:
        return vals[0].get('value')
    for entry in vals:
        try:
            start_s, dur = entry['validTime'].split('/')
            start = dt.datetime.fromisoformat(start_s.replace('Z', '+00:00'))
            hours = _iso_hours(dur)
            if start <= when < start + dt.timedelta(hours=hours):
                return entry.get('value')
        except (ValueError, KeyError):
            continue
    # Past the end of the forecast: hold the last value rather than fail. The caller
    # sees the sample's source and can weigh it; a missing wind would silently become
    # a benign passage.
    return vals[-1].get('value')


def _iso_hours(dur):
    m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?', dur or '')
    if not m:
        return 1.0
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60.0 or 1.0


# --------------------------------------------------------------------------- #
#  WAVEWATCH III — global backstop                                             #
# --------------------------------------------------------------------------- #
def ww3_sample(lat, lon, when=None):
    """Significant wave height / peak period / peak direction from WW3.

    0.5 degree grid: the returned node can be up to ~28 km from the request, and the
    response says which node it actually came from, so the IDW weights it by its TRUE
    position rather than the one we asked for.
    """
    when = when or dt.datetime.now(dt.timezone.utc)
    t = when.replace(minute=0, second=0, microsecond=0)
    stamp = t.strftime('%Y-%m-%dT%H:00:00Z')
    L = lon % 360

    def load():
        parts = ','.join(
            f'{v}%5B({stamp})%5D%5B(0.0)%5D%5B({lat:.4f})%5D%5B({L:.4f})%5D'
            for v in ('Thgt', 'Tper', 'Tdir'))
        j = json.loads(_get(f'{WW3}?{parts}', timeout=60).decode())
        cols = j['table']['columnNames']
        row = j['table']['rows'][0]
        return dict(zip(cols, row))
    d = _cached('ww3:%.2f,%.2f,%s' % (lat, lon, stamp), _CACHE_TTL, load)
    if not d:
        return None
    vals = {}
    if d.get('Thgt') is not None:
        vals['wave_m'] = float(d['Thgt'])
    if d.get('Tper') is not None:
        vals['wave_period_s'] = float(d['Tper'])
    if d.get('Tdir') is not None:
        vals['wave_from_deg'] = float(d['Tdir'])
    if not vals:
        return None
    glat = float(d.get('latitude', lat))
    glon = float(d.get('longitude', L))
    if glon > 180:
        glon -= 360
    return {'lat': glat, 'lon': glon, 'source': 'ww3', 'id': 'ww3',
            'values': vals, 'age_s': None, 'dist_km': _km(glat, glon, lat, lon)}


# --------------------------------------------------------------------------- #
#  Building the sample net                                                     #
# --------------------------------------------------------------------------- #
def gather(points, when=None, nodes=6, buoy_radius_km=120.0,
           use_ndbc=True, use_nws=True, use_ww3=True, progress=None):
    """Every sample available over the line's full range.

    Grid sources are queried at `nodes` positions spread along the LINE — not over
    its bounding box. A transit is one-dimensional; sampling the box wastes fetches
    on water the vessel never sees and leaves the ends under-sampled.
    """
    when = when or dt.datetime.now(dt.timezone.utc)
    samples = []
    notes = []
    if use_ndbc:
        try:
            got = buoy_samples(points, radius_km=buoy_radius_km)
            samples += got
            notes.append(f'{len(got)} NDBC station(s) within {buoy_radius_km:g} km')
        except Exception as e:
            notes.append(f'NDBC unavailable: {type(e).__name__}')
    net = _thin(points, max(2, nodes))
    for i, (lat, lon) in enumerate(net):
        if progress:
            progress({'stage': 'marine', 'done': i, 'total': len(net)})
        if use_nws:
            s = nws_sample(lat, lon, when)
            if s:
                s['lat'], s['lon'] = lat, lon
                samples.append(s)
        if use_ww3:
            s = ww3_sample(lat, lon, when)
            # WW3 is a 0.5 degree grid, so two sample positions 30 km apart snap to
            # the SAME node and would otherwise be added twice — double-weighting
            # one cell purely because the line happened to pass through it twice.
            # Key on the node the model actually returned, not on what we asked for.
            if s and not any(o['source'] == 'ww3'
                             and abs(o['lat'] - s['lat']) < 1e-6
                             and abs(o['lon'] - s['lon']) < 1e-6 for o in samples):
                samples.append(s)
    for src in ('nws', 'ww3'):
        n = sum(1 for s in samples if s['source'] == src)
        notes.append(f'{n} {src.upper()} node(s)')
    return {'samples': samples, 'when': when, 'notes': notes}


def _thin(points, n):
    """`n` positions spread evenly by DISTANCE along the line, ends included.

    By distance, not by vertex: the supplied line has a 1.2 NM leg and a 42.6 NM
    leg, so one sample per vertex would put five of seven samples in the first six
    miles and leave the whole offshore run to a single node.
    """
    if n <= 1 or len(points) < 2:
        return [tuple(points[0])]
    segs = []
    total = 0.0
    for i in range(len(points) - 1):
        d, brg, _ = geo.inverse(*points[i], *points[i + 1])
        segs.append((points[i], brg, d, total))
        total += d
    if total <= 0:
        return [tuple(points[0])]
    out = []
    for k in range(n):
        want = total * k / (n - 1)
        for start, brg, d, base in segs:
            if want <= base + d or (start, brg, d, base) is segs[-1]:
                out.append(geo.direct(start[0], start[1], brg, min(d, want - base)))
                break
    return out


def _km(la1, lo1, la2, lo2):
    return geo.inverse(la1, lo1, la2, lo2)[0] / 1000.0


# --------------------------------------------------------------------------- #
#  IDW                                                                         #
# --------------------------------------------------------------------------- #
SCALAR_FIELDS = ('wind_kt', 'gust_kt', 'wave_m', 'wave_period_s', 'wave_avg_period_s')
DIR_FIELDS = {'wind_from_deg': 'wind_kt', 'wave_from_deg': 'wave_m'}


def idw(samples, lat, lon, power=2.0, eps_km=0.5, max_km=400.0):
    """Interpolate every field at one position. -> {field: value, '_meta': {...}}

    Per-field: each field draws only on the samples that actually carry it.
    """
    out = {}
    meta = {'contributors': {}, 'nearest_km': None, 'max_age_s': None}
    if not samples:
        return {'_meta': meta}

    prepared = []
    for s in samples:
        d = _km(lat, lon, s['lat'], s['lon'])
        if d > max_km:
            continue
        w = QUALITY.get(s['source'], 1.0) / ((d ** power) + eps_km ** power)
        prepared.append((s, d, w))
    if not prepared:
        return {'_meta': meta}
    meta['nearest_km'] = round(min(d for _, d, _ in prepared), 2)

    for field in SCALAR_FIELDS:
        num = den = 0.0
        used = []
        for s, d, w in prepared:
            v = s['values'].get(field)
            if v is None:
                continue
            num += w * v
            den += w
            used.append((s, d, w))
        if den > 0:
            out[field] = num / den
            _note(meta, field, used)

    for field, weight_by in DIR_FIELDS.items():
        x = y = den = 0.0
        used = []
        for s, d, w in prepared:
            v = s['values'].get(field)
            if v is None:
                continue
            # Weight a direction by its own magnitude as well as by distance: the
            # heading of a 0.1 m ripple should not drag the mean off a 2 m swell.
            mag = s['values'].get(weight_by)
            ww = w * (mag if mag and mag > 0 else 1.0)
            r = math.radians(v)
            x += ww * math.sin(r)
            y += ww * math.cos(r)
            den += ww
            used.append((s, d, w))
        if den > 0 and (x or y):
            out[field] = math.degrees(math.atan2(x, y)) % 360.0
            _note(meta, field, used)

    ages = [s['age_s'] for s, _, _ in prepared if s.get('age_s') is not None]
    meta['max_age_s'] = max(ages) if ages else None
    out['_meta'] = meta
    return out


def _note(meta, field, used):
    used.sort(key=lambda t: t[1])
    meta['contributors'][field] = [
        {'source': s['source'], 'id': s.get('id'), 'km': round(d, 1)} for s, d, _ in used[:4]]


def hs_to_wmo(hs_m, table=None):
    """Significant wave height -> WMO sea state, using the fuel model's own bands."""
    if hs_m is None:
        return None
    edges = [(0.0, 0), (0.1, 1), (0.5, 2), (1.25, 3), (2.5, 4), (4.0, 5), (6.0, 6)]
    if table:
        parsed = []
        for row in table:
            spec = str(row.get('hs_m', '')).replace('m', '').strip()
            hi = spec.split('-')[-1].strip() if '-' in spec else spec
            try:
                parsed.append((float(hi), int(row['wmo'])))
            except (ValueError, TypeError, KeyError):
                continue
        if parsed:
            edges = sorted(parsed)
    for hi, c in edges:
        if hs_m <= hi:
            return c
    return edges[-1][1]


def field_at(samples, lat, lon, sea_table=None, **kw):
    """The transit-facing view of one interpolated point."""
    v = idw(samples, lat, lon, **kw)
    hs = v.get('wave_m')
    return {
        'wind_speed_kt': v.get('wind_kt'),
        'wind_from_deg': v.get('wind_from_deg'),
        'gust_kt': v.get('gust_kt'),
        'wave_height_m': hs,
        'wave_period_s': v.get('wave_period_s') or v.get('wave_avg_period_s'),
        'wave_from_deg': v.get('wave_from_deg'),
        'wmo_sea_state': hs_to_wmo(hs, sea_table),
        'derived': hs is not None,
        'source': 'idw',
        'meta': v.get('_meta'),
    }
