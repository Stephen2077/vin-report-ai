import asyncio
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
        return {'status': 'ok', 'summary': 'Brief', 'sources': []}
    monkeypatch.setattr(module, 'decode', fake_decode)
    monkeypatch.setattr(module, 'model_data', fake_model)
    monkeypatch.setattr(module, 'search', fake_search)
    monkeypatch.setattr(module, 'research', fake_research)
    first = await module.report('1HGCM82633A004352', 'en')
    assert first['value']['status'] == 'demo'
    assert first['research']['status'] == 'ok'
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


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [[], {'Results': [None], 'results': [None], 'output': None}, {'Results': None, 'results': None, 'results_other': []}, {'results': {'web': None}}, {'results': [], 'Count': 'bad'}, {'output': {'content': [], 'sources': None}}])
async def test_malformed_provider_shapes(payload, monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        for function, args in [(decode, ('1HGCM82633A004352',)), (model_data, ('complaints', {})), (model_data, ('recalls', {})), (search, ('Honda',)), (research, ('Honda', 'en'))]:
            result = await function(client, *args)
            assert result['status'] in ['ok', 'error']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 429, 500, 503])
async def test_you_http_failures(status, monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        assert (await search(client, 'Honda'))['status'] == 'error'
        assert (await research(client, 'Honda', 'en'))['status'] == 'error'


@pytest.mark.asyncio
async def test_timeout_and_bad_json(monkeypatch):
    monkeypatch.setenv('YOU_API_KEY', 'test-key')
    def timeout(request):
        raise httpx.ReadTimeout('slow', request=request)
    for handler in [timeout, lambda request: httpx.Response(200, text='not json')]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await decode(client, '1HGCM82633A004352'))['status'] == 'error'
            assert (await search(client, 'Honda'))['status'] == 'error'
            assert (await research(client, 'Honda', 'en'))['status'] == 'error'


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_section', ['vehicle', 'recalls', 'complaints', 'market', 'research', None])
async def test_partial_retry_and_concurrent_normalization(monkeypatch, failed_section):
    calls = []
    async def fake_decode(client, vin):
        calls.append(vin)
        await asyncio.sleep(.01)
        return {'status': 'error' if failed_section == 'vehicle' else 'ok', 'data': {'ModelYear': '2003', 'Make': 'HONDA', 'Model': 'Accord'}}
    async def fake_model(client, kind, params):
        return {'status': 'error' if failed_section == kind else 'ok', 'count': 0, 'items': []}
    async def fake_search(client, name):
        return {'status': 'error' if failed_section == 'market' else 'ok', 'items': []}
    async def fake_research(client, name, lang):
        return {'status': 'error' if failed_section == 'research' else 'ok', 'summary': lang}
    monkeypatch.setattr(module, 'decode', fake_decode)
    monkeypatch.setattr(module, 'model_data', fake_model)
    monkeypatch.setattr(module, 'search', fake_search)
    monkeypatch.setattr(module, 'research', fake_research)
    results = await asyncio.gather(module.report(' 1hgcm82633a004352 ', 'en'), module.report('1HGCM82633A004352', 'en'))
    assert len(calls) == 1
    assert results[0] == results[1]
    again = await module.report('1HGCM82633A004352', 'en')
    assert again['cached'] == (failed_section is None)
    assert len(calls) == (1 if failed_section is None else 2)
    assert (await module.report('1HGCM82633A004352', 'zh'))['language'] == 'zh'
    assert not module.IN_FLIGHT


@pytest.mark.parametrize('vin,code', [('', 404), ('1', 422), ('1'*16, 422), ('1'*18, 422), ('I'*17, 422), ('O'*17, 422), ('Q'*17, 422), ('!'*17, 422), ('é'*17, 422), ('1HGCM8263%2FA004352', 404), ('1HGCM8263%20A004352', 422)])
def test_edge_validation(vin, code):
    with TestClient(app) as client:
        assert client.get('/api/report/' + vin).status_code == code


@pytest.mark.parametrize('recall_status,complaint_status', [('error', 'unavailable'), ('ok', 'error'), ('unavailable', 'ok')])
@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_missing_safety_reasons(recall_status, complaint_status, lang):
    result = verdict({'status': recall_status, 'count': 0}, {'status': complaint_status, 'count': 0}, None, {}, lang)
    for reason, status in zip(result['reasons'], [recall_status, complaint_status]):
        if status == 'ok':
            assert reason.startswith('0 ')
        else:
            assert '0' not in reason
            assert ('unavailable' if lang == 'en' else '暂不可用') in reason
    assert result['risk_score'] is None
    assert result['decision'] == 'Inspect first'


def test_verdict_reason_localization():
    empty = {'status': 'ok', 'count': 0}
    vault = {**module.VAULT[0], 'asking_price': 1000000}
    value = valuation(vault, {})
    english = verdict(empty, empty, vault, value)
    chinese = verdict(empty, empty, vault, value, 'zh')
    assert english['reasons'] == [
        '0 model-level recall campaigns; verify VIN repair completion.',
        '0 model-level complaints; counts are not failure rates.',
        f"Illustrative condition grade {vault['auto_grade']}/5; illustrative retail ${value['retail']:,} includes $4,000 markup.",
        'Sample asking price is over 10% above the illustrative retail reference; negotiate against comparable listings.',
    ]
    assert chinese['reasons'][:2] == ['0 项车型层面召回；请按 VIN 核实维修完成情况。', '0 条车型层面投诉；投诉数量不代表故障率。']
    assert '演示车况评分' in chinese['reasons'][2]
    assert '议价' in chinese['reasons'][3]
    assert chinese['decision'] == english['decision']


@pytest.mark.asyncio
@pytest.mark.parametrize('decoded', [False, True])
async def test_report_passes_verdict_language(monkeypatch, decoded):
    async def fake_decode(client, vin):
        return {'status': 'ok' if decoded else 'error', 'data': {'Make': 'HONDA', 'Model': 'Accord', 'ModelYear': '2003'}}
    async def fake_model(client, kind, params):
        return {'status': 'ok', 'count': 2, 'items': []}
    async def fake_search(client, name):
        return {'status': 'ok', 'items': []}
    async def fake_research(client, name, lang):
        return {'status': 'ok', 'summary': 'Brief'}
    monkeypatch.setattr(module, 'decode', fake_decode)
    monkeypatch.setattr(module, 'model_data', fake_model)
    monkeypatch.setattr(module, 'search', fake_search)
    monkeypatch.setattr(module, 'research', fake_research)
    result = await module.report('1HGCM82633A004352', 'zh')
    assert ('车型层面召回' if decoded else '召回数据暂不可用') in result['verdict']['reasons'][0]
    if not decoded:
        assert result['recalls']['note'] == '需要先成功解码 VIN'


@pytest.mark.parametrize('vin', ['1HGCM82633A0043ß', '1HGCM82633A0043ſ2', '1HGCM82633A0043ı2', '1HGCM82633A0043é2'])
def test_unicode_rejected_before_uppercase(vin):
    with TestClient(app) as client:
        assert client.get('/api/report/' + vin).status_code == 422


@pytest.mark.parametrize('note', [
    'Provider returned malformed data', 'Provider unavailable or timed out',
    'VIN could not be fully decoded', 'You.com search is not configured',
    'You.com research is not configured', 'Research returned no summary',
    'Requires successful VIN decode', 'Provider returned HTTP 503',
    valuation(None, {})['note'], verdict({'status': 'ok'}, {'status': 'ok'}, None, {})['note'],
    valuation(None, {'items': [{'url': f'https://example.com/{i}', 'title': 'Used car for sale', 'snippet': '$10,000'} for i in range(3)]})['note'],
])
def test_app_note_localization(note):
    result = {'vehicle': {'note': note, 'data': {'ErrorText': 'Provider verbatim warning'}}, 'research': {'summary': 'Provider summary'}}
    module.localize_notes(result, 'en')
    assert result['vehicle']['note'] == note
    module.localize_notes(result, 'zh')
    assert result['vehicle']['note'] != note
    assert any('\u4e00' <= c <= '\u9fff' for c in result['vehicle']['note'])
    assert result['vehicle']['data']['ErrorText'] == 'Provider verbatim warning'
    assert result['research']['summary'] == 'Provider summary'
    other = {'vehicle': {'note': 'Unknown provider text'}}
    module.localize_notes(other, 'zh')
    assert other['vehicle']['note'] == 'Unknown provider text'
