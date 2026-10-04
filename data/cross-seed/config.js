// =====================================================================
// cross-seed 配置  ——  单种保种（按内容从其它站点匹配单种）
// ---------------------------------------------------------------------
// ⚠️ 版本免责声明（务必读）：
//   cross-seed 各大版本之间，配置键的名字/取值改过多次，例如：
//     - v5 用 linkDir(单个字符串)，v6 改为 linkDirs(数组)；
//     - matchMode 取值集在 v5(safe/risky) 与 v6(strict/flexible/partial) 不同；
//     - webhook 路径/参数、apiKey 生成方式也随版本变化。
//   本文件按"较新版本(v6.x)"书写，且尽量从环境变量取值（见 docker-compose）。
//   部署后请以你实际镜像的官方文档为准：
//     容器内 `cross-seed --help` / `cross-seed gen-config` / 官方 docs 站。
//   若启动报"unknown option"之类，多半是键名随版本变了，按提示改这里即可。
// ---------------------------------------------------------------------
// 值来源：docker-compose.yml 把 .env 的变量注入为环境变量，这里读取它们，
// 以便与编排器 config.yml / .env 单点维护同一批参数（IP、路径、apikey…）。
// =====================================================================

const csv = (s) => (s || "").split(",").map((x) => x.trim()).filter(Boolean);
const bool = (s) => String(s).toLowerCase() === "true";

// =====================================================================
// ★★★ 匹配宽松度 ⇄ 链接类型：一个**结构性互锁**，不是两段各自独立的配置 ★★★
// ---------------------------------------------------------------------
// 背景（2026-09-13 查实的事故，见 README「源文件被写穿」）：
//   linkType=hardlink 时，linkDirs 里的「做种数据」与 dataDirs 里的源**同一个 inode**。
//   于是 matchMode=partial/flexible 放进来一个「名称+大小匹配、但 piece 不一致」的单种后，
//   qB 校验不通过**不会拒绝**，而是**就地重下**那几个 piece —— 这一写直接落到源文件上，
//   无声改写库里的母本，且 Farm 侧仍显示 100%。两处实证时间分秒吻合（哥谭 04421b52 /
//   教父 db7be3ac）。官方文档对 partial 的说法恰好印证：
//     "Nearly all partial matches recheck to 99.9% rather than 100%"
//   —— 上游当预期行为；在硬链接农场里它就是事故。
//
// 所以**能不能放宽匹配，不取决于你想不想，取决于链接是不是 COW 的**：
//   hardlink / symlink → 写穿，只能 strict（本文件强制，忽略 .env）
//   reflink (COW)      → 重下只改副本、源不受影响，才谈得上 partial/flexible
// 这么耦合是**故意的**：把「放宽匹配」的前提写进代码里，以后谁想调 partial，
// 必须先能让 linkType 真的是 reflink，而不是在 .env 里改个值就绕过。
// =====================================================================

function resolveLinkType() {
  const raw = String(process.env.LINK_TYPE || "").trim().toLowerCase();
  const t = raw || "hardlink";
  // ★ reflinkOrCopy 在 reflink 失败时**静默整份拷贝**（上游文档自己也标了 Danger）。
  //   本卷 2026-09-13 实测已用满 100%（仅剩 ~19 GiB）—— 一次静默拷贝就能把注入卡死，
  //   而「静默」意味着你只会看到跨站做种莫名其妙停了。降级为 reflink：不支持就报错。
  if (t === "reflinkorcopy" || t === "reflink_or_copy") {
    console.warn(
      "[config] 拒绝 LINK_TYPE=" + t + "：reflink 失败时它会**静默整份拷贝**，" +
        "而本卷已近满，一份都放不下。改用 reflink（不支持就直接报错，绝不拷贝）。"
    );
    return "reflink";
  }
  if (t === "hardlink" || t === "symlink" || t === "reflink") return t;
  console.warn("[config] 未知 LINK_TYPE=" + raw + "，回退 hardlink（并因此强制 strict）");
  return "hardlink";
}

function resolveMatchMode(linkType) {
  const m = String(process.env.MATCH_MODE || "").trim().toLowerCase() || "partial";
  if (linkType === "hardlink" || linkType === "symlink") {
    if (m !== "strict") {
      console.warn(
        "[config] 忽略 MATCH_MODE=" + m + "：linkType=" + linkType +
          " 时 qB 校验失败会就地重下并**写穿源文件**，已强制 strict。" +
          "要放宽请先把 LINK_TYPE 改成 reflink（需 BTRFS/XFS 支持 COW）。"
      );
    }
    return "strict";
  }
  if (m !== "strict" && m !== "flexible" && m !== "partial") {
    console.warn("[config] 未知 MATCH_MODE=" + m + "，回退 partial");
    return "partial";
  }
  return m;
}

const LINK_TYPE = resolveLinkType();
const MATCH_MODE = resolveMatchMode(LINK_TYPE);
if (LINK_TYPE === "reflink" && MATCH_MODE !== "strict") {
  console.warn(
    "[config] matchMode=" + MATCH_MODE + " + linkType=reflink：不完整匹配会被注入，" +
      "qB 校验不符的 piece 会重下进 **COW 副本**（源文件安全）。" +
      "但分离出来的块占真实空间，本卷已近满 —— 跑起来后留意剩余容量。"
  );
}

module.exports = {
  // --- 站点来源：Prowlarr 的 Torznab feeds（含各站 apikey）---
  torznab: csv(process.env.TORZNAB_URLS),

  // --- 用"我已有的数据"去搜对应单种 ---
  // dataDirs：其"子目录"被当作 searchee。指向大包根目录 → 每部电影=一个 searchee。
  dataDirs: csv(process.env.DATA_DIRS),

  // 命中后在这里按"单种发布名/结构"建链接（做种数据）。
  // v6：linkDirs 为数组；若你的镜像是 v5，改成  linkDir: csv(...)[0]
  linkDirs: csv(process.env.LINK_DIR),
  // ★ 取值 hardlink | symlink | reflink（由上面 resolveLinkType 决定，见那段注释）。
  //   要与 hlink/config.yml 的 matcher.link_type 保持同一个值 —— 那是**另一条**
  //   建链接的路径（orchestrator/hardlink.py::prestage），两边不一致就会一半硬链接。
  linkType: LINK_TYPE,

  // ★ 由 resolveMatchMode 决定：linkType 是 hardlink/symlink 时**强制 strict**，
  //   只有 reflink 才允许 .env 的 MATCH_MODE 生效。理由见文件顶部那段注释。
  matchMode: MATCH_MODE,

  // --- 命中后的动作：注入 qBittorrent ---
  action: "inject",

  // ★★★ v6.13.7 真正的 qB 连接键 = `torrentClients`（带类型前缀的 URL 数组）★★★
  //   2026-10-04（A2）实测：本镜像 v6.13.7 **完全不认 v5 的 `qbittorrentUrl`**——
  //   报 `You need to specify torrentClients when using 'inject'` /
  //   `You need to have a client configured for useClientTorrents`。
  //   格式（`cross-seed gen-config` 原文）：
  //     torrentClients: ["qbittorrent:http://username:password@localhost:8080"]
  //   可选只读标记：`"qbittorrent:readonly:http://user:pass@host:port"`（只当来源、不注入）。
  //   ★ 为什么内嵌账号密码而不是白名单/APIKey：
  //     · cross-seed **只发 login 表单、不支持 `Authorization: Bearer`** ⇒ APIKey 对它无效；
  //     · qB 升到 v5.2.4 后 `AuthSubnetWhitelist` 为空 ⇒ 从 Docker 网 172.x 进来吃 **403**。
  //     ⇒ 不改 qB conf 的唯一出路：URL 内嵌凭据，让它自己登录（实测登录成功回 **HTTP 204**）。
  //   ★ 凭据只来自 .env（gitignore），**绝不进仓库**（仓库要公开）。
  //   ★ 用**服务名** `qbittorrent-reseed` 直连；**别**换成 ${NAS_IP}——
  //     群晖上「容器→宿主机 LAN IP→DNAT 回另一容器」的回环会 timeout（2026-09-11 实测）。
  //   ★ 取值优先级：QBIT_URL_CRED（带凭据） > QBIT_URL（无凭据，回退白名单模式）。
  torrentClients: [
    "qbittorrent:" +
      (process.env.QBIT_URL_CRED || process.env.QBIT_URL || "http://qbittorrent-reseed:3060"),
  ],

  // 注入到统一分类，便于隔离/汇报/清理（cross-seed 会自动创建该分类）
  linkCategory: process.env.QBIT_CATEGORY || "reseed-singles",
  duplicateCategories: false,

  // ★ 校验必开（false）。但**别把 recheck 当安全网** —— 2026-09-13 的教训：
  //   recheck 不通过时 qB 不会拒绝，而是**就地重下**缺失 piece，直接改写源文件。
  //   真正挡住事故的是上面的 matchMode: "strict"（不匹配的根本不注入）。
  //   这个键保持 false，只是为了让万一漏进来的不匹配尽早暴露出来，而不是"修好"它。
  skipRecheck: bool(process.env.SKIP_RECHECK || "false"),

  // --- 守护进程 / API ---
  // 编排器通过 http://cross-seed:2468 调 webhook，用下面的 apiKey 鉴权。
  apiKey: process.env.CROSSSEED_API_KEY || undefined,
  port: 2468,
  host: "0.0.0.0",
  delay: 30, // 每次搜索之间的间隔(秒)，对站点友好、降低被限流风险

  // --- 电影场景的稳妥默认 ---
  includeNonVideos: true,      // 电影目录常含 nfo/字幕/海报，需一并纳入匹配
  includeSingleEpisodes: false, // 电影非剧集
  seasonFromEpisodes: undefined,

  // 中文站过 Cloudflare 时，Prowlarr 侧配置 FlareSolverr 即可，这里通常无需改。

  // 其余高级项（rssCadence、searchCadence、excludeRecentSearch、snatchTimeout…）
  // 按需在官方文档基础上补充。
};
