# jt-live-whisper - 即時英翻中字幕系統 v1.7.8 (by Jason Cheng) - 安裝與使用 SOP

**Author: Jason Cheng (Jason Tools)**

將英文語音即時轉錄並翻譯成繁體中文字幕顯示於終端機。採用 macOS 系統音訊裝置層級擷取，**理論上任何軟體的聲音輸出都能即時處理**：視訊會議（Zoom、Teams、Meet）、YouTube、Podcast、串流影片、教育訓練等，不限定特定應用程式。亦可離線處理音訊檔案。

適用平台：macOS（Apple Silicon）

---

## 一、系統架構

**即時模式：**

```
macOS 系統音訊
  → BlackHole 2ch（虛擬音訊裝置，複製一份音訊給程式）
    → Whisper / Moonshine（即時語音辨識）
      → LLM（Ollama / OpenAI 相容）/ Argos（翻譯）
        → 終端機顯示字幕 + logs/ 記錄檔
```

**離線處理模式（--input）：**

```
音訊檔案（mp3 / wav / m4a / flac 等）
  → ffmpeg 轉檔（→ recordings/ 暫存 16kHz mono WAV）
    → faster-whisper（離線語音辨識）
      → （選配）resemblyzer + spectralcluster（講者辨識）
        → LLM（Ollama / OpenAI 相容）/ Argos（翻譯）
          → 終端機顯示 + logs/ 記錄檔
            → （選配）LLM 摘要 → logs/
```

語音辨識引擎：
- **Whisper**（推薦，預設）：高準確度，完整斷句，支援中英文
- **Moonshine**（替代，僅英文）：真串流架構，延遲 ~300ms
- **faster-whisper**（離線處理專用）：CTranslate2 引擎，Python API，支援 VAD

你仍然可以正常從喇叭或耳機聽到聲音，BlackHole 只是額外複製一份音訊給辨識程式。

**目錄結構：**

```
realtime_voice_translate/
  translate_meeting.py     主程式
  start.sh                 啟動腳本
  install.sh               安裝腳本
  config.json              使用者設定（自動產生）
  logs/                    記錄檔、摘要檔（自動建立）
  recordings/              暫存音訊轉檔（自動建立，處理完自動清除）
  whisper.cpp/             Whisper 引擎
  venv/                    Python 虛擬環境
```

---

## 二、事前準備：macOS 音訊設定

### 2-1. 安裝 BlackHole 虛擬音訊驅動

`./install.sh` 會自動安裝 BlackHole，不需手動執行。

安裝完成後**必須重新啟動電腦**，BlackHole 才會生效。

### 2-2. 建立「多重輸出裝置」

BlackHole 2ch 是虛擬音訊裝置，搭配 macOS「多重輸出裝置」將系統音訊同時送給你的耳機/喇叭和本程式，音訊流向如下：

```
任何應用程式的聲音（Zoom / Teams / Meet / YouTube / Podcast ...）
  │
  ▼
macOS 多重輸出裝置（你建立的）
  ├──▶ MacBook 揚聲器 / AirPods / 耳機（你照常聽到聲音）
  └──▶ BlackHole 2ch（虛擬音訊裝置，無聲複製一份）
         │
         ▼
    jt-live-whisper 讀取 BlackHole 音訊
      → AI 語音辨識 → 翻譯 → 終端機即時字幕
```

1. 開啟 **「音訊 MIDI 設定」**（Audio MIDI Setup）
   - Spotlight 搜尋「音訊 MIDI 設定」，或從 `/Applications/Utilities/Audio MIDI Setup.app` 開啟
2. 點左下角 **「+」** → 選擇 **「建立多重輸出裝置」**
3. 在右側勾選：
   - v 你的喇叭或耳機（例如「MacBook Air 的喇叭」或 AirPods）
   - v **BlackHole 2ch**
4. 確認你的喇叭/耳機排在 BlackHole **上方**（可拖曳調整順序）
5. 勾選 **BlackHole 2ch** 的 **「主裝置」**（Master Device）欄位

![macOS 音訊 MIDI 設定：多重輸出裝置](images/audio-midi-setup.png)

> **重要：主裝置務必選 BlackHole，不要選耳機/喇叭。** BlackHole 是虛擬裝置，永遠不會斷線。如果主裝置設為藍牙耳機（例如 AirPods），一旦耳機斷線，整個多重輸出裝置會失效，導致 Zoom 等應用程式音訊中斷且無法恢復，必須重建裝置或重開機。

### 2-3. 設定音訊輸出

將系統音訊輸出切換到多重輸出裝置，讓 BlackHole 能收到聲音：

1. 打開 **「系統設定」→「聲音」→「輸出」**
2. 選擇剛才建立的 **「多重輸出裝置」**

> **注意：** 多重輸出裝置下無法用系統音量鍵調整音量。如需調整音量，請用應用程式內部的音量控制（如 Google Meet 的音量滑桿）。

> **重要：Zoom / Teams 等視訊軟體的喇叭（輸出）也要設成「多重輸出裝置」，不能直接選 AirPods 或喇叭。** 如果直接選 AirPods，聲音不會經過 BlackHole，程式就收不到對方的聲音。麥克風（輸入）維持原本的設定即可，不需要改。

### 2-4. 建立「聚集裝置」（選配，錄音時需要錄到自己的聲音才需要）

即時轉錄的 ASR 辨識裝置固定使用 BlackHole 2ch（只擷取對方聲音），這樣辨識最準確。但如果你啟用了 `--record` 錄音功能，想要**同時錄下對方和自己的聲音**，就需要建立聚集裝置（Aggregate Device）。

建立步驟：

1. 開啟 **「音訊 MIDI 設定」**（Spotlight 搜尋「音訊 MIDI 設定」）
2. 點左下角 **「+」** → 選擇 **「建立聚集裝置」**（Create Aggregate Device）
3. 勾選：
   - v **BlackHole 2ch**（系統音訊，對方的聲音）
   - v **你的麥克風**（例如「MacBook Air 的麥克風」或 AirPods 麥克風）
4. **時脈來源選 BlackHole 2ch**（虛擬裝置時脈穩定，不會因藍牙斷線而失效）
5. 其他實體裝置勾選 **「偏移修正」**（Drift Correction）
6. 取個好認的名稱，例如「聚集錄音」

![macOS 音訊 MIDI 設定：聚集裝置](images/aggregate-device.png)

> **重要：時脈來源務必選 BlackHole 2ch。** 原因與多重輸出裝置相同：BlackHole 是虛擬裝置，時脈永遠穩定。如果選實體裝置（如 AirPods 或 MacBook 麥克風），藍牙斷線或裝置休眠會導致時脈來源消失，整個聚集裝置跟著失效。

建好之後，程式會自動偵測聚集裝置作為錄音裝置，不需要手動選擇。如果偵測不到聚集裝置，會自動降級使用 BlackHole（僅錄對方聲音）。

**ASR 辨識裝置 vs 錄音裝置的差別：**

| 用途 | 選擇的裝置 | 擷取內容 | 說明 |
|---|---|---|---|
| ASR 即時辨識 | BlackHole 2ch | 僅對方聲音 | 即時字幕只處理對方語音，無法辨識自己的聲音 |
| 錄音 | 聚集裝置 | 對方 + 自己 | 同時錄下雙方聲音，事後用 `--input` 離線轉錄含自己的聲音 |

**Zoom / Teams 的設定不需要改：**

| 設定項目 | 選擇 | 說明 |
|---|---|---|
| Teams/Zoom 喇叭（輸出） | 多重輸出裝置 | 聲音同時送到耳機和 BlackHole |
| Teams/Zoom 麥克風（輸入） | AirPods / 原本的麥克風 | 對方聽到你說話，不受影響 |

完整音訊流向：

```
對方說話 → Teams 輸出 → 多重輸出裝置 → AirPods（你聽到）
                                       → BlackHole（ASR 辨識 + 聚集裝置的一部分）

你說話 → AirPods 麥克風 → Teams 輸入（對方聽到）

錄音時：
  聚集裝置 = BlackHole（對方聲音）+ MacBook 麥克風（你的聲音）
  → 程式同時錄下雙方聲音為 WAV 檔
```

### 2-5. 驗證音訊設定

1. 播放一段英文影片或音訊
2. 確認你的喇叭/耳機有聲音
3. 回到「音訊 MIDI 設定」，確認 BlackHole 2ch 的音量指示器有跳動

---

## 三、安裝程式

### 3-1. 一鍵安裝

打開終端機，貼上這一行即可自動下載並安裝所有元件：

```bash
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh | bash
```

或使用 git clone：

```bash
git clone https://github.com/jasoncheng7115/jt-live-whisper.git
cd jt-live-whisper && ./install.sh
```

安裝腳本會自動檢查並安裝以下項目：

> **首次安裝預估時間：約 10～20 分鐘**（視網路速度而定）。其中 whisper.cpp 編譯和模型下載較耗時：
> - whisper.cpp 編譯：約 3～5 分鐘（需從原始碼編譯 C++ 程式）
> - whisper 模型下載：約 3～10 分鐘（large-v3-turbo 約 809MB）
> - Argos 翻譯模型下載與安裝：約 2～3 分鐘
>
> 編譯過程中終端機會持續輸出訊息，請耐心等待，不要中斷。

| 項目 | 說明 |
|---|---|
| Homebrew | macOS 套件管理器 |
| cmake | 編譯工具 |
| sdl2 | 音訊擷取函式庫 |
| ffmpeg | 音訊轉檔工具（--input 離線處理需要） |
| BlackHole 2ch | 虛擬音訊驅動 |
| Python 3.12 | Python 執行環境 |
| whisper.cpp | 即時語音辨識引擎（自動編譯） |
| whisper 模型 | 語音辨識模型（預設下載 large-v3-turbo） |
| Python venv | 虛擬環境 + ctranslate2、sentencepiece、opencc、sounddevice、numpy、faster-whisper、resemblyzer、spectralcluster |
| Moonshine ASR | 英文串流語音辨識引擎 + medium 模型 (~245MB) |
| Argos 翻譯模型 | 離線英→中翻譯模型 |

全部通過後會顯示：

```
  全部就緒！可以執行 ./start.sh 啟動系統。
```

### 3-2. 升級至最新版本

```bash
./install.sh --upgrade
```

自動從 GitHub 下載最新版本的程式檔案（translate_meeting.py、start.sh、install.sh、SOP.md），不影響現有的 venv、whisper.cpp、模型和設定檔。升級後建議重新執行 `./install.sh` 確認相依套件完整。

### 3-3. 搬遷資料夾後

如果將資料夾搬到其他位置，只需重新執行 `./install.sh`，它會自動偵測並修復損壞的 venv 和 whisper.cpp 編譯。

---

## 四、啟動與使用

### 4-1. 啟動

```bash
./start.sh
```

### 4-2. 命令列參數（跳過選單直接啟動）

除了互動式選單，也可以透過命令列參數直接啟動，跳過所有選單：

```bash
./start.sh [參數...]
```

**可用參數：**

| 參數 | 說明 | 預設值 |
|---|---|---|
| `-h`, `--help` | 顯示說明 | |
| `--mode MODE` | 功能模式 (en2zh / zh2en / en / zh) | en2zh |
| `--asr ASR` | 語音辨識引擎 (whisper / moonshine) | whisper |
| `-m`, `--model MODEL` | Whisper 模型 (large-v3-turbo / large-v3 / small.en / base.en / medium.en) | en2zh: large-v3-turbo / zh: large-v3 |
| `--moonshine-model MODEL` | Moonshine 模型 (medium / small / tiny) | medium |
| `-s`, `--scene SCENE` | 使用場景 (meeting / training / subtitle)，僅 Whisper 即時模式 | training |
| `--topic TOPIC` | 會議主題（提升翻譯品質，例：`--topic 'ZFS 儲存管理'`）。僅翻譯模式有效 | |
| `-d`, `--device ID` | 音訊裝置 ID (數字) | 自動偵測 BlackHole |
| `-e`, `--engine ENGINE` | 翻譯引擎 (ollama / argos，ollama 支援 Ollama 及 OpenAI 相容伺服器) | ollama |
| `--ollama-model NAME` | LLM 翻譯模型名稱 | qwen2.5:14b |
| `--ollama-host HOST` | LLM 伺服器位址，自動偵測 Ollama 或 OpenAI 相容 (支援 host:port 格式) | 192.168.1.40:11434 |
| `--list-devices` | 列出可用音訊裝置後離開 | |
| `--record` | 即時模式同時錄製音訊為 WAV 檔（存入 `recordings/`） | 不錄製 |
| `--rec-device ID` | 錄音裝置 ID，可與 ASR 裝置不同（自動啟用 `--record`） | 自動選擇 |
| `--input FILE [...]` | 離線處理音訊檔（用 faster-whisper 辨識）。不帶 `--mode` 時進入互動選單 | |
| `--diarize` | 講者辨識（需搭配 --input，用 resemblyzer + spectralcluster） | |
| `--num-speakers N` | 指定講者人數（需搭配 --diarize，預設自動偵測 2~8） | |
| `--summarize [FILE ...]` | 摘要模式：讀取記錄檔生成摘要（與 --input 合用時不需指定檔案） | |
| `--summary-model MODEL` | 摘要用的 LLM 模型 | gpt-oss:120b |

**範例：**

```bash
# 查詢可用音訊裝置
./start.sh --list-devices

# 使用預設值，場景為線上會議
./start.sh -s meeting

# 指定模型與場景
./start.sh -m large-v3-turbo -s training

# 全部指定，完全跳過選單
./start.sh -m large-v3-turbo -s training -d 0 -e ollama --ollama-host 192.168.1.40:11434

# 使用 Moonshine 引擎
./start.sh --asr moonshine

# 使用 Whisper 引擎（指定模型和場景）
./start.sh --asr whisper -m large-v3-turbo -s training

# 使用 Moonshine tiny 模型（最快）
./start.sh --asr moonshine --moonshine-model tiny

# 即時模式同時錄音（存入 recordings/）
./start.sh --record

# 即時模式錄音 + 指定模式
./start.sh --record --mode en2zh

# 指定錄音裝置（例如聚集裝置，同時錄雙方聲音）
./start.sh --rec-device 8

# 指定會議主題（提升翻譯品質）
./start.sh --topic 'ZFS 儲存管理'

# 使用離線翻譯
./start.sh -e argos -s subtitle

# 離線處理音訊檔（進入互動選單，選擇模式/辨識/摘要）
./start.sh --input meeting.mp3

# 離線處理（直接執行，跳過選單）
./start.sh --input meeting.mp3 --mode en2zh

# 離線處理（純英文轉錄）
./start.sh --input lecture.wav --mode en

# 離線處理（中文轉錄）
./start.sh --input interview.m4a --mode zh

# 離線處理 + 自動摘要
./start.sh --input meeting.mp3 --summarize

# 批次處理多個音訊檔
./start.sh --input file1.mp3 file2.m4a --mode en2zh

# 離線處理，指定 faster-whisper 模型
./start.sh --input lecture.mp3 -m large-v3

# 離線處理 + 講者辨識
./start.sh --input meeting.mp3 --diarize

# 指定講者人數
./start.sh --input meeting.mp3 --diarize --num-speakers 3

# 講者辨識 + 摘要
./start.sh --input meeting.mp3 --diarize --summarize

# 純英文轉錄 + 講者辨識
./start.sh --input meeting.mp3 --diarize --mode en

# 對記錄檔生成摘要
./start.sh --summarize logs/en2zh_translation_20260303_140000.txt

# 批次摘要多個檔案，指定摘要模型
./start.sh --summarize logs/log1.txt logs/log2.txt --summary-model phi4:14b
```

只要帶任何參數，程式就會進入 CLI 模式，未指定的參數自動使用預設值。不帶任何參數則進入互動式選單。`--input` 不帶 `--mode` 時也會進入互動選單（選擇模式、講者辨識、摘要）。

### 4-3. 互動式選單

![互動式選單](images/interactive-menu.png)

啟動後會依序出現以下選單（都可按 Enter 使用預設值）：

**1) 功能模式**

| 選項 | 說明 |
|---|---|
| **英翻中字幕**（預設） | 英文語音 → 翻譯成繁體中文 |
| 中翻英字幕 | 中文語音 → 翻譯成英文 |
| 英文轉錄 | 英文語音 → 直接顯示英文（不翻譯） |
| 中文轉錄 | 中文語音 → 直接顯示繁體中文（不翻譯） |

選擇「中文轉錄」或「中翻英字幕」時，.en 結尾的模型會自動隱藏，預設使用 large-v3（中文辨識品質最佳）。「英文轉錄」和「英翻中字幕」可使用所有模型，預設 large-v3-turbo。「中翻英字幕」僅支援 LLM 翻譯引擎（不支援 Argos 離線翻譯）。轉錄模式不需要翻譯引擎，會跳過翻譯引擎選擇。

**2) 語音辨識引擎（僅英文模式）**

選擇「英翻中字幕」或「英文轉錄」時，會出現 ASR 引擎選擇：

| 選項 | 說明 |
|---|---|
| **Whisper**（預設） | 高準確度，完整斷句，支援中英文 |
| Moonshine | 真串流架構，延遲極低（~300ms），僅英文 |

選擇 Moonshine 後會進入 Moonshine 模型選擇（不需要選場景），選擇 Whisper 則維持原有的模型和場景選單流程。

中文模式（中文轉錄、中翻英字幕）固定使用 Whisper 引擎。如果 Moonshine 未安裝，會自動使用 Whisper。

**3) 語音辨識模型**

**Moonshine 模型（英文模式）**

| 選項 | 延遲 | 大小 | 說明 |
|---|---|---|---|
| **medium**（預設） | ~300ms | 245MB | 最準確，WER 6.65% |
| small | ~150ms | 123MB | 快速 |
| tiny | ~50ms | 34MB | 最快 |

**Whisper 模型**

| 選項 | 說明 |
|---|---|
| base.en | 最快，準確度一般 |
| small.en | 快，準確度好 |
| **large-v3-turbo**（英翻中預設） | 快，準確度很好 |
| medium.en | 較慢，準確度很好 |
| **large-v3**（中文轉錄預設） | 最慢，中文品質最好 |

> 英翻中模式預設使用 large-v3-turbo；中文轉錄模式預設使用 large-v3（中文辨識品質最佳）。.en 結尾的模型僅支援英文，中文轉錄模式下會自動隱藏。

**4) 使用場景**

| 選項 | 緩衝長度 | 處理間隔 | 適用情境 |
|---|---|---|---|
| 線上會議 | 5 秒 | 3 秒 | 對話短句，反應快 |
| **教育訓練**（預設） | 8 秒 | 3 秒 | 長句連續講述，翻譯更完整 |
| 快速字幕 | 3 秒 | 2 秒 | 最低延遲，適合即時展示 |

> 「緩衝長度」是每次送給 Whisper 辨識的音訊長度，越長句子越完整但延遲越高。「處理間隔」是多久處理一次新的音訊片段。

**字幕延遲說明**

從講者說話到字幕出現，音訊經過以下階段：

```
                        Moonshine 模式              Whisper 模式
                       (推薦，延遲最低)            (準確度最高)
                      ──────────────────        ──────────────────

講者說話                    |                          |
                            v                          v
BlackHole 擷取音訊     即時串流送入模型          累積音訊緩衝 3~8 秒
                            |                          |
                            v                          v
語音辨識               即時辨識 ~0.3 秒           模型推理 ~2.5 秒
                            |                          |
                            v                          v
顯示英文原文           ---- 立即顯示 ----        ---- 立即顯示 ----
                            |                          |
                            v                          v
LLM 翻譯              ~0.3-0.8 秒                ~0.3-0.8 秒
                            |                          |
                            v                          v
顯示中文翻譯           ---- 翻譯完成 ----        ---- 翻譯完成 ----
                      ──────────────────        ──────────────────
總延遲                    ~1-1.5 秒                 ~8-14 秒
```

**Moonshine 模式（推薦，延遲最低）**

真串流架構，音訊即時送入模型，不需要累積緩衝：

```
  時間   0s         1s         2s
         |----------|----------|
  講者   ===說話中===
  辨識       [ ~0.3s ]
  英文             |-> 顯示
  翻譯             [ ~0.5s ]
  中文                 |-> 顯示
                       ^
                 總延遲 ~1-1.5 秒
```

| 模型 | 辨識延遲 | 含翻譯總延遲 |
|---|---|---|
| medium（推薦） | ~300ms | ~1-1.5 秒 |
| small | ~150ms | ~0.5-1 秒 |
| tiny | ~50ms | ~0.5 秒 |

Moonshine 使用內建 VAD（語音活動偵測）自動斷句，不需要設定場景。

**Whisper 模式（準確度最高）**

緩衝視窗架構，需要累積一段音訊才能辨識，延遲較高但斷句更完整：

```
  時間   0s     2s     4s     6s     8s     10s    12s
         |------|------|------|------|------|------|
  講者   ====說話中====
  緩衝       [======累積 8 秒音訊======]
  辨識                                 [推理 ~2.5s]
  英文                                         |-> 顯示
  翻譯                                         [ ~0.5s ]
  中文                                             |-> 顯示
                                                   ^
                                         總延遲 ~8-14 秒（依場景）
```

| 階段 | 延遲 | 說明 |
|---|---|---|
| 音訊緩衝累積 | 3~8 秒 | 依場景設定，越長句子越完整 |
| 處理間隔等待 | 0~3 秒 | 程式每隔 2~3 秒觸發一次辨識 |
| 模型推理 | ~2.5 秒 | large-v3-turbo 在 Apple M2 上的處理時間 |
| LLM 翻譯 | ~0.3-0.8 秒 | qwen2.5:14b 的翻譯時間 |

各場景的預估總延遲（以 large-v3-turbo + LLM 翻譯為例）：

| 場景 | 緩衝長度 | 平均延遲 | 最大延遲 |
|---|---|---|---|
| 快速字幕 | 3 秒 | ~6 秒 | ~8 秒 |
| 線上會議 | 5 秒 | ~8 秒 | ~11 秒 |
| 教育訓練 | 8 秒 | ~10 秒 | ~14 秒 |

延遲主要取決於緩衝長度。如果需要更即時的反應，可選擇「快速字幕」場景，但句子可能較為片段。追求低延遲建議使用 Moonshine 模式。

**5) 翻譯引擎（僅翻譯模式）**

啟動時會自動偵測 LLM 伺服器（預設 `192.168.1.40:11434`），可直接按 Enter 或輸入自訂位址。程式會自動偵測伺服器類型（Ollama 或 OpenAI 相容 API），偵測不到則自動改用 Argos 離線翻譯。

支援的 LLM 伺服器：

| 伺服器 | API 類型 | 預設 port |
|---|---|---|
| Ollama | Ollama 原生 | 11434 |
| LM Studio | OpenAI 相容 | 1234 |
| Jan.ai | OpenAI 相容 | 1337 |
| vLLM | OpenAI 相容 | 8000 |
| LocalAI / llama.cpp | OpenAI 相容 | 8080 |
| LiteLLM | OpenAI 相容 | 4000 |
| text-generation-webui | OpenAI 相容 | 5000 |

Ollama 伺服器的翻譯模型使用作者篩選過的預設清單：

| 選項 | 說明 |
|---|---|
| **qwen2.5:14b**（預設） | 品質好，速度快（推薦） |
| phi4:14b | Microsoft，品質最好 |
| qwen2.5:7b | 品質普通，速度最快 |
| Argos 離線 | 離線翻譯，不需網路，品質普通 |

摘要模型同樣使用作者篩選過的預設清單：

| 選項 | 說明 |
|---|---|
| **gpt-oss:120b**（預設） | 品質最好（推薦） |
| gpt-oss:20b | 速度快，品質好 |

以上模型清單由作者實際測試後篩選，在翻譯品質、速度與中文表現之間取得最佳平衡。如果想使用其他模型，可以在 `config.json` 中加入自訂模型，程式會將自訂模型附加到預設清單後面。範例：

```json
{
  "ollama_host": "192.168.1.40",
  "ollama_port": 11434,
  "translate_models": [
    {"name": "llama3.1:70b", "desc": "Meta，速度較慢但品質好"},
    {"name": "gemma2:27b", "desc": "Google"}
  ],
  "summary_models": [
    {"name": "qwen2.5:32b", "desc": "摘要備用"}
  ]
}
```

每筆自訂模型需包含 `name`（模型名稱），`desc`（說明）為選填。與內建模型名稱相同的項目會自動略過（不會重複）。

OpenAI 相容伺服器的翻譯模型從伺服器取得實際模型清單，直接列出讓使用者選擇。

成功連線後，伺服器位址會自動儲存到 `config.json`，下次啟動不需重新輸入。

LLM 翻譯會自動保留最近 5 筆翻譯作為上下文，讓前後文的翻譯更連貫。

**6) 會議主題（僅翻譯模式，可選）**

翻譯模式（英翻中 / 中翻英）會出現此步驟，轉錄模式（英文 / 中文）跳過。

輸入會議主題後，程式會將主題注入翻譯 prompt，讓 LLM 根據領域上下文翻譯專業術語。例如輸入「ZFS 儲存管理」後，"pool" 會翻譯為「儲存池」而非「游泳池」。直接按 Enter 可跳過，行為與之前完全相同。

CLI 模式使用 `--topic` 參數指定：

```bash
./start.sh --topic 'ZFS 儲存管理'
./start.sh --topic 'K8s 安全架構' --mode en2zh
```

**7) 錄製音訊**

| 選項 | 說明 |
|---|---|
| **不錄製**（預設） | 不儲存音訊 |
| 錄製 | 同時錄製音訊為 WAV 檔，存入 `recordings/` |

**即時辨識的限制：** 即時模式僅處理系統音訊（對方或應用程式的聲音），無法即時辨識麥克風（你自己的聲音）。如需轉錄自己的聲音，請選擇錄製，錄音檔會包含雙方聲音（透過聚集裝置），事後再用 `--input` 離線產出逐字稿與摘要：

```bash
./start.sh --input recordings/recording_20260304_143000.wav --summarize
```

選擇錄製後，程式會自動偵測錄音裝置，不需要手動選擇：
- 優先使用聚集裝置（同時錄到對方與自己的聲音）
- 找不到聚集裝置時降級使用 BlackHole（僅錄對方聲音）
- 都找不到時才顯示手動選單

程式會在即時辨識的同時將音訊存為 16-bit PCM WAV 檔。錄音從開始到停止全程錄在同一個檔案，不會自動切檔。錄音檔名含時間戳，例如 `recording_20260304_143000.wav`。停止時（Ctrl+C 或 Ctrl+S）自動關閉錄音並顯示儲存路徑。

選完錄音後，程式會繼續讓你選擇辨識模型和場景，然後自動偵測 ASR 音訊裝置（BlackHole）並開始辨識。

CLI 模式使用 `--record` 參數啟用（自動選錄音裝置），或用 `--rec-device ID` 指定錄音裝置（會自動啟用錄音）。

### 4-4. 字幕顯示

![即時英翻中字幕運作中](images/realtime-en2zh-1.png)

![即時英翻中字幕運作中](images/realtime-en2zh-2.png)

設定完成後，終端機會即時顯示字幕。英文原文會**立刻顯示**，中文翻譯在背景非同步完成後補上，減少等待感：

```
[EN] So today we're going to talk about the new architecture.  <- 立刻出現
[中] 今天我們要來談談新的架構。                      0.5s     <- 翻好後補上

[EN] The main change is in the authentication layer.           <- 立刻出現
[中] 主要的變更在認證層。                            0.3s     <- 翻好後補上
```

翻譯速度標籤以顏色區分：

| 顏色 | 耗時 | 說明 |
|---|---|---|
| 綠色 | < 1 秒 | 正常 |
| 黃色 | 1～3 秒 | 稍慢 |
| 紅色 | >= 3 秒 | 過慢，建議換用較小模型或檢查網路 |

同時會自動儲存翻譯記錄到 `translation_YYYYMMDD_HHMMSS.txt`。

### 4-5. 自動過濾機制

程式內建多種自動過濾，減少雜訊干擾：

- **Whisper 幻覺過濾**：靜音時 Whisper 可能產生假輸出（如 "thank you"、"subscribe"、"thanks for watching" 等），程式會自動過濾這些常見幻覺文字。
- **非中英文過濾**：LLM 偶爾會輸出俄文、日文等非預期語言，程式會自動偵測並重試翻譯。
- **簡轉繁自動修正**：所有翻譯輸出（LLM 及 Argos）都會經過 OpenCC 簡體→台灣繁體轉換（s2twp），包含字級轉換和台灣用語詞彙轉換（如 软件→軟體、内存→記憶體、服务器→伺服器）。

### 4-6. 停止

- **Ctrl+C**：立即停止程式，不生成摘要
- **Ctrl+S**：停止轉錄並自動生成摘要（校正逐字稿 + 重點摘要）

### 4-7. Ctrl+S 摘要

![AI 會議摘要產出畫面](images/summary-output.png)

轉錄過程中按 `Ctrl+S`，程式會：

1. 停止語音辨識
2. 等待進行中的翻譯完成
3. 讀取翻譯記錄檔
4. 送到 LLM 生成摘要（校正逐字稿 + 重點摘要）
5. 將摘要儲存為獨立檔案
6. 狀態列凍結顯示最終統計，按 ESC 鍵退出

摘要預設使用 `gpt-oss:120b` 模型，可透過 `--summary-model` 參數更換。

摘要檔名規則：

| 原始記錄檔 | 摘要檔 |
|---|---|
| `en2zh_translation_*.txt` | `en2zh_summary_*.txt` |
| `zh2en_translation_*.txt` | `zh2en_summary_*.txt` |
| `en_transcribe_*.txt` | `en_summary_*.txt` |
| `zh_transcribe_*.txt` | `zh_summary_*.txt` |

如果轉錄內容較長（超過約 6000 字），程式會自動分段摘要再合併，避免超過模型的 context window 上限。

### 4-8. --summarize 批次摘要

對已有的翻譯記錄檔進行後處理摘要，不啟動即時轉錄：

```bash
# 單檔摘要
./start.sh --summarize logs/en2zh_translation_20260303_140000.txt

# 多檔批次摘要
./start.sh --summarize logs/log1.txt logs/log2.txt logs/log3.txt

# 指定摘要模型和 LLM 伺服器
./start.sh --summarize logs/log.txt --summary-model phi4:14b --ollama-host 192.168.1.40:11434
```

摘要完成後狀態列會凍結顯示最終統計（時間、tokens、速度），按 ESC 鍵退出。

摘要檔會儲存在 `logs/` 子資料夾下，與記錄檔相同位置。

### 4-9. --input 音訊檔離線處理

![離線處理選單：模式與模型選擇](images/offline-menu-1.png)

![離線處理選單：LLM 伺服器與講者辨識](images/offline-menu-2.png)

對音訊檔案進行離線轉錄和翻譯，不需要 BlackHole 或即時音訊裝置。使用 **faster-whisper**（CTranslate2 引擎）進行辨識，支援 VAD 過濾靜音段。

**互動選單模式：** `--input` 不帶 `--mode` 時，程式會進入三步互動選單，讓使用者選擇功能模式、講者辨識、摘要。帶 `--mode` 則直接執行，不問。

**支援格式：** mp3、wav、m4a、flac 等常見音訊格式（非 wav 格式會自動用 ffmpeg 轉換為 16kHz mono WAV）。可一次指定多個檔案，程式會逐檔處理，搭配 `--summarize` 時合併產出一份摘要。

**基本用法：**

```bash
# 進入互動選單（選擇模式、辨識、摘要）
./start.sh --input meeting.mp3

# 直接執行（跳過選單，英翻中）
./start.sh --input meeting.mp3 --mode en2zh

# 純英文轉錄
./start.sh --input lecture.wav --mode en

# 中文轉錄
./start.sh --input interview.m4a --mode zh

# 中翻英
./start.sh --input chinese_meeting.mp3 --mode zh2en
```

**進階用法：**

```bash
# 轉錄完自動生成摘要
./start.sh --input meeting.mp3 --summarize

# 批次處理多個檔案
./start.sh --input file1.mp3 file2.m4a file3.wav

# 批次處理 + 全部摘要
./start.sh --input file1.mp3 file2.m4a --summarize

# 指定 faster-whisper 模型（預設英文 large-v3-turbo，中文 large-v3）
./start.sh --input lecture.mp3 -m large-v3

# 指定翻譯引擎
./start.sh --input meeting.mp3 -e argos
```

**輸出格式：**

離線處理的記錄檔帶有時間戳記，方便對照原始音訊：

```
[00:05-00:12] [EN] So today we're going to talk about the new architecture.
[00:05-00:12] [中] 今天我們要來談談新的架構。

[00:13-00:20] [EN] The main change is in the authentication layer.
[00:13-00:20] [中] 主要的變更在認證層。
```

記錄檔名格式：`{模式}_{來源檔名}_{YYYYMMDD_HHMMSS}.txt`，例如 `logs/en2zh_translation_meeting_20260303_150000.txt`。所有記錄檔和摘要檔統一存放在 `logs/` 子資料夾。

搭配 `--summarize` 和 `--diarize`，可對匯入的錄音檔產生含講者辨識的摘要與校正逐字稿：

![匯入錄音檔產生的摘要與校正逐字稿](images/offline-summary-diarize.png)

**模型選擇：**

`--input` 模式使用 faster-whisper，支援 `-m` 參數指定模型。模型會在首次使用時自動從 HuggingFace 下載。

| 模型 | 說明 | 預設使用場景 |
|---|---|---|
| large-v3-turbo | 快速，準確度很好 | 英文模式預設 |
| large-v3 | 最準確，中文品質最好 | 中文模式預設 |
| medium | 中等速度和準確度 | |
| small | 較快 | |
| base | 最快 | |

### 4-10. --diarize 講者辨識

對音訊檔進行講者辨識，區分不同講者。使用 **resemblyzer**（d-vector 聲紋特徵提取）+ **spectralcluster**（Google 頻譜分群），不需要 HuggingFace token，在 M2 上處理 30 分鐘音訊的聲紋分群約 30-60 秒。

`--diarize` 需搭配 `--input` 使用，不適用於即時模式。即時模式無法即時辨識講者，因此建議在即時模式啟用錄音功能（`--record`），事後再將錄音檔以 `--input` + `--diarize` 匯入做講者辨識：

```bash
# 步驟 1：即時模式啟用錄音
./start.sh --record

# 步驟 2：事後用錄音檔做講者辨識 + 翻譯 + 摘要
./start.sh --input recordings/recording_20260304_143000.wav --diarize --summarize
```

**基本用法：**

```bash
# 英翻中 + 講者辨識（預設自動偵測講者人數 2~8）
./start.sh --input meeting.mp3 --diarize

# 指定 3 位講者
./start.sh --input meeting.mp3 --diarize --num-speakers 3

# 講者辨識 + 翻譯 + 摘要
./start.sh --input meeting.mp3 --diarize --summarize

# 純英文轉錄 + 講者辨識
./start.sh --input meeting.mp3 --diarize --mode en
```

**輸出格式：**

![講者辨識：不同講者以不同顏色顯示](images/offline-diarize-result.png)

終端機上每位講者以不同顏色顯示（8 色循環），記錄檔為純文字：

```
[00:05-00:12] [Speaker 1] [EN] So today we're going to talk about...
[00:05-00:12] [Speaker 1] [中] 今天我們要來談談...

[00:13-00:20] [Speaker 2] [EN] Can you explain the authentication changes?
[00:13-00:20] [Speaker 2] [中] 你能解釋一下認證的變更嗎？
```

**處理流程：**

1. faster-whisper 辨識所有語音段落（含 VAD 過濾）
2. resemblyzer 對每個段落提取 256 維聲紋向量（d-vector）
3. spectralcluster 對聲紋向量進行頻譜分群
4. 按首次出現順序編號講者（Speaker 1, 2, 3...）
5. 翻譯並輸出帶講者標籤的記錄檔

**注意事項：**

- 段落太短（< 0.5 秒）會嘗試擴展，仍不足則繼承相鄰講者
- 首次使用 resemblyzer 會自動下載聲紋模型（約 17MB）
- `--num-speakers` 不搭配 `--diarize` 時會顯示警告並忽略
- 如果分群失敗，所有段落會降級標記為 Speaker 1

---

## 五、使用流程總結

**即時轉錄：**

1. **確認音訊輸出已切換到「多重輸出裝置」**（系統設定 → 聲音 → 輸出）
2. 開啟終端機，執行 `./start.sh`
3. 按 Enter 使用預設選項（或依需求調整）
4. 開始你的會議或播放英文內容
5. 終端機即時顯示英文原文與中文翻譯
6. 結束後按 `Ctrl+C`（直接停止）或 `Ctrl+S`（停止並生成摘要），翻譯記錄自動儲存

**離線處理音訊檔：**

1. 準備好音訊檔案（mp3、wav、m4a、flac 等）
2. 執行 `./start.sh --input 檔案路徑`（可加 `--mode`、`--diarize`、`--summarize`）
3. 程式自動轉檔、辨識、（講者辨識）、翻譯，完成後輸出記錄檔

---

## 六、常見問題

### Q: 找不到音訊裝置？
確認 BlackHole 2ch 已安裝且電腦已重新啟動。執行 `./install.sh` 檢查。

### Q: 有偵測到 BlackHole 但沒有辨識到任何語音？
確認系統音訊輸出已切換到「多重輸出裝置」，而不是直接輸出到喇叭/耳機。

### Q: 翻譯品質不好？
- 確認使用 LLM 翻譯引擎（而非 Argos 離線翻譯）
- 推薦使用 `phi4:14b` 模型（品質最好）

### Q: 辨識速度太慢？
- 改用 Moonshine 引擎（`--asr moonshine`），延遲從 8-14 秒降至 1-3 秒
- 如果使用 Whisper：確認已編譯為原生架構、選擇「快速字幕」場景、改用較小模型

### Q: 搬遷資料夾後程式無法執行？
重新執行 `./install.sh`，它會自動偵測並修復。

### Q: 沒有 Ollama 伺服器怎麼辦？
程式會自動偵測 LLM 伺服器類型（支援 Ollama 及所有 OpenAI 相容伺服器，如 LM Studio、vLLM、llama.cpp 等），連不到任何 LLM 伺服器時會改用 Argos 離線翻譯（不需網路）。

### Q: --input 找不到 ffmpeg？
執行 `brew install ffmpeg` 安裝，或重新執行 `./install.sh`（會自動安裝）。

### Q: --input 找不到 faster-whisper？
重新執行 `./install.sh`（會自動安裝 faster-whisper 套件），或手動執行 `pip install faster-whisper`。

### Q: --diarize 找不到 resemblyzer 或 spectralcluster？
重新執行 `./install.sh`（會自動安裝），或手動執行 `pip install resemblyzer spectralcluster`。

### Q: AirPods 或藍牙耳機的麥克風消失了？
AirPods 已連線但在系統設定的「聲音 → 輸入」看不到麥克風，這是 macOS 藍牙音訊偶爾會出現的問題，依序嘗試以下方法：

1. 把 AirPods 放回充電盒，等 10 秒再拿出來重新連線
2. 到「系統設定 → 藍牙」，中斷 AirPods 連線後重新連接
3. 開啟「音訊 MIDI 設定」確認 AirPods 裝置是否有出現
4. 在終端機重啟 macOS 音訊服務：
   ```bash
   sudo killall coreaudiod
   ```
5. 還是不行，重啟藍牙服務：
   ```bash
   sudo pkill bluetoothd
   ```
   等幾秒讓 AirPods 重新連上即可。
6. 以上都無效時，重新啟動電腦。

> 這個問題與本程式無關，是 macOS 藍牙音訊的已知問題。

### Q: --diarize 辨識出的講者數不正確？
使用 `--num-speakers N` 指定正確的講者人數，例如 `--diarize --num-speakers 2`。自動偵測適用於大多數情況，但講者聲音相似或音訊品質不佳時可能需要手動指定。

---

## 七、檔案說明

| 檔案 | 說明 |
|---|---|
| `install.sh` | 安裝腳本，檢查並安裝所有依賴 |
| `start.sh` | 啟動腳本 |
| `translate_meeting.py` | 主程式 |
| `whisper.cpp/` | Whisper 語音辨識引擎（自動下載編譯） |
| `venv/` | Python 虛擬環境（自動建立） |
| `config.json` | 使用者設定檔（自動產生，儲存 LLM 伺服器位址、自訂模型等） |
| `en2zh_translation_*.txt` / `zh2en_translation_*.txt` / `en_transcribe_*.txt` / `zh_transcribe_*.txt` | 翻譯/轉錄記錄檔（自動產生） |
| `en2zh_summary_*.txt` / `zh2en_summary_*.txt` / `en_summary_*.txt` / `zh_summary_*.txt` | 摘要檔（Ctrl+S 或 --summarize 產生） |
| `SOP.md` | 本文件 |

---

## 八、Changelog

### v1.7.8 (2026-03-04)

**新功能**
- 新增「會議主題」功能（`--topic`），可在啟動時輸入會議主題或領域，注入翻譯 prompt 提升專業術語翻譯品質
- 互動選單翻譯模式新增「會議主題」步驟（可選，按 Enter 跳過）
- 啟動 banner 顯示目前會議主題（如有設定）

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
- 錄音檔為 16-bit PCM WAV 格式，檔名含時間戳（如 `recording_20260304_143000.wav`）
- 停止時（Ctrl+C 或 Ctrl+S）自動關閉錄音並顯示儲存路徑
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
- 新增 Ctrl+S 摘要：轉錄中按 Ctrl+S 停止並自動生成摘要（校正逐字稿 + 重點摘要）
- 新增 --summarize 批次摘要：對已有的記錄檔進行後處理摘要，不啟動即時轉錄
- 新增 --summary-model 參數，可指定摘要用的 Ollama 模型（預設 gpt-oss:120b）
- 長文自動分段摘要：自動偵測模型 context window 動態決定分段大小

**改進**
- 底部固定狀態列：即時顯示經過時間、翻譯筆數、快捷鍵提示（不被字幕捲走）
- 摘要輸出 Markdown 彩色渲染（標題、列表、粗體各有顏色）
- 摘要完成後自動用系統編輯器開啟摘要檔
- 選單分隔線寬度統一為 60 字元，與程式標題等寬
- 音訊裝置選單修正：只標示實際會被自動選中的裝置為「預設」
- 使用 termios 停用 IXON 釋放 Ctrl+S 按鍵，不影響互動式選單
- atexit + stty sane 雙重安全網確保終端機一定恢復正常
- 摘要檔名自動依原始記錄檔類型命名（en2zh_summary_* / zh2en_summary_* / zh_summary_*）

**文件**
- SOP 新增 Ctrl+S 摘要使用說明
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
- 支援參數：-m (模型)、-s (場景)、-d (音訊裝置)、-e (翻譯引擎)、--ollama-model、--ollama-host
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
- Whisper: base.en / small.en / large-v3-turbo / medium.en / large-v3
- Ollama: phi4:14b（推薦）/ qwen2.5:14b / qwen2.5:7b
