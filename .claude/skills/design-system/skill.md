# Design System

TRIGGER: When creating or modifying UI components, pages, or layouts. Any work in component files, page files, or style files.

Enforce the project's design system. All UI work uses existing components and tokens. No hardcoded values. No one-off styles.

## Rules

**Use components from the UI kit package.** Before creating any new component, check what exists in the UI kit (\`packages/ui/\` or equivalent). If a component exists that does what you need, use it. If it needs a variant, extend it in the UI kit — do not create a one-off version in the app.

**Never hardcode visual values.** This applies to:
- Colors → use design tokens (\`var(--color-primary)\`, \`text-primary\`, etc.)
- Spacing → use the spacing scale (\`gap-4\`, \`p-6\`, etc.)
- Font sizes → use the type scale (\`text-sm\`, \`text-lg\`, etc.)
- Font weights → use the weight tokens (\`font-medium\`, \`font-semibold\`, etc.)
- Border radius → use the radius tokens (\`rounded-md\`, \`rounded-lg\`, etc.)
- Shadows → use the shadow tokens (\`shadow-sm\`, \`shadow-md\`, etc.)
- Breakpoints → use the defined breakpoints, never arbitrary pixel values

If you write a hex code, rgb value, pixel value for spacing, or arbitrary Tailwind value (\`text-[14px]\`, \`bg-[#3B82F6]\`, \`p-[13px]\`), you are violating this skill.

**Compose, don't create.** Build pages by composing UI kit components. A page should be mostly layout and composition with very little custom styling. If a page requires significant custom CSS, that's a signal that the UI kit is missing a component — create it in the UI kit, not in the page.

## Before Writing UI Code

1. **Read the UI kit.** List available components: \`ls packages/ui/src/components/\` (or equivalent path). Note what exists.
2. **Read the design tokens.** Find the token/theme file (e.g., \`tailwind.config.ts\`, \`globals.css\`, \`theme.ts\`) and note available colors, spacing, typography, and other tokens.
3. **Plan the composition.** For each section of the UI, identify which existing components to use. Flag any gaps where a new UI kit component is needed.

## When a Component Doesn't Exist

Do not build it inline in the page. Instead:

1. Create it in the UI kit package
2. Follow existing component patterns (props interface, naming, file structure)
3. Use only design tokens for all visual values
4. Export it from the UI kit barrel file
5. Import and use it in the page

## Verification

After writing UI code, scan for violations:

\`\`\`bash
# Hardcoded colors
grep -rn '#[0-9a-fA-F]\{3,8\}\|rgb(\|rgba(\|hsl(' $CHANGED_TSX_FILES 2>/dev/null

# Arbitrary Tailwind values (square bracket syntax)
grep -rn '\[\d\+px\]\|\[#.\+\]\|\[\d\+rem\]' $CHANGED_TSX_FILES 2>/dev/null

# Inline styles
grep -rn 'style={{' $CHANGED_TSX_FILES 2>/dev/null
\`\`\`

Every hit is a violation. Replace with the appropriate design token or UI kit component.