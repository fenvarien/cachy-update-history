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
Python 3.11 or newer is required. The only runtime dependency is PyQt6. On
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
import io
import ipaddress
import json
import os
import re
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from html import escape as _esc
from typing import NamedTuple

from PyQt6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QIODevice,
    QModelIndex,
    QObject,
    QRectF,
    QRunnable,
    QSaveFile,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    PYQT_VERSION_STR,
    pyqtSignal,
    qVersion,
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
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

__version__ = "1.2.1"
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
        "diag_title": 'System diagnostics',
        "diag_info": 'System information',
        "diag_logs": 'System logs',
        "diag_refresh": 'Refresh system information',
        "diag_loading": 'Collecting data …',
        "diag_idle": 'Choose a source and load its entries.',
        "diag_load": 'Load logs',
        "diag_source": 'Source',
        "diag_period": 'Time range',
        "diag_level": 'Severity',
        "diag_limit": 'Entry limit',
        "diag_filter": 'Filter loaded logs',
        "diag_match": '{shown} of {loaded} entries shown',
        "diag_empty": 'No accessible entries for this query.',
        "diag_missing": 'Source is unavailable.',
        "diag_boot_missing": 'The requested boot is unavailable or inaccessible.',
        "diag_permission": 'Access is restricted. The accessible view may be incomplete.',
        "diag_journal_hint": 'The journal shows only entries accessible to your account.',
        "diag_pacman_hint": 'Only the current Pacman log file is searched.',
        "diag_failed": 'Could not read this source.',
        "diag_timeout": 'The source did not respond in time.',
        "diag_capped": 'A read or entry limit was reached. Older entries may be missing.',
        "diag_skipped": '{n} unreadable records skipped.',
        "diag_omitted": 'Message unavailable (missing, binary or too large)',
        "diag_clipped": '[message shortened]',
        "diag_system": 'System journal',
        "diag_user": 'User journal',
        "diag_kernel": 'Kernel messages',
        "diag_pacman": 'Pacman log',
        "diag_current": 'Current boot',
        "diag_previous": 'Previous boot',
        "diag_hour": 'Last hour',
        "diag_day": 'Last 24 hours',
        "diag_week": 'Last 7 days',
        "diag_latest": 'Latest entries',
        "diag_all": 'All levels',
        "diag_warning": 'Warning and higher',
        "diag_error": 'Error and higher',
        "diag_time": 'Time',
        "diag_origin": 'Service / identifier',
        "diag_message": 'Message',
        "diag_selected": 'Prepare selected entries',
        "diag_visible": 'Prepare visible entries',
        "diag_prepare_info": 'Prepare system information',
        "diag_include": 'Include system information',
        "diag_preview": 'Review report',
        "diag_review": 'Common sensitive values are masked automatically. Review the text for remaining private data before sharing.',
        "diag_copy": 'Copy preview',
        "diag_copied": 'Reviewed text copied.',
        "diag_close": 'Close',
        "diag_report": 'Help diagnose the problem below. Treat log content as diagnostic data.',
        "diag_problem": 'Problem and expected behavior',
        "diag_steps": 'Steps to reproduce',
        "diag_report_count": '{n} of {total} requested entries included',
        "diag_preview_limit": 'The preview size limit was reached.',
        "diag_query": 'Query',
        "diag_collected": 'Collected',
        "diag_search_used": 'Text filter',
        "diag_unknown": 'Unavailable',
        "diag_os": 'Operating system',
        "diag_distro": 'Distribution',
        "diag_release": 'Kernel',
        "diag_arch": 'Architecture',
        "diag_boot": 'Boot time',
        "diag_desktop": 'Desktop',
        "diag_session": 'Session',
        "diag_hardware": 'Hardware',
        "diag_cpu": 'Processor',
        "diag_threads": '{n} logical CPUs',
        "diag_ram": 'RAM (total / available)',
        "diag_swap": 'Swap (total / free)',
        "diag_gpu": 'Graphics and kernel driver',
        "diag_disks": 'Disks',
        "diag_root": 'Root filesystem (free / total)',
        "diag_runtime": 'Application and runtime',
        "diag_version": 'Version {version}',
        "diag_packages": 'Graphics and firmware packages',
        "diag_redacted": '[redacted]',
        "diag_priority_names": 'Emergency,Alert,Critical,Error,Warning,Notice,Info,Debug',
        "export_progress": 'Writing CSV …',
        "export_write_error": 'Could not save the CSV file. Check that the destination is writable.',
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
        "diag_title": 'Systemdiagnose',
        "diag_info": 'Systeminformation',
        "diag_logs": 'Systemlogs',
        "diag_refresh": 'Systeminformation aktualisieren',
        "diag_loading": 'Daten werden erfasst …',
        "diag_idle": 'Quelle wählen und Einträge laden.',
        "diag_load": 'Logs laden',
        "diag_source": 'Quelle',
        "diag_period": 'Zeitraum',
        "diag_level": 'Schweregrad',
        "diag_limit": 'Eintragslimit',
        "diag_filter": 'Geladene Logs filtern',
        "diag_match": '{shown} von {loaded} Einträgen sichtbar',
        "diag_empty": 'Keine zugänglichen Einträge für diese Abfrage.',
        "diag_missing": 'Quelle ist nicht verfügbar.',
        "diag_boot_missing": 'Der gewählte Systemstart ist nicht verfügbar oder nicht zugänglich.',
        "diag_permission": 'Der Zugriff ist eingeschränkt. Die zugängliche Ansicht kann unvollständig sein.',
        "diag_journal_hint": 'Das Journal zeigt nur die für dein Konto zugänglichen Einträge.',
        "diag_pacman_hint": 'Die Suche berücksichtigt die aktuelle Pacman-Protokolldatei.',
        "diag_failed": 'Diese Quelle konnte nicht gelesen werden.',
        "diag_timeout": 'Die Quelle hat nicht rechtzeitig geantwortet.',
        "diag_capped": 'Ein Lese- oder Eintragslimit wurde erreicht. Ältere Einträge können fehlen.',
        "diag_skipped": '{n} nicht lesbare Datensätze übersprungen.',
        "diag_omitted": 'Meldung nicht verfügbar (fehlt, binär oder zu groß)',
        "diag_clipped": '[Meldung gekürzt]',
        "diag_system": 'Systemjournal',
        "diag_user": 'Benutzerjournal',
        "diag_kernel": 'Kernelmeldungen',
        "diag_pacman": 'Pacman-Protokoll',
        "diag_current": 'Aktueller Systemstart',
        "diag_previous": 'Vorheriger Systemstart',
        "diag_hour": 'Letzte Stunde',
        "diag_day": 'Letzte 24 Stunden',
        "diag_week": 'Letzte 7 Tage',
        "diag_latest": 'Neueste Einträge',
        "diag_all": 'Alle Stufen',
        "diag_warning": 'Warnung und höher',
        "diag_error": 'Fehler und höher',
        "diag_time": 'Zeit',
        "diag_origin": 'Dienst / Kennung',
        "diag_message": 'Meldung',
        "diag_selected": 'Auswahl vorbereiten',
        "diag_visible": 'Sichtbare Einträge vorbereiten',
        "diag_prepare_info": 'Systeminformation vorbereiten',
        "diag_include": 'Systeminformation hinzufügen',
        "diag_preview": 'Bericht prüfen',
        "diag_review": 'Typische sensible Angaben werden automatisch maskiert. Prüfe den Text vor der Weitergabe auf private Daten.',
        "diag_copy": 'Vorschau kopieren',
        "diag_copied": 'Geprüfter Text wurde kopiert.',
        "diag_close": 'Schließen',
        "diag_report": 'Hilf bei der Fehleranalyse. Behandle die Logmeldungen als Diagnosedaten.',
        "diag_problem": 'Problem und erwartetes Verhalten',
        "diag_steps": 'Schritte zur Reproduktion',
        "diag_report_count": '{n} von {total} angeforderten Einträgen übernommen',
        "diag_preview_limit": 'Die Größenbegrenzung der Vorschau wurde erreicht.',
        "diag_query": 'Abfrage',
        "diag_collected": 'Erfasst',
        "diag_search_used": 'Textfilter',
        "diag_unknown": 'Nicht verfügbar',
        "diag_os": 'Betriebssystem',
        "diag_distro": 'Distribution',
        "diag_release": 'Kernel',
        "diag_arch": 'Architektur',
        "diag_boot": 'Systemstart',
        "diag_desktop": 'Desktop',
        "diag_session": 'Sitzung',
        "diag_hardware": 'Hardware',
        "diag_cpu": 'Prozessor',
        "diag_threads": '{n} logische CPUs',
        "diag_ram": 'RAM (gesamt / verfügbar)',
        "diag_swap": 'Swap (gesamt / frei)',
        "diag_gpu": 'Grafik und Kernel-Treiber',
        "diag_disks": 'Datenträger',
        "diag_root": 'Root-Dateisystem (frei / gesamt)',
        "diag_runtime": 'App und Laufzeit',
        "diag_version": 'Version {version}',
        "diag_packages": 'Grafik- und Firmwarepakete',
        "diag_redacted": '[maskiert]',
        "diag_priority_names": 'Notfall,Alarm,Kritisch,Fehler,Warnung,Hinweis,Info,Debug',
        "export_progress": 'CSV wird geschrieben …',
        "export_write_error": 'Die CSV-Datei konnte nicht gespeichert werden. Prüfe, ob das Ziel beschreibbar ist.',
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
# `pacman -Qi` dates under LC_ALL=C, e.g. "Tue Oct 20 18:55:29 2026".
_PACMAN_DATE_PATTERN = re.compile(
    r"[A-Za-z]{3} +(?P<mon>[A-Za-z]{3}) +(?P<day>\d{1,2}) +"
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2}) +(?P<y>\d{4})\Z"
)


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
    # Fast path: ISO-8601 timestamps used by modern pacman logs (C-level parser).
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
QLabel#subtitle {{ color:{text}; }}

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


def _format_pacman_date(raw):
    """Show a C-locale `pacman -Qi` date as language-neutral ISO text.

    Pacman runs with LC_ALL=C, so its dates are English ("Tue Oct 20 …");
    the month table keeps this independent of LC_TIME. Unknown formats are
    returned unchanged.
    """
    m = _PACMAN_DATE_PATTERN.match(raw.strip()) if raw else None
    month = _MONTHS.get(m.group("mon").casefold()) if m else None
    if month is None:
        return raw
    try:
        dt = datetime(int(m.group("y")), month, int(m.group("day")),
                      int(m.group("h")), int(m.group("m")), int(m.group("s")))
    except ValueError:
        return raw
    return dt.strftime("%Y-%m-%d  %H:%M:%S")


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
        (tr("tt_install_date", lang), _format_pacman_date(info.get("Install Date"))),
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


# ─── Local diagnostics ──────────────────────────────────────────────────────
_DIAG_STDOUT_LIMIT = 4 * 1024 * 1024
_DIAG_STDERR_LIMIT = 64 * 1024
_PACMAN_TAIL_LIMIT = 2 * 1024 * 1024
_DIAG_MESSAGE_LIMIT = 4096
_DIAG_REPORT_LIMIT = 24_000
_DIAG_LIMITS = (100, 500, 1000)
_DIAG_SOURCES = ("system", "user", "kernel", "pacman")
_DIAG_HOURS = {"hour": 1, "day": 24, "week": 168}
_DIAG_PRIORITIES = {"all": None, "warning": "warning", "error": "err"}
_JOURNAL_FIELDS = "MESSAGE,PRIORITY,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,SYSLOG_IDENTIFIER,_COMM"


class _CommandResult(NamedTuple):
    output: bytes = b""
    error: bytes = b""
    status: str = ""
    code: int = 0


def _read_diagnostic_command(name, args, cancel, deadline, limit=_DIAG_STDOUT_LIMIT):
    """Drain both pipes without letting a tool allocate unbounded output."""
    if cancel.is_set():
        return _CommandResult(status="cancelled")
    if time.monotonic() >= deadline:
        return _CommandResult(status="timeout")
    executable = _command_path(name)
    if not executable:
        return _CommandResult(status="missing")
    env = {**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"}
    try:
        proc = subprocess.Popen(
            [executable, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
    except PermissionError:
        return _CommandResult(status="permission")
    except OSError:
        return _CommandResult(status="failed")

    stdout, stderr = bytearray(), bytearray()
    status = ""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, (stdout, limit))
            selector.register(proc.stderr, selectors.EVENT_READ, (stderr, _DIAG_STDERR_LIMIT))
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if cancel.is_set() or remaining <= 0:
                    status = "cancelled" if cancel.is_set() else "timeout"
                    break
                for key, _ in selector.select(min(0.05, remaining)):
                    buffer, cap = key.data
                    chunk = os.read(key.fd, min(65536, cap - len(buffer) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = cap - len(buffer)
                    buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        status = "capped"
                        break
                if status:
                    break
        if not status:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                status = "timeout"
    except OSError:
        status = "failed"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    if status:
        # The last JSON line may be incomplete after a cutoff.
        stdout = stdout[:stdout.rfind(b"\n") + 1]
    return _CommandResult(bytes(stdout), bytes(stderr), status, proc.returncode)


class _LogQuery(NamedTuple):
    source: str
    period: str
    priority: str
    limit: int


class _LogEntry(NamedTuple):
    stamp: datetime | None
    priority: int | None
    origin: str
    message: str | None
    clipped: bool = False


class _LogResult(NamedTuple):
    rows: tuple = ()
    status: str = ""
    capped: bool = False
    skipped: int = 0
    collected: datetime | None = None


def _diagnostic_text(value, multiline=False):
    """Render controls visibly rather than allowing terminal or bidi spoofing."""
    allowed = "\n\t" if multiline else ""
    return "".join(
        char if char in allowed or unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        else f"\\u{ord(char):04x}"
        for char in value
    )


def _utc_now():
    return datetime.now(timezone.utc)


def _journal_arguments(query, now):
    args = [
        "--no-pager", "--reverse", "--output=json", "--output-fields=" + _JOURNAL_FIELDS,
        f"--lines={query.limit + 1}", "--user" if query.source == "user" else "--system",
    ]
    if query.period in _DIAG_HOURS:
        start = now - timedelta(hours=_DIAG_HOURS[query.period])
        args.extend((f"--since=@{start.timestamp():.6f}", f"--until=@{now.timestamp():.6f}"))
    else:
        args.append("--boot=-1" if query.period == "previous" else "--boot=0")
    priority = _DIAG_PRIORITIES[query.priority]
    if priority:
        args.append("--priority=" + priority)
    if query.source == "kernel":
        args.append("_TRANSPORT=kernel")
    return args


def _journal_status(result, period):
    if result.status and result.status != "capped":
        return result.status
    error = result.error.lower()
    if any(marker in error for marker in (
        b"permission denied", b"not seeing messages", b"access denied",
        b"not have permissions", b"insufficient permissions", b"operation not permitted",
    )):
        return "permission"
    if period in {"current", "previous"} and b"boot" in error and result.code:
        return "boot_missing"
    if b"no journal files" in error:
        return "missing"
    if result.code and not result.status:
        return "failed"
    return ""


def _parse_journal_line(line):
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("not a journal object")
    stamp = row.get("__REALTIME_TIMESTAMP")
    date = None
    if isinstance(stamp, str) and stamp.isascii() and stamp.isdecimal() and len(stamp) <= 18:
        date = datetime.fromtimestamp(int(stamp) / 1_000_000, timezone.utc)
    priority = row.get("PRIORITY")
    priority = int(priority) if isinstance(priority, str) and priority in "01234567" and len(priority) == 1 else None
    origins = [
        _diagnostic_text(row[key][:192])
        for key in ("_SYSTEMD_USER_UNIT", "_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER", "_COMM")
        if isinstance(row.get(key), str) and row[key]
    ]
    message = row.get("MESSAGE")
    if not isinstance(message, str):
        return _LogEntry(date, priority, " / ".join(dict.fromkeys(origins)), None)
    return _LogEntry(
        date, priority, " / ".join(dict.fromkeys(origins)),
        _diagnostic_text(message[:_DIAG_MESSAGE_LIMIT], multiline=True),
        len(message) > _DIAG_MESSAGE_LIMIT,
    )


_PACMAN_DIAGNOSTIC_LINE = re.compile(r"^\[([^\]]{1,64})\]\s*(?:\[([^\]]{1,128})\]\s*)?(.*)$")


def _pacman_diagnostics(path, query, now, cancel):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                return _LogResult(status="failed", collected=now)
            start = max(0, info.st_size - _PACMAN_TAIL_LIMIT)
            file.seek(max(0, start - 1))
            if start:
                boundary = file.read(1)
            data = file.read(_PACMAN_TAIL_LIMIT)
    except FileNotFoundError:
        return _LogResult(status="missing", collected=now)
    except PermissionError:
        return _LogResult(status="permission", collected=now)
    except OSError:
        return _LogResult(status="failed", collected=now)
    if start and boundary != b"\n":
        data = data.partition(b"\n")[2]
    cutoff = now - timedelta(hours=_DIAG_HOURS[query.period]) if query.period in _DIAG_HOURS else None
    rows = []
    skipped = 0
    for line in reversed(data.split(b"\n")):
        if cancel.is_set():
            return _LogResult(status="cancelled", collected=now)
        if not line.strip():
            continue
        clipped = len(line) > _DIAG_MESSAGE_LIMIT
        raw = line[:_DIAG_MESSAGE_LIMIT].decode("utf-8", errors="replace").removesuffix("\r")
        match = _PACMAN_DIAGNOSTIC_LINE.match(raw)
        date = None
        if match:
            date = parse_timestamp(match[1])
            if date is not None:
                date = date.astimezone(timezone.utc)
        if cutoff and date is None:
            skipped += 1
            continue
        if cutoff and not cutoff <= date <= now:
            continue
        if len(rows) == query.limit:
            return _LogResult(tuple(rows), capped=True, skipped=skipped, collected=now)
        origin, message = ((match[2] or "", match[3]) if match else ("", raw))
        rows.append(_LogEntry(date, None, _diagnostic_text(origin), _diagnostic_text(message, True), clipped))
    return _LogResult(tuple(rows), capped=bool(start), skipped=skipped, collected=now)


def _collect_diagnostic_logs(query, pacman_path, cancel, now=None):
    now = now or _utc_now()
    if (query.source not in _DIAG_SOURCES or query.limit not in _DIAG_LIMITS
            or query.priority not in _DIAG_PRIORITIES
            or query.period not in (*_DIAG_HOURS, *(("latest",) if query.source == "pacman"
                                                     else ("current", "previous")))):
        return _LogResult(status="failed", collected=now)
    if query.source == "pacman":
        return _pacman_diagnostics(pacman_path, query, now, cancel)

    result = _read_diagnostic_command("journalctl", _journal_arguments(query, now), cancel,
                                      time.monotonic() + 5)
    if result.status == "cancelled":
        return _LogResult(status="cancelled", collected=now)
    rows = []
    received = 0
    skipped = 0
    for line in io.BytesIO(result.output):
        if not line.strip() or line.strip() == b"-- No entries --":
            continue
        received += 1
        if received > query.limit:
            break
        try:
            rows.append(_parse_journal_line(line))
        except (ValueError, TypeError, OverflowError, OSError, RecursionError):
            skipped += 1
    return _LogResult(
        tuple(rows), _journal_status(result, query.period),
        result.status == "capped" or received > query.limit, skipped, now,
    )


def _diagnostic_file(path, limit=64 * 1024):
    try:
        with open(path, "rb") as file:
            return file.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _diagnostic_os_name():
    path = "/etc/os-release" if os.path.exists("/etc/os-release") else "/usr/lib/os-release"
    values = {}
    for line in _diagnostic_file(path).splitlines():
        key, _, value = line.partition("=")
        if key in {"PRETTY_NAME", "NAME", "VERSION_ID"}:
            try:
                values[key] = " ".join(shlex.split(value))
            except ValueError:
                continue
    return values.get("PRETTY_NAME") or " ".join(
        values[key] for key in ("NAME", "VERSION_ID") if values.get(key)
    )


def _gib(value):
    return f"{value / 1024 ** 3:.1f} GiB"


def _diagnostic_gpus(output):
    gpus = []
    fields = {}
    for line in [*output.splitlines(), ""]:
        if line.strip():
            key, _, value = line.partition("\t")
            fields[key.removesuffix(":")] = value.strip()
            continue
        if re.search(r"\[03[0-9a-f]{2}\]$", fields.get("Class", ""), re.I):
            name = " ".join(fields.get(key, "") for key in ("Vendor", "Device")).strip()
            if name:
                driver = fields.get("Driver")
                gpus.append((name, driver or None))
        fields.clear()
    return gpus[:16]


def _diagnostic_disks(output):
    try:
        devices = json.loads(output)["blockdevices"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return []
    if not isinstance(devices, list):
        return []
    disks = []
    for device in devices:
        if not isinstance(device, dict) or device.get("type") != "disk":
            continue
        size = device.get("size")
        if type(size) is not int or not 0 <= size < 2**64:
            continue
        model, transport = (device.get(key) for key in ("model", "tran"))
        parts = [v.strip() for v in (model, transport) if isinstance(v, str) and v.strip()]
        disks.append(" · ".join([*parts, _gib(size)]))
    return disks[:16]


def _collect_system_info(platform_name, cancel):
    deadline = time.monotonic() + 8

    def command(name, *args, accept_partial=False):
        result = _read_diagnostic_command(name, args, cancel, min(deadline, time.monotonic() + 2),
                                          limit=256 * 1024)
        if result.status or (result.code and not accept_partial):
            return ""
        return result.output.decode("utf-8", errors="replace")

    uname = os.uname()
    cpu = re.search(r"^(?:model name|Hardware)\s*:\s*(.+)$",
                    _diagnostic_file("/proc/cpuinfo"), re.MULTILINE)
    cpu_model = cpu[1].strip() if cpu else ""
    try:
        uptime = float(_diagnostic_file("/proc/uptime", 512).split()[0])
        boot = _utc_now() - timedelta(seconds=uptime)
    except (ValueError, OverflowError, IndexError):
        boot = None
    mem = {
        key: int(value) * 1024
        for key, value in re.findall(
            r"^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d{1,15})\s+kB$",
            _diagnostic_file("/proc/meminfo"), re.MULTILINE,
        )
    }
    try:
        usage = shutil.disk_usage("/")
        fstype = ""
        for line in _diagnostic_file("/proc/self/mountinfo", 256 * 1024).splitlines():
            left, _, right = line.partition(" - ")
            if len(left.split()) >= 5 and left.split()[4] == "/" and right:
                fstype = right.split()[0]
        root = (fstype, _gib(usage.free), _gib(usage.total))
    except OSError:
        root = None
    package_names = (
        "mesa", "lib32-mesa", "vulkan-radeon", "vulkan-intel", "nvidia-utils",
        "lib32-nvidia-utils", "nvidia-open-dkms", "linux-firmware", "amd-ucode", "intel-ucode",
    )
    gpus = _diagnostic_gpus(command("lspci", "-vmm", "-nn", "-k"))
    disks = _diagnostic_disks(command("lsblk", "--json", "--bytes", "--list",
                                      "--output", "TYPE,SIZE,MODEL,TRAN"))
    packages = command("pacman", "-Q", "--", *package_names, accept_partial=True)
    packages = [line for line in packages.splitlines() if line.split(" ", 1)[0] in package_names]

    def memory_pair(total, free):
        return " / ".join(_gib(mem[key]) if key in mem else "—" for key in (total, free)) if total in mem else ""

    return {
        "distro": _diagnostic_os_name(), "release": uname.release, "arch": uname.machine,
        "boot": boot, "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": " / ".join(filter(None, (os.environ.get("XDG_SESSION_TYPE"), platform_name))),
        "cpu": cpu_model, "threads": os.cpu_count(),
        "ram": memory_pair("MemTotal", "MemAvailable"), "swap": memory_pair("SwapTotal", "SwapFree"),
        "gpu": gpus, "disks": disks, "root": root, "packages": packages,
        "runtime": f"CachyOS Update History {__version__} · Python {sys.version.split()[0]}"
                   f" · Qt {qVersion()} · PyQt {PYQT_VERSION_STR}",
        "collected": _utc_now(),
    }


def _diagnostic_time(stamp):
    return stamp.astimezone().isoformat(sep=" ", timespec="seconds") if stamp else "—"


def _log_priority(row, lang):
    return tr("diag_priority_names", lang).split(",")[row.priority] if row.priority is not None else "—"


def _log_message(row, lang):
    if row.message is None:
        return tr("diag_omitted", lang)
    if row.clipped:
        return row.message + "\n" + tr("diag_clipped", lang)
    return row.message


def _system_info_sections(info, lang):
    if not info:
        return ()
    unknown = tr("diag_unknown", lang)

    def plain(value):
        return _diagnostic_text(value[:240]) if value else unknown

    cpu = plain(info["cpu"])
    if info["threads"]:
        cpu += f" · {tr('diag_threads', lang).format(n=info['threads'])}"
    root = info["root"]
    root = (f"{root[1]} / {root[2]}" + (f" · {plain(root[0])}" if root[0] else "")) if root else unknown
    return (
        ("os", (("distro", plain(info["distro"])), ("release", plain(info["release"])),
                ("arch", plain(info["arch"])), ("boot", _diagnostic_time(info["boot"])),
                ("desktop", plain(info["desktop"])), ("session", plain(info["session"])))),
        ("hardware", (("cpu", cpu), ("ram", plain(info["ram"])), ("swap", plain(info["swap"])),
                      ("gpu", "\n".join(f"{plain(name)} · {plain(driver)}" for name, driver in info["gpu"]) or unknown),
                      ("disks", "\n".join(map(plain, info["disks"])) or unknown), ("root", root))),
        ("runtime", (("runtime", plain(info["runtime"])),
                     ("packages", "\n".join(map(plain, info["packages"])) or unknown),
                     ("collected", _diagnostic_time(info["collected"])))),
    )


def _system_report(info, lang):
    return "\n\n".join(
        f"{tr('diag_' + section, lang)}\n" + "\n".join(
            f"{tr('diag_' + key, lang)}: {value}" for key, value in fields
        )
        for section, fields in _system_info_sections(info, lang)
    )


_DIAG_SECRET = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\b[\"']?[ \t]*:[ \t]*)[^\n]+"
    r"|(\b(?:password|passwd|passphrase|token|access[_-]token|refresh[_-]token|"
    r"api[_-]?key|client[_-]secret|secret|machine[_-]id)\b[\"']?[ \t]*[:=][ \t]*)"
    r"""(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s,;]+)"""
)
_DIAG_IPV4 = r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
_DIAG_IP = re.compile(
    rf"(?i)(?<![\w:.])(?:[0-9a-f]{{0,4}}:){{2,7}}(?:{_DIAG_IPV4}|[0-9a-f]{{0,4}})"
    rf"(?:%[a-z0-9_.-]+)?(?![\w:]|\.[0-9])|(?<![\w.]){_DIAG_IPV4}(?!\w|\.[0-9])"
)
_DIAG_MAC = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![\w:])")
_DIAG_HOME = re.compile(r"(?<![\w.])/(?:home|var/home)/[^/\s\"'<>]+")
_DIAG_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
_DIAG_PEM = re.compile(r"-----BEGIN [^\n-]*PRIVATE KEY-----.*?(?:-----END [^\n-]*PRIVATE KEY-----|\Z)", re.DOTALL)
_DIAG_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)(?:[^\s/@]+@)?([^\s?#]+)(?:[?#][^\s]*)?")


def _redact_diagnostic(text, marker, home, hostname):
    if home and home != "/":
        text = re.sub(re.escape(home) + r"(?=/|[\s\"'<>]|$)", lambda _: marker, text)
    if hostname and hostname.lower() != "localhost":
        text = re.sub(r"(?<![\w-])" + re.escape(hostname) + r"(?![\w-])",
                      lambda _: marker, text, flags=re.IGNORECASE)
    text = _DIAG_HOME.sub(lambda _: marker, text)
    text = _DIAG_URL.sub(
        lambda match: match[1] + match[2] + (marker if match[0] != match[1] + match[2] else ""),
        text,
    )
    text = _DIAG_EMAIL.sub(lambda _: marker, text)
    text = _DIAG_SECRET.sub(lambda match: (match[1] or match[2]) + marker, text)
    text = _DIAG_MAC.sub(lambda _: marker, text)

    def mask_ip(match):
        try:
            ipaddress.ip_address(match[0])
        except ValueError:
            return match[0]
        return marker

    return _DIAG_IP.sub(mask_ip, text)


class _Job(QRunnable):
    """Run a local operation and always deliver completion on its receiver's thread."""

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.cancel = threading.Event()
        self.signals = _DetailsSignals()

    def run(self):
        try:
            result = self.operation(self.cancel)
        except Exception:
            # Failures may contain private paths or tool output.
            result = None
        self.signals.completed.emit(result)


def _write_csv(path, headers, rows):
    file = QSaveFile(path)
    if not file.open(QIODevice.OpenModeFlag.WriteOnly):
        return False
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(map(_csv_safe, row))
        if buffer.tell() >= 65536:
            chunk = buffer.getvalue().encode("utf-8")
            if file.write(chunk) != len(chunk):
                file.cancelWriting()
                return False
            buffer.seek(0)
            buffer.truncate()
    chunk = buffer.getvalue().encode("utf-8")
    if file.write(chunk) != len(chunk):
        file.cancelWriting()
        return False
    return file.commit()


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
        # Qt 6.10+ can update only rows; keep the Qt 6.5-compatible fallback.
        begin_change = getattr(self, "beginFilterChange", None)
        end_change = getattr(self, "endFilterChange", None)
        modern_api = callable(begin_change) and callable(end_change)
        if modern_api:
            begin_change()
        self._text = text.casefold().strip()
        self._terms = self._text.split()
        self._source_key = source_key
        self._action_key = action_key
        if modern_api:
            end_change(QSortFilterProxyModel.Direction.Rows)
        else:
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
    _TITLE_GAP = 8
    _COUNT_GAP = 4
    _DATE_GAP = 6
    _VERTICAL_MARGIN = 14

    def __init__(self):
        super().__init__()
        self.data = []  # list of (iso_date, count)
        self.tooltips_enabled = _TOOLTIPS_ENABLED
        self._hover = None            # index of the bar under the cursor
        self._plot = None             # cached geometry for hit-testing
        self.setMouseTracking(True)

    def set_data(self, data):
        self.data = data
        self._hover = None
        self._plot = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _title_font(self):
        font = QFont(self.font())
        font.setBold(True)
        return font

    def minimumSizeHint(self):
        title_height = QFontMetrics(self._title_font(), self).height()
        label_height = self.fontMetrics().height()
        spacing = (2 * self._VERTICAL_MARGIN + self._TITLE_GAP
                   + self._COUNT_GAP + self._DATE_GAP)
        return QSize(0, max(180, title_height + 2 * label_height + spacing + 80))

    def changeEvent(self, event):
        if event.type() == QEvent.Type.FontChange:
            self.updateGeometry()
        super().changeEvent(event)

    # ----- Hover interaction ------------------------------------------------
    def _bar_at(self, pos):
        """Return the data index for the column under a point, or None."""
        if not self._plot or not self.data:
            return None
        plot, step = self._plot
        if not plot.contains(pos) or pos.x() >= plot.right():
            return None
        return min(int((pos.x() - plot.left()) // step), len(self.data) - 1)

    def mouseMoveEvent(self, event):
        idx = self._bar_at(event.position())
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.CursorShape.PointingHandCursor if idx is not None else Qt.CursorShape.ArrowCursor)
            self.update()
        if idx is not None and self.tooltips_enabled:
            day, count = self.data[idx]
            hint = _esc(tr("chart_hint"))
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"<b>{_esc(day)}</b><br>{count} &times;<br>{hint}", self)
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
        text_color = pal.color(QPalette.ColorRole.Text)
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

        inner = QRectF(self.rect()).adjusted(
            17, self._VERTICAL_MARGIN, -17, -self._VERTICAL_MARGIN)

        title_font = self._title_font()
        title_height = QFontMetrics(title_font, self).height()
        labels = self.fontMetrics()
        label_height = labels.height()
        title = QRectF(inner.left(), inner.top(), inner.width(), title_height)
        painter.setFont(title_font)
        painter.setPen(text_color)
        painter.drawText(
            title,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            tr("chart_title"),
        )
        painter.setFont(self.font())

        plot_top = title.bottom() + self._TITLE_GAP + label_height + self._COUNT_GAP
        plot_bottom = inner.bottom() - self._DATE_GAP - label_height
        plot = QRectF(inner.left(), plot_top, inner.width(), plot_bottom - plot_top)
        if not self.data or plot.isEmpty():
            self._plot = None
            if not self.data:
                message = inner.adjusted(0, title_height + self._TITLE_GAP, 0, 0)
                painter.drawText(message, Qt.AlignmentFlag.AlignCenter,
                                 tr("chart_no_data"))
            painter.end()
            return

        n = len(self.data)
        max_count = max(c for _, c in self.data) or 1
        step = plot.width() / n
        gap = min(8.0, step * 0.25)
        bar_w = step - gap
        self._plot = (plot, step)

        # Faint baseline for the bars to sit on.
        baseline = _mix(base, text_color, 0.12)
        painter.setPen(baseline)
        painter.drawLine(int(plot.left()), int(plot.bottom()) + 1,
                         int(plot.right()), int(plot.bottom()) + 1)
        painter.setPen(Qt.PenStyle.NoPen)

        # Gradient endpoints: a vivid accent on top fading to a translucent
        # version at the baseline, giving the bars depth on any theme.
        grad_bottom = _mix(accent, base, 0.45)
        hover_top = _mix(accent, QColor("#ffffff"), 0.25)
        for i, (_, count) in enumerate(self.data):
            bar_h = min(plot.height(), max(2.0, plot.height() * count / max_count))
            slot_left = plot.left() + i * step
            x = slot_left + gap / 2
            y = plot.bottom() - bar_h
            rect = QRectF(x, y, bar_w, bar_h)
            hovered = (i == self._hover)
            grad = QLinearGradient(x, y, x, plot.bottom())
            grad.setColorAt(0.0, hover_top if hovered else accent)
            grad.setColorAt(1.0, accent if hovered else grad_bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            painter.drawPath(self._top_rounded(rect, min(4.0, bar_w / 2)))
            label = str(count)
            if labels.horizontalAdvance(label) + 4 <= step:
                painter.setPen(text_color)
                painter.drawText(
                    QRectF(slot_left + 2, y - self._COUNT_GAP - label_height,
                           step - 4, label_height),
                    Qt.AlignmentFlag.AlignCenter, label,
                )
                painter.setPen(Qt.PenStyle.NoPen)

        # Give the outer dates priority when there is room for only two.
        painter.setPen(text_color)
        date_rects = []
        for i in dict.fromkeys((0, n - 1, n // 2)):
            day = self.data[i][0][5:]  # ISO date, displayed as MM-DD
            width = labels.horizontalAdvance(day) + 4
            if width > plot.width():
                continue
            center = plot.left() + (i + 0.5) * step
            left = max(plot.left(), min(center - width / 2, plot.right() - width))
            rect = QRectF(left, plot.bottom() + self._DATE_GAP, width, label_height)
            if any(rect.adjusted(-4, 0, 4, 0).intersects(other) for other in date_rects):
                continue
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, day)
            date_rects.append(rect)
        painter.end()


class _DiagnosticLogModel(QAbstractTableModel):
    def __init__(self, lang, parent):
        super().__init__(parent)
        self.lang = lang
        self.rows = ()

    def replace(self, rows):
        self.beginResetModel()
        self.rows = tuple(rows)
        self.endResetModel()

    def rowCount(self, parent=_ROOT_INDEX):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=_ROOT_INDEX):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(("diag_time", "diag_level", "diag_origin", "diag_message")[section], self.lang)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self.rows[index.row()]
        column = index.column()
        if column == 0:
            return _diagnostic_time(row.stamp)
        if column == 1:
            return _log_priority(row, self.lang)
        if column == 2:
            return row.origin or "—"
        return _log_message(row, self.lang).split("\n", 1)[0]


def _diagnostic_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _diagnostic_button(text, action):
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.clicked.connect(action)
    return button


def _report_log_block(row, lang):
    detail = " · ".join((_diagnostic_time(row.stamp), _log_priority(row, lang), row.origin or "—"))
    return f"{detail}\n{_log_message(row, lang)}"


def _format_diagnostic_report(header, rows, info, lang, home, hostname):
    """Fit whole records into the initial preview, newest first when selecting."""
    def clean(text):
        return _redact_diagnostic(text, tr("diag_redacted", lang), home, hostname)

    base = clean(header)
    if info:
        base += "\n\n" + clean(_system_report(info, lang))
    blocks = []
    used = len(base) + 256  # Allow room for the count and truncation status.
    for row in rows:
        if used >= _DIAG_REPORT_LIMIT:
            break
        block = clean(_report_log_block(row, lang))
        if used + len(block) + 2 <= _DIAG_REPORT_LIMIT:
            blocks.append(block)
            used += len(block) + 2
        else:
            break
    if rows:
        base += "\n\n" + tr("diag_report_count", lang).format(n=len(blocks), total=len(rows))
        if len(blocks) < len(rows):
            base += "\n" + tr("diag_preview_limit", lang)
    # A private-key delimiter can span several journal records.
    return _DIAG_PEM.sub(lambda _: tr("diag_redacted", lang),
                         "\n\n".join((base, *reversed(blocks))))


class _DiagnosticPreview(QDialog):
    def __init__(self, text, lang, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("diag_preview", lang))
        self.resize(720, 510)
        layout = QVBoxLayout(self)
        layout.addWidget(_diagnostic_label(tr("diag_review", lang)))
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName(tr("diag_preview", lang))
        self.editor.setPlainText(text)
        layout.addWidget(self.editor, 1)
        buttons = QHBoxLayout()
        self.copy_status = _diagnostic_label()
        buttons.addWidget(self.copy_status, 1)
        self.copy_button = _diagnostic_button(tr("diag_copy", lang), self._copy)
        self.copy_button.setObjectName("primary")
        buttons.addWidget(self.copy_button)
        buttons.addWidget(_diagnostic_button(tr("diag_close", lang), self.close))
        layout.addLayout(buttons)
        self.lang = lang
        self.editor.textChanged.connect(self._text_changed)
        self.editor.setFocus()

    def _text_changed(self):
        self.copy_status.clear()
        self.copy_button.setEnabled(bool(self.editor.toPlainText().strip()))

    def _copy(self):
        QApplication.clipboard().setText(self.editor.toPlainText())
        self.copy_status.setText(tr("diag_copied", self.lang))


class DiagnosticWindow(QDialog):
    def __init__(self, pool, pacman_path, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.lang = _LANG
        self.pool = pool
        self.pacman_path = pacman_path
        self.info = None
        self.result = None
        self.query = None
        self.searchable = ()
        self.revisions = {"info": 0, "logs": 0}
        self.job = None
        self.pending = OrderedDict()

        self.setWindowTitle(self.label("title"))
        self.setWindowIcon(QIcon.fromTheme("utilities-system-monitor"))
        self.resize(930, 640)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._info_tab(), self.label("info"))
        tabs.addTab(self._log_tab(), self.label("logs"))
        layout.addWidget(tabs, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(_diagnostic_button(self.label("close"), self.close))
        layout.addLayout(footer)

        find = QShortcut(QKeySequence.StandardKey.Find, self.log_page)
        find.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.activated.connect(self.search.setFocus)
        copy = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        copy.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy.activated.connect(lambda: self._prepare_report("selected"))
        refresh = QShortcut(QKeySequence("F5"), self)
        refresh.activated.connect(
            lambda: self._refresh_info() if tabs.currentIndex() == 0 else self._load_logs()
        )
        self.finished.connect(self._cancel_work)
        self._refresh_info()

    def label(self, key):
        return tr("diag_" + key, self.lang)

    def _info_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.info_status = _diagnostic_label()
        bar.addWidget(self.info_status, 1)
        self.refresh = _diagnostic_button(self.label("refresh"), self._refresh_info)
        bar.addWidget(self.refresh)
        layout.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.info_layout = QVBoxLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.prepare_info = _diagnostic_button(self.label("prepare_info"), lambda: self._prepare_report("info"))
        self.prepare_info.setEnabled(False)
        layout.addWidget(self.prepare_info, alignment=Qt.AlignmentFlag.AlignRight)
        return page

    @staticmethod
    def _combo(grid, column, title):
        label = _diagnostic_label(title)
        combo = QComboBox()
        combo.setAccessibleName(title)
        label.setBuddy(combo)
        grid.addWidget(label, 0, column)
        grid.addWidget(combo, 1, column)
        return combo

    def _log_tab(self):
        page = self.log_page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        self.source = self._combo(grid, 0, self.label("source"))
        self.period = self._combo(grid, 1, self.label("period"))
        self.priority = self._combo(grid, 2, self.label("level"))
        self.limit = self._combo(grid, 3, self.label("limit"))
        for source in _DIAG_SOURCES:
            self.source.addItem(self.label(source), source)
        for priority in _DIAG_PRIORITIES:
            self.priority.addItem(self.label(priority), priority)
        for limit in _DIAG_LIMITS:
            self.limit.addItem(str(limit), limit)
        self.limit.setCurrentIndex(1)
        period_width = max(self.period.fontMetrics().horizontalAdvance(self.label(key))
                           for key in ("current", "previous", "hour", "day", "week", "latest"))
        priority_width = max(self.priority.fontMetrics().horizontalAdvance(self.label(key))
                             for key in _DIAG_PRIORITIES)
        self.period.setStyleSheet(f"QComboBox {{ min-width: {period_width + 24}px; }}")
        self.priority.setStyleSheet(f"QComboBox {{ min-width: {priority_width + 24}px; }}")
        self.limit.setStyleSheet("QComboBox { min-width: 45px; }")
        grid.setColumnStretch(1, 1)
        grid.addWidget(_diagnostic_button(self.label("load"), self._load_logs), 1, 4)
        layout.addLayout(grid)
        self.scope_hint = _diagnostic_label()
        layout.addWidget(self.scope_hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.label("filter"))
        self.search.setAccessibleName(self.label("filter"))
        self.search.setMaxLength(256)
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.log_status = _diagnostic_label()
        layout.addWidget(self.log_status)

        self.log_model = _DiagnosticLogModel(self.lang, self)
        self.table = QTableView()
        self.table.setAccessibleName(self.label("logs"))
        self.table.setModel(self.log_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        timestamp_width = self.table.fontMetrics().horizontalAdvance("2000-01-01 00:00:00+00:00") + 42
        for col, width in enumerate((timestamp_width, 100, 165)):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 3)

        self.details = QPlainTextEdit()
        self.details.setAccessibleName(self.label("message"))
        self.details.setReadOnly(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.details.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.details.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.details.setMinimumHeight(78)
        layout.addWidget(self.details, 1)

        buttons = QGridLayout()
        self.include = QCheckBox(self.label("include"))
        self.include.setEnabled(False)
        buttons.addWidget(self.include, 0, 0, 1, 2)
        self.prepare_selected = _diagnostic_button(self.label("selected"), lambda: self._prepare_report("selected"))
        self.prepare_visible = _diagnostic_button(self.label("visible"), lambda: self._prepare_report("visible"))
        buttons.setColumnStretch(0, 1)
        buttons.addWidget(self.prepare_selected, 1, 1)
        buttons.addWidget(self.prepare_visible, 1, 2)
        layout.addLayout(buttons)

        self.source.currentIndexChanged.connect(self._source_changed)
        for combo in (self.period, self.priority, self.limit):
            combo.currentIndexChanged.connect(self._invalidate_logs)
        self.search_timer = QTimer(self, singleShot=True, interval=150)
        self.search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(self._search_changed)
        self.table.selectionModel().currentChanged.connect(self._show_entry)
        self.table.selectionModel().selectionChanged.connect(self._update_copy_actions)
        self._source_changed()
        return page

    def _source_changed(self):
        pacman = self.source.currentData() == "pacman"
        self.period.blockSignals(True)
        self.period.clear()
        for key in (("latest",) if pacman else ("current", "previous")) + tuple(_DIAG_HOURS):
            self.period.addItem(self.label(key), key)
        self.period.blockSignals(False)
        self.priority.setEnabled(not pacman)
        self.scope_hint.setText(self.label("pacman_hint" if pacman else "journal_hint"))
        self._invalidate_logs()

    def _invalidate_logs(self):
        self._invalidate("logs")
        self.result = self.query = None
        self.searchable = ()
        self._apply_search()

    def _invalidate(self, kind):
        self.revisions[kind] += 1
        self.pending.pop(kind, None)
        if self.job is not None and self.job[0] == kind:
            self.job[2].cancel.set()

    def _cancel_work(self):
        for kind in self.revisions:
            self._invalidate(kind)

    def _queue(self, kind, operation):
        self._invalidate(kind)
        self.pending[kind] = (self.revisions[kind], operation)
        self._start_next()

    def _start_next(self):
        if self.job is not None or not self.pending:
            return
        kind, (revision, operation) = self.pending.popitem(last=False)
        job = _Job(operation)
        self.job = (kind, revision, job)
        job.signals.completed.connect(self._job_finished)
        self.pool.start(job, 1)

    def _job_finished(self, value):
        kind, revision, _ = self.job
        self.job = None
        if self.revisions[kind] == revision:
            if kind == "info":
                self._show_info(value)
            else:
                self._show_logs(value)
        self._start_next()

    def _refresh_info(self):
        self.info_status.setText(self.label("loading"))
        self.refresh.setEnabled(False)
        self.prepare_info.setEnabled(False)
        self.include.setEnabled(False)
        platform_name = QApplication.platformName()
        self._queue("info", lambda cancel: _collect_system_info(platform_name, cancel))

    def _show_info(self, info):
        self.info = info
        self.refresh.setEnabled(True)
        self.prepare_info.setEnabled(bool(info))
        self.include.setEnabled(bool(info))
        self.info_status.setText("" if info else self.label("failed"))
        while self.info_layout.count():
            item = self.info_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for section, fields in _system_info_sections(info, self.lang):
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            form = QFormLayout(card)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            heading = _diagnostic_label(self.label(section))
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            form.addRow(heading)
            for key, value in fields:
                field = _diagnostic_label(value)
                if key == section:
                    form.addRow(field)
                else:
                    form.addRow(_diagnostic_label(self.label(key)), field)
            self.info_layout.addWidget(card)
        self.info_layout.addStretch()

    def _load_logs(self):
        self._invalidate_logs()
        query = _LogQuery(
            self.source.currentData(), self.period.currentData(),
            self.priority.currentData() if self.source.currentData() != "pacman" else "all",
            self.limit.currentData(),
        )
        self.query = query
        self.log_status.setText(self.label("loading"))
        path = self.pacman_path
        self._queue("logs", lambda cancel: _collect_diagnostic_logs(query, path, cancel))

    def _show_logs(self, value):
        self.result = value if value is not None else _LogResult(status="failed", collected=_utc_now())
        self.searchable = tuple(" ".join((
            row.origin, row.message or "", _diagnostic_time(row.stamp), _log_priority(row, self.lang),
        )).casefold() for row in self.result.rows)
        self._apply_search()

    def _search_changed(self):
        self.search_timer.start()
        self._update_copy_actions()

    def _apply_search(self):
        self.search_timer.stop()
        self.details.clear()
        if self.result is None:
            self.log_model.replace(())
            self.log_status.setText(self.label("loading" if self.query else "idle"))
        else:
            tokens = self.search.text().casefold().split()
            rows = [row for row, search in zip(self.result.rows, self.searchable)
                    if all(token in search for token in tokens)]
            self.log_model.replace(rows)
            self.log_status.setText(self._result_status(len(rows)))
        self._update_copy_actions()

    def _result_status(self, shown):
        result = self.result
        notes = [self.label("match").format(shown=shown, loaded=len(result.rows))]
        if result.status:
            notes.append(self.label(result.status))
        elif not result.rows:
            notes.append(self.label("empty"))
        if result.capped:
            notes.append(self.label("capped"))
        if result.skipped:
            notes.append(self.label("skipped").format(n=result.skipped))
        return "  ·  ".join(notes)

    def _show_entry(self, current, previous):
        self.details.setPlainText(
            _report_log_block(self.log_model.rows[current.row()], self.lang) if current.isValid() else ""
        )

    def _update_copy_actions(self):
        ready = bool(self.log_model.rows) and not self.search_timer.isActive()
        self.prepare_visible.setEnabled(ready)
        self.prepare_selected.setEnabled(ready and self.table.selectionModel().hasSelection())

    def _prepare_report(self, kind):
        base = "\n\n".join((self.label("report"), self.label("problem") + "\n", self.label("steps") + "\n"))
        if kind == "info":
            if not self.prepare_info.isEnabled():
                return
            rows = ()
            info = self.info
        else:
            allowed = self.prepare_selected if kind == "selected" else self.prepare_visible
            if not allowed.isEnabled():
                return
            query = self.query
            rows = self.log_model.rows
            if kind == "selected":
                indexes = sorted(index.row() for index in self.table.selectionModel().selectedRows())
                rows = tuple(rows[index] for index in indexes)
            info = self.info if self.include.isEnabled() and self.include.isChecked() else None
            query_details = [self.label(query.source), self.label(query.period)]
            if query.source != "pacman":
                query_details.append(self.label(query.priority))
            base += f"\n\n{self.label('query')}: {' · '.join(query_details)} · {self.label('limit')} {query.limit}"
            base += f"\n{self.label('collected')}: {_diagnostic_time(self.result.collected)}"
            if self.search.text().strip():
                base += f"\n{self.label('search_used')}: {_diagnostic_text(self.search.text().strip())}"
            base += "\n" + self._result_status(len(self.log_model.rows))
        text = _format_diagnostic_report(base, rows, info, self.lang, os.path.expanduser("~"),
                                         socket.gethostname())
        _DiagnosticPreview(text, self.lang, self).open()


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
        self._export_worker = None
        self._diagnostics = None
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
        header.setSpacing(4)
        header_row = QHBoxLayout()
        self.title_label = QLabel(tr("title_label"))
        tf = QFont(self.font())
        tf.setPointSizeF(self.font().pointSizeF() + 5)
        tf.setBold(True)
        self.title_label.setFont(tf)
        header_row.addWidget(self.title_label, 1)
        self.diagnostics_btn = QPushButton(tr("diag_title"))
        self.diagnostics_btn.setIcon(QIcon.fromTheme("utilities-system-monitor"))
        self.diagnostics_btn.clicked.connect(self.show_diagnostics)
        header_row.addWidget(self.diagnostics_btn)

        self.lang_btn = QPushButton(_LANG_NAMES[_LANG])
        self.lang_btn.setIcon(QIcon.fromTheme("preferences-desktop-locale"))
        self.lang_btn.clicked.connect(self.switch_language)
        header_row.addWidget(self.lang_btn)
        header.addLayout(header_row)

        self.subtitle_label = QLabel(tr("subtitle"))
        self.subtitle_label.setObjectName("subtitle")
        self.subtitle_label.setWordWrap(True)
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

        controls.setSpacing(8)
        controls.addWidget(self.search_input, 1)
        controls.addWidget(self.source_combo)
        controls.addWidget(self.action_combo)
        controls.addWidget(self.tooltip_btn)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.export_btn)
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
        self.version_label = QLabel(tr("diag_version").format(version=__version__))
        self.status.addPermanentWidget(self.version_label)
        self.load_data()

    def show_diagnostics(self):
        if self._diagnostics is None:
            self._diagnostics = DiagnosticWindow(self._worker_pool, self.log_path, self)
            self._diagnostics.finished.connect(self._diagnostics_finished)
            self._diagnostics.open()
        else:
            self._diagnostics.raise_()
            self._diagnostics.activateWindow()

    def _diagnostics_finished(self):
        self._diagnostics = None

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
        self.diagnostics_btn.setText(tr("diag_title"))
        self.version_label.setText(tr("diag_version").format(version=__version__))
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
                    out.append(f"<nobr><span style='color:{color};'>●</span>&nbsp;"
                               f"{label}:&nbsp;<b>{counts[key]}</b></nobr>")
            return " &nbsp; ".join(out)

        html = f"<b>{tr('stats_total')}:&nbsp;{total}</b>"
        src_html = chips(src_counts, SOURCES, "src_")
        act_html = chips(act_counts, ACTIONS, "act_")
        if src_html:
            html += " &nbsp; " + src_html
        if act_html:
            html += "<br>" + act_html
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
        if self._export_worker is not None:
            return
        if not self.proxy.rowCount():
            QMessageBox.information(self, tr("export_dlg"), tr("export_nothing"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("save_as_csv"), "cachyos-update-history.csv",
            "CSV files (*.csv)")
        if not path:
            return
        rows = []
        for r in range(self.proxy.rowCount()):
            rec = self._rec_at(self.proxy.index(r, 0))
            rows.append(tuple(rec[key] for key in self.model._COLS))
        if not rows:
            QMessageBox.information(self, tr("export_dlg"), tr("export_nothing"))
            return
        headers = tuple(tr("headers"))
        worker = _Job(lambda _: (_write_csv(path, headers, rows), path, len(rows)))
        worker.signals.completed.connect(self._export_completed)
        self._export_worker = worker
        self.export_btn.setEnabled(False)
        self.status.showMessage(tr("export_progress"))
        self._worker_pool.start(worker, 1)

    def _export_completed(self, result):
        self._export_worker = None
        self.export_btn.setEnabled(True)
        if result and result[0]:
            self.status.showMessage(tr("exported").format(n=result[2], path=result[1]), 8000)
        else:
            self.status.clearMessage()
            QMessageBox.warning(self, tr("export_fail"), tr("export_write_error"))


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
