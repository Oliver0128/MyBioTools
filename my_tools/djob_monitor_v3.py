#!/usr/bin/env python3
import os
import time
import argparse
import smtplib
from email.mime.text import MIMEText

# --- 你的邮箱配置 (需要修改) ---
SENDER_EMAIL = ""
SENDER_PASSWORD = ""  # 授权码
RECEIVER_EMAIL = ""
SMTP_SERVER = "smtp.163.com"
SMTP_PORT = 465

def check_and_extract_log(job_id, directory):
    """
    检查任务是否完成，并精准提取指定的日志部分。
    1. 寻找 .out 文件。
    2. 验证文件内容是否完整（包含三个关键字）。
    3. 验证 TASK_INFO 下一行是否为 JOB_ID。
    4. 提取并返回 TASK_INFO 和 RESOURCE_USAGE_SUMMARY 两部分。

    返回: (布尔值: 是否成功, 字符串: 提取的日志 或 None)
    """
    output_filepath = None
    try:
        # 步骤 1: 寻找 .out 文件
        for filename in os.listdir(directory):
            if str(job_id) in filename and filename.endswith('.out') and \
               os.path.isfile(os.path.join(directory, filename)):
                output_filepath = os.path.join(directory, filename)
                break

        if not output_filepath:
            return False, None

        # 读取文件内容
        with open(output_filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # 步骤 2: 验证内容完整性
        if not all(k in content for k in ["TASK_INFO", "RESOURCE_USAGE_SUMMARY", "EXIT_MESSAGE"]):
            return False, None

        lines = content.splitlines()

        # 步骤 3: 严格验证 TASK_INFO 和 JOB_ID 的位置
        try:
            task_info_index = lines.index("TASK_INFO:")
            # 确保 JOB_ID 紧随 TASK_INFO 之后
            if "JOB_ID:" not in lines[task_info_index + 1]:
                print(f"\n验证失败: 'TASK_INFO:' 下一行不是 'JOB_ID:'。文件可能不规范。")
                return False, None
            resource_summary_index = lines.index("RESOURCE_USAGE_SUMMARY:")
        except (ValueError, IndexError):
            # 如果关键字找不到或索引越界
            return False, None

        # 步骤 4: 精准提取日志块
        # TASK_INFO 块从 "TASK_INFO:" 开始，到 "RESOURCE_USAGE_SUMMARY:" 之前结束
        task_info_block = lines[task_info_index:resource_summary_index]
        # RESOURCE_USAGE_SUMMARY 块从 "RESOURCE_USAGE_SUMMARY:" 开始，到文件末尾
        resource_summary_block = lines[resource_summary_index:]

        # 合并两个块，并用换行符连接成一个字符串
        extracted_content = "\n".join(task_info_block + resource_summary_block)

        return True, extracted_content

    except FileNotFoundError:
        return False, None
    except Exception as e:
        print(f"\n检查文件时发生未知错误: {e}")
        return False, None


def send_email(job_id, directory, extracted_log):
    """发送包含精准提取日志的邮件。"""
    try:
        email_content = (
            f"位于目录 {directory} 下的任务 {job_id} 已结束。\n\n"
            f"关键日志信息如下:\n"
            f"========================================\n\n"
            f"{extracted_log}"
        )
        msg = MIMEText(email_content, 'plain', 'utf-8')
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = f"任务 {job_id} 关键日志报告"

        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        server.quit()
        print(f"\n已成功发送任务 {job_id} 的关键日志邮件。")
    except Exception as e:
        print(f"\n邮件发送失败: {e}")
        pass

def main():
    parser = argparse.ArgumentParser(
        description="监控 .out 文件，验证内容并提取指定部分后，通过邮件发送。"
    )
    parser.add_argument('job_id', type=int, help="要监控的任务ID。")
    parser.add_argument('path', type=str, help="要监控文件所在的目录路径。")
    parser.add_argument(
        '-i', '--interval', type=int, default=60,
        help="检查文件是否存在的间隔时间（秒），默认为60秒。"
    )
    args = parser.parse_args()

    print(f"--- 开始监控任务 {args.job_id} ---")
    print(f"监控目录: {args.path}")
    print(f"检查间隔: {args.interval} 秒")
    print("确认 .out 文件内容并提取关键信息后将发送邮件。按 Ctrl+C 退出。")

    while True:
        is_complete, extracted_data = check_and_extract_log(args.job_id, args.path)

        if is_complete:
            send_email(args.job_id, args.path, extracted_data)
            break
        else:
            try:
                time.sleep(args.interval)
                print(".", end='', flush=True)
            except KeyboardInterrupt:
                print("\n用户手动中断监控。脚本退出。")
                break

if __name__ == "__main__":
    main()
