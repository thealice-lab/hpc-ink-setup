<# Download, verify, locally test and launch the pinned Inkly Desktop release.
No credentials, SSH connections, Slurm operations or HPC deployment. #>
#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ArtifactZip,
    [switch]$NoLaunch
)

$repo = 'thealice-lab/hpc-ink-setup'
$version = '0.4.0'
$releaseTag = "desktop-v$version"
$zipName = "InklyDesktop-$version-windows.zip"
# Public release downloads need no GitHub sign-in and do not expire.
$releaseUrl = "https://github.com/$repo/releases/tag/$releaseTag"
$downloadUrl = "https://github.com/$repo/releases/download/$releaseTag/$zipName"
$sourceCommit = 'bd246f829793010c8c8ea0e1ef9e0a7b3aa29929'
$build = $version
# InklyDesktop.exe SHA-256 from the release's SHA256SUMS.txt.
$expectedSha = '110FD25DD45C4E63A7AAECE8EFC41BDBBED08BF5CB65ECEBAC1DAD40565B4658'

function Assert-InklyExeHash([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "InklyDesktop.exe was not found at: $Path"
    }
    # Get-FileHash may not be discoverable when 5.1 inherits a PowerShell 7
    # module path (including Windows CI). Use the bundled .NET implementation.
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try { $actual = ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '') }
        finally { $stream.Dispose() }
    } finally { $hasher.Dispose() }
    if ($actual -ne $expectedSha) {
        throw "SHA-256 mismatch. Refusing to run or replace this file: $Path. Download $zipName from $releaseUrl and pass -ArtifactZip with its exact ZIP path."
    }
    Write-Host "SHA-256 verified: $actual"
}

function Find-InklyArtifactZip {
    # Windows can relocate Downloads outside the profile (including OneDrive).
    $downloads = Join-Path $env:USERPROFILE 'Downloads'
    $folders = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -ErrorAction SilentlyContinue
    $configured = $folders.'{374DE290-123F-4565-9164-39C4925E467B}'
    if ($configured) { $downloads = [Environment]::ExpandEnvironmentVariables($configured) }
    Get-ChildItem -LiteralPath $downloads -Filter "InklyDesktop-$version-windows*.zip" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}

function Expand-InklyArtifact([string]$Zip, [string]$Directory) {
    if (-not (Test-Path -LiteralPath $Zip -PathType Leaf)) {
        throw "Artifact ZIP does not exist: $Zip"
    }
    # Extract only the known root entry, never arbitrary archive paths or files.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $Zip).Path)
    try {
        $entries = @($archive.Entries | Where-Object { $_.FullName -ceq 'InklyDesktop.exe' })
        if ($entries.Count -ne 1) { throw 'Expected exactly one InklyDesktop.exe at the root of the artifact ZIP.' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], (Join-Path $Directory 'InklyDesktop.exe'), $false)
    } finally {
        $archive.Dispose()
    }
}

function Save-InklyRelease([string]$Directory) {
    $zip = Join-Path $Directory $zipName
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    # Windows PowerShell 5.1 downloads far faster without its progress bar.
    $previous = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try { Invoke-WebRequest -Uri $downloadUrl -OutFile $zip -UseBasicParsing -TimeoutSec 300 }
    finally { $ProgressPreference = $previous }
    return $zip
}

function Get-InklyArtifact([string]$Zip, [string]$Directory) {
    if ($Zip) {
        Write-Host "Using ZIP: $Zip"
        Expand-InklyArtifact $Zip $Directory
        return
    }
    Write-Host "Downloading Inkly Desktop $version from the GitHub release..."
    try {
        Expand-InklyArtifact (Save-InklyRelease $Directory) $Directory
        return
    } catch {
        # Network or proxy failure: offer the same pinned release in the browser.
        Write-Host ('Automatic download failed: ' + $_.Exception.Message)
    }
    Write-Host "Open the release page and download ${zipName}: $releaseUrl"
    Write-Host 'No GitHub sign-in is needed.'
    try { Start-Process -FilePath $releaseUrl | Out-Null }
    catch { Write-Host 'Could not open the browser. Open the link above manually.' }
    $answer = Read-Host 'After downloading, paste the ZIP path (or press Enter to search Downloads; Q to cancel)'
    if ($answer -eq 'q') { throw 'Download cancelled; no application was launched.' }
    $Zip = $answer.Trim().Trim('"')
    if (-not $Zip) { $Zip = Find-InklyArtifactZip }
    if (-not $Zip) { throw "No $zipName found. Download it and rerun with -ArtifactZip and the exact path." }
    Expand-InklyArtifact $Zip $Directory
}

function Assert-InklyDestination([string]$Directory) {
    # Even a redirected LOCALAPPDATA must not install binaries into a Git checkout.
    $parent = [IO.DirectoryInfo]$Directory
    while ($parent) {
        if (Test-Path -LiteralPath (Join-Path $parent.FullName '.git')) {
            throw 'Refusing to install the Desktop build inside a Git checkout. Use your normal LOCALAPPDATA.'
        }
        if ($parent.Exists -and ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing a linked destination directory. Use your normal LOCALAPPDATA.'
        }
        $parent = $parent.Parent
    }
}

function Assert-InklySmokeReport($Report) {
    if ($Report.frozen -isnot [bool] -or -not $Report.frozen -or
        $Report.askpass -isnot [bool] -or -not $Report.askpass -or
        $Report.window -cne 'Inkly Desktop') {
        throw 'Application packaging checks failed. Do not use this EXE yet.'
    }
    if ($Report.ssh -isnot [bool] -or -not $Report.ssh) {
        throw 'Windows OpenSSH Client is unavailable. Enable it through Windows Optional Features, then rerun the helper.'
    }
}

function Invoke-InklySmokeTest([string]$Exe, [string]$ReportFile) {
    Write-Host 'Running local packaged-app smoke test (no HPC connection)...'
    $process = Start-Process -FilePath $Exe -ArgumentList @('--smoke-test', ('"' + $ReportFile + '"')) -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit(60000)) {
            # PyInstaller starts a child process; stop only this invocation's tree.
            & taskkill.exe /PID $process.Id /T /F *> $null
            throw 'Offline smoke test timed out after 60 seconds.'
        }
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $ReportFile -PathType Leaf)) {
            throw "Offline smoke test failed (exit $($process.ExitCode))."
        }
    } finally { $process.Dispose() }
    $report = Get-Content -LiteralPath $ReportFile -Raw | ConvertFrom-Json
    Assert-InklySmokeReport $report
    Write-Host "Smoke test passed: $ReportFile"
}

function Invoke-InklyDesktopTest([string]$Zip, [switch]$SkipLaunch) {
    $ErrorActionPreference = 'Stop'
    if (-not $env:LOCALAPPDATA -or -not [IO.Path]::IsPathRooted($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA must be an absolute path. Run on a normal Windows user profile.'
    }
    $destination = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Inkly\DesktopBuilds\$build"))
    Assert-InklyDestination $destination
    $exe = Join-Path $destination 'InklyDesktop.exe'
    Write-Host "Inkly Desktop $version (pre-release, unsigned) built from $($sourceCommit.Substring(0, 7))"
    Write-Host "Release: $releaseUrl"
    Write-Host "Local destination: $destination"
    if (Test-Path -LiteralPath $exe) { Assert-InklyExeHash $exe }
    if ($Zip -or -not (Test-Path -LiteralPath $exe)) {
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        $work = Join-Path $tempRoot ('inkly-desktop-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $work | Out-Null
        try {
            Get-InklyArtifact $Zip $work
            $candidate = Join-Path $work 'InklyDesktop.exe'
            Assert-InklyExeHash $candidate
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
            if (-not (Test-Path -LiteralPath $exe)) {
                # No-overwrite copy: a concurrent helper cannot replace an existing file.
                [IO.File]::Copy($candidate, $exe, $false)
            }
            Assert-InklyExeHash $exe
        } finally {
            # Only remove this invocation's generated direct child of the temp directory.
            $resolvedWork = [IO.Path]::GetFullPath($work)
            if ([IO.Path]::GetDirectoryName($resolvedWork) -eq $tempRoot -and
                [IO.Path]::GetFileName($resolvedWork).StartsWith('inkly-desktop-')) {
                try { Remove-Item -LiteralPath $resolvedWork -Force -Recurse -ErrorAction Stop }
                catch { Write-Warning 'Temporary download cleanup failed; the build outcome is unchanged.' -WarningAction Continue }
            }
        }
    }
    # A fresh report cannot reuse an old successful result or overwrite another run.
    $reportFile = Join-Path $destination ('local-smoke-test-' + [guid]::NewGuid().ToString('N') + '.json')
    Invoke-InklySmokeTest $exe $reportFile
    Write-Host "SUCCESS: verified EXE is at $exe"
    if (-not $SkipLaunch) {
        Write-Host 'Launching Inkly Desktop (opening the window does not submit jobs)...'
        Start-Process -FilePath $exe | Out-Null
    }
}

# Dot-sourcing exposes functions for isolated Windows regression tests.
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-InklyDesktopTest -Zip $ArtifactZip -SkipLaunch:$NoLaunch }
    catch { [Console]::Error.WriteLine('Inkly Desktop preparation failed: ' + $_.Exception.Message); exit 1 }
}
