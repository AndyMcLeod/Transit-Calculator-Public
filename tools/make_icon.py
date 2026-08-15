"""Draw tools/transit.ico — the desktop-shortcut icon for the Transit Calculator.

    python tools/make_icon.py            # -> tools/transit.ico

A transit line on chart water: a track running from a green START dot, through a
turn, to an amber END dot, with a direction chevron on the long leg. Direction is
the thing this tool was missing until it was asked for, so it is what the icon
shows.

Drawn rather than downloaded, so there is no licensing question and no binary in
the repo without a builder beside it — the same rule the companion fuel planner's
icon follows.

Everything is drawn at 8x and downsampled per size, because Windows renders a
shortcut at 16 px in a list view where a hairline disappears. Below 32 px the
chevron and the waypoint ring are dropped: at that size they turn to mush and read
worse than nothing, so the small sizes keep only the track and its two ends.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / 'transit.ico'
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 8                                     # supersampling factor

# Palette lifted from the app itself, so the icon and the console read as one thing.
SEA = (10, 27, 43, 255)          # chart water
SEA_EDGE = (34, 50, 63, 255)     # tile/graticule hint
GRAT = (22, 40, 56, 255)         # graticule
TRACK = (57, 192, 255, 255)      # the line — --track
START = (63, 191, 107, 255)      # --ok
END = (255, 207, 63, 255)        # --asv
INK = (215, 227, 234, 255)


def draw(px: int) -> Image.Image:
    n = px * SS
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded chart tile.
    pad = n * 0.04
    r = n * 0.18
    d.rounded_rectangle([pad, pad, n - pad, n - pad], radius=r, fill=SEA,
                        outline=SEA_EDGE, width=max(1, int(n * 0.018)))

    # Graticule, only where it will survive the downsample.
    if px >= 32:
        gw = max(1, int(n * 0.008))
        for k in (0.34, 0.66):
            d.line([pad, pad + (n - 2 * pad) * k, n - pad, pad + (n - 2 * pad) * k],
                   fill=GRAT, width=gw)
            d.line([pad + (n - 2 * pad) * k, pad, pad + (n - 2 * pad) * k, n - pad],
                   fill=GRAT, width=gw)

    # The transit: start low-left, a turn, then a long leg to the upper right.
    # Mirrors the shape of a real departure — short manoeuvring legs, then the run.
    # The turn has to be a REAL bend. The first version put b almost on the line
    # a->c (slopes -1.12 and -0.89), and at every size it read as a straight rule
    # with a bead on it rather than a route with a waypoint in it.
    a = (n * 0.20, n * 0.72)
    b = (n * 0.48, n * 0.64)
    c = (n * 0.78, n * 0.24)

    lw = max(2, int(n * 0.055))
    # Dark casing under the track, so it stays legible over the graticule.
    d.line([a, b, c], fill=(5, 8, 12, 210), width=int(lw * 1.7), joint='curve')
    d.line([a, b, c], fill=TRACK, width=lw, joint='curve')

    # Direction chevron on the long leg — the icon's whole point.
    if px >= 32:
        mx, my = (b[0] + c[0]) / 2, (b[1] + c[1]) / 2
        ang = math.atan2(c[1] - b[1], c[0] - b[0])
        s = n * 0.075
        pts = [(mx + math.cos(ang) * s, my + math.sin(ang) * s),
               (mx + math.cos(ang + 2.5) * s, my + math.sin(ang + 2.5) * s),
               (mx + math.cos(ang - 2.5) * s, my + math.sin(ang - 2.5) * s)]
        d.polygon(pts, fill=INK)

    # Ends: START green, END amber — the same colours the chart uses.
    for pt, col in ((a, START), (c, END)):
        rr = n * (0.085 if px >= 32 else 0.10)
        d.ellipse([pt[0] - rr, pt[1] - rr, pt[0] + rr, pt[1] + rr],
                  fill=col, outline=(5, 8, 12, 255), width=max(1, int(n * 0.016)))

    # Turn waypoint, dropped at small sizes.
    if px >= 48:
        rr = n * 0.045
        d.ellipse([b[0] - rr, b[1] - rr, b[0] + rr, b[1] + rr],
                  fill=SEA, outline=TRACK, width=max(1, int(n * 0.016)))

    return img.resize((px, px), Image.LANCZOS)


def main() -> None:
    frames = [draw(s) for s in SIZES]
    # Pillow writes every size into one .ico from the largest frame plus `sizes`;
    # passing the pre-rendered list keeps OUR per-size decisions (dropped chevron,
    # fatter dots) instead of letting it rescale one image for all of them.
    frames[-1].save(OUT, format='ICO', sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print(f'wrote {OUT}  ({", ".join(f"{s}px" for s in SIZES)})')


if __name__ == '__main__':
    main()
