# AgentRelay 入口只读检查

日期：2026-09-26。

- 框架：/Users/aquarist/IdeaProjects/agent-relay
- 检查 HEAD：cde1b9da4cf722152615d15e4e124e0065cf2c36；检查时工作树干净。
- 业务目录初始为空，无 AGENTS.md、Git 或远端；遵守用户消息中的工程原则，并在本项目 AGENTS.md 固化本次协作规则。

已读取 README_FOR_AI.md、AGENTS.md、docs/capabilities/current.md、docs/en/usage.md、docs/plan.md、docs/changes/C-0020-coding-plan-trial.md、docs/contract-validation.md、docs/integrations/zcode.md；核对 contracts.py、cli.py 和 coding_plan_trial.py。

## 结论与证据

1. cli.py 提供 validate/create-run/status/recover/probe-codex/probe-zcode/demo，没有通用 dispatch。create-run 仅保存合同，不启动 Worker。
2. coding_plan_trial.py 的 run 使用 disposable_trial()，从固定 trial.prompt 构造提示，修复分支直接写 src/leases.py，并绑定 missing_key_none_owner 诊断；不能接收本项目的业务合同和工作目录。
3. current.md 与 C-0020 明确只有一次真实 GLM-5.3/high 有界 fixture 证据，通用派发及验收持久化未完成。历史 integrations/zcode.md 仍标记未验证，应以较新的 C-0020 理解 fixture 进展，不能反推通用入口已完成。
4. 无 Worker 调用、无 Coding Plan 消耗测量、无业务候选提交、无业务测试及代码审查。合同校验和 run 建档不算 Worker 实施。

## 状态与接入缺口

business_execution = blocked_missing_general_dispatch。

缺少：业务 workspace/合同参数化、候选提交持久保留、跨调用一次修复限制、通用可信测试接入、业务运行与审查/用量证据关联。现有模块可复用，但尚无已验证的组合入口。

publication = blocked_missing_remote。当前未指定 file-delivery 远端；不得借用 agent-relay 的 origin 发布业务文件。
