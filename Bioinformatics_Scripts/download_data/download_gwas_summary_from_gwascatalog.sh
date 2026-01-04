#!/bin/bash

# GWAS Catalog 数据下载脚本 v2.0
# 支持三种模式：
#   1. 单个下载：直接输入 GCST 编号
#   2. 批量下载（单列）：输入只有 GCST 编号的 txt 文件
#   3. 批量下载（双列）：输入包含 GCST 编号和目录名的 txt 文件

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的信息
print_info() {
    echo -e "${BLUE}[信息]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[成功]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[警告]${NC} $1"
}

print_error() {
    echo -e "${RED}[错误]${NC} $1"
}

# 显示帮助信息
show_help() {
    echo ""
    echo "=========================================="
    echo "  GWAS Catalog 数据下载器 v2.0"
    echo "=========================================="
    echo ""
    echo "用法:"
    echo "  $0 <GCST编号> [保存目录名]        # 模式1: 单个下载"
    echo "  $0 -f <txt文件>                   # 模式2/3: 批量下载"
    echo "  $0 -h                             # 显示帮助信息"
    echo ""
    echo "模式说明:"
    echo ""
    echo "  模式1 - 单个下载:"
    echo "    $0 GCST90044006"
    echo "    $0 GCST90044006 atherosclerosis"
    echo ""
    echo "  模式2 - 批量下载（单列txt）:"
    echo "    txt文件格式（每行一个GCST编号）:"
    echo "      GCST90044006"
    echo "      GCST90044007"
    echo "      GCST90044008"
    echo "    命令: $0 -f gcst_list.txt"
    echo ""
    echo "  模式3 - 批量下载（双列txt）:"
    echo "    txt文件格式（制表符或空格分隔）:"
    echo "      GCST90044006    atherosclerosis"
    echo "      GCST90044007    diabetes"
    echo "      GCST90044008    hypertension"
    echo "    命令: $0 -f gcst_list.txt"
    echo ""
    echo "=========================================="
}

# 验证 GCST 编号格式
validate_gcst() {
    local gcst_id="$1"
    if [[ "$gcst_id" =~ ^GCST[0-9]+$ ]]; then
        return 0
    else
        return 1
    fi
}

# 计算 FTP 范围目录
get_range_dir() {
    local gcst_id="$1"
    local number="${gcst_id#GCST}"
    number=$((10#$number))
    
    local range_start=$(( (number - 1) / 1000 * 1000 + 1 ))
    local range_end=$(( range_start + 999 ))
    
    printf "GCST%08d-GCST%08d" $range_start $range_end
}

# 下载单个 GCST 数据集
download_single() {
    local gcst_id="$1"
    local save_dir="$2"
    
    # 验证 GCST 编号
    if ! validate_gcst "$gcst_id"; then
        print_error "GCST 编号格式不正确: $gcst_id"
        return 1
    fi
    
    # 获取范围目录
    local range_dir=$(get_range_dir "$gcst_id")
    
    # 构建 FTP URL
    local ftp_url="ftp://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/${range_dir}/${gcst_id}/"
    
    echo ""
    echo "----------------------------------------"
    print_info "GCST 编号: $gcst_id"
    print_info "范围目录: $range_dir"
    print_info "下载地址: $ftp_url"
    print_info "保存目录: ./$save_dir"
    echo "----------------------------------------"
    
    # 开始下载
    wget -r -np -nH --cut-dirs=6 -P "./$save_dir" "$ftp_url" 2>&1
    
    if [ $? -eq 0 ]; then
        print_success "下载完成: $gcst_id -> ./$save_dir"
        return 0
    else
        print_error "下载失败: $gcst_id"
        return 1
    fi
}

# 检测 txt 文件格式（单列还是双列）
detect_txt_format() {
    local txt_file="$1"
    
    # 读取第一行非空行
    local first_line=$(grep -v '^[[:space:]]*$' "$txt_file" | head -n 1)
    
    # 检查是否包含制表符或多个空格分隔的第二列
    if echo "$first_line" | grep -qE $'[\t]' || echo "$first_line" | grep -qE '[[:space:]]{2,}'; then
        echo "double"
    elif echo "$first_line" | grep -qE '^GCST[0-9]+[[:space:]]+[^[:space:]]+'; then
        echo "double"
    else
        echo "single"
    fi
}

# 批量下载
batch_download() {
    local txt_file="$1"
    
    # 检查文件是否存在
    if [ ! -f "$txt_file" ]; then
        print_error "文件不存在: $txt_file"
        exit 1
    fi
    
    # 检测文件格式
    local format=$(detect_txt_format "$txt_file")
    
    echo ""
    echo "=========================================="
    echo "  GWAS Catalog 批量下载器"
    echo "=========================================="
    
    if [ "$format" == "double" ]; then
        print_info "检测到双列格式（GCST编号 + 目录名）"
    else
        print_info "检测到单列格式（仅GCST编号）"
    fi
    
    # 统计计数
    local total=0
    local success=0
    local failed=0
    local skipped=0
    
    # 创建失败列表
    local failed_list=""
    
    # 计算总数
    total=$(grep -cvE '^[[:space:]]*$' "$txt_file")
    print_info "共检测到 $total 个数据集待下载"
    echo "=========================================="
    
    # 逐行读取并下载
    local line_num=0
    while IFS= read -r line || [ -n "$line" ]; do
        # 跳过空行
        if [[ -z "${line// }" ]]; then
            continue
        fi
        
        ((line_num++))
        
        # 解析 GCST 编号和目录名
        local gcst_id=""
        local save_dir=""
        
        if [ "$format" == "double" ]; then
            # 双列格式：提取第一列和第二列
            gcst_id=$(echo "$line" | awk '{print $1}')
            save_dir=$(echo "$line" | awk '{print $2}')
            
            # 如果第二列为空，使用 GCST 编号作为目录名
            if [ -z "$save_dir" ]; then
                save_dir="$gcst_id"
            fi
        else
            # 单列格式：只有 GCST 编号
            gcst_id=$(echo "$line" | awk '{print $1}')
            save_dir="$gcst_id"
        fi
        
        # 清理可能的回车符
        gcst_id=$(echo "$gcst_id" | tr -d '\r')
        save_dir=$(echo "$save_dir" | tr -d '\r')
        
        echo ""
        print_info "[$line_num/$total] 正在处理: $gcst_id"
        
        # 验证 GCST 编号
        if ! validate_gcst "$gcst_id"; then
            print_warning "跳过无效编号: $gcst_id"
            ((skipped++))
            continue
        fi
        
        # 下载
        if download_single "$gcst_id" "$save_dir"; then
            ((success++))
        else
            ((failed++))
            failed_list="$failed_list$gcst_id\n"
        fi
        
        # 短暂延迟，避免对服务器造成压力
        sleep 1
        
    done < "$txt_file"
    
    # 显示汇总信息
    echo ""
    echo "=========================================="
    echo "  下载完成 - 汇总报告"
    echo "=========================================="
    print_info "总计: $total"
    print_success "成功: $success"
    if [ $failed -gt 0 ]; then
        print_error "失败: $failed"
    else
        echo -e "失败: $failed"
    fi
    if [ $skipped -gt 0 ]; then
        print_warning "跳过: $skipped"
    else
        echo -e "跳过: $skipped"
    fi
    echo "=========================================="
    
    # 如果有失败的，列出失败列表
    if [ $failed -gt 0 ]; then
        echo ""
        print_error "以下数据集下载失败:"
        echo -e "$failed_list"
    fi
}

# 主程序入口
main() {
    # 无参数时显示帮助
    if [ $# -eq 0 ]; then
        show_help
        exit 0
    fi
    
    # 解析参数
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -f|--file)
            # 批量下载模式
            if [ -z "$2" ]; then
                print_error "请指定 txt 文件路径"
                echo "用法: $0 -f <txt文件>"
                exit 1
            fi
            batch_download "$2"
            ;;
        GCST*)
            # 单个下载模式
            local gcst_id="$1"
            local save_dir="${2:-$gcst_id}"
            
            echo ""
            echo "=========================================="
            echo "  GWAS Catalog 数据下载器"
            echo "=========================================="
            
            download_single "$gcst_id" "$save_dir"
            
            if [ $? -eq 0 ]; then
                echo ""
                echo "=========================================="
                print_success "下载完成！"
                echo "文件列表:"
                ls -lh "./$save_dir" 2>/dev/null
                echo "=========================================="
            fi
            ;;
        *)
            print_error "未知参数: $1"
            show_help
            exit 1
            ;;
    esac
}

# 运行主程序
main "$@"
