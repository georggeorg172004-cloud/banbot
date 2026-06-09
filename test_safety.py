import ast
import unittest
from pathlib import Path


class SafetyTests(unittest.TestCase):
    def test_bot_cannot_call_telegram_ban_api(self):
        tree = ast.parse(Path("ban_bot.py").read_text())

        calls_ban_api = any(
            isinstance(node, ast.Attribute) and node.attr == "ban_chat_member"
            for node in ast.walk(tree)
        )

        self.assertFalse(
            calls_ban_api,
            "Production code must not call ban_chat_member in test-only mode",
        )
