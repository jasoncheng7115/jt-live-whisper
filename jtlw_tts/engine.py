"""文字轉語音：聲音管理、合成（GPU 伺服器／Apple Silicon 本機）、要朗讀的文字、一次朗讀的合成流程

規格：specs/2026-10-08_TTS開發規格_v2.md。重點：
- 聲音一定是管理者匯入、取得錄音者同意的台灣華語錄音；公開版不附預設聲音
- 朗讀時只預先合成目前播放位置之後 PREFETCH 段（不無限預先合成）；轉成音檔時全部依序合成
- 朗讀是主程式的一個模式（--tts-file，2026-10-09 使用者：「要整合進本來流程」）：字幕、懸浮字幕、終端機都沿用既有的
"""
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

from .tw_reading import _tts_split

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICES_DIR = os.path.join(ROOT, "tts_voices")
# 內建聲音（2026-10-09，使用者：「內建多一點，不要讓使用者還要做很多前置作業」「8 個」）：VoxCPM2 依文字描述產生、不是真人錄音，
# 跟著程式發佈與升級；不能刪除、不能改分類。沒有設定聲音時用第一個
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices")
DEFAULT_BUILTIN = "b00000000001"
DEFAULT_EN_BUILTIN = "b00000000010"        # 念給對方聽的預設英文聲音：男聲（2026-10-10 使用者試聽後選定）
TMP_DIR = os.path.join(ROOT, "tts_tmp")
DATA_DIR = os.path.join(ROOT, "tts_data")       # Apple Silicon 本機：moe_words.tsv、G2PWModel、bert-base-chinese
MLX_MODEL = "mlx-community/VoxCPM2-8bit"
MLX_MODEL_REV = "d52725898a0675703f7f9ddc5a4d1a3cdbb99032"
# 推論需要的檔案（README、範例音檔不用）。mlx-audio 自己下載時只抓這些，整份要求會被判成「快取不完整」（Mac 實測）
MLX_FILES = ["config.json", "model.safetensors", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"]
PREFETCH = 2                                     # 朗讀時最多先合成目前播放位置之後幾段
# 合成模型。VoxCPM2 是預設：速度快（GPU 伺服器約 0.9 倍音訊長度、Mac 8bit 約 1.1 倍），GPU 伺服器與 Apple Silicon 都能跑。
# BreezyVoice（2026-10-09 加入）用台灣華語訓練、口音道地，但合成慢（約 1.2～2.5 倍）、只在 GPU 伺服器
MODELS = {
    "voxcpm2": {"label": "VoxCPM2（OpenBMB）", "tag": "預設", "where": ("remote", "cuda", "mlx"),
                "note": "預設，速度快（合成時間約音訊長度的 0.9～1.1 倍），GPU 伺服器與 Apple Silicon Mac 都能用"},
    "breezyvoice": {"label": "BreezyVoice（MediaTek）", "tag": "台灣口音・較慢", "where": ("remote",),
                    "note": "台灣口音，但合成速度慢，不適合即時（合成時間約音訊長度的 1.2～2.5 倍）；只在 GPU 伺服器"},
}
DEFAULT_MODEL = "voxcpm2"
DEFAULTS = {"provider": "auto", "voice": "", "custom": {"和": "ㄏㄢˋ"}, "mac_steps": 6, "chunk_chars": 80,
            "max_chars": 100000}
PAUSES = {"short": 0.2, "normal": 0.5, "long": 1.0}         # 段落之間停頓幾秒
RATE_MIN, RATE_MAX = 0.5, 2.0                                # ffmpeg atempo 單一濾鏡的範圍
GENDERS = {"female": "女聲", "male": "男聲", "other": "其他"}
TEXT_EXTS = (".txt", ".md", ".srt", ".vtt")
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
LIVE_RE = re.compile(r"^live_[0-9a-f]{16}$")             # 瀏覽器播放時，每段音檔放在 tts_tmp/live_<編號>/


class TTSError(Exception):
    """code：給程式判斷；message：給人看（台灣繁體中文）；status：HTTP 狀態碼"""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def settings(cfg):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in ((cfg or {}).get("tts") or {}).items() if k in DEFAULTS})
    return s


# ── 聲音 ─────────────────────────────────────────────────────
def voice_sha(wav_bytes, transcript):
    """與 GPU 伺服器 /v1/tts/voices 的算法相同：sha256(錄音位元組＋換行＋逐字稿)"""
    return hashlib.sha256(wav_bytes + b"\n" + transcript.strip().encode()).hexdigest()


def _ffmpeg():
    for p in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if p and os.path.exists(p):
            return p
    return None


def _wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def list_voices(lang="zh"):
    """內建的在前（依編號），自己匯入的接在後面（依匯入時間）。
    lang：zh＝朗讀用（沒標語言的都算），en＝雙向口譯念英文用的（v2.28.0 內建 2 個 AI 英文聲音），None＝全部"""
    out = {}
    for base in (BUILTIN_DIR, VOICES_DIR):
        if os.path.isdir(base):
            for vid in os.listdir(base):
                v = get_voice(vid, missing_ok=True)
                if v and v["id"] not in out and (lang is None or (v.get("lang") or "zh") == lang):
                    out[v["id"]] = v
    return sorted(out.values(), key=lambda v: (not v.get("builtin"), v.get("created", "") if not v.get("builtin") else v["id"]))


def get_voice(vid, missing_ok=False):
    if vid and _ID_RE.match(vid):
        for base, builtin in ((VOICES_DIR, False), (BUILTIN_DIR, True)):
            meta = os.path.join(base, vid, "voice.json")
            wav = os.path.join(base, vid, "ref.wav")
            if os.path.exists(meta) and os.path.exists(wav):
                try:
                    with open(meta, encoding="utf-8") as f:
                        v = json.load(f)
                except (OSError, ValueError):
                    continue
                v["wav"] = wav
                v["builtin"] = builtin
                return v
    if missing_ok:
        return None
    raise TTSError("voice_not_found", "找不到這個聲音（可能已被刪除）", 404)


def default_voice_id(cfg):
    """設定裡的預設聲音；沒有設定或那個聲音已經刪掉了，就用內建的第一個"""
    vid = settings(cfg)["voice"]
    if vid and get_voice(vid, missing_ok=True):
        return vid
    return DEFAULT_BUILTIN if get_voice(DEFAULT_BUILTIN, missing_ok=True) else ""


def default_en_voice_id():
    """雙向口譯念英文給對方聽的預設聲音：內建的英文男聲（2026-10-10 實測：台灣華語聲音念英文時 IP、版本號較常念錯；
    三種試聽後使用者選男聲）。沒有它（第一次升級還拿不到）就用其他內建的英文聲音"""
    vs = [v for v in list_voices("en") if v.get("builtin")]
    if any(v["id"] == DEFAULT_EN_BUILTIN for v in vs):
        return DEFAULT_EN_BUILTIN
    return vs[0]["id"] if vs else ""


def public_voice(v):
    out = {k: v.get(k) for k in ("id", "name", "source", "duration", "created", "transcript")}
    out["lang"] = v.get("lang") or "zh"
    out["gender"] = v.get("gender") if v.get("gender") in GENDERS else ""
    out["builtin"] = bool(v.get("builtin"))
    return out


def import_voice(src_path, name, transcript, source, consent, gender=""):
    """匯入參考錄音：轉成 48 kHz 單聲道 16-bit WAV，長度 3～60 秒（建議 10～20 秒）。
    consent 必須為真：聲音屬可識別個人的資料，要有錄音者同意（規格 v2 第八節）"""
    name, transcript, source = (name or "").strip(), (transcript or "").strip(), (source or "").strip()
    if not consent:
        raise TTSError("consent_required", "請先確認已取得錄音者同意")
    if not name or len(name) > 40:
        raise TTSError("invalid_name", "聲音名稱不可空白、最多 40 字")
    if not transcript or len(transcript) > 1000:
        raise TTSError("invalid_transcript", "逐字稿不可空白、最多 1000 字，要和錄音內容一字不差")
    if not source or len(source) > 200:
        raise TTSError("invalid_source", "請填寫錄音來源（誰錄的、何時取得同意），最多 200 字")
    if gender and gender not in GENDERS:
        raise TTSError("invalid_gender", "性別只能是 female、male、other")
    ff = _ffmpeg()
    if not ff:
        raise TTSError("ffmpeg_missing", "找不到 ffmpeg，無法轉換錄音格式", 500)
    vid = uuid.uuid4().hex[:12]
    d = os.path.join(VOICES_DIR, vid)
    os.makedirs(d, exist_ok=True)
    wav = os.path.join(d, "ref.wav")
    r = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src_path,
                        "-ac", "1", "-ar", "48000", "-sample_fmt", "s16", wav],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    try:
        if r.returncode != 0 or not os.path.exists(wav):
            raise TTSError("invalid_audio", "讀不懂這個音檔：" + r.stderr.decode(errors="replace").strip()[-200:])
        dur = _wav_seconds(wav)
        if not 3 <= dur <= 60:
            raise TTSError("invalid_audio", f"參考錄音要 3～60 秒（建議 10～20 秒），這段是 {dur:.1f} 秒")
        with open(wav, "rb") as f:
            sha = voice_sha(f.read(), transcript)
        meta = {"id": vid, "name": name, "transcript": transcript, "source": source, "consent": True,
                "gender": gender or "", "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration": round(dur, 2),
                "sha": sha}
        tmp = os.path.join(d, "voice.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(d, "voice.json"))
        return meta
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise


def delete_voice(vid, cfg=None):
    """刪除聲音。有設定 GPU 伺服器時一併刪掉伺服器上快取的參考錄音（錄音者可以要求刪除，只刪這台的不算）；
    伺服器連不上或版本太舊就記在待刪清單，下次連上時補刪（flush_pending_deletes）。
    回傳伺服器那份的狀態："deleted"、"pending"（記下來了），沒有設定 GPU 伺服器回 None"""
    v = get_voice(vid)
    if v.get("builtin"):
        raise TTSError("builtin_voice", "內建聲音不能刪除（不想用的話選別的聲音就好）")
    shutil.rmtree(os.path.join(VOICES_DIR, vid), ignore_errors=True)
    rw = (cfg or {}).get("remote_whisper") or {}
    if not rw.get("host") or not v.get("sha"):
        return None
    try:
        RemoteProvider(rw["host"], rw.get("whisper_port", 8978)).delete_voice(v["sha"])
        return "deleted"
    except Exception:
        _pending_deletes_save(sorted(set(_pending_deletes_load()) | {v["sha"]}))
        return "pending"


def _pending_deletes_path():
    return os.path.join(VOICES_DIR, ".gpu_delete_pending.json")


def _pending_deletes_load():
    try:
        with open(_pending_deletes_path(), encoding="utf-8") as f:
            return [x for x in json.load(f) if isinstance(x, str)]
    except (OSError, ValueError):
        return []


def _pending_deletes_save(shas):
    p = _pending_deletes_path()
    if not shas:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(shas, f)
    os.replace(p + ".tmp", p)


def flush_pending_deletes(provider):
    """之前沒刪到的伺服器端參考錄音補刪（GPU 伺服器連得上時呼叫；沒有待刪的就什麼都不做）"""
    left = []
    for sha in _pending_deletes_load():
        try:
            provider.delete_voice(sha)
        except Exception:
            left.append(sha)
    if left != _pending_deletes_load():
        _pending_deletes_save(left)


def set_voice_gender(vid, gender):
    """匯入後補填或改性別（v2.27.0 之前匯入的聲音沒有這個欄位）"""
    v = get_voice(vid)
    if v.get("builtin"):
        raise TTSError("builtin_voice", "內建聲音不能改分類")
    if gender and gender not in GENDERS:
        raise TTSError("invalid_gender", "性別只能是 female、male、other")
    meta = {k: val for k, val in v.items() if k != "wav"}
    meta["gender"] = gender or ""
    d = os.path.join(VOICES_DIR, vid)
    with open(os.path.join(d, "voice.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(os.path.join(d, "voice.json.tmp"), os.path.join(d, "voice.json"))


# ── 合成 ─────────────────────────────────────────────────────
def _http(method, url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _err_detail(body):
    try:
        d = json.loads(body)
        return d.get("detail") or d.get("error") or ""
    except Exception:
        return body[:200].decode(errors="replace") if isinstance(body, bytes) else str(body)[:200]


class RemoteProvider:
    """GPU 伺服器（remote_whisper_server.py 的 /v1/tts/*）。台灣念法在伺服器端做"""
    kind = "remote"
    label = "GPU 伺服器"

    def __init__(self, host, port, model=DEFAULT_MODEL):
        self.base = f"http://{host}:{port}"
        self.model = model

    def health(self):
        code, body, _ = _http("GET", self.base + "/v1/tts/health", timeout=5)
        if code == 404:
            return {"available": False, "reason": "GPU 伺服器的版本太舊、沒有文字轉語音（需要更新伺服器）"}
        if code != 200:
            return {"available": False, "reason": f"GPU 伺服器回應 HTTP {code}"}
        h = json.loads(body)
        if self.model == DEFAULT_MODEL:
            return h
        m = (h.get("models") or {}).get(self.model)
        if m is None:                    # v2.27.0 第一版的伺服器只有 VoxCPM2
            return {"available": False, "reason": f"GPU 伺服器的版本太舊、沒有 {MODELS[self.model]['label']}（需要更新伺服器）"}
        return m

    def _ensure_voice(self, voice):
        code, _, _ = _http("GET", f"{self.base}/v1/tts/voices/{voice['sha']}", timeout=10)
        if code == 200:
            return
        with open(voice["wav"], "rb") as f:
            wav = f.read()
        bnd = "----jtlw" + uuid.uuid4().hex
        body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{voice['transcript']}\r\n"
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"ref.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{bnd}--\r\n".encode()
        code, resp, _ = _http("POST", self.base + "/v1/tts/voices", body,
                              {"Content-Type": f"multipart/form-data; boundary={bnd}"}, timeout=60)
        if code != 200:
            raise TTSError("voice_upload_failed", "參考錄音上傳到 GPU 伺服器失敗：" + _err_detail(resp), 502)
        if json.loads(resp).get("voice") != voice["sha"]:
            raise TTSError("voice_upload_failed", "GPU 伺服器算出的聲音編號和本機不同（版本不一致？）", 502)

    def synth(self, text, voice, custom, steps=None, lang="zh"):
        """一段（≤300 字）→ (WAV 位元組, 送進模型的文字)。lang＝en：英文句子（雙向口譯），不套台灣念法"""
        self._ensure_voice(voice)
        body = {"text": text, "voice": voice["sha"], "custom": custom or {}, "model": self.model}
        if lang != "zh":
            body["lang"] = lang                 # v2.27.0 的伺服器不認得 lang：只在英文時送
        payload = json.dumps(body).encode()
        for attempt in (1, 2):
            # 第一次可能要啟動 worker（約 30 秒）＋合成
            code, body, headers = _http("POST", self.base + "/v1/tts/speech", payload,
                                        {"Content-Type": "application/json"}, timeout=600)
            if code == 404 and attempt == 1:            # 伺服器端的聲音被清掉了：重傳一次
                self._ensure_voice(voice)
                continue
            break
        if code != 200:
            status = 503 if code == 503 else 502
            raise TTSError("synth_failed", "GPU 伺服器合成失敗：" + _err_detail(body), status)
        sp = {k.lower(): v for k, v in headers.items()}.get("x-tts-spoken", "")
        return body, urllib.parse.unquote(sp)

    def synth_stream(self, text, voice, lang="zh", block=9600, custom=None):
        """串流合成（雙向口譯，v2.28.0 起的伺服器）：一段一段產生 (PCM, 取樣率)，第一段約 0.3～0.6 秒就出來。
        伺服器太舊（沒有 /v1/tts/stream）時丟 TTSError（呼叫端改用 synth）"""
        import http.client
        self._ensure_voice(voice)
        host, port = self.base[len("http://"):].rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port), timeout=120)
        try:
            conn.request("POST", "/v1/tts/stream", json.dumps({"text": text, "voice": voice["sha"], "lang": lang,
                                                                "custom": custom or {}, "model": self.model}),
                         {"Content-Type": "application/json"})
            r = conn.getresponse()
            if r.status != 200:
                body = r.read()
                if r.status == 404 and b"voice_not_found" in body:     # 伺服器端的聲音被清掉了：重傳再來一次
                    conn.close()
                    self._ensure_voice(voice)
                    yield from self.synth_stream(text, voice, lang, block, custom)
                    return
                raise TTSError("synth_failed", "GPU 伺服器串流合成失敗：" + (_err_detail(body) or f"HTTP {r.status}"),
                               503 if r.status in (404, 405) else 502)
            sr = int(r.getheader("X-TTS-SR") or 48000)
            while True:
                b = r.read(block)
                if not b:
                    break
                if len(b) % 2:                  # 16-bit：湊成整數個樣本
                    b += r.read(1)
                yield b, sr
        finally:
            conn.close()

    def delete_voice(self, sha):
        """刪掉伺服器快取的參考錄音（v2.27.0 第一版的伺服器沒有這個端點：回 405，當成失敗、之後補刪）"""
        code, body, _ = _http("DELETE", f"{self.base}/v1/tts/voices/{sha}", timeout=10)
        if code != 200:
            raise TTSError("voice_delete_failed", "GPU 伺服器沒有刪掉參考錄音：" + (_err_detail(body) or f"HTTP {code}"), 502)
        return json.loads(body).get("deleted", False)

    def convert(self, text, custom):
        code, body, _ = _http("POST", self.base + "/v1/tts/convert",
                              json.dumps({"text": text, "custom": custom or {}, "model": self.model}).encode(),
                              {"Content-Type": "application/json"}, timeout=600)
        if code != 200:
            raise TTSError("convert_failed", "台灣念法轉換失敗：" + _err_detail(body), 502)
        return json.loads(body).get("spoken", "")


def _mem_gb():
    try:
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                      timeout=5).stdout.strip()) / 1024 ** 3
    except Exception:
        pass
    return None


class MlxProvider:
    """Apple Silicon 本機（mlx-audio、mlx-community/VoxCPM2-8bit、預設 6 步：M5 實測 RTF 1.07）"""
    kind = "mlx"
    label = "本機（Apple Silicon）"
    _lock = threading.Lock()
    _model = None
    _text = None

    def __init__(self, steps=6):
        self.steps = steps

    @staticmethod
    def unavailable():
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return "本機合成只支援 Apple Silicon Mac"
        mem = _mem_gb()
        if mem is not None and mem < 15.5:
            return f"本機記憶體 {mem:.0f} GB，本機合成需要 16 GB 以上"
        import importlib.util
        for mod in ("mlx_audio", "g2pw", "pypinyin", "opencc", "torch"):
            # 只查有沒有、不載入：設定頁一打開就會問這裡，g2pw 一載入就連 torch 一起帶進 WebUI（規格 A01：沒用到不載入）
            if importlib.util.find_spec(mod) is None:
                return (f"這台 Mac 還沒裝本機合成（缺 {mod}）：在安裝資料夾執行 ./install.sh，"
                        "問到「是否在這台 Mac 啟用本機文字轉語音」時按 Enter（約 4 GB）")
        if not os.path.exists(os.path.join(DATA_DIR, "moe_words.tsv")):
            return "這台 Mac 還沒有台灣念法資源（教育部辭典）：在安裝資料夾執行 ./install.sh，問到「是否在這台 Mac 啟用本機文字轉語音」時按 Enter"
        return None

    def health(self):
        why = self.unavailable()
        return {"available": why is None, "reason": why}

    def _load(self):
        with self._lock:
            if MlxProvider._model is None:
                from huggingface_hub import snapshot_download
                from mlx_audio.tts.utils import load
                from .tw_reading import _tts_load_text, _tts_vocab_chars
                path = snapshot_download(MLX_MODEL, revision=MLX_MODEL_REV, local_files_only=True, allow_patterns=MLX_FILES)
                text = _tts_load_text(DATA_DIR)
                text["vocab"] = _tts_vocab_chars(os.path.join(path, "tokenizer.json"))   # 模型不認得的字一律加念法提示
                MlxProvider._model = load(path)
                MlxProvider._text = text
            return MlxProvider._model, MlxProvider._text

    def synth(self, text, voice, custom, steps=None, lang="zh"):
        import numpy as np
        from .tw_reading import _tts_en_text, _tts_spoken
        model, R = self._load()
        sp = _tts_en_text(text) if lang == "en" else _tts_spoken(R, text, custom)
        with self._lock:
            parts = [np.array(r.audio, dtype=np.float32).reshape(-1) for r in model.generate(
                text=sp, ref_audio=voice["wav"], prompt_audio=voice["wav"], prompt_text=voice["transcript"],
                cfg_value=2.0, inference_timesteps=int(steps or self.steps))]
        audio = np.clip(np.concatenate(parts) if parts else np.zeros(0, np.float32), -1, 1)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(getattr(model, "sample_rate", 48000))
            w.writeframes((audio * 32767).astype("<i2").tobytes())
        return buf.getvalue(), sp

    def convert(self, text, custom):
        from .tw_reading import _tts_spoken
        _, R = self._load()
        return _tts_spoken(R, text, custom)


# ── 本機 NVIDIA 顯示卡（v2.32.0，beta）──────────────────────────────
# Windows 有 NVIDIA 顯示卡時在本機合成（.\install.ps1 -LocalTTS 選用安裝）。跟 GPU 伺服器同一支合成程式：
# remote_whisper_server.py --tts-worker，在獨立的 venv-tts（固定版本的 CUDA 版 torch＋voxcpm，跟產品 venv 的套件版本互不影響）。
# 台灣念法在合成程式裡做（同 GPU 伺服器）。合成程式只聽 127.0.0.1、固定埠號：WebUI 與主程式共用同一份，
# 不會載兩份模型（約 6.5 GB 顯示記憶體）；沒有人管它，閒置 CUDA_IDLE 秒自己結束
CUDA_VENV = os.environ.get("JTLW_TTS_VENV") or os.path.join(ROOT, "venv-tts")
CUDA_PORT = int(os.environ.get("JTLW_TTS_LOCAL_PORT") or 19786)
CUDA_IDLE = 600
CUDA_READY_MARK = "jtlw-tts-ready"              # = remote_whisper_server.TTS_READY_MARK（--tts-setup 檢查通過才寫）
CUDA_LOAD_WAIT = 300                            # 第一次載入模型最多等幾秒（GPU 伺服器約 30 秒；一般硬碟慢很多）
# 顯示卡記憶體（v2.33.0）：2026-11-03 GPU 伺服器實測合成程式載入後佔 5.0 GB、合成 99 字最多 7.0 GB，再加 CUDA 本身的開銷
CUDA_VRAM_NEED_GB = 8.0
CUDA_VRAM_LOW_GB = 1.0                          # 合成中剩不到這麼多就警告：Windows 的驅動用完不會出錯，改借系統記憶體、慢很多
VRAM_WARN_EVERY = 300                           # 同一種警告幾秒內只說一次
_VRAM_WARN = {"cb": None, "shown": {}, "last": None}


def _cuda_python():
    return os.path.join(CUDA_VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(CUDA_VENV, "bin", "python")


def set_vram_warn_handler(cb):
    """顯示卡記憶體警告交給誰說（主程式：終端機＋WebUI）；None＝印到 stderr"""
    _VRAM_WARN["cb"] = cb


def last_vram_warning(max_age=600):
    """最近一次的警告文字（WebUI 設定頁顯示用），太舊的不算"""
    last = _VRAM_WARN["last"]
    return last[1] if last and time.time() - last[0] < max_age else None


def _vram_warn(key, msg):
    now = time.monotonic()
    if now - _VRAM_WARN["shown"].get(key, -1e9) < VRAM_WARN_EVERY:
        return
    _VRAM_WARN["shown"][key] = now
    _VRAM_WARN["last"] = (time.time(), msg)
    cb = _VRAM_WARN["cb"]
    try:
        cb(msg) if cb else print("[本機合成] " + msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _nvidia_smi():
    """nvidia-smi 在哪（JTLW_NVIDIA_SMI 可指定，測試用）。驅動新的都在 PATH 上；舊的在 NVSMI 資料夾（同 install.ps1 find_nvidia_smi）"""
    env = os.environ.get("JTLW_NVIDIA_SMI")
    if env:
        return env
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if os.name == "nt":
        for d in (os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32"),
                  os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "NVIDIA Corporation", "NVSMI")):
            if os.path.exists(os.path.join(d, "nvidia-smi.exe")):
                return os.path.join(d, "nvidia-smi.exe")
    return None


def gpu_memory():
    """合成程式會用的那張 NVIDIA 顯示卡：{name, total_gb, used_gb, free_gb}；查不到 None。
    有 CUDA_VISIBLE_DEVICES 時看它的第一張（torch 也是用那張），否則第 0 張"""
    smi = _nvidia_smi()
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=index,name,memory.total,memory.used,memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                             **({"creationflags": 0x08000000} if os.name == "nt" else {})).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in out.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 5:
            continue
        try:
            rows.append({"index": f[0], "name": f[1], "total_gb": round(float(f[2]) / 1024, 2),
                         "used_gb": round(float(f[3]) / 1024, 2), "free_gb": round(float(f[4]) / 1024, 2)})
        except ValueError:
            continue
    if not rows:
        return None
    want = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").split(",")[0].strip()
    return next((r for r in rows if r["index"] == want), rows[0])


def vram_shortage(free_gb, total_gb=None):
    """開始合成前：剩下的放不下就回警告文字，夠就 None"""
    if free_gb is None or free_gb >= CUDA_VRAM_NEED_GB:
        return None
    if total_gb is not None and total_gb < CUDA_VRAM_NEED_GB:
        return (f"這張顯示卡只有 {total_gb:.1f} GB 記憶體，本機合成約需 {CUDA_VRAM_NEED_GB:.0f} GB：放不下，"
                "Windows 會改借用系統記憶體，合成會慢很多（建議改用 GPU 伺服器）")
    return (f"顯示卡記憶體只剩 {free_gb:.1f} GB（共 {total_gb:.1f} GB），本機合成約需 {CUDA_VRAM_NEED_GB:.0f} GB："
            "可能載入失敗，或改借用系統記憶體而慢很多；請先關掉其他用到顯示卡的程式（遊戲、其他 AI 程式、本機辨識）"
            if total_gb is not None else
            f"顯示卡記憶體只剩 {free_gb:.1f} GB，本機合成約需 {CUDA_VRAM_NEED_GB:.0f} GB：可能載入失敗或慢很多")


def vram_low(free_gb, total_gb=None):
    """合成中：剩不到 CUDA_VRAM_LOW_GB 就回警告文字"""
    if free_gb is None or free_gb >= CUDA_VRAM_LOW_GB:
        return None
    of = f"（共 {total_gb:.1f} GB）" if total_gb is not None else ""
    return (f"顯示卡記憶體快用完了：只剩 {free_gb:.1f} GB{of}。Windows 會改借用系統記憶體，合成會慢很多、口譯可能跟不上；"
            "請關掉其他用到顯示卡的程式")


def _oom_hint(text):
    t = str(text or "").lower()
    return (f"（顯示卡記憶體不足：本機合成約需 {CUDA_VRAM_NEED_GB:.0f} GB，請關掉其他用到顯示卡的程式，或改用 GPU 伺服器）"
            if "out of memory" in t or "outofmemory" in t else "")


class CudaProvider:
    """本機 NVIDIA 顯示卡（beta）：本機的 remote_whisper_server.py --tts-worker，VoxCPM2 bf16、10 步（同 GPU 伺服器）"""
    kind = "cuda"
    label = "本機（NVIDIA 顯示卡，beta）"
    note = "還沒在 Windows＋NVIDIA 實機開會測過"     # 口譯開始時、WebUI 口譯區說明（label 已經寫了 beta）
    _lock = threading.Lock()
    server = os.path.join(ROOT, "remote_whisper_server.py")

    @staticmethod
    def unavailable():
        if sys.platform == "darwin":
            return "本機 NVIDIA 合成只在 Windows 提供（Apple Silicon Mac 用「本機（Apple Silicon）」）"
        hint = "：在安裝資料夾執行 .\\install.ps1 -LocalTTS（需要 NVIDIA 顯示卡、約 20 GB）" if os.name == "nt" \
            else "（目前只在 Windows 提供安裝）"
        if not os.path.exists(os.path.join(CUDA_VENV, CUDA_READY_MARK)) or not os.path.exists(_cuda_python()):
            return "這台還沒安裝本機 NVIDIA 合成（beta）" + hint
        if not os.path.exists(os.path.join(DATA_DIR, "moe_words.tsv")):
            return "這台還沒有台灣念法資源（教育部辭典）" + hint
        return None

    def health(self):
        """不會啟動合成程式。stream／langs：雙向口譯要的（同一支合成程式，v2.33.0 起口譯也能用本機，beta）"""
        why = self.unavailable()
        if why is not None:
            return {"available": False, "reason": why}
        return {"available": True, "reason": None, "stream": True, "langs": ["zh", "en"], "beta": True}

    def running_vram(self):
        """合成程式在跑的話，它看到的顯示卡記憶體 {free_gb, total_gb, used_gb, peak_gb}；沒在跑或舊版沒回報 None"""
        try:
            code, body, _ = _http("GET", self._base() + "/health", timeout=5)
        except (urllib.error.URLError, OSError):
            return None
        if code != 200:
            return None
        try:
            return json.loads(body).get("vram")
        except ValueError:
            return None

    def vram(self):
        """現在的顯示卡記憶體：合成程式在跑就問它，不然問 nvidia-smi"""
        v = self.running_vram()
        if v:
            return dict(v, source="worker")
        g = gpu_memory()
        return dict(g, source="nvidia-smi") if g else None

    def keepalive(self):
        """會議中很久沒人說話時，讓合成程式不要閒置結束（下一句還要重新載入，那句就被略過）。沒在跑就不管"""
        try:
            _http("POST", self._base() + "/ping", b"{}", {"Content-Type": "application/json"}, timeout=5)
        except (urllib.error.URLError, OSError):
            pass

    def check_vram(self):
        """合成中：剩不到 CUDA_VRAM_LOW_GB 就警告（同一種 5 分鐘內只說一次）。回傳 vram()"""
        v = self.vram()
        if v:
            msg = vram_low(v.get("free_gb"), v.get("total_gb"))
            if msg:
                _vram_warn("low", msg)
        return v

    def _note_vram(self, headers):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            free = float(h["x-tts-vram-free"])
            total = float(h["x-tts-vram-total"]) if h.get("x-tts-vram-total") else None
        except (KeyError, ValueError):
            return
        msg = vram_low(free, total)
        if msg:
            _vram_warn("low", msg)

    @staticmethod
    def _precheck():
        """要開合成程式之前：顯示卡剩的放不下就先警告（照樣試，載入失敗會再說原因）"""
        g = gpu_memory()
        if g:
            msg = vram_shortage(g["free_gb"], g["total_gb"])
            if msg:
                _vram_warn("shortage", msg)

    # 埠號、啟動指令、紀錄檔、擴散步數：Mac 本機口譯（MlxWorkerProvider，v2.34.0）照同一套方式叫自己的合成程式
    port_env = "JTLW_TTS_LOCAL_PORT"
    log_name = "local_tts_worker.log"

    @staticmethod
    def _port():
        return CUDA_PORT

    def _base(self):
        return f"http://127.0.0.1:{self._port()}"

    def _cmd(self):
        return [_cuda_python(), self.server, "--tts-worker", str(self._port())]

    def _steps(self):
        return 10

    def _ensure(self):
        """合成程式在（別的程式開的也算）就用；沒有就開一個，等它載入模型"""
        with self._lock:
            deadline = time.monotonic() + CUDA_LOAD_WAIT
            proc = None
            while True:
                try:
                    code, body, _ = _http("GET", self._base() + "/health", timeout=5)
                except (urllib.error.URLError, OSError):
                    code, body = None, b""
                if code == 200:
                    return
                if code == 500:
                    detail = _err_detail(body) or "原因不明"
                    raise TTSError("synth_failed", "本機合成程式載入失敗：" + detail + _oom_hint(detail)
                                   + self._log_hint(), 503)
                if code is None:
                    if proc is None:
                        self._precheck()
                        proc = self._spawn()
                    elif proc.poll() is not None:
                        raise TTSError("synth_failed", f"本機合成程式啟動失敗（結束碼 {proc.returncode}）" + self._log_hint(), 503)
                elif code != 503:               # 埠號被別的程式佔用
                    raise TTSError("synth_failed", f"本機合成的埠號 {self._port()} 被別的程式佔用（HTTP {code}）："
                                   f"可以用環境變數 {self.port_env} 換一個", 503)
                if time.monotonic() > deadline:
                    raise TTSError("synth_failed", f"本機合成程式 {CUDA_LOAD_WAIT} 秒內沒有載入完成" + self._log_hint(), 503)
                time.sleep(1)

    def _log_path(self):
        return os.path.join(TMP_DIR, self.log_name)

    def _log_hint(self):
        try:
            with open(self._log_path(), encoding="utf-8", errors="replace") as f:
                tail = [x.strip() for x in f.read().splitlines() if x.strip()][-3:]
        except OSError:
            tail = []
        return ("（紀錄 " + self._log_path() + "：" + " / ".join(tail)[-300:] + "）") if tail else ""

    def _spawn(self):
        """跟啟動它的程式脫鉤：WebUI 預先叫醒的，主程式朗讀時接著用；Windows 不開主控台視窗"""
        os.makedirs(TMP_DIR, exist_ok=True)
        # PYTHONUTF8：g2pw 開它的字表沒指定編碼，Windows 預設是 cp1252／cp950，載入就失敗（2026-11-03 PC-004 實測；Linux、Mac 預設 UTF-8 沒事）
        env = dict(os.environ, JT_TTS_DIR=DATA_DIR, JT_TTS_WORKER_IDLE=str(CUDA_IDLE), PYTHONIOENCODING="utf-8",
                   PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        kw = dict(stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=ROOT, env=env)
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000      # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        with open(self._log_path(), "w", encoding="utf-8") as log:
            return subprocess.Popen(self._cmd(), stdout=log, **kw)

    def _post(self, path, req, timeout=600):
        for attempt in (1, 2):
            self._ensure()
            try:
                return _http("POST", self._base() + path, json.dumps(req).encode(), {"Content-Type": "application/json"},
                             timeout=timeout)
            except (urllib.error.URLError, ConnectionError) as e:
                # 剛好碰到它閒置結束（查完 /health 才結束）：重開一次
                if attempt == 2:
                    raise TTSError("synth_failed", f"本機合成程式沒有回應（{type(e).__name__}）" + self._log_hint(), 503)
            except OSError as e:                # 逾時
                raise TTSError("synth_failed", f"本機合成程式沒有回應（{type(e).__name__}）" + self._log_hint(), 503)

    def synth(self, text, voice, custom, steps=None, lang="zh"):
        """一段 → (WAV 位元組, 送進模型的文字)。擴散步數固定 10（同 GPU 伺服器；Mac 的 6／10 不套用）"""
        req = {"text": text, "voice_wav": os.path.abspath(voice["wav"]), "voice_text": voice["transcript"],
               "custom": custom or {}, "steps": self._steps()}
        if lang != "zh":
            req["lang"] = lang
        code, body, headers = self._post("/synthesize", req)
        if code != 200:
            detail = _err_detail(body) or f"HTTP {code}"
            raise TTSError("synth_failed", "本機合成失敗：" + detail + _oom_hint(detail), 503 if code == 503 else 502)
        self._note_vram(headers)
        sp = {k.lower(): v for k, v in headers.items()}.get("x-tts-spoken", "")
        return body, urllib.parse.unquote(sp)

    def synth_stream(self, text, voice, lang="zh", block=9600, custom=None):
        """串流合成（雙向口譯，v2.33.0 beta）：一段一段產生 (PCM, 取樣率)。介面同 RemoteProvider.synth_stream"""
        import http.client
        req = {"text": text, "voice_wav": os.path.abspath(voice["wav"]), "voice_text": voice["transcript"],
               "custom": custom or {}, "steps": self._steps()}
        if lang != "zh":
            req["lang"] = lang
        for attempt in (1, 2):
            self._ensure()
            conn = http.client.HTTPConnection("127.0.0.1", self._port(), timeout=120)
            try:
                conn.request("POST", "/synthesize_stream", json.dumps(req), {"Content-Type": "application/json"})
                r = conn.getresponse()
            except (ConnectionError, http.client.HTTPException) as e:
                conn.close()                    # 剛好碰到它閒置結束：重開一次
                if attempt == 2:
                    raise TTSError("synth_failed", f"本機合成程式沒有回應（{type(e).__name__}）" + self._log_hint(), 503)
                continue
            except OSError as e:                # 逾時
                conn.close()
                raise TTSError("synth_failed", f"本機合成程式沒有回應（{type(e).__name__}）" + self._log_hint(), 503)
            break
        try:
            if r.status != 200:
                detail = _err_detail(r.read()) or f"HTTP {r.status}"
                raise TTSError("synth_failed", "本機串流合成失敗：" + detail + _oom_hint(detail),
                               503 if r.status in (404, 405, 503) else 502)
            self._note_vram(dict(r.getheaders()))
            sr = int(r.getheader("X-TTS-SR") or 48000)
            while True:
                b = r.read(block)
                if not b:
                    break
                if len(b) % 2:                  # 16-bit：湊成整數個樣本
                    b += r.read(1)
                yield b, sr
        finally:
            conn.close()

    def convert(self, text, custom):
        code, body, _ = self._post("/convert", {"text": text, "custom": custom or {}})
        if code != 200:
            raise TTSError("convert_failed", "台灣念法轉換失敗：" + (_err_detail(body) or f"HTTP {code}"), 502)
        return json.loads(body).get("spoken", "")


# ── Apple Silicon 本機口譯（v2.34.0，beta；2026-11-03 使用者：「要讓 Mac 也能用本機口譯，但預設還是推薦 GPU server
# 或 Windows 本機＋GPU」）──────────────────────────────────────────────────────────────
# 口譯的合成放在另一個程式（jtlw_tts/mlx_worker.py）：主程式的 mlx-whisper 辨識也用 Metal，同一個程式裡不能同時推論，
# 加鎖排隊的話辨識等超過一個步進就丟掉那段。朗讀照舊在主程式裡合成（MlxProvider，沒有辨識在搶）。
# Mac 合成大約跟說話一樣快（M5 6 步 RTF 1.07～1.2），而且 mlx-audio 的 VoxCPM2 不能串流：一句合成完才念，兩邊連續說話時會跟不上
MLX_PORT = int(os.environ.get("JTLW_TTS_MLX_PORT") or 19787)


class MlxWorkerProvider(CudaProvider):
    """Apple Silicon 本機合成程式（口譯用，beta）：python -m jtlw_tts.mlx_worker，共用一份、閒置自己結束（同 CudaProvider）"""
    kind = "mlx"
    label = "本機（Apple Silicon，beta）"
    # 2026-11-03 MacBook Air 32 GB 實測：合成要花語音長度的 1.5～2.7 倍（同時辨識時更慢）；兩邊每 3 秒輪流一句，每句等 8～20 秒、8 句略過 2 句
    note = ("Mac 合成比說話慢、一句合成完才念，只適合偶爾說幾句；兩邊連續說話時會跟不上（等超過 15 秒的句子會略過）。"
            "建議用 GPU 伺服器或 Windows＋NVIDIA 顯示卡")
    _lock = threading.Lock()
    port_env = "JTLW_TTS_MLX_PORT"
    log_name = "local_tts_mlx_worker.log"

    def __init__(self, steps=6):
        self.steps = steps

    @staticmethod
    def unavailable():
        return MlxProvider.unavailable()

    def health(self):
        """不會啟動合成程式。不能串流（一句合成完才念）；英文用 _tts_en_text（同 GPU 伺服器）"""
        why = self.unavailable()
        if why is not None:
            return {"available": False, "reason": why}
        return {"available": True, "reason": None, "stream": False, "langs": ["zh", "en"], "beta": True}

    @staticmethod
    def _port():
        return MLX_PORT

    def _cmd(self):
        return [sys.executable, "-m", "jtlw_tts.mlx_worker", str(self._port())]

    def _steps(self):
        return int(self.steps)

    @staticmethod
    def _precheck():
        pass                                    # Mac 沒有獨立的顯示卡記憶體可以查

    def running_vram(self):
        return None

    def vram(self):
        return None

    def check_vram(self):
        return None

    def synth_stream(self, text, voice, lang="zh", block=9600, custom=None):
        raise TTSError("stream_unsupported", "Mac 本機合成不能串流", 501)


_PICK_CACHE = {"t": 0.0, "key": None, "val": None}


def pick_provider(cfg, refresh=False, where=None, steps=None, model=None):
    """(provider, None) 或 (None, 原因)。auto：GPU 伺服器優先，其次本機 NVIDIA 顯示卡（beta）、Apple Silicon 本機。結果快取 30 秒。
    where（auto／remote／cuda／mlx）、steps 給的話蓋過設定檔（命令列 --tts-provider、--tts-steps）。
    model：合成模型（MODELS），預設 VoxCPM2；BreezyVoice 只在 GPU 伺服器，不退回本機"""
    s = settings(cfg)
    model = model or DEFAULT_MODEL
    if model not in MODELS:
        return None, f"沒有這個合成模型：{model}（可用：{'、'.join(MODELS)}）"
    if where:
        s["provider"] = where
    if steps:
        s["mac_steps"] = int(steps)
    if s["provider"] not in ("auto", "remote", "cuda", "mlx"):
        s["provider"] = "auto"
    if "mlx" not in MODELS[model]["where"]:
        if s["provider"] in ("mlx", "cuda"):
            return None, f"{MODELS[model]['label']}只在 GPU 伺服器提供（本機不提供）"
        s["provider"] = "remote"
    rw = (cfg or {}).get("remote_whisper") or {}
    key = (s["provider"], rw.get("host"), rw.get("whisper_port"), s["mac_steps"], model)
    if not refresh and _PICK_CACHE["key"] == key and time.time() - _PICK_CACHE["t"] < 30:
        return _PICK_CACHE["val"]
    reason = None
    val = None
    if s["provider"] in ("auto", "remote") and rw.get("host"):
        p = RemoteProvider(rw["host"], rw.get("whisper_port", 8978), model)
        try:
            h = p.health()
            if h.get("available"):
                val = (p, None)
                if os.path.exists(_pending_deletes_path()):
                    flush_pending_deletes(p)          # 上次刪聲音時伺服器連不上：現在補刪
            else:
                reason = h.get("reason") or "GPU 伺服器不能合成"
        except Exception as e:
            reason = f"連不到 GPU 伺服器（{type(e).__name__}）"
    elif s["provider"] == "remote":
        reason = "沒有設定 GPU 伺服器" + ("" if model == DEFAULT_MODEL else f"（{MODELS[model]['label']}只在 GPU 伺服器提供）")
    if val is None and s["provider"] in ("auto", "cuda"):
        why = CudaProvider.unavailable()
        if why is None:
            val = (CudaProvider(), None)
        elif s["provider"] == "cuda":
            reason = why
    if val is None and s["provider"] in ("auto", "mlx"):
        why = MlxProvider.unavailable()
        val = (MlxProvider(s["mac_steps"]), None) if why is None else None
        # auto、沒設 GPU 伺服器、又不是 Apple Silicon：只說「本機只支援 Mac」會讓人以為這台永遠不能用，改說兩條路
        # （why 是 None＝可以用：v2.27.0～v2.31.8 沒擋，沒設 GPU 伺服器的 Apple Silicon Mac 在這裡 AttributeError）
        if why is not None and reason is None and (s["provider"] == "mlx" or not why.startswith("本機合成只支援")):
            reason = why
    if val is None:
        val = (None, reason or "這台不能自己合成語音：請在設定頁設定 GPU 伺服器，並在 GPU 伺服器安裝文字轉語音（重新執行安裝程式）。"
                               "本機合成只支援 Apple Silicon Mac，以及有 NVIDIA 顯示卡的 Windows（beta，.\\install.ps1 -LocalTTS）")
    _PICK_CACHE.update(t=time.time(), key=key, val=val)
    return val


def pick_interp_provider(cfg, refresh=True):
    """雙向口譯的合成 → (provider, None) 或 (None, 原因)。GPU 伺服器優先；沒有能用的 GPU 伺服器時，
    裝了本機 NVIDIA 合成（beta）就用本機（v2.33.0）。設定的合成位置是本機 NVIDIA（cuda）時先用本機。
    Apple Silicon 本機排最後（beta，v2.34.0）：合成大約跟說話一樣快、不能串流，兩邊連續說話時會跟不上，
    所以合成位置設成 mlx 也照樣先用 GPU 伺服器（使用者：預設還是推薦 GPU 伺服器或 Windows 本機＋NVIDIA）"""
    order = ("cuda", "remote") if settings(cfg)["provider"] == "cuda" else ("remote", "cuda")
    why = {}
    for where in order:
        p, why[where] = pick_provider(cfg, refresh=refresh, where=where)
        if p is not None:
            return p, None
    if sys.platform == "darwin":
        p, why["mlx"] = pick_provider(cfg, refresh=refresh, where="mlx")
        if p is not None:
            return MlxWorkerProvider(getattr(p, "steps", 6)), None
    msg = why["remote"] or "GPU 伺服器不能合成"
    if sys.platform == "darwin":
        msg += "；本機（Apple Silicon，beta）：" + (why.get("mlx") or "不能用")
    elif os.name == "nt":
        msg += "；本機 NVIDIA 顯示卡（beta）：" + (why["cuda"] or "不能用")
    return None, msg


# ── 要朗讀的文字 ───────────────────────────────────────────────
_TS = re.compile(r"[\[［【(（]\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–～~]\s*\d{1,2}:\d{2}(?::\d{2})?)?\s*[\]］】)）]")
_REF = re.compile(r"[\[［]\s*(?:\d+|[A-Za-z]\d+)(?:\s*[,，、]\s*(?:\d+|[A-Za-z]\d+))*\s*[\]］]")


def reading_text(text):
    """朗讀前整理：拿掉時間戳、來源編號、Markdown 符號；網址的查詢參數不念（規格 v1 第七節）"""
    t = _TS.sub("", text or "")
    t = _REF.sub("", t)
    t = re.sub(r"(https?://[^\s?#]+)[?#]\S*", r"\1", t)
    lines = []
    for ln in t.splitlines():
        ln = re.sub(r"^\s{0,3}(?:#{1,6}\s+|[-*+•]\s+|>\s*|\d+[.)、]\s+)", "", ln)
        ln = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", ln)
        ln = re.sub(r"\s+([，。！？、；：,.!?;:])", r"\1", ln).strip()     # 拿掉時間戳後留下的「韌體 。」
        if ln and not re.fullmatch(r"[-=─_*]{3,}", ln):
            lines.append(ln)
    return "\n".join(lines)


def summary_reading_text(text):
    """摘要 .txt → 要朗讀的文字：只念「重點摘要」那一段（後面是各段的校正逐字稿）；找不到就整份整理"""
    m = re.search(r"^##\s*重點摘要\s*$(.*?)(?=^---|^##\s|\Z)", text or "", re.S | re.M)
    return reading_text(m.group(1) if m else re.sub(r"^---.*?^---\s*$", "", text or "", flags=re.S | re.M))


_SUB_TS = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}")


def subtitle_reading_text(text):
    """SRT／VTT 字幕檔 → 要朗讀的文字：拿掉 WEBVTT 標頭、段落編號、時間軸、NOTE 與格式標籤"""
    lines, skip = [], False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            skip = False
            continue
        if skip or s.upper().startswith("WEBVTT") or s.isdigit() or _SUB_TS.match(s):
            continue
        if s.startswith("NOTE") or s.startswith("STYLE") or s.startswith("REGION"):
            skip = True
            continue
        s = re.sub(r"<[^>]+>", "", s).strip()
        if s:
            lines.append(s)
    return reading_text("\n".join(lines))


def decode_text(data):
    """文字檔位元組 → 字串：BOM（UTF-8／UTF-16）、UTF-8，不是的話試 Big5（舊的 Windows 記事本存檔）"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("cp950", "big5hkscs"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def prepare_text(text, name=""):
    """依內容與副檔名整理成要朗讀的文字：字幕檔去時間軸；我們的摘要檔只念「重點摘要」；其他拿掉 Markdown 符號"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in (".srt", ".vtt") or (text or "").lstrip().upper().startswith("WEBVTT"):
        return subtitle_reading_text(text)
    if re.search(r"^##\s*重點摘要\s*$", text or "", re.M):
        return summary_reading_text(text)
    return reading_text(text)


def load_text(path):
    with open(path, "rb") as f:
        data = f.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise TTSError("text_too_long", "文字檔超過 8 MB")
    return prepare_text(decode_text(data), path)


# ── 音訊：語速、取樣率 ─────────────────────────────────────────
def wav_pcm(data):
    """WAV 位元組 → (單聲道 16-bit PCM, 取樣率)"""
    with wave.open(io.BytesIO(data), "rb") as r:
        ch, sw, sr = r.getnchannels(), r.getsampwidth(), r.getframerate()
        pcm = r.readframes(r.getnframes())
    if sw != 2 or ch != 1:
        raise TTSError("audio_format", f"合成結果的格式不對（{ch} 聲道、{sw * 8} 位元）", 500)
    return pcm, sr


def pcm_wav(pcm, sr):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def to_pcm(data, rate=1.0, out_sr=None):
    """合成結果（WAV）→ 單聲道 16-bit PCM。rate≠1 時用 ffmpeg atempo 改語速（音調不變），
    out_sr 給的話一併重新取樣（播放裝置不支援模型的取樣率時）"""
    pcm, sr = wav_pcm(data)
    rate = float(rate or 1.0)
    osr = int(out_sr or sr)
    if abs(rate - 1.0) < 1e-3 and osr == sr:
        return pcm, sr
    if not RATE_MIN <= rate <= RATE_MAX:
        raise TTSError("invalid_rate", f"語速要在 {RATE_MIN}～{RATE_MAX} 倍之間")
    ff = _ffmpeg()
    if not ff:
        raise TTSError("ffmpeg_missing", "找不到 ffmpeg，無法調整語速", 500)
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sr), "-ac", "1",
           "-i", "pipe:0"]
    if abs(rate - 1.0) >= 1e-3:
        cmd += ["-filter:a", f"atempo={rate:.3f}"]
    cmd += ["-ar", str(osr), "-ac", "1", "-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, input=pcm, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise TTSError("ffmpeg_failed", "調整語速失敗：" + r.stderr.decode(errors="replace").strip()[-200:], 500)
    return r.stdout, osr


def stream_tempo(chunks, rate):
    """串流合成的 (pcm, sr) 一段一段改語速（ffmpeg atempo，音調不變），合成一段就交一段。

    口譯預設 1.2 倍（2026-10-12 使用者）：以前語速不是 1 就不用串流、等整句合成完才調速再念，每句晚好幾秒。
    一個 ffmpeg 行程從頭接到尾（逐段各開一個會在段落接縫產生雜音）；來源的錯誤（GPU 伺服器斷線＝OSError）原樣丟出，
    呼叫端照舊判斷斷線；中途不要了（取消、靜音）就關掉 ffmpeg 與來源"""
    rate = float(rate or 1.0)
    if abs(rate - 1.0) < 1e-3:
        yield from chunks
        return
    if not RATE_MIN <= rate <= RATE_MAX:
        raise TTSError("invalid_rate", f"語速要在 {RATE_MIN}～{RATE_MAX} 倍之間")
    ff = _ffmpeg()
    if not ff:
        raise TTSError("ffmpeg_missing", "找不到 ffmpeg，無法調整語速", 500)
    it = iter(chunks)
    first = next(it, None)
    if first is None:
        return
    pcm0, sr = first
    # nobuffer／low_delay／probesize：不加的話 ffmpeg 先讀約 1.2 秒才開始輸出（實測第一段晚 1.2 秒），加了收到就出
    p = subprocess.Popen([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                          "-probesize", "32", "-analyzeduration", "0", "-f", "s16le", "-ar", str(sr), "-ac", "1",
                          "-i", "pipe:0", "-filter:a", f"atempo={rate:.3f}", "-f", "s16le", "-ar", str(sr), "-ac", "1",
                          "-flush_packets", "1", "pipe:1"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    err = []

    def feed():
        try:
            p.stdin.write(pcm0)
            for pcm, _sr in it:
                p.stdin.write(pcm)
        except BaseException as e:                          # 來源出錯，或這邊不要了（ffmpeg 已關，寫入 BrokenPipe）
            err.append(e)
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    th = threading.Thread(target=feed, daemon=True)
    th.start()
    done = False
    try:
        blk = max(2, int(sr * 0.1)) * 2                     # 約 0.1 秒一塊
        while True:
            b = p.stdout.read(blk)
            if not b:
                break
            if len(b) % 2:
                b += p.stdout.read(1) or b"\0"
            yield b, sr
        th.join(30)
        if err and not isinstance(err[0], BrokenPipeError):
            raise err[0]
        if p.wait(timeout=10) != 0:
            raise TTSError("ffmpeg_failed", "調整語速失敗：" + p.stderr.read().decode(errors="replace").strip()[-200:], 500)
        done = True
    finally:
        if p.poll() is None:
            p.kill()
        if not done:
            th.join(2)
            close = getattr(it, "close", None)
            if close and not th.is_alive():
                try:
                    close()
                except Exception:
                    pass


# ── 一次朗讀的合成流程 ─────────────────────────────────────────
class Session:
    """依序合成每一段。ahead=None：全部依序合成（轉成音檔）；否則只合成到「目前位置＋ahead」段
    （朗讀時不無限預先合成，停下來時也不會白白算一堆）。播放端用 get(i) 依序取。
    出錯的段落讓整次朗讀停下並說明是第幾段（不可以跳過不說，聽的人會以為內容就是這樣）"""

    def __init__(self, provider, voice, custom, segments, rate=1.0, steps=None, ahead=PREFETCH, first=0):
        """first：segments[0] 在整份文字裡是第幾段（從中間重念時），錯誤訊息照整份文字的段號說"""
        self.provider, self.voice, self.custom, self.first = provider, voice, custom, int(first)
        self.segments, self.rate, self.steps, self.ahead = list(segments), float(rate or 1.0), steps, ahead
        self.out_sr = None
        self.cv = threading.Condition()
        self.results, self.error, self.pos, self.stopped = {}, None, 0, False
        self.thread = threading.Thread(target=self._run, daemon=True, name="tts-session")

    def start(self):
        self.thread.start()
        return self

    def set_position(self, i):
        with self.cv:
            self.pos = max(self.pos, int(i))
            self.cv.notify_all()

    def stop(self):
        with self.cv:
            self.stopped = True
            self.cv.notify_all()

    def get(self, i, timeout=None):
        """第 i 段：{"pcm", "sr", "spoken", "seconds"（合成花的秒數）, "ready"（合成好的 time.monotonic()）}；
        出錯丟 TTSError；停止了回 None。取走後不再保留"""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cv:
            while i not in self.results and self.error is None and not self.stopped:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError
                self.cv.wait(0.5 if left is None else min(left, 0.5))
            if i in self.results:
                return self.results.pop(i)
            if self.error is not None:
                raise self.error
            return None

    def _run(self):
        for i, text in enumerate(self.segments):
            with self.cv:
                while not self.stopped and self.ahead is not None and i > self.pos + self.ahead:
                    self.cv.wait(0.5)
                if self.stopped:
                    return
            t0 = time.monotonic()
            try:
                for attempt in (1, 2):                 # 短暫的服務錯誤（GPU 伺服器重啟、worker 啟動中）重試一次
                    try:
                        data, sp = self.provider.synth(text, self.voice, self.custom, self.steps)
                        break
                    except TTSError as e:
                        if e.status not in (502, 503) or attempt == 2 or self.stopped:
                            raise
                        time.sleep(2)
                pcm, sr = to_pcm(data, self.rate, self.out_sr)
            except TTSError as e:
                err = TTSError(e.code, f"第 {self.first + i + 1} 段合成失敗：{e.message}", e.status)
            except Exception as e:                      # noqa: BLE001  網路中斷等：照樣說是哪一段
                err = TTSError("synth_failed", f"第 {self.first + i + 1} 段合成失敗：{type(e).__name__}: {e}", 500)
            else:
                with self.cv:
                    now = time.monotonic()
                    self.results[i] = {"pcm": pcm, "sr": sr, "spoken": sp, "seconds": now - t0, "ready": now}
                    self.cv.notify_all()
                continue
            with self.cv:
                self.error = err
                self.cv.notify_all()
            return


def sweep_tmp(max_age_hours=24):
    """瀏覽器播放留下的每段音檔：超過 max_age_hours 的整個資料夾刪掉（程式中途被砍時收不到尾）"""
    if not os.path.isdir(TMP_DIR):
        return
    now = time.time()
    for name in os.listdir(TMP_DIR):
        p = os.path.join(TMP_DIR, name)
        try:
            if now - os.path.getmtime(p) > max_age_hours * 3600:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass
