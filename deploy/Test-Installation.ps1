<#
.SYNOPSIS
    Prueft eine cmkoffline2mail-Installation und sagt, warum die geplante
    Aufgabe nicht laeuft.

.DESCRIPTION
    Geht der Reihe nach durch: Verzeichnisstruktur, virtuelle Umgebung,
    Konfiguration, registrierte Aufgabe, Anmelderecht des Kontos, NTFS-Rechte
    und die letzten Protokollzeilen. Aendert nichts.

    Am besten administrativ ausfuehren - sonst koennen Anmelderecht und
    Aufgabendetails nicht vollstaendig geprueft werden.

.EXAMPLE
    .\Test-Installation.ps1

.EXAMPLE
    .\Test-Installation.ps1 -InstallDir "C:\FIRMA\cmkoffline2mail" -TaskName cmkoffline2mail
#>
[CmdletBinding()]
param(
    [string] $InstallDir = (Split-Path -Parent $PSScriptRoot),
    [string] $TaskName   = "cmkoffline2mail"
)

$script:Problems = @()

function Test-UserProfilePath {
    param([string] $Path)
    if (-not $Path) { return $false }
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { $full = $Path }
    return $full.StartsWith((Join-Path $env:SystemDrive "Users"), [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-Item {
    param([string] $Name, [scriptblock] $Check, [string] $Hint = "")
    try {
        $result = & $Check
    } catch {
        $result = $false
        $detail = $_.Exception.Message
    }
    if ($result -is [string]) {
        Write-Host ("  [ok]   {0}: {1}" -f $Name, $result) -ForegroundColor Green
    } elseif ($result) {
        Write-Host ("  [ok]   {0}" -f $Name) -ForegroundColor Green
    } else {
        Write-Host ("  [FEHL] {0}" -f $Name) -ForegroundColor Red
        if ($detail) { Write-Host ("         {0}" -f $detail) -ForegroundColor DarkRed }
        if ($Hint)   { Write-Host ("         -> {0}" -f $Hint) -ForegroundColor Yellow }
        $script:Problems += $Name
    }
}

$InstallDir = (Resolve-Path -LiteralPath $InstallDir -ErrorAction SilentlyContinue).Path
if (-not $InstallDir) { throw "Basisordner nicht gefunden. Bitte -InstallDir angeben." }

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
$configPath = Join-Path $InstallDir "config\config.toml"
$runner     = Join-Path $InstallDir "deploy\Run-Task.cmd"
$taskLog    = Join-Path $InstallDir "logs\task-run.log"
$appLog     = Join-Path $InstallDir "logs\cmkoffline2mail.log"

Write-Host ""
Write-Host "cmkoffline2mail - Installationspruefung" -ForegroundColor Cyan
Write-Host "Basisordner: $InstallDir"
Write-Host ""

# --- 1. Dateien -------------------------------------------------------------
Write-Host "1. Verzeichnisstruktur" -ForegroundColor Cyan
Test-Item "Python-Paket (cmkoffline2mail\__init__.py)" { Test-Path (Join-Path $InstallDir "cmkoffline2mail\__init__.py") } `
          "Inhalt des Repositories in den Basisordner kopieren (beim ZIP nur den INHALT von 'cmkoffline2mail-main')"
if (-not (Test-Path (Join-Path $InstallDir "cmkoffline2mail\__init__.py"))) {
    Write-Host "         Gefunden wurde stattdessen:" -ForegroundColor Yellow
    Get-ChildItem -LiteralPath $InstallDir -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("           {0}" -f $_.Name) -ForegroundColor Yellow }
}
Test-Item "Startskript (deploy\Run-Task.cmd)" { Test-Path $runner } "deploy\Run-Task.cmd mitkopieren"
Test-Item "Konfiguration (config\config.toml)" { Test-Path $configPath } `
          "config\config.example.toml nach config\config.toml kopieren und anpassen"
Test-Item "Mail-Vorlage (config\mailtemplate.toml)" { Test-Path (Join-Path $InstallDir "config\mailtemplate.toml") } `
          "config\mailtemplate.example.toml kopieren"
Test-Item "Datenverzeichnis (data)" { Test-Path (Join-Path $InstallDir "data") } "Ordner 'data' anlegen"
Test-Item "Protokollverzeichnis (logs)" { Test-Path (Join-Path $InstallDir "logs") } "Ordner 'logs' anlegen"

# --- 2. Virtuelle Umgebung --------------------------------------------------
Write-Host ""
Write-Host "2. Virtuelle Umgebung" -ForegroundColor Cyan
Test-Item "venv-Python vorhanden" { Test-Path $venvPython } "Install-ScheduledTask.ps1 erneut ausfuehren"
if (Test-Path $venvPython) {
    Test-Item "venv-Python startet" {
        $v = & $venvPython -c "import sys; print(sys.version.split()[0])" 2>&1
        if ($LASTEXITCODE -ne 0) { throw "$v" }
        "Python $v"
    }
    # Bewusst aus einem FREMDEN Arbeitsverzeichnis - so wie es die
    # Aufgabenplanung im Zweifel auch tut.
    Test-Item "Programm importierbar (ohne passendes Arbeitsverzeichnis)" {
        Push-Location $env:SystemRoot
        try {
            $v = & $venvPython -m cmkoffline2mail --version 2>&1
            if ($LASTEXITCODE -ne 0) { throw "$v" }
            "$v"
        } finally { Pop-Location }
    } "Beheben mit:  .\deploy\Register-PackagePath.ps1"

    $sitePackages = ("$(& $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")").Trim()
    $pthFile = Join-Path $sitePackages "cmkoffline2mail.pth"
    Test-Item "Suchpfad-Eintrag (cmkoffline2mail.pth) vorhanden" {
        if (-not (Test-Path -LiteralPath $pthFile)) { return $false }
        $eingetragen = (Get-Content $pthFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ("$eingetragen".Trim() -ne $InstallDir) { throw "zeigt auf '$eingetragen'" }
        "$eingetragen"
    } "Beheben mit:  .\deploy\Register-PackagePath.ps1"

    # Eine venv verweist dauerhaft auf ihr Basis-Python. Liegt das in einem
    # Benutzerprofil, kann ein Dienstkonto (gMSA) es nicht lesen.
    $pyvenvCfg = Join-Path $InstallDir ".venv\pyvenv.cfg"
    if (Test-Path -LiteralPath $pyvenvCfg) {
        $basePaths = Get-Content $pyvenvCfg |
                     Where-Object { $_ -match "^\s*(home|executable)\s*=" } |
                     ForEach-Object { ($_ -split "=", 2)[1].Trim() }
        Write-Host "         Basis-Python: $($basePaths | Select-Object -First 1)"
        Test-Item "Basis-Python liegt NICHT in einem Benutzerprofil" `
                  { -not ($basePaths | Where-Object { Test-UserProfilePath $_ }) } `
                  ("Python maschinenweit installieren ('Install for all users'), " +
                   "'.venv' loeschen und Install-ScheduledTask.ps1 erneut ausfuehren")
    }
}

# --- 3. Konfiguration -------------------------------------------------------
Write-Host ""
Write-Host "3. Konfiguration" -ForegroundColor Cyan
if ((Test-Path $venvPython) -and (Test-Path $configPath)) {
    Push-Location $InstallDir
    try {
        $out = & $venvPython -m cmkoffline2mail -c $configPath --validate 2>&1
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    Test-Item "config.toml und Vorlage sind gueltig" { $code -eq 0 } "Meldungen unten beachten"
    $out | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkGray }
} else {
    Write-Host "  [--]   uebersprungen (venv oder Konfiguration fehlt)" -ForegroundColor DarkYellow
}

# --- 4. Geplante Aufgabe ----------------------------------------------------
Write-Host ""
Write-Host "4. Geplante Aufgabe" -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Test-Item "Aufgabe '$TaskName' ist registriert" { $null -ne $task } "Install-ScheduledTask.ps1 ausfuehren"

$taskUser = $null
if ($task) {
    $taskUser = $task.Principal.UserId
    Write-Host "         Konto      : $taskUser (LogonType: $($task.Principal.LogonType))"
    Write-Host "         Startet    : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    Write-Host "         Arbeitsordner: $($task.Actions[0].WorkingDirectory)"
    Write-Host "         Status     : $($task.State)"

    Test-Item "Aufgabe ist aktiviert" { $task.State -ne "Disabled" } "Enable-ScheduledTask -TaskName '$TaskName'"

    # cmd.exe verstuemmelt die Befehlszeile, wenn hinter /c mehr als zwei
    # Anfuehrungszeichen stehen - dann landen Pfade als lose Argumente beim Programm.
    $arguments = "$($task.Actions[0].Arguments)"
    Test-Item "Befehlszeile der Aufgabe ist eindeutig (max. zwei Anfuehrungszeichen)" `
              { ($arguments.ToCharArray() | Where-Object { $_ -eq '"' }).Count -le 2 } `
              "Aufgabe neu registrieren:  .\deploy\Install-ScheduledTask.ps1 -GmsaAccount '$taskUser'"

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $rc = $info.LastTaskResult
    Write-Host ("         Letzter Lauf: {0}, Ergebnis 0x{1:X}" -f $info.LastRunTime, $rc)
    $meaning = switch ($rc) {
        0          { "in Ordnung" }
        1          { "allgemeiner Fehler - meist fehlendes Recht 'Anmelden als Stapelverarbeitungsauftrag' oder das Programm endete mit Rueckgabewert 1" }
        2          { "Datei nicht gefunden - Pfad in der Aufgabe stimmt nicht" }
        267009     { "laeuft gerade" }
        267011     { "wurde noch nie ausgefuehrt" }
        267014     { "wurde abgebrochen" }
        2147942401 { "Datei nicht gefunden (0x80070002)" }
        2147942699 { "Arbeitsverzeichnis ungueltig (0x8007010B)" }
        default    { "siehe logs\task-run.log" }
    }
    Write-Host "         Bedeutung  : $meaning" -ForegroundColor Yellow
}

# --- 5. Anmelderecht --------------------------------------------------------
Write-Host ""
Write-Host "5. Anmelderecht des Aufgabenkontos" -ForegroundColor Cyan
if ($taskUser -and $taskUser -notmatch '^(SYSTEM|NT-AUTORIT|NT AUTHORITY)') {
    try {
        $sid = (New-Object System.Security.Principal.NTAccount($taskUser)).Translate(
                    [System.Security.Principal.SecurityIdentifier]).Value
        $export = Join-Path $env:TEMP "cmk-secpol.inf"
        secedit /export /areas USER_RIGHTS /cfg $export | Out-Null
        $line = Get-Content $export | Where-Object { $_ -like "SeBatchLogonRight*" }
        Remove-Item $export -ErrorAction SilentlyContinue
        Test-Item "'$taskUser' darf sich als Stapelverarbeitungsauftrag anmelden" `
                  { $line -match [regex]::Escape($sid) } `
                  ".\Grant-BatchLogonRight.ps1 -Account '$taskUser'"
    } catch {
        Write-Host "  [--]   nicht pruefbar: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "  [--]   uebersprungen" -ForegroundColor DarkYellow
}

# --- 6. NTFS-Rechte ---------------------------------------------------------
Write-Host ""
Write-Host "6. NTFS-Rechte" -ForegroundColor Cyan
if ($taskUser) {
    $short = $taskUser.Split('\')[-1]
    foreach ($pair in @(@{P = $InstallDir; N = "Basisordner (lesen)"},
                        @{P = (Join-Path $InstallDir "data"); N = "data (schreiben)"},
                        @{P = (Join-Path $InstallDir "logs"); N = "logs (schreiben)"})) {
        if (-not (Test-Path -LiteralPath $pair.P)) { continue }
        $aces = (Get-Acl $pair.P).Access | Where-Object { $_.IdentityReference -like "*$short*" }
        if ($aces) {
            Write-Host ("  [ok]   {0}: {1}" -f $pair.N, (($aces | ForEach-Object { $_.FileSystemRights }) -join ", ")) -ForegroundColor Green
        } else {
            Write-Host ("  [FEHL] {0}: keine Berechtigung fuer {1}" -f $pair.N, $taskUser) -ForegroundColor Red
            Write-Host ("         -> icacls `"{0}`" /grant `"{1}:(OI)(CI)M`"" -f $pair.P, $taskUser) -ForegroundColor Yellow
            $script:Problems += $pair.N
        }
    }
}

# --- 7. Protokolle ----------------------------------------------------------
Write-Host ""
Write-Host "7. Protokolle" -ForegroundColor Cyan
foreach ($pair in @(@{P = $taskLog; N = "logs\task-run.log (Ausgabe der Aufgabe)"},
                    @{P = $appLog;  N = "logs\cmkoffline2mail.log (Programm)"})) {
    if (Test-Path -LiteralPath $pair.P) {
        Write-Host "  --- $($pair.N), letzte 15 Zeilen ---" -ForegroundColor DarkGray
        Get-Content $pair.P -Tail 15 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    } else {
        Write-Host "  [--]   $($pair.N) existiert noch nicht" -ForegroundColor DarkYellow
        if ($pair.P -eq $taskLog) {
            Write-Host "         Die Aufgabe hat den Wrapper noch nie gestartet (Anmelderecht? Pfad?)." -ForegroundColor Yellow
        }
    }
}

# --- Ergebnis ---------------------------------------------------------------
Write-Host ""
if ($script:Problems.Count -eq 0) {
    Write-Host "Ergebnis: keine Probleme gefunden." -ForegroundColor Green
    Write-Host "Testlauf:  Start-ScheduledTask -TaskName '$TaskName'   danach:  Get-Content `"$taskLog`" -Tail 40"
} else {
    Write-Host "Ergebnis: $($script:Problems.Count) Punkt(e) zu klaeren:" -ForegroundColor Red
    $script:Problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
}
Write-Host ""
