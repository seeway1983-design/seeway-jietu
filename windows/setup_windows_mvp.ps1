param(
    [switch]$SkipVenv
)

$ErrorActionPreference = "Stop"

function Write-Section($title) {
    Write-Host ""
    Write-Host "== $title ==" -ForegroundColor Cyan
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $ProjectRoot ".venv"
$Requirements = Join-Path $ProjectRoot "requirements-windows.txt"
$DoctorCmd = "python `"$ProjectRoot\run_mvp.py`" --doctor"

Write-Section "Project"
Write-Host "Project root: $ProjectRoot"

Write-Section "Host Checks"
$checks = [ordered]@{
    "python" = (Test-Command "python")
    "node"   = (Test-Command "node")
    "npm"    = (Test-Command "npm")
    "adb"    = (Test-Command "adb")
}

foreach ($item in $checks.GetEnumerator()) {
    $status = if ($item.Value) { "ok" } else { "missing" }
    Write-Host ("{0}: {1}" -f $item.Key, $status)
}

if (-not $checks["python"]) {
    throw "缺少 python。请先在 Windows 安装 Python 3.10+。"
}

if (-not $SkipVenv) {
    Write-Section "Virtualenv"
    if (-not (Test-Path $VenvDir)) {
        python -m venv $VenvDir
    }
    & (Join-Path $VenvDir "Scripts\python.exe") -m pip install --upgrade pip
    & (Join-Path $VenvDir "Scripts\python.exe") -m pip install -r $Requirements
    $env:PATH = (Join-Path $VenvDir "Scripts") + ";" + $env:PATH
}

Write-Section "Python Packages"
python -m pip show Appium-Python-Client adbutils uiautomator2 opencv-python pillow loguru 2>$null | Select-String "^(Name|Version):" | ForEach-Object { $_.Line }

Write-Section "Appium"
if (Test-Command "appium") {
    appium --version
} else {
    Write-Host "appium: missing"
    Write-Host "建议执行: npm install -g appium"
}

Write-Section "ADB Devices"
if (Test-Command "adb") {
    adb devices
} else {
    Write-Host "adb: missing"
    Write-Host "建议安装 Android Platform Tools。"
}

Write-Section "Doctor"
Invoke-Expression $DoctorCmd

Write-Section "Next"
Write-Host "1. 启动 MuMu/雷电"
Write-Host "2. 在模拟器里打开朴朴 App 并登录"
Write-Host "3. 确认 adb devices 能看到模拟器"
Write-Host "4. 再次执行: python .\run_mvp.py --doctor"
