"""新しい空きの通知を、メールで送る(Slackのチャンネル用メールアドレスなどに送れる)。

scraper.py が作った notify_line.txt(1行目=件名、2行目以降=本文)を読んで、SMTPで送ります。
設定は GitHub の Secrets から環境変数で渡されます。設定が無ければ、何もせずに終わります。
    SMTP_USER      送信に使うメールアドレス(例: Gmail)
    SMTP_PASSWORD  そのアドレスの「アプリパスワード」(スペースは自動で取り除きます)
    MAIL_TO        送り先(カンマ区切りで複数可)
    SMTP_HOST / SMTP_PORT  省略時は smtp.gmail.com(587→だめなら465を試す)
"""
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def build_message(user, to, text):
    title, _, body = text.partition("\n")
    msg = EmailMessage()
    msg["Subject"] = title.strip()
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body.strip() or title)
    return msg


def send(msg, host, ports, user, password):
    ctx = ssl.create_default_context()
    for port in ports:
        mode = "SSL" if port == 465 else "STARTTLS"
        try:
            if mode == "SSL":
                smtp = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
            else:
                smtp = smtplib.SMTP(host, port, timeout=30)
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.ehlo()
            with smtp:
                print(f"[mail] {host}:{port} ({mode}) に接続しました")
                smtp.login(user, password)
                smtp.send_message(msg)
            return True
        except smtplib.SMTPAuthenticationError as e:
            print(f"[mail] ログインできませんでした(コード {e.smtp_code})。"
                  "送信用アドレスと、アプリパスワードを確認してください")
            return False
        except Exception as e:
            print(f"[mail] {host}:{port} ({mode}) で失敗しました: {type(e).__name__}: {e}")
    return False


def main():
    user = os.environ.get("SMTP_USER", "").strip()
    password = "".join(os.environ.get("SMTP_PASSWORD", "").split())   # スペース・改行をすべて取り除く
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").replace(";", ",").split(",") if a.strip()]
    if not (user and password and to):
        print("[mail] メールの設定が無いので、スキップします")
        return 0
    with open("notify_line.txt", encoding="utf-8") as f:
        msg = build_message(user, to, f.read())
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = os.environ.get("SMTP_PORT", "").strip()
    ports = [int(port)] if port else [587, 465]
    if send(msg, host, ports, user, password):
        print(f"[mail] {len(to)} 件のアドレスに送信しました")
        return 0
    print("[mail] 送信できませんでした")
    return 1


if __name__ == "__main__":
    sys.exit(main())
