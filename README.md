# Nighty Remote Toast

**Nighty runs on your VPS. The notifications pop up on your PC.**

A bridge that forwards Nighty's notifications — DMs, pings, ghostpings, daily
backup, script toasts — from a remote Nighty instance to a native-looking toast
on your own desktop. Click it and it jumps straight to the message in Discord.

No dependencies on the client side. Just Python with tkinter, which ships with
the official Windows installer.

> Implements suggestion `IGKi4i7g` — *Remote Toast Notifications for Web Version
> (VPS → Local PC)* — outside of Nighty, using only what the script runtime
> already exposes.

---

## The problem

A toast belongs to the machine Nighty runs on. Put Nighty on a VPS and every
notification pops up on a desktop nobody is looking at.

It gets worse in **web mode**: Nighty's toast is a Qt window, and with no desktop
there is no window, so **no toast is drawn at all**. Scripts like *Show DM*
become useless, and the Notification Center is the only place anything shows up.

## How it works

```
VPS                                             YOUR PC
┌─────────────────────────────┐                 ┌──────────────────────────┐
│ Nighty                      │                 │ nighty_toast_client.pyw  │
│  ├─ showToast (scripts)     │                 │                          │
│  ├─ Notification Center     │   HTTP / SSE    │  tkinter draws the toast │
│  └─ watched channels        │ ──────────────► │  and opens Discord on    │
│            │                │    /events      │  click                   │
│            ▼                │                 └──────────────────────────┘
│  nighty_toast_bridge.py     │
│   • captures                │
│   • filters                 │
│   • mirrors /settings       │
│   • aiohttp.web on :8787    │
└─────────────────────────────┘
```

### Three capture paths

| Path | Catches | Notes |
|---|---|---|
| **`showToast`** | Toasts from scripts (Show DM, your own scripts) | Instant. Silent in web mode |
| **Notification Center** | Everything: pings, ghostpings, daily backup, system | Polled every 2s. **Works in web mode** |
| **Watched channels** | Anything sent to a channel you list | How Custom Features reach your PC |

All three run together. A content hash with a 20-second window makes sure an
event caught by two paths is only shown once.

### Why SSE instead of WebSocket

Traffic is one-way (VPS → PC), and Server-Sent Events fit entirely inside
`urllib`. That is what keeps the local client dependency-free.

---

## Requirements

**VPS:** Nighty 2.6+. Nothing to install — the bridge uses `aiohttp`, which is
already bundled.

**Your PC:** Python 3.8+ with tkinter (included in the official Windows
installer — just make sure *tcl/tk* stays checked). Optionally the Discord
desktop app, so clicking a toast opens it there instead of the browser.

## Install

### 1. The bridge (VPS)

1. Copy `nighty_toast_bridge.py` into `data/scripts`
   (shortcut: `Win+R` → `%APPDATA%\Nighty Selfbot\data\scripts`)
   OR add it via "add script" in script tab and paste the content.
2. Enable the script in Nighty's **Scripts** tab.
3. Grab the token — **Toast Bridge** tab, or:

   ```
   <p>toastbridge token
   ```

4. Open the port on the VPS firewall:

   ```powershell
   New-NetFirewallRule -DisplayName "Nighty Remote Toast" -Direction Inbound `
     -Protocol TCP -LocalPort 8787 -Action Allow
   ```

   > Do not use port **80** — that belongs to Nighty's web version.

Check it came up with `<p>toastbridge`:

```
serving on .......... 0.0.0.0:8787
capture center ...... YES  (0 captured, via __all_notifications__)
center error ........ -
```

### 2. The client (your PC)

1. Put `nighty_toast_client.pyw` and `nighty_toast_client_autostart.pyw` in a folder
   that will not move, e.g. `C:\Nighty Toast\`. **Keep them together.**
2. Double click `nighty_toast_client.pyw`. It writes `toast_client.json` and exits.
3. Fill in the three fields:

   ```json
   {
     "host": "YOUR.VPS.IP",
     "port": 8787,
     "token": "the token from the Toast Bridge tab"
   }
   ```

   `host` is the bare IP — no `http://`, no port suffix.

4. Double click again. A green *"Connected to …"* toast appears.
5. Fire a real one:

   ```
   <p>toastbridge test
   ```

### 3. Autostart (optional)

Two ways to launch, pick one:

| You open | What happens |
|---|---|
| `nighty_toast_client.pyw` | Runs this once. Nothing is installed |
| `nighty_toast_client_autostart.pyw` | Opens a panel to turn autostart on and off |

The panel shows the current state (autostart on/off, client running/stopped).
Turning it on creates a shortcut in `shell:startup` pointing at `pythonw.exe`
with `nighty_toast_client_autostart.pyw --boot`; in `--boot` mode it starts the client
directly, with no panel.

It targets `pythonw.exe` rather than the `.pyw` itself so it does not depend on
the file association, which tends to break when Python is updated.

## Stopping the client

The client has no taskbar entry — it is a background process. Three ways to
stop it, easiest first:

1. **Right click any toast → "Quit client".**
2. **Task Manager** (`Ctrl+Shift+Esc`) → *Details* tab → find `pythonw.exe` →
   *End task*. If several are listed, the *Command line* column (right click the
   headers → *Select columns*) shows which one is `nighty_toast_client.pyw`.
3. **PowerShell**, if you prefer a one-liner:

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
     Where-Object CommandLine -like '*nighty_toast*' |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```

To stop it coming back on the next login, open `nighty_toast_client_autostart.pyw` and
click **Disable autostart** (or delete the shortcut from `shell:startup`).

Only one client can run at a time: it holds local port `50787` as a lock, so
opening it twice does nothing rather than doubling every toast.

---

## Commands

```
<p>toastbridge                     status
<p>toastbridge test                send a test toast
<p>toastbridge token               show the connection token
<p>toastbridge filters             list filter rules
<p>toastbridge block <pattern>     add a block rule
<p>toastbridge unblock <n|all>     remove a block rule
<p>toastbridge settings            show the mirrored /settings values
<p>toastbridge watch <channel id>  forward a channel (Custom Features)
<p>toastbridge unwatch <id|all>    stop watching
<p>toastbridge reload              re-read the JSON config from disk
```

## Filters

`/settings toastnotifications` cuts by category (pings, servers, nitro…), but it
cannot express *"I want backup complete, not every saved vanity"*. That is what
the bridge's own filters are for.

Patterns match against **`title | text`** — exactly how the Notification Center
renders a line, so you can copy one from there and paste it:

```
<p>toastbridge block Daily backup | Saved vanity invite
```

That kills the vanity spam and keeps `Daily backup | Full backup complete`.

Same thing in the **Toast Bridge** tab, or by editing
`data/scripts/scriptData/toastBridge.json` and running `<p>toastbridge reload`:

```json
"filters": {
  "block": ["Daily backup | Saved vanity invite", "Custom command used"],
  "only_allow": [],
  "regex": false
}
```

- `block` — matches never reach your PC.
- `only_allow` — if non-empty, **only** matches get through (whitelist).
- `regex` — treat patterns as regular expressions, e.g. `kicked from .*(Ghoul|Stray)`.

## Nighty's own commands drive the client

`/settings toastsettings` and `/settings toastnotifications` write to
`data/notifications.json`. The bridge reads that file, watches it for changes and
pushes them to your PC — **within 4 seconds, no restart**.

| Nighty command | Effect on your PC |
|---|---|
| `/settings toastsettings side left\|right` | Which corner the toast appears in |
| `/settings toastsettings duration_ms 6000` | How long it stays on screen |
| `/settings toastsettings image_url <url>` | The toast icon (when there is no author avatar) |
| `/settings toastnotifications event_type toggle` | Disables the event at the source |

`<p>toastbridge settings` shows what is currently mirrored. To ignore the VPS
and let the local file win, set `"follow_vps": false` in `toast_client.json`.

## Custom Features bridge

Custom Features **cannot** make HTTP requests. Every action it offers is a
Discord operation — there is no "run script", no webhook, no notification block.

But it can **Send Message**, and that is enough:

1. Build your feature as usual, with all the visual filters and conditions.
2. Add a **Send Message** action pointing at a private channel (a channel in
   your own server, or a group DM).
3. Tell the bridge to watch it:

   ```
   <p>toastbridge watch 1319137168412901417
   ```

Every message in that channel becomes a toast on your PC. The same trick works
for anything that can post to Discord — bots, webhooks, other scripts.

## For your own scripts

The bridge exposes a global:

```python
remoteToast("Payment confirmed", title="CryptoPay", type_="SUCCESS")
remoteToast(f"{msg.author.name}: {msg.clean_content}",
            title="DM", url=msg.jump_url, message=msg)
```

Passing `message=` enriches the toast with the avatar, author, server and channel.

## HTTP API

```bash
curl -X POST http://YOUR_VPS:8787/notify \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Deploy","text":"build finished","type":"SUCCESS"}'
```

| Endpoint | Method | Purpose |
|---|---|---|
| `/events` | GET | SSE stream (`?token=…`, optional `?since=N`) |
| `/notify` | POST | Push a toast from anywhere |
| `/health` | GET | Liveness check, no auth |

## Client settings

| Key | Default | What it does |
|---|---|---|
| `follow_vps` | `true` | Obey `/settings toastsettings` from the VPS |
| `position` | `top-right` | `top-left`, `top-right`, `bottom-left`, `bottom-right` |
| `duration` | `8` | Seconds on screen. `0` = stays until clicked |
| `max_visible` | `4` | How many stack before the oldest is dropped |
| `width` | `380` | Width in pixels |
| `avatar` | `true` | Download avatars; without one, a coloured badge |
| `avatar_circular` | `true` | Circular avatar mask (needs Tk 8.6+) |
| `progress_bar` | `true` | Thin bar showing the remaining time |
| `animate` | `true` | Slide + fade in |
| `sound` | `false` | Windows beep on arrival |
| `open_in_app` | `true` | Click opens the Discord app (`discord://`) |

`position` and `duration` are overridden by the VPS while `follow_vps` is on.

Interactions: **click** opens the message · **✕** dismisses · **right click** →
close all / quit · **hovering** pauses the countdown.

---

## Security

The payload carries **DM content**, and over plain HTTP that travels the
internet **in clear text**. The token protects the endpoint from strangers; it
does not encrypt anything.

For a Windows VPS the practical move is to restrict the port to your own IP:

```powershell
Remove-NetFirewallRule -DisplayName "Nighty Remote Toast"
New-NetFirewallRule -DisplayName "Nighty Remote Toast" -Direction Inbound `
  -Protocol TCP -LocalPort 8787 -RemoteAddress YOUR.HOME.IP -Action Allow
```

If your VPS runs SSH, a tunnel is better still — encrypted, and nothing is
exposed publicly. Set the bridge host to `127.0.0.1`, then:

```powershell
ssh -N -L 8787:127.0.0.1:8787 user@your-vps
```

…and point `toast_client.json` at `"host": "127.0.0.1"`.

**While you are at it**, check `%APPDATA%\Nighty Selfbot\web_config.json`. If it
has `"username": null, "password": null` with `"host": "0.0.0.0"`, your Nighty
web UI is open to the internet with no password — a much bigger problem than
anything here.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Nothing happens, no connection toast | Check `toast_client.log` next to the `.pyw` |
| `401` in the log | Token mismatch between the tab and `toast_client.json` |
| Connects then drops repeatedly | Port blocked somewhere; test `curl http://IP:8787/health` |
| `<p>toastbridge` says "not started" | Port already in use. Check the `error` line and change the port |
| Only `test` arrives, real notifications do not | Web mode: `showToast` is silent. Confirm `capture center ... YES` |
| `center error` is filled in | The notification format changed — please open an issue |
| Toast appears on the VPS but not on the PC | The script was not actually reloaded. Disable and re-enable it |
| Edited the JSON, nothing changed | `<p>toastbridge reload` |
| Toast disappears too fast or too slowly | `/settings toastsettings duration_ms`, or `"follow_vps": false` |

---

## Under the hood

None of the runtime API used here is documented — not `showToast`, not
`evaluate_js`, not `main_ui`, not `__all_notifications__`. It was all found by
enumerating the globals Nighty injects into scripts.

What matters for this project:

| Name | What it is |
|---|---|
| `showToast(text, title=, url=, message=, type_=)` | Script-facing toast |
| `__all_notifications__` | The list backing the Notification Center — the bridge's primary source |
| `main_api` | 153 members: `getAllNotifications`, `discordJump`, `hide`, `close`… |
| `notification_sender` | Instance of `NotificationSender`, which is a **PyQt QWidget** |
| `evaluate_js` / `execute_js_via_websocket` | JS execution in the UI; in web mode it runs in the browser |

That QWidget is the whole story behind web mode: the toast is a Qt window, so
with no desktop there is nothing to draw and `showToast` never fires. Hence the
Notification Center path.

### Notification format (build 2.6)

```python
{'type': 'INFO',
 'text': 'You got pinged | lipe.fros | @everyone free shop for whoever joins',
 'id': '<uuid>', 'created_at': 1786319386, 'emojis': {},
 'discordChannel': 'Direct Message with user#0000',
 'channel': {'name': '...'},
 'url': 'discord://discord.com/channels/@me/140.../153...'}
```

Two quirks the bridge handles: there is **no title field** — everything lives in
`text`, separated by `|`, so the bridge splits on the first pipe; and the `url`
already arrives as `discord://`, normalised to the canonical
`discord://-/channels/...` at click time. Ordering comes from `created_at`, not
from the position in the list.

## FAQ

**Does Nighty have to be installed on my PC?**
No. The client is a standalone Python file that only talks to your VPS.

**Does it work if Nighty runs locally?**
Yes, and you can leave `host` as `127.0.0.1`. It is mostly pointless though —
you would already see the toasts.

**Does it work in app mode as well as web mode?**
Yes. In app mode both capture paths are live; in web mode the Notification
Center path carries everything.

**Can I run it on several PCs?**
Yes. Each machine runs its own client with the same token, and each gets every
notification.

**Does it survive Nighty restarting?**
The client reconnects on its own with backoff (1s, 2s, 4s… up to 30s), and
`?since=` replays what it missed while it was away.

**Does it use much CPU?**
The center poll is a list comparison every 2 seconds. Raise `center_interval` in
the bridge config if you want it lazier.

## Credits

Built with Claude Code. Suggestion `IGKi4i7g` by `y'`.

The `showToast` interception idea builds on how Nighty shares one globals dict
across every script — which is also why a single wrapper catches Show DM and any
other script without editing them.
