#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CachyOS Update History
======================

A small, dependency-light Qt6 desktop app that shows your package update
history from several sources in one place:

  * Pacman / official repositories  (parsed from /var/log/pacman.log)
  * AUR                             (detected via `pacman -Qmq`, i.e. foreign
                                     packages currently installed)
  * Flatpak                         (real history from `flatpak history`
                                     --system and --user, plus a guaranteed
                                     entry per installed app via `flatpak list`)

Features
--------
  * Colored badges for both the source and the action (theme-aware: works on
    light and dark KDE/Breeze themes)
  * Filter by text, by source and by action; all combine
  * Activity chart (packages per day) that follows the active filter
  * Sortable columns (click a header)
  * CSV export of the current view
  * Language switch (English / Deutsch)

Dependencies
------------
The only runtime dependency is PyQt6. On Arch-based systems (CachyOS, Arch,
EndeavourOS, ...) install it from the official repositories:

    sudo pacman -S python-pyqt6

On other distributions, use a virtual environment:

    python -m venv .venv && source .venv/bin/activate
    pip install PyQt6

Run
---
    python cachy-update-history.py

Note on AUR detection
---------------------
The pacman log does not record which repository a package came from. An entry
is therefore flagged as "AUR" when the package is *currently* installed as a
foreign package (`pacman -Qmq`). Consequently, AUR packages that have since
been removed, or packages that later moved into the official repositories,
will show up as "Pacman". This is the most reliable heuristic available
without an external database.
"""

import sys
import os
import re
import csv
import glob
import json
import shutil
import subprocess
from collections import OrderedDict, Counter
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableView, QHeaderView, QPushButton, QLineEdit, QComboBox,
    QLabel, QStyledItemDelegate, QStyle, QFileDialog, QMessageBox,
    QAbstractItemView,
)
from PyQt6.QtGui import QColor, QPainter, QFont, QIcon, QPalette
from PyQt6.QtCore import (
    Qt, QRectF, QSize, QTimer, QAbstractTableModel, QModelIndex,
    QSortFilterProxyModel,
)

LOG_PATH = "/var/log/pacman.log"
ACCENT = "#3DAEE9"

# ─── Localisation ──────────────────────────────────────────────────────────
_LANG = "en"
_LANG_NAMES = {"en": "English", "de": "Deutsch"}
_CONFIG_PATH = os.path.expanduser("~/.config/cachy-update-history.json")


def _load_config():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(data):
    # Write atomically (temp file + rename) so an interrupted write can never
    # leave a truncated, unparseable config behind.
    tmp = _CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _CONFIG_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass

_T = {
    "en": {
        "window_title":    "CachyOS Update History",
        "title_label":     "CachyOS Update History",
        "search_ph":       "Filter packages (e.g. linux, nvidia, firefox) …",
        "all_sources":     "All sources",
        "all_actions":     "All actions",
        "reload":          "Reload",
        "export_csv":      "Export CSV",
        "headers":         ["Date & Time", "Source", "Action", "Package",
                            "Old version", "New version"],
        "chart_title":     "Update activity — packages per day",
        "chart_no_data":   "No data for the current selection",
        "stats_total":     "Total",
        "status_entries":  "entries",
        "status_show":     "showing",
        "status_of":       "of",
        "flatpak_missing": "(flatpak not installed)",
        "flatpak_empty":   "(no Flatpak apps found)",
        "export_dlg":      "Export",
        "export_nothing":  "Nothing to export.",
        "export_fail":     "Export failed",
        "save_as_csv":     "Save as CSV",
        "exported":        "{n} entries exported to {path}",
        "file_not_found":  "File not found: {path}",
        "perm_denied":     "Permission denied",
        "cannot_read":     "Cannot read {path}.",
        "src_pacman":      "Pacman",
        "src_aur":         "AUR",
        "src_flatpak":     "Flatpak",
        "act_upgraded":    "Upgraded",
        "act_installed":   "Installed",
        "act_removed":     "Removed",
        "act_reinstalled": "Reinstalled",
        "act_downgraded":  "Downgraded",
    },
    "de": {
        "window_title":    "CachyOS Update-Verlauf",
        "title_label":     "CachyOS Update-Verlauf",
        "search_ph":       "Pakete filtern (z. B. linux, nvidia, firefox) …",
        "all_sources":     "Alle Quellen",
        "all_actions":     "Alle Aktionen",
        "reload":          "Neu laden",
        "export_csv":      "CSV exportieren",
        "headers":         ["Datum & Uhrzeit", "Quelle", "Aktion", "Paket",
                            "Alte Version", "Neue Version"],
        "chart_title":     "Update-Aktivität — Pakete pro Tag",
        "chart_no_data":   "Keine Daten für die aktuelle Auswahl",
        "stats_total":     "Gesamt",
        "status_entries":  "Einträge",
        "status_show":     "Zeige",
        "status_of":       "von",
        "flatpak_missing": "(Flatpak nicht installiert)",
        "flatpak_empty":   "(keine Flatpak-Apps gefunden)",
        "export_dlg":      "Export",
        "export_nothing":  "Nichts zu exportieren.",
        "export_fail":     "Export fehlgeschlagen",
        "save_as_csv":     "Als CSV speichern",
        "exported":        "{n} Einträge exportiert nach {path}",
        "file_not_found":  "Datei nicht gefunden: {path}",
        "perm_denied":     "Keine Leseberechtigung",
        "cannot_read":     "{path} kann nicht gelesen werden.",
        "src_pacman":      "Pacman",
        "src_aur":         "AUR",
        "src_flatpak":     "Flatpak",
        "act_upgraded":    "Aktualisiert",
        "act_installed":   "Installiert",
        "act_removed":     "Entfernt",
        "act_reinstalled": "Neu installiert",
        "act_downgraded":  "Herabgestuft",
    },
}


def tr(key):
    return _T[_LANG].get(key, _T["en"].get(key, key))


# Sources. The order here drives the order in the statistics.
# Each value is the badge color.
SOURCES = OrderedDict([
    ("pacman",  "#1793D1"),  # Arch blue
    ("aur",     "#9C27B0"),  # magenta
    ("flatpak", "#26A69A"),  # teal
])

# Actions. Keys match the verbs used in the pacman log and flatpak history.
ACTIONS = OrderedDict([
    ("upgraded",    "#3DAEE9"),
    ("installed",   "#27AE60"),
    ("removed",     "#DA4453"),
    ("reinstalled", "#9B59B6"),
    ("downgraded",  "#F67400"),
])

# Matches lines such as:
#   [2024-06-20T22:20:15+0200] [ALPM] upgraded firefox (1.0-1 -> 2.0-1)
#   [2023-01-02 09:15]         [ALPM] installed gcc (12.2.1-3)
ACTION_PATTERN = re.compile(
    r"\[(?P<ts>[^\]]+)\] \[ALPM\] "
    r"(?P<action>upgraded|downgraded|installed|reinstalled|removed) "
    r"(?P<pkg>\S+) \((?P<ver>[^)]*)\)"
)


def parse_timestamp(raw):
    """Parse a timestamp string into a datetime.

    Handles the modern ISO pacman format, the older pacman format and the
    formats produced by `flatpak history`. Returns None if nothing matches.
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # flatpak history emits "Jun 20 10:57:01" (LC_ALL=C, no year).
    # Collapse repeated spaces ("Jun  3 …" for single-digit days), then try
    # month-name formats and attach the current year, rolling back one year if
    # the resulting datetime would be in the future.
    normalized = re.sub(r' +', ' ', raw)
    for fmt in ("%b %d %H:%M:%S", "%b %d %H:%M"):
        try:
            dt = datetime.strptime(normalized, fmt)
            now = datetime.now()
            dt = dt.replace(year=now.year)
            if dt > now:
                dt = dt.replace(year=now.year - 1)
            return dt
        except ValueError:
            continue
    return None


def split_version(action, ver):
    """Split the version field into (old version, new version).

    Upgrades/downgrades use the "old -> new" form; installs and removals only
    carry a single version, which is placed in the appropriate column.
    """
    if " -> " in ver:
        old, new = ver.split(" -> ", 1)
        return old.strip(), new.strip()
    if action == "removed":
        return ver.strip(), "—"
    return "—", ver.strip()


def _csv_safe(value):
    """Neutralise CSV formula injection for spreadsheet apps.

    Package names and versions come from logs we do not control. A cell that
    starts with =, +, -, @ (or a control char) is interpreted as a formula by
    Excel / LibreOffice, which can be abused to run code. Prefixing such a value
    with a single quote forces it to be treated as plain text.
    """
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def get_foreign_packages():
    """Return the set of foreign (AUR / manually installed) package names.

    Uses `pacman -Qmq`. Returns an empty set if pacman is unavailable.
    """
    try:
        out = subprocess.run(["pacman", "-Qmq"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0:
            return set(out.stdout.split())
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return set()


def _run_flatpak(args):
    """Run a flatpak subcommand with a stable (C) locale; return stdout or ""."""
    if not shutil.which("flatpak"):
        return ""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        out = subprocess.run(["flatpak", *args], capture_output=True,
                             text=True, timeout=20, env=env)
        if out.returncode == 0:
            return out.stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return ""


# Where flatpak stores deployed apps. The directory mtime of an app's "active"
# deploy gives a good "last installed/updated" timestamp without relying on the
# fragile `flatpak history` command.
FLATPAK_BASE_DIRS = [
    "/var/lib/flatpak",                              # system-wide installs
    os.path.expanduser("~/.local/share/flatpak"),    # per-user installs
]


def _flatpak_deploy_mtime(appid):
    """Best-effort 'last updated' time for an installed Flatpak app.

    Reads the mtime of the app's active deploy symlink. Returns None if the
    deploy directory cannot be located (e.g. unusual custom installation).
    """
    newest = 0.0
    for base in FLATPAK_BASE_DIRS:
        appdir = os.path.join(base, "app", appid)
        # Layout: <base>/app/<id>/<arch>/<branch>/active
        for active in glob.glob(os.path.join(appdir, "*", "*", "active")):
            try:
                newest = max(newest, os.lstat(active).st_mtime)
            except OSError:
                pass
        if newest == 0.0 and os.path.isdir(appdir):
            try:
                newest = max(newest, os.stat(appdir).st_mtime)
            except OSError:
                pass
    return newest or None


def _split_columns(line):
    """Split one row of flatpak table output into trimmed fields.

    flatpak separates columns with a TAB when output is not a terminal (our
    case, via subprocess) and with padded spaces otherwise. Handle both.
    """
    if "\t" in line:
        parts = line.split("\t")
    else:
        parts = re.split(r"\s{2,}", line.strip())
    return [p.strip() for p in parts if p.strip()]


def read_flatpak_installed():
    """Return one "installed" record per installed Flatpak app.

    `flatpak list` shows BOTH user and system installs by default and never
    fails the way `flatpak history` can, so it guarantees every installed app
    is visible. Only application,version are requested (version last) so an
    empty field can never shift the columns.
    """
    text = _run_flatpak(["list", "--app", "--columns=application,version"])
    installed = []
    for line in text.splitlines():
        parts = _split_columns(line)
        # Real application IDs are reverse-DNS (contain a dot); skips the header.
        if not parts or "." not in parts[0]:
            continue
        app = parts[0]
        version = parts[1] if len(parts) > 1 else ""
        mtime = _flatpak_deploy_mtime(app)
        installed.append({
            "dt": datetime.fromtimestamp(mtime) if mtime else None,
            "action_key": "installed",
            "pkg": app,
            "old": "—",
            "new": version or "—",
            "source_key": "flatpak",
        })
    return installed


def _flatpak_change_action(change):
    """Map a `flatpak history` change value to one of our action keys."""
    c = change.lower()
    if "uninstall" in c or "remove" in c:
        return "removed"
    if "update" in c:
        return "upgraded"
    if "install" in c:              # checked after uninstall on purpose
        return "installed"
    return None


def parse_flatpak_history_text(text):
    """Parse one `flatpak history` table (columns: time, change, application).

    `application` is requested as the LAST column so nothing can merge into it.
    We therefore read it as the final field and treat everything between time
    and application as the change value (which may contain a space, e.g.
    "deploy install").
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_columns(line)
        if len(fields) < 3:
            continue
        time_s = fields[0]
        app = fields[-1]
        change_s = " ".join(fields[1:-1])
        if "." not in app:          # skip header / non-app rows
            continue
        action = _flatpak_change_action(change_s)
        if action is None:
            continue
        rows.append({
            "dt": parse_timestamp(time_s),
            "action_key": action,
            "pkg": app,
            "old": "—",
            "new": "—",
            "source_key": "flatpak",
        })
    return rows


def read_flatpak_history():
    """Read the real update history from BOTH the system and user installations.

    `flatpak history` defaults to a single installation, so each scope is
    queried explicitly and the results are combined. `application` is the last
    requested column for robust parsing (see parse_flatpak_history_text).
    """
    rows = []
    for scope in ("--system", "--user"):
        text = _run_flatpak(
            ["history", scope, "--columns=time,change,application"])
        rows.extend(parse_flatpak_history_text(text))
    return rows


def read_flatpak_all():
    """Combine the real history with a guaranteed entry per installed app.

    Returns history entries (system + user) plus, for any installed app that
    has no history entry, a single "installed" row dated from its deploy mtime.
    This way the full timeline is shown where available, while every currently
    installed Flatpak is guaranteed to appear even if its history is empty.

    History is filtered to installed app IDs only — `flatpak history` also
    includes runtimes and locale extensions that are not user-facing apps.
    """
    if not shutil.which("flatpak"):
        return []
    installed = read_flatpak_installed()
    app_ids = {rec["pkg"] for rec in installed}
    history = [r for r in read_flatpak_history() if r["pkg"] in app_ids]
    seen = {r["pkg"] for r in history}
    extra = [rec for rec in installed if rec["pkg"] not in seen]
    return history + extra


# Custom item-data roles used by the model below.
COLOR_ROLE = Qt.ItemDataRole.UserRole + 1   # badge color for source/action cells
SORT_ROLE  = Qt.ItemDataRole.UserRole + 2   # value used by the sort proxy


class UpdateTableModel(QAbstractTableModel):
    """Model holding the update records.

    Using a model/view (instead of QTableWidget) is what keeps the UI fast:
    the view only renders the handful of currently visible rows, so filtering
    a 50 k-entry history is just a model reset — no per-cell widget creation.
    """
    # Column -> key in the record dict for the display text.
    _COLS = ("display", "source_label", "action_label", "pkg", "old", "new")

    def __init__(self):
        super().__init__()
        self.rows = []
        self._bold = QFont()
        self._bold.setBold(True)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        # Called constantly by the view; avoid building a translated list here.
        return len(self._COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr("headers")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return row[self._COLS[col]]
        if role == COLOR_ROLE:                     # badge color (source/action)
            if col == 1:
                return row["source_color"]
            if col == 2:
                return row["action_color"]
        if role == SORT_ROLE:                      # sort key for the proxy
            if col == 0:
                return row["sort_ts"]              # numeric -> chronological
            return str(row[self._COLS[col]]).lower()
        if role == Qt.ItemDataRole.FontRole and col == 3:
            return self._bold
        return None


class UpdateFilterProxy(QSortFilterProxyModel):
    """Sort + filter proxy.

    Filtering happens here (lazily, no model reset), which is what keeps the UI
    responsive even on large logs. `accepts()` is also reused by the window to
    recompute the stats and chart for the current filter.
    """
    def __init__(self):
        super().__init__()
        self.setSortRole(SORT_ROLE)
        self._text = ""
        self._source_key = None
        self._action_key = None

    def set_filters(self, text, source_key, action_key):
        self._text = text.lower().strip()
        self._source_key = source_key
        self._action_key = action_key
        self.invalidateFilter()

    def accepts(self, rec):
        if self._source_key and rec["source_key"] != self._source_key:
            return False
        if self._action_key and rec["action_key"] != self._action_key:
            return False
        if self._text:
            hay = f"{rec['pkg']} {rec['source_label']} {rec['action_label']}".lower()
            if self._text not in hay:
                return False
        return True

    def filterAcceptsRow(self, source_row, source_parent):
        return self.accepts(self.sourceModel().rows[source_row])


class BadgeDelegate(QStyledItemDelegate):
    """Paints the cell text as a rounded, colored "pill" badge.

    The badge color is read from the cell's COLOR_ROLE data. A semi-transparent
    fill keeps it legible on both light and dark themes.
    """
    def paint(self, painter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = QColor(index.data(COLOR_ROLE) or "#888888")

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = option.fontMetrics
        text_w = fm.horizontalAdvance(text)
        pad_h = 12
        pill_w = min(text_w + 2 * pad_h, option.rect.width() - 8)
        pill_h = min(fm.height() + 6, option.rect.height() - 8)
        x = option.rect.left() + 6
        y = option.rect.center().y() - pill_h / 2
        pill = QRectF(x, y, pill_w, pill_h)

        bg = QColor(color)
        bg.setAlpha(48)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(pill, pill_h / 2, pill_h / 2)

        painter.setPen(color)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width() + 28, max(s.height(), 34))


class ActivityChart(QWidget):
    """A simple bar chart of packages-per-day, drawn with QPainter.

    Kept deliberately dependency-free (no matplotlib / QtCharts) so the only
    requirement remains PyQt6.
    """
    def __init__(self):
        super().__init__()
        self.data = []  # list of (iso_date, count)
        self.setMinimumHeight(170)

    def set_data(self, data):
        self.data = data
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text_color = pal.color(QPalette.ColorRole.WindowText)

        # Card background
        card = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pal.color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(card, 8, 8)

        inner = card.adjusted(14, 12, -14, -12)

        # Title
        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(inner.left(), inner.top(), inner.width(), 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            tr("chart_title"),
        )
        painter.setFont(self.font())

        if not self.data:
            faded = QColor(text_color)
            faded.setAlpha(130)
            painter.setPen(faded)
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter,
                             tr("chart_no_data"))
            painter.end()
            return

        top_pad = 28
        bottom_pad = 26
        chart_top = inner.top() + top_pad
        chart_bottom = inner.bottom() - bottom_pad
        chart_h = chart_bottom - chart_top
        chart_left = inner.left()
        chart_w = inner.width()

        n = len(self.data)
        max_count = max(c for _, c in self.data) or 1
        gap = max(2.0, min(8.0, chart_w / n * 0.25))
        bar_w = max(2.0, (chart_w - gap * (n - 1)) / n)

        bar_color = QColor(ACCENT)
        label_color = QColor(text_color)
        label_color.setAlpha(150)
        small_font = QFont(self.font())
        small_font.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))

        # Bars (and a count label on top when there is room)
        for i, (day, count) in enumerate(self.data):
            bar_h = chart_h * count / max_count
            x = chart_left + i * (bar_w + gap)
            y = chart_bottom - bar_h
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bar_color)
            radius = min(3.0, bar_w / 2)
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), radius, radius)
            if bar_w >= 22:
                painter.setFont(small_font)
                painter.setPen(text_color)
                painter.drawText(QRectF(x - 4, y - 16, bar_w + 8, 14),
                                 Qt.AlignmentFlag.AlignCenter, str(count))

        # X-axis labels: first, middle and last day
        painter.setFont(small_font)
        painter.setPen(label_color)

        def fmt_day(iso):
            try:
                return datetime.strptime(iso, "%Y-%m-%d").strftime("%m-%d")
            except ValueError:
                return iso

        for i in sorted(set([0, n // 2, n - 1])):
            x = chart_left + i * (bar_w + gap)
            painter.drawText(QRectF(x - 22, chart_bottom + 4, bar_w + 44, 18),
                             Qt.AlignmentFlag.AlignCenter, fmt_day(self.data[i][0]))
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self, log_path=LOG_PATH):
        super().__init__()
        self.log_path = log_path
        self.all_data = []
        self._entry_count = 0
        self._src_counts = {}
        self._flatpak_available = False
        self._has_flatpak_rows = False

        self.setWindowTitle(tr("window_title"))
        self.setWindowIcon(QIcon.fromTheme("system-software-update"))
        self.resize(1040, 740)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.title_label = QLabel(tr("title_label"))
        tf = QFont(self.font())
        tf.setPointSizeF(self.font().pointSizeF() + 4)
        tf.setBold(True)
        self.title_label.setFont(tf)
        root.addWidget(self.title_label)

        # --- Toolbar -------------------------------------------------------
        controls = QHBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(tr("search_ph"))
        self.search_input.setClearButtonEnabled(True)
        # Debounce: recompute stats/chart at most every 200 ms while typing.
        self._filter_timer = QTimer(self, singleShot=True, interval=200)
        self._filter_timer.timeout.connect(self.apply_filters)
        self.search_input.textChanged.connect(self._filter_timer.start)

        self.source_combo = QComboBox()
        self.source_combo.addItem(tr("all_sources"), None)
        for key, _color in SOURCES.items():
            self.source_combo.addItem(tr(f"src_{key}"), key)
        self.source_combo.currentIndexChanged.connect(self.apply_filters)

        self.action_combo = QComboBox()
        self.action_combo.addItem(tr("all_actions"), None)
        for key, _color in ACTIONS.items():
            self.action_combo.addItem(tr(f"act_{key}"), key)
        self.action_combo.currentIndexChanged.connect(self.apply_filters)

        self.refresh_btn = QPushButton(tr("reload"))
        self.refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.refresh_btn.clicked.connect(self.load_data)

        self.export_btn = QPushButton(tr("export_csv"))
        self.export_btn.setIcon(QIcon.fromTheme("document-save"))
        self.export_btn.clicked.connect(self.export_csv)

        self.lang_btn = QPushButton(_LANG_NAMES[_LANG])
        self.lang_btn.setIcon(QIcon.fromTheme("preferences-desktop-locale"))
        self.lang_btn.clicked.connect(self.switch_language)

        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.source_combo)
        controls.addWidget(self.action_combo)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.export_btn)
        controls.addWidget(self.lang_btn)
        root.addLayout(controls)

        # --- Statistics line (rich text) -----------------------------------
        self.stats_label = QLabel()
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.stats_label.setWordWrap(True)
        root.addWidget(self.stats_label)

        # --- Activity chart ------------------------------------------------
        self.chart = ActivityChart()
        root.addWidget(self.chart)

        # --- Table (model/view for speed) ----------------------------------
        self.model = UpdateTableModel()
        self.proxy = UpdateFilterProxy()
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        badge = BadgeDelegate(self.table)
        self.table.setItemDelegateForColumn(1, badge)  # Source
        self.table.setItemDelegateForColumn(2, badge)  # Action
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)  # newest first

        # Fixed/interactive widths only — no per-row "resize to contents",
        # which would scan every row on each refresh and stall large logs.
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # Package
        for col, width in ((0, 140), (1, 90), (2, 130), (4, 150), (5, 150)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        root.addWidget(self.table, 1)

        self.status = self.statusBar()
        self.load_data()

    # ----- Language switch --------------------------------------------------
    def switch_language(self):
        global _LANG
        _LANG = "de" if _LANG == "en" else "en"
        _save_config({"lang": _LANG})
        self._retranslate_rows()
        self._retranslate_ui()
        self.apply_filters()

    def _retranslate_rows(self):
        """Update translated display labels in all loaded records."""
        for row in self.all_data:
            row["action_label"] = tr(f"act_{row['action_key']}")
            row["source_label"] = tr(f"src_{row['source_key']}")
        self.model.set_rows(self.all_data)

    def _retranslate_ui(self):
        """Update all static UI strings to the active language."""
        self.setWindowTitle(tr("window_title"))
        self.title_label.setText(tr("title_label"))
        self.search_input.setPlaceholderText(tr("search_ph"))
        self.lang_btn.setText(_LANG_NAMES[_LANG])
        self.refresh_btn.setText(tr("reload"))
        self.export_btn.setText(tr("export_csv"))

        src_key = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem(tr("all_sources"), None)
        for key, _color in SOURCES.items():
            self.source_combo.addItem(tr(f"src_{key}"), key)
        self.source_combo.setCurrentIndex(max(0, self.source_combo.findData(src_key)))
        self.source_combo.blockSignals(False)

        act_key = self.action_combo.currentData()
        self.action_combo.blockSignals(True)
        self.action_combo.clear()
        self.action_combo.addItem(tr("all_actions"), None)
        for key, _color in ACTIONS.items():
            self.action_combo.addItem(tr(f"act_{key}"), key)
        self.action_combo.setCurrentIndex(max(0, self.action_combo.findData(act_key)))
        self.action_combo.blockSignals(False)

        self.chart.update()  # repaint chart title

    # ----- Data loading -----------------------------------------------------
    def load_data(self):
        """Read all sources, normalise the records and refresh the views."""
        data = []

        # Pacman + AUR (AUR is inferred from the set of foreign packages)
        foreign = get_foreign_packages()
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                # Stream line by line instead of slurping the whole log into
                # memory — pacman.log can grow to many MB over a system's life.
                for line in f:
                    m = ACTION_PATTERN.search(line)
                    if not m:
                        continue
                    action = m.group("action")
                    pkg = m.group("pkg")
                    old, new = split_version(action, m.group("ver"))
                    source_key = "aur" if pkg in foreign else "pacman"
                    data.append({
                        "dt": parse_timestamp(m.group("ts")),
                        "action_key": action,
                        "pkg": pkg,
                        "old": old,
                        "new": new,
                        "source_key": source_key,
                    })
        except FileNotFoundError:
            self.status.showMessage(tr("file_not_found").format(path=self.log_path))
        except PermissionError:
            QMessageBox.warning(self, tr("perm_denied"),
                                tr("cannot_read").format(path=self.log_path))

        # Flatpak: real history from system + user installations, plus a
        # guaranteed "installed" row for every app that has no history entry.
        flatpak_available = bool(shutil.which("flatpak"))
        flatpak_rows = read_flatpak_all()
        data.extend(flatpak_rows)

        # Enrich each record with display fields, then sort newest first
        for row in data:
            dt = row["dt"]
            row["sort_ts"] = dt.timestamp() if dt else 0.0
            row["display"] = dt.strftime("%Y-%m-%d  %H:%M") if dt else "—"
            row["action_label"] = tr(f"act_{row['action_key']}")
            row["action_color"] = ACTIONS.get(row["action_key"], "#888888")
            row["source_label"] = tr(f"src_{row['source_key']}")
            row["source_color"] = SOURCES.get(row["source_key"], "#888888")
        data.sort(key=lambda r: r["sort_ts"], reverse=True)
        self.all_data = data
        self.model.set_rows(data)

        counts = Counter(r["source_key"] for r in data)
        self._entry_count = len(data)
        self._src_counts = dict(counts)
        self._flatpak_available = flatpak_available
        self._has_flatpak_rows = bool(flatpak_rows)
        self.apply_filters()

    def _build_base_status(self):
        counts = self._src_counts
        parts = [f"{tr(f'src_{k}')}: {counts[k]}"
                 for k in SOURCES if counts.get(k)]
        s = (f"{self._entry_count} {tr('status_entries')} — "
             + ("  ".join(parts) if parts else "—"))
        if not self._flatpak_available:
            s += f"   {tr('flatpak_missing')}"
        elif not self._has_flatpak_rows:
            s += f"   {tr('flatpak_empty')}"
        return s

    def apply_filters(self):
        """Apply the current filter widgets and refresh table, stats and chart."""
        self.proxy.set_filters(
            self.search_input.text(),
            self.source_combo.currentData(),
            self.action_combo.currentData(),
        )
        filtered = [r for r in self.all_data if self.proxy.accepts(r)]
        self.update_stats(filtered)
        self.update_chart(filtered)

        shown, total = len(filtered), len(self.all_data)
        msg = self._build_base_status()
        if shown != total:
            msg += f"   —   {tr('status_show')} {shown} {tr('status_of')} {total}"
        self.status.showMessage(msg)

    def update_stats(self, data):
        total = len(data)
        src_counts = {k: 0 for k in SOURCES}
        act_counts = {k: 0 for k in ACTIONS}
        for row in data:
            src_counts[row["source_key"]] = src_counts.get(row["source_key"], 0) + 1
            act_counts[row["action_key"]] = act_counts.get(row["action_key"], 0) + 1

        def chips(counts, table, prefix):
            out = []
            for key, color in table.items():
                if counts.get(key, 0) > 0:
                    label = tr(f"{prefix}{key}")
                    out.append(f"<span style='color:{color};'>●</span>&nbsp;"
                               f"{label}:&nbsp;<b>{counts[key]}</b>")
            return "&nbsp;&nbsp;&nbsp;".join(out)

        sep = " &nbsp;&nbsp;|&nbsp;&nbsp; "
        html = f"<b>{tr('stats_total')}:</b>&nbsp;{total}"
        src_html = chips(src_counts, SOURCES, "src_")
        act_html = chips(act_counts, ACTIONS, "act_")
        if src_html:
            html += sep + src_html
        if act_html:
            html += sep + act_html
        self.stats_label.setText(html)

    def update_chart(self, data):
        buckets = {}
        for row in data:
            if row["dt"] is None:
                continue
            day = row["dt"].date().isoformat()
            buckets[day] = buckets.get(day, 0) + 1
        # Chronological order; show only the 30 most recent active days
        ordered = sorted(buckets.items(), key=lambda kv: kv[0])[-30:]
        self.chart.set_data(ordered)

    # ----- Export -----------------------------------------------------------
    def export_csv(self):
        """Export the current view (filtered + sorted) to a CSV file."""
        cols = self.model.columnCount()
        rows = []
        for r in range(self.proxy.rowCount()):
            rows.append([_csv_safe(self.proxy.index(r, c).data())
                         for c in range(cols)])
        if not rows:
            QMessageBox.information(self, tr("export_dlg"), tr("export_nothing"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("save_as_csv"), "cachyos-update-history.csv",
            "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(tr("headers"))
                writer.writerows(rows)
            self.status.showMessage(tr("exported").format(n=len(rows), path=path))
        except OSError as e:
            QMessageBox.warning(self, tr("export_fail"), str(e))


def main():
    global _LANG
    config = _load_config()
    if config.get("lang") in _T:
        _LANG = config["lang"]

    app = QApplication(sys.argv)
    app.setApplicationName("CachyOS Update History")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
