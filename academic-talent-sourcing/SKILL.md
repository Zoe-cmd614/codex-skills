---
name: academic-talent-sourcing
description: "Orchestrate compliant academic-talent sourcing from research-paper PDFs, including user requests for 快速处理 or 极速处理: extract authors, run bounded public-web enrichment, assess JD fit, choose candidate-specific outreach policy, generate localized recruiter messages, and export either a complete workbook or the 极速处理 single-sheet candidate roster while keeping Agent decisions separate from three deterministic PDF parsing, academic search/assessment, and outreach/export tools."
---

# 论文人才寻访 Agent

## 1. 架构分层与强制边界

采用一层自主 Agent 加三项无自主性的原子 Skill：

```text
用户：PDF（单篇/批量）+ 可选 JD + 已核验岗位事实
  └─ 论文人才寻访 Agent
       ├─ 规划任务、维护状态、判断分支、控制循环和检索预算
       ├─ Skill 1：PDF 解析脚本
       ├─ Skill 2：学术人物定向检索与评估脚本
       └─ Skill 3：猎头触达文案与批量导出脚本
```

### 1.1 Agent 调度层

Agent 是唯一智能调度主体。执行以下职责：

- 接收单篇或多篇论文 PDF、可选 JD、岗位已核验事实、合规地域和组织策略。
- 拆解任务、生成 `run_id`、候选人队列和逐候选人状态机。
- 调用三个 Skill，但不代替 Skill 做 PDF 抽取、指标计算或文案渲染。
- 判断作者同名、资料缺失、联系方式缺失、匹配分阈值、异动等级、暂缓触达和地域语体。
- 生成下一轮查询关键词并循环调用 Skill 2，直至资料达标或达到检索上限。
- 保存结构化上下文记忆、证据 URL、访问时间、失败原因、决策记录和输出路径。
- 根据候选人差异选择正式触达、行政转达、低打扰存档或完全暂缓。

Agent 不得：

- 自行解析 PDF、抓网页、计算 H 指数、计算匹配分或写最终触达文案。
- 把下一轮查询、是否跳过、语言/力度选择交给任一 Skill。
- 用模型猜测邮箱、薪酬、跳槽意向、在途 offer、经费变化或个人敏感信息。

### 1.2 三个底层原子 Skill

每个 Skill 都是确定性、可重复调用、无上下文自主性的工具函数；脚本之间互不调用。

| Skill | 唯一职责 | 不得承担 |
|---|---|---|
| PDF 解析 | 将 PDF 转成论文和作者基础字段 | 联网补人、判断同名、评分、写文案 |
| 学术人物定向检索与评估 | 对一次给定查询做固定多源查询扇出、证据归一、指标/匹配计算 | 改写关键词、决定补搜、决定触达、写文案 |
| 猎头触达与导出 | 按 Agent 已给定的策略渲染多渠道文案并导出 Excel | 联网、判断候选人、改变匹配分或触达策略 |

“多轮检索”必须实现为 Agent 多次调用 Skill 2。Skill 2 内只允许：

- 对同一组 `姓名 + 机构 + 研究方向`做固定的一次多源扇出；
- 对 429/5xx 做不改变查询语义的有限传输重试；
- 对输入证据做确定性聚合、交叉核验和评分。

## 2. 目录与 Agent 调用入口

```text
academic-talent-sourcing/
├─ SKILL.md
├─ agents/openai.yaml
└─ scripts/
   ├─ pdf_parser_script.py
   ├─ academic_search_analysis_script.py
   └─ recruiter_message_generator_script.py
```

运行前设置：

```powershell
$PYTHON = "<python-with-pypdf-and-openpyxl>"
$ROOT = "<academic-talent-sourcing>"
```

三个入口：

```powershell
& $PYTHON "$ROOT/scripts/pdf_parser_script.py" paper1.pdf paper2.pdf --output parsed.json
& $PYTHON "$ROOT/scripts/academic_search_analysis_script.py" --input search-input.json --output profile.json --config providers.json
& $PYTHON "$ROOT/scripts/recruiter_message_generator_script.py" --input outreach-input.json --output-dir deliverables
```

## 3. Agent 状态、记忆与调度逻辑

### 3.1 运行状态

为每位作者保存：

```json
{
  "candidate_key": "sha256(name|paper_doi|source_pdf)",
  "state": "parsed|searching|enriched|archive_only|ready|hold|exported|failed",
  "attempt": 0,
  "query_history": [],
  "evidence": [],
  "profile_path": null,
  "missing_fields": [],
  "decision_log": [],
  "outreach_directive": null
}
```

持久化原则：

- 使用 JSON/JSONL 保存事实和决策；每个事实保留 `source_url`、`retrieved_at`、`source_tier` 和置信度。
- 姓名、邮箱等个人数据只保存到授权工作区；设置业务必要的最短保留期。
- 不把模型推断写成事实；推断必须标记 `inference` 和证据。
- 同一次运行用 `candidate_key` 去重；跨运行仅在合法目的和保留期内复用。

### 3.2 主调度伪代码

```text
初始化 run_id、检索上限（建议每人 4 次）、完备度阈值（建议 0.75）
调用 Skill 1 批量解析所有 PDF
按 candidate_key 去重，逐作者建立状态

FOR 每位候选人:
  query := 姓名 + 当前论文机构 + 细分研究方向
  prior_evidence := []
  FOR attempt IN 1..检索上限:
    调用 Skill 2(query, JD只读上下文, 已核验薪酬基准, prior_evidence)
    保存证据、身份候选、完备度、缺失项和风险信号
    IF 身份歧义高:
       Agent 在下一轮加入机构全称、论文题目/DOI、细分领域降噪
       CONTINUE
    IF 关键字段缺失 AND 未到上限:
       Agent 根据 missing_fields 生成下一轮定向关键词
       CONTINUE
    BREAK

  Agent 选择触达指令:
    IF 刚入职/授权 CRM 明示在途 offer/退出请求/来源风险:
       mode := hold
    ELSE IF JD 存在且 match_score < 40:
       mode := archive_only
    ELSE IF 无公开机构个人邮箱且有公开院系邮箱:
       mode := admin_forward
    ELSE IF 有合规公开机构个人邮箱:
       mode := formal
    ELSE:
       mode := archive_only

  Agent 根据地域选择 zh_cn / en_us / en_eu
  Agent 根据 move_window 选择 low_pressure / balanced / direct
  Agent 标记竞品人才并写入 opportunity.differentiators（必须是已核验事实）

汇总所有标准化画像与 outreach_directive
调用 Skill 3 一次批量生成素材和 Excel
检查导出清单、失败表和合规审计表
```

### 3.3 动态分支规则

| 场景 | Skill 返回字段 | Agent 下一步 |
|---|---|---|
| 同名作者 | `identity.ambiguity = high` | 加机构、论文 DOI/题目、细分领域二次检索 |
| 无个人邮箱 | `contacts.public_institutional=[]` | 搜院系通讯录；仍无则行政转达或只存档 |
| 信息不全 | `completeness.missing_fields` | 按缺失字段扩充关键词，受 `max_attempts` 限制 |
| 匹配分低 | `jd_match.score < 40` | 不生成正式挖角邮件，仅存标签 |
| 高/中/低窗口 | `mobility.window` | 选择 direct / balanced / low_pressure |
| 刚入职 | `mobility.recent_join=true` | `hold`，生成内部低打扰备注，不发消息 |
| 在途 offer | 授权 CRM 信号 | `hold`；不得从公开网页臆测 |
| 无公开成果切入 | `outreach_anchor=null` | 禁止正式生成，补搜公开论文/演讲 |

### 3.4 快速处理模式

用户说“快速处理”“快速初筛”或同义表达时，Agent 使用受限预算完成论文级初筛和最小公开信息补全，不运行 H 指数、引用量、异动信号、薪酬与合作图谱的深度检索，除非用户另行要求。

快速模式必须逐作者输出且不得删减：`priority`（A/B/C，由 Agent 决定）、`name`、`current_institution`、`paper_role`、`research_track`、`talent_tags`、`jd_match_score`（0–100，注明快速论文证据分）、`academic_tags`、`public_institutional_email`、`departmental_forward_email`、`homepage`、`outreach_single_version`。仅有论文机构时明确标注“论文机构/待核验当前任职”；院系邮箱不得冒充个人邮箱；homepage 仅接受官方个人主页、实验室主页或机构人员页。

字段没有可靠公开证据时写入 `未检索到（待核验）`，同时保留已检索来源和缺失原因；不得留空、猜测邮箱或把论文机构直接冒充当前机构。每位作者至少执行一次“姓名 + 机构 + 论文题目/赛道”的定向查询；若 homepage、个人机构邮箱和院系待转邮箱均缺失，Agent 允许再执行一次仅针对这三项的兜底查询，随后停止。

触达版本由 Agent 写入 `outreach_directive.quick_outreach_variant`：

- `academic`：论文/学术合作切入；
- `full_time`：全职岗位切入；
- `advisory`：兼职顾问切入；
- `admin_forward`：仅向院系行政礼貌请求转达；
- `archive_note`：低分、身份冲突或暂缓候选人的内部素材，不得发送。

快速模式仍遵守隐私与合规红线。没有公开个人机构邮箱时优先输出院系待转稿；两类邮箱都没有时仅输出不含收件人的草稿并标注“不可发送”。正式素材固定为三段：论文成果破冰 → JD 精准匹配 → 礼貌轻量邀约。

### 3.5 极速处理模式

用户明确说“极速处理”时，优先采用本模式；不要将其解释为普通“快速处理”。检索预算、逐作者核验、身份降噪、隐私过滤、JD 快速论文证据评分和单版本触达规则与快速模式相同，但用户侧交付必须收敛为一个 `.xlsx` 文件。

该工作簿必须：

- 仅含一个名为 `候选人清单` 的工作表，不生成或保留评分明细、来源合规、论文解析、触达素材等其他工作表；
- 每位论文作者占一行，不因信息不全而删除作者；
- 按固定顺序输出：`优先级`、`姓名`、`当前机构`、`论文角色`、`研究赛道`、`人才标签`、`JD匹配分`、`学术标签`、`个人公开机构邮箱`、`院系/机构待转邮箱`、`Homepage`、`单版本触达素材`、`论文机构`、`触达模式`、`当前信息置信度`、`证据URL`；
- 将 `Homepage` 固定在第 K 列，将逐人证据链接写入 `证据URL`；
- 对缺失公开证据的字段明确写 `未检索到（待核验）`，不得留空或猜测；
- 仅使用公开机构联系方式，过滤私人未公开邮箱，并区分个人机构邮箱与院系/招聘代转渠道；
- 使所有显示值在单表内自包含；不得保留对已删除工作表的公式引用；
- 保留筛选、冻结表头、自动换行、优先级配色和 JD 分数可视化，导出后执行公式错误扫描和整表视觉检查。

若未提供 JD，将 `JD匹配分` 写为 `未评分（未提供JD）`，不得虚构分数。极速模式仅改变交付范围，不降低身份核验和合规要求。Agent 仍负责模式选择、检索轮次、优先级、触达策略与异常分支；三个底层 Skill 的原子职责保持不变。Agent 将 `export_profile=ultra_fast_single_sheet` 传给 Skill 3，Skill 3 仅按指令渲染单表，不得自行决定启用极速模式。

## 4. Skill 1：PDF 解析

### 4.1 能力

仅批量解析 PDF，输出：

- 论文标题、DOI、期刊、发表日期、关键词/研究方向；
- 全部作者、作者顺序、通讯作者；
- 作者机构、论文署名公开邮箱；
- 资助机构、项目号、来源页码和字段置信度；
- 解析警告、加密/扫描件/文本不足状态。

优先使用可选 GROBID TEI；不可用时使用 `pypdf` 的本地规则解析。解析兜底是固定管线，不是自主分支。

### 4.2 输入

CLI 接收 PDF 文件或目录。目录默认递归收集 `.pdf`。可选：

- `--grobid-url`：已授权的 GROBID 服务根地址；
- `--language-hint`：`auto|zh|en`；
- `--output`：UTF-8 JSON 输出路径。

### 4.3 输出核心

```json
{
  "schema_version": "1.0",
  "papers": [{
    "source_pdf": "absolute/path/paper.pdf",
    "paper": {"title": "", "doi": "", "journal": "", "funding": []},
    "authors": [{
      "name": "", "order": 1, "is_corresponding": false,
      "institutions": [], "paper_emails": [], "research_directions": []
    }],
    "quality": {"parser": "grobid|pypdf", "confidence": 0.0, "warnings": []}
  }],
  "errors": []
}
```

不得把 PDF 中未明确绑定到作者的邮箱强行绑定；保留为论文级未分配邮箱。

## 5. Skill 2：学术人物定向检索与人才评估

### 5.1 能力

一次调用仅执行一个语义查询：

- 固定优先级检索：实验室/院系官网 > Google Scholar 发现页 > ResearchGate > LinkedIn 公开页 > 行业会议资料；
- 可选调用 OpenAlex、Semantic Scholar、Crossref、ORCID 和合规通用搜索 API；
- 不登录、不绕过验证码、不抓取受限页面；Scholar/ResearchGate/LinkedIn 只记录公开搜索发现信息或授权 API 数据；
- 对姓名、机构、研究方向进行多源交叉核验，返回同名歧义；
- 汇总引用量、H 指数、论文、公开演讲、任职、基金、合作者和公开机构联系方式；
- 计算 JD 0–100 匹配分、优势、缺口、学术标签、异动信号和薪酬基准引用；
- 内置逐主机最小间隔、指数退避、`Retry-After`、最大重试和请求日志。

### 5.2 输入边界

检索关键词只允许：

```json
{"name": "姓名", "institution": "当前/论文机构", "research_direction": "细分方向"}
```

`evaluation_context` 是只读数据，不参与自主改写查询：

```json
{
  "jd": {"title": "", "must_have": [], "preferred": [], "location": "", "level": ""},
  "salary_benchmarks": [{"region": "", "role": "", "currency": "", "min": 0, "max": 0, "source_url": "", "as_of": "YYYY-MM-DD"}],
  "prior_evidence": [],
  "authorized_crm_signals": {"in_flight_offer": false}
}
```

密钥和限流参数放在 provider 配置或环境变量，不能写入候选人文件。

### 5.3 输出与评分

关键输出：

- `identity`：最佳身份、候选身份列表、歧义等级、多源一致性；
- `academic_metrics`：引用量、H 指数、论文量、指标来源和日期；
- `career`、`grants`、`public_talks`、`collaboration_graph`；
- `contacts.public_institutional` 和 `contacts.departmental`；
- `jd_match.score/strengths/gaps/components`；
- `mobility.window/recent_join/signals/hold_recommended`；
- `salary_reference`：仅从传入且在有效期内的基准选择，否则返回 `needs_verified_benchmark`；
- `completeness.score/missing_fields`、`evidence`、`risk_flags`。

JD 分数固定权重：

| 维度 | 分值 |
|---|---:|
| 研究方向 | 35 |
| 方法/技术 | 20 |
| 职级与资历 | 15 |
| 产业转化 | 15 |
| 团队/项目领导力 | 10 |
| 地域可行性 | 5 |

不得因年龄、性别、国籍、族裔、婚育、健康、宗教等受保护属性评分。`青年潜力学者`只能依据职业阶段、成果增长和公开职级，不得推断年龄。

### 5.4 异动与关系图谱

- 刚入职：有公开任职起始日期且距检索日不超过 180 天。
- 项目临近结题：公开项目结束日期在 365 天内；180 天内可作为较强但非决定性信号。
- 经费变化：必须有公开、可引用的正式记录；不得根据新闻沉默或项目数量猜测。
- 在途 offer：只能来自用户授权 CRM，不能通过公开网页推断。
- 合作图谱：只列公开共作者关系、共同论文数和来源；“竞品机构”由 Agent 对照用户名单标注。
- 异动窗口不是个人意愿事实，只是业务排期信号，必须标注 `inference`。

## 6. Skill 3：猎头触达文案生成与批量导出

### 6.1 能力

仅接收最终标准化画像及 Agent 写入的 `outreach_directive`，确定性输出：

- A 学术深耕版：研发经费、前沿课题、产业转化平台；
- B 全职流动版：已核验薪酬、商业化、团队规模；
- C 兼职顾问版：低压力学术合作破冰；
- 7 天未回复跟进邮件；
- LinkedIn 好友申请附言和联动私信；
- 无个人邮箱时的院系行政代转模板；
- GDPR/隐私免责声明、退出联系说明；
- `.xlsx` 人才清单、`messages.json` 和 `compliance_audit.json`。

脚本不自行决定模式、语言或力度。Agent 必须传：

```json
{
  "mode": "formal|admin_forward|archive_only|hold",
  "locale": "zh_cn|en_us|en_eu",
  "intensity": "direct|balanced|low_pressure",
  "anchor_preference": "paper|public_talk",
  "competitor_talent": false,
  "opportunity": {
    "role_title": "", "organization": "", "verified_facts": [],
    "research_platform": "", "team_scope": "", "compensation": "",
    "differentiators": []
  }
}
```

批次级 `controller` 必须提供组织核验后的 `organization`、`contact_email`、`legal_basis`、`retention_period` 和 `privacy_notice_url`；欧盟/英国任务建议再提供 `supervisory_authority_url`。脚本只渲染这些事实，不替组织选择法律依据。

### 6.2 邮件强制规范

每封正式初次邮件必须：

1. 主题简洁正式，不使用夸张、制造紧迫感或诱导性措辞。
2. 开篇引用本次论文题目或有 URL 的公开演讲观点；不能捏造观点或逐字引语。
3. 使用固定三段：论文/演讲调研破冰 → 岗位精准匹配 → 礼貌轻量邀约。
4. 只写 `opportunity.verified_facts` 中可证实的信息；未提供薪酬时不写薪酬承诺。
5. 附隐私来源、数据用途、退出联系和 GDPR/适用隐私法说明。

地域语体：

- `zh_cn`：中文、克制、尊称明确，突出课题和平台事实。
- `en_us`：简洁直接、短段落、明确但低压力的 15 分钟邀请。
- `en_eu`：正式审慎，说明联系依据、数据来源和退出方式。

`archive_only` 与 `hold` 不生成可发送的正式邮件，只输出内部存档备注。`admin_forward` 只生成请求院系行政转达的模板，不把行政邮箱称为候选人邮箱。

### 6.3 Excel 列

至少包含：

`优先级、姓名、当前机构、论文角色、赛道、人才标签、JD匹配分、学术标签、公开机构邮箱、院系待转邮箱、Homepage、单版本触达素材、地区、竞品人才、引用量、H指数、优势、缺口、异动窗口、触达模式、暂缓原因、院系代转邮件、A/B/C邮件、7天跟进、LinkedIn附言、LinkedIn私信、证据URL、检索时间、合规状态`。

快速模式必须把前十二列放在主表最左侧；深度模式可继续输出后续完整字段。`Homepage` 无结果时写 `未检索到（待核验）`，不得用聚合搜索页冒充官方主页。

极速模式使用 `export_profile=ultra_fast_single_sheet`，并严格按 3.5 节输出唯一的 `候选人清单` 工作表。单表中的优先级和 JD 分数必须写入可独立读取的最终值，不能引用未导出的辅助表。

## 7. 增值模块落地方式

### 7.1 人才深度评估

- 学术影响：Skill 2 使用明确来源的引用量/H 指数；按职业阶段、领导力和产业证据打 `potential_scholar`、`lab_leader`、`industry_translator` 标签。
- JD 匹配：Skill 2 用固定权重输出 0–100、优势和能力缺口；Agent 仅使用阈值决策。
- 异动信号：Skill 2 提取公开时间线并给排期等级；Agent 决定力度或暂缓。
- 合作图谱：Skill 2 输出共作者边；Agent 对照竞品清单生成内推思路，禁止骚扰第三方。
- 薪酬参考：Agent 提供带来源、地域、币种、日期的基准；Skill 2 只做匹配和区间输出。

### 7.2 检索与限流

- Agent 优先请求官网证据，随后按 Scholar、ResearchGate、LinkedIn、会议资料补充。
- Skill 2 对每个主机限速；默认间隔 1.2 秒、最多 2 次传输重试、429 尊重 `Retry-After`。
- Agent 每人建议最多 4 个语义查询；达到上限后保留缺失项，不无限循环。
- 网络失败时可用 `fixture`/既有证据重放，输出明确 `partial`，不得伪造完整。

### 7.3 批量与个性化

- Skill 1 接受多 PDF；Agent 去重并批量排队；Skill 3 一次导出多人。
- Agent 标注赛道、国内/海外、竞品人才、异动窗口；Skill 3 原样写入清单。
- 竞品人才只有在 `differentiators` 为已核验事实时才写差异化内容。
- Skill 2 输出 `outreach_anchors`；Agent 可用 `anchor_preference` 选择论文或公开演讲，Skill 3 原样嵌入开篇；没有锚点就补搜或存档。

## 8. 异常处理

| 异常 | Skill 行为 | Agent 行为 |
|---|---|---|
| 加密/损坏 PDF | Skill 1 写入 `errors` | 标记失败并请求合法副本 |
| 扫描件无文本 | Skill 1 返回 `needs_ocr` | 调用授权 OCR 预处理后重试 Skill 1 |
| 作者与机构错位 | Skill 1 降低置信度 | Agent 将机构和 DOI 加入 Skill 2 查询核验 |
| 同名结果并列 | Skill 2 返回高歧义 | Agent 改写下一轮关键词，不自动合并 |
| 429/5xx | Skill 2 固定退避并记日志 | 达到预算后延后任务 |
| 来源相互冲突 | Skill 2 保留各来源 | Agent 优先官网和时间更新者，记录理由 |
| 指标缺失 | 返回 `null`，不补零 | Agent 可补搜，导出时保留未知 |
| 邮箱为私人域/无来源 | Skill 2 过滤并记风险 | 不触达；寻找公开机构或院系邮箱 |
| 无公开成果锚点 | Skill 3 拒绝正式模板 | Agent 补搜或存档 |
| Excel 写入失败 | Skill 3 保留 JSON 并非零退出 | Agent 修复路径/权限后重跑 |
| 单候选失败 | 写入失败表 | 批处理继续，其余候选不回滚 |

## 9. 猎头业务使用规范

- 先确定具体岗位和合法联系目的，再收集最小必要数据。
- 人工发送前复核身份、论文锚点、岗位事实、称谓、语言、联系方式和退出机制。
- 不把 H 指数作为唯一筛选条件；不同学科、职业阶段不可直接横比。
- 不承诺未经确认的薪酬、编制、经费、签证、职级、团队规模或知识产权条件。
- 不通过合作者、学生或行政人员施压；行政模板只能礼貌请求转达一次。
- 7 天跟进最多按组织政策执行；退出、拒绝或无意愿后立即停止并加入抑制名单。
- 对 `hold` 候选人只做内部低打扰存档，不生成绕过限制的其他渠道话术。

## 10. 海外合规与红线

本工具提供流程护栏，不替代适用法域的法律意见。执行前由组织确认 GDPR/UK GDPR、ePrivacy、CAN-SPAM、CCPA/CPRA 及当地劳动与反歧视规则。

绝对红线：

- 仅使用机构公开公示、与职业联系合理相关的邮箱；过滤私人、泄露、购买或推测的联系方式。
- 不登录绕过权限、不规避验证码/robots/平台限制、不批量抓取受限 LinkedIn 或 ResearchGate 页面。
- 不收集或推断受保护属性、家庭情况、健康、政治、宗教、精确住址或私人社交资料。
- 不根据国籍等受保护属性做录用评分；地域字段仅用于语言、时区和已核验岗位可行性。
- 记录数据来源、用途、保留期、合法基础、访问控制和删除/异议处理。
- 每封邮件附控制方、数据类别与公开来源、联系目的、组织核验的法律依据、保留期、异议/退出/访问/更正/删除渠道和完整隐私说明；欧盟/英国对象另提供投诉/监管渠道。
- 刚入职、已明确在途 offer、已拒绝或要求不联系者必须 `hold`/抑制。
- 自动生成内容必须由授权招聘人员复核后发送；本 Skill 不执行自动发送。

建议免责声明：

```text
我们仅使用您所在机构公开发布的职业联系信息，就与您研究方向相关的机会与您联系。
如您不希望继续收到联系，或希望查询、更正、删除相关信息，请直接回复告知，我们将及时处理。
```

## 11. 依赖与接口

必需：

- Python 3.11+；
- `pypdf>=5.0`；
- `openpyxl>=3.1`。

可选：

- GROBID 服务：高质量 PDF 结构化解析；
- OpenAlex（API key）、Crossref 公共接口、ORCID Public API（`/read-public` token）；
- Semantic Scholar API（建议密钥）；
- Brave Search 或 Serper API：公开网页发现；
- 组织批准的 OCR 工具处理扫描件。

接口约定：

| 服务 | 脚本调用接口 | 认证/用途 |
|---|---|---|
| GROBID | `POST /api/processFulltextDocument` | 组织自建或已授权服务；返回 TEI XML |
| OpenAlex | `GET /authors`、`GET /works?filter=authorships.author.id:...` | `OPENALEX_API_KEY`；作者指标、论文、共作者 |
| Crossref | `GET /works` | 建议 `mailto` polite pool；论文、作者、机构、资助元数据 |
| Semantic Scholar | `GET /graph/v1/author/search` | 可选 `S2_API_KEY`；作者指标和论文 |
| ORCID | `GET /v3.0/expanded-search` | `ORCID_ACCESS_TOKEN`（`/read-public`）；公开身份核验 |
| Brave Search | `GET /res/v1/web/search` | `BRAVE_SEARCH_API_KEY`；官网/平台/会议公开结果发现 |

接口版本和条款可能变化；部署时按各服务官方文档更新 provider 配置，不得以页面抓取绕过 API 限制。

Provider 配置示例：

```json
{
  "contact_email": "research-ops@example.org",
  "min_interval_seconds": 1.2,
  "max_transport_retries": 2,
  "providers": {
    "openalex": {"enabled": true, "api_key_env": "OPENALEX_API_KEY"},
    "crossref": {"enabled": true},
    "orcid": {"enabled": false, "access_token_env": "ORCID_ACCESS_TOKEN"},
    "semantic_scholar": {"enabled": false, "api_key_env": "S2_API_KEY"},
    "brave": {"enabled": false, "api_key_env": "BRAVE_SEARCH_API_KEY"}
  }
}
```

所有接口都以 UTF-8 JSON 交换；日期使用 ISO 8601；未知值用 `null`，不能用空字符串冒充零；金额必须带币种、地域、期间和来源。

## 12. 验收清单

- Agent/Skill 边界明确，脚本之间无互调。
- Skill 1 能批量解析并保留来源和错误。
- Skill 2 单次固定查询、限流、交叉核验、评分与缺失项可机器读取。
- 同名和缺失资料由 Agent 发起下一轮，不在 Skill 内循环改词。
- 匹配分低于 40、无合规邮箱、刚入职等策略由 Agent 写入指令。
- Skill 3 覆盖三种邮件、三地域语体、7 天跟进、LinkedIn、行政转达、免责声明和 Excel。
- 正式邮件首段引用论文/公开演讲，严格三段结构。
- 私人邮箱过滤、退出机制、证据审计、抑制名单和人工复核生效。
- 三脚本 `--help`、样例批处理和 Skill 官方校验全部通过。

## 13. Codex 最优运行配置

在 Codex 中建议：

- 模型：`GPT-5.6 Terra`；
- 推理强度：`极高（xhigh）`；
- 上下文窗口：调至产品允许的最大值；
- 网络：仅为已批准的公开来源和 API 开放；
- 单候选语义检索上限：4 次，传输重试不计入语义轮次；
- 并发：遵守 provider 条款，默认低并发并启用逐主机限流；
- 输出：开启工作区持久化、逐候选审计日志和最终人工复核。

