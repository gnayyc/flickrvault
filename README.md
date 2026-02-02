# flickrvault

完整的 Flickr 備份與管理工具，支援增量同步、元資料備份、相簿管理、視覺瀏覽等功能。

## 功能特色

- 📷 **完整備份** - 照片檔案 + 完整元資料（EXIF、標籤、地理位置、相簿、人物標記、統計）
- 🔄 **增量同步** - 透過 `lastupdate` 時間戳只下載新/更新的照片
- 🗃️ **SQLite 索引** - 快速搜尋 50K+ 照片
- 🌐 **視覺瀏覽** - 本地 HTML 相簿 / Web 伺服器
- 📤 **上傳** - 支援單檔或整個資料夾上傳
- 🏷️ **標籤管理** - 新增/移除照片標籤
- 📂 **相簿管理** - 建立、刪除、重新命名相簿
- 🔒 **隱私設定** - 批次修改照片可見性

## 安裝

```bash
# 使用 uv 執行（推薦，自動安裝依賴）
uv run flickrvault.py auth

# 或傳統安裝
pip install flickr-api
python flickrvault.py auth
```

## 快速開始

```bash
# 1. 認證（首次使用）
uv run flickrvault.py auth

# 2. 備份所有照片到 ./flickr_backup
uv run flickrvault.py sync

# 3. 查看備份統計
uv run flickrvault.py stats
```

## 命令

### 認證
```bash
flickrvault.py auth
```

### 同步備份
```bash
# 增量同步（預設）
flickrvault.py sync

# 完整重新同步元資料
flickrvault.py sync --full

# 只同步元資料（不下載照片檔案）
flickrvault.py sync --meta-only

# 限制數量（測試用）
flickrvault.py sync -n 100

# 指定日期範圍
flickrvault.py sync --from-date 2024-01-01 --to-date 2024-12-31

# 指定輸出目錄
flickrvault.py sync -o /path/to/backup
```

### 下載相簿
```bash
# 下載指定相簿
flickrvault.py download ALBUM_ID

# 從相簿 URL 下載
flickrvault.py download https://www.flickr.com/photos/user/albums/123456

# 下載包含某照片的所有相簿
flickrvault.py download --photo PHOTO_ID
```

### 上傳
```bash
# 上傳單張照片
flickrvault.py upload photo.jpg

# 上傳到指定相簿
flickrvault.py upload photo.jpg -a "旅遊照片"

# 上傳整個資料夾
flickrvault.py upload ./photos_folder -a "2024"

# 加上標籤
flickrvault.py upload photo.jpg -t "travel,japan,2024"
```

### 搜尋
```bash
# 搜尋自己的照片
flickrvault.py search --text "sunset"

# 搜尋特定標籤
flickrvault.py search -t "travel,japan"

# 搜尋特定日期
flickrvault.py search --date 2024-08
```

### 相簿管理
```bash
# 列出所有相簿
flickrvault.py list

# 建立新相簿
flickrvault.py album create "新相簿名稱"

# 刪除空相簿
flickrvault.py album delete --empty

# 刪除特定相簿
flickrvault.py album delete ALBUM_ID
```

### 標籤管理
```bash
# 列出照片標籤
flickrvault.py tag PHOTO_ID list

# 新增標籤
flickrvault.py tag PHOTO_ID add travel japan

# 移除標籤
flickrvault.py tag PHOTO_ID remove old-tag
```

### 視覺瀏覽
```bash
# 在瀏覽器中查看（依日期）
flickrvault.py browse

# 依相簿查看
flickrvault.py browse --by album

# 啟動本地 Web 伺服器
flickrvault.py serve --port 8080
```

### 其他操作
```bash
# 查看統計
flickrvault.py stats

# 修改隱私設定
flickrvault.py privacy PHOTO_ID --set private

# 移動照片到其他相簿
flickrvault.py move PHOTO_ID --to ALBUM_ID

# 重新命名照片
flickrvault.py rename photo PHOTO_ID --title "新標題"
```

## 選項

| 選項 | 說明 |
|------|------|
| `-c, --config DIR` | 指定設定目錄 |
| `-q, --quiet` | 安靜模式（只顯示錯誤） |
| `-v, --verbose` | 詳細輸出 |
| `--progress` | 顯示進度條 |

## 設定

設定檔預設位於 `~/.config/flickrvault/`，可透過環境變數 `FLICKRVAULT_CONFIG` 或 `-c` 選項指定。

## Rate Limiting

工具內建智慧 rate limit 處理：
- 自動偵測 429 錯誤並等待
- 指數退避（120s → 240s → 480s → 600s）
- 連續成功後自動加速
- 隨機 jitter 避免同步請求

## License

MIT
