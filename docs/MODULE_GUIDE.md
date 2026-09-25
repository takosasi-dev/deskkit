# DeskKit モジュール実装ガイド(実装者向け)

仕様書: DeskKit 仕様書(00_共通 + 各モジュール。リポジトリには含めない)。
この文書は仕様書と今回の実装方針の差分・host 側の実 API をまとめたもの。**仕様書と食い違う場合はこの文書が優先**(利用者が「全部まとめて作る」「フルGUI」「単体exe」「試運転(dry-run/observe)から」を選んだため)。

## 0. 今回の方針(利用者の決定)

- フェーズで止めずに全フェーズを実装する。§8(実物)が空の値は下の「既定値」で埋め、GUI から変えられるようにする。
- 設定 GUI を作る(仕様書の「v1 は GUI なし」「ルール編集 GUI を作らない」「レイアウト削除 UI を作らない」は撤回)。GUI は **できるだけ凝った作り**。
- 初期動作は仕様どおり試運転(dry-run / observe)。GUI のスイッチ1つで本番に切り替えられる。
- 単体 exe(PyInstaller onefile)で他人に配る。`L:\` などの個人的なパスをコードや既定値に書かない。
- 逸脱不可要件(INV)・C-1〜C-12 はそのまま守る(安全に関わるものは緩めない)。
- 例外: 利用者の指示で、host の自動更新(`deskkit/updater.py`)だけが GitHub に HTTPS で接続する(C-6 の例外)。**モジュールは引き続き一切通信しない。**

## 0.1 v0.2.0 の変更点

- 担当間の取り決め: `docs/INTERFACES_v0.2.md`
- モジュールごとの仕様書からの変更点・既定値・理由: `docs/v0.2/modeshift.md` / `dropsort.md` / `layoutkeep.md` / `clipshelf.md`
- 利用者の判断: ModeShift の操作記録(ops.jsonl)に残す URL はドメインだけ(ModeShift 仕様書 INV-11 と共通 C-12 の食い違いの解消)。

## 0.2 v0.3.0 の変更点

- 担当間の取り決め: `docs/INTERFACES_v0.3.md`。共通仕様: `DeskKit_v0.3_追加モジュール共通_仕様書.md`(V-* / H-* / VINV-* / NFR-*)。
- 追加モジュール: SendPrep(共有前クリーナー)・PcCheckup(PC 不調の診断)・TwinSweep(似た写真の整理)。「困ったときに開いて1回で片づける」道具で、
  利用者が操作したときだけ動く(自動で動くのは PcCheckup の「空き容量の見張り」だけ、既定オフ)。仕様書からの変更点・実測は `docs/v0.3/<モジュール>.md`。
- **ごみ箱へ送る処理は `deskkit.fileops.recycle` だけ**を使う(V-6・VINV-2)。`os.remove` / `unlink` / `shutil.rmtree` / `send2trash` で利用者のファイルを消さない。
  `recycle(paths, parent_hwnd=None)` → `RecycleResult`(`sent` / `skipped` / `sent_count` / `count(reason)`)。理由コードは
  `skipped_no_recycle_bin`(固定ディスク以外。ファイルは残る)/ `not_found` / `in_use` / `aborted`(恒久削除の確認で「いいえ」)/ `failed`。
  1件ずつ `SHFileOperationW` を呼ぶので遅い。**GUI スレッドの外から呼び**、結果は `ctx.call_soon` で画面へ返す。テストは `api=` に `RecycleApi` の偽物を渡す。
- 追加ライブラリ: Pillow 12.3 / pi-heif 1.4 / numpy 2.5 / psutil 7.2 / winrt 3.2.1(`winrt.windows.media.ocr` など)。**`create()` と import 時には読まず、
  最初に使う関数の中で import する**(NFR-5)。exe には deskkit.spec が名前で入れている(新しい winrt の名前空間を使うときは spec の `WINRT_NAMESPACES` に足すよう本体担当へ依頼する)。
- ffmpeg(SendPrep だけ): 同梱物は `deskkit/_bundled/ffmpeg.zip`(LZMA、中身は `ffmpeg.exe` と `LICENSE.txt`)と `ffmpeg.sha256`(ffmpeg.exe の SHA-256、16進小文字 + 改行)。
  exe では `Path(sys._MEIPASS) / "deskkit" / "_bundled"`、ソース実行では `Path(deskkit.__file__).parent / "_bundled"`。開発時は `tools/make_ffmpeg_bundle.py` で作る
  (`third_party/ffmpeg/` に BtbN の `ffmpeg-n8.1-latest-win64-lgpl-8.1.zip` と `checksums.sha256` が要る。どちらも git 管理外)。
- CLI `DeskKit <module> open <paths...>`: host が起動していなければ本体が起動してから転送する(最大 15 秒)。モジュールの `handle_cli(["open", *paths])` は
  パスを積んで `ctx.show_page()` を呼び、`(0, "queued N")` を返す。1回で運べるのは 200 個・合計 32,000 文字まで(本体の IPC は 1MB まで受ける)。
  この経路で起動された host は画面を自分では開かない(モジュールの `show_page()` が開く)。受け取るモジュールが無効なら、本体が警告の通知を出して 11 を返す。
- ログ・操作記録・`diagnostics()` にファイル名・フォルダのパス・SSID・文字認識で読んだ文字列を書かない(V-7・VINV-4。v0.2 の「本文・URL」より広い)。
  画面には出してよい(V-8)。
- テーマ: 3モジュールのライト用アクセント色とグラフの色は `deskkit/ui/theme.py`(7 色で dataviz の検証を両テーマで PASS)。モジュールは従来どおり
  `catalog.info(name).accent` と `theme.chart_color(name)` を実行時に読むだけ。
- 本体の設定画面に「ライセンス」(同梱の `THIRD_PARTY_LICENSES.txt` を表示。作り直しは `tools/make_third_party_licenses.py`)。ライブラリを足したら本体担当へ知らせる。
- テストの `DESKKIT_HOME` を一時フォルダに向けた host は、IPC の名前も分かれる(本番の常駐に繋がない)。

## 1. パッケージ構成

```
deskkit/modules/<name>/
  __init__.py      def create(ctx) -> Module を公開(重い import はここでしない)
  __main__.py      python -m deskkit.modules.<name> --selftest
  selftest.py      def run() -> int  (0=合格/1=不合格。host の `DeskKit.exe --selftest <name>` からも呼ぶ)
  page.py          Control Center 用の画面(QWidget)
  ...              仕様書 §2 のファイル群
tests/modules/<name>/   pytest(偽 Win32 で。実機依存は @pytest.mark.win32_real)
```

- 各ソースファイル先頭に責務を3行以内の日本語コメントで書く。
- 他モジュールを import しない(C-1)。host の内部(`deskkit.host`, `deskkit.loader`, `deskkit.win32`)も import しない。
  使ってよい host 側: `deskkit.context`(型)、`deskkit.ui.*`(GUI 部品)、`deskkit.hotkeys`(parse_hotkey/format_hotkey)、`deskkit.catalog`(自分のアクセント色)、`deskkit.foreground.ForegroundInfo`(型)、`deskkit.usage`(§7)、
  (v0.3)`deskkit.fileops`(`recycle` / `RecycleResult` / `RecycleApi`。ファイルをごみ箱へ送るのはこれだけ。§0.2)。
- Win32 は各モジュールの `_win32.py` に ctypes で書き、`Protocol` の背後に置いてテストで偽物に差し替える。argtypes/restype を必ず設定する(64bit で HANDLE が切れるのを防ぐ)。

## 2. Module の形

```python
class Module:
    def start(self) -> None
    def stop(self) -> None
    def handle_cli(self, args: list[str]) -> tuple[int, str]      # 不要なら (2, "unsupported")
    def create_page(self) -> QWidget                               # 任意だが今回は全モジュール実装する
```

- `create(ctx)` で設定を読み、**足りないキーは自分の既定値で補って** `ctx.write_settings(section)` で書き戻してよい(既定値はモジュール側に持つ。host は知らない)。
- 設定が不正なら `create`/`start` で例外を投げれば host がそのモジュールだけ「停止中(理由)」にする。ルール単位の不正は仕様どおりそのルールだけ無効化して通知。
- `stop()` ではスレッドを止める。ホットキー・購読・タイマー・トレイ項目は host が片付ける。
- `create_page()` は start 後に呼ばれる。画面は何度でも作り直される前提(モジュールが再起動されると新しいページを要求される)。

## 3. ModuleContext(`deskkit/context.py` の ModuleContextImpl が実物)

| API | 内容 |
|---|---|
| `ctx.name`, `ctx.data_dir`, `ctx.log` | 名前 / `%LOCALAPPDATA%\DeskKit\<name>\` / logger `deskkit.<name>` |
| `ctx.settings()` | 自分のセクションの読み取り専用コピー(`modules.<name>` の中身。`enabled` を含む) |
| `ctx.settings_dict()` | 書き換え用の深いコピー |
| `ctx.write_settings(section, restart=False)` | 自分のセクションを書く(原子的)。`restart=True` でモジュールを stop→start し直す(ページも作り直される)。settings.json が構文エラーなら `deskkit.settings.SettingsError` |
| `ctx.game_processes()` | 共通のゲーム exe 名(小文字)の frozenset |
| `ctx.list_modes() -> list[tuple[str, str]]` | (v0.2)ModeShift の設定にあるモードの `(name, label)` 一覧。host が settings の `modeshift.modes[*]` を読むだけ(ModeShift が無効でも返す。name が空・重複・壊れた要素は飛ばし、label が無ければ name) |
| `ctx.is_snoozed() -> bool` | (v0.2)一時停止中なら True。**自動で動く処理の入口**で見る(手で押した操作・クイックアクション・CLI は止めない)。どのスレッドからでも呼べる(QUNS の問い合わせは 2 秒キャッシュ)。テスト用の Fake ctx には各自 `is_snoozed()`(既定 False)を足す |
| `ctx.add_tray_action(label, cb, checkable=False, checked=False, submenu=None) -> TrayItem` | トレイの自モジュールのサブメニューに項目。`submenu="モード"` で入れ子。`TrayItem.set_text/set_checked/set_enabled/set_visible` |
| `ctx.add_tray_separator(submenu=None)` / `ctx.clear_tray_actions(submenu=None)` | 区切り / 作り直し用の全消去 |
| `ctx.set_tray_status(text)` | サブメニュー先頭の状態行。Control Center のカードにも出る |
| `ctx.notify(title, text, on_click=None, level="info")` | 通知トースト(level: info/ok/warn/error)。on_click はクリック時のみ。(v0.2)ゲーム・全画面の間は error 以外を host が保留し、あとでまとめて出す(§9.1)。「最近の通知」の行クリックでは、10 分以内でモジュールが動いていれば on_click、それ以外は自分の画面が開く |
| `ctx.hotkeys.register_text(name, "Ctrl+Shift+Space") -> bool` | 表記で登録。空/None なら何もしない。競合・解釈不能は False(host が起動時にまとめて通知する。FR-9) |
| `ctx.hotkeys.register(name, mods, vk)` | 数値で登録。競合は `overlaykit.HotkeyConflictError` |
| `ctx.hotkeys.triggered(name).connect(cb)` / `ctx.hotkeys.unregister(name)` | 押されたときのコールバック(safe 済み) |
| `ctx.on_native(msg, handler(wparam, lparam))` | 隠しウィンドウのメッセージ購読(WM_DISPLAYCHANGE=0x007E, WM_POWERBROADCAST=0x0218, WM_CLIPBOARDUPDATE=0x031D) |
| `ctx.hidden_hwnd()` | host の隠しウィンドウ(クリップボードの OpenClipboard の所有者などに使う) |
| `ctx.foreground() -> ForegroundInfo` | `hwnd pid exe is_game is_fullscreen is_elevated` と `unsafe_for_input()`、`reason()`(game/fullscreen/elevated/...)。全画面判定は host が矩形一致で実装済み(False/True が返る) |
| `ctx.emit(event, payload)` / `ctx.on(event, handler)` | 登録済みイベント: `layout.apply` / `layout.applied` / `modeshift.switched` /(v0.2)`modeshift.reverted` / `host.snooze_changed`。payload は `docs/INTERFACES_v0.2.md` §2 |
| `ctx.safe(fn, label) -> wrapped` | 例外を捕まえてログし、連続 N 回でモジュールを停止中にするラッパー。**自分のウィジェットのシグナル→モジュール処理の接続や、Qt 仮想メソッド(keyPressEvent 等)の中身は必ずこれか try/except で包む**。(v0.3)ログには例外の型名と DeskKit のソースのファイル名・行番号だけを書き、例外の文(`str(e)`。OSError のパスなど)は書かない。`log.exception(...)` も本体のログ書式で同じ扱いになる(`deskkit.logging_setup.describe_exception`)。ただし自分で `log.error("...%s", e)` のように文を埋め込むと書かれてしまうので、しないこと |
| `ctx.call_soon(fn)` | 任意スレッド→メインスレッドで fn を実行(監視スレッドから使う) |
| `ctx.start_timer(ms, cb, single_shot=False) -> QTimer` | safe 済みタイマー(停止時に host が止める) |
| `ctx.dpi_awareness()` | `"per_monitor_aware_v2"` 等 |
| `ctx.add_quick_action(label, cb, keywords="", glyph=None, enabled=None)` / `ctx.clear_quick_actions()` | クイックアクション(Ctrl+Alt+Space の検索窓)に操作を足す。**トレイ項目は自動で候補に入る**ので、トレイに無い操作(個別のモードのプレビュー、特定レイアウトの適用など)だけ足す |
| `ctx.show_page()` | Control Center を開いて自分の画面を表示する(トレイ項目・通知クリックから) |
| `ctx.window_parent()` | ダイアログの親(Control Center が表示中ならそれ、無ければ None) |

## 4. GUI(凝った作りにする)

- 部品は `deskkit/ui/widgets.py`、色とアイコン字形は `deskkit/ui/theme.py`(`G.*` は Segoe Fluent Icons)。**色は直書きせず theme の定数とモジュールのアクセント色(`deskkit.catalog.info(name).accent`)を使う**。
- ページの定番構成: `ScrollPage` の中に `Hero(title, tagline, glyph, accent)`(状態ピル・主要ボタンを載せる)→ `StatTile` の横並び → 機能ごとの `Card`(`SettingRow` + `ToggleSwitch` / `Segmented` / `HotkeyEdit` / `StringListEditor`)→ 表(QTableWidget)や履歴。
- 確認は `widgets.confirm(...)`、お知らせは `widgets.message(...)`、入力は `widgets.text_input(...)`(いずれも枠なし・影付き・フェードイン)。
- 試運転(dry-run/observe)と本番の切替は `Segmented` でページ上部に目立たせ、試運転中は `StatusPill("試運転", "warn")` を出す。
- 設定を変える操作は即 `ctx.write_settings(...)` で保存し、必要なら `restart=True`。保存成功は控えめなトースト or ピル表示で知らせる。
- 独立ウィンドウ(ClipShelf のパレット、ModeShift のプレビュー等)は枠なし+角丸+影+フェード/スライドのアニメーション。`Qt.WindowType.Tool | FramelessWindowHint | WindowStaysOnTopHint`、`WA_TranslucentBackground`。
- 画面に出してよい情報と、ログ・ファイルに書いてよい情報の区別(C-12, 各 INV)を守る。GUI で本文を表示するのは ClipShelf のパレット内だけ。
- 日本語 UI。ボタン文言は動詞で短く。

## 5. 既定値(§8 が空のため今回決めた値。GUI で変えられる)

- 共通: ホットキーは ClipShelf の `open_palette = "Ctrl+Alt+V"` 以外は未割り当て(競合を避ける)。
- DropSort: `temp_extensions = [".crdownload", ".part", ".tmp", ".partial"]`、`stable_seconds = 5`、`full_scan_interval_s = 30`、`exec_extensions = [".exe",".scr",".com",".bat",".cmd",".ps1",".vbs",".js",".msi",".hta",".lnk"]`、`locked_retry_max = 10`、`archive = {enabled: false, mode: "dry-run", idle_days: 30, dir_name: "_archive"}`、`rules = []`(GUI に「テンプレートから追加」: PDF / 画像 / 動画 / 音声 / 圧縮 / インストーラ / Office 文書 など。移動先フォルダは利用者が選ぶ。存在しないフォルダは GUI で利用者が明示的に「作成」を押したときだけ作る)。新規ルールは `mode: "dry-run"`。`watch_mode: "rdcw"`。host_domain(FR-7)は Zone.Identifier に HostUrl があるときだけ効く形で実装してよい(ログ・画面にはドメイン名のみ)。
- ClipShelf: `mode = "observe"`、`hotkeys = {open_palette: "Ctrl+Alt+V", plain_text: "", toggle_pause: ""}`、`debounce_ms = 150`、`open_retry = 5`、`max_chars = 100000`、`retention = {max_items: 1000, max_days: 30}`、`exclude_exes = []`(推測で PM の exe 名を入れない)、`unknown_owner_policy = "skip"`、`auto_paste = "off"`。
- LayoutKeep: `mode = "dry_run"`、`auto_apply = false`、`debounce_ms = 2500`、`settle_checks = 2`、`max_wait_s = 20`、`signature.id_source = "device_interface"`(候補は GUI で選べる)、`targets = []`(GUI に「今開いているウィンドウから選ぶ」)、hotkeys 未割り当て。
- ModeShift: `modes = []`(GUI にモード編集画面と、アクション種別ごとの入力フォーム。電源プランは `powercfg /list` の GUID から選ぶドロップダウン)、`preview = "unconfirmed_only"`、`allow_force_kill = false`、`auto_switch = {enabled: false, poll_interval_s: null, rules: []}`。

## 6. 動作確認

- `.venv\Scripts\python -m deskkit.modules.<name> --selftest` が 0。
- `.venv\Scripts\python -m pytest tests/modules/<name>` が通る。
- `.venv\Scripts\python -m ruff check deskkit/modules/<name>` がエラー0(設定は pyproject.toml)。
- 仕様書 §11 の grep 系 AC(禁止 API が無いこと)を自分で実行して0件を確認。
- 本番環境のデータ(`%LOCALAPPDATA%\DeskKit`)を汚さない。テストは一時フォルダ、または環境変数 `DESKKIT_HOME` を一時フォルダに向ける。

## 7. 利用状況(任意の Module.usage)

```python
from deskkit.usage import UsageSeries, count_jsonl, count_dates
def usage(self, days: int) -> list[UsageSeries]: ...
```

- Control Center の「利用状況」ページが、動作中の各モジュールの `usage(days)`(days は 7/30/90)を呼んで棒グラフにする。
- 返すのは日ごとの件数だけ(古い日→今日)。本文・パス・タイトル等を入れない。自分の oplog/ops.jsonl や DB の日時列から数える(`count_jsonl` が使える)。
- 各モジュールで代表指標を1つ `primary=True` にし、撤退基準(仕様書 R-x)に関わる指標には `hint` にその基準を短く書く。
- 例外を投げても画面側で握りつぶすが、重い処理(全件復号など)はしないこと。
- (v0.2)**usage() は GUI スレッド以外(集計用のスレッド)から呼ばれる。** 例外を出したモジュールだけ、host が GUI スレッドでもう一度呼び直す
  (SQLite の接続のようにスレッドに縛られたものを使っていても結果は出るが、毎回ログに1行出る)。スレッドに依存しない読み方(ファイルを開き直す等)が望ましい。
  結果は期間ごとに 2 分覚えるので、同じ期間を開き直しても再集計しない。

## 8. テーマ

- `theme.set_mode("dark"|"light"|"system")` は起動時に1回だけ呼ばれ、theme の定数(`T.SURFACE` など)とモジュールのアクセント色(`catalog.info(name).accent`)がライト用に差し替わる。**色は必ず T.* と catalog から実行時に読む**(モジュールの import 時に定数へコピーしない。`"#FFFFFF"` 等の直書きもしない。塗りつぶしの上の文字は `T.ON_ACCENT`、濃い背景用の白は `T.HERO_GLYPH` を使う)。
- 確認方法: `set DESKKIT_THEME=light` にして `python tools/screenshot.py <出力先> <モジュール名>`。

## 9. v0.2 で本体が全体に効かせるもの(モジュールは何もしなくてよい)

契約は `docs/INTERFACES_v0.2.md`。ここは本体側の実装の要点と既定値。

### 9.1 通知の保留(H1)
- `ctx.notify` は、前面がゲーム(`game_processes`)・全画面(矩形判定が True)・Windows の通知状態(QUNS)が `running_d3d_full_screen` / `presentation_mode` の間、
  level が `error` 以外なら保留する。矩形で全画面か判定できない(`is_fullscreen is None`)ときは QUNS が `busy` なら保留、それ以外は出す。前面が DeskKit 自身なら保留しない。
- 保留中だけ 2 秒ごとに前面を確かめ、安全になったら1件にまとめて出す: 2件以上は「保留中の通知 N 件」(本文は新しい順に最新3件の「モジュール名: タイトル」、クリックで Control Center のホーム)、1件だけなら元の通知をそのまま。
- 「最近の通知」への記録は保留と関係なくすぐ行う。設定 `host.hold_notifications`(既定 true。設定画面の「ゲーム・全画面中の安全装置」にスイッチ)。

### 9.2 一時停止(H2)
- トレイの「一時停止」(30分 / 1時間 / 再開するまで / 再開)、ホーム、設定画面、クイックアクションから操作する。時間指定は時間になると自動で再開する。
- **状態はメモリだけ**に持つ(DeskKit を再起動すると解除される。止めたことを忘れて自動処理が止まりっぱなしになるのを避けるため)。
- 設定 `host.snooze_follow_quns`(既定 false):「Windows がプレゼン中・通知を控えている間も一時停止」。QUNS が `busy` / `running_d3d_full_screen` / `presentation_mode` の間は一時停止扱い(5 秒ごとに確認)。
- 状態が変わると `host.snooze_changed` `{"snoozed": bool, "until": str | None}` を送る(時間指定のときだけ until に ISO 8601。無期限・QUNS 連動は None)。トレイのツールチップ・アイコン(灰色のバッジ)・メニュー先頭・ホームに状態を出す。

### 9.3 設定の自動世代保存(H3)
- settings.json を書くたび(どの経路でも)、2 秒の間の連続した書き込みを1つにまとめて `%LOCALAPPDATA%\DeskKit\settings-history\settings-<日時>.json` に残す。起動時の内容も1世代目として残す。
- 直前の世代と同じ内容なら増やさない。画面の表示状態(`usage_period` / `usage_view` / `onboarded`)と `update.last_check` だけの違いも増やさない。
- 新しい方から `host.settings_history_keep`(既定 20、1〜200)世代を残し、古いものは消す(このフォルダの、この命名の DeskKit 自身のファイルだけ)。
- 設定画面の「設定の世代」で一覧(時刻 + `backup.summary()`)から戻せる。戻す前に今の設定も1世代残し、「設定を再読み込み」と同じく全モジュールを起動し直す。

### 9.4 診断レポート(H4)と Module.diagnostics()
- 設定画面とクイックアクションの「診断レポートをコピー」。版・exe/ソース・OS build・DPI・モニタ数と倍率・各モジュールの有効/状態/停止理由・ハンドラ例外の累計(起動から)・
  ホットキーの登録と登録失敗・アップデート設定と前回の確認・一時停止・世代の数を載せる。
- モジュールは任意で `def diagnostics(self) -> dict[str, str | int | bool]` を生やせる(動作中のときだけ呼ぶ。例外は「例外 <型名>」と載るだけ)。
  **本文・パス・URL・ウィンドウタイトル・exe 名を入れない**(件数・モード・真偽・理由コードだけ)。host も最後にパス・URL・exe 名・ユーザー名を伏せ字にするが、頼らないこと。
- 例外の回数のキーは `event:` / `native:` / `timer:` / `hotkey:` / `notify:` / `usage` / `handle_cli` / `call_soon` 始まりはそのまま、`tray:` / `quick:` は項目名(利用者のモード名など)を含むので `tray:*` にまとめる。

### 9.5 ホイール対策(UX-1)ほか
- アプリ全体のイベントフィルタで、フォーカスの無い `QAbstractSpinBox` / `QComboBox` / `QAbstractSlider`(スクロールバー以外)へのホイールを、いちばん近い親のスクロール領域へ回す。
  これらの部品の `WheelFocus` は `StrongFocus` に下げる(クリック・Tab でフォーカスしてから回せば値が変わる)。モジュールの対応は不要。
- ホームのホットキー一覧は、登録名から表示名を作る(`host.quick` などの既知の名前は日本語、`modeshift.mode.<name>` は `ctx.list_modes()` のラベル、それ以外は登録名の `_` を空白に)。
  わかりやすく出したいときは登録名を意味のある英単語にしておく。
- 利用状況の期間・表示の選択は `host.usage_period` / `host.usage_view` に残る。

### 9.6 本体の設定キー(v0.2 で追加。型・範囲の合わない値は起動時に既定値へ戻す)

| キー | 既定 | 内容 |
|---|---|---|
| `host.hold_notifications` | `true` | ゲーム・全画面中の通知の保留 |
| `host.snooze_follow_quns` | `false` | Windows の通知状態に連動して一時停止 |
| `host.settings_history_keep` | `20` | 設定の世代を残す数(1〜200) |
| `host.usage_period` / `host.usage_view` | `"30"` / `"chart"` | 利用状況の期間・表示 |
