# PR Check

TRIGGER: Before submitting a pull request, or when asked to "check before PR", "run PR check", "verify before merge", or "is this ready to ship".

Full verification before a PR. More thorough than commit-check — includes tests and compliance.

## Pre-flight

Verify you're in the project root (package.json exists). Detect the package manager from the lock file.

## Step 1: Build

Run the build. Fix ALL TypeScript errors. Re-run until zero errors. Do not proceed until the build passes.

## Step 2: Tests

Run the full test suite. If any tests fail, analyze the failure, fix it, and re-run. Do not proceed until all tests pass.

If the project has separate integration or e2e tests (check package.json scripts for \`test:integration\`, \`test:e2e\`, or similar), run those too.

## Step 3: Lint

Run the linter. Fix ALL errors, including pre-existing ones.

## Step 4: Security Scan

Scan changed files for common vulnerabilities:

\`\`\`bash
CHANGED=$(git diff main...HEAD --name-only -- '*.ts' '*.tsx' ':!*.test.ts')
[ -z "$CHANGED" ] && echo "No changed files" && skip

# Hardcoded secrets
echo "$CHANGED" | xargs grep -n 'sk-\|sk_live\|pk_live\|ghp_\|apiKey\s*=' 2>/dev/null

# Type safety suppressions
echo "$CHANGED" | xargs grep -n 'as any\|as never\|@ts-ignore' 2>/dev/null

# XSS vectors
echo "$CHANGED" | xargs grep -n 'dangerouslySetInnerHTML\|innerHTML\|eval(' 2>/dev/null

# Unvalidated input at server boundaries
git diff main...HEAD --name-only -- '*.ts' | xargs grep -l '"use server"\|export.*POST\|export.*PUT' 2>/dev/null | xargs grep -L 'parse\|safeParse\|validate' 2>/dev/null
\`\`\`

Every hit must be reviewed. Hardcoded secrets and \`as any\` are blocking. XSS and missing validation require justification or a fix.

## Step 5: Compliance Check

If the project has a \`/compliance-check\` skill, run it. Otherwise, spot-check changed \`.tsx\` files for basic accessibility:

\`\`\`bash
echo "$CHANGED" | xargs grep -n '<img\|<Image' 2>/dev/null | grep -v 'alt='
echo "$CHANGED" | xargs grep -n 'div.*onClick\|span.*onClick' 2>/dev/null
\`\`\`

Fix blocking issues before proceeding.

## Report

\`\`\`
✅ Build: Success / Failed
✅ Tests: X passed / Y failed
✅ Lint: Clean / Fixed N issues
✅ Security: Clean / N findings
✅ Accessibility: Clean / N findings
\`\`\`

If anything can't be fixed, explain why and get user approval before proceeding.

## Rules

- Each step must pass before the next
- Fix ALL errors, including pre-existing ones
- Do not skip steps