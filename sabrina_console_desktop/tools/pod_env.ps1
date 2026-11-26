# tools/pod_env.ps1
#
# Points this PowerShell session's Sabrina env vars at a rented Runpod GPU
# pod's Ollama instance -- via a local SSH tunnel (see tools/pod_tunnel.ps1),
# never a public IP/proxy domain. Ollama has no authentication, so it must
# never be reachable from the open internet.
#
# Sourced, not executed -- env vars set with `$env:` only apply to the
# process that sets them, so this must be dot-sourced into the same shell
# you'll run sabrina_gemini_loop.py / sabrina_unified.py from, AFTER the
# tunnel from pod_tunnel.ps1 is already open:
#
#   . .\tools\pod_tunnel.ps1 -SshHost <public-ip> -SshPort <ssh-port>
#   . .\tools\pod_env.ps1 -LocalPort 11500 -SmartVramGb 75
#
# The model must already be pulled on the pod (ollama pull <model> over SSH)
# before Sabrina will find it -- this script only repoints the host, it does
# not install or tunnel anything itself.

param(
    [int]$LocalPort = 11500,
    [int]$SmartVramGb = 75  # A100-80GB default; use ~20 for a 24GB 4090 pod.
)

$env:SABRINA_LAB_HOST = "127.0.0.1"
$env:SABRINA_OLLAMA_PORT = "$LocalPort"
$env:SABRINA_OLLAMA_HOSTS = "127.0.0.1"
$env:SABRINA_MAX_SMART_VRAM_GB = "$SmartVramGb"

Write-Host "Sabrina now targets Ollama at http://127.0.0.1:${LocalPort} (tunneled to the pod; smart-tier VRAM cap: ${SmartVramGb}GB)"
Write-Host "Verify the tunnel + model are up: curl http://127.0.0.1:${LocalPort}/api/tags"
Write-Host "Revert to the lab host: Remove-Item Env:\SABRINA_LAB_HOST,Env:\SABRINA_OLLAMA_PORT,Env:\SABRINA_OLLAMA_HOSTS,Env:\SABRINA_MAX_SMART_VRAM_GB"
