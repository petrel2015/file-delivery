# 七牛远程交付 CLI

FD-004B 已通过离线合同验收，业务提交2545baf；真实七牛账户、下载域名和服务联调尚未验证。先阅读[适配器配置](qiniu-adapter.md)，安装 `pip install '.[archive,qiniu]'`。

```sh
file-delivery deliver-qiniu /绝对路径/输入目录 --root /绝对路径/输入目录 --state-dir /绝对路径/私有状态 --config /绝对路径/qiniu.json --key delivery-001 --json
file-delivery status-qiniu --state-dir /绝对路径/私有状态 --key delivery-001 --json
```

配置文件必须在输入根目录外，权限0600；状态目录在输入根目录外且不能包含输入根目录，新建0700。路径及受保护文件不可使用符号链接。

默认签名链接有效期7天，对象保留30天；可用 `--ttl-seconds`（1～604800）和 `--retention-days`（1～3650）设置，链接有效期不可超过保留期。状态保存在独立remote.sqlite3，已有本地交付账本保持可用。

返回JSON包含任务、对象标识、文件大小、到期时间以及password_file和handoff_file路径。密码仅在私有bundle/password.txt，签名URL仅在私有handoff JSON；普通JSON及账本不含这两个秘密。需要向指定收件人提供链接与密码时，只在已授权的交付流程中读取这些文件。

同key同输入、存储身份及策略复用原加密包、密码和对象；任何内容、存储账户/空间/域名或策略变化返回IDEMPOTENCY_CONFLICT，需明确使用新key。secret_key轮换不改变身份。每次成功都重新核对桶的私有属性和远端下载SHA256；link-verified仅代表本次程序下载验证，不代表收件人收到或阅读。

REMOTE_UNKNOWN表示远端结果不确定，本次不会立即重传。下一次显式执行同key请求时先查询远端，再核对已有对象。包损坏、密码丢失或handoff不一致会停止，保留原文件，不重新生成秘密。进程在打包、上传、保存链接后退出的恢复均经过真实子进程验证。

LINK_EXPIRED不会自动续链或重新上传；状态查询不需要原输入或配置，也不联网。撤销与到期清理正在FD-004C独立实现。SMTP和Hermes集成仍未完成。

[验收与失败记录](evidence/fd004b-accepted.json)保留四次Worker候选和三轮审查，费用未知。业务代码与接受候选逐文件一致，另有新wheel安装后合成存储闭环验证；这些证据不替代真实服务联调。
