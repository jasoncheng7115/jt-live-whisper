#!/bin/bash
# 即時英翻中字幕系統 - 啟動腳本
# Author: Jason Cheng (Jason Tools)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# 24-bit 真彩色
C_TITLE='\033[38;2;100;180;255m'   # 藍色
C_OK='\033[38;2;80;255;120m'       # 綠色
C_WARN='\033[38;2;255;220;80m'     # 黃色
C_ERR='\033[38;2;255;100;100m'     # 紅色
C_DIM='\033[38;2;100;100;100m'     # 暗灰
C_WHITE='\033[38;2;255;255;255m'   # 白色
BOLD='\033[1m'
NC='\033[0m'

echo ""
_COLS=$(tput cols 2>/dev/null || echo 60)
[ "$_COLS" -lt 40 ] && _COLS=40
_LINE=$(printf '%*s' "$_COLS" '' | tr ' ' '=')
echo -e "${C_TITLE}${_LINE}${NC}"
echo -e "${C_TITLE}${BOLD}  jt-live-whisper v2.1.3 - 100% 全地端 AI 語音工具集${NC}"
echo -e "${C_TITLE}  by Jason Cheng (Jason Tools)${NC}"
echo -e "${C_TITLE}${_LINE}${NC}"
echo ""

# 背景檢查 GitHub 新版本（不阻塞啟動流程）
_UPDATE_TMP=$(mktemp /tmp/jt-update.XXXXXX 2>/dev/null || echo "")
if [ -n "$_UPDATE_TMP" ]; then
    trap 'rm -f "$_UPDATE_TMP" 2>/dev/null' EXIT
    (
        _rv=$(curl -s --max-time 3 -r 0-10240 \
            "https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/translate_meeting.py" \
            2>/dev/null | grep -m1 'APP_VERSION' | sed 's/.*"\(.*\)".*/\1/')
        echo "$_rv" > "$_UPDATE_TMP" 2>/dev/null
    ) &
    _UPDATE_PID=$!
fi

# --input 和 --summarize 模式不需要 BlackHole
SKIP_BLACKHOLE=0
for arg in "$@"; do
    if [ "$arg" = "--input" ] || [ "$arg" = "--summarize" ] || [ "$arg" = "--diarize" ]; then
        SKIP_BLACKHOLE=1
        break
    fi
done

# 檢查音訊裝置
if [ "$SKIP_BLACKHOLE" -eq 0 ]; then
    AUDIO_INFO=$(system_profiler SPAudioDataType 2>/dev/null)
    HAS_BLACKHOLE=0
    HAS_MULTIOUT=0
    HAS_AGGREGATE=0
    echo "$AUDIO_INFO" | grep -qi "blackhole" && HAS_BLACKHOLE=1
    echo "$AUDIO_INFO" | grep -qiE "multi.output|多重輸出" && HAS_MULTIOUT=1
    # 聚集裝置偵測：名稱匹配 或 Input Channels >= 3（使用者可能改過名稱）
    echo "$AUDIO_INFO" | grep -qiE "aggregate|聚集" && HAS_AGGREGATE=1
    if [ "$HAS_AGGREGATE" -eq 0 ]; then
        echo "$AUDIO_INFO" | grep -qE "Input Channels: [3-9]" && HAS_AGGREGATE=1
    fi

    MISSING=""
    if [ "$HAS_BLACKHOLE" -eq 0 ]; then
        echo -e "${C_ERR}[缺少] BlackHole 2ch 虛擬音訊裝置${NC}"
        echo -e "  ${C_DIM}brew install --cask blackhole-2ch（安裝後需重新啟動電腦）${NC}"
        MISSING="1"
    fi
    if [ "$HAS_MULTIOUT" -eq 0 ]; then
        echo -e "${C_WARN}[缺少] 多重輸出裝置（Multi-Output Device）${NC}"
        echo -e "  ${C_DIM}音訊 MIDI 設定 → + → 建立多重輸出裝置 → 勾選喇叭/耳機 + BlackHole 2ch${NC}"
        MISSING="1"
    fi
    if [ "$HAS_AGGREGATE" -eq 0 ]; then
        echo -e "${C_WARN}[缺少] 聚集裝置（Aggregate Device）— 錄音時需要${NC}"
        echo -e "  ${C_DIM}音訊 MIDI 設定 → + → 建立聚集裝置 → 勾選 BlackHole 2ch + 麥克風${NC}"
        MISSING="1"
    fi

    if [ -n "$MISSING" ]; then
        echo ""
        echo -e "${C_WHITE}詳細設定方式請參考 SOP.md 第二章「事前準備：macOS 音訊設定」${NC}"
        echo ""
        read -p "是否仍然繼續？(y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 0
        fi
    fi
fi

# 檢查 venv
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${C_ERR}[錯誤] 找不到 Python 虛擬環境: $VENV_DIR${NC}"
    echo "請先執行安裝步驟。"
    exit 1
fi

# 啟用 venv 並執行
source "$VENV_DIR/bin/activate"

echo -e "${C_OK}Python 環境已啟用${NC}"

# 顯示新版本提醒（背景 curl 應已完成）
if [ -n "$_UPDATE_PID" ]; then
    # 最多等候 1.5 秒，避免無網路時阻塞啟動
    for _i in 1 2 3; do
        kill -0 "$_UPDATE_PID" 2>/dev/null || break
        sleep 0.5
    done
    if ! kill -0 "$_UPDATE_PID" 2>/dev/null; then
        wait "$_UPDATE_PID" 2>/dev/null
        if [ -f "$_UPDATE_TMP" ]; then
            _remote_ver=$(cat "$_UPDATE_TMP" 2>/dev/null)
            rm -f "$_UPDATE_TMP"
            _local_ver=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
            if [ -n "$_remote_ver" ] && [ -n "$_local_ver" ] && [ "$_local_ver" != "$_remote_ver" ]; then
                if [ "$(printf '%s\n' "$_local_ver" "$_remote_ver" | sort -V | tail -n1)" = "$_remote_ver" ]; then
                    echo ""
                    echo -e "${C_WARN}  有新版本可用: v${_local_ver} → v${_remote_ver}${NC}"
                    echo -e "${C_DIM}  升級指令: $SCRIPT_DIR/install.sh --upgrade${NC}"
                fi
            fi
        fi
    fi
fi

echo ""

python3 "$SCRIPT_DIR/translate_meeting.py" "$@"

# 安全網：確保終端機恢復正常（防止 Ctrl+S raw mode 殘留）
stty sane 2>/dev/null
