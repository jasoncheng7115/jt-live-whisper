# jt-live-whisper v1.7.8

**100% 全地端 AI 語音工具集**：即時轉錄、即時翻譯、錄音檔批次處理、講者辨識、會議摘要，所有 AI 模型皆在自有設備上運行，資料不經過任何雲端服務。

核心功能涵蓋即時語音轉錄、英中/中英即時翻譯字幕、離線音訊檔批次處理、AI 講者辨識（Speaker Diarization）、以及 LLM 會議摘要產出。採用 macOS 系統音訊裝置層級擷取，**理論上任何軟體的聲音輸出都能即時處理**：視訊會議（Zoom、Teams、Meet）、YouTube、Podcast、串流影片等，不限定特定應用程式。所有 AI 推論皆由地端模型完成，全程不經過第三方雲端 API。

Author: Jason Cheng (Jason Tools)

![即時英翻中字幕運作中](images/realtime-en2zh-1.png)

## 我為什麼要打造 jt-live-whisper？

某次參加原廠的線上技術課程，全程英文授課，聽得七零八落。為了補足自己英文聽力的不足，乾脆動手打造了這套工具來即時翻譯，結果功能越做越多，就變成現在這個樣子了 XD

- **完全地端運行**：語音辨識、翻譯、講者辨識、摘要全部使用自有設備上的 AI 模型，無需雲端 API Key、不上傳任何資料至第三方
- **隱私安全**：會議內容、語音資料全程留在自有設備，適合企業內部會議、機密討論
- **零成本**：不需要付費的雲端 API（OpenAI、Google 等），所有 AI 模型皆為自由開源
- **不限應用程式**：採用系統音訊裝置層級擷取，理論上任何軟體的聲音輸出都能處理（Zoom、Teams、Meet、YouTube、Podcast 等）
- **功能完整**：從即時轉錄翻譯、離線音訊處理、講者辨識到 AI 摘要，一套搞定
- **一鍵安裝**：安裝腳本自動下載並編譯所有 AI 模型和相依套件

## 使用的 AI 模型

| 用途 | AI 模型 | 說明 |
|------|---------|------|
| 語音辨識 (ASR) | **Whisper** (OpenAI) | 開源語音辨識模型，支援中英文，本地端 whisper.cpp 執行 |
| 語音辨識 (ASR) | **Moonshine** (Useful Sensors) | 超低延遲串流辨識模型，英文專用 |
| 語音辨識 (離線) | **faster-whisper** (CTranslate2) | 離線音訊檔處理，支援 VAD 靜音過濾 |
| 翻譯 / 摘要 | **Qwen 2.5** / **Phi-4** 等 LLM | 透過地端 Ollama 或其他 LLM 伺服器運行（本機或區域網路） |
| 翻譯 (離線備援) | **Argos Translate** | 完全離線的輕量翻譯模型，不需 LLM 伺服器 |
| 講者辨識 | **resemblyzer** + **spectralcluster** | 聲紋特徵提取 + Google 頻譜分群演算法 |

所有模型皆在自有設備上推論（本機或區域網路內的 GPU 伺服器），**不需要任何第三方雲端 API**。

## 五大核心功能

### 1. 即時語音轉錄翻譯（主要功能）
擷取 macOS 系統音訊，本地端 AI 即時辨識語音並翻譯成繁體中文字幕顯示於終端機。開會、看影片、聽 Podcast 即時翻譯。

![即時英翻中字幕畫面](images/realtime-en2zh-2.png)

### 2. 離線音訊檔批次處理
支援 mp3 / wav / m4a / flac 等格式，使用 faster-whisper AI 模型進行離線轉錄翻譯，適合會後補做逐字稿。

![離線處理選單：模式與模型選擇](images/offline-menu-1.png)

![離線處理選單：LLM 伺服器與講者辨識](images/offline-menu-2.png)

### 3. AI 講者辨識（Speaker Diarization）
自動辨識音訊中的不同講者，以不同顏色標示，支援自動偵測或手動指定講者人數。

![講者辨識：不同講者以不同顏色顯示](images/offline-diarize-result.png)

### 4. AI 會議摘要
即時按 Ctrl+S 或批次對記錄檔生成摘要，透過本地端 LLM 產出重點整理 + 校正逐字稿。搭配講者辨識，摘要中不同講者以不同顏色區分。

![AI 會議摘要產出畫面](images/summary-output.png)

![匯入錄音檔產生的摘要與校正逐字稿](images/offline-summary-diarize.png)

### 5. 多模式語音轉錄
4 種功能模式：英翻中 / 中翻英 / 純英文轉錄 / 純中文轉錄，滿足各種使用場景。

## 其他特色

- **多種本地端 AI 語音辨識引擎**：即時辨識：Whisper（高準確度）/ Moonshine（超低延遲 ~300ms）；離線音訊檔轉錄：faster-whisper（支援 VAD 靜音過濾）
- **多種本地端翻譯引擎**：LLM 大型語言模型（Ollama / OpenAI 相容伺服器）或 Argos 離線翻譯
- **會議主題感知翻譯**：可指定會議主題（如「ZFS 儲存管理」），讓 LLM 根據領域上下文精準翻譯專業術語
- **自動偵測 LLM 伺服器**：支援 Ollama、LM Studio、Jan.ai、vLLM、LocalAI、llama.cpp、LiteLLM 等本地端 LLM 伺服器
- **互動式選單 + CLI 模式**：新手友善的選單介面，進階用戶可用命令列參數直接啟動

## 系統需求

- macOS（Apple Silicon）
- Python 3.12+
- Homebrew
- [BlackHole 2ch](https://existential.audio/blackhole/)（虛擬音訊驅動，安裝腳本會自動安裝）
- 本地端 LLM 伺服器（推薦 [Ollama](https://ollama.com/)，翻譯/摘要用。推薦搭配 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/) 運行 Ollama，CP 值高。**沒有 LLM 伺服器也能用**：程式可切換為純本機 Argos 離線翻譯引擎，完全不需額外伺服器，但摘要功能需要 LLM）

## 快速開始

### 1. 一鍵安裝

打開終端機，貼上這一行即可自動下載並安裝所有元件：

```bash
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh | bash
```

或使用 git clone：

```bash
git clone https://github.com/jasoncheng7115/jt-live-whisper.git
cd jt-live-whisper && ./install.sh
```

安裝腳本會自動下載並設定所有地端 AI 模型和相依套件（Whisper 語音辨識模型、Moonshine 串流辨識模型、Argos 離線翻譯模型、whisper.cpp 編譯等）。

> 首次安裝預估時間：約 10~20 分鐘（視網路速度而定，主要是下載 AI 模型和編譯 whisper.cpp）

### 3. 設定 macOS 音訊

安裝 BlackHole 後需要**重新啟動電腦**，然後在「音訊 MIDI 設定」中建立虛擬裝置。

#### 3a. 建立「多重輸出裝置」（必要）

讓系統音訊同時送到你的耳機和 BlackHole，程式才能擷取對方的聲音：

1. 開啟「音訊 MIDI 設定」（Spotlight 搜尋「音訊 MIDI 設定」）
2. 點左下角 + → 建立「多重輸出裝置」
3. 勾選你的喇叭/耳機 + BlackHole 2ch
4. **主裝置選 BlackHole 2ch**（虛擬裝置時脈穩定，不會因藍牙斷線而失效）
5. 到「系統設定 → 聲音 → 輸出」，選擇此多重輸出裝置

```
對方說話 → Zoom/Teams 輸出 → 多重輸出裝置 → 耳機（你聽到）
                                            → BlackHole（程式擷取）→ AI 辨識 → 字幕
```

> Zoom / Teams 的喇叭輸出要設成「多重輸出裝置」，不能直接選 AirPods，否則 BlackHole 收不到聲音。麥克風維持原本的設定（如 AirPods），不需要改。

#### 3b. 建立「聚集裝置」（選配，錄音時錄雙方聲音用）

如果你想用 `--record` 錄音功能同時錄下**對方和自己的聲音**，需要額外建立聚集裝置：

1. 在「音訊 MIDI 設定」點左下角 + → 建立「聚集裝置」
2. 勾選 BlackHole 2ch（對方聲音）+ 你的麥克風（你的聲音）
3. **時脈來源選 BlackHole 2ch**，其他實體裝置勾選「偏移修正」

![聚集裝置設定](images/aggregate-device.png)

程式會自動偵測聚集裝置作為錄音裝置，不需要手動選擇。不需要錄音的話可以跳過這步。

> **注意：** 即時辨識僅處理系統音訊（對方/應用程式的聲音），無法即時辨識你自己的聲音。如需轉錄自己的聲音，請啟用錄製功能，事後再用 `--input` 離線產出逐字稿與摘要。

### 4. 安裝地端 LLM（翻譯/摘要用）

LLM 伺服器可安裝在本機或區域網路內的其他主機。推薦使用 [Ollama](https://ollama.com/)：

```bash
# 安裝 Ollama（本機或遠端主機皆可）
brew install ollama

# 下載推薦的翻譯模型
ollama pull qwen2.5:14b
```

> **推薦硬體：** 如果有 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/)（128GB 記憶體），將 Ollama 安裝在 DGX Spark 上是非常實惠的選擇：可運行更大的模型、翻譯品質更好、推論速度更快，macOS 端透過 `--ollama-host` 指向即可。

> **不裝 LLM 也能翻譯：** 程式可切換為純本機 Argos 離線翻譯引擎，翻譯品質較 LLM 低但完全不需要額外伺服器。注意：摘要功能仍需 LLM 伺服器。

### 5. 啟動

```bash
./start.sh
```

程式會進入互動式選單，依序選擇功能模式、翻譯引擎、AI 辨識模型等設定。音訊裝置全自動偵測，不需手動選擇。

![互動式選單](images/interactive-menu.png)

## 使用方式

### 即時模式（預設，邊聽邊轉）

```bash
# 互動式選單
./start.sh

# CLI 模式（跳過選單）
./start.sh --mode en2zh --engine ollama --ollama-model qwen2.5:14b
```

### 離線處理音訊檔

```bash
# 英翻中 + 自動摘要
./start.sh --input meeting.mp3 --summarize

# AI 講者辨識
./start.sh --input meeting.mp3 --diarize

# 指定講者人數 + 摘要
./start.sh --input meeting.mp3 --diarize --num-speakers 3 --summarize
```

### 批次摘要

```bash
./start.sh --summarize logs/en2zh_translation_20260101_120000.txt
```

### 快捷鍵（即時模式）

| 按鍵 | 功能 |
|------|------|
| `Ctrl+C` | 停止轉錄 |
| `Ctrl+S` | 停止並生成 AI 會議摘要 |

## 命令列參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--mode MODE` | 功能模式 (en2zh / zh2en / en / zh) | en2zh |
| `--asr ASR` | AI 語音辨識引擎 (whisper / moonshine) | whisper |
| `-m`, `--model MODEL` | Whisper 模型 | large-v3-turbo |
| `--engine ENGINE` | 翻譯引擎 (ollama / argos) | ollama |
| `--ollama-model MODEL` | LLM 翻譯模型 | qwen2.5:14b |
| `--ollama-host HOST` | LLM 伺服器位址 | 192.168.1.40:11434 |
| `--topic TOPIC` | 會議主題（提升翻譯品質） | |
| `--summary-model MODEL` | 摘要用 LLM 模型 | qwen2.5:14b |
| `--input FILE` | 離線處理音訊檔 | |
| `--diarize` | 啟用 AI 講者辨識 | |
| `--num-speakers N` | 指定講者人數 | 自動偵測 |
| `--summarize [FILE ...]` | 生成 AI 摘要 | |

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

## 目錄結構

```
jt-live-whisper/
  translate_meeting.py     主程式
  start.sh                 啟動腳本
  install.sh               安裝腳本
  config.json              使用者設定（自動產生）
  logs/                    轉錄記錄檔、AI 摘要檔（自動建立）
  recordings/              暫存音訊轉檔（自動建立）
  whisper.cpp/             Whisper AI 引擎（安裝時自動編譯）
  venv/                    Python 虛擬環境（安裝時自動建立）
```

## 技術架構

```
即時模式：
  macOS 系統音訊 → BlackHole 虛擬音訊裝置
    → 本地端 Whisper / Moonshine AI 語音辨識
      → 本地端 LLM 翻譯（Ollama）/ Argos 離線翻譯
        → 終端機即時字幕 + 轉錄記錄檔

離線模式：
  音訊檔（mp3/wav/m4a/flac）
    → ffmpeg 轉檔
      → 本地端 faster-whisper AI 語音辨識
        → （選配）AI 講者辨識
          → 本地端 LLM 翻譯 + AI 摘要
```

## 升級

```bash
./install.sh --upgrade
```

自動從 GitHub 下載最新版本的程式檔案，升級後建議重新執行 `./install.sh` 確認相依套件完整。

---

## >>> [完整使用手冊（SOP.md）](SOP.md) <<<

包含完整安裝教學、macOS 音訊設定圖解、所有功能模式詳細說明、互動式選單操作、講者辨識設定、摘要功能用法、進階 CLI 參數、FAQ 等。

---

## License

本專案採用 [Apache License 2.0](LICENSE) 授權。

Copyright 2026 Jason Cheng (Jason Tools)
