"""weather.py — per-leg wind and sea state for the transit.

WHAT THE FUEL MODEL ACTUALLY WANTS is two numbers per leg: a wind (speed + the
direction it comes FROM, for the heading premium) and a WMO sea state (for the sea
premium). The companion fuel planner takes both as operator input. This module fetches
them instead, from Open-Meteo's free marine and forecast APIs — no key, no account,
and the same forward-looking hours the transit is planned over.

SEA STATE IS DERIVED, NOT OBSERVED. Open-Meteo gives significant wave height; WMO
sea state is a band of Hs. We map Hs -> WMO using the SAME band edges the fuel model
publishes in its own sea_state_premium table, so the two cannot drift apart. That
mapping is a lookup, not a measurement, and every value carries `derived: True` so a
reader knows the sea state was inferred from a forecast wave height rather than
observed from the deck.

FAILURE IS A NULL, NOT A ZERO. If the fetch fails — offline, throttled, out of
domain — every field comes back None and the transit runs with no weather premium at
all, flagged. Silently substituting calm would make a rough passage look cheap, which
is the one direction a fuel estimate must never be wrong.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

MARINE_URL = 'https://marine-api.open-meteo.com/v1/marine'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
USER_AGENT = 'Transit-Calculator/1.0'
MS_TO_KT = 1.9438444924406046

_cache = {}
_CACHE_TTL = 1800.0        # 30 min: forecasts update hourly at best
_down_until = 0.0


def hs_to_wmo(hs_m, table=None):
    """Significant wave height (m) -> WMO sea state code.

    Band edges are the model's own (0, 0.1, 0.5, 1.25, 2.5, 4, 6 m). Passing the
    model's table keeps this honest if those edges are ever revised.
    """
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
    code = edges[-1][1]
    for hi, c in edges:
        if hs_m <= hi:
            code = c
            break
    else:
        code = edges[-1][1]
    return code


def _get_json(url, params, timeout=20.0):
    global _down_until
    if time.monotonic() < _down_until:
        return None
    q = url + '?' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(q, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except (OSError, ValueError):
        _down_until = time.monotonic() + 120.0
        return None


def at(lat, lon, when=None, sea_table=None):
    """Wind and sea state at a position (and optionally a UTC time).

    Returns {wind_speed_kt, wind_from_deg, wave_height_m, wave_period_s,
             wmo_sea_state, derived, source, time}. All-None on failure.
    """
    key = (round(lat, 2), round(lon, 2))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        block = hit[1]
    else:
        common = {'latitude': f'{lat:.4f}', 'longitude': f'{lon:.4f}',
                  'timezone': 'UTC', 'forecast_days': 3}
        marine = _get_json(MARINE_URL, dict(common, hourly='wave_height,wave_period,wave_direction'))
        wind = _get_json(FORECAST_URL, dict(common, hourly='wind_speed_10m,wind_direction_10m'))
        if marine is None and wind is None:
            return _empty('fetch failed (offline or throttled)')
        block = {'marine': marine, 'wind': wind}
        _cache[key] = (now, block)

    idx = _pick_hour(block, when)
    marine, wind = block.get('marine'), block.get('wind')

    hs = _series(marine, 'wave_height', idx)
    per = _series(marine, 'wave_period', idx)
    ws = _series(wind, 'wind_speed_10m', idx)
    wd = _series(wind, 'wind_direction_10m', idx)

    # Open-Meteo reports wind in km/h by default and wind_direction as the direction
    # it comes FROM — which is what the heading premium wants, so no flip here.
    wind_kt = (ws / 3.6) * MS_TO_KT if ws is not None else None
    stamp = None
    for src in (marine, wind):
        if src and idx is not None:
            try:
                stamp = src['hourly']['time'][idx]
                break
            except (KeyError, IndexError, TypeError):
                pass
    return {
        'wind_speed_kt': wind_kt,
        'wind_from_deg': wd,
        'wave_height_m': hs,
        'wave_period_s': per,
        'wmo_sea_state': hs_to_wmo(hs, sea_table),
        'derived': hs is not None,
        'source': 'open-meteo',
        'time': stamp,
        'note': None,
    }


def _empty(note):
    return {'wind_speed_kt': None, 'wind_from_deg': None, 'wave_height_m': None,
            'wave_period_s': None, 'wmo_sea_state': None, 'derived': False,
            'source': None, 'time': None, 'note': note}


def _series(block, name, idx):
    if not block or idx is None:
        return None
    try:
        v = block['hourly'][name][idx]
    except (KeyError, IndexError, TypeError):
        return None
    return None if v is None else float(v)


def _pick_hour(block, when):
    """Index of the forecast hour nearest `when`; 0 (now) if no time given."""
    src = block.get('marine') or block.get('wind')
    if not src:
        return None
    try:
        times = src['hourly']['time']
    except (KeyError, TypeError):
        return None
    if not times:
        return None
    if when is None:
        return 0
    target = when.strftime('%Y-%m-%dT%H:00')
    if target in times:
        return times.index(target)
    # Past the end of the forecast: hold the last hour rather than fail, and the
    # caller sees a `time` stamp that plainly predates the leg.
    return len(times) - 1 if target > times[-1] else 0


def for_legs(legs, departure=None, sea_table=None):
    """Weather for each leg, sampled at the leg MIDPOINT and its ETA.

    Sampling per leg rather than once per transit is the point: a 78 NM passage
    crosses a real wind gradient, and the leg running offshore into a building sea
    is exactly the one whose fuel the mission hangs on.
    """
    import datetime as dt
    out = []
    for leg in legs:
        a, b = leg['from'], leg['to']
        mid_lat = (a['lat'] + b['lat']) / 2
        mid_lon = (a['lon'] + b['lon']) / 2
        when = None
        if departure is not None and leg.get('eta_utc'):
            try:
                when = dt.datetime.fromisoformat(leg['eta_utc'].replace('Z', '+00:00'))
            except ValueError:
                when = departure
        out.append(at(mid_lat, mid_lon, when, sea_table))
    return out
