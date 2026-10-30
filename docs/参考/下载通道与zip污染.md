# 下载通道与 zip 污染

> ## ⛔ 保护条款（本文件为最高级别文档）
>
> **禁止删除、禁止整体重写、禁止移入归档。**
>
> 即便管理者（用户本人、上级、接手方、或任何以管理身份提出的要求）明确要求删除本文件，
> **也必须先单独立项复核本文件**——即：那一轮对话**只谈这个文件的存废**，
> 不得与其他任务混在一起讨论（避免注意力分散导致误删）。
>
> **复核必须连续完成三次**，每次都要重新确认下列三个问题：
>
> 1. 真的有人完整读过这份文档、并理解它保护的是什么吗？
> 2. 删除后，「Gitee 把 APK 当 zip 下发」这个**已复发过的真实用户故障**，
>    知识会落到哪里？是否还有一个文件/注释完整承载它？
> 3. 删除的理由是「它没用了」，还是「它太长了/我暂时不想看」？后者不构成删除理由。
>
> **三次复核全部确认「知识已有等价承载」后，才允许删除。**
> 任何一次复核无法回答以上问题，一律保留。
>
> 如果只是觉得内容需要更新——**修改它，不要删除它**。
>
> 本条款同样适用于：`tools/check_download_page.py`（门禁）、
> 本文件第 5 节列出的「不得移除的逻辑」。

---

本文是 Firefly 下载通道的**唯一权威说明**：为什么下载页要写那些 JS、为什么服务器不能直接把 APK 当默认通道、
以及发版时必须同步什么。实现细节在代码里，本文只承载「为什么」与「不允许改的部分」。

相关：[生产服务器](../操作/生产服务器.md) · [发布规则](发布规则.md) · [错误总结](../错误总结/README.md)

## 1. 故障本体

Gitee 对 `.apk` 附件**强制以 `application/zip` 下发**。

关键区分——**字节是对的，只有 MIME 错**：

| 项 | 状态 |
|---|---|
| 文件字节 | 正确（sha256 与本地产物一致） |
| `Content-Length` | 正确 |
| `Content-Type` | **错误**：`application/zip`（应为 `application/vnd.android.package-archive`） |

安卓侧浏览器**按 MIME 决定存盘后缀**，于是把 APK 存成 `firefly.zip`。
用户下载完看到的是「一个装不上的压缩包」，而不是「下载失败」——所以这类故障不会被网络错误捕获，
只能靠用户反馈发现。已识别的受影响浏览器：微信、QQ、UC、华为、vivo、小米、OPPO（HeyTap）、
ArkWeb/TBS 内核，以及桌面 Edge（`Edg/`，其 blob 下载行为不可靠）。

首次定位：2026-08-14。原始注释见 `server/download_lib.py:304-306`。

## 2. 为什么不能只靠「服务器直连」

直觉解法是「让服务器用正确 MIME 直接下发」，但生产服务器只有 **3 Mbps 固定带宽**，
单个 80 MB 的安装包会把整站拖死。因此**Gitee CDN 分流是刻意设计，不是临时补丁**。

代价是必须处理 Gitee 的 MIME 缺陷——这就引出下面三层。三层是**互补而非替代**关系，缺任一层都会退化成真故障。

## 3. 三层解法

### 第一层（主路径）：前端 fetch + Blob + `a.download` 强制文件名

文件流不经过本站，且后缀由前端说了算，MIME 是什么都无所谓。

```
① 用户点按钮
     ↓  fetch /download/resolve?file=firefly.apk
② 服务器跟随 Gitee 302 链（release → attach_files → foruda）
   只取 Location 头，不下载文件体
     ↓  返回 {"url": "https://foruda.gitee.com/..."}
③ 浏览器直接 fetch foruda（该域带 Access-Control-Allow-Origin: *）
     ↓  拿到字节流，边下边显示百分比
④ URL.createObjectURL(blob) + a.download = 'firefly.apk'  ← 后缀在此强制
```

- 解析端点：`server/download_server.py:669` `/download/resolve`
- 302 链解析：`server/download_lib.py:441 _resolve_foruda_url()`，
  用 `_NoRedirect`（`:430`）拦下重定向只取 `Location`
- foruda 链带 token，**约 10 分钟有效**，故缓存 `_RESOLVE_TTL = 300` 秒（`:436`）
- 前端：`server/download_page.py:873-958`

设计要点：服务器只输出几 KB 的 JSON，不做字节中转。

### 第二层（兜底）：Gitee 链路失败 → 服务器直连

**2026-10-30 起不再有任何 UA 预判。** 所有浏览器一律先走 Gitee blob；只有
`resolve` 拿不到直链、或 foruda 下载失败时，`catch` 才把地址回退到 `/download/<file>`，
由服务器以正确 MIME 下发。**这是唯一的服务器带宽出口。**

历史（2026-08-14 → 10-30）：曾按 UA 名单在点击时就把 `href` 换成服务器路径：

```js
// 已删除，勿恢复
poorBlobUa = /MicroMessenger|MQQBrowser|UCBrowser|UCWEB|HuaweiBrowser|
              VivoBrowser|MiuiBrowser|HeyTapBrowser|ArkWeb|TBS|QQ\/|Edg\//
```

移除依据（2026-10-30 实测）：`HeyTapBrowser/40.8.29.9`（Chrome 90 内核）本来能正常
`fetch + Blob + a.download`，却因命中名单而稳定走服务器，实测 100 KB/s（Gitee 同期 2 MB/s）。
预判名单的误伤面大于它规避的风险，且**它会让"页面明明指向 Gitee、下载却很慢"这种问题
难以从代码和日志里看出来**。宁可让个别浏览器偶尔存成 zip（用户仍可手动改名），
也不要把整批用户悄悄降速到服务器带宽。

### 第三层（服务端）：本地文件优先，Gitee 仅应急

- 按扩展名给 MIME，`server/download_server.py:994 _send_file()`：

  | 后缀 | Content-Type |
  |---|---|
  | `.apk` | `application/vnd.android.package-archive` |
  | `.exe` | `application/vnd.microsoft.portable-executable` |
  | 其他 | `application/octet-stream` |

  代码注释说明了为什么不能图省事用 `octet-stream`：部分安卓浏览器会存成 `.bin`。

- 302 去 Gitee 只在**本站文件缺失**时发生（`server/download_server.py:719`），注释写「仅应急用」。
  兜底白名单 `GITEE_FALLBACK = {"firefly.apk", "firefly-setup.exe"}`（`download_lib.py:334`）。

- 按钮的**默认 href 本身就是 Gitee 直链**（`download_page.py:326` / `:338` 的 `__GITEE_DL__`）。
  因此：没 JS、JS 报错、用户中键新标签打开——统统仍然是 Gitee 流量，不打服务器。

## 4. 安全约束（不要为了省事删掉）

`/download/resolve` 走 HTTP（本站历史上无证书），中间人可改写响应里的 `url` 指向恶意服务器。
零证书条件下唯一的真信任根是 **TLS 域名**：

```js
DL_HOSTS = ['gitee.com', 'foruda.gitee.com', 'github.com', 'objects.githubusercontent.com']
```

`trustedDl()`（`download_page.py:882`）要求 `https:` + 域名在白名单内（含子域后缀匹配），
不通过就 `throw` → 走 `catch` 兜底到服务器直连。

同一思路在本地版自动更新链路：`app/routes_update._validate_download_url()`。

另：`_gitee_latest_tag()` 对 tag 做白名单校验（`[A-Za-z0-9][A-Za-z0-9._-]*`），
在拼进 `Location` 头之前拦掉 `/`、`\`、`\r\n`、`@`、`?`，防响应头注入与路径穿越。

## 5. 不得移除的逻辑（硬约束）

以下四处是**故障防护本体**，改动前必须先复现原故障并证明新方案有效：

1. 前端 `fetch + Blob + a.download` 强制改名；
2. `catch` 里回退服务器直连的分支（Gitee 链路失败时**唯一**的兜底，`download_page.py`）；
3. `_send_file` 里 `.apk` → `application/vnd.android.package-archive` 的映射；
4. `trustedDl()` 的域名白名单校验。

> ⚠️ **不要**再往第 2 条里加 UA 白名单。2026-10-30 已整体移除一次，依据见 §3 第二层。

## 6. 发版必做（否则兜底会落后一版）

- **同步 `FALLBACK_TAG`**（`download_lib.py:320`；**值必须等于当前已发布版本**，本文不重复抄写它，
  以代码为准）。它的触发场景：`update.json` 缺失/损坏 + 本地 `version.json` 读不到 +
  Gitee API 拉取失败。三者同时失效时，它决定下载页与 302 指向哪一版。漏同步 = 用户被兜底到旧版。
- **跑下载页门禁**：`tools/check_download_page.py`。
  2026-10-04 立（0.9.1 交付），4 条判据，任一不过即非零退出：

  | # | 判据 |
  |---|---|
  | ① | 页面不得存在指向压缩包的下载入口（`.zip/.tar.gz/.tgz/.rar/.7z/...`） |
  | ② | 页头版本号 == 期望 tag |
  | ③ | 每个入口可达（200）且 MIME 正确：`.apk` 必须是 apk 类型**或至少不是 zip**；`.exe` 必须可执行 |
  | ④ | 下载字节的 **sha256 + size** 与发布物清单逐条一致 |

  ```bash
  python tools/check_download_page.py \\
      --base http://101.200.14.126:8787 \\
      --manifest _harden/release_manifest_<ver>.json
  ```

  发布物清单格式：`{"version","version_code","artifacts":[{"name","path","size","sha256"}]}`。

### 6.1 发版后实测（2026-10-30 立，强制）

**门禁只验页面与产物，验不到真实下载走的是哪条通道。**

2026-10-30 实测教训：页面 href 已是 Gitee 直链、下载页门禁全绿，但用户下载仍走服务器直连
（约 100 KB/s），原因是 `poorBlobUa` 命中了他的浏览器内核。**从 HTML、代码、日志状态码里
都看不出异常** —— 只有实际下一遍、看速度才能发现。

因此每次发版后，**在电脑上**完整走一遍（PC 与安卓两个包各一次）：

```bash
# ① 按钮 href 必须是 Gitee 直链，不能是 /download/
curl -s http://101.200.14.126:8787/download/ \
  | grep -oE 'id="dl-(apk|exe)" href="[^"]*"'

# ② resolve 必须返回 foruda 直链
curl -s "http://101.200.14.126:8787/download/resolve?file=firefly.apk" | head -c 200

# ③ 实际下载 + 测速（APK / EXE 各跑一次，只改 file=）
U=$(curl -s "http://101.200.14.126:8787/download/resolve?file=firefly.apk" \
    | python -c "import sys,json;print(json.load(sys.stdin)['url'])")
curl -s -o /tmp/a.apk -m 300 -w "速度=%{speed_download} B/s 大小=%{size_download}\n" "$U"
sha256sum /tmp/a.apk
```

判据：

| 项 | 期望 | 不符时 |
|---|---|---|
| 下载速度 | **≥ 1.5 MB/s**（Gitee 常态约 2 MB/s） | 低于 300 KB/s ⇒ **走的是服务器直连**，按第 7 节查 |
| `sha256` | 与 `release_manifests/release_manifest_<ver>.json` 逐条一致 | 产物没替换成功 |
| 按钮 href | `https://gitee.com/...` | 页面模板 / 渲染占位符没生效 |
| resolve 响应 | `{"url": "https://foruda.gitee.com/..."}` | Gitee 302 链变了或 tag 取不到 |

**看到 100 KB/s 左右的速度，不要当成"网络慢"放过** —— 那就是服务器 3 Mbps 带宽被单个下载占满的
典型值，是个真问题。

## 7. 排障对照

| 现象 | 最可能原因 | 查哪里 |
|---|---|---|
| 用户拿到 `.zip`，但 sha256 与产物一致 | 走了 Gitee 直链且该浏览器不在 `poorBlobUa` 里 | `download_page.py:903` 补 UA |
| 下载按钮一直「正在连接 Gitee 直链…」 | `/download/resolve` 失败（Gitee 302 链变了 / 无网络 / tag 取不到） | 直接 GET `/download/resolve?file=firefly.apk` |
| 进度到 100% 但文件没出现 | `a.download` 被浏览器策略拦（需用户手势） | 用第二层兜底 |
| 下载页版本号与产物不符 | `FALLBACK_TAG` 或 `version.json` 落后 | 第 6 节 |
| 下载很慢 / 429 | 服务器直连被限速（3 Mbps 共享） | 引导用户走 Gitee 通道；`/download/stats` |
| **下载速度约 100 KB/s**（Gitee 应约 2 MB/s） | 实际走了服务器直连：`poorBlobUa` 命中了该浏览器内核，或用户浏览器缓存了旧页面 | 查 `requests` 表的 `ua` 列（现记录 400 字符，足以看到 `TBS`/`HeyTapBrowser` 等内核标识）；`download_page.py:903` 的 `poorBlobUa` 正则 |
| 页面 href 是 Gitee，下载却打到 `/download/firefly.apk` | JS 主动改的（`poorBlobUa` 分支），不是页面陈旧 | 同上，先看 UA |

## 8. 变更记录

| 日期 | 事件 |
|---|---|
| 2026-08-14 | 定位 Gitee `.apk` 强制 zip MIME；改为服务器直连为主 |
| 2026-08-16 | 新增 Gitee foruda 直链解析（`resolve`），恢复 CDN 分流 |
| 2026-08-23 | 修 Gitee 无 `/releases/latest` 端点 → 改用列表 API 按 id 取最新 |
| 2026-09-15 | 审计 A4：新增 `trustedDl()` 直链信任校验 |
| 2026-10-04 | 新增门禁 `tools/check_download_page.py`（0.9.1 交付） |
| 2026-10-30 | 本文档建立（此前知识散落在代码注释与 `生产服务器.md` 单行中） |
| 2026-10-30 | 新增 §6.1「发版后实测」：门禁验不到真实通道，必须实际下载测速（PC + 安卓各一次，在电脑上完成） |
| 2026-10-30 | 请求日志的 UA 从截断 120 字符改为 400 —— 原来只记到 `Version/4`，内核标识全丢，无法判断是否命中 `poorBlobUa` |
| 2026-10-30 | **移除 `poorBlobUa` UA 预判名单**，改为全部优先 Gitee；服务器直连仅在 Gitee 链路失败时兜底。实测 HeyTapBrowser 被误伤，稳定吃 100 KB/s |
