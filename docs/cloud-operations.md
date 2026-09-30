# 云端文件查询、下载与链接版本

所有命令由同一 Python 核心实现，CLI 与 MCP 不依赖模型或宿主。

```sh
file-delivery list --config /private/qiniu.json --state-dir /private/state --query 报告 --limit 100 --json
file-delivery download --config /private/qiniu.json --state-dir /private/state --key delivery-001 --output-path /private/downloads/报告.zip --json
file-delivery renew-link --config /private/qiniu.json --state-dir /private/state --delivery-key delivery-001 --key link-002 --ttl-seconds 86400 --json
file-delivery status-link --state-dir /private/state --delivery-key delivery-001 --key link-002 --json
file-delivery send-email --state-dir /private/state --delivery-key delivery-001 --link-key link-002 --smtp-config /private/smtp.json --to reader@example.com --key mail-002 --json
```

`list` 查询真实七牛 bucket，并关联指定私有状态目录中已有任务与原始文件名。`--prefix` 匹配对象键前缀，`--query` 对当前页的对象键和原始文件名做不区分大小写的包含匹配。`next_marker` 非空时，用 `--marker` 请求下一页；不能把当前页结果当作全桶搜索结论。名称相同或多版本时，由用户确认目标。没有本地账本记录的对象标为 unmanaged，不推测密码或原始文件名；本版本仅下载和续签已有 owned 交付对象。

`download` 不依赖原始输入路径，也不依赖旧链接仍有效。它签一个短期请求链接，流式下载、核对 SHA256/大小，再原子发布 0600 的 AES ZIP；目的目录须已存在，无隐式覆盖。临时文件在失败后清理；不会自动解压。CLI 仅返回密码文件路径，Skill 可在用户要求的私人交付回复中展示密码。

`renew-link` 使用原对象和加密包，验证密码/归档及新链接读回，绝不重新上传或打包。新链接版本保存在独立 `links.sqlite3` 与受保护的 `remote-links/<task_id>/<link_key>.json`，旧 handoff/邮件通知保留。版本键绑定交付、目的地、归档摘要及 TTL；重复调用复用原到期时间，不自动续期。新截止时间不能超过记录的对象保留期限；到期后的同键查询仍显示过期，新的分享请求可创建新版本，但不能用新键规避 SMTP_UNKNOWN。

发送已有云端文件时，先查询/选定 `delivery_key`，需要新链接则生成版本，再传 `send-email --link-key`。默认不带版本参数时保持原通知行为。每个新邮件意图有稳定通知键；已接受或不确定的邮件不重发。SMTP 接受不表示收件人已经收到或阅读。

## 宿主接入

Claude Code 的项目 Skill 放在 `.claude/skills/file-delivery`，MCP 可通过 `claude mcp add --transport stdio --scope project file-delivery -- /absolute/project/.venv/bin/file-delivery-mcp` 注册；Claude Desktop 使用单独配置，示例见 [claude-mcp.json](examples/claude-mcp.json)。配置不包含密钥；本机没有发现 Claude 可执行文件或应用，当前仅验证 MCP 客户端协议，不能声称 Claude 接入成功。参考 [Claude 官方 MCP 文档](https://code.claude.com/docs/en/mcp)及 [Skills 文档](https://code.claude.com/docs/en/skills)。

真实邮件仍需要本机 SMTP 配置路径和用户指定测试收件邮箱。无需把密码发到聊天中。
