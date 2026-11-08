"""Real-model UI acceptance for fund 110020, 2021-01-01 to 2025-01-01.

Run against a fresh app process with the user's saved AI configuration:
python -m core.sandbox_coach_acceptance --url http://127.0.0.1:8512
No configuration or credentials are written to the acceptance artifacts.
"""
import argparse
import hashlib
import json
import sys

from .sandbox_advisor import TITLES, build_sandbox_context
from .sandbox_engine import ROOT, load_cumulative_nav, load_valuation_history, run_sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    out = ROOT / 'outputs/sandbox_coach'
    out.mkdir(parents=True, exist_ok=True)
    nav, _ = load_cumulative_nav('110020', offline=True)
    pe, _ = load_valuation_history('110020')
    result = run_sandbox(nav, pe, '2021-01-01', '2025-01-01', 1000.)
    context = build_sandbox_context(result, '110020')
    (out / 'context.json').write_text(json.dumps(context, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1400}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url.rstrip('/') + '/?mode=sandbox')
        end_control = page.get_by_test_id('stDateInput').filter(has_text='回测结束日期')
        year = end_control.locator('[data-type="year"]')
        year.click()
        year.press('ArrowUp')  # the shipped default is 2024-01-01
        year.press('Tab')
        assert end_control.locator('input[type="date"]').input_value() == '2025-01-01'
        page.get_by_role('button', name='运行历史推演', exact=True).click()
        card = page.locator('.st-key-sandbox_ai_card')
        card.get_by_role('heading', name='🤖 AI 投资教练复盘', exact=True).wait_for(timeout=30000)
        page.wait_for_function('''() => document.querySelectorAll('.sandbox-coach-section').length === 3
            || !!document.querySelector('.st-key-sandbox_ai_card [data-testid="stAlertContainer"]')''', timeout=65000)
        if page.locator('.sandbox-coach-section').count() != 3:
            # Safe surface error only; never dump sidebar settings or keys.
            raise RuntimeError(card.inner_text())
        page.wait_for_function("document.querySelector('[data-testid=stApp]')?.getAttribute('data-test-script-state') === 'notRunning'")
        assert not page.get_by_test_id('stException').count()
        traces = page.locator('.js-plotly-plot').evaluate('(el) => el.data.map(t => t.name)')
        assert traces == ['智能定投', '普通定投', '一次性买入']
        paragraphs = page.locator('.sandbox-coach-section').all_inner_texts()
        sections = {title: text.split('】', 1)[1].strip() for title, text in zip(TITLES, paragraphs)}
        prose = ''.join(sections.values())
        assert all(term not in prose.casefold() for term in ('xirr', '夏普比率', '最大回撤', '年化收益'))
        main = page.get_by_test_id('stMain')
        assert '2025-01-01' in main.inner_text()
        grid = page.get_by_test_id('stDataFrame').first
        assert grid.count() == 1
        chart_box, card_box, table_box = (locator.bounding_box() for locator in
                                         (page.locator('.js-plotly-plot'), card, grid))
        assert chart_box['y'] < card_box['y'] < table_box['y']
        source = card.get_by_test_id('stCaptionContainer').first.inner_text()
        (out / 'ai-review.json').write_text(json.dumps(dict(source=source, sections=sections), ensure_ascii=False, indent=2), encoding='utf-8')
        def capture(name, width):
            page.set_viewport_size({'width': width, 'height': 1400 if width > 640 else 844})
            # Let Plotly resize to the responsive content width.
            page.wait_for_function('''() => {const p=document.querySelector('.js-plotly-plot');
                return p && Math.abs(p.getBoundingClientRect().width-p.parentElement.getBoundingClientRect().width)<5;}''')
            height = page.get_by_test_id('stMainBlockContainer').evaluate('el => el.scrollHeight')
            page.set_viewport_size({'width': width, 'height': int(height) + 80})
            main.evaluate('el => el.scrollTo(0,0)')
            page.mouse.move(0, 0)
            page.screenshot(path=str(out / name), full_page=True)
        capture('sandbox-coach-desktop.png', 1440)
        capture('sandbox-coach-mobile.png', 390)
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not errors, errors
        receipt = dict(fund='110020', start='2021-01-01', end='2025-01-01',
            real_model=True, source=source, three_plain_language_sections=True,
            forbidden_jargon_absent=True, three_curves=traces,
            card_after_chart_before_table=True, browser_errors=errors,
            source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                [ROOT / 'core/sandbox_advisor.py', ROOT / 'ui/sandbox_advisor.py', ROOT / 'ui/sandbox.py']},
            screenshots=['sandbox-coach-desktop.png', 'sandbox-coach-mobile.png'])
        (out / 'acceptance.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
    print(json.dumps(dict(status='passed', source=source, output=str(out)), ensure_ascii=False))


if __name__ == '__main__':
    main()
