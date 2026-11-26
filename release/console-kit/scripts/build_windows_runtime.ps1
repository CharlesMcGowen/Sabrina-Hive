param(
    [Parameter(Mandatory)][string] $Python,
    [Parameter(Mandatory)][string] $BuildDirectory
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
$BuildDirectory = [IO.Path]::GetFullPath($BuildDirectory)
if (Test-Path -LiteralPath $BuildDirectory) {
    throw 'Choose a new build directory.'
}
New-Item -ItemType Directory -Path $BuildDirectory | Out-Null
$desktop = Join-Path $repo 'sabrina_console_desktop'
$options = @(
    '--noconfirm', '--onedir', '--windowed', '--name', 'sabrina-console',
    '--distpath', (Join-Path $BuildDirectory 'dist'),
    '--workpath', (Join-Path $BuildDirectory 'cache'),
    '--specpath', $BuildDirectory,
    '--paths', $repo, '--paths', $desktop,
    '--add-data', "${desktop}/sabrina_desktop_ui.html:.",
    '--add-data', "${desktop}/santa-workshop.jpg:.",
    '--add-data', "$repo/swarm_console/assets:swarm_console/assets",
    '--hidden-import', 'webview.platforms.edgechromium',
    '--hidden-import', 'webview.platforms.winforms',
    '--exclude-module', 'torch', '--exclude-module', 'tensorflow',
    '--exclude-module', 'freqtrade',
    (Join-Path $desktop 'sabrina_desktop_ui.py')
)
& $Python -m PyInstaller @options
if ($LASTEXITCODE) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
& $Python (Join-Path $PSScriptRoot 'build_github_release.py') --runtime-dir (Join-Path $BuildDirectory 'dist/sabrina-console')
if ($LASTEXITCODE) { throw "Beta archive creation failed with exit code $LASTEXITCODE." }
