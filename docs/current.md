# 当前实施状态

2026-09-30。用户已改为 Codex 独立完成，不再调用 AgentRelay/ZCode；历史候选、失败和用量保留。

公开仓库：<https://github.com/petrel2015/file-delivery>，分支 main。FD-001～FD-006 的代码入口已实现；真实七牛上传/下载及短期链接过期已通过；撤销/清理、SMTP服务和 Hermes 模型驱动验收仍未完成。

| 特性 | 可用入口 | 业务提交 |
| --- | --- | --- |
| FD-001 只读清单、路径校验、SHA-256 | plan | 73ae140 |
| FD-002 AES256 ZIP、解密逐文件校验 | pack / verify | b8452b3 |
| FD-003 SQLite、本地幂等与中断恢复 | deliver-local / status | f15a3f9 |
| FD-004A 七牛私有对象适配器 | QiniuStore API | 4e54532 |
| FD-004B 远程账本、链接验证与恢复 | deliver-qiniu / status-qiniu | 2545baf |
| FD-004C 撤销、保留期清理 | revoke-qiniu / cleanup-qiniu | 0fb7169 |
| FD-005 联系人、TLS SMTP、不确定不重发 | send-email / status-email | bb5e568 |
| FD-006 Hermes项目Skill、11个MCP工具 | file-delivery-mcp | 7521697 |

## 本机使用

项目 `.venv` 已安装 archive、qiniu、mcp extra。新环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[archive,qiniu,mcp]'
.venv/bin/file-delivery --help
```

基础 `pip install .` 可离线使用 plan，无第三方运行依赖。MCP可选SDK固定2.2.0，stdio入口独立；不需要升级 Hermes 自身SDK。

```sh
file-delivery plan /absolute/input --root /absolute/input --json
file-delivery pack /absolute/input --root /absolute/input --output-dir /absolute/new-bundle --json
file-delivery verify /absolute/new-bundle --json
file-delivery deliver-local /absolute/input --root /absolute/input --state-dir /absolute/private/state --store-dir /absolute/private/objects --key delivery-001 --json
file-delivery status --state-dir /absolute/private/state --key delivery-001 --json
```

输入不能包含符号链接、敏感路径或 `.git`。state/store/bundle/config在输入root外；目录0700，私有文件0600。配置与密码不进Git。包验证失败保留原包和密码；同key请求变化返回IDEMPOTENCY_CONFLICT。

远程、邮件和宿主接入分别见[七牛适配器](qiniu-adapter.md)、[远程交付](qiniu-delivery.md)、[SMTP投递](smtp-delivery.md)、[Hermes Skill](../.agents/skills/file-delivery/SKILL.md)及[宿主安装/配置](../.agents/skills/file-delivery/references/host-integration.md)。Hermes已信任本项目并配置 file-delivery MCP；已通过真实Skill读取、连接及本地工具闭环。需从项目内启动新Hermes会话发现Skill。

`packaged`、`stored-local`、`link-verified`、`channel-accepted`分别表示已验证本地包、本地副本、签名下载读回、SMTP接受；不能据此声称收件人收到或阅读。删除对象和旧链接可用状态分别报告，不能撤回已下载文件。

默认私人交付回复按已确认需求展示链接、密码、大小、到期时间，Skill读取已有受保护 handoff/password 文件。该私人会话是有意保留的交付记录；普通CLI/MCP结果与诊断仍仅给路径及脱敏元数据。没有宿主文件读取能力时明确返回路径和缺口。

## 验证与证据

FD-005：157项业务测试、97项全量控制器/直接回归验收通过（其中26项SMTP固定验收和8项新增安全/恢复回归）。新回归覆盖私有路径、摘要替换、损坏通知记录、DATA后数据库提交失败不重发。

FD-006：7项控制器验收通过；从仓库外安装wheel，以官方Client实际执行 modern和legacy stdio、Unicode计划/加密/校验、本地幂等及错误恢复。真实Hermes自身SDK2.0.0连接成功；实际加载Skill，调用plan/deliver_local/status_local并验证摘要、复用及TASK_NOT_FOUND诊断。11个服务器业务工具；Hermes另注册4个协议辅助工具。无模型或真实服务调用。

- 历史验收：[FD-001](evidence/fd001-accepted.json)、[FD-002](evidence/fd002-accepted.json)、[FD-003](evidence/fd003-accepted.json)、[FD-004A](evidence/fd004a-accepted.json)、[FD-004B](evidence/fd004b-accepted.json)、[FD-004C](evidence/fd004c-accepted.json)。
- 当前验收：[FD-005](evidence/fd005-accepted.json)及[诊断修复](evidence/fd005-diagnostic-fix.json)、[FD-006](evidence/fd006-accepted.json)、[累计调用/用量](evidence/project-costs.json)。

累计24次历史Worker提交，15次修复，19轮独立Worker候选审查；直接完成阶段另记自审，不冒充独立审查。已观察9,490,409 provider tokens；FD-001历史两次超时导致总体用量不完整。货币及Codex用量仍unknown/null。

## 未完成的外部验收与限制

真实七牛：首次AK/SK相同导致401，[失败记录](evidence/qiniu-live-001.json)保留；修正后已上传1KiB测试文件并stat确认ZIP243字节，[上传及DNS诊断](evidence/qiniu-live-002.json)保留。DNS生效后恢复原任务，HTTPS匿名下载403、签名及原handoff下载200，下载ZIP的SHA256和AES解密/逐文件清单校验均通过；重复请求复用原任务、归档、密码，本次无再次上传。另对同一对象的5秒短期链接验证到期前200、到期后403，未延长原handoff期限。[真实下载/过期验收](evidence/qiniu-live-003.json)。当前主任务link-verified；主链接有效期1小时，测试对象保留策略1天。真实撤销、清理和删除后旧链接行为仍未测试；不声称精确时刻自动删除或大陆性能验收。SMTP：等待本地配置路径和测试收件邮箱，尚无真实渠道接受证据。Hermes模型驱动自主交付/诊断：待单次推理通道授权，不由工具闭环替代。

仅验证本机POSIX单用户场景；尚未验证Windows、分布式运行、大文件性能或长期运行。包和七牛表单上传会把单文件读入内存。AES保护内容，ZIP文件名可见；本机私有权限不等于静态加密/钥匙串保护。
