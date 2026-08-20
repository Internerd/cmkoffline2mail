# cmkoffline2mail

Fragt Checkmk (REST-API, getestet mit **2.3.0p20**) ab und verschickt eine SMTP-Mail,
sobald Hosts in bestimmten Checkmk-Ordnern **laenger als konfiguriert** offline sind.
Gedacht fuer den unbeaufsichtigten Betrieb auf einem **Windows Server** in der
**Aufgabenplanung unter einem gMSA-Konto**.

* Pro Ordner eine eigene Zeitspanne (z. B. Produktion 30 Min., Aussenstellen 4 Std.)
* **Eine Mail je Host** (alternativ eine Sammelmail je Ordner oder eine Gesamtmail)
* Hostname, IP-Adresse und beliebige **Custom-Attribute** als Variablen in Betreff und Mailtext
* Zwei Konfigurationsdateien: Steuerung (`config.toml`) und Mailinhalt (`mailtemplate.toml`)
* Merkt sich versendete Meldungen - kein Mail-Spam bei jedem Lauf, optionale Erinnerung
* Ignoriert wahlweise Wartungszeiten, quittierte Stoerungen und stummgeschaltete Hosts
* **Keine externen Python-Pakete noetig** (nur Standardbibliothek)

---

## 1. Voraussetzungen

| | |
|---|---|
| Windows | Server 2016 oder neuer |
| Python | 3.11 oder neuer, von <https://www.python.org/downloads/windows/> ("Install for all users") |
| Ablage | ein frei waehlbarer Basisordner, z. B. `C:\FIRMA\cmkoffline2mail` |
| Checkmk | 2.3.x, erreichbar per HTTPS, Automationsbenutzer vorhanden |
| SMTP | Relay/Smarthost erreichbar (mit oder ohne Anmeldung) |
| AD | gMSA-Konto fuer die Aufgabenplanung |

---

## 2. Installation Schritt fuer Schritt

Die gesamte Installation liegt in **einem frei waehlbaren Basisordner**. In dieser
Anleitung ist das `C:\FIRMA\cmkoffline2mail` - `FIRMA` steht dabei fuer Ihren
eigenen Ordnernamen. Programm, virtuelle Umgebung, Konfiguration, Status und
Protokolle liegen alle darunter:

```
C:\FIRMA\cmkoffline2mail\
├─ .venv\               virtuelle Python-Umgebung (Schritt 2.2)
├─ cmkoffline2mail\     Python-Paket (cli.py, config.py, checkmk.py, ...)
├─ config\              config.toml + mailtemplate.toml
├─ data\                state.json (wird angelegt)
├─ logs\                Protokolle (werden angelegt)
├─ deploy\              PowerShell-Skripte, Run-Task.cmd
├─ tests\
├─ run.cmd              Handaufruf mit der venv
├─ pyproject.toml
└─ README.md
```

Ausserhalb dieses Ordners wird nichts abgelegt. Alle Pfadangaben in der
Konfiguration duerfen relativ sein und gelten dann relativ zur `config.toml` -
der Basisordner laesst sich also spaeter umbenennen oder verschieben.

### 2.1 Dateien auf den Server kopieren

```powershell
# Auf dem Windows Server, administrative PowerShell
New-Item -ItemType Directory "C:\FIRMA\cmkoffline2mail" -Force
# Inhalt dieses Repositories dorthin kopieren (git clone, Copy-Item oder ZIP entpacken)
```

> Beim ZIP-Download von GitHub steckt alles noch einmal in einem Ordner wie
> `cmkoffline2mail-main\`. Es muss dessen **Inhalt** in den Basisordner - nicht
> der Ordner selbst.

Schnelltest - muss `True` liefern:

```powershell
Test-Path "C:\FIRMA\cmkoffline2mail\cmkoffline2mail\cli.py"
```

### 2.2 Virtuelle Umgebung (venv)

Das Programm braucht keine Fremdpakete, eine venv ist trotzdem sinnvoll: sie
bindet die Ausfuehrung an eine bestimmte Python-Version und macht sie unabhaengig
von spaeteren Aenderungen am System-Python.

```powershell
cd "C:\FIRMA\cmkoffline2mail"
& "C:\Program Files\Python311\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m cmkoffline2mail --version
```

`Install-ScheduledTask.ps1` (Schritt 2.7) legt die venv bei Bedarf selbst an.

> **Wichtig fuer die Aufgabenplanung:** Ein `activate` gibt es dort nicht und wird
> auch nicht gebraucht. Die Aufgabe ruft direkt
> `C:\FIRMA\cmkoffline2mail\.venv\Scripts\python.exe` auf - diese `python.exe`
> erkennt ihre Umgebung ueber die daneben liegende `pyvenv.cfg` selbst. Wichtig
> ist nur, dass **nicht** das System-Python eingetragen wird und dass das
> Arbeitsverzeichnis der Basisordner ist.

### 2.3 Automationsbenutzer in Checkmk anlegen

1. In Checkmk: **Setup → Users → Add user**
2. Haken bei **Automation secret for machine accounts** setzen, Geheimnis notieren
3. Rolle zuweisen. Minimal noetige Berechtigungen:
   * *See all hosts and services* (`general.see_all`)
   * *Use Setup* (`wato.use`) - fuer das Auslesen von Ordner und Custom-Attributen
   * *Read access to all hosts and folders* (`wato.see_all_folders`)

   Am einfachsten: eine Kopie der Rolle *Monitoring user* mit diesen drei Rechten.
   Die Rolle *Administrator* funktioniert ebenfalls, ist aber mehr als noetig.

Kurzer Test von der Kommandozeile des Servers aus:

```powershell
curl.exe -H "Authorization: Bearer automation DAS-GEHEIMNIS" `
         "https://cmk.firma.local/produktion/check_mk/api/1.0/version"
```

### 2.4 gMSA anlegen und auf dem Server nutzbar machen

Auf einem Domaincontroller (einmalig, falls noch kein KDS-Rootkey existiert):

```powershell
Add-KdsRootKey -EffectiveImmediately        # im Labor: -EffectiveTime ((Get-Date).AddHours(-10))

New-ADServiceAccount -Name svc-cmkmail `
    -DNSHostName svc-cmkmail.firma.local `
    -PrincipalsAllowedToRetrieveManagedPassword "SRV-MONITOR$"
```

Auf dem Windows Server, der die Aufgabe ausfuehrt:

```powershell
Install-ADServiceAccount svc-cmkmail
Test-ADServiceAccount   svc-cmkmail          # muss True liefern

# Recht "Als Stapelverarbeitungsauftrag anmelden" vergeben:
cd "C:\FIRMA\cmkoffline2mail\deploy"
.\Grant-BatchLogonRight.ps1 -Account "FIRMA\svc-cmkmail$"
```

> Ohne dieses Recht startet die Aufgabe nicht, endet mit `0x1` und erzeugt
> **keinerlei** Ausgabe - auch keine Protokolldatei.

### 2.5 Konfiguration anlegen

```powershell
cd "C:\FIRMA\cmkoffline2mail"
Copy-Item config\config.example.toml       config\config.toml
Copy-Item config\mailtemplate.example.toml config\mailtemplate.toml
notepad config\config.toml
```

Mindestens anzupassen: `checkmk.url`, `checkmk.username`, `checkmk.secret`,
die `[[folders]]`-Abschnitte, `mail.from`, `mail.to` und `smtp.host`.
Status- und Protokolldatei brauchen keine Angabe - ohne `state.file` bzw.
`logging.file` landen sie in `data\` und `logs\` unter dem Basisordner.

Die vorhandenen Ordnerpfade und Attributnamen liefern:

```powershell
.\run.cmd --list-folders
.\run.cmd --show-attributes srv-a01
```

### 2.6 Rechte setzen

Die Konfiguration enthaelt Zugangsdaten. `Install-ScheduledTask.ps1` erledigt das
automatisch; von Hand:

```powershell
$Base = "C:\FIRMA\cmkoffline2mail"
$Gmsa = "FIRMA\svc-cmkmail$"
icacls $Base /inheritance:r
icacls $Base /grant:r "*S-1-5-18:(OI)(CI)F"          # SYSTEM
icacls $Base /grant:r "*S-1-5-32-544:(OI)(CI)F"      # Administratoren
icacls $Base /grant:r "$Gmsa:(OI)(CI)RX"             # gMSA: lesen/ausfuehren
icacls "$Base\data" /grant:r "$Gmsa:(OI)(CI)M"       # ... und schreiben
icacls "$Base\logs" /grant:r "$Gmsa:(OI)(CI)M"
```

Alternativ das Geheimnis aus einer separaten Datei oder Umgebungsvariablen lesen
(`secret_file` / `secret_env`, ebenso `password_file` / `password_env`).

### 2.7 Testen

```powershell
cd "C:\FIRMA\cmkoffline2mail"
.\run.cmd --validate                    # Konfiguration + Vorlage pruefen
.\run.cmd --dry-run                     # Checkmk abfragen, Mails nur anzeigen
.\run.cmd --test-mail ich@firma.de      # eine Testmail verschicken
```

`run.cmd` verwendet automatisch die venv aus dem Basisordner.

### 2.8 Geplante Aufgabe einrichten

```powershell
cd "C:\FIRMA\cmkoffline2mail\deploy"
.\Install-ScheduledTask.ps1 -GmsaAccount "FIRMA\svc-cmkmail$" -IntervalMinutes 15 -RunNow
```

Ohne `-InstallDir` wird der Ordner ueber dem Skript verwendet - das Skript laeuft
also einfach dort, wo es liegt. Es legt bei Bedarf die venv an, erstellt
`config\`, `data\` und `logs\`, setzt die NTFS-Rechte, registriert die Aufgabe und
schreibt einen Mitschnitt nach `logs\install-<Zeitstempel>.log`. Mit `-RunNow`
startet direkt ein Testlauf und die Ausgabe wird angezeigt.

Weitere Schalter: `-InstallDir`, `-TaskName`, `-PythonExe`, `-TimeLimitMinutes`,
`-SkipVenv`.

Die Aufgabe startet nicht direkt `python.exe`, sondern `deploy\Run-Task.cmd`.
Dieser Wrapper leitet **alle** Ausgaben nach `logs\task-run.log` um - auch
Fehler, die auftreten, bevor das Programmprotokoll ueberhaupt existiert
(fehlende venv, falsche Rechte, falscher Pfad). Ohne diesen Umweg verschluckt
die Aufgabenplanung solche Meldungen vollstaendig.

Kontrolle:

```powershell
Start-ScheduledTask -TaskName cmkoffline2mail
Get-ScheduledTaskInfo -TaskName cmkoffline2mail          # LastTaskResult 0 = in Ordnung
Get-Content "C:\FIRMA\cmkoffline2mail\logs\task-run.log" -Tail 40
Get-Content "C:\FIRMA\cmkoffline2mail\logs\cmkoffline2mail.log" -Tail 30
```

Wenn etwas nicht laeuft:

```powershell
.\deploy\Test-Installation.ps1
```

Das Skript prueft Struktur, venv, Konfiguration, Aufgabe, Anmelderecht,
NTFS-Rechte und zeigt die letzten Protokollzeilen samt Bedeutung des
`LastTaskResult`. Es aendert nichts.

> **Intervall und Schwellwert:** Die Aufgabe sollte deutlich haeufiger laufen als der
> kleinste `offline_for`-Wert - sonst wird die Meldung entsprechend spaeter zugestellt.

## 3. Konfigurationsdatei 1: `config.toml`

Vollstaendig kommentiert in [`config/config.example.toml`](config/config.example.toml).

### `[checkmk]`

| Schluessel | Bedeutung |
|---|---|
| `url` | Site-URL, z. B. `https://cmk.firma.local/produktion` |
| `username` | Automationsbenutzer |
| `secret` / `secret_env` / `secret_file` | Automationsgeheimnis direkt, aus Umgebungsvariable oder aus Datei |
| `verify_tls`, `ca_bundle` | TLS-Pruefung, eigenes CA-Zertifikat |
| `timeout`, `retries` | Zeitlimit je Anfrage, Wiederholungen bei Netz-/5xx-Fehlern |
| `sites` | nur bestimmte Livestatus-Sites abfragen |
| `custom_attributes` | Liste der Attribute, die als `{{ attr_... }}` verfuegbar sein sollen |
| `effective_attributes` | auch am Ordner vererbte Attributwerte lesen (Standard: `true`) |
| `folder_lookup_threshold` | ab wie vielen Offline-Hosts die komplette Hostliste statt Einzelabfragen geladen wird |

### `[behaviour]`

| Schluessel | Standard | Bedeutung |
|---|---|---|
| `group_by` | `"host"` | `host` = eine Mail je Host, `folder` = Sammelmail je Ordner, `single` = eine Gesamtmail |
| `include_unreachable` | `true` | UNREACHABLE wie DOWN behandeln |
| `skip_in_downtime` | `true` | geplante Wartung ignorieren |
| `skip_acknowledged` | `false` | quittierte Stoerungen ignorieren |
| `skip_notifications_disabled` | `true` | Hosts mit abgeschalteten Benachrichtigungen ignorieren |
| `skip_pending` | `true` | noch nie geprueft |
| `repeat_after` | `"24h"` | Erinnerung, solange der Host offline bleibt; `"off"` = nur einmal melden |
| `max_hosts_per_mail` | `0` | teilt lange Listen auf mehrere Mails auf (`0` = kein Limit) |
| `datetime_format` | `"%d.%m.%Y %H:%M"` | Zeitformat in der Mail |
| `offline_since_source` | `"auto"` | `last_time_up`, `last_state_change` oder `auto` |

### `[[folders]]` - je Ordner ein Abschnitt

```toml
[[folders]]
path               = "/produktion/aussenstellen"
offline_for        = "4h"        # Zeiteinheiten: s, m/min, h, d, w - auch "1d12h"
include_subfolders = true
recipients         = ["aussendienst@firma.de"]     # optional statt mail.to
subject            = "[Checkmk] {{ host }} in {{ attr_standort }} offline"   # optional
repeat_after       = "12h"                          # optional
enabled            = true
```

Passt mehr als eine Regel auf einen Host, **gewinnt die genaueste (laengste) Pfadangabe**.
Ordner ohne Regel werden nicht gemeldet. Gross-/Kleinschreibung ist egal, `~` und `/`
sind gleichwertig.

### `[mail]`, `[smtp]`, `[state]`, `[logging]`

`mail.from`, `mail.to`, `mail.subject` (mit Platzhaltern) und `mail.template`
(Pfad zur zweiten Konfigurationsdatei; relativ zur `config.toml` erlaubt).

`smtp.security` ist `none`, `starttls` oder `ssl`; `username`/`password` nur ausfuellen,
wenn der Relay eine Anmeldung verlangt. Bei einem internen Relay, der die Server-IP
freigibt, bleiben beide leer - das gMSA meldet sich dann nicht am SMTP an.

`state.file` merkt sich, wer bereits benachrichtigt wurde (Schreibrecht fuer das gMSA noetig);
ohne Angabe `<Basisordner>\data\state.json`. `logging.file` rotiert automatisch
(`max_bytes`, `backup_count`); ohne Angabe `<Basisordner>\logs\cmkoffline2mail.log`.

**Alle Pfadangaben** (`template`, `state.file`, `logging.file`, `ca_bundle`,
`secret_file`, `password_file`) duerfen relativ angegeben werden - sie gelten dann
relativ zur `config.toml`. Umgebungsvariablen wie `%PROGRAMDATA%` werden aufgeloest.

---

## 4. Konfigurationsdatei 2: `mailtemplate.toml`

Enthaelt nur den Mailinhalt - siehe [`config/mailtemplate.example.toml`](config/mailtemplate.example.toml).

```toml
format = "both"        # "text" = nur Text, "html" = nur HTML, "both" = beides

[text]
body     = "..."       # Rumpf der Textmail, enthaelt {{ host_rows }}
host_row = "..."       # eine Zeile je Host

[html]
body     = "..."       # Rumpf der HTML-Mail, enthaelt {{ host_rows }}
host_row = "..."       # eine Tabellenzeile je Host
```

### Platzhalter

Schreibweise `{{ name }}`. HTML-Werte werden automatisch maskiert.

**In `body` und im Betreff (`mail.subject`):**

| Platzhalter | Inhalt |
|---|---|
| `{{ host }}`, `{{ ip }}`, `{{ alias }}` | Hostname, IP-Adresse und Alias (bei `group_by = "host"` genau dieser Host) |
| `{{ hosts }}`, `{{ addresses }}` | alle Hostnamen bzw. IP-Adressen der Mail, kommagetrennt |
| `{{ host_count }}` | Anzahl der Hosts in der Mail |
| `{{ folder }}`, `{{ folder_path }}` | Ordner der Regel |
| `{{ state }}` | `DOWN` oder `UNREACHABLE` |
| `{{ offline_since }}`, `{{ offline_for }}` | Offline-Zeitpunkt und -Dauer |
| `{{ threshold }}` | konfigurierte Wartezeit des Ordners |
| `{{ now }}`, `{{ date }}`, `{{ time }}` | Zeitpunkt des Laufs |
| `{{ site }}`, `{{ checkmk_url }}`, `{{ url }}` | Site-Name, Link zur Oberflaeche, Link zum Host |
| `{{ attr_<name> }}` | Custom-Attribut aus `checkmk.custom_attributes` |
| `{{ host_rows }}` | die zusammengesetzten Hostzeilen (im `body` erforderlich) |

**Zusaetzlich in `host_row` (je Host):**
`{{ offline_seconds }}`, `{{ last_check }}`, `{{ output }}` (Checkmk-Meldung), `{{ folder }}`.

**Custom-Attribute:** Der Name unter `checkmk.custom_attributes` ist die ID aus
*Setup → Hosts → Custom host attributes*; auch **Labels** und **Host-Tags** werden gefunden.
Aus `standort` wird der Platzhalter `{{ attr_standort }}`. Bei Sammelmails enthaelt der
Platzhalter die verschiedenen Werte kommagetrennt. Ein unbekannter Platzhalter fuehrt zu
einem Fehler - `--validate` zeigt ihn vor dem Produktivbetrieb an.

---

## 5. Aufrufe

```
run.cmd [Optionen]                          # nutzt die venv aus dem Basisordner
.venv\Scripts\python.exe -m cmkoffline2mail [-c KONFIG] [Optionen]

-c, --config PFAD      Konfigurationsdatei. Ohne Angabe wird der Reihe nach gesucht in
                       %CMKOFFLINE2MAIL_CONFIG%, <Basisordner>\config\config.toml,
                       <Basisordner>\config.toml
-t, --template PFAD    andere Mail-Vorlage verwenden
    --validate         nur Konfiguration und Vorlage pruefen (keine Verbindung)
-n, --dry-run          Checkmk abfragen, Mails nur anzeigen, Status nicht veraendern
    --test-mail [ADR]  Testmail mit Beispieldaten versenden
    --list-folders     alle Checkmk-Ordner auflisten
    --show-attributes HOST   alle Setup-Attribute eines Hosts anzeigen
    --state-file PFAD  abweichende Statusdatei
    --no-state-update  Statusdatei nicht schreiben
    --log-level STUFE  DEBUG | INFO | WARNING | ERROR
    --version          Version anzeigen
```

**Rueckgabewerte** (in der Aufgabenplanung als *Letztes Ausfuehrungsergebnis* sichtbar):

| Wert | Bedeutung |
|---|---|
| `0` | Lauf erfolgreich (auch wenn nichts zu melden war) |
| `1` | Laufzeitfehler (Checkmk nicht erreichbar, SMTP-Fehler, ...) |
| `2` | Fehler in der Konfiguration oder der Mail-Vorlage |

---

## 6. Wann wird gemeldet?

1. Checkmk liefert alle Hosts im Zustand DOWN (und optional UNREACHABLE).
2. Hosts in Wartung, quittierte oder stummgeschaltete Hosts werden je nach
   `[behaviour]` uebersprungen.
3. Ueber den Setup-Ordner wird die passendste `[[folders]]`-Regel gesucht;
   Hosts in nicht konfigurierten Ordnern werden ignoriert.
4. Ist der Host laenger offline als `offline_for`, wird eine Mail faellig.
5. Die Statusdatei verhindert, dass dieselbe Stoerung erneut gemeldet wird.
   Erst nach `repeat_after` folgt eine Erinnerung; ist der Host wieder online,
   wird sein Eintrag entfernt - eine neue Stoerung wird also wieder gemeldet.

Es gibt bewusst **keine Entwarnungsmail**: gemeldet wird nur das Ausbleiben von Hosts.

---

## 7. Fehlersuche

| Meldung / Symptom | Ursache und Abhilfe |
|---|---|
| `Anmeldung an Checkmk fehlgeschlagen (HTTP 401/403)` | Benutzername oder Automationsgeheimnis falsch, oder der Rolle fehlt *Use Setup* / *See all hosts* |
| `Checkmk-Endpunkt nicht gefunden (HTTP 404)` | `checkmk.url` ohne oder mit falschem Site-Namen |
| `certificate verify failed` | eigenes CA-Zertifikat unter `ca_bundle` hinterlegen (oder notfalls `verify_tls = false`) |
| `Fuer N Host(s) war kein Setup-Ordner ermittelbar` | Der Rolle fehlt der Setup-Zugriff, oder der Host existiert nur im Monitoring |
| `Ordner nicht konfiguriert` im Protokoll | erwartetes Verhalten - Ordner mit `--list-folders` pruefen |
| `SMTP-Anmeldung ... fehlgeschlagen` | `smtp.username`/`password` pruefen; bei IP-Freigabe beide leer lassen |
| Aufgabe endet mit `0x1`, keine Protokolldatei | gMSA fehlt *Als Stapelverarbeitungsauftrag anmelden* (`Grant-BatchLogonRight.ps1`) |
| Aufgabe endet mit `0x2` / `0x80070002` | Pfad in der Aufgabe stimmt nicht - Aufgabe neu registrieren |
| Aufgabe endet mit `0x8007010B` | Arbeitsverzeichnis der Aufgabe ist ungueltig |
| Aufgabe endet mit `2` | `--validate` von Hand ausfuehren, die Meldung nennt den Schluessel |
| Aufgabe endet mit `9` | Wrapper findet `.venv\Scripts\python.exe` nicht |
| `No module named cmkoffline2mail` | Arbeitsverzeichnis ist nicht der Basisordner |
| Es kommen keine Mails mehr | Host wurde bereits gemeldet - `repeat_after` pruefen oder Statusdatei loeschen |
| Keine Protokolldatei | `logging.file` liegt in einem Pfad ohne Schreibrecht fuer das gMSA |

### Die Aufgabe laeuft, aber es passiert nichts

Es gibt drei Protokolle - in dieser Reihenfolge nachsehen:

| Datei | Inhalt |
|---|---|
| `logs\task-run.log` | Ausgabe jedes Aufgabenlaufs, inklusive Fehlern **vor** dem Programmstart |
| `logs\cmkoffline2mail.log` | Programmprotokoll (rotierend) |
| `logs\install-*.log` | Mitschnitt der Installation |

Existiert `logs\task-run.log` gar nicht, hat die Aufgabenplanung den Wrapper nie
gestartet - dann liegt es am Konto (Anmelderecht), nicht am Programm.

Alles auf einmal pruefen:

```powershell
cd "C:\FIRMA\cmkoffline2mail"
.\deploy\Test-Installation.ps1
```

Zusaetzlich hilft der Verlauf der Aufgabenplanung, falls er abgeschaltet ist:

```powershell
wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
```

Fuer Details `--log-level DEBUG` setzen - dann werden auch die angefragten URLs protokolliert
(das Automationsgeheimnis wird nie protokolliert).

---

## 8. Entwicklung

```bash
python -m unittest discover -s tests -t .
```

Die Tests kommen ohne Netzwerk, Checkmk und SMTP-Server aus.

```
cmkoffline2mail/     Python-Paket
├─ cli.py            Kommandozeile und Hauptablauf
├─ config.py         Einlesen und Pruefen von config.toml
├─ checkmk.py        REST-API-Client (Monitoring + Setup)
├─ evaluate.py       Ordnerregeln, Schwellwerte, Gruppierung, Platzhalter
├─ render.py         Mail-Vorlage (mailtemplate.toml)
├─ mailer.py         SMTP-Versand
├─ state.py          Statusdatei gegen wiederholte Mails
├─ duration.py       Zeitangaben wie "1d12h"
└─ logging_setup.py  Datei- und Konsolenprotokoll

deploy/
├─ Install-ScheduledTask.ps1   venv, Verzeichnisse, Rechte, Aufgabe
├─ Test-Installation.ps1       Diagnose: warum laeuft die Aufgabe nicht?
├─ Grant-BatchLogonRight.ps1   Recht "Anmelden als Stapelverarbeitungsauftrag"
└─ Run-Task.cmd                Startskript der Aufgabe (protokolliert alles)

run.cmd                        Handaufruf mit der venv
```
