---
name: academic-advisor-mapping
description: Build evidence-backed advisor-to-publication-to-advisee mappings with public homepages, current public professional emails, and verified OpenReview profiles. Use for 教授/导师 mapping、学生名单核验和实验室学术关系图；do not use for outreach writing, PDF-author sourcing, broad recruiting pipelines, private-contact discovery, or guessed identities.
metadata:
  short-description: 导师论文、学生与公开联系信息 Mapping
---

# Academic Advisor Mapping

把教授论文、学生关系与公开联系入口整理成可筛选、可复核的结构化成果。默认以核验当日为快照日期，并明确区分“完整公开名册”和“公开可核验子集”。

## 默认交付

若用户未指定格式，优先交付一个工作簿，包含：

1. `导师总览`：身份、研究方向、论文/学生计数和主索引。
2. `论文清单`：逐条论文或索引记录及可用链接。
3. `学生与主页`：学生/被指导者、导师关系证据、Homepage、当前公开职业邮箱和 OpenReview Profile，并附核验依据。
4. `说明与入口`：来源、证据等级、覆盖边界和更新入口。

编辑已有文件时保留原有结构和风格。创建或修改 `.xlsx` 时，遵循可用的电子表格技能完成公式重算、错误扫描、渲染和导出检查。

## 核心流程

1. **身份消歧**：用机构教师页、本人主页、DBLP、ORCID 和实验室页面交叉确认同一人；记录精确 profile URL，并为导师与学生分别建立稳定 `Faculty Key` / `Person Key`。名称和机构只能辅助消歧，不能单独作为合并主键。
2. **论文枚举**：优先本人逐篇论文页和精确 DBLP 作者档案；Google Scholar/ORCID 用于补充与持续更新。保留论文直链和索引记录链接。
3. **学生映射**：优先导师本人学生页、机构名册和实验室成员页，再用学生本人主页/CV核验导师关系。共同作者或普通实验室成员不能自动推定为学生。
4. **联系信息补齐**：对每位导师和学生都尝试查找 Homepage、当前公开职业邮箱、邮箱—人物绑定证据、OpenReview Profile、核验依据和核验日期。详细字段和判定规则见 [references/output-schema.md](references/output-schema.md)。
5. **证据分级**：区分正式学位学生、联合培养、访问/科研实习、本科科研指导、博士后以及仅实验室归属。
6. **覆盖说明**：明确论文是“索引记录数”还是去重成果数；学生是“人物数”还是“培养阶段记录数”。没有完整公开名册时，必须标注为公开可核验子集。

## 联系信息规则

- 默认只收录本人主页、本人 CV、当前机构页面等公开来源明确展示的当前职业/机构邮箱。公开个人邮箱只有在用户明确要求时才纳入，并单独标注类型。
- 不依据姓名、单位邮箱域名或同组成员格式猜测邮箱。
- 可解析明确的公开混淆写法（如 `name [at] domain [dot] edu`），但要保留原始来源并标记“由公开混淆格式解析”。
- 实验室公共邮箱、共同作者邮箱或论文通讯邮箱不能直接绑定给某位学生；必须记录邮箱—人物绑定证据和邮箱类型。历史论文邮箱只可标为“已核验—历史”。
- 若只有联系表单，写入专门的 `Contact Form URL` 字段，不伪造邮箱。
- OpenReview Profile 使用强锚点核验：本人主页/CV/机构页直接链接即可；否则必须是姓名一致，再加机构/邮箱域名一致或至少两篇论文/合作者匹配。仅“姓名 + 泛化研究方向”不足以核验。
- 未核验的 OpenReview 候选只放在候选字段，状态写“候选未纳入”，不得写入正式 Profile 列。
- 不绕过登录、权限或反爬限制，不收集非公开或泄露数据。

## 证据等级

- **A**：机构官方页面、导师本人名册或学生本人主页/CV直接写明导师关系。
- **B**：本人或机构材料确认科研/项目指导，但不是正式学位导师关系，或培养层次信息不完整。
- **C**：仅确认实验室归属、合作、课程或共同作者；不视为正式学生关系。

## 质量检查

- 每条人物记录都应有关系证据 URL；没有证据时不得写成确定性师生关系。
- 邮箱和 OpenReview 不得只填 URL；同时填写来源、人物绑定/核验依据、状态和 `verified_at`。区分“未检索”“未找到”“访问受限”和“候选未纳入”。
- 检查同一人的中英文名、邮箱、Homepage 和 OpenReview URL 是否重复或冲突。
- 论文预印本和正式出版可能分别成行；若未做成果级去重，要在说明页明确披露。
- 工作簿计数优先使用公式，并在导出前重算；检查 `#REF!`、`#DIV/0!`、`#VALUE!`、`#NAME?`、`#N/A`、`#NUM!`、`#NULL!`、`#SPILL!`、`#CALC!`。
- 最终回复给出关键计数、主要覆盖限制和可继续更新的权威入口。

