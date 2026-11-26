param(
    [string] $Destination = (Join-Path $env:LOCALAPPDATA 'SabrinaConsole\app')
)

$ErrorActionPreference = 'Stop'
$Destination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    throw "Choose a new installation directory. This path already exists: $Destination"
}

$files = @('sabrina-console.exe', '_internal', 'quickstart.md', 'release_report.md', '.env.example', 'run_console.ps1')
foreach ($name in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name))) {
        throw "Package input is missing: $name"
    }
}
New-Item -ItemType Directory -Path $Destination | Out-Null
foreach ($name in $files) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $Destination -Recurse
}
Write-Output "Installed Sabrina Console in $Destination"
