"""server.py — the Transit Calculator's local HTTP server.

A THIN SEAM, ON PURPOSE. The browser owns the map, the drawing and the table; this
process exists for the three things a page cannot do for itself:

  1. CHART TILES. NOAA's endpoint sends no CORS header, so a browser cannot fetch it
     directly. We proxy and cache — which is also what makes the chart work offline.
  2. CURRENTS. The DBOFS cycle is a 33 MB binary over OPeNDAP; decoding it belongs
     server-side, next to the cache.
  3. FILES. Reading a shapefile off disk and writing one back out.

Everything is stdlib. It binds LOOPBACK ONLY — this is a calculator on someone's
laptop, not a service, and a tool that fetches remote data has no business listening
on a LAN interface it was never hardened for.
"""

import datetime as dt
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import socketserver
import struct
import sys
import threading
import traceback
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import charts
import currents as currents_mod
import drift
import exporters
import fuel
import geo
import marine as marine_mod
import ofs as ofs_mod
import shapefile_io as sio
import transit
import weather as weather_mod

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, 'static')
# Saved missions live UNDER docs/, in their own folder: they are working
# outputs that accumulate, and mixing them with the generated manuals would
# bury the manuals within a week.
MISSIONS_DIR = os.path.join(APP_DIR, 'docs', 'missions')
HOST, PORT = '127.0.0.1', 8078

MODEL = fuel.FuelModel()
_cycle_cache = {}
_cycle_lock = threading.Lock()
_jobs = {}
_job_lock = threading.Lock()
TILE_RE = re.compile(r'^/tiles/(\d+)/(\d+)/(\d+)\.png$')


# --------------------------------------------------------------------------- #
#  Currents                                                                    #
# --------------------------------------------------------------------------- #
def cached_tags():
    """Cycle tags on disk, newest first."""
    try:
        metas = sorted(currents_mod.CACHE.glob('*_meta.json'))
    except OSError:
        return []
    return sorted((p.name[:-len('_meta.json')] for p in metas), reverse=True)


def load_cycle(tag=None):
    """A Currents object, memoised — each is ~33 MB decoded, so loading one per
    request would thrash a laptop for no benefit."""
    tags = cached_tags()
    if not tags:
        return None
    tag = tag or tags[0]
    with _cycle_lock:
        if tag in _cycle_cache:
            return _cycle_cache[tag]
    try:
        c = currents_mod.Currents(tag=tag)
    except Exception:
        return None
    with _cycle_lock:
        _cycle_cache[tag] = c
    return c


def newest_per_model():
    """The newest cached cycle for each OFS model: {ofs: tag}.

    Tags are `<ofs>_<date>_t<cycle>z`, and `cached_tags()` is reverse-sorted, so the
    first tag seen for a model is its newest.
    """
    out = {}
    for t in cached_tags():
        model = t.split('_', 1)[0]
        out.setdefault(model, t)
    return out


def load_multi(tags=None):
    """A MultiCurrents over the newest cycle of every cached model.

    THIS IS WHAT CLOSES A DOMAIN GAP. One model is one region with a hard edge; a
    line that crosses it needs the next model along. Ranks come from the OFS
    registry so the finer regional model wins where they overlap.
    """
    picked = tags if tags else list(newest_per_model().values())
    ranks = {r['ofs']: r['rank'] for r in ofs_mod.REGISTRY}
    cycles = []
    for t in picked:
        c = load_cycle(t)
        if c is None:
            continue
        model = t.split('_', 1)[0]
        cycles.append((ranks.get(model, 50), model, c, None))
    if not cycles:
        return None
    return ofs_mod.MultiCurrents(cycles)


def cycle_info(c):
    if c is None:
        return None
    if isinstance(c, ofs_mod.MultiCurrents):
        return {'tag': c.tag, 'multi': True, 'sources': c.sources(),
                'members': [cycle_info(m[2]) for m in c.cycles]}
    return {'tag': c.tag, 'start_utc': c.start.isoformat().replace('+00:00', 'Z'),
            'end_utc': c.end.isoformat().replace('+00:00', 'Z'),
            'lat0': c.lat0, 'lon0': c.lon0,
            'lat1': c.lat0 + (c.ny - 1) * c.dlat, 'lon1': c.lon0 + (c.nx - 1) * c.dlon,
            'ny': c.ny, 'nx': c.nx,
            'ofs': c.meta.get('ofs'), 'cycle': c.meta.get('cycle'),
            'date': c.meta.get('date'), 'fetched_utc': c.meta.get('fetched_utc')}


def coverage_report(points, c):
    """How much of a line the cycle's grid actually reaches.

    Reported BEFORE anything is computed, because the honest answer to "what is the
    current on this transit" may be "for 87% of it", and the operator should learn
    that from the panel rather than infer it from a suspiciously round zero.
    """
    if c is None or not points:
        return {'available': False, 'covered_fraction': 0.0, 'note': 'no cycle cached'}
    multi = isinstance(c, ofs_mod.MultiCurrents)
    grid = None
    if not multi:
        grid = {'lat0': c.lat0, 'lat1': c.lat0 + (c.ny - 1) * c.dlat,
                'lon0': c.lon0, 'lon1': c.lon0 + (c.nx - 1) * c.dlon}

    # ASKS THE SAME QUESTION THE CALCULATION ASKS. Two earlier versions of this
    # answered a cheaper question — first "are the leg's endpoints inside the grid"
    # (73%), then "are sampled points inside the grid rectangle" (90%) — while the
    # marcher, which requires a value to actually come back, found 87%. Being inside
    # the rectangle is not the same as being resolvable: a node masked as land, or a
    # stencil straddling the grid edge, is inside the box and still yields nothing.
    # So this queries the real source at the real sample points. It costs a few
    # hundred lookups and it makes the panel and the result the same number.
    # A MultiCurrents already IS a source and already reports which model answered;
    # wrapping it in a CurrentSource would hide that. A single cycle still needs the
    # adapter.
    if multi:
        src = c
        c.tally.clear()
        when = max(m[2].start for m in c.cycles)
    else:
        src = transit.CurrentSource(c, allow_projection=False)
        when = c.start
    total = covered = 0.0
    for i in range(len(points) - 1):
        d, brg, _ = geo.inverse(*points[i], *points[i + 1])
        total += d
        if d <= 0:
            continue
        n = max(1, int(math.ceil((d / transit.M_PER_NM) / transit.DEFAULT_STEP_NM)))
        seg = d / n
        for k in range(n):
            plat, plon = geo.direct(points[i][0], points[i][1], brg, d * (k + 0.5) / n)
            if src.query(plat, plon, when)[2] is not None:
                covered += seg
    outside = [i for i, p in enumerate(points)
               if src.query(p[0], p[1], when)[2] is None]
    rep = {'available': True, 'grid': grid,
           'covered_fraction': (covered / total) if total else 1.0,
           'outside_points': outside,
           'gap_nm': max(0.0, total - covered) / transit.M_PER_NM}
    if multi:
        rep['by_model'] = dict(c.tally)
    return rep


# --------------------------------------------------------------------------- #
#  Background jobs (chart prefetch / currents fetch)                           #
# --------------------------------------------------------------------------- #
def start_job(kind, fn):
    jid = f'{kind}-{len(_jobs) + 1}-{int(dt.datetime.now().timestamp())}'
    state = {'id': jid, 'kind': kind, 'status': 'running', 'progress': {}, 'cancel': False}
    with _job_lock:
        _jobs[jid] = state

    def run():
        try:
            state['result'] = fn(state)
            state['status'] = 'cancelled' if state['cancel'] else 'done'
        except Exception as e:
            state['status'] = 'error'
            state['error'] = f'{type(e).__name__}: {e}'
            traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()
    return state


def job_view(s):
    return {k: v for k, v in s.items() if k != 'cancel'}


# --------------------------------------------------------------------------- #
#  Request handling                                                            #
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = 'TransitCalc/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if '/tiles/' not in (self.path or ''):
            sys.stderr.write('  %s\n' % (fmt % args))

    # -- plumbing ---------------------------------------------------------- #
    def _send(self, code, body=b'', ctype='application/octet-stream', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str), 'application/json')

    def _fail(self, msg, code=400):
        self._json({'error': msg}, code)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _payload(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError('body was not valid JSON')

    # -- routing ----------------------------------------------------------- #
    def do_GET(self):
        try:
            self.route_get()
        except Exception as e:
            traceback.print_exc()
            self._fail(f'{type(e).__name__}: {e}', 500)

    do_HEAD = do_GET

    def do_POST(self):
        try:
            self.route_post()
        except ValueError as e:
            self._fail(str(e), 400)
        except Exception as e:
            traceback.print_exc()
            self._fail(f'{type(e).__name__}: {e}', 500)

    def route_get(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)

        if p == '/':
            return self.serve_static('transit.html')

        m = TILE_RE.match(p)
        if m:
            z, x, y = (int(v) for v in m.groups())
            # Cache-only while a prefetch is running would be nice, but the client
            # pans faster than a prefetch fills; network-on-miss keeps the map live.
            data = charts.fetch_tile(z, x, y)
            if data is None:
                return self._send(204, b'', 'image/png')
            return self._send(200, data, 'image/png',
                              {'Cache-Control': 'public, max-age=604800'})

        if p == '/api/config':
            tags = cached_tags()
            c = load_cycle()
            return self._json({
                'configs': MODEL.configs(),
                'default_config': MODEL.default_config(),
                'model_version': MODEL.version,
                'tank_l': MODEL.tank_l,
                'reserve_fraction': MODEL.reserve_fraction,
                'capacity_options': (MODEL.data.get('capacity_options') or {}).get('options', []),
                'sea_table': MODEL.data['sea_state_premium']['table'],
                'formats': exporters.FORMATS,
                'cycles': tags,
                'cycle': cycle_info(c),
                # For the UI's acronym tooltips. Read from the registry rather than
                # hand-listed in the page, so a model added there is explained in the
                # interface without a second edit that can be forgotten.
                'ofs_names': {r['ofs']: r['name'] for r in ofs_mod.REGISTRY},
                # The published leeway classes, for the drift panel. Read from the
                # module so the page never carries a second copy of a coefficient.
                'leeway_classes': leeway_class_payload(),
                'leeway_default': drift.DEFAULT_CLASS,
                'chart_cache': charts.cache_summary(),
                'fallback_roots': [r for r in charts.FALLBACK_ROOTS if os.path.isdir(r)],
            })

        if p == '/api/charts/coverage':
            bbox = [float(q[k][0]) for k in ('lat0', 'lon0', 'lat1', 'lon1')]
            zooms = [int(z) for z in q.get('z', ['8,10,12'])[0].split(',')]
            return self._json({'bbox': bbox,
                               'levels': [charts.bbox_coverage(bbox, z) for z in zooms]})

        if p == '/api/weather':
            lat, lon = float(q['lat'][0]), float(q['lon'][0])
            return self._json(weather_mod.at(lat, lon,
                                             sea_table=MODEL.data['sea_state_premium']['table']))

        if p == '/api/jobs':
            with _job_lock:
                return self._json({'jobs': [job_view(s) for s in _jobs.values()]})

        if p.startswith('/api/job/'):
            jid = p.rsplit('/', 1)[-1]
            with _job_lock:
                s = _jobs.get(jid)
            return self._json(job_view(s)) if s else self._fail('no such job', 404)

        if p == '/api/samples':
            return self._json({'samples': find_samples()})

        if p.startswith('/static/'):
            return self.serve_static(p[len('/static/'):])
        return self._fail('not found', 404)

    def route_post(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path

        if p == '/api/summary':
            d = self._payload()
            pts = _points(d.get('points'))
            if len(pts) < 2:
                return self._json({'legs': [], 'distance_nm': 0.0, 'points': len(pts)})
            s = transit.summarise(pts)
            # The chained source, not a single cycle: the pre-flight number must
            # describe the same currents the calculation will actually use.
            src = load_multi([d['cycle']]) if d.get('cycle') else load_multi()
            s['coverage'] = coverage_report(pts, src)
            return self._json(s)

        if p == '/api/plan':
            return self._json(self.build_plan(self._payload()))

        if p == '/api/drift':
            return self._json(self.build_drift(self._payload()))

        if p == '/api/export':
            data, fn, ct = exporters.export(**self.export_args(self._payload()))
            return self._send(200, data, ct,
                              {'Content-Disposition': f'attachment; filename="{fn}"'})

        if p == '/api/save':
            # SAVE TO DISK rather than stream a download. Same bytes, same
            # timestamped name — the only difference is where it lands, so a saved
            # mission and a downloaded one are the same file.
            d = self._payload()
            data, fn, _ = exporters.export(**self.export_args(d))
            os.makedirs(MISSIONS_DIR, exist_ok=True)
            full = os.path.join(MISSIONS_DIR, fn)
            # Write-then-rename: a half-written export that something else picks up
            # is a corrupt file, and the stamp makes a collision unlikely but the
            # atomic move makes a partial read impossible.
            tmp = full + '.part'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, full)
            return self._json({'saved': fn, 'bytes': len(data),
                               'path': full,
                               'folder': os.path.relpath(MISSIONS_DIR, APP_DIR)})

        if p == '/api/load':
            return self._json(self.load_line(self._payload()))

        if p == '/api/upload':
            return self._json(load_upload(self._body(),
                                          self.headers.get('X-Filename', 'upload')))

        if p == '/api/currents/fetch':
            d = self._payload()
            pts = _points(d.get('points')) if d.get('points') else None
            bbox = d.get('bbox')
            if pts and not bbox:
                # Scope the download to the LINE, padded — the whole point of
                # "currents for the scope of the line". A quarter degree of margin
                # keeps the interpolator fed at the ends.
                # fetch_cycle takes (lat0, lon0, lat1, lon1) — the SAME order
                # line_bbox returns. An earlier version reshuffled these into
                # (lat0, lat1, lon0, lon1), asking for a box spanning latitude 37.4
                # to -75.4: empty, or garbage. It never surfaced because the cycle
                # in the cache had been fetched by another tool entirely.
                bbox = list(transit.line_bbox(pts, margin_deg=0.25))
            # AUTO-CHAIN: which models does this line actually need? One OFS is one
            # region, and a line that leaves it needs the next one. Explicit `ofs`
            # still overrides, for forcing a single model.
            if d.get('ofs'):
                wanted = [d['ofs']]
            elif pts:
                wanted = [m['ofs'] for m in ofs_mod.plan_coverage(pts)['models']]
            else:
                wanted = ['dbofs']
            if not wanted:
                return self._fail('no OFS model covers this line')

            def work(state):
                out = []
                for i, o in enumerate(wanted):
                    if state['cancel']:
                        break
                    state['progress'] = {'stage': f'{o} ({i + 1} of {len(wanted)})',
                                         'done': i, 'total': len(wanted)}
                    out.append(fetch_cycle_job(o, bbox, state))
                return {'models': wanted, 'fetched': out}
            return self._json(job_view(start_job('currents', work)))

        if p == '/api/currents/plan':
            d = self._payload()
            pts = _points(d.get('points'))
            if len(pts) < 2:
                return self._fail('need a line')
            cov = ofs_mod.plan_coverage(pts)
            have = newest_per_model()
            for m in cov['models']:
                m['cached'] = have.get(m['ofs'])
            cov['uncovered'] = [{'lat': a, 'lon': b} for a, b in cov['uncovered'][:60]]
            return self._json(cov)

        if p == '/api/charts/prefetch':
            d = self._payload()
            pts = _points(d.get('points')) if d.get('points') else None
            bbox = d.get('bbox') or (list(transit.line_bbox(pts, margin_deg=0.05)) if pts else None)
            if not bbox:
                return self._fail('need points or bbox')
            zmin, zmax = int(d.get('zmin', 8)), int(d.get('zmax', 12))
            cap = int(d.get('max_tiles', 8000))

            def work(state):
                return charts.prefetch(bbox, zmin, zmax, max_tiles=cap,
                                       progress=lambda s: state.__setitem__('progress', s),
                                       should_stop=lambda: state['cancel'])
            return self._json(job_view(start_job('charts', work)))

        if p.startswith('/api/job/') and p.endswith('/cancel'):
            jid = p.split('/')[3]
            with _job_lock:
                s = _jobs.get(jid)
            if not s:
                return self._fail('no such job', 404)
            s['cancel'] = True
            return self._json(job_view(s))

        return self._fail('not found', 404)

    # -- the emergency case ------------------------------------------------- #
    def build_drift(self, d):
        """Where an unpowered hull goes from a last known position.

        Separate from `build_plan` on purpose. A plan is a vessel holding a line;
        this is a hull that has lost the ability to hold anything, and sharing the
        request shape would invite settings that mean nothing here — speed, mode,
        config, fuel — to look as though they applied.
        """
        try:
            lat = float(d['lat'])
            lon = float(d['lon'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('a drift needs a last known position: lat and lon')
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f'position {lat}, {lon} is not on the earth')
        # `is None`, not `or`. A requested 0 is a mistake worth naming, and with `or`
        # it silently became the 24-hour default — the same falsy-zero trap that hid
        # a horizon of 0.0 in drift.predict. Both were found by trying it.
        hours = float(d['hours']) if d.get('hours') is not None else 24.0
        if hours <= 0 or hours > 336:
            raise ValueError('drift duration must be between 0 and 336 hours '
                             '(two weeks is already far past any useful forecast)')

        start = None
        if d.get('start_utc'):
            try:
                start = dt.datetime.fromisoformat(d['start_utc'].replace('Z', '+00:00'))
            except ValueError:
                raise ValueError(f"could not read the time of loss {d['start_utc']!r}")
        start = start or dt.datetime.now(dt.timezone.utc)

        cyc = load_multi([d['cycle']]) if d.get('cycle') else load_multi()
        if cyc is None or not cyc.cycles:
            raise ValueError('no current forecast is cached — press Currents first. '
                             'A drift with no current is a wind-only guess.')
        cyc.allow_projection = d.get('allow_projection', True)

        wx = d.get('weather')
        wx_source = 'manual' if wx else 'none'
        marine_meta = None
        if d.get('fetch_weather'):
            # A DEGENERATE TWO-POINT LINE, not a bare point: it reuses the gather
            # path the transit already exercises rather than adding a second,
            # less-travelled one for the emergency case to fail on.
            box = [(lat, lon), (lat + 0.02, lon + 0.02)]
            got = marine_mod.gather(box, when=start, nodes=2,
                                    buoy_radius_km=float(d.get('buoy_radius_km') or 150.0))
            wx = transit.MarineField(got['samples'],
                                     MODEL.data['sea_state_premium']['table'])
            wx_source = 'idw'
            marine_meta = {
                'notes': got['notes'],
                'samples': [{'source': s['source'], 'id': s.get('id'),
                             'name': s.get('name'), 'age_s': s.get('age_s'),
                             'fields': sorted(s['values'])} for s in got['samples']],
            }

        out = drift.predict(
            lat, lon, start, hours, cyc, weather=wx,
            leeway_class=d.get('leeway_class') or drift.DEFAULT_CLASS,
            # Zero is a real answer here too — it means "take the forecast exactly
            # as given" — so it must not fall through to the default.
            current_scale=(float(d['current_scale'])
                           if d.get('current_scale') is not None
                           else drift.CURRENT_SCALE),
            step_min=float(d.get('step_min') or drift.DEFAULT_STEP_MIN),
            report_every_h=float(d.get('report_every_h') or 1.0))
        # COMPARING CLASSES IS THE HONEST USE OF A TAXONOMY THAT HAS NO ENTRY FOR
        # THIS VESSEL. The chosen class is an analogue, so how far the answer moves
        # when you pick a different one IS a result — and a bigger one than any
        # decimal place in the datum. Run here rather than by repeated calls from
        # the browser so the forecast is loaded once for all of them.
        want = [k for k in (d.get('compare_classes') or [])
                if k in drift.LEEWAY_CLASSES]
        if len(want) > 8:
            raise ValueError('compare at most 8 leeway classes at once')
        comparison = []
        for k in dict.fromkeys(want):          # de-duplicated, order preserved
            one = out if k == (d.get('leeway_class') or drift.DEFAULT_CLASS) else \
                drift.predict(lat, lon, start, hours, cyc, weather=wx,
                              leeway_class=k,
                              current_scale=(float(d['current_scale'])
                                             if d.get('current_scale') is not None
                                             else drift.CURRENT_SCALE),
                              step_min=float(d.get('step_min')
                                             or drift.DEFAULT_STEP_MIN),
                              report_every_h=float(d.get('report_every_h') or 1.0))
            dat = one['datum'] or {}
            comparison.append({
                'key': k, 'name': drift.LEEWAY_CLASSES[k]['name'],
                'lat': dat.get('lat'), 'lon': dat.get('lon'),
                'from_start_nm': dat.get('from_start_nm'),
                'bearing_from_start': dat.get('bearing_from_start'),
                'radius_nm': dat.get('radius_nm'),
                'dw_slope_pct': drift.LEEWAY_CLASSES[k]['dw'][0],
                'dw_std_cms': drift.LEEWAY_CLASSES[k]['dw'][2],
                'stokes_nm': one.get('stokes_nm'),
                'horizon_h': one.get('horizon_h'),
                'is_primary': k == (d.get('leeway_class') or drift.DEFAULT_CLASS),
            })
        out['comparison'] = comparison
        out['weather_source'] = wx_source
        out['marine'] = marine_meta
        out['cycle'] = cycle_info(cyc)
        out['forecast_until_utc'] = (
            drift.time_horizon(cyc).isoformat().replace('+00:00', 'Z')
            if drift.time_horizon(cyc) else None)
        return out

    # -- exporting ---------------------------------------------------------- #
    def export_args(self, d):
        """Validate an export request and return `exporters.export`'s arguments.

        ONE prelude for both export routes. They used to hold a copy each, and the
        copies drifted from the calculate path: `build_plan` refuses a line with
        fewer than two points, while these two went straight to the writers, which
        indexed `points[0]` and turned a malformed request into a 500 with a
        traceback. A refusal is a 400 that says what was wrong.

        Two points is the floor for every format, not just the line ones: a
        transit is a thing that goes somewhere, and the waypoint exports still
        carry leg and cumulative distances computed from the pairs.
        """
        pts = _points(d.get('points'))
        if len(pts) < 2:
            raise ValueError('a transit needs at least two points')
        fmt = d.get('format', 'geojson')
        plan = None
        if fmt in ('csv_legs', 'shp_legs'):
            plan = self.build_plan(d)
            if 'error' in plan:
                raise ValueError(plan['error'])
        zone = d.get('utm_zone')
        if zone in ('auto', True):
            zone = geo.utm_zone_for(pts[0][0], pts[0][1])[0]
        # ONE stamp for the whole export, taken here rather than inside the
        # writers, so every file of a shapefile set agrees. A client may pass its
        # own; `stamp: false` turns it off for a caller that wants a stable name
        # to overwrite.
        stamp = d.get('stamp', True)
        if stamp is True:
            stamp = exporters.stamp_now()
        elif not stamp:
            stamp = None
        return {'fmt': fmt, 'points': pts, 'name': d.get('name') or 'transit',
                'plan': plan, 'utm_zone': int(zone) if zone else None,
                'hemisphere': d.get('hemisphere'),
                'geometry': d.get('geometry', 'line'), 'stamp': stamp}

    # -- the calculation --------------------------------------------------- #
    def build_plan(self, d):
        pts = _points(d.get('points'))
        if len(pts) < 2:
            return {'error': 'a transit needs at least two points'}
        speed = float(d.get('speed_kt') or 8.0)
        mode = d.get('mode', 'stw')
        rpm = float(d['rpm']) if d.get('rpm') else None
        config = d.get('config') or MODEL.default_config()
        capacity = float(d['capacity_l']) if d.get('capacity_l') else None
        onboard = _onboard(d, capacity if capacity is not None else (MODEL.tank_l or 0.0))
        # Chain every cached model by default; a single tag can still be forced.
        if not d.get('use_currents', True):
            cyc = None
        elif d.get('cycle'):
            cyc = load_multi([d['cycle']])
        else:
            cyc = load_multi()
        if cyc is not None:
            cyc.allow_projection = d.get('allow_projection', True)
            cyc.tally.clear()
        src = cyc if cyc is not None else transit.NullCurrents()

        departure = None
        if d.get('departure_utc'):
            try:
                departure = dt.datetime.fromisoformat(
                    d['departure_utc'].replace('Z', '+00:00'))
            except ValueError:
                return {'error': f"could not read departure {d['departure_utc']!r}"}
        if departure is None and cyc is not None:
            # With several cycles the common window is the LATEST start: a departure
            # before that would put one model on its projection path from the outset.
            departure = max(m[2].start for m in cyc.cycles)
        if False:
            # Default to the start of the cached cycle, not "now": a cycle fetched
            # yesterday cannot answer for this afternoon, and defaulting to now
            # would silently push every leg onto the projection path.
            departure = cyc.start
        departure = departure or dt.datetime.now(dt.timezone.utc)

        wx = d.get('weather')
        wx_source = 'manual'
        marine_meta = None
        if d.get('fetch_weather'):
            # IDW OVER THE WHOLE LINE, not one value per leg. Samples are gathered
            # once — buoys near the track plus grid nodes spread along it — and the
            # marcher then interpolates at every step, so a long leg crossing a wind
            # gradient is costed against the wind it actually meets.
            got = marine_mod.gather(
                pts, when=departure,
                nodes=int(d.get('marine_nodes') or 6),
                buoy_radius_km=float(d.get('buoy_radius_km') or 120.0),
                use_ndbc=d.get('use_ndbc', True),
                use_nws=d.get('use_nws', True),
                use_ww3=d.get('use_ww3', True))
            wx = transit.MarineField(got['samples'],
                                     MODEL.data['sea_state_premium']['table'])
            wx_source = 'idw'
            marine_meta = {
                'notes': got['notes'],
                'samples': [{'source': s['source'], 'id': s.get('id'),
                             'name': s.get('name'), 'lat': s['lat'], 'lon': s['lon'],
                             'age_s': s.get('age_s'),
                             'fields': sorted(s['values'])} for s in got['samples']],
            }

        plan = transit.plan(
            pts, speed, departure=departure, mode=mode, config=config,
            currents=src, weather=wx, model=MODEL,
            step_nm=float(d.get('step_nm') or transit.DEFAULT_STEP_NM),
            capacity_l=capacity,
            reserve_fraction=(float(d['reserve_fraction'])
                              if d.get('reserve_fraction') is not None else None),
            onboard_l=onboard, rpm=rpm)
        plan['weather_source'] = wx_source
        plan['marine'] = marine_meta
        plan['cycle'] = cycle_info(cyc)
        if cyc is not None:
            plan['current_by_model'] = dict(cyc.tally)
        plan['coverage'] = coverage_report(pts, cyc)
        plan['model_version'] = MODEL.version
        plan['loiter_lph'] = MODEL.loiter_lph(config)[0]
        return plan

    # -- loading ----------------------------------------------------------- #
    def load_line(self, d):
        path = d.get('path')
        if not path:
            raise ValueError('give a path')
        if not os.path.isabs(path):
            path = os.path.join(APP_DIR, path)
        if not os.path.exists(path):
            raise ValueError(f'no such file: {path}')
        return read_line_file(path)

    # -- static ------------------------------------------------------------ #
    def serve_static(self, rel):
        rel = posixpath.normpath(rel.replace('\\', '/')).lstrip('./')
        full = os.path.join(STATIC_DIR, *rel.split('/'))
        # Containment check: a normalised path can still escape via a symlink or a
        # leading drive letter on Windows, so compare the resolved paths.
        if not os.path.realpath(full).startswith(os.path.realpath(STATIC_DIR)):
            return self._fail('forbidden', 403)
        if not os.path.isfile(full):
            return self._fail('not found', 404)
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if full.endswith('.js'):
            ctype = 'text/javascript'
        with open(full, 'rb') as f:
            return self._send(200, f.read(), ctype, {'Cache-Control': 'no-cache'})


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _points(raw):
    """Accept [{lat,lon}] or [[lat,lon]] — the UI sends the first, files the second."""
    out = []
    for p in (raw or []):
        if isinstance(p, dict):
            out.append((float(p['lat']), float(p['lon'])))
        else:
            out.append((float(p[0]), float(p[1])))
    return out


def leeway_class_payload():
    """The leeway classes as the drift panel needs them.

    A FUNCTION RATHER THAN AN INLINE COMPREHENSION so the suite can call it. It was
    inline, reading a `cw` key, and when the table gained separate left and right
    rows this raised KeyError on every page load — the console was empty, the class
    dropdown was empty, and the drift still worked because the server falls back to
    the default class. It survived a round of UI checking for exactly that reason:
    the feature under test worked while the page behind it was broken.
    """
    return [{'key': k, 'name': v['name'],
             'dw_slope_pct': v['dw'][0], 'dw_offset_cms': v['dw'][1],
             'dw_std_cms': v['dw'][2],
             'cw_slope_pct': v['cw_right'][0],
             'cw_left_pct': v['cw_left'][0],
             'cw_std_cms': max(v['cw_right'][2], v['cw_left'][2])}
            for k, v in drift.LEEWAY_CLASSES.items()]


def _onboard(d, capacity_l):
    """Fuel aboard at departure, or None for a full tank.

    Refused rather than clamped when it is outside the tank. A figure above
    capacity is a mistyped gauge reading, and silently trimming it to full would
    hand back a margin the operator never had; a negative one is meaningless.
    Note `is None`, not truthiness: 0 L aboard is a legitimate — and loud — answer.
    """
    raw = d.get('onboard_l')
    if raw is None or raw == '':
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'could not read fuel aboard {raw!r}')
    if v < 0:
        raise ValueError('fuel aboard cannot be negative')
    if capacity_l and v > capacity_l + 1e-9:
        raise ValueError(f'fuel aboard {v:g} L is more than the '
                         f'{capacity_l:g} L tank holds')
    return v


def read_line_file(path):
    """Load a line from .shp / .geojson / .json / .gpx / .csv into lat/lon."""
    low = path.lower()
    if low.endswith('.shp'):
        r = sio.read_shapefile(path)
        if not r['records']:
            raise ValueError('shapefile has no non-null geometry')
        crs = r['crs']
        chosen = max(r['records'], key=lambda rec: len(rec.points))
        pts = chosen.points
        if crs.get('kind') == 'utm':
            ll = [geo.utm_to_ll(x, y, crs['zone'], crs['hemisphere']) for x, y in pts]
        elif crs.get('kind') in ('geographic', 'none'):
            # A .prj-less file is AMBIGUOUS. Values inside +-180/+-90 are almost
            # certainly degrees; anything larger is projected metres we cannot place
            # without a CRS, and guessing a zone would put the line in the wrong
            # ocean. Refuse rather than guess.
            if all(abs(x) <= 180 and abs(y) <= 90 for x, y in pts):
                ll = [(y, x) for x, y in pts]
            else:
                raise ValueError('shapefile has no .prj and its coordinates are not '
                                 'lat/lon — cannot place it without a CRS')
        else:
            raise ValueError(f'unsupported CRS in .prj: {r["prj"][:80] if r["prj"] else "?"}')
        return {'points': [{'lat': a, 'lon': b} for a, b in ll],
                'name': os.path.splitext(os.path.basename(path))[0],
                'source': path, 'crs': crs, 'null_shapes': r['null_count'],
                'features': len(r['records']), 'attrs': chosen.attrs,
                'fields': [f['name'] for f in r['fields']]}

    if low.endswith(('.geojson', '.json')):
        with open(path, 'r', encoding='utf-8') as f:
            g = json.load(f)
        pts = _geojson_line(g)
        return {'points': [{'lat': a, 'lon': b} for a, b in pts],
                'name': os.path.splitext(os.path.basename(path))[0], 'source': path}

    if low.endswith('.gpx'):
        with open(path, 'r', encoding='utf-8') as f:
            txt = f.read()
        pts = [(float(a), float(b)) for a, b in
               re.findall(r'<(?:rte|trk)pt[^>]*lat="([-\d.]+)"[^>]*lon="([-\d.]+)"', txt)]
        if not pts:
            raise ValueError('no rtept/trkpt found in GPX')
        return {'points': [{'lat': a, 'lon': b} for a, b in pts],
                'name': os.path.splitext(os.path.basename(path))[0], 'source': path}

    if low.endswith('.csv'):
        import csv as _csv
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            rows = list(_csv.DictReader(f))
        pts = []
        for row in rows:
            keys = {k.lower().strip(): k for k in row if k}
            la = next((keys[k] for k in ('latitude', 'lat', 'y') if k in keys), None)
            lo = next((keys[k] for k in ('longitude', 'lon', 'lng', 'x') if k in keys), None)
            if la and lo and row[la] and row[lo]:
                pts.append((float(row[la]), float(row[lo])))
        if not pts:
            raise ValueError('CSV needs latitude/longitude columns')
        return {'points': [{'lat': a, 'lon': b} for a, b in pts],
                'name': os.path.splitext(os.path.basename(path))[0], 'source': path}

    raise ValueError(f'unsupported file type: {os.path.basename(path)}')


def _geojson_line(g):
    def coords(geom):
        t = geom.get('type')
        if t == 'LineString':
            return [(c[1], c[0]) for c in geom['coordinates']]
        if t == 'MultiLineString':
            return [(c[1], c[0]) for part in geom['coordinates'] for c in part]
        return []
    if g.get('type') == 'FeatureCollection':
        best = []
        for f in g['features']:
            c = coords(f.get('geometry') or {})
            if len(c) > len(best):
                best = c
        if best:
            return best
        pts = [(f['geometry']['coordinates'][1], f['geometry']['coordinates'][0])
               for f in g['features'] if (f.get('geometry') or {}).get('type') == 'Point']
        if pts:
            return pts
    elif g.get('type') == 'Feature':
        return coords(g['geometry'])
    else:
        return coords(g)
    raise ValueError('no line geometry in GeoJSON')


def load_upload(raw, filename):
    """A dropped file: a zipped shapefile set, or a single text/geo file."""
    import tempfile
    if raw[:2] == b'PK':
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                z.extractall(td)
            shp = None
            for root, _, files in os.walk(td):
                for fn in files:
                    if fn.lower().endswith('.shp'):
                        shp = os.path.join(root, fn)
                        break
            if not shp:
                raise ValueError('zip contains no .shp')
            r = read_line_file(shp)
            r['source'] = filename
            return r
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, os.path.basename(filename) or 'upload.geojson')
        with open(p, 'wb') as f:
            f.write(raw)
        r = read_line_file(p)
        r['source'] = filename
        return r


def find_samples():
    """Line files sitting in the app directory — so the supplied survey line is one
    click away instead of a path to type."""
    out = []
    for root, dirs, files in os.walk(APP_DIR):
        dirs[:] = [d for d in dirs if d not in
                   ('charts', 'ofs_cache', '__pycache__', '.git', 'static', 'tests')]
        for fn in files:
            if fn.lower().endswith(('.shp', '.geojson', '.gpx')):
                full = os.path.join(root, fn)
                out.append({'path': os.path.relpath(full, APP_DIR).replace('\\', '/'),
                            'name': os.path.splitext(fn)[0],
                            'size': os.path.getsize(full)})
    return sorted(out, key=lambda d: d['path'])[:40]


def fetch_cycle_job(ofs, bbox, state):
    """Download the newest available OFS cycle, scoped to a bbox."""
    state['progress'] = {'stage': 'listing cycles'}
    avail = currents_mod.available_cycles(ofs)
    if not avail:
        raise RuntimeError(f'no {ofs} cycles listed (offline?)')
    datestr, cycle = avail[0][:2] if isinstance(avail[0], (list, tuple)) else (None, None)
    state['progress'] = {'stage': f'fetching {ofs} {datestr} {cycle}', 'bbox': bbox}
    tag = currents_mod.fetch_cycle(ofs=ofs, datestr=datestr, cycle=cycle, bbox=bbox)
    with _cycle_lock:
        _cycle_cache.pop(tag, None)
    c = load_cycle(tag)
    return {'tag': tag, 'cycle': cycle_info(c)}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # NOT allow_reuse_address ON WINDOWS. There SO_REUSEADDR does what SO_REUSEPORT
    # does elsewhere: it lets a SECOND process bind a port the first is already
    # listening on, and incoming connections are then split between them at the
    # kernel's discretion. The result is a half-working app — some requests answered
    # by a stale server, some by the new one — which is far worse to diagnose than a
    # clean "port already in use". We check the port ourselves in main() and give
    # that message instead.
    allow_reuse_address = (os.name != 'nt')


def port_in_use(host, port):
    """True if something is already listening. Checked BEFORE binding because on
    Windows the bind would otherwise succeed and produce two servers on one port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def open_browser(url, delay=0.6):
    """Open the default browser, AFTER the socket is listening.

    The launcher used to open the browser first and start the server afterwards,
    which is a race the browser always wins: it loads, gets connection-refused, and
    the operator sees a failure page for a server that comes up a moment later. The
    open is deferred to a timer started once serve_forever() is about to run, so the
    listening socket already exists when the browser connects.
    """
    def go():
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            print(f'  could not open a browser ({type(e).__name__}) - open {url} yourself')
    t = threading.Timer(delay, go)
    t.daemon = True
    t.start()


def main():
    port = PORT
    want_browser = True
    for i, a in enumerate(sys.argv):
        if a in ('-p', '--port') and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a in ('--no-browser', '--no-open'):
            want_browser = False
    os.makedirs(charts.CHART_DIR, exist_ok=True)

    if port_in_use(HOST, port):
        print(f'  something is already listening on {HOST}:{port}.')
        print('  If that is another Transit Calculator, just open '
              f'http://{HOST}:{port}/ - it is already running.')
        print(f'  Otherwise start this one elsewhere:  python server.py --port {port + 1}')
        return 1

    try:
        srv = Server((HOST, port), Handler)
    except OSError as e:
        print(f'  could not bind {HOST}:{port} - {e}')
        print(f'  try:  python server.py --port {port + 1}')
        return 1

    url = f'http://{HOST}:{port}/'
    tags = cached_tags()
    print('  Transit Calculator')
    print(f'  {url}')
    print(f'  model {MODEL.version} | config {MODEL.default_config()} | tank {MODEL.tank_l} L')
    print(f'  currents cached: {", ".join(sorted(newest_per_model().values())) or "none - use Currents"}')
    roots = [r for r in charts.FALLBACK_ROOTS if os.path.isdir(r)]
    print(f'  chart fallback: {roots[0] if roots else "none"}')
    print('  Ctrl-C to stop')
    if want_browser:
        open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n  stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
