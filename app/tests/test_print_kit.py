"""A guide's printable copy (engine/print_kit.py): its contents part, then the cited PDF pages, once each and in order.
Needs pypdfium2 (the app ships it); skipped without it."""
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    import pypdfium2
    from engine import print_kit
except ImportError:      # pragma: no cover - the app's own Python has it
    pypdfium2 = None


def make_pdf(path, pages, size=(300, 400)):
    """A PDF whose pages differ by width, so a copied page can be told apart: page n is size[0] + n points wide."""
    pdf = pypdfium2.PdfDocument.new()
    for n in range(1, pages + 1):
        pdf.new_page(size[0] + n, size[1])
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def reading(path, page, label, pages=None, kind='pdf'):
    return {'doc': path, 'page': page, 'label': label, 'src': {'title': 'Source ' + path, 'kind': kind, 'path': path, 'pages': pages}}


@unittest.skipIf(pypdfium2 is None, 'pypdfium2 is not installed')
class PrintKit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-print-'))
        make_pdf(self.tmp / 'a.pdf', 5)
        make_pdf(self.tmp / 'b.pdf', 3, size=(500, 400))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def topic(self):
        return {'id': 'water', 'title': 'Get safe drinking water',
                'limits': [{'text': 'Do not drink flood\xa0water.', 'page': 2, 'src': {'publisher': 'U.S. Government', 'title': 'A'}}],
                'choices': [
                    {'label': 'Tap water is off', 'read': [reading('a.pdf', 2, 'Treat it'), reading('b.pdf', 3, 'Store it')]},
                    {'label': 'I can boil water', 'read': [reading('a.pdf', 2, 'Treat it again'),            # the same page: once
                                                           reading('a.pdf', 9, 'Not there', pages=5),        # past the end: skipped
                                                           reading('v.mp4', None, 'A video', kind='video'),  # not a page
                                                           reading('missing.pdf', 1, 'Gone')]}]}             # not on disk

    def build(self, topic=None):
        return pypdfium2.PdfDocument(print_kit.build(topic or self.topic(), self.tmp))

    def text(self, pdf, i):
        return pdf[i].get_textpage().get_text_range()

    def test_cited_pages_once_each_in_order(self):
        self.assertEqual(print_kit.cited_pages(self.topic(), self.tmp), [('a.pdf', 2), ('b.pdf', 3)])

    def test_contents_then_the_cited_pages(self):
        pdf = self.build()
        self.assertEqual(len(pdf), 3)                                       # one contents page, two cited pages
        self.assertEqual([round(pdf[i].get_width()) for i in (1, 2)], [302, 503])   # a.pdf page 2, then b.pdf page 3
        first = self.text(pdf, 0)
        self.assertIn('Get safe drinking water', first)
        self.assertIn('Do not drink flood water. (U.S. Government, p. 2)', first)    # the no-break space drawn as a space
        self.assertIn('Treat it: page 2 of this copy', first)
        self.assertIn('Store it: page 3 of this copy', first)
        self.assertIn('Treat it again: page 2 of this copy', first)
        self.assertIn('A video: in the app only', first)
        self.assertIn('Not there: in the app only', first)

    def test_page_numbers_count_a_long_contents_part(self):
        t = self.topic()
        t['limits'] = t['limits'] * 80                                      # enough warnings to fill more than one page
        pdf = self.build(t)
        contents = len(pdf) - 2
        self.assertGreater(contents, 1)
        whole = ''.join(self.text(pdf, i) for i in range(contents))
        self.assertIn(f'Treat it: page {contents + 1} of this copy', whole)
        self.assertIn(f'Store it: page {contents + 2} of this copy', whole)
        self.assertEqual(round(pdf[contents].get_width()), 302)

    def test_a_learning_track_uses_its_steps(self):
        t = {'id': 'grow', 'title': 'Grow food', 'route': [{'title': 'Step 1: soil', 'read': [reading('b.pdf', 1, 'Soil')]}]}
        pdf = self.build(t)
        self.assertEqual(len(pdf), 2)
        self.assertIn('Step 1: soil', self.text(pdf, 0))


if __name__ == '__main__':
    unittest.main()
