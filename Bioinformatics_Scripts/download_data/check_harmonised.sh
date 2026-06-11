#!/bin/bash

# GWAS Catalog Harmonised Check Script
# Usage: ./check_harmonised.sh -i input.txt [-o output_dir] [-p prefix]

# Default values
OUTPUT_DIR="."
PREFIX=""

# Parse command line arguments
while getopts "i:o:p:" opt; do
    case $opt in
        i) INPUT_FILE="$OPTARG" ;;
        o) OUTPUT_DIR="$OPTARG" ;;
        p) PREFIX="$OPTARG" ;;
        *) 
            echo "Usage: $0 -i <input_file> [-o output_dir] [-p prefix]"
            echo "  -i  Input file with GWAS IDs (one per line, no header)"
            echo "  -o  Output directory for result files (default: current dir)"
            echo "  -p  Prefix for output file names (optional)"
            exit 1
            ;;
    esac
done

# Validate required parameters
if [[ -z "$INPUT_FILE" ]]; then
    echo "Error: -i is required"
    exit 1
fi

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "Error: Input file '$INPUT_FILE' not found"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Output files
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
if [[ -n "$PREFIX" ]]; then
    HAS_HARM="$OUTPUT_DIR/${PREFIX}_has_harmonised_${TIMESTAMP}.txt"
    NO_HARM="$OUTPUT_DIR/${PREFIX}_no_harmonised_${TIMESTAMP}.txt"
else
    HAS_HARM="$OUTPUT_DIR/has_harmonised_${TIMESTAMP}.txt"
    NO_HARM="$OUTPUT_DIR/no_harmonised_${TIMESTAMP}.txt"
fi

> "$HAS_HARM"
> "$NO_HARM"

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
    
    if [[ $num -ge 90000000 ]]; then
        printf "GCST%08d-GCST%08d" "$start" "$end"
    else
        printf "GCST%06d-GCST%06d" "$start" "$end"
    fi
}

# Count total
TOTAL=$(wc -l < "$INPUT_FILE")
CURRENT=0

echo "Checking $TOTAL studies for harmonised data..."
echo ""

while IFS= read -r GWAS_ID || [[ -n "$GWAS_ID" ]]; do
    [[ -z "$GWAS_ID" ]] && continue
    GWAS_ID=$(echo "$GWAS_ID" | tr -d '[:space:]')
    
    CURRENT=$((CURRENT + 1))
    
    PARENT_DIR=$(get_parent_dir "$GWAS_ID")
    URL="$BASE_URL/$PARENT_DIR/$GWAS_ID/harmonised/"
    
    # Check if harmonised directory exists (HTTP 200)
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$URL")
    
    if [[ "$STATUS" == "200" ]]; then
        echo "[$CURRENT/$TOTAL] $GWAS_ID: YES"
        echo "$GWAS_ID" >> "$HAS_HARM"
    else
        echo "[$CURRENT/$TOTAL] $GWAS_ID: NO"
        echo "$GWAS_ID" >> "$NO_HARM"
    fi
    
done < "$INPUT_FILE"

# Summary
echo ""
echo "========== Summary =========="
HAS_COUNT=$(wc -l < "$HAS_HARM")
NO_COUNT=$(wc -l < "$NO_HARM")
echo "Has harmonised: $HAS_COUNT"
echo "No harmonised:  $NO_COUNT"
echo ""
echo "Results saved to:"
echo "  $HAS_HARM"
echo "  $NO_HARM"
