# YouTube 関連動画抽出ツール

## 構成

| ディレクトリ | 説明 | デプロイ先 |
|---|---|---|
| `frontend/` | HTML/CSS/JS フロントエンド | **Vercel** |
| `backend/` | FastAPI + Playwright API | **Railway** |

## セットアップ

### バックエンド（Railway）
1. Railway で新規プロジェクト作成
2. `backend/` ディレクトリを指定してデプロイ
3. デプロイ後の URL を控える（例: `https://xxx.up.railway.app`）

### フロントエンド（Vercel）
1. `frontend/index.html` の `BACKEND_URL_PLACEHOLDER` をRailway URLに差し替え
2. Vercel で `frontend/` ディレクトリをルートとしてデプロイ
