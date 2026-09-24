# -*- coding: utf-8 -*-
"""ETF 历史表现 · 构建器（详情页「历史表现 · 含息」折叠区用）

产出 cache/etf_perf.json：
    {"date": "2026-09-21", "perf": {代码: {...}}}
    {代码: {"years": 7.0, "from": 起, "to": 止, "cur": 现价,
            "dd_all": -22.0, "dd_all_span": [峰日, 谷日], "cur_dd": -4.8,
            "w1"/"w3"/"w5": {"all": 含息不复投%, "re": 含息复投%, "px": 价格涨幅%,
                             "cagr": 年化不复投, "cagrRe": 年化复投,
                             "div": 窗口内每份分红, "nev": 分红次数,
                             "dd": 窗口最大回撤, "from": 窗口起, "span": 年}}}

⚠️⚠️ 四条口径，全是踩过的，改之前先读完 ⚠️⚠️

坑 1  etf_div.json 存的是【自成立以来累计每份分红】（510880 从 0.009 单调涨到 1.576），
      **不是每笔分红**。直接求和会算出"近3年 +274%"这种鬼数字（第一版探针就这么错的）。
      每笔 = 相邻两次累计值之差（首笔 = 自身）—— 与 export_ui_data.etf_events 同一口径。

坑 2  【不复投 vs 复投 是两个数，都得给】：
        all/cagr   = 分红取出来花掉（期末价 + 窗口内每份分红）÷ 期初价 − 1
        re/cagrRe  = 每次除息【按当日收盘价】再买入（份额累乘）
      ⚠️ 我们这套用二级市场价，所以复投也按收盘价买 —— 这才是投资人真做得到的动作。
         雪球/天天基金显示的是【按基金净值再投资】，跟踪正常时两者几乎一样；
         实测 1 年期三只与雪球**精确到小数点后两位**一致（2026-09-21）。

坑 3  【别用"累计净值"算窗口收益】。天天基金/雪球的累计净值对有过份额折算的基金是失真的：
      510880 的 累计净值 ÷ (单位净值 + 当时累计分红) = **恒定 1.5261**（2009~2026 五年份都是），
      拿它的比值算收益，历史分红会在分子分母里互相抵消，把收益压扁 ——
      近5年真值 +21%，累计净值法只给 +16.2%。第一版探针差点报了个 +8.7%。

坑 4  【历史不够这个窗口就不算】。不能"有多少算多少"再按名义年数年化：
      上市 2.4 年的基金会被算出一行"近5年 +12.6% / 年化 2.4%"——收益是 2.4 年的、
      年化却按 5 年除，两个数都错，而且看着完全正常。窗口起点早于上市日 → 该窗口留空。

其他：
  · 日K 用腾讯【不复权】日线，1700 根上限 ≈ 6.8 年。前/后复权那个接口只给 640 根，
    静默截断，不能用（见 开发文档 S10.26）。
  · 所以 2015 / 2018 那种级别的回撤看不到 → **最大回撤系统性偏乐观**，界面上要写明。
  · 年化按【窗口实际跨度】折算（起点取"N 年前的第一个交易日"，会差几天）。
  · 当天已算过就直接用缓存（行情一天一动，同一天没必要重算）；--force 强制重算。

用法：
    python build_etf_perf.py            # 全量（46 只，约 15 秒；当天算过则秒过）
    python build_etf_perf.py --force    # 强制重算
    python build_etf_perf.py --check 510880
"""
import os, sys, json, time, argparse
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ['NO_PROXY'] = '*'
for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(k, None)
import net  # noqa: E402

CACHE = os.path.join(HERE, 'cache', 'etf_perf.json')
DIVD = os.path.join(HERE, 'cache', 'etf_div.json')
ROWS = os.path.join(HERE, 'cache', 'etf_rows2.json')
TODAY = date.today()
WINS = (1, 3, 5)
N_DAYS = 1700


def qcode(code):
    return ('sh' if code[0] in '65' else 'sz') + code


def kline_q(qc):
    """日线【不复权】→ [[日期, 收盘], ...] 升序。qc 已经是带交易所前缀的代码。

    走 net.kline_day（多源顺次试）：2026-09-21 实测 `web.ifzq.gtimg.cn` 对本机
    一律 501，而 ifzq/proxy.finance 两个域名同结构可用 —— 原来写死 web.ifzq 时
    这里是"全军覆没 → 写成 0 只"，界面上整块「历史表现」变空。
    """
    return net.kline_day(qc, N_DAYS)


def kline(code):
    return kline_q(qcode(code))


# 同期基准（详情页「历史表现」里给一行对照，一眼看出跑赢还是跑输）。
# ⚠️ 只能用【价格指数】：全收益版拿不到 —— 腾讯 shH00300 / shH20269 都是空，
#    东财 push2 的 1.H00300 / 2.H20269 也返回 null（2026-09-21 实测）。
#    所以界面上必须写明"指数不含分红、本基金含息"，否则这个对照会骗人。
BENCH = (('sh000300', '沪深300'), ('sh000922', '中证红利'))


def idx_stat(kl, y):
    """指数在"近 N 年"窗口里的涨跌（价格口径，不含分红）"""
    cut = (TODAY - timedelta(days=int(365.25 * y))).isoformat()
    if kl[0][0] > cut:
        return None
    w = [x for x in kl if x[0] >= cut]
    if len(w) < 60:
        return None
    span = (date.fromisoformat(w[-1][0]) - date.fromisoformat(w[0][0])).days / 365.25
    r = w[-1][1] / w[0][1] - 1
    return {'ret': round(r * 100, 1),
            'cagr': round(((1 + r) ** (1 / span) - 1) * 100, 1) if span > 0 else None,
            'from': w[0][0]}


def build_bench():
    out = {}
    for qc, name in BENCH:
        try:
            kl = kline_q(qc)
        except Exception as e:
            print(f'   ✗ 基准 {name} {type(e).__name__}: {str(e)[:40]}')
            continue
        if len(kl) < 60:
            continue
        rec = {'name': name, 'from': kl[0][0]}
        for y in WINS:
            rec['w%d' % y] = idx_stat(kl, y)
        out[qc[2:]] = rec
    return out


def per_event(cum_list):
    """坑 1：累计 → 每笔（首笔 = 自身）"""
    out, prev = [], 0.0
    for d, cum in sorted(cum_list or [], key=lambda x: x[0]):
        delta = round(float(cum) - prev, 6)
        prev = float(cum)
        if delta > 0:
            out.append([str(d), delta])
    return out


def maxdd(kl):
    peak, peak_d, worst, span = -1e9, None, 0.0, None
    for d, c in kl:
        if c > peak:
            peak, peak_d = c, d
        if (c / peak - 1) * 100 < worst:
            worst, span = (c / peak - 1) * 100, [peak_d, d]
    return round(worst, 1), span


def reinv(kl, dv, d0, d1):
    """坑 2：复投 —— 每次除息按当日收盘价再买入，份额累乘"""
    px = dict(kl)
    sh = 1.0
    for d, p in dv:
        if not (d0 < d <= d1):
            continue
        c = px.get(d)
        if not c:                      # 除息日不在日K里（停牌）→ 用之前最近一天
            cand = [k for k in px if k <= d]
            c = px[max(cand)] if cand else None
        if c:
            sh *= (1 + p / c)
    return sh * px[d1] / px[d0] - 1


def build(code, cum):
    kl = kline(code)
    if len(kl) < 60:
        return None
    dv = per_event(cum)
    px_close = dict(kl)
    cur = kl[-1][1]
    dd_all, span_all = maxdd(kl)
    res = {'from': kl[0][0], 'to': kl[-1][0], 'cur': round(cur, 3),
           'years': round((TODAY - date.fromisoformat(kl[0][0])).days / 365.25, 1),
           'dd_all': dd_all, 'dd_all_span': span_all,
           'cur_dd': round((cur / max(c for _, c in kl) - 1) * 100, 1)}
    for y in WINS:
        cut = (TODAY - timedelta(days=int(365.25 * y))).isoformat()
        if kl[0][0] > cut:             # 坑 4：历史不够 → 留空
            res['w%d' % y] = None
            continue
        w = [x for x in kl if x[0] >= cut]
        if len(w) < 60:
            res['w%d' % y] = None
            continue
        d0, d1 = w[0][0], w[-1][0]
        p0 = w[0][1]
        dsum = round(sum(p for d, p in dv if d0 <= d <= d1), 4)
        nev = len([1 for d, p in dv if d0 <= d <= d1])
        real_span = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
        r_all = (cur + dsum) / p0 - 1
        r_re = reinv(kl, dv, d0, d1)
        res['w%d' % y] = {
            'all': round(r_all * 100, 1), 're': round(r_re * 100, 1),
            'px': round((cur / p0 - 1) * 100, 1),
            'cagr': round((1 + r_all) ** (1 / real_span) * 100 - 100, 1) if real_span > 0 else None,
            'cagrRe': round((1 + r_re) ** (1 / real_span) * 100 - 100, 1) if real_span > 0 else None,
            'div': dsum, 'nev': nev, 'dd': maxdd(w)[0],
            'from': d0, 'span': round(real_span, 2),
        }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()

    ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))
    rows = {m['code']: m for m in json.load(open(ROWS, encoding='utf-8'))}
    cum = json.load(open(DIVD, encoding='utf-8'))
    # 只算 App 能展示的那些：入选 12 + 备查池 34（手动加进清单的也要能看）
    codes = sorted({m['code'] for m in ui['etf']['a'] + ui['etf']['hk'] + ui['etf'].get('more', [])})

    old, oldbench = {}, {}
    if os.path.exists(CACHE) and not a.force and not a.check:
        d = json.load(open(CACHE, encoding='utf-8'))
        oldbench = d.get('bench') or {}
        if d.get('date') == str(TODAY):
            # 个股/ETF 的当天算过就跳过，但**基准只有 2 个请求**，顺手刷一下
            bench = build_bench() or oldbench
            if bench:
                d['bench'] = bench
                json.dump(d, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
            print(f'cache/etf_perf.json 已是今天的（{len(d.get("perf") or {})} 只）—— 跳过重算，'
                  f'基准已刷新（{len(bench)} 个）。要全量重算加 --force')
            return
        old = d.get('perf') or {}
    elif os.path.exists(CACHE) and a.check:
        old = (json.load(open(CACHE, encoding='utf-8')).get('perf') or {})

    todo = a.check or codes
    print(f'待算 {len(todo)} 只（每只 1 个日K请求）...', flush=True)
    t0 = time.time()

    def job(c):
        try:
            return c, build(c, cum.get(c)), None
        except Exception as e:
            return c, None, e

    out, bad = {}, []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(job, c) for c in todo]):
            c, r, err = fut.result()
            if err:
                bad.append((c, err))
            elif r:
                out[c] = r

    print(f'完成 {time.time()-t0:.0f}s：成功 {len(out)}，失败 {len(bad)}')
    for c, e in bad[:8]:
        print(f'   ✗ {c} {type(e).__name__}: {str(e)[:60]}')

    if a.check:
        for c in a.check:
            print(json.dumps({c: out.get(c)}, ensure_ascii=False, indent=1)[:1500])
        return

    # ⚠️ 一只都没算出来 = 数据源挂了（2026-09-21 就被 web.ifzq 的 501 打空过一次）。
    #    这时候**绝不能用空结果覆盖旧缓存** —— 界面上那块会直接变空，而且看不出是"坏了"。
    #    故意用非 0 退出码：本地的 build_all 会停下、云端的 Actions 会红，都得让人看见。
    if not out and old:
        print('!! 本次一只都没算出来（数据源全挂？）—— 保留旧缓存 %d 只，不改写' % len(old))
        for c, e in bad[:3]:
            print('   ✗ %s %s: %s' % (c, type(e).__name__, str(e)[:60]))
        sys.exit(1)

    # 用本次结果覆盖旧缓存（同日 old 里有的、本次没算的保留）
    merged = dict(old)
    merged.update(out)
    bench = build_bench() or oldbench
    json.dump({'date': str(TODAY), 'perf': merged, 'bench': bench},
              open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'→ {CACHE}（{len(merged)} 只）')
    for k, v in bench.items():
        print('   基准 %s：近1年 %s%%  近3年 %s%%  近5年 %s%%' % (
            v['name'],
            (v.get('w1') or {}).get('ret'), (v.get('w3') or {}).get('ret'),
            (v.get('w5') or {}).get('ret')))
    n5 = sum(1 for v in merged.values() if v.get('w5'))
    n3 = sum(1 for v in merged.values() if v.get('w3'))
    print(f'   有近5年 {n5} 只 / 有近3年 {n3} 只 / 只有近1年 {len(merged)-n3} 只')


if __name__ == '__main__':
    main()
