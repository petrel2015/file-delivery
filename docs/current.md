# 当前实施状态

日期：2026-09-27。

状态：继续派发前的控制器准备完成，等待 AgentRelay 主会话确认框架提交、测试及最终命令；尚未启动新的 Worker。

GitHub 私有仓库：<https://github.com/petrel2015/file-delivery>，默认分支 main。

## 已完成

- 本地 Git、GitHub 仓库、远端推送。
- FD-001 revision 2 合同及 10 个控制器验收测试，已在 Worker 调用前冻结。测试覆盖 manifest、路径边界、敏感路径、符号链接、IO 错误和输入未修改。另在 tests/relay_supplemental 补充安装、控制台入口、变更检测和 Python 网络操作检查；这些测试尚未取得业务候选通过结果。
- Codex 在 AgentRelay 实现通用业务派发入口及确定性验证；新增 11 个测试通过。另有凭据目录权限修复、超时用量完整性修复，各自提交和推送。
- 真实 ZCode / BigModel Coding Plan / GLM-5.3 high 调用一次，运行证据已保存。

## 当前未完成

FD-001 无业务实现，不能安装或使用 file-delivery CLI。加密、七牛、邮件和 Hermes Skill 均未实现。

首次控制器启动在凭据导入阶段失败，查明尚未启动 Worker；0755→0700 修复经过无模型调用的复现。原记录未覆盖，另存人工核对证据后才执行真实调用。

真实 Worker 在 180 秒上限超时，进程退出 -9，没有完整终态，也没有业务文件变更。保留候选 Git 快照，但其内容与业务基线相同，不是完成特性。没有执行普通修复或自动重试。没有进行业务代码审查或候选验收。

已记录两次完成的模型请求，共 27,460 tokens（输入 27,239、输出 221；缓存读取 18,368 已包含在输入中）。这是已观察小计，不能确定未完成请求的用量；总用量、货币成本和 Codex 用量未知。原框架 usage_complete 标记过宽，已修复并在业务证据中纠正。

## 证据及下一步边界

- [合同](../contracts/FD-001.json)
- [初始入口检查（历史状态）](evidence/relay-entry-audit.md)
- [首次调用结果与用量](evidence/fd001-first-invocation.json)
- [分阶段计划](plans/implementation.md)

私有运行记录保留在 /Users/aquarist/IdeaProjects/.agent-relay-runs/file-delivery-fd001 和 file-delivery-fd001-qualified；原状态、候选仓库、前置失败核对和用量纠正均保留。无原始私有会话或登录凭据进入 Git。

未知结果阻止自动重试。下一次业务调用前需要显式决定如何处理本次中断及后续预算，不能伪装成新的 run 绕过次数或超时限制。没有证据证明本次失败是限流、供应商故障或工具权限问题。

## 受控继续准备

保留 FD-001 revision 2 和原 tests/acceptance/test_plan_acceptance.py 原字节与指纹。新增 [验收映射](../contracts/FD-001.verification.json) 把全部六个 AC 绑定到两个可信目录中 discovery 可发现的测试；不新增业务要求。

- 安装验证：在临时 tracked 文件副本中离线构建 wheel，全新虚拟环境本地安装，不下载依赖；从仓库外、清除 PYTHONPATH 后执行控制台入口与帮助。缺少控制器构建工具标记 HARNESS_BUILD_TOOLS_MISSING；不因此要求 Worker 更换等价构建后端。
- INPUT_CHANGED：首次非空读取后同步注入增长或等长且 mtime 变化；支持 open、pathlib、readinto/file_digest、descriptor 和 FileIO。尚未覆盖的读取方式或未成功注入分别标记 HARNESS_READ_NOT_OBSERVED / HARNESS_MUTATION_NOT_INJECTED，应先修验证器，不消耗业务自动修复调用。保留不变文件对照。
- 无网络验证：Python audit hook 捕获/阻止 DNS、连接和发送等网络操作，即便业务吞掉异常也失败。它不是 OS 沙箱，不能据此证明所有外部进程或原生调用均无网络；仍需代码审查。
- .DS_Store 已加入忽略规则，原文件未删除。

原 Worker 主进程已由旧控制器 wait 回收，返回 -9。当前只读扫描未发现指向原候选目录或运行参数的相关本地进程，候选 tree 与基线仍相同。原 PID/PGID 未保存，不能穷尽证明所有脱离进程组的子进程；也不能由本地停止推断供应商费用完整。

拟在原 run 上追加最多 2 次 Worker 提交（1 次继续及最多 1 次控制器自动修复），每次 600 秒，原未知用量和费用继续保留。此项仅为准备记录：等待主会话最终命令和预算确认，不修改旧合同预算、不创建替代 run 绕过原状态。

证据：[准备核对](evidence/fd001-resume-preparation.json)、[补充测试的未实现基线](evidence/fd001-supplemental-baseline.json)。业务缺失导致的失败是预期基线，不是验收通过。
