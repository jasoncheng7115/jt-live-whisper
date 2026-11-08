#!/bin/bash
# jt-live-whisper - Linux 安裝腳本
# 由 install.sh 在 Linux 上自動轉交執行，也可直接執行：
#   ./install-linux.sh              桌面版（預設）：即時字幕、WebUI、懸浮字幕
#   ./install-linux.sh --server     伺服器版：無桌面環境，WebUI 以 systemd 服務常駐
#   ./install-linux.sh --upgrade    從 GitHub 升級程式，並檢查新版的相依套件
#   ./install-linux.sh --doctor     檢查執行環境（音訊、套件、GPU、伺服器連線）
#   ./install-linux.sh --uninstall  移除虛擬環境、服務與桌面捷徑（保留模型與設定）
# 支援 Debian / Ubuntu（apt）；其他發行版會列出需要的套件請自行安裝。
# Author: Jason Cheng (Jason Tools)

set -e

# 沿用 install.sh 中與平台無關的函式（spinner、模型下載、GPU 伺服器設定、升級）
JTLW_INSTALL_LIB=1
# shellcheck source=install.sh
source "$(cd "$(dirname "$0")" && pwd)/install.sh"
set +e

LINUX_MODE="desktop"
LINUX_ACTION="install"
for _arg in "$@"; do
    case "$_arg" in
        --server)    LINUX_MODE="server" ;;
        --desktop)   LINUX_MODE="desktop" ;;
        --upgrade)   LINUX_ACTION="upgrade" ;;
        --doctor)    LINUX_ACTION="doctor" ;;
        --uninstall) LINUX_ACTION="uninstall" ;;
        -h|--help)   LINUX_ACTION="help" ;;
        *) echo -e "${C_ERR}[錯誤] 不認得的參數: ${_arg}（可用 --help 查看）${NC}"; exit 1 ;;
    esac
done

SERVICE_NAME="jt-live-whisper-webui"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# REST API（jtlw_api/，v2.25.1 起公開）的背景服務。安裝程式**不自動建立**（手冊附範本），
# 但要認得照範本建的那一份：升級後提示重啟、解除安裝時一併移除（否則 venv 刪掉後它會每 5 秒重啟失敗一次）、
# 相依套件照伺服器模式補裝。只認「名稱是 jtlw-api 而且工作目錄就是這個資料夾」的，別的程式的服務不碰
API_SERVICE_NAME="jtlw-api"
API_SERVICE_FILE="/etc/systemd/system/${API_SERVICE_NAME}.service"
_api_unit_is_ours() {
    [ -f "$API_SERVICE_FILE" ] && grep -qxF -e "WorkingDirectory=${SCRIPT_DIR}" -e "WorkingDirectory=${SCRIPT_DIR}/" "$API_SERVICE_FILE"
}
DESKTOP_FILE="$HOME/.local/share/applications/jt-live-whisper.desktop"

# ARM64 + NVIDIA 本機編譯的 CTranslate2 函式庫位置（不裝進 /usr/local，避免覆蓋同一台主機上
# 其他程式——例如 GPU 辨識服務——正在使用的 libctranslate2）。安裝檢查與 start.sh 都要能載入它，
# 否則 import ctranslate2 失敗會被當成沒裝而改裝 PyPI 的 CPU 版
CT2_LOCAL_PREFIX="$SCRIPT_DIR/.ct2-local"
export LD_LIBRARY_PATH="$CT2_LOCAL_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# 以 root 執行時不需要 sudo；一般使用者需要 sudo 才能安裝系統套件
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    # 無人值守安裝可設定 SUDO_ASKPASS，由該程式提供密碼
    if [ -n "${SUDO_ASKPASS:-}" ]; then SUDO="sudo -A"; else SUDO="sudo"; fi
else
    SUDO=""
fi

_has_nvidia() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v pacman >/dev/null 2>&1; then echo pacman
    else echo unknown
    fi
}

# ─── 系統檢查 ────────────────────────────────────
check_linux_system() {
    section "系統環境"
    local distro="未知發行版"
    if [ -f /etc/os-release ]; then
        distro=$(. /etc/os-release && echo "${PRETTY_NAME:-$NAME}")
    fi
    check_ok "${distro}（$(uname -m)）"
    if [ "$LINUX_MODE" = "server" ]; then
        check_ok "安裝模式：伺服器版（無桌面環境）"
    else
        check_ok "安裝模式：桌面版"
    fi
    if _has_nvidia; then
        check_ok "NVIDIA GPU：$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    else
        echo -e "  ${C_DIM}未偵測到 NVIDIA GPU，本機辨識使用 CPU（建議搭配 GPU 伺服器）${NC}"
    fi
    local mem_gb
    mem_gb=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    if [ -n "$mem_gb" ] && [ "$mem_gb" -lt 8 ]; then
        check_notice "記憶體 ${mem_gb} GB，本機辨識建議使用 small 以下的模型"
    fi
}

# ─── 系統套件 ────────────────────────────────────
check_linux_packages() {
    section "系統套件"
    local pm
    pm=$(_pkg_manager)

    # 指令 → 套件（apt 名稱）
    local need=()
    command -v ffmpeg  >/dev/null 2>&1 || need+=("ffmpeg")
    command -v curl    >/dev/null 2>&1 || need+=("curl")
    command -v unzip   >/dev/null 2>&1 || need+=("unzip")
    command -v ssh     >/dev/null 2>&1 || need+=("openssh-client")
    "$PYTHON_CMD" -c "import ensurepip, venv" >/dev/null 2>&1 || need+=("python3-venv")
    if [ "$pm" = "apt" ]; then
        dpkg -s libportaudio2 >/dev/null 2>&1 || need+=("libportaudio2")
        # webrtcvad（resemblyzer 的相依套件）沒有預建 wheel，需要 C 編譯器與 Python 標頭檔
        command -v gcc >/dev/null 2>&1 || need+=("build-essential")
        dpkg -s python3-dev >/dev/null 2>&1 || need+=("python3-dev")
        if [ "$LINUX_MODE" = "desktop" ]; then
            command -v parec >/dev/null 2>&1 || need+=("pulseaudio-utils")
            dpkg -s libxcb-cursor0 >/dev/null 2>&1 || need+=("libxcb-cursor0")
            dpkg -s fonts-noto-cjk >/dev/null 2>&1 || need+=("fonts-noto-cjk")
            command -v xdg-open >/dev/null 2>&1 || need+=("xdg-utils")
        fi
    fi

    if [ ${#need[@]} -eq 0 ]; then
        check_ok "系統套件齊全"
        return 0
    fi

    if [ "$pm" != "apt" ]; then
        check_notice "請自行安裝以下套件（名稱依發行版可能不同）：${need[*]}"
        echo -e "  ${C_DIM}另需：PortAudio 函式庫、pulseaudio-utils（或 pipewire-pulse）、libxcb-cursor、中文字型${NC}"
        return 0
    fi
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        check_fail "缺少套件且無法使用 sudo，請以系統管理員執行：apt-get install -y ${need[*]}"
        return 1
    fi

    check_install "安裝 ${need[*]}"
    if ! run_spinner "更新套件清單..." $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq; then
        echo ""
        check_notice "套件清單更新失敗，嘗試直接安裝"
    fi
    echo ""
    if run_spinner "安裝中..." $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"; then
        echo ""
        check_ok "系統套件安裝完成"
    else
        echo ""
        check_fail "系統套件安裝失敗："
        echo -e "  ${C_DIM}$(tail -3 "$SPINNER_OUTPUT")${NC}"
        return 1
    fi
}

# ─── 音訊環境 ────────────────────────────────────
check_linux_audio() {
    [ "$LINUX_MODE" = "desktop" ] || return 0
    section "系統音訊擷取（PipeWire / PulseAudio）"
    if command -v pactl >/dev/null 2>&1 && pactl info >/dev/null 2>&1; then
        local server sink
        server=$(pactl info 2>/dev/null | sed -n 's/^Server Name: //p')
        sink=$(pactl get-default-sink 2>/dev/null)
        check_ok "音訊伺服器：${server:-已連線}"
        if [ -n "$sink" ] && pactl list short sources 2>/dev/null | grep -q "	${sink}.monitor	"; then
            check_ok "系統音訊來源：${sink}.monitor（免安裝虛擬音效卡）"
        else
            check_notice "找不到預設喇叭的 monitor 來源，即時模式可能無法擷取系統音訊"
        fi
    else
        check_notice "目前連不到音訊伺服器（SSH 連線時屬正常），請在桌面工作階段內執行即時模式"
    fi
}

# ─── Python 虛擬環境（Linux）──────────────────────
check_linux_venv() {
    section "Python 虛擬環境"

    # 路徑搬遷、從別的作業系統複製、或作業系統升級換了 Python 版本（venv_problem 在 install.sh）
    if [ -d "$VENV_DIR" ] && ! venv_problem "$VENV_DIR"; then
        echo -e "  ${C_WARN}[偵測]${NC} ${VENV_PROBLEM}，需重建"
        rm -rf "$VENV_DIR"
    fi
    if [ ! -d "$VENV_DIR" ]; then
        check_install "正在建立 Python 虛擬環境..."
        if ! "$PYTHON_CMD" -m venv "$VENV_DIR"; then
            check_fail "虛擬環境建立失敗（Debian / Ubuntu 需要 python3-venv）"
            return 1
        fi
        check_ok "虛擬環境建立完成"
    else
        check_ok "虛擬環境正常"
    fi

    source "$VENV_DIR/bin/activate"
    # pip／certifi 太舊就升級（emSign 中間憑證外洩，見 install.sh 的 _PIP_SECURE_PY）
    if pip_secure_upgrade "$VENV_DIR/bin/python3"; then
        check_ok "pip／certifi 憑證清單已是安全版本"
    else
        check_fail "pip／certifi 升級失敗（${PIP_SECURE_STATE}；需要 pip 26.2.2、certifi 2026.10.19 以上）"
    fi
    # resemblyzer 依賴的 webrtcvad 需要 pkg_resources（setuptools < 81）
    pip install --quiet --disable-pip-version-check "setuptools<81" wheel >/dev/null 2>&1

    # torch 由 resemblyzer / noisereduce 間接需要：沒有 NVIDIA GPU 時先裝 CPU 版，
    # 否則 pip 預設會下載數 GB 的 CUDA 版
    if ! python3 -c "import torch" >/dev/null 2>&1; then
        if _has_nvidia && [ "$(uname -m)" = "aarch64" ]; then
            # ARM64（如 DGX Spark）：PyPI 的 torch 不含 CUDA，改用 PyTorch 官方的 CUDA 12.8 版
            run_spinner "PyTorch（CUDA 12.8 ARM64 版，檔案較大）..." pip install --disable-pip-version-check torch \
                --index-url https://download.pytorch.org/whl/cu128
        elif _has_nvidia; then
            run_spinner "PyTorch（CUDA 版，檔案較大）..." pip install --disable-pip-version-check torch
        else
            run_spinner "PyTorch（CPU 版）..." pip install --disable-pip-version-check torch \
                --index-url https://download.pytorch.org/whl/cpu
        fi
        echo ""
    fi

    local pkgs=(
        "ctranslate2|ctranslate2|ctranslate2（語音辨識加速引擎）"
        "sentencepiece|sentencepiece|sentencepiece（分詞工具）"
        "opencc|opencc-python-reimplemented|OpenCC（簡繁轉換）"
        "sounddevice|sounddevice|sounddevice（音訊擷取）"
        "numpy|numpy|numpy（數值計算）"
        "faster_whisper|faster-whisper|faster-whisper（語音辨識）"
        "resemblyzer|resemblyzer|resemblyzer（講者辨識 - 聲紋提取）"
        "spectralcluster|spectralcluster|spectralcluster（講者辨識 - 分群）"
        "noisereduce|noisereduce|noisereduce（背景降噪）"
        "fastapi|fastapi|fastapi（WebUI 伺服器）"
        "uvicorn|uvicorn|uvicorn（WebUI ASGI 伺服器）"
        "websockets|websockets|websockets（WebUI 即時通訊）"
        "python_multipart,multipart|python-multipart|python-multipart（WebUI 檔案上傳）"
        "huggingface_hub|huggingface_hub|huggingface_hub（模型下載）"
    )
    if [ "$LINUX_MODE" = "desktop" ]; then
        pkgs+=("PyQt6|PyQt6|PyQt6（懸浮字幕視窗）")
    fi
    if [ "$LINUX_MODE" = "server" ] || _api_unit_is_ours; then
        pkgs+=("jsonschema|jsonschema|jsonschema（REST API 送件格式檢查）")
    fi

    local item mod pkg label
    for item in "${pkgs[@]}"; do
        mod="${item%%|*}"; item="${item#*|}"
        pkg="${item%%|*}"; label="${item#*|}"
        if _py_import_ok python3 "$mod"; then
            check_ok "${label}（已安裝）"
            continue
        fi
        if [ "$mod" = "resemblyzer" ] && ! python3 -c "import numba" >/dev/null 2>&1; then
            # resemblyzer → librosa → numba → llvmlite，先取預建 wheel 避免從原始碼編譯
            pip install --quiet --disable-pip-version-check --only-binary=:all: llvmlite numba >/dev/null 2>&1 || true
        fi
        if run_spinner "$label ..." pip install --disable-pip-version-check "$pkg" \
                && _py_import_ok python3 "$mod"; then
            echo ""
            check_ok "$label"
        else
            echo ""
            check_fail "$label 安裝失敗："
            echo -e "  ${C_DIM}$(grep -i 'error' "$SPINNER_OUTPUT" 2>/dev/null | tail -3)${NC}"
        fi
    done

    # Qwen3-ASR 本機辨識（實驗，v2.24.0）：transformers 5.17 起內建。看能力不看版本號——
    # 已經裝了舊版 transformers 的機器 import 會成功，但沒有 qwen3_asr。soynlp 是韓文對齊用的。
    # 模型第一次選用時才下載（約 3.4 GB），這裡只裝套件；失敗不影響其他功能
    if python3 -c "$_QWEN_TF_CHECK" >/dev/null 2>&1; then
        check_ok "transformers（Qwen3-ASR 本機辨識，實驗）（已安裝）"
    elif run_spinner "transformers（Qwen3-ASR 本機辨識，實驗）..." \
            pip install --disable-pip-version-check "transformers>=5.18" soynlp \
            && python3 -c "$_QWEN_TF_CHECK" >/dev/null 2>&1; then
        echo ""
        check_ok "transformers（Qwen3-ASR 本機辨識，實驗；模型第一次選用時下載，約 3.4 GB）"
    else
        echo ""
        check_notice "transformers 安裝失敗：Qwen3-ASR 只能透過 GPU 伺服器使用，其他功能不受影響"
    fi

    if _has_nvidia; then
        if python3 -c "import ctranslate2,sys; sys.exit(0 if ctranslate2.get_supported_compute_types('cuda') else 1)" >/dev/null 2>&1; then
            check_ok "CTranslate2 可使用 CUDA（本機辨識 GPU 加速）"
        elif [ "$(uname -m)" = "aarch64" ]; then
            # ARM64 的 CTranslate2 預建套件不含 CUDA，沿用 GPU 伺服器部署的原始碼編譯流程
            deactivate
            if build_ctranslate2_local; then
                check_ok "CTranslate2 CUDA（原始碼編譯）"
            else
                check_notice "CTranslate2 CUDA 編譯失敗，本機辨識將使用 CPU（詳見上方訊息）"
            fi
            source "$VENV_DIR/bin/activate"
        else
            check_notice "CTranslate2 無法使用 CUDA，本機辨識將使用 CPU"
        fi
    fi
    deactivate
}

# ─── ARM64 + NVIDIA：本機編譯 CTranslate2 CUDA 版 ───────────
# install.sh 的 _build_ctranslate2_from_source 原本透過 ssh 在 GPU 伺服器上執行；
# 這裡用同名的 ssh 函式把指令改在本機執行，只有 apt 需要 sudo。
# 函式庫裝在 ${CT2_LOCAL_PREFIX}（見檔案開頭），不裝進 /usr/local。
build_ctranslate2_local() {
    (
        ssh() {
            local cmd="${*: -1}"
            case "$cmd" in
                *"apt "*) $SUDO bash -c "$cmd" ;;
                *) bash -c "$cmd" ;;
            esac
        }
        _build_ctranslate2_from_source "" "local" "localhost" "$VENV_DIR" \
            "$SCRIPT_DIR/.ct2-wheels" "$CT2_LOCAL_PREFIX"
    )
}

# ─── GPU 伺服器設定 ────────────────────────────────
# install.sh 的 setup_remote_whisper 會用 SSH 檢查、修復伺服器。本機沒有可免密碼登入的金鑰時，
# ssh 會在轉圈動畫底下等密碼，自動部署（systemd、SSH 遠端執行）就會一直卡住。
# 已有設定時先以 BatchMode 試連：登不進去就只用 HTTP 確認辨識服務，不做需要 SSH 的檢查。
setup_remote_whisper_linux() {
    local rw
    rw=$("$VENV_DIR/bin/python3" - "$SCRIPT_DIR/config.json" <<'PY' 2>/dev/null
import json, os, sys
p = sys.argv[1]
rw = (json.load(open(p, encoding="utf-8")).get("remote_whisper") or {}) if os.path.isfile(p) else {}
if rw.get("host"):
    print(rw["host"], rw.get("ssh_port", 22), rw.get("ssh_user", "root"),
          rw.get("whisper_port", 8978), rw.get("ssh_key") or "-")
PY
)
    if [ -z "$rw" ]; then
        setup_remote_whisper
        return
    fi
    local host port user wport key opts
    read -r host port user wport key <<< "$rw"
    opts="-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -p $port"
    [ "$key" != "-" ] && [ -f "$key" ] && opts="$opts -i $key"
    # shellcheck disable=SC2086
    if ssh $opts "$user@$host" true >/dev/null 2>&1; then
        setup_remote_whisper
        return
    fi
    section "GPU 伺服器 語音辨識伺服器（非必要，若未裝則用本機進行語音辨識）"
    echo -e "  ${C_WHITE}已有伺服器設定: ${user}@${host}:${port}${NC}"
    check_notice "無法以 SSH 金鑰登入 ${user}@${host}，略過伺服器環境檢查與修復"
    echo -e "  ${C_DIM}  辨識服務走 HTTP，不需要 SSH；要由本機部署或修復伺服器時，先設定金鑰再重新執行安裝：${NC}"
    echo -e "  ${C_DIM}  ssh-copy-id -p ${port} ${user}@${host}${NC}"
    if curl -fsS -m 5 "http://${host}:${wport}/health" >/dev/null 2>&1; then
        check_ok "辨識服務正常（http://${host}:${wport}）"
    else
        check_notice "辨識服務沒有回應（http://${host}:${wport}），辨識會改用本機"
    fi
}

# ─── 從 Mac 搬過來的設定檔修正 ────────────────────
fix_migrated_config() {
    [ -f "$SCRIPT_DIR/config.json" ] || return 0
    "$VENV_DIR/bin/python3" - "$SCRIPT_DIR/config.json" <<'PY' 2>/dev/null || true
import json, os, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding="utf-8"))
rw = cfg.get("remote_whisper") or {}
key = rw.get("ssh_key", "")
if key and not os.path.exists(key) and (key.startswith("/Users/") or key.startswith("C:")):
    new = os.path.expanduser("~/.ssh/" + os.path.basename(key.replace("\\", "/")))
    rw["ssh_key"] = new
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print(f"  [修正] config.json 的 SSH Key 路徑改為 {new}")
PY
}

# ─── 桌面捷徑 ────────────────────────────────────
install_desktop_entry() {
    [ "$LINUX_MODE" = "desktop" ] || return 0
    section "應用程式選單捷徑"
    mkdir -p "$(dirname "$DESKTOP_FILE")"
    # 內容與桌面捷徑共用（install.sh 的 _write_linux_desktop_entry）
    _write_linux_desktop_entry "$DESKTOP_FILE" || { check_fail "無法寫入 ${DESKTOP_FILE}"; return 0; }
    chmod +x "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1
    check_ok "已建立 ${DESKTOP_FILE}"
}

# ─── systemd 服務（伺服器版）──────────────────────
install_systemd_service() {
    [ "$LINUX_MODE" = "server" ] || return 0
    section "WebUI 常駐服務（systemd）"
    if ! command -v systemctl >/dev/null 2>&1; then
        check_notice "此系統沒有 systemd，請自行以 ./start.sh --webui 啟動"
        return 0
    fi
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        check_notice "需要系統管理員權限才能安裝服務，已略過"
        return 0
    fi
    # 服務帳號：v2.22.3 前直接用 id -un，用 root 或 sudo 安裝時 WebUI 就以 root 執行——
    # 任何一個 WebUI 漏洞都會變成整台機器的最高權限（192.168.1.223 實際就是這樣）。
    # 改成：sudo 的原始使用者 → 安裝目錄的擁有者；兩者都是 root 才用 root，並大聲警告。
    local run_user
    run_user="${SUDO_USER:-}"
    if [ -z "$run_user" ] || [ "$run_user" = "root" ]; then
        run_user="$(stat -c %U "$SCRIPT_DIR" 2>/dev/null || id -un)"
    fi
    if [ "$run_user" = "root" ]; then
        check_notice "WebUI 服務將以 root 身分執行：建議改用一般帳號安裝（把程式放在該帳號的目錄下）"
    fi
    local unit
    unit=$(cat <<EOF
[Unit]
Description=jt-live-whisper WebUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/bin/bash ${SCRIPT_DIR}/start.sh --webui
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
# start.sh 收到 SIGINT 後以 130 結束，屬正常停止
SuccessExitStatus=130 SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
)
    local start_verb="start"
    if [ -f "$SERVICE_FILE" ] && [ "$(cat "$SERVICE_FILE")" = "$unit" ]; then
        check_ok "服務設定未變更（${SERVICE_NAME}）"
        if [ -n "${JTLW_RESTART_SERVICE:-}" ]; then
            start_verb="restart"   # 升級後程式已更新，要重啟才會載入新版
        elif systemctl is-active --quiet "$SERVICE_NAME"; then
            check_ok "服務執行中"
            return 0
        fi
    else
        echo "$unit" | $SUDO tee "$SERVICE_FILE" >/dev/null
        $SUDO systemctl daemon-reload
        check_ok "已寫入 ${SERVICE_FILE}"
        start_verb="restart"   # 設定有變更時，已在執行的服務要重啟才會套用
    fi
    if $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 \
            && $SUDO systemctl "$start_verb" "$SERVICE_NAME" >/dev/null 2>&1; then
        check_ok "服務已啟用並啟動（開機自動執行）"
        echo -e "  ${C_DIM}WebUI：http://$(hostname -I 2>/dev/null | awk '{print $1}'):19781${NC}"
        seed_admin_password
    else
        check_fail "服務啟動失敗，請執行 journalctl -u ${SERVICE_NAME} 查看原因"
    fi
}


# ─── 伺服器版：第一次安裝時產生密碼（管理＋唯讀）───────────────
# **沒有這一段，--server 裝完是不能遠端操作的**：/api/start 需要 admin 密碼，
# 而設定密碼的那一頁本身就只有本機能開 —— 雞生蛋。無頭伺服器正是裝 --server
# 的理由，總不能叫人先接螢幕。
# v2.22.3 起**同時產生唯讀密碼**：沒有唯讀密碼時，同網段任何人都能看畫面、列出錄音、讀逐字稿與摘要。
# 只在「兩個都沒設過」＝全新安裝時產生；已有 admin 密碼的既有部署一律不動（升級不改變既有行為），
# 只提醒。
seed_admin_password() {
    local cfg="${SCRIPT_DIR}/config.json"
    command -v python3 >/dev/null 2>&1 || return 0
    local out
    out=$(SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PYEOF'
import hashlib, json, os, pathlib, secrets, sys
p = pathlib.Path(os.environ["SCRIPT_DIR"]) / "config.json"
try:
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
except Exception:
    sys.exit(0)                       # 設定檔壞掉時不要亂動它
wp = cfg.get("webui_passwords") or {}
has_admin = bool(wp.get("admin_sha256") or wp.get("admin"))
has_read = bool(wp.get("read_sha256") or wp.get("read"))
if has_admin:                         # 既有部署：不覆蓋、不新增，只回報狀態
    print("EXISTING" + ("" if has_read else " NO_READ"))
    sys.exit(0)
out = []
for role in ("admin", "read"):
    if role == "read" and has_read:
        continue
    pw = secrets.token_urlsafe(12)
    # 格式與 webui.py 的 _pw_hash 相同（加鹽 PBKDF2，v2.31.8；欄位名稱沿用舊版）
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200000).hex()
    wp[f"{role}_sha256"] = f"pbkdf2_sha256$200000${salt}${dk}"
    out.append(f"{role.upper()} {pw}")
cfg["webui_passwords"] = wp
cfg.setdefault("webui", {}).setdefault("listen", "0.0.0.0")   # 伺服器版就是給別台用（v2.34.1 起新裝的 WebUI 預設只限本機）
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n".join(out))
PYEOF
)
    local admin_pw read_pw
    admin_pw=$(echo "$out" | awk '$1=="ADMIN"{print $2}')
    read_pw=$(echo "$out" | awk '$1=="READ"{print $2}')
    if [ -n "$admin_pw" ]; then
        echo
        echo -e "  ${C_WARN}${BOLD}WebUI 密碼（只顯示這一次，請立刻記下來）${NC}"
        echo -e "      管理密碼（上傳、開始／停止作業）：${C_OK}${BOLD}${admin_pw}${NC}"
        [ -n "$read_pw" ] && echo -e "      唯讀密碼（看畫面、讀逐字稿）  ：${C_OK}${BOLD}${read_pw}${NC}"
        echo -e "  ${C_DIM}存的是加鹽的雜湊，設定檔裡沒有明文，遺失只能重設${NC}"
        echo -e "  ${C_DIM}要更換：在伺服器本機開 WebUI → 安全設定（那一頁只有本機能開）${NC}"
        echo -e "  ${C_DIM}建議同時設定 webui.allowed_ips 限制來源網段${NC}"
    elif echo "$out" | grep -q "NO_READ"; then
        echo -e "  ${C_DIM}遠端管理密碼已設定過，未變更${NC}"
        echo -e "  ${C_WARN}注意：尚未設定唯讀密碼——同網段任何人都能看畫面、讀逐字稿與摘要。${NC}"
        echo -e "  ${C_WARN}建議在伺服器本機開 WebUI → 安全設定，設一組唯讀密碼（或以 webui.allowed_ips 限制來源）${NC}"
    else
        echo -e "  ${C_DIM}WebUI 密碼已設定過，未變更${NC}"
    fi
}

# ─── 環境診斷 ────────────────────────────────────
linux_doctor() {
    local problems=0
    _dr_fail() { check_fail "$1"; problems=$((problems + 1)); }

    section "程式與 Python 環境"
    local app_ver
    app_ver=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
    check_ok "jt-live-whisper v${app_ver:-未知}"
    if [ -x "$VENV_DIR/bin/python3" ] && ! venv_problem "$VENV_DIR"; then
        # 作業系統升級換了 Python 版本：下面每個套件都會 import 失敗，先講真正的原因
        _dr_fail "${VENV_PROBLEM}：執行 ./install.sh --upgrade 會重建 venv、重新安裝套件"
    elif [ -x "$VENV_DIR/bin/python3" ]; then
        check_ok "虛擬環境 $("$VENV_DIR/bin/python3" --version 2>&1)"
        local mod
        for mod in faster_whisper ctranslate2 sounddevice opencc fastapi uvicorn resemblyzer; do
            if "$VENV_DIR/bin/python3" -c "import $mod" >/dev/null 2>&1; then
                check_ok "$mod"
            else
                _dr_fail "$mod 無法載入（請執行 ./install.sh）"
            fi
        done
        if "$VENV_DIR/bin/python3" -c "import sounddevice" >/dev/null 2>&1; then :; else
            echo -e "  ${C_DIM}sounddevice 需要 PortAudio：sudo apt install libportaudio2${NC}"
        fi
    else
        _dr_fail "找不到虛擬環境，請執行 ./install.sh"
    fi
    command -v ffmpeg >/dev/null 2>&1 && check_ok "ffmpeg" || _dr_fail "找不到 ffmpeg（sudo apt install ffmpeg）"

    section "系統音訊"
    if command -v parec >/dev/null 2>&1; then
        check_ok "擷取工具：parec"
    elif command -v pw-record >/dev/null 2>&1; then
        check_ok "擷取工具：pw-record"
    else
        _dr_fail "沒有 parec / pw-record（sudo apt install pulseaudio-utils）"
    fi
    if command -v pactl >/dev/null 2>&1 && pactl info >/dev/null 2>&1; then
        check_ok "音訊伺服器：$(pactl info | sed -n 's/^Server Name: //p')"
        local sink
        sink=$(pactl get-default-sink 2>/dev/null)
        if [ -n "$sink" ]; then
            check_ok "預設喇叭：${sink}"
        else
            _dr_fail "沒有預設喇叭（輸出裝置）"
        fi
        if [ -x "$VENV_DIR/bin/python3" ]; then
            "$VENV_DIR/bin/python3" "$SCRIPT_DIR/translate_meeting.py" --list-devices 2>/dev/null \
                | sed 's/\x1b\[[0-9;]*m//g' | grep -E '^\s+\[' | sed 's/^/  /'
        fi
    else
        check_notice "連不到 PipeWire / PulseAudio（SSH 或伺服器上屬正常；即時模式需在桌面工作階段執行）"
    fi

    section "GPU"
    if _has_nvidia; then
        check_ok "$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null | head -1)"
        if "$VENV_DIR/bin/python3" -c "import ctranslate2,sys; sys.exit(0 if ctranslate2.get_supported_compute_types('cuda') else 1)" >/dev/null 2>&1; then
            check_ok "CTranslate2 CUDA 可用"
        else
            check_notice "CTranslate2 無法使用 CUDA，本機辨識會用 CPU"
        fi
    else
        echo -e "  ${C_DIM}未偵測到 NVIDIA GPU（本機辨識使用 CPU）${NC}"
    fi

    section "伺服器連線"
    local hosts
    hosts=$("$VENV_DIR/bin/python3" - "$SCRIPT_DIR/config.json" <<'PY' 2>/dev/null
import json, sys
try:
    c = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    c = {}
rw = c.get("remote_whisper") or {}
llm = c.get("llm_host") or c.get("ollama_host") or ""
print(f"{rw.get('host','')}:{rw.get('whisper_port',8978)}" if rw.get("host") else "")
print(f"{llm}:{c.get('llm_port', c.get('ollama_port', 11434))}" if llm else "")
PY
)
    local rw_addr llm_addr
    rw_addr=$(echo "$hosts" | sed -n 1p)
    llm_addr=$(echo "$hosts" | sed -n 2p)
    if [ -n "$rw_addr" ]; then
        if curl -s --max-time 5 "http://${rw_addr}/health" | grep -q '"ok"'; then
            check_ok "GPU 伺服器 ${rw_addr} 正常"
        else
            _dr_fail "GPU 伺服器 ${rw_addr} 沒有回應（服務可能未啟動）"
        fi
    else
        echo -e "  ${C_DIM}未設定 GPU 伺服器${NC}"
    fi
    if [ -n "$llm_addr" ]; then
        if curl -s --max-time 5 "http://${llm_addr}/api/tags" | grep -q '"models"' \
                || curl -s --max-time 5 "http://${llm_addr}/v1/models" | grep -q '"data"'; then
            check_ok "LLM 伺服器 ${llm_addr} 正常"
        else
            _dr_fail "LLM 伺服器 ${llm_addr} 沒有回應"
        fi
    else
        echo -e "  ${C_DIM}未設定 LLM 伺服器${NC}"
    fi

    if command -v systemctl >/dev/null 2>&1 && _api_unit_is_ours; then
        section "REST API 服務"
        local amod
        for amod in "jsonschema|jsonschema" "python_multipart,multipart|python-multipart"; do
            if _py_import_ok "$VENV_DIR/bin/python3" "${amod%%|*}"; then
                check_ok "${amod#*|}"
            else
                _dr_fail "${amod#*|} 無法載入（請執行 ./install.sh --server）"
            fi
        done
        if systemctl is-active --quiet "$API_SERVICE_NAME"; then
            check_ok "${API_SERVICE_NAME} 執行中"
        else
            _dr_fail "${API_SERVICE_NAME} 未執行（journalctl -u ${API_SERVICE_NAME}）"
        fi
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]; then
        section "WebUI 服務"
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            check_ok "${SERVICE_NAME} 執行中"
        else
            _dr_fail "${SERVICE_NAME} 未執行（journalctl -u ${SERVICE_NAME}）"
        fi
    fi

    echo ""
    if [ "$problems" -eq 0 ]; then
        echo -e "${C_OK}${BOLD}  檢查完成，沒有發現問題${NC}"
    else
        echo -e "${C_WARN}${BOLD}  檢查完成，發現 ${problems} 個問題（詳見上方）${NC}"
    fi
    echo ""
    [ "$problems" -eq 0 ]
}

# ─── 解除安裝 ────────────────────────────────────
linux_uninstall() {
    section "解除安裝"
    echo -e "  ${C_WHITE}將移除：虛擬環境（venv/）、WebUI 服務、REST API 服務（有的話）、應用程式選單捷徑${NC}"
    echo -e "  ${C_DIM}保留：程式檔、config.json、logs/、recordings/、api_data/（API 的作業紀錄與憑證）、下載的模型（~/.cache/huggingface、~/.local/share）${NC}"
    read -p "  確定要解除安裝？(y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || { echo "  已取消"; return 0; }
    if command -v systemctl >/dev/null 2>&1 && _api_unit_is_ours; then
        # 與下面的 WebUI 服務同一個原則：移除失敗就不要接著刪 venv
        $SUDO systemctl disable --now "$API_SERVICE_NAME" >/dev/null 2>&1
        $SUDO rm -f "$API_SERVICE_FILE" >/dev/null 2>&1
        if [ -f "$API_SERVICE_FILE" ]; then
            check_fail "無法移除 ${API_SERVICE_NAME} 服務（需要 sudo 權限），未做任何變更"
            echo -e "  ${C_DIM}請在終端機手動執行：${NC}"
            echo -e "  ${C_DIM}  sudo systemctl disable --now ${API_SERVICE_NAME} && sudo rm -f ${API_SERVICE_FILE} && sudo systemctl daemon-reload${NC}"
            echo -e "  ${C_DIM}完成後再執行一次 ./install.sh --uninstall${NC}"
            return 1
        fi
        $SUDO systemctl daemon-reload >/dev/null 2>&1
        $SUDO systemctl reset-failed "$API_SERVICE_NAME" >/dev/null 2>&1
        check_ok "已移除 ${API_SERVICE_NAME} 服務"
    fi
    if command -v systemctl >/dev/null 2>&1 && [ -f "$SERVICE_FILE" ]; then
        # sudo 失敗（例如沒有終端機可輸入密碼）時不可謊報成功，也不要接著刪 venv，
        # 否則會留下一個指向已刪除環境、仍在執行的服務
        $SUDO systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1
        $SUDO rm -f "$SERVICE_FILE" >/dev/null 2>&1
        if [ -f "$SERVICE_FILE" ]; then
            check_fail "無法移除 ${SERVICE_NAME} 服務（需要 sudo 權限），未做任何變更"
            echo -e "  ${C_DIM}請在終端機手動執行：${NC}"
            echo -e "  ${C_DIM}  sudo systemctl disable --now ${SERVICE_NAME} && sudo rm -f ${SERVICE_FILE} && sudo systemctl daemon-reload${NC}"
            echo -e "  ${C_DIM}完成後再執行一次 ./install.sh --uninstall${NC}"
            return 1
        fi
        $SUDO systemctl daemon-reload >/dev/null 2>&1
        $SUDO systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1
        check_ok "已移除 ${SERVICE_NAME} 服務"
    fi
    if [ -f "$DESKTOP_FILE" ]; then
        rm -f "$DESKTOP_FILE"
        check_ok "已移除應用程式選單捷徑"
    fi
    # 桌面捷徑：只移除指向這個安裝資料夾的那一份（比對 Path= 那一行）；
    # 移除後重新安裝時會再問一次，選過「不要」的照舊不問
    local _dsk_file
    if _dsk_file=$(_shortcut_path desktop); then
        if [ -f "$_dsk_file" ] && grep -qxF "Path=${SCRIPT_DIR//\\/\\\\}" "$_dsk_file"; then
            rm -f "$_dsk_file"
            check_ok "已移除桌面捷徑"
        fi
    fi
    local _st
    _st=$(cat "$SHORTCUT_STATE_FILE" 2>/dev/null)
    if [ -n "$_st" ] && [ "$_st" != "no" ]; then rm -f "$SHORTCUT_STATE_FILE"; fi
    if [ -d "$VENV_DIR" ]; then
        rm -rf "$VENV_DIR"
        check_ok "已移除虛擬環境"
    fi
    echo ""
    echo -e "  ${C_DIM}如需一併刪除模型：rm -rf ~/.cache/huggingface/hub/models--*whisper* ~/.local/share/jt-live-whisper ~/.local/share/argos-translate${NC}"
}

# ─── 總結 ────────────────────────────────────────
print_linux_summary() {
    section "驗證安裝結果"
    source "$VENV_DIR/bin/activate" 2>/dev/null
    local failed_count=0 mod
    for mod in "faster_whisper|faster-whisper（語音辨識）" "ctranslate2|ctranslate2（辨識加速）" \
               "sounddevice|sounddevice（音訊擷取）" "opencc|OpenCC（簡繁轉換）" \
               "resemblyzer|resemblyzer（講者辨識）" "fastapi|WebUI"; do
        if python3 -c "import ${mod%%|*}" >/dev/null 2>&1; then
            check_ok "${mod#*|}"
        else
            check_fail "${mod#*|}"
            failed_count=$((failed_count + 1))
        fi
    done
    if [ "$LINUX_MODE" = "desktop" ]; then
        if python3 -c "import PyQt6.QtWidgets" >/dev/null 2>&1; then
            check_ok "PyQt6（懸浮字幕視窗）"
        else
            check_fail "PyQt6（懸浮字幕視窗）"
            failed_count=$((failed_count + 1))
        fi
    fi
    python3 -c "from moonshine_voice import get_model_for_language" >/dev/null 2>&1 \
        && check_ok "Moonshine（英文低延遲辨識）" \
        || echo -e "  ${C_DIM}[略過]${NC} Moonshine 未安裝（選裝，不影響主要功能）"
    python3 -c "$_QWEN_TF_CHECK" >/dev/null 2>&1 \
        && check_ok "Qwen3-ASR 本機辨識（實驗）" \
        || echo -e "  ${C_DIM}[略過]${NC} Qwen3-ASR 本機辨識未安裝（實驗，不影響主要功能）"
    python3 -c "$_NEMO_TF_CHECK" >/dev/null 2>&1 \
        && check_ok "Nemotron 講者辨識" \
        || echo -e "  ${C_DIM}[略過]${NC} Nemotron 講者辨識未安裝（講者辨識照舊用現行方法）"
    deactivate 2>/dev/null

    echo ""
    echo -e "${C_TITLE}============================================================${NC}"
    if [ "$failed_count" -eq 0 ]; then
        echo -e "${C_OK}${BOLD}  安裝完成！${NC}"
    else
        echo -e "${C_WARN}${BOLD}  安裝完成（${failed_count} 個元件未安裝，詳見上方提示）${NC}"
    fi
    echo -e "${C_TITLE}============================================================${NC}"
    echo ""
    if [ "$LINUX_MODE" = "desktop" ]; then
        echo -e "  ${C_WHITE}啟動方式: ${C_OK}./start.sh${NC}（終端機選單）  ${C_OK}./start.sh --webui${NC}（瀏覽器介面）"
        echo -e "  ${C_DIM}系統音訊直接從預設喇叭的 monitor 擷取，不需安裝虛擬音效卡${NC}"
    else
        echo -e "  ${C_WHITE}WebUI 服務: ${C_OK}systemctl status ${SERVICE_NAME}${NC}"
        echo -e "  ${C_WHITE}離線處理:   ${C_OK}./start.sh --input 音檔.mp3${NC}"
    fi
    echo -e "  ${C_WHITE}環境診斷: ${C_OK}./install.sh --doctor${NC}"
    echo -e "  ${C_WHITE}升級方式: ${C_OK}./install.sh --upgrade${NC}"
    echo ""
    _api_restart_hint
    if [ -n "$INSTALL_LOG" ] && [ -f "$INSTALL_LOG" ]; then
        echo -e "  ${C_DIM}安裝 log: $INSTALL_LOG${NC}"
        echo ""
    fi
}

# 升級後 REST API 服務仍在跑舊版（已載入記憶體）：提示重啟，不自動重啟
_api_restart_hint() {
    [ -n "${JTLW_RESTART_SERVICE:-}" ] && _api_unit_is_ours || return 0
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$API_SERVICE_NAME" || return 0
    echo -e "  ${C_WARN}REST API 服務（${API_SERVICE_NAME}）仍在執行舊版，要重啟才會換成新版：${NC}"
    echo -e "  ${C_OK}  sudo systemctl restart ${API_SERVICE_NAME}${NC}"
    echo -e "  ${C_DIM}  重啟約 1 秒；進行中的作業會自動接續、不會失敗，挑沒有作業的時候最好${NC}"
    echo ""
}

# ─── 主流程 ──────────────────────────────────────
# 主流程包在 { … exit; } 裡（v2.31.2）：升級會用 cp 原地覆寫安裝程式自己，bash 是邊讀邊執行，
# 沒包的話換檔之後會從新檔案的錯位處接著跑（start.sh 在 2026-10-12 的 Mac 上就這樣）。tools/test_script_self_replace.py
{
if [ -z "${JTLW_UPGRADE_REPO:-}" ]; then            # 升級時交給新版接手的那一次不再印標題（同一個畫面）
    print_title
    echo -e "  ${C_DIM}Linux 版（$( [ "$LINUX_MODE" = "server" ] && echo 伺服器 || echo 桌面 )）${NC}"
fi

case "$LINUX_ACTION" in
    help)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    upgrade)
        # 版本號只取數字：舊版安裝程式交給新版接手時，檔案已經換成新版了，換之前的版本由 JTLW_UPGRADE_FROM 帶過來
        _ver_before=${JTLW_UPGRADE_FROM:-$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')}
        do_upgrade || exit $?
        _ver_after=$(grep -m1 'APP_VERSION' "$SCRIPT_DIR/translate_meeting.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/')
        [ "$_ver_before" != "$_ver_after" ] && export JTLW_RESTART_SERVICE=1
        # 不接著檢查相依套件時（JTLW_SKIP_DEP_CHECK），摘要不會出現，提示要在這裡印
        [ -n "${JTLW_SKIP_DEP_CHECK:-}" ] && _api_restart_hint
        # 程式更新後，用「新版」安裝腳本重新檢查相依套件（新版可能新增系統或 Python 套件）；
        # 已安裝的項目會自動略過。伺服器版沿用伺服器模式。
        if [ -z "${JTLW_SKIP_DEP_CHECK:-}" ]; then
            _mode_flag=""
            if [ "$LINUX_MODE" = "server" ] || [ -f "$SERVICE_FILE" ]; then _mode_flag="--server"; fi
            echo ""
            echo -e "  ${C_WHITE}檢查新版的相依套件...${NC}"
            export JTLW_FROM_UPGRADE=1          # 升級時回答過「不裝 BreezyVoice」就不再問（install.sh 的 _rw_offer_breezy）
            exec bash "$SCRIPT_DIR/install-linux.sh" $_mode_flag
        fi
        exit 0
        ;;
    doctor)
        linux_doctor
        exit $?
        ;;
    uninstall)
        linux_uninstall
        exit 0
        ;;
esac

check_linux_system
check_internet || exit 1
_complete_upgrade_files               # 舊版安裝程式升級時漏掉的新檔（2026-10-10，見 install.sh）
check_running_processes || exit 1
check_disk_space || exit 1
check_linux_packages || exit 1
check_linux_audio
check_linux_venv || exit 1
check_nemotron_local
fix_migrated_config
[ "$LINUX_MODE" = "desktop" ] && check_moonshine
[ "$LINUX_MODE" = "desktop" ] && check_argos_model
check_nllb_model
check_faster_whisper_model
setup_remote_whisper_linux
install_desktop_entry
install_systemd_service
print_linux_summary
offer_desktop_shortcut

exit
}
