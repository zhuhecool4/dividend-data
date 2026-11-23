# -*- coding: utf-8 -*-
"""
红利 ETF 筛选 v2：输出「老牌红利 ETF」与「港股红利 ETF」两张推荐表。

在 v1（全量扫描）基础上补三件事：
  1. 双口径股息率
       TTM  = 近 365 天实际每份分红 ÷ 现价            （分红历史满 1 年时最可靠）
       ANNU = 最近一次单期分红 × 年频次 ÷ 现价         （分红历史不足 1 年时补充，避免低估）
  2. 状态标记
       正常分红 / 停分（末次分红 > 400 天前）/ 不分红（无有效分红记录）
  3. 补充元数据：跟踪指数、管理费率、托管费率（东财 jbgk 页）

数据源：
  腾讯行情 qt.gtimg.cn（现价、规模）｜新浪 fund_etf_dividend_sina（累计分红）
  东财 fund.eastmoney.com/pingzhongdata（净值起点=近似成立日）、f10/jbgk_（指数/费率）
"""
import os, sys, re, json, time, statistics, datetime
sys.stdout.reconfigure(encoding='utf-8')
os.environ['NO_PROXY'] = '*'
for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(k, None)

try:
    import requests
except ImportError:              # 本机没装 requests（见 net.py 的说明），用 stdlib 兜底
    import net as requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache')
# ⚠️ 2026-09-20 改：原来是写死的 datetime.date(2026, 9, 16)（一次性脚本留下的快照日期）。
#    days_since / status（停分）/ years 全靠它算 —— 冻在过去会让"距上次分红"永远不涨、
#    "成立年限"永远不增，跑久了 etf_rows2.json 里的状态就是错的。用运行当天。
TODAY = datetime.date.today()
UA = {'User-Agent': 'Mozilla/5.0'}

pool = json.load(open(os.path.join(CACHE, 'etf_pool.json'), encoding='utf-8'))
DIV = json.load(open(os.path.join(CACHE, 'etf_div.json'), encoding='utf-8'))
FOUND = json.load(open(os.path.join(CACHE, 'etf_found.json'), encoding='utf-8'))

META_CACHE = os.path.join(CACHE, 'etf_meta.json')
META = json.load(open(META_CACHE, encoding='utf-8')) if os.path.exists(META_CACHE) else {}


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            return requests.get(url, timeout=25, headers=UA)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ---------- 补齐元数据：跟踪指数 + 费率 ----------
todo = [c for c in pool if c not in META]
if todo:
    print(f'抓指数/费率 {len(todo)} 只 ...')
    for i, c in enumerate(todo, 1):
        rec = {'idx': None, 'mgmt': None, 'cust': None}
        try:
            r = get(f'https://fund.eastmoney.com/f10/jbgk_{c}.html', tries=2)
            r.encoding = 'utf-8'
            t = re.sub(r'\|+', '|', re.sub(r'<[^>]+>', '|', r.text))
            m = re.search(r'跟踪标的[：:]?\|*([^|]{0,60})', t)
            if m:
                rec['idx'] = m.group(1).strip()
            m = re.search(r'管理费率[：:]?\|*([\d.]+%)', t)
            if m:
                rec['mgmt'] = m.group(1)
            m = re.search(r'托管费率[：:]?\|*([\d.]+%)', t)
            if m:
                rec['cust'] = m.group(1)
        except Exception:
            pass
        META[c] = rec
        if i % 25 == 0:
            json.dump(META, open(META_CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
            print(f'  {i}/{len(todo)}')
        time.sleep(0.2)
    json.dump(META, open(META_CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'元数据已缓存 {len(META)} 只')

FREQ_N = {'月度': 12, '季度': 4, '半年': 2, '年度': 1}


def clean(name):
    return re.sub(r'^(XD|DR|XR)', '', name)


def analyze(code):
    p = pool.get(code)
    rec = DIV.get(code) or []
    if not p:
        return None
    ev = []
    prev = 0.0
    for d, cum in sorted(rec, key=lambda x: x[0]):
        delta = round(cum - prev, 6)
        prev = cum
        if delta > 1e-6:
            ev.append((datetime.date(*map(int, d.split('-'))), delta))
    m = META.get(code, {})
    found = FOUND.get(code)
    base = {
        'code': code, 'name': clean(p['name']), 'idx': m.get('idx'),
        'mgmt': m.get('mgmt'), 'price': p['price'], 'size': p['size'],
        'found': found,
        'years': round((TODAY - datetime.date(*map(int, found.split('-')))).days / 365.25, 1) if found else None,
    }
    if not ev:
        base.update(status='不分红', ttm=0.0, main=0.0, freq='-', n=0,
                    first=None, last=None, hist=[])
        return base

    cut = TODAY - datetime.timedelta(days=365)
    ttm = sum(v for d, v in ev if d >= cut)
    gaps = [(ev[i + 1][0] - ev[i][0]).days for i in range(len(ev) - 1)] if len(ev) >= 3 else []
    g = statistics.median(gaps) if gaps else None
    freq = ('月度' if g and g <= 45 else '季度' if g and g <= 130 else
            '半年' if g and g <= 250 else '年度' if g and g <= 500 else
            '不定期' if g else '-')
    days_since = (TODAY - ev[-1][0]).days
    left = (TODAY - ev[0][0]).days                     # 分红历史长度
    ann = ev[-1][1] * FREQ_N[freq] if freq in FREQ_N else None

    if not ev:
        status = '不分红'
    elif days_since > 400 or (left > 400 and ttm == 0):
        status = '停分'
    elif left < 365 and ann:
        status = '正常（历史不足1年）'
    else:
        status = '正常'
    # 主口径：分红历史不足 1 年 → 年化；否则 → 近 365 天实际
    main = ann if (left < 365 and ann) else ttm
    if status == '停分' or status == '不分红':
        main = ttm

    base.update(status=status, ttm=round(ttm / p['price'] * 100, 2),
                ann=round(ann / p['price'] * 100, 2) if ann else None,
                main=round(main / p['price'] * 100, 2),
                per=round(ev[-1][1], 4), freq=freq, n=len(ev),
                since=days_since, first=str(ev[0][0]), last=str(ev[-1][0]),
                hist=[[str(d), round(v, 4)] for d, v in ev[-10:]])
    return base


ROWS = [a for a in (analyze(c) for c in pool) if a]
ROWS.sort(key=lambda x: -x['size'])
json.dump(ROWS, open(os.path.join(CACHE, 'etf_rows2.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)

print()
print('=' * 108)
print(f'{"代码":<8}{"名称":<22}{"规模亿":>8}{"成立":>11}{"年":>5}{"主股息率":>9}{"TTM":>7}{"频率":>6}{"状态":<14}')
print('=' * 108)
for m in ROWS:
    print(f"{m['code']:<8}{m['name']:<22}{m['size']:>8.1f}{str(m['found']):>11}"
          f"{str(m['years']):>5}{m['main']:>8.2f}%{m['ttm']:>6.2f}%{m['freq']:>6}  {m['status']}")
print(f'\n共 {len(ROWS)} 只')
