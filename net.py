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


# ---------------- 腾讯行情（qt.gtimg.cn）字段解析 ----------------
# 2026-10-04 P1-5：原来 6 个脚本各自手写 `line.split('=')[1].strip('"').split('~')` +
# `len(f) > N` 那一套（写法几乎逐字重复，共 7 个解析点）。集中到这里，**字段下标一个没动**。
# 改前/改后用同一段真实返回 + 一批畸形样本逐字段比对过（见 `_verify_quote_parse.py`）。
#   f[1]=简称  f[2]=代码  f[3]=现价  f[39]=PE(TTM)  f[44]=规模(亿，ETF)
#   f[45]=总市值(亿)  f[46]=PB
# ⚠️ 下标别凭印象：东财那边 f2/f18 是「×10^f1」（S10.101 那条 ETF 价格放大 10 倍）。
#    腾讯这边的锚点在 export_ui_data.py（华域 6.74 / 0.71 / 469.13）。

def quote_fields(line):
    """一行腾讯行情 → 字段列表；不是行情行 → None。

        'v_sh600036="1~招商银行~600036~41.26~…"'  →  ['1', '招商银行', '600036', '41.26', …]

    四种老写法的统一（split('=') / split('="') / strip('"') / strip('";\\n\\r ')）：
    按 `="` 切（比按 `=` 切严：`v_x=1~a~b` 这种没有引号的残行一律当"不是行情行"），
    剥的字符是超集（引号/分号/换行/空格 —— 都是分隔符，不会碰到数据）。
    """
    if '~' not in line:
        return None
    i = line.find('="')
    if i < 0:
        return None
    return line[i + 2:].strip('";\n\r ').split('~')


def _num(f, i):
    """第 i 位转 float；越界 / 空串 / '-' / 非数字 → None（不抛）。"""
    try:
        return float(f[i])
    except (IndexError, TypeError, ValueError):
        return None


def parse_quote(line, min_n=1):
    """一行腾讯行情 → dict；不是行情行或字段数 < min_n → None。

    min_n = 原来各脚本的 `len(f) > N` 写法 + 1（>4 → 5，>45 → 46，>72 → 73）。
    ⚠️ 返回的位一律"取不到就 None"，**不判正负、不判空** —— 有效性（`f[2]` 空、PE ≤ 0
       按"不适用"）仍由调用点自己管：那些是各处的口径，不是解析的事。
        code=f[2]（空串照原样给）  name=f[1]（原样，不去空格）
        price=f[3]  pe=f[39]  pb=f[46]  cap=f[45]（总市值，亿）  size=f[44]（ETF 规模，亿）
    """
    f = quote_fields(line)
    if f is None or len(f) < min_n:
        return None
    return {'code': f[2] if len(f) > 2 else '', 'name': f[1] if len(f) > 1 else '',
            'price': _num(f, 3), 'pe': _num(f, 39), 'pb': _num(f, 46),
            'cap': _num(f, 45), 'size': _num(f, 44)}


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
