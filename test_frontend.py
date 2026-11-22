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
                complaints: {status: 'unavailable'}, market: {status: 'unavailable'},
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
        assert '<img' in malicious['text'] and '[99]' in malicious['text']
        assert page.evaluate("inlineMarkdown('[1]',[{url:'javascript:alert(1)'}])").find('href="#"') > 0
        browser.close()
