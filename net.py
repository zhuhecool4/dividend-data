# -*- coding: utf-8 -*-
"""requests 的最小 stdlib 兜底（本机曾出现 requests/akshare/pandas 全部丢失）。

用法与 requests 兼容的子集：resp = get(url, timeout=20)
    resp.text     -> 按 resp.encoding 解码的文本（默认 gbk，可赋值切换）
    resp.content  -> 原始 bytes（读 xls 用）
只提供 GET。若要更完整能力，仍优先装 requests。
"""
import os
import ssl
import time
import urllib.request
import urllib.error

os.environ.setdefault('NO_PROXY', '*')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)          # 直连，避免 Clash 代理污染行情接口

_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

_CTX = ssl.create_default_context()
try:
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE
except Exception:
    pass


class Response:
    __slots__ = ('content', '_enc')

    def __init__(self, content, encoding):
        self.content = content
        self._enc = encoding

    @property
    def encoding(self):
        return self._enc

    @encoding.setter
    def encoding(self, v):
        self._enc = v or 'utf-8'

    @property
    def text(self):
        try:
            return self.content.decode(self._enc, 'replace')
        except LookupError:
            return self.content.decode('utf-8', 'replace')

    def json(self):
        import json
        return json.loads(self.text)


RETRIES = 3          # 瞬时故障重试次数（DNS 抖动/连接重置/超时）
BACKOFF = 0.8        # 退避基数秒：0.8 → 1.6 → 3.2


def get(url, timeout=20, headers=None, encoding='gbk', **kw):
    """GET，带瞬时故障重试。

    ⚠️ 2026-09-21 加：全量构建跑到一半撞上一次 DNS 抖动（getaddrinfo failed），
       整个 build_fundamentals 直接崩掉、缓存一个字没写 —— 本轮 1600 多个请求白跑。
       只重试【非 HTTP】的传输层错误：HTTPError 是 URLError 的子类，
       4xx/5xx 属于"服务端明确回答"，不该被当成抖动反复打（也避免把限流放大）。
    """
    h = dict(_UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    last = None
    for i in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return Response(r.read(), encoding)
        except urllib.error.HTTPError:
            raise                      # 服务端明确表态，立刻上抛（含 4xx/5xx）
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e                   # DNS / 连接重置 / 读超时 —— 都可能是抖动
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise last


# ---------------- 日K（多个源顺次试） ----------------
# 2026-09-21 实测（本机，当天跑过一轮全量构建之后）：
#   · `web.ifzq.gtimg.cn` 一律回 **HTTP 501**（连裸 GET 都是，不是缺 UA/Referer）；
#   · 同一套接口的 `ifzq.gtimg.cn` / `proxy.finance.qq.com` **同结构 200** ✓；
#   · 东财 push2his 那会儿开始 **RemoteDisconnected**（也是限流类的表现，隔一阵会恢复）。
# 原先两个脚本各写死一个域名，一挂整块数据就变空（build_etf_perf 那次被写成"0 只"、
# 界面上「历史表现」全空）。现在集中在这里顺次试，谁挂都不至于断供。
_QQ_KLINE = ('https://ifzq.gtimg.cn/appstock/app/fqkline/get',
             'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get',
             'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get')


def kline_day(qc, n_days):
    """日线【不复权】→ [[日期, 收盘], ...] 升序。qc 带交易所前缀（sh510880 / sz159915 / bj430047）"""
    err = None
    for host in _QQ_KLINE:
        try:
            j = get(f'{host}?param={qc},day,,,{n_days},', encoding='utf-8', timeout=30).json()
        except Exception as e:
            err = e
            continue
        d = (j.get('data') or {}).get(qc) or {}
        rows = d.get('day') or d.get('qfqday') or []
        out = []
        for r in rows:
            try:
                out.append([str(r[0]), float(r[2])])      # r[2] = 收盘
            except (IndexError, TypeError, ValueError):
                continue
        if out:
            return out
        err = err or RuntimeError('%s 返回空' % host.split('/')[2])
    # 东财兜底（F51=日期 F53=收盘）。本机当天重度抓取后它也会 RemoteDisconnected，
    # 属于限流类、过一阵会恢复 —— 留着当第 4 条路，不保证随时可用。
    mk = {'sh': '1', 'sz': '0', 'bj': '0'}.get(qc[:2])
    if mk:
        try:
            j = get('https://push2his.eastmoney.com/api/qt/stock/kline/get?'
                    f'secid={mk}.{qc[2:]}&fields1=f1&fields2=f51,f53&klt=101&fqt=0'
                    f'&end=20500101&lmt={n_days}', encoding='utf-8', timeout=30).json()
            out = []
            for s in ((j.get('data') or {}).get('klines') or []):
                p = str(s).split(',')
                try:
                    out.append([p[0], float(p[1])])
                except (IndexError, ValueError):
                    continue
            if out:
                return out
        except Exception as e:
            err = e
    if err:
        raise err
    return []
