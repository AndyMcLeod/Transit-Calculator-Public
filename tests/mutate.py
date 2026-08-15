"""tests/mutate.py — prove the test suite has teeth.

A GREEN SUITE MEANS NOTHING UNTIL IT HAS GONE RED ON PURPOSE. Each mutation below
breaks one specific behaviour the suite claims to protect. If the suite still passes
with the behaviour broken, that check is decorative and this runner says so.

SAFETY — THIS IS THE PART THAT MATTERS. Mutations are applied to a COPY of the tree
in a temp directory, never to the working source. A runner that edits real files and
is then interrupted leaves the source silently corrupted; that has happened before,
so the design here removes the possibility rather than handling it. The real
directory is opened read-only and never written.

RUN:  python tests/mutate.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (label, file, find, replace, suite that must go red)
MUTATIONS = [
    ('geodesy falls back to a sphere', 'geo.py',
     '    u2 = cos2_alpha * (A * A - B * B) / (B * B)',
     '    return haversine(lat1, lon1, lat2, lon2), 0.0, 0.0\n'
     '    u2 = cos2_alpha * (A * A - B * B) / (B * B)', 'geodesy'),

    ('current set direction flipped', 'geo.py',
     '    return math.degrees(math.atan2(u_ms, v_ms)) % 360.0, drift',
     '    return math.degrees(math.atan2(v_ms, u_ms)) % 360.0, drift', 'geodesy'),

    ('unholdable cross-set answered instead of refused', 'geo.py',
     '        if abs(cross) > stw_kt:\n            return None',
     '        if False:\n            return None', 'geodesy'),

    ('UTM forced zone ignored', 'geo.py',
     '    if zone is None:\n        zone, auto_hemi = utm_zone_for(lat, lon)',
     '    if True:\n        zone, auto_hemi = utm_zone_for(lat, lon)', 'geodesy'),

    ('shapefile index writes a constant stride', 'shapefile_io.py',
     '        offset_words = (100 + len(body)) // 2',
     '        offset_words = 50', 'shapefile'),

    ('null shape counted as geometry', 'shapefile_io.py',
     '        if st == NULL:\n            null_count += 1\n            continue',
     '        if st == NULL:\n            continue', 'shapefile'),

    ('unknown .prj guessed as geographic', 'shapefile_io.py',
     "    return {'kind': 'unknown'}\n    if 'GEOGCS' in t",
     "    return {'kind': 'geographic', 'epsg': 4326}\n    if 'GEOGCS' in t", 'shapefile'),

    ('GeoJSON writes lat,lon instead of lon,lat', 'exporters.py',
     '    coords = [[round(lon, 8), round(lat, 8)] for lat, lon in points]',
     '    coords = [[round(lat, 8), round(lon, 8)] for lat, lon in points]', 'export'),

    ('shapefile export ignores the requested UTM zone', 'exporters.py',
     '        xy = [tuple(geo.ll_to_utm(lat, lon, utm_zone, hemi)[:2]) for lat, lon in points]',
     '        xy = [(lon, lat) for lat, lon in points]', 'export'),

    ('heading premium dropped', 'fuel.py',
     '        theta = math.radians(course_deg - wind_from_deg)',
     '        return 0.0\n        theta = math.radians(course_deg - wind_from_deg)', 'fuel'),

    ('sea state premium above the table returns zero', 'fuel.py',
     "            return float(self.sea_table[max(self.sea_table)]['premium'])",
     '            return 0.0', 'fuel'),

    ('Config B fuel-law fallback removed', 'fuel.py',
     "        return law if law else self.data['fuel_vs_rpm']",
     '        return law', 'fuel'),

    # -- fuel aboard at departure --------------------------------------------------
    # THE PLAUSIBLE WRONG ONE. Taking the reserve as a fraction of what is LOADED
    # reads perfectly well and errs in the direction that runs a boat dry: 140 L
    # aboard would report 105 L usable where the honest answer is 77.5 L. A check
    # that only asserted "a part tank lowers usable" is satisfied by it.
    ('reserve floor taken from the load instead of the tank', 'fuel.py',
     '        floor = cap * rf',
     '        floor = aboard * rf', 'fuel'),

    ('fuel aboard ignored — always a full tank', 'fuel.py',
     '        aboard = cap if onboard_l is None else onboard_l',
     '        aboard = cap', 'fuel'),

    ('a load under the reserve floor no longer called out', 'fuel.py',
     "            'starts_below_reserve': aboard < floor,",
     "            'starts_below_reserve': False,", 'fuel'),

    ('fuel aboard dropped between the plan and the model', 'transit.py',
     '                                              reserve_fraction, onboard_l)',
     '                                              reserve_fraction, None)', 'transit'),

    ('an empty tank read as a blank field', 'server.py',
     "    if raw is None or raw == '':",
     '    if not raw:', 'transit'),

    ('a load bigger than the tank trimmed instead of refused', 'server.py',
     '    if capacity_l and v > capacity_l + 1e-9:',
     '    if False:', 'transit'),

    # The 500 this guard exists to stop. Without it the writers index points[0].
    ('export takes the line on trust', 'server.py',
     "        if len(pts) < 2:\n            raise ValueError('a transit needs at least two points')",
     '        if False:\n            raise ValueError(\'unreachable\')', 'transit'),

    # -- fixed throttle ------------------------------------------------------------
    # THE PLAUSIBLE WRONG ONE. Ignoring the premium in the inversion reads fine and
    # quietly turns fixed-rpm into "the benign curve speed, whatever the weather" —
    # which is the one thing this mode exists NOT to say.
    ('fixed revs ignore the weather', 'fuel.py',
     '        rpm_benign = rpm / (1.0 + premium)',
     '        rpm_benign = rpm', 'transit'),

    ('the following-sea clamp removed', 'fuel.py',
     '        clamped = premium < PREMIUM_FLOOR',
     '        clamped = False', 'transit'),

    ('fixed revs re-derived from the speed instead of held', 'transit.py',
     "            w_rpm += fixed['rpm'] * seg_nm",
     "            w_rpm += model.rpm_for_speed(stw, config) * seg_nm", 'transit'),

    ('rpm mode secretly sails at the speed box', 'transit.py',
     "            through = fixed['stw_kt'] if fixed else speed_kt",
     '            through = speed_kt', 'transit'),

    # -- drift ---------------------------------------------------------------------
    # THE ONE THAT SENDS A SEARCH THE WRONG WAY. Dropping the reciprocal makes the
    # hull drift INTO the wind, which looks entirely plausible on a chart.
    ('leeway drifts upwind', 'drift.py',
     '    downwind = (wind_from_deg + 180.0) % 360.0',
     '    downwind = wind_from_deg % 360.0', 'drift'),

    # The units seam. Slope is a percentage of the wind in m/s and the answer is
    # cm/s; this tool speaks knots. Skipping the conversion is wrong by ~2x and
    # still produces a plausible-looking drift.
    ('leeway skips the knots-to-m/s conversion', 'drift.py',
     '    w_ms = max(0.0, wind_kt) * MS_PER_KT',
     '    w_ms = max(0.0, wind_kt)', 'drift'),

    ('the leeway offset term dropped', 'drift.py',
     '    dw_cms = (dw_slope + dw_eps / 20.0) * w_ms + dw_offset + dw_eps / 2.0',
     '    dw_cms = (dw_slope + dw_eps / 20.0) * w_ms + dw_eps / 2.0', 'drift'),

    ('the crosswind component dropped', 'drift.py',
     "    cross = (downwind + (90.0 if cw_kt >= 0 else -90.0)) % 360.0",
     '    cross = downwind', 'drift'),

    # Symmetrising the two tacks. Harmless for PIW-1, wrong for the Navy raft,
    # whose crosswind is +5.7 cm/s one way and -3.4 the other.
    ('both tacks read the right-hand row', 'drift.py',
     "    cw_slope, cw_offset, _ = cls['cw_right' if right else 'cw_left']",
     "    cw_slope, cw_offset, _ = cls['cw_right']", 'drift'),

    ('both tacks collapse to one', 'drift.py',
     '    for right in (True, False):',
     '    for right in (True, True):', 'drift'),

    # The payload the page boots from. A stale key here is invisible: the console
    # stays silent, the dropdown comes up empty and the server falls back to its
    # default class, so the tool still answers while the panel behind it is broken.
    ('the leeway payload loses a class', 'server.py',
     "            for k, v in drift.LEEWAY_CLASSES.items()]",
     "            for k, v in list(drift.LEEWAY_CLASSES.items())[:1]]", 'transit'),

    # -- the swell-only Stokes term -----------------------------------------------
    # THE DOUBLE COUNT IT EXISTS TO AVOID. Adding the whole Stokes vector rather than
    # the swell-only remainder counts the wind-driven waves twice — once inside the
    # leeway coefficients and again here — and reads as perfectly reasonable code.
    ('the wind-driven part of the wave transport counted twice', 'drift.py',
     '    if along > 0:',
     '    if False:', 'drift'),

    # The classic factor of two: Hs/2 looks like the amplitude, and overstates a
    # spectrum by double.
    ('Stokes amplitude taken as Hs/2', 'drift.py',
     '    a = hs_m / (2.0 * math.sqrt(2.0))',
     '    a = hs_m / 2.0', 'drift'),

    ('Stokes runs toward where the waves came from', 'drift.py',
     '    toward = (wave_from_deg + 180.0) % 360.0',
     '    toward = wave_from_deg % 360.0', 'drift'),

    ('the wave-steepness cap removed', 'drift.py',
     '    if steep > STEEPNESS_MAX:',
     '    if False:', 'drift'),

    # The measured standard error IS the search radius. Zeroing it leaves a tidy
    # circle that describes nothing but the current scaling.
    ('the measured coefficient spread ignored', 'drift.py',
     "    dw_std = cls['dw'][2]",
     '    dw_std = 0.0', 'drift'),

    # The door refusal. Without it a position with no forecast under it returns a
    # wind-only track formatted exactly like a real one — failing towards
    # confidence, which is the worst direction for this module to fail in.
    ('a start with no forecast is answered anyway', 'drift.py',
     '    if q0 is None:\n        raise ValueError(',
     '    if False:\n        raise ValueError(', 'drift'),

    ('the dry-node nudge removed', 'drift.py',
     '    for km, brg in _NUDGE_RING:',
     '    for km, brg in []:', 'drift'),

    ('the ensemble collapses to the nominal', 'drift.py',
     '    scales = sorted({1.0 - current_scale, 1.0, 1.0 + current_scale})',
     '    scales = [1.0]', 'drift'),

    ('drift ignores the current entirely', 'drift.py',
     '        v_kt, v_deg = _vector_sum(drift_kt * current_scale, set_deg or 0.0,',
     '        v_kt, v_deg = _vector_sum(0.0, set_deg or 0.0,', 'drift'),

    ('transit stops marching (one sample per leg)', 'transit.py',
     '    n_steps = max(1, int(math.ceil(dist_nm / max(0.01, step_nm))))',
     '    n_steps = 1', 'transit'),

    ('missing current silently treated as slack water', 'transit.py',
     '        if quality is None:\n            set_deg, drift_kt = 0.0, 0.0\n        else:\n'
     '            covered_m += seg_m',
     '        if quality is None:\n            set_deg, drift_kt = 0.0, 0.0\n'
     '        if True:\n            covered_m += seg_m', 'transit'),

    ('current evaluated at departure, never advancing the clock', 'transit.py',
     '        when = departure + dt.timedelta(hours=clock0 + hours)',
     '        when = departure', 'transit'),

    ('infeasible leg no longer flagged', 'transit.py',
     '            if held is None:\n                infeasible = True',
     '            if held is None:\n                infeasible = False', 'transit'),

    ('sog mode ignores the target and runs at stw', 'transit.py',
     "            held = geo.stw_to_hold_track(course, set_deg, drift_kt, sog_target_kt=speed_kt)",
     "            held = geo.stw_to_hold_track(course, set_deg, drift_kt, stw_kt=speed_kt)",
     'transit'),

    ('Hs to sea-state mapping ignores the model table', 'weather.py',
     '        if parsed:\n            edges = sorted(parsed)',
     '        if parsed:\n            pass', 'weather'),

    # -- multi-model current chaining -------------------------------------------
    ('handover snaps instead of blending', 'ofs.py',
     '        if len(hits) == 1 or primary[3] >= self.blend_km:',
     '        if True:', 'ofs'),

    ('a coarser model is never consulted', 'ofs.py',
     '            got = self._one(cyc, lat, lon, when)',
     '            got = self._one(cyc, lat, lon, when) if rank <= 10 else None', 'ofs'),

    ('domain occupancy ignores the cell raster', 'ofs.py',
     "            if f'{ci + di},{cj + dj}' in occ:\n                return True",
     '            if True:\n                return True', 'ofs'),

    ('current blending averages angles, not vectors', 'ofs.py',
     '    drift = math.hypot(x, y)',
     '    drift = (da * w + db * (1 - w))\n    s = sa * w + sb * (1 - w)\n'
     '    return s % 360.0, drift, qa\n    drift = math.hypot(x, y)', 'ofs'),

    # -- IDW over scattered marine samples ---------------------------------------
    ('IDW ignores source quality', 'marine.py',
     "        w = QUALITY.get(s['source'], 1.0) / ((d ** power) + eps_km ** power)",
     '        w = 1.0 / ((d ** power) + eps_km ** power)', 'marine'),

    ('IDW treats a missing field as zero', 'marine.py',
     "            v = s['values'].get(field)\n            if v is None:\n"
     '                continue\n            num += w * v',
     "            v = s['values'].get(field) or 0.0\n            num += w * v", 'marine'),

    ('wave direction ignores wave height', 'marine.py',
     '            ww = w * (mag if mag and mag > 0 else 1.0)',
     '            ww = w', 'marine'),

    ('distant samples are not dropped', 'marine.py',
     '        if d > max_km:\n            continue',
     '        if False:\n            continue', 'marine'),

    ('line sampling reverts to per-vertex', 'marine.py',
     '    segs = []\n    total = 0.0',
     '    return [tuple(p) for p in points[:n]]\n    segs = []\n    total = 0.0', 'marine'),

    ('MarineField returns one value everywhere', 'transit.py',
     '        key = (round(lat / self.cache_deg), round(lon / self.cache_deg))',
     '        key = 0', 'marine'),

    # -- timestamped saves --------------------------------------------------------
    ('the save timestamp is dropped from the filename', 'exporters.py',
     "    return f'{base}_{stamp}' if stamp else base",
     '    return base', 'export'),

    ('the timestamp stops at the zip and never reaches its members', 'exporters.py',
     '    nm = _stamped(_clean(name), stamp)\n    if utm_zone:',
     '    nm = _clean(name)\n    if utm_zone:', 'export'),

    ('the timestamp is read from the clock instead of passed in', 'exporters.py',
     "    return f'{base}_{stamp}' if stamp else base",
     "    return f'{base}_{stamp_now()}' if stamp else base", 'export'),
]


def run(cwd, suite=None):
    cmd = [sys.executable, os.path.join('tests', 'test_transit.py')]
    if suite:
        cmd.append(suite)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def main():
    print(f'  source (read-only): {APP}')
    with tempfile.TemporaryDirectory(prefix='transit-mutate-') as td:
        work = os.path.join(td, 'src')
        # Copy only what the suites need. The tile cache and the 33 MB OFS cycle are
        # symlinked-by-omission: the currents suite skips in the copy, which is fine
        # because the point here is whether the ASSERTIONS bite, not the data.
        shutil.copytree(APP, work, ignore=shutil.ignore_patterns(
            'charts', 'ofs_cache', '__pycache__', '.git', 'logs'))
        print(f'  working copy:       {work}\n')

        base_rc, base_out = run(work)
        if base_rc != 0:
            print('  BASELINE IS NOT GREEN — fix that before trusting any mutation below')
            print(base_out[-1500:])
            return 2
        print('  baseline: green\n')

        caught = missed = broken = 0
        for label, fname, find, repl, suite in MUTATIONS:
            path = os.path.join(work, fname)
            with open(path, 'r', encoding='utf-8') as f:
                original = f.read()
            if find not in original:
                print(f'    STALE   {label}  (anchor not found in {fname})')
                broken += 1
                continue
            with open(path, 'w', encoding='utf-8') as f:
                f.write(original.replace(find, repl, 1))
            try:
                rc, out = run(work, suite)
            finally:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(original)          # always restore, even on timeout
            if rc != 0:
                first = next((l.strip() for l in out.splitlines() if 'FAIL' in l), '')
                print(f'    caught  {label}\n              -> {first[:96]}')
                caught += 1
            else:
                print(f'    MISSED  {label}  ({suite} still passed)')
                missed += 1

        print(f'\n  {caught} caught, {missed} missed, {broken} stale '
              f'of {len(MUTATIONS)} mutations')
        # Re-run clean to prove every restore worked.
        rc, _ = run(work)
        print(f'  working copy restored and green: {rc == 0}')
        return 1 if (missed or broken or rc != 0) else 0


if __name__ == '__main__':
    sys.exit(main())
