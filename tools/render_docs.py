"""Rasterise the generated PDFs so the pages can actually be looked at.

    python tools/render_docs.py              # -> tools/pages/<doc>/p01.png ...
    python tools/render_docs.py --contact    # also a contact sheet per document

A DOCUMENT BUILDER IS UNVERIFIED UNTIL THE PAGES HAVE BEEN RENDERED AND READ. The
builder cannot see a caption that collided with a figure, a table that broke across a
page in the wrong place, or an image that came out at the wrong scale — those are
properties of the LAYOUT, and the layout only exists once Word has set it.

This exists because that has already caught real faults in this repo's figures: text
through a panel title, and an arrow struck through a label. Both were invisible in
the source and obvious on the page.

Output is gitignored: it is a verification artifact, regenerated on demand, not a
deliverable.
"""

import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, 'docs')
OUT = os.path.join(HERE, 'pages')
ZOOM = 1.6                     # ~115 dpi: legible on screen, small enough to skim


def render(pdf_path, contact=False):
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    dest = os.path.join(OUT, name)
    os.makedirs(dest, exist_ok=True)
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(ZOOM, ZOOM)
    paths = []
    for i, page in enumerate(doc, 1):
        p = os.path.join(dest, f'p{i:02d}.png')
        page.get_pixmap(matrix=mat).save(p)
        paths.append(p)
    print(f'  {name}: {len(paths)} page(s)')

    if contact and paths:
        # A contact sheet is the cheapest way to spot a page that went wrong: a
        # blank one, a runaway table, a figure that swallowed a page.
        from PIL import Image
        thumbs = [Image.open(p) for p in paths]
        tw = 260
        ths = [t.resize((tw, int(t.height * tw / t.width)), Image.LANCZOS) for t in thumbs]
        cols = min(5, len(ths))
        rows = (len(ths) + cols - 1) // cols
        pad = 10
        h = max(t.height for t in ths)
        sheet = Image.new('RGB', (cols * (tw + pad) + pad, rows * (h + pad) + pad),
                          (220, 224, 228))
        for k, t in enumerate(ths):
            r, c = divmod(k, cols)
            sheet.paste(t, (pad + c * (tw + pad), pad + r * (h + pad)))
        sp = os.path.join(OUT, f'{name}_contact.png')
        sheet.save(sp)
        print(f'    contact sheet -> {os.path.relpath(sp, ROOT)}')
    doc.close()
    return len(paths)


def main():
    contact = '--contact' in sys.argv
    pdfs = sorted(os.path.join(DOCS, f) for f in os.listdir(DOCS) if f.endswith('.pdf'))
    if not pdfs:
        print('  no PDFs in docs/ — run tools/export_pdf.ps1 first')
        return 1
    total = sum(render(p, contact) for p in pdfs)
    print(f'\n  {total} pages under {os.path.relpath(OUT, ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
