# GöBUS – Liniennetze automatisch aktuell halten

Dieser Ordner erzeugt jede Woche die Liniennetze der Verbünde für die GöBUS-Website. Die Daten kommen aus zwei Quellen:

- **Vollständigkeit und Texte** aus den amtlichen Fahrplandaten (GTFS): alle Linien mit Nummer, Betreiber, Start und Ziel. Die Datei stammt von GTFS.DE, erzeugt aus den Daten von DELFI e.V., unter der Lizenz CC BY 4.0.
- **Straßengenaue Verläufe** aus OpenStreetMap, wenn dort eine Linie mit derselben Nummer dicht an den Haltestellen verläuft. Sonst wird die Linie über die Haltestellen gezogen.

Das STRATO-Paket „Homepage Starter“ hat keine Cronjobs. Die Rechenarbeit übernimmt deshalb GitHub, kostenlos und automatisch. Die Website holt sich danach nur die fertigen Dateien ab.

## Einrichtung (einmalig, etwa 10 Minuten)

1. **GitHub-Konto anlegen** (kostenlos) auf github.com.
2. **Neues Repository erstellen**: Oben rechts auf „+“ klicken, dann „New repository“ wählen. Als Name zum Beispiel `goebus-liniennetz` eintragen, „Public“ wählen und auf „Create repository“ klicken.
   „Public“ ist nötig, damit die Website die fertigen Dateien ohne Passwort abholen kann. Im Repository liegen nur öffentliche Fahrplan- und Kartendaten, keine Zugangsdaten.
3. **Dateien hochladen**: Im neuen Repository auf „uploading an existing file“ klicken und den **Inhalt** dieses Ordners hineinziehen. Dazu gehören `liniennetz.py`, `verbuende.json`, `landkreise.topo.json`, `.gitignore`, `README.md` und der Ordner `.github`. Danach auf „Commit changes“ klicken.
   Falls der Ordner `.github` beim Hochladen fehlt (versteckter Ordner): Auf „Add file“ und dann „Create new file“ klicken, als Namen `.github/workflows/liniennetz.yml` eingeben und den Inhalt der gleichnamigen Datei hineinkopieren.
4. **Schreibrecht für den Ablauf erlauben**: Unter „Settings“ → „Actions“ → „General“ → „Workflow permissions“ die Option „Read and write permissions“ wählen und speichern.
5. **Ersten Lauf starten**: Unter „Actions“ → „Liniennetz aktualisieren“ auf „Run workflow“ klicken. Nach etwa 5 bis 15 Minuten liegt im Repository der Ordner `daten` mit `vsn_linien.json`, `nvv_linien.json` usw.
6. **Website verbinden**: In die `config.php` der Website eintragen und dabei `NAME` durch den eigenen GitHub-Namen ersetzen:
   ```php
   define('GB_LINIEN_QUELLE', 'https://raw.githubusercontent.com/NAME/goebus-liniennetz/main/daten/');
   ```

Von da an läuft alles automatisch:

- **GitHub** erzeugt die Daten montags und donnerstags neu – und sofort nach jedem Hochladen von `liniennetz.py` oder `verbuende.json`.
- **Die Website** prüft höchstens alle 6 Stunden die kleine Datei `daten/stand.json` und übernimmt eine neue Auswertung sofort (bis Update 23 erst, wenn die eigenen Dateien älter als 7 Tage waren – neue Ergebnisse kamen dadurch bis zu einer Woche später an).
- **Sofort abholen:** in der Verwaltung unter *Verbund & Tarife → Abgleich der Liniennetze → Jetzt abholen*.

**Optional:** Wer das Abholen unabhängig von Besuchern möchte, lässt einen kostenlosen Dienst wie cron-job.org einmal pro Woche diese Adresse aufrufen: `https://www.goebus.de/verbund-tarife/abgleich.php`.

## Neu in Version 2.0 (Update 24): mehr straßengenaue Verläufe

- **Linienwege aus den Fahrplandaten:** Enthält die GTFS-Datei `shapes.txt`, wird der Linienweg jeder Fahrt übernommen, sofern er zu den Haltestellen passt (`verlauf: "fahrplan"`). Er hat Vorrang vor OpenStreetMap.
- **Nummern vereinheitlicht:** „Bus 30“, „Linie 30“, „30“ und Listen wie „30;31“ in OpenStreetMap passen jetzt zu Linie 30 der Fahrplandaten. Bisher wurden nur exakt gleiche Nummern gefunden.
- **Mehrere Overpass-Server:** Ist overpass-api.de ausgelastet oder bricht die Antwort ab, werden weitere öffentliche Server gefragt. Bisher bekam ein ganzer Verbund in diesem Fall nur Haltestellen-Verläufe.
- **Verlauf der Vorwoche:** Ist OpenStreetMap trotzdem nicht erreichbar, bleibt der straßengenaue Verlauf der Vorwoche (wenn die Haltestellen weiter dazu passen). Es werden also nie weniger Verläufe als zuvor.
- **Prüfung der Daten:** `bericht.json` und `abgleich.md` nennen je Verbund Linien ohne Betreiber, mit weniger als drei Halten, gleiche Nummern bei mehreren Betreibern und Linien ohne straßengenauen Verlauf; `stand.json` nennt Skriptversion, Overpass-Ergebnis und die Zahl der Verläufe je Quelle.

## Was die Auswertung macht (ab Update 20)

- **Alle Fahrtmuster einer Linie:** Nicht nur die längste Fahrt, sondern bis zu acht unterschiedliche Haltestellenfolgen je Linie (Zweige, Schleifen, Schulfahrten, Ortsdurchfahrten). Dieselbe Linie mit mehreren Einträgen in den Fahrplandaten wird zusammengeführt.
- **Straßengenauer Verlauf mit Lückenfüllung:** Alle passenden OpenStreetMap-Linien derselben Nummer werden übernommen (Hin- und Rückrichtung, Zweige). Abschnitte, die OpenStreetMap nicht abdeckt, bleiben als Verbindung der Haltestellen erhalten (`verlauf: "osm+haltestellen"`).
- **Gedächtnis für Ferien:** Die freien Fahrplandaten gelten nur für sieben Tage. Linien, die in einer Woche nicht fahren (z. B. Schulfahrten in den Herbstferien), bleiben bis zu acht Wochen aus der Vorwoche erhalten (`--behalten-tage 56`).
- **Linien nach Betreiber:** In `verbuende.json` kann je Verbund `"betreiber"` stehen. Linien dieser Betreiber gehören immer zum Verbund, auch wenn sie überwiegend außerhalb verkehren (für RVHI eingetragen: Linien nach Hannover, Peine, Salzgitter usw.).
- **Bericht:** `daten/bericht.json` listet je Verbund alle Linien mit Betreiber, Start, Ziel, Art des Verlaufs und Datum der letzten Fahrt. Die Website holt den Bericht mit ab; der Selbsttest zeigt ihn an.

## So holen Sie das Maximum heraus

1. **Nach dem ersten Lauf den Selbsttest öffnen** (Abschnitt „Liniennetz und Unternehmen“). Dort steht, welche Betreiber in den Fahrplandaten vorkommen und welchem Unternehmen sie zugeordnet wurden.
2. **Betreiber ohne Zuordnung eintragen:** in der `config.php` der Website, z. B. `define('GB_BETREIBER_ZUORDNUNG', [12 => ['Regionalverkehr Hildesheim GmbH']]);` (Zahl = ID des Unternehmens). Danach zeigen die Unternehmensprofile die Linien dieses Betreibers auf der Karte.
3. **Liniennummern im CMS so schreiben wie im Fahrplan** (z. B. „30“, nicht „Linie 30“) und das Feld „Verbund“ ausfüllen. Dann bekommen CMS-Linien ohne gezeichneten Verlauf automatisch den Verlauf aus diesen Daten.
4. **Fehlende Verläufe in OpenStreetMap ergänzen:** Linien mit `verlauf: "haltestellen"` im Bericht haben in OpenStreetMap noch keine Route mit derselben Nummer. Wer dort eine Route anlegt oder die Nummer (`ref`) korrigiert, bekommt ab der nächsten Woche den straßengenauen Verlauf.
5. **Vollständiger Fahrplanzeitraum:** Die freien Daten von GTFS.DE gelten sieben Tage. Den gesamten Fahrplanzeitraum enthält der Datensatz von DELFI e.V. auf opendata-oepnv.de (kostenlose Anmeldung nötig) oder das erweiterte Angebot von GTFS.DE. Eine solche Datei lässt sich mit `--gtfs datei.zip` bzw. `--gtfs-url` verwenden.

## Abgleich: Sind die Daten vollständig?

Jeder Lauf schreibt `daten/abgleich.md` (auf GitHub direkt lesbar) und den Abschnitt `abgleich` in `daten/bericht.json`. Die Website zeigt beides im Selbsttest und in der Verwaltung unter *Verbund & Tarife → Abgleich der Liniennetze* (dort zusätzlich der Vergleich mit den Linien im CMS). Geprüft wird je Verbund:

- **verschwunden** – Linien der Vorwoche, die diese Woche nicht in den Fahrplandaten stehen (sie bleiben vorerst sichtbar).
- **neu** – Linien, die erstmals in den Fahrplandaten auftauchen.
- **nur in OpenStreetMap** – OSM-Routen im Gebiet, deren `network`-Angabe zum Verbund passt, die in den Fahrplandaten fehlen.
- **nur beim Verbund** – Liniennummern aus den Linienübersichten des Verbunds (`"quellen"`), die in den Fahrplandaten fehlen.
- **laut Soll-Liste fehlend** – Liniennummern aus einer gepflegten Soll-Liste (`"soll"`), die fehlen.

Beispiel für `verbuende.json` (Einträge ergänzen, nicht ersetzen):

```json
{ "slug": "rvhi", "kurz": "RVHI", "kreise": ["03254"], "datei": "rvhi_linien.json",
  "betreiber": ["Regionalverkehr Hildesheim", "RVHI"],
  "osm_netze": ["Regionalverkehr Hildesheim"],
  "soll": ["30", "31", "32"],
  "quellen": [{ "url": "https://www.beispiel-verbund.de/linien/", "muster": "Linie\\s*(\\d{1,3})" }] }
```

`quellen` liest die angegebene Seite (nur https) und sucht Liniennummern mit dem regulären Ausdruck `muster` (Vorgabe: „Linie 30“, „Bus 30“). Eine Soll-Liste ist der sicherste Weg: Sie wird einmal aus dem Fahrplanbuch des Verbunds übernommen und bei Fahrplanwechseln angepasst.

## Anpassen

- **Verbünde und Landkreise:** in `verbuende.json`. Die Werte entsprechen der Einstellung in `_includes/verbund-gebiete.php` der Website.
- **Einzelne Linien ausblenden**, die es nicht mehr gibt: weiterhin auf der Website in `_includes/verbund-gebiete.php` bei `linien_aus`.
- **Zum Testen auf dem eigenen Rechner:** `python liniennetz.py`. Mit `--ohne-osm` geht es schneller, dann gibt es nur Haltestellen-Verläufe.

## Quellenangabe

Die Kartendateien enthalten die nötige Quellenangabe. Die Website zeigt sie unten auf jeder Netzkarte an: „Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0); Linienverläufe: OpenStreetMap-Mitwirkende (ODbL)“.
