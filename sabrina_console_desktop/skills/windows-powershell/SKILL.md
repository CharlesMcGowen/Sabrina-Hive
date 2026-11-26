---
name: windows-powershell
description: Execute secure read-only PowerShell metadata and inspection commands within workspace boundaries.
---

The powershell_info action retrieves basic system and environment metadata via discovery cmdlets.

The powershell_help action provides built-in usage documentation for allowed inspection commands.

The powershell_item action inspects file attributes and metadata using secure literal paths like `{"path":"Reports/settings.json"}`.

Help is limited to the three built-in names Get-Command, Get-Help and Get-Item.

Scope is read-only metadata.

The workspace boundary enforces strict path validation and prevents traversal outside the trusted root.

Cmdlet discovery is independent of tool permission.

Learn more about powershell executable parameters at [About PowerShell Exe](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1).

Learn more about command discovery at [Get Command](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/get-command?view=powershell-5.1).

Learn more about help documentation at [Get Help](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/get-help?view=powershell-5.1).

Learn more about file item handling at [Get Item](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/get-item?view=powershell-5.1).
