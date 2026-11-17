"""Street addresses for one Spanish region (docs/REGIONS-ES.md): derived/geo/addresses/<ES-XX>.sqlite, from Overture Maps.

A prototype for the shared region work, in the shape of the de-1 index in tools/build_region_de_be.py: the index's meta says
norm = es-1, so engine/address_search.py searches it with the Spanish rule only, and US and German indexes are untouched.

Overture's Spanish rows (source scne.es, CC BY 4.0, via OpenAddresses) carry only the municipality in address_levels, so the
region comes from the postcode: its first two digits are the province, and each province belongs to one community.

  derived/buildenv/bin/python tools/build_addresses_es.py ES-MD [--release 2026-10-21.0] [--parquet rows.parquet] [--out DIR]

--parquet reads a local copy of the Spanish rows (columns number, street, unit, postcode, address_levels, lon, lat) instead of S3.
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.address_search import ES_NORM, place_es, street_es  # noqa: E402  the index and the search share one spelling rule

ROOT = Path(__file__).resolve().parents[1]
PROVINCES = {   # INE province codes (the first two postcode digits) of each community, as in docs/regions-es-proposed.json
    'ES-AN': '04 11 14 18 21 23 29 41', 'ES-AR': '22 44 50', 'ES-AS': '33', 'ES-IB': '07', 'ES-CN': '35 38', 'ES-CB': '39',
    'ES-CL': '05 09 24 34 37 40 42 47 49', 'ES-CM': '02 13 16 19 45', 'ES-CT': '08 17 25 43', 'ES-VC': '03 12 46', 'ES-EX': '06 10',
    'ES-GA': '15 27 32 36', 'ES-MD': '28', 'ES-MC': '30', 'ES-NC': '31', 'ES-PV': '01 20 48', 'ES-RI': '26', 'ES-CE': '51', 'ES-ML': '52'}
SOURCE = 'scne.es, CC BY 4.0, distributed by OpenAddresses, via Overture Maps'


def rows_for(region, release, parquet):
    import duckdb
    c = duckdb.connect()
    prefixes = ', '.join(f"'{p}'" for p in PROVINCES[region].split())
    if parquet:
        src, geo = f"read_parquet('{parquet}')", 'lon, lat'
    else:
        c.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
        src = f"read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=addresses/type=address/*', hive_partitioning=1)"
        geo = 'ST_X(geometry), ST_Y(geometry)'
    where = '' if parquet else "country = 'ES' AND "
    return c.execute(f"""SELECT number, street, unit, postcode, coalesce(address_levels[1].value, ''), {geo} FROM {src}
                         WHERE {where}left(postcode, 2) IN ({prefixes}) AND street IS NOT NULL AND number IS NOT NULL""").fetchall()


def build(region, rows, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript('CREATE TABLE addr(num TEXT, sfx TEXT, street TEXT, norm TEXT, typ TEXT, city TEXT, cnorm TEXT, state TEXT, zip TEXT, lat REAL, lon REAL);'
                     'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);')
    data = []
    for number, street, unit, postcode, city, lon, lat in rows:
        kind, core = street_es(street)
        if not core:
            continue
        num = 'SN' if number.strip().upper() in ('S-N', 'SN') else number.strip()
        sfx = unit.strip().upper() if unit and len(unit.strip()) == 1 and unit.strip().isalpha() else ''
        data.append((num, sfx, street.strip().title(), core, kind, city, place_es(city), region, postcode or '', round(lat, 6), round(lon, 6)))
    db.executemany('INSERT INTO addr VALUES (?,?,?,?,?,?,?,?,?,?,?)', data)
    db.execute('CREATE INDEX addr_norm ON addr(norm, num)')
    db.executemany('INSERT INTO meta VALUES (?,?)', [('region', region), ('norm', ES_NORM), ('count', str(len(data))), ('source', SOURCE),
                                                    ('built', time.strftime('%Y-%m-%dT%H:%M:%S'))])
    db.commit()
    db.execute('VACUUM')
    db.close()
    tmp.replace(out)
    return len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('region', choices=sorted(PROVINCES))
    ap.add_argument('--release', default='2026-10-21.0')
    ap.add_argument('--parquet')
    ap.add_argument('--out', default=str(ROOT / 'derived' / 'geo' / 'addresses'))
    a = ap.parse_args()
    t0 = time.time()
    n = build(a.region, rows_for(a.region, a.release, a.parquet), Path(a.out) / f'{a.region}.sqlite')
    print(f'{a.region}: {n:,} addresses in {time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
