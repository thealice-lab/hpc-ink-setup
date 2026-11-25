<#
.SYNOPSIS
Run the TENSOR file bridge from a Windows laptop.
.DESCRIPTION
Copy tensor-bridge.example.json to tensor-bridge.local.json and edit its three
settings. Use Windows PowerShell 5.1. Authentication stays in native OpenSSH;
the optional local dialog never saves password or MFA responses to disk.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action check
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action preview -InputFile C:\Research\sample_1.gjf
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action resume -Receipt C:\Research\sample_1.gjf.inkly.json
#>
[CmdletBinding()]
param(
    [ValidateSet('check', 'connect', 'run', 'preview', 'resume', 'status', 'fetch')]
    [string]$Action = 'run',
    [string]$InputFile,
    [string]$Receipt,
    [string]$Out,
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'tensor-bridge.local.json'),
    [ValidateRange(1, 336)]
    [int]$Hours = 1,
    [ValidateRange(1, 2147483647)]
    [int]$Cpus,
    [ValidateRange(1, 2147483647)]
    [int]$MemoryMiB,
    [string]$Python,
    [string]$CacheDirectory = (Join-Path $env:LOCALAPPDATA 'Inkly\Ssh')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$exitCode = 1
$savedEnvironment = @{}

try {
    $isPreparation = $Action -in @('run', 'preview')
    $isRecovery = $Action -in @('resume', 'status', 'fetch')
    if ($isPreparation -and -not $InputFile) {
        throw 'Use -InputFile with the path to your exported .gjf or .com file.'
    }
    if ($isRecovery -and -not $Receipt) {
        throw 'Use -Receipt with the recovery .inkly.json printed by the original run.'
    }
    if ($Action -eq 'fetch' -and -not $Out) {
        throw 'Use -Out to choose the local directory for the fetched log.'
    }
    if (-not $isPreparation) {
        foreach ($option in @('InputFile', 'Hours', 'Cpus', 'MemoryMiB')) {
            if ($PSBoundParameters.ContainsKey($option)) {
                throw "-$option applies only to run or preview. Resume retains the recorded resources."
            }
        }
    }

    # Recovery always uses the recorded destination, never a newly edited host.
    $settingsPath = if ($isRecovery) { $Receipt } else { $ConfigPath }
    if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
        throw 'Settings/receipt file not found. For a new connection, copy tensor-bridge.example.json to tensor-bridge.local.json and edit it.'
    }
    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    if ($settings -isnot [System.Management.Automation.PSCustomObject]) {
        throw 'Settings must be a JSON object.'
    }
    if (-not $isRecovery) {
        foreach ($property in $settings.PSObject.Properties.Name) {
            if ($property -notin @('host', 'ink_path', 'auth')) {
                throw 'Connection settings accept only host, ink_path and auth. Keep credentials out of this file.'
            }
        }
    }
    foreach ($property in @('host', 'ink_path')) {
        if (-not $settings.PSObject.Properties[$property] -or $settings.$property -isnot [string]) {
            throw "Settings require a string named $property."
        }
    }
    $target = $settings.host
    $inkPath = $settings.ink_path
    if ($target -notmatch '^[A-Za-z0-9][A-Za-z0-9_.@-]*$' -or $target -match 'YOUR_') {
        throw 'Replace host with your own SSH alias or username@hostname, without spaces or SSH options.'
    }
    if ($inkPath -notmatch '^(?:/|~/)[A-Za-z0-9_./-]+$' -or $inkPath.Split('/') -contains '..') {
        throw 'ink_path must be an absolute or ~/ path to the installed HPC Inkly launcher.'
    }
    $auth = if ($settings.PSObject.Properties['auth']) { $settings.auth } else { 'configured' }
    if ($auth -notin @('keyboard-interactive', 'configured')) {
        throw 'auth must be keyboard-interactive or configured.'
    }

    $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    if (-not (Test-Path -LiteralPath $ssh -PathType Leaf)) {
        throw 'Install the Windows OpenSSH Client optional feature, then retry.'
    }
    $pythonPrefix = @()
    if (-not $Python) {
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            $Python = (Get-Command py.exe).Source
            $pythonPrefix = @('-3')
        } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
            $Python = (Get-Command python.exe).Source
        } else {
            throw 'Install Python 3.9 or newer, or supply its executable with -Python.'
        }
    }
    & $Python @pythonPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'A working Python 3.9 or newer is required. Select it with -Python.'
    }

    if ($auth -eq 'keyboard-interactive') {
        if ($PSVersionTable.PSEdition -ne 'Desktop') {
            throw 'Use Windows PowerShell 5.1 (powershell.exe) to build the SSH sign-in dialog.'
        }
        $source = Join-Path $PSScriptRoot 'ssh_askpass.cs'
        # Work even when the parent shell's module path omits Get-FileHash.
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $sourceHash = ([BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($source)))).Replace('-', '').Substring(0, 12)
        } finally {
            $hasher.Dispose()
        }
        $dialog = Join-Path $CacheDirectory ("InklySshAskpass-$sourceHash.exe")
        if (-not (Test-Path -LiteralPath $dialog -PathType Leaf)) {
            New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null
            Add-Type -Path $source -ReferencedAssemblies System.Windows.Forms.dll,System.Drawing.dll -OutputAssembly $dialog -OutputType WindowsApplication
        }
        foreach ($name in @('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'INKLY_SSH_TARGET')) {
            $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        $env:SSH_ASKPASS = $dialog
        $env:SSH_ASKPASS_REQUIRE = 'force'
        $env:INKLY_SSH_TARGET = $target
    }

    if ($Action -eq 'check') {
        Write-Output 'Local settings, Python and OpenSSH are ready. No network connection or job submission was made.'
        $exitCode = 0
    } elseif ($Action -eq 'connect') {
        # First contact may ask about the host key; the user verifies its fingerprint.
        $sshArguments = @('-tt', '-o', 'StrictHostKeyChecking=ask', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3')
        if ($auth -eq 'keyboard-interactive') {
            $sshArguments += @('-o', 'PreferredAuthentications=keyboard-interactive', '-o', 'PubkeyAuthentication=no')
        }
        & $ssh @sshArguments $target
        $exitCode = $LASTEXITCODE
    } else {
        $bridgePath = Join-Path (Split-Path $PSScriptRoot -Parent) 'tensor_bridge.py'
        $bridgeArguments = @($bridgePath, '--ssh', $ssh)
        if ($isPreparation) {
            $bridgeArguments += @('run', $InputFile, '--host', $target, '--ink-path', $inkPath, '--auth', $auth, '--hours', "$Hours")
            if ($Action -eq 'preview') { $bridgeArguments += '--preview' }
            if ($Receipt) { $bridgeArguments += @('--receipt', $Receipt) }
            if ($PSBoundParameters.ContainsKey('Cpus')) { $bridgeArguments += @('--cpus', "$Cpus") }
            if ($PSBoundParameters.ContainsKey('MemoryMiB')) { $bridgeArguments += @('--memory-mib', "$MemoryMiB") }
        } else {
            $bridgeArguments += @($Action, $Receipt)
        }
        if ($Out) { $bridgeArguments += @('--out', $Out) }
        & $Python @pythonPrefix @bridgeArguments
        $exitCode = $LASTEXITCODE
    }
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
exit $exitCode
