"""tests/test_transit.py — the Transit Calculator's checks.

RUN:  python tests/test_transit.py            (all suites)
      python tests/test_transit.py geodesy    (one suite)

TWO RULES THIS FILE IS WRITTEN TO, both learned the hard way:

  1. A TEST IS NOT TRUSTED UNTIL IT HAS FAILED ON PURPOSE. Every assertion here was
     checked by mutating the code under it and confirming it went red. Where a check
     is cheap to fool, the mutation is encoded as its own case (see the `_mutation`
     helpers) so it keeps proving itself instead of relying on my memory of having
     done it once.

  2. EVERY REFUSAL IS PAIRED WITH AN ACCEPTANCE. A test that only proves the code
     says no is satisfied by code that always says no. So each refusal case sits next
     to the nearest input that must still be accepted.

NETWORK: the suites here never touch the network. The currents suite needs a cached
OFS cycle and SKIPS (loudly) when there is none, rather than passing vacuously.
"""

import datetime as dt
import io
import json
import math
import os
import struct
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift                                                # noqa: E402
import exporters                                            # noqa: E402
import fuel                                                 # noqa: E402
import geo                                                  # noqa: E402
import marine                                               # noqa: E402
import ofs                                                  # noqa: E402
import shapefile_io as sio                                  # noqa: E402
import transit                                              # noqa: E402
import weather                                              # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(APP, 'survey_transit_line', 'survey_transit_line',
                      'survey_transit_line.shp')

# The supplied Lewes line, in lat/lon. Hard-coded rather than read from the file so
# the geometry suites still mean something if the sample is ever moved.
LEWES = [(38.796273, -75.155618), (38.810697, -75.097760), (38.799114, -75.075981),
         (38.763953, -75.065596), (38.469565, -74.783603), (38.357256, -74.792817),
         (37.664897, -74.997880)]

_checks = 0
_failures = []
_skips = []


def check(name, cond, detail=''):
    global _checks
    _checks += 1
    if not cond:
        _failures.append(f'{name}: {detail}' if detail else name)
        print(f'    FAIL  {name}  {detail}')
    return bool(cond)


def close(name, got, want, tol, unit=''):
    return check(name, abs(got - want) <= tol,
                 f'got {got!r}, want {want!r} +-{tol}{unit}')


def skip(name, why):
    _skips.append(f'{name}: {why}')
    print(f'    SKIP  {name} — {why}')


def dms(d, m, s, neg=False):
    v = d + m / 60 + s / 3600
    return -v if neg else v


# --------------------------------------------------------------------------- #
def suite_geodesy():
    # -- Against the published Vincenty vector (Geoscience Australia). This is the
    #    only external truth in the file; everything else is internal consistency.
    lat1, lon1 = dms(37, 57, 3.72030, True), dms(144, 25, 29.52440)
    lat2, lon2 = dms(37, 39, 10.15610, True), dms(143, 55, 35.38390)
    s, a1, a2 = geo.inverse(lat1, lon1, lat2, lon2)
    close('vincenty distance', s, 54972.271, 0.001, ' m')
    close('vincenty initial azimuth', a1, dms(306, 52, 5.37), 1e-5, ' deg')
    # The published alpha2 is the REVERSE azimuth; ours is the forward azimuth at
    # the destination. Asserting the documented relationship keeps the convention
    # pinned — if someone "fixes" inverse() to return the other one, this fails.
    rev = geo.inverse(lat2, lon2, lat1, lon1)[1]
    close('vincenty reverse azimuth', rev, dms(127, 10, 25.07), 1e-5, ' deg')
    close('final azimuth is forward', (a2 - 180) % 360, rev, 1e-6, ' deg')

    close('equator degree', geo.inverse(0, 0, 0, 1)[0], 111319.4908, 0.001, ' m')
    close('meridian quadrant', geo.inverse(0, 0, 90, 0)[0], 10001965.729, 0.01, ' m')

    # -- Degenerate input must not produce NaN. A drawn line WILL contain a
    #    double-click, and one NaN poisons every total downstream.
    d, b1, b2 = geo.inverse(38.5, -75.0, 38.5, -75.0)
    check('coincident points give zero', d == 0.0 and b1 == 0.0 and b2 == 0.0, f'{d},{b1},{b2}')
    check('coincident is not NaN', not any(math.isnan(v) for v in (d, b1, b2)))

    # -- direct/inverse are true inverses
    worst_d = worst_b = 0.0
    for lat, lon, brg, dist in [(38.8, -75.1, 193.3, 78931.3), (0.5, -1.0, 45.0, 1000.0),
                                (-33.9, 151.2, 271.0, 500000.0), (60.0, 5.0, 0.0, 250000.0)]:
        la2, lo2 = geo.direct(lat, lon, brg, dist)
        s2, b, _ = geo.inverse(lat, lon, la2, lo2)
        worst_d = max(worst_d, abs(s2 - dist))
        worst_b = max(worst_b, abs((b - brg + 180) % 360 - 180))
    check('direct/inverse round-trip distance', worst_d < 1e-6, f'{worst_d} m')
    check('direct/inverse round-trip bearing', worst_b < 1e-9, f'{worst_b} deg')

    # -- UTM round-trip on the real line, and the forced-zone contract
    worst = 0.0
    for lat, lon in LEWES:
        e, n, z, h = geo.ll_to_utm(lat, lon, 18, 'N')
        la, lo = geo.utm_to_ll(e, n, z, h)
        worst = max(worst, geo.inverse(lat, lon, la, lo)[0])
    check('UTM round-trip < 1 mm', worst < 1e-3, f'{worst * 1000:.4f} mm')
    check('zone 18 chosen for Lewes', geo.utm_zone_for(38.8, -75.1)[0] == 18)
    check('Norway exception', geo.utm_zone_for(60.0, 5.0)[0] == 32)
    check('Svalbard exception', geo.utm_zone_for(78.0, 15.0)[0] == 33)
    # Forcing a zone must actually force it — a line crossing a boundary has to stay
    # in ONE CRS or the exported shapefile is nonsense under its own .prj.
    e_native = geo.ll_to_utm(38.8, -69.0)[2]
    e_forced = geo.ll_to_utm(38.8, -69.0, 18, 'N')[2]
    check('forced zone overrides native', e_native == 19 and e_forced == 18,
          f'native {e_native}, forced {e_forced}')

    # -- The ellipsoid earns its keep: the console's flat-earth is materially
    #    different over a transit leg. If this ever falls below the threshold the
    #    simpler code would be justified, and we should know.
    d_geo = geo.inverse(38.357256, -74.792817, 37.664897, -74.997880)[0]
    d_flat = geo.flat_dist_m(38.357256, -74.792817, 37.664897, -74.997880)
    check('flat-earth diverges > 100 m on a 42 NM leg', abs(d_flat - d_geo) > 100,
          f'{d_flat - d_geo:.1f} m')

    # -- set and drift
    sog, cog, along, cross = geo.sog_from_stw(8.0, 193.3, 193.3, 1.5)
    close('following current adds', sog, 9.5, 1e-9, ' kt')
    close('following current along +', along, 1.5, 1e-9, ' kt')
    close('following current cross 0', cross, 0.0, 1e-9, ' kt')
    sog, _, along, _ = geo.sog_from_stw(8.0, 193.3, 13.3, 1.5)
    close('opposing current subtracts', sog, 6.5, 1e-9, ' kt')
    close('opposing current along -', along, -1.5, 1e-9, ' kt')
    sog, _, _, _ = geo.sog_from_stw(8.0, 0.0, 90.0, 2.0)
    close('beam current vector sum', sog, math.hypot(8, 2), 1e-9, ' kt')

    # -- REFUSAL, with its acceptance partner beside it: a beam set stronger than
    #    the boat's water speed cannot be crabbed out, and must return None rather
    #    than some plausible slow number.
    check('beam set > STW refuses', geo.stw_to_hold_track(0.0, 90.0, 9.0, stw_kt=8.0) is None)
    ok = geo.stw_to_hold_track(0.0, 90.0, 2.0, stw_kt=8.0)
    check('beam set < STW accepted', ok is not None)
    if ok:
        hdg, sog, crab = ok
        close('crab cancels the cross-set', 8.0 * math.sin(math.radians(crab)) + 2.0, 0.0,
              1e-9, ' kt')
        close('held-track SOG', sog, 8.0 * math.cos(math.radians(crab)), 1e-9, ' kt')
    # Exactly-equal is the boundary: it can be crabbed (90 deg) but makes no ground.
    check('beam set == STW makes no ground', geo.stw_to_hold_track(0.0, 90.0, 8.0, stw_kt=8.0) is None)

    # -- uv -> set uses the OCEANOGRAPHIC convention (toward). Backwards here flips
    #    every current in the tool, so it is asserted on all four quadrants.
    for u, v, want in [(1, 0, 90), (0, 1, 0), (-1, 0, 270), (0, -1, 180)]:
        close(f'uv_to_set({u},{v})', geo.uv_to_set(u, v)[0], want, 1e-9, ' deg')
    close('uv magnitude to knots', geo.uv_to_set(1.0, 0.0)[1], 1.9438444924406046, 1e-12, ' kt')

    check('hm rounds up to the hour', geo.hm(0.9958) == '1:00', geo.hm(0.9958))
    check('hm formats minutes', geo.hm(2.5) == '2:30', geo.hm(2.5))


# --------------------------------------------------------------------------- #
def suite_shapefile():
    if not os.path.exists(SAMPLE):
        return skip('shapefile', 'sample not present')
    r = sio.read_shapefile(SAMPLE)
    check('sample reads one polyline', len(r['records']) == 1, str(len(r['records'])))
    check('sample has 7 vertices', len(r['records'][0].points) == 7)
    # The QGIS leftover: a null shape must be counted and skipped, NOT rejected as
    # corruption and NOT silently swallowed.
    check('null shape counted', r['null_count'] == 1, str(r['null_count']))
    check('sample CRS is UTM 18N', r['crs'] == {'kind': 'utm', 'zone': 18,
                                                'hemisphere': 'N', 'epsg': 32618}, str(r['crs']))
    # A padded-nulls numeric is dBASE for "unset" and must read as None, not crash.
    check('padded-null id reads as None', r['records'][0].attrs.get('id') is None,
          repr(r['records'][0].attrs.get('id')))

    pts = r['records'][0].points
    with tempfile.TemporaryDirectory() as td:
        prj = sio.utm_wkt(18, 'N')
        sio.write_shapefile_files(td, 'rt', [[pts]], [('id', 'N', 10, 0)], [{'id': 1}], prj)
        back = sio.read_shapefile(os.path.join(td, 'rt.shp'))
        got = back['records'][0].points
        worst = max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(pts, got))
        check('write/read is bit-exact', worst == 0.0, f'{worst}')
        check('written CRS re-reads', back['crs']['epsg'] == 32618)
        check('written attrs re-read', back['records'][0].attrs['id'] == 1)

    # -- The .shx is validated INDEPENDENTLY of our reader, which ignores it. A
    #    shapefile whose index disagrees with its geometry opens as an EMPTY layer
    #    in ArcGIS with no error at all, so a self-consistent round-trip proves
    #    nothing here.
    shapes = [[pts], [pts[:3]], [pts[2:5], pts[5:]]]      # ragged, to catch fixed strides
    out = sio.write_shapefile(shapes, [('id', 'N', 10, 0)], [{'id': i} for i in (1, 2, 3)])
    shp, shx, dbf = out['shp'], out['shx'], out['dbf']
    n = (len(shx) - 100) // 8
    check('shx record count', n == 3, str(n))
    ok = True
    for i in range(n):
        off_w, len_w = struct.unpack('>ii', shx[100 + i * 8: 108 + i * 8])
        rec_num, rec_len = struct.unpack('>ii', shp[off_w * 2: off_w * 2 + 8])
        ok = ok and rec_num == i + 1 and rec_len == len_w
    check('shx offsets land on their records', ok)
    off_w, len_w = struct.unpack('>ii', shx[100 + (n - 1) * 8: 108 + (n - 1) * 8])
    end = off_w * 2 + 8 + len_w * 2
    declared = struct.unpack('>i', shp[24:28])[0] * 2
    check('shp length header is right', end == declared == len(shp),
          f'{end}/{declared}/{len(shp)}')
    check('shx length header is right',
          struct.unpack('>i', shx[24:28])[0] * 2 == len(shx))
    check('shp file code', struct.unpack('>i', shp[0:4])[0] == 9994)

    n_rec, hdr, rlen = (struct.unpack('<i', dbf[4:8])[0], struct.unpack('<H', dbf[8:10])[0],
                        struct.unpack('<H', dbf[10:12])[0])
    check('dbf length is exact', len(dbf) == hdr + n_rec * rlen + 1, str(len(dbf)))
    check('dbf field terminator', dbf[hdr - 1] == 0x0D)
    check('dbf EOF marker', dbf[-1] == 0x1A)
    check('dbf numerics right-align', dbf[hdr:hdr + rlen].endswith(b'1'),
          repr(dbf[hdr:hdr + rlen]))

    # -- MUTATION, encoded: corrupt the index and confirm the validation above would
    #    actually have caught it. Without this, the shx check could be vacuous.
    bad = bytearray(shx)
    struct.pack_into('>i', bad, 100, 999)
    off_w = struct.unpack('>ii', bytes(bad[100:108]))[0]
    caught = True
    try:
        rec_num = struct.unpack('>ii', shp[off_w * 2: off_w * 2 + 8])[0]
        caught = rec_num != 1
    except struct.error:
        caught = True
    check('corrupt shx would be detected', caught)

    # -- multi-part and point geometry survive
    with tempfile.TemporaryDirectory() as td:
        sio.write_shapefile_files(td, 'mp', [[pts[:3], pts[3:]]], [('id', 'N', 10, 0)],
                                  [{'id': 1}], sio.utm_wkt(18, 'N'))
        m = sio.read_shapefile(os.path.join(td, 'mp.shp'))
        check('multipart parts preserved', len(m['records'][0].parts) == 2)
        check('multipart vertices preserved', len(m['records'][0].points) == 7)
        sio.write_shapefile_files(td, 'pt', [[[p]] for p in pts], [('id', 'N', 10, 0)],
                                  [{'id': i} for i in range(7)], sio.utm_wkt(18, 'N'),
                                  shape_type=sio.POINT)
        p = sio.read_shapefile(os.path.join(td, 'pt.shp'))
        check('point features written', len(p['records']) == 7)
        check('point coords preserved', p['records'][0].points[0] == pts[0])

    # -- .prj classification: recognise the two we handle, REFUSE to guess the rest
    check('prj utm north', sio.parse_prj(sio.utm_wkt(18, 'N'))['epsg'] == 32618)
    check('prj utm south', sio.parse_prj(sio.utm_wkt(56, 'S'))['epsg'] == 32756)
    check('prj geographic', sio.parse_prj(sio.WGS84_WKT)['kind'] == 'geographic')
    check('prj absent', sio.parse_prj(None)['kind'] == 'none')
    check('prj unknown is not guessed',
          sio.parse_prj('PROJCS["Lambert",PROJECTION["Lambert_Conformal_Conic"]]')['kind']
          == 'unknown')
    # A Transverse Mercator that is NOT a UTM zone reaches a different exit than the
    # Lambert above — it enters the TM branch and must still refuse. Mutation testing
    # found this path uncovered: breaking that exit left the whole suite green.
    check('prj transverse mercator without a zone is not guessed',
          sio.parse_prj('PROJCS["Custom TM",GEOGCS["GCS_WGS_1984"],'
                        'PROJECTION["Transverse_Mercator"],'
                        'PARAMETER["Scale_Factor",0.9996]]')['kind'] == 'unknown')

    # -- reproducible bytes: the same line exported twice must be byte-identical, or
    #    the artifact is not diffable and "did the export change" is unanswerable.
    a = sio.shapefile_zip('t', [[pts]], [('id', 'N', 10, 0)], [{'id': 1}], sio.utm_wkt(18, 'N'))
    b = sio.shapefile_zip('t', [[pts]], [('id', 'N', 10, 0)], [{'id': 1}], sio.utm_wkt(18, 'N'))
    check('zip export is reproducible', a == b)
    check('zip carries all five sidecars',
          sorted(x.split('.')[-1] for x in zipfile.ZipFile(io.BytesIO(a)).namelist())
          == ['cpg', 'dbf', 'prj', 'shp', 'shx'])


# --------------------------------------------------------------------------- #
def suite_export():
    plan = _offline_plan()

    # -- GeoJSON/KML/GPX are DEFINED as WGS84 lon/lat. Writing projected metres into
    #    them puts the line off the coast of Africa, so coordinate order and range
    #    are asserted, not assumed.
    g = json.loads(exporters.to_geojson(LEWES, 'x'))
    ls = [f for f in g['features'] if f['geometry']['type'] == 'LineString'][0]
    c0 = ls['geometry']['coordinates'][0]
    check('geojson is lon,lat order', -76 < c0[0] < -74 and 37 < c0[1] < 39, str(c0))
    check('geojson has a point per waypoint',
          sum(1 for f in g['features'] if f['geometry']['type'] == 'Point') == len(LEWES))
    close('geojson total distance', ls['properties']['distance_nm'], 77.6844, 0.001, ' NM')

    gpx = exporters.to_gpx(LEWES, 'x')
    check('gpx has a route', '<rte>' in gpx and gpx.count('<rtept') == len(LEWES))
    check('gpx has a track', '<trkseg>' in gpx and gpx.count('<trkpt') == len(LEWES))
    check('gpx lat is in range', 'lat="38.796273' in gpx)
    kml = exporters.to_kml(LEWES, 'x')
    check('kml coordinates are lon,lat', '-75.15561800,38.79627300' in kml)

    # -- XML escaping: a name with an ampersand must not produce a broken document.
    bad_name = 'Lewes & <offshore>'
    check('kml escapes the name', '&amp;' in exporters.to_kml(LEWES, bad_name)
          and '<offshore>' not in exporters.to_kml(LEWES, bad_name))
    check('gpx escapes the name', '&amp;' in exporters.to_gpx(LEWES, bad_name))

    # -- CSV
    rows = exporters.to_csv_waypoints(LEWES).strip().splitlines()
    check('waypoint csv row count', len(rows) == len(LEWES) + 1, str(len(rows)))
    legs_csv = exporters.to_csv_legs(plan).strip().splitlines()
    check('leg csv has a row per leg + total', len(legs_csv) >= len(plan['legs']) + 2)
    check('leg csv ends with a total', 'TOTAL' in legs_csv[-1])

    # -- Shapefile export, both CRS choices, verified by reading it back and
    #    reprojecting to the source coordinates.
    for zone, label in ((18, 'utm'), (None, 'geographic')):
        data, base = exporters.to_shapefile_zip(LEWES, 'x', utm_zone=zone, hemisphere='N')
        with tempfile.TemporaryDirectory() as td:
            zipfile.ZipFile(io.BytesIO(data)).extractall(td)
            r = sio.read_shapefile(os.path.join(td, base + '.shp'))
            got = r['records'][0].points
            if zone:
                ll = [geo.utm_to_ll(x, y, 18, 'N') for x, y in got]
                check(f'{label} export CRS', r['crs']['epsg'] == 32618, str(r['crs']))
            else:
                ll = [(y, x) for x, y in got]
                check(f'{label} export CRS', r['crs']['kind'] == 'geographic', str(r['crs']))
            worst = max(geo.inverse(a[0], a[1], b[0], b[1])[0] for a, b in zip(LEWES, ll))
            check(f'{label} export geometry survives', worst < 0.001, f'{worst * 1000:.4f} mm')

    # -- The per-leg export is the one that carries the calculation; every leg must
    #    appear, with its own attributes.
    data, base = exporters.legs_to_shapefile_zip(plan, 'legs', utm_zone=18, hemisphere='N')
    with tempfile.TemporaryDirectory() as td:
        zipfile.ZipFile(io.BytesIO(data)).extractall(td)
        r = sio.read_shapefile(os.path.join(td, base + '.shp'))
        check('one feature per leg', len(r['records']) == len(plan['legs']),
              f"{len(r['records'])} vs {len(plan['legs'])}")
        names = {f['name'] for f in r['fields']}
        check('leg export carries the results',
              {'sog_kt', 'drift_kt', 'litres', 'cur_cov'} <= names, str(sorted(names)))
        close('leg 1 distance survives', r['records'][0].attrs['dist_nm'],
              plan['legs'][0]['distance_nm'], 0.001, ' NM')

    # -- TIMESTAMPED SAVES. A calculated mission is saved more than once, and two
    #    saves must not collide.
    for f in exporters.FORMATS:
        _, fn, _ = exporters.export(f['key'], LEWES, 'mission', plan=plan,
                                    utm_zone=18, stamp='20260814T0130Z')
        check(f"{f['key']} filename carries the stamp", '20260814T0130Z' in fn, fn)
    _, plain, _ = exporters.export('geojson', LEWES, 'mission')
    check('no stamp means no change', plain == 'mission.geojson', plain)

    # The stamp reaches INSIDE the archive too. Stamping only the zip would leave
    # two exports both extracting to mission.shp, clobbering in the same folder —
    # the exact accident the stamp exists to prevent.
    data, _, _ = exporters.export('shp', LEWES, 'mission', utm_zone=18,
                                  stamp='20260814T0130Z')
    members = zipfile.ZipFile(io.BytesIO(data)).namelist()
    check('zip members are stamped too',
          all('20260814T0130Z' in m for m in members), str(members))

    # PASSED IN, NOT READ FROM THE CLOCK — one export's files must all agree, and a
    # fixed stamp must still give byte-identical output or nothing here is testable.
    a = exporters.export('shp', LEWES, 'mission', utm_zone=18, stamp='20260814T0130Z')[0]
    b = exporters.export('shp', LEWES, 'mission', utm_zone=18, stamp='20260814T0130Z')[0]
    check('a fixed stamp is still reproducible', a == b)
    c = exporters.export('shp', LEWES, 'mission', utm_zone=18, stamp='20260814T0200Z')[0]
    check('a different stamp gives different bytes', a != c)
    now = exporters.stamp_now()
    check('stamp_now is a sortable UTC stamp',
          len(now) == 14 and now[8] == 'T' and now.endswith('Z') and now[:8].isdigit(), now)

    # -- dispatch: every advertised format must actually produce bytes, and an
    #    unknown one must be refused rather than silently defaulting.
    for f in exporters.FORMATS:
        try:
            data, fn, ct = exporters.export(f['key'], LEWES, 'x', plan=plan, utm_zone=18)
            check(f"export {f['key']} produces bytes", len(data) > 0 and bool(fn) and bool(ct))
        except Exception as e:
            check(f"export {f['key']} produces bytes", False, f'{type(e).__name__}: {e}')
    try:
        exporters.export('dxf', LEWES, 'x')
        check('unknown format refused', False, 'no exception raised')
    except ValueError:
        check('unknown format refused', True)
    # ...and the two that need a plan must say so rather than emitting an empty file.
    for key in ('csv_legs', 'shp_legs'):
        try:
            exporters.export(key, LEWES, 'x', plan=None)
            check(f'{key} without a plan refused', False, 'no exception raised')
        except ValueError:
            check(f'{key} without a plan refused', True)


# --------------------------------------------------------------------------- #
def suite_fuel():
    M = fuel.FuelModel()
    default = M.default_config()
    # THE VESSEL ANCHORS, asserted only against a real fitted model. If a
    # model.json swap moves these, the transit numbers moved with it and that must
    # not pass silently.
    #
    # The public export substitutes a synthetic placeholder vessel (see
    # tools/make_public.py), whose invented coefficients cannot reproduce a
    # measurement. Hard-coding the anchors unconditionally would mean the scrubbed
    # tree fails its own tests — so against a placeholder the CHAIN is asserted
    # instead: still a real check that speed -> rpm -> burn holds together and lands
    # somewhere physically sane, just not a check of a number nobody measured.
    placeholder = str(M.version).startswith('placeholder')
    b = M.burn(8.0, 0.0, 1.0, default)
    eff = 8.0 / b['rate_lph']
    if placeholder:
        check('placeholder efficiency is physically sane', 1.0 < eff < 6.0,
              f'{eff:.3f} NM/L')
    else:
        # 2.36 until the 16-session refit vendored on 2026-08-15 (model v2.8.0).
        # These anchors are the point of vendoring: a re-vendor that changes the
        # fuel law has to be noticed here rather than silently re-planning.
        close('Config A efficiency at 8 kt', eff, 2.41, 0.01, ' NM/L')
    check('8 kt is inside the fit window', b['in_fit_window'])
    lph, measured, rpm = M.loiter_lph(default)
    if placeholder:
        check('placeholder loiter is a small positive burn', 0.0 < lph < 5.0, f'{lph}')
    else:
        close('Config A loiter', lph, 1.05, 1e-9, ' L/h')
        check('Config A loiter is measured', measured)

    # The heading premium at the reference wind must equal the fitted amplitude —
    # this is the one place the model's own number appears directly.
    close('headwind premium at reference', M.heading_premium(0, 0, 12.0),
          M.head_amp, 1e-12)
    close('following wind is a credit', M.heading_premium(0, 180, 12.0), -M.head_amp, 1e-12)
    close('beam wind is neutral', M.heading_premium(0, 90, 12.0), 0.0, 1e-12)
    close('premium scales with wind squared', M.heading_premium(0, 0, 24.0),
          M.head_amp * 4, 1e-12)
    close('no wind, no premium', M.heading_premium(0, 0, 0.0), 0.0, 1e-12)

    check('sea premium rises with state',
          all(M.sea_state_premium(i) <= M.sea_state_premium(i + 1) for i in range(6)))
    close('above the table holds the top', M.sea_state_premium(12),
          M.sea_state_premium(6), 1e-12)
    close('no sea state, no premium', M.sea_state_premium(None), 0.0, 1e-12)

    # Fuel is CONVEX in RPM, which is why the planner refuses to average premiums.
    # Asserting convexity keeps any future refit honest.
    r = [M.fuel_rate_lph(x, 'config_a') for x in (1600, 2000, 2400)]
    check('fuel law is convex in rpm', r[1] - r[0] < r[2] - r[1], str(r))
    check('burn never goes negative', M.fuel_rate_lph(0, 'config_a') >= 0)

    # Endurance against the 250 L tank and the 25% policy floor.
    e = M.endurance(100.0)
    close('usable litres', e['usable_l'], 187.5, 1e-9, ' L')
    check('100 L is within reserve', e['within_reserve'])
    check('200 L breaches reserve', not M.endurance(200.0)['within_reserve'])
    close('margin', M.endurance(100.0)['margin_l'], 87.5, 1e-9, ' L')

    # -- FUEL ABOARD. Sail with a part-full tank and the whole shortfall comes out
    #    of usable fuel, because the reserve floor belongs to the TANK. The obvious
    #    wrong implementation — reserve as a fraction of what is loaded — passes a
    #    check that only asserts "usable went down", so the checks here pin the
    #    ARITHMETIC: floor unchanged, and usable down by the full shortfall.
    cap, rf = M.tank_l, M.reserve_fraction
    floor = cap * rf
    part = cap * 0.56                       # a bit over half a tank
    ep = M.endurance(0.0, onboard_l=part)
    close('blank means a full tank', M.endurance(60.0, onboard_l=cap)['usable_l'],
          M.endurance(60.0)['usable_l'], 1e-9, ' L')
    check('a full tank says so', M.endurance(60.0)['full_tank'])
    check('a stated load says it is not full', not ep['full_tank'])
    close('the reserve floor is a fraction of the TANK, not of the load',
          ep['reserve_floor_l'], floor, 1e-9, ' L')
    close('usable is what is aboard, less the floor', ep['usable_l'], part - floor,
          1e-9, ' L')
    close('a part tank costs the WHOLE shortfall',
          M.endurance(0.0)['usable_l'] - ep['usable_l'], cap - part, 1e-9, ' L')
    close('remaining counts from what is aboard', M.endurance(20.0, onboard_l=part)
          ['remaining_l'], part - 20.0, 1e-9, ' L')

    # Refusal beside its acceptance: the SAME burn fits a full tank and does not fit
    # a part-full one. A version that ignored onboard_l would pass the first alone.
    burn = (part - floor) + 5.0
    check('that burn fits a full tank', M.endurance(burn)['within_reserve'])
    check('the same burn does not fit a part tank',
          not M.endurance(burn, onboard_l=part)['within_reserve'])

    # Below the floor before slipping the lines — a different fault from eating the
    # margin, and reported as one. Paired with a load just above the floor.
    check('a load under the floor is called out',
          M.endurance(0.0, onboard_l=floor * 0.5)['starts_below_reserve'])
    check('a load above the floor is not',
          not M.endurance(0.0, onboard_l=floor + 10.0)['starts_below_reserve'])
    check('an empty tank has no usable fuel',
          M.endurance(0.0, onboard_l=0.0)['usable_l'] < 0
          and M.endurance(0.0, onboard_l=0.0)['used_fraction_of_usable'] is None)

    try:
        M.burn(8.0, 0.0, 1.0, 'no_such_config')
        check('unknown config refused', False, 'no exception raised')
    except ValueError:
        check('unknown config refused', True)
    check('both configs usable',
          all(M.burn(8.0, 0, 1.0, g['key'])['litres'] > 0 for g in M.configs()))


# --------------------------------------------------------------------------- #
def suite_weather():
    M = fuel.FuelModel()
    tbl = M.data['sea_state_premium']['table']
    # The Hs -> WMO bands must come from the MODEL, so the mapping and the premium
    # cannot drift apart.
    for hs, want in [(0.0, 0), (0.05, 1), (0.3, 2), (0.9, 3), (2.0, 4), (3.5, 5), (5.0, 6)]:
        check(f'Hs {hs} m -> WMO {want}', weather.hs_to_wmo(hs, tbl) == want,
              str(weather.hs_to_wmo(hs, tbl)))
    check('above the top band holds', weather.hs_to_wmo(20.0, tbl) == 6)
    check('no wave height, no sea state', weather.hs_to_wmo(None, tbl) is None)

    # -- THE TABLE MUST ACTUALLY BE READ. The checks above cannot show that: the
    #    built-in fallback edges are identical to the model's, so a function that
    #    ignored the table entirely would still pass every one of them. Mutation
    #    testing caught exactly that. Feeding a DELIBERATELY DIFFERENT table is the
    #    only way to prove the model is consulted rather than shadowed.
    odd = [{'wmo': 0, 'hs_m': '0 - 10'}, {'wmo': 1, 'hs_m': '10 - 20'},
           {'wmo': 2, 'hs_m': '20 - 30'}]
    check('a custom table is honoured', weather.hs_to_wmo(15.0, odd) == 1,
          str(weather.hs_to_wmo(15.0, odd)))
    check('custom table changes the answer', weather.hs_to_wmo(0.3, odd) == 0
          and weather.hs_to_wmo(0.3, tbl) == 2,
          f'{weather.hs_to_wmo(0.3, odd)} vs {weather.hs_to_wmo(0.3, tbl)}')
    check('custom table top band holds', weather.hs_to_wmo(99.0, odd) == 2)
    # A failed fetch must yield None everywhere, never a calm-looking zero.
    e = weather._empty('test')
    check('failure yields nulls not zeros',
          all(e[k] is None for k in ('wind_speed_kt', 'wind_from_deg', 'wmo_sea_state')))


# --------------------------------------------------------------------------- #
def suite_transit():
    M = fuel.FuelModel()
    s = transit.summarise(LEWES)
    close('sample line length', s['distance_nm'], 77.6844, 0.001, ' NM')
    close('straight-line distance', s['straight_nm'], 68.2190, 0.001, ' NM')
    close('routing cost', s['routing_cost_nm'], 9.4654, 0.001, ' NM')
    check('six legs', len(s['legs']) == 6)
    close('leg 1 bearing', s['legs'][0]['bearing'], 72.31, 0.01, ' deg')

    # -- With no current, both modes must agree exactly with distance/speed. This is
    #    the sanity floor: if the marcher cannot reproduce arithmetic, nothing above
    #    it means anything.
    exact_nm = s['distance_nm']          # not the rounded literal: 77.6844 is 3 mm off
    for mode in ('stw', 'sog'):
        p = transit.plan(LEWES, 8.0, mode=mode, model=M, currents=transit.NullCurrents())
        close(f'{mode} no-current hours', p['hours'], exact_nm / 8.0, 1e-9, ' h')
        close(f'{mode} no-current SOG', p['avg_sog_kt'], 8.0, 1e-9, ' kt')
        check(f'{mode} reports zero coverage honestly',
              p['current']['covered_fraction'] == 0.0)
        close(f'{mode} gap is the whole line', p['current']['gap_nm'], 77.6844, 0.001, ' NM')

    # -- FIXED THROTTLE. The revs are the input and the speed is the answer, which
    #    is what an ASV holding revs actually does. Asserted as a CHAIN rather than
    #    against a literal speed: the public tree ships a placeholder vessel whose
    #    curve is invented, and a hard-coded 6.18 kt would fail there for a reason
    #    that has nothing to do with the code being wrong.
    rev = 1650.0
    want = M.speed_for_rpm(rev, 'config_a')
    calm = transit.plan(LEWES, 0, mode='rpm', rpm=rev, model=M,
                        currents=transit.NullCurrents())
    close('fixed revs give the curve speed', calm['avg_sog_kt'], want, 1e-9, ' kt')
    close('and the hours follow from it', calm['hours'], exact_nm / want, 1e-9, ' h')
    check('the commanded revs are what is reported',
          all(abs(l['rpm'] - rev) < 1e-9 for l in calm['legs']))
    check('and are carried on the result', calm['rpm_command'] == rev)
    # The speed box is IGNORED here. A mode that quietly read it would sail at 8 kt.
    fake = transit.plan(LEWES, 8.0, mode='rpm', rpm=rev, model=M,
                        currents=transit.NullCurrents())
    close('speed_kt is not consulted in rpm mode', fake['hours'], calm['hours'], 1e-9, ' h')

    # WEATHER COSTS TIME HERE, NOT FUEL — the inverse of what it does in the speed
    # modes, and the whole reason this mode is not a relabelling of 'stw'.
    rough = transit.plan(LEWES, 0, mode='rpm', rpm=rev, model=M,
                         currents=transit.NullCurrents(),
                         weather={'wmo_sea_state': 5, 'wind_speed_kt': 25,
                                  'wind_from_deg': 200})
    check('a head sea slows a fixed throttle', rough['hours'] > calm['hours'] * 1.02,
          f"{rough['hours']:.3f} vs {calm['hours']:.3f} h")
    lph_calm = calm['litres'] / calm['hours']
    lph_rough = rough['litres'] / rough['hours']
    close('but the burn per hour does not move', lph_rough, lph_calm, 1e-9, ' L/h')
    check('so the revs still read as commanded in weather',
          all(abs(l['rpm'] - rev) < 1e-9 for l in rough['legs']))
    # The speed modes do the OPPOSITE, which is the pairing that gives the above
    # its meaning: same weather, same line, revs rise instead of speed falling.
    s_calm = transit.plan(LEWES, 8.0, mode='stw', model=M, currents=transit.NullCurrents())
    s_rough = transit.plan(LEWES, 8.0, mode='stw', model=M, currents=transit.NullCurrents(),
                           weather={'wmo_sea_state': 5, 'wind_speed_kt': 25,
                                    'wind_from_deg': 200})
    close('stw mode holds the clock instead', s_rough['hours'], s_calm['hours'], 1e-9, ' h')
    check('and pays in revs', s_rough['legs'][0]['rpm'] > s_calm['legs'][0]['rpm'])

    # A following sea must not be allowed to lend unbounded speed: the heading
    # premium is unbounded BELOW and the inversion divides by it.
    gale = M.stw_at_rpm(rev, 0.0, 'config_a', wmo_sea_state=0,
                        wind_speed_kt=60, wind_from_deg=180)
    check('following-sea credit is clamped', gale['premium_clamped'])
    close('and clamped at the stated floor', gale['total_premium'],
          fuel.PREMIUM_FLOOR, 1e-12)
    # Its acceptance case: an ordinary following wind is NOT clamped, so the clamp
    # is a guard rail rather than the model.
    mild = M.stw_at_rpm(rev, 0.0, 'config_a', wmo_sea_state=2,
                        wind_speed_kt=10, wind_from_deg=180)
    check('an ordinary following wind is left alone', not mild['premium_clamped'])
    check('and still helps', mild['stw_kt'] > want)

    for bad in (0, -100, None):
        try:
            transit.plan(LEWES, 8.0, mode='rpm', rpm=bad, model=M,
                         currents=transit.NullCurrents())
            check(f'rpm {bad!r} refused', False, 'no exception raised')
        except ValueError:
            check(f'rpm {bad!r} refused', True)
    try:
        transit.plan(LEWES, 8.0, mode='rpm', rpm=rev, model=None,
                     currents=transit.NullCurrents())
        check('rpm mode without a model refused', False, 'no exception raised')
    except ValueError:
        check('rpm mode without a model refused', True)

    # -- A zero-length leg (double-click) must not vanish and must not produce NaN:
    #    dropping it would renumber every leg and break the map/table correspondence.
    dup = [LEWES[0], LEWES[0], LEWES[1]]
    p = transit.plan(dup, 8.0, model=M, currents=transit.NullCurrents())
    check('zero-length leg is kept', len(p['legs']) == 2)
    check('zero-length leg is harmless', p['legs'][0]['distance_nm'] == 0.0
          and not math.isnan(p['hours']))

    # -- Refusals
    for bad, why in (([LEWES[0]], 'one point'), ([], 'no points')):
        try:
            transit.plan(bad, 8.0, model=M)
            check(f'refuses {why}', False, 'no exception raised')
        except ValueError:
            check(f'refuses {why}', True)
    for spd in (0.0, -3.0):
        try:
            transit.plan(LEWES, spd, model=M)
            check(f'refuses speed {spd}', False, 'no exception raised')
        except ValueError:
            check(f'refuses speed {spd}', True)
    try:
        transit.plan(LEWES, 8.0, mode='sideways', model=M)
        check('refuses unknown mode', False, 'no exception raised')
    except ValueError:
        check('refuses unknown mode', True)
    check('accepts a valid two-point line',
          transit.plan(LEWES[:2], 8.0, model=M)['distance_nm'] > 0)

    # -- Weather must MOVE the fuel, in the right direction, and by the model's
    #    own premium. A weather input that changes nothing is the failure mode.
    calm = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents())
    rough = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(),
                         weather={'wmo_sea_state': 5, 'wind_speed_kt': 25,
                                  'wind_from_deg': 13})
    check('rough weather costs more fuel', rough['litres'] > calm['litres'] * 1.2,
          f"{calm['litres']:.1f} -> {rough['litres']:.1f} L")
    check('weather does not change distance',
          abs(rough['distance_nm'] - calm['distance_nm']) < 1e-9)
    check('weather does not change time in stw mode',
          abs(rough['hours'] - calm['hours']) < 1e-9)

    # -- THE WIND SIGN, on ONE leg with an unambiguous course. Tested across the
    #    whole line this check is meaningless: the six legs run 072 to 193, so a
    #    single wind direction is a headwind on some and a following wind on others,
    #    and the total can move either way. (An earlier version of this test asserted
    #    the opposite of the truth for exactly that reason — `wind_from_deg` EQUAL to
    #    the course is a dead headwind, not a following wind.)
    leg = LEWES[5:7]                       # the 42.6 NM run, bearing 193.3 T
    course = round(geo.inverse(*leg[0], *leg[1])[1])
    head = transit.plan(leg, 8.0, model=M, currents=transit.NullCurrents(),
                        weather={'wmo_sea_state': 0, 'wind_speed_kt': 25,
                                 'wind_from_deg': course})
    follow = transit.plan(leg, 8.0, model=M, currents=transit.NullCurrents(),
                          weather={'wmo_sea_state': 0, 'wind_speed_kt': 25,
                                   'wind_from_deg': (course + 180) % 360})
    beam = transit.plan(leg, 8.0, model=M, currents=transit.NullCurrents(),
                        weather={'wmo_sea_state': 0, 'wind_speed_kt': 25,
                                 'wind_from_deg': (course + 90) % 360})
    bare = transit.plan(leg, 8.0, model=M, currents=transit.NullCurrents())
    check('a headwind costs more than calm', head['litres'] > bare['litres'],
          f"{bare['litres']:.2f} -> {head['litres']:.2f} L")
    check('a following wind is cheaper than calm', follow['litres'] < bare['litres'],
          f"{bare['litres']:.2f} -> {follow['litres']:.2f} L")
    # A beam wind is NEARLY neutral, not exactly: the great-circle bearing turns
    # about a degree over a 42 NM leg, so the wind is not precisely abeam at every
    # step, and because fuel is convex in RPM the small penalty and credit either
    # side do not cancel. Asserting an absolute tolerance here just encodes today's
    # rounding; asserting that beam is an order of magnitude closer to calm than
    # either head or following is the actual claim, and it still fails loudly if the
    # cosine ever loses its sign.
    d_beam = abs(beam['litres'] - bare['litres'])
    d_axis = min(abs(head['litres'] - bare['litres']), abs(follow['litres'] - bare['litres']))
    check('a beam wind is near-neutral', d_beam < 0.1 * d_axis,
          f'beam off by {d_beam:.3f} L vs axial {d_axis:.3f} L')

    # -- A synthetic current that REVERSES mid-passage. This is the whole reason the
    #    calculation marches: a single midpoint sample cannot see the turn, so a
    #    marcher and a one-shot evaluation must disagree here. If they agree, the
    #    integration has quietly stopped stepping.
    class Reversing(transit.CurrentSource):
        def __init__(self):
            super().__init__(None)
            self.tag = 'synthetic-reversing'

        def query(self, lat, lon, when):
            hours = (when - dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() / 3600
            # 6-hourly reversal, 2 kt, aligned with / against the 193 deg run
            return (193.0 if int(hours // 6) % 2 == 0 else 13.0), 2.0, 'measured'

    dep = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    fine = transit.plan(LEWES, 8.0, departure=dep, model=M, currents=Reversing(), step_nm=0.25)
    coarse = transit.plan(LEWES, 8.0, departure=dep, model=M, currents=Reversing(), step_nm=200.0)
    check('reversing current is integrated, not sampled once',
          abs(fine['hours'] - coarse['hours']) > 0.2,
          f"fine {fine['hours']:.3f} h vs one-shot {coarse['hours']:.3f} h")
    check('reversing current reports full coverage',
          abs(fine['current']['covered_fraction'] - 1.0) < 1e-9)
    check('a 2 kt reversing current changes the passage',
          abs(fine['hours'] - calm['hours']) > 0.1,
          f"{calm['hours']:.3f} -> {fine['hours']:.3f} h")

    # -- Step size must converge. If halving the step keeps moving the answer, the
    #    default is too coarse to trust.
    a = transit.plan(LEWES, 8.0, departure=dep, model=M, currents=Reversing(), step_nm=0.5)
    b = transit.plan(LEWES, 8.0, departure=dep, model=M, currents=Reversing(), step_nm=0.125)
    check('step size has converged by 0.5 NM', abs(a['hours'] - b['hours']) < 0.02,
          f"{a['hours']:.4f} vs {b['hours']:.4f} h")

    # -- An unholdable cross-set is a REFUSAL, and the acceptance case sits beside it.
    class Beam(transit.CurrentSource):
        def __init__(self, drift):
            super().__init__(None)
            self.drift = drift
            self.tag = 'synthetic-beam'

        def query(self, lat, lon, when):
            return (193.0 + 90.0) % 360, self.drift, 'measured'

    hard = transit.plan(LEWES, 4.0, model=M, currents=Beam(6.0))
    check('unholdable cross-set is flagged', not hard['feasible'] and hard['infeasible_legs'])
    check('unholdable legs carry a note', any(l['note'] for l in hard['legs'] if l['infeasible']))
    easy = transit.plan(LEWES, 8.0, model=M, currents=Beam(1.0))
    check('holdable cross-set is accepted', easy['feasible'], str(easy['infeasible_legs']))
    check('crabbing costs ground speed', easy['avg_sog_kt'] < 8.0, f"{easy['avg_sog_kt']:.3f}")

    # -- Partial coverage must be REPORTED, and the uncovered part must not be
    #    silently treated as slack water.
    class HalfCover(transit.CurrentSource):
        def __init__(self):
            super().__init__(None)
            self.tag = 'synthetic-half'

        def query(self, lat, lon, when):
            return (193.0, 2.0, 'measured') if lat > 38.4 else (None, None, None)

    h = transit.plan(LEWES, 8.0, model=M, currents=HalfCover())
    frac = h['current']['covered_fraction']
    check('partial coverage is reported', 0.05 < frac < 0.95, f'{frac:.3f}')
    close('coverage and gap agree', h['current']['gap_nm'],
          h['distance_nm'] * (1 - frac), 0.01, ' NM')
    check('covered legs got the current', any(l['drift_kt'] > 0.5 for l in h['legs']))
    check('uncovered legs report it', any(l['current_coverage'] < 0.999 for l in h['legs']))

    # -- Mode contract: sog mode holds the ground speed, stw mode does not.
    p_sog = transit.plan(LEWES, 8.0, departure=dep, mode='sog', model=M, currents=Reversing())
    close('sog mode makes good the target', p_sog['avg_sog_kt'], 8.0, 1e-6, ' kt')
    p_stw = transit.plan(LEWES, 8.0, departure=dep, mode='stw', model=M, currents=Reversing())
    check('stw mode lets the current move the clock',
          abs(p_stw['avg_sog_kt'] - 8.0) > 0.01, f"{p_stw['avg_sog_kt']:.4f}")

    # -- Fuel aboard has to REACH the endurance block. The arithmetic is checked in
    #    the fuel suite; what is checked here is the plumbing, because a keyword
    #    dropped between the request and the model is silent — the plan still
    #    returns an endurance, just the one for a full tank.
    p_full = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents())
    p_part = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(),
                          onboard_l=M.tank_l * 0.5)
    check('the plan carries the stated load', not p_part['endurance']['full_tank'])
    close('a half tank halves what is aboard', p_part['endurance']['onboard_l'],
          M.tank_l * 0.5, 1e-9, ' L')
    check('and it moves the margin',
          p_part['endurance']['margin_l'] < p_full['endurance']['margin_l'] - 1.0,
          f"{p_full['endurance']['margin_l']:.1f} -> {p_part['endurance']['margin_l']:.1f} L")
    close('the burn itself is unchanged by what is in the tank',
          p_part['litres'], p_full['litres'], 1e-9, ' L')

    # -- The request-side reading of it. 0 L is a legitimate answer and must not be
    #    read as "blank"; more than the tank holds is a mistyped gauge and is refused
    #    rather than trimmed to full, which would hand back a margin never held.
    import server
    check('blank means a full tank', server._onboard({}, M.tank_l) is None)
    check('an empty string means a full tank',
          server._onboard({'onboard_l': ''}, M.tank_l) is None)
    check('zero aboard is NOT blank', server._onboard({'onboard_l': 0}, M.tank_l) == 0.0)
    close('a stated load is read', server._onboard({'onboard_l': '140'}, 250.0),
          140.0, 1e-9, ' L')
    check('a full tank is accepted', server._onboard({'onboard_l': 250}, 250.0) == 250.0)
    for bad in (250.5, -1, 'plenty'):
        try:
            server._onboard({'onboard_l': bad}, 250.0)
            check(f'{bad!r} aboard refused', False, 'no exception raised')
        except ValueError:
            check(f'{bad!r} aboard refused', True)

    # -- AN EXPORT REQUEST IS VALIDATED, NOT TRUSTED. Both export routes used to
    #    carry their own copy of this prelude and neither checked the line, so a
    #    request with no points reached the writers, indexed points[0] and came
    #    back as a 500 with a traceback. The calculate path had always refused it.
    #    export_args touches no instance state on these paths, so it takes None.
    args = server.Handler.export_args
    for bad in ({}, {'points': []}, {'points': [{'lat': 38.8, 'lon': -75.2}]}):
        n = len(bad.get('points', []))
        try:
            args(None, dict(bad, format='geojson'))
            check(f'export of {n} points refused', False, 'no exception raised')
        except ValueError:
            check(f'export of {n} points refused', True)

    # The nearest acceptance: two points IS a transit and must still export. A
    # guard that only refuses is satisfied by code that refuses everything.
    two = [{'lat': LEWES[0][0], 'lon': LEWES[0][1]},
           {'lat': LEWES[1][0], 'lon': LEWES[1][1]}]
    a = args(None, {'points': two, 'format': 'geojson'})
    check('two points is an export', len(a['points']) == 2)
    check('and it writes bytes', len(exporters.export(**a)[0]) > 0)
    # The auto-zone branch reads points[0], so it is the line that failed FIRST on
    # an empty request. It has to keep working on a real one.
    az = args(None, {'points': two, 'format': 'shp', 'utm_zone': 'auto'})
    check('auto UTM zone resolves from the line', az['utm_zone'] == 18,
          str(az['utm_zone']))

    # -- THE CONFIG THE PAGE BOOTS FROM. This exact payload raised KeyError on every
    #    page load after the leeway table gained separate left and right rows, and
    #    nothing caught it: the console was silent, the class dropdown came up empty,
    #    and the drift still answered because the server falls back to its default
    #    class. A UI check of the feature passed while the page behind it was broken.
    lw_payload = server.leeway_class_payload()
    check('every leeway class reaches the page',
          len(lw_payload) == len(drift.LEEWAY_CLASSES), str(len(lw_payload)))
    for row in lw_payload:
        check(f"{row['key']} carries what the panel reads",
              all(row.get(f) is not None for f in
                  ('key', 'name', 'dw_slope_pct', 'dw_std_cms', 'cw_slope_pct')))
    check('the default class is among them',
          any(r['key'] == drift.DEFAULT_CLASS for r in lw_payload))
    check('and the payload is JSON-serialisable',
          isinstance(json.dumps(lw_payload), str))

    # -- bbox is what every download is scoped to
    lat0, lon0, lat1, lon1 = transit.line_bbox(LEWES, margin_deg=0.25)
    check('bbox covers the line',
          lat0 < min(p[0] for p in LEWES) and lat1 > max(p[0] for p in LEWES)
          and lon0 < min(p[1] for p in LEWES) and lon1 > max(p[1] for p in LEWES))
    close('bbox margin applied', lat0, min(p[0] for p in LEWES) - 0.25, 1e-9)


# --------------------------------------------------------------------------- #
def suite_currents():
    """Needs a cached OFS cycle. SKIPS loudly rather than passing on no data."""
    import currents as cmod
    try:
        tags = sorted((p.name[:-len('_meta.json')] for p in cmod.CACHE.glob('*_meta.json')),
                      reverse=True)
    except OSError:
        tags = []
    if not tags:
        return skip('currents', 'no cached OFS cycle in ofs_cache/')
    C = cmod.Currents(tag=tags[0])
    src = transit.CurrentSource(C, allow_projection=False)
    when = C.start + dt.timedelta(hours=3)

    # In-domain positions resolve; the supplied line's southern end does not. That
    # asymmetry IS the test — a source that answered everywhere would be inventing.
    inside = src.query(38.80, -75.10, when)
    check('in-domain position resolves', inside[2] == 'measured', str(inside))
    check('in-domain drift is plausible', 0.0 <= inside[1] < 8.0, str(inside[1]))
    far_south = src.query(36.0, -74.0, when)
    check('out-of-domain position refuses', far_south[2] is None, str(far_south))
    check('out-of-domain returns no fabricated drift', far_south[1] is None)

    # Time outside the cached span: refused without projection, answered with it —
    # and flagged as projected when it is.
    way_out = C.end + dt.timedelta(hours=40)
    check('outside the span refuses without projection',
          transit.CurrentSource(C, allow_projection=False).query(38.80, -75.10, way_out)[2] is None)
    proj = transit.CurrentSource(C, allow_projection=True).query(38.80, -75.10, way_out)
    if proj[2] is not None:
        check('projection is labelled as such', proj[2] == 'projected', str(proj[2]))

    # The real line, against the real cycle: coverage must be partial and reported.
    M = fuel.FuelModel()
    p = transit.plan(LEWES, 8.0, departure=C.start + dt.timedelta(hours=1),
                     model=M, currents=src)
    frac = p['current']['covered_fraction']
    check('supplied line is partially covered', 0.5 < frac < 1.0, f'{frac:.4f}')
    check('the uncovered part is the southern end',
          p['legs'][-1]['current_coverage'] < p['legs'][0]['current_coverage'],
          f"{p['legs'][-1]['current_coverage']:.3f} vs {p['legs'][0]['current_coverage']:.3f}")
    close('reported gap matches the coverage', p['current']['gap_nm'],
          p['distance_nm'] * (1 - frac), 0.01, ' NM')
    # ASSERT PER LEG, NOT ON THE TOTAL. An earlier version of this required the whole
    # passage to differ from the benign 9.71 h by more than 3 minutes, and it passed
    # only because of which cycle happened to be in the cache. Refreshing to a
    # different tide phase broke it with nothing wrong: a transit that runs with the
    # flood for half its length and against it for the other half legitimately takes
    # almost exactly the benign time. The total is a function of departure phase; the
    # PER-LEG effect is not, so that is what gets asserted.
    max_dev = max(abs(l['sog_kt'] - 8.0) for l in p['legs'])
    max_drift = max(l['drift_kt'] for l in p['legs'])
    check('real currents reach the legs', max_drift > 0.1, f'max drift {max_drift:.3f} kt')
    check('and move the speed made good', max_dev > 0.1, f'max |SOG-STW| {max_dev:.3f} kt')
    # Direction matters too: an along-track current must push the SOG the right way,
    # or the sign convention could be inverted and every check above still pass.
    for l in p['legs']:
        if abs(l['along_kt']) > 0.15:
            check('a favourable current raises SOG, an opposing one lowers it',
                  (l['sog_kt'] > 8.0) == (l['along_kt'] > 0),
                  f"leg {l['index'] + 1}: along {l['along_kt']:+.2f} kt, SOG {l['sog_kt']:.2f}")
            break


# --------------------------------------------------------------------------- #
def suite_ofs():
    """Multi-model current chaining. All offline: the domain logic is what matters
    and it must be testable without a 6 MB download."""
    C = ofs.CELL
    # -- occupancy raster. Its predecessor used nearest-node distance over a thinned
    #    node list and let DBOFS claim a point 20 km beyond its own boundary, so the
    #    cell semantics are pinned explicitly here.
    info = {'occupied': ['%d,%d' % (int(38.0 / C), int(-75.0 / C))]}
    check('occupied cell is covered', ofs.covers(info, 38.0, -75.0, rings=0))
    check('distant cell is not covered', not ofs.covers(info, 37.0, -75.0, rings=0))
    # Probe at cell CENTRES, not on cell boundaries. 38.05 is exactly a boundary at
    # CELL=0.05 and floating point puts it on either side depending on whether the
    # index is computed as lat/CELL or lat*20 — so an assertion there tests IEEE-754,
    # not the coverage logic. covers() has a stated one-cell tolerance; that is what
    # is checked, at unambiguous positions.
    check('rings=1 reaches the next cell',
          ofs.covers(info, 38.0 + 1.5 * C, -75.0, rings=1)
          and not ofs.covers(info, 38.0 + 1.5 * C, -75.0, rings=0))
    check('rings=1 does not reach three cells out',
          not ofs.covers(info, 38.0 + 3.5 * C, -75.0, rings=1))

    # -- two current answers blend as VECTORS
    out = ofs._blend((0.0, 1.0, 'measured'), (180.0, 1.0, 'measured'), 0.5)
    check('opposed equal currents blend to slack', out[1] < 1e-9, str(out[1]))
    out = ofs._blend((350.0, 1.0, 'measured'), (10.0, 1.0, 'measured'), 0.5)
    close('directions blend as vectors, not angles',
          (out[0] + 180) % 360 - 180, 0.0, 1e-6, ' deg')
    out = ofs._blend((90.0, 2.0, 'measured'), (90.0, 4.0, 'measured'), 0.5)
    close('same direction averages the drift', out[1], 3.0, 1e-9, ' kt')
    check('projected wins over measured in a blend',
          ofs._blend((0.0, 1.0, 'measured'), (0.0, 1.0, 'projected'), 0.5)[2] == 'projected')
    close('weight 1.0 is the primary alone',
          ofs._blend((0.0, 1.0, 'measured'), (180.0, 1.0, 'measured'), 1.0)[1], 1.0, 1e-9, ' kt')

    # -- MultiCurrents priority and fallthrough, on synthetic cycles
    class Fake:
        def __init__(self, tag, box, val):
            self.tag = tag
            self.lat0, self.lon0 = box[0], box[1]
            self.dlat = self.dlon = 0.01
            self.ny = int((box[2] - box[0]) / 0.01) + 1
            self.nx = int((box[3] - box[1]) / 0.01) + 1
            self.val = val
            self.start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

        def at(self, lat, lon, when):
            lat1 = self.lat0 + (self.ny - 1) * self.dlat
            lon1 = self.lon0 + (self.nx - 1) * self.dlon
            if self.lat0 <= lat <= lat1 and self.lon0 <= lon <= lon1:
                return (self.val, 90.0)          # (speed_kt, set_deg)
            return None

        def at_best(self, lat, lon, when):
            return None

    fine = Fake('fine', (38.0, -76.0, 39.0, -74.0), 1.0)
    coarse = Fake('coarse', (36.0, -78.0, 40.0, -73.0), 2.0)
    mc = ofs.MultiCurrents([(10, 'fine', fine, None), (20, 'coarse', coarse, None)])
    when = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    close('finest model wins where both cover', mc.query(38.5, -75.0, when)[1], 1.0, 1e-9, ' kt')
    check('the winning model is recorded', mc.last_source == 'fine', str(mc.last_source))
    close('falls through to the coarser model', mc.query(37.0, -75.0, when)[1], 2.0, 1e-9, ' kt')
    check('fallthrough is recorded', mc.last_source == 'coarse', str(mc.last_source))
    check('neither model: still an honest miss',
          mc.query(10.0, 10.0, when) == (None, None, None))
    check('tally counts both models', mc.tally.get('fine') and mc.tally.get('coarse'),
          str(mc.tally))

    # -- the handover BLENDS. Just inside the fine model's edge the answer must sit
    #    between the two, or the ETA takes a step in the middle of a leg.
    edge = mc.query(38.02, -75.0, when)
    check('handover is blended, not a step', 1.0 < edge[1] < 2.0, '%.4f kt' % edge[1])
    check('blend stays nearer the primary', edge[1] < 1.5, '%.4f kt' % edge[1])

    # -- a line is sampled along its LENGTH, not just at its vertices
    pts = ofs._sample_line(LEWES, step_km=5.0)
    check('line sampling covers the length', len(pts) > len(LEWES) * 3, str(len(pts)))
    check('sampling starts at the first point',
          abs(pts[0][0] - LEWES[0][0]) < 1e-6 and abs(pts[0][1] - LEWES[0][1]) < 1e-6)
    check('sampling ends at the last point',
          abs(pts[-1][0] - LEWES[-1][0]) < 1e-6 and abs(pts[-1][1] - LEWES[-1][1]) < 1e-6)
    check('nominal boxes screen correctly',
          ofs._boxes_touch((38, -76, 39, -74), (37.8, -75.9, 40.3, -73.2))
          and not ofs._boxes_touch((10, 10, 11, 11), (37.8, -75.9, 40.3, -73.2)))


# --------------------------------------------------------------------------- #
def suite_marine():
    """IDW over scattered wind/wave samples. Offline, on synthetic samples."""
    M = fuel.FuelModel()
    tbl = M.data['sea_state_premium']['table']

    # THE CENTRAL CASE, and it is not hypothetical: on the real line buoy 44009
    # reports wind with its wave fields blank and 44084 reports waves with its wind
    # fields blank. Each FIELD must interpolate from whichever samples carry it.
    windy = {'lat': 38.0, 'lon': -75.0, 'source': 'ndbc', 'id': 'W',
             'values': {'wind_kt': 20.0, 'wind_from_deg': 0.0}, 'age_s': 60}
    wavy = {'lat': 38.1, 'lon': -75.0, 'source': 'ndbc', 'id': 'V',
            'values': {'wave_m': 2.0, 'wave_from_deg': 90.0}, 'age_s': 60}
    got = marine.idw([windy, wavy], 38.05, -75.0)
    close('wind comes from the wind-only station', got['wind_kt'], 20.0, 1e-6, ' kt')
    close('wave comes from the wave-only station', got['wave_m'], 2.0, 1e-6, ' m')
    check('wind contributors name only the wind station',
          [c['id'] for c in got['_meta']['contributors']['wind_kt']] == ['W'])
    check('wave contributors name only the wave station',
          [c['id'] for c in got['_meta']['contributors']['wave_m']] == ['V'])
    check('an unreported field is absent, not zero', 'gust_kt' not in got)

    # -- distance weighting
    a = {'lat': 38.0, 'lon': -75.0, 'source': 'nws', 'id': 'A',
         'values': {'wind_kt': 10.0}, 'age_s': None}
    b = {'lat': 39.0, 'lon': -75.0, 'source': 'nws', 'id': 'B',
         'values': {'wind_kt': 30.0}, 'age_s': None}
    close('at a sample, that sample dominates',
          marine.idw([a, b], 38.0, -75.0)['wind_kt'], 10.0, 0.6, ' kt')
    mid = marine.idw([a, b], 38.5, -75.0)['wind_kt']
    check('between two samples the value lies between', 10.0 < mid < 30.0, '%.3f' % mid)
    close('equidistant and equal quality averages', mid, 20.0, 0.5, ' kt')

    # -- source quality: an observation outranks a coarse model at equal distance,
    #    and the preference must reverse when the labels do (or it is not the
    #    QUALITY doing the work).
    obs = {'lat': 38.0, 'lon': -75.0, 'source': 'ndbc', 'id': 'O',
           'values': {'wave_m': 1.0}, 'age_s': 60}
    mod = {'lat': 39.0, 'lon': -75.0, 'source': 'ww3', 'id': 'M',
           'values': {'wave_m': 3.0}, 'age_s': None}
    v = marine.idw([obs, mod], 38.5, -75.0)['wave_m']
    check('an observation outweighs a coarse model', v < 2.0, '%.3f m' % v)
    flipped = marine.idw([dict(obs, source='ww3'), dict(mod, source='ndbc')],
                         38.5, -75.0)['wave_m']
    check('and the preference reverses with the sources', flipped > 2.0, '%.3f m' % flipped)

    # -- directions blend as vectors, weighted by magnitude
    n1 = {'lat': 38.0, 'lon': -75.0, 'source': 'nws', 'id': '1',
          'values': {'wind_kt': 10.0, 'wind_from_deg': 350.0}, 'age_s': None}
    n2 = {'lat': 38.0, 'lon': -75.0, 'source': 'nws', 'id': '2',
          'values': {'wind_kt': 10.0, 'wind_from_deg': 10.0}, 'age_s': None}
    d = marine.idw([n1, n2], 38.0, -75.0)['wind_from_deg']
    close('350 and 010 average to 000, not 180', (d + 180) % 360 - 180, 0.0, 1e-6, ' deg')
    big = {'lat': 38.0, 'lon': -75.0, 'source': 'nws', 'id': 'big',
           'values': {'wave_m': 3.0, 'wave_from_deg': 0.0}, 'age_s': None}
    ripple = {'lat': 38.0, 'lon': -75.0, 'source': 'nws', 'id': 'rip',
              'values': {'wave_m': 0.05, 'wave_from_deg': 180.0}, 'age_s': None}
    wd = marine.idw([big, ripple], 38.0, -75.0)['wave_from_deg']
    check('a ripple does not swing the swell direction',
          abs((wd + 180) % 360 - 180) < 5.0, '%.2f deg' % wd)

    # -- refusals
    empty = marine.field_at([], 38.0, -75.0, sea_table=tbl)
    check('no samples yields nulls, not calm',
          empty['wind_speed_kt'] is None and empty['wmo_sea_state'] is None)
    far = {'lat': 20.0, 'lon': -75.0, 'source': 'nws', 'id': 'F',
           'values': {'wind_kt': 40.0}, 'age_s': None}
    check('samples beyond max_km are dropped',
          'wind_kt' not in marine.idw([far], 38.0, -75.0, max_km=400.0))
    check('...but the same sample inside max_km is used',
          'wind_kt' in marine.idw([far], 38.0, -75.0, max_km=3000.0))

    # -- sampling spreads by DISTANCE. The supplied line has a 1.2 NM leg and a
    #    42.6 NM leg; per-vertex sampling would put most nodes in the first six
    #    miles and leave the whole offshore run to one.
    net = marine._thin(LEWES, 6)
    check('thin returns the requested count', len(net) == 6, str(len(net)))
    check('thin keeps both ends',
          abs(net[0][0] - LEWES[0][0]) < 1e-6 and abs(net[-1][0] - LEWES[-1][0]) < 1e-6)

    # EVEN ALONG THE PATH, not in a straight line. Measuring the straight geodesic
    # between consecutive samples is the wrong quantity on a line with corners: the
    # first samples near Lewes span three sharp turns, so their direct separation is
    # legitimately shorter than their path separation. A straight two-point line is
    # where the two measures coincide, so the spacing logic is checked there...
    straight = [LEWES[0], LEWES[-1]]
    sn = marine._thin(straight, 6)
    sg = [geo.inverse(sn[i][0], sn[i][1], sn[i + 1][0], sn[i + 1][1])[0]
          for i in range(len(sn) - 1)]
    check('thin spreads evenly on a straight line',
          (max(sg) - min(sg)) / max(sg) < 0.01,
          str([round(g / 1852.0, 2) for g in sg]))

    # ...and on the real line the samples must still advance monotonically down it
    # and never bunch, which is what the sampling is actually for.
    gaps = [geo.inverse(net[i][0], net[i][1], net[i + 1][0], net[i + 1][1])[0]
            for i in range(len(net) - 1)]
    check('thin never bunches on a cornered line', min(gaps) > 0.5 * max(gaps),
          str([round(g / 1852.0, 2) for g in gaps]))
    check('thin advances down the line',
          all(net[i][0] > net[i + 1][0] for i in range(len(net) - 1)),
          str([round(p[0], 3) for p in net]))

    # -- MarineField
    field = transit.MarineField([a, b], tbl)
    check('MarineField caches identically', field.at(38.5, -75.0) is field.at(38.5, -75.0))
    check('MarineField varies with position',
          abs(field.at(38.05, -75.0)['wind_speed_kt']
              - field.at(38.95, -75.0)['wind_speed_kt']) > 1.0)
    check('an empty MarineField answers empty',
          transit.MarineField([], tbl).at(38.0, -75.0) == {})

    # -- both weather shapes still work through plan()
    p = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(),
                     weather={'wmo_sea_state': 3, 'wind_speed_kt': 15, 'wind_from_deg': 200})
    check('a leg-wide dict is still accepted', all(l['sea_state'] == 3 for l in p['legs']))

    grad = [{'lat': 38.81, 'lon': -75.16, 'source': 'nws', 'id': 'N',
             'values': {'wind_kt': 5.0, 'wind_from_deg': 200.0, 'wave_m': 0.2},
             'age_s': None},
            {'lat': 37.66, 'lon': -75.00, 'source': 'nws', 'id': 'S',
             'values': {'wind_kt': 25.0, 'wind_from_deg': 200.0, 'wave_m': 2.6},
             'age_s': None}]
    pf = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(),
                      weather=transit.MarineField(grad, tbl))
    seas = [l['sea_state'] for l in pf['legs']]
    winds = [l['wind_kt'] for l in pf['legs']]
    check('an interpolated field gives a VARYING sea state', len(set(seas)) > 1, str(seas))
    check('and a varying wind', max(winds) - min(winds) > 3.0,
          str([round(w, 1) for w in winds]))
    check('the wind rises toward the rough end', winds[-1] > winds[0],
          '%.1f -> %.1f kt' % (winds[0], winds[-1]))
    # A field that varies must also move the FUEL, or the interpolation is decorative.
    flat_field = [dict(g, values=dict(g['values'], wind_kt=5.0, wave_m=0.2)) for g in grad]
    pflat = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(),
                         weather=transit.MarineField(flat_field, tbl))
    check('a rough end costs more fuel than a calm one',
          pf['litres'] > pflat['litres'] * 1.05,
          '%.1f vs %.1f L' % (pflat['litres'], pf['litres']))



# --------------------------------------------------------------------------- #
def _offline_plan():
    return transit.plan(LEWES, 8.0, model=fuel.FuelModel(),
                        currents=transit.NullCurrents(),
                        weather={'wmo_sea_state': 2, 'wind_speed_kt': 12,
                                 'wind_from_deg': 200})


# --------------------------------------------------------------------------- #
class _FakeCurrents:
    """A current source with known answers, so the drift suite tests DRIFT.

    Deliberately not the cached cycle: that would make these checks depend on which
    file happens to be on disk, which this file has already been burned by once (see
    the currents suite). A stub also lets the horizon and the dry-node nudge be
    provoked exactly, which a real forecast will not do on demand.
    """

    def __init__(self, set_deg=90.0, drift_kt=1.0, until_h=None,
                 hole=None, start=None):
        self.set_deg = set_deg
        self.drift_kt = drift_kt
        self.until_h = until_h          # None = forever; else hours of data
        self.hole = hole                # (lat, lon, km) with no water at the centre
        self.start = start or dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
        self.tag = 'fake'

    def query(self, lat, lon, when):
        if self.until_h is not None:
            if (when - self.start).total_seconds() / 3600.0 >= self.until_h:
                return None, None, None
        if self.hole:
            hlat, hlon, km = self.hole
            if geo.inverse(lat, lon, hlat, hlon)[0] < km * 1000.0:
                return None, None, None
        return self.set_deg, self.drift_kt, 'measured'


def suite_drift():
    T0 = dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
    calm = _FakeCurrents(drift_kt=0.0, start=T0)

    # -- THE PUBLISHED EQUATION, CHECKED BY HAND. Slope is a percentage of the wind
    #    in METRES PER SECOND and the result is centimetres per second, while this
    #    tool speaks knots throughout — so the conversion is the obvious place to be
    #    wrong by a factor of two and not notice. PIW-1's downwind slope is 0.96%,
    #    so 20 kt (10.2889 m/s) gives 9.877 cm/s, which is 0.192 kt.
    piw = drift.LEEWAY_CLASSES['piw-1']
    dw, cw = drift.leeway_components(20.0, piw)
    close('downwind leeway matches the published equation', dw,
          0.96 * (20.0 * 0.5144444) * 0.0194384, 1e-9, ' kt')
    close('and is 0.192 kt for PIW-1 at 20 kt', dw, 0.192, 0.001, ' kt')
    close('crosswind likewise', cw,
          0.54 * (20.0 * 0.5144444) * 0.0194384, 1e-9, ' kt')
    check('leeway scales with the wind',
          abs(drift.leeway_components(40.0, piw)[0] - 2 * dw) < 1e-9)
    # THE OFFSET IS NOT DECORATIVE: a sea kayak drifts at a measured rate in NO wind,
    # and a version that dropped the term would read zero here.
    kayak = drift.LEEWAY_CLASSES['kayak']
    close('the offset term survives zero wind',
          drift.leeway_components(0.0, kayak)[0], 11.12 * 0.0194384, 1e-9, ' kt')
    close('and PIW-1, having none, gives zero',
          drift.leeway_components(0.0, piw)[0], 0.0, 1e-12, ' kt')
    # The error term enters the slope AND the offset, per the published form.
    close('the error term enters as slope and offset',
          drift.leeway_components(20.0, piw, dw_eps=12.0)[0],
          ((0.96 + 12.0 / 20.0) * (20.0 * 0.5144444) + 12.0 / 2.0) * 0.0194384,
          1e-9, ' kt')
    check('the low tail can reverse the downwind term',
          drift.leeway_components(20.0, piw, dw_eps=-1.645 * 12.0)[0] < 0,
          'PIW-1 at its 5th percentile drifts upwind of the water, and is NOT '
          'clamped — clipping it would shift the whole ensemble downwind')

    # -- LEEWAY GOES DOWNWIND, and getting that backwards is the one error here
    #    that would send a search the wrong way. Wind FROM 200 puts the hull on 020
    #    plus its crosswind, and the check runs with no current so nothing else can
    #    carry it.
    r = drift.predict(38.0, -75.0, T0, 10.0, calm,
                      weather={'wind_speed_kt': 20.0, 'wind_from_deg': 200.0},
                      leeway_class='skiff-swamped', current_scale=0.0)
    end = r['track'][-1]
    brg = geo.inverse(38.0, -75.0, end['lat'], end['lon'])[1]
    check('leeway runs downwind, within its own divergence',
          abs(((brg - 20.0 + 180.0) % 360.0) - 180.0) < 20.0, f'{brg:.1f} deg')
    dwk, cwk = drift.leeway_components(20.0, drift.LEEWAY_CLASSES['skiff-swamped'])
    close('and travels at the leeway speed',
          geo.inverse(38.0, -75.0, end['lat'], end['lon'])[0] / 1852.0,
          math.hypot(dwk, cwk) * 10.0, 0.05, ' NM')
    # Its acceptance pair: no wind, and a class with no offset, means no movement.
    still = drift.predict(38.0, -75.0, T0, 10.0, calm, weather=None,
                          leeway_class='piw-1', current_scale=0.0)
    close('no wind, no movement', geo.inverse(
        38.0, -75.0, still['track'][-1]['lat'], still['track'][-1]['lon'])[0], 0.0,
        1.0, ' m')

    # -- BOTH TACKS, EQUALLY, HELD. Which side of downwind an object takes is set at
    #    the outset and is not predictable, so the ensemble carries both rather than
    #    averaging them into a single line down the middle.
    ens = drift.predict(38.0, -75.0, T0, 6.0, calm,
                        weather={'wind_speed_kt': 20.0, 'wind_from_deg': 0.0},
                        leeway_class='piw-1', current_scale=0.0)
    sides = [m['orientation'] for m in ens['members']]
    check('both orientations are carried', 'left' in sides and 'right' in sides)
    check('and equally', sides.count('left') == sides.count('right'))
    # Compare the CENTRAL member of each tack. Taking any two members will not do:
    # at PIW-1's measured extremes the downwind term goes negative and the tails
    # genuinely cross over each other, so a pair picked at random can sit on the
    # same side of the wind for a perfectly good reason.
    def _mid(side):
        return next(m for m in ens['members']
                    if m['orientation'] == side and m['dw_eps'] == 0.0
                    and m['cw_eps'] == 0.0 and m['current_scale'] == 1.0)
    bl = geo.inverse(38.0, -75.0, _mid('left')['end']['lat'],
                     _mid('left')['end']['lon'])[1]
    br = geo.inverse(38.0, -75.0, _mid('right')['end']['lat'],
                     _mid('right')['end']['lon'])[1]
    check('the two tacks straddle downwind', bl < 180.0 < br,
          f'left {bl:.1f}, right {br:.1f}, against downwind 180')
    check('and both are within a sensible divergence of it',
          abs(bl - 180.0) < 60.0 and abs(br - 180.0) < 60.0,
          f'left {bl:.1f}, right {br:.1f}')

    for bad in ('nonesuch', '', 'PIW-1 '):
        try:
            drift.predict(38.0, -75.0, T0, 6.0, calm, leeway_class=bad)
            check(f'leeway class {bad!r} refused', False, 'no exception raised')
        except ValueError:
            check(f'leeway class {bad!r} refused', True)
    check('the default class is one that exists',
          drift.DEFAULT_CLASS in drift.LEEWAY_CLASSES)
    # EVERY CLASS CARRIES BOTH ROWS. A class added with only one would be silently
    # symmetrised, which is the thing the two-row table exists to prevent.
    for k, c in drift.LEEWAY_CLASSES.items():
        check(f'{k} carries both crosswind rows',
              len(c['cw_right']) == 3 and len(c['cw_left']) == 3)

    # -- ASYMMETRIC CLASSES ARE NOT MIRRORED. The Navy sub-escape raft with a drogue
    #    has a crosswind of ZERO slope and a constant offset — +5.7 cm/s to the right
    #    against -3.4 to the left. A version that stored one row and flipped its sign
    #    would report the two tacks as equal and opposite, which they are not.
    seie = drift.LEEWAY_CLASSES['seie-drogue']
    _, cw_r = drift.leeway_components(20.0, seie, right=True)
    _, cw_l = drift.leeway_components(20.0, seie, right=False)
    close('its right tack is the published offset', cw_r, 5.70 * 0.0194384, 1e-9, ' kt')
    close('its left tack is the other published offset', cw_l,
          -3.40 * 0.0194384, 1e-9, ' kt')
    check('the two tacks are NOT mirror images', abs(cw_r) - abs(cw_l) > 0.01,
          f'{cw_r:.3f} against {cw_l:.3f} kt')
    # Zero slope means the crosswind does not grow with the wind, unlike PIW-1's.
    close('and its crosswind ignores wind speed',
          drift.leeway_components(40.0, seie, right=True)[1], cw_r, 1e-9, ' kt')
    check('where PIW-1 crosswind does scale',
          drift.leeway_components(40.0, piw, right=True)[1] > 1.9 *
          drift.leeway_components(20.0, piw, right=True)[1])
    # A negative downwind offset shows up in light airs: the raft is measured as
    # drifting slightly BEHIND the water when there is no wind to push it.
    check('the negative downwind offset survives calm',
          drift.leeway_components(0.0, seie)[0] < 0,
          f"{drift.leeway_components(0.0, seie)[0]:.4f} kt")
    check('and it is flagged as not measured on this vessel',
          ens['leeway']['measured_for_this_vessel'] is False)

    # -- ADVECTION. Set is the direction the water flows TOWARD, so a 1 kt set of
    #    090 for 6 h puts the hull 6 NM east and nowhere else.
    east = _FakeCurrents(set_deg=90.0, drift_kt=1.0, start=T0)
    r = drift.predict(38.0, -75.0, T0, 6.0, east, weather=None, current_scale=0.0)
    e = r['track'][-1]
    d_m, brg, _ = geo.inverse(38.0, -75.0, e['lat'], e['lon'])
    close('advected the drift distance', d_m / 1852.0, 6.0, 0.05, ' NM')
    close('advected toward the set', brg, 90.0, 1.0, ' deg')

    # -- THE ENSEMBLE IS THE ANSWER. The lattice is 2 orientations x 3 downwind
    #    quantiles x 3 crosswind x 3 current scales.
    spread = drift.predict(38.0, -75.0, T0, 12.0, east,
                           weather={'wind_speed_kt': 20.0, 'wind_from_deg': 200.0},
                           leeway_class='piw-1')
    check('54 members walked', len(spread['members']) == 54,
          str(len(spread['members'])))
    check('the spread reaches the envelope', spread['datum']['radius_nm'] > 0.5,
          f"{spread['datum']['radius_nm']:.2f} NM")
    # And the pairing that gives it meaning: a class with NO measured spread, and no
    # current doubt, must collapse the radius — otherwise it comes from somewhere
    # unstated. SLDMB is the zero-windage datum buoy, all coefficients zero.
    tight = drift.predict(38.0, -75.0, T0, 12.0, east,
                          weather={'wind_speed_kt': 20.0, 'wind_from_deg': 200.0},
                          leeway_class='sldmb', current_scale=0.0)
    close('no measured doubt, no radius', tight['datum']['radius_nm'], 0.0, 1e-9, ' NM')
    check('and that class needs only its two tacks', len(tight['members']) == 2,
          str(len(tight['members'])))
    # A wider measured spread must give a wider circle, or the std devs are inert.
    tight_std = drift.predict(38.0, -75.0, T0, 12.0, east,
                              weather={'wind_speed_kt': 20.0, 'wind_from_deg': 200.0},
                              leeway_class='skiff-runabout', current_scale=0.0)
    wide_std = drift.predict(38.0, -75.0, T0, 12.0, east,
                             weather={'wind_speed_kt': 20.0, 'wind_from_deg': 200.0},
                             leeway_class='piw-1', current_scale=0.0)
    check('a wider measured spread gives a wider circle',
          wide_std['datum']['radius_nm'] > tight_std['datum']['radius_nm'],
          f"PIW-1 {wide_std['datum']['radius_nm']:.1f} NM vs runabout "
          f"{tight_std['datum']['radius_nm']:.1f} NM — 12.0 cm/s against 2.2")

    # -- DETERMINISM. An emergency is the worst moment to find the datum moved
    #    because something sampled randomly.
    a = drift.predict(38.0, -75.0, T0, 8.0, east,
                      weather={'wind_speed_kt': 15.0, 'wind_from_deg': 45.0})
    b = drift.predict(38.0, -75.0, T0, 8.0, east,
                      weather={'wind_speed_kt': 15.0, 'wind_from_deg': 45.0})
    check('two runs agree exactly',
          a['datum']['lat'] == b['datum']['lat']
          and a['datum']['lon'] == b['datum']['lon']
          and a['datum']['radius_nm'] == b['datum']['radius_nm'])

    # -- THE HORIZON IS REPORTED, INCLUDING AT ZERO. A horizon of 0.0 h — no data
    #    even at the casualty position — is the most severe answer this can give,
    #    and a truthiness test reported it as no horizon at all. That regression
    #    is what this pair exists for.
    short = _FakeCurrents(drift_kt=1.0, until_h=4.0, start=T0)
    r = drift.predict(38.0, -75.0, T0, 12.0, short, weather=None)
    close('horizon at the end of the data', r['horizon_h'], 4.0, 0.3, ' h')
    check('and it is dated', r['horizon_utc'] is not None)
    check('coverage falls with it', r['covered_fraction'] < 0.5,
          f"{r['covered_fraction']:.2f}")
    none_at_all = _FakeCurrents(drift_kt=1.0, until_h=0.0, start=T0)
    try:
        drift.predict(38.0, -75.0, T0, 6.0, none_at_all, weather=None)
        check('a start with no forecast is refused', False, 'no exception raised')
    except ValueError:
        check('a start with no forecast is refused', True)
    # Acceptance beside it: data that covers the whole window reports no horizon.
    full = drift.predict(38.0, -75.0, T0, 6.0, east, weather=None)
    check('a covered window has no horizon', full['horizon_h'] is None)
    check('and says so as full coverage', full['covered_fraction'] == 1.0)

    # -- A DRY GRID NODE IS NOT A DOMAIN EXIT. The first real position tried in
    #    development read back nothing while water sat 0.02 deg away.
    holed = _FakeCurrents(drift_kt=0.2, hole=(38.0, -75.0, 0.4), start=T0)
    r = drift.predict(38.0, -75.0, T0, 3.0, holed, weather=None)
    check('a dry node is read from beside it', r['covered_fraction'] == 1.0,
          f"{r['covered_fraction']:.2f}")
    check('and the borrowing is reported', r['nudged_fraction'] > 0.0)
    # Its refusal pair: a hole wider than the nudge is a real gap, not a node.
    wide = _FakeCurrents(drift_kt=0.2, hole=(38.0, -75.0, 50.0), start=T0)
    try:
        drift.predict(38.0, -75.0, T0, 3.0, wide, weather=None)
        check('a real gap is still refused', False, 'no exception raised')
    except ValueError:
        check('a real gap is still refused', True)

    # -- THE SWELL-ONLY STOKES TERM. Wave transport that the leeway coefficients
    #    cannot already contain, because they were measured with seas running and
    #    hold the wind-correlated part of it.
    hs, tp = 2.0, 8.0
    om = 2 * math.pi / tp
    kk = om * om / 9.80665
    amp = hs / (2 * math.sqrt(2.0))          # from the ENERGY, not Hs/2
    want_kt = (amp * amp * om * kk) / 0.5144444
    got, toward, note = drift.stokes_drift(hs, tp, 270.0)
    close('Stokes matches the deep-water narrow-band result', got, want_kt, 1e-9, ' kt')
    check('and the amplitude comes from the energy, not Hs/2',
          abs(got - want_kt * 2.0) > 1e-6,
          'using Hs/2 would double it — the classic factor-of-two here')
    close('it runs the way the waves go', toward, 90.0, 1e-9, ' deg')
    check('no note for sane wave data', note is None)
    # Longer swell of the same height carries LESS, because Stokes goes as omega^3.
    check('a longer swell drifts less', drift.stokes_drift(hs, 14.0, 270.0)[0] < got,
          'omega-cubed weighting — long swell is not the strong Stokes case')

    # THE PROJECTION IS THE POINT. With the wind, it is already in the leeway.
    with_wind = drift.stokes_drift(hs, tp, 270.0, wind_from_deg=270.0, wind_kt=15.0)
    close('swell running with the wind adds nothing', with_wind[0], 0.0, 1e-9, ' kt')
    against = drift.stokes_drift(hs, tp, 270.0, wind_from_deg=90.0, wind_kt=15.0)
    close('swell against the wind survives in full', against[0], got, 1e-9, ' kt')
    across = drift.stokes_drift(hs, tp, 270.0, wind_from_deg=0.0, wind_kt=15.0)
    close('and so does swell across it', across[0], got, 1e-9, ' kt')
    # A partial angle keeps only the part off the wind axis.
    part = drift.stokes_drift(hs, tp, 270.0, wind_from_deg=315.0, wind_kt=15.0)[0]
    check('an oblique swell is partly absorbed', 0.0 < part < got, f'{part:.4f} kt')

    # Bad data must be capped rather than believed: Stokes goes as the SQUARE of
    # steepness, so a 6 m sea at 5 s would invent a knot of drift.
    steep = drift.stokes_drift(6.0, 5.0, 270.0)
    check('an impossible steepness is capped', 'capped' in (steep[2] or ''), str(steep[2]))
    check('and the capped value is small', steep[0] < 0.5, f'{steep[0]:.3f} kt')
    check('a realistic sea is NOT capped', drift.stokes_drift(2.0, 9.0, 270.0)[2] is None)
    for missing in ((None, tp, 270.0), (hs, None, 270.0), (hs, tp, None), (0.0, tp, 270.0)):
        close(f'no Stokes without {missing}', drift.stokes_drift(*missing)[0], 0.0,
              1e-12, ' kt')

    # And it has to reach the prediction, not just the helper.
    wx_none = {'wind_speed_kt': 15.0, 'wind_from_deg': 200.0}
    wx_with = dict(wx_none, wave_height_m=2.0, wave_period_s=9.0, wave_from_deg=200.0)
    wx_cross = dict(wx_none, wave_height_m=2.0, wave_period_s=9.0, wave_from_deg=110.0)
    p_none = drift.predict(38.0, -75.0, T0, 12.0, east, weather=wx_none)
    p_with = drift.predict(38.0, -75.0, T0, 12.0, east, weather=wx_with)
    p_cross = drift.predict(38.0, -75.0, T0, 12.0, east, weather=wx_cross)
    close('a following swell changes nothing', p_with['datum']['from_start_nm'],
          p_none['datum']['from_start_nm'], 1e-9, ' NM')
    check('a crossing swell moves the datum',
          abs(p_cross['datum']['from_start_nm']
              - p_none['datum']['from_start_nm']) > 0.05,
          f"{p_cross['datum']['from_start_nm']:.2f} against "
          f"{p_none['datum']['from_start_nm']:.2f} NM")
    check('and the distance it carried is reported', p_cross['stokes_nm'] > 0.05,
          f"{p_cross['stokes_nm']:.2f} NM")
    close('where a following swell carries nothing', p_with['stokes_nm'], 0.0,
          1e-12, ' NM')

    # -- COMPARING DRIFT MODELS AGAINST A FIXED ENVIRONMENT. The guarantee is that
    #    the ONLY thing varying between compared runs is the leeway class, so any
    #    difference is attributable to it. That is asserted rather than assumed:
    #    each class run inside a comparison must land exactly where the same class
    #    run on its own lands, given the same position, time, weather and currents.
    wx_cmp = {'wind_speed_kt': 18.0, 'wind_from_deg': 250.0}
    alone = {k: drift.predict(38.0, -75.0, T0, 10.0, east, weather=wx_cmp,
                              leeway_class=k)['datum']
             for k in ('seie-drogue', 'piw-1', 'sldmb')}
    for k, dat in alone.items():
        again = drift.predict(38.0, -75.0, T0, 10.0, east, weather=wx_cmp,
                              leeway_class=k)['datum']
        check(f'{k} is reproducible under a fixed environment',
              again['lat'] == dat['lat'] and again['lon'] == dat['lon']
              and again['radius_nm'] == dat['radius_nm'])
    # And the classes must actually DIFFER from each other, or a comparison that
    # holds the environment fixed would be a table of identical rows.
    check('different drift models give different datums',
          alone['sldmb']['lat'] != alone['piw-1']['lat'],
          'zero-windage against a person in water, same water and wind')
    # The zero-windage class is the control: with no leeway it must be pure
    # advection, so its datum cannot move when the WIND changes.
    calm_sldmb = drift.predict(38.0, -75.0, T0, 10.0, east,
                               weather={'wind_speed_kt': 0.0, 'wind_from_deg': 0.0},
                               leeway_class='sldmb')['datum']
    close('the no-windage class ignores the wind entirely',
          calm_sldmb['lat'], alone['sldmb']['lat'], 1e-12)
    check('where a windage class does not',
          drift.predict(38.0, -75.0, T0, 10.0, east,
                        weather={'wind_speed_kt': 0.0, 'wind_from_deg': 0.0},
                        leeway_class='piw-1')['datum']['lat']
          != alone['piw-1']['lat'])

    for bad_hours in (0.0, -3.0):
        try:
            drift.predict(38.0, -75.0, T0, bad_hours, east)
            check(f'{bad_hours} h refused', False, 'no exception raised')
        except ValueError:
            check(f'{bad_hours} h refused', True)
    try:
        drift.predict(38.0, -75.0, T0, 6.0, None)
        check('no current source refused', False, 'no exception raised')
    except ValueError:
        check('no current source refused', True)

    # The result must carry its own assumptions: this output gets handed to someone
    # who was not in the room when the leeway range was chosen.
    check('the assumptions travel with the answer',
          len(spread['assumptions']) >= 4
          and any('NO CLASS EXISTS FOR THIS VESSEL' in a
                  for a in spread['assumptions']))
    check('and the leeway is flagged unmeasured',
          spread['leeway']['measured_for_this_vessel'] is False)
    check('and cites where the coefficients came from',
          'Allen' in spread['leeway']['source'])


SUITES = {
    'geodesy': suite_geodesy,
    'shapefile': suite_shapefile,
    'export': suite_export,
    'fuel': suite_fuel,
    'weather': suite_weather,
    'transit': suite_transit,
    'drift': suite_drift,
    'ofs': suite_ofs,
    'marine': suite_marine,
    'currents': suite_currents,
}


def main():
    want = [a for a in sys.argv[1:] if not a.startswith('-')]
    names = want or list(SUITES)
    for n in names:
        if n not in SUITES:
            print(f'no suite {n!r}; have {", ".join(SUITES)}')
            return 2
        print(f'  {n}')
        SUITES[n]()
    print(f'\n  {_checks} checks, {len(_failures)} failed, {len(_skips)} skipped')
    for f in _failures:
        print(f'    FAILED  {f}')
    for s in _skips:
        print(f'    SKIPPED {s}')
    return 1 if _failures else 0


if __name__ == '__main__':
    sys.exit(main())
