import json

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
    estimate = valuation(None, {'items': [{'title': '$8,000 to $12,000', 'snippet': '$50/month'}]})
    assert estimate['wholesale_range'] == [4000, 8000]
    assert estimate['status'] == 'estimate'
    empty = {'status': 'ok', 'count': 0}
    assert verdict(empty, empty, None, {})['decision'] == 'Buy'
    assert verdict({'status': 'ok', 'count': 2}, empty, None, {})['decision'] == 'Inspect first'
    assert verdict({'status': 'error'}, empty, None, {})['risk_score'] is None
    assert verdict(empty, empty, vault, value)['decision'] == 'Negotiate'


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
async def test_you_shapes(monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    def handler(request):
        assert request.headers['X-API-Key'] == 'test-key'
        if request.url.host == 'api.you.com':
            assert request.method == 'POST'
            assert 'Simplified Chinese' in json.loads(request.content)['input']
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
        return {'status': 'ok', 'data': {'ModelYear': '2003', 'Make': 'HONDA', 'Model': 'Accord'}}
    async def fake_model(client, kind, params):
        return {'status': 'ok', 'count': 0, 'items': []}
    async def fake_search(client, name):
        return {'status': 'ok', 'items': []}
    async def fake_research(client, name, lang):
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
    other = await module.report('1N4AL3AP8JC123456', 'en')
    assert other['vault'] is None and other['value']['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_failed_decode(monkeypatch):
    async def failed(client, vin):
        return {'status': 'error', 'note': 'Timeout'}
    monkeypatch.setattr(module, 'decode', failed)
    result = await module.report('1N4AL3AP8JC123456', 'en')
    assert result['recalls']['status'] == 'unavailable'
    assert result['verdict']['risk_score'] is None
