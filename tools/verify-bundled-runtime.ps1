param(
    [ValidateSet("All", "Normal", "Headless")]
    [string]$Mode = "All",
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path (Join-Path $PSScriptRoot "..\runtime\notepad-plus-plus-headless")).Path
$probe = (Resolve-Path (Join-Path $PSScriptRoot "probe.txt")).Path
$script = Join-Path $PSScriptRoot "verify-notepadpp-headless.ps1"

$arguments = @(
    "-ExePath", (Join-Path $runtime "notepad++.exe"),
    "-ProbeFile", $probe,
    "-Mode", $Mode
)
if ($Json) {
    $arguments += "-Json"
}
& powershell -NoProfile -ExecutionPolicy Bypass -File $script @arguments
exit $LASTEXITCODE
