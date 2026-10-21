# 真实开发尝试矩阵：a001–a027

截至 a027 的已结算记录；未新增模型、ORCA、科学计算或回归测试。当前仅 T1 a010 的原始证据加只读选型理由补充可作为 1/7 交付证据。它来自历史冻结版本，不能称为同一最新版本七题通过；正式三次重复、独立参考与控制执行均未完成。

Flash = deepseek-flash；Pro = deepseek-v4-pro。tokens 仅列已知用量；未知占用见下文。wall 为 driver 秒数。a001 未发现已建立目录/结算证据，不按零成本运行计算。

| 候选 | 题目 | 模型 | profile | prompt | outcome / delivery | 调用 | 已知 tokens | 已知 ORCA | wall 秒 | 关键事实/阻碍 |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| a001 | — | — | — | — | not_recorded | — | — | — | — | 未发现已建立候选目录或结算证据；不能编造运行、失败或费用。 |
| [a002](<E:/BG6022-V4/data/live-repair/attempts/a002/outcome.json>) | H1 | Flash | thinking_low | v79 | failed / partial | 1 | 0 | 0 | 31.094 | 传输失败，有限纠正/重试用尽；未完成 Goal 定义，token 用量未知。 |
| [a003](<E:/BG6022-V4/data/live-repair/attempts/a003/outcome.json>) | H1 | Flash | thinking_low | v79 | paused / partial | 16 | 339003 | 1 | 467.625 | H1 原始张量已有且自洽，但当前执行/Result 上下文遗漏，人工暂停；交付仍缺项。 |
| [a004](<E:/BG6022-V4/data/live-repair/attempts/a004/outcome.json>) | H1 | Flash | thinking_low | v80 | paused / partial | 20 | 366348 | 1 | 403.984 | 短进程启动证明遗漏，暂停对账；另有两次启动不能确认，保留未知占用。 |
| [a005](<E:/BG6022-V4/data/live-repair/attempts/a005/outcome.json>) | T2 | Flash | thinking_low | v80 | failed / partial | 10 | 197392 | 4 | 498.719 | 4 次单点已有；旧最低证据/全局条件及来源清单门阻碍通用交付，seed 来源信息丢失。 |
| [a006](<E:/BG6022-V4/data/live-repair/attempts/a006/outcome.json>) | T2 | Flash | thinking_low | v81 | paused / partial | 7 | 143641 | 0 | 257.063 | 过宽 original_text 把原生执行要求分配给 task-only Goal，科学启动前暂停；另有 JSON 格式拒绝。 |
| [a007](<E:/BG6022-V4/data/live-repair/attempts/a007/outcome.json>) | T2 | Flash | thinking_low | v81 | failed / partial | 12 | 230968 | 6 | 668.797 | 6 次单点已有，来源清单拒绝后过早部分停止；原 RMSD 表标签/数值另有错误。 |
| [a008](<E:/BG6022-V4/data/live-repair/attempts/a008/outcome.json>) | H1 | Flash | thinking_low | v82 | paused / partial | 12 | 258709 | 0 | 380.937 | 纯元数据 Python 被误作科学启动，关闭后续合法方法/基组选型；原生未启动。 |
| [a009](<E:/BG6022-V4/data/live-repair/attempts/a009/outcome.json>) | H1 | Flash | thinking_low | v82 | failed / partial | 12 | 221792 | 1 | 267.015 | 原生 NMR 语法错误；外层 Python completed 不代表内部成功，旧终止建议允许直接部分停止。 |
| [a010](<E:/BG6022-V4/data/live-repair/attempts/a010/outcome.json>) | T1 | Flash | thinking_low | v82 | completed / complete | 21 | 398392 | 1 | 585.078 | T1 已完成；原报告漏选型理由，已有引用原回执的只读补充。可交付证据来自该历史版本。 |
| [a011](<E:/BG6022-V4/data/live-repair/attempts/a011/outcome.json>) | H1 | Flash | thinking_low | v83 | failed / partial | 24 | 485195 | 1 | 636.718 | 原生张量已有；分析来源 task/ 被重复拼接拒绝，待分析 gap 未呈现已有源的后续路线。 |
| [a012](<E:/BG6022-V4/data/live-repair/attempts/a012/outcome.json>) | H1 | Flash | thinking_low | v83 | paused / partial | 10 | 162085 | 0 | 138.531 | 反复 EOF 缺闭合及 reason 层级错误，零 Attempt 时人工暂停。 |
| [a013](<E:/BG6022-V4/data/live-repair/attempts/a013/outcome.json>) | H1 | Flash | disabled | v83 | failed / partial | 16 | 239594 | 0 | 94.031 | 合法 RHF 选择与旧默认 RKS 冲突，两次原生准入均在启动前拒绝。 |
| [a014](<E:/BG6022-V4/data/live-repair/attempts/a014/outcome.json>) | H1 | Flash | disabled | v83 | budget_exhausted / partial | 24 | 386369 | 0 | 180.640 | 当前 prepared_geometry 已满足却反复重建错误路线；文件生产者错误缺路径反馈，预算耗尽。 |
| [a015](<E:/BG6022-V4/data/live-repair/attempts/a015/outcome.json>) | T3 | Flash | disabled | v83 | budget_exhausted / partial | 24 | 374368 | 0 | 209.938 | 7 个 SDK 约束准入错误被上下文整体省略；随后 writer/文件引用纠正丢具体路径，预算耗尽。 |
| [a016](<E:/BG6022-V4/data/live-repair/attempts/a016/outcome.json>) | H1 | Flash | disabled | v84 | failed / partial | 16 | 238334 | 0 | 87.718 | 原生输入两次预启动失败；同失败输入重试被正确拒绝，但可纠正准入错误被作为致命停止。 |
| [a017](<E:/BG6022-V4/data/live-repair/attempts/a017/outcome.json>) | H1 | Flash | disabled | v85 | failed / partial | 24 | 409637 | 0 | 190.953 | 核性质块在坐标前；模型反复修改外层代码而未修原生输入，未启动 ORCA。 |
| [a018](<E:/BG6022-V4/data/live-repair/attempts/a018/outcome.json>) | T3 | Flash | disabled | v85 | budget_exhausted / partial | 24 | 339671 | 0 | 159.360 | 预检丢 allow_pending，把尚未执行的未来 code writer 当已落地文件解析，计划反复拒绝。 |
| [a019](<E:/BG6022-V4/data/live-repair/attempts/a019/outcome.json>) | T3 | Flash | disabled | v85 | failed / partial | 34 | 589770 | 0 | 275.906 | 4 次 Python 均为占位注释，无科学结果；人工 pause 后预提交检查停止。 |
| [a020](<E:/BG6022-V4/data/live-repair/attempts/a020/outcome.json>) | H1 | Pro | disabled | v85 | failed / partial | 25 | 359987 | 0 | 179.875 | 把执行输出集合当单文件端口；原 CONTROL 未给合法替代值，相同参数反复提交。 |
| [a021](<E:/BG6022-V4/data/live-repair/attempts/a021/outcome.json>) | H1 | Pro | disabled | v86 | paused / partial | 21 | 218206 | 0 | 242.985 | 已认证种子原句的摘要 quantity 非逐字标签，归一化被同一机械门反复拒绝。 |
| [a022](<E:/BG6022-V4/data/live-repair/attempts/a022/outcome.json>) | H1 | Pro | thinking_low | v87 | paused / partial | 21 | 363050 | 1 | 422.891 | 真实 ORCA 拒 GIAO 关键词；已读取的错误正文被后续 knowledge 查询挤出上下文，人工暂停。 |
| [a023](<E:/BG6022-V4/data/live-repair/attempts/a023/outcome.json>) | T5 | Pro | thinking_low | v87 | paused / partial | 8 | 165543 | 0 | 459.516 | 规范显示标签与能量覆盖耦合误拒；纠正后旧首次科学计划门又误拒观察分析路线，未启动。 |
| [a024](<E:/BG6022-V4/data/live-repair/attempts/a024/outcome.json>) | T5 | Pro | thinking_low | v88 | paused / partial | 7 | 94017 | 0 | 292.172 | 首提案漏 fixed_initial；补齐后计算句和后置 Eh 交付句的同一能量义务无法关联，未启动。 |
| [a025](<E:/BG6022-V4/data/live-repair/attempts/a025/outcome.json>) | H1 | Pro | thinking_low | v88 | paused / partial | 17 | 326508 | 1 | 611.219 | EPRNMR 语法失败后知识代码换行/窗口丢失；分析输入 stdout.out 又与运行器日志冲突。 |
| [a026](<E:/BG6022-V4/data/live-repair/attempts/a026/outcome.json>) | T5 | Pro | thinking_low | v89 | failed / partial | 12 | 254405 | 2 | 642.719 | 两次 ORCA 已完成；native 被误允许承接 named value，差值来源环境误拒，最终交付仍不全。 |
| [a027](<E:/BG6022-V4/data/live-repair/attempts/a027/outcome.json>) | T5 | Pro | thinking_low | v91 | failed / partial | 11 | 208431 | 2 | 620.547 | 两次 ORCA 与差值已完成；Delta/交付仍 gap、负号解释缺失，最后 HTTP 402 余额不足阻断模型。 |

26 次已结算候选小计：429 次模型调用，7,371,415 已知 tokens，21 次已知 ORCA，driver 9006.031 秒。此小计不含历史五批及 p001/p002 探针。

- a002：56,378 tokens 未知占用；已知值 0 不表示请求无成本。
- a004：原记录外另有两次启动区间各为 [0,1]，保留总计 2 次未知启动占用。
- a027：93,280 tokens 未知占用；最终 HTTP 402 余额不足中断模型，已有两次 ORCA 与差值原件保留，交付仍 partial。

累计正式账本（包含历史批次/探针）为 784 次调用、14,110,983 已知 tokens、36 次已知 ORCA；另占用 149,658 未知 tokens 与 2 次未知启动。不得把两组小计相加或把未知记为零。

完整字段、冻结源码身份、逐行原件 SHA256 与诊断引用见 [JSON](<E:/BG6022-V4/data/workflow-root-repair/live-attempt-matrix-a027.json>)，SHA256 `d41a4d319d20661908a70f923a2d73375ebf96caccd10af49dd555ef18943836`。
累计成本引用：[accounting-through-a027.json](<E:/BG6022-V4/data/live-repair/accounting-through-a027.json>)，SHA256 `8e039a0b5c42a386c20bbeeafb9d18bc48f9abb2c874867ed3e9fcd80200a952`。

本表仅汇总已存在的结算及诊断，不改变旧 Run、结果、报告、检查标准、科学资格或验收状态。
