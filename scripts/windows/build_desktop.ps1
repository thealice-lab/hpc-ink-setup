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
& $Python -m PyInstaller --noconfirm --onefile --windowed --noupx --name InklyDesktop --distpath $dist --workpath $buildWorkspace --specpath $buildWorkspace --add-binary "$dialog;." (Join-Path $repo 'scripts\tensor_desktop.py')
if ($LASTEXITCODE -ne 0) { throw 'Desktop executable build failed' }
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $digest = ([BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes((Join-Path $dist 'InklyDesktop.exe'))))).Replace('-', '')
    Write-Output ("SHA256: " + $digest)
} finally {
    $hasher.Dispose()
}
