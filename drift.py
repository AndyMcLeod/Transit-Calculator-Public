"""drift.py — where an unpowered the vessel goes, and how well that can be known.

THIS ANSWERS A DIFFERENT QUESTION FROM transit.py. A transit is a vessel holding a
line it chose; a drift is a hull going wherever the water and the wind take it. The
line is an output, there is no speed to set, and the useful product is not a track —
it is a DATUM AND A RADIUS, because a single predicted position for a drifting object
hours out is a number with no honest error bar and search effort spent on it is search
effort wasted.

THE TWO TERMS, AND WHAT IS KNOWN ABOUT EACH:

  ADVECTION — the hull goes with the water. This rests on the same NOAA forecast the
  transit uses, at the position reached and the clock time it was reached, and it is
  the term we have real data for.

  LEEWAY — the wind pushes what stands out of the water. This is NOT MEASURED FOR
  THIS VESSEL. Published leeway coefficients exist for the SAR object classes, a vessel
  is not one of them, and nothing here is a substitute for a drift trial. It is
  carried as a STATED RANGE that the operator can change, and every result says which
  range produced it.

WHY THE TIDE MOSTLY CANCELS. A tidal current reverses about every six hours, so a
hull adrift in one traces an ellipse and creeps. Over a day the displacement is
dominated by the RESIDUAL flow and the wind, not by the peak current — which is why a
drift prediction is far more sensitive to a small steady error than a transit ETA is,
and why the ensemble below varies the current magnitude rather than trusting it.

THE ENSEMBLE IS A LATTICE, NOT A SAMPLE. Members are the corners and centre of the
stated uncertainty ranges, walked deterministically. Two runs of the same input give
the same answer, which a random sample cannot promise — and an emergency is the worst
possible moment to discover that the datum moved because a seed changed.

THE HORIZON IS A REFUSAL. The cached forecast covers its own span plus three tidal
cycles of projection, and the domain has a hard edge the hull will drift over. Past
either, this does not extrapolate: it reports the time it stopped being able to
answer and why. A drift track that keeps drawing past its own data is the single most
dangerous thing this module could do.
"""

import datetime as dt
import math

import geo

M_PER_NM = 1852.0
DEFAULT_STEP_MIN = 15.0

# ---------------------------------------------------------------------------- #
#  LEEWAY — the USCG / Breivik formulation, not a percentage of the wind         #
# ---------------------------------------------------------------------------- #
# The first version of this module used "leeway is 1-3% of wind speed at +/-20
# degrees", which was engineering judgement. The published model is better in three
# ways that matter, and none of them is the magnitude:
#
#   1. There is an OFFSET. Several classes drift at a measured rate in NO wind.
#   2. CROSSWIND IS ITS OWN REGRESSION with separate left and right coefficients,
#      not a fixed rotation of the downwind vector. The effective divergence angle
#      therefore falls out of the object, and varies with wind speed.
#   3. THE SPREAD IS MEASURED. `std` is the standard error of the field regression,
#      and the ensemble is built from it instead of from a guessed range. Breivik
#      notes these coefficient perturbations dominate the search-area growth — far
#      more than any random walk added to the wind.
#
#       DWL = (dw_slope + eps/20) * W10 + dw_offset + eps/2     [cm/s]
#       CWL = (cw_slope + eps/20) * W10 + cw_offset + eps/2     [cm/s]
#
# W10 is the wind at 10 m IN METRES PER SECOND; slope is a percentage, offset and
# eps are cm/s. This tool speaks knots everywhere else, so the conversion happens at
# the seam below and nowhere else — mixing the two is the obvious way to get this
# wrong by a factor of two and not notice.
#
# LEEWAY IS DEFINED RELATIVE TO THE CURRENT AT ABOUT 0.5 m DEPTH — that is how the
# field experiments measured it, and operational implementations interpolate their
# model current to that depth for consistency. Adding leeway on top of a near-surface
# forecast current is therefore the correct convention, NOT a double count. An
# earlier version of this file claimed the opposite and suppressed the leeway to
# compensate; that was wrong.
#
# Coefficients: Allen & Plourde 1999 (USCG R&D Center, CG-D-08-99) and Allen 2005,
# as tabulated in the OBJECTPROP.DAT of the LEEWAY model. US Government work. THE
# EQUATIONS AND THE COEFFICIENTS ARE USED HERE; NO CODE WAS TAKEN FROM OPENDRIFT,
# which is GPLv2 and would carry its licence into a repository that gets exported
# publicly.
# LEFT AND RIGHT ARE SEPARATE ROWS IN THE PUBLISHED TABLE, and they are not always
# mirror images. PIW-1 is symmetric (+0.54 / -0.54), which is why an earlier version
# of this file got away with storing one crosswind triple and flipping its sign. The
# Navy sub-escape raft is not: its crosswind has NO slope at all and a constant
# offset of +5.7 cm/s to the right against -3.4 to the left, with a different
# standard error either side. Carrying both rows as published is the only way to
# hold a class like that without quietly symmetrising it.
#
# Sign convention, as in the source table: POSITIVE crosswind is to the RIGHT of
# downwind, negative to the left. The left row therefore usually carries negative
# numbers, and the code applies THAT SIGN rather than an orientation flag.
LEEWAY_CLASSES = {
    'piw-1': {
        'name': 'Person in water, unknown state',
        'dw': (0.96, 0.00, 12.00),      # slope %, offset cm/s, std cm/s
        'cw_right': (0.54, 0.00, 9.40),
        'cw_left': (-0.54, 0.00, 9.40),
    },
    'sldmb': {
        'name': 'Self-locating datum marker buoy — no windage',
        'dw': (0.00, 0.00, 0.00),
        'cw_right': (0.00, 0.00, 0.00),
        'cw_left': (0.00, 0.00, 0.00),
    },
    'seie-drogue': {
        'name': 'Navy sub-escape (SEIE) 1-man raft, with drogue',
        'dw': (1.70, -3.90, 4.20),
        'cw_right': (0.00, 5.70, 3.30),
        'cw_left': (0.00, -3.40, 2.20),
    },
    'seie-no-drogue': {
        'name': 'Navy sub-escape (SEIE) 1-man raft, no drogue',
        'dw': (3.30, -3.90, 4.20),
        'cw_right': (0.50, 7.00, 5.70),
        'cw_left': (0.10, -6.20, 3.60),
    },
    'kayak': {
        'name': 'Sea kayak with person on aft deck',
        'dw': (1.16, 11.12, 4.12),
        'cw_right': (0.41, 0.00, 4.39),
        'cw_left': (-0.41, 0.00, 4.39),
    },
    'skiff-swamped': {
        'name': 'Skiff, swamped and capsized',
        'dw': (1.65, 0.00, 3.10),
        'cw_right': (0.39, 0.00, 2.90),
        'cw_left': (-0.39, 0.00, 2.90),
    },
    'surfboard': {
        'name': 'Surfboard with person',
        'dw': (1.93, 0.00, 8.30),
        'cw_right': (0.51, 0.00, 6.70),
        'cw_left': (-0.51, 0.00, 6.70),
    },
    'skiff-runabout': {
        'name': 'Skiff, modified-V cathedral-hull runabout',
        'dw': (3.15, 0.00, 2.20),
        'cw_right': (1.29, 0.00, 2.20),
        'cw_left': (-1.29, 0.00, 2.20),
    },
}

# THE VESSEL IS NOT IN THE TAXONOMY. None of the 85 published classes is an unmanned
# surface vessel, so the default is an analogue either way. The one chosen is NOT a
# guess from hull shape: **Andy reports the Navy sub-escape raft with a drogue has
# been found to closely match a THE VESSEL in past operations** (2026-08-14). That is
# operational experience rather than a published measurement of this vessel, and it
# is recorded as exactly that — but it beats the alternative, which was my own
# reasoning from freeboard and sail area.
#
# It is also physically coherent: a drogued raft is a small object with modest
# windage tied to a large underwater drag element, which is close to what a hull
# floating low with a deep config does. Note what the class brings with it — a
# crosswind with NO wind dependence, an asymmetric one at that, and a downwind
# offset that is NEGATIVE, so in light airs it is measured drifting slightly behind
# the water rather than with it.
#
# The spread is much tighter than the person-in-water class considered first —
# 4.2 cm/s against 12.0 — so the search radius it produces is substantially smaller.
# That is only justified because the choice rests on experience with this hull; a
# tighter circle on a worse-grounded class would be the wrong trade entirely.
#
# A DRIFT TRIAL WOULD STILL REPLACE ALL OF THIS. An afternoon with the boat adrift, a
# GPS log and a known wind produces real coefficients, and it is the single
# highest-value change available to this module.
DEFAULT_CLASS = 'seie-drogue'

MS_PER_KT = 0.5144444
KT_PER_CMS = 0.0194384

# Quantiles of the coefficient error, as z-scores, spanning the 5th to the 95th
# percentile: the envelope radius therefore means "if each coefficient is within its
# measured 5-95% band". Note what is NOT here — the intermediate quartiles. THE
# RADIUS IS A MAXIMUM OVER MEMBERS, so interior points cannot widen it; they cost
# time and move nothing. Measured: five points per axis is 150 members and 21 s for a
# three-day run, three points is 54 members and 9 s, and the radius came out IDENTICAL
# to a tenth of a mile at both 24 and 72 hours.
EPS_Z = (-1.645, 0.0, 1.645)

# How wrong the current itself may be. The forecast has no published skill figure we
# can cite at this scale, so this brackets it rather than pretending to know it. The
# projection path has a measured one — currents.PROJECTION_ACCURACY — and this is
# deliberately wider, because that figure covers borrowing a tide, not the model's
# own error.
CURRENT_SCALE = 0.25


# How far off a position the forecast may be consulted when the point itself has no
# model water. A grid has dry nodes, and a hull sitting a few hundred metres off one
# is not outside the forecast — the first test position of this module, in the mouth
# of Delaware Bay, read back nothing while a point 0.02° away read 0.84 kt. Bounded,
# because past a couple of kilometres the answer stops being about this hull: that is
# a domain exit, and it must be counted as one rather than papered over.
NUDGE_KM = 2.0
_NUDGE_RING = [(km, brg) for km in (0.5, 1.0, 2.0) for brg in range(0, 360, 45)]


def time_horizon(currents):
    """The last instant the cached forecast can answer for, or None if unknown.

    Cycles cover their own span and `currents.at_best` will borrow across whole
    tidal cycles beyond it, up to a hard cap. Past that the source returns nothing
    — the same empty answer it gives for a position outside its domain, which is
    why this exists: RUNNING OUT OF FORECAST AND DRIFTING OFF THE EDGE OF ONE ARE
    DIFFERENT PROBLEMS WITH DIFFERENT FIXES, and an operator told only "no data"
    cannot tell which of the two to go and solve.
    """
    try:
        import currents as currents_mod
        ends = [c[2].end for c in getattr(currents, 'cycles', [])]
        if not ends:
            end = getattr(currents, 'end', None)
            if end is None:
                return None
            ends = [end]
        return max(ends) + dt.timedelta(
            hours=currents_mod.MAX_PROJECT_CYCLES * currents_mod.M2_PERIOD_H)
    except Exception:
        return None


def _current_at(currents, lat, lon, when):
    """(set, drift, quality, nudged_km) — the forecast at or beside a position.

    NOT MEMOISED, and that was measured rather than assumed. Caching these on a
    ~550 m grid looked obviously right — 150 members over the same water — and hit
    only 27% of the time, because the members diverge faster than the cell size.
    Building the key cost more than the hits saved and the whole prediction got
    twice as slow. The lattice below is where the time actually went.
    """
    s, d, q = currents.query(lat, lon, when)
    if q is not None:
        return s, d, q, 0.0
    for km, brg in _NUDGE_RING:
        plat, plon = geo.direct(lat, lon, float(brg), km * 1000.0)
        s, d, q = currents.query(plat, plon, when)
        if q is not None:
            return s, d, q, km
    return None, None, None, None


def leeway_components(wind_kt, cls, dw_eps=0.0, cw_eps=0.0, right=True):
    """(downwind_kt, crosswind_kt) for a wind speed on one tack.

    The published equations, with the unit conversion done once. Crosswind is
    SIGNED — positive to the right of downwind — and comes from that tack's own
    row, because the two are not always mirror images.

    THE DOWNWIND COMPONENT CAN COME OUT NEGATIVE at the low tail of a class with a
    large standard error — PIW-1 at -1.645 sigma does exactly that, and the Navy
    raft has a negative offset that does it in light airs. It is not clamped: the
    tail is part of the measured distribution, and clipping it would quietly shift
    the whole ensemble downwind and shrink the search area on the strength of an
    assumption nobody made.
    """
    dw_slope, dw_offset, _ = cls['dw']
    cw_slope, cw_offset, _ = cls['cw_right' if right else 'cw_left']
    w_ms = max(0.0, wind_kt) * MS_PER_KT
    dw_cms = (dw_slope + dw_eps / 20.0) * w_ms + dw_offset + dw_eps / 2.0
    cw_cms = (cw_slope + cw_eps / 20.0) * w_ms + cw_offset + cw_eps / 2.0
    return dw_cms * KT_PER_CMS, cw_cms * KT_PER_CMS


def _leeway_vector(wind_kt, wind_from_deg, cls, dw_eps, cw_eps, right):
    """(speed_kt, toward_deg) the wind contributes.

    Wind direction is the direction it blows FROM; the downwind component goes
    TOWARD the reciprocal. Getting that reciprocal wrong reverses the entire wind
    term, which on a multi-day drift is the difference between the beach and the
    Gulf Stream — so it is asserted directly in the suite and mutation-guarded.

    `right` is the object's orientation relative to downwind, which the field work
    found is set at the outset and essentially unpredictable: the ensemble carries
    both, equally, and holds each member's choice for the whole run.
    """
    if wind_from_deg is None:
        return 0.0, 0.0
    dw_kt, cw_kt = leeway_components(wind_kt or 0.0, cls, dw_eps, cw_eps, right)
    if dw_kt == 0.0 and cw_kt == 0.0:
        return 0.0, 0.0
    downwind = (wind_from_deg + 180.0) % 360.0
    # The crosswind is SIGNED, so the direction comes from its sign and not from the
    # tack: the Navy raft's left row is a negative offset with a zero slope, and
    # forcing it to port by flag would put it on the wrong side of the wind.
    cross = (downwind + (90.0 if cw_kt >= 0 else -90.0)) % 360.0
    return _vector_sum(dw_kt, downwind, abs(cw_kt), cross)


GRAVITY = 9.80665
# Wave steepness ceiling for the Stokes calculation. `ak` above about 0.44 is
# breaking; real seas sit near 0.1, and past that the height and the period disagree
# — a buoy reporting an old Hs against a fresh period, or two sources interpolated
# together. Stokes goes as the SQUARE of steepness, so bad data does not give a
# slightly wrong answer, it invents a knot of drift.
STEEPNESS_MAX = 0.10


def stokes_drift(hs_m, period_s, wave_from_deg, wind_from_deg=None, wind_kt=None):
    """(speed_kt, toward_deg, note) — wave transport the leeway cannot already hold.

    THE WHOLE POINT IS WHAT GETS SUBTRACTED. Leeway coefficients were measured in the
    field with seas running, so the wave forcing that CORRELATES WITH THE LOCAL WIND
    is already inside them. Adding a full Stokes term on top would count that twice.
    The along-wind component is therefore projected out and only what remains — swell
    across or against the wind — is returned. An old swell on a calm morning survives
    in full; a wind sea running with the breeze contributes nothing.

    Magnitude is the deep-water narrow-band result, Us = a^2 * omega * k, with the
    amplitude taken from the ENERGY: a = Hs / (2*sqrt(2)). Using Hs/2 instead, which
    looks like the obvious choice, overstates a spectrum by a factor of two. Narrow
    band suits SWELL, which is what survives the projection; it understates a broad
    wind sea, which is the part being removed anyway.

    Deep water is assumed. Shallower than about half a wavelength this understates
    the transport, and this tool has no bathymetry with which to know.
    """
    if not hs_m or not period_s or wave_from_deg is None:
        return 0.0, 0.0, 'no wave data'
    if hs_m <= 0 or period_s <= 0:
        return 0.0, 0.0, 'no wave data'
    omega = 2.0 * math.pi / period_s
    k = omega * omega / GRAVITY
    a = hs_m / (2.0 * math.sqrt(2.0))
    note = None
    steep = a * k
    if steep > STEEPNESS_MAX:
        a = STEEPNESS_MAX / k
        note = (f'wave steepness {steep:.2f} over {STEEPNESS_MAX:g} — the height and '
                f'period disagree, so the Stokes term was capped')
    us_kt = (a * a * omega * k) / MS_PER_KT
    toward = (wave_from_deg + 180.0) % 360.0

    if wind_from_deg is None or not wind_kt:
        # No wind to attribute any of it to, so none of it can already be inside the
        # leeway: by definition the whole term is swell.
        return us_kt, toward, note

    # Project out the DOWNWIND part, keep the rest. A swell running against the wind
    # has a negative along-wind component, which no leeway coefficient can account
    # for, so it survives in full — that is the case this term exists for.
    downwind = math.radians((wind_from_deg + 180.0) % 360.0)
    wx, wy = math.sin(downwind), math.cos(downwind)
    r = math.radians(toward)
    sx, sy = us_kt * math.sin(r), us_kt * math.cos(r)
    along = sx * wx + sy * wy
    if along > 0:
        sx -= along * wx
        sy -= along * wy
    left = math.hypot(sx, sy)
    if left <= 1e-12:
        return 0.0, 0.0, 'wave transport is wholly downwind — already in the leeway'
    return left, math.degrees(math.atan2(sx, sy)) % 360.0, note


def _vector_sum(a_kt, a_deg, b_kt, b_deg):
    """Two speed/direction pairs into one. Directions are TOWARD, degrees true."""
    ax = a_kt * math.sin(math.radians(a_deg))
    ay = a_kt * math.cos(math.radians(a_deg))
    bx = b_kt * math.sin(math.radians(b_deg))
    by = b_kt * math.cos(math.radians(b_deg))
    x, y = ax + bx, ay + by
    return math.hypot(x, y), math.degrees(math.atan2(x, y)) % 360.0


def _field_at(weather, lat, lon):
    """Wind AND wave at a position — a MarineField asked by position, or one dict
    held constant.

    HELD CONSTANT IS AN ASSUMPTION THAT GETS WORSE BY THE HOUR. A forecast for the
    moment of the casualty says little about the second night. The caller is told to
    say so; this function only resolves what it was given.
    """
    if weather is None:
        return {}
    f = weather.at(lat, lon) if hasattr(weather, 'at') else weather
    return f or {}


def _one_member(lat0, lon0, start, hours, currents, weather,
                cls, dw_eps, cw_eps, right, current_scale, step_min):
    """March one set of assumptions forward. Returns (track, stats)."""
    step_h = step_min / 60.0
    n = max(1, int(math.ceil(hours / step_h)))
    t_end = time_horizon(currents)
    lat, lon = lat0, lon0
    track = [{'t': 0.0, 'lat': lat, 'lon': lon,
              'utc': start.isoformat().replace('+00:00', 'Z')}]
    covered = 0
    steps = 0
    nudged = 0
    stokes_nm = 0.0
    stokes_notes = []
    horizon_h = None
    horizon_why = None

    for k in range(n):
        when = start + dt.timedelta(hours=k * step_h)
        set_deg, drift_kt, quality, off = _current_at(currents, lat, lon, when)
        steps += 1
        if quality is None:
            # NO COVERAGE IS NOT SLACK WATER — the same rule the transit marcher
            # runs by. Here it is sharper: a drift computed with a missing current
            # does not merely lose accuracy, it stops being a drift prediction. The
            # first step without data ends the confident part of the track.
            set_deg, drift_kt = 0.0, 0.0
            if horizon_h is None:
                horizon_h = k * step_h
                if t_end is not None and when > t_end:
                    horizon_why = ('the cached forecast ran out — fetch a newer '
                                   'cycle to see further')
                elif steps > 1:
                    horizon_why = ('the hull drifted out of every forecast domain '
                                   '— fetch a model that covers where it went')
                else:
                    horizon_why = 'no forecast reaches the last known position'
        else:
            covered += 1
            if off:
                nudged += 1

        f = _field_at(weather, lat, lon)
        w_kt, w_from = f.get('wind_speed_kt'), f.get('wind_from_deg')
        lee_kt, lee_deg = _leeway_vector(w_kt, w_from, cls, dw_eps, cw_eps, right)
        v_kt, v_deg = _vector_sum(drift_kt * current_scale, set_deg or 0.0,
                                  lee_kt, lee_deg)
        # SWELL ONLY. The wind-driven part of the wave transport is already inside
        # the leeway coefficients; stokes_drift subtracts it and returns what is
        # left. Not perturbed across the ensemble: it is an order of magnitude below
        # the leeway spread, and inventing an uncertainty for it would dress a guess
        # as a measurement. Said in the assumptions instead.
        st_kt, st_deg, st_note = stokes_drift(
            f.get('wave_height_m'), f.get('wave_period_s'), f.get('wave_from_deg'),
            w_from, w_kt)
        if st_kt > 0:
            stokes_nm += st_kt * step_h
            if st_note and st_note not in stokes_notes:
                stokes_notes.append(st_note)
            v_kt, v_deg = _vector_sum(v_kt, v_deg, st_kt, st_deg)
        if v_kt > 0:
            lat, lon = geo.direct(lat, lon, v_deg, v_kt * step_h * M_PER_NM)
        track.append({'t': (k + 1) * step_h, 'lat': lat, 'lon': lon,
                      'utc': (start + dt.timedelta(hours=(k + 1) * step_h))
                             .isoformat().replace('+00:00', 'Z')})

    return track, {'covered_fraction': covered / steps if steps else 0.0,
                   'nudged_fraction': nudged / steps if steps else 0.0,
                   'stokes_nm': stokes_nm, 'stokes_notes': stokes_notes,
                   'horizon_h': horizon_h, 'horizon_why': horizon_why}


def _at_hour(track, h):
    """The member's position at elapsed hour `h`, by nearest recorded step."""
    best = min(track, key=lambda p: abs(p['t'] - h))
    return best['lat'], best['lon']


def predict(lat, lon, start, hours, currents, weather=None,
            leeway_class=DEFAULT_CLASS, current_scale=CURRENT_SCALE,
            step_min=DEFAULT_STEP_MIN, report_every_h=1.0):
    """Where an unpowered hull goes from (lat, lon) at `start`, for `hours`.

    Returns the nominal track, the ensemble envelope per reporting hour, and the
    horizon past which the data ran out.

    THE RADIUS IS WHAT THE MEASURED COEFFICIENT SPREAD IMPLIES, not a probability.
    It says: with the leeway coefficients anywhere inside their measured 5-95%
    band, either orientation relative to the wind, and the current within the
    stated scale, the hull is inside this circle. It is not a confidence interval
    and must not be passed on as one.
    """
    if hours <= 0:
        raise ValueError('drift duration must be positive')
    if currents is None:
        raise ValueError('a drift prediction needs a current source')
    if leeway_class not in LEEWAY_CLASSES:
        raise ValueError(f'unknown leeway class {leeway_class!r}; have '
                         f'{", ".join(sorted(LEEWAY_CLASSES))}')
    cls = LEEWAY_CLASSES[leeway_class]
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)

    # REFUSE AT THE DOOR IF THE START IS NOT IN ANY FORECAST. Everything below would
    # otherwise run happily and return a wind-only track dressed in the same
    # formatting as a real one — the worst possible failure for this module, because
    # it fails towards confidence. The nudge above already forgives a dry grid node.
    _, _, q0, _ = _current_at(currents, lat, lon, start)
    if q0 is None:
        raise ValueError(
            f'no current forecast reaches {lat:.4f}, {lon:.4f} — '
            f'nothing within {NUDGE_KM:g} km of it is model water. Fetch a model '
            f'that covers this position before predicting a drift from it.')

    # THE LATTICE, not a Monte Carlo draw. The published method samples the
    # coefficient errors randomly; this walks their quantiles instead, so two runs
    # of the same input give the same datum. In an emergency that is worth more
    # than the extra realism of a random sample, and with a small ensemble the
    # quantiles cover the band more evenly than a few hundred draws would.
    #
    # ORIENTATION IS SPLIT EQUALLY AND HELD. The field work found which side of
    # downwind an object takes up is set at the outset and is essentially
    # unpredictable, so both are carried the whole way rather than averaged.
    dw_std = cls['dw'][2]
    # Each tack has its own standard error, so the lattice takes the wider of the
    # two — using each side's own would give the two halves of the ensemble
    # different spans and a lopsided envelope for a reason nobody asked for.
    cw_std = max(cls['cw_right'][2], cls['cw_left'][2])
    dw_eps = sorted({round(z * dw_std, 6) for z in EPS_Z})
    cw_eps = sorted({round(z * cw_std, 6) for z in EPS_Z})
    scales = sorted({1.0 - current_scale, 1.0, 1.0 + current_scale})

    members = []
    for right in (True, False):
        for de in dw_eps:
            for ce in cw_eps:
                for s in scales:
                    track, stats = _one_member(lat, lon, start, hours, currents,
                                               weather, cls, de, ce, right, s,
                                               step_min)
                    members.append({'orientation': 'right' if right else 'left',
                                    'dw_eps': de, 'cw_eps': ce,
                                    'current_scale': s, 'track': track, **stats})

    # The NOMINAL member is every coefficient at its central value — the answer you
    # would get with no ensemble at all. It is the datum precisely so that the radius
    # beside it is visibly the cost of the measured uncertainty, not a second model.
    # Orientation has no central value, so the nominal takes the right-hand tack and
    # the left-hand one sits in the envelope with everything else.
    nominal = next(m for m in members
                   if m['orientation'] == 'right' and m['dw_eps'] == 0.0
                   and m['cw_eps'] == 0.0 and m['current_scale'] == 1.0)

    envelope = []
    h = report_every_h
    while h <= hours + 1e-9:
        dlat, dlon = _at_hour(nominal['track'], h)
        far = 0.0
        for m in members:
            mlat, mlon = _at_hour(m['track'], h)
            far = max(far, geo.inverse(dlat, dlon, mlat, mlon)[0])
        envelope.append({
            'hours': h,
            'utc': (start + dt.timedelta(hours=h)).isoformat().replace('+00:00', 'Z'),
            'lat': dlat, 'lon': dlon,
            'radius_nm': far / M_PER_NM,
            'from_start_nm': geo.inverse(lat, lon, dlat, dlon)[0] / M_PER_NM,
            'bearing_from_start': geo.inverse(lat, lon, dlat, dlon)[1],
        })
        h += report_every_h

    # `is not None`, NOT truthiness. A horizon of 0.0 hours reported as "no horizon"
    # was a real bug here — the most severe answer this can give, rendered as the
    # safest — found by running the thing against a dry grid node. THE DOOR REFUSAL
    # ABOVE NOW MAKES 0.0 UNREACHABLE, so this guard is belt and braces rather than
    # load-bearing, and a mutation of it cannot be caught. Kept deliberately: the
    # refusal is the thing under test, and if it is ever relaxed this is what stops
    # the old bug coming back with it.
    horizons = [m['horizon_h'] for m in members if m['horizon_h'] is not None]
    horizon_h = min(horizons) if horizons else None
    why = next((m['horizon_why'] for m in members
                if m['horizon_h'] == horizon_h), None) if horizons else None

    return {
        'start': {'lat': lat, 'lon': lon,
                  'utc': start.isoformat().replace('+00:00', 'Z')},
        'hours': hours,
        'step_min': step_min,
        'track': nominal['track'],
        'members': [{'orientation': m['orientation'], 'dw_eps': m['dw_eps'],
                     'cw_eps': m['cw_eps'], 'current_scale': m['current_scale'],
                     'end': m['track'][-1]} for m in members],
        'member_tracks': [m['track'] for m in members],
        'envelope': envelope,
        'datum': envelope[-1] if envelope else None,
        'leeway': {
            'key': leeway_class, 'name': cls['name'],
            'dw_slope_pct': cls['dw'][0], 'dw_offset_cms': cls['dw'][1],
            'dw_std_cms': cls['dw'][2],
            'cw_right': list(cls['cw_right']), 'cw_left': list(cls['cw_left']),
            'cw_slope_pct': cls['cw_right'][0],
            'cw_std_cms': max(cls['cw_right'][2], cls['cw_left'][2]),
            'measured_for_this_vessel': False,
            'source': 'Allen & Plourde 1999 (USCG CG-D-08-99) / Allen 2005, as '
                      'tabulated for the LEEWAY model',
            'why_this_class': ('the sub-escape raft with a drogue has been found to '
                               'closely match this vessel in past operations — '
                               'experience, not a drift trial'
                               if leeway_class == 'seie-drogue' else
                               'chosen by the operator from the published classes'),
        },
        'current_scale': current_scale,
        'current_source': getattr(currents, 'tag', None),
        'covered_fraction': min(m['covered_fraction'] for m in members),
        'nudged_fraction': max(m['nudged_fraction'] for m in members),
        # How far the swell-only Stokes term carried the nominal member, so its
        # contribution can be judged against the radius rather than taken on faith.
        'stokes_nm': nominal['stokes_nm'],
        'stokes_notes': sorted({n for m in members for n in m['stokes_notes']}),
        'horizon_h': horizon_h,
        'horizon_utc': (start + dt.timedelta(hours=horizon_h)
                        ).isoformat().replace('+00:00', 'Z')
                       if horizon_h is not None else None,
        'horizon_why': why,
        # Said in the result rather than left to the reader, because the reader of
        # this one may be coordinating a search at three in the morning.
        'assumptions': [
            'Leeway uses the published coefficients for "{}" — {:.2f}% of wind '
            'speed downwind, ±{:.2f}% crosswind. NO CLASS EXISTS FOR THIS VESSEL '
            'and none of these was measured on it.'.format(
                cls['name'], cls['dw'][0], cls['cw_right'][0]),
            'Both orientations relative to the wind are carried, equally, for the '
            'whole run — which side an object takes is set at the outset and is not '
            'predictable.',
            'The coefficient errors are walked across their measured 5–95% band '
            '({:.1f} cm/s downwind, {:.1f} cm/s crosswind), which is where most of '
            'the radius comes from.'.format(cls['dw'][2],
                                            max(cls['cw_right'][2],
                                                cls['cw_left'][2])),
            'Current magnitude varied ±{:.0f}% around the forecast.'.format(
                current_scale * 100),
            'Wind is held as given for the whole prediction unless a field was '
            'supplied; a forecast at the moment of loss says little about tomorrow.',
            'Wave transport is included as a SWELL-ONLY Stokes term: the component '
            'running with the wind is projected out, because the leeway '
            'coefficients were measured with seas running and already contain it. '
            'Deep water is assumed, and the term is not perturbed across the '
            'ensemble.',
            'Leeway is defined relative to the current at about 0.5 m depth, which '
            'is what the forecast is being read as here.',
            'The radius is what that spread implies, not a probability.',
        ],
    }
