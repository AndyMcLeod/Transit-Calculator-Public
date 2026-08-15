"""exporters.py — write a transit line out in the formats other tools actually read.

FIVE FORMATS, THREE AUDIENCES.
  shp / geojson   -> GIS (QGIS, ArcGIS). Shapefile because that is what the line
                     arrived as and what the survey world still runs on.
  kml / gpx       -> navigation. GPX routes load into a chartplotter or OpenCPN;
                     KML opens in Google Earth for a quick look.
  csv             -> the leg table, for a spreadsheet or a report.

THE CRS DECISION IS THE ONE THAT BITES. GeoJSON, KML and GPX are DEFINED as WGS84
lon/lat — RFC 7946 is explicit — so writing projected metres into them produces a
file that opens somewhere off the coast of Africa. Only the shapefile carries its own
.prj and can therefore hold UTM. So: everything is held internally in lat/lon, and
`utm_zone` is honoured for the SHAPEFILE ONLY. That is not an inconsistency, it is
the formats disagreeing, and the shapefile is the one that can express the choice.

Coordinate order is the other trap: GeoJSON and GPX/KML all want LONGITUDE FIRST
(x, y), while everything human-facing here is lat/lon. Each writer converts at its
own boundary rather than trusting a shared convention.
"""

import csv
import datetime as dt
import io
import json
import math
import xml.sax.saxutils as xu

import geo
import shapefile_io as sio

M_PER_NM = 1852.0


def _xml(s):
    return xu.escape(str(s if s is not None else ''))


def stamp_now():
    """UTC stamp for a filename: 20260814T0130Z."""
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%MZ')


def _stamped(base, stamp):
    """`base` with the stamp appended, or unchanged when there is none.

    THE STAMP IS PASSED IN, never read from the clock in here. Two reasons: every
    file of one export must carry the SAME stamp (a shapefile set whose .shp and
    .dbf disagree by a second is a broken set), and a caller that passes a fixed
    stamp gets byte-identical output, which is what lets the export be tested for
    reproducibility at all.
    """
    return f'{base}_{stamp}' if stamp else base


def _clean(name):
    """A filename safe on Windows and inside a zip."""
    keep = ''.join(c if (c.isalnum() or c in '-_ .') else '_' for c in (name or 'transit'))
    return (keep.strip().replace(' ', '_') or 'transit')[:64]


# --------------------------------------------------------------------------- #
#  GeoJSON                                                                     #
# --------------------------------------------------------------------------- #
def to_geojson(points, name='transit', legs=None, properties=None):
    """A LineString for the whole track, plus a Point per waypoint.

    Both in one FeatureCollection because that is what a reader wants to see when
    they drop it on a map — the track and the turns that define it. Leg attributes
    ride on the LineString; per-waypoint cumulative distance rides on the points.
    """
    coords = [[round(lon, 8), round(lat, 8)] for lat, lon in points]
    props = dict(properties or {})
    props.setdefault('name', name)
    summary = _summary(points)
    props.update({'distance_nm': round(summary['distance_nm'], 4),
                  'legs': len(points) - 1,
                  'straight_nm': round(summary['straight_nm'], 4)})
    features = [{'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': coords},
                 'properties': props}]
    cum = 0.0
    for i, (lat, lon) in enumerate(points):
        if i:
            cum += geo.inverse(points[i - 1][0], points[i - 1][1], lat, lon)[0]
        wp = {'name': f'WP{i + 1}', 'index': i, 'cumulative_nm': round(cum / M_PER_NM, 4)}
        if legs and i < len(legs):
            wp['bearing_out'] = round(legs[i].get('bearing', legs[i].get('bearing_start', 0.0)), 2)
        features.append({'type': 'Feature',
                         'geometry': {'type': 'Point', 'coordinates': [round(lon, 8), round(lat, 8)]},
                         'properties': wp})
    return json.dumps({'type': 'FeatureCollection',
                       # RFC 7946 fixes the CRS as WGS84 and REMOVED the crs member;
                       # naming it here as a comment, not a field, keeps strict
                       # parsers happy while leaving the fact recorded.
                       'features': features}, indent=1)


# --------------------------------------------------------------------------- #
#  KML                                                                         #
# --------------------------------------------------------------------------- #
def to_kml(points, name='transit', legs=None):
    coords = ' '.join(f'{lon:.8f},{lat:.8f},0' for lat, lon in points)
    marks = []
    cum = 0.0
    for i, (lat, lon) in enumerate(points):
        if i:
            cum += geo.inverse(points[i - 1][0], points[i - 1][1], lat, lon)[0]
        marks.append(
            f'    <Placemark><name>WP{i + 1}</name>'
            f'<description>{_xml(geo.fmt_pos(lat, lon))} — {cum / M_PER_NM:.2f} NM</description>'
            f'<Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point></Placemark>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
 <Document>
  <name>{_xml(name)}</name>
  <Style id="track"><LineStyle><color>ff00a5ff</color><width>3</width></LineStyle></Style>
  <Placemark>
   <name>{_xml(name)}</name>
   <styleUrl>#track</styleUrl>
   <LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString>
  </Placemark>
  <Folder><name>Waypoints</name>
{chr(10).join(marks)}
  </Folder>
 </Document>
</kml>
'''


# --------------------------------------------------------------------------- #
#  GPX                                                                         #
# --------------------------------------------------------------------------- #
def to_gpx(points, name='transit'):
    """GPX 1.1 with BOTH a <rte> and a <trk>.

    Chartplotters differ on which they will follow: a route is a plan to navigate,
    a track is a record of where you went. Writing both means the file is useful
    whichever the receiving device expects, and neither costs anything.
    """
    rte = '\n'.join(f'  <rtept lat="{lat:.8f}" lon="{lon:.8f}"><name>WP{i + 1}</name></rtept>'
                    for i, (lat, lon) in enumerate(points))
    trk = '\n'.join(f'    <trkpt lat="{lat:.8f}" lon="{lon:.8f}"/>' for lat, lon in points)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Transit Calculator" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">
 <metadata><name>{_xml(name)}</name></metadata>
 <rte>
  <name>{_xml(name)}</name>
{rte}
 </rte>
 <trk>
  <name>{_xml(name)}</name>
  <trkseg>
{trk}
  </trkseg>
 </trk>
</gpx>
'''


# --------------------------------------------------------------------------- #
#  CSV                                                                         #
# --------------------------------------------------------------------------- #
def to_csv_waypoints(points):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['index', 'name', 'latitude', 'longitude', 'lat_dm', 'lon_dm',
                'leg_nm', 'cumulative_nm', 'bearing_true'])
    cum = 0.0
    for i, (lat, lon) in enumerate(points):
        leg_nm = brg = ''
        if i:
            d, b, _ = geo.inverse(points[i - 1][0], points[i - 1][1], lat, lon)
            cum += d
            leg_nm = f'{d / M_PER_NM:.4f}'
        if i < len(points) - 1:
            brg = f'{geo.inverse(lat, lon, points[i + 1][0], points[i + 1][1])[1]:.2f}'
        w.writerow([i + 1, f'WP{i + 1}', f'{lat:.8f}', f'{lon:.8f}',
                    geo.fmt_lat(lat), geo.fmt_lon(lon), leg_nm, f'{cum / M_PER_NM:.4f}', brg])
    return buf.getvalue()


def to_csv_legs(plan):
    """The computed transit table — the thing you paste into a report."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['leg', 'from_lat', 'from_lon', 'to_lat', 'to_lon', 'distance_nm',
                'bearing_true', 'sog_kt', 'stw_kt', 'set_deg', 'drift_kt', 'along_kt',
                'rpm', 'hours', 'hhmm', 'litres', 'sea_state', 'wind_kt', 'wind_from',
                'current_coverage_pct', 'eta_utc', 'note'])
    for l in plan['legs']:
        w.writerow([
            l['index'] + 1, f"{l['from']['lat']:.8f}", f"{l['from']['lon']:.8f}",
            f"{l['to']['lat']:.8f}", f"{l['to']['lon']:.8f}",
            f"{l['distance_nm']:.4f}", f"{l.get('bearing_start', 0):.2f}",
            f"{l['sog_kt']:.3f}", f"{l['stw_kt']:.3f}",
            '' if l['set_deg'] is None else f"{l['set_deg']:.1f}",
            f"{l['drift_kt']:.3f}", f"{l['along_kt']:+.3f}", f"{l['rpm']:.0f}",
            f"{l['hours']:.4f}", l['hhmm'], f"{l['litres']:.3f}",
            '' if l.get('sea_state') is None else l['sea_state'],
            '' if l.get('wind_kt') is None else f"{l['wind_kt']:.1f}",
            '' if l.get('wind_from_deg') is None else f"{l['wind_from_deg']:.0f}",
            f"{l['current_coverage'] * 100:.0f}", l.get('eta_utc') or '', l.get('note') or ''])
    w.writerow([])
    w.writerow(['TOTAL', '', '', '', '', f"{plan['distance_nm']:.4f}", '',
                f"{plan['avg_sog_kt']:.3f}", '', '', '', '', '',
                f"{plan['hours']:.4f}", plan['hhmm'], f"{plan['litres']:.3f}"])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
#  Shapefile                                                                   #
# --------------------------------------------------------------------------- #
def to_shapefile_zip(points, name='transit', utm_zone=None, hemisphere=None,
                     geometry='line', legs=None, stamp=None):
    """Zipped shapefile of the line (or its waypoints).

    `utm_zone=None` writes geographic lat/lon with a WGS84 .prj. Give a zone to
    write projected metres instead — which is what the supplied file used, and what
    a survey package usually expects back.

    ONE ZONE FOR THE WHOLE LINE. The zone is chosen from the line's MIDPOINT and
    forced on every vertex, because a shapefile carries exactly one .prj: letting
    each vertex pick its own zone would silently scatter a line that crosses a
    boundary across two coordinate systems under a header claiming one.
    """
    # Stamp the BASE, so the members inside the zip are stamped as well. If only
    # the archive were stamped, two exports would both extract to transit.shp and
    # the second would clobber the first in the same folder -- which is exactly the
    # accident the stamp exists to prevent.
    nm = _stamped(_clean(name), stamp)
    if utm_zone:
        hemi = (hemisphere or ('N' if points[0][0] >= 0 else 'S')).upper()
        xy = [tuple(geo.ll_to_utm(lat, lon, utm_zone, hemi)[:2]) for lat, lon in points]
        prj = sio.utm_wkt(utm_zone, hemi)
    else:
        xy = [(lon, lat) for lat, lon in points]     # shapefile is (x, y) = (lon, lat)
        prj = sio.WGS84_WKT

    if geometry == 'point':
        cum, shapes, rows = 0.0, [], []
        for i, (lat, lon) in enumerate(points):
            if i:
                cum += geo.inverse(points[i - 1][0], points[i - 1][1], lat, lon)[0]
            shapes.append([[xy[i]]])
            rows.append({'id': i + 1, 'name': f'WP{i + 1}', 'lat': lat, 'lon': lon,
                         'cum_nm': cum / M_PER_NM})
        fields = [('id', 'N', 10, 0), ('name', 'C', 20, 0),
                  ('lat', 'N', 19, 9), ('lon', 'N', 19, 9), ('cum_nm', 'N', 12, 4)]
        return sio.shapefile_zip(nm, shapes, fields, rows, prj, shape_type=sio.POINT), nm

    s = _summary(points)
    fields = [('id', 'N', 10, 0), ('name', 'C', 40, 0),
              ('dist_nm', 'N', 14, 4), ('legs', 'N', 10, 0)]
    rows = [{'id': 1, 'name': name, 'dist_nm': s['distance_nm'], 'legs': len(points) - 1}]
    return sio.shapefile_zip(nm, [[xy]], fields, rows, prj, shape_type=sio.POLYLINE), nm


def legs_to_shapefile_zip(plan, name='transit_legs', utm_zone=None, hemisphere=None,
                          stamp=None):
    """One polyline feature PER LEG, carrying that leg's computed attributes.

    This is the export that makes the calculation portable: the GIS user gets the
    speed, set, drift, fuel and ETA attached to the geometry they can see, instead
    of a single line and a separate table to join by hand.
    """
    nm = _stamped(_clean(name), stamp)
    pts = [(l['from']['lat'], l['from']['lon']) for l in plan['legs']]
    pts.append((plan['legs'][-1]['to']['lat'], plan['legs'][-1]['to']['lon']))
    if utm_zone:
        hemi = (hemisphere or ('N' if pts[0][0] >= 0 else 'S')).upper()
        def px(lat, lon):
            return tuple(geo.ll_to_utm(lat, lon, utm_zone, hemi)[:2])
        prj = sio.utm_wkt(utm_zone, hemi)
    else:
        def px(lat, lon):
            return (lon, lat)
        prj = sio.WGS84_WKT

    fields = [('leg', 'N', 10, 0), ('dist_nm', 'N', 14, 4), ('bearing', 'N', 8, 2),
              ('sog_kt', 'N', 10, 3), ('stw_kt', 'N', 10, 3), ('set_deg', 'N', 8, 2),
              ('drift_kt', 'N', 10, 3), ('along_kt', 'N', 10, 3), ('rpm', 'N', 10, 0),
              ('hours', 'N', 12, 4), ('litres', 'N', 12, 3), ('sea_state', 'N', 5, 0),
              ('wind_kt', 'N', 8, 1), ('cur_cov', 'N', 6, 2), ('eta_utc', 'C', 24, 0)]
    shapes, rows = [], []
    for l in plan['legs']:
        shapes.append([[px(l['from']['lat'], l['from']['lon']),
                        px(l['to']['lat'], l['to']['lon'])]])
        rows.append({
            'leg': l['index'] + 1, 'dist_nm': l['distance_nm'],
            'bearing': l.get('bearing_start', 0.0), 'sog_kt': l['sog_kt'],
            'stw_kt': l['stw_kt'],
            'set_deg': 0.0 if l['set_deg'] is None else l['set_deg'],
            'drift_kt': l['drift_kt'], 'along_kt': l['along_kt'], 'rpm': l['rpm'],
            'hours': l['hours'], 'litres': l['litres'],
            'sea_state': l.get('sea_state') if l.get('sea_state') is not None else 0,
            'wind_kt': l.get('wind_kt') or 0.0,
            'cur_cov': l['current_coverage'], 'eta_utc': (l.get('eta_utc') or '')[:24]})
    return sio.shapefile_zip(nm, shapes, fields, rows, prj, shape_type=sio.POLYLINE), nm


def _summary(points):
    total = geo.path_length_m(points)
    straight = geo.inverse(points[0][0], points[0][1], points[-1][0], points[-1][1])[0]
    return {'distance_nm': total / M_PER_NM, 'straight_nm': straight / M_PER_NM}


# --------------------------------------------------------------------------- #
#  Dispatch                                                                    #
# --------------------------------------------------------------------------- #
#  (bytes, filename, content_type) for every supported format.
def export(fmt, points, name='transit', plan=None, utm_zone=None, hemisphere=None,
           geometry='line', stamp=None):
    fmt = (fmt or '').lower()
    nm = _stamped(_clean(name), stamp)
    if fmt == 'geojson':
        return to_geojson(points, name).encode('utf-8'), f'{nm}.geojson', 'application/geo+json'
    if fmt == 'kml':
        return to_kml(points, name).encode('utf-8'), f'{nm}.kml', \
            'application/vnd.google-earth.kml+xml'
    if fmt == 'gpx':
        return to_gpx(points, name).encode('utf-8'), f'{nm}.gpx', 'application/gpx+xml'
    if fmt == 'csv':
        return to_csv_waypoints(points).encode('utf-8'), f'{nm}_waypoints.csv', 'text/csv'
    if fmt == 'csv_legs':
        if not plan:
            raise ValueError('csv_legs needs a computed plan')
        return to_csv_legs(plan).encode('utf-8'), f'{nm}_legs.csv', 'text/csv'
    if fmt == 'shp':
        data, base = to_shapefile_zip(points, name, utm_zone, hemisphere, geometry,
                                     stamp=stamp)
        return data, f'{base}_shp.zip', 'application/zip'
    if fmt == 'shp_legs':
        if not plan:
            raise ValueError('shp_legs needs a computed plan')
        data, base = legs_to_shapefile_zip(plan, name + '_legs', utm_zone, hemisphere,
                                          stamp=stamp)
        return data, f'{base}_shp.zip', 'application/zip'
    raise ValueError(f'unknown format {fmt!r}')


FORMATS = [
    {'key': 'shp', 'label': 'Shapefile (.zip)', 'note': 'polyline + .prj/.dbf/.shx/.cpg'},
    {'key': 'shp_legs', 'label': 'Shapefile — per leg (.zip)', 'note': 'one feature per leg, with results'},
    {'key': 'geojson', 'label': 'GeoJSON', 'note': 'WGS84 lon/lat, track + waypoints'},
    {'key': 'kml', 'label': 'KML', 'note': 'Google Earth'},
    {'key': 'gpx', 'label': 'GPX', 'note': 'route + track, for a chartplotter'},
    {'key': 'csv', 'label': 'CSV — waypoints', 'note': 'lat/lon, leg and cumulative NM'},
    {'key': 'csv_legs', 'label': 'CSV — leg table', 'note': 'the computed transit'},
]
