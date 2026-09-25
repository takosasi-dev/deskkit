# リリース手順(自動更新に載せる)

DeskKit の自動更新は、GitHub `takosasi-dev/deskkit` の **最新リリース(latest)** を見に行く。
リリースに次の2つが添付されていれば、利用者の画面に「新しい版があります」が出て、ワンクリックで更新できる。

| 添付ファイル | 中身 |
|---|---|
| `DeskKit.exe` | ビルドした exe |
| `DeskKit.exe.sha256` | `<SHA-256 の16進64文字>  DeskKit.exe`(build.ps1 が作る) |

`DeskKit.exe.sha256` が無いリリースは、通知はするが自動入れ替えはしない(リリースページを開く案内になる)。

## 手順

1. `deskkit/__init__.py` の `__version__` を上げる(例: `0.1.0` → `0.2.0`)。`CHANGELOG.md` に節を足す。
2. 検査: `.venv\Scripts\python -m pytest` と `.venv\Scripts\python -m deskkit --selftest all` が通ること。
3. ビルド: `powershell -ExecutionPolicy Bypass -File build.ps1` → `dist\DeskKit.exe` と `dist\DeskKit.exe.sha256`。
4. `dist\DeskKit.exe --selftest all` が 0 で終わることを確認。
5. 公開用フォルダ(説明書「GitHub公開ルール」の手順)にソースをコピーし、チェックリストを通して push。
6. GitHub でリリースを作る。タグは `v0.2.0` の形(`v` + `__version__`)。**プレリリースにしない**(latest だけを見るため)。
   - `gh` がある PC なら: `gh release create v0.2.0 dist\DeskKit.exe dist\DeskKit.exe.sha256 --repo takosasi-dev/deskkit --title "v0.2.0" --notes-file <CHANGELOG の該当節>`
   - Web なら: Releases → Draft a new release → タグ `v0.2.0` → 2ファイルを添付 → Publish。
7. リリースノート(本文)は更新ダイアログにそのまま表示される(Markdown)。

## 更新の仕組み(利用者側)

1. 起動30秒後と1日1回、`https://api.github.com/repos/<repo>/releases/latest` を読む(設定でオフ可)。
2. `tag_name` が今の版より新しければ通知。「更新して再起動」で `DeskKit.exe` をダウンロード。
3. `DeskKit.exe.sha256` と一致するか・PE ヘッダかを確認し、新しい exe を `--version-file` で試しに起動して版数を確かめる。
4. 実行中の exe を `DeskKit.previous.exe` に改名し、新しい exe を置いて再起動する。失敗したら元に戻す。
5. 設定画面の「前の版に戻す」で `DeskKit.previous.exe` と入れ替えられる。

接続先は `api.github.com` / `github.com` / `objects.githubusercontent.com` / `release-assets.githubusercontent.com`(HTTPS のみ)に限っている。
exe が書き込めない場所(Program Files など)にあると入れ替えできないので、利用者にはドキュメント等に置いてもらう。
