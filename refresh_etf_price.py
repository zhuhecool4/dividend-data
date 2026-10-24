# -*- coding: utf-8 -*-
"""刷新 cache/etf_pool.json 里的【现价 + 规模】—— ETF 股息率的分母。

为什么要有这个脚本（2026-10-24 Z 报"513530 股息率一直是 7.64%"）：
    红利 ETF 的股息率 = 近 365 天每份分红 ÷ **现价**，而现价来自 `cache/etf_pool.json` ——
    那是 2026-09-16 一次性扫描脚本的产物，之后再没人动过。分红那边有
    `refresh_etf_div.py` 每天刷（分子是新的），分母却冻在 09-16：
        513530：0.12 ÷ 1.57 = 7.64%（快照价）　vs　0.12 ÷ 1.491 = 8.05%（10-23 收盘）
    于是"股息率一直不变"，而且**每只 ETF 都偏**（510880 快照 3.367 / 实际 3.310 等）。
    `etf_pick2.py` 里没有取价逻辑（它只读 pool 里的 price），所以光重跑它也修不了。

做法：腾讯行情批量取（一只一条 URL 太慢，30 只一批），字段：
    f[3]  现价       f[45] 总市值（亿元，= 份额×现价 → 界面上的「规模」）
    f[30] 行情时间（YYYYMMDDhhmmss，取日期写进 pdate 备查）
写回 pool 的 price / size / pdate，然后默认接着重算 `etf_rows2.json`（跑 etf_pick2.py），
因为 rows2 才是 export_ui_data 真正读的那份 —— 只改 pool 不改 rows2 等于没改。

⚠️ 抓不到 / 停牌价格 0 的票**保留旧值**（不能拿 0 或者 None 顶上去：分母一变成 0，
   股息率就成 inf 或者整只从清单里掉出去）。全失败就不落盘（store.dump_json 的 empty_ok 闸）。

用法：
    python refresh_etf_price.py              # 取价 + 重算 rows2
    python refresh_etf_price.py --no-pick    # 只取价（调试用）
    python refresh_etf_price.py --dry        # 只打印将要变成什么，不落盘
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
UA = {'User-Agent': 'Mozilla/5.0'}
BATCH = 30            # 30 只一批：URL ~300 字符，91 只 = 4 个请求，约 2 秒


def sym(code):
    """交易所前缀：5 开头 = 上交所，1 开头 = 深交所（红利 ETF 只有这两种）。"""
    return ('sh' if code[0] in '56' else 'sz') + code


def fetch_batch(codes, tries=3):
    """→ {code: (price, size亿, 'YYYY-MM-DD')}；失败的票不在返回里（调用方保留旧值）。"""
    url = 'http://qt.gtimg.cn/q=' + ','.join(sym(c) for c in codes)
    for i in range(tries):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=20).read().decode('gbk', 'ignore')
            out = {}
            for seg in raw.split(';'):
                if '=' not in seg:
                    continue
                parts = seg.split('"')
                if len(parts) < 2:
                    continue
                f = parts[1].split('~')
                if len(f) < 46:
                    continue
                code = f[2].strip()
                try:
                    price = float(f[3])
                    size = float(f[45])
                except (TypeError, ValueError):
                    continue
                if price <= 0:                       # 停牌 / 行情缺 → 宁可留旧值
                    continue
                ts = f[30].strip()
                pdate = ('%s-%s-%s' % (ts[0:4], ts[4:6], ts[6:8])) if len(ts) >= 8 else ''
                out[code] = (price, size if size > 0 else None, pdate)
            return out
        except Exception as e:                       # noqa: BLE001 —— 网络抖动重试
            if i == tries - 1:
                print('  取价失败（第 %d 批）：%s' % (tries, e))
            else:
                time.sleep(1.5 * (i + 1))
    return {}


def main():
    args = sys.argv[1:]
    dry = '--dry' in args
    pool = json.load(open(POOL, encoding='utf-8'))
    codes = list(pool)
    got = {}
    print('取价 %d 只（%d 只一批）...' % (len(codes), BATCH))
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        got.update(fetch_batch(chunk))
        time.sleep(0.3)

    if not got:
        print('!! 一只都没取到 —— 不落盘（旧价保留）')
        return 1

    moved, pdate = [], ''
    for c, (price, size, pd) in got.items():
        old = pool[c].get('price') or 0
        pdate = pdate or pd
        pool[c]['price'] = round(price, 4)
        if size:
            pool[c]['size'] = round(size, 2)
        if pd:
            pool[c]['pdate'] = pd
        if old:
            moved.append((abs(price / old - 1), c, old, price))

    print('取到 %d/%d 只；行情日 %s' % (len(got), len(codes), pdate or '—'))
    miss = [c for c in codes if c not in got]
    if miss:
        print('  保留旧价 %d 只：%s' % (len(miss), '、'.join(miss[:12]) + ('…' if len(miss) > 12 else '')))
    moved.sort(reverse=True)
    print('  价格变动最大的 5 只（旧 → 新）：')
    for d, c, o, n in moved[:5]:
        print('    %-8s %7.3f → %7.3f  %+.2f%%   %s'
              % (c, o, n, (n / o - 1) * 100, pool[c].get('name', '')))

    if dry:
        print('（--dry：没落盘、也没重算 etf_rows2.json）')
        return 0

    store.dump_json(POOL, pool, indent=1, ensure_ascii=False, empty_ok=False)
    print('已写回 cache/etf_pool.json')

    if '--no-pick' in args:
        print('（--no-pick：没重算 rows2 —— 界面读的是 rows2，别忘补一遍）')
        return 0

    # rows2 才是 export_ui_data 读的那份：pool 改完必须重算（约 1 秒，不动网络）
    print('重算 cache/etf_rows2.json ...')
    import etf_pick2                                    # noqa: F401 —— 导入即执行（它的 main 就是全脚本）
    return 0


if __name__ == '__main__':
    sys.exit(main())
