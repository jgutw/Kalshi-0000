"""Fresh-process import contracts. Only synthetic credentials and offline imports."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent.parent


class ImportBoundaryTests(unittest.TestCase):
    def run_child(self, body):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / '.env'
            fixture.write_text('KALSHI_API_KEY=fixture-key\nKALSHI_PRIVATE_KEY=fixture-private\n', encoding='utf-8')
            for name in ('logs', 'sessions', 'research_data'):
                (root / name).mkdir()
                (root / name / 'sentinel').write_bytes(b'unchanged')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            env = os.environ.copy()
            for key in list(env):
                if key.startswith('KALSHI_') or key == 'PYTHON_DOTENV_DISABLED':
                    env.pop(key)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            env['PYTHONPATH'] = str(ROOT)
            guard = '''
import os, sys
from pathlib import Path
fixture = Path.cwd() / '.env'
def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
        raise AssertionError('network forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == '.env' and path != fixture:
            raise AssertionError('nonfixture dotenv access forbidden')
        if any(part in ('logs', 'sessions', 'research_data') for part in path.parts):
            raise AssertionError('runtime artifact access forbidden')
sys.addaudithook(audit)
'''
            result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(guard) + textwrap.dedent(body)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_asset_engine_import_is_credential_and_transport_free(self):
        self.run_child('''
import kalshi_bot.asset_engine
import kalshi_bot.config as config
assert 'kalshi_bot.asset_engine' in sys.modules
assert 'kalshi_bot.config' in sys.modules
for module in ('kalshi_bot.kalshi_client', 'kalshi_bot.api_config', 'requests', 'dotenv'):
    assert module not in sys.modules, module
assert not hasattr(config, 'api_cfg')
assert not hasattr(config, 'APIConfig')
assert 'KALSHI_API_KEY' not in os.environ
assert 'KALSHI_PRIVATE_KEY' not in os.environ
''')

    def test_trading_config_alone_is_credential_free(self):
        self.run_child('''
import kalshi_bot.config as config
assert isinstance(config.cfg, config.TradingConfig)
assert config.ASSETS
assert 'dotenv' not in sys.modules
assert 'kalshi_bot.api_config' not in sys.modules
assert 'KALSHI_API_KEY' not in os.environ
assert 'KALSHI_PRIVATE_KEY' not in os.environ
assert not hasattr(config, 'api_cfg') and not hasattr(config, 'APIConfig')
''')

    def test_api_config_loads_fixture_and_preserves_defaults(self):
        self.run_child('''
import kalshi_bot.api_config as api
assert api.api_cfg.KALSHI_API_KEY == 'fixture-key'
assert api.api_cfg.KALSHI_PRIVATE_KEY == 'fixture-private'
assert os.environ['KALSHI_API_KEY'] == 'fixture-key'
assert os.environ['KALSHI_PRIVATE_KEY'] == 'fixture-private'
assert isinstance(api.api_cfg, api.APIConfig)
assert set(api.APIConfig.__dataclass_fields__) == {
    'KALSHI_API_KEY', 'KALSHI_PRIVATE_KEY', 'COINBASE_WS', 'BINANCE_WS', 'OKX_WS', 'GEMINI_WS'}
assert api.api_cfg.COINBASE_WS == 'wss://advanced-trade-ws.coinbase.com'
assert api.api_cfg.BINANCE_WS == 'wss://stream.binance.com:9443/stream'
assert api.api_cfg.OKX_WS == 'wss://ws.okx.com:8443/ws/v5/public'
assert api.api_cfg.GEMINI_WS == 'wss://ws.gemini.com'
assert api.api_cfg.KALSHI_REST == 'https://api.elections.kalshi.com/trade-api/v2'
assert api.api_cfg.KALSHI_WS == 'wss://api.elections.kalshi.com/trade-api/ws/v2'
os.environ['KALSHI_DEMO'] = 'TrUe'
assert api.api_cfg.KALSHI_REST == 'https://demo-api.kalshi.co/trade-api/v2'
assert api.api_cfg.KALSHI_WS == 'wss://demo-api.kalshi.co/trade-api/ws/v2'
os.environ['KALSHI_BASE_URL'] = 'https://fixture.invalid/custom///'
assert api.api_cfg.KALSHI_REST == 'https://fixture.invalid/custom'
assert api.api_cfg.KALSHI_WS == 'wss://demo-api.kalshi.co/trade-api/ws/v2'
for module in ('kalshi_bot.config', 'kalshi_bot.asset_engine', 'kalshi_bot.kalshi_client', 'requests'):
    assert module not in sys.modules, module
''')

    def test_dotenv_preserves_existing_environment_precedence(self):
        self.run_child('''
os.environ['KALSHI_API_KEY'] = 'explicit-key'
os.environ['KALSHI_PRIVATE_KEY'] = 'explicit-private'
import kalshi_bot.api_config as api
assert api.api_cfg.KALSHI_API_KEY == 'explicit-key'
assert api.api_cfg.KALSHI_PRIVATE_KEY == 'explicit-private'
os.environ['KALSHI_API_KEY'] = 'later-key'
assert api.APIConfig().KALSHI_API_KEY == 'later-key'
assert api.api_cfg.KALSHI_API_KEY == 'explicit-key'
''')


if __name__ == '__main__':
    unittest.main()
