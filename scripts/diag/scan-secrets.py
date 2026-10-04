#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推前凭据扫描 —— 按**值的形状**，不是按**键名**。

为什么按形状
------------
按"键名叫什么"匹配（`PROWLARR_API_KEY=` 之类）会漏掉**嵌在结构里**的凭据 ——
`TORZNAB_URLS` 里逗号分隔的多条 URL 各带一个 `apikey=`、Prowlarr 的
`indexer.fields` 里塞着 cookie/passkey、`options`/`fields` 里还嵌一层。
所以这里两道网，**都只看"值长什么样"**：

  ① **值指纹**：从本地 `.env` 提出真实凭据值（只算 sha256，值本身不落到任何输出里），
     再去全仓库找有没有哪一行出现同样的值。
  ② **形状正则**：完全不依赖 `.env` —— 兜住"换个站 / 换了 key / 值被截断"的情况。

★★ 但要知道**①的覆盖有多薄**：本地 `.env` 是**开发存根**（它的 `TORZNAB_URLS` 用的
   就是 `.env.example` 那两个 `xxxx…/yyyy…` 占位符），真实凭据只存在于 NAS 的生产
   `.env` 与 Prowlarr UI 里。所以「值指纹 0 命中」**只证明仓库不含本地 .env 的值，
   几乎不构成保证** —— **这道闸门的真防线是形状层**。也正因如此，形状命中必须
   真的参与退出码（它曾经不参与，是纯装饰，见下面"第四个 bug"）。

只报"命中/未命中"与 文件:行号，**绝不打印命中到的那个值本身**（只打前 4 位 + 长度）。

用法
----
    python scripts/scan-secrets.py                     # 扫 git ls-files 里的全部文件
    python scripts/scan-secrets.py <仓库根> [文件...]    # 换根 / 只扫指定文件
    python scripts/scan-secrets.py . && git push       # 典型用法：绿了再推

退出码
------
    0 = 本次推送**没有新引入**任何凭据形状的东西
    1 = 有新引入的命中（**别推**，先看）
"""
import hashlib
import pathlib
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 不给仓库根时就按**本脚本的位置**推（仓库根 = 本文件的**上上级**），
# 这样从任意 cwd 跑都一样 —— 同 tests/ 里那些脚本的约定。
# ★★ 2026-10-05 修一个**静默失效**：本脚本在 `scripts/diag/` 下（不是 `scripts/`）⇒
#   仓库根是 `__file__` 的**上两级**。旧写法 `.parent.parent` 只到 `scripts/`
#   ⇒ 默认跑时**只扫 scripts/ 那 37 个文件**，屏幕上照旧打印"零命中" ——
#   一个字的错都不报，正是本脚本存在的理由（防凭据入库）被它自己绕过。
#   （与它自己头注里记的那次"中文名被静默跳过"是**同一形状**：错的是取数方式。）
REPO = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
        else pathlib.Path(__file__).resolve().parent.parent.parent)
ENV = REPO / ".env"
FILES = sys.argv[2:]

if not FILES:
    # ★★ 必须 `-c core.quotepath=false` + `-z`（2026-09-17，`#119`/`ERR-GIT-04`）。
    #    默认 `quotepath=true` 会把**非 ASCII 路径**输出成带引号 + 八进制转义：
    #        "summary/00-\345\215\240\344\275\215\347\254\246..."
    #    而本仓**大量文件名是中文**（`summary/*.md` 等 25 个）。旧写法下：
    #      · `out.split("\n")` 拿到的是**带引号的字符串**，它不是真路径；
    #      · 下游 `if not p.is_file(): continue` ⇒ **25 个文件全部静默跳过**；
    #      · 屏幕上照旧打印「工作区零命中」—— **一个字的错都不报**。
    #    ⇒ 实测：默认 111 个文件里有 25 个中文件名，全部没被扫过。
    #    ★ 这也正是本脚本存在的理由（防凭据入库）被**它自己**绕过的那一格。
    #    ★ 判据在下面那段「扫了几个文件」的自证里（数量对不上就报出来）。
    out = subprocess.run(["git", "-C", str(REPO), "-c", "core.quotepath=false",
                          "ls-files", "-z"],
                         capture_output=True).stdout.decode("utf-8", "replace")
    FILES = [f for f in out.split("\0") if f.strip()]


def secret_shape(v):
    """值**长什么样**才算凭据 —— 不按键名。返回形状名或 None。

    ★ 不这么筛的话，`.env` 里的 QBIT_CATEGORY（`alnum-slug len=14`，一个分类名）
      会被当成"凭据"，于是文档里提到那个分类名就误报命中。
    """
    if re.fullmatch(r"[0-9a-fA-F]{20,}", v):
        return "hex"
    if re.search(r"[?&](?:apikey|passkey)=", v):
        return "url-cred"
    if (len(v) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", v)
            and re.search(r"[A-Z]", v) and re.search(r"[a-z]", v)
            and re.search(r"[0-9]", v)):
        return "mixed-alnum"
    return None


# ---------- 0) 什么**不算**凭据 —— 三条"看着像、其实不是"的形态 ----------
# ★ 这三条是 2026-09-12 推前扫描实测逼出来的：全仓库 5 处形状命中，逐个看完
#   **没有一处是凭据** —— 3 处是"读变量"的代码（process.env.X / cfg.matcher.x），
#   2 处是模板占位符（please-change-me-…、yyyy…）。
#   认不出它们，闸门就**次次都红**；而**次次都红的闸门等于没有闸门**
#   —— 同 check-deploy-drift.py 里那句"一个会误报的哨兵，两次之后就没人看了"。
#   代价说清楚：这是启发式，不是证明。**假阳性规则永远优先于"宁可错杀"**，
#   因为错杀的代价是整道闸门被无视（那才是真的漏）。
# ★ 每条跳过的都要**连带理由列出来**。静默跳过 = 把闸门悄悄钻个洞。
CODE_REF = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.){2,}[A-Za-z_][A-Za-z0-9_]*$")


def benign(v):
    """返回"这不是凭据"的理由；返回 None 表示**要人看**。"""
    if len(set(v)) == 1:
        return "同一字符重复"
    if CODE_REF.match(v):
        return "读变量/取属性的代码表达式，不是字面量"
    # ★ 已知盲区，写在这里而不是装作没有：只由**小写字母 + 连字符**组成、每段都是
    #   纯字母的长字符串，本扫描器会当成英文短语放过。真实凭据（hex / base64 /
    #   混合字母数字）几乎不可能长这样，所以这条换来的降噪远大于它带来的盲区；
    #   但"几乎不可能"不是"不可能" —— 一个真的长成英文短语的密钥，这道闸门抓不到。
    segs = re.split(r"[-_]", v)
    if (not re.search(r"[0-9A-Z]", v) and len(segs) >= 3
            and all(s.isalpha() for s in segs)):
        return "英文短语/占位符（无数字、无大写、≥3 段纯字母）"
    return None


# ---------- 1) 从本地 .env 抽出凭据值（只留在内存里，不打印） ----------
needles = {}          # sha256前12位 -> (形状, [键名])
shapes_seen = {}
skipped_needles = []  # [(键名, 形状, 理由)] —— 只报键名与理由，**不报值**
if ENV.exists():
    for line in ENV.read_bytes().decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not v:
            continue
        parts = [v]
        if k == "TORZNAB_URLS":                      # 逗号分隔的 URL 列表
            parts = [p.strip() for p in v.split(",") if p.strip()]
        for p in parts:
            cands = []
            m = re.search(r"[?&]apikey=([^,&]+)", p)  # URL 里的 apikey 段
            if m:
                cands.append(("url-cred", m.group(1)))
            else:
                s = secret_shape(p)
                if s:
                    cands.append((s, p))
            for s, c in cands:
                if len(c) < 12:
                    continue
                why = benign(c)
                if why:
                    # .env 里的占位符（本地 .env 是**开发存根**，TORZNAB_URLS 用的
                    # 就是 .env.example 那两个 xxxx/yyyy 占位符）—— 拿它当"真实凭据"
                    # 去全仓库找，只会把模板本身报成泄露。
                    skipped_needles.append((k, s, why))
                    continue
                shapes_seen[s] = shapes_seen.get(s, 0) + 1
                needles.setdefault(hashlib.sha256(c.encode()).hexdigest()[:12], (s, []))[1].append(k)

print(f"从 .env 提取到 {len(needles)} 个**去重后**的凭据指纹"
      f"（{shapes_seen}；值本身不打印）")
for k, s, why in skipped_needles:
    print(f"   · .env 的 {k} 按「{why}」跳过（不当成真实凭据；只报键名不报值）")

# ---------- 1b) ★★ 自证：到底扫了几个文件（2026-09-17，`#119`）----------
#   ★ 为什么这一步是**必须**的：这个脚本的**全部价值**在于"扫过整个仓库"。
#     而它的失败方式恰恰是**静默** —— 见 `#119`/`ERR-GIT-04` 的实测：
#     `core.quotepath` 默认 true ⇒ 非 ASCII 路径被转义 ⇒ `is_file()` 为假
#     ⇒ **25 个 `summary/*.md` 全部跳过**，而屏幕上照旧打「零命中」。
#   ⇒ 判据：**先说出扫了几个，再对期望数量立断言**。
#     `_missing` 非空 = 有路径没读到文件 —— 那是**观测故障**，不是"干净"。
_checked = 0
_missing = []
for rel in FILES:
    if (REPO / rel).is_file():
        _checked += 1
    else:
        _missing.append(rel)
print(f"扫描范围：**{_checked}/{len(FILES)} 个文件**（git ls-files 给出 {len(FILES)} 个）")
if _missing:
    print(f"   ★★ 有 {len(_missing)} 个路径**读不到文件** ⇒ 本次扫描**不完整**，"
          f"别把下面的「零命中」当结论：")
    for rel in _missing[:5]:
        print(f"      {rel[:90]}")
    if len(_missing) > 5:
        print(f"      …另有 {len(_missing) - 5} 个")
    print("   ★ 常见成因：`core.quotepath` 把非 ASCII 路径转义了（见 ERR-GIT-04）。")

# ---------- 2) 形状正则：不依赖 .env，兜住"换个站/换个 key"的情况 ----------
# ★ 2026-09-12 阳性对照抓出的第三个 bug：`\b` 锚点在 `PROWLARR_API_KEY=` 上**不成立**
#   （前面是 `_`，也是词字符 → 没有词边界），于是 `PREFIX_API_KEY=值` 这种**最常见的**
#   形态一律漏报 —— 而 `PROWLARR_API_KEY` / `CROSSSEED_API_KEY` 正是本仓库最可能泄露的两个名字。
#   改成 `[A-Za-z0-9_]*` 前缀（不用 \b）。Cookie 那条同病同修。
SHAPES = [
    ("URL 里的 apikey=", re.compile(r"[?&]apikey=([A-Za-z0-9._\-]{16,})")),
    ("passkey=", re.compile(r"(?i)passkey=([A-Za-z0-9]{16,})")),
    ("api_key/API_KEY 赋值", re.compile(r"(?i)[A-Za-z0-9_]*(?:api[_-]?key|apikey)\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{20,})")),
    ("Bearer 令牌", re.compile(r"(?i)bearer\s+([A-Za-z0-9._\-]{20,})")),
    ("Cookie 头值", re.compile(r"(?i)[A-Za-z0-9_-]*(?:set-)?cookie\s*[:=]\s*[\"']?([A-Za-z0-9._%\-]{8,})")),
]

hits = []
shape_hits = []
skipped = []          # [(rel, 行号, 规则名, 理由)] —— 跳过的都列出来，不静默

# ★ 「这一行是不是本次新引入的」必须**逐行**比，不能比整个文件 —— 文件只要有一处
#   改动，整文件比对就会把老内容也报成"本次新引入"（第一版就是这么误报的）。
# ★ line_in_head 提到这里：形状层同样要用它。原来它只长在 `if hits:` 里面，
#   于是**形状命中压根没有"是不是本次引入"这个概念** —— 那是第五个 bug 的一半。
head_cache = {}


def line_in_head(rel, ln):
    if rel not in head_cache:
        r = subprocess.run(["git", "-C", str(REPO), "show", f"HEAD:{rel}"],
                           capture_output=True)
        head_cache[rel] = set(
            r.stdout.decode("utf-8", "replace").splitlines()
        ) if r.returncode == 0 else set()
    return ln in head_cache[rel]


for rel in FILES:
    p = REPO / rel
    if not p.is_file():
        continue
    try:
        text = p.read_bytes().decode("utf-8", "replace")
    except OSError:
        continue
    lines = text.splitlines()

    for h, (shp, keys) in needles.items():
        for i, ln in enumerate(lines, 1):
            # 逐"词元"算指纹。★ 必须连 `=` `:` `&` 一起切 —— 否则 `KEY=secret`
            #   整行是一个词元，它的哈希永远不等于 `secret` 的哈希，
            #   于是**泄露在 `KEY=值` 形态里的凭据一律漏报**（阳性对照实测抓到的）。
            for tok in re.split(r"""[\s,;\"'()\[\]{}<>|=&:]+""", ln):
                tok = tok.strip()
                if len(tok) < 12:
                    continue
                if hashlib.sha256(tok.encode()).hexdigest()[:12] == h:
                    hits.append((rel, i, shp + " / " + "/".join(sorted(set(keys))), ln))
                    break

    for name, rx in SHAPES:
        for m in rx.finditer(text):
            val = m.group(1)
            ln_no = text.count("\n", 0, m.start()) + 1
            # 假 key 白名单：测试样本用的，故意入库
            if val.upper().startswith("FAKE") or set(val) <= set("Xx0"):
                skipped.append((rel, ln_no, name, "标注为假样本（FAKE / 全 X0）"))
                continue
            up = text[max(0, m.start() - 120):m.start()].upper()
            if "FAKE" in up:                       # 同一行的注释里点明了是假样本
                skipped.append((rel, ln_no, name, "同一行注释里点明是假样本"))
                continue
            why = benign(val)
            if why:
                skipped.append((rel, ln_no, name, why))
                continue
            # ★ 存**整行文本**，不是行号：line_in_head 是**按内容**比的（HEAD 的行集合）。
            #   传行号进去永远不匹配 ⇒ 每一处形状命中都被判成"本次新引入" ⇒
            #   闸门变成"次次都拦"。（这正是第五个 bug 的另一半。）
            shape_hits.append((rel, ln_no, name, val[:4] + "…" + str(len(val)) + "位",
                               lines[ln_no - 1]))

print()
new_hits = []
if hits:
    print("① 本地 .env 里的凭据值出现在仓库里：")
    for rel, i, k, ln in hits:
        old = line_in_head(rel, ln)
        print(f"   {rel}:{i}  形状/键={k}  {'（HEAD 里本就有）' if old else '★ 本次新引入'}")
        if not old:
            new_hits.append((rel, i, k))
else:
    print("✓ 本地 .env 里的凭据值：**0 命中**")

new_shape = []
if shape_hits:
    print(f"\n② 形状正则命中 {len(shape_hits)} 处（需人工判断是不是假样本）：")
    for rel, ln_no, name, desc, line_txt in shape_hits:
        old = line_in_head(rel, line_txt)
        print(f"   {rel}:{ln_no}  [{name}]  {desc}  "
              f"{'（HEAD 里本就有）' if old else '★ 本次新引入'}")
        if not old:
            new_shape.append((rel, ln_no, name))
else:
    print("✓ 形状正则：**0 命中**")

if skipped:
    print(f"\n   另有 {len(skipped)} 处按规则跳过（**列出来就是为了不静默**）：")
    for rel, ln_no, name, why in skipped:
        print(f"   {rel}:{ln_no}  [{name}]  {why}")

# ---------- 拦截判据 ----------
# ★ 2026-09-12 对照⑤ 抓出的**第四个** bug（形状层不参与退出码），
#   以及顺着查出来的**第五个**（退出码用 hits 而不是 new_hits）。
#
#   ④ 的错法：`sys.exit(1 if hits else 0)` —— 只有值指纹决定退出码，形状层**打印完
#     就退出 0**，是纯装饰。而形状层存在的**唯一理由**恰恰是兜住"值指纹抓不到的"：
#     本地 .env 里有的，值指纹那层就抓到了；值指纹抓不到的（换个站 / 换了 key /
#     值被截断），正是形状层该拦的 —— 它却一个字都不拦。
#     它还为假阳性准备了两道逃生口（FAKE 前缀、同行注释含 FAKE），一个不拦东西的
#     闸门配两个逃生口，说明本意就是要拦。
#
#   ⑤ 的错法：上方代码明明写着"这决定要不要拦推"，算出了 new_hits 却没用它；
#     而且 line_in_head 只长在 `if hits:` 里，形状层连用都没用上。
#     **推前闸门判的是"这次推新引入了什么"** —— HEAD 里本来就有的内容早就推上去了，
#     拿它拦当前这次推，等于闸门永远关着（本地 .env 是存根时尤其如此：
#     .env.example 里那两个占位符会和 .env 撞上，次次都报）。所以判据用 new_*。
#
# ★ 但不假装没看见：HEAD 里就有的那些单列一句警告（下面），它们**已经在仓库里**了。
if (hits and not new_hits) or (shape_hits and not new_shape):
    print("\n   ⚠ 上面标「HEAD 里本就有」的不拦本次推送（早就推上去了），"
          "但它们已经在仓库里，值得单独查一次。")

sys.exit(1 if (new_hits or new_shape) else 0)
