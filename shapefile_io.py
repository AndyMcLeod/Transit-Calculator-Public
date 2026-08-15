"""shapefile_io.py — ESRI Shapefile read/write, stdlib only.

WHY HAND-ROLLED. The obvious answer is pyshp/geopandas, but neither is installed and
this tool has to run from a cold Windows box at sea with nothing but CPython. The
shapefile spec (ESRI, July 1998) is small and stable: a 100-byte header, then
big-endian record headers wrapping little-endian geometry. The dBASE III attribute
table beside it is older still and just as fixed. Writing it out is ~200 lines, and
that beats a dependency the operator cannot install underway.

WHAT WE SUPPORT: Point (1), PolyLine (3), Polygon (5) on read; PolyLine and Point on
write. That covers a transit line and its waypoints, which is the whole job. Z and M
variants (11/13/15/21/23/25) are READ by taking their X/Y and discarding Z/M — a
line plan that arrived with elevations is still a usable transit — but we never
write them, because inventing a Z for a surface transit would be fiction.

THE NULL SHAPE IS NOT AN ERROR. The supplied survey_transit_line.shp carries a
record of type 0 after the real polyline, which is what QGIS leaves when a feature
is deleted from a layer without repacking. Readers that treat it as corruption
reject a perfectly good file; we skip it and count it, so the UI can say so.
"""

import io
import os
import struct
import zipfile

# Shape type codes
NULL, POINT, POLYLINE, POLYGON = 0, 1, 3, 5
POINTZ, POLYLINEZ, POLYGONZ = 11, 13, 15
POINTM, POLYLINEM, POLYGONM = 21, 23, 25

_HAS_Z = {POINTZ, POLYLINEZ, POLYGONZ}
_HAS_M = {POINTM, POLYLINEM, POLYGONM}
_LINEAR = {POLYLINE, POLYGON, POLYLINEZ, POLYGONZ, POLYLINEM, POLYGONM}
_PUNCTUAL = {POINT, POINTZ, POINTM}

WGS84_WKT = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",'
             '6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],'
             'UNIT["Degree",0.0174532925199433]]')


def utm_wkt(zone, hemisphere='N'):
    """ESRI-flavoured WKT for a WGS84 UTM zone — the dialect QGIS and ArcGIS both
    read from a .prj without complaint. Matches the supplied file's .prj byte for
    byte in structure, so a re-export lands in the same CRS it came from."""
    lon0 = (zone - 1) * 6 - 180 + 3
    fn = 0.0 if hemisphere.upper() == 'N' else 10000000.0
    return (f'PROJCS["WGS_1984_UTM_Zone_{zone}{hemisphere.upper()}",{WGS84_WKT},'
            f'PROJECTION["Transverse_Mercator"],'
            f'PARAMETER["False_Easting",500000.0],'
            f'PARAMETER["False_Northing",{fn}],'
            f'PARAMETER["Central_Meridian",{float(lon0)}],'
            f'PARAMETER["Scale_Factor",0.9996],'
            f'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')


# --------------------------------------------------------------------------- #
#  Reading                                                                     #
# --------------------------------------------------------------------------- #
class ShapeRecord:
    __slots__ = ('index', 'shape_type', 'parts', 'attrs')

    def __init__(self, index, shape_type, parts, attrs):
        self.index = index
        self.shape_type = shape_type
        self.parts = parts          # list of [(x, y), ...] — one entry per part
        self.attrs = attrs          # dict from the .dbf

    @property
    def points(self):
        """Every vertex, parts flattened. What a single-part transit line wants."""
        return [p for part in self.parts for p in part]


def read_shapefile(path):
    """Read a shapefile set. `path` may name the .shp or omit the extension.

    Returns a dict: shape_type, bbox, records (list[ShapeRecord]), fields, prj,
    null_count, and `crs` — a parsed {kind, zone, hemisphere, epsg} when the .prj
    is a recognisable WGS84 UTM or geographic definition, else kind='unknown'.
    """
    base = path[:-4] if path.lower().endswith('.shp') else path
    with open(base + '.shp', 'rb') as f:
        shp = f.read()

    if len(shp) < 100 or struct.unpack('>i', shp[0:4])[0] != 9994:
        raise ValueError('not a shapefile: bad file code (expected 9994)')
    declared = struct.unpack('>i', shp[24:28])[0] * 2
    file_type = struct.unpack('<i', shp[32:36])[0]
    bbox = struct.unpack('<4d', shp[36:68])
    # The header length is authoritative; a file padded by a transfer must not
    # have its padding parsed as a record.
    end = min(len(shp), declared) if declared >= 100 else len(shp)

    attrs = _read_dbf(base + '.dbf')
    prj = None
    try:
        with open(base + '.prj', 'r', encoding='utf-8-sig') as f:
            prj = f.read().strip()
    except OSError:
        pass

    records, null_count = [], 0
    off = 100
    while off + 8 <= end:
        rec_num, rec_len = struct.unpack('>ii', shp[off:off + 8])
        if rec_len <= 0:
            break
        body = shp[off + 8: off + 8 + rec_len * 2]
        off += 8 + rec_len * 2
        if len(body) < 4:
            break
        st = struct.unpack('<i', body[0:4])[0]
        idx = rec_num - 1
        row = attrs[idx] if idx < len(attrs) else {}
        if st == NULL:
            null_count += 1
            continue
        parts = _parse_geometry(st, body)
        if parts is None:
            null_count += 1
            continue
        records.append(ShapeRecord(idx, st, parts, row))

    return {
        'shape_type': file_type,
        'bbox': bbox,
        'records': records,
        'fields': _dbf_fields(base + '.dbf'),
        'prj': prj,
        'null_count': null_count,
        'crs': parse_prj(prj),
    }


def _parse_geometry(st, body):
    if st in _PUNCTUAL:
        x, y = struct.unpack('<2d', body[4:20])
        return [[(x, y)]]
    if st not in _LINEAR:
        return None
    n_parts, n_points = struct.unpack('<ii', body[36:44])
    if n_parts <= 0 or n_points <= 0:
        return None
    starts = struct.unpack(f'<{n_parts}i', body[44:44 + 4 * n_parts])
    po = 44 + 4 * n_parts
    pts = [struct.unpack('<2d', body[po + 16 * i: po + 16 * i + 16]) for i in range(n_points)]
    bounds = list(starts) + [n_points]
    return [pts[bounds[i]:bounds[i + 1]] for i in range(n_parts) if bounds[i] < bounds[i + 1]]


def _dbf_fields(path):
    try:
        with open(path, 'rb') as f:
            d = f.read()
    except OSError:
        return []
    fields, o = [], 32
    while o + 32 <= len(d) and d[o] != 0x0D:
        name = d[o:o + 11].split(b'\0')[0].decode('latin-1')
        fields.append({'name': name, 'type': chr(d[o + 11]),
                       'length': d[o + 16], 'decimals': d[o + 17]})
        o += 32
    return fields


def _read_dbf(path):
    """dBASE III attribute rows as dicts. Missing .dbf -> [] (geometry still reads).

    Deleted rows (leading '*') are kept but flagged `_deleted`: the record numbers
    in the .shp index into this table positionally, so dropping a row here would
    silently misalign every attribute after it.
    """
    try:
        with open(path, 'rb') as f:
            d = f.read()
    except OSError:
        return []
    if len(d) < 32:
        return []
    n_rec, hdr_len, rec_len = (struct.unpack('<i', d[4:8])[0],
                               struct.unpack('<H', d[8:10])[0],
                               struct.unpack('<H', d[10:12])[0])
    fields = _dbf_fields(path)
    rows = []
    for r in range(n_rec):
        ro = hdr_len + r * rec_len
        if ro + rec_len > len(d):
            break
        raw = d[ro:ro + rec_len]
        o, row = 1, {'_deleted': raw[0:1] == b'*'}
        for fl in fields:
            v = raw[o:o + fl['length']].decode('latin-1').strip()
            o += fl['length']
            if fl['type'] == 'N' or fl['type'] == 'F':
                # A padded-nulls numeric ('**********') is dBASE for "no value" —
                # QGIS writes it for an unset id. Keep it as None, not a crash.
                try:
                    v = float(v) if fl['decimals'] else int(v)
                except ValueError:
                    v = None
            elif fl['type'] == 'L':
                v = True if v in 'YyTt' else (False if v in 'NnFf' else None)
            row[fl['name']] = v
        rows.append(row)
    return rows


def parse_prj(prj):
    """Classify a .prj into something the reprojection seam can act on.

    Deliberately NOT a WKT parser — it recognises the two shapes that matter (a
    WGS84 UTM zone, or plain geographic lat/lon) and says 'unknown' otherwise.
    A half-understood CRS silently mis-placing a line on the chart is far worse
    than an honest refusal, so anything unrecognised is reported, not guessed.
    """
    if not prj:
        return {'kind': 'none'}
    t = prj.upper()
    if 'PROJCS' in t and 'TRANSVERSE_MERCATOR' in t:
        zone = hemi = None
        if 'UTM_ZONE_' in t:
            tail = t.split('UTM_ZONE_', 1)[1]
            num = ''
            for ch in tail:
                if ch.isdigit():
                    num += ch
                else:
                    hemi = 'S' if ch == 'S' else 'N'
                    break
            zone = int(num) if num else None
        if zone is None and 'CENTRAL_MERIDIAN' in t:
            try:
                cm = float(t.split('CENTRAL_MERIDIAN"', 1)[1].split(']')[0].lstrip(','))
                zone = int(round((cm + 183) / 6))
            except (ValueError, IndexError):
                pass
        if zone:
            # A southern-hemisphere zone is identified by its false northing, which
            # is the only difference in the projection parameters.
            if hemi is None:
                hemi = 'S' if 'FALSE_NORTHING",10000000' in t.replace(' ', '') else 'N'
            return {'kind': 'utm', 'zone': zone, 'hemisphere': hemi,
                    'epsg': (32600 if hemi == 'N' else 32700) + zone}
        return {'kind': 'unknown'}
    if 'GEOGCS' in t and 'PROJCS' not in t:
        return {'kind': 'geographic', 'epsg': 4326}
    return {'kind': 'unknown'}


# --------------------------------------------------------------------------- #
#  Writing                                                                     #
# --------------------------------------------------------------------------- #
def _dbf_bytes(fields, rows):
    """dBASE III table. `fields` = [(name, type, length, decimals), ...]."""
    hdr_len = 32 * (len(fields) + 1) + 1
    rec_len = 1 + sum(f[2] for f in fields)
    out = bytearray()
    # Version 3, no memo. The date is the dBASE epoch-relative (yy, mm, dd); we
    # write a fixed 2000-01-01 rather than today's date so the same line exports
    # byte-identically twice — a diffable artifact is worth more than a timestamp
    # nothing reads.
    out += struct.pack('<4B', 0x03, 100, 1, 1)
    out += struct.pack('<ihh', len(rows), hdr_len, rec_len)
    out += b'\x00' * 20
    for name, ftype, flen, dec in fields:
        nm = name.encode('latin-1')[:10]
        out += nm + b'\x00' * (11 - len(nm))
        out += ftype.encode('ascii')
        out += b'\x00' * 4
        out += struct.pack('<BB', flen, dec)
        out += b'\x00' * 14
    out += b'\x0D'
    for row in rows:
        out += b' '
        for name, ftype, flen, dec in fields:
            v = row.get(name, '')
            if v is None:
                s = ''
            elif ftype == 'N':
                s = f'{v:.{dec}f}' if dec else str(int(v))
            elif ftype == 'F':
                s = f'{float(v):.{dec}f}'
            elif ftype == 'L':
                s = 'T' if v else 'F'
            else:
                s = str(v)
            b = s.encode('latin-1', 'replace')[:flen]
            # Numerics right-align, text left-aligns — dBASE readers depend on it.
            out += (b.rjust(flen) if ftype in 'NF' else b.ljust(flen))
    out += b'\x1A'
    return bytes(out)


def write_shapefile(shapes, fields, rows, shape_type=POLYLINE):
    """Build a shapefile set in memory.

    `shapes`  — list of features; each is a list of parts, each part [(x,y), ...].
                A Point feature is [[(x, y)]].
    `fields`  — [(name, type, length, decimals), ...] for the .dbf.
    `rows`    — one dict per feature.
    Returns {'shp': bytes, 'shx': bytes, 'dbf': bytes}.

    The .shx is built alongside rather than derived afterwards: it is just the
    (offset, length) of every record in 16-bit words, and a shapefile whose index
    disagrees with its geometry opens as an empty layer in ArcGIS with no error.
    """
    if len(shapes) != len(rows):
        raise ValueError(f'{len(shapes)} shapes but {len(rows)} attribute rows')

    body, index = bytearray(), []
    xs, ys = [], []
    for i, parts in enumerate(shapes):
        pts = [p for part in parts for p in part]
        if not pts:
            rec = struct.pack('<i', NULL)
        elif shape_type == POINT:
            x, y = pts[0]
            rec = struct.pack('<i2d', POINT, x, y)
            xs.append(x)
            ys.append(y)
        else:
            px = [p[0] for p in pts]
            py = [p[1] for p in pts]
            xs += px
            ys += py
            starts, n = [], 0
            for part in parts:
                starts.append(n)
                n += len(part)
            rec = struct.pack('<i4d2i', shape_type, min(px), min(py), max(px), max(py),
                              len(parts), len(pts))
            rec += struct.pack(f'<{len(starts)}i', *starts)
            for x, y in pts:
                rec += struct.pack('<2d', x, y)
        offset_words = (100 + len(body)) // 2
        index.append((offset_words, len(rec) // 2))
        body += struct.pack('>ii', i + 1, len(rec) // 2) + rec

    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)

    def header(total_words):
        h = bytearray(struct.pack('>i', 9994) + b'\x00' * 20)
        h += struct.pack('>i', total_words)
        h += struct.pack('<ii', 1000, shape_type)
        h += struct.pack('<4d', *bbox)
        h += struct.pack('<4d', 0.0, 0.0, 0.0, 0.0)   # Z/M range: unused
        return bytes(h)

    shp = header((100 + len(body)) // 2) + bytes(body)
    shx_body = b''.join(struct.pack('>ii', o, l) for o, l in index)
    shx = header((100 + len(shx_body)) // 2) + shx_body
    return {'shp': shp, 'shx': shx, 'dbf': _dbf_bytes(fields, rows)}


def shapefile_zip(name, shapes, fields, rows, prj, shape_type=POLYLINE):
    """A complete, zipped shapefile — the only sane way to hand a 5-file format to
    a browser download. Includes .cpg so the encoding of any text attribute is
    unambiguous (QGIS guesses the system codepage without it)."""
    parts = write_shapefile(shapes, fields, rows, shape_type)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # Fixed timestamps keep the archive reproducible.
        for ext, data in (('shp', parts['shp']), ('shx', parts['shx']),
                          ('dbf', parts['dbf']),
                          ('prj', prj.encode('utf-8')), ('cpg', b'UTF-8')):
            zi = zipfile.ZipInfo(f'{name}.{ext}', date_time=(2000, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, data)
    return buf.getvalue()


def write_shapefile_files(directory, name, shapes, fields, rows, prj, shape_type=POLYLINE):
    """Same, written to disk as the five sidecar files."""
    parts = write_shapefile(shapes, fields, rows, shape_type)
    os.makedirs(directory, exist_ok=True)
    written = []
    for ext, data in (('shp', parts['shp']), ('shx', parts['shx']), ('dbf', parts['dbf']),
                      ('prj', prj.encode('utf-8')), ('cpg', b'UTF-8')):
        p = os.path.join(directory, f'{name}.{ext}')
        with open(p, 'wb') as f:
            f.write(data)
        written.append(p)
    return written
