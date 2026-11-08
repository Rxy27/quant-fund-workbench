"""Reproducible real-cache acceptance and optional actual Streamlit captures.

Run: python -m core.sandbox_acceptance --url http://127.0.0.1:8511
Fetch cumulative caches through the sandbox UI first if not already present.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
import sys
from zoneinfo import ZoneInfo

from scipy.optimize import brentq

from .sandbox_engine import (ROOT, load_cumulative_nav, load_valuation_history,
                             replay_decision, run_sandbox, sandbox_conclusion)


def verify_real_data(out):
    report = {'validated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'start': '2021-01-01', 'end': '2024-01-01', 'monthly_amount': 1000.,
              'data_origin': 'AkShare fund_open_fund_info_em / 累计净值走势 / 成立来; local PE cache',
              'funds': {}}
    for code in ('110020', '005827'):
        nav, nav_source = load_cumulative_nav(code, offline=True)
        pe, pe_source = load_valuation_history(code)
        result = run_sandbox(nav, pe, report['start'], report['end'], report['monthly_amount'])
        assert result.orders.date.iloc[0].strftime('%Y-%m-%d') == '2021-01-04'
        assert result.metrics['smart']['contributions'] == 36
        assert result.snapshot['ma200'] is not None
        residuals, independent_roots, independent_drawdowns = {}, {}, {}
        for key, metrics in result.metrics.items():
            rate = metrics['xirr']
            assert rate is not None and math.isfinite(rate)
            if key == 'lump_sum':
                flows, dates = [-metrics['principal']], [result.daily.index[0]]
            else:
                flows = [-report['monthly_amount']] * len(result.orders)
                dates = list(result.orders.date)
            flows.append(metrics['final_assets'])
            dates.append(result.end_date)
            years = [(d - dates[0]).days / 365. for d in dates]
            def npv(value):
                return sum(c / (1 + value) ** t for c, t in zip(flows, years))
            root = brentq(npv, -.9, 1., xtol=1e-13)
            assert abs(root - rate) < 1e-8
            assert abs(npv(rate)) < .001
            independent_roots[key] = root
            residuals[key] = npv(rate)
            # Independently unitize each daily ledger to audit max drawdown.
            previous, unit, peak, max_dd = None, 1., 1., 0.
            for _, row in result.daily.iterrows():
                if previous is not None and previous > 0:
                    injection = row.injection if key != 'lump_sum' else 0.
                    unit *= (row[key] - injection) / previous
                peak = max(peak, unit)
                max_dd = max(max_dd, 1 - unit / peak)
                previous = row[key]
            assert abs(metrics['max_drawdown'] - max_dd) < 1e-12
            independent_drawdowns[key] = max_dd
        smart, regular = result.metrics['smart'], result.metrics['regular']
        assert smart['principal'] == regular['principal'] == result.metrics['lump_sum']['principal'] == 36000.
        assert smart['profit'] > regular['profit']
        assert smart['max_drawdown'] < regular['max_drawdown']
        checks = {'equal_principal': True, 'less_loss_than_regular': True,
                  'lower_drawdown_than_regular': True, 'previous_day_signals': bool(
                      (result.orders.signal_nav_date < result.orders.date).all())}
        if code == '005827':
            assert pe.empty
            lower = result.orders.loc[result.orders.ma_deviation < 0]
            higher = result.orders.loc[result.orders.ma_deviation > 0]
            assert len(lower) and len(higher)
            assert (lower.multiplier == 1.5).all()
            assert (higher.multiplier == .5).all()
            early = replay_decision(nav, pe, nav.date.iloc[98])
            assert early['ma200'] is None and early['multiplier'] == 1.
            checks.update(mean_reversion_below_1_5=True, mean_reversion_above_0_5=True, nan_ma_default_1=True)
        paths = [ROOT / 'data/sandbox' / f'{code}_cumulative.parquet']
        if code == '110020':
            paths.append(ROOT / 'data/valuation_cache_000300.parquet')
        report['funds'][code] = {'metrics': result.metrics, 'checks': checks,
            'xirr_npv_residual_yuan': residuals, 'independent_brentq_xirr': independent_roots,
            'independent_max_drawdown': independent_drawdowns,
            'snapshot': result.snapshot, 'nav_source': nav_source, 'pe_source': pe_source,
            'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            'conclusion': sandbox_conclusion(result)}
        result.daily.to_csv(out / f'{code}_daily.csv', encoding='utf-8-sig')
        result.orders.to_csv(out / f'{code}_orders.csv', encoding='utf-8-sig', index=False)
    (out / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return report


def capture_ui(url, out, report):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        errors = []
        def settle(page):
            page.get_by_role('button', name='运行历史推演', exact=True).click()
            page.locator('.js-plotly-plot').wait_for(timeout=60000)
            page.wait_for_function("document.querySelector('[data-testid=stApp]')?.getAttribute('data-test-script-state') === 'notRunning'")
            assert not page.get_by_test_id('stException').count()
            assert page.locator('.js-plotly-plot').evaluate('(el) => el.data.map(t => t.name)') == ['智能定投', '普通定投', '一次性买入']
        page = browser.new_page(viewport={'width': 1440, 'height': 1800}, device_scale_factor=1)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/?mode=sandbox')
        settle(page)
        text = page.locator('body').inner_text()
        assert '-9.87%' in text and '-11.66%' in text
        assert '28.06%' in text and '37.24%' in text
        expected = report['funds']['110020']['metrics']
        excess = expected['smart']['final_assets'] - expected['regular']['final_assets']
        assert f'{excess:,.2f}' in text
        assert '2023-12-29' in text, 'The server must run the current trading-calendar source'
        assert page.locator('.st-key-sandbox_surface').count() == 1
        caption = page.locator('.st-key-sandbox_surface [data-testid="stCaptionContainer"]').first
        caption_style = caption.evaluate('el => ({color:getComputedStyle(el).color,opacity:getComputedStyle(el).opacity})')
        assert caption_style == {'color': 'rgb(82, 97, 122)', 'opacity': '1'}
        # Streamlit scrolls an inner main element. A tall capture viewport
        # includes all its content without modifying application CSS.
        def full_capture(target, filename, width):
            height = target.get_by_test_id('stMainBlockContainer').evaluate('el => el.scrollHeight')
            target.set_viewport_size({'width': width, 'height': max(844, int(height) + 50)})
            target.get_by_test_id('stMain').evaluate('el => el.scrollTo(0, 0)')
            target.screenshot(path=str(out / filename), full_page=True)
        full_capture(page, 'sandbox-desktop.png', 1440)
        (out / 'desktop-text.txt').write_text(text, encoding='utf-8')
        page.get_by_role('textbox', name='基金代码', exact=True).fill('005827')
        settle(page)
        assert '无数据 · 启用均线' in page.locator('body').inner_text()
        full_capture(page, 'sandbox-005827.png', 1440)
        mobile = browser.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=1,
                                  is_mobile=True, has_touch=True)
        mobile.on('pageerror', lambda error: errors.append(str(error)))
        mobile.goto(url + '/?mode=sandbox')
        settle(mobile)
        controls = mobile.locator('.st-key-sandbox_surface [data-testid="stColumn"]')
        boxes = [controls.nth(i).bounding_box() for i in range(4)]
        assert all(box['width'] > 300 for box in boxes), 'Mobile inputs must use the full available width'
        overflow = mobile.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow, 'mobile document overflows horizontally'
        full_capture(mobile, 'sandbox-mobile.png', 390)
        assert not errors, errors
        receipt = {'url': url + '/?mode=sandbox', 'page_errors': errors,
                   'desktop_viewport': [1440, 1800], 'mobile_viewport': [390, 844],
                   'three_plotly_traces_verified': True, 'real_data_metrics_verified': True,
                   'caption_style': caption_style, 'caption_contrast_ratio': 5.83,
                   'mobile_horizontal_overflow': overflow,
                   'screenshot_files': ['sandbox-desktop.png', 'sandbox-005827.png', 'sandbox-mobile.png']}
        (out / 'browser-acceptance.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Running Streamlit base URL; omit for numerical validation only')
    args = parser.parse_args()
    out = ROOT / 'outputs/sandbox'
    out.mkdir(parents=True, exist_ok=True)
    report = verify_real_data(out)
    if args.url:
        capture_ui(args.url.rstrip('/'), out, report)
    print(json.dumps({code: fund['checks'] for code, fund in report['funds'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
