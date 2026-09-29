# v0.4.0 本体の実測と決めたこと(host 担当)

測った日: 2026-09-28。PC: この開発 PC(Windows 11 Pro 26200、論理 CPU 16)。Python 3.13.2(venv)、PySide6 6.11.2。
**8モジュールは main にある骨組み(何もしない)で測った。** 本物のモジュールでの測り直しと exe での計測は統合担当が行う。

## H4-1 色(15 モジュール)

v0.3 と同じ手順: dataviz の `scripts/validate_palette.js`(Machado 2009 の色覚シミュレーション・OKLab の ΔE×100)で、
catalog の順(modeshift … twinsweep, eyebreak, jotdrop, keyfree, startupwatch, pagepress, mojifix, cliptrim, plugsave)に並べ、
隣り合う組を両モード・両方の面で確かめた。v0.3 の7色は変えず、新しい8色だけを探した。

探し方: OKLCH の格子(L 0.01・C 0.01・色相 2° 刻み、sRGB に入る色だけ)から、各モジュールの「らしい」色相の ±30° の範囲で、
(1) 隣り合う組が通常視 ΔE ≥ 15・色覚 ΔE ≥ 8、(2) 両方の面でコントラスト ≥ 3:1(ダークの画面の色は ≥ 4.5:1)、を満たす候補のうち、
(3) ほかの 14 色(ダークの画面の色は本体の色 `#7C8CFF`・成功 `#34D399`・危険 `#F87171` も)との通常視 ΔE の最小が大きい物を、
座標ごとに選び直して決め、最後に見た目で整えた(スクリプトは作業フォルダの外)。

| モジュール | ライト(画面とグラフ) | ダークのグラフ | ダークの画面(catalog) | 仮の色(ライト / ダーク) |
|---|---|---|---|---|
| eyebreak | `#046990` | `#1877A6` | `#5AF3EE` | `#0891B2` / `#22D3EE` |
| jotdrop | `#8A5100` | `#836816` | `#FDE68A` | `#A16207` / `#FACC15` |
| keyfree | `#422FD0` | `#4E5EDB` | `#A6B6FB` | `#4F46E5` / `#818CF8` |
| startupwatch | `#C00F6D` | `#E7436B` | `#FDA9B6` | `#E11D48` / `#FB7185` |
| pagepress | `#CC3FFA` | `#DB00BE` | `#F13BFE` | `#C026D3` / `#E879F9` |
| mojifix | `#7B0BAB` | `#9D16FE` | `#CE98D2` | `#9333EA` / `#C084FC` |
| cliptrim | `#4C94C2` | `#2297BA` | `#7DD9FC` | `#0284C7` / `#38BDF8` |
| plugsave | `#076945` | `#198352` | `#22BF12` | `#16A34A` / `#4ADE80` |

`validate_palette.js` の結果(15 色、隣り合う組):

| 色の組 | 面 | 明度の帯 | 彩度 | 色覚差(最小) | 通常視(最小) | コントラスト |
|---|---|---|---|---|---|---|
| ライト | `#FFFFFF` / `#F2F4F9` | PASS | PASS | PASS 8.1(twinsweep↔pccheckup。v0.3 からの組) | PASS 16.0(pccheckup↔sendprep) | PASS(全部 ≥ 3:1) |
| ダークのグラフ | `#151A24` / `#1B2130` | PASS | PASS | PASS 9.4(pagepress↔mojifix) | PASS 15.5(cliptrim↔plugsave) | PASS(全部 ≥ 3:1) |
| ダークの画面 | `#151A24` | **FAIL(わざと)** | PASS | PASS 8.6(pccheckup↔twinsweep。v0.3 からの組) | PASS 15.1(startupwatch↔keyfree) | PASS(全部 ≥ 4.5:1。最小 5.20) |

- ダークの画面の色は、暗い面の上の文字・字形・ナビの色なので明るい(OKLCH L 0.70〜0.92)。グラフ用の明度の帯(0.48〜0.67)から外れるのは設計どおりで、
  v0.3 の7色も同じく外れている。グラフには `_CHART_DARK` を使う。
- **隣り合っていない組**(妥協した点): 15 色を明度の帯に入れると、全部の組を通常視 ΔE 15 以上にはできない。グラフは1系列1グラフで 15 色が同時に並ばないので、
  新しい8色とほかの色の最小を次の値まで上げるのにとどめた。
  - ライト: 最小 10.1(cliptrim↔dropsort)。仮の色では 4.9(cliptrim)だった。
  - ダークのグラフ: 最小 8.2(cliptrim↔dropsort)。仮の色は明度の帯から外れていた(Tailwind の 400 番台)。
  - ダークの画面: 最小 8.1(eyebreak↔cliptrim)。仮の色では keyfree↔本体の色 1.2、jotdrop↔clipshelf 3.4、startupwatch↔危険の色 2.7 だった。
- 色相はモジュールの「らしさ」を残した(eyebreak は青緑、jotdrop は黄土、keyfree は藍、startupwatch は紅、pagepress は赤紫、mojifix は紫、cliptrim は空色、plugsave は緑)。
  ただし、隣と見分けるために、ライトの eyebreak は仮の色より青寄りで暗い、ダークの画面の jotdrop は淡い黄、にした。
- テスト: `tests/test_v04_host.py::test_palettes_adjacent_pairs_and_contrast` が、隣り合う組の通常視 ΔE ≥ 15・コントラスト・新しい8色の最小を確かめる。

## H4-2 probe の実測(本物の RegisterHotKey)

既定の範囲(KeyFree K-4: Ctrl+Alt・Ctrl+Shift・Alt+Shift・Ctrl+Alt+Shift × 88 キー − F12 の4 − Ctrl+Alt+Delete = **347 組**)を、
`HotkeyRegistry.probe(batch=32)` で3回。時間は1組ごとに `RegisterHotKey` の前から `UnregisterHotKey` の戻りまでを測った。

| 回 | 全体 T | 1組 t(中央値 / 最大) | 持つ時間 h(中央値 / 最大) | Σh | 結果 | 後の預かり |
|---|---|---|---|---|---|---|
| 1 | 2.3 ms | 3.2 / 22.9 µs | 1.9 / 12.0 µs | 0.65 ms | 空き 344・使用中 3 | 0 |
| 2 | 2.3 ms | 3.2 / 14.8 µs | 1.8 / 10.0 µs | 0.66 ms | 同じ | 0 |
| 3 | 2.4 ms | 3.3 / 166.2 µs | 1.9 / 141.6 µs | 0.89 ms | 同じ | 0 |

- KeyFree の撤退基準 R-2 (b)(Σh が 1 秒を超える)に対して、Σh は 1 ms 未満。NFR4-4(画面が 200ms 以上固まらない)は、試すのが別スレッドで全体でも 3 ms 未満なので満たす。
- 使用中の 3 組は、この PC で動いている常駐アプリ(本番の DeskKit を含む)の分(組の名前はここに書かない)。
- DeskKit 自身のキーが外れないこと: GUI スレッドで Ctrl+Alt+Shift+F22 を登録した registry で、その組を含めて probe すると結果は `deskkit`(試さない)。
  終わった後も登録名は残り、別の registry(別スレッド)から試すと `used`(= まだ DeskKit が持っている)だった。
- `include_win`(956 組)と、別のプロセスが持つ組の判定(`tools/keyfree_measure.py`)は KeyFree 担当(`docs/v0.4/keyfree.md`)。

## NFR4-1 / NFR4-2 相当(ソース実行)

方法は v0.3(`docs/v0.3/measurements.md`)と同じ: 一時フォルダを `DESKKIT_HOME` にし、`host.show_window_on_start=false`・`onboarded=true`・
`host.update.auto_check=false` を書いて `pythonw.exe -m deskkit` を起動、IPC が応答するまでの時間、20 秒待ってからのメモリ、その後 10 秒の CPU 時間。
`--quit` で終わらせる。v0.3 の7モジュールだけ有効 / 15 モジュール全部有効 を交互に 3 回ずつ、2 組(計 6 回ずつ)。

| 構成 | 起動→IPC 応答(中央値) | 作業セット | private(コミット) | USS | スレッド | 待機 10 秒の CPU |
|---|---|---|---|---|---|---|
| v0.3 の7モジュールだけ有効(v0.3.0 相当) | 0.67 秒 | 153.5〜153.8 MB | 80.6〜80.8 MB | 74.5〜74.7 MB | 18〜19 | 0.00〜0.02 秒 |
| 15 モジュール全部有効(8つは骨組み) | 0.74 秒 | 153.7〜154.1 MB | 81.1 MB | 75.1〜75.2 MB | 18 | 0.02 秒 |
| 差 | +0.07 秒 | +0.2〜0.3 MB | +0.3〜0.5 MB | +0.5 MB | 0 | ほぼ 0 |

- 各回の起動: 7モジュール 3.12(1回目。ディスクのキャッシュが冷えていた)/ 0.64 / 0.66 / 0.66 / 0.68 / 0.78 秒、15 モジュール 0.88 / 0.68 / 0.79 / 0.85 / 0.69 / 0.66 秒。
- 骨組みは何もしないので、この差は「本体が 15 モジュールを扱う分の増加」だけ。NFR4-1(20MB 以下)・NFR4-2(0.5 秒以下)の本当の判定は、本物の8モジュールを入れた後(統合担当)。

## 追加ライブラリを import したときの増え方(NFR-5 の見積もり)

同じ venv で2回ずつ(新しいプロセスで測った)。PagePress は最初に使う関数の中で読むので、常駐のメモリにはこの分は乗らない。

| import するもの | 時間 | private(コミット)の増加 | 作業セットの増加 |
|---|---|---|---|
| pypdf | 122 / 122 ms | +12.0 / +11.8 MB | +14.0 / +13.7 MB |
| pypdfium2(pdfium.dll の読み込みまで) | 130 / 92 ms | +4.5 / +4.4 MB | +9.9 / +9.8 MB |
| pypdfium2 + 空の PDF を1つ作る | 93 / 98 ms | +4.7 / +4.8 MB | +10.2 / +10.3 MB |

## H4-11 exe の大きさの見込み

- pdfium.dll(`pypdfium2_raw`): 7,260,672 バイト。zlib(レベル 9)で 3.44 MB。
- pypdf・pypdfium2・pypdfium2_raw・pypdfium2_cfg の .py 87 個: 1.94 MB → zlib で 0.43 MB(exe にはバイトコードで入る)。
- THIRD_PARTY_LICENSES.txt: 0.30 MB → 0.45 MB(PDFium のビルドに含まれるライブラリの全文 16 個を足した)。
- 見込み: v0.3.0 の 98 MB に約 4 MB。試しのビルドでは +7 MB の 105 MB だった(下の「試しのビルド」)。上限 150 MB に収まる。

## H4-11 exe に入れる物(deskkit.spec)

- `pypdf` / `pypdfium2` / `pypdfium2_raw` / `pypdfium2_cfg` の全サブモジュールを hiddenimports に(PagePress は関数の中で import するため)。
  `pypdfium2.__main__`(コマンドライン。`pypdfium2_cli` を読む)は除き、`pypdfium2_cli` は excludes。
- `pdfium.dll` は `collect_dynamic_libs("pypdfium2_raw")` で binaries に。見つからなければ spec が止まる。`version.json`(pypdfium2 と pypdfium2_raw)は datas に。
  hooks-contrib 2026.7 にも `hook-pypdfium2.py` / `hook-pypdfium2_raw.py` があるが、フックが無い環境でも入るように名前で挙げた。
- venv に無いと spec が止まる名前に 4 つを足した。

## THIRD_PARTY_LICENSES.txt(V4-2 と共通 §11)

- 前半4本と PcCheckup の追加(V4-2)の分は変わりなし。v0.3 の 10 項目は全文が同じまま(差分は番号の付け直しだけ)。
- 後半の PagePress の分として3項目を足した(13 項目): pypdf 6.19.0(BSD-3-Clause)、pypdfium2 5.13.0(Apache-2.0 / BSD-3-Clause と CC-BY-4.0 の文書)、
  PDFium chromium/7999(BSD-3-Clause。pypdfium2_raw の version.json は 153.0.7999.0、origin pdfium-binaries)と、wheel の `BUILD_LICENSES` の全文。
- `BUILD_LICENSES` は **16 ファイル**(依頼文と PagePress 仕様書の「15 個」と違う): abseil・agg23・fast_float・freetype・icu・lcms・libjpeg_turbo.ijg・
  libjpeg_turbo.md・libopenjpeg・libpng・libtiff・llvm-libc・pdfium-binaries・pdfium・simdutf・zlib。libjpeg-turbo が2ファイルなので、数え方の違いと見られる。
  `tools/make_third_party_licenses.py` は 16 でないと止まる(版を上げたときに見直すため)。

## 試しのビルド(worktree の中。配布物ではない)

`build.ps1 -NoCopy -Python <venv の python>` で worktree の `dist\` に作った(8モジュールは骨組み。一つ上のフォルダへはコピーしていない)。

- `dist\DeskKit.exe`: **110,090,009 バイト(105.0 MB)**。v0.3.0 の 98 MB から +7 MB。上の見込み(+4 MB)より大きいのは、pypdf・pypdfium2 のバイトコードと
  ライセンスの全文のほか、本物の8モジュールが入る前の骨組みの分など、細かい物の合計と見られる(内訳は統合担当の実測で見直す)。上限 150 MB に収まる。
- `DeskKit.exe --selftest all` が exit 0(host 30/30。H4-2 の probe・H4-6 の replace_key を含む。15 モジュールの selftest を実行)。
- exe の中身(`PyInstaller.archive.readers.CArchiveReader` で確認): `pypdfium2_raw\pdfium.dll`(1つだけ)・`pypdfium2_raw\version.json`・
  `pypdfium2\version.json`、PYZ に `pypdf.*` 58 個・`pypdfium2.*`・`pypdfium2_cfg.*`。`pypdfium2_cli` は入っていない。
- 警告(`build\deskkit\warn-deskkit.txt`)の PDF 関係は、使わない任意の依存だけ(pypdfium2 の `tabulate`、pypdf の `bidi`・`arabic_reshaper`・`fontTools`)。
- exe の中で pypdfium2 を実際に読み込めるかは、本物の PagePress の selftest で確かめる(統合担当)。

## 画面(15 モジュールで崩れないか)

- **サイドバーが 15 項目で高さ 930 px を超え、ウィンドウの最小の高さが 930 px になっていた**(1366×768 の画面に収まらない)。
  サイドバーをスクロールできる枠に入れ、最小の高さを 254 px(ほかの制約は `setMinimumSize(1000, 660)`)にした。選んだ項目は見える位置までスクロールする。
- `tools/screenshot.py` でホーム・8モジュールの画面・設定・ログをライト / ダークで撮って確認した(8モジュールは骨組みの画面)。
- 各ページの `minimumSizeHint().width()`(offscreen、15 モジュール有効): 既存7モジュール 592〜618、新しい8つ 579〜644、ログ 823(ヘッダーの説明文の幅。
  offscreen の字の幅なので、v0.3 の実測 666 と直接は比べられない)。

## 仕様書・契約から外れた点と決めたこと

- probe の `batch` の既定は契約どおり 16(KeyFree 仕様書 §9 の `batch_size` 既定 32 は KeyFree が渡す)。
- `on_done(ProbeDone("busy", 0))` も GUI スレッドへ運んでから呼ぶ(probe() から戻った後)。「すぐ」は「試さずに」の意味とした。
- `ProbeResult.error` は `error` のときだけ番号を入れ、`used`(1409)は 0。
- probe は普段の登録と同じく `MOD_NOREPEAT` をつけて試す(結果の mods には入れない)。つける・つけないで結果が変わるかは KeyFree の実測(§8)。
- 型(`ProbeResult` / `ProbeBatch` / `ProbeDone`)は overlaykit に置き、`deskkit.hotkeys` から同じ物を出す(registry が作るため)。
  `ProbeHandle` / `HeldKey` / `FailedKey` / `HotkeySnapshot` は `deskkit.hotkeys` に置いた。名前・フィールドは契約どおり。
- `parse` は修飾キーが1つも無い書き方も None(`register_text` が受け付けないため)。KeyFree の FR-9 の「修飾キーの無い組」の文言は、KeyFree が自分で見分ける。
- `format` が名前を持たないキーは `VKxx` と書き、`parse` / `register_text` も `VKxx` を読めるようにした。`VK_OEM_102` に `OEM102` の名前を足した。
- snapshot の failed は、表記を読めなかった設定を含めない(mods / vk が無いため)。モジュールが `unregister(name)` した名前は failed から消える。
- モジュールを止めるとき、probe が預かりを戻すのを最大 2 秒待つ(実測の全体 2.3 ms に対して十分)。
- ctx に内部用の `post(fn)`(safe 済みの関数を GUI スレッドへ運ぶ)を足した。MODULE_GUIDE には載せない(モジュールは `call_soon` を使う)。
- 診断レポートの「ホットキー」に「試して外す: 動作中/なし / 預かり N 件」(件数だけ)。
- `build.ps1` に `-NoCopy`(一つ上のフォルダへのコピーをしない)と `-Python`(使う python.exe)を足した。既定の動きは変わらない。
- pypdf は `cryptography` を入れていないので、AES で暗号化された PDF は pypdf では開けない(PagePress は P-3 でパスワード付きを扱わないので影響は小さい見込み)。

## 実機で確かめること(統合担当・利用者)

- exe: `DeskKit.exe --selftest all`(host の H4-2 / H4-6 の確認を含む)、`pyi-archive_viewer` で `pypdfium2_raw\pdfium.dll` と `version.json` が入っていること、exe の大きさ。
- 本物の8モジュールを入れた後の NFR4-1 / NFR4-2(この表の方法で)。
- KeyFree で、別のプロセスが持つ組が「使用中」と出ること(本体の probe を通した R-2 (a))。
- 1366×768 の画面でサイドバーがスクロールし、窓が収まること。
- PlugSave のドライブの抜き差しで `WM_DEVICECHANGE`(DBT_DEVICEARRIVAL)が隠しウィンドウに届くこと。

## 契約変更の提案

なし(§2.1・§2.2 の形は変えていない)。

## 統合後の exe での NFR4-1 / NFR4-2(2026-09-29、本物の8モジュール入り)

上と同じ方法で、`dist\DeskKit.exe`(108,358,237 バイト・103.3 MB)を測った。v0.3 の7モジュールだけ有効 / 15 モジュール全部有効 を交互に 3 回ずつ。
メモリは onefile の子プロセス(本体)の値。

| 構成 | 起動→IPC 応答(中央値) | 作業セット | private | USS | スレッド | 待機 10 秒の CPU |
|---|---|---|---|---|---|---|
| v0.3 の7モジュールだけ有効 | 2.10 秒 | 146.1〜149.8 MB | 72.4〜75.2 MB | 66.8〜69.6 MB | 11〜18 | 0.00〜0.02 秒 |
| 15 モジュール全部有効 | 2.27 秒 | 154.6〜155.3 MB | 79.5〜80.7 MB | 73.7〜74.8 MB | 20〜21 | 0.02〜0.05 秒 |
| 差 | +0.17 秒 | +5〜9 MB | +5〜8 MB | +5〜8 MB | +2〜3 | ほぼ 0 |

- NFR4-1(20MB 以下の増加)・NFR4-2(0.5 秒以下の増加)とも満たす。
- 各回の起動: 7モジュール 4.87(1回目。キャッシュが冷えていた)/ 2.10 / 2.07 秒、15 モジュール 2.27 / 2.21 / 11.97 秒。
  11.97 秒は、15 モジュールだけを続けて 4 回測り直すと 2.23 / 2.15 / 2.30 / 2.11 秒で再現しなかった(ディスクかウイルス対策の検査と重なったと見ている。原因は未確認)。
- `DeskKit.exe --selftest all` は exit 0。
