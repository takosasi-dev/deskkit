# モジュールの名前と表示用メタ情報(名前・一言説明・アクセント色・アイコン字形)だけを持つ。
# QOL 機能の中身(モード・ルール・レイアウト・履歴)の知識はここに書かない(INV-1)。
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    title: str
    tagline: str
    accent: str
    glyph: str  # Segoe Fluent Icons / MDL2 Assets の字形
    description: str = ""


MODULES: tuple[ModuleInfo, ...] = (
    ModuleInfo("modeshift", "ModeShift", "作業モードをワンタッチで切り替え", "#A78BFA", "",
               "「ゲーム」「勉強」「開発」などのモードを定義し、電源プラン・音量・アプリの起動/終了・ウィンドウ配置の呼び出しを"
               "1操作でまとめて実行します。実行前にプレビューでき、電源と音量は元に戻せます。"),
    ModuleInfo("dropsort", "DropSort", "ダウンロードを自動で整理", "#2DD4BF", "",
               "ダウンロードが完了したファイルだけを、あなたが決めたルールで振り分けます。ファイルは開かず・実行せず、"
               "インターネット由来の印(MOTW)を必ず保ったまま移動し、いつでも元に戻せます。"),
    ModuleInfo("layoutkeep", "LayoutKeep", "ウィンドウ配置を保存して復元", "#60A5FA", "",
               "モニタ構成ごとにウィンドウの位置・サイズ・最大化状態を保存し、抜き差しやスリープ復帰で崩れた配置をワンアクションで戻します。"
               "区別できないウィンドウは動かしません。"),
    ModuleInfo("clipshelf", "ClipShelf", "暗号化クリップボード履歴と定型文", "#FBBF24", "",
               "コピーしたテキストを暗号化して記録し、ホットキーで開く検索パレットから呼び出せます。パスワードマネージャー等の"
               "除外印を尊重し、定型文と書式なし貼り付けも使えます。"),
    ModuleInfo("sendprep", "SendPrep", "送る前に写真と動画を整える", "#F472B6", "",
               "写真・スクリーンショット・動画を放り込むだけで、位置情報などの隠れた情報を消し、送り先のサイズ上限に収め、"
               "見せたくない文字を塗りつぶした別のファイルを作ります。元のファイルは変えません。"),
    ModuleInfo("pccheckup", "PcCheckup", "PC の不調の原因を調べる", "#FB923C", "",
               "「重い」「ネットが遅い」「容量が足りない」のボタンを押すだけで原因の候補を調べ、何が起きているかと次にやることを"
               "わかりやすい言葉で示します。設定は変えず、案内するだけです。"),
    ModuleInfo("twinsweep", "TwinSweep", "似た写真をまとめて片づける", "#A3E635", "",
               "フォルダの中のそっくりな写真をグループにまとめ、いちばん良い1枚を提案します。残りは確認してからごみ箱へ送るので、"
               "ごみ箱から元に戻せます。"),
)
MODULE_NAMES: tuple[str, ...] = tuple(m.name for m in MODULES)


def info(name: str) -> ModuleInfo:
    for m in MODULES:
        if m.name == name:
            return m
    return ModuleInfo(name, name, "", "#94A3B8", "")
