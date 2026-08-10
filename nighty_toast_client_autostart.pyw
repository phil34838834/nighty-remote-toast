"""
NIGHTY REMOTE TOAST  -  autostart panel
=======================================

Same purpose as nighty_toast_client.pyw, with autostart built in.

  - Want autostart?  open THIS file.
  - Don't want it?   open nighty_toast_client.pyw instead.

Double clicking this file opens a small panel to turn autostart on and off and
to start the client. When Windows launches it at logon it passes `--boot` and
the client starts straight away, with no panel.

It does not duplicate the client: it imports nighty_toast_client.pyw from the
same folder. Both files must live together.

Dependencies: none. The shortcut is created through PowerShell, which ships
with Windows.
"""

import importlib.machinery
import importlib.util
import os
import socket
import subprocess
import sys
import traceback

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.abspath(__file__)
CLIENT = os.path.join(HERE, "nighty_toast_client.pyw")
SHORTCUT_NAME = "Nighty Remote Toast.lnk"
LOCK_PORT = 50787          # must match the client

COLORS = {
    "bg": "#16171A", "card": "#1D1F24", "border": "#2A2C31",
    "title": "#F2F3F5", "text": "#B5BAC1", "muted": "#72767D",
    "on": "#43B581", "off": "#ED4245", "accent": "#40A0C6",
}

CREATE_NO_WINDOW = 0x08000000


# ── autostart ───────────────────────────────────────────────────────────────

def startup_folder():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def shortcut_path():
    return os.path.join(startup_folder(), SHORTCUT_NAME)


def is_enabled():
    return os.path.exists(shortcut_path())


def pythonw_exe():
    """pythonw.exe runs without a console window. If we are under python.exe
    (started from a terminal), switch to the pythonw next to it."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(sibling):
            return sibling
    return exe


def enable():
    """Creates the Startup shortcut pointing at this script with --boot."""
    # Paths travel through environment variables so quoting and non-ASCII
    # characters can never break the PowerShell command.
    env = dict(os.environ,
               NT_LNK=shortcut_path(),
               NT_EXE=pythonw_exe(),
               NT_ARGS=f'"{SELF}" --boot',
               NT_DIR=HERE)
    script = (
        '$ws = New-Object -ComObject WScript.Shell; '
        '$l = $ws.CreateShortcut($env:NT_LNK); '
        '$l.TargetPath = $env:NT_EXE; '
        '$l.Arguments = $env:NT_ARGS; '
        '$l.WorkingDirectory = $env:NT_DIR; '
        '$l.Description = "Nighty Remote Toast - local client"; '
        '$l.Save()'
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode != 0 or not is_enabled():
        raise RuntimeError((r.stderr or r.stdout or "unknown failure").strip())


def disable():
    try:
        os.remove(shortcut_path())
    except FileNotFoundError:
        pass


# ── client ──────────────────────────────────────────────────────────────────

def client_running():
    """Detected through the client's single-instance lock."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return False           # bind succeeded => nobody is running
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def import_client():
    loader = importlib.machinery.SourceFileLoader("nighty_toast_client", CLIENT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)   # does not call main(): the __main__ guard misses
    return module


def run_at_boot():
    """Logon path: this process becomes the client, no panel involved."""
    try:
        import_client().main()
    except Exception:
        try:
            with open(os.path.join(HERE, "autostart_error.log"), "a",
                      encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise


def start_detached():
    """Panel path: launch the client as its own process and get out of the way."""
    subprocess.Popen([pythonw_exe(), CLIENT], cwd=HERE,
                     creationflags=CREATE_NO_WINDOW)


# ── panel ───────────────────────────────────────────────────────────────────

class Panel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Nighty Remote Toast")
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(False, False)

        f_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        f_text = tkfont.Font(family="Segoe UI", size=9)
        f_status = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.f_button = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        body = tk.Frame(self.root, bg=COLORS["bg"], padx=22, pady=18)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Nighty Remote Toast", bg=COLORS["bg"],
                 fg=COLORS["title"], font=f_title).pack(anchor="w")
        tk.Label(body, text="Local notification client", bg=COLORS["bg"],
                 fg=COLORS["muted"], font=f_text).pack(anchor="w")

        panel = tk.Frame(body, bg=COLORS["card"], padx=14, pady=12,
                         highlightbackground=COLORS["border"], highlightthickness=1)
        panel.pack(fill="x", pady=(16, 14))

        self.lbl_auto = tk.Label(panel, bg=COLORS["card"], font=f_status,
                                 anchor="w", justify="left")
        self.lbl_auto.pack(anchor="w")
        self.lbl_run = tk.Label(panel, bg=COLORS["card"], font=f_status,
                                anchor="w", justify="left")
        self.lbl_run.pack(anchor="w", pady=(4, 0))

        self.btn_auto = self._button(body, "", self.toggle)
        self.btn_run = self._button(body, "Start client now", self.start)
        self._button(body, "Close", self.root.destroy, quiet=True)

        tk.Label(body, text="With autostart on, the client launches by itself\n"
                            "every time you sign in to Windows.",
                 bg=COLORS["bg"], fg=COLORS["muted"], font=f_text,
                 justify="left").pack(anchor="w", pady=(12, 0))

        self.refresh()
        self.root.eval("tk::PlaceWindow . center")

    def _button(self, parent, text, command, quiet=False):
        b = tk.Button(parent, text=text, command=command, font=self.f_button,
                      bg=COLORS["card"] if quiet else COLORS["accent"],
                      fg=COLORS["text"] if quiet else "#FFFFFF",
                      activebackground=COLORS["border"] if quiet else COLORS["accent"],
                      activeforeground="#FFFFFF",
                      relief="flat", bd=0, cursor="hand2", pady=8)
        b.pack(fill="x", pady=(0, 8))
        return b

    def refresh(self):
        auto = is_enabled()
        self.lbl_auto.configure(
            text=("Autostart: ON" if auto else "Autostart: off"),
            fg=(COLORS["on"] if auto else COLORS["off"]))
        self.btn_auto.configure(
            text=("Disable autostart" if auto else "Enable autostart"))

        running = client_running()
        self.lbl_run.configure(
            text=("Client: running" if running else "Client: stopped"),
            fg=(COLORS["on"] if running else COLORS["muted"]))
        self.btn_run.configure(
            text=("Client is already running" if running else "Start client now"),
            state=("disabled" if running else "normal"))

    def toggle(self):
        try:
            if is_enabled():
                disable()
                messagebox.showinfo("Nighty Remote Toast", "Autostart disabled.")
            else:
                enable()
                messagebox.showinfo(
                    "Nighty Remote Toast",
                    "Autostart enabled.\n\nThe client will start by itself the "
                    "next time you sign in to Windows.")
        except Exception as e:
            messagebox.showerror("Nighty Remote Toast",
                                 f"Could not change autostart:\n\n{e}")
        self.refresh()

    def start(self):
        try:
            start_detached()
        except Exception as e:
            messagebox.showerror("Nighty Remote Toast", f"Could not start:\n\n{e}")
            return
        self.root.after(1200, self.refresh)

    def run(self):
        self.root.mainloop()


def main():
    if not os.path.exists(CLIENT):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Nighty Remote Toast",
            f"Could not find the client:\n\n{CLIENT}\n\n"
            "Both files must be in the same folder.")
        return

    if "--boot" in sys.argv:
        run_at_boot()          # Windows logon: no panel
        return

    Panel().run()


if __name__ == "__main__":
    main()
