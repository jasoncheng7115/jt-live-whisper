#Requires -Version 5.1
<#
.SYNOPSIS
    jt-live-whisper Windows 安裝腳本
.DESCRIPTION
    安裝即時英翻中字幕系統的所有相依套件。
    自動偵測 NVIDIA GPU 並安裝對應的 CUDA 加速版本。
.EXAMPLE
    .\install.ps1
    .\install.ps1 -Upgrade
.NOTES
    Author: Jason Cheng (Jason Tools)
#>

param(
    [switch]$Upgrade
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ─── 環境檢查：必須在 PowerShell 中執行 ─────────────────────
if (-not $PSVersionTable) {
    Write-Host ""
    Write-Host "  [錯誤] 此腳本必須在 PowerShell 中執行，不支援命令提示字元 (cmd.exe)。" -ForegroundColor Red
    Write-Host "  請開啟 PowerShell 或 Windows Terminal 後再執行：" -ForegroundColor Yellow
    Write-Host "    powershell -File .\install.ps1" -ForegroundColor Cyan
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
$GITHUB_REPO    = "https://github.com/jasoncheng7115/jt-live-whisper.git"
$GITHUB_ZIP     = "https://github.com/jasoncheng7115/jt-live-whisper/archive/refs/heads/main.zip"

# ─── Bootstrap：透過 irm | iex 執行時，自動下載並安裝 ─────────
if (-not (Test-Path (Join-Path $SCRIPT_DIR "translate_meeting.py"))) {
    Write-Host ""
    Write-Host "  jt-live-whisper - 一鍵安裝" -ForegroundColor Cyan
    Write-Host ""

    $installDir = "C:\jt-live-whisper"
    if (Test-Path (Join-Path $installDir "translate_meeting.py")) {
        Write-Host "  目錄已存在: $installDir" -ForegroundColor White
        Write-Host "  進入目錄執行安裝..." -ForegroundColor White
    } else {
        Write-Host "  正在從 GitHub 下載 jt-live-whisper..." -ForegroundColor White
        $zipPath = Join-Path $env:TEMP "jt-live-whisper.zip"
        $extractPath = Join-Path $env:TEMP "jt-extract"
        $oldProg = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $GITHUB_ZIP -OutFile $zipPath -UseBasicParsing
        $ProgressPreference = $oldProg
        if (-not (Test-Path $zipPath)) {
            Write-Host "  [錯誤] 下載失敗，請檢查網路連線" -ForegroundColor Red
            exit 1
        }
        Expand-Archive $zipPath -DestinationPath $extractPath -Force
        $srcDir = Get-ChildItem $extractPath -Directory | Select-Object -First 1
        if (Test-Path $installDir) {
            # 保留現有 config.json、venv、logs
            Get-ChildItem $srcDir.FullName | Where-Object { $_.Name -notin @("venv","logs","config.json") } |
                ForEach-Object { Copy-Item $_.FullName (Join-Path $installDir $_.Name) -Recurse -Force }
        } else {
            Move-Item $srcDir.FullName $installDir -Force
        }
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $extractPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  [完成] 已下載至 $installDir" -ForegroundColor Green
    }
    Set-Location $installDir
    & (Join-Path $installDir "install.ps1")
    exit $LASTEXITCODE
}
# ─── Bootstrap 結束 ──────────────────────────────────────────

$VENV_DIR       = Join-Path $SCRIPT_DIR "venv"
$WHISPER_CPP_DIR = Join-Path $SCRIPT_DIR "whisper.cpp"
$CONFIG_PATH    = Join-Path $SCRIPT_DIR "config.json"

# ─── 安裝 Log ─────────────────────────────────────────────────
$LOG_DIR = Join-Path $SCRIPT_DIR "logs"
if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null }
$INSTALL_LOG = Join-Path $LOG_DIR ("install_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
try { Start-Transcript -Path $INSTALL_LOG -Append | Out-Null } catch {}

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
"@ -Name "K32" -Namespace "VTP" -PassThru -ErrorAction Stop

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

# ─── Helper Functions ─────────────────────────────────────────

function section($text) {
    Write-Host "`n${C_TITLE}${BOLD}▎ ${text}${NC}"
    Write-Host "${C_DIM}$('─' * 50)${NC}"
}

function check_ok($text) {
    Write-Host "  ${C_OK}[完成]${NC} ${C_WHITE}${text}${NC}"
}

function check_fail($text) {
    Write-Host "  ${C_ERR}[失敗]${NC} ${C_WHITE}${text}${NC}"
}

function check_warn($text) {
    Write-Host "  ${C_WARN}[警告]${NC} ${C_WHITE}${text}${NC}"
}

function check_notice($text) {
    Write-Host "  ${C_WARN}[注意]${NC} ${C_WHITE}${text}${NC}"
}

function check_missing($text) {
    Write-Host "  ${C_WARN}[缺少]${NC} ${C_WHITE}${text}${NC}"
}

function check_detect($text) {
    Write-Host "  ${C_WARN}[偵測]${NC} ${C_WHITE}${text}${NC}"
}

function check_skip($text) {
    Write-Host "  ${C_WARN}[跳過]${NC} ${C_WHITE}${text}${NC}"
}

function info($text) {
    Write-Host "  ${C_DIM}${text}${NC}"
}

function cmd_exists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function get_free_gb($path) {
    try {
        $drv = (Get-Item $path -ErrorAction Stop).PSDrive
        return [math]::Round($drv.Free / 1GB, 1)
    } catch {
        # 無法取得磁碟資訊時回傳大數，不阻擋安裝
        return 999
    }
}

function read_config() {
    if (Test-Path $CONFIG_PATH) {
        try {
            $raw = Get-Content $CONFIG_PATH -Raw -Encoding UTF8
            if ($raw -and $raw.Trim()) {
                $obj = $raw | ConvertFrom-Json
                if ($obj) { return $obj }
            }
        } catch { }
    }
    return [PSCustomObject]@{}
}

function save_config($obj) {
    $jsonText = $obj | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($CONFIG_PATH, $jsonText, [System.Text.UTF8Encoding]::new($false))
}

function pip_install($pkg, $desc, [string[]]$extraArgs) {
    # 檢查是否已安裝（用套件名，去除版本限制符號）
    $pkgName = ($pkg -split '[<>=!;\[]')[0].Trim()
    & $VENV_PIP show $pkgName 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        check_ok "${desc}（已安裝）"
        return $true
    }
    info "安裝 ${desc}..."
    $allArgs = @("install", $pkg, "--quiet") + $extraArgs
    & $VENV_PIP @allArgs 2>$null
    if ($LASTEXITCODE -eq 0) {
        check_ok $desc
        return $true
    }
    # 重試一次（不加 --quiet，顯示錯誤訊息）
    info "重試 ${desc}..."
    $retryArgs = @("install", $pkg) + $extraArgs
    & $VENV_PIP @retryArgs
    if ($LASTEXITCODE -eq 0) {
        check_ok "${desc}（重試成功）"
        return $true
    }
    check_fail $desc
    return $false
}

function venv_import_ok($module) {
    & $VENV_PYTHON -c "import $module" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function hf_download($repo, $desc, $localDir) {
    # HuggingFace 模型下載，SSL 失敗時自動停用憑證驗證重試
    $localDirArg = if ($localDir) { ", local_dir=r'$localDir'" } else { "" }
    $dlResult = & $VENV_PYTHON -c @"
import os
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
from huggingface_hub import snapshot_download
try:
    snapshot_download('$repo'$localDirArg)
    print('OK')
except Exception as e:
    err = str(e)
    if 'SSL' in err or 'CERTIFICATE' in err.upper() or 'ssl' in err:
        print('SSL_RETRY')
    else:
        print('FAIL:' + err[:300])
"@ 2>&1

    if ($dlResult -match "^SSL_RETRY") {
        check_notice "SSL 憑證驗證失敗（常見於企業網路），嘗試停用驗證重新下載..."
        $dlResult = & $VENV_PYTHON -c @"
import os
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests
session = requests.Session()
session.verify = False
from huggingface_hub import snapshot_download, configure_http_backend
configure_http_backend(backend_factory=lambda: session)
try:
    snapshot_download('$repo'$localDirArg)
    print('OK')
except Exception as e:
    print('FAIL:' + str(e)[:300])
"@ 2>&1
    }

    if ($dlResult -match "^OK") {
        return "ok"
    }
    $errMsg = ($dlResult -replace "^FAIL:", "").Trim()
    check_notice "${desc}下載失敗，可稍後在有網路時重新執行安裝"
    if ($errMsg) { info "  錯誤詳情: $errMsg" }
    return "fail"
}

# ─── Banner ───────────────────────────────────────────────────

$cols = try { $Host.UI.RawUI.WindowSize.Width } catch { 60 }
if ($cols -lt 40) { $cols = 40 }
$banner_line = '=' * $cols

Write-Host ""
Write-Host "${C_TITLE}${banner_line}${NC}"
Write-Host "${C_TITLE}${BOLD}  jt-live-whisper v2.14.6 - 100% 全地端 AI 語音工具集 - Windows 安裝程式${NC}"
Write-Host "${C_TITLE}  by Jason Cheng (Jason Tools)${NC}"
Write-Host "${C_TITLE}${banner_line}${NC}"
Write-Host ""
Write-Host "${C_DIM}  提示：已自動關閉終端機「快速編輯」模式，避免滑鼠誤點導致程式凍結${NC}"
Write-Host ""

# ═══════════════════════════════════════════════════════════════
# Upgrade 模式
# ═══════════════════════════════════════════════════════════════

if ($Upgrade) {
    section "從 GitHub 升級程式"

    $tmpDir = Join-Path $env:TEMP "jt-upgrade-$(Get-Random)"
    $zipPath = Join-Path $tmpDir "jt-live-whisper.zip"
    New-Item -Path $tmpDir -ItemType Directory -Force | Out-Null
    info "正在從 GitHub 下載最新版本..."
    $oldProg = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $GITHUB_ZIP -OutFile $zipPath -UseBasicParsing
    } catch {
        check_fail "無法連接 GitHub，請檢查網路連線"
        Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        exit 1
    }
    $ProgressPreference = $oldProg
    Expand-Archive $zipPath -DestinationPath $tmpDir -Force
    $repoDir = (Get-ChildItem $tmpDir -Directory | Where-Object { $_.Name -ne "jt-live-whisper.zip" } | Select-Object -First 1).FullName

    if (-not $repoDir -or -not (Test-Path (Join-Path $repoDir "translate_meeting.py"))) {
        check_fail "下載的檔案不完整，請檢查網路連線"
        Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        exit 1
    }

    $remoteVer = (Select-String -Path (Join-Path $repoDir "translate_meeting.py") -Pattern 'APP_VERSION\s*=\s*"(.+)"' |
                  Select-Object -First 1).Matches.Groups[1].Value
    $localVer  = (Select-String -Path (Join-Path $SCRIPT_DIR "translate_meeting.py") -Pattern 'APP_VERSION\s*=\s*"(.+)"' |
                  Select-Object -First 1).Matches.Groups[1].Value

    Write-Host "  ${C_WHITE}目前版本: v${localVer}${NC}"
    Write-Host "  ${C_WHITE}最新版本: v${remoteVer}${NC}"

    if ($localVer -eq $remoteVer) {
        # 版本相同但檢查是否缺少檔案（舊版升級可能漏掉新檔案）
        $missingFiles = @()
        foreach ($f in @("webui.py","webui.html")) {
            if (-not (Test-Path (Join-Path $SCRIPT_DIR $f))) { $missingFiles += $f }
        }
        if ($missingFiles.Count -gt 0) {
            info "版本相同但缺少檔案，補充安裝中..."
            foreach ($f in @("translate_meeting.py","start.sh","start.ps1","install.sh","install.ps1","SOP.md","webui.py","webui.html")) {
                $src = Join-Path $repoDir $f
                if (Test-Path $src) { Copy-Item $src (Join-Path $SCRIPT_DIR $f) -Force }
            }
            check_ok "已補充安裝缺少的檔案（$($missingFiles -join '、')）"
        } else {
            check_ok "已經是最新版本 (v${localVer})"
        }
        Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        exit 0
    }

    # 版本比較：如果本地版本較新，提醒使用者
    $localParts  = $localVer.Split('.') | ForEach-Object { [int]$_ }
    $remoteParts = $remoteVer.Split('.') | ForEach-Object { [int]$_ }
    $isLocalNewer = $false
    for ($i = 0; $i -lt [Math]::Max($localParts.Count, $remoteParts.Count); $i++) {
        $lp = if ($i -lt $localParts.Count) { $localParts[$i] } else { 0 }
        $rp = if ($i -lt $remoteParts.Count) { $remoteParts[$i] } else { 0 }
        if ($lp -gt $rp) { $isLocalNewer = $true; break }
        if ($lp -lt $rp) { break }
    }
    if ($isLocalNewer) {
        check_skip "本地版本 (v${localVer}) 比 GitHub 版本 (v${remoteVer}) 更新"
        $ans = Read-Host "  確定要降級嗎？(y/N)"
        if ($ans -ne 'y' -and $ans -ne 'Y') {
            Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
            exit 0
        }
    }

    # 歸檔當前版本
    $archiveDir = Join-Path $SCRIPT_DIR "versions\v${localVer}"
    if (-not (Test-Path $archiveDir)) {
        New-Item -Path $archiveDir -ItemType Directory -Force | Out-Null
        foreach ($f in @("translate_meeting.py","start.sh","start.ps1","install.sh","install.ps1","SOP.md","config.json","webui.py","webui.html")) {
            $src = Join-Path $SCRIPT_DIR $f
            if (Test-Path $src) { Copy-Item $src $archiveDir }
        }
        info "已歸檔 v${localVer} 到 versions\v${localVer}\"
    }

    # 更新檔案
    $updated = 0
    foreach ($f in @("translate_meeting.py","start.sh","start.ps1","install.sh","install.ps1","SOP.md","webui.py","webui.html")) {
        $src = Join-Path $repoDir $f
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $SCRIPT_DIR $f) -Force
            $updated++
        }
    }

    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    check_ok "已升級 v${localVer} -> v${remoteVer}（更新 ${updated} 個檔案）"
    Write-Host ""
    Write-Host "  ${C_WARN}建議重新執行 .\install.ps1 確認相依套件完整${NC}"
    exit 0
}

# ═══════════════════════════════════════════════════════════════
# 1. 環境偵測
# ═══════════════════════════════════════════════════════════════

section "環境偵測"

# ─── 網路連線檢查 ────────────────────────────────────────────
$netOk = $false
$oldProg = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
foreach ($testUrl in @("https://github.com", "https://pypi.org", "https://www.python.org")) {
    try {
        $null = Invoke-WebRequest -Uri $testUrl -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        $netOk = $true
        break
    } catch { }
}
$ProgressPreference = $oldProg
if ($netOk) {
    check_ok "網路連線正常"
} else {
    check_fail "無法連線到 GitHub / PyPI / python.org，請檢查網路"
    exit 1
}

# ─── 執行中程序檢查 ──────────────────────────────────────────
$runningPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "translate_meeting" }
$runningWs = Get-Process -Name "whisper-stream" -ErrorAction SilentlyContinue
if ($runningPy -or $runningWs) {
    check_warn "偵測到 jt-live-whisper 相關程序正在執行"
    if ($runningPy) { info "  - python.exe (translate_meeting.py)" }
    if ($runningWs) { info "  - whisper-stream.exe" }
    info "Windows 檔案鎖定較嚴格，安裝過程可能無法更新正在使用的檔案"
    info ""
    info "  Y = 繼續安裝（不結束程序）"
    info "  K = 強制結束程序後繼續安裝"
    info "  N = 取消安裝"
    $ans = Read-Host "  請選擇 (y/K/N)"
    if ($ans -eq 'k' -or $ans -eq 'K') {
        if ($runningPy) {
            $runningPy | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            info "  已結束 python.exe (translate_meeting.py)"
        }
        if ($runningWs) {
            $runningWs | Stop-Process -Force -ErrorAction SilentlyContinue
            info "  已結束 whisper-stream.exe"
        }
        Start-Sleep -Seconds 1
        check_ok "程序已結束，繼續安裝"
    } elseif ($ans -ne 'y' -and $ans -ne 'Y') {
        exit 0
    }
}

# ─── Windows 版本 ─────────────────────────────────────────────
$winBuild = [System.Environment]::OSVersion.Version.Build
if ($winBuild -lt 17763) {
    check_fail "需要 Windows 10 1809 (Build 17763) 或更新版本"
    info "目前版本: Build $winBuild"
    exit 1
}
$winVer = if ($winBuild -ge 22000) { "Windows 11" } else { "Windows 10" }
check_ok "${winVer} (Build ${winBuild})"

# ─── 長路徑支援 ──────────────────────────────────────────────
$longPathEnabled = $false
try {
    $regVal = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -ErrorAction SilentlyContinue
    if ($regVal -and $regVal.LongPathsEnabled -eq 1) { $longPathEnabled = $true }
} catch { }
# ─── 管理員權限檢查（提前，長路徑啟用需要）─────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    check_ok "以管理員身份執行"
} else {
    check_notice "非管理員身份執行（安裝 ffmpeg / VS Build Tools 等系統元件時可能需要）"
}

# ─── 長路徑支援 ──────────────────────────────────────────────
if ($longPathEnabled) {
    check_ok "Windows 長路徑支援已啟用"
} elseif ($isAdmin) {
    # 管理員權限，直接啟用不用問
    info "正在啟用 Windows 長路徑支援..."
    try {
        reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f 2>$null | Out-Null
        $regVal2 = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -ErrorAction SilentlyContinue
        if ($regVal2 -and $regVal2.LongPathsEnabled -eq 1) {
            $longPathEnabled = $true
            check_ok "Windows 長路徑支援已啟用"
        } else {
            check_notice "長路徑啟用失敗"
        }
    } catch {
        check_notice "長路徑啟用失敗"
    }
} else {
    check_notice "Windows 長路徑支援未啟用（pip 安裝路徑過深時可能失敗）"
    $ans = Read-Host "  是否自動啟用？需要管理員權限 (Y/n)"
    if ($ans -ne 'n' -and $ans -ne 'N') {
        try {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-Command", "reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f" 2>$null
            $regVal2 = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -ErrorAction SilentlyContinue
            if ($regVal2 -and $regVal2.LongPathsEnabled -eq 1) {
                $longPathEnabled = $true
                check_ok "Windows 長路徑支援已啟用"
            } else {
                check_notice "啟用失敗，可稍後手動執行："
                info "  reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f"
            }
        } catch {
            check_notice "啟用失敗（需要管理員權限），可稍後手動執行："
            info "  reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f"
        }
    }
}

# ─── 磁碟空間 ────────────────────────────────────────────────
$freeGB = get_free_gb $SCRIPT_DIR
if ($freeGB -lt 3) {
    check_fail "磁碟可用空間不足: ${freeGB} GB（最少需要 3 GB）"
    exit 1
} elseif ($freeGB -lt 8) {
    check_notice "磁碟可用空間: ${freeGB} GB（建議 8 GB 以上）"
} else {
    check_ok "磁碟可用空間: ${freeGB} GB"
}

# ─── NVIDIA GPU ───────────────────────────────────────────────
$GPU_AVAILABLE  = $false
$GPU_NAME       = ""
$GPU_MEMORY_MB  = 0
$CUDA_VERSION   = ""
$TORCH_CUDA_TAG = ""

# nvidia-smi 可能不在 PATH 中（完全重裝後 PATH 可能被清掉）
$nvidiaSmi = "nvidia-smi"
if (-not (cmd_exists $nvidiaSmi)) {
    $nvSmiFallbacks = @(
        "$env:SystemRoot\System32\nvidia-smi.exe",
        "$env:SystemRoot\SysNative\nvidia-smi.exe",
        "${env:ProgramFiles}\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    )
    foreach ($p in $nvSmiFallbacks) {
        if (Test-Path $p) { $nvidiaSmi = $p; break }
    }
}
if ((cmd_exists $nvidiaSmi) -or (Test-Path $nvidiaSmi)) {
    try {
        $smiCsv = & $nvidiaSmi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $smiCsv) {
            # nvidia-smi 存在但執行失敗
            check_warn "nvidia-smi 執行失敗（exit code: $LASTEXITCODE），可能驅動版本不符"
            info "nvidia-smi 路徑: $nvidiaSmi"
            info "請至 https://www.nvidia.com/drivers/ 更新 NVIDIA 驅動程式"
        }
        if ($LASTEXITCODE -eq 0 -and $smiCsv) {
            # 多 GPU 時取第一張
            if ($smiCsv -is [array]) { $smiCsv = $smiCsv[0] }
            $parts = $smiCsv.Split(',').Trim()
            $GPU_NAME      = $parts[0]
            $GPU_MEMORY_MB = [int]$parts[1]
            $GPU_AVAILABLE = $true

            # CUDA 版本
            $cudaLine = (& $nvidiaSmi 2>$null) -match "CUDA Version"
            if ($cudaLine) {
                if ($cudaLine -is [array]) { $cudaLine = $cudaLine[0] }
                $CUDA_VERSION = ($cudaLine -replace '.*CUDA Version:\s*' -replace '\s.*').Trim()
            }

            check_ok "NVIDIA GPU: ${GPU_NAME} ($([math]::Round($GPU_MEMORY_MB/1024,1)) GB)"
            check_ok "CUDA 驅動: ${CUDA_VERSION}"

            # CUDA 13+：faster-whisper (CTranslate2) 需要 CUDA 12.x 的 cublas64_12.dll
            $CUDA_13_PLUS = $false
            if ($CUDA_VERSION -match "^1[3-9]\.") {
                $CUDA_13_PLUS = $true
                check_notice "CUDA ${CUDA_VERSION} 偵測到 — 將自動安裝 CUDA 12.x 相容程式庫"
            }

            # 對應 PyTorch CUDA wheel 版本
            if     ($CUDA_VERSION -match "^12\.[4-9]|^1[3-9]") { $TORCH_CUDA_TAG = "cu124" }
            elseif ($CUDA_VERSION -match "^12\.")               { $TORCH_CUDA_TAG = "cu121" }
            elseif ($CUDA_VERSION -match "^11\.[8-9]")          { $TORCH_CUDA_TAG = "cu118" }
            else                                                 { $TORCH_CUDA_TAG = "cu121" }
        }
    } catch { }
}

if (-not $GPU_AVAILABLE) {
    # nvidia-smi 偵測失敗，嘗試透過 WMI + 額外路徑搜尋
    $nvGpuPci = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "NVIDIA" }
    if ($nvGpuPci) {
        # WMI 看得到 GPU，嘗試更多路徑找 nvidia-smi
        $extraPaths = @()
        # where.exe 可跨 32/64-bit 搜尋 System32
        try {
            $whereSmi = & where.exe nvidia-smi.exe 2>$null
            if ($LASTEXITCODE -eq 0 -and $whereSmi) {
                if ($whereSmi -is [array]) { $extraPaths += $whereSmi } else { $extraPaths += @($whereSmi) }
            }
        } catch { }
        # NVIDIA 驅動安裝路徑（從 InstalledDisplayDrivers 推導）
        try {
            $drvPaths = $nvGpuPci.InstalledDisplayDrivers -split ','
            foreach ($drv in $drvPaths) {
                $drvDir = Split-Path $drv.Trim() -ErrorAction SilentlyContinue
                if ($drvDir) { $extraPaths += "$drvDir\nvidia-smi.exe" }
            }
        } catch { }
        # 登錄檔 NvSmi 路徑
        try {
            $regPath = (Get-ItemProperty "HKLM:\SOFTWARE\NVIDIA Corporation\Global\NvSmi" -ErrorAction SilentlyContinue).Path
            if ($regPath) { $extraPaths += "$regPath\nvidia-smi.exe" }
        } catch { }

        foreach ($ep in ($extraPaths | Select-Object -Unique)) {
            $ep = $ep.Trim()
            if (-not $ep -or -not (Test-Path $ep)) { continue }
            try {
                $smiCsv = & $ep --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
                if ($LASTEXITCODE -eq 0 -and $smiCsv) {
                    $nvidiaSmi = $ep
                    if ($smiCsv -is [array]) { $smiCsv = $smiCsv[0] }
                    $parts = $smiCsv.Split(',').Trim()
                    $GPU_NAME      = $parts[0]
                    $GPU_MEMORY_MB = [int]$parts[1]
                    $GPU_AVAILABLE = $true
                    $cudaLine = (& $nvidiaSmi 2>$null) -match "CUDA Version"
                    if ($cudaLine) {
                        if ($cudaLine -is [array]) { $cudaLine = $cudaLine[0] }
                        $CUDA_VERSION = ($cudaLine -replace '.*CUDA Version:\s*' -replace '\s.*').Trim()
                    }
                    check_ok "NVIDIA GPU: ${GPU_NAME} ($([math]::Round($GPU_MEMORY_MB/1024,1)) GB)（透過額外路徑偵測）"
                    check_ok "CUDA 驅動: ${CUDA_VERSION}"
                    info "nvidia-smi 路徑: $nvidiaSmi"
                    # CUDA 13+
                    $CUDA_13_PLUS = $false
                    if ($CUDA_VERSION -match "^1[3-9]\.") {
                        $CUDA_13_PLUS = $true
                        check_notice "CUDA ${CUDA_VERSION} 偵測到 — 將自動安裝 CUDA 12.x 相容程式庫"
                    }
                    if     ($CUDA_VERSION -match "^12\.[4-9]|^1[3-9]") { $TORCH_CUDA_TAG = "cu124" }
                    elseif ($CUDA_VERSION -match "^12\.")               { $TORCH_CUDA_TAG = "cu121" }
                    elseif ($CUDA_VERSION -match "^11\.[8-9]")          { $TORCH_CUDA_TAG = "cu118" }
                    else                                                 { $TORCH_CUDA_TAG = "cu121" }
                    break
                }
            } catch { }
        }
    }

    if (-not $GPU_AVAILABLE) {
        check_notice "未偵測到 NVIDIA GPU，將安裝 CPU 版本"
        info "翻譯建議使用區域網路 LLM 伺服器（--llm-host）或 NLLB / Argos 離線翻譯"
        if ($nvGpuPci) {
            check_warn "系統有 NVIDIA 裝置（$($nvGpuPci.Name)）但 nvidia-smi 無法執行"
            info "可能原因：NVIDIA 驅動未安裝或版本過舊，請至 https://www.nvidia.com/drivers/ 更新驅動"
        }
    }
}

# ─── Python ───────────────────────────────────────────────────
$PYTHON_CMD = ""
foreach ($candidate in @("python", "python3", "py -3")) {
    $exe = ($candidate -split ' ')[0]
    if (-not (cmd_exists $exe)) { continue }

    try {
        $verOut = if ($candidate -eq "py -3") { & py -3 --version 2>&1 } else { & $exe --version 2>&1 }
        if ($verOut -match "(\d+)\.(\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 12) {
                $PYTHON_CMD = $candidate
                check_ok "Python $($Matches[0]) ($candidate)"
                break
            }
        }
    } catch { }
}

if (-not $PYTHON_CMD) {
    check_fail "找不到 Python 3.12+"
    info "安裝方式（擇一）："
    info "  winget install Python.Python.3.12"
    info "  https://www.python.org/downloads/"
    info "  安裝時務必勾選 'Add Python to PATH'"
    exit 1
}

# ─── Python 64-bit 檢查 ──────────────────────────────────────
$pyArch = if ($PYTHON_CMD -eq "py -3") {
    & py -3 -c "import struct; print(struct.calcsize('P') * 8)" 2>$null
} else {
    & $PYTHON_CMD -c "import struct; print(struct.calcsize('P') * 8)" 2>$null
}
if ($pyArch -eq "32") {
    check_fail "偵測到 32-bit Python，本程式需要 64-bit Python"
    info "PyTorch / faster-whisper / CUDA 加速皆不支援 32-bit"
    info "請從以下位址下載 64-bit 版本："
    info "  https://www.python.org/downloads/"
    info "  選擇「Windows installer (64-bit)」"
    exit 1
} elseif ($pyArch -eq "64") {
    check_ok "Python 64-bit"
} else {
    info "無法確認 Python 位元數（將繼續安裝）"
}

# ─── Git ──────────────────────────────────────────────────────
$HAS_GIT = cmd_exists "git"
if ($HAS_GIT) {
    $gitVer = ((& git --version 2>$null) -replace 'git version ','').Trim()
    check_ok "Git ${gitVer}"
} else {
    check_missing "找不到 Git，正在自動安裝..."
    if (cmd_exists "winget") {
        info "正在透過 winget 安裝 Git..."
        & winget install Git.Git --accept-source-agreements --accept-package-agreements 2>$null
        # 重新整理 PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        if (cmd_exists "git") {
            $HAS_GIT = $true
            check_ok "Git 安裝完成"
        } else {
            check_notice "Git 已安裝，但需要重新開啟終端機才會生效"
        }
    } else {
        check_notice "找不到 winget，請手動安裝 Git: https://git-scm.com/download/win"
    }
}

# ─── ffmpeg ───────────────────────────────────────────────────
if (cmd_exists "ffmpeg") {
    check_ok "ffmpeg"
} else {
    check_missing "找不到 ffmpeg（處理非 WAV 音訊時需要）"
    $ans = Read-Host "  是否自動安裝 ffmpeg？(Y/n)"
    if ($ans -ne 'n' -and $ans -ne 'N') {
        if (cmd_exists "winget") {
            info "正在透過 winget 安裝 ffmpeg..."
            & winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements 2>$null
            # winget 安裝的 ffmpeg 可能需要新終端才生效
            if (cmd_exists "ffmpeg") {
                check_ok "ffmpeg 安裝完成"
            } else {
                check_notice "ffmpeg 已安裝，但需要重新開啟終端機才會生效"
            }
        } elseif (cmd_exists "choco") {
            info "正在透過 Chocolatey 安裝 ffmpeg..."
            & choco install ffmpeg -y 2>$null
            if (cmd_exists "ffmpeg") { check_ok "ffmpeg" } else { check_notice "ffmpeg 需要重開終端" }
        } else {
            check_fail "找不到 winget 或 choco，請手動安裝 ffmpeg"
            info "下載: https://www.gyan.dev/ffmpeg/builds/"
            info "解壓後將 bin 資料夾加入系統 PATH"
        }
    }
}

# ═══════════════════════════════════════════════════════════════
# 2. Python 虛擬環境
# ═══════════════════════════════════════════════════════════════

section "Python 虛擬環境"

$venvNeedCreate = $true
if (Test-Path $VENV_DIR) {
    $venvPy = Join-Path $VENV_DIR "Scripts\python.exe"
    if (Test-Path $venvPy) {
        $venvCheck = & $venvPy --version 2>$null
        if ($LASTEXITCODE -eq 0) {
            check_ok "虛擬環境已存在且正常: venv\"
            $venvNeedCreate = $false
        } else {
            check_detect "虛擬環境損壞（python.exe 無法執行），正在重建..."
            Remove-Item $VENV_DIR -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        check_detect "虛擬環境不完整（缺少 Scripts\python.exe），正在重建..."
        Remove-Item $VENV_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($venvNeedCreate -and -not (Test-Path $VENV_DIR)) {
    info "正在建立虛擬環境..."
    if ($PYTHON_CMD -eq "py -3") {
        & py -3 -m venv $VENV_DIR
    } else {
        & $PYTHON_CMD -m venv $VENV_DIR
    }
    if (-not (Test-Path (Join-Path $VENV_DIR "Scripts\python.exe"))) {
        check_fail "虛擬環境建立失敗"
        exit 1
    }
    check_ok "虛擬環境建立完成"
}

$VENV_PYTHON = Join-Path $VENV_DIR "Scripts\python.exe"
$VENV_PIP    = Join-Path $VENV_DIR "Scripts\pip.exe"

# 升級 pip（僅首次建立 venv 時）
$pipOutdated = & $VENV_PYTHON -m pip list --outdated --format=json 2>$null | ConvertFrom-Json | Where-Object { $_.name -eq "pip" }
if ($pipOutdated) {
    info "升級 pip..."
    & $VENV_PYTHON -m pip install --upgrade pip --quiet 2>$null
}

# ═══════════════════════════════════════════════════════════════
# 3. 安裝 Python 套件
# ═══════════════════════════════════════════════════════════════

section "安裝 Python 套件"

# ─── PyTorch（GPU 敏感）──────────────────────────────────────
if ($GPU_AVAILABLE) {
    $null = pip_install "torch" "PyTorch (CUDA ${TORCH_CUDA_TAG})" @("--index-url", "https://download.pytorch.org/whl/${TORCH_CUDA_TAG}")
    # cuDNN（faster-whisper CUDA 加速需要）
    $cudnnPkg = if ($TORCH_CUDA_TAG -eq "cu118") { "nvidia-cudnn-cu11" } else { "nvidia-cudnn-cu12" }
    $null = pip_install $cudnnPkg "cuDNN (CUDA 深度學習加速)" @()
    # CUDA 13+：自動安裝 CUDA 12.x cuBLAS（CTranslate2/faster-whisper 需要 cublas64_12.dll）
    if ($CUDA_13_PLUS) {
        $null = pip_install "nvidia-cublas-cu12" "cuBLAS 12.x（CUDA 13+ 相容層）" @()
    }
} else {
    $null = pip_install "torch" "PyTorch (CPU)" @("--index-url", "https://download.pytorch.org/whl/cpu")
}

# ─── setuptools<81（resemblyzer → webrtcvad 需要 pkg_resources）──
$null = pip_install "setuptools<81" "setuptools（<81，保留 pkg_resources）" @()

# ─── 核心套件 ─────────────────────────────────────────────────
# webrtcvad 預編譯版（resemblyzer 依賴，Windows 上避免需要 C 編譯器）
$null = pip_install "webrtcvad-wheels" "webrtcvad（預編譯版）" @()

$corePackages = @(
    @("numpy",                           "numpy（數值計算）"),
    @("ctranslate2",                     "ctranslate2（語音辨識加速引擎）"),
    @("sentencepiece",                   "sentencepiece（分詞工具）"),
    @("faster-whisper",                  "faster-whisper（離線語音辨識）"),
    @("scipy",                           "scipy（科學計算）"),
    @("librosa",                         "librosa（音訊分析）"),
    @("spectralcluster",                 "spectralcluster（講者辨識 - 分群）"),
    @("sounddevice",                     "sounddevice（音訊擷取）"),
    @("noisereduce",                     "noisereduce（背景降噪）"),
    @("fastapi",                         "fastapi（WebUI 伺服器）"),
    @("uvicorn",                         "uvicorn（WebUI ASGI 伺服器）"),
    @("websockets",                      "websockets（WebUI 即時通訊）"),
    @("python-multipart",               "python-multipart（WebUI 檔案上傳）"),
    @("argostranslate",                  "Argos Translate（離線翻譯備援）")
)

$installFailed = @()
foreach ($item in $corePackages) {
    $ok = pip_install $item[0] $item[1] @()
    if (-not $ok) { $installFailed += $item[1] }
}

# resemblyzer 單獨用 --no-deps 安裝（避免拉 webrtcvad 原版需要 C 編譯器）
$ok = pip_install "resemblyzer" "resemblyzer（講者辨識 - 聲紋提取）" @("--no-deps")
if (-not $ok) { $installFailed += "resemblyzer（講者辨識 - 聲紋提取）" }

# PyAudioWPatch（WASAPI Loopback 系統音訊擷取，Windows 專用）
$null = pip_install "PyAudioWPatch" "PyAudioWPatch（WASAPI 系統音訊擷取）" @()

# OpenCC（簡體→台灣繁體轉換，Argos 翻譯必須）
$null = pip_install "opencc-python-reimplemented" "OpenCC 簡繁轉換" @()

# ─── Moonshine（選裝，英文低延遲）────────────────────────────
$moonOk = pip_install "moonshine-voice" "Moonshine 串流辨識引擎" @()
if ($moonOk) {
    # get_model_for_language 有快取就直接回傳，沒有才下載
    $moonOut = & $VENV_PYTHON -c @"
try:
    from moonshine_voice import get_model_for_language, ModelArch
    get_model_for_language('en', ModelArch.MEDIUM_STREAMING)
    print('OK')
except Exception as e:
    print(f'FAIL:{e}')
"@ 2>$null
    if ($moonOut -match "OK") {
        check_ok "Moonshine medium streaming 模型已就緒"
    } else {
        check_notice "Moonshine 模型下載失敗（可稍後重試，不影響其他功能）"
    }
} else {
    check_notice "Moonshine 安裝失敗（非必要，可忽略）"
}

if ($installFailed.Count -gt 0) {
    Write-Host ""
    check_warn "以下套件安裝失敗："
    foreach ($f in $installFailed) { info "  - $f" }
    Write-Host ""
}

# ═══════════════════════════════════════════════════════════════
# 4. 下載 Argos 翻譯模型
# ═══════════════════════════════════════════════════════════════

section "下載 Argos 離線翻譯模型"

# 先檢查是否已安裝
$argosCheck = & $VENV_PYTHON -c "
try:
    import argostranslate.package as pkg
    installed = [p for p in pkg.get_installed_packages()]
    en_zh = any(p.from_code=='en' and p.to_code=='zh' for p in installed)
    zh_en = any(p.from_code=='zh' and p.to_code=='en' for p in installed)
    if en_zh and zh_en: print('OK')
    elif en_zh: print('MISS_ZH_EN')
    elif zh_en: print('MISS_EN_ZH')
    else: print('NONE')
except: print('NONE')
" 2>$null

if ($argosCheck -eq "OK") {
    check_ok "Argos 翻譯模型（en<->zh，已安裝）"
} else {
    info "下載英翻中 / 中翻英模型..."
    & $VENV_PYTHON -c @"
try:
    import argostranslate.package as pkg
    pkg.update_package_index()
    avail = pkg.get_available_packages()
    for p in avail:
        if (p.from_code == 'en' and p.to_code == 'zh') or \
           (p.from_code == 'zh' and p.to_code == 'en'):
            pkg.install_from_path(p.download())
except Exception:
    pass
"@ 2>&1 | Out-Null

    # 重新檢查
    $argosOut = & $VENV_PYTHON -c "
try:
    import argostranslate.package as pkg
    installed = [p for p in pkg.get_installed_packages()]
    en_zh = any(p.from_code=='en' and p.to_code=='zh' for p in installed)
    zh_en = any(p.from_code=='zh' and p.to_code=='en' for p in installed)
    if en_zh and zh_en: print('OK')
    elif en_zh: print('MISS_ZH_EN')
    elif zh_en: print('MISS_EN_ZH')
    else: print('FAIL')
except: print('FAIL')
" 2>$null

    if ($argosOut -eq "OK") {
        check_ok "Argos 翻譯模型（en<->zh）"
    } elseif ($argosOut -eq "MISS_ZH_EN") {
        check_notice "Argos 翻譯模型：英翻中已安裝，中翻英下載失敗"
    } elseif ($argosOut -eq "MISS_EN_ZH") {
        check_notice "Argos 翻譯模型：中翻英已安裝，英翻中下載失敗"
    } else {
        check_notice "Argos 模型下載失敗，可稍後在有網路時重新執行安裝"
    }
}

# ═══════════════════════════════════════════════════════════════
# 4b. 下載 NLLB 離線翻譯模型（中日英互譯，CC-BY-NC 4.0 授權）
# ═══════════════════════════════════════════════════════════════

section "下載 NLLB 離線翻譯模型（中日英互譯）"

$NLLB_MODEL_DIR = Join-Path $env:LOCALAPPDATA "jt-live-whisper\models\nllb-600m"

if ((Test-Path (Join-Path $NLLB_MODEL_DIR "model.bin")) -and
    (Test-Path (Join-Path $NLLB_MODEL_DIR "sentencepiece.bpe.model"))) {
    check_ok "NLLB 模型已安裝（$NLLB_MODEL_DIR）"
} else {
    info "下載 NLLB 600M 模型（約 600MB）..."
    & $VENV_PYTHON -m pip install --disable-pip-version-check -q huggingface_hub 2>$null | Out-Null
    New-Item -ItemType Directory -Path $NLLB_MODEL_DIR -Force | Out-Null
    $null = hf_download "JustFrederik/nllb-200-distilled-600M-ct2-int8" "NLLB 模型" $NLLB_MODEL_DIR

    if (Test-Path (Join-Path $NLLB_MODEL_DIR "model.bin")) {
        check_ok "NLLB 模型安裝完成"
    }
}

# ═══════════════════════════════════════════════════════════════
# 5. whisper.cpp 編譯（選裝 — 本機即時辨識用）
# ═══════════════════════════════════════════════════════════════

section "whisper.cpp 即時辨識引擎"

$WHISPER_STREAM_EXE = ""

if ($true) {

    $canBuild = $true
    $HAS_WINGET = cmd_exists "winget"

    # 檢查 Git（環境偵測階段已自動安裝，這裡僅確認）
    if (-not $HAS_GIT) {
        check_fail "找不到 Git，whisper.cpp 編譯需要 Git"
        $canBuild = $false
    } else {
        check_ok "Git"
    }

    # 檢查 CMake
    if (cmd_exists "cmake") {
        check_ok "CMake"
    } else {
        if ($HAS_WINGET) {
            info "找不到 CMake，正在自動安裝..."
            & winget install Kitware.CMake --accept-source-agreements --accept-package-agreements 2>$null
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if (cmd_exists "cmake") {
                check_ok "CMake 安裝完成"
            } else {
                check_notice "CMake 已安裝，但需要重新開啟終端機才會生效"
                $canBuild = $false
            }
        } else {
            check_fail "找不到 CMake：請從 https://cmake.org/download/ 下載安裝"
            $canBuild = $false
        }
    }

    # 檢查 MSVC (Visual Studio Build Tools)
    $hasMSVC = $false
    $vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vsWhere) {
        $vsPath = & $vsWhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null
        if ($vsPath) { $hasMSVC = $true }
    }
    if (-not $hasMSVC -and (cmd_exists "cl")) { $hasMSVC = $true }

    if ($hasMSVC) {
        check_ok "Visual Studio C++ 編譯器"
    } else {
        if ($HAS_WINGET) {
            check_missing "找不到 Visual Studio C++ 編譯器，正在自動安裝..."
            info "這是編譯 whisper.cpp 的必要元件（約 2-6 GB）"
            info "下載較大，請耐心等候..."
            # 先確保 Build Tools 基底已安裝
            & winget install Microsoft.VisualStudio.2022.BuildTools `
                --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
            # 用 VS 安裝器加裝 C++ workload（處理已安裝但缺 C++ 的情況）
            $vsInstaller = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vs_installer.exe"
            if (Test-Path $vsInstaller) {
                info "正在加裝 C++ 桌面開發工作負載（需要管理員權限）..."
                $btPath = & $vsWhere -latest -products * -property installationPath 2>$null
                if ($btPath) {
                    $vsArgs = "/c `"`"$vsInstaller`" modify --installPath `"$btPath`" --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive >NUL 2>&1`""
                    Start-Process cmd.exe -ArgumentList $vsArgs -Verb RunAs -Wait -WindowStyle Hidden
                }
            }
            # 重新整理 PATH
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
            # 重新偵測
            if (Test-Path $vsWhere) {
                $vsPath = & $vsWhere -latest -products * `
                    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
                    -property installationPath 2>$null
                if ($vsPath) { $hasMSVC = $true }
            }
            if (-not $hasMSVC -and (cmd_exists "cl")) { $hasMSVC = $true }
            if ($hasMSVC) {
                check_ok "Visual Studio C++ 編譯器安裝完成"
            } else {
                check_notice "C++ 編譯器安裝完成，但需要重新開啟終端機"
                info "請重新開啟終端機後再執行 .\\install.ps1"
                $canBuild = $false
            }
        } else {
            check_fail "找不到 Visual Studio Build Tools"
            info "請從以下位址下載安裝："
            info "  https://visualstudio.microsoft.com/visual-cpp-build-tools/"
            info "  安裝時選擇「使用 C++ 的桌面開發」工作負載"
            $canBuild = $false
        }
    }

    # 有 GPU 但沒有 CUDA Toolkit
    if ($GPU_AVAILABLE) {
        $hasCudaTK = Test-Path "${env:CUDA_PATH}\bin\nvcc.exe"
        if (-not $hasCudaTK) {
            # 嘗試常見路徑
            $cudaPaths = Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue
            if ($cudaPaths) { $hasCudaTK = $true }
        }
        if ($hasCudaTK) {
            check_ok "CUDA Toolkit"
        } else {
            check_notice "未偵測到 CUDA Toolkit，whisper.cpp 將編譯為 CPU 版"
            info "如需 GPU 加速，請先安裝 CUDA Toolkit："
            info "  https://developer.nvidia.com/cuda-downloads"
        }
    }

    if ($canBuild) {
        # Clone whisper.cpp
        if (-not (Test-Path $WHISPER_CPP_DIR)) {
            info "正在下載 whisper.cpp 原始碼..."
            & git clone --depth 1 https://github.com/ggerganov/whisper.cpp $WHISPER_CPP_DIR 2>$null
            if (-not (Test-Path (Join-Path $WHISPER_CPP_DIR "CMakeLists.txt"))) {
                check_fail "whisper.cpp 下載失敗"
                $canBuild = $false
            }
        } else {
            check_ok "whisper.cpp 原始碼已存在"
        }
    }

    if ($canBuild) {
        # 下載 SDL2（即時音訊擷取需要）
        $sdl2Dir = Join-Path $WHISPER_CPP_DIR "SDL2"
        if (-not (Test-Path $sdl2Dir)) {
            info "下載 SDL2 開發程式庫..."
            $sdl2Url = "https://github.com/libsdl-org/SDL/releases/download/release-2.30.10/SDL2-devel-2.30.10-VC.zip"
            $sdl2Zip = Join-Path $env:TEMP "sdl2-devel-$(Get-Random).zip"
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $oldProg = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri $sdl2Url -OutFile $sdl2Zip -UseBasicParsing -ErrorAction Stop
                $ProgressPreference = $oldProg
                Expand-Archive -Path $sdl2Zip -DestinationPath $WHISPER_CPP_DIR -Force
                # 重新命名解壓資料夾
                $extracted = Get-ChildItem $WHISPER_CPP_DIR -Directory -Filter "SDL2-*" | Select-Object -First 1
                if ($extracted) {
                    if (Test-Path $sdl2Dir) { Remove-Item $sdl2Dir -Recurse -Force }
                    Rename-Item $extracted.FullName "SDL2"
                }
                Remove-Item $sdl2Zip -Force -ErrorAction SilentlyContinue
                check_ok "SDL2 開發程式庫"
            } catch {
                $ProgressPreference = $oldProg
                check_warn "SDL2 下載失敗：$($_.Exception.Message)"
                info "whisper-stream 需要 SDL2，即時本機辨識可能無法使用"
            }
        } else {
            check_ok "SDL2 已存在"
        }

        # 載入 MSVC 編譯環境（vcvarsall.bat）
        $vcvarsall = ""
        if (Test-Path $vsWhere) {
            $vsInstPath = & $vsWhere -latest -products * `
                -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
                -property installationPath 2>$null
            if ($vsInstPath) {
                $vc = Join-Path $vsInstPath "VC\Auxiliary\Build\vcvarsall.bat"
                if (Test-Path $vc) { $vcvarsall = $vc }
            }
        }
        if ($vcvarsall) {
            info "載入 MSVC 編譯環境..."
            $vcEnv = cmd /c "`"$vcvarsall`" x64 >NUL 2>&1 && set" 2>$null
            foreach ($line in $vcEnv) {
                if ($line -match "^([^=]+)=(.*)$") {
                    [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
                }
            }
        }

        # CMake 設定 + 編譯
        $buildDir = Join-Path $WHISPER_CPP_DIR "build"

        # 檢查是否已編譯過（whisper-stream.exe 已存在）
        $existingExe = $null
        if (Test-Path $buildDir) {
            $searchPaths = @(
                (Join-Path $buildDir "bin\Release\whisper-stream.exe"),
                (Join-Path $buildDir "bin\whisper-stream.exe"),
                (Join-Path $buildDir "examples\stream\Release\whisper-stream.exe"),
                (Join-Path $buildDir "examples\stream\whisper-stream.exe"),
                (Join-Path $buildDir "Release\whisper-stream.exe")
            )
            foreach ($sp in $searchPaths) {
                if (Test-Path $sp) { $existingExe = $sp; break }
            }
            if (-not $existingExe) {
                $found = Get-ChildItem -Path $buildDir -Filter "whisper-stream.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($found) { $existingExe = $found.FullName }
            }
        }

        if ($existingExe) {
            $WHISPER_STREAM_EXE = $existingExe
            check_ok "whisper-stream 已編譯（${WHISPER_STREAM_EXE}）"
        } else {
            $cmakeArgs = @(
                "-S", $WHISPER_CPP_DIR,
                "-B", $buildDir,
                "-DCMAKE_BUILD_TYPE=Release",
                "-DWHISPER_BUILD_EXAMPLES=ON",
                "-DWHISPER_SDL2=ON"
            )
            if (Test-Path $sdl2Dir) {
                $sdl2CmakeDir = Join-Path $sdl2Dir "cmake"
                if (Test-Path $sdl2CmakeDir) {
                    $cmakeArgs += "-DSDL2_DIR=$sdl2CmakeDir"
                }
            }

            $buildDesc = "CPU 版"
            if ($GPU_AVAILABLE -and $hasCudaTK) {
                $cmakeArgs += "-DGGML_CUDA=ON"
                $buildDesc = "CUDA GPU 加速版"
            }

            # 若有舊的 build 目錄，先清除（避免快取干擾）
            if (Test-Path $buildDir) {
                Remove-Item $buildDir -Recurse -Force -ErrorAction SilentlyContinue
            }

            info "CMake 設定（${buildDesc}）..."
            $cmakeOutput = & cmake @cmakeArgs 2>&1

            if ($LASTEXITCODE -ne 0 -and $buildDesc -eq "CUDA GPU 加速版") {
                # CUDA 編譯失敗（常見：CUDA Toolkit 與 VS 整合不完整），自動降級為 CPU 版
                check_warn "CUDA 編譯設定失敗，自動改用 CPU 版編譯"
                info "（whisper.cpp 即時辨識改用 CPU，不影響 faster-whisper 的 CUDA 加速）"
                info "若需 GPU 加速 whisper.cpp，請重新安裝 CUDA Toolkit 並勾選 Visual Studio Integration"
                $cmakeArgs = $cmakeArgs | Where-Object { $_ -ne "-DGGML_CUDA=ON" }
                $buildDesc = "CPU 版（CUDA 降級）"
                if (Test-Path $buildDir) {
                    Remove-Item $buildDir -Recurse -Force -ErrorAction SilentlyContinue
                }
                info "CMake 設定（${buildDesc}）..."
                $cmakeOutput = & cmake @cmakeArgs 2>&1
            }

            if ($LASTEXITCODE -ne 0) {
                check_fail "CMake 設定失敗"
                # 顯示最後幾行錯誤訊息協助診斷
                $errLines = ($cmakeOutput | Select-Object -Last 10) -join "`n"
                if ($errLines) { Write-Host "  ${C_DIM}${errLines}${NC}" }
                info "請嘗試在「Developer PowerShell for VS 2022」中重新執行"
            } else {
                info "編譯中（可能需要數分鐘）..."
                & cmake --build $buildDir --config Release 2>&1 | Out-Null

                # 尋找 whisper-stream.exe（可能在不同子路徑）
                $candidates = @(
                    (Join-Path $buildDir "bin\Release\whisper-stream.exe"),
                    (Join-Path $buildDir "bin\whisper-stream.exe"),
                    (Join-Path $buildDir "examples\stream\Release\whisper-stream.exe"),
                    (Join-Path $buildDir "examples\stream\whisper-stream.exe"),
                    (Join-Path $buildDir "Release\whisper-stream.exe")
                )
                # 萬一都找不到，遞迴搜尋整個 build 目錄
                foreach ($c in $candidates) {
                    if (Test-Path $c) { $WHISPER_STREAM_EXE = $c; break }
                }
                if (-not $WHISPER_STREAM_EXE) {
                    $found = Get-ChildItem -Path $buildDir -Filter "whisper-stream.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($found) { $WHISPER_STREAM_EXE = $found.FullName }
                }

                if ($WHISPER_STREAM_EXE) {
                    check_ok "whisper.cpp 編譯完成（${buildDesc}）"
                    check_ok "whisper-stream: $WHISPER_STREAM_EXE"
                } else {
                    check_fail "whisper.cpp 編譯完成但找不到 whisper-stream.exe"
                    info "請檢查 build 資料夾內容"
                }
            }
        }

        # 下載 GGML 模型（不論是新編譯還是已存在，都檢查模型）
        if ($WHISPER_STREAM_EXE) {
            $modelDir = Join-Path $WHISPER_CPP_DIR "models"
            if (-not (Test-Path $modelDir)) { New-Item $modelDir -ItemType Directory -Force | Out-Null }

            $ggmlModels = @(
                @{
                    Name = "large-v3-turbo"
                    File = "ggml-large-v3-turbo.bin"
                    Size = "1.5 GB"
                    Url  = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
                },
                @{
                    Name = "large-v3"
                    File = "ggml-large-v3.bin"
                    Size = "3.1 GB"
                    Url  = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin"
                }
            )

            foreach ($m in $ggmlModels) {
                $mPath = Join-Path $modelDir $m.File
                if (Test-Path $mPath) {
                    check_ok "Whisper 模型 $($m.Name) 已存在"
                } else {
                    $dlModel = Read-Host "  下載 Whisper 模型 $($m.Name) ($($m.Size))？(Y/n)"
                    if ($dlModel -ne 'n' -and $dlModel -ne 'N') {
                        info "下載中（$($m.Size)）..."
                        try {
                            # 優先用 curl.exe（Windows 10+ 內建，有進度條）
                            $curlExe = Get-Command curl.exe -ErrorAction SilentlyContinue
                            if ($curlExe) {
                                & curl.exe -L --progress-bar -o $mPath $m.Url
                                if ($LASTEXITCODE -ne 0) { throw "curl 下載失敗 (exit code $LASTEXITCODE)" }
                            } else {
                                # Fallback: Invoke-WebRequest（無進度條但可靠）
                                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                                $oldProg = $ProgressPreference
                                $ProgressPreference = 'SilentlyContinue'
                                Invoke-WebRequest -Uri $m.Url -OutFile $mPath -UseBasicParsing -ErrorAction Stop
                                $ProgressPreference = $oldProg
                            }
                            check_ok "Whisper 模型 $($m.Name)"
                        } catch {
                            check_fail "模型下載失敗：$($_.Exception.Message)"
                            info "可稍後手動下載: $($m.Url)"
                        }
                    } else {
                        info "跳過 $($m.Name)"
                    }
                }
            }
        }
    } else {
        check_skip "缺少編譯工具，跳過 whisper.cpp"
        info "仍可使用：離線模式、Moonshine 即時辨識、GPU 伺服器 即時辨識"
    }
}

# ═══════════════════════════════════════════════════════════════
# 5b. faster-whisper 模型預下載
# ═══════════════════════════════════════════════════════════════

section "faster-whisper 模型預下載（large-v3-turbo）"

$fwModelFound = & $VENV_PYTHON -c @"
import os
found = False
dirs = []
try:
    from huggingface_hub.constants import HF_HUB_CACHE
    dirs.append(HF_HUB_CACHE)
except: pass
default = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
if default not in dirs:
    dirs.append(default)
for d in dirs:
    for prefix in ['Systran', 'mobiuslabsgmbh']:
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-large-v3-turbo')):
            found = True
            break
    if found:
        break
print('found' if found else 'notfound')
"@ 2>$null

if ($fwModelFound -eq "found") {
    check_ok "faster-whisper 模型 large-v3-turbo 已存在"
} else {
    info "下載 faster-whisper large-v3-turbo 模型（約 1.6GB）..."
    & $VENV_PYTHON -m pip install --disable-pip-version-check -q huggingface_hub 2>$null | Out-Null
    # mobiuslabsgmbh 是 faster-whisper 內部預設的 repo（Systran 已需認證）
    $null = hf_download "mobiuslabsgmbh/faster-whisper-large-v3-turbo" "faster-whisper 模型" ""

    # 驗證下載
    $fwVerify = & $VENV_PYTHON -c @"
import os
found = False
dirs = []
try:
    from huggingface_hub.constants import HF_HUB_CACHE
    dirs.append(HF_HUB_CACHE)
except: pass
default = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
if default not in dirs:
    dirs.append(default)
for d in dirs:
    for prefix in ['Systran', 'mobiuslabsgmbh']:
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-large-v3-turbo')):
            found = True
            break
    if found:
        break
print('found' if found else 'notfound')
"@ 2>$null

    if ($fwVerify -eq "found") {
        check_ok "faster-whisper 模型 large-v3-turbo 安裝完成"
    }
}

# ═══════════════════════════════════════════════════════════════
# 6. LLM 伺服器設定
# ═══════════════════════════════════════════════════════════════

section "LLM 伺服器設定（翻譯 / 摘要用）"

$cfg = read_config

if (($cfg | Get-Member -Name "llm_host") -and $cfg.llm_host) {
    check_ok "LLM 伺服器已設定: $($cfg.llm_host):$($cfg.llm_port)"
    info "如需修改，請編輯 config.json"
} else {
    info "程式需要 LLM 伺服器（Ollama 等）來翻譯和摘要"
    info "推薦: 在本機或區域網路主機安裝 Ollama（https://ollama.com）"
    if (-not $GPU_AVAILABLE) {
        info "本機無 GPU，建議將 LLM 伺服器安裝在有 GPU 的主機上"
    }
    Write-Host ""
    $llmHost = Read-Host "  LLM 伺服器位址（例 127.0.0.1 或 192.168.1.40，留空跳過）"

    if ($llmHost) {
        $llmPort = Read-Host "  LLM 伺服器 Port（Ollama 預設 11434）"
        if (-not $llmPort) { $llmPort = "11434" }

        # 建立或更新 config
        $newCfg = read_config
        $newCfg | Add-Member -NotePropertyName "llm_host" -NotePropertyValue $llmHost -Force
        $newCfg | Add-Member -NotePropertyName "llm_port" -NotePropertyValue ([int]$llmPort) -Force
        save_config $newCfg

        check_ok "LLM 伺服器: ${llmHost}:${llmPort}"
    } else {
        info "跳過 LLM 設定（可稍後編輯 config.json）"
        info "不設定 LLM 仍可使用 NLLB / Argos 離線翻譯引擎"
    }
}

# ═══════════════════════════════════════════════════════════════
# 7. GPU 伺服器設定（選填）
# ═══════════════════════════════════════════════════════════════

# ─── SSH Helper ──────────────────────────────────────────────
# Windows SSH 不支援 ControlMaster，所有操作直接連線
function ssh_cmd([string]$sshOpts, [string]$userHost, [string]$remoteCmd) {
    $argList = $sshOpts.Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
    $argList += $userHost
    $argList += $remoteCmd
    $result = & ssh @argList 2>$null
    return $result
}

function ssh_test([string]$sshOpts, [string]$userHost, [string]$remoteCmd) {
    $argList = $sshOpts.Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
    $argList += $userHost
    $argList += $remoteCmd
    & ssh @argList 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function scp_file([string]$scpOpts, [string]$localFile, [string]$remoteDest) {
    $argList = $scpOpts.Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
    $argList += $localFile
    $argList += $remoteDest
    & scp @argList 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# ─── SSH 金鑰自動部署（避免重複輸入密碼）─────────────────────
function ensure_ssh_key_auth([string]$userHost, [string]$sshPort) {
    # 1. 已有 key 且 BatchMode 連線成功 → 免密碼
    if ($script:rw_key -and (Test-Path $script:rw_key)) {
        $batchOpts = "-o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $sshPort -i $($script:rw_key)"
        if (ssh_test $batchOpts $userHost "echo ok") {
            # 確保後續全程用 BatchMode + key，避免任何互動提示
            $script:sshOpts = "-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $sshPort -i $($script:rw_key)"
            check_ok "SSH 金鑰驗證成功（免密碼）"
            return
        }
    }

    # 2. 無 key → 自動產生 ed25519 金鑰
    $autoKey = Join-Path $env:USERPROFILE ".ssh\jt_whisper_ed25519"
    if (-not $script:rw_key) {
        if (-not (Test-Path $autoKey)) {
            $sshDir = Join-Path $env:USERPROFILE ".ssh"
            if (-not (Test-Path $sshDir)) {
                New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
            }
            info "自動產生 SSH 金鑰..."
            & ssh-keygen -t ed25519 -f "$autoKey" -N '""' -q -C "jt-whisper-auto" 2>$null | Out-Null
            if (Test-Path $autoKey) {
                check_ok "SSH 金鑰已產生: $autoKey"
            } else {
                check_fail "SSH 金鑰產生失敗"
                return
            }
        }
        $script:rw_key = $autoKey
    }

    # 3. BatchMode 測試 key 是否已部署到伺服器
    $batchOpts = "-o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $sshPort -i $($script:rw_key)"
    if (ssh_test $batchOpts $userHost "echo ok") {
        $script:sshOpts = "-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $sshPort -i $($script:rw_key)"
        check_ok "SSH 金鑰已部署（免密碼）"
        return
    }

    # 4. 未部署 → 一次 SSH 部署公鑰（唯一一次輸入密碼）
    $pubKeyFile = "$($script:rw_key).pub"
    if (-not (Test-Path $pubKeyFile)) {
        check_fail "找不到公鑰: $pubKeyFile"
        return
    }
    info "首次連線，請輸入一次 SSH 密碼以部署金鑰..."
    $pubKey = (Get-Content $pubKeyFile -Raw).Trim()
    $deployOpts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $sshPort"
    $deployCmd = "echo ok && mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$pubKey' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    $argList = $deployOpts.Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
    $argList += $userHost
    $argList += $deployCmd
    & ssh @argList
    if ($LASTEXITCODE -eq 0) {
        $script:sshOpts = "-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $sshPort -i $($script:rw_key)"
        check_ok "SSH 公鑰已部署，之後免密碼登入"
    } else {
        check_fail "SSH 連線或公鑰部署失敗"
    }
}

# ─── CTranslate2 原始碼編譯（aarch64 CUDA）──────────────────
function build_ctranslate2_from_source([string]$sshOpts, [string]$userHost) {
    $REMOTE_PIP = "~/jt-whisper-server/venv/bin/pip"
    $REMOTE_PY  = "~/jt-whisper-server/venv/bin/python3"
    $WHEEL_CACHE = "~/jt-whisper-server/.ct2-wheels"
    $BUILD_DIR   = "/tmp/ctranslate2-build"

    Write-Host ""
    Write-Host "  ${C_WHITE}[CTranslate2] aarch64 偵測到，嘗試從原始碼編譯 CUDA 版...${NC}"

    # 1. 檢查快取 wheel
    $cachedWhl = ssh_cmd $sshOpts $userHost "ls ${WHEEL_CACHE}/ctranslate2-*.whl 2>/dev/null | head -1"
    if ($cachedWhl) {
        check_ok "找到已編譯 wheel: $(Split-Path -Leaf $cachedWhl)"
        info "安裝快取 wheel..."
        ssh_cmd $sshOpts $userHost "${REMOTE_PIP} install --disable-pip-version-check --force-reinstall '$cachedWhl' 2>&1" | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ct2Verify = ssh_cmd $sshOpts $userHost "LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH ${REMOTE_PY} -c `"import ctranslate2; types=ctranslate2.get_supported_compute_types('cuda'); print('ok' if types else 'no')`""
            if ($ct2Verify -eq "ok") {
                check_ok "CTranslate2 CUDA 驗證通過（快取 wheel）"
                ssh_cmd $sshOpts $userHost "${REMOTE_PIP} install --disable-pip-version-check --force-reinstall --no-deps faster-whisper" | Out-Null
                return $true
            }
            check_warn "快取 wheel CUDA 驗證失敗，重新編譯"
        } else {
            check_warn "快取 wheel 安裝失敗，重新編譯"
        }
    }

    # 2. 檢查前提條件 — nvcc
    $nvccPath = ssh_cmd $sshOpts $userHost "if command -v nvcc &>/dev/null; then command -v nvcc; elif [ -x /usr/local/cuda/bin/nvcc ]; then echo /usr/local/cuda/bin/nvcc; elif ls /usr/local/cuda-*/bin/nvcc 2>/dev/null | head -1; then true; else echo ''; fi"
    if (-not $nvccPath) {
        check_skip "nvcc 未安裝（需要 CUDA Toolkit），無法編譯 CTranslate2"
        return $false
    }
    $cudaBinDir = ssh_cmd $sshOpts $userHost "dirname '$nvccPath'"
    info "nvcc: ${nvccPath}"

    # 編譯工具
    $needApt = ""
    foreach ($tool in @("cmake", "git", "g++", "make")) {
        $has = ssh_test $sshOpts $userHost "export PATH=${cudaBinDir}:`$PATH && command -v $tool"
        if (-not $has) {
            if ($tool -eq "g++") { $needApt += " g++ build-essential" } else { $needApt += " $tool" }
        }
    }
    foreach ($pkg in @("python3-dev", "libopenblas-dev")) {
        $has = ssh_test $sshOpts $userHost "dpkg -s $pkg"
        if (-not $has) { $needApt += " $pkg" }
    }
    if ($needApt) {
        info "安裝編譯工具:${needApt}..."
        ssh_cmd $sshOpts $userHost "apt update -qq && apt install -y -qq $needApt 2>&1" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            check_skip "無法安裝編譯工具，無法編譯 CTranslate2"
            return $false
        }
    }
    check_ok "編譯工具就緒"

    # cuDNN
    $hasCudnn = ssh_cmd $sshOpts $userHost "ldconfig -p 2>/dev/null | grep -c libcudnn"
    $cudnnFlag = "OFF"
    if ($hasCudnn -and [int]$hasCudnn -gt 0) {
        $cudnnFlag = "ON"
        info "cuDNN 偵測到，將啟用 cuDNN 加速"
    } else {
        info "cuDNN 未偵測到（可選，不影響編譯）"
    }

    # 磁碟空間
    $availMb = ssh_cmd $sshOpts $userHost "df -m /tmp | awk 'NR==2{print `$4}'"
    if ($availMb -and [int]$availMb -lt 3000) {
        check_skip "/tmp 磁碟空間不足（${availMb}MB < 3GB），無法編譯"
        return $false
    }

    # 3. GPU 架構
    $gpuArch = ssh_cmd $sshOpts $userHost "nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' '"
    if (-not $gpuArch) {
        check_skip "無法偵測 GPU 架構"
        return $false
    }
    $cmakeArch = $gpuArch -replace '\.', ''
    info "GPU 架構: sm_${cmakeArch}（compute capability ${gpuArch}）"

    # 4. 編譯（7 步驟）
    $CUDA_ENV = "export PATH=${cudaBinDir}:`$PATH && export LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH"
    Write-Host "  ${C_WHITE}  開始編譯 CTranslate2（預計 10-20 分鐘）...${NC}"

    ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR} && mkdir -p ${BUILD_DIR}" | Out-Null

    # 4a. git clone
    info "[1/7] 下載 CTranslate2 原始碼..."
    ssh_cmd $sshOpts $userHost "cd ${BUILD_DIR} && git clone --depth 1 --recurse-submodules https://github.com/OpenNMT/CTranslate2.git src 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "git clone 失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4b. cmake
    info "[2/7] cmake 設定（CUDA ${gpuArch}, cuDNN=${cudnnFlag}）..."
    ssh_cmd $sshOpts $userHost "${CUDA_ENV} && mkdir -p ${BUILD_DIR}/src/build && cd ${BUILD_DIR}/src/build && cmake .. -DCMAKE_BUILD_TYPE=Release -DWITH_CUDA=ON -DWITH_CUDNN=${cudnnFlag} -DWITH_MKL=OFF -DWITH_OPENBLAS=ON -DCMAKE_CUDA_ARCHITECTURES=${cmakeArch} -DOPENMP_RUNTIME=NONE -DCMAKE_INSTALL_PREFIX=/usr/local 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "cmake 設定失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4c. make
    $ncpu = ssh_cmd $sshOpts $userHost "nproc"
    if (-not $ncpu) { $ncpu = "4" }
    info "[3/7] 編譯 C++ 原始碼（make -j${ncpu}，此步驟最久）..."
    ssh_cmd $sshOpts $userHost "${CUDA_ENV} && cd ${BUILD_DIR}/src/build && make -j${ncpu} 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "make 編譯失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4d. make install + ldconfig
    info "[4/7] 安裝系統函式庫（make install + ldconfig）..."
    ssh_cmd $sshOpts $userHost "${CUDA_ENV} && cd ${BUILD_DIR}/src/build && make install 2>&1 && ldconfig 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "make install 失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4e. Python wheel
    info "[5/7] 建構 Python wheel..."
    ssh_cmd $sshOpts $userHost "${CUDA_ENV} && cd ${BUILD_DIR}/src/python && ${REMOTE_PIP} install --disable-pip-version-check setuptools wheel pybind11 2>&1 && ${REMOTE_PY} setup.py bdist_wheel 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "Python wheel 建構失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4f. pip install wheel
    info "[6/7] 安裝 CTranslate2 wheel..."
    ssh_cmd $sshOpts $userHost "${CUDA_ENV} && whl=`$(ls ${BUILD_DIR}/src/python/dist/ctranslate2-*.whl 2>/dev/null | head -1) && [ -n `"`$whl`" ] && ${REMOTE_PIP} install --disable-pip-version-check --force-reinstall `"`$whl`" 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        check_fail "wheel 安裝失敗"
        ssh_cmd $sshOpts $userHost "rm -rf ${BUILD_DIR}" | Out-Null
        return $false
    }

    # 4g. 快取 wheel + 清理
    info "[7/7] 快取 wheel 並清理暫存檔..."
    ssh_cmd $sshOpts $userHost "mkdir -p ${WHEEL_CACHE} && cp ${BUILD_DIR}/src/python/dist/ctranslate2-*.whl ${WHEEL_CACHE}/ 2>&1 && rm -rf ${BUILD_DIR}" | Out-Null

    # 5. 驗證 CTranslate2 CUDA
    $ct2Verify = ssh_cmd $sshOpts $userHost "${CUDA_ENV} && ${REMOTE_PY} -c `"import ctranslate2; types=ctranslate2.get_supported_compute_types('cuda'); print(','.join(types) if types else 'no')`""
    if (-not $ct2Verify -or $ct2Verify -eq "no") {
        check_warn "CTranslate2 編譯完成但 CUDA 驗證失敗"
        return $false
    }
    check_ok "CTranslate2 CUDA 支援: ${ct2Verify}"

    # 6. 確認 libctranslate2.so
    $libCheck = ssh_cmd $sshOpts $userHost "ldconfig -p 2>/dev/null | grep -c libctranslate2"
    if ($libCheck -and [int]$libCheck -gt 0) {
        check_ok "libctranslate2.so 已註冊（ldconfig）"
    } else {
        info "libctranslate2.so 未在 ldconfig 中（透過 LD_LIBRARY_PATH 載入）"
    }

    # 7. 重裝 faster-whisper + 驗證
    info "重新安裝 faster-whisper..."
    ssh_cmd $sshOpts $userHost "${REMOTE_PIP} install --disable-pip-version-check --force-reinstall --no-deps faster-whisper 2>&1" | Out-Null
    $fwVerify = ssh_cmd $sshOpts $userHost "${CUDA_ENV} && ${REMOTE_PY} -c `"from faster_whisper import WhisperModel; m=WhisperModel('tiny',device='cuda',compute_type='float16'); print('ok')`""
    if ($fwVerify -eq "ok") {
        check_ok "faster-whisper CUDA 載入驗證通過"
    } else {
        check_warn "faster-whisper 無法以 CUDA 載入模型"
        return $false
    }
    return $true
}

# ─── 伺服器辨識模型預下載 ─────────────────────────────────────
function download_remote_models([string]$sshOpts, [string]$userHost) {
    info "預下載辨識模型（首次約 6 GB）..."
    $modelScript = @'
import sys
use_openai = False
try:
    import ctranslate2
    if not ctranslate2.get_supported_compute_types('cuda'):
        use_openai = True
except:
    use_openai = True
if use_openai:
    try:
        import whisper
    except ImportError:
        use_openai = False
models = ['base.en', 'small.en', 'medium.en', 'large-v3-turbo', 'large-v3']
if use_openai:
    name_map = {'large-v3-turbo': 'turbo'}
    for m in models:
        ow_name = name_map.get(m, m)
        try:
            whisper.load_model(ow_name, device='cpu')
            print(f'  {m}: 已就緒', flush=True)
        except Exception as e:
            print(f'  {m}: 下載失敗 ({e})', flush=True)
else:
    import os, logging
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    logging.getLogger('huggingface_hub').setLevel(logging.ERROR)
    from faster_whisper import WhisperModel
    for m in models:
        try:
            WhisperModel(m, device='cpu', compute_type='float32')
            print(f'  {m}: 已就緒', flush=True)
        except Exception as e:
            print(f'  {m}: 下載失敗 ({e})', flush=True)
'@
    $modelOut = ssh_cmd $sshOpts $userHost "LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c `"$($modelScript -replace '"','\"')`""
    if ($modelOut) {
        $modelOut | Where-Object { $_ -notmatch "^Shared connection" } | ForEach-Object { Write-Host "  ${C_DIM}$_${NC}" }
    }
    check_ok "辨識模型檢查完成"
}

# ─── setup_remote_whisper（主函式）──────────────────────────
section "GPU 伺服器 語音辨識伺服器（非必要，若未裝則用本機進行語音辨識）"

# 先檢查是否有 SSH
if (-not (cmd_exists "ssh")) {
    check_missing "找不到 ssh 指令，跳過GPU 伺服器 設定"
    info "Windows 10 1809+ 內建 OpenSSH，請在「選用功能」中啟用"
} else {

$SERVER_PY = Join-Path $SCRIPT_DIR "remote_whisper_server.py"
$doInstall = $false

# 讀取既有設定
$rwCfg = read_config
$existingHost = ""
if (($rwCfg | Get-Member -Name "remote_whisper") -and $rwCfg.remote_whisper) {
    $rw = $rwCfg.remote_whisper
    $existingHost = $rw.host
}

if ($existingHost) {
    # ─── 既有設定：驗證伺服器環境 ─────────────────────────────
    $rw_host     = $rw.host
    $rw_ssh_port = if ($rw | Get-Member -Name "ssh_port") { $rw.ssh_port } else { 22 }
    $rw_user     = if ($rw | Get-Member -Name "ssh_user") { $rw.ssh_user } else { "root" }
    $rw_key      = if ($rw | Get-Member -Name "ssh_key")  { $rw.ssh_key }  else { "" }
    $rw_port     = if ($rw | Get-Member -Name "whisper_port") { $rw.whisper_port } else { 8978 }

    Write-Host "  ${C_WHITE}已有伺服器設定: ${rw_user}@${rw_host}:${rw_ssh_port}${NC}"

    $sshOpts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $rw_ssh_port"
    if ($rw_key) { $sshOpts += " -i $rw_key" }
    $userHost = "${rw_user}@${rw_host}"

    # 自動確保 SSH 金鑰驗證（避免重複輸入密碼）
    ensure_ssh_key_auth $userHost $rw_ssh_port

    # 重建 sshOpts（加入 BatchMode + key，不依賴函式 scope 更新）
    $autoKey = Join-Path $env:USERPROFILE ".ssh\jt_whisper_ed25519"
    if (-not $rw_key -and (Test-Path $autoKey)) { $rw_key = $autoKey }
    $sshOpts = "-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $rw_ssh_port"
    if ($rw_key) { $sshOpts += " -i $rw_key" }

    $needRepair = 0
    $repairItems = ""

    info "正在檢查伺服器環境..."

    # 1. SSH 連線
    if (ssh_test $sshOpts $userHost "echo ok") {
        check_ok "SSH 連線正常"

        # 2. Python3 + ffmpeg
        if (ssh_test $sshOpts $userHost "command -v python3") {
            if (ssh_test $sshOpts $userHost "command -v ffmpeg") {
                check_ok "Python3 + ffmpeg 就緒"
            } else {
                check_missing "ffmpeg 未安裝"
                $needRepair = 1; $repairItems += " ffmpeg"
            }
        } else {
            check_missing "Python3 未安裝"
            $needRepair = 1; $repairItems += " python3"
        }

        # 3. venv
        if (ssh_test $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 --version") {
            check_ok "venv 正常"
        } else {
            check_missing "venv 損壞或不存在"
            $needRepair = 1; $repairItems += " venv"
        }

        # 4. server.py
        if (ssh_test $sshOpts $userHost "test -f ~/jt-whisper-server/server.py") {
            check_ok "server.py 存在"
        } else {
            check_missing "server.py 不存在"
            $needRepair = 1; $repairItems += " server.py"
        }

        # 5. faster-whisper
        if (ssh_test $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 -c 'import faster_whisper'") {
            check_ok "faster-whisper 套件就緒"
        } else {
            check_missing "faster-whisper 套件缺失"
            $needRepair = 1; $repairItems += " packages"
        }

        # 5b. resemblyzer + spectralcluster
        if (ssh_test $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 -c 'import resemblyzer; import spectralcluster'") {
            check_ok "resemblyzer + spectralcluster 就緒（講者辨識）"
        } else {
            check_missing "resemblyzer + spectralcluster 套件缺失（講者辨識）"
            $needRepair = 1; $repairItems += " packages"
        }

        # 6. NVIDIA GPU + CUDA
        $gpuInfo = ssh_cmd $sshOpts $userHost "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1"
        if ($gpuInfo) {
            check_ok "NVIDIA GPU: ${gpuInfo}"
            $cudaCheck = ssh_cmd $sshOpts $userHost "LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c `"import torch; pt=torch.cuda.is_available(); ct2=False; ow=False
try:
    import ctranslate2; ct2=bool(ctranslate2.get_supported_compute_types('cuda'))
except: pass
try:
    import whisper; ow=True
except: pass
print(f'{pt},{ct2},{ow}')`""
            if ($cudaCheck) {
                $parts = $cudaCheck.Split(',')
                $ptOk  = $parts[0]
                $ct2Ok = $parts[1]
                $owOk  = $parts[2]
                if ($ptOk -eq "True" -and $ct2Ok -eq "True") {
                    $ct2Src = ""
                    if (ssh_test $sshOpts $userHost "ls ~/jt-whisper-server/.ct2-wheels/ctranslate2-*.whl 2>/dev/null") {
                        $ct2Src = "原始碼編譯"
                    }
                    if ($ct2Src) {
                        check_ok "CUDA 可用（faster-whisper + CTranslate2 ${ct2Src}）"
                    } else {
                        check_ok "CUDA 可用（faster-whisper + CTranslate2）"
                    }
                } elseif ($ptOk -eq "True" -and $owOk -eq "True") {
                    $chkArch = ssh_cmd $sshOpts $userHost "uname -m"
                    if ($chkArch -eq "aarch64") {
                        check_notice "aarch64 + openai-whisper（較慢），稍後嘗試編譯 CTranslate2"
                        $needRepair = 2  # 可升級
                    } else {
                        check_ok "CUDA 可用（openai-whisper + PyTorch）"
                    }
                } else {
                    if ($ptOk -ne "True") {
                        check_warn "有 GPU 但 PyTorch CUDA 不可用 — 需修復"
                    } else {
                        check_warn "PyTorch CUDA 正常但無可用 CUDA 辨識引擎 — 需修復"
                    }
                    $needRepair = 1; $repairItems += " cuda"
                }
            }
        } else {
            info "未偵測到 NVIDIA GPU（將以 CPU 辨識）"
        }

        # 7. 伺服器磁碟空間
        $remoteAvailMb = ssh_cmd $sshOpts $userHost "df -m ~ | awk 'NR==2{print `$4}'"
        if ($remoteAvailMb) {
            try {
                $remoteAvailGb = [math]::Round([int]$remoteAvailMb / 1024, 1)
                if ([int]$remoteAvailMb -lt 5000) {
                    check_warn "伺服器磁碟空間偏低（${remoteAvailGb} GB 可用）"
                } else {
                    check_ok "伺服器磁碟空間 ${remoteAvailGb} GB 可用"
                }
            } catch { }
        }
    } else {
        check_fail "SSH 連線失敗"
        $needRepair = 1; $repairItems = "ssh"
    }

    # aarch64 CTranslate2 原始碼編譯
    if ($needRepair -eq 2) {
        if (build_ctranslate2_from_source $sshOpts $userHost) {
            check_ok "CUDA 已升級（faster-whisper + CTranslate2 原始碼編譯）"
        } else {
            check_warn "CTranslate2 原始碼編譯失敗，faster-whisper 無法使用 CUDA GPU"
            check_ok "CUDA 可用（降級使用 openai-whisper + PyTorch，速度較慢約 ~2x realtime）"
        }
        $needRepair = 0
    }

    if ($needRepair -eq 0) {
        # SSH 公鑰
        if ($rw_key -and (Test-Path "${rw_key}.pub")) {
            $pubKey = Get-Content "${rw_key}.pub" -Raw
            $pubKey = $pubKey.Trim()
            if (-not (ssh_test $sshOpts $userHost "grep -qF '$pubKey' ~/.ssh/authorized_keys 2>/dev/null")) {
                $pubKeyContent = Get-Content "${rw_key}.pub" -Raw
                $pubKeyContent | & ssh $sshOpts.Split(' ') $userHost "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    check_ok "SSH 公鑰已加入伺服器，日後免密碼"
                }
            }
        }

        # 預下載辨識模型
        download_remote_models $sshOpts $userHost

        # 同步 server.py（MD5 比對）
        if (Test-Path $SERVER_PY) {
            $localHash = (Get-FileHash $SERVER_PY -Algorithm MD5).Hash.ToLower()
            $remoteHash = ssh_cmd $sshOpts $userHost "md5sum ~/jt-whisper-server/server.py 2>/dev/null | cut -d' ' -f1"
            if ($localHash -ne $remoteHash) {
                $scpOpts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -P $rw_ssh_port"
                if ($rw_key) { $scpOpts += " -i $rw_key" }
                if (scp_file $scpOpts $SERVER_PY "${userHost}:~/jt-whisper-server/server.py") {
                    ssh_cmd $sshOpts $userHost "pkill -f 'server.py --port' 2>/dev/null" | Out-Null
                    check_ok "server.py 已同步更新（已重啟伺服器）"
                }
            }
        }

        check_ok "GPU 伺服器 辨識環境正常（${userHost}）"
    } else {
        # 需要修復
        Write-Host ""
        check_detect "偵測到問題:${repairItems}"
        $doRepair = Read-Host "  是否修復伺服器環境？(Y/n)"
        if ($doRepair -eq 'n' -or $doRepair -eq 'N') {
            info "跳過修復"
        } else {
            # 用既有設定進入安裝流程（跳到下方安裝區塊）
            $doInstall = $true
        }
    }
} else {
    # ─── 無設定：問是否新設 ──────────────────────────────────
    Write-Host "  ${C_WHITE}若有 Linux + NVIDIA GPU 伺服器，可部署伺服器 Whisper 辨識服務，大幅加快語音辨識速度${NC}"
    info "離線處理音訊檔（--input）時速度快 5-10 倍"
    info "支援系統：DGX OS / Ubuntu（需有 NVIDIA 驅動與 CUDA）"
    info "不設定則使用本機 CPU 辨識"
    Write-Host ""
    $setupRemote = Read-Host "  是否設定 GPU 伺服器辨識？(y/N)"

    if ($setupRemote -eq 'y' -or $setupRemote -eq 'Y') {
        $doInstall = $true

        # 收集 SSH 連線資訊
        Write-Host ""
        $rw_host = Read-Host "  SSH 伺服器 IP"
        if (-not $rw_host) { info "未輸入，跳過"; $doInstall = $false }

        if ($doInstall) {
            $rw_ssh_port = Read-Host "  SSH Port [22]"
            if (-not $rw_ssh_port) { $rw_ssh_port = "22" }

            $rw_user = Read-Host "  SSH 使用者"
            if (-not $rw_user) { info "未輸入使用者，跳過"; $doInstall = $false }
        }

        if ($doInstall) {
            # 自動找 SSH key
            $rw_key = ""
            $ed25519 = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
            $rsa_key = Join-Path $env:USERPROFILE ".ssh\id_rsa"
            if (Test-Path $ed25519) { $rw_key = $ed25519 }
            elseif (Test-Path $rsa_key) { $rw_key = $rsa_key }
            $defaultKeyPrompt = if ($rw_key) { $rw_key } else { "留空用密碼" }
            $rw_key_input = Read-Host "  SSH Key 路徑 [${defaultKeyPrompt}]"
            if ($rw_key_input) { $rw_key = $rw_key_input }

            $rw_port = Read-Host "  Whisper 服務 Port [8978]"
            if (-not $rw_port) { $rw_port = "8978" }
        }
    } else {
        info "跳過伺服器設定"
        $doInstall = $false
    }
}

# ─── 伺服器安裝流程（新設或修復共用）──────────────────────────
if ($doInstall) {
    $sshOpts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $rw_ssh_port"
    if ($rw_key) { $sshOpts += " -i $rw_key" }
    $userHost = "${rw_user}@${rw_host}"

    # 自動確保 SSH 金鑰驗證（避免重複輸入密碼）
    Write-Host ""
    ensure_ssh_key_auth $userHost $rw_ssh_port

    # 重建 sshOpts（加入 BatchMode + key，不依賴函式 scope 更新）
    $autoKey = Join-Path $env:USERPROFILE ".ssh\jt_whisper_ed25519"
    if (-not $rw_key -and (Test-Path $autoKey)) { $rw_key = $autoKey }
    $sshOpts = "-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p $rw_ssh_port"
    if ($rw_key) { $sshOpts += " -i $rw_key" }

    # 測試 SSH 連線
    info "測試 SSH 連線..."
    if (-not (ssh_test $sshOpts $userHost "echo ok")) {
        check_fail "SSH 連線失敗（${userHost}:${rw_ssh_port}）"
        info "請確認 SSH 設定後重新執行 install.ps1"
    } else {
        check_ok "SSH 連線成功"

        # 檢查伺服器 Python3 + ffmpeg + 編譯工具（單次 SSH 批次檢查）
        info "檢查伺服器套件..."
        $checkScript = @"
missing=""
command -v python3 >/dev/null 2>&1 || missing="`$missing python3 python3-venv python3-pip"
command -v ffmpeg >/dev/null 2>&1 || missing="`$missing ffmpeg"
for pkg in build-essential python3-dev pkg-config libffi-dev libsndfile1-dev cmake git; do
  dpkg -s "`$pkg" >/dev/null 2>&1 || missing="`$missing `$pkg"
done
echo "`$missing"
"@
        $needApt = "$(ssh_cmd $sshOpts $userHost $checkScript)".Trim()
        if ($needApt) {
            info "伺服器缺少:${needApt}，正在安裝..."
            ssh_cmd $sshOpts $userHost "apt update -qq && apt install -y -qq $needApt 2>&1" | Out-Null
            if ($LASTEXITCODE -ne 0) {
                check_fail "無法在伺服器安裝系統套件"
            }
        }
        check_ok "Python3 + ffmpeg + 編譯工具就緒"

        # 伺服器磁碟空間
        $remoteAvailMb = ssh_cmd $sshOpts $userHost "df -m ~ | awk 'NR==2{print `$4}'"
        if ($remoteAvailMb) {
            try {
                $remoteAvailGb = [math]::Round([int]$remoteAvailMb / 1024, 1)
                if ([int]$remoteAvailMb -lt 5000) {
                    check_fail "伺服器磁碟空間不足：可用 ${remoteAvailGb} GB，最小需要 5 GB"
                    info "GPU 伺服器需要安裝 PyTorch (~2.5GB) + Whisper 模型 (~6GB)"
                } elseif ([int]$remoteAvailMb -lt 12000) {
                    check_notice "伺服器可用空間 ${remoteAvailGb} GB（完整安裝需 12 GB）"
                } else {
                    check_ok "伺服器磁碟空間充足（${remoteAvailGb} GB 可用）"
                }
            } catch { }
        }

        # 伺服器 NVIDIA GPU + CUDA
        $remoteGpuName = ssh_cmd $sshOpts $userHost "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1"
        $torchIndex = ""
        if ($remoteGpuName) {
            check_ok "NVIDIA GPU: ${remoteGpuName}"
            $cudaVersion = ssh_cmd $sshOpts $userHost "nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+'"
            if ($cudaVersion) {
                $cudaMajor = [int]($cudaVersion.Split('.')[0])
                $cudaMinor = [int]($cudaVersion.Split('.')[1])
                check_ok "CUDA: ${cudaVersion}"
                if ($cudaMajor -ge 13 -or ($cudaMajor -eq 12 -and $cudaMinor -ge 8)) {
                    $torchIndex = "https://download.pytorch.org/whl/cu128"
                } elseif ($cudaMajor -eq 12) {
                    $torchIndex = "https://download.pytorch.org/whl/cu124"
                } elseif ($cudaMajor -eq 11) {
                    $torchIndex = "https://download.pytorch.org/whl/cu118"
                }
            } else {
                check_notice "未偵測到 CUDA，PyTorch 將安裝 CPU 版"
            }
        } else {
            check_notice "未偵測到 NVIDIA GPU，PyTorch 將安裝 CPU 版（辨識速度較慢）"
        }

        # 建立 venv
        ssh_cmd $sshOpts $userHost "mkdir -p ~/jt-whisper-server && if [ ! -d ~/jt-whisper-server/venv ]; then python3 -m venv ~/jt-whisper-server/venv; fi" | Out-Null

        # PyTorch（檢查是否已正常，避免重複安裝 2-3 GB）
        $skipTorch = $false
        if ($torchIndex) {
            $ptCheck = ssh_cmd $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 -c 'import torch; print(torch.cuda.is_available())'"
            if ($ptCheck -eq "True") {
                check_ok "PyTorch CUDA 已正常，跳過重裝"
                $skipTorch = $true
            }
        }

        if (-not $skipTorch) {
            $torchExtra = ""
            $torchMsg = "安裝 PyTorch..."
            if ($torchIndex) {
                $torchExtra = "--force-reinstall --index-url $torchIndex"
                $torchMsg = "安裝 PyTorch GPU 版（約 2-3 GB）..."
            }
            info $torchMsg
            ssh_cmd $sshOpts $userHost "~/jt-whisper-server/venv/bin/pip install --disable-pip-version-check torch $torchExtra 2>&1" | Out-Null
            if ($LASTEXITCODE -ne 0) {
                check_fail "PyTorch 安裝失敗"
            } else {
                check_ok "PyTorch 安裝完成"
            }
        }

        # 安裝伺服器 Python 套件（檢查是否已安裝）
        $pkgCheck = ssh_cmd $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 -c 'import faster_whisper, fastapi, resemblyzer, spectralcluster; print(1)' 2>/dev/null"
        if ($pkgCheck -eq "1") {
            check_ok "伺服器 Python 套件已安裝"
        } else {
            info "安裝伺服器 Python 套件..."
            $ct2CachedWhl = ssh_cmd $sshOpts $userHost "ls ~/jt-whisper-server/.ct2-wheels/ctranslate2-*.whl 2>/dev/null | head -1"
            if ($ct2CachedWhl) {
                # 有原始碼編譯 wheel
                ssh_cmd $sshOpts $userHost "PIP=~/jt-whisper-server/venv/bin/pip && `$PIP install --disable-pip-version-check 'setuptools<81' wheel 2>&1 && `$PIP install --disable-pip-version-check --force-reinstall --no-deps '$ct2CachedWhl' 2>&1 && `$PIP install --disable-pip-version-check 'setuptools<81' faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 2>&1" | Out-Null
            } else {
                $fwExtra = ""
                if ($torchIndex) { $fwExtra = "--force-reinstall" }
                ssh_cmd $sshOpts $userHost "PIP=~/jt-whisper-server/venv/bin/pip && `$PIP install --disable-pip-version-check 'setuptools<81' wheel 2>&1 && `$PIP install --disable-pip-version-check $fwExtra 'setuptools<81' ctranslate2 faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 2>&1" | Out-Null
            }
            if ($LASTEXITCODE -ne 0) {
                check_fail "伺服器套件安裝失敗"
            } else {
                check_ok "伺服器 Python 套件安裝完成"
            }
        }

        # 驗證 CUDA
        if ($torchIndex) {
            $cudaCheck = ssh_cmd $sshOpts $userHost "LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c `"import torch; pt=torch.cuda.is_available()
try:
    import ctranslate2; ct2=bool(ctranslate2.get_supported_compute_types('cuda'))
except: ct2=False
print(f'{pt},{ct2}')`""
            if ($cudaCheck) {
                $parts = $cudaCheck.Split(',')
                $ptOk  = $parts[0]
                $ct2Ok = $parts[1]
                if ($ptOk -eq "True" -and $ct2Ok -eq "True") {
                    check_ok "CUDA 驗證通過（faster-whisper + CTranslate2 CUDA）"
                } elseif ($ptOk -eq "True") {
                    $remoteArch = ssh_cmd $sshOpts $userHost "uname -m"
                    $ct2Built = $false
                    if ($remoteArch -eq "aarch64") {
                        $ct2Built = build_ctranslate2_from_source $sshOpts $userHost
                        if ($ct2Built) {
                            check_ok "CUDA 驗證通過（faster-whisper + CTranslate2 原始碼編譯）"
                        }
                    }
                    if (-not $ct2Built) {
                        info "CTranslate2 無 CUDA，改裝 openai-whisper（PyTorch CUDA）..."
                        ssh_cmd $sshOpts $userHost "~/jt-whisper-server/venv/bin/pip install --disable-pip-version-check 'setuptools<81' openai-whisper 2>&1" | Out-Null
                        $owCheck = ssh_cmd $sshOpts $userHost "~/jt-whisper-server/venv/bin/python3 -c `"import whisper; print('ok')`""
                        if ($owCheck -eq "ok") {
                            check_ok "CUDA 驗證通過（openai-whisper + PyTorch CUDA）"
                        } else {
                            check_warn "openai-whisper 安裝失敗，Whisper 將以 CPU 執行"
                        }
                    }
                } else {
                    check_warn "PyTorch CUDA 無法使用，Whisper 將以 CPU 執行"
                }
            }
        }

        # SCP 部署 server.py（比對 hash，相同則跳過）
        if (Test-Path $SERVER_PY) {
            $localHash = (Get-FileHash $SERVER_PY -Algorithm MD5).Hash
            $remoteHash = ssh_cmd $sshOpts $userHost "md5sum ~/jt-whisper-server/server.py 2>/dev/null | cut -d' ' -f1"
            if ($remoteHash -and $localHash.ToLower() -eq $remoteHash.ToLower()) {
                check_ok "server.py 已是最新版"
            } else {
                $scpOpts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -P $rw_ssh_port"
                if ($rw_key -and (Test-Path $rw_key)) { $scpOpts += " -i $rw_key" }
                if (-not (scp_file $scpOpts $SERVER_PY "${userHost}:~/jt-whisper-server/server.py")) {
                    check_fail "SCP 部署失敗"
                } else {
                    check_ok "server.py 已部署"
                }
            }
        }

        # 測試啟動
        ssh_cmd $sshOpts $userHost "cd ~/jt-whisper-server && export LD_LIBRARY_PATH=/usr/local/lib:`$LD_LIBRARY_PATH && nohup venv/bin/python3 server.py --port $rw_port > /tmp/jt-whisper-server.log 2>&1 &" | Out-Null

        # Health check（最多 15 秒）
        info "測試啟動伺服器..."
        $healthOk = $false
        for ($i = 1; $i -le 15; $i++) {
            try {
                $oldProg = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
                $resp = Invoke-WebRequest -Uri "http://${rw_host}:${rw_port}/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction SilentlyContinue 2>$null
                $ProgressPreference = $oldProg
                if ($resp.Content -match '"ok"') { $healthOk = $true; break }
            } catch { $ProgressPreference = $oldProg }
            Start-Sleep -Seconds 1
        }

        # 停止測試 server
        ssh_cmd $sshOpts $userHost "pkill -f 'server.py --port $rw_port'" | Out-Null

        if ($healthOk) {
            check_ok "伺服器測試成功"
        } else {
            check_fail "伺服器無法啟動，請檢查防火牆或 GPU 驅動"
            info "可查看伺服器 log: ssh ${userHost} cat /tmp/jt-whisper-server.log"
        }

        # 預下載辨識模型
        download_remote_models $sshOpts $userHost

        # 寫入 config.json
        $cfgToSave = read_config
        $remoteObj = [PSCustomObject]@{
            host         = $rw_host
            ssh_port     = [int]$rw_ssh_port
            ssh_user     = $rw_user
            ssh_key      = $rw_key
            whisper_port = [int]$rw_port
        }
        $cfgToSave | Add-Member -NotePropertyName "remote_whisper" -NotePropertyValue $remoteObj -Force
        save_config $cfgToSave
        check_ok "設定已儲存至 config.json"
    }
}

}  # end of SSH available check

# ═══════════════════════════════════════════════════════════════
# 8. 驗證安裝
# ═══════════════════════════════════════════════════════════════

section "驗證安裝結果"

$verifyFailed = 0

# Python venv
if (Test-Path $VENV_PYTHON) { check_ok "Python 虛擬環境" }
else { check_fail "Python 虛擬環境"; $verifyFailed++ }

# 核心套件
$verifyModules = @(
    @("numpy",             "numpy（數值計算）"),
    @("ctranslate2",       "ctranslate2（語音辨識加速）"),
    @("sentencepiece",     "sentencepiece（分詞工具）"),
    @("faster_whisper",    "faster-whisper（離線辨識）"),
    @("resemblyzer",       "resemblyzer（講者辨識）"),
    @("spectralcluster",   "spectralcluster（講者分群）"),
    @("sounddevice",       "sounddevice（音訊擷取）"),
    @("argostranslate",    "Argos Translate（離線翻譯）")
)

foreach ($item in $verifyModules) {
    if (venv_import_ok $item[0]) {
        check_ok $item[1]
    } else {
        check_fail $item[1]
        $verifyFailed++
    }
}

# PyTorch + CUDA
if ($GPU_AVAILABLE) {
    $cudaCheck = & $VENV_PYTHON -c "import torch; print(torch.cuda.is_available())" 2>$null
    if ($cudaCheck -eq "True") {
        $gpuDev = & $VENV_PYTHON -c "import torch; print(torch.cuda.get_device_name(0))" 2>$null
        check_ok "PyTorch CUDA: ${gpuDev}"
    } else {
        check_notice "PyTorch 已安裝但 CUDA 不可用（將使用 CPU）"
        info "可能原因: CUDA Toolkit 版本不符或 cuDNN 缺失"
    }
} else {
    if (venv_import_ok "torch") {
        check_ok "PyTorch (CPU)"
    } else {
        check_fail "PyTorch"
        $verifyFailed++
    }
}

# Moonshine
if (venv_import_ok "moonshine_voice") {
    check_ok "Moonshine（英文低延遲 ASR）"
} else {
    info "Moonshine 未安裝（選裝，不影響主要功能）"
}

# whisper.cpp
if ($WHISPER_STREAM_EXE -and (Test-Path $WHISPER_STREAM_EXE)) {
    check_ok "whisper.cpp（本機即時辨識）"
} else {
    info "whisper.cpp 未安裝（離線模式、Moonshine、GPU 伺服器 不受影響）"
}

# ffmpeg
if (cmd_exists "ffmpeg") {
    check_ok "ffmpeg（音訊轉檔）"
} else {
    check_missing "ffmpeg 未安裝（處理非 WAV 音訊時需要）"
}

# ═══════════════════════════════════════════════════════════════
# 結果摘要
# ═══════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "${C_TITLE}${banner_line}${NC}"
if ($verifyFailed -eq 0) {
    Write-Host "${C_OK}${BOLD}  安裝完成！${NC}"
} else {
    Write-Host "${C_WARN}${BOLD}  安裝完成（${verifyFailed} 個元件未安裝，詳見上方提示）${NC}"
}
Write-Host "${C_TITLE}${banner_line}${NC}"
Write-Host ""

# 功能對照表
Write-Host "${C_WHITE}  可用功能：${NC}"

# 檢查 faster-whisper 模型是否已下載
$fwModelOk = (& $VENV_PYTHON -c @"
import os
found = False
dirs = []
try:
    from huggingface_hub.constants import HF_HUB_CACHE
    dirs.append(HF_HUB_CACHE)
except: pass
default = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
if default not in dirs:
    dirs.append(default)
for d in dirs:
    for prefix in ['Systran', 'mobiuslabsgmbh']:
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-large-v3-turbo')):
            found = True
            break
    if found:
        break
print('found' if found else '')
"@ 2>$null) -eq "found"

$features = @(
    @{ OK = (venv_import_ok "faster_whisper"); Desc = "離線音訊處理 (--input)"; Engine = "faster-whisper" },
    @{ OK = $fwModelOk;                        Desc = "Whisper 模型 large-v3-turbo"; Engine = "faster-whisper 格式" },
    @{ OK = (venv_import_ok "resemblyzer");    Desc = "AI 講者辨識 (--diarize)"; Engine = "resemblyzer" },
    @{ OK = (venv_import_ok "argostranslate"); Desc = "Argos 離線翻譯";          Engine = "僅英翻中" },
    @{ OK = (Test-Path (Join-Path $env:LOCALAPPDATA "jt-live-whisper\models\nllb-600m\model.bin")); Desc = "NLLB 離線翻譯"; Engine = "中日英互譯" },
    @{ OK = (venv_import_ok "moonshine_voice");      Desc = "Moonshine 即時辨識";       Engine = "英文低延遲" }
)

foreach ($feat in $features) {
    $icon = if ($feat.OK) { "${C_OK}■${NC}" } else { "${C_DIM}□${NC}" }
    Write-Host "  ${icon} $($feat.Desc)  ${C_DIM}$($feat.Engine)${NC}"
}

$wsIcon = if ($WHISPER_STREAM_EXE) { "${C_OK}■${NC}" } else { "${C_DIM}□${NC}" }
Write-Host "  ${wsIcon} Whisper 本機即時辨識  ${C_DIM}whisper.cpp${NC}"

# GPU 伺服器
$rwCfgFinal = read_config
$hasRemote = (($rwCfgFinal | Get-Member -Name "remote_whisper") -and $rwCfgFinal.remote_whisper.host)
$rwIcon = if ($hasRemote) { "${C_OK}■${NC}" } else { "${C_DIM}□${NC}" }
$rwDesc = if ($hasRemote) { "GPU 伺服器 ($($rwCfgFinal.remote_whisper.host))" } else { "GPU 伺服器 辨識" }
Write-Host "  ${rwIcon} ${rwDesc}  ${C_DIM}remote whisper server${NC}"

Write-Host ""

if ($GPU_AVAILABLE) {
    Write-Host "  ${C_OK}GPU 模式${NC}: ${GPU_NAME}"
    Write-Host "  ${C_DIM}faster-whisper / resemblyzer / PyTorch 皆使用 CUDA 加速${NC}"
} else {
    Write-Host "  ${C_WHITE}CPU 模式${NC}"
    Write-Host "  ${C_DIM}建議搭配區域網路 LLM 伺服器使用（--llm-host）${NC}"
}

Write-Host ""
Write-Host "  ${C_WHITE}啟動方式: ${C_OK}.\start.ps1${NC}"
Write-Host "  ${C_WHITE}升級方式: ${C_OK}.\install.ps1 -Upgrade${NC}"
Write-Host ""
Write-Host "  ${C_DIM}提示：若日後將此資料夾搬移到其他位置，請重新執行 .\install.ps1${NC}"
Write-Host "  ${C_DIM}      安裝程式會自動偵測並修復因路徑變更而損壞的環境${NC}"
Write-Host ""
Write-Host "  ${C_DIM}安裝 log: $INSTALL_LOG${NC}"
Write-Host ""
try { Stop-Transcript | Out-Null } catch {}
