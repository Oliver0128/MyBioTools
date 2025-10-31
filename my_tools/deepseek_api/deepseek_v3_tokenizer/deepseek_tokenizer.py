#!/home/huangxianzhe/miniconda3/envs/ukb/bin/python3
# -*- coding: utf-8 -*-
# 注意：请确保 Shebang 行路径正确，且文件有执行权限 (chmod +x)

import sys
import argparse
import transformers
import os

# --- 配置信息 ---
CHAT_TOKENIZER_DIR = "/home/huangxianzhe/workdir/software/deepseek_v3_tokenizer"


def main():
    """主函数：解析参数，流式执行分词，将总Token数写入文件末尾。"""
    
    # 1. 定义命令行参数解析器
    parser = argparse.ArgumentParser(
        description="使用 DeepSeek 分词器对输入文件进行流式分词，将结果写入文件，总Token数在最后一行。"
    )
    parser.add_argument(
        '-i', '--input', 
        type=str, 
        required=True, 
        help="待分词的输入文件路径（每行文本将被单独处理）。"
    )
    parser.add_argument(
        '-o', '--output', 
        type=str, 
        required=True, 
        help="分词结果输出文件路径。"
    )
    
    # 2. 解析参数
    args = parser.parse_args()
    
    # 3. 加载分词器
    try:
        print(f"--- 正在从 {CHAT_TOKENIZER_DIR} 加载分词器... ---", file=sys.stderr)
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            CHAT_TOKENIZER_DIR, 
            trust_remote_code=True
        )
    except Exception as e:
        print(f"错误：分词器加载失败。错误详情: {e}", file=sys.stderr)
        sys.exit(1)

    # 4. 流式处理、写入和统计
    total_tokens = 0  # 初始化总 Token 计数器
    
    try:
        # 4a. 打开输入和输出文件，进行流式处理
        with open(args.input, 'r', encoding='utf-8') as infile, \
             open(args.output, 'w', encoding='utf-8') as outfile:
            
            print(f"--- 开始流式分词和统计... ---", file=sys.stderr)
            
            # 逐行读取输入文件
            for line in infile:
                clean_line = line.strip() 
                
                if not clean_line:
                    # 遇到空行时，只写入一个换行符（保持行数对应），但不计入 token 总数
                    outfile.write("\n") 
                    continue
                
                # 执行分词
                token_ids = tokenizer.encode(clean_line)
                
                # 统计并累加
                total_tokens += len(token_ids)
                
                # 格式化输出字符串
                output_str = ",".join(map(str, token_ids))
                
                # 写入分词结果到输出文件
                outfile.write(f"{output_str}\n")
                
            # 4b. 🚨 关键修改: 在文件末尾写入总 Token 数量
            # 使用 f-string 写入，确保独占一行
            outfile.write(f"{total_tokens}\n")
                
        print(f"--- 分词完成！总Token数为 {total_tokens}。结果已保存至: {args.output} ---", file=sys.stderr)

    except FileNotFoundError:
        print(f"错误：输入文件 {args.input} 未找到。", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"处理文件或写入文件时发生未预料的错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()