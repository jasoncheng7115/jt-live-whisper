#!/bin/bash
# 即時英翻中字幕系統 - 安裝腳本
# 檢查並安裝所有必要的依賴項目
# 支援一鍵安裝：curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh | bash
# Author: Jason Cheng (Jason Tools)

set -e

GITHUB_REPO="https://github.com/jasoncheng7115/jt-live-whisper.git"
GITHUB_RAW="https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main"

# ─── Bootstrap：透過 curl | bash 執行時，自動 clone 並安裝 ───
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
if [ ! -f "$SCRIPT_DIR/translate_meeting.py" ]; then
    echo ""
    echo -e "\033[38;2;100;180;255m============================================================\033[0m"
    echo -e "\033[38;2;100;180;255m\033[1m  jt-live-whisper - 一鍵安裝\033[0m"
    echo -e "\033[38;2;100;180;255m============================================================\033[0m"
    echo ""

    # 檢查 git
    if ! command -v git &>/dev/null; then
        echo -e "\033[38;2;255;220;80m[提醒] 需要 git，正在觸發 Xcode Command Line Tools 安裝...\033[0m"
        xcode-select --install 2>/dev/null || true
        echo -e "\033[38;2;255;255;255m安裝完成後請重新執行此指令。\033[0m"
        exit 1
    fi

    INSTALL_DIR="./jt-live-whisper"
    if [ -d "$INSTALL_DIR" ]; then
        echo -e "\033[38;2;255;255;255m目錄已存在: $INSTALL_DIR\033[0m"
        echo -e "\033[38;2;255;255;255m進入目錄執行安裝...\033[0m"
        cd "$INSTALL_DIR"
    else
        echo -e "\033[38;2;255;255;255m正在從 GitHub 下載 jt-live-whisper...\033[0m"
        git clone "$GITHUB_REPO" "$INSTALL_DIR"
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

# 偵測 ARM Homebrew Python（Moonshine 需要 ARM64 原生 Python）
if [ -x "/opt/homebrew/bin/python3.12" ]; then
    PYTHON_CMD="/opt/homebrew/bin/python3.12"
elif command -v python3.12 &>/dev/null; then
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

print_title() {
    echo ""
    echo -e "${C_TITLE}============================================================${NC}"
    echo -e "${C_TITLE}${BOLD}  jt-live-whisper v1.7.8 - 即時英翻中字幕系統 - 安裝程式${NC}"
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
        brew install "$pkg"
        if brew list --formula 2>/dev/null | grep -q "^${pkg}$"; then
            check_ok "$desc ($pkg) 安裝完成"
        else
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
        brew install --cask "$pkg"
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
    install_brew_formula "sdl2" "SDL2 音訊函式庫"
    install_brew_formula "ffmpeg" "FFmpeg 音訊轉檔工具"
    install_brew_cask "blackhole-2ch" "BlackHole 虛擬音訊"
}

# ─── Python ──────────────────────────────────────
check_python() {
    section "Python (ARM64)"

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

    # Intel Mac：用一般 Python
    if command -v "$PYTHON_CMD" &>/dev/null; then
        local ver
        ver=$("$PYTHON_CMD" --version 2>&1)
        check_ok "$ver ($PYTHON_CMD)"
        return 0
    else
        check_install "正在安裝 Python 3.12..."
        brew install python@3.12
        if command -v "$PYTHON_CMD" &>/dev/null; then
            check_ok "Python 3.12 安裝完成"
            return 0
        else
            check_fail "Python 3.12 安裝失敗，請手動安裝"
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
        git clone https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
        if [ $? -eq 0 ]; then
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

        # 偵測架構，選擇正確的 SDL2 路徑
        local arch
        arch=$(uname -m)
        local cmake_extra_flags=""
        if [ "$arch" = "arm64" ]; then
            # Apple Silicon: 使用 ARM Homebrew SDL2 + Metal
            if [ -d "/opt/homebrew/Cellar/sdl2" ]; then
                cmake_extra_flags="-DCMAKE_OSX_ARCHITECTURES=arm64 -DWHISPER_METAL=ON -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.5-a+fp16 -DCMAKE_PREFIX_PATH=/opt/homebrew"
            fi
        fi

        cmake -B build -DWHISPER_SDL2=ON $cmake_extra_flags 2>&1 | tail -3
        cmake --build build --target whisper-stream -j"$(sysctl -n hw.ncpu)" 2>&1 | tail -3
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

    if [ "$has_model" -eq 0 ]; then
        check_install "正在下載預設模型 (large-v3-turbo，約 809MB)..."
        cd "$WHISPER_DIR"
        bash models/download-ggml-model.sh large-v3-turbo
        cd "$SCRIPT_DIR"
        if [ -f "$MODELS_DIR/ggml-large-v3-turbo.bin" ]; then
            check_ok "ggml-large-v3-turbo.bin 下載完成"
        else
            check_fail "模型下載失敗，請手動下載"
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
        missing_pkgs+=("ctranslate2")
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
        missing_pkgs+=("numpy")
    fi
    if ! python3 -c "import faster_whisper" &>/dev/null 2>&1; then
        missing_pkgs+=("faster-whisper")
    fi
    if ! python3 -c "import resemblyzer" &>/dev/null 2>&1; then
        # resemblyzer 依賴 webrtcvad，webrtcvad 需要 pkg_resources（setuptools < 81）
        if ! python3 -c "import pkg_resources" &>/dev/null 2>&1; then
            pip install --quiet --disable-pip-version-check "setuptools<81" 2>&1 | tail -1
        fi
        missing_pkgs+=("resemblyzer")
    fi
    if ! python3 -c "import spectralcluster" &>/dev/null 2>&1; then
        missing_pkgs+=("spectralcluster")
    fi

    if [ ${#missing_pkgs[@]} -gt 0 ]; then
        check_install "正在安裝 Python 套件: ${missing_pkgs[*]}..."
        pip install --quiet --disable-pip-version-check "${missing_pkgs[@]}" 2>&1 | tail -1
        # 驗證（用 import 名稱，不是 pip 套件名稱）
        local all_ok=1
        for pkg in ctranslate2 sentencepiece opencc sounddevice numpy faster_whisper resemblyzer spectralcluster; do
            if python3 -c "import $pkg" &>/dev/null 2>&1; then
                check_ok "Python 套件: $pkg"
            else
                check_fail "Python 套件: $pkg 安裝失敗"
                all_ok=0
            fi
        done
    else
        check_ok "Python 套件: ctranslate2, sentencepiece, opencc, sounddevice, numpy, faster-whisper, resemblyzer, spectralcluster"
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
        pip install --quiet --disable-pip-version-check moonshine-voice 2>&1 | tail -1
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
            if python3 -c "
from moonshine_voice import get_model_for_language, ModelArch
path, arch = get_model_for_language('en', ModelArch.MEDIUM_STREAMING)
" 2>&1 | grep -v "^$" | tail -1; then
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
        pip install --quiet --disable-pip-version-check argostranslate 2>&1 | tail -1
        python3 -c "
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

# ─── 升級 ────────────────────────────────────────
do_upgrade() {
    section "從 GitHub 升級程式"

    # 檢查 git
    if ! command -v git &>/dev/null; then
        check_fail "找不到 git，請先安裝：brew install git"
        return 1
    fi

    # 建立暫存目錄
    local tmp_dir
    tmp_dir=$(mktemp -d)
    trap "rm -rf '$tmp_dir'" EXIT

    echo -e "  ${C_DIM}正在從 GitHub 下載最新版本...${NC}"
    if ! git clone --depth 1 "$GITHUB_REPO" "$tmp_dir/repo" 2>/dev/null; then
        check_fail "無法連接 GitHub，請檢查網路連線"
        return 1
    fi

    # 取得遠端版本號
    local remote_version
    remote_version=$(grep -m1 'APP_VERSION' "$tmp_dir/repo/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
    local local_version
    local_version=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')

    echo -e "  ${C_WHITE}目前版本: v${local_version:-未知}${NC}"
    echo -e "  ${C_WHITE}最新版本: v${remote_version:-未知}${NC}"

    if [ "$local_version" = "$remote_version" ]; then
        check_ok "已經是最新版本 (v${local_version})"
        return 0
    fi

    # 更新主要程式檔案
    local files_updated=0
    for fname in translate_meeting.py start.sh install.sh SOP.md; do
        if [ -f "$tmp_dir/repo/$fname" ]; then
            cp "$tmp_dir/repo/$fname" "$SCRIPT_DIR/$fname"
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

# ─── 總結 ────────────────────────────────────────
print_summary() {
    echo ""
    echo -e "${C_TITLE}============================================================${NC}"
    echo -e "${C_TITLE}${BOLD}  安裝結果${NC}"
    echo -e "${C_TITLE}============================================================${NC}"
    echo ""
    echo -e "  ${C_OK}通過: $passed${NC}"
    [ "$installed" -gt 0 ] && echo -e "  ${C_WARN}新安裝: $installed${NC}"
    [ "$failed" -gt 0 ] && echo -e "  ${C_ERR}失敗: $failed${NC}"
    echo ""

    if [ "$failed" -gt 0 ]; then
        echo -e "  ${C_ERR}有 $failed 個項目安裝失敗，請查看上方訊息修正後重新執行。${NC}"
        echo ""
        exit 1
    else
        echo -e "  ${C_OK}${BOLD}全部就緒！可以執行 ./start.sh 啟動系統。${NC}"
        echo ""
    fi
}

# ─── 主流程 ──────────────────────────────────────
print_title

# 處理 --upgrade 參數
if [ "$1" = "--upgrade" ]; then
    do_upgrade
    exit $?
fi

check_homebrew || exit 1
check_brew_deps
check_python || exit 1
check_whisper_cpp
check_whisper_models
check_venv
check_moonshine
check_argos_model
print_summary
