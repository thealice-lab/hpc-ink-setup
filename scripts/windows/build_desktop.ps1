<# Build the Windows app using the supplied isolated Python environment. #>
param(
    [string]$Python = '.\.build-venv\Scripts\python.exe',
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$build = Join-Path $repo 'build'
$dist = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $repo 'dist' }
New-Item -ItemType Directory -Path $build -Force | Out-Null
$dialog = Join-Path $build 'InklySshAskpass.exe'
if (Test-Path -LiteralPath $dialog) {
    # The target is the single generated binary inside this checkout's build dir.
    Remove-Item -LiteralPath $dialog
}
Add-Type -Path (Join-Path $PSScriptRoot 'ssh_askpass.cs') -ReferencedAssemblies System.Windows.Forms.dll,System.Drawing.dll -OutputAssembly $dialog -OutputType WindowsApplication
$buildWorkspace = Join-Path $build ('pyinstaller-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $buildWorkspace -Force | Out-Null
# Setting up Inkly on the HPC installs exactly the commit this app is built from.
$commit = (& git -C $repo rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Cannot read the source commit; build from a Git checkout.' }
$version = (Select-String -Path (Join-Path $repo 'inkly\__init__.py') -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$buildInfo = Join-Path $buildWorkspace 'desktop_build.json'
[IO.File]::WriteAllText($buildInfo, ('{"version":"' + $version + '","commit":"' + $commit + '"}'), (New-Object Text.UTF8Encoding $false))
$setupScript = Join-Path $repo 'scripts\desktop_hpc_setup.sh'
& $Python -m PyInstaller --noconfirm --onefile --windowed --noupx --name InklyDesktop --paths $repo --distpath $dist --workpath $buildWorkspace --specpath $buildWorkspace --add-binary "$dialog;." --add-data "$setupScript;." --add-data "$buildInfo;." (Join-Path $repo 'scripts\tensor_desktop.py')
if ($LASTEXITCODE -ne 0) { throw 'Desktop executable build failed' }
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $digest = ([BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes((Join-Path $dist 'InklyDesktop.exe'))))).Replace('-', '')
    Write-Output ("SHA256: " + $digest)
    Write-Output ("Source commit: " + $commit)
} finally {
    $hasher.Dispose()
}
