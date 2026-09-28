#!/usr/bin/env python3
"""Claude Terminal: a ChatGPT-style home for Claude Code.

Every conversation on disk is listed in the sidebar. Clicking one shows it as
chat bubbles; replying (or pressing Terminal) resumes it with the real Claude
Code running in an embedded terminal, in the folder it was started from.
"""
import copy
import html
import itertools
import json
import os
import shlex
import shutil
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("WebKit2", "4.1")
gi.require_version("Handy", "1")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Handy, Pango, Vte, WebKit2  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import render  # noqa: E402
import sessions  # noqa: E402
from sessions import tilde  # noqa: E402

APP_ID = "io.github.lestorrr.ClaudeTerminal"
APP_NAME = "Claude Terminal"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "claude-terminal" / "config.json"
SIDEBAR_WIDTH = 272

DEFAULTS = {
    "name": "",
    "font": "DejaVu Sans Mono 11",
    "font_scale": 1.0,
    "model": "",
    "mode": "",
    "show_vscode": True,
    "show_sdk": False,
    "pins": [],
    "titles": {},
    "last_folder": str(Path.home()),
    "window": [1320, 840],
    "sidebar": True,
}

MODELS = [("", "Default model"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]
MODES = [("", "Default mode"), ("auto", "Auto"), ("acceptEdits", "Accept edits"),
         ("plan", "Plan first"), ("manual", "Ask every time")]

# Set by a parent Claude Code session; a child claude must not inherit them.
HARNESS_ENV = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
}

TERM_FG, TERM_BG, ACCENT = "#ececec", "#212121", "#d97757"
TERM_PALETTE = [
    "#2e2e2e", "#f28b82", "#81c995", "#fdd663", "#8ab4f8", "#c58af9", "#78d9ec", "#d6d6d6",
    "#5f5f5f", "#f6aea9", "#a8dab5", "#fde293", "#aecbfa", "#d7aefb", "#a1e4f2", "#ffffff",
]
URL_REGEX = r"https?://[\w\-.~:/?#\[\]@!$&'()*+,;=%]+[\w/#=&]"
PCRE2_MULTILINE = 0x00000400


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

class Config(dict):
    def __init__(self):
        super().__init__(copy.deepcopy(DEFAULTS))
        try:
            self.update(json.loads(CONFIG_FILE.read_text()))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self, indent=2))
            os.replace(tmp, CONFIG_FILE)
        except OSError as err:
            print(f"claude-terminal: could not save config: {err}", file=sys.stderr)


def find_claude():
    path = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".local" / "bin")
    return shutil.which("claude", path=path)


def child_env():
    """Extra variables for Claude. VTE merges these into this process's own
    environment, so anything to *remove* is dropped in main() instead."""
    env = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
    local_bin = str(Path.home() / ".local" / "bin")
    path = os.environ.get("PATH", "")
    if local_bin not in path.split(os.pathsep):
        env["PATH"] = local_bin + os.pathsep + path
    return [f"{k}={v}" for k, v in env.items()]


def prompt_args(prompt):
    if not prompt:
        return []
    return ["--", prompt] if prompt.startswith("-") else [prompt]


def rgba(spec):
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def add_class(widget, *names):
    ctx = widget.get_style_context()
    for name in names:
        ctx.add_class(name)
    return widget


def icon_button(icon, tooltip, callback):
    button = Gtk.Button(image=Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON),
                        tooltip_text=tooltip, relief=Gtk.ReliefStyle.NONE)
    button.connect("clicked", lambda *_: callback())
    return button


def label(text="", *classes, xalign=0.0, **kw):
    widget = Gtk.Label(label=text, xalign=xalign, **kw)
    return add_class(widget, *classes)


def group_for(ts, now):
    day = datetime.fromtimestamp(ts).date()
    delta = (now.date() - day).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return "Previous 7 days"
    if delta < 30:
        return "Previous 30 days"
    return day.strftime("%B") if day.year == now.year else day.strftime("%B %Y")


def short_time(ts, now):
    dt = datetime.fromtimestamp(ts)
    delta = (now.date() - dt.date()).days
    if delta <= 0:
        return dt.strftime("%-I:%M %p")
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return dt.strftime("%A")
    return dt.strftime("%b %-d") if dt.year == now.year else dt.strftime("%b %-d, %Y")


def folder_name(folder):
    """Last path part for the sidebar: '~/Desktop/DEV/app' -> 'app'."""
    return folder if folder in ("", "~", "/") else os.path.basename(folder.rstrip("/"))


def where_label(info):
    return {"claude-vscode": "VS Code", "cli": "another terminal"}.get(
        info.get("entrypoint"), "another Claude window")


def gradient_markup(text, stops=("#e8956b", "#d9657a", "#a57fe8")):
    """Per-character colour ramp, a stand-in for CSS gradient text."""
    def mix(a, b, t):
        a, b = int(a[1:], 16), int(b[1:], 16)
        ch = [round(((a >> s) & 255) * (1 - t) + ((b >> s) & 255) * t) for s in (16, 8, 0)]
        return "#%02x%02x%02x" % tuple(ch)
    out, n = [], max(len(text) - 1, 1)
    for i, char in enumerate(text):
        pos = i / n * (len(stops) - 1)
        seg = min(int(pos), len(stops) - 2)
        color = mix(stops[seg], stops[seg + 1], pos - seg)
        out.append(f'<span foreground="{color}">{GLib.markup_escape_text(char)}</span>')
    return "".join(out)


@dataclass
class Entry:
    sid: str
    title: str
    folder: str
    cwd: str
    mtime: float
    pinned: bool
    status: str       # "" | idle | busy | elsewhere
    status_tip: str
    search: str
    entrypoint: str
    group: str = ""
    tab: object = None  # for chats that exist only as a running tab so far


# --------------------------------------------------------------------------
# composer: the rounded "Message Claude" box
# --------------------------------------------------------------------------

class Composer(Gtk.Box):
    def __init__(self, placeholder):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_class(self, "composer")
        self.on_send = None
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                 top_margin=4, bottom_margin=4)
        self.buffer = self.view.get_buffer()
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      propagate_natural_height=True, max_content_height=240,
                                      min_content_height=28)
        scroller.add(self.view)
        self.placeholder = label(placeholder, "placeholder", halign=Gtk.Align.START,
                                 valign=Gtk.Align.START, margin_top=4)
        overlay = Gtk.Overlay()
        overlay.add(scroller)
        overlay.add_overlay(self.placeholder)
        overlay.set_overlay_pass_through(self.placeholder, True)
        self.pack_start(overlay, True, True, 0)

        self.tools = Gtk.Box(spacing=6)
        attach = Gtk.Button(image=Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON),
                            tooltip_text="Add files (or drag them here)")
        add_class(attach, "chip")
        attach.connect("clicked", self._attach)
        self.tools.pack_start(attach, False, False, 0)
        self.send_button = Gtk.Button(image=Gtk.Image.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON),
                                      tooltip_text="Send (Enter) · new line (Shift+Enter)",
                                      valign=Gtk.Align.END)
        add_class(self.send_button, "send")
        self.send_button.connect("clicked", lambda *_: self.submit())
        row = Gtk.Box(spacing=6)
        row.pack_start(self.tools, True, True, 0)
        row.pack_end(self.send_button, False, False, 0)
        self.pack_start(row, False, False, 0)

        self.buffer.connect("changed", self._changed)
        self.view.connect("key-press-event", self._key)
        targets = Gtk.TargetList.new([])
        targets.add_uri_targets(1)  # before text, so file drops arrive as paths
        targets.add_text_targets(0)
        self.view.drag_dest_set_target_list(targets)
        self.view.connect("drag-data-received", self._drop)
        self._changed()

    def text(self):
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, False)

    def set_text(self, text):
        self.buffer.set_text(text)
        self.focus()

    def focus(self):
        self.view.grab_focus()
        self.buffer.place_cursor(self.buffer.get_end_iter())

    def submit(self):
        text = self.text().strip()
        if text and self.on_send and self.on_send(text):
            self.buffer.set_text("")

    def insert_paths(self, paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        before = self.text()
        joined = " ".join(shlex.quote(p) for p in paths)
        if before and not before.endswith((" ", "\n")):
            joined = " " + joined
        self.buffer.insert_at_cursor(joined + " ")
        self.view.grab_focus()

    def _changed(self, *_):
        text = self.text()
        self.placeholder.set_visible(not text)
        self.send_button.set_sensitive(bool(text.strip()))

    def _key(self, _view, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK):
                return False
            self.submit()
            return True
        return False

    def _drop(self, widget, _ctx, _x, _y, data, _info, _time):
        uris = data.get_uris()
        if uris:
            self.insert_paths(Gio.File.new_for_uri(u).get_path() for u in uris)
            GObject.signal_stop_emission_by_name(widget, "drag-data-received")

    def _attach(self, _button):
        dialog = Gtk.FileChooserNative.new("Add files", self.get_toplevel(),
                                           Gtk.FileChooserAction.OPEN, "_Add", "_Cancel")
        dialog.set_select_multiple(True)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.insert_paths(f.get_path() for f in dialog.get_files())
        dialog.destroy()


# --------------------------------------------------------------------------
# a running (or finished) Claude Code process in an embedded terminal
# --------------------------------------------------------------------------

class Tab:
    _ids = itertools.count(1)

    def __init__(self, win, cwd, sid):
        self.win, self.cwd, self.sid = win, cwd, sid
        self.name = f"term-{next(Tab._ids)}"
        self.pid = None
        self.alive = False
        self.stopping = False
        self.status = "idle"
        self.started = time.time()

        term = self.term = Vte.Terminal(margin_start=18, margin_end=6, margin_top=10, margin_bottom=6)
        term.set_font(Pango.FontDescription.from_string(win.config["font"]))
        term.set_font_scale(win.config["font_scale"])
        term.set_colors(rgba(TERM_FG), rgba(TERM_BG), [rgba(c) for c in TERM_PALETTE])
        term.set_color_cursor(rgba(ACCENT))
        term.set_color_cursor_foreground(rgba(TERM_BG))
        term.set_bold_is_bright(False)
        term.set_scrollback_lines(20000)
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        term.set_mouse_autohide(True)
        term.set_audible_bell(False)
        term.set_allow_hyperlink(True)
        term.set_scroll_on_keystroke(True)
        try:
            regex = Vte.Regex.new_for_match(URL_REGEX, -1, PCRE2_MULTILINE)
            tag = term.match_add_regex(regex, 0)
            term.match_set_cursor_name(tag, "pointer")
        except GLib.Error:
            pass
        term.connect("child-exited", self._exited)
        term.connect("button-press-event", self._press)
        term.connect("scroll-event", self._scroll)
        term.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        term.drag_dest_add_uri_targets()
        term.drag_dest_add_text_targets()
        term.connect("drag-data-received", self._drop)

        scrollbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=term.get_vadjustment())
        body = Gtk.Box()
        body.pack_start(term, True, True, 0)
        body.pack_start(scrollbar, False, False, 0)

        bar = add_class(Gtk.Box(spacing=10), "ended")
        self.ended_label = label("Claude has exited.", valign=Gtk.Align.CENTER)
        resume = Gtk.Button(label="Resume")
        resume.connect("clicked", lambda *_: win.resume(self.sid, tab=self))
        close = add_class(Gtk.Button(label="Close"), "secondary")
        close.connect("clicked", lambda *_: win.close_tab(self))
        bar.pack_start(self.ended_label, True, True, 0)
        bar.pack_start(resume, False, False, 0)
        bar.pack_start(close, False, False, 0)
        self.ended = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                  valign=Gtk.Align.END, halign=Gtk.Align.CENTER)
        self.ended.add(bar)

        self.widget = Gtk.Overlay()
        self.widget.add(body)
        self.widget.add_overlay(self.ended)
        self.widget.show_all()

    def spawn(self, argv):
        self.ended.set_reveal_child(False)
        self.term.reset(True, True)
        self.alive = True
        self.stopping = False
        self.status = "idle"
        self.term.spawn_async(Vte.PtyFlags.DEFAULT, self.cwd, argv, child_env(),
                              GLib.SpawnFlags.DEFAULT, None, None, -1, None, self._spawned, None)

    def _spawned(self, _term, pid, error, _data):
        if error is not None or pid < 0:
            self.alive = False
            msg = error.message if error is not None else "unknown error"
            self.term.feed(f"\r\n  Could not start Claude Code: {msg}\r\n".encode())
            self.ended_label.set_text("Claude could not start.")
            self.ended.set_reveal_child(True)
        else:
            self.pid = pid
        self.win.refresh_sidebar()

    def _exited(self, _term, status):
        self.alive = False
        self.pid = None
        code = os.waitstatus_to_exitcode(status) if status > 0 else status
        if self.stopping or code == 0:
            saved = self.sid in self.win.convs_by_sid
            self.ended_label.set_text("Claude stopped. The chat is saved." if saved else "Claude stopped.")
        else:
            self.ended_label.set_text(f"Claude exited unexpectedly (code {code}).")
        self.ended.set_reveal_child(True)
        self.win.refresh_sidebar()
        self.win.sync_header()

    def type_text(self, text):
        """Type a message into Claude's prompt and press Enter."""
        text = text.replace("\r\n", "\n")
        payload = f"\x1b[200~{text}\x1b[201~" if "\n" in text else text
        self.term.feed_child(payload.encode())
        GLib.timeout_add(150, lambda: self.term.feed_child(b"\r") and False)

    def stop(self):
        if self.alive and self.pid:
            self.stopping = True
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass

    def _press(self, term, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 3:
            self.win.terminal_menu(self, event)
            return True
        if event.button == 1 and event.state & Gdk.ModifierType.CONTROL_MASK:
            uri = term.hyperlink_check_event(event) or term.match_check_event(event)[0]
            if uri:
                self.win.open_uri(uri)
                return True
        return False

    def _scroll(self, _term, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            dy = event.get_scroll_deltas()[2]
        else:
            dy = -1 if event.direction == Gdk.ScrollDirection.UP else 1
        if dy:
            self.win.zoom(-0.1 if dy > 0 else 0.1)
        return True

    def _drop(self, widget, _ctx, _x, _y, data, _info, _time):
        uris = data.get_uris()
        if uris:
            paths = [Gio.File.new_for_uri(u).get_path() for u in uris]
            text = " ".join(shlex.quote(p) for p in paths if p)
        else:
            text = data.get_text() or ""
        if text:
            self.term.feed_child((text + " ").encode())
        GObject.signal_stop_emission_by_name(widget, "drag-data-received")


# --------------------------------------------------------------------------
# sidebar row
# --------------------------------------------------------------------------

class ConvRow(Gtk.ListBoxRow):
    def __init__(self, entry):
        super().__init__()
        box = Gtk.Box(spacing=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        # max_width_chars caps the natural width so a long title can't widen the sidebar.
        self.title = label("", "conv-title", ellipsize=Pango.EllipsizeMode.END, single_line_mode=True,
                           max_width_chars=22)
        self.sub = label("", "conv-sub", ellipsize=Pango.EllipsizeMode.END, single_line_mode=True,
                         max_width_chars=28)
        text.pack_start(self.title, False, False, 0)
        text.pack_start(self.sub, False, False, 0)
        self.pin = add_class(Gtk.Image.new_from_icon_name("view-pin-symbolic", Gtk.IconSize.MENU), "pin")
        self.dot = label("●", "dot", valign=Gtk.Align.CENTER)
        box.pack_start(text, True, True, 0)
        box.pack_start(self.pin, False, False, 0)
        box.pack_start(self.dot, False, False, 0)
        self.add(box)
        self.show_all()
        self.update(entry)

    def update(self, entry):
        self.entry = entry
        self.title.set_text(entry.title)
        when = short_time(entry.mtime, datetime.now())
        folder = folder_name(entry.folder)
        self.sub.set_text(f"{folder} · {when}" if folder else when)
        self.pin.set_visible(entry.pinned)
        ctx = self.dot.get_style_context()
        for name in ("idle", "busy", "elsewhere"):
            ctx.remove_class(name)
        self.dot.set_visible(bool(entry.status))
        if entry.status:
            ctx.add_class(entry.status)
            self.dot.set_text("○" if entry.status == "elsewhere" else "●")
        tip = entry.title
        if entry.folder:
            tip += f"\n{entry.folder}"
        if entry.status_tip:
            tip += f"\n{entry.status_tip}"
        self.set_tooltip_text(tip)


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        add_class(self, "ct")
        self.app = app
        self.config = Config()
        self.claude = find_claude()
        self.index = sessions.Index()
        self.convs, self.convs_by_sid = [], {}
        self.live = {}
        self.tabs = []
        self.rows, self._order = {}, None
        self.loaded = self.scanning = False
        self.view, self.current_sid, self.current_tab = "home", None, None
        self.last_view = {}
        self.home_folder = self.config["last_folder"]
        self.chat_gen, self.chat_shown = 0, render.PAGE_SIZE
        self.web_ready, self.pending_js, self.last_thread = False, None, ""
        self.monitor = self.reload_timer = None
        self._syncing = self._selecting = False

        w, h = self.config["window"]
        self.set_default_size(w, h)
        try:
            self.set_icon_from_file(str(APP_DIR / "icon.svg"))
        except GLib.Error:
            self.set_icon_name("utilities-terminal")

        self._build_header()
        self.sidebar = self._build_sidebar()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        add_class(self.stack, "main")
        self.stack.add_named(self._build_home(), "home")
        self.stack.add_named(self._build_chat(), "chat")
        body = Gtk.Box()
        body.pack_start(self.sidebar, False, False, 0)
        body.pack_start(self.stack, True, True, 0)
        self.add(body)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_close)
        self.show_all()
        self._set_sidebar(self.config["sidebar"], animate=False)
        self.show_home()
        self._scan()
        GLib.timeout_add(1500, self._tick)
        if not self.claude:
            GLib.idle_add(self.error, "Claude Code was not found",
                          "Install it, or make sure the claude command is on your PATH "
                          "(usually ~/.local/bin/claude).")

    # ---------------------------------------------------------------- layout

    def _build_header(self):
        hb = self.header = Gtk.HeaderBar(show_close_button=True)
        hb.set_custom_title(Gtk.Box())
        self.header_left = Gtk.Box(spacing=2)
        self.header_left.pack_start(icon_button("sidebar-show-symbolic", "Toggle sidebar (Ctrl+Shift+S)",
                                                self.toggle_sidebar), False, False, 0)
        self.header_left.pack_start(icon_button("document-edit-symbolic", "New chat (Ctrl+Shift+N)",
                                                self.show_home), False, False, 0)
        hb.pack_start(self.header_left)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, margin_start=10)
        self.title_label = label("", "title", ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
        self.subtitle_label = label("", "subtitle", ellipsize=Pango.EllipsizeMode.END, max_width_chars=80)
        titles.pack_start(self.title_label, False, False, 0)
        titles.pack_start(self.subtitle_label, False, False, 0)
        hb.pack_start(titles)

        self.menu_button = Gtk.MenuButton(image=Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON),
                                          tooltip_text="Chat options")
        self.menu_popover = Gtk.Popover()
        self.menu_button.set_popover(self.menu_popover)
        self.menu_button.connect("toggled", self._fill_header_menu)
        hb.pack_end(self.menu_button)

        self.switch = add_class(Gtk.Box(), "viewswitch")
        self.chat_toggle = Gtk.RadioButton.new_with_label_from_widget(None, "Chat")
        self.term_toggle = Gtk.RadioButton.new_with_label_from_widget(self.chat_toggle, "Terminal")
        for button, view in ((self.chat_toggle, "chat"), (self.term_toggle, "term")):
            button.set_mode(False)
            button.connect("toggled", self._on_switch, view)
            self.switch.pack_start(button, False, False, 0)
        self.chat_toggle.set_tooltip_text("Read this chat (Ctrl+Shift+T)")
        self.term_toggle.set_tooltip_text("Continue in Claude Code (Ctrl+Shift+T)")
        hb.pack_end(self.switch)
        self.set_titlebar(hb)

    def _build_sidebar(self):
        side = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, width_request=SIDEBAR_WIDTH),
                         "sidebar")
        new = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE, margin_start=8, margin_end=8, margin_top=4),
                        "newchat")
        inner = Gtk.Box(spacing=10)
        inner.pack_start(Gtk.Image.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        inner.pack_start(label("New chat"), True, True, 0)
        inner.pack_start(label("Ctrl+Shift+N", "hint"), False, False, 0)
        new.add(inner)
        new.connect("clicked", lambda *_: self.show_home())
        side.pack_start(new, False, False, 0)

        self.search = add_class(Gtk.SearchEntry(placeholder_text="Search chats", margin_start=10, margin_end=10),
                                "search")
        self.search.connect("search-changed", lambda *_: self._refilter())
        side.pack_start(self.search, False, False, 0)
        self.folder_filter = Gtk.ComboBoxText(margin_start=10, margin_end=10)
        cell = self.folder_filter.get_cells()[0]
        cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        cell.set_property("max-width-chars", 26)
        self.folder_filter.append("", "All folders")
        self.folder_filter.set_active_id("")
        self.folder_filter.connect("changed", lambda *_: self._refilter())
        side.pack_start(self.folder_filter, False, False, 0)

        self.listbox = add_class(Gtk.ListBox(activate_on_single_click=True), "convlist")
        self.listbox.set_header_func(self._header_func, None)
        self.listbox.set_filter_func(self._filter_func, None)
        self.list_placeholder = label("Loading chats…", "loading", xalign=0.5, wrap=True)
        self.list_placeholder.show()
        self.listbox.set_placeholder(self.list_placeholder)
        self.listbox.connect("row-activated", lambda _lb, row: self.open_entry(row.entry))
        self.listbox.connect("button-press-event", self._list_press)
        self.list_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_scroller.add(self.listbox)
        side.pack_start(self.list_scroller, True, True, 0)

        foot = add_class(Gtk.Box(spacing=4, margin=8, margin_top=0), "sidebar-foot")
        settings = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        add_class(settings, "flat")
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        row.pack_start(label("Settings"), False, False, 0)
        settings.add(row)
        settings.connect("clicked", lambda *_: self.settings_dialog())
        foot.pack_start(settings, False, False, 0)
        side.pack_start(foot, False, False, 0)

        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT, transition_duration=160)
        revealer.add(side)
        return revealer

    def _build_home(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26, valign=Gtk.Align.CENTER,
                      margin_top=40, margin_bottom=40, margin_start=24, margin_end=24)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.greeting = label("", "greeting")
        head.pack_start(self.greeting, False, False, 0)
        head.pack_start(label("What should we work on?", "greeting-sub", wrap=True), False, False, 0)
        box.pack_start(head, False, False, 0)

        composer = self.home_composer = Composer("Ask Claude to build, fix, or explain something…")
        composer.on_send = self._send_new
        self.folder_button = add_class(Gtk.MenuButton(tooltip_text="Folder Claude will work in"), "chip-menu")
        fbox = Gtk.Box(spacing=6)
        fbox.pack_start(Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.MENU), False, False, 0)
        self.folder_label = label("", ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=28)
        fbox.pack_start(self.folder_label, False, False, 0)
        self.folder_button.add(fbox)
        self.folder_popover = Gtk.Popover()
        self.folder_button.set_popover(self.folder_popover)
        self.folder_button.connect("toggled", self._fill_folder_menu)
        composer.tools.pack_start(self.folder_button, False, False, 0)
        self.model_combo = self._combo(MODELS, self.config["model"])
        self.mode_combo = self._combo(MODES, self.config["mode"])
        composer.tools.pack_start(self.model_combo, False, False, 0)
        composer.tools.pack_start(self.mode_combo, False, False, 0)
        box.pack_start(composer, False, False, 0)

        blank = add_class(Gtk.Button(label="or open Claude Code without a prompt", relief=Gtk.ReliefStyle.NONE,
                                     halign=Gtk.Align.CENTER), "flat", "caption")
        blank.connect("clicked", lambda *_: self._send_new(""))
        box.pack_start(blank, False, False, 0)

        self.cards = Gtk.Box(homogeneous=True, spacing=10)
        box.pack_start(self.cards, False, False, 0)

        clamp = Handy.Clamp(maximum_size=760, tightening_threshold=600)
        clamp.add(box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(clamp)
        self._update_folder_label()
        self._refresh_home()
        return scroller

    def _combo(self, options, active):
        combo = add_class(Gtk.ComboBoxText(), "pill")
        for key, text in options:
            combo.append(key, text)
        combo.set_active_id(active if any(k == active for k, _ in options) else "")
        return combo

    def _build_chat(self):
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("ct")
        manager.connect("script-message-received::ct", self._on_web_message)
        web = self.web = WebKit2.WebView.new_with_user_content_manager(manager)
        web.set_background_color(rgba(TERM_BG))
        settings = web.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_property("enable-back-forward-navigation-gestures", False)
        web.connect("decide-policy", self._on_policy)
        web.connect("context-menu", self._on_web_menu)
        css = (APP_DIR / "chat.css").read_text()
        web.load_html(render.shell_html(css), None)

        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar = add_class(Gtk.Box(spacing=10), "banner")
        self.banner_label = label("", wrap=True, valign=Gtk.Align.CENTER)
        fork = add_class(Gtk.Button(label="Open a copy"), "secondary")
        fork.connect("clicked", lambda *_: self.resume(self.current_sid, fork=True))
        bar.pack_start(self.banner_label, True, True, 0)
        bar.pack_start(fork, False, False, 0)
        self.banner.add(bar)

        composer = self.chat_composer = Composer("Message Claude")
        composer.on_send = self._send_chat
        self.chat_caption = label("", "caption", xalign=0.5, wrap=True)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_start=16, margin_end=16, margin_bottom=10, margin_top=6)
        bottom.pack_start(self.banner, False, False, 0)
        bottom.pack_start(composer, False, False, 0)
        bottom.pack_start(self.chat_caption, False, False, 0)
        clamp = Handy.Clamp(maximum_size=780, tightening_threshold=600)
        clamp.add(bottom)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(web, True, True, 0)
        page.pack_start(clamp, False, False, 0)
        return page

    # ------------------------------------------------------------ navigation

    def show_home(self):
        self.view, self.current_sid, self.current_tab = "home", None, None
        self._watch(None)
        self.stack.set_visible_child_name("home")
        self._select_row(None)
        self._update_greeting()
        self._refresh_home()
        self.sync_header()
        self.home_composer.focus()

    def open_entry(self, entry):
        if entry.tab is not None:
            self.show_tab(entry.tab)
            return
        tab = self.tab_for(entry.sid)
        view = self.last_view.get(entry.sid) or ("term" if tab and tab.alive else "chat")
        if view == "term" and tab:
            self.show_tab(tab)
        else:
            self.show_chat(entry.sid)

    def show_chat(self, sid):
        if sid is None:
            return
        changed = sid != self.current_sid or self.view != "chat"
        self.view, self.current_sid = "chat", sid
        self.current_tab = self.tab_for(sid)
        self.last_view[sid] = "chat"
        self.stack.set_visible_child_name("chat")
        self._select_row(sid)
        if changed:
            self.chat_shown = render.PAGE_SIZE
            self._set_thread("", "bottom")
            self._load_chat("bottom")
        s = self.convs_by_sid.get(sid)
        self._watch(s.path if s else None)
        self.sync_header()
        self.chat_composer.focus()

    def show_tab(self, tab):
        self.view, self.current_tab, self.current_sid = "term", tab, tab.sid
        if tab.sid:
            self.last_view[tab.sid] = "term"
        self._watch(None)
        self.stack.set_visible_child_name(tab.name)
        self._select_row(tab.sid or f"tab:{tab.name}")
        self.sync_header()
        tab.term.grab_focus()

    def tab_for(self, sid):
        if not sid:
            return None
        for tab in self.tabs:
            if tab.sid == sid and tab.alive:
                return tab
        for tab in self.tabs:
            if tab.sid == sid:
                return tab
        return None

    def toggle_view(self):
        if self.view == "chat" and self.current_sid:
            self.resume(self.current_sid)
        elif self.view == "term" and self.current_sid:
            self.show_chat(self.current_sid)

    # --------------------------------------------------------------- actions

    def _send_new(self, text):
        folder = self.home_folder
        if not os.path.isdir(folder):
            self.error("That folder doesn't exist anymore", f"{folder}\n\nPick another folder for this chat.")
            return False
        if not self.claude:
            self.error("Claude Code was not found", "The claude command is not on your PATH.")
            return False
        model, mode = self.model_combo.get_active_id() or "", self.mode_combo.get_active_id() or ""
        self.config.update(last_folder=folder, model=model, mode=mode)
        self.config.save()
        sid = str(uuid.uuid4())
        argv = [self.claude, "--session-id", sid]
        if model:
            argv += ["--model", model]
        if mode:
            argv += ["--permission-mode", mode]
        self._open_tab(folder, sid, argv + prompt_args(text))
        return True

    def _send_chat(self, text):
        return self.resume(self.current_sid, prompt=text)

    def resume(self, sid, prompt=None, fork=False, tab=None):
        """Continue a saved chat in a terminal. Returns True if it started."""
        if not sid:
            return False
        existing = tab or self.tab_for(sid)
        if existing and existing.alive and not fork:
            if prompt:
                existing.type_text(prompt)
            self.show_tab(existing)
            return True
        s = self.convs_by_sid.get(sid)
        if s is None and existing is not None and not fork:
            # A new chat that exited before its first message was saved:
            # start it again under the same id.
            existing.spawn([self.claude, "--session-id", sid] + prompt_args(prompt))
            self.show_tab(existing)
            return True
        if s is None:
            self.error("This chat isn't saved yet", "Send it a first message before resuming it.")
            self.sync_header()
            return False
        if not os.path.isdir(s.cwd):
            self.error("Can't resume this chat here",
                       f"It was started in {s.cwd}, which no longer exists. Claude Code only "
                       "resumes a chat from the folder it started in.")
            self.sync_header()
            return False
        if not self.claude:
            self.error("Claude Code was not found", "The claude command is not on your PATH.")
            return False
        if not fork:
            other = self._elsewhere(sid)
            if other:
                choice = self._ask_elsewhere(other)
                if choice is None:
                    self.sync_header()
                    return False
                fork = choice == "fork"
        argv = [self.claude, "--resume", sid] + (["--fork-session"] if fork else []) + prompt_args(prompt)
        if existing and not existing.alive and not fork:
            existing.spawn(argv)
            self.show_tab(existing)
        else:
            self._open_tab(s.cwd, None if fork else sid, argv)
        return True

    def _open_tab(self, cwd, sid, argv):
        tab = Tab(self, cwd, sid)
        self.tabs.append(tab)
        self.stack.add_named(tab.widget, tab.name)
        tab.spawn(argv)
        self.show_tab(tab)
        self.refresh_sidebar()
        return tab

    def close_tab(self, tab):
        if tab.alive:
            if not self.confirm("Stop Claude in this chat?", "The chat is saved; you can resume it later.",
                                "Stop"):
                return
            tab.stop()
        self.tabs.remove(tab)
        was_current = tab is self.current_tab
        self.stack.remove(tab.widget)
        if was_current:
            if tab.sid in self.convs_by_sid:
                self.last_view[tab.sid] = "chat"
                self.show_chat(tab.sid)
            else:
                self.show_home()
        self.refresh_sidebar()

    def stop_tab(self, tab):
        if tab and tab.alive:
            busy = tab.status == "busy"
            if busy and not self.confirm("Claude is still working", "Stop it anyway? The chat so far is saved.",
                                         "Stop"):
                return
            tab.stop()

    def _elsewhere(self, sid):
        ours = {t.pid for t in self.tabs if t.alive}
        for pid, info in self.live.items():
            if pid not in ours and info.get("sessionId") == sid:
                return info
        return None

    def _ask_elsewhere(self, info):
        dialog = self._message(Gtk.MessageType.QUESTION, "This chat is open somewhere else",
                               f"It's running in {where_label(info)} right now. Opening it here too means two "
                               "Claude processes writing the same chat, which can garble it.\n\n"
                               "“Open a copy” starts a new chat with the same history and leaves the "
                               "original alone.")
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open anyway", 1, "Open a copy", 2)
        dialog.set_default_response(2)
        response = dialog.run()
        dialog.destroy()
        return {1: "anyway", 2: "fork"}.get(response)

    def session_actions(self, sid):
        s = self.convs_by_sid.get(sid)
        tab = self.tab_for(sid)
        items = []
        if tab and tab.alive:
            items.append(("Go to terminal", lambda: self.show_tab(tab)))
        else:
            items.append(("Continue in terminal", lambda: self.resume(sid)))
        if not (self.view == "chat" and self.current_sid == sid):
            items.append(("Read as chat", lambda: self.show_chat(sid)))
        items.append(None)
        pinned = sid in self.config["pins"]
        items.append(("Unpin" if pinned else "Pin", lambda: self.toggle_pin(sid)))
        items.append(("Rename…", lambda: self.rename(sid)))
        items.append(("Open a copy (fork)", lambda: self.resume(sid, fork=True)))
        if s and s.cwd:
            items.append(("New chat in this folder", lambda: self.new_chat_in(s.cwd)))
            items.append(("Open folder", lambda: self.open_uri(Gio.File.new_for_path(s.cwd).get_uri())))
        items.append(("Copy session ID", lambda: self.copy_text(sid)))
        if tab and tab.alive:
            items.append(None)
            items.append(("Stop Claude", lambda: self.stop_tab(tab)))
        items.append(None)
        items.append(("Delete…", lambda: self.delete(sid)))
        return items

    def toggle_pin(self, sid):
        pins = self.config["pins"]
        if sid in pins:
            pins.remove(sid)
        else:
            pins.insert(0, sid)
        self.config.save()
        self.refresh_sidebar()

    def rename(self, sid):
        s = self.convs_by_sid.get(sid)
        current = self.config["titles"].get(sid) or (s.title if s else "")
        dialog = self._dialog("Rename chat", "Rename")
        entry = Gtk.Entry(text=current, activates_default=True, width_chars=40)
        dialog.get_content_area().pack_start(entry, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if name and (not s or name != s.title):
                self.config["titles"][sid] = name
            else:
                self.config["titles"].pop(sid, None)
            self.config.save()
            self.refresh_sidebar()
            self.sync_header()
        dialog.destroy()

    def delete(self, sid):
        s = self.convs_by_sid.get(sid)
        if not s:
            return
        tab = self.tab_for(sid)
        if (tab and tab.alive) or self._elsewhere(sid):
            self.error("This chat is still running", "Stop Claude in it first, then delete it.")
            return
        title = self.config["titles"].get(sid) or s.title
        if not self.confirm("Delete this chat?",
                            f"“{title}” will be moved to the Trash. You can restore it from there.",
                            "Delete", destructive=True):
            return
        try:
            Gio.File.new_for_path(s.path).trash(None)
            side = Path(s.path).with_suffix("")
            if side.is_dir():
                Gio.File.new_for_path(str(side)).trash(None)
        except GLib.Error as err:
            self.error("Couldn't delete the chat", err.message)
            return
        if tab:
            self.tabs.remove(tab)
            self.stack.remove(tab.widget)
        if sid in self.config["pins"]:
            self.config["pins"].remove(sid)
        self.config["titles"].pop(sid, None)
        self.config.save()
        self.convs = [c for c in self.convs if c.sid != sid]
        self.convs_by_sid.pop(sid, None)
        if self.current_sid == sid:
            self.show_home()
        self.refresh_sidebar()
        self._scan()

    def new_chat_in(self, folder):
        self.home_folder = folder
        self._update_folder_label()
        self.show_home()

    def copy_text(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def open_uri(self, uri):
        if not uri or uri.split(":", 1)[0].lower() not in ("http", "https", "mailto", "file"):
            return
        try:
            Gtk.show_uri_on_window(self, uri, Gdk.CURRENT_TIME)
        except GLib.Error as err:
            print(f"claude-terminal: can't open {uri}: {err.message}", file=sys.stderr)

    def zoom(self, step):
        scale = round(min(2.5, max(0.6, self.config["font_scale"] + step)), 2)
        self.config["font_scale"] = scale
        for tab in self.tabs:
            tab.term.set_font_scale(scale)

    def toggle_sidebar(self):
        self._set_sidebar(not self.sidebar.get_reveal_child())

    def _set_sidebar(self, show, animate=True):
        self.sidebar.set_transition_duration(160 if animate else 0)
        self.sidebar.set_reveal_child(show)
        self.config["sidebar"] = show
        ctx = self.header.get_style_context()
        if show:
            ctx.add_class("sidebar-open")
            self.header_left.set_size_request(SIDEBAR_WIDTH - 14, -1)
        else:
            ctx.remove_class("sidebar-open")
            self.header_left.set_size_request(-1, -1)

    # ------------------------------------------------------------ chat view

    def _load_chat(self, mode):
        s = self.convs_by_sid.get(self.current_sid)
        if s is None:
            self._set_thread('<div class="empty">No messages saved in this chat yet.</div>', "bottom")
            return
        self.chat_gen += 1
        gen, shown = self.chat_gen, self.chat_shown
        snapshot = copy.copy(s)

        def work():
            try:
                turns = sessions.load_transcript(snapshot.path)
                body = render.thread_html(turns, shown, snapshot)
            except Exception as err:  # show it in the page rather than dying silently
                body = f'<div class="empty">Couldn\'t read this chat: {html.escape(str(err))}</div>'
            GLib.idle_add(self._chat_loaded, gen, body, mode)

        threading.Thread(target=work, daemon=True).start()

    def _chat_loaded(self, gen, body, mode):
        if gen == self.chat_gen and self.view == "chat":
            self._set_thread(body, mode)
        return False

    def _set_thread(self, body, mode):
        self.last_thread = body
        self._js(f"setThread({json.dumps(body)}, {json.dumps(mode)})")

    def _js(self, script):
        if not self.web_ready:
            self.pending_js = script
            return
        if hasattr(self.web, "evaluate_javascript"):
            self.web.evaluate_javascript(script, -1, None, None, None, None, None)
        else:
            self.web.run_javascript(script, None, None, None)

    def _on_web_message(self, _manager, result):
        value = result.get_js_value() if hasattr(result, "get_js_value") else result
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        if msg.get("ready"):
            self.web_ready = True
            script, self.pending_js = self.pending_js, None
            if script:
                self._js(script)
            elif self.last_thread:
                self._set_thread(self.last_thread, "bottom")
        elif msg.get("more"):
            self.chat_shown += render.PAGE_SIZE
            self._load_chat("keep")
        elif "copy" in msg:
            self.copy_text(str(msg["copy"]))
        elif "open" in msg:
            self.open_uri(str(msg["open"]))

    def _on_policy(self, _web, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri and uri != "about:blank":
                decision.ignore()
                self.open_uri(uri)
                return True
        elif kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    def _on_web_menu(self, _web, menu, _event, _hit):
        keep = {WebKit2.ContextMenuAction.COPY, WebKit2.ContextMenuAction.SELECT_ALL,
                WebKit2.ContextMenuAction.COPY_LINK_TO_CLIPBOARD}
        for item in list(menu.get_items()):
            if item.get_stock_action() not in keep:
                menu.remove(item)
        return menu.get_n_items() == 0

    def _watch(self, path):
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None
        if path:
            self.monitor = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect("changed", self._on_file_changed)

    def _on_file_changed(self, *_):
        if self.reload_timer is None:
            self.reload_timer = GLib.timeout_add(900, self._reload_live)

    def _reload_live(self):
        self.reload_timer = None
        if self.view == "chat":
            self._load_chat("live")
        return False

    # -------------------------------------------------------------- sidebar

    def _scan(self):
        if self.scanning:
            return
        self.scanning = True

        def work():
            try:
                changed = self.index.refresh()
                convs = []
                for s in self.index.conversations():
                    c = copy.copy(s)
                    c.prompts = list(s.prompts)
                    convs.append(c)
            except Exception as err:
                print(f"claude-terminal: scan failed: {err}", file=sys.stderr)
                changed, convs = False, None
            GLib.idle_add(self._scan_done, changed, convs)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, changed, convs):
        self.scanning = False
        if convs is not None and (changed or not self.loaded):
            first = not self.loaded
            self.loaded = True
            self.convs = convs
            self.convs_by_sid = {s.sid: s for s in convs}
            self.list_placeholder.set_text("No chats found")
            self._refresh_folders()
            self.refresh_sidebar()
            if self.view == "home" or first:
                self._refresh_home()
            if self.view == "chat" and self.monitor is None:
                s = self.convs_by_sid.get(self.current_sid)
                if s:  # a brand-new chat just got its first saved message
                    self._watch(s.path)
                    self._load_chat("live")
            self.sync_header()
        return False

    def _tick(self):
        self.live = sessions.live_sessions()
        for tab in self.tabs:
            info = self.live.get(tab.pid) if tab.alive else None
            if not info:
                continue
            before = tab.status
            tab.status = info.get("status") or "idle"
            new_sid = info.get("sessionId")
            if new_sid and new_sid != tab.sid:  # /clear, /resume or a fork inside the terminal
                tab.sid = new_sid
                if tab is self.current_tab:
                    self.current_sid = new_sid
                    self.last_view[new_sid] = self.view
            if before == "busy" and tab.status != "busy":
                self._notify_done(tab)
        self._scan()
        self.refresh_sidebar()
        self.sync_header()
        return True

    def _notify_done(self, tab):
        if self.is_active() and tab is self.current_tab:
            return
        s = self.convs_by_sid.get(tab.sid)
        note = Gio.Notification.new("Claude is done")
        note.set_body(self._title_for(tab.sid) if s else "Your chat is ready")
        self.app.send_notification(f"done-{tab.name}", note)

    def _title_for(self, sid):
        s = self.convs_by_sid.get(sid)
        return self.config["titles"].get(sid) or (s.title if s else "New chat")

    def _statuses(self):
        found = {}
        ours = {t.pid for t in self.tabs if t.alive}
        for pid, info in self.live.items():
            if pid not in ours:
                found[info["sessionId"]] = ("elsewhere", f"Open in {where_label(info)}")
        for tab in self.tabs:
            if tab.alive and tab.sid:
                busy = tab.status == "busy"
                found[tab.sid] = ("busy" if busy else "idle", "Claude is working…" if busy else "Running here")
        return found

    def _entries(self):
        now = datetime.now()
        pins, titles = self.config["pins"], self.config["titles"]
        statuses = self._statuses()
        entries = []
        for tab in self.tabs:
            if tab.alive and (tab.sid is None or tab.sid not in self.convs_by_sid):
                status, tip = statuses.get(tab.sid, ("idle", "Running here"))
                entries.append(Entry(tab.sid or f"tab:{tab.name}", "New chat", tilde(tab.cwd), tab.cwd,
                                     tab.started, False, status, tip, "new chat", "cli", tab=tab))
        for s in self.convs:
            status, tip = statuses.get(s.sid, ("", ""))
            title = titles.get(s.sid) or s.title
            entries.append(Entry(s.sid, title, s.folder, s.cwd, s.mtime, s.sid in pins, status, tip,
                                 title.lower() + "\n" + s.search_text(), s.entrypoint))
        entries.sort(key=lambda e: (not e.pinned, pins.index(e.sid) if e.pinned else 0, -e.mtime))
        for e in entries:
            e.group = "Pinned" if e.pinned else group_for(e.mtime, now)
        return entries

    def refresh_sidebar(self):
        if not self.loaded and not self.tabs:
            return
        entries = self._entries()
        order = [(e.sid, e.group) for e in entries]
        if order == self._order:
            for e in entries:
                self.rows[e.sid].update(e)
            self.listbox.invalidate_filter()
            return
        adj = self.list_scroller.get_vadjustment()
        pos = adj.get_value()
        for row in self.listbox.get_children():
            self.listbox.remove(row)
        self.rows = {}
        for e in entries:
            row = ConvRow(e)
            self.rows[e.sid] = row
            self.listbox.add(row)
        self._order = order
        self.listbox.invalidate_headers()
        GLib.idle_add(adj.set_value, pos)
        self._select_row(self.current_sid if self.view != "term" or not self.current_tab
                         else (self.current_tab.sid or f"tab:{self.current_tab.name}"))

    def _select_row(self, sid):
        row = self.rows.get(sid) if sid else None
        if row is None:
            self.listbox.unselect_all()
        else:
            self.listbox.select_row(row)

    def _refilter(self):
        self.listbox.invalidate_filter()
        self.listbox.invalidate_headers()

    def _filter_func(self, row, _data):
        e = row.entry
        if e.tab is None and not e.status:
            if e.entrypoint == "claude-vscode" and not self.config["show_vscode"]:
                return False
            if e.entrypoint.startswith("sdk") and not self.config["show_sdk"]:
                return False
        folder = self.folder_filter.get_active_id()
        if folder and e.cwd != folder:
            return False
        query = self.search.get_text().strip().lower()
        return all(word in e.search for word in query.split()) if query else True

    def _header_func(self, row, before, _data):
        group = row.entry.group
        if before is not None and before.entry.group == group:
            row.set_header(None)
            return
        header = row.get_header()
        if header is None or header.get_text() != group:
            row.set_header(label(group, "group-header"))

    def _refresh_folders(self):
        active = self.folder_filter.get_active_id() or ""
        folders = self._recent_folders(limit=30)
        self.folder_filter.remove_all()
        self.folder_filter.append("", "All folders")
        for folder in folders:
            self.folder_filter.append(folder, tilde(folder))
        if not self.folder_filter.set_active_id(active):
            self.folder_filter.set_active_id("")

    def _recent_folders(self, limit=10):
        seen = []
        for s in self.convs:
            if s.cwd and s.cwd not in seen and os.path.isdir(s.cwd):
                seen.append(s.cwd)
        return seen[:limit]

    def _list_press(self, listbox, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        row = listbox.get_row_at_y(int(event.y))
        if row is None or row.entry.tab is not None:
            return False
        self._popup_menu(self.session_actions(row.entry.sid), event)
        return True

    # ------------------------------------------------------------ home page

    def _update_greeting(self):
        hour = datetime.now().hour
        part = "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"
        name = self.config["name"].strip()
        self.greeting.set_markup(gradient_markup(f"{part}, {name}" if name else part))

    def _update_folder_label(self):
        self.folder_label.set_text(tilde(self.home_folder))

    def _refresh_home(self):
        for child in self.cards.get_children():
            self.cards.remove(child)
        cards = []
        if self.convs:
            latest = self.convs[0]
            cards.append(("Continue last chat", self._title_for(latest.sid),
                          lambda s=latest.sid: self.show_chat(s)))
        cards += [
            ("Explain a project", "What it does, how it's laid out, how to run it",
             lambda: self.home_composer.set_text(
                 "Give me a quick overview of this project: what it does, how it's structured, "
                 "and how to run it.")),
            ("Find and fix bugs", "Review the code, then fix what matters",
             lambda: self.home_composer.set_text(
                 "Look through this project for bugs. Explain the most important ones before changing anything.")),
            ("Check my PC", "Disk, memory, temperatures, failing services",
             lambda: self.home_composer.set_text(
                 "Check my Linux system's health (disk space, memory, CPU temperature, failed services) "
                 "and tell me if anything needs attention.")),
        ]
        for title, sub, callback in cards[:4]:
            button = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "card")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            box.pack_start(label(title, "card-title", ellipsize=Pango.EllipsizeMode.END), False, False, 0)
            box.pack_start(label(sub, "card-sub", wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                                 max_width_chars=10, yalign=0.0), False, False, 0)
            button.add(box)
            button.connect("clicked", lambda _b, cb=callback: cb())
            self.cards.pack_start(button, True, True, 0)
        self.cards.show_all()

    def _fill_folder_menu(self, button):
        if not button.get_active():
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.pack_start(label("Work in folder", "faint", margin_start=10, margin_top=4, margin_bottom=4),
                       False, False, 0)
        folders = self._recent_folders()
        home = str(Path.home())
        if home not in folders:
            folders.append(home)
        for folder in folders:
            item = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "flat")
            item.add(label(tilde(folder), ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=40))
            item.connect("clicked", self._pick_folder, folder)
            box.pack_start(item, False, False, 0)
        box.pack_start(Gtk.Separator(margin_top=4, margin_bottom=4), False, False, 0)
        other = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "flat")
        other.add(label("Choose another folder…"))
        other.connect("clicked", self._choose_folder)
        box.pack_start(other, False, False, 0)
        box.show_all()
        old = self.folder_popover.get_child()
        if old:
            self.folder_popover.remove(old)
        self.folder_popover.add(box)

    def _pick_folder(self, _button, folder):
        self.folder_popover.popdown()
        self.home_folder = folder
        self._update_folder_label()
        self.home_composer.focus()

    def _choose_folder(self, _button):
        self.folder_popover.popdown()
        dialog = Gtk.FileChooserNative.new("Choose a folder for this chat", self,
                                           Gtk.FileChooserAction.SELECT_FOLDER, "_Select", "_Cancel")
        dialog.set_current_folder(self.home_folder if os.path.isdir(self.home_folder) else str(Path.home()))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self._pick_folder(None, dialog.get_filename())
        dialog.destroy()

    # --------------------------------------------------------------- header

    def sync_header(self):
        sid = self.current_sid
        if self.view == "home":
            title, sub = "New chat", ""
        else:
            title = self._title_for(sid) if sid else "New chat"
            s = self.convs_by_sid.get(sid)
            tab = self.current_tab
            folder = s.folder if s else (tilde(tab.cwd) if tab else "")
            status = self._statuses().get(sid, ("", ""))[1] if sid else ""
            if tab and not tab.alive and self.view == "term":
                status = "Stopped"
            sub = " · ".join(x for x in (folder, status) if x)
        self.title_label.set_text(title)
        self.subtitle_label.set_text(sub)
        self.subtitle_label.set_visible(bool(sub))
        self.switch.set_visible(self.view != "home")
        self.menu_button.set_visible(self.view != "home" and bool(sid) and sid in self.convs_by_sid)
        self.chat_toggle.set_sensitive(bool(sid))
        self._syncing = True
        (self.term_toggle if self.view == "term" else self.chat_toggle).set_active(True)
        self._syncing = False

        if self.view == "chat" and sid:
            tab = self.tab_for(sid)
            other = self._elsewhere(sid)
            if tab and tab.alive:
                self.chat_caption.set_text("Claude is running for this chat; messages go straight to it.")
            else:
                self.chat_caption.set_text("Sending a message resumes this chat in Claude Code.")
            self.banner.set_reveal_child(bool(other) and not (tab and tab.alive))
            if other:
                self.banner_label.set_text(f"This chat is open in {where_label(other)} right now.")

    def _on_switch(self, button, view):
        if self._syncing or not button.get_active():
            return
        if view == "chat":
            self.show_chat(self.current_sid)
        else:
            self.resume(self.current_sid)

    def _fill_header_menu(self, button):
        if not button.get_active() or not self.current_sid:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for item in self.session_actions(self.current_sid):
            if item is None:
                box.pack_start(Gtk.Separator(margin_top=3, margin_bottom=3), False, False, 0)
                continue
            text, callback = item
            entry = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "flat")
            entry.add(label(text))
            entry.connect("clicked", lambda _b, cb=callback: (self.menu_popover.popdown(), cb()))
            box.pack_start(entry, False, False, 0)
        box.pack_start(Gtk.Separator(margin_top=3, margin_bottom=3), False, False, 0)
        for text, callback in (("Settings", self.settings_dialog), ("Keyboard shortcuts", self.shortcuts_dialog)):
            entry = add_class(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "flat")
            entry.add(label(text))
            entry.connect("clicked", lambda _b, cb=callback: (self.menu_popover.popdown(), cb()))
            box.pack_start(entry, False, False, 0)
        box.show_all()
        old = self.menu_popover.get_child()
        if old:
            self.menu_popover.remove(old)
        self.menu_popover.add(box)

    # ---------------------------------------------------------------- menus

    def _popup_menu(self, items, event):
        menu = add_class(Gtk.Menu(), "ct-menu")
        for item in items:
            if item is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            text, callback = item
            mi = Gtk.MenuItem(label=text)
            mi.connect("activate", lambda _mi, cb=callback: GLib.idle_add(lambda: cb() and False))
            menu.append(mi)
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(event)

    def terminal_menu(self, tab, event):
        items = [
            ("Copy", lambda: tab.term.copy_clipboard_format(Vte.Format.TEXT)) if tab.term.get_has_selection() else None,
            ("Paste", tab.term.paste_clipboard),
            ("Select all", tab.term.select_all),
        ]
        items = [i for i in items if i is not None]
        if tab.sid and tab.sid in self.convs_by_sid:
            items += [None, ("Read as chat", lambda: self.show_chat(tab.sid))]
        items += [None, ("Zoom in", lambda: self.zoom(0.1)), ("Zoom out", lambda: self.zoom(-0.1)),
                  ("Reset zoom", lambda: self.zoom(1.0 - self.config["font_scale"]))]
        if tab.alive:
            items += [None, ("Stop Claude", lambda: self.stop_tab(tab))]
        self._popup_menu(items, event)

    # -------------------------------------------------------------- dialogs

    def _message(self, kind, title, body):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text(body)
        add_class(dialog, "ct", "ct-dialog")
        return dialog

    def error(self, title, body):
        dialog = self._message(Gtk.MessageType.ERROR, title, body)
        dialog.add_button("OK", Gtk.ResponseType.OK)
        dialog.run()
        dialog.destroy()
        return False

    def confirm(self, title, body, action, destructive=False):
        dialog = self._message(Gtk.MessageType.QUESTION, title, body)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        button = dialog.add_button(action, Gtk.ResponseType.OK)
        if destructive:
            button.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL if destructive else Gtk.ResponseType.OK)
        ok = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return ok

    def _dialog(self, title, action):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True, use_header_bar=True)
        add_class(dialog, "ct", "ct-dialog")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(action, Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(10)
        area.set_border_width(18)
        return dialog

    def settings_dialog(self):
        dialog = self._dialog("Settings", "Save")
        grid = Gtk.Grid(column_spacing=18, row_spacing=12)
        name = Gtk.Entry(text=self.config["name"], placeholder_text="Shown in the greeting", hexpand=True)
        font = Gtk.FontButton(font=self.config["font"])
        font.set_filter_func(lambda family, _face, _d: family.is_monospace(), None)
        vscode = Gtk.Switch(active=self.config["show_vscode"], halign=Gtk.Align.START)
        sdk = Gtk.Switch(active=self.config["show_sdk"], halign=Gtk.Align.START)
        rows = [("Your name", name), ("Terminal font", font),
                ("Show chats started in VS Code", vscode), ("Show headless (claude -p) chats", sdk)]
        for i, (text, widget) in enumerate(rows):
            grid.attach(label(text), 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
        dialog.get_content_area().pack_start(grid, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self.config.update(name=name.get_text().strip(), font=font.get_font(),
                               show_vscode=vscode.get_active(), show_sdk=sdk.get_active())
            self.config.save()
            for tab in self.tabs:
                tab.term.set_font(Pango.FontDescription.from_string(self.config["font"]))
            self._update_greeting()
            self._refilter()
        dialog.destroy()

    def shortcuts_dialog(self):
        keys = [
            ("Ctrl+Shift+N", "New chat"),
            ("Ctrl+Shift+F", "Search chats"),
            ("Ctrl+Shift+S", "Show or hide the sidebar"),
            ("Ctrl+Shift+T", "Switch between chat and terminal"),
            ("Ctrl+Shift+W", "Stop Claude in this chat"),
            ("Ctrl+Shift+C / V", "Copy / paste in the terminal"),
            ("Ctrl+scroll", "Zoom the terminal"),
            ("Ctrl+click", "Open a link in the terminal"),
            ("Enter / Shift+Enter", "Send / new line in the message box"),
        ]
        dialog = self._message(Gtk.MessageType.INFO, "Keyboard shortcuts", "")
        rows = "\n".join(f"{k:<22}{v}" for k, v in keys)
        dialog.format_secondary_markup(f"<tt>{GLib.markup_escape_text(rows)}</tt>")
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.run()
        dialog.destroy()

    # ------------------------------------------------------------ keyboard

    def _on_key(self, _widget, event):
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if mods != (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        key = Gdk.keyval_to_lower(event.keyval)
        tab = self.current_tab if self.view == "term" else None
        if key == Gdk.KEY_n:
            self.show_home()
        elif key == Gdk.KEY_f:
            if not self.sidebar.get_reveal_child():
                self._set_sidebar(True)
            self.search.grab_focus()
        elif key == Gdk.KEY_s:
            self.toggle_sidebar()
        elif key == Gdk.KEY_t:
            self.toggle_view()
        elif key == Gdk.KEY_w and tab:
            self.stop_tab(tab)
        elif key == Gdk.KEY_c and tab:
            tab.term.copy_clipboard_format(Vte.Format.TEXT)
        elif key == Gdk.KEY_v and tab:
            tab.term.paste_clipboard()
        else:
            return False
        return True

    def _on_close(self, *_):
        busy = [t for t in self.tabs if t.alive and t.status == "busy"]
        if busy and not self.confirm("Claude is still working",
                                     f"{len(busy)} chat(s) are mid-task. Closing stops them; "
                                     "what's done so far is saved.", "Close anyway"):
            return True
        if not self.is_maximized():
            self.config["window"] = list(self.get_size())
        self.config.save()
        return False


class App(Gtk.Application):
    def __init__(self):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        GLib.set_prgname("claude-terminal")
        Gdk.set_program_class("claude-terminal")  # WM_CLASS, matched by the launcher
        GLib.set_application_name(APP_NAME)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Handy.init()
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(APP_DIR / "style.css"))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        window = self.props.active_window or MainWindow(self)
        window.present()


def main():
    for name in HARNESS_ENV:  # launched from inside a Claude session
        os.environ.pop(name, None)
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
