<#
.SYNOPSIS
    Behebt "No module named cmkoffline2mail": traegt den Basisordner dauerhaft
    in den Suchpfad der virtuellen Umgebung ein.

.DESCRIPTION
    "python -m cmkoffline2mail" findet das Paket sonst nur, wenn das
    Arbeitsverzeichnis zufaellig der Basisordner ist. Dieses Skript schreibt eine
    .pth-Datei in das site-packages der venv; damit ist der Aufruf von ueberall
    moeglich - ohne pip, ohne Internetzugang.

    Install-ScheduledTask.ps1 erledigt das ebenfalls. Dieses Skript ist der
    schnelle Weg fuer eine bestehende Installation.

.EXAMPLE
    .\Register-PackagePath.ps1

.EXAMPLE
    .\Register-PackagePath.ps1 -InstallDir "C:\FIRMA\cmkoffline2mail"
#>
[CmdletBinding()]
param(
    [string] $InstallDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "cmkoffline2mail\__init__.py"))) {
    Write-Host "Inhalt von '$InstallDir':" -ForegroundColor Yellow
    Get-ChildItem -LiteralPath $InstallDir | ForEach-Object { Write-Host "  $($_.Name)" }
    throw "In '$InstallDir' fehlt das Python-Paket (erwartet: cmkoffline2mail\__init__.py)."
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtuelle Umgebung nicht gefunden: $venvPython"
}

$sitePackages = ("$(& $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")").Trim()
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
Write-Host "Eingetragen: $pthFile" -ForegroundColor Green
Write-Host "        ->  $InstallDir"

# Gegenprobe aus einem fremden Arbeitsverzeichnis
Push-Location $env:SystemRoot
try {
    $out = & $venvPython -m cmkoffline2mail --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Import weiterhin nicht moeglich: $out" }
    Write-Host "Gegenprobe aus $env:SystemRoot : $out" -ForegroundColor Green
} finally {
    Pop-Location
}
