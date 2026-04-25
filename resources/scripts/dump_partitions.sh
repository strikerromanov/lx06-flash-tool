#!/bin/bash
# Dump all MTD partitions for backup
PARTITIONS="mtd0 mtd1 mtd2 mtd3 mtd4 mtd5 mtd6"
OUTPUT_DIR="${1:-./backups}"
mkdir -p "$OUTPUT_DIR"

for p in $PARTITIONS; do
    echo "Dumping $p..."
    update mread "$p" "$OUTPUT_DIR/$p.bin"
done

echo "All partitions dumped to $OUTPUT_DIR"
