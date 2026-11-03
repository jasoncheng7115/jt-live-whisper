"""Apple Silicon 本機合成程式（v2.34.0，口譯 beta）：python -m jtlw_tts.mlx_worker <埠號>

雙向口譯在 Mac 本機合成時，合成要在另一個程式裡跑：主程式的 mlx-whisper 辨識也用 Metal，同一個程式裡兩邊同時推論會出事，
加鎖排隊的話合成一句要好幾秒，辨識等超過一個步進就把那段丟掉（漏聽）。不同程式同時用 Metal 沒問題，只是互相分 GPU 時間。
介面同 remote_whisper_server.py --tts-worker（jtlw_tts.engine.MlxWorkerProvider 照 CudaProvider 的方式叫它）：
GET /health（載入中 503、好了 200、載入失敗 500）、POST /synthesize（WAV＋X-TTS-Spoken）、/convert、/ping。
只聽 127.0.0.1；閒置 JT_TTS_WORKER_IDLE 秒（預設 600）自己結束，載入失敗 30 秒後結束。mlx-audio 的 VoxCPM2 不能串流，沒有 /synthesize_stream。
**載入與合成都在同一條執行緒**：MLX 的運算串流綁在執行緒上，在 A 執行緒載入、B 執行緒合成會整個程式中止
（2026-11-03 Mac 實測：libc++abi … There is no Stream(cpu, 1) in current thread）；HTTP 的執行緒只排隊等結果
"""
import http.server
import json
import os
import queue
import socketserver
import sys
import threading
import time
import urllib.parse

from .engine import MlxProvider, TTSError

FAILED_EXIT_SEC = 30


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"      # Windows 的 SO_REUSEADDR 讓第二份照樣綁上同一個埠（同 server.py 的合成程式）

    def server_bind(self):
        # 不查 127.0.0.1 的反向名稱（http.server 原本會 getfqdn，2026-10-25 Mac 這一步要 35 秒）
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def main(port, idle=None):
    idle = float(idle if idle is not None else os.environ.get("JT_TTS_WORKER_IDLE") or 600)
    prov = MlxProvider()
    st = {"ready": False, "error": None, "last": time.monotonic(), "busy": 0}
    lock = threading.Lock()
    jobs = queue.Queue()

    def mlx_thread():
        """唯一碰 MLX 的執行緒：先載入，之後一次做一件（一次一段，同 GPU 伺服器）"""
        try:
            prov._load()
            st["ready"] = True
            print(f"[mlx-worker] 模型載入完成（埠號 {port}）", flush=True)
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[mlx-worker] 模型載入失敗：{st['error']}", flush=True)
            return
        while True:
            fn, box, done = jobs.get()
            try:
                box["out"] = fn()
            except Exception as e:
                box["err"] = e
            done.set()

    def run(fn):
        box, done = {}, threading.Event()
        jobs.put((fn, box, done))
        done.wait()
        if "err" in box:
            raise box["err"]
        return box["out"]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
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
            if st["error"]:
                return self._send(500, {"ok": False, "error": st["error"]})
            if not st["ready"]:
                return self._send(503, {"ok": False, "loading": True})
            self._send(200, {"ok": True, "kind": "mlx", "pid": os.getpid()})

        def do_POST(self):
            with lock:
                st["last"] = time.monotonic()
                st["busy"] += 1
            try:
                self._post()
            finally:
                with lock:
                    st["last"] = time.monotonic()
                    st["busy"] -= 1

        def _post(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "要 JSON"})
            if self.path == "/ping":
                return self._send(200, {"ok": True, "ready": st["ready"]})
            if st["error"]:
                return self._send(500, {"error": "模型載入失敗：" + st["error"]})
            if not st["ready"]:
                return self._send(503, {"error": "模型還在載入"})
            try:
                if self.path == "/convert":
                    return self._send(200, {"spoken": run(lambda: prov.convert(req.get("text") or "", req.get("custom") or {}))})
                if self.path != "/synthesize":
                    return self._send(404, {"error": "not found"})
                voice = {"wav": req["voice_wav"], "transcript": req.get("voice_text") or ""}
                wav, sp = run(lambda: prov.synth(req.get("text") or "", voice, req.get("custom") or {},
                                                 steps=req.get("steps"), lang=req.get("lang") or "zh"))
                self._send(200, wav, "audio/wav", {"X-TTS-Spoken": urllib.parse.quote(sp or "")})
            except KeyError as e:
                self._send(400, {"error": f"少了 {e}"})
            except TTSError as e:
                self._send(e.status, {"error": e.message})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"[:300]})

    srv = _Server(("127.0.0.1", int(port)), H)     # 埠號被佔用（另一份已經在跑）就在這裡失敗結束，不會載第二份模型
    threading.Thread(target=mlx_thread, name="mlx", daemon=True).start()

    def watch():
        while True:
            time.sleep(1)
            with lock:
                quiet = time.monotonic() - st["last"]
                busy = st["busy"]
            if busy:
                continue
            if (st["error"] and quiet > FAILED_EXIT_SEC) or quiet > idle:
                print(f"[mlx-worker] {'載入失敗' if st['error'] else f'閒置 {idle:.0f} 秒'}，結束", flush=True)
                srv.shutdown()
                return
    threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()
    srv.server_close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit("用法：python -m jtlw_tts.mlx_worker <埠號>")
    main(int(sys.argv[1]))
