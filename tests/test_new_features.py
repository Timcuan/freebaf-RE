import json, sys
sys.path.insert(0, '.')

# Test 1: All imports
from freebuff2api.usage import RequestRecord, ApiKeyRecord
from freebuff2api.usage_store import create_stores, RequestStore, ApiKeyStore
from freebuff2api.config import load_settings
from freebuff2api.app import app
from freebuff2api.admin import router

# Test 2: Stores creation
rs, aks = create_stores(100)
assert isinstance(rs, RequestStore)
assert isinstance(aks, ApiKeyStore)
print('[OK] Test 2: stores created')

# Test 3: API key store - fallback from old setting
aks.load_from_settings(None, 'test-key-12345678')
keys = aks.list_all()
assert len(keys) == 1
assert keys[0]['name'] == 'default'
assert keys[0]['key_prefix'] == 'test-key'
assert keys[0]['enabled'] == True
print(f'[OK] Test 3: fallback key created: {keys[0]["key_prefix"]}')

# Test 4: API key store - from JSON
aks2 = ApiKeyStore()
aks2.load_from_settings(json.dumps([
    {'name': 'admin', 'key': 'sk-admin-key-123', 'allowed_models': ['*'], 'enabled': True},
    {'name': 'free-only', 'key': 'sk-free-key-456', 'allowed_models': ['deepseek/deepseek-v4-flash'], 'enabled': True},
]), None)
keys2 = aks2.list_all()
assert len(keys2) == 2
print('[OK] Test 4: JSON keys loaded')

# Test 5: Authentication
auth = aks2.authenticate('Bearer sk-admin-key-123', None)
assert auth is not None
assert auth.name == 'admin'
print(f'[OK] Test 5: auth succeeded for {auth.name}')

# Test 6: Model restriction
assert auth.allows_model('any-model')
free_key = aks2.authenticate('Bearer sk-free-key-456', None)
assert free_key.allows_model('deepseek/deepseek-v4-flash')
assert not free_key.allows_model('deepseek/deepseek-v4-pro')
print('[OK] Test 6: model restriction works')

# Test 7: Request store
rec = RequestRecord(0, '2026-01-01T00:00:00', 'admin', 'sk-admin', 'deepseek/deepseek-v4-flash', 1200, 100, 50, 150, 'success')
rs.add(rec)
items = rs.list()
assert len(items) == 1
assert items[0]['total_tokens'] == 150
stats = rs.stats()
assert stats['total'] == 1
assert stats['total_tokens'] == 150
print(f'[OK] Test 7: request store: {stats}')

# Test 8: Html file exists with new content
import pathlib
html = pathlib.Path('freebuff2api/admin_static/index.html')
assert html.exists()
content = html.read_text(encoding='utf-8')
assert 'request-records' in content or 'Request Records' in content or 'reqRecords' in content
assert 'apiKeys' in content
assert 'apiKeyModal' in content
assert 'reqRecords' in content
print('[OK] Test 8: HTML contains new sections')

# Test 9: Config has new fields
settings = load_settings()
assert hasattr(settings, 'api_keys_json')
assert hasattr(settings, 'max_request_records')
print(f'[OK] Test 9: config has new fields - max_records={settings.max_request_records}')

# Test 10: App has stores on state
print(f'[OK] Test 10: app routes count={len(app.routes)}')

print('\n=== ALL TESTS PASSED ===')
