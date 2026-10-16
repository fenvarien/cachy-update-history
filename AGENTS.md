# Agent Guidelines

This document provides concise, practical architectural rules and a traceable
record of recent changes for autonomous and pair-programming AI coding agents.

## Overview

CachyOS Update History is a standalone, dependency-light PyQt6 application that
aggregates package update events from Pacman (`/var/log/pacman.log`), AUR
(`pacman -Qmq`), and Flatpak (`flatpak history` and `flatpak list`) into a
single sortable timeline.

## Invariants and Constraints

Agents modifying this codebase must adhere to the following constraints:

1. **Single Runtime File**: The entire application lives in
   `cachy-update-history.py`. Documentation and tests are maintained separately.
2. **Minimal Dependencies**: The only external runtime dependency is `PyQt6`
   (`PyQt6>=6.5,<7`). No external data libraries (e.g. pandas, matplotlib) are
   permitted.
3. **Thread Safety**: All Qt widget and model manipulations must remain on the
   main GUI thread. All disk I/O, subprocess invocations, and metadata queries
   must execute in background workers (`QThreadPool`).
4. **Security Boundaries**: Log content and subprocess output are untrusted.
   Commands must use argument lists (never shell strings), explicit timeouts,
   and stable locales (`LC_ALL=C`). CSV cells must neutralize leading formula
   characters (`=`, `+`, `-`, `@`, control characters).
5. **Localization**: All user-facing strings must be defined in both English
   and German dictionaries within `_T`.
6. **Documentation Integrity**: Any behavioral or schema changes must be
   mirrored across `CHANGELOG.md`, `README.md`, and relevant files in `docs/`.

## Verification Commands

Run these checks before finalizing any change:
(CI in `.github/workflows/ci.yml` runs the first two on Python 3.10 and 3.14.)

```text
# Run test suite with deprecation warnings treated as errors
python3 -W error::DeprecationWarning -m unittest discover -s tests -v

# Bytecode compilation check
python3 -m py_compile cachy-update-history.py tests/test_app.py tests/test_docs.py

# Validate freedesktop entry (when desktop-file-utils is available)
desktop-file-validate cachy-update-history.desktop
```

## Summary of Recent Changes

### Performance and parsing

- **ISO-8601 Fast Path**: `parse_timestamp()` utilizes Python's C-level
  `datetime.fromisoformat()` before falling back to `strptime()` loops; together
  with the ALPM pre-check, full log collection is roughly 4x faster.
- **ALPM Log Pre-check**: Streamed log reader filters for `[ALPM] ` before regex
  matching, avoiding unnecessary pattern evaluations on non-ALPM log entries.
- **Flatpak Mtime Scan Guard**: `_flatpak_deploy_mtime()` checks for directory
  existence prior to globbing deployment symlinks.

### Search and filtering

- **Multi-token Querying**: `UpdateFilterProxy` splits search queries into
  individual terms matching against package names, actions, sources, old/new
  versions, and timestamps with logical AND in any order.
- **Expanded Search Surface**: Record `search_text` includes both old and new
  versions and formatted date strings, allowing direct filtering by version or
  date (e.g. `2026-09`).

### UI and interaction

- **Interactive Activity Chart**: `ActivityChart` supports clicking on daily
  activity bars to filter table rows to that specific day, with hover cursor
  cues and guidance tooltips.
- **Package Details Dialog**: `PackageDetailsDialog` opens on double-click,
  `Enter`, or context menu, presenting rich package metadata, clickable website
  hyperlinks, and copy buttons.
- **Context Menu and Clipboard**: Right-click table menu provides quick actions
  to copy package names, versions, or tab-separated rows, and to filter by
  package.
- **Keyboard Shortcuts**: Added standard navigation shortcuts:
  - `Ctrl+F` or `/`: focus and select search field;
  - `Ctrl+C` (table focus only): copy selected package name(s);
  - `F5` or `Ctrl+R`: reload history from disk;
  - `Ctrl+S` or `Ctrl+E`: export to CSV;
  - `Escape`: clear search field or table selection.
- **Dynamic Theme Adaptation**: `MainWindow.changeEvent()` captures
  `PaletteChange` (how Qt 6 delivers a new application palette to windows) and
  re-runs `apply_theme()`, which is a no-op for an unchanged stylesheet and so
  cannot loop. Tooltip cache and stats line are refreshed on change.
- **Shortcut Rule**: Register shortcut keys explicitly. Never combine a
  `QKeySequence.StandardKey` with an explicit key it already contains (e.g.
  `Refresh` already includes `F5` on Linux); duplicates are ambiguous and fire
  neither.

### Platform and compatibility

- **Locale-independent Flatpak dates**: Yearless `flatpak history` timestamps
  are parsed with a fixed English month table instead of `strptime("%b")`.
  `QApplication` switches `LC_TIME` to the user's locale, so `%b` broke on e.g.
  German systems. This also avoids the Python 3.14+ yearless-date
  `DeprecationWarning` and resolves Feb 29 to the previous leap year.
- **Freedesktop Localization**: `cachy-update-history.desktop` provides German
  translations (`Name[de]`, `GenericName[de]`, `Comment[de]`, `Keywords[de]`).
- **CLI Options**: Added `--version` (`-v`) and `--help` (`-h`) command-line
  flags.

### Test coverage

- `tests/test_app.py` covers multi-token search, version/date filtering,
  timestamp fast paths, locale-independent and leap-day Flatpak dates, ALPM
  pre-filtering and CLI flags; `tests/test_docs.py` checks Markdown links and
  heading structure. Run the suite instead of relying on a fixed test count.

## Reference Documentation

- [User guide](docs/USER_GUIDE.md): Operational details and workflows.
- [Architecture](docs/ARCHITECTURE.md): Data flow, threading model, and schemas.
- [Changelog](CHANGELOG.md): Historical releases and pending master changes.
- [Contributing](CONTRIBUTING.md): Detailed development guidelines.
- [Security policy](SECURITY.md): Threat model and vulnerability handling.
