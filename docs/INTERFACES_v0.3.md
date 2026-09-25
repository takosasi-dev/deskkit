# v0.3.0 担当間の取り決め(契約)

v0.3.0 は本体(host)と新しい3モジュール(sendprep / pccheckup / twinsweep)を4人で並行して作る。担当をまたぐものはこの文書だけで決める。
ここに無いものを他の担当に期待しない。変えたくなったら自分で変えず、最終報告に「契約変更の提案」として書く。

仕様書(リポジトリ外。各担当のプロンプトに全文を渡す):
- 共通: `DeskKit_v0.3_追加モジュール共通_仕様書.md`(V-* / H-* / VINV-* / NFR-*)
- `SendPrep_共有前クリーナー_仕様書.md` / `PcCheckup_PC不調診断_仕様書.md` / `TwinSweep_似た写真の整理_仕様書.md`

## 0. 共通

- 担当は自分の範囲のファイルだけを変更する(下の表)。v0.2 の取り決め(`docs/INTERFACES_v0.2.md` §0)の逸脱不可要件はそのまま。
- 実装の手引きは `docs/MODULE_GUIDE.md`(ctx の API・GUI の作法・テーマ・usage・diagnostics)。**ここに書いた API 以外の host 内部を import しない。**
- 各担当はテストを足し、`pytest`・`ruff check`・`mypy`(strict)・`python -m deskkit --selftest <自分>` を通してから終える。
- 仕様書から外れる点・実測値は `docs/v0.3/<担当>.md` に書く(MODULE_GUIDE.md・CHANGELOG・README は本体担当だけが編集する)。
- テストは本番のデータ(`%LOCALAPPDATA%\DeskKit`)を汚さない(`DESKKIT_HOME` を一時フォルダへ向ける。`tests/conftest.py` を参照)。

| 担当 | 範囲 |
|---|---|
| host | `deskkit/*.py`(modules 以外)・`deskkit/ui/**`・`deskkit/selftest*`・`tests/*.py`・`docs/MODULE_GUIDE.md`・`deskkit.spec`・`build.ps1`・`THIRD_PARTY_LICENSES.txt`・`pyproject.toml`・`README.md`・`CHANGELOG.md` |
| sendprep / pccheckup / twinsweep | `deskkit/modules/<自分>/**`・`tests/modules/<自分>/**`・`docs/v0.3/<自分>.md` |

骨組み(`__init__.py` / `__main__.py` / `module.py` / `selftest.py`)は main に置いてある。各担当はそれを置き換える。

## 1. 本体が用意済みのもの(main にある。使うだけ)

### 1.1 `deskkit.fileops`(共通 V-6・H-2〜H-4)

```python
from deskkit.fileops import recycle, RecycleResult
r: RecycleResult = recycle(paths: Sequence[Path], parent_hwnd: int | None = None)
r.sent: list[Path]; r.skipped: list[tuple[Path, reason]]; r.sent_count; r.count(reason)
# reason: "skipped_no_recycle_bin" | "not_found" | "in_use" | "aborted" | "failed"
```

- 1件ずつ `SHFileOperationW` で送る。遅いので **GUI スレッドの外から呼ぶ**(結果は `ctx.call_soon` で画面へ返す)。
- テストでは `recycle(paths, api=偽物)` の `api` に `RecycleApi`(`drive_type` / `delete_to_recycle_bin` / `exists`)の偽物を渡す。本物のごみ箱を使うテストは `@pytest.mark.win32_real` にする。
- `parent_hwnd` には `ctx.window_parent()` の `winId()` を渡すと、恒久削除の確認がその画面の前に出る(None でもよい)。

### 1.2 catalog

`deskkit/catalog.py` に3モジュールを登録済み(表示名・一言説明・アクセント色・字形)。ライト用の色とグラフの色は本体担当が `deskkit/ui/theme.py` に足す。

### 1.3 追加ライブラリ(venv に導入済み・pyproject に記載済み)

Pillow 12.3 / pi-heif 1.4(`pi_heif.register_heif_opener()`)/ numpy 2.5 / psutil 7.2 / winrt 3.2.1(`winrt.windows.media.ocr`・`winrt.windows.graphics.imaging`・`winrt.windows.storage.streams`・`winrt.windows.globalization`・`winrt.windows.foundation(.collections)`・`winrt.windows.networking.connectivity`)。
**モジュールの `create()` と import 時には読まない。最初に使う関数の中で import する(NFR-5)。** mypy は `ignore_missing_imports = true`。

### 1.4 ffmpeg(sendprep だけが使う)

- 開発時: `third_party/ffmpeg/ffmpeg-n8.1-latest-win64-lgpl-8.1.zip`(BtbN 配布物そのもの。SHA-256 は同じフォルダの `checksums.sha256` で照合済み。git には入れない)。
- 配布時の形(本体担当がビルドで作る): `deskkit/_bundled/ffmpeg.zip`(LZMA、中身は `ffmpeg.exe` と `LICENSE.txt` の2つだけ)と、その中の `ffmpeg.exe` の SHA-256 を書いた `deskkit/_bundled/ffmpeg.sha256`(1行、16進小文字)。exe の中では `Path(sys._MEIPASS) / "deskkit" / "_bundled"`、ソース実行では `Path(deskkit.__file__).parent / "_bundled"`。
- sendprep は `deskkit/modules/sendprep/video.py` に `bundled_dir() -> Path` を持ち、上の2か所を順に探す。見つからないときは「動画の部品が見つかりません」(FR-16 と同じ扱い)。
- 開発・テスト用に、本体担当は `tools/make_ffmpeg_bundle.py` を作る(`third_party` の zip から `deskkit/_bundled/ffmpeg.zip` と `.sha256` を作る。`deskkit/_bundled/` は `.gitignore`)。**sendprep 担当はこれを待たずに、テストでは一時フォルダに同じ形の zip を自分で作ってよい**(`third_party` の zip から ffmpeg.exe を取り出す。無い環境では動画のテストを skip)。

## 2. 本体が作るもの(host 担当)

| # | 内容 |
|---|---|
| H-A | 共通 H-5〜H-9(ライセンス表示・build.ps1・deskkit.spec・CLI 転送 200 件・selftest all) |
| H-B | `deskkit <module> open <paths...>` の起動経路: host が起動していなければ **起動してから** 転送する(最大 15 秒待つ)。SendPrep の「送る」ショートカット(FR-20/21)が `DeskKit.exe sendprep open` を呼ぶ。ソース実行時のショートカットのリンク先は `pythonw.exe run_deskkit.py`(sendprep が作る。host は引数を受けるだけ) |
| H-C | theme: 3モジュールのライト用アクセント色とグラフの色(dataviz の検証スクリプトで 7 色の明度帯・色覚差・コントラストを両モードで確認) |
| H-D | 版数 0.3.0、CHANGELOG、README、MODULE_GUIDE(§1.2 の「使ってよい host 側」に `deskkit.fileops` を追加、v0.3 の節)、はじめにお読みください.txt の文案(`docs/v0.3/readme_first.txt`) |
| H-E | NFR-1〜NFR-3 の実測(`docs/v0.3/measurements.md`)。exe のビルドは統合の後に私(統合担当)が行うので、host 担当はソース実行での計測と、ビルド設定の用意まででよい |

## 3. モジュールから本体への依存(これ以外に期待しない)

- `ctx.*`(MODULE_GUIDE §3 のもの)・`deskkit.ui.*`・`deskkit.catalog`・`deskkit.usage`・`deskkit.fileops`。
- sendprep の `handle_cli(["open", *paths])` は、パスをキューに積んで `ctx.show_page()` を呼び、`(0, "queued N")` を返す。パスが 200 件を超えたら先頭 200 件だけ積む。
- イベントは新しく足さない(3モジュールとも `ctx.emit` / `ctx.on` を使わない)。
- 一時停止(`ctx.is_snoozed()`)を見るのは PcCheckup の「空き容量の見張り」だけ。
