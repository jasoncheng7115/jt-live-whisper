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
    from fastapi.responses import HTMLResponse, JSONResponse
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
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            pid = _proc.pid
            # SIGINT 讓 signal handler 正常清理（儲存錄音檔等），最多等 6 秒
            try:
                os.kill(pid, signal.SIGINT)
                _proc.wait(timeout=6)
            except Exception:
                # 超時：強制 SIGKILL
                try:
                    os.kill(pid, 9)
                    _proc.wait(timeout=2)
                except Exception:
                        pass
            _proc = None
    # 清理靜音 flag 檔案
    for fn in (".mute_lb", ".mute_mic"):
        try:
            (BASE_DIR / fn).unlink()
        except Exception:
            pass


def _start_proc(args: list):
    global _proc
    _stop_proc()
    with _proc_lock:
        cmd = [sys.executable, str(TRANSLATE_SCRIPT), "--webui"] + args
        # stdin 持續送 'y\n' 自動確認所有互動提問（確認開始、錄音等）
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR),
                                 stdin=subprocess.PIPE,
                                 start_new_session=True)
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
            _proc.wait()
            rc = _proc.returncode
            elapsed = time.monotonic() - _proc._start_time if hasattr(_proc, '_start_time') else 999
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
        {"value": "record", "label": "純錄音", "group": "其他"},
    ]
    scenes = [
        {"value": "meeting", "label": "線上會議（5秒）"},
        {"value": "training", "label": "教育訓練（8秒）"},
        {"value": "presentation", "label": "演講簡報（12秒）"},
        {"value": "subtitle", "label": "快速字幕（3秒）"},
    ]
    try:
        from translate_meeting import WHISPER_MODELS as _WM
        models = [{"value": n, "label": f"{n}（{d}）"} for n, _, d in _WM]
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
        from translate_meeting import _recommended_whisper_model
        for m_info in modes:
            recommended_models[m_info["value"]] = _recommended_whisper_model(m_info["value"])
    except Exception:
        pass
    # 摘要模型說明（從 translate_meeting.py 的 SUMMARY_MODELS）
    summary_descs = {}
    try:
        from translate_meeting import SUMMARY_MODELS
        summary_descs = {n: d for n, d in SUMMARY_MODELS if d}
    except Exception:
        summary_descs = {"gpt-oss:120b": "品質最好（推薦）", "gpt-oss:20b": "速度快，品質一般"}
    return {
        "modes": modes, "scenes": scenes, "models": models, "engines": engines,
        "llm_models": llm_models, "llm_host": llm_host, "llm_model": llm_model,
        "devices": devices, "auto_loopback": auto_loopback, "auto_mic": auto_mic,
        "gpu_host": gpu_host, "summary_descs": summary_descs,
        "recommended_models": recommended_models,
        "default_engine": "llm" if llm_host else "nllb",
        "last": last, "version": "2.14.5",
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


@app.post("/api/test-llm")
async def api_test_llm(body: dict = {}):
    """測試 LLM 伺服器連線"""
    host = body.get("host", "").strip()
    if not host:
        return JSONResponse({"ok": False, "error": "未填入主機位址"})
    import urllib.request
    import urllib.error
    # 嘗試 Ollama /api/tags 和 OpenAI /v1/models
    for path in ["/api/tags", "/v1/models"]:
        url = f"http://{host}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                models = []
                if "models" in data:
                    # Ollama format
                    models = [m.get("name", "") for m in data["models"] if m.get("name")]
                elif "data" in data:
                    # OpenAI format
                    models = [m.get("id", "") for m in data["data"] if m.get("id")]
                server_type = "ollama" if "/api/" in path else "openai"
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
        }
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
    _stop_proc()
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
                    _stop_proc()
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

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        _stop_proc()
        print("\n  WebUI 已停止")
        os._exit(0)  # 避免 PyTorch atexit cleanup 的 KeyboardInterrupt 錯誤


if __name__ == "__main__":
    main()
