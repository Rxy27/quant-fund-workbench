# 引擎模块保留依据

本次通过 Python AST 扫描发行包的全部 Python 源文件，检查 `import`、`from ... import ...`、显式 `load('模块名')`，并读取 `core/engine.py` 的 `_NAMES` 注册表及 `spec_from_file_location` 文件映射。扫描命中包含函数内部导入，即使当前测试分支跳过也不能当作无引用代码。

`engine.load(name)` 将注册名映射到 `core/_engine_{name}.py`，以私有模块名 `core._audited_{name}` 加载。`engine._import` 将这些旧模块中的同级绝对导入转到同一加载器，避免与标准库 `statistics` 或其他项目的 `data_loader` 混淆。仅搜索文件名的直接 import 会漏掉这些真实依赖。

| 文件 | 作用 | 实际调用方示例 |
| --- | --- | --- |
| `_engine_data_loader.py` | AkShare 数据获取、限时子进程、缓存校验与净值清洗 | `app.py:40`、`analysis.py:24`、`market_data.py:243` |
| `_engine_decision_data.py` | 将基金与组合数据整理为条件引擎输入 | `analysis.py:118` |
| `_engine_decision_rules.py` | 原有买卖条件计算，供现有适配层调用 | `decision_rules.py:10` |
| `_engine_fund_classifier.py` | 基金分类与分析口径选择 | `analysis.py:95`、`_engine_data_loader.py:150` |
| `_engine_portfolio_analysis.py` | 组合校验、相关性及角色分析 | `state.py:99`、`analysis.py:116` |
| `_engine_position_sizing.py` | 风险配置校验与新增仓位预算扫描 | `position_sizing.py:98`、`state.py:132` |
| `_engine_report_generator.py` | 条件阈值说明及 HTML 报告辅助函数 | `analysis.py:127` |
| `_engine_statistics.py` | 净值对齐、收益率、回撤及统计指标 | `analysis.py:115`、`allocation.py:96` |
| `_engine_stress_test.py` | 历史与假设情景分析 | `analysis.py:134`、`_engine_decision_data.py:85` |
| `_frozen_portfolio.py` | 冻结历史组合算法，供回归比较使用 | `verify_survival.py:12`、`tests/test_survival.py:284` |

上述 10 个文件均有实际引用，全部保留。冻结基线的私有行情快照不随发行包分发，相关历史数据验收需另行准备；代码仍被回归辅助模块引用，因此不能删除。
