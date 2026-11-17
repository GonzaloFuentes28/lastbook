"""A printable copy of one guide, for when the computer is off or the battery is low.

The copy is a PDF made of the guide's own words and the real pages it points to: first a contents part (the guide's title,
its "stop and get help" warnings with their sources, and each situation's readings with the page each is on in this copy),
then each cited PDF page, copied as it is from the library's own file. Nothing is added that the guide does not already say
(content/SCHEMA.md). Readings that are not PDF pages (videos, web pages) are listed as "in the app only".

build() returns the PDF's bytes. It uses pypdfium2, which the app already ships, and the PDF's standard Helvetica font, so
nothing new is bundled and nothing reaches the network.
"""
import ctypes
import io
import textwrap
import unicodedata
from pathlib import Path

import pypdfium2
import pypdfium2.raw as pdfium

PAGE_W, PAGE_H = 612, 792           # US Letter, in points: the size of most of the library's PDFs
MARGIN = 54
WRAP = {20: 44, 12: 82, 10: 98}     # characters per line at each font size, a safe estimate for Helvetica at this width
INTRO = 'A printed copy from Lastbook. The pages after this list are copied from the sources named in it.'


def readings(topic):
    """[(heading, [reading, ...])] in the order the guide shows them: its situations, or a learning track's steps."""
    if topic.get('choices'):
        return [(c.get('label') or '', c.get('read') or []) for c in topic['choices']]
    return [(s.get('title') or '', s.get('read') or []) for s in topic.get('route') or []]


def cited_pages(topic, library_dir):
    """The PDF pages the guide's readings point to, once each, in the order they first appear: [(path, page)]."""
    pages = []
    for _, read in readings(topic):
        for r in read:
            src = r.get('src') or {}
            page = r.get('page')
            if src.get('kind') != 'pdf' or not src.get('path') or not isinstance(page, int) or page < 1:
                continue
            if src.get('pages') and page > src['pages']:
                continue
            key = (src['path'], page)
            if key not in pages and (Path(library_dir) / key[0]).is_file():
                pages.append(key)
    return pages


def _plain(s):
    """Text the standard Helvetica can draw: its WinAnsi characters. A no-break space draws as a stray letter, so it becomes a
    space; anything else outside WinAnsi becomes its base letter (NFKD), or '?'."""
    s = s.replace('\xa0', ' ')
    try:
        s.encode('cp1252')
        return s
    except UnicodeEncodeError:
        return ''.join(c if _winansi(c) else (unicodedata.normalize('NFKD', c).encode('cp1252', 'ignore').decode('cp1252') or '?') for c in s)


def _winansi(c):
    try:
        c.encode('cp1252')
        return True
    except UnicodeEncodeError:
        return False


class _Writer:
    """Lines of text on new Letter pages, top to bottom."""

    def __init__(self, pdf):
        self.pdf, self.page, self.y, self.count = pdf, None, 0, 0

    def _new_page(self):
        if self.page is not None:
            self.page.gen_content()
        self.page = self.pdf.new_page(PAGE_W, PAGE_H)
        self.count += 1
        self.y = PAGE_H - MARGIN

    def text(self, s, size=12, bold=False, indent=0, gap=0):
        if self.page is None:
            self._new_page()
        self.y -= gap
        for line in textwrap.wrap(_plain(s), WRAP[size] - indent // 6) or ['']:
            if self.y - size * 1.3 < MARGIN:
                self._new_page()
            self.y -= size * 1.3
            obj = pdfium.FPDFPageObj_NewTextObj(self.pdf.raw, b'Helvetica-Bold' if bold else b'Helvetica', ctypes.c_float(size))
            units = (line + '\0').encode('utf-16-le')
            buffer = ctypes.create_string_buffer(units, len(units))
            pdfium.FPDFText_SetText(obj, ctypes.cast(buffer, ctypes.POINTER(pdfium.FPDF_WCHAR)))
            pdfium.FPDFPageObj_Transform(obj, 1, 0, 0, 1, MARGIN + indent, self.y)
            pdfium.FPDFPage_InsertObject(self.page.raw, obj)

    def close(self):
        if self.page is not None:
            self.page.gen_content()
        return self.count


def _contents(pdf, topic, pages, first):
    """Writes the contents part; `first` is the page of this copy the first cited page lands on. Returns how many pages it took."""
    w = _Writer(pdf)
    w.text(topic.get('title') or 'Guide', 20, bold=True)
    w.text(INTRO, 10, gap=4)
    if topic.get('limits'):
        w.text('Stop and get help when...', 12, bold=True, gap=14)
        for item in topic['limits']:
            src = item.get('src') or {}
            where = ', '.join(x for x in (src.get('publisher') or src.get('title'), f"p. {item['page']}" if item.get('page') else '') if x)
            w.text('- ' + (item.get('text') or '') + (f' ({where})' if where else ''), 10, indent=8, gap=2)
    w.text('What is in this copy', 12, bold=True, gap=14)
    for heading, read in readings(topic):
        if not read:
            continue
        w.text(heading, 12, bold=True, gap=8)
        for r in read:
            src = r.get('src') or {}
            name = src.get('title') or r.get('doc') or ''
            key = (src.get('path'), r.get('page'))
            where = (f'page {first + pages.index(key)} of this copy ({name}, p. {r["page"]})' if key in pages
                     else f'in the app only ({name})')
            w.text(f"- {r.get('label') or name}: {where}", 10, indent=8, gap=2)
    return w.close()


def build(topic, library_dir):
    """The printable copy of one resolved topic (a server.topics() entry) as PDF bytes. library_dir holds the cited PDFs."""
    library_dir = Path(library_dir)
    pages = cited_pages(topic, library_dir)
    length = 1
    for _ in range(3):      # trial passes: how many pages the contents take, given the page numbers they print (which can change it)
        trial = pypdfium2.PdfDocument.new()
        try:
            took = _contents(trial, topic, pages, length + 1)
        finally:
            trial.close()
        if took == length:
            break
        length = took
    out = pypdfium2.PdfDocument.new()
    try:
        _contents(out, topic, pages, length + 1)
        for path, n in pages:
            src = pypdfium2.PdfDocument(str(library_dir / path))
            try:
                out.import_pages(src, [n - 1])
            finally:
                src.close()
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        out.close()
