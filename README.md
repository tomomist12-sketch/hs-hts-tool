# HS / HTS コード判定ツール

商品情報から HS / HTS コードを自動推定する Streamlit アプリです。
外注スタッフ・スクール生徒が、ブラウザから簡単に HS/HTS コードを判定できます。

## 機能

- 商品URL スクレイピングによる情報取得（eBay API 対応）
- キーワードルールベースの HS コード推定（6桁 → HTS 10桁 / 日本HS 9桁）
- Claude AI による高精度分類（APIキー設定時）
- 最大3候補 + 信頼度表示
- SQLite 履歴保存・検索・CSV エクスポート
- 管理者モード（履歴削除・コード手動修正）

## ローカル実行

```bash
# 依存パッケージをインストール
pip install -r requirements.txt

# アプリを起動
streamlit run app.py
```

ブラウザで `http://localhost:8501` を開きます。

## Streamlit Cloud デプロイ

1. このリポジトリを GitHub にプッシュ
2. [share.streamlit.io](https://share.streamlit.io) にアクセスしてログイン
3. 「New app」からリポジトリ・ブランチ・`app.py` を選択してデプロイ
4. アプリの Settings → Secrets に以下を設定（任意）:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
EBAY_API_KEY = "v^1.1#..."
```

Secrets を設定すると、ユーザーが個別にAPIキーを入力する必要がなくなります。

## APIキーの設定方法

APIキーは以下の優先順位で読み込まれます:

1. **Streamlit Secrets** (`st.secrets`) — Streamlit Cloud デプロイ時に推奨
2. **環境変数** — ローカル実行時に推奨
3. **サイドバーから手動入力** — SQLite に保存

## 技術スタック

Python / Streamlit / SQLite / requests / BeautifulSoup / Pillow
