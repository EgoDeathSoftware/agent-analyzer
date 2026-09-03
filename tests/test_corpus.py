"""End-to-end tests: a synthetic corpus on disk, parsed on demand, through to reports.

These replace the old ingestion tests. Two of those covered cache mechanics that no longer
exist (a warm pass re-parsing nothing, a changed file being re-parsed); the rest covered
discovery, parsing, classification and CLI filtering, and are kept.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import cli, corpus, query
from tests import fixtures as fx


class CorpusTest(unittest.TestCase):
    """Parse a controlled corpus and query it through the CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "claude"
        self.home.mkdir()

        # An empty env file so the repo's real .env (which names another user's paths) is not
        # picked up and the only configured source is our temp one.
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_corpus(self) -> None:
        """A healthy session, a hook-blocked session, and a subagent."""
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        fx.write(self.home, [
            fx.user("search the repo"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "grep -r x ."})]),
            fx.tool_result("t1", "BLOCKED: Use rg (ripgrep) instead of grep", is_error=True),
            fx.assistant([fx.tool_use("t2", "Bash", {"command": "find . -name x"})],
                         ts="2026-08-01T00:00:05Z"),
            fx.tool_result("t2", "BLOCKED: Use fd instead of find", is_error=True,
                           ts="2026-08-01T00:00:06Z"),
        ], name="bbbb2222.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="cccc3333")

    def _load(self) -> corpus.Corpus:
        return corpus.load()

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_load_finds_every_file_including_subagents(self) -> None:
        self._write_corpus()
        loaded = self._load()
        self.assertEqual(len(loaded.sessions), 3,
                         "the nested subagent transcript must be found")
        self.assertEqual(loaded.failed, 0)

    def test_unreachable_sources_are_reported_not_hidden(self) -> None:
        self._write_corpus()
        with mock.patch.dict(os.environ, {"CLAUDE_DIR_WSL": "/nonexistent/elsewhere"}):
            loaded = self._load()
        self.assertTrue(any("wsl" in note for note in loaded.unreachable))

    def test_colliding_session_ids_keep_separate_entries(self) -> None:
        """Two transcripts sharing a sessionId must stay distinguishable."""
        self._write_corpus()
        tops = [e for e in self._load().sessions if not e.session.is_subagent]
        self.assertEqual(len(tops), 2)
        self.assertEqual(len({e.session.sid for e in tops}), 2, "ids must be disambiguated")
        self.assertEqual(len({e.session.path for e in tops}), 2)

    def test_disambiguation_is_stable_across_runs(self) -> None:
        """A suffixed id must not move between runs, or `sk show <id>` breaks."""
        self._write_corpus()
        first = sorted(e.session.sid for e in self._load().sessions)
        second = sorted(e.session.sid for e in self._load().sessions)
        self.assertEqual(first, second)

    def test_subagent_gets_its_own_entry_and_parent_link(self) -> None:
        self._write_corpus()
        sub = next(e for e in self._load().sessions if e.session.is_subagent)
        self.assertEqual(sub.session.sid, "cccc3333")
        self.assertEqual(sub.session.parent_sid, fx.SID)
        self.assertEqual(sub.session.agent_type, "Explore")

    def test_sessions_share_a_project_key(self) -> None:
        self._write_corpus()
        self.assertEqual({e.project_key for e in self._load().sessions}, {"myproject"})

    def test_derived_fields_are_populated(self) -> None:
        """parse_file alone leaves these unset; the load pipeline must fill them in."""
        self._write_corpus()
        blocked = next(e for e in self._load().sessions
                       if any(t.err_class == "hook-block" for t in e.session.tools))
        self.assertNotEqual(blocked.session.end_state, "unknown")
        self.assertTrue(all(t.err_class for t in blocked.session.tools if t.is_error))

    def test_index_excludes_subagents_by_default(self) -> None:
        self._write_corpus()
        loaded = self._load()
        self.assertNotIn("cccc3333", cli.cmd_index(loaded, self._args("index")))
        self.assertIn("cccc3333",
                      cli.cmd_index(loaded, self._args("index", "--subagents", "only")))

    def test_errors_report_ranks_hook_blocks_first(self) -> None:
        self._write_corpus()
        report = cli.cmd_errors(self._load(), self._args("errors", "--subagents", "include"))
        self.assertIn("hook-block", report)
        self.assertIn("Promote the rule into CLAUDE.md", report)

    def test_errors_json_mode_is_machine_readable(self) -> None:
        self._write_corpus()
        doc = json.loads(cli.cmd_errors(self._load(), self._args("errors", "--json")))
        self.assertEqual(doc["failures"], 2)
        self.assertEqual(doc["clusters"][0]["bucket"], "hook-block")

    def test_doctor_reports_effective_retention(self) -> None:
        (self.home / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 365}),
                                                  encoding="utf-8")
        self._write_corpus()
        report = cli.cmd_doctor(self._load(), self._args("doctor"))
        self.assertIn("365", report)

    def test_doctor_names_the_sources_it_cannot_see(self) -> None:
        self._write_corpus()
        with mock.patch.dict(os.environ, {"CLAUDE_DIR_WINDOWS": "/nope"}):
            report = cli.cmd_doctor(self._load(), self._args("doctor"))
        self.assertIn("windows", report)
        self.assertIn("Not visible from this process", report)

    def test_show_rejects_unknown_session(self) -> None:
        with self.assertRaises(SystemExit):
            cli.cmd_show(self._load(), self._args("show", "nope"))

    def test_show_resolves_a_session_by_prefix(self) -> None:
        self._write_corpus()
        loaded = self._load()
        sid = next(e.session.sid for e in loaded.sessions if e.session.is_subagent)
        self.assertIn(sid, cli.cmd_show(loaded, self._args("show", sid[:4])))

    def test_show_falls_back_when_the_id_is_not_the_filename(self) -> None:
        """The fixture's two top-level files both record fx.SID, so neither filename matches.

        The filename fast path must decline rather than miss the session — this is the case
        that would silently report "no session matching" if the fallback were dropped.
        """
        self._write_corpus()
        self.assertIsNone(corpus.load_session(fx.SID[:8]),
                          "fast path must not claim a hit it cannot confirm")
        out = cli.cmd_show(self._load(), self._args("show", fx.SID[:8]))
        self.assertIn(fx.SID[:8], out)

    def test_show_fast_path_resolves_a_subagent_by_filename(self) -> None:
        """Subagent files are named agent-<id>.jsonl but the id is just <id>."""
        self._write_corpus()
        entry = corpus.load_session("cccc3333")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.session.sid, "cccc3333")

    def test_show_fast_path_and_full_load_agree(self) -> None:
        self._write_corpus()
        fast = corpus.load_session("cccc3333")
        slow = query.find_session(self._load(), "cccc3333")
        self.assertEqual(fast.session.sid, slow.session.sid)
        self.assertEqual(fast.project_key, slow.project_key)
        self.assertEqual(len(fast.session.tools), len(slow.session.tools))

    def test_empty_corpus_does_not_crash(self) -> None:
        loaded = self._load()
        self.assertEqual(len(loaded.sessions), 0)
        self.assertIn("sessions=0", cli.cmd_index(loaded, self._args("index")))
        self.assertIn("sessions", cli.cmd_doctor(loaded, self._args("doctor")))


class QueryTest(unittest.TestCase):
    """The aggregation layer that replaced the SQL, tested directly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        home = tmp / "claude"
        home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Two tools failing the same number of times, deliberately written in an order that
        # is neither alphabetical nor reverse-alphabetical.
        fx.write(home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("z1", "Zebra", {"a": 1})]),
            fx.tool_result("z1", "BLOCKED: nope", is_error=True),
            fx.assistant([fx.tool_use("a1", "Alpha", {"a": 1})]),
            fx.tool_result("a1", "BLOCKED: nope", is_error=True),
        ], name="dddd4444.jsonl")
        self.corpus = corpus.load()
        self.scope = query.Filter(subagents="include")

    def test_equal_counts_break_ties_alphabetically(self) -> None:
        """Tie order must be total, or two runs over one corpus can disagree."""
        rows = query.clusters(self.corpus, self.scope, "tool")
        self.assertEqual([r["bucket"] for r in rows], ["Alpha", "Zebra"])

    def test_cluster_counts_distinct_sessions_not_calls(self) -> None:
        rows = query.clusters(self.corpus, self.scope, "class")
        hook = next(r for r in rows if r["bucket"] == "hook-block")
        self.assertEqual(hook["n"], 2, "two failing calls")
        self.assertEqual(hook["sessions"], 1, "in one session")

    def test_exemplar_is_lexicographically_smallest(self) -> None:
        """Reproduces MIN(output_preview); a first-seen exemplar would shift reports."""
        rows = query.clusters(self.corpus, self.scope, "class")
        previews = [t.output_preview for e in self.corpus.sessions
                    for t in e.session.tools if query.is_failure(t)]
        self.assertEqual(rows[0]["exemplar"], min(previews))

    def test_a_call_with_no_result_counts_as_a_failure(self) -> None:
        """An interrupted call sets no is_error flag but is still a failure."""
        from sessionkit.parse import ToolCall
        self.assertTrue(query.is_failure(ToolCall("x", 1, "Bash", "", err_class="no-result")))
        self.assertFalse(query.is_failure(ToolCall("x", 1, "Bash", "")))

    def test_filter_scopes_by_subagents(self) -> None:
        self.assertEqual(len(query.Filter(subagents="only").apply(self.corpus)), 0)
        self.assertEqual(len(query.Filter(subagents="exclude").apply(self.corpus)), 1)


class CommandsTest(unittest.TestCase):
    """query.command_rows and `sk commands`: reviewing what actually ran."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self) -> None:
        # Top-level session runs `ls` twice (same normalised command).
        fx.write(self.home, [
            fx.user("look around"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "ls /home/dev/one"})]),
            fx.tool_result("t1", "one"),
            fx.assistant([fx.tool_use("t2", "Bash", {"command": "ls /home/dev/two"})],
                         ts="2026-08-01T00:00:05Z"),
            fx.tool_result("t2", "two", ts="2026-08-01T00:00:06Z"),
        ], name="aaaa1111.jsonl")
        # A subagent dispatched by that session runs a Grep call that fails.
        fx.write_subagent(self.home, [
            fx.user("explore"),
            fx.assistant([fx.tool_use("s1", "Grep", {"pattern": "TODO"})]),
            fx.tool_result("s1", "not found", is_error=True),
        ], agent_id="eeee5555", agent_type="Explore")

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_group_by_command_dedupes_normalised_calls(self) -> None:
        self._write()
        rows = query.command_rows(corpus.load(), query.Filter(subagents="exclude"), "command")
        top = next(r for r in rows if r["tool"] == "Bash")
        self.assertEqual(top["n"], 2, "both `ls <path>` calls collapse to one signature")

    def test_group_by_tool(self) -> None:
        self._write()
        rows = query.command_rows(corpus.load(), query.Filter(subagents="include"), "tool")
        self.assertEqual({r["bucket"] for r in rows}, {"Bash", "Grep"})

    def test_group_by_agent_only_includes_subagent_calls(self) -> None:
        """The parent's own direct calls have no agentId and must not appear as a pseudo-agent."""
        self._write()
        rows = query.command_rows(corpus.load(), query.Filter(subagents="include"), "agent")
        self.assertEqual(len(rows), 1)
        agent_row = rows[0]
        self.assertEqual(agent_row["bucket"], "eeee5555")
        self.assertEqual(agent_row["n"], 1)
        self.assertEqual(agent_row["agent_type"], "Explore")

    def test_group_by_session_rolls_subagent_calls_up_to_parent(self) -> None:
        self._write()
        rows = query.command_rows(corpus.load(), query.Filter(subagents="include"), "session")
        self.assertEqual(len(rows), 1, "the subagent's calls attribute to its parent session")
        self.assertEqual(rows[0]["bucket"], fx.SID)
        self.assertEqual(rows[0]["n"], 3, "2 parent calls + 1 subagent call")

    def test_agent_type_filter_scopes_to_matching_subagents(self) -> None:
        self._write()
        rows = query.command_rows(corpus.load(), query.Filter(subagents="include"), "agent",
                                  agent_type="Explore")
        self.assertEqual(len(rows), 1)
        rows_none = query.command_rows(corpus.load(), query.Filter(subagents="include"), "agent",
                                       agent_type="general-purpose")
        self.assertEqual(rows_none, [])

    def test_cli_reports_errors_and_avg_duration(self) -> None:
        self._write()
        out = cli.cmd_commands(corpus.load(), self._args("commands", "--subagents", "include",
                                                          "--group-by", "tool"))
        self.assertIn("Grep", out)
        self.assertIn("1", out)  # one Grep error

    def test_cli_no_subagents_at_all_says_so(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        out = cli.cmd_commands(corpus.load(), self._args("commands", "--group-by", "agent"))
        self.assertIn("no subagent", out.lower())
        self.assertNotIn(fx.SID, out, "must not silently fall back to the parent's own calls")

    def test_cli_full_flag_disables_cell_truncation(self) -> None:
        long_path = "/home/dev/" + "x" * 250
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": f"cat {long_path}"})]),
            fx.tool_result("t1", "ok"),
        ], name="aaaa1111.jsonl")
        capped = cli.cmd_commands(corpus.load(), self._args("commands"))
        full = cli.cmd_commands(corpus.load(), self._args("commands", "--full"))
        self.assertNotIn(long_path, capped)
        self.assertIn(long_path, full)


class HooksTest(unittest.TestCase):
    """query.hook_rows / query.deny_rows and `sk hooks`: attributing failures to config."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_settings(self, data: dict) -> None:
        (self.home / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def _write_session(self, command: str, message: str) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": command})]),
            fx.tool_result("t1", message, is_error=True),
        ], name="aaaa1111.jsonl")

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_hook_block_attributed_to_the_specific_rule(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "echo 'BLOCKED: use rg' >&2; exit 2"},
            {"type": "command", "command": "echo 'BLOCKED: use fd' >&2; exit 2"},
        ]}]}})
        self._write_session("grep -r x .", "PreToolUse:Bash hook error: BLOCKED: use rg")
        rows = query.hook_rows(corpus.load(), query.Filter(subagents="include"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "BLOCKED: use rg")
        self.assertEqual(rows[0]["n"], 1)

    def test_hook_block_with_no_matching_rule_is_unattributed(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "echo 'BLOCKED: something else' >&2; exit 2"},
        ]}]}})
        self._write_session("grep -r x .", "PreToolUse:Bash hook error: BLOCKED: totally novel")
        rows = query.hook_rows(corpus.load(), query.Filter(subagents="include"))
        self.assertEqual(rows[0]["event"], "unattributed")

    def test_deny_rule_confirmed_by_matching_input(self) -> None:
        self._write_settings({"permissions": {"deny": ["Bash(git push --force*)"]}})
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "git push --force origin main"})]),
            fx.tool_result("t1", "Permission to use Bash with command git push --force origin "
                                 "main has been denied.", is_error=True),
        ], name="aaaa1111.jsonl")
        rows = query.deny_rows(corpus.load(), query.Filter(subagents="include"))
        confirmed = next(r for r in rows if r["confirmed"])
        self.assertEqual(confirmed["pattern"], "git push --force*")

    def test_deny_rule_unconfirmed_when_pattern_does_not_match(self) -> None:
        self._write_settings({"permissions": {"deny": ["Bash(rm -rf *)"]}})
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": "sudo something"})]),
            fx.tool_result("t1", "The user doesn't want to proceed with this tool use.",
                           is_error=True),
        ], name="aaaa1111.jsonl")
        rows = query.deny_rows(corpus.load(), query.Filter(subagents="include"))
        self.assertFalse(rows[0]["confirmed"])

    def test_deny_rule_with_no_deny_config_for_the_tool_at_all(self) -> None:
        self._write_settings({"permissions": {"deny": []}})
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "WebFetch", {"url": "http://x"})]),
            fx.tool_result("t1", "The user doesn't want to proceed with this tool use.",
                           is_error=True),
        ], name="aaaa1111.jsonl")
        rows = query.deny_rows(corpus.load(), query.Filter(subagents="include"))
        self.assertEqual(rows[0]["pattern"], "")
        self.assertFalse(rows[0]["confirmed"])

    def test_cli_hooks_report_shows_attribution(self) -> None:
        self._write_settings({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "echo 'BLOCKED: use rg' >&2; exit 2"},
        ]}]}, "permissions": {"deny": ["Bash(rm -rf *)"]}})
        self._write_session("grep -r x .", "PreToolUse:Bash hook error: BLOCKED: use rg")
        out = cli.cmd_hooks(corpus.load(), self._args("hooks", "--subagents", "include"))
        self.assertIn("BLOCKED: use rg", out)

    def test_cli_hooks_report_on_clean_corpus(self) -> None:
        self._write_settings({})
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        out = cli.cmd_hooks(corpus.load(), self._args("hooks"))
        self.assertIn("(none)", out)


class ForensicsTest(unittest.TestCase):
    """query.forensics_* and `sk forensics`: findings, anchored timeline, health."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_looping_session(self) -> None:
        """A session that calls the same Bash command three times running (repeat-tool)."""
        records = [fx.user("do the thing")]
        for i in range(3):
            records.append(fx.assistant(
                [fx.tool_use(f"t{i}", "Bash", {"command": "flaky-check"})],
                ts=f"2026-08-01T00:00:0{i}Z"))
            records.append(fx.tool_result(f"t{i}", "still not ready",
                                          ts=f"2026-08-01T00:00:0{i}Z"))
        records.append(fx.assistant([{"type": "text", "text": "gave up"}],
                                    ts="2026-08-01T00:00:09Z"))
        fx.write(self.home, records, name="aaaa1111.jsonl")

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_findings_include_the_prevention_hint(self) -> None:
        self._write_looping_session()
        entry = next(iter(corpus.load().sessions))
        findings = query.forensics_findings(entry)
        repeat = next(f for f in findings if f["kind"] == "repeat-tool")
        self.assertTrue(repeat["hint"])
        self.assertIn("identical input", repeat["hint"])

    def test_timeline_is_anchored_to_findings_only(self) -> None:
        self._write_looping_session()
        entry = next(iter(corpus.load().sessions))
        anchored = query.forensics_timeline(entry)
        full = query.timeline_rows(entry)
        self.assertLess(len(anchored), len(full),
                        "anchored timeline must be a strict subset on a session with unrelated "
                        "lines (the first user turn, the final text reply)")
        anchored_lines = {r["line"] for r in anchored}
        finding_lines = {ln for a in entry.anomalies for ln in a.lines}
        self.assertEqual(anchored_lines, finding_lines & {r["line"] for r in full})

    def test_health_counts_successes_and_failures(self) -> None:
        self._write_looping_session()
        entry = next(iter(corpus.load().sessions))
        health = query.forensics_health(entry)
        self.assertEqual(health["tool_calls"], 3)
        self.assertEqual(health["failed"], 0)
        self.assertEqual(health["succeeded"], 3)

    def test_cli_report_names_the_finding_and_a_line(self) -> None:
        self._write_looping_session()
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        out = cli.cmd_forensics(loaded, self._args("forensics", sid))
        self.assertIn("repeat-tool", out)
        self.assertIn("Health", out)

    def test_cli_rejects_unknown_session(self) -> None:
        with self.assertRaises(SystemExit):
            cli.cmd_forensics(corpus.load(), self._args("forensics", "nope"))

    def test_clean_session_has_no_findings_but_reports_health(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        out = cli.cmd_forensics(loaded, self._args("forensics", sid))
        self.assertIn("(none)", out)
        self.assertIn("succeeded", out)


class ShowSkillsTest(unittest.TestCase):
    """query.skill_rows and the Skills section of `sk show --mode summary`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def _write(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("s1", "Skill", {"skill": "superpowers:brainstorming"})]),
            fx.tool_result("s1", "Launching skill: superpowers:brainstorming"),
            fx.assistant([fx.tool_use("s2", "Skill", {"skill": "superpowers:brainstorming"})],
                         ts="2026-08-01T00:00:05Z"),
            fx.tool_result("s2", "Launching skill: superpowers:brainstorming",
                           ts="2026-08-01T00:00:06Z"),
            fx.assistant([fx.tool_use("s3", "Skill", {"skill": "artifact-design"})],
                         ts="2026-08-01T00:00:07Z"),
            fx.tool_result("s3", "not found", is_error=True, ts="2026-08-01T00:00:08Z"),
        ], name="aaaa1111.jsonl")

    def test_skill_rows_counts_invocations_per_skill(self) -> None:
        self._write()
        entry = next(iter(corpus.load().sessions))
        rows = query.skill_rows(entry)
        brainstorm = next(r for r in rows if r["skill"] == "superpowers:brainstorming")
        self.assertEqual(brainstorm["n"], 2)
        self.assertEqual(brainstorm["errs"], 0)
        design = next(r for r in rows if r["skill"] == "artifact-design")
        self.assertEqual(design["n"], 1)
        self.assertEqual(design["errs"], 1)

    def test_skill_rows_empty_when_no_skills_used(self) -> None:
        fx.write(self.home, fx.simple_session(), name="bbbb2222.jsonl")
        entry = next(iter(corpus.load().sessions))
        self.assertEqual(query.skill_rows(entry), [])

    def test_show_summary_includes_skills_section(self) -> None:
        self._write()
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        out = cli.cmd_show(loaded, self._args("show", sid))
        self.assertIn("Skills", out)
        self.assertIn("superpowers:brainstorming", out)
        self.assertIn("artifact-design", out)

    def test_show_summary_skills_section_present_but_empty_when_none_used(self) -> None:
        fx.write(self.home, fx.simple_session(), name="bbbb2222.jsonl")
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        out = cli.cmd_show(loaded, self._args("show", sid))
        self.assertIn("Skills", out)


class ChildrenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_resolves_by_the_exact_join_not_dispatch_order(self) -> None:
        # Two dispatches; their notifications arrive in the OPPOSITE order, which would silently
        # mispair a positional/timestamp resolver (FIRSTRUN.md §8) but must not fool the real one.
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([
                fx.tool_use("d1", "Agent", {"description": "Implement Task 1"}),
                fx.tool_use("d2", "Agent", {"description": "Implement Task 2"}),
            ]),
            fx.task_notification("d2", "childtwo01"),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01",
                          agent_type="general-purpose")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childtwo01",
                          agent_type="general-purpose")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        by_line = {r["description"]: r["child_sid"] for r in rows}
        self.assertEqual(by_line["Implement Task 1"][:10], "childone01")
        self.assertEqual(by_line["Implement Task 2"][:10], "childtwo01")

    def test_unresolved_dispatch_is_reported_not_guessed(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Implement Task 1"})]),
        ], name="aaaa1111.jsonl")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["resolved"])
        self.assertEqual(rows[0]["child_sid"], "")

        out = cli.cmd_children(corp, self._args("children", parent.session.sid))
        self.assertIn("1 dispatch(es)", out)
        self.assertIn("unresolved", out)

    def test_duplicate_task_notifications_do_not_double_count(self) -> None:
        # A single task can notify twice (progress + completion, per FIRSTRUN.md §8's
        # `a3c7bebc` example). Duplicate (tool_use_id, task_id) edges must collapse to one row.
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Implement Task 1"})]),
            fx.task_notification("d1", "childone01"),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01",
                          agent_type="general-purpose")
        corp = corpus.load()
        parent = next(e for e in corp.sessions if not e.session.is_subagent)
        rows = query.children_rows(parent, corp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["child_sid"][:10], "childone01")
        self.assertTrue(rows[0]["resolved"])


class CostTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_corpus_wide_rollup(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        out = cli.cmd_cost(corp, self._args("cost"))
        self.assertIn(fx.SID[:8], out)

    def test_session_scoped_shows_tool_breakdown(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        out = cli.cmd_cost(corp, self._args("cost", fx.SID[:8]))
        self.assertIn("Cost by tool", out)
        self.assertIn("Read", out)

    def test_unknown_session_exits(self) -> None:
        corp = corpus.load()
        with self.assertRaises(SystemExit):
            cli.cmd_cost(corp, self._args("cost", "no-such-session"))

    def test_bloat_flag_adds_bloat_sections(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        out = cli.cmd_cost(corp, self._args("cost", "--bloat"))
        self.assertIn("Bloat", out)

    def test_subagents_flag_states_sample_size(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Do the thing"})]),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01")
        corp = corpus.load()
        out = cli.cmd_cost(corp, self._args("cost", "--subagents"))
        self.assertIn("dispatch", out.lower())

    def test_corpus_rollup_states_top_level_vs_subagent_split(self) -> None:
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("d1", "Agent", {"description": "Do the thing"})]),
            fx.task_notification("d1", "childone01"),
        ], name="aaaa1111.jsonl")
        fx.write_subagent(self.home, fx.simple_session(), agent_id="childone01")
        corp = corpus.load()
        out = cli.cmd_cost(corp, self._args("cost", "--json"))
        payload = json.loads(out)
        self.assertEqual(payload["top_level"], 1)
        self.assertEqual(payload["subagent"], 1)
        kinds = {row["kind"] for row in payload["sessions"]}
        self.assertEqual(kinds, {"top-level", "subagent"})

    def test_json_never_truncates_and_both_flags_work_before_and_after_subcommand(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        corp = corpus.load()
        out_after = cli.cmd_cost(corp, self._args("cost", "--json", "--bloat"))
        out_before = cli.cmd_cost(corp, self._args("--json", "cost", "--bloat"))
        self.assertEqual(out_after, out_before)


class SinceTest(unittest.TestCase):
    """--since parsing must fail loudly rather than silently ignoring a bad window."""

    def test_valid_windows(self) -> None:
        for value in ("7d", "12h", "2w", " 3d "):
            self.assertTrue(cli.since_cutoff(value))

    def test_absent_window_means_no_cutoff(self) -> None:
        self.assertEqual(cli.since_cutoff(None), "")

    def test_bad_window_raises(self) -> None:
        with self.assertRaises(SystemExit):
            cli.since_cutoff("last tuesday")

    def test_absolute_date(self) -> None:
        self.assertEqual(cli.since_cutoff("2026-08-17"), "2026-08-17T00:00:00Z")

    def test_absolute_datetime(self) -> None:
        self.assertEqual(cli.since_cutoff("2026-08-17T14:30"), "2026-08-17T14:30:00Z")

    def test_calendar_invalid_date_raises(self) -> None:
        with self.assertRaises(SystemExit):
            cli.since_cutoff("2026-13-45")


class IndexColumnsTest(unittest.TestCase):
    """`sk index` surfaces lineage (parent_sid/agent_type) for subagent rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "claude"
        self.home.mkdir()
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home),
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_index_reports_parent_sid_and_agent_type_for_subagents(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        fx.write_subagent(self.home, [
            fx.user("explore"),
            fx.assistant([fx.tool_use("s1", "Grep", {"pattern": "TODO"})]),
            fx.tool_result("s1", "not found"),
        ], agent_id="eeee5555", agent_type="Explore")
        rows = query.index_rows(corpus.load(), query.Filter(subagents="only"))
        self.assertEqual(rows[0]["parent_sid"], fx.SID)
        self.assertEqual(rows[0]["agent_type"], "Explore")

        args = cli.build_parser().parse_args(["index", "--subagents", "only"])
        out = cli.cmd_index(corpus.load(), args)
        self.assertIn("parent_sid", out)
        self.assertIn("Explore", out)

    def test_label_contains_filters_case_insensitively(self) -> None:
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        # simple_session()'s first prompt is "add a feature"
        matches = query.index_rows(corpus.load(), query.Filter(label_contains="FEATURE"))
        self.assertEqual(len(matches), 1)
        no_matches = query.index_rows(corpus.load(), query.Filter(label_contains="nonexistent"))
        self.assertEqual(no_matches, [])

    def test_index_json_always_carries_lineage_keys_even_with_subagents_excluded(self) -> None:
        """--subagents exclude is the CLI default; JSON callers must not lose the fields."""
        fx.write(self.home, fx.simple_session(), name="aaaa1111.jsonl")
        args = cli.build_parser().parse_args(["index", "--json"])
        self.assertEqual(args.subagents, "exclude")
        out = json.loads(cli.cmd_index(corpus.load(), args))
        self.assertIn("parent_sid", out["sessions"][0])
        self.assertIn("agent_type", out["sessions"][0])


class ShowFullTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.home = tmp / "claude"
        self.home.mkdir()
        (tmp / "empty.env").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.home), "SESSIONKIT_ENV": str(tmp / "empty.env")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, *argv: str):
        return cli.build_parser().parse_args(list(argv))

    def test_full_recovers_a_tool_input_past_the_preview_cap(self) -> None:
        long_arg = "x" * 3000  # past INPUT_PREVIEW=2000
        fx.write(self.home, [
            fx.user("go"),
            fx.assistant([fx.tool_use("t1", "Bash", {"command": long_arg})]),
            fx.tool_result("t1", "ok"),
        ], name="aaaa1111.jsonl")
        loaded = corpus.load()
        sid = loaded.sessions[0].session.sid
        capped = cli.cmd_show(loaded, self._args("show", sid, "--mode", "tools"))
        full = cli.cmd_show(loaded, self._args("show", sid, "--mode", "tools", "--full"))
        self.assertNotIn(long_arg, capped)
        self.assertIn(long_arg, full)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
