"""Tests for pi source discovery."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sessionkit import sources as src


class PiSourceTest(unittest.TestCase):
    """Discovering the pi session store as a third Source kind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env_file = self.tmp / "empty.env"
        env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "CLAUDE_DIR": str(self.tmp / "claude"),  # left absent: unreachable, on purpose
            "SESSIONKIT_ENV": str(env_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pi_source_unreachable_without_sessions_dir(self) -> None:
        pi_home = self.tmp / "pi-agent"
        pi_home.mkdir()
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            found = {s.id: s for s in src.discover()}
        self.assertIn("pi", found)
        self.assertEqual(found["pi"].kind, "pi")
        self.assertFalse(found["pi"].reachable)
        self.assertEqual(found["pi"].note, "no sessions/ subdirectory")

    def test_pi_source_reachable_with_sessions_dir(self) -> None:
        pi_home = self.tmp / "pi-agent"
        (pi_home / "sessions").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            found = {s.id: s for s in src.discover()}
        self.assertTrue(found["pi"].reachable)
        self.assertEqual(found["pi"].note, "")

    def test_pi_source_is_scannable_when_reachable(self) -> None:
        pi_home = self.tmp / "pi-agent"
        (pi_home / "sessions").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PI_DIR": str(pi_home)}):
            scannable_ids = {s.id for s in src.scannable(src.discover())}
        self.assertIn("pi", scannable_ids)

    def test_pi_source_defaults_to_home_pi_agent_without_env_var(self) -> None:
        # No PI_DIR set at all: discover() must still produce a "pi" candidate (unreachable is
        # fine, since ~/.pi/agent/sessions is very unlikely to exist under a temp HOME), proving
        # pi is probed by default like the Claude Code "host" source, not opt-in like opencode.
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=False):
            found = {s.id: s for s in src.discover()}
        self.assertIn("pi", found)
