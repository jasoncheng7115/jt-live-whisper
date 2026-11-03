"""雙向語音口譯（v2.28.0）：把雙向模式的譯文念出來。規格：specs/2026-10-09_雙向語音口譯規格_v0.1.md

對方（系統音訊那一路）說的翻成中文 → 念給我聽（耳機）；我（麥克風那一路）說的翻成英文 → 念給對方聽（虛擬麥克風）。

最要緊的三件事：
- 回授：念出來的聲音被自己的擷取錄回去、又翻一次（我耳機的中文被「系統音訊」錄到、喇叭漏進麥克風、對方沒有回音消除）。
  EchoGuard 記住最近 20 秒念過的句子（譯文與原文都記：中文被當成英文辨識時，Whisper 會吐出像原文的英文），辨識結果像它就丟掉
- 落後：一次只合成一句（GPU 的合成程式本來就一次一段），給對方的優先（對方在等你回答）；
  同一個方向排了 2 句以上就用 1.2 倍語速，等太久（15 秒）的不念，字幕照樣顯示
- 兩個方向各自播放（不同裝置），我聽中文的同時對方可以聽英文

合成、播放都由呼叫端給（synth、play），這裡只管排程，測試可以換成假的
"""
import collections
import difflib
import itertools
import queue
import re
import threading
import time

ME, THEM = "me", "them"            # 念給我聽、念給對方聽
LANES = (THEM, ME)                 # 合成的優先順序
ECHO_WINDOW = 20.0                 # 秒：多久以前念過的還算
ECHO_RATIO = 0.6                   # 相似度門檻（與既有的去重同一個值）
ECHO_PART = 6                      # 只辨識到一段時：至少這麼長、而且整段在念過的句子裡才算
ECHO_TAIL = 8.0                    # 念給我聽的期間＋之後這麼久，系統音訊那一路用寬鬆的判斷（辨識緩衝＋延遲）
ECHO_LOOSE = 0.4                   # 寬鬆：相似度、英文字重疊比例
ECHO_SHORT = 3                     # 寬鬆：這麼短的英文（Thank you.）也當成回授
STALE_SEC = 15.0                   # 等這麼久還沒開始合成就不念了
BACKLOG = 2                        # 同一個方向排了這麼多句（不含正在念的）就加速
FAST_RATE = 1.2
DOWN_SEC = 30.0                    # GPU 伺服器連不上（OSError：拒絕連線、逾時、斷線）之後這麼久不再試，句子直接標失敗、不排隊等逾時
INTRO_EN = "Hi, I'm using an AI interpreter, so there will be a short delay."

_KEEP = re.compile(r"[0-9a-z぀-ヿ㐀-鿿가-힯]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def norm(text):
    """比對用：小寫、只留字母數字與中日韓文字（標點、空白、全半形差異都不算）"""
    return "".join(_KEEP.findall((text or "").lower()))


def continues(prev, new):
    """new 是接著 prev 講下去的（滑動視窗）：開頭就是整個 prev，或開頭是 prev 的結尾（至少 ECHO_PART 個字）、後面還有新的字。
    這是對方還在講，不是回授（2026-10-10 審查：線上會議 5 秒窗、3 秒步進時，「上一窗的尾巴＋新的字」在寬鬆判斷下被丟，對方 19% 的字沒顯示）"""
    if len(new) > len(prev) and new.startswith(prev):
        return True
    for k in range(min(len(prev), len(new) - 4), ECHO_PART - 1, -1):
        if prev.endswith(new[:k]):
            return True
    return False


class EchoGuard:
    """最近念過的句子。is_echo：辨識出來的句子像其中一句（相似度 ≥ 0.6，或是其中夠長的一段）就是回授。
    念給我聽的中文會被「系統音訊」錄到（同一個播放裝置），那一路固定用英文辨識，Whisper 會把它變成意思相近的英文、
    跟原文不一定 60% 相同 → 正在念給我聽（以及之後 8 秒）時，系統音訊那一路改用寬鬆的判斷（during＝ME）：
    相似度或英文字重疊 ≥ 0.4、或是 3 個字以內的短句（Thank you.）都當成回授。麥克風那一路照舊嚴格（戴耳機時聽不到念給對方的）"""

    def __init__(self, window=ECHO_WINDOW, ratio=ECHO_RATIO, clock=time.monotonic):
        self.window, self.ratio, self.clock = window, ratio, clock
        self._items = collections.deque()
        self._lock = threading.Lock()
        self._raw = collections.deque(maxlen=40)     # 原文（小寫），英文字重疊用
        self._busy = {}                              # lane → 正在念的句數
        self._last = {}                              # lane → 最後一次念完（或還在念）的時間

    def playing(self, lane, on):
        with self._lock:
            n = self._busy.get(lane, 0) + (1 if on else -1)
            self._busy[lane] = max(0, n)
            self._last[lane] = self.clock()

    def active(self, lane):
        with self._lock:
            return self._busy.get(lane, 0) > 0 or self.clock() - self._last.get(lane, -1e9) <= ECHO_TAIL

    def busy(self, lane):
        with self._lock:
            return self._busy.get(lane, 0) > 0

    def played_within(self, lane, secs):
        """正在念，或 secs 秒內念過（系統音訊這一段錄音可能錄到念給我聽的中文）"""
        with self._lock:
            return self._busy.get(lane, 0) > 0 or self.clock() - self._last.get(lane, -1e9) <= secs

    def note(self, said, src=""):
        """said：念出來的句子；src：它的原文（對方或自己說的）"""
        now = self.clock()
        with self._lock:
            for t, kind in ((said, "said"), (src, "src")):
                n = norm(t)
                if len(n) >= 2:
                    self._items.append((now, n, kind))
                    self._raw.append((now, (t or "").lower(), n, kind))

    def is_echo(self, text, during=None):
        """during：這一路在誰念的時候用寬鬆判斷（系統音訊那一路傳 ME）"""
        n = norm(text)
        if len(n) < 2:
            return False
        now = self.clock()
        with self._lock:
            while self._items and now - self._items[0][0] > self.window:
                self._items.popleft()
            recent = [(x, k) for _, x, k in self._items]
        loose = during is not None and self.active(during)
        words = set(re.findall(r"[a-z]+", (text or "").lower()))
        # 很短的英文只在「正在念給我聽」時才當成回授（之後的 8 秒不算：對方的「Yes.」「OK.」不可以被吃掉）
        if (during is not None and self.busy(during) and 0 < len(re.findall(r"[a-z]+", (text or "").lower())) <= ECHO_SHORT
                and not re.search(r"[\u3400-\u9fff]", text)):
            return True
        if loose and re.search(r"[\u3400-\u9fff]", text or ""):
            return True                 # 系統音訊那一路固定用英文辨識，還出現中文字＝念給我聽的中文被錄回去（2026-10-10 e2e：「This is a作品.」）
        for p, kind in recent:
            if kind == "src" and continues(p, n):
                continue                # 對方還在講：滑動視窗先辨識到一段（已經翻了），這次是接下去的，不是回授（2026-10-10 e2e：It's a → It's a pig.）
            if n == p:
                return True
            short, long_ = (n, p) if len(n) <= len(p) else (p, n)
            if len(short) >= ECHO_PART and short in long_:
                return True
            r = difflib.SequenceMatcher(None, n, p).ratio()
            if r >= self.ratio or (loose and r >= ECHO_LOOSE):
                return True
        if loose and words:
            for p, pn, kind in self._recent_words():
                if kind == "src" and continues(pn, n):
                    continue                # 對方接著講（同上）
                if p and len(words & p) / max(1, min(len(words), len(p))) >= ECHO_LOOSE + 0.2 and len(words & p) >= 3:
                    return True
        return False

    def _recent_words(self):
        """最近 window 秒念過的（以前沒有時間限制：5 分鐘前念過的句子也拿來比，對方的話被當回授丟掉）"""
        now = self.clock()
        with self._lock:
            return [(set(re.findall(r"[a-z]+", t)), pn, kind) for ts, t, pn, kind in self._raw if now - ts <= self.window]


def est_seconds(text, lang):
    """一句話念出來大約幾秒（還沒合成前估，用來決定串流要先存多少）。
    2026-10-09 GPU 實測：中文約每字 0.19～0.23 秒、英文約每個字元 0.07～0.09 秒"""
    if lang == "en":
        return len(text) * 0.08
    cjk = sum(1 for c in text if "㐀" <= c <= "鿿")
    return cjk * 0.22 + (len(text) - cjk) * 0.06


class Pace:
    """最近的合成速度（合成耗時 ÷ 音訊長度，指數平均）。GPU 跟別的工作共用（2026-10-09 深夜 SOCTalk 在跑時
    約 1.5～1.7，平常約 0.9）：比說話慢的時候，串流要先存一段再播，不然播到一半會斷"""

    def __init__(self, rtf=1.0, alpha=0.3):
        self.rtf, self.alpha = rtf, alpha
        self._lock = threading.Lock()

    def update(self, secs, dur):
        if dur > 0.2:
            with self._lock:
                self.rtf = (1 - self.alpha) * self.rtf + self.alpha * (secs / dur)

    def prebuffer(self, est, margin=0.25):
        """要先存幾秒：合成比說話慢時，先存 est×(1−1/速度)，播放才追不上合成"""
        r = self.rtf
        return margin + (est * (1 - 1 / r) if r > 1 else 0.0)


def buffered(chunks, need, pace, clock=time.monotonic):
    """串流合成的 (pcm, sr) 先存到 need 秒才交出去（第一段），之後收到就交；結束時回報合成速度給 pace"""
    t0 = clock()
    buf, have, total, sr, released = [], 0.0, 0.0, None, False
    for pcm, sr in chunks:
        sec = len(pcm) / 2 / sr
        total += sec
        if released:
            yield pcm, sr
            continue
        buf.append(pcm)
        have += sec
        if have >= need:
            yield b"".join(buf), sr
            buf, released = [], True
    if buf:
        yield b"".join(buf), sr
    if total:
        pace.update(clock() - t0, total)


def after_prefix(old, new):
    """new 的開頭就是 old（只看字母數字與中日韓文字）時，回傳 new 去掉那段之後剩下的原文；不是的話回 None。
    辨識的滑動視窗常先給一小段（Hello everyone）、下一次給整句（Hello everyone, welcome to ...）"""
    o, n = norm(old), norm(new)
    if not o or len(n) <= len(o) or not n.startswith(o):
        return None
    k = 0
    for i, ch in enumerate(new):
        if _KEEP.fullmatch(ch.lower()):
            k += 1
            if k == len(o):
                return new[i + 1:].lstrip(" ,，、.。;；:：!！?？-—")
    return None


def finishes(old, new):
    """new 是 old 說完的版本（看原文）：滑動視窗先辨識到前半句、下一次拿到整句。開頭跟 old 一樣（70% 以上，
    或只有最後 2 個中日韓字／4 個字母不同：前半句的結尾常被切在字中間而聽錯，「沒問題，我心情」→「沒問題，我星期五之前…」），
    至少一半相同、而且比 old 長。webui.html 的 _mergeTarget（整句取代半句的泡泡）用同一條規則"""
    o, n = norm(old), norm(new)
    if len(o) < 2 or len(n) <= len(o):
        return False
    k = 0
    while k < len(o) and o[k] == n[k]:
        k += 1
    tail = 2 if _CJK.search(o) else 4
    return k >= 2 and k * 2 >= len(o) and (k * 10 >= len(o) * 7 or k >= len(o) - tail)


class Item:
    __slots__ = ("id", "lane", "text", "src", "created", "state")

    def __init__(self, id_, lane, text, src, created):
        self.id, self.lane, self.text, self.src, self.created = id_, lane, text, src, created
        self.state = "queued"


class Interpreter:
    """lanes：{ME: {"rate": 1.0}, THEM: {"rate": 1.0}}，只放有開的方向。
    synth(item, rate) → (pcm, sr)，或逐段產生 (pcm, sr) 的 generator（串流合成：第一段出來就開始播）。
    play(lane, pcm, sr) 播一段（阻塞到播完）。on_event({"type": "interp", "id", "lane", "state", "text", ...})：
    state＝queued／speaking／done／skipped（太久沒念到）／canceled／muted／failed／replaced（被後來辨識到的整句取代）"""

    def __init__(self, lanes, synth, play, guard=None, on_event=None, clock=time.monotonic,
                 stale=STALE_SEC, backlog=BACKLOG, fast_rate=FAST_RATE, down_sec=DOWN_SEC):
        self.lanes = {k: dict(v) for k, v in lanes.items() if k in LANES}
        self.synth, self.play = synth, play
        self.guard = guard or EchoGuard(clock=clock)
        self.on_event = on_event or (lambda e: None)
        self.clock, self.stale, self.backlog, self.fast_rate = clock, stale, backlog, fast_rate
        self.down_sec, self._down_until, self._down_err = down_sec, None, ""
        self._ids = itertools.count(1)
        self._q = {k: collections.deque() for k in self.lanes}
        self._items = {}
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._play_q = {k: queue.Queue() for k in self.lanes}
        self._threads = []
        self._last_spoken = {}                  # lane → (最後排進去的完整一句, 時間)：後面辨識到整句時只念多出來的
        self.window_merge = 10.0

    # ── 給呼叫端 ──
    def start(self):
        self._threads = [threading.Thread(target=self._synth_loop, name="interp-synth", daemon=True)]
        self._threads += [threading.Thread(target=self._play_loop, args=(k,), name=f"interp-play-{k}", daemon=True)
                          for k in self.lanes]
        for t in self._threads:
            t.start()
        return self

    def speak(self, lane, text, src=""):
        """排一句。回傳編號（取消用）；這個方向沒開或靜音時回 None"""
        text = (text or "").strip()
        if lane not in self.lanes or not text:
            return None
        self.guard.note(text, src)
        if self.lanes[lane].get("muted"):
            item = Item(next(self._ids), lane, text, src, self.clock())   # 不念，但給編號：字幕那一句標「靜音」
            with self._cv:
                self._items[item.id] = item
            self._emit(item, "muted")
            return item.id
        replaced, full = [], text
        with self._cv:
            # 同一句話先辨識到一小段、再辨識到整句：還沒念的那一小段換成整句；已經念了的，整句只念後面多出來的。
            # 譯文不一定是前後接得起來的（「目前正在」→ Currently being；整句 → The final testing is ...），所以也看原文（finishes）
            for old in list(self._q[lane]):
                if old.state == "queued" and (after_prefix(old.text, text) is not None or norm(old.text) == norm(text)
                                              or (old.src and src and finishes(old.src, src))):
                    self._q[lane].remove(old)
                    old.state = "replaced"
                    replaced.append(old)
            if not replaced:
                prev = self._last_spoken.get(lane)
                if prev is not None and self.clock() - prev[1] <= self.window_merge:
                    if norm(prev[0]) == norm(text):
                        return None                 # 一模一樣（滑動視窗又辨識一次）：不再念
                    rest = after_prefix(prev[0], text)
                    if rest is not None:
                        if not norm(rest):
                            return None
                        text = rest
            item = Item(next(self._ids), lane, text, src, self.clock())
            self._items[item.id] = item
            self._q[lane].append(item)
            self._last_spoken[lane] = (full, self.clock())
            self._cv.notify_all()
        for old in replaced:
            self._emit(old, "replaced")
        self._emit(item, "queued")
        return item.id

    def cancel(self, item_id):
        """還沒開始念的才能取消：排隊中、合成中、合成好等著念的（念到一半的不切掉：半句話比整句更容易讓對方誤會）。
        合成中或已經在播放佇列裡的，播放前會看到 canceled 就跳過；開始念與取消用同一把鎖，不會取消成功之後又念出來"""
        with self._cv:
            item = self._items.get(item_id)
            if item is None or item.state not in ("queued", "synth"):
                return False
            if item.state == "queued":
                try:
                    self._q[item.lane].remove(item)
                except ValueError:
                    pass
            item.state = "canceled"
        self._emit(item, "canceled")
        return True

    def set_rate(self, lane, rate):
        """口譯中改語速（WebUI 字幕頁底部的 − ＋，2026-10-12 使用者：「已經雙向口譯可以改語速嗎」）：
        下一句開始生效（正在念的那句不變）；限 0.5～2 倍（ffmpeg atempo 單一濾鏡的範圍）。回傳實際設定的值"""
        if lane not in self.lanes:
            return None
        r = round(min(2.0, max(0.5, float(rate))), 2)
        self.lanes[lane]["rate"] = r
        return r

    def mute(self, lane, on=True):
        """只停語音，字幕照舊；靜音時排著的也不念了"""
        if lane not in self.lanes:
            return
        self.lanes[lane]["muted"] = bool(on)
        if on:
            with self._cv:
                dropped = list(self._q[lane])
                self._q[lane].clear()
                for it in dropped:
                    it.state = "muted"
            for it in dropped:
                self._emit(it, "muted")

    def pending(self, lane):
        with self._cv:
            return len(self._q.get(lane, ()))

    def wait_stop(self, timeout):
        """等到 stop() 或逾時；停了回 True（背景監看用）"""
        return self._stop.wait(timeout)

    def stop(self, wait=2.0):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        for q in self._play_q.values():
            q.put(None)
        for t in self._threads:
            t.join(timeout=wait)

    # ── 內部 ──
    def _emit(self, item, state, **extra):
        item.state = state
        try:
            self.on_event(dict({"type": "interp", "id": item.id, "lane": item.lane, "state": state,
                                "text": item.text}, **extra))
        except Exception:
            pass

    def _next(self):
        """給對方的優先。回傳 (item, 這個方向還排著幾句)"""
        for lane in LANES:
            q = self._q.get(lane)
            if q:
                item = q.popleft()
                return item, len(q)
        return None, 0

    def _synth_loop(self):
        while not self._stop.is_set():
            with self._cv:
                item, left = self._next()
                while item is None and not self._stop.is_set():
                    self._cv.wait(0.5)
                    item, left = self._next()
                if item is None:
                    return
                item.state = "synth"
            if self.clock() - item.created > self.stale:
                self._emit(item, "skipped", reason="stale")
                continue
            if self._down_until is not None and self.clock() < self._down_until:
                self._emit(item, "failed", error=self._down_err, reason="down")
                continue
            rate = float(self.lanes[item.lane].get("rate") or 1.0)
            if left >= self.backlog:
                rate = max(rate, self.fast_rate)
            try:
                out = self.synth(item, rate)
                if isinstance(out, tuple):
                    self._play_q[item.lane].put((item, out, True))
                else:                       # 串流：一段一段交給播放
                    for chunk in out:
                        if self._stop.is_set() or item.state in ("canceled", "muted"):
                            break
                        self._play_q[item.lane].put((item, chunk, False))
                    self._play_q[item.lane].put((item, None, True))
            except Exception as e:
                if self._stop.is_set():             # 結束時裝置先被移除、連線被切：不是錯誤
                    return
                err = f"{type(e).__name__}: {e}"[:200]
                if isinstance(e, OSError):          # 連不上（不是伺服器回錯誤）：先停 down_sec 秒，後面的句子不用每句等逾時
                    self._down_until = self.clock() + self.down_sec
                    self._down_err = "GPU 伺服器連不上：" + err
                    self._emit(item, "failed", error=self._down_err, reason="down")
                else:
                    self._emit(item, "failed", error=err)
                # 串流念到一半才失敗時，播放那邊已經算它「正在念」：送結尾讓它減回去（否則 busy 一直是真的，
                # 系統音訊那一路永遠用寬鬆判斷、對方的短句都被當回授丟掉，原聲也一直被調小）
                self._play_q[item.lane].put((item, None, True))

    def _play_loop(self, lane):
        q = self._play_q[lane]
        cur = None                  # 正在念、已經算進 guard.playing 的那一句：每一條結束的路都要減回去

        def off():
            nonlocal cur
            if cur is not None:
                self.guard.playing(lane, False)
                cur = None
        while not self._stop.is_set():
            msg = q.get()
            if msg is None:
                break
            item, chunk, last = msg
            with self._cv:          # 跟 cancel 同一把鎖
                dead = item.state in ("canceled", "muted", "failed") or bool(self.lanes[lane].get("muted"))
                start = not dead and item.state != "speaking"
                if start:
                    item.state = "speaking"
            if dead:
                if cur == item.id:
                    off()
                if item.state not in ("canceled", "muted", "failed"):
                    self._emit(item, "muted")
                continue
            if start:
                off()
                self.guard.note(item.text, item.src)      # 從開始念起算回授的時間窗
                self.guard.playing(lane, True)
                cur = item.id
                self._emit(item, "speaking")
            if chunk is not None:
                try:
                    self.play(lane, *chunk)
                except Exception as e:
                    off()
                    if self._stop.is_set():
                        break
                    self._emit(item, "failed", error=f"{type(e).__name__}: {e}"[:200], reason="play")
                    continue
            if last:
                off()
                self._emit(item, "done")
        off()
