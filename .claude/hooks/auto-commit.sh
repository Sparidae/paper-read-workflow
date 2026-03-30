#!/bin/bash
# Stop hook: 任务结束前检测未提交变更，有则拦截并提示执行 /commit

INPUT=$(cat)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')

# 防止无限循环：commit 完成后再次触发时直接放行
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0

# 检查是否有未提交变更（已修改、已暂存、未跟踪）
if git diff --quiet 2>/dev/null && \
   git diff --cached --quiet 2>/dev/null && \
   [ -z "$(git ls-files --others --exclude-standard 2>/dev/null)" ]; then
  exit 0
fi

# 有变更，阻止结束，提示提交
cat <<'EOF'
{"decision": "block", "reason": "检测到未提交的变更，请调用 /commit 技能提交更新。"}
EOF
