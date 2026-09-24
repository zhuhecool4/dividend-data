# -*- coding: utf-8 -*-
"""公司概况 · 主营构成 构建器

产出 cache/profile.json —— 详情页「公司简介 · 主营构成」那一块用：
    {代码: {"profile": 简介, "business": 主营, "board": 行业层级, "listed": 上市日,
            "city": 省市, "emp": 员工数, "audit": 审计机构,
            "opReport": 报告期, "items": [[产品, 占比%], ...], "regions": [[地区, 占比%], ...]}}

数据源（都是东财 F10，每只 2 个请求）：
    RPT_F10_BASIC_ORGINFO   公司概况 —— ORG_PROFILE / MAIN_BUSINESS / BOARD_NAME_LEVEL …
    RPT_F10_FN_MAINOP       主营构成 —— 按产品(MAINOP_TYPE=2) / 按地区(=3)

⚠️⚠️ 三个实测踩到的坑 ⚠️⚠️

坑 1  RPT_F10_BASIC_ORGINFO 【没有 REPORT_DATE 字段】。
      带上 sortColumns=REPORT_DATE&sortTypes=-1（本项目其它表都这么写）会
      **静默返回 0 行**，看起来就像"这家公司没有概况资料"。
      第一次探这个接口就是这么判成"接口不存在"的。→ 这张表不许带排序参数。

坑 2  RPT_F10_FN_MAINOP 【必须】带 REPORT_DATE 降序，否则 rows[0] 是 2001 年报
      （实测华域汽车第一条是"2001年报"，取到的占比是二十多年前的）。
      同一批接口里，这两张表的要求正好相反。

坑 3  占比字段叫 MBI_RATIO（是【小数】0.7204），不是 MAIN_BUSINESS_RPOFIT_RATIO
      （那个字段根本不存在，取到 None 会静默让所有占比变 0%）。

其他口径：
  · 只留【最新报告期】的构成。中报口径的占比和年报不同，所以报告期要一起存下来、
    界面上写明（"主营构成 · 2026中报"），不能只写"主营构成"。
  · 简介截到 PROFILE_CHARS 字、断在标点上 —— 全文 400+ 字，payload 撑不起，
    界面上也不会有人读完。
  · 增量：已缓存的跳过（省 2 个请求/只）；--force 全量重拉。

用法：
    python build_profile.py                 # 增量（读 ui_data.json 的全池名单）
    python build_profile.py --force         # 全量
    python build_profile.py --check 600741  # 只看指定代码（不落盘）
"""
import os, sys, json, time, argparse, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed


sys.stdout.reconfigure(encoding='utf-8')
os.environ['NO_PROXY'] = '*'
for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
    os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net  # noqa: E402
# 「池外高息」的阈值：与榜单第三个 chip、export 的 mktEx 同一处真源（2026-09-24 加）
from config import POOL_MIN  # noqa: E402

CACHE = os.path.join(HERE, 'cache', 'profile.json')
BASE = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
ORGINFO = 'RPT_F10_BASIC_ORGINFO'
MAINOP = 'RPT_F10_FN_MAINOP'
PROFILE_CHARS = 200


def mk_suffix(code):
    if code[0] == '6':
        return 'SH'
    return 'SZ' if code[0] in '03' else 'BJ'


def pull(report, filt, size, sort=None):
    p = {'reportName': report, 'columns': 'ALL', 'filter': filt,
         'pageSize': str(size), 'pageNumber': '1',
         'source': 'WEB', 'client': 'WEB'}
    if sort:                                     # 坑 1 / 坑 2：由调用方决定要不要排序
        p['sortColumns'], p['sortTypes'] = sort
    j = net.get(BASE + '?' + urllib.parse.urlencode(p), encoding='utf-8', timeout=25).json()
    return (j.get('result') or {}).get('data') or []


def clip(txt, n=PROFILE_CHARS):
    """截到 n 字，尽量断在标点上（'…激光雷' 这种断法很显眼）"""
    s = (txt or '').strip().replace('\u3000', ' ')
    if len(s) <= n:
        return s
    cut = max(s.rfind(c, int(n * 0.6), n) for c in '。；，、')
    return (s[:cut + 1] if cut > 0 else s[:n]) + '…'


def build(code):
    suf = mk_suffix(code)
    org = pull(ORGINFO, f'(SECUCODE="{code}.{suf}")', 1)          # 坑 1：不排序
    op = pull(MAINOP, f'(SECUCODE="{code}.{suf}")', 60,
              sort=('REPORT_DATE', '-1'))                          # 坑 2：必须排序
    if not org and not op:
        return None
    o = org[0] if org else {}
    out = {
        'name': o.get('SECURITY_NAME_ABBR'),
        'profile': clip(o.get('ORG_PROFILE')),
        'business': clip(o.get('MAIN_BUSINESS'), 120),
        'board': o.get('BOARD_NAME_LEVEL'),
        'listed': str(o.get('LISTING_DATE') or '')[:10] or None,
        'city': city_of(o.get('REG_ADDRESS')),
        'emp': o.get('EMP_NUM'),
        'audit': o.get('ACCOUNTFIRM_NAME'),
    }
    if op:
        rep = op[0].get('REPORT_NAME')
        rows = [r for r in op if r.get('REPORT_NAME') == rep]
        out['opReport'] = rep
        # 坑 3：占比字段是 MBI_RATIO（小数）
        out['items'] = [[r.get('ITEM_NAME'), round((r.get('MBI_RATIO') or 0) * 100, 1)]
                        for r in sorted(rows, key=lambda r: -(r.get('MBI_RATIO') or 0))
                        if r.get('MAINOP_TYPE') == '2'][:6]
        out['regions'] = [[r.get('ITEM_NAME'), round((r.get('MBI_RATIO') or 0) * 100, 1)]
                          for r in sorted(rows, key=lambda r: -(r.get('MBI_RATIO') or 0))
                          if r.get('MAINOP_TYPE') == '3'][:4]
    return out


def city_of(addr):
    """「中国上海市威海路489号」→「上海市」。直接切前 N 个字会切在词中间。"""
    if not addr:
        return None
    a = str(addr).replace('中国', '', 1)
    return (a.split('市')[0] + '市') if '市' in a else a[:8]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--check', nargs='*')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()

    ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))
    codes = {m['code'] for m in
             ui['qual'] + ui['broad'] + ui.get('selfAdd', []) + ui.get('poolRest', [])}
    # 池外高息名单也要公司概况（2026-09-24 Z 定）—— 轻详情页要显示"这公司是干什么的"。
    # 名单从上一版 ui_data.json 的 mkt 快照里取：近一年实收 ÷ 现价 ≥ POOL_MIN
    # （阈值与榜单第三个 chip、export 的 mktEx 同一处真源 —— config.POOL_MIN；
    #  这里读的是【上一版】的 mkt，名单每天最多差几只，下一次跑就补齐）。
    _pool_ex = {m[0] for m in (ui.get('mkt') or [])
                if m[4] and m[3] / m[4] * 100 >= POOL_MIN}
    if _pool_ex:
        print(f'（其中池外高息 {len(_pool_ex)} 只 —— 轻详情页要用的公司概况）', flush=True)
    codes = sorted(codes | _pool_ex)
    cache = {}
    if os.path.exists(CACHE) and not a.force and not a.check:
        cache = json.load(open(CACHE, encoding='utf-8'))

    todo = a.check or [c for c in codes if c not in cache]
    print(f'待抓 {len(todo)} 只（已缓存 {len(cache)}，每只 2 个请求）...', flush=True)
    t0 = time.time()

    def job(c):
        for _ in range(3):
            try:
                return c, build(c), None
            except Exception as e:
                last = e
                time.sleep(1.2)
        return c, None, last

    bad, done = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(job, c) for c in todo]):
            c, r, err = fut.result()
            done += 1
            if err:
                bad.append((c, err))
            elif r:
                cache[c] = r
            if done % 50 == 0:
                print(f'  ... {done}/{len(todo)}  {time.time()-t0:.0f}s', flush=True)

    print(f'完成 {time.time()-t0:.0f}s；本次成功 {len(todo)-len(bad)}，'
          f'失败 {len(bad)}，缓存共 {len(cache)}/{len(codes)}')
    for c, e in bad[:8]:
        print(f'   ✗ {c} {type(e).__name__}: {str(e)[:60]}')

    if not a.check:
        json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
        print(f'→ {CACHE}')

    miss = [c for c in codes if c not in cache]
    if miss:
        print(f'⚠ 无概况数据 {len(miss)} 只：' + '、'.join(miss[:12]))
    noitems = [c for c, v in cache.items() if not v.get('items')]
    if noitems:
        print(f'⚠ 无主营构成 {len(noitems)} 只：' + '、'.join(noitems[:12]))

    if a.check:
        for c in a.check:
            print(f'\n=== {c} ===')
            print(json.dumps(cache.get(c), ensure_ascii=False, indent=1)[:1400])
    else:
        print('\n抽样（前 6 只）：')
        for c in sorted(cache)[:6]:
            v = cache[c]
            it = '、'.join(f'{x[0]} {x[1]}%' for x in (v.get('items') or [])[:3])
            print(f"  {c} {v.get('name','')[:8]:<9}{(v.get('board') or '')[:20]:<22}"
                  f"{v.get('opReport','—'):<9}{it}")


if __name__ == '__main__':
    main()
