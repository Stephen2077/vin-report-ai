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

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).parent
CACHE = Path(os.getenv('VIN_CACHE_PATH', str(ROOT / 'cache.sqlite')))
app = FastAPI(title='OpenRoad · Free VIN report')
VAULT = json.loads((ROOT / 'data/manheim_vault.json').read_text())
IN_FLIGHT = {}


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


async def fetch(client, url, **kwargs):
    try:
        response = await client.request(kwargs.pop('method', 'GET'), url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Expected an object')
        return {'status': 'ok', 'data': payload, 'source': str(response.url)}
    except (httpx.HTTPError, ValueError) as exc:
        note = f'Provider returned HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else 'Provider unavailable or timed out'
        return {'status': 'error', 'note': note, 'source': url}


@provider_data
async def decode(client, vin):
    result = await fetch(client, f'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json')
    if result['status'] == 'ok':
        rows = result.pop('data').get('Results', [])
        if not isinstance(rows, list):
            raise ValueError('Expected decode rows')
        raw = rows[0] if rows else {}
        if not isinstance(raw, dict) or any(v is not None and not isinstance(v, str) for v in raw.values()):
            raise ValueError('Expected decode strings')
        raw = {k: v or '' for k, v in raw.items()}
        result['data'] = {k: raw.get(k, '') for k in ['ModelYear', 'Make', 'Model', 'Trim', 'BodyClass', 'EngineCylinders', 'DisplacementL', 'DriveType', 'FuelTypePrimary', 'PlantCity', 'PlantCountry', 'TransmissionStyle', 'VehicleType', 'ErrorCode', 'ErrorText']}
        if not all(raw.get(k) for k in ['ModelYear', 'Make', 'Model']):
            result.update(status='error', note='VIN could not be fully decoded')
    return result


@provider_data
async def model_data(client, kind, params):
    result = await fetch(client, f'https://api.nhtsa.gov/{kind}/{kind}ByVehicle', params=params)
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
    result = await fetch(client, 'https://ydc-index.io/v1/search', headers={'X-API-Key': key}, params={'query': vehicle + ' used cars for sale price USD common problems reliability', 'count': 8})
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
    prompt = f'Write a concise buyer briefing for {vehicle} in {"Simplified Chinese" if lang == "zh" else "English"}. Under 220 words. Include pros, known issues, what to check, and a fair US used retail price range in USD if supported by current listings. Cite sources. Explain mileage/condition dependence. Do not claim VIN-specific accident, title, or service history. No invented prices.'
    result = await fetch(client, 'https://api.you.com/v1/research', method='POST', headers={'X-API-Key': os.environ['YOU_API_KEY']}, json={'input': prompt, 'research_effort': 'lite'}, timeout=65)
    if result['status'] == 'ok':
        output = result.pop('data').get('output', {})
        result.update(summary=output.get('content', ''), sources=output.get('sources', []))
        if not isinstance(result['summary'], str) or not isinstance(result['sources'], list) or any(not isinstance(s, dict) or not isinstance(s.get('url', ''), str) for s in result['sources']):
            raise ValueError('Expected research content')
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


def verdict(recalls, complaints, vault, value):
    incomplete = any(s['status'] != 'ok' for s in [recalls, complaints])
    rc, cc = recalls.get('count', 0), complaints.get('count', 0)
    score = min(100, rc * 5 + min(cc // 10, 45) + (20 if vault and vault['auto_grade'] < 3 else 0))
    reasons = [f'{rc} model-level recall campaigns; verify VIN repair completion.', f'{cc} model-level complaints; counts are not failure rates.']
    if incomplete:
        reasons.append('Safety data incomplete; do not interpret unavailable results as zero risk.')
    if vault:
        reasons.append(f"Illustrative condition grade {vault['auto_grade']}/5; illustrative retail ${value['retail']:,} includes $4,000 markup.")
    price_premium = bool(vault and vault.get('asking_price', 0) > value['retail'] * 1.1)
    if price_premium:
        reasons.append('Sample asking price is over 10% above the illustrative retail reference; negotiate against comparable listings.')
    decision = 'Inspect first' if incomplete or rc or cc > 100 or (vault and vault['auto_grade'] < 3) else 'Negotiate' if price_premium or (vault and vault['auto_grade'] < 4) else 'Buy'
    return {'status': 'limited' if incomplete else 'ok', 'decision': decision, 'risk_score': None if incomplete else score, 'reasons': reasons, 'checklist': ['Check open recalls by VIN with NHTSA and a dealer.', 'Get an independent pre-purchase inspection.', 'Verify title, accident history, odometer and service records separately.', 'Compare local listings with matching trim and mileage.'], 'note': 'Heuristic research aid, not a safety certification or purchase recommendation. Buy means consider only after inspection.'}


@app.get('/')
async def home():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/report/{vin}')
async def report(vin: str, lang: str = Query('en', pattern='^(en|zh)$')):
    vin = vin.strip().upper()
    if not re.fullmatch('[A-HJ-NPR-Z0-9]{17}', vin):
        raise HTTPException(422, 'Enter a 17-character VIN without I, O or Q.')
    key = 'report-v3:' + vin + ':' + lang
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
            result = dict(vin=vin, vehicle=vehicle, recalls=missing, complaints=missing, market=missing, research=missing, vault=vault, value=valuation(vault, {}), verdict=verdict(missing, missing, vault, valuation(vault, {})))
        else:
            params = {'make': raw['Make'], 'model': raw['Model'], 'modelYear': raw['ModelYear']}
            name = ' '.join(raw.get(k, '') for k in ['ModelYear', 'Make', 'Model', 'Trim'])
            recalls, complaints, market, brief = await asyncio.gather(model_data(client, 'recalls', params), model_data(client, 'complaints', params), search(client, name), research(client, name, lang))
            value = valuation(vault, market, raw)
            result = dict(vin=vin, vehicle=vehicle, recalls=recalls, complaints=complaints, market=market, research=brief, vault=vault, value=value, verdict=verdict(recalls, complaints, vault, value))
    result.update(cached=False, generated_at=time.time(), language=lang)
    if all(result[section]['status'] == 'ok' for section in ['vehicle', 'recalls', 'complaints', 'market', 'research']):
        cached(key, result)
    return result
