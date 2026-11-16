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
        Nummern werden dabei vereinheitlicht („Bus 30“, „30“, „30;31“). Version 2.4 (Fassung 48): aus dem lokalen Auszug
        der Geofabrik (osm/auszug.opl, im Workflow mit osmium erzeugt) – ohne Overpass. Nur ohne Auszug wird Overpass
        gefragt, mit kurzer Wartezeit, ohne Wiederholung nicht erreichbarer Server und höchstens 12 Minuten insgesamt,
     c) der straßengenaue Verlauf der Vorwoche, wenn OpenStreetMap diesmal nicht erreichbar war,
     d) sonst Verbindung der Haltestellen.
  6. Je Verbund eine Datei im bisherigen Format schreiben (daten/vsn_linien.json usw.).

Aufruf
  python liniennetz.py                         # alles (Download, OpenStreetMap, Ausgabe nach daten/)
  python liniennetz.py --gtfs feed.zip         # vorhandene GTFS-Datei verwenden
  python liniennetz.py --ohne-osm              # nur Haltestellen-Verläufe (schneller)
  python liniennetz.py --osm-auszug osm/auszug.opl   # OpenStreetMap aus dem Geofabrik-Auszug (Vorgabe, wenn vorhanden)
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict

VERSION = '2.9'
GTFS_URL = 'https://download.gtfs.de/germany/nv_free/latest.zip'
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
# Weitere öffentliche Overpass-Server, falls der Hauptserver ausgelastet ist (häufigste Ursache für fehlende Verläufe)
OVERPASS_WEITERE = ['https://overpass.kumi.systems/api/interpreter', 'https://overpass.private.coffee/api/interpreter']
HIER = os.path.dirname(os.path.abspath(__file__))
UA = 'GoeBUS-Liniennetz/' + VERSION + ' (+https://www.goebus.de)'
HALTE_REGION = []  # Version 2.3: (lon, lat, Name, Suchname) aller Haltestellen der Region – für Linien aus der offiziellen Linienliste
EIN = {'5/43'}     # Version 2.3: zusammengesetzte Nummern, die trotzdem aufgenommen werden (ROSA 5/43; weitere aus den Regeln „ein“)
LISTEN = {}        # Version 2.3: slug → {Nummer (Schlüssel): (Nummer, Linienweg, Hinweis)} aus der Website
TAGESFAHRTEN = {}  # Version 2.5: {'stichtag': 'JJJJ-MM-TT', 'zeiten': {stop_id: [Minuten]}, 'je': {stop_id: Counter(Nummer)}, 'name': {stop_id: Name}}
SKIZZE = {'an': os.environ.get('GOEBUS_SKIZZE', '1') != '0', 'cache': None, 'ende': 0}   # Version 2.8: Skizzen über die Straßen
FOLGEN = {}        # Version 2.7: Verbund (Kurzname) → Nummer → [Haltestellennamen in Fahrtrichtung des häufigsten Musters]
HALTESTELLEN = {}  # Version 2.4: Name → {'p': [(lon, lat) der Steige], 'l': {(Nummer, Verbund)}} – für daten/haltestellen.json
HALTE_ALLE = []   # (lon, lat, Name) aller Haltestellen der ausgewählten Fahrten – für „von → nach“, wenn der Linienweg fehlt
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


def linie_zeiten(ms, uhr):
    """2.7: erste und letzte Abfahrt einer Linie an einer Haltestelle und ihr üblicher Takt (Minuten, Median der Abstände
    zwischen 6 und 19 Uhr; 0, wenn dort weniger als drei Abfahrten liegen)."""
    ms = sorted(ms)
    tags = [m for m in ms if 360 <= m < 1140]
    luecken = sorted(b - a for a, b in zip(tags, tags[1:]) if b > a)
    takt = luecken[len(luecken) // 2] if len(tags) >= 3 and luecken else 0
    return [uhr(ms[0]), uhr(ms[-1]), takt]


def weg_aus_folge(namen, hoechstens=7):
    """2.8: Linienweg aus der Haltestellenfolge der häufigsten Fahrt – Orte statt Haltestellen („Wolfsburg – Fallersleben –
    Gifhorn“). Bleibt die Linie in einem Ort, die Haltestellen ohne Ortsnamen („Bahnhof – Weende – Nikolausberg“).
    Höchstens sieben Glieder, Anfang und Ende immer dabei."""
    namen = [str(n or '').strip() for n in namen if str(n or '').strip()]
    if len(namen) < 2:
        return ''
    def ort(n):
        return re.split(r'\s*,\s*', n, 1)[0].strip()
    def halt(n):
        t = re.split(r'\s*,\s*', n, 1)
        return (t[1] if len(t) > 1 else t[0]).strip()
    orte = []
    for n in namen:
        o = ort(n)
        if not orte or orte[-1] != o:
            orte.append(o)
    glieder = orte if len(orte) >= 3 or (len(orte) == 2 and len(namen) <= 3) else None
    if glieder is None:    # Stadtlinie (oder nur zwei Orte): Haltestellen zeigen
        glieder = []
        for n in namen:
            h = halt(n)
            if not glieder or glieder[-1] != h:
                glieder.append(h)
        if len(orte) == 2:
            glieder[0], glieder[-1] = namen[0], namen[-1]
    if len(glieder) > hoechstens:
        schritt = (len(glieder) - 1) / (hoechstens - 1)
        glieder = [glieder[round(i * schritt)] for i in range(hoechstens)]
    glieder = [g for i, g in enumerate(glieder) if i == 0 or g != glieder[i - 1]]
    return ' – '.join(glieder) if len(glieder) >= 2 else ''


SKIZZE_URL = os.environ.get('GOEBUS_SKIZZE_URL', 'https://router.project-osrm.org/route/v1/driving/')


def skizze_teil(t, cache, ende):
    """Eine gerade Verbindung (Haltestellen in Fahrtreihenfolge) über die Straßen legen. Rückgabe: Liste der Punkte,
    [] = bleibt gerade (Umweg über 2,5-fach oder kein Weg), None = Zeitbudget aufgebraucht oder Server nicht erreichbar."""
    schluessel = ';'.join('%.5f,%.5f' % (x, y) for x, y in t)
    if schluessel in cache:
        return cache[schluessel]
    if time.time() > ende or SKIZZE.get('abbruch'):
        return None
    weg = []
    for i in range(0, len(t) - 1, 24):
        stueck = t[i:i + 25]
        url = SKIZZE_URL + ';'.join('%.6f,%.6f' % (x, y) for x, y in stueck) + '?overview=full&geometries=geojson&continue_straight=false'
        try:
            time.sleep(1.05)
            req = urllib.request.Request(url, headers={'User-Agent': 'GoeBUS-Liniennetz/%s (+https://goebus.de)' % VERSION})
            with urllib.request.urlopen(req, timeout=20) as r:
                antwort = json.load(r)
            c = antwort['routes'][0]['geometry']['coordinates']
        except Exception as e:    # Server nicht erreichbar oder Rate-Limit: für diesen Lauf aufhören
            log('Skizze: Abbruch –', type(e).__name__, str(e)[:80])
            SKIZZE['abbruch'] = True
            return None
        weg.extend(c if not weg else c[1:])
    gerade = sum(_m(a, b) for a, b in zip(t, t[1:]))
    strasse = sum(_m(a, b) for a, b in zip(weg, weg[1:]))
    if not weg or strasse > 2.5 * gerade + 2000:
        weg = []
    cache[schluessel] = [[round(x, 6), round(y, 6)] for x, y in vereinfachen([tuple(c) for c in weg])] if weg else []
    return cache[schluessel]


def skizzen(feats, cache, ende):
    """2.8: Linien, deren Verlauf nur die Verbindung der Haltestellen ist (gerade Striche), über die Straßen zwischen den
    Haltestellen skizzieren (OSRM, öffentlicher Server; höchstens eine Anfrage je Sekunde, Zeitbudget, Zwischenspeicher
    daten/skizzen.json). Verworfen, wenn der Weg mehr als das 2,5-Fache der geraden Strecke misst.
    2.9: auch die geraden Lücken gemischter Verläufe („osm+haltestellen“, „fahrplan+haltestellen“ → „osm+skizze“ …) und alle
    Teile (bisher höchstens vier). Eine Linie wird erst umgestellt, wenn alle ihre Lücken berechnet sind; was schon berechnet ist,
    liegt im Zwischenspeicher, der nächste Lauf macht dort weiter. → Anzahl skizzierter Linien"""
    n = 0
    for f in feats:
        p, g = f['properties'], f.get('geometry') or {}
        v = str(p.get('verlauf', ''))
        if not v.endswith('haltestellen') or g.get('type') not in ('LineString', 'MultiLineString'):
            continue
        teile = [g['coordinates']] if g['type'] == 'LineString' else list(g['coordinates'])
        k = len(teile) if v == 'haltestellen' else int(p.get('luecken') or 0)
        if k <= 0 or k > len(teile):
            continue
        neu, fertig, skizziert = list(teile[:len(teile) - k]), True, 0
        for t in teile[len(teile) - k:]:
            if len(t) < 2 or sum(_m(a, b) for a, b in zip(t, t[1:])) < 150:    # sehr kurze Stücke bleiben, wie sie sind
                neu.append(t)
                continue
            weg = skizze_teil([tuple(c) for c in t], cache, ende)
            if weg is None:
                fertig = False
                break
            neu.append(weg if weg else t)
            skizziert += 1 if weg else 0
        if not fertig or not skizziert:
            continue
        f['geometry'] = {'type': 'LineString', 'coordinates': neu[0]} if len(neu) == 1 else {'type': 'MultiLineString', 'coordinates': neu}
        p['verlauf'] = 'skizze' if v == 'haltestellen' else v[:-len('haltestellen')] + 'skizze'
        n += 1
    return n


def stichtag(heute=None):
    """nächster Mittwoch (mindestens zwei Tage voraus) – ein gewöhnlicher Werktag ohne Wochenend- und Montagsbesonderheiten"""
    import datetime as _dt
    if not heute and re.match(r'^\d{4}-\d\d-\d\d$', os.environ.get('GOEBUS_STICHTAG', '')):    # Prüfumgebung
        return _dt.date.fromisoformat(os.environ['GOEBUS_STICHTAG'])
    d = heute or _dt.date.today()
    d += _dt.timedelta(days=2)
    while d.weekday() != 2:
        d += _dt.timedelta(days=1)
    return d


def dienste_am(z, tag):
    """service_id, die am Tag fahren (calendar.txt mit Wochentag und Zeitraum, calendar_dates.txt mit Ausnahmen)"""
    ds = tag.strftime('%Y%m%d')
    wt = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')[tag.weekday()]
    aktiv = set()
    namen = z.namelist()
    if 'calendar.txt' in namen:
        for c in gtfs_zeilen(z, 'calendar.txt'):
            if c.get(wt) == '1' and c.get('start_date', '0') <= ds <= c.get('end_date', '99999999'):
                aktiv.add(c['service_id'])
    if 'calendar_dates.txt' in namen:
        for sid, dt, art in gtfs_spalten(z, 'calendar_dates.txt', ('service_id', 'date', 'exception_type')):
            if dt == ds:
                (aktiv.add if art == '1' else aktiv.discard)(sid)
    return aktiv


def tagesfahrten(z, fahrten, dienst, linien, region):
    """Abfahrtszeiten je Haltestelle am Stichtag (nur Busse, Straßenbahnen; ohne Ankunft an der Endhaltestelle)"""
    tag = stichtag()
    aktiv = dienste_am(z, tag)
    def nummer(rid):
        r = linien.get(rid, {})
        try:
            rt = int(r.get('route_type', 3))
        except ValueError:
            rt = 3
        if rt == 2 or 100 <= rt < 200 or 1500 <= rt < 1600:
            return None
        ref = (r.get('route_short_name') or '').strip() or (r.get('route_long_name') or '').strip()
        return re.sub(r'^(?:Stadt)?(?:Bus|Buslinie|Linie)\s+', '', ref, flags=re.I).strip() or None
    nr_von = {}
    heute = {}
    for t, (rid, _, _, _) in fahrten.items():
        if dienst.get(t) in aktiv:
            if rid not in nr_von:
                nr_von[rid] = nummer(rid)
            if nr_von[rid]:
                heute[t] = nr_von[rid]
    log('Abfahrten am %s: %d Fahrten in der Region' % (tag.strftime('%d.%m.%Y'), len(heute)))
    zeiten, je, ende, jl = defaultdict(list), defaultdict(Counter), {}, defaultdict(lambda: defaultdict(list))
    for t, sid, ab, nr in gtfs_spalten(z, 'stop_times.txt', ('trip_id', 'stop_id', 'departure_time', 'stop_sequence')):
        ref = heute.get(t)
        if ref is None or sid not in region:
            continue
        try:
            h, m = ab.split(':')[:2]
            mi, seq = int(h) * 60 + int(m), int(nr)
        except ValueError:
            continue
        zeiten[sid].append(mi)
        je[sid][ref] += 1
        jl[sid][ref].append(mi)    # 2.7: Zeiten je Linie (Takt, erste/letzte Fahrt)
        if t not in ende or seq > ende[t][0]:
            ende[t] = (seq, sid, mi)
    for t, (_, sid, mi) in ende.items():   # Ankunft an der Endhaltestelle ist keine Abfahrt
        try:
            zeiten[sid].remove(mi)
            je[sid][heute[t]] -= 1
            jl[sid][heute[t]].remove(mi)
        except (ValueError, KeyError):
            pass
    TAGESFAHRTEN.update({'stichtag': tag.isoformat(), 'zeiten': zeiten, 'je': je, 'linien': jl})


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
                TAGESFAHRTEN.setdefault('name', {})[s['stop_id']] = s.get('stop_name', '')
                if (s.get('platform_code') or '').strip():    # 2.7: Steig (für den Steigplan der Haltestellenseite)
                    TAGESFAHRTEN.setdefault('steig', {})[s['stop_id']] = s['platform_code'].strip()[:12]
                if LISTEN:
                    HALTE_REGION.append((lon, lat, s.get('stop_name', ''), halt_norm(s.get('stop_name', ''))))
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
    dienst = {}    # 2.5: trip_id → service_id (Fahrten je Werktag)
    for t in gtfs_zeilen(z, 'trips.txt'):
        if t['trip_id'] in treffer:
            fahrten[t['trip_id']] = (t['route_id'], t.get('direction_id') or '0', t.get('trip_headsign') or '', t.get('shape_id') or '')
            dienst[t['trip_id']] = t.get('service_id', '')
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

    HALTE_ALLE.extend(halte.values())

    # 2.5 (Fassung 52): Abfahrten je Haltestelle an einem gewöhnlichen Werktag (nächster Mittwoch) – Anzahl, erste und letzte
    try:
        tagesfahrten(z, fahrten, dienst, linien, region)
    except Exception as e:    # freiwillig – die Liniennetze hängen nicht davon ab
        log('Abfahrten je Haltestelle nicht ermittelt:', e)
    del dienst

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
        # Version 2.1: „Linie 6“/„Bus 6“ als Nummer → „6“; nur Einzellinien (keine „31, 32“, „501/52“); keine Sonderkennungen ohne Fahrgastbetrieb
        ref = re.sub(r'^(?:Stadt)?(?:Bus|Buslinie|Linie)\s+', '', ref, flags=re.I).strip()
        if not ref or keine_linie(ref):
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
        try:
            rt = int(r.get('route_type', 3))
        except ValueError:
            rt = 3
        # Version 2.2: keine Züge (RB, RE, S-Bahn: 2, 100–199), keine Taxis und Anruf-Linien-Taxis (1500–1599, ALT/AST im Namen)
        if rt == 2 or 100 <= rt < 200 or 1500 <= rt < 1600 or keine_linie(ref, '', '', ' '.join([r.get('route_long_name', ''), r.get('route_desc', ''), r.get('route_short_name', '')]), art):
            continue
        # 2.8: keine Fernbusse (route_type 200–209, FlixBus u. a.) – „N66: Budapest Airport => Hamburg“ stand im VRB-Netz
        if 200 <= rt < 210 or re.search(r'flix|blablabus|eurolines', op, re.I):
            continue
        for v in verbuende:
            im_verbund = sum(zaehler[k] for k in v['kreise'])
            anteil = im_verbund / summe if summe else 0
            eigener_betreiber = im_verbund >= 1 and any(b.lower() in op.lower() for b in v.get('betreiber', []) if b)
            gelistet = im_verbund >= 1 and ref_norm(ref) in LISTEN.get(v['slug'], {})     # 2.3: in der offiziellen Linienliste
            # 2.6: … aber nur, wenn ihr Linienweg zur Linie passt (VSN „1“ Northeim – Hillerse ≠ „1“ Dingelstädt – Duderstadt aus Thüringen)
            if gelistet and anteil < 0.25 and not weg_passt(LISTEN[v['slug']][ref_norm(ref)][1], seqs):
                gelistet = False
                log(v['slug'], 'Linie', ref, '(%s) nicht übernommen – gleiche Nummer, anderer Linienweg' % op)
            if (anteil >= 0.25 and im_verbund >= 2) or eigener_betreiber or gelistet:
                von, nach = halte[haupt[0]][2], halte[haupt[-1]][2]
                muster_l = [[(x, y) for x, y, _ in q] for q in seqs]
                geom, verlauf = geometrie(muster_l, fo)
                ergebnis[v['slug']].append({
                    'type': 'Feature',
                    'properties': {
                        'ref': ref, 'name': '%s: %s => %s' % (ref, von, nach), 'from': von, 'to': nach,
                        'operator': op, 'network': v['kurz'], 'route': art,
                        'gtfs:route_id': rid, 'quelle': 'gtfs', 'verlauf': verlauf, 'muster': len(seqs), 'halte': len(haupt),
                        'luecken': sum(1 for x in fo if not x),    # 2.9: gerade Verbindungen am Ende der Geometrie
                        'description': (r.get('route_long_name') or '').strip(),
                        'weg_fp': weg_aus_folge([n for _, _, n in seqs[0]]),    # 2.8: Linienweg aus dem Fahrplan (häufigste Fahrt)
                    },
                    'geometry': geom,
                    '_halte': list(dict.fromkeys((x, y) for q in seqs for x, y, _ in q)),
                    '_namen': list(dict.fromkeys((n, round(x, 5), round(y, 5)) for q in seqs for x, y, n in q)),   # 2.4: Haltestellenseiten
                    '_folge': list(dict.fromkeys(n for _, _, n in seqs[0])),                                     # 2.7: Haltestellenfolge (häufigstes Muster)
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
    # 2.9: erst die Linienwege, dann die geraden Verbindungen – die stehen am Ende (Anzahl in properties.luecken, für skizzen())
    linien = [fo for fo in formen if fo] + [m for m, fo in zip(muster, formen) if not fo]
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
        p.update({'from': laengste['properties']['from'], 'to': laengste['properties']['to'], 'name': laengste['properties']['name'], 'weg_fp': laengste['properties'].get('weg_fp', ''),
                  'gtfs:route_id': ';'.join(dict.fromkeys(g['properties']['gtfs:route_id'] for g in gruppe)), 'muster': len(behalten), 'verlauf': verlauf,
                  'luecken': sum(1 for _, fo in paare if not fo)})
        out.append({'type': 'Feature', 'properties': p, 'geometry': geom,
                    '_halte': list(dict.fromkeys(q for m in behalten for q in m)), '_muster': behalten, '_formen': [fo for _, fo in paare],
                    # 2.7: Haltestellen aller Einträge behalten – bis 2.6 fehlten zusammengeführte Linien in haltestellen.json
                    '_namen': list(dict.fromkeys(x for g in gruppe for x in (g.get('_namen') or []))), '_folge': laengste.get('_folge') or []})
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


FERNBUS_RX = re.compile(r'flix|blablabus|eurolines|regiojet|student ?agency', re.I)


def fernbus(p):
    """2.9: Fernbus (FlixBus u. a.) – auch aus der Vorwoche nicht behalten („023 Dresden → Den Haag“ stand im NVV)."""
    return bool(FERNBUS_RX.search(str(p.get('operator', '') or '')))


def gleiche_nummer(feats, liste, v, verbuende, kreise, slug=''):
    """2.9: Gleiche Nummer mehrmals (NVV 100 Kassel – Calden und VUW 100 Bad Salzungen – Bad Hersfeld): Passt mindestens ein
    Eintrag zum Linienweg der offiziellen Liste, fallen die übrigen weg, wenn sie überwiegend (über 25 %) außerhalb des Verbunds
    fahren oder ihr Betreiber einen anderen Verbund nennt („NVV - Faisy“ im VSN). Kasseler Tram 4 neben StadtBus Eschwege 4 bleibt.
    Verglichen werden Haltestellennamen, Start und Ziel – nicht der angehängte Linienweg der Liste. Rückgabe: Zahl entfernter Einträge."""
    if not liste:
        return 0
    ids = [k for k in v['kreise'] if k in kreise]
    andere = [str(x.get('kurz', '')).lower() for x in verbuende if x.get('slug') != v['slug'] and x.get('kurz')]
    gruppen = {}
    for f in feats:
        gruppen.setdefault(ref_norm(f['properties'].get('ref', '')), []).append(f)
    raus = set()
    for r, fs in gruppen.items():
        if len(fs) < 2 or r not in liste or not weg_woerter(liste[r][1]):
            continue
        def namen(f):
            p = f['properties']
            return [[(0, 0, n) for n, _, _ in (f.get('_namen') or [])] + [(0, 0, str(p.get(k, '') or '')) for k in ('from', 'to', 'name', 'description')]]
        passt = [f for f in fs if weg_passt(liste[r][1], namen(f))]
        if not passt or len(passt) == len(fs):
            continue
        for f in fs:
            if any(f is x for x in passt):
                continue
            p = f['properties']
            op = str(p.get('operator', '') or '').lower()
            fremd_op = any(re.search(r'(^|[^a-z])' + re.escape(re.sub(r'[^a-z0-9]', '', k)) + r'([^a-z]|$)', op) for k in andere if re.sub(r'[^a-z0-9]', '', k))
            pts = list(koordinaten(f.get('geometry')))
            pts = pts[::max(1, len(pts) // 60)]
            drin = (sum(1 for x, y in pts if kreis_von(x, y, kreise, ids) is not None) / len(pts)) if pts and ids else 1.0
            if fremd_op or drin < 0.75:
                raus.add(id(f))
                log(slug, 'Linie', p.get('ref'), '(%s, %s → %s) nicht übernommen – gleiche Nummer, anderer Linienweg, %d %% im Verbund'
                    % (p.get('operator', ''), p.get('from', ''), p.get('to', ''), round(drin * 100)))
    if raus:
        feats[:] = [f for f in feats if id(f) not in raus]
    return len(raus)


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
OVERPASS_TOT = set()          # 2.4: Server, die in diesem Lauf nicht erreichbar waren, werden nicht erneut gefragt
OVERPASS_BUDGET = [12 * 60]   # 2.4: höchstens 12 Minuten Overpass je Lauf (sonst dauerte ein Lauf über 90 Minuten)


def overpass_holen(abfrage, url):
    """Fragt Overpass ab (nur, wenn kein Geofabrik-Auszug vorliegt). Version 2.4: ein Versuch je Server, Wartezeit 150 s,
    nicht erreichbare Server („Network is unreachable“, 502/504) werden für den Rest des Laufs übersprungen,
    insgesamt höchstens OVERPASS_BUDGET Sekunden – danach gelten die Verläufe der Vorwoche."""
    daten = urllib.parse.urlencode({'data': abfrage}).encode()
    for u in [url] + [u for u in OVERPASS_WEITERE if u != url]:
        if u in OVERPASS_TOT:
            continue
        if OVERPASS_BUDGET[0] <= 0:
            if 'budget' not in OVERPASS_TOT:
                log('Overpass: Zeitbudget aufgebraucht – weiter mit den Verläufen der Vorwoche')
                OVERPASS_TOT.add('budget')
            return None
        t0 = time.time()
        try:
            req = urllib.request.Request(u, data=daten, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=min(150, max(20, OVERPASS_BUDGET[0]))) as r:
                j = json.loads(r.read().decode('utf-8'))
            bem = str(j.get('remark') or '')
            if 'error' in bem.lower() or 'timed out' in bem.lower():
                raise RuntimeError('Antwort unvollständig: ' + bem[:120])
            OVERPASS_STATUS.append('ok: ' + u)
            return j
        except Exception as e:
            log('Overpass %s nicht nutzbar (%s) – in diesem Lauf nicht mehr gefragt' % (u, e))
            OVERPASS_STATUS.append('Fehler: %s (%s)' % (u, str(e)[:100]))
            OVERPASS_TOT.add(u)
        finally:
            OVERPASS_BUDGET[0] -= time.time() - t0
    return None


# ---------------------------------------------------------------------------
# Version 2.4: OpenStreetMap aus dem Geofabrik-Auszug (OPL, erzeugt mit osmium) statt Overpass
# ---------------------------------------------------------------------------
ROUTE_OSM = ('bus', 'trolleybus', 'minibus', 'share_taxi', 'tram')


def opl_text(s):
    """OPL-Maskierung auflösen: „%20%“ → Leerzeichen usw."""
    return re.sub(r'%([0-9a-fA-F]+)%', lambda m: chr(int(m.group(1), 16)), s) if '%' in s else s


def opl_tags(s):
    out = {}
    for kv in s.split(',') if s else []:
        k, _, w = kv.partition('=')
        if k:
            out[opl_text(k)] = opl_text(w)
    return out


def osm_auszug_laden(pfad):
    """OPL-Datei (osmium cat -f opl,add_metadata=false) → {'knoten': {id: (lon, lat)}, 'wege': {id: [Knoten]}, 'rel': [...]}"""
    knoten, wege, rel = {}, {}, []
    with open(pfad, encoding='utf-8') as fh:
        for z in fh:
            teile = z.rstrip('\n').split(' ')
            if not teile[0]:
                continue
            art, oid = teile[0][0], int(teile[0][1:])
            f = {t[0]: t[1:] for t in teile[1:] if t}
            if art == 'n':
                if f.get('x') and f.get('y'):
                    knoten[oid] = (float(f['x']), float(f['y']))
            elif art == 'w':
                wege[oid] = [int(n[1:]) for n in f.get('N', '').split(',') if len(n) > 1]
            elif art == 'r':
                t = opl_tags(f.get('T', ''))
                if t.get('type') != 'route' or t.get('route') not in ROUTE_OSM:
                    continue
                mem = []
                for m in f.get('M', '').split(','):
                    if len(m) > 1:
                        ref, _, rolle = m[1:].partition('@')
                        mem.append((m[0], int(ref), opl_text(rolle)))
                rel.append({'id': oid, 'tags': t, 'members': mem})
    log('OpenStreetMap-Auszug: %d Linienrouten, %d Wege, %d Punkte' % (len(rel), len(wege), len(knoten)))
    return {'knoten': knoten, 'wege': wege, 'rel': rel}


def ref_regex(refs):
    """wie ref_muster(), aber als Python-Ausdruck"""
    werte = [' *'.join(re.escape(c) for c in r) for r in sorted({ref_norm(x) for x in refs if str(x).strip()})]
    return re.compile(r'(^|;) *((bus|linie) *)?(%s) *(;|$)' % '|'.join(werte), re.I) if werte else None


def auszug_antwort(az, v, kreise, refs=None, nur_tags=False):
    """Liefert dasselbe wie die Overpass-Abfragen: Routen im Gebiet des Verbunds (mindestens ein Wegpunkt in einem seiner
    Landkreise), auf Wunsch nur mit passender Nummer; mit Geometrie der Wege (wie „out geom“) oder nur Merkmale."""
    ids = [k for k in v['kreise'] if k in kreise]
    if not az or not ids:
        return None
    muster = ref_regex(refs) if refs is not None else None
    if refs is not None and muster is None:
        return {'elements': []}
    zelle = {}

    def im_gebiet(p):
        k = (round(p[0], 2), round(p[1], 2))
        if k not in zelle:
            zelle[k] = kreis_von(p[0], p[1], kreise, ids) is not None
        return zelle[k]
    els = []
    for r in az['rel']:
        t = r['tags']
        if muster is not None and not muster.search(t.get('ref') or ''):
            continue
        glieder, drin = [], False
        for typ, ref, rolle in r['members']:
            if typ != 'w':
                continue
            pts = [az['knoten'][n] for n in az['wege'].get(ref, ()) if n in az['knoten']]
            if not pts:
                continue
            if not drin:
                drin = any(im_gebiet(p) for p in pts[::max(1, len(pts) // 4)] + [pts[-1]])
            if not nur_tags:
                glieder.append({'type': 'way', 'ref': ref, 'role': rolle, 'geometry': [{'lon': x, 'lat': y} for x, y in pts]})
        if drin:
            els.append({'type': 'relation', 'id': r['id'], 'tags': t, **({} if nur_tags else {'members': glieder})})
    return {'elements': els}


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
        f['properties']['luecken'] = len(luecken)    # 2.9: Lücken stehen am Ende der Geometrie
        f['properties']['@id'] = ';'.join('relation/%d' % k['id'] for k in gewaehlt[:6])
        n += 1
    return n


ZUG_RX = re.compile(r'^(?:RB|RE|RS|S|IRE|ME|MEX|ERB|HLB|NWB|STB|IC|ICE|EC|EN|FLX|TER)\s?\d{0,4}[a-z]?$', re.I)   # 2.3: „R15“ ist ein Rufbus, kein Zug
# 2.8 (Fassung 56): „ALT“/„AST“ nur in Großbuchstaben – mit re.I fielen Linien nach „Alt-Grone“ oder „Alt Wallmoden“ heraus
ALT_RX = re.compile(r'\b(?:ALT|AST)\b|\b(?:[Aa]nruf-?[Ll]inien-?[Tt]axi|AnrufLinienTaxi|Anruflinientaxi|[Aa]nruf-?[Ss]ammel-?[Tt]axi|Anrufsammeltaxi|Linientaxi|[Aa]nruf-?[Bb]us)\b')


def keine_linie(ref, von='', nach='', name='', art=''):
    """Version 2.2: Linien, die nicht ins Liniennetz gehören – zusammengesetzte Nummern („31, 32“, „501/52“, „31+32“),
    Sonderkennungen ohne Fahrgastbetrieb („E“, „E80“, „F“, „S“), Züge (RB, RE, S-Bahn …), Anruf-Linien-Taxis
    (ALT, AST) und „Bahnhof A → Bahnhof A“."""
    ref = str(ref or '').strip()
    if ref and ''.join(ref.split()).lower() in EIN:     # 2.3: ausdrücklich aufgenommen (Vorgabe ROSA 5/43, Regel „ROSA: 5/43 = +“)
        return str(art or '').lower() in ('train', 'subway', 'rail', 'monorail', 'funicular', 'aerialway', 'taxi')
    if not ref or re.search(r'[,;/+&]|\s(?:und|u\.)\s', ref, re.I):
        return True
    if re.match(r'^(?:E|F|S)$', ref, re.I) or re.match(r'^E\d{1,3}$', ref, re.I):
        return True
    if ZUG_RX.match(ref) or str(art or '').lower() in ('train', 'subway', 'rail', 'monorail', 'funicular', 'aerialway', 'taxi'):
        return True
    if re.match(r'^(?:ALT|AST|AL)\s?\d*[a-z]?$', ref, re.I) or ALT_RX.search(str(name or '')):
        return True
    a, b = str(von or '').strip().lower(), str(nach or '').strip().lower()
    return bool(a and a == b and re.match(r'^(?:bahnhof\s?[a-z]?|betriebshof.*|depot.*)$', a))


def ersetzt_durch(f, feats, nah=300, anteil=0.7):
    """Version 2.1: Fährt eine aktuelle Linie desselben Betreibers fast denselben Weg (70 % der Punkte näher als 300 m)?
    Dann ist die alte Nummer ersetzt (z. B. VSN 265 → 260) und wird nicht aus der Vorwoche behalten. Rückgabe: neue Nummer oder ''."""
    g = f.get('geometry') or {}
    teile = g.get('coordinates', []) if g.get('type') == 'MultiLineString' else ([g.get('coordinates', [])] if g.get('type') == 'LineString' else [])
    punkte = stichprobe([tuple(p) for t in teile for p in t], 60)
    if len(punkte) < 4:
        return ''
    op = str(f.get('properties', {}).get('operator', '')).strip().lower()
    for neu in feats:
        pn = neu['properties']
        if op and str(pn.get('operator', '')).strip().lower() not in ('', op):
            continue
        gn = neu.get('geometry') or {}
        tn = gn.get('coordinates', []) if gn.get('type') == 'MultiLineString' else ([gn.get('coordinates', [])] if gn.get('type') == 'LineString' else [])
        if not tn:
            continue
        r = Raster([[tuple(p) for p in t] for t in tn])
        if sum(1 for p in punkte if r.abstand(p) <= nah) / len(punkte) >= anteil:
            return str(pn.get('ref', ''))
    return ''


ERSETZT = {}     # slug → {alte Nummer: neue Nummer} (für den Bericht)


def gedaechtnis(feats, alt_pfad, heute, tage=28, slug='', fremd=None):
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
        if schl(p) in vorhanden or gesehen < grenze or not f.get('geometry') or fernbus(p) or keine_linie(p.get('ref', ''), p.get('from'), p.get('to'), p.get('name', ''), p.get('route', '')):
            continue
        if fremd and fremd(f):    # 2.6: früher fälschlich übernommen (gleiche Nummer, anderer Linienweg) – nicht behalten
            log(slug, 'Linie', p.get('ref'), '(%s) aus der Vorwoche nicht behalten – gleiche Nummer, anderer Linienweg' % p.get('operator', ''))
            continue
        neu = ersetzt_durch(f, [x for x in feats if not x['properties'].get('aus_vorwoche')])
        if neu:
            ERSETZT.setdefault(slug, {})[str(p.get('ref', ''))] = neu
            log(slug, 'Linie', p.get('ref'), 'nicht behalten – ersetzt durch', neu)
            continue
        p['gesehen'] = gesehen
        p['aus_vorwoche'] = True
        feats.append(f)
        vorhanden.add(schl(p))
        n += 1
    return n


# ---------------------------------------------------------------------------
# Version 2.2: Regeln der Website (ersetzte Linien) und fehlende Linienwege („von → nach“)
# ---------------------------------------------------------------------------
def regeln_laden(quelle):
    """Regeln aus der Website (_data/verbund-tarife/linien-regeln.json) oder einer Datei.
    {"ersetzt": {"vsn": {"265": {"neu": "260", "von": "Northeim", "nach": "Bad Gandersheim"}}, "*": {…}}, "aus": {"vsn": ["…"]}}"""
    if not quelle:
        return {}
    quelle = re.sub(r'(?<!:)//+', '/', re.sub(r'/_admin/+(?=_data/)', '/', quelle.strip()))   # Fassung 50: GOEBUS_ADRESSE mit „/_admin/“
    try:
        if re.match(r'^https?://', quelle):
            req = urllib.request.Request(quelle, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode('utf-8'))
        else:
            d = json.load(open(quelle, encoding='utf-8'))
        log('Regeln der Website geladen:', sum(len(x) for x in (d.get('ersetzt') or {}).values()), 'ersetzte Linien')
        return d if isinstance(d, dict) else {}
    except Exception as e:      # Regeln sind freiwillig – ohne sie läuft alles wie bisher
        log('Regeln nicht geladen:', quelle, '–', e)
        return {}


def archiv_laden(pfad):
    try:
        return json.load(open(pfad, encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def regeln_anwenden(feats, regeln, slug, alt_pfad, archiv):
    """Ersetzte Linien: alte Nummer entfällt, ihr Verlauf geht auf die neue über, wenn die neue keinen eigenen
    (Fahrplan oder OpenStreetMap) hat. Verläufe ersetzter Linien bleiben im Archiv (daten/ersetzt-archiv.json) dauerhaft erhalten.
    Rückgabe: Anzahl übertragener Verläufe."""
    ers = dict((regeln.get('ersetzt') or {}).get('*') or {})
    ers.update((regeln.get('ersetzt') or {}).get(slug) or {})
    aus = {ref_norm(x) for x in ((regeln.get('aus') or {}).get(slug) or [])}
    if aus:
        feats[:] = [f for f in feats if ref_norm(f['properties'].get('ref', '')) not in aus]
    if not ers:
        return 0
    alt = []
    if os.path.exists(alt_pfad):
        try:
            alt = json.load(open(alt_pfad, encoding='utf-8')).get('features', [])
        except (ValueError, OSError):
            alt = []
    n = 0
    for alte, r in ers.items():
        neu_ref = str((r or {}).get('neu', '')).strip()
        if not neu_ref:
            continue
        ka = ref_norm(alte)
        quelle = None
        for f in feats + alt:
            if ref_norm(f['properties'].get('ref', '')) == ka and f.get('geometry'):
                quelle = f
                break
        schl = slug + '|' + ka
        if quelle is not None:
            archiv[schl] = {'properties': {k: v for k, v in quelle['properties'].items() if not k.startswith('_')}, 'geometry': quelle['geometry']}
        elif schl in archiv:
            quelle = archiv[schl]
        feats[:] = [f for f in feats if ref_norm(f['properties'].get('ref', '')) != ka]
        kn = ref_norm(neu_ref)
        ziel = next((f for f in feats if ref_norm(f['properties'].get('ref', '')) == kn), None)
        von, nach = str((r or {}).get('von', '')).strip(), str((r or {}).get('nach', '')).strip()
        stark = ziel is not None and ziel.get('geometry') and str(ziel['properties'].get('verlauf', '')).split('+')[0] in ('fahrplan', 'osm', 'vorwoche')
        if ziel is not None and stark:
            p = ziel['properties']
            if von and not p.get('from'):
                p['from'] = von
            if nach and not p.get('to'):
                p['to'] = nach
            continue
        if quelle is None:
            if ziel is not None and (von or nach):
                ziel['properties'].update({'from': von or ziel['properties'].get('from', ''), 'to': nach or ziel['properties'].get('to', '')})
            continue
        p = dict(ziel['properties'] if ziel is not None else quelle['properties'])
        p.update({'ref': neu_ref, 'ersetzt_von': str(alte), 'verlauf': 'uebernommen', 'aus_vorwoche': False})
        if von or nach:
            p['from'], p['to'] = von or p.get('from', ''), nach or p.get('to', '')
            p['name'] = '%s: %s => %s' % (neu_ref, p['from'], p['to'])
        else:
            p['name'] = '%s: %s => %s' % (neu_ref, p.get('from', ''), p.get('to', ''))
        f_neu = {'type': 'Feature', 'properties': p, 'geometry': quelle['geometry']}
        if ziel is not None:
            feats[feats.index(ziel)] = f_neu
        else:
            feats.append(f_neu)
        ERSETZT.setdefault(slug, {})[str(alte)] = neu_ref
        log(slug, 'Regel: Linie', alte, '→', neu_ref, '– Verlauf übertragen')
        n += 1
    return n


def wege_ergaenzen(feats, halte_alle, osm=None):
    """Fehlt „von“/„nach“, kommt der Linienweg (in dieser Reihenfolge) aus: Namen der Fahrplandaten („route_long_name“,
    z. B. „Hameln - Klein Berkel“), OpenStreetMap (from/to bzw. „Bus 6: A => B“) oder den nächsten Haltestellen an beiden Enden."""
    n = 0
    for f in feats:
        p = f['properties']
        if str(p.get('from', '')).strip() and str(p.get('to', '')).strip():
            continue
        von = nach = ''
        m = re.match(r'^\s*(.+?)\s*(?:<=>|<->|=>|->|→|–|—| - )\s*(.+?)\s*$', str(p.get('description', '')))
        if m:
            von, nach = m.group(1), m.group(2)
        if not (von and nach) and osm:
            for rel in osm.get(ref_norm(p.get('ref', '')), []):
                t = rel.get('tags', {})
                if t.get('from') and t.get('to'):
                    von, nach = t['from'], t['to']
                    break
                m = re.match(r'^[^:]*:\s*(.+?)\s*(?:=>|->|→|<=>)\s*(.+?)\s*$', str(t.get('name', '')))
                if m:
                    von, nach = m.group(1), m.group(2)
                    break
        if not (von and nach) and halte_alle and f.get('geometry'):
            g = f['geometry']
            teile = g.get('coordinates', []) if g.get('type') == 'MultiLineString' else ([g.get('coordinates', [])] if g.get('type') == 'LineString' else [])
            if teile and teile[0]:
                a, b = tuple(teile[0][0]), tuple(teile[-1][-1] if teile[-1] else teile[0][-1])
                von, nach = naechster_halt(a, halte_alle), naechster_halt(b, halte_alle)
        if von and nach:
            p['from'], p['to'] = von, nach
            if not str(p.get('name', '')).strip() or re.match(r'^(?:Linie|Bus)?\s*' + re.escape(str(p.get('ref', ''))) + r'$', str(p.get('name', '')).strip(), re.I):
                p['name'] = '%s: %s => %s' % (p.get('ref', ''), von, nach)
            n += 1
    return n


def listen_laden(regeln):
    """2.3: offizielle Linienlisten der Website ({"listen": {slug: {"linien": {Nummer: {"weg", "hinweis"}}}}})."""
    LISTEN.clear()
    for slug, l in ((regeln or {}).get('ein') or {}).items():
        for x in (l or []):
            EIN.add(''.join(str(x).split()).lower())
    for slug, l in ((regeln or {}).get('listen') or {}).items():
        d = {}
        for ref, x in ((l or {}).get('linien') or {}).items():
            if str(ref).strip():
                d[ref_norm(ref)] = (str(ref).strip(), str((x or {}).get('weg', '')), str((x or {}).get('hinweis', '')))
        if d:
            LISTEN[slug] = d
    if LISTEN:
        log('Offizielle Linienlisten:', ', '.join('%s %d' % (k, len(v)) for k, v in LISTEN.items()))


def halt_norm(s):
    s = str(s or '').lower().replace('ß', 'ss').replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')
    s = re.sub(r'\bhess\.?\s*', 'hessisch ', s)
    s = re.sub(r'\b(?:bf|bhf)\b\.?', 'bahnhof', s)
    s = re.sub(r'\bstr\b\.?', 'strasse', s)
    return ' '.join(re.findall(r'[a-z0-9]+', s))


WEG_ALLGEMEIN = {'bahnhof', 'bahnhofs', 'strasse', 'markt', 'rathaus', 'schule', 'schulen', 'mitte', 'kirche', 'platz', 'allee', 'ring',
                 'zentrum', 'busbahnhof', 'nord', 'sued', 'west', 'ober', 'unter', 'gross', 'klein', 'neue', 'neuer', 'alte', 'alter',
                 'linie', 'ueber'}


def koordinaten(g):
    """2.6: alle Punkte (lon, lat) einer GeoJSON-Geometrie."""
    if not g:
        return []
    if g.get('type') == 'GeometryCollection':
        return [c for x in g.get('geometries') or [] for c in koordinaten(x)]
    def flach(a):
        if a and isinstance(a[0], (int, float)):
            return [(a[0], a[1])]
        return [c for x in (a or []) for c in flach(x)]
    return flach(g.get('coordinates') or [])


def weg_woerter(t):
    """2.6: Ortsnamen aus einem Linienweg – Wörter ab 4 Buchstaben ohne allgemeine wie „Bahnhof“, „Markt“, „Rathaus“."""
    return {w for w in halt_norm(t).split() if len(w) >= 4 and w.isalpha() and w not in WEG_ALLGEMEIN}


def weg_passt(weg, seqs):
    """2.6: Passt der amtliche Linienweg zu den Haltestellen der Linie? Ohne Ortsnamen im Linienweg: ja."""
    w = weg_woerter(weg)
    if not w:
        return True
    namen = set()
    for q in seqs:
        for _, _, n in q:
            namen |= set(halt_norm(n).split())
    return bool(w & namen)


def _m(a, b):
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110540)


def halte_aus_weg(weg, index):
    """2.3: Linienweg „Hameln – Groß Berkel – Aerzen / Reinerbeck“ → Koordinaten der passenden Haltestellen (lon, lat, Name).
    Erst eindeutige Namen, dann die übrigen nahe an den Nachbarn (z. B. „Bahnhof“ bei Hameln)."""
    teile = [t.strip() for t in re.split(r'\s+[–—-]\s+', str(weg or '')) if t.strip()]
    namen = [halt_norm(re.split(r'\s*/\s*|\(', t)[0]) for t in teile]
    kand = []
    for n in namen:
        if not n:
            kand.append([])
            continue
        k = [h for h in index if h[3] == n or h[3].endswith(' ' + n) or h[3].startswith(n + ' ')]
        if not k:
            k = [h for h in index if (' ' + n + ' ') in (' ' + h[3] + ' ')]
        kand.append(k)
    wahl = [None] * len(namen)
    zentral = re.compile(r'bahnhof|zob|markt|mitte|zentrum|rathaus|kirche|post')
    for i, k in enumerate(kand):
        orte = {h[3].split(' ')[0] for h in k}
        if k and len(orte) <= 2:
            wahl[i] = min(k, key=lambda h: (0 if h[3] == namen[i] or h[3].endswith(' ' + namen[i]) else 1, 0 if zentral.search(h[3]) else 1, len(h[3])))
    for _ in range(2):
        for i, k in enumerate(kand):
            if wahl[i] or not k:
                continue
            nb = [wahl[j] for j in (i - 1, i + 1) if 0 <= j < len(wahl) and wahl[j]]
            if nb:
                wahl[i] = min(k, key=lambda h: min(_m(h, x) for x in nb))
    pts = [w for w in wahl if w]
    out = []
    for w in pts:
        if not out or _m(out[-1], w) > 30:
            out.append(w)
    return out


def liste_anwenden(feats, liste, index, osm_idx, v):
    """2.3: amtlichen Linienweg und Hinweis an vorhandene Linien hängen; gelistete Linien, die in den Fahrplandaten fehlen,
    aus OpenStreetMap oder der Verbindung ihrer Haltestellen ergänzen (quelle „liste“). Rückgabe (ergänzte Wege, neue Linien)."""
    if not liste:
        return 0, 0
    da = {ref_norm(f['properties'].get('ref', '')): f for f in feats}
    weg_n = neu = 0
    for k, (ref, weg, hinweis) in liste.items():
        f = da.get(k)
        if f:
            p = f['properties']
            if weg:
                p['weg'] = weg
                weg_n += 1
            if hinweis:
                p['hinweis'] = hinweis
            continue
        if keine_linie(ref):
            continue
        halte = halte_aus_weg(weg, index) if weg else []
        geom, verlauf = None, ''
        rels = (osm_idx or {}).get(k, [])
        if rels:
            ok = []
            for r in rels:
                ra = Raster(r['linien'])
                if halte and sum(1 for h in halte if ra.abstand((h[0], h[1])) <= 400) / len(halte) >= 0.6:
                    ok.append(r)
            if ok:
                geom = {'type': 'MultiLineString', 'coordinates': [[[round(x, 6), round(y, 6)] for x, y in vereinfachen(l)] for r in ok for l in r['linien']]}
                verlauf = 'osm'
        if not geom and len(halte) >= 2:
            geom = {'type': 'LineString', 'coordinates': [[round(h[0], 6), round(h[1], 6)] for h in halte]}
            verlauf = 'haltestellen'
        if not geom:
            continue
        teile = [t.strip() for t in re.split(r'\s+[–—-]\s+', weg) if t.strip()]
        feats.append({'type': 'Feature', 'properties': {
            'ref': ref, 'name': '%s: %s => %s' % (ref, teile[0], teile[-1]) if len(teile) > 1 else 'Linie ' + ref,
            'from': teile[0] if teile else '', 'to': teile[-1] if len(teile) > 1 else '', 'operator': '', 'network': v.get('kurz', ''), 'route': 'bus',
            'quelle': 'liste', 'verlauf': verlauf, 'halte': len(halte), 'weg': weg, 'hinweis': hinweis, 'description': weg}, 'geometry': geom})
        neu += 1
    if weg_n or neu:
        log(v['slug'], 'Linienliste:', weg_n, 'Linienwege übernommen,', neu, 'fehlende Linien ergänzt')
    return weg_n, neu


def naechster_halt(pt, halte_alle, max_m=400):
    """Name der nächsten Haltestelle (höchstens 400 m) zu einem Punkt (lon, lat)."""
    bester, d_best = '', max_m
    cx = math.cos(math.radians(pt[1]))
    for x, y, name in halte_alle:
        if abs(x - pt[0]) > 0.01 or abs(y - pt[1]) > 0.006 or not name:
            continue
        d = math.hypot((x - pt[0]) * cx, y - pt[1]) * 111320
        if d < d_best:
            bester, d_best = name, d
    return bester


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
        p['luecken'] = a['properties'].get('luecken', 0)
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
    ap.add_argument('--behalten-tage', type=int, default=28, help='fehlende Linien so viele Tage aus der Vorwoche behalten (0 = aus); ersetzte Linien nie')
    ap.add_argument('--osm-auszug', default=os.path.join(HIER, 'osm', 'auszug.opl'), help='OpenStreetMap-Auszug (OPL), Vorgabe osm/auszug.opl; fehlt er, wird Overpass gefragt')
    ap.add_argument('--osm-json', help='vorbereitete Overpass-Antworten statt Abfrage, z. B. osm/{slug}.json (für Tests)')
    ap.add_argument('--regeln', default=os.environ.get('GOEBUS_REGELN', ''), help='Regeln der Website (ersetzte Linien), Adresse oder Datei; Vorgabe: Umgebungsvariable GOEBUS_REGELN')
    a = ap.parse_args()

    T0 = time.time()
    dauer = {}    # 2.4 (Fassung 48): Laufzeit je Abschnitt in Sekunden – die Verwaltung warnt bei langen Läufen
    kreise = landkreise(os.path.join(HIER, 'landkreise.topo.json'))
    verbuende = json.load(open(a.verbuende, encoding='utf-8'))
    pfad = a.gtfs
    if not pfad:
        pfad = os.path.join(HIER, 'gtfs.zip')
        herunterladen(a.gtfs_url, pfad)
    dauer['fahrplandaten_laden'] = round(time.time() - T0)
    regeln = regeln_laden(a.regeln)
    listen_laden(regeln)
    t = time.time()
    ergebnis = gtfs_auswerten(pfad, kreise, verbuende)
    dauer['fahrplandaten_auswerten'] = round(time.time() - t)
    t = time.time()
    auszug = None
    if not a.ohne_osm and not a.osm_json and a.osm_auszug and os.path.exists(a.osm_auszug) and os.path.getsize(a.osm_auszug) > 1000:
        try:
            auszug = osm_auszug_laden(a.osm_auszug)
        except Exception as e:    # beschädigter Auszug → Overpass wie bisher
            log('OpenStreetMap-Auszug nicht lesbar (%s) – frage Overpass' % e)
    elif not a.ohne_osm and not a.osm_json:
        log('Kein OpenStreetMap-Auszug (%s) – frage Overpass (höchstens %d Minuten)' % (a.osm_auszug, OVERPASS_BUDGET[0] // 60))
    dauer['osm_auszug_lesen'] = round(time.time() - t)
    dauer['verbuende'] = {}

    os.makedirs(a.ausgabe, exist_ok=True)
    archiv_pfad = os.path.join(a.ausgabe, 'ersetzt-archiv.json')
    archiv = archiv_laden(archiv_pfad)
    stand = {'erzeugt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'version': VERSION, 'linienwege': bool(FORMEN_IM_FEED and FORMEN_IM_FEED[0]), 'verbuende': {}}
    bericht = {'erzeugt': stand['erzeugt'], 'version': VERSION, 'hinweis': 'Übersicht je Verbund: Linien, Betreiber, Verlauf (fahrplan = Linienweg aus den Fahrplandaten, osm = straßengenau aus OpenStreetMap, …+haltestellen = teilweise, haltestellen = Verbindung der Haltestellen).', 'verbuende': {}}
    for i, v in enumerate(verbuende):
        tv = time.time()
        feats = ergebnis.get(v['slug'], [])
        osm_n = 0
        osm_idx = None
        osm_status = 'aus' if a.ohne_osm else 'nicht nötig'
        if feats and not a.ohne_osm:
            if a.osm_json:
                p = a.osm_json.replace('{slug}', v['slug'])
                antwort = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None
            elif auszug:
                antwort = auszug_antwort(auszug, v, kreise, {f['properties']['ref'] for f in feats} | {x[0] for x in LISTEN.get(v['slug'], {}).values()})
            else:
                if i and not OVERPASS_TOT:
                    time.sleep(5)    # Overpass schonen
                antwort = overpass_holen(overpass_abfrage(v, {f['properties']['ref'] for f in feats} | {x[0] for x in LISTEN.get(v['slug'], {}).values()}), a.overpass_url)
            osm_status = ('ok (Auszug)' if auszug else 'ok') if antwort is not None else 'nicht erreichbar'
            osm_idx = osm_linien(antwort)
            osm_n = verlauf_aus_osm(feats, osm_idx)
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
            elif feats and auszug:
                osm_alle = osm_refs_im_netz(auszug_antwort(auszug, v, kreise, nur_tags=True), v)
            elif feats and not OVERPASS_TOT:
                osm_alle = osm_refs_im_netz(overpass_holen(overpass_alle_abfrage(v), a.overpass_url), v)
        quelle = quellen_refs(v) if v.get('quellen') else (set(), [])
        feats[:] = [f for f in feats if not fernbus(f['properties'])]                                   # 2.9
        gleich_n = gleiche_nummer(feats, LISTEN.get(v['slug'], {}), v, verbuende, kreise, v['slug'])     # 2.9: vor Haltestellen und Folgen
        for f in feats:    # 2.4: Haltestellen mit ihren Linien sammeln (vor dem Aufräumen)
            for n, x, y in f.get('_namen') or []:
                h = HALTESTELLEN.setdefault(n, {'p': [], 'l': set()})
                if len(h['p']) < 12 and (x, y) not in h['p']:
                    h['p'].append((x, y))
                h['l'].add((f['properties']['ref'], v['kurz']))
        for f in feats:    # 2.7: Haltestellenfolge je Linie (für die Linienseiten der Website)
            fl = f.get('_folge') or []
            if len(fl) >= 2 and len(fl) > len(FOLGEN.setdefault(v['kurz'], {}).get(f['properties']['ref'], [])):
                FOLGEN[v['kurz']][f['properties']['ref']] = fl
        for f in feats:
            f.pop('_folge', None)
            f.pop('_namen', None)
            f.pop('_halte', None)
            f.pop('_muster', None)
            f.pop('_formen', None)
            f['properties']['gesehen'] = heute
        ziel = os.path.join(a.ausgabe, v['datei'])
        def fremd(f, v=v):    # 2.6: gelistete Nummer, Verlauf überwiegend außerhalb und Linienweg passt nicht zu Start/Ziel
            p = f.get('properties', {})
            eintrag = LISTEN.get(v['slug'], {}).get(ref_norm(p.get('ref', '')))
            if not eintrag or any(b.lower() in str(p.get('operator', '')).lower() for b in v.get('betreiber', []) if b):
                return False
            pts = [c for c in koordinaten(f.get('geometry'))]
            pts = pts[::max(1, len(pts) // 60)]
            if not pts:
                return False
            drin = sum(1 for x, y in pts if kreis_von(x, y, kreise, [k for k in v['kreise'] if k in kreise]) is not None) / len(pts)
            return drin < 0.25 and not weg_passt(eintrag[1], [[(0, 0, p.get('from', '')), (0, 0, p.get('to', '')), (0, 0, p.get('name', ''))]])
        alt_n = gedaechtnis(feats, ziel, heute, a.behalten_tage, v['slug'], fremd) if a.behalten_tage > 0 else 0
        gleich_n += gleiche_nummer(feats, LISTEN.get(v['slug'], {}), v, verbuende, kreise, v['slug'])    # 2.9: auch Einträge aus der Vorwoche
        # Version 2.2: Regeln der Website (ersetzte Linien, z. B. VSN 265 → 260) und fehlende Linienwege ergänzen
        reg_n = regeln_anwenden(feats, regeln, v['slug'], ziel, archiv) if regeln else 0
        weg_n = wege_ergaenzen(feats, HALTE_ALLE, osm_idx)
        li_weg, li_neu = liste_anwenden(feats, LISTEN.get(v['slug'], {}), HALTE_REGION, osm_idx, v)    # 2.3
        feats[:] = [f for f in feats if not keine_linie(f['properties'].get('ref', ''), f['properties'].get('from'), f['properties'].get('to'), f['properties'].get('name', ''), f['properties'].get('route', ''))]
        if reg_n or weg_n:
            log(v['slug'], reg_n, 'Verläufe nach Regeln übertragen,', weg_n, 'Linienwege („von → nach“) ergänzt')
        # 2.8: gerade Verbindungen der Haltestellen über die Straßen skizzieren (Zwischenspeicher, Zeitbudget je Lauf)
        if SKIZZE['an']:
            if SKIZZE['cache'] is None:
                try:
                    with open(os.path.join(a.ausgabe, 'skizzen.json'), encoding='utf-8') as fh:
                        SKIZZE['cache'] = json.load(fh)
                except (OSError, ValueError):
                    SKIZZE['cache'] = {}
                SKIZZE['ende'] = time.time() + 60 * float(os.environ.get('GOEBUS_SKIZZE_MINUTEN', '20'))
            verworfen = {str(x).strip() for x in ((regeln or {}).get('skizze_aus') or {}).get(v['slug'], [])}    # Fassung 56: in der Verwaltung verworfen
            sk_n = skizzen([f for f in feats if f['properties'].get('ref', '').strip() not in verworfen], SKIZZE['cache'], SKIZZE['ende'])
            if sk_n:
                log(v['slug'], sk_n, 'Linienverläufe über die Straßen skizziert')
        if not feats:
            log(v['slug'], 'keine Linien – Datei wird nicht geschrieben')
            continue
        geo = {'type': 'FeatureCollection', 'generator': 'GöBUS liniennetz.py', 'erzeugt': stand['erzeugt'],
               'attribution': 'Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0); Linienverläufe: OpenStreetMap-Mitwirkende (ODbL)',
               'features': sorted(feats, key=lambda f: (len(f['properties']['ref']), f['properties']['ref']))}
        with open(ziel + '.neu', 'w', encoding='utf-8') as fh:
            json.dump(geo, fh, ensure_ascii=False, separators=(',', ':'))
        os.replace(ziel + '.neu', ziel)
        dauer['verbuende'][v['slug']] = round(time.time() - tv)
        stand['verbuende'][v['slug']] = {'datei': v['datei'], 'linien': len(feats), 'liste_wege': li_weg, 'liste_neu': li_neu, 'osm_verlauf': osm_n, 'fahrplan_verlauf': fp_n, 'verlauf_vorwoche': vw_n, 'aus_vorwoche': alt_n, 'gleiche_nummer_entfernt': gleich_n, 'osm': osm_status}
        bericht['verbuende'][v['slug']] = bericht_verbund(v, feats)
        if ERSETZT.get(v['slug']):
            bericht['verbuende'][v['slug']]['ersetzt'] = ERSETZT[v['slug']]
        bericht['verbuende'][v['slug']]['abgleich'] = abgleich_verbund(v, feats, alt_refs, osm_alle, quelle)
        bericht['verbuende'][v['slug']]['pruefung'] = pruefung_verbund(feats)
        bericht['verbuende'][v['slug']]['osm'] = osm_status
        bericht['verbuende'][v['slug']]['verlauf_vorwoche'] = vw_n
        log(v['slug'], len(feats), 'Linien gespeichert:', fp_n, 'mit Linienweg aus den Fahrplandaten,', osm_n, 'aus OpenStreetMap (%s),' % osm_status, vw_n, 'Verläufe aus der Vorwoche,', alt_n, 'Linien aus den Vorwochen →', v['datei'])
    if archiv:
        with open(archiv_pfad, 'w', encoding='utf-8') as fh:
            json.dump(archiv, fh, ensure_ascii=False, separators=(',', ':'))
    stand['regeln'] = bool(regeln)
    dauer['gesamt'] = round(time.time() - T0)
    stand['dauer'] = dauer
    stand['osm_quelle'] = 'geofabrik' if auszug else ('overpass' if not a.ohne_osm and not a.osm_json else 'aus')
    stand['overpass'] = (['Geofabrik-Auszug (ohne Overpass)'] if auszug else []) + OVERPASS_STATUS[-12:]
    bericht['overpass'] = stand['overpass']
    # 2.4: Haltestellen der Region mit ihren Linien (für die Haltestellenseiten der Website)
    if HALTESTELLEN:    # Fassung 49: ein Fehler hier darf die Liniennetze nicht kosten
        try:
            hl = []
            # 2.5: Abfahrten am Stichtag je Name (alle Steige zusammen): Anzahl, erste, letzte, je Linie
            zt, jl = defaultdict(list), defaultdict(Counter)
            lz, st = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(set))    # 2.7: Zeiten je Linie, Linien je Steig
            for sid, n in (TAGESFAHRTEN.get('name') or {}).items():
                if n in HALTESTELLEN and sid in (TAGESFAHRTEN.get('zeiten') or {}):
                    zt[n].extend(TAGESFAHRTEN['zeiten'][sid])
                    jl[n].update(TAGESFAHRTEN['je'][sid])
                    for r, ms in (TAGESFAHRTEN.get('linien') or {}).get(sid, {}).items():
                        lz[n][r].extend(ms)
                    code = (TAGESFAHRTEN.get('steig') or {}).get(sid)
                    if code:
                        st[n][code].update(r for r, c in TAGESFAHRTEN['je'][sid].items() if c > 0)
            uhr = lambda m: '%02d:%02d' % ((m // 60) % 24, m % 60)
            for n, h in sorted(HALTESTELLEN.items()):
                xs, ys = [p[0] for p in h['p']], [p[1] for p in h['p']]
                e = {'name': n, 'lon': round(sum(xs) / len(xs), 5), 'lat': round(sum(ys) / len(ys), 5),
                     'linien': [[r, k] for r, k in sorted(h['l'], key=lambda x: (x[1], len(x[0]), x[0]))]}
                if zt.get(n):
                    e['tag'] = [len(zt[n]), uhr(min(zt[n])), uhr(max(zt[n])), {r: c for r, c in sorted(jl[n].items(), key=lambda x: (len(x[0]), x[0])) if c > 0},
                                {r: linie_zeiten(ms, uhr) for r, ms in sorted(lz[n].items(), key=lambda x: (len(x[0]), x[0])) if ms},   # 2.7: [erste, letzte, Takt]
                                sum(1 for m in zt[n] if m % 1440 < 300)]                                                               # 2.7: Abfahrten 0–5 Uhr
                if len(st.get(n, {})) >= 2:    # 2.7: Steigplan, wenn die Haltestelle mehrere Steige mit Bezeichnung hat
                    e['steige'] = {c: sorted(rs, key=lambda x: (len(x), x)) for c, rs in sorted(st[n].items(), key=lambda x: (len(x[0]), x[0])) if rs}
                hl.append(e)
            with open(os.path.join(a.ausgabe, 'haltestellen.json'), 'w', encoding='utf-8') as fh:
                json.dump({'erzeugt': stand['erzeugt'], 'version': VERSION, 'attribution': 'Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0)',
                           'stichtag': TAGESFAHRTEN.get('stichtag', ''), 'netze': {v['kurz']: v['slug'] for v in verbuende}, 'haltestellen': hl}, fh, ensure_ascii=False, separators=(',', ':'))
            stand['haltestellen'] = len(hl)
            log(len(hl), 'Haltestellen → haltestellen.json')
            if SKIZZE['cache']:    # 2.8: Zwischenspeicher der Skizzen (nur noch genutzte Einträge wären schöner – Größe bleibt klein)
                with open(os.path.join(a.ausgabe, 'skizzen.json'), 'w', encoding='utf-8') as fh:
                    json.dump(SKIZZE['cache'], fh, ensure_ascii=False, separators=(',', ':'))
            if FOLGEN:    # 2.7: Haltestellenfolge je Linie
                with open(os.path.join(a.ausgabe, 'linienfolgen.json'), 'w', encoding='utf-8') as fh:
                    json.dump({'erzeugt': stand['erzeugt'], 'version': VERSION, 'attribution': 'Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0)', 'netze': FOLGEN},
                              fh, ensure_ascii=False, separators=(',', ':'))
                log(sum(len(x) for x in FOLGEN.values()), 'Linien → linienfolgen.json')
        except Exception as e:
            log('haltestellen.json nicht geschrieben:', e)
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
