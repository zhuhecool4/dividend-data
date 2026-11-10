# -*- coding: utf-8 -*-
"""本地落盘小工具（2026-10-03 加，架构师 review P1-4）。

**为什么要有它**：管线里 26 处缓存落盘原来全是 `json.dump(obj, open(path, 'w', ...))` ——
**非原子**。进程在写的中途死掉（CI 超时被 kill、手动关窗口、磁盘抖动）会留下【半截 JSON】，
而下游的读法两种都难查：
  · 直接 `json.load` → 当场抛异常，看着像"数据坏了"，可你手里没有任何旧数据；
  · `try: json.load ... except: {}` → **静默当空**，整块功能消失且不报错。
    （本工程"静默空"踩过好几次：日历增量、名录被空值顶掉、缓存被空值覆盖 —— 见 S10/S9。）
原子写：先写**同目录**的临时文件（同目录才能保证 os.replace 不跨卷、是原子改名），
写完 flush+fsync 再 `os.replace` 顶上去 —— 要么旧的完好，要么新的完整，不存在半截。

**换行约定（与旧写法的一处有意差异）**：旧的 `open(path,'w')` 走文本模式，
Windows 上把 `\\n` 翻成 `\\r\\n`、Linux/CI 上是 `\\n` —— 同一份缓存在两个平台字节不同。
本工具**统一写 `\\n`**（`newline=''`），与 CI 产物一致；JSON 语义不变（json.load 两种都吃）。
"""
import io, json, os, tempfile


def atomic_write_text(path, text):
    """文本原子写：同目录临时文件 → fsync → os.replace。返回写出的字节数。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', suffix='.part', dir=d)
    try:
        with io.open(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return len(text.encode('utf-8'))


def dump_json(path, obj, *, indent=None, ensure_ascii=True, empty_ok=True):
    """原子写 JSON。返回 True = 写了；False = 被"空结果"闸拦下（没动旧文件）。

    ⚠️ 参数默认值与 `json.dump` **保持一致**（`ensure_ascii=True`）—— 本工程 23 个调用点里
       有一处没显式传它（by_sector.py 的 ind_log），默认要是悄悄换成 False 就会改那份文件的
       输出（语义一样，但字节变了）。"换工具不改输出"这条有测试钉着。
    ⚠️ `empty_ok=False` 是给"抓到 0 条也不算成功"那种脚本用的（S10.35：ETF 表现
       抓 0 条照样写缓存 → 界面整块变空且不报错）。空 = `{}` / `[]` / `None`。
    """
    if not empty_ok and not obj:
        return False
    atomic_write_text(path, json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent))
    return True


def dump_text(path, text, empty_ok=True):
    """文本原子写（报告类产物用，比如 by_sector 的 .md、scan_market 的 .html）。"""
    if not empty_ok and not text:
        return False
    atomic_write_text(path, text)
    return True
