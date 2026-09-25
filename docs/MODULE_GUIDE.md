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
  使ってよい host 側: `deskkit.context`(型)、`deskkit.ui.*`(GUI 部品)、`deskkit.hotkeys`(parse_hotkey/format_hotkey)、`deskkit.catalog`(自分のアクセント色)、`deskkit.foreground.ForegroundInfo`(型)。
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
| `ctx.add_tray_action(label, cb, checkable=False, checked=False, submenu=None) -> TrayItem` | トレイの自モジュールのサブメニューに項目。`submenu="モード"` で入れ子。`TrayItem.set_text/set_checked/set_enabled/set_visible` |
| `ctx.add_tray_separator(submenu=None)` / `ctx.clear_tray_actions(submenu=None)` | 区切り / 作り直し用の全消去 |
| `ctx.set_tray_status(text)` | サブメニュー先頭の状態行。Control Center のカードにも出る |
| `ctx.notify(title, text, on_click=None, level="info")` | 通知トースト(level: info/ok/warn/error)。on_click はクリック時のみ |
| `ctx.hotkeys.register_text(name, "Ctrl+Shift+Space") -> bool` | 表記で登録。空/None なら何もしない。競合・解釈不能は False(host が起動時にまとめて通知する。FR-9) |
| `ctx.hotkeys.register(name, mods, vk)` | 数値で登録。競合は `overlaykit.HotkeyConflictError` |
| `ctx.hotkeys.triggered(name).connect(cb)` / `ctx.hotkeys.unregister(name)` | 押されたときのコールバック(safe 済み) |
| `ctx.on_native(msg, handler(wparam, lparam))` | 隠しウィンドウのメッセージ購読(WM_DISPLAYCHANGE=0x007E, WM_POWERBROADCAST=0x0218, WM_CLIPBOARDUPDATE=0x031D) |
| `ctx.hidden_hwnd()` | host の隠しウィンドウ(クリップボードの OpenClipboard の所有者などに使う) |
| `ctx.foreground() -> ForegroundInfo` | `hwnd pid exe is_game is_fullscreen is_elevated` と `unsafe_for_input()`、`reason()`(game/fullscreen/elevated/...)。全画面判定は host が矩形一致で実装済み(False/True が返る) |
| `ctx.emit(event, payload)` / `ctx.on(event, handler)` | 登録済みイベント: `layout.apply` / `layout.applied` / `modeshift.switched` |
| `ctx.safe(fn, label) -> wrapped` | 例外を捕まえてログし、連続 N 回でモジュールを停止中にするラッパー。**自分のウィジェットのシグナル→モジュール処理の接続や、Qt 仮想メソッド(keyPressEvent 等)の中身は必ずこれか try/except で包む** |
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

## 8. テーマ

- `theme.set_mode("dark"|"light"|"system")` は起動時に1回だけ呼ばれ、theme の定数(`T.SURFACE` など)とモジュールのアクセント色(`catalog.info(name).accent`)がライト用に差し替わる。**色は必ず T.* と catalog から実行時に読む**(モジュールの import 時に定数へコピーしない。`"#FFFFFF"` 等の直書きもしない。塗りつぶしの上の文字は `T.ON_ACCENT`、濃い背景用の白は `T.HERO_GLYPH` を使う)。
- 確認方法: `set DESKKIT_THEME=light` にして `python tools/screenshot.py <出力先> <モジュール名>`。
