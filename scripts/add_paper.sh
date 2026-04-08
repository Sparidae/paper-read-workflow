#!/bin/bash
# ============================================================
# add_paper.sh - 在本地电脑上调用服务器 paper-tool 的便捷脚本
#
# 用法:
#   ./add_paper.sh "https://arxiv.org/abs/2301.00001"
#   ./add_paper.sh --skip-llm "https://arxiv.org/abs/2301.00001"
#   ./add_paper.sh --force "https://arxiv.org/abs/2301.00001"
#
# 推荐做法：在本地 ~/.bashrc 或 ~/.zshrc 中添加以下 alias：
#   alias add-paper='~/scripts/add_paper.sh'
#   然后直接: add-paper "https://arxiv.org/abs/2301.00001"
# ============================================================

# ── 配置项（修改为你的服务器信息） ────────────────────────
SERVER="your-server"                  # SSH 配置名（~/.ssh/config 中的 Host）或 user@host
PROJECT_DIR="~/projects/paper_list"  # 服务器上 paper-tool 项目的路径
# ────────────────────────────────────────────────────────────

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 [--skip-llm] [--force] <arxiv-or-openreview-url>"
    echo ""
    echo "Examples:"
    echo "  $0 'https://arxiv.org/abs/2301.00001'"
    echo "  $0 'https://openreview.net/forum?id=XXXXX'"
    echo "  $0 --skip-llm 'https://arxiv.org/abs/2301.00001'"
    echo "  $0 --force 'https://arxiv.org/abs/2301.00001'"
    exit 1
fi

# Parse optional flags
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-llm|--force)
            EXTRA_ARGS+=("$1")
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Unsupported option: $1"
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -lt 1 ]]; then
    echo "Missing URL."
    exit 1
fi

URL="$1"
EXTRA_ARGS_STR="${EXTRA_ARGS[*]}"

echo "→ 连接服务器 $SERVER ..."
ssh -t "$SERVER" "cd $PROJECT_DIR && uv run paper-tool add $EXTRA_ARGS_STR '$URL'"
