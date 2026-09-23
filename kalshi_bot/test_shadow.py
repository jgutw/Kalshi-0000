"""Synthetic Shadow boundary tests; no config, credentials, feeds or bot imports."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

from .shadow import ShadowSession, ShadowClient, shadow_boundary, reject_live_start_in_shadow


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "shadow_data"

    def session(self, **kwargs):
        params = dict(root=self.root, shadow_session_id="test-1", session_tag="shadow-era-1",
                      code_sha="a" * 40)
        params.update(kwargs)
        session = ShadowSession(**params)
        self.addCleanup(session.close)
        return session

    def order(self, client, count=2, **kwargs):
        return client.place_market_order("BTC", "yes", count, asset="BTC", window_id_ts=900,
                                        decision_id="decision-1", strategy="test", **kwargs)

    def test_intent_never_network_or_fill(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            client = ShadowClient(self.session(), markets={"BTC": {"ticker": "BTC"}})
            result = self.order(client, limit_price=.4)
            self.assertFalse(result)
            self.assertFalse(result.ok)
            self.assertEqual(result.fill_count, 0)
            row = json.loads((self.root / "test-1" / "lifecycle.jsonl").read_text())
            self.assertEqual((row["mode"], row["payload"]["request"]["count"]), ("SHADOW", 2))
            self.assertEqual(client.get_market("BTC"), {"ticker": "BTC"})
            for name in ("_post", "request", "cancel_order", "amend_order", "batch_orders",
                         "intra_transfer_shards", "ensure_crypto_shard_funded"):
                with self.subTest(name=name), self.assertRaises(AttributeError):
                    getattr(client, name)()

    def test_startup_flags_and_tag(self):
        for args in ({"live": True}, {"fresh_round": True},
                     {"session_tag": "p6c_d1_validation"}, {"shadow_session_id": "../escape"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.session(**args)
        self.assertFalse(self.root.exists())

    def test_protected_roots(self):
        for parent in ("logs", "sessions", "research_data", "p6c_d1_validation"):
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                self.session(root=Path(self.temp.name) / parent / "shadow_data")
        with self.assertRaises(ValueError):
            self.session(root=Path(self.temp.name))

    def test_identity_and_no_overwrite(self):
        session = self.session(config_identity={"b": 2, "a": 1})
        self.assertEqual(session.metadata["mode"], "SHADOW")
        self.assertEqual(len(session.metadata["config_sha256"]), 64)
        self.assertTrue(session.metadata["startup_utc"].endswith("+00:00"))
        with self.assertRaises(FileExistsError):
            self.session()
        with self.assertRaises(ValueError):
            session.append("../logs", {})

    def test_symlink_root(self):
        target = Path(self.temp.name) / "target"
        target.mkdir()
        try:
            self.root.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlinks")
        with self.assertRaises(ValueError):
            self.session()

    def test_no_transport_injection(self):
        with self.assertRaises(TypeError):
            ShadowClient(self.session(), markets={"client": Mock()})

    def test_link_guard_without_host_privileges(self):
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(ValueError):
            self.session()
        self.assertFalse(self.root.exists())

    def test_existing_paper_client_contract(self):
        # Real client method, inert config; constructing no authenticated client.
        config = types.ModuleType("kalshi_bot.config")
        config.cfg = types.SimpleNamespace(DRY_RUN=True)
        api_config = types.ModuleType("kalshi_bot.api_config")
        api_config.api_cfg = types.SimpleNamespace()
        name = "kalshi_bot._shadow_client_regression"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("kalshi_client.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"kalshi_bot.config": config, "kalshi_bot.api_config": api_config, name: module}):
            spec.loader.exec_module(module)
            client = object.__new__(module.KalshiClient)
            client._post = Mock(side_effect=AssertionError("network write"))
            result = client.place_market_order("TEST", "yes", 2, limit_price=.4)
            self.assertTrue(result)
            self.assertEqual((result.fill_count, result.entry_price), (2, .4))
            client._post.assert_not_called()

    def test_invalid_intent_no_artifact(self):
        client = ShadowClient(self.session())
        for count in (0, -1, True):
            with self.assertRaises(ValueError):
                self.order(client, count=count)
        with self.assertRaises(ValueError):
            self.order(client, count=1, limit_price=float("nan"))
        self.assertEqual((self.root / "test-1" / "lifecycle.jsonl").read_bytes(), b"")

    def test_actual_live_start_guard_before_dependencies(self):
        # Import the real helper with inert dependencies: no config/.env import.
        modules = {}
        for name, attrs in {
            "config": {"cfg": Mock()},
            "kalshi_client": {"KalshiClient": Mock(side_effect=AssertionError("live client"))},
            "runtime_control": {"PROFILE_PRESETS": {}, "prepare_for_new_round": Mock(),
                                "trading_bot_running": Mock()},
            "session_meta": {"PROJECT_ROOT": Path(self.temp.name), "archive_and_log_round": Mock(),
                             "session_is_active": Mock()},
        }.items():
            module = types.ModuleType("kalshi_bot." + name)
            module.__dict__.update(attrs)
            modules[module.__name__] = module
        spec = importlib.util.spec_from_file_location("kalshi_bot._shadow_safe_live_test",
                                                      Path(__file__).with_name("safe_live.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", modules):
            spec.loader.exec_module(module)
        module._rounds = Mock(side_effect=LookupError("normal entry reached"))
        with shadow_boundary(), self.assertRaises(RuntimeError):
            module.start_live_safe()
        module._rounds.assert_not_called()
        # Outside Shadow the original entry path is still reached.
        with self.assertRaises(LookupError):
            module.start_live_safe()
        reject_live_start_in_shadow()


if __name__ == "__main__":
    unittest.main()
