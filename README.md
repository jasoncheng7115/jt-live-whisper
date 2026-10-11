# jt-live-whisper v2.29.0

**100% 全地端 AI 語音工具箱**：即時轉錄、即時翻譯、錄音檔批次處理、講者辨識、會議摘要、台灣華語朗讀，所有 AI 模型皆在自有設備上執行，資料不經過任何雲端服務。

### 🌐 專案網站：**[jasoncheng7115.github.io/jt-live-whisper](https://jasoncheng7115.github.io/jt-live-whisper/)**

> 功能介紹、畫面導覽、安裝與使用說明，都整理在專案網站上。

| **目錄** | [核心功能](#核心功能) · [其他特色](#其他特色) · [系統需求](#系統需求) · [快速開始](#快速開始) · [使用方式](#使用方式) · [互動式選單](#互動式選單功能一覽) · [命令列參數](#命令列參數) · [技術架構](#技術架構) · [硬體建議](#硬體建議) · [升級](#升級) |
|---|---|

核心功能涵蓋即時語音轉錄、中日韓英即時翻譯字幕、離線音訊檔批次處理、講者辨識（Speaker Diarization）、以及 LLM 會議摘要產出。採用系統音訊層級擷取（macOS 使用內建 ScreenCaptureKit，免安裝驅動；Windows 使用 WASAPI Loopback；Linux 使用 PipeWire / PulseAudio 的 monitor 來源），**理論上任何軟體的聲音輸出都能即時處理**：視訊會議（Zoom、Teams、Meet）、YouTube、Podcast、串流影片等，不限定特定應用程式。所有 AI 推論皆由地端模型完成，全程不經過第三方雲端 API。

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
- **隱私安全**：會議內容、語音資料全程留在自有設備，適合企業內部會議、機密討論。工具本身做到什麼、組織使用後還要做哪些事（個資法、GDPR、ISO/IEC 27001:2022、ISO/IEC 42001:2023）見 **[資料保護與合規](COMPLIANCE.md)**
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
| 語音辨識 (ASR) | **Whisper** (OpenAI) | **多語（中日韓英）** 主力辨識模型；base / small / large-v3-turbo / large-v3 可選 |
| 語音辨識 (ASR) | **Breeze-ASR-26** (MediaTek Research) | **台語（台灣閩南語）專用**，華語模式也可選用（台灣華語夾雜台語時）；Whisper large-v2 微調，直接輸出漢字 |
| 語音辨識 (ASR) | **Moonshine** (Useful Sensors) | **英文專用**，超低延遲串流辨識模型（不支援 Intel Mac） |
| 語音辨識 (ASR) | **Qwen3-ASR 0.6B** (Alibaba Qwen) | **實驗選項（v2.23.0）**：離線處理錄音檔時選用，中文會議與中英夾雜明顯更準（中文真實會議 20 場字錯率 28.78% → 15.75%）；限中文／英文／韓文。GPU 伺服器或本機（v2.24.0 起：Apple Silicon、NVIDIA、CPU）執行 |
| 翻譯 (LLM) | 自架 LLM 伺服器，預設 **gemma4:26b**（伺服器沒有時改用 qwen2.5:14b） | 即時與離線翻譯，透過地端 Ollama 或其他 LLM 伺服器執行；建議 14B 以上，並**選用不會思考、或思考可關閉的模型**，程式會自動關閉思考模式（gemma4、qwen3 等皆可），但 gpt-oss 系列架構上必定推理、關不掉，用於即時翻譯會明顯變慢 |
| 摘要 / 逐字稿校正 (LLM) | 自架 LLM 伺服器，預設 **qwen3.8:27b** | 會議摘要與逐字稿校正（兩者共用同一個模型）；建議 27B 以上，可與翻譯用不同模型 |
| 翻譯 (離線) | **NLLB 600M** (Meta) | 離線翻譯模型，支援中日韓英互譯（`en2zh`/`zh2en`/`ja2zh`/`zh2ja`/`ko2zh`/`zh2ko`） |
| 翻譯 (離線備援) | **Argos Translate** | 完全離線的輕量翻譯模型，僅支援英翻中 |
| 講者辨識 | **resemblyzer** + **spectralcluster** | 聲紋特徵提取 + Google 頻譜分群演算法，可在本機或 GPU 伺服器執行 |
| 講者辨識 | **Nemotron 3 Diarization** (NVIDIA，OpenMDW-1.1) | **v2.26.0 起預設使用**（transformers 5.18 以上，安裝程式自動安裝並下載模型 0.71 GB）；Intel Mac、超過 8 人時沿用上一列的方法 |
| 語音合成 (TTS) | **VoxCPM2** (OpenBMB，Apache-2.0) | **v2.27.0 起**：把文字念成台灣華語；內建 8 個 AI 產生的聲音（不是真人錄音），也可以匯入自己的台灣華語錄音。GPU 伺服器或 Apple Silicon Mac（MLX 8bit） |
| 語音合成 (TTS，選用) | **BreezyVoice** (MediaTek，Apache-2.0) | 台灣口音，但合成速度慢，不適合即時；只在 GPU 伺服器，安裝時問要不要加裝（預設否） |
| 破音字判斷 | **g2pW**（Apache-2.0）＋教育部《重編國語辭典修訂本》 | 朗讀時判斷台灣念法（辭典資料安裝時下載，著作權屬教育部，CC BY-ND 3.0 TW） |

所有模型皆在自有設備上推論（本機或區域網路內的 GPU 伺服器），**不需要任何第三方雲端 API**。

**語音辨識的推論引擎**（同一個模型可跑在不同引擎上，程式依平台與音訊來源自動選擇）：

| 引擎 | 用途 | 可跑的模型 |
|------|------|-----------|
| **whisper.cpp** | macOS 即時辨識（音訊來源為 SDL2 裝置時） | Whisper 全系列（ggml） |
| **faster-whisper** (CTranslate2) | Windows / Linux 即時辨識、全平台離線處理、GPU 伺服器 | Whisper 全系列、Breeze-ASR-26 |
| **mlx-whisper** | Apple Silicon GPU 加速（即時與台語離線） | Whisper 全系列、Breeze-ASR-26 |
| **Moonshine** | 英文超低延遲串流 | Moonshine medium / small / tiny |
| **vLLM** | GPU 伺服器的離線辨識（實驗） | Qwen3-ASR 0.6B |
| **mlx-audio** | Apple Silicon 本機離線辨識（實驗，v2.24.0） | Qwen3-ASR 0.6B（MLX 8bit） |
| **transformers** | Windows / Linux 本機離線辨識（CUDA 或 CPU，實驗，v2.24.0）；三平台講者辨識（CUDA／Apple MPS／CPU，v2.26.0） | Qwen3-ASR 0.6B、Nemotron 3 Diarization |

**語音合成的推論引擎**（v2.27.0）：

| 引擎 | 用途 | 可跑的模型 |
|------|------|-----------|
| **PyTorch**（CUDA 13） | GPU 伺服器朗讀（VoxCPM2 比說話快一點；BreezyVoice 約 1.2～2.5 倍音訊長度） | VoxCPM2（bf16）、BreezyVoice |
| **mlx-audio** | Apple Silicon Mac 本機朗讀（記憶體 16 GB 以上，大約跟說話一樣快） | VoxCPM2（MLX 8bit） |



> **為什麼講者辨識不用 pyannote.audio？** pyannote 的開源模型雖然是 MIT／CC BY 4.0 授權，但設有存取限制：下載前必須登入 HuggingFace、填寫使用者資料同意使用條件，並在本機設定 Token。這不符合本工具「零帳號、零註冊、完全地端」的設計理念。v2.26.0 起講者辨識預設使用 **NVIDIA Nemotron 3 Diarization**（OpenMDW-1.1 授權、可商用，不需帳號或 Token，安裝時自動下載；實測見 [BENCHMARKS.md](BENCHMARKS.md)）；Intel Mac、超過 8 位講者、或沒有 transformers 5.18 時，自動改用完全開源的 resemblyzer + spectralcluster。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 兩種部署方式

- **單機模式**：一台 Mac、Windows PC 或 Linux 桌機即可完成所有處理，不需要額外硬體。
  - **macOS Apple Silicon**（M1/M2/M3/M4）：透過 mlx-whisper 啟用 Metal GPU 加速，辨識速度約 1-3 秒
  - **Windows + NVIDIA GPU**：安裝程式自動偵測並啟用 CUDA 加速，單機就能享受 GPU 加速效能（辨識約 0.5-1 秒），不需另架 GPU 伺服器
  - **Linux + NVIDIA GPU**：faster-whisper 直接使用 CUDA 加速
  - **Windows / Linux 無 GPU、macOS Intel**：CPU 辨識，搭配 small 模型可用

- **本機 + GPU 伺服器模式**：本機負責音訊擷取與介面操作，語音辨識和講者辨識交由區域網路內的 GPU 伺服器處理（系統音訊和麥克風兩路都可送遠端）。離線辨識速度快 5-10 倍，即時辨識約 0.3-0.5 秒。適合需要處理大量音訊或追求最佳即時辨識品質的場景。GPU 伺服器可以是 DGX Spark、安裝有 NVIDIA GPU 的 Ubuntu/Linux 主機，搭消費級 RTX 4090/5090 之類亦可（需已安裝 CUDA）。

- **伺服器模式**：裝在無桌面的 Linux 主機（`install.sh --server`），WebUI 常駐，區網內的電腦用瀏覽器上傳錄音處理；從別台電腦操作需要密碼（安裝時自動產生管理密碼與唯讀密碼）。搭配 GPU 伺服器時 2 vCPU、4 GB 記憶體、30 GB 磁碟即可（實測 37 分鐘會議記憶體峰值約 0.4 GB）。

兩種模式可隨時切換，伺服器離線時自動降級為本機處理，不中斷使用。

**GPU 伺服器的版本會自動檢查**：伺服器上的辨識服務是獨立的一支程式，不會跟著本機升級一起更新。本機每次連上時會比對版本，不一致就提示（但不會中斷作業），並告訴你怎麼更新。另有選填的自動更新機制，設定後由本機把新版推上去、伺服器驗證通過才替換；**預設關閉**，詳見 SOP。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 核心功能

### 1. 即時語音轉錄翻譯（主要功能）
擷取系統音訊（macOS / Windows / Linux），本地端 AI 即時辨識語音並翻譯成繁體中文字幕顯示於終端機。開會、看影片、聽 Podcast 即時翻譯。

![即時英翻中字幕畫面（macOS）](images/realtime-en2zh-2.png)

![即時英翻中：翻譯速度標籤與音訊波形（macOS）](images/realtime-en2zh-3.png)

![即時英翻中字幕畫面（Windows）](images/windows-en2zh.png)

### 2. 離線音訊檔批次處理
支援 mp3 / wav / m4a / flac 等格式，使用 faster-whisper AI 模型進行離線轉錄翻譯，適合會後補做逐字稿。

> **實驗選項 Qwen3-ASR**：離線處理中文／英文／韓文錄音時可選 `qwen3-asr-0.6b`。
> 中文真實會議字錯率 28.78% → 15.75%，中英夾雜時少數語言找回 2~3 倍。日文、台語、雙向、即時字幕不提供
> （不適用時選單裡看不到，命令列指定會說明原因並改用推薦模型）。
> v2.23.0 起可在 GPU 伺服器上跑；**v2.24.0 起也能在本機跑**：Apple Silicon Mac 用 MLX（M5 上 37 分鐘會議約 1 分半）、
> NVIDIA 顯示卡用 transformers；只有 CPU 的電腦（記憶體 12 GB 以上）可以選但可能比錄音還慢。模型第一次選用時下載（Mac 2.3 GB、其他 3.4 GB）。
> 從舊版升級的 macOS／Windows，`--upgrade` 後再執行一次 `./install.sh`（Windows：`.\install.ps1`）補裝套件（Linux 的 `--upgrade` 會自動補）。詳見 SOP「Qwen3-ASR（實驗）」。

![離線處理選單：模式與模型選擇](images/offline-menu-1.png)

![離線處理選單：LLM 伺服器與講者辨識](images/offline-menu-2.png)

![離線處理選單：設定總覽與等效 CLI 指令](images/offline-menu-3.png)

### 3. 講者辨識（Speaker Diarization）
自動辨識音訊中的不同講者，以不同顏色標示，支援自動偵測或手動指定講者人數。

![講者辨識：不同講者以不同顏色顯示](images/offline-diarize-result.png)

![講者辨識：終端機逐字稿輸出](images/offline-diarize-result-2.png)

### 4. AI 會議摘要與時間軸逐字稿
透過地端 LLM 整理出**重點摘要、事件與影響、決議、待辦（負責人、期限）、風險、未決問題、議題時間軸、發言統計**，另附校正逐字稿。
v2.25.0 起改用 [jt-doc-tools](https://jasoncheng7115.github.io/jt-doc-tools/) 的會議分析：**每一條都附逐字稿的時間點，而且會比對引用的原文是不是真的講到這件事**，對不上的不會列出來；重點摘要只從驗證過的項目寫。HTML 版的時間點可以點，跳到下面依據的那段原文；搭配講者辨識時，不同講者以不同顏色區分。

![AI 會議摘要：每一條都附逐字稿時間點](images/meeting-summary.png)

時間逐字稿 HTML 內嵌音訊播放器與波形圖，可直接點選波形任意位置跳至該時間點；播放時對應的逐字稿段落會即時以高亮區塊標示，方便對照聆聽。

![時間逐字稿 HTML](images/offline-transcript.png)

### 5. 多模式語音轉錄
16 種功能模式：英翻中 / 中翻英 / 日翻中 / 中翻日 / **韓翻中** / **中翻韓** / 英中雙向 / 日中雙向 / **韓中雙向** / 純英文轉錄 / 純中文轉錄 / 純日文轉錄 / **純韓文轉錄** / **台語轉錄** / **台翻英** / 純錄音，滿足各種使用場景。

**台語（台灣閩南語）支援**：採用 [MediaTek Breeze-ASR-26](https://huggingface.co/MediaTek-Research/Breeze-ASR-26)（Whisper large-v2 的台語微調版，Apache-2.0），辨識結果**直接輸出漢字**，不需另外翻譯。即時與離線皆可用，模型依平台自動選擇（Apple Silicon 用 mlx 4bit 877MB、CPU 用 int8、NVIDIA GPU 用 float16）。

```bash
./start.sh --mode nan                      # 即時台語字幕
./start.sh --input 台語錄音.mp3 --mode nan   # 離線台語逐字稿
```

> 台語模型是 large-v2 微調（decoder 層數約為 large-v3-turbo 的 8 倍），先天較慢：Apple Silicon 約 1.3 倍即時，純 CPU 約 0.24 倍即時（1 小時音訊需約 4 小時），建議搭配 Apple Silicon 或 NVIDIA GPU 使用。

> **華語模式也可選用 Breeze-ASR-26**：台灣的會議常是華語為主、夾雜台語，可在中文轉錄 / 中翻英 / 中翻日 / 中翻韓模式選 `breeze-asr-26`，與 large-v3 比較哪個適合自己的錄音（`./start.sh --input 會議.mp3 --mode zh -m breeze-asr-26`）。選用時自動套用本模型專屬的辨識參數，並固定在本機辨識。

![即時日翻中字幕畫面（Windows）](images/realtime-ja2zh.png)

### 6. 雙向字幕模式
英中雙向（`en_zh`）、日中雙向（`ja_zh`）和韓中雙向（`ko_zh`），同時擷取系統音訊與麥克風，對方外語翻中文、自己中文翻外語，適用於雙語視訊會議。

![英中雙向即時字幕（終端機）](images/bidi-en-zh-cli.png)

![英中雙向離線逐字稿（HTML 聊天風格）](images/bidi-en-zh-html.png)

![日中雙向即時字幕](images/bidi-ja-zh.png)

![日中雙向離線逐字稿（HTML 聊天風格）](images/bidi-ja-zh-html.png)

### 7. 文字轉語音：台灣華語朗讀（v2.27.0）
WebUI「輸入來源」選「**文字內容朗讀**」：貼上文字或選文字檔，邊念邊顯示字幕（字幕模式、懸浮字幕都可以用）；選「**文字轉語音檔**」直接存成 MP3／WAV。命令列：`./start.sh --tts-file 講稿.txt`。
在自己的 **GPU 伺服器** 或 **Apple Silicon Mac** 上合成，不使用作業系統內建的語音。

- **合成模型**：預設 [OpenBMB VoxCPM2](https://huggingface.co/openbmb/VoxCPM2)（Apache-2.0），**選它當預設是為了速度與效能**：GPU 伺服器比說話快一點、Mac 本機大約跟說話一樣快，兩種機器都能跑，邊念邊播不會卡。
  另外可選 [MediaTek BreezyVoice](https://huggingface.co/MediaTek-Research/BreezyVoice-300M)（Apache-2.0）：**台灣口音，但合成速度慢，不適合即時**（合成時間約音訊長度的 1.2～2.5 倍），只在 GPU 伺服器；安裝或升級時會問要不要加裝（預設否），適合轉成語音檔、不趕時間的時候
- **台灣念法**：VoxCPM2 主要學的是大陸念法，我們花了不少功夫讓它念台灣話：
  - **以台灣日常說法為準**：教育部《重編國語辭典修訂本》為基礎，破音字依上下文判斷（銀行、便宜、垃圾念ㄌㄜˋ ㄙㄜˋ、伺服器的「伺」念ㄙˋ）；
    辭典的念法跟日常說法不同的改用日常說法（市場的「場」念ㄔㄤˇ、強制的「強」念ㄑㄧㄤˊ、參與、擷取、液化、亞洲、包括、角色）；拿不準的做成同一句兩種念法，由人試聽決定
  - 用近 400 句台灣華語句子（新聞、生活對話）自動合成、再辨識回來逐字比對，並把將近 5,000 句裡指定的念法逐一檢查是不是台灣日常說法，找出念錯的地方一一修正（例：很差的「差」、阿嬤、一曝十寒、「一種生物」不再被切成「種生」）
  - 模型不認得的罕用字（矽谷的「矽」、人名用字）一律標上注音；文字轉成模型最熟悉的寫法再送進去，念錯的地方少了約八成
  - 管理者可以加自訂發音（預設「和」念ㄏㄢˋ），並預覽送進模型的文字
- **英文與數字**：英文、日期、時間、IP、版本號、電話照原文念；千分位逗號、負數、金錢符號（NT$、$）先換成念得對的寫法
- **聲音**：內建 8 個（女聲、男聲各 4 個：溫柔、主播、活潑、沉穩／低沉、清爽、主播、溫和），由 VoxCPM2 依文字描述產生，**不是真人錄音**，裝好就能念。
  也可以匯入自己的：管理者匯入一段 10～20 秒的台灣華語錄音與逐字稿，**必須取得錄音者的書面同意**
- 聲音依性別分組；語速 0.8～1.5 倍（音調不變）、段落停頓長短；暫停、停止（已念的照樣存檔）
- 每一句顯示合成花的時間與播放等了多久；「從這段念」從任一句重念、結束後「重新朗讀」
- 播放到這台電腦的喇叭（可選裝置），或在開著網頁的那台電腦的**瀏覽器**播放（伺服器、容器沒有喇叭也能用）；可同時存成 MP3／WAV
- 本工具的摘要檔只念「重點摘要」；字幕檔不念時間軸。長文只預先合成接下來兩段

![文字轉語音：台灣華語朗讀](images/tts-settings.png)

### 8. 雙向語音口譯（英中，v2.28.0）
英中雙向即時模式多一個「**語音口譯**」：**對方說的英文翻成中文念給你聽**（耳機），**你說的中文翻成英文念給對方聽**（經虛擬麥克風送進會議軟體）。字幕與逐字稿照舊。合成在 GPU 伺服器。

> **事前準備：念給對方聽要有「虛擬麥克風」**（會議軟體只能從麥克風收聲音）
> - **macOS：要先安裝 [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)**（免費，GPL-3.0）：`brew install --cask blackhole-2ch`，需要管理者密碼、裝完重新開機；會議軟體的麥克風改選「BlackHole 2ch」
> - **Linux**：不用安裝，程式自動建立、結束時移除
> - **Windows（v2.29.0 起）：要先安裝 usbip-win2**（免費、開放原始碼 BSD-2-Clause，驅動由微軟簽署）：在安裝資料夾執行 `.\install.ps1 -InterpMic`（會跳「使用者帳戶控制」；安裝時 USB 鍵盤、滑鼠、耳機會斷線幾秒，請不要在會議中安裝）。之後選「自動建立虛擬麥克風」，開始時建立「jt-live-whisper Interpreter Mic」、結束時移除；會議軟體的麥克風改選它。Windows 11 的「智慧型應用程式控制」：jt-live-whisper 直接跟 usbip-win2 的驅動（微軟簽署）溝通，不載入它沒有簽章的程式庫，通常不受影響；仍然被擋時的處理方式見 SOP 4-16
>
> 「念給我聽」三個平台都不用另外安裝。

- **接線**：請戴耳機。念給對方聽時，會議軟體（Zoom、Teams、Meet）的麥克風改選虛擬麥克風，結束後記得改回來
- **不會把自己的話翻來翻去**：系統音訊錄到念給你聽的中文時（那段期間正在念），GPU 伺服器先判斷是中文就不辨識；念過的句子 20 秒內又被辨識到（耳機漏音、對方沒有回音消除）也略過
- **邊合成邊播**：第一段約 0.3～0.7 秒就出聲；GPU 伺服器忙、合成比說話慢時，先存一段再播，不會播到一半斷掉
- **跟不上的時候**：給對方的優先；排了 2 句以上用 1.2 倍語速；等超過 15 秒的不念（字幕照樣顯示）
- 每一句標口譯狀態（排隊、念出中、已念、略過）；給對方、還沒念到的可以取消；兩個方向各有靜音鈕
- 第一次念給對方聽之前先說「Hi, I'm using an AI interpreter, so there will be a short delay.」（可關掉）；預設對方只聽到英文，可選「同時送出我的原聲」（念英文時原聲自動調小）
- 英文聲音：內建 2 個 AI 產生的英文聲音（女聲、男聲，不是真人錄音），預設男聲；英文的數字、金額、IP、版本號先換成念得對的寫法
- 開啟：WebUI「英中雙向」即時模式的「語音口譯」區；互動選單選「英中雙向」時會問；或命令列

```bash
# 英中雙向＋語音口譯：中文念到耳機、英文送到虛擬麥克風（Linux、Windows 用 auto 自動建立）
./start.sh --mode en_zh --speak-me default --speak-them "BlackHole 2ch"
.\start.ps1 --mode en_zh --speak-me default --speak-them auto     # Windows（先執行一次 .\install.ps1 -InterpMic）
```

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 其他特色

- **同時轉錄麥克風**：所有即時模式加上 `--mic` 即可同時轉錄自己的麥克風語音，雙向模式自動啟用
- **多種本地端 AI 語音辨識引擎**：即時辨識：Whisper（高準確度）/ Moonshine（超低延遲 ~300ms）；離線音訊檔轉錄：faster-whisper（支援 VAD 靜音過濾）
- **多種本地端翻譯引擎**：LLM 大型語言模型（Ollama / OpenAI 相容伺服器）、NLLB 離線翻譯（中日韓英互譯）或 Argos 離線翻譯
- **會議主題感知翻譯**：可指定會議主題（如「ZFS 儲存管理」），讓 LLM 根據領域上下文精準翻譯專業術語
- **自動偵測 LLM 伺服器**：支援 Ollama、LM Studio、Jan.ai、vLLM、LocalAI、llama.cpp、LiteLLM 等本地端 LLM 伺服器
- **互動式選單 + CLI 模式**：新手友善的選單介面，進階用戶可用命令列參數直接啟動
- **WebUI 瀏覽器介面**：`./start.sh --webui` 在瀏覽器中操作所有功能，支援即時字幕、離線處理、講者辨識、摘要、台灣華語朗讀，手機/平板也可使用
- **關鍵字即時通知**：設定關鍵字，即時辨識出現時自動發出通知。可用於追蹤會議重點、開會時提醒留意關鍵議題，或線上課程摸魚時讓系統在「請實作」「這個會考」時自動提醒。支援全螢幕警示特效、瀏覽器推播、音效提示（警示/柔和可選）、懸浮字幕閃爍，同一關鍵字冷卻機制避免重複通知
- **字幕轉發**：即時字幕自動轉發到通訊平台（Telegram / Slack / Discord / Teams / LINE / Nextcloud Talk / 通用 API），可同時啟用多個平台、自訂發送間隔與內容（含時間/原文/譯文）。通用 API 支援 Body 範本（`{{text}}` 變數）搭配自訂 Headers
- **懸浮字幕**（感謝 OSSLab 熊大提供建議）：桌面半透明字幕覆蓋視窗（PyQt6），可疊加於任何應用程式上方。字體依視窗大小自動縮放、可拖曳移動與調整大小、滑鼠穿透模式、字幕切換淡入淡出動畫。單語/雙語自動切換高度

**關鍵字即時通知**：設定關鍵字後，辨識結果出現時全螢幕警示 + 音效提醒：

![關鍵字通知效果](images/keyword-alert.png)

![關鍵字通知設定](images/keyword-alert-settings.png)

**懸浮字幕**：半透明覆蓋視窗，疊加於任何應用程式上方：

![懸浮字幕效果](images/subtitle-overlay.png)

![懸浮字幕設定](images/subtitle-overlay-settings.png)

**字幕轉發**：即時字幕自動轉發到 Telegram 等通訊平台：

![字幕轉發設定](images/forward-telegram-settings.png)

![Telegram 轉發效果](images/forward-telegram-result.png)

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 系統需求

**macOS：**
- macOS（Apple Silicon / Intel）
- Python 3.12+
- [Homebrew](https://brew.sh/)（需事先安裝）
- [BlackHole 2ch](https://existential.audio/blackhole/)（虛擬音訊驅動；**選配**，macOS 12 以下或不使用 ScreenCaptureKit 時才需要，安裝腳本會協助安裝）

**Windows：**
- Windows 10 以上
- Python 3.12+（從 [python.org](https://www.python.org/downloads/) 安裝，勾選「Add to PATH」）
- PowerShell 5.1+（Windows 10 內建）

**Linux：**
- Ubuntu 22.04 / Debian 12 以上（其他發行版可用，但系統套件需自行安裝）
- Python 3.10+（含 `python3-venv`）
- 桌面版：PipeWire 或 PulseAudio（Ubuntu 桌面版預設即有），**不需要安裝虛擬音效卡**
- 伺服器版（無桌面）：可做離線處理，WebUI 以 systemd 服務常駐；搭配 GPU 伺服器時 2 vCPU／4 GB／30 GB 即可
- 安裝腳本會以 `sudo apt` 自動補齊 ffmpeg、PortAudio、pulseaudio-utils、中文字型等系統套件

**共通：**
- 本地端 LLM 伺服器（推薦 [Ollama](https://ollama.com/)，翻譯/摘要用。推薦搭配 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/) 執行 Ollama，CP 值高。**沒有 LLM 伺服器也能用**：程式可切換為 NLLB/Argos 離線翻譯引擎，完全不需額外伺服器，但摘要功能需要 LLM）

### 磁碟空間需求

安裝腳本會在安裝前自動檢查可用空間是否足夠。

#### 本機

| 元件 | 大小 | 說明 |
|------|------|------|
| Python venv + 套件 | ~1.1 GB | ctranslate2, faster-whisper, resemblyzer, spectralcluster 等 |
| whisper.cpp | ~60 MB | macOS: 原始碼編譯；Windows: 原始碼編譯（選用，沒有時即時辨識改用 faster-whisper）；Linux: 不需要（改用 faster-whisper） |
| Whisper GGML 模型 | 1.5~6.4 GB | 預設 large-v3-turbo (1.5GB)；全部 5 個模型共 6.4 GB |
| Moonshine 模型 | ~245 MB | 英文即時辨識（選用） |
| NLLB 600M 翻譯模型 | ~600 MB | 離線翻譯（中日韓英互譯） |
| Argos 翻譯模型 | ~83 MB | 離線備援翻譯（僅英翻中） |
| Homebrew 套件 | ~140 MB | cmake + sdl2 + ffmpeg（僅 macOS） |
| HuggingFace 快取 | ~5.3 GB | `~/.cache/huggingface/`，`--input` 離線處理用，首次使用時下載 |
| **最小安裝** | **~3 GB** | venv + 1 個 Whisper 模型 + 基本套件 |
| **推薦安裝** | **~8 GB** | 加上 HuggingFace 快取（離線處理音訊檔用） |
| **完整安裝** | **~14 GB** | 全部 Whisper 模型 + HuggingFace 快取 + Moonshine |
| 文字轉語音（Mac，選配） | ~4 GB | VoxCPM2 MLX 8bit 3.2 GB ＋ g2pW 0.6 GB（Apple Silicon、記憶體 16 GB 以上） |

#### GPU 伺服器（選配）

| 元件 | 大小 | 說明 |
|------|------|------|
| PyTorch GPU (CUDA) | ~2.5 GB | 依 CUDA 版本而異 |
| Python venv + 套件 | ~1 GB | faster-whisper, fastapi, resemblyzer 等 |
| Whisper 模型 | ~6 GB | 5 個模型（CTranslate2 格式），首次安裝時下載 |
| openai-whisper | ~500 MB | CTranslate2 CUDA 不可用時才安裝 |
| **最小安裝** | **~5 GB** | PyTorch + 1 個模型 |
| **完整安裝** | **~12 GB** | PyTorch + 全部 5 個模型 + 講者辨識套件 |
| 文字轉語音（選配） | ~11 GB | 獨立的 Python 環境 5.2 GB（CUDA 13 版 PyTorch）＋ VoxCPM2 4.7 GB ＋ g2pW 0.6 GB；安裝時要 20 GB 可用空間 |
| BreezyVoice（選配） | ~8 GB | 獨立的 Python 環境 5.5 GB ＋ 模型 2.2 GB；安裝時要 16 GB 可用空間 |

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

**Linux（Ubuntu / Debian）：**

打開終端機，貼上以下指令（安裝過程會用 `sudo` 補齊系統套件）：

**桌機（有圖形桌面）：**

```bash
mkdir -p ~/Apps/jt-live-whisper && cd ~/Apps/jt-live-whisper
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh -o install.sh
bash install.sh
```

**無桌面伺服器（WebUI 以 systemd 服務常駐，供區域網路其他電腦使用）：**

```bash
mkdir -p ~/Apps/jt-live-whisper && cd ~/Apps/jt-live-whisper
curl -fsSL https://raw.githubusercontent.com/jasoncheng7115/jt-live-whisper/main/install.sh -o install.sh
bash install.sh --server
```

`install.sh` 偵測到 Linux 會自動改用 `install-linux.sh`。安裝完成後可執行 `./install.sh --doctor` 檢查音訊、套件、GPU 與伺服器連線是否正常。

伺服器版第一次安裝時會**自動產生管理密碼（上傳、開始／停止作業）與唯讀密碼（看畫面、讀逐字稿）並各印出一次**，請當下記下來。
**請用一般帳號安裝**：服務會以該帳號執行；用 root 安裝時會以 root 執行並出現警告。

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

> **Windows 的 PowerShell 預設不允許執行腳本**：上面用 `-ExecutionPolicy Bypass` 只對這一次有效。v2.27.0 起安裝程式（含 `-Upgrade`）在 Windows 預設狀態時會自動改成允許執行本機腳本（RemoteSigned，只影響目前使用者；從網路下載、沒有簽章的腳本照樣擋），之後就能直接打 `.\start.ps1`、`.\install.ps1 -Upgrade`。執行原則是你或公司刻意設過的不會自動改（有人在終端機前才問，預設否），那時改用 `powershell -ExecutionPolicy Bypass -File start.ps1`。

安裝腳本會自動下載並設定所有地端 AI 模型和相依套件（Whisper 語音辨識模型、Moonshine 串流辨識模型、NLLB 離線翻譯模型、Argos 離線翻譯模型等）。安裝最後會詢問是否設定 GPU 語音辨識伺服器（選填），若有安裝 NVIDIA GPU 的 Ubuntu/Linux 主機（消費級 RTX 4090/5090 亦可，需已安裝 CUDA），可透過 SSH 自動在伺服器安裝 PyTorch、faster-whisper 等套件，大幅加速語音辨識。

> 首次安裝預估時間：約 10~20 分鐘（視網路速度而定，主要是下載 AI 模型。macOS 需額外編譯 whisper.cpp；Linux 不需要）

### 2. 設定音訊裝置

#### macOS（macOS 13 以上：授權一次即可，不必安裝驅動）

**自 v2.17.0 起**，預設使用系統內建的 **ScreenCaptureKit** 擷取系統音訊：**不需要安裝 BlackHole、不需要建立多重輸出裝置、不需要重開機，Zoom / Teams 的喇叭與麥克風設定也完全不用改**，喇叭或耳機照常出聲。

唯一需要做的是授權一次「螢幕錄製」權限：

- macOS 把 ScreenCaptureKit 歸類在「螢幕錄製」之下，即使程式只取音訊、不擷取畫面（macOS 15 顯示為「螢幕與系統音訊錄製」）
- 授權對象是**啟動本程式的終端機程式**（Terminal / iTerm2 / Ghostty / VS Code…），不是 Python。程式會自動判讀並在提示中指名該勾選哪一個
- 首次執行時會直接詢問是否開啟授權對話框；若曾按過拒絕，會自動開啟系統設定對應頁面
- **授權後必須用 Cmd+Q 完全結束該終端機程式再重新開啟**，權限才會生效（macOS 的規定）
- 隨時可執行 `./start.sh --sck-permission` 重新授權；WebUI 設定頁也有授權按鈕

```
對方說話 → Zoom/Teams → 耳機（你聽到，設定不用改）
                      → ScreenCaptureKit（程式擷取）→ AI 辨識 → 字幕
```

> **注意：** 系統設為靜音時，ScreenCaptureKit 只會收到無聲訊號。用耳機聽沒問題，但不要靜音。
> 錄音想同時錄下自己的聲音，選「系統音訊 + 麥克風」的混合錄音即可，不需要聚集裝置。

<details>
<summary><b>macOS 12 以下，或不想授權螢幕錄製：改用 BlackHole（點開展開）</b></summary>

安裝 BlackHole 後需要**重新啟動電腦**，然後在「音訊 MIDI 設定」中建立虛擬裝置。
也可用 `--audio-source blackhole` 強制走此流程。

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

</details>

> **提示：** 即時辨識預設處理系統音訊（對方/應用程式的聲音）。加上 `--mic` 參數即可同時轉錄你自己的麥克風語音，或使用雙向模式（`en_zh` / `ja_zh`）自動啟用雙路辨識。

#### Windows

Windows 不需要安裝額外的虛擬音訊驅動。程式透過 WASAPI Loopback 直接擷取系統播放的音訊，大多數情況下不需要手動設定。

如果自動偵測失敗，可嘗試啟用「立體聲混音」（Stereo Mix）：右鍵通知區域音量圖示 → 音效設定 → 錄製 → 右鍵「顯示已停用的裝置」→ 啟用「立體聲混音」。

驗證：執行 `.\start.ps1 --list-devices` 確認列表中有 loopback 裝置。

#### Linux

Linux 也**不需要安裝虛擬音效卡**。程式直接從「預設喇叭」的 monitor 來源錄音（PipeWire 與 PulseAudio 都有提供），喇叭或耳機照常出聲，會議軟體的設定不用改。

```
對方說話 → Zoom/Teams → 預設喇叭 / 耳機（你聽到）
                      → monitor 來源（程式以 parec 擷取）→ AI 辨識 → 字幕
```

- 要擷取的是**目前的預設輸出裝置**；換了耳機或喇叭，程式下次偵測時會自動跟著換
- 想指定其他來源：設定環境變數 `JTLW_MONITOR_SOURCE`，或在 `config.json` 加上 `"linux_monitor_source": "來源名稱"`（名稱可用 `pactl list short sources` 查詢）
- 即時模式必須在**桌面工作階段內**執行；透過 SSH 連線時連不到使用者的音訊伺服器，只能做離線處理
- 錄音選「系統音訊 + 麥克風」即可同時錄下雙方聲音

驗證：執行 `./start.sh --list-devices`，應看到 `[-500] 系統音訊（…）`。

### 3. 安裝地端 LLM（翻譯/摘要用）

LLM 伺服器可安裝在本機或區域網路內的其他主機。推薦使用 [Ollama](https://ollama.com/)：

```bash
# macOS：透過 Homebrew 安裝
brew install ollama

# Windows：從 https://ollama.com/ 下載安裝程式

# Linux：官方安裝腳本
curl -fsSL https://ollama.com/install.sh | sh

# 下載推薦的翻譯模型（各平台皆同）
ollama pull gemma4:26b      # 預設翻譯模型（約 17GB；記憶體不足可改 qwen2.5:14b）
```

> **推薦硬體：** 如果有 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/)（128GB 記憶體），將 Ollama 安裝在 DGX Spark 上是非常實惠的選擇：可執行更大的模型、翻譯品質更好、推論速度更快，透過 `--llm-host` 指向即可。

> **不裝 LLM 也能翻譯：** 程式可切換為 NLLB（中日韓英互譯，品質 7-8/10）或 Argos（僅英翻中）離線翻譯引擎，完全不需要額外伺服器。注意：摘要功能仍需 LLM 伺服器。

### 4. 啟動

先切換到安裝目錄：

```bash
# macOS / Linux
cd ~/Apps/jt-live-whisper

# Windows (PowerShell)
cd C:\jt-live-whisper
```

啟動程式：

```bash
# macOS / Linux
./start.sh

# Windows (PowerShell)
.\start.ps1
```

程式會進入互動式選單，依序選擇功能模式、翻譯引擎、AI 辨識模型等設定。音訊裝置全自動偵測，不需手動選擇。

![互動式選單](images/interactive-menu.png)

![互動式選單：使用場景、錄音設定與開始即時翻譯](images/interactive-menu-2.png)

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 使用方式

> 以下範例以 macOS / Linux 指令為主。Windows 使用者請將 `./start.sh` 替換為 `.\start.ps1`，安裝目錄為 `C:\jt-live-whisper`。其餘參數完全相同。

### WebUI 瀏覽器介面（推薦）

```bash
./start.sh --webui            # macOS / Linux
.\start.ps1 --webui           # Windows
```

自動開啟瀏覽器（預設 `http://localhost:19781`），在網頁中完成所有設定後按「開始」即可。
安裝（或升級）結束時可以選擇建立**桌面與應用程式選單捷徑**（macOS「應用程式」、Windows「開始」功能表、Linux 應用程式選單），點兩下就以 WebUI 模式啟動；已經開著時再點一次會直接開啟原本那個（v2.25.4 起）。捷徑圖示是 jt-live-whisper 的 logo（v2.26.2 起）。

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
./start.sh                    # macOS / Linux
.\start.ps1                   # Windows

# CLI 模式（跳過選單）
./start.sh --mode en2zh --engine llm --llm-model gemma4:26b

# 英中雙向字幕（對方英文翻中文 + 自己中文翻英文）
./start.sh --mode en_zh

# 日中雙向字幕（對方日文翻中文 + 自己中文翻日文）
./start.sh --mode ja_zh

# 韓中雙向字幕（對方韓文翻中文 + 自己中文翻韓文）
./start.sh --mode ko_zh

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

# 中文會議用 Qwen3-ASR（實驗；有 GPU 伺服器用伺服器，加 --local-asr 在本機跑）
./start.sh --input meeting.mp3 --mode zh -m qwen3-asr-0.6b --diarize
```

**產出檔案**（存於 `logs/<session>/`）：

| 檔案 | 說明 | 需要 LLM |
|------|------|----------|
| `時間逐字稿_*.txt` | 帶時間戳逐字稿（翻譯模式含原文+譯文） | 校正需要 |
| `時間逐字稿_*.html` | 互動式逐字稿（點按時間戳可播放音訊） | 校正需要 |
| `時間逐字稿_*.srt` | SRT 字幕檔 | 否 |
| `時間逐字稿_*.vtt` | WebVTT 字幕檔 | 否 |
| `摘要_*.txt` | 會議摘要（重點摘要、決議、待辦、風險、議題，每一條附時間點）+ 校正逐字稿 | 是 |
| `摘要_*.html` | 會議摘要 HTML（時間點可點、議題時間軸、發言佔比、心智圖、相關檔案連結） | 是 |

> 有設定 LLM 伺服器時，逐字稿會自動經過 LLM 校正（修正 ASR 辨識錯字），純轉錄模式同樣支援。

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
| 2 | 功能模式 | 英翻中 / 中翻英 / 日翻中 / 中翻日 / 韓翻中 / 中翻韓 / 英中雙向 / 日中雙向 / 韓中雙向 / 英文轉錄 / 中文轉錄 / 日文轉錄 / 韓文轉錄 / 台語轉錄 / 台翻英 / 純錄音 | 16 種模式，分群顯示（單向翻譯、雙向翻譯、轉錄、其他） |
| 3 | 麥克風轉錄 | 是 / 否 | 轉錄模式（en/zh/ja）詢問是否同時轉錄麥克風 |
| 4 | 辨識位置 | GPU 伺服器 / 本機 | 有設定 GPU 伺服器時才顯示 |
| 5 | ASR 引擎 | Whisper / Moonshine | 英文模式可選 Moonshine（超低延遲），其他語言固定 Whisper |
| 6 | 辨識模型 | large-v3-turbo / large-v3 / small / base 等 | 依裝置效能自動推薦適合的模型大小 |
| 7 | 翻譯引擎 | LLM 伺服器 / NLLB 離線 / Argos 離線 | 翻譯模式才顯示，自動偵測可用的 LLM 伺服器 |
| 8 | 翻譯模型 | 伺服器上的模型清單 | 動態查詢 LLM 伺服器上已安裝的模型 |
| 9 | 會議主題 | 自由輸入 | 選填，提升 LLM 翻譯專業術語的準確度 |
| 10 | 音訊場景 | 會議 / 教育訓練 / 快速字幕 | 調整音訊緩衝長度，影響延遲與辨識品質 |
| 11 | 錄音設定 | 混合錄製 / 僅播放音訊 / 不錄音 | 是否同步錄製音訊為檔案 |
| 12 | 確認啟動 | Y / n | 顯示等效 CLI 指令，確認後開始 |

#### 離線處理選單（讀入檔案）

| 步驟 | 選單項目 | 選項 | 說明 |
|------|----------|------|------|
| 1 | 功能模式 | 英文轉錄+中文翻譯 / 中文轉錄+英文翻譯 / 日文轉錄+中文翻譯 / 中文轉錄+日文翻譯 / 韓文轉錄+中文翻譯 / 中文轉錄+韓文翻譯 / 英中雙向 / 日中雙向 / 韓中雙向 / 台語轉錄 / 台翻英 / 純轉錄 | 15 種模式（不含純錄音） |
| 2 | 辨識位置 | GPU 伺服器 / 本機 | GPU 伺服器辨識速度快 5-10 倍 |
| 3 | 辨識模型 | large-v3-turbo / large-v3 / small / base / breeze-asr-26 / qwen3-asr-0.6b（實驗） | 依辨識位置推薦模型，伺服器模式顯示快取標籤；qwen3-asr-0.6b 只在中英韓單向模式、且所選位置跑得了時出現（GPU 伺服器已裝好，或本機有 mlx-audio／transformers） |
| 4 | LLM 伺服器 | host:port | 翻譯模式才詢問，自動偵測伺服器類型 |
| 5 | 翻譯模型 | 伺服器模型 / NLLB 離線 / Argos 離線 | 動態列出伺服器模型 + 本機離線選項 |
| 6 | 講者辨識 | 不辨識 / 自動偵測 / 指定人數 | 自動偵測或手動指定 2~20 位講者 |
| 7 | 摘要與校正 | 摘要+校正逐字稿 / 只摘要 / 只逐字稿 | 需 LLM 伺服器，無 LLM 時僅產出逐字稿 |
| 8 | 摘要模型 | 伺服器模型清單 | 選了摘要才顯示，建議 27B 以上（預設 qwen3.8:27b） |
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
| `--mode MODE` | 功能模式 (`en2zh` / `zh2en` / `ja2zh` / `zh2ja` / `ko2zh` / `zh2ko` / `en_zh` / `ja_zh` / `ko_zh` / `en` / `zh` / `ja` / `ko` / `nan` / `nan2en` / `record`) | `en2zh` |
| `--asr ASR` | 語音辨識引擎 (`whisper` / `moonshine` / `faster-whisper`) | `whisper` |
| `-m`, `--model MODEL` | 辨識模型 (`base.en` / `base` / `small.en` / `small` / `large-v3-turbo` / `large-v3` / `breeze-asr-26` / `qwen3-asr-0.6b`)；`qwen3-asr-0.6b` 為實驗選項，限離線、中英韓單向；在 GPU 伺服器或本機（`--local-asr`）執行 | 依裝置推薦 |
| `--moonshine-model MODEL` | Moonshine 模型 (`medium` / `small` / `tiny`) | `medium` |
| `-s`, `--scene SCENE` | 使用場景 (`meeting` / `training` / `presentation` / `subtitle`) | `training` |
| `-e`, `--engine ENGINE` | 翻譯引擎 (`llm` / `nllb` / `argos`) | `llm` |
| `--llm-model MODEL` | LLM 翻譯模型 | `gemma4:26b`（伺服器沒有時改用 `qwen2.5:14b`） |
| `--llm-host HOST` | LLM 伺服器位址（自動偵測 Ollama 或 OpenAI 相容） | |
| `--topic TOPIC` | 會議主題（提升翻譯與摘要品質） | |
| `-d`, `--device ID` | 音訊裝置 ID（可用 `--list-devices` 查詢） | 自動偵測 |
| `--list-devices` | 列出可用音訊裝置後離開 | |
| `--input FILE [...]` | 離線處理音訊檔 | |
| `--diarize` | 啟用講者辨識（需搭配 `--input`） | |
| `--num-speakers N` | 講者人數（需搭配 `--diarize`）。Nemotron 下是**上限**、現行方法下是強制分群；不確定時不要填，要填寧可多不要少 | 自動偵測 |
| `--diarize-engine ENGINE` | 講者辨識方法 `auto`（能用 Nemotron 就用）/ `nemotron` / `legacy`（resemblyzer） | `auto` |
| `--summarize [FILE ...]` | 生成 AI 摘要（與 `--input` 合用時不需指定檔案） | |
| `--summary-model MODEL` | 摘要用 LLM 模型 | `qwen3.8:27b` |
| `--mic` | 同時轉錄麥克風語音（即時模式） | |
| `--record` | 即時模式同時錄製音訊 | |
| `--rec-device ID` | 錄音裝置 ID（可與辨識裝置不同） | |
| `--rec-source SRC` | 純錄音錄哪些聲音：`both`（系統音訊＋麥克風）／`system`／`mic`；`-d` 指定系統音訊、`--mic-device` 指定麥克風 | 偵測得到麥克風時 `both` |
| `--denoise` | 即時模式啟用背景降噪 | |
| `--local-asr` | 強制使用本機辨識（忽略 GPU 伺服器設定） | |
| `--restart-server` | 強制重啟 GPU 伺服器 | |
| `--speak-me DEV` | 雙向語音口譯：對方的英文翻成中文後念給我聽（`default`、裝置編號或名稱的一部分；請用耳機）。搭配 `--mode en_zh` | |
| `--speak-them DEV` | 雙向語音口譯：我的中文翻成英文後念進虛擬麥克風（macOS 先安裝 BlackHole 2ch 再指定 `BlackHole`；Linux 用 `auto` 自動建立；Windows 先執行 `.\install.ps1 -InterpMic` 安裝 usbip-win2，再用 `auto`） | |
| `--speak-me-voice ID`／`--speak-them-voice ID` | 兩個方向各自的聲音（`--tts-list` 列出；念給對方聽預設用內建的英文聲音） | |
| `--speak-me-rate R`／`--speak-them-rate R` | 兩個方向各自的語速（不是 1 時不用串流合成） | `1` |
| `--interp-intro TEXT` | 第一次念給對方聽之前的開場說明，`none` 不念 | `Hi, I'm using an AI interpreter, so there will be a short delay.` |
| `--passthrough` | 念給對方聽時，你的原聲也同時送進虛擬麥克風（念英文時原聲自動調小）。要搭配 `--speak-them` | 只送英文 |

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
  subtitle_overlay.py      懸浮字幕覆蓋視窗（PyQt6，啟用時由主程式自動啟動）
  start.sh                 啟動腳本（macOS / Linux）
  start.ps1                啟動腳本（Windows）
  install.sh               安裝腳本（macOS；Linux 自動轉交 install-linux.sh）
  install-linux.sh         安裝腳本（Linux，含 --server / --doctor / --uninstall）
  install.ps1              安裝腳本（Windows）
  remote_whisper_server.py GPU 伺服器端 Whisper 辨識服務（選配）
  jtlw_tls.py              WebUI 與 REST API 共用的 TLS（HTTPS）模組
  sck_audio_capture.swift  macOS 系統音訊擷取元件（安裝時自動編譯）
  jtdt_meeting/            會議摘要的會議分析（jt-doc-tools 的程式，原封不動）
  jtlw_api/                REST API（給其他系統串接，伺服器版；介面規格在 schemas/）
  config.json              使用者設定（自動產生，含 LLM/GPU/WebUI 密碼等）
  SOP.md                   完整使用手冊
  CHANGELOG.md             版本更新記錄
  BENCHMARKS.md            實測紀錄（選模型、改預設值的依據）
  logs/                    轉錄記錄檔、AI 摘要檔、HTML 逐字稿（自動建立）
  recordings/              暫存音訊轉檔（自動建立）
  api_data/                REST API 的作業紀錄、上傳暫存、憑證（啟用 API 後自動建立）
  whisper.cpp/             whisper.cpp 即時辨識引擎（macOS 自動編譯，Windows 自動編譯且為選用，Linux 不使用）
  venv/                    Python 虛擬環境（安裝時自動建立）
```

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 技術架構

```
即時模式：
  系統音訊（macOS: ScreenCaptureKit 或 BlackHole / Windows: WASAPI Loopback / Linux: PipeWire・PulseAudio monitor）
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
    → 啟動 translate_meeting.py 子行程
    → TCP localhost:19780 接收即時事件
    → WebSocket 推送到瀏覽器（即時字幕、進度、狀態）
    → 支援遠端觀看（密碼保護）、手機/平板

文字轉語音（WebUI 輸入來源「文字內容朗讀／文字轉語音檔」、./start.sh --tts-file）：
  文字 → 台灣念法（自訂發音 > 台灣日常念法 > 教育部辭典 > g2pW）
    → VoxCPM2（GPU 伺服器 PyTorch / Apple Silicon MLX）或 BreezyVoice（GPU 伺服器，選用）＋ 內建或匯入的聲音
      → 逐段播放（這台的喇叭／瀏覽器）＋ 字幕（WebUI、懸浮字幕、終端機）
      → 同時存成 recordings/朗讀_*.mp3｜wav
```

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 升級

先切到安裝資料夾再執行（下面是預設位置，裝在別的地方就換成你的安裝資料夾）：

```bash
# macOS / Linux
cd ~/Apps/jt-live-whisper
./install.sh --upgrade

# Windows (PowerShell)
cd C:\jt-live-whisper
.\install.ps1 -Upgrade
```

自動從 GitHub 下載最新版本的程式檔案。Linux 接著會自動檢查並補齊相依套件；macOS 與 Windows 升級後請再執行一次安裝腳本（`./install.sh`／`.\install.ps1`），新版本需要的套件才會裝上。
還沒建過捷徑的電腦，升級結束時會問一次要不要建立桌面與應用程式選單捷徑（選了「都不要」之後不再問）。

- **升級執行一次就好**（v2.28.1 起，新版才加入的檔案由新版的安裝程式接手補齊）。從 v2.28.0 以前升級上來的，第一次升級可能還缺新加入的檔案（例如朗讀模組），之後啟動 `./start.sh`／`.\start.ps1`（點捷徑也是）時會自動補齊
- **升級後把 WebUI 關掉再重新啟動**，開著的 WebUI 還在跑升級前的程式

---

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## >>> [完整使用手冊（SOP.md）](SOP.md) <<<

包含完整安裝教學、macOS / Windows / Linux 音訊設定說明、所有功能模式詳細說明、互動式選單操作、講者辨識設定、摘要功能用法、進階 CLI 參數、FAQ 等。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## >>> [版本記錄（CHANGELOG.md）](CHANGELOG.md) <<<

&nbsp;

&nbsp;

## >>> [實測紀錄（BENCHMARKS.md）](BENCHMARKS.md) <<<

選模型、改預設值所依據的實際測試：台語與華台英混合、台語模型比較、辨識準確度、講者辨識、摘要與校正模型。每項都寫出語料、方法與限制。

---

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 品質與效能說明

- **語音辨識品質**取決於所選用的 ASR 模型大小、音訊品質（背景噪音、麥克風距離、多人交談重疊等）以及語言種類。
- **翻譯品質**取決於所選用的翻譯引擎與模型能力。LLM 翻譯品質最佳但需要 LLM 伺服器（本機或區域網路）；NLLB / Argos 離線翻譯品質較低但無需額外伺服器。
- **講者辨識**準確度受限於音訊品質、講者數量與聲紋相似度，在多人交談或遠場收音情境下結果可能不準確。
- **台語**用台語模型（Breeze-ASR-26）辨識，輸出寫成華語用字；它也能處理夾雜的華語，但英文大多會被翻成中文。實測見 [BENCHMARKS.md](BENCHMARKS.md)。
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
> 本機跑 Qwen3-ASR（實驗，v2.24.0）另需約 3.2 GB 記憶體、2.3 GB 磁碟；Intel Mac 不支援。
> 本機朗讀（文字轉語音，v2.27.0）只在 Apple Silicon、記憶體 16 GB 以上提供：合成時另需約 7～10 GB 記憶體，磁碟約 4 GB（模型 3.2 GB、台灣念法資源 0.6 GB）；Intel Mac 不支援，改用 GPU 伺服器合成。

### Windows

| 配置 | 即時辨識 | 離線處理 7 分鐘音檔 | 說明 |
|------|---------|---------------------|------|
| 純 CPU（無獨顯） | 勉強可用 | ~15-25 分鐘 | 即時模式延遲高，建議搭配 GPU 伺服器 |
| GTX 1660 Super（6 GB） | 可用 | ~1-2 分鐘 | 入門級 GPU，VRAM 餘裕較小 |
| **RTX 4060（8 GB）** | **流暢** | **~30-40 秒** | **性價比最高，推薦** |
| RTX 4060 Ti（16 GB） | 流暢 | ~20-30 秒 | VRAM 充裕，未來擴充空間大 |
| RTX 3060（12 GB） | 流暢 | ~40-50 秒 | 上一代，二手性價比高 |

> **Windows + NVIDIA GPU 是最簡單的高效能方案**：不需要額外硬體或伺服器設定，安裝後直接使用 large-v3-turbo 模型，即時辨識和離線處理都有 CUDA 加速。最低建議 6 GB VRAM 的 NVIDIA 顯示卡。沒有獨顯的 Windows 電腦仍可使用，但速度會慢很多。
> 本機跑 Qwen3-ASR（實驗，v2.24.0）：有 NVIDIA 顯示卡約需 5 GB 顯示記憶體；沒有獨顯時可以選（電腦記憶體需 12 GB 以上），但處理時間可能比錄音還長。
> 朗讀（文字轉語音，v2.27.0）在 Windows／Linux 本機不提供（只有 CPU 太慢），由 GPU 伺服器合成，本機不另外佔空間。

### Linux

| 配置 | 說明 |
|------|------|
| 純 CPU | 即時模式建議 base.en / small 模型，或搭配 GPU 伺服器；離線處理可用但較慢 |
| NVIDIA GPU（6 GB VRAM 以上） | 安裝程式自動裝 CUDA 版 PyTorch，faster-whisper 直接走 CUDA，建議 large-v3-turbo |
| 無桌面伺服器（伺服器模式） | `./install.sh --server`：離線處理 + WebUI 常駐服務，供區域網路其他電腦用瀏覽器上傳錄音。搭配 GPU 伺服器時 2 vCPU／4 GB／30 GB 即可；不搭配時建議 8 核、8 GB 以上 |

> Linux 的即時辨識一律使用 faster-whisper（不編譯 whisper.cpp）。ARM64 主機（如 DGX Spark）的 CTranslate2 預建套件不含 CUDA，安裝程式會自動在本機編譯 CUDA 版（約 20～40 分鐘）。GPU 伺服器同時要跑 LLM 等工作時，建議另備一台 Linux 伺服器安裝 jt-live-whisper，辨識交給 GPU 伺服器。`./install.sh --upgrade` 會在更新後自動補齊相依套件。

### GPU 伺服器（選配，語音辨識加速用）

區域網路內的 GPU 伺服器可為本機提供遠端語音辨識，適合沒有獨顯或需要更快處理速度的情境。

| GPU | VRAM | 離線處理 7 分鐘音檔 | 說明 |
|-----|------|---------------------|------|
| RTX 4060 以上 | 8 GB+ | ~20-30 秒 | 消費級入門 |
| RTX 4090 | 24 GB | ~10-15 秒 | 消費級旗艦 |
| NVIDIA DGX Spark | 128 GB | ~10 秒 | 同時跑 Ollama LLM + Whisper 辨識，一機搞定 |

> 要啟用 Qwen3-ASR（實驗）時需再多約 7~9 GB 顯示記憶體常駐、約 14 GB 磁碟，建議 16 GB 以上的顯示卡。
> 要啟用文字轉語音（v2.27.0）時需再多約 9.5 GB 記憶體（第一次用到才啟動、閒置 30 分鐘自動釋放）、約 11 GB 磁碟（安裝時要 20 GB 可用空間）；顯示卡驅動要支援 CUDA 12.8 以上（DGX Spark 要 CUDA 13）。

### LLM 伺服器（選配，翻譯/摘要用）

| 用途 | 建議模型大小 | 記憶體/VRAM 需求 | 說明 |
|------|-------------|-----------------|------|
| 翻譯 | 14B 以上 | ~12 GB（gemma4:26b 約 17 GB） | 如 gemma4:26b（預設）或 qwen2.5:14b，品質與速度兼顧 |
| 摘要 / 逐字稿校正 | 27B 以上 | ~18 GB | 預設 qwen3.8:27b。實測它的校正品質優於 120B 級模型，**不需要**更大的模型 |

> LLM 伺服器可安裝在本機或區域網路內的任何主機。推薦使用 [NVIDIA DGX Spark](https://www.nvidia.com/zh-tw/products/workstations/dgx-spark/)（128 GB 統一記憶體），可同時執行翻譯模型與摘要模型。沒有 LLM 伺服器時，程式可切換為 NLLB/Argos 離線翻譯引擎。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 免責聲明

本工具按「現狀」（AS IS）提供，不附帶任何明示或暗示的保證。語音辨識、翻譯、講者辨識及摘要等功能的輸出結果僅供參考，不保證其準確性與完整性。使用者應自行驗證輸出結果，不應將未經人工審核的輸出直接用於法律文件、醫療紀錄、財務報告或其他需要高度準確性的場合。使用者應確保擁有合法錄音權利並遵守當地隱私法規（建議做法見[資料保護與合規](COMPLIANCE.md)）。作者及貢獻者不對因使用本工具而產生的任何損害承擔責任。

&nbsp;

&nbsp;

&nbsp;

&nbsp;

## 專案網站

🌐 **[jasoncheng7115.github.io/jt-live-whisper](https://jasoncheng7115.github.io/jt-live-whisper/)**：功能介紹、畫面導覽、安裝與使用說明。

&nbsp;

## License

本專案採用 [Apache License 2.0](LICENSE) 授權。

講者辨識預設使用的 [NVIDIA Nemotron 3 Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization) 模型採 OpenMDW-1.1 授權，
安裝時從 HuggingFace 下載（不需帳號），不隨本專案散布。

文字轉語音使用的 [OpenBMB VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) 模型、選用的 [MediaTek BreezyVoice](https://github.com/mtkresearch/BreezyVoice)（程式與模型）與 [g2pW](https://github.com/GitYCC/g2pW) 採 Apache-2.0 授權，
台灣念法使用中華民國教育部《重編國語辭典修訂本》（CC BY-ND 3.0 TW，經 [g0v 萌典](https://github.com/g0v/moedict-data) 取得），
都在安裝時下載，不隨本專案散布。

Copyright 2026 Jason Cheng (Jason Tools)
