# -*- coding: utf-8 -*-
"""重建分红明细缓存（含分红所属报告期）+ 双口径股息率。

背景（2026-09-16）：
  芭田股份 TTM 我算 8.42%(近365天除息 0.93/11.05)，雪球显示 6.96%(0.77/11.05)。
  核查结论：雪球口径同为「近 12 个月除息累计」（美的/招行/神华三只完全吻合），
  差异源于芭田 2025 年起新增中期分红，过去 12 个月内装了 3 笔（2025中报+2025年报+2026中报），
  即"分红频率切换期"，TTM 被一次性放大。

口径定义（本脚本）：
  TTM_1Y   = 近 365 天实际除息金额合计 / 现价              —— 现金流视角（仅参考，会因
             分红日漂移而抖动：中国移动 380 天边界、分众传媒特别分红）
  TTM_CYC  = 以【最新报告期】为终点、往回 12 个月报告期内的全部分红之和 / 现价
             —— 年度周期重构，**主口径**。自适应各种分红节奏：
             · 只派年报的拿 1 笔；年报+中报的拿 2 笔；一季一付的拿满 4 个季度
             · 窗口按报告期切（永远对齐财年），不按除息日切（会漂移，移动中报除息
               距基准日 380 天，卡除息日会误杀）
             · 天然排除上一周期的中期分红，频率切换期不会虚高
  TTM      = TTM_CYC
  AVG3     = 近 3 个完整会计年度（按【分红所属报告期】归集）分红均值 / 现价
             （旧口径按【除息日年份】归集，会把次年 5-7 月才实施的年报分红推到下一年，
               使窗口系统性漏掉最近一个年报并有 1 年滞后。实测：芭田 1.82%→3.50%、
               山西焦煤 11.39%→5.79%、兖矿能源 9.13%→4.57%，全池 128/146 只受影响）

用法：
    python rebuild_div.py            # 抓取并重算（已缓存的跳过）
    python rebuild_div.py --price-only   # **只刷价格**：不抓分红明细，用缓存行 × 新价重算
    python rebuild_div.py --check 002170 600036 601088

⚠️ --price-only（2026-10-18 加）存在的原因：export_ui_data.py 算指标时优先用
   yield2 里的 price（`p = m2.get('price') or PRICE.get(code)` —— 这样价格和
   avg3/ttm 是同一时点、彼此自洽），所以**不跑本脚本，整包的价格就冻在上次重算那天**。
   实测 #103 的价停在 09-28，而手机拉到的云端数据是 10-16，两边入选名单差了 7 只。
   云端 workflow 每天删缓存重跑本脚本；本地 build_all 以前不跑 → 现在 refresh_data
   第一步就是 `--price-only`。见开发文档 S25。
"""
import os, sys, json, time, argparse
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net

CACHE = os.path.join(HERE, 'cache', 'div_detail2.json')   # v2：带登记日/预案日/进度/EPS
TODAY = date.today()
YEAR = TODAY.year
CUT = TODAY - timedelta(days=365)

# 周期重构（ttm_cyc）的【过期阈值】天数，2026-09-28 加。
# 口径背景：ttm_cyc 是"以最新报告期为终点、往回取 12 个月报告期内的分红之和"，
#   好处是重构一个年度周期、不受除息日漂移影响（招行/移动/芭田那几种节奏都对得上）。
# 但它原来没有"这套周期还新不新"的判断 —— 对【早就停派】的票，latest_rp 可能是
#   好几年前，重构出来的"年度周期"就是陈年旧数，却按今天的股价算成"今天的股息率"。
# 判据用【最近一笔已实施分红的除息日】：现金停了多久才是硬事实（报告期只是账面归集）。
# 为什么是 548 天（18 个月）：正常年度派息的现金间隔是 12 个月，留半年迟发余量；
#   超过 18 个月说明至少漏掉一期 —— 那时再拿老周期算股息率就是谎报。
# 过期后不再重构，退回 ttm_1y（近 12 个月实付）—— 停派的票那里本来就是 0。
# ⚠️ 触发这一条的实况（2026-09-28 万科A 000002）：最新一笔已实施分红是 2023-08-25
#    （报告期 2022-12-31），窗口重构出 0.68 元，除以跌到 3.32 的股价 = 20.48%，
#    而它已经三年没分红了（这只还是 FORCE_BASE 里的"崩塌预警"样本）。
#    全池同病 16 只：通威股份 7.97%、晶科能源 5.77%、天齐锂业 3.16%、隆基绿能 1.51%…
#    全都是 2023 年之后停派的。扫描脚本：_scratch/scan_stale_ttm.py
STALE_EX_DAYS = 548

# 行结构在 config.py 里定义（export_ui_data.py 也要读同一份缓存）
from config import (DIV_EX as IDX_EX, DIV_PER as IDX_PER, DIV_RP as IDX_RP,
                    DIV_REC as IDX_REC, DIV_PLAN as IDX_PLAN, DIV_NOTICE as IDX_NOTICE,
                    DIV_PROG as IDX_PROG, DIV_EPS as IDX_EPS, div_is_paid)
import store


def fetch(code):
    """东财数据中心 → 全部行（含未实施的预案行），按报告期倒序。

    字段本来就全在响应里（columns=ALL，30 个），原先只用 3 个。
    现在多存：股权登记日 / 预案公告日 / 进度 —— 分红日历靠这些。
    """
    import urllib.parse
    q = {'reportName': 'RPT_SHAREBONUS_DET', 'columns': 'ALL',
         'filter': f'(SECURITY_CODE="{code}")', 'pageSize': '200', 'pageNumber': '1',
         'sortColumns': 'REPORT_DATE', 'sortTypes': '-1',
         'source': 'WEB', 'client': 'WEB'}
    url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?' + urllib.parse.urlencode(q)
    j = net.get(url, encoding='utf-8', timeout=25).json()
    rows = (j.get('result') or {}).get('data') or []
    out = []
    for r in rows:
        # ⚠️ 不要丢弃 REPORT_DATE 为空的行：1990 年代的老分红多数没有报告期，
        # 老代码用 (rp or 除息年) 兜底。丢掉会让 by_rp 少若干年份键 →
        # 老股的「连续分红年数」(streak) 被截断（实测 78 只受影响）。
        rp = str(r.get('REPORT_DATE') or '')[:10]
        # 纯送股/转增行的 PRETAX_BONUS_RMB 是 None → 记 0.0（沿用老行为）。
        # 丢掉这些行会让 by_rp 少若干年份键（1990 年代老股，实测 78 只）。
        # 各消费方都用 by.get(y, 0.0)，所以 0.0 与"键不存在"在下游等价，
        # 但保持旧值可以确保这次改造对现有产物零影响。
        per = round(float(r.get('PRETAX_BONUS_RMB') or 0) / 10.0, 6)
        g = lambda k: (str(r.get(k))[:10] if r.get(k) else '')
        eps = r.get('BASIC_EPS')
        out.append([g('EX_DIVIDEND_DATE'), per, rp, g('EQUITY_RECORD_DATE'),
                    g('PLAN_NOTICE_DATE'), g('NOTICE_DATE'),
                    str(r.get('ASSIGN_PROGRESS') or ''),
                    round(float(eps), 4) if eps is not None else None])
    return out


def latest_eps(rows):
    """最近一个【年报】(12-31) 报告期的每股收益。

    派息率 = TTM 每股分红 ÷ 本值。EPS<=0（亏损，如建发股份 -3.91）时无意义 → None。
    """
    for r in rows:
        if r[IDX_RP][5:] == '12-31' and r[IDX_EPS] is not None:
            return r[IDX_EPS] if r[IDX_EPS] > 0 else None
    return None


def _pre(code):
    """代码 → 腾讯行情的交易所前缀（北交所 92/43/83/87 用 bj，用 sz 会拿到空串）。"""
    if code[:2] in ('92', '43', '83', '87'):
        return 'bj'
    return 'sh' if code[0] in '65' else 'sz'


def fetch_price(code):
    p = net.get(f'https://qt.gtimg.cn/q={_pre(code)}{code}', timeout=15).text
    return float(p.split('=')[1].strip('";\n\r ').split('~')[3])


def fetch_prices(codes, batch=60):
    """批量取价：60 只一个请求（和 export_ui_data.py 同一招）。

    405 只 = 7 个请求（约 2 秒）；单只版要 405 个连接（约 10 秒）——
    `--price-only` 要挂进 build_all，这段延迟直接算在"出包约 1 分钟"里。
    批量回包里缺的（个别代码偶发缺失）再单只补一次；补不到就不放进结果，
    调用方按"取价失败"处理 —— **不能因为一只失败把整批算丢**。
    """
    codes = list(codes)
    out = {}
    for i in range(0, len(codes), batch):
        part = codes[i:i + batch]
        q = ','.join(_pre(c) + c for c in part)
        r = None
        for attempt in range(4):
            try:
                r = net.get('https://qt.gtimg.cn/q=' + q, timeout=20)
                r.encoding = 'gbk'
                break
            except Exception:
                r = None
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
        if r is None:
            continue
        for line in r.text.strip().split(';'):
            qt = net.parse_quote(line, min_n=5)
            if qt and qt.get('code') and qt.get('price') is not None:
                out[qt['code']] = qt['price']
    miss = [c for c in codes if c not in out]
    if miss:
        # 批量整个挂了才会走到这里（正常 miss 为空）。⚠️ 这里必须**并发**补：
        # 403 只单只串行 ≈ 50 秒（2026-10-18 第一次跑实测 52.4s，冷启动那几批全超时
        # → 退回单只），而 build_all 的"约 1 分钟"经不起这个。
        if len(miss) > 20:
            print(f'  · 批量取价缺 {len(miss)} 只，单只并发补 ...')
        with ThreadPoolExecutor(max_workers=8) as ex:
            for c, p in zip(miss, ex.map(_safe_price, miss)):
                if p is not None:
                    out[c] = p
    return out


def _safe_price(code):
    """单只取价失败 → None（补价用；异常不能把整轮带崩）。"""
    try:
        return fetch_price(code)
    except Exception:
        return None


def metrics(code, price, all_rows):
    """all_rows: 全部行（含未实施预案）。股息率只用已实施行算，见 is_paid()。"""
    rows = [r for r in all_rows if div_is_paid(r)]
    # --- 报告期归集 ---
    by_rp = {}
    for r in rows:
        y = (r[IDX_RP] or r[IDX_EX])[:4]
        by_rp[y] = round(by_rp.get(y, 0.0) + r[IDX_PER], 6)
    avg3 = sum(by_rp.get(str(y), 0.0) for y in range(YEAR - 3, YEAR)) / 3

    # --- 旧口径（除息年归集），保留用于对照 ---
    by_ex = {}
    for r in rows:
        y = r[IDX_EX][:4]
        by_ex[y] = round(by_ex.get(y, 0.0) + r[IDX_PER], 6)
    avg3_ex = sum(by_ex.get(str(y), 0.0) for y in range(YEAR - 3, YEAR)) / 3

    # --- TTM_1Y ---
    ttm_1y = sum(r[IDX_PER] for r in rows if CUT <= date.fromisoformat(r[IDX_EX]) <= TODAY)

    # --- TTM_CYC：以【最新报告期】为终点，往回取 12 个月报告期内的全部分红之和 ---
    # 这是"年度周期重构"的正确做法，自适应各种分红节奏：
    #   · 只派年报（招行）：窗口 = FY2025 年报一笔 → 4.93% ✓
    #   · 年报+中报（移动）：最新 rp=2025-12-31，窗口含 2025 中报+年报 → 4.85% ✓
    #   · 频率切换（芭田）：最新 rp=2026-06-30，窗口 = 2025年报+2026中报 = 0.77 → 6.97% ✓
    #     （旧的 2025 中报 0.16 被排除，不会三重计入 → 这正是雪球显示的数）
    #   · 一季一付（迈瑞/兔宝宝）：窗口拿满 4 个季度，不漏算 ✓
    # 注意：窗口按【报告期】而不是除息日切 —— 除息日会漂移（移动中报除息距基准日 380 天），
    # 报告期永远对齐财年，不会误杀。
    # --- 过期判断：最近一笔【已实施分红】的除息日距今多久（2026-09-28，见 STALE_EX_DAYS）---
    last_ex = max((date.fromisoformat(r[IDX_EX]) for r in rows), default=None)
    stale = last_ex is not None and (TODAY - last_ex).days > STALE_EX_DAYS

    rps = [(date.fromisoformat(r[IDX_RP]), r[IDX_PER]) for r in rows if r[IDX_RP]]
    if stale:
        ann, mid, ttm_cyc = None, 0.0, None       # None = 周期已过期，下面退回 ttm_1y
    elif rps:
        latest_rp = max(r[0] for r in rps)
        win_start = latest_rp - timedelta(days=365)
        ttm_cyc = sum(per for rpd, per in rps if win_start < rpd <= latest_rp)
        ann = next((per for rpd, per in rps if rpd == latest_rp and str(latest_rp)[5:] == '12-31'), None)
        mid = ttm_cyc - (ann or 0.0)
    else:
        ann, mid, ttm_cyc = None, 0.0, None
    if ttm_cyc is None:
        ttm_cyc = ttm_1y                           # 过期 / 无报告期 → 近 12 个月实付（停派即 0）
    elif ttm_cyc <= 0:                             # 无年报分红（罕见）→ 退回最近一次分红
        ttm_cyc = rows[0][IDX_PER] if rows else 0.0

    ttm = ttm_cyc                                # 不取 min：min 会误杀节奏漂移型
    return {
        'price': price,
        'ttm': round(ttm / price * 100, 4),
        'ttm_1y': round(ttm_1y / price * 100, 4),
        'ttm_cyc': round(ttm_cyc / price * 100, 4),
        'avg3': round(avg3 / price * 100, 4),
        'avg3_old': round(avg3_ex / price * 100, 4),
        'by_rp': by_rp, 'by_ex': by_ex,
        'ann': ann, 'mid': mid, 'ttm_1y_amt': round(ttm_1y, 4),
        'last_ex': last_ex.isoformat() if last_ex else None,   # 诊断用：最近一笔已实施分红的除息日
        'cyc_stale': stale,                                    # 诊断用：周期重构是否已过期
        'eps': latest_eps(all_rows),                     # 派息率分母，None=亏损或无数据
    }


def price_only_out(codes, cache, prices, old):
    """`--price-only` 的输出（纯函数，便于测试）：能重算的重算，取不到价就沿用旧记录。

    返回 (out, kept, skipped)：
      · kept    —— 有旧记录但这次没重算（缓存缺行 / 取价失败 / 价格非正）
      · skipped —— 既算不出来也没有旧记录（多半是新加进池、分红明细还没抓）

    ⚠️ 取价失败**绝不能把标的丢掉**：yield2 是 ui_data 唯一的股息来源，少一只 =
       榜单/日历/详情里静默少一只（S3 那条"漏抓会表现成某只不见了"）。
       旧记录里的 price 是上一次的价 —— 比"标的消失"好，下一轮就补上。
    """
    out, kept, skipped = {}, [], []
    for c in codes:
        rows = cache.get(c)
        p = prices.get(c)
        if rows and p and p > 0:
            out[c] = metrics(c, p, rows)
        elif c in old:
            out[c] = old[c]
            kept.append(c)
        else:
            skipped.append(c)
    return out, kept, skipped


def print_check(out, codes):
    """--check 的那张表（两种模式共用，口径只有一处）。"""
    print(f'\n{"代码":<8}{"现价":>7}{"TTM":>8}{"TTM实收":>9}{"TTM年化":>9}'
          f'{"近3均新":>9}{"近3均旧":>9}   {"年报":>6}{"中报":>6}')
    for c in codes:
        m = out.get(c)
        if not m:
            continue
        print(f'{c:<8}{m["price"]:>7.2f}{m["ttm"]:>7.2f}%{m["ttm_1y"]:>8.2f}%'
              f'{m["ttm_cyc"]:>8.2f}%{m["avg3"]:>8.2f}%{m["avg3_old"]:>8.2f}%   '
              f'{(m["ann"] or 0):>6.3f}{(m["mid"] or 0):>6.3f}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--price-only', action='store_true',
                    help='只刷价格：不抓分红明细，用缓存行 × 新价重算 yield2（build_all 每次跑）')
    a = ap.parse_args()

    pool = list(json.load(open(os.path.join(HERE, 'cache', 'pool.json'), encoding='utf-8')))
    extra = json.load(open(os.path.join(HERE, 'cache', 'extra_pool.json'), encoding='utf-8'))
    codes = sorted(set(pool) | set(extra))

    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding='utf-8'))

    YIELD = os.path.join(HERE, 'cache', 'yield2.json')

    # ---------------- 只刷价格（2026-10-18，见文件头） ----------------
    # 完全不碰分红明细（一个东财请求都不发），只把 405 只的现价取回来、按同一套
    # metrics() 重算。缓存里缺行的标的沿用旧记录 —— 见 price_only_out 的说明。
    if a.price_only:
        old = json.load(open(YIELD, encoding='utf-8')) if os.path.exists(YIELD) else {}
        ok = [c for c in codes if cache.get(c)]
        print(f'\n只刷价格：{len(ok)} 只有分红缓存，批量取价 ...')
        t0 = time.time()
        prices = fetch_prices(ok)
        out, kept, skipped = price_only_out(codes, cache, prices, old)
        store.dump_json(YIELD, out, ensure_ascii=False, indent=1, empty_ok=False)   # 空 = 一只都没算出来，别覆盖旧缓存（S10.75）
        print(f'✅ 只刷价格 {time.time()-t0:.1f}s：重算 {len(out)-len(kept)} 只，'
              f'沿用旧记录 {len(kept)} 只'
              + (f'（{"、".join(kept[:6])}{"…" if len(kept) > 6 else ""}）' if kept else '')
              + f'，跳过 {len(skipped)} 只 → cache/yield2.json')
        if a.check:
            print_check(out, a.check)
        return

    todo = [c for c in codes if c not in cache] if not a.check else a.check

    if todo:
        print(f'需抓取 {len(todo)} 只（已有缓存 {len(cache)}）...')
        t0 = time.time()
        done = 0

        def job(c):
            last = None
            for _ in range(3):
                try:
                    return c, fetch(c), None
                except Exception as e:
                    last = e
                    time.sleep(1.2)
            return c, None, last

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for fut in as_completed([ex.submit(job, c) for c in todo]):
                c, rows, err = fut.result()
                done += 1
                if rows is None:
                    print(f'  ✗ {c} {type(err).__name__}')
                else:
                    cache[c] = rows
                if done % 50 == 0:
                    print(f'  ... {done}/{len(todo)}  {time.time()-t0:.0f}s')
        print(f'抓取完成 {time.time()-t0:.0f}s，缓存 {len(cache)} 只')
        store.dump_json(CACHE, cache, ensure_ascii=False, empty_ok=False)   # 空 = 抓取全失败，别覆盖旧缓存（S10.75）

    # 重算（批量取价 + 同一套 metrics）
    print('\n重算双口径 ...')
    ok = [c for c in codes if cache.get(c)]
    prices = fetch_prices(ok)
    bad = [c for c in ok if not prices.get(c)]
    if bad:
        print(f'  ✗ 取价失败 {len(bad)} 只：{bad}')
    out = {c: metrics(c, prices[c], cache[c]) for c in ok if prices.get(c)}
    store.dump_json(YIELD, out, ensure_ascii=False, indent=1, empty_ok=False)   # 空 = 抓取全失败，别覆盖旧缓存（S10.75）
    print(f'✅ {len(out)} 只已写入 cache/yield2.json'
          f'（抓取缺失 {len(codes)-len(ok)}，取价失败 {len(bad)} {bad}）')

    if a.check:
        print_check(out, a.check)


if __name__ == '__main__':
    main()
