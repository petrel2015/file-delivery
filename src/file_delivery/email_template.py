"""Deterministic notification content; no files, network, or tracking assets."""

from datetime import datetime, timezone
from html import escape


def render(url: str, password: str, size: int, expires_at: int) -> tuple[str, str]:
    """Render validated delivery values into plain and mobile-friendly HTML."""
    expiry = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    plain = (
        "Your file delivery is ready.\n\n"
        f"Download URL: {url}\n"
        f"Archive password: {password}\n"
        f"Archive size (bytes): {size}\n"
        f"Link expires at (unix seconds): {expires_at}\n"
        f"Link expires at: {expiry}\n\n"
        "下载后请用解压密码打开 AES 加密 ZIP。链接与密码请妥善保管。\n"
    )
    safe_url, safe_password = escape(url, quote=True), escape(password, quote=True)
    safe_size, safe_expiry = escape(str(size)), escape(expiry)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件下载通知 · file-delivery</title></head>
<body style="margin:0;padding:0;background:#f3f5f8;color:#182230;font-family:Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;table-layout:fixed;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:560px;background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;table-layout:fixed;">
<tr><td style="padding:24px 20px 0;font-size:13px;font-weight:bold;letter-spacing:1px;color:#667085;">FILE DELIVERY</td></tr>
<tr><td style="padding:16px 20px 0;"><h1 style="margin:0;font-size:24px;line-height:1.4;">文件已准备好</h1>
<p style="margin:12px 0 0;font-size:15px;line-height:1.8;color:#475467;">有人通过 file-delivery 与你分享了文件。请在链接到期前下载，随后使用下方密码解压。</p></td></tr>
<tr><td style="padding:24px 20px;"><a href="{safe_url}" style="display:block;text-align:center;background:#2563eb;color:#ffffff;text-decoration:none;padding:15px 12px;border-radius:8px;font-size:16px;font-weight:bold;line-height:1.4;">下载加密文件</a></td></tr>
<tr><td style="padding:0 20px;"><p style="margin:0 0 8px;font-size:13px;color:#667085;">解压密码</p>
<div style="padding:14px;background:#f2f4f7;border:1px solid #e4e7ec;border-radius:8px;font-family:Consolas,monospace;font-size:17px;line-height:1.6;word-break:break-all;overflow-wrap:anywhere;">{safe_password}</div></td></tr>
<tr><td style="padding:20px;font-size:13px;line-height:1.9;color:#475467;">加密包大小：{safe_size} 字节<br>链接到期时间：{safe_expiry}</td></tr>
<tr><td style="padding:0 20px 24px;font-size:12px;line-height:1.8;color:#667085;">如果按钮无法打开，请复制下方链接到浏览器：<br>
<a href="{safe_url}" style="color:#2563eb;word-break:break-all;overflow-wrap:anywhere;">{safe_url}</a>
<p style="margin:16px 0 0;">密码用于解压 AES 加密 ZIP，请使用支持该格式的解压工具。链接和密码请妥善保管。</p></td></tr>
</table><p style="margin:16px 0 0;font-size:12px;color:#98a2b3;">file-delivery · 文件交付通知</p>
</td></tr></table></body></html>"""
    return plain, html
