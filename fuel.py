"""fuel.py — vessel fuel law, reading the companion fuel planner's model.json.

A FAITHFUL RE-IMPLEMENTATION OF ONE PATH THROUGH engine.py, not a new model. Every
coefficient comes from the vendored model.json; nothing is fitted here. The chain is
the planner's, in the planner's order — get it out of order and the numbers are wrong
in a way that still looks plausible:

    target SOG + current  ->  required speed through water
    STW                   ->  benign RPM            (config speed_vs_rpm)
    sea state + heading   ->  premium               (fractional, additive)
    benign RPM            ->  actual RPM            = benign * (1 + premium)
    actual RPM            ->  litres/hour           (config fuel_vs_rpm)

WHY NOT IMPORT engine.py. It is 1,525 lines built around a survey mission — line
counts, turn models, gauge profiles, drawdown — and it imports the planner's own
geometry module. A transit is one leg repeated; pulling the whole planner in to use
5% of it would drag its dependency tree into a tool that has to stay standalone.

WHAT IS DELIBERATELY NOT HERE: the gauge profile and drawdown. Those answer "what
does the gauge read now", which needs a real tank state this tool does not have. We
report litres and the reserve floor against tank volume, and leave gauge
interpretation to the planner that owns it.
"""

import json
import math
import os

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model.json')
IDLE_RPM = 1005.0
# The most speed a following sea is allowed to lend a fixed throttle: `1 + premium`
# is floored at 1 + this. See FuelModel.stw_at_rpm for why the inverse needs it and
# the forward chain does not.
PREMIUM_FLOOR = -0.5


class FuelModel:
    def __init__(self, path=MODEL_PATH):
        with open(path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        h = self.data['heading_effect']
        self.head_amp = h['amplitude_at_reference']
        self.head_ref_wind = h['reference_wind_kt']
        self.head_exp = h['wind_exponent']
        self.sea_table = {r['wmo']: r for r in self.data['sea_state_premium']['table']}
        self.tank_l = (self.data.get('tank_volume') or {}).get('litres')
        self.reserve_fraction = (self.data.get('reserve') or {}).get('default_fraction', 0.25)
        self.version = self.data.get('version')

    # -- configs ---------------------------------------------------------- #
    def configs(self):
        opts = self.data['configs']['options']
        return [{'key': k, 'label': v.get('label', k), 'status': v.get('status')}
                for k, v in opts.items()]

    def default_config(self):
        return self.data['configs'].get('default', 'config_a')

    def _g(self, config):
        opts = self.data['configs']['options']
        if config not in opts:
            raise ValueError(f'unknown config {config!r}; have {sorted(opts)}')
        return opts[config]

    # -- speed <-> rpm ----------------------------------------------------- #
    def rpm_for_speed(self, speed_kt, config):
        s = self._g(config)['speed_vs_rpm']
        if s['m'] == 0:
            raise ValueError('degenerate speed_vs_rpm slope')
        return (speed_kt - s['b']) / s['m']

    def speed_for_rpm(self, rpm, config):
        s = self._g(config)['speed_vs_rpm']
        return s['b'] + s['m'] * rpm

    # -- rpm -> burn ------------------------------------------------------- #
    def _law(self, config):
        """The fuel law for a config, falling back to the model-wide one.

        ONLY THE Config A HAS ITS OWN. The Config B block carries a speed curve but no
        fuel curve, and is meant to read the top-level `fuel_vs_rpm` (the linear
        2024 speed-trials fit). Without this fallback, selecting the Config B raised
        a KeyError — which the test suite caught and a user would have hit the
        first time they changed config.
        """
        law = self._g(config).get('fuel_vs_rpm')
        return law if law else self.data['fuel_vs_rpm']

    def fuel_rate_lph(self, rpm, config):
        law = self._law(config)
        if law.get('kind') == 'quadratic':
            v = law['q0'] + law['q1'] * rpm + law['q2'] * rpm * rpm
        else:
            v = law['f0'] + law['f1'] * rpm
        # The quadratic turns back up below its fitted floor; a negative burn is
        # nonsense in any case, so it is clamped rather than allowed to credit fuel.
        return max(0.0, v)

    def fit_window(self, config):
        law = self._law(config)
        return law.get('valid_rpm_min', 0), law.get('valid_rpm_max', 1e9)

    def in_fit_window(self, rpm, config):
        lo, hi = self.fit_window(config)
        return lo <= rpm <= hi

    def loiter_lph(self, config):
        """(litres/hour, measured?, rpm) for holding station."""
        block = self._g(config).get('loiter') or {}
        rpm = float(block.get('rpm') or IDLE_RPM)
        lph = block.get('lph')
        if lph is not None:
            return float(lph), True, rpm
        return self.fuel_rate_lph(rpm, config), False, rpm

    # -- environment ------------------------------------------------------- #
    def sea_state_premium(self, wmo):
        """Fractional RPM premium for a WMO sea state. Above the table, hold the
        top value — an extrapolated premium past WMO 6 would be invention, and
        holding is the conservative reading of an assumption that is already
        flagged as an assumption in the model."""
        if wmo is None:
            return 0.0
        row = self.sea_table.get(int(wmo))
        if row is None:
            return float(self.sea_table[max(self.sea_table)]['premium'])
        return float(row['premium'])

    def heading_premium(self, course_deg, wind_from_deg, wind_speed_kt):
        """Cosine modulation on required RPM: theta = 0 is a dead headwind (full
        penalty), 180 a following wind (full credit). Scales with wind squared."""
        if not wind_speed_kt or wind_speed_kt <= 0 or self.head_ref_wind <= 0:
            return 0.0
        if wind_from_deg is None:
            return 0.0
        theta = math.radians(course_deg - wind_from_deg)
        scale = (wind_speed_kt / self.head_ref_wind) ** self.head_exp
        return self.head_amp * scale * math.cos(theta)

    # -- the whole chain, for one steady segment --------------------------- #
    def burn(self, stw_kt, course_deg, hours, config,
             wmo_sea_state=None, wind_speed_kt=None, wind_from_deg=None):
        """Fuel for `hours` at `stw_kt` through the water on `course_deg`.

        TAKES SPEED THROUGH WATER, NOT OVER GROUND — the engine pushes the hull
        against the water, and a 2 kt favourable current costs nothing at the
        injector. The caller resolves current into STW before arriving here, which
        is the one ordering mistake that makes a current look like free fuel.
        """
        sea_p = self.sea_state_premium(wmo_sea_state)
        head_p = self.heading_premium(course_deg, wind_from_deg, wind_speed_kt)
        premium = sea_p + head_p
        rpm_benign = max(0.0, self.rpm_for_speed(stw_kt, config))
        rpm = rpm_benign * (1.0 + premium)
        rate = self.fuel_rate_lph(rpm, config)
        return {
            'litres': rate * hours,
            'rate_lph': rate,
            'rpm': rpm,
            'rpm_benign': rpm_benign,
            'sea_premium': sea_p,
            'heading_premium': head_p,
            'total_premium': premium,
            'in_fit_window': self.in_fit_window(rpm, config),
            'stw_kt': stw_kt,
        }

    # -- the same chain, run BACKWARDS, for a fixed throttle ---------------- #
    def stw_at_rpm(self, rpm, course_deg, config,
                   wmo_sea_state=None, wind_speed_kt=None, wind_from_deg=None):
        """Speed through the water when the THROTTLE is held at `rpm`.

        `burn` answers "what revs does this speed need"; this answers "what speed
        do these revs give". It is the same chain inverted, not a second model:
        the premium is extra revs to HOLD a speed, so at fixed revs it comes off
        the speed instead — `rpm = benign * (1 + premium)` rearranged to
        `benign = rpm / (1 + premium)`. THE CONSEQUENCE IS THE POINT: in this mode
        a head sea makes the passage longer at the same litres per hour, where in
        the speed modes it makes it dearer at the same duration.

        THE FLOOR ON THE DIVISOR IS NOT DECORATION. The heading premium scales
        with the square of the wind and is NEGATIVE downwind, with nothing in the
        fit bounding it: a 30 kt following wind drives `1 + premium` towards zero
        and the inversion then reports a boat going twice its own speed. The
        forward direction never divides by it, so this is the first place it
        bites. Clamped, and the clamp is reported rather than hidden — same
        reasoning as holding the sea-state premium at the top of its table.
        """
        sea_p = self.sea_state_premium(wmo_sea_state)
        head_p = self.heading_premium(course_deg, wind_from_deg, wind_speed_kt)
        premium = sea_p + head_p
        clamped = premium < PREMIUM_FLOOR
        if clamped:
            premium = PREMIUM_FLOOR
        rpm_benign = rpm / (1.0 + premium)
        return {
            'stw_kt': self.speed_for_rpm(rpm_benign, config),
            'rpm': rpm,
            'rpm_benign': rpm_benign,
            'rate_lph': self.fuel_rate_lph(rpm, config),
            'sea_premium': sea_p,
            'heading_premium': head_p,
            'total_premium': premium,
            'premium_clamped': clamped,
            'in_fit_window': self.in_fit_window(rpm, config),
        }

    def endurance(self, litres_used, capacity_l=None, reserve_fraction=None,
                  onboard_l=None):
        """How the burn sits against what is IN THE TANK and the reserve floor.

        `onboard_l` is the fuel actually aboard at departure; omit it and a full
        tank is assumed, which is what this returned before the field existed.

        THE RESERVE FLOOR IS A FRACTION OF THE TANK, NOT OF WHAT IS LOADED. The
        floor exists because of the tank — pickup height, sloshing, the margin the
        operator will not plan into — so it does not shrink when you sail with a
        part-full tank. Taking 25% of a 140 L load would call 35 L a floor and hand
        back 105 L usable, when the real floor is 62.5 L and only 77.5 L is yours to
        burn: a 27.5 L overstatement, in the direction that runs a boat dry.
        """
        cap = capacity_l if capacity_l is not None else (self.tank_l or 0.0)
        rf = self.reserve_fraction if reserve_fraction is None else reserve_fraction
        aboard = cap if onboard_l is None else onboard_l
        floor = cap * rf
        usable = aboard - floor
        return {
            'capacity_l': cap,
            'onboard_l': aboard,
            'full_tank': onboard_l is None,
            'reserve_fraction': rf,
            'reserve_floor_l': floor,
            'usable_l': usable,
            'used_l': litres_used,
            'remaining_l': aboard - litres_used,
            'margin_l': usable - litres_used,
            'within_reserve': litres_used <= usable,
            # Already below the floor before slipping the lines. Distinct from a
            # passage that merely eats the margin, and worth saying so.
            'starts_below_reserve': aboard < floor,
            'used_fraction_of_usable': (litres_used / usable) if usable > 0 else None,
        }
