# app/service/email_service.py
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from .. import models
from ..crypto import decrypt_value

def send_emergency_email(email_config: models.Email, plugin_name: str, alert_msg: str):
    """专门用于发送紧急风控报警的邮件引擎"""
    auth_code = decrypt_value(email_config.auth_code)
    try:
        msg = MIMEMultipart("alternative")
        msg['Subject'] = f"🚨【闲鱼雷达-红色警报】节点 [{plugin_name}] 触发验证码！"
        msg['From'] = email_config.sender
        msg['To'] = email_config.receiver or email_config.sender

        html_content = f"""
        <div style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px; border-radius: 8px; background: #fff3f3; border: 1px solid #ffccc7;">
            <h2 style="color: #cf1322; margin-top: 0;">🚨 自动化节点风控报警</h2>
            <p style="color: #555;"><b>节点名称：</b> {plugin_name}</p>
            <p style="color: #555;"><b>报警时间：</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <hr style="border: none; border-top: 1px dashed #ffccc7; margin: 15px 0;">
            <p style="color: #333; font-weight: bold;">详细报警内容：</p>
            <pre style="background: #fff; padding: 15px; border-radius: 4px; border: 1px solid #f5222d; color: #cf1322; white-space: pre-wrap;">{alert_msg}</pre>
            <div style="margin-top: 20px; padding: 15px; background: #ffe58f; border-radius: 4px; color: #ad6800;">
                <b>⚠️ 动作执行结果：</b><br>
                1. 该节点的自动化引擎已在本地紧急锁死。<br>
                2. 云端状态已被强制修改为 [未激活]。<br>
                👉 <b>处理建议：</b>请尽快登录服务器或远程桌面，手动滑过验证码后，在管理面板重新启动节点！
            </div>
        </div>
        """
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        with smtplib.SMTP_SSL(email_config.service, email_config.port, timeout=15) as server:
            server.login(email_config.sender, auth_code)
            server.sendmail(email_config.sender, email_config.receiver or email_config.sender, msg.as_string())
        print(f"📧 报警邮件已成功火速送达至发送人：{email_config.sender} 接收人：{email_config.receiver or email_config.sender}")
    except Exception as e:
        print(f"❌ 报警邮件发送失败: {e}")