# Commit

TRIGGER: When asked to "commit", "save changes", "commit this", or after completing a task that should be committed.

Review changes and create meaningful, well-formatted commits.

## Context

Gather before committing:
\`\`\`bash
git diff --name-status HEAD
git diff --stat HEAD
git diff HEAD
git diff --cached --name-only
git branch --show-current
git log --oneline --format="%s" -5
\`\`\`

## Process

1. **Analyze**: Review all changes. If they span multiple concerns, suggest splitting into separate commits and ask for confirmation.
2. **Safety check**: Scan for secrets, \`.env\` files, debug code (\`console.log\`, \`debugger\`), and unintended files. Do not commit these.
3. **Stage selectively**: Add files individually. Never use \`git add -A\` or \`git add .\` — unstaged files may be excluded intentionally.
4. **Write the message**: Match the style of recent commits on the branch. Use imperative mood ("add feature" not "added feature"). If the change is non-obvious, add a body explaining WHY after a blank line. Wrap body at 72 chars.
5. **Commit**: Execute using a HEREDOC for multi-line messages.

## Files to Never Commit

- \`.env*\` files
- Files containing hardcoded secrets, API keys, or tokens
- \`node_modules/\`, \`.next/\`, \`dist/\`, build artifacts

If these are the only changes, inform the user there's nothing to commit.