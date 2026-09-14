# ADR-0001 — Modular Monolith with Background Workers

- **Status:** Accepted
- **Date:** 2026-09-14
- **Decision owners:** Pulso maintainers

## Context

Pulso contains multiple areas with distinct responsibilities:

- news ingestion and normalization;
- article deduplication;
- Story clustering;
- NLP and AI processing;
- public opinion processing;
- Perspective generation;
- recommendation;
- moderation;
- notifications.

Some workloads are request-driven, while others are computationally expensive, asynchronous or scheduled.

Examples include:

- polling RSS feeds and external APIs;
- generating embeddings;
- semantic similarity calculations;
- clustering Articles into Stories;
- clustering Opinions into Perspectives;
- generating source-grounded summaries;
- rebuilding Perspectives;
- recalculating the Pulse;
- updating recommendation signals.

The system therefore needs clear domain boundaries and asynchronous processing without introducing unnecessary distributed-system complexity during its initial development.

Pulso is currently a new product with:

- a small development team;
- no demonstrated need for independent service scaling;
- no established production traffic patterns;
- a free-first infrastructure requirement;
- a strong requirement for local development and reproducibility.

Starting with microservices would introduce concerns such as service discovery, distributed tracing, network failure handling, multiple deployments, inter-service authentication and distributed data consistency before the product has demonstrated a concrete need for them.

At the same time, executing all workloads synchronously inside HTTP requests would make expensive or long-running operations directly affect API latency and reliability.

---

## Decision

Pulso will initially be implemented as a:

> **Modular Monolith with Background Workers**

The backend will remain a single application codebase with explicit module boundaries.

The main domain capabilities remain logical components inside the monolith:

- News Engine;
- Opinion Engine;
- Recommendation Engine;
- Accounts;
- Moderation;
- Notifications.

The engines are **not independent microservices**.

They are logical domain components that share the same application codebase and primary persistence infrastructure.

---

## Runtime model

The application runs through multiple runtime processes while remaining part of the same application architecture.

```mermaid
flowchart TB
    subgraph Backend["Pulso Backend — single application codebase"]

        subgraph Runtime["Runtime Processes"]
            API["Django / DRF<br/>API Runtime"]
            Worker["Celery<br/>Worker Runtime"]
            Scheduler["Celery Beat<br/>Scheduler"]
        end

        subgraph Modules["Application Modules"]
            Accounts["Accounts"]
            News["News Engine"]
            Opinion["Opinion Engine"]
            Recommendation["Recommendation Engine"]
            Moderation["Moderation"]
            Notifications["Notifications"]
        end

        API --> Accounts
        API --> News
        API --> Opinion
        API --> Recommendation
        API --> Moderation

        Worker --> News
        Worker --> Opinion
        Worker --> Recommendation
        Worker --> Notifications

        Scheduler --> Worker
    end

    DB[("PostgreSQL + pgvector")]
    Redis[("Redis<br/>Broker / Cache")]

    Accounts --> DB
    News --> DB
    Opinion --> DB
    Recommendation --> DB
    Moderation --> DB

    API --> Redis
    Scheduler --> Redis
    Redis --> Worker
```

HTTP-facing operations remain in the API runtime.

Long-running, scheduled or computationally intensive operations are delegated to background workers.

Initial asynchronous processing uses **Celery**, with **Redis** as the task broker.

---

## Module boundaries

Modules expose explicit application interfaces.

A module must not directly manipulate another module's internal implementation details.

### Dependency direction

```mermaid
flowchart TB
    Interface["Interface Layer<br/>API / Workers"]

    Application["Application Layer<br/>Use Cases / Services"]

    Domain["Domain Layer<br/>Rules / Entities / Engines"]

    Interface --> Application
    Application --> Domain
```

The dependency direction always points toward the domain.

### Infrastructure isolation

Infrastructure concerns are accessed through abstractions when appropriate.

```mermaid
flowchart LR
    Application["Domain / Application"]
    Port["Port / Interface"]
    Adapter["Infrastructure Adapter"]

    External["External Technology<br/>Redis / AI / RSS / Notifications"]

    Application --> Port
    Adapter -. implements .-> Port
    Adapter --> External
```

Domain logic must not require direct knowledge of:

- Django REST Framework;
- Celery;
- Redis;
- external AI providers;
- RSS libraries;
- notification providers.

Celery tasks act primarily as execution adapters that invoke application services rather than containing core domain rules themselves.

---

## Background processing

The distinction between synchronous and asynchronous execution is part of the architecture.

```mermaid
flowchart TD
    Start["Operation requested"]

    Immediate{"Must complete<br/>before responding?"}

    Expensive{"Computationally<br/>expensive?"}
    External{"Depends on external service<br/>with unpredictable latency?"}
    Scheduled{"Scheduled, batch or<br/>retryable workload?"}

    Sync["Execute synchronously<br/>in API runtime"]
    Async["Dispatch background task"]
    Queue["Redis / Celery"]
    Worker["Celery Worker"]
    Application["Application Service"]

    Start --> Immediate

    Immediate -- Yes --> Expensive
    Immediate -- No --> Async

    Expensive -- No --> External
    Expensive -- Yes --> Async

    External -- No --> Scheduled
    External -- Yes --> Async

    Scheduled -- No --> Sync
    Scheduled -- Yes --> Async

    Async --> Queue
    Queue --> Worker
    Worker --> Application
```

Typical asynchronous workloads include:

```mermaid
flowchart LR
    SourcePolling["Source Polling"]
    Normalization["Article Normalization"]
    Embedding["Embedding Generation"]
    StoryClustering["Story Clustering"]
    StoryEnrichment["Story Enrichment"]
    PerspectiveGeneration["Perspective Generation"]
    PerspectiveRebuild["Perspective Rebuilding"]
    Pulse["Pulse Recalculation"]
    Recommendation["Recommendation Profile Updates"]
    Notifications["Notification Delivery"]

    SourcePolling --> Normalization
    Normalization --> Embedding
    Embedding --> StoryClustering
    StoryClustering --> StoryEnrichment

    PerspectiveGeneration --> PerspectiveRebuild
    PerspectiveRebuild --> Pulse

    Recommendation
    Notifications
```

Operations required to immediately satisfy an HTTP request remain synchronous unless there is a concrete reason to delegate them.

---

## Data ownership

The initial architecture uses a single PostgreSQL database.

Domain boundaries represent **logical ownership**, not independent physical databases.

```mermaid
flowchart TB
    DB[("PostgreSQL + pgvector")]

    subgraph NewsDomain["News Domain"]
        Source["Source"]
        Article["Article"]
        Story["Story"]
    end

    subgraph OpinionDomain["Opinion Domain"]
        Opinion["Opinion"]
        Perspective["Perspective"]
        Representation["Representation"]
        PulseSnapshot["PulseSnapshot"]
    end

    subgraph RecommendationDomain["Recommendation Domain"]
        UserInterest["UserInterest"]
        FeedImpression["FeedImpression"]
    end

    NewsDomain --- DB
    OpinionDomain --- DB
    RecommendationDomain --- DB
```

The database is shared physically while ownership remains separated logically.

```mermaid
flowchart LR
    News["News Domain"]
    Opinion["Opinion Domain"]
    Recommendation["Recommendation Domain"]

    DB[("Single PostgreSQL")]

    News --> DB
    Opinion --> DB
    Recommendation --> DB

    Note["Logical ownership<br/>≠<br/>Database per service"]

    DB --- Note
```

A future architectural decision may introduce stronger physical isolation when operational requirements justify it.

Database-per-service is explicitly out of scope for the initial architecture.

---

## Alternatives considered

### Alternative A — Microservices from the beginning

Each major capability could be implemented as an independently deployed service.

```mermaid
flowchart TB
    Client["Mobile Application"]

    Gateway["API Gateway"]

    News["News Service"]
    Opinion["Opinion Service"]
    Recommendation["Recommendation Service"]
    Identity["Identity Service"]
    Notification["Notification Service"]

    NewsDB[("News DB")]
    OpinionDB[("Opinion DB")]
    RecommendationDB[("Recommendation DB")]
    IdentityDB[("Identity DB")]

    Client --> Gateway

    Gateway --> News
    Gateway --> Opinion
    Gateway --> Recommendation
    Gateway --> Identity

    News --> NewsDB
    Opinion --> OpinionDB
    Recommendation --> RecommendationDB
    Identity --> IdentityDB

    News -. network .-> Opinion
    Opinion -. network .-> Recommendation

    Notification -. events .-> Opinion
```

#### Advantages

- independent deployment;
- independent scaling;
- stronger runtime isolation;
- technology choices per service;
- explicit network boundaries.

#### Reasons for rejection

The initial product does not yet have evidence that these benefits outweigh the additional complexity.

This approach would require managing:

- service discovery;
- distributed tracing;
- network failures;
- API compatibility between services;
- distributed authentication;
- deployment orchestration;
- duplicated infrastructure;
- eventual consistency across services;
- more complex local development.

The architecture should not pay these costs before a concrete requirement exists.

---

### Alternative B — Traditional synchronous monolith

All processing could happen inside the API runtime.

```mermaid
sequenceDiagram
    actor User
    participant API as Django API
    participant Ingestion
    participant AI
    participant Clustering
    participant DB as PostgreSQL

    User->>API: HTTP request

    API->>Ingestion: Fetch / process data
    Ingestion-->>API: Result

    API->>AI: Generate embedding / summary
    AI-->>API: Result

    API->>Clustering: Cluster data
    Clustering-->>API: Result

    API->>DB: Persist result
    DB-->>API: OK

    API-->>User: HTTP response
```

#### Advantages

- simplest deployment model;
- minimal infrastructure;
- straightforward debugging.

#### Reasons for rejection

Several Pulso workloads have unpredictable execution time and external dependencies.

The sequence above directly couples API response latency to ingestion, AI processing and clustering.

A failure or slowdown in those workloads would affect the user-facing request path.

---

### Alternative C — Modular Monolith without background workers

Domain boundaries could remain explicit while all execution occurs in the API process.

```mermaid
flowchart TB
    Client["Mobile"]
    API["Django API Runtime"]

    subgraph Monolith["Modular Monolith"]
        News["News Engine"]
        Opinion["Opinion Engine"]
        Recommendation["Recommendation Engine"]
    end

    DB[("PostgreSQL")]

    Client --> API

    API --> News
    API --> Opinion
    API --> Recommendation

    News --> DB
    Opinion --> DB
    Recommendation --> DB
```

#### Advantages

- preserves domain boundaries;
- avoids queue infrastructure.

#### Reasons for rejection

This alternative does not address the different execution characteristics of Pulso's workloads.

The distinction between synchronous user operations and asynchronous processing is considered fundamental to the system.

---

## Decision comparison

```mermaid
flowchart LR
    Requirement["Pulso requirements"]

    Requirement --> Boundaries["Explicit domain boundaries"]
    Requirement --> Async["Asynchronous workloads"]
    Requirement --> Cost["Low operational cost"]
    Requirement --> Local["Simple local development"]

    Boundaries --> MM["Modular Monolith<br/>+ Workers"]
    Async --> MM
    Cost --> MM
    Local --> MM

    Micro["Microservices"] -. excessive initial<br/>operational complexity .-> Requirement
    Sync["Synchronous Monolith"] -. unsuitable for<br/>background workloads .-> Requirement
```

---

## Consequences

### Positive

- lower infrastructure complexity than microservices;
- easier local development;
- simpler deployments;
- easier transactions inside domain workflows;
- lower operational cost;
- suitable for the free-first requirement;
- explicit domain boundaries remain possible;
- asynchronous workloads do not block HTTP requests;
- workers can be scaled separately from the API runtime;
- the architecture can evolve incrementally.

### Negative

- all domains initially share the same deployment lifecycle;
- strong module boundaries depend partially on engineering discipline;
- modules share the same primary database;
- a poorly designed module can affect other parts of the monolith;
- independent scaling of specific domain components is limited compared with microservices;
- worker and API code must remain compatible because they share application modules.

---

## Architectural risks

The primary risk is allowing the modular monolith to degrade into an unstructured monolith.

### Undesired coupling

```mermaid
flowchart LR
    Opinion["Opinion Module"]
    NewsInternal["News Internal<br/>Implementation"]

    Recommendation["Recommendation Module"]
    StoryData["Story-owned Data"]

    Task["Celery Task"]
    BusinessRules["Business Rules"]

    OtherModule["Unrelated Module"]
    Tables["Foreign Domain Tables"]

    Opinion -. forbidden .-> NewsInternal
    Recommendation -. direct mutation .-> StoryData
    Task -. should not own .-> BusinessRules
    OtherModule -. uncontrolled access .-> Tables
```

### Boundary enforcement

```mermaid
flowchart LR
    Organization["Package Organization"]
    Services["Application Services"]
    Interfaces["Explicit Interfaces"]
    Tests["Architecture / Integration Tests"]
    Docs["Architecture Documentation"]
    ADR["ADRs"]
    Review["Code Review"]

    Organization --> Boundary["Module Boundaries"]
    Services --> Boundary
    Interfaces --> Boundary
    Tests --> Boundary
    Docs --> Boundary
    ADR --> Boundary
    Review --> Boundary
```

---

## Deployment implications

API, workers and scheduler may use the same application image or codebase while executing different commands.

```mermaid
flowchart TB
    Code["Pulso Backend<br/>Same Codebase / Image"]

    API["Django / DRF<br/>API Process"]
    WorkerA["Celery Worker"]
    WorkerB["Celery Worker"]
    Scheduler["Celery Beat"]

    Code --> API
    Code --> WorkerA
    Code --> WorkerB
    Code --> Scheduler

    Redis[("Redis")]
    DB[("PostgreSQL + pgvector")]

    API --> DB

    Scheduler --> Redis
    Redis --> WorkerA
    Redis --> WorkerB

    WorkerA --> DB
    WorkerB --> DB
```

These are separate **runtime processes**, not separate domain services.

The distinction is:

```mermaid
flowchart LR
    Domain["Domain Architecture"]

    Domain --> News["News Engine"]
    Domain --> Opinion["Opinion Engine"]
    Domain --> Recommendation["Recommendation Engine"]

    Runtime["Runtime Architecture"]

    Runtime --> API["API Process"]
    Runtime --> Worker["Worker Processes"]
    Runtime --> Scheduler["Scheduler Process"]
```

A logical Engine does not imply an independently deployed service.

---

## Evolution criteria

This decision does not prohibit future microservices.

Extraction must be driven by demonstrated operational requirements.

```mermaid
flowchart TD
    Module["Existing Module"]

    Scaling{"Significantly different<br/>scaling requirements?"}
    Hardware{"Specialized hardware<br/>required?"}
    Isolation{"Operational / failure<br/>isolation required?"}
    Release{"Independent release<br/>cycle required?"}
    Availability{"Different availability<br/>requirements?"}
    Ownership{"Independent team<br/>ownership?"}
    Contention{"Persistent database or<br/>resource contention?"}

    Keep["Keep inside<br/>Modular Monolith"]
    Candidate["Candidate for<br/>service extraction"]
    ADR["Create a new ADR"]

    Module --> Scaling

    Scaling -- Yes --> Candidate
    Scaling -- No --> Hardware

    Hardware -- Yes --> Candidate
    Hardware -- No --> Isolation

    Isolation -- Yes --> Candidate
    Isolation -- No --> Release

    Release -- Yes --> Candidate
    Release -- No --> Availability

    Availability -- Yes --> Candidate
    Availability -- No --> Ownership

    Ownership -- Yes --> Candidate
    Ownership -- No --> Contention

    Contention -- Yes --> Candidate
    Contention -- No --> Keep

    Candidate --> ADR
```

The existence of a logical Engine alone is **not** sufficient justification for creating a microservice.

---

## Non-goals

This ADR does not define:

- the internal design of the News Engine;
- the internal design of the Opinion Engine;
- the recommendation algorithm;
- the final deployment provider;
- the AI model or provider;
- database schema details;
- the event consistency strategy;
- the final queue topology.

Those decisions are documented separately when necessary.

---

## Decision summary

Pulso starts with a **modular monolith** to preserve simple deployment and explicit domain boundaries.

Background workers isolate asynchronous and computationally expensive processing from the HTTP lifecycle.

```mermaid
flowchart LR
    Boundaries["Clear boundaries"]
    Deployment["Simple deployment"]
    Async["Asynchronous processing"]
    Cost["Low operational cost"]

    Decision["Modular Monolith<br/>with Background Workers"]

    Boundaries --> Decision
    Deployment --> Decision
    Async --> Decision
    Cost --> Decision

    Decision --> Evolution["Incremental evolution"]

    Evolution -. when justified<br/>by measured requirements .-> Services["Possible service extraction"]
```

The architecture deliberately favors evolutionary distribution over premature distribution.