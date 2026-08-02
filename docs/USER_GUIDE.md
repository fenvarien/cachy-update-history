# User guide

This guide describes the behavior of the current `master` branch. For a stable
release, select the matching tag on GitHub and use the documentation stored in
that tag.

## Contents

- [Install and start](#install-and-start)
- [Desktop integration](#desktop-integration)
- [Use the interface](#use-the-interface)
- [Understand the data](#understand-the-data)
- [Configuration](#configuration)
- [CSV export](#csv-export)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Install and start

### CachyOS and Arch Linux

Use the distribution package for PyQt6 so it follows normal system updates:

```text
sudo pacman -S python-pyqt6
git clone https://github.com/fenvarien/cachy-update-history.git
cd cachy-update-history
python3 cachy-update-history.py
```

The application reads `/var/log/pacman.log`, which is normally readable without
root privileges on Arch-based systems. Do not run the application with `sudo`.

### Other distributions

Python 3.10 or newer and a Qt-capable desktop session are required. A virtual
environment keeps the PyQt6 package isolated from the system interpreter:

```text
git clone https://github.com/fenvarien/cachy-update-history.git
cd cachy-update-history
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cachy-update-history.py
```

Pacman data is absent when `/var/log/pacman.log` does not exist. Flatpak data is
still displayed when the `flatpak` command is available.

## Desktop integration

Copy the script and desktop entry to stable per-user locations:

```text
mkdir -p ~/.local/bin ~/.local/share/applications
cp cachy-update-history.py ~/.local/bin/
cp cachy-update-history.desktop ~/.local/share/applications/
```

Then open
`~/.local/share/applications/cachy-update-history.desktop` and replace
`YOUR_USERNAME` in the `Exec=` line with the actual home-directory component.
The resulting command should point to the copied script, for example:

```ini
Exec=python3 /home/alex/.local/bin/cachy-update-history.py
```

The entry normally appears in the system application menu automatically. Some
desktop environments may require signing out and back in after changing menu
entries.

## Use the interface

### Search and filters

The text field matches the package name and the translated source and action
labels. Text, source and action filters combine with logical AND. Typing is
debounced briefly so statistics and the chart are not recalculated for every
keystroke.

The source choices are Pacman, AUR and Flatpak. Action choices are upgraded,
installed, removed, reinstalled and downgraded. A source can legitimately have
no entry for some actions.

### Sort the table

Click a column header to sort by that column; click it again to reverse the
order. Date sorting uses a numeric timestamp rather than the formatted display
text. The default order is newest first.

### Inspect package details

Hover a table row to request current package metadata:

- Pacman and AUR rows use `pacman -Qi`;
- Flatpak rows use `flatpak info`.

The first hover can briefly show a loading message. Details are queried outside
the GUI thread and cached. If a historical package is no longer installed, the
tooltip keeps the log's version information and explains that live metadata is
unavailable.

The **Details** button disables both table and chart tooltips. Its state is
remembered across restarts.

### Read the activity chart

The chart counts visible records per calendar day and therefore follows all
active filters. It shows the 30 most recent days that contain activity, not the
30 most recent consecutive calendar days. Hover a bar for its date and exact
count.

### Reload and switch language

**Reload** starts a fresh background read of every available source. The button
is disabled until that read finishes, preventing overlapping reloads.

The language button switches between English and German immediately. Package,
source and action labels, filters, status text and future tooltips use the new
language. Package-manager descriptions remain in the stable `C` locale used to
parse command output.

## Understand the data

### Pacman

The parser streams `/var/log/pacman.log` and keeps entries whose `[ALPM]` action
is one of:

- `upgraded`;
- `installed`;
- `removed`;
- `reinstalled`;
- `downgraded`.

Both current ISO timestamps and older Pacman timestamp formats are supported.
The app displays all matching records retained in the log.

### AUR classification

`pacman -Qmq` returns packages currently considered foreign. Matching names in
the Pacman history are labeled AUR; other names are labeled Pacman.

This is a classification heuristic, not historical repository information.
Consequences include:

- removed AUR packages normally appear as Pacman;
- a package moved from AUR to an official repository can appear as Pacman in
  older entries;
- manually installed foreign packages can appear as AUR.

### Flatpak

The app combines:

1. `flatpak history --system`;
2. `flatpak history --user`;
3. `flatpak list --app`;
4. deployment timestamps below `/var/lib/flatpak` and
   `~/.local/share/flatpak` as a fallback.

Only currently installed application IDs are kept. Runtimes, locale extensions
and history for uninstalled apps are excluded. An installed app with no usable
history receives one fallback `installed` row based on deployment metadata.
Flatpak history does not provide old/new versions through the selected columns,
so those cells can contain an em dash.

## Configuration

Preferences are stored in:

```text
~/.config/cachy-update-history.json
```

Current keys are:

| Key | Type | Values | Default |
| --- | --- | --- | --- |
| `lang` | string | `en` or `de` | `en` |
| `tooltips` | boolean | `true` or `false` | `true` |

Unknown keys are preserved when a preference changes. Invalid, non-object or
oversized configuration data is ignored. Writes use a private temporary file
and atomic replacement.

`LOG_PATH`, source colors and action colors are source-code constants near the
top of `cachy-update-history.py`; they do not currently have user-facing config
keys.

To recover from unwanted preferences without deleting the original file, close
the app and rename it:

```text
mv ~/.config/cachy-update-history.json ~/.config/cachy-update-history.json.backup
```

## CSV export

**Export CSV** writes exactly the currently filtered and sorted rows. Headers
use the active UI language. The file is UTF-8 encoded and uses Python's standard
CSV quoting.

Values beginning with a spreadsheet formula prefix or control character receive
a leading apostrophe. This prevents package names or versions originating in
untrusted logs from being interpreted as formulas by spreadsheet software.

## Troubleshooting

### Pacman entries are missing

- Check that `/var/log/pacman.log` exists and is readable.
- Confirm that it contains `[ALPM]` package actions rather than only transaction
  messages.
- On non-Arch distributions, absence of this file is expected.

The status bar reports a missing or unreadable log while continuing to show
other sources.

### A package is classified under the wrong source

Run `pacman -Qmq` and check whether the package is currently listed. Historical
AUR origin cannot be reconstructed from `pacman.log`; see
[AUR classification](#aur-classification).

### Flatpak is missing or empty

Run these commands independently:

```text
flatpak list --app
flatpak history --system
flatpak history --user
```

A missing `flatpak` executable disables the source. Permission or installation
differences can make only one of the system/user history commands useful.

### A tooltip has no live details

The package may have been removed, or its package manager may be unavailable.
The historical row remains valid; only current metadata is missing.

### The desktop entry does not launch

Inspect its `Exec=` path and test that exact Python/script combination in a
terminal. Also validate the entry when `desktop-file-validate` is installed:

```text
desktop-file-validate ~/.local/share/applications/cachy-update-history.desktop
```

## Known limitations

- AUR origin is inferred from current foreign-package state.
- Flatpak history is limited to currently installed apps.
- All matching records are retained in memory; memory use grows with retained
  Pacman and Flatpak history.
- Package-manager command output is captured before parsing; unusually large
  Flatpak histories increase temporary memory use.
- The chart displays only the 30 latest active days.
- There is no package database, background index or cross-machine history.
