# StartupWatch v0.4.0 — 仕様書からの変更点と実測値

対象: `deskkit/modules/startupwatch/**`。StartupWatch 実装仕様書 v1・v0.4 共通仕様書・`docs/MODULE_GUIDE.md`・`docs/INTERFACES_v0.4.md` からの差分と、§8 の実測値だけを書く。
逸脱不可要件(INV-1〜INV-7・V4INV-1〜5)は変えていない。本体(host)の変更には頼っていない(仕様書 §2「本体に頼むこと」は host API の追加なし)。

## 1. ファイル構成(§2 からの追加)

| ファイル | 役割 |
|---|---|
| `fakes.py` | テストと自己検査で使う偽の Win32(`_win32.Api` と同じ形)。レジストリ・フォルダの中身を辞書で持ち、合図を「偽の時計の時刻 + 場所の記号」の台本で送る。開いたハンドルを数え、stop の後に全部閉じたかを確かめられる |

`_win32.py` は、値の読み取りに標準の `winreg`(`OpenKeyEx` に `KEY_READ | 見え方` を付けて開き、`EnumValue` で読む)を、変更の知らせに ctypes の `RegOpenKeyExW`・`RegNotifyChangeKeyValue` を使う。どちらも `KEY_READ`(0x20019)だけで開く(INV-2)。

## 2. 変更点・解釈

| # | 内容 | 理由 |
|---|---|---|
| S-1 | 鍵の合図が来たら、その鍵を **閉じて開き直して** から知らせを掛け直す(同じハンドルで掛け直さない)。開き直せなければ poll に回す | §10「見張っている鍵が消された・作り直された → 開き直す」を、消されたかどうかを調べずに毎回満たすため。前の待ちは合図で終わっているので、資源はもれない |
| S-2 | settle の打ち切り(最初の合図から 10 秒・FR-3)の後にまだ合図が続いていれば、次のまとまりとしてもう一度待って読み直す | 20 秒続くインストーラーの書き込みでも、途中で1回・終わりで1回の読み直しで済む(知らせは、増えた物があった回だけ) |
| S-3 | **手で頼んだ読み直し**(Hero の「読み直す」・CLI `rescan`)は一時停止中でも読む。一時停止中に止めるのは、自動の読み直し(起動時・合図・poll)だけ | MODULE_GUIDE §3「手で押した操作・CLI は止めない」。W-11 は自動の処理についての決まりと読んだ |
| S-4 | 知らせない追加(RunOnce で `notify_runonce` が false・DeskKit 自身)は、記録に「知っている」として入れ、「新しい」の印を付けない。表では「1回だけ」「DeskKit」の印で出す | 知らせないのに「新しく増えた物」のカードに残り続けると、確かめる手間だけが増えるため |
| S-5 | 「新しく増えた物」のカードには、**中身が変わった物も** 出す(印「中身が変わりました」)。「確かめた」「すべて確かめた」で、どちらの印も消える | 仕様書に「中身が変わりました」の印を消す操作が無かったため。知らせはしない(W-4 のまま) |
| S-6 | W-10 の「DeskKit」: `sys.executable` に加えて、ソース実行(`python.exe` / `pythonw.exe`)のときは同じフォルダの `pythonw.exe` と `python.exe` も DeskKit とみなす | 本体はソース実行の自動起動を `pythonw.exe -m deskkit --autostart` で登録する(`deskkit/paths.py`)。exe の実行では `sys.executable` だけ |
| S-7 | スタートアップ フォルダの .lnk 以外のファイル(.bat など)は、「何を起動するか」にそのファイルのフルパスを出し、比べる中身もそのパスにする | 仕様書は .lnk だけを書いていたため |
| S-8 | `events.jsonl` の `added` / `changed` / `removed` は、1回の読み直しにつき種類ごとに1行。`loc` は全部が同じ場所ならその記号、混ざっていれば null。usage の「知らせた数」は `added` で `notified: true` の行の数(=通知の回数) | §9 の形のまま、知らせた回数を数えられるようにするため |
| S-9 | usage の「画面を開いた回数」は、ページが最初に表示されたとき(ページ1つにつき1回)に `open` を1行書いて数える | ページはモジュールの起動し直しでも作り直されるので、作ったときではなく表示したときに数える |
| S-10 | 画面に「見ている場所」のカード(8か所それぞれ: 件数と「変わるとすぐに気づけます」/「この場所は N 分ごとに確かめています」/「読めませんでした(管理者でないと…)」)を足した。HKLM の2つの見え方は、画面の名前の後ろに「(64 ビット)」「(32 ビット)」を付けた | FR-4 の「画面のその場所に」を出す場所として。§2 の表では 64/32 の画面の名前が同じで、並べると見分けられないため |
| S-11 | 開けない場所(5 など)も poll の対象にする(間隔ごとに開き直す) | 権限が変わったり鍵が作られたりしたときに、再起動なしで見張りに戻れるため |
| S-12 | 見張りのスレッドが例外で2回止まったら、`threading.Event` で `poll_minutes` ごとに起きる読み直しだけで続ける(Win32 の待ちを使わない) | §10。待ちそのものが壊れていても続けられるように |
| S-13 | `known.json` の中身が1つでも形に合わない(ハッシュでない鍵・知らない印など)ときは、壊れているとみなして覚え直す(FR-17) | 部分的に読むと、消えた物・増えた物を取り違えるため |
| S-14 | 画面から変えられる設定は `notify_runonce`・`show_running`・`poll_minutes`。`poll_minutes` を変えたらモジュールを起動し直す。`settle_ms` は settings.json でだけ変えられる | settle_ms は利用者が気にする値ではないため(範囲の確認は §9 のとおり) |
| S-15 | 読み直しの結果は、見張りのスレッドから `ctx.call_soon` で GUI スレッドへ渡し、差分・記録・通知は GUI スレッドで行う。「動いているか」(FR-13)の読み取りは短いスレッドで行う | §2 の処理の流れのとおり |
| S-16 | 仕様書 §8 の測定用の `tools/startupwatch_measure.py` は作っていない(`tools/**` は本体担当の範囲)。同じ確かめを `tests/modules/startupwatch/test_real.py::test_ac14_hkcu_run_and_startup_folder_notify_within_10s`(`win32_real`)に書いた。HKCU の Run に試しの値、利用者のスタートアップ フォルダに試しの .txt を置き、finally で必ず消す(試しの値は存在しないプログラムを指す) | 契約の担当表。実行はしていない(利用者の自動起動に書き込むため。§4) |

### ログに書くもの(INV-4)

`startupwatch start/stop`・`baseline n=` / `rebaseline n=`・`diff reason=<理由> added= changed= removed= notified=`・`ack n=`・`arm loc=<記号> rc=<番号>`・`notify loc=<記号> rc=<番号>`・`settings_fixed key=<キー>`・`watcher failed: <型名> (n=)`・`scan failed: <型名>`。名前・コマンド・パスは書かない(`test_ac10_*` で確かめている)。

### diagnostics() のキー(FR-20)

`record`・`known`・`new`・`changed`・`last_scan_ms`・`watch_degraded`・`watch.<場所の記号>`(`notify` / `poll` / `unreadable`)・`count.<場所の記号>`。

## 3. 決めた既定値

§9 のまま(`notify_runonce: false`・`show_running: true`・`poll_minutes: 15`・`settle_ms: 2000`)。範囲外は既定値に戻して書き戻し、ログに `settings_fixed key=` を1行。

## 4. §8 の実測(2026-09-28、この PC: Windows 11 Pro 10.0.26200・管理者でない利用者・ソース実行)

読むだけの測定(`_win32.RealApi` を直接呼ぶ使い捨てのスクリプト。名前・コマンドは出していない)。

| 場所 | 開けるか(読み取り) | 知らせを置けるか | 件数 |
|---|---|---|---|
| hkcu_run | 0(開けた) | RegNotifyChangeKeyValue = 0 | 6 |
| hkcu_runonce | 0 | 0 | 0 |
| hklm_run64 | 0 | 0 | 2 |
| hklm_run32 | 0 | 0 | 4 |
| hklm_runonce64 | 0 | 0 | 1 |
| hklm_runonce32 | 0 | 0 | 0 |
| startup_user | 既知フォルダあり・ローカル | FindFirstChangeNotificationW 成功 | 0 |
| startup_common | 既知フォルダあり・ローカル | 成功 | 0 |

- **管理者でない利用者で、8か所とも読めて、知らせも置けた**(§4 の未確認の点)。よってこの PC では poll は起きず、見張りのスレッドは `WaitForMultipleObjects(INFINITE)` で待つだけ(INV-7。テスト `test_inv7_waits_forever_when_everything_is_notified`)。
- 8か所を1回読む時間: 中央値 0.39 ms・最大 1.15 ms(50 回)。
- W-1 の【推測】の確かめ(数だけ): Run と RunOnce に 13 件、スタートアップ フォルダは 0 件。
- 本物の host(ソース実行、一時フォルダの DESKKIT_HOME)で有効にして起動し、画面を開けること・初回に「今ある 13 個を覚えました」と出ることを確かめた(`show_running` は切って撮影。下の §5)。
- 本体の「最近の通知」(Q-1 の材料): `host.activities` はメモリの一覧(最新 60 件)で、持つのは時刻・モジュール・**題**・level だけ(本文を持たない)。ディスクには書かない(`deskkit/host.py` を読んで確かめた)。本体のログにも `notify source= level=` だけが出る。

## 5. 実機で確かめること(未実施)

- **AC-14**: `pytest -m win32_real tests/modules/startupwatch -k ac14` で、HKCU の Run とスタートアップ フォルダへの追加から通知までの時間(10 秒以内・3 回の中央値)。試しの物は finally で消える。利用者の自動起動に一時的に書き込むので、統合担当か作者が実行する
- W-2: 管理者のターミナルで `reg add HKLM\Software\Microsoft\Windows\CurrentVersion\Run /v DeskKitTest /d x /reg:32` → `hklm_run32` にだけ出ること(終わったら `reg delete ... /reg:32`)
- 何もしない1時間の見張りのスレッドの CPU 時間(設計上は待つだけ。この PC は全部 notify なので起きない)
- FR-13 の「動いているか」の読み取りの時間(依頼の指示でプロセスの一覧を読んでいないため未測定。偽物のテストだけ)
- Windows のスタートアップ アプリの通知(R-1): 設定 > システム > 通知 にあるか・既定でオンか・HKCU の Run とフォルダの追加で出るか
- 設定とタスク マネージャーの「スタートアップ アプリ」に8か所のどれが出るか・オフにしたとき値が変わるか(PcCheckup §8 と一緒に)
- `QFileInfo.symLinkTarget()` が、引数つきの .lnk・先の無い .lnk・インストーラーが作る特別なショートカットで何を返すか(この PC のスタートアップ フォルダには .lnk が無かった)
- DeskKit 自身の自動起動が「DeskKit」になるか(exe とソースの両方。この PC では DeskKit の自動起動が登録されていなかった)

## 6. 契約変更の提案

なし。

### 回答(2026-09-29、統合のとき)

- 利用者の答え: 上の「聞くこと」はすべて今の案のまま v0.4.0 に入れる。
