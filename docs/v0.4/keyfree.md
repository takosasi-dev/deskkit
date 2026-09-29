# KeyFree v0.4.0 — 仕様書からの変更点・既定値・実測値

対象: `deskkit/modules/keyfree/**`。仕様書は `KeyFree_空いているショートカット探し_仕様書.md`(K-* / FR-* / INV-* / AC-*)。
逸脱不可要件(INV-1〜6・V4INV-1〜5)は変えていない。設定の既定値は仕様書 §9 のまま。

## 1. ファイル

仕様書 §2 のとおり(`combos` / `keynames` / `scan` / `trykey` / `_win32` / `page` / `module` / `selftest`)。次を足した。

- `oplog.py`: 利用状況(FR-19)のための `ops.jsonl`。1行 = 日時・種類(`scan` / `check` / `copy`)・件数・所要時間だけ。120 日より古い行は `start()` で落とす。
- `fakes.py`: 偽の `ctx.hotkeys`(probe・snapshot・format・parse・register・unregister)・偽の配列・偽の ctx。テストと自己検査で使う。
- `_win32.py` は `MapVirtualKeyW` だけ(`KeyboardApi` Protocol の背後)。ホットキーの登録・解除は `ctx.hotkeys` だけを通す(INV-1・V4INV-2)。
- 本物の probe を使うテストは `tests/modules/keyfree/test_real.py`(`@pytest.mark.win32_real`)。預かった組は fixture の finally で返し、
  最後に `registry.probe_holding() == 0` を確かめる。

## 2. 仕様書からの変更点・解釈

| # | 内容 | 理由 |
|---|---|---|
| KF-1 | 測定用の `tools/keyfree_measure.py`(§8)は、**`tests/modules/keyfree/keyfree_measure.py`** に置いた(`hold` で別のプロセスとして組を持つ、`try` で試す) | `tools/**` は本体担当の範囲(契約 §0)。配布物に入らない点は同じ |
| KF-2 | CLI `keyfree scan` は設定の `include_win` によらず、**既定の範囲(K-4 の4組・347 組)** を調べる。出すのは空きのうち **Windows 用の印が無い組だけ**(既定の範囲では PrintScreen の4組を除く) | FR-14 の「既定の範囲」を字のとおりに読んだ。印の付いた組は「空き」が本当とは限らない(K-5)ので、貼ってすぐ使える物だけを出す |
| KF-3 | 「1つだけ調べる」「調べ直す」「keyfree check」も、試す前に前面を見る(FR-8 と同じ条件)。CLI で止めたときは 23 | INV-6「前のウィンドウがゲームか全画面の間は試さない」は一覧に限らないため |
| KF-4 | CLI は `QEventLoop`(20ms ごとに終わったかを見るタイマーつき)で待つ。上限は check 10 秒・scan 45 秒で、超えたら cancel して 23 | handle_cli は GUI スレッドで呼ばれる(本体担当の申し送り)。IPC の呼び手は 60 秒で待つのをやめるので、それより短くした |
| KF-5 | 前面の判定は `unsafe_for_input()` をそのまま使うので、**管理者として動いている窓や、判定できない窓が前にあっても止める**。前面を読めないときも止める | FR-8 の字のとおり。安全な側(調べない)に倒した。DeskKit 自身の窓が前なら止めない |
| KF-6 | 「調べられない」の件数(画面・diagnostics の `unavailable`)には、試した結果のエラーと、試さない組(F12・Ctrl+Alt+Delete。表で「調べられない(Windows 用)」)の両方を数える | 画面の4つの状態に合わせた |
| KF-7 | 利用状況の「調べた回数」(primary)は、一覧のほか「1つだけ調べる」「調べ直す」「keyfree check」も1回と数える | R-3 の「調べた回数が 0 のまま」を、使われ方によらず正しく見るため |
| KF-8 | 押して確かめるは、**届いたらその場で返す**(10 秒待たない)。終わりのタイマーは精度の高い種類にし、念のため 250ms ごとの刻みでも期限を見る | INV-2(`try_seconds` を超えて預からない)。Qt の粗いタイマーは 5% 遅れうる |
| KF-9 | host が `busy` を返したとき(CLI など別の調べ物が動いていた)は、何も試していないので前の結果に戻し、「ほかの調べ物が終わるまで待ってください」を出す | §10 |
| KF-10 | 記号キーの表示は今の配列の文字(`MapVirtualKeyW`)、コピーする文字列は `ctx.hotkeys.format`(米国配列の名前)。両者が違うキーを選ぶと、「キーボードの『;』のキーです。コピーした書き方は DeskKit の設定で使う書き方で…」と画面に注記する | K-10 のとおりだと、JIS 配列では「;」のキーを押して `Ctrl+Alt+=` がコピーされ、戸惑うため |
| KF-11 | JIS 配列では `VK_OEM_5`(¥ のキー)と `VK_OEM_102`(ろ のキー)がどちらも「\」と出る(実測 §4)。表では同じ字形の2つのキーになる。ボタンのツールチップに書き方(`Ctrl+Alt+\` / `Ctrl+Alt+OEM102`)を出して区別する | `MapVirtualKeyW` の返す文字がそうなっている |
| KF-12 | 「1つだけ調べる」の「修飾キーが無い」と「読めない」の区別: `ctx.hotkeys.parse` はどちらも None なので、`+` を含まず `"Ctrl+" + 入力` なら読める物を「修飾キーが無い」とした | parse の決まり(H4-4)が区別を返さないため |
| KF-13 | 「DeskKit のキー」のカード(FR-10)の表示名は、ホームのホットキー一覧と同じ `deskkit.ui.main_window.hotkey_label` を使う(読めなければ登録名)。KeyFree 自身の `keyfree.try_key`(押して確かめるの一時の登録)はカードに出さない | 表示名をホームとそろえる(`deskkit.ui.*` は使ってよい host 側) |
| KF-14 | FR-10 の「おすすめを3つ」は、FR-11 のおすすめの先頭3つ | 同じ規則で選ぶため |
| KF-15 | おすすめ(FR-11)は字のとおり `recommend_order` の最初の組から英字・数字の順に埋める(既定では Ctrl+Alt+Shift+A〜H の8つになる) | 仕様書の決まりのまま。組を混ぜて出すかは §8 の結果で見直す項目(§9 の注記) |
| KF-16 | 配列の変化(§10)は、始めたときと終わったときの記号キーの文字を比べて見る(最後まで終わった一覧だけ)。DeskKit のキーの変化(FR-13)は、表にある組のうち DeskKit が持つ物の集まりを前後で比べる | 仕様書は見方を決めていないため |
| KF-17 | 表(修飾キーの組ごとのカード)は、画面が表示中なら1回のイベントで1枚ずつ作る | 956 組を一度に作ると約 200ms 画面が止まった(NFR4-4)。分けると1回 75ms 以内(§4) |
| KF-18 | 「空き」のボタンを押すと、コピー(FR-4)と同時に、そのカードの下に詳しい欄(もう一度コピー・押して確かめる)を開く。「使用中」「調べられない」は FR-5 の文と「調べ直す」、DeskKit は持っている登録の名前、試さない組はわけを出す | FR-4・FR-5・FR-12 の置き場所を1つにまとめた |
| KF-19 | 始める前の確認(FR-1)は、ダイアログではなく画面の中の欄(「始める」「やめる」)にした | モーダルのダイアログは画面の操作を止める。連打(§10)も「始める」を押した時点で欄を閉じて1回にする |

### 既定値(仕様書 §9 のまま)

`include_win: false`・`batch_size: 32`(probe に明示して渡す。host の既定は 16)・`try_seconds: 10`・`recommend_count: 8`・
`recommend_order: ["Ctrl+Alt+Shift", "Ctrl+Alt", "Alt+Shift", "Ctrl+Shift"]`。範囲外・型違いの値は既定値(数値は上下限)に戻して書き戻す。
`recommend_order` は読める修飾キーの組だけを残し(重なりは除く)、1つも無ければ既定の順。画面の「設定」で全部変えられる(並びはドラッグ)。

### diagnostics() のキー(FR-20)

`tried`(試した組)・`free`・`used`・`deskkit`・`unavailable`・`pressed`(押された組の数)・`ms`(所要時間)。直近の終わった一覧の値で、まだなら 0。

### ログ・操作記録に書くもの(INV-4)

件数と所要時間・終わり方(`finished` / `cancelled` / …)・中止のわけ(`user` / `foreground` / `stop` / `timeout`)だけ。
組み合わせの名前・キーの名前・エラー番号は、ログ・`ops.jsonl`・diagnostics・usage に書かない(テストの conftest が、どのテストでもログと diagnostics に
`Ctrl+` `Alt+` `Shift+` `Win+` が無いことを確かめる。AC-9)。CLI の出力と画面には出す(V-8)。

## 3. 受け入れ基準

| AC | 状態 | 確かめ方 |
|---|---|---|
| AC-1 | 済 | `test_module.py::test_states_and_deskkit_not_probed`・自己検査 |
| AC-2 | 済 | `test_combos.py::test_default_and_include_win_counts`(347 / 956、F12・Ctrl+Alt+Delete・Win+L は渡さない) |
| AC-3 | 済 | `test_combos.py::test_missing_symbol_removes_column`・`test_page.py::test_symbol_hidden_from_table` |
| AC-4 | 済 | `test_page.py::test_windows_mark_and_recommend`・`test_combos.py::test_recommend_order_and_filters` |
| AC-5 | 済 | `test_page.py::test_free_click_copies_format`(偽の format の文字列がクリップボードに入る) |
| AC-6 | 済 | `test_module.py::test_foreground_game_blocks_start`・`test_foreground_switch_cancels` |
| AC-7 | 済 | `test_page.py::test_pressed_notice_on_page`・`test_module.py::test_pressed_combo_shown_and_logged_as_count` |
| AC-8 | 済 | `test_module.py::test_stop_cancels_scan_and_try` |
| AC-9 | 済 | `tests/modules/keyfree/conftest.py`(全テストの後にログと diagnostics を調べる) |
| AC-10 | 済 | grep が0件(`test_static.py` でも確かめる)。V4AC-3・V4AC-4 の grep も0件 |
| AC-11 | 済(実機) | `test_real.py::test_other_process_key_is_used`(MOD_NOREPEAT の有無の両方で 20 → 返した後は 0) |
| AC-12 | 一部(実機) | `test_real.py::test_scan_keeps_own_keys`: 一覧の前後で snapshot が同じ・DeskKit が持つ組は別のプロセスから登録できない(1409)= 持ち続けている。**押して動くか** はキー送信をしない決まりなので未確認(§5) |
| AC-13 | 済 | `test_page.py::test_old_host_page`・`test_module.py::test_old_host_unsupported` |
| AC-14 | 済 | `python -m deskkit.modules.keyfree --selftest` と `python -m deskkit --selftest keyfree` / `all` が 0 |

## 4. 実測(2026-09-28、pc1240036、ソース実行)

本物の `overlaykit.HotkeyRegistry.probe` を、登録・解除の前後で時刻を取る `Win32Api` で包んで測った(測定のスクリプトはリポジトリの外)。
batch は 32。t = 1組の時間(登録の前 → 解除の戻り。使用中は失敗した登録の戻りまで)、h = 持つ時間(登録の成功の戻り → 解除の戻り)。

| 範囲 | 回 | N | T | t 中央値 / 最大 | h 中央値 / 最大 | Σh |
|---|---|---|---|---|---|---|
| 既定 | 1 | 347 | 3.34ms | 0.0045 / 0.0294ms | 0.0026 / 0.0175ms | 0.925ms |
| 既定 | 2 | 347 | 2.87ms | 0.0045 / 0.0241ms | 0.0026 / 0.0186ms | 0.939ms |
| 既定 | 3 | 347 | 2.64ms | 0.0045 / 0.0188ms | 0.0026 / 0.0141ms | 0.920ms |
| include_win | 1 | 956 | 7.62ms | 0.0045 / 0.2936ms | 0.0026 / 0.1945ms | 2.586ms |
| include_win | 2 | 956 | 6.47ms | 0.0044 / 0.0363ms | 0.0026 / 0.0131ms | 2.195ms |
| include_win | 3 | 956 | 6.67ms | 0.0045 / 0.0777ms | 0.0026 / 0.0746ms | 2.373ms |

- **R-2 (b)**: 既定の範囲の Σh は約 0.93ms で、1 秒の上限の 1000 分の 1 に届かない。
- **R-2 (a)**: 別のプロセス(`keyfree_measure.py hold`)が持つ Ctrl+Alt+Shift+J を `keyfree check` すると 20、返した後は 0。
  別のプロセスが `MOD_NOREPEAT` を付けても付けなくても同じ(`test_real.py`、2 通りとも合格)。→ 撤退の条件には当たらない。
- 配列: この PC は JIS 配列。記号キー12個すべてに文字があった(S=0)。`VK_OEM_1`=`:`、`PLUS`=`;`、`OEM_3`=`@`、`OEM_7`=`^`、`OEM_5` と `OEM_102` はどちらも `\`。
- 既定の範囲で「空き」でなかったのは Ctrl+Alt+Tab・Alt+Shift+Tab・Ctrl+Alt+Shift+Tab の3つ(使用中)。include_win では 956 組中 119 組が使用中。
- **Ctrl+Alt+Space**: 今日は「空き」(エラーなし)。日報 2026-09-25 の 1409 は、この時点では再現しなかった(持っていたアプリが今は動いていないと見られる)。
- 代表の組(ターミナルから = 別のアプリが前): Win+E 使用中・Win+Alt+K 使用中・Win+Shift+F9 空き・Ctrl+PrintScreen 空き・Ctrl+Alt+Space 空き・
  Ctrl+Alt+Shift+K 空き。DeskKit の画面が前のときの結果と「押して確かめる」で届くかは実機で(§5)。
- Chrome 拡張の候補: Alt+Shift+C・Q・M・N は4つとも「空き」(どこでも効くキーとしては使われていない。Chrome の中の重なりは別に `chrome://extensions/shortcuts` で見る)。
  SourceStamp の Q-16 と TimeNote §8.6 へ渡す値。
- 画面(NFR4-4。偽の probe で同じバッチ数を流し、画面に出さずに測った): 表を作る最初の処理 69ms(既定)・71ms(include_win。KF-17 の分割の後。
  分割前は 145ms / 200ms)、バッチごとの画面の更新は最大 35ms。probe 自体は host のスレッドで動くので GUI を止めない。

## 5. 実機で確かめること

- AC-12 の残り: 一覧の前後で、DeskKit の既存のホットキー(クイックアクションなど)を **実際に押して** 動くか。
- DeskKit の画面が前のときの代表の6組(Win+E・Win+Alt+K・Win+Shift+F9・Ctrl+PrintScreen・Ctrl+Alt+Space・Ctrl+Alt+Shift+K)の結果と、
  「押して確かめる」で届いたか(§8 の表の残りの列)。特に PrintScreen の組(K-5 の「上書きで登録できてしまう」)。
- 一覧の間に Ctrl+Alt+Shift+K を押し続けたとき、FR-7 の文言が出るか(一覧は 3ms ほどで終わるので、押し続けても重ならないことが多いと見込む)。
- 押して確かめるで Ctrl+Shift+S を預かり、メモ帳を前にして押したとき、DeskKit に届くか・メモ帳も反応するか(K-3 の前提)。
- 押して確かめるの途中で DeskKit をタスクマネージャーで終わらせ、起動し直して同じ組を調べると「空き」に戻るか。
- エクスプローラーのショートカット(.lnk)の「ショートカット キー」に Ctrl+Alt+Shift+L を入れたとき「使用中」と出るか(§4 の未確認。利用者の設定を変えるので実装者はしていない)。
- PowerToys の Keyboard Manager で割り当てた組がどう出るか(この PC では確かめていない)。
- exe で、ターミナルから `DeskKit keyfree scan` / `keyfree check Ctrl+Alt+K` の終了コードと出力(IPC 越し)。
- 管理者として動いている窓が前にあると「調べる」が止まる(KF-5)。困る場面があるか。

## 6. 契約変更の提案

- **P-1(ModuleHotkeys の一時の登録)**: 押して確かめるで `ctx.hotkeys.register("try_key", …)` が 1409 で失敗すると、`ModuleHotkeys.register` が
  `hub.note_conflict` を呼ぶため、`keyfree.try_key` が本体の「登録できなかったキー」(`HotkeyHub.failed` / `conflicts`。診断レポートと、再読み込みのときの
  まとめての通知)に残る。`unregister(name)` は `failed_keys` だけを消し、`failed` と `conflicts` は消さない。
  提案: `unregister(name)` で `failed` と `conflicts` の該当も消すか、`register(..., report=False)` のような一時の登録を足す。
- **P-2(表示名)**: ホットキーの登録名 → 表示名の対応(`HOTKEY_LABELS` / `hotkey_label`)は `deskkit.ui.main_window` にある。モジュールから使うには
  `deskkit.hotkeys` か `deskkit.ui` の小さな部品に移すのがよい(KeyFree は今 main_window を遅れて import している)。
- **P-3(測定用のスクリプト)**: 仕様書の `tools/keyfree_measure.py` を tools に置くなら本体担当の範囲。今は `tests/modules/keyfree/keyfree_measure.py`(KF-1)。

## 7. 利用者に聞くこと

- 仕様書 §12 の Q-1〜Q-4 は未回答のまま、どれも「v0.4 では」の案で作った(裏のゲームは見ない・様子見なし・Windows キーは設定でオン・本体のキーは変えない)。
- KF-2: `keyfree scan` を設定の `include_win` に合わせるか(今は常に既定の範囲)。
- KF-5: 管理者の窓が前のときも止める今の作りでよいか。
- KF-15: おすすめを1つの修飾キーの組で埋める(Ctrl+Alt+Shift+A〜H)か、組を混ぜるか。

### 対応(2026-09-29、統合のとき)

- P-1: `HotkeyHub.drop`(`ModuleHotkeys.unregister` から呼ぶ)で `failed` と `conflicts` の該当も消すようにした(`test_unregister_clears_conflict_records`)。
- P-2: `HOTKEY_LABELS` と `hotkey_label` を `deskkit.hotkeys` へ移した。`deskkit.ui.main_window` からも今までどおり読める。KeyFree の画面は `deskkit.hotkeys` から読む。
- P-3: 今のまま(tests の下)。
- §7 は利用者の答え「全部今の案で出す」。
