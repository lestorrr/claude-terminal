"""Turn a parsed transcript into the HTML shown in the chat view."""
import base64
import functools
import html
import json
import re
import threading
from datetime import datetime

import markdown
from pygments.formatters import HtmlFormatter

from sessions import tilde

PAGE_SIZE = 80           # turns rendered per "show earlier" step
MAX_TOOL_TEXT = 3000     # chars of tool input/output kept in the page
MAX_IMAGE_BYTES = 3_000_000

_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def _prepare(text):
    """Nudge Claude's GitHub-flavoured markdown into what Python-Markdown
    accepts: blank lines before lists/tables/fences, 4-space nesting, and
    fences that are not indented."""
    out, prev = [], ""
    fence, fence_indent = None, 0
    for line in text.split("\n"):
        if fence:
            if line.strip().startswith(fence):
                fence = None
                line = line.strip()
            elif fence_indent and line[:fence_indent].strip() == "":
                line = line[fence_indent:]
            out.append(line)
            prev = line
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence, fence_indent = m.group(2), len(m.group(1))
            line = line.lstrip()
            if prev.strip():
                out.append("")
        else:
            m = _LIST_RE.match(line)
            if m:
                indent = len(m.group(1).expandtabs(4))
                if indent % 4:
                    line = " " * (indent * 2) + line.lstrip()
                if prev.strip() and not _LIST_RE.match(prev) and not prev.startswith(" "):
                    out.append("")
            elif line.lstrip().startswith("|") and prev.strip() and not prev.lstrip().startswith("|"):
                out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def _new_md():
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists", "codehilite"],
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "hl"}},
    )
    # Show raw HTML in messages as text instead of letting it into the page.
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return md


_MD = _new_md()
_MD_LOCK = threading.Lock()  # chats render on worker threads; Markdown() is stateful


@functools.lru_cache(maxsize=4096)
def md_to_html(text):
    with _MD_LOCK:
        _MD.reset()
        return _MD.convert(_prepare(text))


def pygments_css():
    return HtmlFormatter(style="monokai").get_style_defs(".hl")


def _clip(text, limit=MAX_TOOL_TEXT):
    text = text or ""
    if len(text) > limit:
        return text[:limit] + f"\n… ({len(text) - limit:,} more characters)"
    return text


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def tool_label(name):
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].replace("claude_ai_", "") if len(parts) > 1 else "mcp"
        return f"{server} · {parts[-1]}"
    return name


def tool_summary(tool):
    inp = tool.input
    for key in ("description", "file_path", "notebook_path", "path", "pattern",
                "url", "query", "command", "skill", "prompt", "subject"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            if key.endswith("path"):
                val = tilde(val)
            return _first_line(val)[:140]
    return ""


def tool_detail(tool):
    inp, name = tool.input, tool.name
    if name == "Bash" and isinstance(inp.get("command"), str):
        return "$ " + inp["command"]
    if name == "Edit" and "old_string" in inp:
        old = "\n".join("- " + l for l in str(inp.get("old_string", "")).splitlines())
        new = "\n".join("+ " + l for l in str(inp.get("new_string", "")).splitlines())
        return f"{tilde(str(inp.get('file_path', '')))}\n{old}\n{new}"
    if name == "Write" and "content" in inp:
        return f"{tilde(str(inp.get('file_path', '')))}\n{inp['content']}"
    return json.dumps(inp, indent=2, ensure_ascii=False)


def _tool_io(tool):
    out = f'<pre class="out">{html.escape(_clip(tool.result))}</pre>' if tool.result else ""
    return f'<pre class="in">{html.escape(_clip(tool_detail(tool)))}</pre>{out}'


def _tool_html(tool):
    status = ""
    if tool.is_error:
        status = '<span class="badge err">error</span>'
    elif tool.result is None:
        status = '<span class="badge">no result</span>'
    return (
        f'<details class="tool" data-k="{html.escape(tool.id)}">'
        f'<summary><b>{html.escape(tool_label(tool.name))}</b>'
        f'<span class="sum">{html.escape(tool_summary(tool))}</span>{status}</summary>'
        f'{_tool_io(tool)}</details>'
    )


def _tool_group_html(group):
    names = []
    for tool in group:
        label = tool_label(tool.name)
        if label not in names:
            names.append(label)
    errors = sum(1 for t in group if t.is_error)
    head = (f"Used {len(group)} tools" if len(group) > 1
            else f"{tool_label(group[0].name)}")
    detail = " · ".join(names[:4]) + (" …" if len(names) > 4 else "")
    if len(group) == 1:
        detail = tool_summary(group[0]) or detail
    err = f'<span class="badge err">{errors} failed</span>' if errors else ""
    # One tool: open straight to its input/output instead of a nested row.
    inner = _tool_io(group[0]) if len(group) == 1 else "".join(_tool_html(t) for t in group)
    return (
        f'<details class="tools" data-k="g-{html.escape(group[0].id)}">'
        f'<summary><span class="gear"></span><span class="head">{html.escape(head)}</span>'
        f'<span class="sum">{html.escape(detail)}</span>{err}</summary>'
        f'<div class="list">{inner}</div></details>'
    )


def _images_html(images):
    parts = []
    for src in images:
        data = src.get("data") if isinstance(src, dict) else None
        mime = src.get("media_type", "image/png") if isinstance(src, dict) else ""
        if data and len(data) <= MAX_IMAGE_BYTES and re.fullmatch(r"image/[\w.+-]+", mime):
            parts.append(f'<img src="data:{mime};base64,{data}" alt="attached image">')
        else:
            parts.append('<span class="chip">Image</span>')
    return f'<div class="imgs">{"".join(parts)}</div>' if parts else ""


def _time(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%b %-d, %-I:%M %p")
    except (ValueError, AttributeError):
        return ""


def turn_html(turn):
    when = html.escape(_time(turn.ts))
    if turn.role == "user":
        text = f'<div class="text">{html.escape(turn.text)}</div>' if turn.text else ""
        return (f'<div class="msg user" title="{when}"><div class="bubble">'
                f'{_images_html(turn.images)}{text}</div></div>')
    if turn.role == "assistant":
        parts, group = [], []
        for kind, value in turn.blocks:
            if kind == "tool":
                group.append(value)
                continue
            if group:
                parts.append(_tool_group_html(group))
                group = []
            parts.append(f'<div class="md">{md_to_html(value)}</div>')
        if group:
            parts.append(_tool_group_html(group))
        return (f'<div class="msg assistant" title="{when}"><div class="avatar"></div>'
                f'<div class="body">{"".join(parts)}</div></div>')
    if turn.role == "command":
        return f'<div class="event"><span class="chip cmd">{html.escape(turn.text)}</span></div>'
    if turn.role == "output":
        return (f'<div class="event"><details class="output"><summary>Command output</summary>'
                f'<pre>{html.escape(_clip(turn.text, 2000))}</pre></details></div>')
    if turn.role == "divider":
        return f'<div class="divider"><span>{html.escape(turn.text)}</span></div>'
    return f'<div class="event"><span class="note">{html.escape(turn.text)}</span></div>'


def thread_html(turns, shown, session=None):
    """Inner HTML for the last `shown` turns plus a 'show earlier' button."""
    hidden = max(0, len(turns) - shown)
    parts = []
    if session is not None:
        bits = [html.escape(session.folder or "")]
        if session.started:
            bits.append(html.escape(_time(session.started)))
        prompts = session.n_prompts
        bits.append(f"{prompts} message{'s' if prompts != 1 else ''}")
        parts.append(f'<div class="meta">{" · ".join(b for b in bits if b)}</div>')
    if hidden:
        parts.append(f'<div class="more"><button onclick="post({{more:1}})">'
                     f'Show earlier messages ({hidden})</button></div>')
    if not turns:
        parts.append('<div class="empty">No messages in this chat yet.</div>')
    parts.extend(turn_html(t) for t in turns[hidden:])
    return "".join(parts)


def shell_html(css):
    """The page loaded once into the web view; content arrives via setThread()."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>{css}
{pygments_css()}</style></head>
<body><main id="thread"></main>
<script>
function post(msg) {{ window.webkit.messageHandlers.ct.postMessage(JSON.stringify(msg)); }}
function nearBottom() {{
  const el = document.scrollingElement;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}}
function addCopyButtons(root) {{
  root.querySelectorAll('.md pre').forEach(pre => {{
    if (pre.parentElement.classList.contains('code')) return;
    const wrap = document.createElement('div');
    wrap.className = 'code';
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);
    const btn = document.createElement('button');
    btn.className = 'copy'; btn.textContent = 'Copy';
    btn.onclick = () => {{
      post({{copy: pre.innerText}});
      btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1200);
    }};
    wrap.appendChild(btn);
  }});
}}
function setThread(html, mode) {{
  const el = document.scrollingElement;
  const thread = document.getElementById('thread');
  const open = new Set([...thread.querySelectorAll('details[open]')].map(d => d.dataset.k));
  const stick = nearBottom();
  const fromBottom = el.scrollHeight - el.scrollTop;
  thread.innerHTML = html;
  thread.querySelectorAll('details').forEach(d => {{ if (open.has(d.dataset.k)) d.open = true; }});
  addCopyButtons(thread);
  if (mode === 'bottom' || (mode === 'live' && stick)) el.scrollTop = el.scrollHeight;
  else el.scrollTop = el.scrollHeight - fromBottom;
}}
document.addEventListener('click', e => {{
  const a = e.target.closest('a[href]');
  if (a) {{ e.preventDefault(); post({{open: a.href}}); }}
}});
post({{ready: 1}});
</script></body></html>"""
