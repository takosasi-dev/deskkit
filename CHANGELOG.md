# Changelog

## [0.1.0] - 2026-09-24

### Added
- host(常駐本体): 単一インスタンス、トレイ、設定(settings.json の原子的書き込み・セクション単位の検証)、ログ(日次ローテーション・faulthandler)、
  隠しウィンドウによるシステムメッセージ配布、ホットキー(OverlayKit 互換 HotkeyRegistry を同梱)、前面ウィンドウ情報(ゲーム・全画面・昇格の判定)、
  イベントバス、CLI 転送(名前付きパイプ)、自動起動(HKCU Run)、モジュールの隔離(例外で1つだけ停止)、`--selftest`。
- Control Center(GUI): ホーム(モジュールカード・前面状態・通知履歴・ホットキー一覧)、各モジュール画面、共通設定、ログ表示。通知トースト。
- モジュール: ModeShift / DropSort / LayoutKeep / ClipShelf(初期状態はすべて無効。有効化後は試運転から始まる)。
- 単体 exe(PyInstaller onefile)のビルドスクリプト `build.ps1`(DeskKit.exe.sha256 も出力)。
- クイックアクション(Ctrl+Alt+Space の検索窓。トレイ項目と各モジュールの追加操作を名前で実行)。
- 利用状況ページ(モジュールごとの日別件数の棒グラフ/表、7/30/90日)。
- GitHub Releases からの自動更新(確認→通知→ワンクリック更新、SHA-256 検証、試し起動、前の版に戻す)。
- 設定のバックアップ/復元、はじめてガイド、ライトテーマと Windows 連動。
