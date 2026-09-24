# -*- coding: utf-8 -*-
"""抓池内红利 ETF 的前十大持仓（季报口径），落 cache/etf_stocks.json。

数据源：天天基金 F10（fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc）。
⚠️ 这个端点必须带 Referer，否则 404（2026-09-19 实测；缺 Referer 会被当盗链拒掉）。
   东财的 clist 接口连打几次就掐连接（见 build_etf_dir.py），但这个端点稳，
   12 只连打无恙。

每只取最新一期季报的 10 行：[股票代码, 名称, 占净值比例%]。
  · A 股成分是 6 位代码 —— 池内有的（中远海控/上海银行这些）详情页能带出
    TTM 股息率、能点进个股详情；
  · 港股成分是 5 位代码（01919 这种）—— 池里没有港股数据，只显示权重。

季报一季度才更新一次，且发布时间有法定窗口（季末后 15 个工作日内）——所以
【按披露规则推算"今天应为哪一期"，与缓存比对：一致就 0 请求直接用缓存，
落后（新季报该出了）才全量重抓】。单只失败或端点不通时沿用旧缓存。
export_ui_data.py 每次刷数据自动调用 load()；窗口外想强制重抓：--force。
用法: python build_etf_stocks.py [--force]
"""
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
os.environ['NO_PROXY'] = '*'
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)

try:
    import requests
except ImportError:                      # requests 丢失时的 stdlib 兜底（同 export_ui_data）
    import net as requests
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'cache', 'etf_stocks.json')

REF = 'https://fundf10.eastmoney.com/ccmx_%s.html'
URL = ('https://fundf10.eastmoney.com/FundArchivesDatas.aspx'
       '?type=jjcc&code=%s&topline=10&year=%d&month=3,6,9,12')


def _tds(tr):
    return [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', '').strip()
            for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]


def parse(raw):
    """从 F10 响应里取【最新一期】季报的前十大。返回 (period, rows) 或 (None, [])。
    响应里每个季度一张表，顺序最新在前；行结构是固定的九列。"""
    m = re.search(r'content:"(.*)"', raw, re.S)
    if not m:
        return None, []
    for tb in re.split(r"<div class='boxitem", m.group(1)):
        dm = re.search(r'(\d{4})年(\d{1,2})季度', tb)
        if not dm:
            continue
        rows = []
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', tb, re.S):
            cells = _tds(tr)
            # 列：序号 | 股票代码 | 股票名称 | 最新价 | 涨跌幅 | 相关资讯 | 占净值比例 | 持股数 | 市值
            if (len(cells) >= 7 and re.fullmatch(r'\d{5,6}', cells[1])
                    and cells[2] and cells[6].endswith('%')):
                rows.append([cells[1], cells[2], cells[6]])
        if rows:
            return '%s-Q%s' % (dm.group(1), dm.group(2)), rows[:10]
    return None, []


def fetch_one(code):
    r = requests.get(URL % (code, date.today().year), timeout=20,
                     headers={'Referer': REF % code})
    r.encoding = 'utf-8'
    period, rows = parse(r.text)
    return {'period': period, 'rows': rows}


def expected_period(today):
    """按披露规则推算【今天应该已公布的最新季报期】。
    季报在季末后 15 个工作日左右出 → 取【20 天前为止最近的季末】所在季：
      9/19 → 20 天前是 8/30，最近季末 6/30 → 2026-Q2
      10/25 → 10/5，最近季末 9/30 → 2026-Q3（窗口刚开，全量重抓开始）
    注意锚的是【季末日】而不是 t 所在季：8/30 在 Q3 里，但 Q3 还没结束，
    它的季报不可能存在。"""
    t = today - timedelta(days=20)
    ends = []
    for y in (t.year - 1, t.year):
        ends += [date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)]
    e = max(d for d in ends if d <= t)
    return '%d-Q%d' % (e.year, e.month // 3)


def refresh():
    """抓全部池内 ETF，结果落盘。单只失败沿用旧缓存里那只；第一只就连败
    （大概率端点不通 / 断网）→ 整批沿用旧缓存，不再空转。"""
    old = {}
    try:
        old = json.load(open(OUT, encoding='utf-8')).get('funds', {})
    except Exception:
        pass
    codes = [m['code'] for m in json.load(
        open(os.path.join(HERE, 'cache', 'etf_rows2.json'), encoding='utf-8'))]
    funds = {}
    dead = False
    for i, c in enumerate(codes):
        if dead:
            funds[c] = old.get(c) or {'period': None, 'rows': []}
            continue
        got = None
        for attempt in range(3):
            try:
                got = fetch_one(c)
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if got is None:
            if i == 0:
                print('  首个请求就连败，判定 F10 端点不通，整批沿用旧缓存')
                dead = True
            else:
                print('  %s 抓取失败，沿用旧缓存' % c)
            funds[c] = old.get(c) or {'period': None, 'rows': []}
        else:
            funds[c] = got
        ok = funds[c]['rows'] and '✓' or '✗'
        print('  [%d/%d] %s %s %s' % (i + 1, len(codes), c, ok,
              funds[c]['rows'][0][1] if funds[c]['rows'] else '无数据'))
    json.dump({'fetched_at': str(date.today()), 'funds': funds},
              open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
    return funds


def load(today=None):
    """给 export_ui_data.py 用的入口：先算期望期和缓存比对，一致就 0 请求；
    缓存缺文件、缺基金、或期数落后（新季报该出了）才全量重抓。"""
    today = today or date.today()
    want = expected_period(today)
    try:
        v = json.load(open(OUT, encoding='utf-8'))
        have = v.get('funds', {})
        codes = [m['code'] for m in json.load(
            open(os.path.join(HERE, 'cache', 'etf_rows2.json'), encoding='utf-8'))]
        # 只比对有数据的基金（period 为 None 的是本就无季报的新基金，跳过）。
        # 比对用 >= 不用 ==：天天基金对部分 ETF 会提前挂出下一季的表（预填当前
        # 持仓），缓存期数可能比推算的还新 —— 比期望新也算最新，否则永远判落后。
        ok = set(have) >= set(codes) and all(
            (have[c] or {}).get('period', '') >= want
            for c in codes if (have.get(c) or {}).get('rows'))
        if ok:
            print('ETF 持仓缓存已是最新（%s），跳过重抓' % want)
            return v
        cached = sorted({(have[c] or {}).get('period') for c in codes
                         if (have.get(c) or {}).get('rows')} - {None})
        print('ETF 持仓缓存落后（缓存 %s，应为 %s），重抓 …' % (
            '/'.join(cached) or '空', want))
    except Exception:
        print('etf_stocks.json 不存在，抓取 …')
    refresh()
    return json.load(open(OUT, encoding='utf-8'))


if __name__ == '__main__':
    if '--force' in sys.argv:
        print('--force：跳过期数比对，直接重抓')
        refresh()
    else:
        load()
    print('✅ cache/etf_stocks.json 就绪')
