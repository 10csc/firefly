# -*- coding: utf-8 -*-
"""新建角色包时写入内容槽位的模板真身。

放在 core 层供建包生命周期直接使用，避免 api/core 反向依赖 routes_pack。
"""

_PACK_CORE_TPL = "# {char_name} · 核心设定\n\n（在这里写：{char_name}是谁——身份、经历、价值观。每条一行，以 [事实] 开头）\n"
_PACK_IDENTITY_TPL = "# {char_name} · 人际关系与认知边界\n\n（在这里写：{char_name}与{user_name}的关系、习惯与喜好、{char_name}知道什么不知道什么）\n"
_PACK_SAMPLES_TPL = "# {char_name} · 短信风格示例\n\n（在这里贴几条{char_name}说的话的示例，模型会模仿这个语气。越真实越好）\n"
