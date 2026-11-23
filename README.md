# GöBUS – Liniennetze automatisch aktuell halten

Dieser Ordner erzeugt jeden Tag die Liniennetze der Verbünde für die GöBUS-Website. Die Daten kommen aus zwei Quellen:

- **Vollständigkeit und Texte** aus den amtlichen Fahrplandaten (GTFS): alle Linien mit Nummer, Betreiber, Start und Ziel. Die Datei stammt von GTFS.DE, erzeugt aus den Daten von DELFI e.V., unter der Lizenz CC BY 4.0.
- **Straßengenaue Verläufe** aus OpenStreetMap, wenn dort eine Linie mit derselben Nummer dicht an den Haltestellen verläuft. Sonst wird die Linie über die Haltestellen gezogen.

Das STRATO-Paket „Homepage Starter“ hat keine Cronjobs. Die Rechenarbeit übernimmt deshalb GitHub, kostenlos und automatisch. Die Website holt sich danach nur die fertigen Dateien ab.

## Einrichtung (einmalig, etwa 10 Minuten)

1. **GitHub-Konto anlegen** (kostenlos) auf github.com.
2. **Neues Repository erstellen**: Oben rechts auf „+“ klicken, dann „New repository“ wählen. Als Name zum Beispiel `goebus-liniennetz` eintragen, „Public“ wählen und auf „Create repository“ klicken.
   „Public“ ist nötig, damit die Website die fertigen Dateien ohne Passwort abholen kann. Im Repository liegen nur öffentliche Fahrplan- und Kartendaten, keine Zugangsdaten.
3. **Dateien hochladen**: Im neuen Repository auf „uploading an existing file“ klicken und den **Inhalt** dieses Ordners hineinziehen. Dazu gehören `liniennetz.py`, `fahrschulen.py`, `fahrplanseiten.py`, `verbuende.json`, `landkreise.topo.json`, `osrm-bus.lua`, `.gitignore`, `README.md` und der Ordner `.github`. Danach auf „Commit changes“ klicken.
   Falls der Ordner `.github` beim Hochladen fehlt (versteckter Ordner): Auf „Add file“ und dann „Create new file“ klicken, als Namen `.github/workflows/liniennetz.yml` eingeben und den Inhalt der gleichnamigen Datei hineinkopieren.
4. **Schreibrecht für den Ablauf erlauben**: Unter „Settings“ → „Actions“ → „General“ → „Workflow permissions“ die Option „Read and write permissions“ wählen und speichern.
5. **Ersten Lauf starten**: Unter „Actions“ → „Liniennetz aktualisieren“ auf „Run workflow“ klicken. Nach etwa 5 bis 15 Minuten liegt im Repository der Ordner `daten` mit `vsn_linien.json`, `nvv_linien.json` usw.
6. **Website verbinden**: In die `config.php` der Website eintragen und dabei `NAME` durch den eigenen GitHub-Namen ersetzen:
   ```php
   define('GB_LINIEN_QUELLE', 'https://raw.githubusercontent.com/NAME/goebus-liniennetz/main/daten/');
   ```

Von da an läuft alles automatisch:

- **GitHub** erzeugt die Daten jeden Tag um 3:23 Uhr (UTC) neu (seit Fassung 60, vorher montags und donnerstags; seit Fassung 61 mit Ersatzquelle und eigenem Routenplaner) – und sofort nach jedem Hochladen von `liniennetz.py` oder `verbuende.json`.
- **Die Website** prüft höchstens alle 6 Stunden die kleine Datei `daten/stand.json` und übernimmt eine neue Auswertung sofort (bis Update 23 erst, wenn die eigenen Dateien älter als 7 Tage waren – neue Ergebnisse kamen dadurch bis zu einer Woche später an).
- **Sofort abholen:** in der Verwaltung unter *Verbund & Tarife → Abgleich der Liniennetze → Jetzt abholen*.

**Optional:** Wer das Abholen unabhängig von Besuchern möchte, lässt einen kostenlosen Dienst wie cron-job.org einmal pro Woche diese Adresse aufrufen: `https://www.goebus.de/verbund-tarife/abgleich.php`.

## Neu in Fassung 67 (Skript 3.7, schulverkehr.py 1.0)

- **Hochladen:** `liniennetz.py`, **`schulverkehr.py` (neu)**, `README.md` und `.github/workflows/liniennetz.yml`.
- **Freigestellter Schülerverkehr im Landkreis Hildesheim:**
  - Der Lauf holt die Liste der Fahrpläne aus der Verwaltung (`_data/verbund-tarife/schulverkehr-quellen.json` neben den Regeln; oder `GOEBUS_SCHULVERKEHR`).
  - Jeder Fahrplan (PDF) wird geladen und mit pdfplumber ausgelesen: Einsatzpläne („Fahrt-Nr. 109; Fahrplan 2245 H“, Spalten „Abfahrt | Haltestelle“, mehrere Kästen), Listen („7.45 Uhr Glashütte“) und Tabellen (Haltestellen als Zeilen, Fahrten als Spalten; „I“ = fährt durch, Spalten „Linie 67“ gehören zum Liniennetz und werden nur vermerkt).
  - Haltestellen werden über die Fahrplandaten (alle Haltestellen um den Landkreis) und OpenStreetMap (`osm/halte.geojsonseq`: Haltestellen mit dem nächsten Ort davor) verortet: genau, ähnlich oder nur ungefähr im Ort; mehrdeutige Namen über die Nachbarhalte, unplausible Sprünge (schneller als die Fahrzeit erlaubt) werden verworfen.
  - Die Fahrten legt der eigene Routenplaner über die Straßen (Zeitbudget `GOEBUS_SCHUL_MINUTEN`, Vorgabe 8 Minuten).
  - Ergebnis: `daten/schulverkehr_hi.json` (Fahrpläne mit Fahrten, Halten, Zeiten, Unternehmen, Status; Karte als GeoJSON), Zwischenspeicher `daten/schulverkehr-cache.json` (unveränderte PDFs werden nicht neu ausgelesen; ist ein PDF nicht abrufbar, gilt der letzte gelesene Stand).
- **Workflow:** pdfplumber wird installiert; aus dem OpenStreetMap-Auszug kommen zusätzlich Haltestellen und Orte (auch im Vorrat für spätere Läufe).

## Neu in Fassung 66 (Skript 3.6)

- **Hochladen:** `liniennetz.py` und `README.md` ersetzen.
- **Gedächtnis:** Eine Linie, die laut Linienlisten zu einem Nachbarverbund gehört, wird nicht mehr als „aus der Vorwoche“ zurückgeholt. Beispiel: Skript 3.4 gab VSN 51 an die Öffis ab, das Gedächtnis holte sie in den VSN zurück. Die Regel gilt auch für Linien, die diese Woche nicht in den Fahrplandaten stehen; dann zählen Start, Ziel und Name der gespeicherten Linie. Eigene Unternehmen sind ausgenommen. Im Protokoll steht „aus der Vorwoche nicht behalten – gehört …“.

## Neu in Fassung 65 (Skript 3.5)

- **Hochladen:** `liniennetz.py`, `README.md` und `.github/workflows/liniennetz.yml` ersetzen.
- **Geofabrik:** Die Dateien liegen jetzt in Unterordnern, zum Beispiel `europe/germany/niedersachsen/niedersachsen-latest.osm.pbf`. Der alte Pfad antwortet mit 404; am 22.11.2026 für alle vier Gebiete geprüft. Prüfung und Download nutzen den neuen Pfad, der Download mit Prüfsumme.
- **Vorrat:** Der Lauf nimmt den OpenStreetMap-Auszug planmäßig aus dem Zwischenspeicher, wenn er jünger als 2 Tage ist. Das erscheint jetzt als Hinweis statt als Warnung und steht in `stand.json` als `osm_planmaessig`. Der Zwischenspeicher wird nur bei einem neuen Auszug gespeichert.
- **Skizzen:** Linien aus der Vorwoche (`aus_vorwoche`) tragen keine Eigenschaft `luecken`. Dann gelten gerade Verbindungen zweier Haltestellen ab 150 m als Lücken und werden skizziert. Beispiel: VSN 110 mit drei solchen Stücken; sie fehlt seit dem 13.11. in den Fahrplandaten. Bisher blieben solche Linien bis zu vier Wochen gerade.

## Neu in Fassung 64 (Skript 3.4)

- **Hochladen:** `liniennetz.py`, `README.md` und `.github/workflows/liniennetz.yml` ersetzen.
- **Linie eines Nachbarverbunds:** Steht eine Linie nicht in der vollständigen Liste des eigenen Verbunds (mindestens 30 Einträge), aber in der Liste eines Nachbarverbunds mit passendem Linienweg, gehört sie dorthin. Beispiel: VSN 51 Bodenwerder – Hameln ist die Öffis-Linie 51. Linien eigener Unternehmen sind ausgenommen. Im Protokoll steht „nicht übernommen – gehört zu …“.
- **Schultage:** Fahrten je Linie am Stichtag und an einem Ferien-Mittwoch (Vorgabe `2027-01-06,2027-03-24,2027-03-31`, änderbar über `GOEBUS_FERIENTAGE`). Gezählt wird der erste Tag, den die Fahrplandaten noch abdecken (mindestens 30 % der Fahrten des Stichtags). Die Ergebnisse stehen in den Eigenschaften `schultage` (`nur` / `weniger`), `ferientag`, `fahrten_schultag` und `fahrten_ferientag`, außerdem in `stand.json` → `schultage`.
- **Linienweg:** Eine Haltestelle ohne Ort zwischen Halten mit Ort zählt zu deren Ort, wenn die Gemeinde passt (VSN 51: „Marienthaler Straße“ vor „Hameln, Bahnhof“).
- **Quellen prüfen:** Jede Quelle hat jetzt eine Rolle (`haupt`, `ersatz` oder `reserve`). Die Overpass-Server sind Reserve und werden höchstens 10 s lang geprüft statt 30 s.

## Neu in Fassung 63 (Skript 3.3)

- **Hochladen:** `liniennetz.py`, `osrm-bus.lua` und `.github/workflows/liniennetz.yml` ersetzen. Der erste Lauf baut den Routenplaner einmal neu (Schlüssel `osrm-bus-v2`, etwa 2 Minuten).
- **Zwischenspeicher:** `actions/cache@v5` (Node 24 statt Node 20). Die Dateien des Routenplaners gehören nach dem Bau dem Lauf (`sudo chown`) – vorher scheiterte das Speichern an `region.osrm.fileIndex: Permission denied`.
- **Skizzen:** Das Bus-Profil sperrt nur noch Durchfahrtshöhen unter 3,2 m; Gewichtsgrenzen unter 18 t und Höhen bis 3,6 m werden gemieden, nicht gesperrt (Ortsdurchfahrten wie Ertinghausen, VSN 222). Wird ein Stück als Umweg verworfen, rechnet das Skript es Haltestelle für Haltestelle neu – erst mit dem eigenen Routenplaner, sonst mit dem öffentlichen (höchstens `GOEBUS_SKIZZE_OEFFENTLICH_MAX` Anfragen, Vorgabe 400). Gerade bleibt nur ein Abschnitt ohne jeden Weg.
- **Start und Ziel mit Ort:** Nennt der Name der Start- oder Endhaltestelle keinen Ort, steht die Gemeinde davor (Gemeindegrenzen aus OpenStreetMap, `osm/gemeinden.geojsonseq`). Liegt daneben eine gleichnamige Haltestelle mit Ort, gilt deren Name: „Göttingen, Bahnhof/ZOB“ statt „Bahnhof Pause“, „Hameln, Ostertorwall“. Stadtlinien zeigen Start und Ziel immer mit Ort. Ohne Gemeindegrenzen wird nichts geraten. Zusätze wie „Pause“, „(Ausstieg)“, „Mast 1 W“ fallen weg.
- **Quellen prüfen:** Die Website wird unter derselben Adresse geprüft, die das Skript nutzt (auch wenn `GOEBUS_ADRESSE` auf `/_admin/` endet); gezeigt wird der echte HTTP-Code.

## Neu in Fassung 62 (Skript 3.2, Korrekturen nach dem ersten Lauf mit eigenem Routenplaner)

- Die IPv4-Umhüllung der Namensauflösung wird nur einmal angelegt, auch wenn das Modul mehrfach geladen wird (Prüfumgebung). Im Lauf ändert sich nichts.
- **AST mit festen Haltestellen (VSN 9130–9220):** Die Fahrplandaten führen sie über Haltestellen wie „Göttingen Bahnhof/ZOB Fernbusse, Mast 1 W (AST)“. Das „(AST)“ im Namen ließ die Linien beim Aufräumen ganz herausfallen, und weil es sie in den Fahrplandaten gab, bekamen sie auch kein Gebiet. Steht die Nummer in der offiziellen Liste, entscheidet der Haltestellenname jetzt nicht mehr (Skript und Karte). Gibt es keine Fahrplan-Linie, kommt das Gebiet (Gemeindefläche oder Hülle der Haltestellen).
- **Quellen prüfen:** Die Website wird so geprüft, wie das Skript sie abruft (Python, gleiche Kennung). curl bekam am 22.11. ein 404, das Skript die Regeln. `ghcr.io` antwortet ohne Anmeldung mit 401; das gilt als erreichbar.

## Neu in Fassung 61 (Skript 3.2)

- **Quellen prüfen:** Der Lauf prüft zuerst von GitHub aus, welche Quellen antworten: Geofabrik, openstreetmap.fr, drei Overpass-Server, öffentlicher Routenplaner, gtfs.de, ghcr.io und die Website. Das Ergebnis steht als Tabelle im Protokoll, in `stand.json` (`quellen`) und in der Verwaltung.
- **Geofabrik gesperrt:** Am 19. und 22.11.2026 lehnte Geofabrik jede Verbindung von GitHub ab („Couldn't connect to server“, auch über IPv4). Overpass sperrt Azure/AWS seit Oktober 2025. Der Ersatzserver der 60. Fassung half nicht: openstreetmap.fr bietet von den deutschen Ländern nur NRW an.
  - Jetzt lädt der Lauf **Deutschland von openstreetmap.fr**: 6 GB, mit Prüfsumme. Daraus schneidet er die Region aus (`osmium extract -b 8.2,50.6,11.8,52.9`).
  - Das geschieht höchstens alle 2 Tage. Dazwischen gilt der letzte gute Auszug aus dem Zwischenspeicher.
  - `stand.json` meldet dann `osm_quelle: openstreetmap.fr`.
- **Eigener Routenplaner** (`osrm-bus.lua`, Schritt „Eigenen Routenplaner bauen und starten“):
  - OSRM läuft in Docker (`ghcr.io/project-osrm/osrm-backend:v26.10.0-debian`) auf den Straßen der Region.
  - Gebaut wird er einmal je Woche. Die fertigen Daten liegen im Zwischenspeicher von GitHub.
  - Das Skript fragt `http://127.0.0.1:5000` ohne Wartezeit, also sind alle Skizzen in einem Lauf fertig.
  - Fällt er aus, nimmt das Skript den öffentlichen Server (`skizze_wechsel()`). `stand.json` nennt das unter `skizze_router`.
- **Bus-Profil:** Busschleusen und Busspuren sind befahrbar, `bus=no` ist gesperrt. Straßen unter 18 t oder 3,6 m sind gesperrt, außer sie sind für Busse freigegeben. Feld- und Fußwege gibt der Auszug gar nicht erst her.
- **Bediengebiet ohne Gemeindefläche:** Fehlt die Gemeindegrenze, bildet `gebiet_aus_haltestellen()` eine Hülle um alle Haltestellen des Orts, mit 500 m Rand (`gebiet_quelle: haltestellen`). Die Karte nennt das Gebiet „ungefähr“.

## Neu in Fassung 60 (Skript 3.1)

- **OpenStreetMap-Ausfall abgefangen:** Am 19.11.2026 lehnte Geofabrik alle Downloads ab (`curl: (7) Failed to connect to download.geofabrik.de`), auch Overpass antwortete nicht – der Lauf hatte keine einzige OpenStreetMap-Linie. Jetzt: vier Wiederholungen je Land, danach IPv4 (`curl -4`), danach der Spiegel `download.openstreetmap.fr`. Klappt nichts, nimmt der Lauf den zuletzt gelungenen Auszug aus dem GitHub-Zwischenspeicher (`actions/cache`, Ordner `osm-vorrat`); `stand.json` meldet dann `osm_quelle: vorrat` und `osm_stand`.
- **Täglich** um 3:23 Uhr statt zweimal die Woche. Gemeindegrenzen kommen im selben osmium-Durchgang wie Buslinien und Fahrschulen.
- **Eine Fahrplan-Linie, mehrere Orte:** „Linie 1“ von NVV - Faisy ist in den Fahrplandaten *eine* route_id für fünf Städte. Bisher kamen nur die 16 längsten Fahrtmuster weiter (aus ein bis zwei Orten), StadtBus Witzenhausen 1 (281) oder Korbach 1–5 (581–585) fehlten. Jetzt werden die Fahrten je Ort ausgewertet (`orte_der_fahrten()`, Raster rund 4 km); Hin- und Rückfahrt einer Überlandlinie führt `zusammenfassen()` wieder zusammen. `stand.json`: `orte_geteilt`.
- **Linienwege ohne eindeutigen Ort** (NVV 23 „Wilhelmshöhe (Park) <> Dönche <> Druseltal <> Herkules“): Gibt es keinen eindeutigen Namen als Anker, probiert `halte_aus_weg()` jeden Kandidaten des seltensten Namens und nimmt die kürzeste Kette (höchstens 15 km je Abschnitt).
- **Gleiche Nummer:** Ein Eintrag fällt schon unter 95 % im Verbund weg, wenn ein anderes Unternehmen dieselbe Nummer ganz im Verbund fährt (VRB 262 Harzer ab Braunlage neben Wolfsburg). Passt ein Eintrag zum Linienweg der Liste, gilt für die übrigen ebenfalls 95 % (vorher 75 %).
- **Ersetzt-Regeln:** Zeigen zwei alte Nummern auf eine neue (ROSA/RVHI 5 und 43 → 5/43), werden beide Verläufe zusammengelegt; bisher überschrieb der zweite den ersten. Der Verlauf einer alten Nummer wird außerdem nur übernommen, wenn er die Haltestellen der neuen abfährt; eine eigene Skizze der neuen Nummer zählt als eigener Verlauf.
- **Skizzen:** Antwortet der Routenplaner mit HTTP 400 („kein Weg“), bleibt nur dieses Stück gerade – bisher brach das alle weiteren Skizzen des Laufs ab. Nach allen Verbünden nutzt ein Nachlauf die restliche Zeit; `stand.json` nennt je Verbund `skizzen_offen`.

## Neu in Fassung 59 (Skript 3.0)

- **Orte trennen:** Gleiche Nummer und gleicher Betreiber heißt nicht gleiche Linie („NVV - Faisy“ fährt die 1 in Bad Wildungen, Eschwege, Witzenhausen, Bad Hersfeld und Korbach). Zusammengeführt wird nur, was höchstens 4 km auseinanderliegt.
- **Stadtbusse:** Nennt die offizielle Liste „StadtBus Eschwege · Linie 1“ (NVV 271), bekommt die Linie 1 in Eschwege die Nummer 271 (`ref_fahrplan` behält die alte). Aus der Vorwoche kommt die alte Nummer nicht zurück.
- **Linienwege der Listen** mit „<>“, „>“ oder „→“ werden jetzt in Haltestellen zerlegt (bisher nur „–“).
- **AST-Gebiete:** „Gemeinde Rosdorf“, „Samtgemeinde Radolfshausen“, „Flecken Bovenden“ … bekommen die Gemeindefläche aus OpenStreetMap (`--gemeinden osm/gemeinden.geojsonseq`, im Workflow aus den Geofabrik-Auszügen; verlauf „gebiet“). Fehlt die Datei, läuft alles wie bisher.
- **Gleiche Nummer ohne passenden Linienweg:** Fährt ein Eintrag ganz im Verbund (mind. 90 %), fallen Einträge mit derselben Nummer weg, die überwiegend außerhalb fahren (unter 60 %) – VRB 264/265 (Harzer ab Braunlage), 803 (PVG ab Wittingen).
- **Werksverkehre** mit Modellnamen (Golf, Tiguan/Tigua, Touran/Toura, Tora …) sind keine Linien.
- **Skizzen:** bis zu 45 Minuten je Lauf (`GOEBUS_SKIZZE_MINUTEN`, als Variable in GitHub änderbar), gerecht auf die Verbünde verteilt. Zeitlimit des Schritts 70, des Auftrags 110 Minuten (`.github/workflows/liniennetz.yml`).

## Neu in Fassung 58 (Skript 2.9, Ergänzung)

- **Lücken skizzieren:** Auch gemischte Verläufe („osm+haltestellen“, „fahrplan+haltestellen“) werden vervollständigt: Die geraden Verbindungen stehen am Ende der Geometrie (Anzahl in `properties.luecken`) und werden über die Straßen gelegt → „osm+skizze“ bzw. „fahrplan+skizze“. Alle Teile einer Linie, nicht mehr höchstens vier. Eine Linie wird erst umgestellt, wenn alle Lücken berechnet sind; Berechnetes liegt in `daten/skizzen.json`.
- Zeitbudget für Skizzen jetzt **20 Minuten** je Lauf (`GOEBUS_SKIZZE_MINUTEN`), der Schritt „Fahrplandaten und OpenStreetMap verarbeiten“ bleibt damit unter seinen 35 Minuten. Bei einem Fehler des Servers (z. B. 429) hört das Skript für diesen Lauf auf.
- Gerade bleibt nur: kein Weg gefunden, Weg länger als das 2,5-Fache plus 2 km, Stück kürzer als 150 m, oder in der Verwaltung verworfen.

## Neu in Fassung 57 (Skript 2.9)

- **Gleiche Nummer, anderes Unternehmen:** Steht eine Nummer in der offiziellen Linienliste und passt mindestens ein Eintrag zu deren Linienweg, fallen die übrigen Einträge mit dieser Nummer weg – wenn sie zu mehr als 25 % außerhalb des Verbunds fahren oder ihr Betreiber einen anderen Verbund nennt. Beispiele: Wartburgmobil 100/172/180/186 im NVV, Paderborn/Höxter 502/504/523, „NVV - Faisy“ 33/42/71/72 im VSN. Linien im Verbund mit gleicher Nummer bleiben (KVG-Tram 4 und StadtBus Eschwege 4). Die Prüfung läuft vor dem Sammeln der Haltestellen und noch einmal nach dem Übernehmen aus der Vorwoche; `stand.json` nennt je Verbund `gleiche_nummer_entfernt`.
- **Fernbusse** (FlixBus u. a.) werden auch aus der Vorwoche nicht mehr behalten.

## Fassung 56 (Skript 2.8, Ergänzung)

- „ALT“ und „AST“ (Anruf-Linien-Taxi, Anruf-Sammel-Taxi) zählen nur noch in Großbuchstaben – Linien nach „Alt Wolfsburg“, „Alt Drispenstedt“ oder „Alt Wallmoden“ fielen vorher heraus.
- In der Verwaltung verworfene Skizzen (`skizze_aus` in `linien-regeln.json`) skizziert das Skript nicht mehr.

## Neu in Fassung 55 (Skript 2.8): Linienweg aus dem Fahrplan, Skizzen, keine Fernbusse

- `weg_fp` je Linie: Linienweg aus der häufigsten Fahrt mit Zwischenorten (höchstens sieben Glieder; bei Stadtlinien die Haltestellen).
- Verläufe, die nur die Haltestellen gerade verbinden, werden über die Straßen skizziert (`verlauf: skizze`): öffentlicher OSRM-Server (`router.project-osrm.org`), höchstens eine Anfrage je Sekunde, Zeitbudget `GOEBUS_SKIZZE_MINUTEN` (Vorgabe 6), Zwischenspeicher `daten/skizzen.json`, Umwege über das 2,5-Fache verworfen. Abschalten mit `GOEBUS_SKIZZE=0`; anderer Server mit `GOEBUS_SKIZZE_URL`.
- Fernbusse (route_type 200–209, FlixBus u. a.) werden nicht mehr aufgenommen.

## Neu in Fassung 54 (Skript 2.7): Takt, Nachtverkehr, Steigplan, Haltestellenfolge

- `haltestellen.json`: `tag[4]` = je Linie [erste, letzte Abfahrt, Takt in Minuten] (Takt = Median der Abstände zwischen 6 und 19 Uhr, 0 bei weniger als drei Fahrten), `tag[5]` = Abfahrten zwischen 0 und 5 Uhr, `steige` = Linien je Steig (Spalte `platform_code` der Fahrplandaten; nur bei mindestens zwei Steigen).
- Neue Datei `linienfolgen.json`: je Verbund und Linie die Haltestellen in Fahrtrichtung der häufigsten Fahrt.
- Behoben: Linien, die in den Fahrplandaten mehrere Einträge haben (z. B. je Fahrplanperiode), fehlten bis 2.6 in `haltestellen.json`.

## Neu in Fassung 53 (Skript 2.6): gleiche Nummer, anderer Linienweg

Eine Linie aus der offiziellen Liste eines Verbunds wird nur noch übernommen, wenn der dort eingetragene Linienweg zur Linie passt: Wenigstens ein Ortsname daraus muss an einer ihrer Haltestellen vorkommen. Beispiel: Die VSN-Liste führt „1“ Hillerse – Höckelheim – Verdistraße (Northeim); die Linie 1 Dingelstädt – Duderstadt aus Thüringen hat dieselbe Nummer, aber nur 6 von 48 Halten im Landkreis Göttingen – sie fällt jetzt heraus, auch aus dem Gedächtnis der Vorwoche. Das Protokoll nennt solche Fälle („nicht übernommen – gleiche Nummer, anderer Linienweg“).

## Neu in Fassung 52 (Skript 2.5): Abfahrten je Haltestelle

`haltestellen.json` enthält je Haltestelle (alle Steige zusammen) die Abfahrten am nächsten Mittwoch: `tag` = [Anzahl, erste, letzte, {Linie: Anzahl}]. Dazu kommen `stichtag` und `netze` (Kurzname → Kennung). Gezählt werden nur Busse und Straßenbahnen, ohne die Ankunft an der Endhaltestelle, nach `calendar.txt` und `calendar_dates.txt`. Das kostet einen weiteren Durchgang durch `stop_times.txt`, nach dem bisherigen Protokoll gut eine Minute.

## Neu in Fassung 48 (Skript 2.4): schnell und ohne Overpass

- **OpenStreetMap aus Geofabrik-Auszügen:** Die öffentlichen Overpass-Server waren von GitHub aus oft nicht erreichbar („Network is unreachable“, 502, 504). Jeder Fehlversuch kostete mehrere Minuten, ein Lauf brach nach 90 Minuten ab. Jetzt lädt der Ablauf die Bundesländer-Dateien der Geofabrik (Niedersachsen, Hessen, Thüringen, Regierungsbezirk Detmold) und filtert mit `osmium` nur Buslinien und Fahrschulen heraus. Erwartet werden wenige Minuten statt einer Stunde; einen echten Lauf damit gab es aber noch nicht.
- **Overpass nur noch als Notlösung**, wenn der Auszug fehlt: Jeder Server wird einmal gefragt (höchstens 150 Sekunden), ein nicht erreichbarer Server wird im selben Lauf nicht erneut gefragt, insgesamt höchstens 12 Minuten. Danach gelten die Verläufe der Vorwoche.
- **Fahrplanseiten (RVHI, ROSA) als eigener Job:** Er läuft parallel, dauert wenige Minuten und speichert sofort. Vorher kam dieser Schritt nie an die Reihe, weil der Lauf vorher abbrach.
- **Neue Dateien:** `daten/haltestellen.json` (Haltestellen der Region mit ihren Linien, für die Haltestellenseiten der Website). Außerdem schreibt das Skript in `stand.json` die Dauer jedes Abschnitts (`dauer`) und die Herkunft der Kartendaten (`osm_quelle`). Die Verwaltung warnt, wenn ein Lauf länger als 30 Minuten dauert oder abbricht.

## Neu in Fassung 42: Fahrschulen mit Busführerschein

- **Neue Datei `fahrschulen.py`** (bitte zusätzlich hochladen, dazu die geänderte `.github/workflows/liniennetz.yml`). Sie läuft im selben Ablauf nach dem Liniennetz.
- **Warum bei GitHub:** Der Webserver erreicht die Overpass-Server von OpenStreetMap nicht. Außerdem könnte er je Lauf nur wenige Websites prüfen. GitHub liest alle Fahrschulen der Region aus OpenStreetMap, prüft ihre Websites auf die Ausbildung zum Busführerschein (Klasse D, D1, DE, D1E) und schreibt `daten/fahrschulen.json`.
- **Die Website** holt die Datei mit den Liniennetzen ab und nimmt Fahrschulen mit Beleg für den Busführerschein selbsttätig unter „Werde Busfahrer“ auf (abschaltbar in der Verwaltung unter Beruf Busfahrer → Fundstellen → Einstellungen).
- **Schlägt die Fahrschul-Suche fehl**, läuft das Liniennetz trotzdem durch und die Datei der letzten Woche bleibt.

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
