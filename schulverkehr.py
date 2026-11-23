"""GöBUS – Freigestellter Schülerverkehr (Fassung 67, zu liniennetz.py 3.7)

Liest die Fahrpläne des Schülerverkehrs, die in der Verwaltung (Bereich „Schülerverkehr“) eingetragen sind, aus den PDF-Dateien
aus, verortet die Haltestellen über die Fahrplandaten (DELFI) und legt die Fahrten über die Straßen (eigener Routenplaner des
Liniennetz-Laufs). Ergebnis: daten/schulverkehr_hi.json – je Fahrplan der Verwaltung ein Eintrag mit Fahrten, Halten, Zeiten,
Unternehmen und Verlauf.

Drei Formen von Fahrplänen werden erkannt (Stand November 2026):
  einsatzplan – Kästen „Fahrt-Nr. 109; Fahrplan 2245 H“ mit den Spalten „Abfahrt | Haltestelle“ (Landkreis Hildesheim,
                Omnibusbetrieb Fischer, IGS Bad Salzdetfurth), mehrere Kästen neben- und untereinander;
  liste       – „Fahrt Nr. 110, Fahrplan 2333 H Sowada KB“ bzw. „Einsatz 901, Elbe-Leine-Express“, darunter „7.45 Uhr Glashütte“;
  tabelle     – Haltestellen als Zeilen, Fahrten als Spalten (RVHI-Zusatzfahrpläne, Molitorisschule Harsum), auch zwei Tabellen
                nebeneinander (Hin- und Rückfahrten); Spalten öffentlicher Linien („Linie 67“) gehören ins Liniennetz und werden
                hier nur vermerkt.
Unbekannte Formen liefern „keine Fahrten erkannt“ – die Verwaltung zeigt das an; die Website verlinkt dann nur das PDF.
"""
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request

VERSION = '1.2'   # Fassung 68: Uhrzeiten mit Komma (RVHI), Halte ohne eigene Zeit, Webseiten und Bilder (Texterkennung); 69: Linienfahrpläne der RVHI (beide Richtungen)
UA = 'GoeBUS-Schulverkehr/' + VERSION + ' (+https://www.goebus.de)'

ZEIT_RX = re.compile(r'^(\d{1,2})[:.,](\d{2})(?:[*¹²³)]+|\s*Uhr)?$')
SPANNE_RX = re.compile(r'^(\d{1,2})[:.,](\d{2})\s*[-–]\s*\d{1,2}[:.,]\d{2}[*)]*$')
DURCH_RX = re.compile(r'^(?:[I|lǀ¦│]|—|–|-|↓|\.{1,3})$')
FAHRT_RX = re.compile(r'Fahrt\s*-?\s*Nr\.?\s*(\d{1,5}[A-Z]?)\b', re.I)
FAHRPLAN_RX = re.compile(r'Fahrplan\s*(?:Nr\.?\s*)?(\d{3,5})\s*([HZR])?\b', re.I)
EINSATZ_RX = re.compile(r'Einsatz(?:fahr)?(?:plan)?\s*(?:Nr\.?\s*)?(\d{1,4})\b', re.I)
TAGE_RX = re.compile(r'\b(?:nur\s+)?(Mo|Di|Mi|Do|Fr|Sa)\.?\s*(?:[-–]\s*(Mo|Di|Mi|Do|Fr|Sa)\.?)?\b', re.I)
UNTERNEHMEN_RX = re.compile(r'Unternehmen\s*:\s*(.+)$', re.I)
GUELTIG_RX = re.compile(r'g[üu]ltig\s+ab\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})', re.I)
OEFFENTLICH_RX = re.compile(r'\bLinie\s*\d{1,4}\b', re.I)
RICHTUNG_RX = re.compile(r'^(HIN|R[ÜU]CK)\s*-?\s*FAHRTE?N?$', re.I)
KOPFWORT_RX = re.compile(r'^(?:Abfahrt|Haltestelle|Ankunft|an|ab|Uhr|Zeit)$', re.I)


# ---------------------------------------------------------------------------------------------------------------------------
# Einlesen: Wörter mit Lage (pdfplumber)
# ---------------------------------------------------------------------------------------------------------------------------
def woerter_aus_pdf(pfad):
    """Seiten als Listen von Wörtern {t, x0, x1, top, bottom, size}. Ohne Textebene (gescannt): leere Seiten."""
    import pdfplumber    # im GitHub-Lauf installiert (pip install pdfplumber)
    seiten = []
    with pdfplumber.open(pfad) as pdf:
        for page in pdf.pages[:12]:
            try:
                ws = page.extract_words(x_tolerance=1.6, y_tolerance=2.2, keep_blank_chars=False, use_text_flow=False, extra_attrs=['size'])
            except Exception:
                ws = []
            seiten.append([{'t': w['text'], 'x0': float(w['x0']), 'x1': float(w['x1']), 'top': float(w['top']), 'bottom': float(w['bottom']),
                            'size': float(w.get('size') or 0)} for w in ws if str(w.get('text', '')).strip()])
    return seiten


def woerter_ocr(bild):
    """1.1: Texterkennung (tesseract, deutsch) für Bilder und eingescannte PDFs → Wörter wie aus pdfplumber, in Punkt umgerechnet
    (Maßstab: mittlere Wortgröße ≈ 9 pt), damit dieselben Abstände wie im PDF gelten."""
    import shutil
    import subprocess
    if not shutil.which('tesseract'):
        return []
    try:
        r = subprocess.run(['tesseract', bild, 'stdout', '-l', 'deu', '--psm', '6', 'tsv'], capture_output=True, text=True, timeout=180)
    except Exception:
        return []
    roh = []
    for z in r.stdout.splitlines()[1:]:
        t = z.split('\t')
        if len(t) < 12 or t[0] != '5' or not t[11].strip():
            continue
        try:
            conf = float(t[10])
        except ValueError:
            conf = -1
        if conf < 20:
            continue
        x, y, w, h = (int(t[6]), int(t[7]), int(t[8]), int(t[9]))
        roh.append((t[11].strip(), x, y, w, h))
    if not roh:
        return []
    hs = sorted(x[4] for x in roh)
    f = 9.0 / max(1, hs[len(hs) // 2])
    return [{'t': t, 'x0': x * f, 'x1': (x + w) * f, 'top': y * f, 'bottom': (y + h) * f, 'size': h * f} for t, x, y, w, h in roh]


def art_von(pfad):
    k = open(pfad, 'rb').read(512)
    if k[:5] == b'%PDF-':
        return 'pdf'
    if k[:8] == b'\x89PNG\r\n\x1a\n' or k[:3] == b'\xff\xd8\xff' or k[:4] in (b'GIF8', b'RIFF'):
        return 'bild'
    if re.search(rb'<(?:!doctype|html|head|body)', k[:512], re.I):
        return 'html'
    return ''


def woerter_lesen(pfad, url, holen_fn, tmp, woerter_fn=None, tiefe=0):
    """1.1: PDF (ohne Text: Seiten als Bild und Texterkennung), Bild (Texterkennung) oder Webseite (die verlinkten Fahrpläne und
    Fahrplan-Bilder darauf, höchstens vier). → (Seiten mit Wörtern, Herkunft 'pdf' | 'ocr' | 'webseite+…')"""
    a = art_von(pfad)
    if a == 'pdf':
        seiten = (woerter_fn or woerter_aus_pdf)(pfad)
        if sum(len(x) for x in seiten) >= 8:
            return seiten, 'pdf'
        try:    # eingescannt: Seiten als Bild (pypdfium2 kommt mit pdfplumber)
            import pdfplumber
            out = []
            with pdfplumber.open(pfad) as pdf:
                for i, page in enumerate(pdf.pages[:6]):
                    b = os.path.join(tmp, 'seite-%d-%d.png' % (id(pfad) % 100000, i))
                    page.to_image(resolution=250).original.save(b)
                    out.append(woerter_ocr(b))
            return out, 'ocr'
        except Exception:
            return seiten, 'pdf'
    if a == 'bild':
        return [woerter_ocr(pfad)], 'ocr'
    host = lambda x: re.sub(r'^www\.', '', urllib.parse.urlsplit(x).netloc.lower())
    if a == 'html' and tiefe == 0 and not host(url).endswith('goebus.de'):    # eigene Seiten: Fahrpläne liegen dort immer als Datei
        import html as _h
        roh = open(pfad, 'rb').read(3 * 1024 * 1024).decode('utf-8', 'replace')
        ziele = []
        for m in re.finditer(r'<(?:a|img)\b[^>]*?(?:href|src)\s*=\s*["\']([^"\']+)["\'][^>]*>', roh, re.I):
            u = urllib.parse.urljoin(url, _h.unescape(m.group(1)))
            if host(u) == host(url) and re.search(r'\.(?:pdf|jpe?g|png|gif|webp)(?:$|[?#])', u, re.I) and re.search(r'bus|fahr|einsatz|linie', urllib.parse.unquote(u), re.I) and u not in ziele:
                ziele.append(u)
        # WordPress-Vorschaubilder („…-300x212.jpg“) nur, wenn das große Bild fehlt
        ziele = [u for u in ziele if not (re.search(r'-\d{2,4}x\d{2,4}(\.\w+)(?:$|[?#])', u) and re.sub(r'-\d{2,4}x\d{2,4}(\.\w+)', r'\1', u) in ziele)]
        seiten, her = [], set()
        for i, u in enumerate(ziele[:4]):
            ziel = os.path.join(tmp, 'teil-%d-%d' % (id(pfad) % 100000, i))
            try:
                if not holen_fn(u, ziel):
                    continue
            except Exception:
                continue
            s2, h2 = woerter_lesen(ziel, u, holen_fn, tmp, woerter_fn, 1)
            seiten += s2
            her.add(h2)
        return seiten, 'webseite+' + '+'.join(sorted(her)) if her else 'webseite'
    return [], a or 'unbekannt'


def zeit_von(t):
    """'7.45' / '07:20' / '8:20-8:22' / '7:12*' → 'HH:MM' oder ''"""
    t = t.strip()
    m = ZEIT_RX.match(t) or SPANNE_RX.match(t)
    if not m:
        return ''
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return ''
    return '%02d:%02d' % (h, mi)


def zeilen(ws, tol=2.6):
    """Wörter zu Zeilen (nach Höhe), jede Zeile nach x sortiert."""
    out = []
    for w in sorted(ws, key=lambda w: (round(w['top'], 1), w['x0'])):
        mitte = (w['top'] + w['bottom']) / 2
        for z in out:
            if abs(z['y'] - mitte) <= tol:
                z['w'].append(w)
                break
        else:
            out.append({'y': mitte, 'w': [w]})
    for z in out:
        z['w'].sort(key=lambda w: w['x0'])
    out.sort(key=lambda z: z['y'])
    return out


def segmente(ws, luecke=14.0):
    """Wörter einer Zeile in Abschnitte teilen, wo der Abstand groß ist (Spalten, Kästen nebeneinander)."""
    seg, akt = [], []
    for w in ws:
        if akt and w['x0'] - akt[-1]['x1'] > luecke:
            seg.append(akt)
            akt = []
        akt.append(w)
    if akt:
        seg.append(akt)
    return seg


def text(ws):
    return re.sub(r'\s+', ' ', ' '.join(w['t'] for w in ws)).strip()


# 1.1: Wochentage und Gültigkeiten sind keine Haltestellen (Übersichtsseite der Molitorisschule Harsum: Zeilen „Mo-Fr“, „Fr“ –
# „Fr“ landete als genauer Treffer in Braunschweig)
_TAG = r'(?:mo|di|mi|do|fr|sa|so|montags?|dienstags?|mittwochs?|donnerstags?|freitags?|samstags?|sonntags?)\.?'
TAGE_RX = re.compile(r'^(?:' + _TAG + r'(?:\s*(?:[-–/,+&]|bis|und)\s*' + _TAG + r')*|täglich|werktags|schultags?|schultage|ferien|nur an schultagen)$', re.I)


def ist_name(t):
    return bool(re.search(r'[A-Za-zÄÖÜäöüß]{2,}', t)) and not zeit_von(t) and not KOPFWORT_RX.match(t) and not TAGE_RX.match(t.strip())


def fahrt_plausibel(f):
    """1.1: Eine Fahrt hält nicht dreimal am selben Halt und besteht nicht überwiegend aus Wiederholungen
    (sonst wurde eine Übersicht oder Legende als Fahrt gelesen)."""
    namen = [norm(h[0]) and ' '.join(norm(h[0])) for h in f['halte']]
    if any(not n for n in namen):
        return False
    from collections import Counter
    c = Counter(namen)
    return max(c.values()) <= 2 and len(c) >= max(2, 0.6 * len(namen))


# ---------------------------------------------------------------------------------------------------------------------------
# Erkennen der Fahrten
# ---------------------------------------------------------------------------------------------------------------------------
def kopf_lesen(zeilen_kopf):
    """Kopfzeilen eines Kastens oder einer Liste → nr, fahrplan, richtung, tage, titel, anschluss"""
    if isinstance(zeilen_kopf, str):
        zeilen_kopf = [zeilen_kopf]
    t = ' ; '.join(zeilen_kopf)
    k = {}
    m = FAHRT_RX.search(t)
    if m:
        k['nr'] = m.group(1)
    m = FAHRPLAN_RX.search(t)
    if m:
        k['fahrplan'] = m.group(1)
        if m.group(2):
            k['richtung'] = 'hin' if m.group(2).upper() == 'H' else 'rueck'
    m = EINSATZ_RX.search(t)
    if m and 'nr' not in k:
        k['einsatz'] = m.group(1)
    tage = []
    for m in re.finditer(r'\(([^)]*\b(?:Mo|Di|Mi|Do|Fr)\b[^)]*)\)', t):
        tage.append(m.group(1).strip())
    if not tage:
        m = re.search(r'\b(nur\s+(?:mo|di|mi|do|fr)[a-z.]*|(?:Mo|Di|Mi|Do|Fr)\.?\s*[-–]\s*(?:Mo|Di|Mi|Do|Fr)\.?)', t, re.I)
        if m:
            tage.append(m.group(1).strip())
    if tage:
        k['tage'] = tage[0]
    if re.search(r'ANSCHLU(?:SS|ß)', t, re.I):
        k['anschluss'] = True
    # Strecke: Zeilen ohne Nummern („Kemme, Kaufhaus - Ottbergen Schulzentrum“, „Ottbergen Grundschule -“ + „Wöhle (Di. - Do.)“, „Ab Glashütte …“)
    strecke = []
    for z in zeilen_kopf:
        if FAHRT_RX.search(z) or FAHRPLAN_RX.search(z) or EINSATZ_RX.search(z) or UNTERNEHMEN_RX.search(z) or re.search(r'ANSCHLU(?:SS|ß)TOUR|^Beginn\b|g[üu]ltig', z, re.I):
            continue
        z = re.sub(r'\([^)]*\)', '', z).strip()
        if z:
            strecke.append(z)
    st = re.sub(r'\s+', ' ', ' '.join(strecke)).strip(' ;,')
    st = re.sub(r'\s*[-–]\s*', ' – ', st).strip(' –')
    if st and (' – ' in st or re.match(r'^(?:Ab|Von|Nach)\s', st, re.I)) and len(st) > 4:
        k['titel'] = st[:120]
    return k


def modus(zs):
    """'paare' (Uhrzeit links, Haltestelle rechts) oder 'tabelle' (Haltestelle links, Uhrzeiten in Spalten)"""
    links, rechts = 0, 0
    for z in zs:
        ts = [w for w in z['w'] if zeit_von(w['t'])]
        if not ts:
            continue
        erst = z['w'][0]
        if zeit_von(erst['t']):
            links += 1
        elif ist_name(erst['t']) and len([w for w in z['w'] if zeit_von(w['t']) or DURCH_RX.match(w['t'])]) >= 1:
            rechts += 1
    if not links and not rechts:
        return ''
    return 'paare' if links >= rechts else 'tabelle'


def fahrten_paare(zs, seite):
    """Form „einsatzplan“ und „liste“: Paare Uhrzeit → Haltestelle, zugeordnet dem nächsten Kasten darüber."""
    paare, anker, richtungen = [], [], []
    for z in zs:
        ws = z['w']
        for seg in segmente(ws, 30.0):
            st = text(seg)
            if RICHTUNG_RX.match(st.replace(' ', '')) or RICHTUNG_RX.match(st):
                richtungen.append({'x0': seg[0]['x0'], 'y': z['y'], 'r': 'hin' if st.upper().startswith('HIN') else 'rueck'})
        i = 0
        while i < len(ws):
            w = ws[i]
            zt = zeit_von(w['t'])
            if zt:
                j = i + 1
                if j < len(ws) and re.match(r'^Uhr$', ws[j]['t'], re.I):
                    j += 1
                name = []
                while j < len(ws) and not zeit_von(ws[j]['t']) and (not name and ws[j]['x0'] - w['x1'] < 80 or name and ws[j]['x0'] - name[-1]['x1'] < 9):
                    name.append(ws[j])
                    j += 1
                nm = text(name)
                if nm and ist_name(nm):
                    paare.append({'zeit': zt, 'name': nm, 'x0': w['x0'], 'y': z['y'], 'nx0': name[0]['x0'], 'h': max(4.0, name[0]['bottom'] - name[0]['top'])})
                i = j
                continue
            if w['t'].lower() == 'abfahrt':
                anker.append({'x0': w['x0'], 'y': z['y'], 'art': 'abfahrt'})
            i += 1
    # 1.1: Folgezeilen ohne Uhrzeit, linksbündig unter dem Namen des vorigen Halts („Nach Ganztag, Sowada“: 15.50 Uhr Lamspringe,
    # Grundschule – Lamspringe, Hellegrund – Glashütte) → Halte ohne eigene Zeit
    for z in zs:
        if any(zeit_von(w['t']) for w in z['w']):
            continue
        seg = segmente(z['w'], 14.0)[0]
        nm = text(seg)
        if not ist_name(nm) or len(nm) > 45 or nm.endswith('.') or FAHRT_RX.search(nm) or EINSATZ_RX.search(nm):
            continue
        vor = [p for p in paare if abs(p.get('nx0', -99) - seg[0]['x0']) < 6 and 0 < z['y'] - p['y'] < 2.6 * p.get('h', 8)]
        if vor:
            v = max(vor, key=lambda p: p['y'])
            paare.append({'zeit': '', 'name': nm, 'x0': v['x0'], 'y': z['y'], 'nx0': v['nx0'], 'h': v['h']})
    # Kopfzeilen (Fahrt-Nr., Einsatz) – Anker, wenn es keine „Abfahrt“-Spalten gibt; sonst Kopftext der Kästen
    koepfe = []
    for z in zs:
        for seg in segmente(z['w'], 18.0):
            st = text(seg)
            if FAHRT_RX.search(st) or FAHRPLAN_RX.search(st) or EINSATZ_RX.search(st):
                koepfe.append({'x0': seg[0]['x0'], 'x1': seg[-1]['x1'], 'y': z['y'], 't': st})
    if not anker:
        anker = [{'x0': k['x0'], 'y': k['y'] - 0.5, 'art': 'kopf'} for k in koepfe]
    if not anker and paare:
        anker = [{'x0': min(p['x0'] for p in paare), 'y': min(p['y'] for p in paare) - 1, 'art': 'seite'}]
    fahrten = []
    for a in anker:
        a['halte'] = []
    for p in paare:
        best = None
        for a in anker:
            if a['y'] < p['y'] and abs(p['x0'] - a['x0']) <= 45 and (best is None or a['y'] > best['y']):
                best = a
        if best is not None:
            best['halte'].append(p)
    for a in anker:
        if len(a['halte']) < 2:
            continue
        # Kopftext: Zeilen über dem Anker im Bereich des Kastens (Einsatzplan) bzw. die Kopfzeile selbst und die Zeile darunter (Liste)
        kt = []
        oben = a['y'] - (70 if a['art'] == 'abfahrt' else 40 if a['art'] == 'seite' else 0)    # 1.1: Liste ohne Fahrt-Nr.: Überschrift darüber
        unten = a['y'] if a['art'] == 'abfahrt' else min(h['y'] for h in a['halte'])
        rechts = min([b['x0'] for b in anker if b is not a and abs(b['y'] - a['y']) < 6 and b['x0'] > a['x0'] + 20] or [a['x0'] + 230]) - 6
        for z in zs:
            if oben - 0.1 <= z['y'] <= unten + (0 if a['art'] == 'abfahrt' else -0.1):
                for seg in segmente(z['w'], 18.0):
                    mitte = (seg[0]['x0'] + seg[-1]['x1']) / 2
                    if a['x0'] - 25 <= mitte <= rechts and not any(zeit_von(w['t']) for w in seg):
                        kt.append((z['y'], text(seg)))
        if a['art'] == 'abfahrt':     # nur der Kasten über dieser Spalte: ab der letzten Zeile mit Fahrt-Nr. oder Strecke
            fn = [i for i, (_, t0) in enumerate(kt) if FAHRT_RX.search(t0) or FAHRPLAN_RX.search(t0)]
            if fn:
                kt = kt[fn[-1]:]
            else:     # ohne Nummer: nur die Zeilen direkt über „Abfahrt“ (Strecke)
                kt = [x for x in kt if a['y'] - x[0] <= 26]
        kopf = kopf_lesen([t0 for _, t0 in kt if not re.match(r'^(?:Abfahrt|Haltestelle)\b', t0, re.I)])
        if 'richtung' not in kopf and richtungen:
            r = [x for x in richtungen if x['y'] < a['y'] and x['x0'] <= a['x0'] + 60]
            if r:
                kopf['richtung'] = max(r, key=lambda x: (x['x0'], x['y']))['r']
        halte = sorted(a['halte'], key=lambda h: h['y'])
        fahrten.append(dict(kopf, seite=seite, halte=[[h['name'], h['zeit']] for h in halte]))
    return fahrten


def fahrten_tabelle(zs, seite):
    """Form „tabelle“: Haltestellen als Zeilen, Fahrten als Spalten – auch zwei Tabellen nebeneinander und untereinander."""
    sizes = sorted(w['size'] for z in zs for w in z['w'] if w['size'])
    med = sizes[len(sizes) // 2] if sizes else 0
    # Bereiche untereinander: an Überschriften (deutlich größere Schrift) teilen
    bereiche, akt = [], []
    for z in zs:
        gross = med and max((w['size'] for w in z['w']), default=0) >= med * 1.45 and not any(zeit_von(w['t']) for w in z['w'])
        if gross and akt:
            bereiche.append(akt)
            akt = []
        akt.append(z)
    if akt:
        bereiche.append(akt)
    fahrten = []
    for br in bereiche:
        zeilen_daten, alle_zeiten = [], []
        for z in br:
            reihe = {'y': z['y'], 'namen': [], 'werte': []}
            for seg in segmente(z['w'], 7.5):
                st = text(seg)
                if all(zeit_von(w['t']) or DURCH_RX.match(w['t']) for w in seg):
                    for w in seg:
                        zt = zeit_von(w['t'])
                        if zt:
                            reihe['werte'].append({'x': (w['x0'] + w['x1']) / 2, 'zeit': zt})
                            alle_zeiten.append((w['x0'] + w['x1']) / 2)
                elif ist_name(st):
                    reihe['namen'].append({'x0': seg[0]['x0'], 'x1': seg[-1]['x1'], 't': st})
            zeilen_daten.append(reihe)
        if len(alle_zeiten) < 4:
            continue
        # Spalten: Uhrzeiten nach x bündeln
        xs = sorted(alle_zeiten)
        spalten, gruppe = [], [xs[0]]
        for x in xs[1:]:
            if x - gruppe[-1] > 9:
                spalten.append(sum(gruppe) / len(gruppe))
                gruppe = []
            gruppe.append(x)
        spalten.append(sum(gruppe) / len(gruppe))
        # Namensspalten (links jeder Tabelle): x0 der Namen, auf deren Zeile Uhrzeiten folgen
        nx = sorted(n['x0'] for r in zeilen_daten if r['werte'] for n in r['namen'])
        blocke = []
        for x in nx:
            if not blocke or x - blocke[-1][-1] > 40:
                blocke.append([x])
            else:
                blocke[-1].append(x)
        bl = [min(b) for b in blocke if len(b) >= 2] or [min(nx)] if nx else []
        if not bl:
            continue

        def block_von(x):
            k = [b for b in bl if b < x]
            return max(k) if k else None
        erste_daten = min((r['y'] for r in zeilen_daten if r['werte']), default=0)
        richtungen, ueber_y = [], set()
        for z in br:
            if z['y'] >= erste_daten:
                continue
            if med and max((w['size'] for w in z['w']), default=0) >= med * 1.45:
                ueber_y.add(z['y'])
            for seg in segmente(z['w'], 12.0):
                st = text(seg).replace(' ', '')
                m = re.match(r'^(Hin|R[üu]ck)fahrt(?:en)?$', st, re.I)
                if m:
                    ueber_y.add(z['y'])
                    richtungen.append({'cx': (seg[0]['x0'] + seg[-1]['x1']) / 2, 'r': 'hin' if m.group(1).lower() == 'hin' else 'rueck'})
        for sx in spalten:
            b = block_von(sx)
            if b is None:
                continue
            halte = []
            for r in zeilen_daten:
                if not r['werte']:
                    continue
                w = [v for v in r['werte'] if abs(v['x'] - sx) <= 8]
                if not w:
                    continue
                n = [m for m in r['namen'] if block_von(m['x0'] + 0.1) == b and m['x0'] < sx]
                if not n:
                    continue
                name = max(n, key=lambda m: m['x0'])['t']
                halte.append([name, w[0]['zeit']])
            if len(halte) < 2:
                continue
            # In einer Spalte stehen die Halte oft nicht in Fahrtreihenfolge (eine Tabelle für mehrere Wege) – nach der Uhrzeit ordnen
            halte = [h for _, h in sorted(enumerate(halte), key=lambda x: (x[1][1], x[0]))]
            # Spaltenkopf: Wörter über der ersten Datenzeile in dieser Spalte (ohne Überschriften wie „H i n f a h r t e n“)
            kopf = []
            for z in br:
                if z['y'] < erste_daten and z['y'] not in ueber_y:
                    for w in z['w']:
                        if abs((w['x0'] + w['x1']) / 2 - sx) <= 14:
                            kopf.append(w['t'])
            kt = ' '.join(kopf)
            richt = ''
            ri = [h for h in richtungen if h['cx'] > b - 20]
            if ri:
                richt = min(ri, key=lambda h: abs(h['cx'] - sx))['r']
            f = {'seite': seite, 'spalte': kt[:60], 'halte': halte}
            if richt:
                f['richtung'] = richt
            if OEFFENTLICH_RX.search(kt) and not re.search(r'\bFV\b|frei', kt, re.I):
                f['oeffentlich'] = OEFFENTLICH_RX.search(kt).group(0)
            m = TAGE_RX.search(kt)
            if m:
                f['tage'] = m.group(0)
            fahrten.append(f)
    return fahrten


def auslesen(seiten):
    """Wörter aller Seiten → {'art': …, 'unternehmen': …, 'gueltig_ab': …, 'fahrten': […]}"""
    erg = {'art': '', 'fahrten': [], 'woerter': sum(len(ws) for ws in seiten)}
    for i, ws in enumerate(seiten):
        if not ws:
            continue
        zs = zeilen(ws)
        for z in zs:
            st = text(z['w'])
            m = UNTERNEHMEN_RX.search(st)
            if m and 'unternehmen' not in erg:
                erg['unternehmen'] = re.split(r'\s{2,}|\bEinsatzplan\b', m.group(1))[0].strip()[:80]
            m = GUELTIG_RX.search(st)
            if m and 'gueltig_ab' not in erg:
                j = int(m.group(3)); j = j + 2000 if j < 100 else j
                erg['gueltig_ab'] = '%04d-%02d-%02d' % (j, int(m.group(2)), int(m.group(1)))
        md = modus(zs)
        if md == 'paare':
            f = fahrten_paare(zs, i + 1)
            if f:
                erg['art'] = erg['art'] or ('einsatzplan' if any(w['t'].lower() == 'abfahrt' for w in ws) else 'liste')
            erg['fahrten'] += f
        elif md == 'tabelle':
            f = fahrten_tabelle(zs, i + 1)
            if f:
                erg['art'] = erg['art'] or 'tabelle'
            erg['fahrten'] += f
        # Unternehmen in der Liste: „Fahrt Nr. 110, Fahrplan 2333 H Sowada KB“ / „Einsatz 901, Elbe-Leine-Express“
        if 'unternehmen' not in erg and zs:
            st = text(zs[0]['w'])
            m = re.match(r'^Einsatz\s*\d+\s*,\s*(.+)$', st, re.I)
            if m:
                erg['unternehmen'] = m.group(1).strip()[:80]
    erg['fahrten'] = [f for f in erg['fahrten'] if len(f['halte']) >= 2]
    # 1.1: Halte, die nur Wochentage sind, und unplausible Fahrten verwerfen
    for f in erg['fahrten']:
        f['halte'] = [h for h in f['halte'] if ist_name(h[0])]
    erg['fahrten'] = [f for f in erg['fahrten'] if len(f['halte']) >= 2 and fahrt_plausibel(f)]
    return erg


# ---------------------------------------------------------------------------------------------------------------------------
# Verorten der Haltestellen (Fahrplandaten DELFI)
# ---------------------------------------------------------------------------------------------------------------------------
ABK = {'str': 'strasse', 'abzw': 'abzweigung', 'gs': 'grundschule', 'bhf': 'bahnhof', 'bf': 'bahnhof', 'hbf': 'hauptbahnhof',
       'obs': 'oberschule', 'apo': 'apotheke', 'kirchpl': 'kirchplatz', 'sch': 'schule', 'turnh': 'turnhalle', 'ri': 'richtung',
       'mitte': 'mitte', 'ortsmitte': 'mitte', 'dorfmitte': 'mitte', 'gh': 'gasthaus', 'bushaltestelle': ''}
STOP = {'am', 'an', 'der', 'die', 'das', 'den', 'dem', 'bei', 'beim', 'b', 'v', 'vor', 'und', 'zum', 'zur', 'hst', 'haltestelle', 'h'}


def norm(s):
    s = unicodedata.normalize('NFC', str(s)).lower().replace('ß', 'ss')
    s = re.sub(r'(\w)str(?:asse|\.)', r'\1 strasse', s)      # Turmstr. / Heerstraße → turm strasse
    s = re.sub(r'[,/()\-.:;"\'’]+', ' ', s)
    out = []
    for t in s.split():
        t = ABK.get(t, t)
        if t and t not in STOP:
            out.extend(t.split())
    return out


def _km(a, b):
    """Abstand zweier (lon, lat) in km"""
    dx = (a[0] - b[0]) * 111.32 * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = (a[1] - b[1]) * 110.57
    return math.hypot(dx, dy)


class HalteIndex:
    """Haltestellen der Fahrplandaten (Name, lon, lat) für die Suche nach Namensteilen."""

    def __init__(self, halte):
        self.halte = []
        self.wort = {}
        gesehen = set()
        for n, lon, lat in halte:
            k = (n, round(lon, 4), round(lat, 4))
            if k in gesehen or not n:
                continue
            gesehen.add(k)
            tok = norm(n)
            i = len(self.halte)
            self.halte.append((n, lon, lat, set(tok)))
            for t in set(tok):
                self.wort.setdefault(t, []).append(i)

    def kandidaten(self, name, ort=''):
        """→ [(score, name, lon, lat, guete)] – guete: genau (alle Wörter), aehnlich (alle besonderen Wörter), ort (nur der Ort)"""
        q = norm(name)
        if not q or len(''.join(q)) < 3:     # 1.1: „Fr“, „B“ – zu kurz für einen Namen
            return []
        qs = set(q)
        teile = re.split(r'\s*[,/]\s*|\s+-\s+', str(name).strip(), maxsplit=1)
        ort_q = set(norm(teile[0])) if len(teile) > 1 else (qs if len(qs) == 1 else set())
        besonders = qs - ALLGEMEIN - ort_q
        listen = [self.wort.get(t, []) for t in qs]
        listen = [l for l in listen if l]
        if not listen:
            return []
        menge = set(min(listen, key=len))
        if ort_q:
            for t in ort_q:
                menge |= set(self.wort.get(t, [])[:400])
        ortw = set(norm(ort))
        out = []
        for i in menge:
            n, lon, lat, ts = self.halte[i]
            if not qs & ts:
                continue
            ueber = len(ts - qs - ortw)
            if qs <= ts:
                out.append((1.0 - 0.03 * ueber, n, lon, lat, 'genau'))
            elif besonders and besonders <= ts and (not ort_q or ort_q & ts):
                out.append((0.8 - 0.03 * ueber, n, lon, lat, 'aehnlich'))
            elif ort_q and ort_q <= ts:
                out.append((0.4, n, lon, lat, 'ort'))
        out.sort(key=lambda x: -x[0])
        return out


ALLGEMEIN = {'strasse', 'weg', 'platz', 'mitte', 'schule', 'grundschule', 'oberschule', 'bahnhof', 'kirche', 'dorf', 'alte', 'alter', 'neue', 'neuer',
             'schulzentrum', 'zob', 'abzweigung', 'siedlung', 'ort', 'gasthaus', 'markt'}


def _min(z):
    return int(z[:2]) * 60 + int(z[3:])


def verorten(fahrten, index, ort=''):
    """Zwei Durchgänge: Fahrten ohne einen einzigen genauen Treffer (nur Schul- und Ortshalte) richten sich im zweiten nach der
    Mitte der übrigen Fahrten desselben Fahrplans."""
    _verorten(fahrten, index, ort, None)
    pk = [(l[0], l[1]) for f in fahrten for l in f.get('lage') or [] if l and l[2] == 'genau']
    if pk:
        mitte = (sum(x[0] for x in pk) / len(pk), sum(x[1] for x in pk) / len(pk))
        offen = [f for f in fahrten if not any(f.get('lage') or [])]
        if offen:
            _verorten(offen, index, ort, mitte)
    return fahrten


def _verorten(fahrten, index, ort, mitte):
    """Jede Haltestelle aller Fahrten → [lon, lat, guete] oder None. guete: genau | aehnlich | ort (ungefähr, nur der Ort bekannt).
    Mehrdeutige Namen nach der Nähe zu den Nachbarhalten; unplausible Sprünge (schneller als 80 km/h) werden verworfen."""
    ort_tok = norm(ort)
    om = None     # 1.1: Mitte des Schulorts (Median – „Ottbergen“ gibt es auch bei Höxter)
    if ort:
        ko = index.kandidaten(ort, ort)
        ko = [x for x in ko if x[0] >= (ko[0][0] - 0.05)] if ko else []
        if ko:
            om = (sorted(x[2] for x in ko)[len(ko) // 2], sorted(x[3] for x in ko)[len(ko) // 2])
    for f in fahrten:
        lage = []
        for name, _ in f['halte']:
            k = index.kandidaten(name, ort)
            weit = bool(k) and om is not None and all(_km((x[2], x[3]), om) > 20 for x in k[:40])    # 1.1: nur gleichnamige Halte anderswo
            if (not k or weit) and ort_tok and not set(norm(name)) & set(ort_tok):
                k = (k if weit else []) + [x[:4] + ('ort' if x[4] == 'ort' else 'aehnlich',) for x in index.kandidaten(ort + ', ' + name, ort)]    # „Glashütte“ in Lamspringe
            lage.append(k[:40])
        pos = [None] * len(lage)
        # 1. eindeutig genaue Treffer
        for i, k in enumerate(lage):
            if not k or k[0][4] != 'genau':
                continue
            gute = [x for x in k if x[4] == 'genau' and x[0] >= k[0][0] - 0.01]
            if max(_km((gute[0][2], gute[0][3]), (x[2], x[3])) for x in gute) < 0.8:
                pos[i] = (k[0][2], k[0][3], 'genau')
        # 2. Plausibilität der eindeutigen: ein Halt, der zu beiden Nachbarn unpassend weit liegt, war eine Namensgleichheit
        def passt(i, p, q, j):
            d = _km(p, q)
            a, b = f['halte'][j][1], f['halte'][i][1]
            dt = abs(_min(a) - _min(b)) if a and b else abs(i - j) * 5    # 1.1: Halt ohne eigene Zeit – etwa 5 Minuten je Halt
            return d <= max(6.0, dt * 1.35 + 2)
        for _ in range(2):
            for i in range(len(pos)):
                if not pos[i]:
                    continue
                nb = [j for j in (i - 1, i + 1, i - 2, i + 2) if 0 <= j < len(pos) and pos[j]]
                if nb and not any(passt(i, pos[i], pos[j], j) for j in nb[:2]):
                    pos[i] = None
        # 2b. (1.1) ein einzelner genauer Treffer weit weg vom Schulort, den kein anderer bestätigt, war eine Namensgleichheit
        #     („Glashütte“ in Lamspringe ≠ Osterwald, Glashütte, wenn die übrigen Halte nur im Ort bekannt sind)
        if om:
            for i in range(len(pos)):
                if pos[i] and _km(pos[i], om) > 20 and not any(pos[j] and j != i and _km(pos[i], pos[j]) < 20 for j in range(len(pos))):
                    pos[i] = None
        # 3. übrige über die Nachbarn (genau vor ähnlich vor Ort)
        for _ in range(3):
            for i, k in enumerate(lage):
                if pos[i] or not k:
                    continue
                nb = [j for j in sorted(range(len(pos)), key=lambda j: abs(j - i)) if pos[j] and j != i][:2]
                best, bw = None, None
                for score, n, lon, lat, g in k:
                    if nb and not any(passt(i, (lon, lat), pos[j], j) for j in nb):
                        continue
                    if not nb and g != 'genau' and not (mitte and _km((lon, lat), mitte) <= 20) and not (om and _km((lon, lat), om) <= 12):
                        continue
                    if not nb and om and _km((lon, lat), om) > 20:      # 1.1: ohne Nachbarn nicht weit weg vom Schulort
                        continue
                    d = min((_km((lon, lat), pos[j]) for j in nb), default=_km((lon, lat), mitte) if mitte else 0)
                    wert = score - d * 0.01
                    if bw is None or wert > bw:
                        best, bw = (lon, lat, g), wert
                if best:
                    pos[i] = best
        # 4. „ort“: Mitte der Haltestellen dieses Orts in der Nähe des gewählten Punkts
        for i, k in enumerate(lage):
            if pos[i] and pos[i][2] == 'ort':
                nahe = [(x[2], x[3]) for x in k if x[4] == 'ort' and _km((x[2], x[3]), pos[i]) < 2.5]
                if nahe:
                    pos[i] = (sum(x[0] for x in nahe) / len(nahe), sum(x[1] for x in nahe) / len(nahe), 'ort')
        f['lage'] = [[round(p[0], 6), round(p[1], 6), p[2]] if p else None for p in pos]
    return fahrten


# ---------------------------------------------------------------------------------------------------------------------------
# Gesamtablauf
# ---------------------------------------------------------------------------------------------------------------------------
def holen(url, ziel, zeit=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/pdf,*/*'})
    with urllib.request.urlopen(req, timeout=zeit) as r, open(ziel, 'wb') as fh:
        daten = r.read(30 * 1024 * 1024)
        fh.write(daten)
    return len(daten) > 20    # 1.1: auch Webseiten und Bilder (Art erkennt woerter_lesen)


def gleiche_firma(a, b):
    k = lambda x: re.sub(r'\b(?:gmbh|mbh|e\.?\s?k\.?|kg|co|ohg|ag|ug|inh\.?)\b|[^a-zäöüß0-9 ]', '', x.lower()).split()
    ka, kb = k(a), k(b)
    return bool(ka and kb) and (ka == kb[:len(ka)] or kb == ka[:len(kb)])


def unternehmen_aus(eintrag, erg, f):
    namen = []
    for n in (f.get('unternehmen'), erg.get('unternehmen'), eintrag.get('unternehmen')):
        n = (n or '').strip()
        if n and not any(gleiche_firma(n, x) for x in namen):
            namen.append(n)
    return namen


def schulverkehr(eintraege, halte, ausgabe, skizze=None, log=print, cache_pfad=None, holen_fn=None, woerter_fn=None, ende=None):
    """eintraege: Fahrpläne der Verwaltung [{id, schule, ort, zone, unternehmen, link, spiegel, schuljahr, stunde, beschreibung}]
    halte: [(Name, lon, lat)] der Fahrplandaten; skizze: Funktion(Punkte) → Weg über die Straßen ([] = gerade, None = keine Zeit)
    → Inhalt von daten/schulverkehr_hi.json"""
    holen_fn = holen_fn or holen
    woerter_fn = woerter_fn or woerter_aus_pdf
    cache = {}
    if cache_pfad and os.path.exists(cache_pfad):
        try:
            cache = json.load(open(cache_pfad, encoding='utf-8'))
        except (ValueError, OSError):
            cache = {}
    index = HalteIndex(halte)
    import tempfile
    tmp = tempfile.mkdtemp(prefix='goebus-schule-')     # nicht in daten/ – der Lauf übernimmt daten/ ins Repository
    plaene, feats, neu_cache = [], [], {}
    for e in eintraege:
        p = {k: e.get(k, '') for k in ('id', 'schule', 'ort', 'zone', 'link', 'schuljahr', 'stunde', 'beschreibung')}
        p['unternehmen'] = [e['unternehmen']] if e.get('unternehmen') else []
        pdf = os.path.join(tmp, '%s.pdf' % re.sub(r'\W', '', str(e.get('id', 'x'))))
        ok, quelle = False, ''
        for u in [x for x in (e.get('link'), e.get('spiegel')) if x]:
            try:
                ok = holen_fn(u, pdf)
            except Exception as ex:
                ok = False
                p.setdefault('fehler_abruf', []).append('%s: %s' % (u[:60], str(ex)[:60]))
            if ok:
                quelle = u
                break
        alt = cache.get(str(e.get('id')))
        if not ok:
            p['status'] = 'nicht_abrufbar' if e.get('link') else 'ohne_link'
            if not (alt and alt.get('link') == e.get('link') and alt.get('erg')):
                plaene.append(p)
                continue
            erg, p['abruf'] = alt['erg'], 'vorrat'     # letzter gelesener Stand derselben Adresse
            neu_cache[str(e.get('id'))] = alt
        else:
            # 1.2 (Fassung 69): Linienfahrplan der RVHI bei gipsprojekt („…/tfp_642_1.pdf“) – die Rückrichtung „_2“ gehört dazu
            pdf2 = ''
            m2 = re.search(r'/tfp_(\w+)_1\.pdf$', quelle, re.I)
            if m2:
                pdf2 = pdf + '.2.pdf'
                try:
                    if not holen_fn(quelle[:m2.start()] + '/tfp_%s_2.pdf' % m2.group(1), pdf2):
                        pdf2 = ''
                except Exception:
                    pdf2 = ''
            h = hashlib.sha1(open(pdf, 'rb').read() + (open(pdf2, 'rb').read() if pdf2 else b'')).hexdigest()
            if alt and alt.get('hash') == h and alt.get('version') == VERSION and alt.get('erg'):
                erg = alt['erg']
            else:
                try:
                    seiten, herkunft = woerter_lesen(pdf, quelle, holen_fn, tmp, woerter_fn)
                    if pdf2:
                        seiten = seiten + woerter_lesen(pdf2, quelle, holen_fn, tmp, woerter_fn)[0]
                    erg = auslesen(seiten)
                    erg['herkunft'] = herkunft
                except Exception as ex:
                    erg = {'art': '', 'fahrten': [], 'fehler': str(ex)[:120]}
            neu_cache[str(e.get('id'))] = {'hash': h, 'version': VERSION, 'erg': erg, 'link': e.get('link'), 'gelesen': time.strftime('%Y-%m-%d')}
            p['abruf'] = 'spiegel' if quelle != e.get('link') else 'quelle'
        p['art'] = erg.get('art', '')
        if erg.get('herkunft') and erg['herkunft'] != 'pdf':
            p['herkunft'] = erg['herkunft']     # 1.1: „ocr“ (Texterkennung) oder „webseite+…“
        if erg.get('gueltig_ab'):
            p['gueltig_ab'] = erg['gueltig_ab']
        fahrten = [dict(f) for f in erg.get('fahrten', [])]
        oeff = sorted({f['oeffentlich'] for f in fahrten if f.get('oeffentlich')})
        # 1.2: Ist der Eintrag selbst eine Linie („Linie 642“ – Grundschule Delligsen, Oberschule Duingen), gehören ihre Fahrten dazu
        ml = re.search(r'\bLinie\s*(\d{2,4})\b', '%s %s' % (e.get('beschreibung', ''), e.get('schule', '')))
        if ml:
            p['linie'] = ml.group(1)
            for f in fahrten:
                if f.get('oeffentlich') and re.sub(r'\D', '', f['oeffentlich']) in ('', ml.group(1)):
                    f.pop('oeffentlich')
                    f['linie'] = ml.group(1)
            oeff = [x for x in oeff if re.sub(r'\D', '', x) != ml.group(1)]
        fahrten = [f for f in fahrten if not f.get('oeffentlich')]
        for f in fahrten:     # 1.1: auch für den Vorrat älterer Läufe
            f['halte'] = [h for h in f['halte'] if ist_name(h[0])]
        fahrten = [f for f in fahrten if len(f['halte']) >= 2 and fahrt_plausibel(f)]
        if oeff:
            p['oeffentlich'] = oeff
        for n in unternehmen_aus(e, erg, {}):
            if not any(gleiche_firma(n, x) for x in p['unternehmen']):
                p['unternehmen'].append(n)
        verorten(fahrten, index, e.get('ort', ''))
        p['fahrten'] = fahrten
        if erg.get('fehler'):
            p['status'], p['fehler'] = 'nicht_lesbar', erg['fehler']
        elif fahrten:
            p['status'] = 'ok' if p.get('abruf') != 'vorrat' else 'vorrat'
        else:
            p['status'] = 'kein_text' if not erg.get('woerter') and p.get('abruf') != 'vorrat' else 'keine_fahrten'
        plaene.append(p)
    # Verläufe
    for p in plaene:
        linien, alle_gerade, muster, zu = [], True, {}, []
        for fi, f in enumerate(p.get('fahrten') or []):
            pts = [(l[0], l[1]) for l in (f.get('lage') or []) if l]
            if len(pts) < 2:
                continue
            k = ';'.join('%.5f,%.5f' % q for q in pts)
            if k in muster:
                zu[muster[k]].append(fi)     # 1.1: gleicher Weg → dieselbe Linie (Website: gleiche Farbe)
                continue
            muster[k] = len(zu)
            zu.append([fi])
            weg = skizze(pts) if skizze and (ende is None or time.time() < ende) else None
            if weg:
                alle_gerade = False
                linien.append([[round(x, 6), round(y, 6)] for x, y in weg])
            else:
                linien.append([[round(x, 6), round(y, 6)] for x, y in pts])
        if not linien:
            continue
        halte_n = sum(len(f['halte']) for f in p['fahrten'])
        verortet = sum(1 for f in p['fahrten'] for l in f.get('lage') or [] if l)
        p['verortet'] = [verortet, halte_n]
        feats.append({'type': 'Feature', 'geometry': {'type': 'MultiLineString', 'coordinates': linien},
                      'properties': {'ref': str(p['id']), 'id': p['id'], 'name': p.get('beschreibung') or p.get('schule', ''), 'schule': p.get('schule', ''),
                                     'ort': p.get('ort', ''), 'verlauf': 'haltestellen' if alle_gerade else 'skizze', 'fahrten': zu}})
    if cache_pfad:
        try:
            with open(cache_pfad, 'w', encoding='utf-8') as fh:
                json.dump(neu_cache, fh, ensure_ascii=False, separators=(',', ':'))
        except OSError:
            pass
    zahl = {'plaene': len(plaene), 'gelesen': sum(1 for p in plaene if p.get('status') == 'ok'),
            'fahrten': sum(len(p.get('fahrten') or []) for p in plaene),
            'halte': sum(len(f['halte']) for p in plaene for f in p.get('fahrten') or []),
            'verortet': sum(1 for p in plaene for f in p.get('fahrten') or [] for l in f.get('lage') or [] if l),
            'skizziert': sum(1 for f in feats if f['properties']['verlauf'] == 'skizze')}
    log('Schülerverkehr: %(plaene)d Fahrpläne, %(gelesen)d ausgelesen, %(fahrten)d Fahrten, %(verortet)d von %(halte)d Halten verortet, %(skizziert)d über die Straßen' % zahl)
    return {'type': 'FeatureCollection', 'version': VERSION, 'erzeugt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'landkreis': 'Hildesheim', 'attribution': 'Fahrpläne: Schulen, Unternehmen und Landkreis Hildesheim; Haltestellen: DELFI e.V. über GTFS.DE (CC BY 4.0)',
            'zahl': zahl, 'plaene': plaene, 'features': feats}


def quellen_laden(url_oder_pfad, log=print):
    """Liste der Fahrpläne aus der Verwaltung (_data/verbund-tarife/schulverkehr-quellen.json)"""
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
        log('Schülerverkehr: Liste der Fahrpläne nicht geladen –', ex)
        return []
    e = d.get('plaene') if isinstance(d, dict) else d
    return [x for x in (e or []) if isinstance(x, dict) and x.get('id')]
