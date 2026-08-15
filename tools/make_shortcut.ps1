<#
.SYNOPSIS
    Create (or refresh) the "Transit Calculator" desktop shortcut.

.DESCRIPTION
    Run this once after cloning the project onto a machine, or after moving the
    folder. It points a desktop shortcut at start_transit.bat IN THIS COPY of the
    project, resolved from the script's own location - so there is no path to edit
    and no way for it to point at a folder that has moved.

    Re-running it overwrites the existing shortcut rather than making a second one,
    so it is safe to run whenever you are not sure it is still right.

    Adapted from the companion ASV console's make_shortcut.ps1, which is where the
    two non-obvious bits come from: resolving the Desktop through GetFolderPath (a
    OneDrive-backed profile redirects it, and $env:USERPROFILE\Desktop silently
    names a folder that does not exist there), and verifying by reading the shortcut
    back rather than trusting Save().

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1 -Name "Transit Calculator (8079)" -Arguments "--port 8079"
    Makes a SECOND shortcut beside the first, on another port, so two can run at
    once. start_transit.bat forwards its arguments to server.py, so --port and
    --no-browser both work here.

.NOTES
    Creates nothing outside the Desktop folder and writes nothing into the project.
    Delete the .lnk to undo it completely.

    The .lnk is deliberately NOT tracked in the repository: it is a binary carrying
    absolute paths for one machine, so a committed copy would be wrong for every
    other clone. This script plus tools\transit.ico are what the repo carries, and
    between them the shortcut is reproducible anywhere. tools\make_icon.py redraws
    the icon, so even that is not an undocumented binary.
#>
[CmdletBinding()]
param(
    # Shortcut file name, without the .lnk extension.
    [string] $Name = 'Transit Calculator',

    # Passed straight through to start_transit.bat -> server.py.
    [string] $Arguments = '',

    # Where to put it. Defaults to the Desktop; GetFolderPath resolves a
    # OneDrive-redirected Desktop correctly.
    [string] $DesktopPath = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'

# Resolve the project from THIS SCRIPT's location: tools\ -> repo root.
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'start_transit.bat'
$icon = Join-Path $root 'tools\transit.ico'

if (-not (Test-Path -LiteralPath $target)) {
    throw "start_transit.bat not found at $target - is make_shortcut.ps1 still inside the project's tools\ folder?"
}
if (-not (Test-Path -LiteralPath $DesktopPath)) {
    throw "Desktop folder not found at $DesktopPath - pass -DesktopPath explicitly."
}

$link = Join-Path $DesktopPath "$Name.lnk"
$existed = Test-Path -LiteralPath $link

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $target
$sc.Arguments = $Arguments
$sc.WorkingDirectory = $root
$sc.Description = 'Plan a vessel transit: chart background, currents, weather, fuel and export (opens in your browser)'
$sc.WindowStyle = 1
# Only claim the icon if it is actually there; a missing IconLocation makes
# Explorer fall back to a blank page icon rather than the .bat's own.
if (Test-Path -LiteralPath $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()

# Verify by reading the shortcut back, rather than trusting Save() - a bad path
# saves happily and only fails when double-clicked.
$check = $shell.CreateShortcut($link)
if ($check.TargetPath -ne $target) {
    throw "Shortcut saved but points at '$($check.TargetPath)' instead of '$target'."
}
if ($check.WorkingDirectory -ne $root) {
    throw "Shortcut saved but starts in '$($check.WorkingDirectory)' instead of '$root'."
}

Write-Host ("{0} shortcut: {1}" -f $(if ($existed) { 'Updated' } else { 'Created' }), $link)
Write-Host ("  -> {0}" -f $check.TargetPath)
if ($Arguments) { Write-Host ("  args: {0}" -f $Arguments) }
Write-Host '  Double-click it; the server starts and opens the console in your browser.'
