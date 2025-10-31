#!/home/huangxianzhe/miniconda3/envs/ukb/bin/python3
# -*- coding: utf-8 -*-

import sys
import csv
import os
from openai import OpenAI
from pathlib import Path  # 引入 pathlib 用于更优雅的文件路径操作

# ----------------- 【配置信息】 -----------------

# ⚠️ 替换为您自己的 DeepSeek API 密钥
API_KEY = 'sk-98b68b44409142b1b49265c5aaed2139' 
# 文件路径：请确保该文件路径是正确的
FILE_PATH = '../output/protein_header/column_headers.txt'

# API Base URL 
BASE_URL = "https://api.deepseek.com/v1"

# 临时文件和最终文件输出目录
OUTPUT_DIR = Path("../output/protein_header/")
FINAL_OUTPUT_FILE = OUTPUT_DIR / "translated_columns_final.md"
BATCH_SIZE = 100 # 每批次处理的行数

# ----------------- 【核心功能函数】 -----------------

def read_and_extract_column(file_path):
    """读取文件，提取第二列英文描述。"""
    english_columns = []
    # 路径检查：如果是相对路径，则转换为绝对路径以确保稳定
    abs_file_path = Path(file_path).resolve()
    
    with open(abs_file_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if len(row) > 1:
                english_columns.append(row[1].strip())
    return english_columns

def batch_data(data, batch_size):
    """使用生成器将数据按指定大小分批，并生成序号（从 1 开始）。"""
    
    # 初始化批次序号
    batch_index = 0
    # 遍历数据，每 batch_size 个元素切片一次
    for i in range(0, len(data), batch_size):
        batch_index += 1
        yield batch_index, data[i:i + batch_size]

def create_translation_prompt(batch_index, english_list):
    """根据提取的列表创建 LLM 翻译指令，并加入批次序号。"""
    
    # 将列表内容以编号的形式整合到 Prompt 中
    # 注意：这里我们只使用简单的换行符连接，让模型自行处理格式
    numbered_list = "\n".join([f"{i+1}. {item}" for i, item in enumerate(english_list)])

    # 关键：在 Prompt 中包含批次序号，确保模型知道是第几批
    prompt = f"""
你是一名专业的生物医学翻译员。正在处理第 {batch_index} 批数据。你的任务是将以下列表中的每一行英文翻译成简洁、准确的中文。

要求：
1. 翻译：将每一项英文描述翻译成中文。
2. 格式化：以 Markdown 表格的形式输出最终结果。表格必须有且仅有两列：原始英文 和 中文翻译。
3. 内容：只输出表格内容，不要输出任何额外的解释性文字。

---
待翻译的英文列表 (第 {batch_index} 批):
{numbered_list}
---
"""
    return prompt

def call_deepseek_api(prompt):
    """调用 DeepSeek API 并返回结果。"""
    
    if not API_KEY or API_KEY == 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx':
        raise ValueError("API 密钥未设置或使用了占位符。请修改脚本中的 API_KEY。")

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一名严谨且专业的生物医学翻译专家。"},
                {"role": "user", "content": prompt},
            ],
            stream=False 
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"致命错误：API 调用失败。错误详情: {e}", file=sys.stderr)
        # 遇到 API 错误时，直接返回 None，让主程序跳过当前批次
        return None

def merge_results(output_dir, final_output_file, total_batches):
    """按序号顺序合并所有临时批次文件，并删除临时文件。"""
    
    print("\n--- 开始合并所有批次结果 ---", file=sys.stderr)
    
    # 用于存储所有成功合并的临时文件路径，以便最终清理
    temp_files_to_clean = []
    
    with open(final_output_file, 'w', encoding='utf-8') as outfile:
        # 循环遍历所有批次序号，确保按顺序合并
        for batch_index in range(1, total_batches + 1):
            temp_file = output_dir / f"temp_batch_{batch_index}.md"
            
            if not temp_file.exists():
                print(f"警告：批次 {batch_index} 的临时文件 {temp_file} 未找到，跳过。", file=sys.stderr)
                continue
                
            with open(temp_file, 'r', encoding='utf-8') as infile:
                # 写入一个分隔符，方便查看
                outfile.write(f"\n\n# --- Batch {batch_index} ---\n\n")
                outfile.write(infile.read())
            
            # 记录成功合并的文件
            temp_files_to_clean.append(temp_file)
            
    print(f"\n--- 结果合并完成，已保存至: {final_output_file} ---", file=sys.stderr)
    
    # 清理临时文件
    for f in temp_files_to_clean:
        os.remove(f)
    print("--- 临时文件清理完毕 ---", file=sys.stderr)


# ----------------- 【主程序运行】 -----------------

if __name__ == "__main__":
    
    # 1. 提取所有第二列数据
    print(f"--- 正在读取文件: {FILE_PATH} ---", file=sys.stderr)
    english_list = read_and_extract_column(FILE_PATH)
    
    if not english_list:
        print("错误：未提取到任何数据。程序终止。", file=sys.stderr)
        sys.exit(1)
    
    print(f"成功提取 {len(english_list)} 行数据。开始分批处理...", file=sys.stderr)
    
    # 确保输出目录存在
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    total_lines = len(english_list)
    total_batches = (total_lines + BATCH_SIZE - 1) // BATCH_SIZE
    
    # 2. 循环处理每个批次
    for batch_index, batch_data_list in batch_data(english_list, BATCH_SIZE):
        
        temp_output_file = OUTPUT_DIR / f"temp_batch_{batch_index}.md"
        print(f"\n--- 正在处理批次 {batch_index}/{total_batches} (包含 {len(batch_data_list)} 行) ---", file=sys.stderr)
        
        # 构造 Prompt
        translation_prompt = create_translation_prompt(batch_index, batch_data_list)
        
        # 调用 API
        markdown_table = call_deepseek_api(translation_prompt)
        
        if markdown_table:
            # 写入临时文件
            with open(temp_output_file, 'w', encoding='utf-8') as f:
                f.write(markdown_table)
            print(f"✅ 批次 {batch_index} 结果已写入临时文件: {temp_output_file.name}", file=sys.stderr)
        else:
            # API 调用失败，不生成临时文件，继续下一批次
            print(f"❌ 批次 {batch_index} API 调用失败，将跳过此批次。", file=sys.stderr)
            
    # 3. 合并所有结果
    merge_results(OUTPUT_DIR, FINAL_OUTPUT_FILE, total_batches)
    
    print("\n--- 所有任务完成 ---", file=sys.stderr)