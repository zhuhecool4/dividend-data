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
    python rebuild_div.py --check 002170 600036 601088
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

# 行结构在 config.py 里定义（export_ui_data.py 也要读同一份缓存）
from config import (DIV_EX as IDX_EX, DIV_PER as IDX_PER, DIV_RP as IDX_RP,
                    DIV_REC as IDX_REC, DIV_PLAN as IDX_PLAN, DIV_NOTICE as IDX_NOTICE,
                    DIV_PROG as IDX_PROG, DIV_EPS as IDX_EPS, div_is_paid)


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


def fetch_price(code):
    # 北交所 92xxxx / 43xxxx / 83xxxx / 87xxxx 用 bj 前缀（用 sz 会拿到空串 → IndexError）
    if code[:2] in ('92', '43', '83', '87'):
        pre = 'bj'
    elif code[0] in '65':
        pre = 'sh'
    else:
        pre = 'sz'
    p = net.get(f'https://qt.gtimg.cn/q={pre}{code}', timeout=15).text
    return float(p.split('=')[1].strip('";\n\r ').split('~')[3])


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
    rps = [(date.fromisoformat(r[IDX_RP]), r[IDX_PER]) for r in rows if r[IDX_RP]]
    if rps:
        latest_rp = max(r[0] for r in rps)
        win_start = latest_rp - timedelta(days=365)
        ttm_cyc = sum(per for rpd, per in rps if win_start < rpd <= latest_rp)
        ann = next((per for rpd, per in rps if rpd == latest_rp and str(latest_rp)[5:] == '12-31'), None)
        mid = ttm_cyc - (ann or 0.0)
    else:
        ann, mid = None, 0.0
        ttm_cyc = rows[0][IDX_PER] if rows else 0.0
    if ttm_cyc <= 0:                             # 无年报分红（罕见）→ 退回最近一次分红
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
        'eps': latest_eps(all_rows),                     # 派息率分母，None=亏损或无数据
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()

    pool = list(json.load(open(os.path.join(HERE, 'cache', 'pool.json'), encoding='utf-8')))
    extra = json.load(open(os.path.join(HERE, 'cache', 'extra_pool.json'), encoding='utf-8'))
    codes = sorted(set(pool) | set(extra))

    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding='utf-8'))
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
        json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)

    # 重算（取价并发）
    print('\n重算双口径 ...')
    ok = [c for c in codes if cache.get(c)]
    prices, bad = {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_price, c): c for c in ok}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                prices[c] = fut.result()
            except Exception as e:
                bad.append(c)
                print(f'  ✗ {c} 取价失败 {type(e).__name__}: {e}')
    out = {c: metrics(c, prices[c], cache[c]) for c in ok if c in prices}
    json.dump(out, open(os.path.join(HERE, 'cache', 'yield2.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'✅ {len(out)} 只已写入 cache/yield2.json'
          f'（抓取缺失 {len(codes)-len(ok)}，取价失败 {len(bad)} {bad}）')

    if a.check:
        print(f'\n{"代码":<8}{"现价":>7}{"TTM":>8}{"TTM实收":>9}{"TTM年化":>9}'
              f'{"近3均新":>9}{"近3均旧":>9}   {"年报":>6}{"中报":>6}')
        for c in a.check:
            m = out.get(c)
            if not m:
                continue
            print(f'{c:<8}{m["price"]:>7.2f}{m["ttm"]:>7.2f}%{m["ttm_1y"]:>8.2f}%'
                  f'{m["ttm_cyc"]:>8.2f}%{m["avg3"]:>8.2f}%{m["avg3_old"]:>8.2f}%   '
                  f'{(m["ann"] or 0):>6.3f}{(m["mid"] or 0):>6.3f}')


if __name__ == '__main__':
    main()
