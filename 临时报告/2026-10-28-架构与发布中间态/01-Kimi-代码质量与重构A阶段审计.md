我会把“大文件”“重复逻辑”“会让执行模型误改的位置”分开取证，生成物和安卓构建镜像只作为冗余来源统计，不拿它们凑问题数。P0/P1 的修改建议会基于当前源码给出可应用 diff，并用静态检查或现有测试验证。
目前已确认一处真实字段漂移：记忆窗口三项能写入并返回，但本地配置的默认值、加载和落盘表都没有它们，重启后会丢失。另一个高风险点是恢复函数对同一种 `backup=True` 同时使用“失败即中止”和“吞错继续”两套语义；这些比单纯文件过长更值得优先修。
问题清单已收敛为 8 项：1 个已有漏改实证的 P0、4 个 P1、3 个 P2。可扩展性会用“新增一个角色卡元数据字段并贯通创建、改编、列表和编辑”来计量；按当前源码是 **6 个手写文件，补测试则 7 个**，主要不是文件大，而是字段表和 preset 构造重复。现在只校验建议补丁的语法与替换前提，不改仓库。
建议补丁已全部在内存中完成替换并通过 Python 语法编译；仓库文件没有被修改。功能行为仍需按各条目的验证项补测，尤其是“备份失败不得继续覆盖”“审核落盘失败不得返回成功”和“响应开始后不得再发 502”三个负例。
# 代码质量概览

当前手写源码中，`app/` 约 4.03 万行、`server/` 约 6759 行；最大的两个手写文件是 `server/download_server.py`（1171 行）和 `app/plaza/api.py`（1044 行）。已确认存在字段表漏改、重复且不一致的错误处理、无调用函数，以及创建/改编路径的字段构造重复。三份 `bundle.js` 均为 11961 行且哈希一致，属于生成物副本，不应作为独立实现交给模型阅读或修改。以下补丁均已基于当前源码在内存中替换并通过 Python 语法编译，**未写入仓库，也未做功能测试**。

# 问题清单

## 1. P0 — 配置字段已经接入 API，却漏出持久化字段表

- **位置**
  - `app/routes_config.py:set_config()`：242–246、273–275 行
  - `app/core/config.py:_load_config()`：186–205、241–284 行
  - `app/core/config.py:save_config()`：490–520 行
- **类型**：字段表重复导致真实漏改；可读性、可扩展性。
- **对 DeepSeek 的具体影响**：模型按现有模式完成“接收参数 → 修改 `cfg.config` → 返回新值”后，会得到表面正确的实现，却不知道还必须同步修改默认值、加载和落盘三张表。记忆窗口三项就是已经发生的漏改：当前进程有效，重启后回退默认值。
- **修改 diff**：

```diff
--- a/app/core/config.py
+++ b/app/core/config.py
@@ -202,6 +202,7 @@
         "proactive_enabled": False, "proactive_hard": 6, "proactive_soft": 0.35,
         "prob_reply_enabled": True, "prob_reply_value": 0.10,
         "hidden_reply_enabled": True,
+        "memory_window_turns": 100, "memory_keep_turns": 30, "memory_warn_turns": 80,
     }
     data = None
     try:
@@ -282,6 +283,11 @@
                 cfg["prob_reply_value"] = 0.10
             # 隐藏式回复配置（缺省：跟随概率式开启；独立开关，关前台概率式不影响隐藏式）
             cfg["hidden_reply_enabled"] = bool(data.get("hidden_reply_enabled", True))
+            for key, default, lo, hi in (("memory_window_turns", 100, 0, 1000), ("memory_keep_turns", 30, 0, 500), ("memory_warn_turns", 80, 0, 1000)):
+                try:
+                    cfg[key] = max(lo, min(hi, int(data.get(key, default))))
+                except (TypeError, ValueError):
+                    cfg[key] = default
     except Exception as e:
         # B1（审计 2026-09-15）：损坏（而非缺失）的 config.json 不再静默吞——先告警
         # 并备份留证再回退默认值。原逻辑下损坏现场会被下一次 save_config 覆盖，
@@ -517,6 +523,9 @@
             "prob_reply_enabled": bool(config.get("prob_reply_enabled", True)),
             "prob_reply_value": max(0.0, min(1.0, float(config.get("prob_reply_value", 0.10)))),
             "hidden_reply_enabled": bool(config.get("hidden_reply_enabled", True)),
+            "memory_window_turns": max(0, min(1000, int(config.get("memory_window_turns", 100)))),
+            "memory_keep_turns": max(0, min(500, int(config.get("memory_keep_turns", 30)))),
+            "memory_warn_turns": max(0, min(1000, int(config.get("memory_warn_turns", 80)))),
         })
```

- **验证方式**：在临时用户目录写入三项非默认值，调用 `save_config()` 后重新执行 `_load_config()`，断言三项保持一致；再覆盖 `0`、上限、非法字符串和字段缺失四种情况。静态检查运行 `python -B -m py_compile app/core/config.py app/routes_config.py`。

## 2. P1 — 审核结果保存失败仍返回成功

- **位置**：`app/plaza/api.py:plaza_review()`，748–762 行。
- **类型**：嵌套异常处理吞错；可读性、局部正确性。
- **对 DeepSeek 的具体影响**：`dr.save_draft()` 的 `DraftError` 只记录日志，随后仍执行 `h._json({"ok": True, ...})`。模型修改审核状态或保存逻辑时，很容易沿用这一模板，使调用方误以为审核结果已经持久化。
- **修改 diff**：

```diff
--- a/app/plaza/api.py
+++ b/app/plaza/api.py
@@ -750,6 +750,7 @@
                           review=result, is_admin=_is_admin_user(h))
         except dr.DraftError as e:
             logger.warning("审核结论落草稿失败：%s", e.code)
+            return _err(h, 500, "审核结果保存失败，请稍后重试", error_kind="storage_error")
     except cf.CardError as e:
         return _err(h, 400, f"卡片校验未通过：{e.message}；请按提示修改后重试")
     except rv.ReviewError as e:
```

- **验证方式**：模拟 `dr.save_draft()` 抛出 `DraftError`，断言响应为 HTTP 500、`ok=False`、`error_kind="storage_error"`，并确认 `finally` 仍清除 `sk`；成功路径响应保持不变。

## 3. P1 — 同一个恢复函数对备份失败使用相反策略

- **位置**：`app/infra/sync/restore.py:_restore_full_snapshot()`，411–416、452–458、479–486 行。
- **类型**：重复逻辑且语义漂移。
- **对 DeepSeek 的具体影响**：自建包定义阶段是“备份失败即中止”，模式包和自建包数据阶段却是“吞错继续覆盖”。模型只检索 `pre-restore` 时会看到多个答案，无法仅凭局部代码判断应遵循哪一种，且容易只修其中一处。
- **修改 diff**：统一为备份失败即中止；自建包在阶段 1 已完成整包备份，删除阶段 2 的重复备份。

```diff
--- a/app/infra/sync/restore.py
+++ b/app/infra/sync/restore.py
@@ -454,8 +454,8 @@
         if backup:
             try:
                 _backup_current_mode(mode, "pre-restore")
-            except Exception:
-                pass    # 备份失败不阻塞恢复（快照本身即数据源）；调用方可传 backup=False 跳过
+            except Exception as e:
+                return False, f"[{mode}] 恢复前备份失败（未改动该包数据）: {e}", n
         tmp = root.with_name(root.name + ".restore_tmp")
         try:
             if tmp.exists():
@@ -479,11 +479,7 @@
     # ── 阶段 2b：自建包用户数据（逐子目录换入，保留阶段 1 的 character/ 定义）──
     for pid, stage in pack_stage.items():
         pack_dir = user_root / pid
-        if backup:
-            try:
-                _backup_current_mode(pid, "pre-restore")
-            except Exception:
-                pass
+        # 阶段 1 已在任何换入前备份整个自建包；此处不重复备份，也不吞备份异常。
         try:
             for sub in sorted(p for p in stage.iterdir() if p.is_dir()):
                 if sub.name == "character":
```

- **验证方式**：分别让 `_backup_current_mode()` 在模式包、自建包上抛出异常；断言返回 `ok=False`、目标目录逐文件哈希不变。自建包正常恢复时，备份函数应只调用一次。

## 4. P1 — `_proxy()` 在响应开始后仍可能再次发送 502，连接关闭也不可靠

- **位置**：`server/download_server.py:_proxy()`，836–954 行；重点为 913–954 行。
- **类型**：单个宽异常块覆盖多个执行阶段；可读性、错误处理重复。
- **对 DeepSeek 的具体影响**：模型修改流式写入或连接关闭时，必须同时理解“响应头是否已发”这一隐式状态。当前正文写入异常会进入统一的 502 分支，导致重复状态行；`conn.close()` 又只在正常路径执行。
- **修改 diff**：

```diff
--- a/server/download_server.py
+++ b/server/download_server.py
@@ -849,6 +849,8 @@
             return self._reply(429, "服务器请求过于频繁（限的是频率，不是你的卡），请稍等约 1 分钟再试".encode("utf-8"),
                                "text/plain; charset=utf-8", {"Retry-After": "10"})
         import http.client
+        conn = None
+        headers_sent = False
         try:
             if (self.headers.get("Transfer-Encoding", "") or "").strip().lower():
                 return self._reply(411, b"Length Required", "text/plain; charset=utf-8")
@@ -901,7 +903,6 @@
                 if len(raw) != int(clen):
                     # 上游声称 N 字节却给少了：正文已被部分消费，**不能**再原样转发
                     # （那会发出"Content-Length: N + 不足的正文"的自相矛盾响应）。如实报 502。
-                    conn.close()
                     _record_request(self, 502)
                     return self._reply(502, "上游响应不完整".encode("utf-8"),
                                        "text/plain; charset=utf-8")
@@ -924,6 +925,7 @@
                     if k.lower() not in ("transfer-encoding", "connection"):
                         self.send_header(k, v)
             self.end_headers()
+            headers_sent = True
             if packed is not None:
                 self._write_throttled(packed, cls)
             elif buffered is not None:
@@ -942,17 +944,21 @@
                     if not GOV.take(cls, len(chunk), timeout=30):
                         break
                     self.wfile.write(chunk)
-            conn.close()
         except Exception as e:
             sys.stderr.write("[PROXY-ERROR] %s\n" % e)
-            _record_request(self, 502)
-            try:
-                self.send_response(502)
-                self.send_header("Content-Length", "0")
-                self.end_headers()
-            except Exception:
-                pass
-
+            if headers_sent:
+                self.close_connection = True
+            else:
+                _record_request(self, 502)
+                try:
+                    self.send_response(502)
+                    self.send_header("Content-Length", "0")
+                    self.end_headers()
+                except Exception:
+                    pass
+        finally:
+            if conn is not None:
+                conn.close()
     def _parse_range(self, size):
         """解析单段 `Range`。返回 `(start, end, partial)`。
```

- **验证方式**：用假连接和假 `wfile` 覆盖两种负例：①响应头后写正文抛错，只允许一个响应并关闭连接；②响应头前上游失败，返回一次 502 并关闭连接。另保留 gzip、非 gzip、流式正文三个正常案例。

## 5. P1 — 存在一个看似“统一入口”、实际无人调用的合并函数

- **位置**
  - 无调用函数：`app/modules/sync_engine.py:_content_sha()`、`apply_merge_file()`，256–298 行
  - 实际上传分派：`app/api/data_sync.py:_import_files_locked()`，268–322 行
  - 实际下载分派：`app/api/data_sync.py:_sync_one_mode_body()`，659–701 行
- **类型**：死代码与近似重复逻辑。
- **对 DeepSeek 的具体影响**：`apply_merge_file()` 的名称和注释都像权威入口，但全仓 `app/server/tests/tools` 没有调用。模型很可能修改它并认为同步行为已经改变；而实际上传、下载各自还有 CAS、备份和冲突处理，不能直接无差别替换成该函数。
- **修改 diff**：删除两个无调用函数，不把语义不同的生产路径强行合并。

```diff
--- a/app/modules/sync_engine.py
+++ b/app/modules/sync_engine.py
@@ -253,46 +253,3 @@
             "to_merge": to_merge, "same": same}
 
 
-def _content_sha(content) -> str:
-    """合并输入内容哈希（blob 判定用；bytes 直算，str 按 utf-8 编码）。"""
-    if isinstance(content, str):
-        content = content.encode("utf-8")
-    return hashlib.sha256(content).hexdigest()
-
-
-def apply_merge_file(root: Path, relpath: str, local_text: str, remote_text: str,
-                     local_mtime: float, remote_mtime: float) -> tuple[str, list, bool]:
-    """执行层裁决：返回 (新内容, 冲突行备份列表, 是否有变化)。
-    - blob（images/ 不可变文件）：不做内容合并——同 sha 跳过；异 sha（uuid 命名下几乎
-      不可能）记日志 + 保留本地 + 返回不变（changed=False）；
-    - .jsonl：append 行级确定性合并；favorites.json：并集；
-    - 其余文档类：本地已有文件时新者胜（mtime）。"""
-    fp = root / relpath
-    if storage.is_blob(relpath):
-        if _content_sha(local_text) != _content_sha(remote_text):
-            logger.warning("blob 合并请求异 sha（应罕见，uuid 命名），保留本地: %s", relpath)
-        return local_text, [], False
-    if relpath.endswith(".jsonl"):
-        st = None
-        if relpath.endswith("data/conversation.jsonl"):
-            try:
-                st = sync_state.load(root)
-            except Exception:
-                st = None
-        merged, conflicts = merge_jsonl(local_text, remote_text, state=st)
-        changed = merged.rstrip("\n") != local_text.rstrip("\n")
-        return merged, conflicts, changed
-    if relpath.endswith("favorites.json"):
-        try:
-            st = sync_state.load(root)
-        except Exception:
-            st = None
-        merged, conflicts = merge_favorites(local_text, remote_text, state=st)
-        changed = merged != local_text
-        return merged, conflicts, changed
-    # 文档类：新者胜（本地 mtime 更新则保留本地；否则用远程）
-    if (remote_mtime or 0) > (local_mtime or 0):
-        if remote_text != local_text:
-            return remote_text, [], True
-        return local_text, [], False
-    return local_text, [], False
```

- **验证方式**：删除后再次检索两个符号均为零引用；运行同步 HTTP、并发、崩溃恢复和规范守卫四份活动测试，确认生产路径未发生变化。

## 6. P1 — 角色卡字段和 preset 构造在创建、改编路径重复

- **位置**
  - `app/plaza/api.py:_card_zip_from_payload()`：396–397、486–490 行
  - `app/plaza/api.py:plaza_derive()`：669–677、690–696 行
  - 对照校验表：`app/plaza/card_format.py:FIELD_MAX/_check_manifest()`，82–83、430–476 行
- **类型**：重复字段表、重复对象构造；可扩展性。
- **对 DeepSeek 的具体影响**：新增字段时，模型可能只改创建路径，导致改编后字段丢失；也可能只改 manifest、不改 preset，造成两份元数据不一致。
- **修改 diff**：

```diff
--- a/app/plaza/api.py
+++ b/app/plaza/api.py
@@ -43,6 +43,14 @@
 # 制卡 payload 里 `images` 允许的键（06 §3.2）；`stickers` 是数组，其余是单图 data URL。
 # `cover` 是 M1 老字段（平铺 `card.cover` 也继续接受），只为老客户端兼容而保留。
 _IMAGE_KEYS = ("avatar", "thumb", "display", "cover")
+_CARD_FIELDS = ("name", "char_name", "user_name", "presentation", "desc", "tagline", "category", "tags")
+
+
+def _preset_bytes(m: dict) -> bytes:
+    return json.dumps({"id": m.get("id"), "name": m.get("name"), "char_name": m.get("char_name"),
+                       "user_name": m.get("user_name"), "presentation": m.get("presentation") or "sticker",
+                       "desc": m.get("desc"), "tagline": m.get("tagline") or "", "schema": 1},
+                      ensure_ascii=False).encode("utf-8")
 
 
 # ── 环境判定 ─────────────────────────────────────────
@@ -393,8 +401,7 @@
         if not str(c.get(key) or "").strip():
             raise cf.CardError(cf.E_BAD_MANIFEST, f"缺少必填字段：{key}")
 
-    manifest = {k: c.get(k) for k in ("id", "name", "char_name", "user_name", "presentation",
-                                      "desc", "tagline", "category", "tags")}
+    manifest = {k: c.get(k) for k in ("id",) + _CARD_FIELDS}
     files: dict = {}
 
     # 1) 老字段（core/identity/sms_samples）——继续接受，等价映射到 files[]
@@ -483,11 +490,7 @@
         # 标签落 character/stickers.json（App 组织器按 label 语义选图，06 §3.2）
         files["stickers.json"] = json.dumps(labels, ensure_ascii=False).encode("utf-8")
 
-    preset = {"id": c.get("id"), "name": c.get("name"), "char_name": c.get("char_name"),
-              "user_name": c.get("user_name"),
-              "presentation": c.get("presentation") or "sticker",
-              "desc": c.get("desc"), "tagline": c.get("tagline") or "", "schema": 1}
-    files["preset.json"] = json.dumps(preset, ensure_ascii=False).encode("utf-8")
+    files["preset.json"] = _preset_bytes(manifest)
 
     if require_thumb is None:
         require_thumb = raw_files is not None or "images" in c
@@ -667,14 +670,10 @@
         up_i, rm_i = _derive_patch_images(card, remove, files)
         # ② 元数据：base 的**卡字段**打底，patch 里出现的才覆盖；id 换新（其余运行期字段一律不带过来）
         manifest = {}
-        for k in ("name", "char_name", "user_name", "presentation", "desc", "tagline",
-                  "category", "tags"):
-            if base["manifest"].get(k) is not None:
-                manifest[k] = base["manifest"].get(k)
-        for k in ("name", "char_name", "user_name", "presentation", "desc", "tagline",
-                  "category", "tags"):
-            if k in card and card.get(k) is not None:
-                manifest[k] = card.get(k)
+        for k in _CARD_FIELDS:
+            value = card.get(k) if k in card and card.get(k) is not None else base["manifest"].get(k)
+            if value is not None:
+                manifest[k] = value
         new_id = str(card.get("id") or "").strip() or _derive_auto_id(base_id, ohash)
         if not st.valid_card_id(new_id):
             return _err(h, 400, "新卡 id 非法（只允许字母/数字/下划线/连字符）")
@@ -688,12 +687,7 @@
                 files["opening.json"] = json.dumps(op, ensure_ascii=False).encode("utf-8")
                 up_f.append("opening.json")
         # preset.json 由后端**按新卡重新生成**（与 `_card_zip_from_payload` 同一口径）
-        files["preset.json"] = json.dumps(
-            {"id": new_id, "name": manifest.get("name"), "char_name": manifest.get("char_name"),
-             "user_name": manifest.get("user_name"),
-             "presentation": manifest.get("presentation") or "sticker",
-             "desc": manifest.get("desc"), "tagline": manifest.get("tagline") or "",
-             "schema": 1}, ensure_ascii=False).encode("utf-8")
+        files["preset.json"] = _preset_bytes(manifest)
         require_thumb = bool(card.get("files") or "images" in card or cf.thumb_name(files))
         data = cf.build_card_zip(manifest, files, require_thumb=require_thumb, is_admin=is_admin)
         meta = dr.save_draft(ohash, data, display=dr.clean_display(body.get("display"), user),
```

- **典型新功能需要修改多少文件**：以“新增一个角色卡元数据字段，并贯通创建、改编、列表和编辑”为例，当前至少需要 **6 个手写文件**：
  1. `app/plaza/card_format.py`
  2. `app/plaza/api.py`
  3. `app/plaza/store.py`
  4. `app/static/js/panels/plaza_forge.js`
  5. `app/static/js/panels/plaza_forge_form.js`
  6. `app/static/js/panels/plaza_forge_local.js`

  加回归测试为 **7 个**；生成 bundle 不计入手写修改。主要耦合点是前后端各自维护字段/上限表、创建与改编分别生成 preset、列表投影单独复制字段。
- **验证方式**：同一字段分别通过创建和改编生成卡片，解析两份 `preset.json` 与 manifest，字段值应完全一致；补丁前后的既有字段输出做逐键对比。

## 7. P2 — 备份排除规则和配置脱敏仍有私有副本

- **位置**
  - 共享定义：`app/infra/sync/manifest.py:17-19`
  - 私有副本：`app/routes_snapshot.py:24-26、62-70`
  - 重复脱敏：`app/routes_snapshot.py:78-86` 与 `app/api/data_export.py:49-59`
- **类型**：重复逻辑、注释与实现不一致。
- **对 DeepSeek 的具体影响**：共享定义附近明确写着“别再各写一份”，但全量快照仍绑定模块私有集合。模型只改共享集合时，会误以为所有备份路径已经同步；新增凭证字段时也只修一条导出路径。
- **处理方向**：全量快照直接复用 `manifest._EXPORT_EXCLUDE_DIRS`；提取一个返回脱敏深拷贝的配置函数供两处调用。这里尚未发现当前 Key 泄漏证据，风险是后续规则漂移。
- **验证方式**：新增一个临时排除目录和凭证字段，只改共享规则，分别生成单包备份与全量快照，断言两种 zip 都排除该目录且递归搜索结果不含凭证值。

# 冗余热点 Top 3

| 热点 | 已确认规模 | 冗余主要形态 |
|---|---:|---|
| 前端 bundle 副本群：`app/static/js/bundle.js`、`android/app/src/main/assets/js/bundle.js`、`android/app/src/main/python/backend/app/static/js/bundle.js` | 每份 11961 行、600559 字节；三份 SHA-256 完全一致 | **生成代码多副本**。不是三套独立实现；检索和修改时应只读源模块，避免模型把副本当额外真相源 |
| `server/download_server.py` | 1171 行；`_proxy()` 自身 119 行；文件内有 27 处 `except Exception` | **多阶段错误处理与连接管理重复**。不是单纯“文件大”，而是响应前、响应后共用一个异常出口，增加局部修改的理解成本 |
| `app/plaza/api.py` | 1044 行 | **字段表、preset 构造和端点校验重复**。创建与改编各写一份元数据逻辑，是最直接影响功能扩展的冗余 |