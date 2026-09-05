# dads-pc / PC-MINI reference artifacts

Reference material from the 2026-09-03 dads-pc / PC-MINI session. These files
are **annotations of the session's original scripts** — they document what was
run on that PC and why, and they are the source the shipped actions were
derived from.

> **These `.ps1` files are NOT executed by BareNOC.** The appliance runs the
> canonical shell wrappers in `src/scripts/` (`windows_diag.sh`,
> `windows_cleanup.sh`, `windows_netdiag.sh`) over SSH, each embedding its own
> PowerShell pass. If a reference `.ps1` disagrees with a `src/scripts/*.sh`,
> the shell wrapper is authoritative (it is the tested, shipped form).

| File | What it is |
|---|---|
| `dadpc-diagnostics.ps1` | The read-only diagnostic battery → `windows_diag.sh` |
| `fix1.ps1` | The cleanup fix + DNS-through-router override → `windows_cleanup.sh` + `windows_netdiag.sh` |
| `PC-MINI-session-findings.md` | The session's findings and decisions |
