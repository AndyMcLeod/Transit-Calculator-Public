# Transit Calculator

> **The vessel model in this repo is a PLACEHOLDER.** `model.json` holds
> invented, round coefficients chosen to be physically plausible so the
> worked examples run — they are not a measurement of any real hull, and
> must not be used to plan a real mission. Everything else — the geodesy,
> the marching integration, the current chaining, the IDW blender, the
> shapefile I/O — is the real implementation. Drop in your own fitted
> speed and fuel laws and the tool is immediately useful.

A browser tool for planning a vessel transit along a drawn or imported line: real
distances and bearings, tidal currents resolved along the track, per-leg weather,
fuel burn against the vessel model, and export in the formats other tools read —
including Shapefile.

Built from parts that already existed: the ASV Console's NOAA ENC chart download and
its transit-line drawing capability, and the companion fuel planner's current and fuel
models.

```bash
python server.py
```

It opens your browser once it is listening. `start_transit.bat` does the same on
Windows, and

```powershell
powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
```

puts a desktop shortcut on it. The `.lnk` is not tracked — it is a binary carrying
absolute paths for one machine — but the script and `tools/transit.ico` are, and
`tools/make_icon.py` redraws the icon, so nothing here is an undocumented binary. Binds loopback only; `--no-browser` suppresses the open, `--port N` moves it.
If the port is already taken it says so and stops rather than starting a second server
on top of the first.

---

## What it does

**Draw a line, or bring one in.** Click the chart to drop transit points; drag to
move one, click and press Delete to remove it, Undo to step back. Or load an existing
line — Shapefile (with its `.prj`, or the whole set as a `.zip`), GeoJSON, GPX or CSV.

**Which way it runs is on the chart.** The ends are labelled **START** and **END**,
and chevrons along the track show the direction of travel. **Reverse** runs it the
other way, and **Compare the other direction** computes both at the same departure.
That is not cosmetic: on the supplied line, southbound costs 42.8 L and northbound
33.8 L — the long leg runs into a wind from 200°T one way and away from it the other,
and the burn law is convex, so a ±5% swing in RPM becomes ~25% in fuel.

**Fuel measured against what is actually aboard.** *Fuel aboard* takes the litres in
the tank at departure; leave it blank for a full one. The reserve floor stays a
fraction of the **tank**, not of the load — sail with 140 L of a 250 L tank and 77.5 L
is yours to burn, not 105 L. The whole shortfall comes off usable fuel, because the
floor exists for the tank and does not shrink when you load less. A figure larger than
the tank is refused rather than trimmed; a load already under the floor is accepted and
flagged as its own condition.

**Emergency drift prediction.** If the vessel loses power, set its last known
position and the tool projects where the water and the wind take it, for hours to
days. The product is a **datum and a search radius**, not a track. Advection comes
from the same NOAA forecast the passage uses; the wind term uses the **US Coast Guard
leeway model** — downwind and crosswind regressions with the standard error of the
field experiments that produced them, both tacks carried. **No leeway class exists
for an unmanned surface vessel**, so the default is an analogue, and the measured
spread of that class is where most of the radius comes from. Swell adds a Stokes
term — but **only the part not running with the wind**, since the leeway coefficients
already hold the wind-driven waves. It refuses outright if no forecast reaches the
last known position, and reports the hour past which the data ran out rather than
drawing past it. Exports to CSV and GeoJSON.

**Three speed modes, including fixed revs.** *Through water* and *over ground* name a
speed and let the current decide the rest. *Fixed revs* names the **throttle** and the
speed becomes the output — which is what a vessel holding revs actually does. In that
mode litres per hour is fixed, so weather costs **time** rather than fuel. It is not a
relabelling of *through water*: the two agree only if the vessel's own controller
agrees with this model about which revs give which speed, and running a plan both ways
measures that disagreement directly.

**Legs are numbered on the chart.** The same numbers the table uses, boxed beside the
segment they name, so a row saying *leg 4 cannot hold track* can be found without
counting segments — and hovering a row lights that leg white. Where the line is too
crowded for a badge to sit beside its leg, the badge is pushed clear on a leader
rather than dropped. A leg is a segment: leg 4 runs from WP4 to WP5.

**Every control carries a tooltip, and so does every abbreviation.** Buttons, fields,
the leg table's column headings, the result chips, the model names, the cycle tag in
the status bar. Hover for what it means and why it matters, rather than going to the
manual — `DBOFS 87% + CBOFS 13%` says which forecast systems those are, and `Cov`
says it is the share of the leg a model actually reached.

**Chart background.** NOAA ENC raster tiles, proxied and cached locally. The
cache reads through to the ASV Console's existing tile store, so charts for this coast
work offline immediately without re-downloading them. *Localize → Charts* fills in
whatever the line needs that isn't already held.

**Currents, over the whole line, from as many models as it takes.** *Localize →
Currents* works out which NOAA Operational Forecast Systems actually reach the line,
downloads each one cut to the line's own bounding box, and serves them as one source —
finest model first, blended across the handover so the ETA doesn't step. The
calculation then resolves set and drift **along the track, marching in half-mile
steps**, looking up the current at the position the boat has reached and the clock time
it reached it.

*The supplied Lewes line is exactly why this exists:* DBOFS stops at 37.82 °N and the
line runs to 37.66 °N, so 10.4 NM had no current at all. CBOFS covers it. Chained, the
line goes from 86.6% to **100%** covered, and the passage from 10:03 to 9:51.

**Wind and waves, IDW over the full range.** Three sources, because no one source does
the job: **NDBC buoys** (real measurements, scattered and partial), **NWS gridpoints**
(~2.5 km, works offshore), and **WAVEWATCH III** (global 0.5°, the backstop). They are
inverse-distance-weighted **per variable** to every marching step. On the supplied line
that gives wind 8.1 → 11.7 kt and Hs 0.31 → 0.81 m along the track — a real gradient,
not one number per leg. Fuel then runs through the companion fuel planner's own heading and
sea-state premiums into the config's measured law.

**Export and save.** Zipped Shapefile (whole line, or one feature per leg carrying
the computed results), GeoJSON, KML, GPX, and CSV. Every save of a calculated mission
is **timestamped** — `Lewes_offshore_20260814T0317Z_shp.zip` — so repeated saves never
overwrite each other, and the stamp reaches the members inside a zipped shapefile so
two exports extracted side by side do not collide. *Save to* is **on by default**, so pressing a format writes the file into
`docs/missions/` and reports the path; untick it for a browser download instead.

---

## Three things worth knowing before you trust a number

**The current is integrated, not sampled.** A tidal current reverses about every six
hours; the supplied 78 NM line takes about ten. A calculator that evaluates the
current once — at the start, or at each leg's midpoint — cannot see the turn. This one
steps along the line and re-evaluates as the clock advances. On the supplied line that
is worth roughly 25 minutes against a single-sample estimate.

**Coverage is reported, never assumed.** Chaining models widens the honest answer; it
does not replace it. Wherever *no* model reaches, those steps are computed with zero
current **and counted** — every result carries the fraction of its distance that had
real data, per leg and overall, and the panel says so before you calculate. A zero
standing in silently for a gap is the failure this design exists to prevent. The same
applies to wind and waves: a field nobody reports comes back null, never calm.

**"Speed" means one of two things, and it matters.** *Through water* is a throttle
setting — the current then decides your ground speed and your ETA. *Over ground* fixes
the arrival and lets the current decide the water speed, the RPM and therefore the
fuel. On the supplied line the same 8 kt gives 9:51 one way and 9:43 the other, at
different fuel.

**IDW is an interpolator, not a model.** It cannot invent structure between samples. A
point 50 km from anything is a smooth guess, so every interpolated value carries the
sources that made it, their distances, and the age of the oldest observation — shown
under *Weather sources* in the results panel.

---

## Layout

| File | What it is |
|---|---|
| `server.py` | Local HTTP server: tile proxy, calculation, export, file I/O |
| `static/transit.html` | The whole client — canvas chart, drawing, table, export |
| `geo.py` | Vincenty geodesics, UTM ⇄ WGS84, set/drift. No I/O, no state |
| `transit.py` | The marching calculation |
| `fuel.py` | The vessel fuel chain, reading `model.json` |
| `charts.py` | NOAA ENC tile download, cache, read-through, prefetch |
| `ofs.py` | OFS domain probing, model selection, multi-model chaining |
| `marine.py` | NDBC + NWS + WAVEWATCH III, and the IDW blender |
| `currents.py` | **Vendored** from the Fuel Planner — see the header |
| `weather.py` | Single-point Open-Meteo fallback (superseded by `marine.py`) |
| `shapefile_io.py` | Shapefile read/write, stdlib only |
| `exporters.py` | The five output formats |
| `model.json` | **Vendored** the vessel coefficients (v2.8.0) |
| `tests/test_transit.py` | 265 checks |
| `tests/mutate.py` | Proves those checks bite |

Standard library only, with one **optional** exception: `certifi`, used solely to
verify the ERDDAP host that serves WAVEWATCH III. Without it everything still runs and
WW3 reports itself unavailable. Nothing else needs installing, which is the point on a
boat.

---

## Documents

Four manuals in `docs/`, as `.docx` and `.pdf`:

| Document | For |
|---|---|
| Quick Start | a computed transit in ten minutes |
| Instruction Manual | the operator — every control, field and flag |
| Technical Manual | deciding whether to trust a number: algorithms, sources, limits |
| Development Manual | changing the code: architecture, conventions, tests, extension |

```bash
python tools/build_figures.py
python tools/build_docs.py
```

Figures first — the manuals embed them. **The numbers in the text are read from the
code at build time**, not transcribed: the check count, the model version, the export
formats, the sample line's length and the worked examples. A manual that quotes a
figure by hand is wrong the first time anyone changes it, and silent about it.

`tools/export_pdf.ps1` drives Word to produce the PDFs; `tools/render_docs.py`
rasterises every page so the layout can actually be looked at — which is the only way
the figure that came out taller than the page, and the two with colliding labels, were
ever going to be found.

---

## Tests

```bash
python tests/test_transit.py
```

265 checks across geodesy, shapefile I/O, exports, fuel, weather, model chaining,
IDW interpolation and the transit calculation. Verified against the published Vincenty test vector (distance to 0.14 mm,
azimuths to 0.003″), the vessel measured anchors (2.41 NM/L at 8 kt, 1.05 L/h loiter),
and bit-exact shapefile round-trips with the `.shx` index validated independently of
the reader.

```bash
python tests/mutate.py
```

Breaks one behaviour at a time — on a **temp copy**, never the working source — and
confirms the suite goes red. All 37 mutations are caught. Two checks were decorative
when this was first run (an unreachable `.prj` branch, and a sea-state table whose
fallback was identical to the real one); both are now real.

---

## Provenance

- **Charts** — NOAA ENC via the MaritimeChartService export endpoint. Tile scheme,
  cache layout and circuit breaker ported from `asv_console.py`.
- **Currents** — NOAA regional OFS models (DBOFS, CBOFS, …) over OPeNDAP, via
  `currents.py` vendored from the companion fuel planner. That header explains why it is a
  copy, what the copy costs, and the one change made to it.
- **Wind / waves** — NDBC `realtime2` observations, `api.weather.gov` gridpoints, and
  WAVEWATCH III via the PacIOOS ERDDAP server. WW3 needs `certifi` (optional): the
  ERDDAP host presents a chain the Windows store cannot always complete, so without it
  WW3 reports itself unavailable and the other two sources carry on.
- **Fuel** — `model.json` v2.8.0 from the Fuel Planner. The Config A config is a
  measured refit (16 flight-log sessions, 131.9 h of cruise, Aug 2026); the Config B has no fuel law of its own
  and falls back to the model-wide 2024 speed-trials fit.
  Sea state is *derived* from significant wave height using the fuel model's own band
  edges, and flagged as such.

The sea-state premium is an assumption in the source model, not a fit. It is carried
here unchanged, including that caveat.
