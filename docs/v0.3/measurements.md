# v0.3.0 本体の実測(host 担当)

測った日: 2026-09-25。PC: この開発 PC(Windows 11 Pro 26200、論理 CPU 16)。Python 3.13.2(venv)、PySide6 6.11.2。
**3モジュールは main にある骨組み(何もしない)で測った。** 本物のモジュールでの測り直しと exe での計測(NFR-2・NFR-3)は統合担当が行う。

## NFR-1 / NFR-2 相当(ソース実行)

方法(スクリプトは作業フォルダの外。手順だけ書く):

1. 一時フォルダを `DESKKIT_HOME` にし、settings.json に `host.show_window_on_start=false`・`onboarded=true`・`update.auto_check=false` を書く(画面を開かず、通信もしない)。
2. `pythonw.exe -m deskkit` を起動し、IPC が応答するまで(= `loader.load_all` が終わってイベントループが回り始めるまで)の時間を測る。
3. 何もせずに 20 秒待ってから、DeskKit のプロセス(venv の pythonw.exe は中継なので、その子の本物の python)のメモリを psutil で読む。さらに 10 秒の CPU 時間を測る。
4. `--quit` で終わらせる(kill しない)。既存4モジュールだけ有効 / 7モジュール全部有効 を交互に 3 回ずつ。

| 構成 | 起動→IPC 応答(中央値) | 作業セット | private(コミット) | USS(専用作業セット) | スレッド | 待機 10 秒の CPU |
|---|---|---|---|---|---|---|
| 既存4モジュールだけ有効(v0.2.0 相当) | 0.52 秒 | 150.3 MB | 76.7 MB | 71.4 MB | 18 | 0.00 秒 |
| 7モジュール全部有効(骨組み) | 0.52 秒 | 149.5 MB | 77.8 MB | 71.6 MB | 18 | 0.00 秒 |
| 差 | ±0.00 秒 | -0.8 MB | +1.1 MB | +0.2 MB | 0 | 0 |

- 各回の値: 4モジュール 0.52 / 0.54 / 0.52 秒、7モジュール 0.51 / 0.55 / 0.52 秒。メモリの揺れは ±1 MB 程度。
- 骨組みは何もしないので、この差は「本体が 7 モジュールを扱う分の増加」だけを表す(ほぼ 0)。

## 追加ライブラリを import したときの増え方(NFR-1・NFR-5 の見積もり)

`create()` や import 時に追加ライブラリを読むと、常駐のメモリと起動時間がこれだけ増える(同じ venv で2回測った値。1回目 / 2回目)。

| import するもの | 時間 | private(コミット)の増加 |
|---|---|---|
| Pillow(`from PIL import Image`) | 65 / 129 ms | +1.6 / +1.8 MB |
| pi_heif(`register_heif_opener()` まで) | 26 / 22 ms | +1.3 / +1.5 MB |
| numpy | 296 / 241 ms | **+493 MB**(下の注) |
| psutil | 102 / 1 ms | +0.0 MB |
| winrt の OCR 一式(media.ocr・graphics.imaging・storage.streams・globalization・foundation(.collections)) | 120 / 106 ms | +6.2 MB |
| winrt networking.connectivity | 284 / 8 ms | +0.1 MB |

**numpy の注(OpenBLAS のスレッド)**: numpy 同梱の OpenBLAS が import の時点で論理 CPU の数(ここでは 16)だけスレッド用の領域を確保する。
「コミット」は約 490 MB 増えるが、作業セット(タスクマネージャーの「メモリ」)の増加は約 12 MB で、NFR-1 の測り方では小さく見える。

| OPENBLAS_NUM_THREADS | import 後 コミット / 作業セット / 専用作業セット | 512×512 の行列積のあと |
|---|---|---|
| 未設定(16) | +491.3 / +12.1 / +9.7 MB | +525.4 / +19.5 / +14.7 MB |
| 4 | +105.8 / +11.5 / +9.1 MB | +139.8 / +17.8 / +13.0 MB |
| 1 | +9.2 / +11.2 / +8.8 MB | +43.2 / +16.7 / +11.8 MB |

→ 本体は起動時(`deskkit.app.main` の最初)に `OPENBLAS_NUM_THREADS` が未設定なら `1` にする(利用者やテストが指定していればそれに従う)。
常駐アプリなのでコミットを 490 MB 抱えないため。TwinSweep の計算が行列積中心で遅くなるなら、統合のときに見直す。

## exe の大きさ(NFR-3)の見込み

- 実測は統合担当(ビルド後)。v0.2.0 の DeskKit.exe は 39.5 MB(39,505,474 バイト)。
- 同梱する ffmpeg: `deskkit/_bundled/ffmpeg.zip` 41,839,816 バイト(ffmpeg.exe 134,087,168 バイトを LZMA で 69% 縮小)。
  作成に 79 秒(`tools/make_ffmpeg_bundle.py`。同じ版なら 0.3 秒で作り直しを省く)。onefile はこの zip をさらに zlib で包むが、LZMA 済みなのでほとんど縮まない。
- THIRD_PARTY_LICENSES.txt: 約 0.3 MB。
- 残りの追加ライブラリ(numpy と OpenBLAS の DLL・Pillow・pi-heif と libheif・winrt・psutil)は、onefile の圧縮後で数十 MB の見込み。合計で 100 MB 前後、150 MB は下回る見込み。

## ffmpeg(同梱する版)の確認(共通 §8)

- 版: `ffmpeg version n8.1.3-20260924`(BtbN `ffmpeg-n8.1-latest-win64-lgpl-8.1.zip`、配布物の SHA-256 `f2d3aa0b81f580454ed771160d8b71b23c9376ba65999be3acab435ac1bfdc9c` は同じリリースの checksums.sha256 と一致)。
- ffmpeg.exe の SHA-256: `62a44a461da3dd8988b7c84e557a5fa6c99c26ea84a0398a12e33e1e5ccda446`。
- configure に `--enable-version3`(LGPL v3)・`--disable-libx264 --disable-libx265`。
- `-encoders` の H.264: `libopenh264`・`h264_amf`・`h264_d3d12va`・`h264_mf`・`h264_nvenc`・`h264_qsv`・`h264_vaapi`・`h264_vulkan`。AAC: `aac`(内蔵)・`aac_mf`。
- `h264_mf` で 5 秒のテスト動画(testsrc2 1280×720 30fps + 440Hz の音、`-b:v 1000k -c:a aac -b:a 128k`)を作れた(終了コード 0、Constrained Baseline)。
  **ただし目標のビットレートを守らない**: 映像 1000k 指定で 1939 kb/s、500k 指定で約 1831 kb/s、3000k 指定で約 3338 kb/s(`-rate_control cbr` でも 1941 kb/s)。
  この PC の Media Foundation の H.264 エンコーダーは、低いビットレートの指定に下限があるように見える。`-hw_encoding 1` は失敗した(この PC にハードウェアの MFT が無い)。
- 比較: `libopenh264`(ソースから作られた OpenH264。BSD-2)は 1000k 指定で 1005 kb/s を守った。
  → SendPrep 担当への情報: サイズ上限に収めるには、`h264_mf` の結果の大きさを確かめて下げ直すか、`libopenh264` を使う。
- テスト映像(testsrc2)は細かい模様で圧縮しにくいので、実際の動画ではもっと下がる可能性がある。実物の動画での確認は SendPrep 担当・統合担当。

## THIRD_PARTY_LICENSES.txt に載せたもの(共通 §8)

| ソフトウェア | 版 | ライセンス |
|---|---|---|
| Qt / PySide6 / shiboken6 | 6.11.2 | LGPL v3 |
| FFmpeg(BtbN の LGPL ビルド) | n8.1.3-20260924 | LGPL v3 以降 |
| Pillow | 12.3.0 | MIT-CMU(同梱の brotli・FreeType・HarfBuzz・lcms2・libavif・libjpeg-turbo・libpng・libtiff・libwebp・OpenJPEG・zlib 等を含む) |
| pi-heif | 1.4.0(libheif 1.23.0 / libde265 1.1.1。`libheif_info()` で確認) | BSD-3-Clause(libheif・libde265 は LGPL v3、MinGW の GCC 実行時ライブラリは GPL v3 + 例外) |
| NumPy | 2.5.3 | BSD-3-Clause ほか(OpenBLAS・LAPACK・GCC 実行時ライブラリ) |
| psutil | 7.2.2 | BSD-3-Clause |
| PyWinRT(winrt-runtime と winrt-Windows.* 8 パッケージ) | 3.2.1 | MIT |
| typing_extensions(PyWinRT が使う) | 4.16.0 | PSF 2.0 |
| Python | 3.13.2 | PSF 2.0 |
| PyInstaller のブートローダー | 6.22.3 | GPL v2 以降 + Bootloader Exception |

- pi-heif の wheel に入っている `LICENSES_bundled.txt` は libheif 1.18.1 / libde265 1.0.15 と書いてあるが、実物は 1.23.0 / 1.1.1(ファイルが古い)。THIRD_PARTY_LICENSES.txt には実物の版を書いた。
- PyWinRT の wheel にはライセンスファイルが無いので、GitHub の LICENSE(MIT、著作権者 Microsoft Corporation と David Lechner)を載せた。

## 画面(7 モジュールで崩れないか)

- `tools/screenshot.py` でホーム・利用状況・設定をライト / ダークで撮って確認した(利用状況は偽の usage を持たせて 7 モジュールの棒グラフを並べた)。
  サイドバーは 7 項目が入り、ウィンドウの最小の高さは 607 px。ホームのカードは 2 列で 4 段目に 1 枚。
- 各ページの `minimumSizeHint().width()`(7モジュール有効、両テーマ同じ): home 68 / usage 68 / modeshift 464 / dropsort 456 / layoutkeep 471 / clipshelf 454 /
  sendprep 458 / pccheckup 465 / twinsweep 468 / settings 68 / logs 666。**最大 666(900 以下)**。ウィンドウ全体は 902(サイドバー 236 + ログの画面 666)。
- ライセンスの画面は最小 848×534。開くときは画面の作業領域の 85% までの高さ(最大 760)にする(1366×768 の画面でもはみ出さない)。

## exe での実測(統合担当、2026-09-25)

- NFR-3: `dist\DeskKit.exe` は **98MB**(v0.2.0 は 39.5MB)。上限 150MB 以内。
- NFR-2: `DeskKit.exe --version-file` の起動から終了まで(onefile の展開を含む、3 回の中央値): v0.2.0 **2.03 秒** / v0.3.0 **2.03 秒**。差は測れる範囲で 0。
- `DeskKit.exe --selftest all` が exit 0(host の H-5・H-7、SendPrep の ffmpeg の展開と照合を含む)。
- exe の中に pi_heif(libheif の DLL を含む)・`_winrt_windows_media_ocr`・numpy・psutil が入っていることを `pyi-archive_viewer` で確認。
- 統合で直したもの: TwinSweep が numpy・PIL を `importlib.import_module` の文字列で読んでいたため、exe に `PIL.ImageEnhance` が入らず selftest が `ModuleNotFoundError` で失敗した。mypy が 3.13 になって回避策が不要になったので、普通の import 文に戻した。
