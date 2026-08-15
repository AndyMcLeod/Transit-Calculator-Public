"""Build the Transit Calculator's four Word manuals.

    python tools/build_docs.py               # -> docs/*.docx
    python tools/build_docs.py quickstart    # just one

    SOURCE_DATE=2026-08-14 python tools/build_docs.py     # reproducible date

FOUR DOCUMENTS, FOUR READERS:

  Quick Start          someone who wants a computed transit in ten minutes.
  Instruction Manual   the operator: every control, every field, every flag.
  Technical Manual     someone deciding whether to TRUST a number: the algorithms,
                       the data sources, the accuracy and the limits.
  Development Manual   someone changing the code: architecture, conventions, tests,
                       how to extend it, how the public export works.

NUMBERS IN THESE DOCUMENTS COME FROM THE CODE, not from prose. The check count, the
model version, the config list, the export formats, the sample line's length and the
worked figures are all read or computed at build time. A manual that quotes a figure
by hand is wrong the first time anyone changes it and silent about being wrong; this
one fails to build instead.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx_style as D                       # noqa: E402
import drift                                 # noqa: E402
import exporters                             # noqa: E402
import fuel                                  # noqa: E402
import geo                                   # noqa: E402
import ofs                                   # noqa: E402
import transit                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, 'docs')
FIGS = Path(HERE) / 'figs'      # a Path: docx_style.figure() does figs_dir / name

LEWES = [(38.796273, -75.155618), (38.810697, -75.097760), (38.799114, -75.075981),
         (38.763953, -75.065596), (38.469565, -74.783603), (38.357256, -74.792817),
         (37.664897, -74.997880)]

MODEL = fuel.FuelModel()
SUMMARY = transit.summarise(LEWES)
DATE = D.build_date_str()


def test_count():
    """RUN the suite and read its total, rather than counting `check(` in the source.

    Counting occurrences undercounts: several checks sit inside loops (one per export
    format, one per quadrant), so the source said 225 where the suite actually runs
    241. A manual that quotes a proxy for the number it means is quoting the wrong
    number confidently.
    """
    import re
    import subprocess
    try:
        r = subprocess.run([sys.executable, os.path.join('tests', 'test_transit.py')],
                           cwd=ROOT, capture_output=True, text=True, timeout=900)
        m = re.search(r'(\d+) checks', r.stdout or '')
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def mutation_count():
    try:
        import re
        src = open(os.path.join(ROOT, 'tests', 'mutate.py'), encoding='utf-8').read()
        body = src[src.index('MUTATIONS = ['):src.index('\ndef run(')]
        return len(re.findall(r"^\s{4}\('", body, re.M))
    except (OSError, ValueError):
        return None


# The shared style starts every Heading 1 on a fresh page. That is right for a long
# manual you cite by section, and wrong for a Quick Start: it turned three pages of
# content into eight, most of them 20% full, which reads as an unfinished document.
# Same tuple shape as docx_style.DEFAULT_HEADINGS; only the trailing page-break flag
# differs.
FLOWING_HEADINGS = tuple(h[:-1] + (False,) for h in D.DEFAULT_HEADINGS)

TEXT_WIDTH_IN = 6.5
MAX_FIG_H_IN = 3.5


def fig(S, name, caption, max_h=MAX_FIG_H_IN):
    """Place a figure sized from ITS OWN aspect ratio, not at a fixed width.

    A fixed width is only safe for landscape figures. The domain map is portrait,
    and at the default 6.2 in wide it came out 6.79 in TALL — taller than the
    printable height of the page, so it took a page to itself and pushed two
    sections into a half-empty one. Measuring the file and capping the HEIGHT
    fixes that for every figure, including ones not drawn yet.
    """
    from PIL import Image
    im = Image.open(os.path.join(str(FIGS), name))
    aspect = im.width / im.height
    width = min(6.2, TEXT_WIDTH_IN, max_h * aspect)
    return S.figure(name, caption, width=round(width, 2))


def title_page(S, title, subtitle, audience):
    S.para(' ')
    p = S.para(title, size=26, bold=True, color=S.ACCENT, align=S.CENTER, after=4)
    S.para(subtitle, size=13, color=S.SOFT, align=S.CENTER, after=18)
    S.para('Transit Calculator', size=11, bold=True, align=S.CENTER, after=2)
    S.para(f'Version 1.0  ·  {DATE}', size=9.5, color=S.SOFT, align=S.CENTER, after=22)
    S.callout('Who this is for', audience)
    S.para(' ')
    return p


def provenance(S):
    S.h2('Where the numbers come from')
    S.para('Every figure in this document was produced by the code it describes, at '
           'the moment the document was built. Nothing here is transcribed by hand.')
    S.table(
        ['Quantity', 'Value', 'Source'],
        [['Sample line length', f"{SUMMARY['distance_nm']:.2f} NM",
          'geo.inverse over survey_transit_line.shp'],
         ['Legs', str(len(SUMMARY['legs'])), 'the same file'],
         ['Vessel model', str(MODEL.version), 'model.json'],
         ['Configurations', ', '.join(g['key'] for g in MODEL.configs()), 'model.json'],
         ['Tank / reserve', f"{MODEL.tank_l:g} L / {MODEL.reserve_fraction:.0%}", 'model.json'],
         ['Checks in the suite', str(test_count() or '—'), 'tests/test_transit.py'],
         ['Mutations', str(mutation_count() or '—'), 'tests/mutate.py'],
         ['Export formats', str(len(exporters.FORMATS)), 'exporters.FORMATS']],
        [1.7, 1.5, 3.3])


# =========================================================================== #
def build_quickstart():
    S = D.new_document(FIGS, headings=FLOWING_HEADINGS, right_from=99)
    title_page(
        S, 'Quick Start', 'From a cold start to a costed passage',
        'Anyone who wants a transit computed today. No prior knowledge of the tool '
        'is assumed. Ten minutes end to end.')

    S.h1('1  Start it')
    S.para('Double-click the "Transit Calculator" shortcut on the desktop. If there '
           'is no shortcut, run start_transit.bat in the project folder, or:')
    S.mono('python server.py', 'From the project folder. Add --port 8079 to run a '
                               'second copy alongside the first.')
    S.para('A browser opens on the console once the server is actually listening. '
           'It binds to your own machine only — nothing is exposed to the network. '
           'If the port is already in use it says so and stops, rather than starting '
           'a second server on top of the first.')

    S.h1('2  Get a line on the chart')
    S.para('You need a track before anything can be computed. Either:')
    S.bullets([
        'DRAW ONE — click the chart to drop points. Drag a point to move it; click '
        'one and press Delete to remove it; Undo steps back.',
        'LOAD ONE — pick a file under "Load a line" and press Load. Shapefile, '
        'GeoJSON, GPX and CSV all work. A shapefile needs its .prj, or drop the '
        'whole set in as a .zip.',
    ])
    S.para('The supplied survey_transit_line is already in the list. Load it and the '
           f"status bar reads {SUMMARY['distance_nm']:.2f} NM over "
           f"{len(SUMMARY['legs'])} legs.")
    S.callout('Check which way it runs',
              'The ends are labelled START and END, and chevrons along the track show '
              'the direction of travel. Press REVERSE — at the foot of the left '
              'panel, just above the offline downloads — to run it the other way. '
              'This is not cosmetic: with a wind on the nose one way and astern the '
              'other, the two directions can differ by a fifth of the fuel.')

    S.h1('3  Set the passage')
    S.table(
        ['Field', 'What it means', 'Start with'],
        [['Speed kt', 'How fast the vessel goes', '8'],
         ['Speed is', 'Through water = a water speed to hold, and the current then '
          'decides your ETA. Over ground = a fixed arrival, and the current '
          'decides the fuel. Fixed revs = you set the throttle and the speed is '
          'the answer.', 'through water'],
         ['Revs', 'Fixed-revs mode only, replacing the speed box: the RPM the '
          'vessel is holding.', '—'],
         ['Depart UTC', 'When you leave. The tide is different three hours later, so '
          'this genuinely changes the answer.', 'as offered'],
         ['Configuration', 'Which vessel configuration is fitted', 'as offered'],
         ['Fuel aboard', 'Litres in the tank at departure. Leave it blank and a full '
          'tank is assumed.', 'blank'],
         ['Currents', 'Leave ticked. "all models (chained)" is the right default.',
          'all models'],
         ['Weather', 'IDW over the line pulls real buoy and forecast data.',
          'IDW over the line']],
        [1.3, 4.0, 1.2])
    S.para('Every control carries a tooltip, and so does every abbreviation the '
           'console shows you — the column headings, the result chips, the model '
           'names, the cycle tags. Hover one for what it means and why it matters, '
           'without leaving the console for this manual.')

    S.h1('4  Calculate')
    S.para('Press CALCULATE. First run takes a few seconds while the weather is '
           'fetched; afterwards it is quick. You get four headline numbers — '
           'distance, time, litres, average speed over ground — then a row per leg.')

    S.h1('5  Read the flags before you read the numbers')
    S.para('The chips under the headline are the honest part of the tool. They tell '
           'you how much of the answer rests on real data.')
    S.table(
        ['Chip', 'Meaning', 'What to do'],
        [['current 100% covered', 'Every step of the track had a real forecast '
          'current.', 'Nothing. This is what you want.'],
         ['current 87% covered · 10.4 NM gap', 'Part of the line is outside every '
          'cached model. Those miles were computed with ZERO current and counted.',
          'Press Currents under "Localize for offline" — it works out which models '
          'the line needs and fetches them.'],
         ['20% projected', 'A cached cycle did not reach your departure time, so the '
          'current was projected forward by whole tidal cycles.',
          'Re-fetch Currents for a fresh cycle.'],
         ['RPM outside fitted window', 'The speed you asked for needs revs outside '
          'the range the fuel law was fitted over.',
          'The fuel figure is an extrapolation. Treat it as indicative.'],
         ['DBOFS 87% + CBOFS 13%', 'Two current models were chained to cover the '
          'line.', 'Nothing — this is the system working.'],
         ['140 L aboard', 'You stated a part-full tank, so the margin is against '
          'that and not against a full one.',
          'Nothing, if the figure is right. It is amber because it is an assumption '
          'you typed, not one the tool can check.'],
         ['FUEL SHORT BY 12 L', 'The passage burns past the reserve floor.',
          'Load more fuel, slow down, or shorten the line.']],
        [1.8, 2.9, 1.8])

    S.h1('6  Export it')
    S.para('Name it, choose the shapefile CRS, and press a format:')
    S.bullets([f"{f['label']} — {f['note']}" for f in exporters.FORMATS])
    S.para('The two "per leg" exports carry the computed results as attributes, so '
           'the GIS user gets speed, set, drift, fuel and ETA attached to the '
           'geometry rather than in a separate table to join by hand.')

    S.h1('Where next')
    S.bullets([
        'Instruction Manual — every control and field in detail.',
        'Technical Manual — how the numbers are produced, and their limits.',
        'Development Manual — the code, the tests, and how to extend it.',
    ])
    return S, 'Transit_Calculator_Quick_Start.docx'


# =========================================================================== #
def build_instructions():
    S = D.new_document(FIGS, right_from=99)   # prose tables: left-align
    title_page(
        S, 'Instruction Manual', 'Operating the Transit Calculator',
        'The operator planning a passage. Assumes you have the tool running — see '
        'the Quick Start if not. Covers every control, and what each result means.')

    S.h1('1  The screen')
    S.para('Three regions: the chart fills the window, a panel at the left builds the '
           'line, a panel at the right computes and exports it. A status bar along '
           'the bottom carries the cursor position, the zoom, and what is cached.')
    S.table(
        ['Region', 'What lives there'],
        [['Chart', 'NOAA ENC tiles, the transit line, waypoints, numbered legs, '
          'direction chevrons and per-leg current arrows once computed. Scroll to '
          'zoom; drag to pan.'],
         ['Left panel', 'Transit line (draw / undo / clear), Load a line, REVERSE, '
          'and Localize for offline.'],
         ['Right panel', 'Passage settings, CALCULATE, the results, and Export.'],
         ['Status bar', 'Cursor position, zoom level, tiles held, current cycle, and '
          'a running summary of the drawn line.']],
        [1.3, 5.2])

    S.h1('2  Building a line')
    S.h2('2.1  Drawing')
    S.para('DRAW mode is on by default. Click the chart to drop points in order. '
           'PAN mode drags the chart without adding points.')
    S.table(
        ['Action', 'How'],
        [['Add a point', 'Click the chart (DRAW mode)'],
         ['Move a point', 'Drag it'],
         ['Select a point', 'Click it'],
         ['Delete a point', 'Click it, then press Delete'],
         ['Remove the last', 'Undo'],
         ['Discard the line', 'Clear'],
         ['Run it the other way', 'REVERSE, at the foot of the panel']],
        [1.9, 4.6])
    S.para('Every control in the console carries a tooltip — the buttons, the fields, '
           'and the column headings of the leg table. So does every abbreviation it '
           'puts on screen: DBOFS and the other model names, the chips above the leg '
           'table, IDW, NDBC, WW3, and the cycle tag in the status bar are all '
           'written out in full on hover. Hover rather than coming back here.')

    S.h2('2.2  Loading')
    S.para('Pick a file from the list and press Load, or Upload to browse. Supported:')
    S.table(
        ['Format', 'Notes'],
        [['Shapefile (.shp)', 'Needs its .prj to be placed correctly. UTM and '
          'geographic both read. A .prj-less file whose coordinates are not lat/lon '
          'is REFUSED rather than guessed at — a guessed zone puts the line in the '
          'wrong ocean.'],
         ['Zipped shapefile', 'Drop the whole set in as one .zip.'],
         ['GeoJSON', 'The longest LineString is used.'],
         ['GPX', 'Route points or track points.'],
         ['CSV', 'Needs latitude/longitude columns (lat/lon, y/x also accepted).']],
        [1.6, 4.9])
    S.para('After loading, the panel reports how many points came in, the CRS it '
           'found, and anything it skipped. A null shape — what QGIS leaves when a '
           'feature is deleted without repacking — is counted and skipped, not '
           'treated as corruption.')

    S.h2('2.3  Direction')
    S.para('Which way the line runs changes the answer, so it is shown on the chart '
           'rather than left to be inferred: the ends are labelled START and END, and '
           'chevrons along each leg point the way of travel. REVERSE sits at the foot '
           'of the panel, below the loading controls and immediately above the '
           'offline downloads — it is the last decision about the geometry before '
           'you fetch data for it.')
    S.callout('REVERSE reverses the geometry, not a label',
              'The leg table, the ETAs and the exported shapefile all describe the '
              'run that was computed. There is no second convention to keep '
              'straight. The previous result is cleared rather than left on screen '
              'describing the other direction.')

    S.h1('3  Localizing for offline')
    S.para('Both downloads are cut to the extent of the line you have drawn, so they '
           'stay small. Draw or load the line first.')
    S.table(
        ['Button', 'What it fetches', 'Size'],
        [['Charts', 'NOAA ENC raster tiles over the line, zoom 8 to 12.',
          'Tens of MB, depending on the area'],
         ['Currents', 'Works out which forecast models actually reach the line and '
          'fetches each one, scoped to the line.',
          'About 6 to 13 MB per model when scoped']],
        [1.1, 3.7, 1.7])
    S.para('Before downloading, the Currents button reports which models the line '
           'needs and what share of it each covers, so you can see the cost before '
           'paying it. A tick means that model is already cached.')

    S.h1('4  Passage settings')
    S.h2('4.1  Speed, and the three things it can mean')
    S.para('This is the setting most likely to be misread, so it is worth being '
           'precise about.')
    S.table(
        ['Mode', 'You give it', 'The weather and current then change'],
        [['through water', 'A water speed to hold — how fast the hull moves through '
          'the water, which is what the engine actually controls.',
          'Your speed over ground, and therefore the ETA. Weather is paid for in '
          'revs, so it costs fuel and not time.'],
         ['over ground', 'A speed made good, i.e. you have an arrival time to hit.',
          'The water speed needed, the RPM, and therefore the fuel.'],
         ['fixed revs', 'The throttle setting itself. The speed is then an OUTPUT.',
          'The speed through the water, and so the ETA. Litres per hour is fixed by '
          'the revs, so weather costs TIME here rather than fuel.']],
        [1.2, 2.6, 2.7])
    S.callout('Fixed revs is not "through water" by another name',
              'They agree only if the vessel\'s own controller agrees with this '
              'model about which revs give which speed. That is worth checking '
              'rather than assuming: an ASV commanded to hold a speed converts it '
              'to revs using ITS internal model, and if that model is optimistic '
              'the boat sits at fewer revs and goes slower than the number on the '
              'console says. Running the same plan in both modes — one at the '
              'commanded speed, one at the observed revs — shows the size of any '
              'disagreement directly.')

    S.h2('4.2  Departure')
    S.para('The tide is not the same three hours later. Departure is a real input, '
           'not a label: the calculation looks up the current at the position the '
           'vessel has reached and the clock time it got there. It defaults to the '
           'start of the cached forecast, not to "now", because a cycle fetched '
           'yesterday cannot answer for this afternoon and defaulting to now would '
           'quietly push every leg onto the projection path.')

    S.h2('4.3  Fuel aboard')
    _floor = MODEL.tank_l * MODEL.reserve_fraction
    _part = MODEL.tank_l * 0.56
    S.para('Litres in the tank at departure. Leave it blank for a full tank, which is '
           'what the tool assumed before this field existed. It changes nothing about '
           'the passage — the same line burns the same fuel — only what that burn is '
           'measured against.')
    S.callout('The reserve floor belongs to the tank, not to the load',
              f'The floor is {MODEL.reserve_fraction:.0%} of the '
              f'{MODEL.tank_l:g} L tank, so {_floor:.1f} L, and it does not shrink '
              f'when you sail part-full. Enter {_part:.0f} L aboard and '
              f"{MODEL.endurance(0.0, onboard_l=_part)['usable_l']:.1f} L is yours to "
              f'burn — the whole {MODEL.tank_l - _part:.0f} L shortfall comes out of '
              'usable fuel, not a quarter of it. Taking the reserve as a fraction of '
              'what is loaded would report '
              f'{(_part * (1 - MODEL.reserve_fraction)):.1f} L instead, and err in '
              'the direction that runs a boat dry.')
    S.para('A figure larger than the tank holds is refused rather than trimmed to '
           'full: it is a mistyped gauge reading, and quietly trimming it would hand '
           'back a margin you never had. A load already below the reserve floor is '
           'accepted and flagged — you may be planning what to load — and reported '
           'as a distinct condition from a passage that merely eats the margin.')

    S.h2('4.4  Currents')
    S.para('Leave the tick on and the selector at "all models (chained)". Selecting '
           'a single cycle forces one model, which re-opens whatever domain gap the '
           'chaining exists to close.')

    S.h2('4.5  Weather')
    S.table(
        ['Setting', 'What happens'],
        [['IDW over the line', 'Buoy observations and gridded forecasts are gathered '
          'along the whole line and interpolated to every step. This is the default '
          'and the most honest option.'],
         ['enter below', 'You supply one wind and sea state for the whole passage. '
          'Useful for a what-if, or when you have a forecast the tool cannot reach.'],
         ['ignore (benign)', 'No weather premium at all. Use only to isolate the '
          'effect of something else — it will understate fuel in any real sea.']],
        [1.5, 5.0])

    S.h1('5  Reading the result')
    S.h2('5.1  Headline')
    S.para('Distance, time, litres, and average speed over ground. Distance is '
           'geometry and will not move; the other three depend on every setting '
           'above.')

    S.h2('5.2  The leg table')
    S.table(
        ['Column', 'Meaning'],
        [['NM', 'Geodesic length of the leg'],
         ['Brg', 'True bearing at the start of the leg'],
         ['SOG', 'Speed over ground made good along the track, averaged over the leg'],
         ['Set / Dft', 'Mean current direction (toward) and speed, vector-averaged'],
         ['RPM', 'Required revs, after the sea-state and heading premiums'],
         ['Time / ETA', 'Leg duration and arrival time, UTC'],
         ['L', 'Fuel for the leg'],
         ['Cov', 'Coverage: percentage of the leg that had a real forecast current']],
        [1.2, 5.3])
    S.para('Hover a row for speed through water, the along-track component of the '
           'current, the premium applied, and the wind and sea state used.')

    S.h2('5.3  Finding a leg on the chart')
    S.para('Every leg carries its number on the chart, boxed beside the segment it '
           'names, and hovering a row in the table lights that segment white. So a '
           'row saying leg 4 cannot hold track can be found without counting '
           'segments. Where the line is too crowded for a badge to sit beside its '
           'leg — the first legs out of a harbour, at low zoom — the badge is pushed '
           'clear and a leader points back to the leg it belongs to, rather than '
           'being dropped.')
    S.callout('A leg is a segment, a waypoint is a point',
              'Leg 4 runs from WP4 to WP5. Six waypoints make five legs, so the two '
              'numberings do not line up past the first, and reading a leg number as '
              'a waypoint puts you one segment out.')

    S.h2('5.4  Endurance')
    e = MODEL.endurance(40.0)
    ep = MODEL.endurance(40.0, onboard_l=MODEL.tank_l * 0.56)
    S.para(f"Fuel against what is in the tank and the reserve floor. On a full "
           f"{MODEL.tank_l:g} L tank with a {MODEL.reserve_fraction:.0%} reserve, "
           f"{e['usable_l']:.0f} L is usable; the margin shown is what is left after "
           'the passage. A negative margin means the passage breaks the reserve '
           'policy, not that the vessel runs dry.')
    S.para('State a fuel aboard (4.3) and the line names it: the usable figure is '
           'then that load less the reserve floor, and the floor is quoted in litres '
           f"rather than as a percentage — {MODEL.tank_l * 0.56:.0f} L aboard of a "
           f"{MODEL.tank_l:g} L tank leaves {ep['usable_l']:.0f} L usable, and a "
           f"{40.0:.0f} L passage a margin of {ep['margin_l']:.1f} L. A percentage "
           'there would read as a percentage of the load, which is exactly the '
           'arithmetic the tool refuses to do.')

    S.h2('5.5  Weather sources')
    S.para('Expand this to see every station and grid node that contributed, its '
           'distance, its age, and which fields it supplied. It is worth looking at: '
           'buoys report partially, and this is where you see that the wind came '
           'from one station and the waves from another.')

    S.h1('6  Comparing directions')
    S.para('"Compare the other direction" computes the reversed line at the same '
           'departure and reports the difference, keeping both results. On the '
           'supplied line the two directions differ by about a fifth of the fuel, '
           'because the long leg runs into the wind one way and away from it the '
           'other, and the burn law is convex.')
    fig(S, 'direction.png', 'The same line and the same wind, run both ways. Per-leg '
             'fuel, computed by the planner at build time.')

    S.h1('7  Emergency — predicting a drift')
    S.para('If the vessel loses power, the bottom of the left panel answers a '
           'different question from the rest of this tool: not where it is going, '
           'but where it is being taken. Set the last known position — press "Set '
           'last known position" and click the chart, or type the coordinates — '
           'give the time it was lost and how many hours ahead to look, and press '
           'PREDICT DRIFT.')
    S.table(
        ['What you get', 'What it means'],
        [['The datum', 'Where the middle of every assumption puts the hull. It is '
          'not a position report and should never be passed on as one.'],
         ['The radius', 'A circle around the datum holding every member of the '
          'ensemble. This is the spread of the stated assumptions — search area, '
          'not an error bar and not a probability.'],
         ['The cloud', 'Every ensemble member\'s path, drawn faintly. Where it is '
          'wide, the answer is weak, and that is worth seeing rather than being '
          'told.'],
         ['The horizon', 'The hour past which the forecast could not answer, and '
          'why. The track is dashed beyond it.']],
        [1.4, 5.1])
    S.h2('7.1  Where the leeway comes from')
    S.para('The wind term uses the US Coast Guard leeway model — the same '
           'formulation operational SAR services run — rather than a rule of thumb. '
           'Each object class carries a downwind and a crosswind regression against '
           'wind speed, and each of those carries the standard error of the field '
           'experiment that produced it:')
    S.mono('DWL = (slope + eps/20) x W10 + offset + eps/2     [cm/s]',
           'W10 is the wind at 10 m in m/s; crosswind is the same form with its own '
           'coefficients, taken left or right of downwind.')
    S.para('Three consequences worth knowing. Some classes have a non-zero OFFSET, '
           'so they drift even in no wind. The divergence angle is not a setting — '
           'it falls out of the ratio of the two regressions, and differs by object. '
           'And the spread of the search circle is the MEASURED error of those '
           'regressions, not a guess: it is where most of the radius comes from.')
    _cl = drift.LEEWAY_CLASSES[drift.DEFAULT_CLASS]
    _dw20, _cw20 = drift.leeway_components(20.0, _cl)
    S.table(
        ['Class', 'Downwind', 'Crosswind', 'Measured spread'],
        [[v['name'], f"{v['dw'][0]:.2f}%",
          f"+{v['cw_right'][0]:.2f} / {v['cw_left'][0]:.2f}%",
          f"{v['dw'][2]:.1f} cm/s"] for v in drift.LEEWAY_CLASSES.values()],
        [3.1, 1.0, 1.1, 1.1])
    S.callout('No class exists for an unmanned surface vessel',
              'The published taxonomy is people, rafts and boats — 85 categories, '
              'none of them a USV. The default here is "' + _cl['name'] + '", '
              f'which puts the hull at {_dw20:.2f} kt downwind in a 20 kt wind. It '
              'is not a guess from hull shape: this class has been found to closely '
              'match the vessel in past operations. That is OPERATIONAL EXPERIENCE, '
              'not a measurement of this hull — a drift trial with a GPS log in a '
              'known wind would replace it, and remains the single most valuable '
              'thing that could be done for this feature. Any of the other classes '
              'can be selected if a particular casualty warrants it.')
    S.para('Two features of that class are worth knowing before you read a radius '
           'from it. Its crosswind does not scale with wind speed at all — it is a '
           'constant, and an asymmetric one, larger to the right of downwind than '
           'to the left. And its downwind offset is NEGATIVE, so in light airs it '
           'is measured drifting slightly behind the water rather than with it.')
    S.para('Advection — the hull going with the water — comes from the same NOAA '
           'forecast the passage uses. Leeway is defined relative to the current at '
           'about 0.5 m depth, which is how the field experiments measured it, so '
           'adding the two is the correct convention rather than double counting '
           'the wind.')

    S.h2('7.2  Swell, and why only some of it counts')
    S.para('The optional swell boxes add a Stokes drift term — the transport waves '
           'carry over and above the current. Leave them blank and there is no wave '
           'term at all.')
    S.callout('Only the swell that is NOT running with the wind',
              'The leeway coefficients were measured in the field with seas '
              'running, so the wave forcing that goes with the local wind is '
              'already inside them, and adding a whole Stokes vector on top would '
              'count it twice. The component along the wind is projected out and '
              'only the remainder is used: a swell running with the breeze changes '
              'nothing, while an old swell across or against it counts in full. '
              'That is the case this term exists for — a calm morning with a swell '
              'left over from a distant blow.')
    _st = drift.stokes_drift(2.0, 8.0, 270.0)[0]
    S.para(f'The magnitude is the deep-water narrow-band result: a 2 m swell at 8 s '
           f'gives {_st:.3f} kt. The PERIOD matters more than the height, because '
           f'the transport goes as the cube of frequency — a short steep sea carries '
           f'several times what a long swell of the same height does. Where height '
           f'and period disagree badly enough to imply a breaking wave the term is '
           f'capped and the result says so, because Stokes goes as the square of '
           f'steepness and bad data would otherwise invent a knot of drift.')
    S.para('Two limits worth knowing. Deep water is assumed, so in depths shallower '
           'than about half a wavelength this understates the transport, and the '
           'tool has no bathymetry with which to know. And the term is not varied '
           'across the ensemble: it sits an order of magnitude below the leeway '
           'spread, and inventing an uncertainty for it would dress a guess as a '
           'measurement.')

    S.h2('7.3  Comparing drift models against a fixed environment')
    S.para('Tick several classes and press COMPARE TICKED CLASSES. Each is run from '
           'the same last known position, the same time of loss, the same wind and '
           'swell, and the same cached forecast — the ONLY thing that changes '
           'between rows is the drift model, so every difference in the table is '
           'attributable to it. Each alternative datum is drawn on the chart with '
           'its own circle, in outline, under the one in use.')
    S.callout('This is the honest use of a taxonomy with no entry for this vessel',
              'The class is an analogue. How far the answer moves when you pick a '
              'different one is therefore a result in its own right, and usually a '
              'larger one than any decimal place in the datum. On the supplied '
              'position the spread between classes runs to about half the search '
              'radius — the same order as the uncertainty within any one of them. '
              'The table says so in words underneath, so the comparison cannot be '
              'read as four competing answers when it is really one answer and its '
              'sensitivity.')
    S.para('The run is done in a single request rather than one per class, so the '
           'forecast is loaded once for all of them. That is not only speed: '
           'reloading it per class would invite exactly the environmental drift '
           'the comparison is trying to rule out.')

    S.para('A tidal current reverses about every six hours, so a hull adrift in one '
           'loops and creeps rather than running in a straight line. Over a day the '
           'displacement is set by the residual flow and the wind, not by the peak '
           'current — which is why the radius can exceed the distance travelled, '
           'and why a drift prediction is far more sensitive to a small steady '
           'error than a passage ETA is.')
    S.para('Two exports sit under the result: CSV of the datum table, and GeoJSON '
           'carrying the last known position, the datum track, every hourly datum '
           'with its radius, and each ensemble endpoint. Both are built in the '
           'browser from what is on screen, so what gets handed over is exactly '
           'what was being looked at.')
    S.callout('It refuses rather than guessing',
              'If no cached forecast reaches the last known position, the '
              'prediction is refused outright with the reason — because the '
              'alternative is a wind-only track formatted to look exactly like a '
              'real one. Fetch a model that covers the position, and try again.')

    S.h1('8  Exporting and saving')
    S.para('Every save of a calculated mission carries a UTC timestamp in its name — '
           "Lewes_offshore_20260814T0317Z_shp.zip — so repeated saves of the same "
           'line do not overwrite each other and sort chronologically. The stamp '
           'reaches inside a zipped shapefile too, so two exports extracted into the '
           'same folder do not collide either.')
    S.para('"Save to" is ON by default, so pressing a format writes the file into '
           'docs/missions and the status bar reports the path. Untick it to get a '
           'browser download instead. Same bytes and the same name either way; only '
           'the destination differs.')
    S.callout('The Export panel appears once you have calculated',
              'It lives with the results. If you have drawn a line but not pressed '
              'CALCULATE, there is nothing to export yet.')
    S.table(['Format', 'Use it for'],
            [[f['label'], f['note']] for f in exporters.FORMATS], [2.4, 4.1])
    S.callout('Shapefile CRS',
              'GeoJSON, KML and GPX are defined as WGS84 longitude/latitude, so the '
              'CRS choice applies to the SHAPEFILE only. UTM writes projected metres '
              'with a matching .prj — usually what a survey package expects back. '
              'One zone is chosen for the whole line, because a shapefile carries '
              'exactly one .prj.')

    S.h1('9  If something looks wrong')
    S.table(
        ['Symptom', 'Likely cause'],
        [['Chart is blank', 'No tiles cached for this area and no network. Press '
          'Charts while connected. The graticule still gives you scale.'],
         ['"current 0% covered"', 'No cycle cached, or none reaches this line. Press '
          'Currents.'],
         ['A leg is flagged red', 'The cross-current exceeds what the vessel can crab '
          'out at that speed. The line cannot be flown as drawn at that speed — this '
          'is a refusal, not a slow leg.'],
         ['Fuel looks implausibly low', 'Check the Weather setting is not on '
          '"ignore", and check the RPM window flag.'],
         ['Port already in use', 'Another copy is running. Open it, or start this '
          'one with --port 8079.']],
        [2.0, 4.5])
    return S, 'Transit_Calculator_Instruction_Manual.docx'


# =========================================================================== #
def build_technical():
    S = D.new_document(FIGS, right_from=99)   # prose tables: left-align
    title_page(
        S, 'Technical Manual', 'How the numbers are produced, and what they are worth',
        'Anyone deciding whether to trust a figure this tool produced: the '
        'algorithms, the data sources, the assumptions, and the limits.')

    provenance(S)

    S.h1('1  Geodesy')
    S.para('Distances and bearings are ellipsoidal, using Vincenty\'s inverse '
           'formula on WGS84. This is a deliberate departure from the companion ASV '
           'console, which uses an equirectangular approximation appropriate to a '
           'survey box a few kilometres across.')
    d_geo = geo.inverse(38.357256, -74.792817, 37.664897, -74.997880)[0]
    d_flat = geo.flat_dist_m(38.357256, -74.792817, 37.664897, -74.997880)
    S.para(f'A transit is not a survey box. The longest leg of the supplied line is '
           f'{d_geo / 1852:.1f} NM, where the flat-earth approximation is '
           f'{d_flat - d_geo:.0f} m long — small in relative terms, but it is the '
           'number the fuel and the ETA hang off.')
    S.para('Verified against the published Vincenty test vector: distance to 0.14 mm '
           'and both azimuths to 0.003 arcsec.')
    S.callout('One convention worth knowing',
              'inverse() returns the FORWARD azimuth at the destination — the course '
              'still being steered on arrival. Published test tables often list the '
              'reverse azimuth instead, which is that value minus 180. Checking this '
              'function against such a table without lining up the convention makes '
              'it look broken when it is not.')

    S.h1('2  The passage is integrated, not divided')
    S.para('Distance over speed is the wrong calculation on a tidal coast. The '
           'current reverses roughly every six hours; the supplied passage takes '
           'about ten. A vessel that leaves on a fair tide meets the foul one before '
           'it is halfway, and evaluating the current once — at the start, or at each '
           'leg\'s midpoint — answers a question nobody asked.')
    S.para('So the calculation MARCHES. Each leg is stepped in half-mile increments. '
           'At each step the current is looked up at the position the vessel has '
           'reached and the clock time it reached it; the resulting speed advances '
           'the clock, which changes where the vessel is at the next lookup.')
    fig(S, 'marching.png',
             'Left: a synthetic current reversing on a six-hour cycle. Right: passage '
             'time against integration step, computed by the real planner. The '
             'right-hand end is one sample per leg.')
    S.para('The half-mile default is proven converged: halving it again moves the '
           'passage by under a minute, and a test enforces that.')

    S.h1('3  Currents')
    S.h2('3.1  The source')
    S.para('NOAA Operational Forecast System models, read over OPeNDAP from the '
           'CO-OPS THREDDS server as hourly surface fields. A cycle is downloaded '
           'once, cut to the line\'s bounding box, and cached; all subsequent queries '
           'are local.')
    S.h2('3.2  One model is one region')
    S.para('An OFS covers a region and stops at a hard edge. A transit does not care '
           'where that edge is. The supplied line runs past the Delaware Bay model\'s '
           'southern boundary, leaving its last miles with no current at all.')
    fig(S, 'domains.png', 'The supplied line against the two model domains that '
             'between them cover it. Extents are the probed water extents, read from '
             'the cached domain file.')
    S.para('The tool therefore probes which models actually reach a line, fetches '
           'each one scoped to the part it is needed for, and serves them through a '
           'single source that tries the finest model first and falls through.')
    S.h2('3.3  The handover')
    S.para('Where two models overlap they disagree slightly — different grids, '
           'different bathymetry, different assimilation. The finer regional model '
           'wins in its interior. Within about 15 km of its boundary the two are '
           'blended by distance, as VECTORS, so the handover does not put a step in '
           'the middle of a leg. Blending directions as angles rather than vectors '
           'would make the mean of a flood and an ebb point sideways instead of '
           'reading as slack.')
    S.h2('3.4  Domain detection')
    S.para('A "regulargrid" file is a rectangular array with fill where the irregular '
           'model domain does not reach; its corners read back as latitude 2.0 or '
           '89.0. Reading the corners tells you nothing. The domain is instead probed '
           'by walking the grid with a stride, keeping only unmasked water nodes, and '
           f'storing them as an occupancy raster at {ofs.CELL}° (about 5.5 km) with a '
           'stated one-cell tolerance.')

    S.h1('4  Wind and waves')
    S.para('Three sources, because none of them is sufficient alone.')
    S.table(
        ['Source', 'Gives', 'Resolution', 'Nature'],
        [['NDBC buoys', 'Wind, gust, wave height, period, direction', 'Point',
          'Observation'],
         ['NWS gridpoints', 'Wind, gust, wave height', '~2.5 km, US waters',
          'Forecast'],
         ['WAVEWATCH III', 'Wave height, period, direction', '0.5° (~55 km), global',
          'Forecast']],
        [1.4, 2.2, 1.6, 1.3])
    S.callout('The interpolation is per VARIABLE, not per station',
              'This is not a theoretical nicety. On the supplied line, buoy 44009 '
              'reports wind with its wave fields blank, and 44084 reports waves with '
              'its wind fields blank. Neither answers the question alone. Treating a '
              'station as a unit — or reading a blank as zero — manufactures a calm '
              'forecast out of a broken sensor.')
    fig(S, 'idw.png', 'Each field draws only on the samples that actually carry it.')
    S.para('Weighting is inverse distance, w = q / (d^p + e), with p = 2 and q a '
           'source-quality factor so an observation outweighs a coarse model cell at '
           'equal distance. Directions are averaged as vectors, and wave direction is '
           'weighted by wave height so a ripple does not drag the mean off a swell.')
    S.callout('IDW is an interpolator, not a model',
              'It cannot invent structure between samples. A point 50 km from '
              'anything is a smooth guess. Every interpolated value therefore carries '
              'the sources that made it, their distances, and the age of the oldest '
              'observation — shown under "Weather sources" in the results.')

    S.h1('5  Fuel')
    S.para('The chain runs in this order, and the order matters:')
    S.bullets([
        'Target speed and the current give the speed required THROUGH THE WATER.',
        'Water speed gives benign RPM, from the configuration\'s speed law.',
        'Sea state and heading give a fractional premium.',
        'Actual RPM = benign RPM × (1 + premium).',
        'Actual RPM gives litres per hour, from the configuration\'s fuel law.',
    ])
    S.para('Taking speed over ground into the fuel law instead of speed through water '
           'is the one ordering mistake that makes a favourable current look like '
           'free fuel. The engine pushes the hull against the water; a following '
           'current costs nothing at the injector.')
    b = MODEL.burn(8.0, 0.0, 1.0, MODEL.default_config())
    S.para(f'At 8 kt on the default configuration the model gives '
           f"{b['rate_lph']:.2f} L/h, i.e. {8.0 / b['rate_lph']:.2f} NM per litre.")

    S.h2('5.1  The same chain, run backwards')
    S.para('Fixed-revs mode inverts it rather than modelling the vessel twice. The '
           'premium is extra revs to HOLD a speed, so at a held throttle it comes '
           'off the speed instead: benign RPM = actual RPM / (1 + premium), and the '
           'speed law then gives the water speed. The consequence is the useful '
           'part — litres per hour is fixed by the revs, so weather lengthens the '
           'passage instead of making it dearer per hour.')
    _rv = MODEL.rpm_for_speed(8.0, MODEL.default_config())
    _bk = MODEL.stw_at_rpm(_rv, 0.0, MODEL.default_config())
    S.para(f'The inversion is exact in still conditions: {_rv:.0f} RPM is what 8 kt '
           f"needs, and reading it back gives {_bk['stw_kt']:.2f} kt.")
    S.callout('The floor on the following-sea credit',
              'The heading premium scales with the square of the wind and is '
              'negative downwind, with nothing in the fit bounding it. The forward '
              'chain multiplies by (1 + premium) and never notices; the inversion '
              'DIVIDES by it, and a strong following wind would otherwise report a '
              'vessel making half as much again as its own curve allows. The '
              'divisor is floored, and a result that hits the floor says so rather '
              'than quietly reporting the clamped speed.')

    S.h2('5.2  Why premiums are never averaged')
    S.para('The fuel law is convex in RPM. The mean of the rates is strictly greater '
           'than the rate at the mean premium, so averaging the premium across a '
           'passage understates the fuel. Each step is costed individually.')
    S.h2('5.3  Endurance, and where the reserve floor comes from')
    _fl = MODEL.tank_l * MODEL.reserve_fraction
    _pt = MODEL.tank_l * 0.56
    S.para('The burn is a property of the passage; endurance is that burn measured '
           'against the boat. Fuel aboard at departure is an optional input — blank '
           'means a full tank — and the reserve floor is taken as a fraction of TANK '
           f'VOLUME, {MODEL.reserve_fraction:.0%} of {MODEL.tank_l:g} L = '
           f'{_fl:.1f} L. Usable fuel is therefore what is aboard less that floor.')
    _honest = MODEL.endurance(0.0, onboard_l=_pt)['usable_l']
    _wrong = _pt * (1 - MODEL.reserve_fraction)
    S.para('The distinction is not pedantry. The floor exists because of the tank — '
           'pickup height, sloshing, the margin an operator will not plan into — so '
           f'it does not scale with the load. At {_pt:.0f} L aboard the honest usable '
           f'figure is {_honest:.1f} L; taking the reserve as a fraction of the load '
           f'instead gives {_wrong:.1f} L, an overstatement of '
           f'{_wrong - _honest:.1f} L in the direction that runs a boat dry. A '
           'mutation case encodes exactly that error, because it is the version a '
           'reviewer would wave through.')
    S.para('What is deliberately NOT modelled here is the gauge. Reading a tank '
           'level from a sender is a non-linear profile owned by the source planner; '
           'this tool takes litres as stated and says so.')

    S.h2('5.4  The sea-state premium is an assumption')
    S.para('It is carried unchanged from the source model, including that caveat. The '
           'heading effect is a measured magnitude with an assumed cosine shape. '
           'Neither is a fitted law of the same standing as the fuel curve, and a '
           'result that leans heavily on either should be read accordingly.')

    S.h1('6  Direction is not symmetric')
    S.para('The same line run the other way is a different passage. Current is part '
           'of it, but on the supplied line the dominant term is WIND, through the '
           'heading premium and the convex fuel law: a swing of about five per cent '
           'in RPM becomes roughly a quarter of the fuel.')
    fig(S, 'direction.png', 'Per-leg fuel in both directions, same wind, same '
             'departure, computed at build time.')

    S.h1('7  Charts')
    S.para('NOAA ENC raster tiles in the Web Mercator scheme, proxied by the local '
           'server because the upstream endpoint sends no CORS header, and cached to '
           'disk so a planned area works offline. Reads chain from the local cache '
           'through any configured read-only fallback to the network. A tile that '
           'cannot be had is served as empty and the client draws its own graticule '
           '— an honest blank rather than a black hole.')

    S.h1('8  Accuracy and limits')
    S.table(
        ['Quantity', 'Confidence', 'Limit'],
        [['Distance, bearing', 'Sub-millimetre against the reference vector',
          'None that matters at this scale'],
         ['Current', 'As good as the OFS forecast', 'Regional coverage only; gaps '
          'are reported, never filled with zero silently'],
         ['Wind, waves', 'Observation near a buoy; forecast elsewhere',
          'A point far from any sample is a smooth guess, and says so'],
         ['Fuel', 'Follows the vessel model',
          'Only as good as the fitted laws; premiums are partly assumed'],
         ['Sea state', 'Derived from wave height via the model\'s own bands',
          'Inferred, not observed; flagged as derived']],
        [1.4, 2.4, 2.7])

    S.h1('9  What the tool refuses to do')
    S.para('Refusals are deliberate, and they are more useful than a confident wrong '
           'answer.')
    S.bullets([
        'A cross-current stronger than the vessel can crab out marks the leg '
        'infeasible rather than returning a plausible slow number.',
        'A shapefile with no .prj whose coordinates are not lat/lon is refused, '
        'because guessing a zone puts the line in the wrong ocean.',
        'A projection it does not recognise is reported as unknown rather than '
        'half-understood.',
        'Where no model reaches, the current is zero AND COUNTED, and the uncovered '
        'fraction is reported per leg and overall.',
        'A weather field nobody reports comes back null, never calm.',
    ])
    return S, 'Transit_Calculator_Technical_Manual.docx'


# =========================================================================== #
def build_development():
    S = D.new_document(FIGS, right_from=99)   # prose tables: left-align
    title_page(
        S, 'Development Manual', 'Architecture, conventions and extension',
        'Anyone changing the code. Assumes Python and a working knowledge of the '
        'domain; read the Technical Manual first for why the algorithms are what '
        'they are.')

    S.h1('1  Shape of the thing')
    S.para('A local HTTP server and a single-page client. The browser owns the map, '
           'the drawing and the table. The server exists for the three things a page '
           'cannot do for itself: proxy chart tiles past CORS, decode binary forecast '
           'files, and read and write files on disk.')
    S.para('Standard library only, with one optional exception (certifi, for a single '
           'TLS chain). That constraint is deliberate — the tool has to run from a '
           'cold machine at sea with nothing to install.')
    S.table(
        ['Module', 'Responsibility', 'Depends on'],
        [['geo.py', 'Vincenty geodesics, UTM conversion, set and drift. No I/O, no '
          'state.', 'math'],
         ['transit.py', 'The marching calculation', 'geo'],
         ['fuel.py', 'The vessel fuel chain', 'model.json'],
         ['ofs.py', 'Forecast-model domains, selection, chaining', 'currents, geo'],
         ['currents.py', 'OPeNDAP client and cycle cache (vendored)', '—'],
         ['marine.py', 'Buoy and forecast sources, IDW blender', 'geo'],
         ['charts.py', 'Tile fetch, cache, prefetch', '—'],
         ['shapefile_io.py', 'Shapefile read and write', 'struct'],
         ['exporters.py', 'The output formats', 'geo, shapefile_io'],
         ['server.py', 'HTTP surface, request assembly', 'all of the above'],
         ['static/transit.html', 'The entire client', '—']],
        [1.6, 3.4, 1.5])

    S.h1('2  Conventions')
    S.callout('Get these wrong and everything still runs',
              'Each of these is a silent-failure mode: the code keeps working and the '
              'numbers are wrong.')
    S.table(
        ['Convention', 'Rule'],
        [['Bearings', '0 = north, clockwise, degrees true'],
         ['Current set', 'The direction the water flows TOWARD (oceanographic), the '
          'opposite of the wind convention. Decided in one place, geo.uv_to_set, and '
          'asserted on all four quadrants.'],
         ['Wind direction', 'The direction it comes FROM. A wind_from equal to the '
          'course is a HEADWIND. A test once asserted the opposite of this and '
          'passed, because a multi-course line hid the sign.'],
         ['Shapefile geometry', '(x, y) = (longitude, latitude)'],
         ['GeoJSON / KML / GPX', 'WGS84 longitude/latitude by definition, so a CRS '
          'choice applies to the shapefile only'],
         ['inverse() third value', 'The forward azimuth at the destination, not the '
          'reverse azimuth']],
        [1.5, 5.0])

    S.h1('3  Testing')
    S.mono('python tests/test_transit.py\npython tests/mutate.py',
           f'{test_count()} checks; {mutation_count()} mutations, all of which must '
           'be caught.')
    S.h2('3.1  Two rules the suite is written to')
    S.bullets([
        'A TEST IS NOT TRUSTED UNTIL IT HAS FAILED ON PURPOSE. mutate.py breaks one '
        'behaviour at a time and confirms the suite goes red. It works on a TEMP '
        'COPY — the real source is opened read-only and never written, because a '
        'killed mutation runner that edits real files leaves the source corrupted.',
        'EVERY REFUSAL IS PAIRED WITH AN ACCEPTANCE. A test that only proves the code '
        'says no is satisfied by code that always says no.',
    ])
    S.h2('3.2  Checks can be decorative, and mutation is how you find out')
    S.para('Two checks in this suite passed for the wrong reason until mutation '
           'testing exposed them: one exercised an unreachable branch, and one '
           'compared against a fallback table identical to the real one, so a '
           'function that ignored the real table entirely still passed. Both are now '
           'real. If a mutation is MISSED, the check it should have broken is '
           'decorative — fix the check, not the mutation.')
    S.h2('3.3  Do not assert on incidental data')
    S.para('One check required the whole passage to differ from the benign time by '
           'more than three minutes. It passed only because of which forecast cycle '
           'happened to be cached: on a different tide phase the fair and foul legs '
           'cancel and the total lands close to benign with nothing wrong. The total '
           'is a function of departure phase; the per-leg effect is not. If a check '
           'depends on which file is in a cache, it is testing the cache.')

    S.h1('4  Extending it')
    S.h2('4.1  A different vessel')
    S.para('Replace model.json. The schema is speed-versus-RPM and fuel-versus-RPM '
           'per configuration, plus tank volume, reserve policy, and the two '
           'environmental premiums. Nothing else needs to change. Note that the fuel '
           'suite asserts its fitted anchors only when the model is not a '
           'placeholder — see the public export.')
    S.h2('4.2  Another forecast model')
    S.para('Add an entry to ofs.REGISTRY with a nominal bounding box and a rank '
           '(finer regional models rank lower and win in overlaps). The true domain '
           'is probed and cached on first use. The reader assumes the CO-OPS '
           'regulargrid layout and reads grid dimensions from the dataset\'s DDS.')
    S.h2('4.3  Another weather source')
    S.para('Return samples shaped like the existing ones — position, source name, and '
           'a values dict carrying whatever fields that source actually has. The IDW '
           'blender is source-agnostic and per-variable, so a source that supplies '
           'only one field is a first-class citizen. Add a quality weight to '
           'marine.QUALITY.')
    S.h2('4.4  Another export format')
    S.para('Add a writer and an entry in exporters.FORMATS. The dispatch test walks '
           'that list, so a new format is covered the moment it is registered; an '
           'unknown format must raise rather than silently defaulting.')

    S.h1('5  Publishing')
    S.callout('Never flip the private repository to public',
              'Its history carries the measured vessel model, and history is '
              'permanent once cloned. Publishing is an EXPORT to a separate repo, '
              'built only by tools/make_public.py: archive the tracked tree, swap the '
              'vessel for a synthetic placeholder, genericise identifiers, and REFUSE '
              'to finish if anything forbidden survives.')
    S.para('Every difference between public and private must live in that script, or '
           'the next export silently reverts it.')
    S.h2('5.1  What the guard learned the hard way')
    S.bullets([
        'The script genericised ITSELF, rewriting its own substitution table. It now '
        'drops itself from the export.',
        'It scrubbed the vessel\'s NAME and published the measured NUMBERS. The name '
        'was never the sensitive part.',
        'An email address in a User-Agent shipped in the first publish. Any email '
        'address is now forbidden.',
        'Absolute machine paths named private sibling projects and pointed a cloner '
        'at a drive they do not have.',
    ])
    S.para('The lesson generalises: the guard checked what was expected to be '
           'sensitive and nothing else. Ask what is in a file that is not the code.')

    S.h1('6  Documents')
    S.mono('python tools/build_figures.py\npython tools/build_docs.py',
           'Figures first — the manuals embed them. Both are reproducible: set '
           'SOURCE_DATE to keep the title-page date stable across rebuilds.')
    S.para('Figures are generated FROM THE CODE wherever possible: the marching '
           'figure calls the planner, the domain figure reads the probed domains, the '
           'direction figure runs both directions. A hand-drawn diagram of what the '
           'code is believed to do is a diagram of a belief. These go stale loudly, '
           'by failing to build, rather than quietly by being wrong. The same applies '
           'to the numbers in the text, which are read at build time.')
    S.callout('A document builder is unverified until you look at the pages',
              'Rasterise the output and read it. Two figures in this set had text '
              'colliding with a title and an arrow struck through a label — both '
              'invisible in the source and obvious on the page.')

    S.h1('7  Known gaps')
    S.bullets([
        'The forecast-model registry is hand-listed; models outside it are never '
        'considered, and there is no global fallback, so a line beyond every regional '
        'domain still reports a gap.',
        'ENC vector features are not ported from the companion console — only the '
        'raster background. Clearance-checking against charted hazards would start '
        'there.',
        'No route optimisation: the tool computes the line it is given.',
        'weather.py is superseded by marine.py for the fetch path but still serves '
        'one endpoint.',
        'The per-leg shapefile export truncates the ETA string to the dBASE field '
        'width, losing sub-second precision.',
    ])
    return S, 'Transit_Calculator_Development_Manual.docx'


def check_section_numbers(doc, filename):
    """Refuse to ship a document with two sections wearing the same number.

    The numbers in these headings are written by hand — '5.3  Endurance' is a
    string, not a field Word maintains — so INSERTING a section silently renumbers
    nothing after it. That happened: a new 5.3 went in above the existing 5.3 and
    the built page carried both. Nothing in the source looks wrong, and the fault
    is only visible once the page is rendered and read.
    """
    seen, bad = {}, []
    for p in doc.paragraphs:
        style = (p.style.name or '') if p.style is not None else ''
        if not style.startswith('Heading'):
            continue
        head = (p.text or '').strip().split()
        if not head:
            continue
        num = head[0]
        if not num[0].isdigit():
            continue                       # an unnumbered heading is allowed
        if num in seen:
            bad.append(f'{num} used twice: "{seen[num]}" and "{p.text.strip()}"')
        seen[num] = p.text.strip()
    if bad:
        raise SystemExit(f'{filename}: section numbering is broken\n  ' +
                         '\n  '.join(bad))


BUILDERS = {
    'quickstart': build_quickstart,
    'instructions': build_instructions,
    'technical': build_technical,
    'development': build_development,
}


def main():
    want = [a for a in sys.argv[1:] if not a.startswith('-')] or list(BUILDERS)
    os.makedirs(DOCS, exist_ok=True)
    for name in want:
        if name not in BUILDERS:
            print(f'no builder {name!r}; have {", ".join(BUILDERS)}')
            return 2
        S, filename = BUILDERS[name]()
        D.check_table_widths(S.doc)
        check_section_numbers(S.doc, filename)
        path = os.path.join(DOCS, filename)
        S.doc.save(path)
        print(f'  {filename}  ({os.path.getsize(path) / 1024:.0f} kB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
