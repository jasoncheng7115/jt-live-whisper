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
if [ ! -f "$SCRIPT_DIR/translate_meeting.py" ] && [ "$_is_upgrade" = false ]; then
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
        unzip -q "$tmp_zip" -d "$tmp_extract"
        mkdir -p "$INSTALL_DIR"
        cp -R "$tmp_extract"/jt-live-whisper-main/* "$INSTALL_DIR/"
        rm -rf "$tmp_zip" "$tmp_extract"
        cd "$INSTALL_DIR"
    fi

    chmod +x install.sh start.sh
    exec ./install.sh "$@"
fi
# ─── Bootstrap 結束 ──────────────────────────────────────

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
    [ -n "$err_msg" ] && echo "  ${C_DIM}錯誤詳情: $err_msg${NC}"
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
    echo -e "${C_TITLE}${BOLD}  jt-live-whisper v2.14.6 - 100% 全地端 AI 語音工具集 - 安裝程式${NC}"
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
        echo -e "  ${C_WARN}[警告]${NC} whisper-stream 正在執行中（PID: $pids）"
        echo -e "  ${C_DIM}重新編譯可能失敗，建議先關閉${NC}"
        found=1
    fi
    # 檢查 translate_meeting.py
    pids=$(pgrep -f "translate_meeting.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo -e "  ${C_WARN}[警告]${NC} translate_meeting.py 正在執行中（PID: $pids）"
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
    # SDL 音訊函式庫：優先 SDL2（whisper.cpp 目前使用），未來 SDL3 可用時自動切換
    if brew list --formula 2>/dev/null | grep -q "^sdl2$"; then
        check_ok "SDL2 音訊函式庫 (sdl2)"
    elif brew list --formula 2>/dev/null | grep -q "^sdl3$"; then
        check_ok "SDL3 音訊函式庫 (sdl3)"
        echo -e "  ${C_WARN}[注意]${NC} whisper.cpp 尚未正式支援 SDL3，若編譯失敗請安裝 SDL2: brew install sdl2"
    else
        # 兩者都沒有，嘗試安裝 SDL2
        install_brew_formula "sdl2" "SDL2 音訊函式庫"
        # SDL2 安裝失敗時嘗試 SDL3（未來 Homebrew 可能只有 SDL3）
        if ! brew list --formula 2>/dev/null | grep -q "^sdl2$"; then
            echo -e "  ${C_WARN}[備選]${NC} SDL2 安裝失敗，嘗試安裝 SDL3..."
            install_brew_formula "sdl3" "SDL3 音訊函式庫"
        fi
    fi
    install_brew_formula "ffmpeg" "FFmpeg 音訊轉檔工具"
    install_brew_cask "blackhole-2ch" "BlackHole 虛擬音訊"
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

        # 偵測 SDL 版本（優先 SDL2，未來相容 SDL3）
        local sdl_cmake_flag="-DWHISPER_SDL2=ON"
        local sdl_prefix=""
        if brew list --formula 2>/dev/null | grep -q "^sdl2$"; then
            sdl_cmake_flag="-DWHISPER_SDL2=ON"
            echo -e "  ${C_DIM}使用 SDL2${NC}"
        elif brew list --formula 2>/dev/null | grep -q "^sdl3$"; then
            # whisper.cpp 未來可能用 -DWHISPER_SDL3=ON，先嘗試
            sdl_cmake_flag="-DWHISPER_SDL3=ON"
            echo -e "  ${C_WARN}使用 SDL3（實驗性）${NC}"
        fi

        # 偵測架構
        local arch
        arch=$(uname -m)
        local cmake_extra_flags=""
        if [ "$arch" = "arm64" ]; then
            # Apple Silicon: ARM Homebrew + Metal
            if [ -d "/opt/homebrew/Cellar/sdl2" ] || [ -d "/opt/homebrew/Cellar/sdl3" ]; then
                cmake_extra_flags="-DCMAKE_OSX_ARCHITECTURES=arm64 -DWHISPER_METAL=ON -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.5-a+fp16 -DCMAKE_PREFIX_PATH=/opt/homebrew"
            fi
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

# ─── Python venv ─────────────────────────────────
check_venv() {
    section "Python 虛擬環境"

    local need_create=0
    if [ ! -d "$VENV_DIR" ]; then
        need_create=1
    else
        # 檢查 venv 是否可用（路徑搬遷後會壞）
        if ! "$VENV_DIR/bin/python3" --version &>/dev/null 2>&1; then
            echo -e "  ${C_WARN}[偵測]${NC} venv 已損壞（可能路徑已變更），需重建"
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
    if ! python3 -c "import multipart" &>/dev/null 2>&1; then
        missing_pkgs+=("python-multipart")
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
from argostranslate import package
package.update_package_index()
pkgs = package.get_available_packages()
en_zh = next((p for p in pkgs if p.from_code == 'en' and p.to_code == 'zh'), None)
if en_zh:
    path = en_zh.download()
    package.install_from_path(path)
    print('OK')
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
    section "NLLB 離線翻譯模型（中日英互譯，CC-BY-NC 4.0 授權）"

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

# ─── faster-whisper 模型預下載 ──────────────────────────────
check_faster_whisper_model() {
    # Apple Silicon → large-v3-turbo（Metal 加速）、Intel → small（CPU 夠用）
    local fw_model
    if [ "$(uname -m)" = "arm64" ]; then
        fw_model="large-v3-turbo"
    else
        fw_model="small"
    fi

    # bash 3.2 (macOS) bug: 全形括號（）內嵌變數會截斷，需拼接繞過
    _fw_section="faster-whisper 模型預下載"
    _fw_section="${_fw_section}（${fw_model}）"
    section "$_fw_section"

    source "$VENV_DIR/bin/activate"

    # 確保 huggingface_hub 已安裝
    pip install --disable-pip-version-check -q huggingface_hub 2>/dev/null

    local fw_found
    fw_found=$(python3 -c "
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
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-$fw_model')):
            found = True
            break
    if found:
        break
print('found' if found else 'notfound')
" 2>/dev/null)

    if [ "$fw_found" = "found" ]; then
        check_ok "faster-whisper 模型 $fw_model 已存在"
    else
        local fw_size
        if [ "$fw_model" = "large-v3-turbo" ]; then
            fw_size="約 1.6GB"
        else
            fw_size="約 500MB"
        fi
        check_install "正在下載 faster-whisper $fw_model 模型（$fw_size）..."
        echo ""
        # mobiuslabsgmbh 是 faster-whisper 內部預設的 repo（Systran 已需認證）
        hf_download "mobiuslabsgmbh/faster-whisper-$fw_model" "faster-whisper 模型" ""
        echo ""

        # 驗證下載
        fw_found=$(python3 -c "
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
        if os.path.isdir(os.path.join(d, 'models--' + prefix + '--faster-whisper-$fw_model')):
            found = True
            break
    if found:
        break
print('found' if found else 'notfound')
" 2>/dev/null)

        if [ "$fw_found" = "found" ]; then
            check_ok "faster-whisper 模型 $fw_model 安裝完成"
        else
            check_fail "faster-whisper 模型下載失敗"
            echo -e "  ${C_DIM}  請確認網路連線後重新執行安裝${NC}"
        fi
    fi

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

# ─── 升級 ────────────────────────────────────────
do_upgrade() {
    section "從 GitHub 升級程式"

    # 建立暫存目錄
    local tmp_dir
    tmp_dir=$(mktemp -d)
    trap "rm -rf '$tmp_dir'" EXIT

    echo -e "  ${C_DIM}正在從 GitHub 下載最新版本...${NC}"
    local zip_path="$tmp_dir/jt-live-whisper.zip"
    if ! curl -fsSL "https://github.com/jasoncheng7115/jt-live-whisper/archive/refs/heads/main.zip" -o "$zip_path"; then
        check_fail "無法連接 GitHub，請檢查網路連線"
        return 1
    fi
    unzip -q "$zip_path" -d "$tmp_dir"
    local repo_dir="$tmp_dir/jt-live-whisper-main"

    if [ ! -f "$repo_dir/translate_meeting.py" ]; then
        check_fail "下載的檔案不完整，請檢查網路連線"
        return 1
    fi

    # 取得伺服器版本號
    local remote_version
    remote_version=$(grep -m1 'APP_VERSION' "$repo_dir/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
    local local_version
    local_version=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')

    echo -e "  ${C_WHITE}目前版本: v${local_version:-未知}${NC}"
    echo -e "  ${C_WHITE}最新版本: v${remote_version:-未知}${NC}"

    if [ "$local_version" = "$remote_version" ]; then
        # 版本相同但檢查是否缺少檔案
        _missing=""
        for _chk in webui.py webui.html; do
            [ ! -f "$SCRIPT_DIR/$_chk" ] && _missing="$_missing $_chk"
        done
        if [ -n "$_missing" ]; then
            echo -e "  ${C_WARN}版本相同但缺少檔案，補充安裝中...${NC}"
            for _uf in translate_meeting.py start.sh start.ps1 install.sh install.ps1 SOP.md webui.py webui.html; do
                [ -f "$repo_dir/$_uf" ] && cp "$repo_dir/$_uf" "$SCRIPT_DIR/$_uf"
            done
            check_ok "已補充安裝缺少的檔案（${_missing}）"
        else
            check_ok "已經是最新版本 (v${local_version})"
        fi
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

    # 更新主要程式檔案
    local files_updated=0
    for fname in translate_meeting.py start.sh install.sh SOP.md webui.py webui.html; do
        if [ -f "$repo_dir/$fname" ]; then
            cp "$repo_dir/$fname" "$SCRIPT_DIR/$fname"
            ((files_updated++)) || true
        fi
    done

    # 確保腳本可執行
    chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/install.sh" 2>/dev/null

    check_ok "已升級 v${local_version} → v${remote_version}（更新 ${files_updated} 個檔案）"
    echo ""
    echo -e "  ${C_WARN}建議重新執行 ./install.sh 確認相依套件完整${NC}"
    return 0
}

# ─── 從原始碼編譯 CTranslate2（aarch64 CUDA）──────────────
# 用法：_build_ctranslate2_from_source "$ssh_opts" "$rw_user" "$rw_host"
# 回傳：0=成功  1=失敗（呼叫端應降級 openai-whisper）
_build_ctranslate2_from_source() {
    local ssh_opts="$1" rw_user="$2" rw_host="$3"
    local REMOTE_PIP="~/jt-whisper-server/venv/bin/pip"
    local REMOTE_PY="~/jt-whisper-server/venv/bin/python3"
    local WHEEL_CACHE="~/jt-whisper-server/.ct2-wheels"
    local BUILD_DIR="/tmp/ctranslate2-build"

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
            ct2_cuda=$(ssh $ssh_opts "$rw_user@$rw_host" "LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH ${REMOTE_PY} -c \"
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
    local CUDA_ENV="export PATH=${cuda_bin_dir}:\$PATH && export LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH"
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
            -DCMAKE_INSTALL_PREFIX=/usr/local \
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

    # 4d. make install + ldconfig
    if ! run_spinner "  [4/7] 安裝系統函式庫（make install + ldconfig）..." ssh $ssh_opts "$rw_user@$rw_host" "
        ${CUDA_ENV} && \
        cd ${BUILD_DIR}/src/build && make install 2>&1 && ldconfig 2>&1
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

    # ── 6. 確認 libctranslate2.so 已註冊 ──
    local lib_check
    lib_check=$(ssh $ssh_opts "$rw_user@$rw_host" "ldconfig -p 2>/dev/null | grep -c libctranslate2" 2>/dev/null)
    if [ "$lib_check" -gt 0 ] 2>/dev/null; then
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

                # 3. venv
                if ssh $chk_opts "$existing_user@$existing_host" "~/jt-whisper-server/venv/bin/python3 --version" &>/dev/null; then
                    check_ok "venv 正常"
                else
                    echo -e "  ${C_WARN}[缺少]${NC} venv 損壞或不存在"
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
                    scp $scp_chk_opts "$SCRIPT_DIR/remote_whisper_server.py" "$existing_user@$existing_host:~/jt-whisper-server/server.py" &>/dev/null
                    if [ $? -eq 0 ]; then
                        # 重啟伺服器以載入新版程式
                        ssh $chk_opts "$existing_user@$existing_host" "pkill -f 'server.py --port' 2>/dev/null" &>/dev/null || true
                        check_ok "server.py 已同步更新（已重啟伺服器，執行程式時自動載入新版）"
                    fi
                fi
            fi
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
        read -r do_repair
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
        read -r setup_remote
        if [[ ! "$setup_remote" =~ ^[Yy]$ ]]; then
            echo -e "  ${C_DIM}跳過伺服器設定${NC}"
            return 0
        fi

        # 收集 SSH 連線資訊
        echo ""
        echo -ne "  ${C_WHITE}SSH 伺服器 IP: ${NC}"
        read -r rw_host
        if [ -z "$rw_host" ]; then
            echo -e "  ${C_DIM}未輸入，跳過${NC}"
            return 0
        fi

        echo -ne "  ${C_WHITE}SSH Port [22]: ${NC}"
        read -r rw_ssh_port
        rw_ssh_port=${rw_ssh_port:-22}

        echo -ne "  ${C_WHITE}SSH 使用者: ${NC}"
        read -r rw_user
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
        read -r rw_key_input
        if [ -n "$rw_key_input" ]; then
            rw_key="$rw_key_input"
        fi

        echo -ne "  ${C_WHITE}Whisper 服務 Port [8978]: ${NC}"
        read -r rw_port
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
        check_fail "SSH 連線失敗（$rw_user@$rw_host:$rw_ssh_port）"
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

    # 建立 venv
    ssh $ssh_opts "$rw_user@$rw_host" "
        mkdir -p ~/jt-whisper-server
        if [ ! -d ~/jt-whisper-server/venv ]; then
            python3 -m venv ~/jt-whisper-server/venv
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
                'setuptools<81' faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 2>&1
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
                'setuptools<81' ctranslate2 faster-whisper fastapi uvicorn python-multipart resemblyzer spectralcluster 2>&1
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
    ssh $ssh_opts "$rw_user@$rw_host" "
        cd ~/jt-whisper-server
        export LD_LIBRARY_PATH=/usr/local/lib:\$LD_LIBRARY_PATH
        nohup venv/bin/python3 server.py --port $rw_port > /tmp/jt-whisper-server.log 2>&1 &
        echo \$!
    " > /tmp/rw_pid.txt 2>/dev/null

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

    # 停止測試 server
    ssh $ssh_opts "$rw_user@$rw_host" "pkill -f 'server.py --port $rw_port'" &>/dev/null

    if [ "$health_ok" -eq 0 ]; then
        echo ""
        check_ok "伺服器測試成功"
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

    # resemblyzer
    if python3 -c "import resemblyzer" &>/dev/null 2>&1; then
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
        echo -e "  ${C_OK}■${NC} NLLB 離線翻譯  ${C_DIM}中日英互譯${NC}"
    else
        echo -e "  ${C_DIM}□ NLLB 離線翻譯  中日英互譯${NC}"
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

# ─── 主流程 ──────────────────────────────────────
print_title

# 處理 --upgrade 參數
if [ "$1" = "--upgrade" ]; then
    do_upgrade
    exit $?
fi

check_macos_version || exit 1
check_xcode_clt || exit 1
check_internet || exit 1
check_running_processes || exit 1
check_disk_space || exit 1
check_homebrew || exit 1
check_brew_deps
check_python || exit 1
check_whisper_cpp
check_whisper_models
check_venv
check_moonshine
check_argos_model
check_nllb_model
check_faster_whisper_model
check_mlx_whisper
setup_remote_whisper
print_summary
