#!/usr/bin/env python3
"""
即時英文語音轉繁體中文字幕
透過 BlackHole 虛擬音訊裝置捕捉音訊，
使用 whisper.cpp stream 即時轉錄，再翻譯成繁體中文。

Author: Jason Cheng (Jason Tools)
"""

import argparse
import atexit
import io
import math
import os
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import wave
from collections import deque

# 避免 OpenMP 重複載入衝突
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
# 抑制 Intel MKL SSE4.2 棄用警告（Apple Silicon + Rosetta 會觸發）
os.environ["MKL_SERVICE_FORCE_INTEL"] = "1"

import json
import urllib.request

import ctranslate2
import opencc
import sentencepiece

# 簡體→台灣繁體轉換器（僅字元層級轉換，不做詞組匹配避免誤轉如「裡面包含」→「裡麵包含」）
# 詞組層級的轉換（如 面包→麵包）交由 LLM 在翻譯時直接輸出正確繁體
_S2TW_RAW = opencc.OpenCC("s2tw")

import re
# s2tw 字元層級會把簡體「只」一律轉成「隻」，但副詞用法應為「只」
# 正則修正：「隻」前面不是量詞語境（數字/幾/兩/這/那/每/各）時，還原為「只」
_RE_ZHI_FIX = re.compile(r"(?<![零一二三四五六七八九十百千萬幾兩這那每各\d])隻(?![眼耳手腳腿])")


def _s2tw(text):
    """簡體→台灣繁體，含已知誤轉後處理"""
    result = _S2TW_RAW.convert(text)
    # 正則修正：非量詞語境的「隻」→「只」
    result = _RE_ZHI_FIX.sub("只", result)
    return result


# 相容舊名
S2TWP = type("_S2TWProxy", (), {"convert": staticmethod(_s2tw)})()

# Moonshine ASR（選用，未安裝時自動降級為 Whisper only）
_MOONSHINE_AVAILABLE = False
try:
    from moonshine_voice import get_model_for_language, ModelArch
    from moonshine_voice.transcriber import Transcriber, TranscriptEventListener
    import sounddevice as sd
    import numpy as np
    _MOONSHINE_AVAILABLE = True
except ImportError:
    pass

# 終端格式（24-bit 真彩色 + 格式）
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"
RESET = "\x1b[0m"
# 24-bit 真彩色
C_TITLE = "\x1b[38;2;100;180;255m"   # 藍色 - 標題
C_HIGHLIGHT = "\x1b[38;2;255;220;80m" # 黃色 - 重點/預設
C_EN = "\x1b[38;2;180;180;180m"       # 灰色 - 英文原文
C_ZH = "\x1b[38;2;80;255;180m"        # 青綠 - 中文翻譯
C_OK = "\x1b[38;2;80;255;120m"        # 綠色 - 成功
C_DIM = "\x1b[38;2;100;100;100m"      # 暗灰 - 次要資訊
C_WHITE = "\x1b[38;2;255;255;255m"    # 白色 - 一般文字
# 速度標籤（背景色 + 黑字，不用 REVERSE 以避免換行時色塊延伸）
C_BADGE_FAST = "\x1b[48;2;80;255;120m\x1b[38;2;0;0;0m"    # 綠底黑字 < 1s
C_BADGE_NORMAL = "\x1b[48;2;255;220;80m\x1b[38;2;0;0;0m"  # 黃底黑字 1-3s
C_BADGE_SLOW = "\x1b[48;2;255;100;100m\x1b[38;2;0;0;0m"   # 紅底黑字 > 3s


def _str_display_width(s):
    """計算字串可見寬度（去除 ANSI 跳脫碼，CJK/全形算 2 格）"""
    w = 0
    in_esc = False
    for c in s:
        if c == '\x1b':
            in_esc = True
            continue
        if in_esc:
            if c == 'm':
                in_esc = False
            continue
        if ('\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f'
                or '\uff00' <= c <= '\uffef' or '\u3400' <= c <= '\u4dbf'):
            w += 2
        else:
            w += 1
    return w


def _print_with_badge(text, badge_color, elapsed):
    """輸出翻譯文字 + 速度 badge，避免 badge 換行導致背景色延伸整行"""
    badge_str = f" {elapsed:.1f}s "
    badge_len = len(badge_str)
    text_width = _str_display_width(text)
    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 80
    cursor_col = text_width % cols
    if cursor_col + 2 + badge_len > cols:
        # badge 放不下，換行後縮排顯示
        print(f"{text}\n    {badge_color}{badge_str}{RESET}", flush=True)
    else:
        print(f"{text}  {badge_color}{badge_str}{RESET}", flush=True)


# 講者辨識色彩（8 色循環，24-bit 真彩色）
SPEAKER_COLORS = [
    "\x1b[38;2;255;165;80m",   # 橘色
    "\x1b[38;2;100;200;255m",  # 天藍
    "\x1b[38;2;255;150;180m",  # 粉紅
    "\x1b[38;2;180;230;100m",  # 黃綠
    "\x1b[38;2;190;160;255m",  # 淡紫
    "\x1b[38;2;255;240;100m",  # 亮黃
    "\x1b[38;2;100;240;200m",  # 薄荷綠
    "\x1b[38;2;255;180;160m",  # 淺珊瑚
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
RECORDING_DIR = os.path.join(SCRIPT_DIR, "recordings")
WHISPER_STREAM = os.path.join(SCRIPT_DIR, "whisper.cpp", "build", "bin", "whisper-stream")
MODELS_DIR = os.path.join(SCRIPT_DIR, "whisper.cpp", "models")
ARGOS_PKG_PATH = os.path.expanduser(
    "~/.local/share/argos-translate/packages/translate-en_zh-1_9"
)

# LLM 伺服器設定（預設無，由 config.json 或 --llm-host 指定）
OLLAMA_DEFAULT_HOST = None
OLLAMA_DEFAULT_PORT = 11434
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    """讀取設定檔，回傳 dict"""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception:
            pass
    return {}


def save_config(cfg):
    """儲存設定檔"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


_config = load_config()
# 向後相容：先讀新欄位 llm_host，再讀舊欄位 ollama_host
OLLAMA_HOST = _config.get("llm_host", _config.get("ollama_host", OLLAMA_DEFAULT_HOST))
OLLAMA_PORT = _config.get("llm_port", _config.get("ollama_port", OLLAMA_DEFAULT_PORT))

# 遠端 GPU Whisper 辨識
REMOTE_WHISPER_DEFAULT_PORT = 8978
REMOTE_WHISPER_CONFIG = _config.get("remote_whisper", None)

# 錄音輸出格式（預設 mp3，支援 mp3/ogg/flac/wav）
RECORDING_FORMAT = _config.get("recording_format", "mp3")
if RECORDING_FORMAT not in ("mp3", "ogg", "flac", "wav"):
    RECORDING_FORMAT = "mp3"

# 內建翻譯模型（作者篩選推薦）
_BUILTIN_TRANSLATE_MODELS = [
    ("phi4:14b", "Microsoft，品質最好"),
    ("qwen2.5:14b", "品質好，速度快（推薦）"),
    ("qwen2.5:7b", "品質普通，速度最快"),
]

# 合併使用者自訂翻譯模型（config.json 的 translate_models）
_user_translate = _config.get("translate_models", [])
OLLAMA_MODELS = list(_BUILTIN_TRANSLATE_MODELS)
_existing_names = {n for n, _ in OLLAMA_MODELS}
for item in _user_translate:
    if isinstance(item, dict) and "name" in item:
        name = item["name"]
        if name not in _existing_names:
            OLLAMA_MODELS.append((name, item.get("desc", "")))
            _existing_names.add(name)

# 功能模式
MODE_PRESETS = [
    ("en2zh", "英翻中字幕", "英文語音 → 翻譯成繁體中文"),
    ("zh2en", "中翻英字幕", "中文語音 → 翻譯成英文"),
    ("en", "英文轉錄", "英文語音 → 直接顯示英文"),
    ("zh", "中文轉錄", "中文語音 → 直接顯示繁體中文"),
    ("record", "純錄音", "僅錄製音訊為 WAV 檔"),
]

# 可用的 whisper 模型（由小到大）
WHISPER_MODELS = [
    ("base.en", "ggml-base.en.bin", "最快，準確度一般"),
    ("small.en", "ggml-small.en.bin", "快，準確度好"),
    ("large-v3-turbo", "ggml-large-v3-turbo.bin", "快，準確度很好（推薦）"),
    ("medium.en", "ggml-medium.en.bin", "較慢，準確度很好"),
    ("large-v3", "ggml-large-v3.bin", "最慢，中文品質最好，有獨立 GPU 可選用"),
]

# 使用場景預設參數 (length_ms, step_ms, 說明)
SCENE_PRESETS = [
    ("線上會議", 5000, 3000, "對話短句，反應快（5秒）"),
    ("教育訓練", 8000, 3000, "長句連續講述，翻譯更完整（8秒）"),
    ("快速字幕", 3000, 2000, "最低延遲，適合即時展示（3秒）"),
]

# Moonshine 串流模型（僅英文）
MOONSHINE_MODELS = [
    ("medium", "最準確，延遲 ~300ms（推薦）", "245MB"),
    ("small", "快速，延遲 ~150ms", "123MB"),
    ("tiny", "最快，延遲 ~50ms", "34MB"),
]

# ASR 引擎選項
ASR_ENGINES = [
    ("whisper", "Whisper", "高準確度，完整斷句，支援中英文（推薦）"),
    ("moonshine", "Moonshine", "真串流，低延遲，僅英文"),
]

APP_VERSION = "2.1.3"

# 常見 LLM 伺服器預設 port（供參考）
LLM_PRESETS = [
    ("Ollama",              "localhost:11434"),
    ("LM Studio",           "localhost:1234"),
    ("Jan.ai",              "localhost:1337"),
    ("vLLM",                "localhost:8000"),
    ("LocalAI / llama.cpp", "localhost:8080"),
    ("LiteLLM",             "localhost:4000"),
]

# 摘要功能設定
SUMMARY_DEFAULT_MODEL = "gpt-oss:120b"
_BUILTIN_SUMMARY_MODELS = [
    ("gpt-oss:120b", "品質最好（推薦）"),
    ("gpt-oss:20b", "速度快，品質一般"),
]

# 合併使用者自訂摘要模型（config.json 的 summary_models）
_user_summary = _config.get("summary_models", [])
SUMMARY_MODELS = list(_BUILTIN_SUMMARY_MODELS)
_existing_summary = {n for n, _ in SUMMARY_MODELS}
for item in _user_summary:
    if isinstance(item, dict) and "name" in item:
        name = item["name"]
        if name not in _existing_summary:
            SUMMARY_MODELS.append((name, item.get("desc", "")))
            _existing_summary.add(name)
# 分段門檻的保底值（查不到模型 context window 時使用）
SUMMARY_CHUNK_FALLBACK_CHARS = 6000
# prompt 模板 + 回應預留的 token 數（不算逐字稿本身）
SUMMARY_PROMPT_OVERHEAD_TOKENS = 2000

SUMMARY_PROMPT_TEMPLATE = """\
你是專業的會議記錄整理員。請根據以下即時轉錄的逐字稿，完成兩件事：

1. **重點摘要**：列出 5-10 個重點，每個重點用一句話概述。
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的段落文字。合併斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。**必須完整輸出所有內容，嚴禁以「以下略」「篇幅限制」「內容省略」等理由截斷或跳過任何段落。**

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

## 校正逐字稿

（整理成流暢段落的純文字逐字稿，不要使用 markdown 格式，不要逐行列出，要合併成自然的段落）

規則：
- 逐字稿中 [EN] 標記的是英文原文語音辨識結果，[中] 標記的是中文翻譯。校正時請以中文翻譯為主，參考英文原文修正翻譯錯誤
- 全部使用台灣繁體中文
- 使用台灣用語（軟體、網路、記憶體、程式、伺服器等）
- 專有名詞維持英文原文
- **嚴禁**加入原文沒有的內容，不要自行編造開場白、結語、總結語句或任何原文未出現的話語
- **嚴禁**截斷或省略逐字稿內容，不可使用「以下略」「篇幅限制略去」「內容省略」等說法跳過任何段落，必須從頭到尾完整輸出
- 不要逐行標註時間戳記或逐行對照英中文，直接輸出流暢的中文段落

以下是逐字稿：
---
{transcript}
---
"""

SUMMARY_PROMPT_DIARIZE_TEMPLATE = """\
你是專業的會議記錄整理員。請根據以下含有講者標記的逐字稿，完成兩件事：

1. **重點摘要**：列出 5-10 個重點，每個重點用一句話概述。
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的對話文字。合併同一位講者的連續斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。**必須完整輸出所有內容，嚴禁以「以下略」「篇幅限制」「內容省略」等理由截斷或跳過任何段落。**

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

## 校正逐字稿

Speaker 1：整理後的這段話內容。

Speaker 2：整理後的這段話內容。

Speaker 2：同一位講者的下一段話，仍然必須標注 Speaker 2。

Speaker 1：整理後的這段話內容。

...

規則：
- **最重要**：每一個段落開頭都必須標注講者（Speaker N：），絕對不可省略，即使連續多段都是同一位講者
- 同一位講者的連續短句要合併成完整的段落，不要逐句列出
- 不同講者之間換行分隔
- 逐字稿中 [EN] 標記的是英文原文語音辨識結果，[中] 標記的是中文翻譯。校正時請以中文翻譯為主，參考英文原文修正翻譯錯誤
- 全部使用台灣繁體中文
- 使用台灣用語（軟體、網路、記憶體、程式、伺服器等）
- 專有名詞維持英文原文
- **嚴禁**加入原文沒有的內容，不要自行編造開場白、結語、總結語句或任何原文未出現的話語
- **嚴禁**截斷或省略逐字稿內容，不可使用「以下略」「篇幅限制略去」「內容省略」等說法跳過任何段落，必須從頭到尾完整輸出
- 不要保留時間戳記

以下是逐字稿：
---
{transcript}
---
"""

SUMMARY_MERGE_PROMPT_TEMPLATE = """\
你是專業的會議記錄整理員。以下是同一場會議分段摘要的結果，請合併整理成一份完整的摘要。

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

規則：
- 全部使用台灣繁體中文
- 使用台灣用語
- 去除重複的重點，合併相似內容
- 按時間或主題順序排列
- 列出 5-15 個重點

以下是各段摘要：
---
{summaries}
---
"""

def _summary_prompt(transcript, topic=None, summary_mode="both"):
    """依據逐字稿內容選擇摘要 prompt（有 Speaker 標籤用對話版）
    summary_mode: "both"（摘要+逐字稿）、"summary"（只摘要）、"transcript"（只逐字稿）
    """
    if "[Speaker " in transcript:
        prompt = SUMMARY_PROMPT_DIARIZE_TEMPLATE.format(transcript=transcript)
    else:
        prompt = SUMMARY_PROMPT_TEMPLATE.format(transcript=transcript)

    if summary_mode == "summary":
        # 移除校正逐字稿相關段落
        prompt = prompt.replace("完成兩件事：", "完成以下任務：")
        prompt = prompt.replace("1. **重點摘要**：", "**重點摘要**：")
        # 移除逐字稿任務描述行
        prompt = re.sub(r'2\. \*\*校正逐字稿\*\*：[^\n]*\n', '', prompt)
        # 移除輸出格式中的校正逐字稿區段
        prompt = re.sub(r'\n## 校正逐字稿\n.*?(?=\n規則：)', '\n', prompt, flags=re.DOTALL)
    elif summary_mode == "transcript":
        # 移除重點摘要相關段落
        prompt = prompt.replace("完成兩件事：", "完成以下任務：")
        prompt = prompt.replace("2. **校正逐字稿**：", "**校正逐字稿**：")
        # 移除摘要任務描述行
        prompt = re.sub(r'1\. \*\*重點摘要\*\*：[^\n]*\n', '', prompt)
        # 移除輸出格式中的重點摘要區段
        prompt = re.sub(r'\n## 重點摘要\n.*?(?=\n## 校正逐字稿)', '', prompt, flags=re.DOTALL)

    if topic:
        prompt = prompt.replace(
            "以下是逐字稿：",
            f"- 本次會議主題：{topic}，請根據此主題的領域知識理解專業術語並正確校正\n\n以下是逐字稿：",
        )
    return prompt


# 場景名稱對照（CLI 用）
SCENE_MAP = {"meeting": 0, "training": 1, "subtitle": 2}
MODE_MAP = {key: i for i, (key, _, _) in enumerate(MODE_PRESETS)}
APP_NAME = f"jt-live-whisper v{APP_VERSION} - 100% 全地端 AI 語音工具集"
APP_AUTHOR = "by Jason Cheng (Jason Tools)"


def check_dependencies(asr_engine="whisper"):
    """檢查所有必要檔案是否存在"""
    errors = []
    if asr_engine == "whisper" and not os.path.isfile(WHISPER_STREAM):
        errors.append(f"找不到 whisper-stream: {WHISPER_STREAM}")
    if asr_engine == "moonshine" and not _MOONSHINE_AVAILABLE:
        errors.append("moonshine-voice 未安裝，請執行: pip install moonshine-voice sounddevice numpy")
    if not os.path.isdir(ARGOS_PKG_PATH):
        errors.append(f"找不到翻譯模型: {ARGOS_PKG_PATH}")
    if errors:
        for e in errors:
            print(f"[錯誤] {e}", file=sys.stderr)
        sys.exit(1)


def select_mode():
    """讓用戶選擇功能模式"""
    default_idx = 0  # 預設：英翻中

    print(f"\n\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    # 計算顯示寬度（中文字佔 2 格）
    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)
    col = max(_dw(name) for _, name, _ in MODE_PRESETS) + 2
    for i, (key, name, desc) in enumerate(MODE_PRESETS):
        padded = name + ' ' * (col - _dw(name))
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(MODE_PRESETS)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    key, name, desc = MODE_PRESETS[idx]
    print(f"  {C_OK}→ {name}{RESET} {C_DIM}({desc}){RESET}\n")
    return key


def select_whisper_model(mode="en2zh"):
    """讓用戶選擇 whisper 模型"""
    available = []
    for name, filename, desc in WHISPER_MODELS:
        # 中文語音模式不能用 .en 模型（僅支援英文）
        if mode in ("zh", "zh2en") and name.endswith(".en"):
            continue
        path = os.path.join(MODELS_DIR, filename)
        if os.path.isfile(path):
            available.append((name, path, desc))

    if not available:
        print("[錯誤] 找不到任何 whisper 模型！", file=sys.stderr)
        sys.exit(1)

    if len(available) == 1:
        print(f"使用模型: {available[0][0]} ({available[0][2]})\n")
        return available[0][0], available[0][1]

    print(f"\n\n{C_TITLE}{BOLD}▎ 語音辨識模型{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    default_model = "large-v3" if mode in ("zh", "zh2en") else "large-v3-turbo"
    default_idx = 0
    for i, (name, path, desc) in enumerate(available):
        if name == default_model:
            default_idx = i
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {name:16s}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{name:16s}{RESET} {C_DIM}{desc}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if 0 <= idx < len(available):
                selected = available[idx]
            else:
                print("[錯誤] 無效的編號", file=sys.stderr)
                sys.exit(1)
        except ValueError:
            print("[錯誤] 請輸入數字", file=sys.stderr)
            sys.exit(1)
    else:
        selected = available[default_idx]

    print(f"  {C_OK}→ {selected[0]}{RESET} {C_DIM}({selected[2]}){RESET}\n")
    return selected[0], selected[1]


def select_whisper_model_remote(mode="en2zh"):
    """遠端模式選擇 Whisper 模型（不檢查本機 .bin 檔案，顯示遠端快取標籤）。
    回傳 model_name (str)。"""
    is_chinese = mode in ("zh", "zh2en")
    available = []
    for name, _filename, desc in WHISPER_MODELS:
        if is_chinese and name.endswith(".en"):
            continue
        available.append((name, desc))

    # 預設模型
    default_name = "large-v3" if is_chinese else "large-v3-turbo"
    default_idx = 0
    for i, (name, _) in enumerate(available):
        if name == default_name:
            default_idx = i
            break

    # 查詢遠端已快取的模型
    remote_cached = set()
    if REMOTE_WHISPER_CONFIG:
        remote_cached = _remote_whisper_models(REMOTE_WHISPER_CONFIG, timeout=3)

    print(f"\n\n{C_TITLE}{BOLD}▎ 辨識模型（遠端 GPU）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    col = max(len(name) for name, _ in available) + 2
    dcol = max(_str_display_width(desc) for _, desc in available) + 2
    for i, (name, desc) in enumerate(available):
        padded = name + ' ' * (col - len(name))
        dpadded = desc + ' ' * (dcol - _str_display_width(desc))
        cache_tag = ""
        if remote_cached:
            if name in remote_cached:
                cache_tag = f" {C_OK}✓{RESET}"
            else:
                cache_tag = f" {C_DIM}(需下載){RESET}"
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{dpadded}{RESET}{cache_tag}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{dpadded}{RESET}{cache_tag}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(available)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    model_name = available[idx][0]
    # 警告未快取
    if remote_cached and model_name not in remote_cached:
        print(f"  {C_HIGHLIGHT}[注意] 模型 {model_name} 尚未下載到遠端，首次辨識需要先下載（可能需數分鐘）{RESET}")
    print(f"  {C_OK}→ {model_name}{RESET} {C_DIM}({available[idx][1]}){RESET}\n")
    return model_name


def select_scene():
    """讓用戶選擇使用場景"""
    if len(SCENE_PRESETS) == 1:
        s = SCENE_PRESETS[0]
        print(f"使用場景: {s[0]} ({s[3]})\n")
        return s[1], s[2]

    default_idx = 1  # 預設：教育訓練

    print(f"\n\n{C_TITLE}{BOLD}▎ 使用場景{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for i, (name, length, step, desc) in enumerate(SCENE_PRESETS):
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {name:8s}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{name:8s}{RESET} {C_DIM}{desc}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_DIM}  * 緩衝長度越長句子越完整；越短反應越即時{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(SCENE_PRESETS)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    name, length, step, desc = SCENE_PRESETS[idx]
    print(f"  {C_OK}→ {name}{RESET} {C_DIM}({desc}){RESET}\n")
    return length, step


def _enumerate_sdl_devices(model_path):
    """列舉 SDL2 音訊捕捉裝置（透過 whisper-stream），回傳 [(id, name), ...]"""
    proc = subprocess.Popen(
        [WHISPER_STREAM, "-m", model_path, "-c", "999", "--length", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    devices = []
    deadline = time.monotonic() + 30
    try:
        for line in proc.stderr:
            match = re.search(r"Capture device #(\d+): '(.+)'", line)
            if match:
                devices.append((int(match.group(1)), match.group(2)))
            if devices and not match:
                break
            if time.monotonic() > deadline:
                break
    finally:
        proc.kill()
        proc.wait()

    return devices


def list_audio_devices(model_path):
    """自動選擇 BlackHole 音訊裝置（SDL2），找不到才 fallback 顯示選單"""
    print(f"{C_DIM}正在偵測音訊裝置...{RESET}")

    devices = _enumerate_sdl_devices(model_path)

    if not devices:
        print("[錯誤] 找不到任何音訊捕捉裝置！", file=sys.stderr)
        print("請確認 BlackHole 2ch 已安裝並重新啟動電腦。", file=sys.stderr)
        sys.exit(1)

    # 自動選 BlackHole
    for dev_id, dev_name in devices:
        if "blackhole" in dev_name.lower():
            print(f"  {C_OK}ASR 裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 BlackHole → fallback 顯示選單讓使用者手動選
    print(f"{C_WARN}[提醒] 未偵測到 BlackHole，請手動選擇音訊裝置{RESET}")
    default_id = devices[0][0]

    print(f"{C_TITLE}{BOLD}▎ 音訊裝置{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for dev_id, dev_name in devices:
        if dev_id == default_id:
            print(f"  {C_HIGHLIGHT}{BOLD}[{dev_id}] {dev_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{dev_id}]{RESET} {C_WHITE}{dev_name}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入其他 ID：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            selected_id = int(user_input)
        except ValueError:
            print("[錯誤] 請輸入數字", file=sys.stderr)
            sys.exit(1)
    else:
        selected_id = default_id

    selected_name = next((n for i, n in devices if i == selected_id), f"裝置 #{selected_id}")
    print(f"  {C_OK}→ [{selected_id}] {selected_name}{RESET}\n")
    return selected_id


def select_asr_engine():
    """讓使用者選擇語音辨識引擎（Moonshine / Whisper）"""
    if not _MOONSHINE_AVAILABLE:
        print(f"  {C_DIM}(Moonshine 未安裝，使用 Whisper){RESET}")
        return "whisper"

    default_idx = 0  # Moonshine

    print(f"\n\n{C_TITLE}{BOLD}▎ 語音辨識引擎{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for i, (key, name, desc) in enumerate(ASR_ENGINES):
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {name:12s}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{name:12s}{RESET} {C_DIM}{desc}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(ASR_ENGINES)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    key, name, desc = ASR_ENGINES[idx]
    print(f"  {C_OK}→ {name}{RESET} {C_DIM}({desc}){RESET}\n")
    return key


def select_asr_location():
    """讓使用者選擇辨識位置（遠端 GPU / 本機），僅在 REMOTE_WHISPER_CONFIG 存在時呼叫。
    回傳 "remote" 或 "local"。"""
    rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
    options = [
        (f"遠端 GPU（{rw_host}，速度快）", "remote"),
        ("本機（Whisper 或 Moonshine）", "local"),
    ]
    default_idx = 0  # 預設遠端

    print(f"\n\n{C_TITLE}{BOLD}▎ 辨識位置{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    col = max(_str_display_width(label) for label, _ in options) + 2
    for i, (label, _) in enumerate(options):
        pad = ' ' * (col - _str_display_width(label))
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {label}{pad}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{label}{pad}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_HIGHLIGHT}  * 遠端不支援 Moonshine，固定使用 Whisper{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(options)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    label, key = options[idx]
    if key == "remote":
        print(f"  {C_OK}→ 遠端 GPU（{rw_host}）{RESET}")
        print(f"  {C_DIM}遠端不支援 Moonshine，使用 Whisper{RESET}\n")
    else:
        print(f"  {C_OK}→ 本機{RESET}\n")
    return key


def select_moonshine_model():
    """讓使用者選擇 Moonshine 串流模型"""
    default_idx = 0  # medium

    print(f"\n\n{C_TITLE}{BOLD}▎ Moonshine 語音模型{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for i, (name, desc, size) in enumerate(MOONSHINE_MODELS):
        label = f"{name:8s} {size}"
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {label:20s}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{label:20s}{RESET} {C_DIM}{desc}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(MOONSHINE_MODELS)):
                idx = default_idx
        except ValueError:
            idx = default_idx
    else:
        idx = default_idx

    name, desc, size = MOONSHINE_MODELS[idx]
    print(f"  {C_OK}→ {name}{RESET} {C_DIM}({desc}){RESET}\n")
    return name


def _moonshine_model_arch(name):
    """將 Moonshine 模型名稱對應到 ModelArch"""
    mapping = {"tiny": ModelArch.TINY_STREAMING, "small": ModelArch.SMALL_STREAMING, "medium": ModelArch.MEDIUM_STREAMING}
    return mapping[name]


def list_audio_devices_sd():
    """自動選擇 BlackHole 音訊裝置（sounddevice），找不到才 fallback 顯示選單"""
    devices = sd.query_devices()
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev["name"], dev["max_input_channels"], int(dev["default_samplerate"])))

    if not input_devices:
        print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
        sys.exit(1)

    # 自動選 BlackHole
    for dev_id, dev_name, _, _ in input_devices:
        if "blackhole" in dev_name.lower():
            print(f"  {C_OK}ASR 裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 BlackHole → fallback 顯示選單
    print(f"{C_WARN}[提醒] 未偵測到 BlackHole，請手動選擇音訊裝置{RESET}")
    default_id = input_devices[0][0]

    print(f"\n\n{C_TITLE}{BOLD}▎ 音訊裝置{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for dev_id, dev_name, ch, sr in input_devices:
        info = f"{ch}ch {sr}Hz"
        if dev_id == default_id:
            print(f"  {C_HIGHLIGHT}{BOLD}[{dev_id}] {dev_name}{RESET} {C_DIM}{info}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{dev_id}]{RESET} {C_WHITE}{dev_name}{RESET} {C_DIM}{info}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入其他 ID：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        try:
            selected_id = int(user_input)
        except ValueError:
            print("[錯誤] 請輸入數字", file=sys.stderr)
            sys.exit(1)
    else:
        selected_id = default_id

    selected_name = next((n for i, n, _, _ in input_devices if i == selected_id), f"裝置 #{selected_id}")
    print(f"  {C_OK}→ [{selected_id}] {selected_name}{RESET}\n")
    return selected_id


def auto_select_device_sd():
    """非互動模式：使用 sounddevice 自動偵測 BlackHole"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
            print(f"{C_OK}自動選擇音訊裝置: [{i}] {dev['name']}{RESET}")
            return i
    # 找不到 BlackHole，用系統預設輸入
    default = sd.default.device[0]
    if default is not None and default >= 0:
        dev = devices[default]
        print(f"{C_HIGHLIGHT}未偵測到 BlackHole，使用系統預設輸入: [{default}] {dev['name']}{RESET}")
        return default
    print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
    sys.exit(1)


class OllamaTranslator:
    """使用 LLM API 翻譯，帶上下文（支援 Ollama 和 OpenAI 相容伺服器）"""

    MAX_CONTEXT = 5  # 保留最近 N 筆翻譯作為上下文

    def __init__(self, model, host=OLLAMA_HOST, port=OLLAMA_PORT, direction="en2zh",
                 skip_check=False, server_type="ollama", meeting_topic=None):
        self.model = model
        self.direction = direction
        self.host = host
        self.port = port
        self.server_type = server_type
        self.meeting_topic = meeting_topic
        self.context = []  # [(src, dst), ...]
        if not skip_check:
            srv_label = "Ollama" if server_type == "ollama" else "LLM"
            print(f"{C_DIM}正在連接 {srv_label} ({model})...{RESET}", end=" ", flush=True)
            try:
                self._call_ollama("hello", [])
                print(f"{C_OK}{BOLD}完成！{RESET}")
            except Exception as e:
                print(f"\n[錯誤] 無法連接 {srv_label}: {e}", file=sys.stderr)
                sys.exit(1)

    def _build_prompt(self, text, context):
        if self.direction == "zh2en":
            return self._build_prompt_zh2en(text, context)
        return self._build_prompt_en2zh(text, context)

    def _build_prompt_en2zh(self, text, context):
        prompt = (
            "你是即時會議翻譯員，將英文翻譯成台灣繁體中文。\n"
            "規則：\n"
            "1. 必須使用繁體中文，禁止使用簡體中文（例：用「軟體」不用「软件」，用「記憶體」不用「内存」）\n"
            "2. 使用台灣用語：軟體、網路、記憶體、程式、伺服器、資料庫、影片、滑鼠、設定、訊息\n"
            "3. 專有名詞維持英文原文（如 iPhone、API、Kubernetes、GitHub）；人名維持英文原文（如 Tim Cook、Jensen Huang），除非是確定的知名中文人名才用中文（如 張忠謀、蔡崇信）\n"
            "4. 只輸出一行繁體中文翻譯，不要輸出原文、解釋、替代版本\n"
            "5. 只能包含繁體中文和英文，禁止輸出俄文、日文、韓文等其他語言\n"
            "6. 禁止添加任何評論、括號註解、翻譯說明（如「此句不完整」「無法翻譯」「有誤」等）\n"
            "7. 即使原文不完整或語意不清，也直接逐字翻譯，不要跳過或加說明\n"
            "8. 直接輸出翻譯結果，不要使用 <think> 標籤或任何思考過程\n"
        )
        if self.meeting_topic:
            prompt += f"\n本次會議主題：{self.meeting_topic}\n請根據此主題的領域知識翻譯專業術語。\n"
        if context:
            prompt += "\n最近的對話上下文：\n"
            for src, dst in context:
                prompt += f"英：{src}\n中：{dst}\n"
        prompt += f"\n請翻譯：{text}"
        return prompt

    def _build_prompt_zh2en(self, text, context):
        prompt = (
            "You are a real-time meeting interpreter. Translate Chinese to English.\n"
            "Rules:\n"
            "1. Output natural, fluent English\n"
            "2. Keep proper nouns as-is (e.g. iPhone, API, Kubernetes, GitHub)\n"
            "3. Output only ONE line of English translation, no explanations or alternatives\n"
            "4. Output English only, no Chinese, Russian, Japanese or other languages\n"
            "5. Never add commentary, parenthetical notes, or translation remarks\n"
            "6. If input is incomplete, translate it literally as-is without explanation\n"
            "7. Output translation directly, do NOT use <think> tags or any thinking process\n"
        )
        if self.meeting_topic:
            prompt += f"\nMeeting topic: {self.meeting_topic}\nTranslate domain-specific terms according to this topic.\n"
        if context:
            prompt += "\nRecent context:\n"
            for src, dst in context:
                prompt += f"中：{src}\nEN：{dst}\n"
        prompt += f"\nTranslate：{text}"
        return prompt

    def _call_ollama(self, text, context):
        return _llm_generate(
            self._build_prompt(text, context), self.model,
            self.host, self.port, self.server_type,
            stream=False, timeout=30, think=False,
        )

    # 翻譯幻覺關鍵詞（模型有時會輸出翻譯說明而非翻譯結果）
    _HALLUCINATION_KEYWORDS = [
        "無法翻譯", "此句不完整", "翻譯似乎有誤", "讓我們回到",
        "請翻譯", "尚未完成", "可能是句子", "可能有誤",
        "翻譯如下", "以下是翻譯", "正確的翻譯",
        "unable to translate", "cannot translate", "incomplete sentence",
    ]

    @staticmethod
    def _contains_bad_chars(text):
        """檢查是否包含非中英文的字元（俄文、日文假名等）"""
        for ch in text:
            if ('\u0400' <= ch <= '\u04ff' or   # 俄文 Cyrillic
                '\u3040' <= ch <= '\u309f' or   # 日文平假名
                '\u30a0' <= ch <= '\u30ff' or   # 日文片假名
                '\u0e00' <= ch <= '\u0e7f' or   # 泰文
                '\u0600' <= ch <= '\u06ff'):     # 阿拉伯文
                return True
        return False

    @classmethod
    def _is_hallucinated(cls, src, result):
        """偵測翻譯幻覺：模型輸出評論/說明而非翻譯結果"""
        low = result.lower()
        for kw in cls._HALLUCINATION_KEYWORDS:
            if kw in low:
                return True
        # 翻譯結果長度異常（超過原文 4 倍以上，且原文短）
        if len(src) < 60 and len(result) > len(src) * 4:
            return True
        # 包含全形括號註解（如「（此句不完整...）」）
        if re.search(r'（[^）]{6,}）', result):
            return True
        return False

    @classmethod
    def _strip_commentary(cls, result):
        """移除翻譯結果中的括號評論/註解"""
        # 移除全形括號評論
        cleaned = re.sub(r'（[^）]*(?:不完整|有誤|無法|說明|翻譯|可能)[^）]*）', '', result)
        # 移除半形括號評論
        cleaned = re.sub(r'\([^)]*(?:incomplete|cannot|unable|translation)[^)]*\)', '', cleaned, flags=re.I)
        return cleaned.strip()

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        try:
            result = self._call_ollama(text, self.context)
            # 移除 <think>...</think> 標籤（部分模型如 Qwen3 會自動思考）
            result = re.sub(r'<think>[\s\S]*?</think>', '', result).strip()
            # 移除未閉合的 <think>（模型可能只輸出開頭）
            result = re.sub(r'<think>[\s\S]*', '', result).strip()
            # 只取第一行，避免 model 輸出多餘解釋
            result = result.split("\n")[0].strip()
            if self.direction == "en2zh":
                # 簡體→台灣繁體轉換
                result = S2TWP.convert(result)
            # 過濾翻譯幻覺（模型輸出評論而非翻譯）
            if self._is_hallucinated(text, result):
                # 先嘗試去除括號評論
                cleaned = self._strip_commentary(result)
                if cleaned and not self._is_hallucinated(text, cleaned):
                    result = cleaned
                else:
                    # 不帶上下文重試一次
                    result = self._call_ollama(text, [])
                    result = re.sub(r'<think>[\s\S]*?</think>', '', result).strip()
                    result = re.sub(r'<think>[\s\S]*', '', result).strip()
                    result = result.split("\n")[0].strip()
                    if self.direction == "en2zh":
                        result = S2TWP.convert(result)
                    if self._is_hallucinated(text, result):
                        result = self._strip_commentary(result)
                        if not result:
                            return ""
            # 過濾非中英文的回應（模型偶爾會輸出俄文等）
            if self._contains_bad_chars(result):
                # 重試一次
                result = self._call_ollama(text, [])
                result = re.sub(r'<think>[\s\S]*?</think>', '', result).strip()
                result = re.sub(r'<think>[\s\S]*', '', result).strip()
                result = result.split("\n")[0].strip()
                if self._contains_bad_chars(result):
                    return ""
            # 更新上下文
            self.context.append((text, result))
            if len(self.context) > self.MAX_CONTEXT:
                self.context.pop(0)
            return result
        except Exception:
            return ""


class ArgosTranslator:
    """使用 ctranslate2 + sentencepiece 離線翻譯"""

    def __init__(self):
        print(f"{C_DIM}正在載入離線翻譯模型...{RESET}", end=" ", flush=True)
        self.sp = sentencepiece.SentencePieceProcessor()
        self.sp.Load(os.path.join(ARGOS_PKG_PATH, "sentencepiece.model"))
        self.ct2 = ctranslate2.Translator(
            os.path.join(ARGOS_PKG_PATH, "model"), device="cpu"
        )
        print(f"{C_OK}{BOLD}完成！{RESET}")

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        tokens = self.sp.Encode(text, out_type=str)
        results = self.ct2.translate_batch([tokens])
        translated_tokens = results[0].hypotheses[0]
        translated = self.sp.Decode(translated_tokens)
        translated = translated.replace("\u2581", " ").strip()
        # 簡體→台灣繁體轉換
        return S2TWP.convert(translated)


def _detect_llm_server(host, port):
    """自動偵測 LLM 伺服器類型，回傳 "ollama" / "openai" / None"""
    # 先嘗試 Ollama
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
            return "ollama"
    except Exception:
        pass
    # 再嘗試 OpenAI 相容
    try:
        req = urllib.request.Request(f"http://{host}:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
            return "openai"
    except Exception:
        pass
    return None


def _llm_list_models(host, port, server_type):
    """列出 LLM 伺服器上的模型，回傳 list[str]"""
    try:
        if server_type == "ollama":
            req = urllib.request.Request(f"http://{host}:{port}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return [m["name"] for m in data.get("models", [])]
        elif server_type == "openai":
            req = urllib.request.Request(f"http://{host}:{port}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return [m["id"] for m in data.get("data", [])
                        if m.get("owned_by") != "remote"]
    except Exception:
        pass
    return []


def _colorize_summary_line(line):
    """摘要 live output 的 markdown 著色"""
    s = line.lstrip()
    if s.startswith("## "):
        return f"{C_TITLE}{BOLD}{line}{RESET}"
    elif s.startswith("# "):
        return f"{C_TITLE}{BOLD}{line}{RESET}"
    elif s.startswith("- "):
        return f"{C_OK}{line}{RESET}"
    elif s.startswith("Speaker ") or s.startswith("**Speaker "):
        return f"{C_HIGHLIGHT}{line}{RESET}"
    elif s.startswith("---"):
        return f"{C_DIM}{line}{RESET}"
    else:
        return f"{C_ZH}{line}{RESET}"


def _live_output_line(line, write_lock):
    """著色並輸出一行摘要文字"""
    colored = _colorize_summary_line(line)
    if write_lock:
        with write_lock:
            sys.stdout.write(colored + "\n")
            sys.stdout.flush()
    else:
        sys.stdout.write(colored + "\n")
        sys.stdout.flush()


def _llm_generate(prompt, model, host, port, server_type, stream=False,
                  timeout=30, spinner=None, live_output=False, think=None):
    """統一 LLM 生成介面，支援 Ollama 原生 API 和 OpenAI 相容 API
    think: True=啟用思考模式, False=關閉思考模式, None=不指定（由模型預設）"""
    write_lock = getattr(spinner, '_lock', None)

    if server_type == "openai":
        url = f"http://{host}:{port}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        # OpenAI 相容：部分伺服器支援 chat_template_kwargs 關閉思考
        if think is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        # 預設 Ollama
        url = f"http://{host}:{port}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        # Ollama：透過 options.think 控制思考模式
        if think is not None:
            payload["options"] = payload.get("options", {})
            payload["options"]["think"] = think

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )

    if not stream:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            if server_type == "openai":
                return result["choices"][0]["message"]["content"].strip()
            else:
                return result["response"].strip()

    # 串流模式
    response_text = ""
    token_count = 0
    line_buf = ""  # live_output 行緩衝（用於 markdown 著色）
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if server_type == "openai":
            # SSE 格式：data: {...}\n\n
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    break
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    response_text += token
                    token_count += 1
                    if spinner:
                        spinner.update_tokens(token_count)
                    if live_output:
                        line_buf += token
                        while "\n" in line_buf:
                            out_line, line_buf = line_buf.split("\n", 1)
                            _live_output_line(out_line, write_lock)
                # 檢查 finish_reason
                if choices[0].get("finish_reason"):
                    break
        else:
            # Ollama NDJSON 格式
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("response", "")
                if token:
                    response_text += token
                    token_count += 1
                    if spinner:
                        spinner.update_tokens(token_count)
                    if live_output:
                        line_buf += token
                        while "\n" in line_buf:
                            out_line, line_buf = line_buf.split("\n", 1)
                            _live_output_line(out_line, write_lock)
                if chunk.get("done", False):
                    break
    # 輸出殘餘緩衝
    if live_output and line_buf.strip():
        _live_output_line(line_buf, write_lock)
    return response_text.strip()


def _ssh_ctrl_sock(rw_cfg):
    """回傳 SSH ControlMaster socket 路徑"""
    user = rw_cfg.get("ssh_user", "root")
    host = rw_cfg.get("host", "localhost")
    port = rw_cfg.get("ssh_port", 22)
    return f"/tmp/jt-ssh-cm-{user}@{host}:{port}"


def _ssh_cmd_parts(rw_cfg):
    """組合 SSH 指令片段（含 key / port / ControlMaster 多工）"""
    ctrl_sock = _ssh_ctrl_sock(rw_cfg)
    parts = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
             "-o", f"ControlMaster=auto", "-o", f"ControlPath={ctrl_sock}",
             "-o", "ControlPersist=300",
             "-p", str(rw_cfg.get("ssh_port", 22))]
    ssh_key = rw_cfg.get("ssh_key", "")
    if ssh_key:
        key_path = os.path.expanduser(ssh_key)
        if os.path.isfile(key_path):
            parts += ["-i", key_path]
    parts.append(f"{rw_cfg['ssh_user']}@{rw_cfg['host']}")
    return parts


def _ssh_close_cm(rw_cfg):
    """關閉 SSH ControlMaster 多工連線"""
    ctrl_sock = _ssh_ctrl_sock(rw_cfg)
    if os.path.exists(ctrl_sock):
        try:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={ctrl_sock}", "-O", "exit",
                 f"{rw_cfg['ssh_user']}@{rw_cfg['host']}"],
                timeout=5, capture_output=True
            )
        except Exception:
            pass


def _inline_spinner(func, *args, **kwargs):
    """執行 func 同時顯示行內 spinner 動畫，回傳 func 結果。
    呼叫前須先 print(..., end="", flush=True) 輸出前綴文字。"""
    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    result = [None]
    error = [None]
    done = threading.Event()

    def _run():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    i = 0
    while not done.wait(0.1):
        sys.stdout.write(f" {_FRAMES[i % len(_FRAMES)]}\b\b")
        sys.stdout.flush()
        i += 1
    # 清除 spinner 殘留
    sys.stdout.write("  \b\b")
    sys.stdout.flush()
    if error[0]:
        raise error[0]
    return result[0]


def _remote_whisper_start(rw_cfg, force_restart=False):
    """SSH nohup 啟動遠端 Whisper server（允許互動輸入密碼）。
    若伺服器已在運行且 force_restart=False，則跳過重啟直接沿用。"""
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    host = rw_cfg["host"]
    # 先檢查伺服器是否已在運行（支援多實例共用同一個伺服器）
    if not force_restart:
        try:
            url = f"http://{host}:{port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    return  # 伺服器已在運行，直接沿用
        except Exception:
            pass  # 伺服器未運行或無回應，先清理再啟動
    # 先停掉舊的 server（避免 port 佔用或 event loop 阻塞導致無法回應）
    kill_cmd = _ssh_cmd_parts(rw_cfg) + [f"pkill -f 'server.py --port {port}' 2>/dev/null; sleep 0.5"]
    try:
        subprocess.run(kill_cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    cmd = _ssh_cmd_parts(rw_cfg) + [
        "cd ~/jt-whisper-server && export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH && "
        f"nohup venv/bin/python3 server.py --port {port} "
        "> /tmp/jt-whisper-server.log 2>&1 &"
    ]
    try:
        # 不用 capture_output，讓 SSH 密碼提示可互動
        subprocess.run(cmd, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _remote_whisper_stop(rw_cfg):
    """SSH pkill 停止遠端 Whisper server，並關閉 SSH 多工連線"""
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    cmd = _ssh_cmd_parts(rw_cfg) + [f"pkill -f 'server.py --port {port}'"]
    try:
        subprocess.run(cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _ssh_close_cm(rw_cfg)


def _remote_whisper_models(rw_cfg, timeout=5):
    """查詢遠端已快取的 Whisper 模型清單"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/models"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return set(data.get("models", []))
    except Exception:
        return set()


def _remote_whisper_health(rw_cfg, timeout=30):
    """輪詢 /health 等待遠端 server 就緒，回傳 (ok, has_gpu)
    額外將 backend 資訊存入 rw_cfg['_backend']（供 metadata 使用）"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    rw_cfg["_backend"] = data.get("backend", "")
                    return True, data.get("gpu", False)
        except Exception:
            pass
        time.sleep(1)
    return False, False


def _remote_whisper_status(rw_cfg):
    """查詢遠端 /v1/status，回傳 dict 或 None（連線失敗）"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/status"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _check_remote_before_upload(rw_cfg, file_size_bytes=0):
    """上傳前檢查遠端狀態：忙碌 / 磁碟空間。
    回傳 True 可繼續，False 使用者取消（降級本機）。"""
    status = _remote_whisper_status(rw_cfg)
    if status is None:
        return True  # 舊版 server 沒有 /v1/status，略過檢查

    # 磁碟空間檢查（至少需要檔案大小的 3 倍 + 500MB 餘裕）
    need_gb = max((file_size_bytes * 3) / (1024 ** 3), 0.5)
    disk_free = status.get("disk_free_gb", 999)
    if disk_free < need_gb:
        print(f"\n  {C_HIGHLIGHT}[警告] 遠端磁碟空間不足：{disk_free} GB 可用（需要約 {need_gb:.1f} GB）{RESET}")
        print(f"  {C_DIM}請清理遠端 /tmp 或磁碟空間後再試{RESET}")
        return False

    # 忙碌狀態檢查
    if status.get("busy"):
        task = status.get("task", {})
        task_type = task.get("type", "unknown")
        elapsed = task.get("elapsed", 0)
        client_ip = task.get("client_ip", "")
        model = task.get("model", "")
        mins = int(elapsed) // 60
        secs = int(elapsed) % 60

        task_desc = "辨識" if task_type == "transcribe" else "講者辨識"
        source = f"（來自 {client_ip}）" if client_ip else ""

        print(f"\n  {C_HIGHLIGHT}[忙碌] 遠端伺服器正在執行{task_desc}{source}{RESET}")
        print(f"  {C_DIM}模型: {model}，已執行 {mins}:{secs:02d}{RESET}")
        print()
        print(f"  {C_DIM}[1]{RESET} {C_WHITE}等候（每 5 秒重試）{RESET}")
        print(f"  {C_DIM}[2]{RESET} {C_WHITE}強制中斷遠端作業（可能是殘留的已斷線作業）{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}改用本機 辨識{RESET}")
        print(f"{C_WHITE}選擇 (1-3) [1]：{RESET}", end=" ")

        try:
            choice = input().strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice == "2":
            # 強制重啟遠端 server
            print(f"  {C_DIM}正在重啟遠端伺服器...{RESET}", end="", flush=True)
            _remote_whisper_start(rw_cfg, force_restart=True)
            ok, _ = _remote_whisper_health(rw_cfg, timeout=30)
            if ok:
                print(f" {C_OK}✓ 已重啟{RESET}")
                return True
            else:
                print(f" {C_HIGHLIGHT}重啟失敗{RESET}")
                return False
        elif choice == "3":
            print(f"  {C_OK}→ 改用本機 辨識{RESET}")
            return False
        else:
            # 等候
            print(f"  {C_DIM}等候遠端伺服器...{RESET}", flush=True)
            while True:
                time.sleep(5)
                st = _remote_whisper_status(rw_cfg)
                if st is None or not st.get("busy"):
                    print(f"  {C_OK}→ 遠端伺服器已就緒{RESET}")
                    return True
                t = st.get("task", {})
                e = t.get("elapsed", 0)
                print(f"  {C_DIM}仍在忙碌（已 {int(e)//60}:{int(e)%60:02d}）...{RESET}", flush=True)

    return True


class _ProgressBody(io.BytesIO):
    """追蹤上傳進度的 BytesIO 包裝器"""

    def __init__(self, data, callback=None, on_complete=None):
        super().__init__(data)
        self._total = len(data)
        self._sent = 0
        self._callback = callback
        self._on_complete = on_complete
        self._complete_fired = False

    def read(self, size=-1):
        chunk = super().read(size)
        if chunk:
            self._sent += len(chunk)
            if self._callback and self._total > 0:
                pct = min(self._sent * 100 // self._total, 100)
                sent_mb = self._sent / (1024 * 1024)
                total_mb = self._total / (1024 * 1024)
                self._callback(f"上傳 {sent_mb:.1f}/{total_mb:.1f} MB（{pct}%）")
                # 上傳完成 → 通知呼叫端切換狀態（伺服器接下來開始辨識）
                if self._sent >= self._total and not self._complete_fired:
                    self._complete_fired = True
                    if self._on_complete:
                        self._on_complete()
        return chunk

    def __len__(self):
        return self._total


def _remote_whisper_transcribe(rw_cfg, wav_path, model, language,
                               progress_callback=None, on_upload_done=None):
    """POST 音訊到遠端 /v1/audio/transcriptions（串流 NDJSON），回傳 (segments, duration, proc_time, device)"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/transcriptions"

    # multipart/form-data 用 urllib（沿用專案現有模式，不加 requests）
    boundary = f"----jt-whisper-{int(time.monotonic() * 1000)}"
    body_parts = []

    # file field
    filename = os.path.basename(wav_path)
    with open(wav_path, "rb") as f:
        file_data = f.read()
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    body_parts.append(file_data)
    body_parts.append(b"\r\n")

    # model field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"model\"\r\n\r\n"
        f"{model}\r\n"
    )

    # language field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"language\"\r\n\r\n"
        f"{language}\r\n"
    )

    # stream field（啟用串流回傳）
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"stream\"\r\n\r\n"
        f"true\r\n"
    )

    body_parts.append(f"--{boundary}--\r\n")

    # 組合 body（混合 str 和 bytes）
    body = b""
    for part in body_parts:
        if isinstance(part, str):
            body += part.encode("utf-8")
        else:
            body += part

    # 用 _ProgressBody 追蹤上傳進度
    body_obj = _ProgressBody(body, callback=progress_callback, on_complete=on_upload_done)

    req = urllib.request.Request(url, data=body_obj, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            content_type = resp.headers.get("Content-Type", "")

            if "ndjson" in content_type:
                # 串流模式：逐行讀取 NDJSON
                if on_upload_done:
                    on_upload_done()
                segments = []
                duration = 0
                proc_time = 0
                device = "unknown"
                for raw_line in resp:
                    line = raw_line.decode().strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if event["type"] == "segment":
                        segments.append({"start": event["start"], "end": event["end"], "text": event["text"]})
                        duration = event.get("duration", 0)
                        if progress_callback and duration > 0:
                            pct = min(event["end"] / duration, 1.0)
                            pos = int(event["end"])
                            dur = int(duration)
                            progress_callback(f"{pct:.0%}  {pos//60}:{pos%60:02d} / {dur//60}:{dur%60:02d}")
                    elif event["type"] == "done":
                        duration = event.get("duration", duration)
                        proc_time = event.get("processing_time", 0)
                        device = event.get("device", "unknown")
                    elif event["type"] == "heartbeat":
                        elapsed = event.get("elapsed", 0)
                        mins = int(elapsed) // 60
                        secs = int(elapsed) % 60
                        if progress_callback:
                            pct = event.get("progress")
                            if pct is not None:
                                hb_cur = event.get("current", 0)
                                hb_dur = event.get("duration", 0)
                                pos = int(hb_cur)
                                dur = int(hb_dur)
                                progress_callback(
                                    f"{pct:.0%}  {pos//60}:{pos%60:02d}/{dur//60}:{dur%60:02d}"
                                    f"  已耗時 {mins}:{secs:02d}")
                            else:
                                progress_callback(f"伺服器辨識中（{mins}:{secs:02d}）")
                    elif event["type"] == "error":
                        raise RuntimeError(f"遠端辨識錯誤: {event.get('detail', '未知錯誤')}")
            else:
                # 非串流模式（向下相容舊版伺服器）
                if progress_callback:
                    progress_callback("辨識中，等待伺服器回應...")
                data = json.loads(resp.read().decode())
                segments = data.get("segments", [])
                duration = data.get("duration", 0)
                proc_time = data.get("processing_time", 0)
                device = data.get("device", "unknown")
    except urllib.error.HTTPError as e:
        # 讀取伺服器回傳的錯誤訊息
        err_body = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        detail = ""
        if err_body:
            try:
                err_data = json.loads(err_body)
                detail = err_data.get("detail", err_data.get("error", ""))
            except (json.JSONDecodeError, ValueError):
                detail = err_body[:200]
        raise RuntimeError(f"遠端伺服器錯誤 ({e.code}): {detail or e.reason}") from e

    return segments, duration, proc_time, device


def _remote_whisper_transcribe_bytes(rw_cfg, wav_bytes, model, language, timeout=120):
    """POST 記憶體中的 WAV bytes 到遠端 /v1/audio/transcriptions
    （即時模式用，每次 ~160KB 不需進度回報）
    回傳 (segments, full_text, proc_time)"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/transcriptions"

    boundary = f"----jt-whisper-{int(time.monotonic() * 1000)}"
    body_parts = []

    # file field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"chunk.wav\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    body_parts.append(wav_bytes)
    body_parts.append(b"\r\n")

    # model field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"model\"\r\n\r\n"
        f"{model}\r\n"
    )

    # language field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"language\"\r\n\r\n"
        f"{language}\r\n"
    )

    body_parts.append(f"--{boundary}--\r\n")

    body = b""
    for part in body_parts:
        if isinstance(part, str):
            body += part.encode("utf-8")
        else:
            body += part

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())

    segments = data.get("segments", [])
    full_text = data.get("text", "").strip()
    proc_time = data.get("processing_time", 0)
    return segments, full_text, proc_time


def _remote_diarize(rw_cfg, wav_path, segments, num_speakers=None,
                    progress_callback=None, on_upload_done=None):
    """POST 音訊 + segments 到遠端 /v1/audio/diarize
    回傳 (speaker_labels, proc_time) 或失敗回傳 (None, 0)"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/diarize"

    # 先檢查遠端是否支援 diarize
    try:
        health_url = f"http://{host}:{port}/health"
        req_h = urllib.request.Request(health_url)
        with urllib.request.urlopen(req_h, timeout=10) as resp_h:
            health_data = json.loads(resp_h.read().decode())
        if not health_data.get("diarize", False):
            print(f"  {C_HIGHLIGHT}[遠端] 伺服器未安裝 resemblyzer/spectralcluster{RESET}")
            return None, 0
    except Exception:
        # health 檢查失敗，仍然嘗試 diarize（可能是舊版伺服器）
        pass

    # 準備 segments JSON
    seg_json = json.dumps(
        [{"start": s["start"], "end": s["end"], "text": s.get("text", "")}
         for s in segments],
        ensure_ascii=False,
    )

    boundary = f"----jt-diarize-{int(time.monotonic() * 1000)}"
    body_parts = []

    # file field
    filename = os.path.basename(wav_path)
    with open(wav_path, "rb") as f:
        file_data = f.read()
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    body_parts.append(file_data)
    body_parts.append(b"\r\n")

    # segments field
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"segments\"\r\n\r\n"
        f"{seg_json}\r\n"
    )

    # num_speakers field
    ns_val = num_speakers if num_speakers else 0
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"num_speakers\"\r\n\r\n"
        f"{ns_val}\r\n"
    )

    body_parts.append(f"--{boundary}--\r\n")

    # 組合 body
    body = b""
    for part in body_parts:
        if isinstance(part, str):
            body += part.encode("utf-8")
        else:
            body += part

    body_obj = _ProgressBody(body, callback=progress_callback, on_complete=on_upload_done)

    req = urllib.request.Request(url, data=body_obj, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            if progress_callback:
                progress_callback("辨識中，等待伺服器回應...")
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        detail = ""
        if err_body:
            try:
                err_data = json.loads(err_body)
                detail = err_data.get("detail", err_data.get("error", ""))
            except (json.JSONDecodeError, ValueError):
                detail = err_body[:200]
        print(f"  {C_HIGHLIGHT}[遠端 diarize] 伺服器錯誤 ({e.code}): {detail or e.reason}{RESET}")
        return None, 0
    except Exception as e:
        print(f"  {C_HIGHLIGHT}[遠端 diarize] 連線失敗: {e}{RESET}")
        return None, 0

    speaker_labels = data.get("speaker_labels")
    proc_time = data.get("processing_time", 0)
    n_spk = data.get("num_speakers", 0)
    device = data.get("device", "unknown")
    print(f"  {C_DIM}[遠端 diarize] {n_spk} 位講者, {proc_time}s ({device}){RESET}")
    return speaker_labels, proc_time


def _check_llm_server(host, port):
    """偵測 LLM 伺服器類型並回傳可用模型列表
    回傳 (server_type, model_list)"""
    server_type = _detect_llm_server(host, port)
    if not server_type:
        return None, []
    all_models = _llm_list_models(host, port, server_type)
    # 回傳伺服器上所有模型（Ollama / OpenAI 相容行為一致）
    return server_type, all_models


def select_translator(init_host=None, init_port=None):
    """讓用戶選擇翻譯引擎和模型，回傳 (engine, model, host, port, server_type)"""
    host = init_host or OLLAMA_HOST
    port = init_port or OLLAMA_PORT

    print(f"\n\n{C_TITLE}{BOLD}▎ 翻譯引擎{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")

    server_type, available_models = None, []
    if host:
        # 有設定 LLM 伺服器，自動偵測
        print(f"  {C_DIM}正在偵測 LLM 伺服器 ({host}:{port})...{RESET}", end=" ", flush=True)
        server_type, available_models = _check_llm_server(host, port)

    if not server_type:
        if host:
            # 有設定但連不上
            print(f"{C_HIGHLIGHT}未偵測到{RESET}")
        # 問使用者要不要輸入位址
        print(f"  {C_WHITE}輸入 LLM 伺服器位址，或按 Enter 使用離線翻譯：{RESET}", end=" ")
        try:
            ip_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if ip_input:
            if ":" in ip_input:
                parts = ip_input.rsplit(":", 1)
                host = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    port = OLLAMA_PORT
            else:
                host = ip_input
            print(f"  {C_DIM}正在偵測 LLM 伺服器 ({host}:{port})...{RESET}", end=" ", flush=True)
            server_type, available_models = _check_llm_server(host, port)
            if not server_type:
                print(f"{C_HIGHLIGHT}未偵測到{RESET}")

        if not server_type:
            print(f"  {C_OK}→ Argos 本機離線翻譯{RESET}\n")
            return "argos", None, None, None, None

    srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
    print(f"{C_OK}{BOLD}{srv_label}（{len(available_models)} 個模型）{RESET}")

    # 記住成功連線的位址
    if host != OLLAMA_HOST or port != OLLAMA_PORT:
        _config["llm_host"] = host
        _config["llm_port"] = port
        _config.pop("ollama_host", None)
        _config.pop("ollama_port", None)
        save_config(_config)

    # 建立選項列表
    _last_model = _config.get("last_llm_model")
    options = []
    if server_type == "ollama":
        for model_name in available_models:
            desc = next((d for n, d in OLLAMA_MODELS if n == model_name), "")
            options.append((f"Ollama {model_name}", desc, "llm", model_name))
    else:
        for model_name in available_models:
            options.append((model_name, "", "llm", model_name))
    options.append(("Argos 本機離線", "品質一般，免網路免 LLM 伺服器", "argos", None))

    # 計算顯示寬度以對齊欄位
    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    col = max(_dw(label) for label, *_ in options) + 2

    # 預設選 qwen2.5:14b（若有），否則第一個
    default_idx = 0
    for i, (_, _, eng, mod) in enumerate(options):
        if mod == "qwen2.5:14b":
            default_idx = i
            break

    for i, (label, desc, engine, model) in enumerate(options):
        padded = label + ' ' * (col - _dw(label))
        tags = []
        if i == default_idx:
            tags.append(f"{C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        if model and model == _last_model:
            tags.append(f"{C_OK}{REVERSE} 前次使用 {RESET}")
        tag_str = " ".join(tags)
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {tag_str}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}  {tag_str}")
    # 檢查推薦翻譯模型是否存在於伺服器
    _rec_names = {n for n, _ in _BUILTIN_TRANSLATE_MODELS}
    _avail_names = {mod for _, _, eng, mod in options if eng == "llm"}
    if not _rec_names & _avail_names:
        _rec_list = " / ".join(n for n, _ in _BUILTIN_TRANSLATE_MODELS)
        print(f"  {C_HIGHLIGHT}注意：本 LLM 伺服器未安裝推薦翻譯模型（{_rec_list}），翻譯品質可能不如預期{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    idx = default_idx
    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(options)):
                idx = 0
        except ValueError:
            idx = 0

    label, desc, engine, model = options[idx]
    print(f"  {C_OK}→ {label}{RESET}\n")
    if engine == "llm":
        # 記住本次使用的模型
        if model != _config.get("last_llm_model"):
            _config["last_llm_model"] = model
            save_config(_config)
        return engine, model, host, port, server_type
    else:
        return engine, None, None, None, None


def _select_llm_model(host, port, server_type):
    """CLI 模式下讓使用者選擇 LLM 翻譯模型（-e llm 但沒指定 --llm-model）"""
    available_models = _llm_list_models(host, port, server_type)

    if not available_models:
        print(f"  {C_HIGHLIGHT}[警告] LLM 伺服器無可用模型，使用預設 qwen2.5:14b{RESET}")
        return "qwen2.5:14b"

    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    _last_model = _config.get("last_llm_model")
    options = []
    if server_type == "ollama":
        for model_name in available_models:
            desc = next((d for n, d in OLLAMA_MODELS if n == model_name), "")
            options.append((f"Ollama {model_name}", desc, model_name))
    else:
        for model_name in available_models:
            options.append((model_name, "", model_name))

    col = max(_dw(label) for label, *_ in options) + 2

    default_idx = 0
    for i, (_, _, mod) in enumerate(options):
        if mod == "qwen2.5:14b":
            default_idx = i
            break

    print(f"\n\n{C_TITLE}{BOLD}▎ LLM 翻譯模型{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for i, (label, desc, mod) in enumerate(options):
        padded = label + ' ' * (col - _dw(label))
        tags = []
        if i == default_idx:
            tags.append(f"{C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        if mod and mod == _last_model:
            tags.append(f"{C_OK}{REVERSE} 前次使用 {RESET}")
        tag_str = " ".join(tags)
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {tag_str}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}  {tag_str}")
    # 檢查推薦翻譯模型是否存在於伺服器
    _rec_names2 = {n for n, _ in _BUILTIN_TRANSLATE_MODELS}
    _avail_names2 = set(available_models)
    if not _rec_names2 & _avail_names2:
        _rec_list2 = " / ".join(n for n, _ in _BUILTIN_TRANSLATE_MODELS)
        print(f"  {C_HIGHLIGHT}注意：本 LLM 伺服器未安裝推薦翻譯模型（{_rec_list2}），翻譯品質可能不如預期{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    idx = default_idx
    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(options)):
                idx = default_idx
        except ValueError:
            idx = default_idx

    label, desc, model = options[idx]
    print(f"  {C_OK}→ {label}{RESET}\n")
    # 記住本次使用的模型
    if model != _config.get("last_llm_model"):
        _config["last_llm_model"] = model
        save_config(_config)
    return model


def _input_interactive_menu(args):
    """--input 互動選單：選擇模式、講者辨識、摘要"""

    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    try:
        # 顯示輸入檔案資訊
        print(f"\n\n{C_TITLE}{BOLD}▎ 離線處理音訊檔{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        for fpath in args.input:
            fname = os.path.basename(fpath)
            fdir = os.path.dirname(os.path.abspath(fpath))
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath)
                if size >= 1024 * 1024:
                    size_str = f"{size / (1024 * 1024):.1f} MB"
                else:
                    size_str = f"{size / 1024:.0f} KB"
                print(f"  {C_WHITE}{fname}{RESET}  {C_DIM}({size_str}){RESET}")
            else:
                print(f"  {C_WHITE}{fname}{RESET}  {C_HIGHLIGHT}(檔案不存在){RESET}")
            print(f"  {C_DIM}{fdir}{RESET}")
        if len(args.input) > 1:
            print(f"  {C_DIM}共 {len(args.input)} 個檔案{RESET}")

        # ── 第一步：功能模式 ──
        default_mode = 0
        # 如果 CLI 帶了 --diarize，預設辨識選項改為「自動偵測」
        cli_diarize = args.diarize

        # 離線處理過濾掉「純錄音」模式，並改用離線用語
        _input_labels = {"en2zh": ("英文轉錄+中文翻譯", "英文語音 → 轉錄並翻譯成繁體中文"),
                         "zh2en": ("中文轉錄+英文翻譯", "中文語音 → 轉錄並翻譯成英文")}
        input_modes = [
            (k, _input_labels[k][0], _input_labels[k][1]) if k in _input_labels else (k, n, d)
            for k, n, d in MODE_PRESETS if k != "record"
        ]

        print(f"\n\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(name) for _, name, _ in input_modes) + 2
        for i, (key, name, desc) in enumerate(input_modes):
            padded = name + ' ' * (col - _dw(name))
            if i == default_mode:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

        user_input = input().strip()
        if user_input:
            try:
                idx = int(user_input)
                if not (0 <= idx < len(input_modes)):
                    idx = default_mode
            except ValueError:
                idx = default_mode
        else:
            idx = default_mode
        mode_key, mode_name, mode_desc = input_modes[idx]
        is_chinese = mode_key in ("zh", "zh2en")
        need_translate = mode_key in ("en2zh", "zh2en")

        # ── 第二步：辨識模型（依語言過濾）──
        available_models = []
        for name, _filename, desc in WHISPER_MODELS:
            if is_chinese and name.endswith(".en"):
                continue
            available_models.append((name, desc))
        # 預設：large-v3-turbo（速度快且準確度高）
        default_fw = 0
        default_name = "large-v3-turbo"
        for i, (name, _) in enumerate(available_models):
            if name == default_name:
                default_fw = i
                break

        # 查詢遠端已快取的模型（若有遠端設定且伺服器在線）
        remote_cached_models = set()
        if REMOTE_WHISPER_CONFIG:
            remote_cached_models = _remote_whisper_models(REMOTE_WHISPER_CONFIG, timeout=3)

        print(f"\n\n{C_TITLE}{BOLD}▎ 辨識模型{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(len(name) for name, _ in available_models) + 2
        dcol = max(_str_display_width(desc) for _, desc in available_models) + 2
        for i, (name, desc) in enumerate(available_models):
            padded = name + ' ' * (col - len(name))
            dpadded = desc + ' ' * (dcol - _str_display_width(desc))
            # 遠端快取標記
            cache_tag = ""
            if remote_cached_models:
                if name in remote_cached_models:
                    cache_tag = f" {C_OK}✓{RESET}"
                else:
                    cache_tag = f" {C_DIM}(需下載){RESET}"
            if i == default_fw:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{dpadded}{RESET}{cache_tag}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{dpadded}{RESET}{cache_tag}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

        user_input = input().strip()
        if user_input:
            try:
                fw_idx = int(user_input)
                if not (0 <= fw_idx < len(available_models)):
                    fw_idx = default_fw
            except ValueError:
                fw_idx = default_fw
        else:
            fw_idx = default_fw
        fw_model = available_models[fw_idx][0]

        # ── 辨識位置 ──
        use_remote_whisper = False
        if REMOTE_WHISPER_CONFIG:
            rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
            location_options = [
                (f"遠端 GPU（{rw_host}，速度快 5-10 倍）", ""),
                ("本機", ""),
            ]
            default_loc = 0
        else:
            location_options = [
                ("本機", ""),
                ("遠端 GPU（尚未設定）", ""),
            ]
            default_loc = 0

        print(f"\n\n{C_TITLE}{BOLD}▎ 辨識位置{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(l) for l, _ in location_options) + 2
        for i, (label, _) in enumerate(location_options):
            padded = label + ' ' * (col - _dw(label))
            if i == default_loc:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

        user_input = input().strip()
        if user_input:
            try:
                loc_idx = int(user_input)
                if not (0 <= loc_idx < len(location_options)):
                    loc_idx = default_loc
            except ValueError:
                loc_idx = default_loc
        else:
            loc_idx = default_loc

        if REMOTE_WHISPER_CONFIG:
            use_remote_whisper = loc_idx == 0
            # 警告：選了遠端但模型未快取
            if use_remote_whisper and remote_cached_models and fw_model not in remote_cached_models:
                print(f"  {C_HIGHLIGHT}[注意] 模型 {fw_model} 尚未下載到遠端，首次辨識需要先下載（可能需數分鐘）{RESET}")
        else:
            # 沒設定時選了遠端 → 提示並降級
            if loc_idx == 1:
                print(f"  {C_HIGHLIGHT}[提示] 遠端 GPU 辨識尚未設定，請執行 ./install.sh 進行設定{RESET}")
                print(f"  {C_DIM}本次將使用本機 辨識{RESET}")
            use_remote_whisper = False

        # ── 第三步：LLM 伺服器 + 翻譯模型（僅翻譯模式）──
        ollama_model = None
        ollama_host = OLLAMA_HOST
        ollama_port = OLLAMA_PORT
        ollama_asked = False
        llm_server_type = None

        if need_translate:
            # LLM 伺服器
            print(f"\n\n{C_TITLE}{BOLD}▎ LLM 伺服器{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            if ollama_host:
                default_addr = f"{ollama_host}:{ollama_port}"
                print(f"  {C_WHITE}目前設定: {default_addr}{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"{C_WHITE}按 Enter 使用目前設定，或輸入新位址（host:port）：{RESET}", end=" ")
            else:
                print(f"  {C_DIM}尚未設定 LLM 伺服器{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"{C_WHITE}輸入 LLM 伺服器位址（host:port），或按 Enter 使用離線翻譯：{RESET}", end=" ")

            addr_input = input().strip()
            if addr_input:
                if ":" in addr_input:
                    parts = addr_input.rsplit(":", 1)
                    ollama_host = parts[0]
                    try:
                        ollama_port = int(parts[1])
                    except ValueError:
                        ollama_port = OLLAMA_PORT
                else:
                    ollama_host = addr_input
            ollama_asked = True

            # 偵測伺服器類型
            if ollama_host:
                print(f"  {C_DIM}正在偵測 LLM 伺服器...{RESET}", end=" ", flush=True)
                llm_server_type, llm_models = _check_llm_server(ollama_host, ollama_port)
                if llm_server_type:
                    srv_label = "Ollama" if llm_server_type == "ollama" else "OpenAI 相容"
                    print(f"{C_OK}✓ {srv_label} @ {ollama_host}:{ollama_port}（{len(llm_models)} 個模型）{RESET}")
                else:
                    print(f"{C_HIGHLIGHT}未偵測到 LLM 伺服器（{ollama_host}:{ollama_port}）{RESET}")
                    print(f"  {C_HIGHLIGHT}⚠ 翻譯功能需要 LLM 伺服器，請確認伺服器已啟動{RESET}")
            else:
                llm_models = []

            # 翻譯模型
            if llm_server_type == "ollama":
                translate_models = [(n, d) for n, d in OLLAMA_MODELS]
            elif llm_server_type == "openai":
                translate_models = [(m, "") for m in llm_models]
            else:
                translate_models = [(n, d) for n, d in OLLAMA_MODELS]
                llm_server_type = "ollama"  # 預設假設 Ollama，實際連線時再偵測

            default_ollama = 0
            for i, (name, _) in enumerate(translate_models):
                if name == "qwen2.5:14b":
                    default_ollama = i
                    break
            print(f"\n\n{C_TITLE}{BOLD}▎ 翻譯模型{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            col = max(len(name) for name, _ in translate_models) + 2
            for i, (name, desc) in enumerate(translate_models):
                padded = name + ' ' * (col - len(name))
                if i == default_ollama:
                    print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                else:
                    print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

            user_input = input().strip()
            if user_input:
                try:
                    o_idx = int(user_input)
                    if not (0 <= o_idx < len(translate_models)):
                        o_idx = default_ollama
                except ValueError:
                    o_idx = default_ollama
            else:
                o_idx = default_ollama
            ollama_model = translate_models[o_idx][0]

        # ── 第四步：講者辨識 ──
        default_diarize = 1
        diarize_options = [
            ("不辨識", ""),
            ("自動偵測講者數", ""),
            ("指定講者數", ""),
        ]

        print(f"\n\n{C_TITLE}{BOLD}▎ 講者辨識{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(l) for l, _ in diarize_options) + 2
        for i, (label, _) in enumerate(diarize_options):
            padded = label + ' ' * (col - _dw(label))
            if i == default_diarize:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET}")
        print(f"  {C_HIGHLIGHT}* 若講者超過 2 位，建議選 [2] 指定人數以提升辨識正確率{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

        user_input = input().strip()
        if user_input:
            try:
                d_idx = int(user_input)
                if not (0 <= d_idx < len(diarize_options)):
                    d_idx = default_diarize
            except ValueError:
                d_idx = default_diarize
        else:
            d_idx = default_diarize

        diarize = d_idx > 0
        num_speakers = None
        if d_idx == 2:
            # 追問講者人數
            print(f"  {C_WHITE}講者人數（2~20）：{RESET}", end=" ")
            sp_input = input().strip()
            if sp_input:
                try:
                    num_speakers = int(sp_input)
                    if not (2 <= num_speakers <= 20):
                        num_speakers = 2
                except ValueError:
                    num_speakers = 2
            else:
                num_speakers = 2

        # ── 第五步：摘要 ──
        default_summarize = 0
        summarize_options = [
            ("產出摘要與校正逐字稿", "both"),
            ("只產出摘要", "summary"),
            ("只產出逐字稿", "transcript"),
        ]

        print(f"\n\n{C_TITLE}{BOLD}▎ 摘要與逐字稿校正{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(l) for l, _ in summarize_options) + 2
        for i, (label, _) in enumerate(summarize_options):
            padded = label + ' ' * (col - _dw(label))
            if i == default_summarize:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

        user_input = input().strip()
        if user_input:
            try:
                s_idx = int(user_input)
                if not (0 <= s_idx < len(summarize_options)):
                    s_idx = default_summarize
            except ValueError:
                s_idx = default_summarize
        else:
            s_idx = default_summarize
        summary_mode = summarize_options[s_idx][1]
        do_summarize = True

        # 選了摘要 → 先確認 LLM 伺服器（若翻譯步驟未問過）→ 選摘要模型
        summary_model = SUMMARY_DEFAULT_MODEL
        if do_summarize:
            if not ollama_asked:
                default_addr = f"{ollama_host}:{ollama_port}"
                print(f"\n\n{C_TITLE}{BOLD}▎ LLM 伺服器{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"  {C_WHITE}目前設定: {default_addr}{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"{C_WHITE}按 Enter 使用目前設定，或輸入新位址（host:port）：{RESET}", end=" ")

                addr_input = input().strip()
                if addr_input:
                    if ":" in addr_input:
                        parts = addr_input.rsplit(":", 1)
                        ollama_host = parts[0]
                        try:
                            ollama_port = int(parts[1])
                        except ValueError:
                            ollama_port = OLLAMA_PORT
                    else:
                        ollama_host = addr_input

                # 偵測伺服器類型
                print(f"  {C_DIM}正在偵測 LLM 伺服器...{RESET}", end=" ", flush=True)
                llm_server_type, llm_models = _check_llm_server(ollama_host, ollama_port)
                if llm_server_type:
                    srv_label = "Ollama" if llm_server_type == "ollama" else "OpenAI 相容"
                    print(f"{C_OK}✓ {srv_label} @ {ollama_host}:{ollama_port}（{len(llm_models)} 個模型）{RESET}")
                else:
                    print(f"{C_HIGHLIGHT}未偵測到 LLM 伺服器（{ollama_host}:{ollama_port}）{RESET}")
                    print(f"  {C_HIGHLIGHT}⚠ 摘要功能需要 LLM 伺服器，請確認伺服器已啟動{RESET}")

            # 摘要模型：列出伺服器上所有模型
            all_summary_models = _llm_list_models(ollama_host, ollama_port, llm_server_type or "ollama")
            summary_models_list = []
            for m_name in all_summary_models:
                desc = next((d for n, d in SUMMARY_MODELS if n == m_name), "")
                summary_models_list.append((m_name, desc))
            if not summary_models_list:
                summary_models_list = [(n, d) for n, d in SUMMARY_MODELS]

            _last_summary = _config.get("last_summary_model")
            default_sm = 0
            for i, (name, _) in enumerate(summary_models_list):
                if name == SUMMARY_DEFAULT_MODEL:
                    default_sm = i
                    break

            def _dw_sm(s):
                return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

            col = max(_dw_sm(name) for name, _ in summary_models_list) + 2
            print(f"\n\n{C_TITLE}{BOLD}▎ 摘要模型{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            for i, (name, desc) in enumerate(summary_models_list):
                padded = name + ' ' * (col - _dw_sm(name))
                tags = []
                if i == default_sm:
                    tags.append(f"{C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                if name == _last_summary:
                    tags.append(f"{C_OK}{REVERSE} 前次使用 {RESET}")
                tag_str = " ".join(tags)
                if i == default_sm:
                    print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {tag_str}")
                else:
                    print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}  {tag_str}")
            # 檢查推薦摘要模型是否存在於伺服器
            _rec_sm = {n for n, _ in _BUILTIN_SUMMARY_MODELS}
            _avail_sm = {n for n, _ in summary_models_list}
            if not _rec_sm & _avail_sm:
                _rec_sm_list = " / ".join(n for n, _ in _BUILTIN_SUMMARY_MODELS)
                print(f"  {C_HIGHLIGHT}注意：本 LLM 伺服器未安裝推薦摘要模型（{_rec_sm_list}），摘要品質可能不如預期{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")

            user_input = input().strip()
            if user_input:
                try:
                    sm_idx = int(user_input)
                    if not (0 <= sm_idx < len(summary_models_list)):
                        sm_idx = default_sm
                except ValueError:
                    sm_idx = default_sm
            else:
                sm_idx = default_sm
            summary_model = summary_models_list[sm_idx][0]
            # 記住本次使用的摘要模型
            if summary_model != _config.get("last_summary_model"):
                _config["last_summary_model"] = summary_model
                save_config(_config)

        # 記住 LLM 伺服器位址（如果有改）
        if ollama_host != OLLAMA_HOST or ollama_port != OLLAMA_PORT:
            _config["llm_host"] = ollama_host
            _config["llm_port"] = ollama_port
            _config.pop("ollama_host", None)
            _config.pop("ollama_port", None)
            save_config(_config)

        # ── 主題（選填，提升翻譯與摘要品質）──
        meeting_topic = None
        print(f"\n\n{C_TITLE}{BOLD}▎ 會議主題（選填，提升翻譯與摘要品質）{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_WHITE}輸入此次會議的主題或領域，例如：K8s 安全架構、ZFS 儲存管理{RESET}")
        print(f"  {C_DIM}若無特定主題要填寫，可直接按 Enter 跳過{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}會議主題：{RESET}", end=" ")

        if hasattr(sys.stdin, 'buffer'):
            sys.stdout.flush()
            raw = sys.stdin.buffer.readline()
            topic_input = raw.decode('utf-8', errors='replace').strip()
        else:
            topic_input = input().strip()

        if topic_input:
            meeting_topic = topic_input
            print(f"  {C_OK}→ 主題: {meeting_topic}{RESET}")
        else:
            print(f"  {C_DIM}→ 跳過{RESET}")

        # ── 確認設定總覽 ──
        diarize_desc = "關閉"
        if d_idx == 1:
            diarize_desc = "自動偵測"
        elif d_idx == 2:
            diarize_desc = f"指定 {num_speakers} 人"

        print(f"\n{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_OK}→ {mode_name}{RESET}  {C_DIM}辨識: {fw_model}{RESET}")
        if use_remote_whisper:
            rw_h = REMOTE_WHISPER_CONFIG.get("host", "?")
            print(f"  {C_OK}  辨識位置: 遠端 GPU（{rw_h}）{RESET}")
        if ollama_model:
            print(f"  {C_OK}  翻譯模型: {ollama_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        if diarize_desc != "關閉" and use_remote_whisper:
            rw_h2 = REMOTE_WHISPER_CONFIG.get("host", "?")
            diarize_desc += f"，遠端 GPU（{rw_h2}）"
        elif diarize_desc != "關閉":
            diarize_desc += "，本機"
        print(f"  {C_OK}  講者辨識: {diarize_desc}{RESET}")
        if do_summarize:
            print(f"  {C_OK}  摘要模型: {summary_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        if meeting_topic:
            print(f"  {C_OK}  會議主題: {meeting_topic}{RESET}")
        print()

        return (mode_key, fw_model, ollama_model, summary_model,
                ollama_host, ollama_port, diarize, num_speakers, do_summarize,
                llm_server_type, use_remote_whisper, meeting_topic, summary_mode)

    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def run_stream(capture_id: int, translator, model_name: str, model_path: str,
               length_ms: int = 5000, step_ms: int = 3000, mode: str = "en2zh",
               record: bool = False, rec_device: int = None,
               meeting_topic: str = None):
    """啟動 whisper-stream 子程序並即時翻譯輸出"""

    whisper_lang = "en" if mode in ("en2zh", "en") else "zh"
    cmd = [
        WHISPER_STREAM,
        "-m", model_path,
        "-c", str(capture_id),
        "-l", whisper_lang,
        "-t", "8",
        "--step", str(step_ms),
        "--length", str(length_ms),
        "--keep", "200",
        "--vad-thold", "0.8",
    ]

    # 翻譯記錄檔（以時間命名）
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿", "en": "英文_逐字稿", "zh": "中文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # 錄音（獨立 InputStream 平行讀裝置）
    # 注意：capture_id 是 SDL2 裝置 ID（whisper-stream 用），
    # sounddevice 用的是 PortAudio 裝置 ID，需要 rec_device 指定
    recorder = None
    rec_stream = None
    if record:
        import sounddevice as sd
        import numpy as np
        # 使用指定的錄音裝置，或自動找 BlackHole
        rec_dev_id = rec_device
        if rec_dev_id is None:
            sd_devices = sd.query_devices()
            for i, dev in enumerate(sd_devices):
                if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
                    rec_dev_id = i
                    break
            if rec_dev_id is None:
                rec_dev_id = sd.default.device[0]
        dev_info = sd.query_devices(rec_dev_id)
        rec_sr = int(dev_info["default_samplerate"])
        rec_ch = max(dev_info["max_input_channels"], 1)
        recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic)

        def rec_callback(indata, frames, time_info, status):
            recorder.write_raw(indata)
            _push_rms(float(np.sqrt(np.mean(indata ** 2))))

        try:
            rec_stream = sd.InputStream(device=rec_dev_id, samplerate=rec_sr,
                                        channels=rec_ch, dtype="float32",
                                        blocksize=int(rec_sr * 0.1),
                                        callback=rec_callback)
        except Exception as e:
            print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_dev_id}]: {e}{RESET}")
            print(f"  {C_DIM}跳過錄音，繼續辨識。如需錄音請重啟程式。{RESET}")
            recorder.close()
            recorder = None
            rec_stream = None

    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ 本機{RESET}")
    if translator:
        if isinstance(translator, OllamaTranslator):
            _srv_type_label = "Ollama" if translator.server_type == "ollama" else "OpenAI 相容"
            print(f"  {C_OK}翻譯引擎: {translator.model} @ {translator.host}:{translator.port}（{_srv_type_label}）{RESET}")
        elif isinstance(translator, ArgosTranslator):
            print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder:
        print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_WHITE}會議主題: {translator.meeting_topic}{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # 使用 -f 選項將文字輸出到檔案，同時我們 tail 檔案
    # 但 whisper-stream 的 stdout 輸出用了 ANSI escape codes
    # 改用 --file 寫入檔案再讀取
    output_file = os.path.join(SCRIPT_DIR, ".whisper_output.txt")

    # 清空舊檔案
    with open(output_file, "w") as f:
        pass

    cmd.extend(["-f", output_file])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # 啟動錄音串流（在 subprocess 啟動後）
    if rec_stream:
        rec_stream.start()

    stop_keypress = threading.Event()
    pause_event = threading.Event()
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_keypress,),
        kwargs={"pause_event": pause_event},
        daemon=True,
    )
    kp_thread.start()

    # 被動音量監控（稍後初始化，signal_handler 透過閉包取得）
    audio_monitor = None

    # 設定 signal handler
    def signal_handler(signum, frame):
        clear_status_bar()
        restore_terminal()
        stop_keypress.set()
        _stop_audio_monitor(audio_monitor)
        # 停止錄音
        if rec_stream:
            try:
                rec_stream.stop()
                rec_stream.close()
            except Exception:
                pass
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 清理暫存檔
        if os.path.exists(output_file):
            os.remove(output_file)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 監控 whisper-stream 的 stderr 來偵測啟動狀態
    # 等待模型載入完成
    print(f"{C_DIM}正在載入 whisper 模型（首次可能需要幾秒）...{RESET}", flush=True)

    # 用一個非阻塞方式讀 stderr
    def read_stderr():
        for line in proc.stderr:
            line = line.decode("utf-8", errors="replace").strip()
            if line:
                # 只顯示重要的 stderr 訊息
                if "failed" in line.lower() or "error" in line.lower():
                    print(f"[whisper] {line}", file=sys.stderr)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    # 等待 whisper-stream 開始輸出
    time.sleep(2)

    if proc.poll() is not None:
        print(f"[錯誤] whisper-stream 意外退出 (code={proc.returncode})", file=sys.stderr)
        if os.path.exists(output_file):
            os.remove(output_file)
        sys.exit(1)

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "zh2en": "說中文即可看到英文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)

    # 設定底部固定狀態列（快捷鍵提示 + 即時資訊）
    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else "")
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, ArgosTranslator) else "")
    setup_status_bar(mode, model_name=model_name, asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # 被動音量監控（Whisper 無錄音時，開輕量 stream 讀 BlackHole 給狀態列波形）
    if not record:
        audio_monitor = _start_audio_monitor()

    # 非同步翻譯：英文立刻顯示，中文在背景翻完再補上（有序輸出）
    print_lock = threading.Lock()
    _trans_seq = [0]       # 遞增序號
    _trans_pending = {}    # seq → (src_text, result, elapsed)
    _trans_next = [0]      # 下一個該顯示的序號
    _trans_lock = threading.Lock()

    def _drain_translations(log_path):
        """按序號依序輸出所有已就緒的翻譯結果"""
        while True:
            with _trans_lock:
                entry = _trans_pending.pop(_trans_next[0], None)
                if entry is None:
                    break
                _trans_next[0] += 1
            src_text, result, elapsed = entry
            if not result:
                continue
            if elapsed < 1.0:
                speed_badge = C_BADGE_FAST
            elif elapsed < 3.0:
                speed_badge = C_BADGE_NORMAL
            else:
                speed_badge = C_BADGE_SLOW
            if mode == "zh2en":
                src_color, src_label = C_ZH, "中"
                dst_color, dst_label = C_EN, "EN"
            else:
                src_color, src_label = C_EN, "EN"
                dst_color, dst_label = C_ZH, "中"
            with print_lock:
                # 原文與翻譯配對輸出
                print(f"{src_color}[{src_label}] {src_text}{RESET}", flush=True)
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            # 寫入記錄檔
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")

    def translate_and_print(seq, src_text, log_path):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed)
        _drain_translations(log_path)

    # 持續讀取輸出檔案的新內容
    last_size = 0
    last_translated = ""
    buffer = ""
    _loop_tick = 0

    while proc.poll() is None:
        try:
            # 每約 0.2 秒更新狀態列（含波形）
            _loop_tick += 1
            if _loop_tick >= 2 and _status_bar_active:
                _loop_tick = 0
                refresh_status_bar()

            if not os.path.exists(output_file):
                time.sleep(0.1)
                continue

            current_size = os.path.getsize(output_file)
            if current_size > last_size:
                if pause_event.is_set():
                    # 暫停中：跳過新輸出，避免恢復後爆量
                    last_size = current_size
                    buffer = ""
                    time.sleep(0.1)
                    continue
                with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    new_data = f.read()
                last_size = current_size

                buffer += new_data

                # 處理完整的行
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    # whisper-stream 用 \r 覆蓋行做即時更新，取最後一段
                    if "\r" in line:
                        line = line.rsplit("\r", 1)[-1]
                    line = line.strip()
                    if not line:
                        continue

                    # 清理 ANSI escape codes 和 whisper 特殊標記
                    line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
                    line = re.sub(r"\[BLANK_AUDIO\]", "", line)
                    line = re.sub(r"\(.*?\)", "", line)  # 移除 (music), (silence) 等
                    line = line.strip()

                    if not line or line == last_translated:
                        continue

                    if mode in ("en2zh", "en"):
                        # 英文模式：過濾英文幻覺
                        stripped_alpha = re.sub(r"[^a-zA-Z]", "", line)
                        if len(stripped_alpha) < 3:
                            continue
                        line_lower = line.lower().strip(".")
                        if line_lower in (
                            "you", "the", "bye", "so", "okay",
                            "thank you", "thanks for watching",
                            "thanks for listening", "see you next time",
                            "subscribe", "like and subscribe",
                        ):
                            continue

                        if mode == "en":
                            # 英文轉錄：直接顯示
                            with print_lock:
                                print(f"{C_EN}{BOLD}[EN] {line}{RESET}", flush=True)
                                print(flush=True)
                                _status_bar_state["count"] += 1
                                refresh_status_bar()
                            last_translated = line
                            timestamp = time.strftime("%H:%M:%S")
                            with open(log_path, "a", encoding="utf-8") as log_f:
                                log_f.write(f"[{timestamp}] [EN] {line}\n\n")
                        else:
                            # 英翻中：原文延後到翻譯完成時一起顯示
                            last_translated = line
                            seq = _trans_seq[0]; _trans_seq[0] += 1
                            t = threading.Thread(
                                target=translate_and_print,
                                args=(seq, line, log_path),
                                daemon=True,
                            )
                            t.start()

                    elif mode == "zh2en":
                        # 中翻英模式：中文輸入過濾 + 翻譯成英文
                        stripped_zh = re.sub(r"[^\u4e00-\u9fff]", "", line)
                        if len(stripped_zh) < 2:
                            continue
                        line = S2TWP.convert(line)
                        if line == last_translated:
                            continue
                        # 過濾中文幻覺
                        if any(kw in line for kw in (
                            "訂閱", "點贊", "點讚", "轉發", "打賞",
                            "感謝觀看", "謝謝大家", "謝謝收看",
                            "字幕由", "字幕提供",
                            "獨播", "劇場", "YoYo", "Television Series",
                            "歡迎訂閱", "明鏡", "新聞頻道",
                        )):
                            continue
                        # 原文延後到翻譯完成時一起顯示
                        last_translated = line
                        # 背景執行緒翻譯成英文
                        seq = _trans_seq[0]; _trans_seq[0] += 1
                        t = threading.Thread(
                            target=translate_and_print,
                            args=(seq, line, log_path),
                            daemon=True,
                        )
                        t.start()

                    else:
                        # 中文轉錄模式：直接顯示
                        stripped_zh = re.sub(r"[^\u4e00-\u9fff]", "", line)
                        if len(stripped_zh) < 2:
                            continue
                        line = S2TWP.convert(line)
                        if line == last_translated:
                            continue
                        if any(kw in line for kw in (
                            "訂閱", "點贊", "點讚", "轉發", "打賞",
                            "感謝觀看", "謝謝大家", "謝謝收看",
                            "字幕由", "字幕提供",
                            "獨播", "劇場", "YoYo", "Television Series",
                            "歡迎訂閱", "明鏡", "新聞頻道",
                        )):
                            continue
                        with print_lock:
                            print(f"{C_ZH}{BOLD}[中] {line}{RESET}", flush=True)
                            print(flush=True)
                            _status_bar_state["count"] += 1
                            refresh_status_bar()
                        last_translated = line
                        timestamp = time.strftime("%H:%M:%S")
                        with open(log_path, "a", encoding="utf-8") as log_f:
                            log_f.write(f"[{timestamp}] [中] {line}\n\n")

            time.sleep(0.1)

        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)

    # 恢復終端機
    clear_status_bar()
    restore_terminal()
    stop_keypress.set()
    _stop_audio_monitor(audio_monitor)

    # 停止錄音
    if rec_stream:
        try:
            rec_stream.stop()
            rec_stream.close()
        except Exception:
            pass
    if recorder:
        rec_path = recorder.close()
        print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)

    # 清理暫存檔
    if os.path.exists(output_file):
        os.remove(output_file)



def run_stream_moonshine(capture_id: int, translator, moonshine_model_name: str,
                         mode: str = "en2zh",
                         record: bool = False, rec_device: int = None,
                         meeting_topic: str = None):
    """使用 Moonshine ASR 引擎即時串流辨識"""

    # 取得 Moonshine 模型
    arch = _moonshine_model_arch(moonshine_model_name)
    print(f"{C_DIM}正在載入 Moonshine 模型 ({moonshine_model_name})...{RESET}", flush=True)
    model_path, model_arch = get_model_for_language("en", arch)

    # 翻譯記錄檔
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # 錄音（實際建立延後到取得 samplerate 之後）
    recorder = None

    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}ASR 引擎: Moonshine ({moonshine_model_name}){RESET}")
    if translator:
        if isinstance(translator, OllamaTranslator):
            _srv_type_label = "Ollama" if translator.server_type == "ollama" else "OpenAI 相容"
            print(f"  {C_OK}翻譯引擎: {translator.model} @ {translator.host}:{translator.port}（{_srv_type_label}）{RESET}")
        elif isinstance(translator, ArgosTranslator):
            print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_WHITE}會議主題: {translator.meeting_topic}{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    stop_event = threading.Event()
    pause_event = threading.Event()
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_event,),
        kwargs={"pause_event": pause_event},
        daemon=True,
    )
    kp_thread.start()

    # 非同步翻譯（有序輸出）
    print_lock = threading.Lock()
    _trans_seq = [0]
    _trans_pending = {}
    _trans_next = [0]
    _trans_lock = threading.Lock()

    def _drain_translations(log_path):
        """按序號依序輸出所有已就緒的翻譯結果"""
        while True:
            with _trans_lock:
                entry = _trans_pending.pop(_trans_next[0], None)
                if entry is None:
                    break
                _trans_next[0] += 1
            src_text, result, elapsed = entry
            if not result:
                continue
            if elapsed < 1.0:
                speed_badge = C_BADGE_FAST
            elif elapsed < 3.0:
                speed_badge = C_BADGE_NORMAL
            else:
                speed_badge = C_BADGE_SLOW
            if mode == "zh2en":
                src_color, src_label = C_ZH, "中"
                dst_color, dst_label = C_EN, "EN"
            else:
                src_color, src_label = C_EN, "EN"
                dst_color, dst_label = C_ZH, "中"
            with print_lock:
                _clear_partial_line()  # 清除 [...] 部分文字
                # 原文與翻譯配對輸出
                print(f"{src_color}[{src_label}] {src_text}{RESET}", flush=True)
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")

    def translate_and_print(seq, src_text, log_path):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed)
        _drain_translations(log_path)

    # 幻覺過濾
    last_translated = ""

    def is_en_hallucination(text):
        stripped_alpha = re.sub(r"[^a-zA-Z]", "", text)
        if len(stripped_alpha) < 3:
            return True
        line_lower = text.lower().strip(".")
        return line_lower in (
            "you", "the", "bye", "so", "okay",
            "thank you", "thanks for watching",
            "thanks for listening", "see you next time",
            "subscribe", "like and subscribe",
        )

    # 部分文字管理
    _partial_line_id = [None]

    def _clear_partial_line():
        """清除 [...] 部分文字行（需在 print_lock 內呼叫）"""
        if _partial_line_id[0] is not None:
            cols = os.get_terminal_size().columns if hasattr(os, "get_terminal_size") else 80
            print(f"\r{' ' * (cols - 1)}\r", end="", flush=True)
            _partial_line_id[0] = None

    # 建立 Moonshine Transcriber
    transcriber = Transcriber(model_path=model_path, model_arch=model_arch, update_interval=1.0)

    class SubtitleListener(TranscriptEventListener):
        def on_line_text_changed(self, event):
            """即時顯示部分辨識文字（用 \r 覆蓋同一行）"""
            if pause_event.is_set():
                return  # 暫停中，不處理
            if event.line.is_complete:
                return  # completed 事件會處理
            text = event.line.text.strip()
            if not text:
                return
            if mode in ("en2zh", "en"):
                if is_en_hallucination(text):
                    return
                _partial_line_id[0] = event.line.line_id
                with print_lock:
                    # 用 \r 覆蓋當前行，顯示部分文字（灰色）
                    cols = os.get_terminal_size().columns if hasattr(os, "get_terminal_size") else 80
                    partial = f"{C_DIM}[...] {text}{RESET}"
                    # 截斷避免超過終端寬度
                    display_text = f"[...] {text}"
                    if len(display_text) > cols - 1:
                        display_text = display_text[:cols - 4] + "..."
                        partial = f"{C_DIM}{display_text}{RESET}"
                    print(f"\r{partial}", end="", flush=True)

        def on_line_completed(self, event):
            if pause_event.is_set():
                return  # 暫停中，不處理
            nonlocal last_translated
            text = event.line.text.strip()
            if not text or text == last_translated:
                return

            if mode in ("en2zh", "en"):
                if is_en_hallucination(text):
                    return

                if mode == "en":
                    with print_lock:
                        _clear_partial_line()
                        print(f"{C_EN}{BOLD}[EN] {text}{RESET}", flush=True)
                        print(flush=True)
                        _status_bar_state["count"] += 1
                        refresh_status_bar()
                    last_translated = text
                    timestamp = time.strftime("%H:%M:%S")
                    with open(log_path, "a", encoding="utf-8") as log_f:
                        log_f.write(f"[{timestamp}] [EN] {text}\n\n")
                else:
                    # en2zh：原文延後到翻譯完成時一起顯示
                    with print_lock:
                        _clear_partial_line()
                    last_translated = text
                    seq = _trans_seq[0]; _trans_seq[0] += 1
                    t = threading.Thread(
                        target=translate_and_print,
                        args=(seq, text, log_path),
                        daemon=True,
                    )
                    t.start()

        def on_error(self, event):
            with print_lock:
                print(f"{C_HIGHLIGHT}[Moonshine] 錯誤: {event.error}{RESET}", file=sys.stderr, flush=True)

    transcriber.add_listener(SubtitleListener())

    # 啟動預設串流（listener 綁定在此）
    transcriber.start()

    # 取得音訊裝置資訊
    dev_info = sd.query_devices(capture_id)
    sd_samplerate = int(dev_info["default_samplerate"])
    sd_channels = min(dev_info["max_input_channels"], 2)

    # 建立錄音
    rec_stream = None
    if record:
        # 錄音裝置與 ASR 裝置可能不同（例如聚集裝置含麥克風+BlackHole）
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            rec_info = sd.query_devices(rec_device)
            rec_sr = int(rec_info["default_samplerate"])
            rec_ch = max(rec_info["max_input_channels"], 1)
            recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic)

            def rec_callback(indata, frames, time_info, status):
                if not stop_event.is_set():
                    recorder.write_raw(indata)

            try:
                rec_stream = sd.InputStream(device=rec_device, samplerate=rec_sr,
                                            channels=rec_ch, dtype="float32",
                                            blocksize=int(rec_sr * 0.1),
                                            callback=rec_callback)
            except Exception as e:
                print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_device}]: {e}{RESET}")
                print(f"  {C_DIM}跳過錄音，繼續辨識。如需錄音請重啟程式。{RESET}")
                recorder.close()
                recorder = None
                rec_stream = None
                use_separate_rec = False
        else:
            # 錄音裝置與 ASR 同一個，在 audio_callback 裡寫入
            recorder = _AudioRecorder(sd_samplerate, topic=meeting_topic)
        if recorder:
            print(f"  {C_DIM}錄音: {recorder.path}{RESET}")

    def audio_callback(indata, frames, time_info, status):
        if stop_event.is_set():
            return
        # 混音：多聲道 → 單聲道
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        else:
            audio = audio.flatten()
        _push_rms(float(np.sqrt(np.mean(audio ** 2))))
        if recorder and rec_stream is None:
            # 同裝置錄音：寫入 mono
            recorder.write(audio)
        transcriber.add_audio(audio.tolist(), sd_samplerate)

    sd_stream = sd.InputStream(
        device=capture_id,
        samplerate=sd_samplerate,
        channels=sd_channels,
        blocksize=int(sd_samplerate * 0.1),  # 100ms
        dtype="float32",
        callback=audio_callback,
    )

    # 清理 flag，防止重複呼叫
    _cleaned_up = [False]

    def _cleanup_moonshine():
        if _cleaned_up[0]:
            return
        _cleaned_up[0] = True
        stop_event.set()
        if rec_stream:
            try:
                rec_stream.stop()
                rec_stream.close()
            except Exception:
                pass
        try:
            sd_stream.stop()
            sd_stream.close()
        except Exception:
            pass
        try:
            transcriber.stop()
        except Exception:
            pass
        try:
            transcriber.close()
        except Exception:
            pass
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)

    # Signal handler
    def signal_handler(signum, frame):
        clear_status_bar()
        restore_terminal()
        _cleanup_moonshine()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 啟動音訊串流
    sd_stream.start()
    if rec_stream:
        rec_stream.start()

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "en": "說英文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)

    # 設定狀態列
    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else "")
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, ArgosTranslator) else "")
    setup_status_bar(mode, model_name=f"Moonshine {moonshine_model_name}", asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # 主迴圈：等待 Ctrl+C，每 0.2 秒更新狀態列（含波形）
    try:
        while not stop_event.is_set():
            time.sleep(0.2)
            if _status_bar_active:
                with print_lock:
                    refresh_status_bar()
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

    # 恢復終端機
    clear_status_bar()
    restore_terminal()
    _cleanup_moonshine()


def run_stream_remote(capture_id: int, translator, model_name: str,
                      remote_cfg: dict, mode: str = "en2zh",
                      length_ms: int = 5000, step_ms: int = 3000,
                      record: bool = False, rec_device: int = None,
                      force_restart: bool = False,
                      meeting_topic: str = None):
    """使用遠端 GPU Whisper 即時辨識：本機 sounddevice 擷取音訊 →
    環形緩衝 → 定期上傳 WAV 到遠端 → 取回結果 → 翻譯顯示"""
    import numpy as np

    whisper_lang = "en" if mode in ("en2zh", "en") else "zh"

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # ── 啟動遠端伺服器 + 預熱模型 ──
    rw_host = remote_cfg.get("host", "?")
    print(f"\n{C_TITLE}{BOLD}▎ 遠端 GPU 伺服器{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    rs_label = "重啟" if force_restart else "啟動"
    print(f"  {C_DIM}{rs_label}遠端 Whisper 伺服器（{rw_host}）...{RESET}", end="", flush=True)
    _inline_spinner(_remote_whisper_start, remote_cfg, force_restart=force_restart)
    print(f" {C_OK}✓{RESET}")
    print(f"  {C_DIM}等待伺服器就緒...{RESET}", end="", flush=True)
    try:
        ok, has_gpu = _inline_spinner(_remote_whisper_health, remote_cfg, timeout=30)
    except Exception:
        ok, has_gpu = False, False
    if not ok:
        print(f" {C_HIGHLIGHT}失敗{RESET}")
        print(f"  {C_HIGHLIGHT}[錯誤] 遠端 Whisper 伺服器無法連線（{rw_host}）{RESET}", file=sys.stderr)
        print(f"  {C_DIM}請確認遠端設定，或使用 --local-asr 改用本機辨識{RESET}", file=sys.stderr)
        sys.exit(1)
    gpu_label = "GPU" if has_gpu else "CPU"
    print(f" {C_OK}就緒（{gpu_label}）{RESET}")
    # 預熱：送一段靜音讓伺服器載入模型到 GPU（首次可能需 30-60 秒）
    print(f"  {C_DIM}載入模型 {C_WHITE}{model_name}{C_DIM} 到 {gpu_label}（首次可能需 30-60 秒）...{RESET}", end="", flush=True)
    import numpy as _np_warmup
    _warmup_t0 = time.monotonic()
    try:
        silence = _np_warmup.zeros(16000, dtype=_np_warmup.int16)
        warmup_io = io.BytesIO()
        with wave.open(warmup_io, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(silence.tobytes())
        warmup_lang = "en" if mode in ("en2zh", "en") else "zh"
        def _do_warmup():
            return _remote_whisper_transcribe_bytes(
                remote_cfg, warmup_io.getvalue(),
                model_name, warmup_lang, timeout=180)
        _inline_spinner(_do_warmup)
        _warmup_elapsed = time.monotonic() - _warmup_t0
        print(f" {C_OK}就緒（{_warmup_elapsed:.1f}s）{RESET}")
    except Exception as e:
        print(f" {C_HIGHLIGHT}失敗{RESET}")
        print(f"  {C_HIGHLIGHT}[警告] 模型預熱失敗: {e}（首次辨識可能較慢）{RESET}")

    # ── 音訊裝置 ──
    dev_info = sd.query_devices(capture_id)
    sd_samplerate = int(dev_info["default_samplerate"])
    sd_channels = min(dev_info["max_input_channels"], 2)
    target_sr = 16000
    resample_ratio = sd_samplerate / target_sr  # e.g. 48000/16000 = 3

    # ── 錄音 ──
    recorder = None
    rec_stream = None
    if record:
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            rec_info = sd.query_devices(rec_device)
            rec_sr = int(rec_info["default_samplerate"])
            rec_ch = max(rec_info["max_input_channels"], 1)
            recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic)

            def rec_callback(indata, frames, time_info, status):
                if not stop_event.is_set():
                    recorder.write_raw(indata)

            try:
                rec_stream = sd.InputStream(device=rec_device, samplerate=rec_sr,
                                            channels=rec_ch, dtype="float32",
                                            blocksize=int(rec_sr * 0.1),
                                            callback=rec_callback)
            except Exception as e:
                print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_device}]: {e}{RESET}")
                recorder.close()
                recorder = None
                rec_stream = None
                use_separate_rec = False
        else:
            recorder = _AudioRecorder(sd_samplerate, topic=meeting_topic)

    # ── Banner ──
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ 遠端 GPU（{rw_host}）{RESET}")
    if translator:
        if isinstance(translator, OllamaTranslator):
            _srv_type_label = "Ollama" if translator.server_type == "ollama" else "OpenAI 相容"
            print(f"  {C_OK}翻譯引擎: {translator.model} @ {translator.host}:{translator.port}（{_srv_type_label}）{RESET}")
        elif isinstance(translator, ArgosTranslator):
            print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    print(f"  {C_WHITE}音訊緩衝: {length_ms}ms / 步進 {step_ms}ms{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder:
        print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_WHITE}會議主題: {translator.meeting_topic}{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # ── 環形緩衝（16kHz mono float32）──
    ring_size = target_sr * length_ms // 1000  # e.g. 5s = 80000
    ring_buffer = np.zeros(ring_size, dtype=np.float32)
    ring_write_pos = 0
    ring_filled = 0  # 已寫入的總 sample 數
    ring_lock = threading.Lock()

    stop_event = threading.Event()
    pause_event = threading.Event()
    print_lock = threading.Lock()
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_event,),
        kwargs={"pause_event": pause_event},
        daemon=True,
    )
    kp_thread.start()

    # ── sounddevice callback ──
    def audio_callback(indata, frames, time_info, status):
        nonlocal ring_write_pos, ring_filled
        if stop_event.is_set():
            return
        audio = indata.astype(np.float32)
        # 混音：多聲道 → 單聲道
        if audio.ndim > 1 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        else:
            audio = audio.flatten()
        # RMS
        _push_rms(float(np.sqrt(np.mean(audio ** 2))))
        # 同裝置錄音
        if recorder and rec_stream is None:
            recorder.write(audio)
        # 降頻到 16kHz（簡單 decimation）
        step = max(1, int(round(resample_ratio)))
        downsampled = audio[::step]
        # 寫入環形緩衝
        n = len(downsampled)
        with ring_lock:
            if ring_write_pos + n <= ring_size:
                ring_buffer[ring_write_pos:ring_write_pos + n] = downsampled
            else:
                first = ring_size - ring_write_pos
                ring_buffer[ring_write_pos:] = downsampled[:first]
                ring_buffer[:n - first] = downsampled[first:]
            ring_write_pos = (ring_write_pos + n) % ring_size
            ring_filled += n

    sd_stream = sd.InputStream(
        device=capture_id,
        samplerate=sd_samplerate,
        channels=sd_channels,
        blocksize=int(sd_samplerate * 0.1),
        dtype="float32",
        callback=audio_callback,
    )

    # ── 提取 WAV bytes ──
    def extract_wav_bytes():
        """從環形緩衝提取正確順序的音訊，回傳 in-memory WAV bytes"""
        with ring_lock:
            pos = ring_write_pos
            buf_copy = ring_buffer.copy()
        # roll 使 write_pos 變成陣列末端（最新的在最後）
        ordered = np.roll(buf_copy, -pos)
        # float32 → int16 PCM
        pcm = (ordered * 32767).clip(-32768, 32767).astype(np.int16)
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_sr)
            wf.writeframes(pcm.tobytes())
        return wav_io.getvalue()

    # ── 非同步翻譯（有序輸出）──
    _trans_seq = [0]
    _trans_pending = {}
    _trans_next = [0]
    _trans_lock = threading.Lock()

    def _drain_translations(_log_path):
        """按序號依序輸出所有已就緒的翻譯結果"""
        while True:
            with _trans_lock:
                entry = _trans_pending.pop(_trans_next[0], None)
                if entry is None:
                    break
                _trans_next[0] += 1
            src_text, result, elapsed = entry
            if not result:
                continue
            if elapsed < 1.0:
                speed_badge = C_BADGE_FAST
            elif elapsed < 3.0:
                speed_badge = C_BADGE_NORMAL
            else:
                speed_badge = C_BADGE_SLOW
            if mode == "zh2en":
                src_color, src_label = C_ZH, "中"
                dst_color, dst_label = C_EN, "EN"
            else:
                src_color, src_label = C_EN, "EN"
                dst_color, dst_label = C_ZH, "中"
            with print_lock:
                # 原文與翻譯配對輸出，避免多段原文連續出現後翻譯才到
                print(f"{src_color}[{src_label}] {src_text}{RESET}", flush=True)
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(_log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")

    def translate_and_print(seq, src_text, _log_path):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed)
        _drain_translations(_log_path)

    # ── 有序非同步上傳 ──
    upload_seq = [0]
    _UPLOAD_FAILED = "FAILED"  # 失敗標記（與 None 區分）
    pending_results = {}  # seq → (segments, full_text, proc_time) 或 _UPLOAD_FAILED
    next_display_seq = [0]
    results_lock = threading.Lock()

    def upload_chunk(seq, wav_bytes):
        """背景上傳並存結果"""
        try:
            segments, full_text, proc_time = _remote_whisper_transcribe_bytes(
                remote_cfg, wav_bytes, model_name, whisper_lang)
            with results_lock:
                pending_results[seq] = (segments, full_text, proc_time)
        except Exception as e:
            with print_lock:
                print(f"{C_DIM}  [遠端辨識失敗: {e}]{RESET}", flush=True)
            with results_lock:
                pending_results[seq] = _UPLOAD_FAILED

    # ── 去重 ──
    recent_texts = deque(maxlen=10)

    def is_duplicate(text):
        text_lower = text.lower().strip()
        for prev in recent_texts:
            if text_lower == prev or text_lower in prev or prev in text_lower:
                return True
        return False

    # ── 過濾 + 顯示 ──
    if mode in ("en2zh", "en"):
        hallucination_check = _is_en_hallucination
        src_color, src_label = C_EN, "EN"
    else:
        hallucination_check = _is_zh_hallucination
        src_color, src_label = C_ZH, "中"

    def drain_ordered_results():
        """按序號依序處理已完成的辨識結果"""
        _NOT_READY = object()
        while True:
            with results_lock:
                result = pending_results.pop(next_display_seq[0], _NOT_READY)
            if result is _NOT_READY:
                break  # 還沒到，等下次
            next_display_seq[0] += 1
            if result is _UPLOAD_FAILED:
                continue  # 上傳失敗，跳過
            segments, full_text, proc_time = result
            if not full_text:
                continue
            # 處理辨識結果
            # 遠端回傳可能含多個 segment，合併或逐段處理
            lines = []
            if segments:
                for seg in segments:
                    text = seg.get("text", "").strip()
                    if text:
                        lines.append(text)
            else:
                lines = [full_text]

            for line in lines:
                if not line:
                    continue
                # 簡繁轉換（中文模式）
                if mode in ("zh", "zh2en"):
                    line = S2TWP.convert(line)
                # 幻覺過濾
                if hallucination_check(line):
                    continue
                # 去重
                if is_duplicate(line):
                    continue
                recent_texts.append(line.lower().strip())
                # 顯示 + 翻譯
                if mode in ("en2zh", "zh2en") and translator:
                    # 原文延後到翻譯完成時一起顯示，避免多段 [EN] 連續出現
                    seq = _trans_seq[0]; _trans_seq[0] += 1
                    threading.Thread(
                        target=translate_and_print,
                        args=(seq, line, log_path),
                        daemon=True,
                    ).start()
                else:
                    # 純轉錄
                    with print_lock:
                        print(f"{src_color}{BOLD}[{src_label}] {line}{RESET}", flush=True)
                        print(flush=True)
                        _status_bar_state["count"] += 1
                        refresh_status_bar()
                    timestamp = time.strftime("%H:%M:%S")
                    with open(log_path, "a", encoding="utf-8") as log_f:
                        log_f.write(f"[{timestamp}] [{src_label}] {line}\n\n")

    # ── 清理 ──
    _cleaned_up = [False]

    def _cleanup_remote():
        if _cleaned_up[0]:
            return
        _cleaned_up[0] = True
        stop_event.set()
        if rec_stream:
            try:
                rec_stream.stop()
                rec_stream.close()
            except Exception:
                pass
        try:
            sd_stream.stop()
            sd_stream.close()
        except Exception:
            pass
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)
        # 遠端伺服器保持運行（不停止，允許多實例共用）
        _ssh_close_cm(remote_cfg)

    def signal_handler(signum, frame):
        clear_status_bar()
        restore_terminal()
        _cleanup_remote()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── 啟動音訊串流 ──
    sd_stream.start()
    if rec_stream:
        rec_stream.start()

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "zh2en": "說中文即可看到英文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)

    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else "")
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, ArgosTranslator) else "")
    setup_status_bar(mode, model_name=model_name, asr_location="伺服器",
                     translate_model=_tr_model, translate_location=_tr_loc)
    signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # ── 主迴圈 ──
    step_sec = step_ms / 1000.0
    length_samples = ring_size  # 填滿整個緩衝才開始
    next_upload_time = time.monotonic() + (length_ms / 1000.0)  # 首次需等緩衝填滿

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
            # 更新狀態列
            if _status_bar_active:
                with print_lock:
                    refresh_status_bar()

            now = time.monotonic()
            if pause_event.is_set():
                # 暫停中：音訊持續擷取但不上傳
                next_upload_time = now + step_sec
                continue

            if now < next_upload_time:
                # 處理已到達的結果
                drain_ordered_results()
                continue

            # 檢查緩衝是否已填滿
            with ring_lock:
                filled = ring_filled
            if filled < length_samples:
                continue

            next_upload_time = now + step_sec

            # 提取 WAV
            wav_bytes = extract_wav_bytes()

            # RMS 靜音檢查
            with ring_lock:
                buf_copy = ring_buffer.copy()
            rms = float(np.sqrt(np.mean(buf_copy ** 2)))
            if rms < 0.001:
                continue  # 靜音，跳過上傳

            # 背景上傳
            seq = upload_seq[0]
            upload_seq[0] += 1
            threading.Thread(
                target=upload_chunk,
                args=(seq, wav_bytes),
                daemon=True,
            ).start()

            # 處理已到達的結果
            drain_ordered_results()

    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

    # 恢復終端機
    clear_status_bar()
    restore_terminal()
    _cleanup_remote()


def render_markdown(text):
    """將 Markdown 文字加上終端機顏色輸出"""
    C_H1 = "\x1b[38;2;100;180;255m"   # 藍色 - H1/H2
    C_H3 = "\x1b[38;2;180;220;255m"   # 淡藍 - H3
    C_BULLET = "\x1b[38;2;80;255;180m"  # 青綠 - 列表項
    C_HRULE = "\x1b[38;2;100;100;100m"  # 暗灰 - 分隔線
    C_TEXT = "\x1b[38;2;230;230;230m"   # 亮白 - 正文
    C_BOLD_MK = "\x1b[38;2;255;220;80m"  # 黃色 - 粗體文字

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            print(f"\n{C_H3}{BOLD}{stripped}{RESET}")
        elif stripped.startswith("## "):
            print(f"\n{C_H1}{BOLD}{stripped}{RESET}")
        elif stripped.startswith("# "):
            print(f"\n{C_H1}{BOLD}{stripped}{RESET}")
        elif stripped.startswith("---"):
            print(f"{C_HRULE}{'─' * 60}{RESET}")
        elif stripped.startswith("- "):
            bullet_text = stripped[2:]
            # 處理行內粗體 **text**
            bullet_text = re.sub(
                r"\*\*(.+?)\*\*",
                f"{C_BOLD_MK}{BOLD}\\1{RESET}{C_TEXT}",
                bullet_text
            )
            print(f"  {C_BULLET}  - {C_TEXT}{bullet_text}{RESET}")
        elif stripped:
            # 處理行內粗體
            rendered = re.sub(
                r"\*\*(.+?)\*\*",
                f"{C_BOLD_MK}{BOLD}\\1{RESET}{C_TEXT}",
                stripped
            )
            print(f"{C_TEXT}{rendered}{RESET}")
        else:
            print()


def _wait_for_esc():
    """等待使用者按 ESC 鍵（或 Ctrl+C）才退出"""
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, new)
        try:
            while True:
                data = os.read(fd, 32)
                if b'\x1b' in data and b'\x1b[' not in data:
                    break  # ESC 鍵（排除方向鍵等 escape sequence）
                if b'\x1b' in data:
                    break  # 任何 ESC 開頭都算
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old)
    except Exception:
        pass


def _topic_to_filename_part(topic):
    """將主題字串轉為檔名安全片段，最多 20 字元。無主題時回傳空字串。
    過濾 macOS 檔名不允許的字元（/ : NUL）及其他常見問題字元。"""
    if not topic:
        return ""
    # 移除 macOS 不允許的 / : 以及 Windows 不允許的 \\ * ? " < > | 和空白、控制字元
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f\s]+', '_', topic)
    # 移除開頭的 . 避免產生隱藏檔
    safe = safe.lstrip('.')
    safe = safe[:20].strip('_')
    return f"_{safe}" if safe else ""


class _AudioRecorder:
    """將即時模式的音訊錄製為 16-bit PCM WAV 檔。
    定期更新 WAV header，即使程式異常終止也能保留已錄製的音訊。
    close() 時自動轉檔為目標格式（預設 MP3）。"""

    _HEADER_UPDATE_INTERVAL = 30  # 每 30 秒更新一次 WAV header

    def __init__(self, samplerate=16000, channels=1, fmt=None, topic=None):
        os.makedirs(RECORDING_DIR, exist_ok=True)
        from datetime import datetime
        topic_part = _topic_to_filename_part(topic)
        fname = datetime.now().strftime(f"錄音{topic_part}_%Y%m%d_%H%M%S.wav")
        self.path = os.path.join(RECORDING_DIR, fname)
        self._samplerate = samplerate
        self._channels = channels
        self._sampwidth = 2  # 16-bit
        self._target_fmt = fmt if fmt else RECORDING_FORMAT
        # 直接操作檔案，手動寫 WAV header 以便定期更新
        self._f = open(self.path, "wb")
        self._data_size = 0
        self._write_header()
        self._last_header_update = time.monotonic()

    def _write_header(self):
        """寫入或更新 WAV header（seek 回檔頭覆寫）"""
        import struct
        self._f.seek(0)
        block_align = self._channels * self._sampwidth
        byte_rate = self._samplerate * block_align
        file_size = 36 + self._data_size
        self._f.write(struct.pack('<4sI4s', b'RIFF', file_size, b'WAVE'))
        self._f.write(struct.pack('<4sIHHIIHH', b'fmt ', 16, 1,
                                  self._channels, self._samplerate,
                                  byte_rate, block_align,
                                  self._sampwidth * 8))
        self._f.write(struct.pack('<4sI', b'data', self._data_size))
        self._f.seek(0, 2)  # 回到檔尾繼續寫入

    def _maybe_update_header(self):
        """定期更新 header + flush，確保異常終止時檔案可用"""
        now = time.monotonic()
        if now - self._last_header_update >= self._HEADER_UPDATE_INTERVAL:
            self._write_header()
            self._f.flush()
            self._last_header_update = now

    def write(self, float32_mono):
        """寫入 float32 單聲道音訊（自動轉換為 int16）"""
        import numpy as np
        pcm = (float32_mono * 32767).clip(-32768, 32767).astype(np.int16)
        raw = pcm.tobytes()
        self._f.write(raw)
        self._data_size += len(raw)
        self._maybe_update_header()

    def write_raw(self, float32_data):
        """寫入 float32 音訊（多聲道或單聲道皆可，自動轉 int16）"""
        import numpy as np
        data = float32_data.astype(np.float32)
        pcm = (data * 32767).clip(-32768, 32767).astype(np.int16)
        raw = pcm.tobytes()
        self._f.write(raw)
        self._data_size += len(raw)
        self._maybe_update_header()

    def _convert(self):
        """將中間 WAV 轉檔為目標格式。成功後刪除 WAV，更新 self.path。"""
        if self._target_fmt == "wav":
            return
        fmt = self._target_fmt
        wav_path = self.path
        out_path = os.path.splitext(wav_path)[0] + "." + fmt
        codec_args = {
            "mp3":  ["-codec:a", "libmp3lame", "-q:a", "0"],
            "ogg":  ["-codec:a", "libvorbis", "-q:a", "8"],
            "flac": ["-codec:a", "flac"],
        }
        args = codec_args.get(fmt, [])
        cmd = ["ffmpeg", "-y", "-i", wav_path] + args + [out_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
            os.remove(wav_path)
            self.path = out_path
        except Exception:
            print(f"\033[33m[警告] 錄音轉 {fmt} 失敗（保留 WAV）\033[0m")

    def close(self):
        try:
            self._write_header()
            self._f.close()
        except Exception:
            pass
        self._convert()
        return self.path


def _auto_detect_rec_device():
    """自動偵測錄音裝置。回傳 (device_id, device_name, label) 或 (None, None, None)"""
    import sounddevice as sd
    devices = sd.query_devices()
    # 1) 聚集裝置
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            name = dev["name"]
            if "聚集" in name or "aggregate" in name.lower():
                return i, name, "雙方聲音"
    # 2) input channels >= 3 的 Apple 虛擬裝置
    for i, dev in enumerate(devices):
        if (dev["max_input_channels"] >= 3
                and "blackhole" not in dev["name"].lower()):
            return i, dev["name"], "雙方聲音"
    # 3) BlackHole
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
            return i, dev["name"], "僅對方聲音"
    return None, None, None


def _ask_record_source():
    """純錄音模式：選擇錄音來源（雙方聲音 / 僅對方聲音）。
    回傳 (device_id, device_name, label)，找不到裝置則 sys.exit(1)。"""
    import sounddevice as sd
    devices = sd.query_devices()

    # 偵測可用裝置
    aggregate_dev = None   # 聚集裝置（雙方聲音）
    blackhole_dev = None   # BlackHole（僅對方聲音）

    for i, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        name = dev["name"]
        # 聚集裝置
        if aggregate_dev is None:
            if "聚集" in name or "aggregate" in name.lower():
                aggregate_dev = (i, name)
            elif dev["max_input_channels"] >= 3 and "blackhole" not in name.lower():
                aggregate_dev = (i, name)
        # BlackHole
        if blackhole_dev is None and "blackhole" in name.lower():
            blackhole_dev = (i, name)

    # 兩種裝置都找不到 → 用系統預設
    if aggregate_dev is None and blackhole_dev is None:
        default = sd.default.device[0]
        if default is not None and default >= 0:
            dev = sd.query_devices(default)
            print(f"{C_HIGHLIGHT}[提醒] 未偵測到聚集裝置或 BlackHole，使用系統預設輸入{RESET}")
            return default, dev["name"], "系統預設"
        print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
        sys.exit(1)

    # 只有一種裝置 → 直接使用
    if aggregate_dev is None:
        return blackhole_dev[0], blackhole_dev[1], "僅對方聲音"
    if blackhole_dev is None:
        return aggregate_dev[0], aggregate_dev[1], "雙方聲音"

    # 兩種都有 → 讓使用者選擇
    print(f"\n\n{C_TITLE}{BOLD}▎ 錄音來源{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_HIGHLIGHT}{BOLD}[0] 雙方聲音{RESET}  {C_WHITE}對方播放 + 我方麥克風{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
    print(f"  {C_DIM}    {aggregate_dev[1]}{RESET}")
    print(f"  {C_DIM}[1]{RESET} {C_WHITE}僅對方聲音{RESET}  {C_DIM}只錄製系統播放的聲音{RESET}")
    print(f"  {C_DIM}    {blackhole_dev[1]}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}選擇 (0-1) [0]：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input == "1":
        print(f"  {C_OK}→ 僅對方聲音{RESET}")
        return blackhole_dev[0], blackhole_dev[1], "僅對方聲音"
    else:
        print(f"  {C_OK}→ 雙方聲音{RESET}")
        return aggregate_dev[0], aggregate_dev[1], "雙方聲音"


def run_record_only(rec_device, topic=None):
    """純錄音模式：僅錄製音訊為 WAV 檔，不做 ASR 或翻譯。
    聚集裝置（ch>=3）自動分離輸出/輸入音軌並分開顯示波形。"""
    import sounddevice as sd
    import numpy as np

    dev_info = sd.query_devices(rec_device)
    rec_sr = int(dev_info["default_samplerate"])
    rec_ch = max(dev_info["max_input_channels"], 1)
    dev_name = dev_info["name"]

    # 判斷是否為聚集裝置（ch >= 3：前 N-1 ch 為播放音訊，最後 1 ch 為麥克風）
    is_aggregate = rec_ch >= 3
    out_channels = rec_ch - 1 if is_aggregate else rec_ch  # 播放音軌數
    # 如果是 BlackHole (2ch)，全部都是播放音訊

    recorder = _AudioRecorder(rec_sr, rec_ch, topic=topic)
    stop_event = threading.Event()

    # 滾動音量歷史（用於波形顯示）
    _WAVE_MAX = 80  # 最多保留 80 筆歷史（約 8 秒）
    _level_lock = threading.Lock()
    if is_aggregate:
        _out_history = deque(maxlen=_WAVE_MAX)  # 播放音訊（對方聲音）
        _in_history = deque(maxlen=_WAVE_MAX)   # 麥克風（我方聲音）
    else:
        _out_history = deque(maxlen=_WAVE_MAX)  # 單軌
        _in_history = None

    def rec_callback(indata, frames, time_info, status):
        if stop_event.is_set():
            return
        recorder.write_raw(indata)
        data = indata.astype(np.float32)
        if is_aggregate:
            # 前 N-1 聲道：播放音訊（對方）
            out_rms = float(np.sqrt(np.mean(data[:, :out_channels] ** 2)))
            # 最後 1 聲道：麥克風（我方）
            in_rms = float(np.sqrt(np.mean(data[:, -1] ** 2)))
            with _level_lock:
                _out_history.append(out_rms)
                _in_history.append(in_rms)
        else:
            rms = float(np.sqrt(np.mean(data ** 2)))
            with _level_lock:
                _out_history.append(rms)

    try:
        stream = sd.InputStream(device=rec_device, samplerate=rec_sr,
                                channels=rec_ch, dtype="float32",
                                blocksize=int(rec_sr * 0.1),
                                callback=rec_callback)
    except Exception as e:
        print(f"[錯誤] 無法開啟錄音裝置 [{rec_device}] {dev_name}: {e}", file=sys.stderr)
        recorder.close()
        sys.exit(1)

    # Banner
    print(f"\n{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}模式: 純錄音{RESET}")
    print(f"  {C_WHITE}裝置: [{rec_device}] {dev_name} ({rec_ch}ch {rec_sr}Hz){RESET}")
    print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if is_aggregate:
        print(f"  {C_WHITE}音軌: 輸出 {out_channels}ch（對方） + 輸入 1ch（我方）{RESET}")
    print(f"  {C_DIM}按 Ctrl+C 停止錄音{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    stream.start()
    start_time = time.monotonic()

    def _level_color(level):
        if level > 0.05:
            return C_OK         # 綠色
        elif level > 0.003:
            return C_HIGHLIGHT  # 黃色
        return C_DIM            # 灰色

    def _build_wave(history, bar_width):
        samples = list(history)
        if len(samples) >= bar_width:
            samples = samples[-bar_width:]
        else:
            samples = [0.0] * (bar_width - len(samples)) + samples
        cur = samples[-1] if samples else 0.0
        wave = "".join(_rms_to_bar(s) for s in samples)
        return wave, cur

    _first_draw = True
    _prev_cols = [0]

    # SIGWINCH 偵測視窗大小變化
    _resized = [False]
    def _on_winch(signum, frame):
        _resized[0] = True
    signal.signal(signal.SIGWINCH, _on_winch)

    # 固定時間欄位寬度（容納 H:MM:SS），波形寬度不會因跨時而跳動
    _TS_W = 7  # "H:MM:SS" = 7 字元，"MM:SS" 右對齊補空格

    # 雙軌前綴: "  " + ts(7) + "  " + "輸出"(4) + " "(1) = 16
    # 單軌前綴: "  " + ts(7) + "  " = 11
    if is_aggregate:
        _BAR_W = max(60 - (_TS_W + 9), 10)  # 60 - 16 = 44
    else:
        _BAR_W = max(60 - (_TS_W + 4), 10)  # 60 - 11 = 49

    try:
        while True:
            time.sleep(0.15)
            elapsed = time.monotonic() - start_time
            secs = int(elapsed)
            if secs >= 3600:
                ts_raw = f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
            else:
                ts_raw = f"{secs // 60:02d}:{secs % 60:02d}"
            ts = ts_raw.rjust(_TS_W)

            try:
                cols = os.get_terminal_size().columns
            except Exception:
                cols = 80

            # 視窗大小變化：重置繪製（避免殘留行錯位）
            if _resized[0] or cols != _prev_cols[0]:
                _resized[0] = False
                _prev_cols[0] = cols
                if not _first_draw:
                    sys.stdout.write("\x1b[1A\r\x1b[J")
                    sys.stdout.flush()
                    _first_draw = True

            if is_aggregate:
                with _level_lock:
                    out_wave, out_cur = _build_wave(_out_history, _BAR_W)
                    in_wave, in_cur = _build_wave(_in_history, _BAR_W)

                out_color = _level_color(out_cur)
                in_color = _level_color(in_cur)

                out_line = f"  {C_WHITE}{BOLD}{ts}{RESET}  {C_TITLE}輸出{RESET} {out_color}{out_wave}{RESET}"
                in_line = f"  {' ' * _TS_W}  {C_HIGHLIGHT}輸入{RESET} {in_color}{in_wave}{RESET}"

                if _first_draw:
                    sys.stdout.write(f"\r\x1b[K{out_line}\n\r\x1b[K{in_line}")
                    sys.stdout.flush()
                    _first_draw = False
                else:
                    sys.stdout.write(f"\x1b[1A\r\x1b[K{out_line}\n\r\x1b[K{in_line}")
                    sys.stdout.flush()
            else:
                with _level_lock:
                    wave_str, cur_level = _build_wave(_out_history, _BAR_W)
                vol_color = _level_color(cur_level)
                line = f"  {C_WHITE}{BOLD}{ts}{RESET}  {vol_color}{wave_str}{RESET}"
                sys.stdout.write(f"\r\x1b[K{line}")
                sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        stream.stop()
        stream.close()
        path = recorder.close()
        elapsed = time.monotonic() - start_time
        secs = int(elapsed)
        if secs >= 3600:
            ts = f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
        else:
            ts = f"{secs // 60:02d}:{secs % 60:02d}"
        print()
        print(f"\n{C_OK}{BOLD}錄音完成{RESET}")
        print(f"  {C_WHITE}時長: {ts}{RESET}")
        print(f"  {C_WHITE}檔案: {path}{RESET}")
        print()


def _select_audio_files():
    """掃描 RECORDING_DIR，列出音訊檔供選擇（每頁 10 筆，可翻頁）。
    回傳 [filepath] (list)，或 None 表示無檔案。"""
    AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    PAGE_SIZE = 10
    files = []
    if os.path.isdir(RECORDING_DIR):
        for fname in os.listdir(RECORDING_DIR):
            ext = os.path.splitext(fname)[1].lower()
            if ext in AUDIO_EXTS:
                fpath = os.path.join(RECORDING_DIR, fname)
                if os.path.isfile(fpath):
                    files.append((fpath, os.path.getmtime(fpath)))
    if not files:
        return None
    # 按修改時間倒序
    files.sort(key=lambda x: x[1], reverse=True)

    def _human_size(size):
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 ** 3):.1f} GB"
        elif size >= 1024 * 1024:
            return f"{size / (1024 ** 2):.1f} MB"
        else:
            return f"{size / 1024:.0f} KB"

    import time as _time
    import struct as _struct

    def _dw(s):
        """計算字串顯示寬度（中日韓字元佔 2 格）"""
        return sum(2 if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u30ff'
                     or '\uff00' <= c <= '\uffef' else 1 for c in s)

    def _wav_duration(fpath):
        """從 WAV header 快速讀取時長（秒），失敗回傳 None"""
        try:
            with open(fpath, "rb") as f:
                riff = f.read(12)
                if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                    return None
                while True:
                    chunk_hdr = f.read(8)
                    if len(chunk_hdr) < 8:
                        return None
                    chunk_id = chunk_hdr[:4]
                    chunk_size = _struct.unpack("<I", chunk_hdr[4:8])[0]
                    if chunk_id == b"fmt ":
                        fmt_data = f.read(chunk_size)
                        channels = _struct.unpack("<H", fmt_data[2:4])[0]
                        sample_rate = _struct.unpack("<I", fmt_data[4:8])[0]
                        bits_per_sample = _struct.unpack("<H", fmt_data[14:16])[0]
                        if sample_rate == 0 or channels == 0 or bits_per_sample == 0:
                            return None
                    elif chunk_id == b"data":
                        bytes_per_sample = bits_per_sample // 8
                        return chunk_size / (sample_rate * channels * bytes_per_sample)
                    else:
                        f.seek(chunk_size, 1)
        except Exception:
            return None

    def _audio_duration(fpath):
        """取得音訊時長（秒），WAV 直接讀 header，其他用 ffprobe"""
        if fpath.lower().endswith(".wav"):
            dur = _wav_duration(fpath)
            if dur is not None:
                return dur
        probe = _ffprobe_info(fpath)
        if probe:
            return probe[0]
        return None

    def _fmt_duration(secs):
        """格式化秒數為 H:MM:SS 或 M:SS，固定 7 字元右對齊"""
        if secs is None:
            return "--"
        secs = int(secs)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    page = 0
    while True:
        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(files))
        page_files = files[start:end]
        has_next = end < len(files)
        total = len(files)

        # 動態計算檔名欄寬度（取當頁最寬 + 2，最小 40）
        fname_col = max(max(_dw(os.path.basename(f)) for f, _ in page_files), 38) + 2

        print(f"\n\n{C_TITLE}{BOLD}▎ 選擇音訊檔{RESET}  {C_WHITE}（recordings/ 下共 {total} 個，顯示第 {start + 1}-{end} 個）{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        for i, (fpath, mtime) in enumerate(page_files):
            num = start + i + 1
            fname = os.path.basename(fpath)
            size_str = _human_size(os.path.getsize(fpath))
            dur_str = _fmt_duration(_audio_duration(fpath))
            date_str = _time.strftime("%m/%d %H:%M", _time.localtime(mtime))
            size_part = f"({size_str})"
            pad = ' ' * (fname_col - _dw(fname))
            info = f"{dur_str:>7s}  {size_part:>10s}  {date_str}"
            if num == 1:
                print(f"  {C_HIGHLIGHT}{BOLD}[{num:>2d}]{RESET} {C_WHITE}{fname}{RESET}{pad} {C_DIM}{info}{RESET}")
            else:
                print(f"  {C_DIM}[{num:>2d}]{RESET} {C_WHITE}{fname}{RESET}{pad} {C_DIM}{info}{RESET}")
        if has_next:
            next_num = end + 1
            remain = total - end
            print(f"  {C_DIM}[{next_num:>2d}]{RESET} {C_WHITE}... 顯示下 {min(PAGE_SIZE, remain)} 筆{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇檔案編號 [1]（多選用逗號分隔，如 1,3,5）：{RESET}", end=" ")

        try:
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if user_input:
            # 支援逗號分隔多選：1,3,5 或單選：3
            parts = [p.strip() for p in user_input.split(",") if p.strip()]
            indices = []
            do_page = False
            for p in parts:
                try:
                    choice = int(p)
                except ValueError:
                    continue
                # 翻頁：輸入的編號 == end+1 且有下一頁（僅單選時觸發）
                if has_next and choice == end + 1 and len(parts) == 1:
                    do_page = True
                    break
                idx = choice - 1
                if 0 <= idx < len(files) and idx not in indices:
                    indices.append(idx)
            if do_page:
                page += 1
                continue
            if not indices:
                indices = [0]
        else:
            indices = [0]

        chosen = [files[idx][0] for idx in indices]
        for fpath in chosen:
            print(f"  {C_OK}→ {os.path.basename(fpath)}{RESET}")
        print()
        return chosen


def _ask_input_source():
    """互動選單第一步：選擇輸入來源。
    回傳 ("realtime", None) 或 ("file", [filepath, ...])"""
    while True:
        print(f"\n\n{C_TITLE}{BOLD}▎ 輸入來源{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_HIGHLIGHT}{BOLD}[1] 即時音訊擷取{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        print(f"  {C_DIM}[2]{RESET} {C_WHITE}讀入音訊檔案{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1-2) [1]：{RESET}", end=" ")

        try:
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if user_input == "2":
            result = _select_audio_files()
            if result is None:
                print(f"  {C_HIGHLIGHT}recordings/ 目錄下沒有音訊檔{RESET}")
                continue  # 回到輸入來源選單
            print(f"  {C_OK}→ 讀入音訊檔案{RESET}")
            return ("file", result)

        # 預設或輸入 1
        print(f"  {C_OK}→ 即時音訊擷取{RESET}\n")
        return ("realtime", None)


def _ask_record():
    """互動選單：詢問錄製音訊方式（混合/僅播放/不錄）。
    回傳 (record: bool, rec_device: int or None)"""
    import sounddevice as sd

    # 偵測錄音裝置
    devices = sd.query_devices()
    aggregate_id = None
    aggregate_name = None
    blackhole_id = None
    blackhole_name = None

    # 1) 聚集裝置
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            name = dev["name"]
            if "聚集" in name or "aggregate" in name.lower():
                aggregate_id, aggregate_name = i, name
                break
    # 2) input channels >= 3 的虛擬裝置（使用者可能改過聚集裝置名稱）
    if aggregate_id is None:
        for i, dev in enumerate(devices):
            if (dev["max_input_channels"] >= 3
                    and "blackhole" not in dev["name"].lower()):
                aggregate_id, aggregate_name = i, dev["name"]
                break
    # 3) BlackHole
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
            blackhole_id, blackhole_name = i, dev["name"]
            break

    has_aggregate = aggregate_id is not None
    has_blackhole = blackhole_id is not None

    print(f"\n\n{C_TITLE}{BOLD}▎ 錄製音訊{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}同時錄製音訊為 WAV 檔（儲存於 recordings/）{RESET}")
    print(f"  {C_DIM}* 即時辨識僅處理播放聲音，無法即時辨識我方說話的聲音{RESET}")
    print()

    # 選項文字固定寬度對齊（「混合錄製（輸出+輸入）」顯示寬 20 全形字元）
    _rec_label1 = "混合錄製（輸出+輸入）"  # 顯示寬 20
    _rec_label2 = "僅錄播放聲音         "  # 補 9 空格對齊到顯示寬 21
    if has_aggregate and has_blackhole:
        # 三選項都可選，預設 [1]
        print(f"  {C_HIGHLIGHT}{BOLD}[1] {_rec_label1}{RESET} {C_DIM}{aggregate_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        print(f"  {C_DIM}[2]{RESET} {C_WHITE}{_rec_label2}{RESET} {C_DIM}{blackhole_name}{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1-3) [1]：{RESET}", end=" ")
        default_choice = "1"
    elif has_blackhole:
        # 沒有聚集裝置，[1] 不可選，預設 [2]
        print(f"  {C_DIM}[1] {_rec_label1}  未偵測到聚集裝置{RESET}")
        print(f"  {C_HIGHLIGHT}{BOLD}[2] {_rec_label2}{RESET} {C_DIM}{blackhole_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (2-3) [2]：{RESET}", end=" ")
        default_choice = "2"
    elif has_aggregate:
        # 有聚集但沒 BlackHole（少見），[2] 不可選，預設 [1]
        print(f"  {C_HIGHLIGHT}{BOLD}[1] {_rec_label1}{RESET} {C_DIM}{aggregate_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        print(f"  {C_DIM}[2] {_rec_label2}  未偵測到 BlackHole{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1,3) [1]：{RESET}", end=" ")
        default_choice = "1"
    else:
        # 都找不到 → fallback 手動選單
        print(f"  {C_HIGHLIGHT}[提醒] 未偵測到聚集裝置或 BlackHole，請手動選擇錄音裝置{RESET}")
        input_devices = []
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                input_devices.append((i, dev["name"], dev["max_input_channels"],
                                      int(dev["default_samplerate"])))
        if not input_devices:
            print(f"  {C_DIM}無可用輸入裝置，跳過錄音{RESET}\n")
            return False, None
        default_id = input_devices[0][0]

        print(f"\n  {C_TITLE}{BOLD}錄音裝置{RESET}")
        for dev_id, dev_name, ch, sr in input_devices:
            info = f"{ch}ch {sr}Hz"
            if dev_id == default_id:
                print(f"  {C_HIGHLIGHT}{BOLD}[{dev_id}] {dev_name}{RESET} {C_DIM}{info}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{dev_id}]{RESET} {C_WHITE}{dev_name}{RESET} {C_DIM}{info}{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}按 Enter 使用預設，或輸入裝置 ID：{RESET}", end=" ")

        try:
            dev_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if dev_input:
            try:
                selected_id = int(dev_input)
            except ValueError:
                selected_id = default_id
        else:
            selected_id = default_id

        selected_name = next((n for i, n, _, _ in input_devices if i == selected_id),
                             f"裝置 #{selected_id}")
        print(f"  {C_OK}→ [{selected_id}] {selected_name}{RESET}\n")
        return True, selected_id

    # 讀取使用者選擇
    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    choice = user_input if user_input else default_choice

    if choice == "1" and has_aggregate:
        print(f"  {C_OK}→ 混合錄製 [{aggregate_id}] {aggregate_name}{RESET}\n")
        return True, aggregate_id
    elif choice == "2" and has_blackhole:
        print(f"  {C_OK}→ 僅錄播放聲音 [{blackhole_id}] {blackhole_name}{RESET}\n")
        return True, blackhole_id
    elif choice == "3":
        print(f"  {C_OK}→ 不錄製{RESET}\n")
        return False, None
    else:
        # 無效輸入 → 使用預設
        if default_choice == "1":
            print(f"  {C_OK}→ 混合錄製 [{aggregate_id}] {aggregate_name}{RESET}\n")
            return True, aggregate_id
        else:
            print(f"  {C_OK}→ 僅錄播放聲音 [{blackhole_id}] {blackhole_name}{RESET}\n")
            return True, blackhole_id


def _ask_topic(record_only=False):
    """互動選單：詢問會議主題（可選）。
    回傳主題字串，若使用者跳過則回傳 None。"""
    if record_only:
        print(f"\n\n{C_TITLE}{BOLD}▎ 會議主題（選填，用做檔名參考）{RESET}")
    else:
        print(f"\n\n{C_TITLE}{BOLD}▎ 會議主題（選填，提升翻譯品質）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}輸入此次會議的主題或領域，例如：K8s 安全架構、ZFS 儲存管理{RESET}")
    print(f"  {C_DIM}若無特定主題要填寫，可直接按 Enter 跳過{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}會議主題：{RESET}", end=" ")

    try:
        # 用 buffer 直接讀 raw bytes 再解碼，避免 macOS 中文輸入法 UnicodeDecodeError
        if hasattr(sys.stdin, 'buffer'):
            sys.stdout.flush()
            raw = sys.stdin.buffer.readline()
            user_input = raw.decode('utf-8', errors='replace').strip()
        else:
            user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input:
        print(f"  {C_OK}→ 主題: {user_input}{RESET}\n")
        return user_input
    print(f"  {C_DIM}→ 跳過{RESET}\n")
    return None


def open_file_in_editor(file_path):
    """用系統預設程式開啟檔案"""
    try:
        subprocess.Popen(["open", file_path])
    except Exception:
        pass


class _SummaryStatusBar:
    """摘要模式的底部狀態列，類似轉錄時的風格"""
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, model="", task="", asr_location="", location=""):
        _loc = location or asr_location
        self._model = f"{model} [{_loc}]" if _loc else model
        self._task = task
        self._stop = threading.Event()
        self._thread = None
        self._tokens = 0
        self._t0 = 0
        self._first_token_time = 0
        self._active = False
        self._lock = threading.Lock()
        self._frozen = False
        self._frozen_time = ""
        self._frozen_stats = ""
        self._progress_text = ""  # 自訂進度文字（取代「等待模型回應」）
        self._last_rows = 0       # 追蹤上一次 terminal 高度，用於清除舊狀態列

    def start(self):
        self._stop.clear()
        self._tokens = 0
        self._first_token_time = 0
        self._t0 = time.monotonic()
        self._needs_resize = False
        # 設定 scroll region，保留最後一行給狀態列
        try:
            cols, rows = os.get_terminal_size()
            self._last_rows = rows
            sys.stdout.write(f"\x1b[1;{rows - 1}r")       # scroll region（游標會跳到 1,1）
            sys.stdout.write(f"\x1b[{rows - 1};1H")        # 移到 scroll region 底部
            sys.stdout.write(f"\n")                         # 換行推開既有內容，游標留在空行
            sys.stdout.flush()
            self._active = True
        except Exception:
            self._active = False
        # 攔截 SIGWINCH
        self._old_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._on_sigwinch)
        self._thread = threading.Thread(target=self._draw_loop, daemon=True)
        self._thread.start()
        return self

    def _on_sigwinch(self, signum, frame):
        self._needs_resize = True

    def set_task(self, task, reset_timer=True):
        self._task = task
        self._tokens = 0
        self._first_token_time = 0
        self._progress_text = ""
        if reset_timer:
            self._t0 = time.monotonic()

    def set_progress(self, text):
        """設定自訂進度文字（顯示在 spinner 右邊）"""
        self._progress_text = text

    def freeze(self):
        """凍結狀態列：停止計時、顯示最終統計"""
        elapsed = time.monotonic() - self._t0
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        self._frozen_time = f"{h:02d}:{m:02d}:{s:02d}"
        if self._tokens > 0 and self._first_token_time:
            gen_elapsed = time.monotonic() - self._first_token_time
            tps = self._tokens / gen_elapsed if gen_elapsed > 0.1 else 0
            self._frozen_stats = f"{self._tokens} tokens | {tps:.1f} t/s"
        else:
            self._frozen_stats = ""
        self._frozen = True

    def update_tokens(self, count):
        self._tokens = count
        if count > 0 and not self._first_token_time:
            self._first_token_time = time.monotonic()

    def _draw_loop(self):
        i = 0
        while not self._stop.is_set():
            if self._needs_resize:
                self._needs_resize = False
                try:
                    cols, rows = os.get_terminal_size()
                    old_rows = self._last_rows
                    self._last_rows = rows
                    with self._lock:
                        # 清除舊狀態列殘留（terminal 變大時舊 bar 會留在畫面中間）
                        clean = ""
                        if old_rows and old_rows != rows and old_rows <= rows:
                            clean = f"\x1b7\x1b[{old_rows};1H\x1b[2K\x1b8"
                        sys.stdout.write(f"{clean}\x1b7\x1b[1;{rows - 1}r\x1b8")
                        sys.stdout.flush()
                except Exception:
                    pass
            self._draw_bar(i)
            i += 1
            self._stop.wait(0.15)

    def _draw_bar(self, frame_idx=0):
        if not self._active:
            return
        try:
            cols, rows = os.get_terminal_size()

            if self._frozen:
                time_str = self._frozen_time
                stats_part = f" | {self._frozen_stats}" if self._frozen_stats else ""
                status = f" {time_str} | {self._model} | {self._task}{stats_part} "
            else:
                elapsed = time.monotonic() - self._t0
                h, rem = divmod(int(elapsed), 3600)
                m, s = divmod(rem, 60)
                time_str = f"{h:02d}:{m:02d}:{s:02d}"

                frame = self.FRAMES[frame_idx % len(self.FRAMES)]

                if self._tokens > 0:
                    gen_elapsed = time.monotonic() - self._first_token_time
                    tps = self._tokens / gen_elapsed if gen_elapsed > 0.1 else 0
                    progress = f"{frame} {self._tokens} tokens | {tps:.1f} t/s"
                elif self._progress_text:
                    progress = f"{frame} {self._progress_text}"
                else:
                    progress = f"{frame} 等待模型回應..."

                status = f" {time_str} | {self._model} | {self._task} | {progress} "
            # 計算顯示寬度（CJK + 全形標點都算 2 格）
            dw = 0
            for c in status:
                if ('\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f'
                        or '\uff00' <= c <= '\uffef' or '\u3400' <= c <= '\u4dbf'):
                    dw += 2
                else:
                    dw += 1
            padding = " " * max(0, cols - dw)

            # 不碰 scroll region，純粹 save cursor → 畫 bar → restore cursor
            buf = (f"\x1b7\x1b[{rows};1H\x1b[2K"
                   f"\x1b[48;2;60;60;60m\x1b[38;2;200;200;200m{status}{padding}\x1b[0m"
                   f"\x1b8")
            with self._lock:
                sys.stdout.write(buf)
                sys.stdout.flush()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
        # 恢復原本的 SIGWINCH handler
        try:
            signal.signal(signal.SIGWINCH, self._old_sigwinch or signal.SIG_DFL)
        except Exception:
            pass
        if self._active:
            try:
                sys.stdout.write("\x1b[r")  # 重設 scroll region
                cols, rows = os.get_terminal_size()
                sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")  # 清除狀態列
                sys.stdout.flush()
            except Exception:
                pass
            self._active = False


def call_ollama_raw(prompt, model, host, port, timeout=300, spinner=None, live_output=False,
                    server_type="ollama"):
    """直接呼叫 LLM API 取得回應（串流模式，可更新 spinner 進度或即時輸出）"""
    return _llm_generate(
        prompt, model, host, port, server_type,
        stream=True, timeout=timeout,
        spinner=spinner, live_output=live_output,
    )


def query_ollama_num_ctx(model, host, port, server_type="ollama"):
    """查詢模型的 context window 大小（token 數），查不到回傳 None
    Ollama 用 /api/show，OpenAI 相容無標準對應（直接回傳 None）"""
    if server_type == "openai":
        return None  # OpenAI 相容 API 無標準對應，用 fallback
    try:
        url = f"http://{host}:{port}/api/show"
        payload = json.dumps({"name": model}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # 優先從 model_info 裡找 context_length
        for key, val in data.get("model_info", {}).items():
            if "context_length" in key and isinstance(val, (int, float)):
                return int(val)
        # 其次從 parameters 字串裡找 num_ctx
        params = data.get("parameters", "")
        for line in params.split("\n"):
            if "num_ctx" in line:
                parts = line.split()
                for p in parts:
                    if p.isdigit():
                        return int(p)
    except Exception:
        pass
    return None


def _calc_chunk_max_chars(num_ctx):
    """根據模型 context window 計算每段逐字稿的最大字數
    中文約 1 字 ≈ 1.5 tokens，留空間給 prompt 模板和模型回應。
    校正逐字稿的輸出長度接近輸入長度，因此輸入只能佔 context 的 1/3，
    剩餘 2/3 留給 prompt + 回應（回應需要完整輸出校正後的逐字稿）。"""
    if not num_ctx:
        return SUMMARY_CHUNK_FALLBACK_CHARS
    # 輸入佔 1/3 context，其餘留給 prompt 模板 + 完整回應
    available_tokens = num_ctx // 3 - SUMMARY_PROMPT_OVERHEAD_TOKENS
    if available_tokens < 2000:
        return SUMMARY_CHUNK_FALLBACK_CHARS
    # 中文 1 字 ≈ 1.5 token，混合中英文取 1.5 倍換算
    max_chars = int(available_tokens / 1.5)
    return max(max_chars, SUMMARY_CHUNK_FALLBACK_CHARS)


def _split_transcript_chunks(text, max_chars):
    """將逐字稿依段落切成不超過 max_chars 的分段"""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _is_en_hallucination(text):
    """檢查英文文字是否為 Whisper 幻覺（靜音時產生的假輸出）"""
    stripped_alpha = re.sub(r"[^a-zA-Z]", "", text)
    if len(stripped_alpha) < 3:
        return True
    line_lower = text.lower().strip(".")
    return line_lower in (
        "you", "the", "bye", "so", "okay",
        "thank you", "thanks for watching",
        "thanks for listening", "see you next time",
        "subscribe", "like and subscribe",
    )


def _is_zh_hallucination(text):
    """檢查中文文字是否為 Whisper 幻覺（YouTube 訓練資料殘留）"""
    # 簡體+繁體關鍵字都要檢查（faster-whisper 可能輸出簡體）
    return any(kw in text for kw in (
        "訂閱", "订阅", "點贊", "点赞", "點讚", "轉發", "转发", "打賞", "打赏",
        "感謝觀看", "感谢观看", "謝謝大家", "谢谢大家", "謝謝收看", "谢谢收看",
        "字幕由", "字幕提供",
        "獨播", "独播", "劇場", "剧场", "YoYo", "Television Series",
        "歡迎訂閱", "欢迎订阅", "明鏡", "明镜", "新聞頻道", "新闻频道",
    ))


def _ffprobe_info(input_path):
    """用 ffprobe 取得音訊檔資訊，回傳 (duration_secs, format_name, sample_rate, channels) 或 None"""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", input_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        fmt_name = info.get("format", {}).get("format_long_name", "")
        # 從第一個 audio stream 取資訊
        sr, ch = 0, 0
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                sr = int(stream.get("sample_rate", 0))
                ch = int(stream.get("channels", 0))
                break
        return duration, fmt_name, sr, ch
    except Exception:
        return None


def _convert_to_wav(input_path):
    """將音訊檔轉換為 16kHz mono WAV（如果已是 wav 則直接回傳）"""
    if input_path.lower().endswith(".wav"):
        return input_path, False  # (path, is_temp)
    # 建立暫存 wav 檔名
    os.makedirs(RECORDING_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_path))[0]
    tmp_wav = os.path.join(RECORDING_DIR, f"tmp_{base}_{int(time.time())}.wav")

    # 取得來源檔資訊
    probe = _ffprobe_info(input_path)
    total_duration = probe[0] if probe else 0

    # 顯示來源檔案資訊
    file_size = os.path.getsize(input_path)
    size_str = (f"{file_size / 1048576:.1f} MB" if file_size >= 1048576
                else f"{file_size / 1024:.0f} KB")
    ext = os.path.splitext(input_path)[1].lstrip(".").upper()
    if probe and total_duration > 0:
        dur_m, dur_s = divmod(int(total_duration), 60)
        dur_h, dur_m = divmod(dur_m, 60)
        dur_str = f"{dur_h}:{dur_m:02d}:{dur_s:02d}" if dur_h else f"{dur_m}:{dur_s:02d}"
        sr_str = f"{probe[2]//1000}kHz" if probe[2] else ""
        ch_str = "mono" if probe[3] == 1 else "stereo" if probe[3] == 2 else f"{probe[3]}ch"
        info_parts = [s for s in [ext, size_str, dur_str, sr_str, ch_str] if s]
        print(f"  {C_WHITE}來源        {RESET}{C_DIM}{' | '.join(info_parts)}{RESET}")
    else:
        print(f"  {C_WHITE}來源        {RESET}{C_DIM}{ext} | {size_str}{RESET}")

    try:
        cmd = [
            "ffmpeg", "-i", input_path, "-ar", "16000", "-ac", "1",
            "-y", "-progress", "pipe:1", "-loglevel", "error",
            tmp_wav,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        t0 = time.monotonic()
        bar_width = 30

        # 讀取 ffmpeg -progress 輸出（key=value 格式）
        current_us = 0
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        current_us = int(line.split("=", 1)[1])
                    except (ValueError, IndexError):
                        pass
                elif line == "progress=continue" or line == "progress=end":
                    if total_duration > 0 and current_us > 0:
                        current_s = current_us / 1_000_000
                        pct = min(current_s / total_duration, 1.0)
                        filled = int(bar_width * pct)
                        bar = f"{'█' * filled}{'░' * (bar_width - filled)}"
                        elapsed = time.monotonic() - t0
                        # ETA
                        if pct > 0.01:
                            eta = elapsed / pct * (1 - pct)
                            eta_str = f"ETA {eta:.0f}s"
                        else:
                            eta_str = ""
                        sys.stdout.write(
                            f"\r  {C_WHITE}轉檔中 {bar} {pct:5.1%}{RESET}  "
                            f"{C_DIM}({elapsed:.0f}s {eta_str}){RESET}  "
                        )
                        sys.stdout.flush()
                    if line == "progress=end":
                        break
        except Exception:
            pass

        proc.wait(timeout=300)
        elapsed = time.monotonic() - t0

        # 清除進度列
        if total_duration > 0:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()

        if proc.returncode != 0:
            stderr_out = proc.stderr.read()
            print(f"  {C_HIGHLIGHT}[錯誤] ffmpeg 轉檔失敗: {stderr_out.strip()[-200:]}{RESET}",
                  file=sys.stderr)
            return None, False

        # 轉檔後的檔案大小
        out_size = os.path.getsize(tmp_wav)
        out_str = (f"{out_size / 1048576:.1f} MB" if out_size >= 1048576
                   else f"{out_size / 1024:.0f} KB")

        return tmp_wav, True  # (path, is_temp, elapsed, out_size_str)

    except FileNotFoundError:
        print(f"  {C_HIGHLIGHT}[錯誤] 找不到 ffmpeg，請先安裝: brew install ffmpeg{RESET}",
              file=sys.stderr)
        return None, False
    except Exception as e:
        print(f"  {C_HIGHLIGHT}[錯誤] 轉檔失敗: {e}{RESET}", file=sys.stderr)
        return None, False


def _format_timestamp(seconds):
    """將秒數格式化為 MM:SS 或 HH:MM:SS"""
    seconds = int(seconds)
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    else:
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"


def _diarize_segments(wav_path, segments, num_speakers=None, sbar=None):
    """用 resemblyzer + spectralcluster 辨識講者。

    segments: list of dict，每個含 start, end, text
    回傳: list of int（講者編號 0-based），失敗回傳 None
    """
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
            from resemblyzer import VoiceEncoder, preprocess_wav
        from spectralcluster import SpectralClusterer
        from spectralcluster import refinement
    except ImportError as e:
        print(f"  {C_HIGHLIGHT}[錯誤] 講者辨識需要額外套件: {e}{RESET}", file=sys.stderr)
        print(f"  {C_DIM}pip install resemblyzer spectralcluster{RESET}", file=sys.stderr)
        return None

    if not segments:
        return None

    if sbar:
        sbar.set_task("載入聲紋模型")

    # 載入音訊
    wav = preprocess_wav(wav_path)
    sr = 16000  # resemblyzer preprocess_wav 輸出 16kHz

    # 初始化聲紋編碼器（首次自動下載 ~17MB 模型）
    encoder = VoiceEncoder("cpu")

    if sbar:
        sbar.set_task(f"提取聲紋（{len(segments)} 段）")

    import numpy as np
    from collections import Counter

    # ── 合併連續短段落（< 0.8s）再提取 embedding ──
    # 避免碎片化：連續短段落合併音訊後一起取 embedding
    merge_groups = []  # list of list of indices
    i = 0
    while i < len(segments):
        duration = segments[i]["end"] - segments[i]["start"]
        if duration < 0.8:
            group = [i]
            j = i + 1
            while j < len(segments) and (segments[j]["end"] - segments[j]["start"]) < 0.8:
                group.append(j)
                j += 1
            if len(group) > 1:
                merge_groups.append(group)
                i = j
                continue
        i += 1
    merged_set = set()
    merged_emb_map = {}  # index → embedding (共享)
    for group in merge_groups:
        # 合併音訊
        combined_audio = np.concatenate([
            wav[int(segments[idx]["start"] * sr):int(segments[idx]["end"] * sr)]
            for idx in group
        ])
        if len(combined_audio) >= int(0.3 * sr):
            try:
                emb = encoder.embed_utterance(combined_audio)
                for idx in group:
                    merged_emb_map[idx] = emb
                    merged_set.add(idx)
            except Exception:
                pass

    # 逐段提取聲紋
    embeddings = []
    valid_indices = []  # 有成功提取 embedding 的段落索引

    for i, seg in enumerate(segments):
        # 已在合併組中處理過的段落
        if i in merged_emb_map:
            embeddings.append(merged_emb_map[i])
            valid_indices.append(i)
            continue

        start_sample = int(seg["start"] * sr)
        end_sample = int(seg["end"] * sr)

        # 段落太短（< 0.5s）：嘗試向前後擴展
        duration = seg["end"] - seg["start"]
        if duration < 0.5:
            mid = (seg["start"] + seg["end"]) / 2
            start_sample = max(0, int((mid - 0.25) * sr))
            end_sample = min(len(wav), int((mid + 0.25) * sr))

        audio_slice = wav[start_sample:end_sample]

        # 仍然太短則跳過
        if len(audio_slice) < int(0.3 * sr):
            embeddings.append(None)
            continue

        try:
            # 滑動窗口 embedding：長段落取多個 partial 後用中位數，更穩定
            if duration >= 1.6:
                emb, partials, _ = encoder.embed_utterance(
                    audio_slice, return_partials=True, rate=1.6, min_coverage=0.75
                )
                emb = np.median(partials, axis=0)
                emb = emb / np.linalg.norm(emb)  # L2 normalize
            else:
                emb = encoder.embed_utterance(audio_slice)
            embeddings.append(emb)
            valid_indices.append(i)
        except Exception:
            embeddings.append(None)

    if not valid_indices:
        print(f"  {C_HIGHLIGHT}[警告] 無法提取任何有效聲紋，跳過講者辨識{RESET}")
        return None

    if sbar:
        sbar.set_task("分群辨識講者")

    # 組合有效 embedding 矩陣
    valid_embeddings = np.array([embeddings[i] for i in valid_indices])

    # SpectralClusterer 分群（啟用 refinement 提升精準度）
    min_clusters = 2 if num_speakers is None else num_speakers
    max_clusters = 8 if num_speakers is None else num_speakers

    refinement_opts = refinement.RefinementOptions(
        gaussian_blur_sigma=1,
        p_percentile=0.95,
        thresholding_soft_multiplier=0.01,
        thresholding_type=refinement.ThresholdType.RowMax,
        symmetrize_type=refinement.SymmetrizeType.Max,
    )

    try:
        clusterer = SpectralClusterer(
            min_clusters=min_clusters,
            max_clusters=max_clusters,
            refinement_options=refinement_opts,
        )
        cluster_labels = clusterer.predict(valid_embeddings)
    except Exception as e:
        print(f"  {C_HIGHLIGHT}[警告] 分群失敗: {e}，所有段落標記為 Speaker 1{RESET}")
        return [0] * len(segments)

    # ── 餘弦相似度二次校正 ──
    # 計算群中心，若某段落與被指派群差距明顯（> 0.1），改指派到最近群
    unique_labels = sorted(set(cluster_labels))
    if len(unique_labels) > 1:
        centroids = {}
        for label in unique_labels:
            mask = [i for i, l in enumerate(cluster_labels) if l == label]
            centroids[label] = np.mean(valid_embeddings[mask], axis=0)
        reassigned = 0
        for idx in range(len(cluster_labels)):
            emb = valid_embeddings[idx]
            assigned = cluster_labels[idx]
            assigned_sim = float(np.dot(emb, centroids[assigned]))
            best_label, best_sim = assigned, assigned_sim
            for label, centroid in centroids.items():
                sim = float(np.dot(emb, centroid))
                if sim > best_sim:
                    best_label, best_sim = label, sim
            if best_label != assigned and (best_sim - assigned_sim) > 0.1:
                cluster_labels[idx] = best_label
                reassigned += 1
        if reassigned > 0 and sbar:
            sbar.set_progress(f"餘弦校正 {reassigned} 段")

    # 將分群結果映射回所有段落（跳過的段落繼承相鄰講者）
    speaker_labels = [None] * len(segments)
    for idx, valid_idx in enumerate(valid_indices):
        speaker_labels[valid_idx] = int(cluster_labels[idx])

    # 填補跳過的段落：繼承最近的有效講者
    last_valid = 0
    for i in range(len(speaker_labels)):
        if speaker_labels[i] is not None:
            last_valid = speaker_labels[i]
        else:
            speaker_labels[i] = last_valid

    # 多數決平滑（窗口 5）：比孤立段落修正更穩定
    changed = 0
    smoothed = list(speaker_labels)
    for i in range(len(smoothed)):
        start = max(0, i - 2)
        end = min(len(smoothed), i + 3)
        window = speaker_labels[start:end]
        majority = Counter(window).most_common(1)[0][0]
        if speaker_labels[i] != majority:
            smoothed[i] = majority
            changed += 1
    speaker_labels = smoothed
    if changed > 0 and sbar:
        sbar.set_progress(f"平滑修正 {changed} 段")

    # 按首次出現順序重新編號 0, 1, 2...
    seen = {}
    renumber_map = {}
    counter = 0
    for label in speaker_labels:
        if label not in seen:
            seen[label] = True
            renumber_map[label] = counter
            counter += 1
    speaker_labels = [renumber_map[l] for l in speaker_labels]

    n_speakers = len(set(speaker_labels))
    if sbar:
        sbar.set_task(f"辨識完成（{n_speakers} 位講者）")

    return speaker_labels


def _srt_timestamp(seconds):
    """秒數 → SRT 時間戳 HH:MM:SS,mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segments_to_srt(segments_data, srt_path):
    """將 segments_data 轉為 SRT 字幕檔。翻譯模式自動雙語。"""
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments_data, 1):
            f.write(f"{i}\n")
            f.write(f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}\n")
            for line in seg["lines"]:
                f.write(f"{line['text']}\n")
            f.write("\n")


def process_audio_file(input_path, mode, translator, model_size="large-v3-turbo",
                       diarize=False, num_speakers=None, remote_whisper_cfg=None):
    """處理音訊檔：ffmpeg 轉檔 → faster-whisper 辨識 → 翻譯 → 存檔，回傳 (log_path, html_path, session_dir)"""
    from datetime import datetime
    import shutil

    # 1. 驗證檔案存在
    if not os.path.isfile(input_path):
        print(f"  {C_HIGHLIGHT}[錯誤] 檔案不存在: {input_path}{RESET}", file=sys.stderr)
        return None, None, None

    basename = os.path.splitext(os.path.basename(input_path))[0]
    print(f"\n\n{C_TITLE}{BOLD}▎ 處理: {os.path.basename(input_path)}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")

    # 整體計時
    t_total_start = time.monotonic()

    # 2. 轉檔
    t_stage = time.monotonic()
    wav_path, is_temp = _convert_to_wav(input_path)
    if wav_path is None:
        return None, None, None
    t_convert_elapsed = time.monotonic() - t_stage
    if is_temp:
        out_size = os.path.getsize(wav_path)
        out_str = (f"{out_size / 1048576:.1f} MB" if out_size >= 1048576
                   else f"{out_size / 1024:.0f} KB")
        print(f"  {C_OK}轉檔        {RESET}{C_DIM}→ 16kHz mono WAV ({out_str})  [{t_convert_elapsed:.1f}s]{RESET}")
    else:
        print(f"  {C_OK}轉檔        {RESET}{C_DIM}已是 WAV 格式{RESET}")

    lang = "zh" if mode in ("zh", "zh2en") else "en"
    need_translate = mode in ("en2zh", "zh2en")

    # Log 檔名（每次處理建子目錄）
    log_prefixes = {"en2zh": "英翻中_時間逐字稿", "zh2en": "中翻英_時間逐字稿",
                    "en": "英文_時間逐字稿", "zh": "中文_時間逐字稿"}
    log_prefix = log_prefixes.get(mode, "時間逐字稿")
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(LOG_DIR, f"{basename}_{ts_str}")
    os.makedirs(session_dir, exist_ok=True)
    log_filename = f"{log_prefix}_{basename}_{ts_str}.txt"
    log_path = os.path.join(session_dir, log_filename)

    # 複製原始音訊到子目錄（保留原始格式）
    audio_copy = os.path.join(session_dir, os.path.basename(input_path))
    if not os.path.exists(audio_copy):
        shutil.copy2(input_path, audio_copy)

    print(f"  {C_WHITE}辨識語言    {lang}{RESET}")
    print(f"  {C_DIM}記錄檔      {os.path.relpath(session_dir)}/{RESET}")

    # 標籤
    if mode in ("en2zh", "en"):
        src_label, dst_label = "EN", "中"
        src_color, dst_color = C_EN, C_ZH
        hallucination_check = _is_en_hallucination
    else:
        src_label, dst_label = "中", "EN"
        src_color, dst_color = C_ZH, C_EN
        hallucination_check = _is_zh_hallucination

    # 取得音訊總時長（用於進度顯示）
    audio_duration = 0
    probe = _ffprobe_info(wav_path)
    if probe and probe[0] > 0:
        audio_duration = probe[0]

    # 3. 辨識：遠端 GPU 或本機
    t_stage = time.monotonic()
    used_remote = False
    raw_segments = None  # 遠端回傳的 segments list

    if remote_whisper_cfg is not None:
        rw_host = remote_whisper_cfg.get("host", "?")
        rw_port = remote_whisper_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
        print(f"  {C_WHITE}辨識位置    遠端 GPU（{rw_host}:{rw_port}）{RESET}")

        # 上傳前檢查遠端狀態（忙碌/磁碟空間）
        file_size = os.path.getsize(wav_path) if os.path.isfile(wav_path) else 0
        if not _check_remote_before_upload(remote_whisper_cfg, file_size):
            print(f"  {C_HIGHLIGHT}[降級] 改用本機 辨識{RESET}")
            remote_whisper_cfg = None

    if remote_whisper_cfg is not None:
        rw_host = remote_whisper_cfg.get("host", "?")
        rw_port = remote_whisper_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
        print(f"  {C_WHITE}上傳辨識中...{RESET}\n")

        sbar = _SummaryStatusBar(model=model_size, task="上傳音訊", asr_location="伺服器").start()

        def _upload_progress(text):
            sbar.set_progress(text)

        def _on_upload_done():
            sbar.set_task("遠端 GPU 辨識中", reset_timer=False)
            sbar.set_progress("等待伺服器回應...")

        try:
            r_segments, r_duration, r_proc_time, r_device = _remote_whisper_transcribe(
                remote_whisper_cfg, wav_path, model_size, lang,
                progress_callback=_upload_progress,
                on_upload_done=_on_upload_done,
            )
            raw_segments = r_segments
            used_remote = True
            sbar.set_task(f"遠端辨識完成（{len(r_segments)} 段，{r_proc_time:.1f}s，{r_device}）", reset_timer=False)
        except Exception as e:
            sbar.set_task("遠端辨識失敗", reset_timer=False)
            sbar.freeze()
            sbar.stop()
            print(f"  {C_HIGHLIGHT}[降級] 遠端辨識失敗: {e}{RESET}")
            print(f"  {C_HIGHLIGHT}[降級] 改用本機 辨識{RESET}")
            remote_whisper_cfg = None  # fallback

    if not used_remote:
        # 本機 faster-whisper
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            print(f"  {C_HIGHLIGHT}[錯誤] faster-whisper 未安裝，請執行: pip install faster-whisper{RESET}",
                  file=sys.stderr)
            return None, None, None

        print(f"  {C_WHITE}載入模型    {model_size}...{RESET}", end=" ", flush=True)
        model = WhisperModel(model_size, device="auto", compute_type="int8")
        print(f"{C_OK}✓{RESET}")
        print(f"  {C_WHITE}辨識中...{RESET}\n")

        sbar = _SummaryStatusBar(model=model_size, task="辨識中", asr_location="本機").start()
        if audio_duration > 0:
            sbar.set_progress("0%")

        segments_iter, info = model.transcribe(wav_path, language=lang, beam_size=5, vad_filter=True)

        # 將 generator 轉為 list of dict（與遠端格式統一）
        raw_segments = []
        for segment in segments_iter:
            if audio_duration > 0:
                pct = min(segment.end / audio_duration, 1.0)
                pos_m, pos_s = divmod(int(segment.end), 60)
                dur_m, dur_s = divmod(int(audio_duration), 60)
                sbar.set_progress(
                    f"{pct:.0%}  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
                )
            text = segment.text.strip()
            if text:
                raw_segments.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": text,
                })

    seg_count = 0
    try:
        # 收集所有有效段落（過濾幻覺和空白）
        valid_segments = []
        for seg_raw in raw_segments:
            text = seg_raw["text"].strip()
            if not text:
                continue
            text = re.sub(r"\(.*?\)", "", text).strip()
            text = re.sub(r"\[.*?\]", "", text).strip()
            if not text:
                continue
            if hallucination_check(text):
                continue
            if mode in ("zh", "zh2en"):
                text = S2TWP.convert(text)
            valid_segments.append({
                "start": seg_raw["start"],
                "end": seg_raw["end"],
                "text": text,
            })

        t_asr_elapsed = time.monotonic() - t_stage
        sbar.set_task(f"辨識完成（{len(valid_segments)} 段，{t_asr_elapsed:.1f}s）", reset_timer=False)
        sbar.set_progress("")

        # 講者辨識
        speaker_labels = None
        t_stage = time.monotonic()
        if diarize and valid_segments:
            # 優先嘗試遠端 GPU diarization
            if remote_whisper_cfg is not None:
                sbar.set_task("遠端講者辨識（上傳中）", reset_timer=False)
                def _diarize_progress(msg):
                    sbar.set_progress(msg)
                def _diarize_upload_done():
                    sbar.set_task("遠端講者辨識（GPU 分析中）", reset_timer=False)
                    sbar.set_progress("等待伺服器回應...")
                speaker_labels, d_proc_time = _remote_diarize(
                    remote_whisper_cfg, wav_path, valid_segments,
                    num_speakers=num_speakers,
                    progress_callback=_diarize_progress,
                    on_upload_done=_diarize_upload_done,
                )
                if speaker_labels is None:
                    # 遠端失敗，降級本機
                    sbar.set_task("遠端失敗，改用本機講者辨識", reset_timer=False)
                    speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                       num_speakers=num_speakers, sbar=sbar)
            else:
                speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                   num_speakers=num_speakers, sbar=sbar)
            t_diarize_elapsed = time.monotonic() - t_stage
            sbar.set_task(f"講者辨識完成（{t_diarize_elapsed:.1f}s）", reset_timer=False)

        # 輸出結果
        t_stage = time.monotonic()
        segments_data = []  # 收集結構化資料給 HTML
        with open(log_path, "w", encoding="utf-8") as log_f:
            for i, seg in enumerate(valid_segments):
                seg_count += 1
                text = seg["text"]
                ts_start = _format_timestamp(seg["start"])
                ts_end = _format_timestamp(seg["end"])
                ts_tag = f"[{ts_start}-{ts_end}]"

                sbar.set_task(f"輸出中（{seg_count}/{len(valid_segments)}）", reset_timer=False)

                # 講者標籤
                spk_tag_term = ""  # 終端機用（帶色彩）
                spk_tag_log = ""   # log 用（純文字）
                spk_num_val = None
                if speaker_labels is not None:
                    spk_num = speaker_labels[i] + 1  # 1-based 顯示
                    spk_num_val = spk_num
                    spk_color = SPEAKER_COLORS[speaker_labels[i] % len(SPEAKER_COLORS)]
                    spk_tag_term = f"{spk_color}[Speaker {spk_num}]{RESET} "
                    spk_tag_log = f"[Speaker {spk_num}] "

                seg_lines = []  # 本段的行資料

                if need_translate and translator:
                    print(f"{src_color}{ts_tag} {spk_tag_term}[{src_label}] {text}{RESET}", flush=True)

                    t0 = time.monotonic()
                    result = translator.translate(text)
                    elapsed = time.monotonic() - t0

                    if result:
                        if elapsed < 1.0:
                            speed_badge = C_BADGE_FAST
                        elif elapsed < 3.0:
                            speed_badge = C_BADGE_NORMAL
                        else:
                            speed_badge = C_BADGE_SLOW
                        _print_with_badge(f"{dst_color}{BOLD}{ts_tag} {spk_tag_term}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                        print(flush=True)

                        log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n")
                        log_f.write(f"{ts_tag} {spk_tag_log}[{dst_label}] {result}\n\n")
                        seg_lines.append({"label": src_label, "text": text})
                        seg_lines.append({"label": dst_label, "text": result})
                    else:
                        print(flush=True)
                        log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n\n")
                        seg_lines.append({"label": src_label, "text": text})
                else:
                    print(f"{src_color}{BOLD}{ts_tag} {spk_tag_term}[{src_label}] {text}{RESET}", flush=True)
                    print(flush=True)
                    log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n\n")
                    seg_lines.append({"label": src_label, "text": text})

                segments_data.append({
                    "start": seg["start"], "end": seg["end"],
                    "speaker": spk_num_val,
                    "lines": seg_lines,
                })

        t_translate_elapsed = time.monotonic() - t_stage
        if need_translate and translator:
            sbar.set_task(f"翻譯完成（{seg_count} 段，{t_translate_elapsed:.1f}s）", reset_timer=False)
        else:
            sbar.set_task(f"輸出完成（{seg_count} 段，{t_translate_elapsed:.1f}s）", reset_timer=False)
        sbar.freeze()

        # 清理暫存 wav
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)

        t_total_elapsed = time.monotonic() - t_total_start
        t_min, t_sec = divmod(int(t_total_elapsed), 60)
        total_str = f"{t_min}m{t_sec:02d}s" if t_min else f"{t_total_elapsed:.1f}s"

        diarize_info = ""
        if speaker_labels is not None:
            n_spk = len(set(speaker_labels))
            diarize_info = f" | {n_spk} 位講者"

        # 產生互動式 HTML 時間逐字稿
        transcript_html_path = os.path.splitext(log_path)[0] + ".html"
        _meta = {
            "asr_engine": "faster-whisper",
            "asr_model": model_size,
            "asr_location": "遠端 GPU" if used_remote else "本機",
            "input_file": os.path.basename(input_path),
        }
        if diarize:
            _meta["diarize"] = True
            _meta["diarize_engine"] = "resemblyzer + spectralcluster"
            if remote_whisper_cfg is not None:
                _meta["diarize_location"] = "遠端 GPU"
            else:
                _meta["diarize_location"] = "本機"
            if num_speakers:
                _meta["num_speakers"] = num_speakers
            # 從 segments_data 計算實際辨識出的講者數
            if segments_data:
                _detected = len(set(s.get("speaker") for s in segments_data if s.get("speaker") is not None))
                if _detected >= 2:
                    _meta["detected_speakers"] = _detected
        # 產出 SRT 字幕檔（在 HTML 之前，讓 HTML footer 能偵測到 SRT）
        _srt = None
        if segments_data:
            srt_path = os.path.splitext(log_path)[0] + ".srt"
            _segments_to_srt(segments_data, srt_path)
            _srt = srt_path

        if segments_data:
            _transcript_to_html(segments_data, transcript_html_path,
                                audio_copy, audio_duration, metadata=_meta)

        _html = transcript_html_path if segments_data else None

        print(f"\n{C_DIM}{'═' * 60}{RESET}")
        print(f"  {C_OK}{BOLD}處理完成{RESET} {C_DIM}（共 {seg_count} 段{diarize_info} | 耗時 {total_str}）{RESET}")
        print(f"  {C_WHITE}{log_path}{RESET}")
        if _html:
            print(f"  {C_WHITE}{_html}{RESET}")
        if _srt:
            print(f"  {C_WHITE}{_srt}{RESET}")
        if diarize and not num_speakers and speaker_labels is not None:
            n_spk = len(set(speaker_labels))
            print(f"  {C_DIM}講者辨識偵測到 {n_spk} 位，若不正確可用 --num-speakers N 指定重跑{RESET}")
        print(f"{C_DIM}{'═' * 60}{RESET}")

        sbar.stop()
        return log_path, _html, session_dir

    except KeyboardInterrupt:
        sbar.stop()
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)
        print(f"\n\n{C_DIM}已中止處理。{RESET}")
        if seg_count > 0:
            print(f"  {C_DIM}已處理的 {seg_count} 段已儲存: {log_path}{RESET}")
        raise  # 向上傳遞，讓外層迴圈停止
    except Exception as e:
        sbar.stop()
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)
        print(f"\n  {C_HIGHLIGHT}[錯誤] 處理失敗: {e}{RESET}", file=sys.stderr)
        return None, None, None


def _build_metadata_header(metadata):
    """根據 metadata dict 產生摘要檔開頭的處理資訊區塊（純文字）"""
    if not metadata:
        return ""
    lines = ["---", f"[ jt-live-whisper v{APP_VERSION} AI 摘要 ]"]

    # 辨識引擎
    asr_engine = metadata.get("asr_engine")
    if asr_engine:
        asr_model = metadata.get("asr_model", "")
        asr_loc = metadata.get("asr_location", "")
        parts = [asr_engine]
        if asr_model:
            parts[0] += f" ({asr_model})"
        if asr_loc:
            parts.append(asr_loc)
        lines.append(f"語音辨識：{'，'.join(parts) if len(parts) > 1 else parts[0]}")

    # 講者辨識
    if metadata.get("diarize"):
        d_engine = metadata.get("diarize_engine", "")
        d_loc = metadata.get("diarize_location", "")
        ns = metadata.get("num_speakers")
        ns_str = f"{ns} 人" if isinstance(ns, int) else str(ns) if ns else "自動偵測"
        d_parts = [p for p in [d_engine, d_loc, ns_str] if p]
        _det = metadata.get("detected_speakers")
        if _det and _det >= 2:
            d_parts.append(f"辨識出 {_det} 位")
        lines.append(f"講者辨識：{'，'.join(d_parts)}" if d_parts else "講者辨識：啟用")

    # 語言翻譯
    t_model = metadata.get("translate_model")
    if t_model:
        t_server = metadata.get("translate_server", "")
        lines.append(f"語言翻譯：{t_model}" + (f" ({t_server})" if t_server else ""))

    # 內容摘要
    s_model = metadata.get("summary_model")
    if s_model:
        s_server = metadata.get("summary_server", "")
        lines.append(f"內容摘要：{s_model}" + (f" ({s_server})" if s_server else ""))

    # 輸入來源
    inp = metadata.get("input_file")
    if inp:
        lines.append(f"來源音訊：{inp}")

    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _fix_speaker_labels_in_text(text):
    """校正逐字稿中 LLM 漏掉的 Speaker 標籤：無標籤的延續段落自動補上前一位講者標籤。"""
    lines = text.split("\n")
    result = []
    current_speaker = None
    in_transcript = False
    _spk_re = re.compile(r'^(Speaker\s*\d+)\s*[：:]\s*')

    for line in lines:
        stripped = line.strip()

        # 偵測進入校正逐字稿區段
        if stripped.startswith("## 校正逐字稿") or stripped.startswith("##校正逐字稿"):
            in_transcript = True
            current_speaker = None
            result.append(line)
            continue

        # 偵測離開（遇到下一個 ## 標題或 --- 分隔線）
        if in_transcript and (stripped.startswith("## ") or stripped.startswith("---")):
            in_transcript = False
            current_speaker = None
            result.append(line)
            continue

        if not in_transcript or not stripped:
            result.append(line)
            continue

        # 有 Speaker 標籤：更新 current_speaker
        m = _spk_re.match(stripped)
        if m:
            current_speaker = m.group(1)
            result.append(line)
        elif current_speaker:
            # 無標籤的延續段落：補上前一位講者
            result.append(f"{current_speaker}：{stripped}")
        else:
            result.append(line)

    return "\n".join(result)


def summarize_log_file(input_path, model, host, port, server_type="ollama",
                       topic=None, metadata=None, summary_mode="both",
                       audio_path=""):
    """讀取記錄檔 → 建 prompt → 呼叫 LLM → 簡繁轉換 → 寫摘要檔
    summary_mode: "both"（摘要+逐字稿）、"summary"（只摘要）、"transcript"（只逐字稿）
    回傳 (output_path, summary_text, html_path)"""
    with open(input_path, "r", encoding="utf-8") as f:
        transcript = f.read().strip()

    if not transcript:
        print(f"  {C_HIGHLIGHT}[跳過] 檔案內容為空: {input_path}{RESET}")
        return None, None, None

    basename = os.path.basename(input_path)
    dirpath = os.path.dirname(input_path) or "."

    # 依原始檔名決定摘要檔名（時間逐字稿優先匹配，再匹配舊版逐字稿）
    if basename.startswith("英翻中_時間逐字稿"):
        out_name = basename.replace("英翻中_時間逐字稿", "英翻中_摘要", 1)
    elif basename.startswith("中翻英_時間逐字稿"):
        out_name = basename.replace("中翻英_時間逐字稿", "中翻英_摘要", 1)
    elif basename.startswith("英文_時間逐字稿"):
        out_name = basename.replace("英文_時間逐字稿", "英文_摘要", 1)
    elif basename.startswith("中文_時間逐字稿"):
        out_name = basename.replace("中文_時間逐字稿", "中文_摘要", 1)
    elif basename.startswith("英翻中_逐字稿"):
        out_name = basename.replace("英翻中_逐字稿", "英翻中_摘要", 1)
    elif basename.startswith("中翻英_逐字稿"):
        out_name = basename.replace("中翻英_逐字稿", "中翻英_摘要", 1)
    elif basename.startswith("英文_逐字稿"):
        out_name = basename.replace("英文_逐字稿", "英文_摘要", 1)
    elif basename.startswith("中文_逐字稿"):
        out_name = basename.replace("中文_逐字稿", "中文_摘要", 1)
    else:
        out_name = f"摘要_{basename}"
    output_path = os.path.join(dirpath, out_name)

    # 查詢模型 context window，動態決定分段大小
    num_ctx = query_ollama_num_ctx(model, host, port, server_type=server_type)
    max_chars = _calc_chunk_max_chars(num_ctx)
    if num_ctx:
        print(f"  {C_DIM}模型 context window: {num_ctx:,} tokens → 每段上限約 {max_chars:,} 字{RESET}")
    else:
        print(f"  {C_DIM}無法偵測模型 context window，使用保底值: 每段 {max_chars:,} 字{RESET}")

    # 檢查是否需要分段摘要
    chunks = _split_transcript_chunks(transcript, max_chars)
    print()  # 空行，與下方摘要內容做視覺區隔

    _llm_loc = "本機" if host in ("localhost", "127.0.0.1", "::1") else "伺服器"
    sbar = _SummaryStatusBar(model=model, task="準備中", location=_llm_loc).start()

    if len(chunks) <= 1:
        # 單段：直接摘要
        prompt = _summary_prompt(transcript, topic=topic, summary_mode=summary_mode)
        sbar.set_task(f"生成摘要（單段，{len(transcript)} 字）")
        summary = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                  server_type=server_type)
    else:
        # 多段：逐段摘要 + 合併
        segment_summaries = []
        for i, chunk in enumerate(chunks):
            sbar.set_task(f"第 {i+1}/{len(chunks)} 段（{len(chunk)} 字）")
            prompt = _summary_prompt(chunk, topic=topic, summary_mode=summary_mode)
            seg = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                  server_type=server_type)
            seg = S2TWP.convert(seg)
            segment_summaries.append(seg)
            print(f"  {C_OK}第 {i+1}/{len(chunks)} 段完成{RESET}", flush=True)

        if summary_mode == "transcript":
            # 只要逐字稿：跳過 merge，直接串接各段校正逐字稿
            summary = ""
            for i, seg in enumerate(segment_summaries):
                marker = "## 校正逐字稿"
                idx = seg.find(marker)
                if idx >= 0:
                    transcript_part = seg[idx + len(marker):].strip()
                else:
                    transcript_part = seg.strip()
                if len(segment_summaries) > 1:
                    summary += f"--- 第 {i+1}/{len(segment_summaries)} 段 ---\n"
                summary += transcript_part + "\n\n"
        else:
            # 合併各段摘要
            sbar.set_task(f"合併 {len(chunks)} 段摘要")
            combined = "\n\n---\n\n".join(
                f"### 第 {i+1} 段\n{s}" for i, s in enumerate(segment_summaries)
            )
            merge_prompt = SUMMARY_MERGE_PROMPT_TEMPLATE.format(summaries=combined)
            if topic:
                merge_prompt = merge_prompt.replace(
                    "以下是各段摘要：",
                    f"- 本次會議主題：{topic}，請根據此主題的領域知識整理重點\n\n以下是各段摘要：",
                )
            merged_summary = call_ollama_raw(merge_prompt, model, host, port, spinner=sbar, live_output=True,
                                             server_type=server_type)

            if summary_mode == "summary":
                # 只要摘要：跳過逐字稿提取
                summary = merged_summary
            else:
                # both：合併摘要在前，各段校正逐字稿在後
                summary = merged_summary + "\n\n"
                for i, seg in enumerate(segment_summaries):
                    marker = "## 校正逐字稿"
                    idx = seg.find(marker)
                    if idx >= 0:
                        transcript_part = seg[idx:].strip()
                    else:
                        transcript_part = seg.strip()
                    summary += f"--- 第 {i+1}/{len(segment_summaries)} 段 ---\n{transcript_part}\n\n"

    sbar.stop()

    # 偵測 LLM 是否跳過重點摘要（summary_mode="both" 時應有兩個段落）
    if summary_mode == "both" and "## 重點摘要" not in summary:
        print(f"\n  {C_HIGHLIGHT}[偵測] LLM 回覆缺少重點摘要段落，自動補發摘要請求...{RESET}")
        # 使用 LLM 已校正的逐字稿（較短、較乾淨）做為重點摘要的輸入
        _retry_input = summary
        _marker = "## 校正逐字稿"
        _idx = _retry_input.find(_marker)
        if _idx >= 0:
            _retry_input = _retry_input[_idx + len(_marker):].strip()
        # 截斷到合理長度避免超出 context window
        if len(_retry_input) > max_chars:
            _retry_input = _retry_input[:max_chars]
        _retry_topic = f"（主題：{topic}）" if topic else ""
        _retry_prompt = f"""\
你是專業的會議記錄整理員。請根據以下校正後的逐字稿，列出 5-10 個重點摘要{_retry_topic}，每個重點用一句話概述。

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

規則：
- 全部使用台灣繁體中文
- 使用台灣用語（軟體、網路、記憶體、程式、伺服器等）
- 嚴禁加入原文沒有的內容

以下是逐字稿：
---
{_retry_input}
---"""
        sbar_retry = _SummaryStatusBar(model=model, task="補產重點摘要", location=_llm_loc).start()
        _retry_result = call_ollama_raw(_retry_prompt, model, host, port, spinner=sbar_retry,
                                        live_output=True, server_type=server_type)
        sbar_retry.stop()
        _retry_result = S2TWP.convert(_retry_result)
        # 將重點摘要放在前面，校正逐字稿放在後面
        summary = _retry_result.rstrip() + "\n\n" + summary.lstrip()
        print(f"  {C_OK}重點摘要已補上{RESET}")

    # 簡體→台灣繁體
    summary = S2TWP.convert(summary)

    # 校正逐字稿：LLM 漏掉的 Speaker 標籤，自動補上（與 HTML 邏輯對齊）
    summary = _fix_speaker_labels_in_text(summary)

    meta_header = _build_metadata_header(metadata)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(meta_header + summary + "\n")

    # 同步產生 HTML 摘要
    html_path = os.path.splitext(output_path)[0] + ".html"
    # 嘗試找到對應的時間逐字稿 HTML（同目錄、同基底名）
    _transcript_html = os.path.splitext(input_path)[0] + ".html"
    if not os.path.exists(_transcript_html):
        _transcript_html = ""
    _summary_to_html(summary, html_path, os.path.basename(input_path),
                     summary_txt_path=output_path, transcript_txt_path=input_path,
                     metadata=metadata, transcript_html_path=_transcript_html,
                     audio_path=audio_path)

    return output_path, summary, html_path


def _summary_to_html(summary_text, html_path, source_name="",
                     summary_txt_path="", transcript_txt_path="",
                     metadata=None, transcript_html_path="",
                     audio_path=""):
    """將摘要純文字轉為帶樣式的 HTML 檔"""
    import html as html_mod

    # 講者顏色（8 色循環，與終端機 SPEAKER_COLORS 對應的 HTML 色碼）
    _SPEAKER_HTML_COLORS = [
        "#ffcb6b",  # 金黃
        "#ff9a6c",  # 亮橘
        "#c3e88d",  # 亮綠
        "#d8a0ff",  # 亮紫
        "#ff7090",  # 亮粉紅
        "#50e8c0",  # 亮青綠
        "#a0d0ff",  # 亮天藍
        "#e0d080",  # 亮卡其
    ]

    lines = summary_text.split("\n")
    body_parts = []
    in_list = False  # 追蹤是否在 <ul> 內
    in_ol = False  # 追蹤是否在 <ol> 內
    in_nested_ol = False  # <ol> 巢狀在 <li> 內
    current_speaker = None  # 追蹤目前講者編號
    pending_br = False  # 延遲插入空行
    for line in lines:
        s = line.strip()
        if not s:
            if in_ol:
                body_parts.append("</ol>")
                in_ol = False
                if in_nested_ol:
                    body_parts.append("</li>")
                    in_nested_ol = False
            if in_list:
                body_parts.append("</ul>")
                in_list = False
            # 記錄有空行，但延遲插入（避免 speaker 段落前多餘空行）
            pending_br = True
            continue

        # 空行後的非 speaker 行才插入 <br>（speaker 自帶 margin-top，heading 自帶 margin-bottom）
        if pending_br:
            if not re.match(r'^\*{0,2}(Speaker \d+|講者 ?\d+)', s):
                # 前一個元素是 heading 時跳過（heading 已有 margin）
                last = body_parts[-1] if body_parts else ""
                if not (last.startswith("<h1>") or last.startswith("<h2>")):
                    body_parts.append("<br>")
            pending_br = False

        # 判斷項目類型
        is_list_item = s.startswith("- ")
        is_ol_item = bool(re.match(r'^\d+\.\s', s))

        # 離開有序列表
        if in_ol and not is_ol_item:
            body_parts.append("</ol>")
            in_ol = False
            if in_nested_ol:
                body_parts.append("</li>")
                in_nested_ol = False

        # 離開無序列表（有序項目不觸發，因為可能巢狀在 <li> 內）
        if in_list and not is_list_item and not is_ol_item:
            body_parts.append("</ul>")
            in_list = False

        escaped = html_mod.escape(s)
        # bold: **text**
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
        if s.startswith("## "):
            heading = html_mod.escape(s[3:])
            heading = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', heading)
            body_parts.append(f'<h2>{heading}</h2>')
            current_speaker = None
        elif s.startswith("# "):
            heading = html_mod.escape(s[2:])
            heading = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', heading)
            body_parts.append(f'<h1>{heading}</h1>')
            current_speaker = None
        elif s.startswith("---"):
            # 分段標記（如 "--- 第 1/2 段 ---"）→ 帶標籤的分隔線
            seg_m = re.match(r'^---\s*(.+?)\s*---$', s)
            if seg_m:
                seg_label = html_mod.escape(seg_m.group(1))
                body_parts.append(f'<hr><p style="color:#888;font-size:0.9em;text-align:center;margin:0.5em 0">{seg_label}</p>')
            else:
                body_parts.append("<hr>")
        elif is_list_item:
            if not in_list:
                body_parts.append("<ul>")
                in_list = True
            item = html_mod.escape(s[2:])
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            body_parts.append(f'<li>{item}</li>')
        elif is_ol_item:
            if not in_ol:
                if in_list and body_parts:
                    # 巢狀：將 <ol> 放入上一個 <li> 內（移除其 </li>）
                    for i in range(len(body_parts) - 1, -1, -1):
                        if body_parts[i].startswith('<li>') and body_parts[i].endswith('</li>'):
                            body_parts[i] = body_parts[i][:-5]  # 移除 </li>
                            break
                    in_nested_ol = True
                body_parts.append("<ol>")
                in_ol = True
            m_ol = re.match(r'^\d+\.\s*(.*)', s)
            ol_text = html_mod.escape(m_ol.group(1)) if m_ol else html_mod.escape(s)
            ol_text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ol_text)
            body_parts.append(f'<li>{ol_text}</li>')
        elif re.match(r'^\*{0,2}(Speaker \d+|講者 ?\d+)', s):
            m = re.match(r'^\*{0,2}(?:Speaker |講者 ?)(\d+)', s)
            if m:
                spk_num = int(m.group(1))
                current_speaker = spk_num
            color = _SPEAKER_HTML_COLORS[(current_speaker or 1) % len(_SPEAKER_HTML_COLORS)]
            body_parts.append(f'<p class="speaker" style="color:{color}">{escaped}</p>')
        else:
            if current_speaker is not None:
                # 同一講者的延續段落，自動補上 Speaker 標籤
                color = _SPEAKER_HTML_COLORS[current_speaker % len(_SPEAKER_HTML_COLORS)]
                spk_label = html_mod.escape(f"Speaker {current_speaker}：")
                body_parts.append(f'<p class="speaker" style="color:{color}"><strong>{spk_label}</strong>{escaped}</p>')
            else:
                body_parts.append(f"<p>{escaped}</p>")

    if in_ol:
        body_parts.append("</ol>")
        if in_nested_ol:
            body_parts.append("</li>")
    if in_list:
        body_parts.append("</ul>")

    body_html = "\n".join(body_parts)
    title = html_mod.escape(source_name) if source_name else "AI 摘要"

    # 底部檔案連結區
    footer_links = []
    html_basename = os.path.basename(html_path)
    footer_links.append(f'<a href="{html_mod.escape(html_basename)}">AI 摘要 (HTML)</a>')
    if summary_txt_path:
        txt_basename = html_mod.escape(os.path.basename(summary_txt_path))
        footer_links.append(f'<a href="{txt_basename}">AI 摘要 (TXT)</a>')
    if transcript_txt_path:
        log_basename = html_mod.escape(os.path.basename(transcript_txt_path))
        footer_links.append(f'<a href="{log_basename}">時間逐字稿 (TXT)</a>')
    if transcript_html_path:
        th_basename = html_mod.escape(os.path.basename(transcript_html_path))
        footer_links.append(f'<a href="{th_basename}">時間逐字稿 (HTML)</a>')
    if transcript_txt_path:
        _srt_bn = os.path.splitext(os.path.basename(transcript_txt_path))[0] + ".srt"
        _srt_full = os.path.join(os.path.dirname(html_path), _srt_bn)
        if os.path.isfile(_srt_full):
            footer_links.append(f'<a href="{html_mod.escape(_srt_bn)}">字幕檔 (SRT)</a>')
    if audio_path and os.path.isfile(audio_path):
        _html_dir = os.path.dirname(os.path.abspath(html_path))
        _audio_rel = os.path.relpath(os.path.abspath(audio_path), _html_dir)
        if _audio_rel.count("..") > 3:
            from urllib.parse import quote as _url_quote
            _audio_href = "file://" + _url_quote(os.path.abspath(audio_path))
        else:
            _audio_href = html_mod.escape(_audio_rel)
        _audio_ext = os.path.splitext(audio_path)[1].lstrip(".").upper() or "音訊"
        footer_links.append(f'<a href="{_audio_href}">音訊檔案 ({_audio_ext})</a>')
    footer_links = [l.replace("<a ", '<a target="_blank" ') for l in footer_links]
    footer_html = " | ".join(footer_links)

    # 建構 metadata 區塊
    meta_lines = [f'來源檔案：{title}']
    if metadata:
        asr_engine = metadata.get("asr_engine")
        if asr_engine:
            asr_model = metadata.get("asr_model", "")
            asr_loc = metadata.get("asr_location", "")
            asr_str = asr_engine + (f" ({asr_model})" if asr_model else "")
            if asr_loc:
                asr_str += f"，{asr_loc}"
            meta_lines.append(f'語音辨識：{asr_str}')
        if metadata.get("diarize"):
            d_engine = metadata.get("diarize_engine", "")
            d_loc = metadata.get("diarize_location", "")
            ns = metadata.get("num_speakers")
            ns_str = f"{ns} 人" if isinstance(ns, int) else str(ns) if ns else "自動偵測"
            d_parts = [p for p in [d_engine, d_loc, ns_str] if p]
            _det = metadata.get("detected_speakers")
            if _det and _det >= 2:
                d_parts.append(f"辨識出 {_det} 位")
            meta_lines.append(f'講者辨識：{"，".join(d_parts)}')
        t_model = metadata.get("translate_model")
        if t_model:
            t_server = metadata.get("translate_server", "")
            meta_lines.append(f'語言翻譯：{t_model}' + (f" ({t_server})" if t_server else ""))
        s_model = metadata.get("summary_model")
        if s_model:
            s_server = metadata.get("summary_server", "")
            meta_lines.append(f'內容摘要：{s_model}' + (f" ({s_server})" if s_server else ""))
        inp = metadata.get("input_file")
        if inp:
            meta_lines.append(f'來源音訊：{inp}')
    _badge = f'<span class="badge">jt-live-whisper v{APP_VERSION} AI 摘要</span>'
    meta_html = '<div class="meta">' + _badge + "<br>\n  " + "<br>\n  ".join(html_mod.escape(l) for l in meta_lines) + '</div>'

    page = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - AI 摘要</title>
<style>
  body {{ font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
         max-width: 800px; margin: 40px auto; padding: 0 20px;
         background: #1a1a2e; color: #e0e0e0; line-height: 1.8; }}
  h1 {{ color: #82aaff; border-bottom: 2px solid #82aaff; padding-bottom: 8px; }}
  h2 {{ color: #c792ea; margin-top: 1.5em; }}
  ul {{ margin: 0.5em 0; padding-left: 1.5em; }}
  ol {{ margin: 0.3em 0; padding-left: 1.5em; }}
  li {{ color: #a8d8a8; margin: 4px 0; }}
  ol > li {{ color: #c8c8c8; }}
  hr {{ border: none; border-top: 1px solid #444; margin: 1.5em 0; }}
  p {{ margin: 0.4em 0; }}
  .speaker {{ font-weight: bold; margin-top: 1em; }}
  .speaker strong {{ color: inherit; }}
  strong {{ color: #f78c6c; }}
  .meta {{ color: #888; font-size: 0.85em; margin-bottom: 2em; }}
  .badge {{ display: inline-block; background: #2d5a88; color: #c0d8f0; padding: 2px 10px;
            border-radius: 4px; font-size: 0.85em; margin-bottom: 0.5em; }}
  .footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #444;
             color: #888; font-size: 0.85em; }}
  .footer a {{ color: #82aaff; text-decoration: none; margin: 0 0.3em; }}
  .footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
{meta_html}
{body_html}
<div class="footer">
  相關檔案：{footer_html}
</div>
</body>
</html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    return html_path


def _transcript_to_html(segments_data, html_path, audio_path, audio_duration,
                         metadata=None, summary_html_path=None):
    """將時間逐字稿轉為互動式 HTML：波形時間軸 + 嵌入音訊 + 點擊跳轉"""
    import html as html_mod

    _SPEAKER_HTML_COLORS = [
        "#ffcb6b",  # 1: 金黃
        "#ff9a6c",  # 2: 亮橘
        "#c3e88d",  # 3: 亮綠
        "#d8a0ff",  # 4: 亮紫
        "#ff7090",  # 5: 亮粉紅
        "#50e8c0",  # 6: 亮青綠
        "#a0d0ff",  # 7: 亮天藍
        "#e0d080",  # 8: 亮卡其
    ]

    # 音訊路徑：相對路徑或 file:// URI
    html_dir = os.path.dirname(os.path.abspath(html_path))
    audio_abs = os.path.abspath(audio_path)
    audio_rel = os.path.relpath(audio_abs, html_dir)
    if audio_rel.count("..") > 3:
        from urllib.parse import quote
        audio_src = "file://" + quote(audio_abs)
    else:
        audio_src = html_mod.escape(audio_rel)

    # 建構波形資料：從音訊取 RMS 振幅，分 ~200 bin
    import json
    import struct
    import math

    NUM_BINS = 200
    rms_bins = [0.0] * NUM_BINS

    # 嘗試讀取 WAV 原始音訊計算 RMS
    _wav_for_rms = None
    if audio_path.lower().endswith(".wav") and os.path.isfile(audio_path):
        _wav_for_rms = audio_path
    else:
        # 非 WAV：嘗試找 process_audio_file 產生的暫存 WAV（已清理則跳過）
        _tmp_wav = os.path.splitext(audio_path)[0] + ".wav"
        if os.path.isfile(_tmp_wav):
            _wav_for_rms = _tmp_wav

    if _wav_for_rms and audio_duration > 0:
        try:
            import wave
            with wave.open(_wav_for_rms, "rb") as wf_audio:
                n_ch = wf_audio.getnchannels()
                sw = wf_audio.getsampwidth()
                sr = wf_audio.getframerate()
                n_frames = wf_audio.getnframes()
                frames_per_bin = max(n_frames // NUM_BINS, 1)

                fmt_map = {1: "b", 2: "<h", 4: "<i"}
                fmt_char = fmt_map.get(sw, "<h")
                max_val = float(2 ** (sw * 8 - 1))

                for b in range(NUM_BINS):
                    chunk = wf_audio.readframes(frames_per_bin)
                    if not chunk:
                        break
                    samples = struct.unpack(fmt_char * (len(chunk) // sw), chunk)
                    # mono mixdown
                    if n_ch > 1:
                        mono = []
                        for j in range(0, len(samples), n_ch):
                            mono.append(sum(samples[j:j+n_ch]) / n_ch)
                        samples = mono
                    if samples:
                        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / max_val
                        rms_bins[b] = rms
        except Exception:
            pass  # 讀取失敗就用預設值

    # 如果 WAV 讀取失敗，降級用 ffmpeg 快速取樣
    if max(rms_bins) == 0 and audio_duration > 0 and os.path.isfile(audio_path):
        try:
            bin_dur = audio_duration / NUM_BINS
            cmd = ["ffmpeg", "-i", audio_path, "-ac", "1", "-ar", "8000",
                   "-f", "s16le", "-v", "quiet", "-"]
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            if proc.returncode == 0 and proc.stdout:
                raw = proc.stdout
                samples_per_bin = max(len(raw) // 2 // NUM_BINS, 1)
                for b in range(NUM_BINS):
                    start_idx = b * samples_per_bin
                    end_idx = min(start_idx + samples_per_bin, len(raw) // 2)
                    if start_idx >= len(raw) // 2:
                        break
                    chunk_samples = struct.unpack(f"<{end_idx - start_idx}h",
                                                  raw[start_idx*2:end_idx*2])
                    if chunk_samples:
                        rms = math.sqrt(sum(s * s for s in chunk_samples) / len(chunk_samples)) / 32768.0
                        rms_bins[b] = rms
        except Exception:
            pass

    # 對應每個 bin 的 speaker
    bin_speakers = [None] * NUM_BINS
    if audio_duration > 0:
        bin_dur = audio_duration / NUM_BINS
        for seg in segments_data:
            spk = seg.get("speaker")
            if spk is None:
                continue
            b_start = int(seg["start"] / bin_dur)
            b_end = int(math.ceil(seg["end"] / bin_dur))
            for b in range(max(0, b_start), min(NUM_BINS, b_end)):
                bin_speakers[b] = spk

    waveform_data = []
    for b in range(NUM_BINS):
        waveform_data.append({
            "rms": round(rms_bins[b], 4),
            "spk": bin_speakers[b],
        })

    waveform_json = json.dumps(waveform_data, ensure_ascii=False)

    # 建構段落 HTML
    seg_parts = []
    for seg in segments_data:
        start_sec = int(seg["start"])
        ts_start = _format_timestamp(seg["start"])
        ts_end = _format_timestamp(seg["end"])
        ts_text = f"{ts_start}-{ts_end}"

        spk = seg.get("speaker")
        lines_html = []
        has_pair = len(seg["lines"]) >= 2
        for li, ln in enumerate(seg["lines"]):
            label = html_mod.escape(ln["label"])
            text = html_mod.escape(ln["text"])
            is_dst = has_pair and li >= 1
            line_cls = "line line-dst" if is_dst else "line"
            if spk is not None:
                color = _SPEAKER_HTML_COLORS[(spk - 1) % len(_SPEAKER_HTML_COLORS)]
                lines_html.append(
                    f'<div class="{line_cls}" style="color:{color}">'
                    f'<span class="spk">Speaker {spk}</span> '
                    f'[{label}] {text}</div>'
                )
            else:
                lines_html.append(
                    f'<div class="{line_cls}">[{label}] {text}</div>'
                )

        seg_parts.append(
            f'<div class="seg" id="t-{start_sec}">\n'
            f'  <a class="ts" data-t="{seg["start"]}" href="#">{ts_text}</a>\n'
            f'  {"".join(lines_html)}\n'
            f'</div>'
        )
    body_html = "\n".join(seg_parts)

    # metadata 區塊
    title = html_mod.escape(os.path.basename(audio_path))
    meta_lines = [f'來源音訊：{title}']
    if metadata:
        asr_engine = metadata.get("asr_engine")
        if asr_engine:
            asr_model = metadata.get("asr_model", "")
            asr_loc = metadata.get("asr_location", "")
            asr_str = asr_engine + (f" ({asr_model})" if asr_model else "")
            if asr_loc:
                asr_str += f"，{asr_loc}"
            meta_lines.append(f'語音辨識：{asr_str}')
        if metadata.get("diarize"):
            d_engine = metadata.get("diarize_engine", "")
            d_loc = metadata.get("diarize_location", "")
            ns = metadata.get("num_speakers")
            ns_str = f"{ns} 人" if isinstance(ns, int) else str(ns) if ns else "自動偵測"
            d_parts = [p for p in [d_engine, d_loc, ns_str] if p]
            _det = metadata.get("detected_speakers")
            if _det and _det >= 2:
                d_parts.append(f"辨識出 {_det} 位")
            meta_lines.append(f'講者辨識：{"，".join(d_parts)}')

    _badge = f'<span class="badge">jt-live-whisper v{APP_VERSION} 時間逐字稿</span>'
    meta_html = '<div class="meta">' + _badge + "<br>\n  " + "<br>\n  ".join(
        html_mod.escape(l) for l in meta_lines) + '</div>'

    # footer 連結（與摘要 HTML 對稱：四個檔案）
    footer_links = []
    html_basename = html_mod.escape(os.path.basename(html_path))
    footer_links.append(f'<a href="{html_basename}">時間逐字稿 (HTML)</a>')
    txt_path = os.path.splitext(html_path)[0] + ".txt"
    txt_basename = html_mod.escape(os.path.basename(txt_path))
    footer_links.append(f'<a href="{txt_basename}">時間逐字稿 (TXT)</a>')
    # 推算對應的摘要檔名（時間逐字稿 → 摘要）
    _txt_bn = os.path.basename(txt_path)
    _sum_bn = _txt_bn
    for _old, _new in [("英翻中_時間逐字稿", "英翻中_摘要"), ("中翻英_時間逐字稿", "中翻英_摘要"),
                        ("英文_時間逐字稿", "英文_摘要"), ("中文_時間逐字稿", "中文_摘要")]:
        if _txt_bn.startswith(_old):
            _sum_bn = _txt_bn.replace(_old, _new, 1)
            break
    if _sum_bn != _txt_bn:
        _sum_html_bn = html_mod.escape(os.path.splitext(_sum_bn)[0] + ".html")
        _sum_txt_bn = html_mod.escape(_sum_bn)
        footer_links.append(f'<a href="{_sum_html_bn}">AI 摘要 (HTML)</a>')
        footer_links.append(f'<a href="{_sum_txt_bn}">AI 摘要 (TXT)</a>')
    elif summary_html_path:
        sum_basename = html_mod.escape(os.path.basename(summary_html_path))
        footer_links.append(f'<a href="{sum_basename}">AI 摘要 (HTML)</a>')
    _srt_bn = os.path.splitext(os.path.basename(txt_path))[0] + ".srt"
    _srt_full = os.path.join(os.path.dirname(html_path), _srt_bn)
    if os.path.isfile(_srt_full):
        footer_links.append(f'<a href="{html_mod.escape(_srt_bn)}">字幕檔 (SRT)</a>')
    _audio_ext = os.path.splitext(audio_path)[1].lstrip(".").upper() or "音訊"
    footer_links.append(f'<a href="{audio_src}">音訊檔案 ({_audio_ext})</a>')
    footer_links = [l.replace("<a ", '<a target="_blank" ') for l in footer_links]
    footer_html = " | ".join(footer_links)

    dur_str = f"{audio_duration:.2f}" if audio_duration else "0"

    # 段落時間資料（給 JS timeupdate 用）
    seg_times = [{"start": round(s["start"], 2), "end": round(s["end"], 2)}
                 for s in segments_data]
    seg_times_json = json.dumps(seg_times, ensure_ascii=False)

    page = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - 時間逐字稿</title>
<style>
  body {{ font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
         max-width: 800px; margin: 0 auto; padding: 0 20px;
         background: #1a1a2e; color: #e0e0e0; line-height: 1.8; }}
  .meta {{ color: #888; font-size: 0.85em; margin-bottom: 1em; padding-top: 40px; }}
  .badge {{ display: inline-block; background: #2d5a88; color: #c0d8f0; padding: 2px 10px;
            border-radius: 4px; font-size: 0.85em; margin-bottom: 0.5em; }}
  .sticky-player {{ position: sticky; top: 0; z-index: 100; background: #1a1a2e;
                     padding: 8px 0 4px; border-bottom: 1px solid #2a2a4a; }}
  audio {{ width: 100%; margin: 0 0 6px; }}
  .waveform {{ position: relative; width: 100%; height: 50px; background: #12122a;
               border-radius: 6px; cursor: pointer; overflow: hidden; }}
  .waveform .bar {{ position: absolute; bottom: 0; background: #3a5a8a; border-radius: 2px 2px 0 0;
                    min-width: 2px; transition: background 0.15s; }}
  .waveform .bar:hover {{ background: #82aaff; }}
  .waveform .tooltip {{ position: absolute; top: -28px; background: #222; color: #ccc;
                         padding: 2px 8px; border-radius: 4px; font-size: 0.75em;
                         pointer-events: none; display: none; white-space: nowrap; }}
  .waveform .playhead {{ position: absolute; top: 0; bottom: 0; width: 2px;
                          background: #ff5370; pointer-events: none; display: none; }}
  .seg {{ padding: 8px 0; border-bottom: 1px solid #2a2a4a; transition: background 0.3s, border-color 0.3s;
          position: relative; }}
  .seg.active {{ background: #1e2a4a; border-radius: 4px; }}
  .seg.playing {{ background: #1a2844; border-left: 3px solid #e8e060; padding-left: 8px; padding-right: 12px;
                  border-radius: 0 6px 6px 0; z-index: 2;
                  box-shadow: 0 0 15px rgba(232,224,96,0.35), 0 0 30px rgba(232,224,96,0.12);
                  outline: 1.5px solid rgba(232,224,96,0.4); }}
  .ts {{ display: inline-block; background: #2a2a4a; color: #9a9ac0; padding: 1px 8px;
         border-radius: 3px; text-decoration: none; font-size: 0.8em; font-family: monospace;
         cursor: pointer; margin-bottom: 4px; }}
  .ts:hover {{ background: #3a3a5a; color: #c0c0e0; }}
  .ts::before {{ content: "\u23f5 "; }}
  .line {{ margin: 2px 0 2px 1em; }}
  .line-dst {{ opacity: 0.7; font-size: 0.92em; margin-left: 1.5em; }}
  .spk {{ font-weight: bold; }}
  .footer {{ margin-top: 3em; padding-top: 1em; padding-bottom: 3em; border-top: 1px solid #444;
             color: #888; font-size: 0.85em; }}
  .footer a {{ color: #82aaff; text-decoration: none; margin: 0 0.3em; }}
  .footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
{meta_html}
<div class="sticky-player">
  <audio id="player" controls preload="metadata">
    <source src="{audio_src}">
  </audio>
  <div class="waveform" id="waveform">
    <div class="tooltip" id="wf-tip"></div>
    <div class="playhead" id="playhead"></div>
  </div>
</div>
{body_html}
<div class="footer">
  相關檔案：{footer_html}
</div>
<script>
(function() {{
  var player = document.getElementById('player');
  var wf = document.getElementById('waveform');
  var tip = document.getElementById('wf-tip');
  var playhead = document.getElementById('playhead');
  var dur = {dur_str};
  var bins = {waveform_json};

  // 建立段落時間索引（從 DOM 的 .ts[data-t] 讀取，最可靠）
  var tsEls = document.querySelectorAll('.ts');
  var segList = [];
  tsEls.forEach(function(a, i) {{
    var st = parseFloat(a.getAttribute('data-t'));
    var next = (i + 1 < tsEls.length) ? parseFloat(tsEls[i+1].getAttribute('data-t')) : dur;
    segList.push({{ start: st, end: next, el: a.closest('.seg') }});
  }});

  // 繪製波形（RMS 振幅 bin）
  if (dur > 0 && bins.length > 0) {{
    var maxRms = Math.max.apply(null, bins.map(function(s) {{ return s.rms; }})) || 0.01;
    var colors = {json.dumps(_SPEAKER_HTML_COLORS)};
    var barW = 100.0 / bins.length;
    bins.forEach(function(s, i) {{
      var bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.left = (i * barW) + '%';
      bar.style.width = Math.max(barW, 0.3) + '%';
      var h = Math.max((s.rms / maxRms) * 44 + 2, 2);
      bar.style.height = h + 'px';
      if (s.spk != null) {{
        bar.style.background = colors[(s.spk - 1) % colors.length];
        bar.style.opacity = '0.7';
      }}
      wf.appendChild(bar);
    }});
  }}

  function fmtTime(t) {{
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var s = Math.floor(t % 60);
    if (h > 0) return h + ':' + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }}

  // tooltip（時:分:秒）
  wf.addEventListener('mousemove', function(e) {{
    if (dur <= 0) return;
    var rect = wf.getBoundingClientRect();
    var pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    tip.textContent = fmtTime(pct * dur);
    tip.style.left = Math.min(e.clientX - rect.left, rect.width - 50) + 'px';
    tip.style.display = 'block';
  }});
  wf.addEventListener('mouseleave', function() {{ tip.style.display = 'none'; }});

  // click 波形跳轉
  var skipAutoScroll = false;
  wf.addEventListener('click', function(e) {{
    if (dur <= 0) return;
    var rect = wf.getBoundingClientRect();
    var pct = (e.clientX - rect.left) / rect.width;
    var t = pct * dur;
    skipAutoScroll = true;
    player.currentTime = t;
    player.play();
    // 手動跳到最近段落
    var best = findSeg(t);
    if (best) {{
      setPlaying(best);
      best.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
    }}
    setTimeout(function() {{ skipAutoScroll = false; }}, 1500);
  }});

  // 時間戳點擊
  tsEls.forEach(function(a) {{
    a.addEventListener('click', function(e) {{
      e.preventDefault();
      skipAutoScroll = true;
      var t = parseFloat(this.getAttribute('data-t'));
      player.currentTime = t;
      player.play();
      setPlaying(this.closest('.seg'));
      setTimeout(function() {{ skipAutoScroll = false; }}, 1500);
    }});
  }});

  // playhead + 段落跟隨
  var lastPlayingEl = null;
  player.addEventListener('timeupdate', function() {{
    if (dur <= 0) return;
    var ct = player.currentTime;
    playhead.style.left = (ct / dur * 100) + '%';
    playhead.style.display = 'block';

    var el = findSeg(ct);
    if (el && el !== lastPlayingEl) {{
      setPlaying(el);
      if (!skipAutoScroll) {{
        el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
      }}
    }}
  }});

  player.addEventListener('pause', function() {{
    if (lastPlayingEl) {{ lastPlayingEl.classList.remove('playing'); lastPlayingEl = null; }}
  }});

  function findSeg(t) {{
    for (var i = 0; i < segList.length; i++) {{
      if (t >= segList[i].start && t < segList[i].end) return segList[i].el;
    }}
    // 落在最後一段之後
    if (segList.length > 0 && t >= segList[segList.length-1].start) {{
      return segList[segList.length-1].el;
    }}
    return null;
  }}

  function setPlaying(el) {{
    if (lastPlayingEl) lastPlayingEl.classList.remove('playing');
    if (el) el.classList.add('playing');
    lastPlayingEl = el;
  }}
}})();
</script>
</body>
</html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    return html_path


# ─── 終端機管理（Ctrl+S 支援）────────────────────
_original_termios = None


def setup_terminal_raw_input():
    """停用 IXON（釋放 Ctrl+S）並設定最小化 raw mode"""
    global _original_termios
    try:
        fd = sys.stdin.fileno()
        _original_termios = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        # 停用 IXON（讓 Ctrl+S 不再被系統攔截）
        new[0] &= ~termios.IXON  # iflag
        # 設定 non-canonical mode：不需 Enter 就能讀取按鍵
        new[3] &= ~(termios.ICANON | termios.ECHO)  # lflag
        new[6][termios.VMIN] = 0   # 不阻塞
        new[6][termios.VTIME] = 0  # 不等待
        termios.tcsetattr(fd, termios.TCSANOW, new)
        atexit.register(restore_terminal)
    except Exception:
        _original_termios = None


def restore_terminal():
    """恢復原始 termios 設定"""
    global _original_termios
    if _original_termios is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _original_termios)
        except Exception:
            pass
        _original_termios = None


def keypress_listener_thread(stop_event, ctrl_s_event=None, pause_event=None):
    """Daemon thread：持續偵測 Ctrl+S / Ctrl+P"""
    fd = sys.stdin.fileno()
    while not stop_event.is_set():
        try:
            rlist, _, _ = select.select([fd], [], [], 0.2)
            if rlist:
                data = os.read(fd, 32)
                if b'\x10' in data and pause_event is not None:  # Ctrl+P
                    if pause_event.is_set():
                        pause_event.clear()
                        _status_bar_state["paused"] = False
                    else:
                        pause_event.set()
                        _status_bar_state["paused"] = True
                if b'\x13' in data and ctrl_s_event is not None:  # Ctrl+S
                    ctrl_s_event.set()
        except Exception:
            return


# ─── 音量波形共用常數 ────────────────────────────────────────
_BARS = "▁▂▃▄▅▆▇█"


def _rms_to_bar(rms):
    """RMS → 波形字元（對數刻度，增強微弱聲音的可見度）"""
    if rms < 0.0005:
        return _BARS[0]
    db = 20 * math.log10(max(rms, 1e-10))
    idx = int((db + 60) / 54 * (len(_BARS) - 1))
    return _BARS[max(0, min(idx, len(_BARS) - 1))]


# ─── 底部狀態列（固定顯示快捷鍵提示 + 即時資訊）────────────────
_status_bar_active = False
_status_bar_needs_resize = False
_status_bar_state = {
    "start_time": 0.0,   # monotonic 起始時間
    "count": 0,          # 翻譯/轉錄筆數
    "mode": "en2zh",     # 功能模式
    "model_name": "",    # 模型名稱（如 large-v3-turbo）
    "asr_location": "",  # ASR 位置（"本機" / "遠端"）
    "translate_model": "",  # 翻譯模型名稱（如 qwen2.5:14b）
    "translate_location": "",  # 翻譯位置（"本機" / "遠端"）
    "rms_history": None,  # deque(maxlen=12)，由 setup_status_bar 初始化
    "rms_lock": None,     # threading.Lock
    "paused": False,     # Ctrl+P 暫停狀態
}


def setup_status_bar(mode="en2zh", model_name="", asr_location="",
                     translate_model="", translate_location=""):
    """設定終端機底部固定狀態列，利用 scroll region 讓字幕只在上方滾動"""
    global _status_bar_active
    _status_bar_state["start_time"] = time.monotonic()
    _status_bar_state["count"] = 0
    _status_bar_state["mode"] = mode
    _status_bar_state["model_name"] = model_name
    _status_bar_state["asr_location"] = asr_location
    _status_bar_state["translate_model"] = translate_model
    _status_bar_state["translate_location"] = translate_location
    _status_bar_state["rms_history"] = deque(maxlen=12)
    _status_bar_state["rms_lock"] = threading.Lock()
    _status_bar_state["paused"] = False
    try:
        cols, rows = os.get_terminal_size()
        _status_bar_state["_last_rows"] = rows
        # 設定滾動區域：第 1 行到倒數第 2 行（最後一行保留給狀態列）
        sys.stdout.write(f"\x1b[1;{rows - 1}r")
        _status_bar_active = True
        _draw_status_bar(rows, cols)
        # 移動游標到滾動區域底部
        sys.stdout.write(f"\x1b[{rows - 1};1H")
        sys.stdout.flush()
    except Exception:
        _status_bar_active = False


def _push_rms(rms):
    """Thread-safe 寫入一筆 RMS 值到狀態列波形歷史"""
    lock = _status_bar_state.get("rms_lock")
    hist = _status_bar_state.get("rms_history")
    if lock and hist is not None:
        with lock:
            hist.append(rms)


def refresh_status_bar():
    """重繪底部狀態列（供外部在 print_lock 內呼叫）"""
    global _status_bar_needs_resize
    if not _status_bar_active:
        return
    if _status_bar_needs_resize:
        _status_bar_needs_resize = False
        try:
            cols, rows = os.get_terminal_size()
            old_rows = _status_bar_state.get("_last_rows", 0)
            # 視窗變大時，清除舊狀態列殘影
            if old_rows and old_rows != rows and old_rows <= rows:
                sys.stdout.write(f"\x1b7\x1b[{old_rows};1H\x1b[2K\x1b8")
            _status_bar_state["_last_rows"] = rows
            sys.stdout.write(f"\x1b[1;{rows - 1}r")
            _draw_status_bar(rows, cols)
            sys.stdout.write(f"\x1b[{rows - 1};1H")
            sys.stdout.flush()
        except Exception:
            pass
    else:
        _draw_status_bar()


def _draw_status_bar(rows=None, cols=None):
    """在終端機最後一行繪製狀態列"""
    try:
        if not rows or not cols:
            cols, rows = os.get_terminal_size()
        sys.stdout.write("\x1b7")  # 儲存游標位置
        # 偵測視窗大小改變，清除舊狀態列殘影
        old_rows = _status_bar_state.get("_last_rows", 0)
        if old_rows and old_rows != rows:
            if old_rows < rows:
                # 視窗變大：舊狀態列位置現在在內容區域，需要清除
                sys.stdout.write(f"\x1b[{old_rows};1H\x1b[2K")
            # 更新 scroll region
            sys.stdout.write(f"\x1b[1;{rows - 1}r")
            _status_bar_state["_last_rows"] = rows
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")  # 移到最後一行並清除
        # 組合狀態文字
        elapsed = time.monotonic() - _status_bar_state["start_time"]
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}"
        count = _status_bar_state["count"]
        label = "轉錄" if _status_bar_state["mode"] in ("zh", "en") else "翻譯"
        # 波形文字（12 字元）
        wave_str = ""
        lock = _status_bar_state.get("rms_lock")
        hist = _status_bar_state.get("rms_history")
        if lock and hist is not None:
            with lock:
                samples = list(hist)
            if len(samples) < 12:
                samples = [0.0] * (12 - len(samples)) + samples
            else:
                samples = samples[-12:]
            wave_str = "".join(_rms_to_bar(s) for s in samples)
        wave_colored = f"\x1b[38;2;80;200;120m{wave_str}\x1b[38;2;200;200;200m" if wave_str else ""
        # 語音辨識 + 翻譯模型欄位
        info_parts = []
        info_parts_display = []
        m_loc = _status_bar_state.get("asr_location", "")
        if m_loc:
            asr_str = f"辨識 [{m_loc}]"
            asr_str_display = f"辨識 \x1b[38;2;100;180;255m[{m_loc}]\x1b[38;2;200;200;200m"
            info_parts.append(asr_str)
            info_parts_display.append(asr_str_display)
        t_model = _status_bar_state.get("translate_model", "")
        t_loc = _status_bar_state.get("translate_location", "")
        if t_model:
            tr_str = f"翻譯 [{t_loc}]" if t_loc else "翻譯"
            tr_str_display = f"翻譯 \x1b[38;2;100;180;255m[{t_loc}]\x1b[38;2;200;200;200m" if t_loc else "翻譯"
            info_parts.append(tr_str)
            info_parts_display.append(tr_str_display)
        model_part = " | ".join(info_parts)
        model_part_display = " | ".join(info_parts_display)
        if _status_bar_state.get("paused"):
            pause_str = "\x1b[38;2;255;220;80m\u23f8 \u5df2\u66ab\u505c\x1b[38;2;200;200;200m"
            hotkey_str = "Ctrl+P \u7e7c\u7e8c | Ctrl+C \u505c\u6b62"
            if model_part:
                status = f" {time_str} {wave_str} | {model_part} | \u23f8 \u5df2\u66ab\u505c | {hotkey_str} "
                status_display = f" {time_str} {wave_colored} | {model_part_display} | {pause_str} | {hotkey_str} "
            else:
                status = f" {time_str} {wave_str} | \u23f8 \u5df2\u66ab\u505c | {hotkey_str} "
                status_display = f" {time_str} {wave_colored} | {pause_str} | {hotkey_str} "
        else:
            hotkey_str = "Ctrl+P \u66ab\u505c | Ctrl+C \u505c\u6b62"
            if model_part:
                status = f" {time_str} {wave_str} | {model_part} | {label} {count} \u7b46 | {hotkey_str} "
                status_display = f" {time_str} {wave_colored} | {model_part_display} | {label} {count} \u7b46 | {hotkey_str} "
            else:
                status = f" {time_str} {wave_str} | {label} {count} \u7b46 | {hotkey_str} "
                status_display = f" {time_str} {wave_colored} | {label} {count} \u7b46 | {hotkey_str} "
        # 計算顯示寬度（中文字佔 2 格）
        dw = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in status)
        padding = " " * max(0, cols - dw)
        sys.stdout.write(f"\x1b[48;2;60;60;60m\x1b[38;2;200;200;200m{status_display}{padding}\x1b[0m")
        sys.stdout.write("\x1b8")  # 恢復游標位置
        sys.stdout.flush()
    except Exception:
        pass


def clear_status_bar():
    """清除狀態列，恢復正常滾動區域"""
    global _status_bar_active
    if not _status_bar_active:
        return
    _status_bar_active = False
    try:
        sys.stdout.write("\x1b[r")  # 重設滾動區域為整個終端機
        cols, rows = os.get_terminal_size()
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")  # 清除最後一行
        sys.stdout.flush()
    except Exception:
        pass


def _handle_sigwinch(signum, frame):
    """終端機視窗大小改變時設定 flag，由主迴圈安全處理"""
    global _status_bar_needs_resize
    if _status_bar_active:
        _status_bar_needs_resize = True


def _start_audio_monitor():
    """開啟輕量 InputStream 被動監控 BlackHole 音量（Whisper 無錄音時用）。
    BlackHole 支援多讀取者，不影響 whisper-stream。回傳 stream 物件。"""
    import sounddevice as sd
    import numpy as np

    # 找 BlackHole PortAudio device
    bh_id = None
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
            bh_id = i
            break
    if bh_id is None:
        return None

    dev_info = sd.query_devices(bh_id)
    sr = int(dev_info["default_samplerate"])
    ch = max(dev_info["max_input_channels"], 1)

    def _monitor_cb(indata, frames, time_info, status):
        _push_rms(float(np.sqrt(np.mean(indata ** 2))))

    try:
        stream = sd.InputStream(
            device=bh_id, samplerate=sr, channels=ch,
            blocksize=int(sr * 0.1), dtype="float32",
            callback=_monitor_cb,
        )
        stream.start()
        return stream
    except Exception:
        return None


def _stop_audio_monitor(stream):
    """停止並關閉被動音量監控 stream"""
    if stream is None:
        return
    try:
        stream.stop()
        stream.close()
    except Exception:
        pass


def parse_args():
    """解析命令列參數"""
    examples = [
        ("./start.sh", "互動式選單"),
        ("./start.sh -s training", "教育訓練場景"),
        ("./start.sh --mode zh", "中文轉錄模式"),
        ("./start.sh --asr moonshine", "使用 Moonshine 引擎"),
        ("./start.sh --topic 'ZFS 儲存管理'", "指定會議主題，提升翻譯品質"),
        ("./start.sh -m large-v3-turbo -e llm -d 0", "全部指定，跳過選單"),
        ("./start.sh --input meeting.mp3", "離線處理音訊檔（互動選單）"),
        ("./start.sh --input meeting.mp3 --mode en2zh", "離線處理（直接執行，跳過選單）"),
        ("./start.sh --input meeting.mp3 --mode en", "離線處理（純英文轉錄）"),
        ("./start.sh --input f1.mp3 f2.m4a --summarize", "離線處理 + 摘要"),
        ("./start.sh --input meeting.mp3 --diarize", "離線處理 + 講者辨識"),
        ("./start.sh --input meeting.mp3 --diarize --mode zh", "中文逐字稿 + 講者辨識"),
        ("./start.sh --input meeting.mp3 --mode zh --summarize", "中文逐字稿 + 摘要修正"),
        ("./start.sh --input meeting.mp3 --diarize --num-speakers 3", "指定 3 位講者"),
        ("./start.sh --input meeting.mp3 --diarize --summarize", "辨識 + 翻譯 + 摘要"),
        ("./start.sh --input m.mp3 --diarize --mode zh --summarize", "中文辨識 + 講者 + 摘要"),
        ("./start.sh --input meeting.mp3 --local-asr", "強制本機 辨識"),
        ("./start.sh --summarize log1.txt log2.txt", "批次摘要記錄檔"),
    ]
    col = max(len(cmd) for cmd, _ in examples) + 3
    epilog = "範例:\n" + "\n".join(f"  {cmd:<{col}}{desc}" for cmd, desc in examples)
    parser = argparse.ArgumentParser(
        description="即時英翻中字幕系統 jt-live-whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    mode_names = list(MODE_MAP.keys())
    model_names = [name for name, _, _ in WHISPER_MODELS]
    scene_names = list(SCENE_MAP.keys())
    moonshine_model_names = [name for name, _, _ in MOONSHINE_MODELS]
    parser.add_argument(
        "--mode", choices=mode_names, metavar="MODE",
        help=f"功能模式 ({' / '.join(mode_names)}，預設 en2zh 英翻中)")
    parser.add_argument(
        "--asr", choices=["whisper", "moonshine"], metavar="ASR",
        help="語音辨識引擎 (whisper / moonshine，預設 whisper)")
    parser.add_argument(
        "-m", "--model", choices=model_names, metavar="MODEL",
        help=f"Whisper 模型 ({' / '.join(model_names)}，--input 預設 large-v3-turbo，中文品質最好用 -m large-v3)")
    parser.add_argument(
        "--moonshine-model", choices=moonshine_model_names, metavar="MMODEL",
        help=f"Moonshine 模型 ({' / '.join(moonshine_model_names)}，預設 medium)")
    parser.add_argument(
        "-s", "--scene", choices=scene_names, metavar="SCENE",
        help=f"使用場景 ({' / '.join(scene_names)})")
    parser.add_argument(
        "--topic", metavar="TOPIC",
        help="會議主題（提升翻譯品質，例：--topic 'ZFS 儲存管理'）")
    parser.add_argument(
        "-d", "--device", type=int, metavar="ID",
        help="音訊裝置 ID (數字，可用 --list-devices 查詢)")
    parser.add_argument(
        "-e", "--engine", choices=["llm", "argos"], metavar="ENGINE",
        help="翻譯引擎 (llm / argos，llm 支援 Ollama 及 OpenAI 相容伺服器)")
    parser.add_argument(
        "--llm-model", metavar="NAME", dest="ollama_model",
        help="LLM 翻譯模型名稱 (預設 qwen2.5:14b)")
    parser.add_argument(
        "--llm-host", metavar="HOST", dest="ollama_host",
        help="LLM 伺服器位址，自動偵測 Ollama 或 OpenAI 相容 (例如 192.168.1.40:11434)")
    parser.add_argument(
        "--list-devices", action="store_true",
        help="列出可用音訊裝置後離開")
    parser.add_argument(
        "--record", action="store_true",
        help="即時模式同時錄製音訊為 WAV 檔（存入 recordings/）")
    parser.add_argument(
        "--rec-device", type=int, metavar="ID",
        help="錄音裝置 ID (可與 ASR 裝置不同，例如聚集裝置可同時錄雙方聲音)")
    parser.add_argument(
        "--input", nargs="+", metavar="FILE",
        help="離線處理音訊檔 (mp3/wav/m4a/flac 等，用 faster-whisper 辨識)")
    parser.add_argument(
        "--summarize", nargs="*", metavar="FILE", default=None,
        help="摘要模式：讀取記錄檔生成摘要後離開（與 --input 合用時不需指定檔案）")
    parser.add_argument(
        "--summary-model", metavar="MODEL", default=SUMMARY_DEFAULT_MODEL,
        help=f"摘要用的 LLM 模型 (預設 {SUMMARY_DEFAULT_MODEL})")
    parser.add_argument(
        "--diarize", action="store_true",
        help="講者辨識（需搭配 --input，用 resemblyzer + spectralcluster）")
    parser.add_argument(
        "--num-speakers", type=int, metavar="N",
        help="指定講者人數（預設自動偵測 2~8，需搭配 --diarize）")
    parser.add_argument(
        "--local-asr", action="store_true",
        help="強制使用本機 辨識（忽略遠端 GPU 設定，即時模式與離線模式皆適用）")
    parser.add_argument(
        "--restart-server", action="store_true",
        help="強制重啟遠端 GPU 伺服器（更新 server.py 後使用）")
    return parser.parse_args()


def auto_select_device(model_path):
    """非互動模式：自動偵測 BlackHole 裝置，找不到就報錯退出"""
    devices = _enumerate_sdl_devices(model_path)

    if not devices:
        print("[錯誤] 找不到任何音訊捕捉裝置！", file=sys.stderr)
        sys.exit(1)

    # 自動選 BlackHole
    for dev_id, dev_name in devices:
        if "blackhole" in dev_name.lower():
            print(f"{C_OK}自動選擇音訊裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 BlackHole，用第一個裝置
    dev_id, dev_name = devices[0]
    print(f"{C_HIGHLIGHT}未偵測到 BlackHole，使用: [{dev_id}] {dev_name}{RESET}")
    return dev_id


def resolve_model(model_name):
    """從模型名稱取得完整路徑，找不到就報錯退出"""
    for name, filename, desc in WHISPER_MODELS:
        if name == model_name:
            path = os.path.join(MODELS_DIR, filename)
            if os.path.isfile(path):
                return name, path
            print(f"[錯誤] 模型檔案不存在: {path}", file=sys.stderr)
            sys.exit(1)
    print(f"[錯誤] 不認識的模型: {model_name}", file=sys.stderr)
    sys.exit(1)


def _resolve_ollama_host(args):
    """從 args 解析 LLM 伺服器 host/port，無設定時回傳 (None, port)"""
    host, port = OLLAMA_HOST, OLLAMA_PORT
    if args.ollama_host:
        if ":" in args.ollama_host:
            parts = args.ollama_host.rsplit(":", 1)
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass  # 保持預設 port
        else:
            host = args.ollama_host
    return host, port


def _build_cli_command(**kwargs):
    """根據設定組裝等效的 ./start.sh CLI 指令字串（所有有值的參數都明確列出）"""
    import shlex
    parts = ["./start.sh"]

    input_files = kwargs.get("input_files")
    if input_files:
        parts.append("--input")
        for f in input_files:
            parts.append(shlex.quote(f))

    mode = kwargs.get("mode")
    if mode:
        parts.append(f"--mode {mode}")

    model = kwargs.get("model")
    if model:
        parts.append(f"-m {model}")

    asr = kwargs.get("asr")
    if asr:
        parts.append(f"--asr {asr}")

    moonshine_model = kwargs.get("moonshine_model")
    if moonshine_model:
        parts.append(f"--moonshine-model {moonshine_model}")

    scene = kwargs.get("scene")
    if scene:
        parts.append(f"-s {scene}")

    engine = kwargs.get("engine")
    if engine:
        parts.append(f"-e {engine}")

    llm_model = kwargs.get("llm_model")
    if llm_model:
        parts.append(f"--llm-model {shlex.quote(llm_model)}")

    llm_host = kwargs.get("llm_host")
    if llm_host:
        parts.append(f"--llm-host {shlex.quote(llm_host)}")

    topic = kwargs.get("topic")
    if topic:
        parts.append(f"--topic {shlex.quote(topic)}")

    device = kwargs.get("device")
    if device is not None:
        parts.append(f"-d {device}")

    diarize = kwargs.get("diarize")
    if diarize:
        parts.append("--diarize")

    num_speakers = kwargs.get("num_speakers")
    if num_speakers:
        parts.append(f"--num-speakers {num_speakers}")

    summarize = kwargs.get("summarize")
    if summarize:
        parts.append("--summarize")

    summary_model = kwargs.get("summary_model")
    if summary_model:
        parts.append(f"--summary-model {shlex.quote(summary_model)}")

    record = kwargs.get("record")
    if record:
        parts.append("--record")

    rec_device = kwargs.get("rec_device")
    if rec_device is not None:
        parts.append(f"--rec-device {rec_device}")

    local_asr = kwargs.get("local_asr")
    if local_asr:
        parts.append("--local-asr")

    return " ".join(parts)


def _confirm_start(cli_cmd):
    """印出等效 CLI 指令，詢問 Y/n 確認。回傳 True 繼續、False 取消。"""
    print(f"  {C_DIM}等效指令    {RESET}{C_OK}{cli_cmd}{RESET}")
    print(f"  {C_DIM}            （下次可直接執行，不需進入互動選單）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    try:
        ans = input(f"\n{C_WHITE}確認開始？({C_HIGHLIGHT}Y{C_WHITE}/n)：{RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans in ("", "y", "yes"):
        return True
    return False


def main():
    args = parse_args()
    cli_mode = (len(sys.argv) > 1 and not args.list_devices
                and args.summarize is None and not args.input)

    # --rec-device 自動啟用 --record
    if args.rec_device is not None and not args.record:
        args.record = True

    # --num-speakers 沒搭配 --diarize 時警告
    if args.num_speakers and not args.diarize:
        print(f"{C_HIGHLIGHT}[警告] --num-speakers 需搭配 --diarize 使用，已忽略{RESET}")

    # 互動模式（無 CLI 參數）：第一步選擇輸入來源
    if (not cli_mode and not args.input and args.summarize is None
            and not args.list_devices):
        source, files = _ask_input_source()
        if source == "file":
            args.input = files

    # --input 離線處理音訊檔
    if args.input:
        # 純錄音模式不適用於離線處理
        if args.mode == "record":
            print("[錯誤] 純錄音模式不適用於離線處理（--input）", file=sys.stderr)
            sys.exit(1)
        # 決定參數來源：有任何使用者明確傳入的 CLI 參數 → CLI 模式；全無 → 互動選單
        # 注意：args.summary_model 有 argparse 預設值，不能用來判斷
        _has_cli_args = (args.mode is not None or args.model or
                         args.diarize or
                         args.num_speakers or args.summarize is not None or
                         args.engine or args.ollama_model or
                         args.ollama_host or
                         args.local_asr or getattr(args, 'topic', None))
        if not _has_cli_args:
            (mode, fw_model, ollama_model, summary_model,
             host, port, diarize, num_speakers, do_summarize,
             server_type, use_remote_whisper, meeting_topic,
             summary_mode) = _input_interactive_menu(args)
            engine = "llm"
            if not server_type:
                server_type = "ollama"
        else:
            mode = args.mode or "en2zh"
            diarize = args.diarize
            num_speakers = args.num_speakers
            do_summarize = args.summarize is not None
            summary_mode = "both"  # CLI 模式預設
            _default_fw = "large-v3" if mode in ("zh", "zh2en") else "large-v3-turbo"
            fw_model = args.model or _default_fw
            host, port = _resolve_ollama_host(args)
            server_type = None  # CLI 模式稍後偵測
            need_translate_cli = mode in ("en2zh", "zh2en")
            ollama_model = None
            if need_translate_cli:
                if args.engine or args.ollama_model or args.ollama_host:
                    # 有指定任何翻譯相關參數 → 隱含 -e llm
                    engine = args.engine or "llm"
                else:
                    # 未指定翻譯參數：自動偵測或用互動選單
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port)
                    if engine == "llm":
                        ollama_model = _sel_model
                        if _sel_host: host = _sel_host
                        if _sel_port: port = _sel_port
                        if _sel_srv: server_type = _sel_srv
                if engine == "llm" and not ollama_model:
                    if not server_type:
                        server_type = _detect_llm_server(host, port)
                    if host:
                        ollama_model = args.ollama_model or _select_llm_model(host, port, server_type or "ollama")
                    else:
                        # 無 LLM 伺服器，降級 Argos
                        engine = "argos"
            else:
                engine = "llm"
            summary_model = args.summary_model
            # 遠端 GPU：有設定且未指定 --local-asr
            use_remote_whisper = (REMOTE_WHISPER_CONFIG is not None
                                 and not args.local_asr)
            meeting_topic = getattr(args, 'topic', None)

        # --diarize 檢查 resemblyzer / spectralcluster
        if diarize:
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="pkg_resources is deprecated")
                    import resemblyzer  # noqa: F401
                import spectralcluster  # noqa: F401
            except ImportError as e:
                print(f"{C_HIGHLIGHT}[錯誤] 講者辨識需要額外套件: {e}{RESET}", file=sys.stderr)
                print(f"  {C_DIM}pip install resemblyzer spectralcluster{RESET}", file=sys.stderr)
                sys.exit(1)

        mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
        need_translate = mode in ("en2zh", "zh2en")
        if not ollama_model:
            ollama_model = "qwen2.5:14b"

        # ── 連線檢查 ──
        ollama_available = False
        need_llm_translate = need_translate and engine == "llm"
        need_remote_asr = use_remote_whisper and REMOTE_WHISPER_CONFIG
        need_check = need_llm_translate or do_summarize or need_remote_asr

        if need_check:
            print(f"\n\n{C_TITLE}{BOLD}▎ 連線檢查{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")

        if need_llm_translate or do_summarize:
            if not server_type:
                server_type = _detect_llm_server(host, port)
            if server_type:
                srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
                if need_llm_translate:
                    print(f"  {C_WHITE}LLM 翻譯    {RESET}{C_WHITE}{ollama_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                if do_summarize:
                    print(f"  {C_WHITE}LLM 摘要    {RESET}{C_WHITE}{summary_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                ollama_available = True
            else:
                label = "LLM" if need_llm_translate else "LLM 摘要"
                model_name_display = ollama_model if need_llm_translate else summary_model
                pad = " " * (12 - _str_display_width(label))
                print(f"  {C_WHITE}{label}{pad}{RESET}{C_WHITE}{model_name_display}{RESET} {C_DIM}@ {host}:{port}{RESET} {C_HIGHLIGHT}✗ 無法連接{RESET}")

        if not server_type:
            server_type = "ollama"

        # 初始化翻譯器（meeting_topic 已在互動選單或 CLI 分支中設定）
        translator = None
        can_summarize = ollama_available
        if need_translate:
            if engine == "llm" and ollama_available:
                translator = OllamaTranslator(ollama_model, host, port, direction=mode,
                                              skip_check=True, server_type=server_type,
                                              meeting_topic=meeting_topic)
            elif engine == "llm" and not ollama_available:
                # LLM 伺服器連不上：降級處理
                if mode == "zh2en":
                    print(f"  {C_HIGHLIGHT}[警告] 中翻英不支援 Argos 離線翻譯，將只做中文轉錄（不翻譯）{RESET}")
                else:
                    print(f"  {C_HIGHLIGHT}[降級] 改用 Argos 離線翻譯（品質較低）{RESET}")
                    translator = ArgosTranslator()
            else:
                # 使用者明確指定 argos
                if mode == "zh2en":
                    print(f"{C_HIGHLIGHT}[錯誤] 中翻英模式不支援 Argos 離線翻譯，請使用 LLM 伺服器{RESET}",
                          file=sys.stderr)
                    sys.exit(1)
                translator = ArgosTranslator()

        if do_summarize and not can_summarize:
            print(f"  {C_HIGHLIGHT}[警告] LLM 伺服器無法連接，摘要將跳過（逐字稿完成後可用 --summarize 補做）{RESET}")

        # 遠端 GPU 啟動與 health check
        remote_whisper_cfg = None
        if need_remote_asr:
            rw_cfg = REMOTE_WHISPER_CONFIG
            rw_host = rw_cfg.get("host", "?")
            rw_port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
            print(f"  {C_WHITE}遠端辨識    {RESET}{C_WHITE}{fw_model}{RESET} {C_DIM}@ {rw_host}:{rw_port}{RESET}", end="", flush=True)
            # 檢查伺服器是否已在運行，沒有才啟動（支援多實例共用）
            force_rs = getattr(args, 'restart_server', False)
            print(f" {C_DIM}{'重啟中' if force_rs else '啟動中'}{RESET}", end="", flush=True)
            _inline_spinner(_remote_whisper_start, rw_cfg, force_restart=force_rs)
            print(f" {C_DIM}...{RESET}", end=" ", flush=True)
            try:
                ok, has_gpu = _inline_spinner(_remote_whisper_health, rw_cfg, timeout=30)
            except Exception:
                ok, has_gpu = False, False
            if ok:
                if has_gpu:
                    print(f"{C_OK}✓ 已連線（GPU）{RESET}")
                else:
                    print(f"{C_HIGHLIGHT}✓ 已連線（注意：遠端未偵測到 GPU，將以 CPU 辨識，速度較慢）{RESET}")
                remote_whisper_cfg = rw_cfg
            else:
                print(f"{C_HIGHLIGHT}✗ 無法連接{RESET}")
                print(f"  {C_HIGHLIGHT}[降級] 改用本機 辨識{RESET}")

        # 顯示設定資訊
        print(f"\n\n{C_TITLE}{BOLD}▎ 設定總覽{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_WHITE}模式        {mode_label}{RESET}")
        print(f"  {C_WHITE}辨識模型    {fw_model}{RESET}")
        if remote_whisper_cfg:
            rw_h = remote_whisper_cfg.get("host", "?")
            print(f"  {C_WHITE}辨識位置    遠端 GPU（{rw_h}）{RESET}")
        else:
            print(f"  {C_WHITE}辨識位置    本機{RESET}")
        if need_translate:
            if engine == "argos":
                print(f"  {C_WHITE}翻譯模型    Argos 本機離線{RESET}")
            else:
                _srv_disp = f"{ollama_model} @ {host}:{port}"
                print(f"  {C_WHITE}翻譯模型    {_srv_disp}{RESET}")
        if diarize:
            sp_info = "resemblyzer + spectralcluster"
            if remote_whisper_cfg:
                sp_info += f"，遠端 GPU（{remote_whisper_cfg.get('host', '?')}）"
            else:
                sp_info += "，本機"
            sp_info += f"，{num_speakers} 人" if num_speakers else "，自動偵測"
            print(f"  {C_WHITE}講者辨識    {sp_info}{RESET}")
        if do_summarize and host:
            print(f"  {C_WHITE}摘要模型    {summary_model} @ {host}:{port}{RESET}")
        if meeting_topic:
            print(f"  {C_WHITE}會議主題    {meeting_topic}{RESET}")
        print(f"  {C_WHITE}檔案數      {RESET}{C_DIM}{len(args.input)}{RESET}")

        # CLI 指令回顯 + 確認（在設定總覽區塊內）
        _cli_kw = dict(input_files=args.input, mode=mode, model=fw_model,
                       diarize=diarize, num_speakers=num_speakers,
                       summarize=do_summarize, summary_model=summary_model,
                       engine=engine if engine == "argos" else None,
                       llm_model=ollama_model if need_translate and engine == "llm" else None,
                       llm_host=f"{host}:{port}" if need_translate and engine == "llm" and host else None,
                       topic=meeting_topic,
                       local_asr=args.local_asr)
        if not _confirm_start(_build_cli_command(**_cli_kw)):
            sys.exit(0)

        # 逐檔處理
        log_paths = []  # list of (log_path, original_input_path, session_dir)
        html_to_open = []  # 收集所有 HTML，最後一起開啟
        try:
            for fpath in args.input:
                log_path, t_html, session_dir = process_audio_file(fpath, mode, translator, model_size=fw_model,
                                                       diarize=diarize, num_speakers=num_speakers,
                                                       remote_whisper_cfg=remote_whisper_cfg)
                if log_path:
                    log_paths.append((log_path, fpath, session_dir))
                if t_html:
                    html_to_open.append(t_html)
        except KeyboardInterrupt:
            remaining = len(args.input) - len(log_paths)
            if remaining > 1:
                print(f"\n{C_DIM}已中止，跳過剩餘 {remaining - 1} 個檔案。{RESET}")

        # 遠端伺服器保持運行（不停止，允許多實例共用）
        if remote_whisper_cfg:
            _ssh_close_cm(remote_whisper_cfg)

        # 如果需要摘要且 LLM 伺服器可用，對產生的 log 檔自動摘要
        if do_summarize and log_paths and can_summarize:
            print(f"\n\n{C_TITLE}{BOLD}▎ 自動摘要{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            print(f"  {C_DIM}摘要模型: {summary_model} ({host}:{port}){RESET}")
            srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"

            for lp, orig_fpath, sess_dir in log_paths:
                print(f"\n  {C_DIM}摘要: {os.path.basename(lp)}{RESET}")
                t_summary_start = time.monotonic()
                # 用子目錄中的音訊副本
                audio_in_session = os.path.join(sess_dir, os.path.basename(orig_fpath))
                # 組裝 metadata
                _meta = {
                    "asr_engine": remote_whisper_cfg.get("_backend", "faster-whisper") if remote_whisper_cfg else "faster-whisper",
                    "asr_model": fw_model,
                    "asr_location": f"遠端 GPU ({remote_whisper_cfg.get('host', '?')})" if remote_whisper_cfg else "本機",
                    "diarize": diarize,
                    "diarize_engine": "resemblyzer + spectralcluster" if diarize else None,
                    "diarize_location": f"遠端 GPU ({remote_whisper_cfg.get('host', '?')})" if diarize and remote_whisper_cfg else ("本機" if diarize else None),
                    "num_speakers": num_speakers if num_speakers else "自動偵測",
                    "translate_model": ollama_model if need_translate and ollama_available else None,
                    "translate_server": f"{srv_label} @ {host}:{port}" if need_translate and ollama_available else None,
                    "input_format": os.path.splitext(orig_fpath)[1].lstrip(".").lower(),
                    "input_file": os.path.basename(orig_fpath),
                    "summary_model": summary_model,
                    "summary_server": f"{srv_label} @ {host}:{port}",
                }
                # 從逐字稿計算實際講者數
                if diarize:
                    try:
                        with open(lp, "r", encoding="utf-8") as _lf:
                            _spk_set = set()
                            for _ll in _lf:
                                _sm = re.search(r'\[Speaker (\d+)\]', _ll)
                                if _sm:
                                    _spk_set.add(int(_sm.group(1)))
                            if len(_spk_set) >= 2:
                                _meta["detected_speakers"] = len(_spk_set)
                    except Exception:
                        pass
                try:
                    out_path, _, html_path = summarize_log_file(lp, summary_model, host, port,
                                                                  server_type=server_type,
                                                                  topic=meeting_topic,
                                                                  metadata=_meta,
                                                                  summary_mode=summary_mode,
                                                                  audio_path=audio_in_session)
                    if out_path:
                        if html_path:
                            html_to_open.append(html_path)
                        t_summary_elapsed = time.monotonic() - t_summary_start
                        s_min, s_sec = divmod(int(t_summary_elapsed), 60)
                        s_str = f"{s_min}m{s_sec:02d}s" if s_min else f"{t_summary_elapsed:.1f}s"
                        _save_labels = {"both": "含重點摘要 + 校正逐字稿", "summary": "重點摘要", "transcript": "校正逐字稿"}
                        _save_label = _save_labels.get(summary_mode, "含重點摘要 + 校正逐字稿")
                        print(f"\n{C_DIM}{'═' * 60}{RESET}")
                        print(f"  {C_OK}{BOLD}摘要已儲存（{_save_label}）{RESET} {C_DIM}[{s_str}]{RESET}")
                        print(f"  {C_WHITE}{out_path}{RESET}")
                        print(f"  {C_WHITE}{html_path}{RESET}")
                        print(f"{C_DIM}{'═' * 60}{RESET}")
                except Exception as e:
                    print(f"  {C_HIGHLIGHT}[錯誤] 摘要失敗: {e}{RESET}")

        # 所有處理完成後一起開啟 HTML + 子目錄
        for hp in html_to_open:
            subprocess.Popen(["open", hp])
        # 開啟每個 session 子目錄（Finder）
        opened_dirs = set()
        for _, _, sess_dir in log_paths:
            if sess_dir and sess_dir not in opened_dirs:
                opened_dirs.add(sess_dir)
                subprocess.Popen(["open", sess_dir])

        if not log_paths:
            print(f"\n{C_HIGHLIGHT}沒有成功處理的檔案{RESET}")
            sys.exit(1)

        print(f"\n{C_HIGHLIGHT}按 ESC 鍵退出{RESET}", flush=True)
        _wait_for_esc()
        sys.exit(0)

    # --summarize 批次摘要模式（不需 ASR 引擎）
    if args.summarize is not None:
        if not args.summarize:
            print(f"{C_HIGHLIGHT}[錯誤] --summarize 需要指定記錄檔，例如: ./start.sh --summarize log.txt{RESET}",
                  file=sys.stderr)
            sys.exit(1)
        host, port = _resolve_ollama_host(args)
        model = args.summary_model

        print(f"\n\n{C_TITLE}{BOLD}▎ 批次摘要模式{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_DIM}摘要模型: {model} ({host}:{port}){RESET}")

        print(f"  {C_DIM}正在連接 LLM 伺服器...{RESET}", end=" ", flush=True)
        server_type = _detect_llm_server(host, port)
        if server_type:
            srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
            remote_models = _llm_list_models(host, port, server_type)
            remote_set = set(remote_models)
            if model not in remote_set:
                print(f"\n{C_HIGHLIGHT}[警告] 模型 {model} 不在伺服器上，可用模型: {', '.join(sorted(remote_set))}{RESET}")
            else:
                print(f"{C_OK}{BOLD}{srv_label}（{len(remote_models)} 個模型）{RESET}")
        else:
            print(f"\n{C_HIGHLIGHT}[錯誤] 無法連接 LLM 伺服器 ({host}:{port}){RESET}",
                  file=sys.stderr)
            sys.exit(1)

        try:
            t_batch_start = time.monotonic()
            # 合併所有檔案內容
            valid_files = []
            combined_transcript = ""
            for fpath in args.summarize:
                if not os.path.isfile(fpath):
                    print(f"\n  {C_HIGHLIGHT}[錯誤] 檔案不存在: {fpath}{RESET}")
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if not content:
                    print(f"\n  {C_HIGHLIGHT}[跳過] 檔案內容為空: {fpath}{RESET}")
                    continue
                valid_files.append(fpath)
                combined_transcript += content + "\n\n"

            if not valid_files:
                print(f"\n{C_HIGHLIGHT}[錯誤] 沒有有效的記錄檔{RESET}")
                sys.exit(1)

            for fpath in valid_files:
                print(f"  {C_DIM}已載入: {os.path.basename(fpath)}{RESET}")
            if len(valid_files) > 1:
                print(f"  {C_WHITE}共 {len(valid_files)} 個檔案，合併摘要{RESET}")

            # 用第一個檔案名決定摘要檔名
            first_base = os.path.basename(valid_files[0])
            if first_base.startswith("英翻中_逐字稿"):
                out_name = "英翻中_摘要_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("中翻英_逐字稿"):
                out_name = "中翻英_摘要_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("英文_逐字稿"):
                out_name = "英文_摘要_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("中文_逐字稿"):
                out_name = "中文_摘要_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            else:
                out_name = "摘要_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            os.makedirs(LOG_DIR, exist_ok=True)
            output_path = os.path.join(LOG_DIR, out_name)

            # 查詢模型 context window
            num_ctx = query_ollama_num_ctx(model, host, port, server_type=server_type)
            max_chars = _calc_chunk_max_chars(num_ctx)
            if num_ctx:
                print(f"  {C_DIM}模型 context window: {num_ctx:,} tokens → 每段上限約 {max_chars:,} 字{RESET}")

            # 啟動摘要狀態列
            combined_transcript = combined_transcript.strip()
            chunks = _split_transcript_chunks(combined_transcript, max_chars)
            print()  # 空行，與下方摘要內容做視覺區隔
            _llm_loc = "本機" if host in ("localhost", "127.0.0.1", "::1") else "伺服器"
            sbar = _SummaryStatusBar(model=model, task="準備中", location=_llm_loc).start()

            _batch_topic = getattr(args, 'topic', None)
            _batch_summary_mode = "both"  # --summarize 批次模式預設
            if len(chunks) <= 1:
                prompt = _summary_prompt(combined_transcript, topic=_batch_topic,
                                         summary_mode=_batch_summary_mode)
                sbar.set_task(f"生成摘要（單段，{len(combined_transcript)} 字）")
                summary = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                          server_type=server_type)
            else:
                segment_summaries = []
                for i, chunk in enumerate(chunks):
                    sbar.set_task(f"第 {i+1}/{len(chunks)} 段（{len(chunk)} 字）")
                    prompt = _summary_prompt(chunk, topic=_batch_topic,
                                             summary_mode=_batch_summary_mode)
                    seg = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                          server_type=server_type)
                    seg = S2TWP.convert(seg)
                    segment_summaries.append(seg)
                    print(f"  {C_OK}第 {i+1}/{len(chunks)} 段完成{RESET}", flush=True)

                sbar.set_task(f"合併 {len(chunks)} 段摘要")
                combined = "\n\n---\n\n".join(
                    f"### 第 {i+1} 段\n{s}" for i, s in enumerate(segment_summaries)
                )
                merge_prompt = SUMMARY_MERGE_PROMPT_TEMPLATE.format(summaries=combined)
                if _batch_topic:
                    merge_prompt = merge_prompt.replace(
                        "以下是各段摘要：",
                        f"- 本次會議主題：{_batch_topic}，請根據此主題的領域知識整理重點\n\n以下是各段摘要：",
                    )
                merged_summary = call_ollama_raw(merge_prompt, model, host, port, spinner=sbar, live_output=True,
                                                 server_type=server_type)

                # 組合完整輸出：合併摘要在前，各段校正逐字稿在後
                summary = merged_summary + "\n\n"
                for i, seg in enumerate(segment_summaries):
                    marker = "## 校正逐字稿"
                    idx = seg.find(marker)
                    if idx >= 0:
                        transcript_part = seg[idx:].strip()
                    else:
                        transcript_part = seg.strip()
                    summary += f"--- 第 {i+1}/{len(segment_summaries)} 段 ---\n{transcript_part}\n\n"

            sbar._task = "完成"
            sbar.freeze()

            # 偵測 LLM 是否跳過重點摘要
            if _batch_summary_mode == "both" and "## 重點摘要" not in summary:
                print(f"\n  {C_HIGHLIGHT}[偵測] LLM 回覆缺少重點摘要段落，自動補發摘要請求...{RESET}")
                _retry_input = summary
                _marker = "## 校正逐字稿"
                _idx = _retry_input.find(_marker)
                if _idx >= 0:
                    _retry_input = _retry_input[_idx + len(_marker):].strip()
                if len(_retry_input) > max_chars:
                    _retry_input = _retry_input[:max_chars]
                _retry_topic = f"（主題：{_batch_topic}）" if _batch_topic else ""
                _retry_prompt = f"""\
你是專業的會議記錄整理員。請根據以下校正後的逐字稿，列出 5-10 個重點摘要{_retry_topic}，每個重點用一句話概述。

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

規則：
- 全部使用台灣繁體中文
- 使用台灣用語（軟體、網路、記憶體、程式、伺服器等）
- 嚴禁加入原文沒有的內容

以下是逐字稿：
---
{_retry_input}
---"""
                sbar_retry = _SummaryStatusBar(model=model, task="補產重點摘要", location=_llm_loc).start()
                _retry_result = call_ollama_raw(_retry_prompt, model, host, port, spinner=sbar_retry,
                                                live_output=True, server_type=server_type)
                sbar_retry.stop()
                _retry_result = S2TWP.convert(_retry_result)
                summary = _retry_result.rstrip() + "\n\n" + summary.lstrip()
                print(f"  {C_OK}重點摘要已補上{RESET}")

            summary = S2TWP.convert(summary)

            # 組裝 metadata（批次摘要只有摘要模型資訊）
            _batch_meta = {
                "summary_model": model,
                "summary_server": f"{srv_label} @ {host}:{port}",
                "input_file": ", ".join(os.path.basename(f) for f in valid_files),
            }
            meta_header = _build_metadata_header(_batch_meta)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(meta_header + summary + "\n")

            # 同步產生 HTML 摘要
            html_path = os.path.splitext(output_path)[0] + ".html"
            source_name = os.path.basename(valid_files[0]) if valid_files else ""
            transcript_path = valid_files[0] if valid_files else ""
            _summary_to_html(summary, html_path, source_name,
                             summary_txt_path=output_path, transcript_txt_path=transcript_path,
                             metadata=_batch_meta)
            subprocess.Popen(["open", html_path])

            t_batch_elapsed = time.monotonic() - t_batch_start
            b_min, b_sec = divmod(int(t_batch_elapsed), 60)
            b_str = f"{b_min}m{b_sec:02d}s" if b_min else f"{t_batch_elapsed:.1f}s"
            print(f"\n{C_DIM}{'═' * 60}{RESET}")
            print(f"  {C_OK}{BOLD}摘要已儲存（含重點摘要 + 校正逐字稿）{RESET} {C_DIM}[{b_str}]{RESET}")
            print(f"  {C_WHITE}{output_path}{RESET}")
            print(f"  {C_WHITE}{html_path}{RESET}")
            print(f"{C_DIM}{'═' * 60}{RESET}")
            open_file_in_editor(output_path)
            print(f"\n{C_HIGHLIGHT}按 ESC 鍵退出{RESET}", flush=True)
            _wait_for_esc()
            sbar.stop()

        except KeyboardInterrupt:
            try:
                sbar.stop()
            except Exception:
                pass
            print(f"\n\n{C_DIM}已中止摘要。{RESET}")

        sys.exit(0)

    if args.list_devices:
        if _MOONSHINE_AVAILABLE:
            print(f"\n\n{C_TITLE}{BOLD}▎ sounddevice 音訊裝置{RESET}")
            list_audio_devices_sd()
        # whisper-stream 裝置
        model_path_exists = os.path.isfile(WHISPER_STREAM)
        if model_path_exists:
            _, model_path = resolve_model("large-v3-turbo")
            print(f"\n\n{C_TITLE}{BOLD}▎ whisper-stream SDL2 音訊裝置{RESET}")
            list_audio_devices(model_path)
        sys.exit(0)

    if cli_mode:
        # CLI 模式：用參數 + 預設值，跳過選單
        mode = args.mode or "en2zh"

        # 純錄音模式：跳過 ASR，直接錄音
        if mode == "record":
            rec_id, rec_name, rec_label = _auto_detect_rec_device()
            if rec_id is None:
                print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
                sys.exit(1)
            print(f"{C_OK}錄音裝置: [{rec_id}] {rec_name}（{rec_label}）{RESET}")
            _cli_kw = dict(mode="record", device=rec_id, topic=args.topic)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            run_record_only(rec_id, topic=args.topic)
            sys.exit(0)

        # 決定 ASR 引擎
        if args.asr:
            asr_engine = args.asr
        elif args.model:
            # -m 指定的是 Whisper 模型，隱含使用 Whisper
            asr_engine = "whisper"
        elif mode in ("en2zh", "en") and _MOONSHINE_AVAILABLE:
            # 沒指定 --asr 也沒指定 -m，讓使用者選
            asr_engine = select_asr_engine()
        else:
            asr_engine = "whisper"
        # 中文模式強制 whisper
        if mode in ("zh", "zh2en"):
            asr_engine = "whisper"

        # 遠端 GPU Whisper 即時模式（非 Moonshine、非 --local-asr）
        use_remote_cli = (REMOTE_WHISPER_CONFIG and not args.local_asr
                          and asr_engine != "moonshine")
        if use_remote_cli:
            # 遠端模式：不需本機 whisper-stream
            if mode in ("zh", "zh2en"):
                default_model = "large-v3"
            else:
                default_model = "large-v3-turbo"
            model_name = args.model or default_model

            if args.device is not None:
                capture_id = args.device
            else:
                capture_id = auto_select_device_sd()

            translator = None
            meeting_topic = args.topic
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            if mode in ("en2zh", "zh2en"):
                ollama_model = None
                if args.engine or args.ollama_model or args.ollama_host:
                    engine = args.engine or "llm"
                else:
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port)
                    if engine == "llm":
                        ollama_model = _sel_model
                        if _sel_host: host = _sel_host
                        if _sel_port: port = _sel_port
                        if _sel_srv: srv_type = _sel_srv
                if engine == "llm":
                    if not ollama_model:
                        ollama_model = args.ollama_model or _select_llm_model(host, port, srv_type)
                    translator = OllamaTranslator(ollama_model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                else:
                    if mode == "zh2en":
                        print(f"[錯誤] 中翻英模式不支援 Argos 離線翻譯，請使用 LLM 伺服器", file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]

            rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            print(f"{C_DIM}模式: {mode_label} | ASR: Whisper ({model_name}) @ 遠端 GPU（{rw_host}） | "
                  f"裝置: {capture_id} | 翻譯: {engine}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            _cli_kw = dict(mode=mode, model=model_name, device=args.device,
                           scene=args.scene, topic=meeting_topic,
                           llm_model=ollama_model if mode in ("en2zh", "zh2en") and engine == "llm" else None,
                           engine=engine if mode in ("en2zh", "zh2en") else None,
                           llm_host=f"{host}:{port}" if mode in ("en2zh", "zh2en") and engine == "llm" else None,
                           record=args.record, rec_device=args.rec_device)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream_remote(capture_id, translator, model_name, REMOTE_WHISPER_CONFIG,
                              mode, length_ms, step_ms,
                              record=args.record, rec_device=args.rec_device,
                              force_restart=args.restart_server,
                              meeting_topic=meeting_topic)
        elif asr_engine == "moonshine":
            check_dependencies(asr_engine)
            # Moonshine 模式
            ms_model_name = args.moonshine_model or "medium"

            if args.device is not None:
                capture_id = args.device
            else:
                capture_id = auto_select_device_sd()

            translator = None
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            meeting_topic = args.topic
            if mode == "en2zh":
                ollama_model = None
                if args.engine or args.ollama_model or args.ollama_host:
                    engine = args.engine or "llm"
                else:
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port)
                    if engine == "llm":
                        ollama_model = _sel_model
                        if _sel_host: host = _sel_host
                        if _sel_port: port = _sel_port
                        if _sel_srv: srv_type = _sel_srv
                if engine == "llm":
                    if not ollama_model:
                        ollama_model = args.ollama_model or _select_llm_model(host, port, srv_type)
                    translator = OllamaTranslator(ollama_model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                else:
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            s_host, s_port = host, port

            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            print(f"{C_DIM}模式: {mode_label} | ASR: Moonshine ({ms_model_name}) | "
                  f"裝置: {capture_id} | 翻譯: {engine if mode == 'en2zh' else '無'}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            _cli_kw = dict(mode=mode, asr="moonshine", moonshine_model=ms_model_name,
                           device=args.device, topic=meeting_topic,
                           llm_model=ollama_model if mode == "en2zh" and engine == "llm" else None,
                           engine=engine if mode == "en2zh" else None,
                           llm_host=f"{host}:{port}" if mode == "en2zh" and engine == "llm" else None,
                           record=args.record, rec_device=args.rec_device)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream_moonshine(capture_id, translator, ms_model_name, mode,
                                 record=args.record, rec_device=args.rec_device,
                                 meeting_topic=meeting_topic)
        else:
            check_dependencies(asr_engine)
            # Whisper 本機模式（原有邏輯）
            if mode in ("zh", "zh2en"):
                default_model = "large-v3"
            else:
                default_model = "large-v3-turbo"
            model_name = args.model or default_model
            if mode in ("zh", "zh2en") and model_name.endswith(".en"):
                print(f"[錯誤] {mode} 模式不支援 {model_name}（僅英文模型），請用 large-v3 或 large-v3-turbo",
                      file=sys.stderr)
                sys.exit(1)
            model_name, model_path = resolve_model(model_name)

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]

            if args.device is not None:
                capture_id = args.device
            else:
                capture_id = auto_select_device(model_path)

            translator = None
            meeting_topic = args.topic
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            if mode in ("en2zh", "zh2en"):
                ollama_model = None
                if args.engine or args.ollama_model or args.ollama_host:
                    engine = args.engine or "llm"
                else:
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port)
                    if engine == "llm":
                        ollama_model = _sel_model
                        if _sel_host: host = _sel_host
                        if _sel_port: port = _sel_port
                        if _sel_srv: srv_type = _sel_srv
                if engine == "llm":
                    if not ollama_model:
                        ollama_model = args.ollama_model or _select_llm_model(host, port, srv_type)
                    translator = OllamaTranslator(ollama_model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                else:
                    if mode == "zh2en":
                        print(f"[錯誤] 中翻英模式不支援 Argos 離線翻譯，請使用 LLM 伺服器", file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            s_host, s_port = host, port

            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            print(f"{C_DIM}模式: {mode_label} | ASR: Whisper ({model_name}) | 場景: {scene_key} | "
                  f"裝置: {capture_id} | 翻譯: {engine}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            _cli_kw = dict(mode=mode, model=model_name, scene=args.scene,
                           device=args.device, topic=meeting_topic,
                           llm_model=ollama_model if mode in ("en2zh", "zh2en") and engine == "llm" else None,
                           engine=engine if mode in ("en2zh", "zh2en") else None,
                           llm_host=f"{host}:{port}" if mode in ("en2zh", "zh2en") and engine == "llm" else None,
                           record=args.record, rec_device=args.rec_device)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream(capture_id, translator, model_name, model_path, length_ms, step_ms, mode,
                       record=args.record, rec_device=args.rec_device,
                       meeting_topic=meeting_topic)
    else:
        # 互動式選單
        mode = select_mode()

        # 純錄音模式：跳過 ASR/翻譯/模型，選擇錄音來源
        if mode == "record":
            rec_id, rec_name, rec_label = _ask_record_source()
            print(f"  {C_OK}錄音裝置: [{rec_id}] {rec_name}（{rec_label}）{RESET}")
            meeting_topic = _ask_topic(record_only=True)
            _cli_kw = dict(mode="record", device=rec_id, topic=meeting_topic)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            run_record_only(rec_id, topic=meeting_topic)
            sys.exit(0)

        # 辨識位置（遠端 GPU / 本機），僅在有設定時顯示
        use_remote_asr = False
        if REMOTE_WHISPER_CONFIG:
            asr_location = select_asr_location()
            use_remote_asr = (asr_location == "remote")

        if use_remote_asr:
            # ── 遠端 GPU 路徑：固定 Whisper，跳過引擎/場景選擇 ──

            # 翻譯引擎（翻譯模式才問）
            translator = None
            meeting_topic = None
            if mode in ("en2zh", "zh2en"):
                engine, model, host, port, srv_type = select_translator()
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                else:
                    if mode == "zh2en":
                        print(f"{C_HIGHLIGHT}[錯誤] 中翻英模式不支援 Argos 離線翻譯，請使用 LLM 伺服器{RESET}",
                              file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()

            # 錄音
            record, rec_device = _ask_record()

            # 遠端 Whisper 模型選擇（帶快取標籤）
            r_model_name = select_whisper_model_remote(mode)

            # 音訊裝置（PortAudio，不是 SDL2）
            capture_id = list_audio_devices_sd()

            _cli_kw = dict(mode=mode, model=r_model_name, device=capture_id,
                           topic=meeting_topic,
                           record=record, rec_device=rec_device,
                           engine=engine if mode in ("en2zh", "zh2en") else None,
                           llm_model=model if mode in ("en2zh", "zh2en") and engine == "llm" else None,
                           llm_host=f"{host}:{port}" if mode in ("en2zh", "zh2en") and engine == "llm" else None)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            run_stream_remote(capture_id, translator, r_model_name, REMOTE_WHISPER_CONFIG,
                              mode, record=record, rec_device=rec_device,
                              force_restart=args.restart_server,
                              meeting_topic=meeting_topic)
        else:
            # ── 本機路徑：既有流程 ──

            # 英文模式：選擇 ASR 引擎
            if mode in ("en2zh", "en"):
                asr_engine = select_asr_engine()
            else:
                asr_engine = "whisper"

            check_dependencies(asr_engine)

            # 翻譯引擎（翻譯模式才問）
            translator = None
            meeting_topic = None
            s_host, s_port = OLLAMA_HOST, OLLAMA_PORT
            s_server_type = None
            if asr_engine == "moonshine" and mode == "en2zh":
                engine, model, host, port, srv_type = select_translator()
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                    s_host, s_port, s_server_type = host, port, srv_type
                else:
                    translator = ArgosTranslator()
            elif asr_engine == "whisper" and mode in ("en2zh", "zh2en"):
                engine, model, host, port, srv_type = select_translator()
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                    s_host, s_port, s_server_type = host, port, srv_type
                else:
                    if mode == "zh2en":
                        print(f"{C_HIGHLIGHT}[錯誤] 中翻英模式不支援 Argos 離線翻譯，請使用 LLM 伺服器{RESET}",
                              file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()

            # 詢問是否錄音（自動偵測錄音裝置）
            record, rec_device = _ask_record()

            # ASR 模型 + 場景 + 自動偵測 ASR 裝置
            if asr_engine == "moonshine":
                ms_model_name = select_moonshine_model()
                capture_id = list_audio_devices_sd()
                _cli_kw = dict(mode=mode, asr="moonshine", moonshine_model=ms_model_name,
                               device=capture_id, topic=meeting_topic,
                               record=record, rec_device=rec_device,
                               engine=engine if mode == "en2zh" and engine else None,
                               llm_model=model if mode == "en2zh" and engine == "llm" else None,
                               llm_host=f"{host}:{port}" if mode == "en2zh" and engine == "llm" else None)
                if not _confirm_start(_build_cli_command(**_cli_kw)):
                    sys.exit(0)
                run_stream_moonshine(capture_id, translator, ms_model_name, mode,
                                     record=record, rec_device=rec_device,
                                     meeting_topic=meeting_topic)
            else:
                model_name, model_path = select_whisper_model(mode)
                length_ms, step_ms = select_scene()
                capture_id = list_audio_devices(model_path)
                _need_llm = mode in ("en2zh", "zh2en") and engine == "llm"
                _cli_kw = dict(mode=mode, model=model_name,
                               device=capture_id, topic=meeting_topic,
                               record=record, rec_device=rec_device,
                               engine=engine if mode in ("en2zh", "zh2en") else None,
                               llm_model=model if _need_llm else None,
                               llm_host=f"{host}:{port}" if _need_llm else None)
                if not _confirm_start(_build_cli_command(**_cli_kw)):
                    sys.exit(0)
                run_stream(capture_id, translator, model_name, model_path, length_ms, step_ms, mode,
                           record=record, rec_device=rec_device,
                           meeting_topic=meeting_topic)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C_DIM}已停止。{RESET}")
        sys.exit(0)
