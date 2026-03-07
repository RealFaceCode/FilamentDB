# FilamentDB Copilot Instructions

## 1) Purpose and Scope
- This file defines practical engineering rules for contributors and AI agents working in FilamentDB.
- Keep changes high-signal, safe, and operationally consistent with current project conventions.
- Prefer concise, actionable implementation over long explanations.
- Do not duplicate runbooks or handbook-level detail; link to canonical docs when needed.

## 2) Mandatory Operating Model
- Runtime is Docker/Compose-first and PostgreSQL-first.
- All runtime services must run via Docker Compose.
- Allowed exception: end-user helper wrapper scripts (for example `.cmd` and `.ps1`) that trigger Docker workflows.
- Wrapper scripts must not introduce an alternative non-Docker service runtime path.
- Use Compose service host `db` for database connections inside containers.
- Assume Linux-friendly command syntax unless explicitly targeting Windows helper scripts.

## 3) Post-Change Mandatory Commands
- Strict trigger: after every repository code change, run:

```bash
docker compose --profile slot-poller up -d --build
```

- Exception: for documentation-only changes (for example `README.md`, `*.md`, comments-only docs), a full rebuild/restart is not required.
- If there is any uncertainty whether runtime behavior is affected, treat the change as code-impacting and run the full rebuild/restart.

- If a proxy profile is part of the runtime, include it in the same rebuild/restart command so the full active stack is rebuilt and restarted together.
- Example for LAN runtime:

```bash
docker compose --profile https-lan --profile slot-poller up -d --build
```

- If a change affects database behavior or schema, also run:

```bash
docker compose exec web alembic upgrade head
```

- Typical DB-impacting changes include:
- Files under `alembic/versions/`
- ORM model changes in `app/models.py`
- DB initialization, metadata, or migration-related logic

## 4) Core Command Reference
- Start or refresh the full active stack (including `slot-poller`):

```bash
docker compose --profile slot-poller up -d --build
```

- Start full active stack with LAN proxy:

```bash
docker compose --profile https-lan --profile slot-poller up -d --build
```

- Run migrations:

```bash
docker compose exec web alembic upgrade head
```

- Check service state:

```bash
docker compose ps
```

- Check web and db logs:

```bash
docker compose logs --tail=100 web
docker compose logs --tail=100 db
```

- Verify app health:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

- Run targeted tests in container:

```bash
docker compose exec -e PYTHONPATH=/app web pytest -q tests/test_supplies_page.py
```

## 5) Implementation Principles
- Prefer minimal diffs that solve the requested problem completely.
- Preserve existing architecture and naming patterns unless a change request says otherwise.
- Keep API, template, and model changes coherent across layers.
- Avoid speculative refactors while implementing focused tasks.
- Add comments only where logic is not self-evident.
- Every code change must be tested thoroughly, including realistic edge cases and failure paths.
- Do not consider a change complete without executing and validating relevant tests.

## 5.1) Test Minimum Matrix (Mandatory)
- Unit tests: verify changed business logic paths and at least one negative/failure path.
- API tests: verify success response, validation error response, and contract-critical fields.
- UI/Template tests: verify changed state rendering, responsive behavior, and dark-mode parity where applicable.
- Data/DB tests: verify migration/model impact, constraints, and rollback-safe behavior for DB-affecting changes.
- Edge cases: include boundary values, empty/null input handling, and stale/missing data behavior.
- Regression check: run at least one targeted existing test around the changed feature area.
- Completion gate: no change is complete until this matrix is covered for relevant layers.

## 6) UI and UX Design Rules
- Preserve the existing visual language in `app/templates/base.html` and related templates.
- Keep status colors semantic and consistent:
- `emerald` for healthy/success/in-use
- `amber` for warning/stale/low
- `rose` or `red` for error/mismatch/empty
- `slate` for neutral/unknown
- Use mobile-first responsive structure (`grid-cols-1` baseline, then `md/lg/xl` expansions).
- Maintain dark-mode parity for all new UI states and components.
- Keep forms consistent:
- labels above fields
- clear focus styles
- explicit error styles for invalid input
- Keep data-dense tables usable with scroll container and sticky headers where applicable.
- Keep dialog sizing predictable and consistent (small/medium/large patterns).
- Keep interaction feedback explicit (hover, focus, disabled, loading).
- Preserve current page state across reload and soft reload (for example filters, expanded sections, open dialogs, selected tabs, and in-page context where feasible).
- Soft-refresh handlers must be idempotent and must not leave stale event listeners behind; re-initialization logic must clean up or replace prior bindings (for example via `AbortController`, explicit unbind, or guard patterns).

## 7) API Design Rules
- Keep routes resource-oriented and naming consistent with existing FastAPI style.
- Validate inputs explicitly and return predictable error structures.
- Maintain response contract stability; avoid accidental breaking changes.
- For intentional breaking changes, provide migration notes and update dependent paths.
- Do not embed UI-only presentation assumptions into API payload contracts.
- Keep business logic on the server side, not in template-side ad hoc transformations.

## 8) Database and Migration Rules
- Evolve schema only through Alembic migrations.
- Do not patch production schema manually outside migration workflow.
- Keep migration scripts deterministic and reversible where practical.
- Use explicit constraints and indexes when they encode integrity or performance expectations.
- Keep naming aligned with existing model and migration conventions.
- Ensure DB-related code and migration state remain synchronized before merge.

## 9) Do and Don’t Checklist
- Do run full-stack Compose rebuild/start after each code change (include `slot-poller` and active proxy profile).
- Do skip full rebuild only for documentation-only changes.
- Do test every code change thoroughly, including edge cases and negative paths.
- Do run `alembic upgrade head` for DB-impacting work.
- Do verify health and logs after operational changes.
- Do keep changes small, tested, and aligned with existing structure.
- Don’t bypass migrations with ad hoc SQL hotfixes.
- Don’t add parallel runtime paths for core services outside Docker.
- Don’t copy large sections from README/runbooks into feature PRs.
- Don’t ship UI changes without responsive and dark-mode checks.

## 10) Canonical References
- Project overview and operations: `README.md`
- Database function handbook: `DB_FUNKTIONSHANDBUCH.md`
- Deployment checklist: `deploy/GO_LIVE_CHECKLIST.md`
- Rollback procedures: `deploy/ROLLBACK_RUNBOOK.md`
- Slicer hooks: `slicer_hooks/README.md`
- Local services bridge: `local_services/README.md`
