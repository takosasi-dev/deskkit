# DeskKit v0.4.1 — 担当 D: TwinSweep・ClipShelf

対象: `deskkit/modules/{twinsweep,clipshelf}/**`、`tests/modules/{twinsweep,clipshelf}/**`。契約は `docs/INTERFACES_v0.4.1.md` §3 D。
v0.4 の不変条件(`INTERFACES_v0.4.md` §0)は変えていない。ClipShelf の DB のスキーマ(仕様書 §9.3)も変えていない。

## 1. 結果

- ブランチ: `w5/sweepshelf`(このファイルと同じコミット)
- `pytest -q`(worktree の直下、全体): `1522 passed, 20 skipped, 48 deselected`
- `ruff check deskkit tests`: `All checks passed!`
- `mypy deskkit overlaykit`: `Success: no issues found in 310 source files`
- テストの置き場所は契約のとおり(`tests/modules/twinsweep/test_keep_choices.py`・`tests/modules/clipshelf/test_snippet_transfer.py` を新規)

## 2. TwinSweep — 似ている度合いを変えても選択を保つ(T-9 を改めた)

| # | 変更 | 理由 |
|---|---|---|
| TS-1 | 利用者が選び直した「残す/ごみ箱へ」を `ResultModel.choices`(写真の pid → 残すか)に覚える。`set_keep` が成功したときだけ記録する。メモリの中だけで、ファイル・ログには書かない | 契約 D-1 |
| TS-2 | `rebuild`(似ている度合い・探す写真の変更)は、作り直したグループの初期の選び方の上に `choices` を当てる。今の度合いでグループに入らない写真の選択も捨てずに持ち越す(別の度合いに戻したときにまた当たる) | 契約 D-1。pid は 1 回のスキャンの中で同じファイルを指す(作り直しは同じ `Photo` を使い回す) |
| TS-3 | 当てた結果「残す」が 1 枚も無いグループは、そのグループの写真を全部初期の選び方(`ResultModel.initial`)に戻す。戻したことで別のグループの最後の「残す」が外れたら、そのグループも戻す(繰り返し)。初期の状態ではどのグループにも「残す」があり、戻した写真は二度と動かないので必ず終わる | 契約 D-2・G-5・INV-2 |
| TS-4 | `start_scan` で実際にスキャンを始めるとき(フォルダが無い等で始まらないときは除く)に `choices` を捨てる。中止して前の結果が残る場合も、覚えた選択は捨てる(今の表示の選択はそのまま) | 契約 D-3 |
| TS-5 | ごみ箱へ送った写真(`remove_photos`)の `choices` も捨てる | 無くなった写真を覚えておく意味が無い |
| TS-6 | 設定の「似ている度合い」の説明に「選び直した『残す』『ごみ箱へ』は引き継ぎます」を足した | 以前は「初期状態に戻る」(T-9)だったので、変わったことを画面で知らせる |

- 送る直前の INV-2 の確認(`recycle_selected` の `invariant_ok`)は今のまま。
- グループの並び(「減らせる大きさ」の大きい順、FR-8)は初期の選び方で決める(今までと同じ。選び直しで並びが動かない)。

## 3. ClipShelf — 定型文の書き出し・読み込み(仕様書 §13 Q-9)

| # | 変更 | 理由 |
|---|---|---|
| CS-1 | `transfer.py` を新設(純粋な処理だけ)。書き出しは定型文の名前と本文だけを UTF-8(BOM なし)の JSON にする。`{"format": "deskkit.clipshelf.snippets", "version": 1, "exported_at": …, "note": …, "snippets": [{"name", "text"}]}`。履歴・ピン・日時・コピー元・ID は書かない。一時ファイル(`.<名前>.part`)に書いて `os.replace` | 契約 D-CS-1 |
| CS-2 | 書き出す前に確認の窓(赤い「平文で書き出す」ボタン)を出す。表題「定型文を書き出す(暗号化されません)」、本文の先頭に「書き出したファイルは暗号化されません。このファイルは誰でも読めます(メモ帳などで開くと、定型文の名前と本文がそのまま見えます)」、続けて共有フォルダ・クラウド・USB メモリに置かない・使い終わったら削除、の注意と件数。確認で取り消すとファイルの選択窓も出さない | 契約 D-CS-1 |
| CS-3 | 読み込みは既存に足す。名前の前後の空白を除き、空の名前は「無題の定型文」とみなしたうえで、名前と本文の両方が同じものは足さない(ファイルの中の重複も)。足すときは先に全部を暗号化してから 1 つのトランザクションで書く(`Store.add_snippets`。全部入るか何も入らないか) | 契約 D-CS-2 |
| CS-4 | 読めない・形が違うファイルは何も変えずに知らせる。理由コード: `too_large`(8 MB 超)・`not_utf8`・`not_json`・`wrong_format`(format 違い・version が整数でない等)・`newer_version`(version > 1)・`bad_entry`(名前が文字列でない・本文が無い/空)・`unreadable`。1 件でも形の違う定型文があればファイル全体を読まない。BOM 付き UTF-8 と、知らないキーは受ける | 契約 D-CS-2 |
| CS-5 | 件数の上限 `transfer.MAX_SNIPPETS = 1000`(読み込みで足すときだけ見る)。超えた分は足さずに件数を知らせる | 契約 D-CS-2「件数の上限があれば」。今までの定型文には上限が無かった(下の「仮に決めたこと」) |
| CS-6 | ログ・ops には件数と理由コードだけ。ops: `snippets_export`(`result`=ok/failed、`count`)・`snippets_import`(`result`=ok/failed/理由コード、`read`・`added`・`duplicates`・`over_limit`)。書き出し先・読み込み元のパス・ファイル名は、ログにも画面のエラー文にも入れない | 契約 D-CS-3・INV-1 |
| CS-7 | 画面: 定型文の欄の「新しい定型文」の下に「書き出す」「読み込む」を並べた。読み込みの結果は窓で「N 件足しました/同じものが既にあるため N 件は足しませんでした/上限のため N 件は足しませんでした」と「読み込んだファイルは暗号化されていません。要らなければ削除してください」を出す。編集中の未保存の変更は読み込みで消さない(一覧だけ作り直す) | 契約 D-CS-4 |
| CS-8 | ファイルの選択窓(`_ask_export_path`・`_ask_import_path`)と、選んだパスで動く処理(`export_snippets_to`・`import_snippets_from`)を分けた。テストは後者を直接呼び、選択窓は出さない。書き出しの既定のファイル名は `Documents\ClipShelf-snippets-<モジュールの時計の日付>.json` | 依頼 |
| CS-9 | `ClipShelfModule.now()`(モジュールの時計)を足した | 画面の既定のファイル名の日付を本物の時計に頼らないため |

## 4. 仮に決めたこと(利用者に確かめたいこと)

1. **TwinSweep: グループが初期に戻っても、覚えた選択は消さない。** 例:「ふつう」で A をごみ箱・C を残すにしたあと「厳しめ」にすると、{A, B} のグループに「残す」が無くなるので初期(A を残す)に戻る。そこで「ふつう」に戻すと、A をごみ箱・C を残す、がまた当たる。「戻したら忘れる」ほうがよければ `_apply_choices` で戻した写真の `choices` を消すだけで変えられる。
2. **TwinSweep: 中止したスキャンでも覚えた選択は捨てる。** 契約の「新しいスキャンを始めたら」を字義どおりにした。前の結果の表示(今の選択)は残るが、そのあと度合いを変えると初期の選び方になる。
3. **ClipShelf: 仕様書 §4(非目標)・§13 Q-9 の「平文エクスポートは作らない前提」とは食い違う。** 契約(INTERFACES_v0.4.1 §3 D)の「平文の JSON を確認の窓ではっきり知らせる」に従った。仕様書の Q-9 を「回答済み(v0.4.1: 平文の JSON、書き出し前に確認)」に直してほしい。
4. **ClipShelf: 定型文の件数の上限を 1,000 件にした(読み込みのときだけ)。** 画面で 1 件ずつ足すときには上限を掛けていない。上限を全体の決まりにするか、数を変えるか確かめたい。読み込むファイルの大きさも 8 MB までにした。
5. **ClipShelf: 1 件でも形の違う定型文があればファイル全体を読まない**(何も足さない)。形の合う分だけ足すほうがよいか確かめたい。
6. **ClipShelf: 復号できない定型文は書き出せない**(確認の窓に件数を出す)。

## 5. 本体への提案

- なし(表の外のファイルは触っていない)。
- 参考: 仕様書 `ClipShelf_クリップボード履歴定型文_仕様書.md` の §4 の非目標「エクスポート(平文ファイル出力)」と §13 Q-9 は、上の 3 のとおり今の実装と合わないので、仕様書の持ち主が直す必要がある(このブランチでは読むだけにした)。
- 参考: `docs/v0.3/twinsweep.md` の T-9(「選択は初期状態に戻る」)は、このファイルの TS-1〜TS-4 で置き換わった。v0.3 の文書は当時の記録として直していない。

## 6. 撮った画面(本物の Windows 描画、一時 DESKKIT_HOME、ClipShelf は観察モード)

撮り方: `shots_real.py` を元にした `scratchpad\w5d\shots_sweepshelf.py`(ClipShelf と TwinSweep だけ有効、定型文を 3 件足して撮る。確認の窓・結果の窓は撮ったら閉じる。キー送信なし)。ダーク・ライト両方。

- `clipshelf_snippets_{dark,light}.png` — 定型文の欄(「新しい定型文」の下に「書き出す」「読み込む」)
- `clipshelf_snippets_card_{dark,light}.png` — 定型文の欄だけ
- `clipshelf_export_confirm_{dark,light}.png` — 書き出し前の確認の窓
- `clipshelf_import_result_{dark,light}.png` — 読み込みの結果(1 件足した・1 件は同じなので足さなかった)
- `clipshelf_import_error_{dark,light}.png` — 形が違うファイル(何も変えない)
- `twinsweep_{dark,light}.png`・`twinsweep_full_{dark,light}.png` — 似ている度合いの説明の文言
