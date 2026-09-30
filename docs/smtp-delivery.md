# 联系人与 SMTP 投递

Python CLI 和 notification.send 共用独立私有通知账本。只发送一个已验证、未过期且未撤销的远程任务。

Skill 面向完整请求：本地文件可以“保存”“分享”或“发邮件”；已知任务键且链接有效的已有云端文件可以复用，不需要重新上传。邮件为 `multipart/alternative`，提供手机适配 HTML 与纯文本后备；包含下载按钮、可复制 URL、解压密码、包大小和 UTC 到期时间，无脚本、远程图片或跟踪。密码用于打开 AES ZIP，目前没有网页提取码入口。

Skill 和 CLI/MCP 不依赖 ChatGPT/Codex、Hermes、Claude 或模型供应商。宿主只负责发现 Skill、运行工具和读取已授权的私有交付文件。云端 list、本地下载和已有对象链接更新尚未实现，新增范围见 [FD-008](../contracts/FD-008.json)，不能用离线状态查询冒充云端搜索。

SMTP/联系人配置示例与权限要求见[宿主接入说明](../.agents/skills/file-delivery/references/host-integration.md)。状态目录0700，配置、密码、账本0600或更严格；拒绝符号链接及非当前用户所有的私有文件。SMTP必须验证 TLS/主机名：implicit 或 mandatory starttls，无明文回退。

```sh
file-delivery send-email --state-dir /absolute/private/state --delivery-key remote-001 --smtp-config /absolute/private/smtp.json --to reader@example.com --key mail-001 --json
file-delivery status-email --state-dir /absolute/private/state --key mail-001 --json
```

别名使用 `--to 同事 --contacts /absolute/private/contacts.json`。不支持显示名、地址列表或多个收件人。

通知指纹绑定远程交付、收件人和发送身份；稳定Message-ID和通知ID在连接前持久化。EHLO/TLS/login/MAIL/RCPT只准备，不发送DATA。DATA前提交sending；DATA及后续持久化错误均视为SMTP_UNKNOWN，避免可能已投递的邮件被再次发送。相同键已接受返回reused；到期后重复查询也不重发。原键请求变化返回IDEMPOTENCY_CONFLICT。

| 状态 | 含义和操作 |
| --- | --- |
| prepared | 已存通知意图，尚未提交发送 |
| failed-before-send | 已确认发送前失败，修复原因后可显式重试原键 |
| sending / unknown | 可能已发送；恢复为unknown，SMTP_UNKNOWN停止重发 |
| channel-accepted | SMTP DATA获得最终2xx；不证明收件人已收到或阅读 |

`status-email`离线读取，不需原配置或输入。账本损坏返回STATE_INVALID并保留文件。不要删除通知记录、换键或重新生成归档来规避unknown。消息只含交付链接、包密码、大小及到期时间；普通结果只给路径/状态，不打印邮件正文或凭据。链接/归档与账本摘要必须一致后才连接SMTP。

离线验收包括真实进程中断、锁超时、TLS协议替身、DATA边界、数据库提交失败和私有路径破坏。真实邮件服务尚需本地配置和明确测试收件地址；离线测试不等于渠道接受。
