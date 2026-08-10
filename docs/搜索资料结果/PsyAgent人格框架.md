# PsyAgent：Big Five 驱动的可控人格 agent 框架

> 来源：PsyAgent: Constructing Human-like Agents Based on Psychological Modeling and Contextual Interaction
> arXiv:2601.06158（2026-01 提交），香港科技大学（Zibin Meng, Kani Chen），25 页含完整附录
> PDF 暂存于 `C:\Users\FANGL\AppData\Local\Temp\opencode\psyagent.pdf`
> 可信度：工程方案可信可复现（附录全公开 prompt/schema/训练配置），但存在"自我验证循环"弱点（数据合成/筛选打分/评估都是 LLM 自循环，无真人数据对标）。

## 核心设计：IS × MSC 双组件

**Individual Structure（IS）**——机器可用的个人档案，4 大领域：
- 教育轨迹（阶段/专攻/成绩/教学环境/良师/转折）
- 生活经历（出身迁移/角色经历/重大事件/旅行/社交模式/兴趣）
- **社会经济背景（家庭构成/家庭文化氛围/网络资本/阶级认同/流动轨迹）**
- 文化资本（具身/客体化/制度化资本/品味/媒体习惯/消费）

**Multi-Scenario Contexting（MSC）**——8 场景框架库（工作/家庭/友谊/陌生人/独处/浪漫/学习/公共表达），每框架结构化记录：`⟨场景, 角色, 对应方类型, 规范, 利害, 子技能, 模板, 反馈⟩`

推理时固定结构化 prompt 绑定「场景标签 + IS 档案 + Big Five 目标向量」。原则：make the persona computable and the context explicit。

## 数据/训练（可复制的工程配方）

- 38,880 样本：6⁵ Big Five 配置 × 5 实例 × IS×MSC 全交叉，llama3_3_70B 零样本合成，三类任务（自述/多轮角色扮演/带理由决策探针）
- 关键规则：**生成文本禁止出现特质名/分数**（防泄漏）
- QLoRA（rank=16）+ SFT（NLL + 特质一致性惩罚）+ DPO；backbone 仅 llama3_2_1B/3B

## 结果

- 18 模型基线（1B-70B）ProfileAcc 50-73，**规模不单调**；PsyAgent 加持 1B +11.70、3B +0.93——架构与监督可战胜规模
- DPO 一致优于 SFT；小模型受益更大；无安全回退
- 消融（最关键）：**移除 Socioeconomic Context 损失最大（−14.31）**——背景设定是保真最强锚点；IS 平均 −8.9 主司特质保真，MSC 平均 −6.3 主司规范契合，互补

## 局限（作者自述+我的判断）

- 合成数据自洽 ≠ 真人感：评估是"模型自述→自答 IPIP-120→比对目标"，无真人/权威数据对标，scorer coupling 风险（论文承认）
- 未用外部基准（PersonaGym/CharacterEval）；表间因 prompt 表面不同不可直接比
- 单机构两人、arXiv 新提交未录用；代码 "upon acceptance" 开源

## 对火萤落地要点

1. IS/MSC 双组件模式可移植到流萤角色卡：背景档案（IS）+ 场景规范（MSC）分离设计
2. **社会经济背景可能是最强保真锚点**（消融 −14.31）：流萤的成长背景档案值得细化
3. "no explicit trait names"：失熵症/特质体现在行为而非口头命名（与三层思考法一致）
4. trait prior 与 demographic 解耦防刻板印象（防"温柔克制"滑向圣母化）
5. IPIP-120 + 百分位指标可作"像不像流萤"自洽性评测；但**要测"像人"必须加真人数据对标**，避免同源打分器自证循环

## 相关调研

- [RPLA角色扮演综述.md](RPLA角色扮演综述.md)：评估框架（角色保真 4 维、InCharacter 大五访谈评测——有真人对照，与 PsyAgent 互补）
- [persona建模方法论.md](persona建模方法论.md)：PersonaTester 三维正交 persona + 真实语料统计
- [信念-行为一致性.md](信念-行为一致性.md)：设定 ≠ 行为的验证警告
