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
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("[錯誤] 需要安裝 fastapi 和 uvicorn：")
    print("  pip install fastapi uvicorn websockets")
    sys.exit(1)

# python-multipart 是 FastAPI 檔案上傳必要套件，舊版安裝可能缺少
try:
    import multipart  # noqa: F401
except ImportError:
    print("[提示] 正在安裝 python-multipart（檔案上傳需要）...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "python-multipart"])
    print("[完成] python-multipart 已安裝")

# ─── 設定 ────────────────────────────────────────────────────
TCP_PORT = 19780
WEB_PORT = 19781
BASE_DIR = Path(__file__).parent
TRANSLATE_SCRIPT = BASE_DIR / "translate_meeting.py"
CONFIG_FILE = BASE_DIR / "config.json"

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
    )
except Exception:
    _TM_WHISPER_MODELS = None
    _TM_SUMMARY_MODELS = None
    _tm_recommended_whisper_model = None
    _TM_SCK_LOOPBACK_ID = -300
    _TM_SCK_MIXED_ID = -400
    _tm_sck_check = None
    _tm_sck_macos_ok = None
    _tm_sck_request_permission = None
    _tm_sck_terminal_app_name = None

# ─── 安全設定 ──────────────────────────────────────────────────
_webui_passwords = {"read": "", "admin": ""}  # 從 config.json 載入
def _load_passwords():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            wp = cfg.get("webui_passwords", {})
            _webui_passwords["read"] = wp.get("read", "")
            _webui_passwords["admin"] = wp.get("admin", "")
        except Exception:
            pass
_load_passwords()

def _is_local(request) -> bool:
    """判斷是否為本機連線"""
    client = request.client.host if request.client else ""
    return client in ("127.0.0.1", "::1", "localhost", "0.0.0.0")

def _check_auth(request, level="read") -> str:
    """檢查授權，回傳 None（通過）或錯誤訊息"""
    if _is_local(request):
        return None  # 本機不需密碼
    if level == "admin":
        if not _webui_passwords["admin"]:
            return "未啟用遠端管理功能"
        token = request.headers.get("X-Auth-Token", "")
        if token != _webui_passwords["admin"]:
            return "需要管理密碼"
    elif level == "read":
        if not _webui_passwords["read"]:
            return None  # 唯讀密碼為空 = 不需密碼
        token = request.headers.get("X-Auth-Token", "")
        if token != _webui_passwords["read"] and token != _webui_passwords["admin"]:
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
    yield
    # shutdown: kill subprocess
    _stop_proc()


app = FastAPI(title="jt-live-whisper WebUI", lifespan=lifespan)

# ─── 靜態檔案服務（logs/ 子目錄，供 WebUI 開啟逐字稿/摘要 HTML）───
_logs_dir = BASE_DIR / "logs"
if _logs_dir.is_dir():
    app.mount("/logs", StaticFiles(directory=str(_logs_dir)), name="logs")

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
def _stop_proc():
    """停止子程序，三段升級：graceful → SIGTERM → SIGKILL。
    Windows 上若子程序在 native crash（如 0xC0000409）卡死，
    SIGINT/CTRL_BREAK 不一定收得到，必須走 SIGKILL 才殺得掉。"""
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            pid = _proc.pid
            # Step 1：graceful（平台相關）
            try:
                if sys.platform == "win32":
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.kill(pid, signal.SIGINT)
                _proc.wait(timeout=4)
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


def _start_proc(args: list):
    global _proc
    _stop_proc()
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
        _proc = subprocess.Popen(cmd, **_popen_kw)
        _proc._start_time = time.monotonic()
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
            if rc != 0 and elapsed < 5:
                msg = f"啟動失敗（錯誤碼 {rc}），請檢查終端機訊息"
            elif rc != 0:
                msg = f"程式異常結束（錯誤碼 {rc}）"
            else:
                msg = "處理已完成"
            print(f"\n  主程式已結束（exit code {rc}），WebUI 等待下一次操作（瀏覽器中按「回到設定」重新開始）")
            print(f"  按 Ctrl+C 可結束 WebUI 伺服器")
            if _event_queue:
                try:
                    _event_queue.put_nowait(json.dumps({"type": "disconnected",
                        "message": msg}))
                except Exception:
                    pass
        threading.Thread(target=_monitor, daemon=True).start()
    return _proc.pid


def _get_config():
    """讀取可用選項（從 translate_meeting.py 的常數 + config.json）"""
    modes = [
        {"value": "en2zh", "label": "英翻中字幕", "group": "單向翻譯"},
        {"value": "zh2en", "label": "中翻英字幕", "group": "單向翻譯"},
        {"value": "ja2zh", "label": "日翻中字幕", "group": "單向翻譯"},
        {"value": "zh2ja", "label": "中翻日字幕", "group": "單向翻譯"},
        {"value": "en_zh", "label": "英中雙向字幕", "group": "雙向翻譯"},
        {"value": "ja_zh", "label": "日中雙向字幕", "group": "雙向翻譯"},
        {"value": "en", "label": "英文轉錄", "group": "轉錄"},
        {"value": "zh", "label": "中文轉錄", "group": "轉錄"},
        {"value": "ja", "label": "日文轉錄", "group": "轉錄"},
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
        {"value": "nllb", "label": "NLLB — 本機離線，中日英互譯"},
        {"value": "argos", "label": "Argos — 本機離線，僅英翻中"},
    ]
    # LLM 翻譯模型清單
    llm_models = [
        {"value": "qwen2.5:14b", "label": "qwen2.5:14b — 品質好，速度快（推薦）"},
        {"value": "qwen2.5:32b", "label": "qwen2.5:32b — 品質很好，中日文翻譯推薦"},
        {"value": "qwen2.5:7b", "label": "qwen2.5:7b — 品質普通，速度最快"},
        {"value": "phi4:14b", "label": "phi4:14b — Microsoft，品質不錯"},
    ]
    # 讀 config.json 的預設 LLM 設定 + 使用者自訂模型
    llm_host = ""
    llm_model = "qwen2.5:14b"
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
    # 前次使用的設定（webui 自己存的）
    last = {}
    if CONFIG_FILE.exists():
        try:
            cfg2 = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            last = cfg2.get("webui_last", {})
        except Exception:
            pass
    # 音訊裝置
    devices = []
    auto_loopback = ""
    auto_mic = ""
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
        devices.append({"id": _TM_SCK_LOOPBACK_ID,
                        "name": "ScreenCaptureKit 系統音訊（免安裝 BlackHole）",
                        "channels": 2, "sr": 48000})
        auto_loopback = f"[{_TM_SCK_LOOPBACK_ID}] ScreenCaptureKit 系統音訊"
    try:
        import sounddevice as sd
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                devices.append({"id": i, "name": name,
                                "channels": dev["max_input_channels"],
                                "sr": int(dev["default_samplerate"])})
                # 自動偵測 loopback
                nl = name.lower()
                if not auto_loopback and ("blackhole" in nl or "loopback" in nl):
                    auto_loopback = f"[{i}] {name}"
        # 自動偵測麥克風（系統預設輸入，排除 loopback/aggregate）
        default_in = sd.default.device[0]
        if default_in is not None and default_in >= 0:
            dinfo = sd.query_devices(default_in)
            dn = dinfo["name"].lower()
            if (dinfo["max_input_channels"] > 0
                    and "blackhole" not in dn and "loopback" not in dn
                    and "aggregate" not in dn and "聚集" not in dinfo["name"]):
                auto_mic = f"[{default_in}] {dinfo['name']}"
    except Exception:
        pass
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
        summary_descs = {"gpt-oss:120b": "品質最好（推薦）", "gpt-oss:20b": "速度快，品質一般"}
    return {
        "modes": modes, "scenes": scenes, "models": models, "engines": engines,
        "llm_models": llm_models, "llm_host": llm_host, "llm_model": llm_model,
        "devices": devices, "auto_loopback": auto_loopback, "auto_mic": auto_mic,
        "gpu_host": gpu_host, "summary_descs": summary_descs,
        "recommended_models": recommended_models,
        "default_engine": "llm" if llm_host else "nllb",
        "sck": sck, "is_macos": sys.platform == "darwin",
        "last": last, "version": "2.18.2",
        "has_read_pw": bool(_webui_passwords["read"]),
        "has_admin_pw": bool(_webui_passwords["admin"]),
    }


# ─── 路由 ────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "webui.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>webui.html not found</h1>", status_code=404)


@app.get("/api/config")
async def api_config(request: Request):
    err = _check_auth(request, "read")
    if err:
        return JSONResponse({"auth_required": True, "error": err, "is_local": _is_local(request)}, status_code=401)
    cfg = _get_config()
    cfg["is_local"] = _is_local(request)
    return JSONResponse(cfg)


@app.post("/api/auth")
async def api_auth(request: Request, body: dict = {}):
    """驗證密碼，回傳角色（admin/read/denied）"""
    token = body.get("password", "")
    if _is_local(request):
        return {"role": "admin", "is_local": True}
    if _webui_passwords["admin"] and token == _webui_passwords["admin"]:
        return {"role": "admin"}
    if not _webui_passwords["read"] or token == _webui_passwords["read"]:
        return {"role": "read"}
    return JSONResponse({"role": "denied", "error": "密碼錯誤"}, status_code=401)


@app.get("/api/passwords")
async def api_get_passwords(request: Request):
    """取得密碼（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機"}, status_code=403)
    return {"read": _webui_passwords["read"], "admin": _webui_passwords["admin"]}


@app.post("/api/save-passwords")
async def api_save_passwords(request: Request, body: dict = {}):
    """儲存安全設定密碼（僅本機可用）"""
    if not _is_local(request):
        return JSONResponse({"ok": False, "error": "僅限本機設定"}, status_code=403)
    read_pw = body.get("read", "").strip()
    admin_pw = body.get("admin", "").strip()
    _webui_passwords["read"] = read_pw
    _webui_passwords["admin"] = admin_pw
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        cfg["webui_passwords"] = {"read": read_pw, "admin": admin_pw}
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
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
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
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
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@app.get("/api/fonts")
async def api_fonts(request: Request):
    """列出系統中支援中文的字型（僅本機）"""
    if not _is_local(request):
        return JSONResponse({"ok": False}, status_code=403)
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
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
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
        subprocess.Popen(["xdg-open", str(full)])
    return {"ok": True}


@app.get("/api/files")
async def api_files():
    """列出 recordings/ 目錄下的音訊/影片檔案"""
    rec_dir = BASE_DIR / "recordings"
    files = []
    if rec_dir.is_dir():
        exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mkv", ".webm", ".avi"}
        for f in sorted(rec_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in exts:
                st = f.stat()
                size_mb = round(st.st_size / 1048576, 1)
                files.append({"name": f.name, "size": size_mb, "path": str(f)})
    return JSONResponse({"files": files, "dir": str(rec_dir)})


@app.post("/api/upload")
async def api_upload():
    """上傳音訊/影片檔案到 recordings/"""
    from starlette.requests import Request
    # 需要 python-multipart
    try:
        from fastapi import UploadFile, File
    except ImportError:
        return JSONResponse({"ok": False, "error": "缺少 python-multipart 套件"})
    return JSONResponse({"ok": False, "error": "請使用 /api/upload-file 端點"})


from fastapi import UploadFile, File as FastFile


@app.post("/api/upload-file")
async def api_upload_file(file: UploadFile = FastFile(...)):
    """上傳音訊/影片檔案到 recordings/"""
    rec_dir = BASE_DIR / "recordings"
    rec_dir.mkdir(exist_ok=True)
    dest = rec_dir / file.filename
    # 避免覆蓋
    if dest.exists():
        stem, ext = dest.stem, dest.suffix
        i = 1
        while dest.exists():
            dest = rec_dir / f"{stem}_{i}{ext}"
            i += 1
    content = await file.read()
    dest.write_bytes(content)
    size_mb = round(len(content) / 1048576, 1)
    return JSONResponse({"ok": True, "name": dest.name, "size": size_mb, "path": str(dest)})


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
async def api_test_llm(body: dict = {}):
    """測試 LLM 伺服器連線"""
    host = body.get("host", "").strip()
    if not host:
        return JSONResponse({"ok": False, "error": "未填入主機位址"})
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
    if engine and mode not in ("en", "zh", "ja"):
        args.extend(["-e", engine])
        if engine == "llm":
            llm_model = body.get("llm_model", "")
            llm_host = body.get("llm_host", "")
            if llm_model:
                args.extend(["--llm-model", llm_model])
            if llm_host:
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
    device = body.get("device")
    if device is not None and device != "":
        args.extend(["-d", str(device)])
    mic_device = body.get("mic_device")
    if mic_device is not None and mic_device != "":
        args.extend(["--mic-device", str(mic_device)])
    return args


@app.post("/api/start")
async def api_start(request: Request, body: dict = {}):
    """啟動 translate_meeting.py"""
    err = _check_auth(request, "admin")
    if err:
        return JSONResponse({"status": "error", "error": err}, status_code=403)
    args = _build_args(body)
    pid = _start_proc(args)
    # 儲存前次使用的設定到 config.json
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        cfg["webui_last"] = {
            "mode": body.get("mode"), "model": body.get("model"),
            "scene": body.get("scene"), "engine": body.get("engine"),
            "llm_model": body.get("llm_model"), "llm_host": body.get("llm_host"),
            "local_asr": body.get("local_asr", False),
            "record": body.get("record", False), "mic": body.get("mic", False),
            "denoise": body.get("denoise", True),
            "diarize": body.get("diarize", False),
            "num_speakers": body.get("num_speakers", 0),
            "summarize": body.get("summarize", False),
            "summary_model": body.get("summary_model", ""),
            "summary_rounds": body.get("summary_rounds", 1),
            "gen_srt": not body.get("no_srt", False),
            "gen_vtt": not body.get("no_vtt", False),
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
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
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
    # 停止目前程序
    _stop_proc()
    await asyncio.sleep(0.5)
    # 用新設定重新啟動
    try:
        args = _build_args(start_body)
        pid = _start_proc(args)
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
async def api_status():
    with _proc_lock:
        running = _proc is not None and _proc.poll() is None
    return {"running": running}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # WS auth：遠端需要 token query param
    client_host = ws.client.host if ws.client else ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")
    if not is_local and _webui_passwords["read"]:
        token = ws.query_params.get("token", "")
        if token != _webui_passwords["read"] and token != _webui_passwords["admin"]:
            await ws.close(code=4001, reason="需要密碼")
            return
    await ws.accept()
    connected_clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") == "stop":
                    await asyncio.to_thread(_stop_proc)
                    await broadcast(json.dumps({"type": "stopped"}))
                elif msg.get("action") == "mute":
                    # 寫入靜音 flag 檔案，translate_meeting.py 的 audio callback 會檢查
                    device = msg.get("device", "")
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
                    # 送 SIGUSR1 到 translate_meeting.py 切換暫停
                    with _proc_lock:
                        if _proc and _proc.poll() is None:
                            try:
                                os.kill(_proc.pid, signal.SIGUSR1)
                            except Exception:
                                pass
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if ws in connected_clients:
            connected_clients.remove(ws)


# ─── 主程式 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="jt-live-whisper WebUI")
    parser.add_argument("--port", type=int, default=WEB_PORT, help=f"HTTP port (預設 {WEB_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    args = parser.parse_args()

    # 檢查 port 是否被佔用
    import socket as _check_sock
    _ports_to_check = [args.port, TCP_PORT]
    for _port in _ports_to_check:
        _s = _check_sock.socket(_check_sock.AF_INET, _check_sock.SOCK_STREAM)
        _s.settimeout(0.5)
        if _s.connect_ex(("127.0.0.1", _port)) == 0:
            _s.close()
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

    print(f"\n  jt-live-whisper WebUI")
    print(f"  http://localhost:{args.port}")
    print(f"  請在瀏覽器中操作\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    # Ctrl+C 強制退出（uvicorn 可能攔截 SIGINT）
    def _sigint_handler(sig, frame):
        print("\n  正在停止...")
        _stop_proc()
        print("  WebUI 已停止")
        os._exit(0)
    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        _stop_proc()
        print("\n  WebUI 已停止")
        os._exit(0)


if __name__ == "__main__":
    main()
