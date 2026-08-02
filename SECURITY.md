# Security policy

CachyOS Update History is a local, unprivileged viewer. It reads package-manager
history and current package metadata; it does not install packages, run a
network service or require root.

## Supported versions

Security fixes are maintained for the latest stable minor release and the
current `master` branch:

| Version | Support |
| --- | --- |
| `master` | Supported as the next-release development branch. |
| Latest GitHub release | Supported. |
| Older releases | Upgrade first; fixes are not routinely backported. |

The current release is always identified by the
[GitHub Releases page](https://github.com/fenvarien/cachy-update-history/releases/latest),
so this policy remains valid after the next version is published.

Supported deployments also require a Python version that still receives
security updates and a maintained PyQt6/Qt package from the operating system or
Python package index.

## Report a vulnerability

Do not post exploit details, crafted payloads or sensitive local data in a
public issue.

1. If **Report a vulnerability** is available on the repository's **Security**
   tab, use GitHub private vulnerability reporting.
2. If that option is unavailable, contact the maintainer through the
   [fenvarien GitHub profile](https://github.com/fenvarien) and request a private
   channel before sharing technical details.
3. Public issues are appropriate for non-sensitive hardening suggestions that
   do not provide an exploitation path.

Maintainers should keep GitHub private vulnerability reporting enabled whenever
the repository settings permit it. This file intentionally does not publish an
unverified email address or promise a channel that may not exist.

Include, when safe:

- affected release or commit;
- operating system, Python, PyQt6 and Qt versions;
- required attacker capabilities and trust boundary;
- minimal reproduction steps or sanitized input;
- impact on confidentiality, integrity or availability;
- suggested mitigation, if known.

Remove usernames, home paths, package inventories and unrelated log content.

## Response and disclosure

This is a small volunteer-maintained project, so no fixed response SLA is
promised. The intended process is:

1. acknowledge and reproduce the report;
2. assess affected versions and severity;
3. prepare a focused fix and regression test privately when necessary;
4. coordinate disclosure and release timing with the reporter;
5. document user-relevant impact and upgrade guidance in the changelog/release.

Please allow reasonable remediation time before public disclosure. Credit is
given when requested and appropriate.

## Threat model

The application assumes the local user and Python/PyQt6 runtime are trusted. It
does not assume these inputs are well formed:

- `/var/log/pacman.log` content;
- `pacman` and `flatpak` output;
- Flatpak application IDs and deployment metadata;
- the app's JSON preference file;
- package names and versions exported to CSV;
- package metadata rendered in rich-text tooltips.

Security reports are especially relevant when crafted input can:

- execute a command or change command options;
- write outside the intended config/export target;
- inject active rich text or spreadsheet formulas;
- traverse filesystem paths or expand glob patterns;
- freeze the GUI or exhaust memory/processes without a practical bound;
- expose local data to another user or a remote party;
- cause the app to require or misuse elevated privileges.

## Maintained controls

- External commands use explicit argument lists, resolved executable paths, no
  shell and timeouts.
- Package arguments are separated from command options.
- Flatpak IDs are validated before path/glob use.
- Dynamic tooltip content is HTML-escaped.
- CSV formula/control prefixes are neutralized.
- Configuration reads are size/type bounded; writes are private and atomic.
- Package I/O is isolated from the GUI thread.
- Worker concurrency, pending tooltip requests and tooltip cache size are
  bounded.
- The Pacman log is streamed rather than loaded as one raw string.
- Regression tests cover principal input-validation and persistence controls.

Implementation details and invariants are maintained in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Residual and out-of-scope risks

- The complete normalized history remains in memory, so retained history size
  still controls steady-state memory use.
- Captured subprocess output is time-bounded but not currently byte-bounded.
- A malicious executable selected through an already-compromised user `PATH`, a
  compromised Python/Qt runtime or arbitrary code running as the same user is
  outside the app's security boundary.
- Package-manager correctness and the authenticity of system package metadata
  are delegated to Pacman, Flatpak and the operating system.
- CSV export intentionally writes to a path selected and confirmed by the user.

These limitations should not be described as vulnerabilities unless an attack
crosses an additional trust boundary or produces impact beyond the stated model.
