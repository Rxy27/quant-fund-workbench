# 安全策略

当前 `main` 的安全修复版本要求 **Python 3.11+**，建议 Python 3.12。旧版本不再作为安全部署基线。

请使用 [安全配置与升级说明](docs/SECURITY.md) 配置密钥、安装锁文件并部署服务。

## 报告漏洞

请通过 GitHub 仓库的 **Security → Advisories → Report a vulnerability** 私密报告可复现问题：
https://github.com/Rxy27/quant-fund-workbench/security/advisories/new

该入口需要仓库管理员启用私密漏洞报告。如果入口不可用，请先用不包含敏感细节的 Issue 请求私密联系渠道。不要公开真实密钥、账户数据、敏感文件内容或可直接利用的细节。

报告中请提供受影响提交、Python/操作系统、复现步骤、影响和脱敏日志。依赖扫描结果表示存在上游安全公告，不能单独证明本项目中的漏洞可被利用；扫描通过也不等于不存在漏洞。

若密钥曾提交或公开，请立即在服务商后台撤销并更换。删除文件、添加 `.gitignore` 或删除最新提交中的密钥，都不会使 Git 历史、克隆或缓存中的旧密钥失效。
