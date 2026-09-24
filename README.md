# dividend-data

「红利雪球」App 的数据源（自用）。

**这个仓库只有管线脚本和公开行情数据**，不含个人持仓/账本 —— 那些只在手机本机。

- `.github/workflows/daily.yml` 每天北京时间 17:30 跑一遍管线
- 产出推到 `data` 分支（force push，永远只有一个 commit，仓库不会一天涨 3MB）

手机 App「数据源」页填的地址：

```
https://cdn.jsdelivr.net/gh/zhuhecool4/dividend-data@data
```

App 会自动把 `raw.githubusercontent.com` 当备选；两个都不通时用内置快照。

## 手上要跑点什么吗

不用。首次推上来之后每天 17:30 自己跑。想立刻要一份新的：
Actions 页 →「每日刷新数据」→ Run workflow。

## ⚠️ 推完记得 purge 一次 jsDelivr

push（或云端跑完）之后，**jsDelivr 可能还在发旧的那份**，而 App 带 `If-None-Match`
→ 直接 304 → 手机**静静继续用旧数据**、界面上什么都不报。实测过一次：
push 完 jsDelivr 仍是 2,565,591 字节的旧文件，手机拉取得到 304。

解法（公开接口，一次 GET 就行）：

```
https://purge.jsdelivr.net/gh/zhuhecool4/dividend-data@data/ui_data.json
```

返回 `status: finished`（CF / FY 两个 provider 都清）→ 再拉就是新的了。

⚠️ **这里的脚本和 `config.py` 是"推上来那一天"的快照。** 电脑上改了口径
（`config.py` 的自加入/剔除/行业修正）或改了管线脚本之后，云端不会自己知道 ——
要在电脑上重跑一次 `_scratch/make_repo.py` 并再 push 一次，云端才用上新口径。

## 这里的文件

| 文件 | 说明 |
|---|---|
| `net.py` `config.py` | 公用（HTTP + 人工口径） |
| `rebuild_div.py` … `export_ui_data.py` | 管线，顺序见 workflow |
| `cache/` | 种子缓存（中证 xls、行业、ETF 池、名录）—— 云端抓不稳的那些 |
| `ui_data.json` | 种子：只给增量脚本一份"要算哪些标的"的清单，每次会被重写 |

增量缓存（`cache/div_detail2.json` 等）**不入库**：它们是"缺了才抓"的，
提交进来云端就再也不更新了。靠 Actions 的 cache 在两次运行之间传。
