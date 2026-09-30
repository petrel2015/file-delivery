# 七牛适配器（FD-004A）

当前提供 Python `QiniuStore` API，已通过离线协议测试和独立审查。远程交付 CLI、账本、撤销和清理现已实现；真实七牛账户验收仍未完成。完整进展见[当前状态](current.md)，调用和失败证据见[FD-004A验收](evidence/fd004a-accepted.json)。

## 安装和配置

Python 3.11+，从项目目录安装可选依赖：

```sh
python -m pip install '.[archive,qiniu]'
```

配置应放在待交付输入目录之外，使用当前用户专属的普通文件，权限0600或更严格，路径不得经过符号链接。示例字段如下；凭据占位符应在本机替换，不提交 Git：

```json
{
  "access_key": "replace-locally",
  "secret_key": "replace-locally",
  "bucket": "your-private-bucket",
  "region": "z0",
  "download_domain": "https://files.example.com",
  "timeout_seconds": 30
}
```

支持区域 z0、z1、z2、na0、as0、cn-east-2。下载域名必须为 HTTPS origin，可有尾斜线或合法端口；不能包含路径、查询、片段、用户凭据、IP地址、localhost或控制字符。无效配置返回 `CONFIG_INVALID`，在发出请求前终止。

```python
from file_delivery.qiniu_store import QiniuStore
store = QiniuStore.from_file("/absolute/private/qiniu.json")
```

构造对象和 `signed_url` 不联网；其余下列操作会访问真实服务，须有对应配置和操作授权。

## API及结果含义

| 方法 | 行为 |
| --- | --- |
| `ensure_private()` | 查询空间信息并确认私有属性；不修改空间权限 |
| `upload(path, key, retention_days=30)` | 上传一个文件，禁止覆盖，校验返回对象键和七牛ETag；返回uploaded |
| `stat(key)` | 查询对象；仅明确的612响应表示不存在 |
| `signed_url(key, ttl_seconds=604800)` | 生成临时链接和expires_at；生成不证明当前可下载或实际过期 |
| `verify_download(key, expected_sha256, expected_size)` | 确认匿名访问被拒绝，再流式读取签名下载并核对SHA256和大小 |
| `delete(key)` | 删除后查询确认对象不存在，才返回object-deleted |

交付流程应先用本项目 `pack`/`verify` 生成并验证加密包，然后只上传 archive.zip。适配器本身不会加密任意传入文件，也不会自动上传密码或清单。签名链接是敏感数据，不应记录到普通日志。

链接有效期与对象保留期独立。生命周期策略不保证精确时刻删除；源对象删除也不证明 CDN 缓存立即失效，更不能撤回已下载副本。后续远程账本必须分别记录这些状态。

## 故障与当前限制

请求不自动重试，禁用重定向并设置有限超时。网络或下载流中断返回脱敏 `REMOTE_UNKNOWN`，调用者不能据此断定远端操作未发生；应先核对对象再决定后续动作。其他关键错误包括 `BUCKET_NOT_PRIVATE`、`REMOTE_AUTH`、`REMOTE_CONFLICT`、`REMOTE_INTEGRITY`、`REMOTE_DELETE_UNCONFIRMED` 和 `DEPENDENCY_MISSING`。

当前使用表单上传，文件会读入内存；尚未验证大文件性能或断点续传。真实域名、实际链接过期、上传下载费用、删除后的旧链接访问都需要后续真实联调，离线测试不替代这些证据。
