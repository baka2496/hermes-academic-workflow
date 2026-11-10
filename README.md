# Hermes 學業總指揮官

小居把 TronClass、Gmail 和使用者交代的學業事項整理成同一份待辦。協調器在 Hermes 的 `command-center` 看板派發準備卡，驗證產物後可透過 Telegram 送出摘要和附件。本人仍負責學習、出席、考試和正式繳交。

儲存庫收錄協調器、來源讀取程式、Hermes 外掛、學業技能和測試。帳號憑證、課務資料、郵件、工作卡、通知收據及資料庫由本機管理。

## 在 Windows 配置

本版對應 Hermes Agent 0.23.0 的 Windows 桌面版，驗證使用 Python 3.14。準備 `commander` 和 `hermes-agent` 兩個 profile、`command-center` 看板，以及兩個 profile 的模型授權。準備卡派給 `hermes-agent`，使用 `openai-codex` 的 `gpt-6-luna`。請在隔離的 Hermes 資料目錄配置，並把 `HERMES_HOME` 指向該目錄。

1. 在新環境建立上述 profile，並以 `hermes kanban boards create command-center` 建立看板。確認以下目的路徑後，把儲存庫內的檔案放入 Hermes 資料目錄。已有同名檔案時，先比對內容再合併。

   | 儲存庫路徑 | Hermes 資料目錄內的路徑 |
   | --- | --- |
   | `hermes/shared/academic-workflow/` | `shared/academic-workflow/` |
   | `hermes/shared/skills/academic-workflow/` | `shared/skills/academic-workflow/` |
   | `hermes/profiles/commander/scripts/academic-coordinator.py` | `profiles/commander/scripts/academic-coordinator.py` |
   | `hermes/profiles/commander/plugins/user-academic-workflow/` | `profiles/commander/plugins/user-academic-workflow/` 和 `profiles/hermes-agent/plugins/user-academic-workflow/` |

2. 將 `shared/academic-workflow/config.example.json` 複製為同目錄的 `config.json`，再填入本機絕對路徑。 `hermes_payload` 指向包含 `hermes-agent` 和 `venv` 的桌面版 `agent-payload`。 `runtime` 指向安裝後的 `shared/academic-workflow`。在 `runtime_python` 對應的 Python 環境安裝 `pydantic`、`openai`、`jiter` 和 `psutil`。狀態目錄 `state_root` 放在 Git checkout 外。

3. 合併下列設定到兩個 profile 各自的 `config.yaml`。將 `external_dirs` 的範例換成本機路徑，並在 `commander` 設定 `cron.script_timeout_seconds: 1500`。啟動或重新載入 gateway 後，從小居查詢學業待辦以確認 `academic_workflow` 已載入。

   ```yaml
   plugins:
     enabled:
       - user-academic-workflow
   skills:
     external_dirs:
       - C:/path/to/hermes-home/shared/skills
   ```

### 接上學校與信箱

- `tronclass_repo` 指向已配置登入的 FJU-TronClass-MCP checkout。協調器用它的 `.venv/Scripts/python.exe` 讀取資料，並在準備卡提供 `.venv/Scripts/fjumcp.exe` 的路徑。 [FJU-TronClass-MCP](https://github.com/RX5950XT/FJU-TronClass-MCP)
- `mailbox` 指向本機已授權的 Gmail 連接器，`mail_python` 指向可執行它的 Python。連接器需提供 [collect_mail.py](hermes/shared/academic-workflow/collect_mail.py) 使用的 `gmail`、`folder`、`imap_result`、`search`、`gmail_fetch`、`describe_gmail` 和 `gmail_message`。Gmail 先用寄件者、主旨和課程名稱篩選候選信，再讀候選信的內文。
- Telegram 送達預設關閉。啟用前，在 `commander` 的本機 Hermes 設定 Bot 憑證和私人 `home_channel`，再將 `delivery_enabled` 改為 `true`。

### 建立暫停中的排程

從 PowerShell 設定 `HERMES_HOME`。用 `cron list --all` 檢查既有排程，只有在清單尚無 `academic-coordinator.py` 時才建立。

```powershell
$env:HERMES_HOME = 'C:/path/to/hermes-home'
$config = Get-Content -Raw "$env:HERMES_HOME/shared/academic-workflow/config.json" | ConvertFrom-Json
hermes -p commander cron list --all
hermes -p commander cron create '*/5 * * * *' --name '小居學業工作流' `
	--script academic-coordinator.py --no-agent --deliver local --failure-deliver local `
	--interpreter $config.runtime_python --workdir $config.runtime `
	--timezone Asia/Taipei --catch-up off --paused
```

準備好後，用 `hermes -p commander cron resume <排程 ID>` 啟用。排程每五分鐘檢查工作卡，預設每三十分鐘讀取來源。既有安裝若也有獨立的 TronClass 公告或晨間郵件排程，請在本機管理其通知範圍，避免同一事項重複通知。此版本的 ICS 是本機待辦匯出。

## 執行測試

在 checkout 根目錄執行。測試資料寫在 `tests/fixtures/` 和 `tests/native-fixtures/`。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install psutil
$env:PYTHONPATH = (Resolve-Path .\hermes\shared\academic-workflow).Path
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

如需執行原生 Kanban 測試，將 `ACADEMIC_HERMES_PAYLOAD` 設為桌面版 `agent-payload` 的路徑，再以配置好的 `runtime_python` 重跑相同測試。測試使用合成資料和隔離的 SQLite，不會登入課務、信箱或 Telegram。
