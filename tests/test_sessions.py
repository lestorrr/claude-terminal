"""Tests for the session reader and transcript renderer (no GTK needed).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import render  # noqa: E402
import sessions  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def user(uid, parent, content, **extra):
    rec = {"type": "user", "uuid": uid, "parentUuid": parent, "cwd": "/home/u/proj",
           "timestamp": "2026-09-28T10:00:00.000Z", "entrypoint": "cli",
           "message": {"role": "user", "content": content}}
    rec.update(extra)
    return rec


def assistant(uid, parent, blocks, **extra):
    rec = {"type": "assistant", "uuid": uid, "parentUuid": parent,
           "message": {"role": "assistant", "model": "claude-x", "content": blocks}}
    rec.update(extra)
    return rec


def text(t):
    return {"type": "text", "text": t}


def write(path, records, tail=""):
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        f.write(tail)


class ClassifyUser(unittest.TestCase):
    def test_typed_prompt(self):
        self.assertEqual(sessions.classify_user(user("a", None, "hello"))[:2], ("prompt", "hello"))

    def test_slash_command(self):
        rec = user("a", None, "<command-name>/model</command-name><command-args>opus</command-args>")
        self.assertEqual(sessions.classify_user(rec)[:2], ("command", "/model opus"))

    def test_command_output_strips_ansi(self):
        rec = user("a", None, "<local-command-stdout>\x1b[1mSet model\x1b[22m</local-command-stdout>")
        self.assertEqual(sessions.classify_user(rec)[:2], ("output", "Set model"))

    def test_hidden_records(self):
        self.assertEqual(sessions.classify_user(user("a", None, "x", isMeta=True))[0], "skip")
        self.assertEqual(sessions.classify_user(user("a", None, "x", isCompactSummary=True))[0], "skip")
        caveat = "<local-command-caveat>Caveat: ignore</local-command-caveat>"
        self.assertEqual(sessions.classify_user(user("a", None, caveat))[0], "skip")

    def test_tool_result(self):
        rec = user("a", None, [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])
        self.assertEqual(sessions.classify_user(rec)[0], "tool_result")

    def test_task_notification_uses_summary(self):
        body = "<task-notification><status>completed</status><summary>Build done</summary></task-notification>"
        rec = user("a", None, body, origin={"kind": "task-notification"})
        self.assertEqual(sessions.classify_user(rec)[:2], ("note", "Build done (completed)"))

    def test_interrupt_is_note(self):
        self.assertEqual(sessions.classify_user(user("a", None, "[Request interrupted by user]"))[:2],
                         ("note", "Interrupted"))

    def test_image_placeholder_removed(self):
        rec = user("a", None, [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                             "data": "AAAA"}},
                               text("[Image: original 10x10, displayed at 10x10.]"), text("what is this")])
        kind, body, images = sessions.classify_user(rec)
        self.assertEqual((kind, body, len(images)), ("prompt", "what is this", 1))

    def test_system_reminder_stripped(self):
        rec = user("a", None, "fix it<system-reminder>internal</system-reminder>")
        self.assertEqual(sessions.classify_user(rec)[1], "fix it")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        (self.projects / "-home-u-proj").mkdir(parents=True)
        self.path = self.projects / "-home-u-proj" / f"{SID}.jsonl"
        self.cache = Path(self.tmp.name) / "cache" / "index.json"

    def tearDown(self):
        self.tmp.cleanup()

    def index(self):
        idx = sessions.Index(self.projects, self.cache)
        idx.refresh()
        return idx

    def test_title_prompt_and_cwd(self):
        write(self.path, [
            {"type": "mode", "mode": "normal", "sessionId": SID},
            user("u1", None, "install clockify please"),
            assistant("a1", "u1", [text("done")]),
            {"type": "ai-title", "aiTitle": "Clockify install", "sessionId": SID},
        ])
        [s] = self.index().conversations()
        self.assertEqual((s.sid, s.title, s.cwd, s.n_prompts), (SID, "Clockify install", "/home/u/proj", 1))
        self.assertEqual(s.first_prompt, "install clockify please")

    def test_spaced_json_is_read_too(self):
        with open(self.path, "w") as f:
            f.write(json.dumps(user("u1", None, "spaced out")) + "\n")
        self.assertEqual(self.index().conversations()[0].first_prompt, "spaced out")

    def test_custom_title_wins(self):
        write(self.path, [user("u1", None, "hi"),
                          {"type": "custom-title", "customTitle": "Mine", "sessionId": SID},
                          {"type": "ai-title", "aiTitle": "Auto", "sessionId": SID}])
        self.assertEqual(self.index().conversations()[0].title, "Mine")

    def test_fallback_title_is_first_prompt(self):
        write(self.path, [user("u1", None, "  what   is\nthis  ")])
        self.assertEqual(self.index().conversations()[0].title, "what is this")

    def test_sessions_without_prompts_are_hidden(self):
        write(self.path, [{"type": "mode", "mode": "normal", "sessionId": SID},
                          user("u1", None, "<command-name>/login</command-name>")])
        self.assertEqual(self.index().conversations(), [])

    def test_incremental_append_and_partial_line(self):
        partial = json.dumps(user("u2", "a1", "second"), separators=(",", ":"))
        write(self.path, [user("u1", None, "first"), assistant("a1", "u1", [text("ok")])],
              tail=partial[:25])
        idx = self.index()
        self.assertEqual(idx.conversations()[0].n_prompts, 1)
        with open(self.path, "a") as f:
            f.write(partial[25:] + "\n")
        self.assertTrue(idx.refresh())
        s = idx.conversations()[0]
        self.assertEqual((s.n_prompts, s.last_prompt), (2, "second"))
        self.assertFalse(idx.refresh())  # nothing new

    def test_rewritten_file_is_rescanned(self):
        write(self.path, [user("u1", None, "old one"), user("u2", "u1", "old two")])
        idx = self.index()
        write(self.path, [user("u9", None, "new")])
        os.utime(self.path, (1, 1))
        idx.refresh()
        s = idx.conversations()[0]
        self.assertEqual((s.n_prompts, s.first_prompt), (1, "new"))

    def test_cache_round_trip(self):
        write(self.path, [user("u1", None, "cached prompt")])
        self.index()
        again = sessions.Index(self.projects, self.cache)
        self.assertEqual(again.sessions[str(self.path)].first_prompt, "cached prompt")
        self.assertFalse(again.refresh())

    def test_deleted_file_leaves_index(self):
        write(self.path, [user("u1", None, "x")])
        idx = self.index()
        self.path.unlink()
        idx.refresh()
        self.assertEqual(idx.conversations(), [])


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / f"{SID}.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_tools_attach_results_and_merge_turn(self):
        write(self.path, [
            user("u1", None, "list files"),
            assistant("a1", "u1", [text("Looking.")]),
            assistant("a2", "a1", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                    "input": {"command": "ls", "description": "List files"}}]),
            user("r1", "a2", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]),
            assistant("a3", "r1", [text("One file.")]),
        ])
        turns = sessions.load_transcript(self.path)
        self.assertEqual([t.role for t in turns], ["user", "assistant"])
        kinds = [k for k, _ in turns[1].blocks]
        self.assertEqual(kinds, ["text", "tool", "text"])
        tool = turns[1].blocks[1][1]
        self.assertEqual((tool.name, tool.result, tool.is_error), ("Bash", "a.txt", False))

    def test_rewind_shows_only_the_active_branch(self):
        write(self.path, [
            user("u1", None, "start"),
            assistant("a1", "u1", [text("first answer")]),
            user("u2", "a1", "abandoned question"),
            assistant("a2", "u2", [text("abandoned answer")]),
            user("u3", "a1", "rewritten question"),  # rewound to a1
            assistant("a3", "u3", [text("new answer")]),
        ])
        texts = [t.text or t.blocks[0][1] for t in sessions.load_transcript(self.path)]
        self.assertEqual(texts, ["start", "first answer", "rewritten question", "new answer"])

    def test_compaction_keeps_earlier_history(self):
        write(self.path, [
            user("u1", None, "before"),
            assistant("a1", "u1", [text("answer before")]),
            {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "parentUuid": None,
             "logicalParentUuid": "a1"},
            user("s1", "c1", "This session is being continued...", isCompactSummary=True),
            user("u2", "s1", "after"),
        ])
        roles = [t.role for t in sessions.load_transcript(self.path)]
        self.assertEqual(roles, ["user", "assistant", "divider", "user"])

    def test_synthetic_error_is_a_note(self):
        write(self.path, [user("u1", None, "hi"),
                          assistant("a1", "u1", [text("You've hit your limit")],
                                    message={"model": "<synthetic>", "content": [text("You've hit your limit")]})])
        turns = sessions.load_transcript(self.path)
        self.assertEqual([(t.role, t.text) for t in turns][-1], ("note", "You've hit your limit"))

    def test_sidechain_records_ignored(self):
        write(self.path, [user("u1", None, "main"),
                          user("x1", "u1", "subagent chatter", isSidechain=True)])
        self.assertEqual([t.text for t in sessions.load_transcript(self.path)], ["main"])


class LiveSessionTests(unittest.TestCase):
    def test_pid_and_start_time_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = sessions._proc_start(os.getpid())
            Path(tmp, "a.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "me",
                                                       "procStart": start}))
            Path(tmp, "b.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "reused",
                                                       "procStart": "1"}))
            Path(tmp, "c.json").write_text(json.dumps({"pid": 2 ** 22 + 7, "sessionId": "dead"}))
            Path(tmp, "d.json").write_text("{not json")
            live = sessions.live_sessions(tmp)
            self.assertEqual([i["sessionId"] for i in live.values()], ["me"])


class RenderTests(unittest.TestCase):
    def test_raw_html_is_escaped(self):
        out = render.md_to_html("hi <script>alert(1)</script> <b>x</b>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_list_after_paragraph_becomes_list(self):
        out = render.md_to_html("Steps:\n- one\n- two")
        self.assertIn("<ul>", out)

    def test_two_space_nested_list(self):
        out = render.md_to_html("- a\n  - b")
        self.assertEqual(out.count("<ul>"), 2)

    def test_indented_fence_renders_as_code(self):
        out = render.md_to_html("1. run:\n   ```bash\n   ls -la\n   ```\n2. done")
        self.assertIn("<pre", out)
        self.assertIn("<code>ls", out)  # highlighted, so spans split "ls -la"
        self.assertIn('<ol start="2">', out)

    def test_table(self):
        out = render.md_to_html("Result:\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", out)

    def test_user_text_is_escaped_and_paged(self):
        turns = [sessions.Turn("user", f"<i>msg {i}</i>") for i in range(5)]
        out = render.thread_html(turns, 2)
        self.assertIn("Show earlier messages (3)", out)
        self.assertIn("&lt;i&gt;msg 4&lt;/i&gt;", out)
        self.assertNotIn("msg 2", out)

    def test_single_tool_group_has_no_nested_row(self):
        tool = sessions.Tool("t1", "Bash", {"command": "ls", "description": "List"}, "a.txt")
        out = render.turn_html(sessions.Turn("assistant", blocks=[("tool", tool)]))
        self.assertNotIn('class="tool"', out)
        self.assertIn("$ ls", out)

    def test_mcp_tool_label(self):
        self.assertEqual(render.tool_label("mcp__claude_ai_Upwork__upwork__find_jobs"), "Upwork · find_jobs")
        self.assertEqual(render.tool_label("Bash"), "Bash")


if __name__ == "__main__":
    unittest.main()
