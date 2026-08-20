<#
.SYNOPSIS
    Gibt einem Konto (z. B. einem gMSA) das Recht "Als Stapelverarbeitungsauftrag anmelden".

.DESCRIPTION
    Ohne dieses Recht startet die geplante Aufgabe nicht und quittiert mit
    "Fehler 0x1: Dem Benutzer wurde der angeforderte Anmeldetyp nicht gewaehrt".
    Das Skript exportiert die lokale Sicherheitsrichtlinie, ergaenzt das Konto
    in SeBatchLogonRight und spielt sie zurueck.

    Alternativ von Hand: secpol.msc > Lokale Richtlinien > Zuweisen von
    Benutzerrechten > "Anmelden als Stapelverarbeitungsauftrag".

.EXAMPLE
    .\Grant-BatchLogonRight.ps1 -Account "CONTOSO\svc-cmkmail$"
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string] $Account)

$ErrorActionPreference = "Stop"

$sid = (New-Object System.Security.Principal.NTAccount($Account)).Translate(
    [System.Security.Principal.SecurityIdentifier]).Value

$export = Join-Path $env:TEMP "secpol-export.inf"
$import = Join-Path $env:TEMP "secpol-import.inf"
secedit /export /areas USER_RIGHTS /cfg $export | Out-Null

$line = (Get-Content $export | Where-Object { $_ -like "SeBatchLogonRight*" })
if ($line -and $line -match [regex]::Escape($sid)) {
    Write-Host "'$Account' besitzt das Recht bereits." -ForegroundColor Green
    return
}

$value = if ($line) { "$line,*$sid" } else { "SeBatchLogonRight = *$sid" }
@(
    "[Unicode]"
    "Unicode=yes"
    "[Version]"
    'signature="$CHICAGO$"'
    "Revision=1"
    "[Privilege Rights]"
    $value
) | Set-Content -Path $import -Encoding Unicode

secedit /configure /db secedit.sdb /cfg $import /areas USER_RIGHTS | Out-Null
Remove-Item $export, $import -ErrorAction SilentlyContinue
Write-Host "'$Account' darf sich jetzt als Stapelverarbeitungsauftrag anmelden." -ForegroundColor Green
