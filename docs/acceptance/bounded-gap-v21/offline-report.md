# v21 离线验证记录

候选 `cd2196bf32d4c2b66c596ced95b5fab56763e7f7`，树 `dfd7b91bf1e1f8be87dd82750af824fb10101e61`，prompt `agent-json-v21`。验证结论：**passed**；用户验收仍待确认。

默认全量在独立干净 Windows checkout 执行一次：**2485 通过、193 跳过、0 失败、0 错误**；Ruff 退出码 0。未设置 maxfail、重跑或用例过滤。38 个生产模块的实际导入路径均在该 checkout 的 `src` 内；开始/结束均无 `data`、Git 无改动，生产文件与依赖锁逐字节不变。Python 与已安装依赖复用工作区 `.venv`。

跳过项按实际 JUnit 理由归类，完整原文在 JSON 中：

- `real_model_disabled`: 130
- `real_orca_disabled`: 13
- `missing_local_archives`: 49
- `windows_symlink_privilege`: 1

先前定向验证分别为 r4 helper/预算守卫 263 通过、终止说明与上下文回归 223 通过、独立审查测试 47 通过。这些组彼此及与全量重叠，不相加。原 r3 审批记录的 233 项仅属于历史证据。

开发期间曾出现 4 项比较测试未固定时钟，以及 1 项新测试误用不存在的 `final_only` 参数；修改测试构造后，最终相关 223 项全部通过。它们属于离线测试构造失败，不是新增真实调用失败。

旧 r3 实际已发送的 V06 第二请求 token 上界是 11983；v21 对该请求的派生**离线、未发送**回放为 11988/12000，单条增加 5、余量 12。另一个 final-slot 用例增加 107，不能把单条 +5 泛化到全部上下文。新提示对真实模型是否有效仍未验证。

本次新增真实模型 HTTP、tokens、费用、PubChem、OPI 准备和 ORCA 启动均为零。r3 的 N06 通过与 V06 事实/语义失败未改判；其 153 个归档 hash 引用（118 个不同路径，含 68 个 r1/r2 历史路径）已重新核对。累计账本仍为 `b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13`：324 HTTP、1018912 tokens、0.4065609 美元，ORCA reference/development/formal 为 16/26/0。

r4 仍未批准：pin `None`，审批、候选、预算迁移及执行目录均不存在。r4 提议把累计上限增至 1124 HTTP、6906912 tokens，保持 10 美元与 ORCA 17/54/48/119 上限；此次增补尚未应用。

完整结构化证据见 [offline-validation.json](offline-validation.json)。原始全量日志在 `C:\WINDOWS＼TEMP\orca-v21-validation-setup-j7i64o8k`，本记录绑定其 SHA256；不将本地大日志复制进仓库。此次离线修复等待用户验收，真实批次六尚未完成。
