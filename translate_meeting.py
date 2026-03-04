#!/usr/bin/env python3
"""
即時英文語音轉繁體中文字幕
透過 BlackHole 虛擬音訊裝置捕捉音訊，
使用 whisper.cpp stream 即時轉錄，再翻譯成繁體中文。

Author: Jason Cheng (Jason Tools)
"""

import argparse
import atexit
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

# 避免 OpenMP 重複載入衝突
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
# 抑制 Intel MKL SSE4.2 棄用警告（Apple Silicon + Rosetta 會觸發）
os.environ["MKL_SERVICE_FORCE_INTEL"] = "1"

import json
import urllib.request

import ctranslate2
import opencc
import sentencepiece

# 簡體→台灣繁體轉換器（Argos 離線翻譯輸出為簡體，需轉換；LLM 偶爾輸出簡體也適用）
S2TWP = opencc.OpenCC("s2twp")

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

# LLM 伺服器設定（預設值，可被 config.json 覆蓋）
OLLAMA_DEFAULT_HOST = "192.168.1.40"
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
OLLAMA_HOST = _config.get("ollama_host", OLLAMA_DEFAULT_HOST)
OLLAMA_PORT = _config.get("ollama_port", OLLAMA_DEFAULT_PORT)

# 內建翻譯模型（作者篩選推薦）
_BUILTIN_TRANSLATE_MODELS = [
    ("qwen2.5:14b", "品質好，速度快（推薦）"),
    ("phi4:14b", "Microsoft，品質最好"),
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
]

# 可用的 whisper 模型（由小到大）
WHISPER_MODELS = [
    ("base.en", "ggml-base.en.bin", "最快，準確度一般"),
    ("small.en", "ggml-small.en.bin", "快，準確度好"),
    ("large-v3-turbo", "ggml-large-v3-turbo.bin", "快，準確度很好（推薦）"),
    ("medium.en", "ggml-medium.en.bin", "較慢，準確度很好"),
    ("large-v3", "ggml-large-v3.bin", "最慢，中文品質最好"),
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

APP_VERSION = "1.7.8"

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
    ("gpt-oss:20b", "速度快，品質好"),
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
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的段落文字。合併斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。

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
- 不要加入原文沒有的內容
- 不要逐行標註時間戳記或逐行對照英中文，直接輸出流暢的中文段落

以下是逐字稿：
---
{transcript}
---
"""

SUMMARY_PROMPT_DIARIZE_TEMPLATE = """\
你是專業的會議記錄整理員。請根據以下含有講者標記的逐字稿，完成兩件事：

1. **重點摘要**：列出 5-10 個重點，每個重點用一句話概述。
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的對話文字。合併同一位講者的連續斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

## 校正逐字稿

Speaker 1：整理後的這段話內容。

Speaker 2：整理後的這段話內容。

Speaker 1：整理後的這段話內容。

...

規則：
- 逐字稿中 [Speaker N] 標記的是不同的講者，校正後必須保留講者標記，格式為「Speaker N：內容」
- 同一位講者的連續短句要合併成完整的段落，不要逐句列出
- 不同講者之間換行分隔
- 逐字稿中 [EN] 標記的是英文原文語音辨識結果，[中] 標記的是中文翻譯。校正時請以中文翻譯為主，參考英文原文修正翻譯錯誤
- 全部使用台灣繁體中文
- 使用台灣用語（軟體、網路、記憶體、程式、伺服器等）
- 專有名詞維持英文原文
- 不要加入原文沒有的內容
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

def _summary_prompt(transcript):
    """依據逐字稿內容選擇摘要 prompt（有 Speaker 標籤用對話版）"""
    if "[Speaker " in transcript:
        return SUMMARY_PROMPT_DIARIZE_TEMPLATE.format(transcript=transcript)
    return SUMMARY_PROMPT_TEMPLATE.format(transcript=transcript)


# 場景名稱對照（CLI 用）
SCENE_MAP = {"meeting": 0, "training": 1, "subtitle": 2}
MODE_MAP = {key: i for i, (key, _, _) in enumerate(MODE_PRESETS)}
APP_NAME = f"jt-live-whisper v{APP_VERSION} - 即時英翻中字幕系統"
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

    print(f"\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
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

    print(f"\n{C_TITLE}{BOLD}▎ 語音辨識模型{RESET}")
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


def select_scene():
    """讓用戶選擇使用場景"""
    if len(SCENE_PRESETS) == 1:
        s = SCENE_PRESETS[0]
        print(f"使用場景: {s[0]} ({s[3]})\n")
        return s[1], s[2]

    default_idx = 1  # 預設：教育訓練

    print(f"\n{C_TITLE}{BOLD}▎ 使用場景{RESET}")
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

    print(f"\n{C_TITLE}{BOLD}▎ 語音辨識引擎{RESET}")
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


def select_moonshine_model():
    """讓使用者選擇 Moonshine 串流模型"""
    default_idx = 0  # medium

    print(f"\n{C_TITLE}{BOLD}▎ Moonshine 語音模型{RESET}")
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

    print(f"\n{C_TITLE}{BOLD}▎ 音訊裝置{RESET}")
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
            stream=False, timeout=30,
        )

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

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        try:
            result = self._call_ollama(text, self.context)
            # 只取第一行，避免 model 輸出多餘解釋
            result = result.split("\n")[0].strip()
            if self.direction == "en2zh":
                # 簡體→台灣繁體轉換
                result = S2TWP.convert(result)
            # 過濾非中英文的回應（模型偶爾會輸出俄文等）
            if self._contains_bad_chars(result):
                # 重試一次
                result = self._call_ollama(text, [])
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
                return [m["id"] for m in data.get("data", [])]
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
                  timeout=30, spinner=None, live_output=False):
    """統一 LLM 生成介面，支援 Ollama 原生 API 和 OpenAI 相容 API"""
    write_lock = getattr(spinner, '_lock', None)

    if server_type == "openai":
        url = f"http://{host}:{port}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
    else:
        # 預設 Ollama
        url = f"http://{host}:{port}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }

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


def _check_llm_server(host, port):
    """偵測 LLM 伺服器類型並回傳可用模型列表
    回傳 (server_type, model_list)"""
    server_type = _detect_llm_server(host, port)
    if not server_type:
        return None, []
    all_models = _llm_list_models(host, port, server_type)
    if server_type == "ollama":
        # Ollama：只回傳 OLLAMA_MODELS 中有的
        remote_set = set(all_models)
        filtered = [name for name, _ in OLLAMA_MODELS if name in remote_set]
        return server_type, filtered
    else:
        # OpenAI 相容：回傳伺服器上所有模型
        return server_type, all_models


def select_translator():
    """讓用戶選擇翻譯引擎和模型，回傳 (engine, model, host, port, server_type)"""
    host = OLLAMA_HOST
    port = OLLAMA_PORT

    print(f"\n{C_TITLE}{BOLD}▎ 翻譯引擎{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")

    # 先自動偵測預設 LLM 伺服器
    print(f"  {C_DIM}正在偵測 LLM 伺服器 ({host}:{port})...{RESET}", end=" ", flush=True)
    server_type, available_models = _check_llm_server(host, port)

    if not server_type:
        # 預設位址連不上，問使用者要不要輸入其他位址
        print(f"{C_HIGHLIGHT}未偵測到{RESET}")
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
    if host != _config.get("ollama_host") or port != _config.get("ollama_port"):
        _config["ollama_host"] = host
        _config["ollama_port"] = port
        save_config(_config)

    # 建立選項列表
    options = []
    if server_type == "ollama":
        for model_name in available_models:
            desc = next((d for n, d in OLLAMA_MODELS if n == model_name), "")
            options.append((f"Ollama {model_name}", desc, "ollama", model_name))
    else:
        for model_name in available_models:
            options.append((model_name, "", "ollama", model_name))
    options.append(("Argos 本機離線", "品質普通，免網路", "argos", None))

    # 計算顯示寬度以對齊欄位
    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    col = max(_dw(label) for label, *_ in options) + 2

    for i, (label, desc, engine, model) in enumerate(options):
        padded = label + ' ' * (col - _dw(label))
        if i == 0:
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

    idx = 0
    if user_input:
        try:
            idx = int(user_input)
            if not (0 <= idx < len(options)):
                idx = 0
        except ValueError:
            idx = 0

    label, desc, engine, model = options[idx]
    print(f"  {C_OK}→ {label}{RESET}\n")
    if engine == "ollama":
        return engine, model, host, port, server_type
    else:
        return engine, None, None, None, None


def _input_interactive_menu(args):
    """--input 互動選單：選擇模式、講者辨識、摘要"""

    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    try:
        # 顯示輸入檔案資訊
        print(f"\n{C_TITLE}{BOLD}▎ 離線處理音訊檔{RESET}")
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

        print(f"\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(name) for _, name, _ in MODE_PRESETS) + 2
        for i, (key, name, desc) in enumerate(MODE_PRESETS):
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
                if not (0 <= idx < len(MODE_PRESETS)):
                    idx = default_mode
            except ValueError:
                idx = default_mode
        else:
            idx = default_mode
        mode_key, mode_name, mode_desc = MODE_PRESETS[idx]
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

        print(f"\n{C_TITLE}{BOLD}▎ 辨識模型{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(len(name) for name, _ in available_models) + 2
        for i, (name, desc) in enumerate(available_models):
            padded = name + ' ' * (col - len(name))
            if i == default_fw:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
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

        # ── 第三步：LLM 伺服器 + 翻譯模型（僅翻譯模式）──
        ollama_model = None
        ollama_host = OLLAMA_HOST
        ollama_port = OLLAMA_PORT
        ollama_asked = False
        llm_server_type = None

        if need_translate:
            # LLM 伺服器
            default_addr = f"{ollama_host}:{ollama_port}"
            print(f"\n{C_TITLE}{BOLD}▎ LLM 伺服器{RESET}")
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
            ollama_asked = True

            # 偵測伺服器類型
            print(f"  {C_DIM}正在偵測 LLM 伺服器...{RESET}", end=" ", flush=True)
            llm_server_type, llm_models = _check_llm_server(ollama_host, ollama_port)
            if llm_server_type:
                srv_label = "Ollama" if llm_server_type == "ollama" else "OpenAI 相容"
                print(f"{C_OK}✓ {srv_label} @ {ollama_host}:{ollama_port}（{len(llm_models)} 個模型）{RESET}")
            else:
                print(f"{C_HIGHLIGHT}未偵測到 LLM 伺服器（{ollama_host}:{ollama_port}）{RESET}")
                print(f"  {C_HIGHLIGHT}⚠ 翻譯功能需要 LLM 伺服器，請確認伺服器已啟動{RESET}")

            # 翻譯模型
            if llm_server_type == "ollama":
                translate_models = [(n, d) for n, d in OLLAMA_MODELS]
            elif llm_server_type == "openai":
                translate_models = [(m, "") for m in llm_models]
            else:
                translate_models = [(n, d) for n, d in OLLAMA_MODELS]
                llm_server_type = "ollama"  # 預設假設 Ollama，實際連線時再偵測

            default_ollama = 0
            print(f"\n{C_TITLE}{BOLD}▎ 翻譯模型{RESET}")
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
        default_diarize = 1 if cli_diarize else 0
        diarize_options = [
            ("不辨識", ""),
            ("自動偵測講者數", ""),
            ("指定講者數", ""),
        ]

        print(f"\n{C_TITLE}{BOLD}▎ 講者辨識{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(_dw(l) for l, _ in diarize_options) + 2
        for i, (label, _) in enumerate(diarize_options):
            padded = label + ' ' * (col - _dw(label))
            if i == default_diarize:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET}")
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
            ("不摘要", ""),
            ("產生摘要與校正逐字稿", ""),
        ]

        print(f"\n{C_TITLE}{BOLD}▎ 摘要與逐字稿校正{RESET}")
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
        do_summarize = s_idx == 1

        # 選了摘要 → 先確認 LLM 伺服器（若翻譯步驟未問過）→ 選摘要模型
        summary_model = SUMMARY_DEFAULT_MODEL
        if do_summarize:
            if not ollama_asked:
                default_addr = f"{ollama_host}:{ollama_port}"
                print(f"\n{C_TITLE}{BOLD}▎ LLM 伺服器{RESET}")
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

            # 摘要模型
            if llm_server_type == "ollama":
                summary_models_list = [(n, d) for n, d in SUMMARY_MODELS]
            elif llm_server_type == "openai":
                summary_models_list = [(m, "") for m in _llm_list_models(ollama_host, ollama_port, llm_server_type)]
            else:
                summary_models_list = [(n, d) for n, d in SUMMARY_MODELS]
                llm_server_type = "ollama"  # 預設假設 Ollama，實際連線時再偵測

            default_sm = 0
            print(f"\n{C_TITLE}{BOLD}▎ 摘要模型{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            col = max(len(name) for name, _ in summary_models_list) + 2
            for i, (name, desc) in enumerate(summary_models_list):
                padded = name + ' ' * (col - len(name))
                if i == default_sm:
                    print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                else:
                    print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
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

        # 記住 LLM 伺服器位址（如果有改）
        if ollama_host != OLLAMA_HOST or ollama_port != OLLAMA_PORT:
            _config["ollama_host"] = ollama_host
            _config["ollama_port"] = ollama_port
            save_config(_config)

        # ── 確認摘要 ──
        diarize_desc = "關閉"
        if d_idx == 1:
            diarize_desc = "自動偵測"
        elif d_idx == 2:
            diarize_desc = f"指定 {num_speakers} 人"

        print(f"\n{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_OK}→ {mode_name}{RESET}  {C_DIM}辨識: {fw_model}{RESET}")
        if ollama_model:
            print(f"  {C_OK}  翻譯: {ollama_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        print(f"  {C_OK}  講者辨識: {diarize_desc}{RESET}")
        if do_summarize:
            print(f"  {C_OK}  摘要: {summary_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        print()

        return (mode_key, fw_model, ollama_model, summary_model,
                ollama_host, ollama_port, diarize, num_speakers, do_summarize,
                llm_server_type)

    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def run_stream(capture_id: int, translator, model_name: str, model_path: str,
               length_ms: int = 5000, step_ms: int = 3000, mode: str = "en2zh",
               summary_model: str = SUMMARY_DEFAULT_MODEL,
               summary_host: str = OLLAMA_DEFAULT_HOST,
               summary_port: int = OLLAMA_DEFAULT_PORT,
               summary_server_type: str = "ollama",
               record: bool = False, rec_device: int = None):
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
    log_prefixes = {"en2zh": "en2zh_translation", "zh2en": "zh2en_translation", "en": "en_transcribe", "zh": "zh_transcribe"}
    log_prefix = log_prefixes.get(mode, "translation")
    log_filename = datetime.now().strftime(f"{log_prefix}_%Y%m%d_%H%M%S.txt")
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
        recorder = _AudioRecorder(rec_sr, rec_ch)

        def rec_callback(indata, frames, time_info, status):
            recorder.write_raw(indata)

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
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder:
        print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_DIM}會議主題: {translator.meeting_topic}{RESET}")
    print(f"  {C_DIM}按 Ctrl+C 停止 | Ctrl+S 停止並生成摘要{RESET}")
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

    # Ctrl+S 支援
    ctrl_s_pressed = threading.Event()
    stop_keypress = threading.Event()

    # 設定 signal handler
    def signal_handler(signum, frame):
        clear_status_bar()
        restore_terminal()
        stop_keypress.set()
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
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n", flush=True)

    # 啟用 Ctrl+S 偵測（在互動選單結束後才改 terminal，不影響 input()）
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_keypress, ctrl_s_pressed),
        daemon=True,
    )
    kp_thread.start()

    # 設定底部固定狀態列（快捷鍵提示 + 即時資訊）
    setup_status_bar(mode)
    signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # 非同步翻譯：英文立刻顯示，中文在背景翻完再補上
    print_lock = threading.Lock()

    def translate_and_print(src_text, log_path):
        """背景執行緒：翻譯並印出結果"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if elapsed < 1.0:
                speed_badge = C_BADGE_FAST
            elif elapsed < 3.0:
                speed_badge = C_BADGE_NORMAL
            else:
                speed_badge = C_BADGE_SLOW
            if mode == "zh2en":
                dst_color, dst_label = C_EN, "EN"
                src_label = "中"
            else:
                dst_color, dst_label = C_ZH, "中"
                src_label = "EN"
            with print_lock:
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            # 寫入記錄檔
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")

    # 持續讀取輸出檔案的新內容
    last_size = 0
    last_translated = ""
    buffer = ""
    _loop_tick = 0

    while proc.poll() is None:
        try:
            # 檢查 Ctrl+S
            if ctrl_s_pressed.is_set():
                break

            # 每約 1 秒更新狀態列時間
            _loop_tick += 1
            if _loop_tick >= 10 and _status_bar_active:
                _loop_tick = 0
                refresh_status_bar()

            if not os.path.exists(output_file):
                time.sleep(0.1)
                continue

            current_size = os.path.getsize(output_file)
            if current_size > last_size:
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
                            # 英翻中：立刻顯示英文原文，背景翻譯
                            with print_lock:
                                print(f"{C_EN}[EN] {line}{RESET}", flush=True)
                            last_translated = line
                            t = threading.Thread(
                                target=translate_and_print,
                                args=(line, log_path),
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
                        # 立刻顯示中文原文
                        with print_lock:
                            print(f"{C_ZH}[中] {line}{RESET}", flush=True)
                        last_translated = line
                        # 背景執行緒翻譯成英文
                        t = threading.Thread(
                            target=translate_and_print,
                            args=(line, log_path),
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

    # Ctrl+S 觸發：停止 whisper → 等待翻譯完成 → 生成摘要
    if ctrl_s_pressed.is_set():
        print(f"\n{C_TITLE}{BOLD}Ctrl+S 偵測到，正在停止轉錄...{RESET}", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        # 等待翻譯 thread 完成
        print(f"{C_DIM}等待翻譯完成...{RESET}", flush=True)
        time.sleep(2)

        # 檢查記錄檔是否有內容
        if not os.path.isfile(log_path) or os.path.getsize(log_path) == 0:
            print(f"{C_HIGHLIGHT}[跳過] 沒有轉錄記錄，無法生成摘要{RESET}")
            sys.exit(0)

        print(f"\n{C_TITLE}{BOLD}▎ 生成摘要{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_DIM}記錄檔: {log_path}{RESET}")
        print(f"  {C_DIM}摘要模型: {summary_model} ({summary_host}:{summary_port}){RESET}")

        try:
            out_path, summary_text, html_path = summarize_log_file(
                log_path, summary_model, summary_host, summary_port,
                server_type=summary_server_type
            )
            if out_path:
                print(f"\n{C_DIM}{'═' * 60}{RESET}")
                print(f"  {C_OK}{BOLD}摘要已儲存（含重點摘要 + 校正逐字稿）{RESET}")
                print(f"  {C_WHITE}{out_path}{RESET}")
                print(f"  {C_WHITE}{html_path}{RESET}")
                print(f"{C_DIM}{'═' * 60}{RESET}")
                print(f"\n{C_HIGHLIGHT}按 ESC 鍵退出{RESET}", flush=True)
                _wait_for_esc()
        except Exception as e:
            print(f"\n{C_HIGHLIGHT}[錯誤] 摘要生成失敗: {e}{RESET}", file=sys.stderr)

        sys.exit(0)


def run_stream_moonshine(capture_id: int, translator, moonshine_model_name: str,
                         mode: str = "en2zh",
                         summary_model: str = SUMMARY_DEFAULT_MODEL,
                         summary_host: str = OLLAMA_DEFAULT_HOST,
                         summary_port: int = OLLAMA_DEFAULT_PORT,
                         summary_server_type: str = "ollama",
                         record: bool = False, rec_device: int = None):
    """使用 Moonshine ASR 引擎即時串流辨識"""

    # 取得 Moonshine 模型
    arch = _moonshine_model_arch(moonshine_model_name)
    print(f"{C_DIM}正在載入 Moonshine 模型 ({moonshine_model_name})...{RESET}", flush=True)
    model_path, model_arch = get_model_for_language("en", arch)

    # 翻譯記錄檔
    from datetime import datetime
    log_prefixes = {"en2zh": "en2zh_translation", "zh2en": "zh2en_translation",
                    "en": "en_transcribe", "zh": "zh_transcribe"}
    log_prefix = log_prefixes.get(mode, "translation")
    log_filename = datetime.now().strftime(f"{log_prefix}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # 錄音（實際建立延後到取得 samplerate 之後）
    recorder = None

    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_DIM}ASR 引擎: Moonshine ({moonshine_model_name}){RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_DIM}會議主題: {translator.meeting_topic}{RESET}")
    print(f"  {C_DIM}按 Ctrl+C 停止 | Ctrl+S 停止並生成摘要{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # Ctrl+S 支援
    ctrl_s_pressed = threading.Event()
    stop_event = threading.Event()

    # 非同步翻譯
    print_lock = threading.Lock()

    def translate_and_print(src_text, log_path):
        """背景執行緒：翻譯並印出結果"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if elapsed < 1.0:
                speed_badge = C_BADGE_FAST
            elif elapsed < 3.0:
                speed_badge = C_BADGE_NORMAL
            else:
                speed_badge = C_BADGE_SLOW
            if mode == "zh2en":
                dst_color, dst_label = C_EN, "EN"
                src_label = "中"
            else:
                dst_color, dst_label = C_ZH, "中"
                src_label = "EN"
            with print_lock:
                _clear_partial_line()  # 清除 [...] 部分文字
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}", speed_badge, elapsed)
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")

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
                    # en2zh：顯示英文，背景翻譯
                    with print_lock:
                        _clear_partial_line()
                        print(f"{C_EN}[EN] {text}{RESET}", flush=True)
                    last_translated = text
                    t = threading.Thread(
                        target=translate_and_print,
                        args=(text, log_path),
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
            recorder = _AudioRecorder(rec_sr, rec_ch)

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
            recorder = _AudioRecorder(sd_samplerate)
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

    # 啟用 Ctrl+S 偵測
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_event, ctrl_s_pressed),
        daemon=True,
    )
    kp_thread.start()

    # 啟動音訊串流
    sd_stream.start()
    if rec_stream:
        rec_stream.start()

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "en": "說英文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n", flush=True)

    # 設定狀態列
    setup_status_bar(mode)
    signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # 主迴圈：等待 Ctrl+C 或 Ctrl+S，每秒更新狀態列時間
    try:
        while not ctrl_s_pressed.is_set() and not stop_event.is_set():
            time.sleep(1.0)
            if _status_bar_active:
                with print_lock:
                    refresh_status_bar()
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

    # 恢復終端機
    clear_status_bar()
    restore_terminal()
    _cleanup_moonshine()

    # Ctrl+S 觸發：生成摘要
    if ctrl_s_pressed.is_set():
        print(f"\n{C_TITLE}{BOLD}Ctrl+S 偵測到，正在停止轉錄...{RESET}", flush=True)

        # 等待翻譯 thread 完成
        print(f"{C_DIM}等待翻譯完成...{RESET}", flush=True)
        time.sleep(2)

        if not os.path.isfile(log_path) or os.path.getsize(log_path) == 0:
            print(f"{C_HIGHLIGHT}[跳過] 沒有轉錄記錄，無法生成摘要{RESET}")
            sys.exit(0)

        print(f"\n{C_TITLE}{BOLD}▎ 生成摘要{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_DIM}記錄檔: {log_path}{RESET}")
        print(f"  {C_DIM}摘要模型: {summary_model} ({summary_host}:{summary_port}){RESET}")

        try:
            out_path, summary_text, html_path = summarize_log_file(
                log_path, summary_model, summary_host, summary_port,
                server_type=summary_server_type
            )
            if out_path:
                print(f"\n{C_DIM}{'═' * 60}{RESET}")
                print(f"  {C_OK}{BOLD}摘要已儲存（含重點摘要 + 校正逐字稿）{RESET}")
                print(f"  {C_WHITE}{out_path}{RESET}")
                print(f"  {C_WHITE}{html_path}{RESET}")
                print(f"{C_DIM}{'═' * 60}{RESET}")
                print(f"\n{C_HIGHLIGHT}按 ESC 鍵退出{RESET}", flush=True)
                _wait_for_esc()
        except Exception as e:
            print(f"\n{C_HIGHLIGHT}[錯誤] 摘要生成失敗: {e}{RESET}", file=sys.stderr)

        sys.exit(0)


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


class _AudioRecorder:
    """將即時模式的音訊錄製為 16-bit PCM WAV 檔。
    定期更新 WAV header，即使程式異常終止也能保留已錄製的音訊。"""

    _HEADER_UPDATE_INTERVAL = 30  # 每 30 秒更新一次 WAV header

    def __init__(self, samplerate=16000, channels=1):
        os.makedirs(RECORDING_DIR, exist_ok=True)
        from datetime import datetime
        fname = datetime.now().strftime("recording_%Y%m%d_%H%M%S.wav")
        self.path = os.path.join(RECORDING_DIR, fname)
        self._samplerate = samplerate
        self._channels = channels
        self._sampwidth = 2  # 16-bit
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

    def close(self):
        try:
            self._write_header()
            self._f.close()
        except Exception:
            pass
        return self.path


def _ask_record():
    """互動選單：詢問是否錄製音訊，自動偵測錄音裝置。
    回傳 (record: bool, rec_device: int or None)"""
    import sounddevice as sd

    # 先偵測錄音裝置，在選單中顯示
    devices = sd.query_devices()
    rec_auto_id = None
    rec_auto_name = None
    rec_auto_label = None
    # 1) 名稱匹配聚集裝置
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            name = dev["name"]
            if "聚集" in name or "aggregate" in name.lower():
                rec_auto_id, rec_auto_name = i, name
                rec_auto_label = "雙方聲音"
                break
    # 2) 名稱不匹配但 input channels >= 3 的 Apple 虛擬裝置（使用者可能改過名稱）
    if rec_auto_id is None:
        for i, dev in enumerate(devices):
            if (dev["max_input_channels"] >= 3
                    and "blackhole" not in dev["name"].lower()):
                rec_auto_id, rec_auto_name = i, dev["name"]
                rec_auto_label = "雙方聲音"
                break
    # 3) fallback: BlackHole（僅對方聲音）
    if rec_auto_id is None:
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
                rec_auto_id, rec_auto_name = i, dev["name"]
                rec_auto_label = "僅對方聲音"
                break

    print(f"\n{C_TITLE}{BOLD}▎ 錄製音訊{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}是否同時錄製音訊為 WAV 檔？（儲存於 recordings/）{RESET}")
    print(f"  {C_HIGHLIGHT}* 即時辨識僅處理播放聲音，無法包含我方說話的聲音{RESET}")
    if rec_auto_name:
        print(f"  {C_WHITE}  錄音裝置: {rec_auto_name}（{rec_auto_label}）{RESET}")
    print(f"  {C_DIM}[1]{RESET} {C_WHITE}不錄製{RESET}")
    print(f"  {C_HIGHLIGHT}{BOLD}[2] 錄製{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}選擇 (1-2) [2]：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input == "1":
        print(f"  {C_OK}→ 不錄製{RESET}\n")
        return False, None

    print(f"  {C_OK}→ 錄製音訊{RESET}")

    # 使用先前偵測到的裝置
    if rec_auto_id is not None:
        print(f"  {C_OK}錄音裝置: [{rec_auto_id}] {rec_auto_name}（{rec_auto_label}）{RESET}")
        return True, rec_auto_id

    # 都找不到 → fallback 顯示選單
    print(f"{C_WARN}[提醒] 未偵測到聚集裝置或 BlackHole，請手動選擇錄音裝置{RESET}")
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev["name"], dev["max_input_channels"],
                                  int(dev["default_samplerate"])))
    default_id = input_devices[0][0] if input_devices else 0

    print(f"\n{C_TITLE}{BOLD}▎ 錄音裝置{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
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


def _ask_topic():
    """互動選單：詢問會議主題（可選，僅翻譯模式使用）。
    回傳主題字串，若使用者跳過則回傳 None。"""
    print(f"\n{C_TITLE}{BOLD}▎ 會議主題（可選，提升翻譯品質）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}輸入此次會議的主題或領域，例如：K8s 安全架構、ZFS 儲存管理{RESET}")
    print(f"  {C_DIM}直接按 Enter 跳過{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}會議主題：{RESET}", end=" ")

    try:
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

    def __init__(self, model="", task=""):
        self._model = model
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

    def set_task(self, task):
        self._task = task
        self._tokens = 0
        self._first_token_time = 0
        self._progress_text = ""
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
    中文約 1 字 ≈ 1.5 tokens，留空間給 prompt 模板和模型回應"""
    if not num_ctx:
        return SUMMARY_CHUNK_FALLBACK_CHARS
    # 可用 token = context window - prompt 模板預留 - 回應預留（取 context 的 1/4）
    available_tokens = num_ctx - SUMMARY_PROMPT_OVERHEAD_TOKENS - num_ctx // 4
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
    tmp_wav = os.path.join(RECORDING_DIR, f".tmp_{base}_{int(time.time())}.wav")

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

    # 逐段提取聲紋
    embeddings = []
    valid_indices = []  # 有成功提取 embedding 的段落索引

    for i, seg in enumerate(segments):
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
    import numpy as np
    valid_embeddings = np.array([embeddings[i] for i in valid_indices])

    # SpectralClusterer 分群
    min_clusters = 2 if num_speakers is None else num_speakers
    max_clusters = 8 if num_speakers is None else num_speakers

    try:
        clusterer = SpectralClusterer(min_clusters=min_clusters, max_clusters=max_clusters)
        cluster_labels = clusterer.predict(valid_embeddings)
    except Exception as e:
        print(f"  {C_HIGHLIGHT}[警告] 分群失敗: {e}，所有段落標記為 Speaker 1{RESET}")
        return [0] * len(segments)

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

    # 平滑修正：孤立段落（前後鄰居相同但自己不同）修正為鄰居的講者
    # 例如 [1,1,2,1,1] → 中間的 2 大概率是分錯，修正為 1
    changed = 0
    for i in range(1, len(speaker_labels) - 1):
        prev_spk = speaker_labels[i - 1]
        curr_spk = speaker_labels[i]
        next_spk = speaker_labels[i + 1]
        if curr_spk != prev_spk and prev_spk == next_spk:
            speaker_labels[i] = prev_spk
            changed += 1
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


def process_audio_file(input_path, mode, translator, model_size="large-v3-turbo",
                       diarize=False, num_speakers=None):
    """處理音訊檔：ffmpeg 轉檔 → faster-whisper 辨識 → 翻譯 → 存檔，回傳 log_path"""
    from datetime import datetime

    # 1. 驗證檔案存在
    if not os.path.isfile(input_path):
        print(f"  {C_HIGHLIGHT}[錯誤] 檔案不存在: {input_path}{RESET}", file=sys.stderr)
        return None

    basename = os.path.splitext(os.path.basename(input_path))[0]
    print(f"\n{C_TITLE}{BOLD}▎ 處理: {os.path.basename(input_path)}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")

    # 2. 轉檔
    wav_path, is_temp = _convert_to_wav(input_path)
    if wav_path is None:
        return None
    if is_temp:
        out_size = os.path.getsize(wav_path)
        out_str = (f"{out_size / 1048576:.1f} MB" if out_size >= 1048576
                   else f"{out_size / 1024:.0f} KB")
        print(f"  {C_OK}轉檔        {RESET}{C_DIM}→ 16kHz mono WAV ({out_str}){RESET}")
    else:
        print(f"  {C_OK}轉檔        {RESET}{C_DIM}已是 WAV 格式{RESET}")

    # 3. 載入 faster-whisper 模型
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(f"  {C_HIGHLIGHT}[錯誤] faster-whisper 未安裝，請執行: pip install faster-whisper{RESET}",
              file=sys.stderr)
        return None

    print(f"  {C_WHITE}載入模型    {model_size}...{RESET}", end=" ", flush=True)
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    print(f"{C_OK}✓{RESET}")

    # 4. 辨識
    lang = "zh" if mode in ("zh", "zh2en") else "en"
    need_translate = mode in ("en2zh", "zh2en")

    # Log 檔名
    log_prefixes = {"en2zh": "en2zh_translation", "zh2en": "zh2en_translation",
                    "en": "en_transcribe", "zh": "zh_transcribe"}
    log_prefix = log_prefixes.get(mode, "translation")
    log_filename = datetime.now().strftime(f"{log_prefix}_{basename}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    print(f"  {C_WHITE}辨識語言    {lang}{RESET}")
    print(f"  {C_DIM}記錄檔      logs/{log_filename}{RESET}")
    print(f"  {C_WHITE}辨識中...{RESET}\n")

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

    # 啟動狀態列
    sbar = _SummaryStatusBar(model=model_size, task="辨識中").start()
    if audio_duration > 0:
        sbar.set_progress("0%")

    segments, info = model.transcribe(wav_path, language=lang, beam_size=5, vad_filter=True)

    seg_count = 0
    try:
        # 收集所有有效段落（過濾幻覺和空白）
        valid_segments = []
        for segment in segments:
            # 更新辨識進度
            if audio_duration > 0:
                pct = min(segment.end / audio_duration, 1.0)
                pos_m, pos_s = divmod(int(segment.end), 60)
                dur_m, dur_s = divmod(int(audio_duration), 60)
                sbar.set_progress(
                    f"{pct:.0%}  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
                )

            text = segment.text.strip()
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
                "start": segment.start,
                "end": segment.end,
                "text": text,
            })

        sbar.set_task(f"辨識完成（{len(valid_segments)} 段）")
        sbar.set_progress("")

        # 講者辨識
        speaker_labels = None
        if diarize and valid_segments:
            speaker_labels = _diarize_segments(wav_path, valid_segments,
                                               num_speakers=num_speakers, sbar=sbar)

        # 輸出結果
        with open(log_path, "w", encoding="utf-8") as log_f:
            for i, seg in enumerate(valid_segments):
                seg_count += 1
                text = seg["text"]
                ts_start = _format_timestamp(seg["start"])
                ts_end = _format_timestamp(seg["end"])
                ts_tag = f"[{ts_start}-{ts_end}]"

                sbar.set_task(f"輸出中（{seg_count}/{len(valid_segments)}）")

                # 講者標籤
                spk_tag_term = ""  # 終端機用（帶色彩）
                spk_tag_log = ""   # log 用（純文字）
                if speaker_labels is not None:
                    spk_num = speaker_labels[i] + 1  # 1-based 顯示
                    spk_color = SPEAKER_COLORS[speaker_labels[i] % len(SPEAKER_COLORS)]
                    spk_tag_term = f"{spk_color}[Speaker {spk_num}]{RESET} "
                    spk_tag_log = f"[Speaker {spk_num}] "

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
                    else:
                        print(flush=True)
                        log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n\n")
                else:
                    print(f"{src_color}{BOLD}{ts_tag} {spk_tag_term}[{src_label}] {text}{RESET}", flush=True)
                    print(flush=True)
                    log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n\n")

        sbar._task = "完成"
        sbar.freeze()

        # 清理暫存 wav
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)

        diarize_info = ""
        if speaker_labels is not None:
            n_spk = len(set(speaker_labels))
            diarize_info = f" | {n_spk} 位講者"

        print(f"\n{C_DIM}{'═' * 60}{RESET}")
        print(f"  {C_OK}{BOLD}處理完成{RESET} {C_DIM}（共 {seg_count} 段{diarize_info}）{RESET}")
        print(f"  {C_WHITE}{log_path}{RESET}")
        print(f"{C_DIM}{'═' * 60}{RESET}")

        sbar.stop()
        return log_path

    except KeyboardInterrupt:
        sbar.stop()
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)
        print(f"\n\n{C_DIM}已中止處理。{RESET}")
        if seg_count > 0:
            print(f"  {C_DIM}已處理的 {seg_count} 段已儲存: {log_path}{RESET}")
            return log_path
        return None
    except Exception as e:
        sbar.stop()
        if is_temp and os.path.exists(wav_path):
            os.remove(wav_path)
        print(f"\n  {C_HIGHLIGHT}[錯誤] 處理失敗: {e}{RESET}", file=sys.stderr)
        return None


def summarize_log_file(input_path, model, host, port, server_type="ollama"):
    """讀取記錄檔 → 建 prompt → 呼叫 LLM → S2TWP 轉換 → 寫摘要檔
    回傳 (output_path, summary_text, html_path)"""
    with open(input_path, "r", encoding="utf-8") as f:
        transcript = f.read().strip()

    if not transcript:
        print(f"  {C_HIGHLIGHT}[跳過] 檔案內容為空: {input_path}{RESET}")
        return None, None, None

    basename = os.path.basename(input_path)
    dirpath = os.path.dirname(input_path) or "."

    # 依原始檔名決定摘要檔名
    if basename.startswith("en2zh_translation"):
        out_name = basename.replace("en2zh_translation", "en2zh_summary", 1)
    elif basename.startswith("zh2en_translation"):
        out_name = basename.replace("zh2en_translation", "zh2en_summary", 1)
    elif basename.startswith("en_transcribe"):
        out_name = basename.replace("en_transcribe", "en_summary", 1)
    elif basename.startswith("zh_transcribe"):
        out_name = basename.replace("zh_transcribe", "zh_summary", 1)
    else:
        out_name = f"summary_{basename}"
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

    sbar = _SummaryStatusBar(model=model, task="準備中").start()

    if len(chunks) <= 1:
        # 單段：直接摘要
        prompt = _summary_prompt(transcript)
        sbar.set_task(f"生成摘要（單段，{len(transcript)} 字）")
        summary = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                  server_type=server_type)
    else:
        # 多段：逐段摘要 + 合併
        segment_summaries = []
        for i, chunk in enumerate(chunks):
            sbar.set_task(f"第 {i+1}/{len(chunks)} 段（{len(chunk)} 字）")
            prompt = _summary_prompt(chunk)
            seg = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                  server_type=server_type)
            seg = S2TWP.convert(seg)
            segment_summaries.append(seg)
            print(f"  {C_OK}第 {i+1}/{len(chunks)} 段完成{RESET}", flush=True)

        # 合併各段摘要
        sbar.set_task(f"合併 {len(chunks)} 段摘要")
        combined = "\n\n---\n\n".join(
            f"### 第 {i+1} 段\n{s}" for i, s in enumerate(segment_summaries)
        )
        merge_prompt = SUMMARY_MERGE_PROMPT_TEMPLATE.format(summaries=combined)
        merged_summary = call_ollama_raw(merge_prompt, model, host, port, spinner=sbar, live_output=True,
                                         server_type=server_type)

        # 組合完整輸出：各段校正逐字稿 + 合併摘要
        summary = ""
        for i, seg in enumerate(segment_summaries):
            summary += f"--- 第 {i+1} 段 ---\n{seg}\n\n"
        summary += f"--- 總結 ---\n{merged_summary}"

    sbar.stop()

    # 簡體→台灣繁體
    summary = S2TWP.convert(summary)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(summary + "\n")

    # 同步產生 HTML 摘要
    html_path = os.path.splitext(output_path)[0] + ".html"
    _summary_to_html(summary, html_path, os.path.basename(input_path),
                     summary_txt_path=output_path, transcript_txt_path=input_path)
    subprocess.Popen(["open", html_path])

    return output_path, summary, html_path


def _summary_to_html(summary_text, html_path, source_name="",
                     summary_txt_path="", transcript_txt_path=""):
    """將摘要純文字轉為帶樣式的 HTML 檔"""
    import html as html_mod

    # 講者顏色（8 色循環，與終端機 SPEAKER_COLORS 對應的 HTML 色碼）
    _SPEAKER_HTML_COLORS = [
        "#ffcb6b",  # 黃色
        "#82aaff",  # 藍色
        "#c3e88d",  # 綠色
        "#f78c6c",  # 橘色
        "#c792ea",  # 紫色
        "#89ddff",  # 青色
        "#ff5370",  # 紅色
        "#a8d8a8",  # 淺綠
    ]

    lines = summary_text.split("\n")
    body_parts = []
    in_list = False  # 追蹤是否在 <ul> 內
    current_speaker = None  # 追蹤目前講者編號
    pending_br = False  # 延遲插入空行
    for line in lines:
        s = line.strip()
        if not s:
            if in_list:
                body_parts.append("</ul>")
                in_list = False
            # 記錄有空行，但延遲插入（避免 speaker 段落前多餘空行）
            pending_br = True
            continue

        # 空行後的非 speaker 行才插入 <br>（speaker 自帶 margin-top）
        if pending_br:
            if not re.match(r'^\*{0,2}(Speaker \d+|講者 ?\d+)', s):
                body_parts.append("<br>")
            pending_br = False

        # 離開列表模式
        is_list_item = s.startswith("- ")
        if in_list and not is_list_item:
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
            body_parts.append("<hr>")
        elif is_list_item:
            if not in_list:
                body_parts.append("<ul>")
                in_list = True
            item = html_mod.escape(s[2:])
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            body_parts.append(f'<li>{item}</li>')
        elif re.match(r'^\*{0,2}(Speaker \d+|講者 ?\d+)', s):
            m = re.match(r'^\*{0,2}(?:Speaker |講者 ?)(\d+)', s)
            if m:
                spk_num = int(m.group(1))
                current_speaker = spk_num
            color = _SPEAKER_HTML_COLORS[(current_speaker or 1) % len(_SPEAKER_HTML_COLORS)]
            body_parts.append(f'<p class="speaker" style="color:{color}">{escaped}</p>')
        else:
            if current_speaker is not None:
                # 同一講者的延續段落，使用相同顏色但不加粗
                color = _SPEAKER_HTML_COLORS[current_speaker % len(_SPEAKER_HTML_COLORS)]
                body_parts.append(f'<p style="color:{color}">{escaped}</p>')
            else:
                body_parts.append(f"<p>{escaped}</p>")

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
        footer_links.append(f'<a href="{log_basename}">逐字稿 (TXT)</a>')
    footer_html = " | ".join(footer_links)

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
  li {{ color: #a8d8a8; margin: 4px 0; }}
  hr {{ border: none; border-top: 1px solid #444; margin: 1.5em 0; }}
  p {{ margin: 0.4em 0; }}
  .speaker {{ font-weight: bold; margin-top: 1em; }}
  .speaker strong {{ color: inherit; }}
  strong {{ color: #f78c6c; }}
  .meta {{ color: #888; font-size: 0.85em; margin-bottom: 2em; }}
  .footer {{ margin-top: 3em; padding-top: 1em; border-top: 1px solid #444;
             color: #888; font-size: 0.85em; }}
  .footer a {{ color: #82aaff; text-decoration: none; margin: 0 0.3em; }}
  .footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="meta">來源：{title} | 由 jt-live-whisper AI 摘要產生</div>
{body_html}
<div class="footer">
  相關檔案：{footer_html}
</div>
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


def keypress_listener_thread(stop_event, ctrl_s_event):
    """Daemon thread：偵測 Ctrl+S (\x13) 按鍵"""
    fd = sys.stdin.fileno()
    while not stop_event.is_set():
        try:
            rlist, _, _ = select.select([fd], [], [], 0.2)
            if rlist:
                data = os.read(fd, 32)
                if b'\x13' in data:  # Ctrl+S
                    ctrl_s_event.set()
                    return
        except Exception:
            return


# ─── 底部狀態列（固定顯示快捷鍵提示 + 即時資訊）────────────────
_status_bar_active = False
_status_bar_needs_resize = False
_status_bar_state = {
    "start_time": 0.0,   # monotonic 起始時間
    "count": 0,          # 翻譯/轉錄筆數
    "mode": "en2zh",     # 功能模式
}


def setup_status_bar(mode="en2zh"):
    """設定終端機底部固定狀態列，利用 scroll region 讓字幕只在上方滾動"""
    global _status_bar_active
    _status_bar_state["start_time"] = time.monotonic()
    _status_bar_state["count"] = 0
    _status_bar_state["mode"] = mode
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
        status = f" {time_str} | {label} {count} 筆 | Ctrl+C 停止 | Ctrl+S 停止並生成摘要 "
        # 計算顯示寬度（中文字佔 2 格）
        dw = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in status)
        padding = " " * max(0, cols - dw)
        sys.stdout.write(f"\x1b[48;2;60;60;60m\x1b[38;2;200;200;200m{status}{padding}\x1b[0m")
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


def parse_args():
    """解析命令列參數"""
    examples = [
        ("./start.sh", "互動式選單"),
        ("./start.sh -s training", "教育訓練場景"),
        ("./start.sh --mode zh", "中文轉錄模式"),
        ("./start.sh --asr moonshine", "使用 Moonshine 引擎"),
        ("./start.sh --topic 'ZFS 儲存管理'", "指定會議主題，提升翻譯品質"),
        ("./start.sh -m large-v3-turbo -e ollama -d 0", "全部指定，跳過選單"),
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
        help="語音辨識引擎 (whisper / moonshine，英文模式預設 moonshine)")
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
        "-e", "--engine", choices=["ollama", "argos"], metavar="ENGINE",
        help="翻譯引擎 (ollama / argos，ollama 支援 Ollama 及 OpenAI 相容伺服器)")
    parser.add_argument(
        "--ollama-model", metavar="NAME",
        help="LLM 翻譯模型名稱 (預設 qwen2.5:14b)")
    parser.add_argument(
        "--ollama-host", metavar="HOST",
        help=f"LLM 伺服器位址，自動偵測 Ollama 或 OpenAI 相容 (預設 {OLLAMA_HOST}:{OLLAMA_PORT})")
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
    """從 args 解析 LLM 伺服器 host/port"""
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

    # --input 離線處理音訊檔
    if args.input:
        # 決定參數來源：有 --mode → CLI 模式；沒有 → 互動選單
        if args.mode is None:
            (mode, fw_model, ollama_model, summary_model,
             host, port, diarize, num_speakers, do_summarize,
             server_type) = _input_interactive_menu(args)
            engine = "ollama"
            if not server_type:
                server_type = "ollama"
        else:
            mode = args.mode
            diarize = args.diarize
            num_speakers = args.num_speakers
            do_summarize = args.summarize is not None
            fw_model = args.model or "large-v3-turbo"
            engine = args.engine or "ollama"
            ollama_model = args.ollama_model or "qwen2.5:14b"
            summary_model = args.summary_model
            host, port = _resolve_ollama_host(args)
            server_type = None  # CLI 模式稍後偵測

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

        # 一開始就檢查 LLM 伺服器連線
        ollama_available = False
        need_llm_translate = need_translate and engine == "ollama"
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
                print(f"  {C_WHITE}{label:12s}{RESET}{C_WHITE}{model_name_display}{RESET} {C_DIM}@ {host}:{port}{RESET} {C_HIGHLIGHT}✗ 無法連接{RESET}")

        if not server_type:
            server_type = "ollama"

        # 初始化翻譯器
        meeting_topic = args.topic if need_translate else None
        translator = None
        can_summarize = ollama_available
        if need_translate:
            if engine == "ollama" and ollama_available:
                translator = OllamaTranslator(ollama_model, host, port, direction=mode,
                                              skip_check=True, server_type=server_type,
                                              meeting_topic=meeting_topic)
            elif engine == "ollama" and not ollama_available:
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

        # 顯示設定資訊
        print(f"  {C_WHITE}模式        {mode_label}{RESET}")
        print(f"  {C_WHITE}辨識模型    {fw_model}{RESET}")
        if diarize:
            sp_info = f"啟用（{num_speakers} 人）" if num_speakers else "啟用（自動偵測）"
            print(f"  {C_WHITE}講者辨識  {sp_info}{RESET}")
        print(f"  {C_WHITE}檔案數      {RESET}{C_DIM}{len(args.input)}{RESET}")

        # 逐檔處理
        log_paths = []
        for fpath in args.input:
            log_path = process_audio_file(fpath, mode, translator, model_size=fw_model,
                                          diarize=diarize, num_speakers=num_speakers)
            if log_path:
                log_paths.append(log_path)

        # 如果需要摘要且 LLM 伺服器可用，對產生的 log 檔自動摘要
        if do_summarize and log_paths and can_summarize:
            print(f"\n{C_TITLE}{BOLD}▎ 自動摘要{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")
            print(f"  {C_DIM}摘要模型: {summary_model} ({host}:{port}){RESET}")

            for lp in log_paths:
                print(f"\n  {C_DIM}摘要: {os.path.basename(lp)}{RESET}")
                try:
                    out_path, _, html_path = summarize_log_file(lp, summary_model, host, port,
                                                                  server_type=server_type)
                    if out_path:
                        print(f"\n{C_DIM}{'═' * 60}{RESET}")
                        print(f"  {C_OK}{BOLD}摘要已儲存（含重點摘要 + 校正逐字稿）{RESET}")
                        print(f"  {C_WHITE}{out_path}{RESET}")
                        print(f"  {C_WHITE}{html_path}{RESET}")
                        print(f"{C_DIM}{'═' * 60}{RESET}")
                except Exception as e:
                    print(f"  {C_HIGHLIGHT}[錯誤] 摘要失敗: {e}{RESET}")

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

        print(f"\n{C_TITLE}{BOLD}▎ 批次摘要模式{RESET}")
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
            if first_base.startswith("en2zh_translation"):
                out_name = "en2zh_summary_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("zh2en_translation"):
                out_name = "zh2en_summary_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("en_transcribe"):
                out_name = "en_summary_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            elif first_base.startswith("zh_transcribe"):
                out_name = "zh_summary_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
            else:
                out_name = "summary_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
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
            sbar = _SummaryStatusBar(model=model, task="準備中").start()

            if len(chunks) <= 1:
                prompt = _summary_prompt(combined_transcript)
                sbar.set_task(f"生成摘要（單段，{len(combined_transcript)} 字）")
                summary = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                          server_type=server_type)
            else:
                segment_summaries = []
                for i, chunk in enumerate(chunks):
                    sbar.set_task(f"第 {i+1}/{len(chunks)} 段（{len(chunk)} 字）")
                    prompt = _summary_prompt(chunk)
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
                merged_summary = call_ollama_raw(merge_prompt, model, host, port, spinner=sbar, live_output=True,
                                                 server_type=server_type)

                summary = ""
                for i, seg in enumerate(segment_summaries):
                    summary += f"--- 第 {i+1} 段 ---\n{seg}\n\n"
                summary += f"--- 總結 ---\n{merged_summary}"

            sbar._task = "完成"
            sbar.freeze()
            summary = S2TWP.convert(summary)

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(summary + "\n")

            # 同步產生 HTML 摘要
            html_path = os.path.splitext(output_path)[0] + ".html"
            source_name = os.path.basename(valid_files[0]) if valid_files else ""
            transcript_path = valid_files[0] if valid_files else ""
            _summary_to_html(summary, html_path, source_name,
                             summary_txt_path=output_path, transcript_txt_path=transcript_path)
            subprocess.Popen(["open", html_path])

            print(f"\n{C_DIM}{'═' * 60}{RESET}")
            print(f"  {C_OK}{BOLD}摘要已儲存（含重點摘要 + 校正逐字稿）{RESET}")
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
            print(f"\n{C_TITLE}{BOLD}▎ sounddevice 音訊裝置{RESET}")
            list_audio_devices_sd()
        # whisper-stream 裝置
        model_path_exists = os.path.isfile(WHISPER_STREAM)
        if model_path_exists:
            _, model_path = resolve_model("large-v3-turbo")
            print(f"\n{C_TITLE}{BOLD}▎ whisper-stream SDL2 音訊裝置{RESET}")
            list_audio_devices(model_path)
        sys.exit(0)

    if cli_mode:
        # CLI 模式：用參數 + 預設值，跳過選單
        mode = args.mode or "en2zh"

        # 決定 ASR 引擎
        if args.asr:
            asr_engine = args.asr
        elif mode in ("en2zh", "en") and _MOONSHINE_AVAILABLE:
            asr_engine = "moonshine"
        else:
            asr_engine = "whisper"
        # 中文模式強制 whisper
        if mode in ("zh", "zh2en"):
            asr_engine = "whisper"

        check_dependencies(asr_engine)

        if asr_engine == "moonshine":
            # Moonshine 模式
            ms_model_name = args.moonshine_model or "medium"

            if args.device is not None:
                capture_id = args.device
            else:
                capture_id = auto_select_device_sd()

            translator = None
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            meeting_topic = args.topic if mode in ("en2zh", "zh2en") else None
            if mode == "en2zh":
                engine = args.engine or "ollama"
                if engine == "ollama":
                    ollama_model = args.ollama_model or "qwen2.5:14b"
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
            print()
            run_stream_moonshine(capture_id, translator, ms_model_name, mode,
                                 summary_model=args.summary_model, summary_host=s_host, summary_port=s_port,
                                 summary_server_type=srv_type,
                                 record=args.record, rec_device=args.rec_device)
        else:
            # Whisper 模式（原有邏輯）
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
            meeting_topic = args.topic if mode in ("en2zh", "zh2en") else None
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            if mode in ("en2zh", "zh2en"):
                engine = args.engine or "ollama"
                if engine == "ollama":
                    ollama_model = args.ollama_model or "qwen2.5:14b"
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
            print()
            run_stream(capture_id, translator, model_name, model_path, length_ms, step_ms, mode,
                       summary_model=args.summary_model, summary_host=s_host, summary_port=s_port,
                       summary_server_type=srv_type,
                       record=args.record, rec_device=args.rec_device)
    else:
        # 互動式選單
        mode = select_mode()

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
        s_server_type = "ollama"
        if asr_engine == "moonshine" and mode == "en2zh":
            engine, model, host, port, srv_type = select_translator()
            meeting_topic = _ask_topic()
            if engine == "ollama":
                translator = OllamaTranslator(model, host, port, direction=mode,
                                              server_type=srv_type,
                                              meeting_topic=meeting_topic)
                s_host, s_port, s_server_type = host, port, srv_type
            else:
                translator = ArgosTranslator()
        elif asr_engine == "whisper" and mode in ("en2zh", "zh2en"):
            engine, model, host, port, srv_type = select_translator()
            meeting_topic = _ask_topic()
            if engine == "ollama":
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
            run_stream_moonshine(capture_id, translator, ms_model_name, mode,
                                 summary_model=args.summary_model, summary_host=s_host, summary_port=s_port,
                                 summary_server_type=s_server_type,
                                 record=record, rec_device=rec_device)
        else:
            model_name, model_path = select_whisper_model(mode)
            length_ms, step_ms = select_scene()
            capture_id = list_audio_devices(model_path)
            run_stream(capture_id, translator, model_name, model_path, length_ms, step_ms, mode,
                       summary_model=args.summary_model, summary_host=s_host, summary_port=s_port,
                       summary_server_type=s_server_type,
                       record=record, rec_device=rec_device)


if __name__ == "__main__":
    main()
