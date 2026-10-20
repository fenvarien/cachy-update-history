# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GitHub Actions CI compiling the sources and running the test suite on
  Python 3.10, 3.14 and 3.15 for pull requests and pushes to `master`.
- Multi-token and multi-field search allowing users to filter by package,
  versions (old and new), date/time, source, and action in any term order.
- Interactive activity chart enabling users to click any date bar to instantly
  filter the table by that specific day.
- Dedicated Package Details dialog with clickable project URLs, full metadata,
  and quick clipboard copy actions accessible via double-click, Enter, or the
  context menu.
- Table context menu and keyboard shortcuts: Ctrl+F / / (focus search), Ctrl+C
  in the table (copy selected package names), F5 / Ctrl+R (reload),
  Ctrl+S / Ctrl+E (export CSV), and Escape (clear search or selection).
- Command-line flags `--version` (`-v`) and `--help` (`-h`).
- Freedesktop application launcher German localization (`Name[de]`,
  `GenericName[de]`, `Comment[de]`, `Keywords[de]`).
- Standard-library regression tests for configuration persistence, untrusted
  input, source parsing, filtering, tooltip cache behavior and internal
  documentation links.
- Maintainer documentation covering architecture, contribution workflow,
  security policy, release history and documentation ownership.
- Structured GitHub templates for bug reports, feature requests and pull
  requests.

### Changed

- Streamed Pacman log parsing is roughly 4x faster (measured on a 2.5 MB,
  12,500-entry log) via ISO-8601 fast-path decoding and non-ALPM line
  pre-filtering.
- Yearless Flatpak timestamps are parsed without `strptime("%b")`, avoiding the
  Python 3.14+ deprecation warning for yearless dates.
- Dynamic system palette and dark/light mode switching without requiring an
  application restart.
- Flatpak deploy timestamp discovery checks directory presence before globbing.
- Pacman, Flatpak and package-detail I/O now runs in a bounded Qt worker pool so
  slow commands do not block the interface.
- Search uses precomputed case-folded text per record and language.
- Tooltip work is bounded by a 256-entry LRU cache, a 12-request queue and three
  worker threads.
- The documented minimum runtime is Python 3.10; PyQt6 remains within major
  version 6. Python 3.10 reached end of life in October 2026: it is still
  tested and supported by the code, but upgrading the interpreter is
  recommended (see the [Security policy](SECURITY.md)).
- CI uses `actions/checkout` and `actions/setup-python` v7.
- Source and tests pass the Ruff linter without findings (sorted imports,
  explicit `check=False` on subprocess calls, no behavior changes); the
  application script is now executable.
- The desktop entry uses `python3` and one standards-compliant main category.
- Project documentation is split into stable entry, user, architecture,
  security, contribution and release-history documents.

### Fixed

- On Wayland (and KDE on X11) the window now reports the
  `cachy-update-history` desktop file, so task bars show the launcher's icon
  instead of a generic one. The desktop entry must keep that file name.
- Flatpak history dates from months whose abbreviation differs in the user's
  locale (e.g. "Mar", "May", "Oct", "Dec" on German systems) are no longer
  shown without a date; month names no longer depend on `LC_TIME`, which
  `QApplication` switches to the system locale.
- Yearless Flatpak dates on February 29 resolve to the previous leap year
  instead of being dropped.
- Transient status messages (copied, exported, loading) no longer erase the
  entry summary in the status bar.

### Security

- Configuration loading is size/type bounded; writes use unpredictable private
  temporary files and atomic replacement.
- Flatpak application IDs are validated before path or glob use.
- Package names are separated from command options, and command paths are
  resolved before execution.
- CSV formula protection covers every leading C0 control character.
- Resource use for package-detail caching and queued queries is explicitly
  bounded.

## [1.1.0] - 2026-07-12

### Added

- On-demand Pacman/AUR and Flatpak package-detail tooltips with description,
  version, license, size, origin/repository and other source-specific fields.
- A persistent **Details** toggle for table and chart tooltips.

### Changed

- UI colors and custom drawing derive from the active system palette and accent
  color for native KDE/Breeze and GNOME/Adwaita light/dark themes.
- Activity-chart presentation and hover feedback were refreshed.

## [1.0.0] - 2026-06-27

### Added

- Combined Pacman, current-state AUR and Flatpak update history.
- Source/action badges, combined filters, sorting and per-day activity chart.
- CSV export with formula-injection protection.
- Runtime English/German language switching with persisted preferences.
- Theme-aware PyQt6 interface and English/German screenshots.
- Argument-list subprocess execution, atomic config writes and streamed Pacman
  log parsing.

[Unreleased]: https://github.com/fenvarien/cachy-update-history/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/fenvarien/cachy-update-history/releases/tag/v1.1.0
[1.0.0]: https://github.com/fenvarien/cachy-update-history/releases/tag/v1.0.0
