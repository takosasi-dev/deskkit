# DeskKit v0.4.1 — 担当表と契約

v0.4.0 のあとの小さな直し(バグ・UI・機能4つ)。v0.4 の不変条件(`INTERFACES_v0.4.md` §0)はすべてそのまま守る。

## 0. 守ること(全担当)

- フックを使わない。管理者権限を求めない。ファイルは完全に消さない(消すのは `deskkit.fileops.recycle` でごみ箱へ)。
- 通信するのは `updater.py` だけ。モジュールはネットワークを使わない。
- ログ・ops ファイル・`diagnostics()`・`usage()` に、ファイル名・パス・中身・利用者の入力・ボリュームラベル・exe 名・自動起動の項目名を書かない。
- モジュールどうしで import しない(本体 `deskkit.*` の共通部品は使ってよい)。
- 時刻はモジュールに渡された時計(`now=` など)を使う。テストで本物の時計に頼らない。
- Python は `<リポジトリ>\.venv\Scripts\python.exe` を絶対パスで呼ぶ(`python3` は呼ばない)。テストは自分の worktree の直下で
  `$env:PYTHONUTF8="1"; $env:QT_QPA_PLATFORM="offscreen"; & "<リポジトリ>\.venv\Scripts\python.exe" -m pytest -q` のように回す。
- SendKeys などのキー送信は使わない。プロセスの一覧の取得・終了もしない。
- ソースの編集は Write / Edit ツールで行う(シェルの heredoc で書かない)。
- コミットは自分のブランチにだけ。作者はリポジトリの設定のまま(`git -c user.*` や `--author` で上書きしない)。
  メッセージの末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` を付ける。

## 1. 担当表(wave 5)

| 担当 | worktree / ブランチ | 触ってよいファイル |
|---|---|---|
| A: UI | `<worktree の置き場>\w5-ui` / `w5/ui` | `deskkit/ui/**`、`deskkit/loader.py`(ログの文言だけ)、`deskkit/backup.py`(`summary` の表示だけ)、`tests/test_host_ui.py`、`tests/test_host_integration.py`、`tests/test_v041_ui.py`(新規)、`docs/v0.4.1/ui.md`(新規) |
| B: 小さなバグ | `<worktree の置き場>\w5-fixes` / `w5/fixes` | `deskkit/modules/{dropsort,startupwatch,jotdrop,pccheckup,modeshift}/**`、`tests/modules/{dropsort,startupwatch,jotdrop,pccheckup,modeshift}/**`、`docs/v0.4.1/fixes.md`(新規) |
| C: PlugSave | `<worktree の置き場>\w5-plugsave` / `w5/plugsave` | `deskkit/modules/plugsave/**`、`tests/modules/plugsave/**`、`docs/v0.4.1/plugsave.md`(新規) |
| D: TwinSweep・ClipShelf | `<worktree の置き場>\w5-sweepshelf` / `w5/sweepshelf` | `deskkit/modules/{twinsweep,clipshelf}/**`、`tests/modules/{twinsweep,clipshelf}/**`、`docs/v0.4.1/sweepshelf.md`(新規) |
| 統合担当 | main | KeyFree(`deskkit/modules/keyfree/**`)、本体の土台(`host.py`・`context.py`)、CHANGELOG・README・版番号 |

- 表に無いファイルは触らない。表に無いファイルを直す必要が出たら、直さずに報告に「本体への提案」として書く。
- `tests/modules/<名前>/` のテストの置き場所が上と違うときは、既存の置き場所に合わせてよい(報告に書く)。

## 2. 土台(統合担当が main に入れた物)

- `ctx.set_quick_action_hotkey(text: str) -> bool` — DeskKit のクイックアクションのキーを変える。`"Ctrl+Alt+K"` の形、空は使わない。
  メインスレッドから呼ぶ。登録できた(または空にした)なら True。取れなかった組も保存はされ、競合として一括通知に載る。
  構文エラーの settings.json は上書きせず `SettingsError`。中身は `Host.set_quick_action_hotkey`。
- `HostSignals.quick_hotkey_changed(str)` — 上が呼ばれたあとに出る。設定画面のキーの欄が表示を合わせる。

## 3. 各担当の仕事

### A: UI(本体の画面)

1. **サイドバーを詰める。** 窓の高さ 800px(既定の大きさ)で、見出し(ロゴ・DeskKit・版)から最後の「ログ」まで、スクロールなしで全部見えるようにする。
   1 行の高さ・区切りの余白を詰める。下のモジュールを選んでも見出しが切れないこと(`open_page` の `ensureWidgetVisible` で上が切れている)。
   最小の高さ 660px では今までどおりスクロールしてよい。既存のテスト `test_window_fits_low_screens_with_15_modules` を壊さない。
2. **ホームのカードを詰める。** 15 枚のモジュールカードを、窓の幅 1220px で 3 列にし、カードの高さを今(約 180px)の 6 割前後にする。
   カードに今ある物(アイコン・名前・一言・状態・状態文・スイッチ・開く、カード全体のクリックで開く)は残す。
   窓を狭くしたとき(中身の幅が 3 列に足りないとき)は 2 列に戻す。ホームの全体の高さを今の半分程度にする。
3. **利用状況の空の欄を畳む。** 選んだ期間に記録が 1 件も無いモジュールは、グラフを出さず 1 行(アイコン・名前・「この期間の記録はありません」)にする。
   記録のあるモジュールを上に、無いモジュールを下にまとめる(記録のあるものの中の順は今のまま)。表の表示でも同じ扱いにする。
4. **表示名と文字切れ。** 画面に出る内部名(`modeshift` など)を `catalog` の表示名(`ModeShift`)にする。対象: 「設定の世代」の要約(`backup.summary`)、ローダーのログの文言(「モジュール clipshelf を起動しました」→「ModeShift を起動しました」の形)。
   ロガー名(`host.loader` など)とログの列の見た目は変えない。
5. **FadeStack の高さ。** 隠れているページの高さまで最小の高さに入れているので、短いページのカードが縦に伸びる(`docs/v0.4/pagepress.md:115`)。
   今表示しているページの高さだけを使うように直す。PagePress・MojiFix がそれぞれで避けている回避策は、モジュールの担当ではないので触らない。
6. 見た目は実際の Windows の描画で撮って確かめる(offscreen はフォントもテーマも違う)。撮り方: 一時 `DESKKIT_HOME`・`theme.apply(app)`・`host.show_window()`・`win.grab()`。
   DropSort を有効にして撮るなら `downloads_dir_override` を一時フォルダにする。撮るのは自分の窓だけ。ダーク・ライト両方で見る。

### B: 小さなバグ

1. **DropSort**: `service.py:334` のログにパス(`snap.path`)が入っている。件数だけにする。ほかのログにもパス・名前が無いか grep して、あれば同じく直す。
2. **StartupWatch**: 設定のキーがまだ無いだけ(初回)でも `settings_fixed` の警告が出る。キーがあって値が合わないときだけ警告にする(無いキーは黙って既定値を入れる)。書き戻しは今のまま。
3. **JotDrop**:
   - ページのファイル名の説明が「2026-09-26 の形」と固定の日付になっている。今日の日付(モジュールの時計)で出す。
   - `module.py:92` の `compose.validate_pattern(p, datetime.now())` をモジュールの時計にする。
   - ページで「キー」の設定が 2 か所(上の「メモ欄を出すキーを決めてください」と「キー」のカード)に出ている。キーが未設定のときの呼びかけのカードだけにし、設定済みなら「キー」のカードだけにする(同じ時に 2 つ出さない)。
4. **PcCheckup**: Secure Boot の判定(`checks/boot.py` の `today()`、`module.py:282` 付近)がモジュールの時計ではなく本物の日付を使う。モジュールの時計を渡す。
5. **ModeShift**: モードの編集の一覧(左の列)で、2 行目(`meeting` など)の文字の下が切れている。行の高さか余白を直す。ダーク・ライトで撮って確かめる。
6. 各項目にテストを足す(4 と 3 の時計は、偽の時計で日付が変わることを確かめる)。

### C: PlugSave — 途中で抜いたときの手当て

背景: v0.4.0 で、1MiB 未満のファイルは fsync しないことにした(`copier.py` の `FSYNC_MIN_BYTES`)。途中で抜くと、小さいファイルが中身の欠けたまま最終の名前で残ることがある。
次の回は大きさと更新日時で比べるので気づかない。

1. 1 回のバックアップ(ドライブ 1 台分)が**終わらずに止まった**ことを、次に同じドライブで動くときに分かるようにする。
   止まり方: ドライブを抜いた・DeskKit が落ちた・PC が止まった・利用者が止めた。印はモジュールのデータ(`ctx.data_dir`)かドライブ側のどちらでもよいが、ファイル名・パスを持たせない。
2. 前の回が終わっていなかったら、次の回で「前の回に書いた小さいファイル」をもう一度コピーする。前の回で書いた物の見分け方は担当が決める
   (例: ドライブ側のファイルの作成日時が前の回の開始より後、など。FAT32・exFAT・NTFS で成り立つか確かめ、成り立たない場合の扱いも決める)。
   大きいファイル(fsync しているもの)は今のまま。
3. 画面と通知: 前の回が途中で止まっていたことを、前回の結果の欄に 1 行で出す(例:「前回は途中で止まったので、その回に書いた小さいファイルをもう一度コピーしました(N 件)」)。
4. ログ・ops・usage には件数だけを書く。
5. テスト: 止まった回のあとの回で、欠けたファイルが直ることを、偽のドライブ(一時フォルダ)で確かめる。印が壊れている・無いときも落ちないこと。

### D: TwinSweep・ClipShelf

**TwinSweep — 似ている度合いを変えても選択を保つ**(`docs/v0.3/twinsweep.md` T-9)

1. 利用者が選び直した「残す/ごみ箱へ」を、写真ごと(同じファイル)に覚えておき、度合いを変えてグループを作り直したあとも当てる。覚えるのはメモリの中だけ(ファイルに書かない)。
2. グループの中に「残す」が 1 枚も無くなる組み合わせになったら、そのグループは初期の選び方に戻す(全部ごみ箱にならないこと)。
3. 新しいスキャンを始めたら、覚えた選択は捨てる。
4. テストを足す。

**ClipShelf — 定型文の書き出し・読み込み**(ClipShelf 仕様書 §13 Q-9)

1. 定型文だけを、利用者が選んだ場所の 1 つのファイルに書き出す(履歴は書き出さない)。形式は UTF-8 の JSON(版番号付き)。
   中身は暗号化しない平文になるので、書き出す前の確認の窓で「このファイルは誰でも読めます」とはっきり書く。
2. 読み込みは、既存の定型文に足す。同じ中身(と同じ名前)の物は足さない。読めない・形が違うファイルは何も変えずに知らせる。件数の上限があればそれに従い、超えた分は足さずに件数を知らせる。
3. ログ・ops・usage には件数だけ。書き出し先のパスもログに書かない。
4. 画面は定型文の欄の近くに「書き出す」「読み込む」を置く。テストを足す。

## 4. 報告の形(全担当)

終わったら次を返す。

- ブランチの最後のコミットの ID
- 自分の worktree でのテスト結果(`pytest -q` の最後の行)、`ruff check deskkit tests`、`mypy deskkit overlaykit` の結果
- やったこと、仮に決めたこと(利用者に確かめたいこと)、本体への提案
- 撮った画面があれば、その PNG のパス
- 同じ内容を `docs/v0.4.1/<担当>.md` にも書いてコミットする
