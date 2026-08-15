"""transit.py — the calculation itself: time, speed, current, fuel along a line.

WHY THIS MARCHES INSTEAD OF MULTIPLYING. The naive transit calculator divides
distance by speed. That is wrong here for a reason that matters on this coast: the
current is a tidal current, it reverses roughly every six hours, and a 78 NM transit
takes about ten. The boat that leaves Lewes on a fair ebb meets the flood before it
is halfway. Evaluating the current once — at the start, or at the leg midpoint —
answers a question nobody asked.

So we MARCH: step along the line in short increments, and at each step look up the
current at the position the boat has actually reached and the CLOCK TIME it reached
it. Time advances by the speed that step's current allowed, which changes where the
boat is at the next lookup. It is a simple forward integration, and it is the whole
difference between a plausible number and a usable one.

THE BOAT FOLLOWS THE LINE. It does not drift off it. A transit that is being planned
against a drawn track holds that track, which means crabbing into any cross-set — so
the speed made good along the line is what `geo.stw_to_hold_track` returns, and the
heading is not the course. When the cross-set exceeds what the boat can crab out,
that is a REFUSAL, not a slow leg: the line cannot be flown as drawn and the result
says so.

NO COVERAGE IS NOT NO CURRENT. Where the forecast grid does not reach — and on the
supplied Lewes line it does not reach the last few miles — the step is computed with
zero current AND COUNTED. Every result carries the fraction of its distance that had
real data. A zero silently standing in for a gap is the exact failure this comment
exists to prevent.
"""

import datetime as dt
import math

import geo

M_PER_NM = 1852.0
DEFAULT_STEP_NM = 0.5


class CurrentSource:
    """Adapter over the vendored `currents.Currents` with an honest miss.

    `query(lat, lon, when)` returns (set_deg, drift_kt, quality) where quality is
    'measured' (a real frame), 'projected' (borrowed across whole tidal cycles by
    at_best), or None when there is no answer at all. The caller must treat None as
    a gap, never as slack water.
    """

    def __init__(self, cycle=None, allow_projection=True):
        self.cycle = cycle
        self.allow_projection = allow_projection
        self.tag = getattr(cycle, 'tag', None)

    def query(self, lat, lon, when):
        if self.cycle is None:
            return None, None, None
        try:
            r = self.cycle.at(lat, lon, when)
            if r is not None:
                return r[1], r[0], 'measured'
        except ValueError:
            # Outside the cached time span. at_best can project by tidal cycles.
            pass
        except Exception:
            return None, None, None
        if not self.allow_projection:
            return None, None, None
        try:
            best = self.cycle.at_best(lat, lon, when)
        except Exception:
            return None, None, None
        if not best or best[0] is None:
            return None, None, None
        vals, shift = best[0], best[1]
        return vals[1], vals[0], ('measured' if abs(shift or 0) < 1e-9 else 'projected')


class NullCurrents(CurrentSource):
    def __init__(self):
        super().__init__(None)


def _leg_weather(weather, index):
    """Per-leg weather: a MarineField (resolved per step), one dict for the whole
    passage, or a list with an entry per leg."""
    if weather is None:
        return {}
    # A MarineField is not indexable and has no length — it answers by POSITION, so
    # it passes straight through to the marcher, which asks it at every step.
    if isinstance(weather, MarineField):
        return weather
    if not weather:
        return {}
    if isinstance(weather, dict):
        return weather
    if index < len(weather):
        return weather[index] or {}
    return weather[-1] or {}


class MarineField:
    """Wind and sea state interpolated at the STEP, not assigned per leg.

    Wraps a bag of scattered marine samples (buoys + grid nodes) and answers at any
    position by IDW. This is what makes weather continuous over the full range of a
    line: a 42 NM leg crosses a real wind gradient, and one value stamped on the
    whole leg throws that away — on the supplied line the wind runs 2 kt at Lewes to
    8 kt at the southern end, which is the difference between two sea states.

    Values are cached on a coarse grid because the interpolation is smooth and the
    marcher asks hundreds of times along one leg; recomputing per step would be
    exact to no useful extra decimal.
    """

    def __init__(self, samples, sea_table=None, cache_deg=0.02):
        self.samples = samples or []
        self.sea_table = sea_table
        self.cache_deg = cache_deg
        self._cache = {}

    def at(self, lat, lon):
        if not self.samples:
            return {}
        key = (round(lat / self.cache_deg), round(lon / self.cache_deg))
        hit = self._cache.get(key)
        if hit is None:
            import marine
            hit = marine.field_at(self.samples, lat, lon, sea_table=self.sea_table)
            self._cache[key] = hit
        return hit


def plan(points, speed_kt, departure=None, mode='stw', config='config_a',
         currents=None, weather=None, model=None, step_nm=DEFAULT_STEP_NM,
         capacity_l=None, reserve_fraction=None, onboard_l=None, rpm=None):
    """Compute a transit along `points` = [(lat, lon), ...].

    mode='stw'  — `speed_kt` is speed THROUGH THE WATER (a throttle setting). The
                  current then decides speed over ground and therefore the clock.
    mode='sog'  — `speed_kt` is the ground speed to be MADE GOOD. The current then
                  decides the water speed, the RPM and therefore the fuel. Use when
                  the arrival time is fixed and you want its cost.
    mode='rpm'  — `rpm` is the THROTTLE, held. The vessel model decides the speed
                  through the water and the current decides the clock, so weather
                  costs TIME here rather than fuel: litres per hour is fixed by the
                  revs. This is what an ASV holding revs actually does, and it is
                  not the same as mode='stw' unless the vessel's own controller
                  agrees with this model about which revs give which speed.

    Returns a dict with per-leg rows, a total, and the provenance of every input.
    """
    if len(points) < 2:
        raise ValueError('a transit needs at least two points')
    if mode not in ('stw', 'sog', 'rpm'):
        raise ValueError("mode must be 'stw', 'sog' or 'rpm'")
    if mode == 'rpm':
        # The revs are the input in this mode, so they get the validation the speed
        # gets in the others, and speed_kt is ignored rather than quietly consulted.
        if model is None:
            raise ValueError('fixed-rpm mode needs a vessel model')
        if not rpm or rpm <= 0:
            raise ValueError('rpm must be positive')
    elif speed_kt <= 0:
        raise ValueError('speed must be positive')

    src = currents or NullCurrents()
    departure = departure or dt.datetime.now(dt.timezone.utc)
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=dt.timezone.utc)

    legs = []
    clock = 0.0                      # hours since departure
    total_litres = 0.0
    total_m = 0.0
    infeasible = []

    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        dist_m, brg1, brg2 = geo.inverse(a[0], a[1], b[0], b[1])
        wx = _leg_weather(weather, i)

        if dist_m <= 0:
            legs.append(_zero_leg(i, a, b, brg1, departure, clock))
            continue

        leg = _march(a, b, dist_m, brg1, speed_kt, mode, config, src, wx,
                     model, step_nm, departure, clock, rpm)
        leg['index'] = i
        leg['from'] = {'lat': a[0], 'lon': a[1]}
        leg['to'] = {'lat': b[0], 'lon': b[1]}
        leg['bearing_start'] = brg1
        leg['bearing_end'] = brg2
        if leg['infeasible']:
            infeasible.append(i + 1)
        clock += leg['hours']
        total_litres += leg['litres']
        total_m += dist_m
        legs.append(leg)

    covered_m = sum(l['current_covered_m'] for l in legs)
    projected_m = sum(l['current_projected_m'] for l in legs)
    result = {
        'legs': legs,
        'mode': mode,
        'speed_kt': speed_kt,
        'rpm_command': rpm if mode == 'rpm' else None,
        'config': config,
        'departure_utc': departure.isoformat().replace('+00:00', 'Z'),
        'arrival_utc': (departure + dt.timedelta(hours=clock)).isoformat().replace('+00:00', 'Z'),
        'distance_m': total_m,
        'distance_nm': total_m / M_PER_NM,
        'hours': clock,
        'hhmm': geo.hm(clock),
        'litres': total_litres,
        'avg_sog_kt': (total_m / M_PER_NM / clock) if clock > 0 else 0.0,
        'current': {
            'source': src.tag,
            'covered_fraction': (covered_m / total_m) if total_m else 0.0,
            'projected_fraction': (projected_m / total_m) if total_m else 0.0,
            'gap_nm': max(0.0, total_m - covered_m) / M_PER_NM,
        },
        'infeasible_legs': infeasible,
        'feasible': not infeasible,
    }
    if model is not None:
        result['endurance'] = model.endurance(total_litres, capacity_l,
                                              reserve_fraction, onboard_l)
    return result


def _zero_leg(i, a, b, brg, departure, clock):
    """A zero-length leg — the operator double-clicked. Kept in the table (dropping
    it would renumber every leg after it and break the map/table correspondence)
    but contributes nothing."""
    return {
        'index': i, 'from': {'lat': a[0], 'lon': a[1]}, 'to': {'lat': b[0], 'lon': b[1]},
        'bearing_start': brg, 'bearing_end': brg, 'distance_m': 0.0, 'distance_nm': 0.0,
        'hours': 0.0, 'hhmm': '0:00', 'litres': 0.0, 'sog_kt': 0.0, 'stw_kt': 0.0,
        'set_deg': None, 'drift_kt': 0.0, 'along_kt': 0.0, 'cross_kt': 0.0,
        'rpm': 0.0, 'rate_lph': 0.0, 'heading_deg': brg, 'crab_deg': 0.0,
        'current_covered_m': 0.0, 'current_projected_m': 0.0, 'current_coverage': 1.0,
        'in_fit_window': True, 'infeasible': False, 'note': 'zero-length leg',
        'eta_utc': None, 'sea_state': None, 'wind_kt': None, 'wind_from_deg': None,
        'wave_m': None, 'premium': 0.0,
    }


def _march(a, b, dist_m, brg, speed_kt, mode, config, src, wx, model,
           step_nm, departure, clock0, rpm=None):
    """Integrate one leg. Returns the leg row."""
    dist_nm = dist_m / M_PER_NM
    n_steps = max(1, int(math.ceil(dist_nm / max(0.01, step_nm))))
    seg_nm = dist_nm / n_steps
    seg_m = dist_m / n_steps

    # A MarineField is resolved per STEP below; a plain dict is one value for the
    # whole leg. Both are supported because a hand-entered sea state is legitimately
    # leg-wide, while an interpolated field must not be flattened to one number.
    field = wx if isinstance(wx, MarineField) else None
    wmo = None if field else wx.get('wmo_sea_state')
    wind_kt = None if field else wx.get('wind_speed_kt')
    wind_from = None if field else wx.get('wind_from_deg')
    w_wind = w_wmo = w_wave = 0.0
    w_wind_x = w_wind_y = 0.0
    wx_seen = 0.0

    hours = 0.0
    litres = 0.0
    w_sog = w_stw = w_rpm = w_drift = w_along = w_cross = w_prem = 0.0
    w_set_x = w_set_y = 0.0
    covered_m = projected_m = 0.0
    in_window = True
    infeasible = False
    note = None

    for k in range(n_steps):
        # Midpoint of the step, along the geodesic. Sampling the current at the
        # midpoint rather than the start is a half-step better for free and stops
        # the first sample sitting exactly on a coastline node.
        frac = (k + 0.5) / n_steps
        plat, plon = geo.direct(a[0], a[1], brg, dist_m * frac)
        when = departure + dt.timedelta(hours=clock0 + hours)

        set_deg, drift_kt, quality = src.query(plat, plon, when)
        if quality is None:
            set_deg, drift_kt = 0.0, 0.0
        else:
            covered_m += seg_m
            if quality == 'projected':
                projected_m += seg_m

        if field is not None:
            f = field.at(plat, plon)
            wmo = f.get('wmo_sea_state')
            wind_kt = f.get('wind_speed_kt')
            wind_from = f.get('wind_from_deg')
            if wind_kt is not None:
                w_wind += wind_kt * seg_nm
                if wind_from is not None:
                    r = math.radians(wind_from)
                    w_wind_x += math.sin(r) * wind_kt * seg_nm
                    w_wind_y += math.cos(r) * wind_kt * seg_nm
            if wmo is not None:
                w_wmo += wmo * seg_nm
            if f.get('wave_height_m') is not None:
                w_wave += f['wave_height_m'] * seg_nm
            wx_seen += seg_nm

        # Course to steer along the line at this point (the geodesic bearing turns
        # slightly along a long leg, so re-take it rather than reuse the start).
        course = geo.inverse(plat, plon, b[0], b[1])[1] if frac < 1.0 else brg

        # FIXED THROTTLE. The revs are the input and the speed is an OUTPUT, so it
        # is recomputed at every step: the sea state and the wind angle change
        # along the line, and at fixed revs that changes how fast the boat goes
        # rather than what it burns.
        fixed = None
        if mode == 'rpm':
            fixed = model.stw_at_rpm(rpm, course, config, wmo_sea_state=wmo,
                                     wind_speed_kt=wind_kt, wind_from_deg=wind_from)
            if fixed['premium_clamped'] and 'following sea' not in (note or ''):
                note = ('speed credit for a following sea capped at the model floor '
                        '— the premium fit does not bound it')

        if mode in ('stw', 'rpm'):
            through = fixed['stw_kt'] if fixed else speed_kt
            if through <= 0:
                raise ValueError(f'{rpm:g} rpm gives no headway on this vessel')
            held = geo.stw_to_hold_track(course, set_deg, drift_kt, stw_kt=through)
            if held is None:
                infeasible = True
                note = ('current exceeds what the boat can crab out at '
                        f'{through:.2f} kt through the water')
                # Keep integrating at bare STW so the row still carries a length and
                # a fuel figure; the leg is already flagged and the total is marked
                # not feasible, which is what the operator acts on.
                heading, sog, crab = course, max(0.1, through), 0.0
            else:
                heading, sog, crab = held
            stw = through
        else:
            held = geo.stw_to_hold_track(course, set_deg, drift_kt, sog_target_kt=speed_kt)
            heading, sog, crab = held
            rel = math.radians(set_deg - course)
            cross = drift_kt * math.sin(rel)
            along = drift_kt * math.cos(rel)
            stw = math.hypot(speed_kt - along, cross)

        dt_h = seg_nm / sog if sog > 0 else 0.0
        hours += dt_h

        if fixed is not None:
            # The revs are COMMANDED, so they are not re-derived from the speed —
            # the burn is the rate at those revs for however long the leg took.
            litres += fixed['rate_lph'] * dt_h
            w_rpm += fixed['rpm'] * seg_nm
            w_prem += fixed['total_premium'] * seg_nm
            in_window = in_window and fixed['in_fit_window']
        else:
            burn = model.burn(stw, heading, dt_h, config, wmo_sea_state=wmo,
                              wind_speed_kt=wind_kt, wind_from_deg=wind_from) if model else None
            if burn:
                litres += burn['litres']
                w_rpm += burn['rpm'] * seg_nm
                w_prem += burn['total_premium'] * seg_nm
                in_window = in_window and burn['in_fit_window']

        rel = math.radians(set_deg - course)
        w_sog += sog * seg_nm
        w_stw += stw * seg_nm
        w_drift += drift_kt * seg_nm
        w_along += drift_kt * math.cos(rel) * seg_nm
        w_cross += drift_kt * math.sin(rel) * seg_nm
        # Average a DIRECTION as a vector, never as an angle: the mean of 350 and
        # 010 is 000, not 180.
        w_set_x += math.sin(math.radians(set_deg)) * drift_kt * seg_nm
        w_set_y += math.cos(math.radians(set_deg)) * drift_kt * seg_nm

    mean_set = (math.degrees(math.atan2(w_set_x, w_set_y)) % 360.0) \
        if (w_set_x or w_set_y) else None
    eta = departure + dt.timedelta(hours=clock0 + hours)

    # Report the DISTANCE-WEIGHTED mean of what the leg actually experienced. With an
    # interpolated field the leg no longer has "a" wind, so reporting the last step's
    # value — or the midpoint's — would misdescribe the leg the fuel was computed for.
    if field is not None and wx_seen > 0:
        wind_kt = (w_wind / wx_seen) if w_wind else None
        wind_from = (math.degrees(math.atan2(w_wind_x, w_wind_y)) % 360.0) \
            if (w_wind_x or w_wind_y) else None
        wmo = int(round(w_wmo / wx_seen)) if w_wmo else None
        wave_m = (w_wave / wx_seen) if w_wave else None
    else:
        wave_m = wx.get('wave_height_m') if isinstance(wx, dict) else None
    return {
        'distance_m': dist_m,
        'distance_nm': dist_nm,
        'hours': hours,
        'hhmm': geo.hm(hours),
        'litres': litres,
        'sog_kt': w_sog / dist_nm if dist_nm else 0.0,
        'stw_kt': w_stw / dist_nm if dist_nm else 0.0,
        'set_deg': mean_set,
        'drift_kt': w_drift / dist_nm if dist_nm else 0.0,
        'along_kt': w_along / dist_nm if dist_nm else 0.0,
        'cross_kt': w_cross / dist_nm if dist_nm else 0.0,
        'rpm': w_rpm / dist_nm if dist_nm else 0.0,
        'premium': w_prem / dist_nm if dist_nm else 0.0,
        'rate_lph': (litres / hours) if hours > 0 else 0.0,
        'heading_deg': brg,
        'crab_deg': None,
        'current_covered_m': covered_m,
        'current_projected_m': projected_m,
        'current_coverage': (covered_m / dist_m) if dist_m else 1.0,
        'in_fit_window': in_window,
        'infeasible': infeasible,
        'note': note,
        'eta_utc': eta.isoformat().replace('+00:00', 'Z'),
        'sea_state': wmo,
        'wind_kt': wind_kt,
        'wind_from_deg': wind_from,
        'wave_m': wave_m,
        'steps': n_steps,
    }


def line_bbox(points, margin_deg=0.0):
    """(lat0, lon0, lat1, lon1) covering the line, optionally padded. This is the
    'scope' every download in this tool is cut to — charts and currents alike."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - margin_deg, min(lons) - margin_deg,
            max(lats) + margin_deg, max(lons) + margin_deg)


def summarise(points):
    """Cheap geometry-only summary: per-leg distance/bearing and the total. No
    current, no fuel, no clock — what the UI shows the instant a point is dropped."""
    legs = []
    cum = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        d, brg, brg2 = geo.inverse(a[0], a[1], b[0], b[1])
        cum += d
        legs.append({'index': i, 'distance_m': d, 'distance_nm': d / M_PER_NM,
                     'bearing': brg, 'bearing_end': brg2,
                     'cumulative_nm': cum / M_PER_NM,
                     'from': {'lat': a[0], 'lon': a[1]},
                     'to': {'lat': b[0], 'lon': b[1]}})
    straight = geo.inverse(points[0][0], points[0][1], points[-1][0], points[-1][1])[0] \
        if len(points) >= 2 else 0.0
    return {'legs': legs, 'distance_m': cum, 'distance_nm': cum / M_PER_NM,
            'straight_nm': straight / M_PER_NM,
            'routing_cost_nm': (cum - straight) / M_PER_NM,
            'bbox': line_bbox(points), 'points': len(points)}
