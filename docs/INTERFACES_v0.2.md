# v0.2.0 担当間の取り決め(契約)

v0.2.0 は本体(host)と4モジュールを5人で並行して作る。担当をまたぐものはこの文書だけで決める。
ここに無いものを他の担当に期待しない。変えたくなったら自分で変えず、最終報告に「契約変更の提案」として書く。

## 0. 共通

- 担当は自分の範囲のファイルだけを変更する(下の表)。範囲外のファイル(特に `deskkit/context.py`・`deskkit/events.py`・`deskkit/host.py`)はモジュール担当は触らない。
- 逸脱不可要件はそのまま: フック禁止(RegisterHotKey のみ。SetWinEventHook も使わない)/管理者権限なし/利用者ファイルの恒久削除なし/ログに本文・URL・ウィンドウタイトルを書かない/ゲーム・全画面が前面の間は前面に作用しない/通信は updater.py だけ/モジュール同士は import しない。
- 新機能は既定でオフ、または試運転(dry-run / observe)から。自動で何かを動かすものは、利用者がオンにするまで動かない。
- 各担当はテストを足し、`pytest`・`ruff check`・`mypy`(strict)・`python -m deskkit --selftest <自分>` を通してから終える。
- 仕様書から外れる点は `docs/v0.2/<担当>.md` に「変更点・理由・既定値」を書く(MODULE_GUIDE.md は本体担当だけが編集し、最後に統合する)。

| 担当 | 範囲 |
|---|---|
| host | `deskkit/*.py`(modules 以外)・`deskkit/ui/**`・`deskkit/selftest*`・`tests/test_host.py` ほか `tests/*.py`・`docs/MODULE_GUIDE.md` |
| modeshift / dropsort / layoutkeep / clipshelf | `deskkit/modules/<自分>/**`・`tests/modules/<自分>/**`・`docs/v0.2/<自分>.md` |

## 1. 本体が新しく提供する ctx API(host が実装、モジュールは使うだけ)

```python
ctx.is_snoozed() -> bool
```
- 一時停止(スヌーズ)中なら True。**自動で動く処理の入口**でモジュールが見る。利用者が手で押した操作(ボタン・クイックアクション・CLI)は止めない。
- 見る場所の目安: ModeShift の自動切替(プロセス・電源のトリガー)/DropSort の自動移動/LayoutKeep の自動適用・自動スナップショット・新規ウィンドウ配置/ClipShelf の記録。
- モジュール側のテスト用 Fake ctx には `is_snoozed()`(既定 False)を自分で足す。

```python
ctx.list_modes() -> list[tuple[str, str]]
```
- ModeShift の設定にあるモードの `(name, label)` 一覧(settings の `modeshift.modes[*].name/label` を本体が読むだけ。ModeShift が無効でも返す。壊れた要素は飛ばす)。
- DropSort・ClipShelf の「このモード中は止める」設定の選択肢に使う。

```python
# モジュールオブジェクトに任意で生やすメソッド(無ければ本体は何もしない)
def diagnostics(self) -> dict[str, str | int | bool]: ...
```
- 診断レポート(H4)に載せる、モジュールの状態の要約。**本文・パス・URL・ウィンドウタイトル・exe 名を入れない**(件数・モード・真偽・理由コードだけ)。

## 2. イベント(`events.py` の REGISTERED_EVENTS に本体担当が追加する)

| イベント | 送信 | payload | 受信 |
|---|---|---|---|
| `modeshift.switched`(既存) | ModeShift | `{"mode": str, "run_id": str, "failed": int}` | DropSort・ClipShelf(新規) |
| `modeshift.reverted`(新規) | ModeShift。元に戻す(手動・自動切替の on_exit・CLI)が終わったとき | `{"mode": str, "run_id": str}`(戻す前のモード名) | DropSort・ClipShelf |
| `host.snooze_changed`(新規) | 本体 | `{"snoozed": bool, "until": str \| None}`(ISO 8601、無期限は None) | 必要なモジュール(例: DropSort は再開時にフルスキャン) |
| `layout.apply`(既存・拡張) | ModeShift | 既存の payload に任意の `"preset": str` を追加 | LayoutKeep |
| `layout.applied`(既存) | LayoutKeep | 変更なし(`preset` を受けた場合は `"preset"` をそのまま返す) | ModeShift |

- 「このモード中は止める」(M3): 受信側は `modeshift.switched` で `mode` が設定のリストに入っていれば停止、`modeshift.reverted` か、リスト外のモードへの `switched` で再開する。止めた・再開したは通知せず、状態表示(ページ・トレイの状態文)だけに出す。
- 再起動時(統合で追加): ModeShift は起動直後、前回切り替えて元に戻していないモードがあれば、全モジュールの購読が済んでから `modeshift.switched` を `{"mode", "run_id", "failed": 0, "restored": true}` で1回だけ送る(通知・実行・記録はしない)。受信側は通常の switched と同じに扱う。
- `modeshift.reverted` の `run_id` は「元に戻す操作」の run_id(ops.jsonl の行と一致)。
- LayoutKeep のプリセット名 `自動` と `抜く前` は予約語(自動スナップショットの枠)。
- `preset` を受けた LayoutKeep は、現在の構成シグネチャのプリセット名で探す。無ければ `result: "no_layout"`。構成が違うものは適用しない(INV-3 のまま)。

## 3. 本体が全体に効かせるもの(モジュールは何もしなくてよい)

- **通知の保留(H1)**: `ctx.notify` は、前面がゲーム・全画面の間、level が `error` 以外なら保留し、前面が安全になったら「保留中の通知 N 件」を1件にまとめて出す(最新の数件のタイトルを本文に)。モジュール側の既存の保留(DropSort の notifier)は残してよい(二重でも害はない)。
- **スピンボックス・コンボボックス・スライダーのホイール対策(UX-1)**: 本体がアプリ全体にイベントフィルタを入れ、フォーカスの無いこれらの部品ではホイールを親のスクロールに回す。モジュールの対応は不要。
- **設定の自動世代保存(H3)**: settings.json を書くたびに本体が世代を残す。モジュールの対応は不要。
