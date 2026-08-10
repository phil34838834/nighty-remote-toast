@nightyScript(
    name="Toast Bridge",
    author="yuri",
    description="Forwards Nighty notifications from a VPS to your local PC over SSE.",
    usage="<p>toastbridge"
)
def ToastBridge():
    """
    NIGHTY REMOTE TOAST  -  server side (runs in Nighty, on your VPS)
    -----------------------------------------------------------------

    The problem: a toast belongs to the machine Nighty runs on. With Nighty on a
    VPS, the showDM toast pops up on a desktop nobody is looking at. And in web
    mode there is no toast at all - Nighty's toast is a Qt window, and without a
    desktop there is no window to draw.

    What this script does:

      1. Captures notifications through two independent paths (see below).
      2. Runs an HTTP server inside the bot's own event loop (aiohttp.web, which
         ships with Nighty) and publishes every notification as Server-Sent
         Events on /events.
      3. The local client (nighty_toast_client.pyw, on YOUR PC) connects to that
         endpoint and draws the toast locally, in Nighty's style.

    The two capture paths:

      * showToast - Nighty exposes `showToast` as a runtime global, and every
        script shares the same globals dict. Since Python resolves globals at
        CALL time, wrapping it here intercepts the toast of every script already
        loaded - showDM included - without editing any of them.
        Does NOT fire in web mode.

      * Notification Center - reads the `__all_notifications__` list that backs
        Nighty's Notification Center. Catches everything: pings, ghostpings,
        daily backup, system messages. Works in both app and web mode.

    A content hash with a 20s window keeps an event from being delivered twice
    when both paths catch it.

    Why SSE instead of WebSocket: traffic is one-way (VPS -> PC), and SSE fits
    entirely inside urllib. That keeps the local client dependency-free - just
    Python with tkinter, which ships with the official Windows installer.

    COMMANDS:
    <p>toastbridge                  status
    <p>toastbridge test             send a test toast
    <p>toastbridge token            show the connection token
    <p>toastbridge filters          list filter rules
    <p>toastbridge block <pattern>  add a block rule
    <p>toastbridge unblock <n|all>  remove a block rule
    <p>toastbridge settings         show the mirrored /settings values
    <p>toastbridge watch <id>       forward messages from a channel (Custom Features)
    <p>toastbridge unwatch <id|all> stop watching a channel
    <p>toastbridge reload           re-read the JSON config from disk

    NOTES:
    - Configurable from the "Toast Bridge" tab.
    - Open the chosen port on the VPS firewall (default 8787). Port 80 is
      already used by Nighty's web version, so do not reuse it.
    - The token is mandatory: without it the endpoint answers 401.
    """

    import asyncio
    import hashlib
    import inspect
    import json
    import os
    import re
    import secrets
    import time
    import traceback
    from collections import deque

    from aiohttp import web

    # ══════════════════════════════════════════════════════════════════════
    # State that survives a script reload
    #
    # Nighty re-executes the file when you save it. Without this, every save
    # would leave an orphan server holding the port and stack one showToast
    # wrapper on top of another.
    # ══════════════════════════════════════════════════════════════════════

    STATE = globals().setdefault("_TOAST_BRIDGE_STATE", {
        "runner": None,        # active web.AppRunner
        "clients": set(),      # queues of the open SSE connections
        "backlog": None,       # recent events, for reconnects
        "seq": 0,              # incrementing event counter
        "original": None,      # the real showToast, stored ONCE
        "loop": None,
        "serving": None,       # (host, port) the server came up on
        "error": None,
    })

    DATA_DIR = f"{getScriptsPath()}/scriptData"
    CONFIG_PATH = f"{DATA_DIR}/toastBridge.json"

    DEFAULTS = {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8787,
        "intercept": True,          # wrap showToast for other scripts
        "mirror_locally": True,     # still show the toast on the VPS itself
        "backlog": 50,              # events kept for reconnecting clients
        "token": "",

        # Filters match against "title | text", exactly how the Notification
        # Center displays it - so you can copy a line from there and paste it.
        "filters": {
            "block": [],            # if it matches, it does NOT reach your PC
            "only_allow": [],       # if non-empty, ONLY matches get through
            "regex": False,         # treat patterns as regular expressions
        },

        # Mirror /settings toastsettings and /settings toastnotifications
        "follow_nighty_settings": True,

        # Notification Center capture. This is the path that works in web mode,
        # where Nighty draws no toast at all but the center still fills up.
        "capture_center": True,
        "center_interval": 2.0,

        # Channel IDs whose messages become toasts. This is how Custom Features
        # reach your PC: give the feature a "Send Message" action pointing at a
        # private channel, and list that channel here.
        "watch_channels": [],
    }

    # data/notifications.json - where Nighty stores the /settings toast* values
    NOTIFICATIONS_PATH = os.path.abspath(
        os.path.join(getScriptsPath(), "..", "notifications.json")
    )

    def load_config():
        cfg = dict(DEFAULTS)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8-sig", errors="ignore") as f:
                    cfg.update(json.load(f) or {})
        except Exception:
            pass
        if not cfg.get("token"):
            cfg["token"] = secrets.token_urlsafe(24)
            save_config(cfg)
        return cfg

    def save_config(cfg):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8", errors="ignore") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            log(f"could not save config: {e}", "ERROR")

    def log(msg, level="INFO"):
        print(f"[ToastBridge] {msg}", type_=level)

    CFG = load_config()

    if STATE["backlog"] is None:
        STATE["backlog"] = deque(maxlen=max(10, int(CFG.get("backlog") or 50)))

    STATE.setdefault("settings", {})        # last snapshot of notifications.json
    STATE.setdefault("settings_mtime", 0)
    STATE.setdefault("watcher", None)
    STATE.setdefault("blocked", 0)
    STATE.setdefault("poller", None)        # task reading the Notification Center
    STATE.setdefault("center_ids", None)    # identities already seen
    STATE.setdefault("center_error", None)
    STATE.setdefault("center_total", 0)
    STATE.setdefault("center_source", None)
    STATE.setdefault("seen", {})            # hash -> ts, de-duplication
    STATE.setdefault("listener", None)      # on_message listener for watch_channels
    STATE.setdefault("channel_total", 0)
    if STATE["center_ids"] is None:
        STATE["center_ids"] = deque(maxlen=600)

    # ══════════════════════════════════════════════════════════════════════
    # Nighty's own settings
    #
    # /settings toastsettings side|duration_ms|image_url  and
    # /settings toastnotifications event_type toggle
    # both write to data/notifications.json. Reading that file lets the local
    # client obey the same commands you already use - no duplicated config.
    # ══════════════════════════════════════════════════════════════════════

    def read_nighty_settings():
        try:
            with open(NOTIFICATIONS_PATH, "r", encoding="utf-8-sig", errors="ignore") as f:
                data = json.load(f) or {}
        except Exception:
            return {}

        toast = data.get("toast") or {}
        tweaks = toast.get("settings") or {}

        # Per-event toggles (what /settings toastnotifications flips).
        events = {k: v for k, v in toast.items()
                  if isinstance(v, bool) and k != "toast"}

        return {
            "enabled": bool(toast.get("toast", True)),
            "title": tweaks.get("title"),
            "side": tweaks.get("side"),               # "left" | "right"
            "duration_ms": tweaks.get("duration_ms"),
            "image_url": tweaks.get("image_url"),
            "events": events,
        }

    def config_event():
        return {
            "v": 1,
            "kind": "config",
            "settings": STATE.get("settings") or {},
            "follow": bool(CFG.get("follow_nighty_settings", True)),
        }

    def check_settings(force=False):
        """True when something changed. The watcher uses it to notify clients."""
        try:
            mtime = os.path.getmtime(NOTIFICATIONS_PATH)
        except OSError:
            return False
        if not force and mtime == STATE.get("settings_mtime"):
            return False
        STATE["settings_mtime"] = mtime
        fresh = read_nighty_settings()
        if fresh == STATE.get("settings") and not force:
            return False
        STATE["settings"] = fresh
        return True

    check_settings(force=True)

    # ══════════════════════════════════════════════════════════════════════
    # Filters
    # ══════════════════════════════════════════════════════════════════════

    def _patterns(key):
        filters = CFG.get("filters") or {}
        values = filters.get(key) or []
        return [str(p) for p in values if str(p).strip()]

    def _matches(pattern, target):
        filters = CFG.get("filters") or {}
        if filters.get("regex"):
            try:
                return re.search(pattern, target, re.IGNORECASE) is not None
            except re.error:
                return False
        return pattern.lower() in target.lower()

    def allowed(event):
        """Matches against 'title | text', the Notification Center format."""
        target = f"{event.get('title') or ''} | {event.get('text') or ''}"

        only = _patterns("only_allow")
        if only and not any(_matches(p, target) for p in only):
            return False
        if any(_matches(p, target) for p in _patterns("block")):
            return False
        return True

    # ══════════════════════════════════════════════════════════════════════
    # De-duplication across the capture paths
    #
    # The same event can arrive through showToast AND the Notification Center.
    # A content hash kept for a few seconds prevents showing it twice.
    # ══════════════════════════════════════════════════════════════════════

    def _content_hash(title, text):
        base = f"{title or ''}|{text or ''}"
        return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()

    def mark_seen(title, text):
        now = time.time()
        STATE["seen"][_content_hash(title, text)] = now
        if len(STATE["seen"]) > 400:
            cutoff = now - 120
            for k in [k for k, v in STATE["seen"].items() if v < cutoff]:
                STATE["seen"].pop(k, None)

    def seen_recently(title, text, window=20):
        ts = STATE["seen"].get(_content_hash(title, text))
        return ts is not None and (time.time() - ts) < window

    # ══════════════════════════════════════════════════════════════════════
    # Publishing
    # ══════════════════════════════════════════════════════════════════════

    def _deliver(event):
        """Runs on the bot loop. Stores in the backlog and pushes to clients."""
        STATE["backlog"].append(event)
        dead = []
        for q in list(STATE["clients"]):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow client: drop its oldest instead of stalling the loop.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            STATE["clients"].discard(q)

    def publish(event, filtered=True):
        """Safe to call from any thread and from sync context."""
        if filtered and event.get("kind") == "toast" and not allowed(event):
            STATE["blocked"] += 1
            return False
        loop = STATE.get("loop") or getattr(bot, "loop", None)
        if loop is None or loop.is_closed():
            return False
        STATE["seq"] += 1
        event["seq"] = STATE["seq"]
        event.setdefault("ts", time.time())
        if event.get("kind") == "toast":
            mark_seen(event.get("title"), event.get("text"))
        try:
            loop.call_soon_threadsafe(_deliver, event)
        except RuntimeError:
            return False
        return True

    def _trim(value, limit=400):
        if value is None:
            return None
        s = str(value)
        return s if len(s) <= limit else s[: limit - 1] + "…"

    def build_event(text=None, title=None, url=None, kind=None, message=None,
                    source="showToast"):
        """Normalises a notification into a JSON payload, enriched when a
        discord.Message is available."""
        event = {
            "v": 1,
            "kind": "toast",
            "title": _trim(title, 120) or "Nighty",
            "text": _trim(text) or "",
            "url": _trim(url, 500),
            "type": (str(kind).upper() if kind else "INFO"),
            "source": source,
        }

        if message is not None:
            try:
                author = getattr(message, "author", None)
                if author is not None:
                    event["author"] = {
                        "name": getattr(author, "display_name", None) or getattr(author, "name", None),
                        "id": str(getattr(author, "id", "") or ""),
                    }
                    try:
                        avatar = getattr(author, "display_avatar", None) or getattr(author, "avatar", None)
                        if avatar is not None:
                            event["author"]["avatar"] = str(getattr(avatar, "url", "") or "")
                    except Exception:
                        pass

                guild = getattr(message, "guild", None)
                channel = getattr(message, "channel", None)
                event["context"] = {
                    "guild": getattr(guild, "name", None),
                    "guild_id": str(getattr(guild, "id", "") or "") or None,
                    "channel": getattr(channel, "name", None),
                    "channel_id": str(getattr(channel, "id", "") or "") or None,
                    "message_id": str(getattr(message, "id", "") or "") or None,
                }
                if not event.get("url"):
                    event["url"] = _trim(getattr(message, "jump_url", None), 500)
                try:
                    files = [str(a.url) for a in (getattr(message, "attachments", None) or [])]
                    if files:
                        event["attachments"] = files[:4]
                except Exception:
                    pass
            except Exception:
                # Enrichment is a bonus. It must never break the toast.
                pass

        return event

    # ══════════════════════════════════════════════════════════════════════
    # Capture path 1: showToast
    # ══════════════════════════════════════════════════════════════════════

    def install_patch():
        current = globals().get("showToast")
        if current is None:
            log("showToast does not exist in this build; interception disabled.", "ERROR")
            return False

        # Store the original ONCE. On reload, `current` is already our wrapper.
        if getattr(current, "_toast_bridge", False):
            original = STATE.get("original") or getattr(current, "_original", None)
        else:
            original = current
            STATE["original"] = original

        if original is None:
            log("could not find the original showToast.", "ERROR")
            return False

        def showToast_bridge(*args, **kwargs):
            # 1) Forward. Shielded: a failure here must not break the caller.
            try:
                if CFG.get("enabled") and CFG.get("intercept"):
                    text = args[0] if args else kwargs.get("text")
                    publish(build_event(
                        text=text,
                        title=kwargs.get("title"),
                        url=kwargs.get("url"),
                        kind=kwargs.get("type_") or kwargs.get("type"),
                        message=kwargs.get("message"),
                        source="showToast",
                    ))
            except Exception:
                pass

            # 2) Original behaviour preserved.
            if CFG.get("mirror_locally", True):
                return original(*args, **kwargs)
            return None

        showToast_bridge._toast_bridge = True
        showToast_bridge._original = original
        globals()["showToast"] = showToast_bridge
        return True

    def remove_patch():
        original = STATE.get("original")
        if original is not None:
            globals()["showToast"] = original

    # Explicit API for new scripts: remoteToast("text", title="...")
    def remoteToast(text=None, title=None, url=None, type_="INFO", message=None):
        """Send a toast ONLY to the local PC, skipping the VPS toast."""
        publish(build_event(text=text, title=title, url=url,
                            kind=type_, message=message, source="remoteToast"))

    globals()["remoteToast"] = remoteToast

    # ══════════════════════════════════════════════════════════════════════
    # Capture path 2: the Notification Center
    #
    # Confirmed format on build 2.6:
    #   {'type': 'INFO', 'text': 'You got pinged | someone | message',
    #    'id': '<uuid>', 'created_at': 1786319386, 'emojis': {},
    #    'discordChannel': 'Direct Message with ...', 'channel': {'name': ...},
    #    'url': 'discord://discord.com/channels/...'}
    #
    # Note there is NO title field: everything arrives in 'text', separated by
    # ' | ', which is how the Notification Center renders it. The other field
    # names below are kept as a safety net in case the format changes.
    # ══════════════════════════════════════════════════════════════════════

    TITLE_KEYS = ("title", "name", "header", "subject")
    TEXT_KEYS = ("text", "description", "message", "content", "body")
    URL_KEYS = ("url", "jump_url", "jumpUrl", "link", "href")
    ID_KEYS = ("id", "uuid", "key", "_id", "notification_id")
    TIME_KEYS = ("created_at", "createdAt", "timestamp", "time", "date")
    TYPE_KEYS = ("type", "level", "severity", "kind")
    CHANNEL_KEYS = ("discordChannel", "channel_name", "channel")

    def _field(item, keys):
        for k in keys:
            try:
                v = item.get(k) if isinstance(item, dict) else getattr(item, k, None)
            except Exception:
                v = None
            if v not in (None, ""):
                return v
        return None

    def _split_text(text):
        """'Daily backup | Full backup complete: x' -> ('Daily backup', 'Full ...').

        The center joins title and body with ' | '. With no separator, the whole
        string becomes the body and the title is left empty.
        """
        if not text:
            return None, text
        parts = str(text).split("|", 1)
        if len(parts) == 2 and parts[0].strip():
            return parts[0].strip(), parts[1].strip()
        return None, str(text).strip()

    def _when(item):
        ts = _field(item, TIME_KEYS)
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    def _flatten(data):
        """The source may return a list, or a dict grouped by category."""
        if data is None:
            return []
        if isinstance(data, (list, tuple)):
            return list(data)
        if isinstance(data, dict):
            out = []
            for v in data.values():
                if isinstance(v, (list, tuple)):
                    out.extend(v)
            return out if out else [data]
        return []

    def _identity(item):
        ident = _field(item, ID_KEYS)
        if ident is not None:
            return f"id:{ident}"
        base = (f"{_field(item, TITLE_KEYS)}|{_field(item, TEXT_KEYS)}"
                f"|{_field(item, TIME_KEYS)}")
        return "h:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()

    async def _read_source(name):
        """Reads one possible source. None means unavailable in this build."""
        if name == "__all_notifications__":
            data = globals().get("__all_notifications__")
            if data is None:
                return None
        elif name == "main_api.getAllNotifications":
            api = globals().get("main_api")
            fn = getattr(api, "getAllNotifications", None) if api else None
            if fn is None:
                return None
            data = fn()
        else:
            fn = globals().get(name)
            if not callable(fn):
                return None
            data = fn()
        if inspect.isawaitable(data):
            data = await data
        return _flatten(data)

    # The global is the primary source: it is the very list backing the center,
    # with no layer in between. The others are fallbacks.
    SOURCES = ("__all_notifications__", "main_api.getAllNotifications", "getNotifications")

    async def read_center():
        preferred = STATE.get("center_source")
        order = ([preferred] + [s for s in SOURCES if s != preferred]) if preferred else list(SOURCES)

        problems = []
        for name in order:
            try:
                data = await _read_source(name)
            except Exception as e:
                problems.append(f"{name}: {type(e).__name__}: {e}")
                continue
            if data is None:
                continue
            if STATE.get("center_source") != name:
                STATE["center_source"] = name
                log(f"reading the center through {name}")
            STATE["center_error"] = None
            return data

        STATE["center_error"] = "; ".join(problems) or "no source available"
        return None

    async def watch_center():
        """Polls the center and forwards whatever is new."""
        # The first read only catalogues what is already there: nobody wants the
        # entire history dumped on them when the bridge starts.
        initial = await read_center()
        if initial is None:
            log(f"center unavailable ({STATE['center_error']}); "
                "capture will rely on showToast only.", "ERROR")
            return
        for item in initial:
            STATE["center_ids"].append(_identity(item))
        log(f"center connected: ignoring {len(initial)} existing notifications.")

        first_sample = True
        while True:
            try:
                await asyncio.sleep(max(0.5, float(CFG.get("center_interval") or 2.0)))
                if not CFG.get("capture_center", True) or not CFG.get("enabled"):
                    continue

                current = await read_center()
                if current is None:
                    await asyncio.sleep(10)
                    continue

                known = STATE["center_ids"]
                fresh = [i for i in current if _identity(i) not in known]
                if not fresh:
                    continue

                # created_at decides the order. Nighty's list is chronological,
                # but sorting explicitly protects against that changing.
                fresh.sort(key=_when)

                for item in fresh:
                    STATE["center_ids"].append(_identity(item))

                    if first_sample:
                        # Helps diagnose an unrecognised field layout.
                        fields = (list(item.keys()) if isinstance(item, dict)
                                  else [a for a in dir(item) if not a.startswith("_")][:20])
                        log(f"center format: {fields}")
                        first_sample = False

                    raw = _field(item, TEXT_KEYS)
                    title = _field(item, TITLE_KEYS)
                    if title:
                        text = raw
                    else:
                        title, text = _split_text(raw)

                    if not title and not text:
                        continue
                    if seen_recently(title, text):
                        continue          # already came through showToast

                    event = build_event(
                        text=text,
                        title=title,
                        url=_field(item, URL_KEYS),
                        kind=_field(item, TYPE_KEYS),
                        source="notificationCenter",
                    )

                    channel = _field(item, CHANNEL_KEYS)
                    if isinstance(channel, dict):
                        channel = channel.get("name")
                    if channel:
                        event["context"] = {"channel": str(channel)}

                    STATE["center_total"] += 1
                    publish(event)
            except asyncio.CancelledError:
                return
            except Exception:
                log("center reader error:\n" + traceback.format_exc(), "ERROR")
                await asyncio.sleep(10)

    # ══════════════════════════════════════════════════════════════════════
    # Capture path 3: watched channels  (the Custom Features bridge)
    #
    # Custom Features cannot make HTTP requests - every action it offers is a
    # Discord operation. But it CAN send a message. So: give your feature a
    # "Send Message" action pointing at a private channel, list that channel
    # here, and the message becomes a toast on your PC.
    # ══════════════════════════════════════════════════════════════════════

    def watched_ids():
        return {str(c).strip() for c in (CFG.get("watch_channels") or []) if str(c).strip()}

    def install_listener():
        previous = STATE.get("listener")
        if previous is not None:
            try:
                bot.remove_listener(previous, "on_message")
            except Exception:
                pass
            STATE["listener"] = None

        async def on_watched_message(message):
            try:
                if not CFG.get("enabled"):
                    return
                ids = watched_ids()
                if not ids:
                    return
                channel = getattr(message, "channel", None)
                if str(getattr(channel, "id", "")) not in ids:
                    return

                content = (getattr(message, "clean_content", None)
                           or getattr(message, "content", None) or "")
                if not content:
                    embeds = getattr(message, "embeds", None) or []
                    if embeds:
                        first = embeds[0]
                        content = (getattr(first, "description", None)
                                   or getattr(first, "title", None) or "(embed)")
                if not content:
                    return

                author = getattr(message, "author", None)
                name = (getattr(author, "display_name", None)
                        or getattr(author, "name", None) or "Custom Feature")

                STATE["channel_total"] += 1
                publish(build_event(text=content, title=name,
                                    message=message, source="watchedChannel"))
            except Exception:
                log("watched channel listener error:\n" + traceback.format_exc(), "ERROR")

        try:
            bot.add_listener(on_watched_message, "on_message")
            STATE["listener"] = on_watched_message
            return True
        except Exception as e:
            log(f"could not register the channel listener: {e}", "ERROR")
            return False

    # ══════════════════════════════════════════════════════════════════════
    # HTTP / SSE server
    # ══════════════════════════════════════════════════════════════════════

    def authorised(request):
        expected = CFG.get("token") or ""
        if not expected:
            return False
        given = request.query.get("token") or ""
        if not given:
            header = request.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                given = header[7:].strip()
        return secrets.compare_digest(str(given), str(expected))

    async def handle_health(request):
        return web.json_response({
            "ok": True,
            "service": "nighty-remote-toast",
            "clients": len(STATE["clients"]),
            "seq": STATE["seq"],
        })

    async def handle_notify(request):
        """Lets anything push a toast (curl, another script, a webhook relay)."""
        if not authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        publish(build_event(
            text=body.get("text"),
            title=body.get("title"),
            url=body.get("url"),
            kind=body.get("type"),
            source=body.get("source") or "http",
        ))
        return web.json_response({"ok": True, "seq": STATE["seq"]})

    async def handle_events(request):
        """SSE stream. `?since=N` replays only what the client missed."""
        if not authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",     # stops buffering behind a proxy
        })
        await response.prepare(request)

        q = asyncio.Queue(maxsize=200)
        STATE["clients"].add(q)

        async def write(event):
            data = json.dumps(event, ensure_ascii=False)
            name = event.get("kind") or "toast"
            # Only sequenced events carry an id: config must stay out of the
            # numbering, or it would break the Last-Event-ID resume.
            header = f"id: {event['seq']}\n" if event.get("seq") else ""
            await response.write(
                f"{header}event: {name}\ndata: {data}\n\n".encode("utf-8")
            )

        try:
            # Settings first, so the very first toast is drawn on the right
            # side, with the right duration, without a second round trip.
            check_settings()
            await write(config_event())

            try:
                since = int(request.query.get("since")
                            or request.headers.get("Last-Event-ID") or 0)
            except (TypeError, ValueError):
                since = 0
            if since:
                for event in list(STATE["backlog"]):
                    if event.get("seq", 0) > since:
                        await write(event)

            await response.write(f": connected seq={STATE['seq']}\n\n".encode("utf-8"))

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    await write(event)
                except asyncio.TimeoutError:
                    # Keepalive: holds the connection against proxy/NAT timeouts.
                    await response.write(b": ping\n\n")
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        except Exception as e:
            log(f"SSE client dropped: {type(e).__name__}: {e}", "INFO")
        finally:
            STATE["clients"].discard(q)
        return response

    def broadcast_config():
        """Sends config to everyone without spending a seq or hitting the backlog."""
        event = config_event()
        for q in list(STATE["clients"]):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    async def watch_settings():
        """Detects /settings toastsettings|toastnotifications and propagates."""
        while True:
            try:
                await asyncio.sleep(4)
                if CFG.get("follow_nighty_settings", True) and check_settings():
                    log("toast settings changed; notifying clients.")
                    broadcast_config()
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(10)

    async def stop_server():
        runner = STATE.get("runner")
        STATE["runner"] = None
        STATE["serving"] = None

        for key in ("watcher", "poller"):
            task = STATE.get(key)
            STATE[key] = None
            if task is not None:
                task.cancel()

        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass

    async def start_server():
        await stop_server()
        if not CFG.get("enabled"):
            log("bridge disabled in config; server not started.")
            return

        app = web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_get("/events", handle_events)
        app.router.add_post("/notify", handle_notify)

        runner = web.AppRunner(app)
        await runner.setup()
        host = CFG.get("host") or "0.0.0.0"
        port = int(CFG.get("port") or 8787)
        try:
            site = web.TCPSite(runner, host, port, reuse_address=True)
            await site.start()
        except OSError as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            log(f"could not bind {host}:{port} -> {e}", "ERROR")
            try:
                await runner.cleanup()
            except Exception:
                pass
            return

        STATE["runner"] = runner
        STATE["serving"] = (host, port)
        STATE["error"] = None
        STATE["watcher"] = asyncio.ensure_future(watch_settings())
        if CFG.get("capture_center", True):
            STATE["poller"] = asyncio.ensure_future(watch_center())
        log(f"listening on http://{host}:{port}/events")

    def restart():
        loop = STATE.get("loop") or getattr(bot, "loop", None)
        if loop is not None:
            loop.create_task(start_server())

    # ══════════════════════════════════════════════════════════════════════
    # Boot
    # ══════════════════════════════════════════════════════════════════════

    STATE["loop"] = getattr(bot, "loop", None) or asyncio.get_event_loop()

    if CFG.get("intercept"):
        if install_patch():
            log("showToast intercepted - toasts from every script are forwarded.")
    else:
        remove_patch()

    install_listener()
    restart()

    # ══════════════════════════════════════════════════════════════════════
    # Command
    # ══════════════════════════════════════════════════════════════════════

    @bot.command(name="toastbridge", usage="[test|token|filters|settings|watch|reload]",
                 description="Nighty Remote Toast bridge status and settings.")
    async def cmd_toastbridge(ctx, *, args: str = ""):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        action = (args or "").strip()
        lowered = action.lower()

        if lowered == "test":
            remoteToast(
                text="If you are reading this on your PC, the bridge works.",
                title="Nighty Remote Toast",
                type_="SUCCESS",
            )
            await ctx.send("```\nTest toast sent.\n```")
            return

        if lowered == "token":
            await ctx.send(f"```\ntoken: {CFG.get('token')}\n```")
            return

        filters = CFG.setdefault("filters", dict(DEFAULTS["filters"]))

        if lowered.startswith("block "):
            pattern = action[6:].strip()
            if pattern:
                filters.setdefault("block", []).append(pattern)
                save_config(CFG)
                await ctx.send(f"```\nblocked: {pattern}\n```")
            return

        if lowered.startswith("unblock"):
            target = lowered[7:].strip()
            rules = filters.setdefault("block", [])
            if target == "all":
                rules.clear()
                save_config(CFG)
                await ctx.send("```\nblock list cleared.\n```")
            elif target.isdigit() and 1 <= int(target) <= len(rules):
                removed = rules.pop(int(target) - 1)
                save_config(CFG)
                await ctx.send(f"```\nremoved: {removed}\n```")
            else:
                await ctx.send("```\nusage: <p>toastbridge unblock <number|all>\n```")
            return

        if lowered == "filters":
            lines = ["=== FILTERS ===", "",
                     "Patterns match against 'title | text' (the Notification",
                     "Center format), so you can copy a line from there.",
                     f"regex: {'ON' if filters.get('regex') else 'off'}", "", "BLOCK:"]
            blocked = filters.get("block") or []
            lines += [f"  {i}. {p}" for i, p in enumerate(blocked, 1)] or ["  (empty)"]
            only = filters.get("only_allow") or []
            lines += ["", "ONLY ALLOW:"]
            lines += [f"  {i}. {p}" for i, p in enumerate(only, 1)] or ["  (empty - everything passes)"]
            lines += ["", f"blocked so far: {STATE['blocked']}",
                      "", "<p>toastbridge block <pattern>",
                      "<p>toastbridge unblock <number|all>"]
            await ctx.send("```\n" + "\n".join(lines) + "\n```")
            return

        if lowered.startswith("watch"):
            target = action[5:].strip()
            channels = CFG.setdefault("watch_channels", [])
            if not target:
                lines = ["=== WATCHED CHANNELS ===", ""]
                lines += [f"  {c}" for c in channels] or ["  (none)"]
                lines += ["", f"forwarded from channels: {STATE['channel_total']}",
                          "",
                          "Give a Custom Feature a 'Send Message' action pointing",
                          "at a private channel, then run:",
                          "  <p>toastbridge watch <channel id>"]
                await ctx.send("```\n" + "\n".join(lines) + "\n```")
                return
            if not target.isdigit():
                await ctx.send("```\nusage: <p>toastbridge watch <channel id>\n```")
                return
            if target not in [str(c) for c in channels]:
                channels.append(target)
                save_config(CFG)
                install_listener()
            await ctx.send(f"```\nwatching channel {target}\n```")
            return

        if lowered.startswith("unwatch"):
            target = lowered[7:].strip()
            channels = CFG.setdefault("watch_channels", [])
            if target == "all":
                channels.clear()
            else:
                CFG["watch_channels"] = [c for c in channels if str(c) != target]
            save_config(CFG)
            install_listener()
            await ctx.send("```\nwatch list updated.\n```")
            return

        if lowered == "reload":
            # Lets you edit toastBridge.json by hand without reloading the script.
            before = (CFG.get("host"), CFG.get("port"))
            fresh = load_config()
            CFG.clear()
            CFG.update(fresh)
            check_settings(force=True)
            broadcast_config()
            install_listener()
            if (CFG.get("host"), CFG.get("port")) != before:
                restart()
                await ctx.send("```\nconfig reloaded. host/port changed: server restarted.\n```")
            else:
                await ctx.send("```\nconfig reloaded from disk.\n```")
            return

        if lowered == "settings":
            check_settings(force=True)
            s = STATE.get("settings") or {}
            events = s.get("events") or {}
            on = [k for k, v in events.items() if v]
            off = [k for k, v in events.items() if not v]
            lines = [
                "=== NIGHTY SETTINGS (mirrored) ===",
                f"toast enabled ....... {'YES' if s.get('enabled') else 'no'}",
                f"side ................ {s.get('side') or '(default)'}",
                f"duration_ms ......... {s.get('duration_ms') or '(default)'}",
                f"image_url ........... {s.get('image_url') or '(default)'}",
                f"title ............... {s.get('title') or '-'}",
                "",
                f"events ON ........... {', '.join(on) or '-'}",
                f"events OFF .......... {', '.join(off) or '-'}",
                "",
                "Change them with /settings toastsettings and",
                "/settings toastnotifications; the local client follows",
                "on its own within 4 seconds.",
            ]
            await ctx.send("```\n" + "\n".join(lines) + "\n```")
            return

        serving = STATE.get("serving")
        s = STATE.get("settings") or {}
        lines = [
            "=== NIGHTY REMOTE TOAST ===",
            f"enabled ............. {'YES' if CFG.get('enabled') else 'no'}",
            f"serving on .......... {('%s:%s' % serving) if serving else 'not started'}",
            f"error ............... {STATE.get('error') or '-'}",
            f"connected clients ... {len(STATE['clients'])}",
            f"events sent ......... {STATE['seq']}",
            f"blocked by filter ... {STATE['blocked']}",
            "",
            f"capture showToast ... {'YES' if CFG.get('intercept') else 'no'}",
            f"capture center ...... {'YES' if CFG.get('capture_center') else 'no'}"
            f"  ({STATE['center_total']} captured, via {STATE.get('center_source') or '-'})",
            f"center error ........ {STATE.get('center_error') or '-'}",
            f"watched channels .... {len(watched_ids())}"
            f"  ({STATE['channel_total']} forwarded)",
            "",
            f"mirror on VPS ....... {'YES' if CFG.get('mirror_locally') else 'no'}",
            f"follow /settings .... {'YES' if CFG.get('follow_nighty_settings') else 'no'}"
            f"  (side={s.get('side') or 'default'}, dur={s.get('duration_ms') or 'default'})",
            "",
            "<p>toastbridge token | test | filters | settings | reload",
            "<p>toastbridge block <pattern> | unblock <number|all>",
            "<p>toastbridge watch <channel id> | unwatch <id|all>",
        ]
        await ctx.send("```\n" + "\n".join(lines) + "\n```")

    # ══════════════════════════════════════════════════════════════════════
    # Tab
    # ══════════════════════════════════════════════════════════════════════

    try:
        tab = Tab(name="Toast Bridge", title="Nighty Remote Toast", icon="bell")
        container = tab.create_container(type="rows")

        card = container.create_card(height="auto", width="full", gap=3)
        card.create_ui_element(
            UI.Text,
            content="Forwards this Nighty's notifications to the client running on your PC.",
            size="sm",
        )

        status_text = card.create_ui_element(UI.Text, content="", size="tiny")

        def status_line():
            serving = STATE.get("serving")
            where = ("%s:%s" % serving) if serving else (STATE.get("error") or "stopped")
            center = STATE.get("center_error") or f"{STATE['center_total']} captured"
            return (f"Server: {where}   |   Clients: {len(STATE['clients'])}"
                    f"   |   Events: {STATE['seq']}\nCenter: {center}")

        status_text.content = status_line()

        def refresh_status():
            try:
                status_text.content = status_line()
            except Exception:
                pass

        def on_enabled(checked):
            CFG["enabled"] = bool(checked)
            save_config(CFG)
            restart()
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description=f"Enabled: {bool(checked)}")

        def on_intercept(checked):
            CFG["intercept"] = bool(checked)
            save_config(CFG)
            if checked:
                install_patch()
            else:
                remove_patch()
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description=f"showToast capture: {bool(checked)}")

        def on_center(checked):
            CFG["capture_center"] = bool(checked)
            save_config(CFG)
            restart()
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description=f"Notification Center capture: {bool(checked)}")

        def on_mirror(checked):
            CFG["mirror_locally"] = bool(checked)
            save_config(CFG)
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description=f"Mirror on VPS: {bool(checked)}")

        card.create_ui_element(UI.Toggle, label="Bridge enabled",
                               checked=bool(CFG.get("enabled")), onChange=on_enabled)
        card.create_ui_element(UI.Toggle,
                               label="Capture toasts from other scripts (showDM etc)",
                               checked=bool(CFG.get("intercept")), onChange=on_intercept)
        card.create_ui_element(UI.Toggle,
                               label="Capture the Notification Center (required in web mode)",
                               checked=bool(CFG.get("capture_center", True)), onChange=on_center)
        card.create_ui_element(UI.Toggle, label="Keep showing the toast on this machine",
                               checked=bool(CFG.get("mirror_locally")), onChange=on_mirror)

        # ---- Connection --------------------------------------------------
        net_card = container.create_card(height="auto", width="full", gap=3)
        net_card.create_ui_element(UI.Text, content="Connection", size="base", weight="bold")

        def on_port(value):
            try:
                CFG["port"] = int(str(value).strip())
            except (TypeError, ValueError):
                return
            save_config(CFG)

        net_card.create_ui_element(
            UI.Input, label="Port", value=str(CFG.get("port")),
            placeholder="8787", full_width=True, onInput=on_port,
            description="Do not use 80: that belongs to Nighty's web version. "
                        "Open this port on the VPS firewall.",
        )

        token_field = net_card.create_ui_element(
            UI.Input, label="Token", value=str(CFG.get("token")),
            full_width=True, readonly=True,
            description="Paste this token into the local client's config.",
        )

        def apply_changes():
            save_config(CFG)
            restart()
            refresh_status()
            tab.toast(type="SUCCESS", title="Nighty Remote Toast",
                      description="Server restarted.")

        def new_token():
            CFG["token"] = secrets.token_urlsafe(24)
            save_config(CFG)
            try:
                token_field.value = CFG["token"]
            except Exception:
                pass
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description="New token generated. Update the local client.")

        def send_test():
            remoteToast(text="If you are reading this on your PC, the bridge works.",
                        title="Nighty Remote Toast", type_="SUCCESS")
            refresh_status()
            tab.toast(type="INFO", title="Nighty Remote Toast",
                      description=f"Sent to {len(STATE['clients'])} client(s).")

        row = net_card.create_group(type="columns", gap=2)
        row.create_ui_element(UI.Button, label="Apply and restart",
                              color="primary", onClick=apply_changes)
        row.create_ui_element(UI.Button, label="Generate new token",
                              color="danger", variant="bordered", onClick=new_token)
        row.create_ui_element(UI.Button, label="Send test",
                              color="success", variant="bordered", onClick=send_test)

        # ---- Filters ------------------------------------------------------
        filter_card = container.create_card(height="auto", width="full", gap=3)
        filter_card.create_ui_element(UI.Text, content="Filters", size="base", weight="bold")
        filter_card.create_ui_element(
            UI.Text, size="tiny", color="#B5BAC1",
            content=("Block by text fragment. Patterns match against "
                     "'title | text', which is how the Notification Center shows "
                     "them.\nExample: 'Daily backup | Saved vanity invite' blocks "
                     "only the vanity lines and keeps 'Full backup complete'."),
        )

        # Always re-read from CFG: <p>toastbridge reload swaps the whole dict,
        # so holding a reference here would edit a dead object.
        def filter_cfg():
            return CFG.setdefault("filters", dict(DEFAULTS["filters"]))

        filter_list = filter_card.create_ui_element(UI.Text, content="", size="tiny")

        def filter_summary():
            rules = filter_cfg().get("block") or []
            if not rules:
                return "Blocking: nothing (everything passes)"
            return "Blocking:\n" + "\n".join(f"  {i}. {p}" for i, p in enumerate(rules, 1))

        filter_list.content = filter_summary()

        draft = {"pattern": ""}

        def on_pattern(value):
            draft["pattern"] = str(value or "").strip()

        filter_card.create_ui_element(
            UI.Input, label="New block rule", full_width=True,
            placeholder="Daily backup | Saved vanity invite",
            show_clear_button=True, onInput=on_pattern,
        )

        def add_filter():
            pattern = draft.get("pattern")
            if not pattern:
                tab.toast(type="ERROR", title="Filters",
                          description="Type a fragment before adding.")
                return
            filter_cfg().setdefault("block", []).append(pattern)
            save_config(CFG)
            try:
                filter_list.content = filter_summary()
            except Exception:
                pass
            tab.toast(type="SUCCESS", title="Filters", description=f"Blocked: {pattern}")

        def clear_filters():
            (filter_cfg().get("block") or []).clear()
            save_config(CFG)
            try:
                filter_list.content = filter_summary()
            except Exception:
                pass
            tab.toast(type="INFO", title="Filters", description="List cleared.")

        def on_regex(checked):
            filter_cfg()["regex"] = bool(checked)
            save_config(CFG)

        filter_card.create_ui_element(UI.Toggle, label="Treat patterns as regex",
                                      checked=bool(filter_cfg().get("regex")),
                                      onChange=on_regex)

        filter_row = filter_card.create_group(type="columns", gap=2)
        filter_row.create_ui_element(UI.Button, label="Add block rule",
                                     color="primary", onClick=add_filter)
        filter_row.create_ui_element(UI.Button, label="Clear all",
                                     color="danger", variant="bordered",
                                     onClick=clear_filters)

        # ---- Nighty settings mirror ---------------------------------------
        sync_card = container.create_card(height="auto", width="full", gap=3)
        sync_card.create_ui_element(UI.Text, content="Nighty settings",
                                    size="base", weight="bold")
        sync_card.create_ui_element(
            UI.Text, size="tiny", color="#B5BAC1",
            content=("With this on, the local client obeys /settings toastsettings "
                     "(side, duration_ms, image_url). Change the command and your "
                     "PC follows within 4 seconds."),
        )
        mirror_text = sync_card.create_ui_element(UI.Text, content="", size="tiny")

        def mirror_summary():
            s = STATE.get("settings") or {}
            events = s.get("events") or {}
            off = [k for k, v in events.items() if not v]
            return (f"side={s.get('side') or 'default'}   "
                    f"duration_ms={s.get('duration_ms') or 'default'}   "
                    f"image_url={'yes' if s.get('image_url') else 'no'}\n"
                    f"events disabled in Nighty: {', '.join(off) or '-'}")

        mirror_text.content = mirror_summary()

        def on_follow(checked):
            CFG["follow_nighty_settings"] = bool(checked)
            save_config(CFG)
            broadcast_config()
            tab.toast(type="INFO", title="Settings",
                      description=f"Follow /settings: {bool(checked)}")

        sync_card.create_ui_element(
            UI.Toggle, label="Follow Nighty's /settings toast values",
            checked=bool(CFG.get("follow_nighty_settings", True)), onChange=on_follow)

        def reload_settings():
            check_settings(force=True)
            broadcast_config()
            try:
                mirror_text.content = mirror_summary()
            except Exception:
                pass
            tab.toast(type="SUCCESS", title="Settings", description="Re-read and sent.")

        sync_card.create_ui_element(UI.Button, label="Re-read now",
                                    color="primary", variant="bordered",
                                    onClick=reload_settings)

        # ---- Custom Features bridge ---------------------------------------
        cf_card = container.create_card(height="auto", width="full", gap=3)
        cf_card.create_ui_element(UI.Text, content="Custom Features bridge",
                                  size="base", weight="bold")
        cf_card.create_ui_element(
            UI.Text, size="tiny", color="#B5BAC1",
            content=("Custom Features cannot make HTTP requests - every action it "
                     "offers is a Discord operation. But it can send a message.\n"
                     "Give your feature a 'Send Message' action pointing at a "
                     "private channel, add that channel ID here, and the message "
                     "becomes a toast on your PC."),
        )

        watch_list = cf_card.create_ui_element(UI.Text, content="", size="tiny")

        def watch_summary():
            ids = sorted(watched_ids())
            if not ids:
                return "Watching: no channel"
            return "Watching:\n" + "\n".join(f"  {c}" for c in ids)

        watch_list.content = watch_summary()

        watch_draft = {"id": ""}

        def on_channel(value):
            watch_draft["id"] = str(value or "").strip()

        cf_card.create_ui_element(
            UI.Input, label="Channel ID", full_width=True,
            placeholder="1319137168412901417",
            show_clear_button=True, onInput=on_channel,
        )

        def add_channel():
            cid = watch_draft.get("id")
            if not cid.isdigit():
                tab.toast(type="ERROR", title="Custom Features",
                          description="A channel ID is digits only.")
                return
            channels = CFG.setdefault("watch_channels", [])
            if cid not in [str(c) for c in channels]:
                channels.append(cid)
                save_config(CFG)
                install_listener()
            try:
                watch_list.content = watch_summary()
            except Exception:
                pass
            tab.toast(type="SUCCESS", title="Custom Features",
                      description=f"Watching {cid}")

        def clear_channels():
            CFG["watch_channels"] = []
            save_config(CFG)
            install_listener()
            try:
                watch_list.content = watch_summary()
            except Exception:
                pass
            tab.toast(type="INFO", title="Custom Features", description="List cleared.")

        cf_row = cf_card.create_group(type="columns", gap=2)
        cf_row.create_ui_element(UI.Button, label="Watch channel",
                                 color="primary", onClick=add_channel)
        cf_row.create_ui_element(UI.Button, label="Clear all",
                                 color="danger", variant="bordered",
                                 onClick=clear_channels)

        tab.render()
    except Exception:
        # A broken tab must not stop the bridge from working.
        log("failed to build the tab:\n" + traceback.format_exc(), "ERROR")


ToastBridge()
