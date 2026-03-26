# Compliance Check

TRIGGER: When asked to scan for accessibility, security, or compliance issues. Also runs before PRs, deployments, or when asked to "check for vulnerabilities", "audit security", or "check accessibility".

Scan changed files (vs main) for accessibility, data protection, and security violations. Static analysis — catches common mistakes, not exhaustive compliance.

## Pre-flight

\`\`\`bash
CHANGED_FILES=$(git diff main...HEAD --name-only -- '*.ts' '*.tsx' ':!*.test.ts' ':!*.test.tsx')
\`\`\`

No changed files → report "Nothing to scan" and exit.

## 1. Accessibility (WCAG 2.1 AA)

### Interactive elements without accessible names — BLOCKING

\`\`\`bash
grep -n '<button\|<Button' $CHANGED_TSX_FILES | grep -v 'aria-label\|aria-labelledby\|children\|>'
grep -n '<img\|<Image' $CHANGED_TSX_FILES | grep -v 'alt='
grep -n '<input\|<Input\|<textarea\|<Textarea\|<select\|<Select' $CHANGED_TSX_FILES | grep -v 'aria-label\|aria-labelledby\|id='
\`\`\`

### Non-semantic interactive patterns — BLOCKING

\`\`\`bash
grep -n 'div.*onClick\|span.*onClick' $CHANGED_TSX_FILES
grep -n 'role="button"' $CHANGED_TSX_FILES
\`\`\`

Use \`<button>\`, \`<a>\`, or native elements. Exception: wrapper divs delegating to child interactive elements.

### Focus and keyboard — WARNING

\`\`\`bash
grep -n 'tabIndex=["{]' $CHANGED_TSX_FILES | grep -v 'tabIndex={-1}\|tabIndex={0}\|tabIndex="-1"\|tabIndex="0"'
grep -n 'onMouseDown\|onMouseUp\|onMouseEnter\|onMouseLeave' $CHANGED_TSX_FILES | grep -v 'onKeyDown\|onKeyUp\|onFocus\|onBlur'
grep -n 'outline.*none\|outline.*0\|outline-none' $CHANGED_TSX_FILES
\`\`\`

## 2. Data Protection

### PII in client-facing output — BLOCKING

\`\`\`bash
grep -l '"use client"' $CHANGED_TSX_FILES | xargs grep -n 'console\.\(log\|warn\|error\)' 2>/dev/null
grep -n 'toast\|alert\|errorMessage\|Error(' $CHANGED_FILES | grep -i 'name\|email\|password\|phone\|address\|ssn'
\`\`\`

User-facing errors must be generic ("Something went wrong"). Never expose PII, internal IDs, or stack traces to the client.

### Unauthenticated data access — BLOCKING

\`\`\`bash
grep -n 'firstName\|lastName\|email\|phone\|password' $CHANGED_FILES | grep -i 'response\|return\|json\|NextResponse'
\`\`\`

Verify every endpoint returning user data calls an auth check first.

### Hard deletes on user data — BLOCKING

\`\`\`bash
grep -n '\.delete()\|DELETE FROM' $CHANGED_FILES | grep -i 'user\|account\|profile\|customer'
\`\`\`

Use soft delete (\`deletedAt\`) for user data to maintain audit trail.

## 3. Security

### Secrets and credentials — BLOCKING

\`\`\`bash
grep -rn 'password\s*=\|secret\s*=\|api_key\s*=\|apiKey\s*=\|token\s*=' $CHANGED_FILES | grep -v 'process\.env\|\.env\|schema\|type\|interface\|zod\|placeholder'
grep -rn 'sk-\|sk_live\|pk_live\|ghp_\|gho_\|xox[bpas]-' $CHANGED_FILES
\`\`\`

Secrets must come from environment variables, never hardcoded.

### SQL injection — BLOCKING

\`\`\`bash
grep -n 'query\s*(\s*\`\|execute\s*(\s*\`' $CHANGED_FILES | grep '\${'
grep -n 'raw\s*(\s*\`' $CHANGED_FILES | grep '\${'
\`\`\`

Never interpolate user input into SQL strings. Use parameterized queries or ORM methods.

### XSS vectors — BLOCKING

\`\`\`bash
grep -n 'dangerouslySetInnerHTML\|innerHTML\|__html' $CHANGED_TSX_FILES
grep -n 'eval(\|new Function(' $CHANGED_FILES
\`\`\`

Review every use. User-supplied content must be sanitized before rendering as HTML.

### Input validation at entry points — WARNING

\`\`\`bash
grep -l '"use server"\|export.*POST\|export.*PUT\|export.*PATCH\|export.*DELETE' $CHANGED_FILES | xargs grep -L 'parse\|safeParse\|schema\|validate' 2>/dev/null
\`\`\`

All user input should be validated at the server boundary (server actions, API routes).

### Rate limiting on mutation endpoints — WARNING

\`\`\`bash
grep -l 'export.*POST\|export.*PUT\|export.*PATCH\|export.*DELETE' $CHANGED_FILES | xargs grep -L 'rateLimit\|RATE_LIMIT\|rateLimiter' 2>/dev/null
\`\`\`

### Insecure dependencies and patterns — WARNING

\`\`\`bash
grep -n 'http://' $CHANGED_FILES | grep -v 'localhost\|127\.0\.0\.1\|http://schemas'
grep -n 'cors.*origin.*\*\|Access-Control-Allow-Origin.*\*' $CHANGED_FILES
grep -n 'rejectUnauthorized.*false\|NODE_TLS_REJECT_UNAUTHORIZED' $CHANGED_FILES
\`\`\`

### File upload validation — WARNING

\`\`\`bash
grep -n 'formData\|upload\|multipart\|file.*type' $CHANGED_FILES | grep -iv 'ALLOWED_.*TYPES\|validateFile\|MAX_.*SIZE'
\`\`\`

File uploads need server-side type and size validation.

### Auth bypass patterns — BLOCKING

\`\`\`bash
grep -n 'TODO.*auth\|FIXME.*auth\|HACK.*auth\|skip.*auth\|bypass.*auth' $CHANGED_FILES
grep -n '//.*disable.*auth\|// .*no.*auth.*check' $CHANGED_FILES
\`\`\`

## Report

Summary:

- **Accessibility**: X blocking / Y warnings
- **Data Protection**: X blocking / Y warnings
- **Security**: X blocking / Y warnings

Per finding: file path, line number, rule violated, BLOCKING or WARNING, suggested fix.

**BLOCKING = fix before shipping. WARNING = review and justify or fix.**