#!/usr/bin/env python3
"""
GöBUS – Fahrschulen mit Busführerschein (Klasse D) in unserer Region
====================================================================

Läuft bei GitHub zusammen mit dem Liniennetz (siehe .github/workflows/liniennetz.yml) – oder auf jedem Rechner mit Python 3.9+.
Keine Zusatzpakete nötig. Version 1.1 (Fassung 44).

Warum hier und nicht auf der Website?
  Der Webserver (STRATO) erreicht die Overpass-Server von OpenStreetMap nicht („OpenStreetMap nicht erreichbar (Netz)“), und je Lauf
  kann er nur wenige Websites prüfen. GitHub erledigt beides in wenigen Minuten; die Website holt nur die fertige Datei ab
  (daten/fahrschulen.json, wie die Liniennetze).

Ablauf
  1. Alle Fahrschulen im Gebiet aus OpenStreetMap (amenity=driving_school, Lizenz ODbL) – seit 1.1 auch Bildungsträger, die OpenStreetMap
     nicht als Fahrschule führt (Name mit Fahrschule, Fahrerschmiede, DEULA, Verkehrsschule, Berufskraftfahrer …), und die Liste ZUSATZ.
  2. Je Fahrschule mit Website: Startseite und bis zu vier passende Unterseiten (Bus, Klassen, Berufskraftfahrer, Leistungen) lesen –
     nur, wenn robots.txt es erlaubt.
  3. Busführerschein belegt, wenn die Seiten Klasse D/D1/DE/D1E in einer Klassenliste („Klassen …“, „Fahrerlaubnisklassen …“) nennen,
     ausdrücklich den Bus-Führerschein anbieten oder den Personenbeförderungsschein (P-Schein); ersatzweise der OSM-Eintrag
     license_classes mit D. Seit 1.1 zählt „Berufskraftfahrer … Personenverkehr“ allein nicht mehr (so beschreibt sich auch ein
     Fahrlehrer, der nur Klasse B ausbildet).
  4. daten/fahrschulen.json schreiben: alle Fahrschulen mit Angaben, „bus“ (true/false) und dem Beleg (kurzer Textausschnitt).

Aufruf
  python fahrschulen.py --ausgabe daten
  python fahrschulen.py --gebiet 50.75,8.70,52.25,10.80   # Süd, West, Nord, Ost
"""
import argparse
import concurrent.futures
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser

VERSION = '1.1'
UA = 'GoeBUS-Fahrschulen/' + VERSION + ' (+https://www.goebus.de)'
GEBIET = '50.75,8.70,52.25,10.80'
OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter', 'https://overpass.private.coffee/api/interpreter']
UNTERSEITEN = re.compile(r'bus|lkw|berufskraft|bkf|klasse|führerschein|fuehrerschein|leistung|ausbildung|angebot|kontakt|impressum', re.I)
# Belege für die Bus-Ausbildung (nicht: „Bussmann“, „Kabus“, „Bushaltestelle vor der Tür“)
BELEG = [
    # Klasse groß geschrieben und allein stehend (nicht „m/w/d“, nicht „….de“)
    re.compile(r'(?i:klassen?|führerschein|fuehrerschein|fahrerlaubnis)\b[^.\n]{0,80}?(?<![/\w.-])(D1E|D1|DE|D)(?![\w/-]|\.\w)'),
    re.compile(r'(?i:klassen?|führerschein\w*|fuehrerschein\w*|fahrerlaubnis\w*)\b[^.\n]{0,80}?(?<![/\w.-])(D1E|D1|DE|D)(?![\w/-]|\.\w)'),
    re.compile(r'\b(?:omni)?bus[-\s]?(?:führerschein|fuehrerschein|fahrschule|ausbildung|fahrerausbildung)\b', re.I),
    re.compile(r'\b(?:omni)?bus-\s*(?:und|&|/|oder)\s*(?:lkw|lastwagen)-?(?:führerschein|fuehrerschein|ausbildung)', re.I),
    re.compile(r'führerschein\w*\s+(?:für\s+(?:den\s+|einen\s+)?)?(?:omni)?bus(?:se)?\b', re.I),
    re.compile(r'ausbildung\s+zu[mr]\s+(?:omni)?busfahrer', re.I),
    re.compile(r'personenbeförderungsschein|p-schein|fahrerlaubnis\s+zur\s+fahrgastbeförderung|fahrgastbeförderungsschein|taxischein', re.I),
]
# Bildungsträger, die OpenStreetMap nicht als Fahrschule führt (Name)
ANBIETER_NAME = r'fahrschul|fahrerschmiede|deula|verkehrsschule|kraftverkehrsschule|fahrer-?akademie|verkehrsakademie|berufskraftfahr|bkf|fahrlehr|omnibusschule|busschule'
# Weitere Anbieter ohne passenden OSM-Eintrag: (Website, Name, Ort)
ZUSATZ = [
    ('https://fahrerschmiede-hildesheim.de/', 'Fahrerschmiede Hildesheim', 'Hildesheim'),
    ('https://deula-hildesheim.de/fahrschule/', 'DEULA Hildesheim – Fahrschule', 'Hildesheim'),
]
KEINE_FAHRSCHULE = re.compile(r'jugendverkehrsschule|verkehrsgarten|verkehrswacht|übungsplatz', re.I)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def holen(url, timeout=20, max_bytes=1_500_000):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        typ = r.headers.get('Content-Type', '')
        if 'html' not in typ and 'text' not in typ:
            return ''
        roh = r.read(max_bytes)
    for enc in ('utf-8', 'cp1252'):
        try:
            return roh.decode(enc)
        except UnicodeDecodeError:
            pass
    return roh.decode('utf-8', 'replace')


def text_aus(h):
    h = re.sub(r'(?is)<(script|style|noscript|svg)\b.*?</\1>', ' ', h)
    h = re.sub(r'(?i)<(br|/p|/div|/li|/h\d|/tr|/td)\b[^>]*>', '\n', h)
    return re.sub(r'[ \t\r\f\v]+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', h)))


_robots = {}


def erlaubt(url):
    p = urllib.parse.urlsplit(url)
    basis = p.scheme + '://' + p.netloc
    if basis not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(basis + '/robots.txt', headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                rp.parse(r.read(200_000).decode('utf-8', 'replace').splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code >= 404 else ['User-agent: *', 'Disallow: /'])
        except Exception:
            rp.parse([])
        _robots[basis] = rp
    return _robots[basis].can_fetch(UA, url)


def beleg_aus(t):
    for rx in BELEG:
        m = rx.search(t)
        if m:
            a, b = max(0, m.start() - 40), min(len(t), m.end() + 40)
            return re.sub(r'\s+', ' ', t[a:b]).strip()
    return ''


def klassen_aus(t):
    # 1.1: nur aus Klassenlisten („Klassen …“, „Fahrerlaubnisklassen …“, „Führerschein …“), nicht aus Anschriften wie „D-37073“
    gef = []
    for ab in re.finditer(r'(?i:klassen?|führerschein\w*|fuehrerschein\w*|fahrerlaubnis\w*)\b[^.\n]{0,120}', t):
        for k in ('D1E', 'D1', 'DE', 'D', 'C1E', 'C1', 'CE', 'C'):
            if re.search(r'(?<![/\w.-])' + k + r'(?![\w/-]|\.\w)', ab.group(0)) and k not in gef:
                gef.append(k)
    if not any(k.startswith('D') for k in gef) and re.search(BELEG[1].pattern + '|' + BELEG[2].pattern + '|' + BELEG[3].pattern, t, re.I):
        gef.insert(0, 'D')   # „Bus-Führerschein“ ohne Klassenliste
    return ', '.join(gef)


def pruefen(s):
    """Website einer Fahrschule lesen → bus, beleg, klassen, azav, grund."""
    web = s['web']
    if not web:
        return s
    if not re.match(r'https?://', web, re.I):
        web = 'https://' + web.lstrip('/')
    try:
        if not erlaubt(web):
            s['grund'] = 'robots.txt untersagt den Abruf'
            return s
        start = holen(web)
    except Exception as e:
        s['grund'] = 'Website nicht erreichbar (%s)' % str(e)[:80]
        return s
    seiten = [start]
    host = urllib.parse.urlsplit(web).netloc
    for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', start):
        if len(seiten) > 4:
            break
        if not UNTERSEITEN.search(m.group(1) + ' ' + re.sub(r'<[^>]+>', ' ', m.group(2))):
            continue
        u = urllib.parse.urljoin(web, html.unescape(m.group(1)))
        if urllib.parse.urlsplit(u).netloc != host or u.lower().endswith(('.pdf', '.jpg', '.png')):
            continue
        try:
            if erlaubt(u):
                seiten.append(holen(u))
        except Exception:
            pass
    t = '\n'.join(text_aus(h) for h in seiten if h)
    s['beleg'] = beleg_aus(t) or s['beleg']
    s['klassen'] = klassen_aus(t)
    s['bus'] = s['bus'] or bool(s['beleg'])
    s['pschein'] = bool(BELEG[-1].search(t))
    s['azav'] = bool(re.search(r'\bAZAV\b|bildungsgutschein', t, re.I))
    s['grund'] = ''
    return s


def overpass(gebiet):
    q = ('[out:json][timeout:120];(nwr["amenity"="driving_school"](%s);nwr["name"~"%s",i](%s););out center tags 3000;'
         % (gebiet, ANBIETER_NAME, gebiet))
    daten = urllib.parse.urlencode({'data': q}).encode()
    for versuch in range(2):
        for u in OVERPASS:
            try:
                req = urllib.request.Request(u, data=daten, headers={'User-Agent': UA, 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read().decode('utf-8')), u
            except Exception as e:
                log('Overpass %s nicht nutzbar (%s)' % (u, e))
        time.sleep(30 * (versuch + 1))
    return None, ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ausgabe', default='daten')
    ap.add_argument('--gebiet', default=os.environ.get('GOEBUS_FAHRSCHUL_GEBIET') or GEBIET)
    a = ap.parse_args()
    os.makedirs(a.ausgabe, exist_ok=True)
    ziel = os.path.join(a.ausgabe, 'fahrschulen.json')
    j, server = overpass(a.gebiet)
    if not j:
        log('OpenStreetMap nicht erreichbar – bisherige Datei bleibt')
        return 0
    schulen = []
    gesehen = set()
    for e in j.get('elements', []):
        t = e.get('tags', {})
        name = (t.get('name') or '').strip()
        if not name or KEINE_FAHRSCHULE.search(name) or (e['type'], e['id']) in gesehen:
            continue
        gesehen.add((e['type'], e['id']))
        # Namenstreffer nur für Einrichtungen (keine Straßen, Haltestellen, Parkplätze)
        if t.get('amenity') != 'driving_school' and (t.get('highway') or t.get('public_transport') or t.get('railway') or t.get('amenity') in ('parking', 'bus_station')):
            continue
        lat = e.get('lat', (e.get('center') or {}).get('lat'))
        lon = e.get('lon', (e.get('center') or {}).get('lon'))
        lic = t.get('license_classes', '')
        schulen.append({'id': e['type'][0] + str(e['id']), 'name': name, 'web': t.get('website') or t.get('contact:website') or '',
                        'tel': t.get('phone') or t.get('contact:phone') or '', 'mail': t.get('email') or t.get('contact:email') or '',
                        'ort': t.get('addr:city', ''), 'plz': t.get('addr:postcode', ''),
                        'strasse': (t.get('addr:street', '') + ' ' + t.get('addr:housenumber', '')).strip(),
                        'lage': '%.5f,%.5f' % (lat, lon) if lat is not None else '',
                        'klassen': '', 'bus': bool(re.search(r'(^|;)\s*D', lic)), 'beleg': ('OpenStreetMap: Klassen ' + lic.replace(';', ', ')) if re.search(r'(^|;)\s*D', lic) else '',
                        'azav': False, 'grund': ''})
    bekannt = {urllib.parse.urlsplit(s['web'] if '://' in s['web'] else 'https://' + s['web']).netloc.lower().removeprefix('www.') for s in schulen if s['web']}
    for web, name, ort in ZUSATZ:
        if urllib.parse.urlsplit(web).netloc.lower().removeprefix('www.') in bekannt:
            continue
        schulen.append({'id': 'w' + re.sub(r'\W+', '-', urllib.parse.urlsplit(web).netloc), 'name': name, 'web': web, 'tel': '', 'mail': '', 'ort': ort, 'plz': '', 'strasse': '',
                        'lage': '', 'klassen': '', 'bus': False, 'beleg': '', 'azav': False, 'grund': ''})
    log('%d Fahrschulen in OpenStreetMap und Zusatzliste, %d mit Website' % (len(schulen), sum(1 for s in schulen if s['web'])))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(pruefen, [s for s in schulen if s['web']]))
    for s in schulen:
        if not s['bus'] and re.search(r'(^|[^a-zäöüß])(omni)?bus(fahr|schule|führerschein)', s['name'], re.I):
            s['bus'], s['beleg'] = True, 'Name: ' + s['name']
    schulen.sort(key=lambda s: (not s['bus'], s['ort'], s['name']))
    aus = {'version': VERSION, 'stand': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'gebiet': a.gebiet, 'server': server,
           'quelle': 'OpenStreetMap-Mitwirkende (ODbL), Angaben der Fahrschulen auf ihren Websites',
           'anzahl': len(schulen), 'mit_bus': sum(1 for s in schulen if s['bus']), 'schulen': schulen}
    with open(ziel, 'w', encoding='utf-8') as f:
        json.dump(aus, f, ensure_ascii=False, indent=1)
    log('%d mit Busführerschein → %s' % (aus['mit_bus'], ziel))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
