"""Capture the unchanged running app, without editing saved user configuration.

Run python -m core.browser_acceptance http://127.0.0.1:8502
"""
import json
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    from playwright.sync_api import sync_playwright

    out = ROOT/'outputs/strategy_pruning'
    out.mkdir(parents=True, exist_ok=True)
    protected = [ROOT/'app.py', *sorted((ROOT/'ui').glob('*.py')),
                 *sorted((ROOT/'config').glob('*.json'))]
    before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8502'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1440, 'height':1200}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.get_by_label('输入基金代码或名称（如 110020）', exact=True).fill('510300')
        page.get_by_role('button', name='开始深度分析', exact=True).click()
        page.get_by_role('tab', name='决策依据清单', exact=True).wait_for(timeout=90000)
        page.get_by_role('tab', name='决策依据清单', exact=True).click()
        monthly = page.get_by_test_id('stAlertContainer').filter(has_text='B5 ·').first
        monthly.wait_for(timeout=90000)
        page.wait_for_function("document.querySelector('[data-testid=stApp]')?.getAttribute('data-test-script-state') === 'notRunning'")
        monthly.scroll_into_view_if_needed()
        page.screenshot(path=str(out/'streamlit-monthly.png'), full_page=False)
        conditions_text = page.locator('body').inner_text()
        assert '每月第一个已观察交易日' in conditions_text
        assert '非月度调仓日，不生成订单' in monthly.inner_text()
        page.get_by_role('tab', name='仓位推导与压力测试', exact=True).click()
        page.get_by_role('heading', name='历史组合扫描', exact=True).wait_for(timeout=90000)
        page.wait_for_function("document.querySelector('[data-testid=stApp]')?.getAttribute('data-test-script-state') === 'notRunning'")
        page.get_by_role('heading', name='历史组合扫描', exact=True).scroll_into_view_if_needed()
        page.screenshot(path=str(out/'streamlit-sizing.png'), full_page=False)
        sizing_text = page.locator('body').inner_text()
        assert '目标杠杆按总波动率即时缩放' in sizing_text
        forbidden = ('熔断', '暂缓加仓', '3日延迟', '3 日回落', '1.50个百分点', '非对称杠杆')
        for phrase in forbidden:
            assert phrase not in conditions_text+sizing_text, phrase
        assert not page.get_by_test_id('stException').count()
        assert not errors, errors
        after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        assert before == after
        receipt = {'url':url, 'screenshots':['streamlit-monthly.png','streamlit-sizing.png'],
            'monthly_rule':'非月度调仓日，不生成订单；等待每月第一个已观察交易日',
            'removed_messages_absent':list(forbidden), 'page_errors':errors,
            'config_changed':False, 'protected_sha256':after,
            'source':'Unmodified app.py, selected ETF 510300, saved holdings and real cached history.'}
        (out/'browser_receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
