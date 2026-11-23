"""GöBUS – E-Fahrten (Einsatz- und Verstärkerfahrten im Linienverkehr) und Frühwagen
Fassung 69: liest die Zusatzfahrpläne der Verkehrsunternehmen aus, die die Verwaltung unter „Zusatzfahrpläne (E-Fahrten)“ führt
(_data/verbund-tarife/efahrten-quellen.json), sucht dabei selbst nach dem jeweils neuesten Stand und legt die Fahrten wie beim
Schülerverkehr auf die Straßen.

Formen (an den echten Dateien vom 23.11.2026 geprüft, außer den Frühwagen – dort nach der Bildschirmansicht):
  svhi        SVHI „Liste der aktuellen E Fahrten“: je Zeile eine Fahrt, Paare „7:00 Marienburg“ von links nach rechts, dazwischen
              Hinweise („ohne Halt bis“, „Angebot des RVHI“, „Fährt nur Dienstag, Donnerstag und Freitag“)
  wagenplan   RVHI „E-Wagenplan“: Nr. | Linie | von | bis | Linienverlauf „A - B - C“ | Bemerkung; Zeiten „7,30/34“ = zwei Fahrten
  matrix      GöVB-Linienfahrplan: Spalten je Fahrt, Kopfzeile „Linie 22 21 … E … E22“, darunter „S“ (nur an Schultagen);
              ausgelesen werden nur die E-Spalten (E-Wagen)
  fruehwagen  GöVB „Frühwagen (Basisfahrplan)“: Kästen „Haltestellen | Montag-Samstag | Umsteigemöglichkeiten“ nebeneinander
  linienseite Webseite mit vielen Linienfahrplänen (goevb.de/fahrplaene/linien): je Linie der heute gültige Stand, daraus „matrix“

Verbund-Zuordnung (Vorgabe der Verwaltung): SVHI + RVHI (Liste der SVHI) = ROSA, nur RVHI = RVHI, GöVB = VSN.
"""
import datetime
import hashlib
import html as _html
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request

import schulverkehr as S

VERSION = '1.0'
UA = 'GoeBUS-EFahrten/' + VERSION + ' (+https://www.goebus.de)'
VERBUND_NAME = {'rosa': 'ROSA', 'rvhi': 'RVHI', 'vsn': 'VSN'}
MONATE = {'januar': 1, 'februar': 2, 'märz': 3, 'maerz': 3, 'april': 4, 'mai': 5, 'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
          'oktober': 10, 'november': 11, 'dezember': 12}


# ---------------------------------------------------------------- Lesen

def fix(t):
    """GöVB-PDFs (dvi) setzen Umlaute als „u¨“; „(cid:5)“ sind Symbole ohne Text."""
    t = re.sub(r'\(cid:\d+\)', '', t)
    for a, b in (('a¨', 'ä'), ('o¨', 'ö'), ('u¨', 'ü'), ('A¨', 'Ä'), ('O¨', 'Ö'), ('U¨', 'Ü'), ('ä', 'ä'), ('ö', 'ö'), ('ü', 'ü')):
        t = t.replace(a, b)
    return t


def woerter(pfad, max_seiten=30):
    import pdfplumber
    seiten = []
    with pdfplumber.open(pfad) as pdf:
        for page in pdf.pages[:max_seiten]:
            try:
                ws = page.extract_words(x_tolerance=1.6, y_tolerance=2.2, keep_blank_chars=False, use_text_flow=False, extra_attrs=['size'])
            except Exception:
                ws = []
            out = []
            for w in ws:
                t = fix(str(w.get('text', ''))).strip()
                if t:
                    out.append({'t': t, 'x0': float(w['x0']), 'x1': float(w['x1']), 'top': float(w['top']), 'bottom': float(w['bottom']), 'size': float(w.get('size') or 0)})
            seiten.append(out)
    return seiten


def zeit(t):
    """„7:00“, „7.13“, „7,30“ → „07:00“; sonst ''"""
    m = re.match(r'^(\d{1,2})[:.,](\d{2})$', t.strip())
    if not m or int(m.group(1)) > 26 or int(m.group(2)) > 59:
        return ''
    return '%02d:%s' % (int(m.group(1)) % 24, m.group(2))


def datum_de(s):
    """„06. Januar 2026“, „08.09.2022“, „26.10.26“ → „2026-01-06“"""
    m = re.search(r'(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})', s)
    if m and m.group(2).lower() in MONATE:
        return '%s-%02d-%02d' % (m.group(3), MONATE[m.group(2).lower()], int(m.group(1)))
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', s)
    if m:
        j = int(m.group(3))
        j = j + 2000 if j < 100 else j
        return '%04d-%02d-%02d' % (j, int(m.group(2)), int(m.group(1)))
    return ''


def ganz(seiten):
    return ' '.join(w['t'] for s in seiten for w in s)


def art_erkennen(seiten):
    t = ganz(seiten[:2])
    if re.search(r'Liste der aktuellen E\s*-?\s*Fahrten', t, re.I):
        return 'svhi'
    if re.search(r'E-?WAGENPLAN', t, re.I) or ('Linienverlauf' in t and 'Bemerkung' in t):
        return 'wagenplan'
    if re.search(r'Fr[üu]hwagen', t, re.I) and 'Haltestellen' in t:
        return 'fruehwagen'
    for s in seiten[:3]:
        if _kopfzeilen(S.zeilen(s)):
            return 'matrix'
    return ''


# ---------------------------------------------------------------- SVHI-Liste

HINWEIS_RX = re.compile(r'^(?:ohne|Angebot|kein|weiter|über|fährt|Fährt|nur)$')


def svhi_liste(seiten):
    """→ {gueltig_ab, stand, fahrten: [{halte, block, betreiber, hinweise, tage}]}"""
    erg = {'fahrten': []}
    t = ganz(seiten)
    m = re.search(r'G[üu]ltig\s+ab\s+([^,]+?\d{4})', t)
    if m:
        erg['gueltig_ab'] = datum_de(m.group(1))
    m = re.search(r'Stand:\s*(\d{1,2}\.\d{1,2}\.\d{2,4})', t)
    if m:
        erg['stand'] = datum_de(m.group(1))
    for s in seiten:
        ws = [w for w in s if w['size'] < 12]
        zs = S.zeilen(ws, 2.0)
        ende = next((z['y'] for z in zs if re.match(r'^Nicht\s+aufgef', S.text(z['w']))), 1e9)
        haupt = [z for z in zs if z['y'] < ende and z['w'] and zeit(z['w'][0]['t']) and z['w'][0]['x0'] < 80]
        if not haupt:
            continue
        x_links = min(z['w'][0]['x0'] for z in haupt)
        haupt = [z for z in haupt if z['w'][0]['x0'] < x_links + 15]
        # Hinweise in eigener (kleinerer) Zeile gehören zur nächsten Hauptzeile
        for z in zs:
            if z['y'] >= ende or z in haupt or not z['w']:
                continue
            nah = min(haupt, key=lambda h: abs(h['y'] - z['y']))
            if abs(nah['y'] - z['y']) < 10:
                nah.setdefault('extra', []).append(z)
        for z in haupt:
            halte, hinweise, akt, rest = [], [], None, []
            for w in z['w']:
                zt = zeit(w['t'])
                if zt:
                    if rest:
                        hinweise.append(S.text(rest)); rest = []
                    akt = {'zeit': zt, 'name': [], 'x1': w['x1']}
                    halte.append(akt)
                    continue
                if akt is not None and not rest and (w['x0'] - akt['x1'] < 12 or not akt['name']) and not (akt['name'] and HINWEIS_RX.match(w['t'])) \
                        and not (not akt['name'] and HINWEIS_RX.match(w['t']) and w['x0'] - akt['x1'] > 12):
                    akt['name'].append(w)
                    akt['x1'] = w['x1']
                else:
                    if rest and w['x0'] - rest[-1]['x1'] > 25:
                        hinweise.append(S.text(rest)); rest = []
                    rest.append(w)
            if rest:
                hinweise.append(S.text(rest))
            for e in z.get('extra', []):
                hinweise.append(S.text(e['w']))
            if len(hinweise) > 1 and any(h.endswith(',') for h in hinweise):    # zweizeiliger Hinweis
                hinweise = [' '.join(hinweise)]
            hl = [[S.text(h['name']), h['zeit']] for h in halte if h['name']]
            if len(hl) < 2:
                continue
            f = {'halte': hl, 'block': 'morgens' if hl[0][1] < '11:00' else 'mittags'}
            hw = [re.sub(r'\s+', ' ', h).strip(' ,') for h in hinweise if h.strip(' ,')]
            if hw:
                f['hinweise'] = hw
            if any(re.search(r'Angebot des RVHI', h) for h in hw):
                f['betreiber'] = 'RVHI'
            mt = next((re.search(r'nur\s+(.+)$', h, re.I) for h in hw if re.search(r'Fährt nur|nur (Mo|Di|Mi|Do|Fr)', h, re.I)), None)
            if mt:
                f['tage'] = mt.group(1)
            erg['fahrten'].append(f)
    return erg


# ---------------------------------------------------------------- RVHI-E-Wagenplan

def _zeiten_liste(t):
    """„7,30/34“ → [07:30, 07:34]; „13,10/13,17“ → [13:10, 13:17]"""
    out, std = [], None
    for x in t.split('/'):
        x = x.strip()
        if re.match(r'^\d{2}$', x) and std is not None:
            out.append('%02d:%s' % (std, x))
            continue
        z = zeit(x)
        if z:
            std = int(z[:2])
            out.append(z)
    return out


def verlauf_teilen(t):
    """„Bockenem - Hildesheim (über Dammtor - Stadtwerke - ZOB)“ → (Halte, Hinweise)"""
    hinweise = []

    def klammer(m):
        inner = m.group(1).strip()
        if re.match(r'^über\s+', inner, re.I) and ' - ' in inner + ' ':
            return ' - ' + re.sub(r'^über\s+', '', inner, flags=re.I) + ' '
        hinweise.append(inner)
        return ' '
    t = re.sub(r'\(([^)]*)\)', klammer, t)
    t = re.sub(r'\s+weiter\s+nach\s+(.+)$', lambda m: hinweise.append('weiter nach ' + m.group(1).strip()) or '', t)
    t = re.sub(r'\s+nach\s+', ' - ', t)
    teile = [x.strip() for x in re.split(r'\s+-\s*', t) if x.strip()]
    if len(teile) < 2:     # „GS Limmer-Limmerburg-Warzer Weg-Göttinger Str.-Gerzen“ – ohne Leerzeichen
        teile = [x.strip() for x in re.split(r'(?<=[a-zäöüß.])-(?=[A-ZÄÖÜ][a-zäöüß])', t) if x.strip()]
    halte = []
    for x in teile:
        m = re.search(r'\s+ohne\s+(ZOB|Halt)$', x)
        if m:
            hinweise.append('ohne ' + m.group(1)); x = x[:m.start()]
        if re.search(r'/\s*alle\s+Haltestellen', x, re.I):
            hinweise.append('alle Haltestellen'); x = re.sub(r'\s*/\s*alle\s+Haltestellen', '', x, flags=re.I)
        x = re.sub(r'(\w)-\s+(?=[A-ZÄÖÜ])', r'\1-', re.sub(r'\s+', ' ', x)).strip(' -')    # „Brauhaus- Schule“
        if x:
            halte.append(x)
    return halte, hinweise


def wagenplan(seiten):
    erg = {'fahrten': []}
    t = ganz(seiten)
    m = re.search(r'G[üu]ltig\s+ab\s+(\d{1,2}\.\d{1,2}\.\d{2,4})', t)
    if m:
        erg['gueltig_ab'] = datum_de(m.group(1))
    for s in seiten:
        zs = S.zeilen(s, 2.0)
        kopf = next((z for z in zs if any(w['t'] == 'Linienverlauf' for w in z['w'])), None)
        if not kopf:
            continue
        x = {w['t']: w['x0'] for w in kopf['w']}
        xv, xb, xl, xm = x.get('von', 96), x.get('bis', 138), x.get('Linienverlauf', 165), x.get('Bemerkung', 9e9)
        xn = x.get('Nr.', 40)
        groesse = sorted(w['size'] for w in kopf['w'])[len(kopf['w']) // 2]
        for z in zs:
            if z['y'] <= kopf['y'] or not z['w'] or not re.match(r'^\d{1,3}$', z['w'][0]['t']) or z['w'][0]['x0'] > xn + 12:
                continue
            ws = z['w'][1:]
            linie = ' '.join(w['t'] for w in ws if w['x1'] < xv - 6)
            von = ''.join(w['t'] for w in ws if xv - 14 <= w['x0'] < xb - 14)
            bis = ''.join(w['t'] for w in ws if xb - 14 <= w['x0'] < xl - 3)
            bem = [w for w in ws if w['x0'] >= xm - 8 and w['size'] < groesse - 0.8]
            vl = [w for w in ws if w['x0'] >= xl - 3 and w not in bem]
            halte, hinweise = verlauf_teilen(S.text(vl))
            if bem:
                hinweise.append(S.text(bem))
            zv, zb = _zeiten_liste(von), _zeiten_liste(bis)
            if len(halte) < 2 or not zv:
                continue
            for i, a in enumerate(zv):
                b = zb[i] if i < len(zb) else (zb[-1] if zb else '')
                hl = [[n, ''] for n in halte]
                hl[0][1], hl[-1][1] = a, b
                f = {'nr': z['w'][0]['t'], 'linie': linie, 'halte': hl}
                if hinweise:
                    f['hinweise'] = list(hinweise)
                if linie.upper() == 'SVHI':
                    f['betreiber'] = 'SVHI'
                if linie.upper() == 'FV':
                    f['freigestellt'] = True
                mt = next((h for h in hinweise if re.match(r'^nur\s+(Mo|Di|Mi|Do|Fr|Sa)', h, re.I)), None)
                if mt:
                    f['tage'] = mt[4:]
                erg['fahrten'].append(f)
    return erg


# ---------------------------------------------------------------- GöVB-Linienfahrplan (Spalten), nur E-Wagen

LABEL_RX = re.compile(r'^(?:E\d{0,3}[a-z]?|\d{1,3}[a-z]?|N\d{1,2})$')
STRICH = {'{', '–', '-', '—', '|', '↓'}


def _kopfzeilen(zs):
    out = []
    for z in zs:
        w = z['w']
        if len(w) >= 7 and w[0]['t'] == 'Linie' and sum(1 for x in w[1:] if LABEL_RX.match(x['t'])) >= max(6, len(w) - 2) and all(len(x['t']) <= 4 for x in w[1:]):
            out.append(z)
    return out


def _zeit_woerter(ws):
    """„7“ + „.15“ (getrennt gesetzt) → „7.15“"""
    out = []
    for w in ws:
        if out and re.match(r'^[.,:]\d{2}$', w['t']) and re.match(r'^\d{1,2}$', out[-1]['t']) and w['x0'] - out[-1]['x1'] < 2.5:
            p = out[-1]
            out[-1] = dict(p, t=p['t'] + w['t'], x1=w['x1'])
            continue
        out.append(w)
    return out


def _linien_titel(zs, y_kopf):
    """Titelzeilen „Linie 2211 Nikolausberg - …“ (Nummer im Kasten doppelt gesetzt) → (['21', '22'], Verlauf)"""
    nr, verlauf = [], ''
    for z in zs:
        if z['y'] >= y_kopf - 20 or not z['w'] or z['w'][0]['t'] != 'Linie' or len(z['w']) < 3:
            continue
        n = z['w'][1]['t']
        if len(n) >= 4 and len(n) % 2 == 0 and n[0::2] == n[1::2]:     # „2211“ – jede Ziffer doppelt gesetzt
            n = n[0::2]
        if n not in nr:
            nr.append(n)
        verlauf = verlauf or S.text(z['w'][2:])
    return nr, verlauf


def matrix_e(seiten):
    erg = {'fahrten': []}
    t = ganz(seiten)
    m = re.search(r'g[üu]ltig\s+ab\s+(\d{1,2}\.\d{1,2}\.\d{2,4})', t, re.I)
    if m:
        erg['gueltig_ab'] = datum_de(m.group(1))
    for si, s in enumerate(seiten):
        zs = S.zeilen(s, 2.2)
        koepfe = _kopfzeilen(zs)
        for ki, kz in enumerate(koepfe):
            cols = [{'t': w['t'], 'x': (w['x0'] + w['x1']) / 2} for w in kz['w'][1:] if LABEL_RX.match(w['t'])]
            if not any(c['t'].startswith('E') for c in cols):
                continue
            abst = min(b['x'] - a['x'] for a, b in zip(cols, cols[1:])) if len(cols) > 1 else 23
            nummern, verlauf = _linien_titel(zs, kz['y'])
            # Verkehrstage über der Kopfzeile, je Spalte der nächste Abschnitt
            ueber = [z for z in zs if kz['y'] - 22 < z['y'] < kz['y'] - 3]
            tage = []
            for z in ueber:
                for sg in S.segmente(z['w'], 30):
                    tage.append(((sg[0]['x0'] + sg[-1]['x1']) / 2, S.text(sg)))
            for c in cols:
                c['tage'] = min(tage, key=lambda x: abs(x[0] - c['x']))[1] if tage else ''
            ende = koepfe[ki + 1]['y'] if ki + 1 < len(koepfe) else 1e9
            rumpf = [z for z in zs if kz['y'] < z['y'] < ende]
            if rumpf and all(w['t'] in ('S', 'F', 'W') for w in rumpf[0]['w']):
                for w in rumpf[0]['w']:
                    c = min(cols, key=lambda c: abs(c['x'] - (w['x0'] + w['x1']) / 2))
                    if abs(c['x'] - (w['x0'] + w['x1']) / 2) < abst * 0.6 and w['t'] == 'S':
                        c['schule'] = True
                rumpf = rumpf[1:]
            x_spalten = cols[0]['x'] - abst * 0.7
            reihen = []
            for z in rumpf:
                name = [w for w in z['w'] if w['x0'] < x_spalten - 4]
                werte = _zeit_woerter([w for w in z['w'] if w['x0'] >= x_spalten - 4])
                nt = re.sub(r'\.{2,}', ' ', S.text(name)).strip(' .')
                if not nt or not re.search(r'[A-Za-zÄÖÜäöüß]{3,}', nt) or re.match(r'^(weiter|nur|S\b|Fahrplan|Eingeschr)', nt):
                    if not nt and not werte:
                        continue
                    if re.match(r'^(weiter|nur|Fahrplan|Eingeschr)', nt) or (nt and not werte):
                        break
                    continue
                if not any(zeit(w['t']) or w['t'] in STRICH for w in werte):
                    continue
                reihen.append((re.sub(r'\s+', ' ', nt), werte))
            for c in cols:
                if not c['t'].startswith('E'):
                    continue
                hl = []
                for nt, werte in reihen:
                    for w in werte:
                        zt = zeit(w['t'])
                        if zt and abs((w['x0'] + w['x1']) / 2 - c['x']) < abst * 0.55:
                            hl.append([nt, zt])
                            break
                if len(hl) < 2:
                    continue
                f = {'linie': c['t'][1:] or '/'.join(nummern), 'halte': hl, 'tage': c['tage'], 'seite': si + 1, 'spalte': c['t']}
                if c.get('schule'):
                    f['schultage'] = True
                if verlauf:
                    f['verlauf'] = verlauf
                erg['fahrten'].append(f)
    erg['linien'] = sorted({n for s in seiten for n in _linien_titel(S.zeilen(s, 2.2), 1e9)[0]}, key=lambda x: (len(x), x))
    return erg


# ---------------------------------------------------------------- GöVB-Frühwagen (Kästen)

def fruehwagen(seiten):
    erg = {'fahrten': []}
    for s in seiten:
        zs = S.zeilen(s, 2.0)
        alle_h = [w for w in s if w['t'] == 'Haltestellen']
        for kz in [z for z in zs if any(w['t'] == 'Haltestellen' for w in z['w'])]:
            for h in [w for w in kz['w'] if w['t'] == 'Haltestellen']:
                x0 = h['x0'] - 4
                rechts = [w['x0'] for w in alle_h if w['x0'] > h['x0'] + 50 and abs(w['top'] - h['top']) < 60]
                x1 = min(rechts) - 6 if rechts else 9e9
                kopf = [w for w in kz['w'] if x0 <= w['x0'] < x1 and w is not h]
                cols = []
                for sg in S.segmente(kopf, 8):
                    tx = S.text(sg)
                    if re.match(r'^Umsteige', tx):
                        continue
                    cols.append({'t': tx, 'x': (sg[0]['x0'] + sg[-1]['x1']) / 2, 'halte': [], 'linie': []})
                if not cols:
                    continue
                titel = [z for z in zs if kz['y'] - 34 < z['y'] < kz['y'] - 2]
                tt = ' '.join(S.text([w for w in z['w'] if x0 - 40 <= w['x0'] < x1 and w['t'] != 'Haltestellen']) for z in titel
                              if not any(w['t'] == 'Haltestellen' and x0 <= w['x0'] < x1 for w in z['w']))
                tt = re.sub(r'\s+', ' ', re.sub(r'Fr[üu]hwagen \(Basisfahrplan\)|F[üu]r baustellenbedingte.*$', '', tt)).strip()
                ende = next((z['y'] - 30 for z in zs if z['y'] > kz['y'] + 4 and any(w['t'] == 'Haltestellen' and x0 <= w['x0'] < x1 for w in z['w'])), 1e9)
                for z in zs:
                    if not (kz['y'] < z['y'] < ende):
                        continue
                    ws = [w for w in z['w'] if x0 <= w['x0'] < x1]
                    if not ws:
                        continue
                    name = [w for w in ws if w['x1'] < cols[0]['x'] - 18]
                    rest = [w for w in ws if w not in name]
                    tx = S.text(rest)
                    m = re.match(r'^(?:weiter als\s+)?Linie\s+(\d{1,3})', tx)
                    if m and not name:
                        for c in cols:
                            c['linie'].append(m.group(1))
                        continue
                    if not name:
                        continue
                    for w in rest:
                        zt = zeit(w['t'])
                        if not zt:
                            continue
                        c = min(cols, key=lambda c: abs(c['x'] - (w['x0'] + w['x1']) / 2))
                        if abs(c['x'] - (w['x0'] + w['x1']) / 2) < 30:
                            c['halte'].append([S.text(name), zt])
                for c in cols:
                    if len(c['halte']) >= 2:
                        erg['fahrten'].append({'halte': c['halte'], 'tage': c['t'], 'linie': '/'.join(dict.fromkeys(c['linie'])), 'titel': tt, 'kasten': tt})
    return erg


# ---------------------------------------------------------------- Neuester Stand

def _datum_aus_url(u):
    u = urllib.parse.unquote(u)
    m = re.search(r'(\d{1,2})[._](\d{1,2})[._](\d{4}|\d{2})(?!\d)', u)
    if m:
        j = int(m.group(3)); j = j + 2000 if j < 100 else j
        try:
            return datetime.date(j, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', u)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r'(20\d{2})', u)
    return datetime.date(int(m.group(1)), 1, 1) if m else None


def pdf_links(html_text, basis):
    out = []
    for m in re.finditer(r'href\s*=\s*["\']([^"\']+\.pdf)(?:[?#][^"\']*)?["\']', html_text, re.I):
        u = urllib.parse.urljoin(basis, _html.unescape(m.group(1)))
        if u not in out:
            out.append(u)
    return out


MUSTER = {'svhi': r'E-?Fahrten', 'wagenplan': r'E-?Fahrten|E-?Wagen', 'fruehwagen': r'Fr(?:ue|ü)hwagen'}


def neuester(links, muster, heute):
    """Unter den passenden Links den mit dem jüngsten Datum, das schon gilt (sonst den frühesten künftigen)."""
    k = [(u, _datum_aus_url(u)) for u in links if re.search(muster, urllib.parse.unquote(u), re.I)]
    if not k:
        return ''
    gilt = [x for x in k if x[1] and x[1] <= heute]
    if gilt:
        return max(gilt, key=lambda x: x[1])[0]
    kuenftig = [x for x in k if x[1]]
    return min(kuenftig, key=lambda x: x[1])[0] if kuenftig else k[0][0]


def linien_auswahl(links, heute):
    """goevb.de/fahrplaene/linien: „…/Fahrplaene_ab_23.11.26/21_22_ab_23.11.26.PDF“ – je Linie der heute gültige Stand"""
    je = {}
    for u in links:
        n = urllib.parse.unquote(u.rsplit('/', 1)[-1])
        m = re.match(r'^(.+?)_ab_(\d{1,2}\.\d{1,2}\.\d{2,4})\.pdf$', n, re.I)
        if not m:
            continue
        d = _datum_aus_url(m.group(2))
        je.setdefault(m.group(1), []).append((u, d))
    out = []
    for k, v in sorted(je.items()):
        gilt = [x for x in v if x[1] and x[1] <= heute]
        u = max(gilt, key=lambda x: x[1])[0] if gilt else min(v, key=lambda x: x[1] or datetime.date(2100, 1, 1))[0]
        out.append((k, u))
    return out


# ---------------------------------------------------------------- Verorten

ERSATZ = [(r'\bGr\.\s*', 'Großer '), (r'\bKl\.\s*', 'Kleiner '), (r'\bHild\.\s*', 'Hildesheimer '), (r'\bKard\.\s*-?\s*', 'Kardinal-'),
          (r'\bPaulusk\.', 'Pauluskirche'), (r'Betram', 'Bertram'), (r'\s+(?:Bus)?[Ss]teig\s+\w+$', ''), (r'\bScharf\b', 'Scharfe'),
          (r'Zeppenfeldstraße', 'Zeppenfeldtstraße'), (r'\bRBG\b', 'Robert-Bosch-Gesamtschule'), (r'\bGS\b', 'Grundschule'),
          (r'\bH\.S\.', ''), (r'\bG\.R\.', ''), (r'Bad\s+gandersheim', 'Bad Gandersheim'), (r'\s*/\s*', ', ')]


STADTTEILE_HI = ('Himmelsthür', 'Itzum', 'Steuerwald', 'Ochtersum', 'Sorsum', 'Moritzberg', 'Drispenstedt', 'Bavenstedt', 'Marienburg', 'Neuhof',
                 'Achtum', 'Einum', 'Uppen', 'Marienrode', 'Oststadt', 'Weststadt', 'Nordstadt', 'Stadtfeld')


def suchname(n, ort=''):
    for a, b in ERSATZ:
        n = re.sub(a, b, n)
    n = re.sub(r'\bKBS\b', 'Kreisberufsschule', n)
    n = re.sub(r'\s+', ' ', n).strip(' ,')
    if ort == 'Hildesheim':     # „Himmelsthür Pauluskirche“, „Itzum Scharfe Ecke“ – in den Fahrplandaten ohne Stadtteil
        m = re.match(r'^(%s),?\s+(?!(?:Süd|Nord|Ost|West|Mitte)\b)([A-ZÄÖÜ].*)$' % '|'.join(STADTTEILE_HI), n)
        if m:
            n = m.group(2)
        n = re.sub(r'^Pauluskirche,\s*Jahnstr(?:aße|\.)?$', 'Pauluskirche', n)
    if n in ('ZOB', 'Hbf-ZOB', 'Hildesheim ZOB') and ort == 'Hildesheim':
        n = 'Hildesheim Hauptbahnhof-ZOB'
    return n


def ort_der_fahrt(f, vorgabe):
    if vorgabe:
        return vorgabe
    for n, _ in f['halte']:
        if n.startswith('Hildesheim') or n in ('ZOB', 'PvH', 'PVH', 'Bohlweg', 'Dammtor') or n.split(' ')[0] in STADTTEILE_HI:
            return 'Hildesheim'
    m = re.match(r'^([A-ZÄÖÜ][\wäöüß-]+)\s*/', f['halte'][0][0])
    return m.group(1) if m else ''


def verorten(fahrten, index, ort):
    """wie beim Schülerverkehr, mit Suchnamen (Abkürzungen der Listen ausgeschrieben); je Ort eine Gruppe"""
    gruppen = {}
    for f in fahrten:
        gruppen.setdefault(ort_der_fahrt(f, ort), []).append(f)
    for o, fs in gruppen.items():
        kopie = [{'halte': [[suchname(h[0], o), h[1]] for h in f['halte']]} for f in fs]
        S.verorten(kopie, index, o)
        if o and not ort:     # Linien über Land (RVHI): ein zweites Mal ohne Ort – sonst fiele „Bockenem“ als zu weit weg von Hildesheim heraus
            k2 = [{'halte': [list(h) for h in k['halte']]} for k in kopie]
            S.verorten(k2, index, '')
            rang = {'genau': 3, 'aehnlich': 2, 'ort': 1}
            for k, x in zip(kopie, k2):
                k['lage'] = [b if b and (not a or rang.get(b[2], 0) > rang.get(a[2], 0)) else a for a, b in zip(k['lage'], x['lage'])]
        for f, k in zip(fs, kopie):
            f['lage'] = k['lage']


# ---------------------------------------------------------------- Ablauf

def holen(url, ziel, zeit=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/pdf,text/html,*/*'})
    with urllib.request.urlopen(req, timeout=zeit) as r, open(ziel, 'wb') as fh:
        daten = r.read(40 * 1024 * 1024)
        fh.write(daten)
    return len(daten) > 20


def seite_lesen(url, holen_fn, tmp):
    p = os.path.join(tmp, 'seite-%s.html' % hashlib.sha1(url.encode()).hexdigest()[:10])
    holen_fn(url, p)
    return open(p, 'rb').read(4 * 1024 * 1024).decode('utf-8', 'replace')


def _plan(q, pid, titel, kurz, fahrten, link, gueltig_ab='', gruppe='e', extra=None):
    p = {'id': pid, 'quelle': q.get('id'), 'verbund': q.get('verbund', ''), 'betreiber': q.get('betreiber', ''), 'gruppe': gruppe,
         'titel': titel, 'kurz': kurz, 'link': link, 'fahrten': fahrten}
    if gueltig_ab:
        p['gueltig_ab'] = gueltig_ab
    if extra:
        p.update(extra)
    return p


def plaene_aus(q, art, erg, link):
    """Ergebnis eines Fahrplans → Einträge für Liste und Karte"""
    v, b = q.get('verbund', ''), q.get('betreiber', '')
    ga = erg.get('gueltig_ab', '')
    basis = re.sub(r'\W+', '-', str(q.get('id', 'q'))).strip('-')
    fs = erg.get('fahrten') or []
    out = []
    if art == 'svhi':
        for blk, titel in (('morgens', 'E-Fahrten morgens'), ('mittags', 'E-Fahrten mittags und nachmittags')):
            x = [f for f in fs if f.get('block') == blk]
            if x:
                out.append(_plan(q, '%s-%s' % (basis, blk), titel, 'E', x, link, ga, extra={'stand': erg.get('stand', ''), 'ort': 'Hildesheim'}))
    elif art == 'wagenplan':
        je = {}
        for f in fs:
            je.setdefault(f.get('linie') or '?', []).append(f)
        for li, x in sorted(je.items(), key=lambda kv: (not kv[0].isdigit(), len(kv[0]), kv[0])):
            t = ('Linie ' + li) if re.match(r'^\d', li) else ('Freigestellter Verkehr' if li.upper() == 'FV' else ('Fahrten der SVHI' if li.upper() == 'SVHI' else li))
            out.append(_plan(q, '%s-%s' % (basis, re.sub(r'\W+', '_', li)), t, li, x, link, ga, extra={'linie': li if re.match(r'^\d', li) else ''}))
    elif art == 'fruehwagen':
        je = {}
        for f in fs:
            je.setdefault(f.get('kasten') or 'Frühwagen', []).append(f)
        for i, (k, x) in enumerate(je.items()):
            li = '/'.join(dict.fromkeys(l for f in x for l in (f.get('linie') or '').split('/') if l))
            out.append(_plan(q, '%s-%d' % (basis, i + 1), k, 'FW', x, link, ga, gruppe='fruehwagen', extra={'linie': li, 'ort': 'Göttingen'}))
    elif art == 'matrix':
        li = '/'.join(erg.get('linien') or []) or (q.get('linie') or '')
        if fs:
            out.append(_plan(q, '%s-%s' % (basis, re.sub(r'\W+', '_', li or 'x')), 'Linie ' + li if li else (q.get('titel') or 'E-Wagen'), li or 'E', fs, link, ga,
                             extra={'linie': li, 'ort': q.get('ort', '')}))
    return out


def efahrten(quellen, halte, ausgabe, skizze=None, log=print, cache_pfad=None, holen_fn=None, woerter_fn=None, ende=None, heute=None):
    """quellen: [{id, titel, url, seite, art, verbund, betreiber, ort, aktiv}] (Verwaltung) → Inhalt von daten/efahrten.json"""
    holen_fn = holen_fn or holen
    woerter_fn = woerter_fn or woerter
    heute = heute or datetime.date.today()
    cache = {}
    if cache_pfad and os.path.exists(cache_pfad):
        try:
            cache = json.load(open(cache_pfad, encoding='utf-8'))
        except (ValueError, OSError):
            cache = {}
    index = S.HalteIndex(halte)
    tmp = tempfile.mkdtemp(prefix='goebus-efahrten-')
    plaene, quellen_info, neu_cache = [], [], {}

    def lies(url, art_vorgabe, q):
        """→ (art, erg, status)"""
        ziel = os.path.join(tmp, hashlib.sha1(url.encode()).hexdigest()[:12] + '.pdf')
        alt = cache.get(url)
        try:
            ok = holen_fn(url, ziel)
        except Exception as ex:
            ok = False
            log('E-Fahrten: nicht abrufbar', url[:90], str(ex)[:80])
        if not ok:
            if alt and alt.get('erg'):
                neu_cache[url] = alt
                return alt.get('art', ''), alt['erg'], 'vorrat'
            return '', {'fahrten': []}, 'nicht_abrufbar'
        h = hashlib.sha1(open(ziel, 'rb').read()).hexdigest()
        if alt and alt.get('hash') == h and alt.get('version') == VERSION and alt.get('erg') is not None:
            neu_cache[url] = alt
            return alt.get('art', ''), alt['erg'], 'ok'
        try:
            if S.art_von(ziel) != 'pdf':
                seiten, _ = S.woerter_lesen(ziel, url, holen_fn, tmp)
            else:
                seiten = woerter_fn(ziel)
            art = art_vorgabe if art_vorgabe in ('svhi', 'wagenplan', 'matrix', 'fruehwagen') else art_erkennen(seiten)
            erg = {'svhi': svhi_liste, 'wagenplan': wagenplan, 'matrix': matrix_e, 'fruehwagen': fruehwagen}.get(art, lambda s: {'fahrten': []})(seiten)
        except Exception as ex:
            art, erg = '', {'fahrten': [], 'fehler': str(ex)[:120]}
        neu_cache[url] = {'hash': h, 'version': VERSION, 'art': art, 'erg': erg, 'gelesen': heute.isoformat()}
        return art, erg, 'ok'

    for q in quellen:
        if q.get('aktiv') is False or not (q.get('url') or q.get('seite')):
            continue
        art = q.get('art') or 'auto'
        info = {'id': q.get('id'), 'titel': q.get('titel', ''), 'verbund': q.get('verbund', ''), 'url': q.get('url', ''), 'art': art}
        if art == 'linienseite':
            try:
                links = pdf_links(seite_lesen(q.get('seite') or q['url'], holen_fn, tmp), q.get('seite') or q['url'])
            except Exception as ex:
                links = []
                info['fehler'] = str(ex)[:100]
            auswahl = linien_auswahl(links, heute)
            if not auswahl and cache.get('seite:' + str(q.get('id'))):
                auswahl = [tuple(x) for x in cache['seite:' + str(q.get('id'))]]
                info['abruf'] = 'vorrat'
            neu_cache['seite:' + str(q.get('id'))] = auswahl
            info['dateien'], info['mit_e'] = len(auswahl), 0
            for key, u in auswahl:
                if ende and time.time() > ende:
                    info['abgebrochen'] = True
                    break
                a, erg, st = lies(u, 'matrix', q)
                x = plaene_aus(dict(q, linie=key.replace('_', '/')), 'matrix', erg, u)
                for p in x:
                    p['status'] = st
                info['mit_e'] += 1 if x else 0
                plaene += x
            quellen_info.append(info)
            continue
        url = q.get('url', '')
        if q.get('seite'):     # neuesten Stand auf der Seite des Unternehmens suchen
            try:
                links = pdf_links(seite_lesen(q['seite'], holen_fn, tmp), q['seite'])
                n = neuester(links, q.get('muster') or MUSTER.get(art, r'E-?Fahrten|E-?Wagen|Fr(?:ue|ü)hwagen'), heute)
                if n and n != url:
                    info['gefunden'] = n
                    log('E-Fahrten: neuerer Stand für', q.get('titel') or q.get('id'), '→', n.rsplit('/', 1)[-1])
                    url = n
            except Exception as ex:
                info['seite_fehler'] = str(ex)[:100]
        a, erg, st = lies(url, art, q)
        info.update({'art': a or art, 'status': st, 'fahrten': len(erg.get('fahrten') or []), 'genutzt': url})
        if erg.get('gueltig_ab'):
            info['gueltig_ab'] = erg['gueltig_ab']
        if erg.get('fehler'):
            info['fehler'] = erg['fehler']
        x = plaene_aus(q, a, erg, url)
        for p in x:
            p['status'] = st
        plaene += x
        quellen_info.append(info)
    # Verorten und Wege
    feats = []
    for p in plaene:
        for f in p['fahrten']:
            f['halte'] = [h for h in f['halte'] if S.ist_name(h[0])]
        p['fahrten'] = [f for f in p['fahrten'] if len(f['halte']) >= 2]
        verorten(p['fahrten'], index, p.get('ort', '') if p.get('gruppe') != 'e' or p.get('ort') else '')
        linien, alle_gerade, muster, zu = [], True, {}, []
        for fi, f in enumerate(p['fahrten']):
            pts = [(l[0], l[1]) for l in (f.get('lage') or []) if l]
            if len(pts) < 2:
                continue
            k = ';'.join('%.5f,%.5f' % q for q in pts)
            if k in muster:
                zu[muster[k]].append(fi)
                continue
            muster[k] = len(zu)
            zu.append([fi])
            weg = skizze(pts) if skizze and (ende is None or time.time() < ende) else None
            if weg:
                alle_gerade = False
                linien.append([[round(x, 6), round(y, 6)] for x, y in weg])
            else:
                linien.append([[round(x, 6), round(y, 6)] for x, y in pts])
        p['verortet'] = [sum(1 for f in p['fahrten'] for l in f.get('lage') or [] if l), sum(len(f['halte']) for f in p['fahrten'])]
        if linien:
            feats.append({'type': 'Feature', 'geometry': {'type': 'MultiLineString', 'coordinates': linien},
                          'properties': {'ref': str(p['id']), 'id': p['id'], 'name': p['titel'], 'verbund': p['verbund'], 'gruppe': p['gruppe'],
                                         'verlauf': 'haltestellen' if alle_gerade else 'skizze', 'fahrten': zu}})
    if cache_pfad:
        try:
            with open(cache_pfad, 'w', encoding='utf-8') as fh:
                json.dump(neu_cache, fh, ensure_ascii=False, separators=(',', ':'))
        except OSError:
            pass
    zahl = {'quellen': len(quellen_info), 'plaene': len(plaene), 'fahrten': sum(len(p['fahrten']) for p in plaene),
            'halte': sum(p['verortet'][1] for p in plaene), 'verortet': sum(p['verortet'][0] for p in plaene),
            'skizziert': sum(1 for f in feats if f['properties']['verlauf'] == 'skizze'),
            'je_verbund': {v: sum(len(p['fahrten']) for p in plaene if p['verbund'] == v) for v in sorted({p['verbund'] for p in plaene})}}
    log('E-Fahrten: %(quellen)d Quellen, %(plaene)d Einträge, %(fahrten)d Fahrten, %(verortet)d von %(halte)d Halten verortet, %(skizziert)d über die Straßen' % zahl)
    return {'type': 'FeatureCollection', 'version': VERSION, 'erzeugt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'attribution': 'Zusatzfahrpläne: SVHI, RVHI, GöVB; Haltestellen: DELFI e.V. über GTFS.DE (CC BY 4.0)',
            'zahl': zahl, 'quellen': quellen_info, 'plaene': plaene, 'features': feats}


def quellen_laden(url_oder_pfad, log=print):
    if not url_oder_pfad:
        return []
    try:
        if re.match(r'^https?://', url_oder_pfad):
            req = urllib.request.Request(url_oder_pfad, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode('utf-8'))
        else:
            d = json.load(open(url_oder_pfad, encoding='utf-8'))
    except Exception as ex:
        log('E-Fahrten: Liste der Zusatzfahrpläne nicht geladen –', ex)
        return []
    e = d.get('quellen') if isinstance(d, dict) else d
    return [x for x in (e or []) if isinstance(x, dict) and x.get('id')]
