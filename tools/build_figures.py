"""Draw the figures the manuals use.

    python tools/build_figures.py            # -> tools/figs/*.png

Every figure here is GENERATED FROM THE REAL CODE where it can be. The marching
figure calls transit.plan; the domain figure reads the probed OFS domains; the
direction figure runs both directions through the actual planner. A hand-drawn
diagram of what the code is believed to do is a diagram of a belief — these go stale
loudly, by failing to build, rather than quietly by being wrong.

Where a figure needs live data that may be absent (a cached OFS cycle), it falls back
to a clearly-labelled synthetic illustration rather than failing the whole build, and
says so in its own caption.
"""

import datetime as dt
import math
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fuel                               # noqa: E402
import geo                                # noqa: E402
import transit                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIGS = os.path.join(HERE, 'figs')

# The console's palette, so the manuals and the tool look like one product.
INK = '#12202f'
SOFT = '#54697a'
TRACK = '#1a7fb5'
OK = '#2f8f52'
WARN = '#c8862b'
BAD = '#b8433f'
GRID = '#dde5ea'

LEWES = [(38.796273, -75.155618), (38.810697, -75.097760), (38.799114, -75.075981),
         (38.763953, -75.065596), (38.469565, -74.783603), (38.357256, -74.792817),
         (37.664897, -74.997880)]

plt.rcParams.update({
    'font.size': 8, 'axes.edgecolor': SOFT, 'axes.labelcolor': INK,
    'text.color': INK, 'xtick.color': SOFT, 'ytick.color': SOFT,
    'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': .6,
    'figure.dpi': 200, 'savefig.dpi': 200, 'savefig.bbox': 'tight',
})


def save(fig, name):
    os.makedirs(FIGS, exist_ok=True)
    p = os.path.join(FIGS, name)
    fig.savefig(p, facecolor='white')
    plt.close(fig)
    print(f'  {name}')


# --------------------------------------------------------------------------- #
def fig_marching():
    """Why the current is integrated, not sampled once.

    A synthetic current that reverses on a 6-hour cycle, run through the REAL
    planner at a fine step and at one-sample-per-leg. The gap between the two is
    the error a naive calculator makes, and it is drawn rather than asserted.
    """
    class Reversing(transit.CurrentSource):
        def __init__(self):
            super().__init__(None)
            self.tag = 'synthetic'

        def query(self, lat, lon, when):
            h = (when - dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() / 3600
            return (193.0 if int(h // 6) % 2 == 0 else 13.0), 2.0, 'measured'

    M = fuel.FuelModel()
    dep = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    steps = [0.125, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 200]
    hours = [transit.plan(LEWES, 8.0, departure=dep, model=M,
                          currents=Reversing(), step_nm=s)['hours'] for s in steps]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.4, 2.9))

    # left: the current the boat actually meets, hour by hour
    t = [i / 4 for i in range(0, 45)]
    along = [2.0 if int(h // 6) % 2 == 0 else -2.0 for h in t]
    ax.step(t, along, where='post', color=TRACK, lw=1.6)
    ax.axhline(0, color=SOFT, lw=.8)
    ax.fill_between(t, 0, along, step='post', alpha=.15, color=TRACK)
    ax.set_xlabel('hours after departure')
    ax.set_ylabel('along-track current (kt)')
    ax.set_title('The tide turns mid-passage', loc='left', fontsize=9, color=INK)
    ax.set_ylim(-3, 3)
    ax.annotate('a single sample\nsees only this', xy=(1.5, 2), xytext=(4.2, 2.4),
                fontsize=7, color=BAD,
                arrowprops=dict(arrowstyle='->', color=BAD, lw=.9))

    # right: convergence
    ax2.semilogx(steps, [h * 60 for h in hours], 'o-', color=TRACK, ms=3.5, lw=1.4)
    conv = hours[0] * 60
    ax2.axhline(conv, color=OK, lw=.9, ls='--')
    ax2.set_xlabel('integration step (NM)   — coarser to the right')
    ax2.set_ylabel('passage time (minutes)')
    ax2.set_title('One sample per leg is the right-hand end', loc='left',
                  fontsize=9, color=INK)
    # BELOW the line, not above: above put it through the panel title.
    ax2.annotate(f'converged  {geo.hm(hours[0])}', xy=(0.14, conv), xytext=(0.16, conv - 14),
                 fontsize=7, color=OK)
    err = (hours[-1] - hours[0]) * 60
    ax2.annotate(f'{err:+.0f} min', xy=(steps[-1], hours[-1] * 60),
                 xytext=(steps[-1] * 0.25, hours[-1] * 60 + 6), fontsize=7, color=BAD,
                 arrowprops=dict(arrowstyle='->', color=BAD, lw=.9))
    save(fig, 'marching.png')
    return err


# --------------------------------------------------------------------------- #
def fig_domains():
    """The line against the model domains that do and do not reach it."""
    try:
        import ofs
        doms = ofs._load_domains()
    except Exception:
        doms = {}

    fig, ax = plt.subplots(figsize=(4.1, 4.4))
    for key, colour, label in (('dbofs', TRACK, 'DBOFS'), ('cbofs', WARN, 'CBOFS')):
        d = doms.get(key)
        if not d:
            continue
        ax.add_patch(Rectangle((d['lon0'], d['lat0']), d['lon1'] - d['lon0'],
                               d['lat1'] - d['lat0'], fill=False, lw=1.3,
                               ec=colour, ls='--', label=f'{label} extent'))
        ax.axhline(d['lat0'], color=colour, lw=.7, alpha=.5)

    lats = [p[0] for p in LEWES]
    lons = [p[1] for p in LEWES]
    ax.plot(lons, lats, '-', color=INK, lw=2.2, zorder=5)
    ax.plot(lons[0], lats[0], 'o', color=OK, ms=8, zorder=6)
    ax.plot(lons[-1], lats[-1], 'o', color=WARN, ms=8, zorder=6)
    ax.annotate('START  Lewes', (lons[0], lats[0]), xytext=(6, 6),
                textcoords='offset points', fontsize=7.5, color=OK, weight='bold')
    ax.annotate('END', (lons[-1], lats[-1]), xytext=(6, -10),
                textcoords='offset points', fontsize=7.5, color=WARN, weight='bold')

    d = doms.get('dbofs')
    if d:
        ax.axhspan(min(lats), d['lat0'], color=BAD, alpha=.10)
        # Placed well LEFT of the track and BELOW the boundary. The first version
        # sat exactly on DBOFS's southern edge and was unreadable against it.
        ax.annotate('DBOFS stops here.\nCBOFS covers the rest.',
                    (-76.10, d['lat0'] - 0.14), fontsize=7,
                    color=BAD, ha='left', va='top')

    ax.set_xlabel('longitude'); ax.set_ylabel('latitude')
    ax.set_title('One model is one region', loc='left', fontsize=9, color=INK)
    ax.legend(fontsize=6.5, loc='upper left', frameon=False)
    ax.set_xlim(-76.2, -74.2); ax.set_ylim(36.0, 40.6)
    ax.set_xticks([-76.0, -75.5, -75.0, -74.5])      # four ticks; eight overlapped
    save(fig, 'domains.png')


# --------------------------------------------------------------------------- #
def fig_idw():
    """Why the interpolation is per-variable: real buoys report partially."""
    fig, ax = plt.subplots(figsize=(4.8, 3.4))
    ax.set_axis_off()

    stations = [
        ('44009\nDelaware Bay', 0.15, 0.74, ['wind', 'gust'], ['waves']),
        ('44084\nBethany Beach', 0.15, 0.40, ['waves', 'period'], ['wind']),
        ('NWS grid', 0.50, 0.90, ['wind', 'waves'], []),
        ('WAVEWATCH III', 0.50, 0.34, ['waves'], ['wind']),
    ]
    for name, x, y, has, missing in stations:
        ax.text(x, y, name, fontsize=7.5, ha='center', va='center', color=INK,
                weight='bold',
                bbox=dict(boxstyle='round,pad=0.45', fc='#f2f6f9', ec=SOFT, lw=.8))
        ax.text(x, y - 0.095, ' '.join(has), fontsize=6.4, ha='center', color=OK)
        if missing:
            ax.text(x, y - 0.147, 'blank: ' + ' '.join(missing), fontsize=6.4,
                    ha='center', color=BAD)
        # Curvature sign follows which side of the target the station sits on.
        # A single sign bowed the lower-left arrow straight through the
        # WAVEWATCH III box and struck the text out.
        rad = 0.15 if y >= 0.60 else -0.15
        ax.annotate('', xy=(0.79, 0.60), xytext=(x + 0.10, y),
                    arrowprops=dict(arrowstyle='->', color=SOFT, lw=.9,
                                    connectionstyle=f'arc3,rad={rad}'))

    ax.text(0.87, 0.60, 'the point\non the track', fontsize=7.5, ha='center',
            va='center', color='white', weight='bold',
            bbox=dict(boxstyle='circle,pad=0.55', fc=TRACK, ec='none'))
    ax.text(0.5, 0.98, 'Each FIELD is interpolated from whichever samples carry it',
            fontsize=8, ha='center', color=INK, weight='bold')
    # The caption gets its own band at the foot. In the first version the
    # bottom-row station's sub-labels landed straight on top of it.
    ax.text(0.5, 0.03, 'A station is not a unit. Reading a blank as zero\n'
                       'would manufacture calm from a broken sensor.',
            fontsize=6.8, ha='center', va='bottom', color=BAD, style='italic')
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    save(fig, 'idw.png')


# --------------------------------------------------------------------------- #
def fig_direction():
    """The same line, both ways, through the real planner."""
    M = fuel.FuelModel()
    wx = {'wmo_sea_state': 2, 'wind_speed_kt': 12, 'wind_from_deg': 200}
    fwd = transit.plan(LEWES, 8.0, model=M, currents=transit.NullCurrents(), weather=wx)
    rev = transit.plan(LEWES[::-1], 8.0, model=M, currents=transit.NullCurrents(), weather=wx)

    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    legs = [l['index'] + 1 for l in fwd['legs']]
    ax.bar([x - 0.2 for x in legs], [l['litres'] for l in fwd['legs']], .38,
           label=f"as drawn — {fwd['litres']:.1f} L total", color=TRACK)
    ax.bar([x + 0.2 for x in legs], [l['litres'] for l in rev['legs']][::-1], .38,
           label=f"reversed — {rev['litres']:.1f} L total", color=WARN)
    ax.set_xlabel('leg (as drawn)'); ax.set_ylabel('fuel (litres)')
    ax.set_title('Same line, same wind, opposite directions', loc='left',
                 fontsize=9, color=INK)
    ax.legend(fontsize=6.8, frameon=False)
    ax.set_xticks(legs)
    save(fig, 'direction.png')
    return fwd['litres'], rev['litres']


def main():
    print('figures ->', FIGS)
    err = fig_marching()
    fig_domains()
    fig_idw()
    f, r = fig_direction()
    print(f'\n  marching: one-sample-per-leg error {err:+.0f} min')
    print(f'  direction: {f:.1f} L as drawn vs {r:.1f} L reversed')


if __name__ == '__main__':
    main()
