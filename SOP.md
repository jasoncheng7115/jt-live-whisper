# jt-live-whisper 安裝與使用 SOP

即時英翻中字幕系統 v2.0.9 (by Jason Cheng)

將英文語音即時轉錄並翻譯成繁體中文字幕顯示於終端機。採用 macOS 系統音訊裝置層級擷取，**理論上任何軟體的聲音輸出都能即時處理**：視訊會議（Zoom、Teams、Meet）、YouTube、Podcast、串流影片、教育訓練等，不限定特定應用程式。亦可離線處理音訊檔案。

適用平台：macOS（Apple Silicon）

**全地端運行，不依賴雲端服務。** 所有語音辨識、翻譯、摘要皆在自有設備上完成，音訊資料不會離開你的網路環境。有兩種部署方式：

- **單機模式**： 一台 MacBook / Mac mini 即可完成所有處理。語音辨識（Whisper/Moonshine）、翻譯（LLM/Argos）全部在本機 CPU 執行，不需要額外硬體。適合個人使用、外出攜帶。

- **本機 + GPU 伺服器模式**： MacBook / Mac mini 負責音訊擷取與介面操作，語音辨識和講者辨識交由區域網路內的 GPU 伺服器（如 DGX Spark、Ubuntu + NVIDIA GPU）處理。離線辨識速度快 5-10 倍，仍然是全地端架構，資料僅在區域網路內傳輸。適合需要處理大量音訊或追求即時辨識品質的場景。

兩種模式可隨時切換，GPU 伺服器離線時自動降級為本機處理，不中斷使用。

---

## 一、系統架構

**即時模式：**

```
macOS 系統音訊
  → BlackHole 2ch（虛擬音訊裝置，複製一份音訊給程式）
    → Whisper / Moonshine（即時語音辨識）           ← 本機 CPU 或遠端 GPU
      → LLM（Ollama / OpenAI 相容）/ Argos（翻譯）
        → 終端機顯示字幕 + logs/ 記錄檔
```

**離線處理模式（--input）：**

```
音訊檔案（mp3 / wav / m4a / flac 等）
  → ffmpeg 轉檔（→ recordings/ 暫存 16kHz mono WAV）
    → faster-whisper（離線語音辨識）                 ← 本機 CPU 或遠端 GPU
      → （選配）resemblyzer + spectralcluster（講者辨識） ← 本機 CPU 或遠端 GPU
        → LLM（Ollama / OpenAI 相容）/ Argos（翻譯）
          → 終端機顯示 + logs/ 記錄檔
            → （選配）LLM 摘要 → logs/
```

**遠端 GPU 伺服器架構（選配）：**

```
[本機 macOS]                          [遠端 Linux + NVIDIA GPU]

translate_meeting.py                  remote_whisper_server.py (FastAPI)
  - 音訊擷取 / 轉檔                    - /v1/audio/transcriptions (ASR)
  - 上傳音訊到遠端    --- HTTP --->     - /v1/audio/diarize (講者辨識)
  - 接收辨識結果      <-- JSON ---     - faster-whisper + GPU CUDA
  - LLM 翻譯 / 顯示 / 儲存            - resemblyzer + GPU CUDA
  - SSH 啟停遠端伺服器  --- SSH --->
```

有設定遠端 GPU 時，語音辨識和講者辨識自動在遠端執行（離線 30 分鐘音訊：本機 CPU 約 3-5 分鐘，遠端 GPU 約 10-30 秒）。遠端失敗時自動降級本機 CPU。多個客戶端可同時共用同一個遠端伺服器。

使用的 AI 模型：

| 用途 | AI 模型 | 執行位置 |
|------|---------|----------|
| 語音辨識 (即時) | **Whisper** (OpenAI) | 本機 CPU 或遠端 GPU |
| 語音辨識 (即時) | **Moonshine** (Useful Sensors) | 僅限本機 CPU |
| 語音辨識 (離線) | **faster-whisper** (CTranslate2) | 本機 CPU 或遠端 GPU |
| 講者辨識 | **resemblyzer** + **spectralcluster** | 本機 CPU 或遠端 GPU |
| 翻譯 / 摘要 | **Qwen 2.5** / **Phi-4** 等 LLM | 本機或區域網路 LLM 伺服器 |
| 翻譯 (離線備援) | **Argos Translate** | 僅限本機 |

語音辨識引擎：
- **Whisper**（推薦，預設）：高準確度，完整斷句，支援中英文，可在本機或遠端 GPU 伺服器執行
- **Moonshine**（替代，僅英文）：真串流架構，延遲 ~300ms（僅限本機）
- **faster-whisper**（離線處理專用）：CTranslate2 引擎，Python API，支援 VAD，可在本機或遠端 GPU 伺服器執行

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

![系統設定 → 聲音 → 輸出：選擇多重輸出裝置](images/sound-output-setting.png)

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

**本機 Mac 安裝項目：**

| 項目 | 說明 |
|---|---|
| [Homebrew](https://brew.sh/) | macOS 套件管理器（需事先安裝，安裝腳本不會自動安裝） |
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

**遠端 GPU 語音辨識伺服器（選填）：**

安裝最後會詢問是否設定遠端 GPU 語音辨識伺服器。若有 NVIDIA GPU 伺服器（如 DGX Spark / Ubuntu + CUDA），安裝腳本會透過 SSH 自動在遠端安裝以下套件，大幅加速語音辨識和講者辨識：

| 項目 | 說明 |
|---|---|
| PyTorch (CUDA) | GPU 加速框架（自動偵測 CUDA 版本選擇對應 wheel） |
| CTranslate2 / faster-whisper | Whisper 語音辨識引擎（GPU 加速版） |
| resemblyzer + spectralcluster | 講者辨識套件（GPU 加速聲紋提取） |
| FastAPI + uvicorn | 辨識 API 伺服器 |
| remote_whisper_server.py | 遠端辨識服務程式（自動部署） |

未設定遠端 GPU 時，所有語音辨識在本機 CPU 執行，功能完全相同但速度較慢。

**磁碟空間需求（本機 Mac）：** 最小安裝約 3 GB（venv + 1 個 Whisper 模型 + 基本套件），推薦 8 GB 以上（含 HuggingFace 快取供離線處理用），完整安裝約 14 GB（全部模型 + Moonshine）。安裝腳本會在安裝前自動檢查可用空間。

**磁碟空間需求（遠端 GPU 伺服器）：** 最小安裝約 5 GB（PyTorch + 1 個模型），完整安裝約 12 GB（PyTorch + 全部 5 個模型 + 講者辨識套件）。

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
| `-e`, `--engine ENGINE` | 翻譯引擎 (llm / argos，llm 支援 Ollama 及 OpenAI 相容伺服器) | llm |
| `--llm-model NAME` | LLM 翻譯模型名稱 | qwen2.5:14b |
| `--llm-host HOST` | LLM 伺服器位址，自動偵測 Ollama 或 OpenAI 相容 (支援 host:port 格式) | 無（需設定） |
| `--list-devices` | 列出可用音訊裝置後離開 | |
| `--record` | 即時模式同時錄製音訊（存入 `recordings/`，預設 MP3） | 不錄製 |
| `--rec-device ID` | 錄音裝置 ID，可與 ASR 裝置不同（自動啟用 `--record`） | 自動選擇 |
| `--input FILE [...]` | 離線處理音訊檔（用 faster-whisper 辨識）。不帶 `--mode` 時進入互動選單 | |
| `--diarize` | 講者辨識（需搭配 --input，用 resemblyzer + spectralcluster，有遠端 GPU 時自動遠端執行） | |
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
./start.sh -m large-v3-turbo -s training -d 0 -e llm --llm-host 192.168.1.40:11434

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
./start.sh --summarize logs/英翻中_逐字稿_20260303_140000.txt

# 批次摘要多個檔案，指定摘要模型
./start.sh --summarize logs/log1.txt logs/log2.txt --summary-model phi4:14b
```

只要帶任何參數，程式就會進入 CLI 模式，未指定的參數自動使用預設值。不帶任何參數則進入互動式選單。`--input` 不帶 `--mode` 時也會進入互動選單（選擇模式、講者辨識、摘要）。互動式選單第一步為「輸入來源」，可選擇即時音訊擷取或從 `recordings/` 讀入已有的錄音檔。

### 4-3. 互動式選單

![互動式選單](images/interactive-menu.png)

啟動後會依序出現以下選單（都可按 Enter 使用預設值）：

**0) 輸入來源**

| 選項 | 說明 |
|---|---|
| **即時音訊擷取**（預設） | 擷取系統播放音訊進行即時辨識翻譯 |
| 讀入音訊檔案 | 從 `recordings/` 目錄選擇已有的錄音檔進行離線處理 |

選擇「讀入音訊檔案」時，會列出 `recordings/` 目錄下最新 10 個音訊檔（.wav/.mp3/.m4a/.flac/.ogg），顯示檔名、大小、修改時間，預設選最新的檔案。選擇檔案後進入離線處理互動選單（功能模式、辨識模型、翻譯、講者辨識、摘要）。若目錄內無音訊檔，會提示並回到輸入來源選單。

選擇「即時音訊擷取」則進入以下即時模式選單流程：

**1) 功能模式**

| 選項 | 說明 |
|---|---|
| **英翻中字幕**（預設） | 英文語音 → 翻譯成繁體中文 |
| 中翻英字幕 | 中文語音 → 翻譯成英文 |
| 英文轉錄 | 英文語音 → 直接顯示英文（不翻譯） |
| 中文轉錄 | 中文語音 → 直接顯示繁體中文（不翻譯） |
| 純錄音 | 僅錄製音訊（不做辨識或翻譯），預設 MP3 格式 |

選擇「純錄音」時，跳過 ASR 引擎、翻譯引擎、模型、場景等所有設定，自動偵測錄音裝置後直接開始錄音。錄音期間顯示即時音量波形圖，按 Ctrl+C 停止並儲存。此模式在離線處理（讀入音訊檔案）選單中不會出現。

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

| 階段 | Moonshine (推薦，延遲最低) | Whisper (準確度最高) |
|---|---|---|
| 音訊擷取 | 即時串流送入模型 | 累積音訊緩衝 3~8 秒 |
| 語音辨識 | 即時辨識 ~0.3 秒 | 模型推理 ~2.5 秒 |
| 顯示英文原文 | 立即顯示 | 立即顯示 |
| LLM 翻譯 | ~0.3-0.8 秒 | ~0.3-0.8 秒 |
| 顯示中文翻譯 | 翻譯完成 | 翻譯完成 |
| **總延遲** | **~1-1.5 秒** | **~8-14 秒** |

**Moonshine 模式（推薦，延遲最低）**

真串流架構，音訊即時送入模型，不需要累積緩衝：

```
          0s        1s        2s
          |---------|---------|
  speech  ===talking===
  ASR       [~0.3s]
  EN              |-> display
  LLM             [~0.5s]
  ZH                    |-> display
                         ^
                  total ~1-1.5s
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
          0s     2s     4s     6s     8s     10s    12s
          |------|------|------|------|------|------|
  speech  ====talking====
  buffer      [========= 8s buffer =========]
  ASR                                        [~2.5s ASR]
  EN                                                    |-> display
  LLM                                                   [~0.5s]
  ZH                                                          |-> display
                                                               ^
                                                    total ~8-14s
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

若已在 `config.json` 設定 LLM 伺服器（或透過 `--llm-host` 指定），啟動時會自動偵測並連線。未設定時可手動輸入伺服器位址，或按 Enter 使用 Argos 離線翻譯。程式會自動偵測伺服器類型（Ollama 或 OpenAI 相容 API）。

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
  "llm_host": "192.168.1.40",
  "llm_port": 11434,
  "recording_format": "mp3",
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
| 錄製 | 同時錄製音訊，存入 `recordings/`（預設 MP3 格式） |

**即時辨識的限制：** 即時模式僅處理系統音訊（對方或應用程式的聲音），無法即時辨識麥克風（你自己的聲音）。如需轉錄自己的聲音，請選擇錄製，錄音檔會包含雙方聲音（透過聚集裝置），事後再用 `--input` 離線產出逐字稿與摘要：

```bash
./start.sh --input recordings/錄音_20260304_143000.mp3 --summarize
```

選擇錄製後，程式會自動偵測錄音裝置，不需要手動選擇：
- 優先使用聚集裝置（同時錄到對方與自己的聲音）
- 找不到聚集裝置時降級使用 BlackHole（僅錄對方聲音）
- 都找不到時才顯示手動選單

程式會在即時辨識的同時錄製音訊。錄音期間以 WAV 格式暫存（每 30 秒更新 header，即使異常終止也能保留音訊），停止時（Ctrl+C）自動轉檔為目標格式並刪除中間 WAV 檔。預設輸出 MP3（近無損品質 VBR ~220-260kbps），可透過 `config.json` 設定為其他格式：

```json
{
  "recording_format": "mp3"
}
```

支援的格式：`mp3`（預設）、`ogg`、`flac`、`wav`。設為 `wav` 時維持原始 16-bit PCM 不轉檔。轉檔失敗時會保留原始 WAV 檔，不影響程式運作。

錄音從開始到停止全程錄在同一個檔案，不會自動切檔。錄音檔名含時間戳，例如 `錄音_20260304_143000.mp3`。

選完錄音後，程式會繼續讓你選擇辨識模型和場景，然後自動偵測 ASR 音訊裝置（BlackHole）並開始辨識。

CLI 模式使用 `--record` 參數啟用（自動選錄音裝置），或用 `--rec-device ID` 指定錄音裝置（會自動啟用錄音）。

### 4-4. 字幕顯示

![即時英翻中字幕運作中](images/realtime-en2zh-1.png)

![即時英翻中字幕運作中](images/realtime-en2zh-2.png)

![即時英翻中：翻譯速度標籤與音訊波形](images/realtime-en2zh-3.png)

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

- **Ctrl+C**：停止轉錄，翻譯記錄自動儲存

### 4-7. --summarize 批次摘要

對已有的翻譯記錄檔進行後處理摘要，不啟動即時轉錄：

```bash
# 單檔摘要
./start.sh --summarize logs/英翻中_逐字稿_20260303_140000.txt

# 多檔批次摘要
./start.sh --summarize logs/log1.txt logs/log2.txt logs/log3.txt

# 指定摘要模型和 LLM 伺服器
./start.sh --summarize logs/log.txt --summary-model phi4:14b --llm-host 192.168.1.40:11434
```

摘要完成後狀態列會凍結顯示最終統計（時間、tokens、速度），按 ESC 鍵退出。

摘要檔會儲存在 `logs/` 子資料夾下，與記錄檔相同位置。

### 4-9. --input 音訊檔離線處理

![離線處理選單：模式與模型選擇](images/offline-menu-1.png)

![離線處理選單：LLM 伺服器與講者辨識](images/offline-menu-2.png)

![離線處理選單：設定總覽與等效 CLI 指令](images/offline-menu-3.png)

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

記錄檔名格式：`{模式}_{來源檔名}_{YYYYMMDD_HHMMSS}.txt`，例如 `logs/英翻中_逐字稿_meeting_20260303_150000.txt`。所有記錄檔和摘要檔統一存放在 `logs/` 子資料夾。

搭配 `--summarize` 和 `--diarize`，可對匯入的錄音檔產生含講者辨識的摘要與校正逐字稿：

![匯入錄音檔產生的摘要與校正逐字稿](images/offline-summary-diarize.png)

![時間逐字稿 HTML](images/offline-transcript.png)

時間逐字稿 HTML 內嵌音訊播放器與波形圖，可直接點選波形任意位置跳至該時間點；播放時對應的逐字稿段落會即時以高亮區塊標示，方便對照聆聽。

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

對音訊檔進行講者辨識，區分不同講者。使用 **resemblyzer**（d-vector 聲紋特徵提取）+ **spectralcluster**（Google 頻譜分群），不需要 HuggingFace token。在 M2 上處理 30 分鐘音訊約 30-60 秒；有遠端 GPU 設定時自動使用遠端 GPU 執行，速度可加快到 5-10 秒。遠端失敗會自動降級本機 CPU。

`--diarize` 需搭配 `--input` 使用，不適用於即時模式。即時模式無法即時辨識講者，因此建議在即時模式啟用錄音功能（`--record`），事後再將錄音檔以 `--input` + `--diarize` 匯入做講者辨識：

```bash
# 步驟 1：即時模式啟用錄音
./start.sh --record

# 步驟 2：事後用錄音檔做講者辨識 + 翻譯 + 摘要
./start.sh --input recordings/錄音_20260304_143000.mp3 --diarize --summarize
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

![講者辨識：終端機逐字稿輸出](images/offline-diarize-result-2.png)

終端機上每位講者以不同顏色顯示（8 色循環），記錄檔為純文字：

```
[00:05-00:12] [Speaker 1] [EN] So today we're going to talk about...
[00:05-00:12] [Speaker 1] [中] 今天我們要來談談...

[00:13-00:20] [Speaker 2] [EN] Can you explain the authentication changes?
[00:13-00:20] [Speaker 2] [中] 你能解釋一下認證的變更嗎？
```

**處理流程：**

1. faster-whisper 辨識所有語音段落（含 VAD 過濾，可在本機或遠端 GPU 執行）
2. resemblyzer 對每個段落提取 256 維聲紋向量（d-vector）
3. spectralcluster 對聲紋向量進行頻譜分群
4. 按首次出現順序編號講者（Speaker 1, 2, 3...）
5. 翻譯並輸出帶講者標籤的記錄檔

步驟 2-3 有遠端 GPU 設定時，自動上傳音訊到遠端 `/v1/audio/diarize` API 執行，遠端失敗則降級本機。

**注意事項：**

- 段落太短（< 0.5 秒）會嘗試擴展，仍不足則繼承相鄰講者
- 首次使用 resemblyzer 會自動下載聲紋模型（約 17MB）
- `--num-speakers` 不搭配 `--diarize` 時會顯示警告並忽略
- 如果分群失敗，所有段落會降級標記為 Speaker 1
- 遠端 GPU 執行需先透過 `install.sh` 在遠端安裝 resemblyzer + spectralcluster

---

## 五、使用流程總結

**即時轉錄：**

1. **確認音訊輸出已切換到「多重輸出裝置」**（系統設定 → 聲音 → 輸出）
2. 開啟終端機，執行 `./start.sh`
3. 按 Enter 使用預設選項（或依需求調整）
4. 開始你的會議或播放英文內容
5. 終端機即時顯示英文原文與中文翻譯
6. 結束後按 `Ctrl+C` 停止，翻譯記錄自動儲存

**離線處理音訊檔：**

1. 準備好音訊檔案（mp3、wav、m4a、flac 等）
2. 執行 `./start.sh --input 檔案路徑`（可加 `--mode`、`--diarize`、`--summarize`）
3. 程式自動轉檔、辨識、（講者辨識）、翻譯，完成後輸出記錄檔

### 互動選單流程圖

```
  ./start.sh
      |
      v
  [輸入來源]
      |
      +--> 即時音訊擷取 --> (即時模式)
      |
      +--> 讀入音訊檔案 --> (離線模式)


  ==================== 即時模式 ====================

  [功能模式] en2zh / zh2en / en / zh / record
      |
      +--> record --> [錄音來源] --> run_record_only() --> 結束
      |
      v
  (有遠端 GPU 設定？)
      |
      +--> 無 --> 直接進入本機流程
      |
      v
  [辨識位置]
      |
      +--> 本機 --------> (本機流程)
      |
      +--> 遠端 GPU ----> (遠端流程)


  ---------- 本機流程 ----------

  (en2zh / en 模式？)
      |
      +--> 是 --> [ASR 引擎]
      |           |
      |           +--> Whisper ----> (路線 A)
      |           |
      |           +--> Moonshine --> (路線 B)
      |
      +--> 否 (zh/zh2en) --> Whisper --> (路線 A)


  路線 A - Whisper:

      (翻譯模式？) --> [翻譯引擎] LLM / Argos
                       [LLM 模型] [會議主題]
      [是否錄音]
      [Whisper 模型] --> [使用場景]
      [音訊裝置 SDL2]
          |
          v
      run_stream()


  路線 B - Moonshine:

      (翻譯模式？) --> [翻譯引擎] LLM / Argos
                       [LLM 模型] [會議主題]
      [是否錄音]
      [Moonshine 模型]
      [音訊裝置 PortAudio]
          |
          v
      run_stream_moonshine()


  ---------- 遠端 GPU 流程 ----------
  (固定 Whisper，不支援 Moonshine)

      (翻譯模式？) --> [翻譯引擎] LLM / Argos
                       [LLM 模型] [會議主題]
      [是否錄音]
      [辨識模型 (遠端 GPU)]
          顯示 [遠端已快取] / [遠端需下載] 標籤
      [音訊裝置 PortAudio]
          |
          v
      啟動遠端伺服器 --> 載入模型到 GPU
          |
          v
      run_stream_remote()


  ==================== 離線模式 ====================

      [選擇音訊檔]
          |
          v
      [功能模式] en2zh / zh2en / en / zh
          |
          v
      [辨識模型]
          顯示 [遠端已快取] / [遠端需下載] 標籤 (有遠端設定時)
          |
          v
      [辨識位置] 遠端 GPU / 本機 CPU
          |
          v
      (翻譯模式？) --> [LLM 伺服器] --> [翻譯模型]
          |
          v
      [講者辨識] 不辨識 / 自動偵測 / 指定人數
          有遠端 GPU 時自動使用遠端執行
          |
          v
      [摘要] 不摘要 / 產生摘要
          |
          +--> 產生摘要 --> [摘要模型]
          |
          v
      [會議主題 (選填)]
          |
          v
      process_audio_file()
```

---

## 六、常見問題

### Q: 找不到音訊裝置？
確認 BlackHole 2ch 已安裝且電腦已重新啟動。執行 `./install.sh` 檢查。

### Q: 有偵測到 BlackHole 但沒有辨識到任何語音？
確認系統音訊輸出已切換到「多重輸出裝置」，而不是直接輸出到喇叭/耳機。

### Q: 應用程式沒有提供音訊輸出裝置的選項，怎麼讓它走多重輸出裝置？
到 **系統設定 → 聲音 → 輸出** 選擇「多重輸出裝置」。大多數應用程式（如 YouTube、Podcast、串流影片等）會直接使用系統預設的音訊輸出，只要系統層級切過去就行，不需要在個別應用程式內設定。只有 Zoom、Teams 等視訊會議軟體會有自己的音訊輸出選項，才需要另外在軟體內手動選。

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

### Q: 為什麼講者辨識不用 pyannote.audio？

pyannote.audio 是目前最知名的講者辨識框架，準確度確實較高，但有以下限制：

- **授權限制**：pyannote 的預訓練模型採用特殊授權條款，限制了可使用的用途與場景，部分模型不允許商業使用
- **需要 HuggingFace 帳號與 Token**：使用前必須在 HuggingFace 網站註冊帳號、申請存取權限、產生 API Token，並在本機設定 Token 才能下載模型，增加了安裝門檻
- **不符合全地端理念**：需要在第三方平台註冊帳號並同意授權條款，與本工具「零帳號、零註冊、完全地端」的設計理念不符

本工具使用的 resemblyzer + spectralcluster 組合：

- 完全開源，無授權限制
- 不需要任何帳號或 Token，安裝即可使用
- 模型自動下載，無需手動申請存取權限
- 在大多數 2-3 人的對話場景下表現良好

### Q: 為什麼不支援 ChatGPT、Gemini、Claude 等雲端大語言模型？

本工具的設計理念就是 100% 全地端運行，所有 AI 模型（語音辨識、翻譯、摘要）皆在自有設備上執行，資料不經過任何雲端服務。如果要使用雲端模型，直接使用現有的雲端服務即可（例如 Google NotebookLM、ChatGPT 等），不需要透過本工具。

### Q: 可以用 --input 處理影片檔嗎？
可以。`--input` 支援任何 ffmpeg 能解碼的格式，包含 mp4、mkv、avi、webm 等影片檔。程式會自動用 ffmpeg 提取音軌並轉換為 16kHz mono WAV，再進行辨識與翻譯：

```bash
# 影片檔辨識 + 翻譯
./start.sh --input video.mp4

# 影片檔 + 講者辨識 + 摘要
./start.sh --input meeting_recording.mkv --diarize --summarize
```

---

## 七、檔案說明

| 檔案 | 說明 |
|---|---|
| `install.sh` | 安裝腳本，檢查並安裝所有依賴（含遠端 GPU 伺服器部署） |
| `start.sh` | 啟動腳本 |
| `translate_meeting.py` | 主程式（本機端） |
| `remote_whisper_server.py` | 遠端 GPU 伺服器程式（FastAPI，由 install.sh 自動部署到遠端） |
| `whisper.cpp/` | Whisper 語音辨識引擎（自動下載編譯） |
| `venv/` | Python 虛擬環境（自動建立） |
| `config.json` | 使用者設定檔（自動產生，含 LLM 伺服器位址、遠端 GPU 設定、錄音格式等） |
| `英翻中_逐字稿_*.txt` / `中翻英_逐字稿_*.txt` / `英文_逐字稿_*.txt` / `中文_逐字稿_*.txt` | 翻譯/轉錄記錄檔（自動產生） |
| `英翻中_摘要_*.txt` / `中翻英_摘要_*.txt` / `英文_摘要_*.txt` / `中文_摘要_*.txt` | 摘要檔（--summarize 產生） |
| `SOP.md` | 本文件 |

---

## 八、Changelog

### v2.0.9 (2026-03-08)

**改善**
- LLM 伺服器預設無連線，需透過 config.json 設定或 --llm-host 指定（不再硬編碼預設 IP）
- config.json 欄位名稱統一為 `llm_host` / `llm_port`（向後相容舊欄位 `ollama_host` / `ollama_port`）
- 未設定 LLM 伺服器時，互動選單提示輸入位址或按 Enter 使用離線翻譯

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
- 摘要狀態列顯示 LLM 伺服器位置（本機/遠端），與 ASR 狀態列風格一致
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
- 遠端伺服器啟動指令自動加入 `LD_LIBRARY_PATH=/usr/local/lib`，確保原始碼編譯的 CTranslate2 可被正確載入
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
- 遠端伺服器狀態檢查 `/v1/status`：上傳前自動偵測伺服器忙碌狀態和磁碟空間
  - 忙碌時提供 3 選項：等候 / 強制中斷殘留作業 / 改用本機 CPU
  - 磁碟空間不足時自動警告並降級本機
- 遠端伺服器作業追蹤：記錄目前作業類型、模型、語言、已執行時間、來源 IP
- openai-whisper 辨識進度：攔截 verbose 輸出追蹤每段解碼進度，心跳帶上百分比、已辨識位置、總時長

**修正**
- 修正 openai-whisper 後端串流模式暫存檔提前刪除的 bug（finally 條件邏輯錯誤導致 "No such file" 錯誤）

**改進**
- 錄音選單從 2 選項改為 3 選項：混合錄製（輸出+輸入）/ 僅錄播放聲音 / 不錄製，自動偵測聚集裝置和 BlackHole 並標示可用狀態
- install.sh 遠端 server.py 更新訊息改為更明確的描述

### v1.9.9 (2026-03-06)

**新增**
- 遠端 GPU 辨識串流進度：`--input` 上傳大檔到遠端 GPU 辨識時，即時顯示辨識進度百分比與已處理時間/總時長，不再只顯示「等待伺服器回應...」
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
- 所有輸出檔案改用中文前綴命名，一目了然：
  - 逐字稿：`英翻中_逐字稿_`、`中翻英_逐字稿_`、`英文_逐字稿_`、`中文_逐字稿_`
  - 摘要：`英翻中_摘要_`、`中翻英_摘要_`、`英文_摘要_`、`中文_摘要_`
  - 錄音：`錄音_`
- 錄音輸出格式改為 MP3（近無損品質 VBR ~220-260kbps），大幅減少檔案體積
- 支援透過 config.json 設定錄音格式（`recording_format`：mp3/ogg/flac/wav）
- 錄音期間仍以 WAV 暫存（保留 30 秒 header 更新防當機機制），停止時自動轉檔
- 轉檔失敗時保留原始 WAV 檔，不中斷程式運作
- --input 暫存檔改為 `tmp_` 前綴（不再是隱藏檔）

### v1.9.4 (2026-03-05)

**改進**
- 支援多個體同時使用遠端 GPU 伺服器：啟動時先檢查伺服器是否已在運行，是則直接沿用；退出時不再關閉伺服器
- 新增 --restart-server 參數：強制重啟遠端伺服器（更新 server.py 後使用）
- 遠端伺服器啟動/等待就緒/載入模型期間新增 spinner 動畫（避免使用者誤以為當機）
- 互動選單檔案選擇支援逗號分隔多選（如 1,3,5 一次選三個檔案處理）
- 上傳音訊完成後狀態列即時切換為「遠端 GPU 辨識中」（不再停留在上傳進度）
- Ctrl+C 中止時不再顯示 Python traceback，乾淨退出
- install.sh 所有耗時操作新增 spinner 動畫（安裝、編譯、下載、遠端檢查）
- install.sh 遠端 GPU 檢查階段統一使用 [完成]/[安裝]/[失敗] 格式
- 互動選單 UI 改善：Banner 資訊三層顏色區分、辨識模型快取標記對齊、錄製音訊區段排版

**修正**
- 修正 --input 遠端辨識時狀態列計時器每段歸零的問題（現在顯示總經過時間）
- 修正辨識模型選單因中文全形字元導致快取標記未對齊的問題（改用顯示寬度計算）
- install.sh 遠端安裝補齊全部編譯依賴: python3-dev, build-essential, pkg-config, libffi-dev, libsndfile1-dev
- install.sh pip 安裝鎖定 setuptools<81（防止 --force-reinstall 升級後 pkg_resources 消失）
- install.sh pip 安裝錯誤訊息不再被 grep 過濾隱藏

**文件**
- SOP.md 新增「全地端運行」兩種部署模式說明（單機模式 / 本機 + GPU 伺服器模式）
- SOP.md 系統架構圖更新遠端 GPU 標註、新增遠端伺服器架構圖和 AI 模型表

### v1.9.3 (2026-03-05)

**新功能**
- 新增遠端 GPU 講者辨識：`--diarize` + `--input` 搭配遠端 Whisper 時自動使用遠端 GPU 執行 diarization
- 伺服器端新增 `/v1/audio/diarize` API endpoint（resemblyzer + spectralcluster，有 GPU 自動加速）
- `/health` 新增 `diarize` 欄位，客戶端可偵測伺服器是否支援講者辨識
- `install.sh` 遠端部署自動安裝 resemblyzer + spectralcluster
- 遠端 diarization 失敗時自動降級本機 CPU 執行，不中斷流程

---

### v1.9.2 (2026-03-05)

**新功能**
- 新增 Ctrl+P 暫停/繼續即時翻譯（三種模式皆支援：Whisper / Moonshine / 遠端 GPU）
- 暫停時音訊擷取持續運作（波形仍跳動），僅暫停辨識與翻譯輸出
- 狀態列即時顯示暫停狀態（黃色 ⏸ 已暫停），並切換快捷鍵提示

---

### v1.9.1 (2026-03-05)

**修正**
- 即時遠端辨識 timeout 從 30 秒改為 120 秒，避免大模型首次載入時逾時
- 新增模型預熱：啟動伺服器後送靜音 WAV 觸發模型載入到 GPU，等載入完成才開始監聽
- 修正 drain_ordered_results 邏輯錯誤（無法區分「尚未到達」與「上傳失敗」）
- 「音訊窗口」改為「音訊緩衝」（修正用語）
- 辨識位置提示「遠端不支援 Moonshine」改為黃色醒目顯示
- large-v3 模型說明加註「有獨立 GPU 可選用」
- SOP.md 新增互動選單完整流程圖（即時模式 + 離線模式所有分支）

---

### v1.9.0 (2026-03-05)

**新功能**
- 即時模式支援遠端 GPU Whisper 辨識：本機擷取音訊 -> 上傳遠端 GPU -> 取回辨識結果 -> 翻譯顯示
- 新增 `select_asr_location()` 互動選單步驟，有遠端設定時自動出現「辨識位置」選項
- 新增 `select_whisper_model_remote()` 遠端模型選擇（顯示遠端快取標籤）
- 新增 `run_stream_remote()` 核心函式：環形緩衝 + 有序非同步上傳 + 去重
- CLI 模式：有遠端設定時即時模式自動走遠端路徑（`--local-asr` 強制本機）
- `--local-asr` 說明更新，明確適用於即時模式與離線模式

**技術細節**
- 音訊緩衝 5 秒、滑動步進 3 秒（與 whisper-stream 一致）
- sounddevice 擷取 48kHz -> 降頻 16kHz -> 環形緩衝 -> in-memory WAV 上傳
- RMS 靜音偵測（< 0.001 跳過上傳），遞增序號確保字幕順序正確
- 遠端不支援 Moonshine（選單明確告知此限制）
- 個別上傳失敗不中斷，跳過該 chunk 繼續運作

---

### v1.8.6 (2026-03-05)

**改進**
- 離線處理互動選單的「辨識位置」步驟改為永遠顯示，未設定遠端 GPU 時選擇會提示執行 install.sh 設定
- install.sh --upgrade 版本比較：若本地版本比 GitHub 還新則不覆蓋（避免開發機被降版）
- install.sh 遠端 GPU 設定改為自動檢查：已設定時檢查 SSH/Python3/venv/server.py/套件，有問題自動提示修復
- install.sh 遠端 GPU 設定使用 SSH ControlMaster 多工，全程只需輸入一次密碼
- install.sh 遠端 GPU 區段標題改為「非必要，若未裝則用本機進行語音辨識」
- install.sh 安裝完成後新增提示：資料夾搬移後需重新執行 install.sh

---

### v1.8.5 (2026-03-05)

**改進**
- 講者辨識精準度提升：啟用 SpectralClusterer refinement（高斯模糊 + 行最大值門檻），抑制噪音相似度
- 講者辨識：長段落（>= 1.6s）改用滑動窗口 embedding + 中位數，聲紋特徵更穩定
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
- 離線處理音訊檔（--input）支援遠端 GPU 語音辨識：將音訊上傳到 Linux + NVIDIA GPU 伺服器辨識，速度快 5-10 倍（支援系統：DGX OS / Ubuntu）
- 新增 remote_whisper_server.py，部署到遠端伺服器的 FastAPI Whisper ASR 服務
- install.sh 新增選填步驟 setup_remote_whisper()，自動 SSH 部署遠端伺服器環境
- 互動選單新增「辨識位置」步驟（遠端 GPU / 本機 CPU），僅在有遠端設定時出現
- 新增 --local-asr 參數，強制使用本機 CPU 辨識（忽略遠端 GPU 設定）
- 新增 --restart-server 參數，強制重啟遠端 GPU 伺服器（更新 server.py 後使用）
- 遠端辨識失敗時自動降級為本機 CPU，不中斷處理流程

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
- Whisper: base.en / small.en / large-v3-turbo / medium.en / large-v3
- Ollama: phi4:14b（推薦）/ qwen2.5:14b / qwen2.5:7b
