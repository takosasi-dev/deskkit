# ModeShift v0.2.0 の変更点(仕様書からの差分)

仕様書: ModeShift 作業モード切替 仕様書 v1(リポジトリ外)。担当間の取り決めは `docs/INTERFACES_v0.2.md`。
この文書は v0.2.0 で仕様書から外れた点・追加した点と、その理由・既定値をまとめる。

## 1. 不具合修正: 強制終了が PID の再利用で別のプロセスに当たりうる

| | 内容 |
|---|---|
| 症状 | FR-14 の確認ダイアログ(最長 10 分待つ)の間に対象が自分で終わり、Windows が同じ PID を別のプロセスに使うと、承認時に PID だけで `OpenProcess` → `TerminateProcess` していたため無関係なプロセスを終了させうる(未保存データの消失) |
| 修正 | 確認ダイアログを出す**前に** `PROCESS_TERMINATE \| SYNCHRONIZE \| PROCESS_QUERY_LIMITED_INFORMATION` でハンドルを開き、そのハンドルで exe 名を確かめる。承認後は PID ではなく**同じハンドル**で終了させる。ハンドルを持っている間はプロセスオブジェクトが残るので PID は再利用されない。ハンドルはすべての経路(承認・拒否・中断・既に終了)で閉じる |
| 形 | `ProcessApi.force_terminate_confirmed(pid)` を `ProcessApi.open_for_force(pid, exe) -> ForceTarget \| None` に置き換えた。`TerminateProcess` を呼ぶのは `ForceHandle.terminate_confirmed()` の1か所だけ(AC-14 の grep は1件) |
| 開けないとき | 昇格プロセスなどでハンドルを開けなければ、確認も強制終了もせず `still_running` のまま(従来どおり回避しない。C-5) |

## 2. 追加したアクション種別(仕様書 §3 の 8 種類 → 10 種類)

仕様書 §0 は「追加のアクション種別は提案にとどめる」だが、v0.2.0 の計画で利用者が追加を決めた。どちらも**公開 API・HKCU だけ**で、管理者権限は要らない。

### 2.1 `mic_volume`(マイク)

```json
{ "type": "mic_volume", "level": null, "mute": true }
```

- 既定の録音デバイス(`IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture=1, eMultimedia)`)の `IAudioEndpointVolume` を、`master_volume` と同じ ctypes の vtable 呼び出しで使う(新しい vtable 番号は無い。`EDataFlow` を変えるだけ)。
- `level`(0.0〜1.0 / null)と `mute`(true / false / null)。少なくとも一方が必要。検証は `master_volume` と同じ。
- **元に戻す対象**。snapshot に `"mic": {"before", "written", "device_id"}` を残し、D-6 と同じく「書いた値のままなら戻す・手で変えていたら『手動で変更済み』」。既定の録音デバイスが変わっていれば戻さない(「既定の録音デバイスが変わった」)。
- ゲーム中も実行する(前面のウィンドウに作用しないため。電源・音量と同じ扱い)。
- 理由: 「会議の前にミュート解除/作業中はミュート」を1操作でまとめたい。値として読んで書き戻せるので D-5 の考え方(戻せるものだけ戻す)に合う。

### 2.2 `theme`(アプリのテーマ)

```json
{ "type": "theme", "apps": "dark", "system": null }
```

- `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` の `AppsUseLightTheme`(`apps`)と、任意で `SystemUsesLightTheme`(`system`)を REG_DWORD で書く(1 = ライト / 0 = ダーク)。`"dark"` / `"light"` / null(変えない)。少なくとも一方が必要。
- 書いた後 `SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 1000ms)` で開いているアプリに知らせる(応答しないウィンドウは待たない)。送り切れなければ結果に「一部のアプリには変更を知らせられなかった(再起動で反映)」と注記する(失敗にはしない)。書いた値は読み戻して一致を確認する。
- **元に戻す対象**。snapshot に `"theme": {"before", "written", "keys"}` を残し、書いた項目(`keys`)だけを戻す。切替前の値が無かった(キーが無い)項目は戻さない(推測で埋めない)。
- **ゲーム中はスキップ**(切替も元に戻すも)。`WM_SETTINGCHANGE` は前面のゲームのウィンドウにも届くため(C-4 / D-10 と同じ扱い。`FOCUS_TYPES` に入れた)。
- 理由: 作業モード(夜の勉強=ダーク 等)と見た目を一緒に切り替えたい。レジストリの値として読んで書き戻せる。

### 2.3 表示

- プレビュー・CLI の `--dry-run`(タブ区切り)・履歴に、種別「マイク」「アプリのテーマ」、現在値(例: `80%・ミュートなし` / `アプリ: ライト・Windows: ダーク`)と変更後の値が出る。
- 編集画面の「追加」メニューに2種類を足し、それぞれの入力フォーム(マイク: 音量スライダー+ミュート / テーマ: アプリ・Windows のドロップダウン)を用意した。

## 3. 自動切替のきっかけに電源(AC ⇄ バッテリー)を追加

```json
"auto_switch": {
  "enabled": true, "poll_interval_s": null,
  "rules": [
    { "trigger": "on_battery", "mode": "eco", "on_exit": "undo" },
    { "exe": "game.exe", "mode": "game", "on_exit": "undo" }
  ]
}
```

- ルールに任意の `trigger` を足した: `"exe"`(省略時。従来どおり)/ `"on_battery"`(AC → バッテリーで適用)/ `"on_ac"`(バッテリー → AC で適用)。形・`mode`・`on_exit` は既存のルールと同じ。`on_exit: "undo"` なら逆向きの切り替わり(`on_battery` なら AC に戻ったとき)で、直前の切替がそのモードのときだけ元に戻す(exe のルールと同じ判断)。
- 検知は host の隠しウィンドウの `WM_POWERBROADCAST`(0x0218)を `ctx.on_native` で購読し、`PBT_APMPOWERSTATUSCHANGE`(0x000A)と復帰(`PBT_APMRESUMESUSPEND` / `PBT_APMRESUMEAUTOMATIC`)で `GetSystemPowerStatus` の `ACLineStatus` を読み直す。ポーリングしない。
- **起動時の状態はきっかけにしない**(exe の「起動時に既に動いているもの」と同じ)。`ACLineStatus` が不明(255)のときは何もしない。
- 同じ電源のきっかけのルールは1つだけ(2つ目は理由つきで無効)。
- 切り替わった先に適用するルールがあれば(例: `on_battery` と `on_ac` の両方がある)、そちらの適用を優先し、逆側の「元に戻す」は頼まない(1段の undo と切替を続けて動かすと、後の要求が実行中として拒否されるため)。
- `poll_interval_s` は exe のきっかけ用。電源のルールだけなら null のままで動く。exe のルールがあって null のときは従来どおり設定エラー(exe の監視だけが止まり、電源のルールは動く)。ルールが1つも無く有効にしたときも従来どおり設定エラー。
- 既存の決まりはそのまま: 未確認のモードは自動では実行せず通知だけ(FR-8)/ゲーム・全画面が前面ならフォーカスに作用する手順はスキップ(D-10)/ops.jsonl の `source` は `auto`。
- 画面: 自動切替のルール表に「きっかけ」列を足した(電源のきっかけでは exe 欄を使わない)。「電源のルールを追加」ボタンと、電源のルールがあるときは今の電源(AC / バッテリー)を表示する。

## 4. 一時停止(スヌーズ)中は自動で切り替えない

- 契約 §1 の `ctx.is_snoozed()` が True の間、自動切替(exe・電源のどちらのきっかけでも)の**適用と元に戻す**をしない(ログに1行だけ。通知しない)。トレイ・ホットキー・画面・CLI からの操作は止めない。
- ctx に `is_snoozed` が無い(古い host)・例外のときは「止めない」側に倒す(従来の動作)。
- 一時停止が明けても、止めていた間のきっかけをさかのぼって実行はしない。

## 5. イベント

| イベント | 変更 |
|---|---|
| `modeshift.reverted`(新規・送信) | 元に戻す(手動・自動切替の on_exit・CLI)の実行が終わったとき `{"mode": <戻す前のモード名>, "run_id": <元に戻す操作の run_id>}`。dry-run・記録なし(終了コード 7)・計画の失敗では送らない。戻す項目が0件でも、実行が終われば送る(状態上のモードは戻るため) |
| `layout.apply`(拡張・送信) | `layout_apply` アクションに任意の `"preset"`(1〜100 文字)。指定したときだけ payload に `"preset"` を足す。未指定のときは設定の正規形にも payload にもキーを持たない |

| `modeshift.switched`(既存・再起動時に1回) | 起動時、前回切り替えてまだ元に戻していないモードがあれば `{"mode": <名前>, "run_id": <前回の run_id>, "failed": 0, "restored": true}` を1回だけ送る(契約への追加。統合担当が承認)。`ctx.call_soon` で遅らせ、全モジュールの購読が済んでから送る。通知・アクションの実行・ops.jsonl の記録はしない。判定は state.json の `current_mode` / `last_run_id` と snapshot.json(同じ run_id の切替で、`undone_at` が無い)。モードが設定から消えていれば送らない |

- restored の理由: DropSort・ClipShelf の「このモード中は止める」は `modeshift.switched` を受けて止まるが、DeskKit を再起動すると今のモードを知る手段が無かった。受け手は `restored` を見なくても通常の `switched` と同じに扱ってよい。
- `preset` を未指定のときにキーを持たないのは、正規形にキーが増えると v0.1 で確認済みのモードの定義ハッシュが変わり、全部「未確認」に戻ってしまうため。

## 6. diagnostics()

Module に `diagnostics()` を足した(契約 §1)。返すのは件数・モード名・真偽・結果コードだけで、exe 名・パス・URL・ウィンドウタイトルは入れない。

| キー | 内容 |
|---|---|
| `running` | サービスが動いているか |
| `modes` / `modes_valid` / `modes_unconfirmed` | モード数 / 有効なモード数 / 有効だが未確認のモード数 |
| `current_mode` | 現在のモード名(`name`。無ければ空文字) |
| `busy` / `undo_available` | 切替中か / 元に戻せる記録があるか |
| `last_result` | 直前の実行の結果: `none` / `ok` / `partial`(失敗・終了せず・中断あり)/ `error`(例外) |
| `preview_policy` / `allow_force_kill` | 動作設定 |
| `auto_switch_enabled` / `auto_switch_active` | 自動切替が有効か / 実際に動いているか |
| `auto_rules_exe` / `auto_rules_power` / `auto_rules_invalid` | ルールの件数 |

## 7. 既定値

| 項目 | 既定値 | 理由 |
|---|---|---|
| 新しいアクション | 無し(利用者が追加したときだけ) | 新機能は既定でオフ(契約 §0) |
| `mic_volume` の新規フォーム | `level: null, mute: true` | よくある使い方(ミュートするだけ)を最初に出す |
| `theme` の新規フォーム | `apps: "dark", system: null` | 仕様の主目的はアプリのテーマ。Windows 側は任意 |
| 自動切替 | `enabled: false`、ルール無し | 従来どおり。電源のルールも利用者が足すまで動かない |
| `WM_SETTINGCHANGE` の待ち時間 | ウィンドウ1つあたり 1000 ms(`SMTO_ABORTIFHUNG`) | 応答しないアプリで切替全体を止めない |

## 8. 仕様書との食い違い(報告)

- 仕様書 D-5 / INV-4 の「元に戻す対象は電源プランと音量だけ」を、マイク・テーマまで広げた。どちらも「値として読んで書き戻せるもの」で、アプリの起動・終了はしない(INV-4 の趣旨は守る)。

## 9. 利用者の判断: ドメインのみ(URL のログ)

- 食い違い: ModeShift 仕様書 INV-11 は「開いた URL のクエリ文字列は書かない」(= パスまでは可)、`docs/INTERFACES_v0.2.md` §0・共通基盤 INV-7(C-12)は「ログに URL を書かない」。
- **利用者の判断: ドメインのみ。** `open_url` について、保存されるもの(ops.jsonl の本番・dry-run の行。画面の履歴もここから読む)には**ホスト名だけ**を書く。小文字にし、スキーム・ユーザー情報(`user:pw@`)・**ポートも落とす**(ポートは識別にほぼ役立たず、ローカルの開発サーバー等の構成が漏れるだけのため)。IPv6 リテラルは `[::1]` の形。例: `https://Example.com:8443/page?token=x` → `example.com`。
- プレビュー窓・CLI の `--dry-run` の標準出力(保存されない、利用者に見せる表示)は従来どおりクエリを落とした URL(`https://example.com/page?…`)のまま。
- host のログ(deskkit.log)に ModeShift が URL を書く箇所は無い。
- 実装: `config.url_host()`、`oplog.log_target()`。
