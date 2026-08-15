"""geo.py — the geodesy layer for the Transit Calculator.

THE PUREST LAYER: no I/O, no state, no imports beyond `math`. Value in, value out.
That is what lets tests/ call the real functions instead of re-deriving them.

WHY VINCENTY AND NOT THE CONSOLE'S FLAT EARTH. The ASV console's static/js/geodesy.js
uses an equirectangular approximation (M_PER_DEG_LAT * cos(mean lat)), which is exact
enough over a survey box a few km across and keeps the routing maths readable. A
transit is a different animal: the supplied Lewes line runs 78 NM and one leg alone is
42 NM. Over that span the flat-earth distance is wrong by tens of metres and the
bearing by a few tenths of a degree — small, but this is the number the fuel and ETA
hang off, so we pay for the ellipsoid here. `flat_dist_m` is kept alongside ONLY so
tests can measure the divergence and prove the choice mattered.

Azimuth convention: 0 = N, 90 = E, clockwise — matching the console, so a bearing
copied from one tool to the other means the same thing.
"""

import math

# WGS84 defining parameters
A = 6378137.0                     # semi-major axis, m
F = 1 / 298.257223563             # flattening
B = A * (1 - F)                   # semi-minor axis, m
E2 = F * (2 - F)                  # first eccentricity squared
EP2 = E2 / (1 - E2)               # second eccentricity squared

M_PER_NM = 1852.0
MS_TO_KT = 1.9438444924406046

# UTM
K0 = 0.9996
FALSE_EASTING = 500000.0
FALSE_NORTHING_N = 0.0
FALSE_NORTHING_S = 10000000.0


# --------------------------------------------------------------------------- #
#  Ellipsoidal geodesics                                                       #
# --------------------------------------------------------------------------- #
def inverse(lat1, lon1, lat2, lon2):
    """Vincenty inverse: (distance_m, initial_bearing_deg, final_bearing_deg).

    Returns bearings in [0,360). Coincident points give (0, 0, 0) rather than a
    NaN out of atan2(0,0) — a zero-length leg is legitimate in a drawn line where
    the operator double-clicked, and it must not poison the whole table.

    THE THIRD VALUE IS THE FORWARD AZIMUTH AT THE DESTINATION — the course still
    being steered on arrival. Geoscience Australia's published Vincenty test vector
    lists its alpha2 as the REVERSE azimuth (destination back to origin), which is
    this value minus 180. Verified against that vector: distance agrees to 0.14 mm
    and both azimuths to 0.003 arcsec once the convention is lined up. If you are
    checking this function against a published table, check which alpha2 it means
    before concluding it is broken.

    Vincenty fails to converge on near-antipodal pairs. That cannot arise from a
    transit line, but rather than return a silently wrong number we fall back to
    the spherical haversine and the caller sees a slightly coarse value instead of
    a confident lie.
    """
    if lat1 == lat2 and lon1 == lon2:
        return 0.0, 0.0, 0.0

    L = math.radians(lon2 - lon1)
    U1 = math.atan((1 - F) * math.tan(math.radians(lat1)))
    U2 = math.atan((1 - F) * math.tan(math.radians(lat2)))
    sU1, cU1 = math.sin(U1), math.cos(U1)
    sU2, cU2 = math.sin(U2), math.cos(U2)

    lam = L
    sin_sigma = cos_sigma = sigma = cos2_alpha = cos_2sigma_m = 0.0
    converged = False
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cU2 * sl, cU1 * sU2 - sU1 * cU2 * cl)
        if sin_sigma == 0.0:
            return 0.0, 0.0, 0.0
        cos_sigma = sU1 * sU2 + cU1 * cU2 * cl
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cU1 * cU2 * sl / sin_sigma
        cos2_alpha = 1 - sin_alpha * sin_alpha
        # cos2_alpha == 0 on an equatorial line; the C term vanishes with it.
        cos_2sigma_m = 0.0 if cos2_alpha == 0 else cos_sigma - 2 * sU1 * sU2 / cos2_alpha
        C = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
        lam_prev = lam
        lam = L + (1 - C) * F * sin_alpha * (
            sigma + C * sin_sigma * (cos_2sigma_m + C * cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)))
        if abs(lam - lam_prev) < 1e-12:
            converged = True
            break

    if not converged:
        return haversine(lat1, lon1, lat2, lon2), bearing_sphere(lat1, lon1, lat2, lon2), \
               (bearing_sphere(lat2, lon2, lat1, lon1) + 180.0) % 360.0

    u2 = cos2_alpha * (A * A - B * B) / (B * B)
    Aa = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    Bb = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    d_sigma = Bb * sin_sigma * (
        cos_2sigma_m + Bb / 4 * (cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)
                                 - Bb / 6 * cos_2sigma_m * (-3 + 4 * sin_sigma ** 2)
                                 * (-3 + 4 * cos_2sigma_m ** 2)))
    s = B * Aa * (sigma - d_sigma)

    sl, cl = math.sin(lam), math.cos(lam)
    a1 = math.degrees(math.atan2(cU2 * sl, cU1 * sU2 - sU1 * cU2 * cl)) % 360.0
    a2 = math.degrees(math.atan2(cU1 * sl, -sU1 * cU2 + cU1 * sU2 * cl)) % 360.0
    return s, a1, a2


def direct(lat, lon, bearing_deg, distance_m):
    """Vincenty direct: travel distance_m from (lat,lon) on bearing. -> (lat2, lon2)."""
    if distance_m == 0:
        return lat, lon
    a1 = math.radians(bearing_deg)
    s_a1, c_a1 = math.sin(a1), math.cos(a1)
    U1 = math.atan((1 - F) * math.tan(math.radians(lat)))
    sU1, cU1 = math.sin(U1), math.cos(U1)
    # sigma1 = angular distance from the equator to the start point on the great
    # circle: atan2(tan U1, cos alpha1). Written via sU1/cU1 to reuse the sines
    # already computed; cU1 is zero only at the poles, which a transit never sees.
    sigma1 = math.atan2(sU1, cU1 * c_a1)
    sin_alpha = cU1 * s_a1
    cos2_alpha = 1 - sin_alpha * sin_alpha
    u2 = cos2_alpha * (A * A - B * B) / (B * B)
    Aa = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    Bb = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))

    sigma = distance_m / (B * Aa)
    for _ in range(200):
        cos_2sigma_m = math.cos(2 * sigma1 + sigma)
        ss, cs = math.sin(sigma), math.cos(sigma)
        d_sigma = Bb * ss * (cos_2sigma_m + Bb / 4 * (cs * (-1 + 2 * cos_2sigma_m ** 2)
                             - Bb / 6 * cos_2sigma_m * (-3 + 4 * ss ** 2) * (-3 + 4 * cos_2sigma_m ** 2)))
        sigma_prev = sigma
        sigma = distance_m / (B * Aa) + d_sigma
        if abs(sigma - sigma_prev) < 1e-12:
            break

    cos_2sigma_m = math.cos(2 * sigma1 + sigma)
    ss, cs = math.sin(sigma), math.cos(sigma)
    lat2 = math.atan2(sU1 * cs + cU1 * ss * c_a1,
                      (1 - F) * math.hypot(sin_alpha, sU1 * ss - cU1 * cs * c_a1))
    lam = math.atan2(ss * s_a1, cU1 * cs - sU1 * ss * c_a1)
    C = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
    L = lam - (1 - C) * F * sin_alpha * (
        sigma + C * ss * (cos_2sigma_m + C * cs * (-1 + 2 * cos_2sigma_m ** 2)))
    return math.degrees(lat2), lon + math.degrees(L)


def haversine(lat1, lon1, lat2, lon2, radius=6371008.8):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def bearing_sphere(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def flat_dist_m(lat1, lon1, lat2, lon2):
    """The console's equirectangular distance. Present so tests can PROVE the
    ellipsoid was worth importing — not used by the transit maths."""
    m_per_deg_lat = 111320.0
    mlon = m_per_deg_lat * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * m_per_deg_lat, (lon2 - lon1) * mlon)


def path_length_m(points):
    """Total geodesic length of a [(lat,lon), ...] polyline."""
    return sum(inverse(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])[0]
               for i in range(len(points) - 1))


# --------------------------------------------------------------------------- #
#  UTM <-> geographic (WGS84)                                                  #
# --------------------------------------------------------------------------- #
# The supplied survey line arrives as UTM 18N eastings/northings; everything the
# chart, the currents and the fuel model speak is lat/lon. These two are the seam.
# Snyder's series (USGS PP-1395), accurate to well under a millimetre inside a zone.
def utm_zone_for(lat, lon):
    """Zone number and hemisphere for a position, including the Norway/Svalbard
    exceptions — a transit crossing into them would otherwise get a zone that does
    not exist in the EPSG registry."""
    zone = int((lon + 180) / 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    elif 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    return zone, ('N' if lat >= 0 else 'S')


def utm_to_ll(easting, northing, zone, hemisphere='N'):
    """UTM -> (lat, lon) degrees, WGS84."""
    x = (easting - FALSE_EASTING) / K0
    y = (northing - (FALSE_NORTHING_N if hemisphere.upper() == 'N' else FALSE_NORTHING_S)) / K0
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)

    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    mu = y / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))

    C1 = EP2 * math.cos(phi1) ** 2
    T1 = math.tan(phi1) ** 2
    N1 = A / math.sqrt(1 - E2 * math.sin(phi1) ** 2)
    R1 = A * (1 - E2) / (1 - E2 * math.sin(phi1) ** 2) ** 1.5
    D = x / N1

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D ** 2 / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2 - 9 * EP2) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 ** 2 - 252 * EP2 - 3 * C1 ** 2) * D ** 6 / 720)
    lon = lon0 + (D
                  - (1 + 2 * T1 + C1) * D ** 3 / 6
                  + (5 - 2 * C1 + 28 * T1 - 3 * C1 ** 2 + 8 * EP2 + 24 * T1 ** 2) * D ** 5 / 120
                  ) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)


def ll_to_utm(lat, lon, zone=None, hemisphere=None):
    """(lat, lon) -> (easting, northing, zone, hemisphere), WGS84.

    `zone` may be forced so every vertex of one line lands in a single zone: a
    transit crossing a zone boundary must NOT have half its vertices renumbered,
    or the exported shapefile is nonsense under its own .prj.
    """
    if zone is None:
        zone, auto_hemi = utm_zone_for(lat, lon)
        hemisphere = hemisphere or auto_hemi
    hemisphere = (hemisphere or ('N' if lat >= 0 else 'S')).upper()

    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    p, l = math.radians(lat), math.radians(lon)
    N = A / math.sqrt(1 - E2 * math.sin(p) ** 2)
    T = math.tan(p) ** 2
    C = EP2 * math.cos(p) ** 2
    Aa = math.cos(p) * (l - lon0)
    M = A * ((1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256) * p
             - (3 * E2 / 8 + 3 * E2 ** 2 / 32 + 45 * E2 ** 3 / 1024) * math.sin(2 * p)
             + (15 * E2 ** 2 / 256 + 45 * E2 ** 3 / 1024) * math.sin(4 * p)
             - (35 * E2 ** 3 / 3072) * math.sin(6 * p))

    easting = K0 * N * (Aa + (1 - T + C) * Aa ** 3 / 6
                        + (5 - 18 * T + T ** 2 + 72 * C - 58 * EP2) * Aa ** 5 / 120) + FALSE_EASTING
    northing = K0 * (M + N * math.tan(p) * (
        Aa ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * Aa ** 4 / 24
        + (61 - 58 * T + T ** 2 + 600 * C - 330 * EP2) * Aa ** 6 / 720))
    if hemisphere == 'S':
        northing += FALSE_NORTHING_S
    return easting, northing, zone, hemisphere


# --------------------------------------------------------------------------- #
#  Set and drift                                                               #
# --------------------------------------------------------------------------- #
def sog_from_stw(stw_kt, course_deg, set_deg, drift_kt):
    """Speed made good over ground when steering `course` at `stw`, in a current
    setting TOWARD `set_deg` at `drift_kt`.

    Returns (sog_kt, cog_deg, along_kt, cross_kt) where `along` is the component
    of current helping (+) or opposing (-) the intended course, and `cross` is the
    component pushing off track (+ to starboard).

    NOTE this is the SIMPLE vector sum — the boat holds its HEADING and accepts the
    set. It is the honest answer for "where do I end up", and `along_kt` is what
    the fuel maths wants. Crabbing to hold the track is a different question and is
    answered by `stw_to_hold_track`.
    """
    cr, sr = math.radians(course_deg), math.radians(set_deg)
    # x = east, y = north
    vx = stw_kt * math.sin(cr) + drift_kt * math.sin(sr)
    vy = stw_kt * math.cos(cr) + drift_kt * math.cos(sr)
    sog = math.hypot(vx, vy)
    cog = math.degrees(math.atan2(vx, vy)) % 360.0
    rel = math.radians(set_deg - course_deg)
    return sog, cog, drift_kt * math.cos(rel), drift_kt * math.sin(rel)


def stw_to_hold_track(course_deg, set_deg, drift_kt, sog_target_kt=None, stw_kt=None):
    """Crab angle and resulting speed for holding the ground track exactly.

    Given EITHER a speed through water (`stw_kt`) or a desired speed over ground,
    returns (heading_deg, sog_kt, crab_deg) or None when the current is too strong
    to hold the track at all — which is a real refusal, not a zero. A drift that
    exceeds the boat's water speed on a beam set cannot be crabbed out, and
    returning some plausible number there would hide a transit that cannot be flown.
    """
    rel = math.radians(set_deg - course_deg)
    cross = drift_kt * math.sin(rel)       # must be cancelled by the crab
    along = drift_kt * math.cos(rel)
    if stw_kt is not None:
        if abs(cross) > stw_kt:
            return None                     # cannot cancel the cross-set
        crab = math.degrees(math.asin(-cross / stw_kt)) if stw_kt else 0.0
        sog = stw_kt * math.cos(math.radians(crab)) + along
        # THE THRESHOLD IS NOT COSMETIC. At exactly cross == stw the boat crabs a
        # full 90 degrees and makes no ground at all, but cos(90 deg) in IEEE-754 is
        # 6.1e-17 rather than 0, so a bare `sog <= 0` let this through as a positive
        # speed. A leg at 5e-16 kt takes ~1e16 hours: not a slow passage, a hang and
        # an absurd ETA. Anything at or below floating-point noise is no transit.
        if sog <= 1e-9:
            return None                     # held track, but making no ground
        return (course_deg + crab) % 360.0, sog, crab
    if sog_target_kt is None:
        raise ValueError('give either stw_kt or sog_target_kt')
    need_along = sog_target_kt - along
    if need_along <= 0:
        return course_deg, sog_target_kt, 0.0
    stw = math.hypot(need_along, cross)
    crab = math.degrees(math.atan2(-cross, need_along))
    return (course_deg + crab) % 360.0, sog_target_kt, crab


def uv_to_set(u_ms, v_ms):
    """Eastward/northward velocity (m/s) -> (set_deg_toward, drift_kt).

    Oceanographic convention: `set` is the direction the water is going TOWARD,
    which is the opposite of the meteorological convention for wind. Getting this
    backwards silently flips every current in the table, so it lives in one place.
    """
    drift = math.hypot(u_ms, v_ms) * MS_TO_KT
    if drift == 0:
        return 0.0, 0.0
    return math.degrees(math.atan2(u_ms, v_ms)) % 360.0, drift


# --------------------------------------------------------------------------- #
#  Formatting                                                                  #
# --------------------------------------------------------------------------- #
def fmt_lat(lat, places=4):
    h = 'N' if lat >= 0 else 'S'
    v = abs(lat)
    d = int(v)
    return f"{d:02d}°{(v - d) * 60:0{places + 3}.{places}f}'{h}"


def fmt_lon(lon, places=4):
    h = 'E' if lon >= 0 else 'W'
    v = abs(lon)
    d = int(v)
    return f"{d:03d}°{(v - d) * 60:0{places + 3}.{places}f}'{h}"


def fmt_pos(lat, lon, places=4):
    return f'{fmt_lat(lat, places)}  {fmt_lon(lon, places)}'


def hm(hours):
    """Decimal hours -> 'h:mm'. Rounds to the minute, carrying 59.7 -> 1:00 rather
    than 0:60."""
    total = int(round(hours * 60))
    return f'{total // 60}:{total % 60:02d}'
