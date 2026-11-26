# Windows x64 beta release report

The beta includes the frozen Sabrina desktop executable, its runtime files, and a per-user PowerShell installer. The build includes no saved provider profiles or workspace data. The launcher uses the executable next to the installed runtime.

The package requires Windows and the Microsoft Edge WebView2 Runtime. Model providers are configured in the console after launch.

## Verification scope

The release builder reopens the ZIP and checks its members and CRCs. A host smoke test extracts the ZIP, runs the installer into a new directory, and starts the installed app with a fresh application profile and a system-only PATH.

Clean VM installation and first-run model-provider verification remain pending. No release was published.
