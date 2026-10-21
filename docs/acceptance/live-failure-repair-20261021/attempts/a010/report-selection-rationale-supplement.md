# a010 方法与基组选择理由补充

旧 a010 报告遗漏了 Request 已记录的方法和基组选择理由。本补充由新报告代码只读呈现原记录；没有重新调用模型或 ORCA，没有修改原报告、Request、Run、科学检查或历史状态。选择理由来自原模型决策，不因此证明方法适用性；原记录未引用文献。

Request：`request_defc5b57e80041aa92561bbd7b9b9f79` v3；规范 JSON SHA256：`9c0c2d2b73d03edf341ee03546d31a7c8b224a681bce4363ed660af05104cd80`。

原选择决策：`model_f70e07ac7728464a9c92ac1dec699817`；原件 SHA256：`40f593159eab038f521b8d655b417cd81ddd71d8875dd1e190db00d18f067c29`。

原模型响应 SHA256：`2a813a388d8cee93fe073f45948fbf82ea7c6c4fc35c69957f6713f4a1635e8c`。

本次 report.py SHA256：`28a1f3aec8a6aaa223ab3ff29b5e5b62d43e1e3675d62052079274901cde0aea`。

条件选择理由（Request v3 原记录）：
- `request.method` = r2SCAN-3c；来源 auto_selected；理由：Composite meta-GGA method with built-in basis, efficient for main-group geometry optimization and harmonic frequency calculations within the 4-core/192 MB per-process budget; suitable for neutral singlet ethanol in gas phase. Selection is model-based, not literature-validated.
选择记录及来源：{'value': 'r2SCAN-3c', 'source': 'auto_selected', 'request_version': 2, 'reason': 'Composite meta-GGA method with built-in basis, efficient for main-group geometry optimization and harmonic frequency calculations within the 4-core/192 MB per-process budget; suitable for neutral singlet ethanol in gas phase. Selection is model-based, not literature-validated.', 'sources': [], 'schema': 'condition-selection-3', 'basis_kind': 'model_reason', 'scientific_validation': 'not_established_by_selection', 'user_request_authorization': {'message_id': 'message_eb0ceade81e647f2880430c4a6270718', 'text_basis': '请为乙醇（中性、单重态、气相）生成结构，在当前资源和预算内自行选择合适的方法和基组并说明依据，完成几何优化和该优化结构上的振动频率计算。', 'start': 0, 'end': 68, 'schema': 'request-semantics-3', 'source': 'explicit'}}
- `request.basis` = built-in；来源 auto_selected；理由：r2SCAN-3c is a self-contained composite method whose basis is included; using the built-in basis avoids a separate orbital-basis choice and is consistent with the managed native-input contract.
选择记录及来源：{'value': 'built-in', 'source': 'auto_selected', 'request_version': 2, 'reason': 'r2SCAN-3c is a self-contained composite method whose basis is included; using the built-in basis avoids a separate orbital-basis choice and is consistent with the managed native-input contract.', 'sources': [], 'schema': 'condition-selection-3', 'basis_kind': 'model_reason', 'scientific_validation': 'not_established_by_selection', 'user_request_authorization': {'message_id': 'message_eb0ceade81e647f2880430c4a6270718', 'text_basis': '请为乙醇（中性、单重态、气相）生成结构，在当前资源和预算内自行选择合适的方法和基组并说明依据，完成几何优化和该优化结构上的振动频率计算。', 'start': 0, 'end': 68, 'schema': 'request-semantics-3', 'source': 'explicit'}}

核对：317 个原文件的 SHA256 均保持不变；科学、目标与交付状态和原报告一致。
