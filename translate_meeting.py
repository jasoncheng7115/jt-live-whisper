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
import signal
import subprocess
import sys
import threading
import time
import wave
from collections import deque

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


_hf_ssl_bypassed = False


def _enable_hf_ssl_bypass():
    """企業網路 SSL 中間人憑證對策：停用 HuggingFace 下載的 SSL 驗證"""
    global _hf_ssl_bypassed
    if _hf_ssl_bypassed:
        return
    _hf_ssl_bypassed = True
    os.environ["CURL_CA_BUNDLE"] = ""
    os.environ["REQUESTS_CA_BUNDLE"] = ""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        import requests
        _s = requests.Session()
        _s.verify = False
        from huggingface_hub import configure_http_backend
        configure_http_backend(backend_factory=lambda: _s)
    except Exception:
        pass


def _call_with_ssl_retry(fn, *args, **kwargs):
    """呼叫 fn，若 SSL 錯誤則停用驗證後重試一次"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        err = str(e)
        if "SSL" in err or "CERTIFICATE" in err.upper() or "ssl" in err:
            print(f"\n  [注意] SSL 憑證驗證失敗，嘗試停用驗證重試...", flush=True)
            _enable_hf_ssl_bypass()
            return fn(*args, **kwargs)
        raise

if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios

# Windows: 啟用 Virtual Terminal Processing（ANSI 色彩碼 / scroll region 支援）
if IS_WINDOWS:
    try:
        import ctypes as _ctypes
        _kernel32 = _ctypes.windll.kernel32
        _h_out = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _mode_out = _ctypes.c_uint32()
        _kernel32.GetConsoleMode(_h_out, _ctypes.byref(_mode_out))
        _kernel32.SetConsoleMode(_h_out, _mode_out.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

# Windows: 確保 stdout/stderr 使用 UTF-8（避免 cp950 無法編碼 ✓✗ 等 Unicode 符號）
if IS_WINDOWS:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Windows: 背景 subprocess 不彈黑色視窗
_SUBPROCESS_FLAGS = {}
if IS_WINDOWS:
    _SUBPROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW}

# 避免 OpenMP 重複載入衝突
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
# 抑制 Intel MKL SSE4.2 棄用警告（Apple Silicon + Rosetta 會觸發）
os.environ["MKL_SERVICE_FORCE_INTEL"] = "1"
# 抑制 HuggingFace Hub 警告（symlink、未認證下載）
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import json
import urllib.request

import ctranslate2
import sentencepiece

# OpenCC 簡體→台灣繁體轉換（用於 ASR 辨識結果；LLM 翻譯結果由 prompt 控制，不經 S2TWP）
try:
    from opencc import OpenCC as _OpenCC
    S2TWP = _OpenCC("s2twp")
except ImportError:
    S2TWP = type("_S2TWProxy", (), {"convert": staticmethod(lambda text: text)})()

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

# Windows WASAPI Loopback（零設定擷取系統播放音訊）
WASAPI_LOOPBACK_ID = -100  # sentinel，表示使用 WASAPI Loopback
WASAPI_MIXED_ID = -200     # sentinel，表示 Windows 混合錄音（Loopback + 麥克風）
_PYAUDIOWPATCH_AVAILABLE = False
if IS_WINDOWS:
    try:
        import pyaudiowpatch as _pyaudio
        _PYAUDIOWPATCH_AVAILABLE = True
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
C_JA = "\x1b[38;2;255;180;100m"       # 橙色 - 日文
C_MY_ZH = "\x1b[38;2;120;200;255m"    # 水藍 - 我方中文原文（雙向模式）
C_MY_EN = "\x1b[38;2;200;160;255m"   # 淡紫 - 我方英文翻譯（雙向模式）
C_MY_JA = "\x1b[38;2;255;200;140m"   # 淡橙 - 我方日文（雙向模式）
C_OK = "\x1b[38;2;80;255;120m"        # 綠色 - 成功
C_DIM = "\x1b[38;2;100;100;100m"      # 暗灰 - 次要資訊
C_WHITE = "\x1b[38;2;255;255;255m"    # 白色 - 一般文字
C_WARN = "\x1b[38;2;255;220;80m"     # 黃色 - 警告提醒
C_ERR = "\x1b[38;2;255;100;100m"     # 紅色 - 錯誤提醒
# 速度標籤（背景色 + 黑字，不用 REVERSE 以避免換行時色塊延伸）
C_BADGE_FAST = "\x1b[48;2;80;255;120m\x1b[38;2;0;0;0m"    # 綠底黑字 < 1s
C_BADGE_NORMAL = "\x1b[48;2;255;220;80m\x1b[38;2;0;0;0m"  # 黃底黑字 1-3s
C_BADGE_SLOW = "\x1b[48;2;255;100;100m\x1b[38;2;0;0;0m"   # 紅底黑字 > 3s
C_BADGE_ASR = "\x1b[48;2;90;90;90m\x1b[38;2;200;200;200m"  # 灰底灰字 - 辨識耗時
C_BADGE_MY_TRANS = "\x1b[48;2;130;90;180m\x1b[38;2;240;230;255m"  # 紫底淡紫字 - 我方翻譯耗時


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
                or '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff'
                or '\uff00' <= c <= '\uffef' or '\u3400' <= c <= '\u4dbf'):
            w += 2
        else:
            w += 1
    return w


def _print_with_badge(text, badge_color, elapsed, label=""):
    """輸出翻譯文字 + 速度 badge，避免 badge 換行導致背景色延伸整行。
    可透過 config.json 設定隱藏：hide_asr_time (辨識) / hide_translate_time (翻譯)"""
    # 檢查是否隱藏此類 badge
    if label == "辨" and _config.get("hide_asr_time", False):
        print(text, flush=True)
        return
    if label == "譯" and _config.get("hide_translate_time", False):
        print(text, flush=True)
        return
    if label:
        badge_str = f" {label} {elapsed:.1f}s "
    else:
        badge_str = f" {elapsed:.1f}s "
    badge_len = len(badge_str)
    text_width = _str_display_width(text)
    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 80
    if cols <= 0:
        cols = 80
    cursor_col = text_width % cols
    if cursor_col + 2 + badge_len > cols:
        # badge 放不下，換行後縮排顯示
        print(f"{text}\n    {badge_color}{badge_str}{RESET}", flush=True)
    else:
        print(f"{text}  {badge_color}{badge_str}{RESET}", flush=True)


def _speed_badge_color(elapsed):
    """依耗時選擇 badge 顏色"""
    if elapsed < 1.0:
        return C_BADGE_FAST
    elif elapsed < 3.0:
        return C_BADGE_NORMAL
    return C_BADGE_SLOW


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
if IS_WINDOWS:
    _ws_exe = "whisper-stream.exe"
    _ws_p1 = os.path.join(SCRIPT_DIR, "whisper.cpp", "build", "bin", _ws_exe)
    _ws_p2 = os.path.join(SCRIPT_DIR, "whisper.cpp", "build", "bin", "Release", _ws_exe)
    WHISPER_STREAM = _ws_p1 if os.path.isfile(_ws_p1) else _ws_p2
else:
    WHISPER_STREAM = os.path.join(SCRIPT_DIR, "whisper.cpp", "build", "bin", "whisper-stream")
MODELS_DIR = os.path.join(SCRIPT_DIR, "whisper.cpp", "models")
# 動態搜尋 Argos 英翻中模型：先用 API 查，失敗再掃目錄
ARGOS_PKG_PATH = ""
try:
    import argostranslate.package as _argos_pkg
    for _p in _argos_pkg.get_installed_packages():
        if _p.from_code == "en" and _p.to_code == "zh":
            ARGOS_PKG_PATH = _p.package_path
            break
except Exception:
    pass
if not ARGOS_PKG_PATH:
    # fallback: 掃描已知目錄
    _argos_bases = []
    if IS_WINDOWS:
        for _env in ("LOCALAPPDATA", "APPDATA"):
            _b = os.environ.get(_env)
            if _b:
                _argos_bases.append(os.path.join(_b, "argos-translate", "packages"))
    else:
        _argos_bases.append(os.path.expanduser("~/.local/share/argos-translate/packages"))
    for _argos_base in _argos_bases:
        if os.path.isdir(_argos_base):
            _candidates = sorted(
                [d for d in os.listdir(_argos_base) if d.startswith("translate-en_zh-")],
                reverse=True)
            if _candidates:
                ARGOS_PKG_PATH = os.path.join(_argos_base, _candidates[0])
                break

# 動態搜尋 NLLB 600M 翻譯模型
NLLB_MODEL_DIR = ""
_nllb_search_dirs = []
if IS_WINDOWS:
    for _env in ("LOCALAPPDATA", "APPDATA"):
        _b = os.environ.get(_env)
        if _b:
            _nllb_search_dirs.append(os.path.join(_b, "jt-live-whisper", "models", "nllb-600m"))
else:
    _nllb_search_dirs.append(os.path.expanduser("~/.local/share/jt-live-whisper/models/nllb-600m"))
for _nd in _nllb_search_dirs:
    if os.path.isdir(_nd) and os.path.isfile(os.path.join(_nd, "model.bin")):
        NLLB_MODEL_DIR = _nd
        break

# 跨平台 Loopback 裝置偵測
_LOOPBACK_LABEL = "WASAPI Loopback" if IS_WINDOWS else "BlackHole 2ch"
_START_CMD = ".\\start.ps1" if IS_WINDOWS else "./start.sh"
_INSTALL_CMD = ".\\install.ps1" if IS_WINDOWS else "./install.sh"


def _force_exit(code=0):
    """強制結束程序。一律用 os._exit() 避免卡在 C 擴展（CTranslate2/MLX Metal）。
    signal handler 在呼叫前已完成音訊裝置清理。"""
    if not IS_WINDOWS:
        # macOS：殺掉 resource_tracker 子程序，避免 semaphore 洩漏警告
        try:
            import multiprocessing.resource_tracker as _rt
            _pid = getattr(_rt._resource_tracker, '_pid', None)
            if _pid:
                os.kill(_pid, 9)  # SIGKILL
        except Exception:
            pass
    os._exit(code)


def _is_loopback_device(name):
    """判斷裝置名稱是否為系統播放聲音的 loopback 裝置"""
    n = name.lower()
    if IS_WINDOWS:
        return ("loopback" in n or "stereo mix" in n
                or "what u hear" in n or "wave out" in n)
    return "blackhole" in n


# ── Windows WASAPI Loopback 支援 ────────────────────────────────

_wasapi_loopback_cache = None  # 快取結果避免重複初始化


def _find_wasapi_loopback():
    """找出 Windows 預設喇叭的 WASAPI Loopback 裝置。
    回傳 pyaudiowpatch device info dict 或 None。結果會快取。"""
    global _wasapi_loopback_cache
    if not _PYAUDIOWPATCH_AVAILABLE:
        return None
    if _wasapi_loopback_cache is not None:
        return _wasapi_loopback_cache if _wasapi_loopback_cache else None
    try:
        p = _pyaudio.PyAudio()
        try:
            info = p.get_default_wasapi_loopback()
            _wasapi_loopback_cache = info
            return info
        except Exception:
            _wasapi_loopback_cache = {}  # 空 dict 表示已查過但找不到
            return None
        finally:
            p.terminate()
    except Exception:
        _wasapi_loopback_cache = {}
        return None


def _find_default_mic():
    """找到 Windows 預設麥克風（排除 Loopback 裝置）。回傳 device_id 或 None。"""
    import sounddevice as sd
    devices = sd.query_devices()
    # 優先使用系統預設輸入裝置
    default_in = sd.default.device[0]
    if default_in is not None and default_in >= 0:
        dev = devices[default_in]
        if dev["max_input_channels"] > 0 and not _is_loopback_device(dev["name"]):
            return default_in
    # Fallback: 找第一個非 Loopback 輸入裝置
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and not _is_loopback_device(dev["name"]):
            return i
    return None


def _find_blackhole_device():
    """找到 macOS BlackHole 裝置 ID（排除 Aggregate Device）"""
    import sounddevice as sd
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and "blackhole" in dev["name"].lower():
            return i
    return None


def _find_mac_mic():
    """找到 macOS 麥克風（排除 BlackHole 和 Aggregate Device）"""
    import sounddevice as sd
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    if default_in is not None and default_in >= 0:
        dev = devices[default_in]
        name_lower = dev["name"].lower()
        if (dev["max_input_channels"] > 0
                and not _is_loopback_device(dev["name"])
                and "aggregate" not in name_lower and "聚集" not in dev["name"]):
            return default_in
    for i, dev in enumerate(devices):
        name_lower = dev["name"].lower()
        if (dev["max_input_channels"] > 0
                and not _is_loopback_device(dev["name"])
                and "aggregate" not in name_lower and "聚集" not in dev["name"]):
            return i
    return None


def _detect_bidi_devices():
    """偵測雙向模式所需的兩個音訊裝置（系統音訊 + 麥克風）。
    回傳 (lb_id, lb_name, mic_id, mic_name) 或 None"""
    import sounddevice as sd
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        mic_id = _find_default_mic()
        if wb_info and mic_id is not None:
            return (WASAPI_LOOPBACK_ID, wb_info["name"],
                    mic_id, sd.query_devices(mic_id)["name"])
    elif IS_MACOS:
        lb_id = _find_blackhole_device()
        mic_id = _find_mac_mic()
        if lb_id is not None and mic_id is not None:
            return (lb_id, sd.query_devices(lb_id)["name"],
                    mic_id, sd.query_devices(mic_id)["name"])
    return None


class _WasapiLoopbackStream:
    """包裝 pyaudiowpatch stream，介面對齊 sd.InputStream。
    callback 簽名：(numpy_array, frames, time_info, status)"""

    def __init__(self, callback, samplerate, channels, blocksize, dtype="float32"):
        import numpy as np
        self._callback = callback
        self._samplerate = samplerate
        self._channels = channels
        self._blocksize = blocksize
        self._np = np
        self._p = _pyaudio.PyAudio()
        wb_info = self._p.get_default_wasapi_loopback()
        self._stream = self._p.open(
            format=_pyaudio.paFloat32,
            channels=channels,
            rate=int(samplerate),
            input=True,
            input_device_index=wb_info["index"],
            frames_per_buffer=blocksize,
            stream_callback=self._pa_callback,
            start=False,  # 不自動啟動，等 start() 明確啟動
        )

    def _pa_callback(self, in_data, frame_count, time_info, status_flags):
        import numpy as np
        audio = np.frombuffer(in_data, dtype=np.float32)
        if self._channels > 1:
            audio = audio.reshape(-1, self._channels)
        else:
            audio = audio.reshape(-1, 1)
        # 轉換 status flags
        status = None
        if self._callback:
            self._callback(audio, frame_count, time_info, status)
        return (None, _pyaudio.paContinue)

    def start(self):
        self._stream.start_stream()

    def stop(self):
        if self._stream.is_active():
            self._stream.stop_stream()

    def close(self):
        self.stop()
        self._stream.close()
        self._p.terminate()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
        self.close()


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

# GPU 伺服器 Whisper 辨識
REMOTE_WHISPER_DEFAULT_PORT = 8978
REMOTE_WHISPER_CONFIG = _config.get("remote_whisper", None)

# 錄音輸出格式（預設 mp3，支援 mp3/ogg/flac/wav）
RECORDING_FORMAT = _config.get("recording_format", "mp3")
if RECORDING_FORMAT not in ("mp3", "ogg", "flac", "wav"):
    RECORDING_FORMAT = "mp3"

# 內建翻譯模型（作者篩選推薦）
_BUILTIN_TRANSLATE_MODELS = [
    ("phi4:14b", "Microsoft，品質不錯"),
    ("qwen2.5:32b", "品質很好，中日文翻譯推薦"),
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
    ("ja2zh", "日翻中字幕", "日文語音 → 翻譯成繁體中文"),
    ("zh2ja", "中翻日字幕", "中文語音 → 翻譯成日文"),
    ("en_zh", "英中雙向字幕", "對方說英文翻中文 + 自己說中文翻英文"),
    ("ja_zh", "日中雙向字幕", "對方說日文翻中文 + 自己說中文翻日文"),
    ("en", "英文轉錄", "英文語音 → 直接顯示英文"),
    ("zh", "中文轉錄", "中文語音 → 直接顯示繁體中文"),
    ("ja", "日文轉錄", "日文語音 → 直接顯示日文"),
    ("record", "純錄音", f"僅錄製音訊為 {RECORDING_FORMAT.upper()} 檔"),
]

# Mode 分類常數
_EN_INPUT_MODES = ("en2zh", "en")
_ZH_INPUT_MODES = ("zh2en", "zh", "zh2ja")
_JA_INPUT_MODES = ("ja2zh", "ja")
_TRANSLATE_MODES = ("en2zh", "zh2en", "ja2zh", "zh2ja", "en_zh", "ja_zh")
_NOENG_MODELS = ("zh", "zh2en", "zh2ja", "ja2zh", "ja", "en_zh", "ja_zh")  # 不能用 .en 模型
_BIDI_MODES = ("en_zh", "ja_zh")  # 雙向翻譯模式（用硬體音訊來源分流）
_BIDI_LB_DIR = {"en_zh": "en2zh", "ja_zh": "ja2zh"}   # 系統音訊翻譯方向
_BIDI_MIC_DIR = {"en_zh": "zh2en", "ja_zh": "zh2ja"}  # 麥克風翻譯方向

# 顯示標籤 dict（src_color, src_label, dst_color, dst_label）
_MODE_LABELS = {
    "en2zh": (C_EN, "EN", C_ZH, "中"),
    "zh2en": (C_ZH, "中", C_EN, "EN"),
    "ja2zh": (C_JA, "日", C_ZH, "中"),
    "zh2ja": (C_ZH, "中", C_JA, "日"),
    "en":    (C_EN, "EN", C_EN, "EN"),
    "zh":    (C_ZH, "中", C_ZH, "中"),
    "ja":    (C_JA, "日", C_JA, "日"),
    "en_zh": (C_EN, "EN", C_ZH, "中"),  # 雙向模式 fallback（即時模式用 _BIDI_LABELS）
    "ja_zh": (C_JA, "日", C_ZH, "中"),
}

# 雙向模式標籤（每個方向各一組 src_color, src_label, dst_color, dst_label）
_BIDI_LABELS = {
    "en_zh": {
        "loopback": (C_EN, "EN", C_ZH, "中"),      # 對方：灰色英文 → 青綠中文
        "mic":      (C_MY_ZH, "中", C_MY_EN, "EN"), # 我方：水藍中文 → 淡紫英文
    },
    "ja_zh": {
        "loopback": (C_JA, "日", C_ZH, "中"),      # 對方：日文 → 中文
        "mic":      (C_MY_ZH, "中", C_MY_JA, "日"), # 我方：中文 → 日文
    },
    "en2zh": {
        "loopback": (C_EN, "EN", C_ZH, "中"),
        "mic":      (C_MY_ZH, "中", C_MY_ZH, "中"),
    },
    "zh2en": {
        "loopback": (C_ZH, "中", C_EN, "EN"),
        "mic":      (C_MY_EN, "EN", C_MY_EN, "EN"),
    },
    "ja2zh": {
        "loopback": (C_JA, "日", C_ZH, "中"),
        "mic":      (C_MY_ZH, "中", C_MY_ZH, "中"),
    },
    "zh2ja": {
        "loopback": (C_ZH, "中", C_JA, "日"),
        "mic":      (C_MY_JA, "日", C_MY_JA, "日"),
    },
    "en": {
        "loopback": (C_EN, "EN", C_EN, "EN"),
        "mic":      (C_MY_EN, "EN", C_MY_EN, "EN"),
    },
    "zh": {
        "loopback": (C_ZH, "中", C_ZH, "中"),
        "mic":      (C_MY_ZH, "中", C_MY_ZH, "中"),
    },
    "ja": {
        "loopback": (C_JA, "日", C_JA, "日"),
        "mic":      (C_MY_JA, "日", C_MY_JA, "日"),
    },
}

# 雙向模式語言對照表（模組級，供 process_bidi_audio_files 等使用）
_LB_LANG = {"en2zh": "en", "zh2en": "zh", "ja2zh": "ja", "zh2ja": "zh",
            "en": "en", "zh": "zh", "ja": "ja", "en_zh": "en", "ja_zh": "ja"}
_MIC_LANG = {"en2zh": "zh", "zh2en": "en", "ja2zh": "zh", "zh2ja": "ja",
             "en": "en", "zh": "zh", "ja": "ja", "en_zh": "zh", "ja_zh": "zh"}

# 可用的 whisper 模型（由小到大）
WHISPER_MODELS = [
    ("base.en", "ggml-base.en.bin", "最快，準確度一般"),
    ("small.en", "ggml-small.en.bin", "快，準確度好"),
    ("small", "ggml-small.bin", "快，多語言（中日文可用）"),
    ("large-v3-turbo", "ggml-large-v3-turbo.bin", "快，準確度很好"),
    ("medium.en", "ggml-medium.en.bin", "較慢，準確度很好"),
    ("medium", "ggml-medium.bin", "較慢，多語言（中日文品質較好）"),
    ("large-v3", "ggml-large-v3.bin", "最慢，中日文品質最好，有獨立 GPU 可選用"),
]

# ── CPU 效能評估（自動選擇適合的 Whisper 模型）──

def _is_apple_silicon():
    """偵測是否為 Apple Silicon (ARM64) Mac"""
    import platform
    return IS_MACOS and platform.machine() == "arm64"


def _has_local_gpu():
    """本機是否有 GPU 加速（Apple Silicon Metal 或 NVIDIA CUDA）"""
    if _is_apple_silicon():
        return True
    if IS_WINDOWS:
        import shutil
        return bool(shutil.which("nvidia-smi"))
    return False


def _has_mlx_whisper():
    """Apple Silicon 且已安裝 mlx-whisper"""
    if not _is_apple_silicon():
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _recommended_whisper_model(mode="en2zh"):
    """根據 CPU 架構與核心數推薦此裝置最適合的即時 Whisper 模型。
    Apple Silicon 有 Metal GPU 加速，同核心數效能遠高於 Intel CPU。"""
    cores = os.cpu_count() or 2
    _need_multilang = mode in _NOENG_MODELS
    has_metal = _is_apple_silicon()
    # Intel Mac / x86_64：沒有 Metal 加速，large 模型太慢
    if IS_MACOS and not has_metal:
        if _need_multilang:
            return "small"  # 無 GPU 加速，用小模型確保即時性
        if cores >= 8:
            return "small.en"
        elif cores >= 4:
            return "base.en"
        else:
            return "base.en"
    # Apple Silicon + mlx-whisper：GPU 加速，多語言用 turbo
    if _need_multilang and _is_apple_silicon() and _has_mlx_whisper():
        return "large-v3-turbo"  # mlx-whisper GPU 加速
    # Apple Silicon 無 mlx-whisper：faster-whisper 不支援 Metal，降回 small
    if _need_multilang and _is_apple_silicon() and not _has_mlx_whisper():
        return "small"
    # Windows (可能有 CUDA)：有 GPU 加速
    if _need_multilang:
        if _has_local_gpu():
            return "large-v3-turbo"  # 有 GPU 加速，用 turbo 品質較好
        else:
            return "small"  # 無 GPU 加速，用小模型確保即時性
    if cores >= 8:
        return "large-v3-turbo"
    elif cores >= 6:
        return "small.en"
    else:
        return "base.en"


def _whisper_model_fit_label(model_name, recommended, has_remote=False):
    """產生模型適用性標籤。"""
    if model_name == recommended:
        return "GPU 伺服器推薦" if has_remote else "此裝置適合"
    return ""

# 使用場景預設參數 (length_ms, step_ms, 說明)
SCENE_PRESETS = [
    ("線上會議", 5000, 3000, "對話短句，反應快（5秒）"),
    ("教育訓練", 8000, 3000, "長句連續講述，翻譯更完整（8秒）"),
    ("演講簡報", 12000, 4000, "長段演講，內容完整度優先（12秒）"),
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

APP_VERSION = "2.14.5"

# ─── WebUI 暫停控制（SIGUSR1 toggle）──────────────────────────────
_webui_pause_event = None  # 由各 streaming 函式設定


def _handle_sigusr1(signum, frame):
    """SIGUSR1：WebUI 暫停/繼續 toggle"""
    if _webui_pause_event is not None:
        if _webui_pause_event.is_set():
            _webui_pause_event.clear()
        else:
            _webui_pause_event.set()


if not IS_WINDOWS and hasattr(signal, "SIGUSR1"):
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

# ─── WebUI Event System ──────────────────────────────────────────
# --webui 啟動時透過 TCP socket 將事件推送到 webui.py
# 不啟用時 _webui_send() 是 no-op，零效能影響
_webui_queue = None  # queue.Queue，啟用時才建立
_WEBUI_PORT = 19780


def _webui_send(event: dict):
    """非阻塞推送事件到 WebUI（未啟用時直接返回）"""
    if _webui_queue is not None:
        try:
            # 清理 UTF-8 replacement character
            for k in ("src_text", "dst_text", "detail"):
                if k in event and isinstance(event[k], str):
                    event[k] = event[k].replace("\ufffd", "")
            _webui_queue.put_nowait(event)
        except Exception:
            pass


def _start_webui_sender():
    """啟動 WebUI TCP sender daemon thread"""
    global _webui_queue
    import queue as _q
    _webui_queue = _q.Queue(maxsize=500)

    def _sender():
        import socket as _sock
        import json as _json
        conn = None

        def _connect():
            """嘗試 TCP 連線到 webui.py"""
            nonlocal conn
            if conn is not None:
                return True
            for _ in range(3):  # 重試 3 次
                try:
                    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                    s.settimeout(1.0)
                    s.connect(("127.0.0.1", _WEBUI_PORT))
                    s.settimeout(None)
                    conn = s
                    return True
                except Exception:
                    time.sleep(0.5)
            return False

        # 預先連線（不等第一個事件）
        time.sleep(1.0)  # 等 webui.py 啟動
        _connect()

        while True:
            try:
                ev = _webui_queue.get(timeout=5.0)
            except Exception:
                # 沒有事件也定期嘗試重連
                if conn is None:
                    _connect()
                continue
            if not _connect():
                continue  # 連不上就丟棄此事件
            try:
                conn.sendall((_json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

    _t = threading.Thread(target=_sender, daemon=True)
    _t.start()


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


TRANSCRIPT_CORRECT_PROMPT_TEMPLATE = """\
你是語音辨識（ASR）文字校正員。以下是語音辨識產出的逐字稿片段，請修正辨識錯誤的文字。

規則：
- 修正語音辨識造成的錯字、同音字錯誤、專有名詞辨識錯誤（例如 safe → Ceph、vme → VMware）
- 不要改變語句結構、語序
- 如果某行是明顯的 ASR 幻覺（無意義的外文音節、亂碼、與上下文完全無關的詞彙），回傳 "序號|[雜音]"
- 每一行格式為 "序號|文字"，請用完全相同的格式逐行回傳
- 如果該行不需修正，原封不動回傳
- 全部使用台灣繁體中文用語（軟體、網路、記憶體、程式、伺服器等）
- 專有名詞維持英文原文
- 直接輸出結果，不要使用 <think> 標籤或任何思考過程
{topic_line}
{lines}
"""

# 場景名稱對照（CLI 用）
SCENE_MAP = {"meeting": 0, "training": 1, "presentation": 2, "subtitle": 3}
MODE_MAP = {key: i for i, (key, _, _) in enumerate(MODE_PRESETS)}
APP_NAME = f"jt-live-whisper v{APP_VERSION} - 100% 全地端 AI 語音工具集"
APP_AUTHOR = "by Jason Cheng (Jason Tools)"


def check_dependencies(asr_engine="whisper", translate_engine=None):
    """檢查所有必要檔案是否存在"""
    errors = []
    if asr_engine == "whisper" and not os.path.isfile(WHISPER_STREAM):
        errors.append(f"找不到 whisper-stream: {WHISPER_STREAM}")
    if asr_engine == "moonshine" and not _MOONSHINE_AVAILABLE:
        errors.append("moonshine-voice 未安裝，請執行: pip install moonshine-voice sounddevice numpy")
    if translate_engine == "argos" and not os.path.isdir(ARGOS_PKG_PATH):
        errors.append(f"找不到翻譯模型: {ARGOS_PKG_PATH}")
    if translate_engine == "nllb" and not os.path.isdir(NLLB_MODEL_DIR):
        errors.append(f"找不到 NLLB 翻譯模型，請執行 {_INSTALL_CMD} 安裝")
    if errors:
        for e in errors:
            print(f"[錯誤] {e}", file=sys.stderr)
        sys.exit(1)


def select_mode():
    """讓用戶選擇功能模式"""
    default_idx = 0  # 預設：英翻中

    print(f"\n\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
    # 計算顯示寬度（中文字佔 2 格）
    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)
    col = max(_dw(name) for _, name, _ in MODE_PRESETS) + 2
    _group_headers = {0: "單向翻譯", 4: "雙向翻譯", 6: "轉錄", 9: "其他"}
    for i, (key, name, desc) in enumerate(MODE_PRESETS):
        if i in _group_headers:
            hdr = _group_headers[i]
            hdr_w = _dw(hdr)
            print(f"{C_DIM}{'─' * 12} {hdr} {'─' * (60 - 13 - hdr_w)}{RESET}")
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


def select_whisper_model(mode="en2zh", use_faster_whisper=False):
    """讓用戶選擇 whisper 模型（包含未下載的模型，選擇後自動下載）
    use_faster_whisper=True 時跳過 ggml 檢查（faster-whisper 自動從 HuggingFace 下載）"""
    # 列出所有適用模型（不限已安裝）
    candidates = []
    for name, filename, desc in WHISPER_MODELS:
        # 中文/日文模式不能用 .en 模型（僅支援英文）
        if mode in _NOENG_MODELS and name.endswith(".en"):
            continue
        path = os.path.join(MODELS_DIR, filename)
        installed = use_faster_whisper or os.path.isfile(path)
        candidates.append((name, filename, path, desc, installed))

    if not candidates:
        print("[錯誤] 沒有適用的 whisper 模型！", file=sys.stderr)
        sys.exit(1)

    print(f"\n\n{C_TITLE}{BOLD}▎ 語音辨識模型{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    recommended = _recommended_whisper_model(mode)
    default_idx = 0
    for i, (name, _, _, _, installed) in enumerate(candidates):
        if name == recommended and installed:
            default_idx = i
    # 若推薦模型未安裝，預設選第一個已安裝的
    if not candidates[default_idx][4]:
        for i, (_, _, _, _, installed) in enumerate(candidates):
            if installed:
                default_idx = i
                break
    for i, (name, _, _, desc, installed) in enumerate(candidates):
        fit = _whisper_model_fit_label(name, recommended)
        fit_tag = f"  {C_OK}({fit}){RESET}" if fit else ""
        dl_tag = f"  {C_DIM}(需下載){RESET}" if not installed else ""
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {name:16s}{RESET} {C_WHITE}{desc}{RESET}{fit_tag}{dl_tag}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{name:16s}{RESET} {C_DIM}{desc}{RESET}{fit_tag}{dl_tag}")
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
            if 0 <= idx < len(candidates):
                selected = candidates[idx]
            else:
                print("[錯誤] 無效的編號", file=sys.stderr)
                sys.exit(1)
        except ValueError:
            print("[錯誤] 請輸入數字", file=sys.stderr)
            sys.exit(1)
    else:
        selected = candidates[default_idx]

    name, filename, path, desc, installed = selected
    # 未安裝的模型：自動下載
    if not installed:
        # 模型名稱 = 去掉 ggml- 開頭和 .bin 字尾
        dl_name = filename.replace("ggml-", "").replace(".bin", "")
        dl_script = os.path.join(MODELS_DIR, "download-ggml-model.sh")
        if os.path.isfile(dl_script):
            print(f"\n{C_WARN}正在下載模型 {name}...{RESET}", flush=True)
            import subprocess as _sp
            rc = _sp.call(["bash", dl_script, dl_name], cwd=os.path.dirname(dl_script))
            if rc != 0 or not os.path.isfile(path):
                print(f"[錯誤] 模型 {name} 下載失敗", file=sys.stderr)
                sys.exit(1)
            print(f"{C_OK}模型 {name} 下載完成{RESET}")
        else:
            print(f"[錯誤] 找不到下載腳本: {dl_script}", file=sys.stderr)
            sys.exit(1)

    print(f"  {C_OK}→ {name}{RESET} {C_DIM}({desc}){RESET}\n")
    return name, (None if use_faster_whisper else path)


def select_whisper_model_remote(mode="en2zh"):
    """伺服器模式選擇 Whisper 模型（不檢查本機 .bin 檔案，顯示伺服器快取標籤）。
    回傳 model_name (str)。"""
    _need_multilang = mode in _NOENG_MODELS
    available = []
    for name, _filename, desc in WHISPER_MODELS:
        if _need_multilang and name.endswith(".en"):
            continue
        available.append((name, desc))

    # 預設模型
    default_name = "large-v3-turbo"
    default_idx = 0
    for i, (name, _) in enumerate(available):
        if name == default_name:
            default_idx = i
            break

    # 查詢伺服器已快取的模型
    remote_cached = set()
    if REMOTE_WHISPER_CONFIG:
        remote_cached = _remote_whisper_models(REMOTE_WHISPER_CONFIG, timeout=3)

    print(f"\n\n{C_TITLE}{BOLD}▎ 辨識模型（GPU 伺服器）{RESET}")
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
        print(f"  {C_HIGHLIGHT}[注意] 模型 {model_name} 尚未下載到伺服器，首次辨識需要先下載（可能需數分鐘）{RESET}")
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
        text=True, encoding="utf-8", errors="replace",
        **_SUBPROCESS_FLAGS,
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
    """自動選擇 Loopback 音訊裝置（SDL2），找不到才 fallback 顯示選單"""
    print(f"{C_DIM}正在偵測音訊裝置...{RESET}")

    devices = _enumerate_sdl_devices(model_path)

    if not devices:
        if IS_WINDOWS and _find_wasapi_loopback():
            print(f"{C_ERR}[錯誤] Whisper (whisper-stream) 使用 SDL2 擷取音訊，無法擷取 Windows 系統播放聲音。{RESET}", file=sys.stderr)
            print(f"{C_WARN}  建議改用以下方式（可自動擷取系統音訊）：{RESET}", file=sys.stderr)
            print(f"{C_WHITE}    1. Moonshine 引擎（--asr moonshine）{RESET}", file=sys.stderr)
            print(f"{C_WHITE}    2. 遠端 GPU 辨識（設定 config.json remote_whisper）{RESET}", file=sys.stderr)
            sys.exit(1)
        print("[錯誤] 找不到任何音訊捕捉裝置！", file=sys.stderr)
        print(f"請確認 {_LOOPBACK_LABEL} 已安裝並重新啟動電腦。", file=sys.stderr)
        sys.exit(1)

    # 自動選 Loopback 裝置
    for dev_id, dev_name in devices:
        if _is_loopback_device(dev_name):
            print(f"  {C_OK}ASR 裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 Loopback → fallback 顯示選單讓使用者手動選
    print(f"{C_WARN}[提醒] 未偵測到 {_LOOPBACK_LABEL}，請手動選擇音訊裝置{RESET}")
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
    if IS_WINDOWS and _PYAUDIOWPATCH_AVAILABLE:
        print(f"  {C_WARN}  * Windows 上 Whisper 使用 SDL2，可能無法擷取系統播放聲音{RESET}")
        print(f"  {C_WARN}    建議使用 Moonshine（可透過 WASAPI 自動擷取系統音訊）{RESET}")
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
    """讓使用者選擇辨識位置（GPU 伺服器 / 本機），僅在 REMOTE_WHISPER_CONFIG 存在時呼叫。
    回傳 "remote" 或 "local"。"""
    rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
    options = [
        (f"GPU 伺服器（{rw_host}，速度快）", "remote"),
        ("本機（Whisper 或 Moonshine）", "local"),
    ]
    default_idx = 0  # 預設伺服器

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
    print(f"{C_HIGHLIGHT}  * 伺服器不支援 Moonshine，固定使用 Whisper{RESET}")
    print(f"\x1b[48;2;130;90;180m\x1b[38;2;255;255;255m{BOLD}  * 若要同時轉錄麥克風(或其它音訊輸入)需選擇「本機」 {RESET}")
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
        print(f"  {C_OK}→ GPU 伺服器（{rw_host}）{RESET}")
        print(f"  {C_DIM}伺服器不支援 Moonshine，使用 Whisper{RESET}\n")
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
    """自動選擇 Loopback 音訊裝置（sounddevice），找不到才 fallback 顯示選單"""
    # Windows: 優先用 WASAPI Loopback（零設定擷取系統音訊）
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            print(f"  {C_OK}ASR 裝置: WASAPI Loopback ({wb_info['name']}){RESET}")
            return WASAPI_LOOPBACK_ID

    devices = sd.query_devices()
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev["name"], dev["max_input_channels"], int(dev["default_samplerate"])))

    if not input_devices:
        print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
        sys.exit(1)

    # 自動選 Loopback 裝置
    for dev_id, dev_name, _, _ in input_devices:
        if _is_loopback_device(dev_name):
            print(f"  {C_OK}ASR 裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 Loopback → fallback 顯示選單
    print(f"{C_WARN}[提醒] 未偵測到 {_LOOPBACK_LABEL}，請手動選擇音訊裝置{RESET}")
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
    """非互動模式：使用 sounddevice 自動偵測 Loopback 裝置"""
    # Windows: 優先用 WASAPI Loopback
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            print(f"{C_OK}自動選擇音訊裝置: WASAPI Loopback ({wb_info['name']}){RESET}")
            return WASAPI_LOOPBACK_ID

    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
            print(f"{C_OK}自動選擇音訊裝置: [{i}] {dev['name']}{RESET}")
            return i
    # 找不到 Loopback，用系統預設輸入
    default = sd.default.device[0]
    if default is not None and default >= 0:
        dev = devices[default]
        print(f"{C_HIGHLIGHT}未偵測到 {_LOOPBACK_LABEL}，使用系統預設輸入: [{default}] {dev['name']}{RESET}")
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
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"連接 {srv_label}（{model}）"})
            try:
                self._call_ollama("hello", [])
                print(f"{C_OK}{BOLD}完成！{RESET}")
            except Exception as e:
                print(f"\n[錯誤] 無法連接 {srv_label}: {e}", file=sys.stderr)
                sys.exit(1)

    def _build_prompt(self, text, context):
        _dispatch = {"zh2en": self._build_prompt_zh2en,
                     "ja2zh": self._build_prompt_ja2zh,
                     "zh2ja": self._build_prompt_zh2ja}
        builder = _dispatch.get(self.direction, self._build_prompt_en2zh)
        return builder(text, context)

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
            "9. 忠實翻譯原文，禁止因政治因素修改任何用語（國名、地名、人物稱謂須與原文一致）\n"
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
            "8. Translate faithfully, never alter wording due to political sensitivity (country names, place names, titles must match the source)\n"
        )
        if self.meeting_topic:
            prompt += f"\nMeeting topic: {self.meeting_topic}\nTranslate domain-specific terms according to this topic.\n"
        if context:
            prompt += "\nRecent context:\n"
            for src, dst in context:
                prompt += f"中：{src}\nEN：{dst}\n"
        prompt += f"\nTranslate：{text}"
        return prompt

    def _build_prompt_ja2zh(self, text, context):
        prompt = (
            "你是即時會議翻譯員，將日文翻譯成台灣繁體中文。\n"
            "規則：\n"
            "1. 必須使用繁體中文，禁止使用簡體中文（例：用「軟體」不用「软件」，用「記憶體」不用「内存」）\n"
            "2. 使用台灣用語：軟體、網路、記憶體、程式、伺服器、資料庫、影片、滑鼠、設定、訊息\n"
            "3. 專有名詞維持原文（如 iPhone、API、Kubernetes、GitHub）；日文人名用片假名或漢字原文\n"
            "4. 只輸出一行繁體中文翻譯，不要輸出原文、解釋、替代版本\n"
            "5. 只能包含繁體中文和英文，禁止輸出日文、俄文、韓文等其他語言\n"
            "6. 禁止添加任何評論、括號註解、翻譯說明\n"
            "7. 即使原文不完整或語意不清，也直接逐字翻譯，不要跳過或加說明\n"
            "8. 直接輸出翻譯結果，不要使用 <think> 標籤或任何思考過程\n"
            "9. 忠實翻譯原文，禁止因政治因素修改任何用語（國名、地名、人物稱謂須與原文一致）\n"
        )
        if self.meeting_topic:
            prompt += f"\n本次會議主題：{self.meeting_topic}\n請根據此主題的領域知識翻譯專業術語。\n"
        if context:
            prompt += "\n最近的對話上下文：\n"
            for src, dst in context:
                prompt += f"日：{src}\n中：{dst}\n"
        prompt += f"\n請翻譯：{text}"
        return prompt

    def _build_prompt_zh2ja(self, text, context):
        prompt = (
            "あなたはリアルタイム会議通訳者です。中国語を日本語に翻訳してください。\n"
            "ルール：\n"
            "1. 自然で流暢な日本語を出力すること\n"
            "2. 固有名詞はそのまま維持（例：iPhone、API、Kubernetes、GitHub）\n"
            "3. 翻訳結果のみを1行で出力し、説明や代替案は不要\n"
            "4. 日本語のみを出力し、中国語、ロシア語、韓国語などは含めない\n"
            "5. コメント、括弧付きの注釈、翻訳に関する備考を追加しない\n"
            "6. 原文が不完全でも、そのまま逐語的に翻訳し、説明を加えない\n"
            "7. 翻訳結果を直接出力し、<think>タグや思考プロセスを使用しない\n"
            "8. 原文に忠実に翻訳し、政治的な理由で用語を変更しないこと（国名、地名、人物の肩書きは原文通り）\n"
        )
        if self.meeting_topic:
            prompt += f"\n会議のテーマ：{self.meeting_topic}\nこのテーマに関連する専門用語を適切に翻訳してください。\n"
        if context:
            prompt += "\n最近のコンテキスト：\n"
            for src, dst in context:
                prompt += f"中：{src}\n日：{dst}\n"
        prompt += f"\n翻訳してください：{text}"
        return prompt

    def warmup(self, max_retries=3, timeout=120):
        """預熱 LLM 模型，確保模型已載入且能正常回應（ASR 耗時可能導致模型被卸載）"""
        _test = {"en2zh": "Hello", "zh2en": "你好", "ja2zh": "こんにちは",
                 "zh2ja": "你好"}.get(self.direction, "Hello")
        for attempt in range(max_retries):
            try:
                result = _llm_generate(
                    self._build_prompt(_test, []), self.model,
                    self.host, self.port, self.server_type,
                    stream=False, timeout=timeout, think=False,
                )
                if result and result.strip():
                    return True
            except Exception:
                pass
        return False

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
        # 日文方向的幻覺（模型輸出評論而非翻譯）
        "修正し", "文法的に正しい", "翻訳すると", "自然な表現",
        "より自然に", "表現へ変更",
    ]

    def _contains_bad_chars(self, text):
        """檢查是否包含非預期語言的字元"""
        _ja_out = self.direction in ("zh2ja", "ja")
        for ch in text:
            if ('\u0400' <= ch <= '\u04ff' or   # 俄文 Cyrillic
                '\u0e00' <= ch <= '\u0e7f' or   # 泰文
                '\u0600' <= ch <= '\u06ff'):     # 阿拉伯文
                return True
            if not _ja_out and (
                '\u3040' <= ch <= '\u309f' or   # 日文平假名
                '\u30a0' <= ch <= '\u30ff'):     # 日文片假名
                return True
        # zh→ja 方向：結果必須含假名（否則可能是中文或英文而非日文）
        if _ja_out and len(text) >= 2:
            has_kana = any(
                '\u3040' <= ch <= '\u309f' or '\u30a0' <= ch <= '\u30ff'
                for ch in text
            )
            if not has_kana:
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
        # 移除句尾的 LLM 評論（如「。修正し、...」）
        cleaned = re.sub(r'[。．.](?:修正|文法的|より自然|翻訳すると|自然な).*$', '。', cleaned)
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
            # 移除 prompt 洩漏（模型把 instruction 輸出到翻譯結果中）
            result = re.sub(r'[/\|]?\s*Instruction:.*$', '', result, flags=re.IGNORECASE).strip()
            result = re.sub(r'忠實翻譯原文.*$', '', result).strip()
            result = re.sub(r'禁止因政治.*$', '', result).strip()
            # 過濾 LLM 自我修正/思考洩漏
            result = re.sub(r'根據格式要求.*$', '', result).strip()
            result = re.sub(r'最終版本[：:].*$', '', result).strip()
            result = re.sub(r'最終定稿[：:].*$', '', result).strip()
            result = re.sub(r'再確認指令後.*$', '', result).strip()
            result = re.sub(r'再依指示修正後.*$', '', result).strip()
            result = re.sub(r'直接翻譯[為为].*$', '', result).strip()
            result = re.sub(r'注意[「「].*如果要更.*$', '', result).strip()
            result = re.sub(r'但遵守指令.*$', '', result).strip()
            # 只取第一行，避免 model 輸出多餘解釋
            result = result.split("\n")[0].strip()
            # LLM 翻譯不經 S2TWP（prompt 已控制繁體，S2TWP 會誤轉如「干擾→幹擾」）
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
        if not os.path.isdir(ARGOS_PKG_PATH):
            print(f"[錯誤] 找不到 Argos 翻譯模型: {ARGOS_PKG_PATH}", file=sys.stderr)
            print(f"請執行 {_INSTALL_CMD} 重新安裝，或改用 LLM 伺服器翻譯", file=sys.stderr)
            sys.exit(1)
        print(f"{C_DIM}正在載入離線翻譯模型...{RESET}", end=" ", flush=True)
        _webui_send({"type": "progress", "stage": "載入中", "detail": "離線翻譯模型"})
        self.sp = sentencepiece.SentencePieceProcessor()
        self.sp.Load(os.path.join(ARGOS_PKG_PATH, "sentencepiece.model"))
        self.ct2 = ctranslate2.Translator(
            os.path.join(ARGOS_PKG_PATH, "model"), device="cpu"
        )
        print(f"{C_OK}{BOLD}完成！{RESET}")

    def _translate_short(self, text: str) -> str:
        """翻譯單句（不超過約 200 tokens 的短文字）。"""
        tokens = self.sp.Encode(text, out_type=str)
        results = self.ct2.translate_batch([tokens])
        translated_tokens = results[0].hypotheses[0]
        translated = self.sp.Decode(translated_tokens)
        return translated.replace("\u2581", " ").strip()

    @staticmethod
    def _has_repetition(text: str) -> bool:
        """偵測翻譯結果是否有過度重複（幻覺）。"""
        if len(text) < 10:
            return False
        # 單字重複：同一個中文字連續出現 4 次以上
        for i in range(len(text) - 3):
            if text[i] == text[i+1] == text[i+2] == text[i+3] and text[i].strip():
                return True
        # 2-8 字元片段重複 5 次以上
        for n in range(2, min(9, len(text) // 3 + 1)):
            for start in range(min(len(text) - n * 4, 30)):
                pat = text[start:start + n]
                if pat.strip() and text.count(pat) >= 5:
                    return True
        # 翻譯結果比原文長太多（3 倍以上通常是幻覺）
        return False

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        import re
        # Argos 對長句容易幻覺，一律按句子切割翻譯
        sentences = re.split(r'(?<=[.!?,;])\s+', text)
        translated_parts = []
        max_chars = 80
        buf = ""
        for sent in sentences:
            if buf and len(buf) + len(sent) > max_chars:
                part = self._translate_short(buf)
                if not self._has_repetition(part):
                    translated_parts.append(part)
                buf = sent
            else:
                buf = (buf + " " + sent).strip() if buf else sent
        if buf:
            part = self._translate_short(buf)
            if self._has_repetition(part):
                # 幻覺 → 逐句重試
                for s in re.split(r'(?<=[.!?,;])\s+', buf):
                    s = s.strip()
                    if not s:
                        continue
                    p = self._translate_short(s)
                    if not self._has_repetition(p):
                        translated_parts.append(p)
            else:
                translated_parts.append(part)
        return S2TWP.convert(" ".join(translated_parts))


class NllbTranslator:
    """使用 NLLB 600M (CTranslate2) 離線多語言翻譯"""

    _LANG_MAP = {
        "en": "eng_Latn",
        "zh": "zho_Hant",
        "ja": "jpn_Jpan",
    }
    _DIRECTION_MAP = {
        "en2zh": ("en", "zh"),
        "zh2en": ("zh", "en"),
        "ja2zh": ("ja", "zh"),
        "zh2ja": ("zh", "ja"),
    }

    def __init__(self, direction="en2zh"):
        if not os.path.isdir(NLLB_MODEL_DIR):
            print(f"[錯誤] 找不到 NLLB 翻譯模型: 請執行 {_INSTALL_CMD} 安裝", file=sys.stderr)
            sys.exit(1)
        src_key, tgt_key = self._DIRECTION_MAP.get(direction, ("en", "zh"))
        self.src_lang = self._LANG_MAP[src_key]
        self.tgt_lang = self._LANG_MAP[tgt_key]
        self.direction = direction
        print(f"{C_DIM}正在載入 NLLB 離線翻譯模型...{RESET}", end=" ", flush=True)
        _webui_send({"type": "progress", "stage": "載入中", "detail": "NLLB 離線翻譯模型"})
        # 檢查 config.json 是否存在（新版 ctranslate2 需要，舊模型可能缺少）
        _cfg_path = os.path.join(NLLB_MODEL_DIR, "config.json")
        if not os.path.exists(_cfg_path):
            print(f"\n  {C_DIM}模型缺少 config.json，正在重新下載...{RESET}", end=" ", flush=True)
            try:
                from huggingface_hub import snapshot_download as _hf_dl
                _hf_dl("JustFrederik/nllb-200-distilled-600M-ct2-int8",
                       local_dir=NLLB_MODEL_DIR)
                print(f"{C_OK}✓{RESET}")
            except Exception as _e:
                print(f"\n  {C_HIGHLIGHT}[警告] 自動修復失敗: {_e}{RESET}")
        try:
            self.sp = sentencepiece.SentencePieceProcessor()
            self.sp.Load(os.path.join(NLLB_MODEL_DIR, "sentencepiece.bpe.model"))
            self.ct2 = ctranslate2.Translator(
                NLLB_MODEL_DIR, device="cpu", compute_type="int8"
            )
            print(f"{C_OK}{BOLD}完成！{RESET}")
        except Exception as e:
            print(f"\n{C_HIGHLIGHT}[錯誤] NLLB 模型載入失敗: {e}{RESET}", file=sys.stderr)
            print(f"  {C_DIM}請刪除模型後重新安裝：{RESET}")
            print(f"  {C_WHITE}rm -rf {NLLB_MODEL_DIR}{RESET}")
            print(f"  {C_WHITE}{_INSTALL_CMD}{RESET}")
            _webui_send({"type": "progress", "stage": "錯誤", "detail": f"NLLB 模型載入失敗: {e}"})
            raise

    def _translate_short(self, text):
        """翻譯單句"""
        tokens = self.sp.Encode(text, out_type=str)
        input_tokens = [self.src_lang] + tokens + ["</s>"]
        results = self.ct2.translate_batch(
            [input_tokens],
            target_prefix=[[self.tgt_lang]],
            beam_size=5,
            no_repeat_ngram_size=4,
            max_decoding_length=256,
        )
        output_tokens = results[0].hypotheses[0][1:]  # skip lang token
        return self.sp.Decode(output_tokens)

    _has_repetition = staticmethod(ArgosTranslator._has_repetition)

    def translate(self, text):
        text = text.strip()
        if not text:
            return ""
        import re
        sentences = re.split(r'(?<=[.!?,;。！？，；])\s*', text)
        translated_parts = []
        max_chars = 80
        buf = ""
        for sent in sentences:
            if buf and len(buf) + len(sent) > max_chars:
                part = self._translate_short(buf)
                if not self._has_repetition(part):
                    translated_parts.append(part)
                buf = sent
            else:
                buf = (buf + " " + sent).strip() if buf else sent
        if buf:
            part = self._translate_short(buf)
            if self._has_repetition(part):
                for s in re.split(r'(?<=[.!?,;。！？，；])\s*', buf):
                    s = s.strip()
                    if not s:
                        continue
                    p = self._translate_short(s)
                    if not self._has_repetition(p):
                        translated_parts.append(p)
            else:
                translated_parts.append(part)
        result = " ".join(translated_parts)
        if self.direction in ("en2zh", "ja2zh"):
            return S2TWP.convert(result)
        return result


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
    import tempfile
    user = rw_cfg.get("ssh_user", "root")
    host = rw_cfg.get("host", "localhost")
    port = rw_cfg.get("ssh_port", 22)
    # Windows 檔名不可含 ':'，統一用 '_' 分隔
    sock_name = f"jt-ssh-cm-{user}@{host}_{port}"
    return os.path.join(tempfile.gettempdir(), sock_name)


def _ssh_cmd_parts(rw_cfg):
    """組合 SSH 指令片段（含 key / port / ControlMaster 多工）"""
    parts = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(rw_cfg.get("ssh_port", 22))]
    # Windows OpenSSH 不支援 ControlMaster
    if not IS_WINDOWS:
        ctrl_sock = _ssh_ctrl_sock(rw_cfg)
        parts += ["-o", f"ControlMaster=auto", "-o", f"ControlPath={ctrl_sock}",
                  "-o", "ControlPersist=300"]
    ssh_key = rw_cfg.get("ssh_key", "")
    if ssh_key:
        key_path = os.path.expanduser(ssh_key)
        if os.path.isfile(key_path):
            parts += ["-i", key_path]
    parts.append(f"{rw_cfg['ssh_user']}@{rw_cfg['host']}")
    return parts


def _ssh_close_cm(rw_cfg):
    """關閉 SSH ControlMaster 多工連線"""
    if IS_WINDOWS:
        return
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
    呼叫前須先 print(..., end="", flush=True) 輸出開頭文字。"""
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
    """SSH nohup 啟動伺服器 Whisper server（允許互動輸入密碼）。
    若伺服器已在執行且 force_restart=False，則跳過重啟直接沿用。"""
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    host = rw_cfg["host"]
    # 先檢查伺服器是否已在執行（支援多實例共用同一個伺服器）
    if not force_restart:
        try:
            url = f"http://{host}:{port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    return  # 伺服器已在執行，直接沿用
        except Exception:
            pass  # 伺服器未執行或無回應，先清理再啟動
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
    """SSH pkill 停止伺服器 Whisper server，並關閉 SSH 多工連線"""
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    cmd = _ssh_cmd_parts(rw_cfg) + [f"pkill -f 'server.py --port {port}'"]
    try:
        subprocess.run(cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _ssh_close_cm(rw_cfg)


def _remote_whisper_models(rw_cfg, timeout=5):
    """查詢伺服器已快取的 Whisper 模型清單"""
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
    """輪詢 /health 等待伺服器 server 就緒，回傳 (ok, has_gpu)
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
    """查詢伺服器 /v1/status，回傳 dict 或 None（連線失敗）"""
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
    """上傳前檢查伺服器狀態：忙碌 / 磁碟空間。
    回傳 True 可繼續，False 使用者取消（降級本機）。"""
    status = _remote_whisper_status(rw_cfg)
    if status is None:
        return True  # 舊版 server 沒有 /v1/status，略過檢查

    # 磁碟空間檢查（至少需要檔案大小的 3 倍 + 500MB 餘裕）
    need_gb = max((file_size_bytes * 3) / (1024 ** 3), 0.5)
    disk_free = status.get("disk_free_gb", 999)
    if disk_free < need_gb:
        print(f"\n  {C_HIGHLIGHT}[警告] 伺服器磁碟空間不足：{disk_free} GB 可用（需要約 {need_gb:.1f} GB）{RESET}")
        print(f"  {C_DIM}請清理伺服器 /tmp 或磁碟空間後再試{RESET}")
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

        print(f"\n  {C_HIGHLIGHT}[忙碌] 伺服器正在執行{task_desc}{source}{RESET}")
        print(f"  {C_DIM}模型: {model}，已執行 {mins}:{secs:02d}{RESET}")
        print()
        print(f"  {C_DIM}[1]{RESET} {C_WHITE}等候（每 5 秒重試）{RESET}")
        print(f"  {C_DIM}[2]{RESET} {C_WHITE}強制中斷伺服器作業（可能是殘留的已斷線作業）{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}改用本機 辨識{RESET}")
        print(f"{C_WHITE}選擇 (1-3) [1]：{RESET}", end=" ")

        try:
            choice = input().strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice == "2":
            # 強制重啟伺服器 server
            print(f"  {C_DIM}正在重啟伺服器...{RESET}", end="", flush=True)
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
            print(f"  {C_DIM}等候伺服器...{RESET}", flush=True)
            while True:
                time.sleep(5)
                st = _remote_whisper_status(rw_cfg)
                if st is None or not st.get("busy"):
                    print(f"  {C_OK}→ 伺服器已就緒{RESET}")
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
    """POST 音訊到伺服器 /v1/audio/transcriptions（串流 NDJSON），回傳 (segments, duration, proc_time, device)"""
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
                        raise RuntimeError(f"伺服器辨識錯誤: {event.get('detail', '未知錯誤')}")
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
        raise RuntimeError(f"伺服器錯誤 ({e.code}): {detail or e.reason}") from e

    return segments, duration, proc_time, device


def _remote_whisper_transcribe_bytes(rw_cfg, wav_bytes, model, language, timeout=120):
    """POST 記憶體中的 WAV bytes 到伺服器 /v1/audio/transcriptions
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
    """POST 音訊 + segments 到伺服器 /v1/audio/diarize
    回傳 (speaker_labels, proc_time) 或失敗回傳 (None, 0)"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/diarize"

    # 先檢查伺服器是否支援 diarize
    try:
        health_url = f"http://{host}:{port}/health"
        req_h = urllib.request.Request(health_url)
        with urllib.request.urlopen(req_h, timeout=10) as resp_h:
            health_data = json.loads(resp_h.read().decode())
        if not health_data.get("diarize", False):
            print(f"  {C_HIGHLIGHT}[伺服器] 伺服器未安裝 resemblyzer/spectralcluster{RESET}")
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
        print(f"  {C_HIGHLIGHT}[伺服器 diarize] 伺服器錯誤 ({e.code}): {detail or e.reason}{RESET}")
        return None, 0
    except Exception as e:
        print(f"  {C_HIGHLIGHT}[伺服器 diarize] 連線失敗: {e}{RESET}")
        return None, 0

    speaker_labels = data.get("speaker_labels")
    proc_time = data.get("processing_time", 0)
    n_spk = data.get("num_speakers", 0)
    device = data.get("device", "unknown")
    print(f"  {C_DIM}[伺服器 diarize] {n_spk} 位講者, {proc_time}s ({device}){RESET}")
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


def select_translator(init_host=None, init_port=None, mode="en2zh"):
    """讓用戶選擇翻譯引擎和模型，回傳 (engine, model, host, port, server_type)"""
    host = init_host or OLLAMA_HOST
    port = init_port or OLLAMA_PORT

    print(f"\n\n{C_TITLE}{BOLD}▎ 翻譯引擎{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_DIM}* 要更強的翻譯能力，請搭配 LLM 伺服器與適當模型效果才好{RESET}")

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
            _nllb_ok = os.path.isdir(NLLB_MODEL_DIR)
            _argos_ok = mode == "en2zh" and os.path.isdir(ARGOS_PKG_PATH)
            _offline_opts = []
            if _nllb_ok:
                _offline_opts.append(("NLLB 本機離線", "支援中日英，品質一般", "nllb"))
            if _argos_ok:
                _offline_opts.append(("Argos 本機離線", "僅英翻中，品質一般", "argos"))
            if len(_offline_opts) == 0:
                print(f"  {C_ERR}[錯誤] 未偵測到 LLM 伺服器{RESET}")
                if mode != "en2zh":
                    print(f"  {C_WHITE}此模式無離線翻譯可用，請設定 LLM 伺服器或執行 {_INSTALL_CMD} 安裝 NLLB{RESET}")
                else:
                    print(f"  {C_WHITE}請輸入 LLM 伺服器位址，或執行 {_INSTALL_CMD} 安裝離線翻譯模型{RESET}")
                sys.exit(1)
            elif len(_offline_opts) == 1:
                # 只有一個離線引擎，直接選用
                _ol, _od, _oe = _offline_opts[0]
                print(f"  {C_OK}→ {_ol}{RESET}\n")
                return _oe, None, None, None, None
            else:
                # 多個離線引擎，顯示選單讓使用者選擇
                print(f"\n  {C_WHITE}可用的離線翻譯引擎：{RESET}")
                for i, (_ol, _od, _oe) in enumerate(_offline_opts):
                    if i == 0:
                        print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {_ol}{RESET}  {C_WHITE}{_od}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                    else:
                        print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{_ol}{RESET}  {C_DIM}{_od}{RESET}")
                print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")
                try:
                    _sel = input().strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)
                _sel_idx = 0
                if _sel.isdigit() and 0 <= int(_sel) < len(_offline_opts):
                    _sel_idx = int(_sel)
                _ol, _od, _oe = _offline_opts[_sel_idx]
                print(f"  {C_OK}→ {_ol}{RESET}\n")
                return _oe, None, None, None, None

    srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
    print(f"{C_OK}{BOLD}{srv_label}（{len(available_models)} 個模型）{RESET}")

    # 記住成功連線的位址
    if host != OLLAMA_HOST or port != OLLAMA_PORT:
        _config["llm_host"] = host
        _config["llm_port"] = port
        _config.pop("ollama_host", None)
        _config.pop("ollama_port", None)
        save_config(_config)

    # 建立選項列表（按名稱排序）
    _last_model = _config.get("last_llm_model")
    options = []
    if server_type == "ollama":
        for model_name in sorted(available_models):
            desc = next((d for n, d in OLLAMA_MODELS if n == model_name), "")
            options.append((f"Ollama {model_name}", desc, "llm", model_name))
    else:
        for model_name in sorted(available_models):
            options.append((model_name, "", "llm", model_name))
    if os.path.isdir(NLLB_MODEL_DIR):
        options.append(("NLLB 本機離線", "支援中日英，品質一般，免 LLM 伺服器", "nllb", None))
    if mode == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
        options.append(("Argos 本機離線", "僅英翻中，品質一般，免 LLM 伺服器", "argos", None))

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


def _clean_backspace(raw: bytes) -> str:
    """處理 raw bytes 中的 backspace，並丟棄殘留的不完整 UTF-8 位元組。

    macOS 終端機 canonical mode 下按 backspace：
      情況 A：\x7f 仍在 raw bytes 中 → 逐 byte 處理，刪除前一個完整 UTF-8 字元
      情況 B：核心已消耗 \x7f 但只刪 1 byte（非整個多位元組字元）→ 殘留孤立位元組
    兩種情況都由 decode(..., errors='ignore') 處理：A 先清 \x7f，B 直接跳過壞序列。
    """
    buf = bytearray()
    for b in raw:
        if b in (0x7F, 0x08):
            # 刪除前一個完整 UTF-8 字元（1~4 bytes）
            while buf and (buf[-1] & 0xC0) == 0x80:
                buf.pop()  # 移除 continuation bytes (10xxxxxx)
            if buf:
                buf.pop()  # 移除 leading byte
        else:
            buf.append(b)
    return bytes(buf).decode('utf-8', errors='ignore').strip()


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
                         "zh2en": ("中文轉錄+英文翻譯", "中文語音 → 轉錄並翻譯成英文"),
                         "ja2zh": ("日文轉錄+中文翻譯", "日文語音 → 轉錄並翻譯成繁體中文"),
                         "zh2ja": ("中文轉錄+日文翻譯", "中文語音 → 轉錄並翻譯成日文"),
                         "en_zh": ("英中雙向轉錄+翻譯", "系統音訊(英→中) + 麥克風(中→英)，需配對兩個檔案"),
                         "ja_zh": ("日中雙向轉錄+翻譯", "系統音訊(日→中) + 麥克風(中→日)，需配對兩個檔案")}
        input_modes = [
            (k, _input_labels[k][0], _input_labels[k][1]) if k in _input_labels else (k, n, d)
            for k, n, d in MODE_PRESETS if k != "record"
        ]

        print(f"\n\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
        col = max(_dw(name) for _, name, _ in input_modes) + 2
        _input_group_headers = {0: "單向翻譯", 4: "雙向翻譯", 6: "轉錄"}
        for i, (key, name, desc) in enumerate(input_modes):
            if i in _input_group_headers:
                hdr = _input_group_headers[i]
                hdr_w = _dw(hdr)
                print(f"{C_DIM}{'─' * 12} {hdr} {'─' * (60 - 13 - hdr_w)}{RESET}")
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
        is_chinese = mode_key in _NOENG_MODELS
        need_translate = mode_key in _TRANSLATE_MODES

        # ── 第二步：辨識位置（先選位置，再依位置推薦模型）──
        use_remote_whisper = False
        remote_cached_models = set()
        if REMOTE_WHISPER_CONFIG:
            rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
            location_options = [
                (f"GPU 伺服器（{rw_host}，速度快 5-10 倍）", ""),
                ("本機", ""),
            ]
            default_loc = 0
        else:
            location_options = [
                ("本機", ""),
                ("GPU 伺服器（尚未設定）", ""),
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
        else:
            if loc_idx == 1:
                print(f"  {C_HIGHLIGHT}[提示] GPU 伺服器 辨識尚未設定，請執行 {_INSTALL_CMD} 進行設定{RESET}")
                print(f"  {C_DIM}本次將使用本機 辨識{RESET}")
            use_remote_whisper = False

        # 查詢伺服器已快取的模型（選了伺服器才查）
        if use_remote_whisper:
            remote_cached_models = _remote_whisper_models(REMOTE_WHISPER_CONFIG, timeout=3)

        # ── 第三步前：辨識模型（依位置推薦）──
        available_models = []
        for name, _filename, desc in WHISPER_MODELS:
            if is_chinese and name.endswith(".en"):
                continue
            available_models.append((name, desc))
        # 預設：GPU 伺服器推薦 large-v3-turbo，本機按 CPU 推薦
        if use_remote_whisper:
            recommended = "large-v3-turbo"
        else:
            recommended = _recommended_whisper_model(mode_key)
        default_fw = 0
        for i, (name, _) in enumerate(available_models):
            if name == recommended:
                default_fw = i
                break

        print(f"\n\n{C_TITLE}{BOLD}▎ 辨識模型{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        col = max(len(name) for name, _ in available_models) + 2
        dcol = max(_str_display_width(desc) for _, desc in available_models) + 2
        for i, (name, desc) in enumerate(available_models):
            padded = name + ' ' * (col - len(name))
            dpadded = desc + ' ' * (dcol - _str_display_width(desc))
            # 伺服器快取標記
            cache_tag = ""
            if remote_cached_models:
                if name in remote_cached_models:
                    cache_tag = f" {C_OK}✓{RESET}"
                else:
                    cache_tag = f" {C_DIM}(需下載){RESET}"
            # 裝置適合標記
            fit = _whisper_model_fit_label(name, recommended, has_remote=use_remote_whisper)
            fit_tag = f" {C_OK}({fit}){RESET}" if fit else ""
            if i == default_fw:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{dpadded}{RESET}{cache_tag}{fit_tag}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{dpadded}{RESET}{cache_tag}{fit_tag}")
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

        # 警告：選了伺服器但模型未快取
        if use_remote_whisper and remote_cached_models and fw_model not in remote_cached_models:
            print(f"  {C_HIGHLIGHT}[注意] 模型 {fw_model} 尚未下載到伺服器，首次辨識需要先下載（可能需數分鐘）{RESET}")

        # ── 第三步：LLM 伺服器 + 翻譯模型（僅翻譯模式）──
        ollama_model = None
        ollama_host = OLLAMA_HOST
        ollama_port = OLLAMA_PORT
        ollama_asked = False
        llm_server_type = None
        _use_nllb = False
        _use_argos = False

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
                    if os.path.isdir(NLLB_MODEL_DIR):
                        print(f"  {C_OK}→ 改用 NLLB 本機離線翻譯{RESET}")
                        _use_nllb = True
                    elif mode_key == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
                        print(f"  {C_OK}→ 改用 Argos 本機離線翻譯{RESET}")
                        _use_argos = True
                    else:
                        print(f"  {C_HIGHLIGHT}⚠ 翻譯功能需要 LLM 伺服器或離線翻譯模型，請確認伺服器已啟動或執行 {_INSTALL_CMD} 安裝 NLLB{RESET}")
            else:
                llm_models = []
                if os.path.isdir(NLLB_MODEL_DIR):
                    print(f"  {C_OK}→ NLLB 本機離線翻譯{RESET}")
                    _use_nllb = True
                elif mode_key == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
                    print(f"  {C_OK}→ Argos 本機離線翻譯{RESET}")
                    _use_argos = True
                else:
                    print(f"  {C_ERR}[錯誤] 未設定 LLM 伺服器，離線翻譯模型也未安裝{RESET}")

            # 日文模式不支援 Argos
            if _use_argos and mode_key in ("ja2zh", "zh2ja"):
                print(f"  {C_HIGHLIGHT}[警告] 日文翻譯不支援 Argos，將只做轉錄（不翻譯）{RESET}")
                _use_argos = False
                need_translate = False

            if not _use_argos and not _use_nllb:
                # 翻譯模型：動態查詢伺服器模型 + 本機離線選項
                all_translate_models = _llm_list_models(ollama_host, ollama_port, llm_server_type or "ollama")
                translate_models = []  # (name, desc, engine)
                for m_name in all_translate_models:
                    desc = next((d for n, d in OLLAMA_MODELS if n == m_name), "")
                    translate_models.append((m_name, desc, "llm"))
                if not translate_models:
                    translate_models = [(n, d, "llm") for n, d in OLLAMA_MODELS]
                    if not llm_server_type:
                        llm_server_type = "ollama"
                _llm_count = len(translate_models)

                # 加入本機離線翻譯選項
                if os.path.isdir(NLLB_MODEL_DIR):
                    translate_models.append(("NLLB 本機離線翻譯", "支援中日英互譯，免 LLM 伺服器", "nllb"))
                if mode_key == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
                    translate_models.append(("Argos 本機離線翻譯", "僅英翻中，免 LLM 伺服器", "argos"))

                _last_tm = _config.get("last_llm_model")
                default_ollama = 0
                for i, (name, _, eng) in enumerate(translate_models):
                    if name == "qwen2.5:14b" and eng == "llm":
                        default_ollama = i
                        break

                def _dw_tm(s):
                    return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

                col = max(_dw_tm(name) for name, _, _ in translate_models) + 2
                print(f"\n\n{C_TITLE}{BOLD}▎ 翻譯模型{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                for i, (name, desc, eng) in enumerate(translate_models):
                    # LLM 模型與本機選項之間印分隔線
                    if i == _llm_count and _llm_count > 0:
                        print(f"  {C_DIM}{'─' * 56}{RESET}")
                    padded = name + ' ' * (col - _dw_tm(name))
                    tags = []
                    if i == default_ollama:
                        tags.append(f"{C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                    if eng == "llm" and name == _last_tm:
                        tags.append(f"{C_OK}{REVERSE} 前次使用 {RESET}")
                    tag_str = " ".join(tags)
                    if i == default_ollama:
                        print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {tag_str}")
                    else:
                        print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}  {tag_str}")
                # 檢查推薦翻譯模型是否存在於伺服器
                _rec_tm = {n for n, _ in _BUILTIN_TRANSLATE_MODELS}
                _avail_tm = {n for n, _, e in translate_models if e == "llm"}
                if not _rec_tm & _avail_tm:
                    _rec_tm_list = " / ".join(n for n, _ in _BUILTIN_TRANSLATE_MODELS)
                    print(f"  {C_HIGHLIGHT}注意：本 LLM 伺服器未安裝推薦翻譯模型（{_rec_tm_list}），翻譯品質可能不如預期{RESET}")
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

                _sel_name, _sel_desc, _sel_engine = translate_models[o_idx]
                if _sel_engine == "nllb":
                    ollama_model = None
                    _use_nllb = True
                elif _sel_engine == "argos":
                    ollama_model = None
                    _use_argos = True
                else:
                    ollama_model = _sel_name
                    # 記住本次使用的翻譯模型
                    if ollama_model != _config.get("last_llm_model"):
                        _config["last_llm_model"] = ollama_model
                        save_config(_config)

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
        # 非翻譯模式時，前面未偵測 LLM 伺服器，在此靜默偵測（摘要需要 LLM）
        if not need_translate and ollama_host and llm_server_type is None:
            llm_server_type, _ = _check_llm_server(ollama_host, ollama_port)
        _has_llm = llm_server_type is not None and ollama_host is not None
        if _has_llm:
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
        else:
            # 沒有 LLM 伺服器 → 只能產出逐字稿，摘要/校正需要 LLM
            summary_mode = "transcript"
            do_summarize = True
            print(f"\n  {C_DIM}（未連線 LLM 伺服器，僅產出逐字稿；摘要與校正需要 LLM）{RESET}")

        # 選了摘要 → 先確認 LLM 伺服器（若翻譯步驟未問過）→ 選摘要模型
        summary_model = SUMMARY_DEFAULT_MODEL
        if do_summarize and summary_mode != "transcript":
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

        # 記住 LLM 伺服器位址（只在連線成功時才存）
        if llm_server_type and (ollama_host != OLLAMA_HOST or ollama_port != OLLAMA_PORT):
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
            topic_input = _clean_backspace(raw)
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
            print(f"  {C_OK}  辨識位置: GPU 伺服器（{rw_h}）{RESET}")
        if ollama_model:
            print(f"  {C_OK}  翻譯模型: {ollama_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        elif _use_nllb:
            print(f"  {C_OK}  翻譯引擎: NLLB 本機離線翻譯{RESET}")
        elif _use_argos:
            print(f"  {C_OK}  翻譯引擎: Argos 本機離線翻譯{RESET}")
        if diarize_desc != "關閉" and use_remote_whisper:
            rw_h2 = REMOTE_WHISPER_CONFIG.get("host", "?")
            diarize_desc += f"，GPU 伺服器（{rw_h2}）"
        elif diarize_desc != "關閉":
            diarize_desc += "，本機"
        print(f"  {C_OK}  講者辨識: {diarize_desc}{RESET}")
        if do_summarize and summary_mode != "transcript":
            print(f"  {C_OK}  摘要模型: {summary_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        elif do_summarize:
            print(f"  {C_OK}  輸出: 逐字稿{RESET}")
        if meeting_topic:
            print(f"  {C_OK}  會議主題: {meeting_topic}{RESET}")
        print()

        # 決定翻譯引擎
        if ollama_model:
            translate_engine = "llm"
        elif _use_nllb:
            translate_engine = "nllb"
        elif _use_argos:
            translate_engine = "argos"
        else:
            translate_engine = None

        return (mode_key, fw_model, ollama_model, summary_model,
                ollama_host, ollama_port, diarize, num_speakers, do_summarize,
                llm_server_type, use_remote_whisper, meeting_topic, summary_mode,
                translate_engine)

    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def run_stream(capture_id: int, translator, model_name: str, model_path: str,
               length_ms: int = 5000, step_ms: int = 3000, mode: str = "en2zh",
               record: bool = False, rec_device: int = None,
               meeting_topic: str = None):
    """啟動 whisper-stream 子程序並即時翻譯輸出"""

    whisper_lang = "en" if mode in _EN_INPUT_MODES else ("ja" if mode in _JA_INPUT_MODES else "zh")
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
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿", "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿", "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿"}
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
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        import sounddevice as sd
        import numpy as np
        # 使用指定的錄音裝置，或自動找 Loopback 裝置
        rec_dev_id = rec_device
        if rec_dev_id is None:
            # Windows: 優先用 WASAPI Loopback
            if IS_WINDOWS:
                wb_info = _find_wasapi_loopback()
                if wb_info:
                    rec_dev_id = WASAPI_LOOPBACK_ID
            if rec_dev_id is None:
                sd_devices = sd.query_devices()
                for i, dev in enumerate(sd_devices):
                    if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
                        rec_dev_id = i
                        break
            if rec_dev_id is None:
                rec_dev_id = sd.default.device[0]
        if IS_WINDOWS and rec_dev_id == WASAPI_MIXED_ID:
            # Windows 混合錄音（Loopback + 麥克風）
            _stop_ev = threading.Event()
            _mixed = _setup_mixed_recording(_stop_ev, meeting_topic)
            if _mixed:
                recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
            else:
                rec_dev_id = WASAPI_LOOPBACK_ID  # 降級
        if IS_WINDOWS and rec_dev_id == WASAPI_LOOPBACK_ID:
            wb_info = _find_wasapi_loopback()
            rec_sr = int(wb_info["defaultSampleRate"])
            rec_ch = wb_info["maxInputChannels"]
            recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

            def rec_callback(indata, frames, time_info, status):
                recorder.write_raw(indata)
                _push_rms(float(np.sqrt(np.mean(indata ** 2))))

            try:
                rec_stream = _WasapiLoopbackStream(
                    callback=rec_callback, samplerate=rec_sr,
                    channels=rec_ch, blocksize=int(rec_sr * 0.1))
            except Exception as e:
                print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_dev_id}]: {e}{RESET}")
                print(f"  {C_DIM}跳過錄音，繼續辨識。如需錄音請重啟程式。{RESET}")
                recorder.close()
                recorder = None
                rec_stream = None
        elif _mixer is None:
            dev_info = sd.query_devices(rec_dev_id)
            rec_sr = int(dev_info["default_samplerate"])
            rec_ch = max(dev_info["max_input_channels"], 1)
            recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

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
        elif isinstance(translator, NllbTranslator):
            print(f"  {C_OK}翻譯引擎: NLLB 本機離線{RESET}")
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
        **_SUBPROCESS_FLAGS,
    )

    # 啟動錄音串流（在 subprocess 啟動後）
    if rec_stream:
        rec_stream.start()
    if _rec_stream_mic:
        _rec_stream_mic.start()

    stop_keypress = threading.Event()
    pause_event = threading.Event()
    global _webui_pause_event; _webui_pause_event = pause_event
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
    _sigint_count_ws = [0]

    def signal_handler(signum, frame):
        _sigint_count_ws[0] += 1
        if _sigint_count_ws[0] >= 2:
            _force_exit(1)
        clear_status_bar()
        restore_terminal()
        stop_keypress.set()
        _stop_audio_monitor(audio_monitor)
        # 停止錄音
        if _rec_stream_mic:
            try:
                _rec_stream_mic.stop()
                _rec_stream_mic.close()
            except Exception:
                pass
        if rec_stream:
            try:
                rec_stream.stop()
                rec_stream.close()
            except Exception:
                pass
        if _mixer:
            _mixer.flush_remaining()
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "正在停止", "detail": ""})
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 清理暫存檔
        if os.path.exists(output_file):
            os.remove(output_file)
        _force_exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 監控 whisper-stream 的 stderr 來偵測啟動狀態
    # 等待模型載入完成
    print(f"{C_DIM}正在載入 whisper 模型（首次可能需要幾秒）...{RESET}", flush=True)
    _webui_send({"type": "progress", "stage": "載入中", "detail": "whisper 模型"})

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
        "ja2zh": "說日文即可看到中文翻譯",
        "zh2ja": "說中文即可看到日文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
        "ja": "說日文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)
    _webui_send({"type": "progress", "stage": "", "detail": ""})
    _webui_send({"type": "started", "mode": mode})

    # 設定底部固定狀態列（快捷鍵提示 + 即時資訊）
    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("NLLB" if isinstance(translator, NllbTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else ""))
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, (ArgosTranslator, NllbTranslator)) else "")
    setup_status_bar(mode, model_name=model_name, asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    if hasattr(signal, 'SIGWINCH'):
        signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # 被動音量監控（Whisper 無錄音時，開輕量 stream 讀 BlackHole 給狀態列波形）
    if not record:
        audio_monitor = _start_audio_monitor()

    # 非同步翻譯：英文立刻顯示，中文在背景翻完再補上（有序輸出）
    print_lock = threading.Lock()
    _trans_seq = [0]       # 遞增序號
    _trans_pending = {}    # seq → (src_text, result, elapsed, asr_elapsed)
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
            src_text, result, elapsed, asr_elapsed = entry
            if not result:
                continue
            src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
            with print_lock:
                # 原文 + 辨識耗時
                _print_with_badge(f"{src_color}[{src_label}] {src_text}{RESET}",
                                  C_BADGE_ASR, asr_elapsed, "辨")
                # 翻譯 + 翻譯耗時
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}",
                                  _speed_badge_color(elapsed), elapsed, "譯")
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            # 寫入記錄檔
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")
            _webui_send({"type": "transcription", "source": "main",
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp})

    def translate_and_print(seq, src_text, log_path, asr_elapsed=0):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if not isinstance(translator, OllamaTranslator): result = S2TWP.convert(result)
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed, asr_elapsed)
        _drain_translations(log_path)

    # 持續讀取輸出檔案的新內容
    last_size = 0
    last_translated = ""
    buffer = ""
    _loop_tick = 0
    _last_output_time = [time.monotonic()]  # whisper-stream ASR 耗時近似

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

                    # whisper-stream 無法直接取得 ASR 耗時，
                    # 用「兩次有效輸出的間隔」近似
                    _asr_elapsed = time.monotonic() - _last_output_time[0]
                    _last_output_time[0] = time.monotonic()

                    if mode in _EN_INPUT_MODES:
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
                            _webui_send({"type": "transcription", "source": "main",
                                         "src_lang": "EN", "src_text": line,
                                         "asr_time": round(_asr_elapsed, 1),
                                         "timestamp": timestamp})
                        else:
                            # 英翻中：原文延後到翻譯完成時一起顯示
                            last_translated = line
                            seq = _trans_seq[0]; _trans_seq[0] += 1
                            t = threading.Thread(
                                target=translate_and_print,
                                args=(seq, line, log_path, _asr_elapsed),
                                daemon=True,
                            )
                            t.start()

                    elif mode in _JA_INPUT_MODES:
                        # 日文模式：過濾日文幻覺
                        if _is_ja_hallucination(line):
                            continue
                        if line == last_translated:
                            continue
                        if mode == "ja":
                            _src_c, _src_l = _MODE_LABELS["ja"][0], _MODE_LABELS["ja"][1]
                            with print_lock:
                                print(f"{_src_c}{BOLD}[{_src_l}] {line}{RESET}", flush=True)
                                print(flush=True)
                                _status_bar_state["count"] += 1
                                refresh_status_bar()
                            last_translated = line
                            timestamp = time.strftime("%H:%M:%S")
                            with open(log_path, "a", encoding="utf-8") as log_f:
                                log_f.write(f"[{timestamp}] [{_src_l}] {line}\n\n")
                            _webui_send({"type": "transcription", "source": "main",
                                         "src_lang": _src_l, "src_text": line,
                                         "asr_time": round(_asr_elapsed, 1),
                                         "timestamp": timestamp})
                        else:
                            # ja2zh：原文延後到翻譯完成時一起顯示
                            last_translated = line
                            seq = _trans_seq[0]; _trans_seq[0] += 1
                            t = threading.Thread(
                                target=translate_and_print,
                                args=(seq, line, log_path, _asr_elapsed),
                                daemon=True,
                            )
                            t.start()

                    elif mode in ("zh2en", "zh2ja"):
                        # 中文輸入翻譯模式：中文輸入過濾 + 翻譯
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
                        seq = _trans_seq[0]; _trans_seq[0] += 1
                        t = threading.Thread(
                            target=translate_and_print,
                            args=(seq, line, log_path, _asr_elapsed),
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
                        _webui_send({"type": "transcription", "source": "main",
                                     "src_lang": "中", "src_text": line,
                                     "asr_time": round(_asr_elapsed, 1),
                                     "timestamp": timestamp})

            time.sleep(0.1)

        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)

    # 恢復終端機
    clear_status_bar()
    restore_terminal()
    stop_keypress.set()
    _stop_audio_monitor(audio_monitor)

    # 停止錄音
    if _rec_stream_mic:
        try:
            _rec_stream_mic.stop()
            _rec_stream_mic.close()
        except Exception:
            pass
    if rec_stream:
        try:
            rec_stream.stop()
            rec_stream.close()
        except Exception:
            pass
    if _mixer:
        _mixer.flush_remaining()
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
    _webui_send({"type": "progress", "stage": "載入中", "detail": f"Moonshine 模型（{moonshine_model_name}）"})
    model_path, model_arch = get_model_for_language("en", arch)

    # 翻譯記錄檔
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿"}
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
        elif isinstance(translator, NllbTranslator):
            print(f"  {C_OK}翻譯引擎: NLLB 本機離線{RESET}")
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
    global _webui_pause_event; _webui_pause_event = pause_event
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
            src_text, result, elapsed, asr_elapsed = entry
            if not result:
                continue
            src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
            with print_lock:
                _clear_partial_line()  # 清除 [...] 部分文字
                # 原文 + 辨識耗時
                _print_with_badge(f"{src_color}[{src_label}] {src_text}{RESET}",
                                  C_BADGE_ASR, asr_elapsed, "辨")
                # 翻譯 + 翻譯耗時
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}",
                                  _speed_badge_color(elapsed), elapsed, "譯")
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")
            _webui_send({"type": "transcription", "source": "main",
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp})

    def translate_and_print(seq, src_text, log_path, asr_elapsed=0):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if not isinstance(translator, OllamaTranslator): result = S2TWP.convert(result)
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed, asr_elapsed)
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

    # Moonshine ASR 計時：從首次 partial 到 completed
    _ms_line_start = {}  # line_id → monotonic time

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
            # 記錄此行首次辨識的時間
            lid = event.line.line_id
            if lid not in _ms_line_start:
                _ms_line_start[lid] = time.monotonic()
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

            # 計算 ASR 耗時
            lid = event.line.line_id
            t_start = _ms_line_start.pop(lid, None)
            asr_elapsed = (time.monotonic() - t_start) if t_start else 0

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
                    _webui_send({"type": "transcription", "source": "main",
                                 "src_lang": "EN", "src_text": text,
                                 "asr_time": round(asr_elapsed, 1),
                                 "timestamp": timestamp})
                else:
                    # en2zh：原文延後到翻譯完成時一起顯示
                    with print_lock:
                        _clear_partial_line()
                    last_translated = text
                    seq = _trans_seq[0]; _trans_seq[0] += 1
                    t = threading.Thread(
                        target=translate_and_print,
                        args=(seq, text, log_path, asr_elapsed),
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
    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        sd_samplerate = int(wb_info["defaultSampleRate"])
        sd_channels = min(wb_info["maxInputChannels"], 2)
    else:
        dev_info = sd.query_devices(capture_id)
        sd_samplerate = int(dev_info["default_samplerate"])
        sd_channels = min(dev_info["max_input_channels"], 2)

    # 建立錄音
    rec_stream = None
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        # 錄音裝置與 ASR 裝置可能不同（例如聚集裝置含麥克風+BlackHole）
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            if IS_WINDOWS and rec_device == WASAPI_MIXED_ID:
                # Windows 混合錄音（Loopback + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅 Loopback
                    rec_device = WASAPI_LOOPBACK_ID
            if rec_device == WASAPI_LOOPBACK_ID and IS_WINDOWS:
                wb_rec = _find_wasapi_loopback()
                rec_sr = int(wb_rec["defaultSampleRate"])
                rec_ch = wb_rec["maxInputChannels"]
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _WasapiLoopbackStream(
                        callback=rec_callback, samplerate=rec_sr,
                        channels=rec_ch, blocksize=int(rec_sr * 0.1))
                except Exception as e:
                    print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_device}]: {e}{RESET}")
                    print(f"  {C_DIM}跳過錄音，繼續辨識。如需錄音請重啟程式。{RESET}")
                    recorder.close()
                    recorder = None
                    rec_stream = None
                    use_separate_rec = False
            elif _mixer is None:
                # 非 Windows WASAPI 的獨立錄音裝置
                rec_info = sd.query_devices(rec_device)
                rec_sr = int(rec_info["default_samplerate"])
                rec_ch = max(rec_info["max_input_channels"], 1)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

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
            recorder = _AudioRecorder(sd_samplerate, topic=meeting_topic, mode=mode)
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

    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        sd_stream = _WasapiLoopbackStream(
            callback=audio_callback, samplerate=sd_samplerate,
            channels=sd_channels, blocksize=int(sd_samplerate * 0.1))
    else:
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
        if _rec_stream_mic:
            try:
                _rec_stream_mic.stop()
                _rec_stream_mic.close()
            except Exception:
                pass
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
        if _mixer:
            _mixer.flush_remaining()
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)

    # Signal handler
    _sigint_count_ms = [0]

    def signal_handler(signum, frame):
        _sigint_count_ms[0] += 1
        if _sigint_count_ms[0] >= 2:
            _force_exit(1)
        clear_status_bar()
        restore_terminal()
        _cleanup_moonshine()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "正在停止", "detail": ""})
        _force_exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 啟動音訊串流
    sd_stream.start()
    if rec_stream:
        rec_stream.start()
    if _rec_stream_mic:
        _rec_stream_mic.start()

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "en": "說英文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)
    _webui_send({"type": "progress", "stage": "", "detail": ""})
    _webui_send({"type": "started", "mode": mode})

    # 設定狀態列
    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("NLLB" if isinstance(translator, NllbTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else ""))
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, (ArgosTranslator, NllbTranslator)) else "")
    setup_status_bar(mode, model_name=f"Moonshine {moonshine_model_name}", asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    if hasattr(signal, 'SIGWINCH'):
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
                      meeting_topic: str = None,
                      denoise: bool = False):
    """使用GPU 伺服器 Whisper 即時辨識：本機 sounddevice 擷取音訊 →
    環形緩衝 → 定期上傳 WAV 到伺服器 → 取回結果 → 翻譯顯示"""
    import numpy as np

    whisper_lang = "en" if mode in _EN_INPUT_MODES else ("ja" if mode in _JA_INPUT_MODES else "zh")

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # ── 啟動伺服器 + 預熱模型 ──
    rw_host = remote_cfg.get("host", "?")
    print(f"\n{C_TITLE}{BOLD}▎ GPU 伺服器{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    rs_label = "重啟" if force_restart else "啟動"
    print(f"  {C_DIM}{rs_label}伺服器 Whisper 伺服器（{rw_host}）...{RESET}", end="", flush=True)
    _webui_send({"type": "progress", "stage": "載入中", "detail": f"{rs_label} GPU 伺服器（{rw_host}）"})
    _inline_spinner(_remote_whisper_start, remote_cfg, force_restart=force_restart)
    print(f" {C_OK}✓{RESET}")
    print(f"  {C_DIM}等待伺服器就緒...{RESET}", end="", flush=True)
    _webui_send({"type": "progress", "stage": "載入中", "detail": "等待 GPU 伺服器就緒"})
    try:
        ok, has_gpu = _inline_spinner(_remote_whisper_health, remote_cfg, timeout=30)
    except Exception:
        ok, has_gpu = False, False
    if not ok:
        print(f" {C_HIGHLIGHT}失敗{RESET}")
        print(f"  {C_HIGHLIGHT}[錯誤] 伺服器 Whisper 伺服器無法連線（{rw_host}）{RESET}", file=sys.stderr)
        print(f"  {C_DIM}請確認伺服器設定，或使用 --local-asr 改用本機辨識{RESET}", file=sys.stderr)
        sys.exit(1)
    gpu_label = "GPU" if has_gpu else "CPU"
    print(f" {C_OK}就緒（{gpu_label}）{RESET}")
    # 預熱：送一段靜音讓伺服器載入模型到 GPU（首次可能需 30-60 秒）
    print(f"  {C_DIM}載入模型 {C_WHITE}{model_name}{C_DIM} 到 {gpu_label}（首次可能需 30-60 秒）...{RESET}", end="", flush=True)
    _webui_send({"type": "progress", "stage": "載入中", "detail": f"載入模型 {model_name} 到 {gpu_label}"})
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
        warmup_lang = "en" if mode in _EN_INPUT_MODES else ("ja" if mode in _JA_INPUT_MODES else "zh")
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
    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        sd_samplerate = int(wb_info["defaultSampleRate"])
        sd_channels = min(wb_info["maxInputChannels"], 2)
    else:
        dev_info = sd.query_devices(capture_id)
        sd_samplerate = int(dev_info["default_samplerate"])
        sd_channels = min(dev_info["max_input_channels"], 2)
    target_sr = 16000
    resample_ratio = sd_samplerate / target_sr  # e.g. 48000/16000 = 3

    stop_event = threading.Event()

    # ── 錄音 ──
    recorder = None
    rec_stream = None
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            if IS_WINDOWS and rec_device == WASAPI_MIXED_ID:
                # Windows 混合錄音（Loopback + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅 Loopback
                    rec_device = WASAPI_LOOPBACK_ID
            if rec_device == WASAPI_LOOPBACK_ID and IS_WINDOWS:
                wb_rec = _find_wasapi_loopback()
                rec_sr = int(wb_rec["defaultSampleRate"])
                rec_ch = wb_rec["maxInputChannels"]
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _WasapiLoopbackStream(
                        callback=rec_callback, samplerate=rec_sr,
                        channels=rec_ch, blocksize=int(rec_sr * 0.1))
                except Exception as e:
                    print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_device}]: {e}{RESET}")
                    recorder.close()
                    recorder = None
                    rec_stream = None
                    use_separate_rec = False
            elif _mixer is None:
                # 非 Windows WASAPI 的獨立錄音裝置
                rec_info = sd.query_devices(rec_device)
                rec_sr = int(rec_info["default_samplerate"])
                rec_ch = max(rec_info["max_input_channels"], 1)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

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
            recorder = _AudioRecorder(sd_samplerate, topic=meeting_topic, mode=mode)

    # ── Banner ──
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ GPU 伺服器（{rw_host}）{RESET}")
    if translator:
        if isinstance(translator, OllamaTranslator):
            _srv_type_label = "Ollama" if translator.server_type == "ollama" else "OpenAI 相容"
            print(f"  {C_OK}翻譯引擎: {translator.model} @ {translator.host}:{translator.port}（{_srv_type_label}）{RESET}")
        elif isinstance(translator, NllbTranslator):
            print(f"  {C_OK}翻譯引擎: NLLB 本機離線{RESET}")
        elif isinstance(translator, ArgosTranslator):
            print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    print(f"  {C_WHITE}音訊緩衝: {length_ms}ms / 步進 {step_ms}ms{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder:
        print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_WHITE}會議主題: {translator.meeting_topic}{RESET}")
    if denoise:
        print(f"  {C_OK}降噪: 已啟用（noisereduce）{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # ── 環形緩衝（16kHz mono float32）──
    ring_size = target_sr * length_ms // 1000  # e.g. 5s = 80000
    ring_buffer = np.zeros(ring_size, dtype=np.float32)
    ring_write_pos = 0
    ring_filled = 0  # 已寫入的總 sample 數
    ring_lock = threading.Lock()

    pause_event = threading.Event()
    global _webui_pause_event; _webui_pause_event = pause_event
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

    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        sd_stream = _WasapiLoopbackStream(
            callback=audio_callback, samplerate=sd_samplerate,
            channels=sd_channels, blocksize=int(sd_samplerate * 0.1))
    else:
        sd_stream = sd.InputStream(
            device=capture_id,
            samplerate=sd_samplerate,
            channels=sd_channels,
            blocksize=int(sd_samplerate * 0.1),
            dtype="float32",
            callback=audio_callback,
        )

    # ── 降噪 ──
    if denoise:
        from noisereduce import reduce_noise as _nr_reduce
        def _denoise(audio, sr):
            peak = np.max(np.abs(audio))
            out = _nr_reduce(y=audio, sr=sr, stationary=True, prop_decrease=0.8)
            peak_after = np.max(np.abs(out))
            if peak_after > 1e-6:
                out = out * (peak / peak_after)
            return out
    else:
        def _denoise(audio, sr):
            return audio

    # ── 提取 WAV bytes ──
    def extract_wav_bytes():
        """從環形緩衝提取正確順序的音訊，回傳 in-memory WAV bytes"""
        with ring_lock:
            pos = ring_write_pos
            buf_copy = ring_buffer.copy()
        # roll 使 write_pos 變成陣列末端（最新的在最後）
        ordered = np.roll(buf_copy, -pos)
        ordered = _denoise(ordered, target_sr)
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
            src_text, result, elapsed, asr_elapsed = entry
            if not result:
                continue
            src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
            with print_lock:
                # 原文 + 辨識耗時
                _print_with_badge(f"{src_color}[{src_label}] {src_text}{RESET}",
                                  C_BADGE_ASR, asr_elapsed, "辨")
                # 翻譯 + 翻譯耗時
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}",
                                  _speed_badge_color(elapsed), elapsed, "譯")
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(_log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")
            _webui_send({"type": "transcription", "source": "main",
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp})

    def translate_and_print(seq, src_text, _log_path, asr_elapsed=0):
        """背景執行緒：翻譯並按序號排隊輸出"""
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if not isinstance(translator, OllamaTranslator): result = S2TWP.convert(result)
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed, asr_elapsed)
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
                print(f"{C_DIM}  [伺服器辨識失敗: {e}]{RESET}", flush=True)
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
    if mode in _EN_INPUT_MODES:
        hallucination_check = _is_en_hallucination
    elif mode in _JA_INPUT_MODES:
        hallucination_check = _is_ja_hallucination
    else:
        hallucination_check = _is_zh_hallucination
    src_color, src_label = _MODE_LABELS[mode][0], _MODE_LABELS[mode][1]

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
            # 伺服器回傳可能含多個 segment，合併或逐段處理
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
                if mode in _ZH_INPUT_MODES:
                    line = S2TWP.convert(line)
                # 幻覺過濾
                if hallucination_check(line):
                    continue
                # 去重
                if is_duplicate(line):
                    continue
                recent_texts.append(line.lower().strip())
                # 顯示 + 翻譯
                if mode in _TRANSLATE_MODES and translator:
                    # 原文延後到翻譯完成時一起顯示，避免多段 [EN] 連續出現
                    seq = _trans_seq[0]; _trans_seq[0] += 1
                    threading.Thread(
                        target=translate_and_print,
                        args=(seq, line, log_path, proc_time),
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
                    _webui_send({"type": "transcription", "source": "main",
                                 "src_lang": src_label, "src_text": line,
                                 "asr_time": round(proc_time, 1), "timestamp": timestamp})

    # ── 清理 ──
    _cleaned_up = [False]

    def _cleanup_remote():
        if _cleaned_up[0]:
            return
        _cleaned_up[0] = True
        stop_event.set()
        if _rec_stream_mic:
            try:
                _rec_stream_mic.stop()
                _rec_stream_mic.close()
            except Exception:
                pass
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
        if _mixer:
            _mixer.flush_remaining()
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}✓ 錄音已儲存: {rec_path}{RESET}", flush=True)
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)
        # 伺服器保持執行（不停止，允許多實例共用）
        _ssh_close_cm(remote_cfg)

    _sigint_count_rm = [0]

    def signal_handler(signum, frame):
        _sigint_count_rm[0] += 1
        if _sigint_count_rm[0] >= 2:
            _force_exit(1)
        clear_status_bar()
        restore_terminal()
        _cleanup_remote()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "正在停止", "detail": ""})
        _force_exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── 啟動音訊串流 ──
    sd_stream.start()
    if rec_stream:
        rec_stream.start()
    if _rec_stream_mic:
        _rec_stream_mic.start()

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "zh2en": "說中文即可看到英文翻譯",
        "ja2zh": "說日文即可看到中文翻譯",
        "zh2ja": "說中文即可看到日文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
        "ja": "說日文即可看到字幕",
    }
    print(f"{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)
    _webui_send({"type": "progress", "stage": "", "detail": ""})
    _webui_send({"type": "started", "mode": mode})

    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("NLLB" if isinstance(translator, NllbTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else ""))
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, (ArgosTranslator, NllbTranslator)) else "")
    setup_status_bar(mode, model_name=model_name, asr_location="伺服器",
                     translate_model=_tr_model, translate_location=_tr_loc)
    if hasattr(signal, 'SIGWINCH'):
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


def run_stream_local_whisper(capture_id: int, translator, model_name: str,
                             mode: str = "en2zh",
                             length_ms: int = 5000, step_ms: int = 3000,
                             record: bool = False, rec_device: int = None,
                             meeting_topic: str = None,
                             denoise: bool = False):
    """Windows 專用：sounddevice/WASAPI 擷取音訊 → 本機 faster-whisper 即時辨識。
    架構類似 run_stream_remote()，但用本機 faster-whisper 取代遠端 HTTP 上傳。"""
    import numpy as np
    from faster_whisper import WhisperModel

    whisper_lang = "en" if mode in _EN_INPUT_MODES else ("ja" if mode in _JA_INPUT_MODES else "zh")

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # ── 載入 faster-whisper 模型 ──
    _fw_model_sizes = {"large-v3-turbo": "1.6GB", "large-v3": "3.1GB",
                       "medium.en": "1.5GB", "medium": "1.5GB",
                       "small.en": "500MB", "small": "500MB",
                       "base.en": "150MB"}
    _fw_need_download = False
    try:
        # 多路徑搜尋：HuggingFace 快取目錄 + 常見位置
        _hf_dirs = []
        try:
            from huggingface_hub.constants import HF_HUB_CACHE as _hf_cache_dir
            _hf_dirs.append(_hf_cache_dir)
        except Exception:
            pass
        _hf_default = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
        if _hf_default not in _hf_dirs:
            _hf_dirs.append(_hf_default)
        # faster-whisper 不同版本使用不同 HuggingFace 來源
        _fw_repo_names = [
            f"models--Systran--faster-whisper-{model_name}",
            f"models--mobiuslabsgmbh--faster-whisper-{model_name}",
        ]
        _fw_found = False
        for _d in _hf_dirs:
            for _rn in _fw_repo_names:
                if os.path.isdir(os.path.join(_d, _rn)):
                    _fw_found = True
                    break
            if _fw_found:
                break
        _fw_need_download = not _fw_found
    except Exception:
        pass
    if _fw_need_download:
        _sz = _fw_model_sizes.get(model_name, "")
        _sz_hint = f"（約 {_sz}）" if _sz else ""
        print(f"\n{C_WARN}首次使用 faster-whisper，正在下載模型 ({model_name}){_sz_hint}...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "載入中", "detail": f"下載 faster-whisper 模型（{model_name}）"})
        print(f"  {C_DIM}faster-whisper 格式與 whisper-stream 的 ggml 格式不同，需另外下載{RESET}")
        print(f"  {C_DIM}下載完成後會快取，之後不需重新下載{RESET}")
    else:
        print(f"\n{C_DIM}正在載入 Whisper 模型 ({model_name})...{RESET}", end="", flush=True)
        _webui_send({"type": "progress", "stage": "載入中", "detail": f"Whisper 模型（{model_name}）"})
    t0 = time.monotonic()
    import warnings, logging
    _hf_logger = logging.getLogger("huggingface_hub")
    _hf_log_level = _hf_logger.level
    _hf_logger.setLevel(logging.ERROR)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        warnings.filterwarnings("ignore", category=FutureWarning)
        fw_model = _call_with_ssl_retry(WhisperModel, model_name, device="auto", compute_type="int8")
    _hf_logger.setLevel(_hf_log_level)
    if _fw_need_download:
        print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
    else:
        print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")

    # ── 音訊裝置 ──
    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        sd_samplerate = int(wb_info["defaultSampleRate"])
        sd_channels = min(wb_info["maxInputChannels"], 2)
    else:
        import sounddevice as sd
        dev_info = sd.query_devices(capture_id)
        sd_samplerate = int(dev_info["default_samplerate"])
        sd_channels = min(dev_info["max_input_channels"], 2)

    stop_event = threading.Event()

    # ── 錄音 ──
    recorder = None
    rec_stream = None
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            if IS_WINDOWS and rec_device == WASAPI_MIXED_ID:
                # Windows 混合錄音（Loopback + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅 Loopback
                    rec_device = WASAPI_LOOPBACK_ID
            if rec_device == WASAPI_LOOPBACK_ID and IS_WINDOWS:
                wb_rec = _find_wasapi_loopback()
                rec_sr = int(wb_rec["defaultSampleRate"])
                rec_ch = wb_rec["maxInputChannels"]
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _WasapiLoopbackStream(
                        callback=rec_callback, samplerate=rec_sr,
                        channels=rec_ch, blocksize=int(rec_sr * 0.1))
                except Exception as e:
                    print(f"{C_HIGHLIGHT}[警告] 無法開啟錄音裝置 [{rec_device}]: {e}{RESET}")
                    recorder.close()
                    recorder = None
                    rec_stream = None
                    use_separate_rec = False
            elif _mixer is None:
                # 非 Windows WASAPI 的獨立錄音裝置
                import sounddevice as sd
                rec_info = sd.query_devices(rec_device)
                rec_sr = int(rec_info["default_samplerate"])
                rec_ch = max(rec_info["max_input_channels"], 1)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

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
            recorder = _AudioRecorder(sd_samplerate, topic=meeting_topic, mode=mode)

    # ── Banner ──
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ 本機（faster-whisper）{RESET}")
    if translator:
        if isinstance(translator, OllamaTranslator):
            _srv_type_label = "Ollama" if translator.server_type == "ollama" else "OpenAI 相容"
            print(f"  {C_OK}翻譯引擎: {translator.model} @ {translator.host}:{translator.port}（{_srv_type_label}）{RESET}")
        elif isinstance(translator, NllbTranslator):
            print(f"  {C_OK}翻譯引擎: NLLB 本機離線{RESET}")
        elif isinstance(translator, ArgosTranslator):
            print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    print(f"  {C_WHITE}音訊緩衝: {length_ms}ms / 步進 {step_ms}ms{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder:
        print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    if translator and hasattr(translator, 'meeting_topic') and translator.meeting_topic:
        print(f"  {C_WHITE}會議主題: {translator.meeting_topic}{RESET}")
    if denoise:
        print(f"  {C_OK}降噪: 已啟用（noisereduce）{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # ── 環形緩衝（原始取樣率 mono float32）──
    ring_size = sd_samplerate * length_ms // 1000  # 例如 48000*8=384000
    ring_buffer = np.zeros(ring_size, dtype=np.float32)
    ring_write_pos = 0
    ring_filled = 0
    ring_lock = threading.Lock()

    pause_event = threading.Event()
    global _webui_pause_event; _webui_pause_event = pause_event
    print_lock = threading.Lock()
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_event,),
        kwargs={"pause_event": pause_event},
        daemon=True,
    )
    kp_thread.start()

    # ── sounddevice callback（存原始取樣率，不降採樣）──
    def audio_callback(indata, frames, time_info, status):
        nonlocal ring_write_pos, ring_filled
        if stop_event.is_set():
            return
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        else:
            audio = audio.flatten()
        _push_rms(float(np.sqrt(np.mean(audio ** 2))))
        if recorder and rec_stream is None:
            recorder.write(audio)
        n = len(audio)
        with ring_lock:
            if ring_write_pos + n <= ring_size:
                ring_buffer[ring_write_pos:ring_write_pos + n] = audio
            else:
                first = ring_size - ring_write_pos
                ring_buffer[ring_write_pos:] = audio[:first]
                ring_buffer[:n - first] = audio[first:]
            ring_write_pos = (ring_write_pos + n) % ring_size
            ring_filled += n

    if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
        sd_stream = _WasapiLoopbackStream(
            callback=audio_callback, samplerate=sd_samplerate,
            channels=sd_channels, blocksize=int(sd_samplerate * 0.1))
    else:
        import sounddevice as sd
        sd_stream = sd.InputStream(
            device=capture_id,
            samplerate=sd_samplerate,
            channels=sd_channels,
            blocksize=int(sd_samplerate * 0.1),
            dtype="float32",
            callback=audio_callback,
        )

    # ── 降噪 ──
    if denoise:
        from noisereduce import reduce_noise as _nr_reduce
        def _denoise(audio, sr):
            peak = np.max(np.abs(audio))
            out = _nr_reduce(y=audio, sr=sr, stationary=True, prop_decrease=0.8)
            peak_after = np.max(np.abs(out))
            if peak_after > 1e-6:
                out = out * (peak / peak_after)
            return out
    else:
        def _denoise(audio, sr):
            return audio

    # ── 提取音訊並寫入暫存 WAV（原始取樣率，讓 faster-whisper 正確 resample）──
    import tempfile as _tempfile
    _tmp_wav_dir = _tempfile.gettempdir()

    def extract_wav_file():
        """提取環形緩衝，寫入暫存 WAV 檔，回傳檔案路徑和 RMS。"""
        with ring_lock:
            pos = ring_write_pos
            buf_copy = ring_buffer.copy()
        ordered = np.roll(buf_copy, -pos)
        rms = float(np.sqrt(np.mean(ordered ** 2)))
        ordered = _denoise(ordered, sd_samplerate)
        pcm = (ordered * 32767).clip(-32768, 32767).astype(np.int16)
        tmp_path = os.path.join(_tmp_wav_dir, f"jt_fw_{os.getpid()}.wav")
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sd_samplerate)  # 原始取樣率（如 48000）
            wf.writeframes(pcm.tobytes())
        return tmp_path, rms

    # ── 本機 faster-whisper 辨識 ──
    def local_transcribe(wav_path):
        """用 faster-whisper 辨識 WAV 檔，回傳 (segments_list, full_text, proc_time)"""
        t0 = time.monotonic()
        segments_iter, info = fw_model.transcribe(
            wav_path, language=whisper_lang, beam_size=5, vad_filter=True)
        segments = []
        texts = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                segments.append({"start": seg.start, "end": seg.end, "text": text})
                texts.append(text)
        full_text = " ".join(texts)
        proc_time = time.monotonic() - t0
        return segments, full_text, proc_time

    # ── 非同步翻譯（有序輸出）──
    _trans_seq = [0]
    _trans_pending = {}
    _trans_next = [0]
    _trans_lock = threading.Lock()

    def _drain_translations(_log_path):
        while True:
            with _trans_lock:
                entry = _trans_pending.pop(_trans_next[0], None)
                if entry is None:
                    break
                _trans_next[0] += 1
            src_text, result, elapsed, asr_elapsed = entry
            if not result:
                continue
            src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
            with print_lock:
                # 原文 + 辨識耗時
                _print_with_badge(f"{src_color}[{src_label}] {src_text}{RESET}",
                                  C_BADGE_ASR, asr_elapsed, "辨")
                # 翻譯 + 翻譯耗時
                _print_with_badge(f"{dst_color}{BOLD}[{dst_label}] {result}{RESET}",
                                  _speed_badge_color(elapsed), elapsed, "譯")
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            with open(_log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] [{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] [{dst_label}] {result}\n\n")
            _webui_send({"type": "transcription", "source": "main",
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp})

    def translate_and_print(seq, src_text, _log_path, asr_elapsed=0):
        t0 = time.monotonic()
        result = translator.translate(src_text)
        elapsed = time.monotonic() - t0
        if result:
            if not isinstance(translator, OllamaTranslator): result = S2TWP.convert(result)
        with _trans_lock:
            _trans_pending[seq] = (src_text, result, elapsed, asr_elapsed)
        _drain_translations(_log_path)

    # ── 有序非同步辨識 ──
    transcribe_seq = [0]
    _TRANSCRIBE_FAILED = "FAILED"
    pending_results = {}
    next_display_seq = [0]
    results_lock = threading.Lock()

    # 限制同時進行的辨識執行緒數量，避免 CPU 過載導致全部卡住
    _active_transcriptions = [0]
    _active_lock = threading.Lock()
    _MAX_CONCURRENT_TRANSCRIPTIONS = 2

    _slow_warned = [False]

    def transcribe_chunk(seq, wav_path):
        with _active_lock:
            _active_transcriptions[0] += 1
        try:
            segments, full_text, proc_time = local_transcribe(wav_path)
            with results_lock:
                pending_results[seq] = (segments, full_text, proc_time)
            # 首次辨識後檢查速度，太慢則建議更小模型
            if not _slow_warned[0] and proc_time > (length_ms / 1000.0) * 2:
                _slow_warned[0] = True
                _rec = _recommended_whisper_model(mode)
                if _rec != model_name:
                    with print_lock:
                        print(f"\n  {C_WARN}[提示] 辨識耗時 {proc_time:.1f}s，建議改用 {_rec}（此裝置適合）{RESET}", flush=True)
                        print(f"  {C_DIM}下次啟動可用 -m {_rec} 參數{RESET}\n", flush=True)
        except Exception as e:
            with print_lock:
                print(f"{C_DIM}  [本機辨識失敗: {e}]{RESET}", flush=True)
            with results_lock:
                pending_results[seq] = _TRANSCRIBE_FAILED
        finally:
            with _active_lock:
                _active_transcriptions[0] -= 1
            try:
                os.unlink(wav_path)
            except Exception:
                pass

    # ── 去重 ──
    recent_texts = deque(maxlen=10)

    def is_duplicate(text):
        text_lower = text.lower().strip()
        for prev in recent_texts:
            if text_lower == prev or text_lower in prev or prev in text_lower:
                return True
        return False

    # ── 過濾 + 顯示 ──
    if mode in _EN_INPUT_MODES:
        hallucination_check = _is_en_hallucination
    elif mode in _JA_INPUT_MODES:
        hallucination_check = _is_ja_hallucination
    else:
        hallucination_check = _is_zh_hallucination
    src_color, src_label = _MODE_LABELS[mode][0], _MODE_LABELS[mode][1]

    def drain_ordered_results():
        _NOT_READY = object()
        while True:
            with results_lock:
                result = pending_results.pop(next_display_seq[0], _NOT_READY)
            if result is _NOT_READY:
                break
            next_display_seq[0] += 1
            if result is _TRANSCRIBE_FAILED:
                continue
            segments, full_text, proc_time = result
            if not full_text:
                continue
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
                if mode in _ZH_INPUT_MODES:
                    line = S2TWP.convert(line)
                if hallucination_check(line):
                    continue
                if is_duplicate(line):
                    continue
                recent_texts.append(line.lower().strip())
                if mode in _TRANSLATE_MODES and translator:
                    seq = _trans_seq[0]; _trans_seq[0] += 1
                    threading.Thread(
                        target=translate_and_print,
                        args=(seq, line, log_path, proc_time),
                        daemon=True,
                    ).start()
                else:
                    with print_lock:
                        print(f"{src_color}{BOLD}[{src_label}] {line}{RESET}", flush=True)
                        print(flush=True)
                        _status_bar_state["count"] += 1
                        refresh_status_bar()
                    timestamp = time.strftime("%H:%M:%S")
                    with open(log_path, "a", encoding="utf-8") as log_f:
                        log_f.write(f"[{timestamp}] [{src_label}] {line}\n\n")
                    _webui_send({"type": "transcription", "source": "main",
                                 "src_lang": src_label, "src_text": line,
                                 "asr_time": round(proc_time, 1), "timestamp": timestamp})

    # ── 清理 ──
    _cleaned_up = [False]

    def _cleanup_local():
        if _cleaned_up[0]:
            return
        _cleaned_up[0] = True
        stop_event.set()
        if _rec_stream_mic:
            try:
                _rec_stream_mic.stop()
                _rec_stream_mic.close()
            except Exception:
                pass
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
        if _mixer:
            _mixer.flush_remaining()
        if recorder:
            rec_path = recorder.close()
            print(f"\n  {C_OK}錄音已儲存: {rec_path}{RESET}", flush=True)
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)

    _sigint_count_lc = [0]

    def signal_handler(signum, frame):
        _sigint_count_lc[0] += 1
        if _sigint_count_lc[0] >= 2:
            _force_exit(1)
        clear_status_bar()
        restore_terminal()
        _cleanup_local()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "正在停止", "detail": ""})
        _force_exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── 啟動音訊串流 ──
    sd_stream.start()
    if rec_stream:
        rec_stream.start()
    if _rec_stream_mic:
        _rec_stream_mic.start()

    # ── 驗證音訊是否正常流入 ──
    _audio_verified = False
    for _chk in range(6):  # 最多等 3 秒
        time.sleep(0.5)
        with ring_lock:
            _chk_filled = ring_filled
        if _chk_filled > 0:
            _chk_samples = min(_chk_filled, ring_size)
            _chk_rms = float(np.sqrt(np.mean(ring_buffer[:_chk_samples] ** 2)))
            print(f"  {C_DIM}音訊已連接（取樣率 {sd_samplerate}Hz, {sd_channels}ch, RMS: {_chk_rms:.4f}）{RESET}", flush=True)
            _audio_verified = True
            break
    if not _audio_verified:
        print(f"  {C_HIGHLIGHT}[警告] 3 秒內未收到音訊資料{RESET}", flush=True)
        print(f"  {C_DIM}請確認系統喇叭正在播放聲音，並檢查 WASAPI Loopback 裝置是否正確{RESET}", flush=True)

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "zh2en": "說中文即可看到英文翻譯",
        "ja2zh": "說日文即可看到中文翻譯",
        "zh2ja": "說中文即可看到日文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
        "ja": "說日文即可看到字幕",
    }
    print(f"\n{C_OK}{BOLD}開始監聽...{RESET} {C_WHITE}{listen_hints.get(mode, '')}{RESET}\n\n", flush=True)
    _webui_send({"type": "started", "mode": mode})

    _tr_model = translator.model if isinstance(translator, OllamaTranslator) else ("NLLB" if isinstance(translator, NllbTranslator) else ("Argos" if isinstance(translator, ArgosTranslator) else ""))
    _tr_loc = "伺服器" if isinstance(translator, OllamaTranslator) else ("本機" if isinstance(translator, (ArgosTranslator, NllbTranslator)) else "")
    setup_status_bar(mode, model_name=f"Whisper {model_name}", asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    if hasattr(signal, 'SIGWINCH'):
        signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # ── 主迴圈 ──
    step_sec = step_ms / 1000.0
    length_samples = ring_size
    next_transcribe_time = time.monotonic() + (length_ms / 1000.0)
    try:
        while not stop_event.is_set():
            time.sleep(0.2)
            if _status_bar_active:
                with print_lock:
                    refresh_status_bar()
            now = time.monotonic()
            if pause_event.is_set():
                next_transcribe_time = now + step_sec
                continue
            if now < next_transcribe_time:
                drain_ordered_results()
                continue
            with ring_lock:
                filled = ring_filled
            if filled < length_samples:
                continue
            next_transcribe_time = now + step_sec
            # 限制同時進行的辨識數量，避免 CPU 過載
            with _active_lock:
                active = _active_transcriptions[0]
            if active >= _MAX_CONCURRENT_TRANSCRIPTIONS:
                drain_ordered_results()
                continue
            wav_path, rms = extract_wav_file()
            if rms < 0.001:
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass
                continue
            seq = transcribe_seq[0]
            transcribe_seq[0] += 1
            threading.Thread(
                target=transcribe_chunk,
                args=(seq, wav_path),
                daemon=True,
            ).start()
            drain_ordered_results()
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

    clear_status_bar()
    restore_terminal()
    _cleanup_local()


# ═══════════════════════════════════════════════════════════════════
#  雙向即時翻譯（en_zh / ja_zh: 系統音訊翻譯 + 麥克風反向翻譯）
# ═══════════════════════════════════════════════════════════════════

def run_stream_bidirectional(lb_device_id, mic_device_id,
                              translator_lb, translator_mic,
                              model_name: str, mode: str = "en_zh",
                              length_ms: int = 5000, step_ms: int = 3000,
                              record: bool = False,
                              meeting_topic: str = None,
                              use_mlx: bool = False,
                              mic_translate: bool = True,
                              denoise: bool = False):
    """雙向即時翻譯：兩路音訊串流 → 共用 faster-whisper/mlx-whisper → 各自翻譯 → 交錯輸出。
    lb_device_id: 系統音訊（BlackHole / WASAPI Loopback）
    mic_device_id: 麥克風
    translator_lb: 系統音訊翻譯器（翻譯方向依模式決定，純轉錄模式為 None）
    translator_mic: 麥克風翻譯器（mic_translate=True 時使用，False 時為 None）
    use_mlx: True 時使用 mlx-whisper GPU 加速（僅 Apple Silicon）
    mic_translate: True=麥克風也翻譯（雙向模式），False=麥克風只轉錄（--mic 模式）"""
    import numpy as np

    bidi_cfg = _BIDI_LABELS[mode]  # {"loopback": (...), "mic": (...)}

    # ── 語言對照（使用模組級 _LB_LANG / _MIC_LANG）──
    _LB_HALLU = {"en2zh": _is_en_hallucination, "zh2en": _is_zh_hallucination,
                 "ja2zh": _is_ja_hallucination, "zh2ja": _is_zh_hallucination,
                 "en": _is_en_hallucination, "zh": _is_zh_hallucination,
                 "ja": _is_ja_hallucination, "en_zh": _is_en_hallucination,
                 "ja_zh": _is_ja_hallucination}
    _MIC_HALLU = {"en2zh": _is_zh_hallucination, "zh2en": _is_en_hallucination,
                  "ja2zh": _is_zh_hallucination, "zh2ja": _is_ja_hallucination,
                  "en": _is_en_hallucination, "zh": _is_zh_hallucination,
                  "ja": _is_ja_hallucination, "en_zh": _is_zh_hallucination,
                  "ja_zh": _is_zh_hallucination}
    lb_lang = _LB_LANG[mode]
    mic_lang = _MIC_LANG[mode]
    lb_hallu = _LB_HALLU[mode]
    mic_hallu = _MIC_HALLU[mode]
    # 雙向模式麥克風語言預偵測（detect_language → 正確語言辨識）
    # en_zh: 中文翻譯、英文直接顯示；ja_zh: 中文翻譯、日文/英文直接顯示
    _mic_auto_detect = (mode in ("en_zh", "ja_zh"))
    _mic_skip_langs = {"en_zh": {"en"}, "ja_zh": {"ja", "en"}}.get(mode)  # 偵測到這些語言時跳過翻譯
    if _mic_auto_detect:
        mic_lang = None  # 觸發 local_transcribe 內的語言預偵測

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    _LOG_PREFIX = {"en_zh": "英中雙向_逐字稿", "ja_zh": "日中雙向_逐字稿",
                   "en2zh": "英翻中_逐字稿",
                   "zh2en": "中翻英_逐字稿", "ja2zh": "日翻中_逐字稿",
                   "zh2ja": "中翻日_逐字稿", "en": "英文轉錄", "zh": "中文轉錄", "ja": "日文轉錄"}
    log_prefix = _LOG_PREFIX.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # ── 載入 ASR 模型（mlx-whisper 或 faster-whisper）──
    fw_model = None        # faster-whisper model（use_mlx=False 時使用）
    _mlx_repo = None       # mlx-whisper HF repo（use_mlx=True 時使用）
    _fw_model_sizes = {"large-v3-turbo": "1.6GB", "large-v3": "3.1GB",
                       "medium": "1.5GB", "small": "500MB", "base": "150MB"}

    if use_mlx:
        # 在 import 前設定，避免 huggingface_hub 的 tqdm 進度條和 "Fetching N files" 訊息
        _old_hf_progress = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS")
        _old_tqdm_disable = os.environ.get("TQDM_DISABLE")
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["TQDM_DISABLE"] = "1"
        try:
            from huggingface_hub.utils import disable_progress_bars as _hf_disable_pb
            _hf_disable_pb()
        except Exception:
            pass
        import mlx_whisper as _mlx_whisper_mod
        # MLX 社群 repo 命名不一致：large-v3-turbo 無字尾，其餘需加 -mlx
        _MLX_REPO_SUFFIX = {"large-v3-turbo": "", "large-v3": "-mlx",
                            "medium": "-mlx", "small": "-mlx",
                            "base": "-mlx", "tiny": "-mlx"}
        _sfx = _MLX_REPO_SUFFIX.get(model_name, "-mlx")
        _mlx_repo = f"mlx-community/whisper-{model_name}{_sfx}"
        _mlx_cache_name = f"models--mlx-community--whisper-{model_name}{_sfx}"
        # 檢查 HF cache 是否已有模型
        _mlx_need_download = True
        try:
            _hf_dirs = []
            try:
                from huggingface_hub.constants import HF_HUB_CACHE as _hf_cache_dir
                _hf_dirs.append(_hf_cache_dir)
            except Exception:
                pass
            _hf_default = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
            if _hf_default not in _hf_dirs:
                _hf_dirs.append(_hf_default)
            for _d in _hf_dirs:
                if os.path.isdir(os.path.join(_d, _mlx_cache_name)):
                    _mlx_need_download = False
                    break
        except Exception:
            pass
        if _mlx_need_download:
            _sz = _fw_model_sizes.get(model_name, "")
            _sz_hint = f"（約 {_sz}）" if _sz else ""
            print(f"\n{C_WARN}首次使用 mlx-whisper，正在下載模型 ({model_name}){_sz_hint}...{RESET}", flush=True)
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"下載 MLX Whisper 模型（{model_name}）"})
            print(f"  {C_DIM}mlx-whisper 使用 MLX 格式（Apple Silicon GPU 加速）{RESET}")
            print(f"  {C_DIM}下載完成後會快取，之後不需重新下載{RESET}")
        else:
            print(f"\n{C_DIM}正在載入 MLX Whisper 模型 ({model_name})...{RESET}", end="", flush=True)
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"MLX Whisper 模型（{model_name}）"})
        t0 = time.monotonic()
        # 用極短靜音 WAV 預熱（首次 transcribe 時才真正載入權重）
        import tempfile as _tempfile_warmup
        _warmup_dir = _tempfile_warmup.gettempdir()
        _warmup_path = os.path.join(_warmup_dir, f"jt_mlx_warmup_{os.getpid()}.wav")
        import wave as _wave_warmup
        with _wave_warmup.open(_warmup_path, "wb") as _ww:
            _ww.setnchannels(1)
            _ww.setsampwidth(2)
            _ww.setframerate(16000)
            _ww.writeframes(b"\x00\x00" * 1600)  # 0.1s 靜音
        import warnings, logging
        _hf_logger = logging.getLogger("huggingface_hub")
        _hf_log_level = _hf_logger.level
        _hf_logger.setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=FutureWarning)
            # 用 lb_lang 預熱；若 mic_lang 不同則再預熱一次（避免首次辨識觸發 MLX 重編譯）
            _call_with_ssl_retry(_mlx_whisper_mod.transcribe, _warmup_path, path_or_hf_repo=_mlx_repo, language=lb_lang)
            if mic_lang is None:
                # 自動偵測模式：預熱 transcribe（偵測可能用到的語言）
                _warmup_langs = {"zh", "en"}
                if mode == "ja_zh":
                    _warmup_langs.add("ja")
                for _wl in _warmup_langs - {lb_lang}:
                    _mlx_whisper_mod.transcribe(_warmup_path, path_or_hf_repo=_mlx_repo, language=_wl)
                # 預熱 detect_language + direct_decode 路徑（避免首次辨識觸發 MLX JIT 編譯）
                import mlx.core as _warmup_mx
                from mlx_whisper.transcribe import ModelHolder as _WarmupMH
                from mlx_whisper.decoding import DecodingOptions as _WarmupDO
                from mlx_whisper.tokenizer import get_tokenizer as _warmup_get_tok
                _w_model = _WarmupMH.get_model(_mlx_repo, _warmup_mx.float16)
                _w_mel = _mlx_whisper_mod.audio.log_mel_spectrogram(
                    _warmup_path, n_mels=_w_model.dims.n_mels,
                    padding=_mlx_whisper_mod.audio.N_SAMPLES,
                )
                _w_mel_seg = _mlx_whisper_mod.audio.pad_or_trim(
                    _w_mel, _mlx_whisper_mod.audio.N_FRAMES, axis=-2
                ).astype(_warmup_mx.float16)
                _w_model.detect_language(_w_mel_seg)
                # 預熱 decode 路徑（tokenizer + model.decode JIT，sample_len 需與實際一致）
                _w_tok = _warmup_get_tok(_w_model.is_multilingual,
                    num_languages=_w_model.num_languages, language="zh", task="transcribe")
                _w_opts = _WarmupDO(language="zh", task="transcribe", temperature=0.0,
                    sample_len=25, fp16=True)
                try:
                    _w_model.decode(_w_mel_seg, _w_opts)
                except Exception:
                    pass
                # 也預熱英文（+ ja_zh 模式的日文）decode
                for _wl2 in ({"en", "ja"} if mode == "ja_zh" else {"en"}):
                    _w_opts2 = _WarmupDO(language=_wl2, task="transcribe", temperature=0.0,
                        sample_len=25, fp16=True)
                    try:
                        _w_model.decode(_w_mel_seg, _w_opts2)
                    except Exception:
                        pass
                del _w_model, _w_mel, _w_mel_seg, _w_tok, _w_opts
            elif mic_lang != lb_lang:
                _mlx_whisper_mod.transcribe(_warmup_path, path_or_hf_repo=_mlx_repo, language=mic_lang)
        _hf_logger.setLevel(_hf_log_level)
        # 恢復 HF 進度條設定
        try:
            from huggingface_hub.utils import enable_progress_bars as _hf_enable_pb
            _hf_enable_pb()
        except Exception:
            pass
        if _old_hf_progress is None:
            os.environ.pop("HF_HUB_DISABLE_PROGRESS_BARS", None)
        else:
            os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = _old_hf_progress
        if _old_tqdm_disable is None:
            os.environ.pop("TQDM_DISABLE", None)
        else:
            os.environ["TQDM_DISABLE"] = _old_tqdm_disable
        try:
            os.unlink(_warmup_path)
        except OSError:
            pass
        if _mlx_need_download:
            print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
        else:
            print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")
    else:
        from faster_whisper import WhisperModel
        _fw_need_download = False
        try:
            _hf_dirs = []
            try:
                from huggingface_hub.constants import HF_HUB_CACHE as _hf_cache_dir
                _hf_dirs.append(_hf_cache_dir)
            except Exception:
                pass
            _hf_default = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
            if _hf_default not in _hf_dirs:
                _hf_dirs.append(_hf_default)
            _fw_repo_names = [
                f"models--Systran--faster-whisper-{model_name}",
                f"models--mobiuslabsgmbh--faster-whisper-{model_name}",
            ]
            _fw_found = False
            for _d in _hf_dirs:
                for _rn in _fw_repo_names:
                    if os.path.isdir(os.path.join(_d, _rn)):
                        _fw_found = True
                        break
                if _fw_found:
                    break
            _fw_need_download = not _fw_found
        except Exception:
            pass
        if _fw_need_download:
            _sz = _fw_model_sizes.get(model_name, "")
            _sz_hint = f"（約 {_sz}）" if _sz else ""
            print(f"\n{C_WARN}首次使用 faster-whisper，正在下載模型 ({model_name}){_sz_hint}...{RESET}", flush=True)
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"下載 faster-whisper 模型（{model_name}）"})
            print(f"  {C_DIM}雙向模式使用 faster-whisper 格式（與 whisper-stream 的 ggml 格式不同）{RESET}")
            print(f"  {C_DIM}下載完成後會快取，之後不需重新下載{RESET}")
        else:
            print(f"\n{C_DIM}正在載入 Whisper 模型 ({model_name})...{RESET}", end="", flush=True)
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"Whisper 模型（{model_name}）"})
        t0 = time.monotonic()
        import warnings, logging
        _hf_logger = logging.getLogger("huggingface_hub")
        _hf_log_level = _hf_logger.level
        _hf_logger.setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=FutureWarning)
            fw_model = _call_with_ssl_retry(WhisperModel, model_name, device="auto", compute_type="int8")
        _hf_logger.setLevel(_hf_log_level)
        if _fw_need_download:
            print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
        else:
            print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")

    # ── 音訊裝置資訊 ──
    import sounddevice as sd
    if IS_WINDOWS and lb_device_id == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        lb_sr = int(wb_info["defaultSampleRate"])
        lb_ch = min(wb_info["maxInputChannels"], 2)
        lb_name = wb_info["name"]
    else:
        lb_info = sd.query_devices(lb_device_id)
        lb_sr = int(lb_info["default_samplerate"])
        lb_ch = min(lb_info["max_input_channels"], 2)
        lb_name = lb_info["name"]

    mic_info = sd.query_devices(mic_device_id)
    mic_sr = int(mic_info["default_samplerate"])
    mic_ch = min(mic_info["max_input_channels"], 2)
    mic_name = mic_info["name"]

    stop_event = threading.Event()

    # ── 錄音（兩個獨立錄音器）──
    recorder_lb = None
    recorder_mic = None
    if record:
        recorder_lb = _AudioRecorder(lb_sr, topic=f"{meeting_topic or ''}_系統音訊".lstrip("_"), mode=mode)
        recorder_mic = _AudioRecorder(mic_sr, topic=f"{meeting_topic or ''}_麥克風".lstrip("_"), mode=mode)

    # ── Banner ──
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print(f"{C_TITLE}{BOLD}  {APP_NAME}{RESET}")
    print(f"{C_TITLE}  {APP_AUTHOR}{RESET}")
    _fw_label = "mlx-whisper GPU" if use_mlx else "faster-whisper"
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ 本機（{_fw_label}）{RESET}")
    if isinstance(translator_lb, OllamaTranslator):
        _srv_label = "Ollama" if translator_lb.server_type == "ollama" else "OpenAI 相容"
        print(f"  {C_OK}翻譯引擎: {translator_lb.model} @ {translator_lb.host}:{translator_lb.port}（{_srv_label}）{RESET}")
    elif isinstance(translator_lb, NllbTranslator):
        print(f"  {C_OK}翻譯引擎: NLLB 本機離線{RESET}")
    elif isinstance(translator_lb, ArgosTranslator):
        print(f"  {C_OK}翻譯引擎: Argos 本機離線{RESET}")
    elif translator_lb is None:
        print(f"  {C_OK}翻譯引擎: 無（直接轉錄）{RESET}")
    # 方向標籤（用 _BIDI_LABELS 的 src_label/dst_label 組合）
    _lb_labels = bidi_cfg["loopback"]  # (src_color, src_label, dst_color, dst_label)
    _mic_labels = bidi_cfg["mic"]
    _lang_name = {"en": "英文", "zh": "中文", "ja": "日文"}
    if translator_lb is not None:
        _lb_dir = f"{_lb_labels[1]}→{_lb_labels[3]}"  # e.g. "EN→中"
    else:
        _lb_dir = f"{_lang_name.get(lb_lang, lb_lang)}轉錄"  # e.g. "中文轉錄"
    if mic_translate and translator_mic is not None:
        _mic_dir = f"{_mic_labels[1]}→{_mic_labels[3]}"  # e.g. "中→EN"
    else:
        _mic_dir = f"{_lang_name.get(mic_lang, mic_lang)}轉錄"  # e.g. "中文轉錄"
    print(f"  {C_WHITE}系統音訊: {lb_name}（{_lb_dir}）{RESET}")
    print(f"  {C_WHITE}麥克風:   {mic_name}（{_mic_dir}）{RESET}")
    print(f"  {C_WHITE}音訊緩衝: {length_ms}ms / 步進 {step_ms}ms{RESET}")
    print(f"  {C_DIM}翻譯記錄: logs/{log_filename}{RESET}")
    if recorder_lb:
        print(f"  {C_DIM}錄音: {recorder_lb.path}{RESET}")
        print(f"  {C_DIM}錄音: {recorder_mic.path}{RESET}")
    if meeting_topic:
        print(f"  {C_WHITE}會議主題: {meeting_topic}{RESET}")
    if denoise:
        print(f"  {C_OK}降噪: 已啟用（noisereduce）{RESET}")
    print(f"  {C_HIGHLIGHT}提醒：{RESET}")
    print(f"  {C_HIGHLIGHT}  1. 建議使用耳機，避免麥克風收到系統音訊的回音{RESET}")
    print(f"  {C_HIGHLIGHT}  2. 請將非說話用的麥克風停用或輸入音量拉到最低，{RESET}")
    print(f"  {C_HIGHLIGHT}     以免影響辨識與翻譯品質{RESET}")
    _hint_n = 3
    if not mic_translate:
        print(f"  {C_HIGHLIGHT}  {_hint_n}. ASR 雙路辨識（非 whisper-stream），辨識負載加倍{RESET}")
        _hint_n += 1
    if _mic_auto_detect:
        _mix_hint = "中日英混雜" if mode == "ja_zh" else "中英混雜"
        print(f"  {C_OK}  {_hint_n}. 麥克風支援{_mix_hint}，開始幾句辨識較慢屬正常（模型預熱中）{RESET}")
    print(f"  {C_DIM}按 Ctrl+P 暫停/繼續 ─ Ctrl+C 停止{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    # ── 兩組環形緩衝（各自 ring buffer）──
    lb_ring_size = lb_sr * length_ms // 1000
    lb_ring_buffer = np.zeros(lb_ring_size, dtype=np.float32)
    lb_ring_write_pos = 0
    lb_ring_filled = 0
    lb_ring_lock = threading.Lock()

    mic_ring_size = mic_sr * length_ms // 1000
    mic_ring_buffer = np.zeros(mic_ring_size, dtype=np.float32)
    mic_ring_write_pos = 0
    mic_ring_filled = 0
    mic_ring_lock = threading.Lock()

    pause_event = threading.Event()
    global _webui_pause_event; _webui_pause_event = pause_event
    print_lock = threading.Lock()
    setup_terminal_raw_input()
    kp_thread = threading.Thread(
        target=keypress_listener_thread,
        args=(stop_event,),
        kwargs={"pause_event": pause_event},
        daemon=True,
    )
    kp_thread.start()

    # ── Loopback 音訊 callback ──
    # WebUI 靜音 flag 檔案
    _mute_lb_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mute_lb")
    _mute_mic_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mute_mic")

    def lb_audio_callback(indata, frames, time_info, status):
        nonlocal lb_ring_write_pos, lb_ring_filled
        if stop_event.is_set():
            return
        if os.path.isfile(_mute_lb_flag):
            return  # 系統音訊已靜音
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        else:
            audio = audio.flatten()
        _push_rms(float(np.sqrt(np.mean(audio ** 2))))
        if recorder_lb:
            recorder_lb.write(audio)
        n = len(audio)
        with lb_ring_lock:
            if lb_ring_write_pos + n <= lb_ring_size:
                lb_ring_buffer[lb_ring_write_pos:lb_ring_write_pos + n] = audio
            else:
                first = lb_ring_size - lb_ring_write_pos
                lb_ring_buffer[lb_ring_write_pos:] = audio[:first]
                lb_ring_buffer[:n - first] = audio[first:]
            lb_ring_write_pos = (lb_ring_write_pos + n) % lb_ring_size
            lb_ring_filled += n

    # ── 麥克風音訊 callback ──
    def mic_audio_callback(indata, frames, time_info, status):
        nonlocal mic_ring_write_pos, mic_ring_filled
        if stop_event.is_set():
            return
        if os.path.isfile(_mute_mic_flag):
            return  # 麥克風已靜音
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        else:
            audio = audio.flatten()
        _push_rms(float(np.sqrt(np.mean(audio ** 2))))
        if recorder_mic:
            recorder_mic.write(audio)
        n = len(audio)
        with mic_ring_lock:
            if mic_ring_write_pos + n <= mic_ring_size:
                mic_ring_buffer[mic_ring_write_pos:mic_ring_write_pos + n] = audio
            else:
                first = mic_ring_size - mic_ring_write_pos
                mic_ring_buffer[mic_ring_write_pos:] = audio[:first]
                mic_ring_buffer[:n - first] = audio[first:]
            mic_ring_write_pos = (mic_ring_write_pos + n) % mic_ring_size
            mic_ring_filled += n

    # ── 建立音訊串流 ──
    if IS_WINDOWS and lb_device_id == WASAPI_LOOPBACK_ID:
        lb_stream = _WasapiLoopbackStream(
            callback=lb_audio_callback, samplerate=lb_sr,
            channels=lb_ch, blocksize=int(lb_sr * 0.1))
    else:
        lb_stream = sd.InputStream(
            device=lb_device_id, samplerate=lb_sr, channels=lb_ch,
            blocksize=int(lb_sr * 0.1), dtype="float32",
            callback=lb_audio_callback)

    mic_stream = sd.InputStream(
        device=mic_device_id, samplerate=mic_sr, channels=mic_ch,
        blocksize=int(mic_sr * 0.1), dtype="float32",
        callback=mic_audio_callback)

    # ── 降噪 ──
    if denoise:
        from noisereduce import reduce_noise as _nr_reduce
        def _denoise(audio, sr):
            peak = np.max(np.abs(audio))
            out = _nr_reduce(y=audio, sr=sr, stationary=True, prop_decrease=0.8)
            peak_after = np.max(np.abs(out))
            if peak_after > 1e-6:
                out = out * (peak / peak_after)
            return out
    else:
        def _denoise(audio, sr):
            return audio

    # ── 暫存 WAV 目錄 ──
    import tempfile as _tempfile
    _tmp_wav_dir = _tempfile.gettempdir()
    _wav_counter = [0]  # 每次抽取遞增，確保檔名唯一

    def extract_wav_lb():
        """提取 loopback 環形緩衝，寫入暫存 WAV 檔，回傳 (path, rms)。"""
        with lb_ring_lock:
            pos = lb_ring_write_pos
            buf_copy = lb_ring_buffer.copy()
        ordered = np.roll(buf_copy, -pos)
        rms = float(np.sqrt(np.mean(ordered ** 2)))
        ordered = _denoise(ordered, lb_sr)
        pcm = (ordered * 32767).clip(-32768, 32767).astype(np.int16)
        _wav_counter[0] += 1
        tmp_path = os.path.join(_tmp_wav_dir, f"jt_bidi_lb_{os.getpid()}_{_wav_counter[0]}.wav")
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(lb_sr)
            wf.writeframes(pcm.tobytes())
        return tmp_path, rms

    _mic_step_samples = mic_sr * step_ms // 1000  # 最新 step_sec 的樣本數
    _mic_peak_window = mic_sr // 2  # 0.5 秒的樣本數，用於峰值 RMS 計算

    def extract_wav_mic():
        """提取麥克風環形緩衝，寫入暫存 WAV 檔，回傳 (path, peak_rms)。
        用 0.5s 滑動視窗的峰值 RMS（最近 step_sec 內），精準偵測短暫語音。"""
        with mic_ring_lock:
            pos = mic_ring_write_pos
            buf_copy = mic_ring_buffer.copy()
        ordered = np.roll(buf_copy, -pos)
        # 峰值 RMS：最近 step_sec 內，取 0.5s 窗口的最大 RMS
        _recent = ordered[-_mic_step_samples:]
        _peak = 0.0
        for _i in range(0, len(_recent) - _mic_peak_window + 1, _mic_peak_window):
            _w = float(np.sqrt(np.mean(_recent[_i:_i + _mic_peak_window] ** 2)))
            if _w > _peak:
                _peak = _w
        rms = _peak
        ordered = _denoise(ordered, mic_sr)
        pcm = (ordered * 32767).clip(-32768, 32767).astype(np.int16)
        _wav_counter[0] += 1
        tmp_path = os.path.join(_tmp_wav_dir, f"jt_bidi_mic_{os.getpid()}_{_wav_counter[0]}.wav")
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(mic_sr)
            wf.writeframes(pcm.tobytes())
        return tmp_path, rms

    # ── 本機 ASR 辨識（mlx-whisper 或 faster-whisper）──
    # initial_prompt 引導 Whisper 輸出風格（繁體中文/日文），顯著提升辨識準確度
    # 注意：small/base/tiny 模型太弱，會把 prompt 當作辨識結果輸出，故只對 medium 以上啟用
    _WHISPER_PROMPT = {
        "zh": "以下是繁體中文語音內容，請使用繁體中文輸出。",
        "en": None,
        "ja": "以下は日本語の音声です。",
    }
    _PROMPT_CAPABLE_MODELS = {"large-v3-turbo", "large-v3", "medium"}
    if model_name not in _PROMPT_CAPABLE_MODELS:
        _WHISPER_PROMPT = {"zh": None, "en": None, "ja": None}
    # 安全過濾：即使 prompt 洩漏到辨識結果，也會被移除
    _PROMPT_LEAK_TEXTS = {"以下是繁體中文語音內容", "請使用繁體中文輸出", "請使用繁體中文",
                          "以下是繁体中文语音内容", "请使用繁体中文输出", "请使用繁体中文",
                          "以下は日本語の音声です"}

    if use_mlx:
        # 預載語言偵測所需模組（en_zh 麥克風自動偵測中/英）
        if _mic_auto_detect:
            import mlx.core as _mx
            from math import gcd as _gcd
            from scipy.signal import resample_poly as _resample_poly
            from mlx_whisper.transcribe import ModelHolder as _MLXModelHolder
            from mlx_whisper.decoding import DecodingOptions as _MLXDecodingOptions
            from mlx_whisper.tokenizer import get_tokenizer as _mlx_get_tokenizer
            _mlx_audio_mod = _mlx_whisper_mod.audio
            _WHISPER_SR = 16000  # Whisper 固定取樣率

            def _load_wav_as_mx(wav_path, target_sr=_WHISPER_SR):
                """用 wave 模組直接讀 WAV + scipy resample 到 16kHz（取代 ffmpeg 子程序）"""
                with wave.open(wav_path, "r") as _wf:
                    _sr = _wf.getframerate()
                    _audio = np.frombuffer(
                        _wf.readframes(_wf.getnframes()), dtype=np.int16
                    ).astype(np.float32) / 32768.0
                if _sr != target_sr:
                    _g = _gcd(_sr, target_sr)
                    _audio = _resample_poly(_audio, up=target_sr // _g, down=_sr // _g)
                return _mx.array(_audio)

            def _direct_decode(model, mel_seg, lang, prompt_text=None, sample_len=50):
                """繞過 transcribe()，直接用 model.decode()（mel 只算一次）。
                回傳 (text, no_speech_prob, avg_logprob, compression_ratio)"""
                _tokenizer = _mlx_get_tokenizer(
                    model.is_multilingual,
                    num_languages=model.num_languages,
                    language=lang, task="transcribe",
                )
                _prompt_tokens = []
                if prompt_text:
                    _prompt_tokens = _tokenizer.encode(" " + prompt_text.strip())
                _opts = _MLXDecodingOptions(
                    language=lang, task="transcribe",
                    temperature=0.0, sample_len=sample_len,
                    prompt=_prompt_tokens or None,
                    fp16=True,
                )
                _result = model.decode(mel_seg, _opts)
                _text = _tokenizer.decode(
                    [t for t in _result.tokens if t < _tokenizer.eot]
                )
                return _text, _result.no_speech_prob, _result.avg_logprob, _result.compression_ratio

        def local_transcribe(wav_path, lang):
            """用 mlx-whisper 辨識 WAV 檔，回傳 (segments_list, full_text, proc_time, detected_lang)"""
            t0 = time.monotonic()
            # 語言預偵測 + 直接 decode：mel 只算一次，不經 transcribe() 重複計算
            if lang is None:
                _model = _MLXModelHolder.get_model(_mlx_repo, _mx.float16)
                # 直接讀 WAV + resample（不用 ffmpeg）
                _audio_mx = _load_wav_as_mx(wav_path)
                _mel = _mlx_audio_mod.log_mel_spectrogram(
                    _audio_mx, n_mels=_model.dims.n_mels,
                    padding=_mlx_audio_mod.N_SAMPLES,
                )
                _mel_seg = _mlx_audio_mod.pad_or_trim(
                    _mel, _mlx_audio_mod.N_FRAMES, axis=-2
                ).astype(_mx.float16)
                # 語言偵測：偏向中文（主要語言），zh > 30% 就用中文
                _, _probs = _model.detect_language(_mel_seg)
                _zh_prob = _probs.get("zh", 0)
                if _zh_prob > 0.3:
                    lang = "zh"
                elif mode == "ja_zh":
                    # ja_zh 模式：非中文時區分日文和英文
                    _ja_prob = _probs.get("ja", 0)
                    lang = "ja" if _ja_prob > _probs.get("en", 0) else "en"
                else:
                    lang = "en"
                # 直接 decode（省掉 transcribe 的 mel 重算 + ffmpeg）
                # sample_len=25：8 秒音訊約 15-20 token，25 足夠且限制誤判時的最壞延遲
                _prompt = _WHISPER_PROMPT.get(lang)
                try:
                    _text, _nsp, _alp, _cr = _direct_decode(
                        _model, _mel_seg, lang, _prompt, sample_len=25)
                except Exception as _e:
                    # fallback：internal API 不相容時回退到 transcribe()
                    # temperature=0 強制單次解碼（不重試），避免 cascade 導致 30s+ 延遲
                    with print_lock:
                        print(f"{C_DIM}  [detect fallback: {_e}]{RESET}", flush=True)
                    result = _mlx_whisper_mod.transcribe(wav_path,
                        path_or_hf_repo=_mlx_repo, language=lang,
                        word_timestamps=False, condition_on_previous_text=False,
                        sample_len=25, temperature=0,
                        **({"initial_prompt": _prompt} if _prompt else {}))
                    detected_lang = result.get("language", lang) or lang
                    segments = []
                    texts = []
                    for seg in result.get("segments", []):
                        text = seg.get("text", "").strip()
                        for _leak in _PROMPT_LEAK_TEXTS:
                            text = text.replace(_leak, "")
                        text = text.strip("，。、 ")
                        if text:
                            segments.append({"start": seg["start"], "end": seg["end"], "text": text})
                            texts.append(text)
                    proc_time = time.monotonic() - t0
                    return segments, " ".join(texts), proc_time, detected_lang
                # 過濾 no_speech / 低品質
                if _nsp > 0.6 and _alp < -1.0:
                    proc_time = time.monotonic() - t0
                    return [], "", proc_time, lang
                # 安全過濾 prompt 洩漏
                for _leak in _PROMPT_LEAK_TEXTS:
                    _text = _text.replace(_leak, "")
                _text = _text.strip("，。、 ")
                proc_time = time.monotonic() - t0
                detected_lang = lang
                if _text:
                    return [{"start": 0, "end": 0, "text": _text}], _text, proc_time, detected_lang
                return [], "", proc_time, detected_lang
            # 非 detect 路徑：走原本的 transcribe() API
            # bidi 模式用較小的 sample_len（35）加速，減少序列化鎖佔用時間
            _sl = 35 if _mic_auto_detect else 50
            _kw = dict(
                path_or_hf_repo=_mlx_repo,
                language=lang,
                word_timestamps=False,
                condition_on_previous_text=False,
                sample_len=_sl,
            )
            _prompt = _WHISPER_PROMPT.get(lang)
            if _prompt:
                _kw["initial_prompt"] = _prompt
            result = _mlx_whisper_mod.transcribe(wav_path, **_kw)
            detected_lang = result.get("language", lang) or lang
            segments = []
            texts = []
            for seg in result.get("segments", []):
                text = seg.get("text", "").strip()
                # 安全過濾：移除 prompt 洩漏文字
                for _leak in _PROMPT_LEAK_TEXTS:
                    text = text.replace(_leak, "")
                text = text.strip("，。、 ")
                if text:
                    segments.append({"start": seg["start"], "end": seg["end"], "text": text})
                    texts.append(text)
            full_text = " ".join(texts)
            proc_time = time.monotonic() - t0
            return segments, full_text, proc_time, detected_lang
    else:
        # Windows: 預載 resample 工具（en_zh mic 語言預偵測用）
        if _mic_auto_detect:
            from math import gcd as _gcd_fw
            from scipy.signal import resample_poly as _resample_poly_fw
            _WHISPER_SR_FW = 16000

            def _load_wav_as_np(wav_path, target_sr=_WHISPER_SR_FW):
                """用 wave 模組直接讀 WAV + scipy resample 到 16kHz（取代 ffmpeg）"""
                with wave.open(wav_path, "r") as _wf:
                    _sr = _wf.getframerate()
                    _audio = np.frombuffer(
                        _wf.readframes(_wf.getnframes()), dtype=np.int16
                    ).astype(np.float32) / 32768.0
                if _sr != target_sr:
                    _g = _gcd_fw(_sr, target_sr)
                    _audio = _resample_poly_fw(_audio, up=target_sr // _g, down=_sr // _g)
                return _audio

        def local_transcribe(wav_path, lang):
            """用 faster-whisper 辨識 WAV 檔，回傳 (segments_list, full_text, proc_time, detected_lang)"""
            t0 = time.monotonic()
            # 語言預偵測：lang=None 時先用 detect_language 判斷語言，再用正確語言辨識
            _audio_input = wav_path  # 預設傳檔案路徑
            if lang is None:
                # 直接讀 WAV + resample（不用 ffmpeg），detect + transcribe 共用同一份音訊
                _audio_np = _load_wav_as_np(wav_path)
                _audio_input = _audio_np  # 傳 numpy array 給 transcribe，省掉第二次 ffmpeg
                _det, _det_prob, _all_probs = fw_model.detect_language(_audio_np)
                # 偏向中文（主要語言）：zh > 30% 就用中文
                _prob_dict = dict(_all_probs)
                _zh_prob_fw = _prob_dict.get("zh", 0)
                if _zh_prob_fw > 0.3:
                    lang = "zh"
                elif mode == "ja_zh":
                    _ja_prob_fw = _prob_dict.get("ja", 0)
                    lang = "ja" if _ja_prob_fw > _prob_dict.get("en", 0) else "en"
                else:
                    lang = "en"
            _kw = dict(
                language=lang, beam_size=1, best_of=1,
                temperature=0, condition_on_previous_text=False,
                vad_filter=True,
                max_new_tokens=40,  # 防止幻覺導致長時間解碼（CPU 每步 ~67ms，40 步≈2.7s 上限）
            )
            _prompt = _WHISPER_PROMPT.get(lang)
            if _prompt:
                _kw["initial_prompt"] = _prompt
            segments_iter, info = fw_model.transcribe(_audio_input, **_kw)
            detected_lang = getattr(info, "language", lang) or lang
            segments = []
            texts = []
            for seg in segments_iter:
                text = seg.text.strip()
                # 安全過濾：移除 prompt 洩漏文字
                for _leak in _PROMPT_LEAK_TEXTS:
                    text = text.replace(_leak, "")
                text = text.strip("，。、 ")
                if text:
                    segments.append({"start": seg.start, "end": seg.end, "text": text})
                    texts.append(text)
            full_text = " ".join(texts)
            proc_time = time.monotonic() - t0
            return segments, full_text, proc_time, detected_lang

    # ── 非同步翻譯（兩組獨立佇列，共用 print_lock）──
    # Loopback pipeline (en→zh)
    _trans_seq_lb = [0]
    _trans_pending_lb = {}
    _trans_next_lb = [0]
    _trans_lock_lb = threading.Lock()

    # Mic pipeline (zh→en)
    _trans_seq_mic = [0]
    _trans_pending_mic = {}
    _trans_next_mic = [0]
    _trans_lock_mic = threading.Lock()

    _NO_TRANSLATE = object()  # 哨兵值：不翻譯，只轉錄

    def _drain_translations(pending, next_seq, lock, source, label_override=None):
        """排乾翻譯結果佇列（有序輸出）。label_override: 覆蓋 src_label（自動偵測語言時用）"""
        labels = bidi_cfg[source]  # (src_color, src_label, dst_color, dst_label)
        src_color, src_label, dst_color, dst_label = labels
        if label_override:
            src_label = label_override
        if source == "loopback":
            prefix_src = "◀ "
            prefix_dst = "◀ "
            badge_trans_color = _speed_badge_color
        else:
            pad = "        "  # 8 格內縮
            prefix_src = f"{pad}{dst_color}▶{RESET} "
            prefix_dst = f"{pad}{dst_color}▶ "
            badge_trans_color = lambda _e: C_BADGE_MY_TRANS
        while True:
            with lock:
                entry = pending.pop(next_seq[0], None)
                if entry is None:
                    break
                next_seq[0] += 1
            src_text, result, elapsed, asr_elapsed = entry
            if result is _NO_TRANSLATE:
                # 不翻譯模式：只印原文一行
                if not src_text:
                    continue
                with print_lock:
                    _print_with_badge(f"{prefix_src}{src_color}[{src_label}] {src_text}{RESET}",
                                      C_BADGE_ASR, asr_elapsed, "辨")
                    print(flush=True)
                    _status_bar_state["count"] += 1
                    refresh_status_bar()
                timestamp = time.strftime("%H:%M:%S")
                _log_prefix = "◀ " if source == "loopback" else "▶ "
                with open(log_path, "a", encoding="utf-8") as log_f:
                    log_f.write(f"[{timestamp}] {_log_prefix}[{src_label}] {src_text}\n\n")
                _webui_send({"type": "transcription", "source": source,
                             "src_lang": src_label, "src_text": src_text,
                             "asr_time": round(asr_elapsed, 1), "timestamp": timestamp})
                continue
            if not result:
                continue
            with print_lock:
                # 原文 + 辨識耗時
                _print_with_badge(f"{prefix_src}{src_color}[{src_label}] {src_text}{RESET}",
                                  C_BADGE_ASR, asr_elapsed, "辨")
                # 翻譯 + 翻譯耗時
                _print_with_badge(f"{prefix_dst}{dst_color}{BOLD}[{dst_label}] {result}{RESET}",
                                  badge_trans_color(elapsed), elapsed, "譯")
                print(flush=True)
                _status_bar_state["count"] += 1
                refresh_status_bar()
            timestamp = time.strftime("%H:%M:%S")
            _log_prefix = "◀ " if source == "loopback" else "▶ "
            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{timestamp}] {_log_prefix}[{src_label}] {src_text}\n")
                log_f.write(f"[{timestamp}] {_log_prefix}[{dst_label}] {result}\n\n")
            _webui_send({"type": "transcription", "source": source,
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp})

    def translate_and_print(seq, src_text, translator, pending, next_seq, lock, source, asr_elapsed=0):
        with _active_trans_lock:
            _active_translations[0] += 1
        try:
            t0 = time.monotonic()
            result = translator.translate(src_text)
            elapsed = time.monotonic() - t0
            if result and not isinstance(translator, OllamaTranslator):
                result = S2TWP.convert(result)
            with lock:
                pending[seq] = (src_text, result, elapsed, asr_elapsed)
            _drain_translations(pending, next_seq, lock, source)
        finally:
            with _active_trans_lock:
                _active_translations[0] -= 1

    # ── 有序非同步辨識（兩組）──
    # Loopback ASR pipeline
    lb_transcribe_seq = [0]
    lb_pending_results = {}
    lb_next_display_seq = [0]
    lb_results_lock = threading.Lock()

    # Mic ASR pipeline
    mic_transcribe_seq = [0]
    mic_pending_results = {}
    mic_next_display_seq = [0]
    mic_results_lock = threading.Lock()

    _TRANSCRIBE_FAILED = "FAILED"

    # 共用辨識計數（保護 CPU / GPU）
    _active_transcriptions = [0]
    _active_lock = threading.Lock()
    # 翻譯 thread 計數（等待停止時用）
    _active_translations = [0]
    _active_trans_lock = threading.Lock()
    # 允許 3 個排隊（1 執行中 + 2 等鎖）；序列化鎖防止 CPU/GPU 競爭
    _MAX_CONCURRENT_TRANSCRIPTIONS = 3
    _serial_lock = threading.Lock()  # 序列化所有辨識（GPU 或 CPU 都受益）

    _slow_warned = [False]

    def transcribe_chunk(seq, wav_path, lang, pending_res, res_lock):
        with _active_lock:
            _active_transcriptions[0] += 1
        try:
            if not wav_path or not os.path.isfile(wav_path):
                with res_lock:
                    pending_res[seq] = _TRANSCRIBE_FAILED
                return
            # 序列化鎖：Metal GPU 不允許並行 command buffer（會 crash）
            # 超時 = step_sec：等太久不如放棄，下一個 chunk 有更新的音訊
            if not _serial_lock.acquire(timeout=step_sec):
                with res_lock:
                    pending_res[seq] = _TRANSCRIBE_FAILED
                return
            try:
                if stop_event.is_set() or not os.path.isfile(wav_path):
                    with res_lock:
                        pending_res[seq] = _TRANSCRIBE_FAILED
                    return
                segments, full_text, proc_time, detected_lang = local_transcribe(wav_path, lang)
            finally:
                _serial_lock.release()
            with res_lock:
                pending_res[seq] = (segments, full_text, proc_time, detected_lang)
            if not _slow_warned[0] and proc_time > (length_ms / 1000.0) * 1.5:
                _slow_warned[0] = True
                _rec = _recommended_whisper_model(mode)
                if _rec != model_name:
                    with print_lock:
                        print(f"\n  {C_WARN}[提示] 辨識耗時 {proc_time:.1f}s，建議改用 {_rec}（此裝置適合）{RESET}", flush=True)
                        print(f"  {C_DIM}下次啟動可用 -m {_rec} 參數{RESET}\n", flush=True)
        except Exception as e:
            with print_lock:
                print(f"{C_DIM}  [本機辨識失敗: {e}]{RESET}", flush=True)
            with res_lock:
                pending_res[seq] = _TRANSCRIBE_FAILED
        finally:
            # 先刪檔再遞減 active，避免主迴圈寫新檔到同一路徑後被舊 thread 刪除
            try:
                os.unlink(wav_path)
            except Exception:
                pass
            with _active_lock:
                _active_transcriptions[0] -= 1

    # ── 去重（兩組各自獨立）──
    lb_recent = deque(maxlen=10)
    mic_recent = deque(maxlen=10)

    def is_duplicate(text, recent):
        from difflib import SequenceMatcher
        text_lower = text.lower().strip()
        if not text_lower:
            return True
        for prev in recent:
            if text_lower == prev:
                return True
            # 子字串比對：重疊度 > 70% 時算重複
            shorter = min(len(text_lower), len(prev))
            longer = max(len(text_lower), len(prev))
            if shorter > 0 and (text_lower in prev or prev in text_lower):
                if shorter / longer > 0.7:
                    return True
            # 字元相似度比對：滑動視窗重疊導致文字略有不同但內容重複
            # shorter >= 8 避免短句誤判（如「中文測試」vs「英文測試」ratio=0.75）
            if shorter >= 8 and SequenceMatcher(None, text_lower, prev).ratio() > 0.6:
                return True
        return False

    # ── 排乾辨識結果 ──
    # 語言→幻覺檢查對照（語言預偵測模式用）
    _hallu_by_lang = {"en": _is_en_hallucination, "zh": _is_zh_hallucination,
                      "ja": _is_ja_hallucination}

    def drain_ordered_results(source, pending_res, next_disp, res_lock,
                              trans_seq, trans_pending, trans_next, trans_lock,
                              translator, recent, lang, hallucination_check,
                              skip_langs=None):
        """排乾辨識結果。skip_langs: set of lang codes，偵測到這些語言時跳過翻譯直接顯示"""
        _NOT_READY = object()
        labels = bidi_cfg[source]
        src_color, src_label = labels[0], labels[1]
        if source == "loopback":
            prefix = "◀ "
        else:
            prefix = "    ▶ "
        while True:
            with res_lock:
                result = pending_res.pop(next_disp[0], _NOT_READY)
            if result is _NOT_READY:
                break
            next_disp[0] += 1
            if result is _TRANSCRIBE_FAILED:
                continue
            segments, full_text, proc_time, detected_lang = result
            if not full_text:
                continue
            # 語言預偵測模式：用 detected_lang 決定處理邏輯
            _effective_lang = detected_lang if (lang is None) else lang
            _skip_this = (skip_langs is not None and _effective_lang in skip_langs)
            _hallu_fn = _hallu_by_lang.get(_effective_lang, hallucination_check) if (lang is None) else hallucination_check
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
                # 中文輸入做 S2TWP 轉換（語言預偵測時根據 detected_lang 判斷）
                if _effective_lang == "zh":
                    line = S2TWP.convert(line)
                if _hallu_fn(line):
                    continue
                if is_duplicate(line, recent):
                    continue
                recent.append(line.lower().strip())
                seq = trans_seq[0]; trans_seq[0] += 1
                if translator is None or _skip_this:
                    # 不翻譯：直接塞入翻譯佇列，用 _NO_TRANSLATE 哨兵
                    with trans_lock:
                        trans_pending[seq] = (line, _NO_TRANSLATE, 0, proc_time)
                    _lbl = {"en": "EN", "ja": "日", "zh": "中"}.get(_effective_lang, _effective_lang.upper()) if _skip_this else None
                    _drain_translations(trans_pending, trans_next, trans_lock, source,
                                        label_override=_lbl)
                else:
                    threading.Thread(
                        target=translate_and_print,
                        args=(seq, line, translator, trans_pending, trans_next, trans_lock, source, proc_time),
                        daemon=True,
                    ).start()

    # ── 清理 ──
    _cleaned_up = [False]

    def _cleanup_bidi():
        if _cleaned_up[0]:
            return
        _cleaned_up[0] = True
        stop_event.set()
        # 等待進行中的辨識完成，避免 MLX Metal mutex 崩潰
        _still_active = False
        for _w in range(20):  # 最多等 2 秒（os._exit 會強制結束，不需等太久）
            with _active_lock:
                if _active_transcriptions[0] <= 0:
                    break
            time.sleep(0.1)
        else:
            _still_active = True
        # 等待進行中的翻譯完成，避免翻譯輸出混入錄音儲存訊息
        for _w in range(20):  # 最多等 2 秒
            with _active_trans_lock:
                if _active_translations[0] <= 0:
                    break
            time.sleep(0.1)
        for s in (mic_stream, lb_stream):
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        if recorder_lb:
            p1 = recorder_lb.close()
            print(f"\n  {C_OK}錄音已儲存: {p1}{RESET}", flush=True)
        if recorder_mic:
            p2 = recorder_mic.close()
            print(f"  {C_OK}錄音已儲存: {p2}{RESET}", flush=True)
        if recorder_lb or recorder_mic:
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)
        return _still_active

    _sigint_count = [0]

    def signal_handler(signum, frame):
        _sigint_count[0] += 1
        if _sigint_count[0] >= 2:
            # 第二次 Ctrl+C：強制結束，跳過所有清理
            _force_exit(1)
        clear_status_bar()
        restore_terminal()
        _cleanup_bidi()
        print(f"\n{C_DIM}正在停止...{RESET}", flush=True)
        _webui_send({"type": "progress", "stage": "正在停止", "detail": ""})
        # 一律用 os._exit() 避免 atexit/thread 清理造成卡住
        _force_exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── 啟動音訊串流 ──
    lb_stream.start()
    mic_stream.start()

    # ── 驗證音訊 ──
    _audio_ok = [False, False]  # [lb, mic]
    for _chk in range(6):
        time.sleep(0.5)
        with lb_ring_lock:
            _lb_f = lb_ring_filled
        with mic_ring_lock:
            _mic_f = mic_ring_filled
        if not _audio_ok[0] and _lb_f > 0:
            _audio_ok[0] = True
            print(f"  {C_DIM}系統音訊已連接（{lb_sr}Hz）{RESET}", flush=True)
        if not _audio_ok[1] and _mic_f > 0:
            _audio_ok[1] = True
            print(f"  {C_DIM}麥克風已連接（{mic_sr}Hz）{RESET}", flush=True)
        if all(_audio_ok):
            break
    if not _audio_ok[0]:
        print(f"  {C_HIGHLIGHT}[警告] 3 秒內未收到系統音訊{RESET}", flush=True)
    if not _audio_ok[1]:
        print(f"  {C_HIGHLIGHT}[警告] 3 秒內未收到麥克風音訊{RESET}", flush=True)

    # 開始監聽提示
    _mic_color = bidi_cfg["mic"][0]  # mic src_color
    if mic_translate:
        _listen_mic_hint = f"{_mic_color}▶ 麥克風（{_mic_dir}）{RESET}"
    else:
        _listen_mic_hint = f"{_mic_color}▶ 麥克風（{_mic_dir}）{RESET}"
    print(f"\n{C_OK}{BOLD}開始監聽...{RESET} {C_OK}◀ 系統音訊（{_lb_dir}）{RESET}  {_listen_mic_hint}\n\n", flush=True)
    _webui_send({"type": "progress", "stage": "", "detail": ""})
    _webui_send({"type": "started", "mode": mode})

    _tr_model = translator_lb.model if isinstance(translator_lb, OllamaTranslator) else ("NLLB" if isinstance(translator_lb, NllbTranslator) else "")
    _tr_loc = "伺服器" if isinstance(translator_lb, OllamaTranslator) else ("本機" if isinstance(translator_lb, NllbTranslator) else "")
    setup_status_bar(mode, model_name=f"Whisper {model_name}", asr_location="本機",
                     translate_model=_tr_model, translate_location=_tr_loc)
    if hasattr(signal, 'SIGWINCH'):
        signal.signal(signal.SIGWINCH, _handle_sigwinch)

    # ── 主迴圈 ──
    step_sec = step_ms / 1000.0
    next_time_lb = time.monotonic() + (length_ms / 1000.0)
    # mic 錯開 step_sec/2（1.5s），讓 loopback 和 mic 交錯辨識，避免序列化鎖衝突
    next_time_mic = time.monotonic() + (length_ms / 1000.0) + step_sec / 2
    _last_lb_rms = 0.0  # echo gate：追蹤 loopback RMS，動態調整 mic 門檻

    try:
        while not stop_event.is_set():
            time.sleep(0.05)
            if _status_bar_active:
                with print_lock:
                    refresh_status_bar()
            now = time.monotonic()
            if pause_event.is_set():
                next_time_lb = now + step_sec
                next_time_mic = now + step_sec
                continue

            # ── Loopback pipeline ──
            if now >= next_time_lb:
                with lb_ring_lock:
                    filled_lb = lb_ring_filled
                if filled_lb >= lb_ring_size:
                    with _active_lock:
                        active = _active_transcriptions[0]
                    if active < _MAX_CONCURRENT_TRANSCRIPTIONS:
                        wav_path, rms = extract_wav_lb()
                        _last_lb_rms = rms
                        if rms >= 0.001:
                            seq = lb_transcribe_seq[0]; lb_transcribe_seq[0] += 1
                            threading.Thread(
                                target=transcribe_chunk,
                                args=(seq, wav_path, lb_lang, lb_pending_results, lb_results_lock),
                                daemon=True,
                            ).start()
                        else:
                            try: os.unlink(wav_path)
                            except Exception: pass
                        next_time_lb = now + step_sec

            drain_ordered_results("loopback", lb_pending_results, lb_next_display_seq, lb_results_lock,
                                  _trans_seq_lb, _trans_pending_lb, _trans_next_lb, _trans_lock_lb,
                                  translator_lb, lb_recent, lb_lang, lb_hallu)

            # ── Mic pipeline ── 與 loopback 共用 _MAX_CONCURRENT 互斥
            if now >= next_time_mic:
                with mic_ring_lock:
                    filled_mic = mic_ring_filled
                if filled_mic >= mic_ring_size:
                    with _active_lock:
                        active = _active_transcriptions[0]
                    if active < _MAX_CONCURRENT_TRANSCRIPTIONS:
                        wav_path, rms = extract_wav_mic()
                        # mic_translate=True（en_zh 雙向）：門檻 0.003 過濾喇叭漏音
                        # mic_translate=False（--mic）：門檻 0.002（峰值 RMS，適合藍牙麥克風）
                        _mic_rms_threshold = 0.003 if mic_translate else 0.002
                        # Echo gate：loopback 有聲音時適度提高 mic 門檻，防止喇叭漏音觸發 ASR
                        # AirPods 正常說話峰值 RMS ~0.01-0.03，門檻不能高於 0.015
                        if _last_lb_rms > 0.01:
                            _mic_rms_threshold = max(_mic_rms_threshold, 0.015)
                        if rms >= _mic_rms_threshold:
                            seq = mic_transcribe_seq[0]; mic_transcribe_seq[0] += 1
                            threading.Thread(
                                target=transcribe_chunk,
                                args=(seq, wav_path, mic_lang, mic_pending_results, mic_results_lock),
                                daemon=True,
                            ).start()
                        else:
                            try: os.unlink(wav_path)
                            except Exception: pass
                        next_time_mic = now + step_sec

            drain_ordered_results("mic", mic_pending_results, mic_next_display_seq, mic_results_lock,
                                  _trans_seq_mic, _trans_pending_mic, _trans_next_mic, _trans_lock_mic,
                                  translator_mic, mic_recent, mic_lang, mic_hallu,
                                  skip_langs=_mic_skip_langs)

    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

    clear_status_bar()
    restore_terminal()
    _force = _cleanup_bidi()
    if _force:
        _force_exit(0)


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
    if IS_WINDOWS:
        try:
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    # 方向鍵/功能鍵開頭：吃掉第二個 scan code
                    if ch in (b'\x00', b'\xe0'):
                        if msvcrt.kbhit():
                            msvcrt.getch()
                        continue
                    if ch == b'\x1b':
                        break
                else:
                    time.sleep(0.1)
        except (KeyboardInterrupt, EOFError):
            pass
    else:
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

    _MODE_FNAME = {"en2zh": "英翻中", "zh2en": "中翻英", "ja2zh": "日翻中", "zh2ja": "中翻日",
                   "en_zh": "英中雙向", "ja_zh": "日中雙向", "en": "英文", "zh": "中文", "ja": "日文"}

    def __init__(self, samplerate=16000, channels=1, fmt=None, topic=None, mode=None):
        os.makedirs(RECORDING_DIR, exist_ok=True)
        from datetime import datetime
        mode_part = f"_{self._MODE_FNAME[mode]}" if mode and mode in self._MODE_FNAME else ""
        topic_part = _topic_to_filename_part(topic)
        fname = datetime.now().strftime(f"錄音{mode_part}{topic_part}_%Y%m%d_%H%M%S.wav")
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
        """將中間 WAV 轉檔為目標格式。成功後刪除 WAV，更新 self.path。
        轉檔過程顯示 spinner + 進度百分比。"""
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

        # 計算 WAV 時長與檔案大小
        duration_s = self._data_size / max(self._samplerate * self._channels * self._sampwidth, 1)
        duration_us = int(duration_s * 1_000_000)
        try:
            wav_size = os.path.getsize(wav_path)
        except OSError:
            wav_size = 0
        dur_mm, dur_ss = divmod(int(duration_s), 60)
        dur_str = f"{dur_mm:02d}:{dur_ss:02d}"
        size_str = f"{wav_size / 1048576:.1f} MB" if wav_size else ""
        info_str = f"（時長 {dur_str}" + (f", {size_str}" if size_str else "") + "）"

        cmd = ["ffmpeg", "-y", "-i", wav_path, "-progress", "pipe:1",
               "-loglevel", "quiet"] + args + [out_path]
        spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        progress_pct = [0]  # mutable for thread access
        ffmpeg_done = threading.Event()

        def _read_progress(proc):
            """背景讀取 ffmpeg -progress 輸出，解析 out_time_us 算百分比"""
            try:
                for line in proc.stdout:
                    if line.startswith("out_time_us=") and duration_us > 0:
                        try:
                            us = int(line.split("=", 1)[1].strip())
                            progress_pct[0] = min(int(us * 100 / duration_us), 99)
                        except (ValueError, IndexError):
                            pass
            except Exception:
                pass
            finally:
                ffmpeg_done.set()

        try:
            fmt_upper = fmt.upper()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", **_SUBPROCESS_FLAGS)
            reader = threading.Thread(target=_read_progress, args=(proc,), daemon=True)
            reader.start()

            spin_idx = 0
            start_t = time.monotonic()
            timeout_s = 300
            _webui_send({"type": "progress", "stage": "存檔中", "detail": f"錄音轉檔 WAV → {fmt_upper}"})
            while not ffmpeg_done.is_set():
                pct = progress_pct[0]
                ch = spinner_chars[spin_idx % len(spinner_chars)]
                line_text = f"\r{C_DIM}{ch} 正在轉檔 WAV → {fmt_upper}  {pct}%{info_str}{RESET}"
                sys.stdout.write(line_text)
                sys.stdout.flush()
                spin_idx += 1
                if time.monotonic() - start_t > timeout_s:
                    proc.kill()
                    break
                ffmpeg_done.wait(timeout=0.1)

            proc.wait(timeout=10)
            # 清除 spinner 行
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()

            if proc.returncode == 0 and os.path.exists(out_path):
                os.remove(wav_path)
                self.path = out_path
                try:
                    out_size = os.path.getsize(out_path)
                    out_str = f"（{out_size / 1048576:.1f} MB）"
                except OSError:
                    out_str = ""
                print(f"{C_OK}✓ WAV → {fmt_upper} 轉檔完成{out_str}{RESET}")
                _webui_send({"type": "progress", "stage": "存檔完成", "detail": f"{fmt_upper} {out_str}"})
            else:
                print(f"{C_WARN}[警告] 錄音轉 {fmt} 失敗（保留 WAV）{RESET}")
                _webui_send({"type": "progress", "stage": "存檔", "detail": f"轉檔失敗，保留 WAV"})
        except Exception:
            # 清除可能殘留的 spinner
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            print(f"{C_WARN}[警告] 錄音轉 {fmt} 失敗（保留 WAV）{RESET}")

    def close(self):
        try:
            self._write_header()
            self._f.close()
        except Exception:
            pass
        self._convert()
        return self.path


class _DualStreamMixer:
    """混合兩個音訊串流（WASAPI Loopback + 麥克風）寫入單一 _AudioRecorder"""

    def __init__(self, recorder, samplerate):
        import numpy as np
        self._recorder = recorder
        self._sr = samplerate
        self._np = np
        self._lock = threading.Lock()
        self._chunk = int(samplerate * 0.1)  # 每 100ms flush
        self._lb_buf = np.zeros(0, dtype=np.float32)
        self._mic_buf = np.zeros(0, dtype=np.float32)

    def add_loopback(self, mono_f32):
        with self._lock:
            self._lb_buf = self._np.concatenate([self._lb_buf, mono_f32])
            self._flush()

    def add_mic(self, mono_f32):
        with self._lock:
            self._mic_buf = self._np.concatenate([self._mic_buf, mono_f32])
            self._flush()

    def _flush(self):
        n = min(len(self._lb_buf), len(self._mic_buf))
        if n < self._chunk:
            return
        n = (n // self._chunk) * self._chunk
        mixed = self._lb_buf[:n] * 0.7 + self._mic_buf[:n] * 0.7
        self._lb_buf = self._lb_buf[n:]
        self._mic_buf = self._mic_buf[n:]
        self._recorder.write(self._np.clip(mixed, -1.0, 1.0))

    def flush_remaining(self):
        """停止時 flush 剩餘 buffer"""
        with self._lock:
            n = max(len(self._lb_buf), len(self._mic_buf))
            if n == 0:
                return
            lb = self._np.pad(self._lb_buf, (0, max(0, n - len(self._lb_buf))))
            mic = self._np.pad(self._mic_buf, (0, max(0, n - len(self._mic_buf))))
            mixed = lb * 0.7 + mic * 0.7
            self._recorder.write(self._np.clip(mixed, -1.0, 1.0))
            self._lb_buf = self._np.zeros(0, dtype=self._np.float32)
            self._mic_buf = self._np.zeros(0, dtype=self._np.float32)


def _setup_mixed_recording(stop_event, meeting_topic):
    """建立 Windows 混合錄音（WASAPI Loopback + 麥克風）。
    回傳 (recorder, mixer, lb_stream, mic_stream) 或 None（失敗時）。"""
    import sounddevice as sd
    import numpy as np

    wb_info = _find_wasapi_loopback()
    mic_id = _find_default_mic()
    if not wb_info or mic_id is None:
        return None

    lb_sr = int(wb_info["defaultSampleRate"])
    lb_ch = wb_info["maxInputChannels"]
    mic_info = sd.query_devices(mic_id)
    mic_sr = int(mic_info["default_samplerate"])

    # 統一用 Loopback 取樣率作為錄音取樣率
    rec_sr = lb_sr
    recorder = _AudioRecorder(rec_sr, 1, topic=meeting_topic)
    mixer = _DualStreamMixer(recorder, rec_sr)

    def lb_callback(indata, frames, time_info, status):
        if stop_event.is_set():
            return
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            mono = audio.mean(axis=1)
        else:
            mono = audio.flatten()
        mixer.add_loopback(mono)

    def mic_callback(indata, frames, time_info, status):
        if stop_event.is_set():
            return
        audio = indata.astype(np.float32)
        if audio.ndim > 1 and audio.shape[1] > 1:
            mono = audio.mean(axis=1)
        else:
            mono = audio.flatten()
        # 麥克風取樣率與 Loopback 不同時，用 np.interp 重採樣
        if mic_sr != rec_sr:
            n_out = int(len(mono) * rec_sr / mic_sr)
            if n_out > 0:
                mono = np.interp(
                    np.linspace(0, len(mono) - 1, n_out),
                    np.arange(len(mono)),
                    mono,
                ).astype(np.float32)
        mixer.add_mic(mono)

    try:
        lb_stream = _WasapiLoopbackStream(
            callback=lb_callback, samplerate=lb_sr,
            channels=lb_ch, blocksize=int(lb_sr * 0.1))
    except Exception as e:
        print(f"{C_HIGHLIGHT}[警告] 無法開啟 WASAPI Loopback 錄音: {e}{RESET}")
        recorder.close()
        return None

    try:
        mic_stream = sd.InputStream(
            device=mic_id, samplerate=mic_sr,
            channels=1, dtype="float32",
            blocksize=int(mic_sr * 0.1),
            callback=mic_callback)
    except Exception as e:
        print(f"{C_HIGHLIGHT}[警告] 無法開啟麥克風錄音: {e}{RESET}")
        lb_stream.close()
        recorder.close()
        return None

    return recorder, mixer, lb_stream, mic_stream


def _auto_detect_rec_device():
    """自動偵測錄音裝置。回傳 (device_id, device_name, label) 或 (None, None, None)"""
    # Windows: 優先用 WASAPI Loopback（有麥克風時用混合模式）
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            mic_id = _find_default_mic()
            if mic_id is not None:
                import sounddevice as sd
                mic_name = sd.query_devices(mic_id)["name"]
                return WASAPI_MIXED_ID, f"WASAPI Loopback + {mic_name}", "雙方聲音"
            return WASAPI_LOOPBACK_ID, wb_info["name"], "僅對方聲音"

    import sounddevice as sd
    devices = sd.query_devices()
    if IS_MACOS:
        # 1) 聚集裝置（macOS 專有）
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                if "聚集" in name or "aggregate" in name.lower():
                    return i, name, "雙方聲音"
        # 2) input channels >= 3 的 Apple 虛擬裝置
        for i, dev in enumerate(devices):
            if (dev["max_input_channels"] >= 3
                    and not _is_loopback_device(dev["name"])):
                return i, dev["name"], "雙方聲音"
    # 3) Loopback 裝置（BlackHole / WASAPI Loopback）
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
            return i, dev["name"], "僅對方聲音"
    return None, None, None


def _ask_record_source():
    """純錄音模式：選擇錄音來源（雙方聲音 / 僅對方聲音）。
    回傳 (device_id, device_name, label)，找不到裝置則 sys.exit(1)。"""
    _last_rec = _config.get("last_rec_choice")  # "1"=混合/雙方 / "2"=僅播放/僅對方
    # Windows: WASAPI Loopback + 麥克風混合
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            mic_id = _find_default_mic()
            if mic_id is not None:
                import sounddevice as sd
                mic_name = sd.query_devices(mic_id)["name"]
                lb_name = f"WASAPI Loopback ({wb_info['name']})"
                mixed_name = f"{lb_name} + {mic_name}"
                _tag0 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "1" else ""
                _tag1 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "2" else ""
                print(f"\n\n{C_TITLE}{BOLD}▎ 錄音來源{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"  {C_HIGHLIGHT}{BOLD}[0] 雙方聲音{RESET}  {C_WHITE}對方播放 + 我方麥克風{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag0}")
                print(f"  {C_DIM}    {mixed_name}{RESET}")
                print(f"  {C_DIM}[1]{RESET} {C_WHITE}僅對方聲音{RESET}  {C_DIM}只錄製系統播放的聲音{RESET}{_tag1}")
                print(f"  {C_DIM}    {lb_name}{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"{C_WHITE}選擇 (0-1) [0]：{RESET}", end=" ")
                try:
                    user_input = input().strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)
                if user_input == "1":
                    print(f"  {C_OK}→ 僅對方聲音{RESET}")
                    if _last_rec != "2":
                        _config["last_rec_choice"] = "2"
                        save_config(_config)
                    return WASAPI_LOOPBACK_ID, wb_info["name"], "僅對方聲音"
                else:
                    print(f"  {C_OK}→ 雙方聲音{RESET}")
                    if _last_rec != "1":
                        _config["last_rec_choice"] = "1"
                        save_config(_config)
                    return WASAPI_MIXED_ID, mixed_name, "雙方聲音"
            else:
                print(f"  {C_OK}錄音裝置: WASAPI Loopback ({wb_info['name']}){RESET}")
                return WASAPI_LOOPBACK_ID, wb_info["name"], "僅對方聲音"

    import sounddevice as sd
    devices = sd.query_devices()

    # 偵測可用裝置
    aggregate_dev = None   # 聚集裝置（雙方聲音，macOS 專有）
    loopback_dev = None    # Loopback（僅對方聲音）

    for i, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        name = dev["name"]
        # 聚集裝置（macOS 專有）
        if IS_MACOS and aggregate_dev is None:
            if "聚集" in name or "aggregate" in name.lower():
                aggregate_dev = (i, name)
            elif dev["max_input_channels"] >= 3 and not _is_loopback_device(name):
                aggregate_dev = (i, name)
        # Loopback 裝置
        if loopback_dev is None and _is_loopback_device(name):
            loopback_dev = (i, name)

    # 兩種裝置都找不到 → 用系統預設
    if aggregate_dev is None and loopback_dev is None:
        default = sd.default.device[0]
        if default is not None and default >= 0:
            dev = sd.query_devices(default)
            print(f"{C_HIGHLIGHT}[提醒] 未偵測到聚集裝置或 {_LOOPBACK_LABEL}，使用系統預設輸入{RESET}")
            return default, dev["name"], "系統預設"
        print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
        sys.exit(1)

    # 只有一種裝置 → 直接使用
    if aggregate_dev is None:
        return loopback_dev[0], loopback_dev[1], "僅對方聲音"
    if loopback_dev is None:
        return aggregate_dev[0], aggregate_dev[1], "雙方聲音"

    # 兩種都有 → 讓使用者選擇
    # 檢查聚集裝置是否包含麥克風（ch >= 3 表示有 Loopback 2ch + Mic）
    agg_ch = devices[aggregate_dev[0]]["max_input_channels"]
    agg_warn = ""
    if agg_ch < 3:
        agg_warn = f"\n  {C_ERR}    [提醒] 此聚集裝置僅 {agg_ch}ch，未包含麥克風，無法錄到我方聲音{RESET}\n  {C_ERR}    請在「音訊 MIDI 設定」將麥克風加入聚集裝置（需 3ch 以上）{RESET}"
    _tag0 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "1" else ""
    _tag1 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "2" else ""
    print(f"\n\n{C_TITLE}{BOLD}▎ 錄音來源{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_HIGHLIGHT}{BOLD}[0] 雙方聲音{RESET}  {C_WHITE}對方播放 + 我方麥克風{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag0}")
    print(f"  {C_DIM}    {aggregate_dev[1]} ({agg_ch}ch){RESET}{agg_warn}")
    print(f"  {C_DIM}[1]{RESET} {C_WHITE}僅對方聲音{RESET}  {C_DIM}只錄製系統播放的聲音{RESET}{_tag1}")
    print(f"  {C_DIM}    {loopback_dev[1]}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"{C_WHITE}選擇 (0-1) [0]：{RESET}", end=" ")

    try:
        user_input = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if user_input == "1":
        print(f"  {C_OK}→ 僅對方聲音{RESET}")
        if _last_rec != "2":
            _config["last_rec_choice"] = "2"
            save_config(_config)
        return loopback_dev[0], loopback_dev[1], "僅對方聲音"
    else:
        print(f"  {C_OK}→ 雙方聲音{RESET}")
        if _last_rec != "1":
            _config["last_rec_choice"] = "1"
            save_config(_config)
        return aggregate_dev[0], aggregate_dev[1], "雙方聲音"


def run_record_only(rec_device, topic=None):
    """純錄音模式：僅錄製音訊為 WAV 檔，不做 ASR 或翻譯。
    聚集裝置（ch>=3）自動分離輸出/輸入音軌並分開顯示波形。"""
    import sounddevice as sd
    import numpy as np

    _is_mixed = IS_WINDOWS and rec_device == WASAPI_MIXED_ID
    _mixer = None
    _mic_stream = None

    if _is_mixed:
        # Windows 混合錄音模式：2 個串流（Loopback + Mic），波形顯示 2 行
        wb_info = _find_wasapi_loopback()
        rec_sr = int(wb_info["defaultSampleRate"])
        rec_ch = 2  # 波形顯示用 2 行（Loopback / Mic）
        dev_name = "WASAPI 混合錄音"
    elif IS_WINDOWS and rec_device == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        rec_sr = int(wb_info["defaultSampleRate"])
        rec_ch = wb_info["maxInputChannels"]
        dev_name = f"WASAPI Loopback ({wb_info['name']})"
    else:
        dev_info = sd.query_devices(rec_device)
        rec_sr = int(dev_info["default_samplerate"])
        rec_ch = max(dev_info["max_input_channels"], 1)
        dev_name = dev_info["name"]

    stop_event = threading.Event()

    # 每個聲道獨立的滾動音量歷史（波形顯示）
    _WAVE_MAX = 80  # 最多保留 80 筆歷史（約 8 秒）
    _level_lock = threading.Lock()

    if _is_mixed:
        # 混合模式：用 _DualStreamMixer，波形分 Loopback / Mic 兩行
        recorder = _AudioRecorder(rec_sr, 1, topic=topic)
        _mixer = _DualStreamMixer(recorder, rec_sr)
        _ch_histories = [deque(maxlen=_WAVE_MAX), deque(maxlen=_WAVE_MAX)]

        def lb_callback(indata, frames, time_info, status):
            if stop_event.is_set():
                return
            audio = indata.astype(np.float32)
            if audio.ndim > 1 and audio.shape[1] > 1:
                mono = audio.mean(axis=1)
            else:
                mono = audio.flatten()
            _mixer.add_loopback(mono)
            with _level_lock:
                _ch_histories[0].append(float(np.sqrt(np.mean(mono ** 2))))

        mic_id = _find_default_mic()
        mic_info = sd.query_devices(mic_id)
        mic_sr = int(mic_info["default_samplerate"])

        def mic_callback(indata, frames, time_info, status):
            if stop_event.is_set():
                return
            audio = indata.astype(np.float32)
            if audio.ndim > 1 and audio.shape[1] > 1:
                mono = audio.mean(axis=1)
            else:
                mono = audio.flatten()
            # 重採樣
            if mic_sr != rec_sr:
                n_out = int(len(mono) * rec_sr / mic_sr)
                if n_out > 0:
                    mono = np.interp(
                        np.linspace(0, len(mono) - 1, n_out),
                        np.arange(len(mono)), mono,
                    ).astype(np.float32)
            _mixer.add_mic(mono)
            with _level_lock:
                _ch_histories[1].append(float(np.sqrt(np.mean(mono ** 2))))

        try:
            stream = _WasapiLoopbackStream(
                callback=lb_callback, samplerate=rec_sr,
                channels=wb_info["maxInputChannels"],
                blocksize=int(rec_sr * 0.1))
            _mic_stream = sd.InputStream(
                device=mic_id, samplerate=mic_sr,
                channels=1, dtype="float32",
                blocksize=int(mic_sr * 0.1),
                callback=mic_callback)
        except Exception as e:
            print(f"[錯誤] 無法開啟混合錄音裝置: {e}", file=sys.stderr)
            recorder.close()
            sys.exit(1)
    else:
        recorder = _AudioRecorder(rec_sr, rec_ch, topic=topic)
        _ch_histories = [deque(maxlen=_WAVE_MAX) for _ in range(rec_ch)]

        def rec_callback(indata, frames, time_info, status):
            if stop_event.is_set():
                return
            recorder.write_raw(indata)
            data = indata.astype(np.float32)
            with _level_lock:
                if rec_ch == 1:
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    _ch_histories[0].append(rms)
                else:
                    for c in range(rec_ch):
                        rms = float(np.sqrt(np.mean(data[:, c] ** 2)))
                        _ch_histories[c].append(rms)

        try:
            if IS_WINDOWS and rec_device == WASAPI_LOOPBACK_ID:
                stream = _WasapiLoopbackStream(
                    callback=rec_callback, samplerate=rec_sr,
                    channels=rec_ch, blocksize=int(rec_sr * 0.1))
            else:
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
    if _is_mixed:
        print(f"  {C_WHITE}裝置: {dev_name} ({rec_sr}Hz){RESET}")
    else:
        print(f"  {C_WHITE}裝置: [{rec_device}] {dev_name} ({rec_ch}ch {rec_sr}Hz){RESET}")
    print(f"  {C_DIM}錄音: {recorder.path}{RESET}")
    # 聚集裝置 ch < 3 表示沒有包含麥克風
    is_name_aggregate = "聚集" in dev_name or "aggregate" in dev_name.lower()
    if is_name_aggregate and rec_ch < 3:
        print(f"  {C_ERR}[提醒] 聚集裝置僅 {rec_ch}ch，未包含麥克風！{RESET}")
        print(f"  {C_ERR}  請在「音訊 MIDI 設定」將麥克風加入聚集裝置{RESET}")
    print(f"  {C_DIM}按 Ctrl+C 停止錄音{RESET}")
    print(f"{C_TITLE}{'=' * 60}{RESET}")
    print()

    stream.start()
    if _mic_stream:
        _mic_stream.start()
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

    # SIGWINCH 偵測視窗大小變化（Windows 改用 polling）
    _resized = [False]
    def _on_winch(signum, frame):
        _resized[0] = True
    if hasattr(signal, 'SIGWINCH'):
        signal.signal(signal.SIGWINCH, _on_winch)

    # 固定時間欄位寬度（容納 H:MM:SS），波形寬度不會因跨時而跳動
    _TS_W = 7  # "H:MM:SS" = 7 字元，"MM:SS" 右對齊補空格
    _num_lines = rec_ch  # 每個聲道一行

    # 多聲道開頭: "  " + ts(7) + "  " + "3 "(2) = 13
    # 單聲道開頭: "  " + ts(7) + "  " = 11
    if rec_ch > 1:
        _CH_LABEL_W = len(str(rec_ch)) + 1  # "3 " = 2 chars for 3ch
        _BAR_W = max(60 - (_TS_W + 4 + _CH_LABEL_W), 10)
    else:
        _BAR_W = max(60 - (_TS_W + 4), 10)

    # 聲道色彩（循環 8 色，讓不同 channel 容易區分）
    _CH_COLORS = [
        "\033[38;2;100;180;255m",   # 藍
        "\033[38;2;100;220;180m",   # 青綠
        "\033[38;2;255;180;100m",   # 橘
        "\033[38;2;200;150;255m",   # 紫
        "\033[38;2;255;255;120m",   # 黃
        "\033[38;2;255;130;160m",   # 粉
        "\033[38;2;130;255;130m",   # 綠
        "\033[38;2;180;220;255m",   # 淺藍
    ]

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
                    # 清除所有波形行
                    if _num_lines > 1:
                        sys.stdout.write(f"\x1b[{_num_lines - 1}A\r\x1b[J")
                    else:
                        sys.stdout.write("\r\x1b[K")
                    sys.stdout.flush()
                    _first_draw = True

            if rec_ch == 1:
                # 單聲道：一行
                with _level_lock:
                    wave_str, cur_level = _build_wave(_ch_histories[0], _BAR_W)
                vol_color = _level_color(cur_level)
                line = f"  {C_WHITE}{BOLD}{ts}{RESET}  {vol_color}{wave_str}{RESET}"
                sys.stdout.write(f"\r\x1b[K{line}")
                sys.stdout.flush()
            else:
                # 多聲道：每個 channel 一行
                with _level_lock:
                    waves = [_build_wave(_ch_histories[c], _BAR_W) for c in range(rec_ch)]

                lines = []
                for c in range(rec_ch):
                    wave_str, cur_level = waves[c]
                    vol_color = _level_color(cur_level)
                    ch_color = _CH_COLORS[c % len(_CH_COLORS)]
                    ch_label = f"{ch_color}{c + 1}{RESET}"
                    if c == 0:
                        lines.append(f"  {C_WHITE}{BOLD}{ts}{RESET}  {ch_label} {vol_color}{wave_str}{RESET}")
                    else:
                        lines.append(f"  {' ' * _TS_W}  {ch_label} {vol_color}{wave_str}{RESET}")

                buf = ""
                if _first_draw:
                    buf = "\r\x1b[K" + ("\n\r\x1b[K").join(lines)
                    _first_draw = False
                else:
                    # 移動到第一行，重寫所有行
                    if _num_lines > 1:
                        buf = f"\x1b[{_num_lines - 1}A\r\x1b[K"
                    else:
                        buf = "\r\x1b[K"
                    buf += ("\n\r\x1b[K").join(lines)
                sys.stdout.write(buf)
                sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if _mic_stream:
            try:
                _mic_stream.stop()
                _mic_stream.close()
            except Exception:
                pass
        stream.stop()
        stream.close()
        if _mixer:
            _mixer.flush_remaining()
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


def _detect_bidi_file_pair(file_list):
    """從檔案列表偵測雙向錄音配對。
    回傳 (lb_path, mic_path) 或 None。
    配對條件：檔名含「_系統音訊」和「_麥克風」，且時間戳部分相同。"""
    import re as _re_bidi
    lb_files = {}   # timestamp → path
    mic_files = {}  # timestamp → path
    for fpath in file_list:
        fname = os.path.basename(fpath)
        m = _re_bidi.match(r"錄音.*_系統音訊_(\d{8}_\d{6})\.", fname)
        if m:
            lb_files[m.group(1)] = fpath
            continue
        m = _re_bidi.match(r"錄音.*_麥克風_(\d{8}_\d{6})\.", fname)
        if m:
            mic_files[m.group(1)] = fpath
    # 找時間戳匹配的配對
    for ts in lb_files:
        if ts in mic_files:
            return (lb_files[ts], mic_files[ts])
    return None


def _select_bidi_audio_pairs():
    """掃描 RECORDING_DIR，找出所有雙向錄音配對。
    回傳 [(lb_path, mic_path, timestamp_str), ...] 按時間倒序，或空 list。"""
    import re as _re_bidi
    AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
    lb_files = {}   # timestamp → path
    mic_files = {}  # timestamp → path
    if not os.path.isdir(RECORDING_DIR):
        return []
    for fname in os.listdir(RECORDING_DIR):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in AUDIO_EXTS:
            continue
        fpath = os.path.join(RECORDING_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        m = _re_bidi.match(r"錄音.*_系統音訊_(\d{8}_\d{6})\.", fname)
        if m:
            lb_files[m.group(1)] = fpath
            continue
        m = _re_bidi.match(r"錄音.*_麥克風_(\d{8}_\d{6})\.", fname)
        if m:
            mic_files[m.group(1)] = fpath
    # 找所有匹配的配對
    pairs = []
    for ts in lb_files:
        if ts in mic_files:
            pairs.append((lb_files[ts], mic_files[ts], ts))
    # 按時間戳倒序
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


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


def _ask_record(prefer_mix=False):
    """互動選單：詢問錄製音訊方式（混合/僅播放/不錄）。
    prefer_mix=True 時預設選「混合錄製」（用於麥克風轉錄模式）。
    回傳 (record: bool, rec_device: int or None)"""
    import sounddevice as sd

    # 偵測錄音裝置
    devices = sd.query_devices()
    aggregate_id = None
    aggregate_name = None
    loopback_id = None
    loopback_name = None

    # Windows: 優先偵測 WASAPI Loopback + 麥克風
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            loopback_id = WASAPI_LOOPBACK_ID
            loopback_name = f"WASAPI Loopback ({wb_info['name']})"
            # 偵測麥克風，有則啟用混合錄製
            mic_id = _find_default_mic()
            if mic_id is not None:
                mic_name = sd.query_devices(mic_id)["name"]
                aggregate_id = WASAPI_MIXED_ID
                aggregate_name = f"WASAPI Loopback + {mic_name}"

    if IS_MACOS:
        # 1) 聚集裝置（macOS 專有）
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
                        and not _is_loopback_device(dev["name"])):
                    aggregate_id, aggregate_name = i, dev["name"]
                    break
    # 3) Loopback 裝置（如果 Windows WASAPI 已找到就跳過）
    if loopback_id is None:
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
                loopback_id, loopback_name = i, dev["name"]
                break

    has_aggregate = aggregate_id is not None
    has_loopback = loopback_id is not None

    print(f"\n\n{C_TITLE}{BOLD}▎ 錄製音訊{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}同時錄製音訊為 WAV 檔（儲存於 recordings/）{RESET}")
    print(f"  {C_DIM}* 即時辨識僅處理播放聲音，無法即時辨識我方說話的聲音{RESET}")
    print()

    # 選項文字固定寬度對齊（「混合錄製（輸出+輸入）」顯示寬 20 全形字元）
    _rec_label1 = "混合錄製（輸出+輸入）"  # 顯示寬 20
    _rec_label2 = "僅錄播放聲音         "  # 補 9 空格對齊到顯示寬 21
    _last_rec = _config.get("last_rec_choice")  # "1"=混合 / "2"=僅播放 / "3"=不錄製
    if has_aggregate and has_loopback:
        default_choice = "1" if prefer_mix else "2"
        _tag1 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "1" else ""
        _tag2 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "2" else ""
        _tag3 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "3" else ""
        if prefer_mix:
            print(f"  {C_HIGHLIGHT}{BOLD}[1] {_rec_label1}{RESET} {C_DIM}{aggregate_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag1}")
            print(f"  {C_DIM}[2]{RESET} {C_WHITE}{_rec_label2}{RESET} {C_DIM}{loopback_name}{RESET}{_tag2}")
        else:
            print(f"  {C_DIM}[1]{RESET} {C_WHITE}{_rec_label1}{RESET} {C_DIM}{aggregate_name}{RESET}{_tag1}")
            print(f"  {C_HIGHLIGHT}{BOLD}[2] {_rec_label2}{RESET} {C_DIM}{loopback_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag2}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}{_tag3}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1-3) [{default_choice}]：{RESET}", end=" ")
    elif has_loopback:
        # 沒有聚集裝置，[1] 不可選，預設 [2]
        _tag2 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "2" else ""
        _tag3 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "3" else ""
        print(f"  {C_DIM}[1] {_rec_label1}  未偵測到聚集裝置{RESET}")
        print(f"  {C_HIGHLIGHT}{BOLD}[2] {_rec_label2}{RESET} {C_DIM}{loopback_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag2}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}{_tag3}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (2-3) [2]：{RESET}", end=" ")
        default_choice = "2"
    elif has_aggregate:
        # 有聚集但沒 Loopback（少見），[2] 不可選，預設 [1]
        _tag1 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "1" else ""
        _tag3 = f"  {C_OK}{REVERSE} 前次使用 {RESET}" if _last_rec == "3" else ""
        print(f"  {C_HIGHLIGHT}{BOLD}[1] {_rec_label1}{RESET} {C_DIM}{aggregate_name}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}{_tag1}")
        print(f"  {C_DIM}[2] {_rec_label2}  未偵測到 {_LOOPBACK_LABEL}{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}不錄製{RESET}{_tag3}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1,3) [1]：{RESET}", end=" ")
        default_choice = "1"
    else:
        # 都找不到 → fallback 手動選單
        print(f"  {C_HIGHLIGHT}[提醒] 未偵測到聚集裝置或 {_LOOPBACK_LABEL}，請手動選擇錄音裝置{RESET}")
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
        if _last_rec != "1":
            _config["last_rec_choice"] = "1"
            save_config(_config)
        return True, aggregate_id
    elif choice == "2" and has_loopback:
        print(f"  {C_OK}→ 僅錄播放聲音 [{loopback_id}] {loopback_name}{RESET}\n")
        if _last_rec != "2":
            _config["last_rec_choice"] = "2"
            save_config(_config)
        return True, loopback_id
    elif choice == "3":
        print(f"  {C_OK}→ 不錄製{RESET}\n")
        if _last_rec != "3":
            _config["last_rec_choice"] = "3"
            save_config(_config)
        return False, None
    else:
        # 無效輸入 → 使用預設
        if default_choice == "1":
            print(f"  {C_OK}→ 混合錄製 [{aggregate_id}] {aggregate_name}{RESET}\n")
            if _last_rec != "1":
                _config["last_rec_choice"] = "1"
                save_config(_config)
            return True, aggregate_id
        else:
            print(f"  {C_OK}→ 僅錄播放聲音 [{loopback_id}] {loopback_name}{RESET}\n")
            if _last_rec != "2":
                _config["last_rec_choice"] = "2"
                save_config(_config)
            return True, loopback_id


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
        # _clean_backspace 處理 backspace 殘留的 UTF-8 孤立位元組
        if hasattr(sys.stdin, 'buffer'):
            sys.stdout.flush()
            raw = sys.stdin.buffer.readline()
            user_input = _clean_backspace(raw)
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
        if IS_WINDOWS:
            os.startfile(file_path)
        else:
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
        # Windows conhost 不支援 scroll region / save-restore cursor，改用視窗標題
        self._title_mode = IS_WINDOWS and not os.environ.get("WT_SESSION")

    def start(self):
        self._stop.clear()
        self._tokens = 0
        self._first_token_time = 0
        self._t0 = time.monotonic()
        self._needs_resize = False
        if self._title_mode:
            self._active = True
            self._draw_title()
        else:
            # 設定 scroll region，保留最後一行給狀態列
            try:
                cols, rows = os.get_terminal_size()
                self._last_rows = rows
                sys.stdout.write(f"\x1b[1;{rows - 1}r")
                sys.stdout.write(f"\x1b[{rows - 1};1H")
                sys.stdout.write(f"\n")
                sys.stdout.flush()
                self._active = True
            except Exception:
                self._active = False
        # 攔截 SIGWINCH
        if hasattr(signal, 'SIGWINCH'):
            self._old_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, self._on_sigwinch)
        else:
            self._old_sigwinch = None
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
            _stage = f"{self._task}（{self._model}）" if self._model else self._task
            _webui_send({"type": "progress", "stage": f"{_stage} 完成",
                         "detail": f"{self._tokens} tokens | {tps:.1f} t/s"})
        else:
            self._frozen_stats = ""
        self._frozen = True

    def update_tokens(self, count):
        self._tokens = count
        if count > 0 and not self._first_token_time:
            self._first_token_time = time.monotonic()
        # WebUI 即時 token 進度（每 5 tokens 更新一次避免洪水）
        if count > 0 and count % 5 == 0 and self._first_token_time:
            gen_elapsed = time.monotonic() - self._first_token_time
            tps = count / gen_elapsed if gen_elapsed > 0.1 else 0
            _stage = f"{self._task}（{self._model}）" if self._model else self._task
            _webui_send({"type": "progress", "stage": _stage,
                         "detail": f"{count} tokens | {tps:.1f} t/s"})

    def _draw_title(self):
        """conhost fallback: 用視窗標題顯示摘要進度"""
        try:
            elapsed = time.monotonic() - self._t0
            m, s = divmod(int(elapsed), 60)
            time_str = f"{m:02d}:{s:02d}"
            parts = [time_str, self._model, self._task]
            if self._tokens > 0 and self._first_token_time:
                gen_elapsed = time.monotonic() - self._first_token_time
                tps = self._tokens / gen_elapsed if gen_elapsed > 0.1 else 0
                parts.append(f"{self._tokens} tokens | {tps:.1f} t/s")
            sys.stdout.write(f"\x1b]0;{' | '.join(parts)}\x07")
            sys.stdout.flush()
        except Exception:
            pass

    def _draw_loop(self):
        i = 0
        while not self._stop.is_set():
            if self._title_mode:
                self._draw_title()
            else:
                # Windows Terminal 無 SIGWINCH，改用 polling
                if IS_WINDOWS:
                    try:
                        new_rows = os.get_terminal_size().lines
                        if new_rows != self._last_rows:
                            self._needs_resize = True
                    except Exception:
                        pass
                if self._needs_resize:
                    self._needs_resize = False
                    try:
                        cols, rows = os.get_terminal_size()
                        old_rows = self._last_rows
                        self._last_rows = rows
                        with self._lock:
                            # 1. 解除 scroll region
                            sys.stdout.write("\x1b[r")
                            # 2. 清除舊 bar 位置和新 bar 位置
                            if old_rows:
                                sys.stdout.write(f"\x1b[{old_rows};1H\x1b[2K")
                            sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")
                            # 3. 重設 scroll region（保留最後一行給 bar）
                            sys.stdout.write(f"\x1b[1;{rows - 1}r")
                            # 4. 游標移到 scroll region 底部
                            sys.stdout.write(f"\x1b[{rows - 1};1H")
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
        if hasattr(signal, 'SIGWINCH'):
            try:
                signal.signal(signal.SIGWINCH, self._old_sigwinch or signal.SIG_DFL)
            except Exception:
                pass
        if self._active:
            if self._title_mode:
                # 恢復視窗標題
                try:
                    sys.stdout.write("\x1b]0;Windows PowerShell\x07")
                    sys.stdout.flush()
                except Exception:
                    pass
            else:
                try:
                    sys.stdout.write("\x1b[r")  # 重設 scroll region
                    cols, rows = os.get_terminal_size()
                    sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")  # 清除狀態列
                    sys.stdout.flush()
                except Exception:
                    pass
            self._active = False


def call_ollama_raw(prompt, model, host, port, timeout=300, spinner=None, live_output=False,
                    server_type="ollama", think=None):
    """直接呼叫 LLM API 取得回應（串流模式，可更新 spinner 進度或即時輸出）"""
    return _llm_generate(
        prompt, model, host, port, server_type,
        stream=True, timeout=timeout,
        spinner=spinner, live_output=live_output, think=think,
    )


def _correct_segments_with_llm(segments_data, model, host, port, server_type="ollama",
                                topic=None):
    """用 LLM 校正離線逐字稿的 ASR 辨識錯誤，原地修改 segments_data"""
    # 1. 提取所有文字行，建立編號對應
    all_lines = []   # [(seg_idx, line_idx, text), ...]
    for si, seg in enumerate(segments_data):
        for li, ln in enumerate(seg["lines"]):
            all_lines.append((si, li, ln["text"]))

    if not all_lines:
        return

    # 2. 查詢 context window → 計算 chunk 大小
    num_ctx = query_ollama_num_ctx(model, host, port, server_type=server_type)
    max_chars = _calc_chunk_max_chars(num_ctx)

    # 3. 分批（按字數切割）
    chunks = []       # [[(global_idx, text), ...], ...]
    current_chunk = []
    current_chars = 0
    for idx, (si, li, text) in enumerate(all_lines):
        line_len = len(text) + 10  # 序號 + 分隔符
        if current_chunk and current_chars + line_len > max_chars:
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append((idx, text))
        current_chars += line_len
    if current_chunk:
        chunks.append(current_chunk)

    # 4. 準備 topic 行
    topic_line = f"- 本次會議主題：{topic}，請根據此主題的領域知識理解專業術語並正確校正\n" if topic else ""

    # 5. 設定狀態列
    _llm_loc = "本機" if host in ("localhost", "127.0.0.1", "::1") else "伺服器"
    sbar = _SummaryStatusBar(model=model, task="LLM 校正逐字稿", location=_llm_loc).start()

    corrected = {}  # global_idx → corrected_text
    total_chunks = len(chunks)

    try:
        for ci, chunk in enumerate(chunks):
            # 組裝編號行
            numbered_lines = "\n".join(f"{i+1}|{text}" for i, (_, text) in enumerate(chunk))
            prompt = TRANSCRIPT_CORRECT_PROMPT_TEMPLATE.format(
                topic_line=topic_line, lines=numbered_lines)

            task_label = f"LLM 校正逐字稿（{ci+1}/{total_chunks}）" if total_chunks > 1 else "LLM 校正逐字稿"
            sbar.set_task(task_label)

            # timeout 依 chunk 字數動態調整（每千字 60 秒，最低 300 秒）
            _timeout = max(300, len(numbered_lines) // 1000 * 60 + 300)
            try:
                result = call_ollama_raw(prompt, model, host, port, timeout=_timeout,
                                         spinner=sbar, server_type=server_type,
                                         think=False)
            except Exception as e:
                print(f"  {C_HIGHLIGHT}[警告] 第 {ci+1}/{total_chunks} 批校正失敗: {e}{RESET}",
                      file=sys.stderr)
                continue

            if not result:
                continue

            # 移除 <think>...</think> 標籤（Qwen3 等模型可能忽略 think=False）
            result = re.sub(r'<think>[\s\S]*?</think>', '', result).strip()
            result = re.sub(r'<think>[\s\S]*', '', result).strip()

            # 簡繁轉換
            result = S2TWP.convert(result)

            # 6. 解析回傳，用正則 ^\d+\|(.+)$ 逐行匹配
            for rline in result.strip().splitlines():
                rline = rline.strip()
                m = re.match(r'^(\d+)\|(.+)$', rline)
                if not m:
                    continue
                local_idx = int(m.group(1)) - 1  # 轉回 0-based
                corrected_text = m.group(2).strip()
                if 0 <= local_idx < len(chunk):
                    global_idx = chunk[local_idx][0]
                    corrected[global_idx] = corrected_text

            if total_chunks > 1:
                print(f"  {C_OK}校正第 {ci+1}/{total_chunks} 批完成{RESET}", flush=True)
    finally:
        sbar.freeze()
        sbar.stop()

    # 7. 將校正結果寫回 segments_data，標記 [雜音] 行待刪除
    n_corrected = 0
    noise_markers = set()  # (seg_idx, line_idx) 要刪除的行
    for idx, (si, li, original) in enumerate(all_lines):
        if idx in corrected and corrected[idx] != original:
            if corrected[idx] == "[雜音]":
                noise_markers.add((si, li))
            else:
                segments_data[si]["lines"][li]["text"] = corrected[idx]
            n_corrected += 1

    # 8. 移除 [雜音] 行（反向刪除避免索引偏移）
    n_noise = 0
    if noise_markers:
        for si in range(len(segments_data) - 1, -1, -1):
            seg = segments_data[si]
            for li in range(len(seg["lines"]) - 1, -1, -1):
                if (si, li) in noise_markers:
                    seg["lines"].pop(li)
                    n_noise += 1
            # 如果整段都被刪光，移除整段
            if not seg["lines"]:
                segments_data.pop(si)

    noise_str = f"，移除 {n_noise} 行雜音" if n_noise else ""
    print(f"  {C_OK}LLM 校正完成{RESET}{C_DIM}（共 {len(all_lines)} 行，修正 {n_corrected} 行{noise_str}）{RESET}")


def query_ollama_num_ctx(model, host, port, server_type="ollama"):
    """查詢模型的 context window 大小（token 數），查不到回傳 None
    Ollama 用 /api/show，OpenAI 相容用 /v1/models 找常見欄位"""
    if server_type == "openai":
        return _query_openai_context_length(model, host, port)
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


def _query_openai_context_length(model, host, port):
    """從 OpenAI 相容 /v1/models 查詢 context length
    各伺服器欄位不同：vLLM 用 max_model_len，LM Studio / llama.cpp 用
    context_length 等。查不到回傳 None（fallback 6000 字）。"""
    # 常見欄位名稱（優先序）
    _CTX_KEYS = ("max_model_len", "context_length", "max_context_length",
                 "context_window", "n_ctx")
    try:
        url = f"http://{host}:{port}/v1/models"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in data.get("data", []):
            if m.get("id") != model:
                continue
            # 直接在 model 物件頂層找
            for k in _CTX_KEYS:
                val = m.get(k)
                if isinstance(val, (int, float)) and val > 0:
                    return int(val)
            # 部分伺服器把資訊放在 meta / model_info 子物件
            for sub in ("meta", "model_info"):
                sub_obj = m.get(sub, {})
                if not isinstance(sub_obj, dict):
                    continue
                for k in _CTX_KEYS:
                    val = sub_obj.get(k)
                    if isinstance(val, (int, float)) and val > 0:
                        return int(val)
            break
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
    if line_lower in (
        "you", "the", "bye", "so", "okay",
        "thank you", "thanks for watching",
        "thanks for listening", "see you next time",
        "subscribe", "like and subscribe",
        "don't forget to subscribe", "please subscribe",
        "please subscribe to my channel",
    ):
        return True
    # 關鍵字比對（Amara / 字幕歸屬 / 版權幻覺）
    return any(kw in line_lower for kw in (
        "amara.org", "otter.ai", "rev.com", "transcribed by",
        "subtitles by", "translated by", "captions by",
        "pomp and circumstance", "sir edward elgar",
        "© bf-watch", "© transcript",
    ))


def _is_zh_hallucination(text):
    """檢查中文文字是否為 Whisper 幻覺（YouTube 訓練資料殘留 + 重複模式）"""
    _t = text.strip()
    # 重複模式偵測 1：單一字元佔比 > 60%（如「衛衛衛衛衛...」）
    if len(_t) >= 6:
        from collections import Counter as _Counter
        _cc = _Counter(_t)
        _most = _cc.most_common(1)[0][1]
        if _most / len(_t) > 0.6:
            return True
    # 重複模式偵測 2：任何字元連續出現 6 次以上
    if re.search(r'(.)\1{5,}', _t):
        return True
    # 重複模式偵測 3：任意位置 2-4 字元片段連續重複 4 次以上（如「有多少多少多少多少...」）
    if len(_t) >= 8 and re.search(r'(.{2,4})\1{3,}', _t):
        return True
    # 太短的中文（去除標點後不到 2 個字）
    _stripped = re.sub(r'[^\u4e00-\u9fff\u3040-\u30ff]', '', _t)
    if len(_stripped) < 2:
        return True
    # 簡體+繁體關鍵字都要檢查（faster-whisper 可能輸出簡體）
    return any(kw in text for kw in (
        # YouTube 用語
        "訂閱", "订阅", "歡迎訂閱", "欢迎订阅",
        "點贊", "点赞", "點讚", "按讚", "轉發", "转发", "打賞", "打赏",
        "感謝觀看", "感谢观看", "謝謝大家", "谢谢大家", "謝謝收看", "谢谢收看",
        "感謝收聽", "感谢收听", "感謝聆聽", "感谢聆听",
        "喜歡的話", "喜欢的话", "別忘了", "别忘了",
        # 字幕/翻譯歸屬（Amara.org 訓練資料殘留）
        "字幕由", "字幕提供", "字幕志願", "字幕志愿", "字幕組", "字幕组",
        "字幕視聽", "字幕视听", "字幕製作", "字幕制作",
        "翻譯志願", "翻译志愿", "校對志願", "校对志愿",
        "Amara", "amara",
        # 版權歸屬幻覺（僅短句時過濾，長句可能是真實討論）
        "版權所有", "版权所有",
        "初音ミク", "初音",
        # 頻道/節目
        "獨播", "独播", "劇場", "剧场", "YoYo", "Television Series",
        "明鏡", "明镜", "新聞頻道", "新闻频道",
        "直播間", "直播间", "觀眾朋友", "观众朋友",
    ))
    # 短句限定：音樂/版權歸屬幻覺（長句中出現這些詞可能是真實討論，不過濾）
    if len(_stripped) <= 10:
        return any(kw in text for kw in (
            "詞曲", "词曲", "作詞", "作词", "作曲", "編曲", "编曲",
            "詞：", "词：", "曲：", "演唱", "原唱",
            "版權", "版权", "著作權", "著作权",
            "李宗盛", "周杰倫", "周杰伦", "林俊傑", "林俊杰", "蔡依林",
            "張惠妹", "张惠妹", "五月天", "陳奕迅", "陈奕迅",
            "鄧紫棋", "邓紫棋", "王力宏",
        ))
    return False


def _is_ja_hallucination(text):
    """檢查日文文字是否為 Whisper 幻覺"""
    ja_chars = sum(1 for c in text if '\u3040' <= c <= '\u309F'
                   or '\u30A0' <= c <= '\u30FF' or '\u4e00' <= c <= '\u9fff')
    if ja_chars < 2:
        return True
    return any(kw in text for kw in (
        "チャンネル登録", "高評価", "ご視聴", "コメント欄",
        "ご覧いただき", "ありがとうございました",
        "字幕提供", "字幕制作", "翻訳者",
        "Amara", "amara",
    ))


def _ffprobe_info(input_path):
    """用 ffprobe 取得音訊檔資訊，回傳 (duration_secs, format_name, sample_rate, channels) 或 None"""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", input_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace", **_SUBPROCESS_FLAGS)
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


def _convert_to_wav(input_path, source_label="來源"):
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
        _lw = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in source_label)
        _pad = ' ' * max(12 - _lw, 1)
        print(f"  {C_WHITE}{source_label}{_pad}{RESET}{C_DIM}{' | '.join(info_parts)}{RESET}")
    else:
        _lw = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in source_label)
        _pad = ' ' * max(12 - _lw, 1)
        print(f"  {C_WHITE}{source_label}{_pad}{RESET}{C_DIM}{ext} | {size_str}{RESET}")

    try:
        cmd = [
            "ffmpeg", "-i", input_path, "-ar", "16000", "-ac", "1",
            "-y", "-progress", "pipe:1", "-loglevel", "error",
            tmp_wav,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", **_SUBPROCESS_FLAGS)

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
        _ffmpeg_hint = "winget install ffmpeg" if IS_WINDOWS else "brew install ffmpeg"
        print(f"  {C_HIGHLIGHT}[錯誤] 找不到 ffmpeg，請先安裝: {_ffmpeg_hint}{RESET}",
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
            # 滑動視窗 embedding：長段落取多個 partial 後用中位數，更穩定
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
                       diarize=False, num_speakers=None, remote_whisper_cfg=None,
                       correct_with_llm=False, llm_model=None, llm_host=None,
                       llm_port=None, llm_server_type=None, meeting_topic=None):
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

    lang = "en" if mode in _EN_INPUT_MODES else ("ja" if mode in _JA_INPUT_MODES else "zh")
    need_translate = mode in _TRANSLATE_MODES

    # Log 檔名（每次處理建子目錄）
    log_prefixes = {"en2zh": "英翻中_時間逐字稿", "zh2en": "中翻英_時間逐字稿",
                    "ja2zh": "日翻中_時間逐字稿", "zh2ja": "中翻日_時間逐字稿",
                    "en": "英文_時間逐字稿", "zh": "中文_時間逐字稿", "ja": "日文_時間逐字稿"}
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
    _webui_send({"type": "progress", "stage": "準備中", "detail": os.path.basename(input_path)})

    # 標籤
    src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
    if mode in _EN_INPUT_MODES:
        hallucination_check = _is_en_hallucination
    elif mode in _JA_INPUT_MODES:
        hallucination_check = _is_ja_hallucination
    else:
        hallucination_check = _is_zh_hallucination

    # 取得音訊總時長（用於進度顯示）
    audio_duration = 0
    probe = _ffprobe_info(wav_path)
    if probe and probe[0] > 0:
        audio_duration = probe[0]

    # 在 ASR 期間背景預熱 LLM（避免 ASR 後模型已卸載導致翻譯超時）
    _warmup_thread = None
    _warmup_ok = [False]
    if need_translate and translator and hasattr(translator, "warmup"):
        import threading
        def _bg_warmup(tr=translator, result=_warmup_ok):
            result[0] = tr.warmup()
        _warmup_thread = threading.Thread(target=_bg_warmup, daemon=True)
        _warmup_thread.start()

    # 3. 辨識：GPU 伺服器 或本機
    t_stage = time.monotonic()
    used_remote = False
    raw_segments = None  # 伺服器回傳的 segments list

    if remote_whisper_cfg is not None:
        rw_host = remote_whisper_cfg.get("host", "?")
        rw_port = remote_whisper_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
        print(f"  {C_WHITE}辨識位置    GPU 伺服器（{rw_host}:{rw_port}）{RESET}")

        # 上傳前檢查伺服器狀態（忙碌/磁碟空間）
        file_size = os.path.getsize(wav_path) if os.path.isfile(wav_path) else 0
        if not _check_remote_before_upload(remote_whisper_cfg, file_size):
            print(f"  {C_HIGHLIGHT}[降級] 改用本機 辨識{RESET}")
            remote_whisper_cfg = None

    if remote_whisper_cfg is not None:
        rw_host = remote_whisper_cfg.get("host", "?")
        rw_port = remote_whisper_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
        print(f"  {C_WHITE}上傳辨識中...{RESET}\n")
        _webui_send({"type": "progress", "stage": "辨識中", "detail": f"GPU 伺服器（{rw_host}）"})

        sbar = _SummaryStatusBar(model=model_size, task="上傳音訊", asr_location="伺服器").start()

        def _upload_progress(text):
            sbar.set_progress(text)

        def _on_upload_done():
            sbar.set_task("GPU 伺服器 辨識中", reset_timer=False)
            sbar.set_progress("等待伺服器回應...")

        try:
            r_segments, r_duration, r_proc_time, r_device = _remote_whisper_transcribe(
                remote_whisper_cfg, wav_path, model_size, lang,
                progress_callback=_upload_progress,
                on_upload_done=_on_upload_done,
            )
            raw_segments = r_segments
            used_remote = True
            sbar.set_task(f"伺服器辨識完成（{len(r_segments)} 段，{r_proc_time:.1f}s，{r_device}）", reset_timer=False)
        except Exception as e:
            sbar.set_task("伺服器辨識失敗", reset_timer=False)
            sbar.freeze()
            sbar.stop()
            print(f"  {C_HIGHLIGHT}[降級] 伺服器辨識失敗: {e}{RESET}")
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
        model = _call_with_ssl_retry(WhisperModel, model_size, device="auto", compute_type="int8")
        print(f"{C_OK}✓{RESET}")
        print(f"  {C_WHITE}辨識中...{RESET}\n")
        _webui_send({"type": "progress", "stage": "辨識中", "detail": f"本機 {model_size}"})

        sbar = _SummaryStatusBar(model=model_size, task="辨識中", asr_location="本機").start()
        if audio_duration > 0:
            sbar.set_progress("0%")

        segments_iter, info = model.transcribe(wav_path, language=lang, beam_size=5, vad_filter=True)

        # 將 generator 轉為 list of dict（與伺服器格式統一）
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
            if mode in _ZH_INPUT_MODES:
                text = S2TWP.convert(text)
            valid_segments.append({
                "start": seg_raw["start"],
                "end": seg_raw["end"],
                "text": text,
            })

        t_asr_elapsed = time.monotonic() - t_stage
        _avg_asr_per_seg = t_asr_elapsed / max(len(valid_segments), 1)
        sbar.set_task(f"辨識完成（{len(valid_segments)} 段，{t_asr_elapsed:.1f}s）", reset_timer=False)
        sbar.set_progress("")
        _webui_send({"type": "progress", "stage": "辨識完成",
                     "detail": f"{len(valid_segments)} 段，{t_asr_elapsed:.1f}s"})

        # 講者辨識
        speaker_labels = None
        t_stage = time.monotonic()
        if diarize and valid_segments:
            _webui_send({"type": "progress", "stage": "講者辨識中", "detail": ""})
            # 優先嘗試GPU 伺服器 diarization
            if remote_whisper_cfg is not None:
                sbar.set_task("伺服器講者辨識（上傳中）", reset_timer=False)
                def _diarize_progress(msg):
                    sbar.set_progress(msg)
                def _diarize_upload_done():
                    sbar.set_task("伺服器講者辨識（GPU 分析中）", reset_timer=False)
                    sbar.set_progress("等待伺服器回應...")
                speaker_labels, d_proc_time = _remote_diarize(
                    remote_whisper_cfg, wav_path, valid_segments,
                    num_speakers=num_speakers,
                    progress_callback=_diarize_progress,
                    on_upload_done=_diarize_upload_done,
                )
                if speaker_labels is None:
                    # 伺服器失敗，降級本機
                    sbar.set_task("伺服器失敗，改用本機講者辨識", reset_timer=False)
                    speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                       num_speakers=num_speakers, sbar=sbar)
            else:
                speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                   num_speakers=num_speakers, sbar=sbar)
            t_diarize_elapsed = time.monotonic() - t_stage
            sbar.set_task(f"講者辨識完成（{t_diarize_elapsed:.1f}s）", reset_timer=False)
            _webui_send({"type": "progress", "stage": "講者辨識完成",
                         "detail": f"{t_diarize_elapsed:.1f}s"})

        # 等待背景預熱完成（ASR 期間已開始，通常此時早已 ready）
        if _warmup_thread is not None:
            _warmup_thread.join(timeout=120)
            if not _warmup_ok[0]:
                # 背景預熱未成功，同步重試一次
                if need_translate and translator and hasattr(translator, "warmup"):
                    print(f"  {C_DIM}預熱翻譯引擎...{RESET}", end="", flush=True)
                    if translator.warmup():
                        print(f" {C_OK}ready{RESET}")
                    else:
                        print(f" {C_HIGHLIGHT}逾時（翻譯可能不完整）{RESET}")

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
                _webui_send({"type": "progress", "stage": "輸出中", "detail": f"{seg_count}/{len(valid_segments)}"})

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
                    _print_with_badge(
                        f"{src_color}{ts_tag} {spk_tag_term}[{src_label}] {text}{RESET}",
                        C_BADGE_ASR, _avg_asr_per_seg, "辨")

                    t0 = time.monotonic()
                    result = translator.translate(text)
                    elapsed = time.monotonic() - t0

                    if result:
                        if not isinstance(translator, OllamaTranslator): result = S2TWP.convert(result)
                        _print_with_badge(
                            f"{dst_color}{BOLD}{ts_tag} {spk_tag_term}[{dst_label}] {result}{RESET}",
                            _speed_badge_color(elapsed), elapsed, "譯")
                        print(flush=True)

                        log_f.write(f"{ts_tag} {spk_tag_log}[{src_label}] {text}\n")
                        log_f.write(f"{ts_tag} {spk_tag_log}[{dst_label}] {result}\n\n")
                        _webui_send({"type": "transcription", "source": "main",
                                     "src_lang": src_label, "src_text": text,
                                     "dst_lang": dst_label, "dst_text": result,
                                     "asr_time": round(_avg_asr_per_seg, 1),
                                     "translate_time": round(elapsed, 1),
                                     "timestamp": ts_tag,
                                     "speaker": spk_num_val})
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
                    _webui_send({"type": "transcription", "source": "main",
                                 "src_lang": src_label, "src_text": text,
                                 "asr_time": round(_avg_asr_per_seg, 1),
                                 "timestamp": ts_tag,
                                 "speaker": spk_num_val})

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

        # ── LLM 文字校正（修正 ASR 辨識錯誤）──
        if correct_with_llm and segments_data and llm_model:
            sbar.stop()  # 停掉原本的狀態列，避免與校正狀態列衝突
            print(f"\n  {C_WHITE}LLM 校正逐字稿文字...{RESET}")
            _webui_send({"type": "progress", "stage": f"LLM 校正逐字稿（{llm_model}）", "detail": "等待模型回應..."})
            try:
                _correct_segments_with_llm(segments_data, llm_model, llm_host, llm_port,
                                           server_type=llm_server_type, topic=meeting_topic)
                # 用校正後的 segments_data 重寫 log 檔
                with open(log_path, "w", encoding="utf-8") as log_f:
                    for seg_d in segments_data:
                        ts_start = _format_timestamp(seg_d["start"])
                        ts_end = _format_timestamp(seg_d["end"])
                        ts_tag = f"[{ts_start}-{ts_end}]"
                        spk_tag = f"[Speaker {seg_d['speaker']}] " if seg_d.get("speaker") else ""
                        for line in seg_d["lines"]:
                            log_f.write(f"{ts_tag} {spk_tag}[{line['label']}] {line['text']}\n")
                        log_f.write("\n")
            except Exception as e:
                print(f"  {C_HIGHLIGHT}[警告] LLM 校正失敗: {e}{RESET}", file=sys.stderr)

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
            "asr_location": "GPU 伺服器" if used_remote else "本機",
            "input_file": os.path.basename(input_path),
            "meeting_topic": meeting_topic,
        }
        if translator:
            if isinstance(translator, NllbTranslator):
                _meta["translate_engine"] = "NLLB 600M"
                _meta["translate_location"] = "本機離線"
            elif isinstance(translator, ArgosTranslator):
                _meta["translate_engine"] = "Argos"
                _meta["translate_location"] = "本機離線"
            elif hasattr(translator, "model"):
                _srv_type = getattr(translator, "server_type", "")
                _srv_label = "Ollama" if _srv_type == "ollama" else "OpenAI 相容" if _srv_type == "openai" else ""
                _meta["translate_engine"] = getattr(translator, "model", "LLM")
                _loc = f"{getattr(translator, 'host', '')}:{getattr(translator, 'port', '')}"
                if _srv_label:
                    _loc += f" ({_srv_label})"
                _meta["translate_location"] = _loc
        if diarize:
            _meta["diarize"] = True
            _meta["diarize_engine"] = "resemblyzer + spectralcluster"
            if remote_whisper_cfg is not None:
                _meta["diarize_location"] = "GPU 伺服器"
            else:
                _meta["diarize_location"] = "本機"
            if num_speakers:
                _meta["num_speakers"] = num_speakers
            # 從 segments_data 計算實際辨識出的講者數
            if segments_data:
                _detected = len(set(s.get("speaker") for s in segments_data if s.get("speaker") is not None))
                if _detected >= 2:
                    _meta["detected_speakers"] = _detected
        if correct_with_llm and llm_model:
            _meta["correct_engine"] = llm_model
            _srv_label_c = "Ollama" if llm_server_type == "ollama" else "OpenAI 相容" if llm_server_type == "openai" else ""
            _loc_c = f"{llm_host}:{llm_port}"
            if _srv_label_c:
                _loc_c += f" ({_srv_label_c})"
            _meta["correct_location"] = _loc_c
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

        _webui_send({"type": "progress", "stage": "處理完成",
                     "detail": f"{seg_count} 段{diarize_info} | {total_str}"})
        print(f"\n{C_DIM}{'═' * 60}{RESET}")
        print(f"  {C_OK}{BOLD}處理完成{RESET} {C_DIM}（共 {seg_count} 段{diarize_info} | 耗時 {total_str}）{RESET}")
        if seg_count == 0:
            _mode_lang = {"en2zh": "英文", "zh2en": "中文", "ja2zh": "日文", "zh2ja": "中文",
                          "en": "英文", "zh": "中文", "ja": "日文",
                          "en_zh": "英文/中文", "ja_zh": "日文/中文"}
            _expected = _mode_lang.get(mode, mode)
            print(f"  {C_HIGHLIGHT}[注意] 辨識結果為 0 段，可能原因：{RESET}")
            print(f"  {C_HIGHLIGHT}  1. 功能模式選錯（目前: {mode}，期望音訊語言: {_expected}）{RESET}")
            print(f"  {C_HIGHLIGHT}  2. 音訊檔內容為靜音或非語音{RESET}")
            print(f"  {C_HIGHLIGHT}  3. 音訊品質太差，辨識引擎無法處理{RESET}")
            print(f"  {C_DIM}  建議：確認音訊語言後選擇正確的功能模式重新處理{RESET}")
            _webui_send({"type": "progress", "stage": "注意",
                         "detail": f"辨識結果為 0 段，請確認功能模式是否正確（目前期望: {_expected}）"})
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


def process_bidi_audio_files(lb_path, mic_path, mode, translator_lb, translator_mic,
                              model_size="large-v3-turbo", remote_whisper_cfg=None,
                              diarize=False, num_speakers=None,
                              correct_with_llm=False, llm_model=None, llm_host=None,
                              llm_port=None, llm_server_type=None, meeting_topic=None):
    """處理雙向錄音檔：兩路 ASR → 合併 → 翻譯 → 存檔，回傳 (log_path, html_path, session_dir)"""
    from datetime import datetime
    import shutil

    # 驗證檔案存在
    for _p in (lb_path, mic_path):
        if not os.path.isfile(_p):
            print(f"  {C_HIGHLIGHT}[錯誤] 檔案不存在: {_p}{RESET}", file=sys.stderr)
            return None, None, None

    lb_basename = os.path.splitext(os.path.basename(lb_path))[0]
    mic_basename = os.path.splitext(os.path.basename(mic_path))[0]
    print(f"\n\n{C_TITLE}{BOLD}▎ 雙向處理{RESET}")
    print(f"  {C_WHITE}系統音訊: {os.path.basename(lb_path)}{RESET}")
    print(f"  {C_WHITE}麥克風:   {os.path.basename(mic_path)}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")

    t_total_start = time.monotonic()

    # 語言對照
    lb_lang = _LB_LANG.get(mode, "en")
    mic_lang = _MIC_LANG.get(mode, "zh")

    # 幻覺檢查函式
    _hall_check = {"en": _is_en_hallucination, "zh": _is_zh_hallucination, "ja": _is_ja_hallucination}
    lb_hall = _hall_check.get(lb_lang, _is_en_hallucination)
    mic_hall = _hall_check.get(mic_lang, _is_zh_hallucination)

    # 轉檔
    t_stage = time.monotonic()
    lb_wav, lb_tmp = _convert_to_wav(lb_path, source_label="系統音訊")
    mic_wav, mic_tmp = _convert_to_wav(mic_path, source_label="麥克風")
    if lb_wav is None or mic_wav is None:
        return None, None, None
    t_convert = time.monotonic() - t_stage
    print(f"  {C_OK}轉檔        {RESET}{C_DIM}兩個檔案 → 16kHz mono WAV  [{t_convert:.1f}s]{RESET}")

    # Log 檔名
    log_prefixes = {"en_zh": "英中雙向_時間逐字稿", "ja_zh": "日中雙向_時間逐字稿",
                    "en2zh": "英翻中_配對時間逐字稿",
                    "zh2en": "中翻英_配對時間逐字稿", "ja2zh": "日翻中_配對時間逐字稿",
                    "zh2ja": "中翻日_配對時間逐字稿", "en": "英文_配對時間逐字稿",
                    "zh": "中文_配對時間逐字稿", "ja": "日文_配對時間逐字稿"}
    log_prefix = log_prefixes.get(mode, "配對_時間逐字稿")
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(LOG_DIR, f"{lb_basename}_{ts_str}")
    os.makedirs(session_dir, exist_ok=True)
    log_filename = f"{log_prefix}_{lb_basename}_{ts_str}.txt"
    log_path = os.path.join(session_dir, log_filename)

    # 複製原始音訊到子目錄
    for _src in (lb_path, mic_path):
        _dst = os.path.join(session_dir, os.path.basename(_src))
        if not os.path.exists(_dst):
            shutil.copy2(_src, _dst)

    print(f"  {C_WHITE}辨識語言    {RESET}{C_DIM}系統音訊 {lb_lang} | 麥克風 {mic_lang}{RESET}")
    print(f"  {C_DIM}記錄檔      {os.path.relpath(session_dir)}/{RESET}")

    # 取得音訊時長（取較長的那個作為參考）
    audio_duration = 0
    for _wp in (lb_wav, mic_wav):
        probe = _ffprobe_info(_wp)
        if probe and probe[0] > audio_duration:
            audio_duration = probe[0]

    # ── ASR：兩路分別辨識 ──
    def _do_asr(wav_path, lang, label):
        """對單一音訊執行 ASR，回傳 raw_segments list"""
        used_remote = False
        raw_segs = None
        _rw_cfg = remote_whisper_cfg

        if _rw_cfg is not None:
            rw_host = _rw_cfg.get("host", "?")
            rw_port = _rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
            print(f"  {C_WHITE}{label} 上傳辨識中...{RESET}")

            sbar = _SummaryStatusBar(model=model_size, task=f"{label} 上傳", asr_location="伺服器").start()

            def _up(text):
                sbar.set_progress(text)

            def _done():
                sbar.set_task(f"{label} GPU 辨識中", reset_timer=False)
                sbar.set_progress("等待伺服器回應...")

            try:
                r_segments, r_duration, r_proc_time, r_device = _remote_whisper_transcribe(
                    _rw_cfg, wav_path, model_size, lang,
                    progress_callback=_up, on_upload_done=_done)
                raw_segs = r_segments
                used_remote = True
                sbar.set_task(f"{label} 辨識完成（{len(r_segments)} 段，{r_proc_time:.1f}s）", reset_timer=False)
                sbar.freeze()
                sbar.stop()
            except Exception as e:
                sbar.set_task(f"{label} 伺服器失敗", reset_timer=False)
                sbar.freeze()
                sbar.stop()
                print(f"  {C_HIGHLIGHT}[降級] {label} 改用本機辨識: {e}{RESET}")
                _rw_cfg = None

        if not used_remote:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                print(f"  {C_HIGHLIGHT}[錯誤] faster-whisper 未安裝{RESET}", file=sys.stderr)
                return []

            print(f"  {C_WHITE}{label} 載入模型 {model_size}...{RESET}", end=" ", flush=True)
            model = _call_with_ssl_retry(WhisperModel, model_size, device="auto", compute_type="int8")
            print(f"{C_OK}✓{RESET}")

            sbar = _SummaryStatusBar(model=model_size, task=f"{label} 辨識中", asr_location="本機").start()

            _dur = 0
            _probe = _ffprobe_info(wav_path)
            if _probe and _probe[0] > 0:
                _dur = _probe[0]

            segments_iter, info = model.transcribe(wav_path, language=lang, beam_size=5, vad_filter=True)
            raw_segs = []
            for segment in segments_iter:
                if _dur > 0:
                    pct = min(segment.end / _dur, 1.0)
                    sbar.set_progress(f"{pct:.0%}")
                text = segment.text.strip()
                if text:
                    raw_segs.append({"start": segment.start, "end": segment.end, "text": text})
            sbar.set_task(f"{label} 辨識完成（{len(raw_segs)} 段）", reset_timer=False)
            sbar.freeze()
            sbar.stop()

        return raw_segs or []

    # 在 ASR 期間背景預熱 LLM（避免 ASR 後模型已卸載導致翻譯超時）
    _warmup_thread = None
    _warmup_ok = [False]
    for _tr in (translator_lb, translator_mic):
        if _tr and hasattr(_tr, "warmup"):
            import threading
            def _bg_warmup(tr=_tr, result=_warmup_ok):
                result[0] = tr.warmup()
            _warmup_thread = threading.Thread(target=_bg_warmup, daemon=True)
            _warmup_thread.start()
            break

    t_stage = time.monotonic()
    lb_raw = _do_asr(lb_wav, lb_lang, "系統音訊")
    mic_raw = _do_asr(mic_wav, mic_lang, "麥克風")
    t_asr = time.monotonic() - t_stage

    # ── 過濾幻覺 + 簡轉繁 ──
    def _filter_segments(raw_segs, lang, hall_check):
        valid = []
        for seg in raw_segs:
            text = seg["text"].strip()
            if not text:
                continue
            text = re.sub(r"\(.*?\)", "", text).strip()
            text = re.sub(r"\[.*?\]", "", text).strip()
            if not text:
                continue
            if hall_check(text):
                continue
            if lang == "zh":
                text = S2TWP.convert(text)
            valid.append({"start": seg["start"], "end": seg["end"], "text": text})
        return valid

    lb_segs = _filter_segments(lb_raw, lb_lang, lb_hall)
    mic_segs = _filter_segments(mic_raw, mic_lang, mic_hall)

    print(f"\n  {C_OK}辨識完成    {RESET}{C_DIM}系統音訊 {len(lb_segs)} 段 + 麥克風 {len(mic_segs)} 段  [{t_asr:.1f}s]{RESET}")

    # ── 講者辨識（兩路各自獨立）──
    lb_speaker_labels = None
    mic_speaker_labels = None
    if diarize and (lb_segs or mic_segs):
        t_diarize_start = time.monotonic()
        d_sbar = _SummaryStatusBar(model=model_size, task="講者辨識", asr_location="").start()
        if lb_segs:
            if remote_whisper_cfg is not None:
                d_sbar.set_task("伺服器講者辨識：系統音訊（上傳中）", reset_timer=False)
                def _d_prog_lb(msg):
                    d_sbar.set_progress(msg)
                def _d_upload_lb():
                    d_sbar.set_task("伺服器講者辨識：系統音訊（GPU 分析中）", reset_timer=False)
                    d_sbar.set_progress("等待伺服器回應...")
                lb_speaker_labels, _ = _remote_diarize(
                    remote_whisper_cfg, lb_wav, lb_segs,
                    num_speakers=num_speakers,
                    progress_callback=_d_prog_lb,
                    on_upload_done=_d_upload_lb,
                )
                if lb_speaker_labels is None:
                    d_sbar.set_task("伺服器失敗，改用本機講者辨識：系統音訊", reset_timer=False)
                    lb_speaker_labels = _diarize_segments(lb_wav, lb_segs,
                                                          num_speakers=num_speakers, sbar=d_sbar)
            else:
                d_sbar.set_task("講者辨識：系統音訊", reset_timer=False)
                lb_speaker_labels = _diarize_segments(lb_wav, lb_segs,
                                                      num_speakers=num_speakers, sbar=d_sbar)
        if mic_segs:
            if remote_whisper_cfg is not None:
                d_sbar.set_task("伺服器講者辨識：麥克風（上傳中）", reset_timer=False)
                def _d_prog_mic(msg):
                    d_sbar.set_progress(msg)
                def _d_upload_mic():
                    d_sbar.set_task("伺服器講者辨識：麥克風（GPU 分析中）", reset_timer=False)
                    d_sbar.set_progress("等待伺服器回應...")
                mic_speaker_labels, _ = _remote_diarize(
                    remote_whisper_cfg, mic_wav, mic_segs,
                    num_speakers=num_speakers,
                    progress_callback=_d_prog_mic,
                    on_upload_done=_d_upload_mic,
                )
                if mic_speaker_labels is None:
                    d_sbar.set_task("伺服器失敗，改用本機講者辨識：麥克風", reset_timer=False)
                    mic_speaker_labels = _diarize_segments(mic_wav, mic_segs,
                                                           num_speakers=num_speakers, sbar=d_sbar)
            else:
                d_sbar.set_task("講者辨識：麥克風", reset_timer=False)
                mic_speaker_labels = _diarize_segments(mic_wav, mic_segs,
                                                       num_speakers=num_speakers, sbar=d_sbar)
        t_diarize_elapsed = time.monotonic() - t_diarize_start
        d_sbar.set_task(f"講者辨識完成（{t_diarize_elapsed:.1f}s）", reset_timer=False)
        d_sbar.freeze()
        d_sbar.stop()

    # ── 合併兩組 segments ──
    merged = []
    for i, seg in enumerate(lb_segs):
        spk = (lb_speaker_labels[i] + 1) if lb_speaker_labels else None
        merged.append({**seg, "source": "loopback", "speaker": spk})
    lb_max = (max(lb_speaker_labels) + 1) if lb_speaker_labels else 0
    for i, seg in enumerate(mic_segs):
        spk = (mic_speaker_labels[i] + 1 + lb_max) if mic_speaker_labels else None
        merged.append({**seg, "source": "mic", "speaker": spk})
    merged.sort(key=lambda s: s["start"])

    if not merged:
        # 清理暫存
        if lb_tmp and os.path.exists(lb_wav):
            os.remove(lb_wav)
        if mic_tmp and os.path.exists(mic_wav):
            os.remove(mic_wav)
        print(f"\n  {C_HIGHLIGHT}[警告] 兩路音訊皆無有效內容{RESET}")
        return None, None, None

    # ── 標籤 ──
    bidi_labels = _BIDI_LABELS.get(mode)
    if bidi_labels:
        lb_src_color, lb_src_label, lb_dst_color, lb_dst_label = bidi_labels["loopback"]
        mic_src_color, mic_src_label, mic_dst_color, mic_dst_label = bidi_labels["mic"]
    else:
        # fallback
        sc, sl, dc, dl = _MODE_LABELS.get(mode, (C_EN, "EN", C_ZH, "中"))
        lb_src_color, lb_src_label, lb_dst_color, lb_dst_label = sc, sl, dc, dl
        mic_src_color, mic_src_label, mic_dst_color, mic_dst_label = dc, dl, sc, sl

    # ── 翻譯 + 輸出 ──
    # 等待背景預熱完成（ASR 期間已開始，通常此時早已 ready）
    if _warmup_thread is not None:
        _warmup_thread.join(timeout=120)
        if not _warmup_ok[0]:
            # 背景預熱未成功，同步重試一次
            for _tr in (translator_lb, translator_mic):
                if _tr and hasattr(_tr, "warmup"):
                    print(f"  {C_DIM}預熱翻譯引擎...{RESET}", end="", flush=True)
                    if _tr.warmup():
                        print(f" {C_OK}ready{RESET}")
                    else:
                        print(f" {C_HIGHLIGHT}逾時（翻譯可能不完整）{RESET}")
                    break

    t_stage = time.monotonic()
    seg_count = 0
    segments_data = []

    sbar = _SummaryStatusBar(model=model_size, task="翻譯中", asr_location="").start()

    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            for seg in merged:
                seg_count += 1
                text = seg["text"]
                source = seg["source"]
                ts_start = _format_timestamp(seg["start"])
                ts_end = _format_timestamp(seg["end"])
                ts_tag = f"[{ts_start}-{ts_end}]"
                direction_mark = "◀" if source == "loopback" else "▶"

                sbar.set_task(f"輸出中（{seg_count}/{len(merged)}）", reset_timer=False)

                if source == "loopback":
                    src_color, src_label = lb_src_color, lb_src_label
                    dst_color, dst_label = lb_dst_color, lb_dst_label
                    translator = translator_lb
                else:
                    src_color, src_label = mic_src_color, mic_src_label
                    dst_color, dst_label = mic_dst_color, mic_dst_label
                    translator = translator_mic

                seg_lines = []

                # 講者標籤
                spk_tag_term = ""  # 終端機用（帶色彩）
                spk_tag_log = ""   # log 用（純文字）
                spk_num_val = seg.get("speaker")
                if spk_num_val is not None:
                    spk_color = SPEAKER_COLORS[(spk_num_val - 1) % len(SPEAKER_COLORS)]
                    spk_tag_term = f"{spk_color}[Speaker {spk_num_val}]{RESET} "
                    spk_tag_log = f"[Speaker {spk_num_val}] "

                if translator:
                    print(f"{src_color}{ts_tag} {direction_mark} {spk_tag_term}[{src_label}] {text}{RESET}", flush=True)

                    t0 = time.monotonic()
                    result = translator.translate(text)
                    elapsed = time.monotonic() - t0

                    if result:
                        print(f"{dst_color}{BOLD}{ts_tag} {direction_mark} {spk_tag_term}[{dst_label}] {result}{RESET}", flush=True)
                        print(flush=True)

                        log_f.write(f"{ts_tag} {direction_mark} {spk_tag_log}[{src_label}] {text}\n")
                        log_f.write(f"{ts_tag} {direction_mark} {spk_tag_log}[{dst_label}] {result}\n\n")
                        seg_lines.append({"label": f"{src_label}", "text": text})
                        seg_lines.append({"label": f"{dst_label}", "text": result})
                    else:
                        print(flush=True)
                        log_f.write(f"{ts_tag} {direction_mark} {spk_tag_log}[{src_label}] {text}\n\n")
                        seg_lines.append({"label": f"{src_label}", "text": text})
                else:
                    print(f"{src_color}{BOLD}{ts_tag} {direction_mark} {spk_tag_term}[{src_label}] {text}{RESET}", flush=True)
                    print(flush=True)
                    log_f.write(f"{ts_tag} {direction_mark} {spk_tag_log}[{src_label}] {text}\n\n")
                    seg_lines.append({"label": f"{src_label}", "text": text})

                segments_data.append({
                    "start": seg["start"], "end": seg["end"],
                    "speaker": spk_num_val,
                    "source": source,
                    "lines": seg_lines,
                })

        t_translate = time.monotonic() - t_stage
        sbar.set_task(f"輸出完成（{seg_count} 段，{t_translate:.1f}s）", reset_timer=False)
        sbar.freeze()

        # LLM 文字校正
        if correct_with_llm and segments_data and llm_model:
            sbar.stop()
            print(f"\n  {C_WHITE}LLM 校正逐字稿文字...{RESET}")
            try:
                _correct_segments_with_llm(segments_data, llm_model, llm_host, llm_port,
                                           server_type=llm_server_type, topic=meeting_topic)
                with open(log_path, "w", encoding="utf-8") as log_f:
                    for seg_d in segments_data:
                        ts_start = _format_timestamp(seg_d["start"])
                        ts_end = _format_timestamp(seg_d["end"])
                        ts_tag = f"[{ts_start}-{ts_end}]"
                        direction_mark = "◀" if seg_d.get("source") == "loopback" else "▶"
                        spk_tag = f"[Speaker {seg_d['speaker']}] " if seg_d.get("speaker") else ""
                        for line in seg_d["lines"]:
                            log_f.write(f"{ts_tag} {direction_mark} {spk_tag}[{line['label']}] {line['text']}\n")
                        log_f.write("\n")
            except Exception as e:
                print(f"  {C_HIGHLIGHT}[警告] LLM 校正失敗: {e}{RESET}", file=sys.stderr)

        # 清理暫存
        if lb_tmp and os.path.exists(lb_wav):
            os.remove(lb_wav)
        if mic_tmp and os.path.exists(mic_wav):
            os.remove(mic_wav)

        t_total = time.monotonic() - t_total_start
        t_min, t_sec = divmod(int(t_total), 60)
        total_str = f"{t_min}m{t_sec:02d}s" if t_min else f"{t_total:.1f}s"

        # HTML
        transcript_html_path = os.path.splitext(log_path)[0] + ".html"
        _meta = {
            "asr_engine": "faster-whisper",
            "asr_model": model_size,
            "asr_location": "GPU 伺服器" if remote_whisper_cfg else "本機",
            "input_file": f"{os.path.basename(lb_path)} + {os.path.basename(mic_path)}",
            "bidi_mode": mode,
            "meeting_topic": meeting_topic,
        }
        if translator_lb:
            if isinstance(translator_lb, NllbTranslator):
                _meta["translate_engine"] = "NLLB 600M"
                _meta["translate_location"] = "本機離線"
            elif isinstance(translator_lb, ArgosTranslator):
                _meta["translate_engine"] = "Argos"
                _meta["translate_location"] = "本機離線"
            elif hasattr(translator_lb, "model"):
                _srv_type = getattr(translator_lb, "server_type", "")
                _srv_label = "Ollama" if _srv_type == "ollama" else "OpenAI 相容" if _srv_type == "openai" else ""
                _meta["translate_engine"] = getattr(translator_lb, "model", "LLM")
                _loc = f"{getattr(translator_lb, 'host', '')}:{getattr(translator_lb, 'port', '')}"
                if _srv_label:
                    _loc += f" ({_srv_label})"
                _meta["translate_location"] = _loc

        if diarize:
            _meta["diarize"] = True
            _meta["diarize_engine"] = "resemblyzer + spectralcluster"
            if remote_whisper_cfg is not None:
                _meta["diarize_location"] = "GPU 伺服器"
            else:
                _meta["diarize_location"] = "本機"
            if num_speakers:
                _meta["num_speakers"] = num_speakers
            if segments_data:
                _detected = len(set(s.get("speaker") for s in segments_data if s.get("speaker") is not None))
                if _detected >= 2:
                    _meta["detected_speakers"] = _detected

        # SRT
        _srt = None
        if segments_data:
            srt_path = os.path.splitext(log_path)[0] + ".srt"
            _segments_to_srt(segments_data, srt_path)
            _srt = srt_path

        # 用系統音訊的副本作為 HTML 主音訊
        audio_copy = os.path.join(session_dir, os.path.basename(lb_path))
        _html = None
        if segments_data:
            _transcript_to_html(segments_data, transcript_html_path,
                                audio_copy, audio_duration, metadata=_meta)
            _html = transcript_html_path

        print(f"\n{C_DIM}{'═' * 60}{RESET}")
        print(f"  {C_OK}{BOLD}雙向處理完成{RESET} {C_DIM}（共 {seg_count} 段 | 耗時 {total_str}）{RESET}")
        print(f"  {C_WHITE}{log_path}{RESET}")
        if _html:
            print(f"  {C_WHITE}{_html}{RESET}")
        if _srt:
            print(f"  {C_WHITE}{_srt}{RESET}")
        print(f"{C_DIM}{'═' * 60}{RESET}")

        sbar.stop()
        return log_path, _html, session_dir

    except KeyboardInterrupt:
        sbar.stop()
        if lb_tmp and os.path.exists(lb_wav):
            os.remove(lb_wav)
        if mic_tmp and os.path.exists(mic_wav):
            os.remove(mic_wav)
        print(f"\n\n{C_DIM}已中止處理。{RESET}")
        if seg_count > 0:
            print(f"  {C_DIM}已處理的 {seg_count} 段已儲存: {log_path}{RESET}")
        raise
    except Exception as e:
        sbar.stop()
        if lb_tmp and os.path.exists(lb_wav):
            os.remove(lb_wav)
        if mic_tmp and os.path.exists(mic_wav):
            os.remove(mic_wav)
        print(f"\n  {C_HIGHLIGHT}[錯誤] 雙向處理失敗: {e}{RESET}", file=sys.stderr)
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

    # 內容主題
    topic = metadata.get("meeting_topic")
    if topic:
        lines.append(f"內容主題：{topic}")

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
                       audio_path="", summary_rounds=1):
    """讀取記錄檔 → 建 prompt → 呼叫 LLM → 簡繁轉換 → 寫摘要檔
    summary_mode: "both"（摘要+逐字稿）、"summary"（只摘要）、"transcript"（只逐字稿）
    summary_rounds: 處理次數（1-3），多次處理後整合可提升品質
    回傳 (output_path, summary_text, html_path)"""
    with open(input_path, "r", encoding="utf-8") as f:
        transcript = f.read().strip()

    if not transcript:
        print(f"  {C_HIGHLIGHT}[跳過] 檔案內容為空: {input_path}{RESET}")
        print(f"  {C_DIM}逐字稿為空表示辨識無結果，可能是功能模式與音訊語言不符{RESET}")
        _webui_send({"type": "progress", "stage": "跳過摘要", "detail": "逐字稿為空，請確認功能模式是否正確"})
        return None, None, None

    basename = os.path.basename(input_path)
    dirpath = os.path.dirname(input_path) or "."

    # 依原始檔名決定摘要檔名（時間逐字稿優先匹配，再匹配舊版逐字稿）
    if basename.startswith("英中雙向_時間逐字稿"):
        out_name = basename.replace("英中雙向_時間逐字稿", "英中雙向_摘要", 1)
    elif basename.startswith("英翻中_雙向時間逐字稿"):
        out_name = basename.replace("英翻中_雙向時間逐字稿", "英翻中_雙向摘要", 1)
    elif basename.startswith("中翻英_雙向時間逐字稿"):
        out_name = basename.replace("中翻英_雙向時間逐字稿", "中翻英_雙向摘要", 1)
    elif basename.startswith("日翻中_雙向時間逐字稿"):
        out_name = basename.replace("日翻中_雙向時間逐字稿", "日翻中_雙向摘要", 1)
    elif basename.startswith("中翻日_雙向時間逐字稿"):
        out_name = basename.replace("中翻日_雙向時間逐字稿", "中翻日_雙向摘要", 1)
    elif basename.startswith("英文_雙向時間逐字稿"):
        out_name = basename.replace("英文_雙向時間逐字稿", "英文_雙向摘要", 1)
    elif basename.startswith("中文_雙向時間逐字稿"):
        out_name = basename.replace("中文_雙向時間逐字稿", "中文_雙向摘要", 1)
    elif basename.startswith("日文_雙向時間逐字稿"):
        out_name = basename.replace("日文_雙向時間逐字稿", "日文_雙向摘要", 1)
    elif basename.startswith("日中雙向_時間逐字稿"):
        out_name = basename.replace("日中雙向_時間逐字稿", "日中雙向_摘要", 1)
    elif basename.startswith("英翻中_配對時間逐字稿"):
        out_name = basename.replace("英翻中_配對時間逐字稿", "英翻中_配對摘要", 1)
    elif basename.startswith("中翻英_配對時間逐字稿"):
        out_name = basename.replace("中翻英_配對時間逐字稿", "中翻英_配對摘要", 1)
    elif basename.startswith("日翻中_配對時間逐字稿"):
        out_name = basename.replace("日翻中_配對時間逐字稿", "日翻中_配對摘要", 1)
    elif basename.startswith("中翻日_配對時間逐字稿"):
        out_name = basename.replace("中翻日_配對時間逐字稿", "中翻日_配對摘要", 1)
    elif basename.startswith("英文_配對時間逐字稿"):
        out_name = basename.replace("英文_配對時間逐字稿", "英文_配對摘要", 1)
    elif basename.startswith("中文_配對時間逐字稿"):
        out_name = basename.replace("中文_配對時間逐字稿", "中文_配對摘要", 1)
    elif basename.startswith("日文_配對時間逐字稿"):
        out_name = basename.replace("日文_配對時間逐字稿", "日文_配對摘要", 1)
    elif basename.startswith("配對_時間逐字稿"):
        out_name = basename.replace("配對_時間逐字稿", "配對_摘要", 1)
    elif basename.startswith("英翻中_時間逐字稿"):
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
    _webui_send({"type": "progress", "stage": f"生成摘要（{model}）", "detail": "準備中..."})

    if len(chunks) <= 1:
        # 單段：直接摘要
        prompt = _summary_prompt(transcript, topic=topic, summary_mode=summary_mode)
        sbar.set_task(f"生成摘要（單段，{len(transcript)} 字）")
        _webui_send({"type": "progress", "stage": f"生成摘要（{model}）",
                     "detail": f"單段，{len(transcript)} 字"})
        summary = call_ollama_raw(prompt, model, host, port, spinner=sbar, live_output=True,
                                  server_type=server_type)
    else:
        # 多段：逐段摘要 + 合併
        segment_summaries = []
        for i, chunk in enumerate(chunks):
            sbar.set_task(f"第 {i+1}/{len(chunks)} 段（{len(chunk)} 字）")
            _webui_send({"type": "progress", "stage": f"生成摘要（{model}）",
                         "detail": f"第 {i+1}/{len(chunks)} 段，{len(chunk)} 字"})
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
            _webui_send({"type": "progress", "stage": f"生成摘要（{model}）",
                         "detail": f"合併 {len(chunks)} 段"})
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

    # 偵測 LLM 是否跳過校正逐字稿（summary_mode="both" 時應有）
    if summary_mode == "both" and "## 校正逐字稿" not in summary:
        print(f"\n  {C_HIGHLIGHT}[偵測] LLM 回覆缺少校正逐字稿，自動補發校正請求...{RESET}")
        _tc_input = transcript[:max_chars] if len(transcript) > max_chars else transcript
        _tc_topic = f"（主題：{topic}）" if topic else ""
        _tc_prompt = f"""\
你是專業的會議記錄整理員。請將以下語音辨識的逐字稿整理成流暢、易讀的段落文字{_tc_topic}。
合併斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。
必須完整輸出所有內容，嚴禁以「以下略」「篇幅限制」等理由截斷或跳過任何段落。
全部使用台灣繁體中文。

輸出格式：

## 校正逐字稿

（整理後的完整文字）

以下是逐字稿：
---
{_tc_input}
---"""
        sbar_tc = _SummaryStatusBar(model=model, task="補產校正逐字稿", location=_llm_loc).start()
        _tc_result = call_ollama_raw(_tc_prompt, model, host, port, spinner=sbar_tc,
                                      live_output=True, server_type=server_type)
        sbar_tc.stop()
        _tc_result = S2TWP.convert(_tc_result)
        summary = summary.rstrip() + "\n\n" + _tc_result.lstrip()
        print(f"  {C_OK}校正逐字稿已補上{RESET}")

    # 多次處理：重新產生摘要並整合（提升品質）
    if summary_rounds > 1:
        round_results = [summary]
        for ri in range(2, min(summary_rounds, 3) + 1):
            print(f"\n  {C_WHITE}第 {ri}/{summary_rounds} 次摘要處理...{RESET}")
            _webui_send({"type": "progress", "stage": f"生成摘要（{model}）",
                         "detail": f"第 {ri}/{summary_rounds} 次處理"})
            sbar_r = _SummaryStatusBar(model=model, task=f"第 {ri} 次摘要", location=_llm_loc).start()
            if len(chunks) <= 1:
                _r_prompt = _summary_prompt(transcript, topic=topic, summary_mode=summary_mode)
                _r_result = call_ollama_raw(_r_prompt, model, host, port, spinner=sbar_r,
                                            live_output=False, server_type=server_type)
            else:
                _r_segs = []
                for ci, chunk in enumerate(chunks):
                    sbar_r.set_task(f"第 {ri} 次 - 段 {ci+1}/{len(chunks)}")
                    _r_prompt = _summary_prompt(chunk, topic=topic, summary_mode=summary_mode)
                    _r_seg = call_ollama_raw(_r_prompt, model, host, port, spinner=sbar_r,
                                             live_output=False, server_type=server_type)
                    _r_segs.append(S2TWP.convert(_r_seg))
                sbar_r.set_task(f"第 {ri} 次 - 合併")
                _r_combined = "\n\n---\n\n".join(
                    f"### 第 {i+1} 段\n{s}" for i, s in enumerate(_r_segs))
                _r_merge_prompt = SUMMARY_MERGE_PROMPT_TEMPLATE.format(summaries=_r_combined)
                _r_result = call_ollama_raw(_r_merge_prompt, model, host, port, spinner=sbar_r,
                                            live_output=False, server_type=server_type)
            sbar_r.freeze()
            sbar_r.stop()
            round_results.append(S2TWP.convert(_r_result))
        # 整合多次結果
        print(f"\n  {C_WHITE}整合 {len(round_results)} 次摘要結果...{RESET}")
        _webui_send({"type": "progress", "stage": f"生成摘要（{model}）",
                     "detail": f"整合 {len(round_results)} 次結果"})
        sbar_m = _SummaryStatusBar(model=model, task="整合摘要", location=_llm_loc).start()
        # 整合時考慮 context window：每次結果截斷到 max_chars / 次數，確保總量不超限
        _per_round_limit = max(max_chars // len(round_results), 2000)
        _truncated_results = []
        for i, r in enumerate(round_results):
            if len(r) > _per_round_limit:
                r = r[:_per_round_limit] + f"\n\n（第 {i+1} 次摘要過長，已截斷至 {_per_round_limit} 字）"
            _truncated_results.append(r)
        _merge_input = "\n\n" + "=" * 40 + "\n\n".join(
            f"【第 {i+1} 次摘要】\n{r}" for i, r in enumerate(_truncated_results))
        _merge_prompt = (
            "以下是同一份會議逐字稿經過多次 AI 摘要處理的結果。"
            "請整合這些摘要，取各版本的最佳內容，產出一份完整、準確、不遺漏的最終摘要。"
            "如果某個版本有提到其他版本遺漏的重點，請納入。"
            "如果有校正逐字稿，以最完整的版本為準。"
            "全部使用台灣繁體中文。\n\n" + _merge_input
        )
        summary = call_ollama_raw(_merge_prompt, model, host, port, spinner=sbar_m,
                                   live_output=True, server_type=server_type)
        sbar_m.freeze()
        sbar_m.stop()

    # 標題格式修正（在所有補發和整合之後）：LLM 有時輸出 ### 或其他標題格式，統一修正
    import re as _re_fmt
    summary = _re_fmt.sub(r'#{2,4}\s*(?:最終)?(?:重點)?摘要', '## 重點摘要', summary)
    summary = _re_fmt.sub(r'#{2,4}\s*(?:校正)?逐字稿', '## 校正逐字稿', summary)

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
            color = _SPEAKER_HTML_COLORS[((current_speaker or 1) - 1) % len(_SPEAKER_HTML_COLORS)]
            body_parts.append(f'<p class="speaker" style="color:{color}">{escaped}</p>')
        else:
            if current_speaker is not None:
                # 同一講者的延續段落，自動補上 Speaker 標籤
                color = _SPEAKER_HTML_COLORS[(current_speaker - 1) % len(_SPEAKER_HTML_COLORS)]
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
        t_engine = metadata.get("translate_engine")
        if t_model:
            t_server = metadata.get("translate_server", "")
            meta_lines.append(f'翻譯引擎：{t_model}' + (f" ({t_server})" if t_server else ""))
        elif t_engine:
            t_loc = metadata.get("translate_location", "")
            meta_lines.append(f'翻譯引擎：{t_engine}' + (f"，{t_loc}" if t_loc else ""))
        s_model = metadata.get("summary_model")
        if s_model:
            s_server = metadata.get("summary_server", "")
            meta_lines.append(f'內容摘要：{s_model}' + (f" ({s_server})" if s_server else ""))
        topic = metadata.get("meeting_topic")
        if topic:
            meta_lines.append(f'內容主題：{topic}')
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
            proc = subprocess.run(cmd, capture_output=True, timeout=30, **_SUBPROCESS_FLAGS)
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

        source = seg.get("source")
        seg_cls = "seg mic" if source == "mic" else "seg"
        if source:
            seg_parts.append(
                f'<div class="{seg_cls}" id="t-{start_sec}">\n'
                f'  <div class="bubble">\n'
                f'    <a class="ts" data-t="{seg["start"]}" href="#">{ts_text}</a>\n'
                f'    {"".join(lines_html)}\n'
                f'  </div>\n'
                f'</div>'
            )
        else:
            seg_parts.append(
                f'<div class="{seg_cls}" id="t-{start_sec}">\n'
                f'  <a class="ts" data-t="{seg["start"]}" href="#">{ts_text}</a>\n'
                f'  {"".join(lines_html)}\n'
                f'</div>'
            )
    body_html = "\n".join(seg_parts)

    # metadata 區塊
    _input_file = metadata.get("input_file") if metadata else None
    title = _input_file or os.path.basename(audio_path)
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
        trans_engine = metadata.get("translate_engine")
        if trans_engine:
            trans_loc = metadata.get("translate_location", "")
            trans_model = metadata.get("translate_model", "")
            if trans_model and trans_model != trans_engine:
                trans_str = f"{trans_model}"
            else:
                trans_str = trans_engine
            if trans_loc:
                trans_str += f"，{trans_loc}"
            meta_lines.append(f'翻譯引擎：{trans_str}')
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
        correct_engine = metadata.get("correct_engine")
        if correct_engine:
            correct_loc = metadata.get("correct_location", "本機")
            meta_lines.append(f'文字校正：{correct_engine}，{correct_loc}')
        topic = metadata.get("meeting_topic")
        if topic:
            meta_lines.append(f'內容主題：{topic}')

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
  /* -- chat bubble (bidirectional mode) -- */
  .bubble {{ display: inline-block; max-width: 85%; padding: 10px 14px; border-radius: 12px;
             text-align: left; }}
  .bubble .ts {{ margin-bottom: 6px; }}
  .bubble .line {{ margin-left: 0.2em; }}
  .bubble .line-dst {{ margin-left: 1em; }}
  .seg:has(.bubble) {{ border-bottom: none; padding: 4px 0; display: flex; }}
  .seg:has(.bubble) .bubble {{ background: #232740; border-bottom-left-radius: 4px; }}
  .seg:has(.bubble).active .bubble {{ background: #1e2a4a; }}
  .seg:has(.bubble).playing {{ background: transparent; border-left: none; padding-left: 0;
                               box-shadow: none; outline: none; border-radius: 0; }}
  .seg:has(.bubble).playing .bubble {{ background: #1a2844;
                                       box-shadow: 0 0 12px rgba(232,224,96,0.3);
                                       outline: 1.5px solid rgba(232,224,96,0.4); }}
  .seg.mic {{ text-align: right; justify-content: flex-end; }}
  .seg.mic .bubble {{ background: #1a2a3e; border-bottom-right-radius: 4px; border-bottom-left-radius: 12px; }}
  .seg.mic .bubble .line {{ color: #7ec8e3; }}
  .seg.mic.active .bubble {{ background: #1a3050; }}
  .seg.mic.playing .bubble {{ background: #162840; }}
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
    if IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(h, ctypes.byref(mode))
            _original_termios = mode.value  # 暫存原始值
            # 停用 ENABLE_LINE_INPUT(0x0002) + ENABLE_ECHO_INPUT(0x0004)
            kernel32.SetConsoleMode(h, mode.value & ~0x0006)
            atexit.register(restore_terminal)
        except Exception:
            _original_termios = None
    else:
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
    """恢復原始 termios / console mode 設定"""
    global _original_termios
    if _original_termios is not None:
        try:
            if IS_WINDOWS:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                h = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
                kernel32.SetConsoleMode(h, _original_termios)
            else:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _original_termios)
        except Exception:
            pass
        _original_termios = None


def keypress_listener_thread(stop_event, ctrl_s_event=None, pause_event=None):
    """Daemon thread：持續偵測 Ctrl+S / Ctrl+P"""
    if IS_WINDOWS:
        while not stop_event.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    # 方向鍵/功能鍵開頭：吃掉第二個 scan code，避免誤判為 Ctrl 按鍵
                    if ch in (b'\x00', b'\xe0'):
                        if msvcrt.kbhit():
                            msvcrt.getch()
                        continue
                    if ch == b'\x10' and pause_event is not None:  # Ctrl+P
                        if pause_event.is_set():
                            pause_event.clear()
                            _status_bar_state["paused"] = False
                        else:
                            pause_event.set()
                            _status_bar_state["paused"] = True
                    if ch == b'\x13' and ctrl_s_event is not None:  # Ctrl+S
                        ctrl_s_event.set()
                else:
                    time.sleep(0.1)
            except Exception:
                return
    else:
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
# Windows 標題列用 Braille 點字（▁▂▃ 等下方塊在系統 UI 字型底部不對齊）
# 由下往上逐排填充：⠀ ⡀ ⣀ ⣄ ⣤ ⣦ ⣶ ⣿
_BARS_TITLE = "⠀⡀⣀⣄⣤⣦⣶⣿"


def _rms_to_bar(rms, title_mode=False):
    """RMS → 波形字元（對數刻度，增強微弱聲音的可見度）"""
    bars = _BARS_TITLE if title_mode else _BARS
    if rms < 0.0005:
        return bars[0]
    db = 20 * math.log10(max(rms, 1e-10))
    idx = int((db + 60) / 54 * (len(bars) - 1))
    return bars[max(0, min(idx, len(bars) - 1))]


# ─── 底部狀態列（固定顯示快捷鍵提示 + 即時資訊）────────────────
_status_bar_active = False
_status_bar_needs_resize = False
_status_bar_title_mode = False  # conhost fallback: 用視窗標題顯示狀態
_status_bar_state = {
    "start_time": 0.0,   # monotonic 起始時間
    "count": 0,          # 翻譯/轉錄筆數
    "mode": "en2zh",     # 功能模式
    "model_name": "",    # 模型名稱（如 large-v3-turbo）
    "asr_location": "",  # ASR 位置（"本機" / "伺服器"）
    "translate_model": "",  # 翻譯模型名稱（如 qwen2.5:14b）
    "translate_location": "",  # 翻譯位置（"本機" / "伺服器"）
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
    # Windows conhost.exe 不支援 ANSI scroll region，改用視窗標題顯示狀態
    global _status_bar_title_mode
    if IS_WINDOWS and not os.environ.get("WT_SESSION"):
        _status_bar_title_mode = True
        _status_bar_active = True
        _refresh_title_bar()
        return
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


_webui_rms_last = [0.0]  # 上次送 RMS 的時間

def _push_rms(rms):
    """Thread-safe 寫入一筆 RMS 值到狀態列波形歷史"""
    lock = _status_bar_state.get("rms_lock")
    hist = _status_bar_state.get("rms_history")
    if lock and hist is not None:
        with lock:
            hist.append(rms)
    # WebUI RMS（每 0.5 秒最多送一次，避免洪水）
    now = time.monotonic()
    if _webui_queue is not None and now - _webui_rms_last[0] > 0.2:
        _webui_rms_last[0] = now
        _webui_send({"type": "rms", "value": round(rms, 4)})


def _refresh_title_bar():
    """conhost fallback：用視窗標題顯示狀態資訊（含音量波形）"""
    try:
        elapsed = time.monotonic() - _status_bar_state["start_time"]
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}"
        count = _status_bar_state["count"]
        label = "轉錄" if _status_bar_state["mode"] in ("zh", "en", "ja") else "翻譯"
        parts = [time_str]
        # 波形圖
        hist = _status_bar_state.get("rms_history")
        lock = _status_bar_state.get("rms_lock")
        if hist is not None and lock:
            with lock:
                bars = [_rms_to_bar(v, title_mode=True) for v in hist]
            if bars:
                parts.append("".join(bars))
        m_loc = _status_bar_state.get("asr_location", "")
        if m_loc:
            parts.append(f"辨識[{m_loc}]")
        t_loc = _status_bar_state.get("translate_location", "")
        if t_loc:
            parts.append(f"翻譯[{t_loc}]")
        parts.append(f"{label} {count} 筆")
        if _status_bar_state.get("paused"):
            parts.append("已暫停")
        title = " | ".join(parts)
        sys.stdout.write(f"\x1b]0;{title}\x07")
        sys.stdout.flush()
    except Exception:
        pass


def refresh_status_bar():
    """重繪底部狀態列（供外部在 print_lock 內呼叫）"""
    global _status_bar_needs_resize
    if not _status_bar_active:
        return
    if _status_bar_title_mode:
        _refresh_title_bar()
        return
    # Windows 無 SIGWINCH，改用 polling 偵測視窗大小變化
    if IS_WINDOWS:
        try:
            new_rows = os.get_terminal_size().lines
            if new_rows != _status_bar_state.get("_last_rows"):
                _status_bar_needs_resize = True
        except Exception:
            pass
    if _status_bar_needs_resize:
        _status_bar_needs_resize = False
        try:
            cols, rows = os.get_terminal_size()
            old_rows = _status_bar_state.get("_last_rows", 0)
            if old_rows and old_rows != rows:
                # 暫時解除 scroll region，以便清除所有可能的殘影
                sys.stdout.write("\x1b[r")
                # 清除舊/新狀態列位置之間所有列
                lo = min(old_rows, rows)
                hi = max(old_rows, rows)
                for r in range(max(1, lo - 2), hi + 1):
                    sys.stdout.write(f"\x1b[{r};1H\x1b[2K")
                # 額外清除新底部附近（終端 reflow 可能把舊狀態列推到這裡）
                for r in range(max(1, rows - 3), rows + 1):
                    sys.stdout.write(f"\x1b[{r};1H\x1b[2K")
            _status_bar_state["_last_rows"] = rows
            # 設定新 scroll region 並重繪
            sys.stdout.write(f"\x1b[1;{rows - 1}r")
            # 不用 save/restore cursor（resize 後位置可能失效），直接定位
            sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")
            sys.stdout.flush()
            _draw_status_bar(rows, cols)
            # 將游標移回 scroll region 底部
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
            # 若偵測到大小改變，設 flag 讓 refresh_status_bar 統一處理（含殘影清除）
            old_rows = _status_bar_state.get("_last_rows", 0)
            if old_rows and old_rows != rows:
                global _status_bar_needs_resize
                _status_bar_needs_resize = True
                return  # 不在這裡畫，交給 refresh_status_bar 統一處理 resize
        sys.stdout.write("\x1b7")  # 儲存游標位置
        sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")  # 移到最後一行並清除
        # 組合狀態文字
        elapsed = time.monotonic() - _status_bar_state["start_time"]
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}"
        count = _status_bar_state["count"]
        label = "轉錄" if _status_bar_state["mode"] in ("zh", "en", "ja") else "翻譯"
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
        # 組合狀態列片段（plain, display, priority）
        # priority 0 = 永遠保留，數字越小越先隱藏
        _sw = lambda t: sum(2 if '\u4e00' <= c <= '\u9fff' or '\uff01' <= c <= '\uff60' or '\u2e80' <= c <= '\u2fd5' else 1 for c in t)
        if _status_bar_state.get("paused"):
            pause_str = "\x1b[38;2;255;220;80m\u23f8 \u5df2\u66ab\u505c\x1b[38;2;200;200;200m"
            segs = [
                (f" {time_str} {wave_str}", f" {time_str} {wave_colored}", 0),
            ]
            if model_part:
                segs.append((f" | {model_part}", f" | {model_part_display}", 1))
            segs.append((f" | \u23f8 \u5df2\u66ab\u505c", f" | {pause_str}", 2))
            segs.append((f" | Ctrl+P \u7e7c\u7e8c", f" | Ctrl+P \u7e7c\u7e8c", 3))
            segs.append((f" | Ctrl+C \u505c\u6b62 ", f" | Ctrl+C \u505c\u6b62 ", 4))
        else:
            segs = [
                (f" {time_str} {wave_str}", f" {time_str} {wave_colored}", 0),
            ]
            if model_part:
                segs.append((f" | {model_part}", f" | {model_part_display}", 2))
            segs.append((f" | {label} {count} \u7b46", f" | {label} {count} \u7b46", 1))
            segs.append((f" | Ctrl+P \u66ab\u505c", f" | Ctrl+P \u66ab\u505c", 3))
            segs.append((f" | Ctrl+C \u505c\u6b62 ", f" | Ctrl+C \u505c\u6b62 ", 4))
        # 按 priority 由小到大移除片段直到總寬度 <= cols
        while len(segs) > 1:
            total_w = sum(_sw(p) for p, _, _ in segs)
            if total_w <= cols:
                break
            rm_idx = min(
                (i for i, (_, _, pri) in enumerate(segs) if pri > 0),
                key=lambda i: segs[i][2],
                default=-1,
            )
            if rm_idx < 0:
                break
            segs.pop(rm_idx)
        status = "".join(p for p, _, _ in segs)
        status_display = "".join(d for _, d, _ in segs)
        dw = _sw(status)
        padding = " " * max(0, cols - dw)
        sys.stdout.write(f"\x1b[48;2;60;60;60m\x1b[38;2;200;200;200m{status_display}{padding}\x1b[0m")
        sys.stdout.write("\x1b8")  # 恢復游標位置
        sys.stdout.flush()
    except Exception:
        pass


def clear_status_bar():
    """清除狀態列，恢復正常滾動區域"""
    global _status_bar_active, _status_bar_title_mode
    if not _status_bar_active:
        return
    _status_bar_active = False
    if _status_bar_title_mode:
        _status_bar_title_mode = False
        try:
            sys.stdout.write(f"\x1b]0;jt-live-whisper\x07")
            sys.stdout.flush()
        except Exception:
            pass
        return
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
    """開啟輕量 InputStream 被動監控 Loopback 裝置音量（Whisper 無錄音時用）。
    macOS BlackHole 支援多讀取者，不影響 whisper-stream。回傳 stream 物件。"""
    import sounddevice as sd
    import numpy as np

    # Windows: 優先用 WASAPI Loopback
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            def _monitor_cb_wasapi(indata, frames, time_info, status):
                _push_rms(float(np.sqrt(np.mean(indata ** 2))))
            try:
                sr = int(wb_info["defaultSampleRate"])
                ch = wb_info["maxInputChannels"]
                stream = _WasapiLoopbackStream(
                    callback=_monitor_cb_wasapi, samplerate=sr,
                    channels=ch, blocksize=int(sr * 0.1))
                stream.start()
                return stream
            except Exception:
                return None

    # 找 Loopback PortAudio device
    bh_id = None
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
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
    _sc = _START_CMD
    examples = [
        (f"{_sc}", "互動式選單"),
        (f"{_sc} -s training", "教育訓練場景"),
        (f"{_sc} --mode zh", "中文轉錄模式"),
        (f"{_sc} --asr moonshine", "使用 Moonshine 引擎"),
        (f"{_sc} --topic 'ZFS 儲存管理'", "指定會議主題，提升翻譯品質"),
        (f"{_sc} -m large-v3-turbo -e llm -d 0", "全部指定，跳過選單"),
        (f"{_sc} --input meeting.mp3", "離線處理音訊檔（互動選單）"),
        (f"{_sc} --input meeting.mp3 --mode en2zh", "離線處理（直接執行，跳過選單）"),
        (f"{_sc} --input meeting.mp3 --mode en", "離線處理（純英文轉錄）"),
        (f"{_sc} --input f1.mp3 f2.m4a --summarize", "離線處理 + 摘要"),
        (f"{_sc} --input meeting.mp3 --diarize", "離線處理 + 講者辨識"),
        (f"{_sc} --input meeting.mp3 --diarize --mode zh", "中文逐字稿 + 講者辨識"),
        (f"{_sc} --input meeting.mp3 --mode zh --summarize", "中文逐字稿 + 摘要修正"),
        (f"{_sc} --input meeting.mp3 --diarize --num-speakers 3", "指定 3 位講者"),
        (f"{_sc} --input meeting.mp3 --diarize --summarize", "辨識 + 翻譯 + 摘要"),
        (f"{_sc} --input m.mp3 --diarize --mode zh --summarize", "中文辨識 + 講者 + 摘要"),
        (f"{_sc} --input meeting.mp3 --local-asr", "強制本機 辨識"),
        (f"{_sc} --summarize log1.txt log2.txt", "批次摘要記錄檔"),
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
        "--asr", choices=["whisper", "moonshine", "faster-whisper"], metavar="ASR",
        help="語音辨識引擎 (whisper / moonshine / faster-whisper，預設 whisper)")
    parser.add_argument(
        "-m", "--model", choices=model_names, metavar="MODEL",
        help=f"Whisper 模型 ({' / '.join(model_names)}，--input 預設 large-v3-turbo，中日文品質最好用 -m large-v3)")
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
        "-e", "--engine", choices=["llm", "argos", "nllb"], metavar="ENGINE",
        help="翻譯引擎 (llm / argos / nllb)")
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
        "--mic-device", type=int, metavar="ID",
        help="麥克風裝置 ID（--mic 或雙向模式時指定麥克風輸入裝置）")
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
        "--summary-rounds", type=int, metavar="N", default=1,
        help="摘要處理次數（1-3，多次處理後整合可提升品質，預設 1）")
    parser.add_argument(
        "--diarize", action="store_true",
        help="講者辨識（需搭配 --input，用 resemblyzer + spectralcluster）")
    parser.add_argument(
        "--num-speakers", type=int, metavar="N",
        help="指定講者人數（預設自動偵測 2~8，需搭配 --diarize）")
    parser.add_argument(
        "--mic", action="store_true",
        help="同時轉錄麥克風語音（ASR 負載加倍，改用 faster-whisper/mlx-whisper 雙路辨識）")
    parser.add_argument(
        "--denoise", action="store_true",
        help="即時模式啟用背景降噪（noisereduce spectral gating，推薦搭配麥克風使用）")
    parser.add_argument(
        "--local-asr", action="store_true",
        help="強制使用本機 辨識（忽略GPU 伺服器 設定，即時模式與離線模式皆適用）")
    parser.add_argument(
        "--restart-server", action="store_true",
        help="強制重啟GPU 伺服器（更新 server.py 後使用）")
    parser.add_argument(
        "--webui", action="store_true",
        help="同時將即時字幕推送到 WebUI（需另外啟動 webui.py）")
    return parser.parse_args()


def auto_select_device(model_path):
    """非互動模式：自動偵測 Loopback 裝置，找不到就報錯退出"""
    devices = _enumerate_sdl_devices(model_path)

    if not devices:
        if IS_WINDOWS and _find_wasapi_loopback():
            print(f"{C_ERR}[錯誤] Whisper (whisper-stream) 使用 SDL2，無法擷取 Windows 系統播放聲音。{RESET}", file=sys.stderr)
            print(f"{C_WARN}  建議改用 --asr moonshine 或遠端 GPU 辨識。{RESET}", file=sys.stderr)
            sys.exit(1)
        print("[錯誤] 找不到任何音訊捕捉裝置！", file=sys.stderr)
        sys.exit(1)

    # 自動選 Loopback 裝置
    for dev_id, dev_name in devices:
        if _is_loopback_device(dev_name):
            print(f"{C_OK}自動選擇音訊裝置: [{dev_id}] {dev_name}{RESET}")
            return dev_id

    # 找不到 Loopback，用第一個裝置
    dev_id, dev_name = devices[0]
    print(f"{C_HIGHLIGHT}未偵測到 {_LOOPBACK_LABEL}，使用: [{dev_id}] {dev_name}{RESET}")
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
    """根據設定組裝等效的啟動指令字串（所有有值的參數都明確列出）"""
    import shlex
    parts = [_START_CMD]

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

    mic = kwargs.get("mic")
    if mic:
        parts.append("--mic")

    denoise = kwargs.get("denoise")
    if denoise:
        parts.append("--denoise")

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

    # --webui：啟動 event sender（webui.py 由 start.sh 或使用者另外啟動）
    if args.webui:
        _start_webui_sender()

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
        # 自動偵測雙向配對：若未指定 mode 但輸入檔案符合配對，從檔名推斷模式
        if args.mode is None and _detect_bidi_file_pair(args.input):
            _FNAME_MODE_MAP = {"英中雙向": "en_zh", "日中雙向": "ja_zh"}
            _detected_mode = "en_zh"  # 預設（向下相容舊檔名無模式標籤）
            for fpath in args.input:
                fname = os.path.basename(fpath)
                for label, m_key in _FNAME_MODE_MAP.items():
                    if label in fname:
                        _detected_mode = m_key
                        break
            args.mode = _detected_mode
            _mode_label = next(n for k, n, _ in MODE_PRESETS if k == _detected_mode)
            print(f"  {C_OK}偵測到雙向錄音配對，自動切換為 {_detected_mode} 模式{RESET}")
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
             summary_mode, engine) = _input_interactive_menu(args)
            if engine == "llm" and not server_type:
                server_type = "ollama"
            # 雙向模式：確認已選檔案是否為配對，若否則重新選擇
            if mode in _BIDI_MODES:
                _pair = _detect_bidi_file_pair(args.input)
                if not _pair:
                    # 已選檔案不是配對，嘗試從 recordings/ 選取
                    pairs = _select_bidi_audio_pairs()
                    if not pairs:
                        print(f"\n  {C_HIGHLIGHT}recordings/ 下沒有雙向錄音配對（需要「_系統音訊」和「_麥克風」時間戳相同的兩個檔案）{RESET}")
                        print(f"  {C_DIM}請改選其他模式，或先進行雙向即時錄音{RESET}")
                        sys.exit(1)
                    # 顯示配對列表
                    print(f"\n\n{C_TITLE}{BOLD}▎ 選擇雙向錄音{RESET}")
                    print(f"  {C_DIM}已自動配對時間戳相同的系統音訊與麥克風錄音{RESET}")
                    print(f"{C_DIM}{'─' * 60}{RESET}")
                    for i, (lp, mp, ts) in enumerate(pairs):
                        lb_name = os.path.basename(lp)
                        mic_name = os.path.basename(mp)
                        lb_size = os.path.getsize(lp)
                        mic_size = os.path.getsize(mp)
                        total_size = lb_size + mic_size
                        size_str = (f"{total_size / 1048576:.1f} MB" if total_size >= 1048576
                                    else f"{total_size / 1024:.0f} KB")
                        # 嘗試取得時長
                        _dur_str = ""
                        _probe = _ffprobe_info(lp)
                        if _probe and _probe[0] > 0:
                            _dm, _ds = divmod(int(_probe[0]), 60)
                            _dur_str = f"  {_dm}:{_ds:02d}"
                        if i == 0:
                            print(f"  {C_HIGHLIGHT}{BOLD}[{i}] {lb_name}  +  {mic_name}{_dur_str}  ({size_str}){RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
                        else:
                            print(f"  {C_DIM}[{i}]{RESET} {C_WHITE}{lb_name}  +  {mic_name}{_dur_str}  ({size_str}){RESET}")
                    print(f"{C_DIM}{'─' * 60}{RESET}")
                    print(f"{C_WHITE}按 Enter 使用預設，或輸入編號：{RESET}", end=" ")
                    try:
                        _sel = input().strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)
                    _pair_idx = 0
                    if _sel:
                        try:
                            _pair_idx = int(_sel)
                            if not (0 <= _pair_idx < len(pairs)):
                                _pair_idx = 0
                        except ValueError:
                            _pair_idx = 0
                    _sel_lb, _sel_mic, _ = pairs[_pair_idx]
                    args.input = [_sel_lb, _sel_mic]
        else:
            mode = args.mode or "en2zh"
            diarize = args.diarize
            num_speakers = args.num_speakers
            do_summarize = args.summarize is not None
            summary_mode = "both"  # CLI 模式預設
            _default_fw = "large-v3" if (mode in _NOENG_MODELS and (REMOTE_WHISPER_CONFIG or _has_local_gpu())) else "large-v3-turbo"
            fw_model = args.model or _default_fw
            host, port = _resolve_ollama_host(args)
            server_type = None  # CLI 模式稍後偵測
            need_translate_cli = mode in _TRANSLATE_MODES
            ollama_model = None
            if need_translate_cli:
                if args.engine or args.ollama_model or args.ollama_host:
                    # 有指定任何翻譯相關參數 → 隱含 -e llm
                    engine = args.engine or "llm"
                else:
                    # 未指定翻譯參數：自動偵測或用互動選單
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port, mode)
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
            # GPU 伺服器：有設定且未指定 --local-asr
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
        need_translate = mode in _TRANSLATE_MODES
        if not ollama_model:
            ollama_model = "qwen2.5:14b"

        # ── 連線檢查 ──
        ollama_available = False
        need_llm_translate = need_translate and engine == "llm"
        need_llm_summary = do_summarize and summary_mode != "transcript"
        need_remote_asr = use_remote_whisper and REMOTE_WHISPER_CONFIG
        need_check = need_llm_translate or need_llm_summary or need_remote_asr

        if need_check:
            print(f"\n\n{C_TITLE}{BOLD}▎ 連線檢查{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")

        if need_llm_translate or need_llm_summary:
            if not server_type:
                server_type = _detect_llm_server(host, port)
            if server_type:
                srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
                if need_llm_translate:
                    print(f"  {C_WHITE}LLM 翻譯    {RESET}{C_WHITE}{ollama_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                if need_llm_summary:
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
        # 雙向模式用 loopback 方向建主翻譯器，mic 翻譯器在 bidi pair 偵測後另建
        _trans_dir = _BIDI_LB_DIR[mode] if mode in _BIDI_MODES else mode
        translator = None
        can_summarize = ollama_available
        if need_translate:
            if engine == "llm" and ollama_available:
                translator = OllamaTranslator(ollama_model, host, port, direction=_trans_dir,
                                              skip_check=True, server_type=server_type,
                                              meeting_topic=meeting_topic)
            elif engine == "llm" and not ollama_available:
                # LLM 伺服器連不上：降級處理
                if os.path.isdir(NLLB_MODEL_DIR):
                    print(f"  {C_HIGHLIGHT}[降級] 改用 NLLB 離線翻譯（品質較低）{RESET}")
                    translator = NllbTranslator(direction=_trans_dir)
                elif _trans_dir == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
                    print(f"  {C_HIGHLIGHT}[降級] 改用 Argos 離線翻譯（品質較低）{RESET}")
                    translator = ArgosTranslator()
                else:
                    print(f"  {C_HIGHLIGHT}[警告] 無離線翻譯可用，將只做轉錄（不翻譯）{RESET}")
            elif engine == "nllb":
                translator = NllbTranslator(direction=_trans_dir)
            else:
                # 使用者明確指定 argos
                if mode in ("zh2en", "ja2zh", "zh2ja") or mode in _BIDI_MODES:
                    print(f"{C_HIGHLIGHT}[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB{RESET}",
                          file=sys.stderr)
                    sys.exit(1)
                translator = ArgosTranslator()

        if need_llm_summary and not can_summarize:
            print(f"  {C_HIGHLIGHT}[警告] LLM 伺服器無法連接，摘要將跳過（逐字稿完成後可用 --summarize 補做）{RESET}")

        # GPU 伺服器 啟動與 health check
        remote_whisper_cfg = None
        if need_remote_asr:
            rw_cfg = REMOTE_WHISPER_CONFIG
            rw_host = rw_cfg.get("host", "?")
            rw_port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
            print(f"  {C_WHITE}伺服器辨識    {RESET}{C_WHITE}{fw_model}{RESET} {C_DIM}@ {rw_host}:{rw_port}{RESET}", end="", flush=True)
            # 檢查伺服器是否已在執行，沒有才啟動（支援多實例共用）
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
                    print(f"{C_HIGHLIGHT}✓ 已連線（注意：伺服器未偵測到 GPU，將以 CPU 辨識，速度較慢）{RESET}")
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
            print(f"  {C_WHITE}辨識位置    GPU 伺服器（{rw_h}）{RESET}")
        else:
            print(f"  {C_WHITE}辨識位置    本機{RESET}")
        if need_translate:
            if engine == "argos":
                print(f"  {C_WHITE}翻譯模型    Argos 本機離線{RESET}")
            elif engine == "nllb":
                print(f"  {C_WHITE}翻譯模型    NLLB 本機離線{RESET}")
            else:
                _srv_disp = f"{ollama_model} @ {host}:{port}"
                print(f"  {C_WHITE}翻譯模型    {_srv_disp}{RESET}")
        if diarize:
            sp_info = "resemblyzer + spectralcluster"
            if remote_whisper_cfg:
                sp_info += f"，GPU 伺服器（{remote_whisper_cfg.get('host', '?')}）"
            else:
                sp_info += "，本機"
            sp_info += f"，{num_speakers} 人" if num_speakers else "，自動偵測"
            print(f"  {C_WHITE}講者辨識    {sp_info}{RESET}")
        if do_summarize and summary_mode != "transcript" and host:
            print(f"  {C_WHITE}摘要模型    {summary_model} @ {host}:{port}{RESET}")
        if meeting_topic:
            print(f"  {C_WHITE}會議主題    {meeting_topic}{RESET}")
        print(f"  {C_WHITE}檔案數      {RESET}{C_DIM}{len(args.input)}{RESET}")

        # CLI 指令回顯 + 確認（在設定總覽區塊內）
        _cli_kw = dict(input_files=args.input, mode=mode, model=fw_model,
                       diarize=diarize, num_speakers=num_speakers,
                       summarize=(do_summarize and summary_mode != "transcript"),
                       summary_model=summary_model if summary_mode != "transcript" else None,
                       engine=engine if engine in ("argos", "nllb") else None,
                       llm_model=ollama_model if need_translate and engine == "llm" else None,
                       llm_host=f"{host}:{port}" if need_translate and engine == "llm" and host else None,
                       topic=meeting_topic,
                       local_asr=args.local_asr)
        if not _confirm_start(_build_cli_command(**_cli_kw)):
            sys.exit(0)

        # 逐檔處理
        _do_llm_correct = do_summarize and can_summarize and summary_mode != "transcript"
        log_paths = []  # list of (log_path, original_input_path, session_dir)
        html_to_open = []  # 收集所有 HTML，最後一起開啟

        # 雙向配對偵測
        _bidi_pair = _detect_bidi_file_pair(args.input)
        # 單檔自動偵測配對：檔名含「系統音訊」或「麥克風」時，找同 timestamp 配對
        if not _bidi_pair and len(args.input) == 1 and mode in _LB_LANG:
            import re as _re_pair
            _fname = os.path.basename(args.input[0])
            _fdir = os.path.dirname(args.input[0]) or RECORDING_DIR
            _m_lb = _re_pair.match(r"(錄音.*_)系統音訊(_\d{8}_\d{6}\..*)", _fname)
            _m_mic = _re_pair.match(r"(錄音.*_)麥克風(_\d{8}_\d{6}\..*)", _fname)
            if _m_lb or _m_mic:
                if _m_lb:
                    _other_fname = _m_lb.group(1) + "麥克風" + _m_lb.group(2)
                else:
                    _other_fname = _m_mic.group(1) + "系統音訊" + _m_mic.group(2)
                _other_path = os.path.join(_fdir, _other_fname)
                if os.path.isfile(_other_path):
                    print(f"\n  {C_OK}偵測到配對檔案: {_other_fname}{RESET}")
                    print(f"  {C_WHITE}要一起處理嗎？(Y/n)：{RESET}", end=" ")
                    try:
                        _ans = input().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        _ans = "n"
                    if _ans != "n":
                        if _m_lb:
                            args.input = [args.input[0], _other_path]
                        else:
                            args.input = [_other_path, args.input[0]]
                        _bidi_pair = _detect_bidi_file_pair(args.input)
        if _bidi_pair and mode in _LB_LANG:
            lb_path, mic_path = _bidi_pair
            # 建立兩路翻譯器
            _bidi_need_translate = mode in _TRANSLATE_MODES
            if _bidi_need_translate and translator:
                translator_lb = translator  # 已建好的翻譯器（lb 方向）
                # mic 翻譯器：雙向模式需要反方向翻譯，其他模式 mic 只轉錄
                if mode in _BIDI_MODES:
                    _mic_dir = _BIDI_MIC_DIR[mode]
                    if engine == "llm" and ollama_available:
                        translator_mic = OllamaTranslator(ollama_model, host, port, direction=_mic_dir,
                                                          skip_check=True, server_type=server_type,
                                                          meeting_topic=meeting_topic)
                    elif engine == "nllb":
                        translator_mic = NllbTranslator(direction=_mic_dir)
                    else:
                        translator_mic = None
                else:
                    translator_mic = None  # 非雙向模式：mic 只轉錄
            else:
                translator_lb = None
                translator_mic = None
            try:
                log_path, t_html, session_dir = process_bidi_audio_files(
                    lb_path, mic_path, mode, translator_lb, translator_mic,
                    model_size=fw_model, remote_whisper_cfg=remote_whisper_cfg,
                    diarize=diarize, num_speakers=num_speakers,
                    correct_with_llm=_do_llm_correct,
                    llm_model=summary_model, llm_host=host, llm_port=port,
                    llm_server_type=server_type, meeting_topic=meeting_topic)
                if log_path:
                    log_paths.append((log_path, lb_path, session_dir))
                if t_html:
                    html_to_open.append(t_html)
            except KeyboardInterrupt:
                pass
        else:
            try:
                for fpath in args.input:
                    log_path, t_html, session_dir = process_audio_file(fpath, mode, translator, model_size=fw_model,
                                                           diarize=diarize, num_speakers=num_speakers,
                                                           remote_whisper_cfg=remote_whisper_cfg,
                                                           correct_with_llm=_do_llm_correct,
                                                           llm_model=summary_model, llm_host=host, llm_port=port,
                                                           llm_server_type=server_type, meeting_topic=meeting_topic)
                    if log_path:
                        log_paths.append((log_path, fpath, session_dir))
                    if t_html:
                        html_to_open.append(t_html)
            except KeyboardInterrupt:
                remaining = len(args.input) - len(log_paths)
                if remaining > 1:
                    print(f"\n{C_DIM}已中止，跳過剩餘 {remaining - 1} 個檔案。{RESET}")

        # 伺服器保持執行（不停止，允許多實例共用）
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
                    "asr_location": f"GPU 伺服器 ({remote_whisper_cfg.get('host', '?')})" if remote_whisper_cfg else "本機",
                    "diarize": diarize,
                    "diarize_engine": "resemblyzer + spectralcluster" if diarize else None,
                    "diarize_location": f"GPU 伺服器 ({remote_whisper_cfg.get('host', '?')})" if diarize and remote_whisper_cfg else ("本機" if diarize else None),
                    "num_speakers": num_speakers if num_speakers else "自動偵測",
                    "translate_model": ollama_model if need_translate and ollama_available else None,
                    "translate_server": f"{srv_label} @ {host}:{port}" if need_translate and ollama_available else None,
                    "input_format": os.path.splitext(orig_fpath)[1].lstrip(".").lower(),
                    "input_file": f"{os.path.basename(_bidi_pair[0])} + {os.path.basename(_bidi_pair[1])}" if _bidi_pair else os.path.basename(orig_fpath),
                    "summary_model": summary_model,
                    "summary_server": f"{srv_label} @ {host}:{port}",
                    "meeting_topic": meeting_topic,
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
                    _sum_rounds = getattr(args, 'summary_rounds', 1) or 1
                    out_path, _, html_path = summarize_log_file(lp, summary_model, host, port,
                                                                  server_type=server_type,
                                                                  topic=meeting_topic,
                                                                  metadata=_meta,
                                                                  summary_mode=summary_mode,
                                                                  audio_path=audio_in_session,
                                                                  summary_rounds=_sum_rounds)
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
            open_file_in_editor(hp)
        # 開啟每個 session 子目錄（Finder / Explorer）
        opened_dirs = set()
        for _, _, sess_dir in log_paths:
            if sess_dir and sess_dir not in opened_dirs:
                opened_dirs.add(sess_dir)
                open_file_in_editor(sess_dir)

        if not log_paths:
            print(f"\n{C_HIGHLIGHT}沒有成功處理的檔案{RESET}")
            sys.exit(1)

        print(f"\n{C_HIGHLIGHT}按 ESC 鍵退出{RESET}", flush=True)
        _wait_for_esc()
        sys.exit(0)

    # --summarize 批次摘要模式（不需 ASR 引擎）
    if args.summarize is not None:
        if not args.summarize:
            print(f"{C_HIGHLIGHT}[錯誤] --summarize 需要指定記錄檔，例如: {_START_CMD} --summarize log.txt{RESET}",
                  file=sys.stderr)
            sys.exit(1)
        host, port = _resolve_ollama_host(args)
        model = args.summary_model

        print(f"\n\n{C_TITLE}{BOLD}▎ 批次摘要模式{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_DIM}摘要模型: {model} ({host}:{port}){RESET}")

        print(f"  {C_DIM}正在連接 LLM 伺服器...{RESET}", end=" ", flush=True)
        _webui_send({"type": "progress", "stage": "載入中", "detail": f"連接 LLM 伺服器（{host}:{port}）"})
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

            # 標題格式修正
            summary = re.sub(r'#{2,4}\s*(?:最終)?(?:重點)?摘要', '## 重點摘要', summary)
            summary = re.sub(r'#{2,4}\s*(?:校正)?逐字稿', '## 校正逐字稿', summary)

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
            open_file_in_editor(html_path)

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

        # ── 雙向翻譯模式（en_zh / ja_zh）：獨立路徑 ──
        if mode in _BIDI_MODES:
            # 驗證不支援的組合
            if args.asr == "moonshine":
                print("[錯誤] 雙向模式不支援 Moonshine（僅支援英文 ASR）", file=sys.stderr)
                sys.exit(1)
            if args.engine == "argos":
                print("[錯誤] 雙向模式不支援 Argos 離線翻譯（僅支援英翻中單向）", file=sys.stderr)
                sys.exit(1)

            bidi = _detect_bidi_devices()
            if bidi is None:
                print("[錯誤] 找不到系統音訊裝置或麥克風", file=sys.stderr)
                sys.exit(1)
            _bidi_lb_id, _bidi_lb_name, _bidi_mic_id, _bidi_mic_name = bidi
            # --mic-device 覆蓋自動偵測的麥克風
            if getattr(args, 'mic_device', None) is not None:
                import sounddevice as _sd_md
                _bidi_mic_id = args.mic_device
                _bidi_mic_name = _sd_md.query_devices(_bidi_mic_id)["name"]
            print(f"  {C_OK}系統音訊: {_bidi_lb_name}{RESET}")
            print(f"  {C_OK}麥克風:   {_bidi_mic_name}{RESET}")

            # 模型
            model_name = args.model or _recommended_whisper_model(mode)
            if model_name.endswith(".en"):
                print(f"[錯誤] 雙向模式不支援 {model_name}（僅英文模型），請用多語言模型", file=sys.stderr)
                sys.exit(1)

            # Apple Silicon + mlx-whisper 自動偵測（--asr faster-whisper 可退回）
            _use_mlx_bidi = False
            if _is_apple_silicon() and _has_mlx_whisper() and args.asr != "faster-whisper":
                _use_mlx_bidi = True

            # 翻譯引擎
            meeting_topic = args.topic
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            ollama_model = None
            if args.engine or args.ollama_model or args.ollama_host:
                engine = args.engine or "llm"
            else:
                engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port, mode)
                if engine == "llm":
                    ollama_model = _sel_model
                    if _sel_host: host = _sel_host
                    if _sel_port: port = _sel_port
                    if _sel_srv: srv_type = _sel_srv
            if engine == "llm":
                if not ollama_model:
                    ollama_model = args.ollama_model or _select_llm_model(host, port, srv_type)
                translator_lb = OllamaTranslator(ollama_model, host, port, direction=_BIDI_LB_DIR[mode],
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                translator_mic = OllamaTranslator(ollama_model, host, port, direction=_BIDI_MIC_DIR[mode],
                                                   server_type=srv_type, skip_check=True,
                                                   meeting_topic=meeting_topic)
            elif engine == "nllb":
                translator_lb = NllbTranslator(direction=_BIDI_LB_DIR[mode])
                translator_mic = NllbTranslator(direction=_BIDI_MIC_DIR[mode])
            else:
                print("[錯誤] 雙向模式不支援 Argos 離線翻譯", file=sys.stderr)
                sys.exit(1)

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]

            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            _fw_label = "mlx-whisper GPU" if _use_mlx_bidi else "faster-whisper"
            print(f"{C_DIM}模式: {mode_label} | ASR: Whisper ({model_name}) [{_fw_label}] | 翻譯: {engine}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            if args.denoise:
                print(f"{C_DIM}降噪: 已啟用（noisereduce）{RESET}")
            _cli_kw = dict(mode=mode, model=model_name, topic=meeting_topic,
                           engine=engine,
                           llm_model=ollama_model if engine == "llm" else None,
                           llm_host=f"{host}:{port}" if engine == "llm" else None,
                           denoise=args.denoise)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream_bidirectional(_bidi_lb_id, _bidi_mic_id,
                                     translator_lb, translator_mic,
                                     model_name, mode,
                                     length_ms=length_ms, step_ms=step_ms,
                                     record=args.record,
                                     meeting_topic=meeting_topic,
                                     use_mlx=_use_mlx_bidi,
                                     denoise=args.denoise)
            sys.exit(0)

        # --mic 衝突檢查
        if args.mic:
            if args.asr == "moonshine":
                print(f"{C_ERR}[錯誤] --mic 不支援 Moonshine（僅英文，無法辨識中/日文麥克風）{RESET}", file=sys.stderr)
                sys.exit(1)
            if mode in _BIDI_MODES or mode == "record":
                args.mic = False  # 靜默忽略（雙向模式已是雙向、record 無 ASR）

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
        # 中文/日文模式強制 whisper（Moonshine 僅支援英文）
        if mode not in ("en2zh", "en"):
            asr_engine = "whisper"
        # --mic 不支援 moonshine（互動選單選到 moonshine 的情況）
        if args.mic and asr_engine == "moonshine":
            print(f"{C_WARN}[警告] --mic 不支援 Moonshine，忽略 --mic{RESET}")
            args.mic = False

        # GPU 伺服器 Whisper 即時模式（非 Moonshine、非 --local-asr）
        use_remote_cli = (REMOTE_WHISPER_CONFIG and not args.local_asr
                          and asr_engine != "moonshine")
        if use_remote_cli and args.mic:
            print(f"{C_WARN}[警告] --mic 不支援遠端 GPU 模式，忽略 --mic{RESET}")
            args.mic = False
        if use_remote_cli:
            # 伺服器模式：不需本機 whisper-stream
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
            if mode in _TRANSLATE_MODES:
                ollama_model = None
                if args.engine or args.ollama_model or args.ollama_host:
                    engine = args.engine or "llm"
                else:
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port, mode)
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
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
                else:
                    if mode in ("zh2en", "ja2zh", "zh2ja"):
                        print(f"[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB", file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]

            rw_host = REMOTE_WHISPER_CONFIG.get("host", "?")
            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            print(f"{C_DIM}模式: {mode_label} | ASR: Whisper ({model_name}) @ GPU 伺服器（{rw_host}） | "
                  f"裝置: {capture_id} | 翻譯: {engine}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            if args.denoise:
                print(f"{C_DIM}降噪: 已啟用（noisereduce）{RESET}")
            _cli_kw = dict(mode=mode, model=model_name, device=args.device,
                           scene=args.scene, topic=meeting_topic,
                           llm_model=ollama_model if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           engine=engine if mode in _TRANSLATE_MODES else None,
                           llm_host=f"{host}:{port}" if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           record=args.record, rec_device=args.rec_device,
                           denoise=args.denoise)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream_remote(capture_id, translator, model_name, REMOTE_WHISPER_CONFIG,
                              mode, length_ms, step_ms,
                              record=args.record, rec_device=args.rec_device,
                              force_restart=args.restart_server,
                              meeting_topic=meeting_topic,
                              denoise=args.denoise)
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
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port, mode)
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
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
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
            default_model = args.model or _recommended_whisper_model(mode)
            model_name = default_model
            if mode in _NOENG_MODELS and model_name.endswith(".en"):
                print(f"[錯誤] {mode} 模式不支援 {model_name}（僅英文模型），請用 small、medium、large-v3-turbo 或 large-v3",
                      file=sys.stderr)
                sys.exit(1)

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]

            # Windows: 先判斷是否改用 faster-whisper（在 resolve_model 之前）
            _cli_use_local_fw = False
            if args.device is not None:
                capture_id = args.device
                # WASAPI Loopback 裝置無法用 whisper-stream (SDL2)，改用 faster-whisper
                if IS_WINDOWS and capture_id == WASAPI_LOOPBACK_ID:
                    _cli_use_local_fw = True
            elif IS_WINDOWS and _find_wasapi_loopback():
                _, _probe_path = resolve_model("large-v3-turbo")
                _sdl_devs = _enumerate_sdl_devices(_probe_path)
                if not _sdl_devs:
                    _cli_use_local_fw = True
                    capture_id = auto_select_device_sd()
                else:
                    capture_id = auto_select_device(_probe_path)

            if _cli_use_local_fw:
                model_path = None  # faster-whisper 自動從 HuggingFace 下載
            else:
                model_name, model_path = resolve_model(model_name)
                if args.device is None and not (IS_WINDOWS and _find_wasapi_loopback()):
                    capture_id = auto_select_device(model_path)

            translator = None
            meeting_topic = args.topic
            host, port = _resolve_ollama_host(args)
            srv_type = _detect_llm_server(host, port) or "ollama"
            if mode in _TRANSLATE_MODES:
                ollama_model = None
                if args.engine or args.ollama_model or args.ollama_host:
                    engine = args.engine or "llm"
                else:
                    engine, _sel_model, _sel_host, _sel_port, _sel_srv = select_translator(host, port, mode)
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
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
                else:
                    if mode in ("zh2en", "ja2zh", "zh2ja"):
                        print(f"[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB", file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            s_host, s_port = host, port

            _asr_label = f"Whisper ({model_name})" + (" [faster-whisper]" if _cli_use_local_fw else "")
            mode_label = next(name for k, name, _ in MODE_PRESETS if k == mode)
            print(f"{C_DIM}模式: {mode_label} | ASR: {_asr_label} | 場景: {scene_key} | "
                  f"裝置: {capture_id} | 翻譯: {engine}{RESET}")
            if meeting_topic:
                print(f"{C_DIM}會議主題: {meeting_topic}{RESET}")
            if args.denoise:
                print(f"{C_DIM}降噪: 已啟用（noisereduce）{RESET}")
            _cli_kw = dict(mode=mode, model=model_name, scene=args.scene,
                           device=args.device, topic=meeting_topic,
                           llm_model=ollama_model if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           engine=engine if mode in _TRANSLATE_MODES else None,
                           llm_host=f"{host}:{port}" if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           record=args.record, rec_device=args.rec_device,
                           mic=args.mic, denoise=args.denoise)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            if args.mic:
                # --mic 模式：切換到雙路架構（loopback + 麥克風）
                bidi_devs = _detect_bidi_devices()
                if not bidi_devs:
                    print(f"{C_WARN}[警告] 找不到麥克風裝置，忽略 --mic{RESET}")
                    args.mic = False
                else:
                    _mic_lb_id, _, _mic_mic_id, _ = bidi_devs
                    if getattr(args, 'mic_device', None) is not None:
                        _mic_mic_id = args.mic_device
                    _use_mlx = _is_apple_silicon() and _has_mlx_whisper() and args.asr != "faster-whisper"
                    # 效能檢查：--mic 改用 faster-whisper/mlx-whisper，模型可能需要調整
                    _mic_rec_model = _recommended_whisper_model(mode)
                    _mic_model = model_name
                    if not _use_mlx and not _has_local_gpu():
                        # 無 GPU 加速，大模型在 faster-whisper CPU 上會太慢
                        _big_models = ("large-v3-turbo", "large-v3", "medium", "medium.en")
                        if model_name in _big_models and _mic_rec_model not in _big_models:
                            print(f"\n{C_WARN}[效能提示] --mic 模式改用 faster-whisper 雙路辨識（非 whisper-stream）{RESET}")
                            print(f"  {C_WARN}此裝置無 GPU 加速，{model_name} 辨識可能太慢（每段 10s+）{RESET}")
                            print(f"  {C_WARN}自動調整為 {_mic_rec_model}（適合此裝置的 faster-whisper 模型）{RESET}")
                            _mic_model = _mic_rec_model
                    elif not _use_mlx:
                        # 有 GPU 但不是 mlx，提示引擎切換
                        print(f"\n{C_DIM}--mic 模式：ASR 引擎切換為 faster-whisper 雙路辨識{RESET}")
                    run_stream_bidirectional(_mic_lb_id, _mic_mic_id,
                                             translator, None,
                                             _mic_model, mode,
                                             length_ms=length_ms, step_ms=step_ms,
                                             record=args.record,
                                             meeting_topic=meeting_topic,
                                             use_mlx=_use_mlx,
                                             mic_translate=False,
                                             denoise=args.denoise)
                    sys.exit(0)
            if _cli_use_local_fw:
                run_stream_local_whisper(capture_id, translator, model_name, mode,
                                        length_ms=length_ms, step_ms=step_ms,
                                        record=args.record, rec_device=args.rec_device,
                                        meeting_topic=meeting_topic,
                                        denoise=args.denoise)
            else:
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

        # ── 雙向翻譯模式（en_zh / ja_zh）：獨立路徑 ──
        if mode in _BIDI_MODES:
            bidi = _detect_bidi_devices()
            if bidi is None:
                print(f"{C_ERR}[錯誤] 找不到系統音訊裝置或麥克風{RESET}")
                if IS_MACOS:
                    print(f"  {C_WHITE}請確認已安裝 BlackHole 並設定為系統音訊輸出{RESET}")
                elif IS_WINDOWS:
                    print(f"  {C_WHITE}請確認 WASAPI Loopback 可用且有麥克風{RESET}")
                sys.exit(1)
            _bidi_lb_id, _bidi_lb_name, _bidi_mic_id, _bidi_mic_name = bidi
            # --mic-device 覆蓋自動偵測的麥克風
            if getattr(args, 'mic_device', None) is not None:
                import sounddevice as _sd_md
                _bidi_mic_id = args.mic_device
                _bidi_mic_name = _sd_md.query_devices(_bidi_mic_id)["name"]
            print(f"  {C_OK}系統音訊: {_bidi_lb_name}{RESET}")
            print(f"  {C_OK}麥克風:   {_bidi_mic_name}{RESET}")

            # 強制多語言 faster-whisper 模型
            model_name, _ = select_whisper_model(mode, use_faster_whisper=True)

            # Apple Silicon + mlx-whisper 自動偵測
            _use_mlx_bidi = False
            if _is_apple_silicon() and _has_mlx_whisper():
                _use_mlx_bidi = True

            # 選擇翻譯引擎（排除 Argos）
            engine, model, host, port, srv_type = select_translator(mode=mode)
            meeting_topic = _ask_topic()

            if engine == "llm":
                translator_lb = OllamaTranslator(model, host, port, direction=_BIDI_LB_DIR[mode],
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                translator_mic = OllamaTranslator(model, host, port, direction=_BIDI_MIC_DIR[mode],
                                                   server_type=srv_type, skip_check=True,
                                                   meeting_topic=meeting_topic)
            elif engine == "nllb":
                translator_lb = NllbTranslator(direction=_BIDI_LB_DIR[mode])
                translator_mic = NllbTranslator(direction=_BIDI_MIC_DIR[mode])
            else:
                print(f"{C_ERR}[錯誤] 雙向模式不支援 Argos 離線翻譯（僅支援單向）{RESET}")
                print(f"  {C_WHITE}請使用 LLM 伺服器或 NLLB 離線翻譯{RESET}")
                sys.exit(1)

            # 錄音
            record_bidi = False
            try:
                print(f"\n{C_WHITE}是否錄音？[y/N]{RESET} ", end="", flush=True)
                _rec_ans = input().strip().lower()
                record_bidi = _rec_ans in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                print()

            # 場景（音訊緩衝長度）
            length_ms, step_ms = select_scene()

            _cli_kw = dict(mode=mode, model=model_name, topic=meeting_topic,
                           engine=engine,
                           llm_model=model if engine == "llm" else None,
                           llm_host=f"{host}:{port}" if engine == "llm" else None,
                           denoise=args.denoise)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            print()
            run_stream_bidirectional(_bidi_lb_id, _bidi_mic_id,
                                     translator_lb, translator_mic,
                                     model_name, mode,
                                     length_ms=length_ms, step_ms=step_ms,
                                     record=record_bidi,
                                     meeting_topic=meeting_topic,
                                     use_mlx=_use_mlx_bidi,
                                     denoise=args.denoise)
            sys.exit(0)

        # 轉錄模式：提前詢問麥克風轉錄（影響辨識位置預設值）
        _early_mic = False
        _early_bidi_devs = None
        if mode in ("en", "zh", "ja"):
            _early_bidi_devs = _detect_bidi_devices()
            if _early_bidi_devs:
                try:
                    print(f"\n{C_WHITE}是否同時轉錄本機麥克風輸入？[y/N]{RESET} ", end="", flush=True)
                    _mic_ans = input().strip().lower()
                    _early_mic = _mic_ans in ("y", "yes")
                except (EOFError, KeyboardInterrupt):
                    print()

        # 辨識位置（GPU 伺服器 / 本機），僅在有設定時顯示
        use_remote_asr = False
        if REMOTE_WHISPER_CONFIG:
            if _early_mic:
                print(f"  {C_OK}→ 辨識位置自動設為「本機」（麥克風轉錄需要本機 ASR）{RESET}")
            else:
                asr_location = select_asr_location()
                use_remote_asr = (asr_location == "remote")

        if use_remote_asr:
            # ── GPU 伺服器 路徑：固定 Whisper，跳過引擎/場景選擇 ──

            # 伺服器 Whisper 模型選擇（帶快取標籤）
            r_model_name = select_whisper_model_remote(mode)

            # 翻譯引擎（翻譯模式才問）
            translator = None
            meeting_topic = None
            if mode in _TRANSLATE_MODES:
                engine, model, host, port, srv_type = select_translator(mode=mode)
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
                else:
                    if mode in ("zh2en", "ja2zh", "zh2ja"):
                        print(f"{C_HIGHLIGHT}[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB{RESET}",
                              file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                # 非翻譯模式（純轉錄）：仍詢問主題（用於記錄檔命名）
                meeting_topic = _ask_topic()

            # 場景（音訊緩衝長度）
            length_ms, step_ms = select_scene()

            # 錄音
            record, rec_device = _ask_record()

            # 音訊裝置（PortAudio，不是 SDL2）
            capture_id = list_audio_devices_sd()

            _cli_kw = dict(mode=mode, model=r_model_name, device=capture_id,
                           topic=meeting_topic,
                           record=record, rec_device=rec_device,
                           engine=engine if mode in _TRANSLATE_MODES else None,
                           llm_model=model if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           llm_host=f"{host}:{port}" if mode in _TRANSLATE_MODES and engine == "llm" else None,
                           denoise=args.denoise)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            run_stream_remote(capture_id, translator, r_model_name, REMOTE_WHISPER_CONFIG,
                              mode, length_ms=length_ms, step_ms=step_ms,
                              record=record, rec_device=rec_device,
                              force_restart=args.restart_server,
                              meeting_topic=meeting_topic,
                              denoise=args.denoise)
        else:
            # ── 本機路徑：既有流程 ──

            # 英文模式：選擇 ASR 引擎
            if mode in ("en2zh", "en"):
                asr_engine = select_asr_engine()
            else:
                asr_engine = "whisper"

            # Windows: Whisper (SDL2) 無法擷取系統音訊，標記改用 faster-whisper
            _use_local_fw = False
            if IS_WINDOWS and asr_engine == "whisper" and _find_wasapi_loopback():
                _, _probe_path = resolve_model("large-v3-turbo")
                _sdl_devs = _enumerate_sdl_devices(_probe_path)
                if not _sdl_devs:
                    _use_local_fw = True  # 改用 WASAPI + faster-whisper
                    print(f"\n{C_DIM}  SDL2 無法擷取系統音訊，將改用 WASAPI + faster-whisper 本機辨識{RESET}")

            check_dependencies(asr_engine)

            # ASR 模型（緊接在引擎選擇後）
            ms_model_name = None
            model_name = model_path = None
            length_ms = step_ms = None
            if asr_engine == "moonshine":
                ms_model_name = select_moonshine_model()
            else:
                model_name, model_path = select_whisper_model(mode, use_faster_whisper=_use_local_fw)
                length_ms, step_ms = select_scene()

            # 翻譯引擎（翻譯模式才問）
            translator = None
            meeting_topic = None
            s_host, s_port = OLLAMA_HOST, OLLAMA_PORT
            s_server_type = None
            if asr_engine == "moonshine" and mode == "en2zh":
                engine, model, host, port, srv_type = select_translator(mode=mode)
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                    s_host, s_port, s_server_type = host, port, srv_type
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
                else:
                    translator = ArgosTranslator()
            elif asr_engine == "whisper" and mode in _TRANSLATE_MODES:
                engine, model, host, port, srv_type = select_translator(mode=mode)
                meeting_topic = _ask_topic()
                if engine == "llm":
                    translator = OllamaTranslator(model, host, port, direction=mode,
                                                  server_type=srv_type,
                                                  meeting_topic=meeting_topic)
                    s_host, s_port, s_server_type = host, port, srv_type
                elif engine == "nllb":
                    translator = NllbTranslator(direction=mode)
                else:
                    if mode in ("zh2en", "ja2zh", "zh2ja"):
                        print(f"{C_HIGHLIGHT}[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB{RESET}",
                              file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                # 非翻譯模式（純轉錄）：仍詢問主題（用於記錄檔命名）
                engine = "無（直接轉錄）"
                meeting_topic = _ask_topic()

            # 詢問是否錄音（自動偵測錄音裝置）
            record, rec_device = _ask_record(prefer_mix=_early_mic)

            # 詢問是否同時轉錄麥克風
            use_mic = False
            bidi_devs = _early_bidi_devs  # 轉錄模式已提前偵測
            if _early_mic:
                use_mic = True
            elif mode not in _BIDI_MODES and mode not in ("record", "en", "zh", "ja") and asr_engine == "whisper":
                bidi_devs = _detect_bidi_devices()
                if bidi_devs:
                    try:
                        print(f"\n{C_WHITE}是否同時轉錄本機麥克風輸入？（ASR 負載加倍，需考慮主機效能是否足夠）[y/N]{RESET} ", end="", flush=True)
                        _mic_ans = input().strip().lower()
                        use_mic = _mic_ans in ("y", "yes")
                    except (EOFError, KeyboardInterrupt):
                        print()

            # 自動偵測 ASR 裝置
            if asr_engine == "moonshine":
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
                if use_mic and bidi_devs:
                    # --mic 互動模式：切換到雙路架構
                    _mic_lb_id, _, _mic_mic_id, _ = bidi_devs
                    _use_mlx_mic = _is_apple_silicon() and _has_mlx_whisper()
                    # 效能檢查：模型是否適合 faster-whisper/mlx-whisper 雙路
                    _mic_model = model_name
                    _mic_rec_model = _recommended_whisper_model(mode)
                    if not _use_mlx_mic and not _has_local_gpu():
                        _big_models = ("large-v3-turbo", "large-v3", "medium", "medium.en")
                        if model_name in _big_models and _mic_rec_model not in _big_models:
                            print(f"\n{C_WARN}[效能提示] 麥克風轉錄改用 faster-whisper 雙路辨識{RESET}")
                            print(f"  {C_WARN}此裝置無 GPU 加速，{model_name} 辨識可能太慢{RESET}")
                            print(f"  {C_WARN}自動調整為 {_mic_rec_model}{RESET}")
                            _mic_model = _mic_rec_model
                    _need_llm = mode in _TRANSLATE_MODES and engine == "llm"
                    _cli_kw = dict(mode=mode, model=_mic_model,
                                   topic=meeting_topic,
                                   record=record,
                                   engine=engine if mode in _TRANSLATE_MODES else None,
                                   llm_model=model if _need_llm else None,
                                   llm_host=f"{host}:{port}" if _need_llm else None,
                                   mic=True, denoise=args.denoise)
                    if not _confirm_start(_build_cli_command(**_cli_kw)):
                        sys.exit(0)
                    run_stream_bidirectional(_mic_lb_id, _mic_mic_id,
                                             translator, None,
                                             _mic_model, mode,
                                             length_ms=length_ms, step_ms=step_ms,
                                             record=record,
                                             meeting_topic=meeting_topic,
                                             use_mlx=_use_mlx_mic,
                                             mic_translate=False,
                                             denoise=args.denoise)
                elif _use_local_fw:
                    # Windows WASAPI + faster-whisper 本機辨識
                    capture_id = list_audio_devices_sd()
                    _need_llm = mode in _TRANSLATE_MODES and engine == "llm"
                    _cli_kw = dict(mode=mode, model=model_name,
                                   device=capture_id, topic=meeting_topic,
                                   record=record, rec_device=rec_device,
                                   engine=engine if mode in _TRANSLATE_MODES else None,
                                   llm_model=model if _need_llm else None,
                                   llm_host=f"{host}:{port}" if _need_llm else None,
                                   denoise=args.denoise)
                    if not _confirm_start(_build_cli_command(**_cli_kw)):
                        sys.exit(0)
                    run_stream_local_whisper(capture_id, translator, model_name, mode,
                                            length_ms=length_ms, step_ms=step_ms,
                                            record=record, rec_device=rec_device,
                                            meeting_topic=meeting_topic,
                                            denoise=args.denoise)
                else:
                    capture_id = list_audio_devices(model_path)
                    _need_llm = mode in _TRANSLATE_MODES and engine == "llm"
                    _cli_kw = dict(mode=mode, model=model_name,
                                   device=capture_id, topic=meeting_topic,
                                   record=record, rec_device=rec_device,
                                   engine=engine if mode in _TRANSLATE_MODES else None,
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
