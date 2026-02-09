#!/bin/bash

# GWAS Catalog Batch Download Script
# Usage: ./gwas_download.sh -i input.txt -o output_dir [-h yes|no] [-l log_dir]

set -e

# Default values
HARMONISED_ONLY="no"
LOG_DIR=""

# Parse command line arguments
while getopts "i:o:h:l:" opt; do
    case $opt in
        i) INPUT_FILE="$OPTARG" ;;
        o) OUTPUT_DIR="$OPTARG" ;;
        h) HARMONISED_ONLY="$OPTARG" ;;
        l) LOG_DIR="$OPTARG" ;;
        *) 
            echo "Usage: $0 -i <input_file> -o <output_dir> [-h yes|no] [-l log_dir]"
            echo "  -i  Input file with GWAS IDs (one per line, no header)"
            echo "  -o  Output directory for downloaded data"
            echo "  -h  Download harmonised only (yes/no, default: no)"
            echo "  -l  Log directory (default: output_dir/logs)"
            exit 1
            ;;
    esac
done

# Validate required parameters
if [[ -z "$INPUT_FILE" || -z "$OUTPUT_DIR" ]]; then
    echo "Error: -i and -o are required parameters"
    echo "Usage: $0 -i <input_file> -o <output_dir> [-h yes|no] [-l log_dir]"
    exit 1
fi

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "Error: Input file '$INPUT_FILE' not found"
    exit 1
fi

# Set log directory
if [[ -z "$LOG_DIR" ]]; then
    LOG_DIR="$OUTPUT_DIR/logs"
fi

# Create directories
mkdir -p "$OUTPUT_DIR"
mkdir -p "$LOG_DIR"

# Log files
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SUCCESS_LOG="$LOG_DIR/success_${TIMESTAMP}.log"
NO_HARMONISED_LOG="$LOG_DIR/no_harmonised_${TIMESTAMP}.log"
FAILED_LOG="$LOG_DIR/failed_${TIMESTAMP}.log"
MAIN_LOG="$LOG_DIR/main_${TIMESTAMP}.log"

# Initialize log files
echo "# Download started at $(date)" > "$SUCCESS_LOG"
echo "# Download started at $(date)" > "$NO_HARMONISED_LOG"
echo "# Download started at $(date)" > "$FAILED_LOG"
echo "# Download started at $(date)" > "$MAIN_LOG"

# Base URL
BASE_URL="https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"

# Function to get the parent directory for a GWAS ID
get_parent_dir() {
    local gwas_id=$1
    local num=$(echo "$gwas_id" | grep -oP '\d+')
    # Force base 10 to avoid octal interpretation of leading zeros
    num=$((10#$num))
    local start=$(( (num - 1) / 1000 * 1000 + 1 ))
    local end=$(( start + 999 ))
    
    # Check if it's a 90-series ID (8 digits) or regular ID (6 digits)
    if [[ $num -ge 90000000 ]]; then
        # 90-series: GCST90000001-GCST90001000 format
        printf "GCST%08d-GCST%08d" "$start" "$end"
    else
        # Regular: GCST000001-GCST001000 format (6 digits)
        printf "GCST%06d-GCST%06d" "$start" "$end"
    fi
}

# Function to check if harmonised directory exists and contains .h.tsv.gz files
check_harmonised_exists() {
    local url=$1
    # Get directory listing and check for .h.tsv.gz files
    local content=$(curl -s "$url")
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    # Check if response contains .h.tsv.gz file links
    echo "$content" | grep -q '\.h\.tsv\.gz'
}

# Count total studies
TOTAL=$(wc -l < "$INPUT_FILE")
CURRENT=0

echo "Starting download of $TOTAL studies..."
echo "Harmonised only: $HARMONISED_ONLY"
echo "Output directory: $OUTPUT_DIR"
echo "Log directory: $LOG_DIR"
echo ""

# Process each GWAS ID
while IFS= read -r GWAS_ID || [[ -n "$GWAS_ID" ]]; do
    # Skip empty lines
    [[ -z "$GWAS_ID" ]] && continue
    
    # Trim whitespace
    GWAS_ID=$(echo "$GWAS_ID" | tr -d '[:space:]')
    
    CURRENT=$((CURRENT + 1))
    echo "[$CURRENT/$TOTAL] Processing: $GWAS_ID" | tee -a "$MAIN_LOG"
    
    # Construct URL
    PARENT_DIR=$(get_parent_dir "$GWAS_ID")
    
    if [[ "$HARMONISED_ONLY" == "yes" ]]; then
        # Harmonised only mode
        DOWNLOAD_URL="$BASE_URL/$PARENT_DIR/$GWAS_ID/harmonised/"
        TARGET_DIR="$OUTPUT_DIR/$GWAS_ID"
        
        # Check if harmonised directory exists
        if ! check_harmonised_exists "$DOWNLOAD_URL"; then
            echo "  -> No harmonised directory found, skipping" | tee -a "$MAIN_LOG"
            echo "$GWAS_ID" >> "$NO_HARMONISED_LOG"
            continue
        fi
        
        # Download harmonised directory
        mkdir -p "$TARGET_DIR"
        if wget -r -np -nH --cut-dirs=8 -e robots=off \
            -R "index.html*,robots.txt" \
            -P "$TARGET_DIR" \
            "$DOWNLOAD_URL" 2>>"$MAIN_LOG"; then
            echo "  -> Download successful" | tee -a "$MAIN_LOG"
            echo "$GWAS_ID" >> "$SUCCESS_LOG"
        else
            echo "  -> Download failed" | tee -a "$MAIN_LOG"
            echo "$GWAS_ID" >> "$FAILED_LOG"
        fi
    else
        # Download entire study directory (including subdirectories)
        DOWNLOAD_URL="$BASE_URL/$PARENT_DIR/$GWAS_ID/"
        TARGET_DIR="$OUTPUT_DIR/$GWAS_ID"
        
        mkdir -p "$TARGET_DIR"
        if wget -r -np -nH --cut-dirs=7 -e robots=off \
            -R "index.html*,robots.txt" \
            -P "$TARGET_DIR" \
            "$DOWNLOAD_URL" 2>>"$MAIN_LOG"; then
            echo "  -> Download successful" | tee -a "$MAIN_LOG"
            echo "$GWAS_ID" >> "$SUCCESS_LOG"
        else
            echo "  -> Download failed" | tee -a "$MAIN_LOG"
            echo "$GWAS_ID" >> "$FAILED_LOG"
        fi
    fi
    
done < "$INPUT_FILE"

# Summary
echo ""
echo "========== Download Summary ==========" | tee -a "$MAIN_LOG"
SUCCESS_COUNT=$(grep -c "^GCST" "$SUCCESS_LOG" 2>/dev/null || echo 0)
NO_HARM_COUNT=$(grep -c "^GCST" "$NO_HARMONISED_LOG" 2>/dev/null || echo 0)
FAILED_COUNT=$(grep -c "^GCST" "$FAILED_LOG" 2>/dev/null || echo 0)

echo "Total studies: $TOTAL" | tee -a "$MAIN_LOG"
echo "Successful: $SUCCESS_COUNT" | tee -a "$MAIN_LOG"
echo "No harmonised (skipped): $NO_HARM_COUNT" | tee -a "$MAIN_LOG"
echo "Failed: $FAILED_COUNT" | tee -a "$MAIN_LOG"
echo "" | tee -a "$MAIN_LOG"
echo "Logs saved to:" | tee -a "$MAIN_LOG"
echo "  Success: $SUCCESS_LOG" | tee -a "$MAIN_LOG"
echo "  No harmonised: $NO_HARMONISED_LOG" | tee -a "$MAIN_LOG"
echo "  Failed: $FAILED_LOG" | tee -a "$MAIN_LOG"
echo "  Main log: $MAIN_LOG" | tee -a "$MAIN_LOG"
