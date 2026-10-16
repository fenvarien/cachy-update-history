# Claude Reference Guide

This file provides project context, constraints, commands, and a change summary
for Claude Code sessions working on CachyOS Update History.

## Project Summary

CachyOS Update History is a lightweight PyQt6 desktop tool designed for Arch
Linux and CachyOS systems to view package transactions (installs, upgrades,
downgrades, removals) across Pacman, the AUR, and Flatpak on a unified timeline.

## Rules and Invariants

- **Single Runtime Script**: All application code belongs in
  `cachy-update-history.py`. Never split application logic into separate runtime
  modules.
- **Zero Extra Dependencies**: Only `PyQt6` is permitted as a runtime
  dependency. Standard library tools (`unittest`, `csv`, `subprocess`, `json`)
  handle testing, data export, and persistence.
- **Threading Separation**: GUI widgets remain strictly on the main thread; disk
  reads and external commands (`pacman`, `flatpak`) run via `QThreadPool`.
- **Localization**: Bilingual English (`en`) and German (`de`) via `_T`.
- **Safe Subprocess Usage**: Use argument lists (no shell strings) and specify
  timeouts and `LC_ALL=C` environment.
- **Documentation Verification**: All documentation files (`*.md`) are checked
  by `tests/test_docs.py` for valid internal links and strict heading
  hierarchies.

## Common Commands

```text
# Run application
python3 cachy-update-history.py

# Run application with CLI flags
python3 cachy-update-history.py --version
python3 cachy-update-history.py --help

# Run full regression test suite
python3 -W error::DeprecationWarning -m unittest discover -s tests -v

# Compile Python source and test files
python3 -m py_compile cachy-update-history.py tests/test_app.py tests/test_docs.py

# Validate desktop launcher specification
desktop-file-validate cachy-update-history.desktop
```

## Summary of Recent Changes

### Performance improvements

- Fast-path timestamp parsing using `datetime.fromisoformat()` for modern Pacman
  logs; with the ALPM pre-filter, log collection is roughly 4x faster.
- Fast `[ALPM] ` pre-filter skipping non-ALPM lines before regex evaluation.
- Guarded Flatpak deploy mtime discovery to skip missing paths.

### Search and user experience

- Multi-term search with space separation in any order.
- Versions (`old` / `new`) and dates included in record search indices.
- Interactive `ActivityChart`: click a daily bar to filter the table by that
  date; hover cursor indicators and tooltip hints.
- `PackageDetailsDialog`: modal inspection dialog with clickable URLs,
  complete package metadata, and copy actions (double-click, Enter, or context
  menu).
- Table context menu and shortcuts: `Ctrl+F`/`/`, `Ctrl+C` (table only),
  `F5`/`Ctrl+R`, `Ctrl+S`/`Ctrl+E`, `Escape`. Keys are registered explicitly;
  never duplicate a key already contained in a `StandardKey` (ambiguous shortcut).
- Live theme and accent adaptation via `PaletteChange`; `apply_theme()` returns
  `False` for an unchanged stylesheet, which prevents repolish loops.
- The package details dialog loads its data in `_DetailsWorker`, never in the
  GUI thread.

### Platform compatibility and tests

- Yearless Flatpak timestamps use a fixed English month table, not
  `strptime("%b")`: `QApplication` sets `LC_TIME` to the user's locale, which
  broke "Mar"/"Oct"/… on German systems. Feb 29 falls back to the previous year.
- Added localized German metadata to `cachy-update-history.desktop`.
- Added `--version` / `-v` and `--help` / `-h` CLI options.
- Regression tests live in `tests/test_app.py` and `tests/test_docs.py`.

## Documentation Map

- [README.md](README.md): Project overview and setup.
- [User guide](docs/USER_GUIDE.md): Feature descriptions and troubleshooting.
- [Architecture](docs/ARCHITECTURE.md): Component diagrams and data flow.
- [Changelog](CHANGELOG.md): Version history and pending changes.
- [Contributing](CONTRIBUTING.md): Workflow guidelines and requirements.
- [Security policy](SECURITY.md): Threat model and vulnerability handling.
