# TypeScript — Checked-In Toolchain Templates

The complete artifacts the T-standards in `../SKILL.md` require checked in. Copy them,
then deviate deliberately — a deviation is a review conversation, not a local preference.

## tsconfig.base.json (T1)

The base is typecheck-only (`noEmit`); nothing in it needs the compiler API, so
TypeScript 7.0 can serve as a fast CI checker while the checked-in 6.x drives lint.

```json
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "module": "nodenext",
    "moduleResolution": "nodenext",
    "target": "es2024",
    "lib": ["es2024"],
    "skipLibCheck": true,
    "noEmit": true
  }
}
```

Library variant — `tsconfig.lib.json`, the build config for published packages:

```json
{
  "extends": "./tsconfig.base.json",
  "compilerOptions": {
    "noEmit": false,
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true,
    "rootDir": "src",
    "outDir": "dist"
  },
  "include": ["src"]
}
```

App variant — the bundler emits, `tsc` only checks:

```json
{
  "extends": "./tsconfig.base.json",
  "compilerOptions": {
    "module": "esnext",
    "moduleResolution": "bundler",
    "lib": ["es2024", "dom", "dom.iterable"]
  },
  "include": ["src"]
}
```

Every other tsconfig extends the base; a flag weakened in a leaf config is config drift.

## eslint.config.js (T2, T9, T12)

Flat config. `recommendedTypeChecked` plus the promise and unsafe rules at error —
these are type-aware, so they consume the compiler API: keep the `typescript` dev
dependency on the 6.x line for lint until typescript-eslint runs on 7.1+.

```js
import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import eslintConfigPrettier from "eslint-config-prettier";

export default tseslint.config(
  eslint.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unsafe-argument": "error",
      "@typescript-eslint/no-unsafe-assignment": "error",
      "@typescript-eslint/no-unsafe-call": "error",
      "@typescript-eslint/no-unsafe-member-access": "error",
      "@typescript-eslint/no-unsafe-return": "error",
      "@typescript-eslint/ban-ts-comment": [
        "error",
        {
          "ts-expect-error": "allow-with-description",
          "ts-ignore": true,
          "ts-nocheck": true,
          "minimumDescriptionLength": 10,
        },
      ],
    },
  },
  eslintConfigPrettier,
);
```

Prettier interop: `eslintConfigPrettier` comes last, disabling every stylistic rule that
would fight the formatter; Prettier runs as its own command, never as an ESLint rule.

## package.json skeleton (T8, T14)

```json
{
  "name": "@org/example",
  "version": "0.1.0",
  "type": "module",
  "engines": {
    "node": ">=24"
  },
  "packageManager": "pnpm@11.0.0",
  "files": ["dist"],
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "import": "./dist/index.js"
    }
  },
  "sideEffects": false,
  "scripts": {
    "typecheck": "tsc --noEmit",
    "lint": "eslint .",
    "format": "prettier --check .",
    "test": "vitest run",
    "coverage": "vitest run --coverage",
    "build": "tsc -p tsconfig.lib.json"
  },
  "pnpm": {
    "onlyBuiltDependencies": ["better-sqlite3"]
  }
}
```

- `packageManager` is pinned exactly — pnpm self-manages from this field; Corepack is
  not involved (and is removed from Node 25+).
- `onlyBuiltDependencies` names only packages whose install scripts were vetted at the
  exact version in the lockfile; re-review the entry on every bump of that package.
- pnpm 11 applies `minimumReleaseAge: 1440` (one day) by default; on pnpm 10, set it
  explicitly in `pnpm-workspace.yaml`:

```yaml
minimumReleaseAge: 1440
```

## vitest.config.ts (T13)

`coverage.all` was removed in Vitest 4 — `include` is what defines the measured
surface, so untested files still count against the thresholds.

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      include: ["src/**"],
      thresholds: {
        statements: 80,
        branches: 75,
      },
    },
  },
});
```

Monorepo hint — one root config fanning out to per-package configs:

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    projects: ["packages/*"],
  },
});
```

## env.ts — the one place process.env is read (T6)

Fail-fast: the process refuses to start and the error lists every missing or invalid
variable at once, not just the first. Every other module imports `env`; a `process.env`
read anywhere else in the codebase is a T6 violation.

```ts
import { z } from "zod";

const EnvSchema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]),
  PORT: z.coerce.number().int().min(1).max(65535).default(3000),
  DATABASE_URL: z.url(),
  API_TOKEN: z.string().min(1),
});

export type Env = z.infer<typeof EnvSchema>;

const parsed = EnvSchema.safeParse(process.env);

if (!parsed.success) {
  const report = parsed.error.issues
    .map((issue) => `  - ${issue.path.join(".")}: ${issue.message}`)
    .join("\n");
  throw new Error(`Invalid environment:\n${report}`);
}

export const env: Env = parsed.data;
```

## Utility snippets (T3, T4, T10)

`assertNever` — the exhaustiveness backstop every closed switch ends with:

```ts
export function assertNever(value: never): never {
  throw new Error(`Unreachable case: ${JSON.stringify(value)}`);
}
```

`Brand` and a validating constructor — only the constructor may mint the brand, so
holding the type is the proof of validation:

```ts
declare const brand: unique symbol;

export type Brand<T, Name extends string> = T & { readonly [brand]: Name };

export type UserId = Brand<string, "UserId">;

export function toUserId(raw: string): UserId {
  if (!/^[0-9a-f]{24}$/i.test(raw)) {
    throw new Error(`Invalid user id: ${raw}`);
  }
  return raw as UserId;
}
```

Typed `Result` union with a narrowing helper — the boundary type for expected failures:

```ts
export type Result<T, E> = { ok: true; value: T } | { ok: false; error: E };

export function ok<T>(value: T): Result<T, never> {
  return { ok: true, value };
}

export function err<E>(error: E): Result<never, E> {
  return { ok: false, error };
}

export function unwrapOr<T, E>(result: Result<T, E>, fallback: T): T {
  return result.ok ? result.value : fallback;
}
```

## CI fragment (GitHub Actions)

`pnpm/action-setup` reads the pinned `packageManager` field, so it takes no version
input; the coverage script carries the thresholds, so a regression fails the job.

```yaml
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 24
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm typecheck
      - run: pnpm lint
      - run: pnpm coverage
      - uses: google/osv-scanner-action/osv-scanner-action@v2
        with:
          scan-args: --lockfile=pnpm-lock.yaml
```

## Anti-patterns in the toolchain itself

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| Config drift between tsconfig files | A leaf config quietly weakens a base flag; "clean typecheck" means different things per package | Every config extends the base; deltas add strictness or change module mode, never remove flags |
| Lint rules downgraded to warn | Warnings scroll past and are never read; the rule is effectively off while appearing on | Errors only — a rule not worth failing the build on is not worth carrying |
| Coverage thresholds deleted to make CI pass | The floor becomes whatever the worst recent commit left behind | Thresholds are ratchet-only; lowering one is a review conversation, not a fix |
| `packageManager` unpinned | Every machine resolves a different package-manager version; installs and lockfiles diverge | Pin the exact version; update it deliberately, in its own commit |
