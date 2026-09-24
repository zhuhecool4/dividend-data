# -*- coding: utf-8 -*-
"""近 5 年股息率分位 —— 回答"当前股息率在自己历史上算高还是低"。

口径：
    历史点(y) = 该年【按报告期归集】的每股分红 ÷ 该年末【不复权】收盘价
    当前点   = TTM 每股分红 ÷ 现价        （与 yield2.json 的主口径一致）
    分位     = 历史序列里小于当前值的占比

为什么用不复权价：股息率是"分红/真实成交价"的比值。用后复权价做分母，
价格被累计复权因子放大，股息率会被系统性低估（长期高分红股尤其严重）。

输出 cache/yield_hist.json:
    {代码: {"series": [[年, 股息率], ...], "cur": 当前股息率, "pct": 分位,
            "px": {"y5": {...}, "y1": {...}, "tot": {...}}}}

    px = 价格位置（2026-09-21 加，详情页「估值与价格位置」用）。同一次日K请求里
    顺手算出来的，**零新增请求**：
        y5/y1 = 近5年 / 近1年：{min, max, pct(现价分位), chg(区间涨跌), from}
        tot   = 近5年含息回报：{ret, divSum, p0, n}
                ret = (期末价 + 窗口内每股分红) / 期初价 − 1，**分红不计再投资**。
    ⚠️ 含息为什么不用【前复权】K线：腾讯的前/后复权只返回 640 根（约 2.6 年），
       传 1700 也不给、且不报错。拿它算"近5年"会得到一个 2.6 年的收益数。
       实测 600741/601088/600036/000895 四只全是 640 根，起点 2024-01。

用法：
    python build_yield_hist.py            # 增量（已缓存的跳过；缺 px 的会补抓）
    python build_yield_hist.py --force    # 全量重拉
    python build_yield_hist.py --check 600036 601088
"""
import os, sys, json, time, argparse, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import OVERRIDE, EXCLUDE, SHORT, sn, qualifies     # noqa: E402
from config import FORCE_BASE                                  # noqa: E402
import net                                                     # noqa: E402
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['NO_PROXY'] = '*'

CACHE = os.path.join(HERE, 'cache', 'yield_hist.json')
DIVD = os.path.join(HERE, 'cache', 'div_detail2.json')
TODAY = date.today()
YEAR = TODAY.year
YEARS = [YEAR - i for i in range(5, 0, -1)]      # 最近 5 个【完整】年度
N_DAYS = 1700                                    # 约 6.8 年交易日，够覆盖 YEARS[0]
PX_WIN = 365 * 5                                 # 价格位置的"近5年"


def day_back(days):
    return date.fromordinal(TODAY.toordinal() - days).isoformat()


def ex_divs(code, from_day, to_day):
    """窗口内【已除息】的每股税前分红，按除息日取 —— 含息回报用。

    数据源 cache/div_detail2.json：[除息日, 每股, 报告期, 登记日, ...]。
    只要买入早于除息日就拿得到这笔，所以窗口内全部计入。
    """
    if not os.path.exists(DIVD):
        return []
    arr = (json.load(open(DIVD, encoding='utf-8')) or {}).get(code) or []
    out = []
    for r in arr:
        try:
            d, per = str(r[0]), float(r[1])
        except (IndexError, TypeError, ValueError):
            continue
        if from_day <= d <= to_day and per > 0:
            out.append([d, per])
    return sorted(out)


def win_stats(kl, days):
    """窗口内的价格位置。kl = [[日期, 收盘], ...] 升序"""
    cut = day_back(days)
    w = [x for x in kl if x[0] >= cut]
    if len(w) < 20:                     # 样本太少不算（新股/长期停牌）
        return None
    closes = [c for _, c in w]
    last = closes[-1]
    return {'from': w[0][0], 'min': round(min(closes), 2), 'max': round(max(closes), 2),
            'pct': round(sum(1 for x in closes if x < last) / len(closes) * 100, 1),
            'chg': round(last / closes[0] * 100 - 100, 1)}


def price_pos(code, kl):
    """y5 / y1 / tot 三块。现价一律取【K线最后一根收盘】——
    和序列同源，不会出现"实时价配历史序列"的错配（yield2 的价是另一次抓的）。"""
    y5 = win_stats(kl, PX_WIN)
    if not y5:
        return None
    y1 = win_stats(kl, 365)
    p0 = next(c for d, c in kl if d == y5['from'])
    p1 = kl[-1][1]
    dv = ex_divs(code, y5['from'], kl[-1][0])
    dsum = round(sum(x[1] for x in dv), 3)
    return {'y5': y5, 'y1': y1,
            'tot': {'p0': round(p0, 2), 'ret': round((p1 + dsum) / p0 * 100 - 100, 1),
                    'divSum': dsum, 'n': len(dv)}}


def qcode(code):
    if code[:2] in ('92', '43', '83', '87'):
        return 'bj' + code
    return ('sh' if code[0] in '65' else 'sz') + code


def fetch_kline(code):
    """日线【不复权】→ [[日期, 收盘], ...] 升序。

    走 net.kline_day（多源顺次试）。2026-09-21 实测 `web.ifzq.gtimg.cn` 对本机一律
    501（原来写死这个域名），换成 ifzq / proxy.finance 都同结构可用。
    """
    return net.kline_day(qcode(code), N_DAYS)


def year_end_closes(kl):
    """{年份: 该年最后一个交易日收盘价}"""
    out = {}
    for d, c in kl:
        y = int(d[:4])
        out[y] = c                                    # kl 升序 → 最后写入的即年末
    return out


def build(code, by_rp, ttm, price):
    kl = fetch_kline(code)
    if not kl:
        return None
    ye = year_end_closes(kl)
    series = []
    for y in YEARS:
        c = ye.get(y)
        if not c:
            continue
        div = by_rp.get(str(y), 0.0)
        series.append([y, round(div / c * 100, 2)])
    if len(series) < 3:
        return None
    cur = round(ttm, 2)
    below = sum(1 for _, v in series if v < cur)
    pct = round(below / len(series) * 100)
    return {'series': series, 'cur': cur, 'pct': pct,
            'px': price_pos(code, kl)}      # 同一次请求里顺手算，零新增


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()

    y2 = json.load(open(os.path.join(HERE, 'cache', 'yield2.json'), encoding='utf-8'))
    ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))
    # 覆盖全部导出标的，不只是入榜的两个池子 —— 「自加入」可以加池内任意标的，
    # 它们的近5年分位也要算（原来只算 141 只，加进来的会缺这一块）
    codes = sorted({m['code'] for m in ui['qual'] + ui['broad'] + ui.get('selfAdd', []) + ui.get('poolRest', [])})

    cache = {}
    prev = {}
    if os.path.exists(CACHE) and not a.check:
        try:
            prev = json.load(open(CACHE, encoding='utf-8')) or {}
        except Exception:
            prev = {}
        if not a.force:
            cache = dict(prev)

    # ⚠️ 迁移判据：光看"在不在缓存里"不行 —— 加了 px 之后老缓存全都不带它，
    #    跑一次增量会一只都不补（0 只待抓），界面上一片空白还以为接口坏了。
    todo = a.check if a.check else [c for c in codes if c not in cache or 'px' not in cache[c]]
    print(f'待抓 {len(todo)} 只（已缓存 {len(cache)}）...', flush=True)
    t0 = time.time()

    def job(c):
        m = y2.get(c) or {}
        by_rp = m.get('by_rp') or {}
        if not by_rp:
            return c, None, 'no by_rp'
        for _ in range(3):
            try:
                return c, build(c, by_rp, m.get('ttm', 0), m.get('price', 0)), None
            except Exception as e:
                last = e
                time.sleep(1.2)
        return c, None, last

    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(job, c) for c in todo]):
            c, res, err = fut.result()
            done += 1
            if err:
                print(f'  ✗ {c} {err}')
            elif res:
                cache[c] = res
            if done % 40 == 0:
                print(f'  ... {done}/{len(todo)}  {time.time()-t0:.0f}s', flush=True)

    if not a.check:
        # ⚠️ 一只都没算出来 = 数据源挂了（--force 时 cache 是空的，正好会写成空文件）。
        #    绝不用空结果覆盖旧缓存：分位/价格位置会整块变空，而且看不出是"坏了"。
        if not cache and prev:
            print('!! 本次一只都没算出来（数据源全挂？）—— 保留旧缓存 %d 只，不改写' % len(prev))
            sys.exit(1)
        json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'完成 {time.time()-t0:.0f}s，共 {len(cache)} 只')

    if a.check:
        for c in a.check:
            r = cache.get(c)
            if not r:
                print(f'  {c}: 无数据'); continue
            print(f'  {c}  当前 {r["cur"]:.2f}%  分位 {r["pct"]}%  '
                  f'序列 {r["series"]}')
            px = r.get('px')
            if not px:
                print('       价格位置：无（日K 太短或没抓到）'); continue
            y5, y1, tt = px['y5'], px['y1'], px['tot']
            print(f'       近5年 {y5["min"]}~{y5["max"]} 分位 {y5["pct"]}% 涨跌 {y5["chg"]}%'
                  f'（{y5["from"]} 起）')
            if y1:
                print(f'       近1年 {y1["min"]}~{y1["max"]} 分位 {y1["pct"]}% 涨跌 {y1["chg"]}%')
            print(f'       含息5年 {tt["ret"]}% = ({tt["p0"]} 期初价 + {tt["divSum"]} 每股分红'
                  f'，共 {tt["n"]} 次除息)')
    else:
        # 抽样打印，肉眼核对量级
        print('\n抽样（当前 vs 近5年序列 → 分位）:')
        for c in codes[:8]:
            r = cache.get(c)
            if r:
                print('  %s 当前 %5.2f%%  %s  分位 %d%%'
                      % (c, r['cur'], ' '.join('%.1f' % v for _, v in r['series']), r['pct']))


if __name__ == '__main__':
    main()
