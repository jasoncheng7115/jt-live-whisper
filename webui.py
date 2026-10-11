#!/usr/bin/env python3
"""jt-live-whisper WebUI — 瀏覽器介面（設定 + 即時字幕）

啟動方式：
    ./start.sh --webui           # 透過啟動腳本
    python3 webui.py             # 直接啟動

瀏覽器中完成所有設定，點「開始」後自動啟動 translate_meeting.py。
"""

import argparse
import asyncio
import json
import collections
import re
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


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

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("[錯誤] 需要安裝 fastapi 和 uvicorn：")
    print("  pip install fastapi uvicorn websockets")
    sys.exit(1)

# python-multipart 是 FastAPI 檔案上傳必要套件，舊版安裝可能缺少。
# 0.0.13 起模組名稱是 python_multipart，舊名 multipart 只剩已標淘汰的相容層：新名優先、舊版才用舊名
_multipart_ok = False
for _mp_name in ("python_multipart", "multipart"):
    try:
        __import__(_mp_name)
        _multipart_ok = True
        break
    except ImportError:
        pass
if not _multipart_ok:
    print("[提示] 正在安裝 python-multipart（檔案上傳需要）...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "python-multipart"])
    print("[完成] python-multipart 已安裝")

# ─── 設定 ────────────────────────────────────────────────────
TCP_PORT = 19780
WEB_PORT = 19781
BASE_DIR = Path(__file__).parent
TRANSLATE_SCRIPT = BASE_DIR / "translate_meeting.py"
CONFIG_FILE = BASE_DIR / "config.json"


def _write_config(cfg):
    """寫 config.json：先寫暫存檔再換上，寫到一半當掉不會留下壞掉的設定檔（壞掉的話主程式會用預設值執行）；
    保留原本的權限（裡面有密碼與 token）（2026-10-05）"""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(CONFIG_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=4))
        if CONFIG_FILE.exists():
            try:
                shutil.copymode(str(CONFIG_FILE), tmp)
            except OSError:
                pass
        os.replace(tmp, str(CONFIG_FILE))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

# 預先匯入 translate_meeting，避免首次 /api/config 才 lazy import 造成冷啟動延遲
try:
    from translate_meeting import (
        WHISPER_MODELS as _TM_WHISPER_MODELS,
        SUMMARY_MODELS as _TM_SUMMARY_MODELS,
        _recommended_whisper_model as _tm_recommended_whisper_model,
        SCK_LOOPBACK_ID as _TM_SCK_LOOPBACK_ID,
        SCK_MIXED_ID as _TM_SCK_MIXED_ID,
        _sck_check as _tm_sck_check,
        _sck_macos_ok as _tm_sck_macos_ok,
        _sck_request_permission as _tm_sck_request_permission,
        _sck_terminal_app_name as _tm_sck_terminal_app_name,
        PULSE_LOOPBACK_ID as _TM_PULSE_LOOPBACK_ID,
        WASAPI_LOOPBACK_ID as _TM_WASAPI_LOOPBACK_ID,
        _find_wasapi_loopback as _tm_find_wasapi_loopback,
        _pulse_available as _tm_pulse_available,
        _pulse_label as _tm_pulse_label,
        _detect_llm_server as _tm_detect_llm_server,
        _parse_llm_host as _tm_parse_llm_host,
        _BUILTIN_TRANSLATE_MODELS as _TM_TRANSLATE_MODELS,
        DEFAULT_TRANSLATE_MODEL as _TM_DEFAULT_TRANSLATE_MODEL,
    SUMMARY_DEFAULT_MODEL as _TM_SUMMARY_DEFAULT_MODEL,
    QWEN_MODEL as _TM_QWEN_MODEL,
    _qwen_server_ready as _tm_qwen_server_ready,
    _qwen_local_backend as _tm_qwen_local_backend,
    _ZH_INPUT_MODES as _TM_ZH_MODES,
    _EN_INPUT_MODES as _TM_EN_MODES,
    _KO_INPUT_MODES as _TM_KO_MODES,
    _tts_output_devices as _tm_tts_output_devices,
    _INTERP_MAC_NEED as _TM_INTERP_MAC_NEED,
    _INTERP_WIN_NEED as _TM_INTERP_WIN_NEED,
    _INTERP_WIN_READY as _TM_INTERP_WIN_READY,
    _interp_win_ready as _tm_interp_win_ready,
    _INTERP_VIRTUAL as _TM_INTERP_VIRTUAL,
)
except Exception:
    _tm_tts_output_devices = None
    _TM_INTERP_MAC_NEED = "需要先安裝 BlackHole 2ch"
    _TM_INTERP_WIN_NEED = "需要先安裝 usbip-win2"
    _TM_INTERP_WIN_READY = "開始時自動建立口譯麥克風"
    _tm_interp_win_ready = None
    _TM_INTERP_VIRTUAL = re.compile(r"blackhole|virtual", re.I)
    _tm_parse_llm_host = None
    _TM_TRANSLATE_MODELS = [("gemma4:26b", "速度快、品質好（推薦，約需 17GB）"),
                            ("qwen2.5:14b", "品質好，較省記憶體（約需 9GB）")]
    _TM_DEFAULT_TRANSLATE_MODEL = "gemma4:26b"
    _TM_WHISPER_MODELS = None
    _TM_SUMMARY_MODELS = None
    _tm_recommended_whisper_model = None
    _TM_SCK_LOOPBACK_ID = -300
    _TM_SCK_MIXED_ID = -400
    _tm_sck_check = None
    _tm_sck_macos_ok = None
    _tm_sck_request_permission = None
    _tm_sck_terminal_app_name = None
    _TM_PULSE_LOOPBACK_ID = -500
    _TM_WASAPI_LOOPBACK_ID = -100
    _tm_find_wasapi_loopback = None
    _tm_pulse_available = None
    _tm_pulse_label = None
    _tm_detect_llm_server = None

# ─── 本機 LLM 伺服器自動探測 ────────────────────────────────────
# 安裝時常先跳過 LLM 設定（還沒裝 Ollama），之後 config.json 就沒有 llm_host。
# 這些伺服器的預設位址是可推斷的：先試連接埠有沒有開，再驗證回傳結構確認真的是 LLM
# 伺服器（8080 常被其他網站服務占用，只看連接埠會誤判）。
_LOCAL_LLM_CANDIDATES = (
    ("127.0.0.1", 11434),   # Ollama
    ("127.0.0.1", 1234),    # LM Studio
    ("127.0.0.1", 8080),    # llama.cpp server / LocalAI
)
_llm_probe_cache = {"t": -1e9, "host": ""}


def _probe_local_llm():
    """回傳本機可用的 LLM 伺服器 "host:port"，找不到回傳空字串（結果快取 30 秒）"""
    now = time.monotonic()
    if now - _llm_probe_cache["t"] < 30:
        return _llm_probe_cache["host"]
    import socket as _socket
    found = ""
    for h, p in _LOCAL_LLM_CANDIDATES:
        try:
            with _socket.create_connection((h, p), timeout=0.25):
                pass
        except OSError:
            continue
        if _tm_detect_llm_server is None or _tm_detect_llm_server(h, p):
            found = f"{h}:{p}"
            break
    _llm_probe_cache["t"] = now
    _llm_probe_cache["host"] = found
    return found


# ─── 安全設定 ──────────────────────────────────────────────────
# 來源 IP 允許清單（`config.json` 的 `webui.allowed_ips`）。
# **空的＝不限制**，維持既有部署的行為；要限制就明確列出來。
# 支援單一 IP 與 CIDR（`192.168.1.0/24`）。本機一律放行，否則設錯清單
# 會把自己鎖在門外，而設定頁本身就只有本機能改——那會變成救不回來的狀態。
_allowed_nets = []


def _load_allowed_ips(cfg):
    import ipaddress
    global _allowed_nets
    nets = []
    raw = (cfg.get("webui") or {}).get("allowed_ips") or []
    env = os.environ.get("JTLW_WEBUI_ALLOWED_IPS", "")
    if env:
        raw = [x.strip() for x in env.split(",") if x.strip()]
    for item in raw:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            print(f"[WebUI] 略過無法解析的 allowed_ips 項目：{item}", flush=True)
    _allowed_nets = nets


# 反向代理：**預設完全不信任 `X-Forwarded-For`**。
# `_is_local()` 只看連線來源，放到代理後面時每一個請求看起來都來自代理本身
# ＝本機，那四個「僅限本機」的設定頁就等於對全世界開放。
# 要用代理就必須把代理的位址明確列進 `webui.trusted_proxies`，
# 只有來自清單內的連線才會去看 XFF，而且取的是**最右邊那個非信任的跳點**
# （最左邊是客戶端自己填的，可以偽造）。
_trusted_proxies = []
_tls_cfg = {"enabled": False, "cert": "", "key": "", "hosts": []}


def _load_proxy_and_tls(cfg):
    import ipaddress
    global _trusted_proxies
    w = cfg.get("webui") or {}
    nets = []
    for item in (w.get("trusted_proxies") or []):
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            print(f"[WebUI] 略過無法解析的 trusted_proxies 項目：{item}", flush=True)
    _trusted_proxies = nets
    _tls_cfg["enabled"] = bool(w.get("tls", False))
    if os.environ.get("JTLW_WEBUI_TLS", "") in ("1", "on", "true"):
        _tls_cfg["enabled"] = True
    _tls_cfg["cert"] = w.get("tls_cert") or str(BASE_DIR / "webui_tls" / "server.crt")
    _tls_cfg["key"] = w.get("tls_key") or str(BASE_DIR / "webui_tls" / "server.key")
    _tls_cfg["hosts"] = w.get("tls_hosts") or []


def _client_ip(request) -> str:
    """真正的客戶端位址。只有連線來自信任的代理時才看 X-Forwarded-For。"""
    peer = request.client.host if request.client else ""
    if not _trusted_proxies or not peer:
        return peer
    import ipaddress
    try:
        if not any(ipaddress.ip_address(peer) in n for n in _trusted_proxies):
            return peer          # 不是從信任的代理來的，XFF 一律不採信
    except ValueError:
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    # 由右往左找第一個不是信任代理的位址——左邊的可以被客戶端偽造
    for part in reversed([x.strip() for x in xff.split(",") if x.strip()]):
        try:
            if not any(ipaddress.ip_address(part) in n for n in _trusted_proxies):
                return part
        except ValueError:
            continue
    return peer


def _ip_allowed(client) -> bool:
    """來源 IP 是否在允許清單內；清單為空時不限制"""
    if not _allowed_nets:
        return True
    if client in ("127.0.0.1", "::1", "localhost", "0.0.0.0", ""):
        return True          # 本機永遠放行，避免把自己鎖在門外
    import ipaddress
    try:
        ip = ipaddress.ip_address(client)
    except ValueError:
        return False
    return any(ip in n for n in _allowed_nets)


# 密碼**只存 sha256 雜湊**（`webui_passwords.read_sha256` / `admin_sha256`）。
# 舊版存的是明文（`read` / `admin`），仍然讀得進來並可登入，
# 但只要從設定頁存過一次就會改寫成雜湊。
_webui_passwords = {"read": "", "admin": ""}   # 這裡放的是雜湊，不是明文


def _pw_hash(raw):
    import hashlib
    raw = (raw or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


def _pw_match(raw, stored_hash):
    """**用 compare_digest 而不是 `==`**：字串比較會在第一個不同的字元就回傳，
    比對時間會洩漏「猜對了幾個字元」。這條路徑是對外開放的。"""
    import secrets as _secrets
    if not stored_hash:
        return False
    return _secrets.compare_digest(_pw_hash(raw), stored_hash)


def _load_passwords():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            wp = cfg.get("webui_passwords", {})
            for role in ("read", "admin"):
                # 新格式優先；沒有才把舊的明文欄位雜湊起來用
                _webui_passwords[role] = (wp.get(f"{role}_sha256")
                                          or _pw_hash(wp.get(role, "")))
            _load_allowed_ips(cfg)
            _load_proxy_and_tls(cfg)
        except Exception as e:
            # **不可以靜默吞掉**：設定讀失敗時「密碼是空的」與「允許清單是空的」
            # 都代表安全設定沒有生效，而兩者的預設都是比較寬鬆的那一邊。
            # 原本這裡是 `pass`，一個 NameError 就能讓整組設定無聲失效。
            print(f"[WebUI] 安全設定載入失敗，將以預設值執行：{e}", flush=True)


_load_passwords()

def _is_local(request) -> bool:
    """判斷是否為本機連線。

    **走 `_client_ip()` 而不是直接讀 `request.client.host`**：
    放到反向代理後面時，每個請求的來源都會是代理本身＝看起來像本機，
    那四個「僅限本機」的設定頁（裡面有密碼與轉發 token）就等於對外開放。
    """
    client = _client_ip(request)
    return client in ("127.0.0.1", "::1", "localhost", "0.0.0.0")

def _check_auth(request, level="read") -> str:
    """檢查授權，回傳 None（通過）或錯誤訊息"""
    if _is_local(request):
        return None  # 本機不需密碼
    if level == "admin":
        if not _webui_passwords["admin"]:
            return "未啟用遠端管理功能"
        token = request.headers.get("X-Auth-Token", "")
        if not _pw_match(token, _webui_passwords["admin"]):
            return "需要管理密碼"
    elif level == "read":
        if not _webui_passwords["read"]:
            return None  # 唯讀密碼為空 = 不需密碼
        token = request.headers.get("X-Auth-Token", "")
        if not (_pw_match(token, _webui_passwords["read"])
                or _pw_match(token, _webui_passwords["admin"])):
            return "需要密碼"
    return None


# ─── App ─────────────────────────────────────────────────────
from contextlib import asynccontextmanager

# 子程序管理
_proc: subprocess.Popen = None
_proc_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    t = threading.Thread(target=_tcp_receiver, daemon=True)
    t.start()
    asyncio.create_task(_event_dispatcher())
    try:
        _qwen_status()               # 一啟動就先查 Qwen3-ASR 在 GPU 伺服器／本機能不能跑（背景，不擋啟動）
    except Exception:
        pass
    yield
    # shutdown: kill subprocess（純錄音要等存檔完成，放執行緒裡等）
    await asyncio.to_thread(_stop_proc)


app = FastAPI(title="jt-live-whisper WebUI", lifespan=lifespan)


@app.middleware("http")
async def _ip_allowlist(request, call_next):
    """來源 IP 限制。**擋在所有路由之前**——逐個端點加檢查一定會漏，
    而漏掉的那個就是出事的那個（2026-09-22 盤點時發現四個端點沒有任何防護）。
    """
    client = _client_ip(request)
    if not _ip_allowed(client):
        return JSONResponse({"ok": False, "error": "來源位址不在允許清單內"},
                            status_code=403)
    return await call_next(request)

# ─── 靜態檔案服務（logs/ 子目錄，供 WebUI 開啟逐字稿/摘要 HTML）───
# v2.22.2 前是 app.mount(StaticFiles)——**完全不經授權**，區網內知道檔名（時間戳可推）就能讀逐字稿與摘要。
# 改成路由：read 權限；瀏覽器點連結帶不了標頭，所以**只有這條**接受 ?token=。
# recordings/（錄音、朗讀存的音訊檔）同一條：結束卡片一直有這些檔案的連結，卻從來沒有路由（點了是 404，v2.27.0 才發現）
@app.get("/logs/{rel:path}")
@app.get("/recordings/{rel:path}")
async def serve_logs(request: Request, rel: str):
    route = getattr(request.scope.get("route"), "path", "") or request.url.path
    top = "recordings" if route.startswith("/recordings") else "logs"
    token = ""
    if not _is_local(request) and _webui_passwords["read"]:
        # 逐字稿 HTML 用相對路徑載入同資料夾的音檔，那個請求帶不了 ?token=：
        # 第一次用 ?token= 驗過後發一個只限 /logs 的 cookie，後續請求靠它
        token = (request.headers.get("X-Auth-Token", "") or request.query_params.get("token", "")
                 or request.cookies.get("jtlw_logs", ""))
        if not (_pw_match(token, _webui_passwords["read"]) or _pw_match(token, _webui_passwords["admin"])):
            return JSONResponse({"ok": False, "error": "需要密碼"}, status_code=403)
    logs_dir = (BASE_DIR / top).resolve()
    target = (logs_dir / rel).resolve()
    if logs_dir not in target.parents or not target.is_file():
        return JSONResponse({"ok": False, "error": "找不到檔案"}, status_code=404)
    from fastapi.responses import FileResponse
    resp = FileResponse(str(target))
    if token and request.query_params.get("token") and top == "logs":
        resp.set_cookie("jtlw_logs", token, path="/logs", httponly=True, samesite="strict")
    return resp

# ─── WebSocket 連線管理 ──────────────────────────────────────
connected_clients: list[WebSocket] = []


async def broadcast(message: str):
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in connected_clients:
            connected_clients.remove(ws)


# ─── TCP 接收器 ──────────────────────────────────────────────
_event_queue: asyncio.Queue = None


def _tcp_receiver():
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", TCP_PORT))
    srv.listen(1)
    srv.settimeout(1.0)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            continue
        buf = ""
        conn.settimeout(0.5)
        while True:
            try:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if '"finishing"' in line:
                        _note_finishing()
                    if line.startswith('{"type": "started"'):
                        # 標上這一次的行程編號（跟結束事件的 pid 同一個來源：WebUI 啟動的那個行程；Windows 的 venv
                        # python.exe 是啟動器，子程式自己的 PID 不一樣）。換下一次（重念）時前端用它忽略上一次晚到的結束事件
                        p = _proc
                        if p is not None:
                            try:
                                ev = json.loads(line)
                                ev["pid"] = p.pid
                                line = json.dumps(ev, ensure_ascii=False)
                            except ValueError:
                                pass
                    if line and _event_queue:
                        try:
                            _event_queue.put_nowait(line)
                        except Exception:
                            pass
            except socket.timeout:
                continue
            except Exception:
                break
        try:
            conn.close()
        except Exception:
            pass


async def _event_dispatcher():
    global _event_queue
    _event_queue = asyncio.Queue(maxsize=500)
    while True:
        msg = await _event_queue.get()
        await broadcast(msg)


# ─── 子程序管理 ──────────────────────────────────────────────
PAUSE_FLAG = BASE_DIR / ".webui_pause"     # translate_meeting.py 的 _WEBUI_PAUSE_FLAG


def _set_pause_flag(paused):
    try:
        if paused:
            PAUSE_FLAG.write_text("1")
        elif PAUSE_FLAG.exists():
            PAUSE_FLAG.unlink()
    except OSError:
        pass


# 子程序收尾時（錄音轉 MP3 等）每秒送 "finishing" 心跳。按下停止後先給 4 秒，之後只要 10 秒內還有心跳
# 就繼續等，不升級成 SIGTERM（v2.26.4）。以前一律 4 秒就 SIGTERM：1 小時的錄音轉檔轉到一半被砍，
# 畫面寫「程式異常結束（錯誤碼 -15）」也看不到檔案在哪。卡住（沒有心跳）的照舊強制結束
_STOP_GRACE = 4.0
_FINISH_IDLE = 10.0
_FINISH_MAX = 4 * 3600.0
_finishing_at = 0.0


def _note_finishing():
    global _finishing_at
    _finishing_at = time.monotonic()


def _stop_should_escalate(since_stop, since_beat):
    """按下停止 since_stop 秒、上次心跳 since_beat 秒前（沒有心跳是 None）：要不要改用 SIGTERM"""
    if since_stop < _STOP_GRACE:
        return False
    if since_stop >= _FINISH_MAX:
        return True
    return since_beat is None or since_beat >= _FINISH_IDLE


def _wait_graceful(p, stop_t):
    """送出停止信號之後等子程序自己結束；回傳 True＝結束了，False＝該強制結束"""
    told = False
    while True:
        try:
            p.wait(timeout=0.5)
            return True
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        beat = _finishing_at
        since_beat = (now - beat) if beat >= stop_t - _FINISH_IDLE else None
        if _stop_should_escalate(now - stop_t, since_beat):
            return False
        if since_beat is not None and not told:
            told = True
            print("  正在儲存（錄音轉檔中），完成後才結束；不要關閉這個視窗", flush=True)


def _stop_proc():
    """停止子程序，三段升級：graceful → SIGTERM → SIGKILL。
    Windows 上若子程序在 native crash（如 0xC0000409）卡死，
    SIGINT/CTRL_BREAK 不一定收得到，必須走 SIGKILL 才殺得掉。
    子程序還在存檔（有 finishing 心跳）時不升級，見 _stop_should_escalate"""
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            pid = _proc.pid
            _proc._user_stop = True
            # Step 1：graceful（平台相關）
            try:
                stop_t = time.monotonic()
                if sys.platform == "win32":
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.kill(pid, signal.SIGINT)
                if not _wait_graceful(_proc, stop_t):
                    raise subprocess.TimeoutExpired(_proc.args, _STOP_GRACE)
            except subprocess.TimeoutExpired:
                # Step 2：SIGTERM
                try:
                    os.kill(pid, signal.SIGTERM)
                    _proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, Exception):
                    # Step 3：SIGKILL（無條件強殺）
                    try:
                        os.kill(pid, 9)
                        _proc.wait(timeout=1)
                    except Exception:
                        pass
            except Exception:
                # graceful 失敗（PID 不存在等）→ 直接強殺保險
                try:
                    os.kill(pid, 9)
                    _proc.wait(timeout=1)
                except Exception:
                    pass
            _proc = None
    # 清理靜音 flag 檔案
    for fn in (".mute_lb", ".mute_mic"):
        try:
            (BASE_DIR / fn).unlink()
        except Exception:
            pass
    _set_pause_flag(False)          # 停在暫停中結束時旗標也要收掉（v2.26.3）
    # 停止懸浮字幕子程序
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object "
                 "{$_.CommandLine -like '*subtitle_overlay.py*'} | "
                 "Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            for line in r.stdout.strip().splitlines():
                pid = line.strip()
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.run(["pkill", "-f", "subtitle_overlay.py"],
                           capture_output=True)
    except Exception:
        pass


# ─── Ctrl+C 結束 WebUI ─────────────────────────────────────────
# 2026-10-09 Mac 實機：朗讀中按 Ctrl+C，WebUI 怎麼按都結束不了。第一次 Ctrl+C 的處理函式在 _stop_proc 裡拿著 _proc_lock
# 等子程式存檔；畫面上沒有任何訊息，使用者再按 → Python 在**同一條執行緒**再跑一次處理函式 → 又去拿同一把鎖 → 永遠等不到
# （Mac 上疊了約 20 層）。所以：第二次 Ctrl+C 一律立刻結束、絕不碰 _proc_lock；子程式已經收到停止信號，會自己在背景存完檔
# （v2.26.4 實測過：WebUI 先結束時，轉檔照樣完成）。第一次按就要說「正在停止」，不然一定會被連按
_STOPPING = [False]


def _say(msg):
    """在信號處理函式裡印訊息：主執行緒剛好在寫 stdout 時，print 會丟「reentrant call」，不可以讓它中斷結束流程"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def _force_exit_now():
    """第二次 Ctrl+C：不等存檔、不拿 _proc_lock，立刻結束 WebUI。子程式還沒收到停止信號的話先送一個，讓它自己存完檔"""
    p = _proc
    try:
        if p is not None and not getattr(p, "_user_stop", False) and p.poll() is None:
            p._user_stop = True
            os.kill(p.pid, signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
    except Exception:
        pass
    _say("  已結束 WebUI；正在進行的錄音或朗讀會在背景自己存完檔")
    os._exit(0)


def _sigint_handler(sig, frame):
    """uvicorn 啟動前、結束後（它會把收到的 Ctrl+C 轉交給這裡）用的 Ctrl+C 處理"""
    if _STOPPING[0]:
        _force_exit_now()
    _STOPPING[0] = True
    _say("\n  正在停止...（再按一次 Ctrl+C 立刻結束）")
    _stop_proc()
    _say("  WebUI 已停止")
    os._exit(0)


class _WebUIServer(uvicorn.Server):
    """uvicorn 執行中收到的 Ctrl+C：第一次照 uvicorn 正常結束（停掉子程式、等存檔），但要先說一聲；第二次立刻結束"""

    def handle_exit(self, sig, frame):
        if sig == signal.SIGINT and (self.should_exit or _STOPPING[0]):
            _force_exit_now()
        if not self.should_exit:
            _say("\n  正在停止 WebUI...（再按一次 Ctrl+C 立刻結束）")
        super().handle_exit(sig, frame)


def _uvicorn_config(**kw):
    """結束時等還沒回完的請求最多 3 秒（預設會一直等，而且 warning 等級什麼都不印，看起來就像當掉）；舊版 uvicorn 沒有這個參數"""
    import inspect
    if "timeout_graceful_shutdown" in inspect.signature(uvicorn.Config.__init__).parameters:
        kw["timeout_graceful_shutdown"] = 3
    return uvicorn.Config(**kw)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _pump_stderr(p):
    """子程式的錯誤輸出照樣寫到 WebUI 自己的錯誤輸出（終端機／systemd 日誌），同時留下最後幾行，
    結束時隨 disconnected 事件送到畫面。以前畫面只寫「請檢查終端機訊息」，伺服器版根本沒有終端機，
    日誌裡的中文還被 journalctl 顯示成 <E9><8C><AF>…（2026-10-08 使用者回報：PVE LXC 裡開即時模式失敗，「沒看到有錯誤噴出」）。
    一定要讀到 EOF：不讀的話管線塞滿，子程式寫錯誤輸出時會卡住"""
    out = getattr(sys.stderr, "buffer", None)
    try:
        for raw in iter(p.stderr.readline, b""):
            try:
                if out is not None:
                    out.write(raw); out.flush()
                else:
                    sys.stderr.write(raw.decode("utf-8", "replace")); sys.stderr.flush()
            except Exception:
                pass
            line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
            if line:
                p._err_tail.append(line)
    except Exception:
        pass


def _error_detail(lines):
    """畫面上要顯示的錯誤原因：有「[錯誤]」就從第一個「[錯誤]」開始，否則取最後幾行（例如 Traceback 的最後一行）"""
    lines = [l for l in lines if l]
    for i, l in enumerate(lines):
        if l.startswith("[錯誤]"):
            return "\n".join(lines[i:i + 8])
    return "\n".join(lines[-6:])


def _start_proc(args: list):
    global _proc
    _stop_proc()
    _set_pause_flag(False)          # 每次開始都不是暫停（上一次停在暫停中也一樣）
    global _finishing_at
    _finishing_at = 0.0
    with _proc_lock:
        cmd = [sys.executable, str(TRANSLATE_SCRIPT), "--webui"] + args
        # stdin 持續送 'y\n' 自動確認所有互動提問（確認開始、錄音等）
        # Windows 必須用 CREATE_NEW_PROCESS_GROUP 把子程序隔離成獨立 console group，
        # 否則 CTRL_BREAK_EVENT 會廣播給 webui.py 自己 + PowerShell 一起炸；
        # POSIX 用 start_new_session 脫離 controlling terminal（避免 SIGINT 廣播）。
        _popen_kw = {"cwd": str(BASE_DIR), "stdin": subprocess.PIPE}
        if sys.platform == "win32":
            _popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            _popen_kw["start_new_session"] = True
        _popen_kw["stderr"] = subprocess.PIPE
        _proc = subprocess.Popen(cmd, **_popen_kw)
        _proc._start_time = time.monotonic()
        _proc._err_tail = collections.deque(maxlen=40)
        _proc._err_pump = threading.Thread(target=_pump_stderr, args=(_proc,), daemon=True)
        _proc._err_pump.start()
        # 背景持續送 y 回答所有 input() 提問（確認開始、錄音、場景等）
        def _auto_yes():
            try:
                for _ in range(30):
                    if _proc.poll() is not None:
                        break
                    _proc.stdin.write(b"y\n")
                    _proc.stdin.flush()
                    time.sleep(0.3)
            except Exception:
                pass
        threading.Thread(target=_auto_yes, daemon=True).start()
        # 監控子程序結束，推送斷線事件到瀏覽器
        def _monitor():
            p = _proc  # 保留本地參照，避免 _stop_proc 將 _proc 設為 None
            if p is None:
                return
            start_t = getattr(p, '_start_time', time.monotonic())
            try:
                p.wait()
                rc = p.returncode
            except Exception:
                rc = -1
            elapsed = time.monotonic() - start_t
            user_stop = getattr(p, "_user_stop", False)
            pump = getattr(p, "_err_pump", None)
            if pump:
                pump.join(timeout=2)            # 讓最後幾行錯誤輸出收齊
            detail = _error_detail(getattr(p, "_err_tail", ())) if rc != 0 and not user_stop else ""
            if rc != 0 and elapsed < 5 and not user_stop:
                msg = f"啟動失敗（錯誤碼 {rc}）"
            elif rc != 0:
                msg = f"程式異常結束（錯誤碼 {rc}）"
            else:
                msg = "已停止" if user_stop else "處理已完成"
            if rc != 0 and not user_stop and not detail:
                detail = "詳細訊息在 WebUI 的執行紀錄（伺服器版：journalctl -u jt-live-whisper-webui）"
            print(f"\n  主程式已結束（exit code {rc}），WebUI 等待下一次操作（瀏覽器中按「回到設定」重新開始）")
            print(f"  按 Ctrl+C 可結束 WebUI 伺服器")
            if _event_queue:
                try:
                    _event_queue.put_nowait(json.dumps({"type": "disconnected", "pid": p.pid,
                        "message": msg, "rc": rc, "user_stop": user_stop, "detail": detail}))
                except Exception:
                    pass
        threading.Thread(target=_monitor, daemon=True).start()
    return _proc.pid


# Qwen3-ASR（實驗）：GPU 伺服器上就緒、或本機跑得了，才列進模型選單。背景查、快取 60 秒，
# 不可以在 /api/config 裡同步去打伺服器（伺服器連不上時頁面會卡住幾秒，v2.16.1 才處理過冷啟動）；
# 本機偵測要載入 torch／transformers（數秒），同樣放背景
_QWEN_PROBE = {"server": False, "local": None, "t": -1e9, "running": False}


def _qwen_status():
    """回傳 {"server": GPU 伺服器就緒, "local": 本機裝置 "mlx"／"cuda"／"cpu" 或 None}（上一次背景查到的）"""
    if time.monotonic() - _QWEN_PROBE["t"] > 60 and not _QWEN_PROBE["running"]:
        _QWEN_PROBE["running"] = True

        def _probe():
            server, local = False, None
            try:
                rw = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("remote_whisper") \
                    if CONFIG_FILE.exists() else None
                server = bool(rw) and _tm_qwen_server_ready(rw)[0]
            except Exception:
                server = False
            try:
                local = _tm_qwen_local_backend()[1]
            except Exception:
                local = None
            _QWEN_PROBE.update(server=server, local=local, t=time.monotonic(), running=False)
        threading.Thread(target=_probe, daemon=True).start()
    return {"server": _QWEN_PROBE["server"], "local": _QWEN_PROBE["local"]}


# ── 音訊裝置清單（v2.26.3）────────────────────────────────────
# sounddevice（PortAudio）的裝置清單在初始化時就固定了：WebUI 開著的時候才接上的 AirPods 看不到，
# 執行中「切換裝置」的清單也一樣；而按下開始／切換時另起的 translate_meeting 會重新列舉，
# 兩邊的編號可能對不上。所以每次列裝置前重新初始化（WebUI 本身不開音訊串流，重來不影響錄音）。
# kind：system＝系統音訊來源（ScreenCaptureKit／monitor 代號、BlackHole、loopback）、aggregate＝聚集裝置、
# mic＝麥克風。麥克風下拉只列 mic：選到系統音訊來源時會把對方的聲音錄兩次、自己的一句都沒有
_DEV_LOCK = threading.Lock()


def _device_kind(name):
    nl = name.lower()
    if "blackhole" in nl or "loopback" in nl or (sys.platform.startswith("linux") and "monitor" in nl):
        return "system"
    if "aggregate" in nl or "聚集" in name:
        return "aggregate"
    return "mic"


def _audio_devices():
    devices = []
    auto_loopback = ""
    auto_mic = ""
    with _DEV_LOCK:
        try:
            import sounddevice as _sd
            _sd._terminate()
            _sd._initialize()
        except Exception:
            pass
        # macOS ScreenCaptureKit：零設定擷取系統音訊，優先作為預設來源
        sck = {"supported": False, "permission": False, "macos": "", "app": ""}
        if sys.platform == "darwin" and _tm_sck_check and _tm_sck_macos_ok:
            try:
                if _tm_sck_macos_ok():
                    _info = _tm_sck_check(build=False) or {}
                    sck = {"supported": bool(_info.get("available")),
                           "permission": bool(_info.get("permission")),
                           "macos": _info.get("macos", ""),
                           # 授權對象是啟動 webui.py 的終端機程式，讓前端能直接指名
                           "app": _tm_sck_terminal_app_name() if _tm_sck_terminal_app_name else ""}
            except Exception:
                pass
        if sck["supported"] and sck["permission"]:
            devices.append({"id": _TM_SCK_LOOPBACK_ID, "kind": "system",
                            "name": "ScreenCaptureKit 系統音訊（免安裝 BlackHole）",
                            "channels": 2, "sr": 48000})
            auto_loopback = f"[{_TM_SCK_LOOPBACK_ID}] ScreenCaptureKit 系統音訊"
        # Linux PipeWire / PulseAudio：預設喇叭的 monitor 來源
        if sys.platform.startswith("linux") and _tm_pulse_available:
            try:
                if _tm_pulse_available():
                    _pl = _tm_pulse_label()
                    devices.append({"id": _TM_PULSE_LOOPBACK_ID, "kind": "system", "name": _pl,
                                    "channels": 2, "sr": 48000})
                    auto_loopback = f"[{_TM_PULSE_LOOPBACK_ID}] {_pl}"
            except Exception:
                pass
        # Windows：WASAPI Loopback（系統播放的聲音）。以前清單裡沒有它：系統音訊下拉只列得出麥克風、
        # 「自動偵測 →」的提示是空的（v2.26.3）
        if sys.platform == "win32" and _tm_find_wasapi_loopback:
            try:
                _wb = _tm_find_wasapi_loopback()
                if _wb:
                    _wn = f"WASAPI Loopback（{_wb['name']}）"
                    devices.append({"id": _TM_WASAPI_LOOPBACK_ID, "kind": "system", "name": _wn,
                                    "channels": 2, "sr": 48000})
                    auto_loopback = f"[{_TM_WASAPI_LOOPBACK_ID}] {_wn}"
            except Exception:
                pass
        try:
            import sounddevice as sd
            for i, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    name = dev["name"]
                    devices.append({"id": i, "name": name, "kind": _device_kind(name),
                                    "channels": dev["max_input_channels"],
                                    "sr": int(dev["default_samplerate"])})
                    # 自動偵測 loopback
                    nl = name.lower()
                    if not auto_loopback and ("blackhole" in nl or "loopback" in nl
                                              or (sys.platform.startswith("linux") and "monitor" in nl)):
                        auto_loopback = f"[{i}] {name}"
            # 自動偵測麥克風（系統預設輸入，排除 loopback/aggregate）
            default_in = sd.default.device[0]
            if default_in is not None and default_in >= 0:
                dinfo = sd.query_devices(default_in)
                dn = dinfo["name"].lower()
                if (dinfo["max_input_channels"] > 0
                        and "blackhole" not in dn and "loopback" not in dn
                        and "monitor" not in dn
                        and "aggregate" not in dn and "聚集" not in dinfo["name"]):
                    auto_mic = f"[{default_in}] {dinfo['name']}"
        except Exception:
            pass
    return {"devices": devices, "auto_loopback": auto_loopback, "auto_mic": auto_mic, "sck": sck}


def _get_config():
    """讀取可用選項（從 translate_meeting.py 的常數 + config.json）"""
    modes = [
        {"value": "en2zh", "label": "英翻中字幕", "group": "單向翻譯"},
        {"value": "zh2en", "label": "中翻英字幕", "group": "單向翻譯"},
        {"value": "ja2zh", "label": "日翻中字幕", "group": "單向翻譯"},
        {"value": "zh2ja", "label": "中翻日字幕", "group": "單向翻譯"},
        {"value": "ko2zh", "label": "韓翻中字幕", "group": "單向翻譯"},
        {"value": "zh2ko", "label": "中翻韓字幕", "group": "單向翻譯"},
        {"value": "en_zh", "label": "英中雙向字幕", "group": "雙向翻譯"},
        {"value": "ja_zh", "label": "日中雙向字幕", "group": "雙向翻譯"},
        {"value": "ko_zh", "label": "韓中雙向字幕", "group": "雙向翻譯"},
        {"value": "en", "label": "英文轉錄", "group": "轉錄"},
        {"value": "zh", "label": "中文轉錄", "group": "轉錄"},
        {"value": "ja", "label": "日文轉錄", "group": "轉錄"},
        {"value": "ko", "label": "韓文轉錄", "group": "轉錄"},
        {"value": "nan", "label": "台語轉錄", "group": "轉錄"},
        {"value": "nan2en", "label": "台翻英字幕", "group": "單向翻譯"},
        {"value": "record", "label": "純錄音", "group": "其他"},
    ]
    scenes = [
        {"value": "meeting", "label": "線上會議（5秒）"},
        {"value": "training", "label": "教育訓練（8秒）"},
        {"value": "presentation", "label": "演講簡報（12秒）"},
        {"value": "subtitle", "label": "快速字幕（3秒）"},
    ]
    try:
        if _TM_WHISPER_MODELS is None:
            raise ImportError("translate_meeting not loaded")
        models = [{"value": n, "label": f"{n}（{d}）"} for n, _, d in _TM_WHISPER_MODELS]
        # Breeze-ASR-26：台語專用，華語模式也可選用（台灣華語夾雜台語時），固定本機辨識
        models.append({"value": "breeze-asr-26", "label": "breeze-asr-26（台灣華語／台語，較慢，固定本機）"})
        qs = _qwen_status()
        if qs["server"] or qs["local"]:
            # 使用限制由後端給，前端照 limits 停用（不在前端寫死模型名稱）：
            # server／local＝選 GPU 伺服器／本機時能不能用；local_slow＝本機只有 CPU（D4：可選但要提示很慢）
            models.append({"value": _TM_QWEN_MODEL,
                           "label": f"{_TM_QWEN_MODEL}（實驗：中文會議、中英夾雜明顯更準）",
                           "limits": {"file_only": True, "server": bool(qs["server"]), "local": bool(qs["local"]),
                                      "local_slow": qs["local"] == "cpu",
                                      "modes": list(_TM_ZH_MODES + _TM_EN_MODES + _TM_KO_MODES)}})
    except Exception:
        models = [
            {"value": "base.en", "label": "base.en（最快，準確度一般）"},
            {"value": "small.en", "label": "small.en（快，準確度好）"},
            {"value": "small", "label": "small（快，多語言）"},
            {"value": "large-v3-turbo", "label": "large-v3-turbo（快，準確度很好）"},
            {"value": "medium.en", "label": "medium.en（較慢，準確度很好）"},
            {"value": "medium", "label": "medium（較慢，多語言）"},
            {"value": "large-v3", "label": "large-v3（最慢，中日文品質最好，有獨立 GPU 可選用）"},
        ]
    engines = [
        {"value": "llm", "label": "LLM — 品質最好，需 LLM 伺服器"},
        {"value": "nllb", "label": "NLLB — 本機離線，中日韓英互譯"},
        {"value": "argos", "label": "Argos — 本機離線，僅英翻中"},
    ]
    # LLM 翻譯模型清單
    llm_models = [{"value": n, "label": f"{n} — {d}"} for n, d in _TM_TRANSLATE_MODELS]
    # 讀 config.json 的預設 LLM 設定 + 使用者自訂模型
    llm_host = ""
    llm_model = _TM_DEFAULT_TRANSLATE_MODEL
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            llm_host = cfg.get("llm_host", "") or cfg.get("ollama_host", "")
            if llm_host:
                port = cfg.get("llm_port", 11434) or cfg.get("ollama_port", 11434)
                llm_host = f"{llm_host}:{port}"
            llm_model = cfg.get("last_llm_model", "") or cfg.get("ollama_model", llm_model)
            # 使用者自訂翻譯模型
            for um in cfg.get("translate_models", []):
                name = um if isinstance(um, str) else um.get("name", "")
                if name and not any(m["value"] == name for m in llm_models):
                    llm_models.append({"value": name, "label": name})
        except Exception:
            pass
    llm_host_auto = False
    if not llm_host:
        llm_host = _probe_local_llm()
        llm_host_auto = bool(llm_host)
    # 前次使用的設定（webui 自己存的）
    last = {}
    if CONFIG_FILE.exists():
        try:
            cfg2 = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            last = cfg2.get("webui_last", {})
        except Exception:
            pass
    # 音訊裝置（每次都重新讀：會議中才接上的 AirPods 之類要看得到，v2.26.3）
    _dev = _audio_devices()
    devices, auto_loopback, auto_mic, sck = _dev["devices"], _dev["auto_loopback"], _dev["auto_mic"], _dev["sck"]
    # GPU 伺服器資訊
    has_gpu_server = bool(llm_host)  # 簡化判斷：有設 LLM host 通常也有 GPU server
    gpu_host = ""
    if CONFIG_FILE.exists():
        try:
            cfg2 = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            rw = cfg2.get("remote_whisper", {})
            gpu_host = rw.get("host", "")
        except Exception:
            pass
    # 推薦模型（根據裝置 + 模式自動偵測）
    recommended_models = {}
    try:
        if _tm_recommended_whisper_model is not None:
            for m_info in modes:
                recommended_models[m_info["value"]] = _tm_recommended_whisper_model(m_info["value"])
    except Exception:
        pass
    # 摘要模型說明（從 translate_meeting.py 的 SUMMARY_MODELS）
    summary_descs = {}
    try:
        if _TM_SUMMARY_MODELS is not None:
            summary_descs = {n: d for n, d in _TM_SUMMARY_MODELS if d}
    except Exception:
        pass
    if not summary_descs:
        summary_descs = {"qwen3.8:27b": "推薦：摘要與校正實測最準，約 18 GB", "glm-4.7-flash:q8_0": "摘要速度最快、內容較精簡；校正未實測", "gpt-oss:120b": "約 65 GB；校正會讓英文逐字稿變差，不建議"}
    return {
        "modes": modes, "scenes": scenes, "models": models, "engines": engines,
        "llm_models": llm_models, "llm_host": llm_host, "llm_model": llm_model,
        "default_llm_model": _TM_DEFAULT_TRANSLATE_MODEL,
        "default_summary_model": _TM_SUMMARY_DEFAULT_MODEL,
        "llm_host_auto": llm_host_auto,
        "devices": devices, "auto_loopback": auto_loopback, "auto_mic": auto_mic,
        "gpu_host": gpu_host, "summary_descs": summary_descs,
        "recommended_models": recommended_models,
        "default_engine": "llm" if llm_host else "nllb",
        "sck": sck, "is_macos": sys.platform == "darwin",
        "is_linux": sys.platform.startswith("linux"),
        "last": last, "version": "2.29.0",
        # 網頁需要的後端功能等級：只換了檔案、WebUI 沒重開時，新網頁會連到舊後端（2026-10-09 Mac 實際發生：
        # 「無法取得文字轉語音狀態」）。網頁發現等級不夠就請使用者重新啟動 WebUI，不會亂報錯
        "api_level": 2,
        "tts": _tts_info(admin=False),
        "has_read_pw": bool(_webui_passwords["read"]),
        "has_admin_pw": bool(_webui_passwords["admin"]),
    }


# ─── 路由 ────────────────────────────────────────────────────
# 分頁圖示（v2.27.0 以前沒有：分頁顯示空白圖示、瀏覽器自動要的 /favicon.ico 一律 404）。
# 公開的 logo，不需要密碼（來源 IP 限制照樣適用）；icons/ 第一次升級可能還沒到，沒有就 404
@app.get("/favicon.ico")
@app.get("/favicon.png")
async def favicon(request: Request):
    from fastapi.responses import FileResponse
    ext = "ico" if request.url.path.endswith(".ico") else "png"
    p = BASE_DIR / "icons" / f"jt-live-whisper.{ext}"
    if not p.is_file():
        return JSONResponse({"ok": False, "error": "找不到圖示"}, status_code=404)
    return FileResponse(str(p), media_type="image/x-icon" if ext == "ico" else "image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "webui.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>webui.html not found</h1>", status_code=404)


@app.get("/api/devices")
async def api_devices(request: Request):
    """重新偵測音訊裝置（設定頁的「重新偵測」、執行中的「切換裝置」清單，v2.26.3）"""
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
    return await asyncio.to_thread(_audio_devices)


@app.get("/api/config")
async def api_config(request: Request):
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"auth_required": True, "error": err, "is_local": _is_local(request)}, status_code=401)
    cfg = await asyncio.to_thread(_get_config)      # 列裝置要重新初始化音訊（v2.26.3），不擋住其他請求
    cfg["is_local"] = _is_local(request)
    return JSONResponse(cfg)


@app.post("/api/auth")
async def api_auth(request: Request, body: dict = {}):
    """驗證密碼，回傳角色（admin/read/denied）"""
    token = body.get("password", "")
    if _is_local(request):
        return {"role": "admin", "is_local": True}
    if _pw_match(token, _webui_passwords["admin"]):
        return {"role": "admin"}
    if not _webui_passwords["read"] or _pw_match(token, _webui_passwords["read"]):
        return {"role": "read"}
    return JSONResponse({"role": "denied", "error": "密碼錯誤"}, status_code=401)


@app.get("/api/passwords")
async def api_get_passwords(request: Request):
    """取得密碼（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機"}, status_code=403)
    # **不回傳密碼本身**（現在存的是雜湊，回傳雜湊更糟——前端會把它當成密碼存回去）。
    # 只說有沒有設定，畫面用 placeholder 呈現。
    return {"read_set": bool(_webui_passwords["read"]),
            "admin_set": bool(_webui_passwords["admin"])}


@app.post("/api/save-passwords")
async def api_save_passwords(request: Request, body: dict = {}):
    """儲存安全設定密碼（僅本機可用）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機設定"}, status_code=403)
    # **沒帶那個欄位＝不更動；帶空字串＝清除。**
    # 不能用「留空＝不更動」：畫面上密碼欄一定是空的（我們不回傳密碼），
    # 那樣就分不出「只想改其中一個」與「想清掉另一個」——
    # 使用者只改唯讀密碼時會把管理密碼一起清掉，而且不會發現。
    for role in ("read", "admin"):
        if role in body:
            _webui_passwords[role] = _pw_hash(body.get(role, ""))
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        # 只寫雜湊，並把舊版留下的明文欄位一起清掉
        cfg["webui_passwords"] = {"read_sha256": _webui_passwords["read"],
                                  "admin_sha256": _webui_passwords["admin"]}
        _write_config(cfg)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.get("/api/keyword-config")
async def api_keyword_config(request: Request):
    """取得關鍵字通知設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False}, status_code=403)
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("keyword_alert", {})
        except Exception:
            pass
    return JSONResponse(cfg)


@app.post("/api/save-keyword")
async def api_save_keyword(request: Request):
    """儲存關鍵字通知設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    body = await request.json()
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        cfg["keyword_alert"] = body
        _write_config(cfg)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.get("/api/overlay-config")
async def api_overlay_config(request: Request):
    """取得懸浮字幕設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False}, status_code=403)
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("subtitle_overlay", {})
        except Exception:
            pass
    return JSONResponse(cfg)


@app.post("/api/save-overlay")
async def api_save_overlay(request: Request):
    """儲存懸浮字幕設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    body = await request.json()
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        cfg["subtitle_overlay"] = body
        _write_config(cfg)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.get("/api/fonts")
async def api_fonts(request: Request):
    """列出系統中支援中文的字型（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False}, status_code=403)
    # Linux：Qt 經 fontconfig 會替缺字自動補字型，inFont('中') 幾乎全部回 True，
    # 改問 fontconfig 哪些字型真的涵蓋中文
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(["fc-list", ":lang=zh", "family"],
                               capture_output=True, text=True, timeout=10)
            fonts = sorted({ln.split(",")[0].strip() for ln in r.stdout.splitlines() if ln.strip()})
            if fonts:
                return JSONResponse(fonts[:80])
        except Exception:
            pass
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from PyQt6.QtWidgets import QApplication; from PyQt6.QtGui import QFontDatabase, QFont, QFontMetrics; "
             "import sys; app = QApplication(sys.argv); "
             "fonts = []; "
             "[fonts.append(f) for f in sorted(QFontDatabase.families()) "
             " if QFontMetrics(QFont(f)).inFont('中')]; "
             "print('\\n'.join(fonts[:80])); app.quit()"],
            capture_output=True, text=True, timeout=10
        )
        fonts = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        fonts = []
    return JSONResponse(fonts)


@app.post("/api/reopen-overlay")
async def api_reopen_overlay(request: Request):
    """重新啟動懸浮字幕子程序（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    try:
        overlay_script = str(BASE_DIR / "subtitle_overlay.py")
        config_path = str(CONFIG_FILE)
        if not Path(overlay_script).is_file():
            return JSONResponse({"ok": False, "error": "找不到 subtitle_overlay.py"})
        proc = subprocess.Popen(
            [sys.executable, overlay_script, "--config", config_path],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/forward-config")
async def api_forward_config(request: Request):
    """取得字幕轉發設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False}, status_code=403)
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("subtitle_forward", {})
        except Exception:
            pass
    return JSONResponse(cfg)


@app.post("/api/save-forward")
async def api_save_forward(request: Request):
    """儲存字幕轉發設定（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    body = await request.json()
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        cfg["subtitle_forward"] = body
        _write_config(cfg)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


def _urlopen_safe(req, timeout=10):
    """urlopen with SSL fallback"""
    import ssl as _ssl
    import urllib.request as _ur2
    try:
        return _ur2.urlopen(req, timeout=timeout)
    except Exception as e:
        if "SSL" in str(e) or "CERTIFICATE" in str(e).upper():
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            return _ur2.urlopen(req, timeout=timeout, context=ctx)
        raise

@app.post("/api/test-forward")
async def api_test_forward(request: Request):
    """測試字幕轉發（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    import urllib.request as _ur
    body = await request.json()
    platform = body.get("platform", "")
    cfg = body.get("config", {})
    test_text = "🔔 jt-live-whisper 字幕轉發測試\nThis is a test message."
    try:
        if platform == "telegram":
            url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
            data = json.dumps({"chat_id": cfg["chat_id"], "text": test_text}).encode()
            req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"})
            _urlopen_safe(req)
        elif platform in ("slack", "teams"):
            data = json.dumps({"text": test_text}).encode()
            req = _ur.Request(cfg["webhook_url"], data=data, headers={"Content-Type": "application/json"})
            _urlopen_safe(req)
        elif platform == "discord":
            data = json.dumps({"content": test_text}).encode()
            req = _ur.Request(cfg["webhook_url"], data=data, headers={"Content-Type": "application/json"})
            _urlopen_safe(req)
        elif platform == "line":
            url = "https://api.line.me/v2/bot/message/push"
            payload = {"to": cfg["target_id"], "messages": [{"type": "text", "text": test_text}]}
            data = json.dumps(payload).encode()
            req = _ur.Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['channel_access_token']}"
            })
            _urlopen_safe(req)
        elif platform == "nctalk":
            import base64 as _b64
            base = cfg["url"].rstrip("/")
            url = f"{base}/ocs/v2.php/apps/spreed/api/v1/chat/{cfg['room_token']}"
            data = json.dumps({"message": test_text}).encode()
            cred = _b64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
            req = _ur.Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {cred}",
                "OCS-APIRequest": "true"
            })
            _urlopen_safe(req)
        elif platform == "custom":
            body_tpl = cfg.get("body_template", "")
            if body_tpl and "{{text}}" in body_tpl:
                escaped = json.dumps(test_text)[1:-1]
                body = body_tpl.replace("{{text}}", escaped).encode("utf-8")
                headers = {"Content-Type": "application/json; charset=utf-8"}
            else:
                body = test_text.encode("utf-8")
                headers = {"Content-Type": "text/plain; charset=utf-8"}
            headers.update(cfg.get("headers", {}))
            req = _ur.Request(cfg["url"], data=body, headers=headers, method="POST")
            _urlopen_safe(req)
        else:
            return JSONResponse({"ok": False, "error": f"未知平台: {platform}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.post("/api/open-folder")
async def api_open_folder(request: Request):
    """開啟指定資料夾（僅限本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機操作"}, status_code=403)
    body = await request.json()
    folder = body.get("path", "")
    if not folder:
        return JSONResponse({"ok": False, "error": "未指定路徑"})
    full = (BASE_DIR / folder).resolve()
    # 安全檢查：必須在專案目錄下
    if not str(full).startswith(str(BASE_DIR.resolve())):
        return JSONResponse({"ok": False, "error": "路徑不合法"})
    if not full.is_dir():
        return JSONResponse({"ok": False, "error": "資料夾不存在"})
    import platform
    if platform.system() == "Darwin":
        subprocess.Popen(["open", str(full)])
    elif platform.system() == "Windows":
        subprocess.Popen(["explorer", str(full)])
    else:
        # Linux：沒有圖形桌面時 xdg-open 無從開啟；有桌面時要脫離 session，
        # 避免檔案管理員掛在 webui.py 底下、並繼承 stdio
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return JSONResponse({"ok": False, "error": f"此主機沒有圖形桌面，請直接前往：{full}"})
        try:
            subprocess.Popen(["xdg-open", str(full)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError:
            return JSONResponse({"ok": False, "error": f"找不到 xdg-open，請直接前往：{full}"})
    return {"ok": True}


@app.get("/api/files")
async def api_files(request: Request):
    """列出 recordings/ 目錄下的音訊/影片檔案；kind=text 時列 recordings/ 與 logs/ 的文字檔（朗讀用）"""
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    rec_dir = BASE_DIR / "recordings"
    if request.query_params.get("kind") == "text":
        files = []
        for d in (rec_dir, BASE_DIR / "logs"):
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file() and f.suffix.lower() in _TEXT_EXTS:
                        st = f.stat()
                        files.append({"name": f.name, "dir": d.name, "size": round(st.st_size / 1024, 1),
                                      "path": f"{d.name}/{f.name}", "mtime": st.st_mtime})
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return JSONResponse({"files": files[:300]})
    files = []
    if rec_dir.is_dir():
        exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".avi"}
        for f in sorted(rec_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in exts:
                st = f.stat()
                size_mb = round(st.st_size / 1048576, 1)
                files.append({"name": f.name, "size": size_mb, "path": str(f)})
    return JSONResponse({"files": files, "dir": str(rec_dir)})


from fastapi import UploadFile, File as FastFile


# 上傳：必須 admin（上傳就是為了接著處理，而開始處理本來就要 admin）。
# v2.22.2 前這個端點**完全沒有授權**，且直接用用戶端送來的檔名組路徑：
# 檔名是 "../../translate_meeting.py" 或絕對路徑時會寫到 recordings/ 外面（任意檔案覆寫 → 可執行任意程式碼）；
# 也沒有大小上限（整檔讀進記憶體）。盤點測試當時沒抓到，是因為它往下 30 行掃到了下一個端點的 _check_auth。
_TEXT_EXTS = {".txt", ".md", ".srt", ".vtt"}         # 朗讀用的文字檔（v2.27.0）
_UPLOAD_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".avi"} | _TEXT_EXTS
_UPLOAD_MAX_MB = int(os.environ.get("JTLW_WEBUI_MAX_UPLOAD_MB", "4096"))


def _safe_upload_name(name):
    """只取檔名本身（去掉任何目錄成分，含 Windows 的反斜線），副檔名必須是音訊／影片或朗讀用的文字檔。
    不合格回傳 None。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    if not base or base in (".", "..") or base.startswith("."):
        return None
    if os.path.splitext(base)[1].lower() not in _UPLOAD_EXTS:
        return None
    return base


@app.post("/api/upload-file")
async def api_upload_file(request: Request, file: UploadFile = FastFile(...)):
    """上傳音訊／影片檔案（離線處理）或文字檔（朗讀，v2.27.0）到 recordings/"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    name = _safe_upload_name(file.filename)
    if not name:
        return JSONResponse({"ok": False, "error": "檔名或副檔名不允許（只接受音訊／影片檔，或朗讀用的 .txt／.md／.srt／.vtt）"},
                            status_code=400)
    rec_dir = (BASE_DIR / "recordings").resolve()
    rec_dir.mkdir(exist_ok=True)
    dest = rec_dir / name
    # 避免覆蓋
    if dest.exists():
        stem, ext = dest.stem, dest.suffix
        i = 1
        while dest.exists():
            dest = rec_dir / f"{stem}_{i}{ext}"
            i += 1
    if dest.resolve().parent != rec_dir:       # 雙重保險：最後的路徑一定在 recordings/ 裡
        return JSONResponse({"ok": False, "error": "路徑不允許"}, status_code=400)
    limit, size = _UPLOAD_MAX_MB * 1048576, 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1048576)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"檔案超過上限 {_UPLOAD_MAX_MB} MB")
                f.write(chunk)
    except ValueError as e:
        dest.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
    return JSONResponse({"ok": True, "name": dest.name, "size": round(size / 1048576, 1),
                         "path": str(dest), "rel": f"recordings/{dest.name}"})


@app.post("/api/sck-permission")
async def api_sck_permission(request: Request):
    """macOS：觸發「螢幕錄製」權限授權對話框（ScreenCaptureKit 擷取系統音訊用）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if sys.platform != "darwin" or not _tm_sck_request_permission:
        return JSONResponse({"ok": False, "error": "僅適用於 macOS"})
    granted = await asyncio.to_thread(_tm_sck_request_permission)
    if granted:
        return JSONResponse({"ok": True, "permission": True})
    return JSONResponse({
        "ok": False, "permission": False,
        "error": "尚未授權。請到「系統設定 → 隱私權與安全性 → 螢幕錄製」勾選終端機程式，"
                 "授權後重新啟動終端機與 WebUI。",
    })


@app.post("/api/test-llm")
async def api_test_llm(request: Request, body: dict = {}):
    """測試 LLM 伺服器連線（需管理密碼）。

    **這支會讓伺服器去連使用者指定的任意位址**，沒有授權的話等於把這台機器
    變成探測內網的工具（回應與逾時的差別就能判斷某個主機/埠開不開）。
    它本來就只有設定畫面在用，而設定畫面本來就需要授權。
    """
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    host = body.get("host", "").strip()
    if not host:
        return JSONResponse({"ok": False, "error": "未填入主機位址"})
    if _tm_parse_llm_host:
        # 格式不對時講清楚哪裡不對（以前 http:// 開頭或連接埠超出範圍都只回籠統的「無法連線」）
        _h, _p, _perr = _tm_parse_llm_host(host)
        if _perr:
            return JSONResponse({"ok": False, "error": _perr})
        host = f"{_h}:{_p}"
    import urllib.request
    import urllib.error
    # 嘗試 Ollama /api/tags 和 OpenAI /v1/models
    # 注意：必須驗證回傳結構，不能只看 HTTP 200。LM Studio 對未實作的
    # endpoint 一律回 200，若只看狀態碼會把 LM Studio 誤判成 Ollama。
    for path in ["/api/tags", "/v1/models"]:
        url = f"http://{host}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if not isinstance(data, dict):
                    continue
                if "/api/" in path:
                    # Ollama format：須有 models 陣列
                    if not isinstance(data.get("models"), list):
                        continue
                    models = [m.get("name", "") for m in data["models"] if m.get("name")]
                    server_type = "ollama"
                else:
                    # OpenAI format：須有 data 陣列
                    if not isinstance(data.get("data"), list):
                        continue
                    models = [m.get("id", "") for m in data["data"] if m.get("id")]
                    server_type = "openai"
                return JSONResponse({"ok": True, "server_type": server_type,
                                     "models": models[:20], "url": url})
        except Exception:
            continue
    return JSONResponse({"ok": False, "error": f"無法連線 {host}（已嘗試 Ollama 和 OpenAI 相容 API）"})


def _build_args(body: dict) -> list:
    """從 start body 組裝 translate_meeting.py CLI 參數"""
    args = []
    input_files = body.get("input_files", [])
    if input_files:
        for f in input_files:
            args.extend(["--input", f])
    mode = body.get("mode", "en2zh")
    args.extend(["--mode", mode])
    model = body.get("model", "large-v3-turbo")
    args.extend(["-m", model])
    scene = body.get("scene", "training")
    args.extend(["-s", scene])
    engine = body.get("engine")
    llm_host = (body.get("llm_host") or "").strip()
    if engine and mode not in ("en", "zh", "ja", "ko", "nan"):
        args.extend(["-e", engine])
        if engine == "llm":
            llm_model = body.get("llm_model", "")
            if llm_model:
                args.extend(["--llm-model", llm_model])
    # LLM 主機不只翻譯用，逐字稿校正與 AI 摘要也要用：純轉錄模式、NLLB / Argos 翻譯時同樣要傳
    if llm_host and mode != "record":
        args.extend(["--llm-host", llm_host])
    topic = body.get("topic", "").strip()
    if topic:
        args.extend(["--topic", topic])
    if body.get("record"):
        args.append("--record")
    if body.get("mic"):
        args.append("--mic")
    if body.get("denoise"):
        args.append("--denoise")
    if body.get("diarize"):
        args.append("--diarize")
        num_spk = body.get("num_speakers")
        if num_spk and int(num_spk) > 0:
            args.extend(["--num-speakers", str(int(num_spk))])
    if body.get("summarize"):
        args.append("--summarize")
        sm = body.get("summary_model", "").strip()
        if sm:
            args.extend(["--summary-model", sm])
        sr = body.get("summary_rounds", 1)
        if sr and int(sr) > 1:
            args.extend(["--summary-rounds", str(int(sr))])
    if body.get("local_asr"):
        args.append("--local-asr")
    if body.get("no_srt"):
        args.append("--no-srt")
    if body.get("no_vtt"):
        args.append("--no-vtt")
    if body.get("subtitle_overlay"):
        args.append("--subtitle-overlay")
    # 純錄音：錄音來源（雙方／只錄系統音訊／只錄麥克風，v2.26.3）；用不到的那個裝置不送
    rec_source = body.get("rec_source") if mode == "record" else None
    if rec_source in ("both", "system", "mic"):
        args.extend(["--rec-source", rec_source])
    device = body.get("device")
    if device is not None and device != "" and rec_source != "mic":
        args.extend(["-d", str(device)])
    mic_device = body.get("mic_device")
    if mic_device is not None and mic_device != "" and rec_source != "system":
        args.extend(["--mic-device", str(mic_device)])
    # 雙向語音口譯（v2.28.0）：只有英中雙向即時模式；裝置與聲音由主程式檢查（找不到就說明並結束，不會默默不念）
    if mode == "en_zh" and not input_files:
        for k, flag in (("me", "--speak-me"), ("them", "--speak-them")):
            if body.get(f"interp_{k}"):
                args.extend([flag, str(body.get(f"interp_{k}_dev") or "default")])
                if body.get(f"interp_{k}_voice"):
                    args.extend([f"{flag}-voice", str(body[f"interp_{k}_voice"])])
        if body.get("interp_them") and body.get("interp_intro") is False:
            args.extend(["--interp-intro", "none"])
        if body.get("interp_them") and body.get("interp_passthrough"):
            args.append("--passthrough")
    return args


@app.post("/api/start")
async def api_start(request: Request, body: dict = {}):
    """啟動 translate_meeting.py"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=403)
    if (body.get("llm_host") or "").strip() and _tm_parse_llm_host:
        _h, _p, _perr = _tm_parse_llm_host(body["llm_host"])
        if _perr:
            return JSONResponse({"status": "error", "error": f"LLM 主機：{_perr}"}, status_code=400)
        body["llm_host"] = f"{_h}:{_p}"
    if body.get("source") in ("tts", "tts_file"):
        if _tts is None:
            return JSONResponse({"status": "error", "error": _TTS_MISSING}, status_code=503)
        try:
            args = _tts_args(body)
        except ValueError as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
        if body.get("tts_device") == "browser":
            try:
                TTS_POS_FLAG.unlink()
            except OSError:
                pass
    else:
        args = _build_args(body)
    pid = await asyncio.to_thread(_start_proc, args)
    # 儲存前次使用的設定到 config.json
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        prev_last = cfg.get("webui_last") or {}
        if body.get("source") in ("tts", "tts_file"):
            # 朗讀只更新朗讀的那幾項，原本辨識的設定（模式、模型、裝置…）照舊保留
            prev_last.update({"source": body["source"], "tts_voice": body.get("tts_voice") or "",
                              "tts_rate": body.get("tts_rate") or 1.0, "tts_pause": body.get("tts_pause") or "normal",
                              "tts_where": body.get("tts_where") or "auto", "tts_device": body.get("tts_device") or "",
                              "tts_save": body.get("tts_save") or "", "tts_steps": body.get("tts_steps") or 6,
                              "tts_model": body.get("tts_model") or ""})
            cfg["webui_last"] = prev_last
            if "subtitle_overlay" in body:
                so = cfg.get("subtitle_overlay", {})
                so["enabled"] = body["subtitle_overlay"]
                cfg["subtitle_overlay"] = so
            _write_config(cfg)
            return {"status": "started", "pid": pid, "args": args}
        cfg["webui_last"] = {**{k: v for k, v in prev_last.items() if k.startswith("tts_")},
            "source": body.get("source") or ("file" if body.get("input_files") else "live"),
            "mode": body.get("mode"), "model": body.get("model"),
            "scene": body.get("scene"), "engine": body.get("engine"),
            "llm_model": body.get("llm_model"), "llm_host": body.get("llm_host"),
            "local_asr": body.get("local_asr", False),
            "record": body.get("record", False), "mic": body.get("mic", False),
            "rec_source": body.get("rec_source") or "both",
            "denoise": body.get("denoise", True),
            "diarize": body.get("diarize", False),
            "num_speakers": body.get("num_speakers", 0),
            "summarize": body.get("summarize", False),
            "summary_model": body.get("summary_model", ""),
            "summary_rounds": body.get("summary_rounds", 1),
            "gen_srt": not body.get("no_srt", False),
            "gen_vtt": not body.get("no_vtt", False),
            **{k: body.get(k) for k in ("interp_me", "interp_me_dev", "interp_me_voice", "interp_them", "interp_them_dev",
                                        "interp_them_voice", "interp_intro", "interp_passthrough") if k in body},
        }
        # 同步字幕轉發、關鍵字通知、懸浮字幕的啟用狀態（避免不勾但沒按儲存，下次還是啟用）
        if "fwd_enabled" in body:
            sf = cfg.get("subtitle_forward", {})
            sf["enabled"] = body["fwd_enabled"]
            cfg["subtitle_forward"] = sf
        if "kw_enabled" in body:
            ka = cfg.get("keyword_alert", {})
            ka["enabled"] = body["kw_enabled"]
            cfg["keyword_alert"] = ka
        if "subtitle_overlay" in body:
            so = cfg.get("subtitle_overlay", {})
            so["enabled"] = body["subtitle_overlay"]
            cfg["subtitle_overlay"] = so
        _write_config(cfg)
    except Exception:
        pass
    return {"status": "started", "pid": pid, "args": args}


@app.post("/api/switch-device")
async def api_switch_device(request: Request, body: dict = {}):
    """切換音訊裝置（停止子程序 → 用新裝置重新啟動）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    start_body = body.get("start_body")
    device_id = body.get("device_id")
    device_type = body.get("device_type", "lb")  # "lb" or "mic"
    if not start_body or device_id is None:
        return JSONResponse({"ok": False, "error": "缺少參數"})
    # 更新裝置 ID
    if device_type == "mic":
        start_body["mic_device"] = device_id
    else:
        start_body["device"] = device_id
    # 廣播切換中事件
    await broadcast(json.dumps({"type": "switching", "message": "正在切換音訊裝置..."}))
    # 雙向語音口譯在 Linux 自動建立的虛擬麥克風：舊的程式不要移除、新的程式接手（會議軟體選的麥克風才一直有效）
    keep = bool(start_body.get("interp_them")) and str(start_body.get("interp_them_dev") or "") == "auto"
    if keep:
        try:
            INTERP_KEEP.write_text("switch", encoding="utf-8")
        except OSError:
            keep = False
    # 停止目前程序（純錄音要等舊的那段轉檔存好，放執行緒裡等，不卡住事件迴圈）
    try:
        await asyncio.to_thread(_stop_proc)
    finally:
        if keep:
            try:
                INTERP_KEEP.unlink()
            except OSError:
                pass
    await asyncio.sleep(0.5)
    # 用新設定重新啟動
    try:
        args = _build_args(start_body)
        pid = await asyncio.to_thread(_start_proc, args)
        return {"ok": True, "pid": pid, "device_id": device_id}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/stop")
async def api_stop(request: Request):
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=403)
    # 在 thread pool 跑避免阻塞 event loop（_stop_proc 最多耗 7 秒：4+2+1）
    await asyncio.to_thread(_stop_proc)
    # 廣播停止事件
    await broadcast(json.dumps({"type": "stopped"}))
    return {"status": "stopped"}


@app.get("/api/status")
async def api_status(request: Request):
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    p = _proc                       # 不拿 _proc_lock：停止中（等存檔）會持有它好幾分鐘，這裡在事件迴圈裡
    running = p is not None and p.poll() is None
    return {"running": running}


def _remote_access_warnings():
    """啟動時的安全提醒（v2.22.3）：既有部署不自動改設定，但要讓管理者知道現況。
    WebUI 綁 0.0.0.0，別台電腦連得進來；沒有唯讀密碼又沒有來源限制時，
    同網段任何人都能看畫面、列出錄音、讀逐字稿與摘要。"""
    out = []
    if not _webui_passwords["read"]:
        out.append("未設定唯讀密碼：" + ("允許清單內的電腦" if _allowed_nets else "任何連得到這台的電腦")
                   + "都能看畫面、讀逐字稿與摘要。請在本機開 WebUI → 安全設定，設一組唯讀密碼")
    if not _webui_passwords["admin"]:
        out.append("未設定管理密碼：遠端無法上傳或開始作業（本機不受影響）")
    return out


def _ws_level(ws):
    """WebSocket 的權限等級：'admin'／'read'／None（拒絕）。規則與 HTTP 的 _check_auth 相同"""
    if _is_local(ws):
        return "admin"
    token = ws.query_params.get("token", "")
    if _webui_passwords["admin"] and _pw_match(token, _webui_passwords["admin"]):
        return "admin"
    if not _webui_passwords["read"]:
        return "read"
    return "read" if _pw_match(token, _webui_passwords["read"]) else None


# ─── 文字轉語音（v2.27.0，jtlw_tts/；規格 specs/2026-10-08_TTS開發規格_v2.md）──────────
# 朗讀是主程式的一個模式（輸入來源「文字內容朗讀／文字轉語音檔」→ /api/start → translate_meeting.py --tts-file）。
# 這裡只剩：設定頁要的資訊（聲音、合成位置、播放裝置）、聲音管理與發音字典（管理者）、
# 瀏覽器播放時每段的音檔與播放位置回報。查狀態、試聽：讀取；匯入／刪除／改性別、預設與字典：管理者
try:
    import jtlw_tts as _tts
    _tts_import_err = ""
except Exception as _e:          # 第一次 --upgrade 跑的是舊腳本舊清單，拿不到新加的 jtlw_tts/；第二次才會到
    _tts = None
    _tts_import_err = f"{type(_e).__name__}: {_e}"
_TTS_MISSING = ("文字轉語音元件還沒安裝完成（通常是升級沒有完成）：請在安裝資料夾執行 "
                + (r".\install.ps1 -Upgrade" if os.name == "nt" else "./install.sh --upgrade")
                + "，完成後關掉 WebUI 再重新啟動")
TTS_POS_FLAG = BASE_DIR / ".webui_tts_pos"   # translate_meeting.py 的 _TTS_POS_FLAG：瀏覽器播到第幾段
_TTS_LIVE_RE = re.compile(r"^[0-9a-f]{16}$")


def _tts_cfg():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    except (OSError, ValueError):
        return {}


def _tts_fail(e):
    return JSONResponse({"ok": False, "error": e.message, "code": e.code}, status_code=e.status)


def _interp_win_ready():
    try:
        return bool(_tm_interp_win_ready and _tm_interp_win_ready())
    except Exception:
        return False


def _interp_them_info(devices):
    """念給對方聽在這台能不能用、要先裝什麼（v2.28.0，2026-10-10 使用者：「要特別標示需要安裝 blackhole」）。
    Windows（v2.29.0）：裝了 usbip-win2 就跟 Linux 一樣自動建立；沒裝時反灰、說明怎麼裝。
    說明文字用主程式那一份（命令列、互動選單、WebUI 同一句話）"""
    if sys.platform.startswith("linux"):
        return {"supported": True, "auto": True, "note": "Linux 自動建立虛擬麥克風，不用安裝"}
    if sys.platform == "darwin":
        return {"supported": True, "auto": False, "need": _TM_INTERP_MAC_NEED,
                "found": any(_TM_INTERP_VIRTUAL.search(d.get("name") or "") for d in devices),
                "found_note": "已偵測到 BlackHole 2ch：會議軟體的麥克風改選「BlackHole 2ch」，結束後記得改回來"}
    if _interp_win_ready():
        try:
            from jtlw_tts import vmic
            sac = vmic.win_sac_state() == "on"
        except Exception:
            sac = False
        if sac:                                   # 智慧型應用程式控制開著：我們直接跟驅動溝通、不載入 usbip-win2 沒簽章的程式庫，通常不受影響
            return {"supported": True, "auto": True, "note": _TM_INTERP_WIN_READY + "（這台開啟了「智慧型應用程式控制」：jt-live-whisper "
                    "直接跟 usbip-win2 的驅動溝通，通常不受影響；建立不起來時見手冊 4-16）"}
        return {"supported": True, "auto": True, "note": _TM_INTERP_WIN_READY}
    # 沒裝 usbip-win2：反灰並說明怎麼裝。已經有別的虛擬音效卡也不提供（常見的那幾套授權不合適，2026-10-10 使用者決定）
    return {"supported": False, "note": _TM_INTERP_WIN_NEED}


def _tts_info(admin, refresh=False):
    """設定頁「文字內容朗讀」要的全部資訊。合成位置分開回報能不能用（不能用的說明原因，前端反灰）"""
    if _tts is None:
        return {"installed": False, "reason": _TTS_MISSING, "detail": _tts_import_err}
    cfg = _tts_cfg()
    s = _tts.settings(cfg)
    rw = cfg.get("remote_whisper") or {}
    # 每個合成模型各自回報合成位置能不能用（BreezyVoice 只在 GPU 伺服器）；最上層的 locations 是預設模型的（舊前端照讀）
    models = []
    for key, m in _tts.MODELS.items():
        mlocs = []
        for where, label in (("remote", "GPU 伺服器" + (f"（{rw.get('host')}）" if rw.get("host") else "")),
                             ("mlx", "本機（Apple Silicon）")):
            prov, why = _tts.pick_provider(cfg, refresh=refresh, where=where, model=key)
            mlocs.append({"value": where, "label": label, "available": prov is not None, "reason": why})
        models.append({"value": key, "label": m["label"], "tag": m["tag"], "note": m["note"], "default": key == _tts.DEFAULT_MODEL,
                       "available": any(x["available"] for x in mlocs), "locations": mlocs})
    locs = next(m["locations"] for m in models if m["default"])
    voices = [_tts.public_voice(v) for v in _tts.list_voices()]
    try:
        devices = _tm_tts_output_devices() if _tm_tts_output_devices else []
    except Exception:
        devices = []
    out = {"installed": True, "locations": locs,
           "models": models,
           "voices": voices, "voice": _tts.default_voice_id(cfg),
           # 雙向語音口譯（v2.28.0）：念英文給對方聽的聲音、Linux 可以自動建立虛擬麥克風
           "en_voices": [_tts.public_voice(v) for v in _tts.list_voices("en")], "en_voice": _tts.default_en_voice_id(),
           "interp_auto_sink": sys.platform.startswith("linux") or _interp_win_ready(),
           "interp_auto_tag": "Linux" if sys.platform.startswith("linux") else "Windows",
           "interp_them": _interp_them_info(devices),
           "provider": s["provider"], "mac_steps": s["mac_steps"], "genders": _tts.GENDERS,
           "pauses": list(_tts.PAUSES), "rate_range": [_tts.RATE_MIN, _tts.RATE_MAX],
           "output_devices": devices, "max_chars": s["max_chars"], "text_exts": list(_tts.TEXT_EXTS)}
    if admin:
        out["custom"] = s["custom"]
    return out


@app.get("/api/tts/status")
def api_tts_status(request: Request):
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    return {"ok": True, **_tts_info(_check_auth(request, "admin") is None, request.query_params.get("refresh") == "1")}


@app.get("/api/tts/voices/{vid}/sample")
def api_tts_voice_sample(request: Request, vid: str):
    """試聽：參考錄音本身（合成出來的音色就是它）"""
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    try:
        return FileResponse(_tts.get_voice(vid)["wav"], media_type="audio/wav")
    except _tts.TTSError as e:
        return _tts_fail(e)


@app.get("/api/tts/live/{job}/{seq}")
def api_tts_live(request: Request, job: str, seq: int):
    """瀏覽器播放：朗讀中的第 seq 段（主程式寫在 tts_tmp/live_<job>/，朗讀結束就刪）"""
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None or not _TTS_LIVE_RE.match(job) or not 0 <= seq < 100000:
        return JSONResponse({"ok": False, "error": "找不到這一段"}, status_code=404)
    p = Path(_tts.engine.TMP_DIR) / f"live_{job}" / f"{seq}.wav"
    if not p.is_file():
        return JSONResponse({"ok": False, "error": "找不到這一段"}, status_code=404)
    return FileResponse(str(p), media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/api/tts/voices")
async def api_tts_voice_add(request: Request, file: UploadFile = FastFile(...)):
    """管理者匯入參考錄音（multipart：file、name、transcript、source、gender、consent=1）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    form = await request.form()
    import tempfile
    data = await file.read(20 * 1048576 + 1)
    if len(data) > 20 * 1048576:
        return JSONResponse({"ok": False, "error": "參考錄音超過 20 MB"}, status_code=413)
    fd, tmp = tempfile.mkstemp(suffix=Path(file.filename or "").suffix[:8] or ".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        meta = await asyncio.to_thread(_tts.import_voice, tmp, form.get("name"), form.get("transcript"),
                                       form.get("source"), form.get("consent") in ("1", "true", "on"),
                                       form.get("gender") or "")
    except _tts.TTSError as e:
        return _tts_fail(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    cfg = _tts_cfg()
    t = cfg.setdefault("tts", {})
    if not _tts.get_voice(t.get("voice"), missing_ok=True):      # 第一個聲音直接當預設
        t["voice"] = meta["id"]
        _write_config(cfg)
    return {"ok": True, "voice": _tts.public_voice(meta)}


@app.post("/api/tts/voices/{vid}")
def api_tts_voice_update(request: Request, vid: str, body: dict = {}):
    """管理者：改聲音的性別（v2.27.0 之前匯入的沒有這個欄位）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    try:
        _tts.set_voice_gender(vid, str(body.get("gender") or ""))
    except _tts.TTSError as e:
        return _tts_fail(e)
    return {"ok": True}


@app.delete("/api/tts/voices/{vid}")
def api_tts_voice_delete(request: Request, vid: str):
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    cfg = _tts_cfg()
    try:
        gpu = _tts.delete_voice(vid, cfg)           # GPU 伺服器上快取的那份一起刪（連不上就記下來、之後補刪）
    except _tts.TTSError as e:
        return _tts_fail(e)
    if (cfg.get("tts") or {}).get("voice") == vid:
        cfg["tts"]["voice"] = ""
        _write_config(cfg)
    return {"ok": True, "gpu": gpu}


@app.post("/api/tts/settings")
def api_tts_settings(request: Request, body: dict = {}):
    """管理者：預設聲音、發音字典（{詞: 注音}）、合成位置（auto／remote／mlx）、Mac 擴散步數（6／10）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    from jtlw_tts.tw_reading import _tts_custom
    cfg = _tts_cfg()
    t = dict(cfg.get("tts") or {})
    if "voice" in body:
        if body["voice"] and not _tts.get_voice(body["voice"], missing_ok=True):
            return JSONResponse({"ok": False, "error": "找不到這個聲音"}, status_code=404)
        t["voice"] = body["voice"] or ""
    if "custom" in body:
        if not isinstance(body["custom"] or {}, dict):
            return JSONResponse({"ok": False, "error": "發音字典格式不對（要是 {詞: 注音}）"}, status_code=400)
        custom = {str(k).strip(): str(v).strip() for k, v in (body["custom"] or {}).items() if str(k).strip()}
        _, bad = _tts_custom(custom)
        if bad:
            return JSONResponse({"ok": False, "error": "這幾個詞的注音字數和詞的字數不同（一個字一個注音，以空白分開）："
                                 + "、".join(bad)}, status_code=400)
        t["custom"] = custom
    if "provider" in body:
        if body["provider"] not in ("auto", "remote", "mlx"):
            return JSONResponse({"ok": False, "error": "合成位置只能是 auto、remote、mlx"}, status_code=400)
        t["provider"] = body["provider"]
    if "mac_steps" in body:
        try:
            steps = int(body["mac_steps"])
        except (TypeError, ValueError):
            steps = None
        if steps not in (6, 10):
            return JSONResponse({"ok": False, "error": "Mac 擴散步數只能是 6 或 10"}, status_code=400)
        t["mac_steps"] = steps
    cfg["tts"] = t
    _write_config(cfg)
    return {"ok": True}


# 選了「文字內容朗讀／文字轉語音檔」就先在背景叫醒 GPU 伺服器的合成程式（第一次啟動約 30 秒；2026-10-09 使用者：
# 「選完後就要先在背後啟動服務 節省時間」）。用預覽念法的介面念兩個字就會啟動它；10 分鐘內只叫一次
_TTS_WARM = {}                  # 合成模型 → {"at": 上次叫的時間, "running": 叫醒中}（兩個模型各自一個 worker）
_TTS_WARM_LOCK = threading.Lock()


def _tts_warm_run(where, model):
    try:
        prov, _ = _tts.pick_provider(_tts_cfg(), where=where, model=model)
        if prov is not None and getattr(prov, "kind", "") == "remote":
            prov.convert("暖機", {})
    except Exception:
        pass
    finally:
        _TTS_WARM[model]["running"] = False


INTERP_CMD = BASE_DIR / ".webui_interp_cmd"   # translate_meeting.py 的 _INTERP_CMD_FILE（一行一個指令）
INTERP_KEEP = BASE_DIR / ".webui_interp_keep"  # translate_meeting.py 的 _INTERP_KEEP_FILE（切換裝置時虛擬麥克風不要移除）


@app.post("/api/interp")
def api_interp(request: Request, body: dict = {}):
    """管理者：雙向語音口譯的控制（v2.28.0）。{action: cancel, id}＝取消還沒念的那句；{action: mute, lane: me|them, on}＝靜音某個方向。
    寫進指令檔，主程式每 0.2 秒讀一次（跟暫停旗標同一種做法：Windows 沒有可用的信號）"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    act = body.get("action")
    if act == "cancel":
        try:
            line = f"cancel {int(body.get('id'))}"
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "id 要是數字"}, status_code=400)
    elif act == "mute" and body.get("lane") in ("me", "them"):
        line = f"mute {body['lane']} {1 if body.get('on') else 0}"
    else:
        return JSONResponse({"ok": False, "error": "action 只有 cancel、mute"}, status_code=400)
    p = _proc                                   # 不拿 _proc_lock：停止時它可能被拿著好幾分鐘（等存檔）
    if p is None or p.poll() is not None:
        return JSONResponse({"ok": False, "error": "沒有在執行"}, status_code=409)
    try:
        with open(INTERP_CMD, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"寫不進指令檔：{e}"}, status_code=500)
    return {"ok": True}


@app.post("/api/tts/warm")
def api_tts_warm(request: Request, body: dict = {}):
    """管理者：背景叫醒合成程式（不等它好）。回 started＝這次有叫"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    where = body.get("where") if body.get("where") in ("auto", "remote", "mlx") else "auto"
    model = body.get("model") if body.get("model") in _tts.MODELS else _tts.DEFAULT_MODEL
    with _TTS_WARM_LOCK:
        w = _TTS_WARM.setdefault(model, {"at": -1e9, "running": False})
        if w["running"] or time.monotonic() - w["at"] < 600:
            return {"ok": True, "started": False}
        w.update(running=True, at=time.monotonic())
    threading.Thread(target=_tts_warm_run, args=(where, model), daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/tts/convert")
def api_tts_convert(request: Request, body: dict = {}):
    """管理者：預覽送進模型的文字（檢查發音字典），不合成"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=403)
    if _tts is None:
        return JSONResponse({"ok": False, "error": _TTS_MISSING}, status_code=503)
    cfg = _tts_cfg()
    where = body.get("where") if body.get("where") in ("auto", "remote", "mlx") else None
    model = body.get("model") if body.get("model") in _tts.MODELS else None
    prov, why = _tts.pick_provider(cfg, where=where, model=model)
    if not prov:
        return JSONResponse({"ok": False, "error": why}, status_code=503)
    text = str(body.get("text") or "").strip()[:300]
    if not text:
        return JSONResponse({"ok": False, "error": "沒有文字"}, status_code=400)
    custom = body.get("custom") if isinstance(body.get("custom"), dict) else _tts.settings(cfg)["custom"]
    try:
        return {"ok": True, "spoken": prov.convert(text, custom)}
    except _tts.TTSError as e:
        return _tts_fail(e)


def _tts_args(body):
    """朗讀的啟動參數（輸入來源 tts／tts_file）。貼上的文字先寫成檔案（命令列放不下、Windows 的編碼也麻煩）"""
    src = body.get("source")
    args = []
    text_file = (body.get("tts_file") or "").strip()
    if text_file:
        p = Path(text_file)
        p = p if p.is_absolute() else BASE_DIR / p
        p = p.resolve()
        ok_dirs = [(BASE_DIR / "recordings").resolve(), (BASE_DIR / "logs").resolve()]
        if not any(str(p).startswith(str(d) + os.sep) for d in ok_dirs) or not p.is_file():
            raise ValueError("只能朗讀 recordings/ 或 logs/ 裡的文字檔")
        if _tts is not None and p.suffix.lower() not in _tts.TEXT_EXTS:
            raise ValueError("只能朗讀 .txt／.md／.srt／.vtt")
        args += ["--tts-file", str(p)]
    else:
        text = str(body.get("tts_text") or "")
        if not text.strip():
            raise ValueError("沒有要朗讀的文字：請貼上文字，或選一個文字檔")
        d = BASE_DIR / "tts_tmp"
        d.mkdir(exist_ok=True)
        if _tts is not None:
            _tts.sweep_tmp()          # 以前貼上的文字（主程式讀完就不需要了），超過一天的刪掉
        p = d / f"input_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.txt"
        p.write_text(text, encoding="utf-8")
        args += ["--tts-file", str(p)]
    model = body.get("tts_model") or ""
    if model and (_tts is None or model not in _tts.MODELS):
        raise ValueError(f"沒有這個合成模型：{model}")
    if model and model != _tts.DEFAULT_MODEL:
        args += ["--tts-model", model]
    if body.get("tts_voice"):
        args += ["--tts-voice", str(body["tts_voice"])]
    try:
        rate = float(body.get("tts_rate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    args += ["--tts-rate", f"{rate:g}"]
    if body.get("tts_pause") in ("short", "normal", "long"):
        args += ["--tts-pause", body["tts_pause"]]
    if body.get("tts_where") in ("auto", "remote", "mlx"):
        args += ["--tts-provider", body["tts_where"]]
    if str(body.get("tts_steps")) in ("6", "10"):
        args += ["--tts-steps", str(body["tts_steps"])]
    if body.get("tts_start") not in (None, "", 1, "1"):
        try:
            n = int(body["tts_start"])
        except (TypeError, ValueError):
            raise ValueError("從第幾段開始念要是數字")
        if n < 1:
            raise ValueError("從第幾段開始念要從 1 起算")
        args += ["--tts-start", str(n)]
    if src == "tts_file":
        args += ["--tts-device", "none", "--tts-save", body.get("tts_save") if body.get("tts_save") in ("mp3", "wav")
                 else "mp3"]
    else:
        dev = str(body.get("tts_device") if body.get("tts_device") not in (None, "") else "default")
        if dev not in ("default", "browser") and not dev.isdigit():
            raise ValueError("播放裝置不對")
        args += ["--tts-device", dev]
        if body.get("tts_save") in ("mp3", "wav"):
            args += ["--tts-save", body["tts_save"]]
    if body.get("subtitle_overlay"):
        args.append("--subtitle-overlay")
    return args


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # v2.22.2 前這裡自己寫一套授權：(1) 拿 token 直接比對設定檔裡的**雜湊**（正確密碼被拒、雜湊本身反而能登入）；
    # (2) 自己判斷本機、沒走 _client_ip → 反向代理後面全部當本機；(3) HTTP middleware 管不到 WebSocket，
    # allowed_ips 對 /ws 無效；(4) 停止／暫停／靜音只要唯讀（沒設唯讀密碼時任何人都行）。一律改用共用函式。
    if not _ip_allowed(_client_ip(ws)):
        await ws.close(code=4003, reason="來源位址不在允許清單內")
        return
    level = _ws_level(ws)
    if level is None:
        await ws.close(code=4001, reason="需要密碼")
        return
    await ws.accept()
    connected_clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") in ("stop", "mute", "pause", "resume", "tts_pos") and level != "admin":
                    await ws.send_text(json.dumps({"type": "error", "error": "需要管理密碼"}))
                    continue
                if msg.get("action") == "stop":
                    await asyncio.to_thread(_stop_proc)
                    await broadcast(json.dumps({"type": "stopped"}))
                elif msg.get("action") == "mute":
                    # 寫入靜音 flag 檔案，translate_meeting.py 的 audio callback 會檢查
                    device = re.sub(r"[^0-9A-Za-z_-]", "", str(msg.get("device", "")))[:32]
                    muted = msg.get("muted", False)
                    flag_path = BASE_DIR / f".mute_{device}"
                    if muted:
                        flag_path.write_text("1")
                    else:
                        try:
                            flag_path.unlink()
                        except Exception:
                            pass
                elif msg.get("action") in ("pause", "resume"):
                    # 暫停／繼續（v2.26.3）：寫入／刪除旗標檔，translate_meeting.py 看到變化才切換。
                    # 以前送 SIGUSR1：Windows 沒有這個訊號（暫停在 Windows 一直沒作用），而且是「切換」，漏一次就永遠相反
                    _set_pause_flag(msg.get("action") == "pause")
                elif msg.get("action") == "tts_pos":
                    # 瀏覽器播放的朗讀：播到第幾段（主程式看這個決定字幕出哪段、預先合成到哪裡）
                    job = str(msg.get("job", ""))
                    if _TTS_LIVE_RE.match(job):
                        TTS_POS_FLAG.write_text(f"{job} {int(msg.get('seq', 0))}", encoding="utf-8")
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if ws in connected_clients:
            connected_clients.remove(ws)


def _tls_hosts_default():
    """沒指定 tls_hosts 時，把本機能對外的位址都寫進憑證的 SAN。

    少了這些，別人用 IP 連進來會驗不過憑證（憑證裡沒有那個 IP），
    症狀是「連得上但一直說憑證無效」——jtlw_api 那邊踩過同一個坑。
    """
    import socket as _s
    hosts = {"localhost", "127.0.0.1"}
    try:
        hosts.add(_s.gethostname())
    except Exception:
        pass
    try:
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.connect(("192.0.2.1", 1))     # 不會真的送封包，只問核心用哪個 IP 出去
        hosts.add(sk.getsockname()[0])
        sk.close()
    except Exception:
        pass
    return sorted(hosts)


# ─── 主程式 ──────────────────────────────────────────────────
def _no_gui():
    """Linux 沒有圖形桌面（SSH / 伺服器）時不開瀏覽器，避免開出文字模式瀏覽器佔住終端機"""
    return (sys.platform.startswith("linux")
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def _open_browser(url):
    """開瀏覽器。Linux 要脫離 session：從桌面捷徑啟動時視窗一關，同一個 session 裡剛開的瀏覽器會被一起帶走"""
    if sys.platform.startswith("linux") and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def _running_webui_url(port):
    """這個 port 上跑的是 jt-live-whisper 的 WebUI 時回傳它的網址，否則 None（v2.25.4）。

    用 /api/config 認：本機連線不需要密碼，回的 JSON 有 version（有密碼的遠端才會是 auth_required）。
    TLS 開關兩種都試，不看這一次的設定——原本那個可能是用 --no-tls 啟動的"""
    import ssl
    import urllib.error
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                 # 只連本機，自簽憑證不驗
    for scheme in ("http", "https"):
        try:
            with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/api/config", timeout=2,
                                        context=ctx if scheme == "https" else None) as r:
                body = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except Exception:
                continue
        except Exception:
            continue
        if isinstance(body, dict) and ("version" in body or "auth_required" in body):
            return f"{scheme}://localhost:{port}"
    return None


def main():
    parser = argparse.ArgumentParser(description="jt-live-whisper WebUI")
    parser.add_argument("--port", type=int, default=WEB_PORT, help=f"HTTP port (預設 {WEB_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    parser.add_argument("--no-tls", action="store_true",
                        help="即使設定開了 TLS 也強制用 HTTP（排除憑證問題時用）")
    args = parser.parse_args()

    # 檢查 port 是否被佔用
    import socket as _check_sock
    _ports_to_check = [args.port, TCP_PORT]
    for _port in _ports_to_check:
        _s = _check_sock.socket(_check_sock.AF_INET, _check_sock.SOCK_STREAM)
        _s.settimeout(0.5)
        if _s.connect_ex(("127.0.0.1", _port)) == 0:
            _s.close()
            _running = _running_webui_url(_port) if _port == args.port else None
            if _running:
                # 佔住的就是另一個 WebUI（例如桌面捷徑又點了一次）：先開瀏覽器連過去。
                # v2.25.4 前這裡預設是「結束佔用的程序」，按一下 Enter 就把正在錄音、處理中的那個砍掉
                print(f"\n  WebUI 已經在執行中：{_running}")
                if not args.no_browser and not _no_gui():
                    _open_browser(_running)
                    print("  已在瀏覽器開啟")
                if not sys.stdin.isatty():
                    sys.exit(0)
                print("  [Enter] 關閉這個視窗（原本的 WebUI 繼續執行）")
                print("  [1] 結束原本的 WebUI，重新啟動")
                print("  [2] 改用其他 Port，另外啟動一個")
                try:
                    _choice = input("  選擇 [Enter]：").strip()
                except (EOFError, KeyboardInterrupt):
                    sys.exit(0)
                if _choice not in ("1", "2"):
                    sys.exit(0)
            else:
                print(f"\n  [注意] Port {_port} 被佔用（可能是上次未正常結束的殘留程序）")
                print(f"  [1] 結束佔用的程序，繼續使用此 Port")
                print(f"  [2] 改用其他 Port")
                try:
                    _choice = input("  選擇 (1/2) [1]：").strip()
                except (EOFError, KeyboardInterrupt):
                    sys.exit(0)
            if _choice == "2":
                if _port == args.port:
                    try:
                        _new = int(input(f"  輸入新的 HTTP Port（預設 {args.port + 1}）：").strip() or str(args.port + 1))
                    except (ValueError, EOFError, KeyboardInterrupt):
                        _new = args.port + 1
                    args.port = _new
                    _ports_to_check[0] = _new
                # TCP port 自動跟隨
                continue
            # 選 1 或預設：砍掉佔用的程序
            try:
                import subprocess as _sp
                if sys.platform == "darwin" or sys.platform == "linux":
                    _pids = _sp.check_output(["lsof", "-ti", f":{_port}"], text=True).strip().split()
                else:
                    _pids = _sp.check_output(["fuser", f"{_port}/tcp"], text=True, stderr=_sp.DEVNULL).strip().split()
                for _pid in _pids:
                    try:
                        os.kill(int(_pid), 9)
                    except Exception:
                        pass
                time.sleep(0.5)
                print(f"  [完成] Port {_port} 已清理")
            except Exception:
                print(f"  [錯誤] 無法清理 Port {_port}，請手動結束佔用的程序")
                sys.exit(1)
        else:
            _s.close()

    # ── TLS ──
    # **預設關閉**：既有部署升級上來時網址不會從 http 變成 https，
    # 書籤、內部連結、別人寫好的腳本都不會壞。要加密必須明確打開。
    ssl_kw, scheme = {}, "http"
    if _tls_cfg["enabled"] and not args.no_tls:
        try:
            sys.path.insert(0, str(BASE_DIR))
            import jtlw_tls
            hosts = list(_tls_cfg["hosts"]) or _tls_hosts_default()
            created = jtlw_tls.ensure_self_signed(_tls_cfg["cert"], _tls_cfg["key"],
                                                  hosts, subject="/CN=jt-live-whisper WebUI")
            ssl_kw = {"ssl_certfile": _tls_cfg["cert"], "ssl_keyfile": _tls_cfg["key"]}
            scheme = "https"
            print(f"\n  TLS：{'自簽（本次新產生）' if created else '沿用既有憑證'}"
                  f"　{_tls_cfg['cert']}")
            print(f"    有效期限：{jtlw_tls.not_after(_tls_cfg['cert'])}")
            print(f"    SHA-256 指紋：{jtlw_tls.fingerprint(_tls_cfg['cert'])}")
            if created:
                print(f"    憑證中的位址：{', '.join(hosts)}")
                print("    （自簽憑證，瀏覽器第一次會跳警告，確認指紋後再繼續）")
        except Exception as e:
            # **產不出憑證就退回 HTTP，不要讓服務起不來。**
            # 這是常駐服務，起不來等於整個功能消失；而使用者原本就是 HTTP。
            print(f"\n  [TLS] 啟用失敗，改用 HTTP：{e}")
            ssl_kw, scheme = {}, "http"

    print(f"\n  jt-live-whisper WebUI")
    print(f"  {scheme}://localhost:{args.port}")
    print(f"  請在瀏覽器中操作\n")
    for line in _remote_access_warnings():
        print(f"  [安全提醒] {line}")
    # **一定要 flush**：systemd 下 stdout 是區塊緩衝，不 flush 的話上面這段
    # （包含憑證指紋）會卡在緩衝區，要等之後的輸出把它填滿才一起吐出來。
    # 管理者重啟後馬上看 journalctl 會看到「什麼都沒有」，而指紋正是那時
    # 最需要的東西。jtlw_api 那邊踩過同一個坑（2026-09-22 修）。
    sys.stdout.flush()

    # Linux 沒有圖形桌面（SSH / 伺服器）時不自動開瀏覽器，避免開出文字模式瀏覽器佔住終端機
    if not args.no_browser and not _no_gui():
        threading.Timer(1.0, lambda: webbrowser.open(f"{scheme}://localhost:{args.port}")).start()

    # Ctrl+C：uvicorn 執行中由 _WebUIServer.handle_exit 處理，前後由 _sigint_handler（見上面「Ctrl+C 結束 WebUI」）
    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        _WebUIServer(_uvicorn_config(app=app, host="0.0.0.0", port=args.port, log_level="warning", **ssl_kw)).run()
    except KeyboardInterrupt:
        pass
    finally:
        _STOPPING[0] = True             # 這裡在等存檔時再按 Ctrl+C → 立刻結束，不可以再進 _stop_proc
        _stop_proc()
        _say("\n  WebUI 已停止")
        os._exit(0)


if __name__ == "__main__":
    main()
