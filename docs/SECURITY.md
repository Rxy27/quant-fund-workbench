# 安全配置与升级

## 从旧版本升级

1. 备份真实配置和账户数据，执行 `git pull --ff-only`。不要重新复制模板覆盖已有账户。
2. 使用 Python 3.11+ 创建新的虚拟环境，建议 Python 3.12。安全更新所需的 Pillow 13、PyArrow 26 和新版 Streamlit 已不支持 Python 3.10。
3. 安装带哈希的锁文件：`python -m pip install --require-hashes -r requirements.lock`。
4. 将旧 `config/ai_settings.json` 的密钥迁移到以下任一服务端配置，然后从旧 JSON、备份、截图中清除真实密钥。新版忽略 JSON 的 `api_key`，保存配置时将该字段清空。程序不会自动迁移或删除已有私人文件。
5. 启动 `python -m streamlit run app.py`。默认仅监听 `127.0.0.1`。

## 密钥和服务地址

支持环境变量，优先级为 `QUANT_AI_API_KEY` > `OPENAI_API_KEY` > Streamlit secrets。环境变量 `QUANT_AI_BASE_URL`、`QUANT_AI_MODEL_ID` 分别覆盖服务地址和模型。

PowerShell 示例（占位内容请在本机替换，不要提交或截图）：

```powershell
$env:QUANT_AI_API_KEY = '<your-key>'
$env:QUANT_AI_BASE_URL = 'https://api.openai.com/v1'
python -m streamlit run app.py
```

macOS / Linux：

```sh
export QUANT_AI_API_KEY='<your-key>'
export QUANT_AI_BASE_URL='https://api.openai.com/v1'
python -m streamlit run app.py
```

或复制 `.streamlit/secrets.example.toml` 为 `.streamlit/secrets.toml` 并在本机填写：

```toml
[ai]
api_key = "<your-key>"
base_url = "https://api.openai.com/v1"
model_id = "gpt-4o-mini"
```

也兼容 secrets 顶层的 `OPENAI_API_KEY`。请勿在同一进程混用不同供应商的密钥和服务地址；自定义供应商应同时设置 key 和 base URL。未指定服务地址时使用 JSON 的 `base_url`，再回退为 OpenAI 默认地址。

服务端密钥不会回填浏览器控件。存在服务端密钥时，页面锁定服务地址和密钥输入，避免访问者更改目的地址并将服务端密钥发送给他人。更换服务地址需由管理员修改环境变量、secrets 或本地配置。

未设置服务端密钥时，仍可在密码框输入临时密钥并保存；密钥仅留在当前 Streamlit 会话内存，不写 JSON，新会话需重新输入。密码框只遮盖屏幕内容，用户输入仍会经浏览器连接传给服务端，因此远程使用必须采用 HTTPS。会话结束后由框架清理内存，不提供内存擦除保证。

启用 AI 会向选定服务发送量化上下文和用于鉴权的密钥。仅使用可信服务；未启用 AI 时可正常生成本地规则报告。

`.gitignore` 忽略 `config/*.json`，仅放行 `*.example.json` 与公共 `index_mapping.json`；还忽略 `.env*`、真实 `secrets.toml`、缓存和导出。Git 已跟踪的文件不受忽略规则保护。限制本机文件访问权限，不要将 secrets、账户、日志或导出目录提供为 Web 静态目录。

## 部署边界

默认保留 CORS、XSRF 防护并禁用静态目录服务。修改监听地址会扩大访问范围；这些配置不能代替身份认证。

该工具的账户配置、行情缓存和持仓同步文件由进程共享，**不是多用户隔离服务**。远程使用应通过受认证的 VPN/SSH 隧道，或在 TLS 反向代理后启用身份认证，仅授权同一可信账户的使用者，并通过防火墙阻止绕过代理直连 Streamlit。不同账户应使用独立进程和数据目录。不要直接将默认无登录界面暴露到公网。

基金代码、指数代码必须为六位 ASCII 数字，参与文件名构造前进行验证；缓存和配置使用固定应用目录。界面当前不提供任意文件路径或文件上传入口。沙盘会校验日期、数值和净值序列；这些检查不能证明第三方行情真实无误，结果仍需对照数据源核验。

## 依赖更新与自动检查

`requirements.txt` 是直接依赖范围；`requirements.lock` 固定完整运行依赖，包含分平台/解释器标记及发布包 SHA-256。哈希校验保证下载内容与锁文件一致，不能证明包本身可信。`requirements-dev.lock` 同时固定测试和审计工具。避免使用私人环境的 `pip freeze` 混入无关依赖。

```sh
# 在仓库根目录执行；更新版本后必须重新测试和扫描
uv pip compile requirements.txt --python-version 3.11 --universal --generate-hashes --no-emit-index-url --output-file requirements.lock
uv pip compile requirements-dev.txt -c requirements.lock --python-version 3.11 --universal --generate-hashes --no-emit-index-url --output-file requirements-dev.lock
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest
python -m pip_audit --local
```

锁文件不是漏洞修复保证。GitHub Actions 在 push、PR 和每周运行测试与依赖扫描；Dependabot 检查依赖和 Actions 更新。修改依赖范围后同步更新两个锁文件；Dependabot 创建的范围更新 PR 也需要重新生成锁文件，扫描依据实际安装的锁定依赖。

安全公告优先查阅供应商官方公告及 GitHub Advisory Database、PyPI/OSV。扫描可能涵盖 Python 包所携带的原生库公告，但不能覆盖所有 C/C++ 或系统库风险。本次原始扫描在旧约束中发现 lxml、PyArrow、Pillow 的上游公告；修复采用更新的兼容版本，未据此宣称本项目存在特定可利用 CVE，也未将反馈中未经核实的 CVE 编号当作事实。
