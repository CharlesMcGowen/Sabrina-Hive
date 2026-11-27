# Opens an SSH tunnel from this machine to the Sabrina Social API on ego.
# ego binds that API to 127.0.0.1 only, so it is reached through the tunnel.
# Local port 18087 -> ego 127.0.0.1:8087. Leave this window open while the console runs.
param([int]$LocalPort = 18087, [int]$RemotePort = 8087, [string]$Host_ = "ego")
$args = @("-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-L", "127.0.0.1:$($LocalPort):127.0.0.1:$($RemotePort)", $Host_)
while ($true) {
    Write-Host "[$(Get-Date -Format s)] opening tunnel 127.0.0.1:$LocalPort -> ego:127.0.0.1:$RemotePort"
    & ssh @args
    Write-Host "[$(Get-Date -Format s)] tunnel dropped; retrying in 10s"
    Start-Sleep -Seconds 10
}
