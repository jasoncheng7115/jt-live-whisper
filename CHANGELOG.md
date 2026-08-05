# Changelog

### v2.18.2 (2026-08-05)

**修正 — macOS 安裝時 whisper.cpp 編譯失敗：`No rule to make target 'whisper-stream'`**（[issue #2](https://github.com/jasoncheng7115/jt-live-whisper/issues/2)，感謝 @jackylinuxcom 回報並提供修正方向）
- 症狀：新版 macOS 執行 `./install.sh` 時，whisper.cpp 編譯階段報 `make: *** No rule to make target 'whisper-stream'. Stop.`
- 根因：**Homebrew 已把 `sdl2` 變成 `sdl2-compat` 的別名**（SDL2 API 跑在 SDL3 之上），`brew list --formula` 只會顯示 `sdl2-compat`。安裝腳本用 `grep "^sdl2$"` 比對 formula 名稱因此比不到，落到 SDL3 分支
- 一併修掉同一段落的另外兩個問題：
  - **whisper.cpp 根本沒有 `WHISPER_SDL3` 這個選項**（只有 `WHISPER_SDL2`）。傳入未知選項時 cmake 不會報錯，只會安靜地建出沒有 `whisper-stream` 的版本，直到 `--target whisper-stream` 才爆錯——這就是錯誤訊息看起來與 SDL 無關的原因
  - **Metal 加速旗標被綁在「`/opt/homebrew/Cellar/sdl2` 目錄是否存在」條件下**。改用 sdl2-compat 後該目錄不存在，整組 `-DWHISPER_METAL=ON -DGGML_CPU_ARM_ARCH=...` 會被跳過，就算編譯成功也是沒有 Metal 加速的版本
- 修法：偵測改為**能力導向**而非比對 formula 名稱——依序檢查 `pkg-config --exists sdl2`、各 Homebrew prefix 下的 `lib/cmake/SDL2/sdl2-config.cmake` 與 `lib/pkgconfig/sdl2.pc`，最後才回退到 formula 名稱（同時接受 `sdl2` 與 `sdl2-compat`）。如此不論 Homebrew 日後怎麼改名都能正確偵測
- 編譯旗標一律使用 `-DWHISPER_SDL2=ON`；Metal 旗標不再依賴 SDL 目錄是否存在
- 實測：`sdl2-compat` 提供完整 SDL2 headers、`sdl2.pc` 與 `lib/cmake/SDL2/`，`find_package(SDL2)` 可正常找到（回報 SDL2_FOUND=1、version 2.32.70），`-DWHISPER_SDL2=ON` 能正確建出 arm64 的 `whisper-stream`


### v2.18.1 (2026-07-29)

**修正 — LLM 翻譯偶爾輸出簡體字時沒有任何機制攔截**
- ASR 辨識結果一直有經過 OpenCC `s2twp` 轉換，但 **LLM 翻譯結果完全跳過**，靠 prompt 控制繁體。模型偶爾仍會吐簡體（實測雙向模式出現「我们的存储集群现在处理大约40」），出現後就直接顯示給使用者
- 原本跳過的理由是「S2TWP 會誤轉如『干擾→幹擾』」——這個顧慮是對的，但只發生在**輸入已經是繁體**的時候；對真正的簡體輸入，`s2twp("干扰")` 會正確轉成「干擾」
- 修法：新增 `_to_traditional()`，**偵測到簡體才轉換**
  - 已是繁體 → 原樣返回，不會把正確的「干擾」弄壞
  - 確認是簡體 → 套 `s2twp`，同時取得台灣用語轉換（内存→記憶體、程序→程式、视频→影片）
- 偵測用整段「繁→簡」往返比對，而非逐字判斷：逐字會把「干」「后」「里」這類簡繁共用字誤判成簡體
- 套用於 5 處 LLM 翻譯輸出；非 LLM 引擎（NLLB / Argos）維持原本無條件轉換的行為

**文件 — 拆開「AI 模型」與「推論引擎」**
- 原本的模型表把 `whisper.cpp` / `faster-whisper` / `mlx-whisper` 當成模型列出，但它們是**推論引擎**；而 Breeze-ASR-26 是模型，且正是跑在 faster-whisper 與 mlx-whisper 上，混在同一張表裡語意不通
- 模型表改列 Whisper / Breeze-ASR-26 / Moonshine；另立一張引擎表說明各引擎的用途與可跑的模型（README 與專案網站同步調整）


### v2.18.0 (2026-07-29)

**新增 — 台語（台灣閩南語）語音辨識**
- 新增兩個模式：`nan` 台語轉錄、`nan2en` 台翻英字幕
- 採用 **MediaTek Breeze-ASR-26**（Whisper large-v2 的台語微調版，Apache-2.0），辨識結果**直接輸出漢字**，不需另外翻譯
- 即時模式與離線 `--input` 皆支援；macOS Apple Silicon 走 mlx-whisper GPU，其餘平台走 faster-whisper
- 模型依平台自動選用社群預先轉檔版本，不需使用者自行轉換：
  | 平台 | 模型 | 大小 |
  |------|------|------|
  | Apple Silicon | `doggy8088/Breeze-ASR-26-MLX-4bit` | 877 MB |
  | CPU | `WizardForest/faster-whisper-Breeze-ASR-26-int8` | 1.56 GB |
  | NVIDIA GPU | `paulpengtw/faster-whisper-Breeze-ASR-26` | 3.09 GB |
- 台語模式會自動鎖定此模型，使用 `-m` 指定其他模型時會提示並忽略

**實測辨識品質**（連續台語語音 20 句 / 共 148 秒，參考文字為華語漢字）
| 模型 | 平均 CER |
|------|---------|
| Breeze-ASR-26 | **33.94%** |
| 原有 large-v3-turbo | 1033%（重複幻覺爆走，完全不可用） |

實際輸出範例（原有模型對照）：
- 參考「李有志 不義之財不可得 不倫之愛不可行」→ Breeze 一字不差；large-v3-turbo 吐出「理由子ŻŻŻŻŻ…」
- 參考「烏白試」→ Breeze「亂試」（語意正確）；large-v3-turbo「歐北七」（純音譯）

> CER 偏高主要來自書寫系統差異：台語音訊的參考文字有台語漢字與華語漢字兩種寫法，
> 本模型輸出華語漢字，語意正確但用字未必與參考一致（例如參考「拚不贏你」對上「鬥不過你」）。

- `nan2en` 的翻譯走與中翻英相同的路徑（台語辨識結果本身即為漢字），LLM 與 NLLB 兩種引擎都已對應

**技術重點 — 本模型的三個特殊限制**
- **必須指定語言為 `en`**：微調時沿用了 Whisper 的 `<|en|>` 語言 token（見模型 config.json 的 `forced_decoder_ids`）。自動偵測會得到 `ha`／`so`／`ps` 等無關語言，品質明顯下降
- **不能沿用專案既有的防幻覺參數組**：該參數組讓同一批音檔的 CER 從 17.99% 惡化到 56.42%、且慢 4.7 倍。原因是微調後 logprob／no_speech 分布與原版 Whisper 不同，門檻持續誤判使 temperature fallback 階梯每句都跑完。台語改用專屬的精簡參數 `_FW_NAN_KW`
- **不產生時間戳**：訓練時被 forced `<|notimestamps|>`，直接整檔辨識只會得到「每 30 秒一段」且結束時間錯誤的結果（實測第二段 25 秒的文字被標成 0.48 秒），SRT／VTT／時間逐字稿全部不可用。改由用戶端 `_nan_vad_windows()` 以語音活動偵測切成不超過 28 秒的視窗，時間戳取自切段邊界，視窗總數與音訊長度成正比，辨識成本與一般模型相當

**效能與使用建議**
| 執行方式 | 速度（相對即時） |
|----------|----------------|
| Apple Silicon mlx 4bit | 約 1.3 倍即時 |
| CPU（CTranslate2 int8） | 約 0.24 倍即時 ⚠️ 1 小時音訊需約 4 小時 |

- 本模型是 large-v2 微調（32 層 decoder，約為 large-v3-turbo 的 8 倍），先天較慢
- 即時模式的步進自動拉高到 6 秒下限，避免辨識跟不上導致字幕越拖越慢
- 台語離線處理固定走本機：GPU 伺服器套用的是一般模型的參數組，對本模型反而大幅劣化


### v2.17.0 (2026-07-29)

**新增 — macOS 改用 ScreenCaptureKit 擷取系統音訊，不必再裝 BlackHole**
- macOS 13 以上改以 ScreenCaptureKit（SCK）直接向系統借音訊，成為預設的系統音訊來源
- 使用者**不需要**再安裝 BlackHole 虛擬音訊驅動、不需要建立「多重輸出裝置」、不需要把 Zoom / Teams 的喇叭改指到虛擬裝置，也不必為了裝驅動重開機
- 音訊照常從原本的喇叭或耳機播放，不受影響；SCK 只是額外複製一份給辨識程式
- 實作為獨立的 Swift 元件 `sck_audio_capture.swift`（編譯為 `bin/jt-sck-audio`），不引入 pyobjc；`install.sh` 會自動編譯，原始碼未變更時直接沿用既有二進位檔
- macOS 12 以下、未授權「螢幕錄製」、或指定 `--audio-source blackhole` 時，自動沿用原本的 BlackHole 流程，既有使用者的設定完全不受影響

**新增 — 「螢幕錄製」權限引導**
- SCK 即使只取音訊，macOS 仍將其歸類在「螢幕錄製」權限之下（macOS 15 顯示為「螢幕與系統音訊錄製」），且授權對象是**啟動本程式的終端機程式**（Terminal / iTerm2 / Ghostty / VS Code…），不是 Python
- 程式會自動判讀 `TERM_PROGRAM` 並在提示中**指名該勾選哪一個程式**，避免使用者在設定頁裡找不到對象
- 終端機互動時直接詢問「現在開啟授權對話框？」，同意即跳出系統授權視窗；若先前按過拒絕（macOS 不會再跳），則自動開啟「系統設定 → 隱私權與安全性 → 螢幕錄製」頁面
- 授權後 macOS 要求該終端機程式完全結束（Cmd+Q）再重新開啟才會生效，提示訊息會明確說明
- 隨時可用 `./start.sh --sck-permission` 重新授權；WebUI 設定頁同樣提供授權按鈕並指名授權對象
- 已安裝 BlackHole 但尚未授權時，過去會靜默降級，現在會明確告知 SCK 未啟用與啟用方式

**新增 — 單向即時本機辨識支援 mlx-whisper GPU 加速**
- 過去 macOS 單機單向即時辨識預設走 whisper.cpp，而 whisper.cpp 的 `whisper-stream` 只能讀 SDL2 音訊裝置、餵不進 SCK
- 因此將雙向模式既有的 mlx-whisper GPU 路徑一併導入單向即時辨識，SCK 音源全部走 Python 端辨識
- `--asr faster-whisper` 時尊重使用者選擇；`.en` 系列模型因無對應 mlx-community repo，仍走 faster-whisper

**改善 — mlx-whisper 辨識不再為每段音訊啟動 ffmpeg 子程序**
- 過去傳檔案路徑給 mlx-whisper，它內部會為每一段音訊 spawn 一次 ffmpeg 解碼；即時模式每 3 秒一段，成本可觀，且 ffmpeg 一旦損壞整條即時辨識就停擺
- 改為自行用 `wave` + `scipy` 解出 16kHz 單聲道 float32 陣列後直接餵入（mlx-whisper 本來就接受陣列）
- 實測 8 秒 48kHz 音訊：解碼 **230.7ms → 9.8ms（23.5 倍）**，即時步進 3 秒下等於每輪多出 7% 的時間餘裕，雙向模式兩路則加倍
- 以 6 段真實中文會議音訊 A/B 驗證，辨識文字**完全一致**（波形相關係數 0.999997）；任何一步失敗會自動退回原本的檔案路徑走法

**修正 — 錄音裝置聲道數在重構後少了保護**
- 音訊串流建立邏輯統一為 `_capture_stream_info()` / `_open_capture_stream()` 時，錄音路徑漏掉原本的「至少 1 聲道」下限
- 混合錄音（系統音訊 + 麥克風）的系統音訊聲道數被誤套上限 2；Windows 多聲道輸出時人聲多半在中央聲道，截到 2ch 會漏掉內容，已還原為保留原始聲道數

**修正 — 收不到音訊時的提示不再一律指向 WASAPI**
- macOS 使用 SCK 時，過去會顯示「請檢查 WASAPI Loopback 裝置」，現在依實際擷取來源給對應說明
- 並補上實測行為：**系統輸出設為靜音時，ScreenCaptureKit 只會收到無聲訊號**，提示中直接點明

### v2.16.8 (2026-07-09)

**修正 — Ollama 關閉思考模式的參數放錯位置，思考模式從未真正關閉**
- 症狀：翻譯引擎選用具思考能力的模型（qwen3 / qwen3.5 / gemma4 / deepseek-r1 等）時，每句翻譯都慢到無法即時使用
- 根因：`_llm_generate()` 將 `think` 放進 `payload["options"]["think"]`，但 Ollama 的 `think` 是 `/api/generate` 的**頂層欄位**。放進 `options` 會被當成未知的模型參數靜默忽略 → 思考模式全程開啟，模型完整思考後才輸出，思考內容再被 `<think>` 正規式整段丟棄，算力全部浪費
- 影響範圍：翻譯、LLM 暖機、離線逐字稿 LLM 校正三條路徑雖然都有傳 `think=False`，但實際上**一次都沒生效**
- 實測（GPU 伺服器 Ollama 0.24.0，同一句中譯英）：
  | 寫法 | 耗時 | 產生 token | 思考長度 |
  |------|------|-----------|---------|
  | `{"think": false}`（頂層，修正後） | 1.9s | 18 | 0 |
  | `{"options": {"think": false}}`（修正前） | 64.2s | 1467 | 6021 |
  | 完全不傳 | 158.4s | 3317 | 13023 |
- 修法：`think` 改放頂層。另加保護 — 對不支援思考的模型（llama3.2 / qwen2.5 等）送 `think: true` 時 Ollama 會回 HTTP 400，攔截後移除 `think` 欄位重送一次；非 400 的錯誤（如模型不存在的 404）直接拋出，不做無謂重試
- 對 OpenAI 相容伺服器（vLLM / LM Studio / llama.cpp）無行為改變，仍走 `chat_template_kwargs`
- 各模型實測加速：qwen3.5:9b 30.0s → 0.8s（約 37 倍）、gemma4:26b 9.6s → 1.7s（約 5.6 倍）。gpt-oss 系列因架構上必定推理，`think: false` 僅降低推理強度，無明顯差異

**改善 — 會議摘要與逐字稿校正預設關閉思考模式**
- `call_ollama_raw()` 的 `think` 預設值由 `None` 改為 `False`，涵蓋全部摘要產出點
- 實測（真實 3 小時教育訓練逐字稿前 800 行，qwen3.5:9b）：**7m02s → 1m27s（約 4.8 倍）**
- 摘要品質未下降：關閉思考後條列項目由 9 項增為 10 項、涵蓋範圍更完整，且未再出現將 Northwest 誤植為「尼泊爾」之類的幻覺
- `<think>` 標籤的正規式過濾全數保留，OpenAI 相容端點仍須倚賴

### v2.16.7 (2026-06-27)

**修正 — LM Studio 被誤判成 Ollama 導致翻譯全程失敗**
- 使用者回報：將翻譯引擎設為 LM Studio（OpenAI 相容、預設 port 1234）後無法成功使用，終端噴 `[錯誤] 無法連接 Ollama: 'response'`、主程式 exit code 1、翻譯 0 筆
- 根因：LM Studio 對**未實作的 endpoint 一律回 HTTP 200**（其 Developer Logs 會印 `Unexpected endpoint or method. Returning 200 anyway`）。而 `_detect_llm_server()` 偵測伺服器類型時，先打 Ollama 的 `/api/tags` 且**只看有沒有 200、未驗證回傳內容** → LM Studio 回了 200（非 Ollama 格式的空 body）就被誤判成 Ollama → 之後改走 Ollama 的 `POST /api/generate`，LM Studio 同樣回空的 200、沒有 `response` 欄位 → `_llm_generate()` 取 `result["response"]` 丟出 `KeyError: 'response'`
- 影響範圍：只要 LM Studio 在 OpenAI 模式執行，自動偵測**永遠**會誤判成 Ollama 而失敗，使用者端無法靠改設定繞過
- 修法：偵測時**驗證回傳結構**，不再只看 HTTP 200
  - `/api/tags` 回傳須為 `{"models": [...]}` 結構才採信為 Ollama
  - 否則往下試 `/v1/models`，須為 `{"data": [...]}` 結構才採信為 OpenAI 相容（LM Studio 走這條，至此正確辨識）
- `webui.py` 的 `/api/test-llm`（WebUI「測試 LLM 伺服器連線」）有同樣的誤判隱患，一併比照修正
- 對 Ollama / vLLM / llama.cpp 等原本就回正確結構的伺服器無任何行為改變

### v2.16.6 (2026-06-09)

**修正 — RTX 50 系列（Blackwell）本機辨識全程失敗**
- 客戶回報：Windows + RTX 5090 Laptop GPU（24GB）即時翻譯時，終端不斷噴 `[本機辨識失敗: cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED]`，翻譯 0 筆
- 根因：RTX 50 系列為 Blackwell 架構（compute capability sm_120），其 INT8 tensor core 需要新的 padding，CTranslate2（faster-whisper 後端）對它跑 `int8` 量化會直接噴 `CUBLAS_STATUS_NOT_SUPPORTED`；而本程式 4 處 `WhisperModel(...)` 全部寫死 `compute_type="int8"`，搭配 `device="auto"` 自動選到該 GPU → 每段辨識都失敗
- 與 PyTorch 的 `sm_120 is not compatible` 警告無關：faster-whisper 走 CTranslate2 不走 torch，該警告不影響轉錄
- 修法：新增 `_fw_local_cuda_ok()` / `_fw_device_kwargs()`，本機偵測到 CUDA → 改用 `device="cuda"` + `compute_type="float16"`（速度更快、準確度更好，VRAM 也夠）；無 CUDA 維持 `device="auto"` + `int8`（CPU 上 int8 才快）。Apple Silicon 因 CTranslate2 無 Metal 後端一律走 CPU（ASR 另用 mlx），不受影響
- 套用於 4 處本機 faster-whisper 載入點（即時單向 / 即時雙向 / 離線單向 / 離線雙向）
- 伺服器端 `remote_whisper_server.py` 早已用 float16，本次不受影響

### v2.16.5 (2026-05-05)

**緊急修正 — Windows 切換下一個檔案噴錯（v2.16.4 引發的回歸）**
- 客戶回報：v2.16.4 跑完一個檔案要切換下一個時，PowerShell 噴出 `forrtl: error (200): program aborting due to control-BREAK event`，webui.py 自己也被 CTRL_BREAK 殺掉、瀏覽器顯示「與伺服器的連線已中斷」
- 根因：v2.16.4 的 `_stop_proc()` 在 Windows 上用 `os.kill(pid, signal.CTRL_BREAK_EVENT)`，但子程序用 `start_new_session=True` 在 **Windows 是 no-op**（這個參數只在 POSIX 生效）→ 子程序與 webui.py + PowerShell **共享同一個 console process group** → CTRL_BREAK_EVENT 廣播給整組 → 全部一起死
- 修法：`subprocess.Popen` 平台分流
  - Windows：`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` 把子程序隔離成獨立 process group，CTRL_BREAK_EVENT 只會打到子程序，不會炸到 webui.py / PowerShell
  - POSIX：維持 `start_new_session=True`（脫離 controlling terminal，避免終端 SIGINT 廣播）
- 此修正使 v2.16.4 的「停止鍵 escalation」在 Windows 真正可用，而不會誤殺整個 WebUI

### v2.16.4 (2026-05-04)

**修正 — 連續離線辨識穩定性**
- 客戶回報：Windows + RTX 3060 12GB + large-v3-turbo + 5 分鐘 MP3 檔案，**第 5 個檔案後出現 native crash**（`exit code 3221226505` = `0xC0000409` = `STATUS_STACK_BUFFER_OVERRUN`）；CPU/RAM 不高、VRAM 充裕，重開 PowerShell 後正常 → 確認是 CTranslate2 / cuDNN 等 native lib 連續呼叫累積 state 導致 fast-fail
- **修法 1：每檔結束主動釋放 GPU 資源**（`_release_gpu_resources()`）
  - `process_audio_file` 與 `process_bidi_audio_files` 結尾加 `finally` 區塊
  - 本機 faster-whisper 路徑：transcribe 完成後立即 `del segments_iter, info, model` 釋放 C++ 物件
  - finally 中 `gc.collect()` + `torch.cuda.empty_cache()` + `torch.cuda.synchronize()` 強制歸還 GPU 記憶體
  - 對 mlx-whisper / CPU 路徑為 no-op（影響 0）

**修正 — 停止按鈕 escalation**
- 客戶回報：子程序 native crash 卡死時「停止」按鈕一直停在「停止中…」
- `_stop_proc()` 三段升級：graceful → SIGTERM → SIGKILL
  - Linux/macOS：SIGINT（4s）→ SIGTERM（2s）→ SIGKILL（1s）
  - Windows：CTRL_BREAK_EVENT（4s）→ SIGTERM（2s）→ SIGKILL（1s）
  - 任何例外都會落到強殺保險，確保進程一定會被清掉
- `/api/stop` 與 WebSocket `action=stop` 改用 `asyncio.to_thread(_stop_proc)`，避免阻塞 event loop（最壞 7s 不會卡住其他 API）

**修正 — WebUI 紅條誤判**
- 客戶回報：子程序 crash 時瀏覽器顯示「WebUI 伺服器已離線，請重新執行 ./start.sh --webui」紅條，但其實 webui.py 主程序還活著
- `tryBackToSetup()` 改為**重試 2 次、每次 4 秒 timeout**（原本單次 2s）→ 避開 `_stop_proc` 短暫忙碌期的誤判
- `showSetup()` 補上停止按鈕還原（離線處理 'stopped' → showSetup 路徑也會重設按鈕，不再卡「停止中…」）

### v2.16.3 (2026-04-30)

**新增**
- 離線辨識自動偵測「低音量／監視器／行車紀錄」類錄音並切換寬鬆參數，解決客戶回報「1 小時 53 分鐘錄音只辨識出 97 段、常常只抓到第一段文字」的問題
  - 偵測手法：上傳/辨識前用 `ffmpeg volumedetect` 取開頭 120 秒的 `mean_volume`（取樣已足以判斷整段特性，避免長音檔分析拖累 large-v3-turbo 本機使用者）
  - 觸發門檻：`mean_volume < -30 dBFS` → 自動套用增益 + 寬鬆參數；其餘維持 v2.16.2 標準參數**完全不變**，確保乾淨會議錄音辨識能力不受影響
  - 自動增益：用 ffmpeg `volume` 濾鏡將音量提升至 -18 dBFS（最多 +20 dB），輸出 PCM s16le 暫存檔餵 Whisper，辨識完即清理
  - 寬鬆參數組 `_FW_OFFLINE_KW_LOOSE`：`vad_filter=False`、`no_speech_threshold=0.3`、`log_prob_threshold=None`、關閉 `word_timestamps` / `hallucination_silence_threshold`（VAD 關了也用不到，順便省成本）
  - `_drop_stuck_segments()` 在寬鬆模式門檻拉到 90 秒（避免誤殺長靜默後的真實短語）
  - 套用範圍：離線單向 + 雙向（兩路獨立偵測，避免單側拖累另一側）+ 用戶端→GPU 伺服器（HTTP form 帶 `noisy=1`，伺服器 `_FW_KW_LOOSE` 對應）
  - 終端顯示一行偵測結果：`[音源分析] mean_volume=-38.2 dBFS → 寬鬆模式，增益 +20.0 dB` 方便診斷
- **效能保護**：極短/極小音檔（< 200KB）跳過分析；分析只取開頭 120 秒；增益用 PCM s16le 純樣本級處理，整體預處理開銷遠小於辨識本身

**部署**
- GPU 伺服器（192.168.1.40）需重新部署 `remote_whisper_server.py`（接收 noisy form 參數 + `_FW_KW_LOOSE` / openai-whisper 對應分支）

### v2.16.2 (2026-04-30)

**修正**
- 離線辨識長音檔 large-v3-turbo 解碼器卡死、單段橫跨數十分鐘只吐一個短詞（如「都可以」），其後出現連串 1 秒重複幻覺（如「接下來、接下來…」）：
  - 根因：faster-whisper / openai-whisper 預設 `condition_on_previous_text=True`，將上一段結果作為下一段 prompt。一旦短句卡住解碼，幻覺被自我強化形成連環污染
  - 修法 1：四處 transcribe 呼叫（`translate_meeting.py` 離線單向 / 離線雙向、`remote_whisper_server.py` faster 同步 / 串流、openai-whisper 路徑）統一改用防幻覺參數組
    - `condition_on_previous_text=False`：切斷上下文傳染
    - `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`：失敗時依序提高溫度重試
    - `repetition_penalty=1.05`：抑制連續重複
    - `word_timestamps=True` + `hallucination_silence_threshold=2.0`：偵測幻覺後跳過 ≥2s 靜音
    - `vad_parameters={"min_silence_duration_ms": 500}`：VAD 靜音偵測更靈敏
  - 修法 2：新增 `_drop_stuck_segments()` 後置過濾，duration > 30s 但文字 ≤ 6 字（中日韓）/ ≤ 4 詞（拉丁語系）的段落視為解碼器卡死直接丟棄
  - 套用於離線單向（`run_offline`）與離線雙向（`_filter_segments`）兩條流程
- 在用戶端 `_FW_OFFLINE_KW` 與伺服器端 `_FW_KW` 共用同一組參數，確保本機 / GPU 伺服器辨識行為一致
- **部署**：GPU 伺服器（192.168.1.40）需重新部署 `remote_whisper_server.py` 才能受惠

### v2.16.1 (2026-04-08)

**修正**
- 摘要與校正逐字稿移除 `<think>...</think>` 思考標籤：部分 LLM（如 Qwen3）會在回覆中夾帶思考過程，導致摘要 / 校正逐字稿混入雜訊
- 套用範圍涵蓋 5 個產出點：逐段摘要、補產重點摘要、補產校正逐字稿、多次處理之逐段結果、最終摘要輸出
- 雙向 / 麥克風並存模式 macOS PortAudio `-9986`（paInternalError）：
  - 根因：CoreAudio 若預先建立兩個 InputStream（BlackHole + 內建麥克風）後再依序 start，第二路 start 必然失敗
  - 修法 1：mic_stream 改為延後到 `lb_stream.start()` 之後才建立並啟動（與 macOS CoreAudio 設備配置流程相容）
  - 修法 2：mic_stream 啟動加入三段式 fallback —— 原始 blocksize → `blocksize=0, latency='high'` → `blocksize=4800, latency='high'`，全部失敗才報錯並提供排查指引

**效能**
- WebUI 開啟時「載入設定中…」冷啟動延遲大幅縮短（約 2.5 秒 → 接近 0 秒）
  - `_has_mlx_whisper()` 改用 `importlib.util.find_spec` 偵測，不再實際 `import mlx_whisper`（省下首次匯入 MLX 框架約 1.2 秒）
  - `_is_apple_silicon` / `_has_local_gpu` / `_has_mlx_whisper` / `_get_system_memory_gb` / `_recommended_whisper_model` 加上 `lru_cache`，避免 `/api/config` 內 10 種模式重複偵測硬體
  - `webui.py` 將 `from translate_meeting import …` 提升至模組層級，首次 `/api/config` 不再因 lazy import 付出 1.2 秒成本

### v2.16.0 (2026-03-27)

**新功能**
- 麥克風辨識支援 GPU 伺服器：系統音訊與麥克風兩路都可送 GPU 伺服器辨識，macOS / Windows 皆適用
- 麥克風辨識支援 mlx-whisper GPU 加速（Apple Silicon）：依記憶體自動選擇引擎與模型
  - 24GB+ → mlx large-v3-turbo
  - 16GB → mlx small
  - 8GB 以下 → CPU small（不啟用 mlx 避免 swap）
- 新增 `_get_system_memory_gb()` 系統記憶體偵測（macOS / Windows / Linux）
- 新增 `_recommended_mic_engine()` 麥克風引擎自動選擇（remote > mlx > cpu）
- GPU 伺服器辨識失敗時自動降級本機（mlx 或 CPU），顯示提示訊息

**改進**
- WebUI「轉錄麥克風」不再限制 GPU 伺服器模式，改為藍色提示「麥克風辨識也會送到 GPU 伺服器處理」
- 選擇 GPU 伺服器辨識時，系統音訊與麥克風兩路都走遠端（不再一路遠端一路本機）

### v2.15.5 (2026-03-25)

**改進**
- install.sh / install.ps1 faster-whisper 模型改為全部預下載（base.en、base、small.en、small、large-v3-turbo），不再依硬體選擇性下載
- install.ps1 faster-whisper 模型下載依有無 GPU 自動選擇預設模型（有 GPU → large-v3-turbo，無 GPU → small），section 標題動態顯示
- 移除 medium.en / medium 模型選項（WHISPER_MODELS、WebUI 下拉選單、SOP 說明），新增 base 多語言模型
- WebUI 模型排序更新（移除 medium 層級）

**修正**
- install.sh / install.ps1 faster-whisper 模型下載 401 失敗時自動嘗試其他 repo（mobiuslabsgmbh → Systran → deepdml），靜默切換不顯示錯誤，全部失敗才提示

### v2.15.4 (2026-03-24)

**修正**
- install.sh 補上缺少的 `check_notice()` 函式定義（升級後首次安裝報 command not found）
- install.sh / install.ps1 Argos 模型下載加入 SSL 憑證驗證失敗自動重試（企業網路相容）
- README.md 目錄結構補上 subtitle_overlay.py

### v2.15.3 (2026-03-22)

**新功能**
- 關鍵字即時通知：設定關鍵字，即時辨識出現時自動通知。可用於追蹤會議重點、開會時提醒留意關鍵議題，或線上課程摸魚時讓系統在講師說到「請實作」「這個會考」時自動提醒
  - WebUI 全螢幕警示特效（紅金交替閃爍 + 中央大字 ⚠ 脈衝動畫，遊戲風格）
  - 瀏覽器桌面推播通知（Notification API，即使在背景也看得到）
  - WebUI 音效提示，可選兩種風格：警示音（核爆風格低頻嗡鳴+高低交替警報）或柔和音（三連遞增音）
  - 懸浮字幕視窗邊框金黃色閃爍（3 次閃爍動畫）
  - 同一關鍵字冷卻機制（可設定秒數，預設 30 秒內不重複通知）
  - 不分大小寫，同時比對原文和譯文，關鍵字前後空格自動去除
  - WebUI 訊息列顯示金黃色關鍵字提醒標記
- WebUI 新增「關鍵通知」設定區塊（僅本機顯示）：關鍵字輸入、冷卻時間、通知方式開關、音效風格選擇
- 字幕轉發功能：即時字幕自動轉發到通訊平台，每隔指定秒數發送一次累積字幕
  - 支援 7 個平台：Telegram（Bot API）、Slack（Webhook）、Discord（Webhook，自動分段 2000 字）、Teams（Webhook）、LINE（Messaging API）、Nextcloud Talk（OCS API）、通用 API（自訂 URL + Body 範本）
  - 可同時啟用多個平台，各自獨立設定認證資訊
  - 發送間隔可調（最低 5 秒），段落間自動空行分隔
  - 可選擇發送內容：含時間戳 / 含原文 / 含譯文（預設原文+譯文）
  - 通用 API 支援 Body 範本：用 `{{text}}` 變數代入字幕，填 JSON 格式自動設定 Content-Type，可搭配自訂 Headers（如 Authorization）
  - 設定存於 config.json `subtitle_forward`，下次啟動自動生效
- WebUI 新增「字幕轉發」設定區塊（僅本機顯示）
  - 各平台卡片含品牌色 icon（Telegram 藍、Slack 多色、Discord 紫、Teams 紫、LINE 綠、Nextcloud 藍、通用 API 程式碼 icon）
  - 「儲存設定」+「測試發送」按鈕，測試發送會逐一驗證所有已啟用平台
- 即時模式上方狀態列新增「轉發」膠囊（青色，顯示已啟用的平台名稱如「轉發 TG+Discord」）
- 懸浮字幕功能：桌面半透明字幕覆蓋視窗（PyQt6），可疊加於任何應用程式上方（感謝 OSSLab 熊大提供建議）
  - 字體依視窗大小自動縮放，最小不低於下限，容不下則換行
  - 可拖曳定位、位置自動記憶、滑鼠穿透模式
  - 系統匣圖示右鍵選單、字幕切換淡入淡出動畫、永遠置頂
  - macOS 原生視窗層級（NSStatusWindowLevel）確保在所有視窗之上
  - 啟動時自動清除前次殘留程序，程式結束時一併終止（含 Ctrl+C / os._exit）
- WebUI 新增「懸浮字幕」設定區塊（僅本機顯示）：透明度 slider、滑鼠穿透、純轉錄單行
- 純轉錄模式（英文/中文/日文）支援 LLM 自動校正逐字稿：有設定 LLM 伺服器時自動啟用
- 離線處理摘要選項新增「只校正逐字稿（不產出摘要）」
- 離線處理可選是否產生 SRT / VTT 字幕檔（WebUI checkbox）
- WebUI 離線處理完成時顯示產出檔案連結（膠囊按鈕）+ 「開啟資料夾」按鈕
- 離線處理新增 VTT (WebVTT) 字幕檔輸出（與 SRT 同時產出）
- LLM 校正逐字稿串流即時推送：校正結果逐行送到 WebUI
- WebUI 設定頁 ↔ 字幕頁滑動淡入淡出轉場動畫
- WebUI 所有設定區塊支援收摺/展開（點按標題列）
- WebUI 載入時顯示各步驟進度文字
- install.sh / install.ps1 新增 PyQt6 必裝項目（懸浮字幕用）
- 計時器從狀態列移至頂端標題列

**改進**
- WebUI 設定頁標題改為「開始使用」（火箭 icon）
- WebUI「離線處理選項」精簡為「離線處理」
- WebUI「摘要模型」改為「摘要 / 校正模型」、「產生摘要」改為「產生摘要與校正逐字稿」
- WebUI 底部控制列垂直置中
- 摘要 prompt 要求校正逐字稿分段（每段 3-8 句）
- 摘要 HTML 超長段落自動每 5 句分段
- 懸浮字幕不顯示語言標籤（[EN] [中]），直接顯示原文和譯文
- 按「開始」時自動儲存字幕轉發、關鍵通知、懸浮字幕的啟用狀態（不需另外按儲存）
- 字型改用系統預設（避免 macOS 搜尋 Microsoft JhengHei 耗時 300ms+）
- WebUI 標題列計時器改為七段顯示器風格（LED 青色光暈 + 暗影底字 + 漸層背景），超過 1 小時自動切換 H:MM:SS 格式
- WebUI 標題列移除句數顯示（第二列已有）
- WebUI 辨識模型下拉選單智慧提示：不支援的 .en 模型自動停用、效能不足的模型標示「此裝置速度可能較慢」
- install.ps1 whisper.cpp 模型 large-v3-turbo 改為自動下載（多語言辨識必備）
- 懸浮字幕單語模式自動縮短高度（42px），動態偵測雙語切換（80px），單語時隱藏空白原文行
- 懸浮字幕支援拖拉改變視窗大小（邊緣游標自動變化），X 按鈕隨 resize 更新位置
- 懸浮字幕外框間距縮減（更緊湊）
- 字幕轉發 / 懸浮字幕 SSL 憑證驗證失敗時自動停用驗證重試（企業網路相容）

**修正**
- 懸浮字幕 crossfade 動畫堆疊：快速更新字幕時先停止舊動畫再啟動新動畫
- 懸浮字幕 flash border timer 堆疊：快速觸發關鍵字時先停止舊 timer
- WebUI 收摺動畫快速連按 race condition：動畫期間忽略重複操作
- webui.py `_monitor` thread race condition：用本地變數保留程序參照避免 AttributeError
- translate_meeting.py overlay atexit lambda 改用全域 `_overlay_proc_ref` 避免殘留程序
- webui.py 停止時一併終止懸浮字幕子程序
- webui.py Ctrl+C 強制退出（signal handler 直接 os._exit）
- install.ps1 whisper.cpp 編譯：非 CUDA 模式明確設定 `-DGGML_CUDA=OFF`
- install.ps1 whisper.cpp CUDA 編譯失敗自動降級 CPU：擴大偵測範圍
- install.ps1 CUDA Toolkit 偵測改用 nvcc.exe 驗證
- LLM 校正逐字稿翻譯配對保護：原文 [EN] 行有配對譯文時不被誤刪為雜音
- WebUI 離線處理完成時清空底部狀態列（停止 spinner 旋轉）
- 中文幻覺過濾新增「字幕by」「字幕BY」
- WebUI 第一筆字幕到達時自動清除底部「載入中」狀態

### v2.14.6 (2026-03-20)

**修正**
- install.ps1 nvidia-smi 偵測強化：新增 SysNative 路徑（修正 32-bit PowerShell 下 WoW64 目錄重定向問題）
- install.ps1 GPU 偵測 fallback：當 nvidia-smi 不在 PATH 時，透過 where.exe、驅動安裝路徑（WMI InstalledDisplayDrivers）、登錄檔（NvSmi）三種額外方式搜尋
- 修正 Developer PowerShell for VS 等非標準環境下有 NVIDIA GPU + CUDA 卻安裝 CPU 版 PyTorch 的問題

### v2.14.5 (2026-03-20)

**改進**
- WebUI 上方狀態列新增「場景」膠囊（如「訓練 8s」），離線模式不顯示
- WebUI 「錄音中」「降噪」膠囊移至底部控制列（裝置膠囊右側），上方空間更簡潔
- WebUI Header 新增「即時模式」/「離線處理」標籤（模式名稱旁）
- WebUI 底部狀態列有處理進度時自動顯示 spinner 動畫
- WebUI 多次摘要功能暫時隱藏（整合品質不穩定）

**修正**
- NLLB 模型載入失敗自動修復：偵測 config.json 缺失時自動從 HuggingFace 重新下載（新版 ctranslate2 需要此檔案）
- 摘要標題格式自動修正：LLM 輸出 `### 最終摘要` 等非標準格式時自動修正為 `## 重點摘要` / `## 校正逐字稿`
- 摘要缺少校正逐字稿時自動補發 LLM 請求（與重點摘要缺失的補發機制對齊）
- 辨識結果 0 段時終端機和 WebUI 提示可能原因（功能模式選錯、音訊靜音、品質太差）
- WebUI 離線處理模式不再顯示「錄音中」「降噪」「麥克風」膠囊
- WebUI 雙向模式切回其他模式時 GPU 伺服器選項可正確選擇（修正 `_asrLastVal` 追蹤邏輯）
- 終端機摘要狀態列視窗大小改變時不再殘影（改善 scroll region resize 邏輯）
- install.ps1 nvidia-smi 執行失敗時顯示路徑和 exit code 協助診斷



**改進**
- WebUI 按鈕加 SVG icon：開始（播放三角形）、密碼顯示/隱藏（眼睛）、儲存密碼（磁碟片）
- WebUI 所有輸入框與按鈕加 tooltip（30 個）
- install.ps1 nvidia-smi 路徑 fallback（搜尋 System32 和 NVSMI 目錄）
- install.ps1 GPU 偵測失敗時透過 WMI 診斷是否有 NVIDIA 裝置，提示更新驅動
- webui.py Ctrl+C 退出不再顯示 PyTorch atexit 錯誤（os._exit）
- README.md / SOP.md 目錄結構補齊 webui.py、webui.html 等檔案

### v2.14.4 (2026-03-19)

**新功能**
- WebUI 安全設定：唯讀密碼（遠端觀看字幕用）+ 管理密碼（遠端操作控制用），本機不需密碼
- WebUI 自訂密碼對話框（不使用瀏覽器原生 prompt），密碼欄位支援顯示/隱藏切換
- WebUI 遠端唯讀模式：不顯示設定頁，直接進字幕頁或等待畫面，隱藏停止/暫停/裝置控制
- WebUI 遠端單次密碼輸入：自動判斷角色（管理密碼→admin、唯讀密碼→read）
- WebSocket 連線驗證：遠端需 token 才能接收字幕串流

**改進**
- WebUI 未勾選「轉錄麥克風」時底部麥克風裝置膠囊顯示 disable 狀態
- WebUI 停止時顯示「正在停止」「存檔中 — WAV → MP3」等錄音轉檔進度
- WebUI 英文/中文/日文純轉錄模式（whisper.cpp 引擎）補上 `_webui_send`，修正字幕不出現問題
- WebUI 所有 API fetch 統一帶 auth token，修正唯讀模式載入卡住問題
- 中文幻覺過濾加強：字幕視聽者、音樂版權歸屬（李宗盛等，短句限定避免誤殺）
- 英文幻覺過濾加強：amara.org、subtitles by、transcribed by 等
- 日文幻覺過濾加強：字幕提供、翻訳者、Amara

### v2.14.3 (2026-03-19)

**新功能**
- WebUI 即時切換音訊裝置：點按底部裝置膠囊彈出 popup，可靜音/恢復或切換系統音訊/麥克風裝置（自動重啟子程序，約 2-3 秒中斷）
- WebUI 裝置 popup 自動刷新裝置清單（每次開啟時查詢最新裝置）
- translate_meeting.py 新增 `--mic-device ID` 參數，可指定麥克風裝置

**改進**
- WebUI 雙向模式（en_zh/ja_zh）辨識位置自動切換為本機，GPU 伺服器選項標記「不支援此模式」
- WebUI GPU 伺服器模式下「轉錄麥克風」顯示醒目橘色提示方塊，說明原因與解法
- WebUI 字幕模式切回對話模式時保留完整歷史訊息（不再清空）
- WebUI 訊息上限 500 筆，超過時頂部顯示「僅顯示最近 500 筆，完整內容已寫入逐字稿記錄檔」
- WebUI 各載入階段即時顯示進度：載入模型、連接 LLM、啟動 GPU 伺服器、等待就緒
- WebUI 波形圖更新頻率從 1 秒改為 200ms，更即時反映音量
- WebUI 狀態列格式統一：辨識「本機 large-v3-turbo」、翻譯「LLM qwen2.5:32b」
- WebUI 子程序異常退出時顯示「啟動失敗」或「異常結束」（不再統一顯示「處理已完成」）
- WebUI 子程序結束後終端機提示「按 Ctrl+C 可結束 WebUI 伺服器」
- WebUI 切換裝置完成後清除底部「正在切換...」殘留文字
- WebUI 裝置 popup 勾號改放右側，文字對齊
- ASR prompt leak 過濾新增「請使用繁體中文」
- `_webui_send` 自動清理 UTF-8 replacement character（`\ufffd`）
- install.ps1 / install.sh 升級時版本相同但缺少 webui.py/webui.html 會自動補充安裝
- install.ps1 whisper.cpp CUDA 編譯失敗時自動降級為 CPU 版
- webui.py 重構：抽出 `_build_args()` 共用函式

### v2.14.2 (2026-03-19)

**修正**
- install.ps1 本機 venv 套件清單補上 python-multipart（WebUI 檔案上傳需要，舊版安裝缺少此套件會導致 WebUI 啟動失敗）
- webui.py 啟動時自動檢查並安裝 python-multipart（免手動 pip install）

### v2.14.1 (2026-03-19)

**新功能**
- WebUI 離線處理選項：講者辨識（含人數設定）、產生摘要（含摘要模型選擇，有專屬說明）
- WebUI 離線處理各階段即時進度推送：辨識/講者辨識/輸出/LLM 校正/摘要，含模型名稱 + tokens 數 + t/s
- WebUI 講者辨識時顯示彩色 Speaker N 標籤（8 色循環，與終端機一致）
- WebUI 辨識模型依裝置 + 模式自動推薦（「此裝置適合」標籤，與終端機互動選單一致）
- WebUI 翻譯引擎依 config 自動推薦（有 LLM 伺服器預設 LLM，無則預設 NLLB）
- WebUI 防呆驗證：未選檔案、LLM 無主機/無模型、摘要無主機、重複啟動
- WebUI 載入中 spinner 效果（API 回來前不顯示空白設定卡片）
- start.ps1 支援 --webui
- install.ps1 Whisper 模型下載改用 curl.exe 顯示進度條（Windows 10+ 內建）

**改進**
- WebUI 離線模式自動隱藏不適用選項：降噪、錄音至檔案、轉錄麥克風、音訊裝置、場景、暫停按鈕
- WebUI 純錄音模式隱藏辨識模型/場景/翻譯引擎/離線選項/其他設定
- WebUI 辨識模型與場景改為各佔一行（避免長標籤擠壓）
- WebUI 辨識模型清單從 translate_meeting.py WHISPER_MODELS 動態產生（含 base.en / small.en / medium.en）
- WebUI 摘要模型清單有專屬說明（與翻譯模型說明區分）
- WebUI 提示文字依即時/離線模式動態切換
- README.md 新增 WebUI 使用說明與截圖
- 用語修正：拖放→拖曳

### v2.14.0 (2026-03-18)

**新功能**
- WebUI 大幅增強：
  - 輸入來源選擇：「即時音訊擷取」或「讀入音訊檔案」，檔案模式可瀏覽 recordings/ 目錄、拖曳上傳、關鍵字即時篩選
  - 字幕模式：電影風格全螢幕字幕顯示（黑底白字、居中、大字），可切換聊天/字幕模式
  - 離線處理選項：講者辨識（含人數設定）、產生摘要（含摘要模型選擇，有專屬說明）
  - 離線處理進度：辨識/講者辨識/輸出/LLM 校正/摘要各階段即時推送，含 model + tokens + t/s
  - 講者辨識 Speaker N 彩色標籤（8 色循環，與終端機一致）
  - 暫停/繼續按鈕（離線模式自動隱藏）
  - 狀態列彩色標籤：模式/模型/翻譯引擎/錄音/降噪/麥克風/講者辨識/摘要，各配 icon
  - 區域說明文字：每個設定區塊加入操作提示，離線/即時模式動態切換
  - 自訂下拉選單：圓角、hover 光棒、分群標題、預設/前次使用標籤（黃/綠膠囊）
  - 辨識模型依裝置 + 模式自動推薦（「此裝置適合」標籤，與終端機互動選單一致）
  - 翻譯引擎依 config 自動推薦（有 LLM 伺服器→預設 LLM，無→預設 NLLB）
  - LLM 模型含特性描述、自動查詢伺服器模型清單
  - 連線中斷即時通知 + 回到設定按鈕
  - toast 通知取代瀏覽器原生 alert
  - 防呆驗證：未選檔案、LLM 無主機/無模型、摘要無主機、重複啟動
  - 離線模式自動隱藏不適用選項（降噪/錄音/轉錄麥克風/裝置/場景/暫停按鈕）
  - 純錄音模式隱藏辨識模型/場景/翻譯引擎/離線選項/其他設定
  - start.ps1 支援 --webui

**改進**
- faster-whisper 模型來源：Systran repo 已需認證，改用 mobiuslabsgmbh（與 faster-whisper 內部一致）
- WebUI 停止改善：先 SIGINT 觸發正常清理，1 秒後 SIGKILL，不再卡住
- Port 衝突處理：啟動時偵測 port 被佔用，提供「結束殘留程序」或「換 port」選項
- 翻譯結果 S2TWP：離線處理的翻譯結果也套用簡轉繁
- install.sh / install.ps1 加入 python-multipart 套件（WebUI 檔案上傳需要）
- 用語修正：拖放→拖曳

### v2.13.0 (2026-03-18)

**新功能**
- WebUI 即時字幕介面：在瀏覽器中即時顯示辨識與翻譯結果，支援文字選取、搜尋、手機觀看
  - 聊天風格排版：系統音訊靠左（對方）、麥克風靠右（自己），類似即時通訊軟體
  - 淺色/深色主題切換（localStorage 記住偏好）
  - 辨識/翻譯耗時 badge（綠/橙/紅）
  - 自動捲動 + 手動捲動時暫停（底部「新訊息」按鈕恢復）
  - 手機 responsive
  - 用法：主程式加 `--webui` 旗標，另開終端機執行 `python3 webui.py`
  - 架構：translate_meeting.py → TCP localhost → webui.py (FastAPI) → WebSocket → 瀏覽器
  - 零效能影響：主執行緒僅 queue.put()（< 0.001ms），webui.py 未啟動時靜默丟棄
- 使用場景新增「演講簡報」（12 秒 buffer / 4 秒步進），適合長段演講或 CPU 較慢的環境
- GPU 伺服器互動選單新增場景選擇（原本硬編碼 5 秒，現在可選 3/5/8/12 秒）

### v2.12.1 (2026-03-17)

**改進**
- 日中雙向模式（ja_zh）麥克風支援中日英混雜輸入：語言預偵測自動區分中文、日文、英文。中文翻譯為日文，日文或英文直接顯示不翻譯
- Banner 提示：ja_zh 顯示「中日英混雜」、en_zh 顯示「中英混雜」

### v2.12.0 (2026-03-17)

**新功能**
- 英中雙向模式麥克風支援中英混雜輸入：以語言預偵測（detect_language）判斷每段音訊的語言，中文自動翻譯為英文，英文直接顯示不翻譯
  - macOS mlx-whisper：方案 A（wave + scipy resample 取代 ffmpeg）+ 方案 B（直接 model.decode 繞過 transcribe，mel 只算一次）大幅加速
  - Windows faster-whisper：透過 fw_model.detect_language() 預偵測，搭配 VAD 過濾
  - 啟動時預熱 detect_language + decode 路徑（中/英兩種語言），避免首次辨識延遲
- 離線處理配對自動偵測：選到含「系統音訊」或「麥克風」的單一檔案時，自動尋找同時間戳的配對檔案並提示一起處理

**改進**
- Ctrl+C 全面改善：所有即時模式的 signal handler 改用 `_force_exit()`（os._exit + 終止 resource_tracker），不再出現 segfault 或 semaphore 警告。按兩次 Ctrl+C 可強制結束
- HuggingFace 模型下載 SSL 容錯：install.ps1 / install.sh / translate_meeting.py 三處加入 SSL 憑證驗證失敗時自動停用驗證重試（企業網路常見的中間人憑證問題）
- install.ps1 CUDA 13+ 自動安裝相容程式庫：偵測到 CUDA 13.x 時自動 `pip install nvidia-cublas-cu12`，不再只是顯示警告
- 翻譯結果簡轉繁：所有即時模式的 LLM/NLLB 翻譯結果統一套用 S2TWP 繁體轉換，避免翻譯輸出殘留簡體字
- 即時辨識去重改善：新增 SequenceMatcher 字元相似度比對（>60%），解決滑動視窗重疊導致的近似重複（如「Windows跟Limps」vs「Windows跟Linux」）
- AirPods 麥克風靈敏度：降低 mic RMS 門檻（基礎 0.003、echo gate 0.015），正常音量即可辨識
- 用語修正：「滑動窗口」統一改為台灣用語「滑動視窗」

**修正**
- 修正中文幻覺偵測遺漏：改用 regex 偵測任意位置的短片段重複（如「有多少多少多少...」）
- 修正即時辨識 `Failed to load audio:` 錯誤：`transcribe_chunk` 新增 WAV 檔案存在性 + stop_event 檢查
- 修正配對處理 log 檔名開頭：非雙向模式走配對處理時，檔名改用「配對時間逐字稿」
- 修正配對處理摘要「來源音訊」只顯示單檔：改為同時顯示系統音訊與麥克風雙檔名
- 修正摘要檔名映射遺漏：補上「日中雙向」和所有「配對」開頭對應

### v2.11.2 (2026-03-16)

**改進**
- AI 模型表格重構：語音辨識拆分為 whisper.cpp / faster-whisper / mlx-whisper 三個引擎分別說明用途與平台
- install.ps1 新增 CUDA 13+ 相容性偵測：faster-whisper (CTranslate2) 需要 CUDA 12.x 的 `cublas64_12.dll`，CUDA 13.x 使用者會看到警告與 CUDA 12.8 安裝連結
- SOP 新增 CUDA 版本注意事項：說明 CUDA 13.x 與 faster-whisper 的相容性問題與解決方式

### v2.11.1 (2026-03-15)

**改進**
- OpenAI 相容 LLM 伺服器（vLLM、LM Studio、llama.cpp 等）摘要前自動查詢模型 context length，正確計算分段大小（之前固定 6000 字 fallback）
- install.sh 新增 pyenv 偵測：pyenv shim 存在但未設定版本時，顯示明確提示訊息與建議指令，避免安裝過程中出現不明確的錯誤
- 文件修正：README 和 SOP 多處內容與程式碼現狀不符的修正
  - Whisper 語言支援說明：「支援中英文」修正為「支援中日英文」
  - AI 模型表格與安裝項目表補上 NLLB 600M 離線翻譯
  - 場景名稱：「影片字幕」修正為「快速字幕」
  - 快捷鍵表格補上 Ctrl+P（暫停/繼續）
  - 流程圖補上 en_zh / ja_zh 雙向模式
  - 記錄檔名格式修正為實際的 `{模式}_逐字稿_YYYYMMDD_HHMMSS.txt`
  - 檔案說明補上日翻中/中翻日/日文/英中雙向/日中雙向等模式的記錄檔
  - 移除 text-generation-webui（程式碼未實作自動掃描）
  - 會議主題適用範圍補上日文相關翻譯模式
  - 「非中英文過濾」修正為「非預期語言過濾」（避免誤導日文模式）
  - LLM 翻譯說明：「需要 GPU 伺服器」修正為「需要 LLM 伺服器（本機或區域網路）」
  - 多處「GPU 伺服器」前後缺少空格修正
- 互動選單「若要同時轉錄麥克風(或其它音訊輸入)需選擇本機」提示改為紫色底白色字，更醒目
- LLM 建議模型列表 phi4:14b 描述從「品質最好」改為「品質不錯」
- 截圖檔名統一命名：`日中-7.png` → `bidi-ja-zh-html.png`
- 硬體建議 Apple 最低需求從 M1 以上改為 M2 以上

### v2.11.0 (2026-03-15)

**新功能**
- 新增「日中雙向字幕」模式（ja_zh）：對方說日文翻中文 + 自己說中文翻日文，適用於日中雙語視訊會議
- 即時模式和離線處理皆支援 ja_zh，與 en_zh 共用雙向架構，支援 LLM 和 NLLB 翻譯
- 離線處理自動從檔名推斷雙向模式（「日中雙向」→ ja_zh，「英中雙向」→ en_zh）

**改進**
- 離線翻譯前新增 LLM 預熱機制：在 ASR 辨識期間背景預載翻譯模型，避免模型被卸載導致翻譯逾時
- 中翻日（zh→ja）翻譯品質改善：新增假名驗證（結果必須含平假名/片假名），過濾 LLM 輸出簡體中文或英文的情況
- 新增日文方向 LLM 評論過濾（「修正し」「文法的に正しい」等），防止 LLM 在譯文後附加自我評論

### v2.10.2 (2026-03-15)

**改進**
- 互動選單功能模式分群顯示：單向翻譯、雙向翻譯、轉錄、其他四個群組，用分隔線區分
- 轉錄模式（英文/中文/日文轉錄）選完模式後立刻詢問是否轉錄麥克風，選是則自動設定辨識位置為本機、錄製音訊預設為混合錄製
- 錄音檔名加入模式標籤（例如「錄音_英翻中_」「錄音_英中雙向_系統音訊_」），方便辨識用途
- 雙向處理（process_bidi_audio_files）資訊區塊排版對齊：統一 12 格標籤寬度，「系統音訊語言」改為「辨識語言」
- 開始監聽提示語言標籤修正：「中轉錄」改為「中文轉錄」，麥克風通道也顯示完整語言名稱
- 辨識位置選單「若要同時轉錄麥克風需選擇本機」提示改為白色+黃色高亮，更醒目

**修正**
- 修正雙向模式 LLM 校正後逐字稿遺失 [Speaker N] 標記與方向符號（導致摘要缺少「辨識出 N 位」講者數）
- 修正雙向模式 LLM 校正重寫使用錯誤的 label 切片方式（line['label'][:1] 產生亂碼）

### v2.10.1 (2026-03-14)

**改進**
- 雙向模式（en_zh）離線處理產出的 HTML 時間逐字稿，麥克風（我方）段落改為靠右對齊、淺藍色文字，呈現聊天對話視覺效果
- HTML 逐字稿移除方向三角形符號（靠左/靠右對齊已足夠區分系統音訊與麥克風）
- 終端機輸出與 .txt 逐字稿保持原有三角形標記不變

### v2.10.0 (2026-03-13)

**新功能**
- 新增 `--mic` 參數：所有即時模式（en2zh、zh2en、ja2zh、zh2ja、en、zh、ja）可同時轉錄麥克風語音，將自己說的話即時顯示為文字
- 互動選單模式新增「是否同時轉錄麥克風」提示（非 en_zh/record 模式、Whisper 引擎時）
- 啟用 `--mic` 時自動切換為雙路 ASR 架構（與 en_zh 雙向模式相同），麥克風通道只轉錄不翻譯

**改進**
- 雙路 ASR 防幻覺：mlx-whisper 加入 `sample_len=80`、faster-whisper 加入 `max_new_tokens=40`，限制解碼器最大輸出長度，防止幻覺導致 15 秒以上的解碼阻塞
- 雙路 ASR 並行上限從 2 提升至 3（1 執行中 + 2 等鎖），避免序列化鎖導致新提取被跳過
- 主迴圈輪詢間隔從 200ms 降至 50ms，降低麥克風轉錄的感知延遲
- 麥克風音量偵測改用峰值 RMS（0.5 秒滑動視窗），改善短語偵測率
- 麥克風與系統音訊提取交錯排程（錯開 step_sec/2），減少序列化鎖衝突
- 序列化鎖改用 timeout 機制（timeout=step_sec），避免幻覺阻塞整條管線
- 重複過濾改用 70% 重疊率門檻，取代純子字串比對，減少誤判
- 中文幻覺過濾增強：單字頻率 >60%、連續重複 6 次以上、新增「初音」等關鍵字
- `initial_prompt` 僅套用於 medium 以上模型，避免 small 模型回聲洩漏
- 新增 `_PROMPT_LEAK_TEXTS` 安全過濾，移除辨識結果中殘留的 prompt 文字
- MLX 模型倉庫名稱修正：自動對應正確的 HuggingFace repo 字尾（large-v3-turbo 無字尾、其他加 -mlx）
- 暖機使用實際語言參數，避免 MLX 重新編譯導致首次辨識 11 秒以上
- 狀態列 resize 殘影清除邏輯改善：擴大清除範圍、不依賴游標 save/restore
- start.ps1 重複執行檢查新增 K 選項（終止舊程序後繼續）

**修正**
- 修正 Ctrl+C 退出時 MLX Metal mutex lock failed 錯誤（等待進行中的辨識完成後再清理）

### v2.9.0 (2026-03-12)

**新功能**
- 雙向模式（en_zh）加入 mlx-whisper 支援：Apple Silicon 自動使用 MLX GPU 加速辨識，large-v3-turbo 辨識耗時從 11-15s 降至 ~1.3s
- Apple Silicon + 雙向模式預設使用 mlx-whisper，使用者可用 `--asr faster-whisper` 退回 CPU 辨識
- install.sh 新增 `check_mlx_whisper()`：ARM64 Mac 自動安裝 mlx-whisper 套件並預下載 MLX 格式模型（約 1.6GB）
- 安裝摘要新增 MLX Whisper 狀態行（僅 Apple Silicon）
- `--asr` 參數新增 `faster-whisper` 選項，可強制指定使用 faster-whisper
- config.json 新增 `hide_asr_time` / `hide_translate_time` 選項，可個別隱藏辨識/翻譯耗時標籤

**改進**
- Apple Silicon 無 mlx-whisper 時，雙向模式模型推薦從 large-v3-turbo 降回 small，避免 CTranslate2 CPU 辨識過慢
- mlx-whisper 辨識使用 Metal GPU 序列化鎖，避免雙路同時存取 Metal command buffer 導致程式崩潰
- 雙向模式「開始監聽」提示文字加入顏色區分（系統音訊綠色、麥克風淡紫色）
- 英中雙向模式描述改為「對方說英文翻中文 + 自己說中文翻英文」

**修正**
- 修正雙向模式翻譯記錄寫入時 `prefix` 未定義的 NameError

### v2.8.0 (2026-03-12)

**新功能**
- 新增「英中雙向字幕」模式（en_zh）：同時擷取系統音訊（對方英文）和麥克風（自己中文），分別翻譯成中文和英文字幕，適用於英中雙語視訊會議
- 雙向模式自動偵測系統音訊裝置（BlackHole / WASAPI Loopback）和麥克風，兩路各自獨立辨識與翻譯
- 對方字幕用 ◀ 靠左顯示（灰/青綠配色），自己字幕用 ▶ 縮排顯示（水藍/淡紫配色），視覺上容易區分
- 支援 LLM 和 NLLB 翻譯引擎（Argos 僅支援單向，不適用雙向模式）
- 啟動時提醒使用耳機並停用非說話用的麥克風，避免漏音和幻覺
- 麥克風通道使用較高的靜音門檻（RMS >= 0.02），過濾喇叭漏音
- 系統音訊管線優先處理，麥克風管線在系統音訊辨識中時讓路，確保對方語音的翻譯速度
- 錄音時產出兩個獨立檔案（系統音訊 + 麥克風），方便事後分別離線處理
- 中文幻覺過濾新增「字幕志願」「字幕組」「翻譯志願」「校對志願」等 Whisper 常見幻覺關鍵字

### v2.7.2 (2026-03-12)

**修正**
- 修正 install.ps1 Python 偵測：移除 WindowsApps 路徑排除邏輯，改為直接用版本輸出判斷，修正 Python 已安裝但被誤判為找不到的問題

### v2.7.1 (2026-03-12)

**改進**
- 安裝/升級不再需要 Git：install.sh 和 install.ps1 的 bootstrap 與 --upgrade 改用 zip 下載，macOS 和 Windows 都不需要預先安裝 Git
- macOS 一鍵安裝指令改為三步驟：建立目錄（~/Apps/jt-live-whisper）、下載 install.sh、執行安裝
- Windows 一鍵安裝指令改為三步驟：建立目錄（C:\jt-live-whisper）、下載 install.ps1、執行安裝
- 啟動說明加上 cd 到安裝目錄的指令
- 離線翻譯選單：LLM 模型列表下方以分隔線附加 NLLB/Argos 本機離線翻譯選項
- 即時模式 GPU 伺服器路徑：辨識模型選單移到辨識位置之後（翻譯引擎之前）

**修正**
- 修正 install.sh bootstrap 在非函式環境使用 local 變數的錯誤
- 修正 install.sh --upgrade 時誤觸 bootstrap 的問題
- 修正 install.sh SSH key 不存在時的處理：自動產生 SSH Key 並部署公鑰到伺服器
- 修正 install.sh Intel Mac 上 remote_whisper_server.py 不存在導致 set -e 中斷的問題
- 修正 install.ps1 GPU 伺服器已安裝套件仍顯示「安裝中」的問題（加入預檢查）
- 修正 install.ps1 server.py 每次都重新部署的問題（加入 hash 比對）
- install.sh / install.ps1 執行中程序檢查新增 K 選項（強制結束後繼續安裝）

**文件**
- GPU 伺服器描述更新：明確說明支援 DGX Spark 及消費級 RTX 4090/5090 + CUDA
- 核心功能描述更新為「中日英即時翻譯字幕」
- 移除 Windows 安裝需求中的 Git
- 移除 macOS git clone 安裝方式
- PowerShell 安裝指令分為獨立區塊，避免多行貼上問題

### v2.7.0 (2026-03-11)

**新功能**
- 離線處理音訊檔新增 LLM 逐字稿文字校正：在有 LLM 伺服器且選擇摘要時，自動用 LLM 修正 ASR 辨識錯誤（專有名詞、同音字、錯字等）
- 校正自動偵測 ASR 幻覺（無意義外文音節、亂碼），標記為雜音並從逐字稿移除
- HTML 時間逐字稿 metadata 區塊新增「文字校正」資訊（校正模型與位置）
- 校正結果同步更新 log 檔、SRT 字幕檔與 HTML 逐字稿
- 離線處理互動選單：LLM 翻譯模型列表下方新增 NLLB / Argos 本機離線翻譯選項，以分隔線區隔，使用者可在 LLM 模型與本機翻譯間自由切換
- 即時模式 remote path 選單順序調整：「辨識模型（GPU 伺服器）」移至「辨識位置」之後（翻譯引擎之前），流程更直覺

**改進**
- LLM 校正加入 Qwen3 三層防禦（API think=False、Prompt 反思考指令、輸出 `<think>` 標籤清除）
- 校正函式 per-chunk 容錯：單批失敗不中斷整個校正流程
- 動態 timeout：依 chunk 字數自動調整（每千字 +60 秒，最低 300 秒）
- `call_ollama_raw` 新增 `think` 參數轉發至 `_llm_generate`
- 無 LLM 伺服器時 fallback 順序改為 NLLB 優先、Argos 次之（原本只有 Argos）
- `_input_interactive_menu` return tuple 新增 `translate_engine` 值，呼叫端直接取用翻譯引擎類型

**安裝程式改進（install.sh / install.ps1）**
- GPU 伺服器 SSH Key 不存在時自動產生 ed25519 金鑰並部署公鑰到伺服器，下次免密碼登入
- 偵測到執行中程序時新增 K 選項：強制結束程序後繼續安裝（原本只有繼續/取消）
- install.ps1 GPU 伺服器安裝流程加入已安裝檢查：Python 套件已裝則跳過、server.py 比對 hash 相同則跳過
- Argos 驗證改為目錄檢查（不依賴 pip 套件 import），修正模型已裝但驗證失敗的問題
- 修正 `remote_whisper_server.py` 不存在時 `set -e` 導致腳本中斷
- 修正 Bash UTF-8 變數展開問題（`$label` -> `${label}`），解決全形括號顯示亂碼
- install.ps1 無 GPU 提示訊息加入 NLLB 離線翻譯說明

**文件**
- SOP.md / README.md 新增「品質與效能說明」與「免責聲明」
- 全面修正中國用語為台灣用語（運行 -> 執行、客戶端 -> 用戶端）

### v2.6.0 (2026-03-10)

**新功能**
- 新增 NLLB 600M 離線翻譯引擎，支援中日英互譯（en2zh / zh2en / ja2zh / zh2ja 四種方向）
- NLLB 模型由使用者執行安裝程式時自行從 HuggingFace 下載（CC-BY-NC 4.0 授權，僅限非商業用途）
- CLI 新增 `-e nllb` 翻譯引擎選項
- install.sh / install.ps1 新增 NLLB 模型自動下載安裝
- 無 LLM 伺服器時 fallback 順序改為：NLLB（所有模式）-> Argos（僅英翻中）-> 錯誤
- 新增 Whisper 多語言 small / medium 模型選項，中日文模式可選（比 large-v3-turbo 更快，適合無 GPU 環境）

**改進**
- 翻譯引擎選單中 NLLB 對所有翻譯模式可選，Argos 維持僅英翻中
- 中日文模式預設模型依硬體自動選擇：有 GPU 時 large-v3-turbo，無 GPU 時 small（確保即時性）
- Windows faster-whisper 模式下 select_whisper_model() 跳過 ggml 檢查，所有模型均可選擇
- CLI 路徑重構：先判斷 faster-whisper 模式再呼叫 resolve_model()，避免多語言模型因缺少 ggml 檔報錯
- 隱藏 HuggingFace Hub 未認證下載警告訊息
- SOP 新增 NLLB 授權聲明（CC-BY-NC 4.0）

### v2.5.0 (2026-03-10)

**改進**
- 語音辨識模型預設邏輯改進：非英文模式 + 有 GPU（伺服器 / Apple Silicon / CUDA）預設 large-v3，無 GPU 預設 large-v3-turbo
- 新增 _has_local_gpu() 偵測本機 GPU（Apple Silicon Metal / NVIDIA CUDA）
- large-v3 模型描述更新為「中日文品質最好」
- 翻譯 prompt 新增忠實翻譯規則，禁止因政治因素修改用語（國名、地名、人物稱謂須與原文一致）
- 日文翻譯模式隱藏 Argos 離線選項（僅英翻中支援 Argos）
- 翻譯模型選單按名稱排序
- 新增 qwen2.5:32b 內建翻譯模型（中日文翻譯推薦）
- 翻譯引擎選單加入 LLM 伺服器推薦提示
- 純錄音模式描述動態顯示實際錄製格式（依 config.json 設定顯示 MP3 或 WAV）

**修正**
- 修正 Intel Mac 上 start.sh grep here-string 產生 broken pipe 錯誤
- 修正 start.ps1 / install.ps1 版本號未同步問題

**文件**
- SOP 新增日文模式說明、翻譯引擎限制（英翻中 / 中翻英 / 日翻中 / 中翻日各支援的翻譯引擎）
- SOP 翻譯引擎章節新增 LLM 伺服器推薦（Jan.ai / LM Studio）
- SOP 翻譯模型清單新增 qwen2.5:32b
- TEST_PLAN.md / TEST_PLAN_WINDOWS.md 更新日文模式數量

---

### v2.4.0 (2026-03-10)

**新功能**
- 日文語音辨識與翻譯支援，新增 3 個模式：
  - ja2zh（日翻中）：日文語音 -> 翻譯成繁體中文
  - zh2ja（中翻日）：中文語音 -> 翻譯成日文
  - ja（日文轉錄）：日文語音 -> 直接顯示日文
- 新增 _is_ja_hallucination() 日文 Whisper 幻覺過濾
- 新增 _build_prompt_ja2zh() / _build_prompt_zh2ja() LLM 翻譯 prompt
- 新增 C_JA 橙色顯示色彩常數
- 新增 mode 分類常數（_EN_INPUT_MODES / _ZH_INPUT_MODES / _JA_INPUT_MODES / _TRANSLATE_MODES / _NOENG_MODELS）簡化全域 mode 判斷
- 新增 _MODE_LABELS dict 統一管理各模式的原文/譯文標籤與色彩
- _str_display_width() 加入平假名/片假名全形寬度判斷
- OllamaTranslator._contains_bad_chars() 改為方向感知（zh2ja 輸出允許日文字元）

**限制**
- 日文翻譯僅支援 LLM，不支援 Argos 離線翻譯
- Moonshine 不支援日文（僅英文）
- 日文/中文模式不能使用 .en 模型

---

### v2.3.0 (2026-03-10)

**新功能**
- Windows 混合錄製支援（WASAPI Loopback + 麥克風同時錄音）
  - 新增 WASAPI_MIXED_ID sentinel，表示 Windows 雙串流混合錄音模式
  - 新增 _find_default_mic() 自動偵測 Windows 預設麥克風（排除 Loopback 裝置）
  - 新增 _DualStreamMixer 類別，以鎖保護即時混合兩個音訊串流寫入單一 WAV
  - 新增 _setup_mixed_recording() helper，建立 WASAPI Loopback + 麥克風雙串流，含取樣率不同時的重採樣
  - _ask_record() / _ask_record_source() / _auto_detect_rec_device() 支援 Windows 混合錄製選項
  - 4 個即時辨識函式（run_stream / run_stream_remote / run_stream_local_whisper / run_stream_moonshine）均支援 WASAPI_MIXED_ID
  - run_record_only() 混合模式波形顯示分 Loopback / Mic 兩行
  - Windows 預設錄音選項為「僅錄播放聲音」，需手動選擇才使用混合錄製
  - 混合錄音失敗時自動降級為僅 Loopback 錄音

---

### v2.2.0 (2026-03-09)

**新功能**
- translate_meeting.py 跨平台支援（macOS + Windows）
- SOP.md / README.md 新增 Windows 使用說明（音訊設定、安裝、啟動、CLI、FAQ）
  - 新增 IS_WINDOWS / IS_MACOS 平台偵測，條件式 import（termios/select vs msvcrt）
  - Windows 啟用 Virtual Terminal Processing（ANSI 色彩碼 / scroll region 支援）
  - 終端機 raw input 跨平台：setup_terminal_raw_input / restore_terminal / keypress_listener_thread / _wait_for_esc
  - SIGWINCH 信號處理以 hasattr 保護，Windows 改用 polling 偵測視窗大小變化
  - WHISPER_STREAM 路徑支援 .exe 與 Release 子目錄
  - 音訊裝置偵測跨平台：新增 _is_loopback_device()（BlackHole / WASAPI Loopback / Stereo Mix）
  - open 指令跨平台：Windows 用 os.startfile()，3 處 inline 改呼叫 open_file_in_editor()
  - SSH ControlMaster 路徑修正（: 改 _），Windows 跳過不支援的 ControlMaster
  - Argos 套件路徑跨平台（APPDATA vs ~/.local/share）
  - 使用者訊息跨平台：./start.sh → _START_CMD、./install.sh → _INSTALL_CMD、ffmpeg 安裝提示
  - subprocess creationflags：Windows 背景程序加 CREATE_NO_WINDOW

---

### v2.1.3 (2026-03-08)

**修正**
- 修正長逐字稿摘要時 LLM 截斷校正逐字稿的問題（「以下內容因篇幅限制略去」）
  - 調整分段算法：輸入佔 context window 的 1/3（原 3/4），確保回應空間充足
  - 摘要 prompt 明確禁止截斷或省略逐字稿內容
- 修正音訊檔選擇選單提示文字過暗（C_DIM → C_WHITE）

---

### v2.1.2 (2026-03-08)

**修正**
- 修正 `--asr` 參數說明文字誤標預設為 moonshine（實際預設為 whisper）
- 修正 README 中 `--summary-model` 預設值標示錯誤（應為 gpt-oss:120b）
- 修正音訊檔選擇選單提示文字過暗（C_DIM → C_WHITE）
- SOP 常見問答「翻譯品質不好」建議改為推薦至少 phi4:14b / qwen2.5:14b 或更高參數模型

---

### v2.1.1 (2026-03-08)

**改善**
- 摘要模型選擇選單列出 LLM 伺服器上所有模型（與翻譯模型選擇行為一致），支援「前次使用」標籤
- 狀態列與摘要狀態列統一用語：LLM 相關顯示「[伺服器]」或「[本機]」（不再使用「[伺服器]」）
- 說明文件與程式內文字統一「本機」用語（移除「本機 CPU」，因 Apple Silicon 上 whisper.cpp 使用 Metal GPU 加速）
- 翻譯引擎 Banner 顯示 LLM 伺服器類型（Ollama / OpenAI 相容）

### v2.1.0 (2026-03-08)

**新功能**
- 啟動 Banner 顯示翻譯引擎資訊（模型名稱、伺服器位址、伺服器類型）
- 狀態列新增「辨識 [伺服器/本機]」與「翻譯 [伺服器/本機]」欄位
- 模型選擇選單記住上次使用的模型，下次顯示「前次使用」標籤
- LLM 伺服器無推薦翻譯模型時顯示提示訊息
- Ollama 伺服器列出全部模型（與 OpenAI 相容伺服器行為一致）

**改善**
- 翻譯呼叫關閉 LLM 思考模式（Ollama `think=false`、OpenAI 相容 `enable_thinking=false`），避免 Qwen3 等模型輸出 `<think>` 標籤；翻譯結果自動剝除殘留的 `<think>` 標籤
- OpenAI 相容伺服器過濾 `owned_by=remote` 的模型（避免列出已斷線的伺服器代理模型）
- LLM 伺服器預設無連線，需透過 config.json 設定或 --llm-host 指定（不再硬編碼預設 IP）
- config.json 欄位名稱統一為 `llm_host` / `llm_port`（向後相容舊欄位 `ollama_host` / `ollama_port`）
- 未設定 LLM 伺服器時，互動選單提示輸入位址或按 Enter 使用離線翻譯

**文件**
- 時間逐字稿 HTML 圖片加上功能說明（波形圖定位、播放高亮）
- 常見問題新增 OpenAI 相容伺服器模型過濾說明

**修正**
- 修正即時模式 `src_color` 未定義導致 NameError 的 bug（影響 run_stream、run_stream_moonshine、run_stream_remote 三個函式）

### v2.0.8 (2026-03-08)

**改善**
- CLI 模式未指定 --asr 時，不再靜默預設為 Moonshine，改為顯示互動選單讓使用者選擇 ASR 引擎（指定 -m 則隱含 Whisper）
- CLI 模式未指定 -e 時，顯示翻譯引擎選單讓使用者選擇；指定 -e llm 但未指定 --llm-model 時，顯示 LLM 模型選單
- 等效 CLI 指令改用實際選擇值（翻譯引擎、模型、伺服器），不再遺漏從選單選取的參數
- 純錄音模式的會議主題提示改為「選填，用做檔名參考」

### v2.0.7 (2026-03-07)

**改善**
- 更新離線處理、摘要、逐字稿、講者辨識等截圖為最新版畫面
- README 與 SOP 新增離線處理選單完整流程截圖（選單、設定總覽、CLI 指令）
- README 與 SOP 新增校正逐字稿 HTML 與時間逐字稿 HTML 截圖
- README 與 SOP 新增講者辨識終端機逐字稿輸出截圖

### v2.0.6 (2026-03-07)

**新功能**
- --input 模式處理完成後自動產出 .srt 字幕檔，翻譯模式為雙語字幕、單語模式為單語字幕
- 逐字稿/摘要 HTML footer 新增 SRT 下載連結
- 所有模式啟動前顯示等效 CLI 指令（方便下次直接貼上執行），並詢問 Y/N 確認

**文件**
- 常見問題新增「為什麼不支援雲端大語言模型」說明
- README 補上音訊 MIDI 設定截圖（audio-midi-setup.png）

### v2.0.5 (2026-03-07)

**新功能**
- --input 模式每次處理建立獨立子目錄（logs/{basename}_{timestamp}/），將音訊副本、逐字稿、摘要集中存放，方便整組帶走

### v2.0.4 (2026-03-06)

**改善**
- 摘要狀態列顯示 LLM 伺服器位置（本機/伺服器），與 ASR 狀態列風格一致
- 摘要 metadata 欄位名稱統一為四字中文（語音辨識、講者辨識、語言翻譯、內容摘要、來源音訊），冒號統一使用全形
- HTML 摘要版本資訊改用標籤（badge）樣式呈現
- 講者辨識選單預設改為「自動偵測講者數」
- HTML 校正逐字稿：LLM 漏掉 Speaker 標籤的延續段落，程式端自動補上講者標籤與顏色

### v2.0.3 (2026-03-06)

**新增**
- install.sh 新增 CTranslate2 aarch64 CUDA 原始碼編譯：aarch64 GPU 伺服器（如 DGX Spark）自動從原始碼編譯 CTranslate2，使 faster-whisper 可用 GPU CUDA 加速（預期速度從 ~2x 提升至 10-20x realtime）
  - 自動偵測 GPU 架構、前提條件檢查（nvcc/cmake/git/g++/cuDNN/磁碟空間）
  - 編譯產出 wheel 快取至 `~/jt-whisper-server/.ct2-wheels/`，後續安裝直接使用
  - 編譯失敗自動降級 openai-whisper（PyTorch CUDA）
- 伺服器啟動指令自動加入 `LD_LIBRARY_PATH=/usr/local/lib`，確保原始碼編譯的 CTranslate2 可被正確載入
- 既有環境檢查可區分顯示原始碼編譯 vs PyPI 預編譯的 CTranslate2

### v2.0.2 (2026-03-06)

**新增**
- 啟動時自動檢查 GitHub 新版本：背景執行不影響啟動速度，有新版時顯示提醒和升級指令（`./install.sh --upgrade`），本地版本較新或相同時不顯示

### v2.0.1 (2026-03-06)

**修正**
- 修正多段校正逐字稿第 2 段以後 Speaker 標籤遺失的問題：強化摘要 prompt，要求每個段落開頭都必須標注講者（即使連續多段為同一位講者也不可省略），並增加同一講者延續的輸出範例

### v2.0.0 (2026-03-06)

**新增**
- 音訊檔名帶主題：即時/錄音模式填入主題時，錄音檔名和記錄檔名自動加入簡化主題（例如 `錄音_Wazuh_20260306_143022.wav`、`英翻中_逐字稿_Wazuh_20260306_143022.txt`）
- 純錄音模式新增主題輸入選單
- 伺服器狀態檢查 `/v1/status`：上傳前自動偵測伺服器忙碌狀態和磁碟空間
  - 忙碌時提供 3 選項：等候 / 強制中斷殘留作業 / 改用本機
  - 磁碟空間不足時自動警告並降級本機
- 伺服器作業追蹤：記錄目前作業類型、模型、語言、已執行時間、來源 IP
- openai-whisper 辨識進度：攔截 verbose 輸出追蹤每段解碼進度，心跳帶上百分比、已辨識位置、總時長

**修正**
- 修正 openai-whisper 後端串流模式暫存檔提前刪除的 bug（finally 條件邏輯錯誤導致 "No such file" 錯誤）

**改進**
- 錄音選單從 2 選項改為 3 選項：混合錄製（輸出+輸入）/ 僅錄播放聲音 / 不錄製，自動偵測聚集裝置和 BlackHole 並標示可用狀態
- install.sh 伺服器 server.py 更新訊息改為更明確的描述

### v1.9.9 (2026-03-06)

**新增**
- GPU 伺服器辨識串流進度：`--input` 上傳大檔到 GPU 伺服器辨識時，即時顯示辨識進度百分比與已處理時間/總時長，不再只顯示「等待伺服器回應...」
- 伺服器端串流 NDJSON 回傳（`stream=true`），每辨識完一段即送出，解決長音檔 timeout 問題
- 向下相容：未傳 `stream` 參數時維持原有 JSON 一次回傳

**改進**
- 互動選單摘要選項從 2 個改為 4 個：產出摘要與校正逐字稿（預設）、只產出摘要、只產出逐字稿、不摘要
- 摘要 prompt 依 summary_mode 動態調整，減少不必要的 LLM 輸出
- 多段摘要的「只逐字稿」模式跳過合併步驟，直接串接各段校正逐字稿

### v1.9.8 (2026-03-06)

**新增**
- 摘要檔（.txt 和 .html）開頭加入處理資訊 metadata header，包含辨識引擎/模型、講者辨識、翻譯模型、摘要模型、輸入檔案等完整處理參數
- `--input` 離線處理的摘要檔包含完整 metadata（ASR、diarization、翻譯、摘要）
- `--summarize` 批次摘要的摘要檔包含基本 metadata（摘要模型、輸入檔案）

### v1.9.7 (2026-03-06)

**修正**
- 即時模式翻譯有序輸出：多句同時送翻譯時，短句先回來不再搶先輸出，嚴格按原文順序排隊顯示
- 「開始監聽...」與第一行辨識結果之間加入間距，避免黏在一起
- Ctrl+C 停止時 ffmpeg 轉檔錯誤訊息簡化，不再暴露完整路徑與指令

### v1.9.6 (2026-03-06)

**改進**
- 所有輸出檔案改用中文開頭命名，一目了然：
  - 逐字稿：`英翻中_逐字稿_`、`中翻英_逐字稿_`、`英文_逐字稿_`、`中文_逐字稿_`
  - 摘要：`英翻中_摘要_`、`中翻英_摘要_`、`英文_摘要_`、`中文_摘要_`
  - 錄音：`錄音_`
- 錄音輸出格式改為 MP3（近無損品質 VBR ~220-260kbps），大幅減少檔案體積
- 支援透過 config.json 設定錄音格式（`recording_format`：mp3/ogg/flac/wav）
- 錄音期間仍以 WAV 暫存（保留 30 秒 header 更新防當機機制），停止時自動轉檔
- 轉檔失敗時保留原始 WAV 檔，不中斷程式運作
- --input 暫存檔改為 `tmp_` 開頭（不再是隱藏檔）

### v1.9.4 (2026-03-05)

**改進**
- 支援多個體同時使用 GPU 伺服器：啟動時先檢查伺服器是否已在執行，是則直接沿用；退出時不再關閉伺服器
- 新增 --restart-server 參數：強制重啟伺服器（更新 server.py 後使用）
- 伺服器啟動/等待就緒/載入模型期間新增 spinner 動畫（避免使用者誤以為當機）
- 互動選單檔案選擇支援逗號分隔多選（如 1,3,5 一次選三個檔案處理）
- 上傳音訊完成後狀態列即時切換為「GPU 伺服器辨識中」（不再停留在上傳進度）
- Ctrl+C 中止時不再顯示 Python traceback，乾淨退出
- install.sh 所有耗時操作新增 spinner 動畫（安裝、編譯、下載、伺服器檢查）
- install.sh GPU 伺服器檢查階段統一使用 [完成]/[安裝]/[失敗] 格式
- 互動選單 UI 改善：Banner 資訊三層顏色區分、辨識模型快取標記對齊、錄製音訊區段排版

**修正**
- 修正 --input 伺服器辨識時狀態列計時器每段歸零的問題（現在顯示總經過時間）
- 修正辨識模型選單因中文全形字元導致快取標記未對齊的問題（改用顯示寬度計算）
- install.sh 伺服器安裝補齊全部編譯依賴: python3-dev, build-essential, pkg-config, libffi-dev, libsndfile1-dev
- install.sh pip 安裝鎖定 setuptools<81（防止 --force-reinstall 升級後 pkg_resources 消失）
- install.sh pip 安裝錯誤訊息不再被 grep 過濾隱藏

**文件**
- SOP.md 新增「全地端執行」兩種部署模式說明（單機模式 / 本機 + GPU 伺服器模式）
- SOP.md 系統架構圖更新 GPU 伺服器標註、新增伺服器架構圖和 AI 模型表

### v1.9.3 (2026-03-05)

**新功能**
- 新增 GPU 伺服器講者辨識：`--diarize` + `--input` 搭配伺服器 Whisper 時自動使用 GPU 伺服器執行 diarization
- 伺服器端新增 `/v1/audio/diarize` API endpoint（resemblyzer + spectralcluster，有 GPU 自動加速）
- `/health` 新增 `diarize` 欄位，用戶端可偵測伺服器是否支援講者辨識
- `install.sh` 伺服器部署自動安裝 resemblyzer + spectralcluster
- 伺服器 diarization 失敗時自動降級本機執行，不中斷流程

---

### v1.9.2 (2026-03-05)

**新功能**
- 新增 Ctrl+P 暫停/繼續即時翻譯（三種模式皆支援：Whisper / Moonshine / GPU 伺服器）
- 暫停時音訊擷取持續運作（波形仍跳動），僅暫停辨識與翻譯輸出
- 狀態列即時顯示暫停狀態（黃色 ⏸ 已暫停），並切換快捷鍵提示

---

### v1.9.1 (2026-03-05)

**修正**
- 即時伺服器辨識 timeout 從 30 秒改為 120 秒，避免大模型首次載入時逾時
- 新增模型預熱：啟動伺服器後送靜音 WAV 觸發模型載入到 GPU，等載入完成才開始監聽
- 修正 drain_ordered_results 邏輯錯誤（無法區分「尚未到達」與「上傳失敗」）
- 「音訊窗口」改為「音訊緩衝」（修正用語）
- 辨識位置提示「伺服器不支援 Moonshine」改為黃色醒目顯示
- large-v3 模型說明加註「有獨立 GPU 可選用」
- SOP.md 新增互動選單完整流程圖（即時模式 + 離線模式所有分支）

---

### v1.9.0 (2026-03-05)

**新功能**
- 即時模式支援 GPU 伺服器 Whisper 辨識：本機擷取音訊 -> 上傳 GPU 伺服器 -> 取回辨識結果 -> 翻譯顯示
- 新增 `select_asr_location()` 互動選單步驟，有伺服器設定時自動出現「辨識位置」選項
- 新增 `select_whisper_model_remote()` 伺服器模型選擇（顯示伺服器快取標籤）
- 新增 `run_stream_remote()` 核心函式：環形緩衝 + 有序非同步上傳 + 去重
- CLI 模式：有伺服器設定時即時模式自動走伺服器路徑（`--local-asr` 強制本機）
- `--local-asr` 說明更新，明確適用於即時模式與離線模式

**技術細節**
- 音訊緩衝 5 秒、滑動步進 3 秒（與 whisper-stream 一致）
- sounddevice 擷取 48kHz -> 降頻 16kHz -> 環形緩衝 -> in-memory WAV 上傳
- RMS 靜音偵測（< 0.001 跳過上傳），遞增序號確保字幕順序正確
- 伺服器不支援 Moonshine（選單明確告知此限制）
- 個別上傳失敗不中斷，跳過該 chunk 繼續運作

---

### v1.8.6 (2026-03-05)

**改進**
- 離線處理互動選單的「辨識位置」步驟改為永遠顯示，未設定 GPU 伺服器時選擇會提示執行 install.sh 設定
- install.sh --upgrade 版本比較：若本地版本比 GitHub 還新則不覆蓋（避免開發機被降版）
- install.sh GPU 伺服器設定改為自動檢查：已設定時檢查 SSH/Python3/venv/server.py/套件，有問題自動提示修復
- install.sh GPU 伺服器設定使用 SSH ControlMaster 多工，全程只需輸入一次密碼
- install.sh GPU 伺服器區段標題改為「非必要，若未裝則用本機進行語音辨識」
- install.sh 安裝完成後新增提示：資料夾搬移後需重新執行 install.sh

---

### v1.8.5 (2026-03-05)

**改進**
- 講者辨識精準度提升：啟用 SpectralClusterer refinement（高斯模糊 + 行最大值門檻），抑制噪音相似度
- 講者辨識：長段落（>= 1.6s）改用滑動視窗 embedding + 中位數，聲紋特徵更穩定
- 講者辨識：連續短段落（< 0.8s）合併音訊後再提取 embedding，避免碎片化
- 講者辨識：分群後增加餘弦相似度二次校正，差距明顯（> 0.1）時重新指派講者
- 講者辨識：平滑修正從孤立段落修正升級為窗口 5 多數決，更穩定
- 離線處理互動選單新增「會議主題」步驟（選填），主題同時影響翻譯 prompt 和摘要 prompt
- 摘要 prompt 支援帶入會議主題，LLM 可根據主題領域知識理解專業術語並正確校正
- 合併摘要 prompt 也支援帶入會議主題
- 摘要帶入會議主題（從翻譯器取得）
- 批次摘要（--summarize）支援透過 --topic 參數指定主題

---

### v1.8.4 (2026-03-05)

**新功能**
- 離線處理音訊檔（--input）支援 GPU 伺服器語音辨識：將音訊上傳到 Linux + NVIDIA GPU 伺服器辨識，速度快 5-10 倍（支援系統：DGX OS / Ubuntu）
- 新增 remote_whisper_server.py，部署到伺服器的 FastAPI Whisper ASR 服務
- install.sh 新增選填步驟 setup_remote_whisper()，自動 SSH 部署伺服器環境
- 互動選單新增「辨識位置」步驟（GPU 伺服器 / 本機），僅在有伺服器設定時出現
- 新增 --local-asr 參數，強制使用本機辨識（忽略 GPU 伺服器設定）
- 新增 --restart-server 參數，強制重啟 GPU 伺服器（更新 server.py 後使用）
- 伺服器辨識失敗時自動降級為本機，不中斷處理流程

**修正**
- 修正 LLM 連線失敗時「LLM 摘要」標籤排版對齊問題（CJK 全形字元寬度計算）

---

### v1.8.3 (2026-03-05)

**改進**
- 狀態列波形刷新頻率從每秒 1 次提升至每秒 5 次（0.2 秒一次），波形跳動更即時流暢
- 同時適用 Whisper 與 Moonshine 兩種模式

---

### v1.8.2 (2026-03-05)

**新功能**
- 翻譯模式（en2zh/zh2en）與轉錄模式（en/zh）的底部狀態列新增即時音量波形圖，讓使用者確認音訊有正常輸入
- 波形 12 字元寬，綠色顯示，無聲時為全平線
- 三種情境自動偵測音量：Moonshine 音訊回呼、Whisper 錄音回呼、Whisper 無錄音時被動監控 BlackHole 裝置

---

### v1.8.1 (2026-03-05)

**新功能**
- 功能模式新增「純錄音」選項，僅錄製音訊為 WAV 檔，不做 ASR 辨識或翻譯
- 純錄音模式顯示即時音量波形圖，讓使用者確認音訊有正常輸入
- 支援 CLI 模式（`--mode record`）及互動選單（選項 [4]）
- 離線處理（讀入音訊檔案）選單自動過濾「純錄音」選項

---

### v1.8.0 (2026-03-05)

**新功能**
- 互動選單新增「輸入來源」第一步，啟動時可選擇「即時音訊擷取」或「讀入音訊檔案」
- 選擇「讀入音訊檔案」時，自動列出 `recordings/` 目錄下音訊檔（.wav/.mp3/.m4a/.flac/.ogg），每頁 10 筆可翻頁，顯示檔名、大小、修改時間
- 選擇檔案後自動進入離線處理互動選單（`_input_interactive_menu`），不需再用 CLI `--input` 參數

**改進**
- 互動選單各區塊間距加大，視覺更清楚
- 翻譯模型排序調整：phi4:14b 移至第一位（預設仍為 qwen2.5:14b）
- 「會議主題」標題用語從「可選」改為「選填」，提示文字改為「若無特定主題要填寫，可直接按 Enter 跳過」

---

### v1.7.9 (2026-03-04)

**修正**
- 修正 HTML 摘要中編號子項目（如「Clonezilla 使用流程：」下的 1. 2. 3.）未正確內縮為巢狀清單的問題，改以 `<ol>` 巢狀於父項 `<li>` 內呈現

---

### v1.7.8 (2026-03-04)

**新功能**
- 新增「會議主題」功能（`--topic`），可在啟動時輸入會議主題或領域，注入翻譯 prompt 提升專業術語翻譯品質
- 互動選單翻譯模式新增「會議主題」步驟（可選，按 Enter 跳過）
- 啟動 banner 顯示目前會議主題（如有設定）

**改進**
- CLI 參數 `--engine` 選項從 `ollama` 改為 `llm`，避免誤解為僅支援 Ollama（實際支援所有 OpenAI 相容伺服器）
- CLI 參數 `--ollama-model` 改為 `--llm-model`、`--ollama-host` 改為 `--llm-host`
- 移除 Ctrl+S 即時摘要功能（摘要改為透過 --summarize 批次處理）

**修正**
- 修正 OpenCC 簡繁轉換誤判：從 `s2twp`（詞組匹配）改為 `s2tw`（僅字元轉換），避免「裡面包含」被誤轉為「裡麵包含」等問題。詞組層級轉換改由 LLM 直接輸出正確繁體
- 修正互動選單輸入中文會議主題時 UnicodeDecodeError 的問題

---

### v1.7.7 (2026-03-04)

**改進**
- 錄音預設改為「錄製」，選單中預先顯示偵測到的錄音裝置
- 錄音選單新增醒目提醒：即時辨識僅處理播放聲音，無法包含我方說話的聲音
- 啟動時檢查 BlackHole、多重輸出裝置、聚集裝置是否已設定，缺少時顯示設定指引
- 聚集裝置偵測支援使用者改過名稱的情況（以 input channels >= 3 作為備用判斷）
- WAV 錄音加入防護機制：每 30 秒自動更新 WAV header，程式異常終止時錄音檔仍可正常播放
- 全部文件與程式中「說話者」統一改為「講者」

---

### v1.7.6 (2026-03-04)

**改進**
- 簡化裝置選擇流程：移除 ASR 音訊裝置和錄音裝置的互動選單，改為全自動偵測
- ASR 裝置自動選擇 BlackHole 2ch，找不到時才 fallback 顯示選單
- 錄音裝置自動偵測聚集裝置（雙方聲音），找不到降級使用 BlackHole（僅對方聲音）
- 互動選單從 7 步簡化為 6 步（移除音訊裝置選擇步驟）
- 翻譯引擎選擇提前到錄音之前，流程更合理
- 一套 macOS 音訊設定通用：設定好多重輸出裝置和聚集裝置後，程式自動偵測，不需每次手動選擇
- 錄音選單新增說明：即時辨識僅處理對方聲音，如需轉錄自己的聲音請錄製後用 `--input` 離線處理

---

### v1.7.5 (2026-03-04)

**新功能**
- 即時模式錄音：`--record` 參數或互動選單選擇，可同時將音訊錄製為 WAV 檔（存入 `recordings/`）
- 錄音裝置選擇：`--rec-device ID` 或互動選單，可選擇與 ASR 不同的錄音裝置
- 預設優先選聚集裝置（同時錄到對方與自己的聲音），其次 BlackHole
- 互動選單新增「錄製音訊」和「錄音裝置」兩個步驟，選完翻譯引擎後出現
- 支援 Whisper 和 Moonshine 兩種引擎的錄音
- 錄音檔預設 MP3 格式（可設定），檔名含時間戳（如 `錄音_20260304_143000.mp3`）
- 停止時（Ctrl+C）自動關閉錄音並顯示儲存路徑
- HTML 摘要底部新增相關檔案連結（摘要 HTML、摘要 TXT、逐字稿 TXT）
- 支援 `config.json` 自訂翻譯模型（`translate_models`）和摘要模型（`summary_models`），附加到內建推薦清單後面
- 錄音裝置選單中，名稱不含 BlackHole 的裝置以暗色顯示，方便辨識
- `--input` 離線模式辨識模型預設改為 large-v3-turbo（不分語言）
- 預設不錄音，完全向下相容
- SOP 大幅改寫音訊設定章節：新增聚集裝置設定圖解、Zoom/Teams 設定說明、完整音訊流向圖

**修正**
- HTML 摘要標題少字（`## 重點摘要` 只顯示「點摘要」）
- HTML 摘要不同講者使用不同顏色（8 色循環），延續段落繼承同色
- HTML 摘要列表項目加上 `<ul>` 包裹和正確內縮

---

### v1.7.4 (2026-03-03)

**新功能**
- `install.sh --upgrade`：從 GitHub 自動下載最新版本程式檔案，顯示版本比對結果

**改進**
- 產出的文字檔（記錄、摘要）統一放在 `logs/` 子資料夾，不再與程式同層
- 暫存音訊轉檔放在 `recordings/` 子資料夾
- 目錄自動建立，無需手動操作
- SOP 新增一鍵安裝指令和升級說明

---

### v1.7.3 (2026-03-03)

**修正**
- 修正 `select_translator()` port 解析缺少 try/except，輸入非數字 port 時程式會崩潰
- 修正 `_resolve_ollama_host()` CLI 參數 port 解析缺少 try/except
- 修正運算子優先順序問題：`if need_translate and engine == "ollama" or do_summarize` 加上括號明確語意
- 修正 LLM 伺服器偵測失敗時仍靜默顯示 Ollama 模型清單，改為顯示明確警告訊息

---

### v1.7.2 (2026-03-03)

**新功能**
- 自動偵測 LLM 伺服器類型：支援 Ollama 原生 API 和 OpenAI 相容 API
- 支援 LM Studio、Jan.ai、vLLM、LocalAI、llama.cpp server、text-generation-webui、LiteLLM 等 OpenAI 相容伺服器
- 偵測策略：先嘗試 Ollama `/api/tags`，失敗則嘗試 OpenAI `/v1/models`，不需使用者手動選擇
- OpenAI 相容伺服器的翻譯/摘要模型從伺服器取得實際模型清單讓使用者選擇
- 新增 `_detect_llm_server()`、`_llm_list_models()`、`_llm_generate()` 統一 LLM 通訊層
- 新增 `LLM_PRESETS` 常數，列出常見 LLM 伺服器預設 port 供參考

**改進**
- 選單文字「Ollama 伺服器」改為「LLM 伺服器」，偵測後顯示伺服器類型和可用模型數
- `OllamaTranslator` 新增 `server_type` 參數，支援 OpenAI 相容 API 的非串流翻譯
- `call_ollama_raw()` 新增 `server_type` 參數，支援 OpenAI 相容 API 的串流生成（SSE 格式）
- `query_ollama_num_ctx()` 遇 OpenAI 相容伺服器直接回傳 None（用既有 fallback）
- `_check_ollama()` 改名為 `_check_llm_server()`，回傳 `(server_type, model_list)`
- `summarize_log_file()` 支援 `server_type` 參數傳遞
- `run_stream()` 和 `run_stream_moonshine()` 新增 `summary_server_type` 參數
- `--input` CLI 模式和互動選單都支援自動偵測 LLM 伺服器類型
- `--summarize` 批次摘要模式支援自動偵測 LLM 伺服器類型

**文件**
- SOP 翻譯引擎章節更新支援的 LLM 伺服器清單
- SOP FAQ 更新 LLM 伺服器相關說明

---

### v1.7.1 (2026-03-03)

**新功能**
- `--input` 不帶 `--mode` 時進入三步互動選單，讓使用者選擇功能模式、講者辨識、摘要
- 互動選單依序選擇：(1) 功能模式 (2) 講者辨識（不辨識/自動偵測/指定人數）(3) 摘要
- 選完後顯示確認行，一目了然
- `--input` 帶 `--mode` 時維持原行為，直接執行不問

**改進**
- `--input` 分支改用統一的 mode/diarize/num_speakers/do_summarize 變數，不再直接讀 args
- CLI 帶 `--diarize` 但沒帶 `--mode` 進選單時，講者辨識預設選「自動偵測」
- 選單任一步驟按 Ctrl+C 正常退出

**文件**
- SOP `--input` 參數說明新增互動選單描述
- SOP 4-9 節新增互動選單模式說明
- SOP 範例新增互動選單用法
- SOP CLI 模式說明補充 `--input` 互動選單行為

---

### v1.7.0 (2026-03-03)

**新功能**
- 新增 `--diarize` 參數：講者辨識，區分不同講者（需搭配 `--input`）
- 使用 resemblyzer（d-vector 聲紋特徵提取）+ spectralcluster（Google 頻譜分群）
- 不需要 HuggingFace token，M2 上處理 30 分鐘音訊約 30-60 秒
- 新增 `--num-speakers N` 參數：指定講者人數（預設自動偵測 2~8）
- 終端機每位講者以不同顏色顯示（8 色循環），記錄檔帶 `[Speaker N]` 標籤
- 可搭配 `--summarize` 一起使用（辨識 + 翻譯 + 摘要）

**改進**
- `install.sh` 自動安裝 resemblyzer 和 spectralcluster 套件
- 段落太短（< 0.5 秒）自動擴展或繼承相鄰講者
- 分群失敗時降級為全部 Speaker 1，不中斷處理
- `--num-speakers` 不搭配 `--diarize` 時顯示警告

**文件**
- SOP 系統架構新增講者辨識流程
- SOP 安裝項目表新增 resemblyzer、spectralcluster
- SOP CLI 參數表新增 `--diarize`、`--num-speakers`
- SOP 新增 4-10 節「--diarize 講者辨識」完整說明
- SOP 使用流程、範例、常見問題同步更新

---

### v1.6.0 (2026-03-03)

**新功能**
- 新增 `--input` 參數：離線處理音訊檔案（mp3/wav/m4a/flac 等）
- 使用 faster-whisper（CTranslate2 引擎）進行離線辨識，支援 VAD 過濾
- 支援批次處理多個音訊檔（`--input f1.mp3 f2.m4a`）
- `--input` 搭配 `--summarize` 可自動轉錄後摘要
- `-m` 參數在 `--input` 模式下指定 faster-whisper 模型
- 離線處理記錄檔帶時間戳記（`[MM:SS-MM:SS]`），方便對照原始音訊
- 非 wav 音訊檔自動用 ffmpeg 轉換為 16kHz mono WAV

**改進**
- 幻覺過濾提取為共用函式（`_is_en_hallucination`、`_is_zh_hallucination`），離線和即時模式共用
- 中文幻覺過濾新增簡體關鍵字（faster-whisper 可能輸出簡體）
- `--summarize` 改為可選檔案參數（`nargs="*"`），與 `--input` 合用時不需指定檔案
- `--summarize` 單獨使用但未指定檔案時，會提示正確用法
- `start.sh` 使用 `--input` 或 `--summarize` 時跳過 BlackHole 檢查

**文件**
- SOP 系統架構新增離線處理流程圖
- SOP 新增 4-9 節「--input 音訊檔離線處理」完整說明
- SOP CLI 參數表新增 `--input`，更新 `--summarize` 說明
- SOP 新增離線處理範例
- SOP 使用流程總結新增離線處理步驟
- SOP 常見問題新增 ffmpeg、faster-whisper 相關 Q&A
- SOP 修正 v1.5.0 遺漏：預設 ASR 引擎為 Whisper（非 Moonshine）
- SOP 修正 v1.5.0 遺漏：摘要完成後狀態列凍結 + ESC 退出
- SOP 修正 v1.5.0 遺漏：install.sh 含 ffmpeg + faster-whisper

---

### v1.5.0 (2026-03-03)

**新功能**
- 新增 Moonshine ASR 引擎：真串流語音辨識，延遲從 8-14 秒降至 1-3 秒
- 英文模式（en2zh、en）可選使用 Moonshine，中文模式維持 Whisper
- 新增 --asr 參數，可選擇語音辨識引擎（moonshine / whisper）
- 新增 --moonshine-model 參數，可選擇 Moonshine 模型（medium / small / tiny）
- 互動式選單新增 ASR 引擎選擇（英文模式時顯示）
- Moonshine 使用內建 VAD 自動斷句，不需要選擇使用場景
- Moonshine 模型三種尺寸：medium（245MB，推薦）、small（123MB）、tiny（34MB）

**改進**
- install.sh 優先使用 ARM Homebrew Python（Moonshine 需要 ARM64 原生 Python）
- install.sh 自動安裝 moonshine-voice、sounddevice、numpy、faster-whisper
- install.sh 自動安裝 ffmpeg
- install.sh 自動下載 Moonshine medium 模型
- 翻譯引擎預設改為 qwen2.5:14b（速度快且品質好）
- 預設 ASR 引擎改為 Whisper（高準確度，支援中英文）
- --list-devices 同時顯示 sounddevice 和 whisper-stream 兩套裝置列表
- 如果 Moonshine 未安裝，英文模式自動降級為 Whisper
- 摘要完成後狀態列凍結顯示最終統計，按 ESC 鍵退出

**文件**
- SOP 新增 Moonshine 引擎說明與效能比較
- SOP 更新 CLI 參數表與範例
- SOP 更新安裝項目清單

---

### v1.4.0 (2026-03-03)

**新功能**
- 新增「中翻英字幕」模式 (zh2en)：中文語音 → Whisper 辨識 → Ollama 翻譯成英文
- 新增 --summarize 批次摘要：對已有的記錄檔進行後處理摘要，不啟動即時轉錄
- 新增 --summary-model 參數，可指定摘要用的 Ollama 模型（預設 gpt-oss:120b）
- 長文自動分段摘要：自動偵測模型 context window 動態決定分段大小

**改進**
- 底部固定狀態列：即時顯示經過時間、翻譯筆數、快捷鍵提示（不被字幕捲走）
- 摘要輸出 Markdown 彩色渲染（標題、列表、粗體各有顏色）
- 摘要完成後自動用系統編輯器開啟摘要檔
- 選單分隔線寬度統一為 60 字元，與程式標題等寬
- 音訊裝置選單修正：只標示實際會被自動選中的裝置為「預設」
- atexit + stty sane 雙重安全網確保終端機一定恢復正常
- 摘要檔名自動依原始記錄檔類型命名（英翻中_摘要_* / 中翻英_摘要_* / 中文_摘要_*）

**文件**
- SOP 新增 --summarize 批次摘要使用說明
- SOP 更新 CLI 參數表與範例

---

### v1.3.0 (2026-03-03)

**新功能**
- 新增「功能模式」選擇：英翻中字幕 (en2zh) 與中文轉錄 (zh) 兩種模式
- 中文轉錄模式直接顯示繁體中文，跳過翻譯引擎，自動隱藏 .en 模型
- 新增 Whisper large-v3 模型支援，中文轉錄模式預設使用（中文辨識品質最佳）
- 新增 --mode CLI 參數，支援從命令列直接指定功能模式
- 新增 config.json 設定檔，自動記住 Ollama 伺服器位址
- 翻譯引擎預設改為 phi4:14b（Microsoft，品質最好）

**改進**
- 翻譯引擎選單重新設計：自動偵測 Ollama 伺服器，連不到時詢問位址或改用 Argos
- 新增中文 Whisper 幻覺過濾（「訂閱」「點贊」「獨播劇場」等 YouTube 訓練資料殘留）
- 重複行偵測移到簡繁轉換之後，避免誤判
- 抑制 Intel MKL SSE4.2 棄用警告（Apple Silicon + Rosetta 環境）
- 選單顯示寬度修正，正確處理中文字元佔位

**文件**
- SOP 新增聚合裝置（Aggregate Device）設定說明，支援同時轉錄自己與對方的聲音
- SOP 新增功能模式說明
- SOP 更新翻譯引擎推薦為 phi4:14b

---

### v1.2.0 (2026-03-03)

**新功能**
- 新增命令列參數支援，可跳過互動式選單直接啟動
- 支援參數：-m (模型)、-s (場景)、-d (音訊裝置)、-e (翻譯引擎)、--llm-model、--llm-host
- 新增 -h / --help 顯示使用說明
- 新增 --list-devices 列出可用音訊裝置
- 不帶參數時維持原有互動式選單行為
- start.sh 支援傳遞命令列參數給主程式

**改進**
- 簡繁轉換改用 OpenCC (s2twp)，取代原本手動 24 組詞彙對照表，轉換更完整
- Argos 離線翻譯輸出現在也會經過簡繁轉換，輸出台灣繁體中文
- install.sh 自動安裝 opencc-python-reimplemented 套件

**文件**
- SOP 新增命令列參數說明與範例
- SOP 新增各場景字幕延遲說明

---

### v1.1.0 (2026-03-02)

**改進**
- 非同步翻譯：英文原文立刻顯示，中文翻譯在背景完成後補上，體感延遲大幅降低
- 檔案輪詢間隔從 0.3 秒縮短至 0.1 秒，反應更即時
- 簡體中文自動轉繁體（24 組高頻 IT 詞彙：软件→軟體、内存→記憶體、服务器→伺服器等）
- Ollama prompt 加強繁體中文要求，明確禁止簡體輸出
- 翻譯引擎選單對齊修正（中英文混排自動計算顯示寬度）
- Argos 標示為「本機離線」，更清楚區分引擎類型
- Whisper 模型名稱欄位加寬，large-v3-turbo 不再溢出
- 使用場景選單加入緩衝長度說明提示
- 標題改為兩行格式（英文名稱 + 作者）
- 加入版本號顯示
- UI 全面改用台灣繁體中文用語

---

### v1.0.0 (2026-03-02)

首次發布。

**功能**
- 即時英文語音轉錄（whisper.cpp whisper-stream）
- 即時英翻繁體中文字幕顯示於終端機
- 支援 Ollama（qwen2.5:14b / 7b）與 Argos 離線雙翻譯引擎
- Ollama 帶上下文翻譯（最近 5 筆），提升前後文連貫性
- 互動式選單：模型 → 場景 → 音訊裝置 → 翻譯引擎
- 三種使用場景預設：線上會議（5s）、教育訓練（8s）、快速字幕（3s）
- 翻譯速度即時標籤（綠 <1s / 黃 <3s / 紅 >=3s）
- 翻譯記錄自動存檔 `translation_YYYYMMDD_HHMMSS.txt`
- Whisper 幻覺過濾（"thank you"、"subscribe" 等靜音假輸出）
- 非中英文輸出過濾 + 自動重試（防止模型輸出俄文/日文）
- 支援自訂 Ollama 伺服器 IP 位址

**安裝**
- 一鍵安裝腳本 `install.sh`，自動處理所有依賴
- 自動偵測 Apple Silicon / Intel 架構，選擇正確的編譯參數
- 路徑搬遷後自動偵測損壞的 venv 和 binary 並重建
- 自動下載 Whisper 模型（預設 large-v3-turbo）

**支援模型**
- Whisper: base.en / small.en / small / large-v3-turbo / medium.en / medium / large-v3
- Ollama: phi4:14b（推薦）/ qwen2.5:14b / qwen2.5:7b
