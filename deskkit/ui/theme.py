# 配色・フォント・アイコンフォント・アプリ全体の QSS を定義する。
# 色はすべてここの定数から使い、各画面で直書きしない(モジュールのアクセント色は catalog にある)。
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

BG0 = "#0A0D13"      # ウィンドウ背景
BG1 = "#0F131B"      # サイドバー
SURFACE = "#151A24"  # カード
SURFACE2 = "#1B2130"  # 入力欄・カード内の面
SURFACE3 = "#232A3B"  # ホバー
BORDER = "#262E40"
BORDER_HI = "#34405A"
TEXT = "#E7EAF3"
TEXT_DIM = "#98A1B7"
TEXT_MUTE = "#5E6880"
ACCENT = "#7C8CFF"
ACCENT_2 = "#B18CFF"
SUCCESS = "#34D399"
WARN = "#FBBF24"
DANGER = "#F87171"
INFO = "#60A5FA"

ON_ACCENT = "#0B0E16"  # 塗りつぶしボタン上の文字
HERO_GLYPH = "#FFFFFF"  # ヒーロー見出しのアイコン色
IS_LIGHT = False
MODE = "dark"

_DARK = dict(BG0=BG0, BG1=BG1, SURFACE=SURFACE, SURFACE2=SURFACE2, SURFACE3=SURFACE3, BORDER=BORDER, BORDER_HI=BORDER_HI,
             TEXT=TEXT, TEXT_DIM=TEXT_DIM, TEXT_MUTE=TEXT_MUTE, ACCENT=ACCENT, ACCENT_2=ACCENT_2, SUCCESS=SUCCESS,
             WARN=WARN, DANGER=DANGER, INFO=INFO, ON_ACCENT=ON_ACCENT, HERO_GLYPH=HERO_GLYPH)
_LIGHT = dict(BG0="#F3F5FA", BG1="#FFFFFF", SURFACE="#FFFFFF", SURFACE2="#F2F4F9", SURFACE3="#E7EBF3", BORDER="#E0E5EE",
              BORDER_HI="#C9D1DF", TEXT="#141A26", TEXT_DIM="#4A5468", TEXT_MUTE="#8590A5", ACCENT="#5B6BF5",
              ACCENT_2="#9A62F2", SUCCESS="#0F9F6E", WARN="#B7791F", DANGER="#DC3B3B", INFO="#2F74D0",
              ON_ACCENT="#FFFFFF", HERO_GLYPH="#1B2233")
# ライトテーマでは淡いアクセント色が白地で読みにくいので、モジュールの色を濃くする
_LIGHT_MODULE_ACCENTS = {"modeshift": "#7C4DDB", "dropsort": "#0E9C8C", "layoutkeep": "#2F74D0", "clipshelf": "#C27C0E"}


# グラフ用のモジュール色(dataviz の検証スクリプトで明度帯・色覚差・コントラストを両モードで確認済み)
_CHART_DARK = {"modeshift": "#9575F0", "dropsort": "#17A594", "layoutkeep": "#4F8FE6", "clipshelf": "#C0820A"}


def chart_color(module: str) -> str:
    if IS_LIGHT:
        return _LIGHT_MODULE_ACCENTS.get(module, ACCENT)
    return _CHART_DARK.get(module, ACCENT)


def system_prefers_light() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _t = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return bool(v)
    except OSError:
        return False


def set_mode(mode: str) -> str:
    """"dark" / "light" / "system"。deskkit.ui の他の部品を import する前に呼ぶこと(色を import 時に読む部品があるため)。"""
    global IS_LIGHT, MODE
    light = mode == "light" or (mode == "system" and system_prefers_light())
    IS_LIGHT = light
    MODE = "light" if light else "dark"
    globals().update(_LIGHT if light else _DARK)
    from dataclasses import replace

    from deskkit import catalog

    if light:
        catalog.MODULES = tuple(replace(m, accent=_LIGHT_MODULE_ACCENTS.get(m.name, m.accent)) for m in catalog.MODULES)
    return MODE


UI_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Yu Gothic UI", "Meiryo UI"]
_icon_family: str | None = None


def icon_family() -> str:
    global _icon_family
    if _icon_family is None:
        fams = set(QFontDatabase.families())
        _icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), "Segoe UI Symbol")
    return _icon_family


def icon_font(px: int) -> QFont:
    f = QFont(icon_family())
    f.setPixelSize(px)
    return f


def ui_font(px: int = 13, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(UI_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def alpha(hex_color: str, a: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{int(a * 255)})"


# Segoe Fluent Icons の字形
class G:
    HOME = ""
    SETTINGS = ""
    LOG = ""
    POWER = ""
    PLAY = ""
    PAUSE = ""
    UNDO = ""
    SAVE = ""
    ADD = ""
    DELETE = ""
    EDIT = ""
    SEARCH = ""
    PIN = ""
    UNPIN = ""
    CHECK = ""
    CLOSE = ""
    WARNING = ""
    ERROR = ""
    INFO = ""
    FOLDER = ""
    OPEN = ""
    REFRESH = ""
    KEYBOARD = ""
    GAME = ""
    SHIELD = ""
    LOCK = ""
    CLOCK = ""
    MONITOR = ""
    EYE = ""
    LIST = ""
    FILTER = ""
    COPY = ""
    PASTE = ""
    ARCHIVE = ""
    SPARKLE = ""
    UP = ""
    DOWN = ""
    CHEVRON = ""
    MORE = ""
    LIGHTNING = ""
    VOLUME = ""
    APP = ""
    LINK = ""
    TERMINAL = ""
    DOWNLOAD = ""
    CLIPBOARD = ""
    LAYOUT = ""
    MODE = ""
    TEXT = ""
    CLEAR = ""


def stylesheet() -> str:
    return f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QMainWindow, #Root {{ background: {BG0}; }}
QToolTip {{ background: {SURFACE3}; color: {TEXT}; border: 1px solid {BORDER_HI}; border-radius: 6px; padding: 6px 8px; }}
QLabel {{ background: transparent; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#Mute {{ color: {TEXT_MUTE}; font-size: 12px; }}
QLabel#H1 {{ font-size: 26px; font-weight: 700; }}
QLabel#H2 {{ font-size: 17px; font-weight: 600; }}
QLabel#H3 {{ font-size: 14px; font-weight: 600; }}
QLabel#Eyebrow {{ color: {TEXT_MUTE}; font-size: 11px; font-weight: 700; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#Inset {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#Divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {SURFACE3}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_MUTE}; background: {SURFACE}; border-color: {BORDER}; }}
QPushButton[kind="primary"] {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2}); border: none; color: {ON_ACCENT}; }}
QPushButton[kind="primary"]:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {QColor(ACCENT).lighter(112).name()}, stop:1 {QColor(ACCENT_2).lighter(112).name()}); }}
QPushButton[kind="primary"]:disabled {{ background: {SURFACE3}; color: {TEXT_MUTE}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: 1px solid transparent; color: {TEXT_DIM}; }}
QPushButton[kind="ghost"]:hover {{ background: {SURFACE2}; color: {TEXT}; }}
QPushButton[kind="danger"] {{ background: {alpha(DANGER, 0.12)}; border: 1px solid {alpha(DANGER, 0.35)}; color: {DANGER}; }}
QPushButton[kind="danger"]:hover {{ background: {alpha(DANGER, 0.22)}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
  background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 9px;
  selection-background-color: {alpha(ACCENT, 0.45)}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
  border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {TEXT_MUTE}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE2}; border: 1px solid {BORDER_HI}; border-radius: 8px;
  selection-background-color: {alpha(ACCENT, 0.3)}; padding: 4px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {BORDER_HI}; background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTableWidget, QTreeWidget, QListWidget, QTableView, QTreeView, QListView {{
  background: {SURFACE}; alternate-background-color: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 10px;
  gridline-color: {BORDER}; selection-background-color: {alpha(ACCENT, 0.28)}; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE2}; color: {TEXT_DIM}; border: none; border-bottom: 1px solid {BORDER};
  padding: 7px 8px; font-weight: 600; font-size: 12px; }}
QTableCornerButton::section {{ background: {SURFACE2}; border: none; }}
QListWidget::item, QTreeWidget::item {{ padding: 6px 4px; border-radius: 6px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {SURFACE3}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 3px; min-height: 30px; margin: 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTE}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 3px; min-width: 30px; margin: 2px 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_HI}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {alpha(ACCENT, 0.22)}; }}
QMenu::item:disabled {{ color: {TEXT_MUTE}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

QTabBar::tab {{ background: transparent; color: {TEXT_DIM}; padding: 8px 14px; border: none; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabWidget::pane {{ border: none; }}
QSplitter::handle {{ background: {BORDER}; }}
QProgressBar {{ background: {SURFACE2}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QSlider::groove:horizontal {{ height: 6px; background: {SURFACE3}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; }}
QDialog {{ background: {BG1}; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(ui_font(13))
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG0))
    pal.setColor(QPalette.ColorRole.Base, QColor(SURFACE2))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(SURFACE2))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(ON_ACCENT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTE))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(SURFACE3))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
