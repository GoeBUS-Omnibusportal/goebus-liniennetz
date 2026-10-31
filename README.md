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

- **GitHub** erzeugt die Daten jeden Montag neu.
- **Die Website** holt sie ab, sobald ihre Kartendateien älter als 7 Tage sind. Angestoßen wird das beim nächsten Besuch auf „Verbund & Tarife“, im Hintergrund und höchstens alle 6 Stunden.
- **Sofort abholen:** Im Selbsttest (`_werkzeuge/pruefen.php`) steht, wie die Dateien sofort abgeholt werden.

**Optional:** Wer das Abholen unabhängig von Besuchern möchte, lässt einen kostenlosen Dienst wie cron-job.org einmal pro Woche diese Adresse aufrufen: `https://www.goebus.de/verbund-tarife/abgleich.php`.

## Anpassen

- **Verbünde und Landkreise:** in `verbuende.json`. Die Werte entsprechen der Einstellung in `_includes/verbund-gebiete.php` der Website.
- **Einzelne Linien ausblenden**, die es nicht mehr gibt: weiterhin auf der Website in `_includes/verbund-gebiete.php` bei `linien_aus`.
- **Zum Testen auf dem eigenen Rechner:** `python liniennetz.py`. Mit `--ohne-osm` geht es schneller, dann gibt es nur Haltestellen-Verläufe.

## Quellenangabe

Die Kartendateien enthalten die nötige Quellenangabe. Die Website zeigt sie unten auf jeder Netzkarte an: „Fahrplandaten: DELFI e.V. über GTFS.DE (CC BY 4.0); Linienverläufe: OpenStreetMap-Mitwirkende (ODbL)“.
