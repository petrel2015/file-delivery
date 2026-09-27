# 当前实施状态

日期：2026-09-27。

FD-001 已完成：ZCode + GLM-5.3 实现，Codex 完成两轮独立审查，一次普通修复。已发布只读文件清单 CLI；完整文件交付产品仍在开发。

GitHub 私有仓库：<https://github.com/petrel2015/file-delivery>，分支 main。业务提交 `73ae140`，验收候选 `47a285fc31581c9f6488870c626397a75ebb6ce3`。8 个业务文件逐字节集成，主分支 33 个测试通过（18 个控制器检查、15 个业务单测），零失败、错误或跳过。干净环境离线安装和仓库外控制台调用均通过。

## 当前可用

需要 Python 3.11+，在项目目录安装后调用：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/file-delivery plan /绝对路径/待发送目录 --root /绝对路径/待发送目录 --json
```

安装可能获取构建工具；运行时只依赖标准库。plan 只读校验文件，返回排序去重后的相对路径、字节数和 SHA-256。拒绝越界路径、符号链接、敏感路径和特殊文件，观察到哈希读取期间的元数据变化时报错。不是对抗性文件系统快照保证。

不应以包含 `.git` 的整个项目目录作为待发送目录。JSON `planned` 只表示清单生成成功，不代表已加密、上传、发送、接收或阅读。

## 验收和成本证据

- [冻结合同](../contracts/FD-001.json)、[验收映射](../contracts/FD-001.verification.json)
- [最终候选、测试指纹、两轮审查和逐请求用量](evidence/fd001-accepted.json)
- [首次失败](evidence/fd001-first-invocation.json)、[历史继续调用失败](evidence/fd001-continuation-outcome.json)

| 调用 | 结果 | 时长 | 已观察 tokens |
| --- | --- | --- | --- |
| 1 | high，超时，无业务实现 | 180.139 秒 | 27,460 |
| 2 | high，超时，无业务实现 | 600.360 秒 | 47,196 |
| 3 | low，候选完成，独立审查要求具体修复 | 116.710 秒 | 217,584 |
| 4 | low，一次修复后验收通过 | 70.101 秒 | 112,782 |
| 累计 | 历史未知用量仍保留 | — | 405,022 |

第 1、2 次用量不完整；总用量、货币成本和 Codex 用量未知，均不按零计算。第 3、4 次在实际出站请求上验证 low 和每请求 max_tokens=8192；不是总 token 或货币上限，不据此声称省钱。未调用付费探针，参数支持由本机 CLI 的隔离 loopback 请求验证。

框架原始 dispatcher 状态仍为 pending_manual_review，其计数仅记录触发修复的一次审查；控制器最终审查单独保存在私有 review-round-2.json，发布证据汇总实际两轮结果，不伪改原 ledger。原始状态、失败日志及私有会话留在忽略的运行目录，不进入 Git。

## 下一阶段

按[实施计划](plans/implementation.md)，继续 FD-002 本地 AES ZIP 打包/解密验证，再做 FD-003 SQLite 账本、幂等恢复与模拟存储。每个新合同先冻结范围和可信验收，各最多两次 Worker 提交、每次 600 秒；成本按项目累计，保留 FD-001 历史失败。

七牛真实上传、SMTP 发送和部署尚未执行；FD-004/005 服务配置与授权及 FD-006 完整 Hermes 交付另行完成。

## FD-002 当前验收阻塞

两个真实Worker调用已结束，观察用量491,682 tokens，费用未知。固定候选 `86447f86ccd12a610442d275767d5d05408ff8ba` 的24项可信检查通过，含独立7-Zip解密，但64项全量测试仍1失败1错误；独立审查发现损坏压缩数据及非UTF8清单的异常未转为JSON错误，另有Worker测试字段断言错误。未集成、未发布FD-002。

修复后控制器因忽略的Python缓存误判越界；原始worker_scope_violation保留，另存缓存哈希与人工范围核对，不改写成自动通过。两次调用和两轮审查额度已用尽，不自动增加调用。FD-003草稿已准备但依赖FD-002验收，尚未执行。

[详细失败证据](evidence/fd002-review-blocked.json)、[项目累计成本](evidence/project-costs.json)。累计已观察896,704 tokens，FD-001历史中断仍使总用量不完整；总费用及Codex用量未知。

## FD-002 已接受发布（后续裁决）

上述原失败保留。显式追加一次180秒补充修复后，候选 `1ee52bdfeed4cc68c6cfa27c736f28414a5be907` 通过27项可信检查及40项业务单测；集成主分支67项测试零失败/错误/跳过。业务提交 `b8452b3`。原2次加补充1次，共3次Worker提交、3轮Codex审查；本功能观察571,615 tokens，项目累计976,637，费用未知。

新增可用命令（先安装 `pip install '.[archive]'`）：

```sh
.venv/bin/file-delivery pack /绝对路径/输入目录 --root /绝对路径/输入目录 --output-dir /绝对路径/新加密包目录 --json
.venv/bin/file-delivery verify /绝对路径/新加密包目录 --json
```

密码位于输出目录password.txt，目录0700，三个文件0600；JSON只返回位置。AES256保护内容，文件名仍可见；同用户/管理员仍可读取密码文件，未使用系统钥匙串。单个大文件当前读入内存，尚未做流式优化或大规模性能验证。

[最终验收和追加修复关联](evidence/fd002-accepted.json)。FD-003现在可以在已验证归档模块上继续。
