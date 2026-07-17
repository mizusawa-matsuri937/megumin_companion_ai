[CmdletBinding()]
param(
    [switch]$SkipQualityGate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This bootstrap script only supports Windows."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

function Find-UvExecutable {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return [string]$command.Source
    }

    $wingetLink = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"
    if (Test-Path -LiteralPath $wingetLink) {
        return $wingetLink
    }

    $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $packageRoot) {
        $candidate = Get-ChildItem -LiteralPath $packageRoot -Directory -Filter "astral-sh.uv_*" |
            ForEach-Object { Join-Path $_.FullName "uv.exe" } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if ($null -ne $candidate) {
            return [string]$candidate
        }
    }

    return $null
}

$script:UvPath = Find-UvExecutable
if ($null -eq $script:UvPath) {
    if ($null -eq (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "uv is missing and WinGet is unavailable. Install uv, then run this script again."
    }
    & winget install --id astral-sh.uv -e --source winget `
        --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet failed to install uv (exit code $LASTEXITCODE)."
    }
    $script:UvPath = Find-UvExecutable
    if ($null -eq $script:UvPath) {
        throw "uv was installed but could not be located. Open a new PowerShell window and retry."
    }
}

function Invoke-Uv {
    param(
        [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
        [string[]]$UvArguments
    )

    & $script:UvPath @UvArguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv $($UvArguments -join ' ') failed (exit code $LASTEXITCODE)."
    }
}

Write-Host "Repository: $repoRoot"
Invoke-Uv "--version"
Invoke-Uv "python" "install" "3.11"
Invoke-Uv "python" "update-shell"

# The uv cache and this repository can be on different Windows volumes.
# Copy mode avoids noisy hard-link fallbacks and is deterministic on either layout.
$env:UV_LINK_MODE = "copy"
Invoke-Uv "sync" "--frozen" "--all-groups" "--all-extras"

if (-not $SkipQualityGate) {
    Invoke-Uv "run" "pytest"
    Invoke-Uv "run" "ruff" "check" "."
    Invoke-Uv "run" "ruff" "format" "--check" "."
    Invoke-Uv "run" "mypy"
}

Invoke-Uv "run" "python" "--version"
Write-Host "Windows development environment is ready."
