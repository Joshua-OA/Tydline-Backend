# Execute Plan

TRIGGER: When a PLAN.md file exists and you're asked to "build it", "execute the plan", "implement this", or "start building". Also triggers when a \`docs/plans/\` folder contains phase files and you're asked to run a phase.

## The Rule

**You are the orchestrator. You do NOT write code. You dispatch sub-agents for ALL implementation work.** This preserves your context window across the entire plan — if you implement code directly, you'll lose track of earlier steps and introduce bugs by Step 5. Sub-agents get a fresh context for each step and exit when done.

If you catch yourself writing code, STOP. Dispatch a sub-agent instead.

## Execution Flow

### 1. Preparation

Read the plan file. Check what's already done (\`git log --oneline -20\`). Report to user: "Steps 1-3 are complete. Next: Step 4. Ready?"

### 2. For Each Step

**Read** the step from the plan yourself.

**Dispatch a sub-agent** with the prompt template below. One step per sub-agent, always.

**Verify** after the sub-agent finishes — lightweight only:
- \`npm run build\` passes
- \`npm run dev\` starts
- Do NOT read source files the sub-agent wrote

**Commit** progress:
\`\`\`
git add . && git commit -m "step N: brief description"
\`\`\`

**Report** to user: "Step N done. [1-line summary]. Build passes. Ready for Step N+1?"

### 3. When Something Breaks

Dispatch a new sub-agent with the error message and relevant file list. Never fix it yourself. If 2 fix attempts fail, escalate to the user.

Never skip a broken step.

### 4. When the Plan Changes

- **Small**: Include the change in the sub-agent prompt.
- **Medium**: Update the plan file first, then continue.
- **Big**: Stop. Tell the user. Go back to planning.

### 5. Multi-Phase Plans

One phase at a time. Re-read the plan at each phase start. Run full verification between phases.

### 6. Plan Complete

Run \`npm run build\` and \`npm run dev\`. Report: steps completed, summary, suggested next actions.

## Sub-Agent Prompt Template

\`\`\`
You are implementing Step [N] of the project plan: [step title].

## What to Build
[Step description from the plan]

## Tech Stack
[From plan header or CLAUDE.md]

## What Already Exists
[Key files from previous steps this step depends on — brief]

## Files to Create or Modify
[Explicit list from the plan]

## When You're Done
1. All files listed above are created
2. \`npm run build\` passes
3. \`npm run dev\` starts
4. Describe what you built and confirm verification passed
\`\`\`