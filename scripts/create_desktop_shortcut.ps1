# PowerShell script to create/update the "Sabrina Operator Console" Desktop shortcut
$TargetBat = "c:\Users\charl\OneDrive\Documents\Sabrina\Launch_Sabrina_Console.bat"
$WorkingDir = "c:\Users\charl\OneDrive\Documents\Sabrina"

# Discover desktop directory robustly across OneDrive and local profiles
$DesktopCandidates = @(
    [Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop),
    (Join-Path $env:USERPROFILE "Desktop"),
    (Join-Path $env:USERPROFILE "OneDrive\Desktop"),
    (Join-Path $env:USERPROFILE "OneDrive - Personal\Desktop")
)

$Desktop = $null
foreach ($cand in $DesktopCandidates) {
    if (![string]::IsNullOrWhiteSpace($cand) -and (Test-Path -LiteralPath $cand)) {
        $Desktop = $cand
        break
    }
}

if (!$Desktop) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}

$ShortcutPath = Join-Path $Desktop "Sabrina Operator Console.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetBat
$Shortcut.WorkingDirectory = $WorkingDir
$Shortcut.WindowStyle = 1 # Normal window
$Shortcut.Description = "Launch latest Sabrina Operator Console"
$Shortcut.Save()

Write-Host "Desktop shortcut created/updated successfully at: $ShortcutPath" -ForegroundColor Green

