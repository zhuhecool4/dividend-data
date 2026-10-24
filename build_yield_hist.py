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
import store
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['NO_PROXY'] = '*'

CACHE = os.path.join(HERE, 'cache', 'yield_hist.json')
DIVD = os.path.join(HERE, 'cache', 'div_detail2.json')
# 池外高息名单的价格位置（2026-10-22，S40）：与池内**同一段代码**算出来，只是名单和
# 除息流水来源不同（见 mkt_px_main）。export_ui_data 的 MKT_VAL 读它。
MKTPX = os.path.join(HERE, 'cache', 'mkt_px.json')
MKT_DIVD = os.path.join(HERE, 'cache', 'market_div.json')
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


_MKDIV = None
def mkt_ex_divs(code, from_day, to_day):
    """池外票的除息流水 —— 形状与 ex_divs 完全一致，但数据源是 cache/market_div.json
    （scan_market 扫全市场的那张分红表；div_detail2 只覆盖池内，池外票在里面是空的）。

    ⚠️ 别把两个源合并成一个函数按代码猜：同一只票在两个源里的覆盖范围不同
       （div_detail2 有"实施分配 + 除息日"过滤，market_div 是另一把尺），混着读会
       在池内票上算出两个含息回报。
    """
    global _MKDIV
    if _MKDIV is None:
        try:
            _MKDIV = json.load(open(MKT_DIVD, encoding='utf-8')) or {}
        except Exception:
            _MKDIV = {}
    out = []
    for r in (_MKDIV.get(code) or []):
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


def price_pos(code, kl, div_fn=None):
    """y5 / y1 / tot 三块。现价一律取【K线最后一根收盘】——
    和序列同源，不会出现"实时价配历史序列"的错配（yield2 的价是另一次抓的）。

    div_fn：除息流水的取法，默认 ex_divs（池内，div_detail2）。池外传 mkt_ex_divs
    （market_div）—— **算法一个字不分叉**，只换数据源（2026-10-22）。
    """
    y5 = win_stats(kl, PX_WIN)
    if not y5:
        return None
    y1 = win_stats(kl, 365)
    p0 = next(c for d, c in kl if d == y5['from'])
    p1 = kl[-1][1]
    dv = (div_fn or ex_divs)(code, y5['from'], kl[-1][0])
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


def mkt_px_main(force, workers):
    """池外高息名单的「价格位置」→ cache/mkt_px.json（2026-10-22，S40）。

    为什么放在这份文件里：`fetch_kline` / `win_stats` / `price_pos` 都在这里，池内池外
    **同一段代码**算出来的才是同一个口径（手机端 pxPosOf 现算的那套也是照抄这里）。

    名单**不自己复算**：直接取上一版 ui_data.json 里 `mktEx` 的键 —— 那一批就是
    "池外高息名单"（近一年实收 ÷ 现价 ≥ 4%、非 ST、非池内，由 export_ui_data 判的）。
    自己再按分红表算一遍 = 第二份口径，迟早与 MKT_EX 漂开（这工程的老毛病）。

    时序是"慢一拍"的，这是有意的：
        export_ui_data（出 mkt/mktEx）→ 本脚本读上一版的名单算 px → 下次 export 用上它。
    首次跑（上一版还没有 mktEx）就打一行说明跳过，下一轮出包自然补上。

    ⚠️ 只覆盖名单内的票（202 只 ≈ 25~50 秒）；名单外的池外票估值块整块不渲染，
       页面自己退回"客户端现拉"那条路（pxPosOf 的第三级），不是崩。
    ⚠️ 增量：算过的跳过（--force 重拉）。空结果绝不覆盖旧缓存（同 yield_hist 的闸）。
    """
    try:
        ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))
    except Exception:
        print('池外价格位置：读不到 ui_data.json，跳过')
        return
    codes = sorted((ui.get('mktEx') or {}).keys())
    if not codes:
        print('池外价格位置：上一版 ui_data.json 里没有 mktEx 名单，跳过（下一轮出包会补）')
        return
    cache = {}
    if os.path.exists(MKTPX) and not force:
        try:
            cache = json.load(open(MKTPX, encoding='utf-8')) or {}
        except Exception:
            cache = {}
    todo = [c for c in codes if c not in cache or not (cache.get(c) or {}).get('y5')]
    print(f'池外价格位置：名单 {len(codes)} 只，待抓 {len(todo)} 只（已缓存 {len(cache)}）...',
          flush=True)
    if not todo:
        return
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(lambda c=c: (c, fetch_kline(c))): c for c in todo}
        for fut in as_completed(futs):
            c, kl = fut.result()
            done += 1
            if kl:
                px = price_pos(c, kl, mkt_ex_divs)
                if px:
                    cache[c] = px
            if done % 40 == 0:
                print(f'  ... {done}/{len(todo)}  {time.time()-t0:.0f}s', flush=True)
    # 只留名单内的（名单会变：新入选的加、掉出去的删）—— 与 mktEx 的成员保持一致
    out = {c: cache[c] for c in codes if (cache.get(c) or {}).get('y5')}
    if not out:
        print('!! 池外价格位置一只都没算出来（数据源全挂？）—— 保留旧缓存，不改写')
        return
    store.dump_json(MKTPX, out, ensure_ascii=False, empty_ok=False)   # 空 = 抓取全失败，别覆盖旧缓存
    print(f'池外价格位置：{len(out)} 只（{time.time()-t0:.0f}s）→ cache/mkt_px.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--refresh-pct', action='store_true',
                    help='只按新的 yield2 ttm 重算缓存里的 cur/pct，不拉日K、不联网')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()

    y2 = json.load(open(os.path.join(HERE, 'cache', 'yield2.json'), encoding='utf-8'))
    ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))

    # --refresh-pct：pct（近5年分位）= "series 里低于当前 ttm 的年份占比"，依赖【当前 ttm】。
    # ttm 的口径改过之后（2026-09-28 加过期判断，17 只从老周期归零），pct 就过期了 ——
    # 而日K和 series 一个都没变，重拉一遍纯属浪费。这个模式只用缓存里的 series 重算。
    # ⚠️ 别把它并进普通增量：普通增量靠"在不在缓存里"决定待抓，改了 ttm 它一只都不会动。
    if a.refresh_pct:
        cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
        n = 0
        skipped = 0
        for c, r in cache.items():
            if not (r and r.get('series')):
                continue
            if c not in y2:
                # ⚠️ 跳过保护（2026-09-29 Z 定）：分位缓存里有、yield2 里没有的票，
                #    多半是"暂时缺数据"而不是"真没分红"—— 不能按 ttm=0 把分位清零。
                #    （yield2 里真没有的停派票由过期判断归零，不走这里。）
                skipped += 1
                continue
            cur = round((y2.get(c) or {}).get('ttm', 0), 2)
            pct = round(sum(1 for _, v in r['series'] if v < cur) / len(r['series']) * 100)
            if r.get('cur') != cur or r.get('pct') != pct:
                n += 1
                r['cur'], r['pct'] = cur, pct
        store.dump_json(CACHE, cache, ensure_ascii=False, empty_ok=False)   # 空 = 抓取全失败，别覆盖旧缓存（S10.75）
        print(f'--refresh-pct：按新 ttm 重算了 {n} 只的 cur/pct'
              f'（跳过 yield2 里没有的 {skipped} 只，共 {len(cache)} 只）')
        return

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
        store.dump_json(CACHE, cache, ensure_ascii=False, empty_ok=False)   # 空 = 抓取全失败，别覆盖旧缓存（S10.75）
    print(f'完成 {time.time()-t0:.0f}s，共 {len(cache)} 只')

    # 池外高息名单的价格位置（2026-10-22）：池内跑完再跑，同一套 fetch_kline，
    # 名单取上一版 ui_data.json 的 mktEx（见 mkt_px_main 的说明）。
    # ⚠️ --check 不碰它（只查指定代码，不该顺带联网抓 200 只）。
    if not a.check:
        mkt_px_main(a.force, a.workers)

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
