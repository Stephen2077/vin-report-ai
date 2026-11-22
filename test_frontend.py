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
