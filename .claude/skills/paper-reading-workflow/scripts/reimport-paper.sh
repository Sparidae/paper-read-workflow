#!/usr/bin/env bash
# Force re-import a paper that was already processed.
# Usage: reimport-paper.sh <paper_dir_name> [--skip-llm] [--debug]
#
# Given a papers/<name> directory, extracts the original URL (if available),
# then runs paper-tool add --force to archive the old Notion page and re-import.

set -euo pipefail

PAPER_DIR_NAME="${1:?Usage: reimport-paper.sh <paper_dir_name> [--skip-llm] [--debug]}"
shift 2>/dev/null || true

# Find project root
PROJECT_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
PAPERS_DIR="$PROJECT_ROOT/papers"

# Resolve paper dir: can be a full path or a name under papers/
if [ -d "$PAPER_DIR_NAME" ]; then
    PAPER_DIR="$PAPER_DIR_NAME"
elif [ -d "$PAPERS_DIR/$PAPER_DIR_NAME" ]; then
    PAPER_DIR="$PAPERS_DIR/$PAPER_DIR_NAME"
else
    echo "ERROR: Paper directory not found: $PAPER_DIR_NAME"
    echo "Looked in: $PAPER_DIR_NAME and $PAPERS_DIR/$PAPER_DIR_NAME"
    exit 1
fi

PAPER_DIR="$(realpath "$PAPER_DIR")"
echo "=== Re-import: $PAPER_DIR ==="

# Try to find the URL from the arxiv ID in the directory name
DIR_BASENAME="$(basename "$PAPER_DIR")"
ARXIV_ID=$(echo "$DIR_BASENAME" | grep -oP '^\d{4}\.\d{4,5}')

if [ -n "$ARXIV_ID" ]; then
    URL="https://arxiv.org/abs/$ARXIV_ID"
    echo "Detected arxiv ID: $ARXIV_ID"
    echo "URL: $URL"
else
    echo "WARNING: Could not extract arxiv ID from directory name: $DIR_BASENAME"
    echo "Please provide the URL manually:"
    read -r URL
fi

echo ""
echo "Running: uv run paper-tool add '$URL' --force $*"
echo ""

cd "$PROJECT_ROOT"
exec uv run paper-tool add "$URL" --force "$@"
