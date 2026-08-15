"""charts.py — NOAA ENC raster background: download, localize, serve.

PORTED FROM the ASV console's asv_console.py (the tile section, verified live
2026-07-20). Same NOAA MaritimeChartService export endpoint, same Web-Mercator
tile scheme, same on-disk layout charts/{z}/{x}/{y}.png — which is the point:

  READ-THROUGH TO THE CONSOLE'S CACHE. The ASV console has already localized ~1.3 GB
  of chart tiles for this coast. Re-downloading them into a second directory would
  cost hours and a gigabyte to end up with identical bytes. So this module writes to
  its OWN cache but READS from a chain: local cache first, then any fallback roots
  (the console's charts/), then NOAA. The fallback is strictly read-only — this tool
  never writes into another app's data, because a second writer in that directory is
  exactly the kind of thing that corrupts a chart set the operator depends on.

OFFLINE IS THE NORMAL CASE, NOT THE ERROR CASE. A miss with no network returns None
and the UI draws its own graticule instead. The circuit breaker stops us re-trying a
dead endpoint 400 times while panning — one failure parks NOAA for 60 s.
"""

import math
import os
import threading
import time
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = os.path.join(APP_DIR, 'charts')

# The ASV console's localized cache. Read-only fallback; absent is fine.
FALLBACK_ROOTS = []   # add a read-only tile cache to chain from, if you have one

ENC_EXPORT = ('https://gis.charttools.noaa.gov/arcgis/rest/services/MCS/'
              'NOAAChartDisplay/MapServer/exts/MaritimeChartService/MapServer/export')
_HALF_3857 = math.pi * 6378137.0
USER_AGENT = 'Transit-Calculator/1.0'

_noaa_down_until = 0.0
_breaker_lock = threading.Lock()


def enc_tile_url(z, x, y):
    """NOAA export URL for one 256px Web-Mercator tile."""
    size = 2 * _HALF_3857
    n = 2 ** z
    x0 = -_HALF_3857 + x * size / n
    y1 = _HALF_3857 - y * size / n
    return (ENC_EXPORT + '?bbox=%f,%f,%f,%f' % (x0, y1 - size / n, x0 + size / n, y1) +
            '&bboxSR=3857&imageSR=3857&size=256,256&format=png&transparent=true&f=image')


def tile_cache_path(z, x, y, root=None):
    return os.path.join(root or CHART_DIR, str(z), str(x), '%d.png' % y)


def cached_tile(z, x, y):
    """Tile bytes from the local cache, then the read-only fallbacks. None if absent."""
    for root in [CHART_DIR] + FALLBACK_ROOTS:
        try:
            with open(tile_cache_path(z, x, y, root), 'rb') as f:
                return f.read()
        except OSError:
            continue
    return None


def fetch_tile(z, x, y, timeout=12.0, allow_network=True):
    """Tile PNG bytes: cache, then fallbacks, then NOAA (caching the result).

    Returns None when the tile is unavailable — offline, out of chart coverage, or
    NOAA parked by the breaker. The caller serves a 204 and the client draws a plain
    sea background there, which is the honest picture: no chart data, not black.
    """
    global _noaa_down_until
    hit = cached_tile(z, x, y)
    if hit is not None:
        return hit
    if not allow_network:
        return None
    with _breaker_lock:
        if time.monotonic() < _noaa_down_until:
            return None
    try:
        req = urllib.request.Request(enc_tile_url(z, x, y), headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        # NOAA answers an out-of-coverage or errored request with a JSON body and a
        # 200. Only a real PNG gets cached, or the cache fills with error documents
        # that then serve forever as "chart".
        if not data.startswith(b'\x89PNG'):
            return None
        path = tile_cache_path(z, x, y)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write-then-rename: a half-written tile that a concurrent reader picks up
        # is a corrupt chart on screen, and .part carries the thread id so two
        # workers racing the same tile cannot truncate each other.
        tmp = path + '.part.%d' % threading.get_ident()
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
        return data
    except (OSError, ValueError):
        with _breaker_lock:
            _noaa_down_until = time.monotonic() + 60.0
        return None


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    s = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)
    return x, y


def tiles_for_bbox(bbox, zmin, zmax, max_tiles=20000):
    """Every (z,x,y) covering a (lat0, lon0, lat1, lon1) box, coarse zoom first.

    Coarse-first matters for a prefetch that gets interrupted: you end up with a
    complete low-zoom picture and a partial high-zoom one, rather than a perfect
    postage stamp and nothing around it.
    """
    lat0, lon0, lat1, lon1 = bbox
    lat0, lat1 = min(lat0, lat1), max(lat0, lat1)
    lon0, lon1 = min(lon0, lon1), max(lon0, lon1)
    out = []
    for z in range(zmin, zmax + 1):
        n = 2 ** z
        x0, y0 = tile_xy(lat1, lon0, z)
        x1, y1 = tile_xy(lat0, lon1, z)
        for x in range(max(0, min(x0, x1)), min(n - 1, max(x0, x1)) + 1):
            for y in range(max(0, min(y0, y1)), min(n - 1, max(y0, y1)) + 1):
                out.append((z, x, y))
                if len(out) >= max_tiles:
                    return out
    return out


def prefetch(bbox, zmin=8, zmax=13, max_tiles=8000, progress=None, should_stop=None):
    """Localize every tile over a bbox so the transit can be planned offline.

    Returns {'fetched','cached','failed','total','stopped'}. `should_stop` is polled
    between tiles so a long prefetch can be cancelled from the UI without killing
    the server; a cancelled run keeps everything it already wrote.
    """
    jobs = tiles_for_bbox(bbox, zmin, zmax, max_tiles=max_tiles)
    stat = {'fetched': 0, 'cached': 0, 'failed': 0, 'total': len(jobs), 'stopped': False}
    for i, (z, x, y) in enumerate(jobs):
        if should_stop is not None and should_stop():
            stat['stopped'] = True
            break
        if cached_tile(z, x, y) is not None:
            stat['cached'] += 1
        elif fetch_tile(z, x, y, timeout=20.0) is None:
            stat['failed'] += 1
        else:
            stat['fetched'] += 1
        if progress and (i % 25 == 0 or i == len(jobs) - 1):
            progress(dict(stat, done=i + 1))
    return stat


def cache_summary():
    """What is already localized, per zoom — so the UI can say whether the area is
    good to go offline before anyone casts off."""
    out = {}
    for label, root in [('local', CHART_DIR)] + [('fallback', r) for r in FALLBACK_ROOTS]:
        if not os.path.isdir(root):
            continue
        for zdir in os.listdir(root):
            if not zdir.isdigit():
                continue
            n = 0
            zp = os.path.join(root, zdir)
            try:
                for xdir in os.listdir(zp):
                    xp = os.path.join(zp, xdir)
                    if os.path.isdir(xp):
                        n += sum(1 for f in os.listdir(xp) if f.endswith('.png'))
            except OSError:
                continue
            key = int(zdir)
            e = out.setdefault(key, {'zoom': key, 'local': 0, 'fallback': 0})
            e[label] += n
    return sorted(out.values(), key=lambda d: d['zoom'])


def bbox_coverage(bbox, z):
    """Fraction of tiles at zoom z over a bbox that are already on disk."""
    jobs = tiles_for_bbox(bbox, z, z, max_tiles=100000)
    if not jobs:
        return {'zoom': z, 'have': 0, 'total': 0, 'fraction': 1.0}
    have = sum(1 for (zz, x, y) in jobs if cached_tile(zz, x, y) is not None)
    return {'zoom': z, 'have': have, 'total': len(jobs), 'fraction': have / len(jobs)}
