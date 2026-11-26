$ErrorActionPreference = 'Stop'
$console = Join-Path $PSScriptRoot 'sabrina-console.exe'
if (-not (Test-Path -LiteralPath $console -PathType Leaf)) {
    throw 'Extract the entire Windows beta ZIP before launching Sabrina.'
}
Start-Process -FilePath $console -WorkingDirectory $PSScriptRoot
