#!/usr/bin/env python3
"""
jt-live-whisper 伺服器 Whisper ASR 伺服器
部署到 GPU 伺服器，提供 REST API 讓本機上傳音訊檔進行語音辨識。

後端引擎自動偵測：
  1. faster-whisper (CTranslate2 CUDA) — x86_64 GPU，速度最快
  2. openai-whisper (PyTorch CUDA) — aarch64 GPU（如 DGX Spark），也能 GPU 加速
  3. faster-whisper (CPU) — 無 GPU 降級

依賴：faster-whisper, fastapi, uvicorn, python-multipart
      （aarch64 無 CTranslate2 CUDA 時額外需要 openai-whisper）
      （講者辨識需額外安裝 resemblyzer, spectralcluster）
啟動：python3 server.py [--port 8978] [--host 0.0.0.0]

Author: Jason Cheng (Jason Tools)
"""

import argparse
import asyncio
import json
import math
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time


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
    "Qwen3-ASR 的 venv-qwen 要照手冊「Qwen3-ASR（實驗）」重建" if "--qwen-worker" in sys.argv else
    "文字轉語音的 venv-tts 要重建：在用戶端重新執行安裝程式，設定 GPU 伺服器的文字轉語音" if "--tts-worker" in sys.argv else
    "請在用戶端重新執行安裝程式（./install.sh 或 install.ps1），檢查 GPU 伺服器時選擇修復：會重建伺服器的 venv")

# ── Qwen3-ASR worker（v2.23.0，實驗）──────────────────────────────
# vLLM 0.14 鎖 torch 2.9.1，這支服務的 venv 是 torch 2.10 → Qwen 必須在**獨立 venv 的子行程**跑。
# 伺服器自動更新只推 server.py 一個檔案，所以 worker 也寫在這裡，以 `--qwen-worker <port>` 啟動；
# 放在所有第三方 import 之前，worker 的 venv 不需要有這支服務的其他套件。
# 實測（2026-09-25，tools/asr_bench/）：中文 20 場會議 CER 28.78% → 15.75%；中英夾雜少數語言召回 2~3 倍
QWEN_ASR_MODEL = "Qwen/Qwen3-ASR-0.6B"
QWEN_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
QWEN_GPU_MEM = 0.06     # E6：0.06 可跑（行程 5.4 GB），0.04 以下起不來；共用機不要給多
# 一次送幾個窗：vLLM 的佔用會隨批次長大（2026-09-26 正式機 37 分鐘中文會議：32 → 49 秒、合計 12.3 GB；
# 16 → 74 秒、8.9 GB）。共用 GPU 預設 16，顯示記憶體寬裕可設 JT_QWEN_BATCH=32
QWEN_BATCH = max(1, int(os.environ.get("JT_QWEN_BATCH") or 16))


def _qwen_worker_main():
    """只聽 127.0.0.1。POST /transcribe {path, windows, language} → {texts, stamps}（每窗文字＋對齊器逐字時間）"""
    import http.server
    import signal
    port = int(sys.argv[sys.argv.index("--qwen-worker") + 1])
    parent = os.getppid()

    def _die():
        try:
            os.killpg(0, signal.SIGKILL)       # 連 vLLM 的 EngineCore 子行程一起收掉，不留孤兒佔 GPU
        finally:
            os._exit(0)

    def _watch():
        while True:
            time.sleep(5)
            if os.getppid() != parent:          # 主服務結束了
                _die()
    threading.Thread(target=_watch, daemon=True).start()
    state = {"ready": False}
    lock = threading.Lock()

    def work(req):
        wav, _ = librosa.load(req["path"], sr=16000, mono=True)
        chunks = [wav[max(0, int(a * 16000)):int(b * 16000)] for a, b in req["windows"]]
        lang = req["language"]
        texts = [""] * len(chunks)
        # 極短的窗（<0.2 秒）不送模型：沒有內容可辨識，還可能讓前處理出錯
        live = [k for k, c in enumerate(chunks) if len(c) >= 3200]
        for k0 in range(0, len(live), QWEN_BATCH):
            ks = live[k0:k0 + QWEN_BATCH]
            r = asr.transcribe(audio=[(chunks[k], 16000) for k in ks], language=[lang] * len(ks))
            for k, x in zip(ks, r):
                texts[k] = x.text
        stamps = [[] for _ in texts]
        idx = [k for k, t in enumerate(texts) if t.strip()]
        align_failed = 0
        for k0 in range(0, len(idx), 8):
            ks = idx[k0:k0 + 8]
            try:
                r = fa.align(audio=[(chunks[k], 16000) for k in ks], text=[texts[k] for k in ks],
                             language=[lang] * len(ks))
            except Exception as e:           # 對齊失敗：這幾窗沒有逐字時間，文字照樣回（主服務切句時不丟字）
                align_failed += len(ks)
                print(f"[qwen-worker] 對齊失敗 {len(ks)} 窗：{type(e).__name__}: {e}", flush=True)
                continue
            for k, xs in zip(ks, r):
                stamps[k] = [[x.text, float(x.start_time), float(x.end_time)] for x in xs]
        return {"texts": texts, "stamps": stamps, "align_failed": align_failed}

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            # 載入中回 503：主服務只把 200 當成就緒
            self._send(200, {"ok": True, "model": QWEN_ASR_MODEL}) if state["ready"] \
                else self._send(503, {"ok": False, "loading": True})

        def do_POST(self):
            if self.path != "/transcribe":
                return self._send(404, {"error": "not found"})
            if not state["ready"]:
                return self._send(503, {"error": "Qwen3-ASR worker 載入中"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with lock:                      # 一次一件（主服務本來就排隊，這裡是保險）
                    try:
                        out = work(req)
                    finally:
                        # 對齊器的暫存不會自己還：正式機處理一個 7 分鐘檔後由 7.1 GB 漲到 10.5 GB（2026-09-26），
                        # 共用 GPU 上要還回去
                        _torch.cuda.empty_cache()
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(200, out)

    # **先綁埠號再載入模型**（約 7 GB、1~3 分鐘）：同一個埠已有 worker 時這裡立刻失敗退出，
    # 不會白白載一份模型；主服務在載入期間也看得出埠被佔（2026-09-26 實測：原本載完才綁，兩個 worker 同時載入）
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import librosa
    import torch as _torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
    asr = Qwen3ASRModel.LLM(model=QWEN_ASR_MODEL, gpu_memory_utilization=QWEN_GPU_MEM, max_model_len=4096,
                            max_inference_batch_size=QWEN_BATCH, max_new_tokens=512)
    fa = Qwen3ForcedAligner.from_pretrained(QWEN_ALIGNER_MODEL, dtype=_torch.bfloat16, device_map="cuda")
    state["ready"] = True
    print(f"[qwen-worker] 就緒 127.0.0.1:{port}（{QWEN_ASR_MODEL}）", flush=True)
    threading.Event().wait()


if __name__ == "__main__" and "--qwen-worker" in sys.argv:
    _qwen_worker_main()
    sys.exit(0)


# ── 文字轉語音（VoxCPM2，2026-10）──────────────────────────────
# 規格與實測：specs/2026-10-08_TTS開發規格_v2.md。VoxCPM2 要 CUDA 13 版 torch（2.11.0+cu130，GB10 的 sm_121），
# 跟這支服務的 venv 不同 → 跟 Qwen 一樣在**獨立 venv（venv-tts）的子行程**跑，以 `--tts-worker <port>` 啟動。
# 常駐約 9.5 GB（行程 3.6＋顯示卡 5.8，2026-10-09 實測），共用 GPU 不常駐：第一次用到才啟動、閒置 JT_TTS_IDLE 秒（預設 30 分鐘）關閉。
# 台灣念法（教育部辭典＋g2pW＋自訂，以 {拼音} 交給模型）也在 worker 裡做，用戶端不必裝 g2pW 與辭典。
# 下面 _tts_ 開頭的純函式與 jtlw_tts/tw_reading.py 逐字相同（Mac 本機合成用那一份；測試比對兩邊）。
TTS_MODEL = "openbmb/VoxCPM2"
TTS_MODEL_REV = "32279effe8c19989596f05d353d1447f51d9e915"
TTS_DIR = os.path.expanduser(os.environ.get("JT_TTS_DIR") or "~/jt-whisper-server/tts")
_TTS_HAN = re.compile(r"[㐀-鿿]+")
_TTS_TONE = {"ˊ": "2", "ˇ": "3", "ˋ": "4"}
_TTS_NO_HINT = frozenset("一不")         # 會變調：辭典標本調，硬加提示反而念錯
# **念法以台灣日常說法為準**（2026-10-09 使用者：「萌典不要以他為準，請以台灣日常為準」）：教育部辭典只是基礎，
# 跟台灣日常說法不同的字，不管在哪個詞裡都改用日常說法（自訂發音照樣優先）。值：(要換掉的念法，None＝一律換, 換成)。
# 液、亞、俄：教育部 ㄧㄝˋ／ㄧㄚˋ／ㄜˊ 跟大陸相同，台灣多念 ㄧˋ／ㄧㄚˇ／ㄜˋ；黑：ㄏㄜˋ 是讀音，辭典 265 個含黑的詞只有 5 個用它；
# 熟（成熟、熟悉）ㄕㄨˊ→ㄕㄡˊ、癌 ㄧㄢˊ→ㄞˊ、它（它們）ㄊㄨㄛ→ㄊㄚ、洽（接洽）ㄒㄧㄚˊ→ㄑㄧㄚˋ、燥（肉燥）ㄙㄠˋ→ㄗㄠˋ、
# 括（包括）ㄎㄨㄛˋ→ㄍㄨㄚ、魄（落魄）ㄊㄨㄛˋ→ㄆㄛˋ；
# 語料裡「因辭典而指定、跟模型預設不同」的 220 種逐一檢查、使用者試聽後再加（2026-10-09）：場（市場、現場、一場）ㄔㄤˊ→ㄔㄤˇ、
# 妨（無妨）ㄈㄤ→ㄈㄤˊ、縱（縱貫、縱谷）ㄗㄨㄥ→ㄗㄨㄥˋ、多（多麼）ㄉㄨㄛˊ→ㄉㄨㄛ、擷（擷取）ㄐㄧㄝˊ→ㄒㄧㄝˊ、
# 伐（步伐）ㄈㄚ→ㄈㄚˊ、玩（把玩）ㄨㄢˋ→ㄨㄢˊ、署（簽署、部署）ㄕㄨˋ→ㄕㄨˇ。
# 使用者試聽決定照教育部的（不要改）：蝸牛 ㄍㄨㄚ、優酪乳 ㄌㄨㄛˋ、從容 ㄘㄨㄥ、剝皮 ㄅㄛ、曝光 ㄆㄨˋ、說服 ㄕㄨㄟˋ、寂寞 ㄐㄧˊ、艘 ㄙㄠ、
# 盡快／盡量 ㄐㄧㄣˋ、言行 ㄒㄧㄥˋ
_TTS_TW_COMMON = {"液": (None, "ㄧ4"), "亞": (None, "ㄧㄚ3"), "俄": (None, "ㄜ4"), "黑": ("ㄏㄜ4", "ㄏㄟ1"),
                  "熟": ("ㄕㄨ2", "ㄕㄡ2"), "癌": ("ㄧㄢ2", "ㄞ2"), "它": ("ㄊㄨㄛ1", "ㄊㄚ1"), "洽": ("ㄒㄧㄚ2", "ㄑㄧㄚ4"),
                  "燥": ("ㄙㄠ4", "ㄗㄠ4"), "括": ("ㄎㄨㄛ4", "ㄍㄨㄚ1"), "魄": ("ㄊㄨㄛ4", "ㄆㄛ4"),
                  "場": (None, "ㄔㄤ3"), "妨": (None, "ㄈㄤ2"), "縱": (None, "ㄗㄨㄥ4"), "多": ("ㄉㄨㄛ2", "ㄉㄨㄛ1"),
                  "擷": (None, "ㄒㄧㄝ2"), "伐": ("ㄈㄚ1", "ㄈㄚ2"), "玩": ("ㄨㄢ4", "ㄨㄢ2"), "署": (None, "ㄕㄨ3")}
# 台灣日常念法（詞）：比辭典優先、自訂發音照樣更優先。角色（教育部主音 ㄐㄩㄝˊ）、暖暖（基隆的暖暖區；教育部 ㄒㄩㄢ）、
# 著急（教育部 ㄓㄠ）、裝載（教育部 ㄗㄞˋ，使用者：要念三聲）、兒子（教育部 ㄗˇ，日常輕聲）、
# 強制（教育部 ㄑㄧㄤˇ）、牛仔（教育部 ㄗˇ）、折返（教育部 ㄓㄜ）、胜肽（教育部 ㄒㄧㄥ）、
# 參與（教育部 ㄩˋ）、罪行（教育部 ㄒㄧㄥˋ）、記載（教育部 ㄗㄞˋ）：使用者試聽決定（言行照教育部 ㄒㄧㄥˋ）、
# 丁丁（教育部是伐木聲 ㄓㄥ）、家樂福、麥當當、亂數；挑戰、慎重：辭典由左往右會切出「大挑」「重考」
# （「強行」不加：會把「加強行員」切成強行）
_TTS_TW_WORDS = {"角色": ["ㄐㄧㄠ3", "ㄙㄜ4"], "主角": ["ㄓㄨ3", "ㄐㄧㄠ3"], "配角": ["ㄆㄟ4", "ㄐㄧㄠ3"],
                 "暖暖": ["ㄋㄨㄢ3", "ㄋㄨㄢ3"], "著急": ["ㄓㄠ2", "ㄐㄧ2"], "裝載": ["ㄓㄨㄤ1", "ㄗㄞ3"], "兒子": ["ㄦ2", "ㄗ5"],
                 "目的事業": ["ㄇㄨ4", "ㄉㄧ4", "ㄕ4", "ㄧㄝ4"],
                 "強制": ["ㄑㄧㄤ2", "ㄓ4"], "牛仔": ["ㄋㄧㄡ2", "ㄗㄞ3"], "折返": ["ㄓㄜ2", "ㄈㄢ3"],
                 "參與": ["ㄘㄢ1", "ㄩ3"], "罪行": ["ㄗㄨㄟ4", "ㄒㄧㄥ2"], "記載": ["ㄐㄧ4", "ㄗㄞ3"],
                 "胜肽": ["ㄕㄥ4", "ㄊㄞ4"], "丁丁": ["ㄉㄧㄥ1", "ㄉㄧㄥ1"], "家樂福": ["ㄐㄧㄚ1", "ㄌㄜ4", "ㄈㄨ2"],
                 "麥當當": ["ㄇㄞ4", "ㄉㄤ1", "ㄉㄤ1"], "亂數": ["ㄌㄨㄢ4", "ㄕㄨ4"], "挑戰": ["ㄊㄧㄠ3", "ㄓㄢ4"],
                 "慎重": ["ㄕㄣ4", "ㄓㄨㄥ4"]}
# 不在辭典詞裡的字（念法是 g2pW 猜的）改用這個念法，值：(要換掉的念法，None＝一律換, 換成)（2026-10-09 自動偵測）。
# 蘋：蘋概股、蘋粉的蘋都是蘋果的蘋（g2pW 猜 ㄆㄧㄣˊ；辭典裡念 ㄆㄧㄣˊ 的白蘋、蘋婆照辭典）。
# 差：g2pW 把很差、太差、變差都判成 ㄔㄚ；教育部「不好、欠缺」念 ㄔㄚˋ，ㄔㄚˋ 也是 ㄔㄚ 的語音（差別、差距、誤差是辭典詞，照辭典）
# 兒：g2pW 把兒化（那兒、鳥兒、好玩兒）標成 ㄦ 一聲，教育部是輕聲 ˙ㄦ（輕聲不加提示，模型自己念兒化）
_TTS_TW_SINGLE = {"蘋": (None, "ㄆㄧㄥ2"), "差": ("ㄔㄚ1", "ㄔㄚ4"), "兒": ("ㄦ1", "ㄦ5")}
# 異體字：辭典查不到時換成辭典用的字再查（沈積→沉積 ㄔㄣˊ，g2pW 判成姓氏的 ㄕㄣˇ；什麽→什麼）。
# 姓氏的沈（沈約、沈括）辭典本來就查得到，不換
_TTS_VARIANT = str.maketrans("沈麽", "沉麼")
# 台灣念法跟模型預設一樣、模型卻還是會念錯的字：一律加念法提示（2026-10-09 試聽：命脈的脈、協會的協念錯；
# 自動偵測：阿嬤的嬤念成ㄇㄛˊ；一曝十寒的曝念成大陸「曝光」的ㄅㄠˋ，教育部只有ㄆㄨˋ）
_TTS_ALWAYS_HINT = frozenset("脈協嬤曝")
# 繁體一個字、簡體依念法分成兩個字，OpenCC 分不出來的：念法不是第一個就寫成第二個字再送進模型
# （扮演著：簡體的「著」只念ㄓㄨˋ，模型照著念；助詞與著急、著陸在簡體寫「着」，2026-10-09 自動偵測）
_TTS_SIMP_BY_READING = {"著": ("ㄓㄨ4", "着")}
# 辭典由左往右找最長的詞會切錯：「扮演著重要」切出「著重」（ㄓㄨㄛˊ）、「組中的字」切出「中的」（射中靶心 ㄓㄨㄥˋ ㄉㄧˋ）、
# 「他的是不是」切出文言「的是」（ㄉㄧˊ）、「都會忘記」切出「都會」（都市）、「環境和文化」切出「和文」、「生存沒有」切出「存沒」。
# 這些常用字在辭典詞裡的念法跟 g2pW（看上下文）不同時，那個詞多半是切錯的 → 不採用，改試短一點的詞或照 g2pW
# （2026-10-09 用 Common Voice 4,636 句比對：改到 111 處，只有公文的「目的事業」改壞，另列在 _TTS_TW_WORDS）。
# 再加種分間得要當重（一種生物≠種生、多分布≠多分、之間有≠間有、找得到≠得到、要不要≠不要、當晚餐≠當晚、很多重要≠多重）：
# 改到 34 處、改壞 5 處（慎重另列在 _TTS_TW_WORDS；鹽分、當名嘴、當日、才會得是 g2pW 判錯）
_TTS_G2P_FIRST = frozenset("的了著都和沒給從參覺會種分間得要當重")
# 送進模型前把沒加提示的字轉成簡體（見 _tts_spoken）
_TTS_SIMPLIFIED = True
_TTS_MAX_WORD = 8
_TTS_SENT_END = "。！？!?；;\n"


def _tts_syl(b):
    """教育部注音（ㄌㄜˋ、˙ㄇㄣ、ㄒㄧ）→ 注音＋聲調數字（ㄌㄜ4、ㄇㄣ5、ㄒㄧ1），與 g2pW 的格式相同"""
    if b.startswith("˙"):
        return b[1:] + "5"
    if b and b[-1] in _TTS_TONE:
        return b[:-1] + _TTS_TONE[b[-1]]
    return b + "1"


def _tts_moe_lines(entries):
    """教育部《重編國語辭典修訂本》（g0v/moedict-data 的 dict-revised.json 解析後的 list）→ 精簡對照檔的行：
    「詞<TAB>讀音|讀音」，讀音以空白分字。只留 2～8 個漢字的詞；同一詞的多個讀音全部保留（大家：ㄐㄧㄚ／ㄍㄨ）"""
    rows = {}
    for e in entries:
        t = e.get("title", "")
        if not (2 <= len(t) <= _TTS_MAX_WORD) or not _TTS_HAN.fullmatch(t):
            continue
        for h in e.get("heteronyms") or []:
            b = re.split(r"[（(]", h.get("bopomofo") or "")[0].strip()   # 「（讀音）……（語音）」取第一個
            syl = [x for x in re.split(r"[\s　]+", b) if x]
            if len(syl) == len(t):
                r = " ".join(_tts_syl(x) for x in syl)
                if r not in rows.setdefault(t, []):
                    rows[t].append(r)
    return [t + "\t" + "|".join(rs) for t, rs in rows.items() if rs]


def _tts_moe_load(lines):
    words = {}
    for ln in lines:
        t, _, rs = ln.rstrip("\n").partition("\t")
        if t and rs:
            words[t] = [r.split() for r in rs.split("|")]
    return words


def _tts_custom(entries):
    """自訂字典 {"和": "ㄏㄢˋ", "垃圾": "ㄌㄜˋ ㄙㄜˋ"} → {詞: [注音＋數字…]}；字數與讀音數不合的回傳在 bad"""
    good, bad = {}, []
    for w, v in (entries or {}).items():
        syl = [x for x in re.split(r"[\s　]+", str(v).strip()) if x]
        if w and _TTS_HAN.fullmatch(w) and len(syl) == len(w):
            good[w] = [_tts_syl(x) for x in syl]
        else:
            bad.append(w)
    return good, bad


def _tts_score(cand, ctx):
    """注音＋聲調都對 2 分、只有注音對 1 分：字音（便 ㄆㄧㄢ／ㄅㄧㄢ）比輕聲與否重要"""
    return sum(2 if a == b else (1 if b and a[:-1] == b[:-1] else 0) for a, b in zip(cand, ctx))


def _tts_overlay(text, tw, moe, custom):
    """每個字的台灣念法：自訂（詞或單字）＞台灣常用念法（_TTS_TW_COMMON，詞裡也換）＞教育部辭典的詞（最長比對；多個讀音挑跟 g2pW 最接近的，
    一樣接近取辭典的第一個）＞g2pW（tw 傳入的就是 g2pW 的結果）。單字的自訂只用在沒被詞涵蓋的字（和平的和照辭典）。
    全由數字字組成的辭典詞不比對：那些是專名或成語（「五百」是古代職官，念ㄨˇ ㄅㄛˊ），數字照 g2pW（2026-10-09 實測三千五百元被念成五{bo2}）"""
    tw = list(tw)
    g2p = list(tw)
    fixed = set()                                    # 自訂發音給的位置：辭典詞、台灣常用念法都不蓋掉它
    words = {**_TTS_TW_WORDS, **{w: r for w, r in custom.items() if len(w) > 1}}
    # 先套自訂的詞：自訂比辭典優先，就算辭典有更長的詞（自訂「液化」、辭典有「液化石油氣」，2026-10-09）
    for m in _TTS_HAN.finditer(text):
        i, end = m.start(), m.end()
        while i < end:
            for n in range(min(max([0] + [len(w) for w in words]), end - i), 1, -1):
                if text[i:i + n] in words:
                    tw[i:i + n] = words[text[i:i + n]]
                    fixed.update(range(i, i + n))
                    i += n
                    break
            else:
                i += 1
    for m in _TTS_HAN.finditer(text):
        i, end = m.start(), m.end()
        while i < end:
            if i in fixed:
                i += 1
                continue
            for n in range(min(_TTS_MAX_WORD, end - i), 1, -1):
                w = text[i:i + n]
                key = w if w in moe else w.translate(_TTS_VARIANT)
                if key in moe and w.strip(_TTS_NUM_HAN) and not fixed.intersection(range(i, i + n)):
                    cands = moe[key]
                    ctx = tw[i:i + n]
                    best = max(cands, key=lambda c: (_tts_score(c, ctx), -cands.index(c)))
                    if any(g2p[i + k] and text[i + k] in _TTS_G2P_FIRST and best[k] != g2p[i + k] for k in range(n)):
                        continue                         # 切錯了：試短一點的詞
                    tw[i:i + n] = best
                    i += n
                    break
            else:
                if text[i] in custom:
                    tw[i] = custom[text[i]][0]
                    fixed.add(i)
                elif text[i] in _TTS_TW_SINGLE and tw[i] and _TTS_TW_SINGLE[text[i]][0] in (None, tw[i]):
                    tw[i] = _TTS_TW_SINGLE[text[i]][1]
                i += 1
    for k, ch in enumerate(text):
        rule = _TTS_TW_COMMON.get(ch)
        if rule and k not in fixed and tw[k] and (rule[0] is None or tw[k] == rule[0]):
            tw[k] = rule[1]
    return tw


def _tts_hint(text, tw, cn, to_pinyin, force=()):
    """台灣念法（tw）與模型預設會念的（cn，pypinyin 的大陸念法）不同的字換成 {拼音}；
    一、不、台灣念輕聲的不換。to_pinyin：注音＋數字 → 拼音＋數字（如 ㄌㄜ4 → le4），換不出來回 None。
    force：一定要換的位置（模型詞彙裡沒有的字，見 _tts_vocab_chars）；g2pW 沒給念法時用 pypinyin 的"""
    out = []
    for k, (ch, t, c) in enumerate(zip(text, tw, cn)):
        if k in force and not t:
            t = c
        if t and (c and (t != c or ch in _TTS_ALWAYS_HINT) or k in force) and ch not in _TTS_NO_HINT and not t.endswith("5"):
            py = to_pinyin(t)
            if py:
                out.append("{" + py + "}")
                continue
        out.append(ch)
    return "".join(out)


_TTS_CNUM = "零一二三四五六七八九"
_TTS_NUM_HAN = "零〇一二三四五六七八九十百千萬億兆兩"
_TTS_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# 模型自己念數字（文字正規化關閉），2026-10-09 GPU 實測各 3 次：千分位逗號 3/3 亂念、負號 3/3 被吞掉、
# NT$ 念成美元；日期、時間、IP、版本號、電話、小數、百分比、分數都對 → 只改念錯的這三種，其他不動
_TTS_MONEY = (
    (re.compile(r"(?:NT|NTD)\$\s*(" + _TTS_NUM + r")(?:\s*元)?"), r"新台幣\1元"),
    (re.compile(r"(?:US|USD)\$\s*(" + _TTS_NUM + r")"), r"\1美元"),
    (re.compile(r"(?<![A-Za-z])\$\s*(" + _TTS_NUM + r")"), r"\1美元"),
    (re.compile(r"€\s*(" + _TTS_NUM + r")"), r"\1歐元"),
    (re.compile(r"£\s*(" + _TTS_NUM + r")"), r"\1英鎊"),
)
_TTS_TEMP = (
    (re.compile(r"(?<![A-Za-z0-9_.])[-−](\d+(?:\.\d+)?)\s*(?:°C|℃)"), r"零下\1度"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:°C|℃)"), r"\1度"),
    (re.compile(r"(?<![A-Za-z0-9_.])[-−](\d+(?:\.\d+)?)\s*(?:°F|℉)"), r"華氏零下\1度"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:°F|℉)"), r"華氏\1度"),
)
# 前面是英數字、小數點、斜線、冒號等就不是負號（2026-10-09、02-2345-6789、A-1、3-5 天）
_TTS_NEG = re.compile(r"(?<![A-Za-z0-9_.,/:\-−+])[-−](?=\d)")
_TTS_COMMA_NUM = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(\.\d+)?(?!\d|,\d)")


def _tts_cn_sec(x, leading):
    """1～9999 → 國字；2 在千、百前念「兩」；開頭的十幾不說「一十」"""
    out, zero, started = "", False, False
    for d, u in zip((x // 1000, x // 100 % 10, x // 10 % 10, x % 10), ("千", "百", "十", "")):
        if d == 0:
            zero = zero or started
            continue
        if zero:
            out += "零"
            zero = False
        ch = "兩" if d == 2 and u in ("千", "百") else _TTS_CNUM[d]
        if d == 1 and u == "十" and not started and leading:
            ch = ""
        out += ch + u
        started = True
    return out


def _tts_cn_int(n):
    """整數 → 國字（台灣說法）：1250000 → 一百二十五萬、10005 → 一萬零五、20000 → 兩萬"""
    if n == 0:
        return "零"
    secs = []
    while n:
        secs.append(n % 10000)
        n //= 10000
    out, gap = "", False
    for i in range(len(secs) - 1, -1, -1):
        sec = secs[i]
        if sec == 0:
            gap = gap or bool(out)
            continue
        if out and (gap or sec < 1000):
            out += "零"
        out += ("兩" if sec == 2 and i else _tts_cn_sec(sec, not out)) + ("", "萬", "億", "兆")[i]
        gap = False
    return out


def _tts_numbers(text):
    """模型念錯的數字寫法先換成念得對的：金額符號 → 幣別、溫度、負號 → 負、千分位逗號 → 國字"""
    for rx, rep in _TTS_MONEY + _TTS_TEMP:
        text = rx.sub(rep, text)
    text = _TTS_NEG.sub("負", text)
    return _TTS_COMMA_NUM.sub(lambda m: _tts_cn_int(int(m.group(1).replace(",", "")))
                              + ("點" + "".join(_TTS_CNUM[int(c)] for c in m.group(2)[1:]) if m.group(2) else ""), text)


def _tts_glued(s, k):
    """在 s[k] 之後切會不會切斷一個詞：英數字中間、數字裡的逗號／小數點／冒號（1,250,000、0.5、3:30）"""
    a, b = s[k], s[k + 1] if k + 1 < len(s) else ""
    if a.isascii() and a.isalnum() and b.isascii() and b.isalnum():
        return True
    if a in ",.:" and k and s[k - 1].isdigit() and b.isdigit():
        return True
    return b in ",.:" and a.isdigit() and k + 2 < len(s) and s[k + 2].isdigit()


def _tts_split(text, limit=80):
    """切句：句末標點與換行；一句超過 limit 字再依逗號切，仍太長就硬切（不切在英數字、1,250,000、3:30 中間）。
    只有標點、沒有字的片段丟掉（送進模型會產生雜音）"""
    sents, buf = [], ""
    for i, ch in enumerate(text):
        buf += ch
        if ch in _TTS_SENT_END or (ch == "." and (i + 1 == len(text) or text[i + 1].isspace())
                                   and not (i and text[i - 1].isdigit())):
            sents.append(buf)
            buf = ""
    sents.append(buf)
    out = []
    for s in sents:
        s = s.strip()
        while len(s) > limit:
            cut = max((k for k, c in enumerate(s[:limit]) if c in "，,、：:" and not _tts_glued(s, k)), default=-1)
            if cut < limit // 3:
                cut = limit - 1
                while cut > limit // 2 and _tts_glued(s, cut):
                    cut -= 1
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        out.append(s)
    return [s for s in out if re.search(r"\w", s)]


def _tts_load_text(tts_dir):
    """台灣念法要用的資源：教育部辭典對照檔、g2pW、pypinyin（模型預設念法的近似）、OpenCC 繁轉簡。
    Mac 本機合成也用同一份（jtlw_tts/tw_reading.py）"""
    import opencc
    from g2pw import G2PWConverter
    from pypinyin import Style, lazy_pinyin
    from pypinyin.contrib.tone_convert import to_tone3
    from pypinyin.pinyin_dict import pinyin_dict
    from pypinyin.style.bopomofo import BopomofoConverter
    moe_path = os.path.join(tts_dir, "moe_words.tsv")
    if not os.path.exists(moe_path):
        raise FileNotFoundError(f"找不到教育部辭典對照檔 {moe_path}（安裝程式會下載並轉檔）")
    with open(moe_path, encoding="utf-8") as f:
        moe = _tts_moe_load(f)
    bc = BopomofoConverter()

    def base_bopo(base):   # 沒有聲調的拼音 pypinyin 會當輕聲加「˙」，拿掉才能跟 g2pW 的格式比（第一版因此一個字都沒換）
        return bc.to_bopomofo(base.replace("v", "ü")).replace("˙", "")

    def split_tone(p):
        m = re.match(r"([a-zü]+)([1-5])$", p.replace("ü", "v"))
        return (m.group(1), m.group(2)) if m else (p, "")

    bopo2base = {}
    for readings in pinyin_dict.values():
        for r in readings.split(","):
            base, _ = split_tone(to_tone3(r, neutral_tone_with_five=True))
            bopo2base.setdefault(base_bopo(base), base)

    def py2bopo(p):
        base, d = split_tone(p)
        return base_bopo(base) + d if d else None

    def bopo2py(b):
        base = bopo2base.get(b[:-1]) if b and b[-1].isdigit() else None
        return base + b[-1] if base else None

    g2p = G2PWConverter(model_dir=os.path.join(tts_dir, "G2PWModel") + "/", style="bopomofo",
                        model_source=os.path.join(tts_dir, "bert-base-chinese"))
    g2p.num_workers = 0          # 預設開子行程：macOS／Windows 用 spawn 會卡死；建構時傳 0 會被當成沒指定
    return {"moe": moe, "g2p": g2p, "lazy_pinyin": lazy_pinyin, "TONE3": Style.TONE3,
            "t2s": opencc.OpenCC("t2s"), "py2bopo": py2bopo, "bopo2py": bopo2py}


def _tts_readings(R, text, custom=None):
    """每個字的 (台灣念法, 模型預設念法)，都是注音＋數字；非漢字為 None"""
    good, _ = _tts_custom(custom)
    tw = _tts_overlay(text, [g if g else None for g in R["g2p"](text)[0]], R["moe"], good)
    src = _tts_simp_src(text, tw)                    # 模型預設念法拿「送進模型的字」算：着急的着、著作的著念法不同
    cn = [None] * len(text)
    for m in _TTS_HAN.finditer(src):
        run = m.group(0)
        simp = R["t2s"].convert(run)
        if len(simp) != len(run):
            continue
        for k, p in enumerate(R["lazy_pinyin"](simp, style=R["TONE3"], neutral_tone_with_five=True)):
            cn[m.start() + k] = R["py2bopo"](p)
    # 刻意指定念法的詞（台灣日常念法、自訂發音）：pypinyin 可能跟前後文切成別的詞（「萬人參與」切出「人參」，「與」算成單字的ㄩˇ，
    # 跟台灣念法一樣就不加提示，模型卻照「參與」念ㄩˋ），所以這個詞再單獨算一次，兩種算法有一種跟台灣念法不同就加提示（2026-10-09）
    for w, r in {**_TTS_TW_WORDS, **{w: r for w, r in good.items() if len(w) > 1}}.items():
        i = text.find(w)
        while i >= 0:
            simp = R["t2s"].convert(src[i:i + len(w)])
            if tw[i:i + len(w)] == r and len(simp) == len(w):
                for k, p in enumerate(R["lazy_pinyin"](simp, style=R["TONE3"], neutral_tone_with_five=True)):
                    b = R["py2bopo"](p)
                    if b and tw[i + k] == cn[i + k] and b != tw[i + k]:
                        cn[i + k] = b
            i = text.find(w, i + 1)
    return tw, cn


def _tts_vocab_chars(path):
    """模型詞彙裡的中文單字（模型資料夾的 tokenizer.json）。不在裡面的字只能拆成位元組送進模型，模型不知道怎麼念，
    一律加念法提示（2026-10-09 自動偵測：人名用字的昀、婞，蚵仔煎的蚵都念錯，三個字都不在詞彙裡）。讀不到回 None（不套這條）"""
    try:
        with open(path, encoding="utf-8") as f:
            vocab = json.load(f)["model"]["vocab"]
        return frozenset(k for k in vocab if len(k) == 1 and _TTS_HAN.fullmatch(k))
    except Exception:
        return None


def _tts_simp_src(text, tw):
    """送進模型前要寫成的字（_TTS_SIMP_BY_READING）：扮演著 → 扮演着、著急 → 着急；著作照舊"""
    if not _TTS_SIMPLIFIED:
        return text
    return "".join(_TTS_SIMP_BY_READING[ch][1] if ch in _TTS_SIMP_BY_READING and t and t != _TTS_SIMP_BY_READING[ch][0]
                   else ch for ch, t in zip(text, tw))


def _tts_spoken(R, text, custom=None):
    """原文 → 送進模型的文字（念錯的數字寫法先換掉；台灣念法與模型預設不同的字換成 {拼音}）。
    沒加提示的字轉成簡體再送：模型幾乎只學過簡體，繁體字會念錯（2026-10-09 實測 協、脈、漲、頒、衝 等）；
    「模型預設念法」本來就是拿簡體算的（_tts_readings 的 cn），轉了之後模型念的正好就是比對時假設的"""
    text = _tts_numbers(text)
    tw, cn = _tts_readings(R, text, custom)
    text = _tts_simp_src(text, tw)
    sent = R["t2s"].convert(text) if _TTS_SIMPLIFIED else text      # 沒加提示時送進模型的字
    vocab = R.get("vocab")
    force = {k for k, ch in enumerate(sent) if _TTS_HAN.fullmatch(ch) and ch not in vocab} if vocab and len(sent) == len(text) else ()
    hinted = _tts_hint(text, tw, cn, R["bopo2py"], force)
    if not _TTS_SIMPLIFIED:
        return hinted
    return "".join(p if p.startswith("{") else R["t2s"].convert(p) for p in re.split(r"(\{[a-z]+[1-5]\})", hinted))


def _tts_build_moe_main():
    """安裝程式用：server.py --tts-build-moe <dict-revised.json[.xz]> <moe_words.tsv>（格式轉換，只用標準函式庫）"""
    import lzma
    i = sys.argv.index("--tts-build-moe")
    src, dst = sys.argv[i + 1], sys.argv[i + 2]
    with (lzma.open if src.endswith(".xz") else open)(src, "rt", encoding="utf-8") as f:
        lines = _tts_moe_lines(json.load(f))
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, dst)
    print(f"教育部辭典對照檔：{len(lines)} 個詞 → {dst}")


_TTS_EN_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                "sixteen seventeen eighteen nineteen").split()
_TTS_EN_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_TTS_EN_MONEY = re.compile(r"(?<![A-Za-z])(NT|US)?\$\s?(\d[\d,]*(?:\.\d+)?)")
_TTS_EN_DOTTED = re.compile(r"(?<![\w.])([vV]?)(\d+(?:\.\d+){2,})(?![\w]|\.\d)")
_TTS_EN_COMMA = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(?![\d,]|\.\d)")


def _tts_en_int(n):
    """英文的整數念法（0～999,999,999,999）"""
    if n < 20:
        return _TTS_EN_ONES[n]
    if n < 100:
        return _TTS_EN_TENS[n // 10] + ("-" + _TTS_EN_ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return _TTS_EN_ONES[n // 100] + " hundred" + (" " + _tts_en_int(n % 100) if n % 100 else "")
    for div, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= div:
            return _tts_en_int(n // div) + " " + name + (" " + _tts_en_int(n % div) if n % div else "")


def _tts_en_text(text):
    """英文句子送進模型前（v2.28.0 雙向口譯）：不套台灣念法、不把數字換成中文（_tts_numbers 會把 1,250,000 換成一百二十五萬）。
    2026-10-10 GPU 實測三種聲音各 4 句：1,250,000 被念成 150,000、IP 與版本號偶爾念錯 →
    千分位的數字寫成英文、金額改成「數字＋幣別」、IP／版本號一段一段用 dot 連起來；其他照原文（模型念得對）"""
    t = " ".join(str(text).split())
    t = _TTS_EN_MONEY.sub(lambda m: f"{m.group(2)} " + {"NT": "NT dollars", "US": "US dollars"}.get(m.group(1) or "", "dollars"), t)
    t = _TTS_EN_DOTTED.sub(lambda m: ("version " if m.group(1) else "") + " dot ".join(m.group(2).split(".")), t)

    def comma(m):
        n = int(m.group(1).replace(",", ""))
        return _tts_en_int(n) if n < 10 ** 12 else m.group(1)
    return _TTS_EN_COMMA.sub(comma, t)


def _tts_worker_main():
    """只聽 127.0.0.1。POST /synthesize {text, voice_wav, voice_text, custom, steps, cfg, lang} → audio/wav（48 kHz 單聲道）；
    POST /synthesize_stream（同上）→ 邊合成邊送 16-bit PCM（audio/L16，X-TTS-SR 取樣率，送完關連線；雙向口譯用，v2.28.0）；
    POST /convert {text, custom} → {spoken}（送進模型的文字，除錯與測試用）。
    lang＝en：英文句子，不套台灣念法與中文數字念法（_tts_en_text）"""
    import http.server
    import io
    import signal
    import urllib.parse
    port = int(sys.argv[sys.argv.index("--tts-worker") + 1])
    parent = os.getppid()

    def _watch():
        while True:
            time.sleep(5)
            if os.getppid() != parent:          # 主服務結束了
                try:
                    os.killpg(0, signal.SIGKILL)
                finally:
                    os._exit(0)
    threading.Thread(target=_watch, daemon=True).start()
    state = {"ready": False, "error": ""}
    lock = threading.Lock()
    R = {}

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, ctype="application/json", headers=None):
            b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            if state["error"]:
                return self._send(500, {"ok": False, "error": state["error"]})
            self._send(200, {"ok": True, "model": TTS_MODEL}) if state["ready"] \
                else self._send(503, {"ok": False, "loading": True})

        def do_POST(self):
            if self.path not in ("/synthesize", "/synthesize_stream", "/convert"):
                return self._send(404, {"error": "not found"})
            if not state["ready"]:
                return self._send(503, {"error": state["error"] or "文字轉語音模型載入中"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                text = str(req.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "沒有文字"})
                with lock:                      # 一次一件（主服務本來就排隊，這裡是保險）
                    sp = _tts_en_text(text) if req.get("lang") == "en" else _tts_spoken(R, text, req.get("custom"))
                    if self.path == "/convert":
                        return self._send(200, {"spoken": sp})
                    kw = dict(text=sp, prompt_wav_path=req["voice_wav"], prompt_text=req["voice_text"],
                              reference_wav_path=req["voice_wav"], cfg_value=float(req.get("cfg") or 2.0),
                              inference_timesteps=int(req.get("steps") or 10), normalize=False)
                    t0 = time.monotonic()
                    if self.path == "/synthesize_stream":
                        return self._stream(kw, sp, t0)
                    try:
                        wav = R["model"].generate(**kw)
                    finally:
                        R["torch"].cuda.empty_cache()
                buf = io.BytesIO()
                R["sf"].write(buf, wav, R["sr"], format="WAV", subtype="PCM_16")
                self._send(200, buf.getvalue(), "audio/wav", {
                    "X-TTS-Spoken": urllib.parse.quote(sp),
                    "X-TTS-Duration": f"{len(wav) / R['sr']:.3f}",
                    "X-TTS-Seconds": f"{time.monotonic() - t0:.3f}"})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _stream(self, kw, sp, t0):
            """第一段合成出來就送（不用等整句）。沒有 Content-Length：送完關連線（HTTP/1.0）。
            標頭送出後才出錯的話只能斷線，用戶端看到的是音訊比預期短"""
            import itertools
            import numpy as np
            gen = R["model"].generate_streaming(**kw)
            try:
                first = next(gen)
            except Exception:
                R["torch"].cuda.empty_cache()
                raise                           # 還沒送標頭：照一般錯誤回 500
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16")
            self.send_header("X-TTS-SR", str(R["sr"]))
            self.send_header("X-TTS-Spoken", urllib.parse.quote(sp))
            self.send_header("X-TTS-First", f"{time.monotonic() - t0:.3f}")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in itertools.chain([first], gen):
                    pcm = (np.clip(np.asarray(chunk, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype("<i2")
                    self.wfile.write(pcm.tobytes())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                            # 用戶端取消（例如靜音）：停止合成
            except Exception as e:              # 標頭已經送出：不可以再寫錯誤回應（會混進音訊），只能斷線（音訊比預期短）
                print(f"[文字轉語音] 串流合成中途失敗：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                self.close_connection = True
            finally:
                gen.close()
                R["torch"].cuda.empty_cache()

    # 先綁埠號再載入（比照 Qwen worker：同一個埠已有 worker 時立刻失敗，不會白白載一份模型）
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import soundfile
        import torch as _torch
        import voxcpm.model.voxcpm2 as _v2
        from huggingface_hub import snapshot_download
        from voxcpm import VoxCPM
        path = snapshot_download(TTS_MODEL, revision=TTS_MODEL_REV, local_files_only=True)
        R.update(_tts_load_text(TTS_DIR), sf=soundfile, torch=_torch, vocab=_tts_vocab_chars(os.path.join(path, "tokenizer.json")))
        # 載入記憶體：上游先在 CPU 建 float32 模型（9.5 GB）再讀 bf16 權重，峰值 14.5 GB；
        # 直接在 GPU 上以 bf16 建，峰值 6.5 GB、輸出波形逐點相同（2026-10-08 實測）
        orig_init = _v2.VoxCPM2Model.__init__

        def _init(self, *a, **k):
            prev = _torch.get_default_dtype()
            _torch.set_default_dtype(_torch.bfloat16)
            try:
                with _torch.device("cuda"):
                    orig_init(self, *a, **k)
            finally:
                _torch.set_default_dtype(prev)
        _v2.VoxCPM2Model.__init__ = _init
        # torch.compile 在 GB10 上反而慢 45%（RTF 0.91 → 1.32），不開；不載降噪（多一個模型、要 modelscope）
        R["model"] = VoxCPM(voxcpm_model_path=path, zipenhancer_model_path=None, enable_denoiser=False, optimize=False)
        R["sr"] = R["model"].tts_model.sample_rate
        state["ready"] = True
        print(f"[tts-worker] 就緒 127.0.0.1:{port}（{TTS_MODEL}）", flush=True)
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        print(f"[tts-worker] 載入失敗：{state['error']}", flush=True)
    threading.Event().wait()


# ── 文字轉語音的安裝（只用標準函式庫：安裝程式在 GPU 伺服器、Mac 本機都用這一份，不必在 install.sh／install.ps1 各寫一遍）──
TTS_G2PW_URL = "https://storage.googleapis.com/esun-ai/g2pW/G2PWModel-v2-onnx.zip"
TTS_BERT = ("google-bert/bert-base-chinese", "8f23c25b06e129b6c986331a13d8d025a92cf0ea")
TTS_MOE_URL = "https://raw.githubusercontent.com/g0v/moedict-data/a6dc997417507eb510fc29822bc514de2c92728c/dict-revised.json.xz"
TTS_MOE_NOTICE = ("moe_words.tsv 由教育部《重編國語辭典修訂本》（g0v/moedict-data 整理）轉成「詞→讀音」對照，只做格式轉換。\n"
                  "著作權屬教育部，創用 CC 姓名標示-禁止改作 3.0 臺灣；依教育部解釋，禁止改作限制的是文字本身，"
                  "不限制格式轉換及後續應用。https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/index.html\n")
# GPU 伺服器 venv-tts 的套件：2026-10-08 在 DGX Spark 測過的組合。只列推論真的用到的（voxcpm 的 funasr、modelscope、gradio 用不到）
TTS_TORCH = "2.11.0"
TTS_PIP = ["voxcpm==2.0.3", "transformers==5.19.0", "huggingface-hub==1.33.0", "tokenizers==0.23.2", "safetensors==0.8.0",
           "numpy==2.5.3", "librosa==1.0.0", "soundfile==0.14.0", "einops==0.8.2", "pydantic==2.13.5", "simplejson==4.2.0",
           "tqdm==4.70.1", "onnxruntime==1.30.0", "g2pw==0.1.1", "pypinyin==0.55.0", "opencc-python-reimplemented==0.1.7",
           "requests"]                          # g2pw 有 import requests 卻沒宣告相依（Mac 實測才發現）


def _tts_download(url, dest, tries=6, stall=30):
    """續傳＋重試（GitHub 原始檔有時很慢、還會停住：一次下載 15 MB 曾超過 5 分鐘）。
    stall 秒收不到資料就斷線續傳（以前 120 秒，畫面一直不動、看起來像當掉：2026-10-09 Mac 實機）；
    在終端機上顯示已下載多少（經 ssh 跑、輸出不是終端機時不顯示，免得記錄裡一堆進度行）"""
    import urllib.request
    tmp = dest + ".part"
    tty = sys.stdout.isatty()
    for n in range(1, tries + 1):
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        shown = False
        try:
            with urllib.request.urlopen(req, timeout=stall) as r, open(tmp, "ab" if have and r.status == 206 else "wb") as f:
                got = have if have and r.status == 206 else 0
                h = getattr(r, "headers", None)
                try:
                    total = int(h.get("Content-Length")) + got if h is not None and h.get("Content-Length") else None
                except (TypeError, ValueError):
                    total = None
                last = 0.0
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
                    got += len(b)
                    if tty and time.monotonic() - last > 0.5:
                        last, shown = time.monotonic(), True
                        print(f"\r    已下載 {got / 1048576:.1f}" + (f"／{total / 1048576:.1f}" if total else "") + " MB   ",
                              end="", flush=True)
            if shown:
                print()
            os.replace(tmp, dest)
            return dest
        except Exception as e:
            if shown:
                print()
            if n == tries:
                raise RuntimeError(f"下載失敗（{url}）：{type(e).__name__}: {e}") from e
            print(f"    下載停住或中斷（{type(e).__name__}），{5 * n} 秒後從中斷處續傳（第 {n + 1}/{tries} 次）", flush=True)
            time.sleep(5 * n)


def _tts_fetch_data(d):
    """台灣念法要用的資源 → d：G2PWModel/（g2pW 模型，約 600 MB）、bert-base-chinese/（分詞器）、moe_words.tsv（教育部辭典對照）。
    已經有的不重下"""
    import lzma
    import zipfile
    os.makedirs(d, exist_ok=True)
    g = os.path.join(d, "G2PWModel")
    if not os.path.exists(os.path.join(g, "version")):
        print("  [文字轉語音] 下載 g2pW 模型（約 600 MB，判斷破音字的台灣念法）...", flush=True)
        z = _tts_download(TTS_G2PW_URL, os.path.join(d, "G2PWModel.zip"))
        shutil.rmtree(g, ignore_errors=True)          # 上次解到一半的先清掉（壓縮檔最上層剛好就叫 G2PWModel）
        with zipfile.ZipFile(z) as zf:
            top = zf.namelist()[0].split("/")[0]
            zf.extractall(d)
        if top != "G2PWModel":
            os.replace(os.path.join(d, top), g)
        os.remove(z)
    b = os.path.join(d, "bert-base-chinese")
    os.makedirs(b, exist_ok=True)
    for f in ("vocab.txt", "tokenizer.json", "tokenizer_config.json", "config.json"):
        if not os.path.exists(os.path.join(b, f)):
            _tts_download(f"https://huggingface.co/{TTS_BERT[0]}/resolve/{TTS_BERT[1]}/{f}", os.path.join(b, f))
    m = os.path.join(d, "moe_words.tsv")
    if not os.path.exists(m):
        print("  [文字轉語音] 下載教育部《重編國語辭典修訂本》（約 15 MB）並轉成讀音對照...", flush=True)
        x = _tts_download(TTS_MOE_URL, os.path.join(d, "dict-revised.json.xz"))
        with lzma.open(x, "rt", encoding="utf-8") as f:
            lines = _tts_moe_lines(json.load(f))
        with open(m + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(m + ".tmp", m)
        os.remove(x)                      # 只留轉好的對照（格式轉換），原始辭典不留
        with open(os.path.join(d, "NOTICE-moe.txt"), "w", encoding="utf-8") as f:
            f.write(TTS_MOE_NOTICE)
    print(f"  [文字轉語音] 資源就緒：{d}", flush=True)


def _tts_gpu_info():
    """(顯示卡名稱, compute capability, 驅動支援的 CUDA 版本 (major, minor))；沒有 NVIDIA 顯示卡回 None"""
    import subprocess
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=30)
        h = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if q.returncode != 0 or not q.stdout.strip():
        return None
    name, cap = [x.strip() for x in q.stdout.strip().splitlines()[0].split(",")[:2]]
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", h.stdout)
    return name, cap, (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _tts_torch_index(cap, cuda):
    """驅動支援 CUDA 13 → cu130；GB10（12.1）一定要 cu130（cu128 的 NVRTC 不認 sm_121，2026-10-08 實測）；其餘 cu128"""
    if cuda >= (13, 0):
        return "cu130"
    if cap.startswith("12.1"):
        raise RuntimeError("這張顯示卡（compute capability 12.1）需要支援 CUDA 13 的驅動（580 以上）")
    if cuda >= (12, 8):
        return "cu128"
    raise RuntimeError(f"顯示卡驅動只支援 CUDA {cuda[0]}.{cuda[1]}，文字轉語音需要 12.8 以上")


def _tts_setup_main():
    """GPU 伺服器：server.py --tts-setup。建立 ~/jt-whisper-server/venv-tts、裝固定版本的套件、下載 VoxCPM2 與台灣念法資源，最後驗證。
    重跑安全：已經裝好的跳過。裝完約 11 GB（venv-tts 5.2 GB：CUDA 13 版 torch 自帶整組 CUDA 程式庫；模型 4.7 GB；g2pW 0.6 GB；2026-10-09 GB10 實測）"""
    import shutil as _sh
    import subprocess
    home = os.path.dirname(os.path.abspath(__file__))
    venv = os.environ.get("JT_TTS_VENV") or os.path.join(home, "venv-tts")
    py = os.path.join(venv, "bin", "python")
    info = _tts_gpu_info()
    if not info:
        print("  [文字轉語音] 這台沒有 NVIDIA 顯示卡（找不到 nvidia-smi），不設定", flush=True)
        sys.exit(2)
    name, cap, cuda = info
    try:
        idx = _tts_torch_index(cap, cuda)
    except RuntimeError as e:
        print(f"  [文字轉語音] {e}", flush=True)
        sys.exit(2)
    print(f"  [文字轉語音] 顯示卡 {name}（{cap}），驅動支援 CUDA {cuda[0]}.{cuda[1]} → torch {TTS_TORCH}+{idx}", flush=True)
    free = _sh.disk_usage(home).free / 1024 ** 3
    if free < 20:
        print(f"  [文字轉語音] 磁碟只剩 {free:.0f} GB，需要約 20 GB（含下載暫存）", flush=True)
        sys.exit(2)

    def run(cmd, what):
        print(f"  [文字轉語音] {what}...", flush=True)
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            print(f"  [文字轉語音] {what}失敗（結束碼 {r.returncode}）", flush=True)
            sys.exit(1)

    if not os.path.exists(py):
        base = next((p for p in (_sh.which("python3.12"), _sh.which("python3.11"), _sh.which("python3")) if p), None)
        if not base:
            print("  [文字轉語音] 找不到 python3", flush=True)
            sys.exit(1)
        run([base, "-m", "venv", venv], f"建立 {venv}")
    ok = subprocess.run([py, "-c", "import importlib.metadata as m,sys;sys.exit(0 if m.version('torch').startswith('%s') "
                                   "and m.version('voxcpm')=='2.0.3' and m.version('g2pw') else 1)" % TTS_TORCH],
                        capture_output=True).returncode == 0
    if not ok:
        run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], "更新 pip")
        run([py, "-m", "pip", "install", "-q", "--no-cache-dir", f"torch=={TTS_TORCH}", f"torchaudio=={TTS_TORCH}",
             "--index-url", f"https://download.pytorch.org/whl/{idx}"], f"安裝 torch {TTS_TORCH}（{idx}，含 CUDA 程式庫約 7 GB）")
        # voxcpm 宣告的相依有 torchcodec（沒有 ARM Linux 版；VoxCPM2 讀音檔用 librosa，用不到）、funasr 等：不讓 pip 自己解
        run([py, "-m", "pip", "install", "-q", "--no-deps", TTS_PIP[0]], "安裝 voxcpm")
        run([py, "-m", "pip", "install", "-q", "--no-cache-dir"] + TTS_PIP[1:], "安裝其他套件")
        print("  （pip 若列出「voxcpm requires matplotlib、modelscope、torchcodec…, which is not installed」是刻意的："
              "那些是訓練、網頁介面與 ARM 沒有的套件，朗讀用不到）", flush=True)
    run([py, "-c", f"from huggingface_hub import snapshot_download as s; s({TTS_MODEL!r}, revision={TTS_MODEL_REV!r})"],
        "下載 VoxCPM2 模型（約 4.7 GB）")
    _tts_fetch_data(TTS_DIR)
    run([py, "-c", "import torch, voxcpm, g2pw, pypinyin, opencc; assert torch.cuda.is_available(), '看不到顯示卡'; "
                   "x = torch.ones(4, device='cuda'); print('  torch', torch.__version__, torch.cuda.get_device_name(0), float(x.sum()))"],
        "檢查")
    print("  [文字轉語音] 完成：第一次朗讀時才啟動合成服務（約 30 秒），閒置 30 分鐘自動關閉", flush=True)


# ── BreezyVoice（MediaTek Research，Apache-2.0；2026-10-09 起選用，不是預設）──────────────
# 用台灣華語訓練，口音與念法道地；但合成慢：GB10 上合成時間約是音訊長度的 1.4～2.5 倍（VoxCPM2 約 0.9），不適合即時朗讀。
# 上游照 requirements 在 ARM 裝不起來（torch 2.3.1＋cu118、WeTextProcessing 的 pynini、ttsfrd 都只有 x86）→
# 獨立 venv（venv-breezy，torch 與 venv-tts 同版），上游原始碼的固定版本放 BREEZY_DIR，worker 載入時補相容：
#   wetext 取代 WeTextProcessing、**不轉簡體**（上游轉簡體後罕見字判斷全亂，漏句、亂念，2026-10-08 實測）、
#   soundfile 讀參考錄音（torchaudio 2.9 起讀檔要 torchcodec，ARM 沒有）、補 torchaudio.set_audio_backend、ruamel.yaml<0.18
BREEZY_MODEL = "MediaTek-Research/BreezyVoice-300M"
BREEZY_MODEL_REV = "e33b502e0ac21c16b0ee0d00df66ac3fa737393d"
BREEZY_FILES = ["cosyvoice.yaml", "configuration.json", "campplus.onnx", "speech_tokenizer_v1.onnx", "llm.pt", "flow.pt",
                "hift.pt", "spk2info.pt"]                    # 約 2.2 GB；不下載 ttsfrd 資源（x86 專用）與 TensorRT 用的檔
BREEZY_CODE_REV = "d592c9d3e8927a0f53f68616387060dcd32a05ea"
BREEZY_CODE_URL = f"https://codeload.github.com/mtkresearch/BreezyVoice/tar.gz/{BREEZY_CODE_REV}"
BREEZY_DIR = os.path.expanduser(os.environ.get("JT_BREEZY_DIR") or "~/jt-whisper-server/breezyvoice")
BREEZY_SR = 22050
# venv-breezy 的套件：2026-10-09 在 DGX Spark 測過的組合（torch 用 TTS_TORCH 同版）
BREEZY_PIP = ["conformer==0.3.2", "diffusers==0.41.0", "hydra-core==1.3.2", "HyperPyYAML==1.2.2", "ruamel.yaml==0.17.40",
              "omegaconf==2.3.0", "lightning==2.6.6", "inflect==7.5.0", "einops==0.8.2", "wetext==0.1.8",
              "openai-whisper==20250625", "tiktoken==0.14.0", "numba==0.68.0", "matplotlib==3.11.2", "gdown==6.4.2",
              "wget==3.2", "pyarrow==26.0.0", "transformers==5.19.0", "huggingface-hub==1.33.0", "tokenizers==0.23.2",
              "safetensors==0.8.0", "numpy==2.5.3", "scipy==1.18.1", "librosa==1.0.0", "soundfile==0.14.0",
              "onnxruntime==1.30.0", "g2pw==0.1.1", "pypinyin==0.55.0", "opencc-python-reimplemented==0.1.7",
              "tqdm==4.70.1", "requests"]
_BREEZY_DOTS = re.compile(r"\d+(?:\.\d+){2,}")


def _breezy_prep(text):
    """送進 BreezyVoice 的文字正規化之前：jtlw 的數字處理（千分位、負數、金錢），版本號與 IP 逐字念
    （wetext 會把 0.6.46 弄成「零.六点四六」）；方括號是上游的注音語法，原文的拿掉"""
    text = _tts_numbers(text).replace("[", " ").replace("]", " ")
    return _BREEZY_DOTS.sub(lambda m: "點".join("".join(_TTS_CNUM[int(c)] for c in p) for p in m.group(0).split(".")), text)


def _breezy_tn_trad(src, out, s2t):
    """wetext 關掉轉簡體，數字念法照樣寫成簡體（点、万）：原文沒有、正規化才多出來的字轉成繁體"""
    return "".join(c if c in src else s2t(c) for c in out)


def _breezy_annotate(text, tw, raw, freq, char2phn, always):
    """BreezyVoice 的念法提示 `字[:ㄅㄧㄢ4]`。挑哪些字照上游（get_bopomofo_rare：訓練資料裡少見的字、這次不是最常見念法的
    破音字，模型就是照這套訓練的），念法換成 jtlw 的（教育部辭典、發音字典、台灣常用念法）；jtlw 跟 g2pW 判斷不同的字
    （我們修正過的）與 _TTS_ALWAYS_HINT 也加；一、不不加（會變調）。tw／raw：每個字的 jtlw 念法／g2pW 原始判斷"""
    out = []
    for k, ch in enumerate(text):
        t = tw[k]
        nxt = text[k + 1] if k + 1 < len(text) else ""
        if not t or ch in _TTS_NO_HINT or nxt == "[" or not _TTS_HAN.fullmatch(ch):
            out.append(ch)
            continue
        f = freq.get(ch, 0)
        cands = char2phn.get(ch) or []
        pick = (f < 500 or (len(cands) >= 2 and t != cands[0] and (f < 10000 or ch in always))
                or (raw[k] is not None and raw[k] != t) or ch in _TTS_ALWAYS_HINT)
        out.append(f"{ch}[:{t}]" if pick else ch)
    return "".join(out)


def _breezy_worker_main():
    """只聽 127.0.0.1，介面同 _tts_worker_main：POST /synthesize {text, voice_wav, voice_text, custom} → audio/wav（22.05 kHz）；
    POST /convert {text, custom} → {spoken}"""
    import http.server
    import io
    import signal
    import types
    import urllib.parse
    port = int(sys.argv[sys.argv.index("--breezy-worker") + 1])
    parent = os.getppid()

    def _watch():
        while True:
            time.sleep(5)
            if os.getppid() != parent:
                try:
                    os.killpg(0, signal.SIGKILL)
                finally:
                    os._exit(0)
    threading.Thread(target=_watch, daemon=True).start()
    state = {"ready": False, "error": ""}
    lock = threading.Lock()
    R = {}
    prompts = {}

    def spoken(text, custom):
        norm = R["cv"].frontend.text_normalize_new(_breezy_prep(text), split=False)
        tw, _ = _tts_readings(R, norm, custom)
        return _breezy_annotate(norm, tw, R["g2p"](norm)[0], R["freq"], R["char2phn"], R["always"])

    def prompt_for(wav, text):
        """參考錄音的特徵每句都一樣：同一個聲音只算一次"""
        if (wav, text) not in prompts:
            ptxt = spoken(text, None)
            mi = R["cv"].frontend.frontend_zero_shot("。", ptxt, R["load_wav"](wav, 16000))
            if len(prompts) >= 8:
                prompts.clear()
            prompts[(wav, text)] = {k: v for k, v in mi.items() if k not in ("text", "text_len")}
        return prompts[(wav, text)]

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, ctype="application/json", headers=None):
            b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            if state["error"]:
                return self._send(500, {"ok": False, "error": state["error"]})
            self._send(200, {"ok": True, "model": BREEZY_MODEL}) if state["ready"] \
                else self._send(503, {"ok": False, "loading": True})

        def do_POST(self):
            if self.path not in ("/synthesize", "/convert"):
                return self._send(404, {"error": "not found"})
            if not state["ready"]:
                return self._send(503, {"error": state["error"] or "BreezyVoice 載入中"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                text = str(req.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "沒有文字"})
                with lock:
                    sp = spoken(text, req.get("custom"))
                    if self.path == "/convert":
                        return self._send(200, {"spoken": sp})
                    t0 = time.monotonic()
                    torch = R["torch"]
                    try:
                        base = prompt_for(req["voice_wav"], req["voice_text"])
                        parts = []
                        for piece in re.split(r"(?<=[？！。.?!])\s*", sp):      # 上游 inference_zero_shot_no_normalize 的切法
                            if piece:
                                tok, tok_len = R["cv"].frontend._extract_text_token(piece)
                                parts.append(R["cv"].model.inference(**dict(base, text=tok, text_len=tok_len))["tts_speech"])
                        wav = torch.concat(parts, dim=1).squeeze(0).float().numpy() if parts else None
                    finally:
                        torch.cuda.empty_cache()
                if wav is None:
                    return self._send(400, {"error": "沒有可以念的文字"})
                buf = io.BytesIO()
                R["sf"].write(buf, wav, BREEZY_SR, format="WAV", subtype="PCM_16")
                self._send(200, buf.getvalue(), "audio/wav", {
                    "X-TTS-Spoken": urllib.parse.quote(sp),
                    "X-TTS-Duration": f"{len(wav) / BREEZY_SR:.3f}",
                    "X-TTS-Seconds": f"{time.monotonic() - t0:.3f}"})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import opencc
        import soundfile
        import torch as _torch
        import torchaudio
        import torchaudio.functional as _AF
        from huggingface_hub import snapshot_download
        from wetext import Normalizer as _WN
        s2t = opencc.OpenCC("s2tw").convert

        class _Zh:                                   # 代替 WeTextProcessing 的 tn.chinese.normalizer.Normalizer
            def __init__(self, **kw):
                self.n = _WN(lang="zh", operator="tn", traditional_to_simple=False, remove_erhua=False, full_to_half=False)

            def normalize(self, text):
                return _breezy_tn_trad(text, self.n.normalize(text), s2t)

        class _En:
            def __init__(self, **kw):
                self.n = _WN(lang="en", operator="tn")

            def normalize(self, text):
                return self.n.normalize(text)
        for name in ("tn", "tn.chinese", "tn.english"):
            sys.modules.setdefault(name, types.ModuleType(name))
        for name, cls in (("tn.chinese.normalizer", _Zh), ("tn.english.normalizer", _En)):
            m = types.ModuleType(name)
            m.Normalizer = cls
            sys.modules[name] = m
        if not hasattr(torchaudio, "set_audio_backend"):
            torchaudio.set_audio_backend = lambda *a, **k: None

        def load_wav(wav, target_sr):
            x, sr = soundfile.read(wav, dtype="float32", always_2d=True)
            s = _torch.from_numpy(x.mean(axis=1)).unsqueeze(0)
            return _AF.resample(s, sr, target_sr) if sr != target_sr else s
        sys.path[:0] = [BREEZY_DIR, os.path.join(BREEZY_DIR, "third_party", "Matcha-TTS")]
        import cosyvoice.utils.file_utils as _fu
        _fu.load_wav = load_wav
        import single_inference as _si
        from utils.word_utils import always_augment_chars, char2phn, word_to_dataset_frequency
        path = snapshot_download(BREEZY_MODEL, revision=BREEZY_MODEL_REV, local_files_only=True, allow_patterns=BREEZY_FILES)
        R.update(_tts_load_text(TTS_DIR), sf=soundfile, torch=_torch, load_wav=load_wav,
                 freq=dict(word_to_dataset_frequency), char2phn=dict(char2phn), always=set(always_augment_chars))
        R["cv"] = _si.CustomCosyVoice(path)
        state["ready"] = True
        print(f"[breezy-worker] 就緒 127.0.0.1:{port}（{BREEZY_MODEL}）", flush=True)
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        print(f"[breezy-worker] 載入失敗：{state['error']}", flush=True)
    threading.Event().wait()


def _breezy_setup_main():
    """GPU 伺服器：server.py --breezy-setup。建立 venv-breezy、裝固定版本的套件、下載上游原始碼（固定版本）與模型（約 2.2 GB），
    台灣念法資源沒有就一起下載，最後檢查。重跑安全：已經裝好的跳過。裝完約 8 GB（venv 約 5.5 GB，含 CUDA 13 版 torch；模型 2.2 GB）"""
    import shutil as _sh
    import subprocess
    import tarfile
    home = os.path.dirname(os.path.abspath(__file__))
    venv = os.environ.get("JT_BREEZY_VENV") or os.path.join(home, "venv-breezy")
    py = os.path.join(venv, "bin", "python")
    tag = "  [BreezyVoice]"
    info = _tts_gpu_info()
    if not info:
        print(f"{tag} 這台沒有 NVIDIA 顯示卡（找不到 nvidia-smi），不設定", flush=True)
        sys.exit(2)
    name, cap, cuda = info
    try:
        idx = _tts_torch_index(cap, cuda)
    except RuntimeError as e:
        print(f"{tag} {e}", flush=True)
        sys.exit(2)
    free = _sh.disk_usage(home).free / 1024 ** 3
    if free < 16:
        print(f"{tag} 磁碟只剩 {free:.0f} GB，需要約 16 GB（含下載暫存）", flush=True)
        sys.exit(2)

    def run(cmd, what):
        print(f"{tag} {what}...", flush=True)
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            print(f"{tag} {what}失敗（結束碼 {r.returncode}）", flush=True)
            sys.exit(1)

    if not os.path.exists(py):
        base = next((p for p in (_sh.which("python3.12"), _sh.which("python3.11"), _sh.which("python3")) if p), None)
        if not base:
            print(f"{tag} 找不到 python3", flush=True)
            sys.exit(1)
        run([base, "-m", "venv", venv], f"建立 {venv}")
    ok = subprocess.run([py, "-c", "import importlib.metadata as m,sys;sys.exit(0 if m.version('torch').startswith('%s') "
                                   "and m.version('wetext')=='0.1.8' and m.version('ruamel.yaml')=='0.17.40' else 1)" % TTS_TORCH],
                        capture_output=True).returncode == 0
    if not ok:
        run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], "更新 pip")
        run([py, "-m", "pip", "install", "-q", "--no-cache-dir", f"torch=={TTS_TORCH}", f"torchaudio=={TTS_TORCH}",
             "--index-url", f"https://download.pytorch.org/whl/{idx}"], f"安裝 torch {TTS_TORCH}（{idx}，含 CUDA 程式庫約 7 GB）")
        run([py, "-m", "pip", "install", "-q", "--no-cache-dir"] + BREEZY_PIP, "安裝其他套件")
    mark = os.path.join(BREEZY_DIR, ".jtlw-rev")
    if not (os.path.exists(mark) and open(mark).read().strip() == BREEZY_CODE_REV):
        tmp = os.path.join(home, f".breezyvoice-{BREEZY_CODE_REV[:12]}.tar.gz")
        print(f"{tag} 下載 BreezyVoice 原始碼（固定版本 {BREEZY_CODE_REV[:12]}）...", flush=True)
        _tts_download(BREEZY_CODE_URL, tmp)
        new = BREEZY_DIR + ".new"
        _sh.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        with tarfile.open(tmp) as tf:
            for m in tf.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) < 2 or not parts[1] or m.issym() or m.islnk() or ".." in parts[1].split("/"):
                    continue
                m.name = parts[1]
                tf.extract(m, new)
        open(os.path.join(new, ".jtlw-rev"), "w").write(BREEZY_CODE_REV + "\n")
        _sh.rmtree(BREEZY_DIR, ignore_errors=True)
        os.replace(new, BREEZY_DIR)
        os.remove(tmp)
    run([py, "-c", f"from huggingface_hub import snapshot_download as s; s({BREEZY_MODEL!r}, revision={BREEZY_MODEL_REV!r}, "
                   f"allow_patterns={BREEZY_FILES!r})"], "下載 BreezyVoice 模型（約 2.2 GB）")
    _tts_fetch_data(TTS_DIR)
    run([py, "-c", "import torch, wetext, whisper, hyperpyyaml, conformer, diffusers, g2pw, pypinyin, opencc; "
                   "assert torch.cuda.is_available(), '看不到顯示卡'; print('  torch', torch.__version__, torch.cuda.get_device_name(0))"],
        "檢查")
    print(f"{tag} 完成：第一次選用時才啟動（約 20 秒），閒置 30 分鐘自動關閉。合成較慢（約音訊長度的 1.2～2.5 倍）", flush=True)


if __name__ == "__main__" and "--tts-worker" in sys.argv:
    _tts_worker_main()
    sys.exit(0)
if __name__ == "__main__" and "--breezy-worker" in sys.argv:
    _breezy_worker_main()
    sys.exit(0)
if __name__ == "__main__" and "--breezy-setup" in sys.argv:
    _breezy_setup_main()
    sys.exit(0)
if __name__ == "__main__" and "--tts-build-moe" in sys.argv:
    _tts_build_moe_main()
    sys.exit(0)
if __name__ == "__main__" and "--tts-fetch-data" in sys.argv:
    _tts_fetch_data(sys.argv[sys.argv.index("--tts-fetch-data") + 1])
    sys.exit(0)
if __name__ == "__main__" and "--tts-setup" in sys.argv:
    _tts_setup_main()
    sys.exit(0)

# 原始碼編譯的 CTranslate2 將 libctranslate2.so 安裝到 /usr/local/lib
# 需在 import ctranslate2 前確保 LD_LIBRARY_PATH 包含此路徑
if "/usr/local/lib" not in os.environ.get("LD_LIBRARY_PATH", ""):
    os.environ["LD_LIBRARY_PATH"] = f"/usr/local/lib:{os.environ.get('LD_LIBRARY_PATH', '')}"

import numpy as np
import torch
import uvicorn
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# ── 版本 ──
# **必須與 translate_meeting.py 的 APP_VERSION 同步**（版本號同步清單第 9 處）。
# 2026-09-21 之前伺服器完全沒有版本號，用戶端也不檢查——GPU 上的服務缺了
# v2.20.0 的講者辨識時間軸修正，而它是預設路徑，三天沒有人發現。
SERVER_VERSION = "2.29.0"

# 講者辨識：只有 >= 這個秒數的段落才進分群（1.6s = resemblyzer partial 長度，
# 短於它的聲紋是補零算出來的）。與 translate_meeting.py 必須一致。
_DIAR_MIN_CLUSTER_SEC = 1.6
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


# 更新用的共用密鑰。**沒有設定就完全不開放更新端點**（預設關閉不是預設開啟）：
# 這個端點本質上是遠端程式碼執行，內網不是空的。
UPDATE_TOKEN = os.environ.get("JT_WHISPER_UPDATE_TOKEN", "").strip()
# 更新用的 body 上限。`await request.body()` 會把整包讀進記憶體，
# 沒有上限的話一個大 POST 就能把這台機器的記憶體吃光。
UPDATE_MAX_BYTES = 8 * 1024 * 1024
UPDATE_KEEP_BACKUPS = 5
# 有作業時不拒絕更新，而是**排定**：驗證完先存起來，等作業做完才換（v2.21.8）。
# 排定期間離線線不收新作業（否則一直有人送就永遠換不了）；等太久就放棄這次更新、重新開門，
# 以免一件卡死的作業讓整台伺服器永遠不收離線作業。
UPDATE_DRAIN_TIMEOUT = int(os.environ.get("JT_WHISPER_UPDATE_DRAIN_SEC", 30 * 60))   # 環境變數只給測試用
UPDATE_RETRY_AFTER = 10
_UPDATE_LOCK = threading.Lock()
_UPDATE_PENDING = None   # {"to", "from", "since", "client_ip", "path"}


def _version_tuple(v):
    """'2.21.1' → (2, 21, 1)；解析不動的部分當 0，未知版本視為最舊"""
    out = []
    for part in str(v or "0").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _parse_server_version(path):
    """從一份 server.py 原始碼裡讀出 SERVER_VERSION"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("SERVER_VERSION"):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return "0"


def _prune_backups(me, keep=UPDATE_KEEP_BACKUPS):
    """只留最近幾份備份，避免長年累積把磁碟吃掉"""
    import glob
    try:
        old = sorted(glob.glob(me + ".bak-*"))
        for f in old[:-keep]:
            os.remove(f)
    except OSError:
        pass


app = FastAPI(title="jt-whisper-server")

# ── 作業排隊 ──
# **GPU 一次只跑一件，其餘排隊（先到先做）。** 先前是「來幾件就同時跑幾件」，
# 多個用戶端同時送離線檔時全部擠在 GPU 上：每一件都變慢、顯示記憶體可能不夠，
# 而 `/v1/status` 只有一個欄位，後到的作業會把先到的蓋掉（busy 的判斷跟著錯）。
#
# 分兩條線，**各自一次一件、彼此不互等**（2026-09-23 使用者指定：
# 「即時的另開一條，跟辨識分開」）：
#   batch     離線辨識（串流）、講者辨識、大檔的非串流辨識
#   realtime  即時字幕送來的幾秒短音訊（非串流、小檔）
# 不分開的話，即時字幕要等一場一小時的離線檔跑完才出得了字。
#
# 判斷哪條線沿用既有協定，**不需要用戶端改版**：即時路徑本來就是非串流、
# 每次約 160KB；離線路徑本來就是 stream=true。

_REALTIME_MAX_BYTES = 8 * 1024 * 1024   # 非串流且不超過這個大小 → 即時線


class _Ticket:
    """隊伍裡的一件作業。比對用物件身分（不要改成 dict：dict 會以內容比對，
    兩件參數相同的作業會被當成同一件）。"""
    __slots__ = ("type", "model", "language", "client_ip", "enqueued", "started")

    def __init__(self, task_type, model, language, client_ip):
        self.type = task_type
        self.model = model
        self.language = language
        self.client_ip = client_ip
        self.enqueued = time.time()
        self.started = None


class _Lane:
    """先到先做的單線隊伍。`_items[0]` 是正在跑的那件，其餘在等。"""

    def __init__(self, name):
        self.name = name
        self._cv = threading.Condition()
        self._items = []
        self.closed = False   # 更新排定時關門：已在隊伍裡的照樣做完，新的不收

    def enter(self, task_type, model, language, client_ip=""):
        """排進隊伍，回傳 ticket；**關門中回傳 None**（呼叫端要回 503）。
        「檢查有沒有關門」與「排進去」在同一把鎖裡，不然更新可能在兩者之間換掉程式。"""
        t = _Ticket(task_type, model, language, client_ip)
        with self._cv:
            if self.closed:
                return None
            self._items.append(t)
            if self._items[0] is t:
                t.started = time.time()
        return t

    def close(self):
        with self._cv:
            self.closed = True

    def reopen(self):
        with self._cv:
            self.closed = False
            self._cv.notify_all()

    def wait_empty(self, timeout):
        """等隊伍清空（含正在跑的那件），最多 timeout 秒；回傳是否清空"""
        with self._cv:
            return self._cv.wait_for(lambda: not self._items, max(timeout, 0))

    def position(self, t):
        """0＝輪到了；n＝前面還有 n 件；-1＝已不在隊伍裡"""
        with self._cv:
            for i, x in enumerate(self._items):
                if x is t:
                    return i
            return -1

    def wait(self, t, timeout):
        """阻塞等候輪到自己，最多 timeout 秒；回傳 position()（執行緒內使用）"""
        with self._cv:
            self._cv.wait_for(lambda: not self._items or self._items[0] is t
                              or all(x is not t for x in self._items), timeout)
        return self.position(t)

    def leave(self, t):
        """做完、出錯或用戶端放棄排隊時都要呼叫；重複呼叫無害。
        **漏掉一次，後面的人就永遠等不到。**"""
        with self._cv:
            self._items = [x for x in self._items if x is not t]
            if self._items and self._items[0].started is None:
                self._items[0].started = time.time()
            self._cv.notify_all()

    def snapshot(self):
        now = time.time()
        with self._cv:
            items = list(self._items)

        def _d(x, running):
            d = {"type": x.type, "model": x.model, "language": x.language,
                 "client_ip": x.client_ip}
            if running:
                d["elapsed"] = round(now - (x.started or now), 1)
            else:
                d["waited"] = round(now - x.enqueued, 1)
            return d
        return {"running": _d(items[0], True) if items else None,
                "waiting": [_d(x, False) for x in items[1:]]}

    def busy(self):
        with self._cv:
            return bool(self._items)

    def count(self):
        with self._cv:
            return len(self._items)


_LANES = {"batch": _Lane("batch"), "realtime": _Lane("realtime")}


def _any_busy():
    return any(l.busy() for l in _LANES.values())


def _update_pending_info():
    with _UPDATE_LOCK:
        p = dict(_UPDATE_PENDING) if _UPDATE_PENDING else None
    if not p:
        return None
    return {"to": p["to"], "waited": round(time.time() - p["since"], 1),
            "waiting_jobs": _LANES["batch"].count()}


def _updating_response():
    """更新排定中、這條線已關門時的回應。用戶端（v2.21.8 起）看到會等更新完再重送；
    舊版用戶端會當成伺服器錯誤、改用本機辨識——兩者都不會拿到殘缺的結果。"""
    with _UPDATE_LOCK:
        to = (_UPDATE_PENDING or {}).get("to")
    return JSONResponse(
        status_code=503, headers={"Retry-After": str(UPDATE_RETRY_AFTER)},
        content={"error": "updating", "retry_after": UPDATE_RETRY_AFTER, "to": to,
                 "detail": f"伺服器即將更新到 v{to}，正在等目前的作業做完"})


async def _wait_turn_async(lane, t, request):
    """在 async 端點裡等輪到自己。用戶端斷線就退出隊伍，回傳 False。"""
    while True:
        pos = lane.position(t)
        if pos == 0:
            return True
        if pos < 0:
            return False
        if await request.is_disconnected():
            lane.leave(t)
            print(f"[排隊] {t.client_ip} 在{lane.name}隊伍中斷線，已移出", flush=True)
            return False
        await asyncio.sleep(0.3)


# ── 偵測最佳後端引擎 ──
_models: dict = {}
_backend = "faster-whisper"  # "faster-whisper" 或 "openai-whisper"
_device = "cpu"
_compute_type = "int8"
_torch_device = "cpu"

if torch.cuda.is_available():
    _torch_device = "cuda"
    # 嘗試 CTranslate2 CUDA（faster-whisper 用）
    try:
        import ctranslate2
        cuda_types = ctranslate2.get_supported_compute_types("cuda")
        if cuda_types:
            _device = "cuda"
            _compute_type = "float16"
            _backend = "faster-whisper"
            print("[引擎] faster-whisper (CTranslate2 CUDA)")
        else:
            raise RuntimeError("CTranslate2 無 CUDA")
    except Exception:
        # CTranslate2 沒 CUDA，嘗試 openai-whisper（PyTorch CUDA）
        try:
            import whisper as openai_whisper  # noqa: F401
            _backend = "openai-whisper"
            _device = "cuda"
            print("[引擎] openai-whisper (PyTorch CUDA)")
        except ImportError:
            print("[警告] CTranslate2 無 CUDA 且 openai-whisper 未安裝，改用 CPU")
            _backend = "faster-whisper"
else:
    print("[引擎] faster-whisper (CPU)")

# ── 偵測 diarization 套件 ──
_HAS_DIARIZE = False
try:
    import warnings as _w
    with _w.catch_warnings():
        _w.filterwarnings("ignore", message="pkg_resources is deprecated")
        from resemblyzer import VoiceEncoder, preprocess_wav  # noqa: F401
    from spectralcluster import SpectralClusterer  # noqa: F401
    from spectralcluster import refinement, laplacian  # noqa: F401
    _HAS_DIARIZE = True
    print(f"[講者辨識] resemblyzer + spectralcluster 可用 (device={_torch_device})")
except ImportError:
    print("[講者辨識] resemblyzer/spectralcluster 未安裝")

# Nemotron 3 Diarization：transformers 內建 nemotron3_diarization（5.18 起）才有
_HAS_NEMO = False
try:
    from transformers.models.auto.configuration_auto import CONFIG_MAPPING_NAMES as _CMN
    _HAS_NEMO = "nemotron3_diarization" in _CMN
    print(f"[講者辨識] Nemotron {'可用' if _HAS_NEMO else '不可用（transformers 版本太舊，需要 5.18 以上）'}")
except Exception:
    print("[講者辨識] Nemotron 不可用（未安裝 transformers）")
if not (_HAS_DIARIZE or _HAS_NEMO):
    print("[講者辨識] 沒有可用的方法，diarize API 停用")


# ── Diarization 核心函式 ──

# ── Qwen3-ASR：主服務這一側（切窗、呼叫 worker、切句）──
# 切窗與用戶端 _nan_vad_windows **是同一支**（台語已在用，≤28 秒）；tools/test_qwen_server.py 逐一比對
_NAN_WINDOW_SEC = 28.0
_NAN_VAD_SILENCE_MS = 500
_QWEN_MODELS = ("qwen3-asr-0.6b",)
_QWEN_LANG = {"zh": "Chinese", "en": "English", "ko": "Korean"}     # 日文實測長檔較差（E3），先不開
_QWEN_SENT_END = "。？！?!"
_QWEN_FILLERS = set("嗯啊呃唔哦喔欸誒呀哈") | {"um", "uh", "mm", "hmm", "mhm"}
_QWEN = {"proc": None, "port": None, "ready": False, "error": "", "restarts": 0, "stopping": False}
_QWEN_MAX_RESTARTS = 3          # 一小時內最多自動重啟幾次（起不來時不要無限重試、一直佔 GPU 載入）


def _nan_vad_windows(audio, samplerate=16000):
    """（與 translate_meeting._nan_vad_windows 相同）依語音活動切成 ≤28 秒視窗，回傳 [(起, 迄)] 秒"""
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
            cur_end = re_
        else:
            windows.append((cur_start, cur_end))
            cur_start, cur_end = rs, re_
        while cur_end - cur_start > _NAN_WINDOW_SEC:
            windows.append((cur_start, cur_start + _NAN_WINDOW_SEC))
            cur_start += _NAN_WINDOW_SEC
    if cur_start is not None:
        windows.append((cur_start, cur_end))
    return windows


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


def _qwen_python():
    p = os.environ.get("JT_QWEN_PYTHON") or os.path.expanduser("~/jt-whisper-server/venv-qwen/bin/python")
    return p if os.path.exists(p) else None


def _qwen_start(port):
    """有 Qwen 的 venv 才啟動 worker（背景載入，約 1~3 分鐘；就緒前 /health 不列 Qwen）。
    worker 意外結束時自動重啟（一小時內最多 _QWEN_MAX_RESTARTS 次）"""
    import signal
    import subprocess
    import urllib.request
    py = _qwen_python()
    if not py or not torch.cuda.is_available():
        return
    if _qwen_port_busy(port):
        _QWEN.update(proc=None, port=port, ready=False,
                     error=f"埠號 {port} 已被佔用（可能是上一次沒收乾淨的 worker），Qwen3-ASR 停用；設 JT_QWEN_PORT 換一個")
        print(f"[Qwen3-ASR] {_QWEN['error']}")
        return
    env = dict(os.environ)
    if os.path.exists("/usr/local/cuda/bin/ptxas"):
        env.setdefault("TRITON_PTXAS_PATH", "/usr/local/cuda/bin/ptxas")   # GB10（sm_121a）Triton 內建的不認得
    log = open(os.path.join(tempfile.gettempdir(), f"jt-qwen-worker-{port}.log"), "ab")
    proc = subprocess.Popen([py, os.path.abspath(__file__), "--qwen-worker", str(port)],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                            start_new_session=True)
    _QWEN.update(proc=proc, port=port, ready=False, error="")
    print(f"[Qwen3-ASR] worker 啟動中（pid {proc.pid}，127.0.0.1:{port}）")

    def _wait():
        t0 = time.monotonic()
        while proc.poll() is None:                 # 不設死線：第一次啟動可能在下載模型（約 4 GB）
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
                    _QWEN.update(ready=True, error="")
                    print(f"[Qwen3-ASR] 就緒（{time.monotonic() - t0:.0f}s）")
                    break
            except Exception:
                if time.monotonic() - t0 > 900 and not _QWEN["error"]:
                    _QWEN["error"] = "載入超過 15 分鐘（第一次啟動可能在下載模型），仍在等待"
                time.sleep(3)
        while proc.poll() is None:                 # 就緒後守著：意外結束就重啟
            time.sleep(5)
        _QWEN["ready"] = False
        # worker 意外結束（被 kill -9、當掉）時，它底下 vLLM 的 EngineCore **不會跟著走**，
        # 會變成孤兒繼續佔約 5 GB 顯示記憶體（2026-09-26 實測，重啟兩次就疊到 10 GB）。
        # 它還留在 worker 的行程群組裡，整個群組一起收
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        if _QWEN["stopping"]:
            return
        now = time.time()
        recent = [t for t in _QWEN.setdefault("restart_times", []) if now - t < 3600]
        _QWEN["restart_times"] = recent
        delay = _qwen_restart_delay(recent, now)
        if delay > 60:
            # 到上限不永久停用：共用 GPU 上的失敗多半是暫時的（2026-09-26 正式機第一次啟動就遇到：
            # Ollama 在 vLLM 估算記憶體的那 20 秒內卸載模型，vLLM 判定估算失敗）。暫停到額度空出來再試
            _QWEN["error"] = (f"worker 一小時內結束 {len(recent) + 1} 次，暫停自動重啟，約 {int(delay // 60) + 1} 分鐘後再試；"
                              f"見 {log.name}")
        else:
            _QWEN["error"] = f"worker 結束（代碼 {proc.returncode}），{int(delay)} 秒後重啟；見 {log.name}"
        print(f"[Qwen3-ASR] {_QWEN['error']}")
        t_end = time.time() + delay
        while time.time() < t_end:
            if _QWEN["stopping"]:
                return
            time.sleep(min(5, max(0.1, t_end - time.time())))
        _QWEN["restart_times"].append(time.time())
        _QWEN["restarts"] += 1
        _qwen_start(port)
    threading.Thread(target=_wait, daemon=True).start()


def _qwen_restart_delay(recent, now, limit=None, base=30.0):
    """worker 意外結束後，多久再重啟（秒）。一小時內還沒到上限：30 秒；到上限：等到最舊那次滿一小時（額度空出來）"""
    limit = _QWEN_MAX_RESTARTS if limit is None else limit
    recent = sorted(t for t in recent if now - t < 3600)
    if len(recent) < limit:
        return base
    return max(base, 3600 - (now - recent[len(recent) - limit]) + 5)


def _qwen_port_busy(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as so:
        so.settimeout(1)
        return so.connect_ex(("127.0.0.1", port)) == 0


def _qwen_stop(wait=0.0):
    """收掉 worker 整個行程群組（含 vLLM 的 EngineCore）。wait>0 時等它真的結束，逾時就 SIGKILL"""
    import signal
    _QWEN["stopping"] = True
    p = _QWEN.get("proc")
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        pass
    t0 = time.monotonic()
    while wait and p.poll() is None and time.monotonic() - t0 < wait:
        time.sleep(0.2)
    if wait and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(5)
        except Exception:
            pass


def _qwen_ready():
    p = _QWEN.get("proc")
    return bool(_QWEN.get("ready") and p is not None and p.poll() is None)


# ── 文字轉語音：worker 的啟動、閒置關閉（worker 本體見檔案前段 _tts_worker_main）──
_TTS_START_LOCK = threading.Lock()
_TTS_RUN_LOCK = threading.Lock()          # 合成一次一件（GPU 共用；辨識有自己的排隊）
TTS_IDLE = float(os.environ.get("JT_TTS_IDLE") or 1800)
TTS_MIN_MEM_GB = float(os.environ.get("JT_TTS_MIN_MEM_GB") or 10)   # 實測常駐約 9.5 GB、載入峰值 11.8 GB（含 GPU）
BREEZY_MIN_MEM_GB = float(os.environ.get("JT_BREEZY_MIN_MEM_GB") or 8)
# 兩個合成模型各一個 worker：各自第一次用到才啟動、閒置關閉、一次一件；彼此不排隊（BreezyVoice 慢，不擋 VoxCPM2）
_TTS = {"proc": None, "port": None, "ready": False, "error": "", "last_used": 0.0, "stopping": False,
        "key": "voxcpm2", "name": "文字轉語音", "flag": "--tts-worker", "venv": "venv-tts", "py_env": "JT_TTS_PYTHON",
        "min_mem": TTS_MIN_MEM_GB, "start_lock": _TTS_START_LOCK, "run_lock": _TTS_RUN_LOCK,
        "setup_hint": "在用戶端重新執行安裝程式，設定 GPU 伺服器的文字轉語音"}
_BREEZY = {"proc": None, "port": None, "ready": False, "error": "", "last_used": 0.0, "stopping": False,
           "key": "breezyvoice", "name": "BreezyVoice", "flag": "--breezy-worker", "venv": "venv-breezy",
           "py_env": "JT_BREEZY_PYTHON", "min_mem": BREEZY_MIN_MEM_GB, "start_lock": threading.Lock(),
           "run_lock": threading.Lock(),
           "setup_hint": "在用戶端執行安裝程式（或 --upgrade），問到「是否加裝 BreezyVoice」時回答 y"}
_TTS_WORKERS = {"voxcpm2": _TTS, "breezyvoice": _BREEZY}


def _tts_python(w=_TTS):
    p = os.environ.get(w["py_env"]) or os.path.expanduser(f"~/jt-whisper-server/{w['venv']}/bin/python")
    return p if os.path.exists(p) else None


def _tts_mem_available_gb():
    """/proc/meminfo 的 MemAvailable（GB）；讀不到回 None。GB10 的 CPU 與 GPU 共用這塊記憶體"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 ** 2
    except OSError:
        pass
    return None


def _tts_unavailable(w=_TTS):
    """這台不能跑這個合成模型的原因；可以跑回 None"""
    if not _tts_python(w):
        return f"GPU 伺服器沒有裝{w['name']}（{w['venv']}）：{w['setup_hint']}"
    if w is _BREEZY and not os.path.exists(os.path.join(BREEZY_DIR, ".jtlw-rev")):
        return f"GPU 伺服器的 BreezyVoice 原始碼不完整（{BREEZY_DIR}）：{w['setup_hint']}"
    if not torch.cuda.is_available():
        return "GPU 伺服器沒有可用的顯示卡"
    return None


def _tts_ready(w=_TTS):
    p = w.get("proc")
    return bool(w.get("ready") and p is not None and p.poll() is None)


def _tts_ensure(timeout=300, w=_TTS):
    """worker 沒在跑就啟動並等它就緒；回傳 None（就緒）或錯誤說明。第一次用到才啟動、閒置由 _tts_idle_watch 關閉"""
    import subprocess
    import urllib.error
    import urllib.request
    why = _tts_unavailable(w)
    if why:
        return why
    name = w["name"]
    with w["start_lock"]:
        if _tts_ready(w):
            return None
        p = w.get("proc")
        if p is None or p.poll() is not None:
            avail = _tts_mem_available_gb()
            if avail is not None and avail < w["min_mem"]:
                return (f"GPU 伺服器可用記憶體只剩 {avail:.1f} GB，{name}需要約 {w['min_mem']:.0f} GB；"
                        f"請稍後再試（其他服務的模型閒置後會釋放）")
            port = w["port"]
            if _qwen_port_busy(port):
                return f"埠號 {port} 已被佔用（可能是上一次沒收乾淨的 worker）；設 {w['py_env'].replace('PYTHON', 'PORT')} 換一個"
            env = dict(os.environ)
            log = open(os.path.join(tempfile.gettempdir(), f"jt-tts-worker-{port}.log"), "ab")
            p = subprocess.Popen([_tts_python(w), os.path.abspath(__file__), w["flag"], str(port)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
            w.update(proc=p, ready=False, error="", stopping=False, log=log.name)
            print(f"[{name}] worker 啟動中（pid {p.pid}，127.0.0.1:{port}）")
        t0 = time.monotonic()
        while p.poll() is None and time.monotonic() - t0 < timeout:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{w['port']}/health", timeout=3):
                    w.update(ready=True, error="", last_used=time.time())
                    print(f"[{name}] 就緒（{time.monotonic() - t0:.0f}s）")
                    return None
            except urllib.error.HTTPError as e:
                if e.code == 500:              # worker 載入失敗：它自己說明原因
                    try:
                        msg = json.loads(e.read()).get("error", "")
                    except Exception:
                        msg = ""
                    _tts_stop(wait=10, w=w)
                    w["error"] = f"{name}模型載入失敗：{msg}（見 {w.get('log')}）"
                    return w["error"]
            except Exception:
                pass
            time.sleep(1)
        if p.poll() is not None:
            w["error"] = f"{name} worker 結束（代碼 {p.returncode}），見 {w.get('log')}"
        else:
            _tts_stop(wait=10, w=w)
            w["error"] = f"{name}模型 {timeout} 秒內沒有載入完成，見 {w.get('log')}"
        return w["error"]


def _tts_stop(wait=0.0, w=_TTS):
    """收掉 worker 整個行程群組。wait>0 時等它真的結束，逾時就 SIGKILL"""
    import signal
    w["stopping"] = True
    w["ready"] = False
    p = w.get("proc")
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        pass
    t0 = time.monotonic()
    while wait and p.poll() is None and time.monotonic() - t0 < wait:
        time.sleep(0.2)
    if wait and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(5)
        except Exception:
            pass


def _tts_stop_all(wait=0.0):
    """兩個合成 worker 都收掉（os.execv 前、結束時）"""
    for w in _TTS_WORKERS.values():
        _tts_stop(wait=wait, w=w)


def _tts_idle_check(w):
    """閒置 TTS_IDLE 秒就關掉 worker。**拿到 run_lock 才關、關完才放**：請求也是先拿 run_lock 再確認 worker 在，
    兩邊不會交錯（以前只看「鎖沒被拿走」就關，關的那 20 秒內進來的請求會等到一個正在結束的 worker、回「worker 結束」，
    朗讀產生 0 位元組；2026-10-10 守門用短閒置時間時抓到，正式機閒置 30 分鐘後的第一個請求也可能碰到）。回傳有沒有關"""
    if not _tts_ready(w) or time.time() - w["last_used"] <= TTS_IDLE:
        return False
    if not w["run_lock"].acquire(blocking=False):
        return False
    try:
        if not _tts_ready(w) or time.time() - w["last_used"] <= TTS_IDLE:
            return False
        print(f"[{w['name']}] 閒置 {int(TTS_IDLE // 60)} 分鐘，關閉 worker")
        _tts_stop(wait=20, w=w)
        return True
    finally:
        w["run_lock"].release()


def _tts_idle_watch():
    """閒置 TTS_IDLE 秒就關掉 worker，把記憶體還給共用 GPU（下次用到再啟動）"""
    while True:
        time.sleep(30)
        for w in _TTS_WORKERS.values():
            _tts_idle_check(w)


def _tts_voice_paths(sha):
    d = os.path.join(TTS_DIR, "voices")
    return os.path.join(d, f"{sha}.wav"), os.path.join(d, f"{sha}.txt")


def _transcribe_qwen(wav_path, language):
    """切窗 → worker 辨識＋對齊 → 切句。回傳 (segments, duration, proc_time)"""
    import librosa
    import urllib.error
    import urllib.request
    t0 = time.monotonic()
    wav, _ = librosa.load(wav_path, sr=16000, mono=True)
    windows = _nan_vad_windows(wav, 16000)
    body = json.dumps({"path": wav_path, "windows": windows, "language": _QWEN_LANG[language]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{_QWEN['port']}/transcribe", data=body,
                                 headers={"Content-Type": "application/json"})
    # 逾時依音訊長度（vLLM 約 50 倍即時，這裡給到 1 倍即時＋5 分鐘，只擋真的卡死）
    try:
        with urllib.request.urlopen(req, timeout=300 + len(wav) / 16000) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Qwen3-ASR worker 回報錯誤：{e.read().decode(errors='replace')[:200]}") from e
    except Exception as e:
        # 原始訊息（例：Remote end closed connection）轉給用戶端會看起來像主服務斷線，講清楚是 worker
        raise RuntimeError(f"Qwen3-ASR worker 中途沒有回應（{type(e).__name__}），它會自動重啟") from e
    if res.get("align_failed"):
        print(f"[Qwen3-ASR] {res['align_failed']} 窗對齊失敗，這些段落的時間以整窗估計")
    segs = []
    for (ws, we), txt, st in zip(windows, res["texts"], res["stamps"]):
        if not txt.strip() or _qwen_filler_only(txt):
            continue
        segs += _qwen_sentences(txt, st, ws, we)
    return segs, len(wav) / 16000, round(time.monotonic() - t0, 1)


# ── Nemotron 3 Diarization ──
# **與 translate_meeting.py 的同名函式是同一套邏輯**（這支伺服器自動更新只推單一檔案，不能共用模組）。
# tools/test_diarizer.py 逐一比對兩邊輸出；改一邊一定要改另一邊。實測數據見那邊的註解。
NEMO_DIAR_MODEL = "nvidia/Nemotron-3-Diarization"
_NEMO_FRAME = 0.01
_NEMO_CHANNELS = 8
_NEMO_CACHE = {}


def _recommended_diarizer(num_speakers=None, engine="auto"):
    """回傳 (engine, 原因)，engine 為 "nemotron" 或 "legacy"（伺服器版：不必判斷 Intel Mac）"""
    if engine == "legacy":
        return "legacy", "指定使用現行方法"
    if num_speakers and num_speakers > _NEMO_CHANNELS:
        return "legacy", f"指定 {num_speakers} 人，超過 Nemotron 上限 {_NEMO_CHANNELS} 人"
    if not _HAS_NEMO:
        return "legacy", "伺服器的 transformers 不支援 Nemotron"
    return "nemotron", ""


# v2.26.1（api_revision 2.6，JTDT 要求）：auto 退回現行方法時給機器看的代碼，呼叫端翻成自己的語言；note 照舊給人看。
# **與 translate_meeting.py 的同名函式相同**（tools/test_diarizer.py 比對）
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
    a = int(seg["start"] / _NEMO_FRAME)
    b = max(a + 1, int(seg["end"] / _NEMO_FRAME))
    b = min(b, probs_len)
    a = min(a, b - 1)
    return max(a, 0), max(b, 1)


def _nemo_segment_labels(probs, segments):
    import numpy as np
    out = []
    for s in segments:
        a, b = _nemo_span(len(probs), s)
        out.append(int(np.asarray(probs[a:b], dtype="float32").sum(axis=0).argmax()))
    return out


def _nemo_saturated(segments, labels):
    used = {l for s, l in zip(segments, labels) if s["end"] - s["start"] >= _DIAR_MIN_CLUSTER_SEC}
    return len(used) >= _NEMO_CHANNELS


def _nemo_limit_speakers(probs, segments, labels, k):
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
    m = {}
    return [m.setdefault(l, len(m)) for l in labels]


def _nemo_probs(wav_path):
    import librosa
    import numpy as np
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    if "model" not in _NEMO_CACHE:
        proc = AutoProcessor.from_pretrained(NEMO_DIAR_MODEL)
        model = AutoModelForAudioFrameClassification.from_pretrained(NEMO_DIAR_MODEL).to(_torch_device).eval()
        _NEMO_CACHE.update(proc=proc, model=model)
    proc, model = _NEMO_CACHE["proc"], _NEMO_CACHE["model"]
    wav, _ = librosa.load(wav_path, sr=16000, mono=True)
    inp = {k: (v.to(_torch_device) if hasattr(v, "to") else v) for k, v in proc(wav, sampling_rate=16000).items()}
    with torch.inference_mode():
        lg = model(**inp).logits[0].float().cpu().numpy()
    return lg if (lg.min() >= 0 and lg.max() <= 1) else 1 / (1 + np.exp(-lg))


def _nemotron_diarize(wav_path, segments, num_speakers=None):
    """回傳 (labels, 原因)，與用戶端同一份規則：8 位全滿時 labels 是 Nemotron 的結果、原因不是空的，
    由 _diarize 用現行方法再分一次，分出超過 8 位才改用（_saturated_prefers_legacy，v2.26.4）"""
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
    """Nemotron 8 位全滿時，現行方法的結果要分出超過 8 位才採用（與用戶端相同，tools/test_diarizer.py 比對）"""
    return legacy_labels is not None and len(set(legacy_labels)) > _NEMO_CHANNELS


def _diarize(wav_path, segments, num_speakers=None, engine="auto"):
    """講者辨識入口，回傳 (labels, 實際用的方法, 說明, 8 位是否全滿)。labels 失敗為 None。
    全滿（saturated，v2.26.5）：Nemotron 的 8 個位置都用到，不論最後採用哪一種方法都是 True，
    呼叫端（JTDT）拿它提醒「實際發言者更多時請填人數」"""
    choice, why = _recommended_diarizer(num_speakers, engine)
    if choice == "nemotron":
        labels, why = _nemotron_diarize(wav_path, segments, num_speakers)
        saturated = labels is not None and bool(why)
        if saturated:                           # 8 位全滿：現行方法再分一次，分出更多人才用它
            legacy = _diarize_legacy(wav_path, segments, num_speakers=num_speakers) if _HAS_DIARIZE else None
            if _saturated_prefers_legacy(legacy):
                print(f"[diarize] 改用現行方法：{why}（現行方法分出 {len(set(legacy))} 位）")
                return legacy, "legacy", why, True
            print(f"[diarize] Nemotron 8 位全滿，現行方法只分出 {len(set(legacy)) if legacy else 0} 位，採用 Nemotron")
            why = ""
        if labels is not None:
            print(f"[diarize] Nemotron（{_torch_device}）{len(set(labels))} 位講者")
            return labels, "nemotron", "", saturated
        print(f"[diarize] 改用現行方法：{why}")
    if not _HAS_DIARIZE:
        return None, "legacy", why or "resemblyzer/spectralcluster 未安裝", False
    note = why if (engine == "nemotron" or choice == "nemotron"
                   or (num_speakers and num_speakers > _NEMO_CHANNELS)) else ""
    return _diarize_legacy(wav_path, segments, num_speakers=num_speakers), "legacy", note, False


def _diarize_legacy(wav_path, segments, num_speakers=None):
    """用 resemblyzer + spectralcluster 辨識講者。
    segments: list of dict，每個含 start, end, text
    回傳: list of int（講者編號 0-based），失敗回傳 None
    """
    from resemblyzer import VoiceEncoder, preprocess_wav
    from spectralcluster import SpectralClusterer, refinement, laplacian
    from spectralcluster import utils as sc_utils

    if not segments:
        return None

    # 載入音訊。
    # **不可以用 preprocess_wav(wav_path) 整檔載入**：它除了重取樣還會做 VAD 靜音
    # 修剪並「刪掉」那些樣本（安裝的版本連 trim_silence 參數都沒有，一定會修），
    # 但下面是用**原始時間軸**去切段落——實測 AMI 一場 18.5 分鐘的會議被刪掉
    # 31.9%，檔尾偏移將近 6 分鐘，等於拿會議別處的聲音去比對。
    # 用戶端 v2.20.0 已修，但伺服器這份是獨立副本，2026-09-21 才發現沒跟到
    # （有 GPU 伺服器時預設就走這條路徑，等於多數使用者一直拿到壞的結果）。
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

    # 初始化聲紋編碼器（有 GPU 就用 GPU）
    encoder = VoiceEncoder(_torch_device)
    print(f"[diarize] 提取聲紋（{len(segments)} 段, device={_torch_device}）")

    # ── 只有夠長的段落才進分群 ──
    # 1.6 秒是 resemblyzer 的 partial utterance 長度：短於它時 embed_utterance
    # 會把音訊補零到 1.6s 再算，那個聲紋不可靠。2026-09-22 用有標準答案的中文
    # 會議量到——標錯率 <1.0s 63.6%、1.0~1.6s 66.9%，而 1.6~2.5s 只有 14.8%、
    # 2.5~4.0s 是 0.0%。短段落只佔 24% 的秒數卻貢獻 64% 的「講者搞錯」，
    # 而且它們一起進 affinity 矩陣，把長段落的分群也一起帶壞。
    # 原本的兩個補救（<0.5s 撐成 0.5s 視窗、連續 <0.8s 合併共用一個 embedding）
    # 方向是反的：合併等於強迫相鄰短段落同一個講者，搶話時它們多半不是。
    cluster_floor = _diar_cluster_floor(segments)

    # 逐段提取聲紋
    embeddings = []
    valid_indices = []

    for i, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        if duration < cluster_floor:
            embeddings.append(None)
            continue

        audio_slice = wav[int(seg["start"] * sr):int(seg["end"] * sr)]

        if len(audio_slice) < int(0.3 * sr):
            embeddings.append(None)
            continue
        if _per_segment_trim:
            audio_slice = preprocess_wav(audio_slice, source_sr=sr)
            if len(audio_slice) < int(0.3 * sr):
                embeddings.append(None)
                continue

        try:
            if duration >= 1.6:
                emb, partials, _ = encoder.embed_utterance(
                    audio_slice, return_partials=True, rate=1.6, min_coverage=0.75
                )
                emb = np.median(partials, axis=0)
                emb = emb / np.linalg.norm(emb)
            else:
                emb = encoder.embed_utterance(audio_slice)
            embeddings.append(emb)
            valid_indices.append(i)
        except Exception:
            embeddings.append(None)

    if not valid_indices:
        print("[diarize] 無法提取任何有效聲紋")
        return None

    print(f"[diarize] 分群辨識（{len(valid_indices)} 有效段落）")

    # 組合有效 embedding 矩陣
    valid_embeddings = np.array([embeddings[i] for i in valid_indices])

    # SpectralClusterer 分群
    min_clusters = 2 if num_speakers is None else num_speakers
    max_clusters = 8 if num_speakers is None else num_speakers

    refinement_opts = refinement.RefinementOptions(
        # gaussian_blur_sigma=0：不要模糊。高斯模糊假設相鄰列是時間上連續的等寬
        # 視窗，但我們送的是「已合併的講者連續發言」，模糊會抹掉講者交界。
        # 18 場 AMI 實測：blur=1 → DER 43.60%、blur=0 → 16.25%
        gaussian_blur_sigma=0,
        p_percentile=0.98,
        thresholding_soft_multiplier=0.01,
        thresholding_type=refinement.ThresholdType.RowMax,
        symmetrize_type=refinement.SymmetrizeType.Max,
        # 沒有這個參數，上面五個全是死的：預設 None 時整組 refinement 一步都不跑
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
            # 不指定時用 affinity 直接分解，特徵值間隙幾乎總是落在 k=2
            laplacian_type=laplacian.LaplacianType.GraphCut,
            # NormalizedDiff：預設的 Ratio 是「後一個特徵值 / 前一個」，分母很靠近
            # 0 時比值爆大，於是永遠挑最小的 k。會議越長段落越多、譜越平滑，偏誤
            # 越嚴重——中文 37 分鐘那場 1043 段一律吐 k=2（實際 7 人），混淆 40.20%；
            # 改用 NormalizedDiff 後判 3 群、22.90%。
            # **兩個改動必須一起上**（見上面 cluster_floor）。真實 ASR 切段實測：
            # 只換 eigengap 幾乎沒有作用；只換門檻會讓英文 ES2011a 的混淆率
            # 由 12.44% 惡化到 23.29%。一起上才是 12.44% → 9.47%。
            eigengap_type=sc_utils.EigenGapType.NormalizedDiff,
        )
        cluster_labels = clusterer.predict(valid_embeddings)
    except Exception as e:
        print(f"[diarize] 分群失敗: {e}，所有段落標記為 Speaker 1")
        return [0] * len(segments)

    # ── 餘弦相似度二次校正 ──
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
        if reassigned > 0:
            print(f"[diarize] 餘弦校正 {reassigned} 段")

    # 映射回所有段落
    speaker_labels = [None] * len(segments)
    for idx, valid_idx in enumerate(valid_indices):
        speaker_labels[valid_idx] = int(cluster_labels[idx])

    # 填補跳過的段落
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

    # 按首次出現順序重新編號
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
    print(f"[diarize] 完成（{n_speakers} 位講者）")

    return speaker_labels


# ── 模型載入 ──

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


def _get_model_faster(model_size: str):
    """faster-whisper 模型"""
    from faster_whisper import WhisperModel
    _fw_av_compat()
    key = f"fw:{model_size}"
    if key not in _models:
        print(f"[載入模型] {model_size} (faster-whisper, device={_device}, compute={_compute_type})")
        _models[key] = WhisperModel(model_size, device=_device, compute_type=_compute_type)
        print(f"[模型就緒] {model_size}")
    return _models[key]


def _get_model_openai(model_size: str):
    """openai-whisper 模型"""
    import whisper as openai_whisper
    # openai-whisper 模型名稱對應：large-v3-turbo → turbo, large-v3 → large
    name_map = {
        "large-v3-turbo": "turbo",
        "large-v3": "large-v3",
        "medium.en": "medium.en",
        "small.en": "small.en",
        "base.en": "base.en",
    }
    ow_name = name_map.get(model_size, model_size)
    key = f"ow:{ow_name}"
    if key not in _models:
        print(f"[載入模型] {ow_name} (openai-whisper, device={_torch_device})")
        _models[key] = openai_whisper.load_model(ow_name, device=_torch_device)
        print(f"[模型就緒] {ow_name}")
    return _models[key], ow_name


# ── 辨識函式 ──

# faster-whisper 離線辨識參數（含長音檔幻覺防護，與用戶端 _FW_OFFLINE_KW 一致）
# - condition_on_previous_text=False：切斷上一段 prompt 傳染
# - hallucination_silence_threshold=2.0：偵測到幻覺時跳過 ≥2s 靜音（需 word_timestamps=True）
# - repetition_penalty=1.05：抑制連續重複片段
_FW_KW = dict(
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

# 寬鬆模式參數（用戶端帶 noisy=1 時切換，對應低音量/監視器/行車紀錄類音源）
# 用戶端在上傳前已做音量增益，伺服器只需放寬 VAD / no_speech / log_prob
_FW_KW_LOOSE = dict(
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


def _transcribe_faster(wav_path, model_size, language, noisy=False):
    """faster-whisper 辨識"""
    m = _get_model_faster(model_size)
    t0 = time.monotonic()
    kw = _FW_KW_LOOSE if noisy else _FW_KW
    segments_iter, info = m.transcribe(wav_path, language=language, **kw)
    segments = []
    full_text = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text})
            full_text.append(text)
    return segments, full_text, round(info.duration, 1), round(time.monotonic() - t0, 1)


def _detect_language_faster(wav_path, model_size):
    """只判斷語言、不辨識（雙向語音口譯，v2.28.0）：faster-whisper 的 transcribe 不給語言時，回傳之前就先判斷好語言，
    產生器不去跑就不會辨識。回傳 (語言, 機率)"""
    m = _get_model_faster(model_size)
    _, info = m.transcribe(wav_path, language=None, beam_size=1, without_timestamps=True, vad_filter=False)
    return info.language, float(info.language_probability or 0)


def _transcribe_faster_stream(wav_path, model_size, language, noisy=False):
    """faster-whisper 串流版，yield (segment_dict, duration) per segment"""
    m = _get_model_faster(model_size)
    kw = _FW_KW_LOOSE if noisy else _FW_KW
    segments_iter, info = m.transcribe(wav_path, language=language, **kw)
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            out = {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text,
                   "language": info.language}
            # 平均對數機率換算成 0～1，供用戶端（REST API 的 confidence）相對比較用
            if getattr(seg, "avg_logprob", None) is not None:
                out["confidence"] = round(min(1.0, max(0.0, math.exp(seg.avg_logprob))), 4)
            yield out, info.duration


# ── 台語（Breeze-ASR-26，v2.25.2）─────────────────────────────────────────
# REST API 的台語模式送到這裡（本機 CPU 跑一小時的會議要約 4 小時）。用戶端送來的 model 是
# translate_meeting._resolve_fw_model(remote=True) 轉好的 repo 名稱。處理方式與用戶端的
# _nan_transcribe_windows **完全相同**（tools/test_breeze_server.py 逐一比對）：
#   - language 一律 "en"：微調沿用 <|en|> token
#   - _FW_NAN_KW：專案的防幻覺參數組（上面的 _FW_KW）會讓台語 CER 17.99% → 56.42%、慢 4.7 倍
#   - 模型不產生時間戳：先用 _nan_vad_windows 切成 ≤28 秒視窗，時間取自視窗邊界
_BREEZE_REPO = "paulpengtw/faster-whisper-Breeze-ASR-26"
_BREEZE_MODELS = (_BREEZE_REPO, "breeze-asr-26")
_BREEZE_WHISPER_LANG = "en"
_FW_NAN_KW = dict(
    beam_size=5,
    condition_on_previous_text=False,
    vad_filter=False,
    word_timestamps=False,
)


def _transcribe_breeze_stream(wav_path):
    """台語辨識串流版：逐視窗辨識，yield (segment_dict, duration)。language 標成 "nan"（用戶端據此標台語）"""
    from faster_whisper.audio import decode_audio
    _fw_av_compat()
    m = _get_model_faster(_BREEZE_REPO)
    audio = decode_audio(wav_path, sampling_rate=16000)
    duration = len(audio) / 16000.0
    for w_start, w_end in _nan_vad_windows(audio, 16000):
        chunk = audio[int(w_start * 16000):int(w_end * 16000)]
        if not len(chunk):
            continue
        segs, _info = m.transcribe(chunk, language=_BREEZE_WHISPER_LANG, **_FW_NAN_KW)
        text = "".join(x.text for x in segs).strip()
        if text:
            yield {"start": round(w_start, 3), "end": round(w_end, 3), "text": text, "language": "nan"}, duration


class _ProgressCapture:
    """攔截 stdout，解析 openai-whisper verbose 輸出追蹤辨識進度。
    whisper verbose=True 每段輸出格式: [00:00.000 --> 00:30.000]  text..."""

    _TS_RE = re.compile(r'\[[\d:.]+\s*-->\s*([\d:.]+)\]')

    def __init__(self, original, progress_q, audio_duration):
        self._orig = original
        self._q = progress_q
        self._duration = audio_duration

    def write(self, text):
        self._orig.write(text)
        m = self._TS_RE.search(text)
        if m and self._duration > 0:
            secs = self._parse_ts(m.group(1))
            if secs is not None:
                pct = min(secs / self._duration, 1.0)
                self._q.put(("progress", secs, self._duration, pct))
        return len(text) if text else 0

    @staticmethod
    def _parse_ts(ts_str):
        parts = ts_str.split(':')
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            pass
        return None

    def flush(self):
        self._orig.flush()


def _transcribe_openai(wav_path, model_size, language, progress_q=None, noisy=False):
    """openai-whisper 辨識。progress_q: Queue，用於回報辨識進度。
    noisy=True：低音量音源切換寬鬆參數（no_speech 0.3、停用 logprob 過濾）。"""
    m, ow_name = _get_model_openai(model_size)

    # 取得音訊時長
    audio_duration = 0
    if progress_q is not None:
        try:
            import whisper as _ow
            audio = _ow.load_audio(wav_path)
            audio_duration = len(audio) / 16000
            progress_q.put(("duration", audio_duration))
        except Exception:
            pass

    t0 = time.monotonic()

    # openai-whisper 防幻覺參數（API 與 faster-whisper 略有不同）
    if noisy:
        _ow_kw = dict(
            beam_size=5,
            condition_on_previous_text=False,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            compression_ratio_threshold=2.4,
            # 與 _FW_KW_LOOSE 同一個修正：門檻調低＝更容易整段跳過，
            # 而 logprob_threshold=None 會關掉唯一的救援（openai-whisper 同邏輯）
            logprob_threshold=-2.0,
            no_speech_threshold=0.6,
        )
    else:
        _ow_kw = dict(
            beam_size=5,
            condition_on_previous_text=False,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
        )

    # 有 progress_q 時用 verbose=True + stdout 攔截追蹤進度
    if progress_q is not None and audio_duration > 0:
        old_stdout = sys.stdout
        sys.stdout = _ProgressCapture(old_stdout, progress_q, audio_duration)
        try:
            result = m.transcribe(wav_path, language=language, verbose=True, **_ow_kw)
        finally:
            sys.stdout = old_stdout
    else:
        result = m.transcribe(wav_path, language=language, **_ow_kw)

    segments = []
    full_text = []
    for seg in result.get("segments", []):
        text = seg["text"].strip()
        if text:
            segments.append({"start": round(seg["start"], 3), "end": round(seg["end"], 3), "text": text})
            full_text.append(text)
    # openai-whisper 不直接回傳 duration，從最後一段取
    duration = round(segments[-1]["end"], 1) if segments else 0
    return segments, full_text, duration, round(time.monotonic() - t0, 1)


# ── API ──

@app.get("/health")
def health():
    """健康檢查"""
    return {
        "status": "ok",
        "version": SERVER_VERSION,
        "gpu": _device == "cuda",
        "device": _device,
        "backend": _backend,
        "diarize": _HAS_DIARIZE or _HAS_NEMO,
        # Qwen3-ASR（實驗）：null＝這台沒裝；ready=false＝載入中或啟動失敗（見 error）
        "qwen": ({"ready": _qwen_ready(), "model": "qwen3-asr-0.6b", "languages": list(_QWEN_LANG),
                  "error": _QWEN["error"], "restarts": _QWEN["restarts"]}
                 if (_QWEN["proc"] is not None or _QWEN["error"]) else None),
        # 台語（v2.25.2）：只有 faster-whisper 後端能跑 Breeze-ASR-26；null＝這台不支援
        "taiwanese": ({"model": "breeze-asr-26"} if _backend == "faster-whisper" else None),
        # 講者辨識可用的方法；auto 時優先 nemotron
        "diar_engines": [e for e, ok in (("nemotron", _HAS_NEMO), ("legacy", _HAS_DIARIZE)) if ok],
        # 用戶端用這個判斷「能不能自動更新」，不必試了才知道
        "can_update": bool(UPDATE_TOKEN),
        # v2.21.7 起一次一件、其餘排隊；用戶端據此決定要不要問「等候／改用本機」
        "queue": True,
        # v2.21.8：有排定的更新時用戶端先等它換完再送件（null＝沒有）
        "update_pending": _update_pending_info(),
        # 文字轉語音（2026-10）：null＝這台沒裝 venv-tts；詳細狀態見 /v1/tts/health
        "tts": ({"model": TTS_MODEL, "running": _tts_ready(),
                 "models": [k for k, w in _TTS_WORKERS.items() if _tts_python(w)]}
                if any(_tts_python(w) for w in _TTS_WORKERS.values()) else None),
    }


# ── 文字轉語音（2026-10）：用戶端先切句，一次送一句；聲音以 sha256 快取（先問有沒有，沒有才上傳）──
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TTS_MAX_CHARS = 300          # 一次一句（用戶端以 80 字切句）；擋掉沒切句就整篇送來的
TTS_VOICE_MAX_BYTES = 20 * 1024 * 1024


_TTS_MODEL_REPO = {"voxcpm2": TTS_MODEL, "breezyvoice": BREEZY_MODEL}


def _tts_model_health(w):
    why = _tts_unavailable(w)
    p = w.get("proc")
    return {"available": why is None, "reason": why, "model": _TTS_MODEL_REPO[w["key"]], "running": _tts_ready(w),
            "loading": bool(p is not None and p.poll() is None and not w["ready"]), "error": w["error"],
            "min_mem_gb": w["min_mem"]}


@app.get("/v1/tts/health")
def tts_health():
    """最上層的欄位是 VoxCPM2（v2.27.0 第一版的格式，舊用戶端照讀）；models 是每個合成模型各自的狀態"""
    avail = _tts_mem_available_gb()
    out = _tts_model_health(_TTS)
    out.update(idle_seconds=TTS_IDLE, mem_available_gb=round(avail, 1) if avail is not None else None,
               models={k: _tts_model_health(w) for k, w in _TTS_WORKERS.items()},
               langs=["zh", "en"], stream=True)          # v2.28.0：英文句子、串流合成（雙向口譯），只有 VoxCPM2
    return out


@app.get("/v1/tts/voices/{sha}")
def tts_voice_exists(sha: str):
    if not _SHA_RE.match(sha) or not all(os.path.exists(x) for x in _tts_voice_paths(sha)):
        return JSONResponse({"error": "voice_not_found"}, status_code=404)
    return {"voice": sha}


@app.delete("/v1/tts/voices/{sha}")
def tts_voice_delete(sha: str):
    """用戶端刪除聲音時一併刪掉這裡快取的參考錄音與逐字稿（錄音者可以要求刪除：不可以只刪用戶端那份）。
    別台用戶端還在用同一段錄音的話，下次合成時會自動重新上傳"""
    if not _SHA_RE.match(sha):
        return JSONResponse({"error": "invalid_voice"}, status_code=400)
    gone = False
    for p in _tts_voice_paths(sha):
        try:
            os.remove(p)
            gone = True
        except FileNotFoundError:
            pass
    return {"voice": sha, "deleted": gone}


@app.post("/v1/tts/voices")
async def tts_voice_upload(file: UploadFile = File(...), text: str = Form(...)):
    """上傳參考錄音（WAV／FLAC，3～60 秒）＋一字不差的逐字稿 → {voice: sha256}"""
    import hashlib
    import io
    import soundfile as sf
    data = await file.read(TTS_VOICE_MAX_BYTES + 1)
    text = (text or "").strip()
    if len(data) > TTS_VOICE_MAX_BYTES:
        return JSONResponse({"error": "voice_too_large", "detail": "參考錄音超過 20 MB"}, status_code=400)
    if not text or len(text) > 1000:
        return JSONResponse({"error": "invalid_transcript", "detail": "逐字稿不可空白、不可超過 1000 字"}, status_code=400)
    try:
        info = sf.info(io.BytesIO(data))
    except Exception:
        return JSONResponse({"error": "invalid_audio", "detail": "讀不懂這個音檔，請轉成 WAV 再上傳"}, status_code=400)
    if not 3 <= info.duration <= 60:
        return JSONResponse({"error": "invalid_audio", "detail": f"參考錄音要 3～60 秒，這段是 {info.duration:.1f} 秒"},
                            status_code=400)
    sha = hashlib.sha256(data + b"\n" + text.encode()).hexdigest()
    wav, txt = _tts_voice_paths(sha)
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    for path, body in ((wav, data), (txt, text.encode())):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    return {"voice": sha, "duration": round(info.duration, 2)}


def _tts_worker_post(path, payload, timeout, w=_TTS):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{w['port']}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return 502, json.dumps({"error": f"{w['name']} worker 沒有回應（{type(e).__name__}）"}).encode(), {}


def _tts_request(payload, worker_path):
    w = _TTS_WORKERS.get(str(payload.get("model") or "voxcpm2"))
    if w is None:
        return None, JSONResponse({"error": "unknown_model", "detail": f"合成模型只有 {'、'.join(_TTS_WORKERS)}"},
                                  status_code=400)
    text = str(payload.get("text") or "").strip()
    if not text:
        return None, JSONResponse({"error": "empty_text"}, status_code=400)
    if len(text) > TTS_MAX_CHARS:
        return None, JSONResponse({"error": "text_too_long",
                                   "detail": f"一次最多 {TTS_MAX_CHARS} 字，請先切句"}, status_code=400)
    lang = str(payload.get("lang") or "zh")
    if lang not in ("zh", "en"):
        return None, JSONResponse({"error": "invalid_lang", "detail": "lang 只有 zh、en"}, status_code=400)
    if (lang == "en" or worker_path == "/synthesize_stream") and w is not _TTS:
        return None, JSONResponse({"error": "unsupported", "detail": f"英文與串流合成只有 VoxCPM2（{w['name']} 不支援）"},
                                  status_code=400)
    body = {"text": text, "custom": payload.get("custom") or {}, "lang": lang}
    if worker_path in ("/synthesize", "/synthesize_stream"):
        sha = str(payload.get("voice") or "")
        wav, txt = _tts_voice_paths(sha) if _SHA_RE.match(sha) else (None, None)
        if not wav or not os.path.exists(wav) or not os.path.exists(txt):
            return None, JSONResponse({"error": "voice_not_found"}, status_code=404)
        with open(txt, encoding="utf-8") as f:
            body.update(voice_wav=wav, voice_text=f.read(), steps=payload.get("steps") or 10,
                        cfg=payload.get("cfg") or 2.0)
    if worker_path == "/synthesize_stream":
        return _tts_stream_open(body, w)
    with w["run_lock"]:                         # 先拿鎖再確認 worker 在（閒置關閉也要拿這把鎖，見 _tts_idle_check）
        err = _tts_ensure(w=w)
        if err:
            return None, JSONResponse({"error": "tts_unavailable", "detail": err}, status_code=503)
        w["last_used"] = time.time()
        try:
            code, data, headers = _tts_worker_post(worker_path, body, 300, w)
        finally:
            w["last_used"] = time.time()
    return (code, data, headers), None


@app.post("/v1/tts/speech")
def tts_speech(payload: dict = Body(...)):
    """{text（一句，≤300 字）, voice（sha256）, custom（自訂讀音）, model（voxcpm2／breezyvoice，預設 voxcpm2）, steps, cfg}
    → audio/wav（VoxCPM2 48 kHz、BreezyVoice 22.05 kHz，單聲道）。
    標頭 X-TTS-Spoken：送進模型的文字（含 {拼音}，URL 編碼）；X-TTS-Duration／X-TTS-Seconds：音訊長度／合成耗時"""
    res, bad = _tts_request(payload, "/synthesize")
    if bad:
        return bad
    code, data, headers = res
    if code != 200:
        try:
            detail = json.loads(data).get("error", "")
        except Exception:
            detail = data[:200].decode(errors="replace")
        return JSONResponse({"error": "tts_failed", "detail": detail}, status_code=502 if code >= 500 else code)
    keep = {k: v for k, v in headers.items() if k.lower().startswith("x-tts-")}
    return Response(content=data, media_type="audio/wav", headers=keep)


def _tts_stream_open(body, w):
    """串流合成：拿到 worker 的第一段才回應（這之前出錯照一般錯誤回），之後一段一段轉送。
    合成程式一次一段：鎖拿到送完為止。成功時回 (200, (產生器, 收尾), 標頭)：
    用戶端中途斷線（按停止、網路斷）時 Starlette 只取消外層迭代、不關這個同步產生器，它的 finally 不會跑
    （還沒開始跑就斷線時連 close() 都不會進 finally）→ 鎖永遠不放、這台的合成全部卡住。
    所以呼叫端一定要用 BackgroundTask 跑「收尾」（回應結束、含斷線都會跑），比照辨識的串流（transcribe）"""
    import http.client
    import socket
    w["run_lock"].acquire()                     # 先拿鎖再確認 worker 在（同 _tts_request）
    err = _tts_ensure(w=w)
    if err:
        w["run_lock"].release()
        return None, JSONResponse({"error": "tts_unavailable", "detail": err}, status_code=503)
    w["last_used"] = time.time()
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", w["port"], timeout=300)
        conn.request("POST", "/synthesize_stream", json.dumps(body), {"Content-Type": "application/json"})
        sock = conn.sock                    # getresponse 之後 conn.sock 會變 None（Connection: close，連線交給回應）
        r = conn.getresponse()
        if r.status != 200:
            data = r.read()
            conn.close()
            w["last_used"] = time.time()
            w["run_lock"].release()
            return (r.status, data, dict(r.getheaders())), None
    except Exception as e:
        if conn is not None:
            conn.close()
        w["run_lock"].release()
        return (502, json.dumps({"error": f"{w['name']} worker 沒有回應（{type(e).__name__}）"}).encode(), {}), None

    released, rel_lock = [False], threading.Lock()

    def release():
        """只放一次（產生器跑完、收尾都會叫）。先 shutdown：另一條執行緒還卡在 read 的話會立刻讀到結尾，
        worker 寫不出去就停止合成（它自己也有一把鎖，下一句會等它停好）"""
        with rel_lock:
            if released[0]:
                return
            released[0] = True
        try:
            sock.shutdown(socket.SHUT_RDWR)     # 讀的執行緒立刻讀到結尾、自己關掉回應（這裡不碰 r，免得兩條執行緒同時關）
        except OSError:
            pass
        conn.close()
        w["last_used"] = time.time()
        w["run_lock"].release()

    chunks = queue.Queue()

    def reader():
        try:
            while True:
                b = r.read(9600)                # 48 kHz 16-bit 約 0.1 秒
                if not b:
                    break
                chunks.put(b)
        except (OSError, ValueError):           # worker 斷線、收尾時 shutdown
            pass
        finally:
            r.close()
            chunks.put(None)
    threading.Thread(target=reader, name="tts-stream-read", daemon=True).start()

    def gen():
        """每 0.5 秒至少交回一次（沒有新的就交空的，uvicorn 不會送出空區塊）：用戶端斷線時 Starlette 取消串流要等 next() 回來，
        直接在這裡讀 worker 的話，GPU 忙、兩段之間停很久時就要等那麼久才能放鎖"""
        try:
            while True:
                try:
                    b = chunks.get(timeout=0.5)
                except queue.Empty:
                    yield b""
                    continue
                if b is None:
                    break
                yield b
        finally:
            release()
    g = gen()

    def cleanup():
        try:
            g.close()
        except ValueError:                      # generator already executing：斷線時讀的那條執行緒還在 read
            pass
        release()
    return (200, (g, cleanup), dict(r.getheaders())), None


@app.post("/v1/tts/stream")
def tts_stream(payload: dict = Body(...)):
    """跟 /v1/tts/speech 一樣的參數，邊合成邊送 16-bit 單聲道 PCM（audio/L16；X-TTS-SR 取樣率）。
    只有 VoxCPM2；雙向口譯用（v2.28.0），第一段約 0.3 秒就送出、不用等整句"""
    res, bad = _tts_request(payload, "/synthesize_stream")
    if bad:
        return bad
    code, data, headers = res
    if code != 200:
        try:
            detail = json.loads(data).get("error", "")
        except Exception:
            detail = data[:200].decode(errors="replace")
        return JSONResponse({"error": "tts_failed", "detail": detail}, status_code=502 if code >= 500 else code)
    keep = {k: v for k, v in headers.items() if k.lower().startswith("x-tts-")}
    g, cleanup = data

    async def _cleanup_async():
        await run_in_threadpool(cleanup)
    return StreamingResponse(g, media_type="audio/L16", headers=keep, background=BackgroundTask(_cleanup_async))


@app.post("/v1/tts/convert")
def tts_convert(payload: dict = Body(...)):
    """{text, custom, model} → {spoken}：只做台灣念法轉換（檢查發音字典用）"""
    res, bad = _tts_request(payload, "/convert")
    if bad:
        return bad
    code, data, _ = res
    try:
        return JSONResponse(json.loads(data), status_code=code)
    except Exception:
        return JSONResponse({"error": "tts_failed"}, status_code=502)


@app.post("/v1/admin/update")
async def admin_update(request: Request):
    """用新版的 server.py 取代自己，驗證通過後重啟。

    **這個端點會執行對方送來的程式碼。** 下面的把關分成兩類，不要混為一談：

      「誰可以更新」——只有簽章這一關。沒有密鑰就完全不開放。
      「更新的東西會不會把服務弄死」——校驗碼、語法、selftest、忙碌檢查。
        這些擋的是**壞掉的**更新，**擋不住惡意的**更新（selftest 本身就會
        執行上傳的程式碼）。密鑰是唯一的安全邊界，請當成密碼保管。

    簽章用 HMAC 而不是直接送密鑰：這條連線是 HTTP 不是 HTTPS，
    直接送 Bearer token 的話，任何能側錄封包的人都拿得到可重複使用的憑證，
    等於拿到這台機器的任意程式碼執行權。HMAC 讓側錄者只能重放
    「同一份內容」（無害——那就是同一支程式），無法偽造新的 payload。
    做法與 jtlw_api 的 webhook 簽章一致：HMAC-SHA256 over "{timestamp}.{body}"。
    """
    global _UPDATE_PENDING
    import hashlib
    import hmac as _hmac
    import subprocess

    client_ip = request.client.host if request.client else "?"

    def _deny(code, status, **extra):
        print(f"[更新] 拒絕 {code} 來自 {client_ip}", flush=True)
        return JSONResponse({"error": code, **extra}, status_code=status)

    if not UPDATE_TOKEN:
        return _deny("update_disabled", 403,
                     detail="伺服器未設定 JT_WHISPER_UPDATE_TOKEN")

    # 先看 Content-Length 再決定要不要讀。await request.body() 會把整包讀進
    # 記憶體，沒有上限的話一個大 POST 就能把這台機器的記憶體吃光。
    try:
        clen = int(request.headers.get("content-length") or 0)
    except ValueError:
        clen = 0
    if clen > UPDATE_MAX_BYTES:
        return _deny("payload_too_large", 413,
                     detail=f"上限 {UPDATE_MAX_BYTES} bytes，收到 {clen}")

    ts = (request.headers.get("x-jtw-timestamp") or "").strip()
    sig = (request.headers.get("x-jtw-signature") or "").strip()
    if not ts or not sig:
        return _deny("unauthorized", 401, detail="缺少簽章標頭")
    try:
        skew = abs(time.time() - float(ts))
    except ValueError:
        return _deny("unauthorized", 401, detail="時間戳格式錯誤")
    if skew > 300:
        # 限制重放窗口；兩邊時鐘差太多也會落在這裡
        return _deny("unauthorized", 401, detail=f"時間戳超出容許範圍（差 {int(skew)}s）")

    body = await request.body()
    if len(body) > UPDATE_MAX_BYTES:
        return _deny("payload_too_large", 413, detail=f"上限 {UPDATE_MAX_BYTES} bytes")

    expect = "v1=" + _hmac.new(UPDATE_TOKEN.encode("utf-8"),
                               f"{ts}.".encode("utf-8") + body,
                               hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expect):
        return _deny("unauthorized", 401, detail="簽章不符")

    want_sha = (request.headers.get("x-content-sha256") or "").strip().lower()
    got_sha = hashlib.sha256(body).hexdigest()
    if want_sha and want_sha != got_sha:
        return _deny("checksum_mismatch", 400, expected=want_sha, actual=got_sha)

    # **有作業在跑時不拒絕**（v2.21.8）：先把驗證做完、排定，等作業做完才換。
    # 以前是回 409 busy 然後放棄——伺服器一直有人在用就永遠更新不了。

    me = os.path.abspath(__file__)
    # 每個請求用自己的暫存檔：兩個用戶端同時推更新時不會互相蓋掉對方正在驗證的檔案
    new_path = f"{me}.new-{os.getpid()}-{threading.get_ident()}-{int(time.time() * 1000)}"
    with open(new_path, "wb") as f:
        f.write(body)

    def _cleanup():
        try:
            os.remove(new_path)
        except OSError:
            pass

    # 驗證一：語法
    try:
        import ast
        ast.parse(open(new_path, encoding="utf-8").read())
    except SyntaxError as e:
        _cleanup()
        return _deny("invalid_syntax", 400, detail=str(e)[:200])

    new_ver = _parse_server_version(new_path)

    # **不接受降版**。多個用戶端共用同一台伺服器是常見情況；只比對「版本不同」
    # 的話，舊用戶端會把伺服器降回舊版，接著新用戶端又推回去——兩邊無限來回，
    # 而每次重啟都會中斷別人正在跑的辨識。
    if _version_tuple(new_ver) < _version_tuple(SERVER_VERSION):
        _cleanup()
        return _deny("downgrade_refused", 409,
                     detail=f"伺服器 {SERVER_VERSION} 比送來的 {new_ver} 新",
                     server_version=SERVER_VERSION, offered=new_ver)
    if _version_tuple(new_ver) == _version_tuple(SERVER_VERSION):
        _cleanup()
        return JSONResponse({"status": "already_current", "version": SERVER_VERSION},
                            status_code=200)
    with _UPDATE_LOCK:
        pend = dict(_UPDATE_PENDING) if _UPDATE_PENDING else None
    if pend and _version_tuple(new_ver) <= _version_tuple(pend["to"]):
        # 已經排定同版或更新的版本：不必再驗一次，告訴對方目前的排定狀態
        _cleanup()
        return JSONResponse(_update_state_body("scheduled"), status_code=202)

    # 驗證二：真的能啟動（import 得起來、設定沒寫壞）
    try:
        r = subprocess.run([sys.executable, new_path, "--selftest"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            _cleanup()
            return _deny("selftest_failed", 400, detail=(r.stderr or r.stdout)[-500:])
    except subprocess.TimeoutExpired:
        _cleanup()
        return _deny("selftest_timeout", 400)

    # 排定。**排定之後離線線立刻關門**：已經在跑、在排隊的照樣做完，新的回 503。
    # 即時線要到換檔前一刻才關，讓即時字幕只斷幾秒。
    staged = me + ".pending"
    with _UPDATE_LOCK:
        if _UPDATE_PENDING and _version_tuple(new_ver) <= _version_tuple(_UPDATE_PENDING["to"]):
            _cleanup()   # selftest 期間別人排定了同版或更新的版本
            return JSONResponse(_update_state_body("scheduled"), status_code=202)
        os.replace(new_path, staged)
        first = _UPDATE_PENDING is None
        _UPDATE_PENDING = {"to": new_ver, "from": SERVER_VERSION, "since": time.time(),
                           "client_ip": client_ip, "path": staged}
        _LANES["batch"].close()
    if first:
        threading.Thread(target=_update_worker, daemon=True).start()
    waiting = _LANES["batch"].count()
    print(f"[更新] 排定 {SERVER_VERSION} → {new_ver}，來自 {client_ip}，"
          f"{'立即換版' if waiting == 0 else f'等 {waiting} 件作業做完'}", flush=True)
    if waiting == 0:
        # 舊版用戶端只認得這個格式（看到就開始輪詢版本號）
        return {"status": "updating", "from": SERVER_VERSION, "to": new_ver,
                "restart_in_sec": 1}
    return JSONResponse(_update_state_body("scheduled"), status_code=202)


def _update_state_body(status):
    with _UPDATE_LOCK:
        pend = dict(_UPDATE_PENDING) if _UPDATE_PENDING else {}
    return {"status": status, "from": SERVER_VERSION, "to": pend.get("to"),
            "waiting": _LANES["batch"].count(),
            "since": round(pend["since"], 1) if pend.get("since") else None}


def _update_worker():
    """等作業做完再換版。

    順序很重要：**先關門、再等清空、最後換檔**。先等清空再關門的話，
    「清空」與「關門」之間進來的作業會在跑到一半時被換掉（v2.21.7 以前的 1 秒空窗
    就是這樣砍掉作業的，而用戶端還把它當成 0 段、成功）。
    """
    global _UPDATE_PENDING
    me = os.path.abspath(__file__)
    batch, rt = _LANES["batch"], _LANES["realtime"]
    deadline = time.time() + UPDATE_DRAIN_TIMEOUT

    def _abort(why):
        global _UPDATE_PENDING
        with _UPDATE_LOCK:
            pend = _UPDATE_PENDING
            _UPDATE_PENDING = None
        rt.reopen()
        batch.reopen()
        try:
            os.remove((pend or {}).get("path") or me + ".pending")
        except OSError:
            pass
        print(f"[更新] 放棄這次更新（{why}），恢復收件", flush=True)

    # 離線線在排定時就關了；這裡等它清空
    if not batch.wait_empty(deadline - time.time()):
        return _abort(f"等了 {UPDATE_DRAIN_TIMEOUT} 秒作業仍未做完")
    # 最後一刻才關即時線，並等正在辨識的那一小段做完（通常不到一秒）
    rt.close()
    if not rt.wait_empty(30):
        return _abort("即時辨識 30 秒內沒有做完")

    with _UPDATE_LOCK:
        pend = dict(_UPDATE_PENDING)
    backup = f"{me}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copy2(me, backup)
        os.replace(pend["path"], me)
        _prune_backups(me)
    except Exception as e:
        return _abort(f"換檔失敗：{e}")
    print(f"[更新] {SERVER_VERSION} → {pend['to']}，來自 {pend['client_ip']}，"
          f"備份 {os.path.basename(backup)}，重啟", flush=True)
    # 讓「立即換版」那次請求的回應先送出去。兩條線都關著、都是空的，
    # 這段時間進來的請求只會拿到 503，不會有作業被砍到一半。
    time.sleep(1.0)
    # **先收掉 Qwen worker 再換**：execv 保留同一個 PID，atexit 不會跑、worker 的看門狗也看不出主服務換了，
    # 不收的話舊 worker 會一直佔著埠號與約 7 GB 顯示記憶體，新服務的 worker 綁不到埠（2026-09-26 審查時發現）
    _qwen_stop(wait=20)
    _tts_stop_all(wait=20)        # 文字轉語音的兩個 worker 同理（VoxCPM2 約 8 GB）
    # os.execv 直接替換行程映像，保留同一個 PID——systemd 看不出差別
    os.execv(sys.executable, [sys.executable] + sys.argv)


@app.get("/v1/status")
def status():
    """伺服器狀態：忙碌、排隊狀況、磁碟空間。

    `busy` / `task` 只看離線線（batch），維持舊用戶端的意思——舊用戶端看到
    busy 會問使用者要不要等；即時線的幾秒短音訊不該觸發那個提示。
    新用戶端看 `queue`：有這個欄位就代表伺服器會自己排隊，直接送出即可。
    """
    batch = _LANES["batch"].snapshot()
    running = batch["running"]

    # /tmp 磁碟空間（暫存檔寫入處）
    disk = shutil.disk_usage(tempfile.gettempdir())
    result = {
        "busy": running is not None,
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
        "queue": {name: lane.snapshot() for name, lane in _LANES.items()},
        "update_pending": _update_pending_info(),
    }
    if running is not None:
        result["task"] = running
    return result


@app.get("/models")
def list_models():
    """列出已快取的模型"""
    cached = set()
    cached.update(k.split(":", 1)[1] for k in _models.keys())
    # 掃描 HuggingFace cache
    try:
        from huggingface_hub import scan_cache_dir
        cache_info = scan_cache_dir()
        for repo in cache_info.repos:
            name = repo.repo_id
            if name.startswith("Systran/faster-whisper-"):
                cached.add(name.replace("Systran/faster-whisper-", ""))
            elif name.startswith("guillaumekln/faster-whisper-"):
                cached.add(name.replace("guillaumekln/faster-whisper-", ""))
    except Exception:
        pass
    # openai-whisper 模型放在 ~/.cache/whisper/
    whisper_cache = os.path.expanduser("~/.cache/whisper")
    if os.path.isdir(whisper_cache):
        # 檔名格式: large-v3-turbo.pt, medium.en.pt 等
        for f in os.listdir(whisper_cache):
            if f.endswith(".pt"):
                cached.add(f[:-3])
    if _qwen_ready():
        cached.update(_QWEN_MODELS)
    return {"models": sorted(cached)}


def _queued_event(lane, t):
    """排隊中的 NDJSON 事件。舊用戶端不認得 type=queued，會直接略過（if/elif 沒有 else）。"""
    pos = lane.position(t)
    return json.dumps({"type": "queued", "position": pos, "ahead": pos,
                       "waited": round(time.time() - t.enqueued, 1)}) + "\n"


@app.post("/v1/audio/transcriptions")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("large-v3-turbo"),
    language: str = Form("en"),
    stream: str = Form("false"),
    noisy: str = Form("false"),
    reject_lang: str = Form(""),
):
    """接收音訊檔，回傳辨識結果（stream=true 時串流 NDJSON）。
    noisy=1/true：用戶端音源分析判定為低音量錄音，套用寬鬆參數。

    排隊：串流（離線）走 batch 線，在串流裡先送 `{"type":"queued"}` 事件
    直到輪到自己；非串流小檔（即時字幕）走 realtime 線。"""
    client_ip = request.client.host if request.client else ""
    is_noisy = str(noisy).lower() in ("1", "true", "yes")
    is_stream = stream.lower() == "true"

    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise

    is_breeze = model in _BREEZE_MODELS
    if is_breeze and (not is_stream or _backend != "faster-whisper"):
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return JSONResponse(status_code=400, content={"error": (
            "台語（Breeze-ASR-26）只支援離線辨識（stream=true）" if not is_stream
            else "這台伺服器的辨識後端不是 faster-whisper，無法執行台語（Breeze-ASR-26）")})

    is_qwen = model in _QWEN_MODELS
    if is_qwen:
        err = None
        if not is_stream:
            err = (400, "Qwen3-ASR 只支援離線辨識（stream=true）")
        elif not _qwen_ready():
            err = (503, f"Qwen3-ASR 尚未就緒（{_QWEN['error'] or ('載入中' if _QWEN['proc'] else '這台沒有安裝')}）")
        elif language not in _QWEN_LANG:
            err = (400, f"Qwen3-ASR 不支援 language={language}（支援 {'／'.join(_QWEN_LANG)}）")
        if err:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return JSONResponse(status_code=err[0], content={"error": err[1]})

    lane = _LANES["batch"] if (is_stream or len(content) > _REALTIME_MAX_BYTES) \
        else _LANES["realtime"]
    ticket = lane.enter("transcribe", model, language, client_ip)
    if ticket is None:   # 更新排定中，這條線已關門
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return _updating_response()
    stream_handed_off = False   # 串流回應交出後，清理改由 background 負責
    if is_noisy:
        print(f"[{client_ip}] noisy=1 → 寬鬆參數")
    ahead = lane.position(ticket)
    if ahead > 0:
        print(f"[排隊] {client_ip} 的辨識排入{lane.name}隊伍，前面 {ahead} 件", flush=True)

    try:
        # 串流模式（NDJSON）
        if is_stream:
            tmp_path = tmp.name

            def _wait_turn():
                """generator 開頭：還沒輪到就每 2 秒送一次排隊事件。
                用戶端斷線時 yield 會丟 GeneratorExit，由外層 finally 移出隊伍。"""
                while True:
                    pos = lane.position(ticket)
                    if pos <= 0:
                        return
                    yield _queued_event(lane, ticket)
                    lane.wait(ticket, 2.0)

            if is_qwen:
                # Qwen3-ASR：worker 做完整件才回（切窗、辨識、對齊），期間每 2 秒心跳
                def generate():
                    import concurrent.futures
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    cancelled = False
                    try:
                        yield from _wait_turn()
                        t0 = time.monotonic()
                        future = pool.submit(_transcribe_qwen, tmp_path, language)
                        try:
                            while not future.done():
                                yield json.dumps({"type": "heartbeat",
                                                  "elapsed": round(time.monotonic() - t0, 1)}) + "\n"
                                concurrent.futures.wait([future], timeout=2)
                            segments, duration, proc_time = future.result()
                            for i, seg in enumerate(segments):
                                yield json.dumps({"type": "segment", "index": i, "start": seg["start"],
                                                  "end": seg["end"], "text": seg["text"],
                                                  "duration": round(duration, 1)}, ensure_ascii=False) + "\n"
                            yield json.dumps({"type": "done", "total_segments": len(segments),
                                              "duration": round(duration, 1), "processing_time": proc_time,
                                              "device": "cuda", "engine": "qwen3-asr"}) + "\n"
                        except GeneratorExit:
                            cancelled = True
                            print("[取消] 客戶端中斷連線，等 Qwen3-ASR 這件做完才讓出隊伍...")
                            pool.shutdown(wait=True)
                            return
                        except Exception as e:
                            print(f"[錯誤] Qwen3-ASR 失敗（{client_ip}）：{e}", flush=True)
                            yield json.dumps({"type": "error", "detail": str(e)}, ensure_ascii=False) + "\n"
                    finally:
                        if not cancelled:
                            pool.shutdown(wait=False)
                        lane.leave(ticket)
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            elif _backend == "faster-whisper":
                def generate():
                    try:
                        yield from _wait_turn()
                        t0 = time.monotonic()
                        count = 0
                        dur = 0
                        try:
                            seg_iter = (_transcribe_breeze_stream(tmp_path) if is_breeze else
                                        _transcribe_faster_stream(tmp_path, model, language, noisy=is_noisy))
                            for seg, dur in seg_iter:
                                count += 1
                                yield json.dumps({
                                    "type": "segment", "index": count - 1,
                                    "start": seg["start"], "end": seg["end"],
                                    "text": seg["text"], "duration": round(dur, 1),
                                    "confidence": seg.get("confidence"),
                                    "language": seg.get("language"),
                                }) + "\n"
                            proc_time = round(time.monotonic() - t0, 1)
                            yield json.dumps({
                                "type": "done", "total_segments": count,
                                "duration": round(dur, 1), "processing_time": proc_time,
                                "device": _device,
                            }) + "\n"
                        except GeneratorExit:
                            elapsed = round(time.monotonic() - t0, 1)
                            print(f"[取消] 客戶端中斷連線（{elapsed:.1f}s），faster-whisper 辨識已停止")
                            return
                        except Exception as e:
                            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"
                    finally:
                        lane.leave(ticket)
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            else:
                # openai-whisper：辨識中發心跳（含進度），完成後逐段回傳
                def generate():
                    import concurrent.futures
                    pool = None
                    cancelled = False
                    try:
                        yield from _wait_turn()
                        t0 = time.monotonic()
                        progress_q = queue.Queue()
                        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        future = pool.submit(_transcribe_openai, tmp_path, model, language,
                                             progress_q=progress_q, noisy=is_noisy)
                        audio_dur = 0
                        last_pct = 0
                        last_pos = 0
                        try:
                            while not future.done():
                                # 讀取 progress queue 中的最新進度
                                while not progress_q.empty():
                                    try:
                                        msg = progress_q.get_nowait()
                                        if msg[0] == "duration":
                                            audio_dur = msg[1]
                                        elif msg[0] == "progress":
                                            last_pos = msg[1]
                                            last_pct = msg[3]
                                    except queue.Empty:
                                        break
                                elapsed = round(time.monotonic() - t0, 1)
                                hb = {"type": "heartbeat", "elapsed": elapsed}
                                if audio_dur > 0:
                                    hb["progress"] = round(last_pct, 3)
                                    hb["current"] = round(last_pos, 1)
                                    hb["duration"] = round(audio_dur, 1)
                                yield json.dumps(hb) + "\n"
                                time.sleep(2)
                            segments, full_text, duration, proc_time = future.result()
                            for i, seg in enumerate(segments):
                                yield json.dumps({
                                    "type": "segment", "index": i,
                                    "start": seg["start"], "end": seg["end"],
                                    "text": seg["text"], "duration": round(duration, 1),
                                }) + "\n"
                            yield json.dumps({
                                "type": "done", "total_segments": len(segments),
                                "duration": round(duration, 1), "processing_time": proc_time,
                                "device": _device,
                            }) + "\n"
                        except GeneratorExit:
                            cancelled = True
                            future.cancel()
                            elapsed = round(time.monotonic() - t0, 1)
                            print(f"[取消] 客戶端中斷連線（{elapsed:.1f}s），等待 openai-whisper 辨識執行緒結束...")
                            # 等 transcribe thread 真正結束再清理（GPU 仍在跑）。
                            # **也要等它結束才讓出隊伍**，否則下一件會跟它同時跑。
                            pool.shutdown(wait=True)
                            print(f"[取消] openai-whisper 執行緒已結束")
                            return
                        except Exception as e:
                            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"
                    finally:
                        if pool is not None and not cancelled:
                            pool.shutdown(wait=False)
                        lane.leave(ticket)
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

            # 串流模式由 generator 負責刪除暫存檔與讓出隊伍，不走 finally。
            # 用戶端中途斷線時，Starlette 只取消外層迭代、不會關閉這個同步 generator，
            # generator 的 finally 就永遠不會執行 → 隊伍卡住、後面的人永遠等不到、暫存檔殘留。
            # 回應結束（含斷線）後一定會跑 background，由它關閉 generator 並補做清理。
            # （generator 還沒開始跑就斷線時 close() 不會進 finally，所以這裡也要 leave。）
            gen = generate()

            def _cleanup_stream():
                try:
                    gen.close()   # 未執行完時觸發 GeneratorExit，走 generator 自己的取消流程
                except Exception as e:
                    print(f"[警告] 關閉辨識串流失敗: {e}")
                lane.leave(ticket)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            async def _cleanup_stream_async():
                await run_in_threadpool(_cleanup_stream)   # openai-whisper 取消時會等執行緒結束，不可卡住 event loop

            stream_handed_off = True
            return StreamingResponse(gen, media_type="text/x-ndjson",
                                     background=BackgroundTask(_cleanup_stream_async))

        # 非串流模式：先等輪到自己（即時線通常只等前一段幾百毫秒）
        if not await _wait_turn_async(lane, ticket, request):
            return JSONResponse(status_code=499, content={"error": "client_disconnected"})

        # 雙向語音口譯（v2.28.0）：念給使用者聽的中文會被「系統音訊」錄回去，那一路固定用英文辨識，Whisper 會把中文變成
        # 意思相近的英文、又翻一次 → 用戶端送 reject_lang=zh：先判斷語言，是中文就不辨識、回空的並標 rejected
        if reject_lang and _backend == "faster-whisper":
            try:
                det, prob = await asyncio.to_thread(_detect_language_faster, tmp.name, model)
            except Exception as e:
                det, prob = "", 0.0
                print(f"[警告] 判斷語言失敗（照常辨識）：{e}")
            if det == reject_lang and prob >= 0.5:
                return {"text": "", "segments": [], "language": det, "language_probability": round(prob, 3),
                        "rejected": True, "model": model, "duration": 0, "processing_time": 0, "device": _device}
        # 用 asyncio.to_thread 避免阻塞 event loop
        try:
            if _backend == "openai-whisper":
                segments, full_text, duration, proc_time = await asyncio.to_thread(
                    _transcribe_openai, tmp.name, model, language, None, is_noisy)
            else:
                segments, full_text, duration, proc_time = await asyncio.to_thread(
                    _transcribe_faster, tmp.name, model, language, is_noisy)
        except Exception as e:
            print(f"[錯誤] 辨識失敗: {model} — {e}")
            return JSONResponse(
                status_code=500,
                content={"error": f"辨識失敗: {model}", "detail": str(e)},
            )

        return {
            "text": " ".join(full_text),
            "segments": segments,
            "language": language,
            "model": model,
            "duration": duration,
            "processing_time": proc_time,
            "device": _device,
            "backend": _backend,
        }
    finally:
        # 非串流模式，或串流回應交出前就出錯時在這裡清理（交出後由 background 清理）
        if not stream_handed_off:
            lane.leave(ticket)
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


@app.post("/v1/audio/diarize")
async def diarize(
    request: Request,
    file: UploadFile = File(...),
    segments: str = Form(...),
    num_speakers: int = Form(0),
    stream: str = Form("false"),
    engine: str = Form("auto"),
):
    """接收音訊檔 + segments JSON，回傳講者辨識結果。
    engine：auto（能用 Nemotron 就用）／nemotron／legacy（resemblyzer）。回應的 engine 是實際用的方法

    走 batch 線排隊。**回應是「前導空白 + JSON」的串流**：排隊與計算期間每 5 秒
    送一個空白字元保持連線（用戶端的讀取逾時是 300 秒，排在一場長會議後面
    一定會超過），最後才送 JSON 本體。JSON 允許前導空白，舊用戶端的
    `json.loads(resp.read())` 照樣解得開。代價是狀態碼一開始就得定成 200，
    排隊之後才發生的錯誤改放在 JSON 的 `error` 欄位。"""
    from fastapi.responses import JSONResponse

    if not (_HAS_DIARIZE or _HAS_NEMO):
        return JSONResponse(
            status_code=500,
            content={"error": "沒有可用的講者辨識方法（resemblyzer 與 Nemotron 都不可用）"},
        )
    if engine not in ("auto", "nemotron", "legacy"):
        return JSONResponse(status_code=400, content={"error": f"engine 必須是 auto／nemotron／legacy，收到 {engine!r}"})

    # 解析 segments JSON
    try:
        seg_list = json.loads(segments)
        if not isinstance(seg_list, list):
            raise ValueError("segments 必須是 list")
        for s in seg_list:
            if not all(k in s for k in ("start", "end", "text")):
                raise ValueError("每個 segment 必須含 start, end, text")
    except (json.JSONDecodeError, ValueError) as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"segments JSON 格式錯誤: {e}"},
        )

    client_ip = request.client.host if request.client else ""
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise

    lane = _LANES["batch"]
    ticket = lane.enter("diarize", _recommended_diarizer(num_speakers or None, engine)[0], "", client_ip)
    if ticket is None:   # 更新排定中，這條線已關門
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return _updating_response()
    ahead = lane.position(ticket)
    if ahead > 0:
        print(f"[排隊] {client_ip} 的講者辨識排入隊伍，前面 {ahead} 件", flush=True)
    ns = num_speakers if num_speakers > 0 else None

    def _release(_=None):
        lane.leave(ticket)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # stream=true（v2.21.9 起的用戶端）：改回 NDJSON，排隊時每 2 秒一個 queued 事件、
    # 計算中每 5 秒一個 heartbeat，最後一行 type=result。呼叫端（v3 API）要靠
    # queued 分辨「在排隊」與「卡住」——空白保活只能保住連線，說不出在等什麼。
    ndjson = str(stream).lower() in ("1", "true", "yes")

    def _line(obj):
        return (json.dumps(obj) + "\n").encode()

    async def body():
        work = None
        try:
            last = 0.0 if ndjson else time.monotonic()
            while lane.position(ticket) > 0:
                if time.monotonic() - last >= (2 if ndjson else 5):
                    yield (_line({"type": "queued", "ahead": lane.position(ticket),
                                  "waited": round(time.time() - ticket.enqueued, 1)})
                           if ndjson else b" ")
                    last = time.monotonic()
                await asyncio.sleep(0.3)
            t0 = time.monotonic()
            work = asyncio.ensure_future(
                asyncio.to_thread(_diarize, tmp.name, seg_list, num_speakers=ns, engine=engine))
            while not work.done():
                await asyncio.wait({work}, timeout=5)
                if not work.done():
                    yield (_line({"type": "heartbeat", "elapsed": round(time.monotonic() - t0, 1)})
                           if ndjson else b" ")
            try:
                speaker_labels, used, note, saturated = work.result()
            except Exception as e:
                print(f"[錯誤] diarize 失敗: {e}")
                err = {"error": f"講者辨識失敗: {e}"}
                yield _line({"type": "error", **err}) if ndjson else json.dumps(err).encode()
                return
            if speaker_labels is None:
                # 無法提取聲紋，降級全部 Speaker 0
                speaker_labels = [0] * len(seg_list)
            res = {
                "speaker_labels": speaker_labels,
                "num_speakers": len(set(speaker_labels)),
                "processing_time": round(time.monotonic() - t0, 2),
                "device": _torch_device,
                "engine": used,
                "note": note,
                "reason": _diar_reason(note) if used == "legacy" else None,   # v2.26.1
                "saturated": bool(saturated),                                  # v2.26.5：Nemotron 8 位全滿
            }
            yield _line({"type": "result", **res}) if ndjson else json.dumps(res).encode()
        finally:
            if work is not None and not work.done():
                # 用戶端斷線了但執行緒還在算：**等它算完才讓出隊伍**，
                # 否則下一件會跟它同時佔用 GPU
                work.add_done_callback(_release)
            else:
                _release()

    return StreamingResponse(body(), media_type="application/x-ndjson" if ndjson else "application/json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="jt-whisper-server")
    parser.add_argument("--port", type=int, default=8978)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--selftest", action="store_true",
                        help="只檢查這份程式能不能正常啟動，然後離開（自動更新前的把關）")
    args = parser.parse_args()

    if args.selftest:
        # 走到這裡代表模組層級的 import 與後端偵測都已經跑完沒有出錯。
        # 再確認幾個實際會被呼叫到的東西存在，避免「import 得起來但端點壞掉」。
        missing = [n for n in ("health", "status", "admin_update", "_diarize", "_diarize_legacy",
                               "_nemotron_diarize", "_transcribe_qwen", "_qwen_worker_main",
                               "_tts_worker_main", "tts_speech", "tts_voice_upload", "_breezy_worker_main", "_tts_stop_all", "tts_voice_delete",
                               "_update_worker", "_update_pending_info")
                   if n not in globals()]
        if missing:
            print(f"[selftest] 失敗：缺少 {missing}", file=sys.stderr)
            sys.exit(1)
        if _HAS_DIARIZE:
            # 講者辨識的設定最容易在改版時寫壞，直接把它建起來看看
            from spectralcluster import refinement as _r, laplacian as _l
            _r.RefinementOptions(
                gaussian_blur_sigma=0, p_percentile=0.98,
                thresholding_soft_multiplier=0.01,
                thresholding_type=_r.ThresholdType.RowMax,
                symmetrize_type=_r.SymmetrizeType.Max,
                refinement_sequence=[_r.RefinementName.CropDiagonal])
            _ = _l.LaplacianType.GraphCut
        print(f"[selftest] OK version={SERVER_VERSION} backend={_backend} device={_device}")
        sys.exit(0)

    print(f"[jt-whisper-server] v{SERVER_VERSION} 啟動 {args.host}:{args.port} "
          f"(backend={_backend}, device={_device}"
          f"{', 可遠端更新' if UPDATE_TOKEN else ''})")
    import atexit
    _qwen_start(int(os.environ.get("JT_QWEN_PORT") or args.port + 11))
    atexit.register(_qwen_stop)
    _TTS["port"] = int(os.environ.get("JT_TTS_PORT") or args.port + 12)   # worker 第一次用到才啟動
    _BREEZY["port"] = int(os.environ.get("JT_BREEZY_PORT") or args.port + 13)
    threading.Thread(target=_tts_idle_watch, daemon=True).start()
    atexit.register(_tts_stop_all)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
