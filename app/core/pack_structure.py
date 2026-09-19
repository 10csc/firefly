# -*- coding: utf-8 -*-
"""角色卡结构规范（声明式 · 唯一权威源，2026-09-15）

一张角色卡的全部内容 = 数据域（domain）列表；每域含若干来源（source）。
（术语：角色包 = 角色卡 + 用户私有聊天数据；本文件只描述卡，不含私有数据。）
角色卡管理树（前端树 UI）、本端点注入的实态、未来的新域/新槽位，**全部以这里的注册为准**——
加内容只改注册，不改树代码（自适应，不写死）。

source.type：
- slot       可编辑槽位文件（人设/提示词；file 相对包根；save/delete/✨AI 辅助 全开）
- file       可编辑非槽位文件（memory/default.md 等；同样可编辑+辅助）
- dir        目录型内容（知识库；树形子节点，逐文件编辑+辅助）
- sys_prompt 代码内置系统提示词（只读展示；module.attr 为模板来源，占位符按包渲染）
- config     配置块（主动消息配置，前端嵌入既有控件）
- identity   用户形象块（称呼+用户头像，前端嵌入既有控件）
- stickers   表情包块（前端嵌入既有网格）
- assets     形象资产块（头像/封面，前端嵌入既有操作）
- view       只读/特殊交互（开场脚本）
when=sticker|narration：按**模式**过滤该来源（sticker=故事/剧情，narration=剧本）。

渲染哲学：规范描述"这个角色卡由什么组成"，实态（存在/大小/用户副本）由 build_tree 注入，
前端只做树形收展与就地展开，**不感知业务结构**。
"""

from modules import app_config as cfg

PACK_STRUCTURE: list[dict] = [
    {"key": "identity", "label": "用户形象", "icon": "👤",
     "desc": "你在角色面前是谁：称呼与头像只对本角色生效",
     "sources": [{"type": "identity", "label": "称呼与用户头像"}]},
    {"key": "persona", "label": "人设与口吻", "icon": "🎭",
     "desc": "回复器（polisher）的人设输入：核心四件 + 回复器提示词",
     "sources": [
         {"type": "slot", "label": "回复器人设（核心文案）", "file": "prompts/polisher.md",
          "used_by": ["回复器"]},
         {"type": "slot", "label": "核心设定（身份/经历/价值观）", "file": "core.md",
          "used_by": ["分析器", "回复器"]},
         {"type": "slot", "label": "人际关系与认知边界", "file": "identity.md",
          "used_by": ["分析器", "回复器"]},
         {"type": "slot", "label": "短信风格示例", "file": "sms_samples.md",
          "used_by": ["回复器"]},
         {"type": "slot", "label": "用户设定", "file": "用户设定.md",
          "used_by": ["分析器", "回复器"]},
         {"type": "file", "label": "时态标记规范", "path": "_时态标记规范.md",
          "used_by": ["人设文档写作约定（core/identity 等引用）"]},
     ]},
    {"key": "retriever", "label": "检索器", "icon": "🔍",
     "desc": "检索设定资料 → 压缩知识摘要（整库注入，缓存高命中）",
     "sources": [
         {"type": "sys_prompt", "label": "检索系统提示词",
          "module": "modules.llm_retriever", "attr": "_SYSTEM_PROMPT"},
         {"type": "dir", "label": "知识库", "path": "knowledge/",
          "used_by": ["检索器"], "feeds": "分析器"},
         {"type": "file", "label": "出厂记忆", "path": "memory/default.md",
          "used_by": ["检索器", "记忆管理（load_head 回落）"], "feeds": "分析器"},
     ]},
    {"key": "analyzer", "label": "分析器", "icon": "🧠",
     "desc": "意图理解 + 事实核查（思考档）",
     "sources": [
         {"type": "sys_prompt", "label": "分析系统提示词",
          "module": "modules.analyzer", "attr": "_ANALYZER_SYSTEM"},
         {"type": "slot", "label": "剧本事实核查补充", "file": "prompts/analyzer_extra.md",
          "used_by": ["分析器"]},
         {"type": "ref", "label": "人设输入（core / identity / 用户设定）", "target": "persona",
          "note": "分析器与回复器共用，改动见「人设与口吻」"},
     ]},
    {"key": "organizer", "label": "组织器", "icon": "🎨",
     "desc": "表情包调度 / 旁白演出（按本包演出形态启用）",
     "sources": [
         {"type": "sys_prompt", "label": "表情包调度系统提示词", "when": "sticker",
          "module": "modules.organizer", "attr": "_ORGANIZER_SYSTEM_FALLBACK"},
         {"type": "slot", "label": "表情包调度提示词", "file": "prompts/organizer_sticker.md",
          "when": "sticker", "used_by": ["组织器"]},
         {"type": "sys_prompt", "label": "旁白生成系统提示词", "when": "narration",
          "module": "modules.organizer", "attr": "_NARRATION_SYSTEM_FALLBACK"},
         {"type": "slot", "label": "旁白生成提示词", "file": "prompts/organizer_narration.md",
          "when": "narration", "used_by": ["组织器"]},
         {"type": "stickers", "label": "表情包（全局共享 + 本包专属）"},
     ]},
    {"key": "proactive", "label": "主动性", "icon": "💌",
     "desc": "角色在合适的时候主动找你：门控文案 + 频率配置",
     "sources": [
         {"type": "slot", "label": "主动消息·情境文案", "file": "prompts/proactive_context.md",
          "used_by": ["主动性"]},
         {"type": "config", "label": "主动消息配置"},
     ]},
    {"key": "stage", "label": "环境与演出", "icon": "🌙",
     "desc": "环境句后缀 / 开场演出（按形态启用）",
     "sources": [
         {"type": "slot", "label": "环境句·世界后缀", "file": "prompts/env_suffix.md",
          "when": "narration", "used_by": ["回复器（环境句拼接）"]},
         {"type": "file", "label": "开场演出脚本", "path": "opening.json",
          "used_by": ["开场演出（首次进入自动消息）"]},
     ]},
    {"key": "assets", "label": "形象资产", "icon": "🖼",
     "desc": "角色头像与封面",
     "sources": [{"type": "assets", "label": "头像 / 封面"}]},
    {"key": "danger", "label": "危险区", "icon": "⚠",
     "desc": "归档 / 恢复默认（不可逆操作都带确认与备份）",
     "sources": [{"type": "danger", "label": "归档与恢复默认"}]},
]


def _slot_state(mode: str, rel: str) -> str:
    """槽位三态（3.3）：baseline / inherited / customized；无清单时回退推导。"""
    try:
        st = cfg.pack_registry().get_slot(mode, rel)
        if st:
            return st
    except Exception:
        pass
    from core.presets import slot_state_from_disk
    return slot_state_from_disk(mode, rel, None)


def _render_sys_prompt(mode: str, module: str, attr: str) -> str:
    """只读系统提示词：从模块取模板，替换包名占位（{knowledge} 等结构占位保留展示）。"""
    try:
        import importlib
        tpl = getattr(importlib.import_module(module), attr, "") or ""
    except Exception as e:
        return f"（模板读取失败: {e}）"
    return tpl.replace("{char_name}", cfg.char_name(mode)).replace("{user_name}", cfg.user_name(mode))


def build_tree(mode: str) -> dict:
    """规范树 + 实态注入。返回 {mode, presentation, domains: [...]}（GET /pack-structure 主体）。"""
    p = cfg.PRESETS.get(mode) or {}
    presentation = p.get("presentation", "sticker")
    domains = []
    for d in PACK_STRUCTURE:
        sources = []
        for s in d["sources"]:
            if s.get("when") and s["when"] != presentation:
                continue
            src = dict(s)
            t = s["type"]
            if t == "slot":
                fp = cfg.mode_character_dir(mode) / s["file"]
                bundled = cfg.bundled_character_dir(mode) / s["file"]
                src["exists"] = fp.is_file() or bundled.is_file()
                src["user_copy"] = fp.is_file()
                src["state"] = _slot_state(mode, s["file"])
                from modules.pack_assist import assistable
                src["assistable"] = assistable(s["file"])
            elif t == "file":
                from api.pack_paths import read_pack_text
                text = read_pack_text(mode, s["path"])
                src["exists"] = bool(text)
                src["user_copy"] = (cfg.mode_character_dir(mode) / s["path"]).is_file()
                from modules.pack_assist import assistable
                src["assistable"] = assistable(s["path"])
            elif t == "dir":
                try:
                    from routes_pack import _kb_bundled_root, _kb_user_root
                    b, u = _kb_bundled_root(mode), _kb_user_root(mode)
                    n = 0
                    for root in (b, u):
                        if root.is_dir():
                            n += len([x for x in root.rglob("*.md") if x.name != "index.md"])
                    src["exists"] = n > 0 or u.is_dir()
                    src["count"] = n
                except Exception:
                    src["exists"] = False
            elif t == "sys_prompt":
                src["text"] = _render_sys_prompt(mode, s["module"], s["attr"])
            sources.append(src)
        domains.append({"key": d["key"], "label": d["label"], "icon": d["icon"],
                        "desc": d["desc"], "sources": sources})
    return {"mode": mode, "presentation": presentation,
            "char_name": p.get("char_name") or "", "user_name": cfg.user_name(mode),
            "custom": bool(p.get("custom")), "domains": domains}
