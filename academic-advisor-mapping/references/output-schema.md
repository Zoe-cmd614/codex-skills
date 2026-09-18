# 输出字段与核验口径

仅在实际制作 Mapping 或工作簿时读取本文件。

## 导师总览

建议字段：

| 字段 | 说明 |
|---|---|
| Faculty Key | 优先使用 ORCID、DBLP PID 或本人主页 canonical URL；显示名不作为关系主键 |
| 教授 / Faculty | 中英文姓名；保留消歧后的规范写法 |
| Snapshot Date | 本轮核验日期 |
| 机构与职位 | 当前官方页面口径 |
| 研究方向 | 官方简介或本人主页摘要 |
| 论文记录数 | 说明是索引记录还是去重成果 |
| 学生记录数 | 说明是人物数还是阶段记录数 |
| 明确目标机构关联数 | 仅在用户指定目标机构时启用，并只统计直接证据 |
| Homepage | 本人或实验室主页 |
| Public Professional Email | 当前公开职业/机构邮箱；没有则留空并填写状态 |
| Email Type | 个人机构邮箱 / 公开职业邮箱 / 实验室公共邮箱 / 历史论文通讯邮箱 |
| Email Source URL | 展示邮箱的公开来源 |
| Email Person-Binding Evidence | 说明来源如何把邮箱绑定到该人物；团队/共同作者地址不得冒充个人地址 |
| Email Status | 已核验—当前 / 已核验—历史 / 仅联系表单 / 候选未纳入 / 未找到 / 未检索 / 访问受限 |
| Email Verified At | 核验日期 |
| Contact Form URL | 只有表单而无公开邮箱时使用 |
| OpenReview Profile | 核验后的个人 Profile URL |
| OpenReview Candidate URL | 同名但尚未核验的候选；不得混入正式 Profile |
| OpenReview Evidence URL | 支撑身份绑定的主页、机构页或论文页 |
| OpenReview Evidence Summary | 使用的强锚点或匹配信号 |
| OpenReview Status | 已核验 / 候选未纳入 / 未找到 / 未检索 / 访问受限 |
| OpenReview Verified At | 核验日期 |
| DBLP / Scholar / ORCID | 精确身份索引 |
| 学生或团队入口 | 导师本人学生页、实验室团队页或机构名册 |
| 覆盖说明 | 完整公开名册或公开可核验子集 |

## 论文清单

建议字段：

`Faculty Key`、`导师`、`年份`、`论文题目`、`作者`、`会议/期刊`、`论文/DOI链接`、`DBLP/来源记录`、`OpenReview论文页（如适用）`、`来源批次`、`核验/备注`。

规则：

- 使用精确作者 profile，避免同名作者混入。
- 优先提供论文页、DOI、arXiv 或出版社链接；同时保留索引记录 URL 便于追溯。
- OpenReview forum 仅在能与题目、作者、会议相匹配时填入。
- 未进行成果级去重时，预印本和正式版可分别成行，但必须披露。

## 学生与主页

建议字段：

基础字段：`Faculty Key`、`Person Key`、`导师`、`姓名/Name`、`培养层次/关系`、`年份/阶段`、`导师关系证据摘要`、`当前状态/去向`、`研究方向`、`Homepage/Profile`、`Homepage状态`、`导师关系证据 URL`、`其他 URL`、`证据级别`、`备注`、`Snapshot Date`。

仅在用户指定目标机构时增加：`目标机构关联等级`、`目标机构关联说明`。

联系字段：`Public Professional Email`、`Email Type`、`Email Source URL`、`Email Person-Binding Evidence`、`Email Status`、`Email Verified At`、`Contact Form URL`、`OpenReview Profile`、`OpenReview Candidate URL`、`OpenReview Evidence URL`、`OpenReview Evidence Summary`、`OpenReview Status`、`OpenReview Verified At`。

## 邮箱判定

来源优先级：

1. 本人主页或本人 CV 明确标注的当前职业邮箱。
2. 当前机构教师/学生页面明确绑定给该人的邮箱。
3. 已核验学术 Profile 明确公开并能绑定到该人的职业邮箱。
4. 论文页面的公开通讯邮箱只能作为历史信息，并标记 `已核验—历史`。

默认不收录个人 Gmail/QQ 等非机构邮箱；只有用户明确要求时才可纳入，并将 `Email Type` 标为“公开个人邮箱”。实验室公共邮箱可作为团队入口保存，但不得填入个人邮箱字段。

禁止事项：

- 不根据 `firstname.lastname@institution` 等模式生成地址。
- 不把搜索摘要中无法打开验证的字符串写成已核验邮箱。
- 不把实验室、课题组或共同作者邮箱绑定给没有直接证据的个人。
- 不使用数据泄露、私人通讯录、付费墙后非公开信息或登录后才可见的隐私字段。

## OpenReview 判定

满足任一强锚点即可标记“已核验”：

- 本人主页、本人 CV 或机构个人页直接链接该 OpenReview Profile。
- Profile 的已验证邮箱域名和当前机构一致，且姓名一致。
- 姓名一致，并有至少两篇论文/两组特征性合作者与本人主页或 DBLP 对应。

“姓名 + 泛化研究方向”不能作为充分证据。只找到同名 Profile 且无法消歧时：写入 `OpenReview Candidate URL`，状态写“候选未纳入”，正式 `OpenReview Profile` 留空。记录 `OpenReview Evidence URL`、证据摘要和核验日期，便于复查。

## 去重与计数

- `Faculty Key` / `Person Key` 优先采用 ORCID、OpenReview ID、DBLP PID 或本人主页 canonical URL。没有稳定公开标识时生成本工作簿内的本地键，并保持独立；不得仅因“同名 + 同机构”自动合并。
- 同一人物跨硕士、博士、访问等阶段可保留多条阶段记录，但总览必须写“阶段记录数”。
- Homepage、邮箱和 OpenReview 的覆盖率应分别统计；阶段记录数与唯一人物数不可混用。

