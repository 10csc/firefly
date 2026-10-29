我会以当前工作区为准重新取证，特别核对三件事：死代码是否仍无调用、角色卡字段在 `manifest` 与 `preset.json` 中的实际职责、服务器版 overlay 与本地配置的读写是否真正对称。`download_server.py` 若已不在主仓，我会明确把相关拆分方案标为基于上一版代码位置，而不假称已复核私有库现状。
当前代码已确认两个会影响方案范围的细节：`manifest.json` 与 `preset.json` 不是同一用途，不能合成一份；另外，服务器配置 overlay 的不对称不仅是“可能扩展困难”，已有消费链绕开 overlay 的实际证据。接下来会把这些分别纳入字段归属决策和 Top 3 风险。
字段方案可以定下来了：**manifest 是完整的交换与编辑元数据，preset 只是安装后运行时需要的子集**。因此，统一构造本身不需要改 `preset_parse.py`；只有新增字段会被安装后的聊天、检索或 `/modes` 消费时，才必须扩展它的解析白名单。拆分也会沿这个所有权边界进行，而不是简单按行数切文件。
架构风险已收敛为三项，且都有当前源码证据：relay 请求与资产快照没有绑定、配置读取存在 `eff_cfg`/`pack_cfg` 两条不对称路径、pack 子系统通过 lazy import 反向调用 API/路由。最后一项还伴随一个确定的符号错误：`assist_turn()` 导入了不存在的 `resolve_pack_text`，会作为风险证据单列，不混成泛泛的“耦合偏高”。
方案已经收敛：A 阶段只做可回归验证的所有权收口，不改外部协议；B 阶段才处理字段规范、端点模块化和 relay/config 的契约统一。`download_server.py` 在当前文件系统仍存在但被 `.gitignore` 排除，因此其拆分方案会以这份 1181 行本地副本为基线，并明确要求在私有库实施前重新对版。
以下方案基于当前源码。另需说明：`server/download_server.py` 在本机仍存在，但被 `.gitignore` 排除；其拆分方案以这份 **1181 行本地副本**为基线，私有库实施前需要重新对版。

# 任务一：代码质量重构规划

## 1. `sync_engine.py` 死代码清理

### 已确认的影响面

- `_content_sha()`：`app/modules/sync_engine.py:256-260`
- `apply_merge_file()`：同文件 263–298 行
- `_content_sha()` 只被 `apply_merge_file()` 调用。
- 两个符号在 `app/`、`tests/`、`tools/` 中均无外部调用，也未发现动态导入或文档契约。
- 实际上传、下载同步路径在 `app/api/data_sync.py` 中分别使用自己的合并与 CAS 逻辑，不经过这两个函数。

因此，**同时删除两者不会改变仓内同步行为**。不能只删 `_content_sha()`，否则会留下潜在 `NameError`。

唯一边界是：`apply_merge_file` 没有下划线，仓外 Python 脚本理论上可以直接 import 它。但当前项目并未把它声明为插件或公开 SDK 接口，不需要为此保留兼容 shim。

### A：最小可落地改动

1. 同一个提交删除 `_content_sha()` 和 `apply_merge_file()`。
2. 不保留 re-export、空函数或“以后可能使用”的注释。
3. 删除后检查：
   - 两个符号全仓零引用；
   - `sync_engine.py` 可编译；
   - 同步 HTTP、并发、崩溃恢复、规范守卫四组测试通过。

### B：长期最优方案

不要为了消除上传/下载的表面重复，直接复活这个 helper。若以后确实要统一合并策略，应从两条**真实生产路径**提取契约：

- 上传的 `base_sha` 检查；
- 下载的本地 CAS；
- 冲突备份；
- blob 处理；
- 写入失败后的状态推进。

先为这些语义建立测试，再形成新的纯合并函数。当前的 `apply_merge_file()` 并不覆盖这些语义，不能作为未来统一实现的基础。

### 清理原则

> **无调用只是删除候选，不是唯一判据；必须同时确认无动态引用、无测试契约、无公开扩展承诺。删除后也不得以“统一”为名恢复一个语义更弱的 helper。**

---

## 2. 角色卡字段与 `preset.json` 构造统一

### 字段归属决策

`manifest.json` 和 `preset.json` 不应合并，也不能共用同一份无差别字段表。

| 字段类型 | `manifest.json` | `preset.json` | 例子 |
|---|---|---|---|
| 安装后运行时需要的身份与演出配置 | 必须 | 必须 | `id/name/char_name/user_name/presentation/desc/tagline` |
| 广场展示、筛选、治理信息 | 必须 | 不应进入 | `category/tags/author/official/status/downloads` |
| 归档格式与完整性信息 | 必须 | 不应进入 | `format/files/digest` |
| 卡正文、知识库、开场、图片 | 通过 `files[]` 记录 | 不复制为字段 | `core.md`、`opening.json`、图片等 |

**新字段是否需要进入 `preset.json` 的判据只有一个：安装后，脱离广场与后端 manifest，运行时是否仍需要它？**

- 如果聊天、检索、组织器、`/modes` 或包注册表需要：**同时进入 manifest 和 preset**。
- 如果只用于广场展示、审核、统计或草稿生命周期：**只进入 manifest**。
- 即使进入 preset，也必须保留在 manifest 中，否则会在“草稿回读 → 再保存 → 改编”过程中丢失。证据是 `app/plaza/api.py:802` 从 `parsed["manifest"]` 重建编辑数据，而 824–825 行不会把 preset 返回前端。

### 是否包含 `app/core/preset_parse.py`

**仅为消除重复、统一构造：不需要改它。**

**新增运行时字段：必须改它。**  
`_parse_preset()` 在 `app/core/preset_parse.py:113-117` 使用固定返回白名单；只把新字段写进 preset 而不扩展这里，字段仍会在解析时消失。

### A：最小可落地改动

在 `app/plaza/card_format.py` 增加两个共享定义：

- `CARD_METADATA_FIELDS`：创建与改编共同继承、提交的字段；
- `build_preset(manifest)`：从 manifest 投影运行时字段并生成 preset。

然后修改 `app/plaza/api.py`：

- `_card_zip_from_payload()`：396–397、486–490 行改用共享定义；
- `plaza_derive()`：669–677、690–696 行改用同一字段表和构造器。

此方案只涉及 `card_format.py` 与 `api.py`，不改变 zip 格式，也不需要触碰 parser。

### B：长期最优方案

将字段定义升级为声明式 `FIELD_SPECS`，每项描述：

- 是否必填、默认值、长度限制；
- 是否进入 preset；
- 是否允许改编继承和 patch；
- 是否进入广场列表/详情投影。

由它驱动 `_check_manifest()`、创建、改编和 preset 投影，避免继续增加平移字段表。

同时把最终所有权收进 `build_card_zip()`：

1. 调用方只提供 manifest 与卡文件；
2. 没有 `preset.json` 时，由 `build_card_zip()` 自动生成；
3. 已带 preset 时，校验共有字段与 manifest 一致，而不是像当前 `parse_card_zip():618-624` 那样主要只强制 id 一致；
4. `store.publish_card()` 继续复用同一入口，不重写发布语义。

**验收标准**：创建、改编、草稿回读、再次保存、发布、安装六条链路的共有字段完全一致；新增广场字段不会改变安装后的运行时行为。

---

## 3. `app/plaza/api.py` 拆分边界

### A：最小可落地改动

先拆出两个不承载路由语义的模块，保持 `api.py` 的公开导入兼容：

| 模块 | 移入内容 | 当前位置 |
|---|---|---|
| `plaza/http_transport.py` | 响应、查询、代理、本地/服务器分派、当前用户与管理员工具 | `api.py:49-204` |
| `plaza/card_payload.py` | data URL、卡 payload 转换、文件/图片 patch | `api.py:351-619` |
| `plaza/api.py` | 保留全部 HTTP handler，并暂时 re-export 被移走的 helper | 207–350、622–1050 行 |

关键约束：

- `app/api/router.py:55-59` 暂时不改；
- 现有测试对 `plaza.api._plaza_side_post`、`_card_zip_from_payload` 的 patch 路径继续有效；
- 不在这一步搬动 endpoint，降低路由回归风险。

### B：长期最优方案

按业务事务拆端点，而不是按行数平均切分：

| 模块 | 职责 |
|---|---|
| `plaza/api_catalog.py` | list、detail、asset、download |
| `plaza/api_library.py` | installed、install、uninstall；始终操作本端数据根 |
| `plaza/api_forge.py` | derive、review、draft、publish、unpublish |
| `plaza/api_moderation.py` | report、admin list/status |
| `plaza/http_transport.py` | 本地代理与服务器直执的边界 |
| `plaza/card_payload.py` | HTTP payload 与卡格式之间的转换，不再拥有 preset 规则 |

最后让 `app/api/router.py` 直接导入各模块；`plaza/api.py` 可以删除，或只保留极薄的废弃兼容层。

必须保留当前 `_plaza_side():169-186` 已表达清楚的边界：

> 制卡、审核、发布操作广场数据；安装、卸载操作本端用户数据。不能为了“统一调用”把两类端点都代理到服务器。

---

## 4. `download_server.py` 拆分边界

### A：最小可落地改动

先拆两块边界明确、已有独立测试价值的功能：

| 新模块 | 内容 |
|---|---|
| `download_tls.py` | `tls_ready()`、`make_tls_context()`、`cert_info()`，当前 60–123 行 |
| `download_proxy.py` | 上游配置与反代实现，当前 132–144、836–964 行 |

`Handler._proxy()` 保留为薄 wrapper，调用新模块，避免破坏现有测试和调用路径。

本迭代**不搬动**：

- 每 IP/半开连接配额；
- watchdog；
- `GOV/BUDGET` 等共享状态；
- `LimitedServer` 的线程管理。

这些状态多方共享，和纯函数一起搬容易制造新的初始化顺序问题。

### B：长期最优方案

| 模块 | 单一职责 |
|---|---|
| `download_config.py` | 环境变量、端口、上游和流量配置；不在 import 时启动副作用 |
| `download_tls.py` | 证书与 TLS 状态 |
| `download_connection.py` | IP/半开连接配额、发送超时、watchdog |
| `download_content.py` | 静态文件、热更、公告、Range 与文件流 |
| `download_proxy.py` | 请求转发、响应压缩与流式回传 |
| `download_http.py` | 薄 Handler，只负责 HTTP 分派 |
| `download_server.py` | `LimitedServer`、TLS listener 与进程启动 |

现有 `download_lib.py`、`download_page.py`、`traffic.py` 保持独立，不再并回大文件。

---

# 任务二：架构健康度评估

## 总体可扩展性判断

当前架构对“增加一个无状态 HTTP 端点”较友好：`app/api/router.py:1-9` 有集中、静态的路由表，handler 边界明确。

但对“增加一个带用户、角色包或资产状态的新功能”，扩展成本明显更高。原因不是分层数量不足，而是部分状态仍通过**进程全局值、浏览器全局值和 lazy import 隐式传递**，请求契约没有完整携带上下文。

## Top 1：relay 请求没有绑定资产快照，浏览器缓存成为隐式权威

### 证据

- `app/modules/api_client_relay.py:30-45` 入队只有 `call_id/payload/api_base`，没有 mode、账号内资产版本或快照 id。
- `app/routes_relay.py:16-22` 下发时也没有这些字段。
- `app/static/js/relay.js:12` 只有一份全局 `_assets`；53–63 行直接用它填充刚取到的 pending。
- 缓存键在 `relay.js:15` 只有资产名和 `CURRENT_MODE`，没有账号或服务器维度。
- `initAssets()` 在 20 行捕获了 mode，但 37 行写缓存时却重新读取实时 `CURRENT_MODE`；等待期间切包可能写错键。
- `views.js:619-633` 切包时调用 `initAssets()` 但不等待，relay 轮询仍可继续。
- `routes_assets.py:279-288` 对全文算 MD5，却用 `text[:64]` 做缓存键；前 64 字相同、后文不同的资产会复用旧版本号。

### 判断

**UI 在 relay 模式中持有核心资产本身不是职责越位**——这是“服务器不持有用户 Key、资产留在本端”的设计要求。真正的问题是：UI 缓存没有请求归属，却实际决定发往 LLM 的核心设定。

结果可能是 A 包、旧账号或旧版本的资产填入 B 请求，不是单纯的界面陈旧。

### A

- 缓存键加入服务器、账号、mode；
- `initAssets()` 全部使用捕获的 mode，并增加资产就绪状态；
- 未就绪时暂不消费 pending；
- 修正版本缓存键，不再用前 64 字代表全文。

### B

在 relay 队列和 pending 协议中加入 `mode + asset_snapshot_id/versions`。浏览器按该快照取资产，而不是读取“此刻的全局 `_assets`”；回传时仍由 `call_id` 关联同一快照。旧客户端需要显式兼容期。

## Top 2：本地与服务器配置读链不对称，请求作用域没有统一入口

### 对称性核对

| 配置类型 | 本地版 | 服务器版 | 判断 |
|---|---|---|---|
| 模型、记忆窗口等使用 `eff_cfg()` 的配置 | 读全局 config | overlay → 全局 config | 对称 |
| `/set-config` 写入的主动性默认值 | 写全局 config，`pack_cfg()` 能读 | 写用户 overlay，但 `pack_cfg()` 不读 | **不对称** |
| 包级 `proactive.json` | 包级优先 | 包级优先 | 对称 |
| 用户安装的广场包元数据 | 通常在全局 `PRESETS` | 必须经 `user_packs()/pack_meta()` | 直接读 `PRESETS` 时不对称 |

关键证据：

- `core/config.py:26-36` 的 `pack_cfg()` 最后直接 `return config.get(...)`，绕开 overlay。
- `eff_cfg():55-61` 才是 overlay → 全局的正确顺序。
- `api/chat_ops.py:321-332` 和 `routes_pack.py:175-184` 的主动性字段均使用 `pack_cfg()`。
- 因此 `/config` 可以显示 overlay 新值，而实际主动性消费链仍用全局旧值。
- 影响范围主要是仍调用 `/set-config` 的旧客户端/API；独立 `/pack-config` 写的是包级文件，不应混为一谈。

同类问题还有：

- `modules/organizer.py:140-146` 直接读全局 `PRESETS`，而不是 `pack_meta()`；服务器用户包可能因此回退为 `sticker`。
- `modules/llm_retriever.py:70` 也直接读 `PRESETS` 的 `knowledge_dirs`。

### A

- 让 `pack_cfg()` 的全局回落复用 `eff_cfg()`；
- 将运行时消费者的直接 `PRESETS` 读取改为当前用户视角的 `pack_meta()`；
- 增加两个用户、两个包并发读取测试，验证配置和元数据互不串扰。

### B

建立唯一的配置解析入口，统一表达：

> 包级覆盖 → 用户 overlay → 全站默认 → 代码默认值

所有运行时消费者只能调用该入口；后台任务必须显式传入用户上下文，不能依赖“碰巧存在”的 contextvar。

## Top 3：pack 子系统存在真实反向依赖，并已暴露符号缺失

### 证据

当前依赖方向包括：

```text
routes_pack
  → core.pack_structure.build_tree()
      → api.pack_paths
      → routes_pack._kb_*_root
```

具体位置：

- `core/pack_structure.py:144-156` 在 core 中反向导入 `modules.pack_assist`、`api.pack_paths` 和 `routes_pack`。
- `modules/pack_assist.py:104-109、209-222` 又反向导入 API 层。
- `api/pack_paths.py:20-29` 再导入 `routes_pack`。

而且 `modules/pack_assist.py:108` 导入的是不存在的 `resolve_pack_text`；`api/pack_paths.py` 实际提供的是 `read_pack_text()`。知识库或出厂记忆的辅助提案会在该路径触发 `ImportError`，随后被 `api/pack_assist.py:29-31` 转成通用失败提示。

### A

- 将错误符号改为现有 `read_pack_text()`，补知识库与出厂记忆两条执行测试；
- 把 `_kb_user_root/_kb_bundled_root` 从路由模块移到路径模块，先切断 `core → routes_pack`；
- 新代码禁止再从 `core/modules` 导入 `routes*`。

### B

重新划定所有权：

- `core/pack_paths.py`：纯路径计算；
- `modules/pack_files.py`：文本读写、备份与缓存失效；
- `modules/pack_structure.py`：组装结构树；
- `routes_pack.py` / `api/pack_assist.py`：只做 HTTP 参数与响应。

最终依赖方向固定为：

```text
routes/api → modules 服务 → core 路径与格式
```

不再依靠 lazy import 隐藏反向调用。