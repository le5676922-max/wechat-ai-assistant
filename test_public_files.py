import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_public_files as checker


class PublicFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.folder, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stage(self, name, content):
        (self.folder / name).write_text(content, encoding="utf-8")
        self.git("add", "--", name)

    def check(self):
        output = io.StringIO()
        with patch.object(checker, "ROOT", self.folder), contextlib.redirect_stdout(output):
            status = checker.main()
        return status, output.getvalue()

    def test_token_blocked_without_printing_its_value(self):
        token = "sk-" + "a" * 24
        self.stage("README.md", "temporary fixture " + token)
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn("README.md", output)
        self.assertNotIn(token, output)

    def test_private_configuration_blocked_even_when_added(self):
        self.stage("config.json", "{}")
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn("private file", output)

    def test_local_secret_with_nonstandard_format_blocked(self):
        token = "local" + "value" * 10
        (self.folder / ".env").write_text("CUSTOM_API_KEY=" + token, encoding="utf-8")
        self.stage("notes.md", "temporary fixture " + token)
        status, output = self.check()
        self.assertEqual(status, 1)
        self.assertIn("local .env secret", output)
        self.assertNotIn(token, output)

    def test_empty_template_allowed_and_only_staged_content_scanned(self):
        self.stage(".env.example", "DEEPSEEK_API_KEY=\n")
        self.stage("README.md", "public description")
        (self.folder / "README.md").write_text("sk-" + "b" * 24, encoding="utf-8")
        status, _ = self.check()
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
