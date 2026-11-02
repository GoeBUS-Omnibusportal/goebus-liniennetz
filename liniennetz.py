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
  5. Linienverlauf (Version 2, Update 24), in dieser Reihenfolge:
     a) Linienweg aus den Fahrplandaten (shapes.txt), wenn die Datei ihn enthält und er zu den Haltestellen passt,
     b) OpenStreetMap (straßengenau), wenn dort eine Linie derselben Nummer dicht an den Haltestellen verläuft –
        Nummern werden dabei vereinheitlicht („Bus 30“, „30“, „30;31“), Overpass mit mehreren Servern,
     c) der straßengenaue Verlauf der Vorwoche, wenn OpenStreetMap diesmal nicht erreichbar war,
     d) sonst Verbindung der Haltestellen.
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

VERSION = '2.0'
GTFS_URL = 'https://download.gtfs.de/germany/nv_free/latest.zip'
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
# Weitere öffentliche Overpass-Server, falls der Hauptserver ausgelastet ist (häufigste Ursache für fehlende Verläufe)
OVERPASS_WEITERE = ['https://overpass.kumi.systems/api/interpreter', 'https://overpass.private.coffee/api/interpreter']
HIER = os.path.dirname(os.path.abspath(__file__))
UA = 'GoeBUS-Liniennetz/' + VERSION + ' (+https://www.goebus.de)'
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
    FORMEN_IM_FEED.append('shapes.txt' in z.namelist())

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
    muster = {}                       # trip_id → Kennung der Haltestellenfolge (gleiche Folge = gleiches Muster)
    treffer = defaultdict(Counter)    # trip_id → Landkreis → Anzahl Halte
    for t, sid in gtfs_spalten(z, 'stop_times.txt', ('trip_id', 'stop_id')):
        laenge[t] += 1
        muster[t] = hash((muster.get(t, 0), sid))
        k = region.get(sid)
        if k:
            treffer[t][k] += 1
    log(len(treffer), 'Fahrten berühren die Region')

    fahrten = {}
    for t in gtfs_zeilen(z, 'trips.txt'):
        if t['trip_id'] in treffer:
            fahrten[t['trip_id']] = (t['route_id'], t.get('direction_id') or '0', t.get('trip_headsign') or '', t.get('shape_id') or '')
    linien = {r['route_id']: r for r in gtfs_zeilen(z, 'routes.txt')}
    betreiber = {a.get('agency_id', ''): a.get('agency_name', '') for a in gtfs_zeilen(z, 'agency.txt')}

    # Je Linie alle unterschiedlichen Fahrtmuster (Haltestellenfolgen), je Muster eine Fahrt –
    # so erscheinen auch Zweige, Schleifen und Abweichungen (Schulfahrten, Ortsdurchfahrten), nicht nur die längste Fahrt
    je_linie = defaultdict(dict)    # route_id → Muster → Fahrt
    for t, (rid, d, _, _) in fahrten.items():
        m = muster.get(t, t)
        if m not in je_linie[rid] or laenge[t] > laenge[je_linie[rid][m]]:
            je_linie[rid][m] = t
    MAX_MUSTER = 16
    for rid in je_linie:
        je_linie[rid] = sorted(je_linie[rid].values(), key=lambda t: -laenge[t])[:MAX_MUSTER]
    vertreter = {t for ts in je_linie.values() for t in ts}
    del muster

    log('Halte der ausgewählten Fahrten lesen (2. Durchgang) …', len(vertreter), 'Fahrtmuster')
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

    # Linienwege aus den Fahrplandaten (shapes.txt) – nur für die ausgewählten Fahrten
    formen = {}
    if 'shapes.txt' in z.namelist():
        noetig_f = {fahrten[t][3] for t in vertreter if fahrten.get(t) and fahrten[t][3]}
        roh = defaultdict(list)
        if noetig_f:
            for sid, la, lo, nr in gtfs_spalten(z, 'shapes.txt', ('shape_id', 'shape_pt_lat', 'shape_pt_lon', 'shape_pt_sequence')):
                if sid in noetig_f:
                    try:
                        roh[sid].append((int(float(nr)), float(lo), float(la)))
                    except ValueError:
                        pass
        formen = {k: [(x, y) for _, x, y in sorted(v)] for k, v in roh.items() if len(v) > 1}
        log('Linienwege aus den Fahrplandaten:', len(formen), 'von', len(noetig_f), 'benötigten')
    else:
        log('Die Fahrplandaten enthalten keine Linienwege (shapes.txt) – Verläufe kommen aus OpenStreetMap.')

    def form_von(t, folge):
        """Linienweg einer Fahrt, wenn er zu ihren Haltestellen passt (mindestens 80 % näher als 250 m)."""
        f = formen.get(fahrten.get(t, ('', '', '', ''))[3])
        if not f:
            return None
        r = Raster([f])
        probe = stichprobe(folge, 30)
        return [(round(x, 6), round(y, 6)) for x, y in vereinfachen(f)] if sum(1 for p in probe if r.abstand(p) <= 250) / len(probe) >= 0.8 else None

    ergebnis = defaultdict(list)    # slug → Features
    for rid, trips in je_linie.items():
        r = linien.get(rid, {})
        ref = (r.get('route_short_name') or '').strip() or (r.get('route_long_name') or '').strip()
        if not ref:
            continue
        # Muster ohne Mehrwert weglassen: Haltestellen vollständig in einem längeren Muster enthalten (auch Gegenrichtung)
        behalten = []
        for t in trips:
            ids = [sid for _, sid in sorted(folgen[t]) if sid in halte]
            if len(ids) < 2:
                continue
            menge = set(ids)
            if any(menge <= b[1] for b in behalten):
                continue
            behalten.append((t, menge, ids))
            if len(behalten) >= 8:
                break
        if not behalten:
            continue
        haupt = behalten[0][2]
        seqs = [[halte[sid] for sid in ids] for _, _, ids in behalten]
        fo = [form_von(t, [(halte[s][0], halte[s][1]) for s in ids]) for t, _, ids in behalten]
        zaehler = Counter()
        for t, _, _ in behalten:
            zaehler.update(treffer[t])
        summe = sum(zaehler.values())
        try:
            art = ROUTE_TYPEN.get(int(r.get('route_type', 3)), None) or ('bus' if 700 <= int(r.get('route_type', 3)) < 800 else 'tram' if 900 <= int(r.get('route_type', 3)) < 1000 else 'bus')
        except ValueError:
            art = 'bus'
        op = betreiber.get(r.get('agency_id', ''), '')
        for v in verbuende:
            im_verbund = sum(zaehler[k] for k in v['kreise'])
            anteil = im_verbund / summe if summe else 0
            eigener_betreiber = im_verbund >= 1 and any(b.lower() in op.lower() for b in v.get('betreiber', []) if b)
            if (anteil >= 0.25 and im_verbund >= 2) or eigener_betreiber:
                von, nach = halte[haupt[0]][2], halte[haupt[-1]][2]
                muster_l = [[(x, y) for x, y, _ in q] for q in seqs]
                geom, verlauf = geometrie(muster_l, fo)
                ergebnis[v['slug']].append({
                    'type': 'Feature',
                    'properties': {
                        'ref': ref, 'name': '%s: %s => %s' % (ref, von, nach), 'from': von, 'to': nach,
                        'operator': op, 'network': v['kurz'], 'route': art,
                        'gtfs:route_id': rid, 'quelle': 'gtfs', 'verlauf': verlauf, 'muster': len(seqs), 'halte': len(haupt),
                    },
                    'geometry': geom,
                    '_halte': list(dict.fromkeys((x, y) for q in seqs for x, y, _ in q)),
                    '_muster': muster_l,
                    '_formen': fo,
                })
    for slug in ergebnis:
        vorher = len(ergebnis[slug])
        ergebnis[slug] = zusammenfassen(ergebnis[slug])
        log(slug, len(ergebnis[slug]), 'Linien aus den Fahrplandaten', '(%d Einträge zusammengefasst)' % (vorher - len(ergebnis[slug])) if vorher > len(ergebnis[slug]) else '')
    return ergebnis


def vereinfachen(pts, toleranz=0.00004):
    """Punkte eines Linienwegs ausdünnen (Douglas-Peucker, etwa 4 m) – hält die Dateien klein."""
    if len(pts) < 3:
        return pts
    behalten = [False] * len(pts)
    behalten[0] = behalten[-1] = True
    stapel = [(0, len(pts) - 1)]
    while stapel:
        a, b = stapel.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll)) if ll else 0.0
            d = math.hypot(px - ax - u * dx, py - ay - u * dy)
            if d > best:
                best, idx = d, i
        if best > toleranz:
            behalten[idx] = True
            stapel += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, behalten) if k]


def geometrie(muster, formen):
    """Geometrie und Art des Verlaufs: Linienweg aus den Fahrplandaten, wo vorhanden, sonst Verbindung der Haltestellen."""
    formen = formen or [None] * len(muster)
    linien = [fo if fo else m for m, fo in zip(muster, formen)]
    n = sum(1 for fo in formen if fo)
    verlauf = 'fahrplan' if n == len(muster) else ('fahrplan+haltestellen' if n else 'haltestellen')
    coords = [[[round(x, 6), round(y, 6)] for x, y in l] for l in linien]
    return ({'type': 'LineString', 'coordinates': coords[0]} if len(coords) == 1 else {'type': 'MultiLineString', 'coordinates': coords}), verlauf


def zusammenfassen(feats):
    """Dieselbe Linie (Nummer und Betreiber) steht in den Fahrplandaten oft mehrfach (mehrere route_id, etwa je
    Fahrplanperiode oder Linienvariante). Diese Einträge werden zu einer Linie mit allen Fahrtmustern zusammengeführt."""
    gruppen = {}
    for f in feats:
        p = f['properties']
        gruppen.setdefault((p['ref'].strip().lower(), p['operator'].strip().lower()), []).append(f)
    out = []
    for gruppe in gruppen.values():
        if len(gruppe) == 1:
            out.append(gruppe[0])
            continue
        alle = sorted(((m, fo) for f in gruppe for m, fo in zip(f['_muster'], f.get('_formen') or [None] * len(f['_muster']))), key=lambda x: len(x[0]), reverse=True)
        paare = []
        for m, fo in alle:
            menge = set(m)
            if not any(menge <= set(b) for b, _ in paare):
                paare.append((m, fo))
            if len(paare) >= 12:
                break
        behalten = [m for m, _ in paare]
        geom, verlauf = geometrie(behalten, [fo for _, fo in paare])
        f = gruppe[0]
        laengste = max(gruppe, key=lambda g: len(g['_muster'][0]))
        p = dict(f['properties'])
        p.update({'from': laengste['properties']['from'], 'to': laengste['properties']['to'], 'name': laengste['properties']['name'],
                  'gtfs:route_id': ';'.join(dict.fromkeys(g['properties']['gtfs:route_id'] for g in gruppe)), 'muster': len(behalten), 'verlauf': verlauf})
        out.append({'type': 'Feature', 'properties': p, 'geometry': geom,
                    '_halte': list(dict.fromkeys(q for m in behalten for q in m)), '_muster': behalten, '_formen': [fo for _, fo in paare]})
    return out


# ---------------------------------------------------------------------------
# OpenStreetMap: straßengenaue Verläufe für dieselben Liniennummern
# ---------------------------------------------------------------------------
def re_werte(werte):
    out = []
    for w in sorted({str(x).strip() for x in werte if str(x).strip()}):
        w = ''.join('\\' + c if c in '.[](){}*+?^$|\\' else c for c in w)
        out.append(w.replace('\\', '\\\\').replace('"', '\\"'))
    return '|'.join(out)


def ref_norm(r):
    """Liniennummer vereinheitlicht: „Bus 30“, „Linie 30“, „30 “ → „30“; Groß-/Kleinschreibung egal."""
    import re
    r = ''.join(str(r).split()).lower()
    return re.sub(r'^(bus|linie|line|nr\.?)(?=[0-9a-z])', '', r) or r


def ref_muster(refs):
    """Overpass-Ausdruck für die Nummern: Leerzeichen beliebig, Vorsatz „Bus“/„Linie“ erlaubt, auch in Listen wie „30;31“."""
    sonder = '.[](){}*+?^$|\\'
    esc = lambda c: '\\\\' + c if c in sonder else ('\\"' if c == '"' else c)     # im QL-Text: \\. = Regex \.
    werte = [' *'.join(esc(c) for c in r) for r in sorted({ref_norm(x) for x in refs if str(x).strip()})]
    return '(^|;) *((bus|linie) *)?(%s) *(;|$)' % '|'.join(werte)


def overpass_abfrage(v, refs):
    k = re_werte(v['kreise'])
    return ('[out:json][timeout:300];\n(\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:amtlicher_gemeindeschluessel"~"^(%s)"];\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:regionalschluessel"~"^(%s)"];\n'
            ')->.gebiet;\n'
            'relation["type"="route"]["route"~"^(bus|trolleybus|minibus|share_taxi|tram)$"]["ref"~"%s",i](area.gebiet);\n'
            'out geom;\n') % (k, k, ref_muster(refs))


OVERPASS_STATUS = []
FORMEN_IM_FEED = []


def overpass_holen(abfrage, url):
    """Fragt Overpass ab – bei Fehlern, Zeitüberschreitung oder abgebrochener Antwort („remark“) auch die weiteren Server."""
    daten = urllib.parse.urlencode({'data': abfrage}).encode()
    server = [url] + [u for u in OVERPASS_WEITERE if u != url]
    for versuch in range(2):
        for u in server:
            try:
                req = urllib.request.Request(u, data=daten, headers={'User-Agent': UA, 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=360) as r:
                    j = json.loads(r.read().decode('utf-8'))
                bem = str(j.get('remark') or '')
                if 'error' in bem.lower() or 'timed out' in bem.lower():
                    raise RuntimeError('Antwort unvollständig: ' + bem[:120])
                OVERPASS_STATUS.append('ok: ' + u)
                return j
            except Exception as e:    # Overpass ist manchmal ausgelastet – anderen Server versuchen, später erneut
                log('Overpass %s nicht nutzbar (%s)' % (u, e))
                OVERPASS_STATUS.append('Fehler: %s (%s)' % (u, str(e)[:100]))
        time.sleep(45 * (versuch + 1))
    return None


def osm_linien(antwort):
    """Overpass-Antwort (out geom) → {ref: [ {id, linien: [[(lon, lat), …], …], tags} ]}"""
    out = defaultdict(list)
    for el in (antwort or {}).get('elements', []):
        if el.get('type') != 'relation':
            continue
        tags = el.get('tags', {})
        refs = {ref_norm(x) for x in str(tags.get('ref') or '').split(';') if x.strip()}
        if not refs:
            continue
        linien = []
        for m in el.get('members', []):
            if m.get('type') != 'way' or not m.get('geometry') or str(m.get('role', '')).startswith(('platform', 'stop')):
                continue
            pts = [(p['lon'], p['lat']) for p in m['geometry'] if p and 'lon' in p]
            if len(pts) > 1:
                linien.append(pts)
        if linien:
            for ref in refs:
                out[ref].append({'id': el['id'], 'linien': linien, 'tags': tags})
    return out


class Raster:
    """Schnelle Abstandssuche: Linienabschnitte in Rasterzellen (~0,008° ≈ 550–900 m) einsortiert."""
    Z = 0.008

    def __init__(self, linien):
        self.zellen = defaultdict(list)
        for l in linien:
            for a, b in zip(l, l[1:]):
                for cx in range(int(math.floor(min(a[0], b[0]) / self.Z)), int(math.floor(max(a[0], b[0]) / self.Z)) + 1):
                    for cy in range(int(math.floor(min(a[1], b[1]) / self.Z)), int(math.floor(max(a[1], b[1]) / self.Z)) + 1):
                        self.zellen[(cx, cy)].append((a, b))

    def abstand(self, p):
        """kleinster Abstand (Meter) von p zu den Abschnitten in der Umgebung; unendlich, wenn keiner in der Nähe"""
        kx = 111320 * math.cos(math.radians(p[1]))
        ky = 110540
        cx, cy = int(math.floor(p[0] / self.Z)), int(math.floor(p[1] / self.Z))
        best = float('inf')
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (ax, ay), (bx, by) in self.zellen.get((cx + dx, cy + dy), ()):
                    ax, ay, bx, by = (ax - p[0]) * kx, (ay - p[1]) * ky, (bx - p[0]) * kx, (by - p[1]) * ky
                    ddx, ddy = bx - ax, by - ay
                    ll = ddx * ddx + ddy * ddy
                    u = max(0, min(1, -(ax * ddx + ay * ddy) / ll)) if ll else 0
                    best = min(best, math.hypot(ax + u * ddx, ay + u * ddy))
        return best


def abstand_m(p, linien):
    """kleinster Abstand (Meter) von Punkt p zu den Linien (für einzelne Abfragen)"""
    return Raster(linien).abstand(p)


def stichprobe(punkte, n):
    return punkte if len(punkte) <= n else punkte[::max(1, len(punkte) // n)]


def verlauf_aus_osm(features, osm, nah=350, mindestens=0.6, variante=0.75):
    """Ersetzt den Haltestellen-Verlauf durch OSM-Verläufe derselben Nummer, wenn die Haltestellen dicht daran liegen.
    Übernommen werden alle passenden OSM-Linien (Hin- und Rückrichtung, Zweige). Abschnitte, die OpenStreetMap nicht
    abdeckt, bleiben als Verbindung der Haltestellen erhalten – so fehlt kein Teil der Linie."""
    n = 0
    for f in features:
        if f['properties'].get('verlauf') == 'fahrplan':
            continue    # Linienweg aus den Fahrplandaten ist vollständig – Vorrang
        kandidaten = osm.get(ref_norm(f['properties']['ref']), [])
        if not kandidaten:
            continue
        probe = stichprobe(f['_halte'], 40)
        eigen = Raster(f['_muster'])
        gewaehlt = []
        for k in kandidaten:
            r = Raster(k['linien'])
            quote = sum(1 for p in probe if r.abstand(p) <= nah) / len(probe)
            if quote >= mindestens:
                gewaehlt.append(k)
                continue
            # Zweig oder Teilstück: die OSM-Linie verläuft fast vollständig entlang dieser Linie
            pts = stichprobe([q for l in k['linien'] for q in l], 60)
            if quote >= 0.3 and sum(1 for q in pts if eigen.abstand(q) <= 500) / len(pts) >= variante:
                gewaehlt.append(k)
        if not gewaehlt:
            continue
        linien = [l for k in gewaehlt for l in k['linien']]
        # vorhandene Linienwege aus den Fahrplandaten bleiben erhalten (Teilverlauf „fahrplan+haltestellen“)
        linien += [fo for fo in (f.get('_formen') or []) if fo]
        r = Raster(linien)
        luecken = []
        for m, fo in zip(f['_muster'], f.get('_formen') or [None] * len(f['_muster'])):
            if fo:
                continue
            lauf = []
            for i, p in enumerate(m):
                if r.abstand(p) > nah:
                    if not lauf and i > 0:
                        lauf.append(m[i - 1])
                    lauf.append(p)
                elif lauf:
                    lauf.append(p)
                    luecken.append(lauf)
                    lauf = []
            if len(lauf) >= 2:
                luecken.append(lauf)
        f['geometry'] = {'type': 'MultiLineString', 'coordinates': [[[round(x, 6), round(y, 6)] for x, y in l] for l in linien + luecken]}
        f['properties']['verlauf'] = 'osm+haltestellen' if luecken else 'osm'
        f['properties']['@id'] = ';'.join('relation/%d' % k['id'] for k in gewaehlt[:6])
        n += 1
    return n


def gedaechtnis(feats, alt_pfad, heute, tage=56):
    """Linien aus der Vorwoche behalten, die diesmal fehlen (z. B. Schulfahrten in den Ferien: Die freien
    Fahrplandaten gelten nur für 7 Tage). Nach `tage` Tagen ohne Fahrt verschwindet eine Linie."""
    if not os.path.exists(alt_pfad):
        return 0
    try:
        alt = json.load(open(alt_pfad, encoding='utf-8'))
    except (ValueError, OSError):
        return 0
    schl = lambda p: (str(p.get('ref', '')).strip().lower(), str(p.get('operator', '')).strip().lower())
    vorhanden = {schl(f['properties']) for f in feats}
    grenze = time.strftime('%Y-%m-%d', time.gmtime(time.time() - tage * 86400))
    n = 0
    for f in alt.get('features', []):
        p = f.get('properties', {})
        gesehen = str(p.get('gesehen') or alt.get('erzeugt', '')[:10] or heute)
        if schl(p) in vorhanden or gesehen < grenze or not f.get('geometry'):
            continue
        p['gesehen'] = gesehen
        p['aus_vorwoche'] = True
        feats.append(f)
        vorhanden.add(schl(p))
        n += 1
    return n


def ref_schluessel(r):
    return ref_norm(r)


STRASSENGENAU = ('fahrplan', 'osm', 'fahrplan+haltestellen', 'osm+haltestellen')


def verlauf_vorwoche(feats, alt_pfad, nah=350):
    """War OpenStreetMap diesmal nicht erreichbar (oder fehlt die Route dort neu), bleibt der straßengenaue Verlauf der
    Vorwoche – sofern die Haltestellen weiterhin dicht daran liegen. So werden es nie weniger Verläufe als vorher."""
    if not os.path.exists(alt_pfad):
        return 0
    try:
        alt = json.load(open(alt_pfad, encoding='utf-8'))
    except (ValueError, OSError):
        return 0
    vorher = {}
    for f in alt.get('features', []):
        p = f.get('properties', {})
        if p.get('verlauf') in STRASSENGENAU and f.get('geometry'):
            vorher[(ref_norm(p.get('ref', '')), str(p.get('operator', '')).strip().lower())] = f
    n = 0
    for f in feats:
        p = f['properties']
        if p.get('verlauf') != 'haltestellen':
            continue
        a = vorher.get((ref_norm(p['ref']), str(p.get('operator', '')).strip().lower()))
        if not a:
            continue
        g = a['geometry']
        linien = [[tuple(c) for c in g['coordinates']]] if g['type'] == 'LineString' else [[tuple(c) for c in l] for l in g['coordinates']]
        r = Raster(linien)
        probe = stichprobe(f['_halte'], 40)
        if sum(1 for q in probe if r.abstand(q) <= nah) / len(probe) < 0.6:
            continue
        f['geometry'] = g
        p['verlauf'] = a['properties']['verlauf']
        p['verlauf_vorwoche'] = True
        if a['properties'].get('@id'):
            p['@id'] = a['properties']['@id']
        n += 1
    return n


def pruefung_verbund(feats):
    """Plausibilität der Fahrplandaten je Verbund: Auffälligkeiten, die auf falsche oder unvollständige Daten hindeuten."""
    nummern = defaultdict(set)
    for f in feats:
        nummern[ref_norm(f['properties']['ref'])].add(f['properties'].get('operator') or '(ohne Angabe)')
    erg = {
        'ohne_betreiber': sorted({f['properties']['ref'] for f in feats if not f['properties'].get('operator')}),
        'wenige_halte': sorted({f['properties']['ref'] for f in feats if 0 < int(f['properties'].get('halte') or 99) < 3}),
        'mehrere_betreiber': sorted('%s (%s)' % (r, ', '.join(sorted(b))) for r, b in nummern.items() if len(b) > 1),
        'ohne_verlauf': sorted({f['properties']['ref'] for f in feats if f['properties'].get('verlauf') == 'haltestellen'}, key=lambda r: (len(r), r)),
    }
    return erg


def overpass_alle_abfrage(v):
    """Alle Buslinien (nur Merkmale) im Gebiet des Verbunds – für den Abgleich „nur in OpenStreetMap“."""
    k = re_werte(v['kreise'])
    return ('[out:json][timeout:180];\n(\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:amtlicher_gemeindeschluessel"~"^(%s)"];\n'
            '  area["boundary"="administrative"]["admin_level"="6"]["de:regionalschluessel"~"^(%s)"];\n'
            ')->.gebiet;\n'
            'relation["type"="route"]["route"~"^(bus|trolleybus|minibus|share_taxi|tram)$"](area.gebiet);\n'
            'out tags;\n') % (k, k)


def osm_refs_im_netz(antwort, v):
    """Liniennummern der OSM-Routen, deren network-Angabe zum Verbund passt (Kurzname oder Name)."""
    namen = [str(v.get('kurz', '')).lower()] + [str(x).lower() for x in v.get('osm_netze', [])]
    out = set()
    for el in (antwort or {}).get('elements', []):
        t = el.get('tags', {})
        ref = (t.get('ref') or '').strip()
        netz = (t.get('network') or '').lower()
        if ref and netz and any(n and n in netz for n in namen):
            out.add(ref)
    return out


def quellen_refs(v, holen=None):
    """Liniennummern aus den Linienübersichten des Verbunds (Einträge „quellen“ in verbuende.json)."""
    import re
    refs, fehler = set(), []
    for q in v.get('quellen', []):
        url = q.get('url', '') if isinstance(q, dict) else str(q)
        muster = (q.get('muster') if isinstance(q, dict) else None) or r'(?:Linie|Line|Bus)\s*([A-Z]{0,3}\s?\d{1,4}[a-z]?)\b'
        if not url.startswith('https://'):
            continue
        try:
            text = holen(url) if holen else urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=60).read().decode('utf-8', 'replace')
        except Exception as e:
            fehler.append('%s: %s' % (url, e))
            continue
        for m in re.finditer(muster, text):
            refs.add(m.group(1).strip())
    return refs, fehler


def abgleich_verbund(v, feats, alt_refs, osm_alle, quelle):
    daten = {ref_schluessel(f['properties']['ref']): f['properties']['ref'] for f in feats if not f['properties'].get('aus_vorwoche')}
    alle = {ref_schluessel(f['properties']['ref']) for f in feats}
    sortiert = lambda xs: sorted(xs, key=lambda r: (len(r), r))
    erg = {
        'verschwunden': sortiert(r for k, r in alt_refs.items() if k not in daten),
        'neu': sortiert(r for k, r in daten.items() if alt_refs and k not in alt_refs),
        'nur_osm': sortiert(r for r in osm_alle if ref_schluessel(r) not in alle),
        'nur_quelle': sortiert(r for r in quelle[0] if ref_schluessel(r) not in alle),
        'nur_soll': sortiert(str(r) for r in v.get('soll', []) if ref_schluessel(r) not in alle),
        'quellen_fehler': quelle[1],
    }
    return erg


def abgleich_markdown(bericht):
    z = ['# Abgleich der Liniennetze', '', 'Erzeugt: %s · Skript %s · Quelle: DELFI über GTFS.DE (CC BY 4.0), OpenStreetMap (ODbL)' % (bericht['erzeugt'], bericht.get('version', '')), '']
    if bericht.get('overpass'):
        z += ['Overpass: ' + ' · '.join(bericht['overpass'][-6:]), '']
    for slug, b in bericht['verbuende'].items():
        a = b.get('abgleich', {})
        z += ['## %s – %d Linien' % (slug.upper(), b['linien']), '']
        vl = b.get('verlauf', {})
        z.append('- **Verlauf:** %d Linienweg aus den Fahrplandaten, %d OpenStreetMap, %d teilweise, %d nur Haltestellen%s · OpenStreetMap: %s'
                 % (vl.get('fahrplan', 0), vl.get('osm', 0), vl.get('osm+haltestellen', 0) + vl.get('fahrplan+haltestellen', 0), vl.get('haltestellen', 0),
                    ' (%d aus der Vorwoche übernommen)' % b['verlauf_vorwoche'] if b.get('verlauf_vorwoche') else '', b.get('osm', '?')))
        pr = b.get('pruefung', {})
        for key, titel in [('ohne_betreiber', 'Ohne Betreiber in den Fahrplandaten'), ('wenige_halte', 'Weniger als drei Halte'),
                           ('mehrere_betreiber', 'Gleiche Nummer, mehrere Betreiber'), ('ohne_verlauf', 'Ohne straßengenauen Verlauf (in OpenStreetMap ergänzen)')]:
            if pr.get(key):
                z.append('- **%s:** %s' % (titel, ', '.join(pr[key][:60])))
        for key, titel in [('verschwunden', 'Diese Woche nicht in den Fahrplandaten (aus der Vorwoche behalten)'), ('neu', 'Neu in den Fahrplandaten'),
                           ('nur_osm', 'Nur in OpenStreetMap (network passt zum Verbund)'), ('nur_quelle', 'Nur in der Linienübersicht des Verbunds'),
                           ('nur_soll', 'Laut Soll-Liste fehlend'), ('quellen_fehler', 'Linienübersicht nicht abrufbar')]:
            werte = a.get(key) or []
            z.append('- **%s:** %s' % (titel, ', '.join(werte) if werte else 'keine'))
        z.append('')
    return '\n'.join(z) + '\n'


def bericht_verbund(v, feats):
    betr = Counter(f['properties'].get('operator') or '(ohne Angabe)' for f in feats)
    verlauf = Counter(f['properties'].get('verlauf', '') for f in feats)
    return {
        'linien': len(feats), 'verlauf': dict(verlauf), 'aus_vorwoche': sum(1 for f in feats if f['properties'].get('aus_vorwoche')),
        'betreiber': dict(betr.most_common()),
        'liste': [{'ref': f['properties']['ref'], 'betreiber': f['properties'].get('operator', ''), 'von': f['properties'].get('from', ''),
                   'nach': f['properties'].get('to', ''), 'verlauf': f['properties'].get('verlauf', ''), 'muster': f['properties'].get('muster', 1),
                   'gesehen': f['properties'].get('gesehen', '')} for f in feats],
    }


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='GöBUS – Liniennetze aus GTFS und OpenStreetMap')
    ap.add_argument('--gtfs', help='vorhandene GTFS-Datei (ZIP); sonst Download von GTFS.DE')
    ap.add_argument('--gtfs-url', default=GTFS_URL)
    ap.add_argument('--ausgabe', default=os.path.join(HIER, 'daten'))
    ap.add_argument('--ohne-osm', action='store_true', help='keine OpenStreetMap-Verläufe abfragen')
    ap.add_argument('--overpass-url', default=OVERPASS_URL)
    ap.add_argument('--verbuende', default=os.path.join(HIER, 'verbuende.json'), help='Verbünde (Vorgabe: verbuende.json neben dem Skript)')
    ap.add_argument('--behalten-tage', type=int, default=56, help='fehlende Linien so viele Tage aus der Vorwoche behalten (0 = aus)')
    ap.add_argument('--osm-json', help='vorbereitete Overpass-Antworten statt Abfrage, z. B. osm/{slug}.json (für Tests)')
    a = ap.parse_args()

    kreise = landkreise(os.path.join(HIER, 'landkreise.topo.json'))
    verbuende = json.load(open(a.verbuende, encoding='utf-8'))
    pfad = a.gtfs
    if not pfad:
        pfad = os.path.join(HIER, 'gtfs.zip')
        herunterladen(a.gtfs_url, pfad)
    ergebnis = gtfs_auswerten(pfad, kreise, verbuende)

    os.makedirs(a.ausgabe, exist_ok=True)
    stand = {'erzeugt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'version': VERSION, 'linienwege': bool(FORMEN_IM_FEED and FORMEN_IM_FEED[0]), 'verbuende': {}}
    bericht = {'erzeugt': stand['erzeugt'], 'version': VERSION, 'hinweis': 'Übersicht je Verbund: Linien, Betreiber, Verlauf (fahrplan = Linienweg aus den Fahrplandaten, osm = straßengenau aus OpenStreetMap, …+haltestellen = teilweise, haltestellen = Verbindung der Haltestellen).', 'verbuende': {}}
    for i, v in enumerate(verbuende):
        feats = ergebnis.get(v['slug'], [])
        osm_n = 0
        osm_status = 'aus' if a.ohne_osm else 'nicht nötig'
        if feats and not a.ohne_osm:
            if a.osm_json:
                p = a.osm_json.replace('{slug}', v['slug'])
                antwort = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None
            else:
                if i:
                    time.sleep(15)    # Overpass schonen
                antwort = overpass_holen(overpass_abfrage(v, {f['properties']['ref'] for f in feats}), a.overpass_url)
            osm_status = 'ok' if antwort is not None else 'nicht erreichbar'
            osm_n = verlauf_aus_osm(feats, osm_linien(antwort))
        heute = stand['erzeugt'][:10]
        ziel0 = os.path.join(a.ausgabe, v['datei'])
        vw_n = verlauf_vorwoche(feats, ziel0)
        fp_n = sum(1 for f in feats if str(f['properties'].get('verlauf', '')).startswith('fahrplan'))
        alt_refs = {}
        if os.path.exists(ziel0):
            try:
                alt_refs = {ref_schluessel(f['properties']['ref']): f['properties']['ref'] for f in json.load(open(ziel0, encoding='utf-8')).get('features', [])
                            if not f.get('properties', {}).get('aus_vorwoche')}
            except (ValueError, OSError, KeyError):
                alt_refs = {}
        osm_alle = set()
        if not a.ohne_osm:
            if a.osm_json:
                p2 = a.osm_json.replace('{slug}', v['slug'] + '-alle')
                osm_alle = osm_refs_im_netz(json.load(open(p2, encoding='utf-8')) if os.path.exists(p2) else None, v)
            elif feats:
                time.sleep(5)
                osm_alle = osm_refs_im_netz(overpass_holen(overpass_alle_abfrage(v), a.overpass_url), v)
        quelle = quellen_refs(v) if v.get('quellen') else (set(), [])
        for f in feats:
            f.pop('_halte', None)
            f.pop('_muster', None)
            f.pop('_formen', None)
            f['properties']['gesehen'] = heute
        ziel = os.path.join(a.ausgabe, v['datei'])
        alt_n = gedaechtnis(feats, ziel, heute, a.behalten_tage) if a.behalten_tage > 0 else 0
        if not feats:
            log(v['slug'], 'keine Linien – Datei wird nicht geschrieben')
            continue
        geo = {'type': 'FeatureCollection', 'generator': 'GöBUS liniennetz.py', 'erzeugt': stand['erzeugt'],
               'attribution': 'Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0); Linienverläufe: OpenStreetMap-Mitwirkende (ODbL)',
               'features': sorted(feats, key=lambda f: (len(f['properties']['ref']), f['properties']['ref']))}
        with open(ziel + '.neu', 'w', encoding='utf-8') as fh:
            json.dump(geo, fh, ensure_ascii=False, separators=(',', ':'))
        os.replace(ziel + '.neu', ziel)
        stand['verbuende'][v['slug']] = {'datei': v['datei'], 'linien': len(feats), 'osm_verlauf': osm_n, 'fahrplan_verlauf': fp_n, 'verlauf_vorwoche': vw_n, 'aus_vorwoche': alt_n, 'osm': osm_status}
        bericht['verbuende'][v['slug']] = bericht_verbund(v, feats)
        bericht['verbuende'][v['slug']]['abgleich'] = abgleich_verbund(v, feats, alt_refs, osm_alle, quelle)
        bericht['verbuende'][v['slug']]['pruefung'] = pruefung_verbund(feats)
        bericht['verbuende'][v['slug']]['osm'] = osm_status
        bericht['verbuende'][v['slug']]['verlauf_vorwoche'] = vw_n
        log(v['slug'], len(feats), 'Linien gespeichert:', fp_n, 'mit Linienweg aus den Fahrplandaten,', osm_n, 'aus OpenStreetMap (%s),' % osm_status, vw_n, 'Verläufe aus der Vorwoche,', alt_n, 'Linien aus den Vorwochen →', v['datei'])
    stand['overpass'] = OVERPASS_STATUS[-12:]
    bericht['overpass'] = stand['overpass']
    with open(os.path.join(a.ausgabe, 'stand.json'), 'w', encoding='utf-8') as fh:
        json.dump(stand, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.ausgabe, 'bericht.json'), 'w', encoding='utf-8') as fh:
        json.dump(bericht, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(a.ausgabe, 'abgleich.md'), 'w', encoding='utf-8') as fh:
        fh.write(abgleich_markdown(bericht))
    if not stand['verbuende']:
        sys.exit('Keine Linien gefunden – bitte Fahrplandaten prüfen.')


if __name__ == '__main__':
    main()
