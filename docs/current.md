# 当前实施状态

日期：2026-09-28。

本地 FD-001～FD-003 及 FD-004A 七牛适配器已实现并发布；这不是完整远程文件交付产品。Codex负责合同、可信验收和独立审查，业务代码由ZCode + GLM-5.3实现及修复。

公开仓库：<https://github.com/petrel2015/file-delivery>，分支main。

| 特性 | 可用命令 | 业务提交 |
| --- | --- | --- |
| FD-001 只读清单、路径校验、SHA-256 | plan | 73ae140 |
| FD-002 本地AES256 ZIP、解密逐文件校验 | pack / verify | b8452b3 |
| FD-003 SQLite账本、幂等、本地模拟存储及中断恢复 | deliver-local / status | f15a3f9 |
| FD-004A 七牛私有对象适配器 | Python QiniuStore API，尚无远程CLI | 4e54532 |

FD-001～003 当时集成110项测试通过，零失败、错误或跳过：34项控制器检查、76项业务单测。包括独立7-Zip解密、全新环境基础安装、Unicode、多文件、敏感路径、错误密码、损坏归档、并发首次请求、两个进程中断点、原包/密码保留、对象缺失恢复和损坏拒绝。实际安装后的CLI也从仓库外运行通过，重复请求复用同一任务、密码和归档。

FD-004A 当前候选另通过48项可信检查、94项业务单测及99个控制字符独立检查；隔离wheel安装也已验证。真实七牛账户尚未联调。API配置和边界见[七牛适配器说明](qiniu-adapter.md)。

## 本机直接使用

项目 `.venv` 已安装当前版本及archive extra。其他环境需Python3.11+：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[archive]'
```

基础plan可仅安装 `pip install .`，运行时不依赖第三方库。加密依赖作为可选extra，安装可能下载构建工具和依赖；业务命令不联网。

```sh
# 只读生成清单
.venv/bin/file-delivery plan /绝对路径/输入目录 --root /绝对路径/输入目录 --json

# 创建独立新加密包目录；不可覆盖或位于输入root内
.venv/bin/file-delivery pack /绝对路径/输入目录 --root /绝对路径/输入目录 --output-dir /绝对路径/新加密包 --json
.venv/bin/file-delivery verify /绝对路径/新加密包 --json

# 带账本的本地交付；state和objects必须彼此分离且在输入root外
.venv/bin/file-delivery deliver-local /绝对路径/输入目录 --root /绝对路径/输入目录 --state-dir /绝对路径/私有状态目录 --store-dir /绝对路径/本地对象目录 --key delivery-001 --json
.venv/bin/file-delivery status --state-dir /绝对路径/私有状态目录 --key delivery-001 --json
```

路径应为真实目录，不含符号链接。不要以含 `.git` 的整个项目目录作为待发送root。新state/store目录0700，已有目录不可允许组或其他用户访问。

`packaged`表示本地包已验证；`stored-local`表示本地对象已哈希读回，不代表上传、链接可用、渠道接受、实际接收或阅读。同key同内容/存储位置复用；内容改变返回IDEMPOTENCY_CONFLICT，应显式使用新key建立新交付。

密码仅在私有bundle/password.txt中，JSON给出其位置。目录0700，文件0600；这是本机权限保护，未使用钥匙串或静态加密，同用户/管理员仍能读取。AES保护文件内容，文件名可见。已有包验证失败不会被删除或重建密码；应修复访问条件并保留原状态。已记录摘要的包/密码丢失会报错，不自动再加密。

重试deliver-local要求原输入仍可读取且未变化；status无需源文件。当前针对本机可信单用户及POSIX锁，尚未验证Windows或分布式运行。单个大文件加密阶段读入内存，未进行大规模性能或长期运行验证。

## 证据及成本

- [FD-001验收](evidence/fd001-accepted.json)
- [FD-002验收及显式追加修复](evidence/fd002-accepted.json)
- [FD-003验收、安装烟测及显式追加修复](evidence/fd003-accepted.json)
- [FD-004A适配器及追加修复验收](evidence/fd004a-accepted.json)
- [项目累计调用/用量/审查](evidence/project-costs.json)

累计13次真实Worker提交、7次修复（4次普通修复、3次显式追加）、11轮Codex审查。共观察2,770,351 tokens；FD-001历史两次超时使总用量仍不完整，货币成本和Codex用量均未知/null，不能据运行时间或low配置宣称降本。

原始失败未覆盖：[FD-001首次](evidence/fd001-first-invocation.json)、[继续超时](evidence/fd001-continuation-outcome.json)、[FD-002审查失败](evidence/fd002-review-blocked.json)、[FD-003审查失败](evidence/fd003-review-blocked.json)。两个修复候选曾因忽略的Python缓存被控制器范围检查拒绝；保留原状态、精确缓存哈希和人工核对记录后验证，没有为缓存问题追加模型调用。额外业务修复各有明确的关联合同、独立上限和成本，不作为新功能清零。

## 后续接入准备

FD-004A 已经独立审查通过并发布，追加一次调用由用户于2026-09-28明确授权。原失败及缓存恢复证据保持不变。下一步为远程账本/CLI和真实联调，详见[FD-004计划](plans/FD-004.md)。

## 未实施

FD-004远程账本/CLI、完整撤销/清理流程及真实七牛验收；FD-005联系人/SMTP；FD-006完整Hermes Skill/MCP。真实存储、邮件和部署仍需对应配置及授权，本轮未执行。后续范围见[分阶段计划](plans/implementation.md)。
