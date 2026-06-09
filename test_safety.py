import asyncio
import ast
import importlib
import os
import sys
import types
import unittest
from pathlib import Path


os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")


class DummyFilter:
    def __getattr__(self, _name):
        return self

    def __eq__(self, _other):
        return self

    def __invert__(self):
        return self


class DummyDispatcher:
    def message(self, *_args):
        def decorator(func):
            return func

        return decorator

    async def start_polling(self, _bot):
        return None


class DummyBot:
    def __init__(self, token):
        self.token = token


aiogram_stub = types.ModuleType("aiogram")
aiogram_stub.Bot = DummyBot
aiogram_stub.Dispatcher = DummyDispatcher
aiogram_stub.F = DummyFilter()
sys.modules.setdefault("aiogram", aiogram_stub)

types_stub = types.ModuleType("aiogram.types")
types_stub.Message = object
types_stub.Document = object
sys.modules.setdefault("aiogram.types", types_stub)

import ban_bot


class FakeBot:
    def __init__(self):
        self.bans = []

    async def ban_chat_member(self, **kwargs):
        self.bans.append(kwargs)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.answers = []

    async def answer(self, text):
        self.answers.append(text)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(ban_bot)

    def test_parse_user_ids_keeps_unique_positive_ids_only(self):
        text = "123\n-100\n=456\nabc\n123\n0\n"

        self.assertEqual(ban_bot.parse_user_ids(text), [123, 456])

    def test_max_users_limit_blocks_oversized_operation(self):
        user_ids = [1, 2, 3]

        with self.assertRaisesRegex(ValueError, "Too many user_id"):
            ban_bot.validate_user_limit(user_ids, max_users=2)

    def test_dry_run_does_not_call_telegram_ban_api(self):
        fake_bot = FakeBot()
        ban_bot.KICK_ENABLED = False

        result = asyncio.run(
            ban_bot.kick_user(
                user_id=123,
                chat_id=-1001,
                bot_client=fake_bot,
                operation_id="op-test",
            )
        )

        self.assertFalse(result)
        self.assertEqual(fake_bot.bans, [])

    def test_confirm_does_not_execute_when_real_mode_is_disabled(self):
        fake_bot = FakeBot()
        operation = ban_bot.PendingOperation(
            operation_id="op-test",
            user_ids=[123],
            created_at=ban_bot.datetime.now(),
        )
        ban_bot.PENDING_OPERATIONS[operation.operation_id] = operation
        ban_bot.KICK_ENABLED = False
        message = FakeMessage("CONFIRM op-test 1")

        asyncio.run(ban_bot.confirm_operation(message, fake_bot))

        self.assertEqual(fake_bot.bans, [])
        self.assertEqual(
            message.answers,
            ["🧪 Реальный кик отключен: KICK_ENABLED не равен true."],
        )

    def test_confirm_rejects_wrong_user_count(self):
        fake_bot = FakeBot()
        operation = ban_bot.PendingOperation(
            operation_id="op-test",
            user_ids=[123, 456],
            created_at=ban_bot.datetime.now(),
        )
        ban_bot.PENDING_OPERATIONS[operation.operation_id] = operation
        ban_bot.KICK_ENABLED = True
        message = FakeMessage("CONFIRM op-test 1")

        asyncio.run(ban_bot.confirm_operation(message, fake_bot))

        self.assertEqual(fake_bot.bans, [])
        self.assertEqual(
            message.answers,
            ["❌ Количество пользователей в CONFIRM не совпадает с операцией."],
        )

    def test_real_mode_calls_telegram_ban_api_once(self):
        fake_bot = FakeBot()
        ban_bot.KICK_ENABLED = True

        result = asyncio.run(
            ban_bot.kick_user(
                user_id=123,
                chat_id=-1001,
                bot_client=fake_bot,
                operation_id="op-test",
            )
        )

        self.assertTrue(result)
        self.assertEqual(len(fake_bot.bans), 1)
        self.assertEqual(fake_bot.bans[0]["chat_id"], -1001)
        self.assertEqual(fake_bot.bans[0]["user_id"], 123)
        self.assertGreater(fake_bot.bans[0]["until_date"], int(ban_bot.datetime.now().timestamp()))

    def test_ban_api_call_is_confined_to_kick_user(self):
        tree = ast.parse(Path("ban_bot.py").read_text(encoding="utf-8"))
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        call_owners = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "ban_chat_member":
                current = node
                while current in parents and not isinstance(
                    current, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    current = parents[current]
                call_owners.append(current.name)

        self.assertEqual(call_owners, ["kick_user"])
