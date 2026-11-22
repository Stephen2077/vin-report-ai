from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')


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
        for vin in ['', 'short', 'I'*17, '1HGCM8263 A004352', '1'*18]:
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
