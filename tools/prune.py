#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prune_core.py —— Steam 图床索引的失效链接探测与剔除（唯一实现）

本地 `steambed.py prune/sync` 与 GitHub Action 都调用这一份代码，
避免"本地判活、云端判死"这类两套逻辑不一致的问题。

判定规则（重要）：
  只有 404 / 410 才算"文件真的没了"，而且必须**复核确认**：
  首次判死后隔一会儿再探两轮，任何一轮活过来就撤销；主域名连续 404 时还会去
  旧域名（steamuserimages-a.akamaihd.net）试一次，能出图同样撤销。
  429 / 5xx / 403 / 超时 / DNS 失败，一律记为 unknown 并**保留**条目 ——
  一次限流或网络抖动就把整个索引清空，是这套工具最容易踩的坑。

三道安全阀：
  1. 全部失效 -> 拒绝剔除
  2. 失效比例 > --max-ratio（默认 50%）且总数 >= --min-total（默认 10）-> 拒绝剔除
  3. 一条活的都没有、却还有一堆探测失败 -> 拒绝剔除（典型的地区封锁/网络故障）
     （确认无误时加 --force 跳过）

删图记录：
  每次剔除都会把条目追加进 deleted_history.json（只增不删、按时间倒序、按路径去重），
  所以"删了哪些、什么时候删的、当时报什么错"永远查得到，即使 r.json 早已清空。
  本地与 Action 写的是同一份结构，export 会把两份合并成站点里的那一份。
  误删了可以用 `steambed.py restore` 把它放回清单（会先复核，确实活着才放回）。

退出码：0 成功 / 1 出错 / 2 被安全阀拦下
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_HOST = "images.steamusercontent.com"
LEGACY_HOST = "steamuserimages-a.akamaihd.net"  # 老图可能存在这个域名上
HISTORY_NAME = "deleted_history.json"
BJ_TZ = timezone(timedelta(hours=8))  # 删图时间统一按北京时间记录
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"

# 有些图在 Range 请求下返回 application/octet-stream，
# 单看 Content-Type 会误判，所以再看文件头。
_MAGIC = (
    b"\xff\xd8\xff",      # jpeg
    b"\x89PNG\r\n\x1a\n",  # png
    b"GIF8",              # gif
    b"BM",                # bmp
    b"RIFF",              # webp
)


def looks_like_image(body: bytes) -> bool:
    return any(body.startswith(m) for m in _MAGIC)


def atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换。写到一半断电/被杀进程，也不会留下半截 JSON。

    Action 里 `git add` 读到半截 r.json 会直接毁掉线上索引，这个坑必须堵。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_busted(url: str) -> str:
    """挂随机查询串强制 CDN 回源。

    CDN 返回 Cache-Control: max-age=604800（7 天）。删图后边缘节点仍会继续
    返回 200，不加这一下会把死链判成活的。
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}prune={int(time.time() * 1000)}{random.randrange(1000, 9999)}"


def probe_image(url: str, timeout: float = 15.0, retries: int = 2,
                retry_server_errors: bool = True) -> tuple[str, str]:
    """探测单个图片地址，返回 (状态, 说明)。只取前 4KB，不下载整张图。

    注意：这里**不会**因为重试而放宽判定 —— 404 第一次就返回 DEAD，
    真正防误判的是上层的 confirm_dead() 复核。
    """
    headers = dict(UA)
    headers["Range"] = "bytes=0-4095"
    last = ""
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(cache_busted(url), headers=headers), timeout=timeout) as r:
                body = r.read(4096)
                ctype = (r.headers.get("Content-Type") or "").lower()
                if ctype.startswith("image/"):
                    if len(body) < 64:
                        return UNKNOWN, f"HTTP {r.status} 但响应体过短"
                    return ALIVE, f"HTTP {r.status} {ctype}"
                if looks_like_image(body):
                    return ALIVE, f"HTTP {r.status} {ctype or 'octet-stream'}（按文件头判定）"
                return UNKNOWN, f"HTTP {r.status} 但类型是 {ctype or '未知'}"
        except HTTPError as e:
            if e.code in (404, 410):
                return DEAD, f"HTTP {e.code}"
            # 429 限流、5xx 服务端抖动：值得再试一次，仍失败才记未知
            if retry_server_errors and e.code in (429, 500, 502, 503, 504) and attempt < retries:
                last = f"HTTP {e.code}"
                time.sleep(1.0 * (attempt + 1) + random.uniform(0, 0.5))
                continue
            # 403 可能是地区封锁 —— 同样不能当删除
            return UNKNOWN, f"HTTP {e.code}"
        except Exception as e:  # 超时、DNS、连接重置
            last = f"{type(e).__name__}: {str(e)[:60]}"
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    return UNKNOWN, last or "探测失败"


def now_iso() -> str:
    """当前时间，带 +08:00 偏移，跟人看到的北京时间一致。"""
    return datetime.now(BJ_TZ).isoformat(timespec="seconds")


def load_history(path: Path) -> dict:
    """读删图记录；文件不存在或损坏时返回空结构，绝不因此中断清理。"""
    empty = {"version": 1, "items": []}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return empty
    return data


def render_history_md(data: dict) -> str:
    """把删图记录渲染成一张可读的表格，供人直接翻。"""
    items = [i for i in data.get("items") or [] if isinstance(i, dict)]
    lines = [
        "# 删图记录",
        "",
        "Steam 图床上被判定为已删除（HTTP 404 / 410）的图片。**只增不删**——",
        "即使 `r.json` 已经清空、图早就没了，这份历史也还在。",
        "",
        f"- 累计：**{len(items)}** 条",
        f"- 最近更新：{items[0].get('removed_at', '') if items else '—'}",
        "- 机器可读版：`deleted_history.json`（由每 7 天的清理任务自动维护）",
        "",
    ]
    if not items:
        lines += ["暂时没有删图记录。", ""]
        return "\n".join(lines)

    src_label = {"action": "云端定时", "local": "本机 sync", "backfill": "历史补录"}
    lines += [
        "| # | 删除时间（北京时间） | 文件名 | 直链路径 | 判定 | 来源 |",
        "|---|---|---|---|---|---|",
    ]
    for n, it in enumerate(items, 1):
        name = str(it.get("name") or "—").replace("|", "\\|")
        path = str(it.get("path") or "").replace("|", "\\|")
        when = str(it.get("removed_at") or "").replace("T", " ")[:19]
        src = src_label.get(str(it.get("source")), str(it.get("source") or ""))
        lines.append(f"| {n} | {when} | {name} | `{path}` | {it.get('reason', '')} | {src} |")
    lines += [
        "",
        "> 完整地址 = `https://images.steamusercontent.com/ugc/` + 直链路径",
        "",
    ]
    return "\n".join(lines)


def save_history(path: Path, data: dict, *, write_md: bool = True) -> bool:
    """按路径去重、按时间倒序后写回。返回 JSON 内容是否真的变了。

    JSON 旁边会同步生成同名 `.md` 表格，本地和 Action 走同一段代码，
    不会出现「云端删了图、表格还是旧的」这种情况。
    """
    seen: set[str] = set()
    items: list[dict] = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        p = str(it.get("path") or "")
        if not p or p in seen:
            continue
        seen.add(p)
        items.append(it)
    items.sort(key=lambda x: str(x.get("removed_at") or ""), reverse=True)

    out = {
        "version": 1,
        "count": len(items),
        "note": ("Steam 图床的删图记录。每次清理判定为已删除（HTTP 404/410）的图片都会记一条，"
                 "按时间倒序、按路径去重，只增不删；清空 r.json 也不会清掉这里。"),
        "items": items,
    }
    text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not items and not old:
        # 从来没删过图，就别造一个空文件出来
        return False

    changed = old != text
    if changed:
        atomic_write(path, text)

    if write_md:
        md_path = path.with_suffix(".md")
        md = render_history_md(out)
        old_md = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
        if md != old_md:
            atomic_write(md_path, md)

    return changed


def append_history(history: Path, records: list[dict]) -> tuple[int, int]:
    """把本次删除的条目追加进记录，返回 (本次新增, 累计条数)。已有路径不重复记。"""
    data = load_history(history)
    known = {str(it.get("path") or "") for it in data.get("items") or []}
    added = 0
    for rec in records:
        if rec.get("path") in known:
            continue
        data.setdefault("items", []).append(rec)
        known.add(rec.get("path"))
        added += 1
    save_history(history, data)
    return added, len(data.get("items") or [])


def merge_history(dst: Path, *extra: Path) -> int:
    """把多份记录合并进 dst（export 用：本地删的也要出现在站点记录里）。

    同一路径保留最早的那条 removed_at —— 第一次被判定删除的时间才是真的。
    """
    by_path: dict[str, dict] = {}
    for src in (dst, *extra):
        for it in load_history(src).get("items") or []:
            if not isinstance(it, dict):
                continue
            p = str(it.get("path") or "")
            if not p:
                continue
            old = by_path.get(p)
            if old is None or str(it.get("removed_at") or "") < str(old.get("removed_at") or ""):
                by_path[p] = it
    save_history(dst, {"version": 1, "items": list(by_path.values())})
    return len(by_path)


def forget_history(history: Path, path: str) -> bool:
    """从删图记录里移除一条（restore 用）。返回是否真的删掉了。"""
    data = load_history(history)
    before = data.get("items") or []
    kept = [it for it in before if str(it.get("path") or "") != path]
    if len(kept) == len(before):
        return False
    data["items"] = kept
    save_history(history, data)
    return True


def _record(path: str, detail: str, host: str, *, source: str,
            extra: dict | None = None) -> dict:
    rec = {
        "path": path,
        "url": f"https://{host.rstrip('/')}/ugc/{path}",
        "removed_at": now_iso(),
        "reason": detail,
        "source": source or "local",
    }
    for k in ("name", "local", "appid"):
        v = (extra or {}).get(k)
        if v:
            rec[k] = v
    return rec


def classify(pics: list[str], host: str = DEFAULT_HOST, *, delay: float = 0.15,
             threads: int = 4, quiet: bool = False) -> dict[str, tuple[str, str]]:
    """并发探测一批 UGC 路径，返回 {path: (状态, 说明)}。"""
    host = host.rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    total = len(pics)
    done = 0
    result: dict[str, tuple[str, str]] = {}

    def work(path: str) -> tuple[str, str, str]:
        # 打散起始时间，避免同一瞬间打出 N 个请求
        time.sleep(random.uniform(0, max(delay, 0)))
        state, detail = probe_image(f"{host}/ugc/{path}")
        return path, state, detail

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        for path, state, detail in pool.map(work, pics):
            done += 1
            result[path] = (state, detail)
            if not quiet:
                tag = {ALIVE: "存活", DEAD: "失效", UNKNOWN: "未知"}[state]
                mark = {ALIVE: "  ", DEAD: "! ", UNKNOWN: "? "}[state]
                label = path[:44].ljust(44)
                print(f"  {mark}[{done:>4}/{total}] {label} {tag}  {detail}")
    return result


def ugc_path(url: str) -> str | None:
    """从任意 Steam 图片地址里取出 UGC 路径片段。

    先砍掉 ?imw=512 这类查询串，否则路径里会混进 "?imw=512"，
    拼出来的探测地址和写进 r.json 的路径都是坏的。
    """
    if not url:
        return None
    clean = str(url).strip().split("#", 1)[0].split("?", 1)[0]
    m = re.search(r"/ugc/(.+?)/?$", clean)
    if not m:
        return None
    p = m.group(1).strip("/")
    return (p + "/") if p else None


# 旧名字，兼容已有调用
_ugc_path = ugc_path


def confirm_dead(paths: list[str], host: str = DEFAULT_HOST, *, rounds: int = 2,
                 delay: float = 1.2, cross_host: bool = True, threads: int = 4,
                 quiet: bool = False) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """复核首次判死的条目 —— 这是防"图没删却被当成删了"的关键一步。

    每条都要隔一段时间再探 rounds 轮：
      · 任意一轮出图   -> 撤销（误判，图还在）
      · 任意一轮探测失败 -> 记为未知（保留，宁可留着也不删）
      · 全是 404        -> 再去旧域名试一次，能出图同样撤销
    只有全程 404 的才返回 confirmed。
    """
    base = host.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    legacy = "https://" + LEGACY_HOST
    confirmed: dict[str, str] = {}
    revived: dict[str, str] = {}
    unsure: dict[str, str] = {}
    # 旧域名在国内经常连不上，连续两次网络层失败就不再试了，免得每条都卡满超时
    legacy_state = {"enabled": bool(cross_host), "fail": 0}

    def try_legacy(path: str) -> str | None:
        if not legacy_state["enabled"]:
            return None
        st, detail = probe_image(f"{legacy}/ugc/{path}", timeout=6.0, retries=0)
        if st == ALIVE:
            return detail
        if st == UNKNOWN and not detail.startswith("HTTP"):
            legacy_state["fail"] += 1
            if legacy_state["fail"] >= 2:
                legacy_state["enabled"] = False
        return None

    def one(path: str) -> tuple[str, str, str]:
        for r in range(max(1, rounds)):
            time.sleep(delay + random.uniform(0, 0.4))
            st, detail = probe_image(f"{base}/ugc/{path}")
            if st == ALIVE:
                return path, ALIVE, f"复核第 {r + 1} 轮又出图了（{detail}），撤销删除"
            if st == UNKNOWN:
                return path, UNKNOWN, f"复核第 {r + 1} 轮探测失败（{detail}），这条先留着"
        hit = try_legacy(path)
        if hit:
            return path, ALIVE, f"旧域名仍能访问（{hit}），撤销删除"
        return path, DEAD, f"连续 {max(1, rounds)} 轮复核仍为 404"

    total = len(paths)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        for path, st, detail in pool.map(one, paths):
            done += 1
            if st == DEAD:
                confirmed[path] = detail
            elif st == ALIVE:
                revived[path] = detail
            else:
                unsure[path] = detail
            if not quiet:
                tag = {DEAD: "确认删除", ALIVE: "撤销", UNKNOWN: "存疑"}[st]
                print(f"  [{done:>3}/{total}] {path[:44].ljust(44)} {tag}  {detail}")
    return confirmed, revived, unsure


def prune_file(src: Path, *, host: str = DEFAULT_HOST, delay: float = 0.15,
               threads: int = 4, dry_run: bool = False, force: bool = False,
               max_ratio: float = 0.5, min_total: int = 10,
               quiet: bool = False, history: Path | None = None,
               write_history: bool = True, source: str = "local",
               confirm: int = 2, confirm_delay: float = 1.2,
               cross_host: bool = True) -> int:
    """探测并剔除 src 里的失效条目。支持两种文件格式：

      {"pics":  ["<ugc 路径>", ...]}                  —— 站点索引 r.json
      {"items": [{"url": "https://.../ugc/..."}, ...]} —— 抓取清单 urls.json
    """
    if not src.is_file():
        print(f"[错误] 找不到 {src}", file=sys.stderr)
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[错误] 解析 {src} 失败：{e}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print(f"[错误] {src} 的顶层不是对象", file=sys.stderr)
        return 1

    # 域名统一成裸主机名，避免配置里带了 https:// 时记录里拼出两遍协议头
    host = re.sub(r"^https?://", "", (host or DEFAULT_HOST).strip().rstrip("/")) or DEFAULT_HOST

    is_pics = isinstance(data.get("pics"), list)
    is_items = isinstance(data.get("items"), list)
    if not is_pics and not is_items:
        print(f"[错误] {src} 里既没有 pics 也没有 items", file=sys.stderr)
        return 1

    pics: list[str] = list(data["pics"]) if is_pics else []
    items: list[dict] = list(data["items"]) if is_items else []

    # 统一成"待探测的 UGC 路径"
    targets: list[str] = list(pics) if is_pics else []
    item_by_path: dict[str, dict] = {}
    for it in items:
        p = _ugc_path(str(it.get("url") or ""))
        if p:
            item_by_path.setdefault(p, it)
            if p not in targets:
                targets.append(p)

    total = len(targets)
    if total == 0:
        print("[跳过] 清单为空，无需探测")
        return 0

    print(f"[探测] {total} 条，{threads} 线程，已绕过 CDN 缓存")
    result = classify(targets, host, delay=delay, threads=threads, quiet=quiet)

    dead = [p for p in targets if result[p][0] == DEAD]

    # 复核：首次 404 不等于真删了。CDN 某个边缘节点、一次 DNS 抖动、
    # 甚至 Steam 侧短暂维护，都可能让一张好图返回 404。
    if dead and confirm > 0:
        print()
        print(f"[复核] {len(dead)} 条疑似失效，逐条再确认（每条约 {confirm_delay}s × {confirm} 轮）")
        confirmed, revived, unsure = confirm_dead(
            dead, host, rounds=confirm, delay=confirm_delay,
            cross_host=cross_host, threads=threads, quiet=quiet)
        for p, detail in revived.items():
            result[p] = (ALIVE, detail)
        for p, detail in unsure.items():
            result[p] = (UNKNOWN, detail)
        for p, detail in confirmed.items():
            result[p] = (DEAD, detail)
        if revived:
            print(f"[复核] {len(revived)} 条其实是活的，已撤销。")
        if unsure:
            print(f"[复核] {len(unsure)} 条复核时探测失败，按未知处理（保留）。")

    dead = [p for p in targets if result[p][0] == DEAD]
    unknown = [p for p in targets if result[p][0] == UNKNOWN]
    alive = [p for p in targets if result[p][0] == ALIVE]

    print()
    print(f"[结果] 存活 {len(alive)} · 失效 {len(dead)} · 未知 {len(unknown)}")
    if dead:
        print("[失效明细]")
        for p in dead:
            print(f"  - {p}  ({result[p][1]})")
    if unknown:
        print("[未知（已保留，不算删除）]")
        for p in unknown[:10]:
            print(f"  ? {p}  ({result[p][1]})")
        if len(unknown) > 10:
            print(f"  ... 另有 {len(unknown) - 10} 条")
        if os.environ.get("GITHUB_ACTIONS"):
            # 让 Action 页面顶部也能看到这类"没判死但要留意"的情况
            print(f"::warning::{len(unknown)} 条链接探测失败，已原样保留（未计入删除）")

    if not dead:
        print("[完成] 没有需要剔除的条目")
        return 0

    # ---- 安全阀 ----
    if len(dead) == total and not force:
        print("[中止] 全部条目都失效，这更像是网络/账号问题，而不是图全删了。", file=sys.stderr)
        print("       确认要清空索引时，加 --force 重跑。", file=sys.stderr)
        return 2
    if not force and total >= min_total and len(dead) / total > max_ratio:
        print(f"[中止] 本次失效 {len(dead)}/{total}，超过阈值 {max_ratio:.0%}。", file=sys.stderr)
        print("       可能是 CDN 对我们限流了。确认无误时加 --force 重跑。", file=sys.stderr)
        return 2
    if not force and not alive and unknown:
        print(f"[中止] 一条都没探活：失效 {len(dead)}、未知 {len(unknown)}。", file=sys.stderr)
        print("       这更像地区封锁 / DNS 故障 / CDN 临时限制，而不是图全被删。", file=sys.stderr)
        print("       过一会儿重跑；确认无误再加 --force。", file=sys.stderr)
        return 2

    if dry_run:
        print(f"[预演] 只报告不改文件。实际会剔除 {len(dead)} 条并记入删图记录，"
              f"加 --apply 才写入")
        return 0

    dead_set = set(dead)
    if is_pics:
        data["pics"] = [p for p in data["pics"] if p not in dead_set]
    if is_items:
        kept = []
        for it in data["items"]:
            p = _ugc_path(str(it.get("url") or ""))
            if p in dead_set:
                continue
            kept.append(it)
        data["items"] = kept

    bak = src.with_suffix(src.suffix + ".bak")
    atomic_write(bak, src.read_text(encoding="utf-8"))
    # 注意：只改 pics / items 两个字段，其它键原样保留，文件形状不变
    atomic_write(src, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"[完成] 已剔除 {len(dead)} 条，写回 {src}（备份 {bak.name}）")

    if write_history:
        hp = history or src.with_name(HISTORY_NAME)
        records = [
            _record(p, result[p][1], host, source=source,
                    extra=item_by_path.get(p))
            for p in dead
        ]
        added, total_kept = append_history(hp, records)
        skipped = len(records) - added
        extra = f"，{skipped} 条此前已有记录" if skipped else ""
        print(f"[记录] 已写入 {hp}（本次新增 {added} 条{extra}，累计 {total_kept} 条）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="prune",
        description="探测 Steam 图片 CDN 直链并剔除已失效的条目（GitHub Action 与本地共用）")
    ap.add_argument("--src", default="r.json", help="要处理的 JSON（默认 r.json）")
    ap.add_argument("--host", default=DEFAULT_HOST, help="图片 CDN 域名")
    ap.add_argument("--delay", type=float, default=0.15, help="每条请求的随机延迟上限（秒）")
    ap.add_argument("--threads", type=int, default=4, help="并发线程数（默认 4，别开太大）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--apply", action="store_true", help="确认写入（不加则等同预演）")
    ap.add_argument("--force", action="store_true", help="跳过安全阀（全部失效/超阈值时仍剔除）")
    ap.add_argument("--max-ratio", type=float, default=0.5, help="单次允许的失效比例上限")
    ap.add_argument("--min-total", type=int, default=10, help="低于这个总数不套用比例阈值")
    ap.add_argument("--quiet", action="store_true", help="不逐条打印")
    ap.add_argument("--history", help=f"删图记录文件（默认与 --src 同目录的 {HISTORY_NAME}）")
    ap.add_argument("--no-history", action="store_true", help="不写删图记录")
    ap.add_argument("--source", default="local", help="记录里的来源标签（local / action）")
    ap.add_argument("--confirm", type=int, default=2,
                    help="判死后复核几轮（默认 2，任意一轮出图就撤销；0 = 不复核，危险）")
    ap.add_argument("--confirm-delay", type=float, default=1.2,
                    help="复核每轮之间隔几秒（默认 1.2）")
    ap.add_argument("--no-cross-check", action="store_true",
                    help="不复用旧域名做交叉验证")
    a = ap.parse_args(argv)

    return prune_file(
        Path(a.src), host=a.host, delay=a.delay, threads=a.threads,
        dry_run=a.dry_run or not a.apply, force=a.force,
        max_ratio=a.max_ratio, min_total=a.min_total, quiet=a.quiet,
        history=Path(a.history) if a.history else None,
        write_history=not a.no_history, source=a.source,
        confirm=a.confirm, confirm_delay=a.confirm_delay,
        cross_host=not a.no_cross_check,
    )


if __name__ == "__main__":
    sys.exit(main())
