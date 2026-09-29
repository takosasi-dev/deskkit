# PcCheckup v0.4.0 — Secure Boot の証明書の確認の追加

対象: `deskkit/modules/pccheckup/**`。仕様書は `PcCheckup_SecureBoot証明書の確認_追加仕様書.md`(B-* / SB-*)。
ここには、仕様書からの変更点・決めた既定値・§8 の実測値・実機で確かめることだけを書く。v0.3 の分は `docs/v0.3/pccheckup.md`。
逸脱不可要件(SB-INV-1〜6)は変えていない。

## 1. ファイル

| ファイル | 変更 |
|---|---|
| `checks/boot.py`(新規) | B1・B2 の判定(`judge_b1` / `judge_b2(raw, b1_reason, today)`)、理由コード、コピーに付ける生の値、`PCA2011_EXPIRY = date(2026, 10, 19)` |
| `probes.py` | `RegRead` / `SecureBootRaw`、`read_reg()`(`KEY_READ` だけ)、`read_secure_boot()`、`Probes.secure_boot()`(`RealProbes` は1回の診断で1回だけ読む) |
| `_win32.py` | `firmware_type()`(GetFirmwareType。argtypes・restype を設定) |
| `runner.py` | `RUNNABLE`(3カテゴリ + `boot`)と `HISTORY_IDS`(履歴に書いてよい ID に B1・B2) |
| `checks/base.py` / `history.py` | category `boot`(表示名「起動の安全」)を受け付ける |
| `report.py` | `build(..., suffixes=)`: チェック ID ごとに見出しの行の末尾へ文字列を足す(SB-FR-5) |
| `module.py` | `secureboot_check`(既定 true)・`run_all()`・クイックアクション・`ALLOWED_URIS` に2つ・`diagnostics()` に3キー・`set_secureboot_check()` |
| `page.py` | 「起動の安全を調べる」の小さなボタン・「起動の安全の確認」の設定カード |
| `fakes.py` / `selftest.py` | 偽の `secure_boot()`(`sb_raw()`)と、B1・B2 の判定表の自己検査 |

## 2. 仕様書からの変更点・解釈

| # | 内容 | 理由 |
|---|---|---|
| SB-D-1 | 「結果をコピー」の B1・B2 の行は、既存の行と同じ形(`[注意] 見出し: 値`)の末尾に生の値を付けた。例: `[注意] 起動の証明書がまだ古いままです: まだ古い(起動方式=UEFI / UEFISecureBootEnabled=1 / UEFICA2023Status=NotStarted / UEFICA2023Error=なし / WindowsUEFICA2023Capable=1)`。B1 の行は `起動方式` と `UEFISecureBootEnabled` だけ。BIOS の PC は B1・B2 とも `(起動方式=BIOS)` だけ(B2 は値を使わないため) | SB-FR-5 の例(`B2 注意 …`)は既存のコピーの形と違う。同じコピーの中で形がそろっている方が読みやすい |
| SB-D-2 | 型が違う値(bad_type)はコピーで `=値(型が違います)`(値が数か文字列でなければ `=型が違います`)と出す | SB-FR-5 は「読めなかった」「無い」の2つだけを決めている。§10 の「生の文字列を出す」に合わせた |
| SB-D-3 | 大きなボタンの一覧 `runner.BY_CATEGORY` は3カテゴリのまま。起動の安全は `RUNNABLE` にだけ入れた。「まとめて診断」は `module.run_all()`(重い → ネット → 容量 → 起動の安全) | B-1(大きなボタンを増やさない)。既存の `ALL_IDS`(18 項目)と、それを使う既存のテストをそのまま保つため |
| SB-D-4 | 設定 `secureboot_check` を画面の「起動の安全の確認」カードのスイッチで変えられるようにした(クイックアクションは作り直す) | MODULE_GUIDE §0「§8 の値は GUI から変えられるようにする」。仕様書 §9 は設定キーだけを決めている |
| SB-D-5 | B2 の `cert_boot_2023`(info)の value は「参考」 | §6.2 の value の一覧に、この行の分が無かった |
| SB-D-6 | 表に文の無い detail は短く補った(`cert_conflict`「Windows の記録に『更新済み』と『失敗』の両方が残っています。」、`cert_missing`・`cert_unexpected`・`sb_unknown`・`cert_sb_off` など)。`sb_denied`・`cert_denied` は「管理者の権限が要る場所でした。PcCheckup は管理者の権限を求めません。」 | §6.2 は title だけの行がある |
| SB-D-7 | `cert_skipped_bios` にも §6.3 の action 2(Windows セキュリティ)と 3(メーカーの案内)を付けた(action 1 は付けない) | §6.3 の「warn・unknown・info の action」を字のとおりに読んだ |
| SB-D-8 | `diagnostics()` の `sb_b1` / `sb_b2` は、今回の起動で調べていなければ `none`、読み取りが想定外の OSError で止まったら `error`(B1・B2 は unknown) | §6.2 の表に、例外のときの理由コードが無い |
| SB-D-9 | 起動の安全も FR-7(前回より悪化)の対象にした(例: B2 が good → warn で印が付く) | 既存の仕組みがカテゴリを問わないため。B は bad を出さないので、印が付くのは warn になったときだけ |
| SB-D-10 | 既存のテストのうち3件の期待値だけを直した: `test_settings_defaults_written_back`(`secureboot_check` も書き戻す)・`test_quick_actions_open_page_and_run`(4つ目に「起動の証明書を調べる」)・`test_diagnostics`(3キーが増える) | 仕様書 §10・SB-FR-7・SB-FR-8 が求める変化。ほかの既存テストは変えずに通る |

## 3. §8 の実測(2026-09-28、この PC: Windows 11 Pro 10.0.26200。ソース実行)

一般ユーザーの権限(`IsUserAnAdmin()` = 0。管理者では試していない)で、`winreg.OpenKey(HKLM, …, 0, KEY_READ)` と GetFirmwareType を呼んだ結果。

| 値 | 読めたか | 値 | 型 |
|---|---|---|---|
| `State\UEFISecureBootEnabled` | ok | 1 | REG_DWORD |
| `Servicing\UEFICA2023Status` | ok | `Updated` | REG_SZ |
| `Servicing\UEFICA2023Error` | **missing**(値が無い) | — | — |
| `Servicing\WindowsUEFICA2023Capable` | ok | 2 | REG_DWORD |
| GetFirmwareType | 成功(戻り値 1) | 2 = FirmwareTypeUefi | — |

- 4つの値はどれも **一般ユーザーの権限で読めた**(denied は 0 件)。Error は「成功なら 0 のまま」(文書 B)ではなく、値そのものが無かった。判定表では Error が missing でも `Updated` なら `cert_updated` になるので問題ない。
- この PC の判定: B1 `sb_on`(good・オン)、B2 `cert_updated`(good・更新済み)。
- 所要時間(SB-FR-3、2 秒以内): 4値と GetFirmwareType の読み取りは 0.2ms。`run_category("boot", RealProbes())` は 1回目 2.2ms、2回目以降 0.1ms。`pytest -m win32_real tests/modules/pccheckup/test_boot.py`(SB-AC-8)も合格。

### 読めない値があったとき(SB-INV-5・仕様書どおり)

`PermissionError`(winerror 5 を含む)は denied、`FileNotFoundError` は missing、型が違えば bad_type。
denied は B1 なら `sb_denied`、B2 なら `cert_denied`(どちらも unknown「読めませんでした」)にして先へ進む。管理者の権限は求めない。
それ以外の OSError は、そのチェックだけ unknown(FR-5)。いずれもテストで winreg の偽物を使って確かめた(この PC では起きなかった)。

## 4. 実機で確かめること(この作業環境ではできなかったもの)

- **Windows セキュリティとの突き合わせ(SB-R-1)**: 「Windows セキュリティ > デバイス セキュリティ > セキュア ブート」の表示(色と文)と、msinfo32 の「セキュア ブートの状態」。この PC の B1・B2 はどちらも good なので、Windows セキュリティが「更新済み」(緑)・msinfo32 が「オン」なら一致。食い違えば SB-R-1 で B2 をやめる。
  依頼者の画面の窓に触れないため、この作業では開いていない。
- **日本語の画面の名前(§8)**: 「デバイス セキュリティ」「セキュア ブート」の実際の表記。§6.3 の action 2 の文(`Windows セキュリティの『デバイス セキュリティ』`)が合っているか。
- **`ms-settings:windowsdefender` を開いたときに何が出るか(§8)**: 設定アプリの「Windows セキュリティ」のページか、Windows セキュリティのアプリか。ボタンの文言「Windows セキュリティを開く」はどちらでも通じる形にしてある。
- BIOS 起動の PC・仮想マシン(`legacy_bios` / `cert_skipped_bios`)、読めない値がある PC(組織の PC など)、`NotStarted` の PC。この PC では起きないので、偽の probes のテストだけで確かめた。

## 5. 契約変更の提案

無し(本体への依頼も無し。標準ライブラリの `winreg` と ctypes だけ)。
