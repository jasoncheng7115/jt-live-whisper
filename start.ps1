#Requires -Version 5.1
<#
.SYNOPSIS
    jt-live-whisper Windows 啟動腳本
.DESCRIPTION
    啟動即時英翻中字幕系統。
    自動檢查 Python 虛擬環境、背景檢查 GitHub 新版本。
.EXAMPLE
    .\start.ps1
    .\start.ps1 --input file.mp3
    .\start.ps1 --mode zh2en
.NOTES
    Author: Jason Cheng (Jason Tools)
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ─── 環境檢查：必須在 PowerShell 中執行 ─────────────────────
if (-not $PSVersionTable) {
    Write-Host ""
    Write-Host "  [錯誤] 此腳本必須在 PowerShell 中執行，不支援命令提示字元 (cmd.exe)。" -ForegroundColor Red
    Write-Host "  請開啟 PowerShell 或 Windows Terminal 後再執行：" -ForegroundColor Yellow
    Write-Host "    powershell -File .\start.ps1" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# ─── 執行權限檢查 ────────────────────────────────────────────
$execPolicy = Get-ExecutionPolicy -Scope CurrentUser
if ($execPolicy -eq 'Restricted' -or $execPolicy -eq 'AllSigned') {
    Write-Host ""
    Write-Host "  [提醒] PowerShell 執行原則為 '$execPolicy'，可能無法執行腳本。" -ForegroundColor Yellow
    Write-Host "  建議執行以下指令後重新啟動終端機：" -ForegroundColor Yellow
    Write-Host "    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# ─── 編碼設定 ─────────────────────────────────────────────────
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ─── 路徑 ─────────────────────────────────────────────────────
$SCRIPT_DIR = if ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $PWD.Path
}
$VENV_DIR    = Join-Path $SCRIPT_DIR "venv"
$VENV_PYTHON = Join-Path $VENV_DIR "Scripts\python.exe"
$MAIN_PY     = Join-Path $SCRIPT_DIR "translate_meeting.py"

# ─── ANSI 色彩（24-bit True Color）────────────────────────────
$ESC = [char]27
$C_TITLE = "$ESC[38;2;100;180;255m"
$C_OK    = "$ESC[38;2;80;255;120m"
$C_WARN  = "$ESC[38;2;255;220;80m"
$C_ERR   = "$ESC[38;2;255;100;100m"
$C_DIM   = "$ESC[38;2;100;100;100m"
$C_WHITE = "$ESC[38;2;255;255;255m"
$BOLD    = "$ESC[1m"
$NC      = "$ESC[0m"

# 啟用 Virtual Terminal Processing（讓 ANSI 碼在 Windows 終端生效）
try {
    $Kernel32 = Add-Type -MemberDefinition @"
[DllImport("kernel32.dll", SetLastError = true)]
public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll")]
public static extern bool GetConsoleMode(IntPtr h, out uint m);
[DllImport("kernel32.dll")]
public static extern bool SetConsoleMode(IntPtr h, uint m);
"@ -Name "K32" -Namespace "StartVTP" -PassThru -ErrorAction Stop

    # STDOUT: 啟用 VTP
    $hOut = $Kernel32::GetStdHandle(-11)
    $m = 0
    $null = $Kernel32::GetConsoleMode($hOut, [ref]$m)
    $null = $Kernel32::SetConsoleMode($hOut, $m -bor 0x0004)

    # STDIN: 關閉 QuickEdit 模式（滑鼠選取時會凍結程式）
    $hIn = $Kernel32::GetStdHandle(-10)
    $mIn = 0
    $null = $Kernel32::GetConsoleMode($hIn, [ref]$mIn)
    # 0x0040 = ENABLE_QUICK_EDIT_MODE，關閉它；保留 0x0080 = ENABLE_EXTENDED_FLAGS
    $null = $Kernel32::SetConsoleMode($hIn, ($mIn -band (-bnot 0x0040)) -bor 0x0080)
} catch {
    # 舊版終端無法設定，不影響功能
}

# ─── Banner ───────────────────────────────────────────────────

$cols = try { $Host.UI.RawUI.WindowSize.Width } catch { 60 }
if ($cols -lt 40) { $cols = 40 }
$banner_line = '=' * $cols

Write-Host ""
Write-Host "${C_TITLE}${banner_line}${NC}"
Write-Host "${C_TITLE}${BOLD}  jt-live-whisper v2.14.6 - 100% 全地端 AI 語音工具集${NC}"
Write-Host "${C_TITLE}  by Jason Cheng (Jason Tools)${NC}"
Write-Host "${C_TITLE}${banner_line}${NC}"
Write-Host ""
Write-Host "${C_DIM}  提示：已自動關閉終端機「快速編輯」模式，避免滑鼠誤點導致程式凍結${NC}"
Write-Host ""

# ─── 背景 GitHub 版本檢查（不阻塞啟動流程）──────────────────
$updateJob = Start-Job -ScriptBlock {
    try {
        $ProgressPreference = 'SilentlyContinue'
        $resp = Invoke-WebRequest -Uri "https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/translate_meeting.py" `
            -TimeoutSec 3 -UseBasicParsing -Headers @{ Range = "bytes=0-10240" } -ErrorAction Stop
        $content = $resp.Content
        if ($content -match 'APP_VERSION\s*=\s*"([^"]+)"') {
            return $Matches[1]
        }
    } catch { }
    return ""
}

# ─── 重複執行檢查 ────────────────────────────────────────────
$runningPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "translate_meeting" -and $_.ProcessId -ne $PID }
if ($runningPy) {
    Write-Host "${C_WARN}[警告] translate_meeting.py 已在執行中（PID: $(($runningPy | ForEach-Object { $_.ProcessId }) -join ', ')）${NC}"
    Write-Host "${C_WHITE}同時執行多個實例可能導致音訊裝置衝突${NC}"
    $ans = Read-Host "  是否仍然繼續？(y/N/k=終止舊程序)"
    if ($ans -eq 'k' -or $ans -eq 'K') {
        foreach ($proc in $runningPy) {
            try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                  Write-Host "${C_OK}  已終止 PID $($proc.ProcessId)${NC}" }
            catch { Write-Host "${C_ERR}  無法終止 PID $($proc.ProcessId)${NC}" }
        }
        Start-Sleep -Milliseconds 500
    } elseif ($ans -ne 'y' -and $ans -ne 'Y') {
        $updateJob | Remove-Job -Force -ErrorAction SilentlyContinue
        exit 0
    }
}

# ─── 檢查 venv ──────────────────────────────────────────────
if (-not (Test-Path $VENV_PYTHON)) {
    Write-Host "${C_ERR}[錯誤] 找不到 Python 虛擬環境: ${VENV_DIR}${NC}"
    Write-Host "${C_WHITE}請先執行安裝步驟：${NC}"
    Write-Host "${C_OK}  .\install.ps1${NC}"
    Write-Host ""
    # 清理背景 Job
    $updateJob | Remove-Job -Force -ErrorAction SilentlyContinue
    exit 1
}

# 驗證 venv python 可執行
$venvTest = & $VENV_PYTHON --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "${C_ERR}[錯誤] Python 虛擬環境損壞（python.exe 無法執行）${NC}"
    Write-Host "${C_WHITE}請重新執行安裝：${NC}"
    Write-Host "${C_OK}  Remove-Item venv -Recurse -Force${NC}"
    Write-Host "${C_OK}  .\install.ps1${NC}"
    Write-Host ""
    $updateJob | Remove-Job -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "${C_OK}Python 環境已啟用${NC}"

# ─── 顯示新版本提醒（背景檢查應已完成）─────────────────────
$null = Wait-Job $updateJob -Timeout 2
if ($updateJob.State -eq 'Completed') {
    $remoteVer = Receive-Job $updateJob
    if ($remoteVer) {
        # 取得本機版本
        $localVerMatch = Select-String -Path $MAIN_PY -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($localVerMatch) {
            $localVer = $localVerMatch.Matches.Groups[1].Value
            if ($localVer -ne $remoteVer) {
                # 比較版本：remote 較新才提醒
                $localParts  = $localVer.Split('.') | ForEach-Object { [int]$_ }
                $remoteParts = $remoteVer.Split('.') | ForEach-Object { [int]$_ }
                $remoteNewer = $false
                for ($i = 0; $i -lt [Math]::Max($localParts.Count, $remoteParts.Count); $i++) {
                    $lp = if ($i -lt $localParts.Count) { $localParts[$i] } else { 0 }
                    $rp = if ($i -lt $remoteParts.Count) { $remoteParts[$i] } else { 0 }
                    if ($rp -gt $lp) { $remoteNewer = $true; break }
                    if ($rp -lt $lp) { break }
                }
                if ($remoteNewer) {
                    Write-Host ""
                    Write-Host "${C_WARN}  有新版本可用: v${localVer} → v${remoteVer}${NC}"
                    Write-Host "${C_DIM}  升級指令: .\install.ps1 -Upgrade${NC}"
                }
            }
        }
    }
}
$updateJob | Remove-Job -Force -ErrorAction SilentlyContinue

Write-Host ""

# ─── 執行主程式 ──────────────────────────────────────────────
if (-not (Test-Path $MAIN_PY)) {
    Write-Host "${C_ERR}[錯誤] 找不到主程式: translate_meeting.py${NC}"
    Write-Host "${C_WHITE}請重新執行安裝或從 GitHub 下載：${NC}"
    Write-Host "${C_OK}  .\install.ps1 -Upgrade${NC}"
    exit 1
}

# --webui 模式：啟動 WebUI 伺服器（設定與字幕都在瀏覽器操作）
$WEBUI_PY = Join-Path $SCRIPT_DIR "webui.py"
if ($args -contains "--webui") {
    & $VENV_PYTHON $WEBUI_PY
} else {
    & $VENV_PYTHON $MAIN_PY @args
}
exit $LASTEXITCODE
