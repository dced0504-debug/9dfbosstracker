# DFP Bossmap Discord Bot

用 GitHub Actions 每 15 分鐘抓一次 [DFProfiler Bossmap](https://www.dfprofiler.com/bossmap)，透過 Discord Webhook 發文。不用開電腦、不用伺服器、免費。

**功能**
- **整點報告**：每小時一則，附地圖截圖、Current Cycle、今日每日特殊王 / 活動王 ✅❌。頻道裡只保留最近 12 則，舊的自動刪除。
- **特殊王通報**：Volatile Leaper、Behemoth、每日 Devil Hound，以及活動王（Werewoo、Nefertiti、Snowman Head…）。每隻在同一個遊戲日只通報一次，附該格子的地圖圖。
- **遊戲日**：依頁面上的 `Last World Reset` 切換。
- Discord 上的訊息全部是英文。

---

## 部署步驟（第一次大約 10 分鐘）

### 1. 建立 Discord Webhook
1. 在要發文的頻道按 ⚙️「編輯頻道」→「整合」→「Webhook」→「新 Webhook」
2. 取好名字和頭像，按「複製 Webhook 網址」，先貼到記事本保存

### 2. 建立 GitHub repo
1. 註冊或登入 [github.com](https://github.com)，右上角 **+ → New repository**
2. Repository name 隨便取，例如 `dfp-bossmap-bot`
3. **一定要選 Public**（public repo 的 Actions 不限分鐘數；private 每月 2000 分鐘，會不夠用）
4. 按 Create repository
5. 在新 repo 頁面點 **uploading an existing file**，把解壓後資料夾裡的**所有東西**拖進去（包含 `.github` 資料夾），按 Commit changes
   - 如果 `.github` 資料夾沒有被上傳（有些系統會隱藏點開頭的資料夾），就在 repo 裡按 **Add file → Create new file**，檔名打 `.github/workflows/bossmap.yml`，把 `bossmap.yml` 的內容貼進去後存檔

### 3. 設定 Webhook 網址（機密）
repo 的 **Settings → Secrets and variables → Actions → New repository secret**
- Name：`DISCORD_WEBHOOK_URL`
- Secret：貼上第 1 步複製的網址

### 4. 開放寫入權限（讓機器人能存狀態）
**Settings → Actions → General → Workflow permissions** → 選 **Read and write permissions** → Save

### 5. 啟動並測試
1. 到 **Actions** 分頁，如果有提示就按綠色按鈕啟用 workflows
2. 左邊點 **DFP Bossmap Bot** → 右邊 **Run workflow** → 勾選「立刻發一則整點報告」→ Run
3. 大約 1–2 分鐘後，Discord 應該會出現報告。如果失敗，點進該次執行就能看到 log

之後就會自動每 15 分鐘跑一次。

---

## 設定（`config.json`）

| 欄位 | 說明 |
|---|---|
| `keep_hourly_messages` | 整點報告保留幾則（預設 12）|
| `alert_mention` | 特殊王通報時要 tag 誰，例如 `"<@&身分組ID>"` 或 `"@here"`；空字串 = 不 tag |
| `daily_specials` | 每日特殊王清單（會固定顯示 ✅/❌）|
| `event_bosses` | 活動王名單；遊戲出新的活動王時，把名字加進來就好 |
| `event_expected_days` | 活動王最近幾天內出現過，今天還沒出時就顯示 ❌（預設 3 天）|
| `daily_window_minutes` | 判斷「每日 Devil Hound」的時間門檻；一般 Devil Hound 只持續 1 小時，每日那隻持續 3 小時 |

在 GitHub 上直接點檔案 → ✏️ 編輯 → Commit，就會在下一次執行時生效。

---

## 注意事項
- **時間不會很準**：GitHub 的排程常延遲 5–20 分鐘，所以「整點報告」實際上可能在 :05–:25 之間發出。特殊王是 3 小時的長窗口，延遲幾分鐘不影響。
- **60 天規則**：public repo 如果 60 天沒有任何活動，GitHub 會停用排程。機器人每天都會 commit `state.json`，正常情況下不會觸發；萬一被停用，到 Actions 頁面重新啟用即可。
- `state.json` 是機器人的記憶（今天出過哪些王、要刪除的訊息 ID），**不要手動刪除**。如果要整個重來，把內容改成 `{}`。
- 如果 dfprofiler 改版導致抓不到資料，這次執行會失敗並且不發文。到 Actions 看 log，再告訴我錯誤訊息。
