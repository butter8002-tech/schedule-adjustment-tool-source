# Schedule Adjustment Tool v2.0.0

練習会の日程調整を行うStreamlitアプリケーションの公開用ソースです。
システム管理者、スケジュール担当者、参加者の各機能を、単一の
`app.py`から利用します。

このリポジトリは、実際の運用データ、Secrets、開発用テスト、監査記録、
サンプルDBを含まないソース公開用スナップショットです。

## 主な機能

- 企画条件、成立条件、個別条件、評価条件の管理
- 参加者登録、回答受付、代理回答
- 厳密・近似・自動探索による日程候補の生成
- 候補の手動調整、比較、公開
- 公開後の改訂と履歴管理
- SQLiteおよびTurso/libSQLへの保存
- システム管理者、スケジュール担当者、参加者の権限制御

## リポジトリ構成

```text
.
├── app.py                         # Streamlitの唯一の起動ファイル
├── requirements.txt              # Python依存パッケージ
├── .streamlit/
│   └── config.toml               # Streamlit共通設定
├── docs/
│   ├── USAGE_GUIDE.md            # v2.0.0利用者ガイド
│   └── user_guide_assets/        # ガイドで使用する画面画像
└── src/
    └── schedule_adjustment_tool/
        ├── domain/               # データモデル、条件判定、候補探索
        ├── exports/              # CSV・Excel出力
        ├── integrations/         # Turso関連処理
        ├── storage/              # SQLite・Turso保存と互換処理
        └── ui/                   # 管理者・担当者・参加者画面
```

参加者機能は別サイトではなく、`app.py`内の操作区分として提供されます。
内部実装は`src/schedule_adjustment_tool/ui/participant_workspace.py`です。

## ローカル起動

Python 3.12を使用します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

ローカルでは既定でSQLiteを使用します。DBやログなどの実行時生成物は
Git管理の対象外です。

## Streamlit Community Cloud

- Main file path: `app.py`
- Python version: Advanced settingsで`3.12`を選択
- Dependencies: ルートの`requirements.txt`

Cloudではローカルファイルが永続化されないため、Tursoを設定します。
SecretsはGitHubへ保存せず、Streamlit CloudのSecrets設定に入力してください。

```toml
SCHEDULE_AUTH_REQUIRED = true
SCHEDULE_BOOTSTRAP_ADMIN_USERNAME = "admin"
SCHEDULE_BOOTSTRAP_ADMIN_PASSWORD = "replace-with-at-least-12-characters"

[schedule_storage]
backend = "turso"
database_url = "libsql://replace-with-database-url"
auth_token = "replace-with-auth-token"

[schedule_security]
password_secret_key = "replace-with-64-hex-characters"
```

詳しい操作方法は[v2.0.0利用者ガイド](docs/USAGE_GUIDE.md)を参照してください。

## ライセンス

このリポジトリにはライセンスを付与していません。ソースコードの公開は、
第三者に利用、改変、再配布の許可を与えるものではありません。
