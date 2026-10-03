# 改编通道 `POST /plaza/api/derive`（2026-10-01）

> **一句话**：以广场上**已发布**的卡为模板改编时，**服务端复制卡体**，客户端**只上传 patch 里变化的文件**
> —— 不再整卡重传。产物是改编者自己的**新草稿**，原卡与原作者完全不受影响，随后走**既有**审核/发布闸门。

用户原话：「用户如果以某角色卡为模版，那发布的时候会不会重新复制一份上传？（我希望不会，
最好只会创建副本，上传只会替换修改的部分，这样节省流量）反正服务器内部复制就一瞬间。」

## 一、请求

```jsonc
POST /plaza/api/derive
{
  "base_id": "base_card",              // 必须：广场上**已发布**的卡（不存在/未发布 ⇒ 404 人话）
  "card": {                            // 可选：**只给变化的部分**（缺省全部沿用 base）
    "id": "my_copy",                   //   新卡 id，缺省自动 "<base>-copy"（撞名则 -copy2…）
    "name": "…", "desc": "…",          //   任意元数据字段（name/char_name/user_name/presentation/
                                       //   desc/tagline/category/tags）
    "files":    [{"path": "core.md", "text": "…"}],                    // 新增/替换文本
    "images":   {"thumb": "data:image/…",                              // 只放**被替换**的图片
                 "stickers": [{"data": "data:image/…", "label": "开心"}]},
    "opening":  {"narrations": […], "first_messages": […]}             // 结构化开场白（同制卡页）
  },
  "remove": {"files": ["knowledge/旧.md"], "images": ["sticker-2"]},   // 可选：删除
  "display": "改编者"
}
```

- `images.stickers` 一旦给出 ⇒ **整组替换**（列表本身是位置的 `sticker-1..N`，部分替换语义含糊）；
  只想删某一张时用 `remove.images: ["sticker-2"]`。
- 请求形状与制卡页 payload **同源**（`files[]`/`images{}`/`opening`），只是允许**部分**给。

## 二、服务端做什么

1. `base_id` 必须是**已发布**卡（`store.get_card()` 默认只认公开状态）⇒ 否则 404；
2. 读 base 的 `card.zip`，把 `character/**` 白名单文件**复制**进新卡（0 上传）；
   `preset.json` **不复制** —— 它是唯一权威清单、里面写着旧 id，按**新 id 重新生成**；
3. 依次应用 patch：文本新增/替换 → 删除文件 → 图片槽位替换（先删旧同槽位，扩展名可能不同）→
   表情包整组替换 → 删除图片（同时把 `stickers.json` 里悬空的标签剪掉）；
4. 元数据 = base 的卡字段打底 + patch 里出现的字段覆盖；`id` 换新；**运行期字段一律不带过来**
   （`created_at`/`files`/`digest`/`author`… 由 `build_card_zip` 与发布路径重算）；
5. `cf.build_card_zip(...)` 走**与普通制卡完全相同**的校验（文字总量 8MB、图片逐张槽位限制、
   文件数 500、路径白名单…）——derive 不另开一套规则，避免两套规则漂移（`docs/错误总结.md` #10）；
6. 存成**当前用户的草稿**（`derived_from` 记进草稿 meta；发布时随 `extra_meta` 落到广场 meta）。

## 三、保证与红线

| 关注 | 保证 |
|---|---|
| 原卡不受影响 | 只读 base；不改、不删、不动发布时间；回归里比对 `card.zip`/`meta.json` **字节不变** |
| 越权 | 只能改编 **published** 的卡；非登录 ⇒ **401**；pending/archived/不存在 ⇒ **404 人话** |
| 隐私 | 只复制**卡体**（`character/**` 白名单）——不碰 base 作者的账号数据/记忆/聊天；产物 manifest **不带** `author`/`uid_hash` |
| 审核 | 改编产物走**既有**闸门（草稿 → 审核 → 发布）；管理员仍走免审路径 |
| `derived_from` | **只记录**在草稿 meta 与广场 meta，`_public_view`/`_detail_view`/列表都按白名单取字段 ⇒ **不在任何客户端响应里** |

## 四、省流量实测（`tests/test_plaza_derive.py`，同一次"只改 `core.md`"的改编）

| 做法 | 请求体 |
|---|---|
| 旧：客户端拉下正文/图片再发**全新卡** | **144775 B（141.4 KB）** |
| 新：`derive` + 只传改动的那个文本 | **206 B（0.2 KB）** ⇒ **↓99.9%** |

同一个测试还钉住：① 只改一个文本 ⇒ `uploaded.files` 只含它；② 未改的图片**不传**也照样在（字节一致）；
③ 删文件/删表情包后产物确实没有、标签不悬空；④ 原卡字节不变；⑤ 401/404；
⑥ 产物只含卡体白名单、不带原作者 author；⑧ `derived_from` 只记录不暴露。

## 五、客户端怎么接（frontend 参考）

- 进"改编"页时**不需要**把整卡正文/图片都拉下来再原样发回：只把用户**真正改过**的文件放进 `card.files`、
  被换掉的图放进 `card.images`，其余一律不发（服务端已有）。
- 想显示"改编自 X"：`derived_from` 在**草稿**接口里可见（自己的草稿），但**不会**出现在广场详情/列表。
- 未改动的图片预览仍可用 `/plaza/api/asset`（详情）或草稿回读接口拿，用于界面展示。
