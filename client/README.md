# BareNOC Chat

A legacy **AIM-style desktop chat client** for the BareNOC queue manager.
Run it on your PC, sign on with your BareNOC account, and talk to the ticket
queue like it's 1999.

![vibe] AIM-style blue sign-on screen, buddy list of devices + tickets, chat
window with system/agent/user messages, slash commands.

## What it does

| You want…                                   | How                                            |
|---------------------------------------------|------------------------------------------------|
| **Open a ticket**                           | Type a message in the Queue Manager chat (a `!P2` prefix sets priority), or use *Actions → New Ticket…* |
| **Request a status update**                 | `/status TKT-20250801-1234`, or click the ticket buddy and read the conversation |
| **Request logs**                            | `/logs [n]` for recent activity, `/system` for host/queue status, `/devices` for the device list |
| **Talk to an existing ticket**              | Click the ticket in the buddy list, type, hit Send — it lands in the ticket's work notes |
| **Report a problem with a device**          | Click the device buddy, type the problem — opens a ticket targeted at that device |
| **Account login**                           | AIM-style Sign On screen (username/password); forced password changes are handled |

## Requirements

- Python 3.8+ with **tkinter** (Ubuntu/Debian: `sudo apt install python3-tk`)
- Network reachability to the BareNOC VM (`https://192.0.2.207`)
- A BareNOC user account (admin/operator/readonly all work)

## Install & run

**Requirements:** Python 3.8+ with tkinter, network reachability to the
BareNOC VM (`https://192.0.2.207`), and a BareNOC user account.

### Linux (Debian/Ubuntu/Fedora/Arch/…)

```bash
cd client && ./install.sh       # installs launcher + desktop entry + icon
barenoc-chat                    # afterwards
```

If tkinter is missing, `install.sh` prints the right package for your distro
(`python3-tk` / `python3-tkinter` / `tk` / `python3-tk`). No root needed —
everything goes under `~/.local`.

### Windows (10/11)

```bash
cd client
install.bat                     # double-click also works
```

Installs to `%LOCALAPPDATA%\BareNOC` (standalone copy of the app), creates
**Start Menu** + **Desktop** shortcuts with the app icon, and uses `pythonw`
so the GUI opens without a console window. Needs Python from
[python.org](https://www.python.org/downloads/) (tick *Add python.exe to PATH*;
tkinter is included). Uninstall = delete the `BareNOC` folder + the 2 shortcuts.

### macOS (10.13+)

```bash
cd client
./install.command               # double-click also works
open ~/Applications/"BareNOC Chat.app"
```

Builds a real `.app` bundle in `~/Applications` (ad-hoc signed) with the app
icon, launchable from Launchpad/Finder. Needs Python from
[python.org](https://www.python.org/downloads/macos/) (tkinter included; the
bare Command-Line-Tools python doesn't ship tkinter). Config lives in
`~/Library/Application Support/BareNOC/chat.json`.

### Run directly from source (any OS)

```bash
cd client
python3 barenoc_chat.py --server https://192.0.2.207
```

The server URL and username are remembered in the per-OS config file
(password only if you tick "Remember password").

## Build standalone binaries (PyInstaller)

For machines without Python, build a single-file executable (Linux/Windows)
or an `.app` bundle (macOS). PyInstaller **cannot cross-compile** — run the
build on each target OS:

```bash
# Linux or macOS:
cd client && ./build.sh
# Windows (cmd):
cd client && build.bat
```

Output:

| OS      | Output                  | Notes                                   |
|---------|-------------------------|------------------------------------------|
| Linux   | `dist/barenoc-chat`     | single-file executable, ~17 MB          |
| Windows | `dist/BareNOC Chat.exe` | single-file, windowed (no console)      |
| macOS   | `dist/BareNOC Chat.app` | onedir .app — drag to /Applications     |

The spec (`barenoc_chat.spec`) bundles the window icon and keeps the same
per-OS config-file behavior; nothing else is needed on the target machine.

## Slash commands (Queue Manager chat)

```
/help                  this list
/tickets [filter]      open | in_progress | completed | failed | P1 | P2 …
/status TKT-…          full status + conversation for a ticket
/logs [n]              recent activity log
/devices               device list
/system                host + queue status
/me                    your account info
/refresh               refresh now
/clear                 clear the window
```

Plain messages open a ticket (P3 by default; prefix `!P1`…`!P4` for priority).
The buddy list auto-refreshes every few seconds.

## Notes & security

- The BareNOC appliance uses a self-signed TLS cert; the client deliberately
  skips cert verification (LAN trust model). Plain `http://` also works.
- "Remember password" stores the password in `~/.config/barenoc/chat.json`
  with 0600 permissions — only use it on a machine you trust.
- Commenting on a ticket (`POST /api/v1/tickets/{id}/notes`) requires the
  matching backend endpoint — deployed with the ticket-chat update. The rest
  of the client works against the stock BareNOC API.

## Files

- `barenoc_chat.py` — entry point
- `bnapi.py` — REST client (stdlib `urllib`, no dependencies)
- `bnui.py` — Tkinter UI
- `install.sh` — optional `~/.local/bin` launcher + `.desktop` entry
