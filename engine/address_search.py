"""Read-only address lookup using the state indexes installed with offline maps."""
import math
import re
import sqlite3
import unicodedata
from pathlib import Path

from engine.paths import ro_uri

# Keep normalization compatible with tools/build_addresses.py.
WORDS = {
    'boulevard': 'blvd', 'avenue': 'ave', 'av': 'ave', 'street': 'st',
    'drive': 'dr', 'road': 'rd', 'place': 'pl', 'court': 'ct', 'lane': 'ln',
    'terrace': 'ter', 'circle': 'cir', 'highway': 'hwy', 'parkway': 'pkwy',
    'square': 'sq', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'mount': 'mt', 'saint': 'st',
}
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Puerto Rico|Rhode Island|South Carolina|South Dakota|'
    'Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
).split('|')
STATE_CODES = 'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY'.split()
STATES = dict(zip((name.lower() for name in STATE_NAMES), STATE_CODES))
ONES = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
ORDINALS = 'zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth'.split()
TO_DIGITS = {word: str(number) for number, word in enumerate(ONES)}
for number, word in enumerate(ORDINALS):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    TO_DIGITS[word] = f'{number}{suffix}'
TO_WORDS = {value: key for key, value in TO_DIGITS.items()}


US_NORM = 'us'        # indexes without a meta 'norm' key: the published US state indexes and the legacy addresses.sqlite
DE_NORM = 'de-1'
_UMLAUTS = str.maketrans({'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'ß': 'ss'})


def normalize(text):
    return ' '.join(WORDS.get(word, word) for word in re.sub(r'[^a-z0-9 ]+', ' ', text.lower()).split())


def normalize_de(text):
    """Scheme 'de-1': 'Müllerstraße' -> 'muellerstr', 'Straße des 17. Juni' -> 'str des 17 juni'.

    German indexes store this in their norm column, so the builder imports it and queries use it unchanged."""
    text = unicodedata.normalize('NFC', text).lower().translate(_UMLAUTS)
    words = re.sub(r'[^a-z0-9 ]+', ' ', text).split()
    return ' '.join(word[:-len('strasse')] + 'str' if word.endswith('strasse') else word for word in words)



# ---- Spain, scheme 'es-1' --------------------------------------------------------------------------------------------
ES_NORM = 'es-1'
# Street types and their common abbreviations, in Spanish and the co-official languages, each to one canonical word.
ES_TYPES = {}
for _canon, _forms in {
        'calle': 'calle c cl cll call carrer carrer kalea rua ru', 'avenida': 'avenida av avda avd avinguda avgda etorbidea',
        'plaza': 'plaza pl pza plz placa praza enparantza', 'paseo': 'paseo ps po pso passeig pasealekua',
        'camino': 'camino cm cno cami bidea', 'carretera': 'carretera ctra crta cr', 'ronda': 'ronda rda', 'travesia': 'travesia trav tr',
        'glorieta': 'glorieta gta', 'pasaje': 'pasaje pje psje passatge', 'urbanizacion': 'urbanizacion urb', 'callejon': 'callejon cjon',
        'lugar': 'lugar lg', 'diseminados': 'diseminados diseminado', 'barrio': 'barrio bo barri', 'partida': 'partida pda',
        'aldea': 'aldea', 'caserio': 'caserio', 'poblado': 'poblado', 'barriada': 'barriada', 'calleja': 'calleja', 'cuesta': 'cuesta',
        'sector': 'sector', 'poligono': 'poligono pol', 'colonia': 'colonia col', 'paraje': 'paraje', 'canada': 'canada', 'vereda': 'vereda',
        'rotonda': 'rotonda', 'jardin': 'jardin', 'puerto': 'puerto', 'bajada': 'bajada', 'subida': 'subida', 'costanilla': 'costanilla',
        'rambla': 'rambla rbla', 'via': 'via', 'grupo': 'grupo', 'arrabal': 'arrabal', 'terrenos': 'terrenos', 'extrarradio': 'extrarradio'}.items():
    for _form in _forms.split():
        ES_TYPES[_form] = _canon
ES_PARTICLES = frozenset('de del la las el los l d dels da das do dos y i'.split())
ES_FLOOR = frozenset('izq izqda izda izquierda dcha dch dcho dra derecha ctro centro bajo bj atico entlo entresuelo principal pral '
                     'piso puerta pta esc escalera portal planta'.split())   # not 'pl': that is also plaza


def fold_es(text):
    """Lower case without accents: 'Alcalá' -> 'alcala', 'Peña' -> 'pena', 'Plaça' -> 'placa', 'Sant Pau' stays, 'l·l' -> 'll'."""
    text = unicodedata.normalize('NFKD', text.lower().replace('·', ''))
    return ''.join(c for c in text if not unicodedata.combining(c))


def place_es(name):
    """A town as the index and the search both write it: 'Rivas-Vaciamadrid' -> 'rivas vaciamadrid', 'Leganés' -> 'leganes'."""
    return ' '.join(re.findall(r'[a-z0-9]+', fold_es(name)))


def street_es(street):
    """(type, core) of a street name: 'Calle de las Cuevas' and Overture's 'CALLE CUEVAS LAS' both -> ('calle', 'cuevas').
    The core has no street type and no articles or 'de', so the cadastre's order and a person's give the same words."""
    words = re.sub(r'[^a-z0-9 ]+', ' ', fold_es(street)).split()
    kind = ES_TYPES.get(words[0], '') if words else ''
    if kind and len(words) > 1:
        words = words[1:]
    return kind, ' '.join(w for w in words if w not in ES_PARTICLES)


def normalize_es(text):
    """Scheme 'es-1': the core of a street name, as street_es. Spanish indexes store this in their norm column."""
    return street_es(text)[1]


def _parts_es(query):
    """(number, suffix, type, core, postcode, place) in Spanish order, or None.
    'Calle de Alcalá, 45, 3º izq., 28014 Madrid' -> ('45', '', 'calle', 'alcala', '28014', 'madrid'). The floor and door after the
    number are dropped. 's/n' is the number 'SN', as the data writes S-N. A single letter after the number is its suffix ('12 B')."""
    text = fold_es(query)
    text = re.sub(r'\bs\s*/\s*n\b|\bs\s*-\s*n\b|\bsin numero\b', ' sn ', text)
    text = re.sub(r'\b(\d+)\s*[ºª°]', r'\1o', text)
    tokens = re.findall(r'[a-z0-9]+|,', text)
    floor = lambda t: bool(re.fullmatch(r'\d+[oa]', t)) or t in ES_FLOOR
    tokens = [t for i, t in enumerate(tokens)          # a floor or door, a bare number just before one ('3 izq'), and 'nº' go
              if not (floor(t) or (t.isdigit() and i + 1 < len(tokens) and floor(tokens[i + 1]))
                      or (t in ('n', 'no', 'num', 'numero') and i + 1 < len(tokens) and tokens[i + 1].isdigit()))]
    postcode, place = '', []
    for i in range(len(tokens) - 1, 0, -1):
        if re.fullmatch(r'\d{5}', tokens[i]):
            postcode, place, tokens = tokens[i], [t for t in tokens[i + 1:] if t != ','], tokens[:i]
            break
    number = None
    for i, t in enumerate(tokens):         # 'Calle de Alcalá, 45', 'Carretera A-42, 30': the number right after the street's comma
        if t == ',' and i + 1 < len(tokens) and (tokens[i + 1].isdigit() or tokens[i + 1] == 'sn') and any(re.search('[a-z]', x) for x in tokens[:i]):
            number = i + 1
            break
    if number is None:                     # 'Calle Alcalá 45, 3 izq': a number between the street and a comma, not the floor after it
        for i, t in enumerate(tokens):
            if (t.isdigit() or t == 'sn') and i and re.search('[a-z]', tokens[i - 1]) and i + 1 < len(tokens) and tokens[i + 1] == ',':
                number = i
                break
    if number is None:                     # else the last number before any place words: 'Calle 2 de Mayo 5 Madrid' -> 5
        for i in range(len(tokens) - 1, -1, -1):
            if tokens[i].isdigit() or tokens[i] == 'sn' or re.fullmatch(r'\d+[a-z]', tokens[i]):
                number = i
                break
    if number is None:
        return None
    num, suffix = re.fullmatch(r'(\d+|sn)([a-z]?)', tokens[number]).groups()
    rest = tokens[number + 1:]
    if not suffix and rest and len(rest[0]) == 1 and rest[0] != ',' and not rest[0].isdigit():
        suffix, rest = rest[0], rest[1:]
    if not place:
        place = [t for t in rest if t != ',' and not t.isdigit()]
    words = [t for t in tokens[:number] if t != ',']
    if not any(re.search('[a-z]', w) for w in words):
        return None
    kind, core = street_es(' '.join(words))
    if not core:
        return None
    return num.upper(), suffix.upper(), kind, core, postcode, ' '.join(place)


def _find_es(parts, geo_directory, limit, near):
    number, suffix, kind, core, postcode, place = parts
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == ES_NORM]
    conditions = ['num = ?', 'norm >= ?', 'norm < ?']
    parameters = [number, core, core + '￿']
    if suffix:
        conditions.append('sfx = ?')
        parameters.append(suffix)
    if postcode:
        conditions.append('zip = ?')
        parameters.append(postcode)
    if place:                              # in SQLite, not after the LIMIT: a common street can pass any limit in one region
        conditions.append('cnorm LIKE ?')
        parameters.append(place + '%')
    sql = ('SELECT num,sfx,street,city,state,zip,lat,lon,norm,typ FROM addr WHERE ' + ' AND '.join(conditions)
           + ' ORDER BY length(norm),norm LIMIT 500')
    rows = []
    for path in paths:
        source = _index_info(path)[1] or 'Overture Maps'
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            rows.extend((*row, source) for row in connection.execute(sql, parameters).fetchall())
        except sqlite3.Error:
            continue
        finally:
            connection.close()

    def rank(row):
        distance = 0
        if near:
            distance = (row[6] - near[0]) ** 2 + ((row[7] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return row[8] != core, bool(kind) and row[9] != kind, row[1] != '', len(row[8]), distance, row[3]
    results, seen = [], set()
    for num, sfx, name, city, state, zip_code, lat, lon, normalized, typ, source in sorted(rows, key=rank):
        key = (num, sfx, normalized, lat, lon)
        if key in seen:
            continue
        seen.add(key)
        house = 's/n' if num == 'SN' else num + (f' {sfx}' if sfx else '')
        results.append({'name': f'{name}, {house}', 'kind': 'Street address', 'detail': ' '.join(p for p in (zip_code, city) if p),
                        'lat': lat, 'lon': lon, 'source': source, 'onMap': True})
        if len(results) == limit:
            break
    return results

def is_address_query(query):
    """A standalone ZIP remains a place query. A number followed by words is an address."""
    return bool(re.match(r'^\s*\d[\w-]*\s+\S', query))


def is_german_address_query(query):
    """Street words, then a house number, then an optional postcode and place ('Unter den Linden 77, 10117 Berlin').

    This form also fits place names such as 'Route 66', so callers search places when no address matches."""
    return _parts_de(query) is not None


def _parts_de(query):
    """(number, street words, postcode, qualifier words) in German order, or None.

    The house number is the last number before an optional postcode and place, so 'Straße des 17. Juni 135' keeps
    its 17 in the street. A single letter after the number is its suffix: '12 a' -> '12A'."""
    tokens = normalize_de(query).split()
    end = len(tokens)
    while end and not re.search(r'\d', tokens[end - 1]):
        end -= 1
    head, qualifier = tokens[:end], tokens[end:]
    postcode = suffix = ''
    if len(head) >= 3 and re.fullmatch(r'\d{5}', head[-1]) and (
            re.fullmatch(r'\d+[a-z]?', head[-2]) or (re.fullmatch(r'[a-z]', head[-2]) and head[-3].isdigit())):
        postcode = head.pop()
    if head and head[-1].isdigit() and not postcode and qualifier and len(qualifier[0]) == 1:
        suffix = qualifier.pop(0)
    elif len(head) >= 2 and re.fullmatch(r'[a-z]', head[-1]) and head[-2].isdigit():
        suffix = head.pop()
    if not head or not re.fullmatch(r'\d+[a-z]?', head[-1]):
        return None
    number = (head.pop() + suffix).upper()
    if not any(re.search(r'[a-z]', word) for word in head):
        return None
    return number, head, postcode, qualifier


def is_german_postcode_query(query, geo_directory):
    """'10117 Berlin': a postcode and a place that an installed German index knows together. That is a place, even
    though it starts with a number like a US address; without a German index the answer is always False."""
    match = re.fullmatch(r'\s*(\d{5})[\s,]+([^\d]+)', query)
    if not match:
        return False
    postcode, qualifier = match.group(1), normalize_de(match.group(2))
    if not qualifier:
        return False
    for path in sorted((Path(geo_directory) / 'addresses').glob('*.sqlite')):
        if _index_info(path)[0] != DE_NORM:
            continue
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                found = connection.execute('SELECT DISTINCT city, district FROM addr WHERE zip = ?', (postcode,)).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        if any(_place_matches(qualifier, city, district) for city, district in found):
            return True
    return False


def _place_matches(qualifier, city, district):
    """The place after a German number may name the city or the Ortsteil, prefix-matched like the US city filter."""
    city, district = city or '', district or ''
    places = (normalize_de(city), normalize_de(district), normalize_de(f'{city} {district}'))
    return any(place.startswith(qualifier) for place in places if place)


_INFO = {}


def _index_info(path):
    """(norm scheme, source line) from an index's meta table, cached until the file changes."""
    try:
        stat = path.stat()
    except OSError:
        return US_NORM, None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _INFO:
        meta = {}
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
            try:
                meta = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('norm','source')").fetchall())
            finally:
                connection.close()
        except sqlite3.Error:
            pass        # no meta table: an index from before norm schemes, so US rules
        _INFO[key] = (meta.get('norm') or US_NORM, meta.get('source'))
    return _INFO[key]


def street_forms(street):
    words = street.split()
    forms = [street, ' '.join(TO_DIGITS.get(word, word) for word in words),
             ' '.join(TO_WORDS.get(word, word) for word in words)]
    forms += [re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', form) for form in forms[:]]
    if words[0] not in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
        forms += [f'{direction} {form}' for form in forms[:] for direction in ('n', 's', 'e', 'w')]
    return list(dict.fromkeys(forms))


def _parts(query):
    number, remainder = query.strip().split(None, 1)
    number = number.upper()
    remainder = re.sub(r'[^a-z0-9 ]+', ' ', remainder.lower()).split()
    postcode = ''
    if remainder and re.fullmatch(r'\d{5}', remainder[-1]):
        postcode = remainder.pop()
    state = ''
    for count in (3, 2, 1):
        if len(remainder) <= count:
            continue
        name = ' '.join(remainder[-count:])
        code = STATES.get(name) or (name.upper() if count == 1 and name.upper() in STATE_CODES else '')
        if code:
            state = code
            remainder = remainder[:-count]
            break
    return number, normalize(' '.join(remainder)).split(), state, postcode


def _rank(near):
    def rank(row):
        distance = 0
        if near:
            distance = (row[5] - near[0]) ** 2 + ((row[6] - near[1]) * math.cos(math.radians(near[0]))) ** 2
        return len(row[7]), distance, row[3], row[2], row[1]
    return rank


def find_addresses(query, geo_directory, limit=8, near=None):
    """Return exact house-number matches from installed indexes, never a guessed house.

    A number-first query searches the US-normalized indexes; a German-order query ('Müllerstr. 12a') searches the
    indexes whose meta norm is de-1. Each index is only ever searched with its own normalization."""
    if is_address_query(query):
        return _find_us(query, Path(geo_directory), limit, near)
    found = []
    parts = _parts_de(query)
    if parts is not None:
        found = _find_de(parts, Path(geo_directory), limit, near)
    parts = _parts_es(query)               # each index is searched with its own rule only: none of this touches a de-1 or US index
    if parts is not None and len(found) < limit:
        found += _find_es(parts, Path(geo_directory), limit - len(found), near)
    return found


def _find_de(parts, geo_directory, limit, near):
    number, words, postcode, qualifier = parts
    street, qualifier = ' '.join(words), ' '.join(qualifier)
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == DE_NORM]
    conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
    parameters = [number, number.lower(), street, street + '\uffff']
    if postcode:
        conditions.append('zip = ?')
        parameters.append(postcode)
    sql = ('SELECT num,street,city,state,zip,lat,lon,norm,district FROM addr WHERE '
           + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT 500')
    rows = []
    for path in paths:
        source = _index_info(path)[1] or 'Overture Maps'
        try:
            connection = sqlite3.connect(ro_uri(path), uri=True)
        except sqlite3.Error:
            continue
        try:
            found = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error:
            continue
        finally:
            connection.close()
        rows.extend((*row, source) for row in found if not qualifier or _place_matches(qualifier, row[2], row[8]))
    results, seen = [], set()
    for num, name, city, state, zip_code, lat, lon, normalized, district, source in sorted(rows, key=_rank(near)):
        key = (num, normalized, lat, lon)
        if key in seen:
            continue
        seen.add(key)
        place = f'{city}-{district}' if district else city
        results.append({
            'name': f'{name} {num}', 'kind': 'Street address',
            'detail': ' '.join(part for part in (zip_code, place) if part),
            'lat': lat, 'lon': lon, 'source': source, 'onMap': True,
        })
        if len(results) == limit:
            break
    return results


def _find_us(query, geo_directory, limit, near):
    number, words, state, postcode = _parts(query)
    if not words:
        return []
    paths = [path for path in sorted((geo_directory / 'addresses').glob('*.sqlite')) if _index_info(path)[0] == US_NORM]
    if state:
        paths = [path for path in paths if path.stem.upper() == state]
    legacy = geo_directory / 'addresses.sqlite'
    if not paths and legacy.is_file():
        paths = [legacy]
    connections = []
    try:
        for path in paths:
            try:
                connections.append(sqlite3.connect(ro_uri(path), uri=True))
            except sqlite3.Error:
                continue
        for count in range(len(words), 0, -1):
            street = ' '.join(words[:count])
            if all(word in set(WORDS.values()) for word in words[:count]):
                continue
            city = ' '.join(words[count:])
            rows = []
            for form in street_forms(street):
                conditions = ['num IN (?, ?)', 'norm >= ?', 'norm < ?']
                parameters = [number, number.lower(), form, form + '\uffff']
                if state:
                    conditions.append('state = ?')
                    parameters.append(state)
                if postcode:
                    conditions.append('zip = ?')
                    parameters.append(postcode)
                if city:
                    conditions.append('lower(city) LIKE ?')
                    parameters.append(city + '%')
                sql = ('SELECT num,street,city,state,zip,lat,lon,norm FROM addr WHERE '
                       + ' AND '.join(conditions) + ' ORDER BY length(norm),norm LIMIT ?')
                for connection in connections:
                    try:
                        rows.extend(connection.execute(sql, [*parameters, limit * 3]).fetchall())
                    except sqlite3.Error:
                        continue
            if rows:
                results, seen = [], set()
                for num, name, city_name, state_code, zip_code, lat, lon, normalized in sorted(rows, key=_rank(near)):
                    key = (num, normalized, lat, lon)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'name': f'{num} {name}', 'kind': 'Street address',
                        'detail': ', '.join(part for part in (city_name, state_code, zip_code) if part),
                        'lat': lat, 'lon': lon, 'source': 'Overture Maps', 'onMap': True,
                    })
                    if len(results) == limit:
                        break
                return results
        return []
    finally:
        for connection in connections:
            connection.close()
