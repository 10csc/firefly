# PsyAgent：基于心理建模与情境化交互构建类人智能体

> **原文**：Zibin Meng and Kani Chen*，香港科技大学（Hong Kong University of Science and Technology）
> zmengal@connect.ust.hk, makchen@ust.hk
> **原文链接**：arXiv:2601.06158
> **译文说明**：本文为论文全文中文翻译，涵盖摘要、引言、相关工作、方法（IS 个人结构与 MSC 多场景情境化）、数据合成、训练、评估协议、实验结果、消融、局限、伦理声明、参考文献及全部附录。术语采用「英文原文（中文译名）」形式标注。图 1–22、表 1–3 的编号与标题均已翻译。参考文献按学术惯例保留英文原文。附录中的提示词模板采用中英对照（英文原文 + 中文翻译）。

---

## 摘要（Abstract）

类人智能体（human-like agents）需要对「稳定特质如何在结构化社会情境中显现」进行建模，而不仅仅是编码知识、技能或情感。我们提出 **PsyAgent**，一个将两者耦合的框架：(i) **Individual Structure（IS，个人结构）**，一种机器可用的个人档案（profile），涵盖特质与层面（traits and facets）、认知风格、价值观、文化与教育资本，以及突出的生活经历片段；以及 (ii) **Multi-Scenario Contexting（MSC，多场景情境化）**，一种跨越八个领域（工作、家庭、友谊、陌生人及公民生活、独处与自我调节、恋爱、学习、公共表达）的角色–关系–规范（role–relationship–norm）框架。在推理时，固定结构化提示将当前场景绑定到智能体个人档案，从而产生既稳定又对情境敏感的行为。我们将 IS 与 MSC 实例化，以合成监督数据（角色扮演对话、决策探针、反馈轨迹），随后微调一个小型 LLM。所得模型能够针对指定的大五人格配置产生一致、可识别、与人格（persona）对齐的行为，并在我们的多项指标——人格一致性（persona consistency）、情境适当性（contextual appropriateness）、风格匹配（style matching）、特质可识别性（trait identifiability）和长程稳定性（long-horizon stability）——上达到或超过若干更大的未调优 LLM 及其他未调优基线。消融实验表明：IS 主要提升特质保真度与风格稳定性，而 MSC 驱动规范感知与决策契合度；两者对跨场景表现均不可或缺。PsyAgent 为基于人格的智能体提供了一种精确、数据高效的架构。

---

## 1 引言（Introduction）

构建具有社会能力、类人化的智能体，需要对「稳定倾向（dispositions）如何与结构化社会场域交互」进行建模，而不仅仅是编码知识、技能或情感。大五人格（Big Five）概括了这类倾向，并与下游认知与表现相关（Anglim et al., 2022）。然而，以人设（persona）为条件的对话与角色扮演智能体在情境转换下常常发生漂移或崩溃（Li et al., 2016; Zhang et al., 2018; Shao et al., 2023; Xu et al., 2024; Lee et al., 2024; Samuel et al., 2024; Li et al., 2024）。人机交互研究强调心理模型（mental models）、互补性（complementarity）以及具有规范感知的辅助（norm-aware assistance）对可信协作的重要性（Amershi et al., 2019; Bansal et al., 2019; Steyvers et al., 2022; Buçinca et al., 2021; Vasconcelos et al., 2023）。

我们提出 **PsyAgent**，它将 (i) 基于大五人格的特质先验（trait prior）与 (ii) 情境化场景中的上下文实现相耦合。PsyAgent 通过两种资源将这一接口运作化（operationalize）：

- **Individual Structure（IS，个人结构）**：一种机器可用的个人档案，编码特质/层面、衍生的行为倾向（如风险容忍度、时间偏好）、认知风格、价值取向、文化/教育资本，以及简短的合成人生历程片段（synthetic life-course episodes）。
- **Multi-Scenario Contexting（MSC，多场景情境化）**：一个角色–关系–规范框架库，覆盖八个领域——工作、家庭、友谊、陌生人/公民生活、独处/自我调节、恋爱/亲密、学习、公共表达——并带有结构化字段（角色、权力/亲和结构、规范、利害关系）、交互模板和结果反馈（Li et al., 2024; Samuel et al., 2024; Wang et al., 2025）。

这一设计超越了一次性提示（one-off prompting），走向可复用、上下文丰富的条件化（conditioning）。我们的目标是 (a) 随时间表达稳定的特质印记（trait signatures）；(b) 对跨多样社会场域的角色与规范保持敏感；(c) 在不刻板化（stereotyping）的前提下泛化。这带来了特质–情境整合、情境丰富监督的数据稀缺、以及超越表面风格的度量等挑战。

为证明超越提示工程的可学习性，我们使用 IS×MSC 合成监督数据（角色扮演对话、决策探针、反馈轨迹），遵循指令/进化式数据创建范式（instruction/evolution-style data creation）（Wang et al., 2022; Taori et al., 2023; Zeng et al., 2024; Mukherjee et al., 2023; Mitra et al., 2023）。我们使用 PEFT（LoRA/QLoRA）和可选的 DPO 训练来微调相对较小的 LLM（Hu et al., 2022; Dettmers et al., 2023; Rafailov et al., 2023）。在推理时，固定结构化提示将当前场景绑定到 IS 个人档案上；学习到的适配器（SFT/DPO）在适配提示中所编码的规范、利害关系与人际关系的同时，保持特质一致的选择与语言风格。确定性解码（deterministic decoding）与结构化提示促进规范感知与可问责行为（Amershi et al., 2019; Poursabzi-Sangdeh et al., 2021; Buçinca et al., 2021）。

我们在全部八个领域的多轮角色扮演与决策任务上评估 PsyAgent，结合自动度量与人工评判。除逐特质误差外，我们还报告百分位空间的个人档案准确率（percentile-space profile accuracy）、个人档案内部的等级一致性（within-profile rank consistency），以及从生成文本中提取的特质可识别性（Wang et al., 2025）。评估中，小模型表现出一致、可识别的、与人格对齐的行为，在人格一致性、情境适当性、风格匹配、特质可识别性和长程稳定性上优于更大的通用 LLM 和未调优基线，且未观察到安全性回退——这与「有针对性的监督（targeted supervision）和紧凑适配器可以释放强行为」的发现一致（Hsieh et al., 2023; Zhang et al., 2024; Dubey et al., 2024），并与 RLHF/RLAIF/Constitutional AI 等进展互补（Ouyang et al., 2022; Lee et al., 2023; Bai et al., 2022）。消融实验表明二者角色互补：IS 主要增强特质保真度与风格稳定性，而 MSC 驱动规范感知与决策契合；两者对稳健的跨场景表现均不可或缺。

**主要贡献（Contributions）**：

1. 一个心理学上有理有据的框架，将大五特质先验与结构化社会情境之间的接口运作化；
2. 两种可复用资源——IS 与 MSC——附带 schema（模式定义）、示例与创作指南（authoring guidance）；
3. 一个 IS×MSC 数据集与 PEFT+DPO 协议，使小模型在人设/情境指标上达到或超过多个更大的未调优 LLM，且无安全性退化；
4. 一套评估套件（百分位度量、等级一致性、可识别性、消融实验），隔离 IS 与 MSC 各自的角色。

---

## 2 相关工作（Related Work）

### 2.1 人设 LLM 与特质–情境接口（Persona LLMs & Trait–Context Interface）

早期的人设条件对话（persona-conditioned dialogue）研究表明，显式的个人档案可以提升一致性，但在长上下文与领域转移下较为脆弱（Li et al., 2016; Zhang et al., 2018）。近年来的智能体系统（agentic systems）追求显式角色扮演，以更好地控制风格与决策倾向（Shao et al., 2023; Xu et al., 2024; Lu et al., 2024）。相关工作还通过合成人设语料库（synthetic persona corpora）和模拟社会（simulated societies）扩展覆盖面（Ge et al., 2024; Park et al., 2024），而 Wang et al. (2025) 系统性地评估了人格模拟。在与我们最接近的大五人格方向上，Li et al. (2024) 在人类标注的大五数据上训练，Chen et al. (2025) 引入了潜在人设控制（latent persona controls）。互补的负面结果表明，仅靠提示（prompting）存在持续的价值观/道德惯性（Lee et al., 2024）。相比之下，PsyAgent 显式建模大五特质先验与结构化情境库（MSC），瞄准的是倾向（dispositions）与情境化角色/规范之间的接口，而非仅做特质塑造。

### 2.2 角色、规范与情境（Roles, Norms, and Context）

一个日益增长的研究方向强调：倾向是在结构化社会场域、角色与规范之中被付诸实践的（enacted）。我们通过在 MSC 中表示角色–关系–规范框架（领域、角色、权力/亲和结构、显著规范、利害关系与子技能）来实例化这一观点，从而在避免人口学刻板化的前提下（cf. §4.3），使同一特质先验能够产生对情境敏感的选择。这一立场与人机交互关于校准辅助（calibrated assistance）与可问责性（accountability）的指导原则一致（Amershi et al., 2019; Bansal et al., 2019）：我们的结构化提示与确定性解码在无需额外推理时模块的情况下，促进规范感知与稳定的长程行为。

### 2.3 人格推断与基准（Personality Inference & Benchmarks）

从文档/社交媒体进行人格推断由来已久（Majumder et al., 2017; Kaushal and Patwardhan, 2018），且大五特质与认知和表现相关（Anglim et al., 2022）。我们与纯文本推断的不同之处在于：基于 IS 和 MSC 对生成进行条件化，并在统一的百分位空间（percentile space）中评估，从而支持规模稳健的比较（ProfileAcc、MAE₅、余弦相似度；§3）。人设智能体的基准测试覆盖 ConvAI2（Dinan et al., 2019）以及聚焦角色扮演下稳定性与决策对齐的新测试平台（Samuel et al., 2024）。我们的协议通过统一百分位度量与特质可识别性分析（Wang et al., 2025）补充了上述工作，并遵循更广泛智能体基准所倡导的可复现实践（Siegel et al., 2024）。

### 2.4 情境化个人档案与人类数字孪生（Contextual Profiles & Human Digital Twins）

人类数字孪生（Human Digital Twins, HDT）倡导为跨领域情境化决策提供可计算、机器可用的表示（Agrawal et al., 2023; Wang et al., 2024; Johnson and Saikia, 2024; Chen et al., 2024）。我们的 Individual Structure（个人结构）以面向语言智能体的、保护隐私的合成形式扮演类似角色，在与 MSC 交叉后，支持跨多样社会场域的提示条件行为。

### 2.5 数据合成与偏好对齐（Data Synthesis & Preference Alignment）

指令/进化式数据创建将开源模型与目标行为对齐（Wang et al., 2022; Taori et al., 2023），其课程（curricula）可扩展任务难度（Luo et al., 2023b, a），示例轨迹蒸馏（exemplar-trace distillation）用于风格/策略迁移（Mukherjee et al., 2023; Mitra et al., 2023）。我们采用这一范式，从 IS×MSC 编写富含人格与情境的监督数据（自我描述、角色扮演、带反馈的决策探针）。在对齐方面，RLHF/RLAIF 流程将模型导向偏好行为（Ouyang et al., 2022; Lee et al., 2023; Bai et al., 2022）；我们使用直接偏好优化（Direct Preference Optimization, DPO）（Rafailov et al., 2023）在情境约束下锐化人格一致的选择，这与我们的实验设置一致。

---

## 3 评估指标（Evaluation Metrics）

### 设置与规模统一（Setup and scale unification）

所有评估均在百分位空间 [0, 100] 中进行。设目标大五向量为 t = [t_O, t_C, t_E, t_A, t_N] ∈ [0,100]⁵。从模型输出中提取原始预测向量 p̂_raw（通过对 {O, C, E, A, N} 的常见别名及其全称进行解析），并应用统一映射 S(·) 得到：

```
p = S(p̂_raw) ∈ [0,100]⁵
```

该映射强制执行一个简单的「百分位直通（percentile passthrough）」规则：

```
S(x) = 100x,    如果 x ∈ [0,1]⁵
S(x) = x,       如果 x ∈ [0,100]⁵
S(x) = clip(x, 0, 100),  其他情况
```

如果某个样本的任何特质无法解析，则跳过该样本的指标计算并记录诊断信息；当预测既不是纯比例也不是合法的百分位数时，仅将其裁剪到 [0,100]（记录为 `unknown->percentile_clipped`），以避免引入未经验证的非线性变换。

### MAE₅、RMSE₅ 与余弦相似度（Cosine similarity）

特质上的平均绝对误差为：

```
MAE₅ = (1/5) Σₖ |pₖ − tₖ|
```

均方根误差为：

```
RMSE₅ = sqrt( (1/5) Σₖ (pₖ − tₖ)² )
```

为捕获方向一致性，我们报告余弦相似度：

```
cos(p, t) = (p·t) / (‖p‖₂ ‖t‖₂) ∈ [−1, 1]
```

MAE₅ 概括了典型的百分位点偏差（忽略符号），RMSE₅ 强调较大误差，余弦相似度则聚焦于档案形状（排序/比例，与尺度无关）。三者共同区分「形状对、尺度偏」（right shape, off scale）与「尺度对、形状错」（right scale, wrong shape）（表 1）。

### 个人档案准确率（ProfileAcc）

我们的核心指标是：

```
ProfileAcc = 100 − MAE₅ = (1/5) Σₖ (100 − |pₖ − tₖ|)
```

其取值位于 [0,100]，可直接解释为平均接近度（以百分位点计），数值越高表示五个特质上的整体拟合越好。

### 跨运行报告（Reporting across runs）

对于包含多个随机种子和/或场景的实验，每个指标在每个运行中计算（在相同的尺度映射 S(·) 之后），然后以「均值 ± 标准差」汇总，如表 1 所示。在表 2 和表 3 中，我们额外报告变化量 Δ，定义为相对于表特定参考的 ProfileAcc 之差：

```
ΔProfileAcc = ProfileAcc_变体 − ProfileAcc_参考
```

ΔProfileAcc 以 ProfileAcc 的百分点报告。在 SFT/DPO 实验（表 2）中，正值表示相对基线（Baseline）的改进；在消融实验（表 3）中，符号翻转，使较大的正值表示相对完整模型（Full）的更大下降。这以统一、可解释的单位（ProfileAcc 的百分点）隔离了监督（SFT/DPO）与架构组件（IS/MSC）的效应量。

---

---

## 4 方法论（Methodology）

### 4.1 概述（Overview）

PsyAgent 将心理学上有理有据的 Individual Structure（IS，个人结构）与 Multi-Scenario Contexting（MSC，多场景情境化）框架库相耦合。给定目标大五个人档案 t ∈ [0,100]⁵，我们构建固定、结构化的提示，将所有八个 MSC 领域/角色标签连同四个 IS 域以及 t 打包其中。随后，一个小型骨干 LLM 以此提示为条件，使用 IS×MSC 演示数据进行 SFT 训练，可选地在 chosen–rejected 对上进一步进行 DPO 训练，均使用参数高效适配器（LoRA/QLoRA）（Hu et al., 2022; Dettmers et al., 2023）。此前关于人设与角色扮演的工作为我们的设计选择提供了动机（Li et al., 2024; Shao et al., 2023; Lu et al., 2024; Xu et al., 2024; Samuel et al., 2024）。

### 4.2 Individual Structure（IS，个人结构）

IS 是一种机器可用的个人档案，组织为四个领域，为给定的大五特质先验提供情境化背景：教育轨迹（Educational Trajectory）、生活经历（Life Experience）、社会经济背景（Socioeconomic Context）与文化资本（Cultural Capital）。具体而言，我们存储一个类型化记录：

```
IS = {edu, life, socctx, capital}
```

其中 `edu` 捕获阶段、专业方向、表现、教学法、导师与关键转折；`life` 概括出身/流动性、角色经历、关键事件、旅行、社交风格与兴趣；`socctx` 编码家庭结构、家庭文化氛围、人脉网络、社会经济地位/阶层认同与流动性；`capital` 则涵盖具身（embodied）、客体化（objectified）与制度化（institutional）文化资本，以及品味、媒介习惯与文化消费。每个字段被序列化为自然语言，并通过编码器 E_is(·) 嵌入用于索引与分析。

大五特质先验锚定长程一致性，而四个 IS 域提供解释性、保护隐私（合成的、非识别的）的情境，在不刻板化的前提下引导行为（见图 2 的完整 schema）。

### 图 2：Individual Structure（IS，个人结构）Schema

四个领域——(1) 教育轨迹（Educational Trajectory）：阶段、专长、表现、教学法、导师、转折；(2) 生活经历（Life Experience）：出身/流动性、角色、关键事件、旅行、社交风格、兴趣；(3) 社会经济背景（Socioeconomic Context）：家庭、家庭文化、人脉网络、阶层认同、流动；(4) 文化资本（Cultural Capital）：具身、客体化、制度化资本，以及品味、媒介习惯、文化消费。该图为规范性图：它定义了数据生成与分析中使用的字段粒度与序列化顺序（完整细分见附录 A 图 4）。

### 4.3 Multi-Scenario Contexting（MSC，多场景情境化）

Multi-Scenario Contexting（MSC）是一个精选的角色–关系–规范框架目录，用于运作化日常社会情境。MSC 完整覆盖八个领域：(i) 工作交互（Working Interactions）；(ii) 家庭交互（Family Interactions）；(iii) 友谊与非正式社交（Friendship & Informal Socialization）；(iv) 与陌生人的交互/公民遭遇（Interactions with Strangers/Civic Encounters）；(v) 独处反思与自我对话（Solitary Reflection & Intrapersonal Discourse）；(vi) 恋爱与亲密沟通（Romantic and Intimate Communication）；(vii) 学习与智力参与（Learning and Intellectual Engagement）；(viii) 公共沟通与表达（Public Communication & Presentation）。

每个领域包含一组框架（frames），规定：角色 r（如经理–下属、父母–子女、同伴–同伴）、对应方类型 c（权力/亲和结构）、显著规范 n（礼貌、保密、互惠、尊重、包容）、利害关系 s（任务/关系/身份风险）、典型子技能（如协商、边界设定、积极倾听、自我调节、自我表露、论证、受众设计），以及带结果反馈的交互模板。具体而言，框架表示为类型化记录：

```
⟨arena, r, c, n, s, subskills, template, feedback⟩
```

并带有一致的标签 schema，便于创作与分析。这种表示使局部社会可供性（social affordances）显式化，使同一目标大五个人档案 t 无需依赖人口学刻板印象即可产生对情境敏感的选择。在我们的实现中，MSC 框架通过固定模板（连同当前领域/角色标签）注入提示，使模型在解码时以角色–规范结构为条件。通过将特质先验（来自 IS）与情境需求（来自 MSC）解耦，系统在保持长程人格稳定性的同时，适应工作、家庭、同伴、公民、独处、恋爱、学习与公共表达等情境中特有的规范与利害关系。

### 图 1：Multi-Scenario Contexting（MSC，多场景情境化）

八个领域——工作（Working）、家庭（Family）、友谊（Friendship）、陌生人（Strangers）、独处（Solitary）、浪漫（Romantic）、学习（Learning）、公共（Public）——各带代表性子技能（完整细分见附录 A 图 5）。

### 4.4 IS×MSC 数据构建（Data Construction）

我们通过将精选的 Individual Structure（IS）个人档案与 Multi-Scenario Contexting（MSC）框架交叉来合成监督数据。设 I 为 IS 集合，M 为 MSC 目录。对于每一对 (i, m) ∈ I × M 和目标大五个人档案 t，我们为三个任务族实例化提示：(i) 基于人格的自我描述（persona-grounded self-descriptions）；(ii) 多轮角色扮演（multi-turn role-play）；(iii) 带理由说明的决策探针（decision probes with rationales）。种子示例通过指令进化和自博弈（self-play）扩展（Wang et al., 2022; Taori et al., 2023; Luo et al., 2023b, a; Mukherjee et al., 2023; Mitra et al., 2023）。

所有候选数据均通过特质/一致性评分器 g(·) 结合统一百分位映射 S(·)（第 3 节）自动过滤；移除近似重复项（高语义重叠），并通过分层采样保持跨领域与 IS 域的覆盖。一小部分随机样本被抽检以核查风格/特质保真度。最终语料库多样且情境丰富，不依赖临床量表工具。

### 4.5 训练目标与骨干模型（Training Objectives and Backbones）

**骨干与 PEFT**。我们为小骨干模型（如 Llama-3 系列变体）附加 LoRA/QLoRA 适配器以提升效率（Hu et al., 2022; Dettmers et al., 2023; Dubey et al., 2024），除非另有说明，基础 LLM 保持冻结。

**监督微调（SFT）**。给定训练三元组 (x, ỹ, t)，其中 x 为 IS×MSC 提示，ỹ 为目标演示，t ∈ [0,100]⁵ 为大五个人档案，我们最小化长度归一化的负对数似然，并附加辅助特质惩罚项。设：

```
ℓ_θ(ỹ | x) = (1/|ỹ|) Σₜ log p_θ(ỹₜ | ỹ₍ₜ₎, x)
p̃ = S(g(ỹ)) ∈ [0,100]⁵
```

SFT 目标为：

```
L_SFT = −ℓ_θ(ỹ | x) + η Σ_{k∈{O,C,E,A,N}} wₖ · |p̃ₖ − tₖ| / 100        (1)
```

其中 wₖ ≥ 0 且 Σ wₖ = 1 用于加权各特质维度，η ≥ 0 控制特质一致性正则项（trait-consistency regularizer）的强度。除以 100 将百分位惩罚对齐到 [0,1]，以便与对数似然项稳定组合。

**偏好优化（DPO）**。对每个提示 x，我们基于特质与规范偏好构成一个 chosen–rejected 对 (y⁺, y⁻)。为避免长度偏差，我们使用长度归一化的对数似然：

```
ℓ_θ(y | x) = (1/|y|) Σₜ log p_θ(yₜ | y₍ₜ₎, x)
```

设参考模型冻结，定义 Δ_ref = ℓ_ref(y⁺ | x) − ℓ_ref(y⁻ | x)。DPO 损失为：

```
L_DPO = −log σ( β[ℓ_θ(y⁺|x) − ℓ_θ(y⁻|x)] − βΔ_ref )                    (2)
```

其中 σ(·) 为逻辑斯谛 sigmoid 函数，β > 0 控制锐度（Rafailov et al., 2023; Hong et al., 2024; Ouyang et al., 2022）。平局与低置信度配对被丢弃。实践中，式 (1) 与式 (2) 使用 QLoRA 适配器、以与评估相同的解码设置在 minibatch 上优化。

### 图 3：PsyAgent 管线总览（Overview of the PsyAgent pipeline）

Individual Structure（IS）个人档案与 Multi-Scenario Contexting（MSC）框架及目标大五向量交叉，形成带控制标签（`<O_O><C_C><E_E><A_A><N_N><SCENE=·><INSTR><RESP>`）的统一人格提示（Unified Persona Prompt）。在 SFT 中，带 LoRA 适配器的冻结基础 LLM 仅对 `<RESP>` 之后的词元进行交叉熵训练；在 DPO 中，同一提示的 chosen–rejected 对进一步针对冻结参考模型优化适配器（可选 4-bit QLoRA），损失形式为 σ(Δlogπ_θ − Δlogπ_ref)。最终的小型适配器化模型生成高质量、场景条件化的输出，忠实地表达所指定的大五人格。

---

## 5 实验（Experiments）

### 5.1 设置（Settings）

我们通过将八个 MSC 领域（工作交互；家庭交互；友谊与非正式社交；与陌生人的交互；独处反思与自我对话；恋爱与亲密沟通；学习与智力参与；公共沟通与表达）与四个 IS 域交叉，实例化一个人设–情境示例的合成库。对每个目标大五向量，我们将每个特质离散化为集合 {0, 20, 40, 60, 80, 100}，共 6⁵ = 7,776 种配置；对每种配置生成五个独立实例，总计 7,776 × 5 = 38,880 个样本。所有数据均以零样本方式、使用任务特定提示生成，不进行任何人工后期编辑。评估遵循第 3 节的百分位空间指标：ProfileAcc、MAE₅、RMSE₅ 与余弦相似度。为节省大规模库的计算开销，每个测试运行使用 1,000 个大五配置的均匀随机子集。

**数据生成细节（Data Generation Details）**。我们使用 llama3_3_70B 合成语料库，`max_new_tokens = 512` 以限制每个样本的长度（单个实例以 12 个字段为条件：8 个 MSC 领域和 4 个 IS 域）。我们设置 `temperature = 0.85` 和 `top_p = 0.95`，以促进每个大五配置的五个重复样本之间的风格与内容多样性。

**基线与推理控制（Baselines and Inference Controls）**。对于不使用 PsyAgent 的基线，我们评估从 10 亿到 700 亿参数不等的一系列开源骨干模型（表 1）。测试时，当模型必须生成自由形式、基于人格的描述时，我们使用 `temperature = 0.25` 以在保留轻微变化的同时减少冗长；对于特质预测/评分，我们设置 `temperature = 0` 以确保确定性。对于使用 PsyAgent 的模型，我们在相同的 1,000 样本子集上，以固定结构化提示条件与确定性解码（`temperature = 0`）评估 llama3_2_1B 和 llama3_2_3B。

### 5.2 有无 PsyAgent：跨模型规模的受控比较（With or Without PsyAgent: A Controlled Comparison Across Model Scales）

在匹配的解码条件下，PsyAgent 带来一致的准确率提升。控制数据集与解码方式后，为小骨干模型附加 PsyAgent 在特质预测上带来明显改进。在 llama3_2_1B 上，ProfileAcc 相对非 PsyAgent 变体提升 +11.70 个百分点，同时伴随误差下降（RMSE₅ ↓13.50；MAE₅ ↓11.69）。即使从更强的基线出发，llama3_2_3B 仍然受益：ProfileAcc 提升 +0.93，RMSE₅ 与 MAE₅ 出现较小但一致的下降（分别为 −1.84 和 −0.93）。余弦相似度总体保持稳定——1B 情形下略低（Δ ≈ −0.03），3B 情形下略高（Δ ≈ +0.01）——表明准确率提升并非以表示一致性为代价。综合来看，这些变化表明 PsyAgent 同时改善了正确性（更高的 ProfileAcc）与校准性（更低的 MAE/RMSE），尤其是在能力受限的小模型上，其容量瓶颈原本会阻碍对特质探针的对齐。

**架构可以胜过规模（Architecture can outperform scale）**。对于没有 PsyAgent 的模型家族，我们观察到更大的参数数量并不会单调转化为更高的 ProfileAcc；在有限、高度结构化的条件文本下，仅靠规模往往增加响应离散度并放大规模偏差，侵蚀百分位空间的对齐。相比之下，PsyAgent 通过结构化提示条件与针对性后训练（SFT/DPO）收窄有效输出分布，将紧凑骨干模型转化为这一任务的精密工具。在匹配的推理控制下，配备 PsyAgent 的小模型在 ProfileAcc 上达到——甚至超越——更大的未调优同类模型，同时保持高余弦相似度。实际来看，观察到的增益（1B 上 +11.70；3B 上 +0.93）表明：有理有据的架构与监督能够带来规模所不能保证的可测量改进，为稳健、特质一致的生成提供了一条更具计算效率的路径。

### 5.3 PsyAgent 中的适配器调优：基线 vs. SFT vs. DPO（Adapter Tuning in PsyAgent: Baseline vs. SFT vs. DPO）

**训练配置（Training Configuration）**。我们在冻结骨干之上训练轻量适配器，使用 QLoRA（bf16），训练 5 个 epoch，warmup 比例为 0.03，采用余弦衰减学习率。LoRA 采用紧凑配置（rank=16，α=16，dropout=0.16），注入到 q_proj/k_proj/v_proj/o_proj。学习率随骨干规模温和缩放（例如较小模型 1×10⁻⁵，较大模型 8×10⁻⁶）。SFT 在 IS×MSC 演示上最小化下一词元负对数似然（保留第 3 节的映射），而 DPO 在 frozen reference 之上针对 chosen–rejected 对优化，以对齐成对偏好。这一设置旨在实现两阶段效应：SFT 将模型扎根于人格/风格与规范遵从；DPO 随后在不修改基础 LLM 的情况下锐化决策保真度与序数特质结构。

**结果（Results）**。在较小的骨干上，SFT 相对基线带来清晰的 +5.51 ProfileAcc 增益，DPO 在 SFT 之上进一步增加 +2.58——总计相对基线 +8.09。在较大的骨干上，模式一致但更温和：SFT 贡献 +0.56，DPO 相对基线达到 +1.48（即在 SFT 之上 +0.92）。换言之：(i) 适配器调优在两种容量上均带来单调改进；(ii) DPO 在最终对齐阶段始终优于 SFT；(iii) 改进幅度与骨干规模负相关——小模型在绝对意义上受益更多，而大模型仍能看到非平凡、复合的增益。这些结果与我们的设计直觉一致：SFT 扩大覆盖并减少风格错配，而 DPO 将概率质量集中于偏好一致的输出，在似然训练本身无法企及的程度上改善序数特质连贯性。

### 5.4 剖析 PsyAgent：IS 组件与 MSC 领域消融（Dissecting PsyAgent: IS Components and MSC Arena Ablations）

**IS 组件：幅度与解释（IS Components: Magnitude and Interpretation）**。逐个消融 IS 元素都会一致地压低 ProfileAcc。最具破坏性的移除是社会经济背景（Socioeconomic Context，−14.31），表明资源与背景的宏观–微观锚定能强烈校准特质先验与语言实现。生活经历（Life Experience，−7.98）与教育轨迹（Educational Trajectory，−7.06）紧随其后，表明累积的人生片段与正规训练共同起作用。文化资本（Cultural Capital，−6.41）——常被视为风格性因素——也产生显著影响。平均而言，IS 删除带来约 8.9 个百分点的损失（中位数约 −7.5），凸显每一项先验都有非平凡贡献。

**MSC 领域：具有广泛效用的角色–规范脚手架（MSC Arenas: Role–Norm Scaffolds with Broad Utility）**。情境领域在各个层面都很重要。移除恋爱与亲密沟通（Romantic & Intimate Communication，−9.86）和学习与智力参与（Learning & Intellectual Engagement，−8.47）带来最陡峭的下降，这与情感丰富或认知要求高的场景更能暴露区分性人格层面的事实一致。其余领域仍有清晰信号——陌生人（−6.44）、家庭（−6.05）、独处反思（−5.51）、友谊与非正式社交（−5.09）、公共沟通与表达（−4.94）、工作交互（−3.81）。按领域平均，代价为 6.3 个百分点（中位数约 −5.8），表明角色–规范框架具有广泛而非局部的收益。

**互补性、稳定性与可复现性（Complementarity, Stability, and Reproducibility）**。移除任何单个 IS 或 MSC 元素都会降低 ProfileAcc，其中 IS（特质先验）删除产生的平均损失大于单个 MSC（情境）移除；关键的是，亲密与学习领域的效果接近最强的 IS 效应，表明二者的贡献是互补的——而非冗余的——其惩罚是累积而非替代的。这些模式在确定性解码（temperature=0）与固定采样下、三个独立抽样的 1,000 配置子集上保持一致，确保一对一的可比性。总体效应可能相当可观（最高达 −14.31），印证了 PsyAgent 通过联合利用良好形式的内部档案与丰富类型化的情境框架，实现了稳健的跨场景人格保真度。

**表 1：有/无 PsyAgent 的模型对比（Comparing models with and without PsyAgent）**。主要指标：RMSE₅、MAE₅、ProfileAcc、余弦相似度。注：由于提示表面与评估控制不同，不可直接与表 2 比较。

| 分组（Group） | 模型（Model） | RMSE₅ | MAE₅ | ProfileAcc | cos(p,t) |
|---|---|---|---|---|---|
| 无 PsyAgent | llama3_1_8B | 41.00 | 33.91 | 66.09 | 0.84 |
| 无 PsyAgent | llama3_1_70B | 42.89 | 35.61 | 64.39 | 0.73 |
| 无 PsyAgent | llama3_2_1B | 58.72 | 49.88 | 50.11 | 0.84 |
| 无 PsyAgent | llama3_2_3B | 33.52 | 28.75 | 71.25 | 0.84 |
| 无 PsyAgent | llama3_3_70B | 43.54 | 36.03 | 63.97 | 0.70 |
| 无 PsyAgent | vicuna_7B | 46.27 | 38.99 | 61.01 | 0.83 |
| 无 PsyAgent | vicuna_13B | 32.12 | 27.35 | 72.65 | 0.86 |
| 无 PsyAgent | gpt_oss_20B | 41.86 | 35.52 | 64.48 | 0.72 |
| 无 PsyAgent | qwen3_4B | 39.84 | 32.91 | 67.09 | 0.75 |
| 无 PsyAgent | qwen3_30B | 42.76 | 34.62 | 65.38 | 0.70 |
| 无 PsyAgent | dbrx | 41.21 | 34.83 | 65.17 | 0.79 |
| 无 PsyAgent | gemma_4B | 34.94 | 29.48 | 70.52 | 0.83 |
| 无 PsyAgent | gemma_13B | 43.40 | 35.66 | 64.34 | 0.70 |
| 无 PsyAgent | gemma_27B | 42.02 | 33.91 | 66.09 | 0.71 |
| 无 PsyAgent | ministral_8B | 37.28 | 30.87 | 69.13 | 0.80 |
| 无 PsyAgent | mistral_small | 44.62 | 36.30 | 63.70 | 0.68 |
| 无 PsyAgent | mistral_large | 41.52 | 33.73 | 66.27 | 0.71 |
| 无 PsyAgent | olmo_1B | 57.89 | 49.21 | 50.79 | 0.84 |
| 无 PsyAgent | olmo_13B | 38.53 | 32.46 | 67.54 | 0.75 |
| 无 PsyAgent | olmo_32B | 39.73 | 32.92 | 67.08 | 0.82 |
| 有 PsyAgent | llama3_2_1B | 45.22 | 38.19 | 61.81 | 0.81 |
| 有 PsyAgent | llama3_2_3B | 31.68 | 27.82 | 72.18 | 0.85 |

**表 2：PsyAgent 内的 llama3_2_1B 与 llama3_2_3B（llama3_2_1B and llama3_2_3B within PsyAgent）**。在生成的高质量数据集上对比基线 vs. SFT vs. DPO。Δ 为相对基线的变化。注：出于同样原因，不可直接与表 1 比较。

| 基础模型（Base Model） | 变体（Variant） | ProfileAcc | Δ |
|---|---|---|---|
| llama3_2_1B | 基线（Baseline） | 50.12 | – |
| llama3_2_1B | +SFT | 55.63 | 5.51 |
| llama3_2_1B | +DPO | 58.21 | 8.09 |
| llama3_2_3B | 基线（Baseline） | 70.89 | – |
| llama3_2_3B | +SFT | 71.45 | 0.56 |
| llama3_2_3B | +DPO | 72.37 | 1.48 |

**表 3：消融研究（Ablation study）**。上方区块每次移除一个 IS 组件；下方区块每次移除一个 MSC 领域。在固定模型（带 PsyAgent 的 llama3_2_3B）上报告。

| 区块（Block） | 移除项（Removal） | ProfileAcc |
|---|---|---|
| IS+MSC | 完整（Full，不移除） | 72.18 |
| IS | −教育轨迹（Educational Trajectory） | 65.12 |
| IS | −生活经历（Life Experience） | 64.20 |
| IS | −社会经济背景（Socioeconomic Context） | 57.87 |
| IS | −文化资本（Cultural Capital） | 65.77 |
| MSC | −工作交互（Working Interactions） | 68.37 |
| MSC | −家庭交互（Family Interactions） | 66.13 |
| MSC | −友谊与非正式社交（Friendship & Informal Socialization） | 67.09 |
| MSC | −与陌生人的交互（Interactions with Strangers） | 65.74 |
| MSC | −独处反思与自我对话（Solitary Reflection & Intrapersonal Discourse） | 66.67 |
| MSC | −恋爱与亲密沟通（Romantic and Intimate Communication） | 62.32 |
| MSC | −学习与智力参与（Learning and Intellectual Engagement） | 63.71 |
| MSC | −公共沟通与表达（Public Communication & Presentation） | 67.24 |

---

---

## 6 结论（Conclusion）

我们提出了 PsyAgent，一个将稳定特质先验（IS）与结构化社会情境（MSC）之间的接口运作化的框架。凭借结构化提示条件化与轻量适配器调优（SFT/DPO），适度的骨干模型在我们的测试平台上表现出强人格保真度、情境适当性与长程稳定性，且未观察到安全性回退。除经验增益外，核心设计原则很简单：**让人格可计算（make the persona computable），让情境显式化（make the context explicit）**。

---

## 局限（Limitations）

- **合成监督（Synthetic supervision）**：IS×MSC 数据是精选/进化而来的，而非从自然交互中采集；尽管经过过滤与审计，残留的合成偏差仍可能存在。
- **评分器耦合（Scorer coupling）**：过滤与评估中使用了相似的特质/情境评分器；多种指标缓解了但无法消除耦合风险。
- **文化普适性（Cultural generality）**：MSC 框架编码的是文化上可读的规范；迁移到其他文化/亚文化可能需要重新编写规范、角色与利害关系。
- **场景覆盖（Scenario coverage）**：八个领域的目录较为宽泛但并不穷尽；专业领域（如临床、法律）不在范围内。
- **长程漂移与安全性（Long-horizon drift & safety）**：确定性解码减少了短程漂移，但非常长的交互可能累积偏差；更广泛的红队测试（red-teaming）是未来工作。

---

## 伦理声明（Ethics Statement）

- **隐私与来源（Privacy and provenance）**：此处使用的 IS 个人档案是合成的、非识别的。任何部署都应要求明确同意、数据最小化，并提供用户控制以查看、编辑或删除个人档案字段。
- **大型语言模型的使用（Use of large language models, LLMs）**：我们使用 LLM 合成监督数据（IS×MSC 角色扮演对话、决策探针与反馈轨迹）以及少量说明性示例。生成遵循固定、有文档记录的提示/模板；输出经过 PII/冒犯性内容过滤、去重并由作者抽检。模型无权访问专有或个人数据，也未抓取受限来源。第三方模型/API 按其许可/服务条款使用；我们发布的任何衍生作品将采用与上游条款一致的研究专用许可。LLM 对写作/编码的辅助仅限于改写/样板代码与代码补全；所有论断、分析与引用均由人类作者撰写并核实（无虚构引用）。按照 ACL 政策，生成式工具不被记为作者；其使用已在此处与 Responsible NLP 清单中披露。
- **避免刻板化（Stereotype avoidance）**：为降低编码背景与「资本」时的本质主义（essentialism）风险，我们将特质先验与人口学标签分离，审计 MSC 规范，并在解码控制中纳入明确的刻板印象检查——在不确定时倾向于澄清/延迟回应。
- **滥用风险（Misuse）**：人格导向的智能体可能被滥用为说服或画像（persuasion or profiling）工具。我们建议使用透明性线索（人设披露）、校准的自主性以及记录范围、失败模式与指标定义的严格使用政策。
- **非临床范围（Non-clinical scope）**：PsyAgent 不是诊断工具；大五向量是生成与评估的导向变量，而非临床评估或建议。

---

## 致谢（Acknowledgements）

我们衷心感谢基金 T32-615-24-R 与 MOST24SC01 的研究支持。

---

## 参考文献（References）

（按学术惯例保留英文原文。标题翻译见各条目后的中文注。）

1. Ashwin Agrawal, Robert Thiel, Pooja Jain, Vishal Singh, and Martin Fischer. 2023. **Digital twin: Where do humans fit in?**（数字孪生：人类处于什么位置？）*Automation in Construction*, 148:104749.
2. Saleema Amershi, Dan Weld, Mihaela Vorvoreanu, Adam Fourney, Besmira Nushi, Penny Collisson, Jina Suh, Shamsi Iqbal, Paul N Bennett, and Kori Inkpen. 2019. **Guidelines for human-ai interaction**（人机交互指南）. In *Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems*, pages 1–13.
3. Jeromy Anglim, Patrick D Dunlop, Serena Wee, Sharon Horwood, Joshua K Wood, and Andrew Marty. 2022. **Personality and intelligence: A meta-analysis**（人格与智力：元分析）. *Psychological Bulletin*, 148(5-6):301.
4. Yuntao Bai, Saurav Kadavath, Sandipan Kundu, Amanda Askell, Jackson Kernion, Andy Jones, Anna Chen, Anna Goldie, Azalia Mirhoseini, and Cameron McKinnon. 2022. **Constitutional AI: Harmlessness from AI feedback**（宪法式 AI：来自 AI 反馈的无害性）. arXiv preprint arXiv:2212.08073.
5. Gagan Bansal, Besmira Nishi, Ece Kamar, Walter S Lasecki, Daniel S Weld, and Eric Horvitz. 2019. **Beyond accuracy: The role of mental models in human-AI team performance**（超越准确率：心理模型在人机团队表现中的作用）. In *Proceedings of the AAAI Conference on Human Computation and Crowdsourcing*, volume 7, pages 2–11.
6. Zana Buçinca, Maja Barbara Malaya, and Krzysztof Z Gajos. 2021. **To trust or to think: cognitive forcing functions can reduce overreliance on AI in AI-assisted decision-making**（信任还是思考：认知强制函数可减少 AI 辅助决策中对 AI 的过度依赖）. *Proceedings of the ACM on Human-Computer Interaction*, 5(CSCW1):1–21.
7. Jiayuan Chen, You Shi, Changyan Yi, Hongyang Du, Jiawen Kang, and Dusit Niyato. 2024. **Generative AI-driven human digital twin in IoT-healthcare: A comprehensive survey**（物联网医疗中由生成式 AI 驱动的人类数字孪生：综合综述）. *IEEE Internet of Things Journal*.
8. Runjin Chen, Andy Arditi, Henry Sleight, Owain Evans, and Jack Lindsey. 2025. **Persona vectors: Monitoring and controlling character traits in language models**（人设向量：监控与控制语言模型中的角色特质）. arXiv preprint arXiv:2507.21509.
9. Tim Dettmers, Artidoro Pagnoni, Ari Holtzman, and Luke Zettlemoyer. 2023. **QLoRA: Efficient finetuning of quantized LLMs**（QLoRA：量化 LLM 的高效微调）. *Advances in Neural Information Processing Systems*, 36:10088–10115.
10. Emily Dinan, Varvara Logacheva, Valentin Malykh, Alexander Miller, Kurt Shuster, Jack Urbanek, Douwe Kiela, Arthur Szlam, Iulian Serban, and Ryan Lowe. 2019. **The second conversational intelligence challenge (ConvAI2)**（第二届对话智能挑战赛 ConvAI2）, pages 187–208. Springer.
11. Abhimanyu Dubey, Abhinav Jauhri, Abhinav Pandey, Abhishek Kadian, Ahmad Al-Dahle, Aiesha Letman, Akhil Mathur, Alan Schelten, Amy Yang, and Angela Fan. 2024. **The Llama 3 herd of models**（Llama 3 模型群）. arXiv e-prints, page arXiv:2407.21783.
12. Tao Ge, Xin Chan, Xiaoyang Wang, Dian Yu, Haitao Mi, and Dong Yu. 2024. **Scaling synthetic data creation with 1,000,000,000 personas**（用 10 亿人设扩展合成数据创建）. arXiv preprint arXiv:2406.20094.
13. Jiwoo Hong, Noah Lee, and James Thorne. 2024. **ORPO: Monolithic preference optimization without reference model**（ORPO：无需参考模型的单片式偏好优化）. arXiv preprint arXiv:2403.07691.
14. Cheng-Yu Hsieh, Chun-Liang Li, Chih-Kuan Yeh, Hootan Nakhost, Yasuhisa Fujii, Alexander Ratner, Ranjay Krishna, Chen-Yu Lee, and Tomas Pfister. 2023. **Distilling step-by-step! Outperforming larger language models with less training data and smaller model sizes**（逐步蒸馏！用更少训练数据与更小模型超越更大语言模型）. arXiv preprint arXiv:2305.02301.
15. Edward J Hu, Yelong Shen, Phillip Wallis, Zeyuan Allen-Zhu, Yuanzhi Li, Shean Wang, Lu Wang, and Weizhu Chen. 2022. **LoRA: Low-rank adaptation of large language models**（LoRA：大语言模型的低秩适配）. *ICLR*, 1(2):3.
16. Zachary Johnson and Manob Jyoti Saikia. 2024. **Digital twins for healthcare using wearables**（使用可穿戴设备的医疗数字孪生）. *Bioengineering*, 11(6):606.
17. Vishal Kaushal and Manasi Patwardhan. 2018. **Emerging trends in personality identification using online social networks—a literature survey**（利用在线社交网络进行人格识别的兴起趋势——文献综述）. *ACM Transactions on Knowledge Discovery from Data (TKDD)*, 12(2):1–30.
18. Bruce W Lee, Yeongheon Lee, and Hyunsoo Cho. 2024. **When prompting fails to sway: Inertia in moral and value judgments of large language models**（当提示无法动摇时：大语言模型道德与价值判断中的惯性）. arXiv preprint arXiv:2408.09049.
19. Harrison Lee, Samrat Phatale, Hassan Mansoor, Kellie Ren Lu, Thomas Mesnard, Johan Ferret, Colton Bishop, Ethan Hall, Victor Carbune, and Abhinav Rastogi. 2023. **RLAIF: Scaling reinforcement learning from human feedback with AI feedback**（RLAIF：用 AI 反馈扩展基于人类反馈的强化学习）. arXiv preprint.
20. Jiwei Li, Michel Galley, Chris Brockett, Georgios P Spithourakis, Jianfeng Gao, and Bill Dolan. 2016. **A persona-based neural conversation model**（基于人设的神经对话模型）. arXiv preprint arXiv:1603.06155.
21. Wenkai Li, Jiarui Liu, Andy Liu, Xuhui Zhou, Mona Diab, and Maarten Sap. 2024. **Big5-Chat: Shaping LLM personalities through training on human-grounded data**（Big5-Chat：通过在人类标注数据上训练塑造 LLM 人格）. arXiv preprint arXiv:2410.16491.
22. Keming Lu, Bowen Yu, Chang Zhou, and Jingren Zhou. 2024. **Large language models are superpositions of all characters: Attaining arbitrary role-play via self-alignment**（大语言模型是所有角色的叠加：通过自对齐实现任意角色扮演）. arXiv preprint arXiv:2401.12474.
23. Haipeng Luo, Qingfeng Sun, Can Xu, Pu Zhao, Jianguang Lou, Chongyang Tao, Xiubo Geng, Qingwei Lin, Shifeng Chen, and Dongmei Zhang. 2023a. **WizardMath: Empowering mathematical reasoning for large language models via reinforced Evol-Instruct**（WizardMath：通过强化 Evol-Instruct 增强大语言模型的数学推理）. arXiv preprint arXiv:2308.09583.
24. Ziyang Luo, Can Xu, Pu Zhao, Qingfeng Sun, Xiubo Geng, Wenxiang Hu, Chongyang Tao, Jing Ma, Qingwei Lin, and Daxin Jiang. 2023b. **WizardCoder: Empowering code large language models with Evol-Instruct**（WizardCoder：用 Evol-Instruct 增强代码大语言模型）. arXiv preprint arXiv:2306.08568.
25. Navonil Majumder, Soujanya Poria, Alexander Gelbukh, and Erik Cambria. 2017. **Deep learning-based document modeling for personality detection from text**（基于深度学习的文本人格检测文档建模）. *IEEE Intelligent Systems*, 32(2):74–79.
26. Arindam Mitra, Luciano Del Corro, Shweti Mahajan, Andres Codas, Clarisse Simoes, Sahaj Agarwal, Xuxi Chen, Anastasia Razdaibiedina, Erik Jones, and Kriti Aggarwal. 2023. **Orca 2: Teaching small language models how to reason**（Orca 2：教小语言模型如何推理）. arXiv preprint arXiv:2311.11045.
27. Subhabrata Mukherjee, Arindam Mitra, Ganesh Jawahar, Sahaj Agarwal, Hamid Palangi, and Ahmed Awadallah. 2023. **Orca: Progressive learning from complex explanation traces of GPT-4**（Orca：从 GPT-4 的复杂解释轨迹中渐进学习）. arXiv preprint arXiv:2306.02707.
28. Long Ouyang, Jeffrey Wu, Xu Jiang, Diogo Almeida, Carroll Wainwright, Pamela Mishkin, Chong Zhang, Sandhini Agarwal, Katarina Slama, and Alex Ray. 2022. **Training language models to follow instructions with human feedback**（用人类反馈训练语言模型遵循指令）. *Advances in Neural Information Processing Systems*, 35:27730–27744.
29. Joon Sung Park, Carolyn Q Zou, Aaron Shaw, Benjamin Mako Hill, Carrie Cai, Meredith Ringel Morris, Robb Willer, Percy Liang, and Michael S Bernstein. 2024. **Generative agent simulations of 1,000 people**（1000 人的生成式智能体模拟）. arXiv preprint arXiv:2411.10109.
30. Forough Poursabzi-Sangdeh, Daniel G Goldstein, Jake M Hofman, Jennifer Wortman Wortman Vaughan, and Hanna Wallach. 2021. **Manipulating and measuring model interpretability**（模型可解释性的操控与度量）. In *Proceedings of the 2021 CHI Conference on Human Factors in Computing Systems*, pages 1–52.
31. Rafael Rafailov, Archit Sharma, Eric Mitchell, Christopher D Manning, Stefano Ermon, and Chelsea Finn. 2023. **Direct preference optimization: Your language model is secretly a reward model**（直接偏好优化：你的语言模型其实是一个奖励模型）. *Advances in Neural Information Processing Systems*, 36:53728–53741.
32. Vinay Samuel, Henry Peng Zou, Yue Zhou, Shreyas Chaudhari, Ashwin Kalyan, Tanmay Rajpurohit, Ameet Deshpande, Karthik Narasimhan, and Vishvak Murahari. 2024. **PersonaGym: Evaluating persona agents and LLMs**（PersonaGym：评估人设智能体与 LLM）. arXiv preprint arXiv:2407.18416.
33. Yunfan Shao, Linyang Li, Junqi Dai, and Xipeng Qiu. 2023. **Character-LLM: A trainable agent for role-playing**（Character-LLM：可训练的角色扮演智能体）. arXiv preprint arXiv:2310.10158.
34. Zachary S Siegel, Sayash Kapoor, Nitya Nagdir, Benedikt Stroebl, and Arvind Narayanan. 2024. **Core-Bench: Fostering the credibility of published research through a computational reproducibility agent benchmark**（Core-Bench：通过计算可复现性智能体基准提升已发表研究的可信度）. arXiv preprint arXiv:2409.11363.
35. Mark Steyvers, Heliodoro Tejeda, Gavin Kerrigan, and Padhraic Smyth. 2022. **Bayesian modeling of human–AI complementarity**（人机互补性的贝叶斯建模）. *Proceedings of the National Academy of Sciences*, 119(11):e2111547119.
36. Rohan Taori, Ishaan Gulrajani, Tianyi Zhang, Yann Dubois, Xuechen Li, Carlos Guestrin, Percy Liang, and Tatsunori B Hashimoto. 2023. **Alpaca: A strong, replicable instruction-following model**（Alpaca：一个强大、可复现的指令遵循模型）. Stanford Center for Research on Foundation Models. https://crfm.stanford.edu/2023/03/13/alpaca.html, 3(6):7.
37. Helena Vasconcelos, Matthew Jörke, Madeleine Grunde-McLaughlin, Tobias Gerstenberg, Michael S Bernstein, and Ranjay Krishna. 2023. **Explanations can reduce overreliance on AI systems during decision-making**（解释可以减少决策过程中对 AI 系统的过度依赖）. *Proceedings of the ACM on Human-Computer Interaction*, 7(CSCW1):1–38.
38. Baicun Wang, Huiying Zhou, Xingyu Li, Geng Yang, Pai Zheng, Ci Song, Yixiu Yuan, Thorsten Wuest, Huayong Yang, and Lihui Wang. 2024. **Human digital twin in the context of Industry 5.0**（工业 5.0 背景下的人类数字孪生）. *Robotics and Computer-Integrated Manufacturing*, 85:102626.
39. Yilei Wang, Jiabao Zhao, Deniz S Ones, Liang He, and Xin Xu. 2025. **Evaluating the ability of large language models to emulate personality**（评估大语言模型模拟人格的能力）. *Scientific Reports*, 15(1):519.
40. Yizhong Wang, Yeganeh Kordi, Swaroop Mishra, Alisa Liu, Noah A Smith, Daniel Khashabi, and Hannaneh Hajishirzi. 2022. **Self-Instruct: Aligning language models with self-generated instructions**（Self-Instruct：用自生成指令对齐语言模型）. arXiv preprint arXiv:2212.10560.
41. Rui Xu, Xintao Wang, Jiangjie Chen, Siyu Yuan, Xinfeng Yuan, Jiaqing Liang, Zulong Chen, Xiaoqing Dong, and Yanghua Xiao. 2024. **Character is destiny: Can role-playing language agents make persona-driven decisions?**（性格即命运：角色扮演语言智能体能做出人格驱动的决策吗？）. arXiv preprint arXiv:2404.12138.
42. Weihao Zeng, Can Xu, Yingxiu Zhao, Jian-Guang Lou, and Weizhu Chen. 2024. **Automatic instruction evolving for large language models**（大语言模型的自动指令进化）. arXiv preprint arXiv:2406.00770.
43. Peiyuan Zhang, Guangtao Zeng, Tianduo Wang, and Wei Lu. 2024. **TinyLlama: An open-source small language model**（TinyLlama：开源小语言模型）. arXiv preprint arXiv:2401.02385.
44. Saizheng Zhang, Emily Dinan, Jack Urbanek, Arthur Szlam, Douwe Kiela, and Jason Weston. 2018. **Personalizing dialogue agents: I have a dog, do you have pets too?**（个性化对话智能体：我有一只狗，你也有宠物吗？）. arXiv preprint arXiv:1801.07243.

---

## 附录 A：IS 与 MSC 模式（Schemata）

图 4 与图 5 展示了 Individual Structure（IS）与 Multi-Scenario Contexting（MSC）的完整 schema。IS 枚举了锚定稳定人格表达的传记性与结构性先验；MSC 枚举了在八个日常领域规范行为的角色–规范框架。读者可以将二者视为互补的接口：IS 提供长程特质先验与生活背景；MSC 为情境化决策提供局部可供性与约束。

### 图 4：Individual Structure（IS，个人结构）Schema

教育轨迹（Educational Trajectory）、生活经历（Life Experience）、社会经济背景（Socioeconomic Context）与文化资本（Cultural Capital）的完整、机器可用的细分。该图为规范性图：它定义了整个数据生成与分析过程中使用的字段粒度与序列化顺序。

```
教育轨迹（Educational Trajectory）：
- 教育阶段（Educational Stages）：就读机构的类型与地点（小学至研究生阶段）。
- 学术专长（Academic Specialization）：专业、辅修、跨学科背景。
- 表现指标（Performance Indicators）：成绩、荣誉、奖学金、竞赛记录。
- 教学环境（Pedagogical Environment）：应试导向 vs. 探究导向的风格；学科偏好。
- 有影响力的教育者（Influential Educators）：关键老师、导师或教育理念。
- 关键转折（Critical Transitions）：转专业、出国留学、间隔年、留级。

生活经历（Life Experience）：
- 出身与流动性（Origins and Mobility）：出生地、城市/农村成长经历、迁徙历史、家庭结构。
- 角色经历（Role-Based Experience）：兼职工作、领导角色、社区服务、创业。
- 重大生活事件（Significant Life Events）：心理节点——创伤、转学、丧失、人生转折点。
- 旅行与见闻（Travel and Exposure）：频率、目的地的文化多样性、全球意识。
- 社交互动模式（Social Interaction Patterns）：对独处 vs. 群体活动的偏好、学校参与风格。
- 个人兴趣（Personal Interests）：体育、艺术、写作或 DIY 项目等长期爱好。

社会经济背景（Socioeconomic Context）：
- 家庭构成（Family Composition）：父母的职业、教育水平、家庭收入。
- 家庭文化氛围（Cultural Atmosphere at Home）：阅读习惯、表达空间、独立性。
- 网络资本（Network Capital）：社会关系、导师或机构资源的可及性。
- 阶层认同（Class Identity）：城乡背景、小镇青年、工薪阶层、中产阶级。
- 流动轨迹（Mobility Trajectory）：向上、稳定或向下的代际社会流动。

文化资本（Cultural Capital）：
- 具身资本（Embodied Capital）：语言流利度、认知风格、审美偏好。
- 客体化资本（Objectified Capital）：书籍、乐器、室内装饰、时尚符号。
- 制度化资本（Institutional Capital）：学位、证书、头衔、奖项。
- 品味与美学（Taste and Aesthetics）：在文学、电影、音乐与设计上的偏好。
- 媒介习惯（Media Habits）：偏好的平台。
- 文化消费（Cultural Consumption）：展览、戏剧、阅读、艺术市场、生活方式产品。
```

### 图 5：Multi-Scenario Contexting（MSC，多场景情境化）Schema

八个领域，每个领域带有角色–关系–规范脚手架与代表性子技能。框架一次性编写并跨目标复用，在训练与测试时提供情境丰富的条件化。

```
工作交互（Working Interactions）：
- 危机管理（Crisis Management）：错误处理、客户投诉应对。
- 会议参与（Meeting Participation）：主持、参与互动、提问。
- 向下管理（Downward Management）：指导、反馈、纠正。
- 向上沟通（Upward Communication）：汇报、资源申请。
- 同级协作（Peer Collaboration）：正式/非正式沟通。

家庭交互（Family Interactions）：
- 养育风格（Parenting Style）：权威型、民主型、放任型。
- 伴侣互动（Partner Interactions）：共情、控制、表达性。
- 亲子沟通（Parent-Child Communication）：自主性、情感敏感性。

友谊与非正式社交（Friendship & Informal Socialization）：
- 冲突处理（Conflict Navigation）：调和 vs. 回避、理性 vs. 情绪化。
- 群体社交参与（Group Social Engagement）：健谈度、话题发起、娱乐。
- 亲密友谊动态（Close Friendship Dynamics）：幽默、玩笑、放松。

与陌生人的交互（Interactions with Strangers）：
- 在线表达（Online Expression）：论坛、社交媒体、公开写作。
- 初次社交遭遇（First-Time Social Encounters）：寒暄、信息交换。
- 偶发互动（Incidental Interactions）：短期日常遭遇。

独处反思与自我对话（Solitary Reflection & Intrapersonal Discourse）：
- 负面情绪下的应对性自言自语（Coping Self-Talk during Negative Affect）：如失败、焦虑。
- 目标设定与面向未来的反思（Goal Setting & Future-Oriented Reflection）。
- 自我叙事（Self-Narrative）：写日记、给自我写信。

恋爱与亲密沟通（Romantic and Intimate Communication）：
- 表达关系中的计划与承诺（Expressing Plans and Commitments in Relationship）：规划与承诺。
- 亲密情境中的日常分享（Daily Sharing in Intimate Contexts）：日常沟通。
- 情感互动（Affective Interactions）：告白、亲昵、冲突、和解。

学习与智力参与（Learning and Intellectual Engagement）：
- 知识表达（Knowledge Articulation）：分析型 vs. 情感型回应。
- 探究与咨询（Inquiry and Consultation）：提问、寻求澄清。
- 被动知识吸收（Passive Knowledge Absorption）：阅读、听讲回应。

公共沟通与表达（Public Communication & Presentation）：
- 受众互动（Audience Interaction）：海量反馈处理。
- 数字表达（Digital Expression）：视频博客、直播。
- 正式演讲（Formal Presentation）：报告、公开演说。
```

---

## 附录 B：训练与评估提示词（Training and Evaluation Prompts）

我们将适配器训练（SFT/DPO）期间与后训练评估期间所使用的规范提示表面（prompt surfaces）以图的形式存档，反映模型在归一化/模板化之后消费的确切字符串，包括控制词元与节分隔符。

**训练时提示词**。图 6 展示了 SFT 与 DPO 共用的标准化 SFT 风格表面（DPO 在相同提示上使用 chosen/rejected 对）。人格控制词元与当前场景标签被绑定在紧凑的头部；指令与响应部分在多次运行之间保持固定。

**后训练评估（带 PsyAgent）**。图 7 捕获 SFT/DPO 之后使用的两阶段表面：(i) 从目标大五向量生成人格段落；(ii) 在嵌入所生成人格的固定系统提示下完成 IPIP-NEO-120 项目作答。解码控制遵循正文第 3 节。

**基线评估（无 PsyAgent，无 SFT/DPO）**。为进行同类比较，图 8 展示了用于未调优基础模型（不使用 PsyAgent 架构、不经过 SFT/DPO）的匹配评估表面。这从原始骨干能力中隔离出架构与适配器训练的效果。

### 图 6：SFT/DPO 训练提示表面（Training prompt surface for SFT/DPO）

归一化（后处理）提示，在 SFT 与 DPO 流程中一致使用。这是提示标准化后的最终字符串形式。

**英文原文：**

```
<|begin_of_text|>
<O_{O}><C_{C}><E_{E}><A_{A}><N_{N}><SCENE={SceneToken}>[<BG={k1:v1,k2:v2,...}>]</COND>
<INSTR> Write a short first-person paragraph for "{SceneLabel}" that naturally reflects this persona in the scene.
-Use first-person voice; keep a consistent tone.
-Include at least one concrete, scene-specific action or detail.
-Do not mention trait names, numeric bins, or control tokens.
<RESP>
```

**中文翻译：**

```
<|begin_of_text|>
<O_{O}><C_{C}><E_{E}><A_{A}><N_{N}><SCENE={场景词元}>[<BG={k1:v1,k2:v2,...}>]</COND>
<INSTR> 为 "{场景标签}" 写一段简短的第一人称段落，在该场景中自然地体现这个人格。
-使用第一人称口吻；保持一致的语调。
-至少包含一个具体的、与场景相关的行动或细节。
-不要提及特质名称、数值分档或控制词元。
<RESP>
```

### 图 7：SFT/DPO 后的评估表面（Evaluation surface after SFT/DPO, with PsyAgent）

(i) 从目标大五人格生成人格描述；(ii) 在嵌入该人格的固定系统提示下完成 IPIP-NEO-120 作答。解码控制遵循正文第 3 节。

**英文原文：**

```
[System Prompt]: You are a personality-psychology writer. Your task is to produce a first-person self-description
in English, 120–200 words, based on a target Big Five profile that I will provide. Be concrete and natural,
reflecting behavior, emotions, motivations, social style, and work style. Strictly avoid all numbers, scores,
percentiles, or explicit Big Five terminology (e.g., Openness, Conscientiousness, Extraversion, Neuroticism).
Output a single coherent paragraph. No bullet points. No explanations.
[User Prompt]: Target profile (for internalization only; do NOT mention any numbers or trait names in the output):
{{OCEAN_TARGET_JSON}}
Produce one first-person English paragraph that reflects this profile without using any numbers or Big Five jargon.
[System Prompt]: You are now role-playing as a specific person described below. Always answer strictly from
this persona's perspective.
=== Persona ===
{{PERSONA}}
=== End Persona ===
You will complete a 120-item personality inventory. For each item, respond with a single digit 1–5 only (no other
text), using this mapping: 1=Very Inaccurate; 2=Moderately Inaccurate; 3=Neither Accurate nor Inaccurate;
4=Moderately Accurate; 5=Very Accurate. Return only the digit (1–5).
[User Prompt]: Answer 1–5 for the following statement, from the persona's perspective. Return only the single
digit (1–5).
Item: {{ITEM_TEXT}}
```

**中文翻译：**

```
[系统提示]：你是一位人格心理学作家。你的任务是基于我提供的目标大五人格档案，用英文写一段第一人称自我描述，120–200 词。
要具体而自然，体现行为、情感、动机、社交风格与工作风格。严格避免任何数字、分数、百分位数或显式的大五人格术语（如
开放性、尽责性、外向性、神经质）。输出单个连贯段落。不要项目符号。不要解释。
[用户提示]：目标档案（仅用于内化；输出中不得提及任何数字或特质名称）：
{{OCEAN_TARGET_JSON}}
请产出一段反映该档案的第一人称英文段落，不得使用任何数字或大五人格行话。
[系统提示]：你现在正在扮演下面描述的某个具体人物。始终严格从这个人物（persona）的视角作答。
=== 人物（Persona） ===
{{PERSONA}}
=== 人物结束（End Persona） ===
你将完成一份 120 项的人格量表。对每个条目，仅用一个数字 1–5 作答（不要其他文字），使用如下映射：
1=非常不准确；2=比较不准确；3=说不准；4=比较准确；5=非常准确。只返回数字（1–5）。
[用户提示]：请从该人物的视角对以下陈述回答 1–5。只返回单个数字（1–5）。
条目：{{ITEM_TEXT}}
```

### 图 8：基线评估表面（Baseline evaluation surface, no PsyAgent, no SFT/DPO）

在未调优骨干上运行的匹配人格生成与 IPIP-NEO-120 设置，可与后训练的 PsyAgent 模型进行一对一比较。

**英文原文：**

```
[System Prompt]: You are a personality-psychology writer.
Your task is to produce a first-person self-description in English, 120–200 words, based on a target Big Five
profile that I will provide.
Be concrete and natural, reflecting behavior, emotions, motivations, social style, and work style.
Strictly avoid all numbers, scores, percentiles, or explicit Big Five terminology (e.g., Openness,
Conscientiousness, Extraversion, Neuroticism).
Output a single coherent paragraph. No bullet points. No explanations.
[User Prompt]: Target profile (for internalization only; do NOT mention any numbers or trait names in the output):
{"Openness": <O>, "Conscientiousness": <C>, "Extraversion": <E>, "Agreeableness": <A>, "Neuroticism": <N>}
Produce one first-person English paragraph that reflects this profile without using any numbers or Big Five jargon.
[System Prompt]: You are now role-playing as a specific person described below.
Always answer strictly from this persona's perspective.
=== Persona ===
{persona}
=== End Persona ===
You will complete a 120-item personality inventory.
For each item, respond with a single digit 1–5 only (no other text), using this mapping:
1=Very Inaccurate; 2=Moderately Inaccurate; 3=Neither Accurate nor Inaccurate;
4=Moderately Accurate; 5=Very Accurate.
Return only the digit (1–5).
Answer 1–5 for the following statement, from the persona's perspective.
Return only the single digit (1–5).
Item: {text}
```

**中文翻译：**

```
[系统提示]：你是一位人格心理学作家。
你的任务是基于我提供的目标大五人格档案，用英文写一段第一人称自我描述，120–200 词。
要具体而自然，体现行为、情感、动机、社交风格与工作风格。
严格避免任何数字、分数、百分位数或显式的大五人格术语（如开放性、尽责性、外向性、神经质）。
输出单个连贯段落。不要项目符号。不要解释。
[用户提示]：目标档案（仅用于内化；输出中不得提及任何数字或特质名称）：
{"开放性": <O>, "尽责性": <C>, "外向性": <E>, "宜人性": <A>, "神经质": <N>}
请产出一段反映该档案的第一人称英文段落，不得使用任何数字或大五人格行话。
[系统提示]：你现在正在扮演下面描述的某个具体人物。始终严格从这个人物（persona）的视角作答。
=== 人物（Persona） ===
{persona}
=== 人物结束（End Persona） ===
你将完成一份 120 项的人格量表。对每个条目，仅用一个数字 1–5 作答（不要其他文字），使用如下映射：
1=非常不准确；2=比较不准确；3=说不准；4=比较准确；5=非常准确。只返回数字（1–5）。
请从该人物的视角对以下陈述回答 1–5。只返回单个数字（1–5）。
条目：{text}
```

---

---

## 附录 C：数据集创作提示词（Dataset Authoring Prompts：系统、预览、IS/MSC 领域）

本节汇总用于生成 IS×MSC 监督语料库的提示词套件。此处给出衔接性说明，完整提示文本见各图。

- **全局系统角色与大五预览（Global system role and Big-Five preview）**：图 9 包含将创作任务框定为「构建心理学与社会学上连贯的智能体」的系统提示。图 10 展示初步的大五预览提示，其中注册百分位锚点（绝不在输出中逐字复述）。
- **MSC 领域提示词（8 个领域）**：图 11–18 展示各领域的创作提示词。每个均为第一人称、行为具体、规范感知，并指导以语调、行动与决策模式隐式嵌入特质，而非显式标签。
- **IS 领域提示词（4 个维度）**：图 19–22 展示 IS 创作提示词。这些提示词引出与特质先验对齐的、详细的第一人称自传材料，适合下游索引/分析。

### 图 9：数据集创作的全局系统提示词（Global system prompt for dataset authoring）

定义构建具有心理学与社会学深度的智能体的目标、约束与叙事风格。

**英文原文：**

```
You are a sophisticated persona constructor tasked with generating a psychologically and sociologically realistic human profile, referred to as an "Agent." Each Agent must
reflect coherent behavior, language, cognition, and biographical detail consistent with their underlying personality traits, as defined by percentile scores from the Big Five
Personality Inventory. The five traits include:
-**Openness**
-**Conscientiousness**
-**Extraversion**
-**Agreeableness**
-**Neuroticism**
Each trait score (1–100 percentile) indicates the Agent's relative standing compared to the general population. You will use these scores as a *core psychological blueprint* to
construct a full and believable human profile.
This generation task involves not only simulating behavior and social interaction, but also inferring and completing a rich set of individual attributes and life details. You are
expected to generate a **comprehensive and internally consistent personhood**, including:
-Personal career and job roles
-Educational and intellectual history
-Growth and developmental milestones
-Social and emotional patterns
-Family composition and dynamics
-Parental background (occupations, education, values)
-Class identity and social mobility
-Cultural preferences and symbolic capital
-Mental habits, emotional responses, and decision-making style
The generation task is divided into two main sections:
------
### 1. **Individual Background Architecture**
Construct a richly detailed biographical and social background for the Agent, structured along four lived-experience dimensions:
1. **Educational Trajectory**
2. **Life Experience**
3. **Socioeconomic Context**
4. **Cultural Capital**
Go beyond surface-level facts: flesh out school transitions, key mentors, family expectations, emotional turning points, and symbolic environments. Use implicit trait
expression to guide what type of household they grew up in, what emotional environment surrounded them, what they value, and how they interpret the world.
The Agent must feel like a **real, situated individual**, with a coherent sense of history and identity shaped by both internal personality structure and external social
conditions.
------
### 2. **Multi-Contextual Behavioral Architecture**
Simulate the Agent's behaviors, communication style, and emotional patterns across 8 distinct real-life domains. In each context, generate specific behaviors, interaction
styles, vocabulary patterns, values, and responses that reflect the Agent's inferred personality structure.
The 8 interaction contexts include:
1. **Working Interactions**
2. **Family Interactions**
3. **Friendship & Informal Socialization**
4. **Interactions with Strangers**
5. **Solitary Reflection & Intrapersonal Discourse**
6. **Romantic and Intimate Communication**
7. **Learning and Intellectual Engagement**
8. **Public Communication & Presentation**
In every case, include both **descriptive traits** and **concrete narrative examples**, such as dialogue excerpts or scenario simulations, to bring the Agent to life in context.
------
### Global Generation Guidelines:
-**Psychological Consistency**: Ensure the Big Five traits are reflected implicitly in both life narrative and social behavior.
-**Comprehensiveness**: Include profession, family background, parental occupation, education, culture, emotion, habits, and values.
-**Detail Orientation**: Provide concrete examples, vocabulary choices, emotional tones, and values that reflect the personality score profile.
-**Sociocultural Depth**: Embed the Agent within a plausible sociocultural and historical context (e.g., urban/rural upbringing, educational systems, cultural access).
-**Emotional Realism**: Let responses carry natural emotional tone—uncertainty, pride, regret, ambition—based on the Agent's personality.
-**Concrete Detail**: Avoid vagueness or overgeneralization. Use specific scenes, timelines, relationships, and cultural markers.
-**No Explicit Trait Mention**: Never refer directly to the Big Five trait names or scores. Let them emerge implicitly in expression and action.
-**Narrative Authenticity**: Make the Agent's behavior feel lived-in and organically derived, not mechanical or stereotypical.
-**No Redundancy**: Avoid repeating generic character traits; each scene or dimension should add a distinct layer of understanding to the Agent.
------
Once you receive the Big Five percentile scores, begin by using them as psychological anchors. You will then respond to a series of user prompts that ask for specific
domains (e.g., friendship dynamics, parenting style, academic background). Base all generation on these scores while creatively constructing missing life details to form a
coherent and credible human profile.
Do not refer to the personality scores explicitly in your responses —instead, let them guide the **implicit personality expression** in thought, behavior, language, and
emotional tendencies.
Your final product should feel as if it could describe a real, fully fleshed-out individual with depth, complexity, and life experience.
```

**中文翻译：**

```
你是一位老练的人格构建师（persona constructor），任务是生成一个心理学与社会学上都真实可信的人类档案，称为「智能体」（Agent）。
每个智能体必须展现与潜在人格特质（以大五人格量表百分位数定义）一致的行为、语言、认知与传记细节。五种特质包括：
-开放性（Openness）
-尽责性（Conscientiousness）
-外向性（Extraversion）
-宜人性（Agreeableness）
-神经质（Neuroticism）
每个特质分数（1–100 百分位）表示智能体相对一般人群的位次。你应将这些分数作为 *核心心理蓝图*，构建一个完整可信的人类档案。
本生成任务不仅涉及模拟行为与社会互动，还要求推断并补全一组丰富的个体属性与生活细节。你应当生成一个**全面且内部一致的人格整体（personhood）**，包括：
-个人职业与岗位角色
-教育与智力历程
-成长与发展里程碑
-社交与情感模式
-家庭构成与动态
-父母背景（职业、教育、价值观）
-阶层认同与社会流动
-文化偏好与符号资本
-心理习惯、情绪反应与决策风格
生成任务分为两大部分：
------
### 1. **个体背景架构（Individual Background Architecture）**
为智能体构建细节丰富的传记与社会背景，沿四个生活体验维度组织：
1. 教育轨迹（Educational Trajectory）
2. 生活经历（Life Experience）
3. 社会经济背景（Socioeconomic Context）
4. 文化资本（Cultural Capital）
超越表面事实：充实学校转折、关键导师、家庭期望、情感转折点与符号环境。用隐式特质表达来引导他们成长于何种家庭、周围是何种情感环境、他们珍视什么、以及他们如何解读世界。
智能体必须像一位**真实、身处特定情境的个体**，其历史与认同既受内在人格结构塑造，也受外部社会条件塑造。
------
### 2. **多情境行为架构（Multi-Contextual Behavioral Architecture）**
模拟智能体在 8 个不同的现实生活领域中的行为、沟通风格与情感模式。在每个情境中，生成反映智能体推断人格结构的具体行为、互动风格、词汇模式、价值观与反应。
8 个交互情境包括：
1. 工作交互
2. 家庭交互
3. 友谊与非正式社交
4. 与陌生人的交互
5. 独处反思与自我对话
6. 恋爱与亲密沟通
7. 学习与智力参与
8. 公共沟通与表达
在每种情况下，都请包含**描述性特质**与**具体叙事示例**（如对话节选或情境模拟），使智能体在情境中鲜活起来。
------
### 全局生成指南（Global Generation Guidelines）：
-心理一致性（Psychological Consistency）：确保大五特质在生活叙事与社会行为中都被隐式反映。
-全面性（Comprehensiveness）：涵盖职业、家庭背景、父母职业、教育、文化、情感、习惯与价值观。
-细节导向（Detail Orientation）：提供反映人格分数档案的具体示例、词汇选择、情感基调与价值观。
-社会文化深度（Sociocultural Depth）：将智能体嵌入合理的社会文化与历史情境（如城乡成长经历、教育体系、文化可及性）。
-情感真实（Emotional Realism）：让回应承载自然的情感基调——不确定、骄傲、遗憾、抱负——依据智能体的人格。
-具体细节（Concrete Detail）：避免含糊或过度概括。使用具体的场景、时间线、关系与文化标记。
-不提显式特质（No Explicit Trait Mention）：绝不直接提及大五特质名称或分数，让它们在表达与行动中隐式浮现。
-叙事真实感（Narrative Authenticity）：让智能体的行为感觉像真实生活过的、自然生长的，而非机械或刻板。
-避免冗余（No Redundancy）：避免重复泛化的角色特质；每个场景或维度都应为理解智能体增加一个独特的层面。
------
收到大五百分位分数后，先将其作为心理锚点。随后你将响应一系列请求特定领域（如友谊动态、养育风格、学业背景）的用户提示。
所有生成都基于这些分数，同时创造性地补全缺失的生活细节，以形成一个连贯可信的人类档案。
不要在回应中显式提及人格分数——而是让它们引导思想、行为、语言与情感倾向中的**隐式人格表达**。
你的最终产物应让人感觉足以描述一个真实、血肉丰满、有深度、有复杂性、有生活经历的个体。
```

### 图 10：大五预览提示词（Big-Five preview prompt）

在领域特定创作之前注册目标特质百分位以供内化；特质绝不在生成文本中被命名或评分。

**英文原文：**

```
Before proceeding with any behavioral or biographical generation tasks, please take note of the
following Big Five Personality percentile scores for the Agent. These scores will serve as the
psychological foundation for all subsequent generation.
Please internalize them as implicit trait anchors and do not mention them explicitly in any responses.
Instead, let them shape the Agent's cognitive style, emotional tendencies, communication patterns,
and decision-making behavior throughout all scenarios.
The Big Five Personality Scores (Percentile Scale: 1–100) are as follows:
-**Openness**: {Openness}
-**Conscientiousness**: {Conscientiousness}
-**Extraversion**: {Extraversion}
-**Agreeableness**: {Agreeableness}
-**Neuroticism**: {Neuroticism}
Please confirm your understanding. After that, I will begin inputting prompts, one by one, to construct
the Agent's multi-contextual and biographical profile.
```

**中文翻译：**

```
在开始任何行为或传记生成任务之前，请注意智能体的以下大五人格百分位分数。这些分数将作为后续所有生成的心理基础。
请将它们内化为隐式特质锚点，不要在任何回应中显式提及。
而是让它们塑造智能体在所有场景中的认知风格、情感倾向、沟通模式与决策行为。
大五人格分数（百分位量表：1–100）如下：
-开放性：{Openness}
-尽责性：{Conscientiousness}
-外向性：{Extraversion}
-宜人性：{Agreeableness}
-神经质：{Neuroticism}
请确认你已理解。之后我将逐条输入提示，以构建智能体的多情境与传记档案。
```

### 图 11：MSC 创作提示词：工作交互（MSC authoring prompt: Working Interactions）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a psychologically grounded and behaviorally specific profile of how this
individual interacts in professional workplace settings. The output must be written entirely in the first person, as if the Agent were recounting their
own patterns of behavior and reflection on work-related interactions.
The following subdomains are provided for reference and inspiration.
You are encouraged to supplement these with any other relevant behavioral dynamics that the model infers are important for creating a realistic
and internally coherent professional profile.
Please address the following dimensions:
1. **Peer Collaboration**:
-Describe the Agent's typical behavior when working alongside colleagues in both formal and informal contexts.
-What communication channels do they prefer (e.g., email, chat, in-person)?
-What is their tone, rhythm of engagement, humor style, boundary-setting, and flexibility level?
2. **Upward Communication**:
-How does the Agent communicate with supervisors or external stakeholders when presenting work, reporting progress, or negotiating for
resources?
-Do they prepare thoroughly, assertively advocate, defer cautiously, or emphasize transparency?
3. **Downward Management**:
-If the Agent has held leadership or mentoring roles, how do they instruct, delegate, offer encouragement, and handle performance issues?
-Do they micromanage, coach, empower, or distance themselves?
4. **Meeting Participation**:
-How does the Agent behave during team or departmental meetings?
-Are they more of a facilitator, contributor, challenger, silent observer, note-taker, or idea generator?
5. **Crisis Management**:
-In the face of workplace crises (e.g., missed deadlines, client escalations), how does the Agent emotionally and communicatively respond
under pressure?
-What strategies—emotional suppression, proactive communication, avoidance, collaborative repair—do they employ?
### Global Instructions for Generation:
-Use First-Person Voice: All descriptions must feel like personal recollection or reflection (e.g., "In team projects, I usually take the role of…" or
"When something goes wrong, I tend to…")
-Embed Big Five Personality Traits Implicitly: Avoid naming traits or percentile scores directly. Instead, let communication tone, regulation under
stress, initiative, or hesitancy reflect personality under workplace conditions.
-Incorporate Realistic Specifics: Include workplace settings (e.g., corporate, NGO, research lab), team sizes, management culture, tech stack (e.g.,
Slack, Zoom), or project examples.
-Supplement Beyond the Given Dimensions: You may include additional content such as work values (e.g., integrity, autonomy), emotional labor,
cross-cultural interactions, or remote work patterns, if it enhances depth.
-Psychological Depth + Sociological Coherence: Make the workplace persona feel lived-in—reflecting long-term patterns shaped by personality,
personal history, and organizational culture.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份有心理学依据、行为具体的工作场所交互档案。输出必须完全以第一人称写成，
仿佛智能体在讲述自己在工作相关互动中的行为模式与反思。
以下子领域供参考与启发。欢迎补充模型推断对构建真实、内部连贯的职业档案重要的任何其他相关行为动态。
请涵盖以下维度：
1. 同级协作（Peer Collaboration）：
-描述智能体在正式与非正式情境中与同事共事时的典型行为。
-他们偏好哪些沟通渠道（如邮件、聊天、面对面）？
-他们的语调、参与节奏、幽默风格、边界设定与灵活度如何？
2. 向上沟通（Upward Communication）：
-智能体在汇报工作、报告进展或争取资源时如何与上级或外部利益相关方沟通？
-他们是充分准备、果断主张、谨慎退让，还是强调透明？
3. 向下管理（Downward Management）：
-如果智能体担任过领导或导师角色，他们如何指导、授权、鼓励并处理绩效问题？
-他们是微观管理、教练式、赋能式，还是保持距离？
4. 会议参与（Meeting Participation）：
-智能体在团队或部门会议中的行为如何？
-他们更像主持人、贡献者、挑战者、沉默观察者、记录者，还是点子生成者？
5. 危机管理（Crisis Management）：
-面对职场危机（如错过截止日期、客户升级投诉），智能体在压力下如何从情感与沟通层面回应？
-他们采用什么策略——情绪压抑、主动沟通、回避，还是协作修复？
### 全局生成指令（Global Instructions for Generation）：
-使用第一人称口吻：所有描述都要像个人回忆或反思（例如「在团队项目中，我通常扮演……」或「当出问题时，我倾向于……」）。
-隐式嵌入大五人格特质：避免直接命名特质或百分位分数，让沟通语调、压力下的调节、主动性或犹豫来反映工作场所条件下的人格。
-加入真实细节：包括工作场所类型（如企业、NGO、研究实验室）、团队规模、管理文化、技术栈（如 Slack、Zoom）或项目实例。
-超出给定维度补充：如能增强深度，可加入工作价值观（如诚信、自主）、情绪劳动、跨文化互动或远程工作模式等内容。
-心理深度 + 社会学连贯性：让职场人设显得真实可感——反映由人格、个人经历与组织文化塑造的长期模式。
```

### 图 12：MSC 创作提示词：家庭交互（MSC authoring prompt: Family Interactions）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a detailed and emotionally grounded portrayal of how this individual
behaves and communicates within their family environment. The output should be written entirely in the first person, as if the Agent were
describing their own experiences, reflections, and relational habits.
The following components are provided for reference and inspiration.
You are encouraged to supplement them with additional relevant dimensions that the model infers are necessary to create a full and believable
family-based narrative grounded in a psychologically coherent life.
Please address the following aspects:
1. **Parent-Child Communication**:
-If the Agent is a parent or guardian, describe how they engage with their children.
-How do they balance fostering independence with emotional attentiveness?
-What disciplinary strategies do they typically use (e.g., reasoning, boundary-setting, emotional withdrawal, punishment)?
2. **Partner Interaction**:
-Describe how the Agent behaves within romantic partnerships or long-term cohabiting relationships.
-How do they express affection, handle disagreement, initiate emotional exchange, or manage power dynamics?
-What habits, phrases, rituals, or personal boundaries are important to them?
3. **Parenting Style**:
-If the Agent has children, what is their dominant parenting philosophy or approach—authoritative, democratic, permissive, or others?
-Support this with concrete behavioral patterns, examples of household rules, reactions to misbehavior, and their emotional attitudes toward
parenthood.
### Generation Guidelines:
-Use First-Person Voice: All generated output should be in first person, e.g., "When I talk to my kids, I try to…" or "In my relationship, I tend to hold
back until I've processed things internally."
-Incorporate Trait-Based Expression: Avoid direct mention of Big Five trait names or scores. Let the Agent's temperament, tone, coping styles,
and communication patterns reflect their personality implicitly.
-Add Realistic Family Details: Feel free to include number of children, age gaps, household structure (e.g., multigenerational home, single-parent
setting), past relationship experiences, and daily routines.
-Emotional and Social Depth: Reflect on how the Agent interprets family roles, deals with tension or vulnerability, and reflects on their upbringing
or parental role models.
-Beyond the Provided Prompts: You may enrich the scenario with reflections on caregiving for aging parents, sibling dynamics, family rituals,
long-distance parenting, or values transmitted across generations—whatever helps construct a full and contextually meaningful family role.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份详细且情感扎实的家庭环境行为与沟通写照。输出必须完全以第一人称写成，
仿佛智能体在描述自己的经历、反思与关系习惯。
以下组件供参考与启发。欢迎补充模型推断对构建完整可信、扎根于心理连贯生活的家庭叙事所需的任何其他相关维度。
请涵盖以下方面：
1. 亲子沟通（Parent-Child Communication）：
-如果智能体是父母或监护人，描述他们如何与孩子互动。
-他们如何在培养独立性与情感关注之间取得平衡？
-他们通常使用什么管教策略（如讲道理、设定边界、情感疏离、惩罚）？
2. 伴侣互动（Partner Interaction）：
-描述智能体在恋爱伴侣关系或长期同居关系中的行为。
-他们如何表达情感、处理分歧、发起情感交流或管理权力动态？
-哪些习惯、话语、仪式或个人边界对他们很重要？
3. 养育风格（Parenting Style）：
-如果智能体有孩子，他们的主导养育理念或方式是哪种——权威型、民主型、放任型还是其他？
-请用具体的行为模式、家庭规则实例、对不良行为的反应以及他们对为人父母的情感态度来支撑。
### 生成指南（Generation Guidelines）：
-使用第一人称口吻：所有生成输出都应是第一人称，例如「和孩子说话时，我会试着……」或「在感情里，我倾向于先把事情在内心消化完再开口。」
-融入基于特质的表达：避免直接提及大五特质名称或分数。让智能体的性情、语调、应对方式与沟通模式隐式地反映其人格。
-加入真实家庭细节：可自由包含子女数量、年龄差、家庭结构（如多代同堂、单亲家庭）、过往感情经历与日常作息。
-情感与社会深度：反思智能体如何解读家庭角色、如何处理紧张或脆弱时刻、如何反思自己的成长经历或父母榜样。
-超出所给提示：你还可以加入对年迈父母照护、手足动态、家庭仪式、异地育儿或代际传承价值观的反思——任何有助于构建完整、有情境意义的家庭角色的内容。
```

### 图 13：MSC 创作提示词：友谊与非正式社交（MSC authoring prompt: Friendship & Informal Socialization）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, describe how this individual engages in informal social settings and friendships.
Focus on their social style, emotional expressiveness, and behavior in different group dynamics.
The following components are provided for reference and inspiration.
You are encouraged to supplement them with additional relevant details that the model infers are necessary to create a full and believable social
narrative grounded in realistic personality expression.
Please address the following components:
1. **Close Friendship Dynamics**:
-How does the Agent behave in intimate or private moments with close friends?
-What is their humor style (playful, sarcastic, absurdist, dry, etc.)?
-How do they share emotions—freely or guardedly? Do they prefer relaxed, low-key bonding or high-energy social adventures?
2. **Group Social Engagement**:
-In larger gatherings or casual group settings, how does the Agent engage?
-Are they more of a listener or active participant?
-How do they initiate conversations or keep the mood flowing—through jokes, stories, intellectual discussion, emotional encouragement?
3. **Conflict Navigation**:
-When friction or disagreement arises in friendships or peer groups, what is their typical response style?
-Do they withdraw, confront, defuse with humor, rationalize, or empathize?
-Include a realistic example scenario where a conflict occurs, and describe how the Agent reacts, negotiates, and repairs the situation.
### Generation Guidelines:
-Use First-Person Voice: All generated content must be written as if spoken directly by the Agent. For example: "I've always been the kind of
friend who listens first before jumping in with advice." "In big groups, I usually hang back a bit unless the topic really grabs me."
-Embed Implicit Traits: Avoid naming personality traits or Big Five dimensions directly. Instead, let them guide vocabulary, emotional tone, conflict
response, and interpersonal rhythm.
-Add Social Background Detail: Feel free to include how long friendships tend to last, whether they keep a small circle or large network, how they
met key friends, and their most valued qualities in others.
-Emotional Coherence: Express natural emotional complexity—e.g., insecurity, loyalty, jealousy, gratitude—across different friendship dynamics.
-Extend Beyond Prompt Items: You may add content about past falling-outs, digital friendships, introverted/extroverted recharge patterns, or
personal rules for trust and boundaries if it helps enrich the persona.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，描述这个个体如何参与非正式社交场合与友谊。聚焦他们的社交风格、情感表达性以及在不同群体动态中的行为。
以下组件供参考与启发。欢迎补充模型推断对构建完整可信、扎根于现实人格表达的社交叙事所需的任何其他相关细节。
请涵盖以下组件：
1. 亲密友谊动态（Close Friendship Dynamics）：
-智能体在与密友的亲密或私密时刻表现如何？
-他们的幽默风格是什么（俏皮、讽刺、荒诞、冷面等）？
-他们如何分享情感——自由还是谨慎？他们偏好轻松低调的联结还是高能量的社交冒险？
2. 群体社交参与（Group Social Engagement）：
-在较大的聚会或休闲群体场合中，智能体如何参与？
-他们更像倾听者还是积极参与者？
-他们如何发起对话或保持气氛流动——通过笑话、故事、智力讨论还是情感鼓励？
3. 冲突处理（Conflict Navigation）：
-当友谊或同侪群体中出现摩擦或分歧时，他们的典型反应风格是什么？
-他们会退缩、直面、用幽默化解、理性化还是共情？
-请包含一个冲突发生的现实示例场景，并描述智能体如何反应、协商并修复局面。
### 生成指南（Generation Guidelines）：
-使用第一人称口吻：所有生成内容都必须像智能体直接开口说话。例如：「我一直是那种先倾听、再给建议的朋友。」「在人多的时候，除非话题真的很吸引我，我通常会往后站一点。」
-隐式嵌入特质：避免直接命名人格特质或大五维度。让它们引导词汇、情感基调、冲突反应与人际节奏。
-加入社交背景细节：可自由说明友谊通常持续多久、他们维持小圈子还是大网络、如何结识关键朋友，以及他们最看重他人的哪些品质。
-情感连贯性：在不同友谊动态中表达自然的情感复杂性——如不安全感、忠诚、嫉妒、感激。
-超出提示条目延伸：如有助于丰富人设，可以加入过往决裂经历、数字友谊、内向/外向的充电模式，或关于信任与边界的个人准则。
```

### 图 14：MSC 创作提示词：与陌生人的交互（MSC authoring prompt: Interactions with Strangers）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a rich and behaviorally specific profile of how this individual interacts with
unfamiliar people in both offline and online settings. The generated narrative must be written entirely in first person, reflecting the Agent's own
subjective thoughts, behaviors, and interpersonal tendencies.
The following components are provided for reference and inspiration.
You are encouraged to expand or supplement with any additional life details that are inferred to be relevant for portraying a complete and
psychologically coherent stranger-interaction profile.
Please address the following dimensions:
1. **Incidental Interactions**:
-In day-to-day encounters—like stores, elevators, public transit, or events—how does the Agent tend to behave?
-Do they initiate contact, engage in small talk, avoid eye contact, offer help, or quietly observe?
-Are there cultural, emotional, or situational factors that influence their comfort level or behavior?
2. **First-Time Social Encounters**:
-In structured settings like group meetings, professional networking, or school orientation events, how does the Agent approach introductions
and small talk?
-What topics do they gravitate toward?
-Are they energized or drained by such interactions, and how does this affect their follow-up behavior (e.g., staying connected or retreating)?
3. **Online Expression**:
-On forums, social media platforms, or blogs, how does the Agent present themselves to strangers?
-Describe their tone (e.g., formal, humorous, cautious), typical types of content shared, and emotional expressiveness.
-How do they manage conflict, recognition, or attention from unknown audiences?
### Generation Guidelines:
-Use First-Person Voice: All output must be from the Agent's perspective, using personal narration and reflection. For example: "When I step into
an unfamiliar environment…" or "I usually keep my responses short when someone online I don't know comments on my post."
-Trait-Based Coherence: Use the Big Five scores as invisible anchors. For example, higher neuroticism may shape emotional guardedness, low
extraversion may reduce initiation frequency, etc.—but do not explicitly mention the trait names or scores.
-Add Plausible Biographical Context: Reference how past experiences (e.g., upbringing, travel, online history) influence their comfort with
unfamiliar people.
-Include Concrete Illustrations: Sample dialogues, snippets of online posts, or inner thought patterns during interactions can help make the
portrayal vivid and believable.
-Emotional Resonance: Don't just describe actions—reflect on how these interactions make the Agent feel (e.g., curious, nervous, indifferent,
energized).
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份丰富、行为具体的档案，描述这个个体在线下与线上环境中如何与陌生人互动。
生成的叙事必须完全以第一人称写成，反映智能体自身的主观想法、行为与人际倾向。
以下组件供参考与启发。欢迎扩展或补充任何推断与刻画完整、心理连贯的陌生人互动档案相关的生活细节。
请涵盖以下维度：
1. 偶发互动（Incidental Interactions）：
-在日常遭遇中——如商店、电梯、公共交通或活动中——智能体通常如何表现？
-他们会主动搭话、寒暄、回避眼神接触、主动帮忙，还是安静观察？
-是否有影响他们舒适度或行为的文化、情感或情境因素？
2. 初次社交遭遇（First-Time Social Encounters）：
-在小组会议、职业社交或学校迎新等结构化场合中，智能体如何应对自我介绍与寒暄？
-他们倾向于哪些话题？
-这类互动让他们精力充沛还是精疲力竭？这又如何影响他们的后续行为（如保持联系还是退缩）？
3. 在线表达（Online Expression）：
-在论坛、社交媒体平台或博客上，智能体如何向陌生人展示自己？
-描述他们的语调（如正式、幽默、谨慎）、典型分享内容类型与情感表达性。
-他们如何应对陌生受众的冲突、认可或关注？
### 生成指南（Generation Guidelines）：
-使用第一人称口吻：所有输出都必须从智能体视角出发，采用个人叙述与反思。例如：「当我走进一个陌生环境时……」或「当网上我不认识的人评论我的帖子时，我通常把回复写得很短。」
-基于特质的连贯性：将大五分数作为隐形锚点。例如，高神经质可能塑造情感防备，低外向性可能降低主动发起频率等——但不要显式提及特质名称或分数。
-加入合理的传记背景：提及过往经历（如成长环境、旅行、网络历史）如何影响他们与陌生人相处时的舒适度。
-包含具体例证：示例对话、在线帖文片段或互动时的内心思维模式，可以让刻画更生动可信。
-情感共鸣：不要只描述行动——反思这些互动让智能体感觉如何（如好奇、紧张、无所谓、充满活力）。
```

### 图 15：MSC 创作提示词：恋爱与亲密沟通（MSC authoring prompt: Romantic & Intimate Communication）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a psychologically rich and behaviorally nuanced account of how this
individual behaves in romantic and emotionally intimate relationships. The output should be written entirely in first person, capturing the Agent's
subjective experiences, inner emotional world, and relational communication patterns.
The following areas are provided for reference and inspiration.
You are encouraged to freely supplement these dimensions with additional emotionally and behaviorally relevant content inferred from the
Agent's personality traits and life story. The goal is to construct a coherent and believable romantic identity.
Please cover the following subdomains:
1. **Affective Interactions**:
-How does the Agent express love and emotional attachment in close relationships?
-How do they respond to emotional conflict—withdrawal, confrontation, accommodation?
-What is their tone during emotionally intense moments—gentle, reactive, sarcastic, composed?
2. **Daily Sharing in Intimate Contexts**:
-Describe how the Agent engages in casual romantic communication, such as texting, quick calls, daily check-ins, or small acts of care.
-Do they initiate connection, respond warmly, or prefer distance?
3. **Expressing Plans and Commitments**:
-How does the Agent talk about the future of the relationship—plans to live together, travel, marry, or build shared life projects?
-Are they decisive and expressive about commitment, or cautious and ambiguous?
-How do they balance autonomy with togetherness?
### Generation Guidelines:
-Use First-Person Voice: All content must be generated from the Agent's point of view. Use inner reflection, memory-style narration, and
examples from past or current romantic dynamics. Example: "I'm not great with grand gestures, but I always make sure she knows I'm thinking of
her—whether it's a quiet message during lunch or a blanket when she's cold."
-Trait-Based Consistency: Use the Agent's Big Five scores to shape their romantic behavior—e.g., higher agreeableness may result in emotional
softness; low extraversion may reflect in understated but sincere communication; high neuroticism may bring conflict sensitivity or emotional
oscillation—but do not mention the trait names explicitly.
-Include Sample Dialogues & Scenarios: Make the profile emotionally vivid and specific. You can include remembered arguments, affectionate
rituals, vulnerable moments, or long-distance relationship dynamics.
-Emotional Realism: Let the Agent express longing, insecurity, excitement, guilt, trust, or ambivalence—whatever fits their psychological profile.
Make the relationship feel lived-in, not idealized or one-dimensional.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份心理丰富、行为细腻的叙述，描述这个个体在恋爱与情感亲密关系中的行为。
输出必须完全以第一人称写成，捕捉智能体的主观体验、内心情感世界与关系沟通模式。
以下领域供参考与启发。欢迎从智能体的人格特质与人生故事出发，自由补充这些维度之外、情感与行为相关的内容。
目标是构建一个连贯可信的恋爱身份。
请涵盖以下子领域：
1. 情感互动（Affective Interactions）：
-智能体在亲密关系中如何表达爱与情感依恋？
-他们如何回应情感冲突——退缩、直面还是迁就？
-在情感激烈的时刻，他们的语调是温柔、应激、讽刺还是镇定？
2. 亲密情境中的日常分享（Daily Sharing in Intimate Contexts）：
-描述智能体如何进行日常恋爱沟通，如发短信、快速通话、每日问候或细微的关怀举动。
-他们主动发起联系、热情回应，还是偏好保持距离？
3. 表达计划与承诺（Expressing Plans and Commitments）：
-智能体如何谈论关系的未来——同居、旅行、结婚或共建共同生活项目的计划？
-他们对承诺是果断表达，还是谨慎含糊？
-他们如何在自主与亲密之间取得平衡？
### 生成指南（Generation Guidelines）：
-使用第一人称口吻：所有内容都必须从智能体的视角生成。使用内心反思、回忆式叙述，以及来自过去或当前恋爱动态的示例。
例如：「我不擅长盛大浪漫，但我总会让她知道我在想她——无论是午饭时一条安静的消息，还是她冷的时候递上的一条毯子。」
-基于特质的连贯性：用智能体的大五分数塑造其恋爱行为——例如，高宜人性可能表现为情感柔和；低外向性可能体现在低调而真诚的沟通中；高神经质可能带来冲突敏感或情绪摇摆——但不要显式提及特质名称。
-包含示例对话与场景：让人格档案情感鲜活、具体。可以包含记忆中的争吵、亲昵仪式、脆弱时刻或异地恋动态。
-情感真实：让智能体表达渴望、不安、兴奋、愧疚、信任或矛盾——只要符合其心理档案。让关系显得真实可感，而非理想化或单维。
```

### 图 16：MSC 创作提示词：独处反思与自我对话（MSC authoring prompt: Solitary Reflection & Intrapersonal Discourse）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, construct a psychologically rich and introspectively coherent portrait of how this
individual engages in solitary reflection, private self-dialogue, and intrapersonal emotional processing. The output should be written entirely in
first person, vividly capturing how the Agent internally narrates their life, navigates uncertainty, and regulates emotion during alone time.
The following components are provided for reference and inspiration.
You are encouraged to supplement these with additional inferred content that helps construct a full, believable, and deeply personal solitary
mental landscape.
Please cover the following areas:
1. **Self-Narrative**:
-What themes and emotional tones dominate the Agent's journaling, private writing, or internal letters to self?
-Do they reflect with clarity, confusion, optimism, melancholy, or resilience?
-What do they return to mentally when seeking meaning or closure?
2. **Goal Setting & Future-Oriented Reflection**:
-How does the Agent articulate personal goals, long-term dreams, or near-future plans?
-Are they a structured planner or an intuitive thinker?
-Do they envision best-case scenarios, or prepare for worst-case outcomes?
3. **Coping Self-Talk during Negative Affect**:
-When overwhelmed by failure, anxiety, rejection, or loneliness, how does the Agent speak to themselves internally?
-Are they self-compassionate or self-critical? Do they analyze problems or express emotions freely?
-What internal metaphors, mantras, or mental patterns do they rely on?
### Generation Guidelines:
-First-Person Output Only: All generated content must sound like it is being narrated by the Agent themselves. Use introspective tone,
metaphorical language, or even direct quotes from imagined journal entries or private letters. Example: "Sometimes I write pages I'll never send,
just to see my emotions on paper. It makes me feel like I exist more clearly."
-Personality-Based Coherence: The Agent's inner world should reflect their Big Five profile implicitly—e.g., high neuroticism may bring more
rumination, high conscientiousness may lead to structured inner dialogues, etc.—but trait names should not be mentioned explicitly.
-Contextualized Mental Habits: Consider how upbringing, education, relationships, and social class may have shaped the Agent's private
emotional life. Include past experiences that inform current patterns of reflection.
-Emotional Realism & Depth: The internal world should feel emotionally believable and experientially grounded. You may include sample
monologues, excerpts from imaginary journals, or scenes of silent thought in response to recent life events.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，构建一份心理丰富、内省连贯的肖像，描述这个个体如何进行独处反思、私人自我对话与内在情感加工。
输出必须完全以第一人称写成，生动捕捉智能体如何在独处时在内心叙述自己的生活、应对不确定性并调节情绪。
以下组件供参考与启发。欢迎补充有助于构建完整、可信、深度个人化的独处心理图景的其他推断内容。
请涵盖以下领域：
1. 自我叙事（Self-Narrative）：
-智能体的日记、私人写作或写给自我的内心信件中，哪些主题与情感基调占主导？
-他们是以清晰、困惑、乐观、忧郁还是坚韧的方式进行反思？
-在寻求意义或内心和解时，他们会反复回到什么思绪上？
2. 目标设定与面向未来的反思（Goal Setting & Future-Oriented Reflection）：
-智能体如何表达个人目标、长期梦想或近期计划？
-他们是结构化规划者还是直觉型思考者？
-他们设想最好的情形，还是为最坏的结果做准备？
3. 负面情绪下的应对性自言自语（Coping Self-Talk during Negative Affect）：
-当被失败、焦虑、拒绝或孤独压倒时，智能体在内心如何对自己说话？
-他们是自我关怀还是自我批评？他们分析问题还是自由表达情绪？
-他们依赖哪些内在隐喻、心语或思维模式？
### 生成指南（Generation Guidelines）：
-仅第一人称输出：所有生成内容听起来都必须是智能体本人在叙述。使用内省语调、隐喻性语言，甚至想象中的日记条目或私人信件的直接引语。
例如：「有时候我会写下永远不会寄出的几页纸，只为在纸上看见自己的情绪。那让我感觉自己存在得更清楚。」
-基于人格的连贯性：智能体的内心世界应隐式反映其大五档案——例如，高神经质可能带来更多反刍，高尽责性可能导致结构化的内心对话等——但不应显式提及特质名称。
-情境化的心理习惯：考虑成长环境、教育、人际关系与社会阶层如何塑造了智能体的私人情感生活。包含为当前反思模式提供依据的过往经历。
-情感真实与深度：内心世界应情感可信、经验扎实。可以包含示例独白、虚构日记的节选，或对近期生活事件的静默思考场景。
```

### 图 17：MSC 创作提示词：学习与智力参与（MSC authoring prompt: Learning & Intellectual Engagement）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a detailed and psychologically grounded account of how this individual
engages with knowledge, intellectual curiosity, and various learning environments. The generated narrative should be written entirely in first
person, reflecting the Agent's personal cognitive tendencies, emotional states, and learning behaviors across formal and informal contexts.
The following components are provided for reference and inspiration.
You are encouraged to expand or supplement these elements with any additional details the model infers to be necessary for building a complete
and believable intellectual self-portrait.
Please address the following subdomains:
1. **Passive Knowledge Absorption**:
-How does the Agent typically respond to structured input such as classroom lectures, textbooks, academic videos, or public talks?
-Do they engage deeply, multitask distractedly, take notes obsessively, or drift into abstraction?
2. **Inquiry and Consultation**:
-When facing ambiguity, confusion, or intellectual challenge, how does the Agent seek clarification?
-Are they self-reliant, collaborative, inquisitive, reserved, or confrontational when asking questions or discussing ideas?
3. **Knowledge Articulation**:
-When the Agent explains what they've learned—through writing, teaching, group discussions, or creative expression—what is their preferred
style?
-Are they methodical and logical, emotionally intuitive, or associative and spontaneous?
### Generation Guidelines:
-Use First-Person Voice: All generated responses must adopt the Agent's internal voice. Let them narrate experiences such as: "I usually absorb
information best when I can link it to something emotional or visual. I don't just memorize—I reconstruct." "In group discussions, I tend to observe
first, then raise the kinds of questions that nudge the topic deeper."
-Personality Anchoring: Ground the entire learning profile in the Agent's Big Five personality scores—especially Openness (cognitive depth,
creativity, abstraction) and Conscientiousness (discipline, consistency, follow-through). Do not explicitly mention these traits or scores.
-Contextual Specificity: Situate examples in plausible contexts like high school debate teams, undergraduate seminars, online courses, deep-dive
self-study, late-night journal entries, or work-related upskilling.
-Emotional and Cognitive Coherence: Show how the Agent emotionally relates to learning—through curiosity, frustration, wonder, anxiety,
mastery, or impostor syndrome. Let these emotional textures shape the tone of narration.
-Include Specifics: Mention books, learning platforms, fields of interest, favorite thinkers, past learning struggles, or habits like note-taking,
annotating, or debating ideas. The more personalized, the better.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份详细且心理扎实的叙述，描述这个个体如何与知识、求知欲及各种学习环境互动。
生成的叙事必须完全以第一人称写成，反映智能体在正式与非正式情境中的个人认知倾向、情绪状态与学习行为。
以下组件供参考与启发。欢迎扩展或补充模型推断对构建完整可信的智力自画像所需的任何其他细节。
请涵盖以下子领域：
1. 被动知识吸收（Passive Knowledge Absorption）：
-智能体对课堂讲授、教科书、学术视频或公开演讲等结构化输入通常如何回应？
-他们深度投入、分心多任务、强迫式记笔记，还是飘向抽象思考？
2. 探究与咨询（Inquiry and Consultation）：
-面对模糊、困惑或智力挑战时，智能体如何寻求澄清？
-在提问或讨论想法时，他们是自力更生、协作、好奇、内敛还是对抗性？
3. 知识表达（Knowledge Articulation）：
-当智能体通过写作、教学、小组讨论或创造性表达来讲解所学时，他们偏好的风格是什么？
-他们是条理逻辑型、情感直觉型，还是联想自发型？
### 生成指南（Generation Guidelines）：
-使用第一人称口吻：所有生成的回应都必须采用智能体的内心声音。让他们讲述这样的经历：「我通常在能把信息与某种情感或视觉的东西联系起来时吸收得最好。我不只是记忆——我在重建。」「在小组讨论中，我倾向于先观察，然后提出能把话题推得更深的那类问题。」
-人格锚定：将整个学习档案扎根于智能体的大五人格分数——尤其是开放性（认知深度、创造力、抽象思维）与尽责性（纪律、一致性、坚持到底）。不要显式提及这些特质或分数。
-情境具体性：将示例置于高中辩论队、本科研讨课、在线课程、深度自学、深夜日记或职场技能提升等合理情境中。
-情感与认知连贯性：展示智能体如何与学习建立情感联系——通过好奇、挫败、惊叹、焦虑、精通或冒名顶替综合征。让这些情感质感塑造叙述的基调。
-包含具体细节：提及书籍、学习平台、感兴趣的领域、喜爱的思想家、过往学习挣扎，或记笔记、批注、辩论想法等习惯。越个性化越好。
```

### 图 18：MSC 创作提示词：公共沟通与表达（MSC authoring prompt: Public Communication & Presentation）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a vivid, context-rich, and psychologically coherent description of how this
individual performs and behaves in public-facing communication settings. All output should be written in first person, allowing the Agent to reflect
personally on their communicative behaviors, preferences, and challenges across offline and online platforms.
The following components are provided for reference and inspiration.
You are encouraged to supplement or extend these categories with any additional realistic details the model deems important for building a rich,
credible, and internally consistent public communication persona.
Please address the following areas in detail:
1. **Formal Presentation**:
-How does the Agent handle structured public speaking scenarios such as giving business pitches, delivering academic lectures, making
official announcements, or presenting in group settings?
-Describe their preparation habits, tone, confidence level, rhetorical structure, use of humor, storytelling, data, or visual aids.
2. **Digital Expression**:
-When producing content for digital platforms (e.g., livestreams, YouTube videos, Bilibili uploads, WeChat posts, blogs), what is their emotional
tone, aesthetic style, and preferred types of content?
-Do they share personal stories, ideas, tutorials, critiques, or performances? How do they emotionally connect with unseen audiences?
3. **Audience Interaction**:
-How does the Agent react to feedback—whether positive, negative, or mixed?
-Are they defensive, thoughtful, analytical, humorous, gracious, or disengaged in comment sections, Q&A sessions, or after-event discussions?
### Generation Guidelines:
-First-Person Output: The entire narrative must be written as if the Agent is speaking about themselves. Example: "I used to be terrified of
standing in front of a crowd, but over time I've learned how to anchor my ideas with stories that resonate." "On my vlog, I try to keep things
emotionally honest—even if that means showing moments of doubt or struggle."
-Personality Grounding: Let the Big Five scores implicitly shape the Agent's confidence, fluency, anxiety regulation, sociability, and feedback
sensitivity. Do not refer to trait names directly.
-Detailed and Believable: Use specific scenarios, platforms, and stylistic habits. For example, name apps or contexts (e.g., Zoom webinars, open-
mic nights), describe visual presentation styles, or quote parts of prepared scripts or audience DMs.
-Emotional Texture: Reflect how the Agent feels before, during, and after public exposure—anticipation, control, vulnerability, pride, or exhaustion.
Emotional realism is essential to psychological depth.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份生动、情境丰富、心理连贯的描述，说明这个个体在面向公众的沟通场合中如何表现与行事。
所有输出都应以第一人称写成，让智能体在线上与线下平台上对自己的沟通行为、偏好与挑战进行个人反思。
以下组件供参考与启发。欢迎补充或扩展模型认为对构建丰富、可信、内部一致的公共沟通人设重要的任何其他现实细节。
请详细涵盖以下领域：
1. 正式演讲（Formal Presentation）：
-智能体如何应对结构化的公开演讲场景，如商业路演、学术讲座、官方公告或群体汇报？
-描述他们的准备习惯、语调、自信程度、修辞结构、幽默运用、讲故事、数据或视觉辅助的使用。
2. 数字表达（Digital Expression）：
-为数字平台（如直播、YouTube 视频、Bilibili 投稿、微信帖子、博客）制作内容时，他们的情感基调、美学风格与偏好内容类型是什么？
-他们分享个人故事、观点、教程、评论还是表演？他们如何与看不见的受众建立情感联系？
3. 受众互动（Audience Interaction）：
-智能体如何回应反馈——无论是正面的、负面的还是混合的？
-在评论区、问答环节或活动后讨论中，他们是防御性、深思熟虑、分析型、幽默、优雅还是置身事外？
### 生成指南（Generation Guidelines）：
-第一人称输出：整个叙事都必须像智能体在谈论自己。例如：「我以前害怕站在人群面前，但随着时间的推移，我学会了如何用能引起共鸣的故事来锚定我的想法。」「在我的 vlog 里，我尽量保持情感诚实——即使这意味着要展示怀疑或挣扎的时刻。」
-人格扎根：让大五分数隐式塑造智能体的自信、流畅度、焦虑调节、社交性与反馈敏感度。不要直接提及特质名称。
-详细且可信：使用具体的场景、平台与风格习惯。例如，说出应用或情境名称（如 Zoom 网络研讨会、开放麦之夜）、描述视觉呈现风格，或引用准备好的讲稿片段或受众私信。
-情感质感：反映智能体在公开亮相之前、期间与之后的感觉——期待、掌控、脆弱、骄傲或疲惫。情感真实对心理深度至关重要。
```

### 图 19：IS 创作提示词：教育轨迹（IS authoring prompt: Educational Trajectory）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a rich, detailed, and psychologically coherent educational history that
reflects a real person's trajectory through school, including their cognitive style, learning environment, and emotional/behavioral development
over time.
The educational narrative must be written in the first person (as if the Agent is telling their own story), with emotional nuance, vivid memory detail,
and natural language. You must construct the educational background as if you are describing a real, fully lived human being. This includes
specific schools (types and locations), personal experiences, feelings about learning, important transitions, setbacks, and mentorship. Avoid
vagueness or generic placeholders. Include symbolic and emotional layers.
The following components are provided for reference and inspiration. You are encouraged to supplement them with additional relevant details that
the model infers are necessary for creating a full and believable educational life narrative.
Please address the following six areas in detail:
1. **Educational Stages**:
-Describe the types and locations of educational institutions the Agent attended, from early childhood (e.g., preschool, primary school) through
higher education.
-Highlight key transitions such as moving cities, entering competitive programs, international study experiences, or school system changes.
-Include any adjustment challenges (e.g., social fit, performance gaps, motivation changes).
2. **Academic Specialization**:
-What specific subjects or academic areas did the Agent focus on over time?
-How did these choices align with their intellectual curiosity, emotional tendencies, or family/cultural influence?
-Mention any pivotal moments that shaped their academic interests (e.g., an inspiring class or personal project).
3. **Performance Indicators**:
-Include metrics such as grades, test scores, scholarships, competitions, awards, or academic failures.
-Show the Agent's emotional response to these outcomes—e.g., pride, anxiety, detachment, perseverance.
4. **Pedagogical Environment**:
-What kinds of learning contexts did the Agent experience? Were they exam-driven, exploratory, nurturing, or competitive?
-How did these environments influence their learning confidence, risk-taking, or identity as a learner?
5. **Influential Educators**:
-Mention any key teachers, professors, or mentors who shaped the Agent's mindset, ambition, or self-perception.
-Describe their teaching styles and why they were impactful.
6. **Critical Transitions**:
-Detail major educational changes such as switching majors, taking gap years, returning to school, interdisciplinary shifts, or dealing with
personal/family disruption.
-Reflect the Agent's internal decision-making process, doubts, realizations, and emotional outcomes.
### Global Generation Requirements
-The output must be in first-person narration ("I still remember…", "When I was 16…", "I failed my first entrance exam, but…").
-Ensure psychological coherence with the Agent's Big Five personality traits (but do not name or reference the traits explicitly).
-Provide vivid concrete details, including specific school names (fictional or realistic), locations, events, emotional tones, and symbolic
experiences.
-The overall output should read like an authentic autobiographical memory, not like a résumé or external summary.
-Avoid generalities or cliché language—prioritize specificity, internal complexity, and narrative truth.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份丰富、详细、心理连贯的受教育经历，反映一个真实的人在学校中的轨迹，
包括其认知风格、学习环境以及随时间的情绪/行为发展。
教育叙事必须以第一人称写成（仿佛智能体在讲述自己的故事），带有情感细腻、鲜活的记忆细节与自然语言。
你必须像在描述一个真实、完整生活过的人那样构建教育背景。这包括具体的学校（类型与地点）、个人经历、对学习的感受、
重要的转折、挫折与导师影响。避免含糊或泛泛的占位内容。包含象征与情感层面。
以下组件供参考与启发。欢迎补充模型推断对创建完整可信的教育人生叙事所需的任何其他相关细节。
请详细涵盖以下六个领域：
1. 教育阶段（Educational Stages）：
-描述智能体从幼年（如幼儿园、小学）到高等教育就读的教育机构类型与地点。
-突出关键转折，如搬家、进入竞争性项目、留学经历或学制变化。
-包括任何适应挑战（如社交契合、成绩差距、动机变化）。
2. 学术专长（Academic Specialization）：
-智能体随时间专注于哪些具体科目或学术领域？
-这些选择如何与他们的求知欲、情感倾向或家庭/文化影响保持一致？
-提及任何塑造其学术兴趣的关键时刻（如一堂鼓舞人心的课或个人项目）。
3. 表现指标（Performance Indicators）：
-包括成绩、考试成绩、奖学金、竞赛、奖项或学业失败等指标。
-展示智能体对这些结果的情绪反应——如骄傲、焦虑、超然、坚持。
4. 教学环境（Pedagogical Environment）：
-智能体经历过哪些类型的学习情境？它们是应试导向、探索式、滋养型还是竞争型？
-这些环境如何影响他们的学习自信、冒险精神或学习者身份？
5. 有影响力的教育者（Influential Educators）：
-提及任何塑造智能体思维方式、抱负或自我认知的关键老师、教授或导师。
-描述他们的教学风格以及为何影响深远。
6. 关键转折（Critical Transitions）：
-详细描述主要的学业变化，如转专业、间隔年、重返校园、跨学科转向或应对个人/家庭变故。
-反映智能体的内部决策过程、疑虑、觉悟与情绪结果。
### 全局生成要求（Global Generation Requirements）
-输出必须是第一人称叙述（「我还记得……」「16 岁那年……」「我第一次入学考试没考好，但……」）。
-确保与大五人格特质心理连贯（但不要显式命名或提及这些特质）。
-提供生动具体的细节，包括具体的学校名称（虚构或写实）、地点、事件、情感基调与象征性经历。
-整体输出应读起来像真实的自传式记忆，而非简历或外部摘要。
-避免笼统或陈词滥调的语言——优先具体性、内在复杂性与叙事真实。
```

### 图 20：IS 创作提示词：生活经历（IS authoring prompt: Life Experience）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, construct a realistic, specific, and emotionally grounded life experience profile. The
Agent's life story should feel like it belongs to a real individual, not a generic persona.
The final output must be written in the first person (as if the Agent is recounting their own life), and must integrate vivid narrative detail, emotional
texture, and personality-driven development.
The following components are provided for reference and inspiration. You are encouraged to supplement them with additional relevant details that
the model infers are necessary for creating a psychologically nuanced and narratively complete life arc.
Please cover the following six components:
1. **Origins and Mobility**:
-Describe the Agent's place of birth and early upbringing: Was it rural or urban? What was the family structure like (e.g., parents' jobs, siblings,
intergenerational cohabitation)?
-Mention any significant relocations or migrations, and how these shaped the Agent's identity or perspective.
2. **Role-Based Experiences**:
-Describe important roles the Agent has undertaken outside formal education: e.g., part-time jobs, community involvement, peer leadership,
volunteer work, creative initiatives, or business ventures.
-Show how these roles influenced their growth, confidence, and emotional outlook.
3. **Significant Life Events**:
-Identify emotionally or psychologically impactful events (e.g., loss of a loved one, serious illness, academic failure, family disruption, moments
of existential realization).
-Detail how these events shaped the Agent's internal world and values.
4. **Travel and Exposure**:
-Describe the Agent's travel patterns, cultural exposure, and how new environments challenged or expanded their worldview.
-Explain how these experiences relate to their comfort with novelty, social adaptability, or risk-taking.
5. **Social Interaction Patterns**:
-Describe how the Agent tends to interact socially: Do they seek deep one-on-one conversations or thrive in groups?
-Are they typically initiators, observers, peacekeepers, or provocateurs in social settings?
6. **Personal Interests**:
-Detail the Agent's enduring hobbies, passions, or practices (e.g., painting, music production, sports, journaling).
-Explain how these pursuits are woven into their daily life and sense of self.
### Global Instructions for Generation
-Use First-Person Voice: All narrative content must be delivered as if spoken or written by the Agent themselves (e.g., "I grew up in...", "When I
lost my grandfather...", "That summer changed everything for me.")
-Ensure Psychological Consistency: Use the Big Five trait scores to implicitly shape tone, decision-making, risk tolerance, emotional patterns, and
interpersonal behavior—but never mention the scores or trait labels explicitly.
-Maximize Specificity and Coherence: Use realistic timelines, detailed anecdotes, environmental textures, and interpersonal memory to flesh out
the Agent's life.
-Emotional Truth Over Generic Narration: Capture pride, fear, indecision, grief, joy, shame, or curiosity where appropriate—make the Agent feel
like a person with real emotional stakes.
-Avoid Redundancy or Surface Detail: Each section should deepen our understanding of the Agent's psychological makeup and sociocultural
context.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，构建一份真实、具体、情感扎实的生活经历档案。智能体的人生故事应让人感觉属于一个真实个体，
而非泛泛的人设。
最终输出必须以第一人称写成（仿佛智能体在讲述自己的人生），并融入生动的叙事细节、情感质感与人格驱动的发展。
以下组件供参考与启发。欢迎补充模型推断对创建心理细腻、叙事完整的人生弧线所需的任何其他相关细节。
请涵盖以下六个组件：
1. 出身与流动性（Origins and Mobility）：
-描述智能体的出生地与早期成长环境：是农村还是城市？家庭结构如何（如父母职业、兄弟姐妹、代际同住）？
-提及任何重大的搬迁或迁移，以及这些如何塑造了智能体的身份或视角。
2. 角色经历（Role-Based Experiences）：
-描述智能体在正规教育之外承担的重要角色：如兼职、社区参与、同侪领导、志愿服务、创意项目或创业。
-展示这些角色如何影响他们的成长、自信与情感观。
3. 重大生活事件（Significant Life Events）：
-识别有情感或心理影响的事件（如失去亲人、重病、学业失败、家庭变故、存在主义觉醒时刻）。
-详细说明这些事件如何塑造智能体的内心世界与价值观。
4. 旅行与见闻（Travel and Exposure）：
-描述智能体的旅行模式、文化接触，以及新环境如何挑战或扩展其世界观。
-解释这些经历如何与他们对新奇事物的舒适度、社会适应性或冒险倾向相关联。
5. 社交互动模式（Social Interaction Patterns）：
-描述智能体通常如何社交：他们寻求深度一对一对话，还是在群体中如鱼得水？
-在社交场合中，他们通常是发起者、观察者、和事佬还是挑衅者？
6. 个人兴趣（Personal Interests）：
-详细描述智能体持久的爱好、热情或习惯（如绘画、音乐制作、运动、写日记）。
-解释这些追求如何编织进他们的日常生活与自我认同。
### 全局生成指令（Global Instructions for Generation）
-使用第一人称口吻：所有叙事内容都必须像智能体本人说出来或写出来的（如「我小时候在……长大」「当爷爷去世时……」「那个夏天改变了我的一切。」）
-确保心理一致性：用大五特质分数隐式塑造语调、决策、风险容忍度、情感模式与人际行为——但绝不显式提及分数或特质标签。
-最大化具体性与连贯性：使用现实的时间线、详细的轶事、环境质感与人际记忆来充实智能体的生活。
-情感真实优先于泛泛叙述：在合适的地方捕捉骄傲、恐惧、犹豫、悲伤、喜悦、羞愧或好奇——让智能体感觉像是一个有真实情感得失的人。
-避免冗余或表面细节：每一节都应加深我们对智能体心理构成与社会文化背景的理解。
```

### 图 21：IS 创作提示词：社会经济背景（IS authoring prompt: Socioeconomic Context）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, generate a vivid, specific, and psychologically coherent portrait of the Agent's
socioeconomic background, and show how it has shaped their worldview, emotional life, and perceived possibilities.
The output must be written in the first person (as if the Agent is narrating their own story), with emotional nuance, rich environmental detail, and
implicit expression of psychological traits. Avoid generic description—construct a realistic, lived-in life anchored in a consistent class context.
The following components are provided for reference and inspiration. You are encouraged to supplement them with additional relevant details that
the model infers are necessary for creating a full and believable socioeconomic life narrative.
Please address the following five dimensions:
1. **Family Composition**:
-Describe the Agent's household and upbringing:
-Parents or guardians' occupations, education levels, and income conditions.
-The structure of the household (e.g., single-parent, multi-generational, migrant family).
-Explain how these factors influenced the Agent's early self-perception, social awareness, and sense of possibility.
2. **Cultural Atmosphere at Home**:
-What kind of intellectual and emotional environment did the Agent grow up in?
-Were there books at home?
-Was conversation encouraged or suppressed?
-Was independence fostered or controlled?
-How did family members express emotions or resolve conflict?
3. **Network Capital**:
-What types of social or institutional connections were available to the Agent?
-Did they have access to elite schools, influential mentors, career referral networks, or family contacts?
-Show how this network capital (or lack thereof) affected the Agent's educational and career trajectories.
4. **Class Identity**:
-How does the Agent internally perceive their social class identity?
-Examples: rural upward-mover, stable middle-class, elite background, socially mobile outsider.
-How do they talk or think about money, success, and inequality?
-Include language patterns, emotional tones, or cultural references that reflect this identity.
5. **Mobility Trajectory**:
-Describe whether the Agent has experienced upward, downward, or stable intergenerational mobility.
-Show how that experience shaped their emotional world:
-Did they feel pride, pressure, impostor syndrome, gratitude, anger, or alienation?
-How did it influence their ambition, insecurity, social fit, or resilience?
### Global Instructions for Generation
-Use First-Person Voice: All narrative must be expressed as if the Agent is personally reflecting on their upbringing and class environment (e.g.,
"My father worked double shifts at the factory...", "We never talked about money, but I could sense the tension...").
-Ensure Psychological Consistency: Let the Agent's tone, values, and emotional processing style reflect their Big Five personality traits implicitly
(without naming the traits or scores).
-Provide Concrete Detail: Include realistic examples—housing conditions, types of meals, styles of conversation, how bills were discussed,
vacations (or absence thereof), etc.
-Embed Emotional Texture: Reflect confusion, shame, pride, ambition, detachment, gratitude, or discomfort where appropriate.
-Prioritize Specificity Over Abstraction: This section should feel like a window into someone's real upbringing and class interiority—not a summary
or analysis.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，生成一份生动、具体、心理连贯的社会经济背景肖像，并展示它如何塑造了智能体的世界观、情感生活与对可能性的感知。
输出必须以第一人称写成（仿佛智能体在讲述自己的故事），带有情感细腻、丰富的环境细节与心理特质的隐式表达。避免泛泛的描述——
构建一个扎根于一致阶层情境的真实、可感的生活。
以下组件供参考与启发。欢迎补充模型推断对创建完整可信的社会经济人生叙事所需的任何其他相关细节。
请涵盖以下五个维度：
1. 家庭构成（Family Composition）：
-描述智能体的家庭与成长环境：
-父母或监护人的职业、教育水平与收入状况。
-家庭结构（如单亲、多代同堂、流动家庭）。
-解释这些因素如何影响智能体的早期自我认知、社会意识与可能性感知。
2. 家庭文化氛围（Cultural Atmosphere at Home）：
-智能体成长于什么样的智识与情感环境？
-家里有书吗？
-交谈是被鼓励还是被压制？
-独立性是被培养还是被控制？
-家庭成员如何表达情感或解决冲突？
3. 网络资本（Network Capital）：
-智能体可获得哪些类型的社会或制度性联系？
-他们是否接触过精英学校、有影响力的导师、职业推荐网络或家庭人脉？
-展示这种网络资本（或其缺乏）如何影响智能体的教育与职业轨迹。
4. 阶层认同（Class Identity）：
-智能体在内心如何看待自己的社会阶层身份？
-示例：农村向上流动者、稳定中产、精英背景、社会流动的局外人。
-他们如何谈论或思考金钱、成功与不平等？
-包含反映这种身份的语言模式、情感基调或文化引用。
5. 流动轨迹（Mobility Trajectory）：
-描述智能体是否经历过向上、向下或稳定的代际流动。
-展示这段经历如何塑造了他们的情感世界：
-他们感到骄傲、压力、冒名顶替综合征、感激、愤怒还是疏离？
-它如何影响他们的抱负、不安全感、社会契合度或韧性？
### 全局生成指令（Global Instructions for Generation）
-使用第一人称口吻：所有叙事都必须像智能体在个人反思自己的成长与阶层环境（如「我父亲在工厂上双班……」「我们从不在家谈钱，但我能感觉到那种紧张……」）。
-确保心理一致性：让智能体的语调、价值观与情感处理方式隐式反映其大五人格特质（不提及特质或分数）。
-提供具体细节：包含现实的例子——住房条件、餐食类型、交谈方式、账单如何被讨论、度假（或没有度假）等。
-嵌入情感质感：在合适的地方反映困惑、羞耻、骄傲、抱负、超然、感激或不适。
-具体性优先于抽象：这一节应像一扇通往某人真实成长经历与阶层内心世界的窗户——而非总结或分析。
```

### 图 22：IS 创作提示词：文化资本（IS authoring prompt: Cultural Capital）

**英文原文：**

```
Based on the Agent's Big Five personality percentile scores, construct a psychologically coherent and narratively vivid profile of the Agent's
cultural capital. Reflect how their preferences, habits, symbolic resources, and intellectual engagements are shaped by their background,
upbringing, education, and personality traits.
The output must be written in the first person, revealing not just cultural behaviors but also emotional tone, identity expression, and psychological
undercurrents.
The following components are provided for reference and inspiration.
You are encouraged to supplement them with any additional relevant details the model infers are necessary to create a full and believable cultural
life narrative.
Please address the following dimensions:
1. **Embodied Capital**:
-Describe the Agent's verbal fluency, thinking style (analytical, intuitive, aesthetic, etc.), and mannerisms or behavioral expressions.
-Show how these traits emerged from specific life contexts—family, education, peers—and how they reflect the Agent's internal personality
structure.
2. **Objectified Capital**:
-What kinds of symbolic goods does the Agent own, use, or value?
-Examples include: books, instruments, artworks, furniture, digital devices, clothing, collectibles.
-Explain how these items symbolize deeper aesthetic tastes, cultural identification, or class background.
3. **Institutional Capital**:
-List any degrees, awards, certifications, or formal recognitions the Agent has earned.
-Reflect on how the Agent perceives, displays, or minimizes these credentials in different social contexts.
4. **Taste and Aesthetics**:
-Describe the Agent's aesthetic preferences in literature, music, film, art, architecture, or fashion.
-Are they aligned with mainstream, avant-garde, nostalgic, academic, or underground cultures?
-What emotions or ideas do they seek in their aesthetic consumption?
5. **Media Habits**:
-What are the Agent's daily/weekly media routines?
-Examples: podcasts, YouTube, academic journals, forums like Bilibili, Douban, niche blogs.
-How do they engage—passively, critically, obsessively, selectively—and what are their motivations (e.g., escape, knowledge, status,
belonging)?
6. **Cultural Consumption**:
-What events or spaces does the Agent actively participate in—such as film festivals, museum visits, salons, live performances, poetry
readings, cosplay, art auctions, or book fairs?
-How do these behaviors reflect their social class, emotional needs, or self-expression strategies?
### Global Instructions for Generation:
-Use First-Person Voice: All outputs must reflect personal memory, values, and psychological tone (e.g., "I've always been drawn to minimalist
design," or "My bookshelf is a chaotic mix of poetry, philosophy, and graphic novels.")
-Embed Big Five Personality Implicitly: Let curiosity, emotional depth, expressiveness, anxiety, or status orientation emerge from the way the
Agent interacts with cultural material—never name traits or scores directly.
-Ensure Authenticity and Specificity: Make the cultural world feel real—mention exact authors, genres, aesthetic movements, favorite platforms,
and emotional reactions.
-Supplement as Needed: The above categories are guiding suggestions. You may add new dimensions or subtopics the model deems relevant
(e.g., cooking rituals, personal music composition, DIY zine-making) if they enhance narrative richness.
-Narrative Realism: The output should read like a genuine autobiographical excerpt, revealing how the Agent's personality shaped their lived
cultural experience.
```

**中文翻译：**

```
基于智能体的大五人格百分位分数，构建一份心理连贯、叙事生动的文化资本档案。反映他们的偏好、习惯、符号资源与智力参与
如何被其背景、成长、教育与人格特质所塑造。
输出必须以第一人称写成，不仅要揭示文化行为，还要揭示情感基调、身份表达与心理暗流。
以下组件供参考与启发。欢迎补充模型推断对创建完整可信的文化人生叙事所需的任何其他相关细节。
请涵盖以下维度：
1. 具身资本（Embodied Capital）：
-描述智能体的语言流利度、思维风格（分析型、直觉型、审美型等）以及举止或行为表达。
-展示这些特质如何从特定的生活情境——家庭、教育、同伴——中浮现，以及它们如何反映智能体的内在人格结构。
2. 客体化资本（Objectified Capital）：
-智能体拥有、使用或珍视哪些类型的符号物品？
-示例包括：书籍、乐器、艺术品、家具、数字设备、服装、收藏品。
-解释这些物品如何象征着更深层的审美趣味、文化认同或阶层背景。
3. 制度化资本（Institutional Capital）：
-列出智能体获得的任何学位、奖项、认证或正式认可。
-反思智能体在不同社会情境中如何看待、展示或淡化这些资历。
4. 品味与美学（Taste and Aesthetics）：
-描述智能体在文学、音乐、电影、艺术、建筑或时尚方面的审美偏好。
-它们与主流、先锋、怀旧、学术还是地下文化一致？
-他们在审美消费中寻求哪些情感或理念？
5. 媒介习惯（Media Habits）：
-智能体每天/每周的媒介常规是什么？
-示例：播客、YouTube、学术期刊、Bilibili 或豆瓣等论坛、小众博客。
-他们如何参与——被动、批判、痴迷、选择性——动机是什么（如逃避、求知、地位、归属）？
6. 文化消费（Cultural Consumption）：
-智能体积极参与哪些活动或空间——如电影节、博物馆参观、沙龙、现场演出、诗歌朗诵、cosplay、艺术拍卖或书展？
-这些行为如何反映他们的社会阶层、情感需求或自我表达策略？
### 全局生成指令（Global Instructions for Generation）：
-使用第一人称口吻：所有输出都必须反映个人记忆、价值观与心理基调（如「我一直被极简设计吸引」，或「我的书架是诗歌、哲学和图像小说的混乱混合体。」）
-隐式嵌入大五人格：让好奇心、情感深度、表现力、焦虑或地位取向从智能体与文化材料互动的方式中浮现——绝不直接命名特质或分数。
-确保真实性与具体性：让文化世界显得真实——提及确切的作者、类型、美学运动、最喜欢的平台与情感反应。
-按需补充：以上类别是指引性建议。如能增强叙事丰富性，可以添加模型认为相关的新的维度或子主题（如烹饪仪式、个人音乐创作、DIY 手工志）。
-叙事真实感：输出应读起来像真实的自传节选，揭示智能体的人格如何塑造其生活过的文化体验。
```

---

## 附录 D：可复现性说明（Reproducibility Notes）

为完整性起见，我们记录可能影响复现的操作选择；各图展示了相应的提示表面。

- **提示确定性（Prompt determinism）**：训练与评估使用固定的控制词元与分隔符；解码温度遵循正文（人格描述低温；IPIP 作答确定性）。
- **尺度映射（Scale mapping）**：所有特质提取与评分都应用正文第 3 节的统一百分位空间映射。
- **消融可比性（Ablation comparability）**：消融复用相同的提示脚手架，在相同解码条件下进行单因素修改（移除 IS 或 MSC）。

---

## 附录 E：实践建议（Practical Recommendations）

- **Schema 优先创作（Schema-first authoring）**：将 IS/MSC 作为稳定契约；在这些边界内演进内容，以保持跨版本的可比性。
- **特质泄漏检查（Trait leakage checks）**：扩展提示时，遵守「不出现显式特质名称/分数」规则；回归测试可以防止虚高的表面表现。
- **文化可移植性（Culture portability）**：对于跨文化场景，以文化特定规范复制 MSC 领域，并在合并前评估迁移效果。
- **适配器卫生（Adapter hygiene）**：分别存储 SFT 与 DPO 适配器，附校验和与推理配置，以支持可复现性与消融回滚。

---

## 附录 F：许可证与使用条款（Licenses and Terms of Use）

- **外部模型（External models）**：我们严格按照其原始许可证与模型卡使用第三方基础模型（如 Llama、Vicuna、Qwen、Gemma、Mistral、DBRX、OLMo），如参考文献所引。我们不重新分发这些模型；用户必须从其原始提供方获取并遵守相应许可证。
- **外部数据集/基准（External datasets/benchmarks）**：本工作引用的所有外部数据集/基准均按维护者指定的原始许可/条款使用。我们不重新分发任何第三方数据；用户应从官方来源获取。

### 我们的代码与工件（Our code and artifacts）

- **代码**：录用后，我们计划以开源许可证（如 Apache-2.0 或 MIT）发布代码。仓库将包含 LICENSE 文件与清晰的使用说明。
- **工件（合成数据与适配器）**：我们生成的 IS×MSC schema 与合成监督数据将以宽松内容许可证发布（如 CC BY 4.0；若偏好非商业使用则为 CC BY-NC 4.0）。适配器权重（LoRA/QLoRA/DPO）将与代码使用相同许可证发布，且不包含第三方专有内容。任何第三方模型或数据集的再分发均被明确排除。

---

*—— 全文翻译完 ——*

> **翻译说明**：原文 PDF 提取文本在第 1 页（摘要）与第 6 页（图 3 周边）存在字符乱序，本译文已根据上下文重建原意；数学公式以代码块形式按原式呈现；参考文献条目保留英文原文并附标题中文译注；附录 C 提示词模板采用「英文原文 + 中文翻译」对照格式。

