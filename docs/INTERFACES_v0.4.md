# v0.4.0 担当間の取り決め(契約)

v0.4.0 は本体(host)と、新しい8モジュール・PcCheckup への追加1つを並行して作る。担当をまたぐものはこの文書だけで決める。
ここに無いものを他の担当に期待しない。変えたくなったら自分で変えず、最終報告に「契約変更の提案」として書く。

仕様書(リポジトリ外。各担当のプロンプトでパスを渡す。作者の手元の仕様書フォルダ):
- 共通: `00_共通\DeskKit_v0.4_追加モジュール共通_仕様書.md`(V4-* / H4-* / V4INV-* / NFR4-*。§11 に後半4本の追記)。v0.3 共通(V-* / VINV-*)も引き続き効く
- 前半(2026-09-26 に書いた物): `EyeBreak\` / `JotDrop\` / `KeyFree\` / `StartupWatch\` / `PcCheckup\PcCheckup_SecureBoot証明書の確認_追加仕様書.md`
- 後半(2026-09-28 に書いた物): `PagePress\` / `MojiFix\` / `ClipTrim\` / `PlugSave\`

## 0. 共通

- 担当は自分の範囲のファイルだけを変更する(下の表)。v0.2・v0.3 の取り決め(`docs/INTERFACES_v0.2.md` §0・`docs/INTERFACES_v0.3.md` §0)はそのまま。
- 実装の手引きは `docs/MODULE_GUIDE.md`。**ここと MODULE_GUIDE に書いた API 以外の host 内部を import しない。**
- 各担当はテストを足し、`pytest`(自分の範囲)・`ruff check`・`mypy deskkit`(strict)・`python -m deskkit --selftest <自分>` を通してから終える。
- 仕様書から外れる点・実測値は `docs/v0.4/<担当>.md` に書く(MODULE_GUIDE・CHANGELOG・README は本体担当だけが編集する)。
- テストは本番のデータ(`%LOCALAPPDATA%\DeskKit`)を汚さない(`DESKKIT_HOME` を一時フォルダへ向ける。`tests/conftest.py`)。
- ソースは Write / Edit ツールで書く(シェルの heredoc で書かない)。画面を動かして確かめるときは、キー送信(SendKeys 等)をしない。
  撮るのは `tools/screenshot.py`(自分のウィンドウだけを撮る)で、依頼者が作業中の窓に触れない。
- `python3` を呼ばない(ストアの入口で固まる)。リポジトリの `.venv\Scripts\python.exe` を絶対パスで使う(worktree の中からも同じ venv)。

| 担当 | 範囲 |
|---|---|
| host | `deskkit/*.py`(modules 以外)・`deskkit/ui/**`・`deskkit/selftest*`・`overlaykit/**`・`tests/*.py`・`tests/overlaykit/**`(あれば)・`docs/MODULE_GUIDE.md`・`docs/INTERFACES_v0.2.md`・`docs/v0.4/measurements.md`・`docs/v0.4/readme_first.txt`・`deskkit.spec`・`build.ps1`・`THIRD_PARTY_LICENSES.txt`・`tools/**`・`pyproject.toml`・`README.md`・`CHANGELOG.md` |
| eyebreak / jotdrop / keyfree / startupwatch / pagepress / mojifix / cliptrim / plugsave | `deskkit/modules/<自分>/**`・`tests/modules/<自分>/**`・`docs/v0.4/<自分>.md` |
| pccheckup(Secure Boot の追加) | `deskkit/modules/pccheckup/**`・`tests/modules/pccheckup/**`・`docs/v0.4/pccheckup.md` |

1人の担当が2つの範囲を受け持つことがある(依頼文に書く)。そのときも範囲の外には触れない。

骨組み(`__init__.py` / `__main__.py` / `module.py` / `selftest.py`)は main に置いてある。各担当はそれを置き換える。

## 1. 本体が用意済みのもの(main にある。使うだけ)

### 1.1 `deskkit.ffmpeg`(SendPrep から移した。ClipTrim も使う)

```python
from deskkit import ffmpeg
mgr = ffmpeg.shared()                 # プロセスで1つ。展開先は %LOCALAPPDATA%\DeskKit\ffmpeg\<sha16>\
exe: Path = mgr.ensure(on_preparing)  # 初回は zip から展開(数秒)。展開するときだけ先に on_preparing() を呼ぶ
                                      # 失敗は ffmpeg.FfmpegError(code)。code: CODE_MISSING / CODE_BROKEN / CODE_DISK_FULL
mgr.state()                           # STATE_MISSING / STATE_NOT_EXTRACTED / STATE_EXTRACTED / STATE_VERIFY_FAILED(重い処理はしない)
mgr.check_h264(exe) -> bool           # h264_mf が使えるか(1回だけ確かめて覚える)。mgr.h264_state() は "untested"/"available"/"unavailable"
ffmpeg.arg_path(p) -> str             # "file:" + パス(- で始まる名前をオプションと取り違えない)
ffmpeg.CREATE_NO_WINDOW               # subprocess の creationflags に渡す
```

- **ensure() と check_h264() は GUI スレッドの外で呼ぶ**(展開と照合に数秒かかる)。同じ展開先の管理役どうしはプロセスの中で1つの鍵を共有する。
- テストでは `ffmpeg.FfmpegManager(一時フォルダ, bundle=lambda: 同じ形の zip があるフォルダ)` を作って使う(`tests/test_ffmpeg_shared.py` の `_bundle()` が手本)。
  本物の ffmpeg が要るテストは、`deskkit/_bundled/`(`tools/make_ffmpeg_bundle.py` で作る)が無ければ skip にする。
- 同梱物は ffmpeg.exe だけ(ffprobe は無い)。`libopenh264` は使わない。作り直しの H.264 は `h264_mf`。
- ffmpeg を呼ぶときは SendPrep と同じ安全策: 引数は配列で渡しシェルを通さない・入力の前に `-protocol_whitelist file,pipe`・パスは `arg_path()`・
  stderr の中身をログに書かない(終了コードだけ)・中止とタイムアウトで子プロセスを終わらせる。

### 1.2 catalog と色

`deskkit/catalog.py` に8モジュールを登録済み(表示名・一言説明・仮のアクセント色・字形・説明文)。色は仮で、本体担当が §2 H4-1 で決め直す。
モジュールは従来どおり `catalog.info(name).accent` と `theme.chart_color(name)` を実行時に読むだけ。

### 1.3 追加ライブラリ(venv に導入済み・pyproject に記載済み)

- pypdf 6.19(BSD-3-Clause、純 Python)/ pypdfium2 5.13(Apache-2.0 / BSD-3、PDFium のバイナリ入り)。**PagePress だけが使う。** PyMuPDF(AGPL)は使わない。
- 既存: Pillow / pi-heif / numpy / psutil / winrt(v0.3)。
- **モジュールの `create()` と import 時には読まない。最初に使う関数の中で import する**(v0.3 NFR-5)。exe に入れる設定は本体担当(§2 H4-11)。
- MojiFix・PlugSave・EyeBreak・JotDrop・KeyFree・StartupWatch・PcCheckup の追加は、標準ライブラリ・PySide6・ctypes・winreg だけで書く(V4-2)。
  ClipTrim は `deskkit.ffmpeg` と Pillow(コマの表示に使うなら)まで。

### 1.4 既にある ctx の仕組みで足りるもの(本体の変更は要らない)

- `DeskKit <module> open <paths...>`(v0.3 H-B)は全モジュールで使える。host が `handle_cli(["open", *paths])` を呼ぶ。受け取ったら `ctx.show_page()`。
- PlugSave のドライブの抜き差し: `ctx.on_native(0x0219, handler)`(WM_DEVICECHANGE)。host の隠しウィンドウはトップレベル(message-only ではない)なので
  DBT_DEVICEARRIVAL / DBT_DEVICEREMOVECOMPLETE のブロードキャストが届く。handler の `lparam` は DEV_BROADCAST_HDR へのポインタで、**handler の中でだけ**読める。
  届かない環境に備えて、仕様書が決める間隔でドライブの一覧を読み直してよい。
- ごみ箱: `deskkit.fileops.recycle`(v0.3)。

## 2. 本体が作るもの(host 担当)

v0.4 共通 §5 の H4-1〜H4-10 に、後半4本のための H4-11〜H4-13 を足す。

| # | 内容 |
|---|---|
| H4-1 | 15 モジュールのライト用アクセント色(`_LIGHT_MODULE_ACCENTS`)とダーク用のグラフの色(`_CHART_DARK`)、catalog のダーク用アクセント色を決め直す。dataviz の `validate_palette.js` で、catalog の順に隣り合う組を両モード・両方の面で確認する(v0.3 と同じ手順。グラフは1系列1グラフなので、15 色が同時に並ぶことはない)。PASS しない組があれば、どの組をどう妥協したかを `docs/v0.4/measurements.md` に書く |
| H4-2〜H4-5 | ホットキーの「試して外す」・一覧・書き方の変換(下の §2.1 の形で出す) |
| H4-6 | `ctx.notify(title, text, on_click=None, level="info", replace_key=None)`(§2.2) |
| H4-7 | ホットキー一覧の表示名に `jotdrop.open_input` →「一行メモを書く」 |
| H4-8 | `docs/INTERFACES_v0.2.md` のイベントの受け手に EyeBreak(`modeshift.switched` / `modeshift.reverted`)を足す。vault の共通基盤 §9.4 は統合担当が直す |
| H4-9 | MODULE_GUIDE に「v0.4.0 の変更点」の節: §2.1・§2.2 の API、`deskkit.ffmpeg`(§1.1)、pypdf / pypdfium2、V4-1 の「利用者が書いた本文」、使ってよい host 側に `deskkit.ffmpeg` を足す |
| H4-10 | selftest の `all` に8モジュール(骨組みの時点でも通る) |
| H4-11 | `deskkit.spec` に pypdf と pypdfium2(`pypdfium2_raw` の pdfium.dll を含む)を入れる。`THIRD_PARTY_LICENSES.txt` に pypdf・pypdfium2・PDFium(と PDFium が同梱する物のライセンス。wheel の中の licenses を確かめる)を足す(`tools/make_third_party_licenses.py` を直して作り直す) |
| H4-12 | 版数 0.4.0、CHANGELOG(v0.4.0 の節。8モジュール・PcCheckup の追加・H4-2〜H4-6・ffmpeg の共通化)、README、`docs/v0.4/readme_first.txt`(はじめにお読みください.txt の文案) |
| H4-13 | NFR4-1・NFR4-2 のソース実行での計測と、exe の大きさの見込み(pdfium の分)を `docs/v0.4/measurements.md` へ。exe のビルドと実測は統合担当が統合の後に行う |

### 2.1 ホットキー(H4-2〜H4-5。KeyFree 仕様書 §2「本体に頼むこと」の (a)〜(g) が正)

型は `deskkit/hotkeys.py` に置く(モジュールは `deskkit.hotkeys` から import してよい。MODULE_GUIDE §1)。

```python
@dataclass(frozen=True)
class ProbeResult:
    mods: int            # MOD_ALT=1 / MOD_CONTROL=2 / MOD_SHIFT=4 / MOD_WIN=8 の和(MOD_NOREPEAT は含めない)
    vk: int
    state: str           # "free" | "used"(GetLastError 1409)| "error"(それ以外)| "deskkit"(registry 自身が持つ組。試さない)
    error: int = 0       # state == "error" のときの GetLastError

@dataclass(frozen=True)
class ProbeBatch:
    results: list[ProbeResult]
    pressed: list[tuple[int, int]]   # このバッチの間に押された組(mods, vk)。WM_HOTKEY を取り出した物

@dataclass(frozen=True)
class ProbeDone:
    reason: str          # "finished" | "cancelled" | "error" | "release_failed" | "busy"
    checked: int         # 試し終えた組の数
    error: int = 0       # release_failed / error のときの GetLastError(無ければ 0)

class ProbeHandle(Protocol):
    def cancel(self) -> None: ...        # どのスレッドからでも呼べる。次の組の前で止まる
    @property
    def done(self) -> bool: ...

@dataclass(frozen=True)
class HeldKey:
    name: str; mods: int; vk: int        # 登録名(host.quick / jotdrop.open_input など)

@dataclass(frozen=True)
class FailedKey:
    name: str; mods: int; vk: int; error: int

@dataclass(frozen=True)
class HotkeySnapshot:
    held: list[HeldKey]
    failed: list[FailedKey]

# ctx.hotkeys に足すもの
def probe(self, combos: Sequence[tuple[int, int]], on_batch: Callable[[ProbeBatch], None],
          on_done: Callable[[ProbeDone], None], batch: int = 16) -> ProbeHandle: ...
def snapshot(self) -> HotkeySnapshot: ...
def format(self, mods: int, vk: int) -> str: ...          # register_text が受け付ける書き方("Ctrl+Alt+Space")
def parse(self, text: str) -> tuple[int, int] | None: ...  # 解釈できなければ None
```

- `on_batch` と `on_done` は GUI スレッドで呼ぶ(ctx の safe で包む)。同時に2本目の probe は、試さずにすぐ `on_done(ProbeDone("busy", 0))`。
- `probe` が終わる前にモジュールが止まったら(stop)、host が cancel して預かりを0に戻す。

### 2.2 通知の置き換えの鍵(H4-6)

`ctx.notify(title, text, on_click=None, level="info", replace_key=None)`。今までの呼び方はそのまま動く。
保留中に同じ `replace_key`(モジュールの中で一意の文字列。例 `"eyebreak.prompt"`)の通知が来たら、古い方を捨てて新しい方だけを残す。
保留していないときは普通に出す(置き換えは保留の中だけ)。「最近の通知」には従来どおり両方が載る。

## 3. モジュールから本体への依存(これ以外に期待しない)

- `ctx.*`(MODULE_GUIDE §3 と上の §2.1・§2.2)・`deskkit.ui.*`・`deskkit.catalog`・`deskkit.usage`・`deskkit.fileops`・`deskkit.hotkeys`(型と parse/format)・`deskkit.ffmpeg`(§1.1)。
- テストの偽 ctx は各担当が自分のテストの中に作る。§2.1・§2.2 の API は、本体担当の実装を待たずに偽物で書いてよい(KeyFree・EyeBreak・JotDrop)。
- イベントは新しく足さない。EyeBreak が `modeshift.switched` / `modeshift.reverted` を `ctx.on` で受けるのは既存のイベント(v0.2 §2)。
- 一時停止(`ctx.is_snoozed()`)は、自動で動く処理の入口で見る(EyeBreak の声かけ・StartupWatch の知らせ・PlugSave の自動開始と「N 日していません」の知らせ)。

## 4. 波

同時に走らせるのは4本まで。

| 波 | 担当 |
|---|---|
| 1 | host / eyebreak + startupwatch / jotdrop + pccheckup / pagepress |
| 2 | keyfree(host の統合の後。§2.1 の本物で確かめる)/ mojifix / cliptrim / plugsave |

worktree はリポジトリの外に担当ごとに1つ、ブランチは `w4/<担当>`。
