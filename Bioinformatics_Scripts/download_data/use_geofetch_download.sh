#!/bin/bash
# 用法: bash batch_geo_supplementary.sh gse_list.txt
# 下载每个 GSE 的 Supplementary 文件 + meta 文件（.soft、.xml 等）

set -e

if [ $# -ne 1 ]; then
    echo "Usage: $0 <gse_list.txt>"
    exit 1
fi

input_file="$1"

while read -r gse; do
    [ -z "$gse" ] && continue
    echo "[INFO] Processing $gse ..."

    mkdir -p "${gse}/meta_data" "${gse}/data"

    prefix="${gse:0:-3}nnn"

    # Supplementary 文件下载
    ftp_base="ftp://ftp.ncbi.nlm.nih.gov/geo/series/${prefix}/${gse}/suppl/"
    echo "[INFO] Fetching supplementary from: ${ftp_base}"
    wget -r -nH -nd -np --continue --show-progress -P "${gse}/data" "${ftp_base}" || \
        echo "[WARN] Failed to download supplementary files for ${gse}"

    # Meta 文件下载
    meta_base="ftp://ftp.ncbi.nlm.nih.gov/geo/series/${prefix}/${gse}/"
    echo "[INFO] Fetching meta from: ${meta_base}"
    wget -r -nH -nd -np --continue --show-progress -A "*.soft,*.xml,*.txt,*.gz" -P "${gse}/meta_data" "${meta_base}" || \
        echo "[WARN] Failed to download metadata for ${gse}"

    echo "[INFO] Finished ${gse}"
done < "$input_file"

echo "[INFO] All GEO supplementary and meta files downloaded successfully."
