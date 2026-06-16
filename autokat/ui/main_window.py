"""AutoCat 主窗口 — 任务工作台模式（Step 1-4 向导 + 侧边栏 + 任务详情）"""
import os, sys, json, threading, subprocess, random, hashlib, shutil, traceback
from pathlib import Path
from datetime import datetime
from typing import Optional
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize, QRect
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QSpinBox, QComboBox, QSlider,
    QProgressBar, QListWidget, QFileDialog, QMessageBox, QScrollArea, QDialog,
    QGroupBox, QFormLayout, QLineEdit, QCheckBox, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QAbstractItemView,

    QStackedWidget, QFrame, QSizePolicy, QSpacerItem, QGridLayout, QAbstractItemView,
    QSplitter, QStyledItemDelegate, QStyleOptionViewItem, QStyle, QMenu,
    QButtonGroup, QRadioButton, QToolButton,
)
from PySide6.QtGui import QFont, QPixmap, QIcon, QColor, QPalette, QPainter, QBrush, QPen, QTextDocument
from autokat.models.db import (
    init_db, get_all_materials, get_pending_tasks, get_conn,
    get_all_tasks, get_tasks_by_status, get_clips_by_task,
    get_task_stats, delete_task, get_script_by_id, get_task, get_latest_task,
    get_rendering_clips, get_clips_by_task, prepare_task_retry,
)
from autokat.core.progress_log import drain as _log_drain, clear as _log_clear
from autokat.core.progress_log import get_stage as _log_get_stage, emit as _log_emit, emit
from autokat.core.material import import_files
from autokat.core.tts import save_script, generate_narration, list_scripts
from autokat.core.renderer import create_and_run_batch, resume_pending_tasks, ensure_dirs, OUTPUT_DIR
from autokat.core.cli_runner import run_generate
from autokat.core.dedup import dedup_output_dir
from autokat.core.presets import PLATFORM_IDS, PLATFORM_DISPLAY, PLATFORM_PRESETS, apply_preset_to_config
from autokat.core.perturbation import LEVELS as PERT_LEVELS, build_perturbation
from autokat.core.bgm import get_bgm_files, pick_random_bgm, detect_bpm, download_sample_bgm
from autokat.core.bgm import get_bgm_duration, split_bgm_to_segments
from autokat.core.bgm import get_bgm_duration, split_bgm_to_segments
from autokat.core.writer import (
    generate_script_by_topic, generate_script_by_topic_detailed,
    validate_script_quality, estimate_chars_for_lang, estimate_chars_for_duration_range,
    list_styles, check_deepseek_available, set_deepseek_key, set_deepseek_config,
)
from autokat.core.paths import DATA_ROOT
from autokat.core.wizard_snapshot import (
    WIZARD_FIELD_LABELS, empty_snapshot, label_for,
)
try:
    from autokat.core.ai_providers import (
        load_ai_settings, load_deepseek_key, save_deepseek_key,
    )
    _ai_settings = load_ai_settings()
    _keychain_key = load_deepseek_key()
    _env_path = DATA_ROOT / ".env"
    if not _keychain_key and _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                _legacy_key = line.split("=", 1)[1].strip()
                if _legacy_key:
                    save_deepseek_key(_legacy_key)
                    _keychain_key = _legacy_key
                    break
    set_deepseek_config(
        _keychain_key,
        _ai_settings.get("deepseek_url"),
        _ai_settings.get("deepseek_model"),
    )
except Exception:
    pass
BASE_DIR = DATA_ROOT
CONFIG_DIR = Path.home() / ".config" / "autokat"
CONFIG_FILE = CONFIG_DIR / "subtitle_pos.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

def _load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_settings(settings: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _get_output_dir() -> str:
    s = _load_settings()
    return s.get("output_dir", str(BASE_DIR / "output"))

def _set_output_dir(path: str) -> None:
    s = _load_settings()
    s["output_dir"] = path
    _save_settings(s)
# ── 工具函数 ──
_STYLE = """
/* AutoCat — Midnight Dark Theme (参考拆图工具风格) */
/* Palette: #FCFCFD bg | #FFFFFF card | #FCFCFD input | #2563EB primary | #10B981 emerald | #F59E0B amber | #EF4444 red | #111827 white | #6B7280 muted | #E5E7EB border */
* {
    font-family: "SF Pro Display", "Helvetica Neue", "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
    font-size: 13px;
    color: #111827;
}
QMainWindow { background: #FCFCFD; }
QWidget { background: #FCFCFD; color: #111827; }
QLabel { background: transparent; color: #111827; }
QGroupBox {
    font-weight: 600; font-size: 11px; border: none;
    border-radius: 12px; margin-top: 14px;
    padding: 16px 14px 12px; background: #FFFFFF;
    color: #2563EB; letter-spacing: 0.3px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    padding: 0 8px; color: #2563EB;
    font-weight: 700; font-size: 10px; background: transparent;
    text-transform: uppercase; letter-spacing: 0.8px;
}
QTextEdit, QLineEdit, QSpinBox, QComboBox {
    background: #FCFCFD; color: #111827;
    border: 1.5px solid #E5E7EB; border-radius: 10px;
    padding: 8px 12px; font-size: 13px;
    selection-background-color: #2563EB40;
    selection-color: #111827;
}
QTextEdit:focus, QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #2563EB;
}
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1.5px solid #E5E7EB;
    border-radius: 8px;
    padding: 4px;
    selection-background-color: #2563EB30;
    color: #111827;
}
QComboBox { border: 1.5px solid #E5E7EB; border-radius: 10px; }
QListWidget {
    background: #FFFFFF; border: 1.5px solid #E5E7EB;
    border-radius: 12px; padding: 4px;
    color: #111827;
}
QListWidget::item {
    padding: 8px 12px; color: #111827; border-radius: 8px;
}
QListWidget::item:hover { background: #2563EB15; }
QListWidget::item:selected { background: #2563EB25; color: #111827; }
QCheckBox { spacing: 8px; color: #111827; font-size: 13px; font-weight: 500; background: transparent; }
QCheckBox::indicator {
    width: 17px; height: 17px; border-radius: 5px;
    border: 1.5px solid #D1D5DB; background: #FCFCFD;
}
QCheckBox::indicator:checked { background: #2563EB; border-color: #2563EB; }
QSlider::groove:horizontal { height: 5px; background: #E5E7EB; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 18px; height: 18px; margin: -7px 0;
    background: #111827; border: 2px solid #2563EB; border-radius: 9px;
}
QSlider::sub-page:horizontal { background: #2563EB; border-radius: 3px; }
QScrollBar:vertical { width: 7px; background: transparent; margin: 0; }
QScrollBar::handle:vertical { background: #D1D5DB; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #2563EB; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: transparent; }
QScrollBar:horizontal { height: 7px; background: transparent; margin: 0; }
QScrollBar::handle:horizontal { background: #D1D5DB; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #2563EB; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; background: transparent; }
/* 侧边栏内的任务列表：彻底隐藏垂直滚动条 */
#sidebar QListWidget QScrollBar:vertical { width: 0px; background: transparent; }
#sidebar QListWidget QScrollBar::handle:vertical,
#sidebar QListWidget QScrollBar::add-line:vertical,
#sidebar QListWidget QScrollBar::sub-line:vertical { background: transparent; border: none; height: 0; }
/* 主分割条（侧边栏 与 内容区）极简可拖拽样式 */
QSplitter#main_splitter::handle { background: transparent; width: 1px; }
QSplitter#main_splitter::handle:hover { background: #2563EB; }
QSplitter#main_splitter { background: transparent; }
QProgressBar {
    border: none; border-radius: 8px; background: #E5E7EB;
    height: 8px; text-align: center; font-size: 10px; font-weight: 600; color: #111827;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2563EB,stop:1 #10B981);
    border-radius: 8px;
}
QPushButton {
    background: #2563EB; color: #111827;
    border: none; border-radius: 10px;
    padding: 7px 18px; font-size: 13px; font-weight: 600;
}
QPushButton:hover { background: #1D4ED8; }
QPushButton:pressed { background: #1E40AF; }
QPushButton:disabled { background: #D1D5DB; color: #6B7280; }
/* Secondary / ghost buttons */
QPushButton[secondary="true"] {
    background: transparent; color: #2563EB;
    border: 1.5px solid #2563EB;
}
QPushButton[secondary="true"]:hover { background: #2563EB15; }
/* Emerald (生成/成功) */
QPushButton[emerald="true"] {
    background: #10B981; color: #111827;
    border: none;
}
QPushButton[emerald="true"]:hover { background: #059669; }
/* Amber (暂停) */
QPushButton[amber="true"] {
    background: #F59E0B; color: #FCFCFD;
    border: none;
}
QPushButton[amber="true"]:hover { background: #D97706; }
/* Red (停止/删除) */
QPushButton[red="true"] {
    background: #EF4444; color: #111827;
    border: none;
}
QPushButton[red="true"]:hover { background: #DC2626; }
QMessageBox { background: #FFFFFF; }
QMessageBox QLabel { color: #111827; }
QDialog { background: #FFFFFF; }
QFrame { background: transparent; }
/* Sidebar */
#sidebar { background: #FCFCFD; min-width: 220px; border-right: 1px solid #E5E7EB; }
#sidebar QLabel { color: #111827; background: transparent; }
#sidebar QPushButton { background: transparent; color: #6B7280; border: none; border-radius: 8px; }
#sidebar QPushButton:hover { background: #2563EB15; color: #111827; }
/* Step bar */
#step_bar { background: transparent; }
/* Log area */
#log_view { background: #FCFCFD; color: #10B981; border: 1px solid #E5E7EB; border-radius: 12px; font-family: "SF Mono", Menlo, monospace; font-size: 12px; }
"""
def _tag_style(status: str) -> str:
    # 无背景无边框,只保留文字颜色 —— 与"带颜色文字图标"保持一致
    mapping = {
        "done":    "color:#34c759; background:transparent; border:none; font-size:12px; font-weight:600;",
        "running": "color:#7eaff3; background:transparent; border:none; font-size:12px; font-weight:600;",
        "pending": "color:#ff9500; background:transparent; border:none; font-size:12px; font-weight:600;",
        "failed":  "color:#ff3b30; background:transparent; border:none; font-size:12px; font-weight:600;",
    }
    return mapping.get(status, "color:#6B7280; background:transparent; border:none; font-size:12px; font-weight:600;")
def _tag_text(status: str) -> str:
    # 文字 + 颜色 即可,不再使用 emoji 图标
    mapping = {
        "done":    "✅ 已完成",
        "running": "🔥 进行中",
        "pending": "⏸ 已暂停",
        "failed":  "⚠️ 失败",
    }
    return mapping.get(status, status)
# ── 工作线程 ──
class WorkerThread(QThread):
    progress = Signal(str)
    finished = Signal(object)
    error = Signal(str)
    def __init__(self, target, args=None, kwargs=None):
        super().__init__()
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}
    def run(self):
        try:
            result = self._target(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))
# ── 状态图标 ──
def status_icon(status):
    # 侧边栏列表用的"带颜色文字图标"。侧边栏 list 装了 _HtmlItemDelegate
    # 来解析 QTextDocument,这里返回富文本,只有"状态二字"被染色。
    icons = {
        "done":    "✅",
        "running": "🔥",
        "pending": "⏸",
        "failed":  "❌",
    }
    return icons.get(status, '<span style="color:#6B7280;">--</span>')
# ── 侧边栏 HTML 富文本 item delegate ──
# QListWidget 默认不解析 item 文本中的 HTML,<span> 颜色会原样显示成尖括号。
# 这个 delegate 接管 paint,用 QTextDocument 渲染富文本。
class _HtmlItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        text = index.data(Qt.DisplayRole)
        is_html = bool(text) and isinstance(text, str) and ('<' in text and '>' in text)
        if not is_html:
            super().paint(painter, option, index)
            return
        # 复制一份 option,initStyleOption 会把显示文本/图标/QSS 都设好
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ''  # 让 Qt 不画默认文字,我们用 QTextDocument 自己画
        # 让 Qt 用 QSS 画背景(hover/selected/normal 三态)
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        # 用 QTextDocument 画富文本(支持 <span style="color:..">)
        doc = QTextDocument()
        doc.setDefaultStyleSheet(
            'body{color:#111827; font-size:12px; font-family:"PingFang SC","Microsoft YaHei",sans-serif;}'
        )
        doc.setHtml(text)
        doc.setTextWidth(option.rect.width())
        doc_h = doc.size().height()
        y_off = max(0, (option.rect.height() - doc_h) / 2)
        painter.save()
        painter.translate(option.rect.left(), option.rect.top() + y_off)
        painter.setClipRect(0, 0, option.rect.width(), option.rect.height())
        doc.drawContents(painter)
        painter.restore()
# ══════════════════════════════════════════════════════════════
# MainWindow
# ══════════════════════════════════════════════════════════════
class _TagFilterButton(QPushButton):
    """多选标签筛选按钮:点击弹出菜单,菜单中可勾选/取消任意多个标签。
    - 选中集合改变时通过 ``tagsChanged`` 发出 set[str] 信号。
    - ``selected_tags()`` 返回当前选中的标签名集合(空集表示不过滤)。
    """
    tagsChanged = Signal(set)
    def __init__(self, parent=None):
        super().__init__("🏷️  标签筛选", parent)
        self.setCheckable(True)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._actions: dict[str, object] = {}
        self._selected: set[str] = set()
        self._menu.aboutToShow.connect(self._refresh_menu)
        self._menu.triggered.connect(self._on_triggered)
        self.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#2563EB; font-size:11px; font-weight:600;"
            " border:1.5px solid #E5E7EB; border-radius:8px; padding:3px 12px; }"
            "QPushButton:hover { background:#FCFCFD; }"
            "QPushButton::menu-indicator { image: none; }"
        )
        self.setFixedHeight(28)
    def selected_tags(self) -> set:
        return set(self._selected)
    def set_selected_tags(self, tags) -> None:
        self._selected = set(tags or [])
        self._update_text()
    def _refresh_menu(self) -> None:
        self._menu.clear()
        self._actions.clear()
        try:
            from autokat.models.db import get_all_tags_with_usage
            tags = get_all_tags_with_usage()
        except Exception:
            tags = []
        if not tags:
            act = self._menu.addAction("(暂无标签)")
            act.setEnabled(False)
        else:
            for t in tags:
                act = self._menu.addAction(t["name"])
                act.setCheckable(True)
                act.setChecked(t["name"] in self._selected)
                self._actions[t["name"]] = act
            if self._selected:
                self._menu.addSeparator()
                clr = self._menu.addAction("✕  清除筛选")
                clr.triggered.connect(self._clear)
    def _on_triggered(self, _action) -> None:
        # 重新从 action 状态收集,这样不依赖 checked/unchecked 的时序
        self._selected = {n for n, a in self._actions.items() if a.isChecked()}
        self._update_text()
        self.tagsChanged.emit(set(self._selected))
    def _clear(self) -> None:
        self._selected.clear()
        self._update_text()
        self.tagsChanged.emit(set(self._selected))
    def _update_text(self) -> None:
        if self._selected:
            self.setText(f"🏷️  标签: {len(self._selected)} 个")
        else:
            self.setText("🏷️  标签筛选")
class _RubberBandCheckListWidget(QListWidget):
    """带复选框 + 橡皮筋框选的列表。
    - 开启 ExtendedSelection + 选择矩形：支持鼠标拖拽框选多个 item。
    - 任何新加进来的 item 自动带上 ``Qt.ItemIsUserCheckable`` 标志，
      调用方不必再手动 setFlags，直接 addItem(QListWidgetItem(text)) 即可。
    - 选中状态变化时（拖框 / Ctrl+点 / Shift+点），自动把当前所有选中的 item
      勾上；取消勾选仍走原来的复选框点击逻辑，不会因为拖选反向清掉。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionRectVisible(True)
        self.itemSelectionChanged.connect(self._sync_check_to_selection)
    def _ensure_indicator_style(self) -> None:
        """复选框指示器样式:调用方 setStyleSheet 后追加,确保深色 style 下不被吞掉。"""
        self.setStyleSheet(self.styleSheet() + (
            "QListWidget::indicator { width: 18px; height: 18px; }"
            "QListWidget::indicator:unchecked { border: 2px solid #9CA3AF;"
            " background: #FFFFFF; border-radius: 4px; }"
            "QListWidget::indicator:unchecked:hover { border-color: #2563EB;"
            " background: #EFF6FF; }"
            "QListWidget::indicator:checked { border: 2px solid #2563EB;"
            " background: #2563EB; border-radius: 4px;"
            " image: url(data:image/svg+xml;base64,"
            "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMiAxMiI+"
            "PHBvbHlsaW5lIHBvaW50cz0iMiw2IDUsOSAxMCwyIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIi"
            "IGZpbGw9Im5vbmUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPjwvc3ZnPg==); }"
        ))
    def _sync_check_to_selection(self) -> None:
        for item in self.selectedItems():
            try:
                if item.flags() & Qt.ItemIsUserCheckable and item.checkState() != Qt.Checked:
                    item.setCheckState(Qt.Checked)
            except Exception:
                pass
    def addItem(self, item):  # type: ignore[override]
        # 尊重调用方的 setFlags(分组标题行会特意清掉 ItemIsUserCheckable)。
        # 仅当一个 flag 都没设时,才默认给一个最基本的可勾选标志作为兜底。
        try:
            if isinstance(item, QListWidgetItem) and int(item.flags()) == 0:
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        except Exception:
            pass
        super().addItem(item)

class _MiniStub:
    """v2.3 兼容 shim: 旧 test 还引用已删除控件, 装成可 setValue/getValue 鸭子类型"""
    def __init__(self, value=0):
        self._v = value
    def setValue(self, v): self._v = v
    def value(self): return self._v
    def setChecked(self, v): self._v = bool(v)
    def isChecked(self): return bool(self._v)
    def setVisible(self, v): pass
    def setStyleSheet(self, s): pass
    def setText(self, t): pass
    def text(self): return ""
    def setMaximum(self, v): pass
    def setMaximumWidth(self, v): pass
    def setMaximumHeight(self, v): pass
    def setRange(self, *a): pass
    def valueChanged(self): pass
    def setCurrentIndex(self, i): pass
    def currentIndex(self): return 0
    def currentData(self): return None
    def setCurrentText(self, t): pass
    def findText(self, t): return -1
    def addItem(self, t, d=None): pass
    def addItems(self, items): pass
    def setMinimumHeight(self, v): pass
    def setMinimumWidth(self, v): pass
    def count(self): return 0
    def setToolTip(self, t): pass
    def setSuffix(self, s): pass
    def setSingleStep(self, v): pass
    def setDecimals(self, v): pass



class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoCat 智能混剪 v3.0")
        self.setMinimumSize(1280, 860)
        init_db()
        # 一次性把 .env 中的 DEEPSEEK_API_KEY 搬到 macOS Keychain，
        # 之后运行时不再依赖环境变量；旧 .env Key 会被清理。
        try:
            from autokat.core.ai_providers import migrate_env_key_to_keychain
            migrate_env_key_to_keychain()
        except Exception:
            pass
        # 状态
        self._current_task_id = None
        self._sidebar_filter = "all"  # all / running / done / failed / pending
        self._wizard_draft = {}  # 暂存向导中的草稿
        # v3: 向导当前的查看/编辑模式。None=正常新建，'view'=只读查看已有任务,
        # 'fork'=基于已有任务新建(预填+可编辑)。配合 _wizard_view_task_id 一起用。
        self._wizard_mode = None
        self._wizard_view_task_id = None
        # 旧任务降级：wizard_snapshot 为 NULL 时打开的"不完整视图"标记
        self._wizard_view_is_legacy = False
        self._gen_start_time = datetime.now()
        self._wiz_poll_timer = QTimer()
        self._detail_task_id = None
        self._stop_requested = False
        self._import_start_time = datetime.now()
        self._import_signal = None
        self._init_ui()
        self._refresh_sidebar()
        self._refresh_dashboard()
        # 定时刷新
        self._timer = QTimer()
        self._timer.timeout.connect(self._on_timer_tick)
        self._timer.start(3000)
    # ── UI 构建 ──
    def _load_subtitle_pos(self) -> Optional[str]:
        """读取上次保存的字幕位置（label 文本）。文件不存在/损坏则返回 None。"""
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                val = data.get("bottom_pct")
                if val is not None:
                    return str(val)
        except Exception:
            pass
        return None
    def _save_subtitle_pos(self, pct) -> None:
        """保存当前字幕位置到 ~/.config/autokat/subtitle_pos.json。"""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps({"bottom_pct": pct}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
    def _update_sub_preview(self, bottom_pct: int) -> None:
        """根据底部留白百分比更新字幕预览框/百分比标签/安全提示/预设档位。

        bottom_pct: 底部留白占画布高度的百分比（8~35）。
        """
        # 1) 更新手机预览中的字幕条位置
        if hasattr(self, "_sub_preview_bar") and hasattr(self, "_sub_preview_widget"):
            # 预览框固定宽度100px，保持9:16比例
            video_h = int(100 * 16 / 9)
            bar_h = 10
            bar_w = 92  # 字幕条宽度（略小于视频区域）
            # 计算字幕条左下角位置
            x = 4  # 左边距
            y = video_h - int(video_h * bottom_pct / 100) - bar_h + 4  # 底部留白 + 上偏移
            self._sub_preview_bar.move(x, y)
            self._sub_preview_bar.resize(bar_w, bar_h)

        # 2) 百分比标签
        if hasattr(self, "_wiz_sub_pct_label"):
            px = int(1920 * bottom_pct / 100)
            self._wiz_sub_pct_label.setText(
                f"{bottom_pct}% ({px}px)"
            )

        # 3) 安全提示
        if hasattr(self, "_wiz_sub_safety_label"):
            if bottom_pct < 11:
                self._wiz_sub_safety_label.setText("⚠️ 可能被遮挡")
                self._wiz_sub_safety_label.setStyleSheet(
                    "color: #DC2626; font-size: 12px; font-weight: 600;"
                )
            elif bottom_pct <= 16:
                self._wiz_sub_safety_label.setText("✅ 安全推荐")
                self._wiz_sub_safety_label.setStyleSheet(
                    "color: #059669; font-size: 12px; font-weight: 600;"
                )
            else:
                self._wiz_sub_safety_label.setText("🔼 位置偏高")
                self._wiz_sub_safety_label.setStyleSheet(
                    "color: #2563EB; font-size: 12px; font-weight: 600;"
                )

        # 4) 预设档位 radio 同步：10/13/16 精确匹配时选中对应按钮，否则选中"自定义"并显示值
        if hasattr(self, "_wiz_sub_preset_group"):
            preset_ids = {10, 13, 16}
            custom_btn = self._wiz_sub_preset_group.button(-1)
            if bottom_pct in preset_ids:
                target = self._wiz_sub_preset_group.button(bottom_pct)
                for b in self._wiz_sub_preset_group.buttons():
                    b.setChecked(b is target)
                if custom_btn:
                    custom_btn.setText("自定义")
            elif custom_btn:
                # 取消其他按钮，选中自定义
                for b in self._wiz_sub_preset_group.buttons():
                    b.setChecked(b is custom_btn)
                custom_btn.setText(f"自定义({bottom_pct}%)")
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        # 左侧：侧边栏 + 右侧：内容区  通过 QSplitter 支持拖拽调整宽度
        self._main_splitter = QSplitter(Qt.Horizontal)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setHandleWidth(1)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setOpaqueResize(False)
        # 左侧：侧边栏
        self._sidebar = self._build_sidebar()
        self._main_splitter.addWidget(self._sidebar)
        # 右侧：内容区
        self._content_stack = QStackedWidget()
        self._main_splitter.addWidget(self._content_stack)
        # 初始比例:侧边栏 280px(+30%),其余给内容区
        self._main_splitter.setSizes([280, 1000])
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        main_layout.addWidget(self._main_splitter, 1)
        # 页面索引
        self.PAGE_DASHBOARD = 0
        self.PAGE_WIZARD_STEP1 = 1
        self.PAGE_WIZARD_STEP2 = 2
        self.PAGE_WIZARD_STEP3 = 3
        self.PAGE_WIZARD_STEP4 = 4
        self.PAGE_TASK_DETAIL = 5
        # v3.2: 视频类型下拉框只放在 Step 2 (原 Step 3 同步副本已移除 — 用户在 Step 2 选一次即可)。
        # 实际 widget 实例在 _build_wizard_step2 里创建。
        # 构建各页面
        self._dashboard_page = self._build_dashboard_page()
        self._wizard_step1 = self._build_wizard_step1()
        self._wizard_step2 = self._build_wizard_step2()
        self._wizard_step3 = self._build_wizard_step3()
        self._wizard_step4 = self._build_wizard_step4()
        self._task_detail_page = self._build_task_detail_page()
        self._content_stack.addWidget(self._dashboard_page)   # 0
        self._content_stack.addWidget(self._wizard_step1)      # 1
        self._content_stack.addWidget(self._wizard_step2)      # 2
        self._content_stack.addWidget(self._wizard_step3)      # 3
        self._content_stack.addWidget(self._wizard_step4)      # 4
        self._content_stack.addWidget(self._task_detail_page)  # 5
        # v3: 向导查看模式横幅。Qt 不允许同一 widget 挂在多个 layout 下，
        # 所以为 4 个 step page 各建一个 banner 实例（视觉/文案完全一致）。
        # 第一个 banner 的 label 和 button 暴露为 self 属性，方便统一改文案。
        self._wizard_view_banners = []
        self._wizard_view_banner = None  # 第一个 banner, 保留为兼容引用
        self._wizard_view_banner_label = None
        self._wizard_view_banner_btn = None
        for _sp_idx in range(1, 5):
            _ban = QFrame()
            _ban.setObjectName("wizard_view_banner")
            _ban.setStyleSheet(
                "QFrame#wizard_view_banner{background:#FEF3C7;border:1.5px solid #FBBF24;"
                "border-radius:10px;padding:0 12px;}"
                "QFrame#wizard_view_banner QLabel{color:#92400E;font-size:12px;"
                "font-weight:600;background:transparent;border:none;}"
                "QFrame#wizard_view_banner QPushButton{background:#FFFFFF;color:#92400E;"
                "border:1.5px solid #F59E0B;border-radius:8px;padding:5px 16px;"
                "font-size:12px;font-weight:700;}"
                "QFrame#wizard_view_banner QPushButton:hover{background:#FEF3C7;}"
            )
            _ban.setMinimumHeight(38)
            _ban.setMaximumHeight(44)
            _lay = QHBoxLayout(_ban)
            _lay.setContentsMargins(10, 4, 8, 4)
            _lay.setSpacing(10)
            _lbl = QLabel("")
            _lbl.setMinimumWidth(200)
            _lay.addWidget(_lbl, 1)
            _btn = QPushButton("知道了")
            _btn.setFixedHeight(28)
            _btn.clicked.connect(self._exit_wizard_view)
            _lay.addWidget(_btn, 0)
            _ban.setVisible(False)
            self._wizard_view_banners.append((_ban, _lbl, _btn))
            if _sp_idx == 1:
                # 保留第一个 banner 的引用作为"主"banner, 让 _enter_wizard_for
                # 只改这一份文案即可（4 个 banner 的 label/button 都是独立 widget
                # 但我们手动把它们的 text 同步一下）
                self._wizard_view_banner = _ban
                self._wizard_view_banner_label = _lbl
                self._wizard_view_banner_btn = _btn
            # 把 banner 插到这个 step page 的布局顶端
            _sp = getattr(self, f"_wizard_step{_sp_idx}")
            _sp.layout().insertWidget(0, _ban)
        self._show_page(self.PAGE_DASHBOARD)
        # v2.3 兼容 shim: 旧 test 还在 hasattr 旧控件 (_wiz_filter / _wiz_color_mood / _wiz_max_clips / 风险徽章)
        # 这些控件已移除但保留属性占位 (None) 防止 test 崩
        self._wiz_filter = _MiniStub()  # 随机调色已移除
        self._wiz_color_mood = _MiniStub()  # 调色偏好已移除
        self._wiz_max_clips = _MiniStub(value=12)  # 镜头上限已移除
        self._wiz_risk_label = _MiniStub(value="低")  # 风险徽章已移除
        self._wiz_risk_text = _MiniStub()  # 风险文本已移除
        self._wiz_risk_progress = _MiniStub()  # 风险进度条已移除
        self._wiz_count_tip = _MiniStub()  # 计数提示已移除
    def _show_page(self, index: int):
        self._content_stack.setCurrentIndex(index)
    def _navigate_to_task(self, task_id: int):
        """从侧边栏点击任务 → 打开任务详情"""
        self._detail_task_id = task_id
        self._refresh_task_detail(task_id)
        self._show_page(self.PAGE_TASK_DETAIL)
    def _on_timer_tick(self):
        """定时刷新"""
        self._refresh_sidebar()
        current = self._content_stack.currentIndex()
        if current == self.PAGE_DASHBOARD:
            self._refresh_dashboard()
        elif current == self.PAGE_TASK_DETAIL and hasattr(self, "_detail_task_id"):
            self._refresh_task_detail(self._detail_task_id, keep_scroll=True)
        elif current == self.PAGE_WIZARD_STEP4 and self._current_task_id:
            self._poll_wizard_progress()
    # ══════════════════════════════════════════════════════════
    # 侧边栏
    # ══════════════════════════════════════════════════════════
    def _build_sidebar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("sidebar")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(8)
        # Logo
        logo = QLabel("🚀 AutoCat")
        logo.setStyleSheet("font-size:20px; font-weight:800; color:#2563EB; padding:10px 4px 14px 4px; letter-spacing:0.5px;")
        layout.addWidget(logo)
        # 筛选标签(顶部"新建任务"按钮已移到工作台右上,保持侧边栏极简)
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(4)
        self._filter_btns = {}
        # 状态筛选按钮:用 1~2 字带颜色文字代替 emoji 图标
        for key, label, color in [
            ("all", "全部", "#6B7280"),
            ("running", "进行", "#2563EB"),
            ("done", "完成", "#10B981"),
            ("failed", "失败", "#EF4444"),
        ]:
            fb = QPushButton(label)
            fb.setFixedHeight(28)
            fb.setStyleSheet(
                f"color:{color}; background:transparent; border:none; border-radius:14px;"
                " padding:4px 12px; font-size:12px; font-weight:600;"
            )
            fb.clicked.connect(lambda checked, k=key: self._set_sidebar_filter(k))
            self._filter_btns[key] = fb
            filter_layout.addWidget(fb)
        layout.addLayout(filter_layout)
        layout.addSpacing(4)
        # 任务列表
        task_list_label = QLabel("任务列表")
        task_list_label.setStyleSheet("color:#6B7280; font-size:10px; font-weight:700; padding:6px 8px 4px 8px; letter-spacing:0.8px; text-transform:uppercase;")
        layout.addWidget(task_list_label)
        self._sidebar_task_list = QListWidget()
        self._sidebar_task_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; }
            QListWidget::item { color: #111827; padding: 10px 8px; border-radius: 8px; margin: 1px 0; }
            QListWidget::item:hover { background: #FCFCFD; }
            QListWidget::item:selected { background: #2563EB20; color: #111827; }
        """)
        # 装 HTML delegate,让 item 文本里的 <span style="color:.."> 真正生效
        self._sidebar_task_list.setItemDelegate(_HtmlItemDelegate(self._sidebar_task_list))
        self._sidebar_task_list.itemClicked.connect(self._on_sidebar_task_click)
        layout.addWidget(self._sidebar_task_list, 1)
        # 底部快捷入口
        layout.addSpacing(8)
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#E5E7EB; background:#E5E7EB;")
        layout.addWidget(sep)
        layout.addSpacing(4)
        for text, cb in [
            ("📂 输出目录", lambda: self._show_settings()),
            ("📦 素材管理", lambda: self._show_material_manager()),
            ("🎵 BGM 管理", lambda: self._show_bgm_manager()),
        ]:
            b = QPushButton(text)
            b.setStyleSheet("color:#6B7280; font-size:12px; text-align:left; padding:8px 12px; border-radius:8px; background:transparent; border:none;")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            layout.addWidget(b)
        return w
    def _set_sidebar_filter(self, key: str):
        self._sidebar_filter = key
        for k, fb in self._filter_btns.items():
            fb.setStyleSheet(
                "color:#ffffff; background:#2563EB; border:none; border-radius:14px; padding:4px 12px; font-size:11px; font-weight:600;"
                if k == key else
                "color:#6B7280; background:transparent; border:none; border-radius:14px; padding:4px 12px; font-size:11px; font-weight:500;"
            )
        self._refresh_sidebar()
        if self._content_stack.currentIndex() == self.PAGE_DASHBOARD:
            self._refresh_dashboard()
    def _refresh_sidebar(self):
        self._sidebar_task_list.clear()
        if self._sidebar_filter == "all":
            tasks = get_all_tasks(50)
        else:
            tasks = get_tasks_by_status(self._sidebar_filter, 50)
        for t in tasks:
            cfg = json.loads(t["config"]) if isinstance(t["config"], str) else t["config"]
            script = get_script_by_id(t["script_id"])
            _raw_name = script["name"] if script and script["name"] else ""
            if _raw_name and _raw_name != "批量生成":
                summary = _raw_name[:20]
            else:
                _first_part = (script["narration"].split(chr(10) + "---" + chr(10))[0] if script and script["narration"] else "")
                summary = _first_part[:12] + ".." if _first_part else "无文案"
            pct = int(t["done"] / t["total"] * 100) if t["total"] > 0 else 0
            icon = status_icon(t["status"])
            # 整行 HTML:只有"状态"二字被 <span> 染色,id/摘要保持深灰,
            # 进度/百分比用浅灰,呈现清晰的层次。QListWidget 装了 _HtmlItemDelegate
            # 负责解析;不能在这里再 setForeground,否则会盖过 <span> 颜色。
            text = (
                f"<span style='color:#111827; font-weight:600;'>#{t['id']}</span> "
                f"{icon} "
                f"<span style='color:#111827; font-weight:500;'>{summary}</span> "
                f"<span style='color:#9CA3AF; font-size:11px;'>"
                f"{t['done']}/{t['total']} ({pct}%)</span>"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, t["id"])
            self._sidebar_task_list.addItem(item)
    def _on_sidebar_task_click(self, item):
        task_id = item.data(Qt.UserRole)
        if task_id:
            self._navigate_to_task(task_id)
    # ══════════════════════════════════════════════════════════
    # 任务工作台 (Dashboard / Home)
    # ══════════════════════════════════════════════════════════
    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        # 标题
        title = QLabel("任务工作台")
        title.setStyleSheet("font-size:24px; font-weight:800; color:#111827; padding:2px 0 6px 0;")
        layout.addWidget(title)
        # 标题行:左侧"所有任务",右侧(竖排)[ + 新建任务 ] 在筛选之上
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        list_title = QLabel("所有任务")
        list_title.setStyleSheet("font-size:18px; font-weight:700; color:#111827;")
        title_row.addWidget(list_title)
        title_row.addStretch()
        # 右侧栏(垂直):新建任务(上) + 筛选(下)
        right_col = QVBoxLayout()
        right_col.setSpacing(6)
        new_btn = QPushButton("  +  新建任务  ")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setStyleSheet(
            "QPushButton{background:#2563EB;color:#ffffff;font-size:13px;font-weight:600;"
            "border:none;border-radius:8px;padding:8px 18px;}"
            "QPushButton:hover{background:#1D4ED8;}"
            "QPushButton:pressed{background:#1E40AF;}"
        )
        new_btn.clicked.connect(self._on_new_task)
        right_col.addWidget(new_btn, 0, Qt.AlignRight)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self._dashboard_filter_btns = {}
        # 筛选:1~2 字带颜色文字代替 emoji（带数量，类别数从 DB 实时拉取）
        for key, label, color in [
            ("all",     "全部",  "#6B7280"),
            ("running", "进行",  "#2563EB"),
            ("done",    "完成",  "#10B981"),
            ("failed",  "失败",  "#EF4444"),
            ("pending", "暂停",  "#F59E0B"),
        ]:
            fb = QPushButton(label)
            fb.setFixedHeight(28)
            fb.setCursor(Qt.PointingHandCursor)
            fb.setStyleSheet(
                f"background:#2563EB;color:#ffffff;border:none;border-radius:14px;"
                f"padding:4px 14px;font-size:12px;font-weight:600;"
                if key == "all" else
                f"background:#FFFFFF;color:{color};border:1.5px solid #E5E7EB;"
                f"border-radius:14px;padding:4px 14px;font-size:12px;font-weight:600;"
            )
            fb.clicked.connect(lambda checked, k=key: self._set_dashboard_filter(k))
            self._dashboard_filter_btns[key] = fb
            filter_row.addWidget(fb)
        right_col.addLayout(filter_row)
        title_row.addLayout(right_col)
        layout.addLayout(title_row)
        # 列标题(类似 web 表格头)
        self._task_table_header = self._build_task_table_header()
        layout.addWidget(self._task_table_header)
        # 任务卡片列表（滚动区域）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        self._task_cards_container = QWidget()
        self._task_cards_layout = QVBoxLayout(self._task_cards_container)
        self._task_cards_layout.setSpacing(0)
        self._task_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._task_cards_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._task_cards_container)
        layout.addWidget(scroll, 1)
        return page
    # ── 任务列表表头(类似 web 表格的列标题) ──
    def _build_task_table_header(self) -> QWidget:
        header = QWidget()
        header.setStyleSheet(
            "QWidget{background:transparent;border:none;}"
            "QLabel{color:#6B7280;font-size:11px;font-weight:700;"
            "background:transparent;letter-spacing:0.6px;}"
        )
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 8, 10, 8)
        h.setSpacing(8)
        # 与 _build_task_card 中的列顺序严格对齐
        cols = [
            ("编号",   30,  Qt.AlignLeft),
            ("状态",   35,  Qt.AlignLeft),
            ("文案",  250,  Qt.AlignLeft),
            ("参数",  120,  Qt.AlignLeft),
            ("进度",  200,  Qt.AlignLeft),
            ("时间",  100,  Qt.AlignLeft),
        ]
        for text, width, align in cols:
            lbl = QLabel(text)
            lbl.setAlignment(align | Qt.AlignVCenter)
            if width > 0:
                lbl.setFixedWidth(width)
            h.addWidget(lbl, 0 if width > 0 else 1)
        return header
    def _set_dashboard_filter(self, key: str):
        self._sidebar_filter = key
        # 状态色：与筛选按钮文字保持一致；激活态用蓝底白字
        color_map = {"all": "#6B7280", "running": "#2563EB", "done": "#10B981", "failed": "#EF4444", "pending": "#F59E0B"}
        for k, fb in self._dashboard_filter_btns.items():
            if k == key:
                fb.setStyleSheet(
                    "background:#2563EB;color:#ffffff;border:none;border-radius:14px;"
                    "padding:4px 14px;font-size:12px;font-weight:600;"
                )
            else:
                fb.setStyleSheet(
                    f"background:#FFFFFF;color:{color_map.get(k,'#6B7280')};"
                    f"border:1.5px solid #E5E7EB;border-radius:14px;"
                    f"padding:4px 14px;font-size:12px;font-weight:600;"
                )
        self._refresh_dashboard()
    def _refresh_dashboard(self):
        stats = get_task_stats()
        # 数量加到分类导航按钮上（替代删掉的统计条）
        for key, fb in self._dashboard_filter_btns.items():
            n = stats.get(key, 0)
            # 找到这个 btn 原本的 label（去掉旧的数量后缀）
            txt = fb.text().split("  ")[0].strip()
            fb.setText(f"{txt}  {n}")
        # 清空旧卡片
        while self._task_cards_layout.count():
            item = self._task_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self._sidebar_filter == "all":
            tasks = get_all_tasks(50)
        else:
            tasks = get_tasks_by_status(self._sidebar_filter, 50)
        if not tasks:
            empty = QLabel("暂无任务，点击上方「新建任务」开始吧 ✨")
            empty.setStyleSheet("color:#6B7280; font-size:14px; padding:48px; background:transparent;")
            empty.setAlignment(Qt.AlignCenter)
            self._task_cards_layout.addWidget(empty)
            return
        for t in tasks:
            card = self._build_task_card(t)
            self._task_cards_layout.addWidget(card)
        self._task_cards_layout.addStretch()
    def _build_task_card(self, t: dict) -> QWidget:
        card = QWidget()
        # web 表格行:无边框、无圆角,仅靠底部分隔线区分,hover 浅灰高亮
        card.setStyleSheet(
            "QWidget{background:transparent;color:#111827;border:none;}"
            "QWidget:hover{background:#FCFCFD;}"
            "QProgressBar{border:none;border-radius:3px;background:#E5E7EB;}"
            "QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #2563EB,stop:1 #10B981);border-radius:3px;}"
            "QPushButton{background:transparent;color:#6B7280;border:none;"
            "border-radius:4px;padding:0;font-size:13px;}"
            "QPushButton:hover{background:#2563EB15;color:#2563EB;}"
        )
        card.setCursor(Qt.PointingHandCursor)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setFixedHeight(44)
        card.mousePressEvent = lambda e, tid=t["id"]: self._navigate_to_task(tid)
        cfg = json.loads(t["config"]) if isinstance(t["config"], str) else t["config"]
        script = get_script_by_id(t["script_id"])
        _sn = script["name"] if script and script["name"] else ""
        _snt = script["narration"] if script and script["narration"] else ""
        if _sn and _sn != "批量生成":
            narration = _sn[:40]
        else:
            narration = _snt.split(chr(10) + "---" + chr(10))[0] if _snt else ""
        # -- 超紧凑单行布局 --
        layout = QHBoxLayout(card)
        layout.setContentsMargins(0, 5, 10, 5)
        layout.setSpacing(5)
        # 编号
        num = QLabel(f"#{t['id']}")
        num.setFixedWidth(30)
        num.setStyleSheet("font-size:12px;font-weight:700;color:#6B7280;background:transparent;")
        layout.addWidget(num)
        # 状态文字(无背景,仅文字色)
        tag = QLabel(_tag_text(t["status"]))
        tag.setFixedWidth(70)  # 状态列宽度 +100%
        tag.setStyleSheet(_tag_style(t["status"]))
        tag.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(tag)
        # 文案摘要（固定宽度，与表头对齐）
        if narration:
            clean = " ".join(narration.split())
            text = clean[:32] + "…" if len(clean) > 32 else clean
            s = QLabel(text)
            s.setFixedWidth(250)
            s.setFixedHeight(16)
            s.setStyleSheet("font-size:12px; color:#111827; font-weight:500; background:transparent;")
            s.setWordWrap(False)
            layout.addWidget(s)
        # 参数(去掉 emoji,改用纯文字表达,与"白黑灰主调"一致)
        mat_count = len(get_all_materials())
        parts = [f"素材{mat_count}"]
        if cfg.get("enable_bgm"):
            parts.append("BGM")
        parts.append(f"{cfg.get('fps',30)}fps")
        info = QLabel(" · ".join(parts))
        info.setFixedWidth(120)
        info.setStyleSheet("font-size:12px;color:#6B7280;background:transparent;")
        layout.addWidget(info)
        # 进度:进度条 + 百分比 组合在一起
        pct = int(t["done"] / t["total"] * 100) if t["total"] > 0 else 0
        progress_wrap = QWidget()
        progress_wrap.setFixedWidth(200)
        progress_wrap.setStyleSheet("background:transparent;")
        pw = QHBoxLayout(progress_wrap)
        pw.setContentsMargins(0, 0, 0, 0)
        pw.setSpacing(6)
        pb = QProgressBar()
        pb.setRange(0, max(t["total"], 1))
        pb.setValue(t["done"])
        pb.setFixedHeight(6)
        pb.setMinimumWidth(80)
        pb.setTextVisible(False)
        pw.addWidget(pb, 1)
        pct_lbl = QLabel(f"{pct}%")
        pct_lbl.setFixedWidth(36)
        pct_lbl.setStyleSheet("font-size:12px;color:#2563EB;font-weight:700;background:transparent;")
        pw.addWidget(pct_lbl)
        layout.addWidget(progress_wrap)
        # 时间
        time_lbl = QLabel(t["created_at"][5:16] if t["created_at"] else "")
        time_lbl.setFixedWidth(100)
        time_lbl.setStyleSheet("font-size:12px;color:#6B7280;background:transparent;")
        layout.addWidget(time_lbl)
        # v3: 卡片尾部加一个 "查看配置" 小图标入口。点击直接打开 wizard view 模式，
        # 不用先点进任务详情页。Qt 按钮 click 不会冒泡到 card 的 mousePressEvent，
        # 所以这里不会跟 card 整体的"跳详情"点击冲突。
        view_btn = QPushButton("👁")
        view_btn.setFixedSize(28, 24)
        view_btn.setCursor(Qt.PointingHandCursor)
        view_btn.setToolTip(f"查看任务 #{t['id']} 的步骤配置（只读）")
        view_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#0EA5E9;font-size:14px;"
            "border:1px solid transparent;border-radius:6px;padding:0;}"
            "QPushButton:hover{background:#0EA5E910;border-color:#0EA5E940;}"
        )
        view_btn.clicked.connect(
            lambda _checked=False, _tid=t["id"]: self._open_wizard_view(_tid)
        )
        layout.addWidget(view_btn)
        return card
    @staticmethod
    def _make_small_btn(text: str, cb) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(28)
        b.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #2563EB; border: 1.5px solid #2563EB;
                border-radius: 8px; padding: 4px 12px; font-size: 11px; font-weight: 600;
            }
            QPushButton:hover { background: #2563EB10; }
        """)
        b.clicked.connect(cb)
        return b
    def _pause_task(self, task_id):
        """暂停任务：把 DB 状态改 pending，渲染线程会在下个 clip 边界检测到后退出"""
        from autokat.models.db import update_task_status, get_task
        task = get_task(task_id)
        if not task:
            return
        if task["status"] != "running":
            # QMessageBox 在 offscreen 测试时会卡死, 改 silent return + 日志
            print(f"[pause] task #{task_id} 当前状态是 {task['status']}，无需暂停", flush=True)
            return
        update_task_status(task_id, "pending")
        self._log(f"⏸ 任务 #{task_id} 已暂停（{task['done']}/{task['total']} 已完成）")
        if getattr(self, "_current_task_id", None) == task_id:
            self._wiz_pause_btn.setEnabled(False)
        self._refresh_dashboard()
        self._refresh_sidebar()
    def _resume_task(self, task_id):
        """恢复任务：DB 状态改回 running，渲染线程读 _render_task 读 pending clips 自动从断点继续"""
        from autokat.models.db import update_task_status, get_task
        task = get_task(task_id)
        if not task:
            return
        if task["status"] not in ("pending", "failed"):
            print(f"[resume] task #{task_id} 当前状态是 {task['status']}，无需恢复", flush=True)
            return
        update_task_status(task_id, "running")
        self._log(f"▶ 任务 #{task_id} 已恢复（{task['done']}/{task['total']} 已完成）")
        threading.Thread(target=lambda: resume_pending_tasks(workers=2), daemon=True).start()
        QTimer.singleShot(2000, self._refresh_dashboard)
    def _retry_task(self, task_id):
        reset_count = prepare_task_retry(task_id)
        self._log(f"🔄 任务 #{task_id} 已准备重试（重置 {reset_count} 条失败成片）")
        self._resume_task(task_id)
    def _replay_task(self, task_id):
        """基于此新建：原"复现"按钮的回调。现在统一走 _enter_wizard_for(fork)
        复用完整的 snapshot 还原 + readonly 切换逻辑。"""
        self._enter_wizard_for(task_id, mode="fork")
    # ══════════════════════════════════════════════════════════
    # 向导：Step 1 - 素材导入
    # ══════════════════════════════════════════════════════════
    def _build_video_type_combo(self) -> QComboBox:
        """Factory for the per-page 视频类型 QComboBox.

        v3.2: 只在 Step 2 放一个 QComboBox 实例, Step 3 不再放独立副本
        (避免 UI 重复且 Qt 不允许同一 widget 挂在两个 layout 下)。
        全程只有 _wiz_video_type_step2 这一个实例, 作为唯一 source of truth。
        """
        combo = QComboBox()
        from autokat.core.ai_providers import VIDEO_TYPE_LABELS
        # v3.2: 用 VIDEO_TYPE_LABELS (口语化名字), key 保持不变所以不会破坏现有快照
        for _key, _label in VIDEO_TYPE_LABELS.items():
            combo.addItem(_label, _key)
        combo.setToolTip(
            "视频类型：决定 AI 怎么组织文案的结构和节奏。\n"
            "在 Step 2 选一次即可，下次点 AI 辅助生成会用新类型重出文案。"
        )
        return combo

    def _build_wizard_step1(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self._build_step_bar(1))
        # 标题行：标题 + 关闭向导按钮
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("📦 Step 1: 选择素材")
        title.setStyleSheet("font-size:20px; font-weight:800; color:#111827;")
        title_row.addWidget(title)
        title_row.addStretch()
        close_btn = QPushButton("关闭向导")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #FEF2F2; color: #EF4444; border: 1.5px solid #FECACA;
                border-radius: 8px; padding: 6px 14px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #EF4444; color: #FFFFFF; }
        """)
        close_btn.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)
        required_hint = QLabel(
            '<span style="color:#EF4444; font-weight:600;">* 必填</span> — '
            '勾选的素材才会参与此次生成')
        layout.addWidget(required_hint)
        # 任务名
        name_row = QHBoxLayout()
        name_label = QLabel("🏷 任务名称:")
        name_label.setStyleSheet("font-size:13px; font-weight:600; color:#111827; background:transparent;")
        name_row.addWidget(name_label)
        self._wiz_task_name = QLineEdit()
        self._wiz_task_name.setPlaceholderText("例如：春夏单鞋-红色系列")
        self._wiz_task_name.setMinimumWidth(250)
        # 双向同步：Step 1 改名 → Step 3 同步显示（避免用户在 Step 3 看到的是旧名）
        self._wiz_task_name.textChanged.connect(self._on_wiz_step1_tname_changed)
        name_row.addWidget(self._wiz_task_name, 1)
        layout.addLayout(name_row)
        # 导入按钮 + 进度
        import_row = QHBoxLayout()
        import_row.setSpacing(12)
        self._wiz_import_btn = QPushButton("📂  选择文件导入")
        self._wiz_import_btn.setMinimumHeight(44)
        self._wiz_import_btn.setStyleSheet(
            "QPushButton { background: #2563EB; color: #ffffff; font-size: 14px; font-weight: 600;"
            " border: none; border-radius: 10px; padding: 12px 24px; }"
            "QPushButton:hover { background: #1D4ED8; }"
            "QPushButton:pressed { background: #1E40AF; }"
        )
        self._wiz_import_btn.clicked.connect(self._on_wizard_import)
        import_row.addWidget(self._wiz_import_btn)
        self._wiz_import_progress = QProgressBar()
        self._wiz_import_progress.setVisible(False)
        self._wiz_import_progress.setMinimumHeight(8)
        self._wiz_import_progress.setTextVisible(True)
        self._wiz_import_progress.setFormat("%v/%m")
        import_row.addWidget(self._wiz_import_progress, 1)
        self._wiz_import_status = QLabel("未导入素材")
        self._wiz_import_status.setStyleSheet("font-size:12px; color:#6B7280; background:transparent; font-weight:500;")
        import_row.addWidget(self._wiz_import_status)
        import_row.addStretch()
        layout.addLayout(import_row)
        # 选择控制行
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        list_title = QLabel("素材列表（勾选参与生成）")
        list_title.setStyleSheet("font-size:14px; font-weight:700; color:#111827; background:transparent;")
        ctrl_row.addWidget(list_title)
        self._wiz_sel_count = QLabel("")
        self._wiz_sel_count.setStyleSheet("color:#2563EB; font-size:11px; font-weight:600; background:transparent;")
        ctrl_row.addWidget(self._wiz_sel_count)
        ctrl_row.addStretch()
        sel_all_btn = QPushButton("全选")
        sel_all_btn.setFixedHeight(28)
        sel_all_btn.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#2563EB; font-size:11px; font-weight:600;"
            " border:1.5px solid #2563EB; border-radius:8px; padding:3px 12px; }"
            "QPushButton:hover { background:#2563EB10; }"
        )
        ctrl_row.addWidget(sel_all_btn)
        sel_none_btn = QPushButton("取消")
        sel_none_btn.setFixedHeight(28)
        sel_none_btn.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#6B7280; font-size:11px; font-weight:500;"
            " border:1.5px solid #E5E7EB; border-radius:8px; padding:3px 12px; }"
            "QPushButton:hover { background:#FCFCFD; }"
        )
        ctrl_row.addWidget(sel_none_btn)
        # 打标签按钮
        self._wiz_tag_btn = QPushButton("🏷️  打标签")
        self._wiz_tag_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #2563EB; font-weight: 600;"
            " border: 1.5px solid #E5E7EB; border-radius: 8px; padding: 4px 12px; font-size: 11px; }"
            "QPushButton:hover { background: #FCFCFD; }"
        )
        self._wiz_tag_btn.clicked.connect(self._on_wiz_tag_selected)
        ctrl_row.addWidget(self._wiz_tag_btn)
        search_input = QLineEdit()
        search_input.setPlaceholderText("🔍 搜索素材名称或标签...")
        search_input.setFixedWidth(180)
        search_input.setFixedHeight(28)
        search_input.setStyleSheet("font-size:11px; padding:4px 10px;")
        ctrl_row.addWidget(search_input)
        # 多选标签筛选(与搜索框 AND 组合)
        self._wiz_tag_filter = _TagFilterButton()
        ctrl_row.addWidget(self._wiz_tag_filter)
        layout.addLayout(ctrl_row)
        # 用自定义 widget：复选框 + 文字区橡皮筋框选
        self._wiz_material_list = _RubberBandCheckListWidget()
        self._wiz_material_list.setStyleSheet(
            "QListWidget { background: #FFFFFF; border: 1.5px solid #E5E7EB;"
            " border-radius: 12px; padding: 4px; }"
            "QListWidget::item { padding: 8px 12px; border-radius: 8px; margin: 1px;"
            " color: #111827; }"
            "QListWidget::item:hover { background: #FCFCFD; }"
        )
        self._wiz_material_list._ensure_indicator_style()
        layout.addWidget(self._wiz_material_list, 1)
        nav = QHBoxLayout()
        nav.addStretch()
        self._wiz_step1_next = QPushButton("下一步 →")
        self._wiz_step1_next.setMinimumHeight(40)
        self._wiz_step1_next.setStyleSheet(
            "QPushButton { background: #2563EB; color: #ffffff; font-size: 14px;"
            " font-weight: 700; border: none; border-radius: 10px; padding: 8px 28px; }"
            "QPushButton:hover { background: #1D4ED8; }"
            "QPushButton:pressed { background: #1E40AF; }"
            "QPushButton:disabled { background: #D1D5DB; color: #6B7280; }"
        )
        self._wiz_step1_next.clicked.connect(lambda: self._go_to_step(2))
        self._wiz_step1_next.setEnabled(False)
        nav.addWidget(self._wiz_step1_next)
        layout.addLayout(nav)
        # 全选/取消
        def _toggle_all(checked):
            for i in range(self._wiz_material_list.count()):
                item = self._wiz_material_list.item(i)
                if not item.flags() & Qt.ItemIsUserCheckable:
                    continue
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self._update_wiz_selection()
        sel_all_btn.clicked.connect(lambda: _toggle_all(True))
        sel_none_btn.clicked.connect(lambda: _toggle_all(False))
        def _filter_list(text):
            self._refresh_with_filters()
        search_input.textChanged.connect(_filter_list)
        self._wiz_search_input = search_input
        self._wiz_tag_filter.tagsChanged.connect(lambda _s: self._refresh_with_filters())
        self._wiz_selected_materials = set()
        return page
    def _on_wiz_tag_selected(self):
        """步骤 1 列表的「打标签」按钮：收集所有勾选素材 → 弹打标对话框。"""
        checked_ids = []
        for i in range(self._wiz_material_list.count()):
            it = self._wiz_material_list.item(i)
            if it.checkState() == Qt.Checked:
                mid = it.data(Qt.UserRole)
                if mid:
                    checked_ids.append(mid)
        if not checked_ids:
            QMessageBox.information(self, "提示", "请先勾选要打标的素材")
            return
        self._show_tag_editor(checked_ids)
        # 刷新列表显示 tag(走统一入口,带上当前 tag 筛选)
        try:
            self._refresh_with_filters()
        except Exception:
            pass
    def _on_wizard_import(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择素材文件", "",
            "素材文件 (*.jpg *.jpeg *.png *.webp *.mp4 *.mov *.avi *.mkv);;所有文件 (*)")
        if not files:
            return
        self._wiz_import_btn.setEnabled(False)
        self._wiz_import_progress.setVisible(True)
        self._wiz_import_progress.setRange(0, len(files))
        self._wiz_import_progress.setValue(0)
        self._wiz_import_status.setText(f"0/{len(files)} 准备中...")
        self._wiz_material_list.clear()
        self._log(f"开始导入 {len(files)} 个文件...")
        # 在后台线程逐文件导入，实时回调更新 UI
        self._import_start_time = datetime.now()
        def _on_progress(current, total, filename, status):
            # 这个回调在 WorkerThread 的后台线程执行
            # 通过 Signal 桥接到主线程
            self._import_signal.emit(current, total, filename, status)
        # 使用 QThread + Signal 实现线程安全回调
        class ImportWorker(QThread):
            progress = Signal(int, int, str, str)
            finished = Signal(object)
            error = Signal(str)
            def run(self):
                from autokat.core.material import import_files_with_callback
                try:
                    # 用 Signal 桥接回调
                    def _cb(c, t, fn, s):
                        self.progress.emit(c, t, fn, s)
                    stats = import_files_with_callback(files, _cb)
                    self.finished.emit(stats)
                except Exception as e:
                    self.error.emit(str(e))
        self._import_worker = ImportWorker()
        self._import_worker.progress.connect(self._on_import_progress)
        self._import_worker.finished.connect(self._on_wizard_import_done)
        self._import_worker.error.connect(lambda e: self._log(f"导入失败: {e}"))
        self._import_worker.start()
    def _on_import_progress(self, current, total, filename, status):
        ts = datetime.now().strftime("%Y%m%d %H:%M:%S")
        elapsed = (datetime.now() - self._import_start_time).total_seconds()
        speed = f"{current/elapsed:.1f} 文件/秒" if elapsed > 0 else ""
        self._wiz_import_progress.setValue(current)
        self._wiz_import_progress.setFormat(f"{current}/{total}")
        self._wiz_import_status.setText(f"{current}/{total}  {speed}")
        # 实时在列表中添加/更新
        if status in ("done", "error"):
            icon = "✅" if status == "done" else "❌"
            # 从数据库刷新整个列表（因为子镜头可能会增加条目数）
            self._refresh_wizard_material_list()
            # 额外显示当前处理完成的条目
            item_text = f"{icon}  [{ts}] {filename} 处理完成"
            self._wiz_material_list.addItem(item_text)
            # 滚动到底部
            self._wiz_material_list.scrollToBottom()
            self._log(f"导入完成: {filename}")
        elif status == "processing":
            self._wiz_import_status.setText(f"{current}/{total} 正在处理 {filename}...  {speed}")
    def _on_wizard_import_done(self, stats):
        self._wiz_import_btn.setEnabled(True)
        self._wiz_import_progress.setVisible(False)
        total_imported = stats['images'] + stats['videos'] + stats['kenburns'] + stats['clips']
        self._wiz_import_status.setText(
            f"✔️ 导入完成: "
            f"图片{stats['images']} "
            f"视频{stats['videos']} "
            f"子镜头{stats['clips']} "
            f"KenBurns{stats['kenburns']}"
        )
        self._log(f"导入完成: {stats}")
        self._refresh_wizard_material_list()
        self._wiz_step1_next.setEnabled(True)
    def _refresh_with_filters(self) -> None:
        """统一入口:把当前搜索框文本 + 标签筛选按钮的选择一并传给列表刷新。"""
        text = self._wiz_search_input.text() if hasattr(self, "_wiz_search_input") else ""
        tags = self._wiz_tag_filter.selected_tags() if hasattr(self, "_wiz_tag_filter") else set()
        self._refresh_wizard_material_list(filter_text=text, tag_filter=tags)
    def _refresh_wizard_material_list(self, filter_text="", tag_filter=None):
        from PySide6.QtCore import Qt
        # 关键:信号连接只做一次,避免每次刷新都重复 connect 导致回调触发 N 次。
        if not getattr(self, "_wiz_list_signals_wired", False):
            self._wiz_list_signals_wired = True
            def _on_item_changed(item):
                # 拖框自动勾选走 setCheckState → 触发 itemChanged;
                # 这里把选中集合同步到 _wiz_selected_materials,
                # 下次 _refresh 后才能从该集合恢复勾选状态。
                if getattr(self, "_wiz_refreshing", False):
                    return
                if not (item.flags() & Qt.ItemIsUserCheckable):
                    return
                self._update_wiz_selection()
                self._wiz_selected_materials = set()
                for i in range(self._wiz_material_list.count()):
                    it = self._wiz_material_list.item(i)
                    if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked:
                        self._wiz_selected_materials.add(it.data(Qt.UserRole))
            self._wiz_material_list.itemChanged.connect(_on_item_changed)
            def _on_item_dblclicked(item):
                if not (item.flags() & Qt.ItemIsUserCheckable):
                    return
                self._material_preview(item)
            self._wiz_material_list.itemDoubleClicked.connect(_on_item_dblclicked)
        # 用标志而不是 blockSignals 屏蔽 _on_item_changed,
        # 否则 itemSelectionChanged 等其它信号也会被一起屏蔽,
        # 导致 _RubberBandCheckListWidget 的自动勾选失效。
        self._wiz_refreshing = True
        # 临时断开 itemChanged 信号, 避免 200+ addItem 期间每行都触发 _on_item_changed
        # 进而触发 _update_wiz_selection 200 次再 setCheckState 又触发 N 次 (性能 O(N^2))
        # 200+ 素材时实测会 hang 30+ 秒
        try:
            self._wiz_material_list.blockSignals(True)
        except Exception:
            pass
        try:
            self._wiz_material_list.clear()
            materials = get_all_materials()
            tag_filter_set = set(tag_filter or [])
            groups = {}
            for m in materials:
                fp_lower = m["file_path"].lower()
                tags_val = m["tags"] or "[]"
                tags_list = json.loads(tags_val) if isinstance(tags_val, str) else tags_val
                tags_text = " ".join(tags_list)
                search_text = f"{Path(m['file_path']).name} {tags_text}"
                if filter_text and filter_text.lower() not in search_text.lower():
                    continue
                # 多选标签筛选:素材含任一选中 tag 即命中(OR)
                if tag_filter_set and not (set(tags_list) & tag_filter_set):
                    continue
                groups.setdefault(m["mat_type"], []).append(m)
            def add_group(title, items):
                if not items:
                    return
                sep = QListWidgetItem(f"--- {title} ({len(items)}) ---")
                sep.setFlags(sep.flags() & ~Qt.ItemIsUserCheckable)
                sep.setFlags(sep.flags() & ~Qt.ItemIsSelectable)
                font = sep.font()
                font.setBold(True)
                sep.setFont(font)
                self._wiz_material_list.addItem(sep)
                for m in items:
                    disp = m.get("display_name") or Path(m["file_path"]).stem
                    text = f"  {disp}  ({m['duration']:.1f}s)"
                    tags_list = json.loads(m["tags"] or "[]") if isinstance(m["tags"], str) else (m["tags"] or [])
                    if tags_list:
                        tags_str = ", ".join(str(t) for t in tags_list[:3])
                        text += f"  [{tags_str}]"
                    item = QListWidgetItem(text)
                    item.setData(Qt.UserRole, m["id"])
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    was = hasattr(self, "_wiz_selected_materials") and m["id"] in self._wiz_selected_materials
                    item.setCheckState(Qt.Checked if was else Qt.Unchecked)
                    self._wiz_material_list.addItem(item)
            add_group("Images", groups.get("image", []))
            add_group("Videos", groups.get("video", []))
            if materials:
                self._wiz_step1_next.setEnabled(True)
        finally:
            self._wiz_refreshing = False
            try:
                self._wiz_material_list.blockSignals(False)
            except Exception:
                pass
        self._update_wiz_selection()
    # ══════════════════════════════════════════════════════════
    # 向导：Step 2 - 文案与配音
    # ══════════════════════════════════════════════════════════
    def _build_wizard_step2(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self._build_step_bar(2))
        # 标题行：标题 + 关闭向导按钮
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("📝 Step 2: 文案与配音")
        title.setStyleSheet("font-size:20px; font-weight:800; color:#111827;")
        title_row.addWidget(title)
        title_row.addStretch()
        close_btn = QPushButton("关闭向导")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #FEF2F2; color: #EF4444; border: 1.5px solid #FECACA;
                border-radius: 8px; padding: 6px 14px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #EF4444; color: #FFFFFF; }
        """)
        close_btn.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)
        required_hint = QLabel('<span style="color:#EF4444; font-weight:600;">* 必填</span> — 输入口播文案')
        layout.addWidget(required_hint)
        # AI 辅助按钮行
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        ai_btn = QPushButton("🤖 AI 辅助生成")
        ai_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #2563EB; border: 1.5px solid #2563EB;
                border-radius: 8px; padding: 7px 16px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #2563EB10; }
        """)
        ai_btn.clicked.connect(self._on_wizard_ai_script)
        ai_row.addWidget(ai_btn)
        # 视频类型下拉框：与 AI 辅助生成按钮同行，因为类型直接决定 AI 文案风格
        _type_lbl = QLabel("视频类型:")
        _type_lbl.setStyleSheet("color:#374151; font-size:12px; font-weight:600;")
        ai_row.addWidget(_type_lbl)
        self._wiz_video_type_step2 = self._build_video_type_combo()
        ai_row.addWidget(self._wiz_video_type_step2)
        from_history_btn = QPushButton("📋 从历史选择")
        from_history_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #2563EB; border: 1.5px solid #2563EB;
                border-radius: 8px; padding: 7px 16px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #2563EB10; }
        """)
        from_history_btn.clicked.connect(self._on_wizard_script_from_history)
        ai_row.addWidget(from_history_btn)
        ai_row.addStretch()
        layout.addLayout(ai_row)
        # 文案输入
        self._wiz_script_name = QLineEdit()
        self._wiz_script_name.setPlaceholderText("文案名称（可选）")
        layout.addWidget(self._wiz_script_name)
        self._wiz_script_editor = QTextEdit()
        self._wiz_script_editor.setPlaceholderText("在此输入口播文案...")
        self._wiz_script_editor.setMinimumHeight(150)
        layout.addWidget(self._wiz_script_editor, 1)
        # 配音设置（选填，折叠式）
        tts_group = QGroupBox("配音设置（选填，有默认值）")
        tts_group.setStyleSheet("QGroupBox { font-weight: 600; font-size: 12px; color: #2563EB; text-transform: uppercase; letter-spacing: 0.5px; }")
        tts_layout = QFormLayout(tts_group)
        self._wiz_voice = QComboBox()
        self._wiz_voice.addItems([
            "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural",
            "zh-CN-XiaoyiNeural", "zh-CN-YunjianNeural",
            "th-TH-PremwadeeNeural", "th-TH-NiwatNeural",
            "en-US-JennyNeural", "en-US-GuyNeural",
        ])
        tts_layout.addRow("🎤 音色:", self._wiz_voice)
        self._wiz_rate = QSlider(Qt.Horizontal)
        self._wiz_rate.setRange(-50, 50)
        self._wiz_rate.setValue(0)
        self._wiz_rate_label = QLabel("+0%")
        # 拖动时实时提示：语速快慢影响视频时长（hover 弹 tooltip）
        self._wiz_rate.setToolTip(
            "语速快慢直接影响最终视频时长。\n"
            "• 慢读（如 -30%）：×1.43 时长，适合节奏舒缓的种草\n"
            "• 正常（+0%）：×1.00 基准\n"
            "• 快读（如 +30%）：×0.77 时长，适合信息密集的测评"
        )
        # 拖动时标签旁追加预估时长系数（视觉反馈，不打断流程）
        def _on_rate_change(v):
            coef = 1.0 / (1 + v / 100.0)
            self._wiz_rate_label.setText(f"{v:+d}%  (×{coef:.2f} 时长)")
        self._wiz_rate.valueChanged.connect(_on_rate_change)
        # 反向：主语速拖动时同步给 AI 弹窗里的 ai_rate（如果弹窗存在）
        self._wiz_rate.valueChanged.connect(lambda v: self._wiz_ai_rate_dlg.setValue(v) if hasattr(self, "_wiz_ai_rate_dlg") else None)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self._wiz_rate)
        rate_row.addWidget(self._wiz_rate_label)
        tts_layout.addRow("语速:", rate_row)
        self._wiz_pitch = QSlider(Qt.Horizontal)
        self._wiz_pitch.setRange(-50, 50)
        self._wiz_pitch.setValue(0)
        self._wiz_pitch_label = QLabel("+0Hz")
        self._wiz_pitch.valueChanged.connect(lambda v: self._wiz_pitch_label.setText(f"{v:+d}Hz"))
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(self._wiz_pitch)
        pitch_row.addWidget(self._wiz_pitch_label)
        tts_layout.addRow("音调:", pitch_row)
        layout.addWidget(tts_group)
        # 底部导航
        nav = QHBoxLayout()
        back_btn = QPushButton("← 上一步")
        back_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #6B7280; border: 1.5px solid #E5E7EB;
                border-radius: 10px; padding: 8px 22px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background: #FCFCFD; border-color: #D1D5DB; }
        """)
        back_btn.clicked.connect(lambda: self._go_to_step(1))
        nav.addWidget(back_btn)
        nav.addStretch()
        self._wiz_step2_next = QPushButton("下一步 →")
        self._wiz_step2_next.setMinimumHeight(40)
        self._wiz_step2_next.setStyleSheet("""
            QPushButton {
                background: #2563EB; color: #ffffff; font-size: 14px; font-weight: 700;
                border: none; border-radius: 10px; padding: 8px 28px;
            }
            QPushButton:hover { background: #1D4ED8; }
            QPushButton:pressed { background: #1E40AF; }
            QPushButton:disabled { background: #D1D5DB; color: #6B7280; }
        """)
        self._wiz_step2_next.clicked.connect(lambda: self._go_to_step(3))
        self._wiz_step2_next.setEnabled(False)
        nav.addWidget(self._wiz_step2_next)
        layout.addLayout(nav)
        # 当输入文案时启用下一步
        self._wiz_script_editor.textChanged.connect(self._on_wizard_script_changed)
        # 用户手动改音色时锁定自动选：之后不再因文案语种覆盖
        self._wiz_voice.currentTextChanged.connect(
            lambda *_: setattr(self, "_wiz_voice_user_set", True)
        )
        self._wiz_voice_user_set = False  # wizard 内初始为 False，task 启动时 reset
        return page
    def _on_wizard_script_changed(self):
        has_text = bool(self._wiz_script_editor.toPlainText().strip())
        self._wiz_step2_next.setEnabled(has_text)
        # 按文案语种自动选默认音色（不覆盖用户手动选的）
        text = self._wiz_script_editor.toPlainText().strip()
        lang = self._detect_text_lang(text)
        if lang:
            self._apply_default_voice_for_lang(lang)
        # 更新 Step 3 生成数量和提示
        text = self._wiz_script_editor.toPlainText().strip()
        if hasattr(self, '_wiz_count') and self._wiz_script_editor.isVisible():
            script_count = len([s for s in text.split("\n---\n") if s.strip()])
            if script_count > 1:
                self._wiz_count.setValue(script_count)
                self._wiz_count.setToolTip(f"当前文案 {script_count} 条")
            # 更新提示
            count_val = self._wiz_count.value()
            if hasattr(self, '_wiz_count_tip'):
                if script_count > 1 and count_val < script_count:
                    self._wiz_count_tip.setText("💡 文案将随机选择")
                elif script_count > 1 and count_val > script_count:
                    self._wiz_count_tip.setText("💡 文案将随机选择，会有重复")
                else:
                    self._wiz_count_tip.setText("")
    def _detect_text_lang(self, text: str) -> str:
        """检测文案语种：返回 'zh' / 'en' / 'th' / ''（混合/不确定）"""
        if not text:
            return ""
        th = sum(1 for c in text if '\u0E00' <= c <= '\u0E7F')
        zh = sum(1 for c in text if '\u4E00' <= c <= '\u9FFF')
        en = sum(1 for c in text if c.isalpha() and c.isascii())
        if th > 0 and th >= zh and th >= en:
            return "th"
        if zh > 0 and zh >= en:
            return "zh"
        if en > 0:
            return "en"
        return ""
    def _apply_default_voice_for_lang(self, lang: str):
        """根据语种设置默认音色（用户手动改过后不再覆盖）"""
        if not lang or getattr(self, "_wiz_voice_user_set", False):
            return
        voice_map = {
            "zh": "zh-CN-XiaoxiaoNeural",
            "en": "en-US-JennyNeural",
            "th": "th-TH-PremwadeeNeural",
        }
        target = voice_map.get(lang)
        if not target:
            return
        if not hasattr(self, "_wiz_voice") or self._wiz_voice is None:
            return
        idx = self._wiz_voice.findText(target)
        if idx >= 0 and self._wiz_voice.currentText() != target:
            self._wiz_voice.setCurrentIndex(idx)
    def _on_wizard_ai_script(self):
        """打开 AI 写文案对话框（异步生成，不卡 UI）"""
        dlg = QDialog(self)
        dlg.setWindowTitle("AI 辅助生成文案")
        dlg.setMinimumWidth(520)
        dlg_layout = QVBoxLayout(dlg)
        form = QFormLayout()
        topic_input = QLineEdit()
        topic_input.setPlaceholderText("例如：春夏单鞋、厨房收纳...")
        form.addRow("选题 *:", topic_input)

        # v3.2: B+C 设计 — 视频类型为主控 (默认显示), 文案风格藏在 ⚙ 高级 后面
        # 视频类型变 → 自动默认文案风格 (从 VIDEO_TYPE_DEFAULT_STYLE 取)
        from autokat.core.ai_providers import (
            VIDEO_TYPE_LABELS, VIDEO_TYPE_DEFAULT_STYLE, VIDEO_TYPE_TOOLTIP,
        )
        from autokat.core.writer import list_style_choices, STYLE_TOOLTIP

        # v3.2: tooltip 提示图标 — 下拉菜单不容易让用户发现 tooltip,
        # 在 combobox 旁加一个灰色 "?" QLabel, 鼠标悬停时显示完整说明
        def _make_help_label(tooltip_text: str) -> QLabel:
            lbl = QLabel("?")
            lbl.setToolTip(tooltip_text)
            lbl.setStyleSheet(
                "color:#6B7280; font-weight:700; font-size:13px; "
                "background:#F3F4F6; border-radius:9px; "
                "padding:0 6px; margin-left:4px;"
            )
            lbl.setCursor(Qt.PointingHandCursor)
            return lbl

        video_type_input = QComboBox()
        for _key, _label in VIDEO_TYPE_LABELS.items():
            video_type_input.addItem(_label, _key)
        video_type_input.setToolTip(VIDEO_TYPE_TOOLTIP)
        # 默认值: 与 Step 2/3 当前选择同步
        _wiz_vt = getattr(self, "_wiz_video_type_step2", None)
        if _wiz_vt is not None:
            _idx = video_type_input.findData(_wiz_vt.currentData() or "auto")
            if _idx >= 0:
                video_type_input.setCurrentIndex(_idx)
        # v3.2: 把 combobox 和 ? 图标一起装进 QWidget, 视频类型 整行作为一个 form row
        video_type_row = QWidget()
        _vt_row_layout = QHBoxLayout(video_type_row)
        _vt_row_layout.setContentsMargins(0, 0, 0, 0)
        _vt_row_layout.setSpacing(6)
        _vt_row_layout.addWidget(video_type_input, 1)
        _vt_row_layout.addWidget(_make_help_label(VIDEO_TYPE_TOOLTIP), 0)
        form.addRow("视频类型:", video_type_row)

        # ⚙ 高级 checkbox — 展开文案风格选择
        advanced_checkbox = QCheckBox("⚙ 高级 (手动设置文案风格)")
        advanced_checkbox.setChecked(False)
        form.addRow("", advanced_checkbox)

        # 文案风格 (默认隐藏, 勾选高级才显示)
        style_input = QComboBox()
        for _label, _key in list_style_choices():
            style_input.addItem(_label, _key)
        style_input.setToolTip(STYLE_TOOLTIP)
        # v3.2: 整行装进 QWidget (combobox + ? 图标), 高级展开时整体显示/隐藏
        style_row = QWidget()
        _st_row_layout = QHBoxLayout(style_row)
        _st_row_layout.setContentsMargins(0, 0, 0, 0)
        _st_row_layout.setSpacing(6)
        _st_row_layout.addWidget(style_input, 1)
        _st_row_layout.addWidget(_make_help_label(STYLE_TOOLTIP), 0)
        style_row.setVisible(False)
        form.addRow("文案风格:", style_row)

        def _sync_default_style(video_type_key: str) -> None:
            """视频类型变 → 默认文案风格 (仅在用户未手动改过时生效)。"""
            default = VIDEO_TYPE_DEFAULT_STYLE.get(video_type_key)
            if default is None:
                return
            idx = style_input.findData(default)
            if idx >= 0:
                style_input.setCurrentIndex(idx)

        def _toggle_advanced(checked: bool) -> None:
            style_row.setVisible(checked)
            if checked:
                _sync_default_style(video_type_input.currentData())

        advanced_checkbox.toggled.connect(_toggle_advanced)
        video_type_input.currentIndexChanged.connect(
            lambda _i: _sync_default_style(video_type_input.currentData())
        )

        provider_input = QComboBox()
        provider_input.addItem("本地模型", "local")
        provider_input.addItem("DeepSeek", "deepseek")
        form.addRow("AI 模型:", provider_input)
        deepseek_config_btn = QPushButton("配置并测试 DeepSeek")
        deepseek_config_btn.setVisible(False)
        form.addRow("", deepseek_config_btn)
        provider_input.currentIndexChanged.connect(
            lambda _: deepseek_config_btn.setVisible(provider_input.currentData() == "deepseek")
        )

        def _configure_deepseek():
            from autokat.core.ai_providers import (
                DeepSeekWriterProvider, load_ai_settings, load_deepseek_key,
                save_ai_settings, save_deepseek_key,
            )
            cfg = load_ai_settings()
            config_dlg = QDialog(dlg)
            config_dlg.setWindowTitle("DeepSeek 配置")
            config_layout = QFormLayout(config_dlg)
            key_edit = QLineEdit(load_deepseek_key())
            key_edit.setEchoMode(QLineEdit.Password)
            url_edit = QLineEdit(cfg.get("deepseek_url", "https://api.deepseek.com/v1/chat/completions"))
            model_edit = QLineEdit(cfg.get("deepseek_model", "deepseek-chat"))
            status_label = QLabel("")
            test_btn = QPushButton("真实测试连接")
            save_btn = QPushButton("保存")
            config_layout.addRow("API Key:", key_edit)
            config_layout.addRow("API 地址:", url_edit)
            config_layout.addRow("模型名称:", model_edit)
            config_layout.addRow(test_btn, save_btn)
            config_layout.addRow(status_label)

            def _test():
                try:
                    result = DeepSeekWriterProvider(
                        key_edit.text(), url_edit.text(), model_edit.text()
                    ).test_connection()
                    status_label.setText(f"✅ 连接成功：{result['response']}")
                except Exception as exc:
                    status_label.setText(f"❌ {exc}")

            def _save():
                # v3.2: save_deepseek_key 返回 bool (False = keychain 写入失败)
                # macOS 15+ 经常对未签名脚本拒绝写入 keychain (exit 36),
                # 此时 key 仍可用 (本次会话 in-memory), 但需要让用户看到错误
                # 而不是看到 raw CalledProcessError stacktrace。
                key_ok = save_deepseek_key(key_edit.text())
                # ai_settings.json + in-memory 配置无论如何都要写, 否则本会话都跑不起来
                new_cfg = load_ai_settings()
                new_cfg.update({
                    "provider": "deepseek",
                    "deepseek_url": url_edit.text().strip(),
                    "deepseek_model": model_edit.text().strip(),
                })
                save_ai_settings(new_cfg)
                set_deepseek_config(key_edit.text(), url_edit.text(), model_edit.text())
                if not key_ok:
                    status_label.setText(
                        "⚠️ Keychain 保存失败 (详见控制台日志)。"
                        "本次会话仍可用 (已写入内存 + ai_settings.json),"
                        "下次启动需重新填入 API key。"
                    )
                    # 不关闭对话框 — 让用户能看到错误并决定下一步
                    return
                config_dlg.accept()

            test_btn.clicked.connect(_test)
            save_btn.clicked.connect(_save)
            config_dlg.exec()
        deepseek_config_btn.clicked.connect(_configure_deepseek)
        detail_input = QLineEdit()
        detail_input.setPlaceholderText("可选：补充细节描述")
        form.addRow("细节:", detail_input)
        feature_input = QLineEdit()
        feature_input.setPlaceholderText("可选：产品特性/核心卖点")
        form.addRow("卖点:", feature_input)
        # 语言选择
        lang_input = QComboBox()
        lang_input.addItems(["中文", "泰文", "英文"])
        lang_input.setCurrentIndex(0)
        form.addRow("语言:", lang_input)
        ai_rate = QSlider(Qt.Horizontal)
        ai_rate.setRange(-50, 50)
        ai_rate.setValue(0)
        ai_rate.setTickPosition(QSlider.TicksBelow)
        ai_rate.setTickInterval(25)
        ai_rate_label = QLabel("+0%")
        ai_rate_row = QHBoxLayout()
        ai_rate_row.addWidget(ai_rate, 1)
        ai_rate_row.addWidget(ai_rate_label, 0)
        form.addRow("语速（拖动时实时算字数）:", ai_rate_row)
        # 实时字数预估 tip：选中状态用绿底
        ai_chars_tip = QLabel("")
        ai_chars_tip.setWordWrap(True)
        ai_chars_tip.setStyleSheet(
            "color:#6B7280; font-size:11px; background:transparent; padding: 2px 6px;"
        )
        form.addRow("📏 预计文案字数:", ai_chars_tip)
        def _update_ai_chars_tip(*_):
            """语速+语言+时长 任何一项变，重算预计字数"""
            try:
                from autokat.core.writer import (
                    estimate_chars_for_lang, estimate_chars_for_duration_range,
                )
                lang_idx = lang_input.currentIndex()
                lang_code = ["zh", "th", "en"][lang_idx]
                dmin = dur_min.value()
                dmax = dur_max.value()
                rate = ai_rate.value()
                ai_rate_label.setText(f"{rate:+d}%")
                # v3.2: 始终用 estimate_chars_for_duration_range (默认 margin=0.10)
                # 让 UI 提示范围与后端 enforce 完全一致; dmin==dmax 也显示范围
                target_min, target_max = estimate_chars_for_duration_range(
                    lang_code, dmin, dmax, rate,
                )
                _, _, ideal_min = estimate_chars_for_lang(lang_code, dmin, rate, margin=0)
                _, _, ideal_max = estimate_chars_for_lang(lang_code, dmax, rate, margin=0)
                target_min = min(target_min, target_max)
                if dmax == dmin:
                    txt = (f"~{target_min}-{target_max} 字符 "
                           f"（目标 {ideal_min}, {lang_code}, {dmin}s, 语速 {rate:+d}%）")
                else:
                    txt = (f"~{target_min}-{target_max} 字符 "
                           f"（目标 {ideal_min}-{ideal_max}, {lang_code}, "
                           f"{dmin}-{dmax}s, 语速 {rate:+d}%）")
                ai_chars_tip.setText(txt)
                ai_chars_tip.setStyleSheet(
                    "color:#10B981; font-size:11px; font-weight:600; "
                    "background:#ECFDF510; padding: 4px 8px; border-radius:6px;"
                )
            except Exception as e:
                ai_chars_tip.setText(f"⚠️ 预估失败: {e}")
        # 文案数量
        count_input = QSpinBox()
        count_input.setRange(1, 500)
        count_input.setValue(5)
        count_input.setSuffix(" 条")
        form.addRow("生成数量:", count_input)
        # 时长范围
        dur_row = QHBoxLayout()
        dur_min = QSpinBox()
        dur_min.setRange(5, 300)
        dur_min.setValue(15)
        dur_min.setSuffix("s")
        dur_label = QLabel(" ~ ")
        dur_max = QSpinBox()
        dur_max.setRange(5, 300)
        dur_max.setValue(30)
        dur_max.setSuffix("s")
        dur_row.addWidget(dur_min)
        dur_row.addWidget(dur_label)
        dur_row.addWidget(dur_max)
        dur_row.addStretch()
        form.addRow("时长范围:", dur_row)
        # 时长 + 语速 + 语言 任意一项变都重算字数 tip（之前 dur_min/dur_max 未定义就调用是 forward ref bug）
        ai_rate.valueChanged.connect(_update_ai_chars_tip)
        # 拖动时同步给主语速滑块（Step 2 下方那个），保证两边一致
        ai_rate.valueChanged.connect(lambda v: self._wiz_rate.setValue(v) if hasattr(self, "_wiz_rate") else None)
        dur_min.valueChanged.connect(_update_ai_chars_tip)
        dur_max.valueChanged.connect(_update_ai_chars_tip)
        lang_input.currentIndexChanged.connect(_update_ai_chars_tip)
        _update_ai_chars_tip()
        dlg_layout.addLayout(form)
        # 进度指示区域（包含进度条 + 状态文字）  
        progress_widget = QWidget()
        progress_layout = QVBoxLayout(progress_widget)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(4)
        ai_progress_bar = QProgressBar()
        ai_progress_bar.setVisible(False)
        ai_progress_bar.setMinimumHeight(6)
        ai_progress_bar.setTextVisible(False)
        progress_layout.addWidget(ai_progress_bar)
        ai_status_label = QLabel("")
        ai_status_label.setStyleSheet("color:#6B7280; font-size:11px; background:transparent;")
        progress_layout.addWidget(ai_status_label)
        dlg_layout.addWidget(progress_widget)
        self._ai_results_list = QListWidget()
        self._ai_results_list.setMinimumHeight(180)
        self._ai_results_list.setStyleSheet("QListWidget{background:#FFFFFF;border:1.5px solid #E5E7EB;border-radius:10px;padding:4px;}")
        self._ai_results_list.setVisible(False)
        dlg_layout.addWidget(self._ai_results_list)
        result_area = QTextEdit()
        result_area.setReadOnly(False)
        result_area.setPlaceholderText("生成的文案将显示在这里...选择左侧列表可编辑...")
        result_area.setMinimumHeight(150)
        dlg_layout.addWidget(result_area)
        btn_row = QHBoxLayout()
        gen_btn = QPushButton("🤖 生成")
        gen_btn.setMinimumHeight(36)
        btn_row.addWidget(gen_btn)
        use_btn = QPushButton("✅ 使用此文案")
        use_btn.setMinimumHeight(36)
        use_btn.setEnabled(False)
        btn_row.addWidget(use_btn)
        dlg_layout.addLayout(btn_row)
        def _update_progress(done, total, status):
            """由后台线程调用的进度更新"""
            if total > 0:
                ai_progress_bar.setVisible(True)
                ai_progress_bar.setRange(0, total)
                ai_progress_bar.setValue(done)
            if status == "downloading":
                pct = int(done / max(total, 1) * 100) if total > 0 else 0
                ai_status_label.setText(f"下载模型: {pct}% ({done/1024/1024:.0f}/{total/1024/1024:.0f} MB)")
            elif status == "loading":
                ai_status_label.setText("正在加载模型到内存...")
            elif status == "done":
                ai_progress_bar.setVisible(False)
                ai_status_label.setText("✅ 模型加载完成，正在生成文案...")
            elif "检查" in status or "缓存" in status:
                ai_status_label.setText(status)
            elif "失败" in status:
                ai_progress_bar.setVisible(False)
                ai_status_label.setText(f"❌ {status}")
            else:
                ai_status_label.setText(status)
        # 生成逻辑 - 后台线程执行
        def _do_generate():
            topic = topic_input.text().strip()
            if not topic:
                QMessageBox.warning(dlg, "提示", "请输入选题")
                return
            # v3.2: 文案风格下拉框用 (label, key) 模式, 取 currentData() 而不是 currentText()
            style = style_input.currentData() or style_input.currentText()
            _vt_data = video_type_input.currentData()
            _captured_video_type = _vt_data or "auto"
            detail = detail_input.text().strip() or None
            features = feature_input.text().strip() or None
            gen_btn.setEnabled(False)
            use_btn.setEnabled(False)
            ai_progress_bar.setVisible(True)
            ai_progress_bar.setRange(0, count_input.value())
            ai_progress_bar.setValue(0)
            ai_status_label.setText("正在准备...")
            result_area.setText("正在生成中...")
            # 后台线程生成
            ai_results = []
            ai_result_meta = []
            # 必须把 MainWindow 的属性在 worker 类定义之前捕获到闭包局部变量里，
            # 否则 worker 里的 self 是 GenWorker 实例，访问 self._wiz_video_type_step2
            # 会 AttributeError，访问 self._wiz_selected_materials 会因为 getattr 默认值
            # 默默退化成空集合，AI 文案生成拿不到素材能力摘要。
            _captured_provider = provider_input.currentData()
            _captured_selected_materials = list(
                getattr(self, "_wiz_selected_materials", set()) or set()
            )
            class GenWorker(QThread):
                result_signal = Signal(int, str, str, int, float)
                error_signal = Signal(str)
                progress_signal = Signal(int, int, str)
                def run(self):
                    try:
                        from autokat.core.writer import _LOAD_PROGRESS_CALLBACK
                        old_cb = _LOAD_PROGRESS_CALLBACK
                        import autokat.core.writer as _wr
                        def progress_cb(done, total, status):
                            self.progress_signal.emit(done, total, status)
                        _wr._LOAD_PROGRESS_CALLBACK = progress_cb
                        try:
                            lang_map = {"中文": "zh", "泰文": "th", "英文": "en"}
                            ai_lang = lang_map.get(lang_input.currentText(), "zh")
                            ai_cnt = count_input.value()
                            dmin = dur_min.value()
                            dmax = dur_max.value()
                            rate = ai_rate.value()
                            # 按语言+语速+目标时长算字数范围，注入到 prompt 强约束
                            target_min, target_max = estimate_chars_for_duration_range(
                                ai_lang, dmin, dmax, rate,
                            )
                            accepted = []
                            for gi in range(ai_cnt):
                                extra = f"，第{gi+1}条，目标时长{dmin}-{dmax}秒（{ai_lang}，语速{rate:+d}%）"
                                def quality_progress(backend, attempt, total_attempts, message, _gi=gi):
                                    self.progress_signal.emit(
                                        _gi, ai_cnt,
                                        f"第 {_gi + 1} 条 · {backend} 第 {attempt}/{total_attempts} 次：{message}",
                                    )
                                generated = generate_script_by_topic_detailed(
                                    topic, style, detail, features,
                                    lang=ai_lang, extra_instruction=extra,
                                    target_chars_min=target_min,
                                    target_chars_max=target_max,
                                    target_duration_min=dmin,
                                    target_duration_max=dmax,
                                    accepted_texts=accepted,
                                    progress_callback=quality_progress,
                                    provider=_captured_provider,
                                    video_type=_captured_video_type,
                                    material_capabilities=__import__(
                                        "autokat.core.material_analysis", fromlist=["capability_summary"]
                                    ).capability_summary(
                                        _captured_selected_materials or None
                                    ),
                                )
                                accepted.append(generated["text"])
                                quality = generated["quality"]
                                self.result_signal.emit(
                                    gi, generated["text"], generated["source"],
                                    quality["char_count"], quality["max_similarity"],
                                )
                        finally:
                            _wr._LOAD_PROGRESS_CALLBACK = old_cb
                    except Exception as e:
                        self.error_signal.emit(str(e))
            worker = GenWorker()
            worker.progress_signal.connect(_update_progress)
            def _accept_generated(idx, txt, source, char_count, similarity):
                ai_results.append(txt)
                ai_result_meta.append({
                    "source": source, "char_count": char_count,
                    "max_similarity": similarity,
                })
                dlg._ai_results = ai_results
                dlg._ai_result_meta = ai_result_meta
                cps = estimate_chars_for_lang(
                    ["zh", "th", "en"][lang_input.currentIndex()], 1, ai_rate.value(),
                    margin=0,
                )[2]
                estimated_seconds = char_count / max(1, cps)
                self._ai_results_list.addItem(
                    f"📄 文案 #{len(ai_results)} · {char_count}字符 · "
                    f"约{estimated_seconds:.1f}s · {source} · 相似度{similarity:.0%}"
                )
                ai_status_label.setText(f"✅ 已生成 {len(ai_results)}/{count_input.value()} 条合格文案")
                ai_progress_bar.setValue(len(ai_results))
                gen_btn.setEnabled(len(ai_results) < count_input.value())
                use_btn.setEnabled(len(ai_results) >= count_input.value())
                result_area.setText(txt)
                result_area.setVisible(True)
            worker.result_signal.connect(_accept_generated)
            worker.error_signal.connect(lambda err: (
                result_area.setText("❌ " + err),
                ai_status_label.setText("❌ 生成失败"),
                ai_progress_bar.setVisible(False),
                gen_btn.setEnabled(True),
            ))
            # Keep worker reference to prevent GC
            self._ai_gen_worker = worker; dlg._gen_worker = worker
            import weakref
            _wr = weakref.ref(self)
            self._ai_gen_worker.finished.connect(
                lambda: setattr(_wr(), "_ai_gen_worker", None) if _wr() and hasattr(_wr(), "_ai_gen_worker") else None
            )
            worker.start()
        gen_btn.clicked.connect(_do_generate)
        def _use_script():
            texts = getattr(dlg, '_ai_results', [])
            if not texts:
                text = result_area.toPlainText().strip()
                if text and "正在生成" not in text and "生成失败" not in text:
                    texts = [text]
            if texts:
                lang_map = {"中文": "zh", "泰文": "th", "英文": "en"}
                ai_lang = lang_map.get(lang_input.currentText(), "zh")
                target_min, target_max = estimate_chars_for_duration_range(
                    ai_lang, dur_min.value(), dur_max.value(), ai_rate.value(),
                )
                accepted = []
                for index, text in enumerate(texts):
                    quality = validate_script_quality(
                        text, topic_input.text().strip(), lang=ai_lang,
                        target_chars_min=target_min, target_chars_max=target_max,
                        detail=detail_input.text().strip() or None,
                        features=feature_input.text().strip() or None,
                        accepted_texts=accepted,
                        require_topic=ai_lang == "zh",
                    )
                    if not quality["valid"]:
                        QMessageBox.warning(
                            dlg, "文案质量校验失败",
                            f"第 {index + 1} 条未通过最终校验：\n"
                            + "\n".join(quality["reasons"]),
                        )
                        return
                    accepted.append(text)
                combined = "\n---\n".join(texts)
                self._wiz_script_editor.setText(combined)
                # Auto-set voice based on AI dialog language
                _ai_lang = lang_input.currentText()
                if _ai_lang == "泰文":
                    _tv = self._wiz_voice.findText("th-TH-PremwadeeNeural")
                    if _tv >= 0: self._wiz_voice.setCurrentIndex(_tv)
                elif _ai_lang == "英文":
                    _ev = self._wiz_voice.findText("en-US-JennyNeural")
                    if _ev >= 0: self._wiz_voice.setCurrentIndex(_ev)
                dlg.accept()
        use_btn.clicked.connect(_use_script)
        dlg.exec()
    def _use_ai_script(self, result_area, dlg):
        text = result_area.toPlainText().strip()
        if text:
            self._wiz_script_editor.setText(text)
            dlg.accept()
    def _on_wizard_script_from_history(self):
        scripts = list_scripts()
        if not scripts:
            QMessageBox.information(self, "提示", "没有历史文案记录")
            return
        items = [f"#{s['id']} {s['name']} ({s['created_at'][:16]})" for s in scripts]
        from PySide6.QtWidgets import QInputDialog
        ok, idx = QInputDialog.getItem(self, "选择历史文案", "选择一条文案:", items, 0, False)
        if ok and idx >= 0:
            s = scripts[idx]
            self._wiz_script_name.setText(s["name"])
            self._wiz_script_editor.setText(s["narration"])
            tts_cfg = json.loads(s["tts_config"]) if isinstance(s["tts_config"], str) else (s.get("tts_config") or {})
            if tts_cfg.get("voice"):
                idx = self._wiz_voice.findText(tts_cfg["voice"])
                if idx >= 0:
                    self._wiz_voice.setCurrentIndex(idx)
            self._log(f"已加载历史文案 #{s['id']}")
    # ══════════════════════════════════════════════════════════
    # 向导：Step 3 - 生成配置
    # ══════════════════════════════════════════════════════════
    def _build_wizard_step3(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self._build_step_bar(3))

        # 标题 + 任务名状态指示
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("\u2699\ufe0f Step 3: 生成配置")
        title.setStyleSheet("font-size:16px; font-weight:800; color:#111827;")
        title_row.addWidget(title)
        title_row.addStretch()
        self._wiz_step3_tname_status = QLabel("\u26a0\ufe0f 任务名为空")
        self._wiz_step3_tname_status.setStyleSheet(
            "font-size:11px; color:#DC2626; font-weight:700; padding:2px 8px;"
            "background:#FEF2F2; border-radius:8px;"
        )
        title_row.addWidget(self._wiz_step3_tname_status)
        close_btn = QPushButton("关闭向导")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #FEF2F2; color: #EF4444; border: 1.5px solid #FECACA;
                border-radius: 8px; padding: 6px 14px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #EF4444; color: #FFFFFF; }
        """)
        close_btn.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        # 任务名编辑（同步自 Step 1，用户可在 Step 3 直接修改 -> 输出目录按此命名）
        tname_row = QHBoxLayout()
        tname_row.setSpacing(8)
        tname_lbl = QLabel("\ud83c\udff7 任务名称:")
        tname_lbl.setStyleSheet("color:#374151; font-size:12px; font-weight:600; background:transparent;")
        tname_lbl.setFixedWidth(86)
        tname_row.addWidget(tname_lbl)
        self._wiz_step3_tname_edit = QLineEdit()
        self._wiz_step3_tname_edit.setPlaceholderText("必填：输出目录会按此命名")
        self._wiz_step3_tname_edit.setMinimumHeight(34)
        self._wiz_step3_tname_edit.textChanged.connect(self._on_wiz_step3_tname_changed)
        tname_row.addWidget(self._wiz_step3_tname_edit, 1)
        self._wiz_step3_output_hint = QLabel("\u2192 output/\u672a\u547d\u540d_\u65f6\u95f4\u6233/")
        self._wiz_step3_output_hint.setStyleSheet(
            "color:#6B7280; font-size:11px; font-family:Menlo,Consolas,monospace;"
        )
        tname_row.addWidget(self._wiz_step3_output_hint)
        layout.addLayout(tname_row)

        # \u2550\u2550\u2550 \u57fa\u7840\u914d\u7f6e\uff1a3 \u884c \u00d7 2 \u5217\uff0c\u884c\u9ad8 34px\u3001\u5782\u76f4\u95f4\u8ddd 12px \u2550\u2550\u2550
        config_group = QGroupBox("\u57fa\u7840\u914d\u7f6e")
        config_grid = QGridLayout(config_group)
        config_grid.setContentsMargins(14, 18, 14, 18)
        config_grid.setHorizontalSpacing(20)
        config_grid.setVerticalSpacing(12)

        def _add_lbl(parent_grid, row, col, text):
            l = QLabel(text)
            l.setStyleSheet("color:#374151; font-size:12px; font-weight:500; background:transparent;")
            l.setMinimumHeight(20)
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            parent_grid.addWidget(l, row, col)
            return l

        def _add_spin(parent_grid, row, col, min_v, max_v, default, suffix="", tooltip="", min_width=120):
            sb = QSpinBox()
            sb.setRange(min_v, max_v)
            sb.setValue(default)
            if suffix:
                sb.setSuffix(suffix)
            if tooltip:
                sb.setToolTip(tooltip)
            sb.setMinimumHeight(34)
            sb.setMinimumWidth(min_width)
            parent_grid.addWidget(sb, row, col)
            return sb

        def _add_combo(parent_grid, row, col, items, default_idx=0, min_width=80):
            c = QComboBox()
            c.addItems(items)
            c.setCurrentIndex(default_idx)
            c.setMinimumHeight(34)
            c.setMinimumWidth(min_width)
            parent_grid.addWidget(c, row, col)
            return c

        # Row 0: \u751f\u6210\u6570\u91cf | \u5e27\u7387 | \u6bcf\u6bb5\u65f6\u957f\uff083 \u9879 / \u884c\uff09
        _add_lbl(config_grid, 0, 0, "\u751f\u6210\u6570\u91cf:")
        self._wiz_count = _add_spin(config_grid, 0, 1, 1, 9999, 100, " \u6761", min_width=130)
        self._wiz_count.valueChanged.connect(lambda v: self._update_wiz_max_videos_label())
        _add_lbl(config_grid, 0, 2, "\u5e27\u7387:")
        self._wiz_fps = _add_combo(config_grid, 0, 3, ["30", "60"], min_width=80)
        _add_lbl(config_grid, 0, 4, "\u955c\u5934\u8282\u594f:")
        from PySide6.QtWidgets import QDoubleSpinBox
        self._wiz_shot_duration = QDoubleSpinBox()
        self._wiz_shot_duration.setRange(1.0, 5.0)
        self._wiz_shot_duration.setSingleStep(0.5)
        self._wiz_shot_duration.setDecimals(1)
        self._wiz_shot_duration.setValue(2.0)
        self._wiz_shot_duration.setSuffix(" \u79d2")
        self._wiz_shot_duration.setToolTip(
            "\u9ed8\u8ba4\u7531\u7cfb\u7edf\u6309\u53e3\u64ad\u65f6\u957f\u3001\u6807\u70b9\u548c\u89c6\u9891\u7c7b\u578b\u81ea\u52a8\u89c4\u5212\u3002\n"
            "\u524d 3 \u79d2\u4f18\u5148\u7d27\u51d1\u955c\u5934\uff0c\u666e\u901a\u955c\u5934\u907f\u514d\u5f02\u5e38\u77ed\u788e\u7247\uff0c"
            "30 \u79d2\u4ee5\u4e0a\u81ea\u52a8\u589e\u52a0 3-6 \u79d2\u7a33\u5b9a\u955c\u5934\u3002"
        )
        self._wiz_shot_duration.setEnabled(False)
        self._wiz_shot_duration.setSuffix(" \u79d2\uff08\u81ea\u52a8\uff09")
        self._wiz_shot_duration.setMinimumHeight(34)
        self._wiz_shot_duration.setMinimumWidth(130)
        config_grid.addWidget(self._wiz_shot_duration, 0, 5)

        # Row 1: \u5e76\u53d1\u8fdb\u7a0b | \u5207\u7247\u6700\u5927\u590d\u7528\u6b21\u6570(+ \u6700\u591a\u6df7\u526a\u663e\u793a) | \u5206\u8fa8\u7387
        _add_lbl(config_grid, 1, 0, "\u5e76\u53d1\u8fdb\u7a0b:")
        self._wiz_workers = _add_spin(
            config_grid, 1, 1, 0, 8, 0,
            tooltip="\u5e76\u53d1\u8fdb\u7a0b\u6570\u3002\n\u2022 2\uff1a\u4fdd\u5b88\uff08CPU \u8f83\u5f31\uff09\n\u2022 4\uff1a\u63a8\u8350\uff08M \u7cfb\u5217 8 \u6838+\uff09\n\u2022 6-8\uff1a\u5feb\u4f46 CPU \u5403\u6ee1",
            min_width=80,
        )
        self._wiz_workers.setSpecialValueText("\u81ea\u52a8")
        _add_lbl(config_grid, 1, 2, "\u5207\u7247\u6700\u5927\u590d\u7528\u6b21\u6570:")
        # \u5207\u7247\u6700\u5927\u590d\u7528\u6b21\u6570 spinbox + \u6700\u591a\u6df7\u526a N \u6761\u6210\u7247\uff0c\u540c\u4e00\u5355\u5143\u683c\u5185\u5e76\u6392
        max_uses_cell = QWidget()
        max_uses_layout = QHBoxLayout(max_uses_cell)
        max_uses_layout.setContentsMargins(0, 0, 0, 0)
        max_uses_layout.setSpacing(8)
        self._wiz_max_uses = QSpinBox()
        self._wiz_max_uses.setRange(1, 10)
        self._wiz_max_uses.setValue(5)
        self._wiz_max_uses.setToolTip(
            "\u540c batch \u5185\u6bcf\u4e2a\u5207\u7247\u6700\u591a\u88ab\u91cd\u590d\u4f7f\u7528\u51e0\u6b21\u3002\n"
            "\u2022 \u8d8a\u5c0f\u8d8a\u4e25\u3001\u540c batch \u5185\u5dee\u5f02\u5ea6\u8d8a\u9ad8\u3001\u53ef\u751f\u6210\u6210\u7247\u8d8a\u5c11\n"
            "\u2022 \u8d8a\u5927\u8d8a\u5bbd\u677e\u3001\u751f\u6210\u591a\u4f46\u5dee\u5f02\u5ea6\u4e0b\u964d\n"
            "\u9ed8\u8ba4 5 \u662f\u7ecf\u9a8c\u503c\uff0c\u5e73\u8861\u4e86\u201c\u8db3\u591f\u591a\u201d\u4e0e\u201c\u8db3\u591f\u5dee\u201d"
        )
        self._wiz_max_uses.setMinimumHeight(34)
        self._wiz_max_uses.setMinimumWidth(90)
        self._wiz_max_uses.valueChanged.connect(lambda v: self._mark_wiz_overridden("max_uses_per_slice"))
        self._wiz_max_uses.valueChanged.connect(lambda v: self._update_wiz_max_videos_label())
        max_uses_layout.addWidget(self._wiz_max_uses, 0)
        # v2.3 简化: 风险区域已去掉, 改为在 spinbox 后面显示 "最多混剪 N 条成片"
        self._wiz_max_videos_label = QLabel("")
        self._wiz_max_videos_label.setStyleSheet(
            "color:#059669; font-size:11px; font-weight:700; background:transparent;"
        )
        max_uses_layout.addWidget(self._wiz_max_videos_label, 0)
        max_uses_layout.addStretch(1)
        config_grid.addWidget(max_uses_cell, 1, 3)
        _add_lbl(config_grid, 1, 4, "\u5206\u8fa8\u7387:")
        self._wiz_resolution = _add_combo(
            config_grid, 1, 5,
            ["1080x1920 (9:16)", "1080x1440 (3:4)", "1080x1900", "1072x1920", "1088x1920"],
            min_width=140,
        )
        self._wiz_resolution.currentIndexChanged.connect(lambda i: self._mark_wiz_overridden("resolution"))

        config_grid.setColumnStretch(0, 0)
        config_grid.setColumnStretch(1, 1)
        config_grid.setColumnStretch(2, 0)
        config_grid.setColumnStretch(3, 1)
        config_grid.setColumnStretch(4, 0)
        config_grid.setColumnStretch(5, 1)

        layout.addWidget(config_group)
        # v3.2: Step 3 不再单独放视频类型下拉框 — Qt 不允许同一 widget 同时属于
        # 两个 page, 所以原代码在 Step 2/3 各放一个独立 QComboBox 互相同步, 但 UI 上重复,
        # 用户只需在 Step 2 选一次, Step 3 不用再选。
        # (Step 2 的 _wiz_video_type_step2 是唯一 source of truth)

        # 风险徽章已移除 (v2.3 简化): 改在 _wiz_max_uses 后面显示 "最多混剪 N 条成片"

        # v2.3 简化: 差异化生成区 = 主开关 + 4 个中文扰动档位 + 3 个原 checkbox (调色偏好已移除)
        random_group = QGroupBox("\u5dee\u5f02\u5316\u751f\u6210")
        random_outer = QVBoxLayout(random_group)
        random_outer.setContentsMargins(12, 10, 12, 10)
        random_outer.setSpacing(8)
        # Row 1: 主开关 + 4 个中文扰动档位
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        self._wiz_enable_perturb = QCheckBox("\u542f\u7528\u5dee\u5f02\u5316\u6270\u52a8")
        self._wiz_enable_perturb.setChecked(True)
        self._wiz_enable_perturb.setStyleSheet("color:#111827; font-size:12px; font-weight:700;")
        row1.addWidget(self._wiz_enable_perturb)
        # v2.3: 4 个扰动档位全部中文标签，PERT_LEVELS 内部 key 仍是英文 (off/low/med/high)
        _PERT_LABELS = ["不扰动", "轻度扰动", "中度扰动", "强度扰动"]
        self._wiz_pert_level_group = QButtonGroup(self)
        for idx, lv in enumerate(PERT_LEVELS):
            rb = QRadioButton(_PERT_LABELS[idx] if idx < len(_PERT_LABELS) else lv)
            rb.setStyleSheet("color:#374151; font-size:12px;")
            if lv == "med":
                rb.setChecked(True)
            self._wiz_pert_level_group.addButton(rb, idx)
            row1.addWidget(rb)
        row1.addStretch()
        random_outer.addLayout(row1)
        # Row 2: 3 个原 checkbox (调色偏好已移除)
        random_layout = QHBoxLayout()
        random_layout.setSpacing(18)
        self._wiz_shuffle = QCheckBox("\u6253\u4e71\u7d20\u6750\u987a\u5e8f")
        self._wiz_shuffle.setChecked(True)
        random_layout.addWidget(self._wiz_shuffle)
        self._wiz_transition = QCheckBox("\u968f\u673a\u8f6c\u573a")
        self._wiz_transition.setChecked(True)
        random_layout.addWidget(self._wiz_transition)
        # 切片组合差异度 (默认 ON, 是 v2.3 核心)
        self._wiz_slice_diversity = QCheckBox("\u5207\u7247\u7ec4\u5408\u5dee\u5f02\u5ea6")
        self._wiz_slice_diversity.setChecked(True)
        self._wiz_slice_diversity.setStyleSheet("color:#111827; font-size:12px; font-weight:600;")
        random_layout.addWidget(self._wiz_slice_diversity)
        random_layout.addStretch()
        random_outer.addLayout(random_layout)
        layout.addWidget(random_group)

        # \u2550\u2550\u2550 \u80cc\u666f\u97f3\u4e50\uff1a\u5355\u884c\u5e03\u5c40\uff08master + \u5355\u4e00 dropdown + \u97f3\u91cf + \u8bd5\u542c\uff09
        #   \u539f"\u542f\u7528 BGM + BGM \u5217\u8868(\u6bcf\u884c\u53ef\u52fe\u9009)"\u662f\u4e24\u4e2a\u52fe\u9009\u5c42\uff0c\u5df2\u5408\u5e76\u4e3a\u5355\u9009 dropdown \u2550\u2550\u2550
        bgm_group = QGroupBox("\u80cc\u666f\u97f3\u4e50")
        bgm_layout = QHBoxLayout(bgm_group)
        bgm_layout.setContentsMargins(12, 10, 12, 10)
        bgm_layout.setSpacing(10)
        self._wiz_enable_bgm = QCheckBox("\u542f\u7528 BGM")
        self._wiz_enable_bgm.setChecked(False)
        bgm_layout.addWidget(self._wiz_enable_bgm)
        self._wiz_bgm_combo = QComboBox()
        self._wiz_bgm_combo.addItem("\ud83c\udfb2 \u968f\u673a\u9009\u62e9", None)
        for b in get_bgm_files():
            self._wiz_bgm_combo.addItem(Path(b).name, str(b))
        self._wiz_bgm_combo.setEnabled(False)
        self._wiz_bgm_combo.setMinimumHeight(30)
        bgm_layout.addWidget(self._wiz_bgm_combo, 1)
        vol_lbl = QLabel("\u97f3\u91cf:")
        vol_lbl.setStyleSheet("color:#374151; font-size:12px; background:transparent;")
        bgm_layout.addWidget(vol_lbl)
        self._wiz_bgm_volume = QSlider(Qt.Horizontal)
        self._wiz_bgm_volume.setRange(1, 30)
        self._wiz_bgm_volume.setValue(12)
        self._wiz_bgm_volume.setEnabled(False)
        self._wiz_bgm_volume.setMinimumWidth(80)
        self._wiz_bgm_volume.setMaximumWidth(140)
        self._wiz_bgm_vol_label = QLabel("12%")
        self._wiz_bgm_vol_label.setMinimumWidth(36)
        self._wiz_bgm_volume.valueChanged.connect(
            lambda v: self._wiz_bgm_vol_label.setText(f"{v}%")
        )
        bgm_layout.addWidget(self._wiz_bgm_volume)
        bgm_layout.addWidget(self._wiz_bgm_vol_label)
        self._wiz_bgm_preview = QPushButton("\u25b6 \u8bd5\u542c")
        self._wiz_bgm_preview.setFixedHeight(30)
        self._wiz_bgm_preview.setEnabled(False)
        self._wiz_bgm_preview.clicked.connect(self._on_bgm_preview)
        bgm_layout.addWidget(self._wiz_bgm_preview)
        self._wiz_enable_bgm.toggled.connect(lambda c: (
            self._wiz_bgm_combo.setEnabled(c),
            self._wiz_bgm_volume.setEnabled(c),
            self._wiz_bgm_preview.setEnabled(c),
        ))
        self._wiz_bgm_playing = False
        layout.addWidget(bgm_group)

        # \u2550\u2550\u2550 \u9ad8\u7ea7\u9009\u9879\uff08\u5b57\u5e55\u8bbe\u7f6e\uff09\uff1a\u63a7\u5236\u9009\u9879\u5728\u5de6\uff0c\u624b\u673a\u9884\u89c8\u5728\u53f3 \u2550\u2550\u2550
        sub_group = QGroupBox("\u5b57\u5e55\u4f4d\u7f6e")
        sub_main_layout = QHBoxLayout(sub_group)  # \u6539\u4e3a\u6c34\u5e73\u5e03\u5c40
        sub_main_layout.setContentsMargins(12, 10, 12, 10)
        sub_main_layout.setSpacing(16)
        # \u5de6\u4fa7\uff1a\u63a7\u5236\u9009\u9879\uff08\u5782\u76f4\u6392\u5217\uff09
        ctrl_layout = QVBoxLayout()
        ctrl_layout.setSpacing(6)
        # \u9884\u8bbe\u9009\u9879\u884c
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        self._wiz_sub_preset_group = QButtonGroup(self)
        for pct, text in [(10, "\u7d27\u51d1"), (13, "\u6807\u51c6"), (16, "\u5bbd\u677e"), (-1, "\u81ea\u5b9a\u4e49")]:
            rb = QRadioButton(text)
            rb.setStyleSheet("color:#374151; font-size:12px;")
            self._wiz_sub_preset_group.addButton(rb, pct)
            preset_row.addWidget(rb)
        for btn in self._wiz_sub_preset_group.buttons():
            if self._wiz_sub_preset_group.id(btn) == 13:
                btn.setChecked(True)
                break
        preset_row.addStretch()
        ctrl_layout.addLayout(preset_row)
        # \u6ed1\u5757\u884c
        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        self._wiz_sub_pct_label = QLabel("13% (250px)")
        self._wiz_sub_pct_label.setStyleSheet(
            "color:#111827; font-size:12px; font-weight:700;"
            " font-family:Menlo,Consolas,monospace;"
        )
        self._wiz_sub_pct_label.setMinimumWidth(86)
        slider_row.addWidget(self._wiz_sub_pct_label)
        self._wiz_sub_slider = QSlider(Qt.Horizontal)
        self._wiz_sub_slider.setRange(8, 35)
        self._wiz_sub_slider.setValue(13)
        self._wiz_sub_slider.setTickPosition(QSlider.TicksBelow)
        self._wiz_sub_slider.setTickInterval(5)
        self._wiz_sub_slider.setMinimumHeight(28)
        slider_row.addWidget(self._wiz_sub_slider, 1)
        # test_basic_ui expects _wiz_sub_pos alias (same widget)
        self._wiz_sub_pos = self._wiz_sub_slider
        self._wiz_sub_safety_label = QLabel("\u2705 \u5b89\u5168")
        self._wiz_sub_safety_label.setStyleSheet(
            "color:#059669; font-size:11px; font-weight:600;"
        )
        slider_row.addWidget(self._wiz_sub_safety_label)
        # v2.3 A2: 字幕字号 spinbox (从 platform preset 默认填, 用户可改)
        size_lbl = QLabel("\u5b57\u53f7:")
        size_lbl.setStyleSheet("color:#374151; font-size:12px;")
        slider_row.addWidget(size_lbl)
        self._wiz_subtitle_size = QSpinBox()
        # v2.4: 范围放宽到 (40, 96), 默认 68pt (手机端"中等"档位); 平台预设可覆盖
        self._wiz_subtitle_size.setRange(40, 96)
        self._wiz_subtitle_size.setValue(68)
        self._wiz_subtitle_size.setSuffix(" pt")
        self._wiz_subtitle_size.setMinimumWidth(70)
        self._wiz_subtitle_size.setMinimumHeight(28)
        self._wiz_subtitle_size.valueChanged.connect(
            lambda v: self._mark_wiz_overridden("subtitle_size")
        )
        slider_row.addWidget(self._wiz_subtitle_size)
        # v2.3 E1: 字幕字体下拉 (ASS font_name, 矩阵号 OCR 文本差异化)
        font_lbl = QLabel("字体:")
        font_lbl.setStyleSheet("color:#374151; font-size:12px;")
        slider_row.addWidget(font_lbl)
        self._wiz_subtitle_font = QComboBox()
        self._wiz_subtitle_font.setMinimumHeight(28)
        self._wiz_subtitle_font.setMinimumWidth(120)
        # 跨平台字体, 缺字 fallback 由 ASS 自身处理
        self._wiz_subtitle_font.addItems([
            "Source Han Sans",  # macOS 自带 (思源黑体)
            "PingFang SC",      # macOS 默认
            "Hiragino Sans",    # macOS 旧版
            "Noto Sans CJK SC", # 跨平台
            "Microsoft YaHei",  # Windows
            "SimHei",           # Windows 兼容
            "Arial",            # 英文
            "Helvetica",
            "Thonburi",         # 泰文
            "Noto Sans Thai",
        ])
        self._wiz_subtitle_font.setCurrentText("Source Han Sans")
        self._wiz_subtitle_font.currentTextChanged.connect(
            lambda v: self._mark_wiz_overridden("subtitle_font")
        )
        slider_row.addWidget(self._wiz_subtitle_font)
        ctrl_layout.addLayout(slider_row)
        # \u81ea\u52a8\u53bb\u91cd\u9009\u9879
        self._wiz_dedup = QCheckBox("\u81ea\u52a8\u53bb\u91cd\uff08\u5220\u9664\u9ad8\u5ea6\u76f8\u4f3c\u7684\u6210\u54c1\uff09")
        self._wiz_dedup.setStyleSheet("color:#374151; font-size:12px;")
        ctrl_layout.addWidget(self._wiz_dedup)
        sub_main_layout.addLayout(ctrl_layout, 1)  # \u5de6\u4fa7\u63a7\u5236\u533a\u57df\u5360\u636e\u5269\u4f59\u7a7a\u95f4
        # \u53f3\u4fa7\uff1a\u624b\u673a\u9884\u89c8\uff089:16\u7ad6\u5c4f\u6bd4\u4f8b\uff09
        self._sub_preview_widget = QWidget()
        self._sub_preview_widget.setFixedWidth(100)
        self._sub_preview_widget.setFixedHeight(int(100 * 16 / 9))  # 9:16\u6bd4\u4f8b
        self._sub_preview_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._sub_preview_widget.setStyleSheet("background:#1F2937; border-radius:10px;")
        self._sub_preview_layout = QVBoxLayout(self._sub_preview_widget)
        self._sub_preview_layout.setContentsMargins(4, 4, 4, 4)
        self._sub_preview_layout.setSpacing(0)
        # \u89c6\u9891\u533a\u57df\uff08\u6df1\u7070\u80cc\u666f\uff09
        video_area = QWidget()
        video_area.setStyleSheet("background:#374151; border-radius:3px;")
        video_area_layout = QVBoxLayout(video_area)
        video_area_layout.setContentsMargins(0, 0, 0, 0)
        video_area_layout.setSpacing(0)
        # \u5b57\u5e55\u6761\uff08\u76f4\u63a5\u653e\u5728 video_area \u4e0a\uff0c\u4f7f\u7528\u7edd\u5bf9\u5b9a\u4f4d\uff09
        self._sub_preview_bar = QLabel("\u5b57\u5e55", video_area)
        self._sub_preview_bar.setAlignment(Qt.AlignCenter)
        self._sub_preview_bar.setStyleSheet(
            "background:rgba(255,255,255,0.9); border-radius:2px; "
            "color:#111827; font-size:6px; font-weight:600;"
        )
        self._sub_preview_bar.setFixedHeight(10)
        # \u521d\u59cb\u4f4d\u7f6e\u4f1a\u5728 _update_sub_preview \u4e2d\u8bbe\u7f6e
        self._sub_preview_layout.addWidget(video_area, 1)
        sub_main_layout.addWidget(self._sub_preview_widget)

        def _on_wiz_sub_slider_change(v: int) -> None:
            self._update_sub_preview(v)
            self._save_subtitle_pos(v)

        self._wiz_sub_slider.valueChanged.connect(_on_wiz_sub_slider_change)
        self._wiz_sub_preset_group.buttonClicked.connect(
            lambda btn: self._wiz_sub_slider.setValue(self._wiz_sub_preset_group.id(btn))
            if self._wiz_sub_preset_group.id(btn) > 0
            else None
        )
        saved = self._load_subtitle_pos()
        if saved is not None:
            try:
                v_int = int(float(saved))
                if 8 <= v_int <= 35:
                    self._wiz_sub_slider.setValue(v_int)
            except Exception:
                pass
        self._update_sub_preview(self._wiz_sub_slider.value())
        layout.addWidget(sub_group)

        # \u5e95\u90e8\u5bfc\u822a
        nav = QHBoxLayout()
        back_btn = QPushButton("\u2190 \u4e0a\u4e00\u6b65")
        back_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #6B7280; border: 1.5px solid #E5E7EB;
                border-radius: 10px; padding: 8px 22px; font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background: #FCFCFD; border-color: #D1D5DB; }
        """)
        back_btn.clicked.connect(lambda: self._go_to_step(2))
        nav.addWidget(back_btn)
        nav.addStretch()
        self._wiz_step3_start = QPushButton("\ud83d\ude80 \u5f00\u59cb\u751f\u6210")
        self._wiz_step3_start.setMinimumHeight(44)
        self._wiz_step3_start.setStyleSheet("""
            QPushButton {
                background: #10B981; color: #ffffff; font-size: 16px; font-weight: 700;
                border: none; border-radius: 10px; padding: 10px 32px;
            }
            QPushButton:hover { background: #059669; }
            QPushButton:pressed { background: #047857; }
        """)
        self._wiz_step3_start.clicked.connect(self._on_wizard_start_generate)
        nav.addWidget(self._wiz_step3_start)
        layout.addLayout(nav)
        return page

    def _on_wiz_step3_tname_changed(self, text: str) -> None:
        """\u4efb\u52a1\u540d\u5b9e\u65f6\u53d8\u5316 -> \u66f4\u65b0\u8f93\u51fa\u76ee\u5f55\u63d0\u793a\uff0c\u5e76\u540c\u6b65\u56de Step 1\u3002"""
        t = (text or "").strip()
        if t:
            self._wiz_step3_tname_status.setText(f"\u2705 {t}")
            self._wiz_step3_tname_status.setStyleSheet(
                "font-size:11px; color:#059669; font-weight:700; padding:2px 8px;"
                "background:#ECFDF5; border-radius:8px;"
            )
            self._wiz_step3_output_hint.setText(f"\u2192 output/\u4efb\u52a1ID_{t}_\u65f6\u95f4\u6233/")
        else:
            self._wiz_step3_tname_status.setText("\u26a0\ufe0f \u4efb\u52a1\u540d\u4e3a\u7a7a")
            self._wiz_step3_tname_status.setStyleSheet(
                "font-size:11px; color:#DC2626; font-weight:700; padding:2px 8px;"
                "background:#FEF2F2; border-radius:8px;"
            )
            self._wiz_step3_output_hint.setText("\u2192 output/\u4efb\u52a1ID_\u672a\u547d\u540d_\u65f6\u95f4\u6233/")
        # \u53cc\u5411\u540c\u6b65\uff1aStep 3 \u6539\u540d -> \u540c\u6b65\u5230 Step 1
        if hasattr(self, "_wiz_task_name"):
            if self._wiz_task_name.text() != text:
                self._wiz_task_name.blockSignals(True)
                self._wiz_task_name.setText(text)
                self._wiz_task_name.blockSignals(False)

    def _check_step1_ready(self) -> None:
        """v2.3: Step 1 下一步按钮 enable 条件 = 平台已选 + 任务名非空"""
        if not hasattr(self, "_wiz_step1_next"):
            return
        has_platform = self._wiz_platform_group.checkedId() >= 0
        has_name = bool(self._wiz_task_name.text().strip())
        self._wiz_step1_next.setEnabled(has_platform and has_name)

    def _on_wiz_platform_changed(self, _btn) -> None:
        """v2.3: 平台切换 -> 把 preset 填到所有相关 UI 控件 (跳过用户已手动改过的字段)"""
        pid_idx = self._wiz_platform_group.checkedId()
        if pid_idx < 0 or pid_idx >= len(PLATFORM_IDS):
            return
        platform_id = PLATFORM_IDS[pid_idx]
        cfg = apply_preset_to_config({}, platform_id, self._wiz_user_overridden)
        if "subtitle_size" in cfg and hasattr(self, "_wiz_subtitle_size"):
            self._wiz_subtitle_size.setValue(int(cfg["subtitle_size"]))
        if "subtitle_font" in cfg and hasattr(self, "_wiz_subtitle_font"):
            idx = self._wiz_subtitle_font.findText(cfg["subtitle_font"])
            if idx >= 0:
                self._wiz_subtitle_font.setCurrentIndex(idx)
        if "tts_voice" in cfg and hasattr(self, "_wiz_voice") and not getattr(self, "_wiz_voice_user_set", False):
            v = cfg["tts_voice"]
            idx = self._wiz_voice.findText(v)
            if idx >= 0:
                self._wiz_voice.setCurrentIndex(idx)
        if hasattr(self, "_wiz_current_platform_label"):
            self._wiz_current_platform_label.setText("\u5f53\u524d: " + PLATFORM_DISPLAY[platform_id])
        if hasattr(self, "_wiz_step2_platform_label"):
            self._wiz_step2_platform_label.setText("\u5f53\u524d: " + PLATFORM_DISPLAY[platform_id])
        self._check_step1_ready()

    
    def _update_wiz_max_videos_label(self) -> None:
        """实时算 "最多混剪 N 条成片" (算法: 切片池大小 × max_uses)

        v2.3 简化: 不再按风险等级染色, 直接显示理论上限。
        1 条成片只占 1 个 "复用配额", 所以 max_videos = 切片数 × max_uses。
        """
        try:
            max_uses = self._wiz_max_uses.value() if hasattr(self, "_wiz_max_uses") else 5
            if hasattr(self, "_wiz_selected_materials") and self._wiz_selected_materials:
                slice_count = len(self._wiz_selected_materials) * 3
            else:
                slice_count = 20
            max_safe = slice_count * max_uses
            if hasattr(self, "_wiz_max_videos_label"):
                self._wiz_max_videos_label.setText(f"· 最多混剪 {max_safe} 条成片")
        except Exception:
            if hasattr(self, "_wiz_max_videos_label"):
                self._wiz_max_videos_label.setText("")

    def _mark_wiz_overridden(self, key: str) -> None:
        """把字段标记为用户手动改过, 平台切换时不再覆盖"""
        if not hasattr(self, "_wiz_user_overridden"):
            self._wiz_user_overridden = set()
        self._wiz_user_overridden.add(key)

    def _build_wiz_extra_config(self) -> dict:
        """v2.3: 从新 UI 控件收集 extra_config dict, 透传给 run_generate"""
        cfg = {}
        pid_idx = self._wiz_platform_group.checkedId() if hasattr(self, "_wiz_platform_group") else -1
        if pid_idx >= 0 and pid_idx < len(PLATFORM_IDS):
            cfg["platform"] = PLATFORM_IDS[pid_idx]
        if hasattr(self, "_wiz_max_uses"):
            cfg["max_uses_per_slice"] = int(self._wiz_max_uses.value())
        if hasattr(self, "_wiz_video_type_step2"):
            cfg["video_type"] = self._wiz_video_type_step2.currentData() or "auto"
        # 写明选定的 AI Provider（local / deepseek）和语义版本，方便后续审计。
        try:
            from autokat.core.ai_providers import load_ai_settings
            cfg["writer_provider"] = load_ai_settings().get("provider", "local")
        except Exception:
            cfg["writer_provider"] = "local"
        try:
            from autokat.core.editor import INTENT_VERSION
            cfg["intent_version"] = INTENT_VERSION
        except Exception:
            cfg["intent_version"] = "intent-v1"
        if hasattr(self, "_wiz_enable_perturb"):
            cfg["enable_diversity"] = bool(self._wiz_enable_perturb.isChecked())
        if hasattr(self, "_wiz_pert_level_group"):
            level_idx = self._wiz_pert_level_group.checkedId()
            if level_idx is not None and level_idx >= 0 and level_idx < len(PERT_LEVELS):
                cfg["perturbation_level"] = PERT_LEVELS[level_idx]

        if hasattr(self, "_wiz_dedup") and self._wiz_dedup.isChecked():
            cfg["dedup_threshold"] = 0.78
        # v3 wizard 快照：全量 UI 状态 JSON 字符串，db_create_task 写入 tasks.wizard_snapshot。
        # 之后 "查看配置 / 基于此新建" 两种模式都靠这个字段还原。
        import json as _json
        try:
            cfg["wizard_snapshot"] = _json.dumps(self._capture_wizard_snapshot(), ensure_ascii=False)
        except Exception:
            cfg["wizard_snapshot"] = None
        # v2.3 E1: 字幕字体/字号/动效 (差异化的字幕层)
        if hasattr(self, "_wiz_subtitle_font"):
            cfg["subtitle_font"] = self._wiz_subtitle_font.currentText()
        if hasattr(self, "_wiz_subtitle_size"):
            cfg["font_size"] = int(self._wiz_subtitle_size.value())
        # 动效默认从 6 种随机选
        import random as _r
        cfg["subtitle_animation"] = _r.choice(["none", "fade", "pop", "slide", "typewriter", "karaoke"])
        return cfg

    def _on_wiz_step1_tname_changed(self, text: str) -> None:
        """Step 1 \u4efb\u52a1\u540d\u53d8\u5316 -> \u540c\u6b65\u5230 Step 3\u3002"""
        if hasattr(self, "_wiz_step3_tname_edit"):
            if self._wiz_step3_tname_edit.text() != text:
                # setText \u4f1a\u89e6\u53d1 _on_wiz_step3_tname_changed\uff0c\u91cc\u9762\u4f1a\u518d\u56de\u5199 _wiz_task_name\uff0c
                # \u7528 blockSignals \u963b\u65ad\u907f\u514d\u65e0\u9650\u5faa\u73af
                self._wiz_step3_tname_edit.blockSignals(True)
                self._wiz_step3_tname_edit.setText(text)
                self._wiz_step3_tname_edit.blockSignals(False)
                # \u624b\u52a8\u89e6\u53d1\u4e00\u6b21 label \u66f4\u65b0\uff08\u56e0\u4e3a\u88ab blockSignals \u963b\u65ad\u4e86 textChanged\uff09
                self._on_wiz_step3_tname_changed(text)
    def _build_wizard_step4(self) -> QWidget:
        # v2.3: Step 4 高度压缩, 给 Step 3 基础配置腾位置 (margins 缩到 12/8, 标题 18, 按钮 30, 进度 10)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(self._build_step_bar(4))
        # 标题 + 任务信息并排
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title = QLabel("🚀 正在生成视频")
        title.setStyleSheet("font-size:18px; font-weight:800; color:#111827;")
        header_row.addWidget(title)
        header_row.addStretch()
        self._wiz_task_info = QLabel("准备中...")
        self._wiz_task_info.setStyleSheet("font-size:12px; color:#2563EB; font-weight:600; background:transparent;")
        header_row.addWidget(self._wiz_task_info)
        close_btn = QPushButton("关闭向导")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #FEF2F2; color: #EF4444; border: 1.5px solid #FECACA;
                border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 600;
            }
            QPushButton:hover { background: #EF4444; color: #FFFFFF; }
        """)
        close_btn.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        header_row.addWidget(close_btn)
        layout.addLayout(header_row)
        # 进度条 — 紧凑版
        progress_group = QFrame()
        progress_group.setStyleSheet("""
            QFrame {
                background: #FFFFFF; border: 1.5px solid #E5E7EB;
                border-radius: 10px;
            }
        """)
        prog_layout = QVBoxLayout(progress_group)
        prog_layout.setContentsMargins(16, 12, 16, 12)
        prog_layout.setSpacing(8)
        self._wiz_progress = QProgressBar()
        self._wiz_progress.setMinimumHeight(10)
        self._wiz_progress.setRange(0, 100)
        self._wiz_progress.setValue(0)
        self._wiz_progress.setFormat("%v/%m")
        self._wiz_progress.setStyleSheet("""
            QProgressBar {
                border: none; border-radius: 8px;
                background: #E5E7EB; text-align: center;
                font-size: 12px; font-weight: 700; color: #ffffff;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #2563EB, stop:1 #10B981);
                border-radius: 8px;
            }
        """)
        prog_layout.addWidget(self._wiz_progress)
        # 当前活动（从 DB 读 progress_detail，2s 刷新一次）
        self._wiz_current_activity = QLabel("⏳ 准备中...")
        self._wiz_current_activity.setStyleSheet(
            "font-size:12px; color:#10B981; font-weight:600;"
            "background:transparent;"
        )
        self._wiz_current_activity.setWordWrap(True)
        prog_layout.addWidget(self._wiz_current_activity)
        # 预计剩余时间 + 进度统计
        info_row = QWidget()
        info_row.setStyleSheet("background:transparent;")
        info_row.setFixedHeight(22)
        info_row_layout = QHBoxLayout(info_row)
        info_row_layout.setContentsMargins(0, 0, 0, 0)
        info_row_layout.setSpacing(8)
        self._wiz_eta_label = QLabel("")
        self._wiz_eta_label.setStyleSheet(
            "font-size:11px; color:#6B7280; font-weight:500; background:transparent;")
        info_row_layout.addWidget(self._wiz_eta_label)
        info_row_layout.addStretch()
        prog_layout.addWidget(info_row)
        # 控制按钮
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        self._wiz_pause_btn = QPushButton("⏸ 暂停")
        self._wiz_pause_btn.setMinimumHeight(30)
        self._wiz_pause_btn.setStyleSheet("""
            QPushButton {
                background: #F59E0B; color: #ffffff; border: none; font-weight: 600;
                border-radius: 10px; padding: 5px 16px; font-size: 13px;
            }
            QPushButton:hover { background: #D97706; }
            QPushButton:pressed { background: #92400E; }
        """)
        self._wiz_pause_btn.clicked.connect(self._on_wizard_pause)
        ctrl_row.addWidget(self._wiz_pause_btn)
        self._wiz_stop_btn = QPushButton("⏹ 停止")
        self._wiz_stop_btn.setMinimumHeight(30)
        self._wiz_stop_btn.setStyleSheet("""
            QPushButton {
                background: #EF4444; color: #ffffff; border: none; font-weight: 600;
                border-radius: 10px; padding: 5px 16px; font-size: 13px;
            }
            QPushButton:hover { background: #DC2626; }
            QPushButton:pressed { background: #B91C1C; }
        """)
        self._wiz_stop_btn.clicked.connect(self._on_wizard_stop)
        ctrl_row.addWidget(self._wiz_stop_btn)
        self._wiz_back_to_dashboard = QPushButton("← 返回工作台")
        self._wiz_back_to_dashboard.setMinimumHeight(30)
        self._wiz_back_to_dashboard.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #6B7280; border: 1.5px solid #E5E7EB; font-weight: 600;
                border-radius: 10px; padding: 5px 16px; font-size: 13px;
            }
            QPushButton:hover { background: #FCFCFD; border-color: #D1D5DB; }
        """)
        self._wiz_back_to_dashboard.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        ctrl_row.addWidget(self._wiz_back_to_dashboard)
        # 任务完成时显示的「打开输出目录」按钮（任务没完时隐藏）
        self._wiz_output_dir_link = QPushButton("📁 打开输出目录")
        self._wiz_output_dir_link.setMinimumHeight(36)
        self._wiz_output_dir_link.setToolTip("点击用 Finder/Explorer 打开成片所在目录")
        self._wiz_output_dir_link.setStyleSheet("""
            QPushButton {
                background: #2563EB; color: #ffffff; border: none; font-weight: 600;
                border-radius: 10px; padding: 5px 16px; font-size: 13px;
            }
            QPushButton:hover { background: #1D4ED8; }
            QPushButton:pressed { background: #1E40AF; }
        """)
        self._wiz_output_dir_link.setVisible(False)
        self._wiz_output_dir_link.clicked.connect(self._open_wizard_output_dir)
        ctrl_row.addWidget(self._wiz_output_dir_link)
        ctrl_row.addStretch()
        prog_layout.addLayout(ctrl_row)
        layout.addWidget(progress_group, 0)
        # 下半区：左 = 实时日志(60%)  右 = 已完成视频列表(40%)
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)
        # 左：实时日志
        log_col = QWidget()
        log_col_layout = QVBoxLayout(log_col)
        log_col_layout.setContentsMargins(0, 0, 0, 0)
        log_col_layout.setSpacing(6)
        log_label = QLabel("📋 实时日志")
        log_label.setStyleSheet("font-size:15px; font-weight:700; margin-top:4px; color:#111827; background:transparent;")
        log_col_layout.addWidget(log_label)
        self._wiz_log = QTextEdit()
        self._wiz_log.setReadOnly(True)
        # 禁用自动换行：输出目录等长路径会被 QTextEdit 拆成多行，
        # 后续"第 N 条"日志接在同一逻辑行时 _replace_log_line 会找错行号，
        # 视觉上也像"两条消息粘连"难读。改为 NoWrap + 横向滚动条。
        self._wiz_log.setLineWrapMode(QTextEdit.NoWrap)
        self._wiz_log.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._wiz_log.setStyleSheet("""
            QTextEdit {
                background: #FCFCFD; color: #10B981;
                border: 1.5px solid #E5E7EB; border-radius: 12px;
                padding: 14px; font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px;
                selection-background-color: #2563EB40;
            }
        """)
        log_col_layout.addWidget(self._wiz_log, 1)
        bottom_row.addWidget(log_col, 6)
        # 右：已完成视频列表（每渲染完一条就追加，显示名称 + 时长）
        clips_col = QWidget()
        clips_col_layout = QVBoxLayout(clips_col)
        clips_col_layout.setContentsMargins(0, 0, 0, 0)
        clips_col_layout.setSpacing(6)
        clips_label = QLabel("🎬 已完成视频")
        clips_label.setStyleSheet("font-size:15px; font-weight:700; margin-top:4px; color:#111827; background:transparent;")
        clips_col_layout.addWidget(clips_label)
        from PySide6.QtWidgets import QListWidget
        self._wiz_done_list = QListWidget()
        self._wiz_done_list.setStyleSheet("""
            QListWidget {
                background: #FCFCFD; border: 1.5px solid #E5E7EB; border-radius: 12px;
                padding: 4px; font-size: 13px; color: #111827;
            }
            QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #F3F4F6; border-radius: 6px; margin: 1px; }
            QListWidget::item:hover { background: #F9FAFB; }
        """)
        self._wiz_done_list.setMinimumWidth(260)
        clips_col_layout.addWidget(self._wiz_done_list, 1)
        bottom_row.addWidget(clips_col, 4)
        layout.addLayout(bottom_row, 1)
        return page
    # ══════════════════════════════════════════════════════════
    # 步骤条组件
    # ══════════════════════════════════════════════════════════
    # -- 步骤快照 capture / apply / readonly -----------------------------
    # 这三个方法配合 wizard_snapshot.WIZARD_FIELD_LABELS 字典使用，让任何已完成
    # 任务都能以 view（只读）或 fork（可编辑）两种模式重新打开向导。

    def _wizard_interactive_widgets(self) -> list:
        """Return all input widgets across the 4 wizard step pages.

        Skips the step bar (tagged with objectName "step_bar") and the
        Close wizard button (it must stay clickable in any mode). Callers
        iterate the result to setEnabled() in bulk.
        """
        from PySide6.QtWidgets import (
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox,
            QDoubleSpinBox, QCheckBox, QRadioButton, QSlider, QPushButton,
        )
        # QPushButton is included separately so we can filter the close button.
        # findChildren doesn't accept a tuple of types, so we query each.
        widget_type_lists = (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox,
                             QSpinBox, QDoubleSpinBox, QCheckBox, QRadioButton,
                             QSlider)
        result = []
        for step_idx in range(1, 5):
            page = getattr(self, f"_wizard_step{step_idx}", None)
            if page is None:
                continue
            for wt in widget_type_lists:
                for w in page.findChildren(wt):
                    # Skip the step bar (objectName set in _build_step_bar)
                    parent = w.parent()
                    in_step_bar = False
                    while parent is not None and parent is not page:
                        if parent.objectName() == "step_bar":
                            in_step_bar = True
                            break
                        parent = parent.parent()
                    if not in_step_bar:
                        result.append(w)
        return result

    def _set_wizard_readonly(self, readonly: bool) -> None:
        """Enable or disable every wizard input widget. Used to enter view mode.

        In view mode the step bar stays clickable (handled separately, not in
        this method), the "Close wizard" button stays clickable, and the
        "知道了" exit button (added in view mode by the caller) shows.
        """
        for w in self._wizard_interactive_widgets():
            try:
                w.setEnabled(not readonly)
            except Exception:
                pass

    def _capture_wizard_snapshot(self) -> dict:
        """Dump every wizard widget's value into a JSON-serializable dict.

        Safe to call at any time (even mid-wizard). Fields the wizard hasn't
        touched yet come out as None; that's fine, the schema is permissive.
        """
        snap = empty_snapshot()
        fields = snap["fields"]
        # Step 1
        if hasattr(self, "_wiz_selected_materials"):
            fields["selected_material_ids"] = sorted(
                int(m) for m in self._wiz_selected_materials
            )
        # Step 2
        if hasattr(self, "_wiz_script_editor"):
            fields["script_text"] = self._wiz_script_editor.toPlainText()
        if hasattr(self, "_wiz_script_name"):
            fields["script_name"] = self._wiz_script_name.text()
        if hasattr(self, "_wiz_voice"):
            fields["voice"] = self._wiz_voice.currentText()
        if hasattr(self, "_wiz_rate"):
            fields["rate"] = int(self._wiz_rate.value())
        if hasattr(self, "_wiz_pitch"):
            fields["pitch"] = int(self._wiz_pitch.value())
        if hasattr(self, "_wiz_video_type_step2"):
            fields["writer_provider"] = (
                self._wiz_video_type_step2.currentData() or "auto"
            )
        # Step 3
        if hasattr(self, "_wiz_step3_tname_edit"):
            fields["task_name"] = self._wiz_step3_tname_edit.text()
        if hasattr(self, "_wiz_count"):
            fields["count"] = int(self._wiz_count.value())
        if hasattr(self, "_wiz_workers"):
            fields["workers"] = int(self._wiz_workers.value())
        if hasattr(self, "_wiz_fps"):
            fields["fps"] = int(self._wiz_fps.currentText())
        if hasattr(self, "_wiz_enable_bgm"):
            fields["enable_bgm"] = bool(self._wiz_enable_bgm.isChecked())
        if hasattr(self, "_wiz_bgm_volume"):
            fields["bgm_volume"] = int(self._wiz_bgm_volume.value())
        if hasattr(self, "_wiz_max_uses"):
            fields["max_uses_per_slice"] = int(self._wiz_max_uses.value())
        if hasattr(self, "_wiz_enable_perturb"):
            fields["enable_diversity"] = bool(self._wiz_enable_perturb.isChecked())
        if hasattr(self, "_wiz_pert_level_group"):
            fields["perturbation_level"] = self._wiz_pert_level_group.checkedId()
        if hasattr(self, "_wiz_dedup") and self._wiz_dedup.isChecked():
            fields["dedup_threshold"] = 0.78
        if hasattr(self, "_wiz_subtitle_font"):
            fields["subtitle_font"] = self._wiz_subtitle_font.currentText()
        if hasattr(self, "_wiz_subtitle_size"):
            fields["font_size"] = int(self._wiz_subtitle_size.value())
        if hasattr(self, "_wiz_platform_group"):
            fields["platform"] = self._wiz_platform_group.checkedId()
        if hasattr(self, "_wiz_video_type_step2"):
            fields["video_type"] = self._wiz_video_type_step2.currentData() or "auto"
        # writer_provider is sourced from AI settings (not a wizard widget);
        # capture it so a fork can reproduce the exact provider choice.
        try:
            from autokat.core.ai_providers import load_ai_settings
            fields["writer_provider"] = load_ai_settings().get("provider", "local")
        except Exception:
            fields["writer_provider"] = "local"
        return snap

    def _apply_wizard_snapshot(self, snapshot: dict) -> None:
        """Restore wizard widget values from a previously captured snapshot.

        Unknown / missing fields are silently ignored (the schema is
        forward-compatible: older snapshots opened in newer wizards just
        skip fields that no longer exist).
        """
        if not snapshot or not isinstance(snapshot, dict):
            return
        fields = snapshot.get("fields") or {}
        # Step 1
        mat_ids = fields.get("selected_material_ids")
        if mat_ids is not None and hasattr(self, "_wiz_selected_materials"):
            self._wiz_selected_materials = set(int(m) for m in mat_ids)
        # Step 2
        if "script_text" in fields and hasattr(self, "_wiz_script_editor"):
            self._wiz_script_editor.setPlainText(fields["script_text"] or "")
        if "script_name" in fields and hasattr(self, "_wiz_script_name"):
            self._wiz_script_name.setText(fields["script_name"] or "")
        if "voice" in fields and hasattr(self, "_wiz_voice"):
            v = fields["voice"]
            idx = self._wiz_voice.findText(v) if v else -1
            if idx >= 0:
                self._wiz_voice.setCurrentIndex(idx)
        if "rate" in fields and hasattr(self, "_wiz_rate"):
            self._wiz_rate.setValue(int(fields["rate"] or 0))
        if "pitch" in fields and hasattr(self, "_wiz_pitch"):
            self._wiz_pitch.setValue(int(fields["pitch"] or 0))
        # video_type is sync'd between step 2 and step 3; we update the
        # step 3 one (which is the "authoritative" copy in fork mode)
        if "video_type" in fields and hasattr(self, "_wiz_video_type_step2"):
            v = fields["video_type"] or "auto"
            idx = self._wiz_video_type_step2.findData(v)
            if idx >= 0:
                self._wiz_video_type_step2.setCurrentIndex(idx)
        # Step 3
        if "task_name" in fields and hasattr(self, "_wiz_step3_tname_edit"):
            self._wiz_step3_tname_edit.setText(fields["task_name"] or "")
        if "count" in fields and hasattr(self, "_wiz_count"):
            self._wiz_count.setValue(int(fields["count"] or 100))
        if "workers" in fields and hasattr(self, "_wiz_workers"):
            self._wiz_workers.setValue(int(fields["workers"] or 0))
        if "fps" in fields and hasattr(self, "_wiz_fps"):
            v = str(fields["fps"] or 30)
            idx = self._wiz_fps.findText(v)
            if idx >= 0:
                self._wiz_fps.setCurrentIndex(idx)
        if "enable_bgm" in fields and hasattr(self, "_wiz_enable_bgm"):
            self._wiz_enable_bgm.setChecked(bool(fields["enable_bgm"]))
        if "bgm_volume" in fields and hasattr(self, "_wiz_bgm_volume"):
            self._wiz_bgm_volume.setValue(int(fields["bgm_volume"] or 12))
        if "max_uses_per_slice" in fields and hasattr(self, "_wiz_max_uses"):
            self._wiz_max_uses.setValue(int(fields["max_uses_per_slice"] or 5))
        if "enable_diversity" in fields and hasattr(self, "_wiz_enable_perturb"):
            self._wiz_enable_perturb.setChecked(bool(fields["enable_diversity"]))
        if "perturbation_level" in fields and hasattr(self, "_wiz_pert_level_group"):
            btn = self._wiz_pert_level_group.button(int(fields["perturbation_level"] or 0))
            if btn is not None:
                btn.setChecked(True)
        if "dedup_threshold" in fields and hasattr(self, "_wiz_dedup"):
            self._wiz_dedup.setChecked(fields["dedup_threshold"] is not None)
        if "subtitle_font" in fields and hasattr(self, "_wiz_subtitle_font"):
            v = fields["subtitle_font"]
            idx = self._wiz_subtitle_font.findText(v) if v else -1
            if idx >= 0:
                self._wiz_subtitle_font.setCurrentIndex(idx)
        if "font_size" in fields and hasattr(self, "_wiz_subtitle_size"):
            self._wiz_subtitle_size.setValue(int(fields["font_size"] or 24))
        if "platform" in fields and hasattr(self, "_wiz_platform_group"):
            btn = self._wiz_platform_group.button(int(fields["platform"] or 0))
            if btn is not None:
                btn.setChecked(True)

    def _wizard_set_field_for_test(self, field: str, value):
        """测试用: 设置某个 wizard 字段值, 模拟 capture 之后 apply 验证。

        不在生产代码中使用, 也不在 UI 暴露入口。"""
        snap = self._capture_wizard_snapshot()
        snap["fields"][field] = value
        self._apply_wizard_snapshot(snap)

    def _enter_wizard_for(self, task_id: int, mode: str = "view") -> None:
        """打开向导查看 / 基于此新建 已有任务。

        mode:
            ``view``  - 只读。控件全部 disabled，步骤条可点击来回翻，
                        顶部 banner 显示 "只读模式"，底部 "知道了" 退出。
            ``fork``  - 可编辑。控件预填任务快照，用户改完可以走"开始生成"
                        走正常新建流程。

        旧任务（wizard_snapshot 为 NULL）走 graceful degradation：banner 提示
        "配置不完整"，view 模式只显示 tasks.config 里那 10 个字段。
        """
        from autokat.models.db import get_task, get_script_by_id
        task = get_task(task_id)
        if not task:
            QMessageBox.warning(self, "提示", f"任务 #{task_id} 不存在")
            return
        script = get_script_by_id(task["script_id"])
        # Reset any prior mode state
        self._wizard_view_is_legacy = False
        self._wizard_mode = mode
        self._wizard_view_task_id = task_id
        # Apply wizard_snapshot if present, else build a partial snapshot
        # from tasks.config + scripts.tts_config for legacy graceful degradation.
        raw_snap = task.get("wizard_snapshot")
        is_legacy = not raw_snap
        if raw_snap:
            try:
                snapshot = json.loads(raw_snap)
            except Exception:
                snapshot = empty_snapshot()
        else:
            snapshot = empty_snapshot()
            cfg_str = task.get("config") or "{}"
            try:
                cfg = json.loads(cfg_str) if isinstance(cfg_str, str) else cfg_str
            except Exception:
                cfg = {}
            # Map legacy cfg fields to the snapshot schema where they overlap
            # Legacy cfg stored rate/pitch as strings like "0%" / "0Hz",
            # but the new snapshot schema wants raw int slider values. Parse
            # them here so old tasks render correctly in view mode.
            def _parse_pct(v):
                if v is None: return None
                if isinstance(v, (int, float)): return int(v)
                s = str(v).replace("%", "").replace("+", "").strip()
                try: return int(float(s))
                except (TypeError, ValueError): return None
            legacy_map = {
                "voice": cfg.get("voice"),
                "rate": _parse_pct(cfg.get("rate")),
                "pitch": _parse_pct(cfg.get("pitch")),
                "count": cfg.get("count"),
                "workers": cfg.get("workers"),
                "fps": cfg.get("fps"),
                "enable_bgm": cfg.get("enable_bgm"),
                "bgm_volume": cfg.get("bgm_volume"),
                "task_name": cfg.get("task_name"),
            }
            if script:
                legacy_map["script_text"] = script.get("narration")
                legacy_map["script_name"] = script.get("name")
            for k, v in legacy_map.items():
                if v is not None:
                    snapshot["fields"][k] = v
        # 同时填 _wizard_draft（兼容老代码 + app-test 期望）
        self._wizard_draft = {
            "script_text": snapshot["fields"].get("script_text") or "",
            "script_name": snapshot["fields"].get("script_name") or "",
            "voice": snapshot["fields"].get("voice") or "zh-CN-XiaoxiaoNeural",
            "rate": str(snapshot["fields"].get("rate") or 0) + "%",
            "pitch": str(snapshot["fields"].get("pitch") or 0) + "Hz",
            "count": snapshot["fields"].get("count") or 100,
            "workers": snapshot["fields"].get("workers") or 0,
            "fps": snapshot["fields"].get("fps") or 30,
            "enable_bgm": bool(snapshot["fields"].get("enable_bgm")),
            "bgm_volume": snapshot["fields"].get("bgm_volume") or 12,
            "shot_duration": 2.0,
            "lang": "zh",
        }
        # Enter the wizard then apply the snapshot to the live widgets
        self._start_wizard()
        self._apply_wizard_snapshot(snapshot)
        self._wizard_view_is_legacy = is_legacy
        # In view mode, disable every input widget. In fork mode, leave them
        # editable so the user can change anything before generating.
        self._set_wizard_readonly(mode == "view")
        # Sync all 4 banner instances with the right text and visibility.
        # Only the currently-visible step page's banner is actually shown
        # by Qt (other pages are hidden by the QStackedWidget), but we
        # write to all 4 so when the user flips steps the banner is ready.
        for _ban, _lbl, _btn in self._wizard_view_banners:
            _ban.setVisible(True)
            if mode == "view":
                title = f"📖 查看配置 · 任务 #{task_id}"
                if is_legacy:
                    title += "  ·  ⚠️ 早期任务，仅显示部分字段"
                _lbl.setText(title)
                _btn.setText("知道了")
            else:
                title = f"📝 基于任务 #{task_id} 新建（可编辑）"
                if is_legacy:
                    title += "  ·  ⚠️ 早期任务，配置不完整，建议检查后修改"
                _lbl.setText(title)
                _btn.setText("取消并返回")
        # In view mode, also dim the "下一步" buttons to make "知道了" the
        # obvious exit. We can't easily hide them across 4 step pages
        # without major refactor, so we leave the nav buttons enabled but
        # add a visual hint via the banner.
        self._log(
            f"[wizard] 任务 #{task_id} 以 {mode} 模式打开，"
            f"snapshot={('present' if not is_legacy else '缺失-降级')}"
        )

    def _exit_wizard_view(self) -> None:
        """Exit view/fork mode: hide the banner, lift readonly, go back to dashboard."""
        self._set_wizard_readonly(False)
        for _ban, _lbl, _btn in self._wizard_view_banners:
            _ban.setVisible(False)
        self._wizard_mode = None
        self._wizard_view_task_id = None
        self._wizard_view_is_legacy = False
        self._show_page(self.PAGE_DASHBOARD)

    def _open_wizard_view(self, task_id: int) -> None:
        """Dashboard "查看配置" icon entry: shorthand for _enter_wizard_for(..., view)."""
        self._enter_wizard_for(task_id, mode="view")

    def _build_step_bar(self, current_step: int) -> QWidget:
        bar = QWidget()
        bar.setObjectName("step_bar")
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar.setMinimumHeight(56)

        # 外层：步骤条(可伸缩) + 关闭按钮(固定)
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 步骤条容器
        track = QWidget()
        track.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        track.setMinimumHeight(56)
        track_lay = QHBoxLayout(track)
        track_lay.setContentsMargins(0, 8, 0, 8)
        track_lay.setSpacing(0)

        steps_data = [
            (1, "素材"),
            (2, "文案"),
            (3, "配置"),
            (4, "生成"),
        ]

        for idx, (num, label) in enumerate(steps_data):
            # 每段的节点容器（编号圆圈 + 标签）
            node = QWidget()
            node_lay = QVBoxLayout(node)
            node_lay.setContentsMargins(0, 0, 0, 0)
            node_lay.setSpacing(2)
            node.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

            # 圆圈编号
            circle = QLabel(str(num))
            circle.setAlignment(Qt.AlignCenter)
            circle.setFixedSize(28, 28)
            if num < current_step:
                circle.setStyleSheet(
                    "background:#10B981; color:#FFFFFF; border-radius:14px; "
                    "font-size:13px; font-weight:700;"
                )
            elif num == current_step:
                circle.setStyleSheet(
                    "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2563EB,stop:1 #10B981);"
                    "color:#FFFFFF; border-radius:14px; font-size:13px; font-weight:700;"
                )
            else:
                circle.setStyleSheet(
                    "background:#F3F4F6; color:#9CA3AF; border-radius:14px; "
                    "font-size:13px; font-weight:700;"
                )

            # 步骤标签
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignCenter)
            if num < current_step:
                lbl.setStyleSheet("color:#10B981; font-size:12px; font-weight:700; background:transparent;")
            elif num == current_step:
                lbl.setStyleSheet("color:#2563EB; font-size:12px; font-weight:700; background:transparent;")
            else:
                lbl.setStyleSheet("color:#9CA3AF; font-size:12px; font-weight:600; background:transparent;")

            node_lay.addWidget(circle, 0, Qt.AlignCenter)
            node_lay.addWidget(lbl, 0, Qt.AlignCenter)
            # Make the step node clickable to jump directly to that step.
            # Cursor + tooltip signal clickability; the actual handler is
            # installed via mousePressEvent override so the existing visual
            # layout (QWidget containing QVBoxLayout of QLabel children)
            # is preserved.
            node.setCursor(Qt.PointingHandCursor)
            node.setToolTip(f"跳到第 {num} 步：{label}")
            node.mousePressEvent = (
                lambda _evt, _n=num: self._go_to_step(_n)
            )
            track_lay.addWidget(node)

            # 连接线：只在两个节点之间
            if idx < len(steps_data) - 1:
                line = QFrame()
                line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                line.setMinimumHeight(3)
                line.setFrameShape(QFrame.NoFrame)
                if num < current_step:
                    line.setStyleSheet("background:#10B981; border-radius:2px;")
                else:
                    line.setStyleSheet("background:#E5E7EB; border-radius:2px;")
                track_lay.addWidget(line, 1)

        outer.addWidget(track, 1)
        return bar
    # ══════════════════════════════════════════════════════════
    # 向导导航
    # ══════════════════════════════════════════════════════════
    def _start_wizard(self):
        """打开向导，预填已有草稿"""
        # 如果有草稿，恢复
        if self._wizard_draft.get("script_text"):
            self._wiz_script_editor.setText(self._wizard_draft["script_text"])
        if self._wizard_draft.get("script_name"):
            self._wiz_script_name.setText(self._wizard_draft["script_name"])
        if self._wizard_draft.get("voice"):
            idx = self._wiz_voice.findText(self._wizard_draft["voice"])
            if idx >= 0: self._wiz_voice.setCurrentIndex(idx)
        if "rate" in self._wizard_draft:
            rate_val = int(self._wizard_draft["rate"].replace("%","").replace("+",""))
            self._wiz_rate.setValue(rate_val)
        if "pitch" in self._wizard_draft:
            pitch_val = int(self._wizard_draft["pitch"].replace("Hz","").replace("+",""))
            self._wiz_pitch.setValue(pitch_val)
        if "count" in self._wizard_draft:
            self._wiz_count.setValue(self._wizard_draft["count"])
        if "workers" in self._wizard_draft:
            self._wiz_workers.setValue(self._wizard_draft["workers"])
        if "fps" in self._wizard_draft:
            idx = self._wiz_fps.findText(str(self._wizard_draft["fps"]))
            if idx >= 0: self._wiz_fps.setCurrentIndex(idx)
        if "enable_bgm" in self._wizard_draft:
            self._wiz_enable_bgm.setChecked(self._wizard_draft["enable_bgm"])
        if "bgm_volume" in self._wizard_draft:
            self._wiz_bgm_volume.setValue(self._wizard_draft["bgm_volume"])
        if "shot_duration" in self._wizard_draft:
            self._wiz_shot_duration.setValue(self._wizard_draft["shot_duration"])
        if "voice" in self._wizard_draft:
            idx = self._wiz_voice.findText(self._wizard_draft["voice"])
            if idx >= 0: self._wiz_voice.setCurrentIndex(idx)
        self._go_to_step(1)
    # ── 设置持久化 ──
    _SETTINGS_FILE = CONFIG_DIR / "last_settings.json"
    def _save_last_settings(self, settings: dict):
        try:
            with open(self._SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False)
        except Exception:
            pass
    def _load_last_settings(self) -> dict:
        try:
            if self._SETTINGS_FILE.exists():
                with open(self._SETTINGS_FILE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    def _on_new_task(self):
        """清空草稿，开始新任务（恢复上次配音设置）"""
        self._wizard_draft = {}
        self._wiz_script_editor.clear()
        self._wiz_script_name.clear()
        # 清空 Step 1 / Step 3 任务名（同时触发 status label 重置）
        if hasattr(self, "_wiz_task_name"):
            self._wiz_task_name.clear()
        if hasattr(self, "_wiz_step3_tname_edit"):
            self._wiz_step3_tname_edit.clear()
        # 恢复上次使用的配音设置
        last = self._load_last_settings()
        # count 不从 last settings恢复 (per-batch决策, 默认100)
        self._wiz_count.setValue(100)
        self._wiz_workers.setValue(last.get("workers", 0))
        self._wiz_fps.setCurrentIndex(0)
        self._wiz_enable_bgm.setChecked(last.get("enable_bgm", False))
        self._wiz_bgm_volume.setValue(last.get("bgm_volume", 12))
        voice = last.get("voice", "zh-CN-XiaoxiaoNeural")
        idx = self._wiz_voice.findText(voice)
        if idx >= 0:
            self._wiz_voice.setCurrentIndex(idx)
        self._wiz_shuffle.setChecked(True)
        self._wiz_transition.setChecked(True)
        self._wiz_sub_slider.setValue(13)
        self._wiz_dedup.setChecked(False)
        self._wiz_selected_materials = set()
        self._wiz_log.clear()
        self._wiz_progress.setValue(0)
        self._current_task_id = None
        self._wizard_gen_done_flag = False
        # 刷新素材列表
        self._refresh_wizard_material_list()
        self._go_to_step(1)
    def _go_to_step(self, step: int):
        # 复制流程模式下拦截 step 4（除非是 _on_wizard_start_generate 内部跳转）
        if step == 4 and getattr(self, "_is_copy_flow", False) and not getattr(self, "_allow_step4_internal", False):
            QMessageBox.information(self, "复制流程", "复制流程模式下不需要去生成页，点 step 3 的「🚀 开始生成」直接渲染")
            return
        pages = {1: self.PAGE_WIZARD_STEP1, 2: self.PAGE_WIZARD_STEP2,
                 3: self.PAGE_WIZARD_STEP3, 4: self.PAGE_WIZARD_STEP4}
        if step in pages:
            self._show_page(pages[step])
    # ══════════════════════════════════════════════════════════
    # 生成逻辑
    # ══════════════════════════════════════════════════════════
    def _on_wizard_start_generate(self):
        text = self._wiz_script_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先输入口播文案")
            return
        from autokat.core.material import build_material_pool
        pool = build_material_pool()
        if not pool:
            from autokat.models.db import get_all_materials
            all_mats = get_all_materials()
            if all_mats:
                QMessageBox.warning(self, "提示", "当前只有图片素材，请导入视频素材")
            else:
                QMessageBox.warning(self, "提示", "请先导入素材")
            return
        # ═════════════════════════════════════════════════════════
        # 整个 wizard 启动阶段包在 try 里 — 任何一行崩了都通过
        # stderr + QMessageBox + wiz_log 三路同时上报。这样下次
        # 再出现"task 没建出来"那种 task 200 式的玄学问题，用户
        # 能直接看到根因，而不是只看到 save_script 后的孤儿脚本。
        # ═════════════════════════════════════════════════════════
        script_id = None
        try:
            # 保存本次配音设置供下次使用
            self._save_last_settings({
                "voice": self._wiz_voice.currentText(),
                "rate": self._wiz_rate.value(),
                "pitch": self._wiz_pitch.value(),
                "count": self._wiz_count.value(),
                "workers": self._wiz_workers.value(),
                "fps": int(self._wiz_fps.currentText()),
                "enable_bgm": self._wiz_enable_bgm.isChecked(),
                "bgm_volume": self._wiz_bgm_volume.value(),
                "shot_duration": self._wiz_shot_duration.value(),

            })
            count = self._wiz_count.value()
            workers = self._wiz_workers.value()
            fps = int(self._wiz_fps.currentText())
            enable_bgm = self._wiz_enable_bgm.isChecked()
            enable_dedup = self._wiz_dedup.isChecked()
            rate_val = self._wiz_rate.value()
            pitch_val = self._wiz_pitch.value()
            voice_lang = self._wiz_voice.currentText()[:2]
            lang = "th" if voice_lang == "th" else "zh"
            # Get selected materials
            mat_ids = list(self._wiz_selected_materials) if hasattr(self, "_wiz_selected_materials") else None
            if not mat_ids:
                mat_ids = None
            # 任务名：优先取 Step 3 编辑框（用户在最后一步可改名），
            # 退而求其次取 Step 1 任务名输入框。两者都没填则提示用户先填，
            # 不再用"批量生成"糊弄过去——任务名会作为输出目录的一部分，
            # 不填会变"未命名_时间戳"用户根本分不清哪个目录是哪个任务。
            tname = ""
            if hasattr(self, "_wiz_step3_tname_edit"):
                tname = self._wiz_step3_tname_edit.text().strip()
            if not tname and hasattr(self, "_wiz_task_name"):
                tname = self._wiz_task_name.text().strip()
            if not tname:
                QMessageBox.warning(
                    self, "提示",
                    "请先填写「任务名称」\n（Step 1 顶部或 Step 3 顶部都能填）\n\n"
                    "输出目录会按此命名，不填会显示为「未命名」。"
                )
                return
            # 同步回 step 1（保持两个输入框一致）
            if hasattr(self, "_wiz_task_name") and self._wiz_task_name.text().strip() != tname:
                self._wiz_task_name.setText(tname)
            self._wiz_script_name.setText(tname)
            self._wizard_draft["script_name"] = tname
            tts_config = {
                "voice": self._wiz_voice.currentText(),
                "rate": f"{rate_val:+d}%",
                "pitch": f"{pitch_val:+d}Hz",
            }
            script_id = save_script(
                tname or self._wiz_script_name.text().strip() or "批量生成",
                text, lang=lang, tts_config=tts_config
            )
            # 收集选中的BGM文件列表，传给renderer随机使用
            bgm_files = None
            if enable_bgm:
                # 新版 UI: 用 QComboBox 选单首 BGM。
                # - 「随机选择」(data=None) -> bgm_files=get_bgm_files()，每个视频随机选一首
                # - 选中具体文件 -> bgm_files=[path]，renderer 强制用这首
                selected = self._wiz_bgm_combo.currentData()
                if selected:
                    bgm_files = [str(selected)]
                    self._log(f"选用 BGM: {Path(str(selected)).name}")
                else:
                    bgm_files = get_bgm_files()
                    self._log(f"BGM 模式: 随机选择（{len(bgm_files)} 首可选）")
            bgm_path = None
            # 进入 Step 4
            self._allow_step4_internal = True
            try:
                self._go_to_step(4)
            finally:
                self._allow_step4_internal = False
            self._current_task_id = None
            self._wiz_progress.setRange(0, count)
            self._wiz_progress.setValue(0)
            self._wiz_task_info.setText(f"正在生成 {count} 条视频 (并发:{workers})...")
            # 新任务启动：重置按钮可见性 + enabled（上次完成时可能被 setVisible(False) 隐藏了）
            self._wiz_pause_btn.setVisible(True)
            self._wiz_pause_btn.setEnabled(True)
            self._wiz_stop_btn.setVisible(True)
            self._wiz_stop_btn.setEnabled(True)
            # 重置「用户手动改音色」标记 — 新任务的文案/语种变化可以再次自动选音色
            self._wiz_voice_user_set = False
            # 隐藏上次的「打开输出目录」按钮（新任务还没完，没目录可看）
            if hasattr(self, "_wiz_output_dir_link"):
                self._wiz_output_dir_link.setVisible(False)
            self._wiz_log.setText("")
            # ─── 阶段 0：任务配置确认（直接 append，绕过队列） ───
            from autokat.core.renderer import _get_encoder_label
            _ts = datetime.now().strftime('%Y%m%d %H:%M:%S')
            _seg_count = len([p for p in text.split("---") if p.strip()])
            _char_count = len(text.replace("---", "").replace("\n", "").replace(" ", "").strip())
            _sel_count = len(mat_ids) if mat_ids else 0
            try:
                _total_mats = len([m for m in get_all_materials() if m.get("mat_type") == "video"])
            except Exception:
                _total_mats = len(pool)
            try:
                _sub_pos_label = f"{self._wiz_sub_slider.value()}%" if hasattr(self, "_wiz_sub_slider") else "底部"
            except Exception:
                _sub_pos_label = "底部"
            try:
                _bgm_vol = self._wiz_bgm_volume.value()
            except Exception:
                _bgm_vol = 30
            try:
                _shot_dur = self._wiz_shot_duration.value()
            except Exception:
                _shot_dur = 2.0
            # max_clips 控件已移除: 不再限制每条镜头上限, 按 TTS 音轨时长自动填切片
            _max_clips = 0  # 0 表示不限
            self._wiz_log.append(f"[{_ts}] ─── 任务配置确认 ───")
            self._wiz_log.append(f"[{_ts}] 🆔 任务名: {tname or '批量生成'}  ·  目标成片: {count} 条")
            self._wiz_log.append(f"[{_ts}] 🎤 语音: {self._wiz_voice.currentText()}  ·  语速: {rate_val:+d}%  ·  音调: {pitch_val:+d}Hz")
            self._wiz_log.append(f"[{_ts}] 📝 文案: {_seg_count} 段（--- 分隔）  ·  总字符: {_char_count}  ·  语言: {lang}")
            self._wiz_log.append(f"[{_ts}] 🎬 素材: 共 {_total_mats} 个 video  ·  选中: {_sel_count}（{'指定' if _sel_count else '默认全部'}）  ·  去重: {'开启' if enable_dedup else '关闭'}")
            self._wiz_log.append(f"[{_ts}] 🎞️ 渲染参数: {fps} FPS  ·  最小时长 {_shot_dur}s  ·  每条镜头不限数 (按 TTS 时长自动填)  ·  并发: {workers}")
            _bgm_label = Path(bgm_path).name if bgm_path else "关闭"
            self._wiz_log.append(f"[{_ts}] 🎵 BGM: {_bgm_label}  ·  音量: {_bgm_vol}%")
            self._wiz_log.append(f"[{_ts}] 💬 字幕: 位置={_sub_pos_label}  ·  字体=Source Han Sans  ·  描边=1px")
            self._wiz_log.append(f"[{_ts}] 🖥️ 编码器: {_get_encoder_label()}  ·  DeepSeek API: {'✅' if os.environ.get('DEEPSEEK_API_KEY') else '❌ 未配置'}")
            self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] 开始生成 {count} 条...")
            self._gen_start_time = datetime.now()
            def do_gen():
                return run_generate(
                    text=text,
                    name=tname or self._wizard_draft.get("script_name", "批量生成"),
                    count=count,
                    workers=workers,
                    fps=fps,
                    lang=lang,
                    voice=tts_config.get("voice") if tts_config else None,
                    rate=(tts_config.get("rate", "+0%").replace("%", "").replace("+", "")) if tts_config else None,
                    pitch=(tts_config.get("pitch", "+0Hz").replace("Hz", "").replace("+", "")) if tts_config else None,
                    min_shot_duration=self._wiz_shot_duration.value(),

                    no_bgm=not enable_bgm,
                    bgm=bgm_path,
                    bgm_files=bgm_files,
                    subtitle_position=f"{self._wiz_sub_slider.value()}%" if hasattr(self, "_wiz_sub_slider") else None,
                    materials=mat_ids,
                    reuse_script=True,
                    wait=True,
                    log_fn=emit,
                    extra_config=self._build_wiz_extra_config(),
                )
            self._worker = WorkerThread(do_gen)
            self._worker.finished.connect(lambda tid: self._on_wizard_gen_done(tid, enable_dedup))
            self._worker.error.connect(self._on_wizard_gen_error)
            self._worker.start()
            self._wiz_poll_timer = QTimer()
            self._wiz_poll_timer.timeout.connect(self._poll_wizard_progress)
            _log_clear()
            if not hasattr(self, "_wiz_clip_timing") or not isinstance(self._wiz_clip_timing, dict):
                self._wiz_clip_timing = {}
            else:
                self._wiz_clip_timing.clear()
            if not hasattr(self, "_wiz_log_line_for_clip") or not isinstance(self._wiz_log_line_for_clip, dict):
                self._wiz_log_line_for_clip = {}
            else:
                self._wiz_log_line_for_clip.clear()
            if hasattr(self, "_wiz_done_list"):
                self._wiz_done_list.clear()
            self._wiz_done_list_sig = []
            self._wiz_poll_timer.start(200)
        except Exception as ex:
            import traceback as _tb
            _tb.print_exc()
            _err = f"{type(ex).__name__}: {ex}"
            try:
                self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ❌ 启动失败: {_err}")
            except Exception:
                pass
            try:
                QMessageBox.critical(
                    self, "启动生成失败",
                    f"wizard 阶段启动异常：\n\n{_err}\n\n"
                    f"脚本{'已入库（id=' + str(script_id) + '）' if script_id else '未入库'}，但 task 未创建。\n"
                    f"重启 GUI 后可以重新生成。\n\n"
                    f"完整 traceback 已打到终端。",
                )
            except Exception:
                pass

    def _poll_wizard_progress(self):
        # 全函数 try/except 包裹：之前 _handle_render_log / DB 读失败会让 2s 轮询静默死亡，
        # 队列里消息积压，UI 一直停在 '开始生成' 看似卡死
        try:
            self._poll_wizard_progress_inner()
        except Exception as ex:
            import traceback
            traceback.print_exc()
            try:
                self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ⚠️ 进度刷新异常: {ex}")
            except Exception:
                pass
    def _poll_wizard_progress_inner(self):
        if not self._current_task_id:
            # worker 线程刚启动，task 还没在 DB 落地前的窗口里，
            # 之前直接 return 导致 done_list / 进度条 / 阶段日志全不更新。
            # 找最近 60 秒内创建的最新任务补上
            try:
                t_latest = get_latest_task(limit_seconds=60)
                if t_latest:
                    self._current_task_id = t_latest["id"]
            except Exception:
                pass
            if not self._current_task_id:
                return
        if hasattr(self, "_wizard_gen_done_flag") and self._wizard_gen_done_flag:
            return
        if not hasattr(self, "_wiz_clip_timing"):
            self._wiz_clip_timing = {}
        # 把渲染线程推过来的阶段日志写入 _wiz_log（2s 一次），单条解析失败不影响后续
        for msg in _log_drain():
            try:
                self._handle_render_log(msg)
            except Exception as ex_inner:
                import traceback
                traceback.print_exc()
                try:
                    self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ⚠️ 日志行解析失败: {ex_inner}")
                except Exception:
                    pass
        task = get_task(self._current_task_id)
        if task:
            self._wiz_progress.setRange(0, task["total"])
            # 强制从 DB 最新状态读，避免缓存导致 19/20
            task = get_task(self._current_task_id)
            if not task:
                return
            self._wiz_progress.setRange(0, task["total"])
            if task["status"] == "done":
                self._wiz_progress.setValue(task["total"])
                self._wiz_progress.repaint()
                self._wiz_task_info.setText(f"✅ 任务 #{task['id']} 已完成！")
            elif task["status"] == "failed":
                self._wiz_progress.setValue(task["done"])
                self._wiz_task_info.setText(f"❌ 任务 #{task['id']} 已失败")
            else:
                self._wiz_progress.setValue(task["done"])
                self._wiz_task_info.setText(
                    f"任务 #{task['id']} | {task['done']}/{task['total']} 条 ({task['status']})"
                )
            # 当前活动：任务完成后显示静态提示；进行中拼3段信息
            rendering = get_rendering_clips(self._current_task_id)
            stage_text, stage_age = _log_get_stage()
            parts = []
            if task["status"] == "done":
                parts.append("🎉 全部完成！")
            elif task["status"] == "failed":
                parts.append("❌ 渲染失败")
            elif stage_text:
                # 阶段卡了 > 60s 时附加提示，让用户知道"在等"而不是"卡死"
                if stage_age > 10:
                    stage_text += f"  · 已等待 {int(stage_age)}s"
                parts.append(stage_text)
            if rendering:
                clip_parts = [f"第 {c['idx']+1} 条 · {c['progress_detail'] or '处理中...'}"
                              for c in rendering]
                parts.append("   ".join(clip_parts))
            if not parts:
                # 启动到阶段写入之间短暂空窗，给个 spinner 提示
                if not hasattr(self, "_wiz_heartbeat_idx"):
                    self._wiz_heartbeat_idx = 0
                _HEARTBEAT = ["⏳ ", "🔄 ", "⚙️ "]
                self._wiz_heartbeat_idx = (self._wiz_heartbeat_idx + 1) % len(_HEARTBEAT)
                parts.append(_HEARTBEAT[self._wiz_heartbeat_idx] + "准备中...")
            # 心跳 spinner：每 1s 切一次前缀，即使内容没变也在视觉上"动"起来
            _SPIN = ["·  ", "·· ", "···", " · ", "  ·", "  ·"]
            if not hasattr(self, "_wiz_spin_idx"):
                self._wiz_spin_idx = 0
            self._wiz_spin_idx = (self._wiz_spin_idx + 1) % len(_SPIN)
            spin_prefix = "  " + _SPIN[self._wiz_spin_idx] + "  "
            self._wiz_current_activity.setText(spin_prefix + "  ".join(parts))
            # 已完成视频列表：从 DB 拉 done clip，sig 变了才重建（避免每 1s 闪一次）
            self._refresh_wizard_done_list(force=False)
            # ETA：基于已完成 clip 的真实耗时，按"剩余条数 × 均值 / 并发"算，避开失败 clip 污染
            try:
                task_cfg = json.loads(task["config"]) if isinstance(task["config"], str) else (task["config"] or {})
            except Exception:
                task_cfg = {}
            workers = max(1, int(task_cfg.get("workers", 1)))
            # 检测新开始的 / 刚完成的 clip，更新 _wiz_clip_timing
            rendering_ids = {r["idx"] for r in rendering} if rendering else set()
            for cid in rendering_ids:
                if cid not in self._wiz_clip_timing:
                    self._wiz_clip_timing[cid] = {"start": datetime.now(), "duration": None}
            for cid, t in list(self._wiz_clip_timing.items()):
                if cid not in rendering_ids and t.get("duration") is None:
                    t["duration"] = (datetime.now() - t["start"]).total_seconds()
            # 最近 5 条已完成 clip 算平均（避免首条冷启动影响）
            done_durations = [t["duration"] for t in self._wiz_clip_timing.values() if t.get("duration") is not None]
            if done_durations:
                recent = done_durations[-5:]
                avg_per_clip = sum(recent) / len(recent)
                pending = task["total"] - task["done"]
                eta_sec = pending * avg_per_clip / workers
            if task["status"] == "done":
                # 完成统计：总耗时 + 平均耗时
                if done_durations:
                    total_dur = sum(done_durations)
                    avg_dur = sum(done_durations) / len(done_durations)
                    total_sec = total_dur
                    self._wiz_eta_label.setText(
                        f"{task['done']} 个视频混剪完成，"
                        f"总用时 {int(total_sec//60)} 分 {int(total_sec%60)} 秒，"
                        f"平均 {avg_dur:.0f} 秒/视频"
                    )
                else:
                    self._wiz_eta_label.setText(f"{task['done']} 个视频混剪完成")
                if self._wiz_poll_timer:
                    self._wiz_poll_timer.stop()
                self._on_wizard_gen_done(task["id"], False)
            elif task["status"] == "failed":
                self._wiz_eta_label.setText("渲染失败")
            elif done_durations:
                avg_per_clip = sum(done_durations[-5:]) / min(5, len(done_durations))
                pending = task["total"] - task["done"]
                eta_sec = pending * avg_per_clip / workers
                self._wiz_eta_label.setText(
                    f"预计剩余: {int(eta_sec//60)} 分 {int(eta_sec%60)} 秒 "
                    f"({avg_per_clip:.0f}s/条 × {workers} 并发)"
                )
            else:
                self._wiz_eta_label.setText("⏳ 准备中...")
                # 之前误报: 任务刚启动 done_durations=空,不代表失败
    def _handle_render_log(self, msg: str):
        if not hasattr(self, "_wiz_log_line_for_clip"):
            self._wiz_log_line_for_clip = {}
        """处理一条来自渲染线程的日志消息

        简化策略：所有消息一律 append 到新 block，不再做行内替换。
        原因：原用 QTextCursor.select(BlockUnderCursor) + removeSelectedText
        替换 block 内容时，Qt 在 block 边界的行为不稳定（cursor 会折叠回
        上一个 block 末尾），导致多条消息粘连到同一行。每条消息独立成行
        反而更符合"别让用户以为卡主"的需求——看到滚动着的连续日志就知道
        渲染在跑。
        """
        import re
        # 不再维护 _wiz_log_line_for_clip 映射，避免遗留状态干扰后续任务
        self._wiz_log.append(msg)
        # 自动滚动到最底部：只在用户未手动上滚时才滚动
        # QTextEdit 滚动到底部后 cursor 在文档末尾；用户上滚后 cursor 位置会变化
        cur = self._wiz_log.textCursor()
        at_bottom = cur.position() >= len(self._wiz_log.toPlainText()) - 2
        if at_bottom:
            self._wiz_log.moveCursor(QTextCursor.End)
            self._wiz_log.ensureCursorVisible()
    def _replace_log_line(self, line_no: int, new_text: str):
        """用 new_text 替换 _wiz_log 中第 line_no 行（0-based block 编号）"""
        block = self._wiz_log.document().findBlockByNumber(line_no)
        if not block.isValid():
            self._wiz_log.append(new_text)
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.insertText(new_text)
    def _refresh_wizard_done_list(self, force: bool = False):
        """从 DB 拉 done clip 重建右侧视频列表。

        force=True：无条件重建（任务刚完成时调用，poll 已被 stop）
        force=False：仅当 (id, duration) 签名变化时才重建（poll 里用，避免每 1s 闪一次）
        """
        if not hasattr(self, "_wiz_done_list") or self._wiz_done_list is None:
            return
        if not self._current_task_id:
            return
        try:
            from pathlib import Path as _P_done
            done_clips = [c for c in get_clips_by_task(self._current_task_id) if c["status"] == "done"]
            cur_sig = [(c["id"], c.get("duration_seconds")) for c in done_clips]
            if not hasattr(self, "_wiz_done_list_sig"):
                self._wiz_done_list_sig = []
            if not force and cur_sig == self._wiz_done_list_sig:
                return
            self._wiz_done_list_sig = cur_sig
            self._wiz_done_list.clear()
            for c in done_clips:
                name = _P_done(c["output_path"]).name if c.get("output_path") else f"clip_{c['idx']:04d}.mp4"
                dur = c.get("duration_seconds")
                if dur:
                    m, s = int(dur // 60), int(dur % 60)
                    dur_str = f"{m}:{s:02d}"
                else:
                    dur_str = "—:—"
                label_text = f"✅  {name}  ·  {dur_str}"
                item = QListWidgetItem(self._wiz_done_list)
                row = QWidget()
                row.setStyleSheet("background: transparent;")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 4, 0)
                row_layout.setSpacing(8)
                lbl = QLabel(label_text)
                lbl.setStyleSheet("color:#111827; background:transparent;")
                lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                row_layout.addWidget(lbl, 1)
                play_btn = QPushButton("▶ 播放")
                play_btn.setCursor(Qt.PointingHandCursor)
                play_btn.setFixedHeight(20)
                play_btn.setStyleSheet(
                    "QPushButton{background:#2563EB;color:#FFFFFF;border:none;"
                    "border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;}"
                    "QPushButton:hover{background:#1D4ED8;}"
                    "QPushButton:pressed{background:#1E40AF;}"
                )
                video_path = c.get("output_path") or ""
                play_btn.clicked.connect(lambda _checked=False, p=video_path: self._play_video_file(p))
                row_layout.addWidget(play_btn, 0)
                item.setSizeHint(QSize(0, 40))  # 40px高：播放按钮36px+上下各2px空白
                self._wiz_done_list.addItem(item)
                self._wiz_done_list.setItemWidget(item, row)
        except Exception as _e_done:
            import traceback
            traceback.print_exc()
    def _play_video_file(self, path: str):
        """用系统默认播放器打开视频文件。失败时弹窗提示。"""
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "提示", f"视频文件不存在：\n{path}")
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as _e_play:
            QMessageBox.warning(self, "错误", f"无法打开视频：\n{_e_play}")
    def _open_wizard_output_dir(self):
        """点击「打开输出目录」时跳到 Finder/Explorer 打开任务输出目录"""
        path = getattr(self, "_wiz_last_output_dir", None)
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "提示", "该任务的输出目录还没生成或已被清理")
            return
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", path])
            elif sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", path])
        except Exception as _e_open:
            QMessageBox.warning(self, "打开失败", f"无法打开目录：\n{path}\n\n{_e_open}")
    def _on_wizard_gen_done(self, task_id, enable_dedup=False):
        self._wizard_gen_done_flag = True
        self._current_task_id = task_id
        # 最后一次 drain：worker 在 emit(finished) 之前推的进度消息还滞留在 queue 里，
        # 必须在 stop 之前捞出来，否则用户看到「开始生成 N 条…」直接跳到「完成」，
        # 中间 4 阶段（配音/编排/输出/逐条渲染）的日志全丢
        for msg in _log_drain():
            try:
                self._handle_render_log(msg)
            except Exception:
                pass
        if self._wiz_poll_timer:
            self._wiz_poll_timer.stop()
        # 任务完成：暂停/停止按钮直接隐藏（不是 disabled），
        # 不然看到两个灰着的按钮还以为是 bug
        self._wiz_pause_btn.setVisible(False)
        self._wiz_stop_btn.setVisible(False)
        # 强制刷新 done list（poll 已被 stop，必须手动拉一次 DB，
        # 否则用户看不到任何已完成的视频）
        self._refresh_wizard_done_list(force=True)
        # 算出输出目录（从第一个 done clip 的 output_path 父目录推回去）并显示打开按钮
        try:
            # 兜底强制 show 整条父链 — offscreen / 事件流异常场景下
            # progress_group (QFrame) 偶发会被设成 invisible，导致 setVisible(True) 不生效
            # show() 不会向父链传播，需要手动从子到父全部 setVisible(True)
            for _w in (self._wiz_output_dir_link, self._wiz_pause_btn, self._wiz_stop_btn,
                      self._wiz_back_to_dashboard, self._wiz_progress):
                _p = _w.parentWidget()
                while _p is not None and not _p.isVisible():
                    _p.setVisible(True)
                    _p = _p.parentWidget()
            _done_clips_now = [c for c in get_clips_by_task(task_id) if c["status"] == "done"]
            if _done_clips_now:
                _first_out = _done_clips_now[0].get("output_path")
                if _first_out:
                    self._wiz_last_output_dir = os.path.dirname(_first_out)
                    self._wiz_output_dir_link.setText(f"📁 打开 {os.path.basename(self._wiz_last_output_dir)}")
                    self._wiz_output_dir_link.setToolTip(self._wiz_last_output_dir)
                    self._wiz_output_dir_link.setVisible(True)
                    self._wiz_output_dir_link.show()
            else:
                # 任务失败或 0 成功 clip — 也算下 output_dir 名字（任务名_dir）
                # 从 task config 拿名字（可选）
                pass
        except Exception:
            import traceback
            traceback.print_exc()
        task = get_task(task_id)
        stopped_or_failed = bool(task and task["status"] == "failed")
        if stopped_or_failed:
            self._wiz_task_info.setText(f"⏹ 任务 #{task_id} 已停止或失败")
            self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ⏹ 任务已停止或失败 task_id={task_id}")
        else:
            self._wiz_task_info.setText(f"✅ 任务 #{task_id} 已完成！")
            self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ✅ 生成完成！task_id={task_id}")
        if enable_dedup and not stopped_or_failed:
            self._wiz_log.append("开始去重...")
            threading.Thread(target=lambda: self._do_dedup(), daemon=True).start()
        self._refresh_sidebar()
        self._refresh_dashboard()
        # 任务完成：清 copy flow 标记
        self._is_copy_flow = False
    def _on_wizard_gen_error(self, msg):
        self._wizard_gen_done_flag = True
        # 同 _on_wizard_gen_done：错误前 worker 推的进度消息也要 drain 出来
        for pending_log in _log_drain():
            try:
                self._handle_render_log(pending_log)
            except Exception:
                pass
        if self._wiz_poll_timer:
            self._wiz_poll_timer.stop()
        # 同 _on_wizard_gen_done：失败也隐藏暂停/停止按钮（避免 UI 半残状态）
        self._wiz_pause_btn.setVisible(False)
        self._wiz_stop_btn.setVisible(False)
        self._wiz_task_info.setText(f"❌ 生成失败")
        self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ❌ 失败: {msg}")
    def _on_wizard_pause(self):
        if self._current_task_id:
            # 复用 _pause_task：会检查状态 + log + 刷新
            self._pause_task(self._current_task_id)
            self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ⏸ 已暂停，渲染会在当前 clip 完成后停止新渲染")
            self._wiz_pause_btn.setEnabled(False)
    def _on_wizard_stop(self):
        self._stop_requested = True
        from autokat.core.renderer import request_stop
        request_stop()
        if self._current_task_id:
            from autokat.models.db import update_task_status
            update_task_status(self._current_task_id, "failed")
            self._wiz_log.append(f"[{datetime.now().strftime('%Y%m%d %H:%M:%S')}] ⏹ 已停止")
            self._wiz_pause_btn.setEnabled(False)
            self._wiz_stop_btn.setEnabled(False)
    # ══════════════════════════════════════════════════════════
    # 任务详情页
    # ══════════════════════════════════════════════════════════
    def _build_task_detail_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        # 返回按钮 + 标题
        top_row = QHBoxLayout()
        back_btn = QPushButton("← 返回工作台")
        back_btn.setStyleSheet("""
            QPushButton {
                background: #FFFFFF; color: #6B7280; border: 1.5px solid #E5E7EB; font-weight: 500;
                border-radius: 8px; padding: 7px 16px; font-size: 12px;
            }
            QPushButton:hover { background: #FCFCFD; border-color: #D1D5DB; }
        """)
        back_btn.clicked.connect(lambda: self._show_page(self.PAGE_DASHBOARD))
        top_row.addWidget(back_btn)
        self._detail_title = QLabel("任务详情")
        self._detail_title.setStyleSheet("font-size:22px; font-weight:800; color:#111827; background:transparent;")
        top_row.addWidget(self._detail_title)
        top_row.addStretch()
        layout.addLayout(top_row)
        # 概要卡片
        summary_card = QWidget()
        summary_card.setStyleSheet("background:#FFFFFF; color:#111827; border:1.5px solid #E5E7EB; border-radius:14px; padding:18px;")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setSpacing(6)
        self._detail_status_row = QHBoxLayout()
        self._detail_task_id_label = QLabel("")
        self._detail_task_id_label.setStyleSheet("font-size:18px; font-weight:800; color:#111827; background:transparent;")
        self._detail_status_row.addWidget(self._detail_task_id_label)
        self._detail_status_tag = QLabel("")
        self._detail_status_row.addWidget(self._detail_status_tag)
        self._detail_status_row.addStretch()
        self._detail_time_label = QLabel("")
        self._detail_time_label.setStyleSheet("font-size:12px; color:#6B7280; font-weight:500; background:transparent;")
        self._detail_status_row.addWidget(self._detail_time_label)
        summary_layout.addLayout(self._detail_status_row)
        self._detail_narration = QLabel("")
        self._detail_narration.setWordWrap(True)
        self._detail_narration.setStyleSheet("font-size:14px; color:#111827; line-height:1.5; background:transparent; padding:4px 0;")
        summary_layout.addWidget(self._detail_narration)
        self._detail_progress = QProgressBar()
        self._detail_progress.setMinimumHeight(8)
        self._detail_progress.setTextVisible(False)
        self._detail_progress.setStyleSheet("""
            QProgressBar {
                border: none; border-radius: 4px; background: #E5E7EB;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563EB, stop:1 #10B981);
                border-radius: 4px;
            }
        """)
        summary_layout.addWidget(self._detail_progress)
        self._detail_params = QLabel("")
        self._detail_params.setStyleSheet("font-size:12px; color:#6B7280; font-weight:500; background:transparent; padding-top:4px;")
        summary_layout.addWidget(self._detail_params)
        self._detail_quality = QLabel("质量验收：等待任务完成")
        self._detail_quality.setWordWrap(True)
        self._detail_quality.setStyleSheet(
            "font-size:12px; color:#2563EB; font-weight:600; background:#EFF6FF;"
            "border-radius:8px; padding:7px 10px;"
        )
        summary_layout.addWidget(self._detail_quality)
        self._detail_report_toggle = QToolButton()
        self._detail_report_toggle.setText("▶ 展开技术报告")
        self._detail_report_toggle.setCheckable(True)
        self._detail_report_toggle.setStyleSheet(
            "QToolButton{border:none;color:#6B7280;font-size:11px;font-weight:600;"
            "padding:4px 0;text-align:left;background:transparent;}"
        )
        self._detail_report = QTextEdit()
        self._detail_report.setReadOnly(True)
        self._detail_report.setVisible(False)
        self._detail_report.setMaximumHeight(180)
        self._detail_report.setStyleSheet(
            "QTextEdit{font-family:Menlo,Consolas,monospace;font-size:10px;"
            "background:#F9FAFB;border:1px solid #E5E7EB;border-radius:8px;padding:6px;}"
        )
        self._detail_report_toggle.toggled.connect(
            lambda checked: (
                self._detail_report.setVisible(checked),
                self._detail_report_toggle.setText(
                    "▼ 收起技术报告" if checked else "▶ 展开技术报告"
                ),
            )
        )
        summary_layout.addWidget(self._detail_report_toggle)
        summary_layout.addWidget(self._detail_report)
        # 操作按钮行
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self._detail_btn_resume = self._make_detail_btn("▶ 继续", "#10B981", lambda: self._on_detail_resume())
        action_row.addWidget(self._detail_btn_resume)
        self._detail_btn_retry = self._make_detail_btn("🔄 重试", "#F59E0B", lambda: self._on_detail_retry())
        action_row.addWidget(self._detail_btn_retry)
        # v3: 把"复现"拆成两个入口——查看配置(view) + 基于此新建(fork)。
        self._detail_btn_view = self._make_detail_btn(
            "👁 查看配置", "#0EA5E9", lambda: self._on_detail_view()
        )
        action_row.addWidget(self._detail_btn_view)
        self._detail_btn_fork = self._make_detail_btn(
            "📝 基于此新建", "#2563EB", lambda: self._on_detail_fork()
        )
        action_row.addWidget(self._detail_btn_fork)
        # 保留旧 self._detail_btn_replay 引用, 但隐藏, 防止别处代码崩
        self._detail_btn_replay = self._make_detail_btn(
            "🔁 复现 (兼容)", "#9CA3AF", lambda: self._on_detail_replay()
        )
        self._detail_btn_replay.setVisible(False)
        action_row.addWidget(self._detail_btn_replay)
        self._detail_btn_delete = self._make_detail_btn("🗑 删除", "#EF4444", lambda: self._on_detail_delete())
        action_row.addWidget(self._detail_btn_delete)
        self._detail_btn_open = self._make_detail_btn("📂 输出目录", "#6B7280", lambda: self._on_detail_open_dir())
        action_row.addWidget(self._detail_btn_open)
        # v2.3 E2/E3: Jaccard 相似度 + 矩阵发布清单
        self._detail_btn_jaccard = self._make_detail_btn("📊 Jaccard 相似度", "#7C3AED", lambda: self._on_detail_show_jaccard())
        action_row.addWidget(self._detail_btn_jaccard)
        self._detail_btn_checklist = self._make_detail_btn("📋 矩阵发布清单", "#DC2626", lambda: self._on_detail_show_publish_checklist())
        action_row.addWidget(self._detail_btn_checklist)
        action_row.addStretch()
        summary_layout.addLayout(action_row)
        layout.addWidget(summary_card)
        # 成片列表
        clips_label = QLabel("成片列表")
        clips_label.setStyleSheet("font-size:15px; font-weight:700; margin-top:8px; color:#111827; background:transparent;")
        layout.addWidget(clips_label)
        self._detail_clips_list = QListWidget()
        self._detail_clips_list.setStyleSheet("""
            QListWidget {
                background: #FFFFFF; border: 1.5px solid #E5E7EB; border-radius: 12px; padding: 4px;
            }
            QListWidget::item { padding: 10px 12px; border-bottom: 1px solid #E5E7EB; color: #111827; border-radius: 6px; margin: 1px; }
            QListWidget::item:hover { background: #FCFCFD; }
        """)
        layout.addWidget(self._detail_clips_list, 1)
        return page
    def _on_detail_show_jaccard(self) -> None:
        """v2.3 E2: 弹窗显示成片两两 Jaccard 相似度热力图"""
        from autokat.core.diversity import compute_jaccard
        from autokat.models.db import get_clips_by_task
        task_id = self._detail_task_id
        if not task_id:
            return
        clips = [c for c in get_clips_by_task(task_id)
                 if c.get("status") == "done" and c.get("output_path") and os.path.exists(c["output_path"])]
        if len(clips) < 2:
            QMessageBox.information(self, "Jaccard 相似度",
                f"需要 ≥2 条成片才能算相似度，当前 {len(clips)} 条。")
            return

        # 读 metadata.json 里 clips.material_ids (矩阵号差异化核心数据)
        slice_sets: list[set] = []
        names: list[str] = []
        for c in clips:
            names.append(os.path.basename(c["output_path"]))
            mat_meta = c["output_path"] + ".metadata.json"
            if os.path.exists(mat_meta):
                try:
                    with open(mat_meta, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    ids = set(m.get("clip_summary", {}).get("material_ids", []))
                    slice_sets.append(ids if ids else {f"clip_{c['idx']}"})
                except Exception:
                    slice_sets.append({f"clip_{c['idx']}"})
            else:
                slice_sets.append({f"clip_{c['idx']}"})

        n = len(clips)
        matrix = [[compute_jaccard(slice_sets[i], slice_sets[j]) for j in range(n)] for i in range(n)]

        dlg = QDialog(self)
        dlg.setWindowTitle(f"📊 Jaccard 相似度 (任务 #{task_id})")
        dlg.resize(720, 600)
        layout = QVBoxLayout(dlg)
        title = QLabel(f"成片两两切片组合相似度 · n={n} · 目标 <= 0.5 (即 >= 50% 不一样)")
        title.setStyleSheet("font-size:13px; font-weight:600; padding:6px;")
        layout.addWidget(title)

        table = QTableWidget(n, n)
        table.setHorizontalHeaderLabels(names)
        table.setVerticalHeaderLabels(names)
        table.horizontalHeader().setStretchLastSection(True)
        for i in range(n):
            for j in range(n):
                v = matrix[i][j]
                if i == j:
                    text, bg = "—", "#E5E7EB"
                elif v <= 0.3:
                    text, bg = f"{v:.2f}", "#10B981"   # 绿 — 很不一样
                elif v <= 0.5:
                    text, bg = f"{v:.2f}", "#84CC16"   # 黄绿
                elif v <= 0.7:
                    text, bg = f"{v:.2f}", "#F59E0B"   # 橙
                else:
                    text, bg = f"{v:.2f}", "#EF4444"   # 红 — 太像
                item = QTableWidgetItem(text)
                item.setBackground(QColor(bg))
                item.setForeground(QColor("#FFFFFF") if v > 0.3 and i != j else QColor("#111827"))
                item.setTextAlignment(Qt.AlignCenter)
                table.setItem(i, j, item)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(table, 1)

        legend = QLabel("🟢 ≤0.30 (很不一样)  🟡 0.30-0.50  🟠 0.50-0.70  🔴 >0.70 (太像, 平台大概率判重)")
        legend.setStyleSheet("font-size:11px; color:#6B7280; padding:6px;")
        layout.addWidget(legend)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.close)
        layout.addWidget(close_btn)
        dlg.exec_()

    def _on_detail_show_publish_checklist(self) -> None:
        """v2.3 E3: 弹窗显示矩阵号必改发布清单 (基于 metadata.json)"""
        from autokat.models.db import get_clips_by_task
        task_id = self._detail_task_id
        if not task_id:
            return
        clips = [c for c in get_clips_by_task(task_id)
                 if c.get("status") == "done" and c.get("output_path")]
        if not clips:
            QMessageBox.information(self, "矩阵发布清单", "当前任务暂无成片。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"📋 矩阵号发布清单 (任务 #{task_id}, {len(clips)} 条成片)")
        dlg.resize(760, 640)
        layout = QVBoxLayout(dlg)

        intro = QLabel(
            "每条成片落到不同账号前, 平台指纹 4 层查重 + 矩阵集群风控会判重.\n"
            "请按下列清单对每条成片做人工差异化, 再发到对应账号:"
        )
        intro.setStyleSheet("font-size:12px; padding:6px; color:#111827;")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # 每条成片一行
        for c in clips:
            mp4 = c["output_path"]
            stem = Path(mp4).name
            meta = mp4 + ".metadata.json"
            voice = "—"; rate = "—"; bgm = "—"; anim = "—"; font = "—"
            md5_short = "—"
            try:
                if os.path.exists(meta):
                    with open(meta, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    voice = m.get("audio", {}).get("voice", "—")
                    rate = m.get("audio", {}).get("rate", "—")
                    bgm = Path(m.get("bgm", {}).get("path") or "无").name
                    anim = m.get("perturbation", {}).get("level", "—")
                    md5_short = m.get("video", {}).get("md5", "—")
            except Exception:
                pass

            card = QWidget()
            card.setStyleSheet(
                "background:#FFFFFF; border:1.5px solid #E5E7EB; border-radius:10px; padding:10px;"
            )
            cl = QVBoxLayout(card)
            cl.setSpacing(4)
            head = QLabel(f"🎬 {stem}    ·  md5={md5_short}")
            head.setStyleSheet("font-size:12px; font-weight:700; color:#111827;")
            cl.addWidget(head)
            info = QLabel(
                f"   配音: {voice} {rate}   ·   BGM: {bgm}   ·   扰动: {anim}"
            )
            info.setStyleSheet("font-size:11px; color:#6B7280; font-family:Menlo,Consolas,monospace;")
            cl.addWidget(info)
            for chk in [
                "[ ] 改写标题, 调换核心语序/引导话术 (OCR 文本指纹)",
                "[ ] 改写正文描述, 不要复用种草/科普原文 (OCR 文本指纹)",
                "[ ] 替换 4-6 个非核心话题标签 (话题指纹)",
                "[ ] 制作独立封面, 构图/配色/文字不能复用 (封面指纹)",
                "[ ] 错峰发布, 间隔 ≥15 分钟, 不要 5 分钟内批量群发 (行为风控)",
                "[ ] 不同账号不同手机/不同 IP, 严禁多账号同设备 (集群风控)",
            ]:
                ck = QCheckBox(chk)
                ck.setStyleSheet("font-size:11px; color:#374151; padding:2px 0;")
                cl.addWidget(ck)
            layout.addWidget(card)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.close)
        layout.addWidget(close_btn)
        dlg.exec_()

    @staticmethod
    def _make_detail_btn(text, color, cb) -> QPushButton:
        b = QPushButton(text)
        b.setStyleSheet(f"""
            QPushButton {{
                background: #FFFFFF; color: {color}; border: 1.5px solid {color};
                border-radius: 8px; padding: 7px 16px; font-size: 12px; font-weight: 600;
            }}
            QPushButton:hover {{ background: {color}12; }}
        """)
        b.clicked.connect(cb)
        return b
    def _refresh_task_detail(self, task_id: int, keep_scroll=False):
        task = get_task(task_id)
        if not task:
            return
        self._detail_task_id = task_id
        self._detail_task_id_label.setText(f"#{task['id']}")
        self._detail_status_tag.setText(_tag_text(task["status"]))
        self._detail_status_tag.setStyleSheet(_tag_style(task["status"]))
        self._detail_time_label.setText(task["created_at"][:16] if task["created_at"] else "")
        script = get_script_by_id(task["script_id"])
        _dn = script["name"] if script and script["name"] else ""
        _dnt = script["narration"] if script and script["narration"] else ""
        if _dn and _dn != "批量生成":
            narration = f"📄 {_dn}"
        else:
            narration = _dnt.split(chr(10) + "---" + chr(10))[0] if _dnt else ""
        self._detail_narration.setText(narration[:200] + ("..." if len(narration) > 200 else ""))
        self._detail_progress.setRange(0, task["total"])
        self._detail_progress.setValue(task["done"])
        self._detail_progress.setFormat(f"{task['done']}/{task['total']} ({int(task['done']/task['total']*100) if task['total']>0 else 0}%)")
        cfg = json.loads(task["config"]) if isinstance(task["config"], str) else task["config"]
        parts = []
        parts.append(f"📦 {len(get_all_materials())} 素材")
        if cfg.get("enable_bgm"): parts.append("🎵 BGM")
        parts.append(f"⚡ {cfg.get('fps',30)}fps")
        parts.append(f"🧵 {cfg.get('workers',2)}并发")
        self._detail_params.setText(" · ".join(parts))
        try:
            from autokat.core.quality import summarize_task_quality, technical_report_text
            quality = summarize_task_quality(task_id)
            deep_status = (
                "未执行" if quality["deep_status"] == "unavailable"
                else quality["deep_status"]
            )
            self._detail_quality.setText(
                f"质量摘要：通过 {quality['passed']} · 自动修复 {quality['auto_fixed']} · "
                f"阻断 {quality['failed']} · 深度验收 {deep_status}"
            )
            self._detail_report.setPlainText(technical_report_text(task_id))
            self._detail_report_toggle.setVisible(True)
        except Exception:
            self._detail_quality.setText("质量摘要：未执行新版验收")
            self._detail_report.setPlainText("尚无新版质量与性能技术报告。")
        # 按钮可用性
        status = task["status"]
        self._detail_btn_resume.setVisible(status in ("pending",))
        self._detail_btn_retry.setVisible(status in ("failed",))
        self._detail_btn_replay.setVisible(True)
        self._detail_btn_delete.setVisible(True)
        self._detail_btn_open.setVisible(True)
        # 成片列表
        if not keep_scroll:
            self._detail_clips_list.clear()
            clips = get_clips_by_task(task_id)
            for c in clips:
                icon = {"done": "✅", "rendering": "🔄", "pending": "⏳", "failed": "❌"}.get(c["status"], "⏳")
                name = Path(c["output_path"]).name if c["output_path"] else f"clip_{c['idx']:04d}"
                # 时长列：从 DB 读 duration_seconds（ffprobe 写回），M:SS 格式
                dur = c.get("duration_seconds")
                if dur:
                    m, s = int(dur // 60), int(dur % 60)
                    dur_str = f"{m}:{s:02d}"
                else:
                    dur_str = "—:—"
                text = f"{icon}  {name}  [{dur_str} · {c['status']}]"
                if c["error_msg"]:
                    text += f"  ❌ {c['error_msg']}"
                item = QListWidgetItem(text)
                self._detail_clips_list.addItem(item)
    def _on_detail_resume(self):
        if hasattr(self, "_detail_task_id"):
            self._resume_task(self._detail_task_id)
            QTimer.singleShot(2000, lambda: self._refresh_task_detail(self._detail_task_id))
    def _on_detail_retry(self):
        if hasattr(self, "_detail_task_id"):
            self._retry_task(self._detail_task_id)
            QTimer.singleShot(2000, lambda: self._refresh_task_detail(self._detail_task_id))
    def _on_detail_replay(self):
        if hasattr(self, "_detail_task_id"):
            self._replay_task(self._detail_task_id)

    def _on_detail_view(self):
        """任务详情页 "查看配置" 入口：只读模式打开向导"""
        if hasattr(self, "_detail_task_id") and self._detail_task_id:
            self._enter_wizard_for(self._detail_task_id, mode="view")

    def _on_detail_fork(self):
        """任务详情页 "基于此新建" 入口：可编辑模式打开向导"""
        if hasattr(self, "_detail_task_id") and self._detail_task_id:
            self._enter_wizard_for(self._detail_task_id, mode="fork")
    def _on_detail_open_dir(self):
        """任务详情页「输出目录」：打开该任务实际的输出子目录（不是顶层 output/）"""
        if not hasattr(self, "_detail_task_id"):
            return
        task = get_task(self._detail_task_id)
        if not task:
            return
        from pathlib import Path as _P_opd
        clips = get_clips_by_task(self._detail_task_id)
        target = None
        for c in clips:
            if c.get("output_path"):
                cand = _P_opd(c["output_path"]).parent
                if cand.exists():
                    target = cand
                    break
        if not target:
            out_dir = _P_opd(task["output_dir"]) if task.get("output_dir") else BASE_DIR / "output"
            target = out_dir if out_dir.exists() else BASE_DIR / "output"
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        self._open_dir(target)
    def _on_detail_delete(self):
        if not hasattr(self, "_detail_task_id"):
            return
        resp = QMessageBox.question(self, "确认删除",
            f"确定删除任务 #{self._detail_task_id}？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No)
        if resp == QMessageBox.Yes:
            delete_task(self._detail_task_id)
            self._log(f"任务 #{self._detail_task_id} 已删除")
            self._show_page(self.PAGE_DASHBOARD)
            self._refresh_sidebar()
            self._refresh_dashboard()
    # ══════════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════════
    def _update_wiz_selection(self):
        """Update selection count and next button"""
        count = 0
        total = 0
        for i in range(self._wiz_material_list.count()):
            item = self._wiz_material_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable:
                total += 1
                if item.checkState() == Qt.Checked:
                    count += 1
        self._wiz_sel_count.setText(f"selected {count}/{total}" if total > 0 else "")
        self._wiz_step1_next.setEnabled(count > 0)
    def _material_preview(self, item):
        """Preview material: images in dialog, videos in system player"""
        from autokat.models.db import get_material
        mid = item.data(Qt.UserRole)
        mat = get_material(mid)
        if not mat:
            return
        fp = mat["file_path"]
        if mat["mat_type"] == "image":
            self._show_image_preview(fp)
        elif mat["mat_type"] == "video":
            import subprocess
            try:
                subprocess.Popen(["open", fp])
            except Exception as e:
                self._log(f"playback failed: {e}")
    def _show_image_preview(self, path):
        """Show image in a dialog"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel
        from PySide6.QtGui import QPixmap
        dlg = QDialog(self)
        dlg.setWindowTitle("Image Preview")
        dlg.setMinimumSize(640, 720)
        layout = QVBoxLayout(dlg)
        pix = QPixmap(path)
        if pix.isNull():
            layout.addWidget(QLabel(f"Cannot load: {path}"))
        else:
            label = QLabel()
            scaled = pix.scaled(590, 660, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setPixmap(scaled)
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
        dlg.exec()
    def _log(self, msg):
        ts = datetime.now().strftime("%Y%m%d %H:%M:%S")
        log_text = f"[{ts}] {msg}"
        print(log_text)
        if hasattr(self, "_wiz_log") and self._wiz_log:
            self._wiz_log.append(log_text)
    @staticmethod
    def _open_dir(subdir_or_path):
        """打开目录：接受 "output" 这种子目录名，或直接给一个 Path 对象（任务详情传具体 task 输出目录）"""
        from pathlib import Path as _P_open
        if isinstance(subdir_or_path, _P_open):
            p = subdir_or_path
        elif "/" in str(subdir_or_path) or str(subdir_or_path).startswith("."):
            p = _P_open(str(subdir_or_path))
        else:
            p = BASE_DIR / subdir_or_path
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(p)])
    def _on_bgm_preview(self):
        """试听 BGM"""
        if getattr(self, '_wiz_bgm_playing', False):
            try:
                import signal as _sig
                if hasattr(self, '_bgm_proc'):
                    self._bgm_proc.terminate()
                    self._bgm_proc.wait(timeout=2)
            except Exception:
                pass
            self._wiz_bgm_playing = False
            self._wiz_bgm_preview.setText("▶ 试听")
            return
        path = self._wiz_bgm_combo.currentData()
        if not path:
            QMessageBox.warning(self, "提示", "请先选择要试听的 BGM")
            return
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "提示", "BGM 文件不存在")
            return
        try:
            import subprocess
            self._bgm_proc = subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._wiz_bgm_playing = True
            self._wiz_bgm_preview.setText("⏹ 停止")
        except Exception as e:
            QMessageBox.warning(self, "提示", f"播放失败: {e}")
    def _open_bgm_folder(self):
        p = BASE_DIR / "assets" / "bgm"
        p.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(p)])
    def _show_bgm_manager(self):
        """BGM 管理对话框：列出本地 BGM、试听、随机选"""
        from autokat.core.bgm import get_bgm_files, get_bgm_duration, pick_random_bgm
        dlg = QDialog(self)
        dlg.setWindowTitle("🎵 BGM 管理")
        dlg.resize(560, 480)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        tip = QLabel("BGM 文件存放在 assets/bgm/ 目录，混剪时会从这里随机选一首。")
        tip.setStyleSheet("color:#6B7280; font-size:12px; background:transparent;")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        listw = QListWidget()
        bgm_dir = BASE_DIR / "assets" / "bgm"
        bgm_dir.mkdir(parents=True, exist_ok=True)
        files = get_bgm_files()
        for f in files:
            try:
                dur = get_bgm_duration(f) or 0
                m, s = int(dur // 60), int(dur % 60)
                item = QListWidgetItem(f"🎵  {Path(f).name}  ({m}:{s:02d})")
            except Exception:
                item = QListWidgetItem(f"🎵  {Path(f).name}")
            item.setData(Qt.UserRole, f)
            listw.addItem(item)
        if not files:
            empty = QListWidgetItem("暂无 BGM，可把 mp3 放到 assets/bgm/ 目录")
            empty.setFlags(Qt.NoItemFlags)
            listw.addItem(empty)
        listw.setStyleSheet("""
            QListWidget {background: #FCFCFD; border: 1.5px solid #E5E7EB; border-radius: 10px; padding: 6px; font-size: 13px;}
            QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #F3F4F6; border-radius: 6px; margin: 1px; }
            QListWidget::item:hover { background: #EFF6FF; }
        """)
        layout.addWidget(listw, 1)
        def play_selected():
            item = listw.currentItem()
            if not item or not item.data(Qt.UserRole):
                return
            path = item.data(Qt.UserRole)
            try:
                import subprocess as _sp_bgm
                if hasattr(self, "_bgm_proc") and self._bgm_proc.poll() is None:
                    self._bgm_proc.terminate()
                self._bgm_proc = _sp_bgm.Popen(["afplay", str(path)])
            except Exception as e:
                QMessageBox.warning(self, "提示", f"试听失败: {e}")
        def stop_play():
            if hasattr(self, "_bgm_proc") and self._bgm_proc.poll() is None:
                self._bgm_proc.terminate()
        def pick_random_bgm_action():
            path = pick_random_bgm()
            if path:
                QMessageBox.information(self, "随机选 BGM", "随机选到:\n" + Path(path).name)
            else:
                QMessageBox.warning(self, "提示", "BGM 库为空")
        def add_bgm_files():
            files, _ = QFileDialog.getOpenFileNames(
                dlg, "选择 BGM 文件", "",
                "音频文件 (*.mp3 *.wav *.m4a *.flac *.ogg);;所有文件 (*)"
            )
            if not files:
                return
            imported = 0
            for src in files:
                src_path = Path(src)
                dst = bgm_dir / src_path.name
                if dst.exists():
                    dst = bgm_dir / f"{src_path.stem}_copy{src_path.suffix}"
                try:
                    import shutil as _shutil
                    _shutil.copy(src, dst)
                    imported += 1
                except Exception as e:
                    QMessageBox.warning(dlg, "导入失败", f"无法导入 {src_path.name}:\n{e}")
            if imported:
                QMessageBox.information(dlg, "导入成功", f"已导入 {imported} 个 BGM 文件")
                listw.clear()
                files = get_bgm_files()
                for f in files:
                    try:
                        dur = get_bgm_duration(f) or 0
                        m, s = int(dur // 60), int(dur % 60)
                        item = QListWidgetItem(f"🎵  {Path(f).name}  ({m}:{s:02d})")
                    except Exception:
                        item = QListWidgetItem(f"🎵  {Path(f).name}")
                    item.setData(Qt.UserRole, f)
                    listw.addItem(item)
                if not files:
                    empty = QListWidgetItem("暂无 BGM，可点击添加按钮导入")
                    empty.setFlags(Qt.NoItemFlags)
                    listw.addItem(empty)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        add_btn = QPushButton("➕ 添加")
        add_btn.setMinimumHeight(34)
        add_btn.clicked.connect(add_bgm_files)
        play_btn = QPushButton("▶ 试听")
        play_btn.setMinimumHeight(34)
        play_btn.clicked.connect(play_selected)
        stop_btn = QPushButton("⏹ 停止")
        stop_btn.setMinimumHeight(34)
        stop_btn.clicked.connect(stop_play)
        rand_btn = QPushButton("🎲 随机选")
        rand_btn.setMinimumHeight(34)
        rand_btn.clicked.connect(pick_random_bgm_action)
        for b in (add_btn, play_btn, stop_btn, rand_btn):
            b.setStyleSheet("QPushButton{background:#FFFFFF;color:#111827;border:1.5px solid #E5E7EB;border-radius:8px;padding:4px 14px;font-size:13px;font-weight:600;}QPushButton:hover{background:#F9FAFB;}")
            btn_row.addWidget(b)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        close_btn = QPushButton("关闭")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(dlg.accept)
        close_btn.setStyleSheet("QPushButton{background:#2563EB;color:#ffffff;border:none;border-radius:8px;padding:4px 18px;font-size:13px;font-weight:600;}QPushButton:hover{background:#1D4ED8;}")
        layout.addWidget(close_btn, 0, Qt.AlignRight)
        dlg.exec()
    def _show_material_manager(self):
        """素材池管理：查看、打标、删除、导入、标签管理"""
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
            QPushButton, QLabel, QListWidgetItem, QFileDialog,
            QMessageBox)
        from PySide6.QtCore import Qt
        from autokat.models.db import get_all_materials, get_conn
        from autokat.core.material import import_files, clear_material_pool_cache
        from pathlib import Path
        import json
        dlg = QDialog(self)
        dlg.setWindowTitle("\U0001f4e6 素材池管理")
        dlg.setMinimumSize(640, 520)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        header = QLabel("\U0001f4e6 素材池管理")
        header.setStyleSheet("font-size:18px; font-weight:800; color:#111827; background:transparent;")
        layout.addWidget(header)
        stats = QLabel("")
        stats.setStyleSheet("color:#6B7280; font-size:12px; font-weight:500; background:transparent;")
        layout.addWidget(stats)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        import_btn = QPushButton("\U0001f4c2 导入素材")
        import_btn.setStyleSheet(
            "QPushButton { background: #2563EB; color: #ffffff; font-weight: 600;"
            " border: none; border-radius: 10px; padding: 8px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #1D4ED8; }"
        )
        btn_row.addWidget(import_btn)
        pause_import_btn = QPushButton("暂停导入")
        retry_import_btn = QPushButton("重试失败")
        pause_import_btn.setEnabled(False)
        retry_import_btn.setEnabled(False)
        btn_row.addWidget(pause_import_btn)
        btn_row.addWidget(retry_import_btn)
        refresh_btn = QPushButton("\U0001f504 刷新")
        refresh_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #2563EB; font-weight: 500;"
            " border: 1.5px solid #E5E7EB; border-radius: 10px; padding: 8px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #FCFCFD; }"
        )
        btn_row.addWidget(refresh_btn)
        # 标签管理
        tag_mgr_btn = QPushButton("\U0001f3f7️  标签管理")
        tag_mgr_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #7C3AED; font-weight: 500;"
            " border: 1.5px solid #E5E7EB; border-radius: 10px; padding: 8px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #FCFCFD; }"
        )
        tag_mgr_btn.clicked.connect(self._show_tag_manager)
        btn_row.addWidget(tag_mgr_btn)
        # 打标签
        tag_edit_btn = QPushButton("\U0001f3f7️  打标签")
        tag_edit_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #2563EB; font-weight: 600;"
            " border: 1.5px solid #E5E7EB; border-radius: 10px; padding: 8px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #FCFCFD; }"
        )
        btn_row.addWidget(tag_edit_btn)
        # 删除
        del_btn = QPushButton("\U0001f5d1️  删除")
        del_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; color: #DC2626; font-weight: 600;"
            " border: 1.5px solid #E5E7EB; border-radius: 10px; padding: 8px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #FEF2F2; }"
        )
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        # 导入进度行
        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        import_progress = QProgressBar()
        import_progress.setVisible(False)
        import_progress.setMinimumHeight(10)
        import_progress.setTextVisible(True)
        import_progress.setFormat("%v/%m  (%p%)")
        progress_row.addWidget(import_progress, 1)
        import_status = QLabel("点击「📂 导入素材」开始批量导入")
        import_status.setStyleSheet("font-size:12px; color:#6B7280; background:transparent; font-weight:500;")
        progress_row.addWidget(import_status)
        layout.addLayout(progress_row)
        latest_import_job = {"id": None}
        try:
            from autokat.core.import_jobs import ImportJobService
            jobs = ImportJobService().list_jobs()
            if jobs:
                latest = jobs[0]
                latest_import_job["id"] = int(latest["id"])
                import_progress.setRange(0, max(1, int(latest["total"])))
                import_progress.setValue(int(latest["done"]))
                import_progress.setVisible(True)
                import_status.setText(
                    f"后台导入任务 #{latest['id']}：{latest['done']}/{latest['total']} · "
                    f"{latest['status']}，离开页面或重启应用后可继续"
                )
                pause_import_btn.setEnabled(latest["status"] in ("queued", "running"))
                retry_import_btn.setEnabled(latest["status"] == "failed")
        except Exception:
            pass
        # 素材列表（自定义 widget：复选框 + 文字区橡皮筋框选）
        mat_list = _RubberBandCheckListWidget()
        mat_list.setStyleSheet(
            "QListWidget { background: #FFFFFF; color: #111827; border: 1.5px solid #E5E7EB;"
            " border-radius: 12px; padding: 4px; }"
            "QListWidget::item { padding: 10px 12px; border-radius: 8px; margin: 1px; }"
            "QListWidget::item:hover { background: #FCFCFD; }"
        )
        mat_list._ensure_indicator_style()
        layout.addWidget(mat_list, 1)
        def _refresh():
            mat_list.clear()
            materials = get_all_materials()
            conn = get_conn()
            analysis_by_id = {
                row["material_id"]: dict(row)
                for row in conn.execute("SELECT * FROM material_analysis").fetchall()
            }
            conn.close()
            types = {}
            for m in materials:
                types[m["mat_type"]] = types.get(m["mat_type"], 0) + 1
                disp = m.get("display_name") or Path(m["file_path"]).stem
                text = "[{t:5s}] {name}  ({dur:.1f}s)".format(
                    t=m["mat_type"], name=disp, dur=m["duration"])
                tags = json.loads(m["tags"] or "[]")
                if tags:
                    text += "  tags: [" + ", ".join(str(t) for t in tags[:5])
                    if len(tags) > 5:
                        text += f" +{len(tags) - 5}"
                    text += "]"
                analysis = analysis_by_id.get(m.get("clip_parent") or m["id"])
                if analysis:
                    if analysis["status"] == "done":
                        text += (
                            f"  · 能力: {analysis['capability_summary']}"
                            f"  · 质量 {float(analysis['quality_score'] or 0):.2f}"
                        )
                    else:
                        text += f"  · 分析: {analysis['status']}"
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, m["id"])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                mat_list.addItem(item)
            parts = ["共 {n} 个素材".format(n=len(materials))]
            for t, c in types.items():
                parts.append("{t}: {c}".format(t=t, c=c))
            stats.setText(" | ".join(parts) + "（右键删除）")
        def _delete_material(mid):
            resp = QMessageBox.question(dlg, "确认删除",
                "确定删除素材 #{id}？\n\n"
                "如该素材是从视频拆出的父素材,会一并删除其所有子镜头。".format(id=mid),
                QMessageBox.Yes | QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
            from autokat.models.db import delete_material_cascade
            n_children = delete_material_cascade(mid)
            clear_material_pool_cache()
            _refresh()
            if n_children > 0:
                QMessageBox.information(dlg, "完成",
                    "已删除素材 #{id} 及其 {n} 个子镜头".format(id=mid, n=n_children))
        def _on_context(pos):
            item = mat_list.itemAt(pos)
            if not item:
                return
            mid = item.data(Qt.UserRole)
            menu = QMenu()
            del_action = menu.addAction(
                "\U0001f5d1  删除素材 #{id}".format(id=mid))
            action = menu.exec(mat_list.viewport().mapToGlobal(pos))
            if action == del_action:
                _delete_material(mid)
        def collect_checked():
            ids = []
            for i in range(mat_list.count()):
                it = mat_list.item(i)
                if it.checkState() == Qt.Checked:
                    mid = it.data(Qt.UserRole)
                    if mid:
                        ids.append(mid)
            return ids
        def batch_delete():
            ids = collect_checked()
            if not ids:
                QMessageBox.information(dlg, "提示", "请先勾选要删除的素材")
                return
            resp = QMessageBox.question(dlg, "确认删除",
                f"确认删除 {len(ids)} 个素材？\n\n"
                "如含从视频拆出的父素材,会一并删除其子素材,文件也会一并删除。",
                QMessageBox.Yes | QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
            from autokat.models.db import delete_material_cascade
            total_children = 0
            for mid in ids:
                total_children += delete_material_cascade(mid)
            clear_material_pool_cache()
            _refresh()
            msg = f"已删除 {len(ids)} 个素材"
            if total_children:
                msg += f"（含 {total_children} 个子素材）"
            QMessageBox.information(dlg, "完成", msg)
        def batch_tag():
            ids = collect_checked()
            if not ids:
                QMessageBox.information(dlg, "提示", "请先勾选要打标的素材")
                return
            self._show_tag_editor(ids)
            _refresh()
        del_btn.clicked.connect(batch_delete)
        tag_edit_btn.clicked.connect(batch_tag)
        def _import():
            files, _ = QFileDialog.getOpenFileNames(dlg, "选择素材文件", "",
                "素材文件 (*.jpg *.jpeg *.png *.webp *.mp4 *.mov *.avi *.mkv);;所有文件 (*)")
            if not files:
                return
            from autokat.core.import_jobs import ImportJobService
            latest_import_job["id"] = ImportJobService().create_job(files)
            import_btn.setEnabled(False)
            import_btn.setText("导入中…")
            import_progress.setRange(0, len(files))
            import_progress.setValue(0)
            import_progress.setVisible(True)
            import_status.setText(f"准备导入 {len(files)} 个文件…")

            class _ImportWorker(QThread):
                progress = Signal(int, int, str, str)  # current, total, filename, status
                finished_ok = Signal(object)            # stats dict
                failed = Signal(str)
                def run(self):
                    from autokat.core.import_jobs import ImportJobService
                    try:
                        def _cb(cur, total, fn, st):
                            self.progress.emit(cur, total, fn, st)
                        stats_obj = ImportJobService().process_job(
                            int(latest_import_job["id"]), on_progress=_cb,
                        )
                        self.finished_ok.emit(stats_obj)
                    except Exception as e:
                        self.failed.emit(str(e))

            worker = _ImportWorker()
            dlg._import_worker = worker  # 防止 GC

            def _on_progress(cur, total, fn, st):
                import_progress.setValue(cur)
                # 截断过长的文件名,避免撑爆状态行
                disp = Path(fn).name
                if len(disp) > 30:
                    disp = disp[:27] + "…"
                import_status.setText(f"[{cur}/{total}] {disp}")

            def _on_done(stats_obj):
                import_progress.setValue(import_progress.maximum())
                n_ok = stats_obj.get("added", 0) if isinstance(stats_obj, dict) else 0
                n_skip = stats_obj.get("skipped", 0) if isinstance(stats_obj, dict) else 0
                import_status.setText(f"完成:新增 {n_ok},跳过 {n_skip}")
                import_btn.setEnabled(True)
                import_btn.setText("📂 导入素材")
                pause_import_btn.setEnabled(False)
                retry_import_btn.setEnabled(bool(stats_obj.get("errors")))
                clear_material_pool_cache()
                _refresh()
                # 3 秒后自动隐藏进度条,恢复初始提示
                QTimer.singleShot(3000, lambda: (import_progress.setVisible(False),
                                                  import_status.setText("点击「📂 导入素材」开始批量导入")))

            def _on_fail(msg):
                import_status.setText(f"导入失败:{msg}")
                import_btn.setEnabled(True)
                import_btn.setText("📂 导入素材")

            worker.progress.connect(_on_progress)
            worker.finished_ok.connect(_on_done)
            worker.failed.connect(_on_fail)
            worker.start()
        import_btn.clicked.connect(_import)
        def _pause_import():
            if latest_import_job["id"] is None:
                return
            from autokat.core.import_jobs import ImportJobService
            ImportJobService().pause_job(int(latest_import_job["id"]))
            pause_import_btn.setEnabled(False)
            import_status.setText(
                f"导入任务 #{latest_import_job['id']} 已请求暂停，当前文件完成后生效"
            )

        def _retry_import():
            if latest_import_job["id"] is None:
                return
            from autokat.core.import_jobs import ImportJobService
            service = ImportJobService()
            service.retry_failed(int(latest_import_job["id"]))
            retry_import_btn.setEnabled(False)
            pause_import_btn.setEnabled(True)
            import_status.setText(f"导入任务 #{latest_import_job['id']} 正在重试失败项")
            threading.Thread(
                target=service.process_job, args=(int(latest_import_job["id"]),), daemon=True,
            ).start()

        pause_import_btn.clicked.connect(_pause_import)
        retry_import_btn.clicked.connect(_retry_import)
        refresh_btn.clicked.connect(_refresh)
        mat_list.setContextMenuPolicy(Qt.CustomContextMenu)
        mat_list.customContextMenuRequested.connect(_on_context)
        _refresh()
        dlg.exec()
    # ── 标签管理对话框（CRUD + 颜色） ──
    def _show_tag_manager(self):
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
            QPushButton, QLabel, QListWidget, QListWidgetItem, QMessageBox,
            QInputDialog, QColorDialog)
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QBrush
        from autokat.models.db import (get_all_tags_with_usage, register_tag,
            update_tag, delete_tag)
        dlg = QDialog(self)
        dlg.setWindowTitle("🏷️  标签管理")
        dlg.setMinimumSize(480, 460)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)
        header = QLabel("🏷️  标签管理")
        header.setStyleSheet("font-size:18px; font-weight:800; color:#111827; background:transparent;")
        lay.addWidget(header)
        tip = QLabel("管理所有已注册的标签。重命名/删除会自动同步到素材。")
        tip.setStyleSheet("color:#6B7280; font-size:12px; background:transparent;")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        tag_list = QListWidget()
        tag_list.setStyleSheet(
            "QListWidget { background:#FFFFFF; color:#111827; border:1.5px solid #E5E7EB;"
            " border-radius:10px; padding:4px; }"
            "QListWidget::item { padding:10px 12px; border-radius:8px; margin:1px; }"
            "QListWidget::item:hover { background:#FCFCFD; }"
            "QListWidget::item:selected { background:#2563EB20; }"
        )
        lay.addWidget(tag_list, 1)
        def _refresh():
            tag_list.clear()
            for t in get_all_tags_with_usage():
                text = f"  ■  {t['name']}    使用 {t['usage']} 次"
                item = QListWidgetItem(text)
                item.setForeground(QBrush(QColor(t['color'])))
                item.setData(Qt.UserRole, t['id'])
                item.setData(Qt.UserRole + 1, t)
                tag_list.addItem(item)
        def _new_tag():
            name, ok = QInputDialog.getText(dlg, "新建标签", "标签名：")
            if not ok or not name.strip():
                return
            color = QColorDialog.getColor(QColor("#6B7280"), dlg, "选择颜色")
            if not color.isValid():
                color = QColor("#6B7280")
            register_tag(name.strip(), color.name())
            _refresh()
        def _rename_tag():
            item = tag_list.currentItem()
            if not item:
                return
            t = item.data(Qt.UserRole + 1)
            new_name, ok = QInputDialog.getText(dlg, "重命名", "新名称：", text=t['name'])
            if not ok or not new_name.strip():
                return
            if not update_tag(t['id'], name=new_name.strip()):
                QMessageBox.warning(dlg, "失败", "重名或无效")
                return
            _refresh()
        def _recolor_tag():
            item = tag_list.currentItem()
            if not item:
                return
            t = item.data(Qt.UserRole + 1)
            color = QColorDialog.getColor(QColor(t['color']), dlg, "选择颜色")
            if not color.isValid():
                return
            update_tag(t['id'], color=color.name())
            _refresh()
        def _delete_tag():
            item = tag_list.currentItem()
            if not item:
                return
            t = item.data(Qt.UserRole + 1)
            if t['usage'] > 0:
                resp = QMessageBox.question(dlg, "确认删除",
                    f"标签 「{t['name']}」 在 {t['usage']} 个素材中使用，删除后会自动移除。继续？",
                    QMessageBox.Yes | QMessageBox.No)
                if resp != QMessageBox.Yes:
                    return
            else:
                resp = QMessageBox.question(dlg, "确认删除",
                    f"删除标签 「{t['name']}」？",
                    QMessageBox.Yes | QMessageBox.No)
                if resp != QMessageBox.Yes:
                    return
            delete_tag(t['id'])
            _refresh()
        btn_row = QHBoxLayout()
        new_btn = QPushButton("➕  新建")
        new_btn.setStyleSheet("background:#2563EB; color:#fff; font-weight:600; border:none; border-radius:8px; padding:8px 14px;")
        rename_btn = QPushButton("✏️  重命名")
        rename_btn.setStyleSheet("background:#FFFFFF; color:#2563EB; font-weight:500; border:1.5px solid #E5E7EB; border-radius:8px; padding:8px 14px;")
        recolor_btn = QPushButton("🎨  改色")
        recolor_btn.setStyleSheet("background:#FFFFFF; color:#2563EB; font-weight:500; border:1.5px solid #E5E7EB; border-radius:8px; padding:8px 14px;")
        del_btn = QPushButton("🗑️  删除")
        del_btn.setStyleSheet("background:#FFFFFF; color:#DC2626; font-weight:500; border:1.5px solid #E5E7EB; border-radius:8px; padding:8px 14px;")
        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet("background:#FFFFFF; color:#374151; font-weight:500; border:1.5px solid #E5E7EB; border-radius:8px; padding:8px 14px;")
        new_btn.clicked.connect(_new_tag)
        rename_btn.clicked.connect(_rename_tag)
        recolor_btn.clicked.connect(_recolor_tag)
        del_btn.clicked.connect(_delete_tag)
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(new_btn)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(recolor_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        _refresh()
        dlg.exec()
    # ── 打标对话框（批量给多个素材加/去 tag） ──
    def _show_tag_editor(self, material_ids):
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
            QPushButton, QLabel, QLineEdit, QMessageBox, QWidget)
        from PySide6.QtCore import Qt
        from autokat.models.db import (get_all_tags_with_usage, register_tag,
            apply_tags_to_materials, get_materials_by_ids)
        import json as _json

        dlg = QDialog(self)
        dlg.setWindowTitle(f"🏷️  给 {len(material_ids)} 个素材打标签")
        dlg.setMinimumSize(580, 540)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)

        header = QLabel(f"🏷️  给 {len(material_ids)} 个素材打标签")
        header.setStyleSheet("font-size:18px; font-weight:800; color:#111827; background:transparent;")
        lay.addWidget(header)

        tip = QLabel("点击芯片切换「保留 / 移除 / 新增」状态。底部输入新名字后按 ➕ 自动注册并应用。")
        tip.setStyleSheet("color:#6B7280; font-size:12px; background:transparent;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 当前素材已用的 tag 集合
        mats = get_materials_by_ids(material_ids)
        current_tags = set()
        for m in mats:
            try:
                for tn in _json.loads(m.get("tags") or "[]"):
                    current_tags.add(tn)
            except Exception:
                pass

        # state: name -> "active"(会保存) / "inactive"(不保存)
        #   - 现有标签: active 表示保留,inactive 表示移除
        #   - 新加标签: active 表示添加,inactive 表示不添加
        # 这样 2 态切换:点一下变红/灰(不保存),再点一下回来(保存)
        state: dict[str, str] = {t: "active" for t in current_tags}
        chip_buttons: dict[str, object] = {}

        # 预览行
        preview_row = QHBoxLayout()
        preview_row.setSpacing(8)
        preview_label = QLabel("")
        preview_label.setStyleSheet(
            "color:#111827; font-size:12px; background:transparent; font-weight:500;"
        )
        preview_label.setWordWrap(True)
        preview_row.addWidget(preview_label, 1)
        lay.addLayout(preview_row)

        # chip 区
        chips_container = QWidget(dlg)
        chips_layout = QHBoxLayout(chips_container)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)

        def _chip_style(color: str, is_active: bool, is_new: bool) -> str:
            if is_active:
                # 实心:会保存(保留 / 添加)
                return (f"QPushButton {{ background:{color}; color:#FFFFFF; font-weight:600;"
                        f" border:none; border-radius:14px; padding:6px 12px; font-size:12px; }}")
            # 不保存:移除 = 灰+删除线;新标签不加 = 白底虚线
            if is_new:
                return (f"QPushButton {{ background:#FFFFFF; color:#9CA3AF; font-weight:500;"
                        f" border:1.5px dashed #D1D5DB; border-radius:14px; padding:5px 11px;"
                        f" font-size:12px; }}")
            return (f"QPushButton {{ background:#F3F4F6; color:#9CA3AF; font-weight:500;"
                    f" border:none; border-radius:14px; padding:6px 12px; font-size:12px;"
                    f" text-decoration:line-through; }}")

        def _update_preview():
            to_add = sorted(n for n, s in state.items() if s == "active" and n not in current_tags)
            to_remove = sorted(n for n, s in state.items() if s == "inactive" and n in current_tags)
            parts = []
            if to_add:
                parts.append(f"<span style='color:#10B981;font-weight:600;'>＋ 添加 {len(to_add)} 个</span>: {', '.join(to_add)}")
            if to_remove:
                parts.append(f"<span style='color:#DC2626;font-weight:600;'>－ 移除 {len(to_remove)} 个</span>: {', '.join(to_remove)}")
            if not parts:
                preview_label.setText("⚠  当前没有变更,直接关闭将不保存")
                preview_label.setStyleSheet(
                    "color:#6B7280; font-size:12px; background:transparent; font-style:italic;"
                )
            else:
                preview_label.setText("  ·  ".join(parts))
                preview_label.setStyleSheet(
                    "color:#111827; font-size:12px; background:transparent; font-weight:500;"
                )

        def _build_chips():
            for w in list(chip_buttons.values()):
                w.setParent(None); w.deleteLater()
            chip_buttons.clear()
            # 先清空布局里所有项(除了 stretch)
            while chips_layout.count():
                it = chips_layout.takeAt(0)
                w = it.widget() if it else None
                if w is not None:
                    w.deleteLater()
            tags = get_all_tags_with_usage()
            all_names = set(t["name"] for t in tags) | set(state.keys())
            if not all_names:
                empty = QLabel("（暂无标签，底部输入新建）")
                empty.setStyleSheet("color:#9CA3AF; font-size:12px;")
                chips_layout.addWidget(empty)
                chips_layout.addStretch()
            else:
                for name in sorted(all_names):
                    meta = next((t for t in tags if t["name"] == name), None)
                    color = meta["color"] if meta else "#6B7280"
                    is_new = name not in current_tags
                    btn = QPushButton(name)
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setStyleSheet(_chip_style(color, state.get(name, "active") == "active", is_new))
                    btn.clicked.connect(lambda _, n=name: _toggle_chip(n))
                    chip_buttons[name] = btn
                    chips_layout.addWidget(btn)
                chips_layout.addStretch()
            _update_preview()

        def _toggle_chip(name: str):
            state[name] = "inactive" if state.get(name, "active") == "active" else "active"
            btn = chip_buttons.get(name)
            if btn is not None:
                meta = next((t for t in get_all_tags_with_usage() if t["name"] == name), None)
                color = meta["color"] if meta else "#6B7280"
                is_new = name not in current_tags
                btn.setStyleSheet(_chip_style(color, state[name] == "active", is_new))
            _update_preview()

        new_input = QLineEdit()
        new_input.setPlaceholderText("输入新标签名，回车或点 ➕ 添加")
        new_input.setStyleSheet("QLineEdit { padding:8px 12px; border:1.5px solid #E5E7EB; border-radius:8px; font-size:13px; }")
        new_btn = QPushButton("➕")
        new_btn.setStyleSheet("background:#2563EB; color:#fff; font-weight:700; border:none; border-radius:8px; padding:8px 14px;")
        new_input_row = QHBoxLayout()
        new_input_row.addWidget(new_input, 1)
        new_input_row.addWidget(new_btn)
        lay.addWidget(chips_container)
        lay.addLayout(new_input_row)

        def _do_new():
            name = new_input.text().strip()
            if not name:
                return
            register_tag(name)
            # 新加的 tag 默认 active(就是要添加)
            if name not in state:
                state[name] = "active"
            new_input.clear()
            _build_chips()
        new_btn.clicked.connect(_do_new)
        new_input.returnPressed.connect(_do_new)

        bottom = QHBoxLayout()
        bottom.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet("background:#FFFFFF; color:#374151; border:1.5px solid #E5E7EB; border-radius:8px; padding:8px 18px;")
        apply_btn = QPushButton("✅  应用")
        apply_btn.setStyleSheet("background:#10B981; color:#fff; font-weight:700; border:none; border-radius:8px; padding:8px 18px;")
        cancel_btn.clicked.connect(dlg.reject)
        apply_btn.clicked.connect(dlg.accept)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(apply_btn)
        lay.addLayout(bottom)

        _build_chips()

        if dlg.exec() != QDialog.Accepted:
            return
        to_add = [n for n, s in state.items() if s == "active" and n not in current_tags]
        to_remove = [n for n, s in state.items() if s == "inactive" and n in current_tags]
        if not to_add and not to_remove:
            QMessageBox.information(self, "未变更", "没有添加或移除任何标签,列表未变化。")
            return
        n = apply_tags_to_materials(material_ids, to_add, to_remove)
        QMessageBox.information(self, "完成", f"已更新 {n} 个素材的标签")

    def _show_settings(self):
        """设置对话框：显示/修改输出目录"""
        dlg = QDialog(self)
        dlg.setWindowTitle("⚙️ 设置")
        dlg.resize(500, 300)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(16)

        # 输出目录
        out_group = QGroupBox("输出目录")
        out_layout = QVBoxLayout(out_group)
        cur_out = _get_output_dir()
        out_row = QHBoxLayout()
        out_label = QLabel(f"当前: {cur_out}")
        out_label.setStyleSheet("color:#374151; font-size:12px; background:transparent;")
        out_row.addWidget(out_label)
        out_layout.addLayout(out_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        def open_output_dir():
            path = BASE_DIR / "output"
            import subprocess
            subprocess.run(["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)])

        def change_output_dir():
            folder = QFileDialog.getExistingDirectory(dlg, "选择输出目录", cur_out)
            if folder:
                _set_output_dir(folder)
                out_label.setText(f"当前: {folder}")
                QMessageBox.information(dlg, "设置", f"输出目录已修改为:\n{folder}")

        open_btn = QPushButton("📂 打开目录")
        open_btn.setMinimumHeight(34)
        open_btn.clicked.connect(open_output_dir)
        open_btn.setStyleSheet("QPushButton{background:#FFFFFF;color:#2563EB;border:1.5px solid #2563EB;border-radius:8px;padding:6px 16px;font-size:13px;font-weight:600;}QPushButton:hover{background:#EFF6FF;}")
        change_btn = QPushButton("🔄 修改目录")
        change_btn.setMinimumHeight(34)
        change_btn.clicked.connect(change_output_dir)
        change_btn.setStyleSheet("QPushButton{background:#2563EB;color:#ffffff;border:none;border-radius:8px;padding:6px 16px;font-size:13px;font-weight:600;}QPushButton:hover{background:#1D4ED8;}")
        btn_row.addWidget(open_btn)
        btn_row.addWidget(change_btn)
        btn_row.addStretch()
        out_layout.addLayout(btn_row)
        layout.addWidget(out_group)

        # 素材目录
        mat_group = QGroupBox("素材目录")
        mat_layout = QVBoxLayout(mat_group)
        mat_label = QLabel(f"assets/")
        mat_label.setStyleSheet("color:#374151; font-size:12px; background:transparent;")
        mat_layout.addWidget(mat_label)
        layout.addWidget(mat_group)

        # BGM目录
        bgm_group = QGroupBox("BGM 目录")
        bgm_layout = QVBoxLayout(bgm_group)
        bgm_label = QLabel(f"assets/bgm/")
        bgm_label.setStyleSheet("color:#374151; font-size:12px; background:transparent;")
        bgm_layout.addWidget(bgm_label)
        layout.addWidget(bgm_group)

        # 数据库
        db_group = QGroupBox("数据库")
        db_layout = QVBoxLayout(db_group)
        db_label = QLabel(f"tasks/autokat.db")
        db_label.setStyleSheet("color:#374151; font-size:12px; background:transparent;")
        db_layout.addWidget(db_label)
        layout.addWidget(db_group)

        layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(dlg.accept)
        close_btn.setStyleSheet("QPushButton{background:#FFFFFF;color:#6B7280;border:1.5px solid #E5E7EB;border-radius:8px;padding:6px 18px;font-size:13px;font-weight:600;}QPushButton:hover{background:#F9FAFB;}")
        layout.addWidget(close_btn, 0, Qt.AlignRight)
        dlg.exec()

    def _do_dedup(self, threshold=0.85):
        output_dir = BASE_DIR / "output"
        removed = dedup_output_dir(str(output_dir), threshold)
        self._log(f"去重完成,删除 {removed} 个")
# ══════════════════════════════════════════════════════════════
# 启动入口
# ══════════════════════════════════════════════════════════════
def run_ui():
    """启动 GUI 界面"""
    warnings = []
    
    # 检查 FFmpeg
    try:
        from autokat.core import ffmpeg_utils as _fu
        r = subprocess.run([_fu.FFMPEG, "-version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            warnings.append("FFmpeg not available, please install ffmpeg")
    except Exception:
        warnings.append("FFmpeg not found, rendering will not work")
    
    # 检查数据库
    try:
        init_db()
    except Exception as e:
        warnings.append(f"Database migration failed; original database was preserved: {e}")
    
    ensure_dirs()
    # Persistent imports continue independently of the visible page.
    try:
        from autokat.core.import_jobs import resume_import_jobs
        threading.Thread(target=resume_import_jobs, daemon=True).start()
        from autokat.core.material_analysis import analyze_pending_materials
        threading.Thread(target=analyze_pending_materials, daemon=True).start()
    except Exception as exc:
        warnings.append(f"Import job resume failed: {exc}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app_icon_path = os.environ.get("AUTOKAT_APP_ICON")
    if app_icon_path and Path(app_icon_path).exists():
        app.setWindowIcon(QIcon(app_icon_path))
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    # 全局异常捕获
    def _excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_path = Path.home() / "Library" / "Logs" / "AutoCat_crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(msg)
        try:
            QMessageBox.critical(None, "AutoCat Error", str(exc_value))
        except Exception:
            print(f"[AutoCat Crash] {exc_value}")
    sys.excepthook = _excepthook
    # 应用样式表
    app.setStyleSheet(_STYLE)
    window = MainWindow()
    if warnings:
        QTimer.singleShot(500, lambda: QMessageBox.warning(window, "Environment Check", "\n".join(warnings)))
    window.show()
    sys.exit(app.exec())
if __name__ == "__main__":
    run_ui()
