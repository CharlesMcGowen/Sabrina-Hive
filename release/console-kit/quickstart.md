# Sabrina Console Windows beta

This is a local Windows x64 beta. The desktop UI uses Microsoft Edge WebView2. The bundle includes Python and its desktop runtime.

1. Extract all files from `windows_beta.zip` into a new folder.
2. Run `install_beta.ps1` from PowerShell. It copies the bundle to `%LOCALAPPDATA%\SabrinaConsole\app` by default. Use `-Destination` to install to a different new folder.
3. Run `run_console.ps1` from the installation, or open `sabrina-console.exe` next to its `_internal` folder.

Open **Models & API Keys** to configure a model provider. A local Ollama installation can use `http://127.0.0.1:11434`. In **Swarm Monitor**, turn off **Allow Writes** for your first inspection mission and check the mission report after it finishes.

The installer copies application files only. The beta keeps its default workspace under `%LOCALAPPDATA%\SabrinaConsole\workspace`.
