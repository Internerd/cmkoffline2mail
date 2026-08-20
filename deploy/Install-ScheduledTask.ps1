<#
.SYNOPSIS
    Richtet cmkoffline2mail als geplante Aufgabe unter einem gMSA-Konto ein.

.DESCRIPTION
    Legt das Datenverzeichnis an, setzt die NTFS-Rechte (nur SYSTEM, Administratoren
    und das gMSA duerfen lesen/schreiben) und registriert die Aufgabe, die das
    Skript in festen Abstaenden ausfuehrt.

    Muss in einer administrativen PowerShell auf dem Zielserver laufen.
    Das gMSA muss auf diesem Server installiert (Install-ADServiceAccount) und
    im Recht "Als Stapelverarbeitungsauftrag anmelden" enthalten sein.

.EXAMPLE
    .\Install-ScheduledTask.ps1 -GmsaAccount "CONTOSO\svc-cmkmail$" -IntervalMinutes 15
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $GmsaAccount,                                        # z. B. "CONTOSO\svc-cmkmail$"

    [string] $TaskName        = "cmkoffline2mail",
    [string] $InstallDir      = "C:\Program Files\cmkoffline2mail",
    [string] $DataDir         = "C:\ProgramData\cmkoffline2mail",
    [string] $PythonExe       = "C:\Program Files\Python311\python.exe",
    [int]    $IntervalMinutes = 15,
    [int]    $TimeLimitMinutes = 30
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python wurde unter '$PythonExe' nicht gefunden. Bitte -PythonExe angeben."
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "cmkoffline2mail\cli.py"))) {
    throw "In '$InstallDir' liegt kein cmkoffline2mail. Bitte das Paket zuerst dorthin kopieren."
}

# --- gMSA pruefen -----------------------------------------------------------
$accountName = $GmsaAccount.Split('\')[-1]
if (Get-Command Test-ADServiceAccount -ErrorAction SilentlyContinue) {
    if (-not (Test-ADServiceAccount -Identity $accountName.TrimEnd('$'))) {
        throw "Das gMSA '$GmsaAccount' ist auf diesem Server nicht nutzbar. Bitte zuerst 'Install-ADServiceAccount $($accountName.TrimEnd('$'))' ausfuehren."
    }
    Write-Host "gMSA '$GmsaAccount' ist auf diesem Server einsatzbereit." -ForegroundColor Green
} else {
    Write-Warning "RSAT/ActiveDirectory-Modul nicht vorhanden - gMSA konnte nicht geprueft werden."
}

# --- Datenverzeichnis und Rechte -------------------------------------------
if (-not (Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    Write-Host "Datenverzeichnis '$DataDir' angelegt."
}

# Vererbung entfernen, dann gezielt berechtigen: Konfiguration enthaelt Passwoerter.
icacls $DataDir /inheritance:r | Out-Null
icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F"        | Out-Null   # SYSTEM
icacls $DataDir /grant:r "*S-1-5-32-544:(OI)(CI)F"    | Out-Null   # Administratoren
icacls $DataDir /grant:r "$($GmsaAccount):(OI)(CI)M"  | Out-Null   # gMSA: Aendern
Write-Host "NTFS-Rechte auf '$DataDir' gesetzt (SYSTEM, Administratoren, $GmsaAccount)."

$configPath = Join-Path $DataDir "config.toml"
if (-not (Test-Path -LiteralPath $configPath)) {
    $example = Join-Path $InstallDir "config\config.example.toml"
    if (Test-Path -LiteralPath $example) {
        Copy-Item $example $configPath
        Copy-Item (Join-Path $InstallDir "config\mailtemplate.example.toml") (Join-Path $DataDir "mailtemplate.toml")
        Write-Warning "Beispielkonfiguration nach '$configPath' kopiert - bitte anpassen!"
    }
}

# --- Aufgabe registrieren ---------------------------------------------------
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m cmkoffline2mail --config `"$configPath`"" `
    -WorkingDirectory $InstallDir

# Alle $IntervalMinutes Minuten, dauerhaft
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# LogonType Password ist bei gMSA korrekt: das Kennwort holt Windows selbst aus dem AD.
$principal = New-ScheduledTaskPrincipal -UserId $GmsaAccount -LogonType Password -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Vorhandene Aufgabe '$TaskName' entfernt."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Meldet per Mail, wenn Hosts in bestimmten Checkmk-Ordnern zu lange offline sind." `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Write-Host ""
Write-Host "Aufgabe '$TaskName' wurde registriert (alle $IntervalMinutes Minuten)." -ForegroundColor Green
Write-Host "Naechste Schritte:" -ForegroundColor Cyan
Write-Host "  1. '$configPath' anpassen (Checkmk-URL, Automationsbenutzer, SMTP, Ordner)"
Write-Host "  2. Konfiguration pruefen:  & '$PythonExe' -m cmkoffline2mail -c '$configPath' --validate"
Write-Host "  3. Testlauf ohne Versand:  & '$PythonExe' -m cmkoffline2mail -c '$configPath' --dry-run"
Write-Host "  4. Aufgabe starten:        Start-ScheduledTask -TaskName '$TaskName'"
