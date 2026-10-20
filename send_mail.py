"""新しい空きの通知を、メールで送る(Slackのチャンネル用メールアドレスなどに送れる)。

scraper.py が作った notify_line.txt(1行目=件名、2行目以降=本文)を読んで、SMTPで送ります。
設定は GitHub の Secrets から環境変数で渡されます。設定が無ければ、何もせずに終わります。
    SMTP_USER      送信に使うメールアドレス(例: Gmail)
    SMTP_PASSWORD  そのアドレスの「アプリパスワード」
    MAIL_TO        送り先(カンマ区切りで複数可)
    SMTP_HOST / SMTP_PORT  省略時は smtp.gmail.com / 587
"""
import os
import smtplib
from email.message import EmailMessage


def main():
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").replace(";", ",").split(",") if a.strip()]
    if not (user and password and to):
        print("[mail] メールの設定が無いので、スキップします")
        return
    with open("notify_line.txt", encoding="utf-8") as f:
        title, _, body = f.read().partition("\n")
    msg = EmailMessage()
    msg["Subject"] = title.strip()
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body.strip() or title)
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"[mail] {len(to)} 件のアドレスに送信しました")


if __name__ == "__main__":
    main()
