"""独立网络诊断，只依赖 httpx，不加载 Streamlit 或项目配置。

运行：python -m core.network_diagnostic
可选鉴权：在环境变量 FOXAPI_API_KEY 中设置密钥，勿将密钥写入脚本。
读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY；不会读取 Windows 系统代理。
退出码：0=HTTP 成功；1=网络/配置异常；2=已收到 HTTP 错误响应。
"""
import os
import re
import socket
from time import perf_counter
import traceback

import httpx


URL = 'https://api.foxapi.cn/v1/models'
PROXY_VARIABLES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')


def report_exception(exc, key):
    """保留完整异常栈及底层原因，同时遮盖 Key 和代理凭据。"""
    detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if key:
        detail = detail.replace(key, '<REDACTED>')
    for name, value in os.environ.items():
        if name.upper() in PROXY_VARIABLES and value:
            detail = detail.replace(value, '<PROXY_REDACTED>')
    detail = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@', r'\1<REDACTED>@', detail)
    print(detail, flush=True)


def main():
    key = os.environ.get('FOXAPI_API_KEY', '').strip()
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    if key:
        headers['Authorization'] = f'Bearer {key}'
    proxy_names = sorted(name for name, value in os.environ.items()
                         if name.upper() in PROXY_VARIABLES and value)
    print(f'GET {URL}\ntimeout=30s, trust_env=True', flush=True)
    print('已设置的代理环境变量（仅显示名称）: ' + (', '.join(proxy_names) or '无'), flush=True)
    if not key:
        print('未设置 FOXAPI_API_KEY；即使返回 HTTP 401，也说明已收到 HTTP 响应。', flush=True)
    started = perf_counter()
    try:
        with httpx.Client(timeout=30.0, trust_env=True, follow_redirects=False, headers=headers) as client:
            response = client.get(URL)
        body = response.text.replace(key, '<REDACTED>') if key else response.text
        print(f'HTTP {response.status_code}\n{body}', flush=True)
        return 0 if response.is_success else 2
    except httpx.TimeoutException as exc:
        print('网络连接超时：请检查代理环境变量、代理是否启动及服务响应速度。', flush=True)
        report_exception(exc, key)
    except httpx.ProxyError as exc:
        print('代理连接失败：请检查 HTTPS_PROXY 的地址、端口和代理服务。', flush=True)
        report_exception(exc, key)
    except (httpx.RequestError, socket.gaierror) as exc:
        print('网络请求失败：请根据异常链检查 DNS、连接或 TLS。', flush=True)
        report_exception(exc, key)
    except (ValueError, ImportError) as exc:
        print('客户端配置失败：请检查代理 URL、协议及依赖（SOCKS 需要 httpx[socks]）。', flush=True)
        report_exception(exc, key)
    except Exception as exc:
        print('未预期的诊断异常：', flush=True)
        report_exception(exc, key)
    finally:
        print(f'耗时: {perf_counter() - started:.2f}s', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
