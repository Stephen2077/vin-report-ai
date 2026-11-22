import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from functools import wraps
from collections import Counter
from pathlib import Path
from statistics import median, quantiles
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).parent
CACHE = Path(os.getenv('VIN_CACHE_PATH', str(ROOT / 'cache.sqlite')))
app = FastAPI(title='OpenRoad · Free VIN report')
VAULT = json.loads((ROOT / 'data/manheim_vault.json').read_text())
IN_FLIGHT = {}


def localize_notes(result, lang):
    if lang != 'zh':
        return
    translations = {
        'Provider returned malformed data': '数据提供方返回的数据格式异常',
        'Provider unavailable or timed out': '数据提供方暂不可用或请求超时',
        'VIN could not be fully decoded': '无法完整解码此 VIN',
        'You.com search is not configured': '尚未配置 You.com 搜索',
        'You.com research is not configured': '尚未配置 You.com 研究服务',
        'Research returned no summary': '研究服务未返回摘要',
        'Requires successful VIN decode': '需要先成功解码 VIN',
        'Insufficient data: need at least three independent, plausible listing-price signals above the $4,000 markup. No valuation generated.': '数据不足：至少需要三个独立、合理且高于 $4,000 加价额的挂牌价格参考。未生成估值。',
        'Central listing-price range after per-source median, IQR and median outlier filtering. Unverified asking prices, not sold comparables; trim/mileage may differ. Wholesale = market signals minus $4,000; not official MMR.': '挂牌价格区间已按来源中位数、四分位距及中位数过滤异常值。挂牌价未经核实，并非可比成交价；配置和里程可能不同。批发估值为市场价格参考减去 $4,000，并非官方 MMR。',
        'Heuristic research aid, not a safety certification or purchase recommendation. Buy means consider only after inspection.': '本报告为启发式研究参考，并非安全认证或购车推荐。“可考虑购买”仅指检查后再考虑。',
    }
    for section in ['vehicle', 'recalls', 'complaints', 'market', 'research', 'value', 'verdict']:
        data = result.get(section, {})
        note = data.get('note', '')
        if note in translations:
            data['note'] = translations[note]
        elif re.fullmatch(r'Provider returned HTTP \d{3}', note):
            data['note'] = '数据提供方返回 HTTP ' + note.rsplit(' ', 1)[1]


def provider_data(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (AttributeError, TypeError, KeyError, ValueError):
            return {'status': 'error', 'note': 'Provider returned malformed data'}
    return guarded


def cached(key, value=None):
    with sqlite3.connect(CACHE) as db:
        db.execute('CREATE TABLE IF NOT EXISTS reports (key TEXT PRIMARY KEY, created REAL, data TEXT)')
        if value is not None:
            db.execute('INSERT OR REPLACE INTO reports VALUES (?, ?, ?)', (key, time.time(), json.dumps(value)))
        else:
            row = db.execute('SELECT created, data FROM reports WHERE key=?', (key,)).fetchone()
            if row and time.time() - row[0] < 86400:
                return json.loads(row[1])


async def fetch(client, url, source=None, **kwargs):
    try:
        response = await client.request(kwargs.pop('method', 'GET'), url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Expected an object')
        return {'status': 'ok', 'data': payload, 'source': source or str(response.url)}
    except (httpx.HTTPError, ValueError) as exc:
        note = f'Provider returned HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else 'Provider unavailable or timed out'
        return {'status': 'error', 'note': note, 'source': source or url}


@provider_data
async def decode(client, vin):
    result = await fetch(client, f'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json', source='https://www.nhtsa.gov/vin-decoder')
    if result['status'] == 'ok':
        rows = result.pop('data').get('Results', [])
        if not isinstance(rows, list):
            raise ValueError('Expected decode rows')
        raw = rows[0] if rows else {}
        if not isinstance(raw, dict) or any(v is not None and not isinstance(v, str) for v in raw.values()):
            raise ValueError('Expected decode strings')
        raw = {k: v or '' for k, v in raw.items()}
        result['data'] = {k: raw.get(k, '') for k in ['ModelYear', 'Make', 'Model', 'Trim', 'BodyClass', 'EngineCylinders', 'DisplacementL', 'DriveType', 'FuelTypePrimary', 'FuelTypeSecondary', 'PlantCity', 'PlantCountry', 'TransmissionStyle', 'VehicleType', 'ErrorCode', 'ErrorText']}
        if not all(raw.get(k) for k in ['ModelYear', 'Make', 'Model']):
            result.update(status='error', note='VIN could not be fully decoded')
    return result


@provider_data
async def model_data(client, kind, params):
    vehicle = '/'.join(quote(str(params.get(k, '')).lower(), safe='') for k in ['modelYear', 'make', 'model'])
    result = await fetch(client, f'https://api.nhtsa.gov/{kind}/{kind}ByVehicle', params=params, source=f'https://www.nhtsa.gov/vehicle/{vehicle}#{kind}')
    if result['status'] != 'ok' and kind == 'complaints':
        result['note'] = "NHTSA's complaints service returned an error. Search this vehicle on NHTSA directly; the complaint count is unavailable."
    if result['status'] == 'ok':
        payload = result.pop('data')
        rows = payload.get('results', [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) or any(v is not None and not isinstance(v, str) for k, v in row.items() if k in ['components', 'summary', 'dateOfIncident', 'NHTSACampaignNumber', 'Component', 'Summary', 'Remedy']) for row in rows):
            raise ValueError('Expected safety rows')
        result['count'] = payload.get('Count', payload.get('count', len(rows)))
        if type(result['count']) is not int or result['count'] < 0:
            raise ValueError('Expected a nonnegative count')
        if kind == 'complaints':
            result['components'] = Counter(c.strip() for row in rows for c in (row.get('components') or '').split(',') if c.strip()).most_common(6)
            def incident_date(row):
                try:
                    return datetime.strptime(row.get('dateOfIncident') or '', '%m/%d/%Y')
                except (ValueError, TypeError):
                    return datetime.min
            rows = sorted(rows, key=incident_date, reverse=True)
            result['items'] = [{'component': r.get('components'), 'summary': (r.get('summary') or '')[:1000], 'date': r.get('dateOfIncident')} for r in rows[:4]]
        else:
            result['items'] = [{'campaign': r.get('NHTSACampaignNumber'), 'component': r.get('Component'), 'summary': r.get('Summary'), 'remedy': r.get('Remedy')} for r in rows]
    return result


@provider_data
async def search(client, vehicle):
    key = os.getenv('YOU_API_KEY')
    if not key:
        return {'status': 'unavailable', 'note': 'You.com search is not configured', 'items': []}
    result = await fetch(client, 'https://ydc-index.io/v1/search', source='https://you.com', headers={'X-API-Key': key}, params={'query': vehicle + ' used cars for sale price USD common problems reliability', 'count': 8})
    if result['status'] == 'ok':
        payload = result.pop('data')
        rows = payload.get('results', {}).get('web', payload.get('hits', []))
        if not isinstance(rows, list) or any(not isinstance(r, dict) or any(not isinstance(r.get(k, ''), str) for k in ['title', 'url', 'description']) or not isinstance(r.get('snippets', []), list) or any(not isinstance(s, str) for s in r.get('snippets', [])) for r in rows):
            raise ValueError('Expected search rows')
        result['items'] = [{'title': r.get('title', ''), 'url': r.get('url', ''), 'snippet': ' '.join(r.get('snippets', [])) or r.get('description', '')} for r in rows]
    return result


@provider_data
async def research(client, vehicle, lang):
    if not os.getenv('YOU_API_KEY'):
        return {'status': 'unavailable', 'note': 'You.com research is not configured'}
    prompt = f'Write a concise buyer briefing for {vehicle} in {"Simplified Chinese" if lang == "zh" else "English"}. Under 220 words. Include pros, known issues, what to check, and a fair US used retail price range in USD if supported by current listings. Cite sources. Explain mileage/condition dependence. Do not discuss other engines or configurations as this vehicle\'s. Unknown configuration fields are not evidence of a specific variant. Do not claim VIN-specific accident, title, or service history. No invented prices.'
    result = await fetch(client, 'https://api.you.com/v1/research', source='https://you.com', method='POST', headers={'X-API-Key': os.environ['YOU_API_KEY']}, json={'input': prompt, 'research_effort': 'lite'}, timeout=65)
    if result['status'] == 'ok':
        output = result.pop('data').get('output', {})
        result.update(summary=output.get('content', ''), sources=output.get('sources', []))
        if not isinstance(result['summary'], str) or not isinstance(result['sources'], list):
            raise ValueError('Expected research content')
        sources, ids, seen = [], {}, {}
        for i, item in enumerate(result['sources'], 1):
            if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                continue
            url = item['url'].strip()
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ['http', 'https'] and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if valid:
                if url not in seen:
                    sources.append({**item, 'url': url})
                    seen[url] = len(sources)
                ids[i] = seen[url]
        def citation(match):
            return ', '.join('[' + (str(ids[int(n)]) if int(n) in ids else 'unsupported source') + ']' for n in re.findall(r'\d+', match[1]))
        result['summary'] = re.sub(r'\[\[?(\d+(?:\s*,\s*\d+)*)\]?\](?!\()', citation, result['summary'])
        result['sources'] = sources
        if not result['summary']:
            result.update(status='error', note='Research returned no summary')
    return result


def valuation(vault, market, vehicle=None):
    if vault:
        return {'status': 'demo', 'label': 'Sample Manheim vault · illustrative, not live auction data', 'mmr_avg': vault['mmr_avg'], 'wholesale_range': vault['mmr_range'], 'retail': vault['mmr_avg'] + 4000, 'markup': 4000}
    signals = []
    seen = set()
    for item in market.get('items', []):
        source = item.get('url', '')
        if not source or source in seen:
            continue
        seen.add(source)
        text = item['title'] + ' ' + item['snippet']
        if vehicle and not all(str(vehicle.get(k, '')).lower() in text.lower() for k in ['ModelYear', 'Make', 'Model']):
            continue
        if not re.search(r'for sale|listing|asking price|used.*price|price.*used', text, re.I):
            continue
        prices = []
        for match in re.finditer(r'\$\s*([\d,]+(?:\.\d{2})?)', text):
            context = text[max(0, match.start() - 30):match.end() + 25]
            if re.search(r'month|\bmo\b|down payment|repair|insurance|per year|savings|save\b', context, re.I):
                continue
            price = float(match.group(1).replace(',', ''))
            if 4500 <= price <= 100000:
                prices.append(int(price))
        if prices:
            signals.append(median(prices))
    insufficient = {'status': 'unavailable', 'label': 'Estimate — live Manheim MMR connects via dealer account', 'note': 'Insufficient data: need at least three independent, plausible listing-price signals above the $4,000 markup. No valuation generated.'}
    if len(signals) < 3:
        return insufficient
    signals.sort()
    q1, _, q3 = quantiles(signals, n=4, method='inclusive')
    iqr, center = q3 - q1, median(signals)
    signals = [p for p in signals if max(q1 - 1.5 * iqr, center * .5) <= p <= min(q3 + 1.5 * iqr, center * 1.75)]
    if len(signals) < 3:
        return insufficient
    q1, _, q3 = quantiles(signals, n=4, method='inclusive')
    low, high = round(q1), round(q3)
    return {'status': 'estimate', 'label': 'Estimate — live Manheim MMR connects via dealer account', 'retail_range': [low, high], 'wholesale_range': [low - 4000, high - 4000], 'signal_count': len(signals), 'note': 'Central listing-price range after per-source median, IQR and median outlier filtering. Unverified asking prices, not sold comparables; trim/mileage may differ. Wholesale = market signals minus $4,000; not official MMR.'}


def verdict(recalls, complaints, vault, value, lang='en'):
    incomplete = any(s['status'] != 'ok' for s in [recalls, complaints])
    rc = recalls.get('count', 0) if recalls['status'] == 'ok' else None
    cc = complaints.get('count', 0) if complaints['status'] == 'ok' else None
    score = None if incomplete else min(100, rc * 5 + min(cc // 10, 45) + (20 if vault and vault['auto_grade'] < 3 else 0))
    def localized(en, zh):
        return zh if lang == 'zh' else en
    reasons = [
        localized(f'{rc} model-level recall campaigns; verify VIN repair completion.', f'{rc} 项车型层面召回；请按 VIN 核实维修完成情况。') if recalls['status'] == 'ok' else localized('Model-level recalls unavailable.', '召回数据暂不可用；请直接按 VIN 核实维修完成情况。'),
        localized(f'{cc} model-level complaints; counts are not failure rates.', f'{cc} 条车型层面投诉；投诉数量不代表故障率。') if complaints['status'] == 'ok' else localized('Model-level complaints unavailable.', '投诉数据暂不可用；数据缺失不代表没有投诉。'),
    ]
    if incomplete:
        reasons.append(localized('Safety data incomplete; do not interpret unavailable results as zero risk.', '安全数据不完整；不可将未返回的数据理解为零风险。'))
    if vault:
        reasons.append(localized(f"Illustrative condition grade {vault['auto_grade']}/5; illustrative retail ${value['retail']:,} includes $4,000 markup.", f"演示车况评分 {vault['auto_grade']}/5；演示零售价 ${value['retail']:,} 已包含 $4,000 加价。"))
    price_premium = bool(vault and vault.get('asking_price', 0) > value['retail'] * 1.1)
    if price_premium:
        reasons.append(localized('Sample asking price is over 10% above the illustrative retail reference; negotiate against comparable listings.', '样本挂牌价比演示零售参考价高出 10% 以上；请参考同类挂牌车辆议价。'))
    decision = 'Inspect first' if incomplete or rc or cc > 100 or (vault and vault['auto_grade'] < 3) else 'Negotiate' if price_premium or (vault and vault['auto_grade'] < 4) else 'Buy'
    return {'status': 'limited' if incomplete else 'ok', 'decision': decision, 'risk_score': score, 'reasons': reasons, 'checklist': ['Check open recalls by VIN with NHTSA and a dealer.', 'Get an independent pre-purchase inspection.', 'Verify title, accident history, odometer and service records separately.', 'Compare local listings with matching trim and mileage.'], 'note': 'Heuristic research aid, not a safety certification or purchase recommendation. Buy means consider only after inspection.'}


@app.get('/')
async def home():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/report/{vin}')
async def report(vin: str, lang: str = Query('en', pattern='^(en|zh)$')):
    vin = vin.strip()
    if not re.fullmatch('[A-HJ-NPR-Za-hj-npr-z0-9]{17}', vin):
        raise HTTPException(422, 'Enter a 17-character VIN without I, O or Q.')
    vin = vin.upper()
    key = 'report-v6:' + vin + ':' + lang
    hit = cached(key)
    if hit:
        return {**hit, 'cached': True}
    if key not in IN_FLIGHT:
        task = asyncio.create_task(build_report(vin, lang, key))
        IN_FLIGHT[key] = task
        task.add_done_callback(lambda done: IN_FLIGHT.pop(key, None))
    return await asyncio.shield(IN_FLIGHT[key])


async def build_report(vin, lang, key):
    vault = next((v for v in VAULT if v['vin'] == vin), None)
    async with httpx.AsyncClient(timeout=httpx.Timeout(22), follow_redirects=True) as client:
        vehicle = await decode(client, vin)
        raw = vehicle.get('data', {})
        if vehicle['status'] != 'ok':
            missing = {'status': 'unavailable', 'note': 'Requires successful VIN decode'}
            result = dict(vin=vin, vehicle=vehicle, recalls=missing, complaints=missing, market=missing, research=missing, vault=vault, value=valuation(vault, {}), verdict=verdict(missing, missing, vault, valuation(vault, {}), lang))
        else:
            params = {'make': raw['Make'], 'model': raw['Model'], 'modelYear': raw['ModelYear']}
            name = ' '.join(raw.get(k, '') for k in ['ModelYear', 'Make', 'Model', 'Trim'])
            name += '; decoded configuration: ' + ', '.join(f'{label}: {raw.get(k) or "unknown"}' for k, label in [('Trim', 'trim'), ('DisplacementL', 'displacement L'), ('EngineCylinders', 'cylinders'), ('DriveType', 'drive'), ('FuelTypePrimary', 'fuel'), ('FuelTypeSecondary', 'secondary fuel')])
            recalls, complaints, market, brief = await asyncio.gather(model_data(client, 'recalls', params), model_data(client, 'complaints', params), search(client, name), research(client, name, lang))
            value = valuation(vault, market, raw)
            result = dict(vin=vin, vehicle=vehicle, recalls=recalls, complaints=complaints, market=market, research=brief, vault=vault, value=value, verdict=verdict(recalls, complaints, vault, value, lang))
    result.update(cached=False, generated_at=time.time(), language=lang)
    localize_notes(result, lang)
    if all(result[section]['status'] == 'ok' for section in ['vehicle', 'recalls', 'complaints', 'market', 'research']):
        cached(key, result)
    return result
