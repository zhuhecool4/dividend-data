# -*- coding: utf-8 -*-
"""刷新 cache/etf_div.json —— 全市场红利类 ETF 的【累计分红】序列。

为什么要这个脚本（2026-10-22）：`etf_div.json` 是历史一次性脚本的产物，停在 2026-09-16。
561580 / 513530 两只**月度分红** ETF 的 10 月那笔（2026-10-19）因此抓不到 → 末笔距今 38 天
> med 28×1.35 → 被判 `stale` → 从 ETF 清单里掉出去（12 → 10，连带 7 条断言红）。
`etf_pick.py` 里那段抓取也补不回来：它的 `todo = [c for c in pool if c not in DIV]` 只抓
"缓存里没有的"，已有的永远不会刷新；而且它依赖 akshare（本机没装、pip 也装不上 3.13 的包）。

数据源（**零依赖**，不装 akshare）：新浪的复权因子文件里就带累计分红 ——
    https://finance.sina.com.cn/realstock/company/{sh|sz}{code}/hfq.js
    → var sh561580hfq={"total":19,"data":[{"d":"2026-10-19","f":...,"s":...,"u":1.16}, ...]}
`u` = 累计分红（元/份）。akshare 的 `fund_etf_dividend_sina` 就是照抄这个文件
（见 akshare/fund/fund_etf_sina.py），这里只是把 pandas 那层壳去掉。

口径：写回 `{代码: [[日期, 累计分红], ...]}`（与老缓存完全同形状）。下游
`export_ui_data.etf_events()` 靠相邻差分还原每笔分红，所以**累计值**不能换成单笔。

用法：
    python refresh_etf_div.py            # 全量刷 91 只（约 30 秒）
    python refresh_etf_div.py 561580 513530   # 只刷指定的几只（增量调试用）
"""
import os, sys, json, time, urllib.request

sys.stdout.reconfigure(encoding='utf-8')
for _k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)
os.environ['NO_PROXY'] = '*'

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store

POOL = os.path.join(HERE, 'cache', 'etf_pool.json')
OUT = os.path.join(HERE, 'cache', 'etf_div.json')
UA = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}


def sym(code):
    return ('sh' if code[0] in '56' else 'sz') + code


def fetch_cum(code, tries=3):
    """→ [[日期, 累计分红], ...] 升序；失败 None（**别用 [] 顶替**：空会覆盖掉旧数据）。"""
    u = 'https://finance.sina.com.cn/realstock/company/%s/hfq.js' % sym(code)
    for i in range(tries):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                         timeout=15).read().decode('utf-8', 'ignore')
            if not raw.startswith('var'):
                raise ValueError('非 var 开头（接口变了？）：%s' % raw[:60])
            # ⚠️ 文件末尾在 JSON 之后还有别的东西（akshare 用 rsplit('}') 截断，照抄）
            body = raw.split('=', 1)[1].strip().rsplit('}', 1)[0].strip() + '}'
            d = json.loads(body)
            rows = []
            for r in (d.get('data') or []):
                dd = str(r.get('d') or '')
                if not dd or dd == '1900-01-01':
                    continue
                try:
                    cum = round(float(r.get('u') or 0), 4)
                except (TypeError, ValueError):
                    continue
                if cum > 0:
                    rows.append([dd, cum])
            rows.sort()
            return rows or None
        except Exception as e:
            if i == tries - 1:
                return ('ERR', str(e))
            time.sleep(1.0 + i)


def main():
    want = sys.argv[1:]
    pool = json.load(open(POOL, encoding='utf-8'))
    codes = want or sorted(pool)
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding='utf-8')) or {}
        except Exception:
            old = {}
    out = dict(old) if want else {}          # 指定代码时保留其余；全量时整体重建
    ok = fail = 0
    t0 = time.time()
    for i, c in enumerate(codes, 1):
        r = fetch_cum(c)
        if isinstance(r, tuple):             # 抓取失败：保留旧值（别把已有数据冲掉）
            fail += 1
            if c in old:
                out[c] = old[c]
            print('  ✗ %s %s' % (c, r[1][:60]))
        elif r is None:
            ok += 1                          # 接口通、没有分红记录（不分红/新上市）
            out.pop(c, None)
            print('  · %s 无分红记录（清掉旧条目）' % c)
        else:
            ok += 1
            out[c] = r
            print('  ✓ %s %d 笔，末笔 %s 累计 %s' % (c, len(r), r[-1][0], r[-1][1]))
        if i % 20 == 0:
            print('  ... %d/%d  %.0fs' % (i, len(codes), time.time() - t0), flush=True)
        time.sleep(0.25)
        if want and i % 20 == 0:
            store.dump_json(OUT, out, ensure_ascii=False, empty_ok=False)
    # 空 = 抓取全失败，绝不覆盖旧缓存（同 yield_hist / mkt_px 那道闸）
    store.dump_json(OUT, out, ensure_ascii=False, empty_ok=False)
    print('完成 %.0fs：成功 %d 只、失败 %d 只，共 %d 只 → cache/etf_div.json'
          % (time.time() - t0, ok, fail, len(out)))


if __name__ == '__main__':
    main()
