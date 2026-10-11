#!/usr/bin/env python3
"""
即時英文語音轉繁體中文字幕
擷取系統播放音訊（macOS 用 ScreenCaptureKit，舊版可用 BlackHole；
Windows 用 WASAPI Loopback），使用 whisper.cpp stream 即時轉錄，
再翻譯成繁體中文。

Author: Jason Cheng (Jason Tools)
"""

import argparse
import atexit
import io
import math
import os
import re
import concurrent.futures
import difflib
import ipaddress
import platform
import unicodedata
import signal
import subprocess
import sys
import shutil
import threading
import time
import wave
import collections
from collections import deque
from functools import lru_cache

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Windows：WebUI 停止時送 CTRL_BREAK。數值函式庫帶進來的 Intel Fortran 執行環境預設會攔下它、直接中止程式
#（「forrtl: error (200): program aborting due to control-BREAK event」），收尾完全不跑：錄音檔的 WAV
# 檔頭停在開頭、最後幾秒還在緩衝區、也不轉 MP3（2026-10-02 實測 5 秒的錄音檔頭寫 0 秒）。
# 必須在載入那些函式庫之前關掉；CTRL_BREAK 改走與 Ctrl+C 相同的收尾（下面的 SIGBREAK 處理）
if IS_WINDOWS:
    os.environ.setdefault("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")


# ── venv 的 Python 版本與建立時不同（2026-10-05）──────────────────────────
# venv 的 python3 指向 /usr/bin/python3 時，作業系統升級（Ubuntu 22.04→24.04 是 3.10→3.12）把它換成新版本，
# 套件卻還在 lib/python<舊版>：每個 import 都失敗、看起來像套件全部消失，服務每 5 秒重啟一次。
# 在任何第三方 import 之前先講清楚原因與修法；結束碼 78 讓 systemd 停止重啟（RestartPreventExitStatus=78）。
# translate_meeting.py、webui.py、remote_whisper_server.py 各一份，逐字相同（tools/test_venv_python_version.py 比對）；
# REST API（python -m jtlw_api）一載入就 import translate_meeting，用的是那一份。
def _venv_python_mismatch():
    """回傳 (建立 venv 時的版本, 現在的版本)；沒有不同、不在 venv 裡、讀不到 pyvenv.cfg 時回傳 None"""
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return None
    built = ""
    try:
        with open(os.path.join(sys.prefix, "pyvenv.cfg"), encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition("=")
                if key.strip() in ("version", "version_info"):
                    built = ".".join(val.strip().split(".")[:2])
    except OSError:
        return None
    now = "%d.%d" % sys.version_info[:2]
    return (built, now) if built and built != now else None


def _exit_if_venv_python_changed(fix):
    mm = _venv_python_mismatch()
    if mm:
        sys.stderr.write(
            f"[錯誤] 這個 venv 是用 Python {mm[0]} 建立的，現在執行的是 Python {mm[1]}"
            f"（作業系統升級換了 Python 版本？），裝好的套件都在 {mm[0]} 的目錄裡，全部無法使用。\n"
            f"       {fix}\n")
        sys.exit(78)


_exit_if_venv_python_changed(
    "請在安裝資料夾執行 " + (r".\install.ps1" if os.name == "nt" else "./install.sh --upgrade"
                            if sys.platform.startswith("linux") else "./install.sh")
    + "：會重建 venv、重新安裝套件")


# ── Windows 的應用程式控制擋下套件的程式檔（2026-10-05，Windows 11 使用者回報）──────────────
# 「DLL load failed while importing _upfirdn_apply: 應用程式控制原則已封鎖此檔案」：Windows 11 的智慧型應用程式控制
# 或公司的應用程式控制原則擋下了 Python 套件裡的 .pyd／.dll。這是 Windows 的安全設定，程式不能也不該繞過，
# 但要講清楚是什麼、怎麼處理，不能只留一串 traceback。
_APP_CONTROL_MARKERS = ("應用程式控制原則", "application control policy", "应用程序控制策略")


def _dll_block_hint(e):
    """這個錯誤是不是 Windows 應用程式控制擋下的；是的話回傳說明，不是回傳空字串"""
    text = f"{e}"
    if not any(m in text.lower() for m in _APP_CONTROL_MARKERS):
        return ""
    folder = os.path.dirname(os.path.abspath(__file__))
    return ("[說明] Windows 的應用程式控制擋下了 Python 套件裡的程式檔（Windows 11 的「智慧型應用程式控制」，"
            "或公司電腦設定的應用程式控制原則）。這是 Windows 的安全設定，本工具無法繞過：\n"
            f"  ・公司電腦：請 IT 把安裝資料夾 {folder} 加入允許清單\n"
            "  ・個人電腦：到「Windows 安全性 → 應用程式與瀏覽器控制 → 智慧型應用程式控制設定」查看；"
            "若是「開啟」，可以改成「關閉」（請先了解關閉後的影響）")


def _excepthook_with_hint(etype, value, tb):
    sys.__excepthook__(etype, value, tb)
    hint = _dll_block_hint(value)
    if hint:
        sys.stderr.write("\n" + hint + "\n")


sys.excepthook = _excepthook_with_hint


def _on_ctrl_break(signum, frame):
    """CTRL_BREAK（WebUI 的停止）交給目前的 Ctrl+C 處理：各模式自己的收尾，沒有的話就是 KeyboardInterrupt"""
    handler = signal.getsignal(signal.SIGINT)
    if callable(handler):
        handler(signal.SIGINT, frame)
    else:
        raise KeyboardInterrupt


if IS_WINDOWS and hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _on_ctrl_break)


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
import http.client
import urllib.error
import urllib.request


# ── Windows＋NVIDIA：讓 CTranslate2 找得到 CUDA 函式庫（2026-10-05）─────────────
# CTranslate2（faster-whisper）用顯示卡時要在執行當下載入 CUDA 12 的 cuBLAS 與 cuDNN 9 的子程式庫，
# 這些**不在顯示卡驅動裡**。CUDA 版 PyTorch 在 torch\lib 自帶一整組、pip 的 nvidia-cublas-cu12／
# nvidia-cudnn-cu12 放在 nvidia\*\bin，但兩者都不在 Windows 找 DLL 的路徑上 → 每一段都是
# 「Library cublas64_12.dll is not found or cannot be loaded」（Windows 10＋RTX 3060 使用者回報）。
# 這裡在載入 ctranslate2 之前把找到的資料夾加進搜尋路徑；cuDNN 的主檔與子程式庫要同一版，
# 主檔先從子程式庫所在的資料夾載入（ctranslate2 自帶一份主檔，不先載入就會跟別處的子程式庫混用）。
# 需要哪些檔從 ctranslate2 自己的 DLL 讀出來，不寫死 CUDA 版本。
_WIN_CUDA = {"checked": False, "dirs": {}, "missing": [], "handles": [], "loaded": False}


def _win_cuda_candidates():
    """可能放著 cuBLAS／cuDNN 的資料夾，依優先順序：CUDA 版 PyTorch 自帶的整組、pip 的 nvidia-*、CUDA Toolkit"""
    import glob
    import site
    import sysconfig
    sps = []
    for p in [sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib")] + \
            list(getattr(site, "getsitepackages", lambda: [])()):
        if p and os.path.isdir(p) and p not in sps:
            sps.append(p)
    out = [os.path.join(sp, "torch", "lib") for sp in sps]
    for sp in sps:
        out += sorted(glob.glob(os.path.join(sp, "nvidia", "*", "bin")))
    for k, v in sorted(os.environ.items()):
        if v and (k.upper() == "CUDA_PATH" or k.upper().startswith("CUDA_PATH_V")):
            out.append(os.path.join(v, "bin"))
    seen = []
    for d in out:
        if os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


def _ct2_cuda_dll_names():
    """ctranslate2 執行時要載入的 cuBLAS／cuDNN 檔名（從它的 DLL 讀出來）與它自帶的 DLL；找不到套件時回傳 ([], set())"""
    import importlib.util
    spec = importlib.util.find_spec("ctranslate2")
    if not spec or not spec.submodule_search_locations:
        return [], set()
    d = list(spec.submodule_search_locations)[0]
    dlls = [f for f in os.listdir(d) if f.lower().endswith(".dll")]
    names = set()
    for f in dlls:
        with open(os.path.join(d, f), "rb") as fh:
            names |= {m.decode().lower() for m in re.findall(rb"(?:cublas|cudnn)[A-Za-z_]*64_\d+\.dll", fh.read(), re.I)}
    return sorted(names), {f.lower() for f in dlls}


def _win_cuda_dll_setup():
    """找齊 ctranslate2 要的 cuBLAS／cuDNN、加進搜尋路徑、先載入 cuDNN 主檔。只在 Windows 而且有 NVIDIA 驅動時做"""
    if _WIN_CUDA["checked"]:
        return
    _WIN_CUDA["checked"] = True
    if not IS_WINDOWS:
        return
    if not os.path.isfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll")):
        return                                           # 沒有 NVIDIA 驅動：用不到顯示卡，什麼都不做
    try:
        need, bundled = _ct2_cuda_dll_names()
    except Exception:
        return
    cands = _win_cuda_candidates()
    for group in ("cublas", "cudnn"):
        names = [n for n in need if n.startswith(group)]
        if not names:
            continue
        # 同一組要從同一個資料夾找齊（版本才一致）；ctranslate2 自帶的 cuDNN 主檔可以不在那裡
        must = [n for n in names if not (group == "cudnn" and n in bundled and re.fullmatch(r"cudnn64_\d+\.dll", n))]
        d = next((c for c in cands if all(os.path.isfile(os.path.join(c, n)) for n in must)), None)
        if d is None:
            _WIN_CUDA["missing"] += must
            continue
        already = any(v[0] == d for v in _WIN_CUDA["dirs"].values())   # cuBLAS 與 cuDNN 常在同一個資料夾
        _WIN_CUDA["dirs"][group] = (d, names)
        if already:
            continue
        if d.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        try:
            _WIN_CUDA["handles"].append(os.add_dll_directory(d))
        except (AttributeError, OSError):
            pass
    shim = [n for n in need if re.fullmatch(r"cudnn64_\d+\.dll", n)]
    if "cudnn" in _WIN_CUDA["dirs"] and shim:
        d = _WIN_CUDA["dirs"]["cudnn"][0]
        if os.path.isfile(os.path.join(d, shim[0])):
            try:
                import ctypes
                ctypes.WinDLL(os.path.join(d, shim[0]))
            except OSError:
                pass


def _win_cuda_libs_ok():
    """要用顯示卡之前：ctranslate2 要的 CUDA 函式庫是否都載得到（先從找到的資料夾載入，之後 ctranslate2
    用檔名載入時拿到的就是這幾個）。回傳 (是否可用, 缺少的檔名)"""
    _win_cuda_dll_setup()
    if not IS_WINDOWS:
        return True, []
    if _WIN_CUDA["missing"]:
        return False, list(_WIN_CUDA["missing"])
    if not _WIN_CUDA["loaded"]:
        import ctypes
        failed = []
        order = {"cublaslt": 0, "cublas": 1, "cudnn_graph": 2, "cudnn64": 3}
        for group, (d, names) in _WIN_CUDA["dirs"].items():
            for n in sorted(names, key=lambda x: min((v for k, v in order.items() if x.startswith(k)), default=9)):
                p = os.path.join(d, n)
                if not os.path.isfile(p):
                    continue
                try:
                    ctypes.WinDLL(p)
                except OSError as e:
                    failed.append(f"{n}（{e}）")
        if failed:
            _WIN_CUDA["missing"] = failed
            return False, failed
        _WIN_CUDA["loaded"] = True
    return True, []


_win_cuda_dll_setup()

import ctranslate2
import sentencepiece

# OpenCC 簡體→台灣繁體轉換（用於 ASR 辨識結果）
try:
    from opencc import OpenCC as _OpenCC
    S2TWP = _OpenCC("s2twp")
    _T2S = _OpenCC("t2s")
    _S2T = _OpenCC("s2t")
except ImportError:
    S2TWP = type("_S2TWProxy", (), {"convert": staticmethod(lambda text: text)})()
    _T2S = _S2T = None


def _looks_simplified(text):
    """整段判斷文字是否為簡體。

    繁體字轉成簡體後一定會有變化；若「繁→簡」後與原文完全相同，
    代表原文本來就是簡體。用整段而非逐字判斷，是因為「干」「后」「里」
    這類簡繁共用字逐字判斷會誤判（例如正確繁體的「干擾」會被當成簡體）。"""
    if not text or _T2S is None:
        return False
    return _T2S.convert(text) == text and _S2T.convert(text) != text


# s2twp 對「已經是繁體」的輸入會誤轉：「干擾」→「幹擾」、「干預」→「幹預」
# （干 既是繁體字、也是幹／乾的簡體形，字級轉換無法分辨）。
# 但對簡體輸入它是對的（「干扰」→「干擾」），而 _to_traditional() 的整段偵測
# 又會漏掉繁簡混雜的行。解法：照常轉換，然後把**原文裡本來就有的正確寫法**還原。
# 只還原原文字面上存在的詞，所以不會把真正該轉的簡體留下來。
_S2TWP_PROTECT = ("干擾", "干涉", "干預", "干戈", "干支", "干係", "干犯",
                  "若干", "相干", "干政", "干練", "干雲")


_CJK_FOR_PUNCT = re.compile(r"[㐀-鿿豈-﫿]")
_HALF_TO_FULL_PUNCT = {",": "，", ".": "。", "?": "？", "!": "！",
                       ":": "：", ";": "；"}


def _cjk_punct_normalize(text):
    """中文句子裡的半形標點轉全形（台灣的文件一律用全形）。

    ASR 模型吐出來的中文標點是半形的（「哈囉大家好,歡迎收聽」），
    先前只有 `standard` 校正時 LLM 會順手改掉，`punctuation_only` 反而讓它現形
    ——與簡繁那件是同一個形狀（2026-09-21 由 JTDT 實測指出）。

    **判斷依據是「前一個字或後一個字是中日文」**，不可以無條件轉：
      大家好,歡迎     → 前後都是中文，轉
      那on the side,我 → 前面是英文但**後面是中文**，一樣要轉
                        （中英夾雜時逗號分隔的仍是中文子句）
      GPT3.0出來      → 後面是數字，不轉（否則變成 GPT3。0）
      1,200 元        → 後面是數字，不轉
      Cloud, Inc.     → 前後都不是中日文，不轉
      Good morning.   → 同上，純英文完全不動
    """
    if not text:
        return text
    out = []
    n = len(text)
    for i, ch in enumerate(text):
        if ch in _HALF_TO_FULL_PUNCT and (
                (i > 0 and _CJK_FOR_PUNCT.match(text[i - 1]))
                or (i + 1 < n and _CJK_FOR_PUNCT.match(text[i + 1]))):
            out.append(_HALF_TO_FULL_PUNCT[ch])
        else:
            out.append(ch)
    return "".join(out)


def _s2twp_safe(text):
    """簡繁轉換，並保護那些會被 s2twp 誤轉的正確繁體詞；順便把中文標點轉全形"""
    out = S2TWP.convert(text)
    for w in _S2TWP_PROTECT:
        if w in text:
            wrong = S2TWP.convert(w)
            if wrong != w:
                out = out.replace(wrong, w)
    return _cjk_punct_normalize(out)


def _to_traditional(text):
    """確保輸出為台灣繁體：偵測到簡體才轉換。

    LLM 翻譯結果原本完全不做轉換（靠 prompt 控制），但模型偶爾仍會吐簡體，
    出現後沒有任何機制攔得住。這裡改為先偵測：
    - 已是繁體 → 原樣返回，避免 OpenCC 把正確的「干擾」誤轉成「幹擾」
    - 確認是簡體 → 套 s2twp，同時取得台灣用語轉換（内存→記憶體、程序→程式）"""
    if _looks_simplified(text):
        return _s2twp_safe(text)
    return text

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
# macOS ScreenCaptureKit（零設定擷取系統播放音訊，macOS 13+）
SCK_LOOPBACK_ID = -300     # sentinel，表示使用 ScreenCaptureKit
SCK_MIXED_ID = -400        # sentinel，表示 macOS 混合錄音（ScreenCaptureKit + 麥克風）
# Linux PipeWire / PulseAudio 監聽來源（零設定擷取系統播放音訊）
PULSE_LOOPBACK_ID = -500   # sentinel，表示使用預設喇叭的 monitor 來源
PULSE_MIXED_ID = -600      # sentinel，表示 Linux 混合錄音（monitor + 麥克風）
_MIXED_REC_IDS = (WASAPI_MIXED_ID, SCK_MIXED_ID, PULSE_MIXED_ID)
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
C_KO = "\x1b[38;2;255;140;200m"       # 粉紅 - 韓文（v2.22.0）
C_MY_KO = "\x1b[38;2;255;185;225m"   # 淡粉 - 我方韓文（雙向模式）
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
_LOOPBACK_LABEL = ("WASAPI Loopback" if IS_WINDOWS
                   else "PipeWire / PulseAudio 系統音訊" if IS_LINUX
                   else "BlackHole 2ch")
_START_CMD = ".\\start.ps1" if IS_WINDOWS else "./start.sh"
_INSTALL_CMD = ".\\install.ps1" if IS_WINDOWS else "./install.sh"


_overlay_proc_ref = None  # 全域參照，供 _force_exit 使用

def _force_exit(code=0):
    """強制結束程序。一律用 os._exit() 避免卡在 C 擴展（CTranslate2/MLX Metal）。
    signal handler 在呼叫前已完成音訊裝置清理。"""
    # 結束懸浮字幕子程序（os._exit 不會觸發 atexit）
    global _overlay_proc_ref
    _webui_flush()                      # os._exit 也不會跑 atexit 的送完事件
    if _interp_modules or _interp_vmic:  # 雙向口譯建的虛擬麥克風（atexit 不會跑，第二次 Ctrl+C／WebUI 強制停止走這裡）
        try:
            _interp_linux_cleanup()
        except Exception:
            pass
    if _overlay_proc_ref is not None:
        try:
            _overlay_proc_ref.terminate()
        except Exception:
            pass
        _overlay_proc_ref = None
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
                or "what u hear" in n or "wave out" in n
                or "立體聲混音" in n or "立体声混音" in n)     # 中文版 Windows 的 Stereo Mix
    if IS_LINUX:
        return "monitor" in n or "loopback" in n
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
        mic_id = _find_mac_mic()
        # 優先 ScreenCaptureKit（零設定），未授權或舊系統才退回 BlackHole
        if _sck_available() and mic_id is not None:
            return (SCK_LOOPBACK_ID, "ScreenCaptureKit 系統音訊",
                    mic_id, sd.query_devices(mic_id)["name"])
        lb_id = _find_blackhole_device()
        if lb_id is not None and mic_id is not None:
            return (lb_id, sd.query_devices(lb_id)["name"],
                    mic_id, sd.query_devices(mic_id)["name"])
    elif IS_LINUX:
        mic_id = _find_default_mic()
        if _pulse_available() and mic_id is not None:
            return (PULSE_LOOPBACK_ID, _pulse_label(),
                    mic_id, sd.query_devices(mic_id)["name"])
    return None



def _bidi_device_name(dev_id):
    """雙向模式裝置代號 → 顯示名稱（含 WASAPI／SCK／PulseAudio 的 sentinel）"""
    if dev_id == WASAPI_LOOPBACK_ID:
        return "WASAPI Loopback"
    if dev_id == SCK_LOOPBACK_ID:
        return "ScreenCaptureKit 系統音訊"
    if dev_id == PULSE_LOOPBACK_ID:
        return _pulse_label()
    import sounddevice as sd
    return sd.query_devices(dev_id)["name"]


def _bidi_apply_device_args(bidi, device=None, mic_device=None):
    """把使用者指定的 -d（系統音訊）與 --mic-device（麥克風）套到雙向模式的自動偵測結果上。
    v2.22.1 前雙向模式只看 --mic-device，-d 完全被忽略（WebUI 選了 BlackHole 仍用 SCK）。
    兩個都指定時，自動偵測失敗（bidi 為 None）也照樣可用。"""
    if bidi is None:
        if device is None or mic_device is None:
            return None
        bidi = (None, None, None, None)
    lb_id, lb_name, mic_id, mic_name = bidi
    if device is not None:
        lb_id, lb_name = device, _bidi_device_name(device)
    if mic_device is not None:
        mic_id, mic_name = mic_device, _bidi_device_name(mic_device)
    return (lb_id, lb_name, mic_id, mic_name)

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


# ── macOS ScreenCaptureKit 系統音訊擷取 ─────────────────────────
# 由 sck_audio_capture.swift 編譯出的 helper 取得系統播放音訊，
# 不需要 BlackHole 虛擬裝置與多重輸出裝置，使用者也不必改變輸出裝置。
# 需要 macOS 13+ 與「螢幕錄製」權限（SCK 即使只取音訊也歸在此權限）。

_SCK_SAMPLERATE = 48000
_SCK_CHANNELS = 2
_SCK_FORCE_OFF = False  # --audio-source blackhole 時停用 SCK
_SCK_SRC_NAME = "sck_audio_capture.swift"
_SCK_BIN_NAME = "jt-sck-audio"


def _sck_source_path():
    return os.path.join(SCRIPT_DIR, _SCK_SRC_NAME)


def _sck_binary_path():
    return os.path.join(SCRIPT_DIR, "bin", _SCK_BIN_NAME)


def _sck_macos_ok():
    """macOS 版本是否支援 ScreenCaptureKit 音訊擷取（13.0+）"""
    if not IS_MACOS:
        return False
    try:
        import platform
        ver = platform.mac_ver()[0]
        return int(ver.split(".")[0]) >= 13
    except Exception:
        return False


def _sck_source_hash():
    try:
        import hashlib
        with open(_sck_source_path(), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return ""


def _sck_build(verbose=True):
    """需要時編譯 SCK helper。回傳可執行檔路徑或 None。
    以原始碼 hash 判斷是否需要重編，避免每次啟動都花時間。"""
    if not _sck_macos_ok() or not os.path.isfile(_sck_source_path()):
        return None
    binary = _sck_binary_path()
    stamp = os.path.join(os.path.dirname(binary), f".{_SCK_BIN_NAME}.hash")
    src_hash = _sck_source_hash()
    if os.path.isfile(binary) and src_hash:
        try:
            with open(stamp, "r", encoding="utf-8") as f:
                if f.read().strip() == src_hash:
                    return binary
        except Exception:
            pass
    import shutil
    if not shutil.which("swiftc"):
        if verbose:
            print(f"  {C_HIGHLIGHT}[系統音訊] 找不到 swiftc，無法編譯 ScreenCaptureKit 元件{RESET}")
            print(f"  {C_DIM}請安裝 Xcode Command Line Tools：xcode-select --install{RESET}")
        return None
    if verbose:
        print(f"  {C_DIM}[系統音訊] 首次使用 ScreenCaptureKit，編譯元件中（約 1 分鐘）...{RESET}")
    os.makedirs(os.path.dirname(binary), exist_ok=True)
    import platform
    cmd = [
        "swiftc", "-O",
        "-target", f"{platform.machine()}-apple-macos13.0",
        "-o", binary, _sck_source_path(),
        "-framework", "ScreenCaptureKit", "-framework", "AVFoundation",
        "-framework", "CoreMedia", "-framework", "CoreGraphics",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        if verbose:
            print(f"  {C_HIGHLIGHT}[系統音訊] 編譯失敗: {e}{RESET}")
        return None
    if r.returncode != 0 or not os.path.isfile(binary):
        if verbose:
            print(f"  {C_HIGHLIGHT}[系統音訊] 編譯失敗{RESET}")
            err = (r.stderr or "").strip().splitlines()
            for line in err[-5:]:
                print(f"  {C_DIM}{line}{RESET}")
        return None
    try:
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(src_hash)
    except Exception:
        pass
    if verbose:
        print(f"  {C_OK}[系統音訊] ScreenCaptureKit 元件編譯完成{RESET}")
    return binary


def _sck_check(build=True, verbose=False):
    """查詢 SCK 能力與權限，回傳 dict(available, permission, macos) 或 None。"""
    binary = _sck_binary_path()
    if not os.path.isfile(binary):
        if not build:
            return None
        binary = _sck_build(verbose=verbose)
        if not binary:
            return None
    else:
        # 原始碼有更新時重編
        rebuilt = _sck_build(verbose=verbose) if build else binary
        binary = rebuilt or binary
    try:
        r = subprocess.run([binary, "--check"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.strip())
    except Exception:
        return None


@lru_cache(maxsize=1)
def _sck_supported():
    """SCK 元件是否可用（macOS 版本 + 元件編譯成功），不含權限判斷。"""
    if not _sck_macos_ok():
        return False
    info = _sck_check(build=False)
    return bool(info and info.get("available"))


def _sck_permission():
    """是否已取得「螢幕錄製」權限（每次查詢，使用者可能中途授權）。"""
    if not _sck_macos_ok():
        return False
    info = _sck_check(build=False)
    return bool(info and info.get("permission"))


def _sck_available():
    """SCK 是否可直接使用（元件就緒 + 已授權 + 未被 --audio-source 停用）"""
    if _SCK_FORCE_OFF:
        return False
    return _sck_supported() and _sck_permission()


def _sck_request_permission():
    """觸發系統「螢幕錄製」授權對話框，回傳是否已授權。"""
    binary = _sck_binary_path()
    if not os.path.isfile(binary):
        binary = _sck_build()
        if not binary:
            return False
    try:
        r = subprocess.run([binary, "--request"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


_SCK_TERMINAL_NAMES = {
    "Apple_Terminal": "終端機 (Terminal)",
    "iTerm.app": "iTerm2",
    "ghostty": "Ghostty",
    "WarpTerminal": "Warp",
    "vscode": "Visual Studio Code",
    "Hyper": "Hyper",
    "WezTerm": "WezTerm",
    "kitty": "kitty",
    "alacritty": "Alacritty",
    "Tabby": "Tabby",
}


def _sck_terminal_app_name():
    """回傳需要授權的程式名稱。
    macOS 把「螢幕錄製」權限授予啟動本程式的終端機 / IDE，不是 python 本身，
    使用者常在設定頁找不到該勾誰，所以這裡直接指名。"""
    term = os.environ.get("TERM_PROGRAM", "")
    return _SCK_TERMINAL_NAMES.get(term, term or "你用來執行本程式的終端機程式")


def _sck_open_privacy_settings():
    """直接開啟「系統設定 → 隱私權與安全性 → 螢幕錄製」頁面"""
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security"
                     "?Privacy_ScreenCapture"],
            check=False, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _sck_permission_hint(interactive=None):
    """權限未授予時的引導（CLI 用）。
    互動終端下直接詢問是否跳出授權對話框，被拒或曾拒絕過則改開系統設定頁。
    回傳是否已在本次取得授權（仍需重啟終端機才會生效）。"""
    app = _sck_terminal_app_name()
    print(f"\n  {C_HIGHLIGHT}[系統音訊] 需要「螢幕錄製」權限才能擷取系統聲音{RESET}")
    print(f"  {C_DIM}ScreenCaptureKit 只取音訊、不會擷取畫面，但 macOS 將其歸在此權限之下。{RESET}")
    print(f"  {C_DIM}授權對象是「{app}」，不是 Python。{RESET}")

    if interactive is None:
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if interactive:
        try:
            ans = input(f"  {C_WHITE}現在開啟授權對話框？(Y/n)：{RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("", "y", "yes"):
            if _sck_request_permission():
                print(f"  {C_OK}已取得授權 — 請完全結束「{app}」（Cmd+Q）再重新開啟，"
                      f"然後重跑一次{RESET}\n")
                return True
            print(f"  {C_DIM}系統沒有跳出對話框（多半是先前按過拒絕），改為開啟設定頁{RESET}")
            _sck_open_privacy_settings()

    print(f"  {C_WHITE}請到「系統設定 → 隱私權與安全性 → 螢幕錄製」勾選「{app}」，{RESET}")
    print(f"  {C_WHITE}再完全結束「{app}」（Cmd+Q）並重新開啟，權限才會生效。{RESET}")
    print(f"  {C_DIM}（也可隨時執行 ./start.sh --sck-permission 重新授權）{RESET}")
    print(f"  {C_DIM}（或改用 BlackHole：{_INSTALL_CMD} 會協助安裝，適用 macOS 12 以下）{RESET}\n")
    return False


def _is_sys_audio_device(device_id):
    """是否為「系統播放音訊」的擷取 sentinel（WASAPI Loopback / ScreenCaptureKit）"""
    return ((IS_WINDOWS and device_id == WASAPI_LOOPBACK_ID)
            or (IS_MACOS and device_id == SCK_LOOPBACK_ID)
            or (IS_LINUX and device_id == PULSE_LOOPBACK_ID))


def _sys_audio_loopback_id():
    """目前平台的系統音訊擷取 sentinel"""
    if IS_MACOS:
        return SCK_LOOPBACK_ID
    if IS_LINUX:
        return PULSE_LOOPBACK_ID
    return WASAPI_LOOPBACK_ID


def _capture_stream_info(device_id, cap_channels=2):
    """回傳擷取裝置的 (samplerate, channels)。
    支援 WASAPI Loopback / ScreenCaptureKit 兩個 sentinel，其餘查 sounddevice。
    cap_channels=None 表示不限制聲道數（錄音用，保留原始聲道）。"""
    def _cap(ch):
        # cap_channels=None（錄音用）保留原始聲道數，但至少 1 聲道
        return max(int(ch), 1) if cap_channels is None else min(int(ch), cap_channels)

    if IS_WINDOWS and device_id == WASAPI_LOOPBACK_ID:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            return int(wb_info["defaultSampleRate"]), _cap(wb_info["maxInputChannels"])
    if IS_MACOS and device_id == SCK_LOOPBACK_ID:
        return _SCK_SAMPLERATE, _cap(_SCK_CHANNELS)
    if IS_LINUX and device_id == PULSE_LOOPBACK_ID:
        return _PULSE_SAMPLERATE, _cap(_PULSE_CHANNELS)
    import sounddevice as sd
    dev_info = sd.query_devices(device_id)
    return int(dev_info["default_samplerate"]), _cap(dev_info["max_input_channels"])


def _open_capture_stream(device_id, callback, samplerate, channels, blocksize,
                         dtype="float32"):
    """依裝置 ID 建立音訊輸入串流，介面一致（start/stop/close）。
    WASAPI Loopback / ScreenCaptureKit 走各自的包裝類別，其餘走 sd.InputStream。"""
    if IS_WINDOWS and device_id == WASAPI_LOOPBACK_ID:
        return _WasapiLoopbackStream(
            callback=callback, samplerate=samplerate,
            channels=channels, blocksize=blocksize)
    if IS_MACOS and device_id == SCK_LOOPBACK_ID:
        return _SCKLoopbackStream(
            callback=callback, samplerate=samplerate,
            channels=channels, blocksize=blocksize)
    if IS_LINUX and device_id == PULSE_LOOPBACK_ID:
        return _PulseLoopbackStream(
            callback=callback, samplerate=samplerate,
            channels=channels, blocksize=blocksize)
    import sounddevice as sd
    return sd.InputStream(
        device=device_id, samplerate=samplerate, channels=channels,
        blocksize=blocksize, dtype=dtype, callback=callback)


def _no_audio_hint(device_id):
    """收不到音訊時的排查提示（依實際擷取來源給對應說明）"""
    if IS_MACOS and device_id == SCK_LOOPBACK_ID:
        return ("請確認系統喇叭正在播放聲音；"
                "系統若設為靜音，ScreenCaptureKit 只會收到無聲訊號")
    if IS_WINDOWS and device_id == WASAPI_LOOPBACK_ID:
        return "請確認系統喇叭正在播放聲音，並檢查 WASAPI Loopback 裝置是否正確"
    if IS_LINUX and device_id == PULSE_LOOPBACK_ID:
        return ("請確認系統喇叭正在播放聲音，且播放到預設輸出裝置"
                "（pactl get-default-sink）；喇叭靜音時 monitor 可能只收到無聲訊號")
    return "請確認系統喇叭正在播放聲音，並檢查所選音訊裝置是否正確"


_WHISPER_INPUT_SR = 16000   # Whisper 固定輸入取樣率


def _mlx_input(wav_path):
    """回傳 mlx-whisper 的音訊輸入。

    mlx-whisper 拿到「檔案路徑」時，內部會為每一段音訊 spawn 一次 ffmpeg 解碼；
    即時模式每 3 秒就一段，這個子程序成本可觀，且 ffmpeg 一壞整條即時辨識就停擺。
    這裡改成自行用 wave + scipy 解出 16kHz 單聲道 float32 ndarray 餵進去
    （mlx-whisper 的 audio 參數本來就接受 ndarray），資料內容與 ffmpeg 解出的相同。
    任何一步失敗就退回原本的檔案路徑，讓 mlx-whisper 照舊走 ffmpeg。"""
    try:
        import numpy as _np
        from math import gcd as _gcd
        from scipy.signal import resample_poly as _resample_poly

        with wave.open(wav_path, "r") as _wf:
            _sr = _wf.getframerate()
            _ch = _wf.getnchannels()
            if _wf.getsampwidth() != 2:
                return wav_path
            _raw = _wf.readframes(_wf.getnframes())
        _audio = _np.frombuffer(_raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        if _ch > 1:
            _audio = _audio.reshape(-1, _ch).mean(axis=1)
        if _sr != _WHISPER_INPUT_SR:
            _g = _gcd(int(_sr), _WHISPER_INPUT_SR)
            _audio = _resample_poly(_audio, _WHISPER_INPUT_SR // _g, int(_sr) // _g)
        return _np.ascontiguousarray(_audio, dtype=_np.float32)
    except Exception:
        return wav_path


class _SCKLoopbackStream:
    """包裝 ScreenCaptureKit helper，介面對齊 sd.InputStream。
    callback 簽名：(numpy_array, frames, time_info, status)"""

    def __init__(self, callback, samplerate=_SCK_SAMPLERATE, channels=_SCK_CHANNELS,
                 blocksize=None, dtype="float32"):
        import numpy as np
        self._callback = callback
        self._samplerate = int(samplerate)
        self._channels = int(channels)
        self._blocksize = int(blocksize or self._samplerate * 0.1)
        self._np = np
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._stderr_tail = deque(maxlen=10)
        self._binary = _sck_binary_path()
        if not os.path.isfile(self._binary):
            built = _sck_build()
            if not built:
                raise RuntimeError("ScreenCaptureKit 元件無法編譯")
            self._binary = built

    def _reader(self):
        chunk_bytes = self._blocksize * self._channels * 4
        np = self._np
        stdout = self._proc.stdout
        # 注意：bufsize=0 的 pipe，read(n) 只保證「最多 n 位元組」（helper 一次寫
        # 960 frames = 7680 bytes），必須自行累積滿一個 block 才送出，
        # 不可補零湊滿 —— 否則會憑空插入靜音、音訊長度暴增數倍。
        pending = b""
        while not self._stop.is_set():
            try:
                piece = stdout.read(chunk_bytes - len(pending))
            except Exception:
                break
            if not piece:
                break  # EOF：不足一個 block 的尾端直接捨棄
            pending += piece
            if len(pending) < chunk_bytes:
                continue
            audio = np.frombuffer(pending, dtype=np.float32).reshape(-1, self._channels)
            pending = b""
            if self._callback and not self._stop.is_set():
                try:
                    self._callback(audio, audio.shape[0], None, None)
                except Exception:
                    pass

    def _drain_stderr(self):
        for line in iter(self._proc.stderr.readline, b""):
            try:
                self._stderr_tail.append(line.decode("utf-8", "replace").strip())
            except Exception:
                pass

    def _command(self):
        return [self._binary, "--rate", str(self._samplerate),
                "--channels", str(self._channels)]

    def start(self):
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self._command(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # helper 啟動失敗（多半是權限問題）時立刻回報，不要讓使用者空等
        time.sleep(0.6)
        if self._proc.poll() is not None:
            msg = "; ".join(self._stderr_tail) or f"helper 結束（exit {self._proc.returncode}）"
            self._proc = None
            raise RuntimeError(msg)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def close(self):
        self.stop()
        for pipe in ("stdout", "stderr"):
            try:
                getattr(self._proc, pipe).close()
            except Exception:
                pass
        self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
        self.close()


# ── Linux PipeWire / PulseAudio 系統音訊擷取 ─────────────────────
# 從「預設喇叭」的 monitor 來源錄音：不需虛擬音效卡、不必改輸出裝置。
# PipeWire（Ubuntu 22.10+ 預設）與傳統 PulseAudio 都提供 monitor；
# 優先用 parec（pulseaudio-utils），沒有時退回 pw-record（pipewire-bin）。

_PULSE_SAMPLERATE = 48000
_PULSE_CHANNELS = 2
_pulse_cache = {"t": -1e9, "info": None}


def _pulse_capture_tool():
    """回傳可用的擷取工具（'parec' / 'pw-record'）或 None"""
    if not IS_LINUX:
        return None
    import shutil
    for tool in ("parec", "pw-record"):
        if shutil.which(tool):
            return tool
    return None


def _pulse_cmd_output(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _pulse_monitor_source():
    """找出預設喇叭的 monitor 來源，回傳 dict(source, sink, desc) 或 None。
    可用環境變數 JTLW_MONITOR_SOURCE 或 config.json 的 linux_monitor_source 指定來源。
    結果快取 2 秒（使用者可能中途切換輸出裝置）。"""
    if not IS_LINUX:
        return None
    now = time.monotonic()
    if now - _pulse_cache["t"] < 2:
        return _pulse_cache["info"]
    info = None
    import shutil
    if shutil.which("pactl"):
        sources = []
        for line in _pulse_cmd_output(["pactl", "list", "short", "sources"]).splitlines():
            cols = line.split("\t")
            if len(cols) > 1:
                sources.append(cols[1])
        want = (os.environ.get("JTLW_MONITOR_SOURCE")
                or _config.get("linux_monitor_source") or "")
        sink = _pulse_cmd_output(["pactl", "get-default-sink"]).strip()
        if not sink:
            for line in _pulse_cmd_output(["pactl", "info"]).splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip()
        if want and want in sources:
            src = want
        elif sink and f"{sink}.monitor" in sources:
            src = f"{sink}.monitor"
        else:
            src = next((x for x in sources if x.endswith(".monitor")), "")
        if src:
            sink_name = src[:-len(".monitor")] if src.endswith(".monitor") else src
            desc = sink_name
            # 取喇叭的人類可讀名稱（例如「Built-in Audio Analog Stereo」）
            _cur = None
            for line in _pulse_cmd_output(["pactl", "list", "sinks"]).splitlines():
                line = line.strip()
                if line.startswith("Name:"):
                    _cur = line.split(":", 1)[1].strip()
                elif line.startswith("Description:") and _cur == sink_name:
                    desc = line.split(":", 1)[1].strip()
                    break
            info = {"source": src, "sink": sink_name, "desc": desc}
    elif shutil.which("pw-record"):
        # 純 PipeWire、沒有 pactl：由 pw-record 直接錄預設喇叭
        info = {"source": "", "sink": "", "desc": "預設喇叭"}
    _pulse_cache["t"] = now
    _pulse_cache["info"] = info
    return info


def _pulse_available():
    """Linux 是否能直接擷取系統播放音訊（有擷取工具 + 找得到 monitor 來源）"""
    return bool(IS_LINUX and _pulse_capture_tool() and _pulse_monitor_source())


def _pulse_label():
    info = _pulse_monitor_source() or {}
    desc = info.get("desc") or ""
    return f"系統音訊（{desc}）" if desc else "系統音訊（PipeWire / PulseAudio）"


def _pulse_missing_hint():
    """Linux 找不到系統音訊來源時的排查說明。沒有桌面工作階段（伺服器、PVE LXC、SSH）時直接講清楚：
    這種環境本來就沒有音訊，叫人裝 pulseaudio-utils 或檢查 pactl 都沒用（2026-10-08 使用者在 PVE LXC 開即時模式）"""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return ("這台沒有桌面工作階段（伺服器、容器或 SSH 連線），沒有系統音訊可以擷取。"
                "即時字幕要在開會用的電腦（有喇叭與麥克風）上執行；這台請改用「讀入音訊檔案」離線處理")
    if not _pulse_capture_tool():
        return "請安裝 pulseaudio-utils（sudo apt install pulseaudio-utils）以擷取系統音訊"
    return ("找不到 PipeWire / PulseAudio 的 monitor 來源；"
            "請確認音訊伺服器正在執行（pactl info）且有輸出裝置")


class _PulseLoopbackStream(_SCKLoopbackStream):
    """以 parec / pw-record 擷取預設喇叭的 monitor，介面對齊 sd.InputStream。
    讀取、累積、停止邏輯沿用 _SCKLoopbackStream（同樣是 float32 interleaved pipe）。"""

    def __init__(self, callback, samplerate=_PULSE_SAMPLERATE, channels=_PULSE_CHANNELS,
                 blocksize=None, dtype="float32"):
        import numpy as np
        self._callback = callback
        self._samplerate = int(samplerate)
        self._channels = int(channels)
        self._blocksize = int(blocksize or self._samplerate * 0.1)
        self._np = np
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._stderr_tail = deque(maxlen=10)
        self._tool = _pulse_capture_tool()
        self._info = _pulse_monitor_source()
        if not self._tool or not self._info:
            raise RuntimeError(_pulse_missing_hint())

    def _command(self):
        rate, ch = str(self._samplerate), str(self._channels)
        if self._tool == "parec" and self._info.get("source"):
            return ["parec", "--raw", "--format=float32le", f"--rate={rate}",
                    f"--channels={ch}", "--latency-msec=50",
                    "--client-name=jt-live-whisper", "-d", self._info["source"]]
        cmd = ["pw-record", "--format", "f32", "--rate", rate, "--channels", ch,
               "-P", "{ stream.capture.sink=true node.name=jt-live-whisper }"]
        if self._info.get("sink"):
            cmd += ["--target", self._info["sink"]]
        return cmd + ["-"]


# LLM 伺服器設定（預設無，由 config.json 或 --llm-host 指定）
OLLAMA_DEFAULT_HOST = None
OLLAMA_DEFAULT_PORT = 11434
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


_CONFIG_LOAD_ERROR = [None]       # 設定檔讀不懂的原因；有值時這個程序不寫回設定檔
_CONFIG_SAVE_WARNED = [False]


def load_config():
    """讀取設定檔，回傳 dict。

    讀不懂（手動編輯多一個逗號、寫到一半的檔案）時以前默默當成空設定：GPU 伺服器、LLM 主機都不生效，
    之後任何一次互動選擇呼叫 save_config 還會把整個檔案覆寫成幾乎空白，設定永久遺失（2026-10-05）。
    現在說明哪裡讀不懂、這次用預設值執行，而且不寫回這個檔案"""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            if not isinstance(data, dict):
                raise ValueError("最外層不是 { ... } 物件")
            return data
        except Exception as e:
            _CONFIG_LOAD_ERROR[0] = e
            sys.stderr.write(
                f"[錯誤] 設定檔讀不懂：{CONFIG_PATH}（{type(e).__name__}: {e}）\n"
                "       這次先用預設設定執行（GPU 伺服器、LLM 主機等設定這次不會生效），也不會覆寫這個檔案。\n"
                "       請修正這個檔案；或把它改名保留，再重新執行安裝程式產生新的。\n")
    return {}


def save_config(cfg):
    """儲存設定檔：先寫暫存檔再換上（寫到一半當掉不會留下壞掉的檔案），保留原本的權限（裡面有密碼與 token）。
    設定檔一開始就讀不懂時不寫：寫了就是用幾乎空白的設定蓋掉使用者原本的檔案"""
    if _CONFIG_LOAD_ERROR[0] is not None:
        if not _CONFIG_SAVE_WARNED[0]:
            _CONFIG_SAVE_WARNED[0] = True
            sys.stderr.write(f"[提示] 設定檔讀不懂，這次的選擇不會存檔（不覆寫 {CONFIG_PATH}）\n")
        return
    d = os.path.dirname(os.path.abspath(CONFIG_PATH))
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        if os.path.exists(CONFIG_PATH):
            try:
                shutil.copymode(CONFIG_PATH, tmp)
            except OSError:
                pass
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
    ("gemma4:26b", "速度快、品質好（推薦，約需 17GB）"),
    ("phi4:14b", "Microsoft，品質不錯"),
    ("qwen2.5:32b", "品質很好，中日文翻譯推薦"),
    ("qwen2.5:14b", "品質好，較省記憶體（約需 9GB）"),
    ("qwen2.5:7b", "品質普通，速度最快"),
]

# 預設翻譯模型；LLM 伺服器沒有時依序退回備援模型，再沒有才選清單第一個
# gemma4 會思考，翻譯呼叫一律送 think=False（見 _llm_generate）
DEFAULT_TRANSLATE_MODEL = "gemma4:26b"
_TRANSLATE_MODEL_FALLBACKS = (DEFAULT_TRANSLATE_MODEL, "qwen2.5:14b")


def _default_translate_index(names):
    """回傳清單中預設翻譯模型的位置（找不到時為 0）"""
    for want in _TRANSLATE_MODEL_FALLBACKS:
        if want in names:
            return names.index(want)
    return 0

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
    ("ko2zh", "韓翻中字幕", "韓文語音 → 翻譯成繁體中文"),
    ("zh2ko", "中翻韓字幕", "中文語音 → 翻譯成韓文"),
    ("en_zh", "英中雙向字幕", "對方說英文翻中文 + 自己說中文翻英文"),
    ("ja_zh", "日中雙向字幕", "對方說日文翻中文 + 自己說中文翻日文"),
    ("ko_zh", "韓中雙向字幕", "對方說韓文翻中文 + 自己說中文翻韓文"),
    ("en", "英文轉錄", "英文語音 → 直接顯示英文"),
    ("zh", "中文轉錄", "中文語音 → 直接顯示繁體中文"),
    ("ja", "日文轉錄", "日文語音 → 直接顯示日文"),
    ("ko", "韓文轉錄", "韓文語音 → 直接顯示韓文"),
    ("nan", "台語轉錄", "台語語音 → 直接顯示繁體中文（Breeze-ASR-26）"),
    ("nan2en", "台翻英字幕", "台語語音 → 翻譯成英文"),
    ("record", "純錄音", f"僅錄製音訊為 {RECORDING_FORMAT.upper()} 檔"),
]

# Mode 分類常數
_EN_INPUT_MODES = ("en2zh", "en")
_ZH_INPUT_MODES = ("zh2en", "zh", "zh2ja", "zh2ko")
_JA_INPUT_MODES = ("ja2zh", "ja")
_KO_INPUT_MODES = ("ko2zh", "ko")      # v2.22.0
_NAN_INPUT_MODES = ("nan", "nan2en")   # 台語輸入（Breeze-ASR-26 專用，輸出為漢字）
_TRANSLATE_MODES = ("en2zh", "zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko",
                    "en_zh", "ja_zh", "ko_zh", "nan2en")
_NOENG_MODELS = ("zh", "zh2en", "zh2ja", "ja2zh", "ja", "en_zh", "ja_zh",
                 "ko2zh", "zh2ko", "ko", "ko_zh",
                 "nan", "nan2en")  # 不能用 .en 模型
_BIDI_MODES = ("en_zh", "ja_zh", "ko_zh")  # 雙向翻譯模式（用硬體音訊來源分流）
_BIDI_LB_DIR = {"en_zh": "en2zh", "ja_zh": "ja2zh", "ko_zh": "ko2zh"}   # 系統音訊翻譯方向
_BIDI_MIC_DIR = {"en_zh": "zh2en", "ja_zh": "zh2ja", "ko_zh": "zh2ko"}  # 麥克風翻譯方向
# 雙向模式對方的外語（麥克風自動偵測到這個語言時不翻譯，直接顯示）
_BIDI_FOREIGN = {"en_zh": "en", "ja_zh": "ja", "ko_zh": "ko"}

# 顯示標籤 dict（src_color, src_label, dst_color, dst_label）
_MODE_LABELS = {
    "en2zh": (C_EN, "EN", C_ZH, "中"),
    "zh2en": (C_ZH, "中", C_EN, "EN"),
    "ja2zh": (C_JA, "日", C_ZH, "中"),
    "zh2ja": (C_ZH, "中", C_JA, "日"),
    "ko2zh": (C_KO, "韓", C_ZH, "中"),
    "zh2ko": (C_ZH, "中", C_KO, "韓"),
    "en":    (C_EN, "EN", C_EN, "EN"),
    "zh":    (C_ZH, "中", C_ZH, "中"),
    "ja":    (C_JA, "日", C_JA, "日"),
    "ko":    (C_KO, "韓", C_KO, "韓"),
    "en_zh": (C_EN, "EN", C_ZH, "中"),  # 雙向模式 fallback（即時模式用 _BIDI_LABELS）
    "ja_zh": (C_JA, "日", C_ZH, "中"),
    "ko_zh": (C_KO, "韓", C_ZH, "中"),
    "nan":    (C_ZH, "台", C_ZH, "台"),   # 台語辨識結果本身即為漢字
    "nan2en": (C_ZH, "台", C_EN, "EN"),
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
    "ko_zh": {
        "loopback": (C_KO, "韓", C_ZH, "中"),      # 對方：韓文 → 中文
        "mic":      (C_MY_ZH, "中", C_MY_KO, "韓"), # 我方：中文 → 韓文
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
    "ko2zh": {
        "loopback": (C_KO, "韓", C_ZH, "中"),
        "mic":      (C_MY_ZH, "中", C_MY_ZH, "中"),
    },
    "zh2ko": {
        "loopback": (C_ZH, "中", C_KO, "韓"),
        "mic":      (C_MY_KO, "韓", C_MY_KO, "韓"),
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
    "ko": {
        "loopback": (C_KO, "韓", C_KO, "韓"),
        "mic":      (C_MY_KO, "韓", C_MY_KO, "韓"),
    },
}

# 雙向模式語言對照表（模組級，供 process_bidi_audio_files 等使用）
_LB_LANG = {"en2zh": "en", "zh2en": "zh", "ja2zh": "ja", "zh2ja": "zh",
            "ko2zh": "ko", "zh2ko": "zh",
            "en": "en", "zh": "zh", "ja": "ja", "ko": "ko",
            "en_zh": "en", "ja_zh": "ja", "ko_zh": "ko",
            "nan": "en", "nan2en": "en"}
_MIC_LANG = {"en2zh": "zh", "zh2en": "en", "ja2zh": "zh", "zh2ja": "ja",
             "ko2zh": "zh", "zh2ko": "ko",
             "en": "en", "zh": "zh", "ja": "ja", "ko": "ko",
             "en_zh": "zh", "ja_zh": "zh", "ko_zh": "zh",
             "nan": "en", "nan2en": "en"}

# ── 台語辨識模型（MediaTek Breeze-ASR-26，Whisper large-v2 台語微調）────────
# 直接輸出漢字，不需另外翻譯。以下為社群預先轉檔好的版本，格式與本專案既有引擎相同。
BREEZE_MODEL = "breeze-asr-26"
_BREEZE_REPOS = {
    "mlx":     "doggy8088/Breeze-ASR-26-MLX-4bit",          # Apple Silicon GPU，877MB
    "fw_int8": "WizardForest/faster-whisper-Breeze-ASR-26-int8",  # CPU，1.56GB
    "fw_fp16": "paulpengtw/faster-whisper-Breeze-ASR-26",    # CUDA，3.09GB
}
# 微調時沿用 Whisper 的 <|en|> 語言 token（見模型 config.json 的 forced_decoder_ids），
# 傳其他語言會明顯降低品質，故台語模式一律用 "en"。
_BREEZE_WHISPER_LANG = "en"


# 華語模式也可選用 Breeze-ASR-26：台灣的會議常是華語為主、夾雜台語。
# 本模型專屬的處理（language="en"、_FW_NAN_KW、自行 VAD 切段、即時步進下限、
# 固定本機辨識）都以 _is_nan_mode() 判斷；選用時由 _enforce_nan_model() 打開旗標，
# 讓這些處理一起生效，避免只換模型卻沿用一般參數組而大幅劣化。
_BREEZE_OPTIONAL_MODES = ("zh", "zh2en", "zh2ja", "zh2ko")
_breeze_selected = False


def _is_nan_mode(mode):
    """是否走 Breeze-ASR-26 的處理流程：台語模式，或華語模式選用了 Breeze-ASR-26"""
    return mode in _NAN_INPUT_MODES or (_breeze_selected and mode in _BREEZE_OPTIONAL_MODES)


def _enforce_nan_model(mode, model_name, quiet=False):
    """決定實際使用的模型，並同步 Breeze-ASR-26 處理流程的開關。
    - 台語模式只有 Breeze-ASR-26 能用，指定其他模型時改回並提示
    - 華語模式（zh / zh2en / zh2ja / zh2ko）可選用 Breeze-ASR-26
    - 其他模式不支援 Breeze-ASR-26，改用該模式的推薦模型
    未選用 Breeze-ASR-26 時，回傳值與處理流程都和原本相同。"""
    global _breeze_selected
    if mode in _NAN_INPUT_MODES:
        _breeze_selected = False
        if model_name != BREEZE_MODEL and not quiet:
            print(f"  {C_HIGHLIGHT}[提示] 台語模式僅支援 {BREEZE_MODEL}，"
                  f"已忽略指定的 {model_name}{RESET}")
        return BREEZE_MODEL
    if model_name == BREEZE_MODEL and mode not in _BREEZE_OPTIONAL_MODES:
        fallback = _recommended_whisper_model(mode)
        if not quiet:
            print(f"  {C_HIGHLIGHT}[提示] {BREEZE_MODEL} 僅支援台語與華語輸入模式"
                  f"（nan / nan2en / zh / zh2en / zh2ja / zh2ko），已改用 {fallback}{RESET}")
        model_name = fallback
    _breeze_selected = (model_name == BREEZE_MODEL)
    return model_name


# ── Qwen3-ASR（v2.23.0，實驗）──────────────────────────────────
# 2026-09-25 實測（tools/asr_bench/）：中文 20 場真實會議 CER 28.78% → 15.75%、中英夾雜少數語言召回 2~3 倍、
# 低音量 21% → 14%、韓文長檔 13.35% → 3.54%；**日文長檔較差**（8.38% vs 6.97%）、台語遠不如 Breeze → 這兩種不開。
# GPU 伺服器（vLLM worker，見 remote_whisper_server.py）或本機（v2.24.0 起，見下方 _qwen_local_backend）；只支援離線處理。
QWEN_MODEL = "qwen3-asr-0.6b"


def _qwen_server_ready(rw_cfg):
    """回傳 (能不能用, 原因)：GPU 伺服器 /health 的 qwen.ready"""
    if not rw_cfg:
        return False, "沒有設定 GPU 伺服器"
    try:
        url = f"http://{rw_cfg['host']}:{rw_cfg.get('whisper_port', REMOTE_WHISPER_DEFAULT_PORT)}/health"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=5) as r:
            q = json.loads(r.read().decode()).get("qwen")
    except Exception as e:
        return False, f"GPU 伺服器連不上（{type(e).__name__}）"
    if not q:
        return False, "GPU 伺服器沒有安裝 Qwen3-ASR"
    if not q.get("ready"):
        return False, f"GPU 伺服器的 Qwen3-ASR 尚未就緒（{q.get('error') or '載入中'}）"
    return True, ""


def _enforce_qwen_model(mode, model_name, rw_cfg=None, quiet=False):
    """選了 Qwen3-ASR 但這個模式／環境不適用時，改用該模式的推薦模型並說明原因（比照 _enforce_nan_model）。
    rw_cfg 是「這次在哪裡辨識」：有值＝GPU 伺服器、None＝本機。未選 Qwen3-ASR 時原樣回傳"""
    if model_name != QWEN_MODEL:
        return model_name
    if mode in _NAN_INPUT_MODES or _is_nan_mode(mode):
        why = "台語請用 Breeze-ASR-26（實測 Qwen3-ASR 遠不如它）"
    elif mode in _BIDI_MODES:
        why = "雙向模式尚未支援"
    elif mode not in _QWEN_MODES:
        # 用明確的清單放行，不靠 _mode_whisper_lang 的預設值（它對純錄音等模式也回 zh，2026-09-26 測試抓到）
        why = "目前只支援中文、英文、韓文輸入（日文實測長檔較差）"
    elif rw_cfg:
        ok, why = _qwen_server_ready(rw_cfg)
        if ok:
            return model_name
    else:
        backend, device, why = _qwen_local_backend()
        if backend:
            if device == "cpu" and not quiet:
                print(f"  {C_HIGHLIGHT}[提示] Qwen3-ASR 在這台電腦用 CPU 執行：{_QWEN_CPU_HINT}{RESET}")
            return model_name
        why = f"本機無法執行（{_qwen_local_fix_hint(why)}）"
    # 有 GPU 伺服器就退回伺服器的預設（large-v3-turbo）；沒有才依本機硬體推薦
    # （2026-09-26 實測：原本一律用本機推薦，有 GPU 伺服器的人被退到 small）
    fallback = "large-v3-turbo" if rw_cfg else _recommended_whisper_model(mode)
    if not quiet:
        print(f"  {C_HIGHLIGHT}[提示] Qwen3-ASR 無法使用：{why}，已改用 {fallback}{RESET}")
    return fallback


# ── Qwen3-ASR 本機（v2.24.0）──
# Apple Silicon 用 MLX（mlx-audio）；其他平台用 transformers 內建版（5.17 起）。
# 2026-09-25 平台實測（6.7 分中文會議，只算辨識）：Mac MLX 28.7 倍即時、CER 13.58%（現行 mlx-whisper turbo 21.33%）；
# NVIDIA transformers 6.5 倍；CPU 0.7（2 核 i5）~5.6 倍（M5）→ CPU 只當手動選項、附速度提示（D4）。
# 流程與 GPU 伺服器相同：_nan_vad_windows 切 ≤28 秒窗 → 辨識 → 對齊器逐字時間 → 依句末標點切句。
# _qwen_core／_qwen_filler_only／_qwen_sentences 與 remote_whisper_server.py 是同一套（tools/test_qwen_local.py 比對）
QWEN_LOCAL_REPOS = {
    "mlx": ("mlx-community/Qwen3-ASR-0.6B-8bit", "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"),
    "hf": ("Qwen/Qwen3-ASR-0.6B-hf", "Qwen/Qwen3-ForcedAligner-0.6B-hf"),
}
QWEN_LOCAL_GB = {"mlx": 2.3, "hf": 3.4}          # 兩個模型合計（HF 檔案大小）
_QWEN_LOCAL_GB_EACH = {"mlx": (1.0, 1.3), "hf": (1.6, 1.9)}    # 辨識、對齊器各自
_QWEN_CPU_HINT = "較準但很慢，請預留比錄音長度更久的時間"
_QWEN_CPU_MIN_RAM_GB = 12
_QWEN_LANG = {"zh": "Chinese", "en": "English", "ko": "Korean"}     # 日文實測長檔較差（E3），先不開
_QWEN_MODES = _ZH_INPUT_MODES + _EN_INPUT_MODES + _KO_INPUT_MODES
_QWEN_SENT_END = "。？！?!"
_QWEN_FILLERS = set("嗯啊呃唔哦喔欸誒呀哈") | {"um", "uh", "mm", "hmm", "mhm"}


def _transformers_supports(model_type):
    """產品 venv 的 transformers 有沒有內建某個模型（看能力、不比版本號）。回傳 (能不能用, 原因)；
    版本太舊時原因是空字串，由呼叫端寫出需要的版本"""
    try:
        import importlib.util
        if importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None:
            return False, "未安裝 transformers"
        from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES
    except Exception as e:
        return False, f"transformers 無法載入（{type(e).__name__}）"
    return model_type in CONFIG_MAPPING_NAMES, ""


@lru_cache(maxsize=1)
def _qwen_local_backend():
    """本機能不能跑 Qwen3-ASR：回傳 (後端, 裝置, 不能用的原因)。後端 "mlx"／"hf"、裝置 "mlx"／"cuda"／"cpu"；
    不能用時前兩個是 None。**平台判斷只在這裡**（比照 _recommended_mic_engine）"""
    import importlib.util
    if IS_MACOS and not _is_apple_silicon():
        return None, None, "Intel Mac 不支援"
    if _is_apple_silicon():
        # Mac 只走 MLX：MPS 只有 7.8 倍即時（MLX 28.7 倍），不值得多一條要維護的路。
        # 看套件裡有沒有 qwen3_asr 模組（不 import：mlx_audio.stt.models 會一次載入所有模型）
        try:
            spec = importlib.util.find_spec("mlx_audio")
        except Exception:
            spec = None
        if spec is None:
            return None, None, "未安裝 mlx-audio"
        if not any(os.path.isdir(os.path.join(p, "stt", "models", "qwen3_asr"))
                   for p in (spec.submodule_search_locations or [])):
            return None, None, "mlx-audio 版本太舊（需要 0.5.6 以上）"
        return "mlx", "mlx", ""
    ok, why = _transformers_supports("qwen3_asr")
    if not ok:
        return None, None, why or "transformers 版本太舊（Qwen3-ASR 需要 5.17 以上）"
    try:
        import torch
        cuda = torch.cuda.is_available()
    except Exception as e:
        return None, None, f"torch 無法載入（{type(e).__name__}）"
    if cuda:
        return "hf", "cuda", ""
    # CPU 用 fp32：處理中記憶體最高約 7.5 GB（2026-09-27 Windows 2 核 i5 16 GB 實測 7.55 GB、GB10 9.4 GB），
    # 8 GB 的電腦會一直用虛擬記憶體、慢到不能用 → 不開放。讀不到記憶體大小（0）時不擋
    mem = _get_system_memory_gb()
    if mem and mem < _QWEN_CPU_MIN_RAM_GB:
        return None, None, f"只有 CPU 且記憶體 {mem:.0f} GB，需要 {_QWEN_CPU_MIN_RAM_GB} GB 以上"
    return "hf", "cpu", ""


def _qwen_local_fix_hint(why):
    """不能跑的原因＋怎麼補：套件沒裝或太舊 → 重新執行安裝程式。Intel Mac、記憶體不足裝了也沒用，不給"""
    if "未安裝" not in why and "太舊" not in why:
        return why
    return f"{why}；重新執行 {_INSTALL_CMD} 會安裝"


def _qwen_local_cached(backend, which=None):
    """模型是否已下載（which：None＝兩個都要、"asr"／"al"＝只看其中一個）。沒有的話第一次使用要下載"""
    repos = QWEN_LOCAL_REPOS[backend]
    if which is not None:
        repos = repos[:1] if which == "asr" else repos[1:]
    try:
        from huggingface_hub import try_to_load_from_cache
        return all(isinstance(try_to_load_from_cache(r, "config.json"), str) for r in repos)
    except Exception:
        return False


def _qwen_core(s):
    return re.sub(r"[\W_]+", "", s)


def _qwen_filler_only(text):
    """一窗只有語氣詞（嗯／啊／um…）→ 丟掉。E3：60 秒靜音、雜訊、和弦、嗡嗡聲 Qwen 會吐「嗯。」"""
    words = re.findall(r"[a-z]+", text.lower())
    cjk = [c for c in _qwen_core(text) if not ("a" <= c.lower() <= "z")]
    return bool(words or cjk) and all(w in _QWEN_FILLERS for w in words) and all(c in _QWEN_FILLERS for c in cjk)


def _qwen_sentences(text, stamps, off, win_end):
    """一窗的文字依句末標點切句，用對齊器的逐字時間定起訖（E2 方案 A）。
    對齊器的 token 沒有標點 → 用「去掉標點後的字數」對回去。**不丟字**：
    對齊結果不夠時，剩下的文字照樣成一段（時間用到窗尾）；只有標點的尾巴接回前一句"""
    tc = [(s, e) for tk, s, e in stamps for _ in _qwen_core(tk)]
    out, buf, n, pos = [], "", 0, 0

    def flush():
        nonlocal buf, n, pos
        if n:
            if pos < len(tc):
                s0, e0 = off + tc[pos][0], off + tc[min(pos + n, len(tc)) - 1][1]
            else:
                s0, e0 = (out[-1]["end"] if out else off), win_end
            out.append({"start": round(s0, 3), "end": round(max(e0, s0), 3), "text": buf.strip()})
        elif buf.strip() and out:
            out[-1]["text"] += buf.strip()
        pos += n
        buf, n = "", 0

    for i, ch in enumerate(text):
        buf += ch
        if _qwen_core(ch):
            n += 1
        # 英文／韓文的句點：後面是空白或結尾、前一個字不是數字（「3.5」不切）才算句末
        if ch in _QWEN_SENT_END or (ch == "." and (i + 1 == len(text) or text[i + 1].isspace())
                                    and not (i and text[i - 1].isdigit())):
            flush()
    flush()
    return out


class _QwenLocal:
    """本機 Qwen3-ASR＋對齊器。MLX 逐窗；transformers 一次送一批（CUDA 8、CPU 4，與平台實測相同）。
    兩個模型**用到才載入、辨識完先釋放辨識模型再載對齊器**：CPU 上是 fp32，兩個同時在記憶體約 9 GB
    （2026-09-27 Windows 2 核 i5 實測），先後載入只要其中大的那個"""

    def __init__(self, backend, device):
        self.backend, self.device = backend, device
        self.asr = self.al = self.proc = self.al_proc = None
        if backend == "mlx":
            self.batch = 1
            return
        import torch
        if device == "cuda":
            self.dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        else:
            self.dtype = torch.float32
        self.batch = 8 if device == "cuda" else 4

    def load(self, which):
        """載入 "asr"（辨識）或 "al"（對齊器）；已載入就不動。第一次用會從 HuggingFace 下載"""
        if getattr(self, which) is not None:
            return
        repo = QWEN_LOCAL_REPOS[self.backend][0 if which == "asr" else 1]
        if self.backend == "mlx":
            from mlx_audio.stt.utils import load_model
            setattr(self, which, _call_with_ssl_retry(load_model, repo))
            return
        from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor
        cls = AutoModelForMultimodalLM if which == "asr" else AutoModelForTokenClassification
        # 不用 device_map：那要多裝 accelerate（E1）
        setattr(self, "proc" if which == "asr" else "al_proc", _call_with_ssl_retry(AutoProcessor.from_pretrained, repo))
        setattr(self, which, _call_with_ssl_retry(cls.from_pretrained, repo, dtype=self.dtype).to(self.device).eval())

    def release_asr(self):
        """辨識做完、對齊前呼叫：把辨識模型的記憶體還回去"""
        self.asr = self.proc = None
        self._free()

    def transcribe(self, chunks, language, progress=None):
        """每窗一段文字；極短的窗（<0.2 秒）不送模型（與伺服器相同）"""
        texts = [""] * len(chunks)
        live = [k for k, c in enumerate(chunks) if len(c) >= 3200]
        if live:
            self.load("asr")
        for k0 in range(0, len(live), self.batch):
            ks = live[k0:k0 + self.batch]
            if self.backend == "mlx":
                texts[ks[0]] = self.asr.generate(chunks[ks[0]], language=language, max_tokens=512).text
            else:
                import torch
                inp = self.proc.apply_transcription_request(
                    audio=[chunks[k] for k in ks], language=[language] * len(ks)).to(self.device, self.dtype)
                with torch.inference_mode():
                    ids = self.asr.generate(**inp, max_new_tokens=512, do_sample=False)
                out = self.proc.decode(ids[:, inp["input_ids"].shape[1]:], return_format="transcription_only")
                for k, t in zip(ks, out):
                    texts[k] = t
            if progress:
                progress(ks[-1])
        return texts

    def align(self, chunks, texts, language, progress=None):
        """每窗的逐字時間 [[字, 起, 迄]]（窗內相對秒數）。某批失敗時那幾窗留空、文字照用（切句時不丟字），回傳 (時間, 失敗窗數)"""
        stamps = [[] for _ in texts]
        failed = 0
        idx = [k for k, t in enumerate(texts) if t.strip()]
        if idx:
            self.load("al")
        for k0 in range(0, len(idx), self.batch):
            ks = idx[k0:k0 + self.batch]
            try:
                if self.backend == "mlx":
                    r = self.al.generate([chunks[k] for k in ks], [texts[k] for k in ks], language=[language] * len(ks))
                    res = [[[x.text, float(x.start_time), float(x.end_time)] for x in items] for items in r]
                else:
                    import torch
                    inp, words = self.al_proc.prepare_forced_aligner_inputs(
                        audio=[chunks[k] for k in ks], transcript=[texts[k] for k in ks],
                        language=[language] * len(ks))
                    inp = inp.to(self.device, self.dtype)
                    with torch.inference_mode():
                        logits = self.al(**inp).logits
                    r = self.al_proc.decode_forced_alignment(
                        logits=logits, input_ids=inp["input_ids"], word_lists=words,
                        timestamp_token_id=self.al.config.timestamp_token_id)
                    res = [[[x["text"], float(x["start_time"]), float(x["end_time"])] for x in items] for items in r]
            except Exception as e:
                failed += len(ks)
                print(f"  {C_DIM}[Qwen3-ASR] 對齊失敗 {len(ks)} 窗（{type(e).__name__}: {e}），這幾段的時間以整窗估計{RESET}")
                continue
            for k, v in zip(ks, res):
                stamps[k] = v
            if progress:
                progress(ks[-1])
        return stamps, failed

    def close(self):
        self.asr = self.al = self.proc = self.al_proc = None
        self._free()

    def _free(self):
        import gc
        gc.collect()
        if self.backend == "mlx":
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
        else:
            _release_gpu_resources()


def _qwen_local_transcribe(wav_path, mode, progress_cb=None, stage_cb=None):
    """本機 Qwen3-ASR 離線辨識，回傳 [{"start", "end", "text"}]（與其他辨識路徑同格式）。
    progress_cb(秒)：辨識進度；stage_cb(文字)：階段（載入、對齊）。失敗時丟例外，由呼叫端改用 Whisper"""
    backend, device, why = _qwen_local_backend()
    if not backend:
        raise RuntimeError(why)
    lang = _QWEN_LANG[_mode_whisper_lang(mode)]
    audio = _read_wav_mono16k(wav_path)
    sr = _WHISPER_INPUT_SR
    windows = _nan_vad_windows(audio, sr)
    chunks = [audio[max(0, int(a * sr)):int(b * sr)] for a, b in windows]
    def _stage(text):
        if stage_cb:
            stage_cb(text)

    def _load(which):
        name = "辨識" if which == "asr" else "對齊"
        gb = _QWEN_LOCAL_GB_EACH[backend][0 if which == "asr" else 1]
        _stage(f"載入{name}模型" if _qwen_local_cached(backend, which)
               else f"第一次使用，下載{name}模型（約 {gb} GB）")
        eng.load(which)

    prog = (lambda k: progress_cb(windows[k][1])) if progress_cb else None
    eng = _QwenLocal(backend, device)
    try:
        _load("asr")
        _stage("辨識中")
        texts = eng.transcribe(chunks, lang, progress=prog)
        eng.release_asr()
        # 只有語氣詞的窗反正要丟（靜音時模型會吐「嗯。」），先丟掉就不必對齊；整段都沒內容時連對齊器都不用載
        texts = ["" if _qwen_filler_only(t) else t for t in texts]
        if any(t.strip() for t in texts):
            _load("al")
        _stage("對齊時間")
        stamps, failed = eng.align(chunks, texts, lang, progress=prog)
    finally:
        eng.close()
    segs = []
    for (ws, we), txt, st in zip(windows, texts, stamps):
        if not txt.strip() or _qwen_filler_only(txt):
            continue
        segs += _qwen_sentences(txt, st, ws, we)
    return segs


def _qwen_menu_desc(mode, use_remote, remote_models=None):
    """互動選單要不要列 Qwen3-ASR：要列就回傳說明文字，否則 None。
    GPU 伺服器：伺服器上已就緒（就緒才會出現在它的模型清單）；本機：_qwen_local_backend 跑得了"""
    if mode not in _QWEN_MODES:
        return None
    if use_remote:
        return "（實驗）中文會議、中英夾雜明顯更準" if remote_models and QWEN_MODEL in remote_models else None
    backend, device, _why = _qwen_local_backend()
    if not backend:
        return None
    desc = "（實驗）較準但很慢（本機只有 CPU）" if device == "cpu" else "（實驗）中文會議、中英夾雜明顯更準"
    if not _qwen_local_cached(backend):
        desc += f"，第一次使用下載約 {QWEN_LOCAL_GB[backend]} GB"
    return desc


def _qwen_local_offline(wav_path, mode, audio_duration=0, fell_back=False):
    """離線處理的本機 Qwen3-ASR 那一段（含狀態列與退回）。回傳 (模型, segments, 狀態列)：
    不能跑或失敗時 segments 為 None、模型換成本機推薦的 Whisper，呼叫端接著走原本的本機辨識。
    fell_back：原本要用 GPU 伺服器、是伺服器失敗才退到這裡"""
    backend, device, why = _qwen_local_backend()
    fallback = _recommended_whisper_model(mode)
    if not backend:
        print(f"  {C_HIGHLIGHT}[降級] 本機無法執行 Qwen3-ASR（{_qwen_local_fix_hint(why)}），改用 {fallback}{RESET}")
        return fallback, None, None
    if fell_back and device == "cpu":
        # CPU 跑 Qwen 比錄音還久，使用者選的是 GPU 伺服器、不是這個（D4：CPU 只當手動選項）
        print(f"  {C_HIGHLIGHT}[降級] 本機只有 CPU，Qwen3-ASR 會很慢，改用 {fallback}{RESET}")
        return fallback, None, None
    label = {"mlx": "MLX GPU", "cuda": "CUDA GPU", "cpu": "CPU"}.get(device, device)
    print(f"  {C_WHITE}辨識引擎    Qwen3-ASR 0.6B（本機 {label}，實驗）{RESET}\n")
    _webui_send({"type": "progress", "stage": "辨識中", "detail": f"本機 {QWEN_MODEL}"})
    sbar = _SummaryStatusBar(model=QWEN_MODEL, task="準備中", asr_location="本機").start()

    def _prog(pos):
        if audio_duration > 0:
            pct = min(pos / audio_duration, 1.0)
            pm, ps = divmod(int(pos), 60)
            dm, ds = divmod(int(audio_duration), 60)
            sbar.set_progress(f"{pct:.0%}  {pm}:{ps:02d} / {dm}:{ds:02d}")

    def _stage(s):
        sbar.set_task(s, reset_timer=False)
        _webui_send({"type": "progress", "stage": s, "detail": f"本機 {QWEN_MODEL}"})

    try:
        return QWEN_MODEL, _qwen_local_transcribe(wav_path, mode, progress_cb=_prog, stage_cb=_stage), sbar
    except Exception as e:
        sbar.set_task("Qwen3-ASR 失敗", reset_timer=False)
        sbar.freeze()
        sbar.stop()
        print(f"  {C_HIGHLIGHT}[降級] 本機 Qwen3-ASR 失敗（{type(e).__name__}: {e}），改用 {fallback}{RESET}")
        return fallback, None, None


def _mode_whisper_lang(mode):
    """依模式決定要傳給 Whisper 的語言代碼"""
    if _is_nan_mode(mode):
        return _BREEZE_WHISPER_LANG
    if mode in _EN_INPUT_MODES:
        return "en"
    if mode in _JA_INPUT_MODES:
        return "ja"
    if mode in _KO_INPUT_MODES:
        return "ko"
    return "zh"


def _resolve_fw_model(model_name, remote=False):
    """把模型代號轉成 faster-whisper 可載入的名稱或 HF repo。
    一般模型原樣回傳；台語模型依執行位置選 float16 / int8 版本
    （GPU 伺服器一律 float16，本機看有沒有 CUDA）。"""
    if model_name == BREEZE_MODEL:
        if remote or _fw_local_cuda_ok():
            return _BREEZE_REPOS["fw_fp16"]
        return _BREEZE_REPOS["fw_int8"]
    return model_name


def _resolve_mlx_repo(model_name):
    """把模型代號轉成 mlx-whisper 的 HF repo"""
    if model_name == BREEZE_MODEL:
        return _BREEZE_REPOS["mlx"]
    # MLX 社群 repo 命名不一致：large-v3-turbo 無字尾，其餘需加 -mlx
    _sfx = {"large-v3-turbo": "", "large-v3": "-mlx", "medium": "-mlx",
            "small": "-mlx", "base": "-mlx", "tiny": "-mlx"}.get(model_name, "-mlx")
    return f"mlx-community/whisper-{model_name}{_sfx}"

# 可用的 whisper 模型（由小到大）
WHISPER_MODELS = [
    ("base.en", "ggml-base.en.bin", "最快，準確度一般"),
    ("base", "ggml-base.bin", "最快，中日文可用"),
    ("small.en", "ggml-small.en.bin", "快，準確度好"),
    ("small", "ggml-small.bin", "快，中日文可用"),
    ("large-v3-turbo", "ggml-large-v3-turbo.bin", "快，準確度很好"),
    ("large-v3", "ggml-large-v3.bin", "最慢，中日文品質最好，有獨立 GPU 可選用"),
]

# ── CPU 效能評估（自動選擇適合的 Whisper 模型）──

@lru_cache(maxsize=1)
def _is_apple_silicon():
    """偵測是否為 Apple Silicon (ARM64) Mac"""
    import platform
    return IS_MACOS and platform.machine() == "arm64"


@lru_cache(maxsize=1)
def _has_local_gpu():
    """本機是否有 GPU 加速（Apple Silicon Metal 或 NVIDIA CUDA）"""
    if _is_apple_silicon():
        return True
    if IS_WINDOWS:
        import shutil
        return bool(shutil.which("nvidia-smi"))
    if IS_LINUX:
        return _fw_local_cuda_ok()
    return False


_FW_AV_PATCHED = False


def _fw_av_compat():
    """PyAV 19（2026-10）拿掉了 av.open 的 metadata_errors 參數，faster-whisper（到 1.2.1 都是）讀音檔時還在傳，
    而它對 av 的版本沒設上限 → 新安裝的機器每一段辨識都是「open() got an unexpected keyword argument
    'metadata_errors'」（Windows 10 使用者回報）。av 不認得這個參數時，換上一個把它濾掉的 av.open；
    認得（av 18 以前）或沒裝 av 時什麼都不做。只檢查一次，載入 faster-whisper 之後呼叫。
    translate_meeting.py 與 remote_whisper_server.py 各一份，逐字相同（tools/test_av_compat.py 比對）"""
    global _FW_AV_PATCHED
    if _FW_AV_PATCHED:
        return
    _FW_AV_PATCHED = True
    import io
    try:
        import av
        orig = av.open
    except Exception:
        return
    try:
        orig(io.BytesIO(b""), metadata_errors="ignore")
    except TypeError as e:
        if "metadata_errors" not in str(e):
            return
    except Exception:
        return                      # 參數收下了（空的資料本來就打不開）
    else:
        return

    def _open(*args, metadata_errors=None, **kwargs):
        return orig(*args, **kwargs)
    av.open = _open


_FW_CUDA_OFF = {"reason": None}         # 這個程序已改用 CPU 的原因：之後都不再試顯示卡
_FW_CUDA_NOTICED = [False]
_FW_CUDA_HAS_DEVICE = [None]


def _fw_cuda_notice(reason, fix=""):
    """顯示卡不能用、改用 CPU 時說一次原因與修法（以前每一段都印一次「本機辨識失敗」，看不出該怎麼辦）"""
    if _FW_CUDA_NOTICED[0]:
        return
    _FW_CUDA_NOTICED[0] = True
    print(f"\n  {C_WARN}[提示] 顯示卡（CUDA）不能用來辨識：{reason}。本機辨識改用 CPU（較慢）。{RESET}", flush=True)
    if fix:
        print(f"  {C_DIM}{fix}{RESET}", flush=True)


def _fw_local_cuda_ok():
    """本機 CTranslate2（faster-whisper）能否使用 CUDA 加速。
    Apple Silicon 的 CTranslate2 沒有 Metal 後端，一律走 CPU（ASR 另用 mlx）。
    Windows 另外要載得到 CUDA 函式庫（_win_cuda_libs_ok）：有顯示卡不代表有 cuBLAS／cuDNN"""
    if _is_apple_silicon() or _FW_CUDA_OFF["reason"]:
        return False
    if _FW_CUDA_HAS_DEVICE[0] is None:                 # 原本整個函式用 lru_cache；改成只記住「有沒有 CUDA」，
        try:                                           # 停用的旗標每次都要看（執行中改用 CPU 之後要生效）
            import ctranslate2
            _FW_CUDA_HAS_DEVICE[0] = bool(ctranslate2.get_supported_compute_types("cuda"))
        except Exception:
            _FW_CUDA_HAS_DEVICE[0] = False
    if not _FW_CUDA_HAS_DEVICE[0]:
        return False
    ok, missing = _win_cuda_libs_ok()
    if not ok:
        _FW_CUDA_OFF["reason"] = "missing"
        _fw_cuda_notice("找不到 " + "、".join(missing[:4]) + ("…" if len(missing) > 4 else ""),
                        "要用顯示卡加速：在安裝資料夾重新執行 .\\install.ps1（會補裝 CUDA 版 PyTorch 與 "
                        "nvidia-cublas-cu12、nvidia-cudnn-cu12，裡面有這些函式庫）")
        return False
    return True


# mlx-community 有對應 repo 的模型（.en 系列不在其中，需退回 faster-whisper）
_MLX_CAPABLE_MODELS = {"large-v3-turbo", "large-v3", "medium", "small", "base", "tiny",
                       BREEZE_MODEL}


def _local_asr_use_mlx(model_name, args=None):
    """Python 端本機即時辨識是否改用 mlx-whisper GPU 加速（僅 Apple Silicon）。
    使用者明確指定 --asr faster-whisper 時尊重其選擇。"""
    if args is not None and getattr(args, "asr", None) == "faster-whisper":
        return False
    if model_name not in _MLX_CAPABLE_MODELS:
        return False
    return _is_apple_silicon() and _has_mlx_whisper()


def _fw_device_kwargs():
    """faster-whisper WhisperModel 的 device / compute_type 設定。

    RTX 50 系列（Blackwell, sm_120）跑 int8 量化會噴
    `cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED`，
    故 CUDA 一律改用 float16（速度更快、準確度更好，VRAM 也夠）；
    無 CUDA 時用 CPU + int8（CPU 上 int8 才快）。
    顯示卡不支援 float16（compute capability 5.3 以下）時用 float32。
    不用顯示卡時一定要寫 "cpu"：寫 "auto" 的話 ctranslate2 看得到顯示卡就會自己選回 CUDA，
    缺 CUDA 函式庫的機器照樣每一段都失敗（2026-10-05）"""
    if _fw_local_cuda_ok():
        try:
            import ctranslate2
            types = ctranslate2.get_supported_compute_types("cuda")
        except Exception:
            types = set()
        return {"device": "cuda", "compute_type": "float16" if "float16" in types else "float32"}
    return {"device": "cpu", "compute_type": "int8"}


def _make_denoiser(denoise):
    """即時模式的降噪函式（audio, sr）→ audio。降噪是選用功能：載入失敗（例如 Windows 的應用程式控制擋下 scipy
    的程式檔）時說明原因、這次不降噪、照常辨識，不可以讓整個程式結束（2026-10-05，以前直接 traceback 結束）"""
    def _identity(audio, sr):
        return audio
    if not denoise:
        return _identity
    try:
        from noisereduce import reduce_noise as _nr_reduce
    except Exception as e:
        print(f"  {C_WARN}[提示] 降噪無法啟用：{str(e).splitlines()[0][:200]}。這次不降噪，照常辨識。{RESET}", flush=True)
        hint = _dll_block_hint(e)
        if hint:
            print(f"  {C_DIM}{hint}{RESET}", flush=True)
        return _identity

    def _denoise(audio, sr):
        peak = np.max(np.abs(audio))
        out = _nr_reduce(y=audio, sr=sr, stationary=True, prop_decrease=0.8)
        peak_after = np.max(np.abs(out))
        if peak_after > 1e-6:
            out = out * (peak / peak_after)
        return out
    return _denoise


def _fw_is_cuda_error(e):
    """這個錯誤是不是顯示卡那一端的問題（函式庫載不到、顯示記憶體不足、這張卡不支援）"""
    s = f"{type(e).__name__}: {e}".lower()
    return any(k in s for k in ("cuda", "cublas", "cudnn", "out of memory", "not found or cannot be loaded",
                                "no kernel image", "invalid device function", "nvrtc", "compute capability"))


class _FwModel:
    """faster-whisper 的 WhisperModel，顯示卡出錯時自動改用 CPU（2026-10-05）。

    沒有 Windows＋NVIDIA 的測試機，函式庫以外的顯示卡問題（顯示記憶體不足、舊卡不支援、版本不合）也測不完，
    所以在用的地方兜底：建立模型、或第一段辨識就因為顯示卡失敗時，說明一次原因、在 CPU 重建模型再做一次，
    之後整個程序都用 CPU。不是顯示卡的錯誤照樣丟出去；第一段之後才失敗的也照樣丟出去（不會重複產生段落）。
    transcribe 以外的方法（detect_language 等）同樣處理。"""
    _END = object()

    def __init__(self, model_path, **kw):
        from faster_whisper import WhisperModel
        _fw_av_compat()
        self._cls, self._path, self._kw = WhisperModel, model_path, dict(kw)
        self._lock = threading.Lock()
        self._m = None
        try:
            self._m = WhisperModel(model_path, **self._kw)
        except Exception as e:
            if self._kw.get("device") != "cuda" or not _fw_is_cuda_error(e):
                raise
            self._to_cpu(e)

    @property
    def device(self):
        return self._kw.get("device")

    def _to_cpu(self, e):
        with self._lock:
            if self._kw.get("device") != "cuda" and self._m is not None:
                return                                    # 別的執行緒已經換好了
            msg = (str(e).strip().splitlines() or [type(e).__name__])[0][:160]
            _FW_CUDA_OFF["reason"] = msg
            if "out of memory" in msg.lower():
                fix = "顯示記憶體不足：改用較小的模型（例如 -m small）就能繼續用顯示卡"
            elif IS_WINDOWS:
                fix = "在安裝資料夾重新執行 .\\install.ps1 可以補裝 CUDA 函式庫"
            else:
                fix = ""
            _fw_cuda_notice(msg, fix)
            self._m = None
            try:
                _release_gpu_resources()
            except Exception:
                pass
            self._kw = _fw_device_kwargs()                # 已停用 CUDA → cpu／int8
            self._m = self._cls(self._path, **self._kw)

    def transcribe(self, *args, **kwargs):
        if self._kw.get("device") != "cuda":
            return self._m.transcribe(*args, **kwargs)
        try:
            segs, info = self._m.transcribe(*args, **kwargs)
            it = iter(segs)
            first = next(it, self._END)                   # 顯示卡的錯誤多半在第一段的推論才出現
        except Exception as e:
            if not _fw_is_cuda_error(e):
                raise
            self._to_cpu(e)
            return self._m.transcribe(*args, **kwargs)

        def _rest():
            if first is not self._END:
                yield first
                yield from it
        return _rest(), info

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._m, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if self._kw.get("device") != "cuda":
                return getattr(self._m, name)(*args, **kwargs)
            try:
                return getattr(self._m, name)(*args, **kwargs)
            except Exception as e:
                if not _fw_is_cuda_error(e):
                    raise
                self._to_cpu(e)
                return getattr(self._m, name)(*args, **kwargs)
        return call


@lru_cache(maxsize=1)
def _get_system_memory_gb():
    """取得系統實體記憶體大小（GB）"""
    try:
        if IS_MACOS:
            result = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                    capture_output=True, text=True, timeout=5)
            return int(result.stdout.strip()) / (1024 ** 3)
        elif IS_WINDOWS:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulong = ctypes.c_ulonglong
            mem = c_ulong()
            kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(mem))
            return mem.value / (1024 * 1024)  # KB → GB
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) / (1024 * 1024)  # KB → GB
    except Exception:
        pass
    return 0


def _recommended_mic_engine(mode="zh", remote_whisper_cfg=None):
    """推薦麥克風轉錄的 ASR 引擎與模型。
    優先順序：GPU 伺服器 > mlx GPU > 本機 CPU。
    回傳 (engine, model)：engine = 'remote' | 'mlx' | 'cpu', model = 模型名。"""
    _need_multilang = mode in _NOENG_MODELS
    # 1. 有 GPU 伺服器 → 優先遠端（macOS / Windows 都適用）
    if remote_whisper_cfg:
        return "remote", "large-v3-turbo"
    # 2. Apple Silicon + mlx-whisper → GPU 加速（依記憶體選模型）
    if _is_apple_silicon() and _has_mlx_whisper():
        mem_gb = _get_system_memory_gb()
        if mem_gb >= 24:
            return "mlx", "large-v3-turbo"
        elif mem_gb >= 16:
            return "mlx", "small" if _need_multilang else "small.en"
        else:
            return "cpu", "small" if _need_multilang else "base.en"
    # 3. 本機 CPU
    return "cpu", "small" if _need_multilang else "base.en"


@lru_cache(maxsize=1)
def _has_mlx_whisper():
    """Apple Silicon 且已安裝 mlx-whisper"""
    if not _is_apple_silicon():
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("mlx_whisper") is not None
    except Exception:
        return False


@lru_cache(maxsize=16)
def _recommended_whisper_model(mode="en2zh"):
    """根據 CPU 架構與核心數推薦此裝置最適合的即時 Whisper 模型。
    Apple Silicon 有 Metal GPU 加速，同核心數效能遠高於 Intel CPU。"""
    if mode in _NAN_INPUT_MODES:
        return BREEZE_MODEL   # 台語只有 Breeze-ASR-26 可用
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
    # Linux 無 CUDA：faster-whisper 純 CPU，比照 Intel Mac 用小模型
    if IS_LINUX and not _has_local_gpu():
        if _need_multilang:
            return "small"
        return "small.en" if cores >= 8 else "base.en"
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

APP_VERSION = "2.29.0"

# faster-whisper 離線辨識參數（含長音檔幻覺防護）— 標準模式
# - condition_on_previous_text=False：切斷上一段 prompt 傳染，避免一個短句卡住後幻覺自我強化
# - temperature 列表：解碼失敗時依序提高溫度重試（faster-whisper 預設行為）
# - hallucination_silence_threshold=2.0：偵測到幻覺時跳過 ≥2s 靜音（需 word_timestamps=True）
# - repetition_penalty=1.05：抑制連續重複片段
_FW_OFFLINE_KW = dict(
    beam_size=5,
    vad_filter=True,
    vad_parameters={"min_silence_duration_ms": 500},
    condition_on_previous_text=False,
    temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    compression_ratio_threshold=2.4,
    log_prob_threshold=-1.0,
    no_speech_threshold=0.6,
    repetition_penalty=1.05,
    word_timestamps=True,
    hallucination_silence_threshold=2.0,
)

# faster-whisper 寬鬆模式：低音量 / 監視器 / 行車紀錄等難搞音源自動切換
# 觸發條件：mean_volume < -30 dBFS（_analyze_audio_loudness 偵測）
# 差異：不靠 Silero VAD 預剃除、放寬 no_speech 與 log_prob、關閉 word_timestamps（節省成本）
_FW_OFFLINE_KW_LOOSE = dict(
    beam_size=5,
    vad_filter=False,
    condition_on_previous_text=False,
    temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    compression_ratio_threshold=2.4,
    # **這兩個一定要一起看**（2026-09-22 修）：faster-whisper 的判斷是
    #     should_skip = no_speech_prob > no_speech_threshold
    #     if log_prob_threshold is not None and avg_logprob > log_prob_threshold:
    #         should_skip = False        ← 唯一的救援
    #     if should_skip: 整個 30 秒視窗直接丟掉
    # **門檻調低是「更容易跳過」，方向與「寬鬆」相反**；原本又把 log_prob_threshold
    # 設成 None 關掉救援，於是它變成唯一且嚴苛的閘門。large-v3 因此吐 0 段
    # （turbo 的 no_speech_prob 剛好低一點才躲過，所以 v2.16.3 至今沒被發現）。
    # 實測同一份低音量中文會議：0.3/None → 0 段、0.6/-2.0 → 100 段；
    # turbo 兩者皆 92 段（無回歸）。-2.0 比嚴格模式的 -1.0 寬，低信心的字仍留得住。
    log_prob_threshold=-2.0,
    no_speech_threshold=0.6,
    repetition_penalty=1.05,
    word_timestamps=False,
)


# ── 台語（Breeze-ASR-26）專用辨識參數 ─────────────────────────────────────
# 專案既有的防幻覺參數組（_FW_OFFLINE_KW）對本模型有害：實測同一批台語音檔
# CER 17.99% → 56.42%、且慢 4.7 倍。原因是該模型微調後 logprob / no_speech
# 分布與原版 Whisper 不同，門檻持續誤判，temperature fallback 階梯每句都跑完。
# 另外本模型訓練時被 forced <|notimestamps|>，不產生時間戳 token，
# 開 vad_filter / word_timestamps 只會拿到錯誤時間（實測第二段 25 秒文字
# 被標成 0.48 秒），故一律關閉，時間戳改由 _nan_vad_windows() 自行切段取得。
_FW_NAN_KW = dict(
    beam_size=5,
    condition_on_previous_text=False,
    vad_filter=False,
    word_timestamps=False,
)

_NAN_WINDOW_SEC = 28.0       # 每個辨識視窗最長秒數（Whisper 單次上限為 30 秒）
_NAN_VAD_SILENCE_MS = 500    # 切段用的最短靜音長度


_NAN_MIN_STEP_MS = 6000   # 台語即時模式的最短步進


def _nan_adjust_step(mode, length_ms, step_ms, quiet=False):
    """台語即時模式的步進下限。

    Breeze-ASR-26 是 Whisper large-v2 微調（32 層 decoder，約為 turbo 的 8 倍），
    Apple Silicon 上一段約需 5 秒。步進若短於辨識時間，佇列會無限累積、
    字幕越拖越慢，因此拉高步進下限，改以稍高的延遲換取穩定。"""
    if not _is_nan_mode(mode) or step_ms >= _NAN_MIN_STEP_MS:
        return length_ms, step_ms
    new_step = _NAN_MIN_STEP_MS
    new_length = max(length_ms, new_step + 2000)
    if not quiet:
        _tag = "台語" if mode in _NAN_INPUT_MODES else BREEZE_MODEL
        print(f"  {C_DIM}[{_tag}] 辨識較慢，步進 {step_ms}ms → {new_step}ms"
              f"（緩衝 {length_ms}ms → {new_length}ms）避免字幕越拖越慢{RESET}")
    return new_length, new_step


def _fw_transcribe_kwargs(mode, loose=False):
    """依模式回傳離線辨識參數組（台語走專用組，其餘維持原本行為）"""
    if _is_nan_mode(mode):
        return _FW_NAN_KW
    return _FW_OFFLINE_KW_LOOSE if loose else _FW_OFFLINE_KW


def _nan_vad_windows(audio, samplerate=16000):
    """把音訊依語音活動切成不超過 _NAN_WINDOW_SEC 的視窗，回傳 [(起, 迄)] 秒。

    Breeze-ASR-26 不會產生時間戳，時間資訊只能由外部切段提供。相鄰語音段
    會盡量併進同一個視窗，讓總視窗數接近「音訊長度 / 28 秒」，
    辨識成本與一般模型相當，不會因為切太碎而變慢。
    偵測不到語音時回傳整段固定切窗，確保不會漏內容。"""
    total = len(audio) / float(samplerate)
    try:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        regions = get_speech_timestamps(
            audio, VadOptions(min_silence_duration_ms=_NAN_VAD_SILENCE_MS),
            sampling_rate=samplerate)
    except Exception as e:
        # 改用固定 28 秒切段：邊界會切斷字、靜音段也會送去辨識（可能出現幻覺），要讓使用者知道（2026-10-05）
        print(f"  [台語] 語音活動偵測（VAD）無法使用（{type(e).__name__}: {e}），改用固定 28 秒切段，斷句可能較差", flush=True)
        regions = []

    if not regions:
        # 無 VAD 可用（或整段沒偵測到語音）→ 固定長度切窗
        out, t = [], 0.0
        while t < total:
            out.append((t, min(t + _NAN_WINDOW_SEC, total)))
            t += _NAN_WINDOW_SEC
        return out or [(0.0, total)]

    windows = []
    cur_start = cur_end = None
    for r in regions:
        rs, re_ = r["start"] / float(samplerate), r["end"] / float(samplerate)
        if cur_start is None:
            cur_start, cur_end = rs, re_
        elif re_ - cur_start <= _NAN_WINDOW_SEC:
            cur_end = re_                      # 併入目前視窗
        else:
            windows.append((cur_start, cur_end))
            cur_start, cur_end = rs, re_
        # 單一語音段就超過視窗長度時強制切開，避免超出模型 30 秒上限
        while cur_end - cur_start > _NAN_WINDOW_SEC:
            windows.append((cur_start, cur_start + _NAN_WINDOW_SEC))
            cur_start += _NAN_WINDOW_SEC
    if cur_start is not None:
        windows.append((cur_start, cur_end))
    return windows


def _read_wav_mono16k(wav_path):
    """讀 16-bit WAV 成 16 kHz 單聲道 float32（台語與本機 Qwen3-ASR 逐窗辨識共用）"""
    import numpy as np
    import wave as _wave

    with _wave.open(wav_path, "r") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        audio = np.frombuffer(wf.readframes(wf.getnframes()),
                              dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != _WHISPER_INPUT_SR:
        from math import gcd as _gcd
        from scipy.signal import resample_poly as _resample_poly
        g = _gcd(int(sr), _WHISPER_INPUT_SR)
        audio = _resample_poly(audio, _WHISPER_INPUT_SR // g, int(sr) // g)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _nan_transcribe_windows(model, wav_path, progress_cb=None, use_mlx=False):
    """台語離線辨識：自行 VAD 切段後逐段辨識，時間戳取自切段邊界。

    Breeze-ASR-26 不產生時間戳 token，直接整檔辨識只會得到「每 30 秒一段」
    且結束時間錯誤的結果，SRT / VTT / 時間逐字稿全部不可用，因此改由這裡切段。
    use_mlx=True 時走 mlx-whisper GPU（Apple Silicon 上快約 4 倍）。
    回傳格式與其他辨識路徑一致：[{"start", "end", "text"}]。"""
    audio = _read_wav_mono16k(wav_path)

    if use_mlx:
        import mlx_whisper as _mlx
        _repo = _resolve_mlx_repo(BREEZE_MODEL)

    out = []
    for w_start, w_end in _nan_vad_windows(audio, _WHISPER_INPUT_SR):
        chunk = audio[int(w_start * _WHISPER_INPUT_SR):int(w_end * _WHISPER_INPUT_SR)]
        if not len(chunk):
            continue
        if use_mlx:
            text = _call_with_ssl_retry(
                _mlx.transcribe, chunk, path_or_hf_repo=_repo,
                language=_BREEZE_WHISPER_LANG, condition_on_previous_text=False,
                word_timestamps=False)["text"].strip()
        else:
            segs, _info = model.transcribe(chunk, language=_BREEZE_WHISPER_LANG, **_FW_NAN_KW)
            text = "".join(x.text for x in segs).strip()
        if text:
            out.append({"start": w_start, "end": w_end, "text": text})
        if progress_cb:
            progress_cb(w_end)
    return out


def _drop_stuck_segments(segments, loose=False):
    """過濾解碼器卡死產生的可疑段落：duration 過長但文字過短。
    典型症狀：單一段橫跨數十分鐘只吐一個短詞（如「都可以」）。
    loose=True：低音量音源段落本身常較長，門檻拉高避免誤殺真實短語。"""
    threshold = 90.0 if loose else 30.0
    out = []
    for s in segments:
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            dur = 0
        text = (s.get("text") or "").strip()
        has_cjk = any('぀' <= ch <= '鿿' or '가' <= ch <= '힯' for ch in text)
        too_short = (len(text) <= 6) if has_cjk else (len(text.split()) <= 4)
        if dur > threshold and too_short:
            continue
        out.append(s)
    return out


def _analyze_audio_loudness(wav_path, sample_seconds=120):
    """用 ffmpeg volumedetect 取得 mean_volume / max_volume（dBFS）。
    僅分析開頭 sample_seconds 秒（預設 120s）以加速長音檔處理 —— 監視器/低音量錄音
    特性整段一致，取樣已足以判斷；對 large-v3-turbo 本機使用者尤其重要。
    回傳 {'mean_dbfs': float, 'max_dbfs': float} 或 None（偵測失敗）。"""
    try:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner"]
        if sample_seconds and sample_seconds > 0:
            cmd += ["-t", str(int(sample_seconds))]
        cmd += ["-i", wav_path, "-af", "volumedetect",
                "-vn", "-sn", "-dn", "-f", "null", "-"]
        # ffmpeg 輸出可能含 UTF-8 中文（檔名、音檔標籤），Windows 預設以 cp950 解碼會失敗，
        # 導致音量分析被略過、低音量錄音不會啟用增益
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace", **_SUBPROCESS_FLAGS)
        out = result.stderr or ""
        m_mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", out)
        m_max = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", out)
        if not m_mean:
            return None
        return {
            "mean_dbfs": float(m_mean.group(1)),
            "max_dbfs": float(m_max.group(1)) if m_max else 0.0,
        }
    except Exception as e:
        # 以前不說：低音量錄音就不會增益、也不會切寬鬆模式，大量漏段卻看不出原因（2026-10-05）
        print(f"  [音源分析] 失敗（{type(e).__name__}: {e}），以標準模式辨識；音量很低的錄音可能漏段", flush=True)
        return None


def _boost_audio_if_quiet(wav_path, mean_dbfs, target_dbfs=-18.0, max_gain=20.0):
    """音量偏低時提升至目標音量。
    回傳 (處理後路徑, 是否新建暫存檔)。max_volume 留 ~3 dB headroom 避免削峰。
    僅對 mean_volume < -30 dBFS 觸發；其餘維持原檔。
    使用 PCM s16le 避免重編碼 overhead，對長音檔處理時間可降至 1/5。"""
    if mean_dbfs >= -30.0:
        return wav_path, False
    gain = min(target_dbfs - mean_dbfs, max_gain)
    if gain <= 0:
        return wav_path, False
    base, ext = os.path.splitext(wav_path)
    out_path = f"{base}.boosted.wav"
    try:
        # PCM s16le + 同 SR/channel：純樣本級增益，速度約等於 I/O 上限
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-y", "-i", wav_path, "-af", f"volume={gain:.1f}dB",
             "-c:a", "pcm_s16le", out_path],
            capture_output=True, check=True, timeout=900,
        )
        return out_path, True
    except Exception:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        return wav_path, False


def _release_gpu_resources():
    """每檔離線處理結束後主動釋放 GPU 資源。
    避免 CTranslate2 / cuDNN / PyTorch 等 native lib 在連續呼叫後累積 state，
    造成 Windows 上 STATUS_STACK_BUFFER_OVERRUN (0xC0000409) 等 fast-fail 崩潰。
    對 mlx-whisper / CPU 路徑為 no-op。"""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception:
        pass


def _audio_profile(wav_path, label="", min_size_bytes=200_000):
    """分析音源並回傳 (處理後路徑, 是否新建暫存檔, use_loose, mean_dbfs)。
    對乾淨錄音完全不介入；只在 mean_volume < -30 dBFS 時切換寬鬆模式 + 增益。
    極短/極小音檔（< 200KB）跳過分析以避免不必要的 ffmpeg 啟動成本。"""
    try:
        if os.path.getsize(wav_path) < min_size_bytes:
            return wav_path, False, False, None
    except OSError:
        return wav_path, False, False, None
    info = _analyze_audio_loudness(wav_path)
    if not info:
        return wav_path, False, False, None
    mean_v = info["mean_dbfs"]
    use_loose = mean_v < -30.0
    proc_path, boosted = (_boost_audio_if_quiet(wav_path, mean_v) if use_loose
                          else (wav_path, False))
    tag = f"{label} " if label else ""
    if use_loose:
        gain = min(-18.0 - mean_v, 20.0)
        boost_msg = f"，增益 +{gain:.1f} dB" if boosted else "（增益失敗，原檔送辨識）"
        print(f"  {C_DIM}[音源分析] {tag}mean_volume={mean_v:.1f} dBFS → 寬鬆模式{boost_msg}{RESET}")
    else:
        print(f"  {C_DIM}[音源分析] {tag}mean_volume={mean_v:.1f} dBFS → 標準模式{RESET}")
    return proc_path, boosted, use_loose, mean_v

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

# v2.26.3：WebUI 改用旗標檔通知暫停／繼續。SIGUSR1 在 Windows 不存在，WebUI 的暫停在 Windows 從來沒有作用；
# 而且它是「切換」，漏一次之後狀態就永遠相反。旗標是明確的狀態，只在旗標「變化」時才動作，
# 不會蓋掉終端機的 Ctrl+P。SIGUSR1 保留給舊版 webui.py
_WEBUI_PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".webui_pause")


def _start_webui_pause_watch():
    try:
        os.remove(_WEBUI_PAUSE_FLAG)          # 上一次沒收乾淨的旗標，不可以一開始就是暫停
    except OSError:
        pass

    def _watch():
        last = False
        while True:
            now = os.path.exists(_WEBUI_PAUSE_FLAG)
            ev = _webui_pause_event
            if ev is not None and now != last:
                if now:
                    ev.set()
                else:
                    ev.clear()
                last = now
            time.sleep(0.25)
    threading.Thread(target=_watch, daemon=True, name="webui-pause").start()

# ─── WebUI Event System ──────────────────────────────────────────
# --webui 啟動時透過 TCP socket 將事件推送到 webui.py
# 不啟用時 _webui_send() 是 no-op，零效能影響
_webui_queue = None  # queue.Queue，啟用時才建立
_WEBUI_PORT = int(os.environ.get("JTLW_WEBUI_EVENT_PORT") or 19780)   # 環境變數只給測試用（e2e 不跟別的 WebUI 搶 19780）


def _webui_send_realtime_results(log_path=None, rec_paths=None):
    """即時模式停止時，送出結果檔案清單給 WebUI"""
    files = []
    if log_path and os.path.isfile(log_path):
        rel = os.path.relpath(log_path, os.path.dirname(os.path.abspath(__file__)))
        files.append({"name": os.path.basename(log_path), "path": rel})
    for rp in (rec_paths or []):
        if rp and os.path.isfile(rp):
            rel = os.path.relpath(rp, os.path.dirname(os.path.abspath(__file__)))
            files.append({"name": os.path.basename(rp), "path": rel})
    if files:
        _webui_send({"type": "output_files", "files": files, "dirs": []})


_subtitle_forwarder = None  # SubtitleForwarder 實例（啟用時才建立）
_keyword_monitor = None     # KeywordMonitor 實例（啟用時才建立）


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
    # 字幕轉發：餵入即時辨識結果
    if event.get("type") == "transcription" and _subtitle_forwarder is not None:
        _subtitle_forwarder.feed(event)
    # 關鍵字通知：檢查是否匹配
    if event.get("type") == "transcription" and _keyword_monitor is not None:
        _keyword_monitor.check(event)


def _webui_flush(timeout=3.0):
    """等送事件的執行緒把佇列送完（最多 timeout 秒）。結束前呼叫：最後一個事件通常是檔案清單，
    以前程式結束得比送出快時 WebUI 就看不到錄音檔在哪（v2.26.4）"""
    q = _webui_queue
    if q is None:
        return
    end = time.monotonic() + timeout
    while not q.empty() and time.monotonic() < end:
        time.sleep(0.05)
    time.sleep(0.2)                     # 取出佇列之後還要 sendall


def _start_webui_sender():
    """啟動 WebUI TCP sender daemon thread"""
    global _webui_queue
    import queue as _q
    _webui_queue = _q.Queue(maxsize=500)
    import atexit as _atexit
    _atexit.register(_webui_flush)

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


# ─── SSL 容錯（企業網路 SSL 中間人憑證）───
import ssl as _ssl
_ssl_ctx_noverify = _ssl.create_default_context()
_ssl_ctx_noverify.check_hostname = False
_ssl_ctx_noverify.verify_mode = _ssl.CERT_NONE

def _urlopen_safe(req, timeout=10):
    """urlopen with SSL fallback（SSL 驗證失敗時自動停用驗證重試）"""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        if "SSL" in str(e) or "CERTIFICATE" in str(e).upper():
            return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx_noverify)
        raise

# ─── 字幕轉發（Telegram / Slack / Discord / Teams / 自訂 API）───

class SubtitleForwarder:
    """即時字幕聚合轉發器：每 N 秒將累積字幕發送到通訊平台"""

    def __init__(self, config: dict):
        self._interval = max(5, config.get("interval", 10))
        self._platforms = config.get("platforms", {})
        self._inc_ts = config.get("include_timestamp", False)
        self._inc_src = config.get("include_source", True)
        self._inc_dst = config.get("include_translation", True)
        self._buffer = []
        self._lock = threading.Lock()
        self._timer = None
        self._active = True
        self._schedule()

    def feed(self, event: dict):
        with self._lock:
            self._buffer.append((
                event.get("timestamp", ""),
                event.get("src_lang", ""),
                event.get("src_text", ""),
                event.get("dst_lang", ""),
                event.get("dst_text", ""),
            ))

    def _schedule(self):
        if not self._active:
            return
        self._timer = threading.Timer(self._interval, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        with self._lock:
            lines = self._buffer[:]
            self._buffer.clear()
        if lines:
            text = self._format(lines)
            for name, cfg in self._platforms.items():
                if cfg.get("enabled") and text:
                    threading.Thread(target=self._send, args=(name, cfg, text),
                                     daemon=True).start()
        if self._active:
            self._schedule()

    def _format(self, lines):
        parts = []
        for ts, sl, st, dl, dt in lines:
            ts_prefix = f"[{ts.strip('[]')}] " if self._inc_ts and ts else ""
            seg = []
            if self._inc_src and st:
                seg.append(f"{ts_prefix}{st}")
            if self._inc_dst and dt:
                seg.append(f"{ts_prefix}{dt}")
            # 至少輸出一行（都沒勾時輸出原文）
            if not seg and st:
                seg.append(f"{ts_prefix}{st}")
            if seg:
                parts.append("\n".join(seg))
        return "\n\n".join(parts)

    def _send(self, platform, cfg, text):
        try:
            if platform == "telegram":
                self._send_telegram(cfg, text)
            elif platform == "slack":
                self._send_webhook(cfg["webhook_url"], {"text": text})
            elif platform == "discord":
                # Discord 2000 字元限制
                for chunk in self._chunk_text(text, 1990):
                    self._send_webhook(cfg["webhook_url"], {"content": chunk})
            elif platform == "teams":
                self._send_webhook(cfg["webhook_url"], {"text": text})
            elif platform == "line":
                self._send_line(cfg, text)
            elif platform == "nctalk":
                self._send_nctalk(cfg, text)
            elif platform == "custom":
                self._send_custom(cfg, text)
        except Exception as e:
            print(f"  [字幕轉發] {platform} 發送失敗: {e}", file=sys.stderr, flush=True)

    def _send_telegram(self, cfg, text):
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        data = json.dumps({"chat_id": cfg["chat_id"], "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        _urlopen_safe(req)

    def _send_webhook(self, url, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        _urlopen_safe(req)

    def _send_line(self, cfg, text):
        """LINE Messaging API push message"""
        url = "https://api.line.me/v2/bot/message/push"
        payload = {
            "to": cfg["target_id"],
            "messages": [{"type": "text", "text": text}]
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['channel_access_token']}"
        })
        _urlopen_safe(req)

    def _send_nctalk(self, cfg, text):
        """Nextcloud Talk OCS API 發送訊息"""
        base = cfg["url"].rstrip("/")
        room = cfg["room_token"]
        url = f"{base}/ocs/v2.php/apps/spreed/api/v1/chat/{room}"
        data = json.dumps({"message": text}).encode("utf-8")
        # Basic auth
        import base64 as _b64
        cred = _b64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {cred}",
            "OCS-APIRequest": "true"
        })
        _urlopen_safe(req)

    def _send_custom(self, cfg, text):
        body_tpl = cfg.get("body_template", "")
        if body_tpl and "{{text}}" in body_tpl:
            # JSON 模板：替換 {{text}} 並自動設 Content-Type
            # 需要 JSON-escape 文字內容（處理換行、引號等）
            escaped = json.dumps(text)[1:-1]  # 去掉外層引號，保留轉義
            body = body_tpl.replace("{{text}}", escaped).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
        else:
            body = text.encode("utf-8")
            headers = {"Content-Type": "text/plain; charset=utf-8"}
        headers.update(cfg.get("headers", {}))
        req = urllib.request.Request(cfg["url"], data=body,
                                     headers=headers, method="POST")
        _urlopen_safe(req)

    @staticmethod
    def _chunk_text(text, max_len):
        while len(text) > max_len:
            # 在換行處切割
            idx = text.rfind("\n", 0, max_len)
            if idx <= 0:
                idx = max_len
            yield text[:idx]
            text = text[idx:].lstrip("\n")
        if text:
            yield text

    def reload(self, config: dict):
        self._interval = max(5, config.get("interval", 10))
        self._platforms = config.get("platforms", {})
        self._inc_ts = config.get("include_timestamp", False)
        self._inc_src = config.get("include_source", True)
        self._inc_dst = config.get("include_translation", True)

    def stop(self):
        self._active = False
        if self._timer:
            self._timer.cancel()


def _init_subtitle_forwarder():
    """從 config.json 初始化字幕轉發器"""
    global _subtitle_forwarder
    try:
        fwd_cfg = _config.get("subtitle_forward", {})
        if fwd_cfg.get("enabled"):
            has_active = any(p.get("enabled") for p in fwd_cfg.get("platforms", {}).values())
            if has_active:
                _subtitle_forwarder = SubtitleForwarder(fwd_cfg)
                _plat_names = [n for n, p in fwd_cfg.get("platforms", {}).items() if p.get("enabled")]
                print(f"  [字幕轉發] 已啟用：{', '.join(_plat_names)}，間隔 {fwd_cfg.get('interval', 10)} 秒")
    except Exception as e:
        print(f"  [字幕轉發] 初始化失敗: {e}", file=sys.stderr)


# ─── 關鍵字即時通知 ──────────────────────────────────────

class KeywordMonitor:
    """即時辨識結果關鍵字比對，匹配時推送通知事件到 WebUI / 懸浮字幕"""

    def __init__(self, config: dict):
        self._keywords = [k.strip().lower() for k in config.get("keywords", []) if k.strip()]
        self._cooldown = max(5, config.get("cooldown", 30))
        self._browser_notify = config.get("browser_notify", True)
        self._sound = config.get("sound", True)
        self._overlay_flash = config.get("overlay_flash", True)
        self._last_fired = {}  # keyword → timestamp

    def check(self, event: dict):
        if not self._keywords:
            return
        src = (event.get("src_text") or "").lower()
        dst = (event.get("dst_text") or "").lower()
        text = src + " " + dst
        now = time.monotonic()

        for kw in self._keywords:
            if kw in text:
                # 冷卻檢查
                if kw in self._last_fired and now - self._last_fired[kw] < self._cooldown:
                    continue
                self._last_fired[kw] = now
                # 取上下文（原文優先，沒有則用譯文）
                context = event.get("src_text") or event.get("dst_text") or ""
                ts = event.get("timestamp", "")
                _webui_send({
                    "type": "keyword_alert",
                    "keyword": kw,
                    "context": context,
                    "timestamp": ts,
                    "browser_notify": self._browser_notify,
                    "sound": self._sound,
                    "overlay_flash": self._overlay_flash,
                })


def _init_keyword_monitor():
    """從 config.json 初始化關鍵字通知"""
    global _keyword_monitor
    try:
        kw_cfg = _config.get("keyword_alert", {})
        if kw_cfg.get("enabled") and kw_cfg.get("keywords"):
            _keyword_monitor = KeywordMonitor(kw_cfg)
            print(f"  [關鍵字通知] 已啟用：{len(kw_cfg['keywords'])} 個關鍵字，冷卻 {kw_cfg.get('cooldown', 30)} 秒")
    except Exception as e:
        print(f"  [關鍵字通知] 初始化失敗: {e}", file=sys.stderr)


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
# 2026-09-18 三方交錯 A/B（同一份 63 分鐘逐字稿跑三輪、每輪對調順序）後改為 qwen3.8:27b：
# 記憶體 17.7GB（gpt-oss:120b 要 65GB）、三輪都比它快、摘要內容還更多，
# 人名與關鍵數字的正確性三輪全對。
#
# **這個常數同時決定逐字稿校正用哪個模型**（離線流程 llm_model=summary_model），
# 所以也用 342 段有標準答案的語料驗過校正（tools/correction_corpus/）：
#   qwen3.8:27b   每 100 行語意被改 中 10.5 / 日 13.2 / 英 0.9，CER 中 11.57→9.82、英 9.67→9.48
#   gemma4:26b    22.8 / 16.7 / 7.9，CER 中 →11.0、英 →9.71（變差）
#   gpt-oss:120b  20.2 / 17.5 / 18.4，CER 中 →11.07、英 →10.81（明顯變差）
# gpt-oss:120b 保留為可選項目（既有使用者相容），但不建議用於校正。
SUMMARY_DEFAULT_MODEL = "qwen3.8:27b"
_BUILTIN_SUMMARY_MODELS = [
    ("qwen3.8:27b", "推薦：摘要與校正實測最準，約 18 GB"),
    ("glm-4.7-flash:q8_0", "摘要速度最快、內容較精簡；校正未實測"),
    ("gpt-oss:120b", "約 65 GB；校正會讓英文逐字稿變差，不建議"),
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
# 伺服器沒有預設摘要模型時的備援順序。
# v2.20.0 把預設從 gpt-oss:120b 換成 qwen3.8:27b，既有使用者的伺服器上可能還沒有
# 新模型；沒有這層保護，非互動路徑（CLI --input、WebUI）的摘要會直接失敗。
# 翻譯模型早就有同樣的機制（_TRANSLATE_MODEL_FALLBACKS），摘要漏掉了。
# 備援順序刻意把 gpt-oss:120b 放第二：它的校正品質不好（見下），但它是 v2.19 以前的
# 預設，既有使用者的伺服器上幾乎一定有，退到它至少能動。glm-4.7-flash 的校正沒實測過，
# 不該排在有實測資料的模型前面。
_SUMMARY_MODEL_FALLBACKS = (SUMMARY_DEFAULT_MODEL, "gpt-oss:120b",
                            "glm-4.7-flash:q8_0", "gpt-oss:20b")

# 分段門檻的保底值（查不到模型 context window 時使用）
SUMMARY_CHUNK_FALLBACK_CHARS = 6000
# prompt 模板 + 回應預留的 token 數（不算逐字稿本身）
SUMMARY_PROMPT_OVERHEAD_TOKENS = 2000
# 每批的絕對上限：模型宣告的 context 不等於伺服器實際配置的（Ollama 的 OLLAMA_CONTEXT_LENGTH
# 可能小很多），照宣告值送會在輸出寫到一半被截斷且沒有任何錯誤訊息。
# 實測：gemma4:26b 宣告 262144，Ollama 實際 131072 → 43KB 逐字稿一次送，校正逐字稿只吐出 7% 就斷在句中
SUMMARY_CHUNK_CEILING_CHARS = 12000

SUMMARY_PROMPT_TEMPLATE = """\
你是專業的會議記錄整理員。請根據以下即時轉錄的逐字稿，完成兩件事：

1. **重點摘要**：列出 5-10 個重點，每個重點用一句話概述。
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的段落文字。合併斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。**必須完整輸出所有內容，嚴禁以「以下略」「篇幅限制」「內容省略」等理由截斷或跳過任何段落。** 話題轉換時必須換段（空一行），每段約 3-8 句，嚴禁整篇輸出成一個段落。

輸出格式：

## 重點摘要

- 重點一
- 重點二
...

## 校正逐字稿

（整理成流暢段落的純文字逐字稿，不要使用 markdown 格式，不要逐行列出，要合併成自然的段落。話題轉換時換段，每段空一行分隔）

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
2. **校正逐字稿**：將零碎的語音辨識結果整理成流暢、易讀的對話文字。合併同一位講者的連續斷句、修正錯字，保留原始語意，不要增刪內容。不需要保留時間戳記。**必須完整輸出所有內容，嚴禁以「以下略」「篇幅限制」「內容省略」等理由截斷或跳過任何段落。** 每位講者的每段發言約 3-8 句，過長時分成多段（每段都要標注 Speaker N）。

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
    summary_mode: "both"（摘要+逐字稿）、"summary"（只摘要）、"correct_only"（只校正）、"transcript"（純 ASR）
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
- 保持每一行原本的語言，絕對不要翻譯：英文行維持英文、日文行維持日文
- 中文行使用台灣繁體中文用語（軟體、網路、記憶體、程式、伺服器等）
- 數字、金額、日期不要更動
- 每一行只校正該行本身，不要把文字移到其他行，也不要合併或拆分行
- 專有名詞維持英文原文
- 直接輸出結果，不要使用 <think> 標籤或任何思考過程
{topic_line}
{lines}
"""

# 英文、日文逐字稿改用英文提示詞：中文提示詞會讓模型傾向輸出中文
# （實測 gpt-oss:120b 即使被要求「不要翻譯」，仍把大量英文行翻成中文）
TRANSCRIPT_CORRECT_PROMPT_TEMPLATE_EN = """\
You are a proofreader for speech recognition (ASR) transcripts. Fix recognition errors in the transcript lines below.

Rules:
- Fix misrecognized words, homophones, and misspelled proper nouns (e.g. "safe" -> "Ceph", "vme" -> "VMware"); you may fix punctuation and capitalization
- NEVER translate. Every line must stay in its original language: English stays English, Japanese stays Japanese
- Do not change numbers, amounts, or dates
- Do not change sentence structure or word order
- Correct each line on its own. Do not move words to another line, and do not merge or split lines
- If a line is an obvious ASR hallucination (meaningless syllables, garbage, unrelated to the context), return "number|[雜音]"
- Each line is formatted as "number|text". Return every line in exactly the same format
- If a line needs no correction, return it unchanged
- Output only the result, no explanations and no <think> tags
{topic_line}
{lines}
"""


def _transcript_is_chinese(texts):
    """逐字稿以中文為主（CJK 字元多於拉丁字母、且沒有假名）時回傳 True"""
    joined = "".join(texts)
    if _KANA_RE.search(joined):
        return False
    cjk = len(_CJK_RE.findall(joined))
    latin = sum(1 for ch in joined if ch.isascii() and ch.isalpha())
    return cjk > latin


_CORRECT_MAX_LINES = 60   # 每批校正最多幾行
_CORRECT_PARALLEL = 2     # 同時送出幾批

# ── LLM 校正結果的把關 ──
# 模型偶爾會把英文整段翻成中文、竄改數字、混入其他文字系統的字元或控制字元、
# 把文字搬到相鄰行（實測 gpt-oss:120b 把 63 分鐘英文會議的後半段翻成中文；
# gemma4:26b 把 $625 billion 改成 "$6<tab>65 billion"、插入「成功」與西里爾字母）。
# 校正只該做小幅修字，不符合的修改一律退回原文。
_KANA_RE = re.compile(r'[\u3040-\u30ff]')
_CJK_RE = re.compile(r'[\u3400-\u9fff]')
_DIGITS_RE = re.compile(r'\d+')


def _script_profile(text):
    """回傳文字中出現的文字系統集合（latin / cjk / kana / 其他 Unicode 字母區塊）"""
    kinds = set()
    for ch in text:
        if ch.isascii():
            if ch.isalpha():
                kinds.add("latin")
            continue
        if _KANA_RE.match(ch):
            kinds.add("kana")
        elif _CJK_RE.match(ch):
            kinds.add("cjk")
        elif ch.isalpha():
            try:
                name = unicodedata.name(ch)
            except ValueError:
                name = "UNKNOWN"
            # 帶附加符號的拉丁字母（é、ü）仍算拉丁；其他字母區塊（西里爾、希臘…）各自一類
            kinds.add("latin" if name.startswith("LATIN") else name.split(" ")[0].lower())
    return kinds


_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")
# 「這個詞是某個保護詞的誤聽」的相似度門檻，見 _accept_correction
_GARBLED_TERM_RATIO = 0.6
_GLOSSARY_RELATED_RATIO = 0.5   # LLM 換上去的專有名詞跟原文的字要多像才算「有關」（Uboot → Ubuntu 是 0.545）
_GLOSSARY_PROMPT_MAX = 80        # 校正提示詞最多列幾個專有名詞（太長會擠掉逐字稿的份量）

# 講者辨識：只有 >= 這個秒數的段落才進分群。
# 1.6s 是 resemblyzer partial utterance 的長度，短於它的聲紋是補零算出來的
# （2026-09-22 實測：<1.6s 的段落標錯 63~67%，1.6~4.0s 是 0~15%）。
_DIAR_MIN_CLUSTER_SEC = 1.6
# 夠長的段落少於這個數量時不套用上面的門檻——短訪談可能整場都沒幾段夠長，
# 那時寧可收下不可靠的聲紋，也不要沒有東西可以分群。
_DIAR_MIN_CLUSTER_UNITS = 8

# 講者辨識：判斷「現場幾個人」的門檻。
# 做法是數「正規化 Laplacian 的特徵值低於這個值的個數」——近似連通塊數。
# 取代原本的 eigengap（相鄰特徵值差最大處），因為 eigengap 取的是**全域最大**
# 間隙，而前面幾個間隙天生就比較大（2 群 vs 3 群的差異本來就比 5 群 vs 6 群明顯），
# 於是系統性地低估。2026-09-22 在 AMI 保留集 16 場實測：
#   eigengap NormalizedDiff  混 13.99%（5/16 場判太少）
#   特徵值 < 0.5             混 11.81%
# 0.45 / 0.5 / 0.55 是平滑的平台不是尖峰（dev 10.06 / 8.57 / 8.30、
# test 11.62 / 11.81 / 12.00），取中間值。
#
# **這個規則只有在短段落被排除之後才成立**：同一個方法在含短段落的聲紋上
# 反而把中文那場從 30.51% 惡化到 48.10%（見 _diar_cluster_floor）。
_DIAR_EIGENVALUE_TAU = 0.5


def _diar_estimate_speakers(embeddings, refinement_opts, laplacian_type,
                            lo=2, hi=8):
    """估計講者人數：數 Laplacian 特徵值低於門檻的個數。

    這裡要自己把 affinity → refinement → Laplacian → 特徵值再算一次，
    因為 spectralcluster 沒有提供這個規則（它只支援 eigengap 的兩種變體），
    而它內部那段是私有的。算兩次的成本是一次特徵分解，可接受。
    失敗時回傳 None，呼叫端退回函式庫自己的估計。
    """
    try:
        from spectralcluster import laplacian as _lap
        from spectralcluster import utils as _u
        import numpy as _np
        aff = _u.compute_affinity_matrix(embeddings)
        for name in (refinement_opts.refinement_sequence or []):
            aff = refinement_opts.get_refinement_operator(name).refine(aff)
        lap = _lap.compute_laplacian(aff, laplacian_type=laplacian_type)
        ev, _vec = _u.compute_sorted_eigenvectors(lap, descend=False)
        n = int(_np.sum(_np.asarray(ev) < _DIAR_EIGENVALUE_TAU))
        return int(max(lo, min(hi, n)))
    except Exception as e:
        # 以前不說：改用函式庫的 eigengap，系統性偏少（中文長會議會塌成 2 人），使用者看不出原因（2026-10-05）
        print(f"  [講者辨識] 人數估計失敗（{type(e).__name__}: {e}），改用函式庫內建的估計，人數可能偏少", flush=True)
        return None



def _diar_cluster_floor(segments):
    """講者辨識：決定多長的段落才進分群，回傳秒數門檻。

    抽成獨立函式是為了測得到——判斷埋在 _diarize_segments 裡面時只能比對
    原始碼字串，而那種斷言會被註解騙過（2026-09-21 踩過，見 test_refinement_alive）。

    < 1.6s 的段落聲紋是 resemblyzer 補零算出來的，實測標錯率 63~67%；
    1.6~4.0s 只有 0~15%。但夠長的段落太少時（短訪談、幾句話的錄音）
    一律套門檻會變成沒東西可以分群，那時寧可全收。
    """
    long_n = sum(1 for s in segments
                 if s["end"] - s["start"] >= _DIAR_MIN_CLUSTER_SEC)
    return _DIAR_MIN_CLUSTER_SEC if long_n >= _DIAR_MIN_CLUSTER_UNITS else 0.3

# 校正不可憑空插入的字元（括號、引號、數學與排版符號）
_BRACKET_CHARS = set("[]{}()（）［］｛｝【】〔〕《》〈〉「」『』<>〈〉|/\\@#$%^*_~`«»‹›")

# ── 中日文「姓＋職稱」保護 ────────────────────────────
# _protected_terms() 只認得拉丁字母（靠大寫當專有名詞訊號），中日文人名完全沒有保護。
# 語料實測：日文「換掉人名或產品名」7 件、中文 2 件、英文 0 件，最嚴重的兩件是
#   超マネージャー → 上長マネージャー（換成「上司」這個普通名詞）
#   相撲主任       → SRE主任（換成職務縮寫）
# 兩者都讀得通，所以比亂碼更難發現——引用這段的結論會指向不存在的人。
_NAME_TITLE_TITLES = (r"(?:マネージャー|主任|課長|部長|係長|氏|様"
                      r"|經理|经理|副理|工程師|工程师|小姐|先生|總監|总监|組長|组长)")
_NAME_TITLE_RE = re.compile(r"([一-鿿]{1,3}|[A-Za-z]{1,6})?(" + _NAME_TITLE_TITLES + r")")
# 常見姓氏。不追求完整——用途是「把明顯不是姓的東西擋下來」，
# 漏收的姓只會少擋一件，不會誤擋（比對不到就當作不是姓＋職稱，規則不觸發）。
_SURNAMES = set("陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴徐周葉蘇莊呂江何蕭羅高潘簡朱鍾"
                "游詹胡施沈余趙盧梁顏柯孫魏翁戴范宋方鄧杜傅侯曹薛丁卓阮馬董唐溫藍石紀")
_SURNAMES |= {"佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林",
              "加藤", "吉田", "山田", "松本", "井上", "木村", "清水", "斎藤", "佐々木"}


def _ends_with_surname(s):
    """s 的結尾是不是一個姓。

    要看結尾而不是整串，因為擷取名字的 `{1,3}` 是貪婪的：
    「請程副理」會擷到「請程」而不是「程」，直接比對整串就會把
    「請程副理→請陳副理」這種正確的姓氏修正誤擋掉。
    """
    return any(s[-n:] in _SURNAMES for n in (1, 2, 3) if len(s) >= n)


def _name_title_slots(text):
    """{職稱: [依出現順序的名字, ...]}；該位置沒有名字時放 None"""
    slots = {}
    for m in _NAME_TITLE_RE.finditer(text):
        slots.setdefault(m.group(2), []).append(m.group(1))
    return slots


def _name_title_damaged(original, corrected):
    """原文的「姓＋職稱」被換成不是姓的東西、或名字整個被刪掉 → 這筆校正不可採用。

    判斷的是「替換上去的是不是一個姓」，不是「有沒有變動」——
    直覺寫法（變動就擋）會連「把聽錯的姓改對」一起擋掉，
    大マネージャー → 王マネージャー 正是我們要的修正。

    同一行可能有多個相同職稱（「張マネージャーと王マネージャーは別の人です」），
    所以必須**依出現順序配對**；只找第一個職稱會拿第二個名字去比第一個位置，
    連「原文與校正完全相同」都會被判成損壞。
    """
    corr = _name_title_slots(corrected)
    for title, names in _name_title_slots(original).items():
        new_names = corr.get(title, [])
        if len(new_names) < len(names):
            return True                       # 這個職稱整個不見了
        for old, new in zip(names, new_names):
            if old is None:
                continue                      # 原文該處本來就沒有名字，不保護
            if new is None:
                return True                   # 名字被刪掉，只剩職稱
            if new != old and not _ends_with_surname(new):
                return True                   # 換成「上長」「SRE」這種不是姓的詞
    return False
# 模型偶爾輸出的特殊空白與連字號，換回一般字元
_PUNCT_NORMALIZE = str.maketrans({"\u00a0": " ", "\u202f": " ", "\u2007": " ",
                                  "\u2010": "-", "\u2011": "-"})


# 呼叫端送來的專有名詞常常一行寫好幾個（`Proxmox VE / PVE`、`王經理、李主任`）。
# 整行當一個詞永遠比對不到（v2.26.7 以前就是這樣：`Proxmox` 從來不在清單裡）。
# 「/」兩邊至少一邊有空白才拆：`TCP/IP`、`I/O` 這種本身就是一個詞
_GLOSSARY_SPLIT_RE = re.compile(r"\s+/\s*|\s*/\s+|\s*[／、，,;；|｜\n]\s*")


_GLOSSARY_EDGE = " \t\r\"'「」『』"


def _glossary_trim(part):
    """去頭尾空白與引號；括號只拿掉**落單的半邊**（拆開 `(Proxmox, PVE)` 剩下的）與**包住整個詞的那一對**，
    詞裡成對的括號留著。v2.26.10 前一律剝掉頭尾括號：`Proxmox (PVE)` 變成 `Proxmox (PVE`，
    附錯寫法時照表換進逐字稿的就是少了右括號的字"""
    while True:
        p = part.strip(_GLOSSARY_EDGE)
        for o, c in (("(", ")"), ("（", "）")):
            if p.startswith(o) and p.count(o) > p.count(c):
                p = p[1:]
            if p.endswith(c) and p.count(c) > p.count(o):
                p = p[:-1]
            if p.startswith(o) and p.endswith(c):
                depth = 0
                for i, ch in enumerate(p):
                    depth += (ch == o) - (ch == c)
                    if depth == 0:
                        break
                if i == len(p) - 1:            # 開頭那個括號一直到最後一個字才關上：整個詞被包住
                    p = p[1:-1]
        if p == part:
            return p
        part = p


def _glossary_parts(sources):
    """專有名詞（呼叫端一行一筆）→ 一個一個的詞：拆開一行裡的多個詞、去頭尾空白與引號、
    去掉一個字的、不分大小寫去重，保留原本的順序（ASR 提示只取前面幾個）"""
    out, seen = [], set()
    for src in sources or []:
        for part in _GLOSSARY_SPLIT_RE.split(str(src)):
            part = _glossary_trim(part)
            if len(part) < 2 or part.lower() in seen:
                continue
            seen.add(part.lower())
            out.append(part)
    return out


def _glossary_words(parts):
    """專有名詞裡的拉丁字（小寫、3 個字母以上、不是常見字）：校正把關拿它判斷
    「誤聽換成專有名詞的拼法」（Proximity → Proxmox）與「專有名詞不可被換掉」"""
    out = set()
    for part in parts or []:
        for w in _LATIN_TOKEN_RE.findall(part):
            w = w.lower()
            if len(w) >= 3 and w not in _COMMON_WORDS:
                out.add(w)
    return out


def _glossary_fix(word, candidates):
    """被換掉的字 word 是不是某個專有名詞字的誤聽（拼法相近）。
    長度下限與門檻沿用 _GARBLED_TERM_RATIO 的量測：短字光靠相似度一定會誤判（I 對 AI 是 0.667）"""
    if len(word) < 4:
        return False
    return any(len(g) >= 3 and difflib.SequenceMatcher(None, word, g).ratio() >= _GARBLED_TERM_RATIO
               for g in candidates)


def _latin_counts(text):
    return collections.Counter(w.lower() for w in _LATIN_TOKEN_RE.findall(text))


def _glossary_more(original, corrected, glossary):
    """校正後出現次數變多的專有名詞字（＝校正換上去的）"""
    co, cn = _latin_counts(original), _latin_counts(corrected)
    return {g for g in glossary if cn[g] > co[g]}


def _looks_proper(text, m):
    """原文裡這個英文字像不像專有名詞：全大寫、大小寫混合、或不在句首的大寫開頭（與 _protected_terms 同一套判斷）"""
    tok = m.group(0)
    before = text[:m.start()].rstrip()
    sentence_start = not before or before[-1] in ".?!:;\"“。！？"
    return tok.isupper() or any(c.isupper() for c in tok[1:]) or (tok[0].isupper() and not sentence_start)


def _glossary_garbled(original, corrected, glossary):
    """原文裡像某個專有名詞的字（Proximity 像 Proxmox）被換成了不相干的東西：回 True＝要擋（v2.26.8）。

    - 只看原文裡像專有名詞的字：清單裡難免有 turbo、premium 這種一般字，句首的 Medium 改成 Median 不可以被擋
    - 換上去的是另一個也算相近（0.5 以上）的專有名詞時放行：Uboot 像 turbo（0.6）、但換成 Ubuntu（0.545）是對的
    - 用出現次數判斷「換上去」：同一行本來就有 Proxmox，再把 Proximity 改成 Proxmox 也算"""
    new_words = set(_latin_counts(corrected))
    more = _glossary_more(original, corrected, glossary)
    for m in _LATIN_TOKEN_RE.finditer(original):
        w = m.group(0).lower()
        if w in new_words or w in glossary or len(w) < 4 or w in _COMMON_WORDS or not _looks_proper(original, m):
            continue
        close = [g for g in glossary if len(g) >= 3 and
                 difflib.SequenceMatcher(None, w, g).ratio() >= _GARBLED_TERM_RATIO]
        if close and not any(difflib.SequenceMatcher(None, w, g).ratio() >= _GLOSSARY_RELATED_RATIO for g in more):
            return True
    return False


def _protected_terms(texts):
    """整份逐字稿中出現兩次以上的專有名詞（全大寫縮寫，或不在句首的大寫開頭詞），回傳 {小寫: 次數}。
    校正時不可把它們改成別的詞（例如 Ceph 被改成 Cef、人名 Ida 被改成 I）；
    改成另一個更常出現的專有名詞則視為修正誤聽（TIA → TI）"""
    counts = {}
    for text in texts:
        for m in _LATIN_TOKEN_RE.finditer(text):
            tok = m.group(0)
            before = text[:m.start()].rstrip()
            sentence_start = not before or before[-1] in ".?!:;\"“"
            if tok.lower() in _COMMON_WORDS or tok == "I":
                continue
            if (len(tok) >= 2 and tok.isupper()) or (not sentence_start and tok[0].isupper()):
                counts[tok.lower()] = counts.get(tok.lower(), 0) + 1
    return {w: c for w, c in counts.items() if c >= 2}


def _normalize_correction(text):
    return text.translate(_PUNCT_NORMALIZE)


def _accept_correction(original, corrected, protected=None, glossary=None):
    """判斷 LLM 校正後的單行文字能不能採用（protected：不可刪改的專有名詞，小寫；
    glossary：呼叫端給的專有名詞拉丁字，小寫，見 _glossary_words）"""
    if corrected == original or corrected == "[雜音]":
        return True
    if not corrected.strip():
        return False
    # 控制字元（tab 等）、位元組殘片（<0xA0>）、連續空白、原文沒有的反斜線（LaTeX 之類）
    if any(ord(ch) < 32 for ch in corrected):
        return False
    if re.search(r"<0x[0-9A-Fa-f]{2}>", corrected) or "   " in corrected:
        return False
    if "\\" in corrected and "\\" not in original:
        return False
    # 憑空長出來的括號／符號（實測 gemma4 把「スナップショット」寫成「スナップ］ショット」）。
    # 校正只該補一般標點，插入括號類字元一定是雜訊或改寫
    if (set(corrected) - set(original)) & _BRACKET_CHARS:
        return False
    # 專有名詞不可被刪改；只能換成另一個出現次數更多的專有名詞（修正誤聽）
    glossary = glossary or set()
    if glossary:
        # 呼叫端給的專有名詞本身不可被換掉（VMware → Proxmox 這種兩個專有名詞互換），v2.26.8
        co, cn = _latin_counts(original), _latin_counts(corrected)
        if any(cn[g] < co[g] for g in glossary):
            return False
        # 換上英文專有名詞時，原文必須有被換掉的英文字（聽錯的拼法：safe → Ceph、Proximity → Proxmox）。
        # 從中日文換過來的等於翻譯或硬塞，擋（校正語料實測：新竹 → Hsinchu、林エンジニア → Engineer Lin、
        # セフ → Ceph，答案就是原文的寫法）
        if _glossary_more(original, corrected, glossary) and not any(cn[w] < co[w] for w in co):
            return False
        # 原文裡像某個專有名詞的字被換成不相干的東西（Proximity → VMware）
        if _glossary_garbled(original, corrected, glossary):
            return False
    if protected:
        orig_words = {w.lower() for w in _LATIN_TOKEN_RE.findall(original)}
        new_words = {w.lower() for w in _LATIN_TOKEN_RE.findall(corrected)}
        removed = (orig_words & protected.keys()) - new_words
        added = {w for w in new_words - orig_words if w in protected}
        more_glossary = _glossary_more(original, corrected, glossary) if glossary else set()
        for w in removed:
            if any(protected[a] > protected[w] for a in added):
                continue
            # 聽錯的字在整份逐字稿出現兩次以上，就會被當成「聽對的專有名詞」保護起來；
            # 換成拼法相近的專有名詞（Proximity → Proxmox）是修正誤聽，要放行（v2.26.8）
            if _glossary_fix(w, more_glossary):
                continue
            return False
        # 辨識聽壞的專有名詞，被換成「別的」詞。
        # 上面那條看不到它：聽壞的詞（Groxmoxity）不在保護清單裡，交集是空的——
        # 我們保護了辨識聽對的專有名詞，對聽壞的卻一條規則都沒有，
        # 而那正是模型最會拿另一個真實產品名去填的時候（實測 Groxmoxity → Ceph、
        # DGBX Spark → Databricks）。
        # 判斷方式：消失的詞如果明顯是某個保護詞的誤聽，校正後就必須出現那個保護詞。
        # 門檻 0.6 是量出來的——誤聽版本落在 0.62～0.86，
        # 而該放行的同音修正（safe → Ceph）最高只有 0.44。
        # 長度下限是必要的：實測「I」對保護詞「AI」的相似度是 0.667，
        # 短 token 光靠相似度一定會誤判（第一版就把一句正常的校正擋掉了）。
        for w in orig_words - new_words:
            if len(w) < 4 or w in protected or w in _COMMON_WORDS:
                continue
            # 換成拼法相近的專有名詞（呼叫端給的）優先：同一個詞有兩種聽錯的寫法時，較少的那種像出現較多次的那種
            # （被當成專有名詞保護），但那種本身也是聽錯的；換成清單上的正確寫法是對的（v2.26.9，JTDT 回報）
            if _glossary_fix(w, more_glossary):
                continue
            for p in protected:
                if len(p) >= 3 and p not in new_words and \
                        difflib.SequenceMatcher(None, w, p).ratio() >= _GARBLED_TERM_RATIO:
                    return False
    # 中日文人名：_protected_terms 只看得到拉丁字母，姓＋職稱要另外擋
    if _name_title_damaged(original, corrected):
        return False
    # 行內插入的雜音標記（整行雜音只能是 "[雜音]"）
    if "[雜音]" in corrected:
        return False
    # 文字系統：不可出現原文沒有的文字系統（英文行出現中文 = 被翻譯；出現西里爾字母 = 亂碼）
    extra = _script_profile(corrected) - _script_profile(original)
    # 中文行補上英文專有名詞（safe → Ceph）是正常的校正
    if "cjk" in _script_profile(original):
        extra.discard("latin")
    if extra:
        return False
    # 數字不可更動（原文有數字時，校正後的數字序列必須相同）
    orig_digits = _DIGITS_RE.findall(original)
    if orig_digits and _DIGITS_RE.findall(corrected) != orig_digits:
        return False
    # 拉丁文字行的字數不可大增（重複插入片語，例如 "in Hong and you lived in Hong Kong"）
    n_orig, n_new = len(original.split()), len(corrected.split())
    if "cjk" not in _script_profile(original) and n_new - n_orig > max(2, n_orig * 0.2):
        return False
    # 改動幅度：長度或相似度差太多，多半是改寫或把相鄰行的內容搬進來
    if not (0.6 <= len(corrected) / max(len(original), 1) <= 1.6):
        return False
    if difflib.SequenceMatcher(None, original, corrected).ratio() < 0.5:
        return False
    return True


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]{2,}")
# 校正時常見的補字（冠詞、連接詞等），不當作「從鄰行搬來的內容」
_COMMON_WORDS = {
    "the", "and", "but", "for", "nor", "yet", "you", "are", "was", "were", "has", "had", "have",
    "that", "this", "with", "from", "they", "their", "there", "then", "than", "its", "it's",
    "what", "which", "who", "will", "would", "can", "could", "not", "all", "any", "our", "your",
}


def _content_tokens(text):
    """比對跨行搬移用：拉丁字母取 3 字以上的單字，中日文取相鄰兩字"""
    toks = {w.lower() for w in _WORD_RE.findall(text)}
    for run in re.findall(r'[\u3040-\u30ff\u3400-\u9fff]{2,}', text):
        toks.update(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def _moved_between(orig_a, corr_a, orig_b, corr_b):
    """相鄰兩行 a、b：a 少掉的內容出現在 b 新增的內容裡（或反過來），視為被搬到別行"""
    lost_a = _content_tokens(orig_a) - _content_tokens(corr_a)
    lost_b = _content_tokens(orig_b) - _content_tokens(corr_b)
    gained_a = _content_tokens(corr_a) - _content_tokens(orig_a)
    gained_b = _content_tokens(corr_b) - _content_tokens(orig_b)
    return bool(lost_a & gained_b) or bool(lost_b & gained_a)


# 場景名稱對照（CLI 用）
SCENE_MAP = {"meeting": 0, "training": 1, "presentation": 2, "subtitle": 3}
MODE_MAP = {key: i for i, (key, _, _) in enumerate(MODE_PRESETS)}
APP_NAME = f"jt-live-whisper v{APP_VERSION} - 100% 全地端 AI 語音工具箱"
APP_AUTHOR = "by Jason Cheng (Jason Tools)"


def _win_whisper_stream_problem():
    """Windows 的 whisper-stream 不能用的原因（能用回傳 None；非 Windows 一律 None）。
    - 沒有 whisper.cpp：第一次安裝一定如此，C++ 編譯器是安裝當下才裝的，要重開終端機才生效
      （2026-10-06 Win11 實機照 README 新裝後，即時字幕直接「找不到 whisper-stream」結束，v2.26.14 修）
    - 有 whisper-stream.exe 但找不到 SDL2.dll：install.ps1 從來沒把它複製到 exe 旁邊（v2.26.15 起會），
      一執行 Windows 就跳「SDL2.dll was not found」對話框，即時字幕卡到有人按確定（2026-10-06 pc-002）
    Windows 擷取系統音訊走 WASAPI，SDL2 本來就讀不到，faster-whisper 才是主要路徑；whisper-stream 能用時行為不變"""
    if not IS_WINDOWS:
        return None
    if not os.path.isfile(WHISPER_STREAM):
        return "沒有 whisper.cpp"
    dirs = [os.path.dirname(WHISPER_STREAM)] + [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    if not any(os.path.isfile(os.path.join(d, "SDL2.dll")) for d in dirs):
        return f"whisper.cpp 缺 SDL2.dll（重新執行 {_INSTALL_CMD} 可修復）"
    return None


def _win_without_whisper_stream():
    """Windows 的 whisper-stream 不能用（不存在或缺 SDL2.dll）：即時辨識一律走 Python 端 faster-whisper"""
    return _win_whisper_stream_problem() is not None


def _no_windows_error_dialogs():
    """Windows：接下來啟動的子行程缺 DLL 等載入錯誤時不跳系統對話框（子行程繼承錯誤模式），直接失敗返回。
    否則對話框擋在使用者桌面，程式一直等到有人按確定（2026-10-06 whisper-stream 缺 SDL2.dll）。
    回傳還原用的函式：old = _no_windows_error_dialogs(); try: Popen(...) finally: old()"""
    if not IS_WINDOWS:
        return lambda: None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        prev = k.SetErrorMode(0)
        k.SetErrorMode(prev | 0x0001 | 0x0002 | 0x8000)   # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        return lambda: k.SetErrorMode(prev)
    except Exception:
        return lambda: None


def check_dependencies(asr_engine="whisper", translate_engine=None):
    """檢查所有必要檔案是否存在"""
    errors = []
    if asr_engine == "whisper" and _win_without_whisper_stream():
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is None:
            errors.append(f"whisper-stream 不能用（{_win_whisper_stream_problem()}），也沒有安裝 faster-whisper，"
                          f"請執行 {_INSTALL_CMD} 安裝")
    elif asr_engine == "whisper" and not IS_LINUX and not os.path.isfile(WHISPER_STREAM):
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
    # 分組標題依模式代號定位，不可寫死索引（v2.22.0 插入韓文單向模式後標題錯位）
    _group_headers = {"en2zh": "單向翻譯", "en_zh": "雙向翻譯", "en": "轉錄", "nan": "其他"}
    for i, (key, name, desc) in enumerate(MODE_PRESETS):
        if key in _group_headers:
            hdr = _group_headers[key]
            hdr_w = _dw(hdr)
            print(f"{C_DIM}{'─' * 12} {hdr} {'─' * (60 - 13 - hdr_w)}{RESET}")
        padded = name + ' ' * (col - _dw(name))
        if i == default_idx:
            print(f"  {C_HIGHLIGHT}{BOLD}[{i:>2}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        else:
            print(f"  {C_DIM}[{i:>2}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
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
    # 台語模式只有 Breeze-ASR-26 可用，不必選
    if mode in _NAN_INPUT_MODES:
        _enforce_nan_model(mode, BREEZE_MODEL, quiet=True)
        print(f"  {C_OK}→ 辨識模型：{BREEZE_MODEL}（台語專用）{RESET}\n")
        return BREEZE_MODEL, None
    # 列出所有適用模型（不限已安裝）
    candidates = []
    for name, filename, desc in WHISPER_MODELS:
        # 中文/日文模式不能用 .en 模型（僅支援英文）
        if mode in _NOENG_MODELS and name.endswith(".en"):
            continue
        path = os.path.join(MODELS_DIR, filename)
        installed = use_faster_whisper or os.path.isfile(path)
        candidates.append((name, filename, path, desc, installed))
    # 華語模式可選用 Breeze-ASR-26（固定走 Python 端 faster-whisper / mlx-whisper）
    if mode in _BREEZE_OPTIONAL_MODES:
        candidates.append((BREEZE_MODEL, "", None, "台灣華語／台語混用，較慢", True))

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
    _enforce_nan_model(mode, name, quiet=True)
    return name, (None if (use_faster_whisper or name == BREEZE_MODEL) else path)


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


def _ggml_model_file(name):
    """whisper.cpp（ggml）模型檔的路徑；沒有下載回傳 None（不像 resolve_model 會直接結束）"""
    for n, filename, _desc in WHISPER_MODELS:
        if n == name:
            p = os.path.join(MODELS_DIR, filename)
            return p if os.path.isfile(p) else None
    return None


def _win_sdl_loopback_device():
    """Windows：whisper-stream（SDL2）擷取得到系統音訊嗎？SDL2 讀不到 WASAPI Loopback，
    只有「立體聲混音」這類裝置才行。有就回傳 (id, 名稱)，沒有回傳 None（不會結束程式）。
    以前只要列得出任何 SDL2 裝置（一定有麥克風）就改走 whisper-stream，找不到 Loopback 時拿第一個裝置＝麥克風，
    即時字幕辨識的是自己的麥克風而不是會議的聲音（2026-10-08 pc-002：v2.26.15 補上 SDL2.dll、whisper-stream 第一次真的跑起來才發現）"""
    if _win_without_whisper_stream():
        return None
    probe = next((f for f in (_ggml_model_file(n) for n, _f, _d in WHISPER_MODELS) if f), None)
    if not probe:
        return None
    for dev_id, dev_name in _enumerate_sdl_devices(probe):
        if _is_loopback_device(dev_name):
            return dev_id, dev_name
    return None


def _enumerate_sdl_devices(model_path):
    """列舉 SDL2 音訊捕捉裝置（透過 whisper-stream），回傳 [(id, name), ...]"""
    if not os.path.isfile(WHISPER_STREAM) or _win_without_whisper_stream():
        return []                   # whisper-stream 不存在或缺 SDL2.dll：沒有 SDL2 裝置可列（以前丟 FileNotFoundError／跳對話框卡住）
    _restore = _no_windows_error_dialogs()
    try:
        proc = subprocess.Popen(
            [WHISPER_STREAM, "-m", model_path, "-c", "999", "--length", "1000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **_SUBPROCESS_FLAGS,
        )
    finally:
        _restore()

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
    import sounddevice as sd
    # Windows: 優先用 WASAPI Loopback（零設定擷取系統音訊）
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            print(f"  {C_OK}ASR 裝置: WASAPI Loopback ({wb_info['name']}){RESET}")
            return WASAPI_LOOPBACK_ID

    # macOS: 優先用 ScreenCaptureKit（零設定，不需 BlackHole 與多重輸出裝置）
    if IS_MACOS and _sck_supported():
        if _sck_permission():
            print(f"  {C_OK}ASR 裝置: ScreenCaptureKit 系統音訊{RESET}")
            return SCK_LOOPBACK_ID
        if _find_blackhole_device() is None:
            # 沒有 BlackHole 可退，直接引導使用者授權
            _sck_permission_hint()
        else:
            # 有 BlackHole 可退，但仍要讓使用者知道 SCK 沒啟用、以及怎麼啟用
            print(f"  {C_DIM}[提示] 未取得「螢幕錄製」權限，改用 BlackHole；"
                  f"授權後即可免設定多重輸出裝置（./start.sh --sck-permission）{RESET}")

    # Linux: 優先用 PipeWire / PulseAudio 的 monitor（零設定）
    _no_audio_hint = None
    if IS_LINUX:
        if _pulse_available():
            print(f"  {C_OK}ASR 裝置: {_pulse_label()}{RESET}")
            return PULSE_LOOPBACK_ID
        _no_audio_hint = _pulse_missing_hint()

    devices = sd.query_devices()
    input_devices = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            input_devices.append((i, dev["name"], dev["max_input_channels"], int(dev["default_samplerate"])))

    if not input_devices:
        # 錯誤與原因一起印到 stderr：WebUI 的「啟動失敗」卡片只收得到 stderr（原因以前印在 stdout，畫面上看不到）
        print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
        if _no_audio_hint:
            print(f"[提示] {_no_audio_hint}", file=sys.stderr)
        sys.exit(1)
    if _no_audio_hint:
        print(f"  {C_DIM}[提示] {_no_audio_hint}{RESET}")

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
    import sounddevice as sd
    # Windows: 優先用 WASAPI Loopback
    if IS_WINDOWS:
        wb_info = _find_wasapi_loopback()
        if wb_info:
            print(f"{C_OK}自動選擇音訊裝置: WASAPI Loopback ({wb_info['name']}){RESET}")
            return WASAPI_LOOPBACK_ID

    # macOS: 優先用 ScreenCaptureKit
    if IS_MACOS and _sck_supported():
        if _sck_permission():
            print(f"{C_OK}自動選擇音訊裝置: ScreenCaptureKit 系統音訊{RESET}")
            return SCK_LOOPBACK_ID
        if _find_blackhole_device() is None:
            _sck_permission_hint()
        else:
            print(f"{C_DIM}[提示] 未取得「螢幕錄製」權限，改用 BlackHole；"
                  f"授權後即可免設定多重輸出裝置（./start.sh --sck-permission）{RESET}")

    # Linux: 優先用 PipeWire / PulseAudio 的 monitor
    _no_audio_hint = None
    if IS_LINUX:
        if _pulse_available():
            print(f"{C_OK}自動選擇音訊裝置: {_pulse_label()}{RESET}")
            return PULSE_LOOPBACK_ID
        _no_audio_hint = _pulse_missing_hint()

    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
            print(f"{C_OK}自動選擇音訊裝置: [{i}] {dev['name']}{RESET}")
            return i
    # 找不到 Loopback，用系統預設輸入
    default = sd.default.device[0]
    if default is not None and default >= 0:
        dev = devices[default]
        if _no_audio_hint:
            print(f"{C_DIM}[提示] {_no_audio_hint}{RESET}")
        print(f"{C_HIGHLIGHT}未偵測到 {_LOOPBACK_LABEL}，使用系統預設輸入: [{default}] {dev['name']}{RESET}")
        return default
    # 錯誤與原因一起印到 stderr：WebUI 的「啟動失敗」卡片只收得到 stderr（原因以前印在 stdout，畫面上看不到；
    # 2026-10-08 使用者在 PVE LXC 開即時模式，只看到「啟動失敗」）
    print("[錯誤] 找不到任何音訊輸入裝置！", file=sys.stderr)
    if _no_audio_hint:
        print(f"[提示] {_no_audio_hint}", file=sys.stderr)
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
                     "zh2ja": self._build_prompt_zh2ja,
                     "ko2zh": self._build_prompt_ko2zh,
                     "zh2ko": self._build_prompt_zh2ko,
                     # 台語辨識結果本身就是漢字，翻英文與中翻英同一條路徑
                     "nan2en": self._build_prompt_zh2en}
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

    def _build_prompt_ko2zh(self, text, context):
        prompt = (
            "你是即時會議翻譯員，將韓文翻譯成台灣繁體中文。\n"
            "規則：\n"
            "1. 必須使用繁體中文，禁止使用簡體中文（例：用「軟體」不用「软件」，用「記憶體」不用「内存」）\n"
            "2. 使用台灣用語：軟體、網路、記憶體、程式、伺服器、資料庫、影片、滑鼠、設定、訊息\n"
            "3. 專有名詞維持原文（如 iPhone、API、Kubernetes、GitHub）；韓文人名用韓文原文或其漢字\n"
            "4. 只輸出一行繁體中文翻譯，不要輸出原文、解釋、替代版本\n"
            "5. 只能包含繁體中文和英文，禁止輸出韓文、日文、俄文等其他語言\n"
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
                prompt += f"韓：{src}\n中：{dst}\n"
        prompt += f"\n請翻譯：{text}"
        return prompt

    def _build_prompt_zh2ko(self, text, context):
        # 跟 zh2ja 一樣用目標語言寫指示：用中文寫會讓模型傾向輸出中文
        prompt = (
            "당신은 실시간 회의 통역사입니다. 중국어를 한국어로 번역하세요.\n"
            "규칙:\n"
            "1. 자연스럽고 매끄러운 한국어로 출력할 것\n"
            "2. 고유 명사는 그대로 유지할 것 (예: iPhone, API, Kubernetes, GitHub)\n"
            "3. 번역 결과만 한 줄로 출력하고, 설명이나 다른 번역안은 쓰지 말 것\n"
            "4. 한국어만 출력하고 중국어, 일본어, 러시아어 등은 포함하지 말 것\n"
            "5. 코멘트, 괄호 주석, 번역에 관한 메모를 덧붙이지 말 것\n"
            "6. 원문이 불완전해도 그대로 번역하고 설명을 덧붙이지 말 것\n"
            "7. 번역 결과를 바로 출력하고 <think> 태그나 사고 과정을 쓰지 말 것\n"
            "8. 원문에 충실하게 번역하고 정치적 이유로 용어를 바꾸지 말 것 (국가명, 지명, 인물의 직함은 원문 그대로)\n"
        )
        if self.meeting_topic:
            prompt += f"\n회의 주제: {self.meeting_topic}\n이 주제에 관련된 전문 용어를 적절히 번역하세요.\n"
        if context:
            prompt += "\n최근 대화 맥락:\n"
            for src, dst in context:
                prompt += f"중: {src}\n한: {dst}\n"
        prompt += f"\n번역하세요: {text}"
        return prompt

    def warmup(self, max_retries=3, timeout=120):
        """預熱 LLM 模型，確保模型已載入且能正常回應（ASR 耗時可能導致模型被卸載）"""
        _test = {"en2zh": "Hello", "zh2en": "你好", "ja2zh": "こんにちは",
                 "zh2ja": "你好", "ko2zh": "안녕하세요", "zh2ko": "你好",
                 "nan2en": "你好"}.get(self.direction, "Hello")
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
        # 韓文（v2.22.0）。只加在韓文方向上，其他既有模式的判斷不動
        _has_hangul = any('\uac00' <= ch <= '\ud7a3' or '\u3130' <= ch <= '\u318f'
                          for ch in text)
        if self.direction == "ko2zh" and _has_hangul:
            return True          # 韓翻中：夾帶韓文原文是最常見的失誤
        if self.direction == "zh2ko" and len(text) >= 2 and not _has_hangul:
            return True          # 中翻韓：沒有韓文字多半是模型直接回了中文
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
            # LLM 翻譯不無條件套 S2TWP（會誤轉如「干擾→幹擾」），
            # 改由呼叫端用 _to_traditional() 偵測到簡體才轉
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
        except Exception as e:
            _note_translate_error(e)
            return _TranslateFailed(f"{type(e).__name__}: {e}")


class _TranslateFailed(str):
    """翻譯「出錯」的結果：跟空字串一樣是假值（既有的 `if not result` 照舊），但呼叫端分得出它跟
    「翻譯被過濾掉」（幻覺、亂碼，回傳一般的 ""）不同。以前兩者都是 ""，即時字幕看到就整筆略過，
    LLM 伺服器卡住或逾時時連原文都不見、也沒有任何訊息（2026-10-05）"""
    def __new__(cls, reason=""):
        obj = super().__new__(cls, "")
        obj.reason = reason
        return obj


_TRANSLATE_ERR = {"last": 0.0, "count": 0}


def _note_translate_error(e):
    """翻譯出錯時說明原因：第一次馬上說，之後同樣的狀況每 60 秒最多說一次（附這段期間失敗幾次）"""
    _TRANSLATE_ERR["count"] += 1
    now = time.monotonic()
    if _TRANSLATE_ERR["last"] and now - _TRANSLATE_ERR["last"] < 60:
        return
    n = _TRANSLATE_ERR["count"]
    _TRANSLATE_ERR["last"], _TRANSLATE_ERR["count"] = now, 0
    msg = (str(e).strip().splitlines() or [""])[0][:160]
    extra = f"（近 60 秒共 {n} 段）" if n > 1 else ""
    print(f"\n  {C_WARN}[翻譯失敗] {type(e).__name__}: {msg}{extra}；原文照樣顯示，譯文標「（翻譯失敗）」。"
          f"請檢查 LLM 伺服器（{OLLAMA_HOST}:{OLLAMA_PORT}）是否正常{RESET}", flush=True)


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
        return _s2twp_safe(" ".join(translated_parts))


class NllbTranslator:
    """使用 NLLB 600M (CTranslate2) 離線多語言翻譯"""

    _LANG_MAP = {
        "en": "eng_Latn",
        "zh": "zho_Hant",
        "ja": "jpn_Jpan",
        "ko": "kor_Hang",
    }
    _DIRECTION_MAP = {
        "en2zh": ("en", "zh"),
        "zh2en": ("zh", "en"),
        "ja2zh": ("ja", "zh"),
        "zh2ja": ("zh", "ja"),
        "ko2zh": ("ko", "zh"),
        "zh2ko": ("zh", "ko"),
        "nan2en": ("zh", "en"),
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
        if self.direction in ("en2zh", "ja2zh", "ko2zh"):
            return _s2twp_safe(result)
        return result


def _is_private_host(host):
    """判斷是不是區域網路位址（含 .local 主機名）"""
    host = (host or "").strip()
    if host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _macos_local_network_hint(host, force=False):
    """macOS 15 以後連區域網路裝置需要「本機網路」權限，未授權時連線會直接失敗
    （curl 在終端機可以連，Python 卻回 No route to host）。印出一次提示。"""
    global _LOCAL_NET_HINT_SHOWN
    if _LOCAL_NET_HINT_SHOWN or not IS_MACOS or not _is_private_host(host):
        return
    if not force and not _macos_version_at_least(15):
        return
    _LOCAL_NET_HINT_SHOWN = True
    print(f"  {C_HIGHLIGHT}[提示] macOS 需要「本機網路」權限才能連線到區域網路的伺服器（{host}）{RESET}")
    print(f"  {C_DIM}  系統設定 → 隱私權與安全性 → 本機網路 → 開啟你用來執行的終端機程式{RESET}")
    print(f"  {C_DIM}  第一次執行時會跳出授權視窗；透過 SSH 執行時不會跳出，需先在桌面授權一次{RESET}")


_LOCAL_NET_HINT_SHOWN = False


def _macos_version_at_least(major):
    """macOS 主版本是否大於等於 major"""
    if not IS_MACOS:
        return False
    try:
        return int(platform.mac_ver()[0].split(".")[0]) >= major
    except (ValueError, IndexError):
        return False


def _detect_llm_server(host, port):
    """自動偵測 LLM 伺服器類型，回傳 "ollama" / "openai" / None

    注意：不能只看 HTTP 200。LM Studio 對未實作的 endpoint 一律回 200
    （Developer Logs 會印 "Unexpected endpoint... Returning 200 anyway"），
    若只看狀態碼會把 LM Studio 的 /api/tags 誤判成 Ollama，之後改走 Ollama
    的 /api/generate 取不到 response 欄位而失敗。因此必須驗證回傳結構。
    """
    # 先嘗試 Ollama：回傳須為 {"models": [...]} 結構
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                return "ollama"
    except Exception:
        pass
    # 再嘗試 OpenAI 相容：回傳須為 {"data": [...]} 結構（LM Studio 走這條）
    try:
        req = urllib.request.Request(f"http://{host}:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return "openai"
    except Exception:
        pass
    return None


def _resolve_summary_model(model, host, port, server_type="ollama"):
    """伺服器上沒有指定的摘要模型時，依序退回備援模型；都沒有就用伺服器上的第一個。

    會印出實際使用的模型——**不可以安靜地換掉**，否則使用者以為在用 A、其實是 B。
    查不到模型清單（連線失敗等）時原樣回傳，讓後續流程自己報錯。
    """
    names = _llm_list_models(host, port, server_type)
    if not names or model in names:
        return model
    for want in _SUMMARY_MODEL_FALLBACKS:
        if want in names:
            print(f"  {C_HIGHLIGHT}[摘要模型] 伺服器沒有 {model}，改用 {want}{RESET}")
            return want
    print(f"  {C_HIGHLIGHT}[摘要模型] 伺服器沒有 {model}，改用 {names[0]}{RESET}")
    return names[0]


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
    """著色並輸出一行摘要文字。

    即時顯示也要過簡繁轉換：轉換原本只在存檔前做，畫面印的是模型的原始輸出，
    所以使用者會看著簡體字一行行閃過、最後檔案才是繁體。
    qwen 系列比 gpt-oss 更常吐簡體，換預設摘要模型之後這個落差變得明顯。

    這裡用無條件 S2TWP，與摘要存檔那一步相同——**畫面與檔案必須是同一套規則**，
    否則又變成「看到的和存下來的不一樣」。不用 _to_traditional() 是因為它整段偵測，
    對繁簡混雜的行會漏掉（摘要的輸出正是這種形狀）。
    代價是 s2twp 會把已經正確的「干擾」轉成「幹擾」，這是存檔路徑本來就有的
    已知限制，不在這裡另外處理，以免畫面與檔案再度分歧。
    """
    colored = _colorize_summary_line(_s2twp_safe(line))
    if write_lock:
        with write_lock:
            sys.stdout.write(colored + "\n")
            sys.stdout.flush()
    else:
        sys.stdout.write(colored + "\n")
        sys.stdout.flush()


# 不支援思考模式的模型（Ollama 對 think 參數回 400）。記起來，避免每次呼叫都白送一次請求
_NO_THINK_MODELS = set()


def _llm_generate(prompt, model, host, port, server_type, stream=False,
                  timeout=30, spinner=None, live_output=False, think=None,
                  on_line=None):
    """統一 LLM 生成介面，支援 Ollama 原生 API 和 OpenAI 相容 API
    think: True=啟用思考模式, False=關閉思考模式, None=不指定（由模型預設）
    on_line: 串流模式下每收到完整一行時呼叫 on_line(line_text)"""
    write_lock = getattr(spinner, '_lock', None)

    if server_type == "openai":
        url = f"http://{host}:{port}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        # OpenAI 相容：部分伺服器支援 chat_template_kwargs 關閉思考（Qwen3 等）；
        # Ollama 的 /v1 端點不吃這個，gemma4 要用 reasoning_effort="none" 才關得掉
        if think is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"
    else:
        # 預設 Ollama
        url = f"http://{host}:{port}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
        }
        # Ollama：think 必須放頂層，放進 options 會被當成未知欄位忽略
        if think is not None and (model, host, port) not in _NO_THINK_MODELS:
            payload["think"] = think

    def _send():
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def _open():
        try:
            return _send()
        except urllib.error.HTTPError as e:
            # 模型不支援思考模式時 Ollama 回 400，移除 think 欄位重送；
            # 不認得 reasoning_effort="none" 的 OpenAI 相容伺服器同樣回 400，移除後重送
            if e.code != 400:
                raise
            if payload.pop("reasoning_effort", None) is None:
                if payload.pop("think", None) is None:
                    raise
                # 這個模型不支援思考模式，記下來，之後不再送 think
                _NO_THINK_MODELS.add((model, host, port))
        return _send()

    if not stream:
        with _open() as resp:
            result = json.loads(resp.read())
            if server_type == "openai":
                return result["choices"][0]["message"]["content"].strip()
            else:
                return result["response"].strip()

    # 串流模式
    response_text = ""
    token_count = 0
    line_buf = ""  # live_output 行緩衝（用於 markdown 著色）
    with _open() as resp:
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
                    if live_output or on_line:
                        line_buf += token
                        while "\n" in line_buf:
                            out_line, line_buf = line_buf.split("\n", 1)
                            if live_output:
                                _live_output_line(out_line, write_lock)
                            if on_line:
                                on_line(out_line)
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
                    if live_output or on_line:
                        line_buf += token
                        while "\n" in line_buf:
                            out_line, line_buf = line_buf.split("\n", 1)
                            if live_output:
                                _live_output_line(out_line, write_lock)
                            if on_line:
                                on_line(out_line)
                if chunk.get("done", False):
                    break
    # 輸出殘餘緩衝
    if line_buf.strip():
        if live_output:
            _live_output_line(line_buf, write_lock)
        if on_line:
            on_line(line_buf)
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
    # **不可以用 pkill -f 'server.py --port N'**：這條指令自己的遠端 shell 命令列
    # 也含有那串字，pkill 會把自己一起殺掉，後面的指令就不會執行了。
    # `[s]` 讓 pattern 比對不到 awk 自己的命令列。
    kill_cmd = _ssh_cmd_parts(rw_cfg) + [
        f"kill $(ps aux | awk '/[s]erver\\.py --port {port}/ {{print $2}}') 2>/dev/null; sleep 0.5"]
    try:
        subprocess.run(kill_cmd, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    cmd = _ssh_cmd_parts(rw_cfg) + [
        # setsid + </dev/null 是必要的：少了它們，ssh 連線結束時服務會被 SIGHUP 帶走。
        # 外面那層 `( ... ) >/dev/null 2>&1` 也是必要的：只重導背景那個指令不夠，
        # 子殼仍握著 ssh 的 stdout/stderr，ssh 等不到 EOF 就會一直掛著——
        # 這支先前是靠下面的 timeout=30 吞掉，**每次重啟都白等 30 秒**
        # （2026-09-23 實測：包了子殼之後 1 秒返回）。
        # 伺服器有裝 systemd 單元（install.sh 的 _rw_install_unit）時改走 systemctl，
        # 由 systemd 帶起來的行程才會在主機重開或崩潰後自動回來。
        f"if [ $(id -u) = 0 ] && systemctl is-enabled --quiet jt-whisper-server@{port} 2>/dev/null; "
        f"then systemctl restart jt-whisper-server@{port}; else "
        "( cd ~/jt-whisper-server && export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH && "
        f"nohup setsid venv/bin/python3 server.py --port {port} "
        "> /tmp/jt-whisper-server.log 2>&1 < /dev/null & ) >/dev/null 2>&1; fi"
    ]
    try:
        # 不用 capture_output，讓 SSH 密碼提示可互動
        subprocess.run(cmd, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _remote_whisper_stop(rw_cfg):
    """停止伺服器 Whisper server，並關閉 SSH 多工連線。

    **不可以用 `pkill -f 'server.py --port N'`**：這條指令自己的遠端 shell
    命令列也含有那串字，pkill 會把自己一起殺掉。`_restart_remote_whisper()`
    在 v2.21.1 就改掉了，這支當時漏了（2026-09-23 補）。
    """
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    cmd = _ssh_cmd_parts(rw_cfg) + [
        f"kill $(ps aux | awk '/[s]erver\\.py --port {port}/ {{print $2}}') 2>/dev/null"]
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


def _version_tuple(v):
    """'2.21.1' → (2, 21, 1)；解析不動的部分當 0，未知版本視為最舊。
    伺服器端 remote_whisper_server.py 有一份相同的實作（兩邊獨立不互相 import）。"""
    out = []
    for part in str(v or "0").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _remote_server_health(rw_cfg, timeout=5):
    """取得 GPU 伺服器的 /health；連不上回傳 None。"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _push_server_update(rw_cfg, token, sbar=None):
    """把本機的 remote_whisper_server.py 推給 GPU 伺服器，由它驗證後自行重啟。

    伺服器端會先 py_compile、再實跑 `--selftest`，通過才換檔——
    那台機器沒有 systemd 看門狗，換上去起不來就是服務直接消失。
    回傳 (成功?, 訊息)。
    """
    import hashlib
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    src = os.path.join(SCRIPT_DIR, "remote_whisper_server.py")
    if not os.path.isfile(src):
        return False, "本機找不到 remote_whisper_server.py"
    body = open(src, "rb").read()

    def _say(msg):
        if sbar:
            sbar.set_progress(msg)
        else:
            print(f"  {C_DIM}{msg}{RESET}")

    _say(f"上傳新版（{len(body) // 1024} KB）...")
    # 用 HMAC 簽章而不是直接送密鑰：這條連線是 HTTP 不是 HTTPS，
    # 直接送 Bearer token 的話，任何能側錄封包的人都拿得到可重複使用的憑證，
    # 等於拿到那台機器的任意程式碼執行權。HMAC 讓側錄者只能重放「同一份內容」
    # （無害——那就是同一支程式），無法偽造新的 payload。
    # 與 jtlw_api 的 webhook 簽章同一套寫法。
    import hmac as _hmac
    ts = str(int(time.time()))
    sig = "v1=" + _hmac.new(token.encode("utf-8"),
                            f"{ts}.".encode("utf-8") + body,
                            hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/admin/update", data=body, method="POST",
        headers={"X-JTW-Timestamp": ts,
                 "X-JTW-Signature": sig,
                 "X-Content-Sha256": hashlib.sha256(body).hexdigest(),
                 "Content-Type": "application/octet-stream"})
    try:
        # 伺服器要跑 selftest（會載入模型相依套件），逾時要給足
        with urllib.request.urlopen(req, timeout=240) as r:
            info = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            d = {}
        code = d.get("error", f"HTTP {e.code}")
        hint = {"update_disabled": "伺服器未設定 JT_WHISPER_UPDATE_TOKEN",
                "unauthorized": "簽章不符（密鑰不同，或兩邊時鐘差超過 5 分鐘）",
                "busy": "伺服器正在執行其他作業",
                "checksum_mismatch": "上傳內容校驗不符",
                "payload_too_large": "檔案超出伺服器允許的大小",
                "downgrade_refused": "伺服器上的版本比本機新，不予降版",
                "selftest_failed": "新版在伺服器上無法啟動，已保留舊版"}.get(code, "")
        detail = hint or str(d.get("detail", ""))[:120]
        return False, f"{code}{('：' + detail) if detail else ''}"
    except Exception as e:
        return False, f"連線失敗：{str(e)[:120]}"

    if info.get("status") == "scheduled":
        # v2.21.8 起：伺服器上有作業在跑時不拒絕、而是排定，等作業做完才換。
        # 不在這裡等（可能要幾分鐘），這次作業會自動等它換完再送。
        return True, (f"伺服器有 {info.get('waiting', '?')} 件作業在跑，"
                      f"已排定做完後更新到 v{info.get('to')}")
    _say(f"伺服器驗證通過（{info.get('from')} → {info.get('to')}），重啟中...")
    # 輪詢到版本真的變了為止。**不能只看 /health 通不通**——舊進程可能還活著，
    # 那樣會把「根本沒換成功」誤判成更新完成（今天手動操作時就踩過：
    # kill 沒生效，跑的還是三天前的進程，但 /health 一切正常）。
    target = str(info.get("to") or APP_VERSION)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        time.sleep(2)
        h = _remote_server_health(rw_cfg, timeout=3)
        if h and str(h.get("version")) == target:
            return True, f"已更新到 v{target}"
        _say(f"等待伺服器重啟...（剩 {int(deadline - time.monotonic())}s）")
    return False, "重啟逾時，請登入伺服器確認服務狀態"


def _ensure_remote_server_version(rw_cfg, auto_update=True):
    """比對 GPU 伺服器與本機的版本；不一致時警告，能自動更新就更新。

    2026-09-21 之前完全沒有這個檢查：GPU 上的服務缺了 v2.20.0 的講者辨識
    時間軸修正，而它是**預設路徑**，三天沒有人發現。

    版本不一致**不會擋下作業**——伺服器舊一點通常還是能用，
    硬擋會讓人在急著用的時候完全動不了。
    """
    h = _remote_server_health(rw_cfg)
    if h is None:
        return None
    sv = h.get("version")
    if sv == APP_VERSION:
        return sv
    shown = f"v{sv}" if sv else "未知版本（v2.21.1 以前的伺服器不回報版本）"
    print(f"\n  {C_HIGHLIGHT}[版本不一致] GPU 伺服器 {shown}，本機 v{APP_VERSION}{RESET}")

    # **伺服器比本機新時不可以推上去**。多個用戶端共用同一台伺服器是常見情況
    # （SOP 有寫）；只比對「版本不同」的話，舊的用戶端會把伺服器降回舊版，
    # 接著新的用戶端又推回去——兩邊無限來回，而每次重啟都會中斷別人正在跑的辨識。
    if _version_tuple(sv) > _version_tuple(APP_VERSION):
        print(f"  {C_DIM}伺服器版本較新，維持不動；建議把本機也升級到 v{sv}{RESET}")
        return sv

    token = rw_cfg.get("update_token", "")
    if not (auto_update and token and h.get("can_update")):
        why = ("伺服器未開放遠端更新" if not h.get("can_update")
               else "本機未設定 remote_whisper.update_token")
        print(f"  {C_DIM}辨識與講者辨識仍會使用伺服器上的舊版；{why}{RESET}")
        print(f"  {C_DIM}手動更新：scp remote_whisper_server.py "
              f"{rw_cfg['host']}:~/jt-whisper-server/server.py 後重啟服務{RESET}")
        return sv

    sbar = _SummaryStatusBar(task="更新 GPU 伺服器", location=rw_cfg["host"]).start()
    try:
        ok, msg = _push_server_update(rw_cfg, token, sbar=sbar)
    finally:
        sbar.stop()
    if ok and "已排定" in msg:
        print(f"  {C_OK}[已排定] {msg}{RESET}")
        return sv
    if ok:
        print(f"  {C_OK}[完成] {msg}{RESET}")
        return APP_VERSION
    print(f"  {C_HIGHLIGHT}[更新失敗] {msg}{RESET}")
    print(f"  {C_DIM}繼續使用伺服器上的舊版{RESET}")
    return sv


_server_version_checked = set()


def _ensure_remote_server_version_once(rw_cfg):
    """同一個 session 對同一台伺服器只檢查一次（離線批次會連續處理多個檔案）"""
    key = (rw_cfg.get("host"), rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT))
    if key in _server_version_checked:
        return
    _server_version_checked.add(key)
    try:
        _ensure_remote_server_version(rw_cfg)
    except Exception as e:
        # 版本檢查失敗絕不可以害到正事
        print(f"  {C_DIM}[版本檢查略過] {str(e)[:100]}{RESET}")


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

    # 伺服器 v2.21.8 起更新會排定、等作業做完才換；排定期間離線線不收新件（回 503）。
    # 先等它換完再上傳，免得上傳完才被拒、整個檔案要重傳。
    if status.get("update_pending"):
        p = status["update_pending"]
        print(f"  {C_DIM}[等候] GPU 伺服器即將更新到 v{p.get('to', '?')}，"
              f"等 {p.get('waiting_jobs', '?')} 件作業做完；換完後自動送出{RESET}")
        if not _wait_remote_update(rw_cfg):
            print(f"  {C_HIGHLIGHT}[等候] 等不到伺服器更新完成{RESET}")
            return False
        status = _remote_whisper_status(rw_cfg) or {}

    # 伺服器 v2.21.7 起自己會排隊（一次一件），不必再問使用者要不要等：
    # 直接送出，排隊進度會從串流的 queued 事件顯示。
    # 「強制中斷」那個選項在排隊伺服器上會砍掉別人正在跑的作業，所以不再提供。
    if isinstance(status.get("queue"), dict):
        bq = status["queue"].get("batch") or {}
        n = (1 if bq.get("running") else 0) + len(bq.get("waiting") or [])
        if n:
            print(f"  {C_DIM}[排隊] 伺服器目前有 {n} 件作業，送出後會自動排隊，輪到時開始{RESET}")
        return True

    # 忙碌狀態檢查（舊版伺服器：沒有排隊，同時送會一起擠在 GPU 上）
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


class _RemoteUpdating(Exception):
    """GPU 伺服器正在更新（503 updating、或串流被重啟切斷），等它換完再送"""


def _iter_stream_lines(resp):
    """逐行讀串流（邊讀邊處理，進度才能即時顯示）；連線被切斷時轉成 _RemoteUpdating"""
    try:
        for raw in resp:
            yield raw
    except (http.client.IncompleteRead, ConnectionError, OSError) as e:
        raise _RemoteUpdating(f"串流中斷：{type(e).__name__}") from e


def _wait_remote_update(rw_cfg, progress_callback=None, max_wait=600):
    """等 GPU 伺服器把排定的更新換完、重新起來。回傳是否等到。

    伺服器 v2.21.8 起有作業在跑時不拒絕更新，而是排定、等作業做完才換；
    排定期間離線作業會收到 503。這時**等**比改用本機好：通常只要幾秒到幾分鐘，
    結果仍是 GPU 的品質。等太久（max_wait）才放棄，交給呼叫端改用本機。
    """
    deadline = time.monotonic() + max_wait
    # 單純連不上（沒看過「更新排定」）最多等 60 秒：那可能是真的停機，
    # 不能讓使用者乾等 10 分鐘才改用本機。重啟通常 10 秒內就回來。
    down_since = None
    while time.monotonic() < deadline:
        h = _remote_server_health(rw_cfg, timeout=3)
        if h and h.get("status") == "ok" and not h.get("update_pending"):
            return True
        if h is None:
            down_since = down_since or time.monotonic()
            if time.monotonic() - down_since > 60:
                return False
        else:
            down_since = None
        pend = (h or {}).get("update_pending") or {}
        msg = (f"GPU 伺服器更新中（等 {pend.get('waiting_jobs', '?')} 件作業做完後換到 "
               f"v{pend.get('to', '?')}），稍候…" if pend else "GPU 伺服器重啟中，稍候…")
        if progress_callback:
            progress_callback(msg)
        time.sleep(3)
    return False


def _remote_whisper_transcribe(rw_cfg, wav_path, model, language,
                               progress_callback=None, on_upload_done=None,
                               noisy=False, on_event=None):
    """POST 音訊到伺服器辨識（串流 NDJSON），回傳 (segments, duration, proc_time, device)。

    伺服器正在更新時（v2.21.8）等它換完再重送，最多 3 次；等不到就拋例外，
    由呼叫端改用本機。**不可以把被切斷的串流當成辨識完成**——v2.21.7 以前會那樣，
    結果是一份 0 段的逐字稿、畫面還顯示「處理完成」。
    """
    for attempt in range(3):
        try:
            return _remote_whisper_transcribe_once(
                rw_cfg, wav_path, model, language, progress_callback=progress_callback,
                on_upload_done=on_upload_done, noisy=noisy, on_event=on_event)
        except _RemoteUpdating as e:
            if attempt == 2 or not _wait_remote_update(rw_cfg, progress_callback):
                raise RuntimeError(f"GPU 伺服器更新中，等不到它恢復：{e}") from e
            if progress_callback:
                progress_callback("GPU 伺服器已恢復，重新送出…")


def _remote_whisper_transcribe_once(rw_cfg, wav_path, model, language,
                                    progress_callback=None, on_upload_done=None,
                                    noisy=False, on_event=None):
    """POST 音訊到伺服器 /v1/audio/transcriptions（串流 NDJSON），回傳 (segments, duration, proc_time, device)。
    noisy=True：用戶端音源分析判定為低音量錄音，伺服器套用寬鬆參數。"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/transcriptions"
    model = _resolve_fw_model(model, remote=True)

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

    # noisy field（用戶端音源分析判定，伺服器決定是否切換寬鬆參數）
    if noisy:
        body_parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"noisy\"\r\n\r\n"
            f"1\r\n"
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
                got_done = False
                for raw_line in _iter_stream_lines(resp):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as e:
                        # 只有被切斷的最後一行才會解不開
                        raise _RemoteUpdating("串流在一行的中間被切斷") from e
                    if on_event:
                        # 給 v3 API 用：把排隊（queued）與辨識進度（segment）即時回報給呼叫端。
                        # 回呼出錯不可以害到辨識本身
                        try:
                            on_event(event)
                        except Exception:
                            pass
                    if event["type"] == "segment":
                        # confidence / language 是伺服器 v2.19.0 起才有，舊伺服器沒有就留 None
                        segments.append({"start": event["start"], "end": event["end"],
                                         "text": event["text"],
                                         "confidence": event.get("confidence"),
                                         "language": event.get("language")})
                        duration = event.get("duration", 0)
                        if progress_callback and duration > 0:
                            pct = min(event["end"] / duration, 1.0)
                            pos = int(event["end"])
                            dur = int(duration)
                            progress_callback(f"{pct:.0%}  {pos//60}:{pos%60:02d} / {dur//60}:{dur%60:02d}")
                    elif event["type"] == "done":
                        got_done = True
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
                    elif event["type"] == "queued":
                        # 伺服器 v2.21.7 起一次跑一件，其餘排隊
                        if progress_callback:
                            w = int(event.get("waited", 0))
                            progress_callback(f"伺服器排隊中：前面還有 {event.get('ahead', '?')} 件"
                                              f"（已等 {w//60}:{w%60:02d}）")
                    elif event["type"] == "error":
                        raise RuntimeError(f"伺服器辨識錯誤: {event.get('detail', '未知錯誤')}")
                if not got_done:
                    # 伺服器在送完之前斷掉（重啟、更新、崩潰）。**不是「0 段、成功」。**
                    raise _RemoteUpdating(f"串流在完成前中斷（已收到 {len(segments)} 段）")
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
                if e.code == 503 and err_data.get("error") == "updating":
                    raise _RemoteUpdating(err_data.get("detail", "updating")) from e
                detail = err_data.get("detail", err_data.get("error", ""))
            except (json.JSONDecodeError, ValueError):
                detail = err_body[:200]
        raise RuntimeError(f"伺服器錯誤 ({e.code}): {detail or e.reason}") from e
    except urllib.error.URLError as e:
        # 連不上：可能是更新／重啟的那幾秒。交給外層等它回來（等不到才改用本機）
        raise _RemoteUpdating(f"連線失敗：{e.reason}") from e

    return segments, duration, proc_time, device


def _remote_whisper_transcribe_bytes(rw_cfg, wav_bytes, model, language, timeout=120, reject_lang=None):
    """POST 記憶體中的 WAV bytes 到伺服器 /v1/audio/transcriptions
    （即時模式用，每次 ~160KB 不需進度回報）
    回傳 (segments, full_text, proc_time)。reject_lang（雙向語音口譯，v2.28.0）：伺服器先判斷語言，是這個語言就不辨識、
    丟 _AudioRejected（舊伺服器不認得這個欄位，照常辨識）"""
    host = rw_cfg["host"]
    port = rw_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
    url = f"http://{host}:{port}/v1/audio/transcriptions"
    model = _resolve_fw_model(model, remote=True)

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

    if reject_lang:
        body_parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"reject_lang\"\r\n\r\n"
            f"{reject_lang}\r\n"
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

    if data.get("rejected"):
        raise _AudioRejected(data.get("language") or reject_lang)
    segments = data.get("segments", [])
    full_text = data.get("text", "").strip()
    proc_time = data.get("processing_time", 0)
    return segments, full_text, proc_time


class _AudioRejected(Exception):
    """GPU 伺服器判斷這段是 reject_lang 的語言、沒有辨識（雙向語音口譯：系統音訊錄到自己念的中文）"""


def _remote_diarize(rw_cfg, wav_path, segments, num_speakers=None,
                    progress_callback=None, on_upload_done=None, _attempt=0, on_event=None,
                    engine=None, info=None):
    """POST 音訊 + segments 到伺服器 /v1/audio/diarize
    回傳 (speaker_labels, proc_time) 或失敗回傳 (None, 0)。
    伺服器正在更新（503 updating、或回應被重啟切斷）時等它換完再送，最多 3 次。
    info（dict，選填）：成功時填入實際用的方法 engine（nemotron／legacy）與伺服器說明 note（v3 API 回報用）"""
    def _retry(why):
        if _attempt >= 2 or not _wait_remote_update(rw_cfg, progress_callback):
            print(f"  {C_HIGHLIGHT}[伺服器 diarize] GPU 伺服器更新中，等不到它恢復（{why}）{RESET}")
            return None, 0
        return _remote_diarize(rw_cfg, wav_path, segments, num_speakers=num_speakers,
                               progress_callback=progress_callback,
                               on_upload_done=on_upload_done, _attempt=_attempt + 1,
                               on_event=on_event, engine=engine, info=info)
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

    # engine 欄位：auto／nemotron／legacy。舊版伺服器不認得，會忽略（照舊用 resemblyzer）
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"engine\"\r\n\r\n"
        f"{engine or _diarize_engine}\r\n"
    )

    # stream 欄位（v2.21.9 起）：伺服器改回 NDJSON，排隊時送 queued 事件，
    # 呼叫端才分得出「在排隊」與「卡住」。舊版伺服器不認得這個欄位，照舊回 JSON
    body_parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"stream\"\r\n\r\n"
        f"true\r\n"
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
            if "ndjson" in resp.headers.get("Content-Type", ""):
                raw = ""
                for line in _iter_stream_lines(resp):
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get("type") in ("result", "error"):
                        raw = json.dumps(ev)
                    elif on_event:
                        try:
                            on_event(ev)
                        except Exception:
                            pass
            else:
                raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip():
            # 只收到保持連線的空白就斷了：伺服器在算完前被重啟
            return _retry("回應在完成前中斷")
        data = json.loads(raw)
    except (http.client.IncompleteRead, ConnectionError, ValueError, _RemoteUpdating) as e:
        return _retry(f"回應在完成前中斷：{type(e).__name__}")
    except urllib.error.HTTPError as e:
        if e.code == 503:
            try:
                if json.loads(e.read().decode()).get("error") == "updating":
                    return _retry("伺服器更新中")
            except Exception:
                pass
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

    # 伺服器 v2.21.7 起講者辨識會排隊，回應一開始就定成 200，
    # 排隊之後才發生的錯誤放在 error 欄位
    if data.get("error"):
        print(f"  {C_HIGHLIGHT}[伺服器 diarize] {data['error']}{RESET}")
        return None, 0
    speaker_labels = data.get("speaker_labels")
    proc_time = data.get("processing_time", 0)
    n_spk = data.get("num_speakers", 0)
    device = data.get("device", "unknown")
    used = data.get("engine")                    # 舊版伺服器沒有這個欄位
    print(f"  {C_DIM}[伺服器 diarize] {n_spk} 位講者, {proc_time}s ({device}"
          f"{', ' + ('Nemotron' if used == 'nemotron' else '現行方法') if used else ''}){RESET}")
    if data.get("note"):
        print(f"  {C_HIGHLIGHT}[伺服器 diarize] {data['note']}{RESET}")
    if info is not None:
        # 舊版伺服器（v2.23 前）沒有 engine 欄位：它只有現行方法
        note = data.get("note") or None
        reason = data.get("reason") if data.get("reason") in _DIAR_REASONS else _diar_reason(note)
        info.update(engine=used or "legacy", note=note, reason=reason if (used or "legacy") == "legacy" else None,
                    saturated=bool(data.get("saturated")))      # v2.26.5；舊版伺服器沒有這個欄位＝False
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
        _h, _p = _ask_llm_host()
        ip_input = bool(_h)
        if ip_input:
            host, port = _h, _p
            print(f"  {C_DIM}正在偵測 LLM 伺服器 ({host}:{port})...{RESET}", end=" ", flush=True)
            server_type, available_models = _check_llm_server(host, port)
            if not server_type:
                print(f"{C_HIGHLIGHT}未偵測到{RESET}")

        if not server_type:
            _nllb_ok = os.path.isdir(NLLB_MODEL_DIR)
            _argos_ok = mode == "en2zh" and os.path.isdir(ARGOS_PKG_PATH)
            _offline_opts = []
            if _nllb_ok:
                _offline_opts.append(("NLLB 本機離線", "支援中日韓英，品質一般", "nllb"))
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
        options.append(("NLLB 本機離線", "支援中日韓英，品質一般，免 LLM 伺服器", "nllb", None))
    if mode == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
        options.append(("Argos 本機離線", "僅英翻中，品質一般，免 LLM 伺服器", "argos", None))

    # 計算顯示寬度以對齊欄位
    def _dw(s):
        return sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in s)

    col = max(_dw(label) for label, *_ in options) + 2

    # 預設選 DEFAULT_TRANSLATE_MODEL（沒有時退回備援），否則第一個
    default_idx = _default_translate_index(
        [mod if eng == "llm" else None for _, _, eng, mod in options])

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
        print(f"  {C_HIGHLIGHT}[警告] LLM 伺服器無可用模型，使用預設 {DEFAULT_TRANSLATE_MODEL}{RESET}")
        return DEFAULT_TRANSLATE_MODEL

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

    default_idx = _default_translate_index([mod for _, _, mod in options])

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
                         "nan2en": ("台語轉錄+英文翻譯", "台語語音 → 轉錄成漢字並翻譯成英文"),
                         "ja2zh": ("日文轉錄+中文翻譯", "日文語音 → 轉錄並翻譯成繁體中文"),
                         "zh2ja": ("中文轉錄+日文翻譯", "中文語音 → 轉錄並翻譯成日文"),
                         "ko2zh": ("韓文轉錄+中文翻譯", "韓文語音 → 轉錄並翻譯成繁體中文"),
                         "zh2ko": ("中文轉錄+韓文翻譯", "中文語音 → 轉錄並翻譯成韓文"),
                         "en_zh": ("英中雙向轉錄+翻譯", "系統音訊(英→中) + 麥克風(中→英)，需配對兩個檔案"),
                         "ja_zh": ("日中雙向轉錄+翻譯", "系統音訊(日→中) + 麥克風(中→日)，需配對兩個檔案"),
                         "ko_zh": ("韓中雙向轉錄+翻譯", "系統音訊(韓→中) + 麥克風(中→韓)，需配對兩個檔案")}
        input_modes = [
            (k, _input_labels[k][0], _input_labels[k][1]) if k in _input_labels else (k, n, d)
            for k, n, d in MODE_PRESETS if k != "record"
        ]

        print(f"\n\n{C_TITLE}{BOLD}▎ 功能模式{RESET}")
        col = max(_dw(name) for _, name, _ in input_modes) + 2
        _input_group_headers = {"en2zh": "單向翻譯", "en_zh": "雙向翻譯", "en": "轉錄"}
        for i, (key, name, desc) in enumerate(input_modes):
            if key in _input_group_headers:
                hdr = _input_group_headers[key]
                hdr_w = _dw(hdr)
                print(f"{C_DIM}{'─' * 12} {hdr} {'─' * (60 - 13 - hdr_w)}{RESET}")
            padded = name + ' ' * (col - _dw(name))
            if i == default_mode:
                print(f"  {C_HIGHLIGHT}{BOLD}[{i:>2}] {padded}{RESET} {C_WHITE}{desc}{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
            else:
                print(f"  {C_DIM}[{i:>2}]{RESET} {C_WHITE}{padded}{RESET} {C_DIM}{desc}{RESET}")
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
        if mode_key in _NAN_INPUT_MODES:
            # 台語只有 Breeze-ASR-26 可用
            available_models.append((BREEZE_MODEL, "台語專用，輸出漢字（固定本機辨識）"))
        else:
            for name, _filename, desc in WHISPER_MODELS:
                if is_chinese and name.endswith(".en"):
                    continue
                available_models.append((name, desc))
            if mode_key in _BREEZE_OPTIONAL_MODES:
                available_models.append((BREEZE_MODEL, "台灣華語／台語混用，較慢（固定本機辨識）"))
            # Qwen3-ASR（實驗）：單向中／英／韓輸入，且這次辨識的位置跑得了才列出（不支援的地方不讓人選到）
            _qdesc = _qwen_menu_desc(mode_key, use_remote_whisper, remote_cached_models)
            if _qdesc:
                available_models.append((QWEN_MODEL, _qdesc))
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

            _h, _p = _ask_llm_host()
            if _h:
                ollama_host, ollama_port = _h, _p
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

            # 日文、韓文模式不支援 Argos（Argos 只裝了英翻中）
            if _use_argos and mode_key in ("ja2zh", "zh2ja", "ko2zh", "zh2ko"):
                _lang_nm = "韓文" if mode_key in ("ko2zh", "zh2ko") else "日文"
                print(f"  {C_HIGHLIGHT}[警告] {_lang_nm}翻譯不支援 Argos，將只做轉錄（不翻譯）{RESET}")
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
                    translate_models.append(("NLLB 本機離線翻譯", "支援中日韓英互譯，免 LLM 伺服器", "nllb"))
                if mode_key == "en2zh" and os.path.isdir(ARGOS_PKG_PATH):
                    translate_models.append(("Argos 本機離線翻譯", "僅英翻中，免 LLM 伺服器", "argos"))

                _last_tm = _config.get("last_llm_model")
                default_ollama = _default_translate_index(
                    [name if eng == "llm" else None for name, _, eng in translate_models])

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
        # 舊提示「講者超過 2 位建議指定人數以提升正確率」與 v2.21.2 實測相反（見 SOP 常見問題）
        print(f"  {C_DIM}* 指定人數會強制分成那麼多群；不確定時用自動偵測{RESET}")
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
        # 非翻譯模式時，前面未偵測 LLM 伺服器，在此靜默偵測（摘要/校正需要 LLM）
        if not need_translate and ollama_host and llm_server_type is None:
            llm_server_type, _ = _check_llm_server(ollama_host, ollama_port)
        _has_llm = llm_server_type is not None and ollama_host is not None
        if _has_llm:
            default_summarize = 0
            summarize_options = [
                ("產出摘要與校正逐字稿", "both"),
                ("只校正逐字稿（不產出摘要）", "correct_only"),
                ("只產出摘要", "summary"),
                ("不校正、不摘要（純 ASR 輸出）", "transcript"),
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

        # 選了摘要或校正 → 先確認 LLM 伺服器（若翻譯步驟未問過）→ 選摘要模型
        summary_model = SUMMARY_DEFAULT_MODEL
        _need_llm_for_output = do_summarize and summary_mode not in ("transcript",)
        if _need_llm_for_output:
            if not ollama_asked:
                default_addr = f"{ollama_host}:{ollama_port}"
                print(f"\n\n{C_TITLE}{BOLD}▎ LLM 伺服器{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"  {C_WHITE}目前設定: {default_addr}{RESET}")
                print(f"{C_DIM}{'─' * 60}{RESET}")
                print(f"{C_WHITE}按 Enter 使用目前設定，或輸入新位址（host:port）：{RESET}", end=" ")

                _h, _p = _ask_llm_host()
                if _h:
                    ollama_host, ollama_port = _h, _p

                # 偵測伺服器類型
                print(f"  {C_DIM}正在偵測 LLM 伺服器...{RESET}", end=" ", flush=True)
                llm_server_type, llm_models = _check_llm_server(ollama_host, ollama_port)
                if llm_server_type:
                    srv_label = "Ollama" if llm_server_type == "ollama" else "OpenAI 相容"
                    print(f"{C_OK}✓ {srv_label} @ {ollama_host}:{ollama_port}（{len(llm_models)} 個模型）{RESET}")
                else:
                    print(f"{C_HIGHLIGHT}未偵測到 LLM 伺服器（{ollama_host}:{ollama_port}）{RESET}")
                    print(f"  {C_HIGHLIGHT}⚠ 摘要功能需要 LLM 伺服器，請確認伺服器已啟動{RESET}")

            if summary_mode == "correct_only":
                # 校正用翻譯模型或預設摘要模型，不需選摘要模型
                summary_model = ollama_model or SUMMARY_DEFAULT_MODEL
            else:
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
        if do_summarize and summary_mode in ("both", "summary"):
            print(f"  {C_OK}  摘要模型: {summary_model}{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        if do_summarize and summary_mode in ("both", "correct_only"):
            print(f"  {C_OK}  LLM 校正: 啟用{RESET}  {C_DIM}@ {ollama_host}:{ollama_port}{RESET}")
        elif do_summarize and summary_mode == "transcript":
            print(f"  {C_OK}  輸出: 純 ASR 逐字稿{RESET}")
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

    whisper_lang = _mode_whisper_lang(mode)
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
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿", "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿", "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿",
                    "ko2zh": "韓翻中_逐字稿", "zh2ko": "中翻韓_逐字稿", "ko": "韓文_逐字稿"}
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

    _restore = _no_windows_error_dialogs()      # 缺 DLL 時直接失敗，不跳對話框卡住
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **_SUBPROCESS_FLAGS,
        )
    finally:
        _restore()

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
            _webui_send_realtime_results(log_path, [rec_path])
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
        "ko2zh": "說韓文即可看到中文翻譯",
        "zh2ko": "說中文即可看到韓文翻譯",
        "ko": "說韓文即可看到字幕",
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
                if not isinstance(result, _TranslateFailed):
                    continue                     # 被過濾掉的翻譯（幻覺、亂碼）：照舊整筆略過
                result = "（翻譯失敗）"           # 出錯：原文照樣顯示（以前連原文都不見）
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
            result = _s2twp_safe(result) if not isinstance(translator, OllamaTranslator) else _to_traditional(result)
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

                    elif mode in _JA_INPUT_MODES or mode in _KO_INPUT_MODES:
                        # 日文／韓文模式：過濾各自的幻覺（韓文 v2.22.0 起）
                        _hall = _is_ko_hallucination if mode in _KO_INPUT_MODES else _is_ja_hallucination
                        if _hall(line):
                            continue
                        if line == last_translated:
                            continue
                        if mode in ("ja", "ko"):
                            _src_c, _src_l = _MODE_LABELS[mode][0], _MODE_LABELS[mode][1]
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
                            # ja2zh／ko2zh：原文延後到翻譯完成時一起顯示
                            last_translated = line
                            seq = _trans_seq[0]; _trans_seq[0] += 1
                            t = threading.Thread(
                                target=translate_and_print,
                                args=(seq, line, log_path, _asr_elapsed),
                                daemon=True,
                            )
                            t.start()

                    elif mode in ("zh2en", "zh2ja", "zh2ko"):
                        # 中文輸入翻譯模式：中文輸入過濾 + 翻譯
                        stripped_zh = re.sub(r"[^\u4e00-\u9fff]", "", line)
                        if len(stripped_zh) < 2:
                            continue
                        line = _s2twp_safe(line)
                        if line == last_translated:
                            continue
                        # 過濾中文幻覺
                        if any(kw in line for kw in (
                            "訂閱", "點贊", "點讚", "轉發", "打賞",
                            "感謝觀看", "謝謝大家", "謝謝收看",
                            "字幕由", "字幕提供", "字幕by", "字幕BY",
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
                        line = _s2twp_safe(line)
                        if line == last_translated:
                            continue
                        if any(kw in line for kw in (
                            "訂閱", "點贊", "點讚", "轉發", "打賞",
                            "感謝觀看", "謝謝大家", "謝謝收看",
                            "字幕由", "字幕提供", "字幕by", "字幕BY",
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
        _webui_send_realtime_results(log_path, [rec_path])

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
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿",
                    "ko2zh": "韓翻中_逐字稿", "zh2ko": "中翻韓_逐字稿", "ko": "韓文_逐字稿"}
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
                if not isinstance(result, _TranslateFailed):
                    continue                     # 被過濾掉的翻譯（幻覺、亂碼）：照舊整筆略過
                result = "（翻譯失敗）"           # 出錯：原文照樣顯示（以前連原文都不見）
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
            result = _s2twp_safe(result) if not isinstance(translator, OllamaTranslator) else _to_traditional(result)
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
            cols = shutil.get_terminal_size((80, 24)).columns
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
                    cols = shutil.get_terminal_size((80, 24)).columns
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
    sd_samplerate, sd_channels = _capture_stream_info(capture_id)

    # 建立錄音
    rec_stream = None
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        # 錄音裝置與 ASR 裝置可能不同（例如聚集裝置含麥克風+BlackHole）
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            if rec_device in _MIXED_REC_IDS:
                # 混合錄音（系統音訊 + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅系統音訊
                    rec_device = _sys_audio_loopback_id()
            if _is_sys_audio_device(rec_device):
                rec_sr, rec_ch = _capture_stream_info(rec_device, cap_channels=None)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _open_capture_stream(
                        rec_device, rec_callback, rec_sr, rec_ch,
                        blocksize=int(rec_sr * 0.1))
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

    sd_stream = _open_capture_stream(
        capture_id, audio_callback, sd_samplerate, sd_channels,
        blocksize=int(sd_samplerate * 0.1))  # 100ms

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
            _webui_send_realtime_results(log_path, [rec_path])

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

    whisper_lang = _mode_whisper_lang(mode)

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿",
                    "ko2zh": "韓翻中_逐字稿", "zh2ko": "中翻韓_逐字稿", "ko": "韓文_逐字稿"}
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
        warmup_lang = _mode_whisper_lang(mode)
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
    sd_samplerate, sd_channels = _capture_stream_info(capture_id)
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
            if rec_device in _MIXED_REC_IDS:
                # 混合錄音（系統音訊 + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅系統音訊
                    rec_device = _sys_audio_loopback_id()
            if _is_sys_audio_device(rec_device):
                rec_sr, rec_ch = _capture_stream_info(rec_device, cap_channels=None)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _open_capture_stream(
                        rec_device, rec_callback, rec_sr, rec_ch,
                        blocksize=int(rec_sr * 0.1))
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

    sd_stream = _open_capture_stream(
        capture_id, audio_callback, sd_samplerate, sd_channels,
        blocksize=int(sd_samplerate * 0.1))

    # ── 降噪 ──
    _denoise = _make_denoiser(denoise)

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
                if not isinstance(result, _TranslateFailed):
                    continue                     # 被過濾掉的翻譯（幻覺、亂碼）：照舊整筆略過
                result = "（翻譯失敗）"           # 出錯：原文照樣顯示（以前連原文都不見）
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
            result = _s2twp_safe(result) if not isinstance(translator, OllamaTranslator) else _to_traditional(result)
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
    elif mode in _KO_INPUT_MODES:
        # 不可以落到中文過濾：它要求至少兩個漢字，韓文會整句被丟掉
        hallucination_check = _is_ko_hallucination
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
                    line = _s2twp_safe(line)
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
            _webui_send_realtime_results(log_path, [rec_path])
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
        "ko2zh": "說韓文即可看到中文翻譯",
        "zh2ko": "說中文即可看到韓文翻譯",
        "ko": "說韓文即可看到字幕",
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
                             denoise: bool = False,
                             use_mlx: bool = False):
    """Python 端本機即時辨識：sounddevice / WASAPI Loopback / ScreenCaptureKit 擷取音訊
    → 本機 mlx-whisper（Apple Silicon GPU）或 faster-whisper 即時辨識。
    架構類似 run_stream_remote()，但用本機辨識取代遠端 HTTP 上傳。
    use_mlx: True 時使用 mlx-whisper GPU 加速（僅 Apple Silicon）"""
    import numpy as np
    if not use_mlx:
        from faster_whisper import WhisperModel
        _fw_av_compat()

    whisper_lang = _mode_whisper_lang(mode)

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    log_prefixes = {"en2zh": "英翻中_逐字稿", "zh2en": "中翻英_逐字稿",
                    "ja2zh": "日翻中_逐字稿", "zh2ja": "中翻日_逐字稿",
                    "en": "英文_逐字稿", "zh": "中文_逐字稿", "ja": "日文_逐字稿",
                    "ko2zh": "韓翻中_逐字稿", "zh2ko": "中翻韓_逐字稿", "ko": "韓文_逐字稿"}
    log_prefix = log_prefixes.get(mode, "逐字稿")
    topic_part = _topic_to_filename_part(meeting_topic)
    log_filename = datetime.now().strftime(f"{log_prefix}{topic_part}_%Y%m%d_%H%M%S.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, log_filename)

    # ── 載入 ASR 模型（mlx-whisper 或 faster-whisper）──
    fw_model = None        # faster-whisper model（use_mlx=False 時使用）
    _mlx_repo = None       # mlx-whisper HF repo（use_mlx=True 時使用）
    _mlx_whisper_mod = None
    _fw_model_sizes = {"large-v3-turbo": "1.6GB", "large-v3": "3.1GB",
                       "medium.en": "1.5GB", "medium": "1.5GB",
                       "small.en": "500MB", "small": "500MB",
                       "base.en": "150MB"}
    if use_mlx:
        # 在 import 前設定，避免 huggingface_hub 的 tqdm 進度條和 "Fetching N files" 訊息
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["TQDM_DISABLE"] = "1"
        try:
            from huggingface_hub.utils import disable_progress_bars as _hf_disable_pb
            _hf_disable_pb()
        except Exception:
            pass
        import mlx_whisper as _mlx_whisper_mod
        # MLX 社群 repo 命名不一致：large-v3-turbo 無字尾，其餘需加 -mlx
        _mlx_repo = _resolve_mlx_repo(model_name)
        _mlx_cache_name = "models--" + _mlx_repo.replace("/", "--")
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
        else:
            print(f"\n{C_DIM}正在載入 Whisper 模型 ({model_name}，mlx GPU)...{RESET}", end="", flush=True)
            _webui_send({"type": "progress", "stage": "載入中", "detail": f"Whisper 模型（{model_name}，mlx GPU）"})
        t0 = time.monotonic()
        # 暖機：第一次 transcribe 會編譯 Metal kernel，先用靜音音訊觸發
        try:
            import tempfile as _tf
            _warmup_fd, _warmup_path = _tf.mkstemp(suffix=".wav")
            os.close(_warmup_fd)
            with wave.open(_warmup_path, "wb") as _wf:
                _wf.setnchannels(1)
                _wf.setsampwidth(2)
                _wf.setframerate(16000)
                _wf.writeframes(b"\x00" * 32000)
            _call_with_ssl_retry(_mlx_whisper_mod.transcribe, _mlx_input(_warmup_path),
                                 path_or_hf_repo=_mlx_repo, language=whisper_lang)
            os.unlink(_warmup_path)
        except Exception:
            pass
        if _mlx_need_download:
            print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
        else:
            print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")
    else:
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
            fw_model = _call_with_ssl_retry(_FwModel, _resolve_fw_model(model_name), **_fw_device_kwargs())
        _hf_logger.setLevel(_hf_log_level)
        if _fw_need_download:
            print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
        else:
            print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")

    # ── 音訊裝置 ──
    import sounddevice as sd
    sd_samplerate, sd_channels = _capture_stream_info(capture_id)

    stop_event = threading.Event()

    # ── 錄音 ──
    recorder = None
    rec_stream = None
    _rec_stream_mic = None   # Windows 混合錄音的麥克風串流
    _mixer = None            # Windows 混合錄音的 mixer
    if record:
        use_separate_rec = (rec_device is not None and rec_device != capture_id)
        if use_separate_rec:
            if rec_device in _MIXED_REC_IDS:
                # 混合錄音（系統音訊 + 麥克風）
                _mixed = _setup_mixed_recording(stop_event, meeting_topic)
                if _mixed:
                    recorder, _mixer, rec_stream, _rec_stream_mic = _mixed
                else:
                    # 降級為僅系統音訊
                    rec_device = _sys_audio_loopback_id()
            if _is_sys_audio_device(rec_device):
                rec_sr, rec_ch = _capture_stream_info(rec_device, cap_channels=None)
                recorder = _AudioRecorder(rec_sr, rec_ch, topic=meeting_topic, mode=mode)

                def rec_callback(indata, frames, time_info, status):
                    if not stop_event.is_set():
                        recorder.write_raw(indata)

                try:
                    rec_stream = _open_capture_stream(
                        rec_device, rec_callback, rec_sr, rec_ch,
                        blocksize=int(rec_sr * 0.1))
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
    _asr_engine_label = "mlx-whisper GPU" if use_mlx else "faster-whisper"
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ 本機（{_asr_engine_label}）{RESET}")
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

    import sounddevice as sd
    sd_stream = _open_capture_stream(
        capture_id, audio_callback, sd_samplerate, sd_channels,
        blocksize=int(sd_samplerate * 0.1))

    # ── 降噪 ──
    _denoise = _make_denoiser(denoise)

    # ── 提取音訊並寫入暫存 WAV（原始取樣率，讓 faster-whisper 正確 resample）──
    import tempfile as _tempfile
    _tmp_wav_dir = _tempfile.gettempdir()
    _fw_wav_seq = [0]                   # 每段遞增（同雙向模式的 _wav_counter）

    def extract_wav_file():
        """提取環形緩衝，寫入暫存 WAV 檔，回傳檔案路徑和 RMS。
        每一段用自己的檔名：最多同時 2 段在辨識（_MAX_CONCURRENT_TRANSCRIPTIONS），以前都寫同一個 jt_fw_<pid>.wav——
        Windows 第二段寫不進去（Permission denied，那一段辨識失敗）、Linux／macOS 直接蓋掉前一段正在讀的檔
        （辨識到錯的音訊、前一段刪檔時連這一段也刪掉），2026-10-10 守門抓到"""
        with ring_lock:
            pos = ring_write_pos
            buf_copy = ring_buffer.copy()
        ordered = np.roll(buf_copy, -pos)
        rms = float(np.sqrt(np.mean(ordered ** 2)))
        ordered = _denoise(ordered, sd_samplerate)
        pcm = (ordered * 32767).clip(-32768, 32767).astype(np.int16)
        _fw_wav_seq[0] += 1
        tmp_path = os.path.join(_tmp_wav_dir, f"jt_fw_{os.getpid()}_{_fw_wav_seq[0]}.wav")
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sd_samplerate)  # 原始取樣率（如 48000）
            wf.writeframes(pcm.tobytes())
        return tmp_path, rms

    # ── 本機 faster-whisper 辨識 ──
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

    def local_transcribe(wav_path):
        """用 mlx-whisper / faster-whisper 辨識 WAV 檔，回傳 (segments_list, full_text, proc_time)"""
        t0 = time.monotonic()
        segments = []
        texts = []
        if use_mlx:
            _kw = dict(
                path_or_hf_repo=_mlx_repo,
                language=whisper_lang,
                word_timestamps=False,
                condition_on_previous_text=False,
                sample_len=50,
            )
            _prompt = _WHISPER_PROMPT.get(whisper_lang)
            if _prompt:
                _kw["initial_prompt"] = _prompt
            result = _mlx_whisper_mod.transcribe(_mlx_input(wav_path), **_kw)
            for seg in result.get("segments", []):
                text = seg.get("text", "").strip()
                # 安全過濾：移除 prompt 洩漏文字
                for _leak in _PROMPT_LEAK_TEXTS:
                    text = text.replace(_leak, "")
                text = text.strip("，。、 ")
                if text:
                    segments.append({"start": seg["start"], "end": seg["end"], "text": text})
                    texts.append(text)
        else:
            segments_iter, info = fw_model.transcribe(
                wav_path, language=whisper_lang, beam_size=5, vad_filter=True)
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
                if not isinstance(result, _TranslateFailed):
                    continue                     # 被過濾掉的翻譯（幻覺、亂碼）：照舊整筆略過
                result = "（翻譯失敗）"           # 出錯：原文照樣顯示（以前連原文都不見）
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
            result = _s2twp_safe(result) if not isinstance(translator, OllamaTranslator) else _to_traditional(result)
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
    # mlx-whisper 的 Metal 推論不可並行，需序列化（與雙向模式同樣策略）
    _serial_lock = threading.Lock() if use_mlx else None

    _slow_warned = [False]

    def transcribe_chunk(seq, wav_path):
        with _active_lock:
            _active_transcriptions[0] += 1
        try:
            if _serial_lock is not None:
                with _serial_lock:
                    segments, full_text, proc_time = local_transcribe(wav_path)
            else:
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
    elif mode in _KO_INPUT_MODES:
        # 不可以落到中文過濾：它要求至少兩個漢字，韓文會整句被丟掉
        hallucination_check = _is_ko_hallucination
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
                    line = _s2twp_safe(line)
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
            _webui_send_realtime_results(log_path, [rec_path])

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
        print(f"  {C_DIM}{_no_audio_hint(capture_id)}{RESET}", flush=True)

    listen_hints = {
        "en2zh": "說英文即可看到翻譯",
        "zh2en": "說中文即可看到英文翻譯",
        "ja2zh": "說日文即可看到中文翻譯",
        "zh2ja": "說中文即可看到日文翻譯",
        "en": "說英文即可看到字幕",
        "zh": "說中文即可看到字幕",
        "ja": "說日文即可看到字幕",
        "ko2zh": "說韓文即可看到中文翻譯",
        "zh2ko": "說中文即可看到韓文翻譯",
        "ko": "說韓文即可看到字幕",
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
                              denoise: bool = False,
                              mic_remote_cfg: dict = None,
                              interp=None):
    """雙向即時翻譯：兩路音訊串流 → 共用 faster-whisper/mlx-whisper → 各自翻譯 → 交錯輸出。
    lb_device_id: 系統音訊（BlackHole / WASAPI Loopback）
    mic_device_id: 麥克風
    translator_lb: 系統音訊翻譯器（翻譯方向依模式決定，純轉錄模式為 None）
    translator_mic: 麥克風翻譯器（mic_translate=True 時使用，False 時為 None）
    use_mlx: True 時使用 mlx-whisper GPU 加速（僅 Apple Silicon）
    mic_translate: True=麥克風也翻譯（雙向模式），False=麥克風只轉錄（--mic 模式）
    interp: 雙向語音口譯（_interp_build 建好、還沒啟動的 Interpreter；v2.28.0）"""
    import numpy as np

    bidi_cfg = _BIDI_LABELS[mode]  # {"loopback": (...), "mic": (...)}

    # ── 語言對照（使用模組級 _LB_LANG / _MIC_LANG）──
    _LB_HALLU = {"en2zh": _is_en_hallucination, "zh2en": _is_zh_hallucination,
                 "ja2zh": _is_ja_hallucination, "zh2ja": _is_zh_hallucination,
                 "en": _is_en_hallucination, "zh": _is_zh_hallucination,
                 "ja": _is_ja_hallucination, "en_zh": _is_en_hallucination,
                 "ja_zh": _is_ja_hallucination,
                 "ko2zh": _is_ko_hallucination, "zh2ko": _is_zh_hallucination,
                 "ko": _is_ko_hallucination, "ko_zh": _is_ko_hallucination}
    _MIC_HALLU = {"en2zh": _is_zh_hallucination, "zh2en": _is_en_hallucination,
                  "ja2zh": _is_zh_hallucination, "zh2ja": _is_ja_hallucination,
                  "en": _is_en_hallucination, "zh": _is_zh_hallucination,
                  "ja": _is_ja_hallucination, "en_zh": _is_zh_hallucination,
                  "ja_zh": _is_zh_hallucination,
                  "ko2zh": _is_zh_hallucination, "zh2ko": _is_ko_hallucination,
                  "ko": _is_ko_hallucination, "ko_zh": _is_zh_hallucination}
    lb_lang = _LB_LANG[mode]
    mic_lang = _MIC_LANG[mode]
    lb_hallu = _LB_HALLU[mode]
    mic_hallu = _MIC_HALLU[mode]
    # 雙向模式麥克風語言預偵測（detect_language → 正確語言辨識）
    # en_zh: 中文翻譯、英文直接顯示；ja_zh／ko_zh: 中文翻譯、日文或韓文／英文直接顯示
    _mic_auto_detect = (mode in ("en_zh", "ja_zh", "ko_zh"))
    _mic_skip_langs = {"en_zh": {"en"}, "ja_zh": {"ja", "en"},
                       "ko_zh": {"ko", "en"}}.get(mode)  # 偵測到這些語言時跳過翻譯
    if _mic_auto_detect:
        mic_lang = None  # 觸發 local_transcribe 內的語言預偵測

    # ── 翻譯記錄檔 ──
    from datetime import datetime
    _LOG_PREFIX = {"en_zh": "英中雙向_逐字稿", "ja_zh": "日中雙向_逐字稿",
                   "ko_zh": "韓中雙向_逐字稿",
                   "en2zh": "英翻中_逐字稿",
                   "zh2en": "中翻英_逐字稿", "ja2zh": "日翻中_逐字稿",
                   "zh2ja": "中翻日_逐字稿", "ko2zh": "韓翻中_逐字稿", "zh2ko": "中翻韓_逐字稿",
                   "en": "英文轉錄", "zh": "中文轉錄", "ja": "日文轉錄", "ko": "韓文轉錄"}
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
        _mlx_repo = _resolve_mlx_repo(model_name)
        _mlx_cache_name = "models--" + _mlx_repo.replace("/", "--")
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
            _call_with_ssl_retry(_mlx_whisper_mod.transcribe, _mlx_input(_warmup_path), path_or_hf_repo=_mlx_repo, language=lb_lang)
            if mic_lang is None:
                # 自動偵測模式：預熱 transcribe（偵測可能用到的語言）
                _warmup_langs = {"zh", "en"}
                if mode in ("ja_zh", "ko_zh"):
                    _warmup_langs.add(_BIDI_FOREIGN[mode])
                for _wl in _warmup_langs - {lb_lang}:
                    _mlx_whisper_mod.transcribe(_mlx_input(_warmup_path), path_or_hf_repo=_mlx_repo, language=_wl)
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
                # 也預熱英文（+ ja_zh／ko_zh 模式的日文／韓文）decode
                for _wl2 in ({"en", _BIDI_FOREIGN[mode]} if mode in ("ja_zh", "ko_zh") else {"en"}):
                    _w_opts2 = _WarmupDO(language=_wl2, task="transcribe", temperature=0.0,
                        sample_len=25, fp16=True)
                    try:
                        _w_model.decode(_w_mel_seg, _w_opts2)
                    except Exception:
                        pass
                del _w_model, _w_mel, _w_mel_seg, _w_tok, _w_opts
            elif mic_lang != lb_lang:
                _mlx_whisper_mod.transcribe(_mlx_input(_warmup_path), path_or_hf_repo=_mlx_repo, language=mic_lang)
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
        _fw_av_compat()
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
            fw_model = _call_with_ssl_retry(_FwModel, _resolve_fw_model(model_name), **_fw_device_kwargs())
        _hf_logger.setLevel(_hf_log_level)
        if _fw_need_download:
            print(f"  {C_OK}模型下載完成（{time.monotonic() - t0:.1f}s）{RESET}")
        else:
            print(f" {C_OK}完成（{time.monotonic() - t0:.1f}s）{RESET}")

    # ── 音訊裝置資訊 ──
    import sounddevice as sd
    lb_sr, lb_ch = _capture_stream_info(lb_device_id)
    if IS_WINDOWS and lb_device_id == WASAPI_LOOPBACK_ID:
        lb_name = _find_wasapi_loopback()["name"]
    elif IS_MACOS and lb_device_id == SCK_LOOPBACK_ID:
        lb_name = "ScreenCaptureKit 系統音訊"
    elif IS_LINUX and lb_device_id == PULSE_LOOPBACK_ID:
        lb_name = _pulse_label()
    else:
        lb_name = sd.query_devices(lb_device_id)["name"]

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
    # 有 GPU 伺服器時兩路都送遠端（見 transcribe_chunk 的 use_remote），橫幅要照實寫，
    # 不可一律顯示「本機」——否則畫面說的和實際做的不一樣
    if mic_remote_cfg:
        _asr_where = (f"GPU 伺服器 {mic_remote_cfg.get('host', '?')}:"
                      f"{mic_remote_cfg.get('whisper_port', REMOTE_WHISPER_DEFAULT_PORT)}"
                      f"（失敗時自動改用本機）")
    else:
        _asr_where = f"本機（{'mlx-whisper GPU' if use_mlx else 'faster-whisper'}）"
    print(f"  {C_OK}ASR 引擎: Whisper ({model_name}) @ {_asr_where}{RESET}")
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
    _lang_name = {"en": "英文", "zh": "中文", "ja": "日文", "ko": "韓文"}
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
        _mix_hint = {"ja_zh": "中日英混雜", "ko_zh": "中韓英混雜"}.get(mode, "中英混雜")
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
    if interp is not None:
        from jtlw_tts import interp as _I
        interp.audio.pause_ev = pause_event          # 暫停時語音也停
        interp.on_event = lambda e: _interp_event(e, print_lock)
        interp.start()
        threading.Thread(target=_interp_watch_cmds, args=(interp, stop_event), daemon=True).start()
        threading.Thread(target=interp.warm, daemon=True).start()      # 先叫醒 GPU 的合成程式
        if getattr(interp, "passthrough", None) is not None:
            interp.passthrough.start()
        _names = {"me": "念給我聽（中文）", "them": "念給對方聽（英文）"}
        _how = "GPU 伺服器串流合成" if interp.streaming else getattr(interp.provider, "label", "")
        print(f"{C_DIM}語音口譯：{'、'.join(_names[k] for k in interp.lanes)}｜{_how}｜"
              f"念過的句子 20 秒內被錄回來會自動略過；請戴耳機{RESET}")
        if _I.THEM in interp.lanes:
            print(f"{C_DIM}  會議軟體的麥克風請改選虛擬麥克風；結束後記得改回來"
                  f"{'（同時送出你的原聲，念英文時調小）' if getattr(interp, 'passthrough', None) else ''}{RESET}")
    _interp_intro_done = [interp is None or not getattr(interp, "intro", None)]
    _pt = getattr(interp, "passthrough", None) if interp is not None else None
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
        if _pt is not None:
            _pt.push(audio, mic_sr)
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
    lb_stream = _open_capture_stream(
        lb_device_id, lb_audio_callback, lb_sr, lb_ch,
        blocksize=int(lb_sr * 0.1))

    # 注意：mic_stream 故意延後到 lb_stream.start() 之後才建立。
    # macOS CoreAudio 若同時預先建立兩個 InputStream（BlackHole + 內建麥克風），
    # 第二個 stream.start() 會偶發 PaErrorCode -9986 (paInternalError)。
    # 改為「先 start lb，再建立並 start mic」可穩定避開。
    mic_stream = None

    # ── 降噪 ──
    _denoise = _make_denoiser(denoise)

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
                elif mode in ("ja_zh", "ko_zh"):
                    # ja_zh／ko_zh 模式：非中文時區分日文（或韓文）和英文
                    _fl = _BIDI_FOREIGN[mode]
                    lang = _fl if _probs.get(_fl, 0) > _probs.get("en", 0) else "en"
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
                    result = _mlx_whisper_mod.transcribe(_mlx_input(wav_path),
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
            result = _mlx_whisper_mod.transcribe(_mlx_input(wav_path), **_kw)
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
                elif mode in ("ja_zh", "ko_zh"):
                    _fl = _BIDI_FOREIGN[mode]
                    lang = _fl if _prob_dict.get(_fl, 0) > _prob_dict.get("en", 0) else "en"
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
                _iid = None
                if interp is not None and source == "mic" and label_override == "EN" and _pt is None:
                    _iid = _interp_say("them", src_text, src_text)   # 我直接講英文：照原文念給對方（同時送出原聲時對方已經聽到了，不再念一次）
                _webui_send({"type": "transcription", "source": source,
                             "src_lang": src_label, "src_text": src_text,
                             "asr_time": round(asr_elapsed, 1), "timestamp": timestamp,
                             **({"interp_id": _iid} if _iid else {})})
                continue
            if not result:
                if not isinstance(result, _TranslateFailed):
                    continue                     # 被過濾掉的翻譯：照舊略過
                result = "（翻譯失敗）"           # 出錯：原文照樣顯示
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
            _iid = None
            if interp is not None and not isinstance(result, _TranslateFailed) and result != "（翻譯失敗）":
                _iid = _interp_say("me" if source == "loopback" else "them", result, src_text)
            _webui_send({"type": "transcription", "source": source,
                         "src_lang": src_label, "src_text": src_text,
                         "dst_lang": dst_label, "dst_text": result,
                         "asr_time": round(asr_elapsed, 1),
                         "translate_time": round(elapsed, 1),
                         "timestamp": timestamp,
                         **({"interp_id": _iid} if _iid else {})})

    def _interp_say(lane, text, src):
        """排進口譯；給對方的第一句之前先念開場說明"""
        if lane == "them" and not _interp_intro_done[0]:
            _interp_intro_done[0] = True
            interp.speak("them", interp.intro)
        return interp.speak(lane, text, src)

    def translate_and_print(seq, src_text, translator, pending, next_seq, lock, source, asr_elapsed=0):
        with _active_trans_lock:
            _active_translations[0] += 1
        try:
            t0 = time.monotonic()
            result = translator.translate(src_text)
            elapsed = time.monotonic() - t0
            if result and not isinstance(translator, OllamaTranslator):
                result = _s2twp_safe(result)
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

    def transcribe_chunk(seq, wav_path, lang, pending_res, res_lock, use_remote=False, reject_lang=None):
        with _active_lock:
            _active_transcriptions[0] += 1
        try:
            if not wav_path or not os.path.isfile(wav_path):
                with res_lock:
                    pending_res[seq] = _TRANSCRIBE_FAILED
                return

            # 遠端 GPU 伺服器辨識（麥克風）
            if use_remote and mic_remote_cfg:
                try:
                    _rl = lang or ("zh" if mode in ("zh", "zh2en", "zh2ja", "zh2ko", "en_zh", "ja_zh", "ko_zh") else "en")
                    with open(wav_path, "rb") as _rf:
                        _wav_bytes = _rf.read()
                    _segs, _full, _pt = _remote_whisper_transcribe_bytes(
                        mic_remote_cfg, _wav_bytes, model_name, _rl, timeout=30, reject_lang=reject_lang)
                    with res_lock:
                        pending_res[seq] = (_segs, _full, _pt, _rl)
                    return
                except _AudioRejected as _rj:
                    # 雙向語音口譯：系統音訊錄到的是念給我聽的中文（GPU 伺服器判斷語言），不辨識、不翻
                    _webui_send({"type": "interp", "state": "echo", "lane": "me", "text": f"（系統音訊是{_rj}：自己念的）"})
                    with res_lock:
                        pending_res[seq] = _TRANSCRIBE_FAILED
                    return
                except Exception as _re:
                    # 遠端失敗 → 降級本機辨識
                    with print_lock:
                        print(f"{C_DIM}  [麥克風遠端辨識失敗，降級本機: {_re}]{RESET}", flush=True)

            # 本機辨識（mlx / faster-whisper）
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
                      "ja": _is_ja_hallucination, "ko": _is_ko_hallucination}

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
                    line = _s2twp_safe(line)
                if _hallu_fn(line):
                    continue
                if is_duplicate(line, recent):
                    continue
                if interp is not None and interp.guard.is_echo(line, during="me" if source == "loopback" else None):
                    # 自己念出來的聲音被錄回來（耳機漏音、系統音訊錄到、對方沒有回音消除）：不翻、不念
                    _webui_send({"type": "interp", "state": "echo", "lane": "me" if source == "loopback" else "them",
                                 "text": line})
                    continue
                recent.append(line.lower().strip())
                seq = trans_seq[0]; trans_seq[0] += 1
                if translator is None or _skip_this:
                    # 不翻譯：直接塞入翻譯佇列，用 _NO_TRANSLATE 哨兵
                    with trans_lock:
                        trans_pending[seq] = (line, _NO_TRANSLATE, 0, proc_time)
                    _lbl = {"en": "EN", "ja": "日", "ko": "韓", "zh": "中"}.get(_effective_lang, _effective_lang.upper()) if _skip_this else None
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
        # 口譯：先移除虛擬裝置（很快；pacat 跟著結束），執行緒與播放最後才收（最慢）：
        # WebUI 停止 4 秒沒有心跳就送 SIGTERM，第二次進信號處理會直接 _force_exit，錄音要先存好
        if interp is not None:
            interp.stop(wait=0)                 # 先通知停（不等），之後裝置被移除的錯誤就不會當成失敗印出來
            if _interp_linux_cleanup():
                pass                            # WebUI 切換裝置：虛擬麥克風留給重新啟動的程式，會議軟體不用改
            elif "them" in interp.lanes:
                print(f"\n  {C_WARN}語音口譯已停止：會議軟體的麥克風請改回原本的麥克風，否則對方聽不到你{RESET}", flush=True)
        if recorder_lb:
            p1 = recorder_lb.close()
            print(f"\n  {C_OK}錄音已儲存: {p1}{RESET}", flush=True)
        if recorder_mic:
            p2 = recorder_mic.close()
            print(f"  {C_OK}錄音已儲存: {p2}{RESET}", flush=True)
        if recorder_lb or recorder_mic:
            print(f"  {C_DIM}提示: 可再次執行本程式，選擇「讀入檔案」匯入錄音檔，產生逐字稿校正與 AI 摘要{RESET}", flush=True)
            _webui_send_realtime_results(log_path, [p1 if recorder_lb else None, p2 if recorder_mic else None])
        if interp is not None:
            interp.stop(wait=0.5)
            interp.audio.close()
            if _pt is not None:
                _pt.stop()
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
    # 先啟動 lb_stream，再「建立並啟動」mic_stream（macOS 必要的順序，見上方說明）
    lb_stream.start()

    def _open_mic_stream(**extra):
        return sd.InputStream(
            device=mic_device_id, samplerate=mic_sr, channels=mic_ch,
            dtype="float32", callback=mic_audio_callback, **extra)

    _mic_attempts = [
        ("blocksize=int(mic_sr*0.1)", dict(blocksize=int(mic_sr * 0.1))),
        ("blocksize=0, latency=high", dict(blocksize=0, latency='high')),
        ("blocksize=int(mic_sr*0.1), latency=high", dict(blocksize=int(mic_sr * 0.1), latency='high')),
    ]
    _mic_started = False
    _last_err = None
    for _label, _kw in _mic_attempts:
        try:
            mic_stream = _open_mic_stream(**_kw)
            mic_stream.start()
            _mic_started = True
            break
        except Exception as _e:
            _last_err = _e
            try:
                if mic_stream is not None:
                    mic_stream.close()
            except Exception:
                pass
            mic_stream = None
            print(f"  {C_DIM}麥克風串流嘗試（{_label}）失敗：{_e}{RESET}", flush=True)
    if not _mic_started:
        try:
            lb_stream.stop(); lb_stream.close()
        except Exception:
            pass
        print(f"  {C_HIGHLIGHT}[錯誤] 麥克風串流所有重試方案均失敗：{_last_err}{RESET}", flush=True)
        print(f"  {C_DIM}建議：1) 確認麥克風未被其他程式佔用 2) 系統設定→隱私權與安全性→麥克風 確認 Terminal/Python 已授權 3) 重啟 CoreAudio：sudo killall coreaudiod{RESET}", flush=True)
        raise _last_err

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
                            _lb_use_remote = bool(mic_remote_cfg)
                            threading.Thread(
                                target=transcribe_chunk,
                                args=(seq, wav_path, lb_lang, lb_pending_results, lb_results_lock, _lb_use_remote,
                                      _interp_reject_lang(interp, _pt, length_ms)),
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
                            _mic_use_remote = bool(mic_remote_cfg)
                            threading.Thread(
                                target=transcribe_chunk,
                                args=(seq, wav_path, mic_lang, mic_pending_results, mic_results_lock, _mic_use_remote),
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
                   "ko2zh": "韓翻中", "zh2ko": "中翻韓",
                   "en_zh": "英中雙向", "ja_zh": "日中雙向", "ko_zh": "韓中雙向",
                   "en": "英文", "zh": "中文", "ja": "日文", "ko": "韓文"}

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
        # 錄音檔大小回報 WebUI、磁碟快滿時自動停止（v2.26.3）
        self._rel = os.path.relpath(self.path, os.path.dirname(os.path.abspath(__file__)))
        self._last_report = 0.0
        self._last_disk_check = 0.0
        self._free = None
        self.disk_stopped = False
        self._check_disk(force=True)

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

    # 磁碟快滿時自動停止（v2.26.3）：寫到滿才停的話，WAV 的 header 來不及更新、結束時也沒空間轉 MP3，
    # 整段可能都救不回來。要留的空間＝磁碟總容量的 2%（至少 1 GB、最多 10 GB）＋目前錄音的 30%
    # （結束時轉 MP3 要寫新檔，WAV 刪掉之前兩份並存）；剩下不到兩倍時先提醒
    _DISK_CHECK_INTERVAL = 5.0
    _DISK_BASE_MIN, _DISK_BASE_MAX, _DISK_BASE_RATIO = 1 << 30, 10 << 30, 0.02
    _DISK_CONVERT_RATIO = 0.3

    def disk_reserve(self, total=None):
        if total is None:
            total = shutil.disk_usage(os.path.dirname(self.path)).total
        base = min(max(self._DISK_BASE_MIN, int(total * self._DISK_BASE_RATIO)), self._DISK_BASE_MAX)
        return base + int(self._data_size * self._DISK_CONVERT_RATIO)

    def _check_disk(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_disk_check < self._DISK_CHECK_INTERVAL:
            return
        self._last_disk_check = now
        try:
            du = shutil.disk_usage(os.path.dirname(self.path))
        except OSError:
            return
        self._free = du.free
        if du.free < self.disk_reserve(du.total):
            self._stop_for_disk(du.free)

    def _stop_for_disk(self, free):
        if self.disk_stopped:
            return
        self.disk_stopped = True
        try:
            self._write_header()
            self._f.flush()
        except OSError:
            pass
        gb = free / (1 << 30)
        if self._data_size == 0:
            need = self.disk_reserve() / (1 << 30)
            print(f"\n{C_ERR}[錄音] 磁碟只剩 {gb:.1f} GB，不開始錄音（至少要留 {need:.1f} GB，"
                  f"錄到快滿時檔案可能救不回來）；請先清出空間{RESET}", flush=True)
        else:
            print(f"\n{C_ERR}[錄音] 磁碟只剩 {gb:.1f} GB，已自動停止錄音（保留結束時轉檔的空間）；"
                  f"已錄的部分保存在 {self.path}{RESET}", flush=True)
        _webui_send({"type": "rec_stopped", "reason": "disk_low", "free": free, "path": self._rel,
                     "bytes": 44 + self._data_size})

    def _after_write(self):
        self._maybe_update_header()
        now = time.monotonic()
        if now - self._last_report >= 1.0:
            self._last_report = now
            self._check_disk()
            if not self.disk_stopped:
                low = self._free is not None and self._free < 2 * self.disk_reserve()
                _webui_send({"type": "rec_size", "path": self._rel, "bytes": 44 + self._data_size,
                             "free": self._free, "low": low})

    def write(self, float32_mono):
        """寫入 float32 單聲道音訊（自動轉換為 int16）"""
        if self.disk_stopped:
            return
        import numpy as np
        pcm = (float32_mono * 32767).clip(-32768, 32767).astype(np.int16)
        raw = pcm.tobytes()
        self._f.write(raw)
        self._data_size += len(raw)
        self._after_write()

    def write_raw(self, float32_data):
        """寫入 float32 音訊（多聲道或單聲道皆可，自動轉 int16）"""
        if self.disk_stopped:
            return
        import numpy as np
        data = float32_data.astype(np.float32)
        pcm = (data * 32767).clip(-32768, 32767).astype(np.int16)
        raw = pcm.tobytes()
        self._f.write(raw)
        self._data_size += len(raw)
        self._after_write()

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
        # 聲道超過格式上限（MP3／OGG 2 聲道、FLAC 8 聲道）就降成立體聲：多聲道的 USB 錄音介面、
        # 聚集裝置、PipeWire 的 default（回報 64 聲道）以前會轉檔失敗，留下 0 位元組的檔案（v2.26.3）
        if self._channels > {"mp3": 2, "ogg": 2, "flac": 8}.get(fmt, 2):
            args = ["-ac", "2"] + args

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
            # 長錄音要的時間跟長度成正比（1 小時的會議在慢的 CPU 上可能超過 5 分鐘）；逾時只是不轉、WAV 照樣保留
            timeout_s = max(300, duration_s / 2)
            last_beat = 0.0
            while not ffmpeg_done.is_set():
                pct = progress_pct[0]
                # 每秒告訴 WebUI「還在存檔」：它看到這個就不會在按下停止 4 秒後強制結束我們（v2.26.4）。
                # 以前 1 小時的錄音轉到一半就被 SIGTERM，畫面寫「程式異常結束（錯誤碼 -15）」
                if time.monotonic() - last_beat >= 1.0:
                    last_beat = time.monotonic()
                    _webui_send({"type": "progress", "stage": "存檔中", "finishing": True,
                                 "detail": f"錄音轉檔 WAV → {fmt_upper} {pct}%"})
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
                self._drop_partial(out_path)
                print(f"{C_WARN}[警告] 錄音轉 {fmt} 失敗（保留 WAV）{RESET}")
                _webui_send({"type": "progress", "stage": "存檔", "detail": f"轉檔失敗，保留 WAV"})
        except Exception:
            # 清除可能殘留的 spinner
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            self._drop_partial(out_path)
            print(f"{C_WARN}[警告] 錄音轉 {fmt} 失敗（保留 WAV）{RESET}")

    @staticmethod
    def _drop_partial(out_path):
        """轉檔失敗時刪掉沒轉完的目標檔（WAV 還在；留著空檔會讓人以為錄音壞了）"""
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass

    def close(self):
        try:
            self._write_header()
            self._f.close()
        except Exception as e:
            # 檔頭沒寫好就轉檔，轉完會刪掉 WAV（唯一完整的那份），MP3 可能少掉最後一段卻顯示轉檔完成（2026-10-05）
            try:
                self._f.close()
            except Exception:
                pass
            print(f"\n  {C_WARN}[錄音] 收尾寫入失敗（{type(e).__name__}: {e}），保留 WAV、不轉檔：{self.path}{RESET}", flush=True)
            return self.path
        if self._data_size == 0:          # 一開始磁碟空間就不夠、一個樣本都沒錄：不必轉檔
            return self.path
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

    def reset(self):
        """暫停後繼續時丟掉兩邊還沒配對的部分，從同一個時間點重新對齊（暫停的那一刻兩路不會剛好同時停）"""
        with self._lock:
            self._lb_buf = self._np.zeros(0, dtype=self._np.float32)
            self._mic_buf = self._np.zeros(0, dtype=self._np.float32)

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
    """建立混合錄音（系統音訊 + 麥克風）。
    系統音訊來源：Windows 用 WASAPI Loopback，macOS 用 ScreenCaptureKit。
    回傳 (recorder, mixer, lb_stream, mic_stream) 或 None（失敗時）。"""
    import sounddevice as sd
    import numpy as np

    if IS_MACOS:
        lb_device_id = SCK_LOOPBACK_ID
        mic_id = _find_mac_mic()
        if not _sck_available() or mic_id is None:
            return None
    elif IS_LINUX:
        lb_device_id = PULSE_LOOPBACK_ID
        mic_id = _find_default_mic()
        if not _pulse_available() or mic_id is None:
            return None
    else:
        wb_info = _find_wasapi_loopback()
        lb_device_id = WASAPI_LOOPBACK_ID
        mic_id = _find_default_mic()
        if not wb_info or mic_id is None:
            return None

    # 錄音路徑保留原始聲道數（Windows 多聲道輸出時，人聲多半在中央聲道，
    # 截到 2ch 會漏掉；callback 內本來就會自行 downmix 成單聲道）
    lb_sr, lb_ch = _capture_stream_info(lb_device_id, cap_channels=None)
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
        lb_stream = _open_capture_stream(
            lb_device_id, lb_callback, lb_sr, lb_ch,
            blocksize=int(lb_sr * 0.1))
    except Exception as e:
        _lb_label = ("ScreenCaptureKit" if IS_MACOS
                     else "系統音訊 monitor" if IS_LINUX else "WASAPI Loopback")
        print(f"{C_HIGHLIGHT}[警告] 無法開啟 {_lb_label} 錄音: {e}{RESET}")
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

    # macOS: 優先用 ScreenCaptureKit（不需聚集裝置，有麥克風時用混合模式）
    if IS_MACOS and _sck_available():
        mic_id = _find_mac_mic()
        if mic_id is not None:
            import sounddevice as sd
            mic_name = sd.query_devices(mic_id)["name"]
            return SCK_MIXED_ID, f"ScreenCaptureKit 系統音訊 + {mic_name}", "雙方聲音"
        return SCK_LOOPBACK_ID, "ScreenCaptureKit 系統音訊", "僅對方聲音"

    # Linux: PipeWire / PulseAudio monitor（有麥克風時用混合模式）
    if IS_LINUX and _pulse_available():
        lb_name = _pulse_label()
        mic_id = _find_default_mic()
        if mic_id is not None:
            import sounddevice as sd
            mic_name = sd.query_devices(mic_id)["name"]
            return PULSE_MIXED_ID, f"{lb_name} + {mic_name}", "雙方聲音"
        return PULSE_LOOPBACK_ID, lb_name, "僅對方聲音"

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


# ── 純錄音：照指定的來源與裝置錄（v2.26.3）──────────────────────
# 以前命令列的 --mode record 一律自動偵測，-d／--mic-device 都不看，WebUI 的兩個裝置下拉在純錄音時等於擺設；
# 互動選單印出的「等效指令」（-d -400 之類）拿去執行也重現不了當初的選擇。
REC_SOURCES = ("both", "system", "mic")      # 雙方（混成一軌）／只錄系統音訊／只錄麥克風


def _mixed_rec_id():
    """目前平台的混合錄音 sentinel（系統音訊＋麥克風）"""
    if IS_MACOS:
        return SCK_MIXED_ID
    if IS_LINUX:
        return PULSE_MIXED_ID
    return WASAPI_MIXED_ID


def _auto_rec_mic():
    """純錄音自動選的麥克風（排除 BlackHole、loopback、聚集裝置）"""
    return _find_mac_mic() if IS_MACOS else _find_default_mic()


def _auto_rec_loopback():
    """純錄音自動選的系統音訊：平台內建的擷取（ScreenCaptureKit／WASAPI／monitor）能用就用，否則找 BlackHole 之類的裝置"""
    if ((IS_WINDOWS and _find_wasapi_loopback()) or (IS_MACOS and _sck_available())
            or (IS_LINUX and _pulse_available())):
        return _sys_audio_loopback_id()
    import sounddevice as sd
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and _is_loopback_device(dev["name"]):
            return i
    return None


def _rec_device_name(dev_id):
    if dev_id == _sys_audio_loopback_id():
        if IS_MACOS:
            return "ScreenCaptureKit 系統音訊"
        if IS_LINUX:
            return _pulse_label()
        wb = _find_wasapi_loopback()
        return f"WASAPI Loopback ({wb['name']})" if wb else "WASAPI Loopback"
    import sounddevice as sd
    return sd.query_devices(dev_id)["name"]


def _check_rec_device(dev_id, what):
    """指定的裝置代號要是這台的：負數只收本平台的系統音訊／混合錄音代號，其餘要是有輸入聲道的裝置"""
    if dev_id < 0:
        if dev_id in (_sys_audio_loopback_id(), _mixed_rec_id()):
            return
        raise SystemExit(f"[錯誤] {what} {dev_id} 不是這個平台的系統音訊代號"
                         f"（這台是 {_sys_audio_loopback_id()}，混合錄音 {_mixed_rec_id()}）")
    import sounddevice as sd
    try:
        dev = sd.query_devices(dev_id)
    except Exception:
        raise SystemExit(f"[錯誤] 找不到{what} {dev_id}，請用 --list-devices 查看可用的裝置")
    if dev["max_input_channels"] <= 0:
        raise SystemExit(f"[錯誤] {what} [{dev_id}] {dev['name']} 沒有輸入聲道，不能錄音")


def _resolve_record_device(rec_source=None, device=None, mic_device=None):
    """純錄音要錄什麼。回傳 (rec_id, 名稱, 說明, 系統音訊裝置, 麥克風裝置)；後兩個只在混合錄音時有值，
    其餘情況 rec_id 就是要錄的那個裝置。找不到可錄的裝置時以 SystemExit 結束並說明。
      rec_source  None＝沒指定：有 -d 就錄那個（-d 是混合錄音代號時＝both），沒有就自動偵測（預設雙方）
                  both／system／mic＝WebUI 的「錄音來源」與命令列 --rec-source
      device      系統音訊裝置（-d）；mic_device＝麥克風（--mic-device）"""
    if device is not None:
        _check_rec_device(device, "裝置")
    if mic_device is not None:
        if mic_device < 0:
            raise SystemExit(f"[錯誤] 麥克風裝置 {mic_device} 不能是系統音訊代號")
        _check_rec_device(mic_device, "麥克風裝置")
    if rec_source is None:
        if device is None:
            rec_id, name, label = _auto_detect_rec_device()
            if rec_id is None:
                raise SystemExit("[錯誤] 找不到任何音訊輸入裝置！")
            if rec_id not in _MIXED_REC_IDS:
                return rec_id, name, label, None, None
            rec_source = "both"                    # 自動偵測到「雙方」：麥克風可以另外指定
        elif device in _MIXED_REC_IDS:
            rec_source, device = "both", None
        else:
            label = "僅對方聲音" if (_is_sys_audio_device(device) or _is_loopback_device(_rec_device_name(device))) else "指定裝置"
            return device, _rec_device_name(device), label, None, None
    if rec_source == "system":
        lb = device if device is not None and device not in _MIXED_REC_IDS else _auto_rec_loopback()
        if lb is None:
            raise SystemExit("[錯誤] 找不到系統音訊的擷取來源（macOS 需要螢幕錄製權限或 BlackHole）")
        return lb, _rec_device_name(lb), "僅對方聲音", None, None
    if rec_source == "mic":
        mic = mic_device if mic_device is not None else _auto_rec_mic()
        if mic is None:
            raise SystemExit("[錯誤] 找不到麥克風")
        return mic, _rec_device_name(mic), "僅我方聲音", None, None
    lb = device if device is not None and device not in _MIXED_REC_IDS else _auto_rec_loopback()
    mic = mic_device if mic_device is not None else _auto_rec_mic()
    if lb is None and mic is None:
        raise SystemExit("[錯誤] 找不到任何音訊輸入裝置！")
    if mic is None:
        print(f"  {C_HIGHLIGHT}[提醒] 找不到麥克風，只錄系統音訊{RESET}")
        return lb, _rec_device_name(lb), "僅對方聲音", None, None
    if lb is None:
        print(f"  {C_HIGHLIGHT}[提醒] 找不到系統音訊的擷取來源，只錄麥克風{RESET}")
        return mic, _rec_device_name(mic), "僅我方聲音", None, None
    return _mixed_rec_id(), f"{_rec_device_name(lb)} + {_rec_device_name(mic)}", "雙方聲音", lb, mic


def _ask_record_source():
    """純錄音模式：選擇錄音來源（雙方聲音 / 僅對方聲音）。
    回傳 (device_id, device_name, label)，找不到裝置則 sys.exit(1)。"""
    _last_rec = _config.get("last_rec_choice")  # "1"=混合/雙方 / "2"=僅播放/僅對方
    # 系統音訊 + 麥克風混合（Windows: WASAPI Loopback，macOS: ScreenCaptureKit）
    _wb_info = _find_wasapi_loopback() if IS_WINDOWS else None
    if IS_WINDOWS:
        _sys_audio_ok = bool(_wb_info)
    elif IS_LINUX:
        _sys_audio_ok = _pulse_available()
    else:
        _sys_audio_ok = IS_MACOS and _sck_available()
    if _sys_audio_ok:
        if IS_WINDOWS:
            lb_name = f"WASAPI Loopback ({_wb_info['name']})"
            _lb_id, _mixed_id = WASAPI_LOOPBACK_ID, WASAPI_MIXED_ID
        elif IS_LINUX:
            lb_name = _pulse_label()
            _lb_id, _mixed_id = PULSE_LOOPBACK_ID, PULSE_MIXED_ID
        else:
            lb_name = "ScreenCaptureKit 系統音訊"
            _lb_id, _mixed_id = SCK_LOOPBACK_ID, SCK_MIXED_ID
        mic_id = _find_mac_mic() if IS_MACOS else _find_default_mic()
        if mic_id is None:
            print(f"  {C_OK}錄音裝置: {lb_name}{RESET}")
            return _lb_id, lb_name, "僅對方聲音"

        import sounddevice as sd
        mic_name = sd.query_devices(mic_id)["name"]
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
            return _lb_id, lb_name, "僅對方聲音"
        print(f"  {C_OK}→ 雙方聲音{RESET}")
        if _last_rec != "1":
            _config["last_rec_choice"] = "1"
            save_config(_config)
        return _mixed_id, mixed_name, "雙方聲音"

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


def run_record_only(rec_device, topic=None, lb_device=None, mic_device=None, channels=None):
    """純錄音模式：僅錄製音訊為 WAV 檔，不做 ASR 或翻譯。
    聚集裝置（ch>=3）自動分離輸出/輸入音軌並分開顯示波形。
    混合錄音（rec_device 是混合錄音代號）：lb_device／mic_device 是要混的兩個來源，沒給就自動選（v2.26.3）"""
    import sounddevice as sd
    import numpy as np

    _is_mixed = rec_device in _MIXED_REC_IDS
    _mixer = None
    _mic_stream = None
    _lb_device_id = lb_device if lb_device is not None else _sys_audio_loopback_id()

    if _is_mixed:
        # 混合錄音模式：2 個串流（系統音訊 + Mic），波形顯示 2 行
        rec_sr, _ = _capture_stream_info(_lb_device_id, cap_channels=None)
        rec_ch = 2  # 波形顯示用 2 行（系統音訊 / Mic）
        mic_id = mic_device if mic_device is not None else _auto_rec_mic()
        if mic_id is None:
            print("[錯誤] 找不到麥克風，無法混合錄音", file=sys.stderr)
            sys.exit(1)
        dev_name = f"{_rec_device_name(_lb_device_id)} + {_rec_device_name(mic_id)}"
    elif _is_sys_audio_device(rec_device):
        rec_sr, rec_ch = _capture_stream_info(rec_device, cap_channels=None)
        if IS_MACOS:
            dev_name = "ScreenCaptureKit 系統音訊"
        elif IS_LINUX:
            dev_name = _pulse_label()
        else:
            dev_name = f"WASAPI Loopback ({_find_wasapi_loopback()['name']})"
    else:
        dev_info = sd.query_devices(rec_device)
        rec_sr = int(dev_info["default_samplerate"])
        # channels：只錄麥克風時固定單聲道（v2.26.3）；其他照裝置的聲道數（聚集裝置要分得出各軌）
        rec_ch = channels or max(dev_info["max_input_channels"], 1)
        dev_name = dev_info["name"]

    stop_event = threading.Event()
    # 暫停（v2.26.3）：WebUI 的「暫停」以前對純錄音沒有作用，畫面寫已暫停、檔案照錄。暫停期間不寫入檔案
    pause_event = threading.Event()
    global _webui_pause_event
    _webui_pause_event = pause_event

    # 每個聲道獨立的滾動音量歷史（波形顯示）
    _WAVE_MAX = 80  # 最多保留 80 筆歷史（約 8 秒）
    _level_lock = threading.Lock()

    if _is_mixed:
        # 混合模式：用 _DualStreamMixer，波形分 Loopback / Mic 兩行
        recorder = _AudioRecorder(rec_sr, 1, topic=topic)
        _mixer = _DualStreamMixer(recorder, rec_sr)
        _ch_histories = [deque(maxlen=_WAVE_MAX), deque(maxlen=_WAVE_MAX)]

        def lb_callback(indata, frames, time_info, status):
            if stop_event.is_set() or pause_event.is_set():
                return
            audio = indata.astype(np.float32)
            if audio.ndim > 1 and audio.shape[1] > 1:
                mono = audio.mean(axis=1)
            else:
                mono = audio.flatten()
            _mixer.add_loopback(mono)
            with _level_lock:
                _ch_histories[0].append(float(np.sqrt(np.mean(mono ** 2))))

        mic_info = sd.query_devices(mic_id)
        mic_sr = int(mic_info["default_samplerate"])

        def mic_callback(indata, frames, time_info, status):
            if stop_event.is_set() or pause_event.is_set():
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
            _lb_sr, _lb_ch = _capture_stream_info(_lb_device_id, cap_channels=None)
            stream = _open_capture_stream(
                _lb_device_id, lb_callback, _lb_sr, _lb_ch,
                blocksize=int(_lb_sr * 0.1))
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
            if stop_event.is_set() or pause_event.is_set():
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
            stream = _open_capture_stream(
                rec_device, rec_callback, rec_sr, rec_ch,
                blocksize=int(rec_sr * 0.1))
        except Exception as e:
            print(f"[錯誤] 無法開啟錄音裝置 [{rec_device}] {dev_name}: {e}", file=sys.stderr)
            recorder.close()
            sys.exit(1)

    if recorder.disk_stopped:             # 一開始空間就不夠（訊息已印出）：沒錄到東西，空檔也不留
        stream.close()
        if _mic_stream:
            _mic_stream.close()
        try:
            os.remove(recorder.close())
        except OSError:
            pass
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
    # 暫停的時間不算在錄音長度裡
    _paused_total = 0.0
    _paused_since = None
    _rec_rel = os.path.relpath(recorder.path, os.path.dirname(os.path.abspath(__file__)))
    _webui_send({"type": "started", "mode": "record"})
    _webui_send({"type": "progress", "stage": "錄音中", "detail": _rec_rel})
    _last_rms_sent = 0.0
    # 從 WebUI 啟動時，輸出接在使用者開著的終端機視窗：每 0.15 秒重畫波形會讓終端機與 WindowServer
    # 一直重繪（2026-10-02 使用者在 Mac 上覺得變慢）。WebUI 有自己的波形，這裡改成每 10 秒一行狀態
    _quiet = _webui_queue is not None
    _last_quiet = 0.0

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
            if recorder.disk_stopped:         # 磁碟快滿，錄音元件已停止寫入：收尾、轉檔
                break
            now_t = time.monotonic()
            if pause_event.is_set() and _paused_since is None:
                _paused_since = now_t
                _webui_send({"type": "progress", "stage": "已暫停", "detail": "這段不會錄進檔案"})
            elif not pause_event.is_set() and _paused_since is not None:
                _paused_total += now_t - _paused_since
                _paused_since = None
                if _mixer:
                    _mixer.reset()
                _webui_send({"type": "progress", "stage": "錄音中", "detail": _rec_rel})
            elapsed = now_t - start_time - _paused_total - ((now_t - _paused_since) if _paused_since else 0.0)
            # WebUI 右上的音量波形（每 0.3 秒；混合錄音取兩路較大的）
            if now_t - _last_rms_sent >= 0.3:
                _last_rms_sent = now_t
                with _level_lock:
                    _lv = max((h[-1] for h in _ch_histories if h), default=0.0)
                _webui_send({"type": "rms", "value": 0.0 if _paused_since else float(_lv)})
            secs = int(elapsed)
            if secs >= 3600:
                ts_raw = f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
            else:
                ts_raw = f"{secs // 60:02d}:{secs % 60:02d}"
            ts = ts_raw.rjust(_TS_W)
            if _quiet:
                if now_t - _last_quiet >= 10:
                    _last_quiet = now_t
                    _state = "暫停中（這段不會錄進檔案）" if _paused_since is not None else "錄音中"
                    print(f"  {ts_raw}  {_state}  {(44 + recorder._data_size) / 1048576:.1f} MB", flush=True)
                continue
            if _paused_since is not None:
                ts = f"{ts}  {C_HIGHLIGHT}暫停中（這段不會錄進檔案）{RESET}"

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
        _end_t = time.monotonic()           # 錄音到這裡為止；以前在轉檔之後才量，時長把轉檔時間也算進去
        _webui_send({"type": "progress", "stage": "存檔中", "finishing": True, "detail": "停止錄音"})
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
        _webui_send({"type": "progress", "stage": "存檔中", "finishing": True, "detail": "寫入錄音檔"})
        path = recorder.close()
        elapsed = _end_t - start_time - _paused_total - ((_end_t - _paused_since) if _paused_since else 0.0)
        _webui_send_realtime_results(None, [path])
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


# ─── 文字轉語音：朗讀模式（v2.27.0）──────────────────────────────
# 朗讀是主程式的一個模式（2026-10-09 使用者：「要整合進本來流程」）：WebUI 的「文字內容朗讀／文字轉語音檔」、
# 命令列 --tts-file／--tts-text 都走這裡；字幕（終端機、WebUI 對話／字幕模式、懸浮字幕）用既有的 transcription 事件。
# 播放三種：本機喇叭（sounddevice）、瀏覽器（WebUI 依序取每段音檔播放，播到哪段回報，字幕跟著那段出現）、不播放只存檔
_TTS_POS_FLAG = os.path.join(SCRIPT_DIR, ".webui_tts_pos")   # webui.py 寫入「<朗讀編號> <瀏覽器正在播第幾段>」
_TTS_BROWSER_STALL = 90          # 瀏覽器多久沒有播下一段就當成分頁關了（另加兩倍的段落長度）


def _tts_output_devices():
    """播放裝置清單（有輸出聲道的）。Windows 同一個裝置會依 MME／DirectSound／WASAPI 各列一次，只列系統預設輸出那一組"""
    out = []
    try:
        import sounddevice as sd
        default_out = sd.default.device[1]
        ha = None
        if default_out is not None and default_out >= 0:
            try:
                ha = sd.query_devices(default_out)["hostapi"]
            except Exception:
                ha = None
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and (ha is None or d["hostapi"] == ha):
                out.append({"id": i, "name": d["name"], "default": i == default_out,
                            "sr": int(d["default_samplerate"])})
    except Exception:                       # 沒有音效卡（伺服器、容器）：清單是空的，WebUI 只剩「瀏覽器」
        pass
    return out


def _tts_fail(msg):
    """朗讀開始前的錯誤：印在錯誤輸出（WebUI「啟動失敗」卡片看得到），結束碼 1"""
    print(f"[錯誤] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def _tts_list():
    try:
        import jtlw_tts as T
    except Exception as e:
        _tts_fail(f"文字轉語音元件載入失敗（{type(e).__name__}: {e}）：請再執行一次升級")
    default_vid = T.default_voice_id(load_config())
    print(f"\n{C_TITLE}{BOLD}▎ 聲音{RESET}")
    vs = T.list_voices()
    if not vs:
        print(f"  {C_DIM}還沒有聲音：在 WebUI 的「文字內容朗讀」按「管理聲音」匯入一段台灣華語錄音{RESET}")
    for v in vs:
        mark = f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if v["id"] == default_vid else ""
        if v.get("builtin"):
            mark = f"  {C_DIM}內建{RESET}" + mark
        g = T.GENDERS.get(v.get("gender") or "", "")
        print(f"  {v['id']}  {v['name']}{'（' + g + '）' if g else ''}  {C_DIM}{v.get('duration')} 秒，來源：{v.get('source')}{RESET}{mark}")
    en = T.list_voices("en")
    if en:
        print(f"\n{C_TITLE}{BOLD}▎ 英文聲音（雙向口譯念給對方聽，--speak-them-voice）{RESET}")
        en_vid = T.default_en_voice_id()
        for v in en:
            mark = (f"  {C_DIM}內建{RESET}" if v.get("builtin") else "") + (f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if v["id"] == en_vid else "")
            print(f"  {v['id']}  {v['name']}  {C_DIM}{v.get('duration')} 秒，來源：{v.get('source')}{RESET}{mark}")
    print(f"\n{C_TITLE}{BOLD}▎ 播放裝置{RESET}")
    devs = _tts_output_devices()
    if not devs:
        print(f"  {C_DIM}找不到播放裝置（伺服器、容器沒有音效卡）：用 --tts-device none --tts-save mp3 轉成音訊檔{RESET}")
    for d in devs:
        print(f"  [{d['id']}] {d['name']}" + (f"  {C_HIGHLIGHT}{REVERSE} 系統預設 {RESET}" if d["default"] else ""))
    print()


def _tts_play(stream, pcm, sr, pause_ev):
    """每次寫 0.1 秒：暫停、停止（Ctrl+C）都能立刻生效"""
    block = max(2, int(sr * 0.1)) * 2
    for off in range(0, len(pcm), block):
        while pause_ev.is_set():
            time.sleep(0.1)
        stream.write(pcm[off:off + block])


def _tts_open_stream(dev, sr):
    """開播放裝置。裝置不支援模型的取樣率時改用裝置預設的（之後每段重新取樣）。回傳 (stream, 實際取樣率)"""
    import sounddevice as sd
    try:
        st = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16", device=dev)
        st.start()
        return st, sr
    except Exception:
        info = sd.query_devices(dev if dev is not None else sd.default.device[1])
        osr = int(info["default_samplerate"])
        st = sd.RawOutputStream(samplerate=osr, channels=1, dtype="int16", device=dev)
        st.start()
        return st, osr


# ── 雙向語音口譯（v2.28.0，規格 specs/2026-10-09_雙向語音口譯規格_v0.1.md）──────────────
# 對方說的英文（系統音訊）翻成中文 → 念給我聽（耳機）；我說的中文（麥克風）翻成英文 → 念進虛擬麥克風給對方聽。
# 排程與回授過濾在 jtlw_tts/interp.py；這裡是裝置、合成與接到雙向模式
_INTERP_MODES = ("en_zh",)
# 念給對方聽要把英文送進會議軟體的麥克風：要有虛擬麥克風（2026-10-10 使用者決定）。
# macOS 安裝 BlackHole 2ch（GPL-3.0，免費）；Linux 程式自動建立；
# Windows（v2.29.0）：安裝 usbip-win2（開放原始碼 BSD-2-Clause，核心驅動由微軟簽署），程式自己當一支 USB 麥克風（jtlw_tts/vmic.py）。
# 常見的虛擬音效卡是捐贈軟體、公司使用要付費，授權不合適；其他開放原始碼的音訊驅動要開測試簽章模式才裝得起來
_INTERP_MAC_NEED = "需要先安裝 BlackHole 2ch（免費，GPL-3.0）：brew install --cask blackhole-2ch，裝完重新開機；會議軟體的麥克風改選「BlackHole 2ch」"
_INTERP_WIN_MIC = "jt-live-whisper Interpreter Mic"
_INTERP_WIN_NEED = ("需要先安裝 usbip-win2（免費、開放原始碼，驅動由微軟簽署）：在安裝資料夾執行 .\\install.ps1 -InterpMic；"
                    "之後開始時自動建立「" + _INTERP_WIN_MIC + "」，會議軟體的麥克風改選它")
_INTERP_WIN_READY = "開始時自動建立「" + _INTERP_WIN_MIC + "」（usbip-win2）：會議軟體的麥克風改選它，結束後記得改回來"
_INTERP_SINK = "jtlw_interp"                 # Linux --speak-them auto 自動建立的虛擬裝置（結束時移除）
_INTERP_SRC = "jtlw_interp_mic"
_interp_modules = []                         # 自己載入（或接手上一次留下）的 PulseAudio 模組編號
_interp_atexit = [False]


def _interp_linux_sink():
    """Linux：建立虛擬喇叭（念給對方聽的英文播到這裡）＋把它的聲音當成麥克風（會議軟體選「jt-live-whisper 口譯麥克風」）。
    上一次沒收乾淨（當掉、被強制結束）留下的就接手：記進 _interp_modules，結束時一起移除（以前沿用但不記，永遠不會被移除）。
    結束時一定移除：雙向模式的收尾、_force_exit、atexit（載入模型時按 Ctrl+C 會直接 sys.exit）。回傳錯誤說明或 None"""
    import subprocess as sp
    try:
        mods = sp.run(["pactl", "list", "short", "modules"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, sp.SubprocessError) as e:
        return f"找不到 pactl（PipeWire／PulseAudio），不能建立虛擬麥克風：{type(e).__name__}"
    have = {}
    for line in mods.splitlines():
        f = line.split("\t")
        if len(f) >= 3 and f[1] == "module-null-sink" and f"sink_name={_INTERP_SINK}" in f[2].split():
            have["sink"] = f[0]
        if len(f) >= 3 and f[1] == "module-remap-source" and f"source_name={_INTERP_SRC}" in f[2].split():
            have["src"] = f[0]
    for k in ("sink", "src"):
        if k in have and have[k] not in _interp_modules:
            _interp_modules.append(have[k])
    cmds = []
    if "sink" not in have:
        cmds.append(["pactl", "load-module", "module-null-sink", f"sink_name={_INTERP_SINK}",
                     "sink_properties=device.description=jt-live-whisper-interpreter"])
    if "src" not in have:
        cmds.append(["pactl", "load-module", "module-remap-source", f"master={_INTERP_SINK}.monitor",
                     f"source_name={_INTERP_SRC}", "source_properties=device.description=jt-live-whisper-interpreter-mic"])
    if not _interp_atexit[0]:
        _interp_atexit[0] = True
        atexit.register(_interp_linux_cleanup)
    for c in cmds:
        r = sp.run(c, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return f"建立虛擬麥克風失敗：{(r.stderr or r.stdout).strip()[:200]}"
        _interp_modules.append(r.stdout.strip())
    return None


def _interp_linux_cleanup():
    """移除虛擬麥克風（Linux 的 PulseAudio 模組、Windows 的口譯麥克風）。
    WebUI 切換裝置（重新啟動主程式）時保留給新的程式接手（.webui_interp_keep，60 秒內寫的才算）：
    移除的話會議軟體會改用實體麥克風，新的建好之後也不會自己切回來，對方就聽到原聲而不是英文。回傳是不是保留了"""
    import subprocess as sp
    try:
        keep = (_interp_modules or _interp_vmic) and time.time() - os.path.getmtime(_INTERP_KEEP_FILE) < 60
    except OSError:
        keep = False
    while _interp_vmic:
        # Windows：保留＝只關伺服器，usbip-win2 會一直重試、新的程式起來就接回同一個接口（同一支裝置）
        try:
            _interp_vmic.pop().close(keep=bool(keep))
        except Exception:
            pass
    if keep:
        _interp_modules.clear()
        return True
    while _interp_modules:
        m = _interp_modules.pop()
        try:
            sp.run(["pactl", "unload-module", m], capture_output=True, timeout=5)
        except (OSError, sp.SubprocessError):
            pass
    return False


_interp_vmic = []                            # Windows：這一場建立的口譯麥克風（jtlw_tts.vmic.WinVirtualMic）


def _interp_win_ready():
    """Windows 能不能自動建立口譯麥克風（裝了 usbip-win2）"""
    if not IS_WINDOWS:
        return False
    try:
        from jtlw_tts import vmic
    except Exception:                            # 從舊版第一次升級拿不到 vmic.py
        return False
    return vmic.usbip_installed()


def _interp_win_mic():
    """Windows：建立口譯麥克風（usbip-win2＋本機的 USB 麥克風伺服器）。回 (物件, 錯誤)。
    結束時一定移除：雙向模式的收尾、_force_exit、atexit；WebUI 切換裝置時留給新的程式接手（_interp_linux_cleanup）"""
    if _interp_vmic:
        return _interp_vmic[0], None
    try:
        from jtlw_tts import vmic
    except Exception as e:
        return None, f"口譯麥克風元件不完整（{type(e).__name__}: {e}）：請再執行一次升級"
    if not vmic.usbip_installed():
        return None, _INTERP_WIN_NEED
    m = vmic.WinVirtualMic()
    err = m.start()
    if err:
        return None, err
    _interp_vmic.append(m)
    if not _interp_atexit[0]:
        _interp_atexit[0] = True
        atexit.register(_interp_linux_cleanup)
    return m, None


def _interp_find_device(spec):
    """--speak-me／--speak-them 的裝置 → ((種類, 值), 錯誤)。種類：sd＝sounddevice 編號（None＝系統預設）；
    pulse＝Linux 的 PulseAudio／PipeWire 裝置名稱（sounddevice 在 Linux 只看得到 ALSA，個別的虛擬裝置要用 pacat 播）"""
    spec = str(spec).strip()
    if spec.lower() == "default":
        return ("sd", None), None
    if spec.lstrip("-").isdigit():
        # 編號一律是 sounddevice 的（WebUI、互動選單送的都是）；要先判斷，不然 Linux 會拿「1」去比對
        # PulseAudio 裝置名稱的一部分（alsa_output.pci-0000_00_1f...），播到別的裝置
        import sounddevice as sd
        try:
            devs = sd.query_devices()
        except Exception as e:
            return None, f"列不出播放裝置：{type(e).__name__}: {e}"
        i = int(spec)
        if 0 <= i < len(devs) and devs[i]["max_output_channels"] > 0:
            return ("sd", i), None
        return None, f"沒有編號 {i} 的播放裝置（--tts-list 列出全部）"
    if spec.lower() == "auto":
        if IS_WINDOWS:
            m, err = _interp_win_mic()
            return (None, err) if err else (("vmic", m), None)
        if not IS_LINUX:
            return None, ("auto 只有 Linux 與 Windows（自動建立虛擬麥克風）。" + _INTERP_MAC_NEED + "，再用 --speak-them \"BlackHole\" 指定")
        err = _interp_linux_sink()
        return (None, err) if err else (("pulse", _INTERP_SINK), None)
    if IS_LINUX:
        import subprocess as sp
        try:
            sinks = [l.split("\t")[1] for l in sp.run(["pactl", "list", "short", "sinks"], capture_output=True,
                                                         text=True, timeout=5).stdout.splitlines() if "\t" in l]
        except (OSError, sp.SubprocessError):
            sinks = []
        hit = [s for s in sinks if s == spec] or [s for s in sinks if spec.lower() in s.lower()]
        if hit:
            return ("pulse", hit[0]), None
    import sounddevice as sd
    try:
        devs = list(enumerate(sd.query_devices()))
    except Exception as e:
        return None, f"列不出播放裝置：{type(e).__name__}: {e}"
    hit = [i for i, d in devs if d["max_output_channels"] > 0 and spec.lower() in d["name"].lower()]
    if not hit:
        return None, f"找不到名稱含「{spec}」的播放裝置（--tts-list 列出全部）"
    return ("sd", hit[0]), None


def _resample_pcm(pcm, sr, osr):
    """16-bit 單聲道線性內插重新取樣（串流的每一段都要換時用；整句的用 ffmpeg）"""
    import numpy as np
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if not len(x) or sr == osr:
        return pcm
    n = max(1, int(round(len(x) * osr / sr)))
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


class _InterpAudio:
    """每個方向一個播放裝置：sounddevice 的 RawOutputStream、Linux 的 pacat（指定 PulseAudio 裝置）、
    或 Windows 的口譯麥克風（vmic：每個方向一個聲道，在麥克風裡混音）"""

    def __init__(self, devices, pause_ev=None):
        self.devices = devices                     # {lane: (種類, 值)}
        self.pause_ev = pause_ev or threading.Event()
        self._out = {}                             # lane → (物件, 實際取樣率, 模型取樣率)
        self._end = {}                             # lane → pacat 預計播完的時間

    def _open(self, lane, sr):
        kind, val = self.devices[lane]
        if kind == "vmic":
            from jtlw_tts import vmic
            return vmic.Feeder(val.mic, lane), vmic.RATE
        if kind == "pulse":
            import subprocess as sp
            p = sp.Popen(["pacat", "--playback", f"--device={val}", "--format=s16le", f"--rate={sr}", "--channels=1",
                          "--latency-msec=100"], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            return p, sr
        return _tts_open_stream(val, sr)

    def play(self, lane, pcm, sr):
        cur = self._out.get(lane)
        if cur is None or cur[2] != sr:
            self._close(lane)
            obj, osr = self._open(lane, sr)
            cur = self._out[lane] = (obj, osr, sr)
        obj, osr, _ = cur
        if osr != sr:
            pcm = _resample_pcm(pcm, sr, osr)
        try:
            if hasattr(obj, "sink"):                # Windows 的口譯麥克風：照真實時間送，大約念完才回來（跟喇叭一樣）
                obj.write(pcm, self.pause_ev)
            elif hasattr(obj, "stdin"):
                while self.pause_ev.is_set():
                    time.sleep(0.1)
                # pacat 不會等播完：照時間軸等到這段快播完（留一點緩衝，段與段之間才不會斷）。
                # 管線滿的時候 write 本身就會擋（pacat 照播放速度讀），所以不可以寫完再整段等一次（以前 5 秒的段落要 9 秒）
                now = time.monotonic()
                end = max(now, self._end.get(lane, 0.0)) + len(pcm) / 2 / osr
                self._end[lane] = end
                obj.stdin.write(pcm)
                obj.stdin.flush()
                time.sleep(max(0.0, end - time.monotonic() - 0.3))
            else:
                _tts_play(obj, pcm, osr, self.pause_ev)
        except Exception:
            self._close(lane)                       # 耳機拔掉、pacat 結束：丟掉這個串流，下一句重新開（以前之後每句都失敗）
            raise

    def _close(self, lane):
        cur = self._out.pop(lane, None)
        self._end.pop(lane, None)
        if not cur:
            return
        obj = cur[0]
        try:
            if hasattr(obj, "sink"):
                pass                                # 麥克風本身由 _interp_linux_cleanup 收
            elif hasattr(obj, "stdin"):
                obj.stdin.close()
                obj.wait(timeout=3)
            else:
                obj.stop()
                obj.close()
        except Exception:
            pass

    def close(self):
        for lane in list(self._out):
            self._close(lane)


class _InterpPassthrough:
    """同時送出原音（--passthrough）：麥克風的聲音即時送進虛擬麥克風，對方也聽得到你的原聲；念英文時原聲調小。
    跟念出來的英文是兩個播放串流，混音由系統做（BlackHole、虛擬音效卡、PulseAudio 都會混）。
    暫停時照樣送（暫停的是口譯，不是你的麥克風）；WebUI 把麥克風靜音時不送"""
    DUCK = 0.25
    MAX_Q = 20                      # 約 2 秒：裝置卡住寫不出去時丟掉舊的，延遲不會越積越長

    def __init__(self, device, guard):
        import queue
        self.audio = _InterpAudio({"pt": device})
        self.guard = guard
        self.q, self._empty = queue.Queue(), queue.Empty
        self._stop = threading.Event()
        self.error = None

    def start(self):
        threading.Thread(target=self._loop, name="interp-passthrough", daemon=True).start()
        return self

    def push(self, audio, sr):
        """audio：float32 單聲道（-1～1）"""
        import numpy as np
        if self._stop.is_set() or self.error:
            return
        g = self.DUCK if self.guard.busy("them") else 1.0
        pcm = (np.clip(np.asarray(audio, dtype=np.float32) * g, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        while self.q.qsize() >= self.MAX_Q:
            try:
                self.q.get_nowait()
            except self._empty:
                break
        self.q.put((pcm, sr))

    def _loop(self):
        while not self._stop.is_set():
            try:
                pcm, sr = self.q.get(timeout=0.3)
            except self._empty:
                continue
            try:
                self.audio.play("pt", pcm, sr)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"[:200]
                with _interp_print_lock:
                    print(f"{C_DIM}  [口譯給對方] 原音送不出去，只送英文：{self.error}{RESET}", flush=True)
                return

    def stop(self):
        self._stop.set()
        self.audio.close()


def _interp_build(args, mode, pause_ev=None):
    """命令列的口譯參數 → (Interpreter（還沒啟動）, None)、(None, 錯誤說明)；沒有開口譯時 (None, None)"""
    if not (args.speak_me or args.speak_them):
        return None, None
    if mode not in _INTERP_MODES:
        return None, "語音口譯目前只支援英中雙向（--mode en_zh）"
    try:
        import jtlw_tts as T
        from jtlw_tts import interp as I
    except Exception as e:                       # 從舊版第一次 --upgrade 拿不到新加的 interp.py（舊的安裝程式、舊的清單）
        return None, f"語音口譯元件不完整（{type(e).__name__}: {e}）：請再執行一次升級"
    cfg = load_config()
    s = T.settings(cfg)
    # 合成只用 GPU 伺服器：Apple Silicon 本機合成約跟說話一樣快，兩個方向加上辨識跟不上（規格第一節）
    prov, why = T.pick_provider(cfg, refresh=True, where="remote")
    if prov is None:
        return None, f"語音口譯要在 GPU 伺服器合成：{why}"
    try:
        health = prov.health()
    except Exception:
        health = {}
    stream = bool(health.get("stream"))
    if args.speak_them and "en" not in (health.get("langs") or ()):
        # v2.27.0 的伺服器不認得 lang=en：英文句子會套台灣念法、數字念成中文，念給對方聽一定錯
        return None, (f"GPU 伺服器的版本太舊（{health.get('version') or '不明'}），念不對英文：念給對方聽要 v2.28.0 以上的伺服器"
                      "（設定了自動更新密鑰會自己更新，否則請在 GPU 伺服器更新 server.py）")
    if args.speak_them and IS_WINDOWS and str(args.speak_them).strip().lower() != "auto":
        # Windows 只用自己的口譯麥克風：其他常見的虛擬音效卡授權不合適（2026-10-10 使用者決定），喇叭則會念給自己聽
        return None, "Windows 的念給對方聽請用 --speak-them auto（自動建立「" + _INTERP_WIN_MIC + "」）"
    lanes, devices = {}, {}
    for lane, dev, vid, rate, lang in ((I.ME, args.speak_me, args.speak_me_voice, args.speak_me_rate, "zh"),
                                       (I.THEM, args.speak_them, args.speak_them_voice, args.speak_them_rate, "en")):
        if not dev:
            continue
        if not T.RATE_MIN <= float(rate) <= T.RATE_MAX:
            return None, f"語速要在 {T.RATE_MIN}～{T.RATE_MAX} 之間"
        d, err = _interp_find_device(dev)
        if err:
            return None, err
        v = T.get_voice(vid or (T.default_en_voice_id() if lang == "en" else "") or T.default_voice_id(cfg), missing_ok=True)
        if v is None:
            return None, f"找不到聲音 {vid}（--tts-list 列出全部）"
        if lang == "en" and not vid and v.get("lang") != "en":
            print(f"{C_HIGHLIGHT}[提示] 沒有英文聲音（從舊版第一次升級拿不到，再執行一次升級就有），先用台灣華語的聲音念英文（會有口音）{RESET}")
        lanes[lane] = {"rate": float(rate), "voice": v, "lang": lang}
        devices[lane] = d
    pace = I.Pace()

    def synth(item, rate):
        L = lanes[item.lane]
        if stream and abs(rate - 1.0) < 1e-3:
            need = pace.prebuffer(I.est_seconds(item.text, L["lang"]))
            return I.buffered(prov.synth_stream(item.text, L["voice"], L["lang"],
                                                custom=s["custom"] if L["lang"] == "zh" else None), need, pace)
        t0 = time.monotonic()
        wav, _ = prov.synth(item.text, L["voice"], s["custom"] if L["lang"] == "zh" else {}, lang=L["lang"])
        raw, sr0 = T.wav_pcm(wav)
        pace.update(time.monotonic() - t0, len(raw) / 2 / sr0)
        return T.to_pcm(wav, rate)

    def warm():
        """開始時先各合成一句短的（背景）：GPU 的合成程式沒在跑時第一次要啟動約 28 秒，不先叫醒的話會議一開始的幾句
        等超過 15 秒就被略過；順便上傳聲音、量現在的合成速度（GPU 忙的時候比說話慢，串流要先存多一點）"""
        err = None
        for L in lanes.values():
            try:
                t0 = time.monotonic()
                wav, _ = prov.synth("好的。" if L["lang"] == "zh" else "Okay.", L["voice"], {}, lang=L["lang"])
                raw, sr0 = T.wav_pcm(wav)
                if time.monotonic() - t0 < 20:                  # 啟動合成程式的那一次不算速度
                    pace.update(time.monotonic() - t0, len(raw) / 2 / sr0)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:160]
        with _interp_print_lock:
            if err is None:
                print(f"{C_DIM}  [語音口譯] GPU 語音合成已就緒{RESET}", flush=True)
            else:                                               # 以前失敗也印「已就緒」
                print(f"{C_HIGHLIGHT}  [語音口譯] GPU 語音合成沒有準備好：{err}（會議中每一句會再試）{RESET}", flush=True)

    audio = _InterpAudio(devices, pause_ev)
    ip = I.Interpreter(lanes, synth, audio.play, on_event=_interp_event)
    ip.passthrough = _InterpPassthrough(devices[I.THEM], ip.guard) \
        if getattr(args, "passthrough", False) and I.THEM in devices else None
    ip.warm = warm
    ip.audio, ip.provider, ip.streaming = audio, prov, stream
    intro = I.INTRO_EN if args.interp_intro is None else args.interp_intro
    ip.intro = None if str(intro).strip().lower() in ("", "none", "off") else intro
    return ip, None


_INTERP_VIRTUAL = re.compile(r"blackhole|virtual", re.I)   # 念給對方聽預選的虛擬裝置（同 webui.html）


def _ask_pick(title, items, default_idx, default_label):
    """列出 items（(值, 顯示文字)）讓使用者選，Enter＝default_idx。回傳值"""
    print(f"\n{C_TITLE}{BOLD}▎ {title}{RESET}")
    for k, (_, lab) in enumerate(items, 1):
        print(f"  {C_DIM}[{k}]{RESET} {C_WHITE}{lab}{RESET}" + (f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if k - 1 == default_idx else ""))
    print(f"{C_WHITE}選擇 (1-{len(items)}) [{default_label}]：{RESET}", end=" ")
    try:
        ans = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return items[int(ans) - 1][0] if ans.isdigit() and 1 <= int(ans) <= len(items) else items[default_idx][0]


def _ask_yes(q, default):
    print(f"{C_WHITE}{q}({'Y/n' if default else 'y/N'})：{RESET}", end=" ")
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return default if not ans else ans in ("y", "yes")


def _ask_interp(args, mode):
    """互動選單：英中雙向時問要不要語音口譯，選了就填 args.speak_me／speak_them（之後跟命令列走同一條路）。
    GPU 伺服器沒有文字轉語音就不問（沒設定 GPU 伺服器時什麼都不印）"""
    if mode not in _INTERP_MODES or args.speak_me or args.speak_them:
        return
    try:
        import jtlw_tts as T
        prov, why = T.pick_provider(load_config(), refresh=True, where="remote")
    except Exception as e:
        prov, why = None, f"{type(e).__name__}: {e}"
    if prov is None:
        if REMOTE_WHISPER_CONFIG:
            print(f"\n{C_DIM}（語音口譯要在 GPU 伺服器合成，現在不能用：{why}）{RESET}")
        return
    print(f"\n{C_TITLE}{BOLD}▎ 語音口譯{RESET}")
    print(f"  {C_DIM}把譯文念出來：對方的英文翻成中文念給你聽、你的中文翻成英文念給對方聽（合成在 GPU 伺服器）。{RESET}")
    print(f"  {C_DIM}請戴耳機；念給對方聽時，會議軟體的麥克風要改選虛擬裝置，結束後改回來。{RESET}")
    if IS_MACOS:
        print(f"  {C_HIGHLIGHT}念給對方聽{_INTERP_MAC_NEED}{RESET}")
    elif IS_WINDOWS:
        win_ok = _interp_win_ready()
        print(f"  {C_HIGHLIGHT}念給對方聽：{_INTERP_WIN_READY if win_ok else _INTERP_WIN_NEED}{RESET}")
    if not _ask_yes("是否開啟語音口譯？", False):
        return
    devs = _tts_output_devices()
    if _ask_yes("念給我聽（對方的英文 → 中文，從耳機）？", True):
        if len(devs) > 1:
            items = [("default", "系統預設的播放裝置")] + [(str(d["id"]), d["name"]) for d in devs]
            args.speak_me = _ask_pick("念給我聽：播放裝置（耳機）", items, 0, "系統預設")
        else:
            args.speak_me = "default"
    if (not IS_WINDOWS or win_ok) and _ask_yes("念給對方聽（我的中文 → 英文，送進虛擬麥克風）？", False):
        if IS_WINDOWS:
            args.speak_them = "auto"
        elif IS_LINUX:
            import subprocess as sp
            try:
                sinks = [l.split("\t")[1] for l in sp.run(["pactl", "list", "short", "sinks"], capture_output=True,
                                                             text=True, timeout=5).stdout.splitlines() if "\t" in l]
            except (OSError, sp.SubprocessError):
                sinks = []
            items = [("auto", "自動建立虛擬麥克風（會議軟體的麥克風選 jt-live-whisper-interpreter-mic）")] + \
                    [(n, n) for n in sinks if n != _INTERP_SINK]
            args.speak_them = _ask_pick("念給對方聽：送到哪裡", items, 0, "自動建立")
        else:
            virt = [d for d in devs if _INTERP_VIRTUAL.search(d["name"])]
            if not virt:
                print(f"  {C_HIGHLIGHT}沒有偵測到 BlackHole 2ch：{_INTERP_MAC_NEED}；這次只念給我聽{RESET}")
            else:
                items = [(str(d["id"]), d["name"]) for d in virt] + [(str(d["id"]), d["name"]) for d in devs if d not in virt]
                args.speak_them = _ask_pick("念給對方聽：虛擬麥克風（會議軟體的麥克風選它的另一端）", items, 0, virt[0]["name"])
        if args.speak_them and not _ask_yes("開場先用英文告訴對方在用 AI 口譯？", True):
            args.interp_intro = "none"
        if args.speak_them and _ask_yes("同時送出你的原聲（念英文時原聲自動調小）？", False):
            args.passthrough = True
    if not (args.speak_me or args.speak_them):
        print(f"  {C_DIM}→ 不念（只顯示字幕）{RESET}")


_interp_print_lock = threading.Lock()
_INTERP_CMD_FILE = os.path.join(SCRIPT_DIR, ".webui_interp_cmd")    # webui.py 的 INTERP_CMD（取消、靜音）
_INTERP_KEEP_FILE = os.path.join(SCRIPT_DIR, ".webui_interp_keep")  # webui.py 的 INTERP_KEEP（切換裝置：虛擬麥克風留給新的程式）


def _interp_watch_cmds(interp, stop_event):
    """WebUI 的口譯控制：指令檔一行一個（cancel <編號>、mute me|them 1|0）。先改名再讀，WebUI 同時寫入也不會掉"""
    work = _INTERP_CMD_FILE + ".work"
    for f in (_INTERP_CMD_FILE, work):
        try:
            os.remove(f)                            # 上一場留下的不算
        except OSError:
            pass
    while not stop_event.is_set():
        time.sleep(0.2)
        try:
            os.replace(_INTERP_CMD_FILE, work)
            with open(work, encoding="utf-8") as f:
                lines = f.read().splitlines()
            os.remove(work)
        except OSError:
            continue
        for line in lines:
            p = line.split()
            try:
                if p[0] == "cancel":
                    interp.cancel(int(p[1]))
                elif p[0] == "mute":
                    interp.mute(p[1], p[2] == "1")
            except (IndexError, ValueError):
                pass


_interp_err_shown = {}


def _interp_reject_lang(interp, passthrough, length_ms):
    """系統音訊這一段要不要請 GPU 伺服器先判斷語言、是中文就不辨識（雙向口譯的回授）。
    只在這段錄音可能錄到自己的中文時才送：念給我聽正在念或剛念過（這段錄音的長度內，加 2 秒延遲）；
    同時送出原聲只在 macOS（ScreenCaptureKit 錄的是所有程式的聲音，可能錄到自己送給 BlackHole 的原聲）。
    以前開了念給我聽就每一段都送：對方自己說中文、或只開原聲（Linux／Windows 本機根本沒播中文）時整段默默不見"""
    if interp is None:
        return None
    if "me" in interp.lanes and interp.guard.played_within("me", length_ms / 1000 + 2):
        return "zh"
    if passthrough is not None and IS_MACOS:
        return "zh"
    return None


def _interp_event(e, lock=None):
    """口譯的進度給 WebUI（每句標「排隊／念出／略過／取消／失敗」）；失敗與略過在終端機也說。
    同樣的失敗 60 秒內只說一次（GPU 伺服器斷線時每一句都會失敗，不要洗版）。
    lock：雙向模式的 print_lock（跟狀態列共用，才不會印到一半被狀態列蓋掉）"""
    _webui_send(e)
    if e.get("state") not in ("failed", "skipped"):
        return
    who = "給我" if e.get("lane") == "me" else "給對方"
    if e["state"] == "failed":
        key = e.get("reason") or e.get("error", "")[:80]
        now = time.monotonic()
        if now - _interp_err_shown.get(key, -1e9) < 60:
            return
        _interp_err_shown[key] = now
        why = {"down": "GPU 伺服器連不上，先不念（字幕照常），之後自動再試：", "play": "播放失敗："}.get(e.get("reason"), "合成失敗：") \
            + e.get("error", "").replace("GPU 伺服器連不上：", "")
    else:
        why = "等太久，不念了（字幕照樣顯示）"
    with (lock or _interp_print_lock):
        print(f"{C_DIM}  [口譯{who}] {why}｜{e.get('text', '')[:40]}{RESET}", flush=True)


def _tts_browser_pos(job):
    try:
        with open(_TTS_POS_FLAG, encoding="utf-8") as f:
            j, seq = f.read().split()[:2]
        return int(seq) if j == job else -1
    except (OSError, ValueError):
        return -1


def _tts_wait_browser(job, target, sess, pause_ev, stall):
    """等瀏覽器播到第 target 段（播完全部時 target＝段數）。瀏覽器沒有在播（分頁關了）超過 stall 秒就停"""
    last, since = -2, time.monotonic()
    while True:
        pos = _tts_browser_pos(job)
        if pos != last:
            last, since = pos, time.monotonic()
            if pos >= 0:
                sess.set_position(pos)
        if pos >= target:
            return
        if pause_ev.is_set():
            since = time.monotonic()
        elif time.monotonic() - since > stall:
            raise TimeoutError
        time.sleep(0.2)


def run_tts(args):
    """文字朗讀／文字轉語音檔（--tts-file、--tts-text）"""
    try:
        import jtlw_tts as T
        from jtlw_tts.tw_reading import _tts_split
    except Exception as e:
        _tts_fail(f"文字轉語音元件載入失敗（{type(e).__name__}: {e}）：請再執行一次升級（第一次升級拿不到新加的 jtlw_tts/）")
    cfg = load_config()
    s = T.settings(cfg)
    file_only = args.tts_device == "none"
    browser = args.tts_device == "browser"
    if browser and not args.webui:
        _tts_fail("瀏覽器播放只能從 WebUI 使用；命令列請用 --tts-device default 或裝置 ID")
    fmt = args.tts_save or ("mp3" if file_only else None)
    rate = float(args.tts_rate or 1.0)
    if not T.RATE_MIN <= rate <= T.RATE_MAX:
        _tts_fail(f"語速要在 {T.RATE_MIN}～{T.RATE_MAX} 倍之間（--tts-rate）")
    try:
        if args.tts_file:
            text = T.load_text(args.tts_file)
        else:
            text = T.prepare_text(args.tts_text or "")
    except OSError as e:
        _tts_fail(f"讀不到文字檔：{args.tts_file}（{e.strerror or e}）")
    except T.TTSError as e:
        _tts_fail(e.message)
    if len(text) > int(s["max_chars"]):
        _tts_fail(f"文字太長：一次最多 {int(s['max_chars'])} 字，這份有 {len(text)} 字")
    segments = _tts_split(text, int(s["chunk_chars"]))
    if not segments:
        _tts_fail("沒有可以朗讀的字（只有標點、空白或時間軸）")
    start = int(getattr(args, "tts_start", 1) or 1) - 1          # 重念：從第幾段開始（段號照整份文字）
    if not 0 <= start < len(segments):
        _tts_fail(f"--tts-start 要在 1～{len(segments)} 之間（這份文字共 {len(segments)} 段）")
    voice = T.get_voice(args.tts_voice or T.default_voice_id(cfg), missing_ok=True)
    if not voice:
        _tts_fail("還沒有設定聲音：在 WebUI 選「文字內容朗讀」→「管理聲音」匯入一段取得同意的台灣華語錄音"
                  if not args.tts_voice else f"找不到聲音 {args.tts_voice}（--tts-list 列出可用的聲音）")
    model = getattr(args, "tts_model", None) or T.DEFAULT_MODEL
    prov, why = T.pick_provider(cfg, refresh=True, where=args.tts_provider, steps=args.tts_steps, model=model)
    if not prov:
        _tts_fail(why)
    model_label = T.MODELS[model]["label"]
    dev, dev_label = None, "系統預設"
    if not file_only and not browser:
        devs = _tts_output_devices()
        if not devs:
            _tts_fail("這台沒有播放裝置（伺服器、容器沒有音效卡）：從別台電腦開 WebUI 選「瀏覽器」播放，"
                      "或改用「文字轉語音檔」")
        if args.tts_device not in ("default", ""):
            try:
                dev = int(args.tts_device)
            except ValueError:
                _tts_fail(f"--tts-device 要是 default、none 或裝置 ID，不是「{args.tts_device}」")
            hit = [d for d in devs if d["id"] == dev]
            if not hit:
                _tts_fail(f"找不到播放裝置 {dev}（--tts-list 列出可用的裝置）")
            dev_label = hit[0]["name"]
        else:
            dev_label = next((d["name"] for d in devs if d["default"]), "系統預設")
    n = len(segments)
    gap = T.PAUSES.get(args.tts_pause or "normal", 0.5)
    g = T.GENDERS.get(voice.get("gender") or "", "")
    out_label = "不播放，只存成音訊檔" if file_only else ("瀏覽器" if browser else dev_label)
    print(f"\n{C_TITLE}{BOLD}▎ {'文字轉語音檔' if file_only else '文字朗讀'}{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    print(f"  {C_WHITE}聲音：{voice['name']}{'（' + g + '）' if g else ''}　合成：{prov.label}・{model_label}　語速：{rate:g} 倍{RESET}")
    print(f"  {C_WHITE}播放：{out_label}{'　存檔：' + fmt.upper() if fmt else ''}　共 {n} 段、{len(text)} 字"
          f"{'　從第 ' + str(start + 1) + ' 段開始' if start else ''}{RESET}")
    if model != T.DEFAULT_MODEL and not file_only:
        print(f"  {C_DIM}{model_label}：{T.MODELS[model]['note']}。朗讀時每段之間可能要等合成{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}", flush=True)
    mode = "tts_file" if file_only else "tts"
    _webui_send({"type": "started", "mode": mode})
    _webui_send({"type": "tts_info", "voice": voice["name"], "gender": voice.get("gender") or "",
                 "provider": prov.label, "model": model, "model_label": model_label,
                 "rate": rate, "segments": n, "start": start, "chars": len(text),
                 "output": out_label, "save": fmt or "", "browser": browser})
    sess = T.Session(prov, voice, s["custom"], segments[start:], rate, args.tts_steps,
                     ahead=None if file_only else T.engine.PREFETCH, first=start).start()
    k = n - start                       # 這次要念的段數；Session、瀏覽器播放用 0～k-1，字幕與紀錄用整份文字的段號
    global _webui_pause_event
    pause_ev = threading.Event()
    _webui_pause_event = pause_ev
    ts_name = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(RECORDING_DIR, exist_ok=True)
    save_final = os.path.join(RECORDING_DIR, f"朗讀_{ts_name}.{fmt}") if fmt else None
    save_wav = (os.path.join(RECORDING_DIR, f".朗讀_{ts_name}.part.wav") if fmt == "mp3" else save_final) if fmt else None
    writer, wsr = None, None
    stream, out_sr = None, None
    job = live_dir = None
    if browser:
        import uuid
        job = uuid.uuid4().hex[:16]
        live_dir = os.path.join(T.engine.TMP_DIR, f"live_{job}")
        T.sweep_tmp()
        os.makedirs(live_dir, exist_ok=True)
        try:
            os.remove(_TTS_POS_FLAG)
        except OSError:
            pass
    t0 = time.monotonic()
    done = 0
    audio_secs = 0.0
    err = None
    stopped = False
    first_wait_hint = True
    try:
        t_free = time.monotonic()       # 播放端「可以播下一段」的時間：等＝這一段合成好的時間減掉它（第一段就是按下開始後多久出聲）
        for j, seg in enumerate(segments[start:]):
            i = start + j
            if not file_only and not browser:
                sess.set_position(j)
                t_free = time.monotonic()   # 上一段剛播完
            r = None
            waited = 0
            while r is None:
                try:
                    r = sess.get(j, timeout=1.0)
                    if r is None:
                        raise KeyboardInterrupt
                except TimeoutError:
                    waited += 1
                    if waited == 3 and first_wait_hint:
                        msg = "合成中（第一次要先啟動合成服務，約 30 秒）" if j == 0 else f"合成第 {i + 1} 段中"
                        print(f"  {C_DIM}{msg}...{RESET}", flush=True)
                        _webui_send({"type": "progress", "stage": "合成中", "detail": msg})
            first_wait_hint = j == 0 and waited < 3
            synth_secs = r.get("seconds", 0.0)
            wait_secs = max(0.0, r.get("ready", t_free) - t_free)
            pcm, sr = r["pcm"], r["sr"]
            if fmt:
                if writer is None:
                    writer = wave.open(save_wav, "wb")
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(sr)
                    wsr = sr
                wpcm = pcm if sr == wsr else T.to_pcm(T.pcm_wav(pcm, sr), 1.0, wsr)[0]
                writer.writeframes(wpcm)
                if i < n - 1:
                    writer.writeframes(b"\x00\x00" * int(wsr * gap))
            secs = len(pcm) / 2 / sr
            audio_secs += secs + (gap if i < n - 1 else 0)
            timestamp = time.strftime("%H:%M:%S")
            if browser:
                path = os.path.join(live_dir, f"{j}.wav")
                with open(path + ".tmp", "wb") as f:
                    f.write(T.pcm_wav(pcm, sr))
                os.replace(path + ".tmp", path)
                _webui_send({"type": "tts_audio", "job": job, "seq": j, "total": k, "duration": round(secs, 2),
                             "gap": gap})
                _tts_wait_browser(job, j, sess, pause_ev, stall=_TTS_BROWSER_STALL + 2 * secs)
                t_free = time.monotonic() + secs + gap     # 瀏覽器開始播這一段了：播完（加停頓）才需要下一段
            elif not file_only:
                if stream is None:
                    stream, out_sr = _tts_open_stream(dev, sr)
                    if out_sr != sr:
                        sess.out_sr = out_sr          # 之後的段落合成時就重新取樣
                if sr != out_sr:
                    pcm = T.to_pcm(T.pcm_wav(pcm, sr), 1.0, out_sr)[0]
            label = "轉檔" if file_only else "朗讀"
            timing = f"合 {synth_secs:.1f}s" + ("" if file_only or wait_secs < 0.1 else f" 等 {wait_secs:.1f}s")
            print(f"{C_DIM}[{timestamp}] {i + 1}/{n}  {timing}{RESET}  {C_WHITE}{seg}{RESET}", flush=True)
            ev = {"type": "transcription", "source": "main", "src_lang": label, "src_text": seg,
                  "timestamp": timestamp, "tts_seq": i, "tts_total": n,
                  "tts_synth": round(synth_secs, 1), "tts_secs": round(secs, 1)}
            if not file_only:
                ev["tts_wait"] = round(wait_secs, 1)
            _webui_send(ev)
            if file_only:
                _webui_send({"type": "progress", "stage": "合成中", "detail": f"{i + 1}/{n} 段"})
            elif not browser:
                _tts_play(stream, pcm, out_sr, pause_ev)
                if i < n - 1:
                    _tts_play(stream, b"\x00\x00" * int(out_sr * gap), out_sr, pause_ev)
            done = j + 1
        if browser:
            _tts_wait_browser(job, k, sess, pause_ev, stall=_TTS_BROWSER_STALL + 2 * audio_secs / max(k, 1))
    except T.TTSError as e:
        err = e.message
    except TimeoutError:
        err = "瀏覽器沒有在播放（分頁關掉了、或從別的分頁停止了），停止朗讀"
    except KeyboardInterrupt:
        stopped = True
    except Exception as e:                       # noqa: BLE001  播放裝置拔掉等：說清楚，已存的檔案照樣收好
        err = f"{type(e).__name__}: {e}"
    finally:
        sess.stop()
        if stream is not None:
            try:
                (stream.stop if done == k and not stopped else stream.abort)()
                stream.close()
            except Exception:
                pass
        saved = None
        if writer is not None:
            try:
                writer.close()
                if fmt == "mp3":
                    _webui_send({"type": "progress", "stage": "存檔中", "finishing": True, "detail": "轉成 MP3"})
                    ff = T.engine._ffmpeg()
                    rr = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", save_wav,
                                         "-codec:a", "libmp3lame", "-q:a", "3", save_final],
                                        capture_output=True, timeout=1800, **_SUBPROCESS_FLAGS) if ff else None
                    if rr is not None and rr.returncode == 0 and os.path.exists(save_final):
                        os.remove(save_wav)
                        saved = save_final
                    else:
                        saved = save_final[:-4] + ".wav"
                        os.replace(save_wav, saved)
                        print(f"  {C_HIGHLIGHT}[注意] 轉 MP3 失敗，存成 WAV{RESET}", flush=True)
                else:
                    saved = save_final
            except Exception as e:                # noqa: BLE001
                print(f"  {C_HIGHLIGHT}[注意] 音訊檔沒有存好：{type(e).__name__}: {e}{RESET}", flush=True)
        if live_dir:
            shutil.rmtree(live_dir, ignore_errors=True)
        el = int(time.monotonic() - t0)
        print()
        if err:
            print(f"[錯誤] {err}", file=sys.stderr, flush=True)
            _webui_send({"type": "progress", "stage": "錯誤", "detail": err})
        head = "已停止" if stopped else ("沒有完成" if err else "完成")
        print(f"{C_OK if head == '完成' else C_HIGHLIGHT}{BOLD}{'文字轉語音檔' if file_only else '朗讀'}{head}{RESET}"
              f"  {C_WHITE}{done}/{k} 段、聲音 {int(audio_secs // 60):02d}:{int(audio_secs % 60):02d}、"
              f"花了 {el // 60:02d}:{el % 60:02d}{RESET}")
        if saved:
            print(f"  {C_WHITE}檔案：{saved}{RESET}")
            _webui_send_realtime_results(None, [saved])
        print(flush=True)
        _webui_flush()
    if err:
        sys.exit(1)


def _tts_text_files():
    """互動選單用：recordings/、logs/ 裡的文字檔（新的在前）"""
    out = []
    for d in (RECORDING_DIR, LOG_DIR):
        if os.path.isdir(d):
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if os.path.isfile(p) and os.path.splitext(f)[1].lower() in (".txt", ".md", ".srt", ".vtt"):
                    out.append(p)
    return sorted(out, key=os.path.getmtime, reverse=True)


def _ask_tts(args, file_only):
    """互動選單：文字內容朗讀／文字轉語音檔要用的文字檔、聲音、語速、播放裝置"""
    try:
        import jtlw_tts as T
    except Exception as e:
        _tts_fail(f"文字轉語音元件載入失敗（{type(e).__name__}: {e}）：請再執行一次升級")
    files = _tts_text_files()[:20]
    print(f"\n\n{C_TITLE}{BOLD}▎ 要念的文字{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    for k, p in enumerate(files, 1):
        print(f"  {C_DIM}[{k}]{RESET} {C_WHITE}{os.path.relpath(p, SCRIPT_DIR)}{RESET}")
    print(f"  {C_DIM}或直接輸入文字檔的路徑（.txt／.md／.srt／.vtt）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    while True:
        print(f"{C_WHITE}選擇{' (1-' + str(len(files)) + ')' if files else ''} 或輸入路徑：{RESET}", end=" ")
        try:
            ans = input().strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if ans.isdigit() and 1 <= int(ans) <= len(files):
            args.tts_file = files[int(ans) - 1]
            break
        if ans and os.path.isfile(os.path.expanduser(ans)):
            args.tts_file = os.path.expanduser(ans)
            break
        print(f"  {C_HIGHLIGHT}找不到這個檔案{RESET}")
    print(f"  {C_OK}→ {os.path.basename(args.tts_file)}{RESET}")
    vs = T.list_voices()
    if len(vs) > 1:
        dv = T.settings(load_config())["voice"]
        print(f"\n{C_TITLE}{BOLD}▎ 聲音{RESET}")
        for k, v in enumerate(vs, 1):
            g = T.GENDERS.get(v.get("gender") or "", "")
            print(f"  {C_DIM}[{k}]{RESET} {C_WHITE}{v['name']}{'（' + g + '）' if g else ''}{RESET}"
                  + (f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if v["id"] == dv else ""))
        print(f"{C_WHITE}選擇 (1-{len(vs)}) [預設]：{RESET}", end=" ")
        try:
            ans = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if ans.isdigit() and 1 <= int(ans) <= len(vs):
            args.tts_voice = vs[int(ans) - 1]["id"]
    rates = [(0.8, "慢"), (1.0, "正常"), (1.2, "稍快"), (1.5, "快")]
    print(f"\n{C_TITLE}{BOLD}▎ 語速{RESET}")
    for k, (r, lab) in enumerate(rates, 1):
        print(f"  {C_DIM}[{k}]{RESET} {C_WHITE}{lab}（{r:g} 倍）{RESET}" + (f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if r == 1.0 else ""))
    print(f"{C_WHITE}選擇 (1-4) [2]：{RESET}", end=" ")
    try:
        ans = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    args.tts_rate = rates[int(ans) - 1][0] if ans in ("1", "2", "3", "4") else 1.0
    if file_only:
        args.tts_device, args.tts_save = "none", "mp3"
        return
    devs = _tts_output_devices()
    if len(devs) > 1:
        print(f"\n{C_TITLE}{BOLD}▎ 播放裝置{RESET}")
        for k, d in enumerate(devs, 1):
            print(f"  {C_DIM}[{k}]{RESET} {C_WHITE}{d['name']}{RESET}" + (f"  {C_HIGHLIGHT}{REVERSE} 系統預設 {RESET}" if d["default"] else ""))
        print(f"{C_WHITE}選擇 (1-{len(devs)}) [系統預設]：{RESET}", end=" ")
        try:
            ans = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if ans.isdigit() and 1 <= int(ans) <= len(devs):
            args.tts_device = str(devs[int(ans) - 1]["id"])
    print(f"{C_WHITE}同時存成 MP3？(y/N)：{RESET}", end=" ")
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if ans == "y":
        args.tts_save = "mp3"


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
    回傳 ("realtime", None)、("file", [filepath, ...])、("tts", None)、("tts_file", None)"""
    while True:
        print(f"\n\n{C_TITLE}{BOLD}▎ 輸入來源{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"  {C_HIGHLIGHT}{BOLD}[1] 即時音訊擷取{RESET}  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}")
        print(f"  {C_DIM}[2]{RESET} {C_WHITE}讀入音訊檔案{RESET}")
        print(f"  {C_DIM}[3]{RESET} {C_WHITE}文字內容朗讀{RESET}  {C_DIM}台灣華語念出來（GPU 伺服器或 Apple Silicon Mac）{RESET}")
        print(f"  {C_DIM}[4]{RESET} {C_WHITE}文字轉語音檔{RESET}  {C_DIM}不播放，存成 MP3{RESET}")
        print(f"{C_DIM}{'─' * 60}{RESET}")
        print(f"{C_WHITE}選擇 (1-4) [1]：{RESET}", end=" ")

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
        if user_input in ("3", "4"):
            print(f"  {C_OK}→ {'文字內容朗讀' if user_input == '3' else '文字轉語音檔'}{RESET}")
            return ("tts" if user_input == "3" else "tts_file", None)

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

    # Linux: PipeWire / PulseAudio monitor + 麥克風
    if IS_LINUX and _pulse_available():
        loopback_id = PULSE_LOOPBACK_ID
        loopback_name = _pulse_label()
        mic_id = _find_default_mic()
        if mic_id is not None:
            aggregate_id = PULSE_MIXED_ID
            aggregate_name = f"{loopback_name} + {sd.query_devices(mic_id)['name']}"

    if IS_MACOS:
        # 0) ScreenCaptureKit（零設定，不需聚集裝置）
        if _sck_available():
            loopback_id = SCK_LOOPBACK_ID
            loopback_name = "ScreenCaptureKit 系統音訊"
            mic_id = _find_mac_mic()
            if mic_id is not None:
                aggregate_id = SCK_MIXED_ID
                aggregate_name = f"ScreenCaptureKit + {sd.query_devices(mic_id)['name']}"
        # 1) 聚集裝置（macOS 專有）
        for i, dev in enumerate(devices):
            if aggregate_id is not None:
                break
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
        elif IS_LINUX:
            # 沒有圖形桌面（SSH / 伺服器）時不開啟；xdg-open 會把整個桌面程式
            # 掛在本程序底下，必須脫離 session 並切斷 stdio，否則外層的 pipe 會卡住
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return
            import shutil
            if not shutil.which("xdg-open"):
                return
            subprocess.Popen(["xdg-open", file_path],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
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
        # 攔截 SIGWINCH（只有主執行緒能註冊訊號；API 的工作執行緒會跳過）
        self._old_sigwinch = None
        if hasattr(signal, 'SIGWINCH') and threading.current_thread() is threading.main_thread():
            try:
                self._old_sigwinch = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, self._on_sigwinch)
            except ValueError:
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
                if threading.current_thread() is threading.main_thread():
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
                    server_type="ollama", think=False, on_line=None):
    """直接呼叫 LLM API 取得回應（串流模式，可更新 spinner 進度或即時輸出）

    think 預設 False：摘要與逐字稿校正都不需要思考過程，開著會慢上百倍。"""
    return _llm_generate(
        prompt, model, host, port, server_type,
        stream=True, timeout=timeout,
        spinner=spinner, live_output=live_output, think=think,
        on_line=on_line,
    )


def _correct_segments_with_llm(segments_data, model, host, port, server_type="ollama",
                                topic=None, on_progress=None, glossary=None):
    """用 LLM 校正離線逐字稿的 ASR 辨識錯誤，原地修改 segments_data。
    glossary：呼叫端給的專有名詞（已用 _glossary_parts 拆開）。提示詞列出正確拼法，
    把關放行「誤聽換成拼法相近的專有名詞」、不准把專有名詞換掉（v2.26.8，REST API 用）"""
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

    # 3. 分批（按字數切割，每批最多 _CORRECT_MAX_LINES 行）
    #    一批行數太多時，模型回傳的行號容易錯位，錯位後的修改全部對不上原文而被退回
    #    （實測 388 行一次送，gemma4 有 144 行、gpt-oss 有 121 行因此白做）
    chunks = []       # [[(global_idx, text), ...], ...]
    current_chunk = []
    current_chars = 0
    for idx, (si, li, text) in enumerate(all_lines):
        line_len = len(text) + 10  # 序號 + 分隔符
        if current_chunk and (current_chars + line_len > max_chars
                              or len(current_chunk) >= _CORRECT_MAX_LINES):
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append((idx, text))
        current_chars += line_len
    if current_chunk:
        chunks.append(current_chunk)

    # 4. 依逐字稿語言選提示詞，並準備 topic 行
    glossary = list(glossary or [])[:_GLOSSARY_PROMPT_MAX]
    if _transcript_is_chinese([text for _, _, text in all_lines]):
        prompt_template = TRANSCRIPT_CORRECT_PROMPT_TEMPLATE
        topic_line = f"- 本次會議主題：{topic}，請根據此主題的領域知識理解專業術語並正確校正\n" if topic else ""
        if glossary:
            topic_line += (f"- 本次會議的專有名詞（正確寫法）：{'、'.join(glossary)}。逐字稿裡發音或拼法相近的誤聽請改成這裡的寫法；"
                           "不是這些詞的不要硬改成它們，也不要把中文翻成這些詞\n")
    else:
        prompt_template = TRANSCRIPT_CORRECT_PROMPT_TEMPLATE_EN
        topic_line = (f"- Meeting topic: {topic}. Use domain knowledge of this topic to fix technical terms\n"
                      if topic else "")
        if glossary:
            topic_line += (f"- Proper nouns in this meeting (correct spelling): {', '.join(glossary)}. "
                           "If a word in the transcript is a mishearing or misspelling of one of these, use the spelling given here. "
                           "Do not force other words into these terms, and do not translate\n")
    glossary_words = _glossary_words(glossary)
    glossary_tokens = set()                 # 跨行搬移檢查不算專有名詞：兩行都把誤聽改成同一個詞是正常的
    for g in glossary:
        glossary_tokens |= _content_tokens(g)

    # 5. 設定狀態列
    _llm_loc = "本機" if host in ("localhost", "127.0.0.1", "::1") else "伺服器"
    sbar = _SummaryStatusBar(model=model, task="LLM 校正逐字稿", location=_llm_loc).start()

    corrected = {}  # global_idx → corrected_text
    n_rejected = 0  # 未通過把關、退回原文的行數
    protected = _protected_terms([text for _, _, text in all_lines])
    total_chunks = len(chunks)

    def _run_chunk(ci, chunk):
        """送出一批校正（在執行緒中執行），回傳 LLM 原始輸出；失敗時回傳 None"""
        numbered_lines = "\n".join(f"{i+1}|{text}" for i, (_, text) in enumerate(chunk))
        prompt = prompt_template.format(topic_line=topic_line, lines=numbered_lines)
        # timeout 依 chunk 字數動態調整（每千字 60 秒，最低 300 秒）
        _timeout = max(300, len(numbered_lines) // 1000 * 60 + 300)

        # 即時推送每行校正結果到 WebUI
        def _on_correct_line(line_text, _chunk=chunk):
            line_text = line_text.strip()
            m = re.match(r'^(\d+)\|(.+)$', line_text)
            if not m:
                return
            local_idx = int(m.group(1)) - 1
            corrected_text = m.group(2).strip()
            if 0 <= local_idx < len(_chunk):
                global_idx = _chunk[local_idx][0]
                orig_si, orig_li, orig_text = all_lines[global_idx]
                # 只在有變化、且通過把關時推送（簡繁轉換只套用在中文行）
                if not _KANA_RE.search(orig_text):
                    corrected_text = _s2twp_safe(corrected_text)
                corrected_text = _normalize_correction(corrected_text)
                if (corrected_text != orig_text and corrected_text != "[雜音]"
                        and _accept_correction(orig_text, corrected_text, protected, glossary_words)):
                    _corrected_tc = corrected_text
                    _webui_send({"type": "correction",
                                 "original": orig_text,
                                 "corrected": _corrected_tc})

        try:
            return call_ollama_raw(prompt, model, host, port, timeout=_timeout,
                                   spinner=sbar, server_type=server_type,
                                   think=False, on_line=_on_correct_line)
        except Exception as e:
            print(f"  {C_HIGHLIGHT}[警告] 第 {ci+1}/{total_chunks} 批校正失敗: {e}{RESET}",
                  file=sys.stderr)
            return None

    try:
        sbar.set_task(f"LLM 校正逐字稿（{total_chunks} 批）" if total_chunks > 1 else "LLM 校正逐字稿")
        # 同時送出 _CORRECT_PARALLEL 批（Ollama 預設可並行處理多個請求）；結果回到主執行緒再依序解析
        # on_progress(完成批數, 總批數)：給 v3 API 回報校正進度（v2.21.9），回呼出錯不影響校正
        def _report(done):
            if on_progress:
                try:
                    on_progress(done, total_chunks)
                except Exception:
                    pass
        _report(0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=_CORRECT_PARALLEL) as pool:
            futures = {pool.submit(_run_chunk, ci, chunk): (ci, chunk) for ci, chunk in enumerate(chunks)}
            n_done = 0
            for fut in concurrent.futures.as_completed(futures):
                ci, chunk = futures[fut]
                result = fut.result()
                n_done += 1
                _report(n_done)
                if total_chunks > 1:
                    sbar.set_task(f"LLM 校正逐字稿（{n_done}/{total_chunks} 批完成）")
                if not result:
                    continue

                # 移除 <think>...</think> 標籤（Qwen3 等模型可能忽略 think=False）
                result = re.sub(r'<think>[\s\S]*?</think>', '', result).strip()
                result = re.sub(r'<think>[\s\S]*', '', result).strip()

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
                        orig_text = all_lines[global_idx][2]
                        # 簡繁轉換只套用在中文行（日文的漢字不可轉；英文行轉了也沒作用）
                        if not _KANA_RE.search(orig_text):
                            corrected_text = _s2twp_safe(corrected_text)
                        corrected_text = _normalize_correction(corrected_text)
                        if _accept_correction(orig_text, corrected_text, protected, glossary_words):
                            corrected[global_idx] = corrected_text
                        else:
                            n_rejected += 1
    finally:
        sbar.freeze()
        sbar.stop()

    # 6b. 從鄰行搬字：新增的實詞出現在鄰行原文、自己原文卻沒有 → 退回原文（鄰行沒被改也適用）
    for idx in sorted(corrected):
        new_text = corrected[idx]
        if new_text == "[雜音]":
            continue
        own = all_lines[idx][2]
        gained = _content_tokens(new_text) - _content_tokens(own) - _COMMON_WORDS - glossary_tokens
        neighbors = set()
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(all_lines):
                neighbors |= _content_tokens(all_lines[j][2])
        if gained & neighbors:
            del corrected[idx]
            n_rejected += 1

    # 6c. 跨行搬移：相鄰兩行都有修改、且內容互相流動時，兩行都退回原文
    for idx in range(len(all_lines) - 1):
        a, b = corrected.get(idx), corrected.get(idx + 1)
        if a is None or b is None or "[雜音]" in (a, b):
            continue
        if _moved_between(all_lines[idx][2], a, all_lines[idx + 1][2], b):
            for k in (idx, idx + 1):
                if corrected.pop(k, None) is not None:
                    n_rejected += 1

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
    #    保護翻譯配對：如果該段有多行且只有此行被標為雜音，保留（避免只剩譯文沒原文）
    _SRC_LABELS = {"EN", "英", "日"}
    n_noise = 0
    if noise_markers:
        for si in range(len(segments_data) - 1, -1, -1):
            seg = segments_data[si]
            for li in range(len(seg["lines"]) - 1, -1, -1):
                if (si, li) in noise_markers:
                    # 翻譯配對保護：若此行是原文且同段還有譯文，跳過不刪
                    if len(seg["lines"]) >= 2 and seg["lines"][li]["label"] in _SRC_LABELS:
                        has_dst = any(ln["label"] not in _SRC_LABELS for j, ln in enumerate(seg["lines"]) if j != li)
                        if has_dst:
                            continue  # 保留原文行
                    seg["lines"].pop(li)
                    n_noise += 1
            # 如果整段都被刪光，移除整段
            if not seg["lines"]:
                segments_data.pop(si)

    noise_str = f"，移除 {n_noise} 行雜音" if n_noise else ""
    reject_str = f"，{n_rejected} 行修改幅度異常已保留原文" if n_rejected else ""
    print(f"  {C_OK}LLM 校正完成{RESET}{C_DIM}（共 {len(all_lines)} 行，修正 {n_corrected} 行{noise_str}{reject_str}）{RESET}")


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


def _transcript_section_len(summary_text):
    """取出摘要結果中「校正逐字稿」那一段的字數（沒有這一段時回傳 None）"""
    m = re.search(r'^##\s*校正逐字稿\s*$', summary_text, re.M)
    if not m:
        return None
    rest = summary_text[m.end():]
    nxt = re.search(r'^##\s', rest, re.M)
    return len(rest[:nxt.start()] if nxt else rest)


def _warn_if_transcript_truncated(source_text, summary_text, label=""):
    """校正逐字稿明顯短於輸入時提醒：多半是模型輸出被 context 上限截斷（不會有錯誤訊息）"""
    got = _transcript_section_len(summary_text)
    if got is None or len(source_text) < 2000:
        return False
    ratio = got / len(source_text)
    if ratio >= 0.5:
        return False
    tag = f"（{label}）" if label else ""
    print(f"\n  {C_HIGHLIGHT}[警告] 校正逐字稿疑似被截斷{tag}："
          f"輸入 {len(source_text):,} 字，只產出 {got:,} 字{RESET}", file=sys.stderr)
    print(f"  {C_DIM}模型實際可用的 context 可能小於它宣告的值；"
          f"請改用較小的模型分段或降低 SUMMARY_CHUNK_CEILING_CHARS{RESET}", file=sys.stderr)
    return True


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
    return min(max(max_chars, SUMMARY_CHUNK_FALLBACK_CHARS), SUMMARY_CHUNK_CEILING_CHARS)


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


def _is_repetitive_hallucination(_t):
    """重複模式的幻覺（中文與韓文共用；v2.22.0 從 _is_zh_hallucination 抽出，判斷完全不變）"""
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
    # 重複模式偵測 3：任意位置 2-8 字元片段連續重複 4 次以上（如「有多少多少多少多少...」「prova prova prova...」）
    if len(_t) >= 8 and re.search(r'(.{2,8})\1{3,}', _t):
        return True
    # 重複模式偵測 4：同一個單詞（含空格）重複出現 5 次以上
    _words = _t.split()
    if len(_words) >= 5:
        from collections import Counter as _WC
        _wc = _WC(_words)
        _top_word, _top_count = _wc.most_common(1)[0]
        if _top_count >= 5 and _top_count / len(_words) > 0.5:
            return True
    return False


def _is_zh_hallucination(text):
    """檢查中文文字是否為 Whisper 幻覺（YouTube 訓練資料殘留 + 重複模式）"""
    _t = text.strip()
    if _is_repetitive_hallucination(_t):
        return True
    # 太短的中文（去除標點後不到 2 個字）
    _stripped = re.sub(r'[^\u4e00-\u9fff\u3040-\u30ff]', '', _t)
    if _stripped.startswith("字幕") and len(_stripped) <= 6:
        return True
    if len(_stripped) < 2:
        return True
    # 簡體+繁體關鍵字都要檢查（faster-whisper 可能輸出簡體）
    if any(kw in text for kw in (
        # YouTube 用語
        "訂閱", "订阅", "歡迎訂閱", "欢迎订阅",
        "點贊", "点赞", "點讚", "按讚", "轉發", "转发", "打賞", "打赏",
        "感謝觀看", "感谢观看", "謝謝大家", "谢谢大家", "謝謝收看", "谢谢收看",
        "感謝收聽", "感谢收听", "感謝聆聽", "感谢聆听",
        "喜歡的話", "喜欢的话", "別忘了", "别忘了",
        # 字幕/翻譯歸屬（Amara.org 訓練資料殘留）
        "字幕由", "字幕提供", "字幕by", "字幕BY",
        "中文字幕", "繁體中文", "简体中文", "擁體中文",
        "字幕志願", "字幕志愿", "字幕組", "字幕组",
        "字幕視聽", "字幕视听", "字幕製作", "字幕制作",
        "翻譯志願", "翻译志愿", "校對志願", "校对志愿",
        "Amara", "amara", "Saya", "saya", "prova", "Prova",
        # 版權歸屬幻覺（僅短句時過濾，長句可能是真實討論）
        "版權所有", "版权所有",
        "初音ミク", "初音",
        # 頻道/節目
        "獨播", "独播", "劇場", "剧场", "YoYo", "Television Series",
        "明鏡", "明镜", "新聞頻道", "新闻频道",
        "直播間", "直播间", "觀眾朋友", "观众朋友",
    )):
        return True
    # 短句限定：音樂/版權歸屬幻覺（長句中出現這些詞可能是真實討論，不過濾）
    if len(_stripped) <= 20:
        return any(kw in text for kw in (
            "詞曲", "词曲", "作詞", "作词", "作曲", "編曲", "编曲",
            "詞：", "词：", "曲：", "演唱", "原唱",
            "版權", "版权", "著作權", "著作权",
            "李宗盛", "周杰倫", "周杰伦", "林俊傑", "林俊杰", "蔡依林",
            "張惠妹", "张惠妹", "五月天", "陳奕迅", "陈奕迅",
            "鄧紫棋", "邓紫棋", "王力宏",
        ))
    return False


def _is_ko_hallucination(text):
    """檢查韓文文字是否為 Whisper 幻覺（v2.22.0）。

    清單是**實際蒐集**來的，不是憑印象列：把靜音、雜訊、和弦、旋律、掌聲、鍵盤聲
    以韓文模式、關掉 VAD 送進 base／small（本機）與 large-v3-turbo／large-v3（GPU），
    整理重複出現的無關輸出（工具與結果見 tools/korean/）。沒觀察到的就不加
    （例如 KBS／SBS 新聞結尾語都沒出現過，只收 MBC）。

    擋不住的：隨機的正常詞句（small 對鍵盤聲吐「닭고기」「오늘의 주인공은」），
    跟真的講話分不開，硬擋會誤殺。
    """
    _t = text.strip()
    if _is_repetitive_hallucination(_t):          # 아, 아, 아…／이곳은 이곳은…／1,2,3,4,4,4…
        return True
    _hangul = sum(1 for c in _t if '\uac00' <= c <= '\ud7a3' or '\u3131' <= c <= '\u318e')
    if _hangul < 2:
        return True
    # 整句就是這個詞才擋：會議裡真的會有人說「謝謝」，不能出現在句中就擋
    _bare = re.sub(r"[\s.,!?。！？…~]", "", _t)
    if _bare in ("감사합니다", "고맙습니다", "아멘"):
        return True
    if any(kw in _t for kw in (
        # YouTube 結尾語（GPU 上最常見：「下支影片見」「感謝收看」）
        "다음 영상에서", "시청해주셔서", "시청해 주셔서",
        "구독과 좋아요", "좋아요와 구독", "구독 부탁",
        # 新聞台結尾語
        "MBC 뉴스",
    )):
        return True
    # 字幕歸屬（與中文「字幕提供」、日文「字幕制作」同一類）：「한글자막 by …」「자막 제공 …」
    if "자막" in _t and re.search(r"자막\s*(by|BY|제공|제작|협찬)", _t):
        return True
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


# ── 講者辨識：NVIDIA Nemotron 3 Diarization ─────────────────────────
# 2026-09-24~25 實測（tools/diar_bench/）：同一批真實 ASR 段落，段落講者搞錯
#   中文 AISHELL-4 20 場 18.52% → 3.07%、英文 AMI 16 場 12.31% → 4.65%，人數判對 2/20 → 17/20。
# CUDA／MPS／CPU 三種跑法結果逐幀一致（Linux GPU、Mac M5、Windows CPU 都驗過）。
# 需要 transformers 內建的 nemotron3_diarization（5.18 起）；沒有就沿用 resemblyzer，行為與先前完全相同。
# **這一段在 remote_whisper_server.py 有一份同樣的**（伺服器自動更新只推單一檔案，不能共用模組），
# tools/test_diarizer.py 會逐一比對兩邊的輸出。
NEMO_DIAR_MODEL = "nvidia/Nemotron-3-Diarization"
_NEMO_FRAME = 0.01              # 模型每格 10 毫秒
_NEMO_CHANNELS = 8              # 最多 8 位講者
_DIARIZE_ENGINES = ("auto", "nemotron", "legacy")
_diarize_engine = "auto"        # --diarize-engine
_NEMO_CACHE = {}


def _nemo_platform_ok():
    """Intel Mac 不支援（使用者 2026-09-25 決定；PyTorch 2.3 起沒有 x86_64 macOS 版本）"""
    return not (IS_MACOS and not _is_apple_silicon())


def _nemo_available():
    """回傳 (能不能用, 不能用的原因)。只看平台與套件，不載入模型"""
    if not _nemo_platform_ok():
        return False, "Intel Mac 不支援 Nemotron"
    return _nemo_transformers_ok()


@lru_cache(maxsize=1)
def _nemo_transformers_ok():
    ok, why = _transformers_supports("nemotron3_diarization")
    return ok, (why or ("" if ok else "transformers 版本太舊（Nemotron 需要 5.18 以上）"))


def _recommended_diarizer(num_speakers=None, engine="auto"):
    """決定講者辨識用哪個方法，回傳 (engine, 原因)，engine 為 "nemotron" 或 "legacy"。
    **所有平台與條件判斷都在這裡**（比照 _recommended_mic_engine），不要散落到呼叫端"""
    if engine == "legacy":
        return "legacy", "指定使用現行方法"
    if num_speakers and num_speakers > _NEMO_CHANNELS:
        return "legacy", f"指定 {num_speakers} 人，超過 Nemotron 上限 {_NEMO_CHANNELS} 人"
    ok, why = _nemo_available()
    if not ok:
        return "legacy", why
    return "nemotron", ""


# v2.26.1（api_revision 2.6，JTDT 要求）：auto 退回現行方法時給機器看的代碼，呼叫端翻成自己的語言；note 照舊給人看。
# **與 remote_whisper_server.py 的同名函式相同**（tools/test_diarizer.py 比對）。舊版伺服器只回 note，用戶端也靠這支推回代碼
_DIAR_REASONS = ("too_many_speakers", "speakers_saturated", "nemotron_unavailable", "nemotron_failed")


def _diar_reason(why):
    """退回現行方法的原因（_recommended_diarizer／_nemotron_diarize 的說明文字）→ 代碼；沒有原因或不是 Nemotron 的問題回 None"""
    why = why or ""
    if not why or "resemblyzer" in why or why == "指定使用現行方法":
        return None
    if why.startswith("指定 ") and "超過 Nemotron 上限" in why:
        return "too_many_speakers"
    if "全部用滿" in why:
        return "speakers_saturated"
    if why.startswith("Nemotron 執行失敗"):
        return "nemotron_failed"
    return "nemotron_unavailable"


def _nemo_span(probs_len, seg):
    """段落對應的格數範圍 [a, b)，至少一格、不超出音檔"""
    a = int(seg["start"] / _NEMO_FRAME)
    b = max(a + 1, int(seg["end"] / _NEMO_FRAME))
    b = min(b, probs_len)
    a = min(a, b - 1)
    return max(a, 0), max(b, 1)


def _nemo_segment_labels(probs, segments):
    """每段的講者＝段落時間內 8 個通道活動機率加總最大的那個（實測時用的就是這個規則）"""
    import numpy as np
    out = []
    for s in segments:
        a, b = _nemo_span(len(probs), s)
        out.append(int(np.asarray(probs[a:b], dtype="float32").sum(axis=0).argmax()))
    return out


def _nemo_saturated(segments, labels):
    """8 個通道都有實質發言（≥1.6 秒的段落）→ 可能超過上限。
    E5 實測：原本 36 場（≤7 人）0 場觸發；合成的 11 人、15 人都觸發"""
    used = {l for s, l in zip(segments, labels) if s["end"] - s["start"] >= _DIAR_MIN_CLUSTER_SEC}
    return len(used) >= _NEMO_CHANNELS


def _nemo_limit_speakers(probs, segments, labels, k):
    """使用者指定 k 人（≤8）：**當上限**，不硬拆成 k 群（實測指定正確人數反而更差）。
    偵測到的人比 k 多時，保留發言秒數最多的 k 個通道，其餘段落改判給保留通道裡機率加總最大的"""
    import numpy as np
    sec = {}
    for s, l in zip(segments, labels):
        sec[l] = sec.get(l, 0.0) + (s["end"] - s["start"])
    if len(sec) <= k:
        return list(labels)
    keep = sorted(sec, key=lambda l: (-sec[l], l))[:k]
    out = []
    for s, l in zip(segments, labels):
        if l in keep:
            out.append(l)
            continue
        a, b = _nemo_span(len(probs), s)
        tot = np.asarray(probs[a:b], dtype="float32").sum(axis=0)
        out.append(max(keep, key=lambda c: tot[c]))
    return out


def _renumber_first_seen(labels):
    """依首次出現順序重新編號（與現行方法的輸出一致：第一個開口的是 0）"""
    m = {}
    return [m.setdefault(l, len(m)) for l in labels]


def _nemo_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if _is_apple_silicon() and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _nemo_probs(wav_path):
    """整檔跑一次 Nemotron，回傳 (格數, 8) 的講者活動機率"""
    import librosa
    import numpy as np
    import torch
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    if "model" not in _NEMO_CACHE:
        dev = _nemo_device()
        proc = _call_with_ssl_retry(AutoProcessor.from_pretrained, NEMO_DIAR_MODEL)
        model = _call_with_ssl_retry(AutoModelForAudioFrameClassification.from_pretrained, NEMO_DIAR_MODEL)
        _NEMO_CACHE.update(proc=proc, model=model.to(dev).eval(), dev=dev)
    proc, model, dev = _NEMO_CACHE["proc"], _NEMO_CACHE["model"], _NEMO_CACHE["dev"]
    wav, _ = librosa.load(wav_path, sr=16000, mono=True)
    inp = {k: (v.to(dev) if hasattr(v, "to") else v) for k, v in proc(wav, sampling_rate=16000).items()}
    with torch.inference_mode():
        lg = model(**inp).logits[0].float().cpu().numpy()
    return lg if (lg.min() >= 0 and lg.max() <= 1) else 1 / (1 + np.exp(-lg))


def _nemotron_diarize(wav_path, segments, num_speakers=None):
    """回傳 (labels, 原因)。labels 為 None 表示要退回現行方法，原因說明為什麼。
    8 位全部用滿時 labels 是 Nemotron 的結果、原因不是空的：呼叫端用現行方法再分一次，
    **分出超過 8 位才改用現行方法**（_saturated_prefers_legacy，v2.26.4）"""
    try:
        probs = _nemo_probs(wav_path)
    except Exception as e:
        return None, f"Nemotron 執行失敗（{type(e).__name__}: {e}）"
    labels = _nemo_segment_labels(probs, segments)
    if not num_speakers and _nemo_saturated(segments, labels):
        return _renumber_first_seen(labels), f"{_NEMO_CHANNELS} 位講者全部用滿，可能超過 Nemotron 上限"
    if num_speakers:
        labels = _nemo_limit_speakers(probs, segments, labels, num_speakers)
    return _renumber_first_seen(labels), ""


def _saturated_prefers_legacy(legacy_labels):
    """Nemotron 8 位全滿時，現行方法的結果要分出超過 8 位才採用（v2.26.4）。
    以前全滿就一律退回，但全滿不一定代表超過 8 人（最後幾位可能只講了幾句）；而混音錄音
    （麥克風＋系統音訊）時現行方法可能照音軌只分成 2 人。退回的理由是「可能超過 8 人」，
    現行方法沒分出更多人時這個理由就不成立"""
    return legacy_labels is not None and len(set(legacy_labels)) > _NEMO_CHANNELS


def _diar_engine_label(*infos):
    """輸出檔與畫面上的「實際用了哪個講者辨識方法」（info 由 _remote_diarize／_diarize_segments 填入；雙軌時兩路各一個）"""
    used = {i.get("engine") for i in infos if i and i.get("engine")}
    if used == {"nemotron"}:
        return "NVIDIA Nemotron 3 Diarization"
    if "nemotron" in used:
        return "NVIDIA Nemotron 3 Diarization／resemblyzer + spectralcluster（依音軌）"
    return "resemblyzer + spectralcluster"


def _diar_requested_label():
    """處理前的設定摘要：還不知道實際用哪個，顯示要求的方法（--diarize-engine）"""
    if _diarize_engine == "legacy":
        return "resemblyzer + spectralcluster（--diarize-engine legacy）"
    return "NVIDIA Nemotron（不能用時 resemblyzer + spectralcluster）"


def _diarize_segments(wav_path, segments, num_speakers=None, sbar=None, engine=None, info=None):
    """講者辨識入口：能用 Nemotron 就用，否則（或它退回時）用現行 resemblyzer。
    回傳 list of int（講者編號 0-based），失敗回傳 None。
    info（dict，選填）：填入實際用的方法 engine（nemotron／legacy）、退回原因 note／reason，
    與 saturated（Nemotron 8 位全滿，不論最後採用哪一種方法，v2.26.5）（v3 API 回報用）"""
    engine = engine or _diarize_engine
    choice, why = _recommended_diarizer(num_speakers, engine)
    if choice == "nemotron" and segments:
        if sbar:
            sbar.set_task("講者辨識（Nemotron）")
        labels, why = _nemotron_diarize(wav_path, segments, num_speakers)
        saturated = labels is not None and bool(why)
        if saturated:                           # 8 位全滿：現行方法再分一次，分出更多人才用它
            legacy = _diarize_segments_legacy(wav_path, segments, num_speakers=num_speakers, sbar=sbar)
            if _saturated_prefers_legacy(legacy):
                print(f"  {C_HIGHLIGHT}[講者辨識] 改用現行方法：{why}（現行方法分出 {len(set(legacy))} 位）{RESET}")
                if info is not None:
                    info.update(engine="legacy", note=why, reason=_diar_reason(why), saturated=True)
                return legacy
            n_leg = len(set(legacy)) if legacy is not None else 0
            print(f"  {C_DIM}[講者辨識] Nemotron 8 位全滿，現行方法只分出 {n_leg} 位，採用 Nemotron{RESET}")
            why = ""
        if labels is not None:
            print(f"  {C_DIM}[講者辨識] Nemotron（{_NEMO_CACHE.get('dev')}）{len(set(labels))} 位講者{RESET}")
            if info is not None:
                info.update(engine="nemotron", note=None, reason=None, saturated=saturated)
            return labels
    # 自動模式下「沒裝」不提示（那就是先前的行為）；指定了 Nemotron、或試過才退回的，要講清楚
    if why and (engine == "nemotron" or choice == "nemotron"
                or (num_speakers and num_speakers > _NEMO_CHANNELS)):
        print(f"  {C_HIGHLIGHT}[講者辨識] 改用現行方法：{why}{RESET}")
    if info is not None:
        # 指定現行方法時不必說明；自動模式退回的才把原因帶出去
        note = why if engine != "legacy" and why else None
        info.update(engine="legacy", note=note, reason=_diar_reason(note), saturated=False)
    return _diarize_segments_legacy(wav_path, segments, num_speakers=num_speakers, sbar=sbar)


def _diarize_segments_legacy(wav_path, segments, num_speakers=None, sbar=None):
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
        from spectralcluster import refinement, laplacian
        from spectralcluster import utils as sc_utils
    except ImportError as e:
        print(f"  {C_HIGHLIGHT}[錯誤] 講者辨識需要額外套件: {e}{RESET}", file=sys.stderr)
        print(f"  {C_DIM}pip install resemblyzer spectralcluster{RESET}", file=sys.stderr)
        return None

    if not segments:
        return None

    if sbar:
        sbar.set_task("載入聲紋模型")

    # 載入音訊
    # resemblyzer 的 preprocess_wav() 除了重取樣，還會做 VAD 靜音修剪並「刪掉」那些樣本
    # （實測 AMI 一場 18.5 分鐘的會議被刪掉 31.9%）。整檔修剪之後，樣本索引就不再對應
    # 原本的時間戳，越後面的段落偏移越大——AMI 那場到檔尾已經差了將近 6 分鐘，
    # 等於拿會議別處的聲音去比對，講者辨識必然大亂。
    # 正確做法是**逐段修剪**：先用原始時間軸切出段落，再對該段落做 preprocess。
    sr = 16000
    try:
        import librosa
        wav, _ = librosa.load(wav_path, sr=sr, mono=True)
        _per_segment_trim = True
    except Exception as e:
        # 舊做法整檔修剪靜音，時間軸會錯位（v2.20.0 修掉的問題：檔尾偏移近 6 分鐘），不可以不說（2026-10-05）
        print(f"  [講者辨識] 讀取音檔失敗（{type(e).__name__}: {e}），改用舊的讀法；講者與時間可能對不準", flush=True)
        wav = preprocess_wav(wav_path)      # 退而求其次，維持舊行為
        _per_segment_trim = False

    # 初始化聲紋編碼器（首次自動下載 ~17MB 模型）
    encoder = VoiceEncoder("cpu")

    if sbar:
        sbar.set_task(f"提取聲紋（{len(segments)} 段）")

    import numpy as np

    # ── 只有夠長的段落才進分群 ──
    # 1.6 秒是 resemblyzer 的 partial utterance 長度：短於它時 embed_utterance
    # 會把音訊補零到 1.6s 再算，那個聲紋不可靠。2026-09-22 用有標準答案的
    # 中文會議量到——標錯率 <1.0s 63.6%、1.0~1.6s 66.9%，而 1.6~2.5s 只有
    # 14.8%、2.5~4.0s 是 0.0%。短段落只佔 24% 的秒數卻貢獻 64% 的「講者搞錯」，
    # 而且它們一起進 affinity 矩陣，把長段落的分群也一起帶壞。
    # 原本的兩個補救（<0.5s 撐成 0.5s 視窗、連續 <0.8s 合併共用一個 embedding）
    # 方向是反的：合併等於強迫相鄰的短段落同一個講者，而搶話時它們多半不是。
    cluster_floor = _diar_cluster_floor(segments)

    # 逐段提取聲紋
    embeddings = []
    valid_indices = []  # 有成功提取 embedding 的段落索引

    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        if duration < cluster_floor:
            embeddings.append(None)
            continue

        audio_slice = wav[int(seg["start"] * sr):int(seg["end"] * sr)]
        if _per_segment_trim and len(audio_slice) >= int(0.3 * sr):
            # 切好之後才修剪這一段自己的靜音，不影響時間軸對應
            audio_slice = preprocess_wav(audio_slice, source_sr=sr)

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

    min_clusters = 2 if num_speakers is None else num_speakers
    max_clusters = 8 if num_speakers is None else num_speakers

    refinement_opts = refinement.RefinementOptions(
        # gaussian_blur_sigma=0：**不要模糊**。高斯模糊假設相鄰列是時間上連續的
        # 等寬視窗，但我們送進去的是「已合併的講者連續發言」，模糊會把講者
        # 交界處抹掉。18 場 AMI 實測：blur=1 → DER 43.60%、blur=0 → 16.25%
        gaussian_blur_sigma=0,
        p_percentile=0.98,
        thresholding_soft_multiplier=0.01,
        thresholding_type=refinement.ThresholdType.RowMax,
        symmetrize_type=refinement.SymmetrizeType.Max,
        # **沒有這個參數，上面五個全是死的**：refinement_sequence 預設 None 時
        # 整組步驟一步都不跑。2026-09-21 實測發現——把 p_percentile 從 0.90 掃到
        # 0.97、stop_eigenvalue 掃四個數量級，15 組結果一字不差，才看出來。
        refinement_sequence=[
            refinement.RefinementName.CropDiagonal,
            refinement.RefinementName.GaussianBlur,
            refinement.RefinementName.RowWiseThreshold,
            refinement.RefinementName.Symmetrize,
            refinement.RefinementName.Diffuse,
            refinement.RefinementName.RowWiseNormalize,
        ],
    )

    # 使用者沒指定人數時，用特徵值門檻自己估一個（見 _diar_estimate_speakers）。
    # 估不出來就把 min/max 交給函式庫自己的 eigengap，行為與先前相同。
    if num_speakers is None:
        _est = _diar_estimate_speakers(valid_embeddings, refinement_opts,
                                       laplacian.LaplacianType.GraphCut)
        if _est:
            min_clusters = max_clusters = _est

    try:
        clusterer = SpectralClusterer(
            min_clusters=min_clusters,
            max_clusters=max_clusters,
            refinement_options=refinement_opts,
            # GraphCut Laplacian：不指定時用 affinity 直接分解，特徵值間隙幾乎
            # 總是落在 k=2，未知人數時一律猜 2 人（4 人會議也判成 2 人）
            laplacian_type=laplacian.LaplacianType.GraphCut,
            # NormalizedDiff：預設的 Ratio 是「後一個特徵值 / 前一個」，
            # 分母是很靠近 0 的特徵值時比值會爆大，於是永遠挑最小的 k。
            # 會議越長段落越多、譜越平滑，這個偏誤越嚴重——中文 37 分鐘那場
            # 1043 段一律吐 k=2（實際 7 人），混淆率 40.20%。
            # 改成「相鄰差除以最大特徵值」之後同一場判 3 群、22.90%。
            # **兩個改動必須一起上**（見上面 cluster_floor）。真實 ASR 切段實測：
            # 只換 eigengap 幾乎沒有作用（短段落的雜訊還在譜裡）；
            # 只換門檻會讓英文 ES2011a 的混淆率由 12.44% 惡化到 23.29%
            # （單位變少之後 Ratio 更容易塌）。一起上才是 12.44% → 9.47%。
            eigengap_type=sc_utils.EigenGapType.NormalizedDiff,
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

    # 多數決平滑已移除（2026-09-21）。
    # 它強制每段採用前後窗口內的多數講者，是當年分群壞掉（未知人數時一律吐 2 群）
    # 時加的補丁。分群修好之後，它變成純粹的傷害，而且**窗口越大越差**：
    #   真實 ASR 段落、AMI 3 場平均 DER —— 不平滑 16.76%、窗口3 25.05%、
    #   窗口5（原設定）30.38%、窗口7 35.12%
    # 單調惡化代表問題出在這個啟發式本身，不是窗口大小沒調好。

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


def _vtt_timestamp(seconds):
    """秒數 → VTT 時間戳 HH:MM:SS.mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _segments_to_vtt(segments_data, vtt_path):
    """將 segments_data 轉為 WebVTT 字幕檔。翻譯模式自動雙語。"""
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, seg in enumerate(segments_data, 1):
            f.write(f"{i}\n")
            f.write(f"{_vtt_timestamp(seg['start'])} --> {_vtt_timestamp(seg['end'])}\n")
            for line in seg["lines"]:
                f.write(f"{line['text']}\n")
            f.write("\n")


def process_audio_file(input_path, mode, translator, model_size="large-v3-turbo",
                       diarize=False, num_speakers=None, remote_whisper_cfg=None,
                       correct_with_llm=False, llm_model=None, llm_host=None,
                       llm_port=None, llm_server_type=None, meeting_topic=None,
                       gen_srt=True, gen_vtt=True):
    """處理音訊檔：ffmpeg 轉檔 → faster-whisper 辨識 → 翻譯 → 存檔，回傳 (log_path, html_path, session_dir)"""
    from datetime import datetime
    import shutil
    _diar_info, _diar_info_mic = {}, {}      # 講者辨識實際用的方法（_diar_engine_label）

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

    lang = _mode_whisper_lang(mode)
    need_translate = mode in _TRANSLATE_MODES

    # Log 檔名（每次處理建子目錄）
    log_prefixes = {"en2zh": "英翻中_時間逐字稿", "zh2en": "中翻英_時間逐字稿",
                    "ja2zh": "日翻中_時間逐字稿", "zh2ja": "中翻日_時間逐字稿",
                    "en": "英文_時間逐字稿", "zh": "中文_時間逐字稿", "ja": "日文_時間逐字稿",
                    "ko2zh": "韓翻中_時間逐字稿", "zh2ko": "中翻韓_時間逐字稿", "ko": "韓文_時間逐字稿"}
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

    if mode in _NAN_INPUT_MODES:
        _lang_disp = "台語（Breeze-ASR-26，輸出漢字）"
    elif _is_nan_mode(mode):
        _lang_disp = "華語（Breeze-ASR-26，台灣華語／台語混用）"
    else:
        _lang_disp = lang
    print(f"  {C_WHITE}辨識語言    {_lang_disp}{RESET}")
    print(f"  {C_DIM}記錄檔      {os.path.relpath(session_dir)}/{RESET}")
    _webui_send({"type": "progress", "stage": "準備中", "detail": os.path.basename(input_path)})

    # 標籤
    src_color, src_label, dst_color, dst_label = _MODE_LABELS[mode]
    if mode in _EN_INPUT_MODES:
        hallucination_check = _is_en_hallucination
    elif mode in _JA_INPUT_MODES:
        hallucination_check = _is_ja_hallucination
    elif mode in _KO_INPUT_MODES:
        # 不可以落到中文過濾：它要求至少兩個漢字，韓文會整句被丟掉
        hallucination_check = _is_ko_hallucination
    else:
        hallucination_check = _is_zh_hallucination

    # 取得音訊總時長（用於進度顯示）
    audio_duration = 0
    probe = _ffprobe_info(wav_path)
    if probe and probe[0] > 0:
        audio_duration = probe[0]

    # 音源分析：mean_volume < -30 dBFS 視為低音量錄音（監視器/行車紀錄/遠場），
    # 自動增益並切換寬鬆參數。乾淨會議錄音完全不介入。
    asr_wav_path, _boosted, use_loose, _mean_dbfs = _audio_profile(wav_path)

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

    if remote_whisper_cfg is not None and _is_nan_mode(mode):
        # Breeze-ASR-26 走本機：伺服器端套用的是一般模型的防幻覺參數組，對 Breeze-ASR-26
        # 反而大幅劣化（實測 CER 17.99% → 56.42%），且時間戳需由用戶端切段產生
        print(f"  {C_DIM}[{BREEZE_MODEL}] 改用本機辨識（GPU 伺服器的辨識參數不適用本模型）{RESET}")
        remote_whisper_cfg = None
    _want_remote = remote_whisper_cfg is not None     # 之後退到本機時，分得出是使用者選的還是伺服器失敗

    if remote_whisper_cfg is not None:
        rw_host = remote_whisper_cfg.get("host", "?")
        rw_port = remote_whisper_cfg.get("whisper_port", REMOTE_WHISPER_DEFAULT_PORT)
        print(f"  {C_WHITE}辨識位置    GPU 伺服器（{rw_host}:{rw_port}）{RESET}")

        # 版本比對（必要時自動更新伺服器），每個 session 只做一次
        _ensure_remote_server_version_once(remote_whisper_cfg)

        # 上傳前檢查伺服器狀態（忙碌/磁碟空間）
        file_size = os.path.getsize(asr_wav_path) if os.path.isfile(asr_wav_path) else 0
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
            try:
                r_segments, r_duration, r_proc_time, r_device = _remote_whisper_transcribe(
                    remote_whisper_cfg, asr_wav_path, model_size, lang,
                    progress_callback=_upload_progress,
                    on_upload_done=_on_upload_done,
                    noisy=use_loose,
                )
            except _RemoteUpdating:
                raise
            except Exception as qe:
                if model_size != QWEN_MODEL:
                    raise
                # Qwen3-ASR 失敗（worker 剛好掛掉、重啟中…）：伺服器本身多半還好，先用伺服器的 Whisper，
                # 不要直接退到本機 CPU（慢很多）
                model_size = "large-v3-turbo"
                print(f"  {C_HIGHLIGHT}[降級] Qwen3-ASR 失敗（{qe}），改用 GPU 伺服器的 {model_size}{RESET}")
                sbar.set_task("GPU 伺服器 辨識中（large-v3-turbo）", reset_timer=False)
                r_segments, r_duration, r_proc_time, r_device = _remote_whisper_transcribe(
                    remote_whisper_cfg, asr_wav_path, model_size, lang,
                    progress_callback=_upload_progress, noisy=use_loose,
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
            _macos_local_network_hint((remote_whisper_cfg or {}).get("host", ""))
            remote_whisper_cfg = None  # fallback

    if not used_remote and model_size == QWEN_MODEL:
        # 本機 Qwen3-ASR；不能跑或失敗時 raw_segments 仍是 None、模型換成本機推薦的 Whisper，接著走下面原本的路
        model_size, raw_segments, _qsbar = _qwen_local_offline(asr_wav_path, mode, audio_duration,
                                                               fell_back=_want_remote)
        if raw_segments is not None:
            sbar = _qsbar

    if not used_remote and raw_segments is None:
        # 本機 faster-whisper
        try:
            from faster_whisper import WhisperModel
            _fw_av_compat()
        except ImportError:
            print(f"  {C_HIGHLIGHT}[錯誤] faster-whisper 未安裝，請執行: pip install faster-whisper{RESET}",
                  file=sys.stderr)
            return None, None, None

        # 台語在 Apple Silicon 上走 mlx-whisper GPU（實測比 CTranslate2 CPU 快約 4 倍）
        _nan_mlx = _is_nan_mode(mode) and _is_apple_silicon() and _has_mlx_whisper()
        _engine_label = "mlx-whisper GPU" if _nan_mlx else "faster-whisper"
        print(f"  {C_WHITE}載入模型    {model_size}（{_engine_label}）...{RESET}", end=" ", flush=True)
        model = None
        if not _nan_mlx:
            model = _call_with_ssl_retry(_FwModel, _resolve_fw_model(model_size),
                                         **_fw_device_kwargs())
        print(f"{C_OK}✓{RESET}")
        print(f"  {C_WHITE}辨識中...{RESET}\n")
        _webui_send({"type": "progress", "stage": "辨識中", "detail": f"本機 {model_size}"})

        sbar = _SummaryStatusBar(model=model_size, task="辨識中", asr_location="本機").start()
        if audio_duration > 0:
            sbar.set_progress("0%")

        def _sbar_progress(pos_sec):
            if audio_duration > 0:
                pct = min(pos_sec / audio_duration, 1.0)
                pos_m, pos_s = divmod(int(pos_sec), 60)
                dur_m, dur_s = divmod(int(audio_duration), 60)
                sbar.set_progress(f"{pct:.0%}  {pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}")

        raw_segments = []
        if _is_nan_mode(mode):
            # 台語：模型不產生時間戳，改由自行 VAD 切段取得時間資訊
            raw_segments = _nan_transcribe_windows(model, asr_wav_path, _sbar_progress,
                                                   use_mlx=_nan_mlx)
            try: del model
            except NameError: pass
        else:
            _kw = _fw_transcribe_kwargs(mode, use_loose)
            segments_iter, info = model.transcribe(asr_wav_path, language=lang, **_kw)

            # 將 generator 轉為 list of dict（與伺服器格式統一）
            for segment in segments_iter:
                _sbar_progress(segment.end)
                text = segment.text.strip()
                if text:
                    raw_segments.append({
                        "start": segment.start,
                        "end": segment.end,
                        "text": text,
                    })

            # 主動釋放 ASR 模型參考（搭配 finally 的 gc + cuda.empty_cache()）
            # 這條路徑使用本機 faster-whisper，模型與 generator 用完即刪除
            try: del segments_iter, info, model
            except NameError: pass

    # 清理增益暫存檔（辨識結束後）
    if _boosted and asr_wav_path != wav_path:
        try:
            os.remove(asr_wav_path)
        except Exception:
            pass

    seg_count = 0
    try:
        # 過濾解碼器卡死的可疑長段（門檻依音源寬鬆與否調整）
        raw_segments = _drop_stuck_segments(raw_segments, loose=use_loose)
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
                text = _s2twp_safe(text)
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
                    info=_diar_info,
                )
                if speaker_labels is None:
                    # 伺服器失敗，降級本機
                    sbar.set_task("伺服器失敗，改用本機講者辨識", reset_timer=False)
                    speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                       num_speakers=num_speakers, sbar=sbar, info=_diar_info)
            else:
                speaker_labels = _diarize_segments(wav_path, valid_segments,
                                                   num_speakers=num_speakers, sbar=sbar, info=_diar_info)
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
                        result = _s2twp_safe(result) if not isinstance(translator, OllamaTranslator) else _to_traditional(result)
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
            _meta["diarize_engine"] = _diar_engine_label(_diar_info)
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
        # 產出 SRT / VTT 字幕檔（在 HTML 之前，讓 HTML footer 能偵測到）
        _srt = None
        if segments_data:
            if gen_srt:
                srt_path = os.path.splitext(log_path)[0] + ".srt"
                _segments_to_srt(segments_data, srt_path)
                _srt = srt_path
            if gen_vtt:
                vtt_path = os.path.splitext(log_path)[0] + ".vtt"
                _segments_to_vtt(segments_data, vtt_path)

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
                          "ko2zh": "韓文", "zh2ko": "中文",
                          "en": "英文", "zh": "中文", "ja": "日文", "ko": "韓文",
                          "en_zh": "英文/中文", "ja_zh": "日文/中文", "ko_zh": "韓文/中文"}
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
            if _diar_info.get("engine") == "nemotron":
                # Nemotron 的 --num-speakers 是上限：偏多時能修，偏少時指定也不會變多
                print(f"  {C_DIM}講者辨識偵測到 {n_spk} 位（Nemotron）；人數偏多時可用 --num-speakers N 設上限重跑{RESET}")
            else:
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
    finally:
        # 釋放 GPU 資源避免 native lib state 累積（0xC0000409 等 fast-fail 崩潰）
        _release_gpu_resources()


def process_bidi_audio_files(lb_path, mic_path, mode, translator_lb, translator_mic,
                              model_size="large-v3-turbo", remote_whisper_cfg=None,
                              diarize=False, num_speakers=None,
                              correct_with_llm=False, llm_model=None, llm_host=None,
                              llm_port=None, llm_server_type=None, meeting_topic=None,
                              gen_srt=True, gen_vtt=True):
    """處理雙向錄音檔：兩路 ASR → 合併 → 翻譯 → 存檔，回傳 (log_path, html_path, session_dir)"""
    from datetime import datetime
    import shutil
    _diar_info, _diar_info_mic = {}, {}      # 講者辨識實際用的方法（_diar_engine_label）

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
    _hall_check = {"en": _is_en_hallucination, "zh": _is_zh_hallucination, "ja": _is_ja_hallucination,
                   "ko": _is_ko_hallucination}
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
                    "ko_zh": "韓中雙向_時間逐字稿",
                    "en2zh": "英翻中_配對時間逐字稿",
                    "zh2en": "中翻英_配對時間逐字稿", "ja2zh": "日翻中_配對時間逐字稿",
                    "zh2ja": "中翻日_配對時間逐字稿", "ko2zh": "韓翻中_配對時間逐字稿",
                    "zh2ko": "中翻韓_配對時間逐字稿", "en": "英文_配對時間逐字稿",
                    "zh": "中文_配對時間逐字稿", "ja": "日文_配對時間逐字稿", "ko": "韓文_配對時間逐字稿"}
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
        """對單一音訊執行 ASR，回傳 (raw_segments list, use_loose)"""
        # 音源分析（兩路獨立判斷，避免單側拖累另一側）
        asr_path, _b, _loose, _ = _audio_profile(wav_path, label=label)

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
                    _rw_cfg, asr_path, model_size, lang,
                    progress_callback=_up, on_upload_done=_done, noisy=_loose)
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
                _fw_av_compat()
            except ImportError:
                print(f"  {C_HIGHLIGHT}[錯誤] faster-whisper 未安裝{RESET}", file=sys.stderr)
                if _b and asr_path != wav_path:
                    try: os.remove(asr_path)
                    except Exception: pass
                return [], _loose

            print(f"  {C_WHITE}{label} 載入模型 {model_size}...{RESET}", end=" ", flush=True)
            model = _call_with_ssl_retry(_FwModel, _resolve_fw_model(model_size), **_fw_device_kwargs())
            print(f"{C_OK}✓{RESET}")

            sbar = _SummaryStatusBar(model=model_size, task=f"{label} 辨識中", asr_location="本機").start()

            _dur = 0
            _probe = _ffprobe_info(asr_path)
            if _probe and _probe[0] > 0:
                _dur = _probe[0]

            _kw = _FW_OFFLINE_KW_LOOSE if _loose else _FW_OFFLINE_KW
            segments_iter, info = model.transcribe(asr_path, language=lang, **_kw)
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

            # 主動釋放本機 faster-whisper 模型（搭配 process_bidi_audio_files 的 finally）
            try: del segments_iter, info, model
            except NameError: pass

        # 清理增益暫存檔
        if _b and asr_path != wav_path:
            try: os.remove(asr_path)
            except Exception: pass

        return raw_segs or [], _loose

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
    lb_raw, lb_loose = _do_asr(lb_wav, lb_lang, "系統音訊")
    mic_raw, mic_loose = _do_asr(mic_wav, mic_lang, "麥克風")
    t_asr = time.monotonic() - t_stage

    # ── 過濾幻覺 + 簡轉繁 ──
    def _filter_segments(raw_segs, lang, hall_check, loose=False):
        raw_segs = _drop_stuck_segments(raw_segs, loose=loose)
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
                text = _s2twp_safe(text)
            valid.append({"start": seg["start"], "end": seg["end"], "text": text})
        return valid

    lb_segs = _filter_segments(lb_raw, lb_lang, lb_hall, loose=lb_loose)
    mic_segs = _filter_segments(mic_raw, mic_lang, mic_hall, loose=mic_loose)

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
                    info=_diar_info,
                )
                if lb_speaker_labels is None:
                    d_sbar.set_task("伺服器失敗，改用本機講者辨識：系統音訊", reset_timer=False)
                    lb_speaker_labels = _diarize_segments(lb_wav, lb_segs,
                                                          num_speakers=num_speakers, sbar=d_sbar, info=_diar_info)
            else:
                d_sbar.set_task("講者辨識：系統音訊", reset_timer=False)
                lb_speaker_labels = _diarize_segments(lb_wav, lb_segs,
                                                      num_speakers=num_speakers, sbar=d_sbar, info=_diar_info)
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
                    info=_diar_info_mic,
                )
                if mic_speaker_labels is None:
                    d_sbar.set_task("伺服器失敗，改用本機講者辨識：麥克風", reset_timer=False)
                    mic_speaker_labels = _diarize_segments(mic_wav, mic_segs,
                                                           num_speakers=num_speakers, sbar=d_sbar, info=_diar_info_mic)
            else:
                d_sbar.set_task("講者辨識：麥克風", reset_timer=False)
                mic_speaker_labels = _diarize_segments(mic_wav, mic_segs,
                                                       num_speakers=num_speakers, sbar=d_sbar, info=_diar_info_mic)
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
            _meta["diarize_engine"] = _diar_engine_label(_diar_info, _diar_info_mic)
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

        # SRT / VTT
        _srt = None
        if segments_data:
            if gen_srt:
                srt_path = os.path.splitext(log_path)[0] + ".srt"
                _segments_to_srt(segments_data, srt_path)
                _srt = srt_path
            if gen_vtt:
                vtt_path = os.path.splitext(log_path)[0] + ".vtt"
                _segments_to_vtt(segments_data, vtt_path)

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
    finally:
        # 釋放 GPU 資源避免 native lib state 累積（0xC0000409 等 fast-fail 崩潰）
        _release_gpu_resources()


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


def _write_meeting_summary(meeting, corrected, output_path, input_path, metadata=None, audio_path=""):
    """會議分析（＋校正逐字稿）寫成摘要 .txt 與 .html。回傳值與 summarize_log_file 相同 (txt, 文字, html)"""
    pub, segs, measured = meeting
    corrected = re.sub(r"^\s*#{2,4}\s*校正逐字稿\s*\n", "", corrected or "").strip()
    text = meeting_summary_markdown(pub, segs, measured)
    if corrected:
        text += "\n## 校正逐字稿\n\n" + corrected + "\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_build_metadata_header(metadata) + text)
    html_path = os.path.splitext(output_path)[0] + ".html"
    _th = os.path.splitext(input_path)[0] + ".html"
    meeting_summary_html(pub, segs, html_path, os.path.basename(input_path), measured=measured,
                         corrected=corrected, summary_txt_path=output_path, transcript_txt_path=input_path,
                         metadata=metadata, transcript_html_path=_th if os.path.exists(_th) else "",
                         audio_path=audio_path)
    return output_path, text, html_path


def _meeting_for_summary(transcript, model, host, port, server_type="ollama", topic=None):
    """摘要要用的會議分析（JTDT）。能用時回傳 (結果, 段落, 時間是否量到的)；
    不能用（套件不在、逐字稿讀不出段落、只有韓文／日文原文、分析整個失敗）時說明原因並回 None，呼叫端退回舊的摘要方式"""
    if not _meeting_modules():
        print(f"  {C_HIGHLIGHT}[提示] 找不到會議分析模組 jtdt_meeting，這次用舊的摘要方式"
              f"（從舊版升級的請再執行一次 {_INSTALL_CMD} --upgrade）{RESET}")
        return None
    segs, measured = meeting_segments_from_log(transcript)
    if len(segs) < 2:
        print(f"  {C_DIM}[提示] 逐字稿讀不出足夠的段落（{len(segs)} 段），這次用舊的摘要方式{RESET}")
        return None
    ok, why = _meeting_language_ok(segs)
    if not ok:
        print(f"  {C_HIGHLIGHT}[提示] {why}，這次用舊的摘要方式{RESET}")
        return None
    _loc = "本機" if host in ("localhost", "127.0.0.1", "::1") else "伺服器"
    sbar = _SummaryStatusBar(model=model, task="會議分析：準備中", location=_loc).start()

    def _prog(frac, msg):
        sbar.set_task(f"會議分析：{msg}", reset_timer=False)
        sbar.set_progress(f"{frac:.0%}")
        _webui_send({"type": "progress", "stage": f"會議分析（{model}）", "detail": f"{msg}，{frac:.0%}"})
    try:
        pub = meeting_analysis(segs, model, host, port, server_type, context=meeting_context(topic),
                               on_progress=_prog)
    except Exception as e:
        sbar.stop()
        print(f"  {C_HIGHLIGHT}[降級] 會議分析失敗（{type(e).__name__}: {e}），這次用舊的摘要方式{RESET}")
        return None
    sbar.stop()
    n = {k: len(v) for k, v in (pub.get("items") or {}).items()}
    print(f"  {C_OK}會議分析完成{RESET} {C_DIM}（{len(segs)} 段、{pub.get('llm_calls', 0)} 次模型請求；"
          f"決議 {n.get('decisions', 0)}、待辦 {n.get('actions', 0)}、風險 {n.get('risks', 0)}、"
          f"未決 {n.get('questions', 0)}、事件 {n.get('impacts', 0)}；議題 {len(pub.get('chapters') or [])}）{RESET}")
    return pub, segs, measured


def summarize_log_file(input_path, model, host, port, server_type="ollama",
                       topic=None, metadata=None, summary_mode="both",
                       audio_path="", summary_rounds=1):
    """讀取記錄檔 → 建 prompt → 呼叫 LLM → 簡繁轉換 → 寫摘要檔
    summary_mode: "both"（摘要+逐字稿）、"summary"（只摘要）、"correct_only"（只校正）、"transcript"（純 ASR）
    summary_rounds: 處理次數（1-3），多次處理後整合可提升品質
    回傳 (output_path, summary_text, html_path)"""
    model = _resolve_summary_model(model, host, port, server_type)
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
    # 韓文（v2.22.0）：比照日文的對應
    elif basename.startswith("韓翻中_雙向時間逐字稿"):
        out_name = basename.replace("韓翻中_雙向時間逐字稿", "韓翻中_雙向摘要", 1)
    elif basename.startswith("中翻韓_雙向時間逐字稿"):
        out_name = basename.replace("中翻韓_雙向時間逐字稿", "中翻韓_雙向摘要", 1)
    elif basename.startswith("韓文_雙向時間逐字稿"):
        out_name = basename.replace("韓文_雙向時間逐字稿", "韓文_雙向摘要", 1)
    elif basename.startswith("韓中雙向_時間逐字稿"):
        out_name = basename.replace("韓中雙向_時間逐字稿", "韓中雙向_摘要", 1)
    elif basename.startswith("韓翻中_配對時間逐字稿"):
        out_name = basename.replace("韓翻中_配對時間逐字稿", "韓翻中_配對摘要", 1)
    elif basename.startswith("中翻韓_配對時間逐字稿"):
        out_name = basename.replace("中翻韓_配對時間逐字稿", "中翻韓_配對摘要", 1)
    elif basename.startswith("韓文_配對時間逐字稿"):
        out_name = basename.replace("韓文_配對時間逐字稿", "韓文_配對摘要", 1)
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

    # 會議分析（JTDT，v2.25.0）：重點摘要改用它；「摘要＋校正逐字稿」時校正逐字稿仍用下面原本的方式產生
    meeting = _meeting_for_summary(transcript, model, host, port, server_type, topic) \
        if summary_mode in ("both", "summary") else None
    if meeting is not None:
        if summary_mode == "summary":
            return _write_meeting_summary(meeting, "", output_path, input_path, metadata, audio_path)
        summary_mode = "transcript"          # 下面只產生校正逐字稿

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
        _warn_if_transcript_truncated(transcript, summary)
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
            seg = re.sub(r'<think>[\s\S]*?</think>', '', seg).strip()
            seg = re.sub(r'<think>[\s\S]*', '', seg).strip()
            seg = _s2twp_safe(seg)
            _warn_if_transcript_truncated(chunk, seg, f"第 {i+1}/{len(chunks)} 段")
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
        _retry_result = re.sub(r'<think>[\s\S]*?</think>', '', _retry_result).strip()
        _retry_result = re.sub(r'<think>[\s\S]*', '', _retry_result).strip()
        _retry_result = _s2twp_safe(_retry_result)
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
        _tc_result = re.sub(r'<think>[\s\S]*?</think>', '', _tc_result).strip()
        _tc_result = re.sub(r'<think>[\s\S]*', '', _tc_result).strip()
        _tc_result = _s2twp_safe(_tc_result)
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
                    _r_seg = re.sub(r'<think>[\s\S]*?</think>', '', _r_seg).strip()
                    _r_seg = re.sub(r'<think>[\s\S]*', '', _r_seg).strip()
                    _r_segs.append(_s2twp_safe(_r_seg))
                sbar_r.set_task(f"第 {ri} 次 - 合併")
                _r_combined = "\n\n---\n\n".join(
                    f"### 第 {i+1} 段\n{s}" for i, s in enumerate(_r_segs))
                _r_merge_prompt = SUMMARY_MERGE_PROMPT_TEMPLATE.format(summaries=_r_combined)
                _r_result = call_ollama_raw(_r_merge_prompt, model, host, port, spinner=sbar_r,
                                            live_output=False, server_type=server_type)
            sbar_r.freeze()
            sbar_r.stop()
            round_results.append(_s2twp_safe(_r_result))
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

    # 移除 <think>...</think> 標籤（部分模型如 Qwen3 會自動思考）
    summary = re.sub(r'<think>[\s\S]*?</think>', '', summary).strip()
    summary = re.sub(r'<think>[\s\S]*', '', summary).strip()

    summary = _s2twp_safe(summary)

    # 校正逐字稿：LLM 漏掉的 Speaker 標籤，自動補上（與 HTML 邏輯對齊）
    summary = _fix_speaker_labels_in_text(summary)

    if meeting is not None:                   # both：會議分析＋校正逐字稿
        return _write_meeting_summary(meeting, summary, output_path, input_path, metadata, audio_path)

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


# ── 會議摘要：jt-doc-tools（JTDT）的會議分析（v2.25.0）──────────────────
# 使用者 2026-09-28：「把 jtdt 的會議摘要功能完全抄過來，讓 jtlw 產生的會議摘要也有同樣高品質」。
# 核心在 jtdt_meeting/（與 JTDT 同一份、**不在這邊改**，tools/test_jtdt_modules_in_sync.py 比對雜湊）；
# 這裡只做 jtlw 的接法：jtlw 逐字稿 → 段落 → full_analysis(段落, ask) → Markdown／HTML。
# 它的設計（每一條都要附段號、引用逐條驗證、摘要只從驗證過的項目寫）與實測數據見 jtdt_meeting/meeting_insight.py 開頭。
#
# 呼叫模型照 JTDT 的 LLMClient.text_query 一比一：/v1/chat/completions、溫度 0、system 一句「只輸出答案」、
# 使用者訊息前加 /no_think、Ollama 再帶 think:false 與 reasoning_effort:"none"。
# 多做兩件 jtlw 本來就有的事：去掉 <think>…</think>（JTDT 沒去，模型吐推理時那一窗會變成空的）、
# 模型寫的文字偵測到簡體才轉繁體（_to_traditional）。
_MEETING_SYSTEM = ("Respond with ONLY the requested output. No reasoning traces, no <think> tags, "
                   "no prefaces, no explanations. Output the final answer directly.")
_MEETING_TIMEOUT = 600          # JTDT timeout_seconds 預設值
_MEETING_KIND_ORDER = ("impacts", "decisions", "actions", "risks", "questions")
_MEETING_KIND_LABELS = {"impacts": "事件與影響", "decisions": "決議", "actions": "待辦",
                        "risks": "風險", "questions": "未決問題"}
_MEETING_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
# jtlw 逐字稿的一行：[時間] 或 [起-迄]、雙向的 ◀／▶、[Speaker N]、[語言標籤] 文字
_MEETING_LINE_RE = re.compile(
    rf"^\[({_MEETING_TS})(?:-({_MEETING_TS}))?\]\s*([◀▶])?\s*(?:\[(Speaker \d+)\]\s*)?\[([^\]\s]{{1,4}})\]\s*(.+)$")
_MEETING_DIRECTION = {"◀": "對方", "▶": "我方"}


def _meeting_modules():
    """(meeting_insight, meeting_charts, transcript_parse)；jtdt_meeting 不在時回 None。
    從舊版升級時，第一次 --upgrade 跑的是舊的安裝腳本、拿不到這個資料夾（要跑第二次），
    這時退回舊的摘要方式，不可以讓整個程式 import 失敗"""
    try:
        from jtdt_meeting import meeting_insight, meeting_charts, transcript_parse
        return meeting_insight, meeting_charts, transcript_parse
    except Exception:
        return None


def _clock_ms(s):
    sec = 0
    for p in s.split(":"):
        sec = sec * 60 + int(p)
    return sec * 1000


def meeting_segments_from_log(text):
    """jtlw 逐字稿文字 → (段落, 時間是否為量到的)。段落是 JTDT 會議分析的格式
    `{seq, speaker?, start_ms, end_ms?, text}`，已經照 JTDT 的規則合併同一講者的短句、切開超過 400 字的段落、重新編號。

    - 翻譯模式同一個時間點有原文與譯文兩行：**取「中」那一行**（分析的提示詞與引用比對都是中文），沒有中文才取第一行
    - 離線逐字稿有起訖時間（量到的）；即時逐字稿只有牆上時間，換成相對第一句的時間、結束用下一句的開始補
      （與 JTDT 讀純文字逐字稿相同，所以「發言時間」要標成推估）
    - 講者：[Speaker N]；雙向逐字稿沒有講者時用 ◀ 對方／▶ 我方"""
    mods = _meeting_modules()
    rows = []
    for raw in (text or "").splitlines():
        m = _MEETING_LINE_RE.match(raw.strip())
        if not m:
            continue
        t1, t2, direction, spk, label, body = m.groups()
        body = body.strip()
        if not body:
            continue
        who = spk or _MEETING_DIRECTION.get(direction or "")
        key = (t1, t2, who)
        if rows and rows[-1]["key"] == key and label not in rows[-1]["texts"]:
            rows[-1]["texts"][label] = body
        else:
            rows.append({"key": key, "t1": t1, "t2": t2, "who": who, "texts": {label: body}})
    measured = bool(rows) and all(r["t2"] for r in rows)
    segs, base, prev, day = [], None, None, 0
    offset, last_start, last_end = 0, None, 0     # --summarize 一次給多個檔：每個檔都從 00:00 起算
    for r in rows:
        start = _clock_ms(r["t1"])
        if not r["t2"]:                     # 即時逐字稿：牆上時間 → 相對第一句（跨午夜補一天）
            if prev is not None and start + day < prev - 12 * 3600 * 1000:
                day += 24 * 3600 * 1000
            start += day
            prev = start
            base = start if base is None else base
            start -= base
        elif last_start is not None and start + offset < last_start - 60 * 1000:
            offset = last_end               # 時間倒退超過一分鐘＝下一個檔案，接在前一個後面
        if r["t2"]:
            start += offset
            last_start = start
        seg = {"text": r["texts"].get("中") or next(iter(r["texts"].values())), "start_ms": start}
        if r["t2"]:
            end = _clock_ms(r["t2"]) + offset
            if end >= start:
                seg["end_ms"] = end
                last_end = max(last_end, end)
        if r["who"]:
            seg["speaker"] = r["who"]
        segs.append(seg)
    if not measured:
        for a, b in zip(segs, segs[1:]):
            if "end_ms" not in a and b["start_ms"] >= a["start_ms"]:
                a["end_ms"] = b["start_ms"]
    if mods and segs:
        segs = mods[2]._merge(segs)
    else:
        for i, s in enumerate(segs, 1):
            s["seq"] = i
    return segs, measured


def _meeting_language_ok(segments):
    """JTDT 的引用驗證只認漢字（二元字組）與拉丁字：**韓文會整條被當成空的丟掉、日文只剩漢字勉強比得到**
    （2026-09-28 實測 `수요일까지 견적서 송부` → 內容是空的）。這兩種只有原文、沒有中文譯文時不走會議分析。
    回傳 (能不能用, 原因)"""
    text = "".join(s.get("text", "") for s in segments)
    hangul = sum(1 for c in text if "가" <= c <= "힯" or "ᄀ" <= c <= "ᇿ")
    kana = sum(1 for c in text if "぀" <= c <= "ヿ")
    han = sum(1 for c in text if "㐀" <= c <= "鿿")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    total = max(1, hangul + kana + han + latin)
    if hangul / total > 0.2:
        return False, "韓文逐字稿（會議分析目前只能驗證中文與英文的引用）"
    if kana / total > 0.2:
        return False, "日文逐字稿（會議分析目前只能驗證中文與英文的引用）"
    return True, ""


def _meeting_ask(model, host, port, server_type="ollama", timeout=_MEETING_TIMEOUT, cancelled=None):
    """回傳 ask(prompt) → 模型回覆文字（會議分析每一次呼叫模型都經過這裡）"""
    url = f"http://{host}:{port}/v1/chat/completions"
    ollama = server_type != "openai"
    state = {"reasoning_effort": ollama}

    def ask(prompt):
        if cancelled and cancelled():
            raise RuntimeError("已取消")
        payload = {"model": model, "temperature": 0.0, "stream": False,
                   "messages": [{"role": "system", "content": _MEETING_SYSTEM},
                                {"role": "user", "content": "/no_think\n\n" + prompt}]}
        if ollama:
            payload["think"] = False
            if state["reasoning_effort"]:
                payload["reasoning_effort"] = "none"

        def send():
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        try:
            res = send()
        except urllib.error.HTTPError as e:
            # 不認得 reasoning_effort 的伺服器回 400：拿掉再送一次，之後都不送
            if e.code != 400 or "reasoning_effort" not in payload:
                raise
            payload.pop("reasoning_effort")
            state["reasoning_effort"] = False
            res = send()
        out = (res.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        out = re.sub(r"<think>[\s\S]*?</think>", "", out)
        out = re.sub(r"<think>[\s\S]*", "", out)
        return out.strip()
    return ask


def _meeting_traditional(pub):
    """模型寫的文字（摘要、項目、負責人、期限、章節標題、心智圖標籤）偵測到簡體才轉繁體。
    **只轉顯示用的文字**：引用驗證在分析裡已經做完，段號不動"""
    s = pub.get("summary") or {}
    if s.get("text"):
        s["text"] = _to_traditional(s["text"])
    for items in (pub.get("items") or {}).values():
        for it in items:
            for k in ("text", "owner", "due_text"):
                if isinstance(it.get(k), str) and it[k]:
                    it[k] = _to_traditional(it[k])
    for c in pub.get("chapters") or []:
        if c.get("title"):
            c["title"] = _to_traditional(c["title"])
    for n in pub.get("mindmap") or []:
        for k in ("label", "label_full"):
            if isinstance(n.get(k), str) and n[k]:
                n[k] = _to_traditional(n[k])
    return pub


def meeting_context(topic=None, extra=None):
    """給會議分析的背景資料（JTDT 的 context）：只拿來讀懂逐字稿，**不會變成項目**（有防抄機制）"""
    lines = []
    if topic:
        lines.append(f"會議主題：{topic}")
    if extra:
        lines.append(str(extra).strip())
    return "\n".join(l for l in lines if l) or None


def meeting_analysis(segments, model, host, port, server_type="ollama", context=None,
                     on_progress=None, cancelled=None, timeout=_MEETING_TIMEOUT):
    """段落 → 會議分析結果（JTDT `Analysis.to_public()` 再加 `llm_calls`）。套件不在時丟 RuntimeError"""
    mods = _meeting_modules()
    if not mods:
        raise RuntimeError("找不到 jtdt_meeting（從舊版升級時請再執行一次 --upgrade）")
    mi = mods[0]
    ask = _meeting_ask(model, host, port, server_type, timeout=timeout, cancelled=cancelled)
    an = mi.full_analysis(segments, ask, context=context, on_progress=on_progress)
    pub = an.to_public()
    pub["llm_calls"] = an.calls
    return _meeting_traditional(pub)


def _meeting_cite(ids, by_seq):
    """引用 → 「03:12、05:40」；沒有時間就寫段號"""
    out = []
    for i in ids or []:
        seg = by_seq.get(i)
        if seg is not None and seg.get("start_ms") is not None:
            out.append(_format_timestamp(seg["start_ms"] / 1000))
        else:
            out.append(f"第 {i} 段")
    return "、".join(dict.fromkeys(out))


def _meeting_speaker_rows(pub, measured):
    """發言統計：[(名稱, 次數, 字數, 字數佔比, 發言時間文字)]；有時間照時間排、否則照字數"""
    stats = pub.get("speaker_stats") or {}
    rows = []
    for name, st in stats.items():
        label = "未標示發言者" if name == "unknown" else name
        ms = st.get("speaking_ms")
        t = ""
        if ms is not None:
            t = _format_timestamp(ms / 1000) + (f"（{st.get('percentage', 0)}%）" if st.get("percentage") is not None else "")
        rows.append((label, st.get("turn_count", 0), st.get("chars", 0), st.get("char_pct", 0), t, ms))
    if rows and all(r[5] is not None for r in rows):
        rows.sort(key=lambda r: -r[5])
    else:
        rows.sort(key=lambda r: -r[2])
    return rows


def meeting_summary_markdown(pub, segments, measured=True):
    """會議分析 → Markdown（JTDT 匯出的章節順序：摘要、五類項目、議題、發言統計）。
    引用寫成逐字稿的時間點（jtlw 的逐字稿與字幕檔都用時間找），沒有時間才寫段號"""
    by_seq = {s["seq"]: s for s in segments}
    out = ["## 重點摘要", ""]
    summ = pub.get("summary") or {}
    out.append(summ.get("text") or "（摘要沒有產生；下面的項目與議題不受影響）")
    if summ.get("grounded") is False and summ.get("unsupported"):
        out += ["", "> ⚠ 這幾個詞在逐字稿裡找不到依據：" + "、".join(summ["unsupported"])]
    items = pub.get("items") or {}
    for kind in _MEETING_KIND_ORDER:
        rows = items.get(kind) or []
        if not rows:
            continue
        out += ["", f"## {_MEETING_KIND_LABELS[kind]}", ""]
        for it in rows:
            extra = []
            if kind == "actions":
                extra.append(f"負責：{it.get('owner') or '未指定'}")
                extra.append(f"期限：{it.get('due_text') or '未定'}")
            tail = f"（{'，'.join(extra)}）" if extra else ""
            cite = _meeting_cite(it.get("segment_ids"), by_seq)
            out.append(f"- {it.get('text', '')}{tail}" + (f"（{cite}）" if cite else ""))
    if not any(items.get(k) for k in _MEETING_KIND_ORDER):
        out += ["", "> 分析沒有在逐字稿裡找到決議、待辦、風險、未決問題或事件。"]
    chapters = pub.get("chapters") or []
    if chapters:
        out += ["", "## 議題", ""]
        for c in chapters:
            if c.get("start_ms") is not None:
                span = f"{_format_timestamp(c['start_ms'] / 1000)}–{_format_timestamp((c.get('end_ms') or c['start_ms']) / 1000)}"
                pct = f"，{c['percentage']}%" if c.get("percentage") is not None else ""
                out.append(f"- {span} {c['title']}（{_format_timestamp((c.get('duration_ms') or 0) / 1000)}{pct}）")
            else:
                out.append(f"- {c['title']}（第 {c['start_seq']}–{c['end_seq']} 段）")
    rows = _meeting_speaker_rows(pub, measured)
    if len(rows) >= 2:
        tcol = "發言時間" if measured else "推估發言時間"
        out += ["", "## 發言統計", "", f"| 發言者 | 發言次數 | 字數 | 字數佔比 | {tcol} |",
                "|---|--:|--:|--:|--:|"]
        for label, turns, chars, pct, t, _ms in rows:
            out.append(f"| {label} | {turns} | {chars} | {pct}% | {t or '—'} |")
        if not measured:
            out += ["", "> 逐字稿沒有每一句的結束時間，發言時間是用下一句的開始推估的，包含停頓。"]
    out += ["", f"> 會議分析：每一條都附逐字稿時間點、引用經過比對；共送出 {pub.get('llm_calls', 0)} 次模型請求。"
            "空白的類別表示**分析沒有在逐字稿裡找到**，不代表會議一定沒有。"]
    return "\n".join(out).rstrip() + "\n"


def _meeting_html_body(pub, segments, measured, corrected=""):
    """會議分析的 HTML 主體（卡片＋圖表＋依據逐字稿）；引用可點、跳到下面的逐字稿那一段"""
    import html as H
    mods = _meeting_modules()
    by_seq = {s["seq"]: s for s in segments}

    def cites(ids):
        chips = []
        for i in ids or []:
            seg = by_seq.get(i)
            lab = _format_timestamp(seg["start_ms"] / 1000) if seg and seg.get("start_ms") is not None else f"#{i}"
            chips.append(f'<a class="cite" href="#seg-{int(i)}">{H.escape(lab)}</a>')
        return " ".join(chips)

    parts = ['<h2>重點摘要</h2>']
    summ = pub.get("summary") or {}
    parts.append(f'<p class="lead">{H.escape(summ.get("text") or "（摘要沒有產生；下面的項目與議題不受影響）")}</p>')
    if summ.get("grounded") is False and summ.get("unsupported"):
        parts.append('<p class="warn">⚠ 這幾個詞在逐字稿裡找不到依據：' + H.escape("、".join(summ["unsupported"])) + "</p>")
    parts.append('<h2>決議與待辦</h2><div class="cards">')
    items = pub.get("items") or {}
    for kind in _MEETING_KIND_ORDER:
        rows = items.get(kind) or []
        parts.append(f'<div class="card k-{kind}"><h3>{_MEETING_KIND_LABELS[kind]}'
                     f'<span class="n">{len(rows)}</span></h3>')
        if not rows:
            parts.append('<p class="empty">分析沒有在逐字稿裡找到這一類的內容。</p>')
        else:
            parts.append("<ul>")
            for it in rows:
                meta = ""
                if kind == "actions":
                    meta = (f'<span class="who">負責：{H.escape(it.get("owner") or "未指定")}</span>'
                            f'<span class="who">期限：{H.escape(it.get("due_text") or "未定")}</span>')
                parts.append(f'<li>{H.escape(it.get("text", ""))} {meta}<span class="cites">{cites(it.get("segment_ids"))}</span></li>')
            parts.append("</ul>")
        parts.append("</div>")
    parts.append("</div>")
    charts = mods[1].build_all(pub, segments) if mods else {}
    chapters = pub.get("chapters") or []
    if chapters:
        parts.append("<h2>議題時間軸</h2><table class=\"tbl\"><tr><th>時間</th><th>議題</th><th>佔比</th></tr>")
        for c in chapters:
            if c.get("start_ms") is not None:
                t = f'<a class="cite" href="#seg-{int(c["start_seq"])}">{_format_timestamp(c["start_ms"] / 1000)}</a>'
                pct = c.get("percentage")
            else:
                t = f'<a class="cite" href="#seg-{int(c["start_seq"])}">#{int(c["start_seq"])}</a>'
                pct = None
            bar = (f'<div class="bar"><span style="width:{max(1, min(100, float(pct)))}%"></span></div>{pct}%'
                   if pct is not None else "")
            parts.append(f"<tr><td>{t}</td><td>{H.escape(c['title'])}</td><td>{bar}</td></tr>")
        parts.append("</table>")
        if charts.get("timeline"):
            parts.append(f'<div class="chart">{charts["timeline"]}</div>')
    rows = _meeting_speaker_rows(pub, measured)
    if len(rows) >= 2:
        tcol = "發言時間" if measured else "推估發言時間"
        parts.append(f'<h2>發言統計</h2><table class="tbl"><tr><th>發言者</th><th>發言次數</th><th>字數</th>'
                     f'<th>字數佔比</th><th>{tcol}</th></tr>')
        for label, turns, chars, pct, t, _ms in rows:
            parts.append(f"<tr><td>{H.escape(str(label))}</td><td>{turns}</td><td>{chars}</td><td>{pct}%</td>"
                         f"<td>{H.escape(t or '—')}</td></tr>")
        parts.append("</table>")
        if not measured:
            parts.append('<p class="note">逐字稿沒有每一句的結束時間，發言時間是用下一句的開始推估的，包含停頓。</p>')
        if charts.get("speaker_share"):
            parts.append(f'<div class="chart">{charts["speaker_share"]}</div>')
    if charts.get("mindmap"):
        # 圖自己帶「討論結構」標題，外面不再加一次（JTDT 也有擋標題重複的測試）
        parts.append(f'<div class="chart" style="margin-top:1.6em">{charts["mindmap"]}</div>')
    if corrected.strip():
        parts.append("<h2>校正逐字稿</h2>")
        for para in re.split(r"\n\s*\n", corrected.strip()):
            para = para.strip()
            if not para or para.startswith("## "):
                continue
            m = re.match(r"^\*{0,2}(Speaker \d+|講者 ?\d+|對方|我方)\*{0,2}\s*[：:]\s*", para)
            color = _speaker_html_color(m.group(1)) if m else None
            if color:                       # 與舊的摘要 HTML 相同：每位講者一個顏色
                parts.append(f'<p class="speaker" style="color:{color}"><strong>{H.escape(m.group(1))}：</strong>'
                             f'{H.escape(para[m.end():])}</p>')
            else:
                parts.append(f"<p>{H.escape(para)}</p>")
    parts.append('<h2>依據（逐字稿）</h2><div class="segs">')
    for s in segments:
        t = _format_timestamp(s["start_ms"] / 1000) if s.get("start_ms") is not None else f"#{s['seq']}"
        spk = s.get("speaker")
        parts.append(f'<p id="seg-{int(s["seq"])}"><span class="t">{H.escape(t)}</span>'
                     + (f'<span class="spk" style="color:{_speaker_html_color(spk) or "#ffcb6b"}">'
                        f'{H.escape(str(spk))}</span>' if spk else "")
                     + f'{H.escape(s.get("text", ""))}</p>')
    parts.append("</div>")
    parts.append(f'<p class="note">每一條都附逐字稿時間點（點一下跳到依據的原文），引用經過比對；'
                 f'共送出 {pub.get("llm_calls", 0)} 次模型請求。空白的類別表示分析沒有在逐字稿裡找到，不代表會議一定沒有。</p>')
    return "\n".join(parts)


_MEETING_HTML_CSS = """
  body { font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
         max-width: 980px; margin: 40px auto; padding: 0 20px;
         background: #1a1a2e; color: #e0e0e0; line-height: 1.8; }
  h1 { color: #82aaff; border-bottom: 2px solid #82aaff; padding-bottom: 8px; }
  h2 { color: #c792ea; margin-top: 1.6em; }
  .meta { color: #999; font-size: 0.85em; margin-bottom: 1.5em; line-height: 1.6; }
  .badge { display: inline-block; background: #2d3a5a; color: #82aaff; padding: 2px 10px;
           border-radius: 10px; font-size: 0.9em; margin-bottom: 4px; }
  .lead { font-size: 1.05em; }
  .warn { color: #ffcb6b; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 14px; }
  .card { background: #22223a; border-radius: 10px; padding: 10px 16px; border-top: 4px solid #666; min-width: 0; }
  .card h3 { margin: 4px 0 6px; font-size: 1.05em; }
  .card .n { float: right; color: #999; font-weight: normal; }
  .card ul { margin: 0; padding-left: 1.2em; }
  .card li { margin: 6px 0; overflow-wrap: anywhere; }
  .k-impacts { border-top-color: #0f766e; } .k-decisions { border-top-color: #047857; }
  .k-actions { border-top-color: #1d4ed8; } .k-risks { border-top-color: #b91c1c; }
  .k-questions { border-top-color: #b45309; }
  .empty { color: #888; font-size: 0.9em; }
  .who { display: inline-block; font-size: 0.85em; color: #c3e88d; margin-right: 8px; }
  .cite { display: inline-block; font-size: 0.8em; color: #82aaff; background: #2d3a5a; border-radius: 6px;
          padding: 0 6px; margin: 0 2px; text-decoration: none; }
  .cite:hover { background: #3d4d78; }
  .tbl { border-collapse: collapse; width: 100%; margin: 0.6em 0; }
  .tbl th, .tbl td { border-bottom: 1px solid #333; padding: 4px 8px; text-align: left; }
  .tbl td:nth-child(n+2) { overflow-wrap: anywhere; }
  .bar { display: inline-block; width: 90px; height: 8px; background: #333; border-radius: 4px;
         margin-right: 6px; vertical-align: middle; }
  .bar span { display: block; height: 100%; background: #82aaff; border-radius: 4px; }
  .chart { background: #fff; border-radius: 10px; padding: 8px; margin: 10px 0; overflow-x: auto; }
  .chart svg { max-width: 100%; height: auto; }
  .segs p { margin: 2px 0; padding: 2px 6px; border-radius: 4px; }
  .segs p:target { background: #3d4d78; }
  .segs .t { color: #82aaff; margin-right: 8px; font-size: 0.85em; }
  .segs .spk { color: #ffcb6b; margin-right: 8px; font-size: 0.85em; }
  .note { color: #999; font-size: 0.85em; }
  .footer { margin-top: 2em; color: #888; font-size: 0.9em; }
  .footer a { color: #82aaff; }
  @media (max-width: 600px) { body { margin: 16px auto; padding: 0 12px; } .cards { grid-template-columns: 1fr; } }
"""


def meeting_summary_html(pub, segments, html_path, source_name="", measured=True, corrected="",
                         summary_txt_path="", transcript_txt_path="", metadata=None,
                         transcript_html_path="", audio_path=""):
    """會議分析 → 獨立的 HTML 檔（樣式與舊版摘要一致；圖表是 JTDT 的 SVG，白底卡片）"""
    import html as H
    title = H.escape(source_name) if source_name else "會議記錄"
    meta_html = _summary_meta_html(title, metadata, badge="會議記錄")
    footer_html = _summary_footer_html(html_path, summary_txt_path, transcript_txt_path,
                                       transcript_html_path, audio_path)
    page = (f'<!DOCTYPE html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{title} - 會議記錄</title>\n<style>{_MEETING_HTML_CSS}</style>\n</head>\n<body>\n'
            f'<h1>{title}</h1>\n{meta_html}\n{_meeting_html_body(pub, segments, measured, corrected)}\n'
            f'<div class="footer">{footer_html}</div>\n</body>\n</html>\n')
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    return html_path


def _summary_footer_html(html_path, summary_txt_path="", transcript_txt_path="",
                         transcript_html_path="", audio_path=""):
    """摘要 HTML 底部的檔案連結（舊版摘要與會議分析摘要共用）"""
    import html as html_mod
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
        _vtt_bn = os.path.splitext(os.path.basename(transcript_txt_path))[0] + ".vtt"
        _vtt_full = os.path.join(os.path.dirname(html_path), _vtt_bn)
        if os.path.isfile(_vtt_full):
            footer_links.append(f'<a href="{html_mod.escape(_vtt_bn)}">字幕檔 (VTT)</a>')
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
    return footer_html


def _summary_meta_html(title, metadata, badge="AI 摘要"):
    """摘要 HTML 開頭的處理資訊（來源、辨識、講者、翻譯、摘要模型、主題）。title 已跳脫"""
    import html as html_mod
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
    _badge = f'<span class="badge">jt-live-whisper v{APP_VERSION} {badge}</span>'
    meta_html = '<div class="meta">' + _badge + "<br>\n  " + "<br>\n  ".join(html_mod.escape(l) for l in meta_lines) + '</div>'
    return meta_html


# 講者顏色（8 色循環，與終端機 SPEAKER_COLORS 對應的 HTML 色碼）；舊的摘要與會議分析的 HTML 共用
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


def _speaker_html_color(label):
    """Speaker N／講者 N → 第 N 個顏色；雙向逐字稿的對方／我方各一色；其他（沒有講者）回 None"""
    m = re.match(r"^(?:Speaker |講者 ?)(\d+)$", str(label or "").strip())
    if m:
        return _SPEAKER_HTML_COLORS[(int(m.group(1)) - 1) % len(_SPEAKER_HTML_COLORS)]
    return {"對方": _SPEAKER_HTML_COLORS[0], "我方": _SPEAKER_HTML_COLORS[2]}.get(str(label or "").strip())


def _summary_to_html(summary_text, html_path, source_name="",
                     summary_txt_path="", transcript_txt_path="",
                     metadata=None, transcript_html_path="",
                     audio_path=""):
    """將摘要純文字轉為帶樣式的 HTML 檔"""
    import html as html_mod

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
                # 超長段落（LLM 未分段的校正逐字稿）→ 每 5 句左右自動插入段落分隔
                if len(s) > 500:
                    sentences = re.split(r'(?<=[。！？.!?])\s*', s)
                    chunk, chunks = [], []
                    for sent in sentences:
                        chunk.append(sent)
                        if len(chunk) >= 5:
                            chunks.append("".join(chunk))
                            chunk = []
                    if chunk:
                        chunks.append("".join(chunk))
                    for c in chunks:
                        c_esc = html_mod.escape(c)
                        c_esc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', c_esc)
                        body_parts.append(f"<p>{c_esc}</p>")
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

    footer_html = _summary_footer_html(html_path, summary_txt_path, transcript_txt_path,
                                      transcript_html_path, audio_path)

    meta_html = _summary_meta_html(title, metadata)

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
    _vtt_bn = os.path.splitext(os.path.basename(txt_path))[0] + ".vtt"
    _vtt_full = os.path.join(os.path.dirname(html_path), _vtt_bn)
    if os.path.isfile(_vtt_full):
        footer_links.append(f'<a href="{html_mod.escape(_vtt_bn)}">字幕檔 (VTT)</a>')
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
    "translate_model": "",  # 翻譯模型名稱（如 gemma4:26b）
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
        label = "轉錄" if _status_bar_state["mode"] in ("zh", "en", "ja", "ko") else "翻譯"
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
        label = "轉錄" if _status_bar_state["mode"] in ("zh", "en", "ja", "ko") else "翻譯"
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
        (f"{_sc} --input meeting.mp3 --mode zh -m {QWEN_MODEL}", "中文會議用 Qwen3-ASR（實驗；GPU 伺服器或本機）"),
        (f"{_sc} --input meeting.mp3 --diarize --summarize", "辨識 + 翻譯 + 摘要"),
        (f"{_sc} --input m.mp3 --diarize --mode zh --summarize", "中文辨識 + 講者 + 摘要"),
        (f"{_sc} --input meeting.mp3 --local-asr", "強制本機 辨識"),
        (f"{_sc} --summarize log1.txt log2.txt", "批次摘要記錄檔"),
        (f"{_sc} --tts-file 講稿.txt", "文字朗讀：台灣華語念出來（GPU 伺服器或 Apple Silicon Mac）"),
        (f"{_sc} --tts-file 講稿.txt --tts-rate 1.2 --tts-save mp3", "朗讀＋存成 MP3，語速 1.2 倍"),
        (f"{_sc} --tts-file 講稿.txt --tts-device none --tts-save mp3", "文字轉語音檔（不播放）"),
        (f"{_sc} --tts-text '今天下午三點半開會。'", "直接念一段文字"),
    ]
    col = max(len(cmd) for cmd, _ in examples) + 3
    epilog = "範例:\n" + "\n".join(f"  {cmd:<{col}}{desc}" for cmd, desc in examples)
    parser = argparse.ArgumentParser(
        description="即時英翻中字幕系統 jt-live-whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    mode_names = list(MODE_MAP.keys())
    model_names = [name for name, _, _ in WHISPER_MODELS] + [BREEZE_MODEL, QWEN_MODEL]
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
        help=f"語音辨識模型 ({' / '.join(model_names)}，--input 預設 large-v3-turbo，中日文品質最好用 -m large-v3；"
             f"{BREEZE_MODEL} 限台語與華語模式，台灣華語夾雜台語時可選用；"
             f"{QWEN_MODEL}（實驗）限離線中／英／韓，GPU 伺服器或本機（Apple Silicon 用 MLX、其他平台用 transformers）)")
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
        help=f"LLM 翻譯模型名稱 (預設 {DEFAULT_TRANSLATE_MODEL}，伺服器沒有時改用 qwen2.5:14b)")
    parser.add_argument(
        "--llm-host", metavar="HOST", dest="ollama_host",
        help="LLM 伺服器位址，自動偵測 Ollama 或 OpenAI 相容 (例如 192.168.1.40:11434)")
    parser.add_argument(
        "--list-devices", action="store_true",
        help="列出可用音訊裝置後離開")
    parser.add_argument(
        "--audio-source", choices=["sck", "blackhole"], metavar="SOURCE",
        help="macOS 系統音訊來源 (sck / blackhole，預設 sck，未授權時自動退回 blackhole)")
    parser.add_argument(
        "--sck-permission", action="store_true",
        help="macOS 觸發「螢幕錄製」權限授權對話框後離開（ScreenCaptureKit 需要）")
    parser.add_argument(
        "--record", action="store_true",
        help="即時模式同時錄製音訊為 WAV 檔（存入 recordings/）")
    parser.add_argument(
        "--rec-device", type=int, metavar="ID",
        help="錄音裝置 ID (可與 ASR 裝置不同，例如聚集裝置可同時錄雙方聲音)")
    parser.add_argument(
        "--mic-device", type=int, metavar="ID",
        help="麥克風裝置 ID（--mic、雙向模式、純錄音時指定麥克風輸入裝置）")
    parser.add_argument(
        "--rec-source", choices=list(REC_SOURCES),
        help="純錄音（--mode record）錄哪些聲音：both＝系統音訊＋麥克風混成一軌（偵測得到麥克風時的預設）、"
             "system＝只錄系統音訊、mic＝只錄麥克風。-d 指定系統音訊、--mic-device 指定麥克風")
    parser.add_argument(
        "--input", nargs="+", metavar="FILE",
        help="離線處理音訊檔 (mp3/wav/m4a/flac 等，用 faster-whisper 辨識)")
    parser.add_argument(
        "--summarize", nargs="*", metavar="FILE", default=None,
        help="摘要模式：讀取記錄檔生成會議摘要（重點摘要、決議、待辦、風險、議題，每一條附逐字稿時間點）後離開"
             "（與 --input 合用時不需指定檔案）")
    parser.add_argument(
        "--summary-model", metavar="MODEL", default=SUMMARY_DEFAULT_MODEL,
        help=f"摘要與逐字稿校正用的 LLM 模型 (預設 {SUMMARY_DEFAULT_MODEL})")
    parser.add_argument(
        "--summary-rounds", type=int, metavar="N", default=1,
        help="摘要處理次數（1-3，多次處理後整合可提升品質，預設 1）")
    parser.add_argument(
        "--diarize", action="store_true",
        help="講者辨識（需搭配 --input；預設 NVIDIA Nemotron，不能用時 resemblyzer + spectralcluster）")
    parser.add_argument(
        "--num-speakers", type=int, metavar="N",
        help="講者人數（需搭配 --diarize）：Nemotron 下是上限、現行方法下是強制分群；不確定就不要填，要填寧可多不要少")
    parser.add_argument(
        "--diarize-engine", choices=_DIARIZE_ENGINES, default="auto",
        help="講者辨識方法：auto（能用 Nemotron 就用，預設）、nemotron、legacy（resemblyzer）")
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
        "--no-srt", action="store_true",
        help="離線處理不產生 SRT 字幕檔")
    parser.add_argument(
        "--no-vtt", action="store_true",
        help="離線處理不產生 VTT 字幕檔")
    parser.add_argument(
        "--restart-server", action="store_true",
        help="強制重啟GPU 伺服器（更新 server.py 後使用）")
    parser.add_argument(
        "--subtitle-overlay", action="store_true",
        help="啟動桌面懸浮字幕覆蓋視窗（需安裝 PyQt6）")
    parser.add_argument(
        "--webui", action="store_true",
        help="同時將即時字幕推送到 WebUI（需另外啟動 webui.py）")
    # 文字轉語音（v2.27.0）：朗讀是主程式的一個模式，字幕、懸浮字幕、WebUI 都沿用既有的
    tts = parser.add_argument_group("文字轉語音（台灣華語朗讀，GPU 伺服器或 Apple Silicon Mac）")
    tts.add_argument("--tts-file", metavar="FILE",
                     help="朗讀文字檔（.txt／.md／.srt／.vtt；本工具的摘要檔只念「重點摘要」）")
    tts.add_argument("--tts-text", metavar="TEXT", help="直接朗讀這段文字")
    tts.add_argument("--tts-voice", metavar="ID", help="聲音 ID（預設用設定裡的預設聲音；--tts-list 列出）")
    tts.add_argument("--tts-rate", type=float, default=1.0, metavar="R",
                     help="語速倍數 0.5～2.0（預設 1.0；音調不變，存成的音訊檔也是這個速度）")
    tts.add_argument("--tts-pause", choices=["short", "normal", "long"], default="normal",
                     help="段落之間停頓：short／normal／long（預設 normal）")
    tts.add_argument("--tts-device", metavar="DEV", default="default",
                     help="播放裝置：default（系統預設）、裝置 ID（--tts-list 列出）、none（不播放，只存檔）")
    tts.add_argument("--tts-save", choices=["mp3", "wav"], metavar="FORMAT",
                     help="同時存成音訊檔（mp3／wav，存在 recordings/）；--tts-device none 時預設 mp3")
    tts.add_argument("--tts-provider", choices=["auto", "remote", "mlx"], metavar="WHERE",
                     help="合成位置：auto（GPU 伺服器優先）、remote（GPU 伺服器）、mlx（Apple Silicon 本機）")
    tts.add_argument("--tts-model", choices=["voxcpm2", "breezyvoice"], metavar="MODEL",
                     help="合成模型：voxcpm2（預設，速度快）、breezyvoice（台灣口音，但合成速度慢，不適合即時；只在 GPU 伺服器）")
    tts.add_argument("--tts-steps", type=int, choices=[6, 10], metavar="N",
                     help="Apple Silicon 本機的擴散步數：6（較快，預設）、10（音質較好）")
    tts.add_argument("--tts-start", type=int, default=1, metavar="N",
                     help="從第 N 段開始念（重念；預設 1）")
    tts.add_argument("--tts-list", action="store_true", help="列出聲音與播放裝置後離開")
    ip = parser.add_argument_group("雙向語音口譯（v2.28.0，英中雙向 --mode en_zh；合成在 GPU 伺服器或 Apple Silicon Mac）")
    ip.add_argument("--speak-me", metavar="DEV", default=None,
                    help="對方說的英文翻成中文後念給我聽：播放裝置（default＝系統預設、裝置編號或名稱的一部分；請用耳機）")
    ip.add_argument("--speak-them", metavar="DEV", default=None,
                    help="我說的中文翻成英文後念給對方聽：輸出到虛擬麥克風，會議軟體的麥克風改選它。macOS 要先安裝 BlackHole 2ch"
                         "（brew install --cask blackhole-2ch）再指定 \"BlackHole\"；Linux 用 auto 自動建立；"
                         "Windows 先安裝 usbip-win2（.\\install.ps1 -InterpMic），再用 auto 自動建立「jt-live-whisper Interpreter Mic」")
    ip.add_argument("--speak-me-voice", metavar="ID", default=None, help="念給我聽的聲音（預設：朗讀的預設聲音）")
    ip.add_argument("--speak-them-voice", metavar="ID", default=None, help="念給對方聽的聲音（預設：內建英文聲音；--tts-list 列出）")
    ip.add_argument("--speak-me-rate", type=float, default=1.0, metavar="R", help="念給我聽的語速（0.5～2，預設 1；不是 1 時不用串流合成）")
    ip.add_argument("--speak-them-rate", type=float, default=1.0, metavar="R", help="念給對方聽的語速（0.5～2，預設 1；不是 1 時不用串流合成）")
    ip.add_argument("--interp-intro", default=None, metavar="TEXT",
                    help="第一次念給對方聽之前先念的開場說明（預設："
                         "\"Hi, I'm using an AI interpreter, so there will be a short delay.\"；none＝不念）")
    ip.add_argument("--passthrough", action="store_true",
                    help="念給對方聽時，你的原聲也同時送進虛擬麥克風（念英文時原聲自動調小）。預設只送英文")
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
    if model_name == BREEZE_MODEL:
        print(f"[錯誤] {BREEZE_MODEL} 沒有 whisper.cpp（ggml）版本，"
              f"請改用 faster-whisper / mlx-whisper 路徑（例如加上 --local-asr）", file=sys.stderr)
        sys.exit(1)
    for name, filename, desc in WHISPER_MODELS:
        if name == model_name:
            path = os.path.join(MODELS_DIR, filename)
            if os.path.isfile(path):
                return name, path
            print(f"[錯誤] 模型檔案不存在: {path}", file=sys.stderr)
            sys.exit(1)
    print(f"[錯誤] 不認識的模型: {model_name}", file=sys.stderr)
    sys.exit(1)


def _parse_llm_host(text, default_port=None):
    """LLM 伺服器位址「主機」「主機:連接埠」「http://主機:連接埠/路徑」→ (host, port, None)；格式不對 → (None, None, 說明)。
    命令列、互動選單、WebUI 都用這一支（以前四個地方各自 rsplit，連接埠打錯就默默改用 11434，
    `http://` 開頭會組成 http://http://…；2026-10-08 有人填 http://10.1.1.35:111434，只得到籠統的「無法連線」）"""
    if default_port is None:
        default_port = OLLAMA_PORT
    s = (text or "").strip()
    if not s:
        return None, None, "未填入主機位址"
    if s.lower().startswith("https://"):
        return None, None, "目前只支援 http，請填「主機:連接埠」（例如 192.168.1.40:11434）"
    if s.lower().startswith("http://"):
        s = s[7:]
    s = s.split("/", 1)[0]                       # 貼上 OpenAI 相容的網址（…/v1）時去掉路徑
    host, port_txt = s, None
    if s.startswith("["):                        # IPv6：[::1]:11434
        end = s.find("]")
        if end < 0:
            return None, None, f"主機位址「{text.strip()}」格式不正確"
        host, rest = s[:end + 1], s[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return None, None, f"主機位址「{text.strip()}」格式不正確"
            port_txt = rest[1:]
    elif s.count(":") == 1:
        host, port_txt = s.split(":")
    elif s.count(":") > 1:
        return None, None, "IPv6 位址請用方括號，例如 [::1]:11434"
    if not host or any(c in host for c in " \t@?#\\"):
        return None, None, f"主機位址「{text.strip()}」格式不正確"
    port = default_port
    if port_txt is not None:
        if not port_txt.isdigit():
            return None, None, f"連接埠「{port_txt}」不是數字（例如 192.168.1.40:11434）"
        port = int(port_txt)
        if not 1 <= port <= 65535:
            return None, None, f"連接埠 {port} 超出範圍（要 1～65535；Ollama 預設 11434）"
    return host, port, None


def _ask_llm_host():
    """互動選單讀一個 LLM 位址：空白回傳 (None, None)；格式不對時說明原因、請使用者重打"""
    while True:
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if not raw:
            return None, None
        host, port, err = _parse_llm_host(raw)
        if not err:
            return host, port
        print(f"  {C_WARN}[格式不對] {err}{RESET}")
        print(f"{C_WHITE}請重新輸入（主機:連接埠），或按 Enter 略過：{RESET}", end=" ")


def _resolve_ollama_host(args):
    """從 args 解析 LLM 伺服器 host/port，無設定時回傳 (None, port)；--llm-host 格式不對時說明並結束"""
    host, port = OLLAMA_HOST, OLLAMA_PORT
    if args.ollama_host:
        host, port, err = _parse_llm_host(args.ollama_host)
        if err:
            print(f"[錯誤] --llm-host {args.ollama_host}：{err}", file=sys.stderr)
            sys.exit(1)
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

    rec_source = kwargs.get("rec_source")
    if rec_source:
        parts.append(f"--rec-source {rec_source}")

    device = kwargs.get("device")
    if device is not None:
        parts.append(f"-d {device}")

    mic_device = kwargs.get("mic_device")
    if mic_device is not None:
        parts.append(f"--mic-device {mic_device}")

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

    for flag in ("speak_me", "speak_me_voice", "speak_them", "speak_them_voice", "interp_intro"):
        v = kwargs.get(flag)
        if v:
            parts.append(f"--{flag.replace('_', '-')} {shlex.quote(str(v))}")
    for flag in ("speak_me_rate", "speak_them_rate"):
        v = kwargs.get(flag)
        if v and abs(float(v) - 1.0) > 1e-6:
            parts.append(f"--{flag.replace('_', '-')} {float(v):g}")
    if kwargs.get("passthrough"):
        parts.append("--passthrough")

    return " ".join(parts)


def _confirm_start(cli_cmd):
    """印出等效 CLI 指令，詢問 Y/n 確認。回傳 True 繼續、False 取消。"""
    print(f"  {C_DIM}等效指令    {RESET}{C_OK}{cli_cmd}{RESET}")
    print(f"  {C_DIM}            （下次可直接執行，不需進入互動選單）{RESET}")
    print(f"{C_DIM}{'─' * 60}{RESET}")
    # 非互動執行（管線、排程、SSH 腳本）沒有人能回答，直接開始
    if not sys.stdin.isatty():
        print(f"\n{C_DIM}非互動執行，直接開始{RESET}")
        return True
    try:
        ans = input(f"\n{C_WHITE}確認開始？({C_HIGHLIGHT}Y{C_WHITE}/n)：{RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans in ("", "y", "yes"):
        return True
    return False


def main():
    global _diarize_engine
    args = parse_args()
    if (args.speak_me or args.speak_them) and (args.mode not in _INTERP_MODES or args.input or args.tts_file or args.tts_text):
        # 只有命令列的英中雙向即時模式會接口譯；其他情況靜靜忽略的話，使用者會以為有在念（畫面選的要真的生效）
        print(f"{C_ERR}[錯誤] --speak-me／--speak-them 要搭配即時的 --mode en_zh（英中雙向）{RESET}", file=sys.stderr)
        sys.exit(1)
    if args.passthrough and not args.speak_them:
        print(f"{C_ERR}[錯誤] --passthrough（同時送出原音）要搭配 --speak-them（念給對方聽的虛擬麥克風）{RESET}", file=sys.stderr)
        sys.exit(1)
    _diarize_engine = args.diarize_engine
    if args.model == QWEN_MODEL and not args.input:
        print(f"{C_HIGHLIGHT}[提示] Qwen3-ASR 只支援離線處理（--input），即時模式改用推薦模型{RESET}")
        args.model = None

    # macOS ScreenCaptureKit：權限授權 / 來源指定
    if getattr(args, "sck_permission", False):
        if not IS_MACOS:
            print("[錯誤] --sck-permission 僅適用於 macOS", file=sys.stderr)
            sys.exit(1)
        if not _sck_macos_ok():
            print(f"{C_HIGHLIGHT}ScreenCaptureKit 需要 macOS 13.0 以上{RESET}")
            sys.exit(1)
        if not _sck_build():
            sys.exit(1)
        if _sck_request_permission():
            print(f"  {C_OK}已取得「螢幕錄製」權限，可直接使用 ScreenCaptureKit 擷取系統音訊{RESET}")
            sys.exit(0)
        _sck_permission_hint()
        sys.exit(1)
    if getattr(args, "audio_source", None) == "blackhole":
        global _SCK_FORCE_OFF
        _SCK_FORCE_OFF = True

    if getattr(args, "tts_list", False):
        _tts_list()
        return
    tts_mode = bool(args.tts_file or args.tts_text is not None)
    cli_mode = (len(sys.argv) > 1 and not args.list_devices
                and args.summarize is None and not args.input)

    # --webui：啟動 event sender（webui.py 由 start.sh 或使用者另外啟動）
    if args.webui:
        _start_webui_sender()
        _start_webui_pause_watch()

    # 字幕轉發、關鍵字通知（從 config.json 讀取設定）：朗讀念的是自己給的文字，不轉發、不通知
    if not tts_mode:
        _init_subtitle_forwarder()
        _init_keyword_monitor()

    # 懸浮字幕覆蓋視窗
    global _overlay_proc_ref
    if getattr(args, 'subtitle_overlay', False):
        _overlay_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subtitle_overlay.py")
        if IS_LINUX and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print(f"  {C_HIGHLIGHT}[懸浮字幕] 此工作階段沒有圖形桌面（DISPLAY 未設定），略過覆蓋視窗{RESET}")
        elif os.path.isfile(_overlay_script):
            # 清除前次殘留的 overlay 程序
            try:
                if IS_WINDOWS:
                    # Windows: 用 PowerShell 找含 subtitle_overlay.py 的程序並 taskkill
                    _tl = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process | Where-Object "
                         "{$_.CommandLine -like '*subtitle_overlay.py*'} | "
                         "Select-Object -ExpandProperty ProcessId"],
                        capture_output=True, text=True, **_SUBPROCESS_FLAGS)
                    for _line in _tl.stdout.strip().splitlines():
                        _pid = _line.strip()
                        if _pid.isdigit():
                            try:
                                subprocess.run(["taskkill", "/F", "/PID", _pid],
                                               capture_output=True, **_SUBPROCESS_FLAGS)
                            except Exception:
                                pass
                else:
                    # macOS / Linux: pkill
                    subprocess.run(["pkill", "-f", "subtitle_overlay.py"],
                                   capture_output=True)
            except Exception:
                pass
            try:
                _overlay_proc = subprocess.Popen(
                    [sys.executable, _overlay_script,
                     "--config", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")],
                    **_SUBPROCESS_FLAGS)
                # 等待 0.5 秒檢查是否立即退出（如 PyQt6 未安裝）
                import time as _t
                _t.sleep(0.5)
                if _overlay_proc.poll() is not None:
                    print(f"  {C_HIGHLIGHT}[懸浮字幕] 啟動失敗（可能未安裝 PyQt6：pip install PyQt6）{RESET}")
                else:
                    _overlay_proc_ref = _overlay_proc
                    import atexit
                    atexit.register(lambda: _overlay_proc_ref.terminate() if _overlay_proc_ref and _overlay_proc_ref.poll() is None else None)
                    print(f"  [懸浮字幕] 已啟動覆蓋視窗（PID {_overlay_proc.pid}）")
            except Exception as _e:
                print(f"  {C_HIGHLIGHT}[懸浮字幕] 啟動失敗: {_e}{RESET}")
        else:
            print(f"  [懸浮字幕] 找不到 subtitle_overlay.py", file=sys.stderr)

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
        elif source in ("tts", "tts_file"):
            _ask_tts(args, source == "tts_file")
            tts_mode = True

    # 文字朗讀／文字轉語音檔（v2.27.0）
    if tts_mode:
        run_tts(args)
        return

    # --input 離線處理音訊檔
    if args.input:
        # 純錄音模式不適用於離線處理
        if args.mode == "record":
            print("[錯誤] 純錄音模式不適用於離線處理（--input）", file=sys.stderr)
            sys.exit(1)
        # 自動偵測雙向配對：若未指定 mode 但輸入檔案符合配對，從檔名推斷模式
        if args.mode is None and _detect_bidi_file_pair(args.input):
            _FNAME_MODE_MAP = {"英中雙向": "en_zh", "日中雙向": "ja_zh", "韓中雙向": "ko_zh"}
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
            fw_model = _enforce_nan_model(mode, fw_model)
            fw_model = _enforce_qwen_model(mode, fw_model, REMOTE_WHISPER_CONFIG if use_remote_whisper else None)
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
            summary_mode = "both" if do_summarize else "correct_only"  # 有 --summarize 才產摘要，否則只校正
            if mode in _NAN_INPUT_MODES:
                _default_fw = BREEZE_MODEL   # 未指定 -m 時不該提示「已忽略指定的模型」
            else:
                _default_fw = "large-v3" if (mode in _NOENG_MODELS and (REMOTE_WHISPER_CONFIG or _has_local_gpu())) else "large-v3-turbo"
            fw_model = _enforce_nan_model(mode, args.model or _default_fw)
            fw_model = _enforce_qwen_model(mode, fw_model, None if args.local_asr else REMOTE_WHISPER_CONFIG)
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

        # Breeze-ASR-26 固定本機辨識：一開始就決定，不必連線、啟動 GPU 伺服器
        if use_remote_whisper and _is_nan_mode(mode):
            use_remote_whisper = False
            print(f"  {C_DIM}[{BREEZE_MODEL}] 使用本機辨識（GPU 伺服器的辨識參數不適用本模型）{RESET}")

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
            ollama_model = DEFAULT_TRANSLATE_MODEL

        # ── 連線檢查 ──
        ollama_available = False
        need_llm_translate = need_translate and engine == "llm"
        need_llm_summary = do_summarize and summary_mode in ("both", "summary")
        # 純轉錄模式：有 LLM 設定時自動校正逐字稿
        need_llm_correct = (not need_translate) and host is not None
        need_remote_asr = use_remote_whisper and REMOTE_WHISPER_CONFIG
        need_check = need_llm_translate or need_llm_summary or need_llm_correct or need_remote_asr

        if need_check:
            print(f"\n\n{C_TITLE}{BOLD}▎ 連線檢查{RESET}")
            print(f"{C_DIM}{'─' * 60}{RESET}")

        if need_llm_translate or need_llm_summary or need_llm_correct:
            if not server_type:
                server_type = _detect_llm_server(host, port)
            if server_type:
                srv_label = "Ollama" if server_type == "ollama" else "OpenAI 相容"
                if need_llm_translate:
                    print(f"  {C_WHITE}LLM 翻譯    {RESET}{C_WHITE}{ollama_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                if need_llm_summary:
                    print(f"  {C_WHITE}LLM 摘要    {RESET}{C_WHITE}{summary_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                if need_llm_correct and not need_llm_translate:
                    print(f"  {C_WHITE}LLM 校正    {RESET}{C_WHITE}{summary_model}{RESET} {C_DIM}@ {host}:{port} ({srv_label}){RESET} {C_OK}✓{RESET}")
                ollama_available = True
            else:
                label = "LLM" if need_llm_translate else ("LLM 校正" if need_llm_correct else "LLM 摘要")
                model_name_display = ollama_model if need_llm_translate else summary_model
                pad = " " * (12 - _str_display_width(label))
                print(f"  {C_WHITE}{label}{pad}{RESET}{C_WHITE}{model_name_display}{RESET} {C_DIM}@ {host}:{port}{RESET} {C_HIGHLIGHT}✗ 無法連接{RESET}")
                _macos_local_network_hint(host)

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
                if mode in ("zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko") or mode in _BIDI_MODES:
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
                _ensure_remote_server_version_once(rw_cfg)
            else:
                print(f"{C_HIGHLIGHT}✗ 無法連接{RESET}")
                print(f"  {C_HIGHLIGHT}[降級] 改用本機 辨識{RESET}")
                _macos_local_network_hint(rw_host)

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
            sp_info = _diar_requested_label()
            if remote_whisper_cfg:
                sp_info += f"，GPU 伺服器（{remote_whisper_cfg.get('host', '?')}）"
            else:
                sp_info += "，本機"
            sp_info += f"，{num_speakers} 人" if num_speakers else "，自動偵測"
            print(f"  {C_WHITE}講者辨識    {sp_info}{RESET}")
        if summary_mode in ("both", "summary") and host:
            print(f"  {C_WHITE}摘要模型    {summary_model} @ {host}:{port}{RESET}")
        if ollama_available and summary_mode in ("both", "correct_only"):
            print(f"  {C_WHITE}LLM 校正    啟用{RESET}")
        if meeting_topic:
            print(f"  {C_WHITE}會議主題    {meeting_topic}{RESET}")
        print(f"  {C_WHITE}檔案數      {RESET}{C_DIM}{len(args.input)}{RESET}")

        # CLI 指令回顯 + 確認（在設定總覽區塊內）
        _cli_kw = dict(input_files=args.input, mode=mode, model=fw_model,
                       diarize=diarize, num_speakers=num_speakers,
                       summarize=(summary_mode in ("both", "summary")),
                       summary_model=summary_model if summary_mode in ("both", "summary") else None,
                       engine=engine if engine in ("argos", "nllb") else None,
                       llm_model=ollama_model if need_translate and engine == "llm" else None,
                       llm_host=f"{host}:{port}" if need_translate and engine == "llm" and host else None,
                       topic=meeting_topic,
                       local_asr=args.local_asr)
        if not _confirm_start(_build_cli_command(**_cli_kw)):
            sys.exit(0)

        # 逐檔處理
        _do_llm_correct = can_summarize and summary_mode in ("both", "correct_only")
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
                _gen_srt = not getattr(args, 'no_srt', False)
                _gen_vtt = not getattr(args, 'no_vtt', False)
                log_path, t_html, session_dir = process_bidi_audio_files(
                    lb_path, mic_path, mode, translator_lb, translator_mic,
                    model_size=fw_model, remote_whisper_cfg=remote_whisper_cfg,
                    diarize=diarize, num_speakers=num_speakers,
                    correct_with_llm=_do_llm_correct,
                    llm_model=summary_model, llm_host=host, llm_port=port,
                    llm_server_type=server_type, meeting_topic=meeting_topic,
                    gen_srt=_gen_srt, gen_vtt=_gen_vtt)
                if log_path:
                    log_paths.append((log_path, lb_path, session_dir))
                if t_html:
                    html_to_open.append(t_html)
            except KeyboardInterrupt:
                pass
        else:
            try:
                _gen_srt = not getattr(args, 'no_srt', False)
                _gen_vtt = not getattr(args, 'no_vtt', False)
                for fpath in args.input:
                    log_path, t_html, session_dir = process_audio_file(fpath, mode, translator, model_size=fw_model,
                                                           diarize=diarize, num_speakers=num_speakers,
                                                           remote_whisper_cfg=remote_whisper_cfg,
                                                           correct_with_llm=_do_llm_correct,
                                                           llm_model=summary_model, llm_host=host, llm_port=port,
                                                           llm_server_type=server_type, meeting_topic=meeting_topic,
                                                           gen_srt=_gen_srt, gen_vtt=_gen_vtt)
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

        # 如果需要摘要且 LLM 伺服器可用，對產生的 log 檔自動摘要（correct_only 不產摘要）
        if log_paths and can_summarize and summary_mode in ("both", "summary"):
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
                    "diarize_engine": _diar_requested_label() if diarize else None,
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

        # 送出所有產出檔案給 WebUI
        _output_files = []
        for lp, orig_fpath, sess_dir in log_paths:
            if sess_dir and os.path.isdir(sess_dir):
                for fname in sorted(os.listdir(sess_dir)):
                    fpath = os.path.join(sess_dir, fname)
                    if os.path.isfile(fpath):
                        # 排除原始音訊副本（太大不需要列出）
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".wma", ".aac", ".opus"):
                            continue
                        rel = os.path.relpath(fpath, os.path.dirname(os.path.abspath(__file__)))
                        _output_files.append({"name": fname, "path": rel})
        # 收集 session 目錄（相對路徑）
        _session_dirs = []
        for _, _, sess_dir in log_paths:
            if sess_dir and os.path.isdir(sess_dir):
                rel = os.path.relpath(sess_dir, os.path.dirname(os.path.abspath(__file__)))
                if rel not in _session_dirs:
                    _session_dirs.append(rel)
        if _output_files or _session_dirs:
            _webui_send({"type": "output_files", "files": _output_files, "dirs": _session_dirs})

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
            # 會議分析（JTDT，v2.25.0）：重點摘要改用它，下面只產生校正逐字稿
            sbar.stop()
            _batch_meeting = _meeting_for_summary(combined_transcript, model, host, port, server_type,
                                                  _batch_topic)
            sbar = _SummaryStatusBar(model=model, task="準備中", location=_llm_loc).start()
            if _batch_meeting is not None:
                _batch_summary_mode = "transcript"
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
                    seg = _s2twp_safe(seg)
                    segment_summaries.append(seg)
                    print(f"  {C_OK}第 {i+1}/{len(chunks)} 段完成{RESET}", flush=True)

            if len(chunks) > 1 and _batch_summary_mode == "transcript":
                # 重點摘要已由會議分析產生：各段校正逐字稿直接接起來，不再合併出一份舊式摘要
                summary = "\n\n".join(
                    re.sub(r"^\s*#{2,4}\s*校正逐字稿\s*\n", "", seg_s).strip() for seg_s in segment_summaries)
            elif len(chunks) > 1:
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
                _retry_result = _s2twp_safe(_retry_result)
                summary = _retry_result.rstrip() + "\n\n" + summary.lstrip()
                print(f"  {C_OK}重點摘要已補上{RESET}")

            # 標題格式修正
            summary = re.sub(r'#{2,4}\s*(?:最終)?(?:重點)?摘要', '## 重點摘要', summary)
            summary = re.sub(r'#{2,4}\s*(?:校正)?逐字稿', '## 校正逐字稿', summary)

            summary = _s2twp_safe(summary)

            # 組裝 metadata（批次摘要只有摘要模型資訊）
            _batch_meta = {
                "summary_model": model,
                "summary_server": f"{srv_label} @ {host}:{port}",
                "input_file": ", ".join(os.path.basename(f) for f in valid_files),
            }
            if _batch_meeting is not None:
                _, _, html_path = _write_meeting_summary(_batch_meeting, summary, output_path,
                                                         valid_files[0] if valid_files else output_path,
                                                         _batch_meta)
            else:
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
        if IS_MACOS:
            print(f"\n\n{C_TITLE}{BOLD}▎ 系統音訊擷取（ScreenCaptureKit）{RESET}")
            _sck_info = _sck_check(build=False) or {}
            if not _sck_macos_ok():
                print(f"  {C_DIM}需要 macOS 13.0 以上，本機為 {_sck_info.get('macos', '未知版本')}{RESET}")
            elif not _sck_info:
                print(f"  {C_DIM}元件尚未編譯（執行 {_INSTALL_CMD} 或首次使用時自動編譯）{RESET}")
            elif _sck_info.get("permission"):
                print(f"  {C_OK}[{SCK_LOOPBACK_ID}] ScreenCaptureKit 系統音訊{RESET}  {C_DIM}48000Hz 2ch，已授權{RESET}")
                print(f"  {C_OK}[{SCK_MIXED_ID}] ScreenCaptureKit + 麥克風混合錄音{RESET}")
            else:
                print(f"  {C_HIGHLIGHT}[{SCK_LOOPBACK_ID}] ScreenCaptureKit 系統音訊（尚未取得螢幕錄製權限）{RESET}")
        if IS_LINUX:
            print(f"\n\n{C_TITLE}{BOLD}▎ 系統音訊擷取（PipeWire / PulseAudio）{RESET}")
            _pinfo = _pulse_monitor_source()
            if _pulse_available():
                print(f"  {C_OK}[{PULSE_LOOPBACK_ID}] {_pulse_label()}{RESET}  "
                      f"{C_DIM}{_pinfo.get('source') or '預設喇叭'}，擷取工具 {_pulse_capture_tool()}{RESET}")
                print(f"  {C_OK}[{PULSE_MIXED_ID}] 系統音訊 + 麥克風混合錄音{RESET}")
            else:
                print(f"  {C_HIGHLIGHT}{_pulse_missing_hint()}{RESET}")
            import sounddevice as _sd_ls
            print(f"\n\n{C_TITLE}{BOLD}▎ 音訊輸入裝置（sounddevice）{RESET}")
            _default_in = _sd_ls.default.device[0]
            for _i, _dev in enumerate(_sd_ls.query_devices()):
                if _dev["max_input_channels"] > 0:
                    _tag = f"  {C_HIGHLIGHT}{REVERSE} 預設 {RESET}" if _i == _default_in else ""
                    print(f"  {C_WHITE}[{_i}] {_dev['name']}{RESET}  "
                          f"{C_DIM}{_dev['max_input_channels']}ch {int(_dev['default_samplerate'])}Hz{RESET}{_tag}")
            sys.exit(0)
        if _MOONSHINE_AVAILABLE:
            print(f"\n\n{C_TITLE}{BOLD}▎ sounddevice 音訊裝置{RESET}")
            list_audio_devices_sd()
        # whisper-stream 裝置
        _probe = next((f for f in (_ggml_model_file(n) for n, _f, _d in WHISPER_MODELS) if f), None)
        model_path_exists = os.path.isfile(WHISPER_STREAM) and not _win_without_whisper_stream() and bool(_probe)
        if model_path_exists:
            model_path = _probe
            print(f"\n\n{C_TITLE}{BOLD}▎ whisper-stream SDL2 音訊裝置{RESET}")
            list_audio_devices(model_path)
        sys.exit(0)

    if cli_mode:
        # CLI 模式：用參數 + 預設值，跳過選單
        mode = args.mode or "en2zh"

        # 純錄音模式：跳過 ASR，直接錄音
        if mode == "record":
            # 照 --rec-source／-d／--mic-device 錄（v2.26.3 以前一律自動偵測，WebUI 選的裝置不生效）
            try:
                rec_id, rec_name, rec_label, rec_lb, rec_mic = _resolve_record_device(
                    args.rec_source, args.device, args.mic_device)
            except SystemExit as e:
                print(e, file=sys.stderr)
                sys.exit(1)
            print(f"{C_OK}錄音裝置: [{rec_id}] {rec_name}（{rec_label}）{RESET}")
            if args.rec_source:
                _cli_kw = dict(mode="record", rec_source=args.rec_source, device=args.device,
                               mic_device=args.mic_device, topic=args.topic)
            else:
                _cli_kw = dict(mode="record", device=rec_id, mic_device=args.mic_device, topic=args.topic)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                sys.exit(0)
            run_record_only(rec_id, topic=args.topic, lb_device=rec_lb, mic_device=rec_mic,
                            channels=1 if rec_label == "僅我方聲音" else None)
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

            bidi = _bidi_apply_device_args(_detect_bidi_devices(), args.device,
                                           getattr(args, 'mic_device', None))
            if bidi is None:
                print("[錯誤] 找不到系統音訊裝置或麥克風", file=sys.stderr)
                sys.exit(1)
            _bidi_lb_id, _bidi_lb_name, _bidi_mic_id, _bidi_mic_name = bidi
            print(f"  {C_OK}系統音訊: {_bidi_lb_name}{RESET}")
            print(f"  {C_OK}麥克風:   {_bidi_mic_name}{RESET}")

            # 模型
            model_name = _enforce_nan_model(mode, args.model or _recommended_whisper_model(mode))
            if model_name.endswith(".en"):
                print(f"[錯誤] 雙向模式不支援 {model_name}（僅英文模型），請用多語言模型", file=sys.stderr)
                sys.exit(1)

            # Apple Silicon + mlx-whisper 自動偵測（依記憶體決定，--asr faster-whisper 可退回）
            _bidi_engine, _ = _recommended_mic_engine(mode, REMOTE_WHISPER_CONFIG)
            _use_mlx_bidi = (_bidi_engine == "mlx") and args.asr != "faster-whisper"

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
            length_ms, step_ms = _nan_adjust_step(mode, length_ms, step_ms)

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
                           denoise=args.denoise, speak_me=args.speak_me, speak_them=args.speak_them,
                           interp_intro=args.interp_intro, passthrough=args.passthrough,
                           speak_me_voice=args.speak_me_voice, speak_them_voice=args.speak_them_voice,
                           speak_me_rate=args.speak_me_rate, speak_them_rate=args.speak_them_rate)
            _interp, _interp_err = _interp_build(args, mode)
            if _interp_err:
                print(f"{C_ERR}[錯誤] 語音口譯：{_interp_err}{RESET}", file=sys.stderr)
                _interp_linux_cleanup()
                sys.exit(1)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                _interp_linux_cleanup()
                sys.exit(0)
            print()
            # 麥克風遠端辨識：有 GPU 伺服器時麥克風也送遠端
            _mic_remote = REMOTE_WHISPER_CONFIG if REMOTE_WHISPER_CONFIG else None
            run_stream_bidirectional(_bidi_lb_id, _bidi_mic_id,
                                     translator_lb, translator_mic,
                                     model_name, mode,
                                     length_ms=length_ms, step_ms=step_ms,
                                     record=args.record,
                                     meeting_topic=meeting_topic,
                                     use_mlx=_use_mlx_bidi,
                                     denoise=args.denoise,
                                     mic_remote_cfg=_mic_remote,
                                     interp=_interp)
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

        # Breeze-ASR-26（台語模式，或華語模式指定 -m breeze-asr-26）只在本機執行：
        # GPU 伺服器套用的是一般模型的參數組，對本模型會大幅劣化
        if args.model or mode in _NAN_INPUT_MODES:
            _enforce_nan_model(mode, args.model or BREEZE_MODEL, quiet=True)
        # GPU 伺服器 Whisper 即時模式（非 Moonshine、非 --local-asr、非 Breeze-ASR-26）
        use_remote_cli = (REMOTE_WHISPER_CONFIG and not args.local_asr
                          and asr_engine != "moonshine" and not _is_nan_mode(mode))
        if REMOTE_WHISPER_CONFIG and not args.local_asr and _is_nan_mode(mode):
            print(f"  {C_DIM}[{BREEZE_MODEL}] 改用本機辨識（GPU 伺服器的辨識參數不適用本模型）{RESET}")
        # --mic + GPU 伺服器：麥克風也送遠端辨識（不再限制）
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
                    if mode in ("zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko"):
                        print(f"[錯誤] 此模式不支援 Argos 離線翻譯，請使用 LLM 伺服器或 NLLB", file=sys.stderr)
                        sys.exit(1)
                    translator = ArgosTranslator()
            else:
                engine = "無（直接轉錄）"

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]
            length_ms, step_ms = _nan_adjust_step(mode, length_ms, step_ms)

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
            # --mic + GPU 伺服器：麥克風也送遠端，走雙路架構
            if args.mic:
                bidi_devs = _detect_bidi_devices()
                if bidi_devs:
                    _mic_lb_id, _, _mic_mic_id, _ = bidi_devs
                    if getattr(args, 'mic_device', None) is not None:
                        _mic_mic_id = args.mic_device
                    print(f"\n{C_DIM}--mic 模式：麥克風辨識也送 GPU 伺服器{RESET}")
                    run_stream_bidirectional(_mic_lb_id, _mic_mic_id,
                                             translator, None,
                                             model_name, mode,
                                             length_ms=length_ms, step_ms=step_ms,
                                             record=args.record,
                                             meeting_topic=meeting_topic,
                                             use_mlx=False,
                                             mic_translate=False,
                                             denoise=args.denoise,
                                             mic_remote_cfg=REMOTE_WHISPER_CONFIG)
                else:
                    print(f"{C_WARN}[警告] 偵測不到麥克風裝置，忽略 --mic{RESET}")
                    run_stream_remote(capture_id, translator, model_name, REMOTE_WHISPER_CONFIG,
                                      mode, length_ms, step_ms,
                                      record=args.record, rec_device=args.rec_device,
                                      force_restart=args.restart_server,
                                      meeting_topic=meeting_topic,
                                      denoise=args.denoise)
            else:
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
            default_model = _enforce_nan_model(mode, args.model or _recommended_whisper_model(mode))
            model_name = default_model
            if mode in _NOENG_MODELS and model_name.endswith(".en"):
                print(f"[錯誤] {mode} 模式不支援 {model_name}（僅英文模型），請用 small、medium、large-v3-turbo 或 large-v3",
                      file=sys.stderr)
                sys.exit(1)

            scene_key = args.scene or "training"
            scene_idx = SCENE_MAP[scene_key]
            _, length_ms, step_ms, _ = SCENE_PRESETS[scene_idx]
            length_ms, step_ms = _nan_adjust_step(mode, length_ms, step_ms)

            # 先判斷是否改用 Python 端本機辨識（在 resolve_model 之前）
            # WASAPI Loopback 與 ScreenCaptureKit 都不是 SDL2 裝置，whisper-stream 讀不到
            # 台語只有 Breeze-ASR-26，whisper.cpp 沒有對應的 ggml 模型，一律走 Python 端
            # Linux 不編譯 whisper.cpp，本機即時辨識一律走 Python 端
            _cli_use_local_fw = _is_nan_mode(mode) or IS_LINUX or _win_without_whisper_stream()
            if _win_without_whisper_stream():
                print(f"{C_DIM}  {_win_whisper_stream_problem()}，即時辨識使用 faster-whisper 本機辨識{RESET}")
            if args.device is not None:
                capture_id = args.device
                if _is_sys_audio_device(capture_id):
                    _cli_use_local_fw = True
            elif _win_without_whisper_stream():
                capture_id = auto_select_device_sd()   # WASAPI Loopback 優先（不經 whisper-stream 列 SDL2 裝置）
            elif IS_MACOS and _sck_available():
                capture_id = auto_select_device_sd()
                _cli_use_local_fw = True
            elif IS_LINUX:
                # Linux 系統音訊走 PipeWire / PulseAudio monitor，不經 whisper-stream（SDL2）
                capture_id = auto_select_device_sd()
                _cli_use_local_fw = True
            elif IS_WINDOWS and _find_wasapi_loopback():
                # 系統音訊走 WASAPI＋faster-whisper；只有 SDL2 有「立體聲混音」而且有這個模型的 ggml 檔時才走 whisper-stream
                _sdl_lb = _win_sdl_loopback_device()
                if _sdl_lb and _ggml_model_file(model_name):
                    capture_id = _sdl_lb[0]
                    print(f"{C_OK}自動選擇音訊裝置: [{_sdl_lb[0]}] {_sdl_lb[1]}{RESET}")
                else:
                    _cli_use_local_fw = True
                    capture_id = auto_select_device_sd()

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
                    if mode in ("zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko"):
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
                    # 麥克風引擎選擇：依記憶體自動決定 mlx GPU / CPU
                    _mic_engine, _mic_rec_model = _recommended_mic_engine(mode, REMOTE_WHISPER_CONFIG)
                    _use_mlx = (_mic_engine == "mlx") and args.asr != "faster-whisper"
                    _mic_model = model_name if _use_mlx else _mic_rec_model
                    _mem_gb = _get_system_memory_gb()
                    if _use_mlx:
                        print(f"\n{C_DIM}--mic 模式：使用 mlx-whisper GPU 加速（{_mic_model}，記憶體 {_mem_gb:.0f}GB）{RESET}")
                    elif not _has_local_gpu():
                        _big_models = ("large-v3-turbo", "large-v3")
                        if model_name in _big_models and _mic_rec_model not in _big_models:
                            print(f"\n{C_WARN}[效能提示] --mic 模式改用 faster-whisper 雙路辨識{RESET}")
                            if _mem_gb and _mem_gb < 16:
                                print(f"  {C_WARN}記憶體 {_mem_gb:.0f}GB 不足，不啟用 mlx GPU 加速{RESET}")
                            print(f"  {C_WARN}自動調整為 {_mic_rec_model}（適合此裝置）{RESET}")
                            _mic_model = _mic_rec_model
                    else:
                        print(f"\n{C_DIM}--mic 模式：ASR 引擎 faster-whisper 雙路辨識{RESET}")
                    _mic_remote = REMOTE_WHISPER_CONFIG if (_mic_engine == "remote") else None
                    run_stream_bidirectional(_mic_lb_id, _mic_mic_id,
                                             translator, None,
                                             _mic_model, mode,
                                             length_ms=length_ms, step_ms=step_ms,
                                             record=args.record,
                                             meeting_topic=meeting_topic,
                                             use_mlx=_use_mlx,
                                             mic_translate=False,
                                             denoise=args.denoise,
                                             mic_remote_cfg=_mic_remote)
                    sys.exit(0)
            if _cli_use_local_fw:
                run_stream_local_whisper(capture_id, translator, model_name, mode,
                                        length_ms=length_ms, step_ms=step_ms,
                                        record=args.record, rec_device=args.rec_device,
                                        meeting_topic=meeting_topic,
                                        denoise=args.denoise,
                                        use_mlx=_local_asr_use_mlx(model_name, args))
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
            bidi = _bidi_apply_device_args(_detect_bidi_devices(), args.device,
                                           getattr(args, 'mic_device', None))
            if bidi is None:
                print(f"{C_ERR}[錯誤] 找不到系統音訊裝置或麥克風{RESET}")
                if IS_MACOS:
                    print(f"  {C_WHITE}請確認已安裝 BlackHole 並設定為系統音訊輸出{RESET}")
                elif IS_WINDOWS:
                    print(f"  {C_WHITE}請確認 WASAPI Loopback 可用且有麥克風{RESET}")
                elif IS_LINUX:
                    print(f"  {C_WHITE}{_pulse_missing_hint() if not _pulse_available() else '請確認有可用的麥克風（arecord -l / pactl list short sources）'}{RESET}")
                sys.exit(1)
            _bidi_lb_id, _bidi_lb_name, _bidi_mic_id, _bidi_mic_name = bidi
            print(f"  {C_OK}系統音訊: {_bidi_lb_name}{RESET}")
            print(f"  {C_OK}麥克風:   {_bidi_mic_name}{RESET}")

            # 強制多語言 faster-whisper 模型
            model_name, _ = select_whisper_model(mode, use_faster_whisper=True)

            # Apple Silicon + mlx-whisper 自動偵測（依記憶體決定）
            _bidi_engine, _ = _recommended_mic_engine(mode, REMOTE_WHISPER_CONFIG)
            _use_mlx_bidi = (_bidi_engine == "mlx")

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
            _ask_interp(args, mode)

            _cli_kw = dict(mode=mode, model=model_name, topic=meeting_topic,
                           engine=engine,
                           llm_model=model if engine == "llm" else None,
                           llm_host=f"{host}:{port}" if engine == "llm" else None,
                           denoise=args.denoise, speak_me=args.speak_me, speak_them=args.speak_them,
                           interp_intro=args.interp_intro, passthrough=args.passthrough,
                           speak_me_voice=args.speak_me_voice, speak_them_voice=args.speak_them_voice,
                           speak_me_rate=args.speak_me_rate, speak_them_rate=args.speak_them_rate)
            _interp, _interp_err = _interp_build(args, mode)
            if _interp_err:
                print(f"{C_ERR}[錯誤] 語音口譯：{_interp_err}{RESET}", file=sys.stderr)
                _interp_linux_cleanup()
                sys.exit(1)
            if not _confirm_start(_build_cli_command(**_cli_kw)):
                _interp_linux_cleanup()
                sys.exit(0)
            print()
            _mic_remote = REMOTE_WHISPER_CONFIG if REMOTE_WHISPER_CONFIG else None
            run_stream_bidirectional(_bidi_lb_id, _bidi_mic_id,
                                     translator_lb, translator_mic,
                                     model_name, mode,
                                     length_ms=length_ms, step_ms=step_ms,
                                     record=record_bidi,
                                     meeting_topic=meeting_topic,
                                     use_mlx=_use_mlx_bidi,
                                     denoise=args.denoise,
                                     mic_remote_cfg=_mic_remote,
                                     interp=_interp)
            sys.exit(0)

        # 轉錄模式：提前詢問麥克風轉錄（影響辨識位置預設值）
        _early_mic = False
        _early_bidi_devs = None
        if mode in ("en", "zh", "ja", "ko"):
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
        if REMOTE_WHISPER_CONFIG and mode in _NAN_INPUT_MODES:
            print(f"  {C_OK}→ 辨識位置自動設為「本機」（台語模型 Breeze-ASR-26 只在本機執行）{RESET}")
        elif REMOTE_WHISPER_CONFIG:
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
                    if mode in ("zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko"):
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

            # whisper.cpp (SDL2) 讀不到 WASAPI Loopback / ScreenCaptureKit，標記改走 Python 端辨識
            _use_local_fw = False
            if IS_MACOS and asr_engine == "whisper" and _sck_available():
                _use_local_fw = True  # 改用 ScreenCaptureKit + mlx/faster-whisper
                _sck_engine_label = ("mlx-whisper GPU" if (_is_apple_silicon() and _has_mlx_whisper())
                                     else "faster-whisper")
                print(f"\n{C_DIM}  系統音訊來源為 ScreenCaptureKit，將改用 {_sck_engine_label} 本機辨識{RESET}")
            if IS_LINUX and asr_engine == "whisper":
                _use_local_fw = True  # Linux：PipeWire / PulseAudio + faster-whisper
                print(f"\n{C_DIM}  Linux 本機辨識使用 faster-whisper"
                      f"{'（CUDA）' if _fw_local_cuda_ok() else '（CPU）'}{RESET}")
            if IS_WINDOWS and asr_engine == "whisper" and _win_without_whisper_stream():
                _use_local_fw = True  # whisper-stream 不能用（沒有 whisper.cpp、缺 SDL2.dll）：一律 faster-whisper
                print(f"\n{C_DIM}  {_win_whisper_stream_problem()}，即時辨識使用 faster-whisper 本機辨識{RESET}")
            elif IS_WINDOWS and asr_engine == "whisper" and _find_wasapi_loopback():
                # SDL2 沒有「立體聲混音」這類裝置時 whisper-stream 只錄得到麥克風 → 改用 WASAPI＋faster-whisper
                if not _win_sdl_loopback_device():
                    _use_local_fw = True
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
                if _is_nan_mode(mode):
                    _use_local_fw = True  # Breeze-ASR-26 沒有 ggml 版，whisper-stream 跑不了
                length_ms, step_ms = select_scene()
                length_ms, step_ms = _nan_adjust_step(mode, length_ms, step_ms)

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
                    if mode in ("zh2en", "ja2zh", "zh2ja", "ko2zh", "zh2ko"):
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
            elif mode not in _BIDI_MODES and mode not in ("record", "en", "zh", "ja", "ko") and asr_engine == "whisper":
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
                    # 麥克風引擎選擇：依記憶體自動決定 mlx GPU / CPU
                    _mic_engine, _mic_rec_model = _recommended_mic_engine(mode, REMOTE_WHISPER_CONFIG)
                    _use_mlx_mic = (_mic_engine == "mlx")
                    _mic_model = model_name if _use_mlx_mic else _mic_rec_model
                    _mem_gb = _get_system_memory_gb()
                    if _use_mlx_mic:
                        print(f"\n{C_DIM}麥克風轉錄：使用 mlx-whisper GPU 加速（{_mic_model}，記憶體 {_mem_gb:.0f}GB）{RESET}")
                    elif not _has_local_gpu():
                        _big_models = ("large-v3-turbo", "large-v3")
                        if model_name in _big_models and _mic_rec_model not in _big_models:
                            print(f"\n{C_WARN}[效能提示] 麥克風轉錄改用 faster-whisper 雙路辨識{RESET}")
                            if _mem_gb and _mem_gb < 16:
                                print(f"  {C_WARN}記憶體 {_mem_gb:.0f}GB，不啟用 mlx GPU 加速{RESET}")
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
                    _mic_remote = REMOTE_WHISPER_CONFIG if (_mic_engine == "remote") else None
                    run_stream_bidirectional(_mic_lb_id, _mic_mic_id,
                                             translator, None,
                                             _mic_model, mode,
                                             length_ms=length_ms, step_ms=step_ms,
                                             record=record,
                                             meeting_topic=meeting_topic,
                                             use_mlx=_use_mlx_mic,
                                             mic_translate=False,
                                             denoise=args.denoise,
                                             mic_remote_cfg=_mic_remote)
                elif _use_local_fw:
                    # WASAPI Loopback / ScreenCaptureKit + 本機辨識（mlx 或 faster-whisper）
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
                                            denoise=args.denoise,
                                            use_mlx=_local_asr_use_mlx(model_name, args))
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
