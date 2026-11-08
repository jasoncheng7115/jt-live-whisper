# Security Policy

## Supported Versions

Only the latest release receives security fixes. Please upgrade before reporting
(`./install.sh --upgrade` on macOS / Linux, `.\install.ps1 -Upgrade` on Windows).

This covers everything in this repository: the desktop program and WebUI, the server
mode (`install.sh --server`), the GPU server (`remote_whisper_server.py`) and the
REST API (`jtlw_api/`).

## Reporting a Vulnerability

Please do not open a public issue.
Use GitHub "Report a vulnerability" (Security tab), or email service@jason.tools.
We aim to acknowledge within 3 business days and to release a fix and
publish a GitHub Security Advisory within 7 days for confirmed issues.

Please do not include real meeting recordings or transcripts in a report.

## Security Advisories

Published advisories: https://github.com/jasoncheng7115/jt-live-whisper/security/advisories

| Advisory | Component | Fixed in |
|---|---|---|
| [GHSA-7m57-qfrp-hxfp](https://github.com/jasoncheng7115/jt-live-whisper/security/advisories/GHSA-7m57-qfrp-hxfp) | WebUI file upload | 2.22.2 |
| [GHSA-3gqv-3c48-2q4w](https://github.com/jasoncheng7115/jt-live-whisper/security/advisories/GHSA-3gqv-3c48-2q4w) | WebUI transcripts and live connection | 2.22.2 |
| [GHSA-ggpf-p5mq-8xxw](https://github.com/jasoncheng7115/jt-live-whisper/security/advisories/GHSA-ggpf-p5mq-8xxw) | WebUI behind a reverse proxy | 2.31.1 |
| [GHSA-j4wj-9r27-mv74](https://github.com/jasoncheng7115/jt-live-whisper/security/advisories/GHSA-j4wj-9r27-mv74) | WebUI LLM connection test | 2.21.3 |

Each release has a git tag (`v2.34.1`) and a GitHub Release; earlier versions back to 2.16.4 are tagged where the
release commit could be identified.

---

## 資安問題通報（中文）

- **只有最新版會收到資安修正**，通報前請先升級（macOS／Linux：`./install.sh --upgrade`；Windows：`.\install.ps1 -Upgrade`）
- 範圍包含本專案的所有元件：桌面程式與 WebUI、伺服器模式（`install.sh --server`）、GPU 伺服器（`remote_whisper_server.py`）、REST API（`jtlw_api/`）
- **請不要開公開的 issue**。請用 GitHub 專案 Security 分頁的「Report a vulnerability」私下通報，或寄信到 service@jason.tools
- 我們會在 3 個工作天內回覆；確認是問題後，目標在 7 天內釋出修正版並發布 GitHub 安全公告
- 通報時請不要附上真實的會議錄音或逐字稿
- 已發布的安全公告：https://github.com/jasoncheng7115/jt-live-whisper/security/advisories（編號與修正版本見上方英文段的表格）
- 每一版都有 git tag（例如 `v2.34.1`）與 GitHub Release；更早的版本（2.16.4 起）找得到發版 commit 的也補了 tag
