"""Read Claude Code's on-disk session store.

Claude Code writes one append-only JSONL file per conversation under
~/.claude/projects/<mangled-cwd>/<session-id>.jsonl, and one small JSON file
per running process under ~/.claude/sessions/<pid>.json. This module only
reads those; the app never writes into ~/.claude.
"""
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
LIVE_DIR = HOME / ".claude" / "sessions"
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "claude-terminal" / "index.json"
INDEX_VERSION = 2

MAX_SEARCH_PROMPTS = 80
MAX_SEARCH_CHARS = 300

_WRAPPER_RE = re.compile(r"<(system-reminder|local-command-caveat)>.*?</\1>", re.S)
_CMD_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_STDOUT_RE = re.compile(r"<local-command-(stdout|stderr)>(.*?)</local-command-\1>", re.S)
_IMAGE_NOTE_RE = re.compile(r"^\[Image[^\]\n]*\]\s*$", re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_TAG_RE = re.compile(r"<(\w[\w-]*)>(.*?)</\1>", re.S)


def tilde(path):
    """Shorten a path under $HOME to ~/..."""
    home = str(HOME)
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def strip_ansi(text):
    return _ANSI_RE.sub("", text)


def _tag(text, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return m.group(1).strip() if m else ""


def _note_for_origin(origin, text):
    if origin == "task-notification":
        summary = _tag(text, "summary") or "Background task finished"
        status = _tag(text, "status")
        return f"{summary} ({status})" if status and status not in summary else summary
    if origin == "peer":
        return "Message from another Claude session"
    if origin == "auto-continuation":
        return "Usage limit reset, continuing the task"
    first = text.strip().splitlines()[0] if text.strip() else origin
    return first[:160]


def classify_user(rec):
    """Work out what a `type: user` record means to a reader.

    Returns (kind, text, images). kind is one of:
      prompt       something the person typed
      command      a slash command, text is e.g. "/model opus"
      output       local command stdout (text has ANSI stripped)
      note         harness-generated message (task done, interrupt, ...)
      tool_result  carries tool output only
      skip         nothing worth showing
    """
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return "skip", "", []
    content = (rec.get("message") or {}).get("content")
    texts, images = [], []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                return "tool_result", "", []
            if kind == "text":
                texts.append(block.get("text") or "")
            elif kind == "image":
                images.append(block.get("source") or {})
            elif kind == "document":
                texts.append("[Attached document]")
    text = "\n".join(texts)

    origin = (rec.get("origin") or {}).get("kind")
    if origin not in (None, "human"):
        return "note", _note_for_origin(origin, text), []

    m = _CMD_RE.search(text)
    if m:
        cmd = m.group(1).strip()
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        args = _ARGS_RE.search(text)
        if args and args.group(1).strip():
            cmd += " " + args.group(1).strip()
        return "command", cmd, []
    m = _STDOUT_RE.search(text)
    if m:
        out = strip_ansi(m.group(2)).strip()
        return ("output", out, []) if out else ("skip", "", [])

    text = _WRAPPER_RE.sub("", text)
    text = _IMAGE_NOTE_RE.sub("", text).strip()
    if text.startswith("[Request interrupted"):
        return "note", "Interrupted", []
    if not text and not images:
        return "skip", "", []
    return "prompt", text, images


# --------------------------------------------------------------------------
# Conversation index (sidebar)
# --------------------------------------------------------------------------

@dataclass
class Session:
    sid: str
    path: str
    cwd: str = ""
    ai_title: str = ""
    custom_title: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    started: str = ""
    entrypoint: str = ""
    n_prompts: int = 0
    prompts: list = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0
    offset: int = 0

    @property
    def title(self):
        if self.custom_title:
            return self.custom_title
        if self.ai_title:
            return self.ai_title
        text = " ".join(self.first_prompt.split())
        return (text[:70] + "…") if len(text) > 70 else (text or "Untitled chat")

    @property
    def folder(self):
        return tilde(self.cwd) if self.cwd else ""

    def search_text(self):
        return "\n".join([self.title, self.folder, *self.prompts]).lower()


def _consume(session, rec):
    kind = rec.get("type")
    if kind == "ai-title" and rec.get("aiTitle"):
        session.ai_title = str(rec["aiTitle"]).strip()
    elif kind == "custom-title" and rec.get("customTitle"):
        session.custom_title = str(rec["customTitle"]).strip()
    elif kind == "summary" and rec.get("summary") and not session.ai_title:
        session.ai_title = str(rec["summary"]).strip()
    if not session.cwd and isinstance(rec.get("cwd"), str):
        session.cwd = rec["cwd"]
    if kind != "user":
        return
    what, text, images = classify_user(rec)
    if what != "prompt":
        return
    if not text and images:
        text = "[Image]"
    session.n_prompts += 1
    session.last_prompt = text[:MAX_SEARCH_CHARS]
    if not session.first_prompt:
        session.first_prompt = text[:500]
        session.started = rec.get("timestamp") or ""
        session.entrypoint = rec.get("entrypoint") or ""
    if len(session.prompts) < MAX_SEARCH_PROMPTS:
        session.prompts.append(text[:MAX_SEARCH_CHARS])


def _wanted(line, have_cwd):
    """Cheap byte test so we only json-parse lines that can matter."""
    if b'"aiTitle"' in line or b'"customTitle"' in line or b'"summary"' in line:
        return True
    if (b'"type":"user"' in line or b'"type": "user"' in line) and b'"tool_use_id"' not in line:
        return True
    return not have_cwd and b'"cwd"' in line


def scan_file(session, path):
    """Read whatever was appended since session.offset. Returns True if read."""
    read_any = False
    with open(path, "rb") as f:
        f.seek(session.offset)
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written; pick it up next time
            session.offset += len(line)
            read_any = True
            if not _wanted(line, bool(session.cwd)):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                _consume(session, rec)
    return read_any


class Index:
    """Incremental, cached index of every conversation on disk."""

    def __init__(self, projects_dir=PROJECTS_DIR, cache_file=CACHE_FILE):
        self.projects_dir = Path(projects_dir)
        self.cache_file = Path(cache_file) if cache_file else None
        self.sessions = {}  # path -> Session
        self._load_cache()

    def _load_cache(self):
        if not self.cache_file or not self.cache_file.exists():
            return
        try:
            data = json.loads(self.cache_file.read_text())
            if data.get("version") != INDEX_VERSION:
                return
            for path, fields in data.get("files", {}).items():
                self.sessions[path] = Session(**fields)
        except (OSError, ValueError, TypeError):
            self.sessions = {}

    def _save_cache(self):
        if not self.cache_file:
            return
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        payload = {"version": INDEX_VERSION,
                   "files": {p: asdict(s) for p, s in self.sessions.items()}}
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.cache_file)

    def refresh(self):
        """Stat every session file and read only what changed. Returns True
        if anything in the index changed."""
        changed = False
        seen = set()
        for path in self.projects_dir.glob("*/*.jsonl"):
            key = str(path)
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            s = self.sessions.get(key)
            if s and s.mtime == st.st_mtime and s.size == st.st_size:
                continue
            if s is None or st.st_size < s.offset:
                s = Session(sid=path.stem, path=key)  # new, or rewritten
            try:
                scan_file(s, path)
            except OSError:
                continue
            s.mtime, s.size = st.st_mtime, st.st_size
            self.sessions[key] = s
            changed = True
        for gone in set(self.sessions) - seen:
            del self.sessions[gone]
            changed = True
        if changed:
            try:
                self._save_cache()
            except OSError:
                pass
        return changed

    def conversations(self):
        """Sessions with at least one real prompt, newest first."""
        items = [s for s in self.sessions.values() if s.n_prompts > 0]
        items.sort(key=lambda s: s.mtime, reverse=True)
        return items


# --------------------------------------------------------------------------
# Running Claude processes
# --------------------------------------------------------------------------

def _proc_start(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Fields after "(comm)" start at field 3; starttime is field 22.
    fields = stat.rsplit(")", 1)[1].split()
    return fields[19] if len(fields) > 19 else None


def live_sessions(live_dir=LIVE_DIR):
    """Running Claude Code processes, keyed by pid."""
    out = {}
    try:
        files = list(Path(live_dir).glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or not info.get("sessionId"):
            continue
        start = _proc_start(pid)
        if start is None:
            continue
        if info.get("procStart") and str(info["procStart"]) != start:
            continue  # pid was reused by something else
        out[pid] = info
    return out


# --------------------------------------------------------------------------
# Transcript (chat view)
# --------------------------------------------------------------------------

@dataclass
class Tool:
    id: str
    name: str
    input: dict
    result: str = None
    is_error: bool = False


@dataclass
class Turn:
    role: str  # user | assistant | note | command | output | divider
    text: str = ""
    images: list = field(default_factory=list)
    blocks: list = field(default_factory=list)  # assistant: ("text", str) | ("tool", Tool)
    ts: str = ""


def _result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif block.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts)


def _active_branch(path):
    """Records on the branch that ends at the newest message.

    Rewinding (Esc Esc) leaves the abandoned branch in the file, and
    compaction starts a new root that links back via logicalParentUuid.
    """
    records, order, leaf = {}, [], None
    with open(path, "rb") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            uid = rec.get("uuid")
            if not uid:
                continue
            records[uid] = rec
            order.append(rec)
            if rec.get("type") in ("user", "assistant"):
                leaf = uid
    chain, seen, uid = [], set(), leaf
    while uid and uid in records and uid not in seen:
        seen.add(uid)
        rec = records[uid]
        chain.append(rec)
        uid = rec.get("parentUuid") or rec.get("logicalParentUuid")
    if uid and uid not in records:
        return order  # broken link: fall back to file order
    chain.reverse()
    return chain


def load_transcript(path):
    turns, tools, current = [], {}, None
    for rec in _active_branch(path):
        kind = rec.get("type")
        if kind == "assistant":
            msg = rec.get("message") or {}
            content = msg.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if rec.get("isApiErrorMessage") or msg.get("model") == "<synthetic>":
                text = _result_text(content).strip()
                if text and text != "No response requested.":
                    turns.append(Turn("note", text, ts=rec.get("timestamp", "")))
                    current = None
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and (block.get("text") or "").strip():
                    item = ("text", block["text"])
                elif btype == "tool_use":
                    tool = Tool(block.get("id", ""), block.get("name", "tool"),
                                block.get("input") if isinstance(block.get("input"), dict) else {})
                    tools[tool.id] = tool
                    item = ("tool", tool)
                else:
                    continue
                if current is None:
                    current = Turn("assistant", ts=rec.get("timestamp", ""))
                    turns.append(current)
                current.blocks.append(item)
        elif kind == "user":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool = tools.get(block.get("tool_use_id"))
                        if tool:
                            tool.result = _result_text(block.get("content"))
                            tool.is_error = bool(block.get("is_error"))
            what, text, images = classify_user(rec)
            if what in ("skip", "tool_result"):
                continue
            current = None
            role = "user" if what == "prompt" else what
            turns.append(Turn(role, text, images, ts=rec.get("timestamp", "")))
        elif kind == "system" and rec.get("subtype") == "compact_boundary":
            current = None
            turns.append(Turn("divider", "Earlier messages were compacted",
                              ts=rec.get("timestamp", "")))
    return turns
