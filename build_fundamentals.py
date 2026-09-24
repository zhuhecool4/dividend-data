# -*- coding: utf-8 -*-
"""基本面 · 股息安全垫 构建器

产出 cache/fundamentals.json，7 个字段：
    ocfCover3y / fcfCover3y / fcfCoverLatest / npLatest / year / basis / fin

覆盖倍数回答的是「这一年挣的现金流够不够发这一年的分红」，所以：
    ratio_y = 现金流_y ÷ 分红_y           ← 先逐年配对
    Cover3y = median(ratio_2023, ratio_2024, ratio_2025)   ← 再取 3 年中位数
这就是 basis = "pairedMedian3y" 的含义。

⚠️⚠️ 五个实测踩过的坑，改这个脚本前先读完 ⚠️⚠️

坑 1  现金流量表/利润表是【累计季度值】：一季 ⊂ 半年 ⊂ 三季 ⊂ 全年。
      直接取 rows[0] 会拿到最新报告期（如 2026 中报）的半年累计数，
      却拿它去对一整年的分红 → 覆盖倍数腰斩。
      【唯一防线：只取 REPORT_DATE 以 12-31 结尾的行】
      实测神华 2025：一季 173.6 → 半年 515.5 → 三季 652.5 → 全年 750.6 亿。

坑 2  npLatest 必须取 RPT_DMSK_FN_INCOME.PARENT_NETPROFIT（归母净利）。
      ⚠️ 不能用 (OCF − 资本开支) 顶替 —— 那是自由现金流，不是净利。
      实测神华 2025：净利 528.5 亿，而 OCF−资本开支 = 750.6−484.0 = 266.6 亿。
      早期版本误把 266.6 写成 npLatest，被 App 端抓出。
      为什么致命：npLatest<0 是一票否决。建发股份 2025 净利 −108.1 亿（真亏损），
      但同年 FCF 是 +84.2 亿（正）—— 用 FCF 当净利会把它判成"正常"，
      正好漏掉最该拦下的那种。

坑 3  请求必须带 sortColumns=REPORT_DATE&sortTypes=-1，否则 rows[0] 是历史记录
      （曾因此拿到建发 1.85 亿股的历史股本）。

坑 4  RPT_DMSK_FN_INCOME 的字段里【不含】BASIC_EPS。
      早期版本 shares() 写的是 inc[y].get('BASIC_EPS')，在 DMSK 表上恒为 None，
      于是静默回退到"当前股本"—— 不报错、不告警。
      只有 RPT_F10_FINANCE_GINCOME 才有 BASIC_EPS（203 字段）。

坑 5  ⭐【分红总额】必须按分红表每行的 TOTAL_SHARES 逐行相乘求和，
      既不能"净利÷EPS 反推股本"，也不能"统一乘当前股本"。2026-09-17 实测：

      大秦铁路 601006（同样是 2023--2025 三年，同样先逐年配对再取中位数）：

          口径                          2023    2024    2025    fcfCover3y
          ─────────────────────────────────────────────────────────────
          A 每行 TOTAL_SHARES（✅正确）  181.42  201.47  198.63      0.88
          B 每股股息 × 当前股本        198.63  198.63  198.63      0.89  ← App 端
          C 每股股息 × 净利÷EPS       151.01  177.24  196.67      1.00  ← 我的旧版

      B 错在股本逐年增长被抹平；C 错在 BASIC_EPS 是【加权平均】股本且常被四舍五入
      （大秦 2025 的 EPS 原始值就是 0.3），两种偏差方向相反却都"看起来合理"。
      真相（分红表 TOTAL_SHARES 逐期）：
          2019--2021  148.67 亿股
          2022        151.67
          2023        181.42   ← 年内增发
          2024 年报   201.47
          2025 年报   198.63   ← 回购注销后回落
      股本三年动了 33%，用任何"单一股本"套三年都是错的。
      实测 40 只里 13 只的"当年股本 vs 当前股本"偏差 >5%，最极端 军信股份 48%。

银行专项块（bank）—— 2026-09-17 加，App 端契约
  fin=true 且 ORG_TYPE=银行 的记录多一个 "bank" 键，其余记录【没有】这个键。
  数据源：RPT_F10_FINANCE_MAINFINADATA（165 列，"主要指标"表，名字看不出和银行有关）。
  字段名是【拼音首字母】，不是英文：

      NONPERLOAN            不良贷款率            %
      BLDKBBL               拨备覆盖率            %   ← 锚点：招行 2024 末 411.98（=年报公开值）
      NET_INTEREST_MARGIN   净息差                %   ← 锚点：招行 2025 年报 1.87
      HXYJBCZL              核心一级资本充足率     %   ← 锚点：招行 2024 末 14.86（一致）
      ORG_TYPE              机构类型（银行/保险/证券）

  bank = {npl, provision, nim, cet1, provChg, nplChgBp, year, trend[4]}

  坑 6  【只取年报期】。季度期净息差只有 56.9% 非空，银行季度报告披露口径窄，
        拿季度值做趋势会大面积空。与坑 1 同一个防线：REPORT_DATE 以 12-31 结尾。
  坑 7  分流依据是 ORG_TYPE，不是 fin。中国平安 fin=true 但 ORG_TYPE=保险，
        拿不良率+息差量它是错的 → 【不给 bank 键】（App 端明确要求）。
  坑 8  别按英文字面猜字段，也别用「拨贷比 ÷ 不良率」反算拨备覆盖率 ——
        BLDKBBL 独立成立且已用公开值锚定，反算只会引入舍入误差。
  坑 9  ★ 0 是【值】，不是缺失。写 `x or default` 会在 x 合法为 0 时静默返回 default。
        本文件第 425-429 行的规则回归里也有这个写法，实测【当前规律无害】
        （`> 0` / `< -10` / `< 0` 这几个比较里 0 落到哪边都不改变结果），
        但 `verify_bank_block.py` 曾用 `(b['nplChgBp'] or -1) >= 0` 把
        不良率同比恰好为 0 的张家港行 002839 判成【不命中】，对外少报 1 只。
        → 新增规则时一律用 `num(x, d)` 显式判 None，别用 `or`。
        → 本数据的不良率是两位小数，bp 差恒为整数（实测全是 ±1/±2/±3/±5/±10），
          所以 0 是"两年持平"的真实含义，不存在四舍五入歧义。
          恰好为 0 的有 3 只：张家港行、南京银行、上海银行。

其他口径：
  · 3 年 = 近 3 个完整会计年度

  · 「中位数」的读法已锁死为 X = 逐年配对再取中位数（basis="pairedMedian3y"）。
    另一种读法 Y（先各取分子分母的中位数再相除）会把「A 年的高现金流」和
    「B 年的低分红」凑成一对，得出历史上从未发生过的覆盖倍数，已废弃。
  · 覆盖不足 2 个年度 → 不产出（宁缺勿错）
  · 分红总额只算【已实施】行（progress='实施分配' 且有除息日）；
    预案/董事会决议行同报告期会造成重复计数。

用法：
    python build_fundamentals.py            # 全量（读 ui_data.json 的入榜名单）
    python build_fundamentals.py 601088     # 指定代码（调试）
"""
import os
import sys
import json
import urllib.parse
from datetime import date
from statistics import median
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding='utf-8')
os.environ['NO_PROXY'] = '*'
for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
          'all_proxy', 'ALL_PROXY'):
    os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net  # noqa: E402
from config import OVERRIDE  # noqa: E402

BASE = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
INCOME = 'RPT_DMSK_FN_INCOME'                   # 仅用于落盘前交叉复核
CASHFLOW = 'RPT_DMSK_FN_CASHFLOW'
F10_INCOME = 'RPT_F10_FINANCE_GINCOME'          # 主力利润表：DMSK 表【没有】BASIC_EPS
DIV = 'RPT_SHAREBONUS_DET'                      # 分红明细：唯一权威的【当期股本】来源
MAINFIN = 'RPT_F10_FINANCE_MAINFINADATA'        # 银行专项（165 列，字段名是拼音首字母）

# bank 块的四项核心指标（顺序即 trend 数组的元素顺序，App 端按此解析）
BANK_F = ('NONPERLOAN', 'BLDKBBL', 'NET_INTEREST_MARGIN', 'HXYJBCZL')

# ⚠️ 坑 4（本次新发现）：RPT_DMSK_FN_INCOME 的字段里【不含】BASIC_EPS。
# 早期版本 shares() 写的是 inc[y].get('BASIC_EPS')，在 DMSK 表上恒为 None，
# 于是静默回退到"当前股本"—— 不报错、不告警，只是分红总额逐年都用同一个股本。
# 只有 RPT_F10_FINANCE_GINCOME 才有 BASIC_EPS（203 字段）。实测：
#   神华 528.49亿 ÷ 2.66  = 198.7 亿股 ✅（实际 198.69 亿股）
#   五粮液 89.54亿 ÷ 2.3068 = 38.8 亿股 ✅（实际 38.82 亿股）
# 所以：净利用 F10 表取（两表 PARENT_NETPROFIT 完全一致，已逐只核对），
#       DMSK 表降级为校验源。
YEAR = date.today().year

# 金融股判定依据【原始行业全称】，不依赖 ui_data.json 的 ind —— 那是 sn() 压缩后的
# 显示名，实测是"短名 + 全称"混合（神华=煤炭开采和洗选业（全称）、招行=银行（短名））。
# 显示层会随 SHORT 表变动，拿它做语义判断等于把业务规则绑死在显示层。
FIN_PAT = ('货币金融服务', '资本市场服务', '保险业', '其他金融业',
           '银行', '证券', '保险', '金融')


def raw_industry():
    """原始行业全称表（同 export_ui_data.py 的合并顺序，保证两边口径一致）"""
    d = json.load(open(os.path.join(HERE, 'cache', 'industry.json'), encoding='utf-8'))
    d.update(OVERRIDE)
    p = os.path.join(HERE, 'cache', 'extra_pool.json')
    if os.path.exists(p):
        for c, e in json.load(open(p, encoding='utf-8')).items():
            d[c] = e['industry']
    return d


def is_fin(raw):
    return any(p in raw for p in FIN_PAT)


def nm_of(code):
    """名称兜底：调试时手动传入的代码可能不在 ui_data 名单里（如 002142 宁波银行）"""
    return (CODE_MAP.get(code) or {}).get('name') or code


def pull(report, code, size=100):
    p = {'reportName': report, 'columns': 'ALL',
         'filter': f'(SECURITY_CODE="{code}")',
         'pageSize': str(size), 'pageNumber': '1',
         'sortColumns': 'REPORT_DATE', 'sortTypes': '-1',   # 坑 3
         'source': 'WEB', 'client': 'WEB'}
    j = net.get(BASE + '?' + urllib.parse.urlencode(p),
                encoding='utf-8', timeout=25).json()
    return (j.get('result') or {}).get('data') or []


def annual(rows):
    """只留年报（12-31）。坑 1 的唯一防线。"""
    return {int(str(r['REPORT_DATE'])[:4]): r for r in rows
            if str(r.get('REPORT_DATE', ''))[5:10] == '12-31'}


def cur_shares(codes):
    """当前总股本（亿股）。⚠️ 只用于自检对照，**不参与**覆盖倍数计算（见坑 5）"""
    out = {}
    for i in range(0, len(codes), 60):
        grp = codes[i:i + 60]
        q = ','.join(('sh' if c[0] in '65' else 'sz') + c for c in grp)
        try:
            r = net.get('https://qt.gtimg.cn/q=' + q, timeout=20, encoding='gbk')
        except Exception:
            continue
        for line in r.text.strip().split(';'):
            if '~' not in line:
                continue
            f = line.split('=')[1].strip('";\n\r ').split('~')
            try:
                if len(f) > 45 and f[2] and float(f[3]) > 0:
                    out[f[2]] = float(f[45]) / float(f[3])      # 亿股
            except (ValueError, IndexError):
                pass
    return out


def div_totals(code):
    """按【报告期年】归集的现金分红总额（元）—— 坑 5 的正确实现。

    关键：每一行自带 TOTAL_SHARES（**当期**股本，不是当前股本），逐行相乘再求和。
    同一报告期可能有多行（年报 + 中报），各自股本不同，必须分行算。

    只收【已实施】行：未实施的预案行同报告期会重复计数。
    """
    out = {}
    for r in pull(DIV, code, 200):
        rp = str(r.get('REPORT_DATE') or '')
        if r.get('ASSIGN_PROGRESS') != '实施分配' or not r.get('EX_DIVIDEND_DATE'):
            continue
        per = r.get('PRETAX_BONUS_RMB') or 0
        sh = r.get('TOTAL_SHARES')
        if not rp or not per or not sh:
            continue
        out[rp[:4]] = out.get(rp[:4], 0.0) + float(per) / 10.0 * float(sh)
    return out


def mk_suffix(code):
    if code[0] == '6':
        return 'SH'
    return 'SZ' if code[0] in '03' else 'BJ'


def pull_mainfin(code, size=40):
    """银行专项表。注意过滤字段是 SECUCODE（带交易所后缀），不是 SECURITY_CODE。"""
    p = {'reportName': MAINFIN, 'columns': 'ALL',
         'filter': f'(SECUCODE="{code}.{mk_suffix(code)}")',
         'pageSize': str(size), 'pageNumber': '1',
         'sortColumns': 'REPORT_DATE', 'sortTypes': '-1',   # 坑 3
         'source': 'WEB', 'client': 'WEB'}
    j = net.get(BASE + '?' + urllib.parse.urlencode(p),
                encoding='utf-8', timeout=25).json()
    return (j.get('result') or {}).get('data') or []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def bank_block(code, rows=None):
    """银行专项块。非银行（保险/证券）或数据不足 → None（宁缺勿错，不给键）
    rows 可由调用方传入（它已经为 trend 拉过一次 ，别再拉第二遍）。"""
    rows = pull_mainfin(code) if rows is None else rows
    if not rows:
        return None
    if rows[0].get('ORG_TYPE') != '银行':        # 坑 7：中国平安是保险，不给 bank
        return None
    ann = [r for r in rows                       # 坑 6：只取年报期
           if str(r.get('REPORT_DATE', ''))[5:10] == '12-31']
    if not ann:
        return None
    cur = ann[0]
    npl, prov, nim, cet1 = (_f(cur.get(k)) for k in BANK_F)
    if npl is None or prov is None:              # 核心两项必须有，否则整块不给
        return None

    prov_chg = npl_bp = None
    if len(ann) > 1:
        p_prev, n_prev = _f(ann[1].get('BLDKBBL')), _f(ann[1].get('NONPERLOAN'))
        if p_prev:
            prov_chg = round((prov - p_prev) / p_prev * 100, 1)   # 同比变化 %
        if n_prev is not None:
            npl_bp = int(round((npl - n_prev) * 100))             # 同比变化（基点）

    trend = [[int(str(r['REPORT_DATE'])[:4])]
             + [_f(r.get(k)) for k in BANK_F]
             for r in ann[:4]][::-1]              # 升序：老 → 新

    return {'npl': npl, 'provision': prov, 'nim': nim, 'cet1': cet1,
            'provChg': prov_chg, 'nplChgBp': npl_bp,
            'year': int(str(cur['REPORT_DATE'])[:4]), 'trend': trend}


TREND_YEARS = 5      # 详情页「财务 5 年」的窗口（与"近5年股息率分位"同宽）


def _rd(v, scale=1e8, nd=1):
    return round(float(v) / scale, nd) if v is not None else None


def trend_block(y0, inc, cas, mf, total_of):
    """详情页「财务 5 年」的逐年序列（2026-09-21 加）。

    ⚠️ 这是【展示层】数据，不参与任何阈值判断 —— 覆盖倍数那套仍走 pairedMedian3y。
    全部来自 build() 已经拉过的三张表（F10 利润表 / DMSK 现金流量表 / MAINFIN 主要指标），
    只是以前只留中位数、把序列丢了。
    分红总额用 total_of()（= 分红表逐行 TOTAL_SHARES 口径，坑 5 的正确实现），
    与覆盖倍数完全同源，界面上两处不会打架。
    """
    ys = list(range(y0 - TREND_YEARS + 1, y0 + 1))
    out = {'years': ys}
    for key, src, f in (('rev', inc, 'TOTAL_OPERATE_INCOME'),
                        ('np', inc, 'PARENT_NETPROFIT')):
        out[key] = [_rd((src.get(y) or {}).get(f)) for y in ys]
    for key, f in (('ocf', 'NETCASH_OPERATE'), ('capex', 'CONSTRUCT_LONG_ASSET')):
        out[key] = [_rd((cas.get(y) or {}).get(f)) for y in ys]
    out['fcf'] = [None if (o is None or c is None) else round(o - c, 1)
                  for o, c in zip(out['ocf'], out['capex'])]
    out['div'] = [round(total_of(y) / 1e8, 2) or None for y in ys]
    out['cov'] = [None if (x is None or not d) else round(x / d, 2)
                  for x, d in zip(out['fcf'], out['div'])]
    # 派息率 = 分红 ÷ 归母净利。净利 ≤ 0 时不给（负派息率没有意义，同 payout 的处理）
    out['pay'] = [None if (n is None or n <= 0 or not d) else round(d / n * 100, 1)
                  for n, d in zip(out['np'], out['div'])]
    for key, f in (('roe', 'ROEJQ'), ('gpm', 'XSMLL'), ('lev', 'ZCFZL')):
        out[key] = [_rd((mf.get(y) or {}).get(f), 1, 1) for y in ys]
    return out


def build(code, tot, by_rp, sh_fallback, raw='', mf_rows=None):
    """返回 8 字段 dict（含 trend），或 None（数据不足 2 个年度 → 宁缺勿错）"""
    inc = annual(pull(F10_INCOME, code))          # 坑 4：只有这张表有 BASIC_EPS
    cas = annual(pull(CASHFLOW, code))
    if not inc:
        return None
    y0 = max((y for y in inc if y <= YEAR - 1), default=None)
    if y0 is None:
        return None
    # 主要指标表（ROE/毛利率/资产负债率/每股净资产）。以前只给银行拉，
    # 现在全池都要（详情页「财务 5 年」），由调用方拉一次传进来 —— 银行专项也用它。
    mf_all = mf_rows if mf_rows is not None else pull_mainfin(code)
    mf = {int(str(r['REPORT_DATE'])[:4]): r for r in mf_all
          if str(r.get('REPORT_DATE', ''))[5:10] == '12-31'}

    np_latest = inc[y0].get('PARENT_NETPROFIT')            # 坑 2：只认这个字段

    def total_of(y):
        """该报告期的现金分红总额（元）。坑 5：优先用分红表自带的当期股本。"""
        v = tot.get(str(y)) or 0
        if v > 0:
            return v
        # 兜底（老股分红表缺 TOTAL_SHARES 时）：每股派息 × 当前股本
        dps, sh = by_rp.get(str(y)) or 0, sh_fallback
        return dps * sh * 1e8 if dps and sh else 0

    cov_o, cov_f = [], []
    latest_f = None
    for y in range(y0 - 2, y0 + 1):
        c = cas.get(y) or {}
        ocf = c.get('NETCASH_OPERATE')
        total = total_of(y)
        if ocf is None or total <= 0:
            continue
        cap = c.get('CONSTRUCT_LONG_ASSET') or 0
        cov_o.append(float(ocf) / total)
        v = (float(ocf) - float(cap)) / total
        cov_f.append(v)
        if y == y0:
            latest_f = v

    if len(cov_o) < 2 or len(cov_f) < 2:
        return None

    return {
        'ocfCover3y': round(median(cov_o), 2),
        'fcfCover3y': round(median(cov_f), 2),
        'fcfCoverLatest': round(latest_f, 2) if latest_f is not None else None,
        'npLatest': round(float(np_latest) / 1e8, 1) if np_latest is not None else None,
        'year': y0,
        'basis': 'pairedMedian3y',   # App 端要求改名：明示"逐年配对后再取中位数"
        # 每股净资产（最新年报）：界面上配「市净率」用。
        # ⚠️ 别用「现价÷PB」反算 —— 腾讯的 PB 只有两位小数，反出来 20.96 而真值 21.30，
        #    界面会出现自相矛盾的两个数。
        'bps': _rd((mf.get(y0) or {}).get('BPS'), 1, 2),
        'trend': trend_block(y0, inc, cas, mf, total_of),
        # 金融股标记（App 端要求，2026-09-17 加，字段集 6 → 7）。
        # 银行/保险的 OCF 是吸储放贷现金流，与工商企业不可比，界面应显示"不适用"。
        # 数值照算不剔除 —— 留着便于审计，"是否显示"是界面的决定。
        'fin': is_fin(raw),
    }


# ---------------- 落盘前自检 ----------------
def cross_check(samples, out, y2, sh_now):
    """两条独立验证（只跑样本）：
       A. 分红总额三口径对照：证明"用哪个股本"这一步到底能差多少（坑 5）
       B. F10 利润表 vs DMSK 利润表 复核年报归母净利 —— 抓字段误用
    """
    print('\n【自检 A】分红总额三口径对照（亿元）　勘误见坑 5')
    print('-' * 88)
    for c in samples:
        v = out.get(c)
        if not v:
            continue
        y0 = v['year']
        tot = div_totals(c)
        by_rp = (y2.get(c) or {}).get('by_rp') or {}
        rows = annual(pull(F10_INCOME, c, 100))
        line = []
        for y in range(y0 - 2, y0 + 1):
            a = (tot.get(str(y)) or 0) / 1e8
            dps = by_rp.get(str(y)) or 0
            b = dps * (sh_now.get(c) or 0)                      # 当前股本
            r = rows.get(y) or {}
            try:
                eps, npv = float(r['BASIC_EPS']), float(r['PARENT_NETPROFIT'])
                shy = npv / eps / 1e8 if eps > 0 and npv > 0 else 0
            except (TypeError, ValueError, KeyError):
                shy = 0
            line.append(f'{y}({a:.1f}/{b:.1f}/{dps*shy:.1f})')
        print(f"   {c} {nm_of(c):<8} " + '  '.join(line))
    print('   格式  年( A✅当期股本 / B当前股本 / C净利÷EPS )')

    print('\n【自检 B】F10 利润表复核年报归母净利（另一张报表，抓字段误用）')
    print('-' * 74)
    for c in samples:
        v = out.get(c)
        if not v:
            continue
        rows = annual(pull(INCOME, c, 100))            # DMSK 表当校验源
        r = rows.get(v['year']) or {}
        dk = r.get('PARENT_NETPROFIT')
        dky = f'{float(dk)/1e8:,.1f}' if dk is not None else '—'
        ok = '✅' if dk is not None and abs(float(dk) / 1e8 - v['npLatest']) < 0.2 else '⚠'
        print(f"   {ok} {c} {nm_of(c):<8} {v['year']}  "
              f"F10 {v['npLatest']:>9.1f}亿   DMSK {dky:>10}亿")


def bank_report(out):
    """银行专项块落盘前复核：派生字段手算验证 + 四条候选规则的真实命中率"""
    banks = {c: v['bank'] for c, v in out.items() if 'bank' in v}
    if not banks:
        print('\n⚠ 没有任何记录带 bank 块，跳过银行复核')
        return
    fink = sorted(c for c, v in out.items() if v['fin'])
    print(f'\n【银行块】带 bank {len(banks)} 只 / fin=true {len(fink)} 只'
          f'　差集（金融但无 bank，应为保险）：'
          + '、'.join(f'{c} {nm_of(c)}' for c in fink if c not in banks))
    print(f'  ORG_TYPE 分流：银行 {len(banks)} 只；'
          f'trend 长度分布 {sorted({len(b["trend"]) for b in banks.values()})}')

    print('\n  ① 派生字段人工复核（最新年报 vs 上一年报）')
    print('  ' + '-' * 80)
    for c in sorted(banks)[:5]:
        b, t = banks[c], banks[c]['trend']
        if len(t) < 2:
            continue
        y0, n0, p0 = t[-2][0], t[-2][1], t[-2][2]
        y1, n1, p1 = t[-1][0], t[-1][1], t[-1][2]
        chk = (p1 - p0) / p0 * 100
        bp = round((n1 - n0) * 100)
        print(f'  {c} {nm_of(c):<8}'
              f'拨备 {p0}({y0})→{p1}({y1})  provChg 存 {b["provChg"]:>6} / 算 {chk:>6.1f}'
              f'　不良 {n0}({y0})→{n1}({y1})  nplChgBp 存 {b["nplChgBp"]:>3} / 算 {bp:>3}')

    print('\n  ② 最新年报全量（bank 原始值，供 App 端逐只对齐）')
    print('  ' + '-' * 80)
    print(f'  {"代码":<8}{"名称":<10}{"年":<6}{"不良率":>8}{"拨备覆盖":>10}'
          f'{"净息差":>8}{"核心一级":>9}{"拨备同比":>9}{"不良bp":>7}')
    for c in sorted(banks):
        b = banks[c]
        f1 = lambda x, w=8: (f'{x:>{w}.2f}' if isinstance(x, (int, float)) else f'{"—":>{w}}')
        print(f'  {c:<8}{nm_of(c):<10}{b["year"]:<6}'
              f'{f1(b["npl"])}{f1(b["provision"], 10)}{f1(b["nim"])}{f1(b["cet1"], 9)}'
              f'{f1(b["provChg"], 9)}{("—" if b["nplChgBp"] is None else str(b["nplChgBp"])):>7}')

    def rel_npl(b):
        t = b['trend']
        if len(t) < 2 or not t[-2][1] or t[-1][1] is None:
            return None
        return (t[-1][1] - t[-2][1]) / t[-2][1] * 100

    def hit(f):
        return sorted(c for c, b in banks.items() if f(b))

    r_cet = hit(lambda b: b['cet1'] is not None and b['cet1'] < 8.5)
    r_dead = hit(lambda b: b['provision'] < 150)
    r_lvl2 = hit(lambda b: b['provision'] < 200)
    r_combo = hit(lambda b: b['provision'] < 250 and (b['provChg'] or 0) < -10)
    r_dual_raw = hit(lambda b: (b['provChg'] or 0) < 0 and (b['nplChgBp'] or 0) > 0)
    r_dual_gated = hit(lambda b: (b['provChg'] or 0) < 0 and (b['nplChgBp'] or 0) > 0
                       and ((b['nplChgBp'] > 5) or ((rel_npl(b) or -9) > 5)))
    r_trend = hit(lambda b: (b['provChg'] or 0) < -8)

    def down3(b):
        t = b['trend']
        v = [x[3] for x in t]
        return len(v) == 4 and all(x is not None for x in v) and all(
            v[i] > v[i + 1] for i in range(3))

    r_nim = hit(down3)
    nm = lambda lst: '、'.join(nm_of(c) for c in lst) or '—'
    print('\n  ③ 规则回归（分母 = 带 bank 的 %d 只）' % len(banks))
    print('  ' + '-' * 80)
    for lab, lst in [
            ('红 拨备<150%（监管红线）', r_dead),
            ('黄 拨备<200%（App端⑤）', r_lvl2),
            ('黄 拨备<250% 且 同比<-10%（App端⑥）', r_combo),
            ('黄 双杀 原始定义（不良任一上行）', r_dual_raw),
            ('黄 双杀 加门槛（>5bp 或 >5%）', r_dual_gated),
            ('黄 拨备同比<-8%（App端④）', r_trend),
            ('黄 核心一级<8.5%', r_cet),
            ('提示 净息差连降3年', r_nim)]:
        print(f'  {lab:<38}{len(lst):>3} / {len(banks)}　{nm(lst)}')

    print('\n  ④ 双杀逐只幅度（判断门槛该定在哪）')
    print('  ' + '-' * 80)
    for c, b in sorted(banks.items()):
        bp, rl = b['nplChgBp'], rel_npl(b)
        if bp is None or bp <= 0:
            continue
        print(f'  {c} {nm_of(c):<8} 不良 {bp:>3}bp / 相对 '
              f'{rl:>5.2f}%　拨备同比 {b["provChg"]:>6}%　'
              f'{"✅过门槛" if (bp > 5 or (rl or 0) > 5) else "✗ 门槛下不算上行"}')


def main():
    global CODE_MAP, RAW
    CODE_MAP, RAW = {}, {}
    ui = json.load(open(os.path.join(HERE, 'ui_data.json'), encoding='utf-8'))
    y2 = json.load(open(os.path.join(HERE, 'cache', 'yield2.json'), encoding='utf-8'))
    # 同上：覆盖全部导出标的，「自加入」里的标的也要有基本面
    CODE_MAP = {m['code']: m for m in ui['qual'] + ui['broad'] + ui.get('selfAdd', []) + ui.get('poolRest', [])}
    codes = sys.argv[1:] or sorted(CODE_MAP)
    print(f'入榜标的 {len(codes)} 只（qual {len(ui["qual"])} + broad {len(ui["broad"])} 去重）')

    sh_now = cur_shares(codes)
    print(f'当前股本兜底覆盖 {len(sh_now)} 只')

    RAW = raw_industry()
    fin_raw = sorted({RAW.get(c, '未分类') for c in codes if is_fin(RAW.get(c, ''))})
    print(f'金融股原始行业名 {len(fin_raw)} 种：{fin_raw}')

    def job(c):
        by_rp = (y2.get(c) or {}).get('by_rp') or {}
        sh, fallback, tot = sh_now.get(c), 0, {}
        try:
            # ⚠️ div_totals 原来在 try 之外 —— 一次 DNS 抖动就让整个线程把异常抛出去，
            #    ex.map 再把它抛给主线程，**整轮构建直接死掉、缓存一个字不写**
            #    （2026-09-21 实测：1600 个请求跑到一半全白跑）。
            tot = div_totals(c)
            if not tot:
                fallback = 3                # 完全没有分红表数据 → 全程走兜底
            # MAINFIN 每只【只拉一次】：trend（ROE/毛利率/负债率/每股净资产）和
            # 银行专项块都要它。以前只有银行拉，现在全池拉 —— 每只 +1 个请求。
            mf = []                         # 主要指标拿不到不影响覆盖倍数，trend 里留空
            try:
                mf = pull_mainfin(c)
            except Exception:
                pass
            r = build(c, tot, by_rp, sh, RAW.get(c, ''), mf_rows=mf)
            # 银行专项块：只给 fin=true 的记录挂（名单里 24 只）
            if r and r.get('fin'):
                bb = bank_block(c, mf)
                if bb:
                    r['bank'] = bb
            return c, (r, tot, fallback)
        except Exception as e:
            return c, (f'{type(e).__name__}: {str(e)[:40]}', tot, fallback)

    out, bad, fb = {}, [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, (r, tot, fallback) in ex.map(job, codes):
            if isinstance(r, str):
                bad.append((c, r))
            elif r:
                out[c] = r
                need = {str(y) for y in range(r['year'] - 2, r['year'] + 1)}
                if fallback or need - set(tot):
                    fb.append(c)

    miss = [c for c in codes if c not in out]
    print(f'✅ 产出 {len(out)} 只，失败 {len(bad)} 只，无数据 {len(miss)} 只')
    for c, e in bad[:10]:
        print(f'   ✗ {c} {e}')
    if miss[:12]:
        print('   无数据：' + '、'.join(f'{c} {nm_of(c)}' for c in miss[:12]))

    yrs = sorted({v['year'] for v in out.values()})
    print(f'year 取值：{yrs}（契约要求一致）')
    base = sorted({k for v in out.values() for k in v} - {'bank'})
    nb = [c for c in out if 'bank' in out[c]]
    print(f'基础字段集：{base}（{len(base)} 个，所有记录都有）')
    print(f'bank 块：{len(nb)} 只  ⚠ 字段集不再统一 —— 银行 8 键、其余 7 键，'
          f'App 端的"字段集一致"校验要把 bank 当可选键')
    print(f'分红总额走【每行 TOTAL_SHARES】：{len(out) - len(fb)} 只；'
          f'需兜底（缺某年数据）：{len(fb)} 只 {fb[:8]}')

    # ---- 阈值回归：确认规则在真实名单上的命中率不是噪音 ----
    fin = [c for c in out if out[c]['fin']]     # 用服务端自己的 fin，不再靠显示名猜
    rest = {c: v for c, v in out.items() if c not in fin}
    loss = [c for c, v in rest.items() if (v['npLatest'] or 0) < 0]
    neg3 = [c for c, v in rest.items() if c not in loss and v['fcfCover3y'] < 0]
    warn = [c for c, v in rest.items() if c not in loss and c not in neg3
            and v['fcfCover3y'] >= 0 and (v['fcfCoverLatest'] or 0) < 0]
    yellow = [c for c, v in rest.items() if c not in loss + neg3 + warn and v['fcfCover3y'] < 1]
    ok = [c for c in rest if c not in loss + neg3 + warn + yellow]

    print('\n阈值回归（非金融股，界面分流用）')
    print('-' * 74)
    print(f'  金融股（界面显示 —）      {len(fin):>4} 只')
    print(f'  红 npLatest<0 亏损        {len(loss):>4} 只  ' +
          '、'.join(nm_of(c) for c in loss[:6]))
    print(f'  红 fcfCover3y<0           {len(neg3):>4} 只  ' +
          '、'.join(nm_of(c) for c in neg3[:6]))
    print(f'  黄 3y≥0 但最新年<0        {len(warn):>4} 只  ' +
          '、'.join(nm_of(c) for c in warn[:6]))
    print(f'  黄 0≤fcfCover3y<1         {len(yellow):>4} 只')
    print(f'  正常 fcfCover3y≥1         {len(ok):>4} 只')

    bank_report(out)

    if sys.argv[1:]:
        dst = None
    else:
        dst = os.path.join(HERE, 'cache', 'fundamentals.json')
        json.dump(out, open(dst, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'\n✅ → {dst}')

    cross_check(['601088', '600036', '600153', '000895', '601006'], out, y2, sh_now)

    print('\n抽样（前 10 只）：')
    for c in sorted(out)[:10]:
        v = out[c]
        print(f"  {c} {nm_of(c):<8}{(CODE_MAP.get(c) or {}).get('ind', '')[:8]:<10}"
              f"ocf3y {v['ocfCover3y']:>6}  fcf3y {v['fcfCover3y']:>7}  "
              f"最新{v['year']} {str(v['fcfCoverLatest']):>7}  "
              f"净利 {str(v['npLatest']):>8}亿")


if __name__ == '__main__':
    main()
