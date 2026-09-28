# file-delivery 工程约定

本项目为独立的文件交付工具。确定性工作由代码完成，Hermes 通过稳定 CLI / Skill 执行日常任务；避免绑定模型或存储供应商。

- Codex 负责设计、任务拆分、合同、可信验收和独立审查；ZCode + GLM-5.3 负责业务实现和修复。用户于2026-09-28授权后续多次调用直至FD-004～006完整交付；新run不再设一次普通修复上限，使用明确delivery-policy和无进展停止条件。旧合同、已消耗调用和失败记录不回写。AgentRelay 框架修改由 Codex 负责。
- 执行前读取 contracts 中对应合同和 docs/plans/implementation.md。冻结范围、必须通过的验收和非阻塞偏好；不得因为风格或等价实现返工，不得追溯增加验收条件。
- 使用本机 AgentRelay 的合格通用入口。当前入口缺失时不得用演示 fixture 冒充业务执行，不得绕开分工由 Codex 静默完成业务代码。
- Worker 不修改合同、控制器验收证据或发布记录。保存候选 SHA、测试指纹、失败原因、调用用量、修复次数和 Codex 审查次数；未知用量或费用用 null。
- 每个独立验证的特性、修复或规划单元分别 commit、push。初始分支 main；尚无远端时记录 publication blocked，不创建猜测的远端仓库。
- 外部上传、邮件发送、真实存储费用和部署需对应的明确授权。用户已允许本任务使用 ZCode 当前 Coding Plan；须在实际派发前落盘调用次数和超时边界。
- 凭据、密码、签名链接、原始会话及私有运行状态不进入 Git。日志脱敏；结果必须区分上传、链接可用、渠道接受、实际接收及阅读。
- 以低成本、幂等、超时、有界重试、状态恢复、质量校验和结构化诊断支持 Hermes 独立维护；避免为未来扩展过度设计。


<!-- agent-relay:managed:start -->
## AgentRelay integration (rules v1)

Run the Coordinator project preflight before business execution. Framework defaults
may be migrated; project-specific requirements and explicit user resource limits
remain authoritative. Complete required repairs within existing authorization;
repair count is diagnostic, not a default one-repair delivery ceiling. Preferences
and equivalent implementations do not block acceptance. Diagnose stagnation before
asking the user; ask only for unresolved decisions or genuinely new authority.
Project migration never replaces frozen run policies or erases attempts and costs.
<!-- agent-relay:managed:end -->
