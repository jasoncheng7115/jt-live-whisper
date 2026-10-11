#!/bin/bash
# 即時英翻中字幕系統 - 安裝腳本
# 檢查並安裝所有必要的依賴項目
# 支援一鍵安裝：curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh | bash
# Author: Jason Cheng (Jason Tools)

set -e

GITHUB_REPO="https://github.com/jasoncheng7115/jt-live-whisper.git"
GITHUB_RAW="https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main"

# ─── Bootstrap：透過 curl | bash 執行時，自動下載並安裝 ───
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
_is_upgrade=false
for _arg in "$@"; do [ "$_arg" = "--upgrade" ] && _is_upgrade=true; done
# 當函式庫載入（JTLW_INSTALL_LIB=1）時絕不走這段：`bash -c` 的 $0 是 bash 本身（/bin/bash → SCRIPT_DIR=/bin），
# 會被當成 curl | bash，cd 到 ~/Apps/jt-live-whisper 並 exec 正式安裝的 install.sh（2026-10-09 在 Mac 測試時發生，
# 正式那份因 set -e 在載入點的 return 出錯而結束，沒有動到東西）
if [ ! -f "$SCRIPT_DIR/translate_meeting.py" ] && [ "$_is_upgrade" = false ] && [ -z "${JTLW_INSTALL_LIB:-}" ]; then
    echo ""
    echo -e "\033[38;2;100;180;255m============================================================\033[0m"
    echo -e "\033[38;2;100;180;255m\033[1m  jt-live-whisper - 一鍵安裝\033[0m"
    echo -e "\033[38;2;100;180;255m============================================================\033[0m"
    echo ""

    INSTALL_DIR="$HOME/Apps/jt-live-whisper"
    if [ -f "$INSTALL_DIR/translate_meeting.py" ]; then
        echo -e "\033[38;2;255;255;255m目錄已存在: $INSTALL_DIR\033[0m"
        echo -e "\033[38;2;255;255;255m進入目錄執行安裝...\033[0m"
        cd "$INSTALL_DIR"
    else
        echo -e "\033[38;2;255;255;255m正在從 GitHub 下載 jt-live-whisper...\033[0m"
        tmp_zip="/tmp/jt-live-whisper-$$.zip"
        tmp_extract="/tmp/jt-extract-$$"
        curl -fsSL "https://github.com/jasoncheng7115/jt-live-whisper/archive/refs/heads/main.zip" -o "$tmp_zip"
        if [ ! -f "$tmp_zip" ]; then
            echo -e "\033[38;2;255;80;80m[錯誤] 下載失敗，請檢查網路連線\033[0m"
            exit 1
        fi
        if command -v unzip >/dev/null 2>&1; then
            unzip -q "$tmp_zip" -d "$tmp_extract"
        else
            # 部分 Linux 發行版預設沒有 unzip
            python3 -m zipfile -e "$tmp_zip" "$tmp_extract"
        fi
        mkdir -p "$INSTALL_DIR"
        cp -R "$tmp_extract"/jt-live-whisper-main/* "$INSTALL_DIR/"
        rm -rf "$tmp_zip" "$tmp_extract"
        cd "$INSTALL_DIR"
    fi

    chmod +x install.sh start.sh
    chmod +x install-linux.sh 2>/dev/null || true
    exec ./install.sh "$@"
fi
# ─── Bootstrap 結束 ──────────────────────────────────────

# ─── Linux：改由 install-linux.sh 安裝（本檔以下為 macOS 流程）───
# install-linux.sh 會以 JTLW_INSTALL_LIB=1 載入本檔，沿用平台無關的函式
if [ "$(uname -s)" = "Linux" ] && [ -z "${JTLW_INSTALL_LIB:-}" ]; then
    if [ ! -f "$SCRIPT_DIR/install-linux.sh" ]; then
        # 由舊版升級上來時可能還沒有這支腳本
        echo "正在下載 install-linux.sh ..."
        curl -fsSL "$GITHUB_RAW/install-linux.sh" -o "$SCRIPT_DIR/install-linux.sh" || {
            echo "[錯誤] 無法下載 install-linux.sh，請檢查網路連線"; exit 1; }
    fi
    exec bash "$SCRIPT_DIR/install-linux.sh" "$@"
fi

VENV_DIR="$SCRIPT_DIR/venv"
WHISPER_DIR="$SCRIPT_DIR/whisper.cpp"
MODELS_DIR="$WHISPER_DIR/models"
ARGOS_PKG_DIR="$HOME/.local/share/argos-translate/packages/translate-en_zh-1_9"
NLLB_MODEL_DIR="$HOME/.local/share/jt-live-whisper/models/nllb-600m"

# ─── 安裝 Log ─────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/logs" 2>/dev/null
INSTALL_LOG="$SCRIPT_DIR/logs/install_$(date +%Y%m%d_%H%M%S).log"
# 所有終端機輸出同時寫入 log 檔（tee 複製）
exec > >(tee -a "$INSTALL_LOG") 2>&1

# 偵測 ARM Homebrew Python（Moonshine 需要 ARM64 原生 Python）
if [ -x "/opt/homebrew/bin/python3.12" ]; then
    PYTHON_CMD="/opt/homebrew/bin/python3.12"
elif command -v python3.12 &>/dev/null && python3.12 --version &>/dev/null 2>&1; then
    PYTHON_CMD="python3.12"
else
    PYTHON_CMD="python3"
fi

# 24-bit 真彩色
C_TITLE='\033[38;2;100;180;255m'
C_OK='\033[38;2;80;255;120m'
C_WARN='\033[38;2;255;220;80m'
C_ERR='\033[38;2;255;100;100m'
C_DIM='\033[38;2;100;100;100m'
C_WHITE='\033[38;2;255;255;255m'
BOLD='\033[1m'
NC='\033[0m'

passed=0
failed=0
installed=0

# Spinner 動畫：在背景執行指令，前景顯示動畫
# 用法: run_spinner "顯示文字" command arg1 arg2 ...
# 指令的 stdout/stderr 會存到 $SPINNER_OUTPUT
SPINNER_OUTPUT="/tmp/jt-install-spinner-$$.log"
run_spinner() {
    local msg="$1"
    shift
    local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
    printf "  ${C_DIM}%s ${NC}" "$msg"
    "$@" > "$SPINNER_OUTPUT" 2>&1 &
    local pid=$!
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "${C_DIM}%s${NC}" "${frames[$((i % 10))]}"
        sleep 0.12
        printf "\b"
        ((i++)) || true
    done
    wait "$pid"
    local rc=$?
    printf " \b"
    # 將指令的詳細輸出寫入安裝 log（畫面上被 spinner 隱藏的部分）
    if [ -n "$INSTALL_LOG" ] && [ -f "$SPINNER_OUTPUT" ] && [ -s "$SPINNER_OUTPUT" ]; then
        echo "--- [run_spinner] $msg (rc=$rc) ---" >> "$INSTALL_LOG"
        cat "$SPINNER_OUTPUT" >> "$INSTALL_LOG"
        echo "--- [/run_spinner] ---" >> "$INSTALL_LOG"
    fi
    return $rc
}

# HuggingFace 模型下載（SSL 失敗時自動停用憑證驗證重試）
# 用法: hf_download "repo_id" "描述" ["local_dir"]
hf_download() {
    local repo="$1" desc="$2" local_dir="$3"
    local local_dir_arg=""
    [ -n "$local_dir" ] && local_dir_arg=", local_dir='$local_dir'"

    local result
    result=$(python3 -c "
import os
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
from huggingface_hub import snapshot_download
try:
    snapshot_download('$repo'$local_dir_arg)
    print('OK')
except Exception as e:
    err = str(e)
    if 'SSL' in err or 'CERTIFICATE' in err.upper() or 'ssl' in err:
        print('SSL_RETRY')
    else:
        print('FAIL:' + err[:300])
" 2>&1)

    if echo "$result" | grep -q "^SSL_RETRY"; then
        check_notice "SSL 憑證驗證失敗（常見於企業網路），嘗試停用驗證重新下載..."
        result=$(python3 -c "
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
    snapshot_download('$repo'$local_dir_arg)
    print('OK')
except Exception as e:
    print('FAIL:' + str(e)[:300])
" 2>&1)
    fi

    if echo "$result" | grep -q "^OK"; then
        return 0
    fi
    local err_msg
    err_msg=$(echo "$result" | sed -n 's/^FAIL://p')
    check_notice "${desc}下載失敗，可稍後在有網路時重新執行安裝"
    [ -n "$err_msg" ] && echo -e "  ${C_DIM}錯誤詳情: $err_msg${NC}"
    return 1
}

# 背景 Spinner：檢查階段用，不吞輸出
# 用法: spinner_start "訊息" → （執行檢查，輸出存到暫存檔）→ spinner_stop → cat 暫存檔
_SPINNER_PID=""
_CHECK_BUF="/tmp/jt-install-check-$$.log"
spinner_start() {
    local msg="$1"
    (
        trap 'exit 0' TERM
        local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
        local i=0
        while true; do
            printf "\r  ${C_DIM}%s %s${NC} " "$msg" "${frames[$((i % 10))]}"
            sleep 0.12
            ((i++)) || true
        done
    ) &
    _SPINNER_PID=$!
}
spinner_stop() {
    if [ -n "$_SPINNER_PID" ]; then
        kill "$_SPINNER_PID" 2>/dev/null
        wait "$_SPINNER_PID" 2>/dev/null
        _SPINNER_PID=""
        printf "\r\033[K"
    fi
}

print_title() {
    echo ""
    echo -e "${C_TITLE}============================================================${NC}"
    echo -e "${C_TITLE}${BOLD}  jt-live-whisper v2.29.0 - 100% 全地端 AI 語音工具箱 - 安裝程式${NC}"
    echo -e "${C_TITLE}  by Jason Cheng (Jason Tools)${NC}"
    echo -e "${C_TITLE}============================================================${NC}"
    echo ""
}

check_ok() {
    echo -e "  ${C_OK}[完成]${NC} $1"
    ((passed++)) || true
}

check_install() {
    echo -e "  ${C_WARN}[安裝]${NC} $1"
    ((installed++)) || true
}

check_fail() {
    echo -e "  ${C_ERR}[失敗]${NC} $1"
    ((failed++)) || true
}

check_notice() {
    echo -e "  ${C_WARN}[注意]${NC} $1"
}

section() {
    echo ""
    echo -e "${C_TITLE}${BOLD}▎ $1${NC}"
    echo -e "${C_DIM}$( printf '─%.0s' {1..50} )${NC}"
}

# ─── 環境前置檢查 ────────────────────────────────
check_macos_version() {
    section "macOS 版本"
    local ver
    ver=$(sw_vers -productVersion 2>/dev/null)
    if [ -z "$ver" ]; then
        check_fail "無法偵測 macOS 版本"
        return 1
    fi
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -lt 13 ]; then
        check_fail "macOS $ver 不支援（最低需要 macOS 13 Ventura，whisper.cpp Metal 加速需要）"
        return 1
    fi
    check_ok "macOS $ver"
}

check_xcode_clt() {
    section "Xcode Command Line Tools"
    if xcode-select -p &>/dev/null; then
        check_ok "Xcode CLT 已安裝（$(xcode-select -p)）"
        # macOS 大版本升級後常見：SDK 換新了，但編譯器還是舊的 → 編譯與連結都會失敗
        # （實測 macOS 26：MacOSX27.0.sdk 由 Swift 6.4 建置，swiftc 卻是 6.3.3）
        local _sdk_ver _clang_ok
        _sdk_ver=$(xcrun --show-sdk-version 2>/dev/null)
        _clang_ok=1
        printf 'int main(void){return 0;}\n' > /tmp/jt-clt-check.c 2>/dev/null
        clang -o /tmp/jt-clt-check.out /tmp/jt-clt-check.c >/tmp/jt-clt-check.log 2>&1 || _clang_ok=0
        rm -f /tmp/jt-clt-check.c /tmp/jt-clt-check.out
        if [ "$_clang_ok" -eq 0 ]; then
            check_fail "命令列工具無法編譯（SDK ${_sdk_ver:-未知} 與編譯器版本不符）"
            echo -e "  ${C_DIM}  $(tail -2 /tmp/jt-clt-check.log | head -1)${NC}"
            echo -e "  ${C_WHITE}  請更新命令列工具後重跑安裝：${NC}"
            echo -e "  ${C_DIM}    sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install${NC}"
            echo -e "  ${C_DIM}  （已安裝 Xcode 的話：sudo xcode-select -s /Applications/Xcode.app）${NC}"
            echo -e "  ${C_DIM}  安裝會繼續，但 whisper.cpp 與 ScreenCaptureKit 元件無法編譯${NC}"
        fi
    else
        check_install "Xcode Command Line Tools 未安裝，正在觸發安裝..."
        xcode-select --install 2>/dev/null || true
        echo -e "  ${C_WHITE}請在彈出的視窗中按「安裝」，完成後重新執行 ./install.sh${NC}"
        return 1
    fi
}

check_internet() {
    section "網路連線"
    # 依序測試三個關鍵來源，任一成功即可
    local ok=0
    for url in "https://github.com" "https://pypi.org" "https://brew.sh"; do
        if curl -s --connect-timeout 5 --max-time 8 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null | grep -qE '^[23]'; then
            ok=1
            break
        fi
    done
    if [ "$ok" -eq 1 ]; then
        check_ok "網路連線正常"
    else
        check_fail "無法連線至 GitHub / PyPI / Homebrew，請確認網路連線"
        return 1
    fi
}

check_running_processes() {
    section "執行中程序檢查"
    local found=0
    local pids
    # 檢查 whisper-stream
    pids=$(pgrep -f "whisper-stream" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo -e "  ${C_WARN}[警告]${NC} whisper-stream 正在執行中（PID: ${pids}）"
        echo -e "  ${C_DIM}重新編譯可能失敗，建議先關閉${NC}"
        found=1
    fi
    # 檢查 translate_meeting.py
    pids=$(pgrep -f "translate_meeting.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo -e "  ${C_WARN}[警告]${NC} translate_meeting.py 正在執行中（PID: ${pids}）"
        echo -e "  ${C_DIM}安裝期間可能衝突，建議先關閉${NC}"
        found=1
    fi
    if [ "$found" -eq 1 ]; then
        echo ""
        echo -e "  ${C_WHITE}Y = 繼續安裝（不結束程序）${NC}"
        echo -e "  ${C_WHITE}K = 強制結束程序後繼續安裝${NC}"
        echo -e "  ${C_WHITE}N = 取消安裝${NC}"
        read -p "  請選擇 (y/K/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Kk]$ ]]; then
            pids=$(pgrep -f "whisper-stream" 2>/dev/null || true)
            if [ -n "$pids" ]; then
                kill $pids 2>/dev/null || true
                echo -e "  ${C_DIM}已結束 whisper-stream${NC}"
            fi
            pids=$(pgrep -f "translate_meeting.py" 2>/dev/null || true)
            if [ -n "$pids" ]; then
                kill $pids 2>/dev/null || true
                echo -e "  ${C_DIM}已結束 translate_meeting.py${NC}"
            fi
            sleep 1
            check_ok "程序已結束，繼續安裝"
        elif [[ ! $REPLY =~ ^[Yy]$ ]]; then
            return 1
        fi
    else
        check_ok "無衝突程序"
    fi
}

# ─── Homebrew ────────────────────────────────────
check_homebrew() {
    section "Homebrew"
    if command -v brew &>/dev/null; then
        check_ok "Homebrew 已安裝"
        return 0
    else
        echo -e "  ${C_ERR}[缺少]${NC} Homebrew 未安裝"
        echo -e "  ${C_WHITE}請先手動安裝：${NC}"
        echo -e "  ${C_DIM}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
        ((failed++))
        return 1
    fi
}

# ─── Brew packages ───────────────────────────────
# ── SDL2 偵測（能力導向，不比對 formula 名稱）────────────────────────────
# Homebrew 自 2026 起 sdl2 已成為 sdl2-compat 的別名：SDL2 API 跑在 SDL3 之上，
# brew list --formula 只會顯示 sdl2-compat。whisper.cpp 也只有 WHISPER_SDL2 選項
# （沒有 WHISPER_SDL3），而 sdl2-compat 提供完整的 SDL2 headers、sdl2.pc 與
# lib/cmake/SDL2/sdl2-config.cmake，find_package(SDL2) 找得到，可直接使用。
# 因此改成偵測「SDL2 API 有沒有到位」，而不是「裝了哪個 formula」。
_sdl2_prefixes() {
    local p
    p=$(brew --prefix 2>/dev/null) && [ -n "$p" ] && echo "$p"
    echo "/opt/homebrew"
    echo "/usr/local"
}

_sdl2_available() {
    pkg-config --exists sdl2 2>/dev/null && return 0
    local p
    for p in $(_sdl2_prefixes); do
        [ -f "$p/lib/cmake/SDL2/sdl2-config.cmake" ] && return 0
        [ -f "$p/lib/pkgconfig/sdl2.pc" ] && return 0
    done
    brew list --formula 2>/dev/null | grep -qE "^(sdl2|sdl2-compat)$" && return 0
    return 1
}

_sdl2_kind() {
    if brew list --formula 2>/dev/null | grep -q "^sdl2-compat$"; then
        echo "sdl2-compat，SDL2 API on SDL3"
    else
        echo "sdl2"
    fi
}

install_brew_formula() {
    local pkg="$1"
    local desc="$2"
    if brew list --formula 2>/dev/null | grep -q "^${pkg}$"; then
        check_ok "$desc ($pkg)"
    else
        check_install "正在安裝 $desc ($pkg)..."
        run_spinner "安裝中..." brew install "$pkg" || true
        if brew list --formula 2>/dev/null | grep -q "^${pkg}$"; then
            echo ""
            check_ok "$desc ($pkg) 安裝完成"
        else
            echo ""
            check_fail "$desc ($pkg) 安裝失敗"
        fi
    fi
}

install_brew_cask() {
    local pkg="$1"
    local desc="$2"
    if brew list --cask 2>/dev/null | grep -q "^${pkg}$"; then
        check_ok "$desc ($pkg)"
    else
        check_install "正在安裝 $desc ($pkg)..."
        if [ "$pkg" = "blackhole-2ch" ]; then
            # BlackHole 是音訊驅動，需要管理者密碼授權
            echo ""
            echo -e "  ${C_WARN}[需要密碼] BlackHole 是虛擬音訊驅動，安裝時 macOS 會要求輸入管理者密碼${NC}"
            echo ""
            brew install --cask "$pkg" || true
        else
            run_spinner "安裝中..." brew install --cask "$pkg" || true
        fi
        if brew list --cask 2>/dev/null | grep -q "^${pkg}$"; then
            check_ok "$desc ($pkg) 安裝完成"
            if [ "$pkg" = "blackhole-2ch" ]; then
                echo ""
                echo -e "  ${C_WARN}[注意] BlackHole 安裝後需要重新啟動電腦才能使用${NC}"
                echo -e "  ${C_WHITE}並需要設定 macOS 多重輸出裝置：${NC}"
                echo -e "  ${C_DIM}  1. 開啟「音訊 MIDI 設定」(Audio MIDI Setup)${NC}"
                echo -e "  ${C_DIM}  2. 點左下角 + → 建立「多重輸出裝置」${NC}"
                echo -e "  ${C_DIM}  3. 勾選你的喇叭/耳機 + BlackHole 2ch${NC}"
                echo -e "  ${C_DIM}  4. 在系統音訊設定中，將輸出設為此多重輸出裝置${NC}"
            fi
        else
            check_fail "$desc ($pkg) 安裝失敗"
        fi
    fi
}

check_brew_deps() {
    section "系統套件 (Homebrew)"
    install_brew_formula "cmake" "CMake 建構工具"
    # SDL2 音訊函式庫（whisper.cpp 的 whisper-stream 需要）
    # 注意：Homebrew 自 2026 起把 sdl2 變成 sdl2-compat 的別名（SDL2 API 跑在 SDL3 之上），
    # brew list 會顯示 sdl2-compat 而不是 sdl2，所以不能只比對 formula 名稱。
    if _sdl2_available; then
        check_ok "SDL2 音訊函式庫（$(_sdl2_kind)）"
    else
        install_brew_formula "sdl2" "SDL2 音訊函式庫"
        if ! _sdl2_available; then
            check_fail "SDL2 安裝後仍偵測不到，whisper.cpp 的即時辨識將無法編譯"
            echo -e "  ${C_DIM}可手動安裝：brew install sdl2（現會安裝 sdl2-compat）${NC}"
        fi
    fi
    install_brew_formula "ffmpeg" "FFmpeg 音訊轉檔工具"

    # BlackHole：macOS 13+ 預設改用 ScreenCaptureKit（見 check_sck），
    # 這裡只在舊系統或使用者已安裝時處理，不再強制安裝音訊驅動。
    if brew list --cask 2>/dev/null | grep -q "^blackhole-2ch$"; then
        check_ok "BlackHole 虛擬音訊 (blackhole-2ch)"
    elif _sck_macos_ok; then
        check_notice "略過 BlackHole：macOS 13+ 改用 ScreenCaptureKit 擷取系統音訊"
        echo -e "  ${C_DIM}若之後不想授權「螢幕錄製」，可自行安裝：brew install --cask blackhole-2ch${NC}"
    else
        install_brew_cask "blackhole-2ch" "BlackHole 虛擬音訊"
    fi
}

# ─── Python ──────────────────────────────────────
check_python() {
    local _arch_label
    if [ "$(uname -m)" = "arm64" ]; then
        _arch_label="Python (ARM64)"
    else
        _arch_label="Python"
    fi
    section "$_arch_label"

    local is_arm_mac=0
    [ "$(uname -m)" = "arm64" ] && is_arm_mac=1

    # Apple Silicon：必須用 ARM64 Python（Moonshine 的 libmoonshine.dylib 是 ARM64 限定）
    if [ "$is_arm_mac" -eq 1 ]; then
        # 優先檢查 ARM Python
        if [ -x "/opt/homebrew/bin/python3.12" ]; then
            PYTHON_CMD="/opt/homebrew/bin/python3.12"
            local ver
            ver=$("$PYTHON_CMD" --version 2>&1)
            check_ok "$ver (ARM64, $PYTHON_CMD)"
            return 0
        fi

        # ARM Python 不存在，嘗試自動安裝
        if [ -x "/opt/homebrew/bin/brew" ]; then
            check_install "正在用 ARM Homebrew 安裝 Python 3.12（Moonshine 需要 ARM64）..."
            /opt/homebrew/bin/brew install python@3.12 2>&1 | tail -3
            if [ -x "/opt/homebrew/bin/python3.12" ]; then
                PYTHON_CMD="/opt/homebrew/bin/python3.12"
                check_ok "Python 3.12 ARM64 安裝完成 ($PYTHON_CMD)"
                return 0
            else
                check_fail "ARM64 Python 安裝失敗"
                return 1
            fi
        else
            # 沒有 ARM Homebrew，嘗試安裝
            echo -e "  ${C_WARN}[偵測]${NC} 未找到 ARM Homebrew，嘗試安裝..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null
            if [ -x "/opt/homebrew/bin/brew" ]; then
                check_install "正在用 ARM Homebrew 安裝 Python 3.12..."
                /opt/homebrew/bin/brew install python@3.12 2>&1 | tail -3
                if [ -x "/opt/homebrew/bin/python3.12" ]; then
                    PYTHON_CMD="/opt/homebrew/bin/python3.12"
                    check_ok "Python 3.12 ARM64 安裝完成 ($PYTHON_CMD)"
                    return 0
                fi
            fi
            check_fail "無法安裝 ARM64 Python，請手動執行: /opt/homebrew/bin/brew install python@3.12"
            return 1
        fi
    fi

    # Intel Mac：用一般 Python（需要 >= 3.9，ctranslate2 不支援 3.8）
    local need_py_install=0
    if command -v "$PYTHON_CMD" &>/dev/null; then
        # pyenv shim 偵測：指令存在但無法實際執行
        if ! "$PYTHON_CMD" --version &>/dev/null 2>&1; then
            if command -v pyenv &>/dev/null; then
                local _pyenv_avail
                _pyenv_avail=$(pyenv versions --bare 2>/dev/null | grep '^3\.\(1[2-9]\|[2-9][0-9]\)' | head -5 | tr '\n' ' ')
                echo -e "  ${C_WARN}[偵測]${NC} 偵測到 pyenv，但 ${BOLD}$PYTHON_CMD${NC} 未設定可用版本。"
                if [ -n "$_pyenv_avail" ]; then
                    echo -e "  ${C_WARN}       請先執行: ${BOLD}pyenv shell ${_pyenv_avail%% *}${NC} 後重新安裝"
                else
                    echo -e "  ${C_WARN}       請先執行: ${BOLD}pyenv install 3.12 && pyenv shell 3.12${NC} 後重新安裝"
                fi
                check_fail "pyenv Python 版本未設定"
                return 1
            fi
            need_py_install=1
        else
            local py_ver_num
            py_ver_num=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            local py_minor
            py_minor=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
            if [ -n "$py_minor" ] && [ "$py_minor" -lt 9 ] 2>/dev/null; then
                echo -e "  ${C_WARN}[偵測]${NC} $PYTHON_CMD 版本 $py_ver_num 過舊（需要 >= 3.9），嘗試安裝 Python 3.12..."
                need_py_install=1
            else
                local ver
                ver=$("$PYTHON_CMD" --version 2>&1)
                check_ok "$ver ($PYTHON_CMD)"
                return 0
            fi
        fi
    else
        need_py_install=1
    fi
    if [ "$need_py_install" -eq 1 ]; then
        check_install "正在安裝 Python 3.12..."
        brew install python@3.12 || true
        if command -v python3.12 &>/dev/null; then
            PYTHON_CMD="python3.12"
            check_ok "Python 3.12 安裝完成 ($PYTHON_CMD)"
            return 0
        elif [ -x "/usr/local/bin/python3.12" ]; then
            PYTHON_CMD="/usr/local/bin/python3.12"
            check_ok "Python 3.12 安裝完成 ($PYTHON_CMD)"
            return 0
        else
            check_fail "Python 3.12 安裝失敗，請手動執行: brew install python@3.12"
            return 1
        fi
    fi
}

# ─── whisper.cpp ─────────────────────────────────
check_whisper_cpp() {
    section "whisper.cpp (語音辨識引擎)"

    # 檢查原始碼
    if [ ! -d "$WHISPER_DIR" ]; then
        check_install "正在下載 whisper.cpp..."
        run_spinner "下載中..." git clone https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR" || true
        if [ -d "$WHISPER_DIR" ]; then
            check_ok "whisper.cpp 下載完成"
        else
            check_fail "whisper.cpp 下載失敗"
            return 1
        fi
    else
        check_ok "whisper.cpp 原始碼存在"
    fi

    # 檢查是否需要（重新）編譯
    local need_build=0
    if [ ! -f "$WHISPER_DIR/build/bin/whisper-stream" ]; then
        need_build=1
    else
        # 檢查 dylib 是否正常（路徑搬遷後會壞）
        if ! "$WHISPER_DIR/build/bin/whisper-stream" --help &>/dev/null 2>&1; then
            echo -e "  ${C_WARN}[偵測]${NC} whisper-stream 無法執行（可能路徑已變更），需重新編譯"
            need_build=1
        fi
    fi

    if [ "$need_build" -eq 1 ]; then
        check_install "正在編譯 whisper.cpp（可能需要幾分鐘）..."
        rm -rf "$WHISPER_DIR/build"
        cd "$WHISPER_DIR"

        # 修補 gguf.cpp 缺少 errno 標頭檔（whisper.cpp 上游 bug，新版 clang 會報錯）
        local _gguf="$WHISPER_DIR/ggml/src/gguf.cpp"
        if [ -f "$_gguf" ] && ! grep -q '#include <cerrno>' "$_gguf"; then
            if grep -q 'errno' "$_gguf"; then
                echo -e "  ${C_DIM}修補 gguf.cpp（加入 #include <cerrno>）${NC}"
                sed -i.bak '1s/^/#include <cerrno>\n/' "$_gguf"
            fi
        fi

        # whisper.cpp 只有 WHISPER_SDL2 這個選項，沒有 WHISPER_SDL3。
        # 傳入未知的 -DWHISPER_SDL3=ON 時 cmake 不會報錯，只會安靜地建出
        # 沒有 whisper-stream 的版本，接著在 --target whisper-stream 才爆
        # 「No rule to make target」，因此這裡一律用 SDL2（sdl2-compat 亦適用）。
        local sdl_cmake_flag="-DWHISPER_SDL2=ON"
        if _sdl2_available; then
            echo -e "  ${C_DIM}使用 SDL2（$(_sdl2_kind)）${NC}"
        else
            echo -e "  ${C_WARN}偵測不到 SDL2，whisper-stream 可能無法編譯${NC}"
            echo -e "  ${C_DIM}請先執行：brew install sdl2${NC}"
        fi

        # 偵測架構
        local arch
        arch=$(uname -m)
        local cmake_extra_flags=""
        if [ "$arch" = "arm64" ]; then
            # Apple Silicon: ARM Homebrew + Metal
            # 注意 1：Metal 與架構旗標不可綁在「SDL 目錄是否存在」之下——改用
            #   sdl2-compat 後 /opt/homebrew/Cellar/sdl2 不會存在，整組加速旗標
            #   會被跳過，建出沒有 Metal 的版本
            # 注意 2：prefix 固定寫 /opt/homebrew，不能用 $(brew --prefix)——
            #   同時裝了 Intel Homebrew 時 brew --prefix 可能回傳 /usr/local，
            #   arm64 建置吃到 x86_64 的函式庫會失敗
            cmake_extra_flags="-DCMAKE_OSX_ARCHITECTURES=arm64 -DWHISPER_METAL=ON -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.5-a+fp16 -DCMAKE_PREFIX_PATH=/opt/homebrew"
        elif [ "$arch" = "x86_64" ]; then
            # Intel Mac: Homebrew 在 /usr/local，不啟用 Metal（Intel Mac 用 AVX 加速）
            cmake_extra_flags="-DCMAKE_OSX_ARCHITECTURES=x86_64 -DGGML_METAL=OFF -DCMAKE_PREFIX_PATH=/usr/local"
        fi

        local ncpu
        ncpu=$(sysctl -n hw.ncpu)
        if ! run_spinner "編譯中..." bash -c "cd '$WHISPER_DIR' && cmake -B build $sdl_cmake_flag $cmake_extra_flags 2>&1 && cmake --build build --target whisper-stream -j$ncpu 2>&1"; then
            echo ""
            check_fail "whisper.cpp 編譯失敗:"
            # 從 log 中找實際的編譯器錯誤（error: 開頭的行）
            local _compiler_errors
            _compiler_errors=$(grep -i "error:" "$SPINNER_OUTPUT" 2>/dev/null | grep -v "^make" | head -5)
            if [ -n "$_compiler_errors" ]; then
                echo -e "  ${C_DIM}${_compiler_errors}${NC}"
            else
                echo -e "  ${C_DIM}$(tail -10 "$SPINNER_OUTPUT")${NC}"
            fi
            echo -e "  ${C_DIM}完整 log: $SPINNER_OUTPUT${NC}"
        fi
        echo ""
        cd "$SCRIPT_DIR"

        if [ -f "$WHISPER_DIR/build/bin/whisper-stream" ]; then
            check_ok "whisper.cpp 編譯完成"
        else
            check_fail "whisper.cpp 編譯失敗"
            return 1
        fi
    else
        check_ok "whisper-stream 已編譯且可執行"
    fi
}

# ─── Whisper 模型 ─────────────────────────────────
check_whisper_models() {
    section "Whisper 語音模型"

    local has_model=0
    for model_file in "ggml-base.en.bin" "ggml-small.en.bin" "ggml-large-v3-turbo.bin" "ggml-medium.en.bin"; do
        local model_path="$MODELS_DIR/$model_file"
        if [ -f "$model_path" ]; then
            local size
            size=$(du -h "$model_path" | cut -f1 | xargs)
            check_ok "$model_file ($size)"
            has_model=1
        fi
    done

    local arch
    arch=$(uname -m)
    if [ "$has_model" -eq 0 ]; then
        if [ "$arch" = "x86_64" ]; then
            # Intel Mac：下載 small.en（適合 Intel CPU，466MB）
            check_install "正在下載預設模型 (small.en，適合 Intel CPU，約 466MB)..."
            cd "$WHISPER_DIR"
            run_spinner "下載中..." bash models/download-ggml-model.sh small.en
            echo ""
            cd "$SCRIPT_DIR"
            if [ -f "$MODELS_DIR/ggml-small.en.bin" ]; then
                check_ok "ggml-small.en.bin 下載完成"
            else
                check_fail "模型下載失敗，請手動下載"
            fi
        else
            # Apple Silicon：下載 large-v3-turbo（有 Metal 加速，809MB）
            check_install "正在下載預設模型 (large-v3-turbo，約 809MB)..."
            cd "$WHISPER_DIR"
            run_spinner "下載中..." bash models/download-ggml-model.sh large-v3-turbo
            echo ""
            cd "$SCRIPT_DIR"
            if [ -f "$MODELS_DIR/ggml-large-v3-turbo.bin" ]; then
                check_ok "ggml-large-v3-turbo.bin 下載完成"
            else
                check_fail "模型下載失敗，請手動下載"
            fi
        fi
    fi

    # Intel Mac：確保有適合的小模型（large-v3-turbo 在 Intel CPU 上太慢）
    if [ "$arch" = "x86_64" ]; then
        if [ ! -f "$MODELS_DIR/ggml-small.en.bin" ]; then
            check_install "Intel CPU 建議使用 small.en 模型，正在下載（約 466MB）..."
            cd "$WHISPER_DIR"
            run_spinner "下載中..." bash models/download-ggml-model.sh small.en
            echo ""
            cd "$SCRIPT_DIR"
            if [ -f "$MODELS_DIR/ggml-small.en.bin" ]; then
                check_ok "ggml-small.en.bin 下載完成"
            else
                check_fail "small.en 下載失敗（可在程式啟動時選擇下載）"
            fi
        fi
        if [ ! -f "$MODELS_DIR/ggml-base.en.bin" ]; then
            check_install "正在下載 base.en 模型（最快速，約 142MB）..."
            cd "$WHISPER_DIR"
            run_spinner "下載中..." bash models/download-ggml-model.sh base.en
            echo ""
            cd "$SCRIPT_DIR"
            if [ -f "$MODELS_DIR/ggml-base.en.bin" ]; then
                check_ok "ggml-base.en.bin 下載完成"
            else
                check_fail "base.en 下載失敗（可在程式啟動時選擇下載）"
            fi
        fi
    fi
}

# ─── venv 能不能用（2026-10-05）──────────────────
# 以前只看 `venv/bin/python3 --version`。venv 的 python3 指向 /usr/bin/python3 時，作業系統升級
# （Ubuntu 22.04→24.04 是 3.10→3.12）把它換成新版本，--version 照樣成功，套件卻全在 lib/python<舊版>：
# 每個 import 都失敗、服務一直重啟，安裝程式還說「venv 正常」。改成比對建立 venv 時的版本（pyvenv.cfg）。
# 同一段 Python 也送到 GPU 伺服器上跑（_rw_venv_ok），install.ps1 有一份逐字相同的（tools/test_venv_python_version.py 比對）。
_VENV_CHECK_PY='import os, sys
v = ""
for l in open(os.path.join(sys.prefix, "pyvenv.cfg"), encoding="utf-8"):
    k, _, x = l.partition("=")
    if k.strip() in ("version", "version_info"):
        v = ".".join(x.strip().split(".")[:2])
n = "%d.%d" % sys.version_info[:2]
print(v, n)
sys.exit(0 if v in ("", n) else 3)'

# $1=venv 資料夾。可用回傳 0；要重建回傳 1，原因放在 VENV_PROBLEM
venv_problem() {
    local out rc
    VENV_PROBLEM=""
    out=$(printf '%s\n' "$_VENV_CHECK_PY" | "$1/bin/python3" - 2>/dev/null); rc=$?
    [ $rc -eq 0 ] && return 0
    if [ $rc -eq 3 ]; then
        VENV_PROBLEM="venv 是用 Python ${out%% *} 建立的，現在的 python3 是 ${out##* }（作業系統升級換了 Python 版本？）"
    else
        VENV_PROBLEM="venv 已損壞（可能路徑已變更或從其他作業系統複製）"
    fi
    return 1
}

# 套件載入檢查：$1=python、$2=模組名稱（逗號分隔＝任一個載入得了就算有）。
# python-multipart 0.0.13 起模組改名 python_multipart，舊名 multipart 只剩相容層（已標淘汰）；
# 只認舊名的話，等它拿掉相容層，今天新裝的機器會一直被判成缺少（pip 又回「已經裝了」），只認新名則舊版判錯
_py_import_ok() {
    local py="$1" m
    local IFS=','
    for m in $2; do
        "$py" -c "import $m" >/dev/null 2>&1 && return 0
    done
    return 1
}

# ─── Python venv ─────────────────────────────────
check_venv() {
    section "Python 虛擬環境"

    local need_create=0
    if [ ! -d "$VENV_DIR" ]; then
        need_create=1
    else
        # 檢查 venv 是否可用（路徑搬遷、或作業系統升級換了 Python 版本都會壞）
        if ! venv_problem "$VENV_DIR"; then
            echo -e "  ${C_WARN}[偵測]${NC} ${VENV_PROBLEM}，需重建"
            need_create=1
        # Apple Silicon：檢查 venv 是否為 ARM64（x86 venv 跑不了 Moonshine）
        elif [ "$(uname -m)" = "arm64" ]; then
            local venv_arch
            venv_arch=$("$VENV_DIR/bin/python3" -c "import platform; print(platform.machine())" 2>/dev/null)
            if [ "$venv_arch" != "arm64" ]; then
                echo -e "  ${C_WARN}[偵測]${NC} venv 是 $venv_arch 架構，需要 ARM64，重建中"
                need_create=1
            fi
        fi
    fi

    if [ "$need_create" -eq 1 ]; then
        check_install "正在建立 Python 虛擬環境..."
        rm -rf "$VENV_DIR"
        "$PYTHON_CMD" -m venv "$VENV_DIR"
        if [ $? -eq 0 ]; then
            check_ok "虛擬環境建立完成"
        else
            check_fail "虛擬環境建立失敗"
            return 1
        fi
    else
        check_ok "虛擬環境正常"
    fi

    # 檢查必要套件
    source "$VENV_DIR/bin/activate"

    local missing_pkgs=()
    if ! python3 -c "import ctranslate2" &>/dev/null 2>&1; then
        # Intel Mac (x86_64) 只有 ctranslate2 <= 4.3.1 有預建 wheel
        local arch
        arch=$(uname -m)
        if [ "$arch" = "x86_64" ]; then
            missing_pkgs+=("ctranslate2==4.3.1")
        else
            missing_pkgs+=("ctranslate2")
        fi
    fi
    if ! python3 -c "import sentencepiece" &>/dev/null 2>&1; then
        missing_pkgs+=("sentencepiece")
    fi
    if ! python3 -c "import opencc" &>/dev/null 2>&1; then
        missing_pkgs+=("opencc-python-reimplemented")
    fi
    if ! python3 -c "import sounddevice" &>/dev/null 2>&1; then
        missing_pkgs+=("sounddevice")
    fi
    if ! python3 -c "import numpy" &>/dev/null 2>&1; then
        # Intel Mac (x86_64)：ctranslate2==4.3.1 編譯時用 NumPy 1.x，與 NumPy 2.x 不相容
        if [ "$(uname -m)" = "x86_64" ]; then
            missing_pkgs+=("numpy<2")
        else
            missing_pkgs+=("numpy")
        fi
    elif [ "$(uname -m)" = "x86_64" ]; then
        # Intel Mac：已裝 NumPy 但若為 2.x 需降級（ctranslate2==4.3.1 不相容）
        local np_major
        np_major=$(python3 -c "import numpy; print(numpy.__version__.split('.')[0])" 2>/dev/null)
        if [ "$np_major" = "2" ]; then
            missing_pkgs+=("numpy<2")
        fi
    fi
    if ! python3 -c "import faster_whisper" &>/dev/null 2>&1; then
        missing_pkgs+=("faster-whisper")
    fi
    if ! python3 -c "import resemblyzer" &>/dev/null 2>&1; then
        # resemblyzer 依賴 webrtcvad，webrtcvad 需要 pkg_resources（setuptools < 81）
        if ! python3 -c "import pkg_resources" &>/dev/null 2>&1; then
            pip install --quiet --disable-pip-version-check "setuptools<81" 2>&1 | tail -1
        fi
        # resemblyzer → librosa → numba → llvmlite
        # 先確保 llvmlite/numba 有預建 wheel 的版本，避免 source build 失敗
        if ! python3 -c "import numba" &>/dev/null 2>&1; then
            pip install --quiet --disable-pip-version-check --only-binary=:all: "llvmlite" "numba" 2>/dev/null || true
        fi
        missing_pkgs+=("resemblyzer")
    fi
    if ! python3 -c "import spectralcluster" &>/dev/null 2>&1; then
        missing_pkgs+=("spectralcluster")
    fi
    if ! python3 -c "import noisereduce" &>/dev/null 2>&1; then
        missing_pkgs+=("noisereduce")
    fi
    if ! python3 -c "import fastapi" &>/dev/null 2>&1; then
        missing_pkgs+=("fastapi")
    fi
    if ! python3 -c "import uvicorn" &>/dev/null 2>&1; then
        missing_pkgs+=("uvicorn")
    fi
    if ! python3 -c "import websockets" &>/dev/null 2>&1; then
        missing_pkgs+=("websockets")
    fi
    if ! _py_import_ok python3 python_multipart,multipart; then
        missing_pkgs+=("python-multipart")
    fi
    if ! python3 -c "import PyQt6" &>/dev/null 2>&1; then
        missing_pkgs+=("PyQt6")
    fi

    # 套件中文說明對照（pip 套件名 → 說明）
    _pkg_label() {
        case "$1" in
            ctranslate2*)              echo "ctranslate2（語音辨識加速引擎）" ;;
            sentencepiece)             echo "sentencepiece（分詞工具）" ;;
            opencc-python-reimplemented) echo "OpenCC（簡繁轉換）" ;;
            sounddevice)               echo "sounddevice（音訊擷取）" ;;
            numpy*)                    echo "numpy（數值計算）" ;;
            faster-whisper)            echo "faster-whisper（離線語音辨識）" ;;
            resemblyzer)               echo "resemblyzer（講者辨識 - 聲紋提取）" ;;
            spectralcluster)           echo "spectralcluster（講者辨識 - 分群）" ;;
            noisereduce)               echo "noisereduce（背景降噪）" ;;
            fastapi)                   echo "fastapi（WebUI 伺服器）" ;;
            uvicorn)                   echo "uvicorn（WebUI ASGI 伺服器）" ;;
            websockets)                echo "websockets（WebUI 即時通訊）" ;;
            python-multipart)          echo "python-multipart（WebUI 檔案上傳）" ;;
            PyQt6)                     echo "PyQt6（懸浮字幕視窗）" ;;
            *)                         echo "$1" ;;
        esac
    }
    # import 名稱 → 說明
    _import_label() {
        case "$1" in
            ctranslate2)    echo "ctranslate2（語音辨識加速引擎）" ;;
            sentencepiece)  echo "sentencepiece（分詞工具）" ;;
            opencc)         echo "OpenCC（簡繁轉換）" ;;
            sounddevice)    echo "sounddevice（音訊擷取）" ;;
            numpy)          echo "numpy（數值計算）" ;;
            faster_whisper) echo "faster-whisper（離線語音辨識）" ;;
            resemblyzer)    echo "resemblyzer（講者辨識 - 聲紋提取）" ;;
            spectralcluster) echo "spectralcluster（講者辨識 - 分群）" ;;
            noisereduce)    echo "noisereduce（背景降噪）" ;;
            *)              echo "$1" ;;
        esac
    }

    if [ ${#missing_pkgs[@]} -gt 0 ]; then
        check_install "正在安裝 ${#missing_pkgs[@]} 個 Python 套件..."
        # 逐個安裝，避免單一套件失敗導致全部取消
        for pkg in "${missing_pkgs[@]}"; do
            local label
            label="$(_pkg_label "$pkg")"
            if ! run_spinner "$label ..." pip install --disable-pip-version-check "$pkg"; then
                echo ""
                check_fail "$label 安裝失敗:"
                echo -e "  ${C_DIM}$(grep -i 'error' "$SPINNER_OUTPUT" 2>/dev/null | grep -v "^make" | tail -3)${NC}"
                echo ""
            else
                echo ""
            fi
        done
        # 驗證（用 import 名稱，不是 pip 套件名稱）
        local all_ok=1
        for pkg in ctranslate2 sentencepiece opencc sounddevice numpy faster_whisper resemblyzer spectralcluster noisereduce; do
            local label
            label="$(_import_label "$pkg")"
            if python3 -c "import $pkg" &>/dev/null 2>&1; then
                check_ok "$label"
            else
                check_fail "$label 安裝失敗"
                all_ok=0
            fi
        done
    else
        for pkg in ctranslate2 sentencepiece opencc sounddevice numpy faster_whisper resemblyzer spectralcluster noisereduce; do
            local label
            label="$(_import_label "$pkg")"
            check_ok "${label}（已安裝）"
        done
    fi

    deactivate
}

# ─── Moonshine ASR ──────────────────────────────
check_moonshine() {
    section "Moonshine ASR (英文串流辨識引擎)"

    source "$VENV_DIR/bin/activate"

    if python3 -c "from moonshine_voice import get_model_for_language" &>/dev/null 2>&1; then
        check_ok "moonshine-voice 已安裝"
    else
        check_install "正在安裝 moonshine-voice..."
        if ! run_spinner "安裝中..." pip install --disable-pip-version-check moonshine-voice; then
            echo ""
            check_fail "moonshine-voice 安裝失敗:"
            echo -e "  ${C_DIM}$(tail -5 "$SPINNER_OUTPUT")${NC}"
        fi
        echo ""
        if python3 -c "from moonshine_voice import get_model_for_language" &>/dev/null 2>&1; then
            check_ok "moonshine-voice 安裝完成"
        else
            check_fail "moonshine-voice 安裝失敗（英文模式將改用 Whisper）"
        fi
    fi

    # 下載預設模型 (medium streaming)
    if python3 -c "from moonshine_voice import get_model_for_language" &>/dev/null 2>&1; then
        # 先檢查模型是否已存在
        local model_status
        model_status=$(python3 -c "
import os, sys
from moonshine_voice import get_model_for_language, ModelArch
try:
    path, arch = get_model_for_language('en', ModelArch.MEDIUM_STREAMING)
    if os.path.isdir(path):
        print('EXISTS:' + path)
    else:
        print('NEED_DOWNLOAD')
except Exception:
    print('NEED_DOWNLOAD')
" 2>/dev/null)
        if [[ "$model_status" == EXISTS:* ]]; then
            check_ok "Moonshine medium 模型就緒"
        else
            check_install "正在下載 Moonshine 模型 (medium, ~245MB)..."
            if run_spinner "下載中..." python3 -c "
from moonshine_voice import get_model_for_language, ModelArch
path, arch = get_model_for_language('en', ModelArch.MEDIUM_STREAMING)
"; then
                echo ""
                check_ok "Moonshine medium 模型下載完成"
            else
                check_fail "Moonshine 模型下載失敗（英文模式將改用 Whisper）"
            fi
        fi
    fi

    deactivate
}

# ─── Argos 翻譯模型 ──────────────────────────────
check_argos_model() {
    section "Argos 離線翻譯模型 (英→中)"

    if [ -d "$ARGOS_PKG_DIR" ] && [ -f "$ARGOS_PKG_DIR/sentencepiece.model" ] && [ -d "$ARGOS_PKG_DIR/model" ]; then
        check_ok "翻譯模型已安裝 ($ARGOS_PKG_DIR)"
    else
        check_install "正在下載 Argos 翻譯模型..."
        # 使用 argos-translate Python 套件來安裝模型
        source "$VENV_DIR/bin/activate"
        if ! run_spinner "安裝套件..." pip install --disable-pip-version-check argostranslate; then
            echo ""
            check_fail "argostranslate 安裝失敗:"
            echo -e "  ${C_DIM}$(tail -5 "$SPINNER_OUTPUT")${NC}"
        fi
        echo ""
        run_spinner "下載模型..." python3 -c "
import os, ssl
from argostranslate import package
package.update_package_index()
pkgs = package.get_available_packages()
en_zh = next((p for p in pkgs if p.from_code == 'en' and p.to_code == 'zh'), None)
if en_zh:
    try:
        path = en_zh.download()
        package.install_from_path(path)
        print('OK')
    except Exception as e:
        if 'SSL' in str(e) or 'CERTIFICATE' in str(e).upper():
            ssl._create_default_https_context = ssl._create_unverified_context
            os.environ['CURL_CA_BUNDLE'] = ''
            os.environ['REQUESTS_CA_BUNDLE'] = ''
            package.update_package_index()
            pkgs2 = package.get_available_packages()
            en_zh2 = next((p for p in pkgs2 if p.from_code == 'en' and p.to_code == 'zh'), None)
            if en_zh2:
                package.install_from_path(en_zh2.download())
                print('OK')
            else:
                print('FAIL')
        else:
            print('FAIL')
else:
    print('FAIL')
"
        echo ""
        deactivate

        if [ -d "$ARGOS_PKG_DIR" ]; then
            check_ok "翻譯模型安裝完成"
        else
            # 模型可能安裝在不同版本的目錄
            local found
            found=$(find "$HOME/.local/share/argos-translate/packages" -maxdepth 1 -name "translate-en_zh*" -type d 2>/dev/null | head -1)
            if [ -n "$found" ]; then
                check_ok "翻譯模型安裝完成 ($found)"
                echo -e "  ${C_WARN}[注意]${NC} 模型版本路徑可能與程式預設不同"
                echo -e "  ${C_DIM}  程式預設: $ARGOS_PKG_DIR${NC}"
                echo -e "  ${C_DIM}  實際路徑: $found${NC}"
                echo -e "  ${C_WHITE}  可能需要更新 translate_meeting.py 中的 ARGOS_PKG_PATH${NC}"
            else
                check_fail "翻譯模型安裝失敗，請手動安裝"
                echo -e "  ${C_DIM}  pip install argostranslate${NC}"
                echo -e "  ${C_DIM}  然後用 Python 安裝 en→zh 模型${NC}"
            fi
        fi
    fi
}

# ─── NLLB 翻譯模型 ──────────────────────────────
check_nllb_model() {
    section "NLLB 離線翻譯模型（中日韓英互譯，CC-BY-NC 4.0 授權）"

    if [ -d "$NLLB_MODEL_DIR" ] && [ -f "$NLLB_MODEL_DIR/model.bin" ] && \
       [ -f "$NLLB_MODEL_DIR/sentencepiece.bpe.model" ]; then
        check_ok "NLLB 模型已安裝 ($NLLB_MODEL_DIR)"
    else
        check_install "正在下載 NLLB 600M 模型（約 600MB）..."
        source "$VENV_DIR/bin/activate"
        # 確保 huggingface_hub 已安裝
        pip install --disable-pip-version-check -q huggingface_hub 2>/dev/null
        mkdir -p "$NLLB_MODEL_DIR"
        echo ""
        hf_download "JustFrederik/nllb-200-distilled-600M-ct2-int8" "NLLB 模型" "$NLLB_MODEL_DIR"
        echo ""
        deactivate

        if [ -f "$NLLB_MODEL_DIR/model.bin" ]; then
            check_ok "NLLB 模型安裝完成"
        else
            check_fail "NLLB 模型下載失敗"
            echo -e "  ${C_DIM}  請確認網路連線後重新執行安裝${NC}"
        fi
    fi
}

# ─── faster-whisper 模型預下載（全部下載）──────────────────────────────
check_faster_whisper_model() {
    section "faster-whisper 模型預下載"

    source "$VENV_DIR/bin/activate"
    pip install --disable-pip-version-check -q huggingface_hub 2>/dev/null

    local _fw_models="base.en:約150MB base:約150MB small.en:約500MB small:約500MB large-v3-turbo:約1.6GB"

    for _fw_entry in $_fw_models; do
        local _fw_name="${_fw_entry%%:*}"
        local _fw_size="${_fw_entry##*:}"

        # 檢查是否已存在
        local _fw_found
        _fw_found=$(python3 -c "
import os
dirs = []
try:
    from huggingface_hub.constants import HF_HUB_CACHE   # 有設 HF_HOME／HF_HUB_CACHE 時模型在那裡（2026-10-05）
    dirs.append(HF_HUB_CACHE)
except Exception: pass
default = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
if default not in dirs: dirs.append(default)
for d in dirs:
    for prefix in ['Systran', 'mobiuslabsgmbh', 'deepdml']:
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-$_fw_name')):
            print('found'); exit()
print('notfound')
" 2>/dev/null)

        if [ "$_fw_found" = "found" ]; then
            check_ok "faster-whisper $_fw_name 已存在"
            continue
        fi

        local _fw_dl_msg="下載 faster-whisper $_fw_name"
        _fw_dl_msg="${_fw_dl_msg}（${_fw_size}）..."
        echo -e "  ${C_DIM}$_fw_dl_msg${NC}"

        # 嘗試多個 repo（靜默切換）
        local _dl_ok=0 _fw_attempt=0 _fw_total=3
        for _fw_repo in "mobiuslabsgmbh/faster-whisper-$_fw_name" "Systran/faster-whisper-$_fw_name" "deepdml/faster-whisper-$_fw_name"; do
            ((_fw_attempt++)) || true
            python3 -c "
import os
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
from huggingface_hub import snapshot_download
try:
    snapshot_download('$_fw_repo')
except:
    try:
        import ssl; ssl._create_default_https_context = ssl._create_unverified_context
        os.environ['CURL_CA_BUNDLE'] = ''
        os.environ['REQUESTS_CA_BUNDLE'] = ''
        snapshot_download('$_fw_repo')
    except:
        pass
" 2>/dev/null
            local _fw_check
            _fw_check=$(python3 -c "
import os
dirs = []
try:
    from huggingface_hub.constants import HF_HUB_CACHE   # 有設 HF_HOME／HF_HUB_CACHE 時模型在那裡（2026-10-05）
    dirs.append(HF_HUB_CACHE)
except Exception: pass
default = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
if default not in dirs: dirs.append(default)
for d in dirs:
    for prefix in ['Systran', 'mobiuslabsgmbh', 'deepdml']:
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-$_fw_name')):
            print('found'); exit()
print('notfound')
" 2>/dev/null)
            if [ "$_fw_check" = "found" ]; then
                _dl_ok=1
                break
            fi
            echo -e "  ${C_DIM}  ($_fw_attempt/$_fw_total) 嘗試更換其他來源...${NC}"
        done

        if [ $_dl_ok -eq 1 ]; then
            check_ok "faster-whisper $_fw_name 安裝完成"
        else
            check_notice "faster-whisper $_fw_name 下載失敗，可稍後重新執行安裝"
        fi
    done

    deactivate
}

# ─── MLX Whisper（僅 Apple Silicon）────────────────
check_mlx_whisper() {
    # 僅 ARM64 Mac，Intel 跳過
    if [ "$(uname -m)" != "arm64" ]; then
        return 0
    fi

    section "MLX Whisper（Apple Silicon GPU 加速）"

    source "$VENV_DIR/bin/activate"

    # 安裝 mlx-whisper（含 MLX 框架，首次安裝需較長時間）
    if python3 -c "import mlx_whisper" &>/dev/null 2>&1; then
        check_ok "mlx-whisper 已安裝"
    else
        check_install "正在安裝 mlx-whisper（含 MLX 框架，首次約需 3-5 分鐘）..."
        echo ""
        run_spinner "安裝 mlx-whisper..." pip install --disable-pip-version-check mlx-whisper
        echo ""
        if python3 -c "import mlx_whisper" &>/dev/null 2>&1; then
            check_ok "mlx-whisper 安裝完成"
        else
            check_fail "mlx-whisper 安裝失敗"
            deactivate
            return 0
        fi
    fi

    # 檢查 MLX 格式模型（large-v3-turbo）
    local mlx_model="large-v3-turbo"
    local mlx_found
    mlx_found=$(python3 -c "
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
    if os.path.isdir(os.path.join(d, 'models--mlx-community--whisper-$mlx_model')):
        found = True
        break
print('found' if found else 'notfound')
" 2>/dev/null)

    if [ "$mlx_found" = "found" ]; then
        check_ok "MLX Whisper 模型 $mlx_model 已存在"
    else
        check_install "正在下載 MLX Whisper $mlx_model 模型（約 1.6GB）..."
        echo ""
        hf_download "mlx-community/whisper-$mlx_model" "MLX Whisper 模型" ""
        echo ""

        # 驗證下載
        mlx_found=$(python3 -c "
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
    if os.path.isdir(os.path.join(d, 'models--mlx-community--whisper-$mlx_model')):
        found = True
        break
print('found' if found else 'notfound')
" 2>/dev/null)

        if [ "$mlx_found" = "found" ]; then
            check_ok "MLX Whisper 模型 $mlx_model 安裝完成"
        else
            check_fail "MLX Whisper 模型下載失敗"
            echo -e "  ${C_DIM}  請確認網路連線後重新執行安裝${NC}"
        fi
    fi

    deactivate
}

# ─── Qwen3-ASR 本機辨識（實驗，v2.24.0）──────────
# Apple Silicon 用 mlx-audio（會一併裝 transformers）；soynlp 是韓文對齊用的。
# 模型第一次選用時才下載（約 2.3 GB），這裡只裝套件。看能力不看套件名稱：舊版 mlx-audio 沒有 qwen3_asr
_QWEN_MLX_CHECK='import importlib.util as u, os, sys, soynlp
s = u.find_spec("mlx_audio")
sys.exit(0 if s and any(os.path.isdir(os.path.join(p, "stt", "models", "qwen3_asr")) for p in (s.submodule_search_locations or [])) else 1)'

# 其他平台（install-linux.sh、install.ps1 同一個判斷）：transformers 內建 qwen3_asr（5.17 起）＋ torch ＋ soynlp
_QWEN_TF_CHECK='import sys, torch, soynlp
from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES as M
sys.exit(0 if "qwen3_asr" in M else 1)'

# ─── Nemotron 講者辨識（v2.26.0）────────────────────
# transformers 5.18 起內建 nemotron3_diarization。看能力不看版本號（與 install.ps1 的 $NEMO_TF_CHECK 同一個判斷）。
# Intel Mac 不支援（PyTorch 2.3 起沒有 x86_64 macOS 版）。模型（nvidia/Nemotron-3-Diarization，約 0.71 GB）
# 安裝時先下載，講者辨識時不必上網。任何一步失敗都不影響其他功能：講者辨識照舊用現行方法（resemblyzer）
_NEMO_TF_CHECK='import sys, torch
from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES as M
sys.exit(0 if "nemotron3_diarization" in M else 1)'
_NEMO_MODEL="nvidia/Nemotron-3-Diarization"

check_nemotron_local() {
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" != "arm64" ]; then
        return 0
    fi
    local act=0
    if [ -z "${VIRTUAL_ENV:-}" ]; then
        source "$VENV_DIR/bin/activate" || return 0
        act=1
    fi
    section "Nemotron 講者辨識"
    local ok=0
    if python3 -c "$_NEMO_TF_CHECK" >/dev/null 2>&1; then
        check_ok "transformers（Nemotron 講者辨識）（已安裝）"
        ok=1
    elif run_spinner "安裝 transformers 5.18（Nemotron 講者辨識）..." \
            pip install --disable-pip-version-check "transformers>=5.18" \
            && python3 -c "$_NEMO_TF_CHECK" >/dev/null 2>&1; then
        echo ""
        check_ok "transformers（Nemotron 講者辨識）"
        ok=1
    else
        echo ""
        check_notice "transformers 5.18 安裝失敗：講者辨識照舊用現行方法，其他功能不受影響"
    fi
    if [ $ok -eq 1 ]; then
        if python3 -c "from huggingface_hub import snapshot_download as d; d('$_NEMO_MODEL', local_files_only=True)" >/dev/null 2>&1; then
            check_ok "Nemotron 模型（已下載）"
        elif run_spinner "下載 Nemotron 模型（約 0.71 GB）..." \
                python3 -c "from huggingface_hub import snapshot_download as d; d('$_NEMO_MODEL')"; then
            echo ""
            check_ok "Nemotron 模型下載完成"
        else
            echo ""
            check_notice "Nemotron 模型下載失敗：第一次做講者辨識時會再下載（需要網路）"
        fi
    fi
    if [ $act -eq 1 ]; then deactivate; fi
    return 0
}

check_qwen_local_mac() {
    # 僅 ARM64 Mac（Intel Mac 決定不支援）
    if [ "$(uname -m)" != "arm64" ]; then
        return 0
    fi
    section "Qwen3-ASR 本機辨識（實驗，Apple Silicon MLX）"
    source "$VENV_DIR/bin/activate"
    if python3 -c "$_QWEN_MLX_CHECK" &>/dev/null; then
        check_ok "mlx-audio（已安裝）"
    else
        check_install "正在安裝 mlx-audio ..."
        if run_spinner "安裝 mlx-audio..." pip install --disable-pip-version-check "mlx-audio>=0.5.6" soynlp \
                && python3 -c "$_QWEN_MLX_CHECK" &>/dev/null; then
            echo ""
            check_ok "mlx-audio 安裝完成（Qwen3-ASR 模型第一次選用時下載，約 2.3 GB）"
        else
            echo ""
            check_notice "mlx-audio 安裝失敗：Qwen3-ASR 只能透過 GPU 伺服器使用，其他功能不受影響"
        fi
    fi
    # 與 mlx-whisper 共用 MLX（2026-09-27 實測 mlx-audio 0.5.6 不動既有套件版本、辨識結果逐字相同）；裝完再確認一次
    if ! python3 -c "import mlx_whisper" &>/dev/null; then
        check_notice "mlx-whisper 無法載入，請重新執行 ./install.sh"
    fi
    deactivate
}

# ─── 文字轉語音本機合成（2026-10，Apple Silicon，mlx-audio 的 VoxCPM2）────
# 有 GPU 伺服器時合成在伺服器做，這台不必裝。要裝的話：**有人可以回答才問**（約 4 GB），無人值守不動。
# 看能力不看版本：mlx-audio 已經有 voxcpm2 就不升級（Qwen3-ASR 本機路徑也用它，能不動就不動），只補 g2pw、pypinyin、opencc
_TTS_MLX_CHECK='import importlib.util as u, os, sys, g2pw, pypinyin, opencc
s = u.find_spec("mlx_audio")
sys.exit(0 if s and any(os.path.isdir(os.path.join(p, "tts", "models", "voxcpm2")) for p in (s.submodule_search_locations or [])) else 1)'
_TTS_MLX_MODEL="mlx-community/VoxCPM2-8bit"
_TTS_MLX_REV="d52725898a0675703f7f9ddc5a4d1a3cdbb99032"
_TTS_MLX_FILES='["config.json", "model.safetensors", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"]' 

# 能不能登入 GPU 伺服器（已有設定、檢查伺服器環境之前）：先試金鑰、不問密碼；沒有金鑰就產生一個；
# 要密碼時講清楚再問（輸入一次就把這台的金鑰加到伺服器）。以前直接在「正在檢查伺服器環境」的轉圈裡連線，
# ssh 問密碼的提示被轉圈蓋掉，看起來像卡住（2026-10-09 Mac 實機）。沒有人可以輸入時不問、回 1
# 用法：_rw_ensure_key_auth 使用者 主機 埠；成功時 existing_key 是可用的金鑰（可能是空的＝ssh 預設金鑰就能登入）
_rw_ensure_key_auth() {
    local user="$1" host="$2" port="$3" key="${existing_key:-}"
    local base="-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -p ${port}"
    if [ -n "$key" ]; then
        ssh $base -o BatchMode=yes -i "$key" "${user}@${host}" "echo ok" &>/dev/null && return 0
    else
        ssh $base -o BatchMode=yes "${user}@${host}" "echo ok" &>/dev/null && return 0
        key="$HOME/.ssh/jt_whisper_ed25519"
        if [ ! -f "$key" ]; then
            mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
            ssh-keygen -t ed25519 -f "$key" -N "" -q -C "jt-whisper-auto" || return 1
        fi
        if ssh $base -o BatchMode=yes -i "$key" "${user}@${host}" "echo ok" &>/dev/null; then
            _rw_save_key "$key"
            return 0
        fi
    fi
    if [ ! -t 0 ] && ! { : </dev/tty; } 2>/dev/null; then
        echo -e "  ${C_WARN}[略過]${NC} 登入 ${user}@${host} 要密碼，這次沒有人可以輸入"
        return 1
    fi
    [ -f "${key}.pub" ] || { check_fail "找不到公鑰 ${key}.pub"; return 1; }
    echo -e "  ${C_WHITE}這台電腦登入 ${user}@${host} 需要密碼：輸入一次（輸入時畫面不會顯示），會把這台的金鑰加到伺服器，之後就不用再輸入${NC}"
    if ssh $base "${user}@${host}" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" < "${key}.pub"; then
        _rw_save_key "$key"
        check_ok "已把這台的金鑰加到伺服器，之後免密碼"
        return 0
    fi
    check_fail "登入 ${user}@${host} 失敗"
    return 1
}

# 金鑰記進 config.json 的 remote_whisper.ssh_key（之後的檢查、更新都用它）
_rw_save_key() {
    existing_key="$1"
    "$_PY" -c "
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding='utf-8'))
c.setdefault('remote_whisper', {})['ssh_key'] = sys.argv[2]
json.dump(c, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$SCRIPT_DIR/config.json" "$1" 2>/dev/null || true
}

check_tts_local_mac() {
    [ "$(uname -m)" = "arm64" ] || return 0
    local mem_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
    [ "$mem_gb" -ge 16 ] || return 0
    source "$VENV_DIR/bin/activate" || return 0
    if python3 -c "$_TTS_MLX_CHECK" &>/dev/null && [ -f "$SCRIPT_DIR/tts_data/moe_words.tsv" ] \
        && python3 -c "from huggingface_hub import snapshot_download as d; d('$_TTS_MLX_MODEL', revision='$_TTS_MLX_REV', local_files_only=True, allow_patterns=$_TTS_MLX_FILES)" &>/dev/null; then
        section "文字轉語音（本機合成，Apple Silicon）"
        check_ok "本機文字轉語音（已安裝）"
        deactivate
        return 0
    fi
    local tty_in
    if [ -t 0 ]; then
        tty_in=/dev/stdin
    elif { : </dev/tty; } 2>/dev/null; then
        tty_in=/dev/tty
    else
        deactivate
        return 0
    fi
    section "文字轉語音（本機合成，Apple Silicon）"
    echo -e "  ${C_WHITE}把文字、逐字稿、摘要念成台灣華語。有設定文字轉語音的 GPU 伺服器時用伺服器，這台不必裝${NC}"
    echo -e "  ${C_DIM}  本機合成：約 4 GB（模型 3.2 GB、台灣念法資源 0.6 GB），速度約與念出來一樣快${NC}"
    local ans=""
    # 預設「是」（2026-10-09 使用者：不要讓使用者還要做很多前置作業；以前預設否，按 Enter 就跳過，WebUI 的本機一直不能選）
    if ! read -r -p "  是否在這台 Mac 啟用本機文字轉語音？(Y/n) " ans < "$tty_in"; then echo; deactivate; return 0; fi
    case "$ans" in
        [Nn]*) echo -e "  ${C_DIM}跳過（之後要用：重新執行 ./install.sh，問到這一題時按 Enter）${NC}"; deactivate; return 0 ;;
    esac
    # requests：g2pw 有 import 卻沒宣告相依（2026-10-09 Mac 實測）
    local pkgs="g2pw==0.1.1 pypinyin==0.55.0 opencc-python-reimplemented==0.1.7 requests"
    python3 -c 'import importlib.util as u, os, sys
s = u.find_spec("mlx_audio")
sys.exit(0 if s and any(os.path.isdir(os.path.join(p, "tts", "models", "voxcpm2")) for p in (s.submodule_search_locations or [])) else 1)' &>/dev/null \
        || pkgs="$pkgs mlx-audio>=0.5.6"
    # g2pw 會 import torch（只用 DataLoader）：產品 venv 通常已經有（講者辨識用），沒有才裝（2026-10-09 乾淨 venv 實測）
    python3 -c "import torch" &>/dev/null || pkgs="$pkgs torch"
    if run_spinner "安裝文字轉語音套件..." pip install --disable-pip-version-check $pkgs \
            && python3 -c "$_TTS_MLX_CHECK" &>/dev/null; then
        echo ""
        check_ok "文字轉語音套件"
    else
        echo ""
        check_notice "文字轉語音套件安裝失敗：朗讀只能透過 GPU 伺服器，其他功能不受影響"
        deactivate
        return 0
    fi
    if python3 "$SCRIPT_DIR/remote_whisper_server.py" --tts-fetch-data "$SCRIPT_DIR/tts_data"; then
        check_ok "台灣念法資源（教育部辭典、g2pW）"
    else
        check_notice "台灣念法資源下載失敗：重新執行 ./install.sh 會從中斷處續傳"
    fi
    if run_spinner "下載 VoxCPM2 模型（約 3.2 GB）..." \
            python3 -c "from huggingface_hub import snapshot_download as d; d('$_TTS_MLX_MODEL', revision='$_TTS_MLX_REV', allow_patterns=$_TTS_MLX_FILES)"; then
        echo ""
        check_ok "VoxCPM2 模型"
    else
        echo ""
        check_notice "VoxCPM2 模型下載失敗：重新執行 ./install.sh"
    fi
    # 共用 MLX：確認既有功能還能載入
    python3 -c "import mlx_whisper" &>/dev/null || check_notice "mlx-whisper 無法載入，請重新執行 ./install.sh"
    deactivate
}

# ─── 升級 ────────────────────────────────────────
# ─── macOS ScreenCaptureKit 系統音訊 helper ──────────────
# 取代 BlackHole + 多重輸出裝置：直接向 macOS 借系統播放音訊，
# 使用者不必改變輸出裝置，也不需安裝音訊驅動。需要 macOS 13+ 與「螢幕錄製」權限。
_sck_macos_ok() {
    [ "$(uname)" = "Darwin" ] || return 1
    local major
    major=$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)
    [ -n "$major" ] && [ "$major" -ge 13 ]
}

build_sck_helper() {
    _sck_macos_ok || return 0
    [ -f "$SCRIPT_DIR/sck_audio_capture.swift" ] || return 0
    command -v swiftc >/dev/null 2>&1 || return 1

    local bin="$SCRIPT_DIR/bin/jt-sck-audio"
    local stamp="$SCRIPT_DIR/bin/.jt-sck-audio.hash"
    local src_hash
    src_hash=$(shasum -a 256 "$SCRIPT_DIR/sck_audio_capture.swift" 2>/dev/null | cut -c1-16)

    # 原始碼未變更且已編譯過 → 略過（編譯約需 1 分鐘）
    if [ -f "$bin" ] && [ -f "$stamp" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$src_hash" ]; then
        return 0
    fi

    mkdir -p "$SCRIPT_DIR/bin"
    SCK_BUILD_LOG="$SCRIPT_DIR/logs/sck_build.log"
    mkdir -p "$SCRIPT_DIR/logs"
    if swiftc -O -target "$(uname -m)-apple-macos13.0" \
            -o "$bin" "$SCRIPT_DIR/sck_audio_capture.swift" \
            -framework ScreenCaptureKit -framework AVFoundation \
            -framework CoreMedia -framework CoreGraphics > "$SCK_BUILD_LOG" 2>&1; then
        echo "$src_hash" > "$stamp"
        return 0
    fi
    rm -f "$bin" "$stamp"
    return 1
}

check_sck() {
    section "系統音訊擷取（ScreenCaptureKit）"
    if ! _sck_macos_ok; then
        check_notice "macOS 12 以下不支援 ScreenCaptureKit，將使用 BlackHole 擷取系統音訊"
        return 0
    fi
    if ! command -v swiftc >/dev/null 2>&1; then
        check_fail "找不到 swiftc（需要 Xcode Command Line Tools），系統音訊改用 BlackHole"
        echo -e "  ${C_DIM}  執行 xcode-select --install 後重跑本安裝程式即可改用 ScreenCaptureKit${NC}"
        return 0
    fi

    local bin="$SCRIPT_DIR/bin/jt-sck-audio"
    # 這個元件是選配（失敗可退回 BlackHole），編譯失敗不可讓整個安裝中止（本檔有 set -e）
    if [ -f "$bin" ] && [ -f "$SCRIPT_DIR/bin/.jt-sck-audio.hash" ]; then
        run_spinner "檢查 ScreenCaptureKit 元件" build_sck_helper || true
    else
        echo -e "  ${C_DIM}編譯 ScreenCaptureKit 音訊元件（約 1 分鐘）...${NC}"
        run_spinner "編譯 ScreenCaptureKit 元件" build_sck_helper || true
    fi
    if [ ! -f "$bin" ]; then
        check_fail "ScreenCaptureKit 元件編譯失敗，改用 BlackHole 擷取系統音訊"
        # 常見原因：Xcode Command Line Tools 的 SDK 比 swiftc 新（macOS 升級後只更新了 SDK）
        if grep -q "this SDK is not supported by the compiler" "$SCRIPT_DIR/logs/sck_build.log" 2>/dev/null; then
            echo -e "  ${C_WARN}  原因：Xcode Command Line Tools 的 SDK 與 swiftc 版本不符${NC}"
            echo -e "  ${C_DIM}  請更新命令列工具後重跑安裝：${NC}"
            echo -e "  ${C_DIM}    sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install${NC}"
            echo -e "  ${C_DIM}  （已安裝 Xcode 的話：sudo xcode-select -s /Applications/Xcode.app）${NC}"
        else
            echo -e "  ${C_DIM}  錯誤訊息：$SCRIPT_DIR/logs/sck_build.log${NC}"
        fi
        echo -e "  ${C_DIM}  安裝會繼續，系統音訊改用 BlackHole：brew install --cask blackhole-2ch${NC}"
        return 0
    fi
    check_ok "ScreenCaptureKit 元件已就緒"

    # 權限狀態（只取音訊也需要「螢幕錄製」權限）
    local perm
    perm=$("$bin" --check 2>/dev/null | grep -o '"permission":[a-z]*' | cut -d: -f2)
    if [ "$perm" = "true" ]; then
        check_ok "已取得「螢幕錄製」權限，可直接擷取系統音訊"
        echo -e "  ${C_DIM}不需要 BlackHole，也不必建立多重輸出裝置${NC}"
    else
        check_notice "尚未取得「螢幕錄製」權限"
        echo -e "  ${C_WHITE}ScreenCaptureKit 只擷取音訊、不會擷取畫面，但 macOS 將其歸在此權限之下${NC}"
        echo -e "  ${C_DIM}授權方式：執行 ./start.sh --sck-permission，或到${NC}"
        echo -e "  ${C_DIM}「系統設定 → 隱私權與安全性 → 螢幕錄製」勾選你的終端機程式${NC}"
        echo -e "  ${C_DIM}授權後需重新啟動終端機程式。未授權時會自動改用 BlackHole${NC}"
    fi
    return 0
}

# 升級時要更新的檔案清單（補檔與升級共用同一份，避免兩邊漂掉而漏檔）
# README.md 與 CHANGELOG.md 也要更新，否則升級後看不到改了什麼、版本號還停在舊版
# jtdt_meeting/ 是第一個放在子資料夾的（v2.25.0，會議摘要）：複製時要先建資料夾
# jtlw_api/（REST API）v2.25.1 起公開：只放程式與介面規格，測試（test_*.py）跟 tools/ 一樣不發佈
_UPGRADE_FILES="translate_meeting.py start.sh start.ps1 install.sh install.ps1 install-linux.sh \
SOP.md README.md CHANGELOG.md BENCHMARKS.md COMPLIANCE.md webui.py webui.html subtitle_overlay.py sck_audio_capture.swift \
jtlw_tls.py remote_whisper_server.py \
jtdt_meeting/__init__.py jtdt_meeting/meeting_insight.py jtdt_meeting/meeting_charts.py \
jtdt_meeting/transcript_parse.py jtdt_meeting/zip_guard.py \
jtlw_api/__init__.py jtlw_api/__main__.py jtlw_api/app.py jtlw_api/config.py jtlw_api/engine.py \
jtlw_api/events.py jtlw_api/keys.py jtlw_api/log.py jtlw_api/store.py jtlw_api/tls.py \
jtlw_api/schemas/jtlw-api-v1.schema.json \
jtlw_tts/__init__.py jtlw_tts/__main__.py jtlw_tts/engine.py jtlw_tts/tw_reading.py jtlw_tts/interp.py jtlw_tts/vmic.py \
jtlw_tts/voices/b00000000001/voice.json jtlw_tts/voices/b00000000001/ref.wav \
jtlw_tts/voices/b00000000002/voice.json jtlw_tts/voices/b00000000002/ref.wav \
jtlw_tts/voices/b00000000003/voice.json jtlw_tts/voices/b00000000003/ref.wav \
jtlw_tts/voices/b00000000004/voice.json jtlw_tts/voices/b00000000004/ref.wav \
jtlw_tts/voices/b00000000005/voice.json jtlw_tts/voices/b00000000005/ref.wav \
jtlw_tts/voices/b00000000006/voice.json jtlw_tts/voices/b00000000006/ref.wav \
jtlw_tts/voices/b00000000007/voice.json jtlw_tts/voices/b00000000007/ref.wav \
jtlw_tts/voices/b00000000008/voice.json jtlw_tts/voices/b00000000008/ref.wav \
jtlw_tts/voices/b00000000009/voice.json jtlw_tts/voices/b00000000009/ref.wav \
jtlw_tts/voices/b00000000010/voice.json jtlw_tts/voices/b00000000010/ref.wav \
icons/jt-live-whisper.png icons/jt-live-whisper.ico icons/jt-live-whisper.icns"
# jtlw_tts/（v2.27.0）：文字轉語音。新加的子資料夾：第一次 --upgrade 跑舊腳本、拿不到，第二次才會到
# icons/（v2.26.2）：捷徑的 logo 圖示，由 tools/build_icons.py 照網站 favicon 產生

# ─── GPU 伺服器 server.py 的啟停與版本比較 ──────────────────────
# 這三支是 2026-09-23 補的。先前 install.sh / install.ps1 各自inline 一份，
# 而且都踩了同樣兩個坑（自殺式 pkill、殺完不啟動）。

# 停掉遠端的 server.py。
# **絕對不可以用 `pkill -f 'server.py --port N'`**：執行這條指令的遠端 shell
# 自己的命令列也含有那串字，pkill 會把自己一起殺掉。`[s]` 打斷自我比對。
_rw_stop() {    # $1=ssh_opts  $2=user@host  $3=port
    ssh $1 "$2" "kill \$(ps aux | awk '/[s]erver\.py --port $3/ {print \$2}') 2>/dev/null; sleep 0.5" &>/dev/null || true
}

# 啟動遠端 server.py。
# **setsid 與 `< /dev/null` 是必要的**，否則 ssh 連線結束時服務會被 SIGHUP 帶走。
# **整段還要包在子殼裡、子殼自己也重導**（`( ... & ) >/dev/null 2>&1`）：
# 只重導背景那個指令不夠，子殼仍握著 ssh 的 stdout/stderr，ssh 會一直等不到 EOF。
# 2026-09-23 實測：不包子殼時 ssh 掛滿 35 秒才逾時（服務其實已經起來了），
# 包了之後 1 秒返回。`translate_meeting.py` 先前是用 timeout=30 + except 吞掉的。
#
# 有裝 systemd 單元（見 _rw_install_unit）時改走 systemctl：由 systemd 帶起來的
# 行程才會在主機重開後、或程式崩潰後自動回來。
_rw_start() {   # $1=ssh_opts  $2=user@host  $3=port
    ssh $1 "$2" "if [ \$(id -u) = 0 ] && systemctl is-enabled --quiet jt-whisper-server@$3 2>/dev/null; then systemctl restart jt-whisper-server@$3; else ( cd ~/jt-whisper-server && export LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH && nohup setsid venv/bin/python3 server.py --port $3 > /tmp/jt-whisper-server.log 2>&1 < /dev/null & ) >/dev/null 2>&1; fi" &>/dev/null || true
}

# 在 GPU 伺服器裝 systemd 範本單元 jt-whisper-server@<port>，開機自動啟動。
# 2026-09-17 發現服務停了 29 天：主機重開過，而先前服務只靠 ssh + nohup 拉起來，
# 重開之後沒有任何東西會去啟動它。
#
# Restart=on-failure 而**不是 always**：舊版用戶端的「重啟」是先送 SIGTERM
# 再自己用 nohup 啟動；SIGTERM 對 systemd 算正常結束、不會補啟動，兩邊才不會
# 搶同一個 port。崩潰（非零結束、SIGSEGV 等）仍會自動重啟。
# 伺服器的自動更新用 os.execv 換掉自己，PID 不變，systemd 看不出差別。
#
# 非 root 或沒有 systemd 時什麼都不做（回傳 1），沿用 nohup。
# 輸出一個字：ENABLED / NOROOT / NOSYSTEMD
_RW_UNIT_SCRIPT='set -e
PORT="$1"
[ "$(id -u)" = 0 ] || { echo NOROOT; exit 0; }
{ command -v systemctl >/dev/null && [ -d /run/systemd/system ]; } || { echo NOSYSTEMD; exit 0; }
D="$HOME/jt-whisper-server"
cat > /etc/systemd/system/jt-whisper-server@.service <<UNIT
[Unit]
Description=jt-live-whisper GPU ASR server (port %i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$D
Environment=LD_LIBRARY_PATH=/usr/local/lib
# JT_WHISPER_UPDATE_TOKEN 等設定放這裡（選用）
EnvironmentFile=-$D/server.env
ExecStart=$D/venv/bin/python3 server.py --port %i
Restart=on-failure
RestartSec=5
# 78＝venv 的 Python 版本與建立時不同（作業系統升級）：重啟也沒用，停下來讓 log 最後一行說明原因
RestartPreventExitStatus=78
StandardOutput=append:/tmp/jt-whisper-server.log
StandardError=inherit

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "jt-whisper-server@$PORT" >/dev/null 2>&1
echo ENABLED'

_rw_install_unit() {   # $1=ssh_opts  $2=user@host  $3=port
    local r
    r=$(printf '%s\n' "$_RW_UNIT_SCRIPT" | ssh $1 "$2" "bash -s -- $3" 2>/dev/null | tail -1)
    case "$r" in
        ENABLED)   check_ok "已設定開機自動啟動（systemd：jt-whisper-server@$3）"; return 0 ;;
        NOROOT)    echo -e "  ${C_DIM}非 root 帳號，未設定開機自動啟動（主機重開後需重新啟動服務）${NC}" ;;
        NOSYSTEMD) echo -e "  ${C_DIM}伺服器沒有 systemd，未設定開機自動啟動${NC}" ;;
        *)         echo -e "  ${C_DIM}設定開機自動啟動失敗，沿用手動啟動${NC}" ;;
    esac
    return 1
}

# 等服務起來。**要看版本號不能只看通不通**：舊進程可能還活著，
# 那樣會把「根本沒換成功」誤判成更新完成（手動操作時實際踩過）。
_rw_wait_health() {   # $1=host  $2=port  $3=秒數  [$4=期望版本]
    local i body
    for i in $(seq 1 "$3"); do
        body=$(curl -s --connect-timeout 2 "http://$1:$2/health" 2>/dev/null)
        if echo "$body" | grep -q '"ok"'; then
            [ -z "$4" ] && return 0
            echo "$body" | grep -q "\"$4\"" && return 0
        fi
        sleep 1
    done
    return 1
}

# $1 比 $2 舊嗎？空字串視為最舊（很舊的伺服器沒有 SERVER_VERSION）。
_rw_ver_lt() {
    [ "$1" = "$2" ] && return 1
    [ -z "$1" ] && return 0
    [ -z "$2" ] && return 1
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

# 文字轉語音（2026-10）：GPU 伺服器的 venv-tts。已經裝好只報告；沒裝的話**有人可以回答才問**（預設否：約 11 GB，
# 而且 GPU 伺服器多半是共用正式機），無人值守不動。回答是就在伺服器上跑 server.py --tts-setup：
# 套件、模型、台灣念法資源都在那裡處理（install.ps1 叫同一個指令，不必兩邊各寫一遍）
_rw_offer_tts() {   # $1=ssh_opts  $2=user@host
    local st
    st=$(ssh $1 "$2" "test -x ~/jt-whisper-server/venv-tts/bin/python && test -f ~/jt-whisper-server/tts/moe_words.tsv && ~/jt-whisper-server/venv-tts/bin/python -c 'import voxcpm, g2pw' >/dev/null 2>&1 && echo ready || echo missing" 2>/dev/null)
    if [ "$st" = "ready" ]; then
        check_ok "GPU 伺服器 文字轉語音已設定"
        return 0
    fi
    local tty_in
    if [ -t 0 ]; then
        tty_in=/dev/stdin
    elif { : </dev/tty; } 2>/dev/null; then
        tty_in=/dev/tty
    else
        echo -e "  ${C_DIM}文字轉語音（朗讀台灣華語）還沒設定；要用時在有終端機的地方重新執行安裝程式${NC}"
        return 0
    fi
    if ! ssh $1 "$2" "grep -q _tts_setup_main ~/jt-whisper-server/server.py" 2>/dev/null; then
        echo -e "  ${C_DIM}GPU 伺服器上的 server.py 還沒有文字轉語音（版本較舊），更新伺服器後再設定${NC}"
        return 0
    fi
    echo -e "  ${C_WHITE}文字轉語音：把文字、逐字稿、摘要念成台灣華語（VoxCPM2，在 GPU 伺服器合成）${NC}"
    echo -e "  ${C_DIM}  會在 GPU 伺服器裝約 11 GB（Python 環境 5.2 GB、模型 4.7 GB、台灣念法資源 0.6 GB；要有 20 GB 可用空間），第一次約 10～30 分鐘；辨識不受影響${NC}"
    local ans=""
    if ! read -r -p "  是否在 GPU 伺服器設定文字轉語音？(y/N) " ans < "$tty_in"; then echo; return 0; fi
    case "$ans" in
        [Yy]*) ;;
        *) echo -e "  ${C_DIM}跳過（之後要用：重新執行安裝程式）${NC}"; return 0 ;;
    esac
    if ssh $1 "$2" "cd ~/jt-whisper-server && venv/bin/python3 server.py --tts-setup"; then
        check_ok "GPU 伺服器 文字轉語音設定完成"
    else
        check_fail "GPU 伺服器 文字轉語音沒有設定完成（辨識不受影響；可再執行一次安裝程式）"
    fi
}

# BreezyVoice（2026-10-09，選用、不是預設）：台灣口音，但合成速度慢（約音訊長度的 1.2～2.5 倍），不適合即時。
# 安裝與升級都問（預設否）；沒有人可以回答就不問、也不裝。升級時回答「否」記在 config.json（remote_whisper.breezy = "no"），
# 之後升級不再問（完整重跑安裝程式照樣問）。$3=1 或 JTLW_FROM_UPGRADE=1 表示從升級來的
_rw_offer_breezy() {   # $1=ssh_opts  $2=user@host  $3=upgrade
    local upgrading="${3:-${JTLW_FROM_UPGRADE:-}}"
    local st
    st=$(ssh $1 "$2" "if test -x ~/jt-whisper-server/venv-breezy/bin/python && test -f ~/jt-whisper-server/breezyvoice/.jtlw-rev; then echo ready; elif grep -q _breezy_setup_main ~/jt-whisper-server/server.py 2>/dev/null; then echo missing; else echo old; fi" 2>/dev/null)
    case "$st" in
        ready) check_ok "GPU 伺服器 BreezyVoice 已安裝（選用的台灣口音合成模型）"; return 0 ;;
        missing|old) ;;
        *) return 0 ;;                                   # 連不上：不問
    esac
    if [ "$upgrading" = "1" ] && [ "$(_rw_cfg_get breezy)" = "no" ]; then
        return 0
    fi
    local tty_in
    if [ -t 0 ]; then
        tty_in=/dev/stdin
    elif { : </dev/tty; } 2>/dev/null; then
        tty_in=/dev/tty
    else
        return 0
    fi
    if [ "$st" = "old" ]; then
        echo -e "  ${C_DIM}BreezyVoice（選用的台灣口音合成模型）：GPU 伺服器上的程式較舊，執行 ./install.sh 更新伺服器後就能加裝${NC}"
        return 0
    fi
    echo -e "  ${C_WHITE}BreezyVoice（MediaTek，選用）：台灣口音，但合成速度慢（約音訊長度的 1.2～2.5 倍），不適合即時；預設仍用 VoxCPM2${NC}"
    echo -e "  ${C_DIM}  會在 GPU 伺服器裝約 8 GB（Python 環境 5.5 GB、模型 2.2 GB；要有 16 GB 可用空間），第一次約 10～20 分鐘；辨識不受影響${NC}"
    local ans=""
    if ! read -r -p "  是否加裝 BreezyVoice？(y/N) " ans < "$tty_in"; then echo; return 0; fi
    case "$ans" in
        [Yy]*) ;;
        *)
            if [ "$upgrading" = "1" ]; then
                _rw_cfg_set breezy no
                echo -e "  ${C_DIM}跳過（升級時不再問；之後要裝：執行 ./install.sh）${NC}"
            else
                echo -e "  ${C_DIM}跳過（之後要裝：重新執行安裝程式）${NC}"
            fi
            return 0 ;;
    esac
    if ssh $1 "$2" "cd ~/jt-whisper-server && venv/bin/python3 server.py --breezy-setup"; then
        _rw_cfg_set breezy ""
        check_ok "GPU 伺服器 BreezyVoice 安裝完成（WebUI「合成模型」選 BreezyVoice）"
    else
        check_fail "GPU 伺服器 BreezyVoice 沒有安裝完成（其他功能不受影響；可再執行一次安裝程式）"
    fi
}

# config.json 的 remote_whisper.<鍵>：讀（沒有回空字串）／寫（空字串＝刪掉）
_rw_cfg_get() {
    "${_PY:-python3}" -c "
import json, sys
try:
    print((json.load(open(sys.argv[1], encoding='utf-8')).get('remote_whisper') or {}).get(sys.argv[2], ''))
except Exception:
    print('')
" "$SCRIPT_DIR/config.json" "$1" 2>/dev/null
}

_rw_cfg_set() {
    "${_PY:-python3}" -c "
import json, os, sys
p = sys.argv[1]
c = json.load(open(p, encoding='utf-8'))
rw = c.setdefault('remote_whisper', {})
if sys.argv[3]:
    rw[sys.argv[2]] = sys.argv[3]
else:
    rw.pop(sys.argv[2], None)
tmp = p + '.tmp'
json.dump(c, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
os.replace(tmp, p)
" "$SCRIPT_DIR/config.json" "$1" "$2" 2>/dev/null || true
}

# 升級（macOS；Linux 升級後會重跑完整安裝流程、在那裡問）：有設定 GPU 伺服器、而且不必輸入密碼就連得上時，問要不要加裝 BreezyVoice
offer_breezy_on_upgrade() {
    [ "$(uname -s)" = "Linux" ] && return 0              # Linux 升級後會重跑完整安裝流程（先同步伺服器程式），在那裡問
    [ -n "${JTLW_UPGRADE_QUIET:-}" ] && return 0         # start.sh 啟動時自動補檔：不問、不連 GPU 伺服器
    [ -f "$SCRIPT_DIR/config.json" ] || return 0
    local host user port key opts
    host=$(_rw_cfg_get host)
    [ -n "$host" ] || return 0
    user=$(_rw_cfg_get ssh_user); user=${user:-root}
    port=$(_rw_cfg_get ssh_port); port=${port:-22}
    key=$(_rw_cfg_get ssh_key)
    opts="-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -p $port"
    if [ -n "$key" ] && [ -f "$key" ]; then opts="$opts -i $key"; fi
    _rw_offer_breezy "$opts" "${user}@${host}" 1
}

do_upgrade() {
    section "從 GitHub 升級程式"

    # 升級一律用「新版」的清單（2026-10-10）：以前跑的是舊版安裝程式、只複製舊版清單上的檔案，
    # 新版才加入的檔案（v2.27.0 的 jtlw_tts/ 等）要再升級一次才會到；只跑一次的人停在「新版 WebUI＋缺模組」。
    # 現在複製完、發現安裝程式本身換了，就把下載好的資料夾交給新版的安裝程式（JTLW_UPGRADE_REPO）接手補齊，
    # 不重新下載；exec 不會執行 EXIT trap，暫存資料夾由接手的那一個刪（JTLW_UPGRADE_TMP）
    local tmp_dir repo_dir handoff=""
    if [ -n "${JTLW_UPGRADE_REPO:-}" ] && [ -f "$JTLW_UPGRADE_REPO/translate_meeting.py" ]; then
        repo_dir="$JTLW_UPGRADE_REPO"
        tmp_dir="${JTLW_UPGRADE_TMP:-}"
        [ -n "$tmp_dir" ] && trap "rm -rf '$tmp_dir'" EXIT
        handoff=1
        unset JTLW_UPGRADE_REPO JTLW_UPGRADE_TMP
        echo -e "  ${C_DIM}由新版安裝程式接手，補齊新版才加入的檔案...${NC}"
    else
        tmp_dir=$(mktemp -d)
        trap "rm -rf '$tmp_dir'" EXIT
        echo -e "  ${C_DIM}正在從 GitHub 下載最新版本...${NC}"
        local zip_path="$tmp_dir/jt-live-whisper.zip"
        if ! curl -fsSL "https://github.com/jasoncheng7115/jt-live-whisper/archive/refs/heads/main.zip" -o "$zip_path"; then
            check_fail "無法連接 GitHub，請檢查網路連線"
            return 1
        fi
        unzip -q "$zip_path" -d "$tmp_dir"
        repo_dir="$tmp_dir/jt-live-whisper-main"
    fi

    if [ ! -f "$repo_dir/translate_meeting.py" ]; then
        check_fail "下載的檔案不完整，請檢查網路連線"
        return 1
    fi

    # 取得伺服器版本號
    local remote_version
    remote_version=$(grep -m1 'APP_VERSION' "$repo_dir/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
    local local_version
    local_version=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')

    if [ -z "$handoff" ]; then
        echo -e "  ${C_WHITE}目前版本: v${local_version:-未知}${NC}"
        echo -e "  ${C_WHITE}最新版本: v${remote_version:-未知}${NC}"
    fi

    if [ "$local_version" = "$remote_version" ]; then
        # 版本相同時，逐檔比對內容而不是只看檔案在不在。
        # 只檢查「存在與否」會漏掉「檔案在、但內容是舊的」——例如某一版的升級清單
        # 漏了 README.md / CHANGELOG.md，之後再升級也永遠補不回來（v2.20.2 實機踩到）。
        _stale=""
        for _uf in $_UPGRADE_FILES; do
            [ -f "$repo_dir/$_uf" ] || continue
            if [ ! -f "$SCRIPT_DIR/$_uf" ] || ! cmp -s "$repo_dir/$_uf" "$SCRIPT_DIR/$_uf"; then
                _stale="$_stale $_uf"
            fi
        done
        if [ -n "$_stale" ]; then
            [ -z "$handoff" ] && echo -e "  ${C_WARN}版本相同但有檔案與最新版不符，更新中...${NC}"
            for _uf in $_stale; do
                mkdir -p "$(dirname "$SCRIPT_DIR/$_uf")"
                cp "$repo_dir/$_uf" "$SCRIPT_DIR/$_uf"
            done
            chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/install.sh" 2>/dev/null
            chmod +x "$SCRIPT_DIR/install-linux.sh" 2>/dev/null || true
            build_sck_helper
            if [ -n "$handoff" ]; then
                check_ok "已補上新版才加入的檔案（${_stale}）"
            else
                check_ok "已更新與最新版不符的檔案（${_stale}）"
            fi
        elif [ -z "$handoff" ]; then
            check_ok "已經是最新版本 (v${local_version})"
        fi
        if [ -n "$handoff" ] && [ "$(uname -s)" != "Linux" ]; then
            echo ""
            echo -e "  ${C_WARN}建議重新執行 ./install.sh 確認相依套件完整${NC}"
        fi
        offer_desktop_shortcut
        offer_breezy_on_upgrade
        return 0
    fi

    # 比較版本號：若伺服器比本地舊，不蓋過（開發機本地可能比 GitHub 新）
    _ver_gt() {
        # 回傳 0 表示 $1 > $2（版本號比較）
        [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ] && [ "$1" != "$2" ]
    }
    if [ -n "$local_version" ] && [ -n "$remote_version" ] && _ver_gt "$local_version" "$remote_version"; then
        echo -e "  ${C_WARN}[跳過]${NC} 本地版本 (v${local_version}) 比 GitHub (v${remote_version}) 還新，不覆蓋"
        return 0
    fi

    # 安裝程式本身有沒有換（換了就交給新版接手：新版的清單可能多了檔案）
    local installer_changed=""
    cmp -s "$repo_dir/install.sh" "$SCRIPT_DIR/install.sh" || installer_changed=1

    # 更新主要程式檔案
    local files_updated=0
    for fname in $_UPGRADE_FILES; do
        if [ -f "$repo_dir/$fname" ]; then
            mkdir -p "$(dirname "$SCRIPT_DIR/$fname")"
            cp "$repo_dir/$fname" "$SCRIPT_DIR/$fname"
            ((files_updated++)) || true
        fi
    done

    # 確保腳本可執行
    chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/install.sh" 2>/dev/null
    chmod +x "$SCRIPT_DIR/install-linux.sh" 2>/dev/null || true

    # ScreenCaptureKit helper 原始碼可能一併更新，重新編譯
    build_sck_helper

    check_ok "已升級 v${local_version} → v${remote_version}（更新 ${files_updated} 個檔案）"
    if [ -n "$installer_changed" ] && [ -z "$handoff" ] && [ -f "$SCRIPT_DIR/install.sh" ]; then
        # 交給新版安裝程式：用它的清單補齊、問它新增的問題。JTLW_UPGRADE_FROM 讓 Linux 照樣知道版本換了（要重啟服務）
        export JTLW_UPGRADE_REPO="$repo_dir" JTLW_UPGRADE_TMP="$tmp_dir" JTLW_UPGRADE_FROM="$local_version"
        trap - EXIT
        exec bash "$SCRIPT_DIR/install.sh" --upgrade
    fi
    echo ""
    echo -e "  ${C_WARN}建議重新執行 ./install.sh 確認相依套件完整${NC}"
    offer_desktop_shortcut
    offer_breezy_on_upgrade
    return 0
}

# 清單上缺了哪些檔（空白分隔；沒缺就是空字串）
_upgrade_missing_files() {
    local _f
    for _f in $_UPGRADE_FILES; do
        [ -e "$SCRIPT_DIR/$_f" ] || printf '%s ' "$_f"
    done
}

# 完整安裝一開始先補齊上次升級漏掉的檔案（2026-10-10）：v2.28.0 以前的安裝程式升級時只複製它自己清單上的檔案，
# Linux 接著執行「新版」的 install-linux.sh 檢查相依套件——新版才加入的檔案就在這裡補上。
# 從 git 或壓縮檔全新安裝時不會缺，什麼都不做
_complete_upgrade_files() {
    local miss
    miss=$(_upgrade_missing_files)
    [ -n "$miss" ] || return 0
    echo ""
    echo -e "  ${C_WARN}上次升級沒有完成：缺 $(echo $miss | wc -w | tr -d ' ') 個新版才加入的檔案，先補齊${NC}"
    JTLW_UPGRADE_QUIET=1 do_upgrade || true
    miss=$(_upgrade_missing_files)
    if [ -n "$miss" ]; then
        check_notice "還缺 $(echo $miss | wc -w | tr -d ' ') 個檔案（連不到 GitHub？），連上網路後執行 ./install.sh --upgrade"
    fi
    return 0
}

# ─── 從原始碼編譯 CTranslate2（aarch64 CUDA）──────────────
# 用法：_build_ctranslate2_from_source "$ssh_opts" "$rw_user" "$rw_host" [venv 目錄] [wheel 快取目錄] [安裝前綴]
# 安裝前綴預設 /usr/local（GPU 伺服器部署）；指定其他目錄時不跑 ldconfig，執行時靠 LD_LIBRARY_PATH 載入，
# 不會覆蓋系統上其他程式正在使用的 libctranslate2
# 後兩個參數省略時沿用 GPU 伺服器的路徑；install-linux.sh 在本機編譯時會指定
# 回傳：0=成功  1=失敗（呼叫端應降級 openai-whisper）
_build_ctranslate2_from_source() {
    local ssh_opts="$1" rw_user="$2" rw_host="$3"
    local _venv="${4:-~/jt-whisper-server/venv}"
    local REMOTE_PIP="${_venv}/bin/pip"
    local REMOTE_PY="${_venv}/bin/python3"
    local WHEEL_CACHE="${5:-~/jt-whisper-server/.ct2-wheels}"
    local CT2_PREFIX="${6:-/usr/local}"
    local BUILD_DIR="/tmp/ctranslate2-build"
    [ "$CT2_PREFIX" != "/usr/local" ] && BUILD_DIR="/tmp/ctranslate2-build-$(id -un)-$$"

    echo ""
    echo -e "  ${C_WHITE}[CTranslate2] aarch64 偵測到，嘗試從原始碼編譯 CUDA 版...${NC}"

    # ── 1. 檢查快取 wheel ──
    local cached_whl
    cached_whl=$(ssh $ssh_opts "$rw_user@$rw_host" "ls ${WHEEL_CACHE}/ctranslate2-*.whl 2>/dev/null | head -1" 2>/dev/null)
    if [ -n "$cached_whl" ]; then
        echo -e "  ${C_OK}[快取] 找到已編譯 wheel: $(basename "$cached_whl")${NC}"
        if run_spinner "  安裝快取 wheel..." ssh $ssh_opts "$rw_user@$rw_host" "
            ${REMOTE_PIP} install --disable-pip-version-check --force-reinstall '$cached_whl' 2>&1
        "; then
            echo ""
            # 驗證
            local ct2_cuda
            ct2_cuda=$(ssh $ssh_opts "$rw_user@$rw_host" "LD_LIBRARY_PATH=${CT2_PREFIX}/lib:\$LD_LIBRARY_PATH ${REMOTE_PY} -c \"
import ctranslate2
types = ctranslate2.get_supported_compute_types('cuda')
print('ok' if types else 'no')
\"" 2>/dev/null)
            if [ "$ct2_cuda" = "ok" ]; then
                check_ok "CTranslate2 CUDA 驗證通過（快取 wheel）"
                # 重裝 faster-whisper 確保版本相容
                ssh $ssh_opts "$rw_user@$rw_host" "${REMOTE_PIP} install --disable-pip-version-check --force-reinstall --no-deps faster-whisper" &>/dev/null
                return 0
            fi
            echo -e "  ${C_WARN}[警告] 快取 wheel CUDA 驗證失敗，重新編譯${NC}"
        else
            echo ""
            echo -e "  ${C_WARN}[警告] 快取 wheel 安裝失敗，重新編譯${NC}"
        fi
    fi

    # ── 2. 檢查前提條件 ──
    # nvcc（必要）— 檢查 PATH 和常見 CUDA 安裝路徑
    local nvcc_path
    nvcc_path=$(ssh $ssh_opts "$rw_user@$rw_host" "
        if command -v nvcc &>/dev/null; then
            command -v nvcc
        elif [ -x /usr/local/cuda/bin/nvcc ]; then
            echo /usr/local/cuda/bin/nvcc
        elif ls /usr/local/cuda-*/bin/nvcc 2>/dev/null | head -1; then
            true
        else
            echo ''
        fi
    " 2>/dev/null)
    if [ -z "$nvcc_path" ]; then
        echo -e "  ${C_WARN}[跳過] nvcc 未安裝（需要 CUDA Toolkit），無法編譯 CTranslate2${NC}"
        return 1
    fi
    # 確保 nvcc 所在目錄加入 PATH（後續 cmake 需要）
    local cuda_bin_dir
    cuda_bin_dir=$(dirname "$nvcc_path")
    echo -e "  ${C_DIM}  nvcc: ${nvcc_path}${NC}"

    # 編譯所需工具與函式庫（一次檢查、一次安裝）
    local need_build_apt=""
    # cmake: 建構系統、git: 下載原始碼、g++: C++ 編譯器
    # python3-dev: Python.h（bdist_wheel 需要）
    # libopenblas-dev: aarch64 替代 Intel MKL 的 BLAS 函式庫
    local build_tools="cmake git g++ make"
    local build_libs="python3-dev libopenblas-dev"
    for tool in $build_tools; do
        if ! ssh $ssh_opts "$rw_user@$rw_host" "export PATH=${cuda_bin_dir}:\$PATH && command -v $tool" &>/dev/null; then
            case "$tool" in
                g++) need_build_apt="$need_build_apt g++ build-essential" ;;
                *)   need_build_apt="$need_build_apt $tool" ;;
            esac
        fi
    done
    for pkg in $build_libs; do
        if ! ssh $ssh_opts "$rw_user@$rw_host" "dpkg -s $pkg" &>/dev/null 2>&1; then
            need_build_apt="$need_build_apt $pkg"
        fi
    done
    if [ -n "$need_build_apt" ]; then
        check_install "安裝編譯工具:${need_build_apt}"
        if ! run_spinner "  安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "apt update -qq && apt install -y -qq $need_build_apt 2>&1"; then
            echo ""
            echo -e "    ${C_WARN}[跳過] 無法安裝編譯工具，無法編譯 CTranslate2${NC}"
            return 1
        fi
        echo ""
    fi
    check_ok "編譯工具就緒"

    # cuDNN（可選，影響效能但非必要）
    local has_cudnn
    has_cudnn=$(ssh $ssh_opts "$rw_user@$rw_host" "ldconfig -p 2>/dev/null | grep -c libcudnn" 2>/dev/null)
    local cudnn_flag="OFF"
    if [ "$has_cudnn" -gt 0 ] 2>/dev/null; then
        cudnn_flag="ON"
        echo -e "  ${C_DIM}  cuDNN 偵測到，將啟用 cuDNN 加速${NC}"
    else
        echo -e "  ${C_DIM}  cuDNN 未偵測到（可選，不影響編譯）${NC}"
    fi

    # 磁碟空間（需要 >= 3GB）
    local avail_mb
    avail_mb=$(ssh $ssh_opts "$rw_user@$rw_host" "df -m /tmp | awk 'NR==2{print \$4}'" 2>/dev/null)
    if [ -n "$avail_mb" ] && [ "$avail_mb" -lt 3000 ] 2>/dev/null; then
        echo -e "  ${C_WARN}[跳過] /tmp 磁碟空間不足（${avail_mb}MB < 3GB），無法編譯${NC}"
        return 1
    fi

    # ── 3. 偵測 GPU 架構 ──
    local gpu_arch
    gpu_arch=$(ssh $ssh_opts "$rw_user@$rw_host" "nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' '" 2>/dev/null)
    if [ -z "$gpu_arch" ]; then
        echo -e "  ${C_WARN}[跳過] 無法偵測 GPU 架構${NC}"
        return 1
    fi
    echo -e "  ${C_DIM}  GPU 架構: sm_${gpu_arch//.}（compute capability ${gpu_arch}）${NC}"

    # ── 4. 編譯（分步驟顯示進度）──
    # gpu_arch="12.1" → cmake_arch="121"（移除小數點）
    local cmake_arch="${gpu_arch//.}"
    # 所有編譯步驟共用的環境變數開頭（確保 nvcc 在 PATH、libctranslate2 可被找到）
    local CUDA_ENV="export PATH=${cuda_bin_dir}:\$PATH && export LD_LIBRARY_PATH=${CT2_PREFIX}/lib:\$LD_LIBRARY_PATH && export CTRANSLATE2_ROOT=${CT2_PREFIX}"
    echo -e "  ${C_WHITE}  開始編譯 CTranslate2（預計 10-20 分鐘）...${NC}"

    # 清理舊的暫存目錄
    ssh $ssh_opts "$rw_user@$rw_host" "rm -rf ${BUILD_DIR} && mkdir -p ${BUILD_DIR}" &>/dev/null

    # _build_fail: 統一的失敗處理（顯示錯誤 + 清理）
    _build_fail() {
        echo ""
        echo -e "    ${C_WARN}[失敗] $1${NC}"
        # 顯示最後幾行錯誤輸出幫助排查
        if [ -f "$SPINNER_OUTPUT" ]; then
            local err_lines
            err_lines=$(grep -i -E 'error|fatal|fail|not found|no such' "$SPINNER_OUTPUT" 2>/dev/null | tail -5)
            if [ -n "$err_lines" ]; then
                echo -e "    ${C_DIM}錯誤訊息:${NC}"
                echo "$err_lines" | while IFS= read -r line; do
                    echo -e "    ${C_DIM}  $line${NC}"
                done
            fi
        fi
        ssh $ssh_opts "$rw_user@$rw_host" "rm -rf ${BUILD_DIR}" &>/dev/null
    }

    # 4a. git clone
    if ! run_spinner "  [1/7] 下載 CTranslate2 原始碼..." ssh $ssh_opts "$rw_user@$rw_host" "
        cd ${BUILD_DIR} && git clone --depth 1 --recurse-submodules https://github.com/OpenNMT/CTranslate2.git src 2>&1
    "; then
        _build_fail "git clone 失敗"
        return 1
    fi
    echo ""

    # 4b. cmake
    if ! run_spinner "  [2/7] cmake 設定（CUDA ${gpu_arch}, cuDNN=${cudnn_flag}）..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        mkdir -p ${BUILD_DIR}/src/build && cd ${BUILD_DIR}/src/build && \
        cmake .. \
            -DCMAKE_BUILD_TYPE=Release \
            -DWITH_CUDA=ON \
            -DWITH_CUDNN=${cudnn_flag} \
            -DWITH_MKL=OFF \
            -DWITH_OPENBLAS=ON \
            -DCMAKE_CUDA_ARCHITECTURES=${cmake_arch} \
            -DCUDA_NVCC_FLAGS='-gencode=arch=compute_${cmake_arch},code=sm_${cmake_arch}' \
            -DOPENMP_RUNTIME=NONE \
            -DCMAKE_INSTALL_PREFIX=${CT2_PREFIX} \
            2>&1
    "; then
        _build_fail "cmake 設定失敗"
        return 1
    fi
    echo ""

    # 4c. make（最耗時，使用全部 CPU 核心）
    local ncpu
    ncpu=$(ssh $ssh_opts "$rw_user@$rw_host" "nproc" 2>/dev/null)
    ncpu=${ncpu:-4}
    if ! run_spinner "  [3/7] 編譯 C++ 原始碼（make -j${ncpu}，此步驟最久）..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        cd ${BUILD_DIR}/src/build && make -j${ncpu} 2>&1
    "; then
        _build_fail "make 編譯失敗"
        return 1
    fi
    echo ""

    # 4d. make install + ldconfig（非系統前綴只 make install）
    local _install_cmd="make install 2>&1 && ldconfig 2>&1"
    local _install_label="安裝系統函式庫（make install + ldconfig）"
    if [ "$CT2_PREFIX" != "/usr/local" ]; then
        _install_cmd="mkdir -p ${CT2_PREFIX} && make install 2>&1"
        _install_label="安裝函式庫到 ${CT2_PREFIX}"
    fi
    if ! run_spinner "  [4/7] ${_install_label}..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        cd ${BUILD_DIR}/src/build && ${_install_cmd}
    "; then
        _build_fail "make install 失敗"
        return 1
    fi
    echo ""

    # 4e. Python wheel
    if ! run_spinner "  [5/7] 建構 Python wheel..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        cd ${BUILD_DIR}/src/python && \
        ${REMOTE_PIP} install --disable-pip-version-check setuptools wheel pybind11 2>&1 && \
        ${REMOTE_PY} setup.py bdist_wheel 2>&1
    "; then
        _build_fail "Python wheel 建構失敗"
        return 1
    fi
    echo ""

    # 4f. pip install wheel
    if ! run_spinner "  [6/7] 安裝 CTranslate2 wheel..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        whl=\$(ls ${BUILD_DIR}/src/python/dist/ctranslate2-*.whl 2>/dev/null | head -1)
        if [ -z \"\$whl\" ]; then
            echo 'ERROR: wheel 未產生'
            exit 1
        fi
        ${REMOTE_PIP} install --disable-pip-version-check --force-reinstall \"\$whl\" 2>&1
    "; then
        _build_fail "wheel 安裝失敗"
        return 1
    fi
    echo ""

    # 4g. 快取 wheel + 清理
    run_spinner "  [7/7] 快取 wheel 並清理暫存檔..." ssh $ssh_opts "$rw_user@$rw_host" "
        mkdir -p ${WHEEL_CACHE}
        cp ${BUILD_DIR}/src/python/dist/ctranslate2-*.whl ${WHEEL_CACHE}/ 2>&1
        rm -rf ${BUILD_DIR}
    "
    echo ""

    # ── 5. 驗證 CTranslate2 CUDA ──
    local ct2_verify
    ct2_verify=$(ssh $ssh_opts "$rw_user@$rw_host" "${CUDA_ENV} && ${REMOTE_PY} -c \"
import ctranslate2
types = ctranslate2.get_supported_compute_types('cuda')
print(','.join(types) if types else 'no')
\"" 2>/dev/null)
    if [ "$ct2_verify" = "no" ] || [ -z "$ct2_verify" ]; then
        echo -e "  ${C_WARN}[失敗] CTranslate2 編譯完成但 CUDA 驗證失敗${NC}"
        return 1
    fi
    check_ok "CTranslate2 CUDA 支援: ${ct2_verify}"

    # ── 6. 確認 libctranslate2.so 已註冊（非系統前綴不進 ldconfig，由 LD_LIBRARY_PATH 載入）──
    local lib_check
    lib_check=$(ssh $ssh_opts "$rw_user@$rw_host" "ldconfig -p 2>/dev/null | grep -c libctranslate2" 2>/dev/null)
    if [ "$CT2_PREFIX" != "/usr/local" ]; then
        check_ok "libctranslate2.so 位於 ${CT2_PREFIX}/lib（不影響系統上其他程式）"
    elif [ "$lib_check" -gt 0 ] 2>/dev/null; then
        check_ok "libctranslate2.so 已註冊（ldconfig）"
    else
        echo -e "  ${C_DIM}  libctranslate2.so 未在 ldconfig 中（透過 LD_LIBRARY_PATH 載入）${NC}"
    fi

    # ── 7. 重裝 faster-whisper + 驗證 CUDA 載入 ──
    run_spinner "  重新安裝 faster-whisper..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${REMOTE_PIP} install --disable-pip-version-check --force-reinstall --no-deps faster-whisper 2>&1
    "
    echo ""

    local fw_verify
    fw_verify=$(ssh $ssh_opts "$rw_user@$rw_host" "${CUDA_ENV} && ${REMOTE_PY} -c \"
from faster_whisper import WhisperModel
m = WhisperModel('tiny', device='cuda', compute_type='float16')
print('ok')
\"" 2>/dev/null)
    if [ "$fw_verify" = "ok" ]; then
        check_ok "faster-whisper CUDA 載入驗證通過"
    else
        echo -e "  ${C_WARN}[警告] faster-whisper 無法以 CUDA 載入模型${NC}"
        return 1
    fi

    return 0
}

# ─── GPU 伺服器 Whisper 伺服器（選填）──────────────
setup_remote_whisper() {
    section "GPU 伺服器 語音辨識伺服器（非必要，若未裝則用本機進行語音辨識）"

    # 使用 venv Python 讀寫 config（避免依賴系統 python3）
    local _PY="$VENV_DIR/bin/python3"

    # 檢查是否已有設定
    local existing_host existing_port existing_user existing_key existing_wport
    existing_host=$("$_PY" -c "
import json, os
p = '$SCRIPT_DIR/config.json'
if os.path.isfile(p):
    c = json.load(open(p))
    rw = c.get('remote_whisper')
    if rw: print(rw.get('host',''))
" 2>/dev/null)

    if [ -n "$existing_host" ]; then
        # 已有設定，讀取完整資訊
        existing_port=$("$_PY" -c "import json; rw=json.load(open('$SCRIPT_DIR/config.json'))['remote_whisper']; print(rw.get('ssh_port',22))" 2>/dev/null)
        existing_user=$("$_PY" -c "import json; rw=json.load(open('$SCRIPT_DIR/config.json'))['remote_whisper']; print(rw.get('ssh_user','root'))" 2>/dev/null)
        existing_key=$("$_PY" -c "import json; rw=json.load(open('$SCRIPT_DIR/config.json'))['remote_whisper']; print(rw.get('ssh_key',''))" 2>/dev/null)
        existing_wport=$("$_PY" -c "import json; rw=json.load(open('$SCRIPT_DIR/config.json'))['remote_whisper']; print(rw.get('whisper_port',8978))" 2>/dev/null)

        echo -e "  ${C_WHITE}已有伺服器設定: ${existing_user}@${existing_host}:${existing_port}${NC}"

        # 檢查 SSH key 是否存在，不存在則自動產生
        if [ -n "$existing_key" ] && [ ! -f "$existing_key" ]; then
            echo -e "  ${C_WARN}[提醒]${NC} 設定的 SSH Key 不存在: ${existing_key}"
            echo -e "  ${C_DIM}  自動產生 SSH Key...${NC}"
            mkdir -p "$(dirname "$existing_key")"
            ssh-keygen -t ed25519 -f "$existing_key" -N "" -q
            if [ -f "$existing_key" ]; then
                check_ok "SSH Key 已產生: ${existing_key}"
            else
                echo -e "  ${C_WARN}[提醒]${NC} SSH Key 產生失敗，將使用密碼認證"
                existing_key=""
            fi
        fi

        if ! _rw_ensure_key_auth "$existing_user" "$existing_host" "$existing_port"; then
            echo -e "  ${C_DIM}  略過 GPU 伺服器檢查（之後要檢查：重新執行 ./install.sh）${NC}"
            return 0
        fi

        # 組合 SSH（含 ControlMaster）
        local ctrl_sock="/tmp/jt-ssh-cm-${existing_user}@${existing_host}:${existing_port}"
        local chk_opts="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $existing_port"
        chk_opts="$chk_opts -o ControlMaster=auto -o ControlPath=$ctrl_sock -o ControlPersist=120"
        if [ -n "$existing_key" ]; then
            chk_opts="$chk_opts -i $existing_key"
        fi

        local need_repair=0
        local repair_items=""
        local gpu_info="" cuda_check="" pt_ok="" ct2_ok="" ow_ok=""

        # 背景 spinner + 輸出緩衝（SSH 檢查需時數秒）
        spinner_start "正在檢查伺服器環境"
        {
            # 1. SSH 連線
            if ssh $chk_opts "$existing_user@$existing_host" "echo ok" &>/dev/null; then
                check_ok "SSH 連線正常"

                # 2. Python3 + ffmpeg
                if ssh $chk_opts "$existing_user@$existing_host" "command -v python3" &>/dev/null; then
                    if ssh $chk_opts "$existing_user@$existing_host" "command -v ffmpeg" &>/dev/null; then
                        check_ok "Python3 + ffmpeg 就緒"
                    else
                        echo -e "  ${C_WARN}[缺少]${NC} ffmpeg 未安裝"
                        need_repair=1
                        repair_items="${repair_items} ffmpeg"
                    fi
                else
                    echo -e "  ${C_WARN}[缺少]${NC} Python3 未安裝"
                    need_repair=1
                    repair_items="${repair_items} python3"
                fi

                # 3. venv（也要是建立時的 Python 版本：伺服器作業系統升級後 --version 照樣成功，套件卻全部不見）
                if printf '%s\n' "$_VENV_CHECK_PY" | ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 -" &>/dev/null; then
                    check_ok "venv 正常"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} venv 損壞、不存在，或作業系統升級後 Python 版本與建立時不同"
                    need_repair=1
                    repair_items="${repair_items} venv"
                fi

                # 4. server.py
                if ssh $chk_opts "$existing_user@$existing_host" "test -f ~/jt-whisper-server/server.py" &>/dev/null; then
                    check_ok "server.py 存在"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} server.py 不存在"
                    need_repair=1
                    repair_items="${repair_items} server.py"
                fi

                # 5. faster-whisper 套件
                if ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 -c 'import faster_whisper'" &>/dev/null 2>&1; then
                    check_ok "faster-whisper 套件就緒"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} faster-whisper 套件缺失"
                    need_repair=1
                    repair_items="${repair_items} packages"
                fi

                # 5b. resemblyzer + spectralcluster（講者辨識）
                if ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 -c 'import resemblyzer; import spectralcluster'" &>/dev/null 2>&1; then
                    check_ok "resemblyzer + spectralcluster 就緒（講者辨識）"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} resemblyzer + spectralcluster 套件缺失（講者辨識）"
                    need_repair=1
                    repair_items="${repair_items} packages"
                fi

                # 5c. transformers 5.18（Nemotron 講者辨識，v2.26.0）。伺服器沒有它時講者辨識照舊用現行方法
                if ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 -c 'import transformers.models.nemotron3_diarization'" &>/dev/null 2>&1; then
                    check_ok "transformers 就緒（Nemotron 講者辨識）"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} transformers 5.18（Nemotron 講者辨識；沒有時照舊用現行方法）"
                    need_repair=1
                    repair_items="${repair_items} packages"
                fi

                # 6. NVIDIA GPU + CUDA
                gpu_info=$(ssh $chk_opts "$existing_user@$existing_host" "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1" 2>/dev/null)
                if [ -n "$gpu_info" ]; then
                    check_ok "NVIDIA GPU: ${gpu_info}"
                    cuda_check=$(ssh $chk_opts "$existing_user@$existing_host" "LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c \"
import torch
pt = torch.cuda.is_available()
ct2 = False
ow = False
try:
    import ctranslate2
    ct2 = bool(ctranslate2.get_supported_compute_types('cuda'))
except: pass
try:
    import whisper
    ow = True
except: pass
print(f'{pt},{ct2},{ow}')
\"" 2>/dev/null)
                    pt_ok=$(echo "$cuda_check" | cut -d, -f1)
                    ct2_ok=$(echo "$cuda_check" | cut -d, -f2)
                    ow_ok=$(echo "$cuda_check" | cut -d, -f3)
                    if [ "$pt_ok" = "True" ] && [ "$ct2_ok" = "True" ]; then
                        # 區分原始碼編譯 vs PyPI 預編譯
                        local ct2_src=""
                        if ssh $chk_opts "$existing_user@$existing_host" "ls ~/jt-whisper-server/.ct2-wheels/ctranslate2-*.whl" &>/dev/null 2>&1; then
                            ct2_src="原始碼編譯"
                        fi
                        if [ -n "$ct2_src" ]; then
                            check_ok "CUDA 可用（faster-whisper + CTranslate2 ${ct2_src}）"
                        else
                            check_ok "CUDA 可用（faster-whisper + CTranslate2）"
                        fi
                    elif [ "$pt_ok" = "True" ] && [ "$ow_ok" = "True" ]; then
                        # 檢查是否為 aarch64（spinner 結束後再觸發編譯）
                        local chk_arch
                        chk_arch=$(ssh $chk_opts "$existing_user@$existing_host" "uname -m" 2>/dev/null)
                        if [ "$chk_arch" = "aarch64" ]; then
                            echo -e "  ${C_WARN}[提醒]${NC} aarch64 + openai-whisper（較慢），稍後嘗試編譯 CTranslate2"
                            need_repair=2  # 特殊值：不是故障，而是可升級
                        else
                            check_ok "CUDA 可用（openai-whisper + PyTorch）"
                        fi
                    else
                        if [ "$pt_ok" != "True" ]; then
                            echo -e "  ${C_WARN}[警告]${NC} 有 GPU 但 PyTorch CUDA 不可用 — 需修復"
                        else
                            echo -e "  ${C_WARN}[警告]${NC} PyTorch CUDA 正常但無可用 CUDA 辨識引擎 — 需修復"
                        fi
                        need_repair=1
                        repair_items="${repair_items} cuda"
                    fi
                else
                    echo -e "  ${C_DIM}未偵測到 NVIDIA GPU（將以 CPU 辨識）${NC}"
                fi

                # 7. 伺服器磁碟空間（非阻斷，僅提示）
                local remote_avail_mb
                remote_avail_mb=$(ssh $chk_opts "$existing_user@$existing_host" "df -m ~ | awk 'NR==2{print \$4}'" 2>/dev/null)
                if [ -n "$remote_avail_mb" ] && [ "$remote_avail_mb" -gt 0 ] 2>/dev/null; then
                    local remote_avail_gb
                    remote_avail_gb=$(awk "BEGIN{printf \"%.1f\", $remote_avail_mb/1024}")
                    if [ "$remote_avail_mb" -lt 5000 ]; then
                        echo -e "  ${C_WARN}[警告]${NC} 伺服器磁碟空間偏低（${remote_avail_gb} GB 可用）"
                    else
                        check_ok "伺服器磁碟空間 ${remote_avail_gb} GB 可用"
                    fi
                fi
            else
                check_fail "SSH 連線失敗"
                need_repair=1
                repair_items="ssh"
            fi
        } > "$_CHECK_BUF" 2>&1
        spinner_stop
        cat "$_CHECK_BUF"

        # aarch64 CTranslate2 原始碼編譯（need_repair=2 表示可升級）
        if [ "$need_repair" -eq 2 ]; then
            if _build_ctranslate2_from_source "$chk_opts" "$existing_user" "$existing_host"; then
                check_ok "CUDA 已升級（faster-whisper + CTranslate2 原始碼編譯）"
            else
                echo -e "  ${C_WARN}[提醒]${NC} CTranslate2 原始碼編譯失敗，faster-whisper 無法使用 CUDA GPU"
                check_ok "CUDA 可用（降級使用 openai-whisper + PyTorch，速度較慢約 ~2x realtime）"
            fi
            need_repair=0
        fi

        if [ "$need_repair" -eq 0 ]; then
            # 確認 SSH 免密碼登入（ControlMaster 仍在，不會再問密碼）
            if [ -n "$existing_key" ] && [ -f "${existing_key}.pub" ]; then
                if ! ssh $chk_opts "$existing_user@$existing_host" "grep -qF '$(cat "${existing_key}.pub")' ~/.ssh/authorized_keys 2>/dev/null"; then
                    ssh $chk_opts "$existing_user@$existing_host" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" < "${existing_key}.pub"
                    if [ $? -eq 0 ]; then
                        check_ok "SSH 公鑰已加入伺服器，日後免密碼"
                    fi
                fi
            fi
            # 檢查預設模型是否已下載
            local model_ok
            model_ok=$(ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 -c \"
from huggingface_hub import scan_cache_dir
try:
    ci = scan_cache_dir()
    names = [r.repo_id for r in ci.repos]
    print('yes' if any('large-v3-turbo' in n for n in names) else 'no')
except: print('no')
\"" 2>/dev/null)
            # 預下載所有辨識模型
            ssh $chk_opts "$existing_user@$existing_host" "
                LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c \"
import sys
# 偵測後端
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
\"
            " 2>&1 | grep -v "^Shared connection"
            check_ok "辨識模型檢查完成"
            # 同步部署最新 server.py（僅本地有此檔案時）
            # 先前裝的伺服器沒有開機自動啟動；補裝（已裝過就只是覆寫同一份）。
            # 要放在下面的更新重啟之前，重啟才會交給 systemd。
            _rw_install_unit "$chk_opts" "$existing_user@$existing_host" "$existing_wport" || true
            if [ -f "$SCRIPT_DIR/remote_whisper_server.py" ]; then
                local scp_chk_opts="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -P $existing_port"
                scp_chk_opts="$scp_chk_opts -o ControlMaster=auto -o ControlPath=$ctrl_sock -o ControlPersist=120"
                if [ -n "$existing_key" ] && [ -f "$existing_key" ]; then
                    scp_chk_opts="$scp_chk_opts -i $existing_key"
                fi
                local local_hash remote_hash
                local_hash=$(md5 -q "$SCRIPT_DIR/remote_whisper_server.py" 2>/dev/null || md5sum "$SCRIPT_DIR/remote_whisper_server.py" 2>/dev/null | cut -d' ' -f1)
                remote_hash=$(ssh $chk_opts "$existing_user@$existing_host" "md5sum ~/jt-whisper-server/server.py 2>/dev/null | cut -d' ' -f1" 2>/dev/null)
                if [ "$local_hash" != "$remote_hash" ]; then
                    # **只比 hash 會把伺服器降版**：本機這份可能比伺服器上的舊。
                    # 2026-09-23 之前 remote_whisper_server.py 不在 _UPGRADE_FILES 裡，
                    # 所以每一台 --upgrade 上來的機器手上都是舊的，一跑 install.sh
                    # 就會把 GPU 伺服器蓋回去。先比版本號再決定。
                    local local_ver remote_ver
                    local_ver=$(grep -m1 '^SERVER_VERSION' "$SCRIPT_DIR/remote_whisper_server.py" 2>/dev/null | cut -d'"' -f2)
                    remote_ver=$(ssh $chk_opts "$existing_user@$existing_host" "grep -m1 '^SERVER_VERSION' ~/jt-whisper-server/server.py 2>/dev/null | cut -d'\"' -f2" 2>/dev/null)
                    if _rw_ver_lt "$local_ver" "$remote_ver"; then
                        check_ok "伺服器上的 server.py 較新（v${remote_ver} > 本機 v${local_ver}），不覆蓋"
                    elif scp $scp_chk_opts "$SCRIPT_DIR/remote_whisper_server.py" "$existing_user@$existing_host:~/jt-whisper-server/server.py" &>/dev/null; then
                        # 舊版到這裡只 pkill、**沒有任何啟動指令**，卻印「已重啟伺服器」
                        # ——服務就停在那裡，而畫面說成功。
                        _rw_stop "$chk_opts" "$existing_user@$existing_host" "$existing_wport"
                        _rw_start "$chk_opts" "$existing_user@$existing_host" "$existing_wport"
                        if _rw_wait_health "$existing_host" "$existing_wport" 15 "$local_ver"; then
                            check_ok "server.py 已更新為 v${local_ver} 並重新啟動"
                        else
                            check_fail "server.py 已更新為 v${local_ver}，但伺服器沒有起來"
                            echo -e "  ${C_DIM}可查看 log: ssh $existing_user@$existing_host cat /tmp/jt-whisper-server.log${NC}"
                        fi
                    fi
                fi
            fi
            _rw_offer_tts "$chk_opts" "$existing_user@$existing_host"
            _rw_offer_breezy "$chk_opts" "$existing_user@$existing_host"
            # 關閉 SSH 多工
            ssh -o ControlPath="$ctrl_sock" -O exit "$existing_user@$existing_host" &>/dev/null || true
            check_ok "GPU 伺服器 辨識環境正常（${existing_user}@${existing_host}）"
            return 0
        fi

        # 關閉檢查用 SSH 多工（修復前先關閉，安裝流程會建新的）
        ssh -o ControlPath="$ctrl_sock" -O exit "$existing_user@$existing_host" &>/dev/null || true

        # 需要修復
        echo ""
        echo -e "  ${C_WARN}偵測到問題:${repair_items}${NC}"
        echo -ne "  ${C_WHITE}是否修復伺服器環境？(Y/n): ${NC}"
        # 讀不到輸入（沒有終端機、自動化派送）時當成「否」：預設是「是」，無人值守時不可以去動 GPU 伺服器（2026-10-05）
        read -r do_repair || do_repair="n"
        if [[ "$do_repair" =~ ^[Nn]$ ]]; then
            echo -e "  ${C_DIM}跳過修復${NC}"
            return 0
        fi

        # 用既有設定進入安裝流程
        local rw_host="$existing_host"
        local rw_ssh_port="$existing_port"
        local rw_user="$existing_user"
        local rw_key="$existing_key"
        local rw_port="$existing_wport"
    else
        # 沒有設定，問要不要新設
        echo -e "  ${C_WHITE}若有 Linux + NVIDIA GPU 伺服器，可部署伺服器 Whisper 辨識服務，大幅加快語音辨識速度${NC}"
        echo -e "  ${C_DIM}離線處理音訊檔（--input）時速度快 5-10 倍${NC}"
        echo -e "  ${C_DIM}支援系統：DGX OS / Ubuntu（需有 NVIDIA 驅動與 CUDA）${NC}"
        echo -e "  ${C_DIM}不設定則使用本機 CPU 辨識${NC}"
        echo ""
        echo -ne "  ${C_WHITE}是否設定GPU 伺服器 辨識？(y/N): ${NC}"
        # 讀不到輸入時當成沒有人回答（預設：否）；以前 set -e 讓整個安裝程式在這裡以失敗結束（2026-10-05）
        read -r setup_remote || setup_remote=""
        if [[ ! "$setup_remote" =~ ^[Yy]$ ]]; then
            echo -e "  ${C_DIM}跳過伺服器設定${NC}"
            return 0
        fi

        # 收集 SSH 連線資訊
        echo ""
        echo -ne "  ${C_WHITE}SSH 伺服器 IP: ${NC}"
        read -r rw_host || rw_host=""
        if [ -z "$rw_host" ]; then
            echo -e "  ${C_DIM}未輸入，跳過${NC}"
            return 0
        fi

        echo -ne "  ${C_WHITE}SSH Port [22]: ${NC}"
        read -r rw_ssh_port || rw_ssh_port=""
        rw_ssh_port=${rw_ssh_port:-22}

        echo -ne "  ${C_WHITE}SSH 使用者: ${NC}"
        read -r rw_user || rw_user=""
        if [ -z "$rw_user" ]; then
            echo -e "  ${C_DIM}未輸入使用者，跳過${NC}"
            return 0
        fi

        # 自動找 SSH key
        local rw_key=""
        if [ -f "$HOME/.ssh/id_ed25519" ]; then
            rw_key="$HOME/.ssh/id_ed25519"
        elif [ -f "$HOME/.ssh/id_rsa" ]; then
            rw_key="$HOME/.ssh/id_rsa"
        fi
        echo -ne "  ${C_WHITE}SSH Key 路徑 [${rw_key:-留空用密碼}]: ${NC}"
        read -r rw_key_input || rw_key_input=""
        if [ -n "$rw_key_input" ]; then
            rw_key="$rw_key_input"
        fi

        echo -ne "  ${C_WHITE}Whisper 服務 Port [8978]: ${NC}"
        read -r rw_port || rw_port=""
        rw_port=${rw_port:-8978}
    fi

    # 組合 SSH 指令（使用 ControlMaster 多工，只需輸入一次密碼）
    local ctrl_sock="/tmp/jt-ssh-cm-${rw_user}@${rw_host}:${rw_ssh_port}"
    local ssh_opts="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $rw_ssh_port"
    ssh_opts="$ssh_opts -o ControlMaster=auto -o ControlPath=$ctrl_sock -o ControlPersist=120"
    if [ -n "$rw_key" ] && [ -f "$rw_key" ]; then
        ssh_opts="$ssh_opts -i $rw_key"
    fi

    # 清理函式：關閉 SSH 多工連線
    _cleanup_ssh_cm() {
        ssh -o ControlPath="$ctrl_sock" -O exit "$rw_user@$rw_host" &>/dev/null || true
    }

    # 測試 SSH 連線（第一次連線，建立 ControlMaster）
    echo ""
    echo -e "  ${C_DIM}測試 SSH 連線...${NC}"
    if ! ssh $ssh_opts "$rw_user@$rw_host" "echo ok" &>/dev/null; then
        check_fail "SSH 連線失敗（$rw_user@$rw_host:${rw_ssh_port}）"
        echo -e "  ${C_DIM}請確認 SSH 設定後重新執行 install.sh${NC}"
        _cleanup_ssh_cm
        return 1
    fi
    check_ok "SSH 連線成功（後續操作免重複輸入密碼）"

    # 設定 SSH 免密碼登入（若尚未設定）
    if [ -n "$rw_key" ] && [ -f "${rw_key}.pub" ]; then
        if ! ssh $ssh_opts "$rw_user@$rw_host" "grep -qF '$(cat "${rw_key}.pub")' ~/.ssh/authorized_keys 2>/dev/null"; then
            ssh $ssh_opts "$rw_user@$rw_host" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" < "${rw_key}.pub"
            if [ $? -eq 0 ]; then
                check_ok "SSH 公鑰已加入伺服器，日後免密碼"
            fi
        else
            check_ok "SSH 免密碼登入已設定"
        fi
    fi

    # 檢查伺服器 Python3 + ffmpeg + 編譯工具
    local need_apt=""
    if ! ssh $ssh_opts "$rw_user@$rw_host" "command -v python3" &>/dev/null; then
        need_apt="python3 python3-venv python3-pip"
    fi
    if ! ssh $ssh_opts "$rw_user@$rw_host" "command -v ffmpeg" &>/dev/null; then
        need_apt="$need_apt ffmpeg"
    fi
    # 編譯工具與系統函式庫（C 擴充套件需要）
    # webrtcvad: 需要 gcc + Python.h
    # soundfile: 需要 libsndfile（resemblyzer → librosa → soundfile）
    # cffi: 需要 libffi（soundfile → cffi）
    # pkg-config: 用於偵測系統函式庫
    local build_pkgs="build-essential python3-dev pkg-config libffi-dev libsndfile1-dev cmake git"
    for pkg in $build_pkgs; do
        if ! ssh $ssh_opts "$rw_user@$rw_host" "dpkg -s $pkg" &>/dev/null 2>&1; then
            need_apt="$need_apt $pkg"
        fi
    done
    if [ -n "$need_apt" ]; then
        check_install "伺服器缺少:${need_apt}，正在安裝..."
        if ! run_spinner "安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "apt update -qq && apt install -y -qq $need_apt"; then
            echo ""
            check_fail "無法在伺服器安裝系統套件"
            _cleanup_ssh_cm
            return 1
        fi
        echo ""
    fi
    check_ok "Python3 + ffmpeg + 編譯工具就緒"

    # 檢查伺服器磁碟空間
    local remote_avail_mb
    remote_avail_mb=$(ssh $ssh_opts "$rw_user@$rw_host" "df -m ~ | awk 'NR==2{print \$4}'" 2>/dev/null)
    if [ -n "$remote_avail_mb" ] && [ "$remote_avail_mb" -gt 0 ] 2>/dev/null; then
        local remote_avail_gb
        remote_avail_gb=$(awk "BEGIN{printf \"%.1f\", $remote_avail_mb/1024}")
        if [ "$remote_avail_mb" -lt 5000 ]; then
            check_fail "伺服器磁碟空間不足：可用 ${remote_avail_gb} GB，最小需要 5 GB"
            echo -e "  ${C_DIM}GPU 伺服器需要安裝 PyTorch (~2.5GB) + Whisper 模型 (~6GB)${NC}"
            _cleanup_ssh_cm
            return 1
        elif [ "$remote_avail_mb" -lt 12000 ]; then
            echo -e "  ${C_WARN}[注意]${NC} 伺服器可用空間 ${remote_avail_gb} GB（完整安裝需 12 GB）"
        else
            check_ok "伺服器磁碟空間充足（${remote_avail_gb} GB 可用）"
        fi
    fi

    # 檢查伺服器 NVIDIA GPU + CUDA
    local remote_gpu_name
    remote_gpu_name=$(ssh $ssh_opts "$rw_user@$rw_host" "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1" 2>/dev/null)
    local torch_index=""
    if [ -n "$remote_gpu_name" ]; then
        check_ok "NVIDIA GPU: ${remote_gpu_name}"
        # 偵測 CUDA 版本（major.minor），決定 PyTorch wheel
        local cuda_version cuda_major cuda_minor
        cuda_version=$(ssh $ssh_opts "$rw_user@$rw_host" "nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+'" 2>/dev/null)
        if [ -n "$cuda_version" ]; then
            cuda_major=$(echo "$cuda_version" | cut -d. -f1)
            cuda_minor=$(echo "$cuda_version" | cut -d. -f2)
            check_ok "CUDA: ${cuda_version}"
            # Blackwell (sm_100) 需要 cu128+；CUDA 13.x 或 12.8+ 用 cu128
            if [ "$cuda_major" -ge 13 ] || { [ "$cuda_major" -eq 12 ] && [ "$cuda_minor" -ge 8 ]; }; then
                torch_index="https://download.pytorch.org/whl/cu128"
            elif [ "$cuda_major" -eq 12 ]; then
                torch_index="https://download.pytorch.org/whl/cu124"
            elif [ "$cuda_major" -eq 11 ]; then
                torch_index="https://download.pytorch.org/whl/cu118"
            fi
        else
            echo -e "  ${C_WARN}未偵測到 CUDA，PyTorch 將安裝 CPU 版${NC}"
        fi
    else
        echo -e "  ${C_WARN}未偵測到 NVIDIA GPU，PyTorch 將安裝 CPU 版（辨識速度較慢）${NC}"
    fi

    # 建立 venv。已經有、但壞了或 Python 版本與建立時不同（作業系統升級）就重建：留著也不能用。
    # 優先用固定版本的 python3.12：venv 指向通用的 python3 時，系統換版後它會「看起來正常、套件全不見」
    if printf '%s\n' "$_VENV_CHECK_PY" | ssh $ssh_opts "$rw_user@$rw_host" "test -d ~/jt-whisper-server/venv && ! ~/jt-whisper-server/venv/bin/python3 -" &>/dev/null; then
        echo -e "  ${C_WARN}[偵測]${NC} 伺服器的 venv 不能用（損壞，或 Python 版本與建立時不同），重建中"
        ssh $ssh_opts "$rw_user@$rw_host" "rm -rf ~/jt-whisper-server/venv"
    fi
    ssh $ssh_opts "$rw_user@$rw_host" "
        mkdir -p ~/jt-whisper-server
        if [ ! -d ~/jt-whisper-server/venv ]; then
            if command -v python3.12 >/dev/null 2>&1; then python3.12 -m venv ~/jt-whisper-server/venv; else python3 -m venv ~/jt-whisper-server/venv; fi
        fi
    "

    # 檢查 PyTorch CUDA 是否已正常（避免重複安裝 2-3 GB）
    local skip_torch=0
    if [ -n "$torch_index" ]; then
        local pt_ok
        pt_ok=$(ssh $ssh_opts "$rw_user@$rw_host" "~/jt-whisper-server/venv/bin/python3 -c 'import torch; print(torch.cuda.is_available())'" 2>/dev/null)
        if [ "$pt_ok" = "True" ]; then
            check_ok "PyTorch CUDA 已正常，跳過重裝"
            skip_torch=1
        fi
    fi

    if [ "$skip_torch" -eq 0 ]; then
        local torch_extra=""
        local torch_msg="安裝 PyTorch..."
        if [ -n "$torch_index" ]; then
            torch_extra="--force-reinstall --index-url $torch_index"
            torch_msg="安裝 PyTorch GPU 版（約 2-3 GB）..."
        fi
        check_install "$torch_msg"
        run_spinner "安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "
            PIP=~/jt-whisper-server/venv/bin/pip
            \$PIP install --disable-pip-version-check torch $torch_extra 2>&1
        "
        if [ $? -ne 0 ]; then
            echo ""
            check_fail "PyTorch 安裝失敗"
            _cleanup_ssh_cm
            return 1
        fi
        echo ""
        check_ok "PyTorch 安裝完成"
    fi

    # 安裝其他套件
    check_install "安裝伺服器 Python 套件..."
    # 檢查是否有原始碼編譯的 CTranslate2 快取 wheel（aarch64 CUDA）
    local ct2_cached_whl=""
    ct2_cached_whl=$(ssh $ssh_opts "$rw_user@$rw_host" "ls ~/jt-whisper-server/.ct2-wheels/ctranslate2-*.whl 2>/dev/null | head -1" 2>/dev/null)
    # setuptools<81: 保留 pkg_resources（webrtcvad 等舊套件需要，setuptools 82+ 已移除）
    # 依賴鏈: resemblyzer → webrtcvad(需gcc+Python.h+pkg_resources) + librosa → soundfile(需libsndfile+libffi)
    if [ -n "$ct2_cached_whl" ]; then
        # 有原始碼編譯 wheel：跳過 PyPI 的 ctranslate2，用快取 wheel + --no-deps 保護
        run_spinner "安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "
            PIP=~/jt-whisper-server/venv/bin/pip
            \$PIP install --disable-pip-version-check 'setuptools<81' wheel 2>&1
            \$PIP install --disable-pip-version-check --force-reinstall --no-deps '$ct2_cached_whl' 2>&1
            \$PIP install --disable-pip-version-check \
                'setuptools<81' faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 'transformers>=5.18' 2>&1
        "
    else
        local fw_extra=""
        if [ -n "$torch_index" ]; then
            fw_extra="--force-reinstall"
        fi
        run_spinner "安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "
            PIP=~/jt-whisper-server/venv/bin/pip
            \$PIP install --disable-pip-version-check 'setuptools<81' wheel 2>&1
            \$PIP install --disable-pip-version-check $fw_extra \
                'setuptools<81' ctranslate2 faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 'transformers>=5.18' 2>&1
        "
    fi
    if [ $? -ne 0 ]; then
        echo ""
        check_fail "伺服器套件安裝失敗"
        _cleanup_ssh_cm
        return 1
    fi
    echo ""
    check_ok "伺服器 Python 套件安裝完成"

    # 驗證 CUDA（PyTorch + CTranslate2）
    if [ -n "$torch_index" ]; then
        local cuda_check
        cuda_check=$(ssh $ssh_opts "$rw_user@$rw_host" "LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH ~/jt-whisper-server/venv/bin/python3 -c \"
import torch
pt = torch.cuda.is_available()
try:
    import ctranslate2
    ct2 = bool(ctranslate2.get_supported_compute_types('cuda'))
except:
    ct2 = False
print(f'{pt},{ct2}')
\"" 2>/dev/null)
        local pt_ok=$(echo "$cuda_check" | cut -d, -f1)
        local ct2_ok=$(echo "$cuda_check" | cut -d, -f2)
        if [ "$pt_ok" = "True" ] && [ "$ct2_ok" = "True" ]; then
            check_ok "CUDA 驗證通過（faster-whisper + CTranslate2 CUDA）"
        elif [ "$pt_ok" = "True" ]; then
            # 偵測架構：aarch64 嘗試原始碼編譯 CTranslate2
            local remote_arch
            remote_arch=$(ssh $ssh_opts "$rw_user@$rw_host" "uname -m" 2>/dev/null)
            local ct2_built=0
            if [ "$remote_arch" = "aarch64" ]; then
                if _build_ctranslate2_from_source "$ssh_opts" "$rw_user" "$rw_host"; then
                    ct2_built=1
                    check_ok "CUDA 驗證通過（faster-whisper + CTranslate2 原始碼編譯）"
                fi
            fi
            if [ "$ct2_built" -eq 0 ]; then
                check_install "CTranslate2 無 CUDA，改裝 openai-whisper（PyTorch CUDA）..."
                run_spinner "安裝中..." ssh $ssh_opts "$rw_user@$rw_host" "
                    PIP=~/jt-whisper-server/venv/bin/pip
                    \$PIP install --disable-pip-version-check 'setuptools<81' openai-whisper 2>&1
                "
                echo ""
                # 驗證 openai-whisper
                local ow_ok
                ow_ok=$(ssh $ssh_opts "$rw_user@$rw_host" "~/jt-whisper-server/venv/bin/python3 -c 'import whisper; print(\"ok\")'" 2>/dev/null)
                if [ "$ow_ok" = "ok" ]; then
                    check_ok "CUDA 驗證通過（openai-whisper + PyTorch CUDA）"
                else
                    echo -e "  ${C_WARN}[警告]${NC} openai-whisper 安裝失敗，Whisper 將以 CPU 執行"
                fi
            fi
        else
            echo -e "  ${C_WARN}[警告]${NC} PyTorch CUDA 無法使用，Whisper 將以 CPU 執行"
        fi
    fi

    # SCP 部署 server.py（ControlMaster 也適用於 scp）
    local scp_opts="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -P $rw_ssh_port"
    scp_opts="$scp_opts -o ControlMaster=auto -o ControlPath=$ctrl_sock -o ControlPersist=120"
    if [ -n "$rw_key" ] && [ -f "$rw_key" ]; then
        scp_opts="$scp_opts -i $rw_key"
    fi
    if ! scp $scp_opts "$SCRIPT_DIR/remote_whisper_server.py" "$rw_user@$rw_host:~/jt-whisper-server/server.py" &>/dev/null; then
        check_fail "SCP 部署失敗"
        _cleanup_ssh_cm
        return 1
    fi
    check_ok "server.py 已部署"

    # 測試啟動
    # setsid + < /dev/null：少了它們，ssh 一結束服務就被 SIGHUP 帶走
    _rw_start "$ssh_opts" "$rw_user@$rw_host" "$rw_port"

    # Health check（最多 15 秒）+ spinner
    _test_health() {
        local ok=1
        for i in $(seq 1 15); do
            if curl -s --connect-timeout 2 "http://$rw_host:$rw_port/health" 2>/dev/null | grep -q '"ok"'; then
                ok=0
                break
            fi
            sleep 1
        done
        return $ok
    }
    run_spinner "測試啟動伺服器..." _test_health
    local health_ok=$?

    # 停止測試 server（不可用 pkill -f，會殺到執行它的遠端 shell 自己）
    _rw_stop "$ssh_opts" "$rw_user@$rw_host" "$rw_port"

    if [ "$health_ok" -eq 0 ]; then
        echo ""
        check_ok "伺服器測試成功"
        # 測試成功就交給 systemd 常駐（開機自動啟動）
        if _rw_install_unit "$ssh_opts" "$rw_user@$rw_host" "$rw_port"; then
            _rw_start "$ssh_opts" "$rw_user@$rw_host" "$rw_port"
        fi
    else
        echo ""
        check_fail "伺服器無法啟動，請檢查防火牆或 GPU 驅動"
        echo -e "  ${C_DIM}可查看伺服器 log: ssh $rw_user@$rw_host cat /tmp/jt-whisper-server.log${NC}"
        _cleanup_ssh_cm
        return 1
    fi

    # 預下載所有辨識模型
    check_install "預下載辨識模型（首次約 6 GB）..."
    ssh $ssh_opts "$rw_user@$rw_host" "
        ~/jt-whisper-server/venv/bin/python3 -c \"
import sys
# 偵測後端
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
\"
    " 2>&1 | grep -v "^Shared connection"
    check_ok "辨識模型下載完成"
    _rw_offer_tts "$ssh_opts" "$rw_user@$rw_host"
    _rw_offer_breezy "$ssh_opts" "$rw_user@$rw_host"

    # 關閉 SSH 多工連線
    _cleanup_ssh_cm

    # 寫入 config.json（merge 進現有設定）
    "$_PY" -c "
import json, os
config_path = '$SCRIPT_DIR/config.json'
cfg = {}
if os.path.isfile(config_path):
    with open(config_path, 'r') as f:
        cfg = json.load(f)
cfg['remote_whisper'] = {
    'host': '$rw_host',
    'ssh_port': int('$rw_ssh_port'),
    'ssh_user': '$rw_user',
    'ssh_key': '$rw_key',
    'whisper_port': int('$rw_port'),
}
with open(config_path, 'w') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write('\n')
print('  config.json 已更新')
"
    check_ok "設定已儲存至 config.json"
}

# ─── 驗證安裝結果 ────────────────────────────────
verify_installation() {
    section "驗證安裝結果"

    local verify_failed=0

    # Python venv
    if [ -f "$VENV_DIR/bin/python3" ]; then
        check_ok "Python 虛擬環境"
    else
        check_fail "Python 虛擬環境"
        ((verify_failed++)) || true
    fi

    source "$VENV_DIR/bin/activate" 2>/dev/null

    # 核心套件
    local verify_modules=(
        "numpy|numpy（數值計算）"
        "ctranslate2|ctranslate2（語音辨識加速）"
        "sentencepiece|sentencepiece（分詞工具）"
        "faster_whisper|faster-whisper（離線辨識）"
        "resemblyzer|resemblyzer（講者辨識）"
        "spectralcluster|spectralcluster（講者分群）"
        "sounddevice|sounddevice（音訊擷取）"
        "argos_check|Argos Translate（離線翻譯）"
        "opencc|OpenCC（簡繁轉換）"
    )

    for item in "${verify_modules[@]}"; do
        local mod="${item%%|*}"
        local desc="${item#*|}"
        if [ "$mod" = "argos_check" ]; then
            # Argos: 檢查模型目錄（translate_meeting.py 有 fallback 不需 pip 套件）
            local _argos_found
            _argos_found=$(find "$HOME/.local/share/argos-translate/packages" -maxdepth 1 -name "translate-en_zh*" -type d 2>/dev/null | head -1)
            if [ -n "$_argos_found" ]; then
                check_ok "$desc"
            else
                check_fail "$desc"
                ((verify_failed++)) || true
            fi
        elif python3 -c "import $mod" &>/dev/null 2>&1; then
            check_ok "$desc"
        else
            check_fail "$desc"
            ((verify_failed++)) || true
        fi
    done

    # Moonshine
    if python3 -c "from moonshine_voice import get_model_for_language" &>/dev/null 2>&1; then
        check_ok "Moonshine（英文低延遲 ASR）"
    else
        echo -e "  ${C_DIM}[略過]${NC} Moonshine 未安裝（選裝，不影響主要功能）"
    fi

    # whisper.cpp
    if [ -x "$WHISPER_DIR/build/bin/whisper-stream" ]; then
        check_ok "whisper.cpp（本機即時辨識）"
    else
        echo -e "  ${C_DIM}[略過]${NC} whisper.cpp 未安裝（離線模式、Moonshine、GPU 伺服器不受影響）"
    fi

    # ScreenCaptureKit 系統音訊
    if _sck_macos_ok; then
        if [ -x "$SCRIPT_DIR/bin/jt-sck-audio" ]; then
            local _sck_perm
            _sck_perm=$("$SCRIPT_DIR/bin/jt-sck-audio" --check 2>/dev/null | grep -o '"permission":[a-z]*' | cut -d: -f2)
            if [ "$_sck_perm" = "true" ]; then
                check_ok "ScreenCaptureKit（系統音訊擷取，已授權）"
            else
                check_notice "ScreenCaptureKit 已就緒但尚未授權「螢幕錄製」"
                echo -e "  ${C_DIM}執行 ./start.sh --sck-permission 完成授權；未授權時改用 BlackHole${NC}"
            fi
        else
            check_fail "ScreenCaptureKit 元件未編譯"
            ((verify_failed++)) || true
        fi
    fi

    # PyQt6
    if python3 -c "from PyQt6.QtWidgets import QApplication" &>/dev/null 2>&1; then
        check_ok "PyQt6（懸浮字幕視窗）"
    else
        check_fail "PyQt6 未安裝"
        ((verify_failed++)) || true
    fi

    # ffmpeg
    if command -v ffmpeg &>/dev/null; then
        check_ok "ffmpeg（音訊轉檔）"
    else
        check_fail "ffmpeg 未安裝（處理非 WAV 音訊時需要）"
        ((verify_failed++)) || true
    fi

    deactivate 2>/dev/null
    _VERIFY_FAILED=$verify_failed
}

# ─── 總結 ────────────────────────────────────────
print_summary() {
    _VERIFY_FAILED=0
    verify_installation
    local verify_failed=$_VERIFY_FAILED

    echo ""
    echo -e "${C_TITLE}============================================================${NC}"
    if [ "$verify_failed" -eq 0 ] 2>/dev/null; then
        echo -e "${C_OK}${BOLD}  安裝完成！${NC}"
    else
        echo -e "${C_WARN}${BOLD}  安裝完成（${verify_failed} 個元件未安裝，詳見上方提示）${NC}"
    fi
    echo -e "${C_TITLE}============================================================${NC}"
    echo ""

    # 功能對照表
    source "$VENV_DIR/bin/activate" 2>/dev/null

    echo -e "  ${C_WHITE}可用功能：${NC}"

    # faster-whisper
    if python3 -c "import faster_whisper" &>/dev/null 2>&1; then
        echo -e "  ${C_OK}■${NC} 離線音訊處理 (--input)  ${C_DIM}faster-whisper${NC}"
    else
        echo -e "  ${C_DIM}□ 離線音訊處理 (--input)  faster-whisper${NC}"
    fi

    # faster-whisper 模型
    local _fw_model
    if [ "$(uname -m)" = "arm64" ]; then
        _fw_model="large-v3-turbo"
    else
        _fw_model="small"
    fi
    local _fw_model_found
    _fw_model_found=$(python3 -c "
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
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-' + '$_fw_model')):
            found = True
            break
    if found:
        break
print('found' if found else '')
" 2>/dev/null)
    if [ -n "$_fw_model_found" ]; then
        echo -e "  ${C_OK}■${NC} Whisper 模型 $_fw_model  ${C_DIM}faster-whisper 格式${NC}"
    else
        echo -e "  ${C_DIM}□ Whisper 模型 $_fw_model  faster-whisper 格式${NC}"
    fi

    # 講者辨識：Nemotron（Apple Silicon、transformers 5.18）優先，沒有時用 resemblyzer
    if [ "$(uname -m)" = "arm64" ] && python3 -c "$_NEMO_TF_CHECK" &>/dev/null 2>&1; then
        echo -e "  ${C_OK}■${NC} AI 講者辨識 (--diarize)  ${C_DIM}Nemotron（resemblyzer 備援）${NC}"
    elif python3 -c "import resemblyzer" &>/dev/null 2>&1; then
        echo -e "  ${C_OK}■${NC} AI 講者辨識 (--diarize)  ${C_DIM}resemblyzer${NC}"
    else
        echo -e "  ${C_DIM}□ AI 講者辨識 (--diarize)  resemblyzer${NC}"
    fi

    # Argos（檢查模型目錄而非 pip 套件）
    local _argos_pkg_dir="$HOME/.local/share/argos-translate/packages"
    if [ -d "$_argos_pkg_dir" ] && find "$_argos_pkg_dir" -maxdepth 1 -name "translate-en_zh*" -type d 2>/dev/null | grep -q .; then
        echo -e "  ${C_OK}■${NC} Argos 離線翻譯  ${C_DIM}僅英翻中${NC}"
    else
        echo -e "  ${C_DIM}□ Argos 離線翻譯  僅英翻中${NC}"
    fi

    # NLLB
    local nllb_dir="$HOME/.local/share/jt-live-whisper/models/nllb-600m"
    if [ -f "$nllb_dir/model.bin" ]; then
        echo -e "  ${C_OK}■${NC} NLLB 離線翻譯  ${C_DIM}中日韓英互譯${NC}"
    else
        echo -e "  ${C_DIM}□ NLLB 離線翻譯  中日韓英互譯${NC}"
    fi

    # Moonshine
    if python3 -c "from moonshine_voice import get_model_for_language" &>/dev/null 2>&1; then
        echo -e "  ${C_OK}■${NC} Moonshine 即時辨識  ${C_DIM}英文低延遲${NC}"
    else
        echo -e "  ${C_DIM}□ Moonshine 即時辨識  英文低延遲${NC}"
    fi

    # whisper.cpp
    if [ -x "$WHISPER_DIR/build/bin/whisper-stream" ]; then
        echo -e "  ${C_OK}■${NC} Whisper 本機即時辨識  ${C_DIM}whisper.cpp${NC}"
    else
        echo -e "  ${C_DIM}□ Whisper 本機即時辨識  whisper.cpp${NC}"
    fi

    # MLX Whisper（僅 ARM64）
    if [ "$(uname -m)" = "arm64" ]; then
        if python3 -c "import mlx_whisper" &>/dev/null 2>&1; then
            echo -e "  ${C_OK}■${NC} MLX Whisper 即時辨識  ${C_DIM}Apple Silicon GPU 加速${NC}"
        else
            echo -e "  ${C_DIM}□ MLX Whisper 即時辨識  Apple Silicon GPU 加速${NC}"
        fi
    fi

    # Qwen3-ASR 本機（僅 ARM64）
    if [ "$(uname -m)" = "arm64" ]; then
        if python3 -c "$_QWEN_MLX_CHECK" &>/dev/null 2>&1; then
            echo -e "  ${C_OK}■${NC} Qwen3-ASR 本機辨識（實驗）  ${C_DIM}離線中英韓，mlx-audio${NC}"
        else
            echo -e "  ${C_DIM}□ Qwen3-ASR 本機辨識（實驗）  離線中英韓，mlx-audio${NC}"
        fi
    fi

    # GPU 伺服器
    local rw_host=""
    if [ -f "$SCRIPT_DIR/config.json" ]; then
        rw_host=$(python3 -c "
import json
try:
    cfg = json.load(open('$SCRIPT_DIR/config.json'))
    print(cfg.get('remote_whisper',{}).get('host',''))
except: pass
" 2>/dev/null)
    fi
    if [ -n "$rw_host" ]; then
        echo -e "  ${C_OK}■${NC} GPU 伺服器 ($rw_host)  ${C_DIM}remote whisper server${NC}"
    else
        echo -e "  ${C_DIM}□ GPU 伺服器 辨識  remote whisper server${NC}"
    fi

    deactivate 2>/dev/null

    echo ""
    echo -e "  ${C_WHITE}CPU 模式${NC}"
    echo -e "  ${C_DIM}建議搭配區域網路 LLM 伺服器使用（--llm-host）${NC}"
    echo ""
    echo -e "  ${C_WHITE}啟動方式: ${C_OK}./start.sh${NC}"
    echo -e "  ${C_WHITE}升級方式: ${C_OK}./install.sh --upgrade${NC}"
    echo ""
    echo -e "  ${C_DIM}提示：若日後將此資料夾搬移到其他位置，請重新執行 ./install.sh${NC}"
    echo -e "  ${C_DIM}      安裝程式會自動偵測並修復因路徑變更而損壞的環境${NC}"
    echo ""
    if [ -n "$INSTALL_LOG" ] && [ -f "$INSTALL_LOG" ]; then
        echo -e "  ${C_DIM}安裝 log: $INSTALL_LOG${NC}"
        echo ""
    fi
}

# ─── 磁碟空間檢查 ────────────────────────────────
check_disk_space() {
    section "磁碟空間檢查"

    # 取得安裝目錄可用空間（MB）
    local avail_mb
    avail_mb=$(df -m "$SCRIPT_DIR" | awk 'NR==2{print $4}')
    local avail_gb
    avail_gb=$(awk "BEGIN{printf \"%.1f\", $avail_mb/1024}")

    # 最小需求 3 GB，推薦 8 GB，完整 14 GB
    if [ "$avail_mb" -lt 3000 ]; then
        check_fail "磁碟空間不足：可用 ${avail_gb} GB，最小需要 3 GB"
        echo -e "  ${C_DIM}請釋放磁碟空間後再執行安裝${NC}"
        return 1
    elif [ "$avail_mb" -lt 8000 ]; then
        echo -e "  ${C_WARN}[注意]${NC} 可用空間 ${avail_gb} GB（推薦 8 GB 以上，完整安裝需 14 GB）"
        echo -e "  ${C_DIM}基本功能可正常安裝，但離線處理模型快取需更多空間${NC}"
    else
        check_ok "磁碟空間充足（${avail_gb} GB 可用）"
    fi

    # 檢查 ~/.cache 所在分割區（HuggingFace 快取約 5.3 GB）
    local home_dev script_dev
    script_dev=$(df "$SCRIPT_DIR" | awk 'NR==2{print $1}')
    home_dev=$(df "$HOME" | awk 'NR==2{print $1}')
    if [ "$script_dev" != "$home_dev" ]; then
        local home_avail_mb
        home_avail_mb=$(df -m "$HOME" | awk 'NR==2{print $4}')
        local home_avail_gb
        home_avail_gb=$(awk "BEGIN{printf \"%.1f\", $home_avail_mb/1024}")
        if [ "$home_avail_mb" -lt 6000 ]; then
            echo -e "  ${C_WARN}[注意]${NC} 家目錄可用空間 ${home_avail_gb} GB（~/.cache/huggingface/ 模型快取需約 5-6 GB）"
        fi
    fi
}

# ─── 桌面與應用程式選單捷徑（v2.25.4）──────────────────
# 有圖形桌面的機器，安裝或升級結束時問一次要不要建捷徑（點兩下＝WebUI 模式）：
#   macOS：桌面（.command）與「應用程式」（.app，交給「終端機」執行），可只選一邊
#   Linux：應用程式選單安裝時就會建（install-linux.sh），只問要不要也放桌面
# 答案記在安裝資料夾的 .desktop_shortcut：no，或建在哪裡（desktop／menu）。選 no 之後升級不再問；
# 建過之後使用者自己刪掉也不再問，還在的話內容過期（例如安裝資料夾搬過）就更新。
# 沒有人可以回答時（SSH 非互動、排程、測試）不問、也不記錄，等下次有人在終端機前再問。
# Windows 版在 install.ps1（offer_desktop_shortcut），規則相同
SHORTCUT_STATE_FILE="$SCRIPT_DIR/.desktop_shortcut"
SHORTCUT_NAME="jt-live-whisper"

# 捷徑圖示（v2.26.2，網站的 logo）：$1＝png／icns；檔案在就印路徑，不在回 1
# （從舊版第一次 --upgrade 時 icons/ 還沒到，捷徑先用系統圖示，第二次升級補檔後再換上）
_shortcut_icon() {
    local f="$SCRIPT_DIR/icons/$SHORTCUT_NAME.$1"
    [ -f "$f" ] || return 1
    printf '%s\n' "$f"
}
_GUI_SESSION_DIRS="/usr/share/xsessions /usr/share/wayland-sessions"
_MAC_APPS_DIRS="${JTLW_MAC_APPS_DIRS:-/Applications $HOME/Applications}"   # 先放得進去的那一個；找舊的兩個都找（測試可換掉，不碰真的「應用程式」）

# Linux 有沒有圖形桌面：正在圖形環境裡，或裝了桌面工作階段（從 SSH 安裝一台桌機時也算）
_linux_has_gui() {
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}${XDG_CURRENT_DESKTOP:-}" ] && return 0
    local d
    for d in $_GUI_SESSION_DIRS; do
        ls "$d"/*.desktop >/dev/null 2>&1 && return 0
    done
    return 1
}

# 桌面資料夾（Linux 依語系可能是 ~/桌面）；沒有就回 1
_desktop_dir() {
    local d=""
    if [ "$(uname -s)" = "Linux" ] && command -v xdg-user-dir >/dev/null 2>&1; then
        d=$(xdg-user-dir DESKTOP 2>/dev/null || true)
        [ "$d" = "$HOME" ] && d=""          # 沒設定桌面資料夾時 xdg-user-dir 回家目錄
    fi
    [ -n "$d" ] || d="$HOME/Desktop"
    [ -d "$d" ] || return 1
    printf '%s\n' "$d"
}

# 捷徑的位置：$1＝desktop／menu。menu 只有 macOS（Linux 的選單由 install-linux.sh 管）。
# $2＝find：找已經存在的那一份（macOS 的 .app 兩個資料夾都找）；沒有就回 1
_shortcut_path() {
    local d
    case "$1" in
        desktop)
            d=$(_desktop_dir) || return 1
            if [ "$(uname -s)" = "Darwin" ]; then
                printf '%s\n' "$d/$SHORTCUT_NAME.command"
            else
                printf '%s\n' "$d/$SHORTCUT_NAME.desktop"
            fi
            return 0
            ;;
        menu)
            [ "$(uname -s)" = "Darwin" ] || return 1
            for d in $_MAC_APPS_DIRS; do
                if [ "${2:-}" = "find" ]; then
                    [ -d "$d/$SHORTCUT_NAME.app" ] && { printf '%s\n' "$d/$SHORTCUT_NAME.app"; return 0; }
                elif [ -d "$d" ] && [ -w "$d" ]; then
                    printf '%s\n' "$d/$SHORTCUT_NAME.app"; return 0
                elif [ "$d" = "$HOME/Applications" ] && mkdir -p "$d" 2>/dev/null; then
                    printf '%s\n' "$d/$SHORTCUT_NAME.app"; return 0
                fi
            done
            return 1
            ;;
    esac
    return 1
}

# 依 Desktop Entry 規格把一個參數放進 Exec 的雙引號裡：引號內的 " ` $ \ 要跳脫，
# 而檔案本身的字串跳脫先套用一次，所以反斜線要寫成四個；% 是欄位代碼，要寫成 %%
_desktop_exec_quote() {
    local s="$1"
    s=${s//\\/\\\\\\\\}
    s=${s//\"/\\\\\"}
    s=${s//\`/\\\\\`}
    s=${s//\$/\\\\\$}
    s=${s//%/%%}
    printf '"%s"' "$s"
}

# Linux 的 .desktop（應用程式選單與桌面捷徑共用同一份內容）
# --shortcut：WebUI 異常結束時視窗先停住，錯誤訊息才看得到（start.sh）
_write_linux_desktop_entry() {  # $1＝檔案路徑
    local exec_path icon
    exec_path=$(_desktop_exec_quote "$SCRIPT_DIR/start.sh")
    icon=$(_shortcut_icon png) || icon="audio-input-microphone"
    cat > "$1" 2>/dev/null <<EOF || return 1
[Desktop Entry]
Type=Application
Name=jt-live-whisper
Comment=100% 全地端 AI 語音工具箱（WebUI）
Exec=${exec_path} --webui --shortcut
Path=${SCRIPT_DIR//\\/\\\\}
Icon=${icon//\\/\\\\}
Terminal=true
Categories=AudioVideo;Audio;
EOF
}

# macOS 的 .command：點兩下由「終端機」執行（螢幕錄製權限也是給終端機，WebUI 擷取系統音訊要用）
_write_mac_command() {          # $1＝檔案路徑
    {
        printf '#!/bin/bash\n'
        printf '# jt-live-whisper 捷徑（安裝程式建立）：以 WebUI（瀏覽器介面）模式啟動\n'
        printf 'cd %q && exec ./start.sh --webui --shortcut\n' "$SCRIPT_DIR"
    } > "$1" 2>/dev/null || return 1
    chmod +x "$1"
}

# macOS 的 .app（放在「應用程式」，Launchpad、Spotlight 找得到）：
# 本身只把裡面的 .command 交給「終端機」打開，WebUI 仍在終端機裡跑，權限與桌面捷徑相同。
# **執行檔不能是 shell 腳本**：macOS 26 上 LaunchServices 打不開（open 回 -10669，2026-09-30 實測），
# 所以用系統內建的 osacompile 產生 AppleScript applet（Mach-O），放進 .command 之後重新 ad-hoc 簽章
_MAC_APP_SCRIPT='do shell script "open -a Terminal " & quoted form of (POSIX path of (path to me) & "Contents/Resources/webui.command")'
_write_mac_app() {              # $1＝.app 路徑
    local app="$1"
    command -v osacompile >/dev/null 2>&1 || return 1
    rm -rf "$app" 2>/dev/null
    osacompile -o "$app" -e "$_MAC_APP_SCRIPT" >/dev/null 2>&1 || return 1
    _write_mac_command "$app/Contents/Resources/webui.command" || return 1
    # 不在 Dock 留圖示（它只是把 .command 交給終端機就結束）
    plutil -replace LSUIElement -bool YES "$app/Contents/Info.plist" >/dev/null 2>&1 || true
    # 換成我們的 logo：applet 的圖示是 Resources/applet.icns（CFBundleIconFile）；
    # 有 Assets.car／CFBundleIconName 時系統會優先用它，一併拿掉
    local icns
    if icns=$(_shortcut_icon icns); then
        cp "$icns" "$app/Contents/Resources/applet.icns" 2>/dev/null || true
        rm -f "$app/Contents/Resources/Assets.car" 2>/dev/null
        plutil -remove CFBundleIconName "$app/Contents/Info.plist" >/dev/null 2>&1 || true
    fi
    # 加了檔案、改了 Info.plist 之後簽章就不完整了，重新做 ad-hoc 簽章
    codesign --force -s - "$app" >/dev/null 2>&1 || true
    return 0
}

# macOS 桌面的 .command 換成我們的 logo（Finder 的自訂圖示，寫在檔案的延伸屬性裡；內容照舊）
_mac_set_file_icon() {          # $1＝檔案  $2＝.icns
    osascript -l JavaScript - "$2" "$1" >/dev/null 2>&1 <<'JXA'
function run(argv) {
    ObjC.import('AppKit');
    var img = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
    return $.NSWorkspace.sharedWorkspace.setIconForFileOptions(img, argv[1], 0);
}
JXA
}

# 已經建好的捷徑還沒換上 logo（v2.26.1 以前建的，或 icons/ 那時還沒到）
_shortcut_icon_stale() {        # $1＝desktop／menu  $2＝捷徑路徑
    [ "$(uname -s)" = "Darwin" ] || return 1          # Linux 的圖示寫在 .desktop 內容裡，比內容就會發現
    local icns
    icns=$(_shortcut_icon icns) || return 1
    if [ "$1" = "menu" ]; then
        ! cmp -s "$icns" "$2/Contents/Resources/applet.icns"
    else
        ! xattr "$2" 2>/dev/null | grep -q com.apple.ResourceFork
    fi
}

# 判斷捷徑內容是不是最新的時要比的那個檔（.app 只比裡面的 .command：applet 每次產生不一定逐位元組相同）
_shortcut_content() {           # $1＝desktop／menu  $2＝捷徑路徑
    if [ "$1" = "menu" ] && [ "$(uname -s)" = "Darwin" ]; then
        printf '%s\n' "$2/Contents/Resources/webui.command"
    else
        printf '%s\n' "$2"
    fi
}

_write_shortcut() {             # $1＝desktop／menu  $2＝路徑
    if [ "$(uname -s)" = "Darwin" ]; then
        if [ "$1" = "menu" ]; then _write_mac_app "$2"; return; fi
        _write_mac_command "$2" || return 1
        local icns
        icns=$(_shortcut_icon icns) && { _mac_set_file_icon "$2" "$icns" || true; }
        return 0
    fi
    _write_linux_desktop_entry "$2" || return 1
    chmod +x "$2" || return 1
    # GNOME 桌面要標成「信任」才點得開；沒有圖形工作階段（例如從 SSH 安裝）時會失敗，第一次點的時候再允許
    command -v gio >/dev/null 2>&1 && gio set "$2" metadata::trusted true >/dev/null 2>&1 || true
    return 0
}

# 建過的捷徑還在就確認內容是最新的（安裝資料夾搬過、啟動方式改過）；刪掉了就不管
# 這個捷徑是不是這一份安裝的（2026-10-10）：指向這一份、或指向的資料夾已經不在（搬過家）才算；
# 指向另一份還在的安裝就不碰。以前位置上有捷徑就當成自己的，第二份安裝（測試副本）升級時把正式那份的
# 「應用程式」捷徑改成指向自己（macOS 的 /Applications 不在家目錄裡，連換掉 HOME 的升級模擬都擋不住）
_shortcut_ours() {              # $1＝desktop／menu  $2＝捷徑路徑
    local f t
    f=$(_shortcut_content "$1" "$2")
    [ -f "$f" ] || return 1
    if grep -q '^\[Desktop Entry\]' "$f" 2>/dev/null; then
        t=$(sed -n 's/^Path=//p' "$f" | head -1 | sed 's/\\\\/\\/g')
    else
        grep -qF "$(printf 'cd %q && exec' "$SCRIPT_DIR")" "$f" 2>/dev/null && return 0
        t=$(sed -n 's/^cd \(.*\) && exec .*/\1/p' "$f" | head -1)
        case "$t" in \$\'*|\"*|\'*) return 1 ;; esac          # printf %q 的 $'…'、引號形式不猜
        t=$(printf '%s' "$t" | sed 's/\\\(.\)/\1/g')          # 還原反斜線跳脫（空白、$ 等），不用 eval
    fi
    [ -n "$t" ] || return 1
    [ "$t" = "$SCRIPT_DIR" ] || [ ! -d "$t" ]
}

_refresh_shortcuts() {          # $@＝記錄的位置
    local loc path tmp
    for loc in "$@"; do
        path=$(_shortcut_path "$loc" find) || continue
        [ -e "$path" ] || continue
        _shortcut_ours "$loc" "$path" || continue           # 別份安裝的捷徑不碰
        tmp=$(mktemp -d) || continue
        # 期望的內容先寫到暫存檔（.app 只產生裡面那份 .command），跟現在的比；圖示另外看（v2.26.2 起是 logo）
        if [ "$(uname -s)" = "Darwin" ]; then
            _write_mac_command "$tmp/x"
        else
            _write_linux_desktop_entry "$tmp/x"
        fi
        if { [ -f "$tmp/x" ] && ! cmp -s "$tmp/x" "$(_shortcut_content "$loc" "$path")"; } \
                || _shortcut_icon_stale "$loc" "$path"; then
            _write_shortcut "$loc" "$path" && check_ok "已更新捷徑：$path"
        fi
        rm -rf "$tmp"
    done
    return 0
}

_shortcut_label() {             # 給使用者看的位置名稱
    case "$1" in
        desktop) echo "桌面" ;;
        menu) echo "「應用程式」" ;;
    esac
}

offer_desktop_shortcut() {
    [ "$(id -u)" -ne 0 ] || return 0            # root 的桌面不是使用者的桌面
    local state
    state=$(cat "$SHORTCUT_STATE_FILE" 2>/dev/null || true)
    [ "$state" = "no" ] && return 0
    if [ -n "$state" ]; then
        # shellcheck disable=SC2086
        _refresh_shortcuts $state
        return 0
    fi
    if [ -n "${JTLW_UPGRADE_QUIET:-}" ]; then            # start.sh 啟動時自動補檔：不問
        return 0
    fi
    local locs="desktop"
    if [ "$(uname -s)" = "Linux" ]; then
        [ "${LINUX_MODE:-desktop}" = "desktop" ] || return 0
        if [ -n "${SERVICE_FILE:-}" ] && [ -f "$SERVICE_FILE" ]; then return 0; fi   # 伺服器版
        _linux_has_gui || return 0
        _desktop_dir >/dev/null || return 0
    else
        locs="desktop menu"
    fi
    # 已經有了：這一份建的（或舊版留下的）當作建過、之後升級會更新；別人的（使用者自己做的、另一份安裝的）
    # 不問、不記、永遠不改（以前一律記成建過，下一次升級的「更新」就把它改成指向這一份）
    local loc p found="" other=""
    for loc in $locs; do
        p=$(_shortcut_path "$loc" find 2>/dev/null) || continue
        [ -e "$p" ] || continue
        if _shortcut_ours "$loc" "$p"; then found="$found $loc"; else other=1; fi
    done
    if [ -n "$found" ]; then
        printf '%s\n' "${found# }" > "$SHORTCUT_STATE_FILE" 2>/dev/null || true
        return 0
    fi
    [ -n "$other" ] && return 0
    # 只在有人可以回答時問。curl | bash 時標準輸入是管線，改從 /dev/tty 讀
    local tty_in
    if [ -t 0 ]; then
        tty_in=/dev/stdin
    elif { : </dev/tty; } 2>/dev/null; then
        tty_in=/dev/tty
    else
        return 0
    fi
    section "捷徑"
    local ans="" want="" tries=0
    if [ "$(uname -s)" = "Linux" ]; then
        echo -e "  ${C_WHITE}應用程式選單裡已經有 jt-live-whisper；也可以在桌面放一個，點兩下就以 WebUI（瀏覽器介面）模式啟動${NC}"
        if ! read -r -p "  是否建立桌面捷徑？(Y/n) " ans < "$tty_in"; then echo; return 0; fi
        case "$ans" in [Nn]*) want="no" ;; *) want="desktop" ;; esac
    else
        echo -e "  ${C_WHITE}建立 jt-live-whisper 捷徑，點兩下就以 WebUI（瀏覽器介面）模式啟動${NC}"
        echo -e "  ${C_WHITE}  [Enter] 桌面和「應用程式」都建立${NC}"
        echo -e "  ${C_WHITE}  [1] 只建立在桌面${NC}"
        echo -e "  ${C_WHITE}  [2] 只建立在「應用程式」（Launchpad、Spotlight 找得到）${NC}"
        echo -e "  ${C_WHITE}  [n] 都不要${NC}"
        while [ -z "$want" ]; do
            if ! read -r -p "  是否建立捷徑？選擇 [Enter] " ans < "$tty_in"; then echo; return 0; fi
            case "$ans" in
                ""|[Yy]*) want="desktop menu" ;;
                1) want="desktop" ;;
                2) want="menu" ;;
                [Nn]*) want="no" ;;
                *) tries=$((tries + 1)); [ $tries -ge 3 ] && return 0
                   echo -e "  ${C_DIM}請輸入 Enter、1、2 或 n${NC}" ;;
            esac
        done
    fi
    if [ "$want" = "no" ]; then
        printf 'no\n' > "$SHORTCUT_STATE_FILE" 2>/dev/null || true
        echo -e "  ${C_DIM}不建立；之後升級不會再問。想建立時刪掉 ${SHORTCUT_STATE_FILE}，再執行一次 ./install.sh${NC}"
        return 0
    fi
    local made="" path
    for loc in $want; do
        if path=$(_shortcut_path "$loc") && _write_shortcut "$loc" "$path"; then
            made="$made $loc"
            check_ok "已建立捷徑（$(_shortcut_label "$loc")）：$path"
        else
            check_fail "無法在$(_shortcut_label "$loc")建立捷徑${path:+：$path}"
            if [ "$(uname -s)" = "Darwin" ] && [ "$loc" = "desktop" ]; then
                echo -e "  ${C_DIM}終端機可能沒有「桌面」資料夾的存取權限：系統設定 → 隱私權與安全性 → 檔案與檔案夾${NC}"
            fi
        fi
    done
    # 至少建成一個才記下來；全部失敗的話下次再問
    [ -n "$made" ] && printf '%s\n' "${made# }" > "$SHORTCUT_STATE_FILE" 2>/dev/null
    if [ -n "$made" ] && [ "$(uname -s)" = "Linux" ]; then
        echo -e "  ${C_DIM}第一次點兩下時如果出現「不受信任的啟動器」，按右鍵選「允許啟動」${NC}"
    fi
    return 0
}

# 被 install-linux.sh 當函式庫載入時到此為止
if [ -n "${JTLW_INSTALL_LIB:-}" ]; then
    return 0
fi

# ─── 主流程 ──────────────────────────────────────
[ -z "${JTLW_UPGRADE_REPO:-}" ] && print_title      # 升級時交給新版接手的那一次不再印標題（同一個畫面）

# 處理 --upgrade 參數
if [ "$1" = "--upgrade" ]; then
    do_upgrade
    exit $?
fi

check_macos_version || exit 1
check_xcode_clt || exit 1
check_internet || exit 1
_complete_upgrade_files
check_running_processes || exit 1
check_disk_space || exit 1
check_homebrew || exit 1
check_brew_deps
check_sck
check_python || exit 1
check_whisper_cpp || {
    # whisper.cpp 只影響 whisper.cpp 即時辨識；mlx-whisper 與 faster-whisper 仍可用，不中止安裝
    echo -e "  ${C_DIM}即時辨識可改用 mlx-whisper（Apple Silicon）或 faster-whisper${NC}"
}
check_whisper_models
check_venv
check_moonshine
check_argos_model
check_nllb_model
check_faster_whisper_model
check_mlx_whisper
check_qwen_local_mac
check_nemotron_local
setup_remote_whisper
check_tts_local_mac
print_summary
offer_desktop_shortcut
