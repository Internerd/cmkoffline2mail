<#
.SYNOPSIS
    Richtet cmkoffline2mail komplett in EINEM Ordner ein: virtuelle Python-Umgebung,
    Verzeichnisse, NTFS-Rechte und die geplante Aufgabe unter einem gMSA-Konto.

.DESCRIPTION
    Der Basisordner ist frei waehlbar (z. B. C:\FIRMA\cmkoffline2mail) und enthaelt
    anschliessend alles:

        <Basisordner>\
        ├─ .venv\             virtuelle Python-Umgebung (wird hier angelegt)
        ├─ cmkoffline2mail\   Python-Paket
        ├─ config\            config.toml + mailtemplate.toml
        ├─ data\              state.json
        ├─ logs\              Protokoll
        └─ deploy\            dieses Skript

    Ohne -InstallDir wird der Ordner ueber diesem Skript verwendet - das Skript
    laesst sich also einfach dort ausfuehren, wo es liegt.

    Die Aufgabe ruft direkt <Basisordner>\.venv\Scripts\python.exe auf. Ein
    "activate" ist in der Aufgabenplanung weder moeglich noch noetig: die
    python.exe der venv erkennt ihre Umgebung ueber die daneben liegende
    pyvenv.cfg selbst.

    Voraussetzungen: administrative PowerShell; das gMSA ist auf diesem Server
    installiert (Install-ADServiceAccount) und besitzt das Recht "Anmelden als
    Stapelverarbeitungsauftrag" (siehe Grant-BatchLogonRight.ps1).

.EXAMPLE
    .\Install-ScheduledTask.ps1 -GmsaAccount "FIRMA\svc-cmkmail$"

.EXAMPLE
    .\Install-ScheduledTask.ps1 -GmsaAccount "FIRMA\svc-cmkmail$" `
        -InstallDir "C:\FIRMA\cmkoffline2mail" -IntervalMinutes 10
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $GmsaAccount,                                   # z. B. "FIRMA\svc-cmkmail$"

    [string] $InstallDir       = (Split-Path -Parent $PSScriptRoot),
    [string] $TaskName         = "cmkoffline2mail",
    [string] $PythonExe        = "",                         # leer = automatisch suchen
    [int]    $IntervalMinutes  = 15,
    [int]    $TimeLimitMinutes = 30,
    [switch] $SkipVenv,                                      # vorhandene venv unveraendert lassen
    [switch] $RunNow                                         # Aufgabe direkt testen und Protokoll zeigen
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string] $Text) Write-Host "==> $Text" -ForegroundColor Cyan }

# Installation vollstaendig mitschneiden - hilft, wenn spaeter etwas nicht laeuft.
$transcript = $null
try {
    $transcriptDir = Join-Path $InstallDir "logs"
    if (-not (Test-Path -LiteralPath $transcriptDir)) {
        New-Item -ItemType Directory -Path $transcriptDir -Force | Out-Null
    }
    $transcript = Join-Path $transcriptDir ("install-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
    Start-Transcript -Path $transcript | Out-Null
    Write-Host "Mitschnitt dieser Installation: $transcript" -ForegroundColor DarkGray
} catch {
    Write-Warning "Mitschnitt konnte nicht gestartet werden: $($_.Exception.Message)"
}

# --- 1. Struktur pruefen ----------------------------------------------------
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "cmkoffline2mail\cli.py"))) {
    throw ("In '$InstallDir' liegt kein cmkoffline2mail (erwartet: cmkoffline2mail\cli.py). " +
           "Bitte den Inhalt des Repositories dorthin kopieren oder -InstallDir angeben.")
}
Write-Step "Basisordner: $InstallDir"

$venvDir     = Join-Path $InstallDir ".venv"
$venvPython  = Join-Path $venvDir "Scripts\python.exe"
$configDir   = Join-Path $InstallDir "config"
$dataDir     = Join-Path $InstallDir "data"
$logDir      = Join-Path $InstallDir "logs"
$configPath  = Join-Path $configDir "config.toml"

# --- 2. Virtuelle Umgebung --------------------------------------------------
if ($SkipVenv -and (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Vorhandene virtuelle Umgebung wird verwendet: $venvDir"
} else {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (-not $PythonExe) {
            $candidate = Get-Command python.exe -ErrorAction SilentlyContinue |
                         Where-Object { $_.Source -notlike "*WindowsApps*" } |
                         Select-Object -First 1
            if ($candidate) {
                $PythonExe = $candidate.Source
            } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
                $PythonExe = (& py.exe -3 -c "import sys; print(sys.executable)")
            } else {
                throw "Kein Python gefunden. Bitte Python 3.11+ installieren und -PythonExe angeben."
            }
        }
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            throw "Python wurde unter '$PythonExe' nicht gefunden."
        }

        $version = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
        if ([version]$version -lt [version]"3.11") {
            throw "Python $version ist zu alt - benoetigt wird 3.11 oder neuer (gefunden: $PythonExe)."
        }

        Write-Step "Lege virtuelle Umgebung an ($PythonExe, Python $version)"
        & $PythonExe -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "Anlegen der virtuellen Umgebung ist fehlgeschlagen." }
    } else {
        Write-Step "Virtuelle Umgebung ist bereits vorhanden: $venvDir"
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "In der virtuellen Umgebung fehlt '$venvPython'."
}

# Funktionstest: laeuft das Programm mit dieser venv?
$reported = & $venvPython -c "import sys; print(sys.version.split()[0])"
Push-Location $InstallDir
try {
    $selfTest = & $venvPython -m cmkoffline2mail --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Selbsttest fehlgeschlagen: $selfTest" }
    # Bytecode vorab erzeugen - das gMSA erhaelt spaeter nur Leserechte.
    & $venvPython -m compileall -q (Join-Path $InstallDir "cmkoffline2mail") | Out-Null
} finally {
    Pop-Location
}
Write-Host "    venv-Python $reported, $selfTest" -ForegroundColor Green

# --- 3. Verzeichnisse und Beispielkonfiguration -----------------------------
foreach ($dir in @($configDir, $dataDir, $logDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "    Verzeichnis angelegt: $dir"
    }
}

$configCreated = $false
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item (Join-Path $configDir "config.example.toml") $configPath
    $configCreated = $true
}
$templatePath = Join-Path $configDir "mailtemplate.toml"
if (-not (Test-Path -LiteralPath $templatePath)) {
    Copy-Item (Join-Path $configDir "mailtemplate.example.toml") $templatePath
}

# --- 4. NTFS-Rechte ---------------------------------------------------------
# Die Konfiguration enthaelt Zugangsdaten: Vererbung entfernen, nur SYSTEM,
# Administratoren und das gMSA berechtigen.
Write-Step "Setze NTFS-Rechte fuer $GmsaAccount"
icacls $InstallDir /inheritance:r                       | Out-Null
icacls $InstallDir /grant:r "*S-1-5-18:(OI)(CI)F"       | Out-Null   # SYSTEM
icacls $InstallDir /grant:r "*S-1-5-32-544:(OI)(CI)F"   | Out-Null   # Administratoren
icacls $InstallDir /grant:r "$($GmsaAccount):(OI)(CI)RX" | Out-Null  # gMSA: nur lesen/ausfuehren
foreach ($dir in @($dataDir, $logDir)) {                             # ausser hier: schreiben
    icacls $dir /grant:r "$($GmsaAccount):(OI)(CI)M"     | Out-Null
}

# --- 5. gMSA pruefen --------------------------------------------------------
$accountName = $GmsaAccount.Split('\')[-1].TrimEnd('$')
if (Get-Command Test-ADServiceAccount -ErrorAction SilentlyContinue) {
    if (-not (Test-ADServiceAccount -Identity $accountName)) {
        throw ("Das gMSA '$GmsaAccount' ist auf diesem Server nicht nutzbar. " +
               "Bitte zuerst 'Install-ADServiceAccount $accountName' ausfuehren.")
    }
    Write-Host "    gMSA '$GmsaAccount' ist einsatzbereit." -ForegroundColor Green
} else {
    Write-Warning "ActiveDirectory-Modul nicht vorhanden - gMSA konnte nicht geprueft werden."
}

# --- 6. Aufgabe registrieren ------------------------------------------------
Write-Step "Registriere geplante Aufgabe '$TaskName' (alle $IntervalMinutes Minuten)"

# Die Aufgabe ruft den Wrapper auf: er leitet stdout/stderr in
# logs\task-run.log um - sonst geht jede Fehlermeldung verloren, die vor dem
# Start des Programmprotokolls entsteht.
$runner = Join-Path $PSScriptRoot "Run-Task.cmd"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Startskript '$runner' fehlt - bitte deploy\Run-Task.cmd mitkopieren."
}

$action = New-ScheduledTaskAction `
    -Execute $env:ComSpec `
    -Argument "/c `"$runner`" --config `"$configPath`"" `
    -WorkingDirectory $InstallDir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# LogonType Password ist bei einem gMSA korrekt - das Kennwort holt Windows aus dem AD.
$principal = New-ScheduledTaskPrincipal -UserId $GmsaAccount -LogonType Password -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "    Vorhandene Aufgabe entfernt."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Meldet per Mail, wenn Hosts in bestimmten Checkmk-Ordnern zu lange offline sind." `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

$taskLog = Join-Path $logDir "task-run.log"

Write-Host ""
Write-Host "Fertig. Die Aufgabe startet:" -ForegroundColor Green
Write-Host "  $env:ComSpec /c `"$runner`" --config `"$configPath`""
Write-Host "  Arbeitsverzeichnis: $InstallDir"
Write-Host "  Ausgabe der Laeufe: $taskLog"
Write-Host "  Programmprotokoll : $(Join-Path $logDir 'cmkoffline2mail.log')"
Write-Host ""
if ($configCreated) {
    Write-Warning "Die Konfiguration wurde aus der Beispieldatei erzeugt und muss angepasst werden!"
}
Write-Host "Naechste Schritte:" -ForegroundColor Cyan
Write-Host "  1. notepad `"$configPath`""
Write-Host "  2. & `"$venvPython`" -m cmkoffline2mail -c `"$configPath`" --validate"
Write-Host "  3. & `"$venvPython`" -m cmkoffline2mail -c `"$configPath`" --dry-run"
Write-Host "  4. Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "     Get-ScheduledTaskInfo -TaskName '$TaskName'   # LastTaskResult 0 = in Ordnung"
Write-Host "     Get-Content `"$taskLog`" -Tail 40"
Write-Host ""
Write-Host "Pruefung der gesamten Installation:  .\deploy\Test-Installation.ps1" -ForegroundColor Cyan

if ($RunNow) {
    Write-Step "Testlauf der Aufgabe"
    Start-ScheduledTask -TaskName $TaskName
    $waited = 0
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and $waited -lt 120) {
        Start-Sleep -Seconds 2
        $waited += 2
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("    LastTaskResult: 0x{0:X} (0 = in Ordnung)" -f $info.LastTaskResult)
    if (Test-Path -LiteralPath $taskLog) {
        Write-Host "--- $taskLog (letzte 40 Zeilen) ---" -ForegroundColor DarkGray
        Get-Content $taskLog -Tail 40
    } else {
        Write-Warning ("Es wurde keine Datei '$taskLog' erzeugt - die Aufgabe hat den Wrapper nicht " +
                       "gestartet. Meist fehlt dem gMSA das Recht 'Anmelden als Stapelverarbeitungsauftrag'.")
    }
}

if ($transcript) { try { Stop-Transcript | Out-Null } catch { } }
