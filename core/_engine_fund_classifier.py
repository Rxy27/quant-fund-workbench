"""Evidence-based classification. No code-to-type guesses or performance labels."""
from dataclasses import dataclass, asdict
import re


@dataclass
class Classification:
    category: str
    subtype: str
    evidence: list[str]
    active: bool = False

    def to_dict(self):
        return asdict(self)


def classify(info, holdings=None):
    name = ' '.join(str(info.get(k, '')) for k in ('基金名称', '基金全称', '基金简称'))
    kind = str(info.get('基金类型', ''))
    text = name + ' ' + kind
    evidence = [f'名称字段：{name or "数据不可得"}', f'akshare基金类型字段：{kind or "数据不可得"}']
    evidence.append('持仓特征：' + (str(holdings) if holdings else '持仓数据不可得；未依据缺失持仓推断类型'))
    def result(category, subtype='', active=False):
        return Classification(category, subtype or category, evidence, active)
    if '货币' in kind and re.search(r'基础设施|REIT|沪深300|中债|股票|黄金|债券', name, re.I):
        evidence.append('名称资产类别与货币型字段冲突，不能仅凭关键词覆盖冲突。')
        return result('无法判定', '无法判定，需人工确认')
    if re.search(r'QDII|境外|海外|全球|恒生|标普|纳斯达克|道琼斯', text, re.I):
        return result('QDII', '境外基金；具体资产以合同为准')
    if re.search(r'FOF|基金中基金', text, re.I):
        return result('FOF')
    if re.search(r'REIT|不动产投资信托|基础设施', text, re.I):
        return result('REITs')
    if re.search(r'黄金|白银|原油|商品|有色金属期货', text):
        return result('商品型')
    if re.search(r'货币|现金管理', text):
        return result('货币型')
    if re.search(r'债券|纯债|国开|国债|政策性金融债|信用债|可转债|债券指数|中债', text):
        if re.search(r'偏债混合|混合型-偏债', kind):
            return result('混合型', '偏债混合', True)
        return result('债券型', '可转债' if '可转债' in text else ('债券指数' if re.search(r'指数|中债', text) else '债券；细分需核对合同'))
    if re.search(r'指数|ETF|沪深300|中证\d|上证\d', text, re.I):
        return result('指数型', '指数增强型' if '增强' in text else ('ETF 联接' if '联接' in text else '被动指数'))
    if re.search(r'股票型|偏股|混合型-偏股|普通股票', kind):
        return result('主动权益型', '偏股混合' if '偏股' in kind else '普通股票型', True)
    if re.search(r'灵活配置|平衡混合|偏债混合|混合', kind):
        return result('混合型', kind, True)
    return result('无法判定', '无法判定，需人工确认')
