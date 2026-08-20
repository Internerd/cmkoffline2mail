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
    [string] $PythonExe        = "",                         # leer = maschinenweites Python suchen
                                                             # (muss "Install for all users" sein!)
    [int]    $IntervalMinutes  = 15,
    [int]    $TimeLimitMinutes = 30,
    [switch] $SkipVenv,                                      # vorhandene venv unveraendert lassen
    [switch] $RunNow                                         # Aufgabe direkt testen und Protokoll zeigen
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string] $Text) Write-Host "==> $Text" -ForegroundColor Cyan }

function Test-UserProfilePath {
    <#  Liegt der Pfad in einem Benutzerprofil (C:\Users\...)?
        Solche Installationen sind fuer ein gMSA unbrauchbar: das Dienstkonto
        hat keinen Zugriff auf fremde Profile.  #>
    param([string] $Path)
    if (-not $Path) { return $false }
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { $full = $Path }
    $usersDir = Join-Path $env:SystemDrive "Users"
    return $full.StartsWith($usersDir, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-PythonVersion {
    param([string] $Exe)
    $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$out".Trim()
}

function Find-MachineWidePython {
    <#  Sucht maschinenweit installierte Interpreter (HKLM-Registrierung und
        die ueblichen Programmverzeichnisse) und liefert den neuesten ab 3.11.  #>
    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($hive in @("HKLM:\SOFTWARE\Python\PythonCore", "HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore")) {
        if (-not (Test-Path $hive)) { continue }
        foreach ($key in (Get-ChildItem $hive -ErrorAction SilentlyContinue)) {
            $installPath = (Get-ItemProperty -Path (Join-Path $key.PSPath "InstallPath") `
                            -ErrorAction SilentlyContinue).'(default)'
            if ($installPath) { $candidates.Add((Join-Path $installPath "python.exe")) }
        }
    }

    foreach ($pattern in @("$env:ProgramFiles\Python3*\python.exe",
                           "${env:ProgramFiles(x86)}\Python3*\python.exe",
                           "$env:SystemDrive\Python3*\python.exe")) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }

    $best = $null
    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        if (Test-UserProfilePath $exe) { continue }
        $version = Get-PythonVersion $exe
        if (-not $version) { continue }
        if ([version]$version -lt [version]"3.11") { continue }
        if (-not $best -or [version]$version -gt [version]$best.Version) {
            $best = [pscustomobject]@{ Exe = $exe; Version = $version }
        }
    }
    return $best
}

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
$packageInit = Join-Path $InstallDir "cmkoffline2mail\__init__.py"
if (-not (Test-Path -LiteralPath $packageInit)) {
    Write-Host "Inhalt von '$InstallDir':" -ForegroundColor Yellow
    Get-ChildItem -LiteralPath $InstallDir -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("  {0}  {1}" -f $(if ($_.PSIsContainer) { "[Ordner]" } else { "[Datei] " }), $_.Name) }
    throw ("In '$InstallDir' fehlt das Python-Paket (erwartet: cmkoffline2mail\__init__.py).`n" +
           "Haeufige Ursache: Beim ZIP-Download steckt alles noch einmal in einem Ordner wie " +
           "'cmkoffline2mail-main'. Es muss dessen INHALT in den Basisordner - nicht der Ordner selbst.`n" +
           "Alternativ mit -InstallDir den richtigen Basisordner angeben.")
}
Write-Step "Basisordner: $InstallDir"

$venvDir     = Join-Path $InstallDir ".venv"
$venvPython  = Join-Path $venvDir "Scripts\python.exe"
$configDir   = Join-Path $InstallDir "config"
$dataDir     = Join-Path $InstallDir "data"
$logDir      = Join-Path $InstallDir "logs"
$configPath  = Join-Path $configDir "config.toml"

# --- 2. Virtuelle Umgebung --------------------------------------------------
# WICHTIG: Das Basis-Python MUSS maschinenweit installiert sein. Eine venv
# verweist in ihrer pyvenv.cfg dauerhaft auf das Basis-Python; liegt dieses in
# einem Benutzerprofil (z. B. C:\Users\<Name>\AppData\Local\Python\...), kann
# das gMSA es nicht lesen und die Aufgabe scheitert sofort.

if ($SkipVenv -and (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Vorhandene virtuelle Umgebung wird verwendet: $venvDir"
} elseif (-not (Test-Path -LiteralPath $venvPython)) {
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            throw "Python wurde unter '$PythonExe' nicht gefunden."
        }
        $version = Get-PythonVersion $PythonExe
        if (-not $version) { throw "'$PythonExe' liess sich nicht ausfuehren." }
    } else {
        Write-Step "Suche maschinenweit installiertes Python (3.11+)"
        $found = Find-MachineWidePython
        if (-not $found) {
            throw ("Es wurde kein maschinenweit installiertes Python 3.11+ gefunden.`n" +
                   "Ein Python aus dem Benutzerprofil (C:\Users\...\AppData\...) ist hier " +
                   "unbrauchbar, weil das gMSA nicht darauf zugreifen kann.`n" +
                   "Bitte Python von python.org mit der Option 'Install for all users' " +
                   "installieren (Ziel z. B. C:\Program Files\Python313) und den Aufruf " +
                   "wiederholen - notfalls mit -PythonExe `"C:\Program Files\Python313\python.exe`".")
        }
        $PythonExe = $found.Exe
        $version   = $found.Version
    }

    if ([version]$version -lt [version]"3.11") {
        throw "Python $version ist zu alt - benoetigt wird 3.11 oder neuer (gefunden: $PythonExe)."
    }

    if (Test-UserProfilePath $PythonExe) {
        throw ("'$PythonExe' liegt in einem Benutzerprofil.`n" +
               "Eine daraus erzeugte virtuelle Umgebung verweist dauerhaft auf diesen Pfad " +
               "und ist unter dem Dienstkonto $GmsaAccount nicht lauffaehig.`n" +
               "Bitte Python maschinenweit installieren ('Install for all users').")
    }

    Write-Step "Lege virtuelle Umgebung an ($PythonExe, Python $version)"
    & $PythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Anlegen der virtuellen Umgebung ist fehlgeschlagen." }
} else {
    Write-Step "Virtuelle Umgebung ist bereits vorhanden: $venvDir"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "In der virtuellen Umgebung fehlt '$venvPython'."
}

# Auch eine bereits vorhandene venv pruefen: worauf verweist ihre pyvenv.cfg?
$pyvenvCfg = Join-Path $venvDir "pyvenv.cfg"
if (Test-Path -LiteralPath $pyvenvCfg) {
    $basePaths = Get-Content $pyvenvCfg |
                 Where-Object { $_ -match "^\s*(home|executable)\s*=" } |
                 ForEach-Object { ($_ -split "=", 2)[1].Trim() }
    $homePath = $basePaths | Select-Object -First 1
    Write-Host "    Basis-Python der venv: $homePath"
    $inProfile = $basePaths | Where-Object { Test-UserProfilePath $_ } | Select-Object -First 1
    if ($inProfile) {
        throw ("Die virtuelle Umgebung '$venvDir' verweist auf ein Python im Benutzerprofil:`n" +
               "    $inProfile`n" +
               "Unter dem Dienstkonto $GmsaAccount ist sie damit nicht lauffaehig.`n" +
               "Bitte den Ordner '.venv' loeschen, Python maschinenweit installieren " +
               "('Install for all users') und dieses Skript erneut ausfuehren.")
    }
}

# Das Paket in der venv bekanntmachen: eine .pth-Datei im site-packages der venv
# nimmt den Basisordner dauerhaft in den Suchpfad auf. Damit funktioniert
# "python -m cmkoffline2mail" unabhaengig vom Arbeitsverzeichnis - genau das ist
# die Ursache der Meldung "No module named cmkoffline2mail".
$sitePackages = & $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
if ($LASTEXITCODE -ne 0 -or -not $sitePackages) {
    throw "site-packages der virtuellen Umgebung konnte nicht ermittelt werden."
}
$sitePackages = "$sitePackages".Trim()
if (-not (Test-Path -LiteralPath $sitePackages)) {
    New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
}
if ($InstallDir -match "[^\u0020-\u007E]") {
    Write-Warning ("Der Basisordner enthaelt Sonderzeichen: '$InstallDir'. " +
                   "Der Suchpfad-Eintrag (.pth) wird je nach Python-Version unterschiedlich " +
                   "gelesen - ein Basisordner ohne Umlaute/Sonderzeichen ist zuverlaessiger.")
}
$pthFile = Join-Path $sitePackages "cmkoffline2mail.pth"
Set-Content -LiteralPath $pthFile -Value $InstallDir -Encoding ASCII
Write-Host "    Suchpfad eingetragen: $pthFile -> $InstallDir"

# Funktionstest bewusst aus einem FREMDEN Arbeitsverzeichnis - so wie es die
# Aufgabenplanung im Zweifel auch tut.
$reported = & $venvPython -c "import sys; print(sys.version.split()[0])"
Push-Location $env:SystemRoot
try {
    $selfTest = & $venvPython -m cmkoffline2mail --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("Selbsttest fehlgeschlagen: $selfTest`n" +
               "Das Paket ist aus der venv nicht importierbar. Bitte pruefen, ob " +
               "'$packageInit' existiert.")
    }
    # Bytecode vorab erzeugen - das gMSA erhaelt spaeter nur Leserechte.
    & $venvPython -m compileall -q (Join-Path $InstallDir "cmkoffline2mail") | Out-Null
} finally {
    Pop-Location
}
Write-Host "    venv-Python $reported, $selfTest (Import ohne Arbeitsverzeichnis geprueft)" -ForegroundColor Green

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
