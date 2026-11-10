# 有数 · 基金分析工作台

> **环境要求：必须 Python >= 3.11，建议 Python 3.12。** 使用独立虚拟环境。

本次安全更新升级了 Streamlit、Pillow、PyArrow 和 lxml，并提供带 SHA-256 哈希的完整依赖锁文件。Python 3.10 已无法安装所需安全更新。已有用户请先阅读 [安全配置与升级](docs/SECURITY.md)，保留自己的账户配置。

基于 Streamlit 的基金研究与持仓工作台，支持 Windows、macOS、Linux。

![主界面](assets/main.png)

## 🎯 适用人群与产品边界

**✅ 适合你，如果：**

- 你认可“先控风险，再求收益”，偏好稳健、低回撤的长期复利。
- 你希望用数据（PE分位、资金池、XIRR）来克服追涨杀跌的人性弱点。

**❌ 不适合你，如果：**

- 你是极致的进攻型选手，追求短期暴富、重仓博弈。
- 你期望软件能“预测明天必涨”或“全自动一键下单”。

*(注：本项目定位为“保守型量化风控终端”。)*

## 三个核心亮点

1. **11 条买卖条件引擎**：结合估值、趋势、费率、风险预算与组合角色给出依据；数据不足明确显示“无法判定”，支持导出分析结果与交易指令试算。
2. **历史估值分位智能定投沙盘**：比较智能定投、普通定投与一次性买入，展示资金池、最大回撤，并按实际现金流日期计算 XIRR。
3. **实时行情与动态市值同步**：接入 AkShare 股票/ETF 行情及 ETF 定向报价，支持请求冷却和失败降级；持仓区分官方净值校准与盘中临时估算，避免将估算当作已确认金额。

### 历史沙盘推演

设置回溯区间与月度预算，核对当时的估值分位和计划投入倍数，再结合 AI 投资教练复盘模拟结果。

![历史沙盘推演：回溯设置与当时买入依据](assets/sandbox1.png)

![历史沙盘推演：AI 投资教练复盘](assets/sandbox2.png)

### 动态市值同步

核对持仓份额、官方净值日期和同步反馈，将盘中临时估算与官方校准结果分开阅读。

![动态市值同步：官方净值校准反馈与基金分析](assets/sync.png)

## Quick Start：快速启动三步走

👉 **[📖 点击查看详细使用说明书 (User Manual)](docs/USER_MANUAL.md)**

在本目录打开终端。建议先创建并激活 Python 虚拟环境，确保 `pip` 和 `streamlit` 使用同一个 Python。

### 1. 安装核心依赖

```sh
python -m pip install --require-hashes -r requirements.lock
```

`requirements.txt` 维护直接依赖范围，日常安装使用 `requirements.lock` 固定所有传递依赖并校验下载哈希。修改依赖后需重新生成运行和开发锁文件、运行测试并扫描漏洞，具体命令见安全说明。

### 2. 复制全部配置模板

**首次启动前执行一次。命令会覆盖同名真实配置；已有账户请先备份，不要在每次启动时重复执行。**

Windows（命令提示符 CMD，使用 `copy`）：

```bat
copy config\ai_settings.example.json config\ai_settings.json
copy config\user_portfolio.example.json config\user_portfolio.json
copy config\risk_budget.example.json config\risk_budget.json
copy config\ui_defaults.example.json config\ui_defaults.json
copy config\portfolio.example.json config\portfolio.json
copy config\holdings.example.json config\holdings.json
copy config\rule_parameters.example.json config\rule_parameters.json
copy config\scenarios.example.json config\scenarios.json
copy config\mappings.example.json config\mappings.json
copy config\intraday_etf_mapping.example.json config\intraday_etf_mapping.json
```

macOS / Linux（使用 `cp`）：

```sh
cp config/ai_settings.example.json config/ai_settings.json
cp config/user_portfolio.example.json config/user_portfolio.json
cp config/risk_budget.example.json config/risk_budget.json
cp config/ui_defaults.example.json config/ui_defaults.json
cp config/portfolio.example.json config/portfolio.json
cp config/holdings.example.json config/holdings.json
cp config/rule_parameters.example.json config/rule_parameters.json
cp config/scenarios.example.json config/scenarios.json
cp config/mappings.example.json config/mappings.json
cp config/intraday_etf_mapping.example.json config/intraday_etf_mapping.json
```

请复制全部模板：除账户、风险和 AI 设置外，界面默认值、规则参数、压力情景、组合设置与盘中 ETF 映射也是当前代码的配置依赖。`config/index_mapping.json` 已随包提供，用于指数估值映射，请保留。

模板账户金额为 0、持仓和成本记录为空、AI 默认关闭且 API Key 为空。程序只读取真实 `.json`；不会自动改读 `.example.json`。可在界面填写持仓及风险设置。AI 密钥使用环境变量或 Streamlit secrets；界面临时输入仅在当前会话有效，不写入 JSON，旧 JSON 密钥不再读取。详见 [安全配置](docs/SECURITY.md)。

### 3. 启动应用

```sh
streamlit run app.py
```

按终端提示打开浏览器。此发行包入口为 Streamlit 工作台。默认空账户可直接进入界面；开始分析前填写自己的持仓或基金代码。首次使用没有本地行情缓存：联网分析需关闭“仅使用本地缓存”；历史沙盘可按界面提示联网获取。请求失败会显示缺失或降级状态，不会伪造行情。

默认仅监听 `127.0.0.1`。远程使用需要受认证的隧道或带 TLS 和身份认证的反向代理，并禁止绕过代理直连。应用使用共享本地账户文件，不提供多用户数据隔离，不能直接作为公网多用户服务。

## 配置、缓存与测试

- `core/`、`ui/` 和 `app.py` 来自本次打包时的当前源码，包含最新实时行情模块；`core/templates/` 为 HTML 导出所需模板。
- `config/` 只发布脱敏模板和公共指数映射。账户、成本、净值快照、日志与历史导出均不随包分发；运行时缓存和输出由程序按需创建。
- 已提供 `.gitignore`，默认忽略真实 JSON 配置、环境变量文件和 secrets，只保留模板与公共指数映射。测试中的金额、报价及假密钥是合成测试夹具，不是用户数据。
- 安装测试依赖后，在本目录直接执行无参数 `pytest`。`pytest.ini` 仅收集 `tests/` 和 `core/tests/`；测试配置从脱敏模板复制到临时目录，业务代码仍只读取真实 `.json`，测试不读取或修改用户账户配置。

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest
python -m pip_audit --local
```

本次安全更新已在 Windows 的 Python 3.11、3.12 独立环境验证安装、依赖一致性和测试；安全检查覆盖密钥持久化、服务端密钥回显/目的地址、会话隔离、路径输入与监听配置。GitHub Actions 另覆盖 Ubuntu/Windows 两个平台。`pip-audit` 的结果只代表查询时已收录的上游公告。

需要私有历史快照或独立旧账本的测试显式跳过。`core/` 中的浏览器验收辅助脚本需要额外安装 Playwright 和浏览器，未列入工作台核心依赖；历史缓存验收脚本需要用户自行获取行情，不属于开箱启动流程。

## 免责声明

*注：AkShare 免费接口在自检时若长时间无响应，可直接按 Ctrl+C 中断，不影响主程序的正常启动与使用。*

此提示针对独立运行的行情自检命令；主程序仍按上面的 `streamlit run app.py` 启动。

仅供研究和学习，不构成投资建议或收益承诺，不自动下单。行情、基金净值和估值可能延迟、缺失、限流或修订；盘中估算不是最终净值，也不是可成交价格。ETF 代理涨跌幅不代表对应基金的实际涨跌幅。历史收益、XIRR、回撤和情景分析不能预测未来，未必涵盖全部费用、分红及交易约束。请独立核对数据并自行承担决策风险。启用外部 AI 会向所选服务商发送当前分析上下文，请根据自己的隐私要求决定是否启用。

## 📸 功能展示

AI 决策解读、指标翻译、基金体检与买卖纪律一览：

<div align="center">
  <img src="assets/feature_showcase_1.png" alt="AI 大白话总结与本月操作决策" width="45%" style="margin: 5px;"/>
  <img src="assets/feature_showcase_2.png" alt="硬核指标翻译与未来盯盘重点" width="45%" style="margin: 5px;"/>
  <br>
  <img src="assets/feature_showcase_3.png" alt="基金历史收益风险指标与净值曲线" width="45%" style="margin: 5px;"/>
  <img src="assets/feature_showcase_4.png" alt="买入条件 B1 至 B6 的状态与原始依据" width="45%" style="margin: 5px;"/>
</div>

## 开源协议

[MIT License](LICENSE)。第三方行情数据与接口的使用还需遵守相应供应商条款。
