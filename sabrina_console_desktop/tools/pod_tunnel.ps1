# tools/pod_tunnel.ps1
#
# Opens a background SSH local port-forward to a rented Runpod pod's Ollama
# instance, WITHOUT ever exposing it publicly. Ollama has no auth of its own,
# so it must be bound to 127.0.0.1 on the pod (not 0.0.0.0) -- otherwise
# Runpod's own reverse-proxy domain (https://<pod-id>-11434.proxy.runpod.net)
# serves it to anyone on the internet who finds/guesses that URL. This script
# assumes that's already true; use pod_bind_local.ps1-equivalent SSH command
# below on the pod first if Ollama was started with OLLAMA_HOST=0.0.0.0:
#
#   ssh root@<ssh-host> -p <ssh-port> "pkill ollama; sleep 1; \
#     OLLAMA_HOST=127.0.0.1 OLLAMA_MODELS=/workspace/ollama setsid nohup \
#     ollama serve > /workspace/ollama_serve.log 2>&1 < /dev/null &"
#
# Usage:
#   . .\tools\pod_tunnel.ps1 -SshHost <public-ip> -SshPort <ssh-port> -LocalPort 11500
#   . .\tools\pod_env.ps1 -LocalPort 11500 -SmartVramGb 75
#
# Get <public-ip>/<ssh-port> from the pod's `ssh.direct` block (list-pods /
# get-pod via Runpod MCP) -- NOT the 11434/http port mapping, which for a
# Secure Cloud pod only reaches Runpod's public proxy domain, not a raw IP.

param(
    [Parameter(Mandatory = $true)][string]$SshHost,
    [Parameter(Mandatory = $true)][int]$SshPort,
    [int]$LocalPort = 11500,
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$RemotePort = 11434
)

$logPath = "$env:TEMP\sabrina_pod_tunnel.log"
$sshArgs = @(
    "-N", "-L", "${LocalPort}:127.0.0.1:${RemotePort}",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-i", $KeyPath,
    "root@$SshHost", "-p", "$SshPort"
)

$proc = Start-Process -FilePath "ssh" -ArgumentList $sshArgs -WindowStyle Hidden -RedirectStandardError $logPath -PassThru
Start-Sleep -Seconds 3

if ($proc.HasExited) {
    Write-Host "Tunnel process exited immediately -- check $logPath" -ForegroundColor Red
} else {
    Write-Host "Tunnel open: localhost:$LocalPort -> $SshHost (via SSH) -> 127.0.0.1:$RemotePort on the pod. PID $($proc.Id)."
    Write-Host "Verify: curl http://127.0.0.1:$LocalPort/api/tags"
    Write-Host "Close it: Stop-Process -Id $($proc.Id)"
}
