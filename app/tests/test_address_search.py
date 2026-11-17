"""Offline address lookup must preserve house numbers and geographic qualifiers."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.address_search import (find_addresses, is_address_query, is_german_address_query, is_german_postcode_query,
                                   normalize, normalize_de, street_es)


class AddressSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_state('MI', [
            ('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47),
            ('18002', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.46, -83.47),
            ('18001', 'Nine Mile Rd', 'Farmington', 'MI', '48335', 42.47, -83.40),
        ])
        self.write_state('CA', [
            ('18001', 'Nine Mile Road', 'Example', 'CA', '90001', 34.0, -118.0),
            ('23001', 'Maple Avenue', 'Kansas City', 'MO', '64105', 39.0997, -94.5786),
            ('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01),
        ])

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, state, rows):
        db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute('CREATE INDEX addr_num_norm ON addr(num,norm)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?)',
                           [(num, street, normalize(street), city, st, z, lat, lon)
                            for num, street, city, st, z, lat, lon in rows])
            db.commit()
        finally:
            db.close()      # the `with` form commits but does not close, and Windows will not remove an open file

    def test_numeric_words_directions_and_qualifiers(self):
        for query in ('18001 9 mile rd Novi MI', '18001 Nine Mile Road Novi Michigan',
                      '18001 nine mile Novi 48375', '18001 W Nine Mile Road, Novi, MI 48375'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['name'], '18001 W Nine Mile Road')
                self.assertEqual(result[0]['lat'], 42.45)
                self.assertEqual(result[0]['kind'], 'Street address')

    def test_unknown_number_does_not_return_nearby_house(self):
        self.assertEqual(find_addresses('18003 nine mile Novi MI', self.geo), [])

    def test_city_and_zip_do_not_silently_fall_back(self):
        self.assertEqual(find_addresses('18001 nine mile Wrongtown MI', self.geo), [])
        self.assertEqual(find_addresses('18001 nine mile MI 99999', self.geo), [])

    def test_state_uses_only_that_index(self):
        result = find_addresses('18001 nine mile CA', self.geo)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['detail'], 'Example, CA, 90001')

    def test_ordinal_street(self):
        result = find_addresses('21 ninth st Example CA', self.geo)
        self.assertEqual(result[0]['name'], '21 W 9 ST')

    def test_plain_zip_remains_place_query(self):
        self.assertFalse(is_address_query('48375'))
        self.assertFalse(is_address_query('Novi MI'))
        self.assertTrue(is_address_query('18001 nine mile'))

    def test_missing_indexes_and_legacy_database(self):
        empty = self.geo / 'empty'
        self.assertEqual(find_addresses('23001 Maple Ave', empty), [])
        empty.mkdir()
        db = sqlite3.connect(empty / 'addresses.sqlite')
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL)')
            db.execute("INSERT INTO addr VALUES('23001','Maple Avenue','maple ave','Kansas City','MO','64105',39.0997,-94.5786)")
            db.commit()
        finally:
            db.close()      # as in write_state: the `with` form does not close, and Windows will not remove an open file
        self.assertEqual(find_addresses('23001 Maple Ave Kansas City MO', empty)[0]['lat'], 39.0997)

    def test_app_route_preserves_address_number(self):
        import server
        with patch.object(server, 'HERE', self.geo.parent), patch.object(server.address_search, 'find_addresses', return_value=[]) as lookup:
            self.assertEqual(server.places('18003 nine mile'), [])
            lookup.assert_called_once_with('18003 nine mile', self.geo.parent / 'derived' / 'geo', limit=8, near=None)


BERLIN_SOURCE = 'Geoportal Berlin, Adressen Berlin (dl-de/zero-2.0)'
BERLIN = [
    ('77', 'Unter den Linden', '10117', 'Mitte', 52.5170, 13.3888),
    ('12', 'Müllerstraße', '13353', 'Wedding', 52.5480, 13.3600),
    ('12A', 'Müllerstraße', '13353', 'Wedding', 52.5481, 13.3601),
    ('135', 'Straße des 17. Juni', '10623', 'Tiergarten', 52.5130, 13.3270),
    ('25', 'Oranienstraße', '10999', 'Kreuzberg', 52.5010, 13.4190),
    ('5', 'Hauptstraße', '10827', 'Schöneberg', 52.4880, 13.3530),
    ('5', 'Hauptstraße', '12589', 'Rahnsdorf', 52.4400, 13.6900),
    ('77', 'Lindenstraße', '10969', 'Kreuzberg', 52.5030, 13.3980),
]


class GermanAddressSearchTests(unittest.TestCase):
    """Berlin addresses are written street first, number after, and may carry a postcode and an Ortsteil."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.write_berlin(self.geo / 'addresses' / 'DE-BE.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_berlin(path, rows=BERLIN, source=BERLIN_SOURCE):
        """The DE-BE index contract: a district column and meta norm 'de-1' with its own source line."""
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE addr(num TEXT,street TEXT,norm TEXT,city TEXT,state TEXT,zip TEXT,lat REAL,lon REAL,district TEXT)')
            db.execute('CREATE INDEX addr_norm ON addr(norm,num)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO addr VALUES(?,?,?,?,?,?,?,?,?)',
                           [(num, street, normalize_de(street), 'Berlin', 'DE-BE', z, lat, lon, district)
                            for num, street, z, district, lat, lon in rows])
            db.executemany('INSERT INTO meta VALUES(?,?)', [('norm', 'de-1'), ('source', source)])
            db.commit()
        finally:
            db.close()      # as in AddressSearchTests.write_state: Windows will not remove an open file

    def write_us_state(self, state, rows, source=None):
        AddressSearchTests.write_state(self, state, rows)
        if source:      # the published US state indexes carry a source line but no norm key
            db = sqlite3.connect(self.geo / 'addresses' / f'{state}.sqlite')
            try:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO meta VALUES('source',?)", (source,))
                db.commit()
            finally:
                db.close()

    def one(self, query):
        result = find_addresses(query, self.geo)
        self.assertEqual(len(result), 1, query)
        return result[0]

    def test_street_then_number_with_optional_postcode_and_city(self):
        """The plain German order, with or without the postcode line, finds the house and names it the German way."""
        for query in ('Unter den Linden 77', 'unter den linden 77, 10117 Berlin', 'Unter den Linden 77 10117'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['name'], 'Unter den Linden 77')
                self.assertEqual(hit['detail'], '10117 Berlin-Mitte')
                self.assertEqual(hit['lat'], 52.5170)
                self.assertEqual(hit['kind'], 'Street address')
                self.assertEqual(hit['source'], BERLIN_SOURCE)     # credit the index's own source, not Overture

    def test_umlaut_sharp_s_and_str_abbreviation_spellings_meet(self):
        """People type ß, ss, ü, ue and 'str.' interchangeably; all must reach house 12A and never plain 12."""
        for query in ('Müllerstr. 12a', 'Müllerstraße 12 a', 'Muellerstrasse 12A', 'MÜLLERSTRASSE 12A, 13353 Berlin'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['name'], 'Müllerstraße 12A')
                self.assertEqual(hit['lat'], 52.5481)

    def test_normalize_de_scheme(self):
        """The builder imports this function, so its output is the stored norm column and must follow scheme de-1."""
        self.assertEqual(normalize_de('Müllerstraße'), 'muellerstr')
        self.assertEqual(normalize_de('Straße des 17. Juni'), 'str des 17 juni')
        self.assertEqual(normalize_de('Karl-Marx-Str.'), 'karl marx str')
        self.assertEqual(normalize_de('Hauptstrasse'), normalize_de('Hauptstraße'))
        self.assertEqual(normalize('18001 W Nine Mile Road'), '18001 w nine mile rd')      # the US scheme is untouched

    def test_number_inside_street_name_is_not_the_house_number(self):
        """'Straße des 17. Juni 135': the house is the last number, the 17 belongs to the street."""
        hit = self.one('Straße des 17. Juni 135')
        self.assertEqual(hit['name'], 'Straße des 17. Juni 135')
        self.assertEqual(find_addresses('Straße des 17. Juni', self.geo), [])

    def test_district_or_city_qualifier_filters_like_the_us_city(self):
        """A trailing Ortsteil or city narrows the street to that place, prefix-matched, and a wrong one finds nothing."""
        self.assertEqual(self.one('Oranienstraße 25 Kreuzberg')['detail'], '10999 Berlin-Kreuzberg')
        self.assertEqual(self.one('Oranienstr 25 kreuz')['lat'], 52.5010)
        self.assertEqual(self.one('Hauptstraße 5 Schöneberg')['detail'], '10827 Berlin-Schöneberg')
        self.assertEqual(self.one('Hauptstraße 5 Berlin-Rahnsdorf')['detail'], '12589 Berlin-Rahnsdorf')
        self.assertEqual(len(find_addresses('Hauptstraße 5 Berlin', self.geo)), 2)
        self.assertEqual(find_addresses('Oranienstraße 25 Spandau', self.geo), [])
        self.assertEqual(find_addresses('Oranienstraße 25, 10117 Berlin', self.geo), [])

    def test_unknown_number_does_not_return_nearby_house(self):
        """Exact house numbers only: 78 must not fall back to 77, and 12B not to 12 or 12A."""
        self.assertEqual(find_addresses('Unter den Linden 78', self.geo), [])
        self.assertEqual(find_addresses('Müllerstraße 12b', self.geo), [])

    def test_postcode_city_and_district_alone_stay_place_queries(self):
        """'10117 Berlin', 'Berlin' and 'Kreuzberg' name places; treating them as addresses would hide the place."""
        for query in ('10117 Berlin', 'Berlin', 'Kreuzberg', '10117'):
            with self.subTest(query=query):
                self.assertFalse(is_german_address_query(query))
                self.assertEqual(find_addresses(query, self.geo), [])
        # '10117 Berlin' is number-first like '18001 nine mile', so places() asks whether a German index knows it
        self.assertTrue(is_address_query('10117 Berlin'))
        self.assertTrue(is_german_postcode_query('10117 Berlin', self.geo))
        self.assertTrue(is_german_postcode_query('10999, Kreuzberg', self.geo))
        self.assertFalse(is_german_postcode_query('18003 nine mile', self.geo))
        self.assertFalse(is_german_postcode_query('10117 Spandau', self.geo))
        self.assertTrue(is_german_address_query('Unter den Linden 77'))

    def test_us_results_unchanged_with_berlin_installed(self):
        """Adding DE-BE must not change a single US answer, including the 'Overture Maps' credit."""
        (self.geo / 'addresses' / 'DE-BE.sqlite').unlink()
        self.write_us_state('MI', [('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47),
                                   ('77', 'Linden Street', 'Novi', 'MI', '48375', 42.40, -83.40)],
                            source='Overture Maps addresses 2026-10-21.0 (open licences, mostly CDLA-Permissive-2.0)')
        self.write_us_state('CA', [('21', 'W 9 ST', 'Example', 'CA', '90001', 34.01, -118.01)])
        queries = ('18001 nine mile', '18001 W Nine Mile Road, Novi, MI 48375', '77 Linden', '77 Linden St Novi',
                   '21 ninth st Example CA', '18003 nine mile')
        before = [find_addresses(query, self.geo) for query in queries]
        self.assertEqual(before[0][0]['source'], 'Overture Maps')
        self.write_berlin(self.geo / 'addresses' / 'DE-BE.sqlite')
        self.assertEqual([find_addresses(query, self.geo) for query in queries], before)

    def test_delaware_stays_delaware(self):
        """Bare 'DE' is the US state code: a Delaware query must use DE.sqlite only, never the DE-BE index."""
        self.write_us_state('DE', [('77', 'Linden Street', 'Wilmington', 'DE', '19801', 39.74, -75.55)])
        for query in ('77 Linden DE', '77 Linden Delaware'):
            with self.subTest(query=query):
                hit = self.one(query)
                self.assertEqual(hit['detail'], 'Wilmington, DE, 19801')
                self.assertEqual(hit['source'], 'Overture Maps')

    def test_app_route_falls_back_to_places_for_route_66(self):
        """'Route 66' has the German shape and '10117 Berlin' the US one, but both are places: with no such house the
        normal place search answers. An unknown US house stays an empty address answer, as before."""
        import server
        root = self.geo / 'root'
        geo = root / 'derived' / 'geo'
        (geo / 'addresses').mkdir(parents=True)
        self.write_berlin(geo / 'addresses' / 'DE-BE.sqlite')
        places = sqlite3.connect(geo / 'places.sqlite')
        try:
            places.executescript(
                "CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);"
                "CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Route 66','Locale','Arizona',35.2,-113.9,3,'gnis');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Berlin','City','Germany',52.52,13.40,1,'test');"
                "INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES ('Nine Mile','Locale','Michigan',42.4,-83.4,3,'gnis');"
                "INSERT INTO place_fts(place_fts) VALUES('rebuild');")
            places.commit()
        finally:
            places.close()
        with patch.object(server, 'HERE', root), patch.object(server, 'PLACEDB', geo / 'places.sqlite'), \
                patch.object(server, 'WATERDB', root / 'no-water.sqlite'), patch.object(server, 'home_area', return_value=None):
            self.assertEqual([hit['name'] for hit in server.places('Route 66')], ['Route 66'])
            self.assertEqual([hit['name'] for hit in server.places('10117 Berlin')], ['Berlin'])
            self.assertEqual([hit['name'] for hit in server.places('Kreuzberg')], [])
            self.assertEqual(server.places('18003 nine mile'), [])
            hit, = server.places('Unter den Linden 77')
            self.assertEqual((hit['name'], hit['kind']), ('Unter den Linden 77', 'Street address'))



def _es_builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_addresses_es', Path(__file__).resolve().parents[2] / 'tools' / 'build_addresses_es.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# (number, street, unit, postcode, town, lon, lat) as Overture writes Spanish rows: capitals, accents mostly dropped, cadastre order.
# Invented streets in invented towns, except that the shapes are the real ones measured in docs/REGIONS-ES.md.
SPAIN = [
    ('134', 'CALLE CUEVAS LAS', None, '28991', 'Villaejemplo', -3.60, 40.10),
    ('45', 'CALLE ALCALA', None, '28014', 'Madrid', -3.69, 40.42),
    ('45', 'CALLE ALCALA', None, '28991', 'Villaejemplo', -3.61, 40.11),
    ('3', 'CALLE PENA', None, '28992', 'Peñaejemplo', -3.62, 40.12),
    ('640', 'AVENIDA DIAGONAL', None, '28993', 'Diagonalejo', -3.63, 40.13),
    ('S-N', 'PLAZA MAYOR', None, '28994', 'Mayorejo', -3.64, 40.14),
    ('12', 'CALLE OLMO', 'B', '28995', 'Olmeda', -3.65, 40.15),
    ('12', 'CALLE OLMO', None, '28995', 'Olmeda', -3.6501, 40.1501),
    ('30', 'CARRETERA A-42', None, '28996', 'Rivas-Ejemplo', -3.66, 40.16),
]


class SpanishAddressSearchTests(unittest.TestCase):
    """Scheme es-1 (prototype): Spanish order, accent folding, the cadastre's articles, s/n, a door letter, and floors ignored."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.geo = Path(self.temp.name)
        (self.geo / 'addresses').mkdir()
        self.builder = _es_builder()
        self.builder.build('ES-MD', SPAIN, self.geo / 'addresses' / 'ES-MD.sqlite')

    def tearDown(self):
        self.temp.cleanup()

    def first(self, query):
        result = find_addresses(query, self.geo)
        self.assertTrue(result, query)
        return result[0]

    def test_index_contract(self):
        db = sqlite3.connect(self.geo / 'addresses' / 'ES-MD.sqlite')
        try:
            meta = dict(db.execute('SELECT key,value FROM meta').fetchall())
            self.assertEqual((meta['norm'], meta['region'], meta['count']), ('es-1', 'ES-MD', str(len(SPAIN))))
            self.assertEqual(db.execute("SELECT num,sfx FROM addr WHERE norm='mayor'").fetchall(), [('SN', '')])
        finally:
            db.close()

    def test_cadastre_order_and_a_persons_order_meet(self):
        self.assertEqual(street_es('CALLE CUEVAS LAS'), ('calle', 'cuevas'))
        self.assertEqual(street_es('Calle de las Cuevas'), ('calle', 'cuevas'))
        self.assertEqual(street_es('Paseo de la Castellana'), ('paseo', 'castellana'))
        self.assertEqual(self.first('Calle de las Cuevas 134')['lat'], 40.10)

    def test_accents_abbreviations_and_co_official_types(self):
        for query, lat in (('Calle Peña 3', 40.12), ('c/ pena 3', 40.12), ('Avda. Diagonal 640', 40.13),
                           ('Avinguda Diagonal 640', 40.13), ('Diagonal 640', 40.13)):
            with self.subTest(query=query):
                self.assertEqual(self.first(query)['lat'], lat)

    def test_floor_door_postcode_and_town(self):
        for query in ('Calle de Alcalá, 45, 3º izq., 28014 Madrid', 'Calle Alcalá 45, 3 izq, Madrid', 'Calle Alcalá nº 45, 28014'):
            with self.subTest(query=query):
                result = find_addresses(query, self.geo)
                self.assertEqual([r['detail'] for r in result], ['28014 Madrid'])

    def test_town_with_a_hyphen(self):
        self.assertEqual(self.first('Carretera A-42, 30, Rivas-Ejemplo')['lat'], 40.16)
        self.assertEqual(self.first('Carretera A-42, 30, rivas ejemplo')['lat'], 40.16)

    def test_no_number_and_door_letter(self):
        self.assertEqual(self.first('Plaza Mayor s/n')['name'], 'Plaza Mayor, s/n')
        self.assertEqual(self.first('Calle Olmo 12 B')['name'], 'Calle Olmo, 12 B')
        self.assertEqual(self.first('Calle Olmo 12')['name'], 'Calle Olmo, 12')       # the plain number first, then its letters

    def test_unknown_number_does_not_return_nearby_house(self):
        self.assertEqual(find_addresses('Calle Alcalá 47, Madrid', self.geo), [])

    def test_town_filter_is_applied_before_the_limit(self):
        many = [('45', 'CALLE ALCALA', None, '28990', f'Pueblo {i}', -3.7, 40.0 + i / 1000) for i in range(600)]
        self.builder.build('ES-MD', many + SPAIN, self.geo / 'addresses' / 'ES-MD.sqlite')
        self.assertEqual(self.first('Calle Alcalá 45, Villaejemplo')['detail'], '28991 Villaejemplo')

    def test_us_and_german_results_unchanged_with_spain_installed(self):
        (self.geo / 'addresses' / 'ES-MD.sqlite').unlink()
        AddressSearchTests.write_state(self, 'MI', [('18001', 'W Nine Mile Road', 'Novi', 'MI', '48375', 42.45, -83.47)])
        GermanAddressSearchTests.write_berlin(self.geo / 'addresses' / 'DE-BE.sqlite')
        queries = ('18001 nine mile', 'Unter den Linden 77', 'Unter den Linden 77, 10117 Berlin', 'Müllerstraße 12a')
        before = [find_addresses(query, self.geo) for query in queries]
        self.assertTrue(before[0] and before[1])
        self.builder.build('ES-MD', SPAIN, self.geo / 'addresses' / 'ES-MD.sqlite')
        self.assertEqual([find_addresses(query, self.geo) for query in queries], before)

if __name__ == '__main__':
    unittest.main()
