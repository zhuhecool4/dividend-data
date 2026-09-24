# -*- coding: utf-8 -*-
"""导出 UI 原型数据：只读本地缓存（指数池/行业/分红历史）+ 腾讯实时价"""
import os, re, sys, json, time, statistics
sys.stdout.reconfigure(encoding='utf-8')
os.environ['NO_PROXY'] = '*'
for k in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY'):
    os.environ.pop(k, None)
try:
    import requests
except ImportError:                      # 依赖丢失时的 stdlib 兜底
    import net as requests
from datetime import date, timedelta
from config import (OVERRIDE, EXCLUDE, FORCE_BASE, QUAL_ADD, SHORT, sn, qualifies, fails, POOL_MIN,
                    DIV_EX, DIV_PER, DIV_RP, DIV_REC, DIV_PLAN, DIV_PROG,
                    div_is_paid, div_is_pending,
                    div_is_scheduled, div_is_undated)     # 人工口径 + 分红行结构唯一真源

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY, YEAR = date.today(), date.today().year
def Jf(p): return json.load(open(os.path.join(HERE, 'cache', p), encoding='utf-8'))

def _qf(f, i):
    """腾讯行情第 i 个字段转 float。空串/'-' 都是缺失（停牌或亏损股 PE 会给负）。
    ⚠️ 亏损股的 PE 是负数，界面要按 <=0 当"不适用"，别当"超便宜"渲染。"""
    try:
        v = float(f[i])
        return v if v > 0 else None
    except (IndexError, TypeError, ValueError):
        return None

# divhist.json（旧"除息年"口径）已弃用：yield2.json 覆盖了全部有效标的，
# 保留兜底只会在新代码漏抓时静默给出错年份的股息率。见 开发文档.md S7。
pool = Jf('pool.json')
IND  = Jf('industry.json')
# ⚠️ 这里曾经读 E:\dev\stock-app\www\data.js 拿名称和每股分红。本工程要独立出包，
#    不能再依赖另一个 App：名称改由下面的腾讯行情填充（返回字段 f[1] 就是证券简称），
#    每股分红用 yield2.json（口径还更准）。见 开发文档.md。
NAME = {}

# ---- 人工口径（OVERRIDE/EXCLUDE/FORCE/SHORT）全部来自 config.py，
#      与 by_sector.py 共享同一份，不再各存一份手工同步 ----
for c, ind in OVERRIDE.items(): IND[c] = ind

# ---- 人工补充池：与 by_sector.py 同一份 cache/extra_pool.json ----
EXTRA_PATH = os.path.join(HERE, 'cache', 'extra_pool.json')
EXTRA = json.load(open(EXTRA_PATH, encoding='utf-8')) if os.path.exists(EXTRA_PATH) else {}
for c, e in EXTRA.items():
    pool.setdefault(c, []).extend(e['tags'])
    IND[c] = e['industry']
    NAME[c] = e['name']
FORCE = FORCE_BASE | set(EXTRA)          # 人工补充池一律视为人工纳入
print(f'人工补充池 {len(EXTRA)} 只：' + '、'.join(f'{c} {e["name"]}' for c, e in EXTRA.items()))

# ---- 实时价 ----
# 池内全部取价（405 只 = 7 个批次）。以前用"有分红才取"省请求，而那个判断要靠
# 外部工程的 divs 表；为省这几毫秒引入跨工程依赖不划算，而且漏取会让标的静默消失。
ALL = sorted(pool)
PRICE = {}
# 估值三件套（详情页「估值与价格位置」用）：和价格同一次请求里就有，**零新增请求**。
# 字段下标已用独立锚点核对过：
#   f[39] 市盈率TTM   华域 6.74
#   f[46] 市净率      华域 0.71 ← 用财报 BPS 21.297 反算 = 0.699，对得上
#   f[45] 总市值(亿)  华域 469.13 ← 31.53 亿股 × 14.88，对得上
# ⚠️ 别用「现价 ÷ PB」推每股市净率再反算每股净资产：腾讯的 PB 只有两位小数，
#    反出 20.96 而财报真值 21.30，界面上会出现两个自相矛盾的数。
QUOTE = {}
for i in range(0, len(ALL), 60):
    q = ','.join(('sh' if c[0] in '65' else 'sz') + c for c in ALL[i:i+60])
    for attempt in range(4):
        try:
            r = requests.get('https://qt.gtimg.cn/q=' + q, timeout=20); r.encoding = 'gbk'; break
        except Exception:
            if attempt == 3: continue
            time.sleep(2 * (attempt + 1))
    else:
        continue
    for line in r.text.strip().split(';'):
        if '~' in line:
            f = line.split('=')[1].strip('"').split('~')
            if len(f) > 4 and f[2]:
                PRICE[f[2]] = float(f[3])
                NAME.setdefault(f[2], f[1])     # f[1]=证券简称；人工补充池的名称优先
                QUOTE[f[2]] = {'pe': _qf(f, 39), 'pb': _qf(f, 46), 'cap': _qf(f, 45)}
print(f'价格 {len(PRICE)} 只（含估值 {len(QUOTE)} 只）', flush=True)

YEAR_NOW = YEAR
# 口径同 rebuild_div.py：TTM = 最近年报 + 最近中报（年度周期重构）
#                      AVG3 = 近 3 个完整会计年度，按【分红所属报告期】归集
Y2 = Jf('yield2.json') if os.path.exists(os.path.join(HERE, 'cache', 'yield2.json')) else {}
print(f'新口径覆盖 {len(Y2)} 只')
# 近5年股息率分位（python build_yield_hist.py 生成；缺失时 ypct=None，界面隐藏该行）
YHIST = Jf('yield_hist.json') if os.path.exists(os.path.join(HERE, 'cache', 'yield_hist.json')) else {}
print(f'股息率分位覆盖 {len(YHIST)} 只')
# 基本面（现金流覆盖等）：由 build_fundamentals.py 独立产出。
# 拆成单独文件是为了让它能与本脚本并行开发 —— 这里只做"有就读、没有就 None"的挂载，
# 字段口径见 基本面-契约冻结-给WorkBuddy.md。缺失时界面自动隐藏相关行，不影响主流程。
FUND = Jf('fundamentals.json') if os.path.exists(os.path.join(HERE, 'cache', 'fundamentals.json')) else {}
print(f'基本面覆盖 {len(FUND)} 只')
# 公司概况 + 主营构成（build_profile.py 产出）—— 详情页「公司简介 · 主营构成」用
PROFILE = Jf('profile.json') if os.path.exists(os.path.join(HERE, 'cache', 'profile.json')) else {}
print(f'公司概况覆盖 {len(PROFILE)} 只')
def metrics(code):
    m2 = Y2.get(code)
    if not m2: return None               # 只认新口径；无覆盖的标的直接不进池（不再退回旧口径）
    by = m2.get('by_rp') or {}
    p = m2.get('price') or PRICE.get(code)
    if not p or not by: return None
    ys = sorted(int(y) for y in by if by[y] > 0)
    if not ys: return None
    avg3, ttm = m2['avg3'], m2['ttm']
    latest, streak, y = ys[-1], 0, ys[-1]
    while by.get(str(y), 0) > 0: streak += 1; y -= 1
    # 波动率窗口只取【完整会计年度】：当年（如 2026）可能只有中期分红、年度不完整，
    # 混进去会把稳定型股票的 cv 推高。实测分众传媒 0.43→0.10、隧道股份 0.44→0.19，
    # 全池 64 只含当年中期分红的股票受影响。
    cv_end = min(latest, YEAR_NOW - 1)
    v5 = [by.get(str(y), 0.0) for y in range(cv_end-4, cv_end+1)]
    mean = sum(v5) / 5
    cv = (sum((x-mean)**2 for x in v5)/5)**0.5/mean if mean > 0 else 9.9
    ratio = ttm/avg3 if avg3 > 0 else 0
    # 派息率 = 每股分红 ÷ 每股收益（EPS 取自东财同一接口的 BASIC_EPS）。
    # EPS<=0（亏损，如建发股份 -3.91）时不计算 —— 分母为负会得出负派息率，无意义。
    # ⚠️ 这里的 ttm 是【比率】(4.27)，不是每股金额 —— 必须先还原成金额再除 EPS。
    # 写成 ttm/eps*100 会得到 160% 这种离谱值（神华实测踩过）。
    eps = m2.get('eps')
    payout = round(ttm * p / eps, 1) if eps and eps > 0 else None
    # 近 5 年股息率分位 + 价格位置（build_yield_hist.py 产出；缺了不影响主流程）
    yh = YHIST.get(code) or {}
    q = QUOTE.get(code) or {}
    return dict(code=code, name=NAME.get(code, code), price=p, avg3=avg3, ttm=ttm,
                # 每股 TTM 分红（元）。⚠️ yield2 里的 ttm 是【百分数】（招行 4.97，不是 2.016），
                # 所以这里还原成金额：dps = ttm% × 现价 ÷ 100。详情页的「买点对照」要用它
                # 反算"想拿 6% 该在什么价位买" —— 口径只有这一处，别在 App 里现乘除。
                dps=round(ttm * p / 100, 4),
                streak=streak, cv=cv, ratio=ratio, ind=sn(IND.get(code, '未分类')),
                forced=code in FORCE, payout=payout, eps=eps,
                ypct=yh.get('pct'), yseries=yh.get('series'),
                # 估值三件套（腾讯行情同一次请求里多读的三列）；亏损股 PE 为 None
                pe=q.get('pe'), pb=q.get('pb'), cap=q.get('cap'),
                # 价格位置：{y5, y1, tot}；老缓存没有 px 时为 None，界面隐藏那一块
                px=yh.get('px'),
                # 财务 5 年序列 + 每股净资产在 fund 里（trend / bps），不另开顶层键 ——
                # 它们和覆盖倍数同源同一次抓取，分开挂只会有"一半有、一半没有"的状态
                fund=FUND.get(code),          # None = 未接入，界面隐藏
                org=PROFILE.get(code))        # 公司概况/主营构成，None = 未接入

ROWS = [m for m in (metrics(c) for c in pool) if m and m['code'] not in EXCLUDE]
# 人工纳入的不再混进老登池：它们本来就是「四道过滤没全过、被人工放行」的，
# 混在一起会让「吃息老登 96 只」名不副实。单独成组，UI 上是「自加入」。
QUAL = [m for m in ROWS if qualifies(m) or m['code'] in QUAL_ADD]
SELFADD = [m for m in ROWS if m['code'] in FORCE]

# ---- 吃息分 & 风格标签 ----
def score(m):
    s_yield = min(m['avg3']/8.0, 1) * 40                  # 收益 40
    s_stab  = max(0, 1 - m['cv']/0.5) * 25                # 稳定 25
    s_last  = min(m['streak']/20.0, 1) * 20               # 持续 20
    s_grow  = min(max(m['ratio'], 0)/1.2, 1) * 15         # 成长 15
    s = s_yield + s_stab + s_last + s_grow
    if m['ratio'] < 0.85: s *= 0.75                       # 当期分红回落 → 降权，避免占据前排
    return round(s, 1)

def tag(m):
    if m['ratio'] < 0.6:   return ('崩塌预警', 'warn')
    if m['ratio'] < 0.85:  return ('分红回落', 'fall')
    if m['cv'] > 0.35:
        # 波动大又当期远高于历史均值 = 派息是从低基数突然跳上来的，
        # 与"周期股高波动"是两种风险，分开标注
        return ('分红突增', 'jump') if m['ratio'] >= 1.5 else ('高波动', 'cycle')
    if m['ratio'] >= 1.15: return ('成长分红', 'grow')
    if m['cv'] <= 0.15 and m['streak'] >= 15: return ('稳健老登', 'rock')
    return ('稳健分红', 'steady')

# 所有入池标的都算分、打标签 —— 不只是老登和全量。
# 「自加入」要从池内任意标的里挑（长江电力、贵州茅台这类过不了四道过滤的），
# 只给两个池子赋值的话，那些记录导出去会缺 score/tag，gen_app 的 slim() 直接 KeyError。
for m in ROWS:
    m['score'] = score(m)
    m['tag'], m['tagKey'] = tag(m)
QUAL.sort(key=lambda x: -x['score'])

# 逐年派息明细（详情页用）
def hist(code, n=6):
    by = (Y2.get(code) or {}).get('by_rp') or {}
    ys = sorted((int(y) for y in by), reverse=True)[:n]
    return [{'y': y, 'v': round(by[str(y)], 4)} for y in sorted(ys)]

# 全量表（≥4%）
# ---- 全量池：近3年均 ≥4% 的高息名单。2026-09-19 起【排除老登池成员】——
#      两榜差异化：老登=又高又稳（四道全过+免试），这里=高息但有瑕疵
#      （卡在波动/年限/当期回落，46 只的分布见 2026-09-19 复核）。
#      温度卡的「≥4% 池」口径不变：前端用 qual ∪ broad 再滤 avg3≥4 计算，
#      数字与排除前完全一致（见 renderTemp）。
QUAL_CODES = {m['code'] for m in QUAL}
BROAD = sorted([m for m in ROWS if m['avg3'] >= 4 and m['code'] not in QUAL_CODES],
               key=lambda x: -x['avg3'])
# score/tag 已在上面按 ROWS 统一赋过，这里不再重复

# ---- 红利 ETF：口径同 etf_short.py v3（三口径取最小 + 节奏门槛）----
ETF_ROWS = {m['code']: m for m in Jf('etf_rows2.json')}
ETF_DIV = Jf('etf_div.json')

def etf_events(code):
    out, prev = [], 0.0
    for d, cum in sorted(ETF_DIV.get(code) or [], key=lambda x: x[0]):
        delta = round(cum - prev, 6)
        prev = cum
        if delta > 1e-6:
            out.append((date(*map(int, d.split('-'))), delta))
    return out

def etf_build(code):
    m = ETF_ROWS.get(code)
    if not m: return None
    ev = etf_events(code)
    if len(ev) < 2: return None
    price = m['price']
    gap = (TODAY - ev[-1][0]).days
    hist = (TODAY - ev[0][0]).days
    gaps = [(ev[i+1][0]-ev[i][0]).days for i in range(len(ev)-1)]
    med = statistics.median(gaps)
    gap_cv = statistics.pstdev(gaps)/statistics.mean(gaps) if len(gaps) > 1 else 0.0
    N = max(1, min(12, round(365/med)))
    freq = ('月度' if N >= 10 else '双月' if N >= 5 else '季度' if N >= 3
            else '半年' if N == 2 else '年度')
    W = max(1, N // 2)
    cut = TODAY - timedelta(days=365)
    ttm = sum(v for d, v in ev if d >= cut) / price * 100
    cyc = (sum(v for _, v in ev[-N:]) / price * 100) if len(ev) >= N else None
    rec = statistics.mean([v for _, v in ev[-W:]]) * N / price * 100
    trend = None
    if len(ev) >= W * 2:
        a = statistics.mean([v for _, v in ev[-W:]])
        b = statistics.mean([v for _, v in ev[-W*2:-W]])
        if b > 1e-9: trend = a / b
    cand = [x for x in (ttm, cyc, rec) if x is not None and x > 0]
    sort_val = round(min(cand), 2) if cand else 0.0
    return {'code': code, 'name': m['name'], 'mgmt': m['mgmt'], 'price': price,
            'size': m['size'], 'years': m['years'], 'freq': freq, 'n': len(ev),
            'hist': hist, 'stale': gap > med * 1.35, 'gap_cv': round(gap_cv, 2),
            'ttm': round(ttm, 2), 'rec': round(rec, 2), 'sort': sort_val,
            'trend': round(trend, 2) if trend else None,
            # 逐笔分红（详情页柱图用）。ETF_DIV 是累计值，etf_events 已差分过。
            'events': [[str(d), round(v, 4)] for d, v in ev[-12:]],
            'med_gap': int(med),
            'hk': bool(m['idx'] and ('港股' in m['idx'] or '恒生' in m['idx']))}

ETF_ALL = [x for x in (etf_build(c) for c in ETF_ROWS) if x]

# ---- ETF 历史表现（build_etf_perf.py 产出）：详情页「历史表现 · 含息」折叠区用。
#      含 近1/3/5 年含息回报（不复投 + 复投两个口径）、年化、最大回撤、当前距高点。
#      缺失就是 None，界面整块不渲染（同 fund/px/org 的做法）。
_EP = Jf('etf_perf.json') if os.path.exists(os.path.join(HERE, 'cache', 'etf_perf.json')) else {}
PERF = _EP.get('perf') or {}
# 同期基准（沪深300 / 中证红利）—— 只能用价格指数，全收益版接口拿不到（见 build_etf_perf.py）
BENCH = _EP.get('bench') or {}
print(f'ETF 历史表现覆盖 {len(PERF)} 只（数据日 {_EP.get("date", "—")}）；'
      f'同期基准 {len(BENCH)} 个：' + '、'.join(v.get('name', '') for v in BENCH.values()))
for _m in ETF_ALL:
    _m['perf'] = PERF.get(_m['code'])

# ---- ETF 前十大持仓（季报，详情页「成分股介绍」用）：见 build_etf_stocks.py。
#      缓存 30 天内直接用，缺失/过期会自动抓（12 个请求，约 15 秒）。
import build_etf_stocks
ETF_STOCKS = build_etf_stocks.load()
for _m in ETF_ALL:
    _f = ETF_STOCKS.get('funds', {}).get(_m['code'])
    if _f and _f.get('rows'):
        _m['stocks'] = {'period': _f['period'], 'rows': _f['rows']}

ETF_QUAL = [m for m in ETF_ALL
            if m['hist'] >= 365 and m['n'] >= 3 and m['size'] >= 5
            and not m['stale'] and m['gap_cv'] <= 0.55 and m['sort'] >= 4.0]
ETF_QUAL.sort(key=lambda x: -x['sort'])
# ---- 人工加入的 ETF（add_etf.py 维护 cache/etf_extra.json）----
# 上面那套门槛（年限/规模/节奏/保守股息率）是自动挑选用的；用户"看上了别的红利 ETF"
# 时走 `python add_etf.py <代码>`，那条路**绕过门槛**（它多半正是被门槛挡住的）。
# App 里这些标的带「※ 人工纳入」标记，跟股票的 QUAL_ADD/FORCE 是一套语义。
# ⚠️ 仍要求 ETF_ALL 里有它（≥2 笔分红才算得出节奏）—— 一笔都不分的 ETF 进来也没数据可看。
_qual_codes = {m['code'] for m in ETF_QUAL}
ETF_EXTRA = [m for m in ETF_ALL if m['code'] in set(Jf('etf_extra.json') or {})
             and m['code'] not in _qual_codes]
for _m in ETF_EXTRA:
    _m['extra'] = True          # 界面据此挂 ※
# ---- 备查池：有分红数据、但既没入选也没人工加入的那些（2026-09-20）----
# 用途：App 里「自选 → ETF → ＋」要能把他看上的 ETF 加进清单 ——
# 这些标的的数据（保守股息率 / 派息频率 / 规模 / 逐笔分红）建包时已经算好了，
# 不用等下一轮抓取。默认**不显示**（清单仍是入选的 + 人工加入的），只给 ＋ 搜索用。
# 约 39 只 / 16KB。真·新上市的 ETF 不在这里 —— 那种只能走 add_etf.py 抓数据。
_picked_codes = _qual_codes | {m['code'] for m in ETF_EXTRA}
ETF_MORE = [m for m in ETF_ALL if m['code'] not in _picked_codes]
for _m in ETF_MORE:
    _m['more'] = True
ETF_ZERO = sum(1 for m in ETF_ROWS.values() if not etf_events(m['code']))   # 零分红只数
ETF = {'a': [m for m in ETF_QUAL + ETF_EXTRA if not m['hk']],
       'hk': [m for m in ETF_QUAL + ETF_EXTRA if m['hk']],
       'more': ETF_MORE,
       'scanned': len(ETF_ROWS), 'zero': ETF_ZERO, 'extra': len(ETF_EXTRA)}
print(f'红利 ETF：扫描 {ETF["scanned"]} 只（零分红 {ETF_ZERO} 只），入选 '
      f'{len(ETF["a"])} A股 + {len(ETF["hk"])} 港股'
      + (f'（其中人工加入 {len(ETF_EXTRA)} 只）' if ETF_EXTRA else '')
      + f'；另有 {len(ETF_MORE)} 只备查（App 里可手动加进清单）')

# ---- 分红日历 ----
# 事件窗口：往前 400 天 + 往后 270 天。
# 往前为什么要 400 天：组合页的「月度股息分布」要看过去 12 个月的派息节奏，
# 只留 120 天的话分布图是缺的。往后 270 天够覆盖下一个年报季。
CAL_FROM, CAL_TO = TODAY - timedelta(days=400), TODAY + timedelta(days=270)
DIV2 = Jf('div_detail2.json') if os.path.exists(os.path.join(HERE, 'cache', 'div_detail2.json')) else {}
# 覆盖全部入榜分组，含「自加入」—— 它们被移出老登池，但仍然入榜，
# 漏掉的话它们的除权除息/预案事件会从日历里静默消失
CAL_CODES = sorted({m['code'] for m in QUAL + BROAD + SELFADD})

# ---- 全池除息事件（给账本 / 月度分布 / 未来到账用，日历页不用它）----
# 2026-09-19 修：日历只覆盖入榜标的（CAL_CODES），持仓里的【备选池】票
# （如宁波银行 002142）除息事件整个缺席 —— 账本不自动记、图表不计入。
# 这里把全池（含备选池 257 只）的 ex 事件单独输出一份：
#   · 日历页仍用 calendar（否则从 539 条涨到约 1380 条，密到没法看）；
#   · 账本/图表改用这份（持仓全覆盖，含未来事件供「未来 90 天到账」）。
# ⚠️ POOL_CODES 在下面 POOL_REST 定义之后才算 —— 它要等备选池分好。


def pool_ex_events():
    """[日期, 代码, 每股, 类型]—— ex=除权除息 / rec=股权登记。
    ⚠️ 消费端（App 的 heldEvents）必须【只取 ex】—— rec 也取会把同一笔分红算两次；
       rec 另外用于「今日/明日登记提示」（收盘持有才拿得到分红）。"""
    ev = []
    for c in POOL_CODES:
        for r in DIV2.get(c) or []:
            if div_is_paid(r) or div_is_scheduled(r):
                if r[DIV_EX]:
                    ev.append([r[DIV_EX], c, r[DIV_PER], 'ex'])
                if r[DIV_REC]:
                    ev.append([r[DIV_REC], c, r[DIV_PER], 'rec'])
    ev = [e for e in ev if CAL_FROM <= date.fromisoformat(e[0]) <= CAL_TO]
    ev.sort(key=lambda e: (e[0], e[1]))
    return ev


def calendar_events():
    """[日期, 代码, 类型, 每股, 报告期]；类型 ex=除权除息 rec=股权登记 plan=预案。

    过去事件只取【已实施】行（有登记日/除息日）；未来事件取【未实施但已过会】的
    行，靠 config.div_is_pending 过滤掉 PLan_NOTICE_DATE 的脏值。
    """
    ev = []
    for c in CAL_CODES:
        for r in DIV2.get(c) or []:
            # 已实施 or 日期已排（状态还停在董事会决议通过）→ 都按真实日期出 ex/rec
            # 第 6 位是"待实施"标记：日期来自明细但状态未到"实施分配"，界面上标出来
            if div_is_paid(r) or div_is_scheduled(r):
                pend = 0 if div_is_paid(r) else 1
                if r[DIV_EX]:
                    ev.append([r[DIV_EX], c, 'ex', r[DIV_PER], r[DIV_RP], pend])
                if r[DIV_REC]:
                    ev.append([r[DIV_REC], c, 'rec', r[DIV_PER], r[DIV_RP], pend])
            elif div_is_pending(r):
                ev.append([r[DIV_PLAN], c, 'plan', r[DIV_PER] or 0.0, r[DIV_RP]])
    ev = [e for e in ev if CAL_FROM <= date.fromisoformat(e[0]) <= CAL_TO]
    ev.sort(key=lambda x: (x[0], x[1]))
    return ev


CAL = calendar_events()
_n_ex = sum(1 for e in CAL if e[2] == 'ex')
_n_plan = sum(1 for e in CAL if e[2] == 'plan')
print(f'分红日历：{len(CAL)} 个事件（除权除息 {_n_ex}，预案 {_n_plan}），'
      f'窗口 {CAL_FROM} ~ {CAL_TO}')


def undated_events():
    """已过会、金额是真的，但预案公告日是脏值的行 → [代码, 每股, 报告期]。

    【不给日期】：它们的 PLAN_NOTICE_DATE 沿用了上一期公告日（3~4 月），
    挂上去就是往"未来事件"里塞过去的日期。单独一栏只报"谁、哪一期、多少钱"。
    不进 CAL（那是按日期索引的），另起一个数组。见 config.div_is_undated。
    """
    out = []
    for c in CAL_CODES:
        for r in DIV2.get(c) or []:
            if div_is_undated(r):
                out.append([c, r[DIV_PER], r[DIV_RP]])
    out.sort(key=lambda x: (x[2], x[0]))
    return out


UNDATED = undated_events()
print(f'已过会 · 日期待公告：{len(UNDATED)} 笔（不挂日期，单独一栏）')

# ---- 逐笔分红明细（详情页「逐笔分红」用，2026-09-24 改版）----
# [除息日, 每股]，按除息日升序。筛选尺子只有一把：config.div_is_paid ——
# 只收【已实施且真有除息日】的行（预案/股东大会通过/取消分配天然出局），
# 与股息率、日历、账本用的是同一条判定，不在 App 里重写规则。
# 窗口 = 最近 DIV_YEARS 个【自然年】（2021-01-01 起 = 2021..2026 六个整年）。
# ⚠️ 按自然年切、不按"TODAY 减 N 年"切：Z 的格式是"一行一年"，后者会切出 7 个年份行
#    （2020-09-24 之后的几笔会多顶出一行 2020）。
# 只带两个字段：改版后行里不再显示【报告期】期次 —— 少带一列 ≈ 省 35KB（89 → 54）。
DIV_YEARS = 6
DIV_FROM = '%d-01-01' % (TODAY.year - DIV_YEARS + 1)


def divs(code):
    out = []
    for r in DIV2.get(code) or []:
        if div_is_paid(r) and r[DIV_EX] >= DIV_FROM:
            out.append([r[DIV_EX], round(r[DIV_PER], 4)])
    return sorted(out, key=lambda x: x[0])


DIVS = {c: divs(c) for c in sorted({m['code'] for m in ROWS})}
print(f'逐笔分红明细：{sum(len(v) for v in DIVS.values())} 笔（{len(DIVS)} 只，'
      f'{DIV_FROM} 起 = 近 {DIV_YEARS} 个自然年）')

# 池内已算好指标、但没进任何分组的那一批（给「自加入」当备选）
_IN_POOLS = {m['code'] for m in QUAL + BROAD + SELFADD}
POOL_REST = [m for m in ROWS if m['code'] not in _IN_POOLS]
print(f'备选池（自加入可挑）{len(POOL_REST)} 只', flush=True)

# 全池除息事件（八到 POOL_REST 之后才能算，见上方 pool_ex_events 的注释）
POOL_CODES = sorted({m['code'] for m in QUAL + BROAD + SELFADD + POOL_REST})
POOL_EX = pool_ex_events()
print(f'全池除息事件：{len(POOL_EX)} 条（{len(POOL_CODES)} 只，含备选池 —— 账本/图表用）')

# ---- 全市场分红快照（搜索扩到全市场用，2026-09-19 任务10）----
# 池外 + 非 ST、近 365 天有分红的 A 股：[代码, 名称, 行业, 每股(近365天), 现价, 最后一笔除息, 市值]
# 数据来自 scan_market.py 的缓存（分红明细 market_div.json + 行业 market_industry.json）；
# 缓存不存在就为空数组 —— 功能静默降级，搜索退回只覆盖池内。
# 口径：近 365 天实收（同日历/账本），不是 TTM 报告期口径 —— 防僵尸高息（见 scan_market.py）。
MKT = []
_MKDIV_PATH = os.path.join(HERE, 'cache', 'market_div.json')
if os.path.exists(_MKDIV_PATH):
    _MKDIV = json.load(open(_MKDIV_PATH, encoding='utf-8'))
    _MIND = {}
    _MIND_PATH = os.path.join(HERE, 'cache', 'market_industry.json')
    if os.path.exists(_MIND_PATH):
        _MIND = json.load(open(_MIND_PATH, encoding='utf-8'))
    _ALLA = json.load(open(os.path.join(HERE, 'cache', 'all_a.json'), encoding='utf-8')).get('list') or []
    _NM = {x[0]: x[1] for x in _ALLA}
    _inpool = {m['code'] for m in ROWS}
    _cut365 = str(TODAY - timedelta(days=365))
    _rows = []
    for x in _ALLA:
        c = x[0]
        if c in _inpool or 'ST' in (x[1] or '').upper() or '退' in (x[1] or ''):
            continue
        rs = _MKDIV.get(c) or []
        per = sum(r[1] for r in rs if r[0] >= _cut365)
        if per <= 0:
            continue
        _rows.append((c, per, max((r[0] for r in rs), default='')))
    _mp = {}
    for i in range(0, len(_rows), 60):
        q = ','.join(('sh' if r[0][0] == '6' else 'sz') + r[0] for r in _rows[i:i + 60])
        for attempt in range(4):
            try:
                _rr = requests.get('https://qt.gtimg.cn/q=' + q, timeout=20); _rr.encoding = 'gbk'; break
            except Exception:
                if attempt == 3: continue
                time.sleep(2 * (attempt + 1))
        else:
            continue
        for line in _rr.text.strip().split(';'):
            try:
                f = line.split('=')[1].strip('"').split('~')
                _mp[f[2]] = (float(f[3]), float(f[45]) if len(f) > 45 else 0.0)
            except Exception:
                pass
    for c, per, last in _rows:
        pr, cp = _mp.get(c, (0.0, 0.0))
        if pr <= 0:
            continue
        # 第 8 列：逐年派息（按【报告期】年份归集，最近 5 年）—— 池外「轻详情」用，
        # 让用户能看出这只票的分红是在涨还是在退（比如苏垦农发 0.36→0.27→0.15）。
        # 只留 per>0 的年份；口径与池内的 hist 一致（报告期归集），不是"按除息年"。
        _by = {}
        for _r in (_MKDIV.get(c) or []):
            _rp = str(_r[2] or '')[:4]
            if _rp.isdigit() and _r[1]:
                _by[int(_rp)] = round(_by.get(int(_rp), 0.0) + _r[1], 4)
        _ys = sorted(_by)[-5:]
        # 第 9 列：连续分红年数（2026-09-24 加，为「榜单 → 池外高息」那个池子）。
        # ⚠️ 与池内 streak 同一把尺：按【报告期】归集、从最近一个有分红的报告期往回数连续年数
        #    （招行那种 24 年）。**不是** scan_market 报告里那个 yr5（那是"近 5 年里有几年分红"，
        #    0~5 的另一套口径），两者别混。
        _st, _yy = 0, (max(_by) if _by else None)
        while _yy is not None and _by.get(_yy, 0) > 0:
            _st += 1
            _yy -= 1
        MKT.append([c, _NM.get(c, c), _MIND.get(c, ''), round(per, 4),
                    round(pr, 2), last, round(cp), [[y, _by[y]] for y in _ys], _st])
print(f'全市场快照（搜索 + 池外高息池）：{len(MKT)} 只（池外近一年有分红，含逐年派息 + 连续分红年数）',
      flush=True)

# ---- 池外高息名单的"池内那把尺"（2026-09-24 Z 定：轻详情页要能跟池内口径对照）----
# 关键：**复用 rebuild_div.metrics()** —— 池外和池内用【同一段代码】算报告期口径
# （TTM_CYC / avg3）。数据源 market_div.json 与池内的 div_detail2.json 是**同一张东财表**，
# 而且 scan_market.fetch_div 已经只收"实施分配 + 有除息日"的行（与 config.div_is_paid 同一把尺），
# 所以伪造成 div_detail2 的行形状（补上进度/EPS 两格）就能直接喂进去。
# 只算【池外高息名单】（近一年实收 ÷ 现价 ≥ 4%）—— 轻详情会被翻的就是这些；
# 剩下两千多只池外票不进 payload（省 ~900KB，页面自然降级成现在这样）。
from rebuild_div import metrics as _div_metrics      # 有 __main__ 守卫，import 安全
# POOL_MIN（4.0）来自 config.py —— 与榜单第三个 chip、scan_market 报告同一处真源


def _cv_streak(by_rp):
    """派息波动 cv + 连续分红年数 —— 与池内 export.metrics() 同一套公式、同一归集规则
    （都按【报告期】归集、cv 只取完整会计年度）。那边读 yield2 的 by_rp、这边读
    market_div 现算的 by_rp，键都是年份字符串。"""
    ys = sorted(int(y) for y in by_rp if by_rp[y] > 0)
    if not ys:
        return 9.9, 0
    y = latest = ys[-1]
    streak = 0
    while by_rp.get(str(y), 0) > 0:
        streak += 1
        y -= 1
    cv_end = min(latest, YEAR - 1)
    v5 = [by_rp.get(str(k), 0.0) for k in range(cv_end - 4, cv_end + 1)]
    mean = sum(v5) / 5
    cv = (sum((x - mean) ** 2 for x in v5) / 5) ** 0.5 / mean if mean > 0 else 9.9
    return round(cv, 2), streak


def _slim_org(p):
    """公司概况（池外版）：整份约 1KB/只，226 只就是 230KB —— 把简介截到 220 字以内
    （断在标点上，别硬切在字中间），其余字段（主营构成/行业/上市/注册地/审计）留着。"""
    if not p:
        return None
    o = dict(p)
    pr = (o.get('profile') or '').strip()
    if len(pr) > 220:
        cut = max(pr.rfind(c, 150, 220) for c in '。；，、')
        o['profile'] = (pr[:cut + 1] + '…') if cut > 0 else (pr[:220] + '…')
    return o


MKT_EX = {}
for _m in MKT:
    _c, _price, _per365 = _m[0], _m[4], _m[3]
    if not _price or _per365 / _price * 100 < POOL_MIN:
        continue
    _rows = [[ex, per, rp, '', '', '', '实施分配', None]
             for ex, per, rp in (_MKDIV.get(_c) or [])]
    if not _rows:
        continue
    _mx = _div_metrics(_c, _price, _rows)
    _cv, _st = _cv_streak(_mx['by_rp'])
    _ttm, _avg3 = _mx['ttm'], _mx['avg3']
    _e = {'ttm': round(_ttm, 2), 'avg3': round(_avg3, 2), 'cv': _cv, 'streak': _st,
          'ratio': round(_ttm / _avg3, 2) if _avg3 > 0 else 0,
          'n1y': sum(1 for r in _rows if r[0] >= str(TODAY - timedelta(days=365))),
          'divs': [[r[0], round(r[1], 4)] for r in _rows if r[0] >= DIV_FROM]}
    _e['fails'] = fails(_e)           # 四道过滤：config.fails 同一把尺，不在这儿重写门槛
    _org = _slim_org(PROFILE.get(_c))
    if _org:
        _e['org'] = _org
    MKT_EX[_c] = _e
print(f'池外高息 · 池内口径对照：{len(MKT_EX)} 只（近一年 ≥ {POOL_MIN}%）；'
      f'含逐笔 {sum(len(v["divs"]) for v in MKT_EX.values())} 笔、'
      f'公司概况 {sum(1 for v in MKT_EX.values() if v.get("org"))} 只', flush=True)

OUT = {
    'meta': {'date': str(TODAY), 'poolSize': len(pool) - len(EXTRA),
             'qualCount': len(QUAL), 'broadCount': len(BROAD),
             'forced': sorted(FORCE), 'excluded': sorted(EXCLUDE),
             'extra': sorted(EXTRA),
             'extraNames': {c: e['name'] for c, e in EXTRA.items()}},
    'qual': QUAL,
    'selfAdd': SELFADD,
    'broad': BROAD,
    # 「自加入」的备选池：池内已算好指标、但既不在老登也不在全量的那些
    # （实测 261 只，含长江电力 / 贵州茅台 / 建设银行 / 宁波银行 / 宁德时代）。
    # 不导的话，它们在 App 里"不存在"，你想手工加也加不进来。
    'poolRest': POOL_REST,
    'etf': ETF,
    'etfBench': BENCH,          # ETF 历史表现的同期基准（沪深300 / 中证红利，价格口径）
    'calendar': CAL,
    'poolEx': POOL_EX,          # 全池除息事件（含备选池）—— 账本/月度分布/未来到账用
    'mkt': MKT,                 # 全市场分红快照 —— 搜索扩到全市场用（只查，不进榜）
    # 池外高息名单的"池内那把尺"（2026-09-24）：报告期口径 TTM/avg3 + cv/连续年数/ratio
    # + 四道过滤的结论（fails）+ 逐笔明细 + 公司概况。轻详情页拿它跟"近一年实收"对照 ——
    # 差多少一眼看出是"真高息"还是"僵尸高息/一次性分红"。只覆盖近一年 ≥4% 的那些票。
    'mktEx': MKT_EX,
    'calendarWindow': [str(CAL_FROM), str(CAL_TO)],
    # 已过会、金额真、但公告日是脏值的那些：没有可信日期，所以不塞进 CAL，
    # 单列一栏 [代码, 每股, 报告期]。「已公告待实施」是【日期已定】，
    # 这一栏是【日期不可信】—— 两码事，别混。
    'undated': UNDATED,
    # 逐年派息给【全部】入榜标的：App 里点任意一行都要能画出柱图，
    # 只给 8 只的话详情页对大多数个股是空的（原型阶段只画招行所以没暴露）
    # 逐年派息柱图：全部入池标的都给（详情页要用；「自加入」可能加任意一只）
    'hist': {c: hist(c, 6) for c in sorted({m['code'] for m in ROWS})},
    # 逐笔分红明细（详情页「分红时间线」用）：[除息日, 每股, 报告期]，近 10 年。
    # 与 hist（逐年归集）是两种粒度：hist 画柱图看趋势，这个看笔次/节奏/每期多少钱。
    'divs': DIVS,
}
dst = os.path.join(HERE, 'ui_data.json')
json.dump(OUT, open(dst, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(f'✅ 真老登 {len(QUAL)} 只 / 全量 {len(BROAD)} 只 → {dst}', flush=True)
print('\nTOP 15 吃息分：')
for m in QUAL[:15]:
    print(f"  {m['score']:>5.1f}  {m['code']} {m['name']:<8} {m['ind']:<10} "
          f"近3年均{m['avg3']:>5.2f}% TTM{m['ttm']:>5.2f}% 连续{m['streak']:>2}年 "
          f"波动{m['cv']:.2f} [{m['tag']}]", flush=True)
