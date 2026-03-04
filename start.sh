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
echo -e "${C_TITLE}============================================================${NC}"
echo -e "${C_TITLE}${BOLD}  jt-live-whisper v1.7.8 - 即時英翻中字幕系統${NC}"
echo -e "${C_TITLE}  by Jason Cheng (Jason Tools)${NC}"
echo -e "${C_TITLE}============================================================${NC}"
echo ""

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
echo ""

python3 "$SCRIPT_DIR/translate_meeting.py" "$@"

# 安全網：確保終端機恢復正常（防止 Ctrl+S raw mode 殘留）
stty sane 2>/dev/null
