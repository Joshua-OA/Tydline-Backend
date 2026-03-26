# Planning Skill

TRIGGER: When starting a new feature, page, project, or any non-trivial task. If the work touches more than one file or takes more than a few minutes to think through, plan first.

## The Rule

**Never start coding without a plan.** You are the architect. Claude Code is the builder. A builder without a blueprint makes a mess. A builder with a clear blueprint moves fast and gets it right.

## How to Plan

### For small tasks (a new page, a component, a simple feature):

Create a plan in the chat. Before writing any code:

1. State what you're building and why
2. List the files you'll create or modify
3. Describe the expected behavior when it's done
4. Identify anything you're unsure about — ask questions now, not after writing 200 lines of code

Example prompt:
\`\`\`
I need to add a pricing page to my site. Before writing any code, plan this out:
- What files need to be created or changed?
- What components do I need?
- What data does this page need (pricing tiers, features, etc)?
- What should the page look like structurally?

Don't write code yet. Just give me the plan.
\`\`\`

Review the plan. Push back on anything that doesn't make sense. Then say: "Looks good. Build it."

### For medium tasks (a feature with multiple parts, a form with backend logic, an API integration):

Write the plan to a file. This creates a reference that survives across sessions.

1. Ask Claude Code to create a \`PLAN.md\` in your project root (or a \`docs/plans/\` folder)
2. The plan should include:
   - **Goal**: One sentence on what this feature does and who it's for
   - **Files**: Every file that will be created or modified, with a one-line description of what each does
   - **Steps**: A numbered sequence of implementation steps, ordered so each step produces something testable
   - **Dependencies**: Any packages to install, accounts to create, API keys needed
   - **Open questions**: Things you need to decide before building (flag these instead of guessing)

Example prompt:
\`\`\`
I want to add a contact form that collects name, email, and message, saves submissions to localStorage, and shows a success state after submitting.

Create a PLAN.md for this feature. Include the goal, every file involved, implementation steps in order, and any open questions. Each step should produce something I can see working. Don't write any code yet.
\`\`\`

3. Review the plan, make changes, then tell Claude Code to execute it step by step

### For large tasks (a new product, a major feature, a system redesign):

Break the work into phases, and plan each phase separately.

1. Start with a high-level plan that breaks the project into phases
2. Each phase gets its own plan file (e.g., \`docs/plans/phase-1-mvp.md\`, \`docs/plans/phase-2-users.md\`)
3. Each phase plan follows the medium task structure above
4. Only plan one phase in detail at a time — the later phases will change as you learn

Example prompt:
\`\`\`
I'm building a lead generation site with a landing page, a waitlist form that saves to a database, and an admin dashboard to view signups.

Break this into phases. Phase 1 should be the smallest thing I can ship that's useful. Write a high-level plan with phases, then create a detailed PLAN.md for Phase 1 only. Don't write any code yet.
\`\`\`

## Plan File Format

Keep plans simple. Here's the structure:

\`\`\`markdown
# Plan: [Feature Name]

## Goal
One sentence. What does this do and who is it for?

## Files
- \`src/app/pricing/page.tsx\` — Pricing page (server component, fetches tier data)
- \`src/app/pricing/pricing-card.tsx\` — Individual pricing tier card (client component)
- \`src/lib/pricing-data.ts\` — Pricing tier definitions (static data)

## Steps
1. Create pricing data file with tier definitions
2. Create the pricing card component
3. Create the pricing page that renders the cards
4. Add pricing link to the navigation
5. Test on mobile, adjust responsive layout

## Dependencies
- None (uses existing project setup)

## Open Questions
- Do we want monthly and annual toggle? (Deciding to skip for v1, can add later)
\`\`\`

## Key Principles

**Each step should produce something you can see or test.** Don't plan five invisible backend steps before anything shows up in the browser. Interleave visible progress so you can catch problems early.

**Plans are not contracts.** They will change as you build. That's fine. The point is to think before you build, not to predict the future. If something changes, update the plan or just keep going.

**Smaller plans are better plans.** A 50-line plan you actually follow beats a 500-line plan you ignore. If your plan is getting long, you're trying to build too much at once. Break it into phases.

**Name your open questions.** The worst bugs come from assumptions nobody stated. If you're not sure about something — how auth should work, what data format to use, whether a feature is needed — write it down as an open question and decide before building.

**Tell Claude Code not to code.** Explicitly. If you don't say "don't write code yet," Claude Code will start building immediately. The phrase "don't write any code yet, just give me the plan" is the most important sentence in your workflow.