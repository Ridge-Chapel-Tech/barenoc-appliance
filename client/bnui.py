"""BareNOC Chat — legacy AIM-style desktop client (Tkinter, stdlib only).

Conversations:
  * Queue Manager (Juniper) — system status + ticket activity; type a message to OPEN a
                     ticket, or use slash commands (/status, /logs, /system …).
  * Device buddies — click a device to see its details; sending a message there
                     opens a ticket targeted at that device.
  * Ticket buddies — click a ticket to see the conversation; messages you send
                     are appended to the ticket's work_notes for the agents.
"""

import datetime
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from bnapi import BareNOCClient, APIError

APP_NAME = "BareNOC Chat"
CLIENT_VERSION = "0.1.0"   # keep in sync with src/api/version.py (packaged with BareNOC)

# The Queue Manager's role label — shown next to the configured name (Juniper)
# in the chat header: "Juniper — Queue Manager".
QM_ROLE = "Queue Manager"

# per-OS config location (kept out of the repo / sync folders):
#   Linux:   ~/.config/barenoc/chat.json
#   macOS:   ~/Library/Application Support/BareNOC/chat.json
#   Windows: %APPDATA%\BareNOC\chat.json
if sys.platform == "win32":
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BareNOC")
    CONFIG_FILE = os.path.join(CONFIG_DIR, "chat.json")
elif sys.platform == "darwin":
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "BareNOC")
    CONFIG_FILE = os.path.join(CONFIG_DIR, "chat.json")
else:
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "barenoc")
    CONFIG_FILE = os.path.join(CONFIG_DIR, "chat.json")

# ── palette (AIM 5.x vibes) ─────────────────────────────────────
C = {
    "banner":    "#2155b8",
    "banner_dk": "#0d3a8f",
    "banner_fg": "#ffffff",
    "buddy_bg":  "#ffffff",
    "buddy_sel": "#cde1f7",
    "group":     "#0d3a8f",
    "qm":        "#123c7a",
    "online":    "#2f9e44",
    "offline":   "#868e96",
    "warning":   "#f08c00",
    "danger":    "#e03131",
    "sys":       "#1c6ea4",   # AIM system-message blue
    "me":        "#111111",
    "them":      "#0b7285",
    "dim":       "#868e96",
    "p1":        "#e03131",
    "p2":        "#f08c00",
    "p3":        "#2f9e44",
    "p4":        "#868e96",
    "done":      "#5c7cfa",
}

DEFAULT_SERVER = "https://<appliance-ip>"
POLL_INTERVAL = 8.0

FONT_SCALE = 1.0   # +2pt over the previous 0.8 scale, everywhere


def fs(n: int) -> int:
    return max(7, int(n * FONT_SCALE))


# ── helpers ──────────────────────────────────────────────────────

def fmt_ts(iso: str) -> str:
    """ISO timestamp (UTC, naive) → local 'YYYY-MM-DD HH:MM'."""
    if not iso:
        return "?"
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)[:16]


def fmt_clock() -> str:
    return time.strftime("%H:%M:%S")


def load_config() -> dict:
    cfg = {"server": DEFAULT_SERVER, "username": "", "password": "",
           "remember": False, "poll_interval": POLL_INTERVAL, "geometry": ""}
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


def parse_priority(text: str):
    """'!P2 rest of message' → (priority, rest). Defaults P3."""
    t = text.strip()
    up = t.upper()
    for p in ("P1", "P2", "P3", "P4"):
        if up.startswith("!" + p):
            return p, t[3:].strip()
    return "P3", t


class ChangePasswordDialog(tk.Toplevel):
    """Forced-change password dialog (backend blocks everything until changed)."""

    def __init__(self, parent, client, on_ok):
        super().__init__(parent)
        self.client = client
        self.on_ok = on_ok
        self.title(f"{APP_NAME} — Password Change Required")
        self.resizable(False, False)
        self.configure(bg="#f2f2f2")
        self._busy = False

        tk.Label(self, bg="#2155b8", fg="white", font=("Helvetica", fs(13), "bold"),
                 text="  Your administrator requires a password change").pack(fill="x", pady=(0, 8))

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Current password:").grid(row=0, column=0, sticky="e", pady=3)
        self.cur = ttk.Entry(frm, show="*", width=28)
        self.cur.grid(row=0, column=1, pady=3, padx=4)
        ttk.Label(frm, text="New password (min 8):").grid(row=1, column=0, sticky="e", pady=3)
        self.new = ttk.Entry(frm, show="*", width=28)
        self.new.grid(row=1, column=1, pady=3, padx=4)
        ttk.Label(frm, text="Confirm new:").grid(row=2, column=0, sticky="e", pady=3)
        self.conf = ttk.Entry(frm, show="*", width=28)
        self.conf.grid(row=2, column=1, pady=3, padx=4)
        self.status = ttk.Label(frm, text="", foreground="#c0392b")
        self.status.grid(row=3, column=0, columnspan=2, pady=4)
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, pady=6)
        ttk.Button(btns, text="Update Password", command=self._submit).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)

        self.bind("<Return>", lambda e: self._submit())
        self.new.focus_set()
        self.transient(parent)
        self.grab_set()

    def _submit(self):
        if self._busy:
            return
        cur, new, conf = self.cur.get(), self.new.get(), self.conf.get()
        if not cur or not new:
            self.status.config(text="Fill in both password fields.")
            return
        if new != conf:
            self.status.config(text="New passwords do not match.")
            return
        self._busy = True
        self.status.config(text="Updating…")
        try:
            self.client.change_password(cur, new)
        except APIError as e:
            self._busy = False
            self.status.config(text=str(e.detail))
            return
        self.destroy()
        self.on_ok()


class BareNOCChatApp:
    def __init__(self, root: tk.Tk, server: str = None):
        self.root = root
        self.cfg = load_config()
        if server:
            self.cfg["server"] = server

        self.client = None
        self.me = None
        self._evq = queue.Queue()
        self._running = False
        self._poller = None

        self.tickets = []          # cached ticket list
        self.qm_name = "Queue Manager"   # bot name from settings (Juniper)        self.devices = []          # cached device list
        self.system = None         # cached system status
        self.chat_user_list = []   # tech buddies
        self.chat_convs = []       # tech threads (unread counts)
        self.user_by_id = {}       # user id -> user brief
        self.conv = "qm"           # current conversation id
        self._displayed_conv = "qm"  # whose content is in the chat widget
        self._chat_buffers = {}     # per-conversation chat history
        self._last_ticket_id = None # context: last ticket mentioned/created
        self._last_ticket = None   # cached detail for the open ticket conv
        self.server_version = None  # appliance version (from /api/v1/client)

        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_wm_identity()
        self._build_style()
        self.show_login()
        # arm the event-drain loop for the whole app lifetime — the login
        # screen needs it too (sign-on results arrive via worker threads)
        self.root.after(150, self._drain_queue)

    # ── window manager identity (taskbar grouping + icon) ────────

    def _set_wm_identity(self):
        """Stable WM_CLASS (matches StartupWMClass in the .desktop entry) so
        the taskbar groups the window with the launcher, plus the window icon
        from the repo/installed PNG when present. Best-effort: no crash if
        unsupported (e.g. Wayland quirks / missing icon)."""
        try:
            self.root.tk.call('wm', 'wmclass', '.', 'BareNOC-Chat', 'BareNOCChat')
        except Exception:
            pass
        icon_path = self._find_icon()
        if icon_path:
            try:
                self.root.iconphoto(True, tk.PhotoImage(file=icon_path))
            except Exception:
                pass

    @staticmethod
    def _find_icon():
        """Locate the window icon: PyInstaller bundles it next to the app in
        the extraction dir (sys._MEIPASS); source runs find it beside the
        modules."""
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(base, "barenoc-chat.png")
        return candidate if os.path.exists(candidate) else None

    # ── styles ───────────────────────────────────────────────────

    def _build_style(self):
        # Force every Tk named font to the same body font — labels, entries,
        # menus and buttons otherwise fall back to theme defaults (TkDefaultFont
        # vs TkTextFont) which differ in size on this system.
        import tkinter.font as _tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                _tkfont.nametofont(name).configure(family="Helvetica", size=fs(10))
            except Exception:
                pass
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("Helvetica", fs(10)))  # all ttk text = body size
        # pin every ttk widget family to the SAME body font — ttk entries
        # otherwise resolve to the TkTextFont named font (bigger, different)
        for fam in ("TLabel", "TEntry", "TButton", "TCheckbutton", "TCombobox", "TFrame", "TLabelframe"):
            style.configure(fam, font=("Helvetica", fs(10)))
        style.configure("TEntry", padding=(6, 8))  # taller input fields
        style.configure("Buddies.Treeview", background=C["buddy_bg"], fieldbackground=C["buddy_bg"],
                        rowheight=26, font=("Helvetica", fs(10)))
        style.configure("Buddies.Treeview.Item", padding=2)
        style.map("Buddies.Treeview",
                  background=[("selected", C["buddy_sel"])],
                  foreground=[("selected", "#000000")])

    # ── login window ─────────────────────────────────────────────

    def show_login(self, message: str = ""):
        self._stop_poller()
        self._clear_root()

        root = self.root
        root.geometry(self._clamp_geometry(self.cfg.get("geometry") or "620x760"))
        root.minsize(400, 520)
        root.configure(bg="#f0f0f0")

        banner = tk.Frame(root, bg=C["banner"], height=56)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(banner, bg=C["banner"], fg=C["banner_fg"], text="BareNOC",
                 font=("Helvetica", fs(13), "bold")).pack(side="left", padx=12, pady=0)
        tk.Label(banner, bg=C["banner"], fg="#dbe7ff", text="Chat Client — Sign On",
                 font=("Helvetica", fs(8))).pack(side="right", padx=12, pady=0)

        # center the whole form block in the window (content-sized, not stretched)
        wrap = ttk.Frame(root)
        wrap.pack(fill="both", expand=True)
        frm = ttk.Frame(wrap, padding=14)
        frm.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(frm, text="Server:").grid(row=0, column=0, sticky="e", pady=9)
        self.v_server = ttk.Entry(frm, width=22)
        self.v_server.insert(0, self.cfg.get("server", DEFAULT_SERVER))
        self.v_server.grid(row=0, column=1, sticky="w", pady=9, padx=4)

        ttk.Label(frm, text="Screen Name:").grid(row=1, column=0, sticky="e", pady=9)
        self.v_user = ttk.Entry(frm, width=22)
        self.v_user.insert(0, self.cfg.get("username", ""))
        self.v_user.grid(row=1, column=1, sticky="w", pady=9, padx=4)

        ttk.Label(frm, text="Password:").grid(row=2, column=0, sticky="e", pady=9)
        self.v_pass = ttk.Entry(frm, width=22, show="*")
        if self.cfg.get("remember") and self.cfg.get("password"):
            self.v_pass.insert(0, self.cfg.get("password", ""))
        self.v_pass.grid(row=2, column=1, sticky="w", pady=9, padx=4)

        self.v_remember = tk.BooleanVar(value=bool(self.cfg.get("remember")))
        ttk.Checkbutton(frm, text="Remember password on this machine",
                        variable=self.v_remember).grid(row=3, column=1, sticky="w", pady=12)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, pady=(16, 0))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(2, weight=1)
        inner = ttk.Frame(btns)
        inner.grid(row=0, column=1)
        self.btn_signon = tk.Button(inner, text="Sign On", width=14, bg="#2155b8", fg="white",
                                    activebackground="#0d3a8f", activeforeground="white",
                                    font=("Helvetica", fs(13), "bold"), command=self._on_login)
        self.btn_signon.pack(side="left", padx=4)

        self.login_status = ttk.Label(root, text=message, foreground="#c0392b", background="#f0f0f0")
        self.login_status.pack(side="bottom", pady=(0, 8))

        self.v_pass.bind("<Return>", lambda e: self._on_login())
        self.v_user.bind("<Return>", lambda e: self.v_pass.focus_set())
        if self.cfg.get("username"):
            self.v_pass.focus_set()
        else:
            self.v_user.focus_set()

    def _set_login_busy(self, busy: bool):
        self.btn_signon.config(state="disabled" if busy else "normal",
                               text="Signing on…" if busy else "Sign On")

    def _on_login(self):
        server = self.v_server.get().strip().rstrip("/")
        username = self.v_user.get().strip()
        password = self.v_pass.get()
        if not server.startswith(("http://", "https://")):
            server = "https://" + server
        if not username or not password:
            self.login_status.config(text="Enter screen name and password.")
            return
        self._set_login_busy(True)
        self.login_status.config(text="Signing on…", foreground="#555555")
        client = BareNOCClient(server)
        threading.Thread(target=self._login_worker,
                         args=(client, username, password), daemon=True).start()

    def _login_worker(self, client, username, password):
        try:
            data = client.login(username, password)
            me = client.me()
        except APIError as e:
            self._evq.put({"type": "login_error", "detail": str(e.detail)})
            return
        except Exception as e:
            self._evq.put({"type": "login_error", "detail": repr(e)})
            return
        self._evq.put({"type": "login_ok", "client": client, "data": data, "me": me})

    def _handle_login_ok(self, ev):
        self.client = ev["client"]
        self.me = ev["me"]
        self.cfg["server"] = self.client.server
        self.cfg["username"] = self.v_user.get().strip()
        self.cfg["remember"] = self.v_remember.get()
        self.cfg["password"] = self.v_pass.get() if self.v_remember.get() else ""
        save_config(self.cfg)
        if ev["data"].get("password_change_required"):
            ChangePasswordDialog(self.root, self.client, on_ok=self._after_pw_change)
        else:
            self._after_pw_change()

    def _after_pw_change(self):
        self.me = self.client.me()
        self.open_main()
        self.refresh_now()
        threading.Thread(target=self._fetch_server_version, daemon=True).start()

    def _fetch_server_version(self):
        try:
            info = self.client._get("/api/v1/client")
            self._evq.put({"type": "client_info", "version": info.get("version")})
        except Exception:
            pass

    # ── main window ──────────────────────────────────────────────

    def open_main(self):
        self._clear_root()
        self._reset_session_state()
        root = self.root
        root.geometry(self._clamp_geometry(self.cfg.get("geometry") or "500x800"))
        root.minsize(400, 560)

        # banner
        banner = tk.Frame(root, bg=C["banner"], height=48)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(banner, bg=C["banner"], fg=C["banner_fg"], text="BareNOC",
                 font=("Helvetica", fs(13), "bold")).pack(side="left", padx=10)
        who = self.me.get("display_name") or self.me.get("username", "")
        role = self.me.get("role", "")
        tk.Label(banner, bg=C["banner"], fg="#dbe7ff",
                 text=f"Signed on: {who}  ({role})").pack(side="right", padx=10)

        # menu
        menubar = tk.Menu(root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Sign Off", command=self.show_login)
        m_file.add_separator()
        m_file.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=m_file)
        m_act = tk.Menu(menubar, tearoff=0)
        m_act.add_command(label="New Ticket…", command=self.new_ticket_dialog)
        m_act.add_command(label="Refresh Now", command=self.refresh_now)
        m_act.add_separator()
        m_act.add_command(label="Clear Chat", command=lambda: self._clear_chat())
        menubar.add_cascade(label="Actions", menu=m_act)
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="User Wiki…", command=self._open_wiki)
        m_help.add_command(label="Download / Update…", command=self._open_downloads)
        m_help.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=m_help)
        root.config(menu=menubar)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True, padx=4, pady=4)

        # buddy list + chat, split by a draggable sash (adjustable widths)
        self.paned = ttk.Panedwindow(body, orient="horizontal")
        self.paned.pack(fill="both", expand=True)
        self.left_pane = ttk.Frame(self.paned)
        self.right_pane = ttk.Frame(self.paned)
        self._buddies_visible = False  # buddies hidden by default — clean chat
        self.paned.add(self.right_pane, weight=1)
        # left_pane is added on demand by _toggle_buddies

        # left: buddy list
        left = self.left_pane
        ttk.Label(left, text="  Buddies", style="TLabel",
                  font=("Helvetica", fs(8), "bold")).pack(anchor="w", pady=(0, 2))
        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, style="Buddies.Treeview", show="tree", selectmode="browse")
        vs = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.tag_configure("group", foreground=C["group"], font=("Helvetica", fs(10), "bold"))
        self.tree.tag_configure("qm", foreground=C["qm"], font=("Helvetica", fs(10), "bold"))
        for tag, col in (("online", C["online"]), ("offline", C["offline"]),
                         ("warning", C["warning"]), ("danger", C["danger"]),
                         ("p1", C["p1"]), ("p2", C["p2"]), ("p3", C["p3"]),
                         ("p4", C["p4"]), ("done", C["done"]), ("me_tkt", "#0b7285")):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.tag_configure("unread", foreground="#e8590c",
                                font=("Helvetica", fs(10), "bold"))
        self.tree.tag_configure("them_t", foreground="#0b7285")
        self.tree.tag_configure("dim_t", foreground=C["dim"])
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.insert("", "end", iid="qm", text="☎  " + self.qm_name, tags=("qm",))

        # right: chat + input
        right = self.right_pane
        # chat header row: [← back]  title ................  [☰ buddies]
        headrow = ttk.Frame(right)
        headrow.pack(fill="x")
        self.btn_back = tk.Button(headrow, text="←", width=3, relief="flat",
                                  font=("Helvetica", fs(8), "bold"), command=self._go_qm)
        self.btn_back.pack(side="left")  # hidden/restored via _update_head
        self.chat_head = ttk.Label(headrow, text=self._qm_head(),
                                   font=("Helvetica", fs(13), "bold"))
        self.chat_head.pack(side="left", padx=4)
        self.btn_buddies = tk.Button(headrow, text="☰ Buddies", command=self._toggle_buddies,
                                     relief="flat", font=("Helvetica", fs(8), "bold"))
        self.btn_buddies.pack(side="right")
        self.chat_sub = ttk.Label(right, text="", foreground=C["dim"], font=("Helvetica", fs(8)))
        self.chat_sub.pack(anchor="w", pady=(0, 3))
        self._update_head()

        # input row — anchored to the bottom FIRST so the chat area can never
        # squeeze it out (pack order matters: side=bottom grabs space early)
        inp = ttk.Frame(right)
        inp.pack(side="bottom", fill="x", pady=(4, 0))
        self.entry = ttk.Entry(inp)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.bind("<Escape>", lambda e: self._go_qm())
        self.btn_send = tk.Button(inp, text="Send", width=8, bg="#2155b8", fg="white",
                                  activebackground="#0d3a8f", activeforeground="white",
                                  font=("Helvetica", fs(10), "bold"), command=self._send)
        self.btn_send.pack(side="left", padx=(4, 0))
        hint = ttk.Label(right, text="Ask anything · /new opens a ticket · /help",
                         foreground=C["dim"], font=("Helvetica", fs(8)))
        hint.pack(side="bottom", anchor="w", pady=(2, 0))

        # chat area fills whatever is left in the middle
        chat_wrap = ttk.Frame(right)
        chat_wrap.pack(fill="both", expand=True)
        self.chat = tk.Text(chat_wrap, wrap="word", bg="#fcfcf8", relief="flat",
                            font=("Helvetica", fs(12)), padx=8, pady=6, state="disabled",
                            cursor="arrow")
        cs = ttk.Scrollbar(chat_wrap, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=cs.set)
        self.chat.pack(side="left", fill="both", expand=True)
        cs.pack(side="right", fill="y")
        self.chat.tag_configure("h1", font=("Helvetica", fs(13), "bold"), foreground=C["qm"])
        self.chat.tag_configure("h2", font=("Helvetica", fs(12), "bold"), foreground="#333333")
        self.chat.tag_configure("sub", foreground=C["dim"], font=("Helvetica", fs(8)))
        self.chat.tag_configure("sys", foreground=C["sys"], font=("Helvetica", fs(12), "italic"))
        self.chat.tag_configure("me", foreground=C["me"], font=("Helvetica", fs(12)))
        self.chat.tag_configure("them", foreground=C["them"], font=("Helvetica", fs(12)))
        self.chat.tag_configure("err", foreground=C["danger"], font=("Helvetica", fs(12)))
        self.chat.tag_configure("ok", foreground=C["online"], font=("Helvetica", fs(12)))
        self.chat.tag_configure("dim", foreground=C["dim"], font=("Helvetica", fs(8)))
        self.chat.tag_configure("rule", foreground="#cccccc")
        self.chat.tag_configure("code", foreground="#0b7285", font=("Courier", fs(12)))

        # status bar
        self.status_bar = ttk.Label(root, text="", foreground=C["dim"], padding=(8, 2))
        self.status_bar.pack(side="bottom", fill="x")

        self.root.after(60, self._apply_sash)

        # conversations
        self.tree.selection_set("qm")
        self._render_qm_intro()

        # poller
        self._running = True
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    # ── poller ───────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                ev = self._fetch_snapshot()
            except APIError as e:
                ev = {"type": "error", "detail": str(e.detail),
                      "auth": e.status == 401}
            except Exception as e:
                ev = {"type": "error", "detail": repr(e)}
            self._evq.put(ev)
            time.sleep(float(self.cfg.get("poll_interval", POLL_INTERVAL)))

    def _fetch_snapshot(self) -> dict:
        tickets = self.client.tickets(limit=100)
        devices = self.client.devices(limit=500)
        system = None
        try:
            system = self.client.system_status()
        except APIError:
            pass  # system endpoint is best-effort
        sel = None
        if self.conv.startswith("tkt-"):
            tid = self.conv[4:]
            try:
                sel = self.client.ticket(tid)
            except APIError:
                sel = None
        # tech chat: buddy list + unread badges, plus the open thread if any
        chat_users = chat_convs = chat_thread = None
        try:
            chat_users = self.client.chat_users()
        except APIError:
            pass
        try:
            chat_convs = self.client.chat_conversations()
        except APIError:
            pass
        if self.conv.startswith("user-"):
            uname = self._current_user().get("username")
            if uname:
                try:
                    chat_thread = self.client.chat_messages(uname)
                except APIError:
                    chat_thread = None
        return {"type": "snapshot", "tickets": tickets, "devices": devices,
                "system": system, "selected_ticket": sel,
                "chat_users": chat_users, "chat_convs": chat_convs,
                "chat_thread": chat_thread}

    def _current_user(self) -> dict:
        if self.conv.startswith("user-"):
            uid = int(self.conv[5:])
            return self.user_by_id.get(uid, {})
        return {}

    def _drain_queue(self):
        """Process worker-thread events on the Tk main thread.

        Armed from both the login screen and the main window, so sign-on
        results are always picked up (fix: 'Signing on…' hung forever because
        the queue was only drained after login).
        """
        try:
            while True:
                ev = self._evq.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        try:
            self.root.after(150, self._drain_queue)
        except tk.TclError:
            pass  # root destroyed

    def _handle_event(self, ev):
        t = ev.get("type")
        if t == "login_error":
            self._set_login_busy(False)
            self.login_status.config(text=ev["detail"], foreground="#c0392b")
        elif t == "login_ok":
            self._handle_login_ok(ev)
        elif t == "snapshot":
            if not self._running:
                return
            self._update_buddy_list(ev)
            if ev.get("system") is not None:
                self.system = ev["system"]
            # re-render a ticket conversation only when its content changed
            # (the Queue Manager chat is append-only — never wipe it)
            if self.conv.startswith("tkt-"):
                sel = ev.get("selected_ticket")
                if sel is not None:
                    sig = json.dumps(sel, sort_keys=True)
                    if sig != getattr(self, "_tkt_sig", None):
                        self._tkt_sig = sig
                        self._last_ticket = sel
                        self._render_ticket(sel)
            # re-render an open tech IM thread when new messages arrive
            if self.conv.startswith("user-") and ev.get("chat_thread") is not None:
                thr = ev["chat_thread"].get("messages", [])
                sig = json.dumps(thr, sort_keys=True)
                if sig != getattr(self, "_chat_sig", None):
                    self._chat_sig = sig
                    self._render_chat_thread(self._current_user().get("username"), thr)
            self.status_bar.config(
                text=f"BareNOC v{self.server_version or '?'} · Connected · last update {fmt_clock()}")
        elif t == "chat_thread":
            self._set_send_busy(False)
            if self.conv.startswith("user-"):
                thr = ev["data"].get("messages", [])
                self._chat_sig = json.dumps(thr, sort_keys=True)
                self._render_chat_thread(ev["username"], thr)
        elif t == "ticket_sent":
            self._after_ticket_sent(ev.get("ticket"), ev.get("echo"))
        elif t == "open_ticket":
            self._open_ticket_conv(ev["ticket_id"])
        elif t == "cmd_out":
            self._set_send_busy(False)
            lines = ev.get("lines", [])
            if lines == ["__CLEAR__"]:
                self._clear_chat()
            else:
                if ev.get("echo"):
                    self._append(f"[{fmt_clock()}] You: {ev['echo']}", "me")
                    self._append("", None)   # blank line before the reply
                for i, ln in enumerate(lines):
                    if ln.startswith("✖"):
                        tag = "err"
                    elif ln and ln.isupper():
                        tag = "h2"
                    elif ln.startswith(("  /", "  Type", "  Click", "  Prefix", "  usage")):
                        tag = "code"
                    else:
                        tag = "them"
                    if i == 0:
                        self._append(f"{self.qm_name}: {ln}", tag)
                    else:
                        self._append(ln, tag)
                self._scroll_bottom()
        elif t == "send_error":
            self._after_send_error(ev.get("detail", ""))
        elif t == "error":
            if ev.get("auth"):
                self._handle_auth_error()
            else:
                self.status_bar.config(text=f"⚠ {ev['detail']}", foreground=C["danger"])
        elif t == "me":
            self.me = ev["user"]
        elif t == "client_info":
            self.server_version = ev.get("version")
            self.status_bar.config(
                text=f"BareNOC v{self.server_version or '?'} · Connected · last update {fmt_clock()}")

    def _handle_auth_error(self):
        self._stop_poller()
        messagebox.showwarning(APP_NAME, "Session expired — please sign on again.")
        self.show_login("Session expired.")

    # ── buddy list ───────────────────────────────────────────────

    def _update_buddy_list(self, ev) -> bool:
        """Rebuild tree if contents changed. Returns True if changed.

        Buddy list is intentionally minimal: Queue Manager + ONLINE techs only
        (no Devices/Tickets groups).
        """
        tickets = (ev["tickets"] or {}).get("tickets", [])
        devices = (ev["devices"] or {}).get("devices", [])
        # tech chat buddy data (best-effort — keep last on failure)
        if ev.get("chat_users") is not None:
            users = ev["chat_users"].get("users", [])
            names = ev["chat_users"].get("names") or {}
            if names.get("queue_manager"):
                self.qm_name = names["queue_manager"]
            self.chat_user_list = users
            self.user_by_id = {u["id"]: u for u in users}
        if ev.get("chat_convs") is not None:
            self.chat_convs = ev["chat_convs"].get("conversations", [])
        users = getattr(self, "chat_user_list", [])
        convs = getattr(self, "chat_convs", [])
        unread_map = {c.get("other", {}).get("id"): c.get("unread", 0) for c in convs}
        online = self._online_techs(users, unread_map)

        sig = json.dumps(
            [(u.get("id"), u.get("last_login"), unread_map.get(u.get("id"), 0))
             for u in online],
            sort_keys=True)
        if sig == getattr(self, "_buddy_sig", None):
            return False
        self._buddy_sig = sig
        self.tickets, self.devices = tickets, devices

        # preserve selection
        sel = self.tree.selection()
        sel_id = sel[0] if sel else None
        selected_ok = (sel_id == "qm")
        selected_ok = selected_ok or (sel_id and sel_id in [f"user-{u['id']}" for u in online])

        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", iid="qm", text="☎  " + self.qm_name, tags=("qm",))

        techs = self.tree.insert("", "end", iid="techs", text="Techs", tags=("group",))
        if not online:
            self.tree.insert(techs, "end", iid="no-techs", text="(no techs online)",
                             tags=("dim_t",))
        for u in online:
            un = u.get("username") or "?"
            label = un
            if unread_map.get(u.get("id")):
                label += f"  ({unread_map[u['id']]})"
                self.tree.insert(techs, "end", iid=f"user-{u['id']}",
                                 text=label, tags=("unread",))
            else:
                self.tree.insert(techs, "end", iid=f"user-{u['id']}",
                                 text=label, tags=("them_t",))
        self.tree.item("techs", open=True)

        if selected_ok and sel_id:
            try:
                self.tree.selection_set(sel_id)
                self.tree.focus(sel_id)
            except Exception:
                self.tree.selection_set("qm")
        else:
            self.tree.selection_set(self.conv if self.tree.exists(self.conv) else "qm")
        return True

    def _online_techs(self, users: list, unread_map: dict) -> list:
        """Techs seen in the last 15 min (or with unread messages from you)."""
        import datetime as _dt
        now = _dt.datetime.utcnow()
        out = []
        for u in users:
            online = False
            ul = u.get("last_login")
            if ul:
                try:
                    last = _dt.datetime.fromisoformat(str(ul).replace("Z", ""))
                    if (now - last).total_seconds() <= 15 * 60:
                        online = True
                except Exception:
                    pass
            if online or unread_map.get(u.get("id"), 0) > 0:
                out.append(u)
        return out

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        cid = sel[0]
        if cid in ("devs", "tix", "no-techs"):
            self.tree.selection_set(self.conv if self.tree.exists(self.conv) else "qm")
            return
        if not (cid == "qm" or cid.startswith(("dev-", "tkt-", "user-"))):
            self.tree.selection_set(self.conv if self.tree.exists(self.conv) else "qm")
            return
        self._set_conv(cid)
        if cid == "qm":
            self.chat_head.config(text=self._qm_head())
            self.chat_sub.config(text="ask me anything · /help")
            if not self._chat_buffers.get("qm"):
                self._render_qm()
            else:
                self._show_conversation()   # restore preserved history
        elif cid.startswith("dev-"):
            did = int(cid[4:])
            dev = next((d for d in self.devices if d.get("id") == did), None)
            self._render_device(dev)
        elif cid.startswith("tkt-"):
            tid = cid[4:]
            tkt = next((t for t in self.tickets if t.get("ticket_id") == tid), None)
            self.chat_head.config(text=tid)
            self.chat_sub.config(text="ticket conversation · messages add a comment")
            self._render_ticket(tkt or self._last_ticket, fetch=True)
        elif cid.startswith("user-"):
            u = self._current_user()
            name = u.get("username") or u.get("display_name") or "tech"
            self.chat_head.config(text=name)
            self.chat_sub.config(text="tech IM · messages go straight to them")
            self._load_chat_thread(name)
        self._update_head()

    def _set_conv(self, conv: str):
        """Switch the active conversation (and which one is on screen)."""
        self.conv = conv
        self._displayed_conv = conv

    def _load_chat_thread(self, username: str):
        def _fetch():
            try:
                self._evq.put({"type": "chat_thread", "username": username,
                               "data": self.client.chat_messages(username)})
            except APIError as e:
                self._evq.put({"type": "send_error", "detail": str(e.detail)})
        threading.Thread(target=_fetch, daemon=True).start()

    def _go_qm(self):
        """Return to the Queue Manager conversation (works with buddies hidden)."""
        if self.conv == "qm":
            return
        self._set_conv("qm")
        if self.tree.exists("qm"):
            self.tree.selection_set("qm")   # triggers _on_select → preserves history
        self._update_head()
        self.entry.focus_set()

    def _update_head(self):
        """Show the ← back button only when not in the Queue Manager chat."""
        if not hasattr(self, "btn_back"):
            return
        try:
            if self.conv == "qm":
                self.btn_back.pack_forget()
            else:
                self.btn_back.pack(side="left", before=self.chat_head)
        except Exception:
            pass

    def _open_ticket_conv(self, ticket_id: str):
        """Jump into a ticket conversation without the buddy list
        (used by '/open TKT-…' and 'open ticket TKT-…')."""
        try:
            t = self.client.ticket(ticket_id)
        except APIError as e:
            self._append(f"✖ {e.detail}", "err")
            return
        self._last_ticket_id = ticket_id
        self._set_conv(f"tkt-{ticket_id}")
        if self.tree.exists(self.conv):
            self.tree.selection_set(self.conv)   # triggers _on_select
        else:
            self._render_ticket(t)
        self._update_head()
        self.entry.focus_set()

    # ── chat rendering ───────────────────────────────────────────

    def _clear_chat(self):
        """Clear the chat widget AND the current conversation's buffer."""
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.config(state="disabled")
        self._chat_buffers[self.conv] = []

    def _append(self, text: str, tag: str = None, end="\n"):
        """Append to the current conversation's buffer; render if visible."""
        self._chat_buffers.setdefault(self.conv, []).append((text, tag, end))
        if self._displayed_conv == self.conv:
            self.chat.config(state="normal")
            self.chat.insert("end", text + end, tag or ())
            self.chat.config(state="disabled")

    def _show_conversation(self):
        """Re-render the current conversation from its buffer (view switch)."""
        self._displayed_conv = self.conv
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        for text, tag, end in self._chat_buffers.get(self.conv, []):
            self.chat.insert("end", text + end, tag or ())
        self.chat.config(state="disabled")
        self._scroll_bottom()

    def _rule(self):
        self._append("─" * 78, "rule")

    def _scroll_bottom(self):
        self.chat.see("end")

    def _render_qm_intro(self):
        self._render_qm()

    def _render_qm(self, force=False):
        """One-line conversational intro — no dashboard, no clutter."""
        self._clear_chat()
        self._append("Ask me anything — or /new <title> to open a ticket.", "sys")
        self._scroll_bottom()

    def _render_device(self, dev):
        self._clear_chat()
        if not dev:
            self._append("Device not found.", "err")
            return
        name = dev.get("name") or "?"
        self.chat_head.config(text=name)
        self.chat_sub.config(text="device buddy · send a message to open a ticket for it")
        self._append(name, "h1")
        st = (dev.get("status") or "?").upper()
        tag = "ok" if st == "ONLINE" else ("warning" if st in ("WARNING", "PENDING") else "err")
        self._append(f"Status: {st}", tag)
        self._append(f"Type: {dev.get('device_type', '?')} · Vendor: {dev.get('vendor', '?')} · "
                     f"Model: {dev.get('model', '?')}", "them")
        self._append(f"Hostname: {dev.get('hostname', '?')} · IP: {dev.get('ip_address', '?')}", "them")
        if dev.get("mac_address"):
            self._append(f"MAC: {dev['mac_address']}", "them")
        tags = dev.get("tags") or []
        if tags:
            self._append("Tags: " + ", ".join(str(x) for x in tags), "them")
        self._append(f"Last seen: {fmt_ts(dev.get('last_seen'))} · "
                     f"SNMP: {'yes' if dev.get('snmp_configured') else 'no'} · "
                     f"SSH: {'yes' if dev.get('ssh_configured') else 'no'}", "dim")
        self._rule()
        self._append("Send a message below to open a ticket targeting this device.", "sys")
        self._scroll_bottom()

    def _render_ticket(self, tkt, fetch=False):
        if fetch and tkt:
            tid = tkt.get("ticket_id")
            def _fetch_ticket():
                try:
                    self._evq.put({"type": "ticket_sent", "ticket": self.client.ticket(tid),
                                   "silent": True})
                except APIError as e:
                    self._evq.put({"type": "send_error", "detail": str(e.detail)})
            threading.Thread(target=_fetch_ticket, daemon=True).start()
            return
        self._clear_chat()
        if not tkt:
            self._append("Ticket not found.", "err")
            return
        self.chat_head.config(text=tkt.get("ticket_id"))
        self.chat_sub.config(text="ticket conversation · messages add a comment")
        self._append(tkt.get("ticket_id") or "?", "h1")
        status = (tkt.get("status") or "?").upper()
        prio = (tkt.get("priority") or "P3").upper()
        self._append(f"{prio} · {status}", "ok" if status in ("COMPLETED", "CLOSED")
                     else ("warning" if status in ("AWAITING_APPROVAL", "ESCALATED")
                           else ("err" if status == "FAILED" else "sys")))
        self._append(tkt.get("title") or "", "h2")
        if tkt.get("description"):
            self._append(tkt["description"], "them")
        meta = f"Opened {fmt_ts(tkt.get('created_at'))} · source {tkt.get('source', '?')}"
        if tkt.get("assigned_to"):
            meta += f" · assigned {tkt['assigned_to']}"
        if tkt.get("llm_model"):
            meta += f" · {tkt['llm_model']}"
        if tkt.get("llm_cost_usd"):
            meta += f" · ${float(tkt['llm_cost_usd']):.4f}"
        self._append(meta, "dim")
        self._rule()

        notes = tkt.get("work_notes")
        try:
            notes = json.loads(notes) if notes else []
        except Exception:
            notes = []
        if not notes:
            self._append("(no conversation yet — queue manager will respond here)", "dim")
        for n in notes:
            ts = fmt_ts(n.get("timestamp"))
            ev = n.get("event") or "note"
            detail = n.get("detail") or ""
            actor = n.get("actor") or ""
            me_name = (self.me or {}).get("username", "")
            if ev == "user_message":
                who = "You" if actor == me_name else (actor or "User")
                self._append(f"[{ts}] {who}:", "me")
                self._append(f"  {detail}", "me")
            elif ev in ("agent_completed", "agent_failed"):
                who = "Agent" + (f" ({actor})" if actor else "")
                tag = "ok" if ev == "agent_completed" else "err"
                self._append(f"[{ts}] {who}:", tag)
                self._append(f"  {detail}", tag)
            else:
                self._append(f"[{ts}] [{ev}] {detail}" + (f" — {actor}" if actor else ""), "sys")
        if tkt.get("resolution"):
            self._rule()
            self._append("RESOLUTION", "h2")
            self._append(tkt["resolution"], "them")
        self._scroll_bottom()

    # ── sending ──────────────────────────────────────────────────

    def _send(self):
        msg = self.entry.get().strip()
        if not msg or not self.client:
            return
        self.entry.delete(0, "end")
        self._set_send_busy(True)
        if self.conv == "qm":
            threading.Thread(target=self._send_qm, args=(msg,), daemon=True).start()
        elif self.conv.startswith("tkt-"):
            threading.Thread(target=self._send_comment, args=(self.conv[4:], msg), daemon=True).start()
        elif self.conv.startswith("user-"):
            uname = self._current_user().get("username")
            if uname:
                threading.Thread(target=self._send_chat, args=(uname, msg), daemon=True).start()
        elif self.conv.startswith("dev-"):
            threading.Thread(target=self._send_device, args=(int(self.conv[4:]), msg), daemon=True).start()
        else:
            self._set_send_busy(False)

    def _set_send_busy(self, busy: bool):
        self.btn_send.config(state="disabled" if busy else "normal")

    def _send_qm(self, msg):
        try:
            if msg.startswith("/new"):
                self._new_ticket(msg[4:].strip())
            elif msg.startswith("/"):
                out = self._run_command(msg)
                self._evq.put({"type": "cmd_out", "lines": out, "echo": msg})
            else:
                lines = self._answer(msg)
                if lines is None:
                    if self._smalltalk(msg):
                        self._evq.put({"type": "cmd_out",
                                       "lines": ["👋 What can I help with? Ask me about "
                                                 "tickets and the queue — or just tell me "
                                                 "the problem and I'll open a ticket for it."],
                                       "echo": msg})
                        return
                    # Queue Manager escalates: append to the active open ticket in
                    # this conversation, else open a new one.
                    outcome = self._escalate(msg)
                    if outcome.get("type") == "appended":
                        self._evq.put({"type": "cmd_out",
                                       "lines": [f"Noted on {outcome.get('ticket_id')} — a "
                                                 "technician will pick it up from here."],
                                       "echo": msg})
                    else:
                        self._evq.put({"type": "cmd_out",
                                       "lines": [f"Ticket created ({outcome.get('ticket_id')}) — "
                                                 "awaiting technician response. I'll post "
                                                 "updates here as it progresses."],
                                       "echo": msg})
                    return
                self._evq.put({"type": "cmd_out", "lines": lines, "echo": msg})
        except APIError as e:
            self._evq.put({"type": "send_error", "detail": str(e.detail)})
        except Exception as e:
            self._evq.put({"type": "send_error", "detail": repr(e)})

    def _new_ticket(self, text: str):
        """Open a ticket from explicit intent: /new !P2 title | description.
        Returns the created ticket dict."""
        prio, body = parse_priority(text)
        if "|" in body:
            title, desc = body.split("|", 1)
        else:
            title, desc = body, body
        title = (title.strip() or "(no title)")[:80]
        desc = desc.strip()
        tkt = self.client.create_ticket(title, description=desc, priority=prio)
        self._last_ticket_id = tkt.get("ticket_id")  # set immediately — don't wait for the event loop
        self._evq.put({"type": "ticket_sent", "ticket": tkt, "echo": f"/new {text}"})
        return tkt

    def _escalate(self, msg: str) -> dict:
        """Append to the active open ticket in this conversation, else reopen a
        RECENT closed/awaiting ticket and append there (thread continuity —
        'how about now?' continues the last conversation), else open a new one.
        Returns {"type": "appended"|"created", "ticket_id": ...}."""
        import datetime as _dt

        def _recent(t) -> bool:
            upd = t.get("updated_at") or t.get("created_at")
            if not upd:
                return False
            try:
                d = _dt.datetime.fromisoformat(str(upd).replace("Z", ""))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_dt.timezone.utc)
                return (_dt.datetime.now(_dt.timezone.utc) - d).total_seconds() <= 45 * 60
            except Exception:
                return False

        tid = getattr(self, "_last_ticket_id", None)
        if tid:
            try:
                t = self.client.ticket(tid)
            except APIError:
                t = None
            if t and (t.get("status") or "") in ("open", "in_progress",
                                                   "awaiting_approval", "escalated"):
                try:
                    self.client.add_note(tid, msg)
                    return {"type": "appended", "ticket_id": tid}
                except APIError:
                    pass  # fall through to a new ticket
            elif (t and (t.get("status") or "") in ("closed", "completed", "customer_action")
                  and _recent(t)):
                # Follow-up on a recently closed thread: reopen it so the agent
                # re-engages WITH the prior conversation instead of a blank slate.
                try:
                    self.client.update_ticket(tid, status="open")
                    self.client.add_note(tid, msg)
                    return {"type": "appended", "ticket_id": tid}
                except APIError:
                    pass
        tkt = self._new_ticket(msg)
        tid = (tkt or {}).get("ticket_id", "")
        # Defensive: if the server returned the same ticket we tried to append to
        # (shouldn't happen), report it as appended rather than a duplicate "created".
        if tid and tid == getattr(self, "_last_ticket_id", None) and tid != "":
            return {"type": "appended", "ticket_id": tid}
        return {"type": "created", "ticket_id": tid}

    def _smalltalk(self, msg: str) -> bool:
        """Friendly chit-chat — never worth a ticket."""
        m = msg.lower().strip().rstrip("?!.")
        exact = {"hello", "hi", "hey", "howdy", "yo", "sup", "good morning",
                 "good afternoon", "good evening", "thanks", "thank you", "thx",
                 "ty", "bye", "goodbye", "see ya", "ok", "okay", "cool"}
        return m in exact or any(m.startswith(g + " ") for g in
                                 ("hello", "hi", "hey", "thanks", "thank you"))

    def _qm_head(self) -> str:
        """Header title for the Queue Manager conversation: name + role."""
        return f"{self.qm_name} — {QM_ROLE}"

    # ── natural-language questions (Queue Manager) ───────────────

    def _answer(self, msg: str):
        """Try to answer a plain-language question. Returns lines to print,
        or None if it doesn't look like a question (→ open a ticket)."""
        import re as _re
        m = " ".join(msg.lower().replace("?", "").replace(".", "").replace(",", "").split())
        if not m:
            return None

        # ── identity: who am I talking to? (never a ticket) ────────────────
        if (_re.search(r"\b(what('?s| is)? your (name|title|role))\b", m)
                or _re.search(r"\bwhat are you called\b", m)
                or _re.search(r"\b(what|who) are you$", m)
                or _re.search(r"\bwho('?s| is)? (juniper|the queue manager|this chatbot)\b", m)):
            return [
                f"I'm {self.qm_name} — {QM_ROLE}. I run the ticket queue: open,"
                " prioritize, assign, close, and answer questions about tickets.",
                "Tell me a problem and I'll open a ticket — Lily (AI Technician)"
                " or a human tech picks it up from there.",
            ]

        # resolve the target ticket: explicit TKT-x id, a bare 4-digit id that
        # matches a recent ticket, or the conversation's active ticket when the
        # message points at it
        tkt_id = _re.search(r"(tkt-\d{8}-\d{4})", m)
        target = tkt_id.group(1).upper() if tkt_id else None
        if not target:
            suf = _re.search(r"\b(\d{4})\b", m)
            if suf:
                for _t in (getattr(self, "tickets", None) or []):
                    if (str(_t.get("ticket_id") or "").endswith("-" + suf.group(1))):
                        target = str(_t["ticket_id"])
                        break
        if target:
            self._last_ticket_id = target
        if not target and any(k in m for k in ("this ticket", "that ticket", "the ticket")):
            target = getattr(self, "_last_ticket_id", None)

        # ── Queue Manager actions: prioritize / assign / close ──
        # (word-boundary verbs so "closed"/"assigned" questions don't act)
        prio = _re.search(r"\b(p[1-4])\b", m)
        if target and prio and any(k in m for k in
                ("make", "set", "bump", "raise", "prioritize", "priority", "change")):
            try:
                self.client.update_ticket(target, priority=prio.group(1).upper())
                return [f"✅ {target} priority set to {prio.group(1).upper()}."]
            except APIError as e:
                return [f"✖ {e.detail}"]

        if target and _re.search(r"\b(assign|give|hand|route|transfer)\b", m):
            at = _re.search(r"\bto\s+([a-z0-9_.-]+)$", m)
            if at:
                try:
                    self.client.update_ticket(target, assigned_to=at.group(1))
                    return [f"✅ {target} assigned to {at.group(1)}."]
                except APIError as e:
                    return [f"✖ {e.detail}"]
            return [f"Assign {target} to whom? e.g. \"assign {target} to bob\""]

        if target and (_re.search(r"\bclose\b", m) or _re.search(r"\bmark\b", m)):
            status = "completed" if ("mark" in m or "complete" in m) else "closed"
            try:
                self.client.update_ticket(target, status=status)
                return [f"✅ {target} marked {status}."]
            except APIError as e:
                return [f"✖ {e.detail}"]

        # a bare ticket id → summary / open the conversation
        if target:
            if "open" in m or "show" in m:
                self._evq.put({"type": "open_ticket", "ticket_id": target})
                return []
            try:
                return self._ticket_summary(self.client.ticket(target))
            except APIError as e:
                return [f"✖ {e.detail}"]

        # conversation context: 'status on that ticket' → last ticket mentioned
        if (any(p in m for p in ("that ticket", "this ticket", "the ticket"))
                and getattr(self, "_last_ticket_id", None)
                and any(k in m for k in ("status", "update", "progress", "going",
                                         "how", "resolved", "done", "complete", "finished"))):
            try:
                return self._ticket_summary(self.client.ticket(self._last_ticket_id))
            except APIError as e:
                return [f"✖ {e.detail}"]

        if "ticket" in m and "status" in m:
            return ["Which ticket? Give me the id — e.g. \"status of TKT-…\" — "
                    "or tell me the problem and I'll open one."]

        # active-ticket context: "are there any notes yet?" / "any updates on it?"
        # → show the ticket we've been talking about, not the queue list
        if (getattr(self, "_last_ticket_id", None) and
                any(k in m for k in ("any notes", "notes yet", "any updates",
                                     "any progress", "did it say", "does it say",
                                     "what does the ticket", "what did the ticket",
                                     "on the ticket", "details", "update on"))):
            try:
                return self._ticket_summary(self.client.ticket(self._last_ticket_id))
            except APIError as e:
                return [f"✖ {e.detail}"]

        if m in ("help", "what can you do", "commands", "help me", "how do i use this"):
            return self._help_text()

        if "ticket" in m or "queue" in m or ("open" in m and "device" not in m):
            status_f, prio_f = None, None
            if "p1" in m:
                prio_f = "P1"
            elif "p2" in m:
                prio_f = "P2"
            elif "p3" in m:
                prio_f = "P3"
            elif "p4" in m:
                prio_f = "P4"
            if "closed" in m or "completed" in m or "done" in m or "finished" in m:
                status_f = "closed"
            elif "escalat" in m or "approval" in m or "needs human" in m or "require" in m:
                status_f = "escalated"
            elif "customer" in m or "my input" in m or "feedback" in m or "me to do" in m:
                status_f = "customer_action"
            elif "all" in m or ("mine" in m or "my tickets" in m):
                status_f = None
            else:
                status_f = "open"   # default: active work

            # fetch all recent and filter client-side. 'open' = open + in_progress
            # (active work); 'escalated' = waiting on human/customer; 'closed' = done.
            res = self.client.tickets(limit=200)
            all_tix = res.get("tickets", [])
            if status_f == "open":
                tix = [t for t in all_tix
                       if (t.get("status") or "").lower() in ("open", "in_progress")]
            elif status_f:
                tix = [t for t in all_tix if (t.get("status") or "").lower() == status_f]
            else:
                tix = all_tix
            if prio_f:
                tix = [t for t in tix if (t.get("priority") or "").upper() == prio_f]

            if "how many" in m or "count" in m:
                return [f"{len(tix)} ticket(s) match" +
                        (f" ({status_f})" if status_f else "") +
                        (f" {prio_f}" if prio_f else "") + "."]

            if "mine" in m or "my tickets" in m:
                meid = (self.me or {}).get("id")
                tix = [t for t in tix if t.get("submitter_id") == meid]
                return self._ticket_list_lines(tix, "my tickets")
            return self._ticket_list_lines(tix, status_f or "all")

        # ── Queue Manager scope ends here: anything else becomes a ticket ──
        if "refresh" in m:
            self.refresh_now()
            return ["Refreshing…"]

        return None

    def _usage_lines(self, days: int = 7) -> list:
        """Answer token/cost usage questions (admin-only endpoint)."""
        try:
            u = self.client.llm_usage(days=days)
        except APIError as e:
            if e.status == 403:
                return ["LLM usage stats are admin-only — your account can't view them."]
            return [f"✖ {e.detail}"]
        lines = [f"LLM USAGE (last {u.get('period_days', days)} days)",
                 f"  calls: {u.get('total_calls', 0)} · tokens: {u.get('total_tokens', 0)} · "
                 f"cost: ${u.get('total_cost_usd', 0):.4f}"]
        if u.get("daily"):
            today = u["daily"][0]
            lines.append(f"  {today.get('date')}: {today.get('calls', 0)} calls · "
                         f"{today.get('prompt_tokens', 0)} prompt + "
                         f"{today.get('response_tokens', 0)} resp tokens · "
                         f"${today.get('cost', 0):.4f}")
        if u.get("by_model"):
            lines.append("  by model:")
            for model, mb in u["by_model"].items():
                lines.append(f"    {model}: {mb.get('calls', 0)} calls · "
                             f"{mb.get('tokens', 0)} tokens · ${mb.get('cost', 0):.4f}")
        return lines

    def _ticket_list_lines(self, tix, label):
        if not tix:
            return [f"No tickets ({label})."]
        lines = [f"{len(tix)} ticket(s) ({label}):"]
        for t in tix[:40]:
            lines.append(f"  {t['ticket_id']} [{t.get('priority')} · {t.get('status')}] {t.get('title')}")
        return lines

    def _device_lines(self, dev):
        return [f"{dev.get('name')} — {(dev.get('status') or '?').upper()}",
                f"  {dev.get('device_type', '?')} · {dev.get('vendor', '?')} · {dev.get('model', '?')}",
                f"  {dev.get('hostname', '?')} · {dev.get('ip_address', '?')}",
                f"  Last seen {fmt_ts(dev.get('last_seen'))} · "
                f"SNMP {'yes' if dev.get('snmp_configured') else 'no'} · "
                f"SSH {'yes' if dev.get('ssh_configured') else 'no'}"]

    def _logs_lines(self, n):
        res = self.client.tickets(limit=50)
        tix = res.get("tickets", [])[:n]
        lines = [f"Recent activity (last {len(tix)}):"]
        for t in tix:
            last = ""
            try:
                nl = json.loads(t.get("work_notes")) if t.get("work_notes") else []
                if nl:
                    last = " — " + nl[-1].get("detail", "")[:60]
            except Exception:
                pass
            lines.append(f"  {t['ticket_id']} [{t.get('status')}] {t.get('title')}{last}")
        return lines

    def _send_comment(self, tid, msg):
        try:
            tkt = self.client.add_note(tid, msg)
            self._evq.put({"type": "ticket_sent", "ticket": tkt})
        except APIError as e:
            self._evq.put({"type": "send_error", "detail": str(e.detail)})
        except Exception as e:
            self._evq.put({"type": "send_error", "detail": repr(e)})

    def _send_device(self, did, msg):
        try:
            dev = next((d for d in self.devices if d.get("id") == did), None)
            devname = dev.get("name") if dev else f"device #{did}"
            prio, rest = parse_priority(msg)
            title = rest.splitlines()[0][:80] if rest else f"Status request: {devname}"
            desc = f"[Requested via chat on {devname}]\n\n{rest}"
            tkt = self.client.create_ticket(title, description=desc, priority=prio,
                                            target_device_id=did)
            self._evq.put({"type": "ticket_sent", "ticket": tkt, "echo": msg})
        except APIError as e:
            self._evq.put({"type": "send_error", "detail": str(e.detail)})
        except Exception as e:
            self._evq.put({"type": "send_error", "detail": repr(e)})

    def _send_chat(self, uname, msg):
        """Send a tech IM; then re-fetch the thread to show it."""
        try:
            self.client.chat_send(uname, msg)
            thr = self.client.chat_messages(uname)
            self._evq.put({"type": "chat_thread", "username": uname, "data": thr})
        except APIError as e:
            self._evq.put({"type": "send_error", "detail": str(e.detail)})
        except Exception as e:
            self._evq.put({"type": "send_error", "detail": repr(e)})

    def _render_chat_thread(self, username, msgs):
        """AIM-style instant-message view of a tech thread."""
        self._clear_chat()
        me_name = (self.me or {}).get("username", "")
        self._append(username or "Tech", "h1")
        self._append("Internal tech chat — messages go straight to them.", "sys")
        self._rule()
        if not msgs:
            self._append("(no messages yet — say hi)", "dim")
        for m in msgs:
            ts = fmt_ts(m.get("created_at"))
            sender = m.get("from_username", "?")
            body = m.get("body", "")
            if sender == me_name:
                self._append(f"[{ts}] You:", "me")
                self._append(f"  {body}", "me")
            else:
                self._append(f"[{ts}] {sender}:", "them")
                self._append(f"  {body}", "them")
        self._scroll_bottom()

    def _after_ticket_sent(self, tkt, echo=None):
        self._set_send_busy(False)
        if not tkt:
            return
        if echo:
            self._append(f"[{fmt_clock()}] You: {echo}", "me")
            self._append("", None)   # blank line before the response
        if self.conv.startswith("tkt-") and tkt.get("ticket_id") == self.conv[4:]:
            self._render_ticket(tkt)
        else:
            self._last_ticket_id = tkt.get("ticket_id")
            self._set_conv("qm")
            self.tree.selection_set("qm")
            tid = tkt.get("ticket_id", "?")
            self._append(f"✔ Ticket {tid} opened — Queue Manager will process it. "
                         f"Use /status {tid} to track it.", "ok")
            self._scroll_bottom()
            self.refresh_now()

    def _after_send_error(self, detail):
        self._set_send_busy(False)
        self._append(f"✖ {detail}", "err")
        self._scroll_bottom()

    # ── queue manager commands ───────────────────────────────────

    def _run_command(self, msg: str) -> list:
        """Handle slash commands. Returns list of lines to print."""
        parts = msg.split()
        cmd = parts[0].lower()
        arg = msg[len(parts[0]):].strip() if len(parts) > 1 else ""
        lines = []
        try:
            if cmd in ("/help", "/?"):
                lines = self._help_text()
            elif cmd == "/me":
                m = self.me or {}
                lines = [f"Screen name: {m.get('username', '?')}",
                         f"Display name: {m.get('display_name') or '-'}",
                         f"Role: {m.get('role', '?')}",
                         f"Email: {m.get('email') or '-'}",
                         f"Active: {'yes' if m.get('is_active') else 'no'}"]
            elif cmd == "/status":
                if not arg:
                    lines = ["usage: /status TKT-YYYYMMDD-NNNN"]
                else:
                    tid = arg.upper()
                    self._last_ticket_id = tid
                    t = self.client.ticket(tid)
                    lines = self._ticket_summary(t)
            elif cmd == "/open":
                if not arg:
                    lines = ["usage: /open TKT-YYYYMMDD-NNNN  (open the ticket conversation)"]
                else:
                    self._last_ticket_id = arg.upper()
                    self._evq.put({"type": "open_ticket", "ticket_id": arg.upper()})
                    lines = []
            elif cmd == "/usage":
                lines = self._usage_lines(1)
            elif cmd == "/version":
                lines = [f"BareNOC appliance: v{self.server_version or '?'}",
                         f"Chat client: v{CLIENT_VERSION}",
                         f"Downloads: {self.client.server}/downloads"]
            elif cmd == "/msg":
                bits = arg.split(None, 1)
                if len(bits) < 2:
                    lines = ["usage: /msg <username> <message>"]
                else:
                    uname, text = bits[0], bits[1]
                    try:
                        self.client.chat_send(uname, text)
                        lines = [f"→ {uname}: {text}"]
                    except APIError as e:
                        lines = [f"✖ {e.detail}"]
            elif cmd == "/tickets":
                status_filter, prio_filter = None, None
                flt = arg.lower() if arg else ""
                if flt in ("open", "in_progress", "completed", "failed", "escalated", "closed",
                           "awaiting_approval"):
                    status_filter = flt
                elif flt in ("p1", "p2", "p3", "p4"):
                    prio_filter = flt.upper()
                res = self.client.tickets(status=status_filter, priority=prio_filter, limit=60)
                tix = res.get("tickets", [])
                lines = [f"{len(tix)} ticket(s):"]
                for t in tix:
                    lines.append(f"  {t['ticket_id']} [{t.get('priority')} · {t.get('status')}] {t.get('title')}")
            elif cmd == "/devices":
                devs = self.client.devices(limit=500).get("devices", [])
                lines = [f"{len(devs)} device(s):"]
                for d in devs:
                    lines.append(f"  {d.get('name')} — {d.get('status')} ({d.get('ip_address')})")
            elif cmd == "/system":
                lines = self._system_lines(self.client.system_status())
            elif cmd == "/logs":
                n = 10
                if arg.isdigit():
                    n = min(int(arg), 50)
                res = self.client.tickets(limit=50)
                tix = res.get("tickets", [])
                lines = [f"Recent activity (last {min(n, len(tix))}):"]
                for t in tix[:n]:
                    notes = t.get("work_notes")
                    last = ""
                    try:
                        nl = json.loads(notes) if notes else []
                        if nl:
                            last = nl[-1].get("detail", "")[:60]
                    except Exception:
                        last = ""
                    lines.append(f"  {t['ticket_id']} [{t.get('status')}] {t.get('title')}"
                                 + (f" — {last}" if last else ""))
            elif cmd == "/refresh":
                self.refresh_now()
                lines = ["Refreshing…"]
            elif cmd == "/clear":
                lines = ["__CLEAR__"]
            else:
                lines = [f"Unknown command: {cmd}", "Type /help for the command list."]
        except APIError as e:
            lines = [f"✖ {e.detail}"]
        return lines

    def _help_text(self) -> list:
        return [
            "JUST ASK",
            "  \"what tickets are open?\"    \"any P1 tickets?\"",
            "  \"how many open tickets?\"    \"my tickets\"",
            "  \"status of TKT-…\"    \"open ticket TKT-…\"",
            "  \"device status\"        \"which devices are offline?\"",
            "  \"recent logs\"          \"how's the system?\"",
            "",
            "COMMANDS",
            "  /status TKT-…            full status + conversation",
            "  /open TKT-…              open the ticket conversation",
            "  /msg <user> <text>       send a tech IM",
            "  /tickets [status|P1-P4]  list tickets",
            "  /logs [n]                recent activity log",
            "  /devices /system /me     lists & status",
            "  /refresh /clear          refresh · clear this window",
            "",
            "OPENING TICKETS",
            "  /new <title> | <description>   opens a ticket (chat text never does)",
            "  Priority prefix:  /new !P2 internet is down",
            "  Actions → New Ticket…  also works",
            "  Click a device buddy + message → ticket for that device.",
            "  Click a ticket buddy + message → appends a comment.",
        ]

    def _ticket_summary(self, t) -> list:
        """Ticket status + outcome. Structured (JSON) resolutions are compacted
        to one line; plain-text answers (e.g. the AI Technician's VLAN list)
        render in full — the chat window scrolls."""
        lines = [f"{t['ticket_id']}  [{t.get('priority')} · {t.get('status')}]  {t.get('title')}"]
        res = (t.get("resolution") or "").strip()
        if res:
            if res.startswith("{") or res.startswith("["):
                lines.append("  " + self._compact_result(res))
            else:
                for rl in res.splitlines():
                    lines.append("  " + rl)
        else:
            try:
                notes = json.loads(t.get("work_notes")) if t.get("work_notes") else []
            except Exception:
                notes = []
            if notes:
                last = notes[-1].get("detail") or ""
                if len(last) > 600:
                    last = last[:600] + " …"
                for nl in last.splitlines():
                    lines.append("  " + nl)
            elif t.get("description"):
                lines.append("  " + str(t.get("description"))[:200])
        return lines

    def _compact_result(self, res: str) -> str:
        """Turn a resolution (JSON or python-dict-repr) into one readable line."""
        if res.startswith("{") and res.endswith("}"):
            rj = None
            try:
                rj = json.loads(res)
            except Exception:
                try:
                    import ast
                    rj = ast.literal_eval(res)
                except Exception:
                    rj = None
            if isinstance(rj, dict):
                parts = []
                if rj.get("network"):
                    parts.append(f"scanned {rj['network']}")
                if rj.get("count") is not None:
                    parts.append(f"{rj['count']} host(s) found")
                if rj.get("found"):
                    parts.append(", ".join(str(h.get("ip", h)) if isinstance(h, dict) else str(h)
                                            for h in rj["found"][:8]))
                if not parts and rj.get("error"):
                    parts.append(str(rj["error"]))
                if parts:
                    return " · ".join(parts)[:200]
        return res[:200]

    def _system_lines(self, sysd) -> list:
        app = sysd.get("app", {})
        host = sysd.get("host", {})
        lines = ["SYSTEM STATUS",
                 f"  Host: up {host.get('uptime', '?')} · CPU {host.get('cpu_percent', '?')}% · "
                 f"mem {host.get('memory', {}).get('used_gb', '?')}/{host.get('memory', {}).get('total_gb', '?')} GB · "
                 f"disk {host.get('disk', {}).get('used', '?')} ({host.get('disk', {}).get('percent', '?')}%)"]
        for c in sysd.get("containers", []):
            lines.append(f"  container: {c['name']} — {c['status']}")
        lines.append(f"  queue: {app.get('total_tickets', '?')} tickets, "
                     f"{app.get('open_tickets', '?')} open, "
                     f"{app.get('online_devices', '?')}/{app.get('total_devices', '?')} devices online, "
                     f"LLM cost ${app.get('llm_total_cost_usd', 0):.4f}")
        return lines

    # ── new ticket dialog ────────────────────────────────────────

    def new_ticket_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"{APP_NAME} — Open a Ticket")
        dlg.resizable(False, False)
        dlg.configure(bg="#f2f2f2")
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, bg="#2155b8", fg="white", font=("Helvetica", fs(13), "bold"),
                 text="  Open a New Ticket").pack(fill="x", pady=(0, 8))
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Title:").grid(row=0, column=0, sticky="ne", pady=3)
        e_title = ttk.Entry(frm, width=52)
        e_title.grid(row=0, column=1, pady=3, padx=4)
        ttk.Label(frm, text="Description:").grid(row=1, column=0, sticky="ne", pady=3)
        e_desc = tk.Text(frm, width=52, height=6, font=("Helvetica", fs(10)))
        e_desc.grid(row=1, column=1, pady=3, padx=4)
        ttk.Label(frm, text="Priority:").grid(row=2, column=0, sticky="ne", pady=3)
        e_prio = ttk.Combobox(frm, values=["P1", "P2", "P3", "P4"], width=8, state="readonly")
        e_prio.set("P3")
        e_prio.grid(row=2, column=1, sticky="w", pady=3, padx=4)
        ttk.Label(frm, text="Device:").grid(row=3, column=0, sticky="ne", pady=3)
        e_dev = ttk.Combobox(frm, state="readonly", width=48)
        dev_map = {}
        for d in self.devices:
            dev_map[f"{d.get('name')} ({d.get('ip_address')})"] = d.get("id")
        e_dev["values"] = list(dev_map.keys()) or ["(none)"]
        e_dev.grid(row=3, column=1, sticky="w", pady=3, padx=4)

        def _create():
            title = e_title.get().strip()
            if not title:
                return
            did = dev_map.get(e_dev.get()) if e_dev.get() else None
            try:
                tkt = self.client.create_ticket(title, description=e_desc.get("1.0", "end").strip(),
                                                priority=e_prio.get(), target_device_id=did)
            except APIError as ex:
                messagebox.showerror(APP_NAME, str(ex.detail), parent=dlg)
                return
            dlg.destroy()
            self._last_ticket_id = tkt.get("ticket_id")
            self._set_conv("qm")
            self.tree.selection_set("qm")
            self._append(f"✔ Ticket {tkt['ticket_id']} opened ({tkt.get('priority')} · {tkt.get('status')}).",
                         "ok")
            self.refresh_now()

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="Open Ticket", command=_create).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)
        e_title.focus_set()
        dlg.bind("<Return>", lambda e: _create())

    # ── misc ─────────────────────────────────────────────────────

    def refresh_now(self):
        if self.client:
            threading.Thread(target=self._poll_once, daemon=True).start()

    def _poll_once(self):
        try:
            self._evq.put(self._fetch_snapshot())
        except APIError as e:
            self._evq.put({"type": "error", "detail": str(e.detail), "auth": e.status == 401})

    def _show_help(self):
        top = tk.Toplevel(self.root)
        top.title(f"{APP_NAME} — Commands")
        top.geometry("560x420")
        txt = tk.Text(top, wrap="word", font=("Courier", fs(10)), padx=8, pady=6)
        txt.insert("1.0", "\n".join(self._help_text()))
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)

    def _show_about(self):
        """About dialog with normal-weight body text (no bold everywhere)."""
        top = tk.Toplevel(self.root)
        top.title(f"About {APP_NAME}")
        top.configure(bg="#ffffff")
        top.resizable(False, False)
        frm = ttk.Frame(top, padding=20)
        frm.pack()
        ttk.Label(frm, text="BareNOC Chat", font=("Helvetica", fs(13), "bold")).pack()
        ttk.Label(frm, text=f"Client v{CLIENT_VERSION} · appliance v{self.server_version or '?'}",
                  justify="center").pack(pady=(8, 2))
        ttk.Label(frm, text="Legacy AIM-style client for the BareNOC queue manager.",
                  justify="center").pack(pady=(0, 2))
        ttk.Label(frm, text="Stdlib-only Python/Tkinter · talks to the BareNOC REST API.",
                  justify="center").pack(pady=(0, 2))
        ttk.Label(frm, text="Config: ~/.config/barenoc/chat.json",
                  foreground=C["dim"]).pack(pady=(0, 12))
        ttk.Button(frm, text="Close", command=top.destroy).pack()
        top.transient(self.root)
        top.grab_set()

    def _open_wiki(self):
        """Open the user wiki in the browser, straight at the chat-client page."""
        import webbrowser
        server = self.client.server if self.client else DEFAULT_SERVER
        webbrowser.open(f"{server}/wiki/chat-client")

    def _open_downloads(self):
        """Open the portal's Downloads page (get the latest client)."""
        import webbrowser
        server = self.client.server if self.client else DEFAULT_SERVER
        webbrowser.open(f"{server}/downloads")

    def _reset_session_state(self):
        """Clear cached buddy-list state so a fresh session always rebuilds
        the tree. (Fix: after sign-off + sign-on, identical server data
        matched the stale signature and Tickets/Devices/Techs never appeared.)"""
        self._buddy_sig = None
        self._tkt_sig = None
        self._chat_sig = None
        self.tickets = []
        self.devices = []
        self.system = None
        self.chat_user_list = []
        self.chat_convs = []
        self.user_by_id = {}
        self._last_ticket = None

    def _clamp_geometry(self, geom: str) -> str:
        """Keep the window inside the screen so the input field is always
        reachable (the chat/input live at the bottom of the window)."""
        import re
        m = re.match(r"(\d+)x(\d+)(.*)$", geom)
        if not m:
            return geom
        w, h, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return f"{min(w, max(400, sw - 60))}x{min(h, max(480, sh - 140))}{rest}"

    def _apply_sash(self):
        """Set the buddy-list sash to the saved/default width (wider by default)."""
        try:
            self.paned.sashpos(0, int(self.cfg.get("buddies_sash", 320)))
        except Exception:
            pass

    def _toggle_buddies(self):
        """Collapse / expand the buddy list pane (fully hidden when collapsed).

        Panes are re-added in order instead of using 'before=' — some Tk
        versions reject the -before option on ttk.Panedwindow.add().
        """
        try:
            self.paned.forget(self.right_pane)
            try:
                self.paned.forget(self.left_pane)
            except Exception:
                pass
            if self._buddies_visible:
                self.paned.add(self.right_pane, weight=1)
                self._buddies_visible = False
                self.btn_buddies.config(text="☰ Buddies")
            else:
                self.paned.add(self.left_pane, weight=0)
                self.paned.add(self.right_pane, weight=1)
                self._buddies_visible = True
                self.btn_buddies.config(text="◧ Buddies")
                self.root.after(60, self._apply_sash)
        except Exception as e:
            self.status_bar.config(text=f"⚠ buddies toggle: {e}", foreground=C["danger"])

    def _stop_poller(self):
        self._running = False
        self._poller = None

    def _clear_root(self):
        for w in self.root.winfo_children():
            w.destroy()

    def _on_close(self):
        try:
            self.cfg["geometry"] = self.root.geometry()
            if getattr(self, "paned", None) is not None:
                try:
                    pos = self.paned.sashpos(0)
                    if pos:
                        self.cfg["buddies_sash"] = pos
                except Exception:
                    pass
            save_config(self.cfg)
        except Exception:
            pass
        self._stop_poller()
        self.root.destroy()


def main(server: str = None):
    root = tk.Tk()
    app = BareNOCChatApp(root, server=server)
    root.mainloop()


if __name__ == "__main__":
    main()
