# Technical Architecture

TRIGGER: When an agent needs to understand the project structure, migration context, package boundaries, or how the system fits together. Read this before any structural changes.

## What Is This Project?

Willow Next is a ground-up rebuild of the Willow platform (source: \`../willow-vercel-migration\`). The goal is to create the ideal architecture for a fully AI-managed codebase where autonomous agents execute the migration and build new features full-time.

The current production codebase:
- Serves 5,000 DAU, doubling every 6 months
- Splits data between Firebase (Firestore + Auth + RTDB + Functions) and Supabase
- Uses Recoil (237 files), MUI, Express, and Vite
- Has 3,297 TypeScript files and 70 Supabase migrations

This rebuild consolidates everything into one clean stack. The old codebase runs in production until this one reaches feature parity on a per-domain basis.

## Monorepo Structure

\`\`\`
willow-next-migration/
├── packages/
│   ├── app/          # @willow/app - Next.js 15 App Router application
│   ├── db/           # @willow/db - Drizzle ORM schema, client, relations, migrations
│   ├── shared/       # @willow/shared - Zod schemas, enums, constants, validators
│   └── email/        # @willow/email - Postmark email client and templates
├── CLAUDE.md         # Agent instructions (kept concise)
├── ARCHITECTURE_DECISIONS.md  # Key structural decisions with rationale
├── turbo.json        # Turborepo build orchestration
├── biome.json        # Lint + format config
└── pnpm-workspace.yaml
\`\`\`

Managed with pnpm workspaces + Turborepo.

## Package Boundaries

### @willow/app (packages/app/)
The Next.js application. Contains all UI, routing, business logic services, auth, and API routes.

\`\`\`
src/
├── app/                    # Next.js App Router pages
│   ├── (auth)/             # Login, signup, Clever auth (route group)
│   ├── student/            # Student role pages (URL-visible segment)
│   ├── staff/              # Staff role pages (URL-visible segment)
│   ├── mentor/             # Mentor role pages (URL-visible segment)
│   ├── admin/              # Admin role pages (URL-visible segment)
│   └── api/                # Route handlers
│       ├── ai/             # AI chat and reflection endpoints
│       ├── auth/           # OAuth callbacks
│       ├── cron/           # Scheduled jobs (clever-sync, career-readiness, embeddings)
│       └── webhooks/       # External integrations (Clever, Common App, Sentry, Stripe)
├── components/
│   ├── layouts/            # Shell components (student-shell, staff-shell, sidebar, topbar)
│   ├── shared/             # Cross-feature components (error-boundary, loading, query-provider)
│   └── ui/                 # shadcn/ui primitives (button, card, input, etc.)
├── hooks/                  # Client-side hooks (use-active-role, use-current-user, use-realtime)
└── lib/
    ├── ai/                 # AI client, models, safety, tools
    ├── auth/               # Session, roles, permissions
    ├── services/           # Business logic (fat services, thin entry points)
    ├── storage/            # Supabase Storage file uploads
    ├── supabase/           # Supabase client helpers (admin, client, server, middleware)
    └── utils/              # Errors, logger, general utilities
\`\`\`

### @willow/db (packages/db/)
Drizzle ORM package. All database schema, relations, and the client.

\`\`\`
src/
├── client.ts         # Drizzle client instance (with schema + relations)
├── index.ts          # Barrel export
├── relations.ts      # Drizzle relational query definitions
├── migrate.ts        # Migration runner
├── schema/           # Table definitions (one file per domain)
│   ├── users.ts      # students, staff, mentors (@new - from Firestore)
│   ├── schools.ts    # schools, districts, classes (@new)
│   ├── curriculum.ts # courses, quests, lessons, progress (@new)
│   ├── colleges.ts   # colleges, programs (@existing - in Supabase)
│   ├── ai.ts         # conversations, messages, safety (@existing)
│   ├── feed.ts       # posts, polls, comments (@new)
│   ├── goals.ts      # goals, action items (@new)
│   └── ...           # 17 total domain schema files
└── seed/             # Seed data for development
\`\`\`

Tables marked \`@existing\` must match the real Supabase schema (use \`drizzle-kit introspect\`). Tables marked \`@new\` are migrating from Firestore and are defined fresh.

### @willow/shared (packages/shared/)
Shared types, validation schemas, enums, and constants. No runtime dependencies on app or db.

\`\`\`
src/
├── schemas/          # Zod validation schemas (API contract, not DB shape)
├── enums/            # Status enums, user roles, collections
├── constants/        # Grades, scoring rules
└── utils/            # Pure validators
\`\`\`

### @willow/email (packages/email/)
Isolated email package wrapping Postmark. Will grow to include template management, delivery tracking, and batch operations.

## Routing Architecture

Routes use URL-visible segments (not route groups) for role separation:
- \`/student/home\`, \`/student/feed\`, \`/student/settings\`
- \`/staff/dashboard\`, \`/staff/feed\`, \`/staff/students\`
- \`/mentor/dashboard\`, \`/mentor/mentee/[studentId]\`
- \`/admin/courses\`, \`/admin/ai-prompts\`

Auth routes use a route group \`(auth)\` since they share a layout but don't need a URL prefix.

Each role segment has its own \`layout.tsx\` (with the appropriate shell component) and \`error.tsx\` boundary.

## Data Flow

\`\`\`
Server Component  ->  Service (lib/services/)  ->  Drizzle (@willow/db)  ->  Supabase PostgreSQL
     |                      |
     |                Permission check (canAccess)
     |
Client Component  ->  Server Action (_actions.ts)  ->  Service  ->  Drizzle
     |
     └─> TanStack Query (only for polling, optimistic updates, infinite scroll)
\`\`\`

- Server Components fetch data by calling services directly
- Client Components mutate data via Server Actions
- API Route Handlers are only for external consumers (webhooks, cron, OAuth)
- Services contain all business logic and permission checks
- Drizzle is the only way to access the database

## AI Integration

- Vercel AI SDK with OpenAI + Anthropic providers
- Streaming chat via API route (\`/api/ai/chat\`)
- AI safety layer checks all student interactions
- Conversation types: alma (advisor), reflection, exit_ticket, durable_skills

## Key Design Principles

1. **Agent-first architecture** - Every pattern is documented in skills so autonomous agents can build features without human guidance
2. **Skills over bloated CLAUDE.md** - Detailed patterns live in \`.claude/skills/\` and auto-load contextually
3. **Fat services, thin entry points** - Business logic in services, not in pages or actions
4. **Server-first rendering** - Server Components by default, Client Components only for interactivity
5. **Single source of truth** - Drizzle schema for DB, Zod schemas for validation, enums for constants