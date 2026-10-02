# DeskKit(QOL ツールキット)

PC 生活の QOL を上げる15のツールを、**1つの常駐プロセス・1つのトレイアイコン**にまとめた Windows 用アプリ。
常駐して自動で動くもの(ModeShift・DropSort・LayoutKeep・ClipShelf、v0.4.0 の EyeBreak・StartupWatch・PlugSave)、
キー1つで呼ぶもの(v0.4.0 の JotDrop)、困ったときに開いて1回で片づけるもの(v0.3.0 の SendPrep・PcCheckup・TwinSweep、
v0.4.0 の KeyFree・PagePress・MojiFix・ClipTrim)がある。

| モジュール | できること | 最初の動き |
|---|---|---|
| **ModeShift** | 「ゲーム」「勉強」などのモードを作り、電源プラン・音量(全体/アプリ別)・アプリの起動/終了・マイク・アプリのダーク/ライト・フォルダ/URL・ウィンドウ配置の呼び出しを1操作で実行。アプリの起動や電源(AC/バッテリー)で自動切替もできる。電源・音量・マイク・テーマは元に戻せる | 新しいモードは初回にプレビューで確認してから実行 |
| **DropSort** | ダウンロードが**完了した**ファイルだけをルールで振り分け。開かない・実行しない・MOTW(インターネット由来の印)を必ず保つ。`{yyyy}\{mm}` などの移動先テンプレート、ルールの試し当て。元に戻せる | ルールは試運転(予定を表示するだけ)から |
| **LayoutKeep** | モニタ構成ごとにウィンドウ配置を保存し、抜き差し・スリープ復帰で崩れたら戻す。名前付きプリセット・自動スナップショット・新しいウィンドウの配置。区別できないウィンドウは動かさない | 試運転(計画を表示するだけ)から |
| **ClipShelf** | コピーしたテキストを DPAPI で暗号化して記録し、`Ctrl+Alt+V` のパレットで検索・呼び出し。定型文(入力欄つき)・書式なし貼り付け・変換して貼り付け | 観察モード(記録しない)から |
| **SendPrep** | 写真・スクリーンショット・動画を人に送る前に、位置情報などの隠れた情報を消し、送り先のサイズ上限に収め、見せたくない文字を塗りつぶした**別のファイル**を作る。エクスプローラーの「送る」からも渡せる | 放り込んで「整える」を押したときだけ動く。元のファイルは変えない |
| **PcCheckup** | 「重い」「ネットが遅い」「容量が足りない」のボタンで原因の候補を調べ、次にやることをわかりやすい言葉で示す。設定は変えず、案内するだけ | ボタンを押したときだけ調べる(空き容量の見張りは既定オフ) |
| **TwinSweep** | フォルダの中のそっくりな写真をグループにまとめ、いちばん良い1枚を提案する。残りは確認してから**ごみ箱へ**送る(元に戻せる) | 選んだフォルダを調べるだけ。消すのは確認のあと |
| **EyeBreak** | キーボードとマウスを続けて使った時間を数え、「目を休めませんか」(20 分)「ひと休みしませんか」(60 分)と声をかける。ゲーム・全画面の間は出さない | 様子見(声をかけずに「出していたら N 回」だけ表示)から |
| **JotDrop** | 決めたキーで小さな入力欄を出し、1行書いて Enter で、決めた Markdown / テキストのファイルの末尾に時刻つきで足す。書く前の内容は変えない | キーは未割り当て。自分で決める |
| **KeyFree** | キーの組み合わせを1つずつ「登録してすぐ外す」で、ほかのアプリが使っているかを調べ、空いている組を一覧にする(クリックでコピー)。**どのアプリが使っているかは分からない** | 「調べる」を押したときだけ動く |
| **StartupWatch** | 自動起動(Run・RunOnce・スタートアップ フォルダ)に新しい物が増えたら知らせる。外すときは Windows の設定などを開くだけで、DeskKit は書き換えない | 初めは今ある物を覚えるだけ |
| **PagePress** | PDF の結合・分割・並べ替え・回転・抜き取り、写真から PDF を作る、軽くする、を PC の中だけで行う。**元の PDF は変えない**(パスワード付きの PDF は扱えない) | 操作したときだけ動く |
| **MojiFix** | 化けた CSV など・zip の中のファイル名・分かれた濁点を、候補を見比べながら直す。新しいファイルを作る(濁点の直しは確認のあとでファイル名を変える) | 候補を選ぶまで書き出さない |
| **ClipTrim** | 録画した動画から要る区間だけを切り出す。画質そのままの速い切り方と、ぴったりの位置で切る切り方。**元の動画は変えない** | 操作したときだけ動く |
| **PlugSave** | 登録したドライブを挿すと、選んだフォルダの増えた分と変わった分だけをコピーする。バックアップ先のファイルは消さず、上書きする前の版も残す | 初めてのドライブは件数を見せて「始める」を押してから |

初期状態はすべてのモジュールが **無効**。Control Center(メイン画面)のスイッチで1つずつ有効にする。

本体の機能:

- **クイックアクション**(既定 `Ctrl+Alt+Space`): どこからでも検索窓を開き、全モジュールの操作を名前で実行(ゲーム・全画面中は開かない)
- **利用状況**: モジュールごとの日別件数を棒グラフ/表で表示(7/30/90日)。仕様書の撤退基準の目安も表示
- **自動更新**: GitHub `takosasi-dev/deskkit` の最新リリースを確認し、ワンクリックで更新(SHA-256 検証・前の版に戻せる)。手順は `docs/RELEASING.md`
- **設定のバックアップ/復元**、**はじめてガイド**、**ダーク/ライト/Windows 連動のテーマ**
- **一時停止**: トレイから 30分 / 1時間 / 再開するまで、自動で動く処理だけを止める(再起動で解除)
- **ゲーム・全画面中の通知の保留**: 終わってから「保留中の通知 N 件」にまとめて表示(エラーはすぐ表示)
- **設定の自動世代保存**: settings.json の変更ごとに直近 20 世代を残し、設定画面から戻せる
- **診断レポート**: 不具合の相談用に、パス・ユーザー名・本文を含まない状態の要約をコピー
- **ライセンス表示**: 設定画面の「ライセンス」で、同梱しているライブラリと ffmpeg のライセンスとソースの入手先を表示
- **ホットキーの試し**(v0.4.0): KeyFree のために、キーの組み合わせを専用のスレッドで一瞬だけ登録して外す。試しの登録は終わり方によらず必ず外す

> **開発状況: v0.4.1(実機確認中)**
> 単体テストと自己検査は通っていますが、電源プラン・音量の切り替え、ウィンドウの移動、実際のクリップボードの記録、
> 写真・動画・PDF の処理やごみ箱送り、ドライブの抜き差し(PlugSave)など、実機で物を動かす操作の確認はまだ途中です。
> 常駐の4モジュールは試運転(予定を表示するだけ)から始まるので、確認してから本番に切り替えてください。

動作環境: Windows 10 / 11(64bit)。exe で使う場合は Python 不要。

## 使い方(exe)

ビルド済みの exe は [Releases](https://github.com/takosasi-dev/deskkit/releases/latest) から `DeskKit.exe` をダウンロードする。

1. `DeskKit.exe` をダブルクリック(インストール不要・管理者権限不要)。初回は Control Center が開く。
2. 使いたいモジュールをオンにして、各画面で設定する。
3. ウィンドウを閉じてもトレイで動き続ける。トレイアイコンのクリックで画面が開く。終了はトレイの「終了」。
4. 「設定」画面で Windows 起動時の自動起動を切り替えられる。

コマンドライン(起動中の DeskKit へ転送される):

```
DeskKit.exe mode --list            ModeShift のモード一覧
DeskKit.exe mode <名前> --dry-run  モードの実行計画を表示
DeskKit.exe dropsort status        DropSort の状態
DeskKit.exe sendprep open a.jpg b.mp4
                                   SendPrep にファイルを渡す(起動していなければ起動してから。最大 200 個)
DeskKit.exe pagepress open a.pdf b.jpg
                                   PagePress の「まとめる」に足す(mojifix open・cliptrim open も同じ形)
DeskKit.exe keyfree check Ctrl+Alt+Shift+K
                                   その組が空いているか(0 空き / 20 使用中 / 21 DeskKit / 22 調べられない)
DeskKit.exe eyebreak status        EyeBreak の状態
DeskKit.exe startupwatch list      自動起動の一覧
DeskKit.exe --selftest all         自己検査
DeskKit.exe --quit                 常駐を終了
```

終了コード: 0 正常 / 2 未対応 / 10 DeskKit が起動していない(`open` は起動を 15 秒待っても繋がらない) / 11 モジュールが無効・停止中 / 12 応答なし。モジュール固有のコードは各仕様書のとおり。

## データの置き場所

| 種類 | 場所 |
|---|---|
| 設定 | `%APPDATA%\DeskKit\settings.json`(GUI と同じ内容。手で編集したらトレイの「設定を再読み込み」) |
| データ | `%LOCALAPPDATA%\DeskKit\<モジュール>\` |
| ログ | `%LOCALAPPDATA%\DeskKit\logs\`(本文・URL・ウィンドウタイトルは書かない) |

アンインストールは exe を消し、上の2フォルダと、自動起動をオンにしていた場合は画面でオフにする(または `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` の `DeskKit` を消す)。

## この道具がしないこと

- 通信するのはアップデートの確認とダウンロード(GitHub、HTTPS)だけ。設定でオフにできる。それ以外は同じ PC 内の名前付きパイプだけ(v0.3.0・v0.4.0 のモジュールも通信しない)
- キーボード/マウスのフックをしない(ホットキーは `RegisterHotKey` のみ。KeyFree の調べ方も同じで、登録はすぐ外す。EyeBreak は「最後の入力からの時間」だけを読む)
- 管理者権限を求めない。ゲームのメモリ・入力に触れない。ゲームや全画面アプリが前面にある間は、前面に作用する操作をしない
- ファイルを恒久削除しない(ClipShelf の履歴の保持上限・全消去だけは例外)。TwinSweep・PcCheckup が消すのは、確認のあとごみ箱へ送るものだけ。ごみ箱の無いドライブ(USB メモリ・ネットワーク)のファイルは送らずに残す
- 元のファイルを書き換えない(SendPrep・PagePress・MojiFix・ClipTrim は必ず別のファイルに書き出す。JotDrop はファイルの末尾に足すだけ。
  PlugSave はバックアップ先のファイルを消さない。MojiFix の濁点の直しは、一覧で確かめてからファイルの名前だけを変える)
- 自動起動の登録を書き換えない(StartupWatch は見て知らせるだけ)

## 開発

```
python -m venv .venv
.venv\Scripts\pip install -e . pyinstaller pytest ruff mypy   # pyproject.toml の依存(PySide6・Pillow・pi-heif・numpy・psutil・winrt・pypdf・pypdfium2)も入る
.venv\Scripts\python -m deskkit                 # 起動
.venv\Scripts\python -m deskkit --selftest all  # 自己検査(host + 15モジュール)
.venv\Scripts\python -m pytest                  # 単体テスト(実機依存は -m win32_real)
.venv\Scripts\python -m ruff check .
powershell -ExecutionPolicy Bypass -File build.ps1   # dist\DeskKit.exe を作る(下の ffmpeg の準備が要る)
```

exe のビルドには ffmpeg の配布物が要る(ビルドの中で自動ダウンロードはしない)。
[BtbN/FFmpeg-Builds の Releases](https://github.com/BtbN/FFmpeg-Builds/releases) から `ffmpeg-n8.1-latest-win64-lgpl-8.1.zip`(**LGPL 版**)と `checksums.sha256` を
`third_party\ffmpeg\` に置く(git には入れない)。`build.ps1` が SHA-256 を照合し、`tools\make_ffmpeg_bundle.py` で exe に入れる形(`deskkit\_bundled\ffmpeg.zip`)を作る。

構成:

```
deskkit/            host(bootstrap・settings・tray・nativewin・hotkeys・foreground・events・ipc・autostart・loader)
deskkit/ui/         Control Center・共通ウィジェット・テーマ・通知トースト
deskkit/modules/    modeshift / dropsort / layoutkeep / clipshelf / sendprep / pccheckup / twinsweep /
                    eyebreak / jotdrop / keyfree / startupwatch / pagepress / mojifix / cliptrim / plugsave(互いに import しない。ctx だけを使う)
deskkit/ffmpeg.py   ffmpeg の同梱物の展開と照合(SendPrep・ClipTrim が共有)
overlaykit/         OverlayKit の HotkeyRegistry 部分の最小実装(仕様書 §9.1 と同じ形)
tools/              probe.py(共通 §9.6)・lk_diag.py(LayoutKeep §9.6)・clip_formats.py(ClipShelf §8.1)・screenshot.py・make_icon.py
                    make_ffmpeg_bundle.py(ffmpeg の同梱物)・make_third_party_licenses.py(THIRD_PARTY_LICENSES.txt)
docs/MODULE_GUIDE.md  モジュール実装者向けの ctx API と今回の既定値
```

仕様書からの主な変更点は `docs/MODULE_GUIDE.md` §0 を参照。
特に共通仕様 C-6「ネットワーク接続をしない」は、**自動更新(`deskkit/updater.py`)だけを例外** にしている(接続先は GitHub の許可リストのみ、設定でオフ可)。

## 同梱しているライブラリとライセンス

DeskKit.exe には次のソフトウェアが入っている。全文とソースの入手先は `THIRD_PARTY_LICENSES.txt`(アプリの設定画面の「ライセンス」からも開ける)。

| ソフトウェア | 版 | ライセンス | 使いどころ |
|---|---|---|---|
| Qt 6 / PySide6 | 6.11.2 | LGPL v3 | 画面全体 |
| FFmpeg(BtbN の LGPL ビルド) | n8.1.3-20260924 | LGPL v3 | SendPrep・ClipTrim の動画の処理(別プロセスとして起動するだけ。GPL の部品は含まない) |
| Pillow | 12.3.0 | MIT-CMU(同梱のライブラリはそれぞれ) | 画像の読み書き |
| pi-heif(libheif・libde265) | 1.4.0 | BSD-3-Clause(libheif・libde265 は LGPL v3) | HEIC の読み込み(読み込み専用) |
| NumPy | 2.5.3 | BSD-3-Clause ほか | 似た写真の計算 |
| psutil | 7.2.2 | BSD-3-Clause | PC の状態(プロセス・ディスク) |
| PyWinRT | 3.2.1 | MIT | Windows の文字認識・ネットワークの状態 |
| pypdf | 6.19.0 | BSD-3-Clause | PagePress の PDF の結合・分割・回転 |
| pypdfium2 / PDFium | 5.13.0 / chromium 7999 | Apache-2.0 または BSD-3-Clause / BSD-3-Clause(PDFium に含まれるライブラリはそれぞれ) | PagePress の PDF の表示・画像化 |

ほかに Python(PSF License)と PyInstaller のブートローダー(GPL v2 + 例外。DeskKit には GPL の条件はかからない)を含む。

## ライセンス

DeskKit 本体は MIT License。`LICENSE` を参照。同梱しているソフトウェアのライセンスは上の節と `THIRD_PARTY_LICENSES.txt` を参照。
