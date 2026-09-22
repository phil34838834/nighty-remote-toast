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

To start it automatically with Windows, use `nighty_toast_autostart.pyw`
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
LOCK_PORT = 48888                        # local port used only as a single-instance lock

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
    "width": 510,
    "duration": 8,               # seconds on screen (0 = stay until clicked)
    "max_visible": 4,
    "avatar": False,             # original Nighty uses the authentic vector circle icons
    "avatar_circular": True,
    "sound": False,
    "open_in_app": True,         # open the Discord app instead of the browser
    "progress_bar": False,       # original Nighty has no bottom progress bar
    "animate": True,             # slide + fade in
    "monitor": "primary",        # "primary" | "secondary" | 1 | 2 (select which display shows toasts)

    # Obey `/settings toastsettings` from the VPS (side, duration_ms, image_url).
    # Set to false to let this file win.
    "follow_vps": True,
}

# Authentic Nighty palette & styling
TARGET_ALPHA = 0.92

COLORS = {
    "bg_fallback": "#020306",
    "title": "#FFFFFF",
    "text": "#E5E5E5",
    "footer": "#72767D",
    "close": "#1664B8",
    "close_hover": "#40A0C6",
}

ACCENTS = {
    "INFO": "#40A0C6",           # Nighty cyan/blue
    "SUCCESS": "#43B581",        # Emerald green
    "ERROR": "#ED4245",          # Red
    "WARNING": "#FAA61A",        # Amber
}

GRADIENTS = {
    "INFO": ((1, 2, 5), (1, 4, 24)),
    "SUCCESS": ((1, 3, 1), (8, 22, 10)),
    "ERROR": ((4, 1, 1), (24, 5, 5)),
    "WARNING": ((4, 3, 1), (24, 16, 4)),
}

# Official Nighty 'N' logo URL
NIGHTY_LOGO_URL = (
    "https://nighty.one/_next/image?url=%2Fassets%2Fimg%2Fnighty%40500px.png&w=32&q=75"
)


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


_mutex = None
_lock_socket = None
_manager_instance = None


def ensure_single_instance():
    """Guarantees that it is impossible to have two instances running simultaneously.

    1. Checks a Win32 Named Mutex ('Local\\NightyToastClient_Mutex').
    2. Binds a local TCP socket on 127.0.0.1:48888.
    3. If another instance is already running:
       Sends a 'WAKEUP' ping to the existing instance so it brings up a toast
       notifying the user that it is already active in the system tray,
       and then this process exits immediately without spawning another instance.
    """
    global _mutex, _lock_socket

    # 1. Win32 Named Mutex
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Local\\NightyToastClient_Mutex"
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            _notify_existing_and_exit()
            return False
    except Exception as e:
        log(f"mutex check warning: {e}")

    # 2. Local Socket Lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(2)
        _lock_socket = s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        _notify_existing_and_exit()
        return False

    # Start listener thread for wakeup pings from subsequent launch attempts
    def _listen_for_pings():
        while True:
            try:
                conn, _ = _lock_socket.accept()
                data = conn.recv(128)
                conn.close()
                if b"WAKEUP" in data:
                    if _manager_instance:
                        _manager_instance.root.after(0, _manager_instance.on_second_instance_launch)
            except Exception:
                break

    threading.Thread(target=_listen_for_pings, daemon=True).start()
    return True


def _notify_existing_and_exit():
    """Signals the existing instance that another launch was attempted, then exits immediately."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", LOCK_PORT))
        s.sendall(b"WAKEUP\n")
        s.close()
    except Exception:
        pass
    log("Another client instance was launched; notified existing instance and exiting.")
    sys.exit(0)


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
# Images & Graphics Helpers
# ══════════════════════════════════════════════════════════════════════════

class AvatarCache:
    """Downloads and converts avatars. Tk only reads PNG/GIF, so we ask the
    Discord CDN for PNG explicitly."""

    def __init__(self, enabled=False, circular=True, size=22):
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
        """Generic icon (the image_url from /settings toastsettings)."""
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        image = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=6).read()
            image = tk.PhotoImage(data=raw)
            factor = max(1, image.width() // self.size)
            if factor > 1:
                image = image.subsample(factor, factor)
            if self.circular:
                self.mask_circle(image)
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
            pass


def make_gradient_ppm(w, h, c1, c2):
    """Generates an in-memory binary PPM image with a smooth horizontal gradient."""
    w = max(1, int(w))
    h = max(1, int(h))
    row = bytearray()
    for x in range(w):
        t = x / max(1, w - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        row.extend([r, g, b])
    return bytes(bytearray(f"P6\n{w} {h}\n255\n".encode("ascii")) + row * h)


def make_rounded_gradient(w, h, c1, c2, radius=18):
    """Generates an in-memory PNG image with a smooth horizontal gradient and rounded corners."""
    try:
        from PIL import Image, ImageDraw
        import io

        w = max(1, int(w))
        h = max(1, int(h))
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)

        row = bytearray()
        for x in range(w):
            t = x / max(1, w - 1)
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            row.extend([r, g, b])
        base = Image.frombytes("RGB", (w, h), bytes(row * h))
        base.putalpha(mask)

        bio = io.BytesIO()
        base.save(bio, format="PNG")
        return bio.getvalue()
    except Exception:
        return make_gradient_ppm(w, h, c1, c2)


def round_corners(window, width, height, radius=18):
    """Applies true OS-level rounded window corners on Windows via GDI region."""
    try:
        import ctypes
        window.update_idletasks()
        hwnd = window.winfo_id()
        root_hwnd = ctypes.windll.user32.GetAncestor(hwnd, 2)
        for h in {hwnd, root_hwnd}:
            if h:
                rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                    0, 0, int(width) + 1, int(height) + 1, int(radius * 2), int(radius * 2)
                )
                ctypes.windll.user32.SetWindowRgn(h, rgn, True)
    except Exception as e:
        log(f"round_corners: {e}")


def get_monitors():
    """Returns a list of connected monitors with work areas (excluding taskbar)."""
    monitors = []
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        def _cb(hMonitor, hdcMonitor, lprcMonitor, dwData):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(hMonitor, ctypes.byref(mi))
            is_primary = bool(mi.dwFlags & 1)
            r = mi.rcWork
            monitors.append({
                "index": len(monitors) + 1,
                "primary": is_primary,
                "x": r.left,
                "y": r.top,
                "width": r.right - r.left,
                "height": r.bottom - r.top,
            })
            return True

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
        )
        ctypes.windll.user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
    except Exception as e:
        log(f"get_monitors error: {e}")
    return monitors


def draw_icon(canvas, x, y, kind, accent):
    """Draws the authentic Nighty vector circle icon (diameter 22px)."""
    # Circular outline
    canvas.create_oval(x, y, x + 22, y + 22, outline=accent, width=2)

    if kind == "SUCCESS":
        # Checkmark ✓
        canvas.create_line(x + 6, y + 11, x + 10, y + 16, fill=accent, width=2, capstyle="round")
        canvas.create_line(x + 10, y + 16, x + 17, y + 6, fill=accent, width=2, capstyle="round")
    elif kind == "ERROR":
        # Exclamation mark !
        canvas.create_line(x + 11, y + 5, x + 11, y + 13, fill=accent, width=2, capstyle="round")
        canvas.create_oval(x + 10, y + 15, x + 12, y + 17, fill=accent, outline=accent)
    elif kind == "WARNING":
        # Exclamation mark !
        canvas.create_line(x + 11, y + 5, x + 11, y + 13, fill=accent, width=2, capstyle="round")
        canvas.create_oval(x + 10, y + 15, x + 12, y + 17, fill=accent, outline=accent)
    else:
        # INFO: 'i'
        canvas.create_oval(x + 10, y + 4, x + 12, y + 6, fill=accent, outline=accent)
        canvas.create_line(x + 11, y + 8, x + 11, y + 17, fill=accent, width=2)


# ══════════════════════════════════════════════════════════════════════════
# Toast
# ══════════════════════════════════════════════════════════════════════════

class Toast:
    PADDING_X = 22
    PADDING_Y = 18

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
        self._bg_img = None
        self._avatar_img = None
        self._anim_step = 0
        self._anim_total = 22

        kind = str(event.get("type", "INFO")).upper()
        accent = ACCENTS.get(kind, ACCENTS["INFO"])
        c1, c2 = GRADIENTS.get(kind, GRADIENTS["INFO"])

        # Nighty width: standard is ~510px matching original screenshots
        w = int(self.cfg.get("width") or 510)
        if w == 380:
            w = 510
        self.width = w

        self.win = tk.Toplevel(manager.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        try:
            self.win.wm_attributes("-transparentcolor", "#000001")
        except (tk.TclError, AttributeError):
            pass
        self.win.configure(bg="#000001")

        self.canvas = tk.Canvas(self.win, width=w, highlightthickness=0, bd=0, bg="#000001")
        self.canvas.pack(fill="both", expand=True)

        title_text = self._clip(event.get("title") or "Nighty", 60)
        body_text = (event.get("text") or "").strip()
        footer_text = self._footer(event)

        # Content column offset to place it beside the Nighty 'N' logo
        content_x = 78
        wrap_width = max(200, w - content_x - 32)

        # Measure content height dynamically
        current_bottom = 40
        t_id = None
        if body_text:
            t_id = self.canvas.create_text(
                content_x, 52, text=body_text,
                fill=COLORS["text"], font=manager.f_text, anchor="nw", width=wrap_width
            )
            self.win.update_idletasks()
            bbox = self.canvas.bbox(t_id)
            if bbox:
                current_bottom = bbox[3]

        f_id = None
        foot_y = current_bottom + (6 if body_text else 16)
        if footer_text:
            f_id = self.canvas.create_text(
                content_x, foot_y, text=footer_text,
                fill=COLORS["footer"], font=manager.f_footer, anchor="nw", width=wrap_width
            )
            self.win.update_idletasks()
            bbox_f = self.canvas.bbox(f_id)
            if bbox_f:
                current_bottom = bbox_f[3]

        self.height = max(68, current_bottom + self.PADDING_Y)

        self.win.geometry(f"{w}x{self.height}")
        self.canvas.configure(height=self.height)
        round_corners(self.win, w, self.height, radius=18)

        # Draw gradient background (smooth anti-aliased rounded rectangle)
        self._bg_img = tk.PhotoImage(data=make_rounded_gradient(w, self.height, c1, c2, radius=18))
        self.canvas.create_image(0, 0, image=self._bg_img, anchor="nw")

        # Nighty 'N' logo placed beside the content, vertically centered
        self.logo_item = None
        if manager.nighty_logo is not None:
            logo_y = (self.height - manager.nighty_logo.height()) // 2
            self.logo_item = self.canvas.create_image(22, logo_y, image=manager.nighty_logo, anchor="nw")

        # Notification Type Icon (i / ✓ / !) - Uniform authentic Nighty design across ALL notifications
        icon_x = content_x
        icon_y = 18
        draw_icon(self.canvas, icon_x, icon_y, kind, accent)
        title_x = icon_x + 34
        title_y = icon_y + 11

        # Title
        self.canvas.create_text(
            title_x, title_y, text=title_text,
            fill=COLORS["title"], font=manager.f_title, anchor="w"
        )

        # Close button ('✕') at top-right
        cx, cy = w - 30, 28
        self.close_l1 = self.canvas.create_line(
            cx - 6, cy - 6, cx + 6, cy + 6,
            fill=COLORS["close"], width=2.5, capstyle="round", tags="close"
        )
        self.close_l2 = self.canvas.create_line(
            cx + 6, cy - 6, cx - 6, cy + 6,
            fill=COLORS["close"], width=2.5, capstyle="round", tags="close"
        )
        # Larger hit area for easy click
        self.canvas.create_rectangle(w - 52, 0, w, 52, fill="", outline="", tags="close")

        # Redraw text above the background
        if body_text:
            self.canvas.create_text(
                content_x, 52, text=body_text,
                fill=COLORS["text"], font=manager.f_text, anchor="nw", width=wrap_width
            )
        if footer_text:
            self.canvas.create_text(
                content_x, foot_y, text=footer_text,
                fill=COLORS["footer"], font=manager.f_footer, anchor="nw", width=wrap_width
            )

        # Remove temporary measurement text objects
        if t_id:
            self.canvas.delete(t_id)
        if f_id:
            self.canvas.delete(f_id)

        # Context menu
        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="Close all", command=manager.close_all)
        self.menu.add_separator()
        self.menu.add_command(label="Quit client", command=manager.quit)

        # Close button interactions
        self.canvas.tag_bind("close", "<Button-1>", self.on_close_click)
        self.canvas.tag_bind("close", "<Enter>", self.on_close_enter)
        self.canvas.tag_bind("close", "<Leave>", self.on_close_leave)

        # Toast-wide interactions
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_menu)
        self.canvas.bind("<Enter>", self.on_enter)
        self.canvas.bind("<Leave>", self.on_leave)

        if event.get("url"):
            self.canvas.configure(cursor="hand2")

    # -- helpers -----------------------------------------------------------

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

    def update_logo(self):
        """Draws the Nighty logo if it finishes downloading while toast is visible."""
        if self.manager.nighty_logo is not None and self.logo_item is None and not self.closing:
            try:
                logo_y = (self.height - self.manager.nighty_logo.height()) // 2
                self.logo_item = self.canvas.create_image(22, logo_y, image=self.manager.nighty_logo, anchor="nw")
            except Exception:
                pass

    # -- interaction --------------------------------------------------------

    def on_close_click(self, _e=None):
        self.close()
        return "break"

    def on_close_enter(self, _e=None):
        self.canvas.itemconfig(self.close_l1, fill=COLORS["close_hover"])
        self.canvas.itemconfig(self.close_l2, fill=COLORS["close_hover"])
        self.canvas.configure(cursor="hand2")

    def on_close_leave(self, _e=None):
        self.canvas.itemconfig(self.close_l1, fill=COLORS["close"])
        self.canvas.itemconfig(self.close_l2, fill=COLORS["close"])
        if not self.event.get("url"):
            self.canvas.configure(cursor="")

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
        try:
            self.win.attributes("-alpha", min(1.0, TARGET_ALPHA + 0.05))
        except (tk.TclError, AttributeError):
            pass

    def on_leave(self, _e=None):
        self.paused = False
        try:
            self.win.attributes("-alpha", TARGET_ALPHA)
        except (tk.TclError, AttributeError):
            pass

    # -- lifecycle & smooth ease-out animation ------------------------------

    def show(self, y):
        x = self.manager.x_for(self.width)
        self.target_x = x
        self.y = y
        self.offset = -60 if self.manager.on_left() else 60

        if self.cfg.get("animate", True):
            self.win.geometry(f"+{x + self.offset}+{y}")
            self.win.attributes("-alpha", 0.0)
            self.win.deiconify()
            round_corners(self.win, self.width, self.height, radius=18)
            self._anim_step = 0
            self._animate_in()
        else:
            self.win.geometry(f"+{x}+{y}")
            self.win.attributes("-alpha", TARGET_ALPHA)
            self.win.deiconify()
            round_corners(self.win, self.width, self.height, radius=18)

        duration = float(self.cfg.get("duration", 8) or 0)
        if duration > 0:
            self.total = duration
            self.deadline = time.time() + duration
            self._tick()

    def move(self, y, x=None):
        if x is not None:
            self.target_x = x
        self.y = y
        try:
            self.win.geometry(f"+{self.target_x}+{y}")
        except (tk.TclError, AttributeError):
            pass

    def _animate_in(self):
        """Smooth cubic ease-out slide-in + fade-in animation."""
        if self.closing:
            return
        self._anim_step += 1
        t = min(1.0, self._anim_step / self._anim_total)
        # Cubic ease-out: 1 - (1 - t)^3
        progress = 1.0 - (1.0 - t) ** 3
        cur_x = int(self.target_x + self.offset * (1.0 - progress))
        # Quadratic ease-out for alpha
        self.alpha = TARGET_ALPHA * (1.0 - (1.0 - t) ** 2)

        try:
            self.win.geometry(f"+{cur_x}+{self.y}")
            self.win.attributes("-alpha", self.alpha)
        except (tk.TclError, AttributeError):
            return

        if t < 1.0:
            self.win.after(14, self._animate_in)
        else:
            try:
                self.win.geometry(f"+{self.target_x}+{self.y}")
                self.win.attributes("-alpha", TARGET_ALPHA)
            except (tk.TclError, AttributeError):
                pass

    def _tick(self):
        """Countdown dismiss timer; pauses while mouse hovers."""
        if self.closing:
            return
        try:
            if self.paused:
                self.deadline = time.time() + self._remaining
            else:
                self._remaining = max(0.0, self.deadline - time.time())
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
        self._close_step = 0
        self._close_total = 14
        self._fade_out()

    def _fade_out(self):
        """Smooth ease-in fade-out animation."""
        self._close_step += 1
        t = min(1.0, self._close_step / self._close_total)
        fade = max(0.0, TARGET_ALPHA * (1.0 - t ** 2))
        try:
            if t >= 1.0 or fade <= 0.02:
                self.win.destroy()
                self.manager.remove(self)
                return
            self.win.attributes("-alpha", fade)
            self.win.after(14, self._fade_out)
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
    GAP = 12

    def __init__(self, cfg):
        global _manager_instance
        _manager_instance = self

        self.cfg = cfg
        self.active = []
        self.queue = queue.Queue()
        self.online = False
        self._recent_events = []
        self._raw_logo_bytes = None
        self.tray_icon = None

        self.root = tk.Tk()
        self.root.withdraw()

        self.f_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self.f_text = tkfont.Font(family="Segoe UI", size=11)
        self.f_footer = tkfont.Font(family="Segoe UI", size=9)

        # Fetch official Nighty logo from URL asynchronously
        self.nighty_logo = None
        self._load_logo()

        # Initialize official Nighty System Tray Icon
        self._init_tray()

        self.avatars = AvatarCache(
            enabled=bool(cfg.get("avatar", False)),
            circular=bool(cfg.get("avatar_circular", True)),
            size=22,
        )
        self.default_icon = None      # from /settings toastsettings image_url
        self.vps_settings = {}

        self.reader = SSEReader(cfg, self.queue)
        self.reader.start()
        self.root.after(100, self.pump)

    def on_second_instance_launch(self):
        """Notifies the user via toast when a second launch attempt is blocked."""
        self.render({
            "title": "Nighty Remote Toast",
            "text": "Client is already running in the system tray!",
            "type": "INFO",
        })

    def _create_tray_image(self):
        """Generates the official Nighty 'N' tray icon (64x64 RGBA)."""
        try:
            from PIL import Image, ImageDraw
            import io

            if self._raw_logo_bytes:
                try:
                    logo = Image.open(io.BytesIO(self._raw_logo_bytes)).convert("RGBA")
                    canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                    logo.thumbnail((54, 54), Image.Resampling.LANCZOS)
                    x = (64 - logo.width) // 2
                    y = (64 - logo.height) // 2
                    canvas.paste(logo, (x, y), logo)
                    return canvas
                except Exception:
                    pass

            # Fallback vector 'N' icon
            canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((2, 2, 61, 61), radius=16, fill=(1, 4, 24, 255), outline=(64, 160, 198, 255), width=2)
            draw.line([(18, 46), (18, 18)], fill=(64, 160, 198, 255), width=6)
            draw.line([(18, 18), (46, 46)], fill=(64, 160, 198, 255), width=6)
            draw.line([(46, 46), (46, 18)], fill=(64, 160, 198, 255), width=6)
            return canvas
        except Exception as e:
            log(f"_create_tray_image: {e}")
            return None

    def _init_tray(self):
        """Initializes the official Nighty System Tray Icon with status menu."""
        def _tray_worker():
            has_deps = False
            try:
                import pystray
                from PIL import Image
                has_deps = True
            except ImportError:
                log("pystray/Pillow missing; attempting automatic background installation via pip...")
                exe = sys.executable
                if os.path.basename(exe).lower() == "pythonw.exe":
                    sibling = os.path.join(os.path.dirname(exe), "python.exe")
                    if os.path.exists(sibling):
                        exe = sibling
                try:
                    import subprocess
                    subprocess.run(
                        [exe, "-m", "pip", "install", "--quiet", "pystray", "pillow"],
                        creationflags=0x08000000,
                        check=True,
                        timeout=60,
                    )
                    import pystray
                    from PIL import Image
                    has_deps = True
                    log("pystray and Pillow successfully installed!")
                except Exception as e:
                    log(f"auto-install pystray/pillow failed: {e}")

            if not has_deps:
                self.root.after(2000, lambda: self.render({
                    "title": "System Tray Notice",
                    "text": "To enable the tray icon, please run: pip install pystray pillow",
                    "type": "WARNING",
                }))
                return

            self.root.after(0, self._setup_tray)

        threading.Thread(target=_tray_worker, daemon=True).start()

    def _setup_tray(self):
        try:
            import pystray

            tray_img = self._create_tray_image()
            if tray_img is None:
                return

            monitors = get_monitors()
            def _is_mon_checked(val):
                cur = self.cfg.get("monitor", "primary")
                if str(cur).lower() in ("primary", "main", "1st") and str(val).lower() in ("primary", "main", "1st"):
                    return True
                if str(cur).lower() in ("secondary", "second", "2nd") and str(val).lower() in ("secondary", "second", "2nd"):
                    return True
                return str(cur) == str(val)

            display_items = [
                pystray.MenuItem("Primary Display", lambda _: self._tray_set_monitor("primary"),
                                 checked=lambda item: _is_mon_checked("primary")),
            ]
            if len(monitors) > 1:
                display_items.append(
                    pystray.MenuItem("Secondary Display", lambda _: self._tray_set_monitor("secondary"),
                                     checked=lambda item: _is_mon_checked("secondary"))
                )
            for idx, m in enumerate(monitors, 1):
                label = f"Monitor {idx} ({m['width']}x{m['height']})" + (" [Primary]" if m["primary"] else "")
                display_items.append(
                    pystray.MenuItem(label, lambda _, i=idx: self._tray_set_monitor(i),
                                     checked=lambda item, i=idx: _is_mon_checked(i))
                )

            menu = pystray.Menu(
                pystray.MenuItem("Nighty Remote Toast", None, enabled=False),
                pystray.MenuItem(lambda item: f"Status: {'Connected' if self.online else 'Connecting...'}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Display", pystray.Menu(*display_items)),
                pystray.MenuItem("Open Settings (JSON)", self._tray_open_settings, default=True),
                pystray.MenuItem("Open Log", self._tray_open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Restart Client", self._tray_restart),
                pystray.MenuItem("Quit Client", self._tray_quit),
            )

            status_text = "Connected" if self.online else "Connecting..."
            self.tray_icon = pystray.Icon(
                "NightyToast",
                tray_img,
                f"Nighty Remote Toast ({status_text})",
                menu
            )
            self.tray_icon.run_detached()
        except Exception as e:
            log(f"tray icon init failed: {e}")
            self.tray_icon = None

    def _tray_set_monitor(self, mon_val):
        self.cfg["monitor"] = mon_val
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            data["monitor"] = mon_val
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"failed to save monitor setting: {e}")
        self.root.after(0, self.relayout)
        self.root.after(0, lambda: self.render({
            "title": "Display Changed",
            "text": f"Toasts will now appear on {mon_val} display.",
            "type": "INFO",
        }))

    def _tray_open_settings(self, _icon=None, _item=None):
        try:
            if hasattr(os, "startfile"):
                os.startfile(CONFIG_PATH)
        except Exception as e:
            log(f"failed to open config: {e}")

    def _tray_open_log(self, _icon=None, _item=None):
        try:
            if hasattr(os, "startfile"):
                os.startfile(LOG_PATH)
        except Exception as e:
            log(f"failed to open log: {e}")

    def _tray_restart(self, _icon=None, _item=None):
        def _do_restart():
            try:
                import subprocess
                subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                                 cwd=HERE, close_fds=True)
            except Exception as e:
                log(f"restart failed: {e}")
            self.quit()
        self.root.after(0, _do_restart)

    def _tray_quit(self, _icon=None, _item=None):
        self.root.after(0, self.quit)

    def _load_logo(self):
        """Fetches the Nighty logo directly from the official URL."""
        try:
            req = urllib.request.Request(
                NIGHTY_LOGO_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            raw = urllib.request.urlopen(req, timeout=3).read()
            self._raw_logo_bytes = raw
            self.nighty_logo = tk.PhotoImage(data=raw)
        except Exception as e:
            log(f"initial logo fetch failed ({e}); retrying in background")
            def worker():
                try:
                    req = urllib.request.Request(
                        NIGHTY_LOGO_URL, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    raw2 = urllib.request.urlopen(req, timeout=10).read()
                    self.root.after(0, lambda: self._apply_logo(raw2))
                except Exception as e2:
                    log(f"background logo fetch failed: {e2}")

            threading.Thread(target=worker, daemon=True).start()

    def _apply_logo(self, raw_bytes):
        try:
            self._raw_logo_bytes = raw_bytes
            self.nighty_logo = tk.PhotoImage(data=raw_bytes)
            if self.tray_icon:
                try:
                    new_icon = self._create_tray_image()
                    if new_icon:
                        self.tray_icon.icon = new_icon
                except Exception:
                    pass
            for toast in list(self.active):
                toast.update_logo()
        except Exception as e:
            log(f"failed to create logo PhotoImage: {e}")

    # -- placement ----------------------------------------------------------

    def get_monitor_geometry(self):
        monitors = get_monitors()
        if not monitors:
            return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        target = self.cfg.get("monitor", "primary")
        if isinstance(target, str):
            t = target.lower().strip()
            if t in ("primary", "main", "1st"):
                for m in monitors:
                    if m["primary"]:
                        return m["x"], m["y"], m["width"], m["height"]
                return monitors[0]["x"], monitors[0]["y"], monitors[0]["width"], monitors[0]["height"]
            elif t in ("secondary", "second", "2nd"):
                for m in monitors:
                    if not m["primary"]:
                        return m["x"], m["y"], m["width"], m["height"]
                return monitors[0]["x"], monitors[0]["y"], monitors[0]["width"], monitors[0]["height"]
            try:
                target = int(target)
            except ValueError:
                target = 1
        if isinstance(target, int):
            if 1 <= target <= len(monitors):
                m = monitors[target - 1]
                return m["x"], m["y"], m["width"], m["height"]
        for m in monitors:
            if m["primary"]:
                return m["x"], m["y"], m["width"], m["height"]
        return monitors[0]["x"], monitors[0]["y"], monitors[0]["width"], monitors[0]["height"]

    def on_top(self):
        return str(self.cfg.get("position", "top-right")).startswith("top")

    def on_left(self):
        return str(self.cfg.get("position", "top-right")).endswith("left")

    def x_for(self, width):
        mx, my, mw, mh = self.get_monitor_geometry()
        margin = int(self.cfg.get("margin_x", 24))
        if self.on_left():
            return mx + margin
        return mx + mw - width - margin

    def relayout(self):
        mx, my, mw, mh = self.get_monitor_geometry()
        margin = int(self.cfg.get("margin_y", 24))
        if self.on_top():
            y = my + margin
            for t in self.active:
                x = self.x_for(t.width)
                t.move(y, x=x)
                y += t.height + self.GAP
        else:
            y = my + mh - margin
            for t in self.active:
                y -= t.height
                x = self.x_for(t.width)
                t.move(y, x=x)
                y -= self.GAP

    def next_y(self, height):
        mx, my, mw, mh = self.get_monitor_geometry()
        margin = int(self.cfg.get("margin_y", 24))
        if self.on_top():
            return my + margin + sum(t.height + self.GAP for t in self.active)
        base = my + mh - margin
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
                if self.tray_icon:
                    try:
                        self.tray_icon.title = f"Nighty Remote Toast - Connected ({self.cfg['host']}:{self.cfg['port']})"
                    except Exception:
                        pass
                self.render({
                    "title": "Connected",
                    "text": f"Connected to {self.cfg['host']}:{self.cfg['port']}",
                    "type": "SUCCESS",
                })
            return
        if status == "offline":
            self.online = False
            if self.tray_icon:
                try:
                    self.tray_icon.title = "Nighty Remote Toast - Connecting..."
                except Exception:
                    pass
            return
        if status == "auth":
            self.render({
                "title": "ERROR",
                "text": "Token rejected by the server. Check the token in toast_client.json.",
                "type": "ERROR",
            })
            return
        if event.get("kind") == "config":
            self.apply_config(event)
            return
        if event.get("kind") == "toast":
            if not self._is_duplicate(event):
                self.render(event)

    def _is_duplicate(self, event):
        """Filters duplicate notifications arriving from multiple VPS paths (showToast vs NotificationCenter)."""
        now = time.time()
        self._recent_events = [e for e in self._recent_events if now - e["time"] < 15]

        url = event.get("url")
        raw_text = f"{event.get('title', '')} {event.get('text', '')}".lower()
        import re
        norm = re.sub(r"[^a-z0-9]", "", raw_text)
        for prefix in ("yougotpinged", "ping", "directmessage"):
            if norm.startswith(prefix):
                norm = norm[len(prefix):]

        for e in self._recent_events:
            # 1. Same Discord message link
            if url and e.get("url") and url == e["url"]:
                return True
            # 2. Same normalized content
            if norm and e.get("norm"):
                if norm == e["norm"] or (len(norm) > 10 and (norm in e["norm"] or e["norm"] in norm)):
                    return True

        self._recent_events.append({"time": now, "url": url, "norm": norm})
        return False

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
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main():
    if not ensure_single_instance():
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
