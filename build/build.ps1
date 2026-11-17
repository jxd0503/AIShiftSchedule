$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = "C:\Program Files\Python312\python.exe"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if ($candidate) {
        $python = $candidate.Source
    }
}
if (-not (Test-Path $python)) {
    throw "Python 3.12 not found: C:\Program Files\Python312\python.exe（或把 python 加入 PATH）"
}
if (-not (Test-Path $venvPython)) {
    & $python -m venv (Join-Path $root ".venv")
}

# pip 的进度/版本提示会写 stderr，PowerShell 5 在 Stop 模式下会误判为失败
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "backend\requirements.txt")
$pipExit = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($pipExit -ne 0) {
    throw "pip install failed with exit code $pipExit"
}

if (Test-Path (Join-Path $root "dist\AIShiftSchedule.exe")) {
    Remove-Item -Force (Join-Path $root "dist\AIShiftSchedule.exe")
}
if (Test-Path (Join-Path $root "build\work")) {
    Remove-Item -Recurse -Force (Join-Path $root "build\work")
}

& (Join-Path $root ".venv\Scripts\pyinstaller.exe") `
    (Join-Path $root "build\app.spec") `
    --noconfirm `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build\work")

$exe = Join-Path $root "dist\AIShiftSchedule.exe"
if (-not (Test-Path $exe)) {
    throw "Packaging did not produce the exe"
}
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Built $exe ($size MB)"
