"""Offline pause dependency tests; never consult production runtime files."""
import builtins
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

from .asset_engine import AssetEngine
from .config import ASSETS


class PauseProviderTests(unittest.TestCase):
    def engine(self, **kwargs):
        sim = Mock()
        sim.is_halted.return_value = (False, '')
        sim.activity_idle.return_value = False
        engine = AssetEngine(ASSETS[0], object(), sim, **kwargs)
        engine.signal.is_ready = Mock(return_value=False)
        return engine

    def runtime(self, provider):
        module = types.ModuleType('kalshi_bot.runtime_control')
        module.entries_paused = provider
        return patch.dict('sys.modules', {'kalshi_bot.runtime_control': module})

    def test_default_provider_true_before_other_gates(self):
        provider = Mock(return_value=True)
        engine = self.engine()
        with self.runtime(provider):
            decision = engine.make_decision(.5)
        self.assertEqual((decision['action'], decision['reason']), ('WAIT', 'telegram_paused'))
        provider.assert_called_once_with()
        engine.sim.is_halted.assert_not_called()
        engine.signal.is_ready.assert_not_called()

    def test_default_provider_false_reaches_warmup(self):
        provider = Mock(return_value=False)
        engine = self.engine()
        with self.runtime(provider):
            self.assertEqual(engine.make_decision(.5)['reason'], 'signal_warmup')
        provider.assert_called_once_with()

    def test_default_provider_exception_blocks_new_risk(self):
        provider = Mock(side_effect=OSError('synthetic unreadable state'))
        engine = self.engine()
        with self.runtime(provider):
            self.assertEqual(engine.make_decision(.5)['reason'], 'telegram_paused')
        provider.assert_called_once_with()

    def test_default_import_exception_blocks_new_risk(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name in ('runtime_control', 'kalshi_bot.runtime_control'):
                raise ImportError('synthetic import failure')
            return original(name, *args, **kwargs)
        engine = self.engine()
        with patch('builtins.__import__', side_effect=guarded):
            self.assertEqual(engine.make_decision(.5)['reason'], 'telegram_paused')

    def test_injected_false_no_runtime_import_stat_or_read(self):
        provider = Mock(return_value=False)
        production = Mock(side_effect=AssertionError('production provider forbidden'))
        engine = self.engine(entries_paused_provider=provider)
        original = builtins.__import__
        attempted = []
        def guarded(name, *args, **kwargs):
            if name in ('runtime_control', 'kalshi_bot.runtime_control'):
                attempted.append(name)
                raise AssertionError('runtime import forbidden')
            return original(name, *args, **kwargs)
        with self.runtime(production), patch('builtins.__import__', side_effect=guarded), \
             patch.object(Path, 'stat', side_effect=AssertionError('stat forbidden')) as stat, \
             patch.object(Path, 'read_text', side_effect=AssertionError('read forbidden')) as read, \
             patch('builtins.open', side_effect=AssertionError('open forbidden')) as opened:
            decision = engine.make_decision(.5)
        self.assertEqual(decision['reason'], 'signal_warmup')
        self.assertEqual(attempted, [])
        provider.assert_called_once_with()
        production.assert_not_called()
        stat.assert_not_called()
        read.assert_not_called()
        opened.assert_not_called()

    def test_injected_true_before_warmup(self):
        provider = Mock(return_value=True)
        production = Mock(side_effect=AssertionError('production provider forbidden'))
        engine = self.engine(entries_paused_provider=provider)
        with self.runtime(production):
            decision = engine.make_decision(.42)
        self.assertEqual((decision['action'], decision['reason'], decision['p_market']),
                         ('WAIT', 'telegram_paused', .42))
        engine.sim.is_halted.assert_not_called()
        engine.signal.is_ready.assert_not_called()
        provider.assert_called_once_with()
        production.assert_not_called()

    def test_injected_exception_blocks_new_risk(self):
        provider = Mock(side_effect=RuntimeError('synthetic failure'))
        production = Mock(side_effect=AssertionError('no default fallback'))
        engine = self.engine(entries_paused_provider=provider)
        with self.runtime(production):
            self.assertEqual(engine.make_decision(.5)['reason'], 'telegram_paused')
        provider.assert_called_once_with()
        production.assert_not_called()

    def test_falsey_callable_is_still_injected(self):
        class Provider:
            def __bool__(self):
                return False
            def __call__(self):
                return True
        engine = self.engine(entries_paused_provider=Provider())
        production = Mock()
        with self.runtime(production):
            self.assertEqual(engine.make_decision(.5)['reason'], 'telegram_paused')
        production.assert_not_called()

    def test_existing_positional_constructor_compatible(self):
        engine = AssetEngine(ASSETS[0], object(), Mock(), None, None, None)
        self.assertIsNone(engine._entries_paused_provider)
        with self.assertRaises(TypeError):
            AssetEngine(ASSETS[0], object(), Mock(), None, None, None, lambda: False)


if __name__ == '__main__':
    unittest.main()
