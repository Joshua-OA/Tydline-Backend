# Submit PR

TRIGGER: When asked to "submit PR", "create PR", "open a pull request", "push and create PR", or after completing a feature that's ready for review.

Push branch and create a pull request via GitHub CLI.

## Context

Gather before creating:
\`\`\`bash
git branch --show-current
git log main..HEAD --oneline
git diff main...HEAD --name-status
git diff main...HEAD --stat
\`\`\`

## Process

1. **Push** the branch: \`git push -u origin $(git branch --show-current)\`
2. **Create the PR** using \`gh pr create\`
3. **Report** the PR URL to the user

## PR Format

**Title**: Concise, imperative mood, 50-72 chars. ("Add pricing page" not "Added pricing page")

**Body** using HEREDOC:

\`\`\`bash
gh pr create --title "Title here" --body "$(cat <<'EOF'
## Summary
1-3 sentences: what this does and why.

## Changes
- Key changes, organized logically
- Breaking changes or important notes highlighted

## Testing
- How it was tested
- New tests added, if any
EOF
)"
\`\`\`

Add a **Technical Details** section only if the PR involves non-obvious architectural decisions or trade-offs. Skip it for straightforward changes.