import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app as module
from app import app, cached, decode, fetch, model_data, research, search, valuation, verdict


@pytest.fixture(autouse=True)
def cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'CACHE', tmp_path / 'cache.sqlite')


def test_validation():
    with TestClient(app) as client:
        for vin in ['short', 'I' * 17, 'O' * 17, 'Q' * 17]:
            assert client.get('/api/report/' + vin).status_code == 422
        assert client.get('/api/report/1HGCM82633A004352?lang=fr').status_code == 422
        assert client.get('/').status_code == 200


def test_cache(monkeypatch):
    assert cached('vin:en') is None
    cached('vin:en', {'status': 'ok'})
    assert cached('vin:en') == {'status': 'ok'}
    assert cached('vin:zh') is None
    monkeypatch.setattr(module.time, 'time', lambda: 99999999999)
    assert cached('vin:en') is None


def test_value_and_verdict():
    vault = module.VAULT[0]
    value = valuation(vault, {})
    assert value['retail'] == vault['mmr_avg'] + 4000
    assert value['status'] == 'demo'
    assert valuation(None, {})['status'] == 'unavailable'
    estimate = valuation(None, {'items': [{'url': f'https://example.com/{i}', 'title': 'Used car for sale', 'snippet': f'Asking price ${p:,}'} for i, p in enumerate([8000, 10000, 12000, 99000])]})
    assert estimate['wholesale_range'] == [5000, 7000]
    assert estimate['status'] == 'estimate'
    empty = {'status': 'ok', 'count': 0}
    assert verdict(empty, empty, None, {})['decision'] == 'Buy'
    assert verdict({'status': 'ok', 'count': 2}, empty, None, {})['decision'] == 'Inspect first'
    assert verdict({'status': 'error'}, empty, None, {})['risk_score'] is None
    assert verdict(empty, empty, vault, value)['decision'] == 'Negotiate'


@pytest.mark.parametrize('status', ['error', 'unavailable'])
@pytest.mark.parametrize('kind', ['recalls', 'complaints'])
def test_unknown_safety_counts(status, kind):
    sections = {'recalls': {'status': 'ok', 'count': 0}, 'complaints': {'status': 'ok', 'count': 0}}
    sections[kind] = {'status': status, 'count': 999}
    result = verdict(sections['recalls'], sections['complaints'], None, {})
    assert result['risk_score'] is None
    assert result['decision'] == 'Inspect first'
    assert f'Model-level {kind} unavailable.' in result['reasons']
    assert not any('999' in reason for reason in result['reasons'])
    both = verdict({'status': status}, {'status': status}, None, {})
    assert both['risk_score'] is None
    assert not any(reason.startswith('0 ') for reason in both['reasons'])


def test_price_signal_filtering():
    vehicle = {'ModelYear': '2018', 'Make': 'Nissan', 'Model': 'Altima'}
    def item(price, suffix, text='2018 Nissan Altima for sale'):
        return {'url': 'https://example.com/' + suffix, 'title': text, 'snippet': f'Price ${price:,}'}
    bad = [item(500, 'a'), item(1500, 'b'), item(120000, 'c'), item(8000, 'd', '2005 Honda Accord for sale'), item(8000, 'e', '2018 Nissan Altima repair cost'), item(9000, 'f', '2018 Nissan Altima for sale down payment')]
    assert valuation(None, {'items': bad}, vehicle)['status'] == 'unavailable'
    assert valuation(None, {'items': [item(8000, 'same')] * 4}, vehicle)['status'] == 'unavailable'
    good = [item(p, str(i)) for i, p in enumerate([9000, 10000, 11000, 100000])]
    result = valuation(None, {'items': bad + good}, vehicle)
    assert result['status'] == 'estimate'
    assert result['signal_count'] == 3
    assert result['wholesale_range'][0] > 0


@pytest.mark.asyncio
async def test_provider_errors():
    def handler(request):
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch(client, 'https://example.com')
        assert result['status'] == 'error'
        assert '503' in result['note']
    def bad(request):
        return httpx.Response(200, text='not json')
    async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
        assert (await fetch(client, 'https://example.com'))['status'] == 'error'


@pytest.mark.asyncio
async def test_empty_decode_and_complaints():
    def handler(request):
        return httpx.Response(200, json={'Results': [], 'results': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await decode(client, '1HGCM82633A004352'))['status'] == 'error'
        result = await model_data(client, 'complaints', {})
        assert result['count'] == 0 and result['items'] == []


@pytest.mark.asyncio
async def test_complaints_sort():
    def handler(request):
        return httpx.Response(200, json={'results': [{'components': 'ENGINE', 'dateOfIncident': '12/01/2020'}, {'components': 'ENGINE,BRAKES', 'dateOfIncident': '01/01/2025'}, {'components': 'OTHER', 'dateOfIncident': None}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await model_data(client, 'complaints', {})
        assert result['items'][0]['date'] == '01/01/2025'
        assert result['components'][0] == ('ENGINE', 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 400, 503])
async def test_public_section_sources(status, monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    params = {'make': 'FORD', 'model': 'F-150', 'modelYear': '2013'}
    def handler(request):
        if request.url.host == 'api.nhtsa.gov':
            assert dict(request.url.params) == params
        if request.url.host == 'ydc-index.io':
            return httpx.Response(status, json={'results': {'web': []}})
        return httpx.Response(status, json={'count': 0, 'results': [], 'Results': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await decode(client, '1FTFX1ET5DKF73050'))['source'] == 'https://www.nhtsa.gov/vin-decoder'
        for kind in ['recalls', 'complaints']:
            result = await model_data(client, kind, params)
            assert result['source'] == f'https://www.nhtsa.gov/vehicle/2013/ford/f-150#{kind}'
            if kind == 'complaints' and status != 200:
                assert "NHTSA's complaints service returned an error" in result['note']
                assert 'count' not in result
        assert (await search(client, 'Ford'))['source'] == 'https://you.com'
        assert (await research(client, 'Ford', 'en'))['source'] == 'https://you.com'


@pytest.mark.asyncio
@pytest.mark.parametrize('sources,summary,expected,content', [
    ([{'url': 'https://example.com/a'}, {'url': 'javascript:bad'}, {'url': 'https://example.com/a'}, {'url': 'http://example.com/b'}, {'url': 'https://user:pass@example.com'}],
     'Brief [[1, 2, 3, 4, 5, 99]]', ['https://example.com/a', 'http://example.com/b'], '[1], [unsupported source], [1], [2], [unsupported source], [unsupported source]'),
    ([], 'Brief [1]', [], '[unsupported source]'),
    ([{}, {'url': None}, {'url': '/relative'}, {'url': 'https://'}], 'Brief [1]', [], '[unsupported source]'),
    ([{'url': 'https://example.com'}], 'Brief [1]', ['https://example.com'], '[1]'),
])
async def test_research_sources(monkeypatch, sources, summary, expected, content):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    def handler(request):
        return httpx.Response(200, json={'output': {'content': summary, 'sources': sources}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await research(client, 'Honda', 'en')
        assert [s['url'] for s in result['sources']] == expected
        assert content in result['summary']


@pytest.mark.asyncio
async def test_you_shapes(monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    def handler(request):
        assert request.headers['X-API-Key'] == 'test-key'
        if request.url.host == 'api.you.com':
            assert request.method == 'POST'
            assert 'Simplified Chinese' in json.loads(request.content)['input']
            assert "Do not discuss other engines or configurations as this vehicle's" in json.loads(request.content)['input']
            return httpx.Response(200, json={'output': {'content': 'Brief [1]', 'sources': [{'url': 'https://example.com'}]}})
        assert request.method == 'GET'
        return httpx.Response(200, json={'results': {'web': [{'title': 'Market', 'url': 'https://example.com', 'snippets': ['A', 'B']}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await search(client, 'Honda'))['items'][0]['snippet'] == 'A B'
        assert (await research(client, 'Honda', 'zh'))['summary'] == 'Brief [1]'
    monkeypatch.delenv('YOU_API_KEY')
    assert (await search(None, 'Honda'))['status'] == 'unavailable'
    assert (await research(None, 'Honda', 'en'))['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_report_and_cache(monkeypatch):
    calls = []
    async def fake_decode(client, vin):
        calls.append(vin)
        return {'status': 'ok', 'data': {'ModelYear': '2003', 'Make': 'HONDA', 'Model': 'Accord', 'Trim': 'EX-V6', 'DisplacementL': '3.0', 'EngineCylinders': '6', 'DriveType': 'FWD', 'FuelTypePrimary': 'Gasoline'}}
    async def fake_model(client, kind, params):
        return {'status': 'ok', 'count': 0, 'items': []}
    async def fake_search(client, name):
        for text in ['trim: EX-V6', 'displacement L: 3.0', 'cylinders: 6', 'drive: FWD', 'fuel: Gasoline', 'secondary fuel: unknown']:
            assert text in name
        return {'status': 'ok', 'items': []}
    async def fake_research(client, name, lang):
        assert 'displacement L: 3.0' in name and 'cylinders: 6' in name
        return {'status': 'error', 'note': 'Timed out'}
    monkeypatch.setattr(module, 'decode', fake_decode)
    monkeypatch.setattr(module, 'model_data', fake_model)
    monkeypatch.setattr(module, 'search', fake_search)
    monkeypatch.setattr(module, 'research', fake_research)
    first = await module.report('1HGCM82633A004352', 'en')
    assert first['value']['status'] == 'demo'
    assert first['research']['status'] == 'error'
    assert (await module.report('1HGCM82633A004352', 'en'))['cached']
    assert len(calls) == 1
    other = await module.report('1N4AL3AP2JC123510', 'en')
    assert other['vault'] is None and other['value']['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_failed_decode(monkeypatch):
    async def failed(client, vin):
        return {'status': 'error', 'note': 'Timeout'}
    monkeypatch.setattr(module, 'decode', failed)
    result = await module.report('1N4AL3AP2JC123510', 'en')
    assert result['recalls']['status'] == 'unavailable'
    assert result['verdict']['risk_score'] is None


def test_sample_vin_identities():
    expected = {'5YJ3E1EB9KF442116': (2019, 'Tesla', 'Model 3'), '1FTFX1ET5DKF73050': (2013, 'Ford', 'F-150')}
    html = Path('index.html').read_text()
    samples = re.findall(r'data-vin="([^"]+)"', html)
    assert samples == ['1HGCM82633A004352', '5YJ3E1EB9KF442116', '1FTFX1ET5DKF73050', '1N4AL3AP2JC123510']
    for vin, identity in expected.items():
        row = next(v for v in module.VAULT if v['vin'] == vin)
        assert (row['year'], row['make'], row['model']) == identity
        assert row['trim'] == ''
