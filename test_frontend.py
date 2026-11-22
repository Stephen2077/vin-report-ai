import json
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(params=[390, 1280])
def controlled_report_page(request):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': request.param, 'height': 740}, reduced_motion='reduce')
        html = Path(__file__).with_name('index.html').read_text()
        page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html') if route.request.url == 'http://openroad.test/' else route.abort())
        page.goto('http://openroad.test/')
        page.evaluate('''() => {
            window.pending = [];
            window.fetch = (url, {signal}) => new Promise(resolve => pending.push({url, signal, resolve}));
            window.finish = (index, vin, model) => pending[index].resolve({ok: Boolean(model), json: async () => ({
                vin, vehicle: {status: 'ok', data: {Model: model}},
                verdict: {decision: 'Inspect first', reasons: [], checklist: []},
                value: {status: 'unavailable'}, recalls: {status: 'unavailable'},
                complaints: {status: 'unavailable'},
                market: {status: 'ok', items: [{url: 'https://example.com', title: 'Listing',
                           snippet: '{"metadata":"' + 'x'.repeat(512) + '"}'}]},
                research: {status: 'ok', summary: '**Price:** *Varies by mileage.* [1]',
                           sources: [{url: 'https://example.com', title: 'Source'}]}
            })});
        }''')
        yield page
        browser.close()


def test_latest_quick_pick_wins_and_report_title_is_visible(controlled_report_page):
    page = controlled_report_page
    ford = page.get_by_role('button', name='Ford F-150', exact=False)
    nissan = page.get_by_role('button', name='Nissan Altima', exact=False)
    ford_vin = ford.get_attribute('data-vin')
    nissan_vin = nissan.get_attribute('data-vin')
    ford.click()
    ford.click()
    nissan.click()
    assert page.evaluate('pending.map(r => [r.url, r.signal.aborted])') == [
        [f'/api/report/{ford_vin}?lang=en', True],
        [f'/api/report/{nissan_vin}?lang=en', False],
    ]

    page.evaluate('(vin) => finish(1, vin, "Nissan Altima")', nissan_vin)
    playwright.expect(page.locator('#report h2')).to_have_text('Nissan Altima')
    playwright.expect(page.locator('#report .summary em')).to_have_text('Varies by mileage.')
    playwright.expect(page.locator('#report .summary .citation')).to_have_attribute('href', 'https://example.com/')
    top = page.locator('.report-top').bounding_box()
    assert top and top['y'] >= 0
    title = page.locator('#report h2').bounding_box()
    assert title and title['y'] + title['height'] <= 740
    assert page.evaluate('document.documentElement.scrollWidth === document.documentElement.clientWidth')

    # The mock can still complete an aborted fetch, exercising the stale-response guard.
    page.evaluate('(vin) => finish(0, vin, "Ford F-150")', ford_vin)
    assert page.locator('#vin').input_value() == nissan_vin
    playwright.expect(page.locator('#report h2')).to_have_text('Nissan Altima')
    playwright.expect(page.locator('#error')).to_be_empty()


def test_editing_vin_cancels_pending_report(controlled_report_page):
    page = controlled_report_page
    ford = page.get_by_role('button', name='Ford F-150', exact=False)
    ford.click()
    page.locator('#vin').fill('123')
    assert page.evaluate('pending[0].signal.aborted')
    playwright.expect(page.locator('#submit')).to_be_enabled()
    page.evaluate('(vin) => finish(0, vin, "Ford F-150")', ford.get_attribute('data-vin'))
    playwright.expect(page.locator('#report')).to_be_hidden()
    playwright.expect(page.locator('#error')).to_be_empty()


def test_markdown_rendering():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('http://openroad.test/', lambda route: route.fulfill(body=Path(__file__).with_name('index.html').read_text(), content_type='text/html'))
        page.goto('http://openroad.test/')
        rendered = page.evaluate('''() => {
            const host = document.createElement('div');
            host.innerHTML = markdown('# Buyer briefing\\n\\n**Pros** [[1, 2]]\\n- Reliable\\n- [Details](https://example.com/details)\\n\\n1. Inspect\\n2. Negotiate', [{url:'https://example.com/one'}, {url:'https://example.com/two'}]);
            return {bold:host.querySelector('strong').textContent, heading:host.querySelector('h4').textContent, lists:host.querySelectorAll('li').length, citations:[...host.querySelectorAll('.citation')].map(a=>a.href)};
        }''')
        assert rendered == {'bold': 'Pros', 'heading': 'Buyer briefing', 'lists': 4, 'citations': ['https://example.com/one', 'https://example.com/two']}
        assert page.evaluate("markdown('')") == ''
        malicious = page.evaluate('''() => {
            const host=document.createElement('div');
            host.innerHTML=markdown('<img src=x onerror=alert(1)> **<script>alert(1)</script>** [x](javascript:alert(1)) [99]', []);
            return {tags:host.querySelectorAll('img,script,a').length,text:host.textContent};
        }''')
        assert malicious['tags'] == 0
        assert '<img' in malicious['text'] and 'unsupported source' in malicious['text']
        assert '[99]' not in malicious['text']
        assert page.evaluate("markdown('[0] [[1, 2, 99]]', [{url:'https://example.com/one'}])").count('class="citation"') == 1
        assert 'unsupported source' in page.evaluate("markdown('[1]', [])")
        assert '99' not in page.evaluate("markdown('[99]', [])")
        assert page.evaluate("inlineMarkdown('[1]',[{url:'javascript:alert(1)'}])").find('href="#"') > 0
        browser.close()


def test_print_disclosures():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('http://openroad.test/', lambda route: route.fulfill(body=Path(__file__).with_name('index.html').read_text(), content_type='text/html'))
        page.goto('http://openroad.test/')
        for markup, expected in [('', []), ('<details><summary>Closed</summary>Body</details><details open><summary>Open</summary>Body</details>', [False, True])]:
            page.evaluate('(html) => document.querySelector("#report").innerHTML = html', markup)
            page.evaluate("dispatchEvent(new Event('beforeprint')); dispatchEvent(new Event('beforeprint'))")
            assert page.locator('#report details').evaluate_all('(els) => els.every(e => e.open)')
            page.evaluate("dispatchEvent(new Event('afterprint')); dispatchEvent(new Event('afterprint'))")
            assert page.locator('#report details').evaluate_all('(els) => els.map(e => e.open)') == expected
        browser.close()


def test_lookup_edges():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('http://openroad.test/', lambda route: route.fulfill(body=Path(__file__).with_name('index.html').read_text(), content_type='text/html'))
        page.goto('http://openroad.test/')
        page.locator('#vin').fill(' 1hgcm82633a004352 ')
        assert page.locator('#vin').input_value() == ' 1hgcm82633a004352 '
        result = page.evaluate('''async () => {
            let calls=[];
            window.fetch=(url, options)=>{calls.push(url);return new Promise(resolve=>window.finish=()=>resolve({ok:false}))};
            const first=lookup();await lookup();
            const disabled=document.querySelector('#submit').disabled;
            finish();await first;
            return {calls,disabled,vin:document.querySelector('#vin').value,error:document.querySelector('#error').textContent,enabled:!document.querySelector('#submit').disabled};
        }''')
        assert result['calls'] == ['/api/report/1HGCM82633A004352?lang=en']
        assert result['disabled'] and result['enabled']
        assert result['vin'] == '1HGCM82633A004352'
        assert result['error'] == 'We could not load this report. Please try again.'
        for vin in ['', 'short', 'I'*17, '1HGCM8263 A004352', '1'*18, '1HGCM82633A0043ß', '1HGCM82633A0043ſ2']:
            page.locator('#vin').fill(vin)
            page.locator('#submit').click()
            assert page.locator('#error').inner_text() == 'Please enter 17 characters, without I, O or Q.'
        page.locator('#vin').fill('1HGCM82633A004352')
        assert page.evaluate('''async () => {
            window.fetch=async()=>({ok:true,json:async()=>{throw new SyntaxError('Unexpected token <')}});
            await lookup();return document.querySelector('#error').textContent;
        }''') == 'We could not load this report. Please try again.'
        assert page.evaluate('''async () => {
            const original=window.setTimeout;
            window.setTimeout=(fn,ms)=>original(fn,ms===90000?1:ms);
            window.fetch=(url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))));
            await lookup();window.setTimeout=original;
            return document.querySelector('#error').textContent;
        }''') == 'The report timed out. Please try again.'
        assert page.locator('#submit').is_enabled()
        browser.close()


@pytest.mark.parametrize('success', [False, True])
def test_loading_leaves_vin_and_samples_available(success):
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('http://openroad.test/', lambda route: route.fulfill(body=Path(__file__).with_name('index.html').read_text(), content_type='text/html'))
        page.goto('http://openroad.test/')
        page.locator('#vin').fill('1HGCM82633A004352')
        page.evaluate('''() => {
            window.requestCount=0;
            window.fetch=()=>{requestCount++;return new Promise(resolve=>window.finishLookup=resolve)};
            window.renderedVin=null;window.render=r=>{
                window.renderedVin=r.vin;
                document.querySelector('#report').innerHTML='<div class="report-top"></div>';
            };
            window.pendingLookup=lookup();
        }''')
        assert page.locator('#vin').is_enabled()
        assert page.locator('#submit').is_disabled()
        assert page.locator('#language').is_disabled()
        assert page.locator('.sample:disabled').count() == 0
        assert page.locator('#vin').input_value() == '1HGCM82633A004352'
        assert page.evaluate('requestCount') == 1
        page.evaluate('''async success => {
            finishLookup({ok:success,json:async()=>({vin:'1HGCM82633A004352'})});
            await pendingLookup;
        }''', success)
        assert page.locator('#vin').is_enabled()
        assert page.locator('#submit').is_enabled()
        assert page.locator('#language').is_enabled()
        assert page.locator('.sample:disabled').count() == 0
        assert page.evaluate('renderedVin') == ('1HGCM82633A004352' if success else None)
        font = page.evaluate('''() => {const e=document.createElement('div');e.className='mono';document.body.append(e);return getComputedStyle(e).fontFamily}''')
        assert 'Noto Sans SC' in font and 'sans-serif' in font
        browser.close()


def test_v2_featured_cars_gallery_and_pricing():
    cars_payload = json.loads(Path(__file__).with_name('data').joinpath('featured_cars.json').read_text())
    api_cars = {
        'batch_date': '2026-04-15',
        'total': len(cars_payload),
        'count': len(cars_payload),
        'makes': sorted({i['make'] for i in cars_payload}),
        'years': sorted({i['year'] for i in cars_payload}, reverse=True),
        'items': cars_payload,
    }
    target = cars_payload[0]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route('http://openroad.test/v2', lambda route: route.fulfill(body=Path(__file__).with_name('index-v2.html').read_text(), content_type='text/html'))
        page.route('http://openroad.test/api/cars', lambda route: route.fulfill(body=json.dumps(api_cars), content_type='application/json'))
        page.route(f"http://openroad.test/api/cars/{target['id']}", lambda route: route.fulfill(
            body=json.dumps({**target, 'safety': {'recalls_count': 2, 'complaints_count': 14, 'recalls_source': 'https://www.nhtsa.gov', 'complaints_source': 'https://www.nhtsa.gov'}}),
            content_type='application/json',
        ))
        page.route('http://openroad.test/static/**', lambda route: route.fulfill(body=b'', content_type='image/jpeg'))
        page.goto('http://openroad.test/v2')
        page.locator('.car-card').first.wait_for()
        assert page.locator('#car-count').text_content() == '12 / 12'
        assert page.locator('.car-card').count() == 12
        assert 'Post kit' not in page.content()

        page.locator('#car-make').select_option('BMW')
        assert page.locator('.car-card').count() == 1
        assert page.locator('#car-count').text_content() == '1 / 12'
        card_text = page.locator('.car-card').first.text_content()
        assert '$30,900' in card_text
        assert '$27,900' in card_text

        page.locator('.car-card').first.click()
        page.locator('#modal-backdrop.open').wait_for()
        assert page.locator('#gal-counter').text_content() == '1 / 6'
        page.locator('#gal-next').click()
        assert page.locator('#gal-counter').text_content() == '2 / 6'
        assert '比市场价低 $3,000' in page.locator('#modal-price').text_content()
        assert '车型召回：2 项' in page.locator('#modal-safety').text_content()
        browser.close()

