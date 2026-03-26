# Test-Driven Development

TRIGGER: When building new features, fixing bugs, or when asked to "write tests", "add tests", or "TDD this".

Write tests first. Tests define what the code should do. Implementation makes them pass. If all tests pass and the feature is broken, the tests are wrong.

## Workflow: RED → GREEN → REFACTOR

1. **RED**: Write ONE failing test for the next piece of behavior. Run it. Confirm it fails for the right reason (missing function, wrong return value — not a typo).
2. **GREEN**: Write the minimum code to make that test pass. Nothing more.
3. **REFACTOR**: Clean up if needed. Tests must stay green.
4. **Repeat** until the feature is complete.

## What to Mock vs What to Keep Real

**Never mock the logic under test.** Mock only at external boundaries.

### Never mock:
- **Validation schemas** (Zod, Yup, etc.) — mocking validation hides type mismatches and missing fields
- **Authorization/permission checks** — mocking auth hides broken access control, the #1 source of security bugs
- **Query builders and operators** (Drizzle \`eq\`/\`and\`/\`inArray\`, Prisma filters, etc.) — mocking these hides malformed queries
- **Your own functions** — never mock one service to test another. Test the real call chain.

### OK to mock:
- **Database transport** — the execution layer that hits the actual database. Return realistic row shapes.
- **Session/auth user** — return a well-formed user fixture
- **External APIs** — third-party services, email, payment, storage
- **Cache invalidation** — \`revalidatePath\`, \`revalidateTag\`, etc.
- **Non-deterministic values** — \`crypto.randomUUID()\`, \`Date.now()\` when determinism matters

### The key distinction:

Mock the **boundary** (database connection, HTTP client), not the **logic** (permission rules, validation, query construction). If your mock makes the test pass regardless of whether the logic is correct, the mock is hiding bugs.

## Required Test Coverage

For every feature, cover:

1. **Authorization** — at least one allowed role AND one denied role
2. **Happy path** — realistic inputs, check every field of the output
3. **Validation** — invalid input rejected using real schemas
4. **Edge cases** — null/undefined optional fields, empty arrays, empty strings, missing related records, boundary values
5. **Error paths** — not found, forbidden, conflict/duplicate, invalid state

## Test File Structure

\`\`\`
src/
  lib/
    services/
      pricing-service.ts
      pricing-service.test.ts    ← adjacent to source
  app/
    pricing/
      _actions.ts
      _actions.test.ts           ← adjacent to source
  test/
    fixtures.ts                  ← shared user/data fixtures
\`\`\`

Keep test files adjacent to source files. Create shared fixtures for common data shapes (users, records) — never inline the same object across multiple test files.

## Anti-Patterns

**Mock choreography**: Testing that a mock was called with certain args instead of testing that the logic produces correct results. If the test passes regardless of what the real function would return, it's testing mocks, not code.

**Mocking validation as passthrough**: \`vi.mocked(schema.parse).mockReturnValue(input)\` passes even if the schema would reject the input.

**Happy-path-only coverage**: Five tests that all verify success. Zero tests for denied access, invalid input, or missing records. These are where production bugs live.

**Type suppression in tests**: \`as any\` or \`as never\` to silence type errors hides mismatches between what the test provides and what the code expects. If the type doesn't fit, the test data is wrong — fix it.

## Completeness Checklist

A feature is test-complete when:

- [ ] Every authorization role tested (allow AND deny)
- [ ] Happy path for each public function
- [ ] Validation with real schemas
- [ ] Edge cases: nulls, empty arrays, missing records
- [ ] Error paths: not found, forbidden, validation failure
- [ ] No mocks on validation, auth, or query logic
- [ ] No \`as any\` / \`as never\` in test code
- [ ] All tests pass
- [ ] Build passes