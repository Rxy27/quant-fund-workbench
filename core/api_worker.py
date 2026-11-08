"""One bounded, isolated AkShare call. No HTTP calls outside this adapter."""
import json
from pathlib import Path
import sys
import re
from io import StringIO


def main():
    import akshare as ak
    endpoint, kwargs, destination = sys.argv[1:]
    kwargs = json.loads(kwargs)
    if endpoint == 'disclosure_scale_em':
        # Supplemental observations from the same Eastmoney fund snapshot used by AkShare.
        from akshare.utils import http
        import pandas as pd
        text = http.get(f"https://fund.eastmoney.com/pingzhongdata/{kwargs['symbol']}.js", timeout=15).text
        match = re.search(r'var Data_fluctuationScale\s*=\s*(.*?);', text)
        if not match:
            raise ValueError('No scale disclosure')
        value = json.loads(match.group(1))
        frame = pd.DataFrame({'截止日期': value['categories'], '净资产规模（亿元）': [row['y'] for row in value['series']]})
    elif endpoint == 'disclosure_purchase_fee_em':
        from akshare.utils import http
        from bs4 import BeautifulSoup
        import pandas as pd
        text = http.get(f"https://fundf10.eastmoney.com/jjfl_{kwargs['symbol']}.html", timeout=15).text
        soup = BeautifulSoup(text, 'html.parser')
        headers = [h for h in soup.find_all('h4', class_='t') if h.get_text(strip=True) in ('申购费率', '申购费率（前端）')]
        if not headers:
            raise ValueError('No purchase-fee disclosure')
        frame = pd.read_html(StringIO(str(headers[0].find_next('table'))))[0]
    elif endpoint == 'disclosure_managers_em':
        from akshare.utils import http
        import pandas as pd
        text = http.get(f"https://fundf10.eastmoney.com/jjjl_{kwargs['symbol']}.html", timeout=15).text
        tables = pd.read_html(StringIO(text))
        tables = [t.rename(columns={'起始期': '任职日期', '截止期': '离任日期'}) for t in tables]
        matches = [t for t in tables if '任职日期' in t.columns and '基金经理' in t.columns]
        if not matches:
            raise ValueError('No manager tenure disclosure')
        groups = matches[0][['任职日期', '离任日期', '基金经理']].copy()
        groups['任职日期'] = pd.to_datetime(groups['任职日期'], errors='coerce')
        groups = groups.dropna(subset=['任职日期']).sort_values('任职日期', ascending=False)
        current = groups.loc[groups['离任日期'].astype(str).str.contains('至今|现任')]
        records = []
        for record in current.to_dict('records'):
            for name in re.split(r'[\s、,，]+', str(record['基金经理']).strip()):
                start = record['任职日期']
                for past in groups.loc[groups['任职日期'] < start].to_dict('records'):
                    end = pd.to_datetime(past['离任日期'], errors='coerce')
                    members = re.split(r'[\s、,，]+', str(past['基金经理']).strip())
                    if name not in members or pd.isna(end) or (start - end).days > 1:
                        break
                    start = past['任职日期']
                records.append({'任职日期': str(start.date()), '离任日期': '至今', '基金经理': name})
        frame = pd.DataFrame(records)
    else:
        frame = getattr(ak, endpoint)(**kwargs)
    if frame is None:
        raise ValueError('Provider returned None')
    # JSON handles nested provider fields; each raw response is itself auditable.
    Path(destination).write_text(frame.to_json(orient='split', date_format='iso', force_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
