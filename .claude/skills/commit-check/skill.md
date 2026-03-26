# Commit Check

TRIGGER: Before committing code, or when asked to "check before commit", "run pre-commit", or "verify the build".

Fast pre-commit verification. Catches errors before they enter the codebase.

## Pre-flight

Verify you're in the project root (package.json exists). Detect the package manager from the lock file:
- \`pnpm-lock.yaml\` → use \`pnpm\`
- \`package-lock.json\` → use \`npm\`
- \`yarn.lock\` → use \`yarn\`

## 1. Build

Run the build command (e.g., \`npm run build\`). If TypeScript errors exist, fix ALL of them and re-run until zero errors. Do not proceed until the build passes.

## 2. Lint

Run the lint command (e.g., \`npm run lint\`). Fix ALL errors — including pre-existing ones, not just yours. If the project has an auto-fix command (e.g., \`npm run lint:fix\`), run that first.

## 3. Type Safety Scan — BLOCKING

\`\`\`bash
CHANGED=$(git diff --cached --name-only | grep -E '\.(ts|tsx)$')
[ -z "$CHANGED" ] && echo "No staged TS files" && exit 0
echo "$CHANGED" | xargs grep -n ': any\b' 2>/dev/null
echo "$CHANGED" | xargs grep -n '@ts-ignore\|@ts-expect-error' 2>/dev/null
\`\`\`

\`any\` types and suppression comments hide real bugs. Fix the underlying type error.

## Report

\`\`\`
✅ Build: Success / Failed
✅ Lint: Clean / Fixed N issues
✅ Type safety: Clean / N violations
\`\`\`

If something can't be fixed, explain why — don't silently skip it.

## Rules

- Fix ALL errors, including pre-existing ones
- Each step must pass before moving to the next
- Do not skip stepss