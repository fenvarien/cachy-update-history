#!/usr/bin/env python3
#
# CachyOS Update History — package update history for Pacman, AUR and Flatpak.
# Copyright (C) 2026 fenvarien
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
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
  * Theme-aware source/action badges and native palette/accent styling
  * Filter by text, by source and by action; all combine
  * Activity chart (packages per day) that follows the active filter
  * Sortable columns (click a header)
  * On-demand package details queried outside the GUI thread
  * CSV export of the current view
  * Language switch (English / Deutsch)

Dependencies
------------
Python 3.10 or newer is required. The only runtime dependency is PyQt6. On
Arch-based systems (CachyOS, Arch, EndeavourOS, ...) install it from the
official repositories:

    sudo pacman -S python-pyqt6

On other distributions, use a virtual environment:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

Run
---
    python3 cachy-update-history.py

Project documentation
---------------------
See README.md for the entry point, docs/USER_GUIDE.md for behavior and
troubleshooting, and docs/ARCHITECTURE.md for data flow, threading, security
boundaries and extension rules.

Note on AUR detection
---------------------
The pacman log does not record which repository a package came from. An entry
is therefore flagged as "AUR" when the package is *currently* installed as a
foreign package (`pacman -Qmq`). Consequently, AUR packages that have since
been removed, or packages that later moved into the official repositories,
will show up as "Pacman". This is the most reliable heuristic available
without an external database.
"""

import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from datetime import datetime
from html import escape as _esc

from PyQt6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QRectF,
    QRunnable,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

__version__ = "1.2.0"
LOG_PATH = "/var/log/pacman.log"
ACCENT = "#3DAEE9"

# ─── Localisation ──────────────────────────────────────────────────────────
_LANG = "en"
_LANG_NAMES = {"en": "English", "de": "Deutsch"}
_CONFIG_PATH = os.path.expanduser("~/.config/cachy-update-history.json")
_CONFIG_MAX_BYTES = 64 * 1024


def _load_config():
    try:
        with open(_CONFIG_PATH, "rb") as f:
            raw = f.read(_CONFIG_MAX_BYTES + 1)
        if len(raw) > _CONFIG_MAX_BYTES:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError,
            RecursionError):
        return {}


def _save_config(data):
    """Write the config atomically through a private, unpredictable file."""
    config_dir = os.path.dirname(_CONFIG_PATH)
    tmp = None
    try:
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".cachy-update-history-", suffix=".tmp", dir=config_dir,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _CONFIG_PATH)
        tmp = None
    except (OSError, TypeError):
        pass
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _update_config(**changes):
    """Merge ``changes`` into the on-disk config, preserving other keys."""
    data = _load_config()
    data.update(changes)
    _save_config(data)


# Runtime preferences (loaded from the config in main()).
_TOOLTIPS_ENABLED = True

_T = {
    "en": {
        "window_title":    "CachyOS Update History",
        "title_label":     "CachyOS Update History",
        "subtitle":        "Pacman · AUR · Flatpak — every package change on one timeline",
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
        "status_loading":  "Loading package history …",
        "load_failed":     "Package history could not be loaded: {error}",
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
        "tooltips_btn":    "Details",
        "tooltips_hint":   "Show detailed package information when hovering a row",
        "tt_change":       "Version change",
        "tt_description":  "Description",
        "tt_repository":   "Repository",
        "tt_version":      "Version",
        "tt_licenses":     "License",
        "tt_size":         "Installed size",
        "tt_install_date": "Install date",
        "tt_url":          "Website",
        "tt_origin":       "Origin",
        "tt_runtime":      "Runtime",
        "tt_loading":      "Loading package details …",
        "tt_not_installed": "Not currently installed — showing log data only.",
        "ctx_copy_pkg":    "Copy package name",
        "ctx_copy_ver":    "Copy version",
        "ctx_copy_row":    "Copy row",
        "ctx_filter_pkg":  "Filter by this package",
        "ctx_details":     "Package details …",
        "details_title":    "Package details",
        "details_copy_all": "Copy all details",
        "details_close":    "Close",
        "chart_hint":       "Click to filter by this day",
        "copied":           "Copied „{text}“ to clipboard",
        "copied_row":       "Copied row to clipboard",
        "copied_rows":      "Copied {n} packages to clipboard",
        "search_hint":      "Filter packages, versions or dates (Ctrl+F) …",
    },
    "de": {
        "window_title":    "CachyOS Update-Verlauf",
        "title_label":     "CachyOS Update-Verlauf",
        "subtitle":        "Pacman · AUR · Flatpak — alle Paketänderungen in einer Zeitleiste",
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
        "status_loading":  "Paketverlauf wird geladen …",
        "load_failed":     "Paketverlauf konnte nicht geladen werden: {error}",
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
        "tooltips_btn":    "Details",
        "tooltips_hint":   "Detaillierte Paketinfos beim Überfahren einer Zeile anzeigen",
        "tt_change":       "Versionswechsel",
        "tt_description":  "Beschreibung",
        "tt_repository":   "Repository",
        "tt_version":      "Version",
        "tt_licenses":     "Lizenz",
        "tt_size":         "Installierte Größe",
        "tt_install_date": "Installiert am",
        "tt_url":          "Webseite",
        "tt_origin":       "Quelle",
        "tt_runtime":      "Runtime",
        "tt_loading":      "Paketdetails werden geladen …",
        "tt_not_installed": "Derzeit nicht installiert — es werden nur Log-Daten angezeigt.",
        "ctx_copy_pkg":    "Paketname kopieren",
        "ctx_copy_ver":    "Version kopieren",
        "ctx_copy_row":    "Zeile kopieren",
        "ctx_filter_pkg":  "Nach diesem Paket filtern",
        "ctx_details":     "Paketdetails …",
        "details_title":    "Paketdetails",
        "details_copy_all": "Alle Details kopieren",
        "details_close":    "Schließen",
        "chart_hint":       "Klicken zum Filtern nach diesem Tag",
        "copied":           "„{text}“ in Zwischenablage kopiert",
        "copied_row":       "Zeile in Zwischenablage kopiert",
        "copied_rows":      "{n} Pakete in Zwischenablage kopiert",
        "search_hint":      "Pakete, Versionen oder Daten filtern (Strg+F) …",
    },
}


def tr(key, lang=None):
    lang = lang if lang in _T else _LANG
    return _T[lang].get(key, _T["en"].get(key, key))


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

# Flatpak IDs are used as path components when locating deployment metadata.
# Reject separators and glob metacharacters even if a compromised/malformed
# flatpak command produces output that violates Flatpak's own ID rules.
FLATPAK_APP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


# Yearless "Mon DD HH:MM[:SS]" timestamps from `flatpak history`.
_YEARLESS_TS_PATTERN = re.compile(
    r"(?P<mon>[A-Za-z]{3}) +(?P<day>\d{1,2}) +"
    r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?\Z"
)
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _valid_flatpak_app_id(appid):
    return (isinstance(appid, str) and "." in appid
            and FLATPAK_APP_ID_PATTERN.fullmatch(appid) is not None)


def _command_path(name):
    """Resolve a command once per invocation and return an absolute path."""
    path = shutil.which(name)
    return os.path.realpath(path) if path else None


def parse_timestamp(raw):
    """Parse a timestamp string into a datetime.

    Handles the modern ISO pacman format, the older pacman format and the
    formats produced by `flatpak history`. Returns None if nothing matches.
    """
    raw = raw.strip()
    # Fast path: ISO-8601 timestamps used by modern pacman logs (C-level
    # parser; Python 3.10 rejects the "+0200" offset and uses strptime below).
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # flatpak history emits "Jun 20 10:57:01" (LC_ALL=C, no year). Month names
    # are matched against a fixed English table instead of strptime's %b,
    # which follows LC_TIME — and QApplication switches that to the user's
    # locale, so e.g. "Mar"/"Oct" would fail on German systems. The year is
    # the current one, or the previous one if the date would lie in the future
    # or does not exist this year (Feb 29).
    m = _YEARLESS_TS_PATTERN.match(raw)
    if m is None:
        return None
    month = _MONTHS.get(m.group("mon").casefold())
    if month is None:
        return None
    now = datetime.now()
    for year in (now.year, now.year - 1):
        try:
            dt = datetime(year, month, int(m.group("day")), int(m.group("h")),
                          int(m.group("m")), int(m.group("s") or 0))
        except ValueError:
            continue
        if dt <= now:
            return dt
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
    if s and (s[0] in ("=", "+", "-", "@") or ord(s[0]) < 32):
        return "'" + s
    return s


def get_foreign_packages():
    """Return the set of foreign (AUR / manually installed) package names.

    Uses `pacman -Qmq`. Returns an empty set if pacman is unavailable.
    """
    pacman = _command_path("pacman")
    if pacman is None:
        return set()
    try:
        out = subprocess.run([pacman, "-Qmq"], capture_output=True,
                             text=True, timeout=10, check=False)
        if out.returncode == 0:
            return set(out.stdout.split())
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return set()


def _run_flatpak(args):
    """Run a flatpak subcommand with a stable (C) locale; return stdout or ""."""
    flatpak = _command_path("flatpak")
    if flatpak is None:
        return ""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        out = subprocess.run([flatpak, *args], capture_output=True,
                             text=True, timeout=20, env=env,
                             check=False)
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
    if not _valid_flatpak_app_id(appid):
        return None
    newest = 0.0
    for base in FLATPAK_BASE_DIRS:
        appdir = os.path.join(base, "app", appid)
        if not os.path.isdir(appdir):
            continue
        # Layout: <base>/app/<id>/<arch>/<branch>/active
        for active in glob.glob(os.path.join(appdir, "*", "*", "active")):
            try:
                newest = max(newest, os.lstat(active).st_mtime)
            except OSError:
                pass
        if newest == 0.0:
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
        # Real application IDs are reverse-DNS; this also skips the header and
        # prevents unsafe values from reaching filesystem path/glob handling.
        if not parts or not _valid_flatpak_app_id(parts[0]):
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
        if not _valid_flatpak_app_id(app):  # skip header / unsafe non-app rows
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
    if _command_path("flatpak") is None:
        return []
    installed = read_flatpak_installed()
    app_ids = {rec["pkg"] for rec in installed}
    history = [r for r in read_flatpak_history() if r["pkg"] in app_ids]
    seen = {r["pkg"] for r in history}
    extra = [rec for rec in installed if rec["pkg"] not in seen]
    return history + extra


def collect_update_data(log_path):
    """Collect raw records without touching Qt widgets.

    This function deliberately contains all potentially slow filesystem and
    subprocess work so it can run outside the GUI thread.
    """
    data = []
    log_error = None
    foreign = get_foreign_packages()
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[ALPM] " not in line:
                    continue
                m = ACTION_PATTERN.search(line)
                if not m:
                    continue
                action = m.group("action")
                pkg = m.group("pkg")
                old, new = split_version(action, m.group("ver"))
                data.append({
                    "dt": parse_timestamp(m.group("ts")),
                    "action_key": action,
                    "pkg": pkg,
                    "old": old,
                    "new": new,
                    "source_key": "aur" if pkg in foreign else "pacman",
                })
    except FileNotFoundError:
        log_error = ("file_not_found", log_path)
    except PermissionError:
        log_error = ("permission", log_path)
    except OSError:
        log_error = ("cannot_read", log_path)

    flatpak_available = _command_path("flatpak") is not None
    flatpak_rows = read_flatpak_all() if flatpak_available else []
    data.extend(flatpak_rows)
    return {
        "rows": data,
        "log_error": log_error,
        "flatpak_available": flatpak_available,
        "has_flatpak_rows": bool(flatpak_rows),
    }


# ─── Theme / palette-derived colors ─────────────────────────────────────────
# Every accent used by the custom-painted widgets and the rich-text tooltips is
# derived from the active QPalette at startup. Reading the system palette (and,
# crucially, its Highlight color) is what makes the app feel native on both KDE
# (Breeze) and GNOME (Adwaita) and adapt automatically to light/dark themes and
# the user's chosen accent color. Populated by apply_theme().
_UI = {
    "accent":  ACCENT,
    "muted":   "#888888",
    "base":    "#ffffff",
}


def _mix(a, b, t):
    """Linear blend between two QColors: t=0 → a, t=1 → b."""
    return QColor(
        round(a.red()   + (b.red()   - a.red())   * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def apply_theme(app):
    """Build and apply a palette-derived stylesheet for a modern, native look.

    Everything is computed from the running QPalette — most importantly the
    Highlight (accent) color — so the result matches the system theme on both
    KDE/Breeze and GNOME/Adwaita and adapts automatically to light and dark
    variants and the user's chosen accent. The computed colors are also stashed
    in ``_UI`` for the custom-painted widgets and rich-text tooltips.
    """
    pal = app.palette()
    Role = QPalette.ColorRole
    base   = pal.color(Role.Base)
    window = pal.color(Role.Window)
    text   = pal.color(Role.WindowText)
    accent = pal.color(Role.Highlight)
    hltext = pal.color(Role.HighlightedText)
    dark = base.lightness() < 128

    def h(c):
        return c.name()

    def rgba(c, a):
        return f"rgba({c.red()},{c.green()},{c.blue()},{a})"

    c = {
        "bg":            h(window),
        "base":          h(base),
        "text":          h(text),
        "accent":        h(accent),
        "accent_hover":  h(_mix(accent, QColor("#ffffff"), 0.16 if not dark else 0.12)),
        "accent_soft":   rgba(accent, 0.16),
        "hltext":        h(hltext),
        "border":        h(_mix(base, text, 0.20 if dark else 0.15)),
        "field":         h(base),
        "alt":           h(_mix(base, text, 0.05)),
        "header":        h(_mix(window, text, 0.05)),
        "muted":         h(_mix(base, text, 0.52)),
        "btn":           h(_mix(window, text, 0.07 if not dark else 0.11)),
        "btn_hover":     h(_mix(window, text, 0.13 if not dark else 0.18)),
        "btn_press":     h(_mix(window, text, 0.20 if not dark else 0.25)),
        "tooltip":       h(_mix(base, text, 0.06)),
        "sb":            h(_mix(base, text, 0.22)),
        "sb_hover":      h(_mix(base, text, 0.36)),
    }

    _UI.update(accent=c["accent"], muted=c["muted"], base=c["base"])

    # Re-applying an identical sheet would needlessly repolish every widget
    # (and can re-trigger palette change events). Return whether it changed.
    sheet = _STYLESHEET.format(**c)
    if sheet == app.styleSheet():
        return False
    app.setStyleSheet(sheet)
    return True


_STYLESHEET = """
QWidget#central {{ background:{bg}; }}
QLabel#subtitle {{ color:{muted}; }}

QLineEdit {{
    background:{field}; border:1px solid {border}; border-radius:9px;
    padding:7px 12px; color:{text};
    selection-background-color:{accent}; selection-color:{hltext};
}}
QLineEdit:focus {{ border:1px solid {accent}; }}

QComboBox {{
    background:{field}; border:1px solid {border}; border-radius:9px;
    padding:6px 12px; color:{text}; min-width:118px;
}}
QComboBox:hover {{ border:1px solid {accent}; }}
QComboBox::drop-down {{
    border:none; width:22px;
    subcontrol-origin:padding; subcontrol-position:center right; right:4px;
}}
QComboBox QAbstractItemView {{
    background:{field}; border:1px solid {border}; border-radius:8px;
    padding:4px; outline:none;
    selection-background-color:{accent}; selection-color:{hltext};
}}

QPushButton {{
    background:{btn}; border:1px solid {border}; border-radius:9px;
    padding:7px 15px; color:{text};
}}
QPushButton:hover {{ background:{btn_hover}; border:1px solid {accent}; }}
QPushButton:pressed {{ background:{btn_press}; }}

QPushButton#primary {{
    background:{accent}; border:1px solid {accent}; color:{hltext};
    font-weight:bold;
}}
QPushButton#primary:hover {{
    background:{accent_hover}; border:1px solid {accent_hover};
}}
QPushButton#primary:pressed {{ background:{accent}; }}

QPushButton#toggle:checked {{
    background:{accent_soft}; border:1px solid {accent}; color:{accent};
    font-weight:bold;
}}

QTableView {{
    background:{base}; alternate-background-color:{alt};
    border:1px solid {border}; border-radius:12px;
    gridline-color:transparent; outline:none; padding:3px;
    selection-background-color:{accent}; selection-color:{hltext};
}}
QTableView::item {{ padding:4px 8px; border:none; }}

QHeaderView {{ background:transparent; }}
QHeaderView::section {{
    background:{header}; color:{muted}; border:none;
    border-bottom:1px solid {border}; padding:9px 8px; font-weight:bold;
}}
QHeaderView::section:hover {{ color:{accent}; }}
QTableCornerButton::section {{ background:{header}; border:none; }}

QScrollBar:vertical {{ background:transparent; width:12px; margin:2px; }}
QScrollBar::handle:vertical {{
    background:{sb}; border-radius:5px; min-height:34px;
}}
QScrollBar::handle:vertical:hover {{ background:{sb_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QScrollBar:horizontal {{ background:transparent; height:12px; margin:2px; }}
QScrollBar::handle:horizontal {{
    background:{sb}; border-radius:5px; min-width:34px;
}}
QScrollBar::handle:horizontal:hover {{ background:{sb_hover}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}

QToolTip {{
    background:{tooltip}; color:{text}; border:1px solid {border};
    border-radius:8px; padding:8px 10px;
}}

QMenu {{
    background:{field}; border:1px solid {border}; border-radius:8px;
    padding:4px; color:{text};
}}
QMenu::item {{
    padding:6px 20px; border-radius:4px;
}}
QMenu::item:selected {{
    background:{accent}; color:{hltext};
}}
QMenu::separator {{
    height:1px; background:{border}; margin:4px 8px;
}}

QDialog {{
    background:{bg}; color:{text};
}}

QStatusBar, QStatusBar QLabel {{ color:{muted}; }}
QStatusBar::item {{ border:none; }}
"""


# ─── Package detail tooltips ────────────────────────────────────────────────
# Rich, on-demand package information shown when the user hovers over a row.
# The underlying `pacman -Qi` / `flatpak info` calls are relatively slow, so the
# result is queried lazily (only on first hover) in a worker and cached without
# blocking the GUI. The LRU limit prevents an unbounded memory footprint.
_TOOLTIP_CACHE_LIMIT = 256
_TOOLTIP_QUEUE_LIMIT = 12
_TOOLTIP_CACHE = OrderedDict()
_TOOLTIP_MISSING = object()


def _query_kv(args):
    """Run a command with a stable C locale and parse its "Key : Value" output.

    Both `pacman -Qi` and `flatpak info` print aligned "Key : Value" lines with
    wrapped values continued on further-indented lines. Returns
    ``(title, OrderedDict)`` where *title* is a leading non-key line (Flatpak's
    "Name - summary" banner) or None. Returns None if the command is missing or
    fails (e.g. the package is no longer installed).
    """
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10,
                             env={**os.environ, "LC_ALL": "C"}, check=False)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    info = OrderedDict()
    title = None
    last = None
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9 /_-]*?)\s*:\s+(\S.*)$", line)
        if m:
            last = m.group(1).strip()
            info[last] = m.group(2).strip()
        elif last is not None:               # wrapped continuation of a value
            info[last] += " " + line.strip()
        elif title is None:                  # leading banner (Flatpak)
            title = line.strip()
    return title, info


def _clip(value, limit=160):
    """Trim overly long field values so a tooltip stays compact."""
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _version_change(rec):
    """Human-readable version delta for a log record, or ''. HTML-escaped."""
    old, new = rec["old"], rec["new"]
    if old not in ("—", "") and new not in ("—", "") and old != new:
        return f"{_esc(old)} &rarr; {_esc(new)}"
    if new not in ("—", ""):
        return _esc(new)
    if old not in ("—", ""):
        return _esc(old)
    return ""


def _tt_row(label, value):
    """One label/value row of the tooltip's detail table (value pre-escaped)."""
    return (f"<tr>"
            f"<td style='color:{_UI['muted']};padding:2px 14px 2px 0;"
            f"white-space:nowrap;vertical-align:top;'>{_esc(label)}</td>"
            f"<td style='vertical-align:top;'>{value}</td></tr>")


def _pkg_fields(rec, lang=None):
    """Query and return the (label, value) detail rows for a package.

    Returns a list of pairs, or None when the package could not be queried
    (uninstalled / tool missing), so the caller can show a fallback note.
    """
    if rec["source_key"] == "flatpak":
        flatpak = _command_path("flatpak")
        if flatpak is None:
            return None
        parsed = _query_kv([flatpak, "info", "--", rec["pkg"]])
        if not parsed:
            return None
        title, info = parsed
        desc = title.split(" - ", 1)[1] if title and " - " in title else ""
        return [
            (tr("tt_description", lang), desc),
            (tr("tt_version", lang),     info.get("Version")),
            (tr("tt_licenses", lang),    info.get("License")),
            (tr("tt_size", lang),        info.get("Installed Size")),
            (tr("tt_origin", lang),      info.get("Origin")),
            (tr("tt_runtime", lang),     info.get("Runtime")),
        ]
    pacman = _command_path("pacman")
    if pacman is None:
        return None
    parsed = _query_kv([pacman, "-Qi", "--", rec["pkg"]])
    if not parsed:
        return None
    _title, info = parsed
    return [
        (tr("tt_description", lang),  info.get("Description")),
        (tr("tt_repository", lang),   info.get("Installed From") or info.get("Repository")),
        (tr("tt_version", lang),      info.get("Version")),
        (tr("tt_licenses", lang),     info.get("Licenses")),
        (tr("tt_size", lang),         info.get("Installed Size")),
        (tr("tt_install_date", lang), info.get("Install Date")),
        (tr("tt_url", lang),          info.get("URL")),
    ]


def _render_tooltip(rec, lang=None):
    """Build the full rich-text (HTML) tooltip for one log record."""
    accent = _UI["accent"]
    muted = _UI["muted"]

    header = (
        f"<div style='font-size:13px;font-weight:bold;color:{accent};'>"
        f"{_esc(rec['pkg'])}</div>"
        f"<div style='color:{muted};margin:2px 0 7px 0;'>"
        f"<span style='color:{rec['source_color']};'>●</span>&nbsp;"
        f"{_esc(rec['source_label'])}"
        f"&nbsp;&nbsp;·&nbsp;&nbsp;"
        f"<span style='color:{rec['action_color']};'>"
        f"{_esc(rec['action_label'])}</span>"
        f"&nbsp;&nbsp;·&nbsp;&nbsp;{_esc(rec['display'])}</div>"
    )

    rows = []
    change = _version_change(rec)
    if change:
        rows.append(_tt_row(tr("tt_change", lang), change))

    fields = _pkg_fields(rec, lang)
    if fields is None:
        rows.append(
            f"<tr><td colspan='2' style='color:{muted};padding-top:3px;'>"
            f"{_esc(tr('tt_not_installed', lang))}</td></tr>")
    else:
        for label, value in fields:
            if value and value.strip() and value.strip() != "None":
                rows.append(_tt_row(label, _esc(_clip(value))))

    table = ("<table cellspacing='0' cellpadding='0'>" + "".join(rows) +
             "</table>") if rows else ""
    return header + table


def _tooltip_key(rec, lang=None):
    return rec["source_key"], rec["pkg"], lang or _LANG


def _tooltip_cache_get(key):
    html = _TOOLTIP_CACHE.get(key, _TOOLTIP_MISSING)
    if html is not _TOOLTIP_MISSING:
        _TOOLTIP_CACHE.move_to_end(key)
    return html


def _tooltip_cache_put(key, html):
    _TOOLTIP_CACHE[key] = html
    _TOOLTIP_CACHE.move_to_end(key)
    while len(_TOOLTIP_CACHE) > _TOOLTIP_CACHE_LIMIT:
        _TOOLTIP_CACHE.popitem(last=False)


def tooltip_for(rec, lang=None):
    """Synchronously build/cache a tooltip; workers use the same primitives."""
    lang = lang or _LANG
    key = _tooltip_key(rec, lang)
    html = _tooltip_cache_get(key)
    if html is _TOOLTIP_MISSING:
        html = _render_tooltip(rec, lang)
        _tooltip_cache_put(key, html)
    return html


class _LoadSignals(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)


class _LoadWorker(QRunnable):
    def __init__(self, log_path):
        super().__init__()
        self.log_path = log_path
        self.signals = _LoadSignals()

    def run(self):
        try:
            self.signals.completed.emit(collect_update_data(self.log_path))
        except Exception as exc:  # noqa: BLE001 - keep unexpected I/O/tool failures out of Qt
            self.signals.failed.emit(str(exc))


class _DetailsSignals(QObject):
    completed = pyqtSignal(object)


class _DetailsWorker(QRunnable):
    """Query the detail rows for the package dialog outside the GUI thread."""

    def __init__(self, rec, lang):
        super().__init__()
        self.rec = dict(rec)
        self.lang = lang
        self.signals = _DetailsSignals()

    def run(self):
        try:
            fields = _pkg_fields(self.rec, self.lang)
        except Exception:  # noqa: BLE001 - the dialog falls back to basic fields
            fields = None
        self.signals.completed.emit(fields)


class _TooltipSignals(QObject):
    completed = pyqtSignal(object, str)
    failed = pyqtSignal(object)


class _TooltipWorker(QRunnable):
    def __init__(self, key, rec, lang):
        super().__init__()
        self.key = key
        self.rec = dict(rec)
        self.lang = lang
        self.signals = _TooltipSignals()

    def run(self):
        try:
            html = _render_tooltip(self.rec, self.lang)
            self.signals.completed.emit(self.key, html)
        except Exception:  # noqa: BLE001 - a failed tooltip must not reach Qt
            self.signals.failed.emit(self.key)


# Default parent for flat-model methods (avoids a call in the signature).
_ROOT_INDEX = QModelIndex()

# Custom item-data roles used by the model below.
COLOR_ROLE = Qt.ItemDataRole.UserRole + 1   # badge color for source/action cells
SORT_ROLE  = Qt.ItemDataRole.UserRole + 2   # value used by the sort proxy


class UpdateTableModel(QAbstractTableModel):
    """Model holding the update records.

    Using a model/view (instead of QTableWidget) is what keeps the UI fast:
    the view only renders the handful of currently visible rows, so filtering
    a 50 k-entry history is just a model reset — no per-cell widget creation.
    """
    tooltip_requested = pyqtSignal(object, object)

    # Column -> key in the record dict for the display text.
    _COLS = ("display", "source_label", "action_label", "pkg", "old", "new")

    def __init__(self):
        super().__init__()
        self.rows = []
        self.tooltips_enabled = _TOOLTIPS_ENABLED
        self._pending_tooltips = set()
        self._bold = QFont()
        self._bold.setBold(True)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self._pending_tooltips.clear()
        self.endResetModel()

    def rowCount(self, parent=_ROOT_INDEX):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=_ROOT_INDEX):
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
        if role == Qt.ItemDataRole.ToolTipRole and self.tooltips_enabled:
            key = _tooltip_key(row)
            html = _tooltip_cache_get(key)
            if html is not _TOOLTIP_MISSING:
                return html
            if key not in self._pending_tooltips:
                self._pending_tooltips.add(key)
                self.tooltip_requested.emit(key, row)
            return tr("tt_loading")
        return None

    def tooltip_finished(self, key):
        self._pending_tooltips.discard(key)


def _search_text(rec):
    """Case-folded haystack for the text filter: name, labels, versions, date."""
    parts = (rec["pkg"], rec["source_label"], rec["action_label"],
             rec.get("old"), rec.get("new"), rec.get("display"))
    return " ".join(p for p in parts if p and p != "—").casefold()


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
        self._terms = []
        self._source_key = None
        self._action_key = None

    def set_filters(self, text, source_key, action_key):
        self._text = text.casefold().strip()
        self._terms = self._text.split()
        self._source_key = source_key
        self._action_key = action_key
        self.invalidateFilter()

    def accepts(self, rec):
        if self._source_key and rec["source_key"] != self._source_key:
            return False
        if self._action_key and rec["action_key"] != self._action_key:
            return False
        if self._terms:
            hay = rec.get("search_text")
            if hay is None:
                hay = _search_text(rec)
            return all(term in hay for term in self._terms)
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
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        color = QColor(index.data(COLOR_ROLE) or "#888888")
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            # A badge in its own color vanishes on an accent-colored
            # highlight; use the highlighted-text color instead.
            color = option.palette.highlightedText().color()

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


class PackageDetailsDialog(QDialog):
    """Detailed view for a single package with clickable links and copy buttons."""

    def __init__(self, rec, fields, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.rec = rec
        self.fields = [(label, value.strip()) for label, value in fields or []
                       if value and value.strip() and value.strip() != "None"]
        self.setWindowTitle(f"{rec['pkg']} — {tr('details_title')}")
        self.resize(520, 420)
        self.setMinimumSize(420, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        # Header section
        header_box = QVBoxLayout()
        header_box.setSpacing(4)

        pkg_title = QLabel(rec["pkg"])
        pkg_title.setTextFormat(Qt.TextFormat.PlainText)
        pkg_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() + 4)
        f.setBold(True)
        pkg_title.setFont(f)
        pkg_title.setStyleSheet(f"color: {_UI['accent']};")
        header_box.addWidget(pkg_title)

        meta_line = QLabel(
            f"<span style='color:{rec['source_color']};'>●</span>&nbsp;<b>{_esc(rec['source_label'])}</b>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<span style='color:{rec['action_color']};'><b>{_esc(rec['action_label'])}</b></span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{_esc(rec['display'])}"
        )
        meta_line.setTextFormat(Qt.TextFormat.RichText)
        header_box.addWidget(meta_line)

        change = _version_change(rec)
        if change:
            change_label = QLabel(f"<b>{_esc(tr('tt_change'))}:</b> {change}")
            change_label.setTextFormat(Qt.TextFormat.RichText)
            header_box.addWidget(change_label)

        layout.addLayout(header_box)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # Detail fields inside a scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        form = QFormLayout(scroll_content)
        form.setContentsMargins(4, 4, 4, 4)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        if not self.fields:
            not_inst = QLabel(tr("tt_not_installed"))
            not_inst.setStyleSheet(f"color: {_UI['muted']}; font-style: italic;")
            form.addRow(not_inst)
        else:
            for label, value in self.fields:
                lbl = QLabel(f"<span style='color:{_UI['muted']};'>{_esc(label)}:</span>")
                lbl.setTextFormat(Qt.TextFormat.RichText)

                if value.startswith(("http://", "https://")):
                    val = QLabel(f"<a href='{_esc(value)}' style='color:{_UI['accent']};'>{_esc(value)}</a>")
                    val.setTextFormat(Qt.TextFormat.RichText)
                    val.setOpenExternalLinks(True)
                else:
                    # Plain text: values come from package metadata and must be
                    # shown verbatim (no HTML interpretation, no visible &amp;).
                    val = QLabel(value)
                    val.setTextFormat(Qt.TextFormat.PlainText)
                    val.setWordWrap(True)
                    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(lbl, val)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_copy_pkg = QPushButton(tr("ctx_copy_pkg"))
        btn_copy_pkg.setIcon(QIcon.fromTheme("edit-copy"))
        btn_copy_pkg.clicked.connect(self._copy_pkg)
        btn_layout.addWidget(btn_copy_pkg)

        btn_copy_all = QPushButton(tr("details_copy_all"))
        btn_copy_all.clicked.connect(self._copy_all)
        btn_layout.addWidget(btn_copy_all)

        btn_layout.addStretch(1)

        btn_close = QPushButton(tr("details_close"))
        btn_close.setObjectName("primary")
        btn_close.setDefault(True)
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)

        layout.addLayout(btn_layout)

    def _copy_pkg(self):
        QApplication.clipboard().setText(self.rec["pkg"])

    def _copy_all(self):
        # Same labels and order as the table columns, then the live metadata.
        lines = [f"{label}: {self.rec[key]}"
                 for label, key in zip(tr("headers"), UpdateTableModel._COLS,
                                      strict=True)]
        lines += [f"{label}: {value}" for label, value in self.fields]
        QApplication.clipboard().setText("\n".join(lines))


class ActivityChart(QWidget):
    """A simple bar chart of packages-per-day, drawn with QPainter.

    Kept deliberately dependency-free (no matplotlib / QtCharts) so the only
    requirement remains PyQt6.
    """
    bar_clicked = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.data = []  # list of (iso_date, count)
        self.tooltips_enabled = _TOOLTIPS_ENABLED
        self._hover = None            # index of the bar under the cursor
        self._plot = None             # cached geometry for hit-testing
        self.setMinimumHeight(180)
        self.setMouseTracking(True)

    def set_data(self, data):
        self.data = data
        self._hover = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    # ----- Hover interaction ------------------------------------------------
    def _bar_at(self, pos):
        """Return the data index for the column under a point, or None."""
        if not self._plot or not self.data:
            return None
        left, top, bottom, step = self._plot
        if pos.y() < top - 6 or pos.y() > bottom + 4:
            return None
        idx = int((pos.x() - left) // step)
        return idx if 0 <= idx < len(self.data) else None

    def mouseMoveEvent(self, event):
        idx = self._bar_at(event.position())
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.CursorShape.PointingHandCursor if idx is not None else Qt.CursorShape.ArrowCursor)
            self.update()
        if idx is not None and self.tooltips_enabled:
            day, count = self.data[idx]
            hint = f"<br><span style='font-size:9px;color:{_UI['muted']};'>{_esc(tr('chart_hint'))}</span>"
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"<b>{_esc(day)}</b><br>{count} &times;{hint}", self)
        else:
            QToolTip.hideText()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._bar_at(event.position())
            if idx is not None:
                self.bar_clicked.emit(self.data[idx][0])
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        if self._hover is not None:
            self._hover = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        QToolTip.hideText()

    @staticmethod
    def _top_rounded(rect, r):
        """A rectangle path with only its two top corners rounded."""
        r = max(0.0, min(r, rect.width() / 2, rect.height()))
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom())
        path.lineTo(rect.left(), rect.top() + r)
        path.quadTo(rect.left(), rect.top(), rect.left() + r, rect.top())
        path.lineTo(rect.right() - r, rect.top())
        path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + r)
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text_color = pal.color(QPalette.ColorRole.WindowText)
        accent = pal.color(QPalette.ColorRole.Highlight)
        base = pal.color(QPalette.ColorRole.Base)

        # Card background with a hairline border for a crisp, modern edge.
        card = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawRoundedRect(card, 12, 12)
        border = _mix(base, text_color, 0.16)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card, 12, 12)
        painter.setPen(Qt.PenStyle.NoPen)

        inner = card.adjusted(16, 13, -16, -13)

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
            self._plot = None
            faded = QColor(text_color)
            faded.setAlpha(130)
            painter.setPen(faded)
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter,
                             tr("chart_no_data"))
            painter.end()
            return

        top_pad = 30
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
        step = bar_w + gap
        self._plot = (chart_left, chart_top, chart_bottom, step)

        # Faint baseline for the bars to sit on.
        baseline = _mix(base, text_color, 0.12)
        painter.setPen(baseline)
        painter.drawLine(int(chart_left), int(chart_bottom) + 1,
                         int(chart_left + chart_w), int(chart_bottom) + 1)
        painter.setPen(Qt.PenStyle.NoPen)

        # Gradient endpoints: a vivid accent on top fading to a translucent
        # version at the baseline, giving the bars depth on any theme.
        grad_bottom = _mix(accent, base, 0.45)
        hover_top = _mix(accent, QColor("#ffffff"), 0.25)
        label_color = QColor(text_color)
        label_color.setAlpha(150)
        small_font = QFont(self.font())
        small_font.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))

        # Bars (a count label on top when the bar is wide enough, or on hover)
        for i, (day, count) in enumerate(self.data):
            bar_h = max(2.0, chart_h * count / max_count)
            x = chart_left + i * step
            y = chart_bottom - bar_h
            rect = QRectF(x, y, bar_w, bar_h)
            hovered = (i == self._hover)
            grad = QLinearGradient(x, y, x, chart_bottom)
            grad.setColorAt(0.0, hover_top if hovered else accent)
            grad.setColorAt(1.0, accent if hovered else grad_bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawPath(self._top_rounded(rect, min(4.0, bar_w / 2)))
            if bar_w >= 22 or hovered:
                painter.setFont(small_font)
                painter.setPen(accent if hovered else text_color)
                painter.drawText(QRectF(x - 6, y - 16, bar_w + 12, 14),
                                 Qt.AlignmentFlag.AlignCenter, str(count))
                painter.setPen(Qt.PenStyle.NoPen)

        # X-axis labels: first, middle and last day
        painter.setFont(small_font)
        painter.setPen(label_color)

        def fmt_day(iso):
            try:
                return datetime.strptime(iso, "%Y-%m-%d").strftime("%m-%d")
            except ValueError:
                return iso

        for i in sorted({0, n // 2, n - 1}):
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
        self._log_error = None
        self._load_worker = None
        self._details_worker = None
        self._tooltip_workers = {}
        self._worker_pool = QThreadPool(self)
        self._worker_pool.setMaxThreadCount(3)

        self.setWindowTitle(tr("window_title"))
        self.setWindowIcon(QIcon.fromTheme("system-software-update"))
        self.resize(1040, 740)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # --- Header (title + subtitle) -------------------------------------
        header = QVBoxLayout()
        header.setSpacing(1)
        self.title_label = QLabel(tr("title_label"))
        tf = QFont(self.font())
        tf.setPointSizeF(self.font().pointSizeF() + 5)
        tf.setBold(True)
        self.title_label.setFont(tf)
        header.addWidget(self.title_label)

        self.subtitle_label = QLabel(tr("subtitle"))
        self.subtitle_label.setObjectName("subtitle")
        sf = QFont(self.font())
        sf.setPointSizeF(max(8.0, self.font().pointSizeF() - 0.5))
        self.subtitle_label.setFont(sf)
        header.addWidget(self.subtitle_label)
        root.addLayout(header)

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
        for key in SOURCES:
            self.source_combo.addItem(tr(f"src_{key}"), key)
        self.source_combo.currentIndexChanged.connect(self.apply_filters)

        self.action_combo = QComboBox()
        self.action_combo.addItem(tr("all_actions"), None)
        for key in ACTIONS:
            self.action_combo.addItem(tr(f"act_{key}"), key)
        self.action_combo.currentIndexChanged.connect(self.apply_filters)

        self.tooltip_btn = QPushButton(tr("tooltips_btn"))
        self.tooltip_btn.setObjectName("toggle")
        self.tooltip_btn.setCheckable(True)
        self.tooltip_btn.setChecked(_TOOLTIPS_ENABLED)
        self.tooltip_btn.setIcon(QIcon.fromTheme("dialog-information"))
        self.tooltip_btn.setToolTip(tr("tooltips_hint"))
        self.tooltip_btn.toggled.connect(self.toggle_tooltips)

        self.refresh_btn = QPushButton(tr("reload"))
        self.refresh_btn.setObjectName("primary")
        self.refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.refresh_btn.clicked.connect(self.load_data)

        self.export_btn = QPushButton(tr("export_csv"))
        self.export_btn.setIcon(QIcon.fromTheme("document-save"))
        self.export_btn.clicked.connect(self.export_csv)

        self.lang_btn = QPushButton(_LANG_NAMES[_LANG])
        self.lang_btn.setIcon(QIcon.fromTheme("preferences-desktop-locale"))
        self.lang_btn.clicked.connect(self.switch_language)

        controls.setSpacing(8)
        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.source_combo)
        controls.addWidget(self.action_combo)
        controls.addWidget(self.tooltip_btn)
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
        self.model.tooltip_requested.connect(self._queue_tooltip)
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
        # The date column must fit "YYYY-MM-DD  HH:MM" and, in either
        # language, its bold header plus the sort indicator.
        header_font = QFont(hh.font())
        header_font.setBold(True)
        header_fm = QFontMetrics(header_font)
        date_w = max(
            140,
            self.table.fontMetrics().horizontalAdvance("0000-00-00  00:00") + 36,
            max(header_fm.horizontalAdvance(t["headers"][0]) for t in _T.values()) + 48,
        )
        for col, width in ((0, date_w), (1, 90), (2, 130), (4, 150), (5, 150)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        root.addWidget(self.table, 1)

        # Shortcuts. Keys are listed explicitly: combining StandardKey.Refresh
        # (already F5 on Linux) with an extra "F5" shortcut would make F5
        # ambiguous so that neither fires.
        for keys, slot in ((("Ctrl+F", "/"), self._focus_search),
                           (("Escape",), self._on_escape),
                           (("F5", "Ctrl+R"), self.load_data),
                           (("Ctrl+S", "Ctrl+E"), self.export_csv)):
            shortcut = QShortcut(self)
            shortcut.setKeys([QKeySequence(k) for k in keys])
            shortcut.activated.connect(slot)
        # Copy only while the table has focus, so Ctrl+C elsewhere keeps its
        # normal meaning.
        copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.table,
                                  self._copy_selection)
        copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

        # Interactions
        self.search_input.setToolTip(tr("search_hint"))
        self.chart.bar_clicked.connect(self._on_chart_bar_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        self.table.installEventFilter(self)

        # Permanent summary in a status bar widget; transient messages
        # (loading, copied, exported) temporarily cover it and then reveal it
        # again instead of erasing it.
        self.status = self.statusBar()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status.addWidget(self.status_label, 1)
        self.load_data()

    # ----- Keyboard & Mouse interactions -----------------------------------
    def _rec_at(self, proxy_index):
        """Return the record behind a (proxy) table index, or None."""
        if not proxy_index.isValid():
            return None
        source_index = self.proxy.mapToSource(proxy_index)
        if not source_index.isValid():
            return None
        return self.model.rows[source_index.row()]

    def _focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _on_escape(self):
        if self.search_input.hasFocus() or self.search_input.text():
            self.search_input.clear()
            self.table.setFocus()
        else:
            self.table.clearSelection()

    def _copy_text(self, text, message=None):
        QApplication.clipboard().setText(text)
        self.status.showMessage(message or tr("copied").format(text=text), 3500)

    def _copy_selection(self):
        indexes = sorted(self.table.selectionModel().selectedRows(),
                         key=lambda i: i.row())
        names = [rec["pkg"] for rec in map(self._rec_at, indexes) if rec]
        if len(names) == 1:
            self._copy_text(names[0])
        elif names:
            self._copy_text("\n".join(names),
                            tr("copied_rows").format(n=len(names)))

    def _show_context_menu(self, pos):
        rec = self._rec_at(self.table.indexAt(pos))
        if rec is None:
            return

        menu = QMenu(self)

        act_copy_pkg = menu.addAction(tr("ctx_copy_pkg"))
        act_copy_pkg.setIcon(QIcon.fromTheme("edit-copy"))
        act_copy_pkg.triggered.connect(lambda: self._copy_text(rec["pkg"]))

        ver = rec["new"] if rec["new"] != "—" else rec["old"]
        if ver and ver != "—":
            act_copy_ver = menu.addAction(tr("ctx_copy_ver"))
            act_copy_ver.triggered.connect(lambda: self._copy_text(ver))

        act_copy_row = menu.addAction(tr("ctx_copy_row"))
        row_str = (
            f"{rec['display']}\t{rec['source_label']}\t"
            f"{rec['action_label']}\t{rec['pkg']}\t{rec['old']}\t{rec['new']}"
        )
        act_copy_row.triggered.connect(
            lambda: self._copy_text(row_str, tr("copied_row")))

        menu.addSeparator()

        act_filter = menu.addAction(tr("ctx_filter_pkg"))
        act_filter.setIcon(QIcon.fromTheme("edit-find"))
        act_filter.triggered.connect(lambda: self.search_input.setText(rec["pkg"]))

        act_details = menu.addAction(tr("ctx_details"))
        act_details.setIcon(QIcon.fromTheme("dialog-information"))
        act_details.triggered.connect(lambda: self.show_package_details(rec))

        menu.exec(self.table.viewport().mapToGlobal(pos))
        menu.deleteLater()  # parented to the window; free it once closed

    def _on_row_double_clicked(self, proxy_index):
        rec = self._rec_at(proxy_index)
        if rec is not None:
            self.show_package_details(rec)

    def show_package_details(self, rec):
        """Query details in a worker, then open the dialog (non-blocking)."""
        if self._details_worker is not None:
            return
        self.status.showMessage(tr("tt_loading"))
        worker = _DetailsWorker(rec, _LANG)
        worker.signals.completed.connect(
            lambda fields: self._details_completed(rec, fields))
        self._details_worker = worker
        self._worker_pool.start(worker, 1)

    def _details_completed(self, rec, fields):
        self._details_worker = None
        self.status.clearMessage()
        dlg = PackageDetailsDialog(rec, fields, self)
        dlg.open()

    def _on_chart_bar_clicked(self, day):
        if self.search_input.text().strip() == day:
            self.search_input.clear()
        else:
            self.search_input.setText(day)

    def eventFilter(self, source, event):
        if (source == self.table and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            idx = self.table.currentIndex()
            if idx.isValid():
                self._on_row_double_clicked(idx)
                return True
        return super().eventFilter(source, event)

    def changeEvent(self, event):
        # Follow live system theme / accent changes. In Qt 6 a new application
        # palette reaches the window as PaletteChange; apply_theme() is a no-op
        # when the derived stylesheet is unchanged, which also prevents a
        # feedback loop from the repolish triggered by setStyleSheet().
        if (event.type() in (QEvent.Type.ApplicationPaletteChange,
                             QEvent.Type.PaletteChange)
                and apply_theme(QApplication.instance())):
            _TOOLTIP_CACHE.clear()      # cached tooltip HTML embeds old colors
            self.apply_filters()        # stats line embeds the accent color
            self.chart.update()
        super().changeEvent(event)

    # ----- Toggles ----------------------------------------------------------
    def toggle_tooltips(self, enabled):
        """Enable/disable the hover detail tooltips and remember the choice."""
        global _TOOLTIPS_ENABLED
        _TOOLTIPS_ENABLED = enabled
        self.model.tooltips_enabled = enabled
        self.chart.tooltips_enabled = enabled
        _update_config(tooltips=enabled)

    # ----- Language switch --------------------------------------------------
    def switch_language(self):
        global _LANG
        _LANG = "de" if _LANG == "en" else "en"
        _update_config(lang=_LANG)
        _TOOLTIP_CACHE.clear()          # labels are language-specific
        self._retranslate_rows()
        self._retranslate_ui()
        self.apply_filters()

    @staticmethod
    def _translate_row(row):
        row["action_label"] = tr(f"act_{row['action_key']}")
        row["source_label"] = tr(f"src_{row['source_key']}")
        row["search_text"] = _search_text(row)

    def _retranslate_rows(self):
        """Update translated display labels in all loaded records."""
        for row in self.all_data:
            self._translate_row(row)
        self.model.set_rows(self.all_data)

    def _retranslate_ui(self):
        """Update all static UI strings to the active language."""
        self.setWindowTitle(tr("window_title"))
        self.title_label.setText(tr("title_label"))
        self.subtitle_label.setText(tr("subtitle"))
        self.search_input.setPlaceholderText(tr("search_ph"))
        self.search_input.setToolTip(tr("search_hint"))
        self.lang_btn.setText(_LANG_NAMES[_LANG])
        self.refresh_btn.setText(tr("reload"))
        self.export_btn.setText(tr("export_csv"))
        self.tooltip_btn.setText(tr("tooltips_btn"))
        self.tooltip_btn.setToolTip(tr("tooltips_hint"))

        src_key = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem(tr("all_sources"), None)
        for key in SOURCES:
            self.source_combo.addItem(tr(f"src_{key}"), key)
        self.source_combo.setCurrentIndex(max(0, self.source_combo.findData(src_key)))
        self.source_combo.blockSignals(False)

        act_key = self.action_combo.currentData()
        self.action_combo.blockSignals(True)
        self.action_combo.clear()
        self.action_combo.addItem(tr("all_actions"), None)
        for key in ACTIONS:
            self.action_combo.addItem(tr(f"act_{key}"), key)
        self.action_combo.setCurrentIndex(max(0, self.action_combo.findData(act_key)))
        self.action_combo.blockSignals(False)

        self.chart.update()  # repaint chart title

    # ----- Data loading -----------------------------------------------------
    def load_data(self):
        """Load all sources in a bounded worker pool, keeping Qt responsive."""
        if self._load_worker is not None:
            return
        self.refresh_btn.setEnabled(False)
        self.status.showMessage(tr("status_loading"))
        worker = _LoadWorker(self.log_path)
        worker.signals.completed.connect(self._load_completed)
        worker.signals.failed.connect(self._load_failed)
        self._load_worker = worker
        self._worker_pool.start(worker, 1)

    def _load_completed(self, result):
        self._load_worker = None
        self.refresh_btn.setEnabled(True)
        self.status.clearMessage()
        data = result["rows"]

        # Enrich records only in the GUI thread: translated labels depend on
        # the language that is active at the instant loading completes.
        for row in data:
            dt = row["dt"]
            row["sort_ts"] = dt.timestamp() if dt else 0.0
            row["display"] = dt.strftime("%Y-%m-%d  %H:%M") if dt else "—"
            row["action_color"] = ACTIONS.get(row["action_key"], "#888888")
            row["source_color"] = SOURCES.get(row["source_key"], "#888888")
            self._translate_row(row)
        data.sort(key=lambda r: r["sort_ts"], reverse=True)
        self.all_data = data
        self.model.set_rows(data)

        counts = Counter(r["source_key"] for r in data)
        self._entry_count = len(data)
        self._src_counts = dict(counts)
        self._flatpak_available = result["flatpak_available"]
        self._has_flatpak_rows = result["has_flatpak_rows"]
        self._log_error = result["log_error"]
        self.apply_filters()

        if self._log_error and self._log_error[0] == "permission":
            QMessageBox.warning(
                self, tr("perm_denied"),
                tr("cannot_read").format(path=self._log_error[1]),
            )

    def _load_failed(self, error):
        self._load_worker = None
        self.refresh_btn.setEnabled(True)
        self.status.showMessage(tr("load_failed").format(error=error))

    # ----- Asynchronous package details ------------------------------------
    def _queue_tooltip(self, key, rec):
        if key in self._tooltip_workers:
            return
        if len(self._tooltip_workers) >= _TOOLTIP_QUEUE_LIMIT:
            self.model.tooltip_finished(key)
            return
        worker = _TooltipWorker(key, rec, key[2])
        worker.signals.completed.connect(self._tooltip_completed)
        worker.signals.failed.connect(self._tooltip_failed)
        self._tooltip_workers[key] = worker
        self._worker_pool.start(worker)

    def _tooltip_completed(self, key, html):
        self._tooltip_workers.pop(key, None)
        self.model.tooltip_finished(key)
        _tooltip_cache_put(key, html)

        # Replace the temporary "Loading …" tooltip if the pointer still
        # rests on the same package when the worker finishes.
        if not self.model.tooltips_enabled:
            return
        global_pos = QCursor.pos()
        view_pos = self.table.viewport().mapFromGlobal(global_pos)
        rec = self._rec_at(self.table.indexAt(view_pos))
        if rec is not None and _tooltip_key(rec) == key:
            QToolTip.showText(global_pos, html, self.table.viewport())

    def _tooltip_failed(self, key):
        self._tooltip_workers.pop(key, None)
        self.model.tooltip_finished(key)

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
        if self._log_error:
            error_key, path = self._log_error
            if error_key == "file_not_found":
                s += f"   {tr('file_not_found').format(path=path)}"
            else:
                s += f"   {tr('cannot_read').format(path=path)}"
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
        self.status_label.setText(msg)

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
        html = (f"<b>{tr('stats_total')}:</b>&nbsp;"
                f"<b style='color:{_UI['accent']};'>{total}</b>")
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
            self.status.showMessage(
                tr("exported").format(n=len(rows), path=path), 8000)
        except OSError as e:
            QMessageBox.warning(self, tr("export_fail"), str(e))


def main():
    global _LANG, _TOOLTIPS_ENABLED
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"CachyOS Update History {__version__}")
        sys.exit(0)
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            f"CachyOS Update History {__version__}\n"
            "Usage: cachy-update-history [OPTIONS]\n\n"
            "Options:\n"
            "  -h, --help     Show this help message and exit\n"
            "  -v, --version  Show program version and exit"
        )
        sys.exit(0)

    config = _load_config()
    if config.get("lang") in _T:
        _LANG = config["lang"]
    if isinstance(config.get("tooltips"), bool):
        _TOOLTIPS_ENABLED = config["tooltips"]

    app = QApplication(sys.argv)
    app.setApplicationName("CachyOS Update History")
    app.setApplicationVersion(__version__)
    # Wayland app id / KDE desktop-file hint: lets the shell match the window
    # to cachy-update-history.desktop and show its icon in task switchers.
    app.setDesktopFileName("cachy-update-history")
    apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
