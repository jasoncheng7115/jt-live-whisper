# jt-live-whisper v2.14.5

**100% 全地端 AI 語音工具集**：即時轉錄、即時翻譯、錄音檔批次處理、講者辨識、會議摘要，所有 AI 模型皆在自有設備上執行，資料不經過任何雲端服務。

| **目錄** | [核心功能](#核心功能) · [其他特色](#其他特色) · [系統需求](#系統需求) · [快速開始](#快速開始) · [使用方式](#使用方式) · [互動式選單](#互動式選單功能一覽) · [命令列參數](#命令列參數) · [技術架構](#技術架構) · [硬體建議](#硬體建議) · [升級](#升級) |
|---|---|

核心功能涵蓋即時語音轉錄、中日英即時翻譯字幕、離線音訊檔批次處理、講者辨識（Speaker Diarization）、以及 LLM 會議摘要產出。採用系統音訊裝置層級擷取（macOS 使用 BlackHole，Windows 使用 WASAPI Loopback），**理論上任何軟體的聲音輸出都能即時處理**：視訊會議（Zoom、Teams、Meet）、YouTube、Podcast、串流影片等，不限定特定應用程式。所有 AI 推論皆由地端模型完成，全程不經過第三方雲端 API。

Author: Jason Cheng (Jason Tools)

![即時英翻中字幕運作中](images/realtime-en2zh-1.png)

![WebUI 瀏覽器介面 - 英中雙向對話模式](images/webui-chat-bidi.png)

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 我為什麼要打造 jt-live-whisper？

某次參加原廠的線上技術課程，全程英文授課，聽得七零八落。為了補足自己英文聽力的不足，乾脆動手打造了這套工具來即時翻譯，結果功能越做越多，就變成現在這個樣子了 XD

- **完全地端執行**：語音辨識、翻譯、講者辨識、摘要全部使用自有設備上的 AI 模型，無需雲端 API Key、不上傳任何資料至第三方
- **隱私安全**：會議內容、語音資料全程留在自有設備，適合企業內部會議、機密討論
- **零月租成本**：不需要付費的雲端 API（ChatGPT、Claude、Gemini 等），所有採用的 AI 模型皆為自由開源
- **不限應用程式**：採用系統音訊裝置層級擷取，理論上任何軟體的聲音輸出都能處理（Zoom、Teams、Meet、YouTube、Podcast 等）
- **功能完整**：從即時轉錄翻譯、離線音訊處理、講者辨識到 AI 摘要，一套搞定
- **一鍵安裝**：安裝腳本自動下載並編譯所有 AI 模型和相依套件

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 使用的 AI 模型

| 用途 | AI 模型 | 說明 |
|------|---------|------|
| 語音辨識 (ASR) | **whisper.cpp** | macOS 即時辨識引擎，支援中日英文，可在本機或 GPU 伺服器執行 |
| 語音辨識 (ASR) | **faster-whisper** (CTranslate2) | Windows 即時辨識 + 全平台離線處理，支援 VAD 靜音過濾 |
| 語音辨識 (ASR) | **mlx-whisper** | Apple Silicon GPU 加速，雙向模式（en_zh / ja_zh）即時辨識專用 |
| 語音辨識 (ASR) | **Moonshine** (Useful Sensors) | 超低延遲串流辨識模型，英文專用（僅限 Apple Silicon） |
| 翻譯 / 摘要 | 搭配自架 LLM 伺服器使用，推薦 **Qwen** / **Phi-4** / **GPT-OSS** 等模型 | 透過地端 Ollama 或其他 LLM 伺服器執行（本機或區域網路），翻譯建議 14B 以上、摘要建議 120B 以上 |
| 翻譯 (離線) | **NLLB 600M** (Meta) | 離線翻譯模型，支援中日英互譯（`en2zh`/`zh2en`/`ja2zh`/`zh2ja`） |
| 翻譯 (離線備援) | **Argos Translate** | 完全離線的輕量翻譯模型，僅支援英翻中 |
| 講者辨識 | **resemblyzer** + **spectralcluster** | 聲紋特徵提取 + Google 頻譜分群演算法，可在本機或 GPU 伺服器執行 |

所有模型皆在自有設備上推論（本機或區域網路內的 GPU 伺服器），**不需要任何第三方雲端 API**。

> **為什麼講者辨識不用更精準更快速的 pyannote.audio？** pyannote 的預訓練模型授權限制了可使用的用途與場景，且需要在 HuggingFace 註冊帳號、申請存取權限並設定 Token 才能下載模型。這不符合本工具「零帳號、零註冊、完全地端」的設計理念。resemblyzer + spectralcluster 完全開源、安裝即用、無需任何帳號或 Token。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 兩種部署方式

- **單機模式**： 一台 Mac 或 Windows PC 即可完成所有處理。語音辨識（Whisper/Moonshine）、翻譯（LLM/NLLB/Argos）全部在本機執行，不需要額外硬體。適合個人使用、外出攜帶。

- **本機 + GPU 伺服器模式**： 本機負責音訊擷取與介面操作，語音辨識和講者辨識交由區域網路內的 GPU 伺服器（如 DGX Spark，或安裝有 NVIDIA GPU 的 Ubuntu/Linux 主機，搭消費級 RTX 4090/5090 之類亦可，需已安裝 CUDA）處理。離線辨識速度快 5-10 倍，仍然是全地端架構，資料僅在區域網路內傳輸。適合需要處理大量音訊或追求即時辨識品質的場景。

兩種模式可隨時切換，伺服器離線時自動降級為本機處理，不中斷使用。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 核心功能

### 1. 即時語音轉錄翻譯（主要功能）
擷取系統音訊（macOS / Windows），本地端 AI 即時辨識語音並翻譯成繁體中文字幕顯示於終端機。開會、看影片、聽 Podcast 即時翻譯。

![即時英翻中字幕畫面（macOS）](images/realtime-en2zh-2.png)

![即時英翻中：翻譯速度標籤與音訊波形（macOS）](images/realtime-en2zh-3.png)

![即時英翻中字幕畫面（Windows）](images/windows-en2zh.png)

### 2. 離線音訊檔批次處理
支援 mp3 / wav / m4a / flac 等格式，使用 faster-whisper AI 模型進行離線轉錄翻譯，適合會後補做逐字稿。

![離線處理選單：模式與模型選擇](images/offline-menu-1.png)

![離線處理選單：LLM 伺服器與講者辨識](images/offline-menu-2.png)

![離線處理選單：設定總覽與等效 CLI 指令](images/offline-menu-3.png)

### 3. 講者辨識（Speaker Diarization）
自動辨識音訊中的不同講者，以不同顏色標示，支援自動偵測或手動指定講者人數。

![講者辨識：不同講者以不同顏色顯示](images/offline-diarize-result.png)

![講者辨識：終端機逐字稿輸出](images/offline-diarize-result-2.png)

### 4. AI 會議摘要與時間軸逐字稿
批次對記錄檔生成摘要，透過本地端 LLM 產出重點整理 + 校正逐字稿。搭配講者辨識，摘要中不同講者以不同顏色區分。

![AI 會議摘要產出畫面](images/summary-output.png)

![匯入錄音檔產生的摘要與校正逐字稿](images/offline-summary-diarize.png)

時間逐字稿 HTML 內嵌音訊播放器與波形圖，可直接點選波形任意位置跳至該時間點；播放時對應的逐字稿段落會即時以高亮區塊標示，方便對照聆聽。

![時間逐字稿 HTML](images/offline-transcript.png)

### 5. 多模式語音轉錄
10 種功能模式：英翻中 / 中翻英 / 日翻中 / 中翻日 / 英中雙向 / 日中雙向 / 純英文轉錄 / 純中文轉錄 / 純日文轉錄 / 純錄音，滿足各種使用場景。

![即時日翻中字幕畫面（Windows）](images/realtime-ja2zh.png)

### 6. 雙向字幕模式
英中雙向（`en_zh`）和日中雙向（`ja_zh`），同時擷取系統音訊與麥克風，對方外語翻中文、自己中文翻外語，適用於雙語視訊會議。

![英中雙向即時字幕（終端機）](images/bidi-en-zh-cli.png)

![英中雙向離線逐字稿（HTML 聊天風格）](images/bidi-en-zh-html.png)

![日中雙向即時字幕](images/bidi-ja-zh.png)

![日中雙向離線逐字稿（HTML 聊天風格）](images/bidi-ja-zh-html.png)

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 其他特色

- **同時轉錄麥克風**：所有即時模式加上 `--mic` 即可同時轉錄自己的麥克風語音，雙向模式自動啟用
- **多種本地端 AI 語音辨識引擎**：即時辨識：Whisper（高準確度）/ Moonshine（超低延遲 ~300ms）；離線音訊檔轉錄：faster-whisper（支援 VAD 靜音過濾）
- **多種本地端翻譯引擎**：LLM 大型語言模型（Ollama / OpenAI 相容伺服器）、NLLB 離線翻譯（中日英互譯）或 Argos 離線翻譯
- **會議主題感知翻譯**：可指定會議主題（如「ZFS 儲存管理」），讓 LLM 根據領域上下文精準翻譯專業術語
- **自動偵測 LLM 伺服器**：支援 Ollama、LM Studio、Jan.ai、vLLM、LocalAI、llama.cpp、LiteLLM 等本地端 LLM 伺服器
- **互動式選單 + CLI 模式**：新手友善的選單介面，進階用戶可用命令列參數直接啟動
- **WebUI 瀏覽器介面**：`./start.sh --webui` 在瀏覽器中操作所有功能，支援即時字幕、離線處理、講者辨識、摘要，手機/平板也可使用

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 系統需求

**macOS：**
- macOS（Apple Silicon / Intel）
- Python 3.12+
- [Homebrew](https://brew.sh/)（需事先安裝）
- [BlackHole 2ch](https://existential.audio/blackhole/)（虛擬音訊驅動，安裝腳本會自動安裝）

**Windows：**
- Windows 10 以上
- Python 3.12+（從 [python.org](https://www.python.org/downloads/) 安裝，勾選「Add to PATH」）
- PowerShell 5.1+（Windows 10 內建）

**共通：**
- 本地端 LLM 伺服器（推薦 [Ollama](https://ollama.com/)，翻譯/摘要用。推薦搭配 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/) 執行 Ollama，CP 值高。**沒有 LLM 伺服器也能用**：程式可切換為 NLLB/Argos 離線翻譯引擎，完全不需額外伺服器，但摘要功能需要 LLM）

### 磁碟空間需求

安裝腳本會在安裝前自動檢查可用空間是否足夠。

#### 本機

| 元件 | 大小 | 說明 |
|------|------|------|
| Python venv + 套件 | ~1.1 GB | ctranslate2, faster-whisper, resemblyzer, spectralcluster 等 |
| whisper.cpp | ~60 MB | macOS: 原始碼編譯；Windows: 預編譯版本 |
| Whisper GGML 模型 | 1.5~6.4 GB | 預設 large-v3-turbo (1.5GB)；全部 5 個模型共 6.4 GB |
| Moonshine 模型 | ~245 MB | 英文即時辨識（選用） |
| NLLB 600M 翻譯模型 | ~600 MB | 離線翻譯（中日英互譯） |
| Argos 翻譯模型 | ~83 MB | 離線備援翻譯（僅英翻中） |
| Homebrew 套件 | ~140 MB | cmake + sdl2 + ffmpeg（僅 macOS） |
| HuggingFace 快取 | ~5.3 GB | `~/.cache/huggingface/`，`--input` 離線處理用，首次使用時下載 |
| **最小安裝** | **~3 GB** | venv + 1 個 Whisper 模型 + 基本套件 |
| **推薦安裝** | **~8 GB** | 加上 HuggingFace 快取（離線處理音訊檔用） |
| **完整安裝** | **~14 GB** | 全部 Whisper 模型 + HuggingFace 快取 + Moonshine |

#### GPU 伺服器（選配）

| 元件 | 大小 | 說明 |
|------|------|------|
| PyTorch GPU (CUDA) | ~2.5 GB | 依 CUDA 版本而異 |
| Python venv + 套件 | ~1 GB | faster-whisper, fastapi, resemblyzer 等 |
| Whisper 模型 | ~6 GB | 5 個模型（CTranslate2 格式），首次安裝時下載 |
| openai-whisper | ~500 MB | CTranslate2 CUDA 不可用時才安裝 |
| **最小安裝** | **~5 GB** | PyTorch + 1 個模型 |
| **完整安裝** | **~12 GB** | PyTorch + 全部 5 個模型 + 講者辨識套件 |

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 快速開始

### 1. 一鍵安裝

**macOS：**

打開終端機，貼上以下指令即可自動下載並安裝所有元件：

```bash
mkdir -p ~/Apps/jt-live-whisper && cd ~/Apps/jt-live-whisper
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh -o install.sh
bash install.sh
```

**Windows：**

開啟 PowerShell（以管理員身份），建立資料夾並切換過去（不需要 Git）：

```powershell
mkdir C:\jt-live-whisper -Force | Out-Null; cd C:\jt-live-whisper
```

下載安裝程式：

```powershell
irm https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.ps1 -OutFile install.ps1
```

執行安裝：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

安裝腳本會自動下載並設定所有地端 AI 模型和相依套件（Whisper 語音辨識模型、Moonshine 串流辨識模型、NLLB 離線翻譯模型、Argos 離線翻譯模型等）。安裝最後會詢問是否設定 GPU 語音辨識伺服器（選填），若有安裝 NVIDIA GPU 的 Ubuntu/Linux 主機（消費級 RTX 4090/5090 亦可，需已安裝 CUDA），可透過 SSH 自動在伺服器安裝 PyTorch、faster-whisper 等套件，大幅加速語音辨識。

> 首次安裝預估時間：約 10~20 分鐘（視網路速度而定，主要是下載 AI 模型。macOS 需額外編譯 whisper.cpp）

### 2. 設定音訊裝置

#### macOS

安裝 BlackHole 後需要**重新啟動電腦**，然後在「音訊 MIDI 設定」中建立虛擬裝置。

**3a. 建立「多重輸出裝置」（必要）**

讓系統音訊同時送到你的耳機和 BlackHole，程式才能擷取對方的聲音：

1. 開啟「音訊 MIDI 設定」（Spotlight 搜尋「音訊 MIDI 設定」）
2. 點左下角 + → 建立「多重輸出裝置」
3. 勾選你的喇叭/耳機 + BlackHole 2ch
4. **主裝置選 BlackHole 2ch**（虛擬裝置時脈穩定，不會因藍牙斷線而失效）
5. 到「系統設定 → 聲音 → 輸出」，選擇此多重輸出裝置

![macOS 音訊 MIDI 設定：多重輸出裝置](images/audio-midi-setup.png)

```
對方說話 → Zoom/Teams 輸出 → 多重輸出裝置 → 耳機（你聽到）
                                            → BlackHole（程式擷取）→ AI 辨識 → 字幕
```

> Zoom / Teams 的喇叭輸出要設成「多重輸出裝置」，不能直接選 AirPods，否則 BlackHole 收不到聲音。麥克風維持原本的設定（如 AirPods），不需要改。

**3b. 建立「聚集裝置」（選配，錄音時錄雙方聲音用）**

如果你想用 `--record` 錄音功能同時錄下**對方和自己的聲音**，需要額外建立聚集裝置：

1. 在「音訊 MIDI 設定」點左下角 + → 建立「聚集裝置」
2. 勾選 BlackHole 2ch（對方聲音）+ 你的麥克風（你的聲音）
3. **時脈來源選 BlackHole 2ch**，其他實體裝置勾選「偏移修正」

![聚集裝置設定](images/aggregate-device.png)

程式會自動偵測聚集裝置作為錄音裝置，不需要手動選擇。不需要錄音的話可以跳過這步。

> **提示：** 即時辨識預設處理系統音訊（對方/應用程式的聲音）。加上 `--mic` 參數即可同時轉錄你自己的麥克風語音，或使用雙向模式（`en_zh` / `ja_zh`）自動啟用雙路辨識。

#### Windows

Windows 不需要安裝額外的虛擬音訊驅動。程式透過 WASAPI Loopback 直接擷取系統播放的音訊，大多數情況下不需要手動設定。

如果自動偵測失敗，可嘗試啟用「立體聲混音」（Stereo Mix）：右鍵通知區域音量圖示 → 音效設定 → 錄製 → 右鍵「顯示已停用的裝置」→ 啟用「立體聲混音」。

驗證：執行 `.\start.ps1 --list-devices` 確認列表中有 loopback 裝置。

### 3. 安裝地端 LLM（翻譯/摘要用）

LLM 伺服器可安裝在本機或區域網路內的其他主機。推薦使用 [Ollama](https://ollama.com/)：

```bash
# macOS：透過 Homebrew 安裝
brew install ollama

# Windows：從 https://ollama.com/ 下載安裝程式

# 下載推薦的翻譯模型（兩平台皆同）
ollama pull qwen2.5:14b
```

> **推薦硬體：** 如果有 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/)（128GB 記憶體），將 Ollama 安裝在 DGX Spark 上是非常實惠的選擇：可執行更大的模型、翻譯品質更好、推論速度更快，透過 `--llm-host` 指向即可。

> **不裝 LLM 也能翻譯：** 程式可切換為 NLLB（中日英互譯，品質 7-8/10）或 Argos（僅英翻中）離線翻譯引擎，完全不需要額外伺服器。注意：摘要功能仍需 LLM 伺服器。

### 4. 啟動

先切換到安裝目錄：

```bash
# macOS
cd ~/Apps/jt-live-whisper

# Windows (PowerShell)
cd C:\jt-live-whisper
```

啟動程式：

```bash
# macOS
./start.sh

# Windows (PowerShell)
.\start.ps1
```

程式會進入互動式選單，依序選擇功能模式、翻譯引擎、AI 辨識模型等設定。音訊裝置全自動偵測，不需手動選擇。

![互動式選單](images/interactive-menu.png)

![互動式選單：GPU 伺服器 與錄音設定](images/interactive-menu-2.png)

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 使用方式

> 以下範例以 macOS 指令為主。Windows 使用者請將 `./start.sh` 替換為 `.\start.ps1`，安裝目錄為 `C:\jt-live-whisper`。其餘參數完全相同。

### WebUI 瀏覽器介面（推薦）

```bash
./start.sh --webui            # macOS
.\start.ps1 --webui           # Windows
```

自動開啟瀏覽器（預設 `http://localhost:19781`），在網頁中完成所有設定後按「開始」即可。

- 所有即時/離線功能皆可在瀏覽器操作，不需記指令
- 離線處理：講者辨識、摘要、摘要模型選擇
- 辨識模型依裝置自動推薦、翻譯引擎依設定自動選擇
- 各階段即時進度顯示（辨識/講者辨識/LLM 校正/摘要 含 tokens 數）
- 聊天模式與字幕模式切換、淺色/深色主題
- 手機/平板也可使用

**設定頁面**

![WebUI 設定頁 - 輸入來源與語音辨識](images/webui-settings-1.png)

![WebUI 設定頁 - 翻譯引擎與音訊裝置](images/webui-settings-2.png)

**對話模式**（聊天風格，對方靠左、自己靠右）

![WebUI 對話模式](images/webui-chat.png)

**字幕模式**（電影風格，黑底大字）

![WebUI 字幕模式](images/webui-subtitle.png)

![WebUI 字幕模式 - 雙向](images/webui-subtitle-bidi.png)

### 即時模式（預設，邊聽邊轉）

```bash
# 互動式選單
./start.sh                    # macOS
.\start.ps1                   # Windows

# CLI 模式（跳過選單）
./start.sh --mode en2zh --engine llm --llm-model qwen2.5:14b

# 英中雙向字幕（對方英文翻中文 + 自己中文翻英文）
./start.sh --mode en_zh

# 日中雙向字幕（對方日文翻中文 + 自己中文翻日文）
./start.sh --mode ja_zh

# 即時翻譯 + 同時轉錄麥克風
./start.sh --mode en2zh --mic
```

### 離線處理音訊檔

```bash
# 英翻中 + 自動摘要
./start.sh --input meeting.mp3 --summarize

# 講者辨識
./start.sh --input meeting.mp3 --diarize

# 指定講者人數 + 摘要
./start.sh --input meeting.mp3 --diarize --num-speakers 3 --summarize
```

### 批次摘要

```bash
./start.sh --summarize logs/英翻中_逐字稿_20260101_120000.txt
```

### 快捷鍵（即時模式）

| 按鍵 | 功能 |
|------|------|
| `Ctrl+C` | 停止轉錄 |
| `Ctrl+P` | 暫停 / 繼續 |

### 互動式選單功能一覽

不帶任何參數啟動程式（`./start.sh` 或 `.\start.ps1`）即進入互動式選單，依序引導完成所有設定。

#### 即時模式選單

| 步驟 | 選單項目 | 選項 | 說明 |
|------|----------|------|------|
| 1 | 輸入來源 | 即時語音 / 讀入檔案 | 選擇即時擷取系統音訊或匯入錄音檔離線處理 |
| 2 | 功能模式 | 英翻中 / 中翻英 / 日翻中 / 中翻日 / 英中雙向 / 日中雙向 / 英文轉錄 / 中文轉錄 / 日文轉錄 / 純錄音 | 10 種模式，分群顯示（單向翻譯、雙向翻譯、轉錄、其他） |
| 3 | 麥克風轉錄 | 是 / 否 | 轉錄模式（en/zh/ja）詢問是否同時轉錄麥克風 |
| 4 | 辨識位置 | GPU 伺服器 / 本機 | 有設定 GPU 伺服器時才顯示 |
| 5 | ASR 引擎 | Whisper / Moonshine | 英文模式可選 Moonshine（超低延遲），其他語言固定 Whisper |
| 6 | 辨識模型 | large-v3-turbo / large-v3 / medium 等 | 依裝置效能自動推薦適合的模型大小 |
| 7 | 翻譯引擎 | LLM 伺服器 / NLLB 離線 / Argos 離線 | 翻譯模式才顯示，自動偵測可用的 LLM 伺服器 |
| 8 | 翻譯模型 | 伺服器上的模型清單 | 動態查詢 LLM 伺服器上已安裝的模型 |
| 9 | 會議主題 | 自由輸入 | 選填，提升 LLM 翻譯專業術語的準確度 |
| 10 | 音訊場景 | 會議 / 教育訓練 / 快速字幕 | 調整音訊緩衝長度，影響延遲與辨識品質 |
| 11 | 錄音設定 | 混合錄製 / 僅播放音訊 / 不錄音 | 是否同步錄製音訊為檔案 |
| 12 | 確認啟動 | Y / n | 顯示等效 CLI 指令，確認後開始 |

#### 離線處理選單（讀入檔案）

| 步驟 | 選單項目 | 選項 | 說明 |
|------|----------|------|------|
| 1 | 功能模式 | 英文轉錄+中文翻譯 / 中文轉錄+英文翻譯 / 日文轉錄+中文翻譯 / 中文轉錄+日文翻譯 / 英中雙向 / 日中雙向 / 純轉錄 | 9 種模式（不含純錄音） |
| 2 | 辨識位置 | GPU 伺服器 / 本機 | GPU 伺服器辨識速度快 5-10 倍 |
| 3 | 辨識模型 | large-v3-turbo / large-v3 / medium 等 | 依辨識位置推薦模型，伺服器模式顯示快取標籤 |
| 4 | LLM 伺服器 | host:port | 翻譯模式才詢問，自動偵測伺服器類型 |
| 5 | 翻譯模型 | 伺服器模型 / NLLB 離線 / Argos 離線 | 動態列出伺服器模型 + 本機離線選項 |
| 6 | 講者辨識 | 不辨識 / 自動偵測 / 指定人數 | 自動偵測或手動指定 2~20 位講者 |
| 7 | 摘要與校正 | 摘要+校正逐字稿 / 只摘要 / 只逐字稿 | 需 LLM 伺服器，無 LLM 時僅產出逐字稿 |
| 8 | 摘要模型 | 伺服器模型清單 | 選了摘要才顯示，推薦 120B 以上 |
| 9 | 會議主題 | 自由輸入 | 選填，提升翻譯與摘要品質 |
| 10 | 確認啟動 | Y / n | 顯示等效 CLI 指令與設定總覽 |

> 互動選單的所有設定都可透過命令列參數直接指定，跳過選單直接執行。選單最後會顯示等效的 CLI 指令，方便下次直接使用。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 命令列參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--webui` | 啟動 WebUI 瀏覽器介面 | |
| `--mode MODE` | 功能模式 (`en2zh` / `zh2en` / `ja2zh` / `zh2ja` / `en_zh` / `ja_zh` / `en` / `zh` / `ja` / `record`) | `en2zh` |
| `--asr ASR` | 語音辨識引擎 (`whisper` / `moonshine` / `faster-whisper`) | `whisper` |
| `-m`, `--model MODEL` | Whisper 模型 (`base.en` / `small.en` / `small` / `medium.en` / `medium` / `large-v3-turbo` / `large-v3`) | 依裝置推薦 |
| `--moonshine-model MODEL` | Moonshine 模型 (`medium` / `small` / `tiny`) | `medium` |
| `-s`, `--scene SCENE` | 使用場景 (`meeting` / `training` / `presentation` / `subtitle`) | `training` |
| `-e`, `--engine ENGINE` | 翻譯引擎 (`llm` / `nllb` / `argos`) | `llm` |
| `--llm-model MODEL` | LLM 翻譯模型 | `qwen2.5:14b` |
| `--llm-host HOST` | LLM 伺服器位址（自動偵測 Ollama 或 OpenAI 相容） | |
| `--topic TOPIC` | 會議主題（提升翻譯與摘要品質） | |
| `-d`, `--device ID` | 音訊裝置 ID（可用 `--list-devices` 查詢） | 自動偵測 |
| `--list-devices` | 列出可用音訊裝置後離開 | |
| `--input FILE [...]` | 離線處理音訊檔 | |
| `--diarize` | 啟用講者辨識（需搭配 `--input`） | |
| `--num-speakers N` | 指定講者人數（需搭配 `--diarize`） | 自動偵測 |
| `--summarize [FILE ...]` | 生成 AI 摘要（與 `--input` 合用時不需指定檔案） | |
| `--summary-model MODEL` | 摘要用 LLM 模型 | `gpt-oss:120b` |
| `--mic` | 同時轉錄麥克風語音（即時模式） | |
| `--record` | 即時模式同時錄製音訊 | |
| `--rec-device ID` | 錄音裝置 ID（可與辨識裝置不同） | |
| `--denoise` | 即時模式啟用背景降噪 | |
| `--local-asr` | 強制使用本機辨識（忽略 GPU 伺服器設定） | |
| `--restart-server` | 強制重啟 GPU 伺服器 | |

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 支援的本地端 LLM 伺服器

程式會自動偵測 LLM 伺服器類型，不需手動選擇：

| 伺服器 | 預設 Port | API 類型 |
|--------|-----------|----------|
| Ollama | 11434 | Ollama 原生 |
| LM Studio | 1234 | OpenAI 相容 |
| Jan.ai | 1337 | OpenAI 相容 |
| vLLM | 8000 | OpenAI 相容 |
| LocalAI / llama.cpp | 8080 | OpenAI 相容 |
| LiteLLM | 4000 | OpenAI 相容 |

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 目錄結構

```
jt-live-whisper/
  translate_meeting.py     主程式（即時辨識、離線處理、翻譯、摘要，跨平台）
  webui.py                 WebUI 伺服器（FastAPI + WebSocket，瀏覽器介面後端）
  webui.html               WebUI 前端（單一 HTML，內嵌 CSS/JS）
  start.sh                 啟動腳本（macOS）
  start.ps1                啟動腳本（Windows）
  install.sh               安裝腳本（macOS）
  install.ps1              安裝腳本（Windows）
  remote_whisper_server.py GPU 伺服器端 Whisper 辨識服務（選配）
  config.json              使用者設定（自動產生，含 LLM/GPU/WebUI 密碼等）
  SOP.md                   完整使用手冊
  CHANGELOG.md             版本更新記錄
  logs/                    轉錄記錄檔、AI 摘要檔、HTML 逐字稿（自動建立）
  recordings/              暫存音訊轉檔（自動建立）
  whisper.cpp/             whisper.cpp 即時辨識引擎（macOS 自動編譯，Windows 下載預編譯版本）
  venv/                    Python 虛擬環境（安裝時自動建立）
```

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 技術架構

```
即時模式：
  系統音訊（macOS: BlackHole / Windows: WASAPI Loopback）
    → 本地端 Whisper / Moonshine AI 語音辨識
      → 本地端 LLM 翻譯（Ollama）/ NLLB / Argos 離線翻譯
        → 終端機即時字幕 + 轉錄記錄檔

離線模式：
  音訊檔（mp3/wav/m4a/flac）
    → ffmpeg 轉檔
      → 本地端 faster-whisper AI 語音辨識
        → （選配）講者辨識
          → 本地端 LLM / NLLB / Argos 翻譯 + AI 摘要

WebUI 瀏覽器介面（./start.sh --webui）：
  webui.py（FastAPI + WebSocket）
    → 瀏覽器設定頁（所有功能皆可操作）
    → 啟動 translate_meeting.py 子程序
    → TCP localhost:19780 接收即時事件
    → WebSocket 推送到瀏覽器（即時字幕、進度、狀態）
    → 支援遠端觀看（密碼保護）、手機/平板
```

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 升級

```bash
# macOS
./install.sh --upgrade

# Windows (PowerShell)
.\install.ps1 -Upgrade
```

自動從 GitHub 下載最新版本的程式檔案，升級後建議重新執行安裝腳本確認相依套件完整。

---

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## >>> [完整使用手冊（SOP.md）](SOP.md) <<<

包含完整安裝教學、macOS / Windows 音訊設定說明、所有功能模式詳細說明、互動式選單操作、講者辨識設定、摘要功能用法、進階 CLI 參數、FAQ 等。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## >>> [版本記錄（CHANGELOG.md）](CHANGELOG.md) <<<

---

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 品質與效能說明

- **語音辨識品質**取決於所選用的 ASR 模型大小、音訊品質（背景噪音、麥克風距離、多人交談重疊等）以及語言種類。
- **翻譯品質**取決於所選用的翻譯引擎與模型能力。LLM 翻譯品質最佳但需要 LLM 伺服器（本機或區域網路）；NLLB / Argos 離線翻譯品質較低但無需額外伺服器。
- **講者辨識**準確度受限於音訊品質、講者數量與聲紋相似度，在多人交談或遠場收音情境下結果可能不準確。
- **處理速度**取決於硬體算力（CPU/GPU）與模型大小。使用 GPU 伺服器可大幅加速；純 CPU 環境下處理速度較慢。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 硬體建議

本工具所有 AI 推論皆在地端執行，硬體規格直接影響辨識速度與使用體驗。以下為不同使用場景的建議配置。

### macOS

| 配置 | 記憶體 | 適用場景 | 說明 |
|------|--------|----------|------|
| Apple CPU（M2 以上） | 16 GB | 即時轉錄、離線處理 | 統一記憶體架構，GPU 加速 mlx-whisper，推薦 large-v3-turbo 模型 |
| Apple CPU（M2 以上） | 24 GB+ | 即時轉錄 + 本機 LLM | 可同時執行 Ollama 14B 翻譯模型 + Whisper 辨識 |
| Intel CPU | 8 GB+ | 離線處理為主 | 純 CPU 辨識速度較慢，即時模式建議搭配 GPU 伺服器 |

> Apple Silicon Mac 的統一記憶體架構讓 GPU 可直接存取系統記憶體，不需獨立顯示卡即可流暢執行 AI 推論。16GB 機型足以應付大多數使用場景。

### Windows

| 配置 | 即時辨識 | 離線處理 7 分鐘音檔 | 說明 |
|------|---------|---------------------|------|
| 純 CPU（無獨顯） | 勉強可用 | ~15-25 分鐘 | 即時模式延遲高，建議搭配 GPU 伺服器 |
| GTX 1660 Super（6 GB） | 可用 | ~1-2 分鐘 | 入門級 GPU，VRAM 餘裕較小 |
| **RTX 4060（8 GB）** | **流暢** | **~30-40 秒** | **性價比最高，推薦** |
| RTX 4060 Ti（16 GB） | 流暢 | ~20-30 秒 | VRAM 充裕，未來擴充空間大 |
| RTX 3060（12 GB） | 流暢 | ~40-50 秒 | 上一代，二手性價比高 |

> Windows 搭配 NVIDIA GPU（CUDA）可大幅加速 faster-whisper 語音辨識。large-v3-turbo 模型約需 3 GB VRAM，**最低建議 6 GB VRAM 的顯示卡**。沒有獨顯的 Windows 電腦仍可使用，但離線處理速度會慢很多，即時辨識延遲也較高。

### GPU 伺服器（選配，語音辨識加速用）

區域網路內的 GPU 伺服器可為本機提供遠端語音辨識，適合沒有獨顯或需要更快處理速度的情境。

| GPU | VRAM | 離線處理 7 分鐘音檔 | 說明 |
|-----|------|---------------------|------|
| RTX 4060 以上 | 8 GB+ | ~20-30 秒 | 消費級入門 |
| RTX 4090 | 24 GB | ~10-15 秒 | 消費級旗艦 |
| NVIDIA DGX Spark | 128 GB | ~10 秒 | 同時跑 Ollama LLM + Whisper 辨識，一機搞定 |

### LLM 伺服器（選配，翻譯/摘要用）

| 用途 | 建議模型大小 | 記憶體/VRAM 需求 | 說明 |
|------|-------------|-----------------|------|
| 翻譯 | 14B 以上 | ~12 GB | 如 qwen2.5:14b，品質與速度兼顧 |
| 摘要 | 120B 以上 | ~80 GB | 如 gpt-oss:120b，需要大記憶體主機 |

> LLM 伺服器可安裝在本機或區域網路內的任何主機。推薦使用 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/)（128 GB 統一記憶體），可同時執行翻譯模型與摘要模型。沒有 LLM 伺服器時，程式可切換為 NLLB/Argos 離線翻譯引擎。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 免責聲明

本工具按「現狀」（AS IS）提供，不附帶任何明示或暗示的保證。語音辨識、翻譯、講者辨識及摘要等功能的輸出結果僅供參考，不保證其準確性與完整性。使用者應自行驗證輸出結果，不應將未經人工審核的輸出直接用於法律文件、醫療紀錄、財務報告或其他需要高度準確性的場合。使用者應確保擁有合法錄音權利並遵守當地隱私法規。作者及貢獻者不對因使用本工具而產生的任何損害承擔責任。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## License

本專案採用 [Apache License 2.0](LICENSE) 授權。

Copyright 2026 Jason Cheng (Jason Tools)
