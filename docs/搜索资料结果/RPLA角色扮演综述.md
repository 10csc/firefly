# RPLA 综述笔记：角色扮演语言代理的三种 Persona 分类法

> 来源：From Persona to Personalization: A Survey on Role-Playing Language Agents
> arXiv:2404.18231，TMLR 2024（复旦 Yanghua Xiao 团队），PDF 暂存于 `C:\Users\FANGL\AppData\Local\Temp\opencode\rpla_survey.pdf`
> 全文 50 页已读完。可信度高：TMLR 正式发表、OpenReview 公开评审、领域内最活跃团队之一；局限：覆盖到 2024 年初，部分内容略旧。

## 三种 Persona 分类法（核心框架）

| 维度 | Demographic（群体型） | Character（角色型） | Individualized（个性化型） |
|------|---------------------|-------------------|--------------------------|
| 定义 | 群体共性：职业/MBTI/社会群体 | 公认角色：历史人物/名人/虚构角色 | 个体用户数据构建的动态画像 |
| 数据 | LLM 预训练内建 | 描述（百科/原作）+ 示范对话 | profile + interactions + domain knowledge |
| 构建 | 简单 prompt | 参数化训练 或 prompt+记忆+检索 | 离线微调 或 在线记忆管理 |
| 应用 | 专家任务/多智能体/社会模拟 | 陪伴/NPC | 个人助理/数字分身 |

三种可共存（苏格拉底哲学导师 = 三者叠加）。构建两大路线：参数化（预训练/SFT/RL，需重训、有遗忘风险）vs 非参数化（ICL+记忆+RAG，免训练、主流）。

## 对火萤最有用的章节要点

### 角色数据三来源（§5）
1. Experience Extraction：从原作提取对话——忠实，但缺背景难用
2. Dialogue Synthesis：LLM 合成——无原作参照时质量受限需过滤
3. Human Annotation：人工标注——质量最高但贵

### 评估框架（§3/§5）
- 角色无关能力：投入度（不出戏）、对话质量、拟人能力（ToM/共情/情绪智力）
- 角色保真 4 维：语言风格（表层）→ 知识（表层）→ 人格与思维过程（深层，大五量表可量化）
- 4 种方法：有/无 ground truth 自动评估、多选题、人工评估

### 关键概念
- **Character Hallucination**：表现超出角色范围的知识/能力（苏格拉底写代码）——角色 agent 核心问题，解法：教"不知道就拒绝"或 SFT
- **Point-in-time hallucination**：说出角色当时不该知道的事（幼年哈利波特剧透未来）
- **自我中心倾向**：只顾展示自己的 persona 而忽视用户——评测时需盯
- **persona 构建偏见**：简单 persona 流于表面，无法承载细粒度决策；会放大刻板印象与毒性

## 火萤落地清单

1. 流萤 = Character（本体）+ Individualized（对开拓者好感演化）复合体；后者走在线非参数路线（记忆+检索，现有 memory_manager/llm_retriever 已对齐）
2. 语言风格保真靠示范对话：从崩铁剧情提取对话是值得做的数据工作（演示不必复现原话，泛化其模式）
3. 知识边界约束：游戏外世界知识、未经历的事件不可"记得"
4. 人格量化：大五量表对话测试可迁移为"像不像流萤"的评测
5. 风险：设定 prompt 处理不当会放大自怜/圣母化；评测中对照 5 条清单（投入度/一致性/知识边界/人格/情感支持）

## 相关调研

- 同目录 [persona建模方法论.md](persona建模方法论.md)：PersonaTester（FSE 2026）的"三维正交+真实语料统计"做法——与本文 §7"persona 需更丰富维度"互相印证
