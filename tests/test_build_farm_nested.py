# -*- coding: utf-8 -*-
r"""`build-farm.sh` 的**镜像深度跟随**回归 —— 钉住"嵌套单片"这条本轮新增的能力。

## 为什么这个文件必须存在

大包里的**嵌套单片**（如 `DC/01.绿箭侠/Arrow.S01-S08/Arrow.S01.Bluray/…`）要靠
`build-farm.sh **深镜像**进农场；而**旧脚本只镜像每个源的一层直接子项**。
本轮（2026-10-05）把建场这一半改成"**镜像深度跟随源**"：`FARM_SOURCES` 仍是一层，
`FARM_DEEP_SOURCES` 按 `FARM_DEPTH` 递归。**难点全在"校验口径"**：

### ① 为什么不用"全局 DEPTH_ANY"数孤儿（最易错处，直接导致假孤儿）

第一版想当然：把 `N_FARM` 改成 `find -maxdepth $DEPTH_ANY`。但**扁平源按深度 1 枚举、
却用 `cp -al` 带出整棵子树** ⇒ 拿统一深度去数，会把 `PackA/single` 这种合法深层
一律当孤儿。**修法 = 覆盖集 COVER**：农场路径合法 ⟺ 它是某个期望条目的**前缀之一**
（因为"对每个顶层期望条目一次 cp -al"保证农场里任何路径都落在某棵期望树里）。
本文件的多源夹具**同时含扁平源与深源**，正是为了让"统一深度"的实现**必然红**。

### ② 为什么孤儿只报"顶层"

一棵被删的源树在盘上留下 `Gone`、`Gone/sub`、`Gone/sub/x` 全未覆盖。若逐层报，
一条删除会变成三行噪音、`--prune` 还会逐层 `rm -rf`。判据 = **只报其"父已被覆盖
（或父是农场根）"的那一层**，即孤儿树的顶端。子断言⑤钉住这点。

### ③ 为什么"父存在时嵌套缺项"必须单独补（自愈）

顶层 `Arrow.S01-S08` 已存在时，它的 `cp -al` 因"已存在"被跳过 ⇒ 若只对嵌套项
`continue`，那个被删的子目录**永远修不好**。子断言④b 钉住"增量自愈"。

## 夹具怎么造（全合成，不碰真库/真农场）
    env/.env     CRLF 行尾（**必须**，见 build-farm.sh 头注的血泪注释），
                 FARM_SOURCES=<Flat>  FARM_DEEP_SOURCES=<DC/01.green>  FARM_DEPTH=2
    src/Flat/PackA/single/s.mkv            扁平源（一层）
    src/DC/01.green/Arrow.S01-S08/…        深源（两层）
    farm/                                    空
★ 用 `sh`（不是 `bash`）跑脚本，贴着 POSIX 语义 —— 本仓没有 bash 时也成立。
★ 本机 `cp -al`/`find -maxdepth` 不支持时（少见），相关断言**明说未验**、不算败。
"""
import os
import pathlib
import shutil
import subprocess
import stat
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "data" / "drive-loop" / "scripts" / "build-farm.sh"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

fails: list[str] = []


def ck(label, got, want):
    ok = got == want
    print(("  ok  " if ok else " FAIL ") + f"{label}: {got!r}  (want {want!r})")
    if not ok:
        fails.append(f"{label}: got {got!r} want {want!r}")


def ck_true(label, cond, detail=""):
    print(("  ok  " if cond else " FAIL ") + f"{label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(f"{label} [{detail}]")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
LAB = pathlib.Path(tempfile.mkdtemp(prefix="farmlab-"))
ENV = LAB / "env"
FARM = LAB / "farm"
SRC = LAB / "src"
ENV.mkdir()
FARM.mkdir()
(SRC / "Flat" / "PackA" / "single").mkdir(parents=True)
(SRC / "Flat" / "PackA" / "single" / "s.mkv").write_bytes(b"flat")
DEEP = SRC / "DC" / "01.green"
(SRC / "DC" / "01.green" / "Arrow.S01-S08" / "Arrow.S01.Bluray").mkdir(parents=True)
(SRC / "DC" / "01.green" / "Arrow.S01-S08" / "Arrow.S02.Bluray").mkdir(parents=True)
(DEEP / "Arrow.S01-S08" / "Arrow.S01.Bluray" / "a.mkv").write_bytes(b"a")
(DEEP / "Arrow.S01-S08" / "Arrow.S01.Bluray" / "b.mkv").write_bytes(b"b")
(DEEP / "Arrow.S01-S08" / "Arrow.S02.Bluray" / "c.mkv").write_bytes(b"c")

ENVFILE = ENV / ".env"


def write_env(deep_line: str = None, flat_line: str = None):
    """写 CRLF .env。默认 = 扁平源 Flat + 深源 DC/01.green + FARM_DEPTH=2。

    ★ 路径写 **正斜杠**（同 `run()` 的理由）。
    """
    flat = flat_line if flat_line is not None else (SRC / "Flat").as_posix()
    deep = deep_line if deep_line is not None else DEEP.as_posix()
    body = f"DATA_DIRS=\r\nFARM_SOURCES={flat}\r\nFARM_DEEP_SOURCES={deep}\r\nFARM_DEPTH=2\r\n"
    ENVFILE.write_bytes(body.encode("utf-8"))


def run(*args):
    """跑一次 build-farm.sh。★ 显式传 COMPOSE_DIR/FARM，绝不用 NAS 默认路径。

    ★★ 路径要转成 **正斜杠**（`Path.as_posix()`）再给 `sh`：
       Windows 的 `tempfile` 给的是 `C:\\Users\\…`，反斜杠会被 POSIX shell 当转义符，
       `cd`/`find`/`cp` 全解析不到 ⇒ 脚本静默建不出任何东西（我第一版就栽在这儿）。
       Git Bash / MSYS 认得 `C:/Users/…` 这种正斜杠形式。
    """
    env = dict(os.environ)
    env["COMPOSE_DIR"] = ENV.as_posix()
    env["FARM"] = FARM.as_posix()
    env["SUDO"] = ""            # 夹具里不能真 sudo
    env.setdefault("PATH", "/usr/bin:/bin")
    return subprocess.run(
        ["sh", str(SCRIPT), *args], cwd=str(REPO), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=180, env=env,
    )


def farm_files():
    # ★ 正斜杠：Windows 上 `relative_to` 给的是 `PackA\\single\\s.mkv`，而断言用的
    #   字面量是 `PackA/single/s.mkv` —— 不归一化就会"明明建好了却判 False"。
    return sorted(p.relative_to(FARM).as_posix() for p in FARM.rglob("*") if p.is_file())


def ino(p):
    return os.stat(p).st_ino


def reset_farm():
    if FARM.exists():
        shutil.rmtree(FARM)
    FARM.mkdir()


# 探针：本机支持 cp -al 与 find -maxdepth 吗？不支持则相关断言明说"未验"。
def _probe():
    d = LAB / "_probe"
    d.mkdir(exist_ok=True)
    f = d / "x"
    f.write_bytes(b"x")
    ok_cp = subprocess.run(["sh", "-c", f'cp -al "{f}" "{f}.l" 2>/dev/null && '
                                       f'[ "$(stat -c %i "{f}")" = "$(stat -c %i "{f}.l")" ]'],
                           capture_output=True).returncode == 0
    ok_find = subprocess.run(["sh", "-c", f'find "{d}" -mindepth 1 -maxdepth 1'],
                             capture_output=True).returncode == 0
    return ok_cp, ok_find


OK_CP, OK_FIND = _probe()

# --------------------------------------------------------------------------- #
print("== ① 深源整树进农场（含嵌套层），且真硬链 ==")
write_env()
r = run("--apply")
ck_true("apply 退出码 0", r.returncode == 0, r.stderr[:200])
# 深源的**直接子项**是 Arrow.S01-S08（一层）；2 层 = 再进一层到季目录
ck("农场含扁平条目 PackA/single/s.mkv", "PackA/single/s.mkv" in farm_files(), True)
# ★ 季层是**目录** —— 用 exists() 判，别拿 farm_files()（只有文件）比。
ck("农场含深源季层目录 Arrow.S01-S08/Arrow.S01.Bluray（真目录）",
   (FARM / "Arrow.S01-S08" / "Arrow.S01.Bluray").is_dir(), True)
ck("农场含深源季层目录 2 Arrow.S01-S08/Arrow.S02.Bluray（真目录）",
   (FARM / "Arrow.S01-S08" / "Arrow.S02.Bluray").is_dir(), True)
ck("深源 2 层不越界（不含 01.green 本身）", (FARM / "01.green").exists(), False)
if OK_CP:
    ck("嵌套 mkv 是真硬链（inode 相同）",
       ino(DEEP / "Arrow.S01-S08" / "Arrow.S01.Bluray" / "a.mkv"),
       ino(FARM / "Arrow.S01-S08" / "Arrow.S01.Bluray" / "a.mkv"))
    ck("扁平 mkv 也是真硬链",
       ino(SRC / "Flat" / "PackA" / "single" / "s.mkv"),
       ino(FARM / "PackA" / "single" / "s.mkv"))
else:
    print("  ok  (未验) 本机 cp -al 不支持硬链 —— 跳过 inode 断言")

print("== ② 幂等：二次 --apply 不重建（新建=0）==")
r2 = run("--apply")
ck_true("二次 apply 退出码 0", r2.returncode == 0, r2.stderr[:200])
ck_true("二次 apply 新建 = 0", "★ 新建         : 0" in r2.stdout, r2.stdout[-200:])

print("== ③ --verify 在无漂移时退出 0（同深度口径，不假报孤儿）==")
r3 = run("--verify")
ck("无漂移 verify rc", r3.returncode, 0)
ck_true("verify 报『无漂移』", "无漂移" in r3.stdout, r3.stdout[-200:])
ck_true("verify 不报『农场有但源没有』>0", "农场有但源没有 : 0" in r3.stdout, r3.stdout[-300:])

print("== ④a 删除一个被跟踪的嵌套目录 → 漂移 rc=1，N_MISSING 精确 ==")
shutil.rmtree(FARM / "Arrow.S01-S08" / "Arrow.S02.Bluray")
r4 = run("--verify")
ck("删嵌套目录后 verify rc", r4.returncode, 1)
ck_true("报『源有但农场没有 : 1』", "源有但农场没有 : 1" in r4.stdout, r4.stdout[-300:])
ck_true("孤儿明细里没有它（它只是缺、不是孤儿）",
        "Arrow.S01-S08/Arrow.S02.Bluray\n          ←" in r4.stdout,
        "missing detail")

print("== ④b 顶层已存在时，缺失的嵌套项要能**增量自愈**（不能只 continue 跳过）==")
r5 = run("--apply")
ck_true("自愈 apply 退出码 0", r5.returncode == 0, r5.stderr[:200])
ck_true("被删的嵌套季层已补回",
        (FARM / "Arrow.S01-S08" / "Arrow.S02.Bluray" / "c.mkv").exists(),
        str(farm_files()))
r6 = run("--verify")
ck("自愈后 verify rc", r6.returncode, 0)

print("== ⑤ prune 键 = 农场相对路径，且只报被删树的**顶层** ==")
reset_farm()
run("--apply")
# 把深源整棵移走 ⇒ 在盘上留下孤儿树 Arrow.S01-S08（含季层）
moved = SRC / "DC" / "01.green.MOVED"
(DEEP).rename(moved)
r7 = run("--apply", "--prune", "--dry-run")
ck_true("prune 预演报顶层 Arrow.S01-S08",
        "[prune·预演] 将删除: Arrow.S01-S08" in r7.stdout, r7.stdout[-400:])
ck_true("prune 预演**不**逐层报内部季层（不报 Arrow.S01-S08/…）",
        "[prune·预演] 将删除: Arrow.S01-S08/" not in r7.stdout, r7.stdout[-400:])
ck_true("prune 预演里扁平源不受影响（PackA 仍在期望集）",
        "[prune·预演] 将删除: PackA" not in r7.stdout, r7.stdout[-400:])
ck_true("PackA 仍在农场里", (FARM / "PackA" / "single" / "s.mkv").exists())
# 恢复
moved.rename(DEEP)
r7b = run("--verify")
ck("恢复深源后 verify rc", r7b.returncode, 0)

print("== ⑥ 扁平源零 churn：FARM_DEEP_SOURCES 为空时行为与旧脚本一致 ==")
reset_farm()
write_env(deep_line="")
r8 = run("--apply")
ck_true("无深源 apply rc 0", r8.returncode == 0, r8.stderr[:200])
ck_true("无深源时不打『深镜像源』行", "深镜像源" not in r8.stdout, r8.stdout[:300])
ck_true("无深源时不打『含嵌套层』注", "含嵌套层" not in r8.stdout, r8.stdout[-300:])
ck("无深源时农场只有扁平那一层（无 Arrow.S01-S08）",
   (FARM / "Arrow.S01-S08").exists(), False)
ck("无深源时扁平条目仍在", "PackA/single/s.mkv" in farm_files(), True)
r8b = run("--verify")
ck("无深源 verify rc", r8b.returncode, 0)

print("== ⑦ 自指闸对 FARM_DEEP_SOURCES 也生效（含 $FARM 即 die）==")
write_env(deep_line=FARM.as_posix())
r9 = run("--verify")
ck_true("深源 = 农场自身 ⇒ 非 0 退出", r9.returncode != 0, str(r9.returncode))
ck_true("深源自指有明确报错", "就是农场自己" in (r9.stdout + r9.stderr),
        (r9.stdout + r9.stderr)[-300:])

print("== ⑧ 期望集为空时拒绝 prune（安全闸，连深源一起数）==")
reset_farm()
write_env(flat_line="", deep_line="")
# 农场里放一个孤儿，但两个源键都空 —— 会先因"读不到源清单"die，这就是安全闸的第一道
r10 = run("--apply", "--prune")
ck_true("两键皆空 ⇒ die（不静默）", r10.returncode != 0, str(r10.returncode))

print("== ⑨ CRLF：末条行尾 \\r 必须被剥掉（深源是最后一行）==")
reset_farm()
write_env()      # FARM_DEEP_SOURCES 是最后一行 ⇒ 若没 tr -d '\r' 它会被当"源目录缺失"
r11 = run("--apply")
ck_true("CRLF 末条（深源）不被当缺失", "源目录缺失   : 0" in r11.stdout, r11.stdout[-300:])
ck_true("CRLF 下深源仍被镜像",
        (FARM / "Arrow.S01-S08" / "Arrow.S01.Bluray" / "a.mkv").exists(),
        str(farm_files()))

print()
if fails:
    print(f"!!! {len(fails)} 个失败")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("全部通过")
