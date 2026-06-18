# ============================================================
#  VCoreX Trading Bot — PowerShell Launcher
#  File: start_bot.ps1
#  Dat file nay tai: D:\vcorex_C206_12_06_ADX_THAN_NEN_1\
#
#  Cach chay:
#    1. Chuot phai -> Run with PowerShell
#    2. Hoac: powershell -ExecutionPolicy Bypass -File start_bot.ps1
# ============================================================

$BOT_DIR   = "D:\vcorex_C206_12_06_ADX_THAN_NEN_1"
$PYTHON    = "$BOT_DIR\venv\Scripts\python.exe"
$MAIN      = "$BOT_DIR\main.py"
$LOG_DIR   = "$BOT_DIR\logs"
$ENV_FILE  = "$BOT_DIR\.env"

function Write-Header  { Write-Host ("=" * 60) -ForegroundColor Cyan }
function Write-OK      { param($msg) Write-Host "[OK]  $msg" -ForegroundColor Green }
function Write-WARN    { param($msg) Write-Host "[!!]  $msg" -ForegroundColor Yellow }
function Write-ERR     { param($msg) Write-Host "[XX]  $msg" -ForegroundColor Red }
function Write-INFO    { param($msg) Write-Host "[--]  $msg" -ForegroundColor White }

# --- BANNER ---
Clear-Host
Write-Header
Write-Host "   VCoreX Institutional Trading Bot" -ForegroundColor Cyan
Write-Host "   Powered by Python + OKX API + Telegram" -ForegroundColor DarkGray
Write-Header
Write-Host ""

# --- BUOC 1: Kiem tra thu muc goc ---
if (-not (Test-Path $BOT_DIR)) {
    Write-ERR "Khong tim thay thu muc bot: $BOT_DIR"
    Read-Host "Nhan Enter de thoat"
    exit 1
}
Write-OK "Thu muc bot: $BOT_DIR"

# --- BUOC 2: Kiem tra Python venv ---
if (-not (Test-Path $PYTHON)) {
    Write-ERR "Khong tim thay Python venv: $PYTHON"
    Write-WARN "Hay chay: python -m venv venv"
    Write-WARN "Sau do  : venv\Scripts\pip install -r requirements.txt"
    Read-Host "Nhan Enter de thoat"
    exit 1
}
$pyVersion = & $PYTHON --version 2>&1
Write-OK "Python: $pyVersion"

# --- BUOC 3: Kiem tra file .env ---
if (-not (Test-Path $ENV_FILE)) {
    Write-ERR "Khong tim thay file .env: $ENV_FILE"
    Write-WARN "Hay copy .env.example -> .env va dien API keys"
    Read-Host "Nhan Enter de thoat"
    exit 1
}
Write-OK "File .env: OK"

# --- BUOC 4: Tao thu muc logs neu chua co ---
if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
    Write-OK "Tao thu muc logs: $LOG_DIR"
} else {
    Write-OK "Thu muc logs: $LOG_DIR"
}

# --- THONG TIN CAU HINH ---
Write-Host ""
Write-INFO "Cau hinh:"
Write-INFO "  Auto-restart khi crash : Co (toi da 10 lan)"
Write-INFO "  Delay giua cac lan restart : 5 giay"
Write-INFO "  Log file: $LOG_DIR\bot_<ngay>.log"
Write-Host ""
Write-Header
Write-Host ""

# --- VONG LAP AUTO-RESTART ---
$restartCount = 0
$maxRestarts  = 10
$restartDelay = 5

while ($true) {

    $logFile   = "$LOG_DIR\bot_$(Get-Date -Format 'yyyy-MM-dd').log"
    $startTime = Get-Date

    Write-OK "Khoi dong bot... (lan $($restartCount + 1)) — $(Get-Date -Format 'HH:mm:ss dd/MM/yyyy')"
    Write-INFO "Log: $logFile"
    Write-Host ""

    # Chay bot, output ra ca console lan file log
    try {
        & $PYTHON $MAIN 2>&1 | Tee-Object -FilePath $logFile -Append
    } catch {
        Write-ERR "Loi khi khoi dong process: $_"
    }

    $exitCode = $LASTEXITCODE
    $uptime   = ((Get-Date) - $startTime).ToString("hh\:mm\:ss")

    Write-Host ""
    Write-WARN "Bot da dung — Exit code: $exitCode | Uptime: $uptime"

    # Thoat neu exit code = 0 (shutdown co chu dich, vi du Ctrl+C)
    if ($exitCode -eq 0) {
        Write-OK "Bot da tat theo yeu cau (exit 0). Khong restart."
        break
    }

    $restartCount++

    if ($restartCount -ge $maxRestarts) {
        Write-ERR "Da dat gioi han $maxRestarts lan restart. Dung han."
        Write-WARN "Kiem tra log tai: $logFile"
        break
    }

    Write-WARN "Se restart sau $restartDelay giay... (Ctrl+C de huy)"

    for ($i = $restartDelay; $i -gt 0; $i--) {
        Write-Host "`r[!!]  Restart sau: $i giay..." -NoNewline -ForegroundColor Yellow
        Start-Sleep -Seconds 1
    }
    Write-Host ""
    Write-Host ""
}

Write-Host ""
Write-Header
Write-OK "VCoreX Bot da dung. Tong so lan restart: $restartCount"
Write-Header
Read-Host "Nhan Enter de dong cua so"
