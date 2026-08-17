[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$serverPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "server.py"))
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "server.py was not found next to install.ps1: $serverPath"
}

$pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $pythonCommand) {
    throw "Python 3.10 or newer was not found on PATH."
}

$pythonExe = (& $pythonCommand.Source -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not [System.IO.Path]::IsPathRooted($pythonExe) -or -not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Python did not report a valid absolute executable path."
}

$pythonVersionOk = & $pythonExe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "DGS Notepad++ MCP requires Python 3.10 or newer."
}

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $codexCommand) {
    throw "The Codex CLI was not found on PATH."
}

$null = & $codexCommand.Source mcp get dgs-npp 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Existing Codex MCP registration found: dgs-npp"
} else {
    & $codexCommand.Source mcp add dgs-npp -- $pythonExe $serverPath
    if ($LASTEXITCODE -ne 0) {
        throw "codex mcp add dgs-npp failed."
    }
    Write-Host "Registered dgs-npp with Python: $pythonExe"
    Write-Host "Registered server: $serverPath"
}

$requests = @(
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"dgs-npp-installer","version":"1.0"}}}'
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
)
$probeErrorPath = Join-Path ([System.IO.Path]::GetTempPath()) ("dgs-npp-install-" + [guid]::NewGuid().ToString("N") + ".log")
$probeErrorAction = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $probeOutput = @($requests | & $pythonExe $serverPath 2> $probeErrorPath)
    $probeExitCode = $LASTEXITCODE
    $probeError = if (Test-Path -LiteralPath $probeErrorPath) {
        Get-Content -LiteralPath $probeErrorPath -Raw
    } else {
        ""
    }
} finally {
    $ErrorActionPreference = $probeErrorAction
    if (Test-Path -LiteralPath $probeErrorPath) {
        Remove-Item -LiteralPath $probeErrorPath -Force
    }
}

if ($probeExitCode -ne 0) {
    throw "The MCP stdio probe failed with exit code ${probeExitCode}: $probeError"
}

$responses = @($probeOutput | ForEach-Object { $_ | ConvertFrom-Json })
$initializeResponse = $responses | Where-Object { $_.id -eq 1 } | Select-Object -First 1
$toolsResponse = $responses | Where-Object { $_.id -eq 2 } | Select-Object -First 1
if ($null -eq $initializeResponse -or $initializeResponse.result.serverInfo.name -ne "dgs_npp_mcp") {
    throw "The MCP initialize probe returned an unexpected response."
}
if ($null -eq $toolsResponse) {
    throw "The MCP tools/list probe returned no response."
}

$expectedTools = @(
    "dgs_open_file"
    "dgs_read_file"
    "dgs_write_file"
    "dgs_search"
    "dgs_apply_patch"
    "dgs_list_open_files"
    "dgs_shutdown"
)
$actualTools = @($toolsResponse.result.tools | ForEach-Object { $_.name })
$missingTools = @($expectedTools | Where-Object { $_ -notin $actualTools })
$unexpectedTools = @($actualTools | Where-Object { $_ -notin $expectedTools })
if ($actualTools.Count -ne $expectedTools.Count -or $missingTools.Count -ne 0 -or $unexpectedTools.Count -ne 0) {
    throw "Unexpected MCP tool contract. Missing: $($missingTools -join ', '); unexpected: $($unexpectedTools -join ', ')."
}

Write-Host "MCP stdio probe passed: initialize + tools/list, 7 tools."
Write-Host "请重启 Codex，再创建新任务或 Fork 旧任务。"
