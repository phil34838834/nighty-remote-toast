"""
NIGHTY REMOTE TOAST  -  local client
====================================

Runs on YOUR computer, not on the VPS.

Connects to the SSE endpoint published by `nighty_toast_bridge.py` (the Nighty
script running on your VPS) and draws the notification here, on your own screen,
in Nighty's visual style. Clicking a toast opens the message in Discord.

Dependencies: NONE. Just Python 3.8+ with tkinter, which ships with the official
Windows installer. No pip install required.

Usage
-----
1. Run it once (double click). It creates `toast_client.json` next to itself and
   exits.
2. Fill in host / port / token (get the token from the "Toast Bridge" tab in
   Nighty, or with `<p>toastbridge token`).
3. Run it again. The `.pyw` extension means no console window appears.

To start it automatically with Windows, use `nighty_toast_client_autostart.pyw`
instead - it has a small panel to turn autostart on and off.

To stop it: right click any toast -> "Quit client". If no toast is on screen,
end the `pythonw.exe` process in Task Manager (Ctrl+Shift+Esc).
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import tkinter as tk
import tkinter.font as tkfont

try:
    import winsound
except ImportError:                      # non-Windows
    winsound = None


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "toast_client.json")
LOG_PATH = os.path.join(HERE, "toast_client.log")

LOG_LIMIT = 512 * 1024                   # rotate past 512 KB
LOCK_PORT = 50787                        # local port used only as a single-instance lock

# --boot is set by the autostart shortcut. It silences dialogs that make no
# sense while Windows is still logging in.
QUIET = "--boot" in sys.argv

_lock = None                             # socket held for the life of the process


DEFAULTS = {
    "scheme": "http",
    "host": "YOUR_VPS_IP",
    "port": 8787,
    "token": "PASTE_YOUR_TOKEN_HERE",

    "position": "top-right",     # top-left | top-right | bottom-left | bottom-right
    "margin_x": 24,
    "margin_y": 24,
    "width": 380,
    "duration": 8,               # seconds on screen (0 = stay until clicked)
    "max_visible": 4,
    "avatar": True,
    "avatar_circular": True,
    "sound": False,
    "open_in_app": True,         # open the Discord app instead of the browser
    "progress_bar": True,        # thin bar showing the remaining time
    "animate": True,             # slide + fade in

    # Obey `/settings toastsettings` from the VPS (side, duration_ms, image_url).
    # Set to false to let this file win.
    "follow_vps": True,
}

# Palette mirroring Nighty's own look.
COLORS = {
    "bg": "#16171A",
    "bg_hover": "#1C1D21",
    "border": "#2A2C31",
    "title": "#F2F3F5",
    "text": "#B5BAC1",
    "footer": "#72767D",
    "close": "#6B7075",
    "close_hover": "#F2F3F5",
    "track": "#232529",
}
ACCENTS = {
    "INFO": "#40A0C6",           # Nighty blue
    "SUCCESS": "#43B581",
    "ERROR": "#ED4245",
    "WARNING": "#FAA61A",
}
GLYPHS = {"INFO": "i", "SUCCESS": "✓", "ERROR": "!", "WARNING": "!"}


def log(msg):
    try:
        # Under autostart this runs for weeks, so the log has to be capped.
        # Keeps at most the current file plus one .old.
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_LIMIT:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8", errors="ignore") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def alert(title, message, kind="info"):
    """Simple dialog. A .pyw has no console, so errors need somewhere to show."""
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        (messagebox.showwarning if kind == "warn" else
         messagebox.showerror if kind == "error" else
         messagebox.showinfo)(title, message)
    finally:
        root.destroy()


def single_instance():
    """True when this is the only client running.

    Binding a local port is the most reliable single-instance lock on Windows
    without dependencies: without SO_REUSEADDR the second bind fails. The socket
    stays open until the process dies, and the OS releases it even on a crash.
    """
    global _lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        _lock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
        return None
    cfg = dict(DEFAULTS)
    try:
        # utf-8-sig: Windows Notepad saves JSON with a BOM and the plain parser
        # chokes on it. Reading with -sig accepts the file either way.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig", errors="ignore") as f:
            cfg.update(json.load(f) or {})
    except Exception as e:
        log(f"invalid config: {e}")
        alert("Nighty Remote Toast",
              f"Could not read {CONFIG_PATH}:\n\n{e}\n\n"
              "Fix the JSON (trailing comma, missing quote) and run again.",
              "error")
        return False
    return cfg


# ══════════════════════════════════════════════════════════════════════════
# SSE reader
#
# Runs on its own thread and pushes events into a Queue. tkinter may only be
# touched from the main thread, so the UI drains that queue via root.after.
# ══════════════════════════════════════════════════════════════════════════

class SSEReader(threading.Thread):
    def __init__(self, cfg, out):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.out = out
        self.last_seq = 0
        self.stop = threading.Event()

    def url(self):
        base = f"{self.cfg['scheme']}://{self.cfg['host']}:{self.cfg['port']}/events"
        params = {"token": self.cfg["token"]}
        if self.last_seq:
            params["since"] = self.last_seq
        return base + "?" + urllib.parse.urlencode(params)

    def run(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                self.connect()
                backoff = 1          # connected then dropped: retry quickly
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.out.put({"_status": "auth", "detail": "token rejected (401)"})
                    log("401: wrong token")
                    backoff = 30
                else:
                    log(f"HTTP {e.code}")
                    backoff = min(backoff * 2, 30)
            except Exception as e:
                log(f"connection dropped: {type(e).__name__}: {e}")
                backoff = min(backoff * 2, 30)

            self.out.put({"_status": "offline"})
            if self.stop.wait(backoff):
                return

    def connect(self):
        req = urllib.request.Request(self.url(), headers={
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Authorization": f"Bearer {self.cfg['token']}",
        })
        # Longer than the server's 20s keepalive, otherwise an idle connection
        # gets torn down for no reason.
        response = urllib.request.urlopen(req, timeout=60)
        self.out.put({"_status": "online"})
        log("connected")

        payload = []
        for raw in response:
            if self.stop.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

            if line == "":                        # end of event
                if payload:
                    self.dispatch("\n".join(payload))
                    payload = []
                continue
            if line.startswith(":"):              # comment / keepalive
                continue
            if line.startswith("data:"):
                payload.append(line[5:].lstrip())
            elif line.startswith("id:"):
                try:
                    self.last_seq = max(self.last_seq, int(line[3:].strip()))
                except ValueError:
                    pass

    def dispatch(self, text):
        try:
            event = json.loads(text)
        except Exception:
            return
        seq = event.get("seq")
        if isinstance(seq, int):
            self.last_seq = max(self.last_seq, seq)
        self.out.put(event)


# ══════════════════════════════════════════════════════════════════════════
# Images
# ══════════════════════════════════════════════════════════════════════════

class AvatarCache:
    """Downloads and converts avatars. Tk only reads PNG/GIF, so we ask the
    Discord CDN for PNG explicitly."""

    def __init__(self, enabled=True, circular=True, size=44):
        self.enabled = enabled
        self.circular = circular
        self.size = size
        self.cache = {}

    @staticmethod
    def normalize(url):
        if not url:
            return None
        base = url.split("?")[0]
        for ext in (".webp", ".jpg", ".jpeg", ".gif"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)] + ".png"
                break
        if not base.lower().endswith(".png"):
            return None
        return base + "?size=64"

    def get(self, url):
        if not self.enabled:
            return None
        key = self.normalize(url)
        if not key:
            return None
        if key in self.cache:
            return self.cache[key]

        image = None
        try:
            req = urllib.request.Request(key, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=6).read()
            image = tk.PhotoImage(data=raw)

            factor = max(1, image.width() // self.size)
            if factor > 1:
                image = image.subsample(factor, factor)

            if self.circular:
                self.mask_circle(image)
        except Exception as e:
            log(f"avatar failed ({key}): {type(e).__name__}: {e}")
            image = None

        self.cache[key] = image
        return image

    def get_icon(self, url):
        """Generic icon (the image_url from /settings toastsettings). No circular
        mask: Nighty's logo is a rounded square, not a circle."""
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        image = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=6).read()
            image = tk.PhotoImage(data=raw)          # Tk reads PNG and GIF
            factor = max(1, image.width() // self.size)
            if factor > 1:
                image = image.subsample(factor, factor)
        except Exception as e:
            log(f"icon failed ({url}): {type(e).__name__}: {e}")
            image = None
        self.cache[url] = image
        return image

    @staticmethod
    def mask_circle(image):
        """Circular mask via transparency_set (Tk 8.6+), no PIL needed."""
        try:
            width, height = image.width(), image.height()
            cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
            radius = min(cx, cy)
            r2 = radius * radius
            for y in range(height):
                dy = y - cy
                dy2 = dy * dy
                for x in range(width):
                    dx = x - cx
                    if dx * dx + dy2 > r2:
                        image.transparency_set(x, y, True)
        except Exception:
            pass          # no mask beats no avatar


def round_corners(window):
    """Windows 11 rounded corners through DWM. No-op elsewhere."""
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kw):
    """Rounded rectangle on a Canvas - tkinter has no primitive for it."""
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


# ══════════════════════════════════════════════════════════════════════════
# Toast
# ══════════════════════════════════════════════════════════════════════════

class Toast:
    PADDING_X = 14
    PADDING_Y = 12
    ICON = 44

    def __init__(self, manager, event):
        self.manager = manager
        self.event = event
        self.cfg = manager.cfg
        self.closing = False
        self.paused = False
        self.timer = None
        self.alpha = 0.0
        self.deadline = None
        self.total = 0.0
        self._image = None        # keeps the reference alive or Tk drops it

        kind = str(event.get("type", "INFO")).upper()
        accent = ACCENTS.get(kind, ACCENTS["INFO"])
        width = int(self.cfg.get("width", 380))

        self.win = tk.Toplevel(manager.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        self.win.configure(bg=COLORS["border"])

        outer = tk.Frame(self.win, bg=COLORS["border"])
        outer.pack(fill="both", expand=True)

        # The progress track must be packed BEFORE the body: the body expands
        # to fill the cavity, so anything packed after it would get no space.
        self.progress = None
        show_progress = (self.cfg.get("progress_bar", True)
                         and float(self.cfg.get("duration", 8) or 0) > 0)
        if show_progress:
            track = tk.Frame(outer, bg=COLORS["track"], height=2)
            track.pack(side="bottom", fill="x")
            track.pack_propagate(False)
            self.progress = tk.Frame(track, bg=accent, height=2)
            self.progress.place(x=0, y=0, relwidth=1.0, height=2)

        self.body = tk.Frame(outer, bg=COLORS["bg"])
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        # Accent stripe down the left edge.
        tk.Frame(self.body, bg=accent, width=3).pack(side="left", fill="y")

        inner = tk.Frame(self.body, bg=COLORS["bg"])
        inner.pack(side="left", fill="both", expand=True,
                   padx=self.PADDING_X, pady=self.PADDING_Y)

        head = tk.Frame(inner, bg=COLORS["bg"])
        head.pack(fill="x")

        author = event.get("author") or {}
        image = manager.avatars.get(author.get("avatar"))
        if image is None:
            image = manager.default_icon        # /settings toastsettings image_url

        if image is not None:
            self._image = image
            tk.Label(head, image=image, bg=COLORS["bg"], bd=0).pack(
                side="left", padx=(0, 12))
            icon_width = self.ICON + 12
        else:
            self._badge(head, author.get("name"), accent, kind)
            icon_width = self.ICON + 12

        texts = tk.Frame(head, bg=COLORS["bg"])
        texts.pack(side="left", fill="x", expand=True)

        wrap = max(150, width - icon_width - (self.PADDING_X * 2) - 34)

        tk.Label(
            texts, text=self._clip(event.get("title") or "Nighty", 44),
            bg=COLORS["bg"], fg=COLORS["title"], font=manager.f_title,
            anchor="w", justify="left",
        ).pack(fill="x")

        text = (event.get("text") or "").strip()
        if text:
            tk.Label(
                texts, text=self._clip(text, 220),
                bg=COLORS["bg"], fg=COLORS["text"], font=manager.f_text,
                anchor="w", justify="left", wraplength=wrap,
            ).pack(fill="x", pady=(3, 0))

        footer = self._footer(event)
        if footer:
            tk.Label(
                inner, text=footer, bg=COLORS["bg"], fg=COLORS["footer"],
                font=manager.f_footer, anchor="w", justify="left",
            ).pack(fill="x", pady=(7, 0))

        close = tk.Label(head, text="✕", bg=COLORS["bg"],
                         fg=COLORS["close"], font=manager.f_close, cursor="hand2")
        close.pack(side="right", anchor="n")
        close.bind("<Enter>", lambda e: close.configure(fg=COLORS["close_hover"]))
        close.bind("<Leave>", lambda e: close.configure(fg=COLORS["close"]))
        close.bind("<Button-1>", lambda e: self.close())

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="Close all", command=manager.close_all)
        self.menu.add_separator()
        self.menu.add_command(label="Quit client", command=manager.quit)

        for widget in self._walk(self.win):
            if widget is close:
                continue
            widget.bind("<Button-1>", self.on_click)
            widget.bind("<Button-3>", self.on_menu)
            widget.bind("<Enter>", self.on_enter)
            widget.bind("<Leave>", self.on_leave)
            if event.get("url"):
                try:
                    widget.configure(cursor="hand2")
                except tk.TclError:
                    pass

        self.win.update_idletasks()
        self.height = self.win.winfo_reqheight()
        self.win.geometry(f"{width}x{self.height}")
        round_corners(self.win)

    # -- building helpers ---------------------------------------------------

    def _badge(self, parent, name, accent, kind):
        """No avatar: rounded square badge, like the Notification Center."""
        size = self.ICON
        canvas = tk.Canvas(parent, width=size, height=size, bg=COLORS["bg"],
                           highlightthickness=0, bd=0)
        rounded_rect(canvas, 1, 1, size - 1, size - 1, 12, fill=accent, outline="")
        label = (name or "").strip()
        glyph = label[0].upper() if label else GLYPHS.get(kind, "i")
        canvas.create_text(size / 2, size / 2 + 1, text=glyph, fill="#FFFFFF",
                           font=self.manager.f_badge)
        canvas.pack(side="left", padx=(0, 12))

    @staticmethod
    def _clip(text, limit):
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _footer(event):
        ctx = event.get("context") or {}
        parts = []
        if ctx.get("guild"):
            parts.append(str(ctx["guild"]))
        if ctx.get("channel"):
            channel = str(ctx["channel"])
            parts.append(channel if channel.startswith("#") or " " in channel
                         else "#" + channel)
        elif not ctx.get("guild") and (event.get("author") or {}).get("name"):
            parts.append("Direct message")
        files = event.get("attachments") or []
        if files:
            parts.append(f"{len(files)} attachment(s)")
        return "  ·  ".join(parts)

    @staticmethod
    def _walk(root):
        stack, out = [root], []
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(node.winfo_children())
        return out

    def _tint(self, color):
        for widget in self._walk(self.win):
            try:
                if widget.cget("bg") in (COLORS["bg"], COLORS["bg_hover"]):
                    widget.configure(bg=color)
            except tk.TclError:
                pass

    # -- interaction --------------------------------------------------------

    def on_click(self, _event=None):
        url = self.event.get("url")
        if url:
            open_url(url, bool(self.cfg.get("open_in_app", True)))
        self.close()

    def on_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def on_enter(self, _e=None):
        self.paused = True
        self._tint(COLORS["bg_hover"])

    def on_leave(self, _e=None):
        self.paused = False
        self._tint(COLORS["bg"])

    # -- lifecycle ----------------------------------------------------------

    def show(self, y):
        x = self.manager.x_for(self.cfg.get("width", 380))
        self.target_x = x
        if self.cfg.get("animate", True):
            offset = -36 if self.manager.on_left() else 36
            self.win.geometry(f"+{x + offset}+{y}")
        else:
            self.win.geometry(f"+{x}+{y}")
        self.y = y
        self.win.deiconify()
        self._fade_in()

        duration = float(self.cfg.get("duration", 8) or 0)
        if duration > 0:
            self.total = duration
            self.deadline = time.time() + duration
            self._tick()

    def move(self, y):
        self.y = y
        try:
            self.win.geometry(f"+{self.target_x}+{y}")
        except (tk.TclError, AttributeError):
            pass

    def _fade_in(self):
        self.alpha = min(1.0, self.alpha + 0.12)
        try:
            self.win.attributes("-alpha", self.alpha)
            if self.cfg.get("animate", True):
                offset = -36 if self.manager.on_left() else 36
                x = int(self.target_x + offset * (1.0 - self.alpha))
                self.win.geometry(f"+{x}+{self.y}")
        except (tk.TclError, AttributeError):
            return
        if self.alpha < 1.0:
            self.win.after(16, self._fade_in)

    def _tick(self):
        """Runs the countdown and the progress bar; freezes while hovered."""
        if self.closing:
            return
        try:
            if self.paused:
                self.deadline = time.time() + self._remaining
            else:
                self._remaining = max(0.0, self.deadline - time.time())
                if self.progress is not None:
                    self.progress.place_configure(
                        relwidth=max(0.0, self._remaining / self.total))
                if self._remaining <= 0:
                    self.close()
                    return
        except (tk.TclError, AttributeError):
            return
        self.timer = self.win.after(50, self._tick)

    _remaining = 0.0

    def close(self, _e=None):
        if self.closing:
            return
        self.closing = True
        if self.timer:
            try:
                self.win.after_cancel(self.timer)
            except Exception:
                pass
        self._fade_out()

    def _fade_out(self):
        self.alpha -= 0.14
        try:
            if self.alpha <= 0:
                self.win.destroy()
                self.manager.remove(self)
                return
            self.win.attributes("-alpha", self.alpha)
            self.win.after(16, self._fade_out)
        except tk.TclError:
            self.manager.remove(self)


def open_url(url, prefer_app=True):
    """Try the Discord app first, fall back to the browser.

    Two shapes arrive: showDM sends jump_url (https://discord.com/channels/...)
    and the Notification Center sends discord://discord.com/channels/... . Both
    become the canonical discord://-/channels/... that the app understands.
    """
    if not url:
        return

    path = None
    if "/channels/" in url:
        path = "/channels" + url.split("/channels", 1)[1]

    if prefer_app and path and hasattr(os, "startfile"):
        try:
            os.startfile("discord://-" + path)
            return
        except Exception as e:
            log(f"deep link failed: {e}")

    target = ("https://discord.com" + path) if path else url
    if target.startswith("discord://"):
        # No known http equivalent: last resort through the shell.
        try:
            if hasattr(os, "startfile"):
                os.startfile(target)
        except Exception as e:
            log(f"shell failed: {e}")
        return
    try:
        webbrowser.open(target)
    except Exception as e:
        log(f"browser failed: {e}")


# ══════════════════════════════════════════════════════════════════════════
# Manager: toast stack + queue pump
# ══════════════════════════════════════════════════════════════════════════

class ToastManager:
    GAP = 10

    def __init__(self, cfg):
        self.cfg = cfg
        self.active = []
        self.queue = queue.Queue()
        self.online = False

        self.root = tk.Tk()
        self.root.withdraw()

        self.f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_text = tkfont.Font(family="Segoe UI", size=9)
        self.f_footer = tkfont.Font(family="Segoe UI", size=8)
        self.f_close = tkfont.Font(family="Segoe UI", size=9)
        self.f_badge = tkfont.Font(family="Segoe UI", size=15, weight="bold")

        self.avatars = AvatarCache(
            enabled=bool(cfg.get("avatar", True)),
            circular=bool(cfg.get("avatar_circular", True)),
        )
        self.default_icon = None      # from /settings toastsettings image_url
        self.vps_settings = {}

        self.reader = SSEReader(cfg, self.queue)
        self.reader.start()
        self.root.after(100, self.pump)

    # -- placement ----------------------------------------------------------

    def on_top(self):
        return str(self.cfg.get("position", "top-right")).startswith("top")

    def on_left(self):
        return str(self.cfg.get("position", "top-right")).endswith("left")

    def x_for(self, width):
        margin = int(self.cfg.get("margin_x", 24))
        if self.on_left():
            return margin
        return self.root.winfo_screenwidth() - width - margin

    def relayout(self):
        margin = int(self.cfg.get("margin_y", 24))
        if self.on_top():
            y = margin
            for t in self.active:
                t.move(y)
                y += t.height + self.GAP
        else:
            y = self.root.winfo_screenheight() - margin
            for t in self.active:
                y -= t.height
                t.move(y)
                y -= self.GAP

    def next_y(self, height):
        margin = int(self.cfg.get("margin_y", 24))
        if self.on_top():
            return margin + sum(t.height + self.GAP for t in self.active)
        base = self.root.winfo_screenheight() - margin
        base -= sum(t.height + self.GAP for t in self.active)
        return base - height

    # -- loop ---------------------------------------------------------------

    def pump(self):
        try:
            while True:
                self.handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def handle(self, event):
        status = event.get("_status")
        if status == "online":
            if not self.online:
                self.online = True
                self.render({
                    "title": "Nighty Remote Toast",
                    "text": f"Connected to {self.cfg['host']}:{self.cfg['port']}.",
                    "type": "SUCCESS",
                })
            return
        if status == "offline":
            self.online = False
            return
        if status == "auth":
            self.render({
                "title": "Nighty Remote Toast",
                "text": "Token rejected by the server. Check the token in "
                        "toast_client.json.",
                "type": "ERROR",
            })
            return
        if event.get("kind") == "config":
            self.apply_config(event)
            return
        if event.get("kind") == "toast":
            self.render(event)

    def apply_config(self, event):
        """Applies `/settings toastsettings` from the VPS: side, duration_ms,
        image_url. Only touches what Nighty actually defines; everything else
        keeps coming from toast_client.json. With follow_vps=false, ignored."""
        if not self.cfg.get("follow_vps", True) or not event.get("follow", True):
            return

        s = event.get("settings") or {}
        if s == self.vps_settings:
            return
        self.vps_settings = s

        side = str(s.get("side") or "").lower()
        if side in ("left", "right"):
            self.cfg["position"] = f"top-{side}"

        ms = s.get("duration_ms")
        try:
            if ms is not None and float(ms) > 0:
                self.cfg["duration"] = float(ms) / 1000.0
        except (TypeError, ValueError):
            pass

        url = s.get("image_url")
        self.default_icon = self.avatars.get_icon(url) if url else None

        log(f"applied VPS config: side={s.get('side')} "
            f"duration_ms={s.get('duration_ms')} image={'yes' if url else 'no'}")
        self.relayout()

    def render(self, event):
        limit = int(self.cfg.get("max_visible", 4))
        while len(self.active) >= limit:
            self.active[0].close()
            # close() is asynchronous (fade); drop it from the list right away
            # so the stack does not keep growing.
            if self.active and self.active[0].closing:
                self.active.pop(0)
                self.relayout()

        try:
            toast = Toast(self, event)
        except Exception:
            log("failed to build toast:\n" + traceback.format_exc())
            return

        y = self.next_y(toast.height)
        self.active.append(toast)
        toast.show(y)

        if self.cfg.get("sound") and winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    def remove(self, toast):
        if toast in self.active:
            self.active.remove(toast)
        self.relayout()

    def close_all(self):
        for t in list(self.active):
            t.close()

    def quit(self):
        self.reader.stop.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main():
    if not single_instance():
        log("another client is already running; exiting.")
        if not QUIET:
            alert("Nighty Remote Toast",
                  "The client is already running.\n\n"
                  "If you cannot see it, right click a toast and choose "
                  "'Quit client', or end pythonw.exe in Task Manager.", "warn")
        return

    cfg = load_config()

    if cfg is None:
        alert("Nighty Remote Toast",
              f"Created the configuration file:\n\n{CONFIG_PATH}\n\n"
              "Fill in host, port and token, then run it again.")
        return
    if cfg is False:
        return                      # broken JSON; load_config already warned

    if cfg["host"] == DEFAULTS["host"] or cfg["token"] == DEFAULTS["token"]:
        alert("Nighty Remote Toast",
              "The configuration still has the example values.\n\n"
              f"Edit {CONFIG_PATH} and fill in host and token.", "warn")
        return

    ToastManager(cfg).run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("crash:\n" + traceback.format_exc())
        raise
