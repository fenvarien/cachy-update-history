# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Standard-library regression tests for configuration persistence, untrusted
  input, source parsing, filtering, tooltip cache behavior and internal
  documentation links.
- Maintainer documentation covering architecture, contribution workflow,
  security policy, release history and documentation ownership.
- Structured GitHub templates for bug reports, feature requests and pull
  requests.

### Changed

- Pacman, Flatpak and package-detail I/O now runs in a bounded Qt worker pool so
  slow commands do not block the interface.
- Search uses precomputed case-folded text per record and language.
- Tooltip work is bounded by a 256-entry LRU cache, a 12-request queue and three
  worker threads.
- The documented minimum runtime is Python 3.10; PyQt6 remains within major
  version 6.
- The desktop entry uses `python3` and one standards-compliant main category.
- Project documentation is split into stable entry, user, architecture,
  security, contribution and release-history documents.

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
