#!/usr/bin/env python3
"""
GöBUS – Fahrplanseiten der Verbünde für die Website zwischenspeichern
=====================================================================

Läuft bei GitHub zusammen mit dem Liniennetz (siehe .github/workflows/liniennetz.yml) – oder auf jedem Rechner mit Python 3.9+.
Keine Zusatzpakete nötig. Version 1.0 (Fassung 46).

Warum hier und nicht auf der Website?
  Der Webserver (STRATO) erreicht extern.gipsprojekt.de nicht („Connection timed out“, auch über IPv4). Dort liegen die
  Fahrplantabellen von RVHI und ROSA mit den PDF-Links je Linie und Richtung. GitHub lädt diese Seiten und legt sie
  unverändert (nur nach UTF-8 umgewandelt) neben die Liniennetze. Die Website liest die Kopie mit demselben Leser wie
  die Originalseite. Die PDFs selbst lädt niemand herunter – die Website verlinkt sie direkt.

Ablauf
  1. Adressen: die Quellen der Linienlisten der Website (linien-regeln.json, Variable GOEBUS_ADRESSE) und die Liste STANDARD.
  2. Je Adresse: robots.txt beachten, Seite laden, Verweisen auf weitere Fahrplantabellen bei extern.gipsprojekt.de folgen.
  3. daten/fahrplanseiten/<Kennung>.html und daten/fahrplanseiten.json (Adresse → Datei, Stand, Anzahl PDF-Links) schreiben.

Aufruf
  python fahrplanseiten.py --ausgabe daten
"""
import argparse
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser

VERSION = '1.0'
UA = 'GoeBUS-Fahrplanseiten/' + VERSION + ' (+https://www.goebus.de)'
STANDARD = [
    'https://extern.gipsprojekt.de/hildesheim/rvhi/rvhi2019/isoli/tfp/tfp_linien.htm',
    'https://extern.gipsprojekt.de/hildesheim/svhi/svhi2023/isoli/tfp/tfp_linien2017.htm',
]
FOLGEN = re.compile(r'''(?:https?:)?//extern\.gipsprojekt\.de/[^\s"'<>]+?/tfp/tfp_linien[^\s"'<>]*\.htm''', re.I)
MAX_SEITEN = 25

_robots = {}


def erlaubt(url):
    teile = urllib.parse.urlsplit(url)
    basis = teile.scheme + '://' + teile.netloc
    if basis not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(basis + '/robots.txt', headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                rp.parse(r.read().decode('utf-8', 'replace').splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code in (404, 410) else ['User-agent: *', 'Disallow: /'] if e.code in (401, 403) else [])
        except Exception:
            rp.parse([])
        _robots[basis] = rp
    return _robots[basis].can_fetch(UA, url)


def laden(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml'})
    with urllib.request.urlopen(req, timeout=40) as r:
        roh = r.read(8 * 1024 * 1024)
        zs = r.headers.get_content_charset() or ''
    if not zs:
        m = re.search(rb'''<meta[^>]+charset=["']?([\w-]+)''', roh[:4000], re.I)
        zs = m.group(1).decode('ascii', 'ignore') if m else ''
    for k in [zs, 'utf-8', 'windows-1252']:
        if not k:
            continue
        try:
            text = roh.decode(k)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        text = roh.decode('utf-8', 'replace')
    # Zeichensatz-Angaben auf UTF-8 setzen (die Datei wird als UTF-8 gespeichert)
    text = re.sub(r'''(<meta[^>]+charset=["']?)[\w-]+''', r'\1utf-8', text, flags=re.I)
    return text


def adressen_der_website():
    u = os.environ.get('GOEBUS_REGELN', '').strip()
    if not u.startswith('http'):
        return []
    try:
        req = urllib.request.Request(u, headers={'User-Agent': UA, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print('Linienlisten der Website nicht lesbar:', e)
        return []
    out = []
    for l in (j.get('listen') or {}).values():
        for z in str((l or {}).get('quelle') or '').splitlines():
            z = z.strip()
            if z.startswith('https://') or z.startswith('http://'):
                out.append(z)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ausgabe', default='daten')
    a = ap.parse_args()
    ziel = os.path.join(a.ausgabe, 'fahrplanseiten')
    os.makedirs(ziel, exist_ok=True)
    warte = list(dict.fromkeys(STANDARD + adressen_der_website()))
    index = {'version': VERSION, 'stand': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'seiten': {}}
    gesehen = set()
    while warte and len(gesehen) < MAX_SEITEN:
        url = warte.pop(0)
        if url in gesehen:
            continue
        gesehen.add(url)
        eintrag = {'stand': index['stand']}
        try:
            if not erlaubt(url):
                eintrag.update(ok=False, grund='robots.txt untersagt den Abruf')
            else:
                text = laden(url)
                kennung = hashlib.sha1(url.encode('utf-8')).hexdigest()[:20]
                with open(os.path.join(ziel, kennung + '.html'), 'w', encoding='utf-8') as f:
                    f.write(text)
                pdfs = len(re.findall(r'''href\s*=\s*["']?[^"'\s>]+\.pdf''', text, re.I))
                eintrag.update(ok=True, datei='fahrplanseiten/' + kennung + '.html', bytes=len(text.encode('utf-8')), pdf_links=pdfs)
                for v in FOLGEN.findall(text):
                    v = ('https:' + v) if v.startswith('//') else v
                    if v not in gesehen:
                        warte.append(v)
            print(('✔ ' if eintrag.get('ok') else '✘ ') + url, eintrag.get('pdf_links', eintrag.get('grund', '')))
        except Exception as e:
            eintrag.update(ok=False, grund=str(e)[:200])
            print('✘', url, e)
        index['seiten'][url] = eintrag
        time.sleep(1)
    with open(os.path.join(a.ausgabe, 'fahrplanseiten.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
