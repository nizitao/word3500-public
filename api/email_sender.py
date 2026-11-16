#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 word3500 contributors
# SPDX-License-Identifier: GPL-3.0

"""
word3500 邮件发送模块 —— 仅用 Python 标准库 smtplib + email.mime

两种模式（自动检测）：
  1. 本机 MTA 模式（默认）：连接 localhost:25，无需配置，依赖系统 Postfix/sendmail
  2. 远程 SMTP 模式：通过环境变量配置外部 SMTP（QQ/163/Gmail 等）

发件人地址优先级：WORD3500_SMTP_FROM > WORD3500_SMTP_USER > noreply@<主机名>
"""
import os
import socket
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ---- 配置 ----
SMTP_HOST = os.environ.get("WORD3500_SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("WORD3500_SMTP_PORT", "25"))
SMTP_USER = os.environ.get("WORD3500_SMTP_USER", "")
SMTP_PASS = os.environ.get("WORD3500_SMTP_PASS", "")
SMTP_USE_TLS = os.environ.get("WORD3500_SMTP_TLS", "0") == "1"

# 发件人地址：优先环境变量 > SMTP_USER > 本机 MTA 兜底 noreply@<主机名>
_hostname = socket.gethostname()
_local_from = f"noreply@{_hostname}" if "." in _hostname else f"noreply@{_hostname}.local"
SMTP_FROM = os.environ.get("WORD3500_SMTP_FROM", "") or SMTP_USER or _local_from

# 本机 MTA 模式：localhost + 无认证
USE_LOCAL_MTA = (SMTP_HOST in ("localhost", "127.0.0.1", "::1")) and not SMTP_USER


def is_configured() -> bool:
    """检查是否有可用的邮件发送方式"""
    if USE_LOCAL_MTA:
        # 检查本机 25 端口是否可达
        try:
            with socket.create_connection(("localhost", 25), timeout=1):
                return True
        except OSError:
            pass
        return False
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def send_verify_email(to_email: str, username: str, verify_url: str):
    """
    异步发送邮箱验证邮件。
    无可用发送方式时静默跳过。
    """
    if not is_configured():
        print(f"[email] 无可用的邮件发送方式，跳过 {to_email}")
        return

    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "邮箱验证 - 高考英语词汇3500"
            msg["From"] = SMTP_FROM
            msg["To"] = to_email

            html_body = f"""\
<html><body style="font-family:'Microsoft YaHei',sans-serif;padding:20px;background:#f5f7fa">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.06)">
<h2 style="color:#1e293b;margin:0 0 8px">验证您的邮箱</h2>
<p style="color:#475569;line-height:1.6">您好 <b>{username}</b>，感谢注册高考英语词汇3500！</p>
<p style="color:#475569;line-height:1.6">请点击下方按钮验证您的邮箱地址：</p>
<p style="text-align:center;margin:24px 0">
<a href="{verify_url}" style="display:inline-block;padding:12px 32px;background:#2f6fed;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">验证邮箱</a>
</p>
<p style="color:#94a3b8;font-size:12px;line-height:1.5">如按钮无法点击，请复制以下链接：<br>{verify_url}</p>
<hr style="border:none;border-top:1px solid #e8ecf1;margin:20px 0">
<p style="color:#94a3b8;font-size:12px">此邮件由系统自动发送，请勿回复。</p>
</div></body></html>"""

            text_body = f"您好 {username}，请访问以下链接验证邮箱：\n{verify_url}"

            msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_USE_TLS:
                    server.starttls()
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [to_email], msg.as_string())

            print(f"[email] 验证邮件已发送至 {to_email} (via {SMTP_HOST}:{SMTP_PORT})")
        except Exception as e:
            print(f"[email] 发送失败 ({to_email}): {e}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()
