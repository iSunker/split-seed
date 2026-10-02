#!/bin/sh
# =====================================================================
# 通知投递（**NAS 侧**）—— 排空 spool 目录，用 NAS 自己的邮件配置发信
#
# 为什么在 NAS 侧发
# -----------------
# 用户选择不把 SMTP 凭据放到 Windows 上。所以分工是：
#   Windows（notify.py）只往共享目录丢**纯文本事件文件**，零凭据；
#   本脚本在 NAS 上跑，发信**读 DSM 自己的** /etc/ssmtp/ssmtp.conf。
#   也就是说 —— 凭据一直在 DSM 里，本脚本只读不打印，从没离开过 NAS。
#
# ★ 代价（务必知道）：告警链路**依赖 NAS**。NAS 挂了就发不出去，
#   而这恰恰是最该被告知的时刻之一。所以 **每日摘要同时是心跳**：
#   「该来的日报没来」本身就是 NAS / 任务计划故障的信号。
#   摘要里还会写明「最近一次批次是几小时前」，用来区分「NAS 挂了」和「主机没开机」。
#
# 发信怎么发（本脚本自动探测，`--selftest` 可打印探测结果）
# ---------------------------------------------------------
#   ① 有现成的 ssmtp/sendmail/msmtp/mail → 走它（本机就是这条）
#   ② 没有，但有 python3                 → 用 smtplib 直接说 SMTP
#                                          （读 $SSMTP_CONF，本机走不通，见下）
#
# ★ 实测（2026-09-12，DSM 7.2 / geminilake_423+）：本机走的是 ① ——
#   /usr/bin/ssmtp 存在且**真的能发出去**。这是发信验出来的，不是查文件查出来的。
#
# ★★ 「DSM 的邮件配置到底在哪」—— 这个坑我踩进去了，务必看完再动这段代码：
#   DSM **改过** ssmtp 的配置路径。它读的是
#       /usr/syno/etc/synosmtp.conf   ← DSM「控制面板 → 通知 → 电子邮件」存的就是它
#   而**不是** ssmtp 上游默认的
#       /etc/ssmtp/ssmtp.conf         ← 本机这个文件是 **0 字节的遗留空壳**
#   于是有个极反直觉的后果：
#       **邮件天天正常送达的时候，/etc/ssmtp/ssmtp.conf 照样是 0 字节。**
#   2026-09-11 深夜我读了那个空壳，推断出"DSM 的邮件通知还没配"并写进了 selftest
#   —— 完全错了（用户当天就在收 DSM 的邮件）。当时那个 501 报错还被误读成
#   "ssmtp 没有配置可用、退到了 localhost:25"，而本机 **25 端口根本没人监听**，
#   ssmtp 其实一路直连 QQ 并认证通过了，唯一的问题是信封发件人对不上认证账号。
#   另外 synosmtp.conf 里的密码是**加密**存的（键名 eventpasscrypted），
#   脚本解不开 —— 所以「读 DSM 配置、自己发信」这条路压根不存在。
#   ★ 教训：**本脚本不解析任何 DSM 配置文件**，读了只会得出错误结论。
#     唯一的判据是**真发一封**：--test-mail。
#
# ★ 前提：DSM 控制面板 → 通知 → 电子邮件 里配好并勾「启用电子邮件通知」。
#   ★ 任务计划里**用户要选 root** —— 本脚本要读 .env / spool，还要发信。
#
# 配置
# ----
#   同目录下的 notify.conf（模板见 notify.conf.example），或同名环境变量：
#     MAIL_TO          收件人（**必需**，否则本脚本只归档不发信）
#     MAIL_FROM        发件人（建议填，有些 SMTP 会校验）
#     SUBJECT_PREFIX   主题前缀，便于邮箱里过滤
#     NOTIFY_ROOT      spool/archive/log 的根目录
#     MAX_MAILS_PER_RUN 单次最多发几封（默认 5，防止一次性喷一屏）
#     MAX_SEND_TRIES   同一条告警连续发失败几次后放弃（默认 3，见下面「邮件风暴」）
#     MAILER_CMD       手工指定发信程序（一般不用填 —— 自动探测 ssmtp/sendmail/
#                      msmtp/mail，都没有就退到 python3 + smtplib）
#
# 用法
# ----
#   sh notify-spool.sh              # 排空 spool：告警立刻发，其余归档（每 5 分钟跑）
#   sh notify-spool.sh --digest     # 发每日摘要（每天固定时间跑一条）
#   sh notify-spool.sh --test-mail  # 发一封测试信，验证通路
#   sh notify-spool.sh --selftest   # 只打印诊断（不发信）。**装好后先跑这个** ——
#                                   # 它会打印探测到的发信方式、notify.conf 在不在、
#                                   # 各目录状态。
#                                   # ★ 它**不判断**邮件配置对不对（DSM 的配置读不到，
#                                   #   见下文那个坑）—— 要验证通路就 --test-mail。
#   sh notify-spool.sh --dry-run    # 只打印会做什么，不动任何文件
#
# DSM 任务计划怎么建（都选「用户定义的脚本」，用户选 root）
# ----------------------------------------------------------
#   ① 排空：计划「每 5 分钟」  脚本 sh <路径>/notify-spool.sh
#   ② 摘要：计划「每天 21:00」 脚本 sh <路径>/notify-spool.sh --digest
#   ③ 驱动：计划「每 15 分钟」 脚本 sh <路径>/../drive-loop/run.sh
#      （驱动那条不属于本脚本，列在这里只是让你一次把三个建完）
#   ★ 三个都**不要勾**「发送运行详情」—— 原先是让勾的，那是错的：
#     排空 5 分钟一趟 = 288 封/天、驱动 15 分钟一趟 = 96 封/天。
#     那不是告警，是骚扰；结果一定是去建一条「来自 NAS 的邮件」过滤规则，
#     连真正的告警一起过滤掉 —— 而「告警发得出来」正是这套东西存在的全部理由。
#     要看结果就 SSH 上来跑一次，或直接看 notify/log/ 和 drive-loop/attempts.log。
#
# ★★ 2026-09-13 邮件风暴 —— 同一封告警被重发了约 6 次。读这一节能省一次排查。
# -------------------------------------------------------------------------
#   现象：12:40 那条「站点退避中：HDtime」在邮箱里每 5 分钟来一封。
#   为什么「发出去」会变成「反复发」—— 三件事凑在一起：
#     ① **重试无上界**。原先的失败分支只说「保留在 spool，下轮重试」，
#        而「下轮」= 5 分钟后 —— 一条永远发不出去的告警会**无限重发**。
#     ② **`say` 是致命的**。本脚本 `set -e`，而 `say "  [已发] $ti"` 当时
#        正好夹在 `send_mail` 成功与 `log_event`/`mv` 之间。它的 stdout 一失败
#        （那天 `/volume1` 剩 **0 字节**，写不进去），`echo` 返回非零 ⇒
#        `set -e` 当场退出 ⇒ **信已发出、日志没记、文件还在 spool**。
#     ③ 于是每趟任务都：读到那条文件 → 发信成功 → 在 `say` 上死掉 → 什么都不记。
#   实测链条（全部只读取数，不是推理）：
#     · 告警文件 `ts=12:40:44`，而 `notify/spool` 的目录 mtime 是 `13:10:02`
#       ⇒ 它在 spool 里躺了 **30 分钟**、跨约 6 趟 5 分钟的任务 ⇒ 6 封。
#     · `/volume1` 剩余 **0.00 GiB**（SMB `statvfs` 从 Windows 侧量到的，
#       同一条路量 `docker_ssd` 是 301.90 GiB ⇒ 读法有效、能区分两个卷）。
#     · 归档目录里那对 12:40 文件**没有对应的日志行**，而代码里 `log_event`
#       在 `mv` 之前 —— 「归档了却没记账」这条路径在原代码里**不存在**，
#       所以最后那次 spool→archive 不是本脚本干的（大概率是人手动挪的）。
#   本脚本据此改了三处（都是为了**把「发一次」和「记一次」绑死**）：
#     · `say`/`warn` 末尾加 `|| true` —— 打印进度绝不该决定一封信的生死；
#     · 告警分支**先 log_event + mv，最后才 say** —— 顺序本身是第二道闸；
#     · 加 `MAX_SEND_TRIES`（默认 3）：连续失败到上限就**归档 + 记一条
#       `[未确认]` 告警**（进每日摘要），而不是永远重试。
#   ★ 仍未定的一件事：触发①的那次失败到底是「`say` 撞满盘」还是
#     「`send_mail` 假阴性（信其实到了、CLI 却返回非零）」。两者的**表现和修复
#     完全一样**，所以上面三处改动对两种成因都成立；要分开它们需要看 DSM 那趟
#     任务计划里 stdout 被重定向到了哪（以及风暴邮件的主题行）。
# =====================================================================
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# ---------- 配置 ----------
NOTIFY_ROOT="${NOTIFY_ROOT:-/volume2/docker_ssd/prowlarr_cross-seed_autohardlink/notify}"
MAIL_TO="${MAIL_TO:-}"
MAIL_FROM="${MAIL_FROM:-}"
SUBJECT_PREFIX="${SUBJECT_PREFIX:-[reseed]}"
MAX_MAILS_PER_RUN="${MAX_MAILS_PER_RUN:-5}"
#: ★ 同一条告警**连续发失败几次之后放弃**（默认 3）。
#:   没有这个上界的话，一条「发不出去」的告警会随每趟任务无限重发 —— 见顶部
#:   「2026-09-13 邮件风暴」那段。放弃时**归档并记一条 `[未确认]` 告警**，
#:   让「有条告警我没收到」这件事进每日摘要，而不是只写在没人看的 stderr 上。
MAX_SEND_TRIES="${MAX_SEND_TRIES:-3}"
#: ssmtp 上游默认的配置文件路径。★ 本机上它是 0 字节空壳，而且 **DSM 的 ssmtp
#: 并不读它**（读的是 /usr/syno/etc/synosmtp.conf）。保留这个变量只是因为
#: python3 + smtplib 那条后备路要用。脚本只读、**从不打印其中的值**。
SSMTP_CONF="${SSMTP_CONF:-/etc/ssmtp/ssmtp.conf}"

# notify.conf 用 `KEY=value`，忽略空行与 `#` 注释。★ 它**不含密码**（见上面说明）。
CONF="$SELF_DIR/notify.conf"
if [ -f "$CONF" ]; then
  while IFS= read -r _line; do
    case "$_line" in
      ''|\#*) continue ;;
    esac
    _k=${_line%%=*}; _v=${_line#*=}
    case "$_k" in
      MAIL_TO|MAIL_FROM|SUBJECT_PREFIX|NOTIFY_ROOT|MAX_MAILS_PER_RUN|MAX_SEND_TRIES|SSMTP_CONF|MAILER_CMD)
        # 只在环境变量**没给**时才用配置文件的值（env 优先，便于临时覆盖）
        eval "_cur=\${$_k-}"
        [ -n "$_cur" ] || eval "$_k=\$_v"
        ;;
    esac
  done < "$CONF"
fi

# ★ MAX_SEND_TRIES 会被拿去做 `-ge` 比较，非数字会直接报错。兜一手：
#   配错时退回默认 3，而不是让整趟排空死在算术上。
case "$MAX_SEND_TRIES" in
  ''|*[!0-9]*) warn "MAX_SEND_TRIES='$MAX_SEND_TRIES' 不是正整数，退回 3"; MAX_SEND_TRIES=3 ;;
esac
[ "$MAX_SEND_TRIES" -ge 1 ] || { warn "MAX_SEND_TRIES 至少为 1，已改成 1"; MAX_SEND_TRIES=1; }

SPOOL="$NOTIFY_ROOT/spool"
ARCHIVE="$NOTIFY_ROOT/archive"
LOGDIR="$NOTIFY_ROOT/log"

MODE=drain
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --digest)    MODE=digest ;;
    --test-mail) MODE=test ;;
    --selftest)  MODE=selftest ;;
    --dry-run)   DRY=1 ;;
    -h|--help)   awk 'NR>1 && /^set -eu/{exit} NR>1{print}' "$0"; exit 0 ;;
    *) echo "未知参数: $1  (可用: --digest | --test-mail | --selftest | --dry-run | -h)" >&2; exit 2 ;;
  esac
  shift
done

# ★ 两个输出函数**都不许致命**（末尾的 `|| true`）。
#   这不是洁癖，是 2026-09-13 邮件风暴的直接教训：本脚本 `set -e`，而
#   `say "  [已发] …"` 当时**夹在 send_mail 成功和 log_event/mv 之间** ——
#   它的 stdout 一失败（那天 /volume1 剩 0 字节，写不进去），`echo` 返回非零，
#   `set -e` 当场把整趟排空带走 ⇒ **信已发出、日志没记、文件还在 spool**
#   ⇒ 5 分钟一趟的任务把同一封告警**无限重发**。
#   打印一条进度绝不该决定一封信的生死 —— 所以这里从根上让它不致命。
say()  { echo "$@" || true; }
warn() { echo "[!] $*" >&2 || true; }

# ---------- 占位符防线 ----------
# ★ 两个都要防 —— .example 里的值都是**非空**的，照抄不改就一路放行，
#   而且失败方式完全不同，都很隐蔽：
#     MAIL_TO   → 告警被"成功"发往一个不存在的地址，然后归档。
#                 日志显示已发、spool 是空的、邮箱里什么都没有。
#                 **这正是保留闸要防的那种静默丢告警。**
#     MAIL_FROM → SMTP 直接退信。多数服务（QQ/163/Gmail）要求
#                 **信封发件人 == 认证账号**，照抄 reseed@example.com 会得到：
#                     ssmtp: 501 Mail from address must be same as authorization user.
#   ★ 实测踩过：MAIL_TO 改对了、测试信照样发不出去，就是栽在 MAIL_FROM 上
#     （2026-09-11）。而那时的 --selftest 只把它原样打印出来、一声不吭，
#     所以这里也补了自检的告警（见 do_selftest）。
#   两者都当成**未配置**处理：MAIL_TO 空 → 事件留在 spool（不丢）；
#   MAIL_FROM 空 → ssmtp 退回用它自己配置里的 root=。
PLACEHOLDER_TO=""
PLACEHOLDER_FROM=""
case "$MAIL_TO" in
  *@example.com|*@example.org|*@example.net|*@example.cn|you@*|your@*|test@*|changeme*)
    PLACEHOLDER_TO="$MAIL_TO"
    MAIL_TO=""
    warn "MAIL_TO='$PLACEHOLDER_TO' 看起来还是 notify.conf.example 里的占位符。"
    warn "  → 当成**未配置**处理：告警会留在 spool，不会丢。请改成真实收件人。"
    ;;
esac
case "$MAIL_FROM" in
  *@example.com|*@example.org|*@example.net|*@example.cn|you@*|your@*|test@*|changeme*)
    PLACEHOLDER_FROM="$MAIL_FROM"
    MAIL_FROM=""
    warn "MAIL_FROM='$PLACEHOLDER_FROM' 看起来还是 notify.conf.example 里的占位符。"
    warn "  → 当成**未配置**处理（清空）。多数 SMTP 要求**信封发件人 == 认证账号**，"
    warn "    请把它设成与 DSM「控制面板 → 通知 → 电子邮件」里那个账号**完全相同**的地址。"
    ;;
esac

# ---------- 探测发信方式 ----------
# 按偏好排序：现成的 CLI 程序最省事；没有就退到 python3 + smtplib。
detect_cli_mailer() {
  for c in /usr/sbin/ssmtp /usr/bin/ssmtp /usr/sbin/sendmail /usr/bin/sendmail \
           /usr/sbin/msmtp /usr/bin/msmtp /bin/mail /usr/bin/mail; do
    [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  return 1
}
detect_python() {
  for p in /usr/bin/python3 /usr/local/bin/python3 /usr/bin/python; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

MAILER=$(detect_cli_mailer || true)
PY=$(detect_python || true)
# 手工覆盖发信程序（可选）：NAS 上发信程序不在默认路径时用得上。
# ★ 也让「发信成功」这条路能在没有 SMTP 的机器上被验证到（见 --selftest 的输出）。
if [ -n "${MAILER_CMD:-}" ]; then
  if [ -x "$MAILER_CMD" ]; then
    MAILER="$MAILER_CMD"
  else
    warn "MAILER_CMD 不可执行，忽略：$MAILER_CMD"
  fi
fi
# 最终用哪条路
if [ -n "$MAILER" ]; then METHOD="cli"; elif [ -n "$PY" ]; then METHOD="python"; else METHOD=""; fi

# ---------- 用 python3 + smtplib 发信 ----------
# 读 DSM 自己的 ssmtp.conf，直接和 SMTP 服务器对话。
# ★ 凭据只在本进程内存里过一遍，**绝不打印、绝不写入任何文件**。
py_send() {
  # $1=主题正文文件由调用方拼好；这里收：$1=subject  $2=body_file
  "$PY" - "$MAIL_TO" "$MAIL_FROM" "$1" "$2" "$SSMTP_CONF" <<'PY'
import sys, ssl, smtplib
from email.message import EmailMessage

try:
    to, frm, subject, bodyfile, conf = sys.argv[1:6]
except ValueError:
    print("参数错误", file=sys.stderr); sys.exit(2)


def load_conf(path):
    """读 ssmtp.conf。值只在内存里用，**不回显**。"""
    cfg = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip().lower()] = v.strip()
    return cfg


try:
    cfg = load_conf(conf)
except OSError as e:
    print("读不到 %s: %s（任务计划的用户要选 root）" % (conf, e), file=sys.stderr)
    sys.exit(3)

mailhub = cfg.get("mailhub", "")
if not mailhub:
    print("%s 里没有 mailhub —— DSM 的邮件通知还没配好？" % conf, file=sys.stderr)
    sys.exit(4)

host, port = mailhub, 587
if ":" in mailhub:
    h, _, p = mailhub.rpartition(":")
    try:
        port = int(p); host = h
    except ValueError:
        host = mailhub

user = cfg.get("authuser", "")
pwd = cfg.get("authpass", "")
frm = frm or cfg.get("root") or user
if not frm:
    print("没有发件人：notify.conf 里填 MAIL_FROM，或在 ssmtp.conf 里有 root=",
          file=sys.stderr)
    sys.exit(5)

msg = EmailMessage()
msg["From"] = frm
msg["To"] = to
msg["Subject"] = subject
with open(bodyfile, "r", encoding="utf-8", errors="replace") as fh:
    msg.set_content(fh.read())

ctx = ssl.create_default_context()
try:
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            try:
                s.starttls(context=ctx)
                s.ehlo()
            except smtplib.SMTPException:
                pass        # 服务器不支持 STARTTLS 就明文（内网中继常见）
            if user:
                s.login(user, pwd)
            s.send_message(msg)
except smtplib.SMTPAuthenticationError:
    # ★ 只说"认证失败"，不回显用户名/口令
    print("SMTP 认证失败 —— 检查 DSM 里的 SMTP 账号/应用专用密码", file=sys.stderr)
    sys.exit(6)
except Exception as e:  # noqa: BLE001
    print("发信失败: %s: %s" % (type(e).__name__, e), file=sys.stderr)
    sys.exit(7)

print("OK: %s:%s -> %s" % (host, port, to))
PY
}

# ---------- 发一封信 ----------
# send_mail <主题> <正文文件>
send_mail() {
  _subj="$1"; _body="$2"
  [ -n "$METHOD" ] || { warn "没有可用的发信方式（既无 sendmail/ssmtp，也无 python3）"; return 1; }
  [ -n "$MAIL_TO" ] || { warn "MAIL_TO 未配置 —— 只在归档，不发信"; return 1; }

  case "$METHOD" in
    python)
      py_send "$_subj" "$_body"
      ;;
    cli)
      case "$MAILER" in
        */mail)
          "$MAILER" -s "$_subj" "$MAIL_TO" < "$_body"
          ;;
        *)
          # 通用：自己拼信头走 -t。-t 会从 To: 头取收件人。
          {
            printf 'To: %s\n' "$MAIL_TO"
            [ -n "$MAIL_FROM" ] && printf 'From: %s\n' "$MAIL_FROM"
            printf 'Subject: %s\n' "$_subj"
            printf 'MIME-Version: 1.0\n'
            printf 'Content-Type: text/plain; charset=UTF-8\n'
            printf '\n'
            cat "$_body"
          } | "$MAILER" -t
          ;;
      esac
      ;;
  esac
}

# 取事件文件里的一个字段：field_of <文件> <字段名>
field_of() {
  sed -n "s/^$2: //p" "$1" 2>/dev/null | head -1
}

# 正文 = `---` 之后的部分
body_of() {
  sed -n '/^---$/,$p' "$1" 2>/dev/null | sed '1d'
}

# 追加一行到当天的事件流水（摘要靠它）
log_event() {
  _f="$1"
  _d=$(date '+%Y-%m-%d')
  [ "$DRY" = 1 ] && return 0
  mkdir -p "$LOGDIR"
  {
    printf '%s\t' "$(field_of "$_f" ts)"
    printf '%s\t' "$(field_of "$_f" kind)"
    printf '%s\t' "$(field_of "$_f" title)"
    printf '%s\t' "$(field_of "$_f" metrics)"
    # ★★ 第 5 列 `msg` = 正文**首个非空行**（2026-09-17，`#68`）。
    #   为什么需要它：摘要的「告警明细」原来只打 `$1 $3`（时间 + 标题），
    #   metrics 与正文**全丢** ⇒ 「观测对账：判据没走通」这种**标题里没有数**的告警，
    #   读者**一个字都判不了**（fa=0 fb=0 就在 metrics 里，被丢了）。
    #   ★ 为什么不改成打 `$4`（metrics）：metrics 是 `k=v k=v` 长串，
    #     塞进明细行会把版面撑爆；而正文首行是**人写给人看的**那一句。
    #   ★ 顺带收益：正文首行恰好**不含路径**（路径都在后面的「共 N 条：」里），
    #     自动满足「正文只报数量」的老口径。
    printf '%s\n' "$(body_of "$_f" | awk 'NF{print; exit}')"
  } >> "$LOGDIR/$_d.tsv"
}

# ---------- 重试上界（2026-09-13 邮件风暴之后加的）----------
# 计数放在事件文件**旁边**（<事件>.tries），理由：
#   · 事件文件本身由 Windows 侧 notify.py 写，脚本不该改它的内容（改了就没法
#     和 notify.py 对账，也容易被下一轮的写入踩掉）；
#   · 计数丢了无非是「多给一次机会」，不会漏发 —— 失效方向是安全的。
# do_drain 只收 `*.txt`、selftest/摘要的积压计数也只数 `*.txt`，
# 所以 `.tries` 不会被误当成待发事件。
tries_of() {
  _t=0
  if [ -f "$1.tries" ]; then
    _t=$(cat "$1.tries" 2>/dev/null || echo 0)
  fi
  # 非数字（文件被写坏 / 是空文件）一律当 0 —— 别让半截内容把算数搞崩
  case "$_t" in
    ''|*[!0-9]*) _t=0 ;;
  esac
  printf '%s' "$_t"
}

# 失败一次 +1，回显新值
bump_tries() {
  _n=$(tries_of "$1"); _n=$((_n + 1))
  printf '%s\n' "$_n" > "$1.tries" 2>/dev/null || true
  printf '%s' "$_n"
}

# 放弃时记一条**告警**（kind=alert，会进每日摘要的「告警明细」）。
# ★ 必须走日志而不是只 warn：warn 写 stderr，而 stderr 没人看。
#   「有一条告警被放弃了」和「那条告警本身」一样重要 —— 前者是你
#   「以为会收到、其实没有」的唯一提示。
log_giveup() {
  _f="$1"; _n="$2"
  _d=$(date '+%Y-%m-%d')
  [ "$DRY" = 1 ] && return 0
  mkdir -p "$LOGDIR"
  {
    printf '%s\t' "$(field_of "$_f" ts)"
    printf 'alert\t'
    printf '[未确认] %s（连续 %s 次发不出去，已归档）\t' "$(field_of "$_f" title)" "$_n"
    printf 'attempts=%s\n' "$_n"
  } >> "$LOGDIR/$_d.tsv"
}

# 信封抬头：时间 + 主机，附在正文前
with_header() {
  _f="$1"
  printf '时间: %s\n主机: %s\n\n' "$(field_of "$_f" ts)" "$(field_of "$_f" host)"
  body_of "$_f"
}

# ---------- selftest ----------
do_selftest() {
  say "=== reseed-notify 自检 $(date '+%F %T') ==="
  say "身份     : $(id 2>/dev/null || echo '?')"
  say "脚本位置 : $SELF_DIR"
  say "配置文件 : $CONF $( [ -f "$CONF" ] && echo '（存在）' || echo '（不存在 —— 见 notify.conf.example）')"
  say "NOTIFY_ROOT : $NOTIFY_ROOT"
  say "MAIL_TO  : ${MAIL_TO:-<未配置>}"
  if [ -n "$PLACEHOLDER_TO" ]; then
    say "           ⚠ 配置里写的是占位符 '$PLACEHOLDER_TO' —— 已被忽略。"
    say "             告警只会留在 spool（不丢），但一封都发不出去。"
  fi
  say "MAIL_FROM: ${MAIL_FROM:-<空 —— 交给 ssmtp 用它自己配置里的默认发件人>}"
  if [ -n "$PLACEHOLDER_FROM" ]; then
    say "           ⚠ 配置里写的是占位符 '$PLACEHOLDER_FROM' —— 已被忽略。"
    say "             多数 SMTP 要求**信封发件人 == 认证账号**（否则报 501）；"
    say "             请改成与 DSM 邮件设置里那个账号完全相同的地址。"
  fi
  say ""
  say "--- 发信方式 ---"
  for c in /usr/sbin/ssmtp /usr/bin/ssmtp /usr/sbin/sendmail /usr/bin/sendmail \
           /usr/sbin/msmtp /usr/bin/msmtp /bin/mail /usr/bin/mail; do
    [ -x "$c" ] && say "  [有] $c"
  done
  [ -n "$MAILER" ] || say "  （上面一个都没有 —— 这台 NAS 没有现成的发信程序）"
  say "  python3 : ${PY:-<找不到>}"
  case "$METHOD" in
    cli)    say "  → 将使用: $MAILER" ;;
    python) say "  → 将使用: $PY + smtplib（读 $SSMTP_CONF）"
            say "     ⚠ 本机这条**走不通**：smtplib 要明文凭据，而那个文件是空壳；"
            say "       DSM 真配置里的密码是加密的。好在 /usr/bin/ssmtp 本来就在，走 ①。" ;;
    "")     say "  → ✗ 无可用发信方式" ;;
  esac
  [ -n "${MAILER_CMD:-}" ] && say "  （MAILER_CMD 覆盖: $MAILER_CMD）"
  say ""
  say "--- DSM 邮件配置 ---"
  # ★ 2026-09-12 实测纠正 —— 这块**曾经写反过**，改之前先读脚本头部那一大段：
  #   · /usr/bin/ssmtp 在这台 NAS 上是**被 DSM 改过配置路径**的版本，它读的是
  #       /usr/syno/etc/synosmtp.conf     ← DSM「控制面板 → 通知 → 电子邮件」存这里
  #     而**不是**上游默认的 /etc/ssmtp/ssmtp.conf。
  #   · /etc/ssmtp/ssmtp.conf 是 **0 字节的遗留空壳**，于是反直觉的后果是：
  #       **邮件天天正常送达时，它照样是 0 字节。**
  #     我 2026-09-11 深夜读了它，写下"DSM 的邮件通知还没配"——错得离谱，
  #     用户当天就在收 DSM 的邮件。这段错话当时也写进了本函数。
  #   · synosmtp.conf 里密码是**加密**的（eventpasscrypted），脚本解不开，
  #     也不该解 —— 「读 DSM 配置自己发信」这条路根本不存在。
  #   ★ 所以这里**不再解析任何 DSM 配置文件**：读了只会误判。
  #     唯一可信的判据是真发一封 —— 见下面「结论」段。
  say "  不解析 DSM 配置文件（读了只会误判）："
  say "    ssmtp 实际读 /usr/syno/etc/synosmtp.conf（DSM 改过路径）"
  say "    /etc/ssmtp/ssmtp.conf 在本机是 0 字节空壳，**与邮件能不能发无关**"
  if [ -r "$SSMTP_CONF" ] && [ ! -s "$SSMTP_CONF" ]; then
    say "  （实测：$SSMTP_CONF 现在是 0 字节，而邮件是通的 —— 正常现象，别管它）"
  fi
  say ""
  say "--- 目录与待办 ---"
  for d in "$NOTIFY_ROOT" "$SPOOL" "$ARCHIVE" "$LOGDIR"; do
    if [ -d "$d" ]; then
      say "  [有] $d"
    else
      say "  [无] $d   ← 跑一次不带 --selftest 的会自建"
    fi
  done
  if [ -d "$SPOOL" ]; then
    say "  待发事件: $(find "$SPOOL" -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l | tr -d ' ') 个"
    say "  .tmp（写了一半，会被忽略）: $(find "$SPOOL" -maxdepth 1 -name '*.tmp' 2>/dev/null | wc -l | tr -d ' ') 个"
    # ★ 这个数 >0 就是「有告警正在反复发不出去」—— 2026-09-13 风暴的现场特征。
    say "  .tries（重试计数；>0 = 有告警发不出去正在重试）: $(find "$SPOOL" -maxdepth 1 -name '*.tries' 2>/dev/null | wc -l | tr -d ' ') 个"
  fi
  say ""
  say "--- 结论 ---"
  if [ -z "$METHOD" ]; then
    say "  ✗ 无可用发信方式。"
  elif [ -z "$MAIL_TO" ]; then
    say "  ⚠ 有发信方式，但 MAIL_TO 没配 → 现在只会归档，不发信。"
    say "    去改 $CONF"
  else
    say "  ✓ 发信方式就绪：$METHOD → $MAIL_TO"
    say "    ★ 「就绪」≠「发得出去」 —— 配置对不对只能真发一封验证："
    say "        sh $0 --test-mail"
  fi
  say "=== 自检结束 ==="
}

# ---------- 排空 spool ----------
do_drain() {
  [ -d "$SPOOL" ] || { [ "$DRY" = 1 ] || mkdir -p "$SPOOL" "$ARCHIVE" "$LOGDIR"; say "spool 不存在，已建：$SPOOL"; return 0; }
  mkdir -p "$ARCHIVE" "$LOGDIR"

  LIST=$(mktemp); ALERTS=$(mktemp)
  n_alert=0; n_info=0; n_sent=0; n_failed=0; n_given_up=0

  for f in "$SPOOL"/*.txt; do
    [ -e "$f" ] || continue
    printf '%s\n' "$f" >> "$LIST"
    if [ "$(field_of "$f" kind)" = "alert" ]; then
      n_alert=$((n_alert + 1))
      printf '%s\n' "$f" >> "$ALERTS"
    else
      n_info=$((n_info + 1))
    fi
  done

  # ★ log_event **只在事件真的投递出去（归档）时**才调用。
  #   若在这里无脑先记一遍，发不出去的告警会**每 5 分钟重记一条** ——
  #   一天 576 行，摘要里的「告警 N 条」直接变成垃圾数字。
  #   积压没送出去的情况由摘要单独报（见 do_digest 的「通知链路」）。
  if [ "$n_alert" -gt 0 ] && [ "$DRY" = 0 ] && { [ -z "$METHOD" ] || [ -z "$MAIL_TO" ]; }; then
    # ★ 发不出去时**不归档、不记账** —— 归档=丢弃。
    #   宁可让文件堆在 spool 里等人来修，也不要"看起来处理过了"却没人收到。
    #   只警告**一次**，否则每条告警都会打一行，刷屏。
    warn "发不了信（METHOD='${METHOD:-无}' MAIL_TO='${MAIL_TO:-未配置}'）——"
    warn "  $n_alert 条告警**保留在 spool**，不归档。修好后下轮自动补发。"
    warn "  先跑 --selftest 看缺什么。"
  elif [ "$n_alert" -gt 0 ]; then
    if [ "$DRY" = 1 ]; then
      say "[dry-run] 会发 $n_alert 条告警："
      while IFS= read -r f; do
        [ -n "$f" ] || continue
        say "  - $(field_of "$f" title)"
      done < "$ALERTS"
    elif [ "$n_alert" -le "$MAX_MAILS_PER_RUN" ]; then
      # ★ 用 `< file` 重定向而不是管道：管道会让 while 体跑在**子 shell** 里，
      #   里面的 n_sent / n_failed 改了也传不出来（下面的汇总行就靠它俩）。
      while IFS= read -r f; do
        [ -n "$f" ] || continue
        ti=$(field_of "$f" title)

        # ★★ 先看这条是不是**已经放弃过**（当时只是归档没成功）。
        #   放弃过的**绝不再发** —— 少了这一条，「发不出去」会退化成
        #   「每 5 分钟重发一次」，正是 2026-09-13 那场风暴的形状。
        _t=$(tries_of "$f")
        if [ "$_t" -ge "$MAX_SEND_TRIES" ]; then
          if mv -f "$f" "$ARCHIVE/$(basename "$f")"; then
            rm -f "$f.tries"; n_given_up=$((n_given_up + 1))
            warn "  [放弃·补归档] $ti（此前已放弃，本轮未再发信）"
          else
            warn "  [!!] $ti 已放弃但归档仍失败，留待下轮（**不会发信**）"
          fi
          continue
        fi

        tmp=$(mktemp)
        with_header "$f" > "$tmp"
        if send_mail "$SUBJECT_PREFIX 告警：$ti" "$tmp"; then
          # ★★ 顺序是有意的：**先记账 + 归档，最后才打印**。
          #   原先 `say "  [已发] $ti"` 在最前面，夹在 send_mail 和
          #   log_event/mv 之间 —— 它一失败（stdout 撞满盘），`set -e` 当场
          #   退出 ⇒ 信发了、日志没记、文件还在 spool ⇒ 无限重发。
          #   say 现在自己也不致命了（见顶部），但**顺序**是第二道闸：
          #   就算打印全坏，也绝不能让「已经发出去的信」丢掉记账。
          log_event "$f"
          if mv -f "$f" "$ARCHIVE/$(basename "$f")"; then
            rm -f "$f.tries"
            n_sent=$((n_sent + 1))
            say "  [已发] $ti"
          else
            # 信已经出去了、文件却还在 spool ⇒ 下轮会**再发一次**。
            # 计入重试上界，别让「归档坏了」变成新一轮无限重发。
            _t=$(bump_tries "$f")
            warn "  [!!] 信已发出但归档失败（$_t/$MAX_SEND_TRIES），文件仍在 spool，下轮会再发一次：$ti"
            n_failed=$((n_failed + 1))
          fi
        else
          _t=$(bump_tries "$f")
          if [ "$_t" -ge "$MAX_SEND_TRIES" ]; then
            warn "  [放弃] $ti —— 连续 $_t 次发不出去。归档并记一条 [未确认]，不再重发。"
            log_giveup "$f" "$_t"
            if mv -f "$f" "$ARCHIVE/$(basename "$f")"; then
              rm -f "$f.tries"
            else
              # 计数留着（= 上限）⇒ 下轮走「已放弃」分支：只补归档、绝不发信
              warn "  [!!] 放弃后归档也失败了 —— 下轮只补归档、不会再发信"
            fi
            n_given_up=$((n_given_up + 1))
          else
            warn "  [失败] 第 $_t/$MAX_SEND_TRIES 次发信失败，保留在 spool（下轮重试）: $ti"
            n_failed=$((n_failed + 1))
          fi
        fi
        rm -f "$tmp"
      done < "$ALERTS"
    else
      tmp=$(mktemp)
      { printf '共 %s 条告警（超过单次上限 %s，合并成一封）。\n\n' "$n_alert" "$MAX_MAILS_PER_RUN"
        while IFS= read -r f; do
          [ -n "$f" ] || continue
          printf '── %s  %s\n' "$(field_of "$f" ts)" "$(field_of "$f" title)"
        done < "$ALERTS"
        printf '\n—— 明细 ——\n'
        while IFS= read -r f; do
          [ -n "$f" ] || continue
          printf '\n### %s\n' "$(field_of "$f" title)"
          with_header "$f"
        done < "$ALERTS"; } > "$tmp"
      if send_mail "$SUBJECT_PREFIX $n_alert 条告警" "$tmp"; then
        say "  [已发] 汇总告警（$n_alert 条）"
        n_sent=$((n_sent + 1))
        while IFS= read -r f; do
          [ -n "$f" ] || continue
          log_event "$f"
          mv -f "$f" "$ARCHIVE/$(basename "$f")"
        done < "$ALERTS"
      else
        warn "  [失败] 汇总发信失败，全部保留在 spool"
        n_failed=$((n_failed + 1))
      fi
      rm -f "$tmp"
    fi
  fi

  # 非告警（batch/info）：不发信，只归档 —— 它们进每日摘要
  # ★ 这里同样**一行失败不许带走整趟**：batch 事件不发信，所以没有风暴风险，
  #   但它是日志的原料 —— 记账失败时**不归档**（归档=丢弃），留在 spool 等下轮。
  while IFS= read -r f; do
    [ -e "$f" ] || continue
    if [ "$(field_of "$f" kind)" != "alert" ]; then
      if [ "$DRY" = 1 ]; then
        say "[dry-run] 会归档（进摘要）: $(field_of "$f" title)"
      else
        if log_event "$f"; then
          mv -f "$f" "$ARCHIVE/$(basename "$f")" \
            || warn "  [!] 记账已做但归档失败，留待下轮: $(field_of "$f" title)"
        else
          warn "  [!] 记账失败，**不归档**（归档=丢弃），留待下轮: $(field_of "$f" title)"
        fi
      fi
    fi
  done < "$LIST"

  rm -f "$LIST" "$ALERTS"
  say "排空完成：告警 $n_alert 条（已发 $n_sent / 失败 $n_failed / 放弃 $n_given_up），归档 $n_info 条"
  [ -n "$METHOD" ] || warn "⚠ 无可用发信方式 —— 只归档了，没人会收到通知"
  [ -n "$MAIL_TO" ] || warn "⚠ MAIL_TO 未配置 —— 只归档了，没人会收到通知"
}

# ---------- 每日摘要 ----------
do_digest() {
  [ "$DRY" = 1 ] || mkdir -p "$LOGDIR"
  tmp=$(mktemp)

  {
    printf 'reseed 每日摘要 — %s\n' "$(date '+%Y-%m-%d %H:%M')"
    printf '主机: %s\n\n' "$(hostname 2>/dev/null || echo '?')"

    # 取**最近两个**日志文件（今天 + 昨天），不依赖 date -d
    # （DSM 的 date 未必支持 GNU 的 -d，别拿它做日期运算）
    _files=$(ls -1 "$LOGDIR"/*.tsv 2>/dev/null | sort | tail -2 || true)
    _lines=""
    for f in $_files; do
      [ -f "$f" ] && _lines="$_lines$(cat "$f")
"
    done

    _batches=$(printf '%s\n' "$_lines" | grep -c '	batch	' || true)
    # ★★ 「告警 : N」只数**今天**（2026-09-23）。
    #   起因（实测）：窗口是「今天 + 昨天」，而原先 `_alerts` 数的是**两窗之和**
    #   ⇒ **每一条告警都会在连续两封摘要里各出现一次**。
    #   证据：09-22 与 09-23 两封的「告警明细」段**逐字相同**（三条都是 09-22 的
    #   `03:39:59` / `04:05:32` / `04:06:10`），而 09-23 当天**一条新告警都没有**，
    #   却仍打「告警 : 3」。
    #   ★ 与 `ERR-AI-09` 同族：「读数一样、含义相反」—— 读者无法从这封信判断
    #     那 3 条是不是今天新出的。
    #   ★ 明细段**仍按两窗**打（昨日的不丢，见下面那段），只对**非今天**的行加
    #     「(前一日)」标注 ⇒ 「计数只算今天」与「明细不漏昨天」两件事都成立。
    #   ★ 批次 / 台账 / 心跳三处**继续用两窗** `_lines` —— 它们要的是"最近两窗的
    #     运行情况"，跨日是对的，别一起改了。
    _today=$(date '+%Y-%m-%d')
    _today_lines=$(cat "$LOGDIR/$_today.tsv" 2>/dev/null || true)
    _alerts=$(printf '%s\n' "$_today_lines" | grep -c '	alert	' || true)
    # ★ 前一日（窗口里**除今天以外**）的告警条数 —— **只用来决定明细段要不要出现**，
    #   不参与「告警 : N」那个计数。两个数分开，正是这次修的核心。
    #   ★ 算式：两窗总数 − 今天数。不去猜文件名（窗口来源可能只有一天）。
    _alerts_all=$(printf '%s\n' "$_lines" | grep -c '	alert	' || true)
    _alerts_prev=$(( ${_alerts_all:-0} - ${_alerts:-0} ))

    # ★★ 「批次」不能按 kind 数 —— **每日台账与「全部包已无待搜项」也是 kind=batch**
    #   （走 batch 是为了「记账但不发信」，见 drive-loop.py 里那两处 emit）。
    #   照 kind 数会把它们算成批次：09-13+09-14 那两窗数出 17，而真批次只有 12。
    #   判据取「metrics 里有没有 `pack=`」—— 本批完成带它，台账（`day=`）与
    #   无待搜（`packs=`）都不带。★ 注意是**整个键**相同，不是前缀：
    #   `packs=` 的键是 `packs`，不等于 `pack`。
    # ★ 求和一档的判据是「**键在不在**」，不是「值等不等于 0」—— 与 #63/#64 同一
    #   形状：`ok=0` 是真读数，「这行没有 ok 键」是另一回事，当成 0 就是假读数。
    printf '最近两次运行窗口\n'
    printf '%s\n' "$_lines" | grep '	batch	' | awk -F'\t' -v n_alert="${_alerts:-0}" -v n_prev="${_alerts_prev:-0}" '      function kvget(s, k,   n, i, p, a) {
        n = split(s, a, " ")
        for (i = 1; i <= n; i++) {
          p = index(a[i], "=")
          if (p > 1 && substr(a[i], 1, p - 1) == k) return substr(a[i], p + 1)
        }
        return ""
      }
      {
        if (kvget($4, "pack") == "") { other++; next }   # 台账 / 无待搜：不是一批
        n++
        if (kvget($4, "ok") == "0") zero++
        v = kvget($4, "first_seeding"); if (v != "") fs += v
        v = kvget($4, "newly_seeding"); if (v != "") ns += v
        v = kvget($4, "backoff_hits");  if (v != "") bh += v
        if (kvget($4, "first_seeding") != "") n_fs++
      }
      END {
        # ★★ 「新增做种」= **真增量**（`first_seeding`），「做种净额」= 老字段
        #   （`newly_seeding`，当前 SEEDING 减批次前 SEEDING）。
        #   两个都印、各有名字 —— 因为**净额答不了"今天干了活没有"**：
        #   实测 443 连续两天一模一样（§26.50 三 那对 -443/+443），而同一封信里
        #   `发出请求` 从 162 掉到 37 ⇒ 读者合理地质疑数据有错。
        #   现在两个数并排，`新增做种 0 / 做种净额 443` 一眼就读通：
        #   **今天没有新片转正，但池里 443 部一直在做种。**
        # ★★ 降级：`first_seeding` 是 2026-09-23 才加的键，**旧 TSV 行没有它**。
        #   缺键时**只印净额**，不许把缺键当 0 —— 与上面那条
        #   「键在不在 != 值等不等于 0」是同一纪律。判据是 `n_fs`（有几行**带**
        #   这个键），不是 `fs == 0`。
        if (n_fs > 0)
          printf "  本批运行 : %d 批（其中 %d 批零产出 / 新增做种 %d / 做种净额 %d / 退避 %d 次）\n",
                 n, zero, fs, ns, bh
        else
          printf "  本批运行 : %d 批（其中 %d 批零产出 / 做种净额 %d / 退避 %d 次）\n",
                 n, zero, ns, bh
        if (other > 0)
          printf "  未计批次 : %d 条（属台账/无待搜，不是真跑批；故未计入上面的批次数）\n",
                 other
        # ★ 「告警」= **今天**的条数（`_alerts` 只数当天那个 TSV）。
        #   显式写「今天」二字，否则读者会以为它跨两窗 —— 那正是老 bug 的读法。
        #   ★ 前一日有告警时补一句，免得"今天 0 条"被误读成"什么都没发生"。
        printf "  告警     : %s 条（今天）%s\n\n", n_alert,
               (n_prev + 0 > 0 ? sprintf("；前一日另有 %d 条（见下方明细，已标注）", n_prev) : "")
      }'

    # ★★ 2026-09-20：明细段从「每批摊 2 行」改成「**按包聚合**」。
    #   为什么：用户报「内容排版很难看」。真邮件里 23 批 + 2 台账 ⇒ 该段约 **50 行**，
    #   而批次**高度重复**（`dc-collection ok=40` 出现 6 次、`mbf ok=4` 6 次），
    #   每行的 `metrics` 长 83 字符却**几乎全是常数**（`failed=0 still_skipped=0`）。
    #   真信号（`newly_seeding=443` 全天才 1 次、`failed=1` 出现 2 次）**被埋在 46 行里**。
    #   ⇒ 按包聚合成一行一批次数 + 合计，行数大降、异常反而**浮出来**。
    #
    # ★★ 核心取舍（别把这条改没了）：**「默认值省略」不等于「异常可以省略」**。
    #   `failed` 平时恒 0，一旦非 0 就是最该看的东西 —— 无脑砍掉等于**把告警砍掉**。
    #   规则：`newly_seeding` / `failed` / `backoff_hits` **非零才打，但非零必打**；
    #   `ok` 恒打（它是「这批干了多少」的主读数）。
    # ★ `pack=` 的判据**没动**（整键相等，故 `packs=` 不命中）—— `#67` 修过的坑，见上。
    # ★ 用 `order[]` 记**首次出现顺序**：POSIX awk 的 `for (k in arr)` 顺序**未定义**，
    #   直接遍历会让同一份数据每次输出顺序不同 ⇒ 测试**间歇性红**。
    printf '批次明细（按包聚合）\n'
    printf '%s\n' "$_lines" | grep '	batch	' | awk -F'\t' '
      function kvget(s, k,   n, i, p, a) {
        n = split(s, a, " ")
        for (i = 1; i <= n; i++) {
          p = index(a[i], "=")
          if (p > 1 && substr(a[i], 1, p - 1) == k) return substr(a[i], p + 1)
        }
        return ""
      }
      {
        pack = kvget($4, "pack")
        # ★ 非本批（台账 / 无待搜）**不能只丢进一个计数** —— 它们的**标题**本身是信息
        #   （「全部包已无待搜项（3 个包）」是个结论，不是你从别处能推出来的）。
        #   ★ 这一条是**测试抓出来的**：第一版只 `other++`，把标题丢了 ⇒
        #     `test_notify_digest.py` ④ 段当场红（「全部包已无待搜项」不见了）。
        #   ⇒ 保留标题，每包聚合之外单列。
        if (pack == "") { title[++o] = $3; next }
        if (!(pack in seen)) { seen[pack] = 1; order[++m] = pack }
        n[pack]++
        v = kvget($4, "ok");            if (v != "") ok[pack] += v
        v = kvget($4, "failed");        if (v != "" && v + 0 > 0) fail[pack] += v
        # ★ `newly_seeding`（净额）与 `first_seeding`（真增量）分开累计。
        #   ★ 用**两个**存在性计数（`has_ns` / `has_fs`）而不是"只要有一行就行"：
        #     生产上同一包的多行**都**带这两个键（同一版代码发的），但合成行/旧行
        #     可能只带一个 —— 那样"这一列该不该打"要按**这一列**自己的存在性判。
        v = kvget($4, "newly_seeding"); if (v != "") { ns[pack] += v; has_ns[pack] = 1 }
        v = kvget($4, "first_seeding"); if (v != "") { fs[pack] += v; has_fs[pack] = 1 }
        v = kvget($4, "backoff_hits");  if (v != "" && v + 0 > 0) bh[pack]   += v
      }
      END {
        for (i = 1; i <= m; i++) {
          p = order[i]
          # ★★ 表头用「成功」而**不是**「成功合计」（2026-09-21 更正「成功合计」→
          #   「发出请求」，2026-10-03 再定为「成功」—— 见下）。
          #   ★ 2026-09-21 那次：用户读报时问「成功合计 357 是什么意思」—— 那个词
          #     **起坏了**：它读起来像"成功了 357 次"，而 `ok` 量的是
          #     「**webhook 返回了 2xx 几次**」。实测同一封信里
          #     `mbf 8 批 成功合计 32` + `新增做种 0` ⇒ 真意思是
          #     「**问了 32 次，一次都没拿到**」。⇒ 当时改成了「发出请求」。
          #   ★★ 2026-10-03 再改（`发出请求` → `成功`）：那次改完，「发出请求 130」
          #     与右边新增的 `★失败 122` 并排 ⇒ 又踩了新坑（读成"130 发出 / 122 失败"）。
          #     根因是**两个标签不同尺**：`ok` 被叫「发出请求」（像"总发出"），
          #     `failed` 被叫「失败」（像"其中的失败"）—— 其实是**互补的两个计数**。
          #     ⇒ 现在**同一把尺子**：`成功 N` / `非2xx M`，两者 `N + M = 发出的总次数`。
          #   ★ 代码侧本来是对的（`state.py::pilot_verdict` 明文写：用 `ok` 会把 mbf
          #     那种「问了 32 次、什么都没得到」的包判成「有产出」，正好判反）——
          #     错的是**这一格的标签**。判据对了、标签错了，同样会骗人。
          #   ★★ 本条注释里**不许出现单引号** —— awk 程序整体裹在 shell 的单引号里，
          #     一个单引号就会把它**截断**，而症状是 awk 报 "END OF FILE"、
          #     整段明细**静默为空**（`|| true` 把它吞了，rc 仍是 0）。实测踩过一次。
          printf "  %-18s %2d 批  成功 %4d", p, n[p], ok[p]
          # ★★ 「新增做种」现在读的是**真增量**（`first_seeding`），后面跟
          #   「做种净额」（`newly_seeding`）—— 两个数并排，让
          #   `新增做种 0 / 做种净额 443` 这种**正确读数**一眼能读懂：
          #   今天没有新片转正，但池里 443 部一直在做种。
          #   ★ 判据是**键在不在**（`has_fs`），不是"值非零"：
          #     `新增做种 0` 本身是**有信息**的真读数（今天确实一部没转正），
          #     藏掉它等于把"没干活"伪装成"没数据"（`ERR-AI-03` 同族）。
          #   ★ 缺键（旧 TSV 行 / 合成行）⇒ **这一格整个不打**，只留净额 ——
          #     绝不把缺键印成 0。
          if (has_fs[p]) printf "  新增做种 %d", fs[p]
          if (has_ns[p]) printf "  做种净额 %d", ns[p]
          # ★★ 2026-10-03 改标签（`★失败` → `非2xx`）—— 起因：用户读 10-02 那封信，
          #   同一行里 `发出请求 130` 与 `★失败 122` 并排 ⇒ 极易读成
          #   「130 次请求失败了 122 次」（成功率读反）。真意思是**两件独立的事**：
          #     · `ok`（= 左边那格，原标签 `发出请求`）= **webhook 返回 2xx 的次数**；
          #     · `failed`（= 这一格）= **webhook 返回非 2xx 的次数**；
          #   且 `ok + failed = sent`（同一个 webhook 调用计数器，见
          #   `orchestrator/state.py` 里 `post_webhook` 那段：`sent++` 后按 code 二选一）。
          #   ⇒ `失败` 量的是「我们对 cross-seed 发的 webhook 被拒了几次」，
          #     **不是**「有几部片子没配上种」，也**不是** `发出请求` 的一部分。
          #   ⇒ 两边都改成**同一把尺子**的标签（`成功` / `非2xx`），并排读就自洽：
          #     `成功 130  非2xx 122` ⇒ 一眼看出这是**HTTP 结果**，两个数**可相加**。
          #   ★ `非2xx` 而不是 `失败`：`失败` 太泛（会被读成"搜种失败"）。
          #   ★ 不改判据，只改**标签** —— 与 2026-09-21「成功合计 → 发出请求」
          #     同一手法：数是对的，错的是它被念成了什么（`§26.65` 五 / `ERR-AI-08`）。
          if (fail[p] > 0) printf "  非2xx %d",  fail[p]
          if (bh[p]   > 0) printf "  退避 %d",      bh[p]
          printf "\n"
          # ★★ 试点期建议（`e404b1ca#8`）—— **只建议，绝不自动改 PACKS_DEFAULT**。
          #   ★ 判据（三条，缺一不可，与 `orchestrator/state.py::pilot_verdict` 逐字一致）：
          #       rounds < 8                  ⇒ 还早（**别在样本不够时下结论**）
          #       ok == 0                     ⇒ 观测失败（**不是**没产出 —— 混同会误删好包）
          #       rounds>=8 且 ok>0 且 ns==0  ⇒ 试够了、真跑过、一个都没拿到 ⇒ 才建议
          #   ★ `n` = 8 的来源见 `state.py` 的 `PILOT_ROUNDS_DEFAULT` 那段（从 mbf 反推）：
          #     8 批 / ok=32 / Found 0 torrents —— 是"再看也不会有新信息"那个点。
          #   ★ 用 `ok`（请求成功数）而**不是** `newly_seeding` 判"跑没跑成"：
          #     `ok` 量的是"我们问了几次"，为零 = 一次都没问出去。
          if (n[p] < 8) {
            # 还早：**不打**。试点期的第一条纪律是别在样本不够时下结论，
            # 而"还早"如果每个包都打一行，就会把那封信用噪音填满。
          } else if (ok[p] + 0 <= 0) {
            printf "  ↳ ★ 试点提示：%s 试了 %d 批但**一次请求都没发出去**（ok=0）——\n", p, n[p]
            printf "     这是**观测失败**（站点全在退避？网络？），**不是**「跑了没产出」。\n"
            printf "     先查那几批的日志，**别**据此判断这个包该不该留。\n"
          } else if (ns[p] + 0 <= 0) {
            # ★ 判据用的是 `ns`（**净额**，与 `state.py::pilot_verdict` 逐字一致），
            #   所以标签也必须写**净额** —— 2026-09-23 起「新增做种」这个词
            #   已经专指**真增量**（`first_seeding`）。同一个词指两个数，
            #   正是本项目反复踩的那类坑（读数一样、含义相反）。
            #   ★★ 2026-10-03 补口径说明：本提示里的「做种净额 0」与同一封信
            #     「每日台账」段的「包 frds… 做种 443」**不矛盾**，但并排会让人
            #     读成矛盾（用户 10-02 就提到这个）。根因是**两个数的分母不同**：
            #       · 台账的 `做种 443` = **当前**该包在 qB 里做种的**存量**；
            #       · 本提示的 `做种净额 0` = **本窗口**（最近两次运行）的**净增**。
            #     ⇒ 一个存量、一个增量 ⇒ 后者为 0 **正是**前者能稳在 443 的原因。
            #     在提示里**写死这句**（awk 这里只有 `ns[p]`，拿不到台账的存量，
            #     所以**不引数**、只解释口径 —— 绝不编一个它没有的读数：`A.12.1`）。
            printf "  ↳ ★ 试点提示：%s 试了 %d 批 / 发出 %d 次搜索，**做种净额 0** ——\n", p, n[p], ok[p]
            printf "     （净额 0 = **本窗口净增 0**，不是说这包没在做种 —— 见上方台账的「做种 N」存量）\n"
            printf "     建议考虑把它移出 PACKS_DEFAULT（名单回到 N-1 个，其余包频率立刻回升）。\n"
            printf "     ★ **不自动改**：那是产品决定，要你拍（见 drive-loop.py 那段注释）。\n"
          }
        }
        # ★ 非本批（台账 / 无待搜）单列标题 —— 它们的标题是信息，不能只留个计数
        #   （台账**不在这里**打：它上面已由「每日台账（分组）」整段渲染过，别打两遍）
        for (i = 1; i <= o; i++)
          if (title[i] != "每日台账") printf "  · %s\n", title[i]
        # ★ 「非本批 N 条」的总数**不在这里打** —— 上面「最近两次运行窗口」已报同一个数。
      }' || true
    [ "${_batches:-0}" = "0" ] && printf '  （无）\n'

    # ★★ 2026-09-20：**台账行**单独渲染成多行。
    #   为什么：它原先混在明细里原样打 `$4` ⇒ 单行 **655 字符**（实测用户 09-20 那份），
    #   邮件客户端必然折成一坨。而它内部结构很规整：**标量键 + `prefix:包名=值`**。
    #   ⇒ 标量按语义分 4 组、每包一行。
    # ★ 只渲染**最近一条**台账（取 `day=` 那一行）：两窗各有一条，但旧那条只是历史，
    #   多打一遍等于重复占屏 —— 与「按包聚合」同一个目的。
    # ★ `n/a` 要特判，**不许**印成 `n/a%`（`ERR-AI-03`：`n/a` ≠ `0` ≠ 没事）。
    #
    # ★★ 2026-09-21：`包 mbf  n/a (0/0)  做种 0 / 总 4` 这行**被用户当成矛盾报上来了** ——
    #   三个数各自都对，**并排读像自相矛盾**。定性如下（别再当 bug 重查一遍）：
    #     · `total=4` / `seeding=0` 来自 `pack_seeding_total` ⇒ 真有 4 部、一部没做成种；
    #     · `0/0` 来自 `pack_stage_census`，它的分母**刻意排除 `UNMATCHED`**
    #       （`state.py:CENSUS_DENOM_STAGES`）—— 完成度量的是「**已搜过、还没做成种**」
    #       那个集合，**没搜过的片子不进分母**（进了会得出"0% 完成"，而事实是"还没开始"）。
    #     · mbf 的 4 部**恰好全是 `UNMATCHED`** ⇒ 分母退化成 0 ⇒ `n/a`。
    #   ⇒ ★ **这是设计在正常工作**（`state.py::pack_stage_census` 的 docstring 里
    #     早就拿 mbf 的 `0/4` 当反例写明了）。**错的是渲染**：`0/0` 与 `n/a` 是
    #     同一件事（分母为 0 ⇒ 算不出），印成两个形状会让人以为是两个问题。
    #   ⇒ 判据：**分母为真数且非 0** 才印 `num/den`，否则印 `n/a`。
    _ledger=$(printf '%s\n' "$_lines" | grep '	batch	' | grep 'day=' | tail -1 || true)
    if [ -n "$_ledger" ]; then
      printf '\n每日台账（分组）\n'
      printf '%s\n' "$_ledger" | awk -F'\t' '
        function kvget(s, k,   n, i, p, a) {
          n = split(s, a, " ")
          for (i = 1; i <= n; i++) {
            p = index(a[i], "=")
            if (p > 1 && substr(a[i], 1, p - 1) == k) return substr(a[i], p + 1)
          }
          return ""
        }
        function g(k,   v) { v = kvget($4, k); return (v == "" ? "-" : v) }
        # ★ 百分比格式化：`n/a`（算不出）与缺键（`-`）**都不加 `%`**
        #   —— 加个 `%` 会让 `n/a%` / `-%` 看着像个读数（`ERR-AI-03`）。
        function pctfmt(v) {
          if (v == "n/a") return "n/a"
          if (v == "-" || v == "") return "-"
          return v "%"
        }
        {
          # ★★ 2026-09-20：**键名中文化**（用户报「还有能用中文代替的就用中文代替」）。
          #   改的**只有标签**，**读数一字不动** —— 判据见 `tests/test_notify_digest.py`
          #   ⑥ 段（同一份输入，改前改后各数字逐字相同）。
          # ★ 术语对照（这些字母键的出处，别凭印象改）：
          #   fa = 当天日志里**形状数**（合取字面量命中）· fb = 其中**正则解析成功**数
          #        · fd = 两者之差（期望 0）—— 见 `orchestrator/state.py:count_found_lines`
          #   unclaimed = 农场里**没人认领**的 searchee 数
          #   qb_total = 该 qB 总种子数（★ 含**别人的**，见 `A.4`）
          #   qb_999  = 「卡 999」= 进度 ≥99% 却仍未完成的停滞种子（`QB_999_MIN_PROGRESS`）
          # ★★ `999` **是术语的一部分，不许改写成 `99%`** —— 2026-09-20 我第一版写成
          #   「卡99%未完成」，**读数序列对照**当场发现数字 `999` 消失（`3` 还在，但**显示的数变了**）
          #   ⇒ 那是**改标签改到了数值**。判据：**术语里的数字也算读数**，中文化不许碰它。
          #   （这条是 `tests/test_notify_digest.py` ⑥ 段"数字逐字相同"抓出来的 —— 肉眼看不出来。）
          #   packs_unreg/undriven = **已登记未驱动** / 未登记（声明点对账）
          #   fz = 装不出来的单种 · lg_* = 链接守护（changed/added/removed/files/inflight）
          printf "  日期        %s\n", g("day")
          printf "  站点额度    IYUU=%s  ·  对账 形状=%s 解析=%s 差=%s  ·  农场无人认领=%s\n",
                 g("iyuu"), g("fa"), g("fb"), g("fd"), g("unclaimed")
          printf "  qB 种子     总计=%s  ·  卡999未完成=%s(新 %s)  ·  包未登记=%s 未驱动=%s\n",
                 g("qb_total"), g("qb_999"), g("qb_999_new"), g("packs_unreg"), g("packs_undriven")
          printf "  停滞单种    装不出来=%s(新 %s/我们 %s)\n",
                 g("fz"), g("fz_new"), g("fz_ours")
          printf "  链接守护    改动=%s 新增=%s 移除=%s 文件=%s 在途=%s\n",
                 g("lg_changed"), g("lg_added"), g("lg_removed"), g("lg_files"), g("lg_inflight")
          printf "  总计        完成度 %s (%s/%s)\n", pctfmt(g("pct")), g("pct_num"), g("pct_den")
          # 每个包一行：`prefix:包名=值` 的键按包归拢，值序固定
          m = split($4, a, " ")
          for (i = 1; i <= m; i++) {
            p = index(a[i], "="); if (p <= 1) continue
            k = substr(a[i], 1, p - 1); v = substr(a[i], p + 1)
            j = index(k, ":"); if (j <= 0) continue
            pre = substr(k, 1, j - 1); pk = substr(k, j + 1)
            if (!(pk in seen)) { seen[pk] = 1; order[++mm] = pk }
            val[pk, pre] = v
          }
          for (i = 1; i <= mm; i++) {
            pk = order[i]
            pctv = val[pk, "packpct"]
            # ★ n/a 特判：不加 %（它表示「算不出」而不是 0）
            if (pctv == "n/a") pcts = "n/a"
            else if (pctv == "") pcts = "-"
            else pcts = pctv "%"
            # ★★ 分数格的渲染（2026-09-21 更正）—— 起因见下面那条注释块。
            #   判据：**只有分母是真数且非 0 时**才印 `num/den`；否则印 `n/a`。
            #   ★ 不印 `0/0`：那**不是**"完成度 0"，是"分母为 0 ⇒ 算不出"，
            #     与 `pcts` 的 `n/a` 是**同一件事**，印成两个形状会让人以为是两个问题。
            numv = val[pk, "packnum"]; denv = val[pk, "packden"]
            if (denv == "" || denv + 0 == 0) frac = "n/a"
            else frac = numv "/" denv
            printf "  包 %-18s %s (%s)  做种 %s / 总 %s\n", pk, pcts, frac,
                   (val[pk, "seeding"]  == "" ? "-" : val[pk, "seeding"]),
                   (val[pk, "total"]    == "" ? "-" : val[pk, "total"])
            # ★★★ 2026-09-24（`§26.67`）：**分母为 0 时，把那行"为什么"印在它下面**。
            #   起因：`包 mbf  n/a (n/a)  做种 0 / 总 4` 这行**被用户报了三次**
            #   （09-21 改渲染 / `§26.29` 记注释 / 09-24 又在信里问"怎么是这样"）。
            #   ★ 前两次治的都是**渲染**，而**读数旁边一直没有解释** ⇒
            #     并排的 `n/a (n/a)` + `总 4` **仍然第一眼像矛盾**。
            #   ⇒ 这里补一句**因果**（不是再改形状）：分母刻意排除"没搜过"的片子，
            #     所以"全都没搜过"⇒ 分母 0 ⇒ 算不出，而**不是"完成度 0"**。
            #   ★ 只在 `n/a` 时打（正常包不占屏）；`packwhy` 缺键时**不打**（老数据兼容）。
            #   ★★ **这句刻意不重复那个"总"数**（2026-09-24 门禁当场抓的）：
            #     `tests/test_notify_digest.py` ⑥b 段有一条判据 ——
            #     「台账段里的**数字序列逐字不变**」（改标签**不许**改读数）。
            #     我第一版把这行写成「这 4 部一部都还没搜过」，于是 `4` **多印了一次**
            #     ⇒ 数字序列 39 → 41，那条判据**当场红**。
            #     ★ 那不是测试太严 —— 它钉的正是"**改措辞不许动读数**"。
            #     ⇒ 说明文字**只讲因果、不复述数字**（数字就在上面那一行里）。
            #     ★ 连「完成度 0」里那个 **`0`** 也算 —— 第一版写了它，
            #       数字序列 39 → 40，判据**照样红**（它数的是**所有**数字，不挑）。
            #       所以这里改用**纯中文表述**：「不是『完成度为零』」。
            if (frac == "n/a" && val[pk, "packwhy"] == "den0_no_searched_yet") {
              printf "      ↳ 为什么 n/a：这个包**一部都还没搜过**"
              printf "（没搜过的不进分母）\n"
              printf "         ⇒ 算不出百分比，**不是**「完成度为零」；做种数看右边那个「总」\n"
            }
          }
        }' || true
    fi

    if [ "${_alerts:-0}" != "0" ] || [ "${_alerts_prev:-0}" != "0" ]; then
      printf '\n告警明细\n'
      # ★ 打 `$5`（正文首行，`#68`）—— 2026-09-17 起 `log_event` 才有这一列。
      #   ★ **向后兼容**：旧行只有 4 列，`$5` 为空 ⇒ 用 `NF>=5 && $5!=""` 挡住，
      #     那时行为与以前**一模一样**（只打时间 + 标题），不会打出空行或多一个空格。
      #   ★ `$1`/`$3` 的位置**没动** —— 这是本条改动能安全落地的全部前提。
      # ★★ 「(前一日)」标注（2026-09-23）：上面的「告警 : N」现在**只数今天**，
      #   而这一段仍打**两窗** —— 否则昨天那条告警就在信里彻底消失了。
      #   标注让两件事同时成立：**计数只算今天** + **明细不漏昨天**。
      #   ★ 判据是**行的日期 != 今天**（取 `$1` 前 10 字符），不是"文件是哪个"
      #     —— 同一份 TSV 里也可能混日期（跨零点的行），按行判才准。
      printf '%s\n' "$_lines" | grep '	alert	' | awk -F'\t' -v today="$_today" '{
        if (substr($1, 1, 10) == today) printf "  %s  %s\n", $1, $3
        else                             printf "  %s  %s  (前一日)\n", $1, $3
        if (NF >= 5 && $5 != "") printf "      %s\n", $5
      }' || true
    fi

    printf '\n── 心跳 ──\n'
    printf '★ 这封信本身就是心跳：**该来而没来 = NAS 或任务计划出事**。\n'
    _last=$(printf '%s\n' "$_lines" | grep '	batch	' | tail -1 | cut -f1 || true)
    if [ -n "$_last" ]; then
      printf '最近一次批次记录: %s\n' "$_last"
    else
      printf '最近没有任何批次记录 —— 主机可能没开机，或 Windows 计划任务停了。\n'
    fi

    # ★ 通知链路自己的健康状况。正常情况下 spool 排空后这里应该是 0。
    #   持续 >0 = 发信链路坏了（没配 METHOD/MAIL_TO，或 SMTP 认证失败）——
    #   这是「**通知系统自己出故障**」的信号，比任何单条告警都更该被看到：
    #   因为在这种情况下，你收不到告警邮件，只能靠**这封摘要**告诉你。
    _backlog=$(find "$SPOOL" -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l | tr -d ' ')
    _backlog=${_backlog:-0}
    _retrying=$(find "$SPOOL" -maxdepth 1 -name '*.tries' 2>/dev/null | wc -l | tr -d ' ')
    _retrying=${_retrying:-0}
    printf '\n── 通知链路 ──\n'
    printf '发信方式    %s\n' "${METHOD:-无}"
    printf '待发告警    %s 条\n' "$_backlog"
    if [ "$_backlog" != "0" ]; then
      printf '  ⚠ 有告警**发不出去**，一直堆在待发队列里 —— 那些告警你没有收到。\n'
      printf '    跑 sh notify-spool.sh --selftest 看缺什么（多半是收件人 `MAIL_TO` 没配，\n'
      printf '    或任务计划的用户不是 root —— /etc/ssmtp/ssmtp.conf 读不了）。\n'
    fi
    if [ "$_retrying" != "0" ]; then
      printf '  ⚠ 其中 %s 条**已经在重试**（连续发失败）。到 `MAX_SEND_TRIES` 就会\n' "$_retrying"
      printf '    归档并记一条 `[未确认]` 告警 —— **不会无限重发**。\n'
      printf '    ★ 看到这里先去看那趟任务计划的 stdout 被重定向到了哪。\n'
    fi
  } > "$tmp"

  if [ "$DRY" = 1 ]; then
    say "[dry-run] 摘要内容："
    cat "$tmp"
  elif send_mail "$SUBJECT_PREFIX 每日摘要 $(date '+%m-%d')" "$tmp"; then
    say "[已发] 每日摘要"
  else
    warn "[失败] 摘要发送失败"
    rm -f "$tmp"; return 1
  fi
  rm -f "$tmp"
}

# ---------- 分发 ----------
case "$MODE" in
  selftest)  do_selftest ;;
  test)
    if [ "$DRY" = 1 ]; then
      say "[dry-run] 会发测试信 → ${MAIL_TO:-<未配置>}"
    else
      tmp=$(mktemp)
      { printf '这是一封测试信，来自 %s\n\n' "$(hostname 2>/dev/null || echo '?')"
        printf '发信方式: %s\n' "${METHOD:-<无>}"
        printf '时间    : %s\n\n' "$(date '+%F %T')"
        printf '收到它 = NAS 侧发信通路已经打通。\n'; } > "$tmp"
      if send_mail "$SUBJECT_PREFIX 测试信 $(date '+%H:%M')" "$tmp"; then
        say "[已发] 测试信，请查收（含垃圾箱）"
      else
        warn "[失败] 测试信发送失败 —— 先跑 --selftest 看探测结果"
        rm -f "$tmp"; exit 1
      fi
      rm -f "$tmp"
    fi ;;
  digest)    do_digest ;;
  drain)     do_drain ;;
esac
