"""Windows 的口譯麥克風（v2.29.0）：雙向口譯「念給對方聽」把英文送進會議軟體的麥克風。

做法（研究與實測：specs/2026-10-10_Windows虛擬麥克風_usbip研究.md）：
本機 127.0.0.1 跑一個 USB/IP 伺服器，假裝是一支 USB Audio Class 1.0 麥克風（48 kHz、16 bit、單聲道）；
usbip-win2（開放原始碼 BSD-2-Clause，核心驅動由微軟簽署）把它接成本機的 USB 裝置，Windows 用內建的 USB 音訊驅動認它，
會議軟體的麥克風清單就看得到「jt-live-whisper Interpreter Mic」。我們不寫驅動、不需要簽章。

- 念出來的英文、同時送出的原聲各是一個聲道（channel），在這裡混音（Windows 不會幫一支麥克風混兩個來源）
- 沒有東西要念的時候送靜音；沒有人在錄（會議軟體沒開麥克風）時丟掉，不累積
- 協定（USB/IP 1.1.1）與等時傳輸的時序參考 Virtual Cables（BSD-2-Clause，github.com/tarekwasfy01/Virtual-Cables）：
  Windows 一次排好幾個 10 ms 的等時 URB，裝置要用自己的時鐘一個接一個完成（同時完成的話錄音快十倍）

協定的部分不依賴 Windows（Linux 的 vhci-hcd 也掛得上，測試用）；掛載與卸載（usbip.exe）只有 Windows。
"""
import heapq
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
DIR_OUT, DIR_IN = 0, 1
NO_ISO = 0xFFFFFFFF
ST_OK, ST_PIPE, ST_CONNRESET = 0, -32, -104
SPEED_FULL = 2

RATE = 48000
FRAME_BYTES = RATE // 1000 * 2          # 每 1 ms：48 個樣本 × 2 位元組（單聲道）
EP_IN = 0x81

PRODUCT = "jt-live-whisper Interpreter Mic"
MANUFACTURER = "jt-live-whisper"
SERIAL = "JTLW-INTERP-0001"             # 固定序號：Windows 認同一支裝置，會議軟體記得的選擇不會因為換了接口而跑掉
VID, PID = 0xFFFF, 0x4A01
BUSID = "1-1"
PORT = int(os.environ.get("JTLW_VMIC_PORT") or 3240)
LEAD = 0.2                              # 念的時候最多先送多少秒進緩衝（太少會斷音，太多對方聽到的時間比我們以為的晚）


def _str_desc(s):
    b = s.encode("utf-16-le")[:252]
    return bytes([len(b) + 2, 3]) + b


def build_descriptors(vid=VID, pid=PID, manufacturer=MANUFACTURER, product=PRODUCT, serial=SERIAL):
    dev = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0110, 0, 0, 0, 64, vid, pid, 0x0001, 1, 2, 3, 1)
    ac_body = (
        # 麥克風輸入端（Input Terminal 1，類型 0x0201 Microphone，單聲道）
        struct.pack("<BBBBHBBHBB", 12, 0x24, 0x02, 1, 0x0201, 0, 1, 0, 0, 0)
        # Feature Unit 2（來源 1，主控：靜音＋音量；聲道 1：無）
        + struct.pack("<BBBBBBBBB", 9, 0x24, 0x06, 2, 1, 1, 0x03, 0x00, 0)
        # USB 串流輸出端（Output Terminal 3，類型 0x0101，來源 2）
        + struct.pack("<BBBBHBBB", 9, 0x24, 0x03, 3, 0x0101, 0, 2, 0)
    )
    ac = struct.pack("<BBBHHBB", 9, 0x24, 0x01, 0x0100, 9 + len(ac_body), 1, 1) + ac_body
    cfg_body = (
        struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 0, 0x01, 0x01, 0x00, 0)          # 介面 0：AudioControl
        + ac
        + struct.pack("<BBBBBBBBB", 9, 4, 1, 0, 0, 0x01, 0x02, 0x00, 0)        # 介面 1 alt 0：零頻寬
        + struct.pack("<BBBBBBBBB", 9, 4, 1, 1, 1, 0x01, 0x02, 0x00, 0)        # 介面 1 alt 1：串流
        + struct.pack("<BBBBBH", 7, 0x24, 0x01, 3, 1, 0x0001)                  # AS general：連到端點 3、PCM
        + struct.pack("<BBBBBBBB", 11, 0x24, 0x02, 1, 1, 2, 16, 1) + RATE.to_bytes(3, "little")  # Type I：單聲道 16 bit 48 kHz
        + struct.pack("<BBBBHBBB", 9, 5, EP_IN, 0x0D, FRAME_BYTES, 1, 0, 0)   # 等時 IN、同步、96 位元組／1 ms
        + struct.pack("<BBBBBH", 7, 0x25, 0x01, 0x01, 0, 0)                    # CS 端點：取樣率控制
    )
    cfg = struct.pack("<BBHBBBBB", 9, 2, 9 + len(cfg_body), 2, 1, 0, 0x80, 50) + cfg_body
    strings = {0: bytes([4, 3, 0x09, 0x04]), 1: _str_desc(manufacturer), 2: _str_desc(product), 3: _str_desc(serial)}
    return dev, cfg, strings


class Ring:
    """一個聲道的 PCM 緩衝：push 進來、等時封包拿走；上限防止越積越多（延遲）"""

    def __init__(self, max_sec=2.0):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.max = int(RATE * max_sec) * 2

    def push(self, pcm):
        with self.lock:
            self.buf += pcm
            if len(self.buf) > self.max:
                del self.buf[: len(self.buf) - self.max]

    def read(self, n):
        """最多 n 位元組（不補零）"""
        with self.lock:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out

    def clear(self):
        with self.lock:
            self.buf.clear()

    def __len__(self):
        return len(self.buf)


def mix(parts, n):
    """幾個聲道各自讀出來的 PCM（長度可能不足）→ 混成 n 位元組（不足補靜音、相加後截到 16 bit）"""
    parts = [p for p in parts if p]
    if not parts:
        return b"\0" * n
    if len(parts) == 1:
        return parts[0] + b"\0" * (n - len(parts[0]))
    try:
        import numpy as np
    except Exception:                      # 小程式只靠 Python 本身也要能跑（numpy 的程式庫可能被應用程式控制擋）
        np = None
    if np is None:
        import array
        acc = [0] * (n // 2)
        for p in parts:
            a = array.array("h", p[: len(p) // 2 * 2])
            if sys.byteorder != "little":
                a.byteswap()
            for i, v in enumerate(a):
                acc[i] += v
        out = array.array("h", (32767 if v > 32767 else -32768 if v < -32768 else v for v in acc))
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()
    acc = np.zeros(n // 2, dtype=np.int32)
    for p in parts:
        a = np.frombuffer(p[: len(p) // 2 * 2], dtype="<i2")
        acc[: len(a)] += a
    return np.clip(acc, -32768, 32767).astype("<i2").tobytes()


class UsbMic:
    """USB/IP 伺服器＋UAC1 麥克風。channel(名稱) 取得一個聲道的緩衝，送出時混在一起"""

    def __init__(self, product=PRODUCT, manufacturer=MANUFACTURER, serial=SERIAL, vid=VID, pid=PID, busid=BUSID, log=None):
        self.dev, self.cfg, self.strings = build_descriptors(vid, pid, manufacturer, product, serial)
        self.vid, self.pid, self.busid = vid, pid, busid
        self.log = log or (lambda m: None)
        self.channels = {}
        self._ch_lock = threading.Lock()
        self.configuration = 0
        self.alt = {0: 0, 1: 0}
        self.mute, self.volume = False, 0
        self.stats = {"iso_urbs": 0, "iso_bytes": 0, "underrun_bytes": 0, "nonzero_urbs": 0, "control": 0, "unlink": 0,
                      "imports": 0}
        self.connected = threading.Event()        # Windows（或測試的用戶端）正在用這支裝置
        self._conn_lock = threading.Lock()
        self._busy = False
        self._stop = threading.Event()
        self._srv = None
        self._sock = None
        self.port = None

    # ── 給呼叫端 ──
    def channel(self, name):
        with self._ch_lock:
            if name not in self.channels:
                self.channels[name] = Ring()
            return self.channels[name]

    def active(self):
        """有人在錄（介面 1 切到 alt 1）"""
        return self.connected.is_set() and self.configuration == 1 and self.alt.get(1) == 1

    def clear(self, name=None):
        for n, r in list(self.channels.items()):
            if name is None or n == name:
                r.clear()

    def push(self, name, pcm):
        """送進一個聲道。沒有人在錄的時候丟掉（不累積：不然會議軟體一開麥克風先聽到舊的）"""
        if self.active():
            self.channel(name).push(pcm)

    def serve(self, host="127.0.0.1", port=PORT):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        else:                                     # Windows 的 SO_REUSEADDR 會讓兩個程式綁同一個埠：改用獨佔
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        s.bind((host, port))
        s.listen(4)
        s.settimeout(0.5)
        self._srv, self.port = s, s.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="vmic-accept", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        for s in (self._srv, self._sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    # ── 伺服器 ──
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._conn, args=(c,), name="vmic-conn", daemon=True).start()

    @staticmethod
    def _recv(c, n):
        b = bytearray()
        while len(b) < n:
            chunk = c.recv(n - len(b))
            if not chunk:
                raise ConnectionError("closed")
            b += chunk
        return bytes(b)

    def _dev_record(self):
        path = f"/sys/devices/platform/jtlw-interp/{self.busid}".encode().ljust(256, b"\0")
        busid = self.busid.encode().ljust(32, b"\0")
        d = self.dev
        return path + busid + struct.pack(">IIIHHHBBBBBB", 1, 1, SPEED_FULL, self.vid, self.pid, 0x0001,
                                          d[4], d[5], d[6], 1, d[17], 2)

    def _conn(self, c):
        try:
            c.settimeout(10)
            _, code, _ = struct.unpack(">HHI", self._recv(c, 8))
            if code == OP_REQ_DEVLIST:
                c.sendall(struct.pack(">HHI", VERSION, OP_REP_DEVLIST, 0) + struct.pack(">I", 1) + self._dev_record()
                          + bytes([1, 1, 0, 0]) + bytes([1, 2, 0, 0]))
                return
            if code != OP_REQ_IMPORT:
                return
            want = self._recv(c, 32).rstrip(b"\0").decode(errors="replace")
            if want != self.busid:
                c.sendall(struct.pack(">HHI", VERSION, OP_REP_IMPORT, 1))
                return
            # 同一時間只給一個連線：兩個虛擬裝置共用同一份狀態（介面、音量）會互相蓋掉、錄到靜音
            # （2026-10-10 pc-002：usbip-win2 自己重新連回舊的接口、又 attach 一次就變成兩個）。第二個拒絕，不影響正在用的
            with self._conn_lock:
                if self._busy:
                    c.sendall(struct.pack(">HHI", VERSION, OP_REP_IMPORT, 1))
                    self.log("已經有一個連線，拒絕第二個")
                    return
                self._busy = True
            try:
                c.sendall(struct.pack(">HHI", VERSION, OP_REP_IMPORT, 0) + self._dev_record())
                c.settimeout(None)
                self._sock = c
                self.stats["imports"] += 1
                self.connected.set()
                self._urb_loop(c)
            finally:
                self.connected.clear()
                self.configuration, self.alt = 0, {0: 0, 1: 0}
                self.clear()
                with self._conn_lock:
                    self._busy = False
        except (ConnectionError, OSError, struct.error):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def _read_audio(self, total):
        if not (self.active() and not self.mute):
            return b"\0" * total
        parts = [r.read(total) for r in list(self.channels.values())]
        got = max((len(p) for p in parts), default=0)
        if got < total and got:
            self.stats["underrun_bytes"] += total - got
        return mix(parts, total)

    def _urb_loop(self, c):
        wlock = threading.Lock()
        pending = {}                         # seq → 等時請求（還沒完成）
        heap, hcv = [], threading.Condition()
        iso_next = [0.0]
        alive = [True]

        def send(b):
            with wlock:
                c.sendall(b)

        def ret_submit(seq, status, actual, data=b"", packets=None, start_frame=0, nump=NO_ISO, errs=0):
            hdr = struct.pack(">IIIII", RET_SUBMIT, seq, 0, 0, 0)
            body = struct.pack(">iiiIi", status, actual, start_frame, nump if packets is not None else NO_ISO, errs) + b"\0" * 8
            tail = b"".join(struct.pack(">IIIi", *p) for p in (packets or []))
            send(hdr + body + data + tail)

        def iso_worker():
            while alive[0]:
                with hcv:
                    while alive[0] and not heap:
                        hcv.wait(0.5)
                    if not alive[0]:
                        return
                    due, seq = heap[0]
                    wait = due - time.monotonic()
                    if wait > 0:
                        hcv.wait(min(wait, 0.05))
                        continue
                    heapq.heappop(heap)
                    job = pending.pop(seq, None)
                if job is None:
                    continue                 # 已經被取消（UNLINK）
                req_len, packets, start_frame = job
                total = min(sum(p[1] for p in packets), req_len)
                data = self._read_audio(total)
                out, remaining = [], total
                for off, length, _, _ in packets:
                    take = max(0, min(length, remaining))
                    out.append((off, length, take, 0))
                    remaining -= take
                self.stats["iso_urbs"] += 1
                self.stats["iso_bytes"] += total
                if any(data):
                    self.stats["nonzero_urbs"] += 1
                try:
                    ret_submit(seq, ST_OK, total, data, out, start_frame, len(out))
                except OSError:
                    return

        threading.Thread(target=iso_worker, name="vmic-iso", daemon=True).start()
        try:
            while True:
                cmd, seq, _devid, direction, ep = struct.unpack(">IIIII", self._recv(c, 20))
                if cmd == CMD_SUBMIT:
                    _flags, length, _sf, nump, _interval = struct.unpack(">IiiIi", self._recv(c, 20))
                    setup = self._recv(c, 8)
                    out = self._recv(c, length) if direction == DIR_OUT and length > 0 else b""
                    packets = []
                    if nump not in (0, NO_ISO):
                        raw = self._recv(c, 16 * nump)
                        packets = [struct.unpack(">IIIi", raw[i:i + 16]) for i in range(0, len(raw), 16)]
                    if ep == 0:
                        self.stats["control"] += 1
                        data, status = self._control(setup, out)
                        data = data[:length]
                        actual = len(out) if direction == DIR_OUT and status == ST_OK else len(data)
                        ret_submit(seq, status, actual if status == ST_OK else 0, data if status == ST_OK else b"")
                    elif ep == (EP_IN & 0x0F) and direction == DIR_IN and packets:
                        dur = min(len(packets) * 0.001, 0.25)
                        now = time.monotonic()
                        base = max(iso_next[0], now)
                        iso_next[0] = base + dur
                        # 起始框（1 ms 一框）照排定的時間遞增：usbip-win2 一律用 ASAP、把這個值交給 Windows
                        sf = int(base * 1000) & 0x3FFFFFFF
                        with hcv:
                            pending[seq] = (length, packets, sf)
                            heapq.heappush(heap, (iso_next[0], seq))
                            hcv.notify()
                    else:
                        ret_submit(seq, ST_PIPE, 0, b"", [(p[0], p[1], 0, ST_PIPE) for p in packets] if packets else None,
                                   0, len(packets) if packets else NO_ISO, len(packets))
                elif cmd == CMD_UNLINK:
                    target = struct.unpack(">I", self._recv(c, 4))[0]
                    self._recv(c, 24)
                    with hcv:
                        found = pending.pop(target, None) is not None
                    self.stats["unlink"] += 1
                    send(struct.pack(">IIIII", RET_UNLINK, seq, 0, 0, 0) + struct.pack(">i", ST_CONNRESET if found else ST_OK)
                         + b"\0" * 24)
                else:
                    return
        finally:
            alive[0] = False
            with hcv:
                hcv.notify_all()

    # ── 控制請求 ──
    def _control(self, setup, out):
        rtype, req, value, index, length = struct.unpack("<BBHHH", setup)
        if rtype & 0x60 == 0:                          # 標準請求
            if req == 0x06:                            # GET_DESCRIPTOR
                typ, idx = value >> 8, value & 0xFF
                if typ == 1:
                    return self.dev[:length], ST_OK
                if typ == 2:
                    return self.cfg[:length], ST_OK
                if typ == 3 and idx in self.strings:
                    return self.strings[idx][:length], ST_OK
                return b"", ST_PIPE
            if req == 0x05:                            # SET_ADDRESS
                return b"", ST_OK
            if req == 0x09:                            # SET_CONFIGURATION
                self.configuration = value & 0xFF
                self.alt = {0: 0, 1: 0}
                return b"", ST_OK
            if req == 0x08:
                return bytes([self.configuration])[:length], ST_OK
            if req == 0x0B:                            # SET_INTERFACE（介面 1：alt 1＝開始錄音、alt 0＝停止）
                iface, alt = index & 0xFF, value & 0xFF
                if iface not in self.alt or alt > (1 if iface == 1 else 0):
                    return b"", ST_PIPE
                if iface == 1 and self.alt.get(1) == 1 and alt == 0:
                    self.clear()                       # 停止錄音：還沒送出去的丟掉，下次開麥克風不會先聽到舊的
                self.alt[iface] = alt
                return b"", ST_OK
            if req == 0x0A:
                return bytes([self.alt.get(index & 0xFF, 0)])[:length], ST_OK
            if req == 0x00:
                return b"\0\0"[:length], ST_OK
            if req in (0x01, 0x03):
                return b"", ST_OK
            return b"", ST_PIPE
        recipient = rtype & 0x1F
        if recipient == 2 and (index & 0xFF) == EP_IN and (value >> 8) == 1:   # 端點：取樣率
            if req == 0x01:
                return b"", ST_OK if out[:3] == RATE.to_bytes(3, "little") else ST_PIPE
            if req in (0x81, 0x82, 0x83):
                return RATE.to_bytes(3, "little")[:length], ST_OK
            if req == 0x84:
                return (1).to_bytes(3, "little")[:length], ST_OK
        if recipient == 1 and (index >> 8) == 2:       # Feature Unit 2
            cs = value >> 8
            if cs == 1:                                # 靜音
                if req == 0x01:
                    self.mute = bool(out[:1] and out[0])
                    return b"", ST_OK
                if req == 0x81:
                    return bytes([1 if self.mute else 0])[:length], ST_OK
            if cs == 2:                                # 音量（1/256 dB）：記下來但不套用（Windows 預設 0 dB）
                vals = {0x81: self.volume, 0x82: -60 * 256, 0x83: 0, 0x84: 256}
                if req == 0x01:
                    if len(out) >= 2:
                        self.volume = struct.unpack("<h", out[:2])[0]
                    return b"", ST_OK
                if req in vals:
                    return struct.pack("<h", vals[req])[:length], ST_OK
        return b"", ST_PIPE


class Feeder:
    """把一個聲道的 PCM 照真實時間送進麥克風（念出來的英文、原聲各一個）。sink：UsbMic，或連到口譯麥克風小程式的 Client。
    write() 跟喇叭播放一樣：大約念完才回來（留 0.3 秒讓下一段接上），呼叫端用它判斷「正在念」（回授過濾、原聲調小）。
    沒有人在錄的時候照樣等時間，但麥克風那邊不收（不累積舊的聲音）"""

    def __init__(self, sink, name):
        self.sink, self.name = sink, name
        self._end = 0.0

    def write(self, pcm, pause_ev=None, tail=0.3):
        step = RATE // 50 * 2                          # 20 ms
        t = max(time.monotonic(), self._end)
        for i in range(0, len(pcm), step):
            if pause_ev is not None and pause_ev.is_set():
                while pause_ev.is_set():
                    time.sleep(0.1)
                t = max(time.monotonic(), t)
            chunk = pcm[i:i + step]
            ahead = t - time.monotonic()
            if ahead > LEAD:
                time.sleep(ahead - LEAD)
            self.sink.push(self.name, chunk)
            t += len(chunk) / 2 / RATE
        self._end = t
        time.sleep(max(0.0, t - time.monotonic() - tail))

    def flush(self):
        """取消：還沒送出去的丟掉"""
        self.sink.clear(self.name)
        self._end = 0.0


# ── Windows：usbip-win2 的掛載與卸載 ─────────────────────────────────────────────
INSTALL_URL = "https://github.com/vadimgrn/usbip-win2/releases"
CTL_PORT = int(os.environ.get("JTLW_VMIC_CTL_PORT") or PORT + 9)   # 口譯麥克風小程式的控制埠（主程式送聲音、問狀態）
IDLE_SEC = 60                       # 沒有主程式連著多久就拔掉、結束（WebUI 切換裝置重開主程式約幾秒；跟 .webui_interp_keep 的 60 秒一致）


def usbip_exe():
    """usbip-win2 的命令列工具；沒有安裝回 None（測試可以用 JTLW_USBIP_EXE 指定）"""
    if os.environ.get("JTLW_USBIP_EXE"):
        return os.environ["JTLW_USBIP_EXE"]
    for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"):
        if base:
            p = os.path.join(base, "USBip", "usbip.exe")
            if os.path.isfile(p):
                return p
    return None


def _run(args, timeout=20):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000           # CREATE_NO_WINDOW
    r = subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    out = (r.stdout or b"") + (r.stderr or b"")
    for enc in ("utf-8", "cp950", "mbcs"):
        try:
            return r.returncode, out.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.returncode, out.decode("utf-8", "replace")


def parse_ports(text):
    """`usbip port` 的輸出 → [(接口編號, 網址)]"""
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"\s*Port\s+(\d+):", line)
        if m:
            cur = int(m.group(1))
            continue
        m = re.search(r"->\s*(usbip://\S+)", line)
        if m and cur is not None:
            out.append((cur, m.group(1)))
            cur = None
    return out


def our_ports(text, port=PORT, busid=BUSID):
    url = f"usbip://127.0.0.1:{port}/{busid}"
    return [n for n, u in parse_ports(text) if u == url]


# ── Windows：直接跟 usbip-win2 的驅動溝通（不經 usbip.exe）──────────────────────
# usbip.exe 要載入 libusbip.dll、resources.dll（0.9.8.1 沒有數位簽章），「智慧型應用程式控制」開啟時可能被擋；
# 驅動本身（usbip2_ude）由微軟簽署。驅動的介面（DeviceIoControl）照 usbip-win2 v.0.9.8.1 的 include/usbip/vhci.h，
# 用的都是 Windows 自己的 kernel32／cfgmgr32 與 Python 的 ctypes，所以不受影響。
# 回傳的長度跟這裡算的結構大小對不上（之後的版本改了介面）就不用它，退回 usbip.exe
_VHCI_GUID = "B4030C06-DC5F-4FCC-87EB-E5515A0935C0"


def _ctl(func):
    return (0x22 << 16) | (3 << 14) | (func << 2)          # CTL_CODE(FILE_DEVICE_UNKNOWN, func, METHOD_BUFFERED, FILE_READ_DATA|FILE_WRITE_DATA)


IOCTL_PLUGIN, IOCTL_PLUGOUT, IOCTL_GET_IMPORTED, IOCTL_STOP_ATTEMPTS = _ctl(0x800), _ctl(0x801), _ctl(0x802), _ctl(0x805)
LOC_SIZE = 1100                     # imported_device_location：port 4＋location_hash 4＋busid 32＋service 32＋host 1025，補到 4 的倍數
DEV_SIZE = LOC_SIZE + 32            # imported_device＝location＋properties（devid 4、speed 4、vendor 2、product 2、serial 16、iserial 1、wsk 1）
PLUGIN_SIZE = 4 + LOC_SIZE + 16 + 1 + 3       # base＋location＋serial[16]＋wsk_events，補到 4 的倍數＝1124
STOP_SIZE = 4 + LOC_SIZE + 4                  # base＋location＋count


def _pack_location(buf, off, host, service, busid):
    for val, start, size in ((busid, 8, 32), (service, 40, 32), (host, 72, 1025)):
        b = val.encode("ascii")[:size - 1]
        buf[off + start:off + start + len(b)] = b


def _cstr(b):
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def parse_imported(raw):
    """GET_IMPORTED_DEVICES 的回傳 → [(port, 網址, vendor, product)]；長度對不上回 None（介面變了）"""
    if len(raw) < 4 or (len(raw) - 4) % DEV_SIZE:
        return None
    out = []
    for i in range((len(raw) - 4) // DEV_SIZE):
        d = raw[4 + i * DEV_SIZE:4 + (i + 1) * DEV_SIZE]
        port = struct.unpack_from("<i", d, 0)[0]
        busid, service, host = _cstr(d[8:40]), _cstr(d[40:72]), _cstr(d[72:1097])
        vendor, product = struct.unpack_from("<HH", d, LOC_SIZE + 8)
        out.append((port, f"usbip://{host}:{service}/{busid}", vendor, product))
    return out


class VhciDriver:
    """usbip-win2 驅動的四個指令：列出、掛上、拔掉、停止重試。打不開（沒裝、不是 Windows）時 open() 回錯誤說明"""

    def __init__(self):
        self.h = None

    @staticmethod
    def path():
        import ctypes
        from ctypes import wintypes
        cfg = ctypes.WinDLL("cfgmgr32")
        guid = _guid(_VHCI_GUID)
        n = wintypes.ULONG()
        if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(n), ctypes.byref(guid), None, 0) != 0 or n.value <= 1:
            return None
        buf = ctypes.create_unicode_buffer(n.value)
        if cfg.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buf, n.value, 0) != 0:
            return None
        paths = [x for x in buf[:n.value].split("\0") if x]
        return paths[0] if len(paths) == 1 else None

    def open(self):
        if os.name != "nt":
            return "不是 Windows"
        import ctypes
        from ctypes import wintypes
        try:
            p = self.path()
        except OSError as e:
            return f"找不到 usbip-win2 的驅動（{e}）"
        if not p:
            return "找不到 usbip-win2 的驅動（沒有安裝，或需要重新開機）"
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE)
        h = k.CreateFileW(p, 0xC0000000, 3, None, 3, 0x80, None)
        if h in (None, wintypes.HANDLE(-1).value):
            return f"打不開 usbip-win2 的驅動（錯誤 {ctypes.get_last_error()}）"
        self.h, self.k = h, k
        return None

    def _io(self, code, inbuf, outlen):
        import ctypes
        from ctypes import wintypes
        k = self.k
        k.DeviceIoControl.restype = wintypes.BOOL
        k.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        ib = ctypes.create_string_buffer(bytes(inbuf), len(inbuf))
        ob = ctypes.create_string_buffer(max(outlen, 1))
        got = wintypes.DWORD()
        ok = k.DeviceIoControl(self.h, code, ib, len(inbuf), ob if outlen else None, outlen, ctypes.byref(got), None)
        if not ok:
            raise OSError(ctypes.get_last_error(), f"DeviceIoControl 0x{code:x}")
        return ob.raw[:got.value]

    def imported(self):
        """[(port, 網址, vendor, product)]；介面對不上回 None"""
        for cnt in (4, 16, 64, 256):
            try:
                # size＝sizeof(get_imported_devices)（含一個 devices 元素），驅動會檢查
                raw = self._io(IOCTL_GET_IMPORTED, struct.pack("<I", 4 + DEV_SIZE), 4 + cnt * DEV_SIZE)
            except OSError as e:
                if e.errno == 122:                  # ERROR_INSUFFICIENT_BUFFER
                    continue
                raise
            return parse_imported(raw)
        return None

    def attach(self, host, service, busid):
        """掛上（伺服器暫時不在時驅動會自己一直重試）→ 接口編號；回傳長度不對回 None（介面變了）"""
        buf = bytearray(PLUGIN_SIZE)
        struct.pack_into("<I", buf, 0, PLUGIN_SIZE)
        _pack_location(buf, 4, host, service, busid)
        out = self._io(IOCTL_PLUGIN, buf, 8)
        if len(out) != 8:
            return None
        return struct.unpack_from("<i", out, 4)[0]

    def detach(self, port):
        self._io(IOCTL_PLUGOUT, struct.pack("<Ii", 8, port), 0)

    def stop_attempts(self, host, service, busid):
        buf = bytearray(STOP_SIZE)
        struct.pack_into("<I", buf, 0, STOP_SIZE)
        _pack_location(buf, 4, host, service, busid)
        out = self._io(IOCTL_STOP_ATTEMPTS, buf, STOP_SIZE)
        return struct.unpack_from("<i", out, STOP_SIZE - 4)[0] if len(out) == STOP_SIZE else None

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None


def _try_driver():
    return os.name == "nt"


def usbip_installed():
    """裝了 usbip-win2：驅動在（或至少有 usbip.exe）"""
    if usbip_exe():
        return True
    if os.name != "nt":
        return False
    try:
        return bool(VhciDriver.path())
    except Exception:
        return False


# ── Windows：智慧型應用程式控制 ─────────────────────────────────────────────
# usbip-win2 0.9.8.1 的 libusbip.dll、resources.dll 沒有數位簽章：「開啟」時 Windows 可能擋下，usbip.exe 起不來、口譯麥克風掛不上。
# 微軟沒有提供個別放行（FAQ："There is currently no way to bypass Smart App Control protection for individual apps"），只能整個關掉
SAC_HINT = ("這台電腦開啟了「智慧型應用程式控制」，可能擋下沒有數位簽章的程式庫。"
            "處理方式見手冊 4-16「智慧型應用程式控制」（只能整個關閉；不關的話改用「念給我聽」）")


def win_sac_state():
    """'on'／'eval'／'off'／None（不是 Windows 或讀不到）"""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            v = winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0]
        return {0: "off", 1: "on", 2: "eval"}.get(int(v))
    except (OSError, ValueError):
        return None


def _with_sac_hint(msg):
    return msg + ("。" + SAC_HINT if win_sac_state() == "on" else "")


# ── Windows：系統預設的麥克風 ────────────────────────────────────────────────
# Windows 會把新接上的 USB 麥克風自動設成預設麥克風（2026-10-10 pc-002 實測：口譯麥克風、測試麥克風一接上就變預設）。
# 會議軟體用「預設」的話就改收口譯麥克風了，所以掛上之前記下預設、之後被換掉就改回來。
# 讀用 MMDevice API（公開）；設定用 IPolicyConfig（Windows 7 起沒變過、沒有公開文件，各種切換音訊裝置的工具都用它）
_ROLES = (0, 2)                     # eConsole（一般；eMultimedia 跟著它）、eCommunications（通訊）


def _com_call(obj, index, argtypes, *args, restype=None):
    import ctypes
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype or ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _guid(s):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]
    u = uuid.UUID(s)
    g = GUID(u.fields[0], u.fields[1], u.fields[2])
    g.d4[:] = list(u.bytes[8:])
    return g


def _co_create(clsid, iid):
    import ctypes
    ole32 = ctypes.OleDLL("ole32")
    p = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid(clsid)), None, 0x17, ctypes.byref(_guid(iid)), ctypes.byref(p))
    return p


def win_default_mics():
    """{角色: 裝置 ID}（錄音）；拿不到的角色不列"""
    import ctypes
    ole32 = ctypes.OleDLL("ole32")
    try:
        ole32.CoInitializeEx(None, 0)
    except OSError:
        pass
    out = {}
    en = _co_create("BCDE0395-E52F-467C-8E3D-C4579291692E", "A95664D2-9614-4F35-A746-DE8DB63617E6")
    try:
        for role in _ROLES:
            dev = ctypes.c_void_p()
            try:
                _com_call(en, 4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)), 1, role, ctypes.byref(dev))
            except OSError:
                continue                           # 沒有任何麥克風
            sid = ctypes.c_wchar_p()
            try:
                _com_call(dev, 5, (ctypes.POINTER(ctypes.c_wchar_p),), ctypes.byref(sid))
                out[role] = sid.value
                ole32.CoTaskMemFree(sid)
            finally:
                _com_call(dev, 2, (), restype=ctypes.c_ulong)
    finally:
        _com_call(en, 2, (), restype=ctypes.c_ulong)
    return out


def win_set_default_mic(dev_id, role):
    import ctypes
    ole32 = ctypes.OleDLL("ole32")
    try:
        ole32.CoInitializeEx(None, 0)
    except OSError:
        pass
    pc = _co_create("870af99c-171d-4f9e-af0d-e63df40c2bc9", "f8679f50-850a-41cf-9c72-430f290290c8")
    try:
        _com_call(pc, 13, (ctypes.c_wchar_p, ctypes.c_int), dev_id, role)
    finally:
        _com_call(pc, 2, (), restype=ctypes.c_ulong)


# ── 口譯麥克風小程式（Windows）────────────────────────────────────────────────
# USB/IP 伺服器放在另一個行程：
# - 主程式結束（WebUI 切換裝置會重開主程式）時裝置不會消失：伺服器一斷線 usbip-win2 就把裝置拔掉，會議軟體看到麥克風不見
#   （2026-10-10 pc-002 實測）。小程式在沒有主程式連著 IDLE_SEC 秒之後才拔掉、結束；主程式正常結束時叫它馬上拔
# - 等時傳輸每 10 ms 要回一次，跟忙著辨識、翻譯的主程式分開比較穩
# 控制埠的訊框：指令 1 位元組＋名稱長度 2＋資料長度 4（big-endian）＋名稱＋資料。
#   P 送聲音（名稱＝聲道）、C 清掉聲道、S 狀態（回 S＋JSON）、Q 拔掉並結束（做完回 Q）
_HDR = ">cHI"


def _self_sha():
    import hashlib
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _send(c, cmd, name=b"", data=b""):
    c.sendall(struct.pack(_HDR, cmd, len(name), len(data)) + name + data)


def _recv_frame(c):
    cmd, nl, dl = struct.unpack(_HDR, UsbMic._recv(c, 7))
    name = UsbMic._recv(c, nl) if nl else b""
    data = UsbMic._recv(c, dl) if dl else b""
    return cmd, name, data


class Helper:
    """口譯麥克風小程式本體（python vmic.py --serve）"""

    def __init__(self, port=PORT, ctl_port=CTL_PORT, idle=IDLE_SEC, attach=True, log=None):
        self.port, self.ctl_port, self.idle, self.attach = port, ctl_port, idle, attach
        self.log = log or (lambda m: None)
        self.mic = None
        self.exe = None
        self.drv = None                     # VhciDriver：直接跟驅動溝通（智慧型應用程式控制擋不到）
        self.method = None                  # "driver"／"usbip.exe"
        self.hub_port = None
        self.error = None
        self.restored = []
        self.quit = threading.Event()
        self.clients = 0
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.sha = _self_sha()

    def status(self):
        m = self.mic
        return {"connected": bool(m and m.connected.is_set()), "active": bool(m and m.active()), "error": self.error,
                "hub_port": self.hub_port, "method": self.method, "restored": self.restored, "sha": self.sha, "pid": os.getpid(),
                "stats": dict(m.stats) if m else {}}

    def run(self):
        if os.name == "nt":
            # 程式庫被擋、找不到時不要跳系統錯誤視窗卡住（沒有人看得到這個背景程式），直接失敗回報；usbip.exe 會繼承
            try:
                import ctypes
                ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
            except Exception:
                pass
        # 上一個小程式剛結束時埠號可能還沒放出來（TIME_WAIT）：重試幾秒
        ctl = err = None
        for _ in range(20):
            ctl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt":
                ctl.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
            else:
                ctl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                ctl.bind(("127.0.0.1", self.ctl_port))
                break
            except OSError as e:
                ctl.close()
                ctl, err = None, e
                time.sleep(0.3)
        if ctl is None:
            self.log(f"控制埠 {self.ctl_port} 被占用：{err}")
            return 3
        ctl.listen(8)
        ctl.settimeout(0.5)
        self.mic = UsbMic(log=self.log)
        for i in range(10):
            try:
                self.mic.serve(port=self.port)
                break
            except OSError as e:
                if i == 9:
                    self.error = f"口譯麥克風用的連接埠 {self.port} 被別的程式占用（{e.strerror or e}）"
                time.sleep(0.3)
        if self.mic.port and self.attach and not self.error:
            self._attach()
        while not self.quit.is_set():
            try:
                c, _ = ctl.accept()
            except socket.timeout:
                with self.lock:
                    idle = self.clients == 0 and time.monotonic() - self.last > self.idle
                if idle:
                    self.log(f"{self.idle} 秒沒有主程式連著，拔掉口譯麥克風")
                    break
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(c,), daemon=True).start()
        self._detach()
        self.mic.stop()
        ctl.close()
        return 0

    def _client(self, c):
        with self.lock:
            self.clients += 1
        try:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                cmd, name, data = _recv_frame(c)
                if cmd == b"P":
                    self.mic.push(name.decode(), data)
                elif cmd == b"C":
                    self.mic.clear(name.decode() or None)
                elif cmd == b"S":
                    import json
                    _send(c, b"S", data=json.dumps(self.status()).encode())
                elif cmd == b"Q":
                    self._detach()
                    _send(c, b"Q")
                    self.quit.set()
                    return
        except (ConnectionError, OSError, struct.error):
            pass
        finally:
            with self.lock:
                self.clients -= 1
                self.last = time.monotonic()
            try:
                c.close()
            except OSError:
                pass

    def _attach(self):
        """已經掛著（上一個小程式當掉留下的）就等驅動自己接回來，不再 attach（會多一支「2-…」）；沒有才 attach。
        先直接跟驅動溝通；不行（打不開、介面對不上）才用 usbip.exe。掛上前記下預設麥克風，之後 15 秒內被換掉就改回來"""
        want = os.environ.get("JTLW_VMIC_METHOD", "")          # 測試用：driver／usbip.exe，空的＝自動
        if _try_driver() and want != "usbip.exe":
            d = VhciDriver()
            err = d.open()
            if err:
                self.log(f"不能直接跟驅動溝通：{err}")
            else:
                self.drv = d
        self.exe = usbip_exe() if want != "driver" else None
        if not (self.drv or self.exe):
            self.error = "沒有安裝 usbip-win2"
            return
        before = {}
        if os.name == "nt":
            try:
                before = win_default_mics()
            except Exception as e:
                self.log(f"讀不到預設麥克風：{type(e).__name__}: {e}")
        url = f"usbip://127.0.0.1:{self.port}/{BUSID}"
        if self.drv:
            try:
                imp = self.drv.imported()
                if imp is None:
                    raise ValueError("驅動回傳的長度跟這一版不同")
                mine = [p for p, u, _, _ in imp if u == url]
                if mine:
                    self.hub_port = mine[0]
                else:
                    hp = self.drv.attach("127.0.0.1", str(self.port), BUSID)
                    if hp is None:
                        raise ValueError("驅動回傳的長度跟這一版不同")
                    self.hub_port = hp
                self.method = "driver"
            except (OSError, ValueError) as e:
                self.log(f"直接跟驅動溝通失敗（{e}），改用 usbip.exe")
                self.drv.close()
                self.drv = None
                if not self.exe:
                    self.error = _with_sac_hint(f"usbip-win2 掛載失敗（{e}）")
                    return
        if not self.drv:
            try:
                rc, txt = _run([self.exe, "--tcp-port", str(self.port), "port"])
                mine = our_ports(txt, self.port)
                if mine:
                    self.hub_port = mine[0]
                else:
                    rc, txt = _run([self.exe, "--tcp-port", str(self.port), "attach", "-r", "127.0.0.1", "-b", BUSID, "-t"])
                    if rc != 0:
                        self.error = _with_sac_hint(f"usbip-win2 掛載失敗（{txt.strip()[:200] or rc}）")
                        return
                    m = re.search(r"\b(\d{1,3})\b", txt.strip())
                    self.hub_port = int(m.group(1)) if m else None
                self.method = "usbip.exe"
            except (OSError, subprocess.SubprocessError) as e:
                self.error = _with_sac_hint(f"usbip-win2 執行失敗：{type(e).__name__}: {e}")
                return
        if before:
            threading.Thread(target=self._keep_default, args=(before,), daemon=True).start()

    def _keep_default(self, before, secs=15):
        end = time.monotonic() + secs
        while time.monotonic() < end and not self.quit.is_set():
            time.sleep(0.5)
            try:
                cur = win_default_mics()
                for role, dev in before.items():
                    if cur.get(role) and cur[role] != dev:
                        win_set_default_mic(dev, role)
                        self.restored.append(role)
                        self.log(f"Windows 把預設麥克風換成口譯麥克風了，改回原本的（角色 {role}）")
            except Exception as e:
                self.log(f"改回預設麥克風失敗：{type(e).__name__}: {e}")
                return

    def _detach(self):
        """先拔掉（伺服器還在，Windows 看到的是正常拔除）、再停止重試"""
        if not self.attach:
            return
        drv, exe, self.drv, self.exe = self.drv, self.exe, None, None
        url = f"usbip://127.0.0.1:{self.port}/{BUSID}"
        if drv:
            try:
                imp = drv.imported() or []
                for n in [p for p, u, _, _ in imp if u == url] or ([self.hub_port] if self.hub_port else []):
                    drv.detach(n)
                drv.stop_attempts("127.0.0.1", str(self.port), BUSID)
                return
            except (OSError, ValueError) as e:
                self.log(f"直接跟驅動拔除失敗（{e}），改用 usbip.exe")
            finally:
                drv.close()
        if not exe:
            return
        try:
            rc, txt = _run([exe, "--tcp-port", str(self.port), "port"], timeout=10)
            for n in our_ports(txt, self.port) or ([self.hub_port] if self.hub_port else []):
                _run([exe, "detach", "-p", str(n)], timeout=10)
            _run([exe, "--tcp-port", str(self.port), "attach", "-r", "127.0.0.1", "-b", BUSID, "--stop"], timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


class Client:
    """主程式這邊：連到口譯麥克風小程式（Feeder 的 sink）"""

    def __init__(self, c):
        self.c = c
        self.lock = threading.Lock()

    def push(self, name, pcm):
        with self.lock:
            _send(self.c, b"P", name.encode(), pcm)

    def clear(self, name=None):
        with self.lock:
            _send(self.c, b"C", (name or "").encode())

    def status(self):
        import json
        with self.lock:
            _send(self.c, b"S")
            cmd, _, data = _recv_frame(self.c)
        return json.loads(data)

    def quit(self, timeout=20):
        with self.lock:
            self.c.settimeout(timeout)
            _send(self.c, b"Q")
            try:
                _recv_frame(self.c)
            except (OSError, struct.error):
                pass


class WinVirtualMic:
    """開口譯時建立、結束時移除的口譯麥克風（Windows）。

    start()：連到口譯麥克風小程式；沒有在跑（或是別的版本）就啟動一個，等 Windows 接上。
    close(keep)：keep＝留給下一個程式接手（WebUI 切換裝置）：只斷線，小程式 60 秒內等新的主程式；否則叫它馬上拔掉並結束"""

    def __init__(self, log=None, port=PORT, ctl_port=CTL_PORT, attach=True):
        self.log = log or (lambda m: None)
        self.port, self.ctl_port, self.attach = port, ctl_port, attach
        self.mic = None                            # Client（Feeder 的 sink）
        self.helper_pid = None

    def _connect(self, wait=0.0):
        end = time.monotonic() + wait
        while True:
            try:
                c = socket.create_connection(("127.0.0.1", self.ctl_port), timeout=3)
                c.settimeout(10)
                return Client(c)
            except OSError:
                if time.monotonic() >= end:
                    return None
                time.sleep(0.2)

    def _spawn(self):
        args = [sys.executable, os.path.abspath(__file__), "--serve", "--port", str(self.port), "--ctl-port", str(self.ctl_port)]
        if not self.attach:
            args.append("--no-attach")
        log = os.path.join(tempfile.gettempdir(), f"jtlw-vmic-{self.port}.log")
        kw = {"stdin": subprocess.DEVNULL, "stdout": open(log, "wb"), "stderr": subprocess.STDOUT, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000200 | 0x08000000      # CREATE_NEW_PROCESS_GROUP（主程式的 Ctrl+C 不會打到它）、CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        return subprocess.Popen(args, **kw)

    def start(self, timeout=20):
        """回傳錯誤說明或 None"""
        if self.attach and not usbip_installed():
            return "沒有安裝 usbip-win2"
        cl = self._connect()
        if cl:
            try:
                st = cl.status()
                if st.get("sha") != _self_sha():          # 別的版本留下的：叫它結束，換自己的
                    cl.quit()
                    cl.c.close()
                    cl = None
                    for _ in range(50):                    # 等它真的結束（控制埠不再接受連線）
                        x = self._connect()
                        if x is None:
                            break
                        x.c.close()
                        time.sleep(0.2)
            except (OSError, ValueError, struct.error):
                cl = None
        if cl is None:
            p = self._spawn()
            cl = self._connect(wait=15)
            if cl is None:
                return f"口譯麥克風小程式沒有啟動（{'結束碼 ' + str(p.poll()) if p.poll() is not None else '連不上'}）"
        self.mic = cl
        end = time.monotonic() + timeout
        st = {}
        while time.monotonic() < end:
            try:
                st = cl.status()
            except (OSError, ValueError, struct.error) as e:
                return f"口譯麥克風小程式沒有回應（{type(e).__name__}）"
            if st.get("error"):
                self.close()
                return st["error"]
            if st.get("connected"):
                self.helper_pid = st.get("pid")
                return None
            time.sleep(0.3)
        self.close()
        return _with_sac_hint(f"Windows 在 {timeout} 秒內沒有接上口譯麥克風（usbip-win2 的驅動可能沒有裝好，或需要重新開機）")

    def status(self):
        try:
            return self.mic.status() if self.mic else {}
        except (OSError, ValueError, struct.error):
            return {}

    def close(self, keep=False):
        if not self.mic:
            return
        try:
            if not keep:
                self.mic.quit()
        except (OSError, struct.error):
            pass
        finally:
            try:
                self.mic.c.close()
            except OSError:
                pass
            self.mic = None


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="jt-live-whisper 口譯麥克風小程式（由主程式啟動）")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--ctl-port", type=int, default=CTL_PORT)
    ap.add_argument("--idle", type=float, default=IDLE_SEC)
    ap.add_argument("--no-attach", action="store_true", help="不叫 usbip-win2（測試用：由測試自己當驅動連上來）")
    a = ap.parse_args(argv)
    if not a.serve:
        ap.print_help()
        return 2

    def log(m):
        print(time.strftime("%H:%M:%S"), m, flush=True)
    return Helper(a.port, a.ctl_port, a.idle, attach=not a.no_attach, log=log).run()


if __name__ == "__main__":
    sys.exit(_main())
