"""
NIGHTY REMOTE TOAST  -  autostart panel
=======================================

Same purpose as nighty_toast_client.pyw, with autostart built in.

  - Want autostart?  open THIS file.
  - Don't want it?   open nighty_toast_client.pyw instead.

Double clicking this file opens a small panel to turn autostart on and off and
to start or stop the client. When Windows launches it at logon it passes `--boot` and
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
LOCK_PORT = 48888          # matches nighty_toast_client.pyw

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
    """Detected through the client's Win32 mutex and socket lock."""
    try:
        import ctypes
        mutex = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\NightyToastClient_Mutex")
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
            return True
    except Exception:
        pass
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


def find_client_pids():
    """Finds PIDs of running nighty_toast_client instances."""
    pids = set()
    try:
        r = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
        )
        for line in r.stdout.splitlines():
            if f":{LOCK_PORT}" in line and "LISTENING" in line:
                parts = line.strip().split()
                pids.add(int(parts[-1]))
    except Exception:
        pass
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.name().lower()
                if "python" in name:
                    cmd = " ".join(proc.cmdline() or [])
                    if "nighty_toast_client.pyw" in cmd and proc.pid != os.getpid():
                        pids.add(proc.pid)
            except Exception:
                pass
    except Exception:
        pass
    return list(pids)


def stop_client():
    """Terminates any running client processes."""
    pids = find_client_pids()
    stopped = False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
            stopped = True
        except Exception:
            pass
        try:
            os.kill(pid, 9)
            stopped = True
        except Exception:
            pass
    return stopped


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
        self.btn_stop = self._button(body, "Stop Client", self.stop, danger=True)
        self._button(body, "Close", self.root.destroy, quiet=True)

        tk.Label(body, text="With autostart on, the client launches by itself\n"
                            "every time you sign in to Windows and runs in\n"
                            "the system tray with the official Nighty icon.",
                 bg=COLORS["bg"], fg=COLORS["muted"], font=f_text,
                 justify="left").pack(anchor="w", pady=(12, 0))

        self.refresh()
        self.root.eval("tk::PlaceWindow . center")
        self._poll_status()

    def _button(self, parent, text, command, quiet=False, danger=False):
        bg_col = COLORS["off"] if danger else (COLORS["card"] if quiet else COLORS["accent"])
        fg_col = "#FFFFFF" if (danger or not quiet) else COLORS["text"]
        b = tk.Button(parent, text=text, command=command, font=self.f_button,
                      bg=bg_col, fg=fg_col,
                      activebackground=COLORS["border"] if quiet else bg_col,
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
            state=("disabled" if running else "normal"),
            bg=(COLORS["card"] if running else COLORS["accent"]),
            fg=(COLORS["muted"] if running else "#FFFFFF"))

        self.btn_stop.configure(
            text="Stop Client",
            state=("normal" if running else "disabled"),
            bg=(COLORS["off"] if running else COLORS["card"]),
            fg=("#FFFFFF" if running else COLORS["muted"]))

    def _poll_status(self):
        try:
            self.refresh()
            self.root.after(2000, self._poll_status)
        except Exception:
            pass

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

    def stop(self):
        try:
            stop_client()
        except Exception as e:
            messagebox.showerror("Nighty Remote Toast", f"Could not stop:\n\n{e}")
            return
        self.root.after(400, self.refresh)

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
