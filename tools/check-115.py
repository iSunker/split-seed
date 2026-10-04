# -*- coding: utf-8 -*-
"""「一批跑完之后该看什么」的观察器 —— 给 #115 现场验收**只读**用。

为什么要有它（2026-09-16）：
  #115 的判据是**三条对时间敏感的日志**，而现在每次验收都是
  「ssh 没有 ⇒ 手工 tail + grep + 心算 UTC/本地 + 假设 --max-wait=默认」——
  这个流程**同时踩过三个坑**：
    ① tail 一屏（≈40 行）**看不到批次头** ⇒ 该批的 `ok` 要从批头往下数；
    ② NAS 日志是**本地时间（+0800）**，而 cron 与 `last_end_ts` 是 **UTC** ——
       差 8 小时，几次三番差点把"次日凌晨"读成"当天凌晨"（同 #114）；
    ③ `--max-wait` 默认 30 分钟**只是默认**，run.sh 可以传别的 ⇒
       「N 分钟 > 上限 30 分钟」这句里的 30 必须**从日志里读**，不能假设。
  ⇒ 与其每次都手工算，不如让脚本把这三件事一次算完，并给出**三值结论**。

结论只有三个（**别自己发明第四种**）：
    PASS   三条判据同时成立（见下）
    FAIL   出现「一批 ok=0 且中止」= 旧行为 ⇒ 部署没生效或逻辑有误 ⇒ 考虑 rollback
    WAIT   本批压根没出退避（绝大多数情况）⇒ **不算通过也不算失败**，等下一批
    ERROR  连日志都读不到（NAS 掉线 / 路径变了）—— 这是**观测故障**，不是 #115 的结论

三条判据（缺一不可，`--expect-*` 就是照抄 `SUMMARY` §24 里那句）：
    1. `要等到 …（N 分钟 > 上限 M 分钟）` 且 **N > M**
    2. 该批 **`ok ≠ 0`**（照发了，不是中止）
    3. 出现 `不等，照发`，且同句带「**还剩 N 个健康站**」

用法：
    python tools/check-115.py                 # 自动挑最近有日志的批次
    python tools/check-115.py --batch 4       # 看倒数第 4 批（0 = 最近一批）
    python tools/check-115.py --since 2026-09-16T07:31   # 只看部署之后的批次
    python tools/check-115.py --grep 4622300  # 手工搜一段（绕过批次切分）

★ **只读**：全程 `open(..., "r")`，不写 NAS、不联网、不碰生产库。
  唯一会写的是 stdout。
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★ 默认路径就是现场那个；挂不上就报 ERROR 而不是崩。
LOG = ("//iSunker-DS423/docker_ssd/reseed"
       "/data/drive-loop/state/drive-loop.log")

#: ★ 部署时刻（UTC）。这是 #115 的**分界线**：之前的批次跑的是老代码，
#:   把它们算进来会得出"FAIL"——而那个 FAIL 是**历史**，不是本次部署的结论。
#:   来源：09-16 07:31 UTC `deploy.sh --apply`（见 #114/#115 的部署记录）。
DEPLOY_UTC = datetime(2026, 9, 16, 7, 31, tzinfo=timezone.utc)

#: NAS 日志是**本地时间**（+0800）。★ 别用本机时区去猜：本机可能不在 +0800。
LOG_TZ = timezone(timedelta(hours=8))

HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] "
                  r"=== drive-loop 启动：packs=(\[[^\]]*\]) "
                  r"indexers=([^ ]+) limit=(\d+) ===")
#: 该批的包：`[--once] 跑包 frds-top250-2024（第 3/3 个）`
PACK = re.compile(r"\[--once\] 跑包 ([^（]+)（第 (\d+)/(\d+) 个）")
#: 该批有没有配 `--max-wait`（写了才认；没写就是默认 30 分钟）
MAXWAIT = re.compile(r"max[_-]wait[= ](\d+(?:\.\d+)?)\s*分")
#: 判据 1 的正体
WAIT_UNTIL = re.compile(
    r"要等到\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"（(\d+(?:\.\d+)?)\s*分钟\s*>\s*上限\s*(\d+(?:\.\d+)?)\s*分钟）")
#: 判据 3 的正体
SHIP_ANYWAY = re.compile(r"不等，照发")
HEALTHY = re.compile(r"还剩\s*(\d+)\s*个健康站")

OK = re.compile(r"\[(\d+)/(\d+)\] OK")
SKIP = re.compile(r"\[(\d+)/(\d+)\] (?:SKIP|跳过)")
_UNIT = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "h": 3600, "s": 1,
         "m": 60}


def dur_min(s):
    """从一句中文里取「还要等多久（分钟）」。取不到返回 None（**不猜**）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|h|m|s)", s)
    if not m:
        return None
    return float(m.group(1)) * _UNIT.get(m.group(2), 1) / 60.0


def local(s):
    """'2026-09-16 23:45:03' → 带 +0800 的 datetime。"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOG_TZ)


def batches(path, since=None):
    """把日志切成批次。返回 [(line_no, header_dt, packs, indexers, [lines])]。"""
    out, cur = [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f, 1):
            line = raw.rstrip("\r\n")
            m = HEAD.match(line)
            if m:
                if cur:
                    out.append(cur)
                dt = local(m.group(1))
                cur = (i, dt, m.group(2), m.group(3), [line])
                continue
            if cur:
                cur[4].append(line)
    if cur:
        out.append(cur)
    if since:
        out = [b for b in out if b[1] >= since]
    return out


def analyse(b, *, verbose=True):
    ln, dt, packs, indexers, lines = b
    end = local(lines[-1][:19]) if lines and re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", lines[-1]) else dt
    body = "\n".join(lines)

    # ★★ 先判这一批**跑的是不是新代码** —— 判据 2/3 只在部署后才有意义。
    #   ★ 这是个**独立的轴**，别把它和「本批有没有出事」搅在一起：
    #     实测（2026-09-17）：日志里 `=== drive-loop 启动 ===` 有 **430+ 次**，
    #     而批次数**远多于** `===` 的条数 ⇒ **有批次没有自己的批头**
    #     （crontab 唤醒后直接走 `--once` 那条路）。
    #     ⇒ 「倒数第 N 批」这种数法**会指错**，可能指到部署**之前**的老行为上。
    #     所以：① 部署前一律给 **PRE**，不参与判定；② `--batch N` 要提示这一点。
    after_deploy = dt.astimezone(timezone.utc) >= DEPLOY_UTC
    ok_n = len(OK.findall(body))
    skip_n = len(SKIP.findall(body))
    # 该批 `--max-wait`：★ 从日志读，读不到就用默认 30 并**明说这是默认**。
    mw = MAXWAIT.search(body)
    maxwait = float(mw.group(1)) if mw else 30.0

    if not after_deploy:
        # ★ 第四种结论：**老代码，不参与 #115 判定**。
        #   它的中止在当时是**正确行为**，把它算成 FAIL 是本仓反复防的
        #   「把历史读成结论」。
        verdict = "PRE"
        why = (f"该批在部署（{DEPLOY_UTC:%m-%d %H:%M} UTC）之前 ⇒ 跑的是旧代码，"
               "它的中止是**正确行为**，不能当 #115 的结论")
        if verbose:
            print("─" * 72)
            print(f"批次 @{ln}  {dt:%Y-%m-%d %H:%M:%S} +0800"
                  f"   = {dt.astimezone(timezone.utc):%m-%d %H:%M} UTC")
            print(f"  包 {packs}   indexers={indexers}")
            print(f"  ok={ok_n}  跳过={skip_n}  中止={'是' if '提前中止' in body else '否'}"
                  f"  --max-wait={maxwait:.0f} 分钟")
            print("  部署之后？否")
            print(f"  ⇒ 结论 **{verdict}**：{why}")
        return verdict, why, dict(ln=ln, dt=dt, ok=ok_n, judged={},
                                  ship=0, healthy=[], maxwait=maxwait,
                                  after=False)

    aborted = "提前中止" in body or "本批中止" in body
    ship = [l for l in lines if SHIP_ANYWAY.search(l)]
    healthy = HEALTHY.findall(body)
    halted = [l for l in lines
              if "要等到" in l or "被限流" in l or "中止" in l]
    # `!` / `★` 前缀是 `emit` 写给运维看的那一档（warn / alert）
    loud = [l for l in lines if re.search(r"^\d{4}.*\[WARNING\]|^\s*[!★]", l)]

    judged = {}
    for l in lines:
        m = WAIT_UNTIL.search(l)
        if m:
            n = float(m.group(2))
            cap = float(m.group(3))
            judged = dict(at=l[11:19], until=m.group(1), n=n, cap=cap,
                          n_gt_cap=n > cap)
            break

    # ★ 判据 1 的第二条腿：`要等到` 那行可能没有「（N 分钟 > 上限 M 分钟）」括号
    #   （短等那一路是「退避中，等到 MM-DD HH:MM:SS（Ns）」）。
    #   ⇒ 只认「> 上限」那条；认不出就**明确说认不出**，别拿别的行冒充。
    if not judged:
        for l in lines:
            m = re.search(r"要等到\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
            if m:
                need = dur_min(l[m.end():]) or dur_min(l)
                n = need if need is not None else 0.0
                judged = dict(at=l[11:19], until=m.group(1), n=n,
                              cap=maxwait, n_gt_cap=n > maxwait,
                              note="该行没有「（N 分钟 > 上限 M）」，"
                                   "N 是从正文里的时长反推的")
                break

    if verbose:
        print("─" * 72)
        print(f"批次 @{ln}  {dt:%Y-%m-%d %H:%M:%S} +0800"
              f"   = {dt.astimezone(timezone.utc):%m-%d %H:%M} UTC")
        print(f"  包 {packs}   indexers={indexers}")
        print(f"  跨度 {dt:%H:%M:%S} → {end:%H:%M:%S}"
              f"（{(end-dt).total_seconds()/60:.1f} 分钟）")
        print(f"  ok={ok_n}  跳过={skip_n}  中止={'是' if aborted else '否'}"
              f"  --max-wait={maxwait:.0f} 分钟"
              f"{'（日志未写，按默认）' if not mw else ''}")
        print(f"  部署之后？{'是' if dt.astimezone(timezone.utc) >= DEPLOY_UTC else '否 —— 老代码，不参与 #115 判定'}")
        if judged:
            print(f"  ★ 要等到 {judged['until']}  N={judged['n']:.0f} 分钟"
                  f"  > 上限 {judged['cap']:.0f} 分钟？"
                  f"{'是' if judged['n_gt_cap'] else '否'}")
            if judged.get("note"):
                print(f"    （{judged['note']}）")
        else:
            print("  · 本批**没有**「要等到 …」这类退避行")
        if ship:
            print(f"  ★ 「不等，照发」出现 {len(ship)} 次：")
            for l in ship[:3]:
                print("      " + l.strip())
            print(f"    同句带「还剩 N 个健康站」？"
                  f"{'是 ' + ','.join(healthy) if healthy else '★ 没看到 —— 判据 3 不完整'}")
        for l in loud[:8]:
            print("    ! " + l.strip()[:150])
        if halted and not judged:
            for l in halted[:5]:
                print("    ? " + l.strip()[:150])

    # ---- 结论 -------------------------------------------------------------
    if aborted and ok_n == 0:
        verdict = "FAIL"
        why = "出现「一批 ok=0 且中止」⇒ 旧行为（部署没生效或逻辑有误）⇒ 考虑 rollback"
    elif judged and judged["n_gt_cap"] and ok_n > 0 and ship and healthy:
        verdict = "PASS"
        why = "三条判据同时成立：超上限退避 + ok≠0 + 不等照发（还剩健康站）"
    elif judged and judged["n_gt_cap"] and ok_n > 0 and ship and not healthy:
        verdict = "WAIT"
        why = ("出现了「不等，照发」但**同句没看到「还剩 N 个健康站」**"
               "⇒ 判据 3 不完整，**先别记 PASS**，把原文抄回来人工确认")
    else:
        verdict = "WAIT"
        if not judged:
            why = "本批压根没出退避（正常跑完）⇒ 不算通过也不算失败，等下一批"
        elif not judged["n_gt_cap"]:
            why = (f"退避有，但 N={judged['n']:.0f} ≤ 上限 {judged['cap']:.0f} 分钟"
                   "⇒ 没触发「超上限」那一支，等下一批")
        elif ok_n == 0:
            why = "超上限退避且本批一条都没发 ⇒ 见上面原文，可能是另一条路"
        else:
            why = "退避超上限但没看到「不等，照发」⇒ 等下一批（或看原文）"

    if verbose:
        print(f"  ⇒ 结论 **{verdict}**：{why}")
    return verdict, why, dict(ln=ln, dt=dt, ok=ok_n, aborted=aborted,
                              judged=judged, ship=len(ship),
                              healthy=healthy, maxwait=maxwait)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--batch", type=int, default=None,
                    help="0 = 最近一批，1 = 倒数第二批 …（默认：最近一批）")
    ap.add_argument("--since", help="只看此后的批次，ISO 本地时间 "
                                   "（如 2026-09-16T15:31 表示 +0800）")
    ap.add_argument("--grep", help="不切批次，直接搜这段（只打印命中行）")
    ap.add_argument("--all", action="store_true",
                    help="逐批列表 + 结论（默认只看最后一批）")
    a = ap.parse_args()

    if not os.path.exists(a.log):
        print(f"★ ERROR 读不到日志：{a.log}")
        print("  这是**观测故障**，不是 #115 的结论 —— 别据此说『没出现退避』。")
        return 2

    since = None
    if a.since:
        since = datetime.fromisoformat(a.since).replace(tzinfo=LOG_TZ)

    if a.grep:
        with open(a.log, encoding="utf-8", errors="replace") as f:
            for i, l in enumerate(f, 1):
                if a.grep in l:
                    print(f"{i:>6}  {l.rstrip()[:160]}")
        return 0

    bs = batches(a.log, since=since)
    if not bs:
        print("★ 没有切出任何批次（日志为空？或 --since 太靠后？）")
        return 2
    print(f"共 {len(bs)} 批"
          + (f"（--since 之后）" if since else "")
          + f"；最后一批 @{bs[-1][0]}  {bs[-1][1]:%Y-%m-%d %H:%M:%S} +0800")
    #   ★★ 这两个数**对不上是正常的**（实测 2026-09-17：切出 44 批、`===` 只有 42 条，
    #      而日志里其实跑了几百批）⇒ **批次数远多于 `===` 的条数**。
    #      成因：crontab 唤醒后直接走 `--once`，**不是每次都有批头**。
    #      ⇒ 「倒数第 N 批」只能当**近似**，判据 2/3 一律以**批头之后那一段**为准。
    _n_head = 0
    with open(a.log, encoding="utf-8", errors="replace") as _f:
        for _l in _f:
            if HEAD.match(_l.rstrip()):
                _n_head += 1
    if _n_head != len(bs):
        print(f"  ★ 提醒：日志里 `=== drive-loop 启动 ===` 有 {_n_head} 条，"
              f"而本次切出 {len(bs)} 批 —— 两者不等说明**有批次没有批头**；"
              f"`--batch N` 只当近似，别用它定位关键批次。")
    print(f"部署分界线 {DEPLOY_UTC:%Y-%m-%d %H:%M} UTC"
          f" = {(DEPLOY_UTC.astimezone(LOG_TZ)):%m-%d %H:%M} +0800")

    if a.all:
        for b in bs:
            analyse(b, verbose=False)
            print(f"@{b[0]:<7} {b[1]:%m-%d %H:%M} {b[2]:<34}", end="  ")
            v, why, _ = analyse(b, verbose=False)
            print(f"{v:<5} {why[:60]}")
        return 0

    want = bs[-1] if a.batch is None else bs[-(a.batch + 1)]
    v, _w, _ = analyse(want)
    return 0 if v in ("PASS",) else (1 if v == "FAIL" else 0)


if __name__ == "__main__":
    sys.exit(main())
