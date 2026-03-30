# mlbai

報知新聞MLBページ（`https://hochi.news/mlb/?kd_page=top`）から試合情報を取得し、
試合のイニング別スコアと結果をXへ自動投稿するGitHub Actions BOTです。

## 機能

- 定期実行（15分ごと）で最新の試合ページURLを自動検出
- 試合ページからイニング別スコア表を抽出
- 1試合ごとにXへ投稿（必要に応じてスレッド分割）
- `state.json`で投稿済み試合（内容ハッシュ）を管理し、重複投稿を防止

## ファイル構成

- `bot.py`: スクレイピング + 整形 + X投稿 + 状態管理
- `requirements.txt`: Python依存ライブラリ
- `state.json`: 投稿済み管理
- `.github/workflows/mlb_x_post.yml`: GitHub Actions定期実行設定

## セットアップ

### 1. GitHub Secretsを設定

Repository Settings → Secrets and variables → Actions → New repository secret で以下を追加。

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

### 2. Actionsを手動実行（初回確認）

- GitHubの `Actions` タブ
- `MLB X Auto Post` ワークフロー
- `Run workflow`

## ローカル実行（任意）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
DRY_RUN=true python bot.py
```

`DRY_RUN=true` の場合はX投稿せず、投稿内容をログ出力します。

## 補足

- 対象サイトのHTML構造変更時は、`parse_score_table()` の抽出ロジックを更新してください。
- `state.json` はActions実行時に更新され、自動コミットされます。
