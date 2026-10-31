#!/usr/bin/env python3
"""
GöBUS – Liniennetze der Verbünde aus amtlichen Fahrplandaten (GTFS) und OpenStreetMap
=======================================================================================

Läuft wöchentlich bei GitHub (siehe .github/workflows/liniennetz.yml) – oder auf jedem Rechner mit Python 3.9+.
Keine Zusatzpakete nötig.

Ablauf
  1. Fahrplandaten für den Nahverkehr in Deutschland herunterladen (GTFS.DE, erzeugt aus den Daten von DELFI e.V.,
     Lizenz CC BY 4.0) – rund 265 MB.
  2. Nur unsere Region behalten: Haltestellen in den Landkreisen der Verbünde (landkreise.topo.json, verbuende.json).
  3. Je Linie die vollständige Fahrt mit den meisten Haltestellen bestimmen → Nummer, Betreiber, Start und Ziel.
  4. Jede Linie dem Verbund zuordnen, in dessen Landkreisen ihre Haltestellen liegen.
  5. Linienverlauf: aus OpenStreetMap (straßengenau), wenn dort eine passende Linie mit derselben Nummer
     verläuft; sonst Verbindung der Haltestellen.
  6. Je Verbund eine Datei im bisherigen Format schreiben (daten/vsn_linien.json usw.).

Aufruf
  python liniennetz.py                         # alles (Download, OpenStreetMap, Ausgabe nach daten/)
  python liniennetz.py --gtfs feed.zip         # vorhandene GTFS-Datei verwenden
  python liniennetz.py --ohne-osm              # nur Haltestellen-Verläufe (schneller)
"""
import argparse
import csv
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict

GTFS_URL = 'https://download.gtfs.de/germany/nv_free/latest.zip'
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
HIER = os.path.dirname(os.path.abspath(__file__))
UA = 'GoeBUS-Liniennetz/1.0 (+https://www.goebus.de)'
ROUTE_TYPEN = {0: 'tram', 1: 'subway', 2: 'train', 3: 'bus', 4: 'ferry', 5: 'tram', 6: 'aerialway', 7: 'funicular', 11: 'trolleybus', 12: 'monorail'}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------------------
# Landkreise (TopoJSON) und Punkt-in-Fläche
# ---------------------------------------------------------------------------
def landkreise(pfad):
    topo = json.load(open(pfad, encoding='utf-8'))
    sx, sy = topo['transform']['scale']
    tx, ty = topo['transform']['translate']
    arcs = []
    for arc in topo['arcs']:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(idx):
        out = []
        for i in idx:
            pts = arcs[i] if i >= 0 else list(reversed(arcs[~i]))
            out.extend(pts if not out else pts[1:])
        return out

    kreise = {}
    for g in topo['objects']['kreise']['geometries']:
        polys = [g['arcs']] if g['type'] == 'Polygon' else g['arcs']
        rings = [[ring(r) for r in p] for p in polys]
        xs = [c[0] for p in rings for c in p[0]]
        ys = [c[1] for p in rings for c in p[0]]
        kreise[str(g['id'])] = {'name': g['properties']['name'], 'polys': rings, 'bbox': (min(xs), min(ys), max(xs), max(ys))}
    return kreise


def im_ring(x, y, r):
    drin = False
    j = len(r) - 1
    for i in range(len(r)):
        xi, yi = r[i]
        xj, yj = r[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            drin = not drin
        j = i
    return drin


def kreis_von(lon, lat, kreise, ids):
    for k in ids:
        kk = kreise[k]
        b = kk['bbox']
        if lon < b[0] or lon > b[2] or lat < b[1] or lat > b[3]:
            continue
        for p in kk['polys']:
            if im_ring(lon, lat, p[0]) and not any(im_ring(lon, lat, h) for h in p[1:]):
                return k
    return None


# ---------------------------------------------------------------------------
# GTFS lesen (zeilenweise, ohne alles in den Speicher zu laden)
# ---------------------------------------------------------------------------
def gtfs_zeilen(z, name):
    with z.open(name) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig', newline='')):
            yield row


def gtfs_spalten(z, name, felder):
    """schneller als DictReader – für die große Datei stop_times.txt: liefert nur die gewünschten Spalten als Tupel"""
    with z.open(name) as f:
        r = csv.reader(io.TextIOWrapper(f, encoding='utf-8-sig', newline=''))
        kopf = next(r)
        idx = [kopf.index(x) for x in felder]
        for row in r:
            if len(row) > max(idx):
                yield tuple(row[i] for i in idx)


def herunterladen(url, ziel):
    log('Lade', url)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=600) as r, open(ziel, 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    log('Fertig:', round(os.path.getsize(ziel) / 1048576), 'MB')


def gtfs_auswerten(pfad, kreise, verbuende):
    alle_kreise = sorted({k for v in verbuende for k in v['kreise'] if k in kreise})
    bx = (min(kreise[k]['bbox'][0] for k in alle_kreise), min(kreise[k]['bbox'][1] for k in alle_kreise),
          max(kreise[k]['bbox'][2] for k in alle_kreise), max(kreise[k]['bbox'][3] for k in alle_kreise))
    z = zipfile.ZipFile(pfad)

    log('Haltestellen in der Region bestimmen …')
    region = {}    # stop_id → Landkreis
    cache = {}
    for s in gtfs_zeilen(z, 'stops.txt'):
        try:
            lat, lon = float(s['stop_lat']), float(s['stop_lon'])
        except (KeyError, ValueError):
            continue
        if bx[0] <= lon <= bx[2] and bx[1] <= lat <= bx[3]:
            schl = (round(lon, 4), round(lat, 4))
            if schl not in cache:
                cache[schl] = kreis_von(lon, lat, kreise, alle_kreise)
            k = cache[schl]
            if k:
                region[s['stop_id']] = k
    log(len(region), 'Haltestellen in der Region')

    log('Fahrten durchsuchen (1. Durchgang) …')
    laenge = Counter()
    treffer = defaultdict(Counter)    # trip_id → Landkreis → Anzahl Halte
    for t, sid in gtfs_spalten(z, 'stop_times.txt', ('trip_id', 'stop_id')):
        laenge[t] += 1
        k = region.get(sid)
        if k:
            treffer[t][k] += 1
    log(len(treffer), 'Fahrten berühren die Region')

    fahrten = {}
    for t in gtfs_zeilen(z, 'trips.txt'):
        if t['trip_id'] in treffer:
            fahrten[t['trip_id']] = (t['route_id'], t.get('direction_id') or '0', t.get('trip_headsign') or '')
    linien = {r['route_id']: r for r in gtfs_zeilen(z, 'routes.txt')}
    betreiber = {a.get('agency_id', ''): a.get('agency_name', '') for a in gtfs_zeilen(z, 'agency.txt')}

    # Je Linie und Richtung die Fahrt mit den meisten Halten (= vollständiger Linienweg)
    beste = {}
    for t, (rid, d, _) in fahrten.items():
        key = (rid, d)
        if key not in beste or laenge[t] > laenge[beste[key]]:
            beste[key] = t
    vertreter = set(beste.values())

    log('Halte der ausgewählten Fahrten lesen (2. Durchgang) …')
    folgen = defaultdict(list)
    for t, sid, nr in gtfs_spalten(z, 'stop_times.txt', ('trip_id', 'stop_id', 'stop_sequence')):
        if t in vertreter:
            try:
                folgen[t].append((int(nr), sid))
            except ValueError:
                pass
    noetig = {sid for f in folgen.values() for _, sid in f}
    halte = {}
    for s in gtfs_zeilen(z, 'stops.txt'):
        if s['stop_id'] in noetig:
            try:
                halte[s['stop_id']] = (float(s['stop_lon']), float(s['stop_lat']), s.get('stop_name', ''))
            except ValueError:
                pass

    # Linien zusammenstellen (je Linie die längere Richtung), Verbünde zuordnen
    je_linie = defaultdict(list)
    for (rid, d), t in beste.items():
        je_linie[rid].append(t)
    ergebnis = defaultdict(list)    # slug → Features
    for rid, trips in je_linie.items():
        r = linien.get(rid, {})
        ref = (r.get('route_short_name') or '').strip() or (r.get('route_long_name') or '').strip()
        if not ref:
            continue
        haupt = max(trips, key=lambda t: laenge[t])
        seq = [halte[sid] for _, sid in sorted(folgen[haupt]) if sid in halte]
        if len(seq) < 2:
            continue
        zaehler = Counter()
        for t in trips:
            zaehler.update(treffer[t])
        summe = sum(zaehler.values())
        try:
            art = ROUTE_TYPEN.get(int(r.get('route_type', 3)), None) or ('bus' if 700 <= int(r.get('route_type', 3)) < 800 else 'tram' if 900 <= int(r.get('route_type', 3)) < 1000 else 'bus')
        except ValueError:
            art = 'bus'
        for v in verbuende:
            anteil = sum(zaehler[k] for k in v['kreise']) / summe if summe else 0
            if anteil >= 0.25 and sum(zaehler[k] for k in v['kreise']) >= 2:
                von, nach = seq[0][2], seq[-1][2]
                ergebnis[v['slug']].append({
                    'type': 'Feature',
                    'properties': {
                        'ref': ref, 'name': '%s: %s => %s' % (ref, von, nach), 'from': von, 'to': nach,
                        'operator': betreiber.get(r.get('agency_id', ''), ''), 'network': v['kurz'], 'route': art,
                        'gtfs:route_id': rid, 'quelle': 'gtfs', 'verlauf': 'haltestellen',
                    },
                    'geometry': {'type': 'LineString', 'coordinates': [[round(x, 6), round(y, 6)] for x, y, _ in seq]},
                    '_halte': [(x, y) for x, y, _ in seq],
                })
    for slug in ergebnis:
        log(slug, len(ergebnis[slug]), 'Linien aus den Fahrplandaten')
    return ergebnis


# ---------------------------------------------------------------------------
# OpenStreetMap: straßengenaue Verläufe für dieselben Liniennummern
# ---------------------------------------------------------------------------
def re_werte(werte):
    out = []
    for w in sorted({str(x).strip() for x in werte if str(x).strip()}):
        w = ''.join('\\' + c if c in '.[](){}*+?^$|\\' else c for c in w)
        out.append(w.replace('\\', '\\\\').replace('"', '\\"'))
    return '|'.join(out)


def overpass_abfrage(v, refs):
    k = re_werte(v['kreise'])
    return ('[out:json][timeout:300];\n(\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:amtlicher_gemeindeschluessel"~"^(%s)"];\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:regionalschluessel"~"^(%s)"];\n'
            ')->.gebiet;\n'
            'relation["type"="route"]["route"~"^(bus|trolleybus|minibus|share_taxi|tram)$"]["ref"~"^(%s)$"](area.gebiet);\n'
            'out geom;\n') % (k, k, re_werte(refs))


def overpass_holen(abfrage, url):
    daten = urllib.parse.urlencode({'data': abfrage}).encode()
    for versuch in range(3):
        try:
            req = urllib.request.Request(url, data=daten, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=360) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:    # Overpass ist manchmal ausgelastet – kurz warten, erneut versuchen
            log('Overpass nicht erreichbar (%s), Versuch %d von 3' % (e, versuch + 1))
            time.sleep(30 * (versuch + 1))
    return None


def osm_linien(antwort):
    """Overpass-Antwort (out geom) → {ref: [ {id, linien: [[(lon, lat), …], …], tags} ]}"""
    out = defaultdict(list)
    for el in (antwort or {}).get('elements', []):
        if el.get('type') != 'relation':
            continue
        tags = el.get('tags', {})
        ref = (tags.get('ref') or '').strip()
        if not ref:
            continue
        linien = []
        for m in el.get('members', []):
            if m.get('type') != 'way' or not m.get('geometry') or str(m.get('role', '')).startswith(('platform', 'stop')):
                continue
            pts = [(p['lon'], p['lat']) for p in m['geometry'] if p and 'lon' in p]
            if len(pts) > 1:
                linien.append(pts)
        if linien:
            out[ref].append({'id': el['id'], 'linien': linien, 'tags': tags})
    return out


def abstand_m(p, linien):
    """kleinster Abstand (Meter) von Punkt p zu den Linien (örtliche Projektion)"""
    kx = 111320 * math.cos(math.radians(p[1]))
    ky = 110540
    best = float('inf')
    for l in linien:
        for (ax, ay), (bx, by) in zip(l, l[1:]):
            ax, ay, bx, by = (ax - p[0]) * kx, (ay - p[1]) * ky, (bx - p[0]) * kx, (by - p[1]) * ky
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            u = max(0, min(1, -(ax * dx + ay * dy) / ll)) if ll else 0
            best = min(best, math.hypot(ax + u * dx, ay + u * dy))
    return best


def verlauf_aus_osm(features, osm, nah=350, mindestens=0.6):
    """Ersetzt den Haltestellen-Verlauf durch den OSM-Verlauf, wenn die Haltestellen dicht daran liegen."""
    n = 0
    for f in features:
        kandidaten = osm.get(f['properties']['ref'], [])
        halte = f['_halte']
        probe = halte if len(halte) <= 40 else halte[::max(1, len(halte) // 40)]
        beste = None
        for k in kandidaten:
            quote = sum(1 for p in probe if abstand_m(p, k['linien']) <= nah) / len(probe)
            if quote >= mindestens and (not beste or quote > beste[0]):
                beste = (quote, k)
        if beste:
            k = beste[1]
            f['geometry'] = {'type': 'MultiLineString', 'coordinates': [[[round(x, 6), round(y, 6)] for x, y in l] for l in k['linien']]}
            f['properties']['verlauf'] = 'osm'
            f['properties']['@id'] = 'relation/%d' % k['id']
            n += 1
    return n


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='GöBUS – Liniennetze aus GTFS und OpenStreetMap')
    ap.add_argument('--gtfs', help='vorhandene GTFS-Datei (ZIP); sonst Download von GTFS.DE')
    ap.add_argument('--gtfs-url', default=GTFS_URL)
    ap.add_argument('--ausgabe', default=os.path.join(HIER, 'daten'))
    ap.add_argument('--ohne-osm', action='store_true', help='keine OpenStreetMap-Verläufe abfragen')
    ap.add_argument('--overpass-url', default=OVERPASS_URL)
    ap.add_argument('--osm-json', help='vorbereitete Overpass-Antworten statt Abfrage, z. B. osm/{slug}.json (für Tests)')
    a = ap.parse_args()

    kreise = landkreise(os.path.join(HIER, 'landkreise.topo.json'))
    verbuende = json.load(open(os.path.join(HIER, 'verbuende.json'), encoding='utf-8'))
    pfad = a.gtfs
    if not pfad:
        pfad = os.path.join(HIER, 'gtfs.zip')
        herunterladen(a.gtfs_url, pfad)
    ergebnis = gtfs_auswerten(pfad, kreise, verbuende)

    os.makedirs(a.ausgabe, exist_ok=True)
    stand = {'erzeugt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'verbuende': {}}
    for i, v in enumerate(verbuende):
        feats = ergebnis.get(v['slug'], [])
        osm_n = 0
        if feats and not a.ohne_osm:
            if a.osm_json:
                p = a.osm_json.replace('{slug}', v['slug'])
                antwort = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None
            else:
                if i:
                    time.sleep(15)    # Overpass schonen
                antwort = overpass_holen(overpass_abfrage(v, {f['properties']['ref'] for f in feats}), a.overpass_url)
            osm_n = verlauf_aus_osm(feats, osm_linien(antwort))
        for f in feats:
            f.pop('_halte', None)
        if not feats:
            log(v['slug'], 'keine Linien – Datei wird nicht geschrieben')
            continue
        geo = {'type': 'FeatureCollection', 'generator': 'GöBUS liniennetz.py', 'erzeugt': stand['erzeugt'],
               'attribution': 'Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0); Linienverläufe: OpenStreetMap-Mitwirkende (ODbL)',
               'features': sorted(feats, key=lambda f: (len(f['properties']['ref']), f['properties']['ref']))}
        ziel = os.path.join(a.ausgabe, v['datei'])
        with open(ziel + '.neu', 'w', encoding='utf-8') as fh:
            json.dump(geo, fh, ensure_ascii=False, separators=(',', ':'))
        os.replace(ziel + '.neu', ziel)
        stand['verbuende'][v['slug']] = {'datei': v['datei'], 'linien': len(feats), 'osm_verlauf': osm_n}
        log(v['slug'], len(feats), 'Linien gespeichert,', osm_n, 'mit Verlauf aus OpenStreetMap →', v['datei'])
    with open(os.path.join(a.ausgabe, 'stand.json'), 'w', encoding='utf-8') as fh:
        json.dump(stand, fh, ensure_ascii=False, indent=1)
    if not stand['verbuende']:
        sys.exit('Keine Linien gefunden – bitte Fahrplandaten prüfen.')


if __name__ == '__main__':
    main()
