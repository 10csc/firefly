# 通过人设化大语言模型实现自动化众包测试（Towards Automated Crowdsourced Testing via Personified-LLM）

> 原文出处：Proc. ACM Softw. Eng. 3, FSE, Article FSE166 (2026 年 7 月)，23 页
> DOI：https://doi.org/10.1145/3808173

**作者**：于晟成（Shengcheng Yu），德国慕尼黑工业大学；凌宇辰（Yuchen Ling）、房春荣（Chunrong Fang）、陈振宇（Zhenyu Chen），中国南京大学软件新技术国家重点实验室；陈春阳（Chunyang Chen），德国慕尼黑工业大学

---

## 摘要

软件的快速普及与日益复杂化要求具备稳健的质量保障能力，其中图形用户界面（GUI，Graphical User Interface）测试扮演着关键角色。众包测试（crowdsourced testing）通过利用人类测试者的多样性，在不同设备、用户行为和使用环境下获得丰富、基于场景的覆盖，已被证明在这一背景下行之有效。与此同时，自动化测试——尤其是随着大语言模型（LLM，Large Language Model）的出现——在可控性、可复现性和效率方面具有显著优势，能够实现可扩展且系统化的探索。然而，自动化方法往往缺乏人类测试者特有的行为多样性，限制了其完整模拟真实世界测试动态的能力。为弥补这一差距，我们提出了 **PersonaTester**，一个新颖的基于人设化大语言模型（personified-LLM）的框架，旨在实现自动化众包 GUI 测试。通过将具有代表性的用户人设（persona）注入基于 LLM 的智能体（agent）——这些 persona 沿三个正交维度定义：**测试思维方式（testing mindset）、探索策略（exploration strategy）与交互习惯（interaction habit）**——PersonaTester 能够以可控且可重复的方式模拟多样化的类人测试行为。实验结果表明，PersonaTester 忠实地再现了真实众包测试者（crowd worker）的行为模式，展现出强烈的组内一致性（intra-persona consistency）与清晰的组间变异性（inter-persona variability）（较基线提升 117.86%–126.23%）。此外，人设引导（persona-guided）的测试智能体持续生成更有效的测试事件，并触发比无人设基线更多的崩溃（100+ 个）与功能性缺陷（11 个），从而显著提升了自动化众包 GUI 测试的真实性与有效性。

**CCS 概念**：• 软件及其工程 → 软件测试与调试。

**附加关键词与短语**：软件测试（Software Testing）、众包测试（Crowdsourced Testing）、大语言模型（LLM）、大语言模型人设化（LLM Personification）

**ACM 引用格式**：
Shengcheng Yu, Yuchen Ling, Chunrong Fang, Zhenyu Chen, and Chunyang Chen. 2026. Towards Automated Crowdsourced Testing via Personified-LLM. Proc. ACM Softw. Eng. 3, FSE, Article FSE166 (July 2026), 23 pages. https://doi.org/10.1145/3808173

---

## 1 引言

随着现代软件系统复杂性与交互性的不断提高，确保稳健的质量保障变得比以往任何时候都更为关键 [55]。在各种测试范式之中，众包测试已作为一种强大的手工测试策略脱颖而出，它通过在线平台招募分散的人类测试者群体（即众包测试者，crowd worker）的集体智慧来评估软件质量 [8, 42]。作为传统手工测试的可扩展延伸，众包测试能够快速招募来自不同背景、设备与地区的人员，从而捕获更广泛的真实世界用户行为。这种多样性在场景覆盖、功能探索与缺陷发现方面带来了显著收益，尤其是对于具有复杂或用户中心界面的系统 [50]。众包测试者贡献了因文化、教育、领域知识、测试思维方式与输入偏好差异而形成的异构交互风格，有助于触发传统测试难以发现的缺陷。uTest、百度众测（Baidu CrowdTest）与陌测（MoocTest）等平台在工业界和学术界均被广泛采用，已在 Web、移动与桌面软件上组织了数千场测试活动 [43, 44]。

众包测试的一个关键优势在于其放大行为多样性的能力。众包测试者因教育背景、文化、领域知识、测试思维方式与输入偏好的差异而贡献出异构的交互风格，产生丰富多样的测试轨迹（trace）。这种多样性提升了功能级与场景级的覆盖率，并能发现常规内部测试（in-house testing）经常遗漏的边缘缺陷与可用性问题。例如，游戏开发中的 α 测试与 β 测试常依赖众包来发现地域或设备特定问题。先前研究表明，众包测试报告经常捕获难以仅通过自动化测试复现的真实故障场景 [6, 15]。

然而，众包测试的人工特性也带来了固有局限 [6, 53, 57]。协调开销、结果差异性、缺乏可复现性以及高昂的人力成本，使其难以在迭代开发周期中持续扩展。虽然多样性是其最大优势，但它也在维持测试可靠性、覆盖可追溯性与反馈整合方面带来挑战。这些问题促使人们对将众包测试演化为更可扩展、更自动化的范式产生了日益浓厚的兴趣——该范式既能模拟人类行为的广度，又能最大限度减少人类参与的弊端。自动化测试的最新进展 [1, 18, 19, 27, 32, 39]，尤其是由大语言模型（LLM）驱动的进展 [22]，为此提供了契机。LLM 具备强大的推理能力，能够在极少人工监督下生成具有语义意义的测试动作。然而，传统的基于 LLM 的智能体往往缺乏真实人类测试者所表现出的行为变异性和交互丰富性 [4, 22, 23]，因此在复现众包测试中多样的探索路径方面效果不佳。大多数自动化方法采用固定策略，导致测试行为重复而狭窄，无法反映真实世界的使用多样性。

为弥补这一差距，我们提出 **PersonaTester**——一个新颖的框架，通过将众包测试者的人设融入基于 LLM 的测试工作流，将传统众包测试演化为自动化众包测试范式。PersonaTester 通过显式建模三个正交维度来驾驭人类行为多样性：**测试思维方式、探索策略与交互习惯**，这些维度源自对真实众包测试轨迹的大规模分析。这些 persona 作为轻量级认知画像（cognitive profile），引导 LLM 驱动的决策过程，使每个自动化测试实例都能体现独特而真实的测试行为。通过将人类启发的多样性与 LLM 提供的可扩展性和可控性相结合，PersonaTester 保留了人类智能与人工智能二者的优势。与其仅仅通过知识图谱（即应用侧知识）等机制整合人类领域知识，测试者的人格特质或交互偏好等人类侧信息同样应考虑进来以提升多样性。本质上，PersonaTester 将众包测试重新构想为——不是纯粹的手工流程 [52]，而是人类测试者的集体行为模式与 LLM 推理能力之间的协同合作。这种范式转变开辟了可扩展、可复现、类人的软件测试新方向，在不依赖人力的情况下将众包多样性带入自动化测试生成。

PersonaTester 引入了结构化的灵活框架用于人设化 LLM GUI 测试，由四个紧密集成的组件构成：**LLM 人设化建模（LLM personification modeling）、GUI 状态理解（GUI state understanding）、基于 LLM 的决策（LLM-based decision-making）以及带反馈验证的操作执行（operation execution with feedback validation）**。该框架的核心是其人设化机制，该机制沿三个正交维度——测试思维方式、探索策略与交互习惯——以经验为基础（empirically grounded）的 persona 来建模 LLM 测试智能体的行为。这些维度刻画了认知取向（如顺序式 vs. 发散式逻辑）、交互偏好（如点击、输入驱动动作或核心功能优先）与输入风格（如短而有效、长边界或无效值）。通过对真实众包测试报告的经验分析，我们构建了九种代表性 persona，反映了实践中观察到的多样化测试行为。Persona 被显式注入 LLM 的提示词（prompt）中，确保 LLM 智能体的决策保持一致、可复现，并与真实行为模式对齐。

在每个测试会话中，LLM 智能体迭代式运作。流程始于稳健的 GUI 感知（GUI perception）：屏幕截图经由结合计算机视觉技术与多模态大语言模型（MLLM，Multimodal LLM）的混合流水线处理，以提取文本、结构与空间的小部件（widget）信息。过滤机制移除静态或不可交互元素，并识别瞬态组件（如下拉菜单），随后 GUI 被文本化为 JSON 表示，以支持精确的下游推理。LLM 智能体随后接收包含结构化 GUI 状态、测试历史与所分配 persona 的上下文提示词，首先生成高层测试意图（例如"修改闹钟设置"）以明确推理目标，随后生成与意图和智能体交互习惯相一致的具体操作。这一两步过程提升了可解释性与一致性，使得测试序列在语义上丰富、行为上具有意义。生成的操作根据映射的 GUI 坐标执行。执行之后，PersonaTester 通过基于意图的状态检查（intent-based state checking）以及基于 MLLM 的视觉语义缺陷检测来验证操作效果。这种连接推理、执行与验证的闭环设计，实现了智能、自主的 GUI 测试，真实地模拟人类众包测试者的多样化策略。

实验结果表明，所提出的 PersonaTester 能够有效模仿人类众包测试者，从而实现并增强众包测试的自动化。PersonaTester 在组内一致性与组间差异性上较基线提升了 117.86%–126.23%。具体而言，具有相同 persona 的智能体始终表现出高度相似的探索趋势，与其分配的 persona 特征紧密对齐；而具有不同 persona 的智能体则表现出显著不同的探索行为。此外，通过注入代表多样化测试思维方式、探索策略与交互习惯的 persona，人设引导的智能体能更有效地生成测试事件，与 persona 属性密切相关，并且始终优于未注入 persona 的智能体。此外，与基线相比，人设引导的智能体发现了更多崩溃缺陷（100+）与功能性缺陷（11 个），凸显了 PersonaTester 在自动化与增强众包 GUI 测试方面的有效性与实用性。

本文值得注意的贡献可归纳如下：

- 本文提出 **PersonaTester**，一个新颖的框架，切实实现了人设化 LLM 智能体模拟多样化且真实的类人 GUI 测试行为，这是**首个将"persona"概念应用于软件测试的工作**。
- 本文引入**结构化的三维 persona 方案**，系统地建模了测试探索的思维方式、探索策略与交互习惯。
- 经验实验表明，**人设引导的 LLM 智能体提升了测试多样性与有效性**，在缺陷触发方面优于非人设化基线。

---

## 2 预备研究

为使 PersonaTester 的设计植根于真实测试实践，我们首先对大规模众包 GUI 测试报告语料展开调研。该分析使我们能够将人类测试者的常见行为模式抽象为结构化的 persona 维度与配置，作为我们人设化测试框架的基础。随后我们给出一个示例，展示不同人设引导的智能体如何以不同方式探索同一测试任务。这些工作激发了模拟多样化类人测试行为的必要性，凸显了将 persona 建模集成到自动化众包测试中的潜力。

### 2.1 来自真实众包测试的 Persona 调研

为确保 persona 建模的真实性与代表性，我们对真实世界的众包 GUI 测试行为进行了实证调研。我们从众包测试报告公开数据集（取自使用最广泛且在学术上被广泛研究的平台之一¹ [6, 51, 56]）中随机抽取了 1,500 条 GUI 探索轨迹。该数据集包含约 23,000 份报告，覆盖 50 多个软件系统，涉及 1,100 多名不同的众包测试者。值得注意的是，该平台上的测试过程并不为众包测试者分配固定任务；他们只获得描述应用功能的需求文档。这种开放式测试设置鼓励测试者自主而自然地探索，使个体行为特征得以更清晰地表达。

通过对抽样轨迹的定性分析，我们识别出众包测试者行为中三个反复出现且具有判别性的方面：**测试思维方式**（如探索是顺序、有条理式的，还是发散、机会式的）、**探索策略**（如强调可点击元素、输入交互或核心功能路径）以及**交互习惯**（如测试操作中输入文本的性质与长度）。这三个维度分别从高层思维方式、中层策略与具体的低层习惯层面描述了众包测试者的用户交互。这些维度不仅在轨迹中一致地反复出现，也与先前关于人类测试行为与软件测试多样性的工作相吻合。它们共同提供了一个紧凑而富有表现力的空间，用于在测试生成中建模众包测试者的差异性。

随后我们对 1,500 条轨迹按这三个维度进行人工标注。标注由三位作者独立完成，并通过共识最终确定。在 18 种可能组合（2×3×3）中，我们识别出 9 种频率超过 1% 的 persona 配置，占比从 2.27% 到 21.80% 不等。这 9 种配置合计占所有观测轨迹的 95.40%，其余组合均低于 1% 阈值。因此我们在 PersonaTester 中选择这些有经验依据的 9 种 persona，在行为多样性与真实世界代表性之间取得平衡。值得注意的是，该子集在所有维度上满足成对覆盖（pairwise coverage）[26]，确保每个属性在多种组合中均有体现。该设计支持全面评估不同 persona 特质如何影响测试行为与缺陷发现，同时保持理论完备性与实践可行性。

### 2.2 示例说明

为展示人设引导的基于 LLM 的 GUI 测试所能实现的行为多样性与实际测试效果，我们给出一个示例，涉及由配置了不同 persona 的 PersonaTester 执行的三个测试会话：Persona B、Persona C 与 Persona E（人设引导 LLM 智能体的设计细节见 3.1 节）。分配给 LLM 智能体的具体任务是探索给定应用的闹钟管理功能。每个 persona 定义了测试思维方式、探索策略与交互习惯的独特组合，进而驱动智能体在测试执行过程中的行为（见图 1）。该示例突出展示了不同 persona 如何导致与人类测试者在众包测试中的差异性相对应的差异化探索路径，并从多个视角揭示潜在问题。

**图 1. 动机示例：不同 persona 的人设化 LLM 智能体的探索轨迹**

- Persona B（路径 1）：测试思维方式 A. 顺序且连贯（sequential & coherent），探索策略 b. 核心功能导向（core function focused），交互习惯 ii. 有效且短输入（valid & short input）
- Persona C（路径 2）：测试思维方式 A. 顺序且连贯，探索策略 c. 输入导向（input oriented），交互习惯 iii. 无效输入（invalid input）
- Persona E（路径 3）：测试思维方式 B. 发散且非线性（divergent & non-linear），探索策略 a. 点击导向（click oriented），交互习惯 ii. 有效且短输入

在第一种情况（图 1 中路径 1）中，配备 Persona B 的智能体表现出顺序且连贯的测试思维方式、核心功能导向的探索策略以及提供短而有效输入的习惯。该智能体遵循任务导向、系统化的探索模式：先点击开关关闭现有闹钟，随后进入已停用闹钟的编辑页，导航至配置闹钟类型的菜单，选择"指定星期天开启（On Specified Week Days）"选项，最后点击"保存"按钮确认更改。这一探索不仅与 Persona B 的定义行为画像高度吻合，还发现了一个功能性缺陷：编辑已关闭的闹钟并修改其类型后，更新后的闹钟无法重新开启或删除。该缺陷正是由"打开已关闭的闹钟 → 更改其类型 → 保存更改"这一操作序列触发的，而缺乏上下文与目的驱动推理的标准自动化测试策略几乎不可能遇到该条件。这里的缺陷发现说明了将人设驱动的探索逻辑融入测试过程的重要性。

相比之下，配备 Persona C 的智能体（图 1 中路径 2）同样遵循顺序且连贯的测试思维方式，但配置了输入导向的探索策略与生成短而无效输入的习惯。该智能体先点击"+"按钮添加新闹钟，随后与时间设置界面交互，尝试在小时和分钟输入区域输入无效字符，然后点击"保存"按钮。该轨迹代表了评估输入处理逻辑健壮性的宝贵测试路径。这种行为反映了该 persona 倾向于识别不当输入场景——这是缺乏对抗性交互模式概念的传统自动化工具经常遗漏的一类测试。

第三种轨迹（图 1 中路径 3）由 Persona E 驱动，反映了显著不同的行为风格。凭借发散且非线性的测试思维方式、点击导向的探索策略以及对短而有效交互的偏好，该智能体先点击"+"按钮开始测试，但很快偏离到次要设置中。它选择"自定义（Customize）"选项，打开"振动（Vibration）"开关，进入"声音和振动"设置页，并打开"音频通道（Audio Channel）"配置菜单。这种行为是好奇、探索型用户的典型表现，使得超出主要功能范围的 GUI 状态得到测试。这种探索通过遍历结构化探索策略可能忽略的外围路径，为覆盖多样性贡献良多。

总体而言，这三条探索轨迹展示了不同 persona 如何引导基于 LLM 的智能体产生独特而有意义的测试行为：Persona B 通过系统化的任务聚焦导航发现了关键功能性缺陷，Persona C 测试了输入验证的边界条件，Persona E 则通过非线性交互遍历了较少探索的 GUI 路径。关键在于，只有通过人设引导的 LLM 探索才能实现这种行为分化。依赖统一、确定性策略的传统自动化 GUI 测试框架缺乏模拟多样化用户视角的灵活性。相比之下，PersonaTester 的人设驱动设计使基于 LLM 的测试智能体能够采用类人的推理模式与行为意图，实现真实、自适应且有效的 GUI 探索。这一组示例强调了将人设化 LLM 智能体集成到众包 GUI 测试中的实际价值——在增强众包 GUI 测试自动化的同时，保留了人类测试者行为的丰富性与变异性。

---

## 3 方法论

PersonaTester 通过由人设化 LLM 智能体驱动的结构化流水线，将人类样式的行为多样性集成到众包测试自动化中（图 2）。我们首先对人设进行形式化定义，分别表示测试思维方式、探索策略与交互习惯。基于对真实众包测试行为的经验分析，我们选择九种代表性 persona 来引导 LLM 行为。迭代流程随后从 GUI 状态理解开始：应用 GUI 截图使用 CV 与多模态模型处理，以提取并持久化结构化的 widget 表示。测试生成通过两步提示流程进行——先是意图形成（intent formulation），然后是人设对齐的操作生成（persona-aligned operation generation）——从而增强可解释性与一致性。最后，操作被执行，其结果通过意图检查与基于 MLLM 的缺陷触发来验证。这一端到端框架将探索自动化与众包测试者的行为真实性相结合，为 GUI 测试提供了一种新颖范式。

**图 2. PersonaTester 工作流总览**

（图中包含：真实世界众包测试报告 → 人设引导智能体；widget 识别（CV+OCR）→ 静态信息移除（MLLM）→ 瞬态 widget 识别（MLLM）→ widget 标注与文本化（LLM）→ widget 持久化（缓存）；ReAct 推理机制：身份与任务规范 → 去重 → LLM 上下文 + 历史 + 当前 GUI 状态 → 人设引导提示（LLM）→ 意图生成 → 操作生成 → 坐标映射 → 操作执行；意图检查（MLLM）→ 缺陷触发（MLLM）；人设维度：测试思维方式（顺序且连贯 / 发散且非线性）、探索策略（时钟导向 / 核心功能导向 / 输入导向）、交互习惯（短且有效 / 长且有效 / 无效输入））

### 3.1 面向 LLM 的人设化（Personification for LLM）

为模拟真实众包测试中观察到的行为多样性，我们采用基于 persona 的建模方法，借鉴人机交互（HCI，Human-Computer Interaction）与对话式 AI 领域的既有概念。一般而言，persona 指一个虚构但基于数据的用户原型表示，常用于系统设计与评估中建模用户需求、行为与目标 [10]。在 LLM 应用语境中，persona 已被用于通过将角色特定属性嵌入提示词来塑造响应风格、推理逻辑与领域对齐 [13]。在这些基础之上，我们将框架中的 persona 定义为一种抽象测试者画像，封装了众包测试者中经常观察到的特定行为倾向与决策偏好。在真实众包测试报告经验分析的指导下，我们引入一种结构化、可解释的方案，将人类测试行为分解为三个正交维度：**测试思维方式**（探索过程中的认知取向）、**探索策略**（偏好的交互目标）与**交互习惯**（输入生成风格）。这种结构化表述支持系统化地实例化多样化的人设化 LLM 测试智能体，每个智能体模拟一种独特而真实的测试者画像，以人类样式的行为多样性丰富自动化 GUI 测试。这是首个将"persona"应用于软件测试的工作。

**图 3. LLM 人设化提示词示例（Persona F）**

（提示词片段示例：）
- 你是一名众包测试者，正在测试一款名为"{{APP_NAME}}"的{{APP_TYPE}}应用{{SCENARIO-SPEC}}。
- 你是一名偏好发散思维的探索型测试者。测试中你采用直觉式、好奇心驱动的方法，经常根据在 UI 中所见或所发现的内容改变测试路径。你容易被屏幕上新的或意想不到的元素分散注意力，目标是通过探索发现边缘情况与意外行为。例如，测试某个功能时，你中途注意到一个"帮助（Help）"按钮并出于好奇点击它。此后，你可能返回原任务，也可能因视觉好奇心引发而分支到新的测试路径。——【测试思维方式】
- 同时，你是一名输入型探索者，总是优先并专注于点击输入框与输入文本来驱动交互。但你不重复与同一输入框交互。此外，如果当前页面没有输入框或只有已测试过的输入框，你会尽力通过点击找到一个输入框。例如：…… ——【探索策略】
- 在当前会话中，你已执行以下操作：…… 当前页面结构为…… 你的任务是（1）生成一个测试意图……，（2）然后确定一个具体操作……。返回一个 JSON 对象为……。对于下一步，你计划执行一次输入操作：{{INPUT_OP}}。
- 作为测试者，做输入操作时你偏好与上下文相关的短输入。例如，在测试语法检查时，输入一句带语法问题的长句："The teacher give us many homeworks yesterday and we was trying to finished it but it were too hard so nobody don't understand what to do"。——【交互习惯】
- ……

**测试思维方式（Testing Mindset）** 定义了智能体的高层认知取向，影响探索过程的结构与逻辑。我们用两个属性建模：A. *顺序且连贯（sequential_and_coherent）*，代表遵循结构化、线性探索路径且流程目标导向的众包测试者；B. *发散且非线性（divergent_and_non-linear）*，代表采用更分散、好奇心驱动路径的众包测试者。这些属性抽象了众包测试会话中观察到的主要思维模式，涵盖了广泛的人类认知风格。

**探索策略（Exploration Strategy）** 指定了智能体在选择 GUI 中何处交互时的战术偏好。它包含三种常见的测试倾向：a. *点击导向（click_oriented）*，偏好与可点击 widget 的一般性交互；b. *核心功能导向（core_function_focused）*，优先考虑应用功能核心的特性；c. *输入导向（input_oriented）*，聚焦于接受用户输入的 widget。这些类别源自先前研究 [56] 的经验调查：众包 GUI 测试中绝大多数用户交互（>90%）属于点击与输入动作，且功能目标导向是手工测试的首要启发式。

**交互习惯（Interaction Habit）** 捕捉测试过程中输入生成的风格。由于输入操作对值的内容与类型高度敏感，我们用三个独特属性建模输入行为：i. *有效且短（valid_and_short）*，对应标准用户输入；ii. *有效且长（valid_and_long）*，代表通过冗长或极端值进行压力测试；iii. *无效（invalid）*，聚焦边缘情况或触发错误的输入。该维度反映了用户交互行为中真实的差异性，可显著影响应用执行路径与缺陷发现潜力。

为实例化人设化 LLM 智能体，我们将 persona 定义为一个元组：**Persona = ⟨m, s, h⟩，m ∈ {A, B}，s ∈ {a, b, c}，h ∈ {i, ii, iii}**。每个元组编码一个完整的行为画像，用于提示并约束 LLM 智能体的测试生成行为。这种形式化支持实例化具有明确定义行为先验的人设化智能体，便于在自动化 GUI 测试中进行受控实验与可靠的行为模拟。通过在提示构造期间用这些 persona 元组对 LLM 智能体进行条件约束，PersonaTester 实现了对多样化人类测试行为的一致、可复现且真实的模拟，显著推进了自动化 GUI 测试中人类因素的集成。

**表 1. 人设引导 LLM 智能体的配置**

| 人设引导智能体 | 测试思维方式 | | 探索策略 | | | 交互习惯 | | |
|---|---|---|---|---|---|---|---|---|
| | A | B | a | b | c | i | ii | iii |
| Persona A | ✓ | | ✓ | | | | ✓ | |
| Persona B | ✓ | | | ✓ | | | ✓ | |
| Persona C | ✓ | | | | ✓ | | | ✓ |
| Persona D | ✓ | | | ✓ | | ✓ | | |
| Persona E | | ✓ | ✓ | | | | ✓ | |
| Persona F | | ✓ | | | ✓ | | ✓ | |
| Persona G | | ✓ | ✓ | | | | | ✓ |
| Persona H | | ✓ | | ✓ | | | | ✓ |
| Persona I | | ✓ | | | ✓ | | ✓ | |

*注：A = 顺序且连贯，B = 发散且非线性；a = 点击导向，b = 核心功能导向，c = 输入导向；i = 有效且短，ii = 有效且长，iii = 无效输入。*


### 3.2 GUI 状态理解

PersonaTester 的关键基础在于其解释应用 GUI 状态的能力。这种解释不仅要捕捉每个状态的可视结构与语义，还要动态反映瞬态状态与对 persona 引导推理至关重要的可交互 GUI 元素。为此，我们设计了一种多阶段 GUI 理解方法，将传统计算机视觉（CV，Computer Vision）技术与多模态大语言模型（MLLM）相结合。这种混合策略确保了在不同 GUI 场景下的精确性与泛化能力。

GUI 状态理解过程始于 **widget 识别**，使用传统 CV 与 OCR 技术的组合从 GUI 状态中提取原始 GUI 元素 [53]。该步骤识别 widget 边界与文本内容，同时保留布局信息。重要的是，我们不直接依赖端到端 MLLM 完成此步骤，因为此类模型在未经大量微调的情况下难以处理复杂 GUI 布局，且经常丢失对下游推理至关重要的结构信息 [33]。初始识别之后，我们使用基于 MLLM 的过滤器进行**静态信息移除**，消除对有意义交互无贡献的 GUI 元素 [23]，包括常驻状态栏、装饰性元素与非功能性文本。该步骤提高了 GUI 状态的信号噪声比，防止智能体被无关组件分散注意力。接下来应用**瞬态 widget 识别**，识别上下文敏感的 GUI 元素，如临时弹窗（modal）、下拉菜单或弹出窗口。这些瞬态 widget 常被静态解析忽略，但对确定当前 GUI 状态起着重要作用。此阶段再次使用 MLLM，利用其视觉-语言推理能力将短暂元素与持久布局区分开来。

精炼后的 GUI widget 集合随后进行 **widget 标注**，每个 GUI 元素被分类（如按钮、输入框、开关）并关联语义描述符。这有助于 LLM 智能体的下游解释，确保智能体获得与其 persona 引导决策对齐的结构化、带标签的输入。然后，在 **widget 文本化** 步骤中，我们使用 LLM 将标注后的 GUI 状态转换为结构化 JSON 表示。每个 GUI widget 表示为捕获其类型、内容与潜在交互的键值对。该 JSON 表示作为 GUI 状态的语义抽象，使得不同 LLM 提示词之间能够进行一致的解释。此阶段使用 LLM 可实现稳健的自然语言接地（grounding），尤其是在 widget 标签或上下文模糊的情况下。最后，为优化性能并避免冗余处理，我们引入 **widget 持久化**：视觉相似度高的截图（余弦相似度高于 0.99，该阈值依据既有研究确定 [51, 55]）被检测并缓存，使系统能够复用先前解析的 JSON 表示。这不仅降低了计算开销，还确保了在重复出现的 GUI 场景中状态解释的一致性。

PersonaTester 在 CV 算法的优势与 (M)LLM 灵活的语义推理能力之间取得了平衡。通过显式处理持久与瞬态元素，并将视觉信息转化为结构化、persona 可理解的形式，我们的 GUI 状态理解模块为智能而多样的测试探索奠定了坚实基础。

### 3.3 面向 GUI 测试生成的 LLM 决策

作为自动化众包测试方法，PersonaTester 采用结构化的决策流水线，旨在生成具有语义意义且受 persona 引导的 GUI 测试行为。该过程迭代展开，借鉴 ReAct 范式——每一步根据当前上下文（包括 GUI 状态、测试历史与 persona 配置）动态决定下一个测试动作。决策提示词以模块化方式构造：首先以明确的身份与任务规范告知 LLM 目标应用与场景特定的测试目标。关键在于，提示词嵌入 persona 画像，尤其是测试思维方式与探索策略，从而引导推理过程。例如，具有顺序且任务聚焦画像的 persona 被引导按自上而下的逻辑顺序优先处理中央 GUI 元素，而发散型 persona 可能探索外围或非常规的界面路径。提示词还整合了历史交互上下文与感知阶段生成的结构化 GUI 表示，促进操作间的连贯性并减少语义冗余。

在此上下文基础之上，PersonaTester 采用**两阶段推理与动作过程**。首先，LLM 执行**测试意图生成**，表述高层目标，如"尝试切换通知设置"。该语义层增强了可解释性、与 persona 逻辑对齐，并在必要时引导隐藏元素的发现。接下来进行**基于意图的测试生成**，产生与既定目标及 persona 交互习惯相一致的具体 GUI 操作。例如，倾向于边界测试的 persona 可能发出长或无效输入，而其他 persona 可能偏好短而有效的输入。当未检测到即时交互目标时，系统利用生成的意图作为引导，主动搜索隐藏或瞬态 widget（如下拉菜单）。这种将推理与动作解耦的设计相比单步方法具有多重优势：提升了行为模块化，增强了调试与可追溯性，并确保跨测试会话的更高一致性。此外，它还支持意图对齐检查与 widget 优先级排序等任务。

为确保稳健性与行为保真度，提示词设计进一步纳入防护措施：防止重复操作、强制执行 persona 对齐行为、优先处理瞬态 widget 以免遗漏短暂元素。LLM 的输出是结构化的，包含测试意图、目标 widget 引用、动作类型、可选参数（如文本或滚动方向）以及用于下游追溯的摘要。总而言之，该决策框架使 PersonaTester 能够生成可解释、多样化且策略引导的 GUI 测试行为。通过集成 persona 建模、上下文感知与 ReAct 风格的推理-动作机制，系统在保持稳定性、可复现性与语义清晰度等自动化优势的同时，有效模拟了真实众包测试者的行为。

### 3.4 操作执行与检查

一旦生成测试操作，PersonaTester 即执行并验证它。该阶段不仅确保操作的实际执行，还确保对其意图的语义验证以及潜在 GUI 相关缺陷的检测。流程首先利用感知阶段构建的结构化 GUI 状态表示，将抽象的目标 widget 与关联操作转换为精确的屏幕坐标。这种坐标级映射保证了与底层 GUI 驱动的兼容性，实现准确且与设备无关的交互。随后在设备上执行指定操作（如点击、文本输入或滚动），完成后捕获结果屏幕并将其送入验证流水线。

验证由两个互补过程组成。首先，**意图检查（intent check）** 验证所执行的操作是否达到预期效果。该检查使用 MLLM 执行：将生成意图所定义的预期结果与更新后的 GUI 状态进行比较。这种语义验证提供了一种轻量级机制，用于评估应用是否按预期响应。其次，**缺陷检测机制** 分析操作后的界面是否存在异常，包括视觉布局不一致、缺失或损坏的 widget，以及错误消息或意外 UI 重置等故障迹象。通过利用视觉-语言推理，该方法在多样化的界面设计与应用场景中保持稳健。这一设计使 PersonaTester 能够进行智能 GUI 探索，同时持续评估多样化测试行为的有效性。

---

## 4 实验

为评估 PersonaTester，我们开展了一项综合研究，覆盖基于人设化 LLM 的众包测试的关键方面。我们的实验考察由 persona 引导的 LLM 智能体能否模拟类人测试行为并增强自动化众包测试。我们评估不同 persona 间的行为一致性与多样性（RQ1）、所生成测试事件执行有意义交互的有效性（RQ2），以及智能体触发崩溃与功能性缺陷的能力（RQ3）。评估覆盖多样化的真实世界应用与任务，同时使用定量指标与用户研究分析来验证研究发现。

### 4.1 实验设置

#### 4.1.1 研究问题（RQ）

为在自动化众包测试中评估 PersonaTester，我们设计了一组针对框架关键方面的研究问题（RQ）。这些 RQ 聚焦于评估人设引导 LLM 智能体的行为模式、其测试生成有效性以及触发缺陷的能力——这些核心因素反映了模拟类人测试行为、提升 GUI 测试多样性与质量的能力。

**RQ1** 考察 PersonaTester 的探索趋势，即人设引导测试是否产生一致或可区分的行为模式。我们通过检查相同 persona 在多次运行中是否产生稳定的探索轨迹来评估组内一致性（RQ1.1），并通过比较相同任务上不同 persona 的行为来评估组间差异性（RQ1.2）。为进一步评估对齐程度，我们开展一项用户研究（RQ1.3），参与者对每种行为在多大程度上反映对应 persona 维度进行评分。

**RQ2** 评估测试生成有效性，即人设引导智能体是否生成真实而有效的测试事件（RQ2.1），以及其行为是否与其定义的交互习惯对齐（RQ2.2）。我们分析所生成事件的数量与质量，尤其是输入操作，以确定不同 persona 是否表现出有助于有意义交互多样性的独特而一致的动作模式。

**RQ3** 针对人设引导 LLM 智能体的缺陷触发能力。除行为多样性外，GUI 测试的首要目标是识别应用缺陷。我们评估智能体有效发现崩溃缺陷（RQ3.1，通常由无效操作或边缘输入触发）以及功能性缺陷（RQ3.2，如行为异常的工作流、逻辑错误或 UI 显示问题）的能力。通过分析各人设引导 LLM 触发缺陷的重叠情况，我们评估将类人行为多样性纳入 LLM 测试自动化的实际益处。

我们不采用代码覆盖率作为评估指标，因为我们的测试任务是场景特定且目标驱动的，此时覆盖率并不能可靠地表明智能体是否有意义地完成或执行了目标功能 [52]。在此类设定下，高覆盖率可能来自与测试目标无关的交互。相反，我们设计研究问题以对 PersonaTester 提供全面且任务对齐的评估，考察人设化智能体的行为保真度、所生成测试事件的有效性、输入行为的真实性以及触发功能性与崩溃性缺陷的能力。这种多面评估更好地反映了在自动化众包 GUI 测试中模拟真实众包测试者多样性与意图的有效性。

为全面评估我们的方法，RQ1–RQ3 被设计为捕捉人设引导测试互补而相互关联的方面。RQ1 考察不同 persona 是否产生一致且独特的探索行为，验证我们结构化 persona 建模的保真度与可解释性，确保注入的 persona 有意义地引导智能体的测试模式。RQ2 随后评估沿这些路径产生的动作质量，聚焦其上下文有效性、可执行性与功能健全性。RQ1 关注 persona 如何塑造行为，RQ2 则验证这些行为在可执行测试层面产生什么成果。在两者基础上，RQ3 调查人设引导智能体能否有效发现真实缺陷，展示多样化探索策略的实际影响。总之，这些 RQ 确立我们的方法不仅产生独特且可解释的行为（RQ1），还保持测试可靠性（RQ2）并实现切实的测试价值（RQ3）。

#### 4.1.2 数据准备

为确保实证评估的普遍性与代表性，我们收集了 15 个多样化移动应用作为被测对象，在实验泛化性与实际资源约束之间取得平衡。所选应用涵盖广泛的功能领域，包括笔记、购物、旅行、阅读等，以确保代表性。所包含的应用具有多样的 GUI 结构与交互模式。这种广泛领域覆盖旨在减少领域特定偏差，并验证 PersonaTester 在不同使用流程上的泛化能力。这些应用选自先前研究 [32, 53]，包括 4 个开源应用与 11 个商业应用。此类应用文档完善、公开可用且被广泛研究，具有成熟的实验工件与真实的 GUI 复杂度。这使我们能够评估 PersonaTester 在真实而异构应用条件下的有效性。为构造评估任务，我们通过动手探索与官方应用描述，人工分析每个应用的用户界面、功能集与文档化的使用模式。该过程涉及识别代表应用核心功能的主要用户流程与高频操作 [52]。基于此分析，我们为每个应用制定一个任务，捕捉真实而具有代表性的用户场景，确保测试上下文与实际使用行为对齐（在线资源详情见第 7 节）。我们确保评估反映有意义的真实世界交互，而非抽象探索行为。这也使我们能够评估每个智能体在上下文丰富场景中的表现——在这些场景中，类人推理与 persona 对齐可发挥重要作用。鉴于在不同 persona 与基线上进行多次运行的成本，进一步扩展规模并不可行。

#### 4.1.3 基线构建

我们将基线实现为使用完整 PersonaTester 框架但禁用 personification 模块的非 persona 智能体，同时保持 GUI 理解、决策与执行完全一致。这隔离了 persona 注入的效果，确保公平、受控的比较。在累积实验中，我们运行基线九次以匹配人设化智能体的数量，从而能够公平聚合并降低单次运行方差。我们还报告每个人设化智能体与基线之间的一对一比较，以提供细粒度评估。

本文中我们聚焦于任务导向的 GUI 测试，即期望智能体完成特定功能场景而非进行无引导的探索。随机测试策略不具可比性，因为它们面向整个应用的探索，缺乏与预定任务对齐的语义引导 [25, 47, 52]（尽管如此，我们仍运行随机策略以展示性能，数据见第 7 节）。我们的基线是共享相同决策过程但不进行 persona 条件约束的非人设化智能体，从而隔离结构化 persona 注入的影响。虽然 PersonaTester 采用与先前方法相似的基于 LLM 的测试流水线，但无需直接比较，因为我们的方法被设计为模型无关且可插拔（pluggable）。它可以集成到现有框架中，以结构化行为多样性增强它们，而非取代或与之竞争。

#### 4.1.4 实验配置

为对 PersonaTester 进行严谨且可复现的评估，我们精心配置实验环境。PersonaTester 的架构依赖多次 LLM 调用来完成测试流水线中的不同子任务。具体而言，我们使用 **GPT-4o** 模型进行 GUI 理解与操作后验证，因其具备卓越的多模态能力——这对于准确解释截图与触发 GUI 异常至关重要。GPT-4o 的温度设置为 0，以确保确定性且一致的输出。对于测试决策智能体，我们采用 **GPT-o4-mini** 模型，一个轻量高效的 LLM 变体。选择该模型是为了模拟计算成本受限的真实使用条件，同时不过度牺牲推理能力。easoning_effort 设置为"medium"以平衡响应质量与延迟。

我们使用 GPT 系列模型，因为它们在复杂推理、上下文理解与多模态能力方面表现强劲——这些特性对涉及屏幕解释与意图生成的 GUI 测试任务至关重要。我们的目标是评估人设引导智能体设计的有效性，使用最先进的模型可确保观测结果反映方法的潜力而非模型局限。尽管如此，PersonaTester 是模型无关的，可以适配到持续提升准确性与能力的开源 LLM 上。为更好地说明可配置性，我们将 GPT 模型替换为 DeepSeek 模型 [11] 以观察性能（见第 7 节）。结果明确支持我们的结论：使用不同模型时，PersonaTester 仍能表现出明显的组内凝聚（intra-cluster cohesion）与组间分离（inter-cluster separation）。

考虑到 LLM 决策的随机性并捕捉潜在行为模式，每个任务对每种智能体配置执行五次，每次执行 20 分钟。为确定执行时间，我们招募了五名具备众包 GUI 测试必要基础知识的研究生（作为普通众包测试者的代表 [31]），他们每人完成每个任务的平均时间约为 10 分钟。我们为 LLM 智能体加倍该时间，将上限设为 20 分钟。这种重复使我们能够观察组内一致性，并支持计算统计稳健的评估指标。每个任务运行多个会话还能揭示智能体是否在多次执行中表现出与其分配 persona 对齐的稳定行为特质。

### 4.2 RQ1：探索趋势

为评估 PersonaTester 是否表现出同一 persona 内一致、不同 persona 间多样的探索行为，我们设计了一项由三个部分组成的综合评估：组内凝聚性（RQ1.1）、组间分离度（RQ1.2）与 persona 一致性（RQ1.3）。该评估使我们能够确定 persona 中编码的行为差异是否被忠实地反映在智能体产生的测试路径中。

对于 RQ1.1，**组内凝聚性**量化同一 persona 的重复测试执行产生相似探索轨迹的程度。对每个 persona 与每个任务，我们进行五次独立测试运行。计算每对路径之间的相似度，其平均值报告为凝聚分数。凝聚度计算为所有 n×(n−1)/2 组合的平均成对相似度，其中 n = 5 表示运行次数：

> Cohesion_intra = (Σ_{i=1..n, j=1..n, i<j} sim(i,j)) / n

对于 RQ1.2，**组间分离度**通过计算不同人设引导智能体生成路径之间的平均相似度来衡量不同 persona 之间的区分度。具体而言，对每对 persona M 与 N，我们比较 M 的每条测试轨迹与 N 的每条轨迹，并计算所有 5×5 = 25 个成对相似度的平均值：

> Separation_inter(M, N) = (Σ_{i=1..5} Σ_{j=1..5} sim(i,j)) / 25

高组内凝聚度与低组间相似度相结合，表明每个 persona 都能引发稳定而独特的探索行为。

**图 4. RQ1.1：组内凝聚性**

（图注：X 轴为（非）人设化 LLM 智能体 A–I 及 X；Y 轴为组内凝聚分数（相似度，取值 0.3–1.0）。多数人设化智能体的组内凝聚分数通常接近或超过 0.9。）

为计算两条测试路径之间的相似度，我们首先将每条路径编码为向量，然后度量它们之间的余弦相似度。路径向量化过程是一个多步骤流程，旨在以原则性、可解释的方式捕捉语义级行为模式。首先，我们验证探索轨迹以构建干净、可解释的测试轨迹。为将原始 GUI 交互转化为简洁的自然语言短语，我们应用轻量级、基于规则的净化过程 [51]。每个交互首先根据执行日志映射到其动作类型（如点击、输入），相应的 widget 标签从 GUI 元数据与 OCR 结果中提取。随后使用简单的动宾模板（如"点击保存按钮"、"输入闹钟时间"）组合这些元素 [40, 51]，并为无标签 widget 提供后备描述。生成的短语被规范化以保证一致性，随后使用 SBERT 编码，提供探索轨迹的语义意义且可扩展的表示。每个动作短语使用 Sentence-BERT（SBERT）[30] 单独编码，将句子转换为 384 维向量表示。整个编码动作序列随后通过双向 LSTM（BiLSTM）模型 [9]，聚合为表示完整测试路径的固定大小 256 维向量。我们选择 SBERT 是因为它为短自然语言短语提供语义有意义的嵌入，非常适合编码单个 GUI 动作。为捕捉完整测试路径的序列与上下文结构，我们使用 BiLSTM，它能有效建模动作序列中的前向与后向依赖。这种组合确保语义意图与行为流都保留在最终路径表示中。该模型配置（包括 SBERT 与 BiLSTM）基于 GPT 辅助参数调优与语义序列编码的先前研究选定。余弦相似度在由 BiLSTM 生成的路径级嵌入上计算，BiLSTM 按序列聚合 SBERT 编码的动作短语。每个动作短语通过基于规则的净化过程生成，将低级 GUI 事件抽象为简洁、语义有意义的描述。BiLSTM 捕捉这些动作的语义内容与序列依赖，确保执行顺序与上下文的变化保留在最终嵌入中。对这些嵌入应用余弦相似度，使我们能够量化探索行为的差异——不仅捕捉采取了哪些动作，还捕捉它们如何被结构化。该设计与我们评估 persona 诱发行为模式的目标一致，支持以原则性、顺序敏感的方式测量组内凝聚度与组间分离度。

为准备评估 persona 一致性的用户研究（RQ1.3），我们招募了 20 名具有软件 GUI 测试相关经验的参与者，包括软件工程专业的研究生以及具有五年以上行业经验的专业 QA 工程师或测试人员。研究材料方面，我们生成了一组完整的视频录制，捕捉不同人设引导 LLM 智能体在各种任务中产生的 GUI 探索轨迹。所有视频均经过仔细匿名化，移除可能泄露底层 persona 的任何标签或标识符。观看每个视频后，参与者会获得一份预定义的 persona 画像列表，每个画像沿三个维度描述：测试思维方式、探索策略与交互习惯。参与者被要求使用 10 点李克特量表（Likert scale）[16] 评估所观察的智能体行为与每个人设画像的吻合程度，其中 1 表示完全不吻合，10 表示完全吻合。这一设计使我们能够从人类视角定量评估 persona 驱动行为的可解释性与语义保真度。

对于组内凝聚性，我们量化同一人设引导智能体在相同任务上执行的多个测试会话之间的相似度。如图 4 所示，大多数人设化智能体表现出高组内一致性，分数通常接近或超过 0.9。这表明每个智能体的决策过程都稳定地由其分配的人设引导，在重复运行中产生可复现的行为模式。相比之下，非人设化基线智能体（Persona X）表现出明显较低的凝聚度，范围从 0.31 到 0.7，反映出在缺乏显式行为建模时更不稳定、更不规律（erratic）的探索行为。这些发现证实 persona 注入不仅告知智能体的高层意图，还以一致且可重复的方式锚定其交互行为。

**图 5. RQ1.2：组间分离度**

（热力图矩阵：X 轴与 Y 轴均为智能体 X、A–I；每个子图对应一个任务（任务 1–15），单元格为成对路径相似度。X 与其他智能体的行相似度普遍较低，各 persona 内部（对角块）相似度较高，例如 Persona A 的组内值常高于 0.90，而组间值普遍低于 0.7。图注说明：为评估不同 persona 的区分度，我们测量执行相同任务的智能体之间的组间分离度。如图 5 所示，成对相似度矩阵揭示组间 persona 间相似度始终远低于组内相似度，确认每个 persona 驱动独特的探索行为。例如，虽然 Persona B 与 Persona C 共享顺序式思维方式，但它们不同的策略与输入习惯导致显著的行为分化，相似度分数在 0.27 到 0.67 之间。同样，Persona E 与 Persona G 等发散型 persona 与 Persona A、Persona B 等结构化 persona 之间显示出清晰的分离。这些结果凸显了我们的 persona 配置空间在产生多样化、行为上有意义的测试模式方面的有效性。）

对于 RQ1.3，结果表明人设引导智能体的行为与其预期 persona 画像之间具有高度感知一致性。所有参与者的平均评分：测试思维方式 8.35 分，探索策略 8.70 分，交互习惯 8.80 分（所有分数均 ≥ 7）。这些高分表明参与者能够一致地识别并解读 persona 中编码的独特行为特征。

在这三个维度中，**交互习惯**的平均评分最高（8.80），且标准差相对较低（0.77），表明参与者认为输入风格与行为倾向（如输入有效且长或无效的值）特别可区分且表达可靠。**测试思维方式**紧随其后，平均分 8.35，标准差最低（0.67），表明参与者在识别智能体行为中的顺序式或发散式思维模式方面具有稳定共识。**探索策略**虽然仍获得较高评分（平均 8.70），但变异性最高，标准差为 1.08。这表明尽管参与者普遍认为探索意图（如点击导向或核心功能导向）与 persona 描述一致，但与其它维度相比，此类策略可能更具主观性或表达一致性稍弱。

这些结果验证了人设引导 LLM 智能体产生的行为不仅多样、一致，而且从人类视角看也是可解释、具有语义意义的。智能体行为与 persona 描述之间的强对齐证实了 PersonaTester 在模拟真实、可区分的众包测试者式测试风格方面的有效性。


### 4.3 RQ2：测试生成有效性

RQ2 考察人设引导 LLM 智能体在自动化 GUI 探索中生成测试事件的有效性。我们从两个视角评估该有效性：**一般测试事件生成**，涵盖点击与导航等所有交互类型；以及**输入事件生成**，专门聚焦于基于文本的输入动作，如向表单或字段中输入值。本研究中，我们将测试生成有效性定义为生成的测试事件对被测应用探索做出有意义贡献的程度。具体而言，若测试事件带来成功且语义有效的交互——如触发新的 GUI 状态、调用有意义的响应或推进任务场景——则认为其有效。我们计算有效事件占所有事件的比例，结果见图 6。

**图 6. RQ2：测试生成有效性**

- (a) RQ2.1：一般测试生成有效性（Y 轴为测试生成有效性百分比，X 轴为（非）人设化 LLM 智能体 X、A–I）
- (b) RQ2.2：输入事件生成有效性（Y 轴为输入事件生成有效性百分比，X 轴为（非）人设化 LLM 智能体）

在总体测试事件生成有效性方面，大多数人设引导智能体表现优于非人设化基线智能体（Persona X），平均提升 33%–47%。多个智能体持续优于基线。这表明人设引导行为意图的集成并未损害 LLM 与 GUI 交互的能力；相反，它引导智能体产生更有目的性、更可执行的测试动作。

在输入事件生成有效性上观察到更明显的模式，如图 6b 所示。结果清楚地表明该能力受 persona 中指定的探索策略强烈影响。具有输入导向策略的智能体（即 Persona C、Persona F、Persona I）在多个任务中持续获得高有效性分数，平均超过基线 Persona X 683.32%–697.50%。它们的行为与聚焦于与输入框交互的意图一致，产生更高效的、有效且语义恰当的文本输入。相比之下，具有点击导向探索策略的智能体（即 Persona A、Persona E、Persona G）持续表现出较低的输入事件有效性，平均较基线 Persona X 低 -28.03% 至 -6.60%。这些智能体按 persona 定义优先与可点击 UI 元素交互，通常忽略输入组件。具有核心功能导向策略的智能体（即 Persona B、Persona D、Persona H）表现更不稳定，平均超出基线 Persona X 80.31%–156.59%。虽然它们偶尔在任务导向工作流中产生有效输入事件，但该领域的有效性仍不一致，总体处于中等水平。

这些结果证实 persona 设计可以直接塑造 LLM 智能体的注意力与交互模式。输入导向策略与输入事件生成有效性之间的强对齐，证明了细粒度行为建模在实现目标测试覆盖方面的重要性。总体而言，这些发现支持如下结论：人设引导智能体不仅增强了自动化 GUI 测试的多样性与真实性，还有助于生成更有意义、更具功能性的测试事件。

### 4.4 RQ3：缺陷触发能力

RQ3 评估人设引导 LLM 智能体在自动化 GUI 测试中触发缺陷的实际有效性。具体而言，我们聚焦两类缺陷：崩溃缺陷与功能性缺陷。目标是确定通过人设化引入的多样性是否在单个非人设化智能体可达到的水平之外，对缺陷触发做出了有意义的贡献。

崩溃缺陷触发结果汇总于图 7。每个子图展示非人设化智能体 Persona X 与每个人设引导智能体（Persona A 至 Persona I）触发的崩溃缺陷集合之间的比较。重叠区域表示共同发现的缺陷，非重叠区域突出独特发现。在全部比较中，人设引导智能体相对于基线表现出触发共同与独特崩溃缺陷的能力。其中六个智能体触发了超过 20 个基线 Persona X 未覆盖的独特缺陷，其中 Persona E 表现最佳，触发 26 个独特缺陷。直观上，人设引导智能体在不同任务上共触发 29–38 个崩溃缺陷，而 Persona X 只能触发 22 个崩溃缺陷。这表明通过 persona 配置引入的探索多样性使智能体能够到达通用策略较难访问的 UI 状态或使用路径。

**图 7. RQ3.1：崩溃缺陷触发能力**

（文氏图/交集图矩阵：每个子图为 X × Y 的崩溃缺陷集合比较，其中 Y ∈ {ALL, A, B, C, D, E, F, G, H, I}。数字代表各集合的缺陷计数，如 X 独有的崩溃数、重叠数、persona 独有数。九个人设化智能体的汇总（9X × ALL）显示集合并集显著大于九次基线运行。）

我们观察到少数缺陷仅由非人设化基线智能体触发。这些情况很大程度上源于基线决策中固有的随机性，偶尔会导致与任何已定义 persona 都不对齐的替代性、非结构化路径。然而，这种行为不可重复且缺乏一致模式。相比之下，人设引导智能体表现出稳定、可解释的行为。虽然我们当前的人设集合并非穷尽，但这些发现表明扩展 persona 的多样性可以进一步提高缺陷覆盖。总体而言，结果凸显人设化智能体能够发现与其行为画像对齐的独特缺陷，验证了结构化多样性在自动化 GUI 测试中的价值。

为进一步评估实用性，我们将九个人设引导智能体（Persona A 至 Persona I）的累积结果与 Persona X 的九次运行结果进行比较，如图 7 中子图"9X × ALL"所示。在此设置中，我们模拟用九个多样化人设驱动智能体替换九名人类众包测试者的场景。结果表明人设引导群体共同发现了显著更广泛的崩溃缺陷集合。虽然存在一定重叠，但 persona 智能体发现的独特缺陷并集大幅超过基线九次重复的并集。这证实行为多样性（而非重复）是最大化崩溃缺陷暴露的关键因素。

功能性缺陷触发结果见表 2。该表列出每个智能体在预定义任务集上触发的功能性缺陷。每个条目对应一个智能体在特定任务中触发的具体缺陷。表 2 中的缺陷 ID 根据匹配的故障症状与触发条件分配。若多个智能体在相似的交互路径与 GUI 状态下遇到同一问题，其发现归入同一 ID。该过程确保跨智能体的重叠反映实际共享的缺陷发现，而非表面相似性。

**表 2. RQ3.2：功能性缺陷触发能力**

| 任务 ID | Persona X | Persona A | Persona B | Persona C | Persona D | Persona E | Persona F | Persona G | Persona H | Persona I |
|---|---|---|---|---|---|---|---|---|---|---|
| 4 | | fb11 | fb11 | fb11 | fb11 | fb11 | fb11 | | | |
| 5 | | fb10 | fb10 | | | | | fb10 | fb10 | |
| 6 | fb1 | fb1 | fb1 | fb1 | fb1 | | | | | |
| 9 | fb6 | | fb7 | fb7 | fb9 | fb6, fb7, fb8 | fb8 | | | |
| 10 | | fb2 | fb3 | fb2 | | | | | | |
| 15 | fb4 | fb4 | fb4 | fb4 | fb5 | fb4 | fb4 | | | |
| 合计 | 3 | 3 | 4 | 3 | 2 | 3 | 3 | 4 | 6 | 3 |

所有报告的功能性缺陷均通过人工检查确认。三位作者独立审查探索轨迹，包括 UI 上下文、执行动作与产生的结果。只有当所有审查者达成共识时，缺陷才被标记为功能性缺陷，确保一致而客观的验证。多模态 LLM 仅用于分析 GUI 级视觉问题（如布局错误），不用于判定功能正确性。

在所有任务中，**Persona H** 触发数量最高（6 个缺陷），其次是 Persona B 与 Persona G（各 4 个）。有趣的是，非人设化智能体总共仅触发 3 个缺陷，且这些缺陷均被部分人设引导智能体触发。这些结果呈现出与崩溃缺陷触发相似的趋势：人设引导智能体不仅能发现与基线相同的功能性缺陷集合，还能贡献额外、此前未见的问题。

为说明 persona 配置对功能性缺陷触发的影响，我们重点介绍几个代表性案例，其中人设引导智能体的行为特质直接促成了功能性缺陷的发现。

- **fb2（时序敏感过滤错误）**：仅在会话一开始便应用闹钟过滤器时触发。该缺陷由采用顺序式测试思维方式与点击导向策略（优先与顶层 UI 元素早期交互）的 Persona A 与 Persona G 智能体发现。它们有条理的行为与暴露该瞬态问题所需的时间条件精确吻合。
- **fb3（闹钟编辑锁死，即 2.2 节所述缺陷）**：导致无效闹钟在类型更改后变得不可编辑。该缺陷仅由强调连贯、目标驱动探索的 Persona B 智能体触发。该 persona 的完整工作流执行（包括编辑与保存）对于到达缺陷状态至关重要。
- **fb5（外围页面渲染缺陷）**：发生在一个很少访问的"支持与发展（Support Development）"页面上，其下方内容无法渲染。设计为发散、寻求输入特质的 Persona F 在寻找输入机会时遍历非显而易见路径，发现了任务聚焦策略很可能遗漏的缺陷。
- **fb7（文本长度导致 UI 重叠）**：涉及一个视觉缺陷——"发送（Send）"按钮被长文本输入遮挡。配置了长而有效输入交互习惯的 Persona A、Persona F、Persona H 智能体一致地浮出该缺陷。它们的交互习惯模拟了真实用户的压力条件，揭示了短或默认输入无法发现的 UI 漏洞。

这些示例表明，交互时机、输入风格与探索广度等 persona 特质对所发现缺陷的类型具有切实影响。通过编码真实的用户行为，PersonaTester 使自动化智能体能够发现与真实使用模式对齐的缺陷。总之，RQ3 的发现证实人设引导 LLM 智能体增强了自动化 GUI 测试的缺陷触发能力。通过将多样化行为意图嵌入智能体，PersonaTester 模拟了一个虚拟测试者群体，其集体探索模式比通用或重复性自动化触发更多缺陷。这验证了在自动化测试中融入行为多样性的实际益处，尤其是在最大化缺陷发现至关重要的场景中。

### 4.5 有效性威胁

内部有效性的一项威胁源于相似度分析与用户研究所用探索轨迹的人工标注。为减少偏差，我们应用交叉验证，并对动作描述使用标准化的语言模板。用户研究还采用了随机化的轨迹呈现顺序与匿名化的 persona 身份，以最小化实验者与参与者偏差。

外部有效性可能受限于我们对测试应用、任务与所定义 persona 的选择。虽然这些覆盖了多样的行为模式与真实任务，但它们可能无法捕捉用户行为或应用领域的全部光谱。为缓解这一点，我们变换了任务类型与 UI 结构，并将 persona 设计植根于真实众包测试者数据。测量有效性也存在风险，因为路径相似度等指标可能无法完全捕捉复杂的行为细微差别。为解决此问题，我们用定性评估补充定量分析，包括评估语义对齐的用户研究，以确保对 persona 驱动测试保真度进行更稳健的验证。我们已尽最大努力设计严谨的评估协议，在可复现性、可解释性与真实性之间取得平衡。这些步骤共同增强了我们发现的有效性，并支持人设引导 LLM 智能体在自动化 GUI 测试中的广泛适用性。

### 4.6 讨论

#### 4.6.1 新颖性要点

PersonaTester 的一项关键贡献在于我们如何通过结构化、自动化的方法模拟众包测试。我们并非复制同一智能体多次或依赖隐式行为随机性，而是将多样化 persona 注入共享的基于 LLM 的测试框架。这使人设化智能体能够代表不同的众包测试者原型，并使其能够协同工作以模拟真实众包测试范式的行为多样性。由此产生的人设引导智能体集合产生互补的测试行为，它们共同实现的探索广度与缺陷触发效率超过任何单一智能体。总而言之，PersonaTester 可以定位为众包 GUI 测试的自动化实现。

#### 4.6.2 应用特征的影响

有效性可能受 GUI 与功能复杂度、UI 框架与应用领域等因素影响。虽然详细的逐因素分析超出本研究范围，但我们通过选择来自不同领域、具有多样 GUI 结构与交互模式的应用来缓解潜在偏差，隐式覆盖不同复杂度水平。我们在这些异构应用上观察到一致的趋势，表明人设引导智能体对此类变化具有稳健性。更细粒度地分析具体应用特征如何影响人设引导测试，仍是未来工作的重要方向。

#### 4.6.3 应对 LLM 固有局限

PersonaTester 通过显式设计选择来缓解 LLM 的固有局限，如不确定性、幻觉与可复现性问题。两阶段意图验证机制使用独立的"LLM 即裁判（LLM-as-a-judge）"针对当前 GUI 上下文验证每个提议的动作，减少不对齐的决策并防止错误传播。此外，采用确定性 LLM 设置（如 temperature = 0）以确保跨运行的稳定、可复现行为，从而支持公平评估 persona 效应。

#### 4.6.4 经济成本分析

PersonaTester 采用模块化模型配置以平衡准确性与效率。轻量级 o4-mini 模型用于决策，而能力更强的多模态 GPT-4o 模型保留用于需要更高精度的 GUI 理解与执行后验证。基于我们的测量，GUI 理解与验证每任务每智能体平均消耗 112,386 个输入 token（约 .28）与 12,305 个输出 token（约 .12）。决策过程消耗 70,569 个输入 token（约 .08）与 45,720 个输出 token（约 .20）。总计，每任务每智能体执行成本约 .68，比招募真实众包测试者执行相同测试任务更经济。由于篇幅限制，结果的明细表见我们的在线补充材料（第 7 节）。这种成本效益使 PersonaTester 能够为自动化众包测试进行可扩展部署。


---

## 5 相关工作

### 5.1 基于 LLM 的 GUI 测试：自动化与众包

近期研究探索了利用 LLM 增强自动化 GUI 测试。Liu 等人 [22] 提出 GPTDroid，将应用 GUI 状态传递给 LLM 并与应用迭代"对话"以生成测试动作。Wang 等人 [37] 提出 LLMDroid，将传统自动化探索与偶尔的 LLM 引导相结合。总体而言，这些研究表明，若能审慎使用以平衡洞察与成本，LLM 可以驱动类人探索式测试，策略性地导航应用 GUI 并提高覆盖率。超越原始覆盖率，研究者正使用 LLM 追求与应用功能及用户场景对齐的更多语义测试目标。Yu 等人全面分析了 LLM 在生成与迁移 GUI 测试脚本方面的能力 [54]。Yoon 等人开发了 DroidAgent [49]，一个自主的 LLM 驱动测试器，设定高层任务目标后执行 GUI 步骤以完成目标。类似地，Yu 等人相继提出 ScenTest [52] 与 ScenGen [58]，这些框架通过向 LLM 智能体或知识图谱增强模块分配不同子任务来镜像手工测试过程。

LLM 还被应用于在不同应用或平台间迁移 GUI 测试脚本。传统测试迁移技术依赖功能相似应用间的静态 widget 映射，但 GUI 出现差异时这种方法常常失效 [60]。Zhang 等人 [59] 提出使用 LLM 的抽象-具体化（abstraction-concretization）范式。Cao 等人 [2] 的另一种方法使用 LLM"意图理解"来迁移测试。这些研究展示了 LLM 如何充当软件测试翻译器，通过理解 GUI 动作背后的目的来执行测试复用。

另一个有前景的应用是使用 LLM 解释缺陷报告并在 GUI 上复现所描述的问题。AdbGPT [7] 是早期系统，使用 GPT-3.5 从自由格式缺陷报告中提取结构化 S2R（Steps-to-Reproduce）步骤，然后迭代使用 ChatGPT 选择匹配的 GUI widget 并执行每个步骤。Wang 等人 [38] 提出 ReBL，放弃显式步骤提取，转而将完整缺陷描述连同执行期间的反馈循环一起馈入 LLM。另一项工作是 Huang 等人 [14] 的 ReActDroid，结合"推理+行动"提示词仅从崩溃摘要推断缺失上下文与步骤。这些进展表明 LLM 可以充当测试中强大的自然语言中介：它们阅读并理解缺陷描述或日志，然后驱动应用 GUI 为开发者重演故障。

LLM 还可用于增强众包测试，包括任务分配 [5, 41, 46]、报告聚类 [3, 12, 21, 57]、报告重复检测 [17, 40, 45]、优先级排序 [6, 35, 48]、质量检测 [56] 等。LLMPrior [20] 是代表性的基于 LLM 的框架，通过利用语义理解对众包缺陷报告进行优先级排序。然而，尚无现有工作旨在通过自动化众包测试者的行为来增强众包测试；当前方法仍严重依赖人类参与及其固有的不可预测性。本文引入 PersonaTester——一个基于人设化 LLM 的框架，通过设计好的 persona 模拟多样化测试者行为，从而在不牺牲行为多样性的前提下提升众包测试的效率、可靠性与可控性。

### 5.2 LLM 人设化

另一个活跃研究领域是 LLM 人设化（LLM personification），即将 persona 注入 LLM 以引导行为并提升任务性能。人设化最初在对话式 AI 中探索（如 PersonaChat），现已成为跨领域强大的提示工程策略 [36]。基于角色的提示词（如"充当软件测试员"）影响 LLM 的推理风格与焦点。近期研究表明，良好对齐的 persona 可以改善任务结果。例如，Hu 等人 [13] 发现 LLM 响应在 162 个测试 persona 中语气与内容显著不同，其收益取决于 persona 与任务的对齐程度及提示词表述方式。在软件工程中，将 LLM 提示为测试员或开发者会带来更相关、更聚焦的响应。多智能体框架也采用了 persona 专业化，例如 Luo 等人 [24] 与 Qian 等人 [29] 证明，分配角色的 LLM 智能体（规划者、编码者、测试者）能实现更好的协作与结构化推理。这些发现共同凸显了 persona 注入作为实现更有效、更有针对性的 LLM 行为的实用手段。

超越 NLP 任务，LLM 人设化在 HCI 领域用于模拟用户 persona 也日渐兴起。Sun 等人 [34] 提出 Persona-L，使用基于能力的 persona 提示词为复杂需求的用户（如残障人士）建模。在社交与教育模拟中，Park 等人 [28] 提出"生成式智能体（generative agents）"——具有人格的 LLM 在虚拟环境中自主交互，表现出类人行为。这些研究凸显了基于 persona 技术的多功能性，展示了引导 LLM 输出以进行以人为中心的推理的能力。LLM 人设化是引导 LLM 知识、风格与推理的多功能方法，可增强其针对特定角色或用户需求的有效性。

---

## 6 结论

本文提出 PersonaTester——一个新颖的框架，将 persona 集成到 LLM 驱动的智能体中以实现众包测试自动化。通过在三个维度（测试思维方式、探索策略、交互习惯）上建模测试者，PersonaTester 实现了真实而多样的测试行为。实验结果显示强烈的组内一致性与清晰的组间区分度，有效复现了真实众包测试者的行为模式。与基线智能体相比，PersonaTester 提升了测试生成有效性与缺陷触发率，显示出弥合手工与自动化测试范式之间差距的潜力。

## 7 数据可用性

更多细节与复现包见 https://sites.google.com/view/personatester。

## 致谢

于晟成（Shengcheng Yu）部分受慕尼黑工业大学高等研究院（IAS, Institute for Advanced Study）资助；陈振宇（Zhenyu Chen）部分受国家重点研发计划（2024YFF0908001）资助。

---

## 参考文献（References）

> 注：仅翻译文献标题；作者、期刊、年份等格式保留原文。

[1] Domenico Amalfitano, Anna Rita Fasolino, Porfirio Tramontana, Salvatore De Carmine, 与 Atif M Memon. 2012. 使用 GUI ripping 对 Android 应用进行自动化测试（Using GUI ripping for automated testing of Android applications）. 收录于第 27 届 IEEE/ACM 国际自动化软件工程会议论文集. 258–261.

[2] Shaoheng Cao, Minxue Pan, Yuanhong Lan, 与 Xuandong Li. 2025. 基于大语言模型的移动应用意图驱动 GUI 测试迁移（Intention-Based GUI Test Migration for Mobile Apps using Large Language Models）. ACM 软件工程会议论文集 2, ISSTA (2025), 2296–2318.

[3] Hao Chen, Song Huang, Yuchan Liu, Run Luo, 与 Yifei Xie. 2021. 基于句子嵌入的有效众包测试报告聚类模型（An effective crowdsourced test report clustering model based on sentence embedding）. 收录于 2021 年 IEEE 第 21 届国际软件质量、可靠性与安全会议 (QRS). IEEE, 888–899.

[4] Mengzhuo Chen, Zhe Liu, Chunyang Chen, Junjie Wang, Boyu Wu, Jun Hu, 与 Qing Wang. 2025. 站在巨人的肩膀上：通过检索增强进行缺陷感知的自动化 GUI 测试（Standing on the Shoulders of Giants: Bug-Aware Automated GUI Testing via Retrieval Augmentation）. ACM 软件工程会议论文集 2, FSE (2025), 825–846.

[5] Qiang Cui, Junjie Wang, Guowei Yang, Miao Xie, Qing Wang, 与 Mingshu Li. 2017. 众包测试中应选择谁来执行任务？（Who should be selected to perform a task in crowdsourced testing?）. 收录于 2017 年 IEEE 第 41 届年度计算机软件与应用会议 (COMPSAC), 第 1 卷. IEEE, 75–84.

[6] Chunrong Fang, Shengcheng Yu, Quanjun Zhang, Xin Li, Yulei Liu, 与 Zhenyu Chen. 2024. 通过图文语义理解与特征融合增强众包测试报告优先级排序（Enhanced Crowdsourced Test Report Prioritization via Image-and-Text Semantic Understanding and Feature Integration）. IEEE 软件工程汇刊 (2024).

[7] Sidong Feng 与 Chunyang Chen. 2024. 提示即一切：利用大语言模型自动化复现 Android 缺陷（Prompting is all you need: Automated android bug replay with large language models）. 收录于第 46 届 IEEE/ACM 国际软件工程会议论文集. 1–13.

[8] Ruizhi Gao, Yabin Wang, Yang Feng, Zhenyu Chen, 与 W Eric Wong. 2019. 成功、挑战与反思——众包移动应用测试的工业调查（Successes, challenges, and rethinking – an industrial investigation on crowdsourced mobile application testing）. 经验软件工程 24, 2 (2019), 537–561.

[9] Alex Graves, Navdeep Jaitly, 与 Abdel-rahman Mohamed. 2013. 深度双向 LSTM 混合语音识别（Hybrid speech recognition with deep bidirectional LSTM）. 收录于 2013 年 IEEE 自动语音识别与理解研讨会. IEEE, 273–278.

[10] Jonathan Grudin 与 John Pruitt. 2002. Persona、参与式设计与产品开发：参与式基础设施（Personas, participatory design and product development: An infrastructure for engagement）. 收录于 PDC. 144–152.

[11] Daya Guo, Dejian Yang, Haowei Zhang, Junxiao Song, Peiyi Wang, Qihao Zhu, Runxin Xu, Ruoyu Zhang, Shirong Ma, Xiao Bi, 等. 2025. DeepSeek-R1：通过强化学习激励大语言模型的推理能力（Deepseek-r1: Incentivizing reasoning capability in llms via reinforcement learning）. arXiv 预印本 arXiv:2501.12948 (2025).

[12] Rui Hao, Yang Feng, James A Jones, Yuying Li, 与 Zhenyu Chen. 2019. CTRAS：众包测试报告聚合与摘要（CTRAS: Crowdsourced test report aggregation and summarization）. 收录于 2019 年 IEEE/ACM 第 41 届国际软件工程会议 (ICSE). IEEE, 900–911.

[13] Tiancheng Hu 与 Nigel Collier. 2024. 量化 LLM 模拟中的 Persona 效应（Quantifying the Persona Effect in LLM Simulations）. 收录于第 62 届计算语言学协会年会论文集（第 1 卷：长文）. 10289–10307.

[14] Yuchao Huang, Junjie Wang, Zhe Liu, Mingyang Li, Song Wang, Chunyang Chen, Yuanzhe Hu, 与 Qing Wang. 2025. 一句话可杀死缺陷：从一句话概览自动重放移动应用崩溃（One Sentence Can Kill the Bug: Auto-replay Mobile App Crashes from One-sentence Overviews）. IEEE 软件工程汇刊 (2025).

[15] Yuekai Huang, Junjie Wang, Song Wang, Zhe Liu, Yuanzhe Hu, 与 Qing Wang. 2020. 寻求黄金方法：重复众包测试报告检测的实验评估（Quest for the golden approach: An experimental evaluation of duplicate crowdtesting reports detection）. 收录于第 14 届 ACM/IEEE 国际经验软件工程与测量研讨会 (ESEM) 论文集. 1–12.

[16] Ankur Joshi, Saket Kale, Satish Chandel, 与 D Kumar Pal. 2015. 李克特量表：探索与解释（Likert scale: Explored and explained）. 英国应用科学与技术杂志 7, 4 (2015), 396.

[17] Taemin Kim 与 Geunseok Yang. 2022. 使用基于主题的重复学习与基于微调 BERT 的算法预测缺陷报告中的重复（Predicting duplicate in bug report using topic-based duplicate learning with finetuning-based bert algorithm）. IEEE Access 10 (2022), 129666–129675.

[18] Yuanchun Li, Ziyue Yang, Yao Guo, 与 Xiangqun Chen. 2017. Droidbot：轻量级 UI 引导的 Android 测试输入生成器（Droidbot: a lightweight ui-guided test input generator for android）. 收录于 2017 年 IEEE/ACM 第 39 届国际软件工程会议配套卷 (ICSE-C). IEEE, 23–26.

[19] Yuanchun Li, Ziyue Yang, Yao Guo, 与 Xiangqun Chen. 2019. Humanoid：基于深度学习的自动化黑盒 Android 应用测试方法（Humanoid: A deep learning-based approach to automated black-box android app testing）. 收录于 2019 年第 34 届 IEEE/ACM 国际自动化软件工程会议 (ASE). IEEE, 1070–1073.

[20] Yuchen Ling, Shengcheng Yu, Chunrong Fang, Guobin Pan, Jun Wang, 与 Jia Liu. 2025. 重新定义众包测试报告优先级排序：一种使用大语言模型的创新方法（Redefining crowdsourced test report prioritization: An innovative approach with large language model）. 信息与软件技术 179 (2025), 107629.

[21] Di Liu, Yang Feng, Xiaofang Zhang, James A Jones, 与 Zhenyu Chen. 2020. 使用图像理解对移动应用众包测试报告进行聚类（Clustering crowdsourced test reports of mobile applications using image understanding）. IEEE 软件工程汇刊 48, 4 (2020), 1290–1308.

[22] Zhe Liu, Chunyang Chen, Junjie Wang, Mengzhuo Chen, Boyu Wu, Xing Che, Dandan Wang, 与 Qing Wang. 2024. 让 LLM 成为测试专家：通过功能感知决策为移动 GUI 测试带来类人交互（Make llm a testing expert: Bringing human-like interaction to mobile gui testing via functionality-aware decisions）. 收录于 IEEE/ACM 第 46 届国际软件工程会议论文集. 1–13.

[23] Zhe Liu, Chunyang Chen, Junjie Wang, Mengzhuo Chen, Boyu Wu, Yuekai Huang, Jun Hu, 与 Qing Wang. 2024. 解除文本输入盲区：通过 LLM 预测移动应用中文本输入的提示文本（Unblind text inputs: predicting hint-text of text input in mobile apps via LLM）. 收录于 2024 年 CHI 计算机系统人为因素会议论文集. 1–20.

[24] Jing Luo, Run Luo, Longze Chen, Liang Zhu, Chang Ao, Jiaming Li, Yukun Chen, Xin Cheng, Wen Yang, Jiayuan Su, 等. 2024. PersonaMath：通过 Persona 驱动的数据增强提升数学推理（PersonaMath: Enhancing Math Reasoning through Persona-Driven Data Augmentation）. arXiv 预印本 arXiv:2410.01504 (2024).

[25] Mostafa Mohammed, Haipeng Cai, 与 Na Meng. 2019. 猴子测试与人工测试的经验比较（工作进展论文）（An empirical comparison between monkey testing and human testing (wip paper)）. 收录于第 20 届 ACM SIGPLAN/SIGBED 国际语言、编译器与嵌入式系统工具会议论文集. 188–192.

[26] Changhai Nie 与 Hareton Leung. 2011. 组合测试综述（A survey of combinatorial testing）. ACM 计算综述 (CSUR) 43, 2 (2011), 1–29.

[27] Minxue Pan, An Huang, Guoxin Wang, Tian Zhang, 与 Xuandong Li. 2020. 基于强化学习的好奇心驱动 Android 应用测试（Reinforcement learning based curiosity-driven testing of android applications）. 收录于第 29 届 ACM SIGSOFT 国际软件测试与分析研讨会论文集. 153–164.

[28] Joon Sung Park, Joseph C O'Brien, Carrie J Cai, Meredith Ringel Morris, Percy Liang, Michael S Bernstein, 等. 2023. 生成式智能体：人类行为的交互模拟（Generative agents: Interactive simulacra of human behavior）. arXiv.Org (2023 年 4 月 7 日) https://arxiv.org/abs/2304.03442 v2 (2023).

[29] Chen Qian 与 Xin Cong. 2023. 面向软件开发的通信式智能体（Communicative agents for software development）. arXiv 预印本 arXiv:2307.07924 6, 3 (2023).

[30] Nils Reimers 与 Iryna Gurevych. 2019. Sentence-BERT：使用孪生 BERT 网络的句子嵌入（Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks）. 收录于 2019 年自然语言处理经验方法会议论文集. 中国香港, 3982–3992. doi:10.18653/v1/D19-1410

[31] Iflaah Salman, Ayse Tosun Misirli, 与 Natalia Juristo. 2015. 软件工程实验中研究生能否代表专业人士？（Are students representatives of professionals in software engineering experiments?）. 收录于 2015 年 IEEE/ACM 第 37 届 IEEE 国际软件工程会议, 第 1 卷. IEEE, 666–676.

[32] Ting Su, Guozhu Meng, Yuting Chen, Ke Wu, Weiming Yang, Yao Yao, Geguang Pu, Yang Liu, 与 Zhendong Su. 2017. Android 应用的引导式随机模型驱动 GUI 测试（Guided, stochastic model-based GUI testing of Android apps）. 收录于 2017 年第 11 届软件工程基础联合会议论文集. 245–256.

[33] Yanqi Su, Zhenchang Xing, Chong Wang, Chunyang Chen, Sherry Xu, Qinghua Lu, 与 Liming Zhu. 2025. LLM 与场景知识引导的自动化肥皂剧测试：可行性、挑战与前行之路（Automated Soap Opera Testing Directed by LLMs and Scenario Knowledge: Feasibility, Challenges, and Road Ahead）. ACM 软件工程会议论文集 2, FSE (2025), 757–778.

[34] Lipeipei Sun, Tianzi Qin, Anran Hu, Jiale Zhang, Shuojia Lin, Jianyan Chen, Mona Ali, 与 Mirjana Prpa. 2025. Persona-L 已进入聊天：利用 LLM 与基于能力的框架为复杂需求用户构建 Persona（Persona-L has Entered the Chat: Leveraging LLMs and Ability-based Framework for Personas of People with Complex Needs）. 收录于 2025 年 CHI 计算机系统人为因素会议论文集. 1–31.

[35] Yao Tong 与 Xiaofang Zhang. 2021. 考虑缺陷严重程度的众包测试报告优先级排序（Crowdsourced test report prioritization considering bug severity）. 信息与软件技术 139 (2021), 106668.

[36] Yu-Min Tseng, Yu-Chao Huang, Teng-Yun Hsiao, Wei-Lin Chen, Chao-Wei Huang, Yu Meng, 与 Yun-Nung Chen. 2024. LLM 中 persona 的两个故事：角色扮演与个性化综述（Two tales of persona in llms: A survey of role-playing and personalization）. arXiv 预印本 arXiv:2406.01171 (2024).

[37] Chenxu Wang, Tianming Liu, Yanjie Zhao, Minghui Yang, 与 Haoyu Wang. 2025. LLMDroid：以大语言模型引导增强移动应用 GUI 测试覆盖率（LLMDroid: Enhancing Automated Mobile App GUI Testing Coverage with Large Language Model Guidance）. ACM 软件工程会议论文集 2, FSE (2025), 1001–1022.

[38] Dingbang Wang, Yu Zhao, Sidong Feng, Zhaoxu Zhang, William G J Halfond, Chunyang Chen, Xiaoxia Sun, Jiangfan Shi, 与 Tingting Yu. 2024. 反馈驱动的 Android 应用全缺陷报告自动复现（Feedback-driven automated whole bug report reproduction for android apps）. 收录于第 33 届 ACM SIGSOFT 国际软件测试与分析研讨会论文集. 1048–1060.

[39] Jue Wang, Yanyan Jiang, Chang Xu, Chun Cao, Xiaoxing Ma, 与 Jian Lu. 2020. Combodroid：通过用例组合为 Android 应用生成高质量测试输入（Combodroid: generating high-quality test inputs for android apps via use case combinations）. 收录于 ACM/IEEE 第 42 届国际软件工程会议论文集. 469–480.

[40] Junjie Wang, Mingyang Li, Song Wang, Tim Menzies, 与 Qing Wang. 2019. 图像不说谎：基于截图信息的重复众包测试报告检测（Images don't lie: Duplicate crowdtesting reports detection with screenshot information）. 信息与软件技术 110 (2019), 139–155.

[41] Junjie Wang, Song Wang, Jianfeng Chen, Tim Menzies, Qiang Cui, Miao Xie, 与 Qing Wang. 2019. 刻画众包人员以更好优化众包测试中的工作者推荐（Characterizing crowds to better optimize worker recommendation in crowdsourced testing）. IEEE 软件工程汇刊 47, 6 (2019), 1259–1276.

[42] Junjie Wang, Ye Yang, Song Wang, Jun Hu, 与 Qing Wang. 2022. 上下文与公平感知的流程内众包测试者推荐（Context- and fairness-aware in-process crowdworker recommendation）. ACM 软件工程方法与技术汇刊 (TOSEM) 31, 3 (2022), 1–31.

[43] Junjie Wang, Ye Yang, Song Wang, Yuanzhe Hu, Dandan Wang, 与 Qing Wang. 2020. 上下文感知的流程内众包测试者推荐（Context-aware in-process crowdworker recommendation）. 收录于 ACM/IEEE 第 42 届国际软件工程会议论文集. 1535–1546.

[44] Qing Wang, Zhenyu Chen, Junjie Wang, 与 Yang Feng. 2022. 智能众包测试（Intelligent Crowdsourced Testing）. Springer.

[45] Xiaoxue Wu, Wenjing Shan, Wei Zheng, Zhiguo Chen, Tao Ren, 与 Xiaobing Sun. 2023. 基于技术术语提取的智能重复缺陷报告检测方法（An intelligent duplicate bug report detection method based on technical term extraction）. 收录于 2023 年 IEEE/ACM 国际软件测试自动化会议 (AST). IEEE, 1–12.

[46] Miao Xie, Qing Wang, Guowei Yang, 与 Mingshu Li. 2017. Cocoon：上下文覆盖约束下的众包测试质量最大化（Cocoon: Crowdsourced testing quality maximization under context coverage constraint）. 收录于 2017 年 IEEE 第 28 届国际软件可靠性工程研讨会 (ISSRE). IEEE, 316–327.

[47] Yiheng Xiong, Ting Su, Jue Wang, Jingling Sun, Geguang Pu, 与 Zhendong Su. 2024. Android 应用的通用实用基于属性的测试（General and practical property-based testing for android apps）. 收录于第 39 届 IEEE/ACM 国际自动化软件工程会议论文集. 53–64.

[48] Yuxuan Yang 与 Xin Chen. 2021. 基于文本分类的众包测试报告优先级排序（Crowdsourced test report prioritization based on text classification）. IEEE Access 10 (2021), 92692–92705.

[49] Juyeon Yoon, Robert Feldt, 与 Shin Yoo. 2024. 自主大语言模型智能体的意图驱动移动 GUI 测试（Intent-driven mobile gui testing with autonomous large language model agents）. 收录于 2024 年 IEEE 软件测试、验证与确认会议 (ICST). IEEE, 129–139.

[50] Shengcheng Yu. 2019. 通过缺陷截图理解进行众包报告生成（Crowdsourced report generation via bug screenshot understanding）. 收录于 2019 年第 34 届 IEEE/ACM 国际自动化软件工程会议. IEEE, 1277–1279.

[51] Shengcheng Yu, Chunrong Fang, Zhenfei Cao, Xu Wang, Tongyu Li, 与 Zhenyu Chen. 2021. 通过深度截图理解对众包测试报告进行优先级排序（Prioritize crowdsourced test reports via deep screenshot understanding）. 收录于 2021 年 IEEE/ACM 第 43 届国际软件工程会议 (ICSE). IEEE, 946–956.

[52] Shengcheng Yu, Chunrong Fang, Mingzhe Du, Zimin Ding, Zhenyu Chen, 与 Zhendong Su. 2024. 实用、自动化的基于场景的移动应用测试（Practical, Automated Scenario-based Mobile App Testing）. IEEE 软件工程汇刊 (2024).

[53] Shengcheng Yu, Chunrong Fang, Xin Li, Yuchen Ling, Zhenyu Chen, 与 Zhendong Su. 2024. 通过图像嵌入与强化学习进行有效、平台无关的 GUI 测试（Effective, Platform-Independent GUI Testing via Image Embedding and Reinforcement Learning）. ACM 软件工程方法与技术汇刊 33, 7 (2024), 1–27.

[54] Shengcheng Yu, Chunrong Fang, Yuchen Ling, Chentian Wu, 与 Zhenyu Chen. 2023. 用于测试脚本生成与迁移的 LLM：挑战、能力与机遇（Llm for test script generation and migration: Challenges, capabilities, and opportunities）. 收录于 2023 年 IEEE 第 23 届国际软件质量、可靠性与安全会议. IEEE, 206–217.

[55] Shengcheng Yu, Chunrong Fang, Ziyuan Tuo, Quanjun Zhang, Chunyang Chen, Zhenyu Chen, 与 Zhendong Su. 2023. 基于视觉的移动应用 GUI 测试：综述（Vision-based mobile app gui testing: A survey）. arXiv 预印本 arXiv:2310.13518 (2023).

[56] Shengcheng Yu, Chunrong Fang, Quanjun Zhang, Zhihao Cao, Yexiao Yun, Zhenfei Cao, Kai Mei, 与 Zhenyu Chen. 2023. 通过深度图文融合理解的移动应用众包测试报告一致性检测（Mobile app crowdsourced test report consistency detection via deep image-and-text fusion understanding）. IEEE 软件工程汇刊 49, 8 (2023), 4115–4134.

[57] Shengcheng Yu, Chunrong Fang, Quanjun Zhang, Mingzhe Du, Jia Liu, 与 Zhenyu Chen. 2024. 通过截图-文本绑定规则进行半监督众包测试报告聚类（Semi-supervised Crowdsourced Test Report Clustering via Screenshot-Text Binding Rules）. ACM 软件工程会议论文集 1, FSE (2024), 1540–1563.

[58] Shengcheng Yu, Yuchen Ling, Chunrong Fang, Quan Zhou, Chunyang Chen, Shaomin Zhu, 与 Zhenyu Chen. 2025. LLM 引导的基于场景的 GUI 测试（LLM-Guided Scenario-based GUI Testing）. arXiv 预印本 arXiv:2506.05079 (2025).

[59] Yakun Zhang, Chen Liu, Xiaofei Xie, Yun Lin, Jin Song Dong, Dan Hao, 与 Lu Zhang. 2024. 基于 LLM 的 GUI 测试迁移抽象与具体化（LLM-based Abstraction and Concretization for GUI Test Migration）. arXiv 预印本 arXiv:2409.05028 (2024).

[60] Yakun Zhang, Qihao Zhu, Jiwei Yan, Chen Liu, Wenjie Zhang, Yifan Zhao, Dan Hao, 与 Lu Zhang. 2024. 基于合成的 GUI 测试用例迁移增强（Synthesis-Based Enhancement for GUI Test Case Migration）. 收录于第 33 届 ACM SIGSOFT 国际软件测试与分析研讨会论文集. 869–881.

---

*收稿日期：2025-09-02；录用日期：2026-03-24*
*Proc. ACM Softw. Eng., Vol. 3, No. FSE, Article FSE166. 出版日期：2026 年 7 月。*
