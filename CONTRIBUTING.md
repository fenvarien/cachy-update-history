# Contributing

Thanks for improving CachyOS Update History. Small, focused changes are easiest
to review and safest for a local system-information application.

## Before starting

- Search existing issues and pull requests for overlapping work.
- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing data
  collection, threading, security boundaries or the normalized record schema.
- Use [SECURITY.md](SECURITY.md) instead of a public issue for a potentially
  exploitable vulnerability.
- Keep optional package managers optional and avoid requiring root.

## Development setup

Python 3.11 or newer is supported; use the latest security patch in your series.
On CachyOS/Arch, prefer the system PyQt6:

```text
sudo pacman -S python-pyqt6
git clone https://github.com/fenvarien/cachy-update-history.git
cd cachy-update-history
```

On another distribution, use an isolated interpreter without activating the
environment:

```text
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use `python3` below for a system installation or `.venv/bin/python` for the
virtual environment.

## Repository map

| Path | Role |
| --- | --- |
| `cachy-update-history.py` | Complete runtime application. |
| `tests/test_app.py` | Application security/parser/performance regressions. |
| `tests/test_docs.py` | Internal Markdown link, anchor and index checks. |
| `cachy-update-history.desktop` | Optional freedesktop launcher template. |
| `requirements.txt` | Runtime dependency range for virtual environments. |
| `README.md` | Stable project overview and quick start. |
| `docs/USER_GUIDE.md` | User workflows, source semantics and troubleshooting. |
| `docs/ARCHITECTURE.md` | Maintained implementation constraints and extension points. |
| `CHANGELOG.md` | Release history and pending user-visible changes. |
| `SECURITY.md` | Supported versions, controls and vulnerability reporting. |

## Change workflow

1. Start from an up-to-date `master` and create a descriptive branch.
2. Keep code, tests and documentation changes in one coherent scope.
3. Preserve existing behavior unless the pull request explains the intentional
   compatibility change.
4. Run the checks below and manually exercise affected UI paths.
5. Add a concise entry under `CHANGELOG.md` → `Unreleased` for user-visible,
   security, dependency or architectural changes.
6. Open a pull request using the repository template.

Use conventional, imperative commit subjects where practical, for example:

```text
fix: keep removed Flatpak rows out of the current-app view
docs: explain AUR classification limits
```

## Required checks

GitHub Actions (`.github/workflows/ci.yml`) compiles the sources and runs the
regression suite on Python 3.11, 3.14 and 3.15 for every pull request and push to
`master`. Run the same checks locally before pushing.

Run the regression suite:

```text
python3 -m unittest discover -s tests -v
```

Compile the application and tests:

```text
python3 -m py_compile cachy-update-history.py tests/test_app.py tests/test_docs.py
```

When the desktop entry changes and `desktop-file-utils` is available:

```text
desktop-file-validate cachy-update-history.desktop
```

Run the application in a real desktop session for changes affecting loading,
filtering, sorting, tooltips, translations, theming, export or chart behavior:

```text
python3 cachy-update-history.py
```

The repository currently has no test-only dependency and no mandatory formatter
or type checker. Do not claim these checks ran unless the relevant tools are
actually installed and invoked.

## Code and architecture rules

- Keep Qt widget/model mutations in the GUI thread.
- Keep filesystem and subprocess I/O in workers.
- Never pass untrusted values through a shell command string.
- Use explicit subprocess timeouts and stable output formats/locales.
- Validate identifiers before filesystem, HTML or export use.
- Bound input, queues and caches where system/user data controls their size.
- Preserve model/view rendering; do not create per-cell widgets.
- Prefer Python, Qt and existing helpers over new dependencies.
- Add comments for reasons and invariants, not restatements of obvious code.

Broad refactors of the single runtime file should establish a concrete module
boundary and preserve direct `python3 cachy-update-history.py` execution.

## Tests

`tests/test_app.py` loads the hyphenated script with `importlib`. Tests must not
modify the real user configuration or depend on the host's installed package
set. Use temporary directories and mocks for filesystem/command boundaries.
`tests/test_docs.py` keeps internal Markdown paths and section anchors valid.

For a bug fix, add a regression test that fails for the old behavior whenever a
stable non-GUI seam exists. Prioritize:

- malformed or adversarial input;
- missing commands/files and non-zero exits;
- timestamp/version/source normalization;
- resource-bound behavior;
- translated/filterable data;
- command argument construction.

GUI integration should be checked manually or in an offscreen Qt smoke test in
addition to pure unit tests.

## Translations and screenshots

User-facing strings belong in `_T` and require matching English (`en`) and
German (`de`) entries. Test both languages when labels influence filtering or
cached content.

Keep documentation prose in English to avoid two independently drifting manuals.
Update both screenshots together when a visual change materially affects the
documented interface.

## Documentation maintenance

Update documentation in the same pull request as the behavior it describes:

| Change | Required documentation |
| --- | --- |
| User workflow, configuration or troubleshooting | `docs/USER_GUIDE.md`, `CHANGELOG.md` |
| Data source, record schema, threading or cache | `docs/ARCHITECTURE.md`, user guide if visible, changelog |
| Security boundary or supported runtime | `SECURITY.md`, README summary, changelog |
| Development/test process | `CONTRIBUTING.md`, PR template if its checklist changes |
| New stable release | Finalize changelog section, verify README links/badges and release notes |
| Material UI redesign | README text and both screenshots |

README should remain a stable entry point. Put detailed operational information
in the user guide and implementation details in the architecture document.

## Pull requests

A pull request should explain:

- the observed problem or intended outcome;
- why the chosen solution fits this project;
- security, performance and compatibility effects;
- tests and manual checks actually run;
- remaining risks or follow-up work.

Avoid unrelated formatting, refactoring or dependency changes. Reviewers should
be able to map each changed file to the stated objective.

## Release checklist for maintainers

1. Ensure `master` is clean and all required checks pass.
2. Move `Unreleased` entries into a versioned, dated changelog section and add a
   fresh empty `Unreleased` section.
3. Confirm the supported Python/PyQt range and screenshots.
4. Create an annotated version tag following Semantic Versioning.
5. Publish GitHub release notes based on the changelog and link the relevant
   documentation.
6. Verify the release badge and compare links after publication.

Commits, tags and releases should only be created with explicit maintainer
authorization.

By submitting a contribution, you agree that it can be distributed under the
project's [GNU General Public License v3.0](LICENSE).
