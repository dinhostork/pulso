# Backend module boundaries

Pulso has one backend codebase and one primary PostgreSQL database. Domain
modules express logical ownership inside that modular monolith; they are not
independent deployable services. This maps
[ADR-0001](../adr/0001-modular-monolith-with-workers.md) into the initial code layout.

## Current packages

| Path | Responsibility |
| --- | --- |
| `backend/config/` | Environment validation, Django settings, URL composition and ASGI/WSGI entry points |
| `backend/api/` | HTTP route registry; currently has no endpoints |
| `backend/accounts/` | Account identity, Django model integration and initial migration |
| `backend/database/` | Shared PostgreSQL extension migration; no product models |

Accounts uses Django's `AbstractUser` and a database-generated `BigAutoField`
primary key. Future relationships use `settings.AUTH_USER_MODEL` in model fields
and `get_user_model()` at runtime. Username/password and permissions retain
Django behavior. No profile, position or API authentication contract is introduced.

There is no application use case to implement yet, so no empty application,
port or service packages are created. Add module-local application services when
real use cases arrive. The route registry is the integration point for future
HTTP adapters; it must not accumulate domain rules.

## Dependency direction

```text
HTTP / worker adapters → application use cases → domain rules / ports
                                               ↑
                                    infrastructure implements ports
```

HTTP adapters parse requests and translate results. Application services
coordinate use cases and transactions. Domain rules remain independent of DRF,
Celery, Redis and provider SDKs. Infrastructure supplies persistence and external
integrations through ports when the use case needs that separation.

The current Account model is a Django persistence/authentication integration;
it is not a framework-independent domain layer. Future product rules should not
be placed in serializers, views, tasks or settings merely because those files
are convenient. Do not add wrapper repositories around Django without a concrete
boundary to protect. Cross-module writes go through the owner's application
interface rather than another module's internal models.

## Logical ownership

| Module | Owns | Current state |
| --- | --- | --- |
| Accounts | Stable user identity and account authentication foundation | Minimal user model exists; API authentication is deferred to issue #6 |
| News | Source publications, Articles, Stories and source-grounded factual synthesis | Planned; no package or models |
| Opinion | Human Opinions, declared positions, derived Perspectives and Pulse | Planned; no package or models |
| Recommendation | Discovery ranking, interests and ranking signals | Planned; no package or models |
| Moderation | Moderation decisions and eligibility policies, coordinated with content owners | Planned; policies and interfaces remain undecided |
| Notifications | Notification delivery coordination and provider integration | Planned; no package or models |

These ownership boundaries preserve the accepted decisions:

- [ADR-0002](../adr/0002-postgresql-pgvector.md): PostgreSQL is the shared primary
  datastore; pgvector will live alongside relational data. Sharing a database
  does not transfer table ownership. The root Compose stack provides local
  PostgreSQL/pgvector; the `database` infrastructure app enables `vector`
  through a Django migration.
- [ADR-0003](../adr/0003-article-not-equal-story.md): News keeps Articles distinct
  from Stories and preserves source provenance.
- [ADR-0004](../adr/0004-ai-is-not-a-source.md): generated output is derived;
  Articles and human Opinions remain authoritative inputs. Provider integrations
  must not become sources of factual truth.
- [ADR-0006](../adr/0006-opinion-not-equal-perspective.md): Opinion owns the
  distinction between human-authored Opinions and derived Perspectives.
- [ADR-0007](../adr/0007-pulse-counts-unique-users.md): stable account references
  support one active position per user per Story later. Account identity alone
  does not implement positions, unique-person verification or Pulse counting.
- [ADR-0008](../adr/0008-recommend-stories-not-truth.md): Recommendation consumes
  domain data to rank discovery; it cannot rewrite Story facts, source evidence,
  Perspectives or Pulse state.

## Background execution

Under [ADR-0005](../adr/0005-asynchronous-processing-with-celery.md), future API,
worker and scheduler processes share the same backend codebase. Task adapters
invoke application services, dispatch after successful database commit where
needed, and account for repeat execution. Redis and task-result metadata are not
authoritative domain storage. Worker infrastructure belongs to issue #4 and is
not implemented by this bootstrap.
