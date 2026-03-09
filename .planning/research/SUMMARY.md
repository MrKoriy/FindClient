# Project Research Summary

**Project:** 2GIS Lead Scraper Bot
**Domain:** Telegram bot -- lead generation via 2GIS Places API
**Researched:** 2026-03-09
**Confidence:** MEDIUM

## Executive Summary

The 2GIS Lead Scraper Bot is a personal-use Telegram bot that searches the 2GIS Places API for organizations in Moscow by business category and delivers contact data (phone, email, website, social links) as a CSV file. The expert approach for this type of tool is a layered Python monolith using aiogram 3 for Telegram integration, aiohttp for async API calls, and SQLite for lightweight persistence. This is a well-understood pattern: async bot framework + external API client + local database + in-memory export. No microservices, no web frontend, no message queues. The architecture research confirms that a bottom-up build order (config/models first, then API client, then database, then services, then handlers) enables validation at each layer before wiring them together.

The recommended approach is to validate the 2GIS API access first -- specifically the `items.contact_groups` field that contains phone/email data -- because this is a go/no-go gate for the entire project. Research uncovered that this field is a premium paid feature, and the project's entire value proposition depends on it. Demo API keys are further capped at 50 results total (10 per page, 5 pages max). The first development phase must prove that the configured API key actually returns contact data at the needed volume before any other code is written.

The two highest risks are (1) discovering that contact data is paywalled and inaccessible with the current API key, which would kill the project, and (2) violating 2GIS Terms of Service, which explicitly prohibit data storage, caching, and export -- all core features of this tool. Mitigation for the first risk is straightforward: test the API key immediately. Mitigation for the second requires accepting the legal risk for personal use, keeping the tool private, minimizing stored data, and never distributing the bot or its output commercially. Beyond these two blockers, the remaining pitfalls (Cyrillic encoding, FSM state loss, callback query handling, SQLite locking) are all well-documented and have known fixes that must be applied during their respective build phases.

## Key Findings

### Recommended Stack

The stack is a pure Python async setup with four dependencies. aiogram 3.x was chosen over python-telegram-bot because it provides native async support, a Router system for organizing handlers by feature, and built-in FSM for multi-step conversation flows (niche selection -> count input -> processing -> result delivery). All HTTP calls go through aiohttp (bundled with aiogram) using a single shared ClientSession to avoid resource leaks. SQLite via aiosqlite handles persistence without any server setup.

**Core technologies:**
- **Python 3.10+**: Best ecosystem for Telegram bots, native async/await support
- **aiogram 3.x**: Modern async Telegram framework with Router, FSM, inline keyboards
- **aiohttp**: Async HTTP client for 2GIS API (shared session, connection pooling)
- **SQLite + aiosqlite**: Zero-config persistent storage for dedup history, async-safe
- **python-dotenv**: .env-based configuration for API keys and bot token

**Critical version note:** aiogram must be 3.4+ (Router architecture). Python must be 3.10+ (union types, match statements used in patterns).

### Expected Features

**Must have (table stakes -- v1 launch):**
- /start with welcome message and main menu
- /scrape flow: preset category selection + custom text input
- Lead count input (default 50)
- 2GIS Places API search with pagination
- Contact data extraction (name, phone, email, website, address, rating)
- CSV generation with utf-8-sig encoding, delivered via Telegram
- Summary statistics (total found, counts with phone/email/website)
- Progress indication during scraping (message edits)
- Error handling with Russian-language feedback
- SQLite-backed duplicate detection (skip previously scraped orgs)
- Search history (/history command)

**Should have (differentiators -- v1.x):**
- Social media links extraction (Instagram/VK/Facebook for cold DM)
- Working hours and review count in CSV (outreach prioritization)
- "New leads only" mode with transparent counts
- Filter by data completeness ("only orgs with email")
- XLSX export option (for Russian CRM tools)

**Defer (v2+):**
- Multi-city support (SPb, Novosibirsk)
- Inline category suggest via 2GIS Suggest API
- Google Sheets direct export
- Multiple CSV column templates for different outreach platforms

**Anti-features (explicitly exclude):**
- Built-in email sending / cold outreach (scope explosion, different domain)
- Web dashboard / analytics UI (Telegram chat is the interface)
- Automated scheduled scraping (cron overkill for personal use)
- Proxy rotation (using official API, not scraping)
- AI-powered search (preset categories + free text covers 99%)

### Architecture Approach

The system is a layered modular monolith: a single Python process with clearly separated internal layers. Handlers receive Telegram input and delegate to Services. Services orchestrate business logic by calling the 2GIS Client and Database Repository. Models are shared data classes used across all layers. This separation means business logic is testable without mocking Telegram, and the 2GIS client can be validated independently before anything else exists.

**Major components:**
1. **Handlers** (`handlers/`) -- Receive Telegram updates, manage FSM conversation flow, send responses
2. **Services** (`services/`) -- Orchestrate scraping: call API -> filter dupes -> save -> build CSV
3. **2GIS Client** (`api/`) -- HTTP wrapper with pagination, rate limiting, response parsing
4. **DB Repository** (`db/`) -- Async SQLite: store history, check duplicates, CRUD
5. **Models** (`models/`) -- Data classes (Organization, ScrapeResult) shared across layers
6. **Keyboards/States** (`keyboards/`, `states/`) -- UI definitions (inline keyboards, FSM state groups)
7. **Config** (`config.py`) -- Loads .env, exposes settings to all layers

**Key patterns to follow:**
- Single aiohttp.ClientSession for the entire bot lifecycle (Pattern 1)
- FSM StatesGroup for multi-step conversation (Pattern 2)
- Repository pattern isolating all SQL behind async methods (Pattern 3)
- In-memory CSV via io.BytesIO, never write to disk (Pattern 4)
- Sequential paginated fetching with asyncio.sleep between pages (Pattern 5)

**Key anti-patterns to avoid:**
- Business logic in handlers (keep handlers under 20 lines)
- Blocking calls in async context (no `requests`, no `sqlite3`, no `open()`)
- Global mutable state (pass dependencies via middleware or bot data)
- One giant router file (one router per feature domain)

### Critical Pitfalls

1. **contact_groups is a paid field** -- The phone/email/website data that makes this tool useful requires paid API access. Test the actual API key before writing any other code. This is a project-level go/no-go gate.

2. **2GIS ToS prohibit data storage and export** -- All core features (SQLite history, CSV export, dedup) violate the Terms of Service. Accept risk for personal use, keep the tool private, store only org IDs for dedup (not full records), never distribute.

3. **Demo key caps at 50 results** -- page_size max 10, page max 5. Must detect key type at startup, cap user requests to actual API limits, and show transparent messaging about available capacity.

4. **No progress feedback causes user confusion** -- Scraping 50+ leads takes 30-120 seconds. Without progress updates via message.edit_text(), users spam the bot with duplicate requests. Must send immediate acknowledgment + periodic progress edits.

5. **Cyrillic CSV breaks in Excel** -- Use `utf-8-sig` encoding (UTF-8 with BOM). Simple fix, easy to miss if testing with Latin data only.

6. **SQLite + asyncio requires aiosqlite + WAL mode** -- Synchronous sqlite3 blocks the event loop. Use aiosqlite from day one and enable WAL journal mode to allow concurrent reads during writes.

7. **Callback queries must be answered** -- Every inline keyboard handler must call `callback_query.answer()` or Telegram shows a frozen loading spinner for 30 seconds.

## Implications for Roadmap

Based on combined research, the project should be built in 5 phases following a bottom-up dependency order. The critical insight is that Phase 1 is a validation gate: if the API key does not return contact data, the project cannot proceed as designed.

### Phase 1: Foundation and API Validation
**Rationale:** Everything depends on two things being true: (a) the API key returns contact data, and (b) the basic project skeleton works. Validate the riskiest assumption first. Architecture research confirms config/models/entry-point come first, and the 2GIS client should be testable independently.
**Delivers:** Working bot process that connects to Telegram, a validated 2GIS API client that fetches and parses organization data with contact info, and the data model definitions.
**Features:** Config loading, bot entry point, 2GIS API search with pagination, contact data extraction, Organization data model.
**Avoids:** Pitfall 1 (paid fields -- validate immediately), Pitfall 3 (demo key limits -- detect and report), Pitfall 10 (null fields -- defensive parsing from the start), Pitfall 11 (location bias -- use Moscow bounding box), Pitfall 8 (allowed_updates -- configure polling correctly).

### Phase 2: Database and Dedup Engine
**Rationale:** The database layer is required before services can filter duplicates or store history. Architecture research places this as the second build step. Must get aiosqlite + WAL mode right from the start to avoid Pitfall 9.
**Delivers:** SQLite database with table creation, repository methods for saving orgs, checking duplicates, and recording scrape history.
**Features:** SQLite storage of scraped org IDs, duplicate detection, search history storage.
**Avoids:** Pitfall 9 (SQLite write locking -- use aiosqlite + WAL), Pitfall 2 (ToS -- store only org IDs for dedup, not full contact records).

### Phase 3: Service Layer and CSV Export
**Rationale:** Services connect the API client and database. ScrapeService orchestrates the full flow (fetch -> dedup -> save -> export). ExportService builds CSV in memory. These can be tested with unit tests before any Telegram UI exists. Architecture research identifies this as the layer that should contain all business logic, keeping handlers thin.
**Delivers:** Complete scrape orchestration (API call -> dedup filter -> DB save -> CSV generation), in-memory CSV with proper Cyrillic encoding, summary statistics computation.
**Features:** CSV generation and delivery, summary statistics (total found, with-phone/email/website counts), smart dedup filtering.
**Avoids:** Pitfall 5 (Cyrillic encoding -- utf-8-sig from day one), anti-pattern of business logic in handlers.

### Phase 4: Telegram Bot UX (Handlers, Keyboards, FSM)
**Rationale:** The Telegram-facing layer depends on all lower layers being ready. Build /start first (trivial), then /scrape flow (the core multi-step conversation), then /history. This is where FSM, inline keyboards, progress feedback, and error messages all come together.
**Delivers:** Complete user-facing bot: /start menu, /scrape flow (niche selection -> count input -> progress -> CSV delivery), /history display, error handling with Russian messages.
**Features:** /start with main menu, /scrape flow with preset + custom niche selection, lead count input, progress indication, error handling, /history command.
**Avoids:** Pitfall 4 (no progress feedback -- immediate ack + periodic edits), Pitfall 6 (FSM state loss -- graceful fallback handler), Pitfall 7 (frozen callback spinners -- always answer()), Pitfall 12 (category mismatch -- test each preset against API results).

### Phase 5: Enhancements (v1.x)
**Rationale:** Once the core loop works end-to-end, layer on differentiating features. These are all low-complexity additions that build on the existing architecture without structural changes.
**Delivers:** Social media extraction, working hours/rating data, "new leads only" mode, data completeness filters, XLSX export option.
**Features:** Social media links in CSV, working hours/review count, smart "new leads only" mode, filter by data completeness, XLSX export.
**Avoids:** No new pitfalls -- all infrastructure risks are resolved by Phase 4.

### Phase Ordering Rationale

- **API validation must come first** because Pitfall 1 (paid contact_groups field) is a project-level blocker. No point building database, services, or UI if the API key cannot return the data this tool needs.
- **Database before services** because the ScrapeService needs the Repository to filter duplicates and save history. Building them in the wrong order means the service layer has to be rewritten.
- **Services before handlers** because handlers should be thin wrappers that call services. If handlers are built first, business logic leaks into them (Architecture anti-pattern 1).
- **Enhancements last** because they are all additive features on top of a working core. None require architectural changes -- just additional fields in CSV, additional query modes, and UI options.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 1:** Needs `/gsd:research-phase` -- 2GIS API behavior is partially documented. Actual pagination limits, contact_groups field availability, response structure for different query types, and Moscow city_id/bounding box values all need empirical validation with the real API key. The Categories API (rubric_id filtering) also needs investigation.
- **Phase 4:** May need research -- aiogram 3 middleware patterns for dependency injection (passing session/repository to handlers) and FSM edge cases (user sends /scrape while already mid-flow) are not fully covered in the current research.

Phases with standard patterns (skip research-phase):
- **Phase 2:** SQLite + aiosqlite + repository pattern is thoroughly documented. Schema is defined in STACK.md and ARCHITECTURE.md.
- **Phase 3:** In-memory CSV generation and service orchestration are standard Python patterns with clear examples in the research.
- **Phase 5:** All enhancement features are incremental additions to existing components. No new integration patterns needed.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All technologies are well-established with official documentation. aiogram 3, aiosqlite, SQLite -- no exotic choices. |
| Features | MEDIUM | Feature landscape derived from competitor analysis and broader lead-scraper ecosystem. The 2GIS+Telegram niche is small, so some extrapolation was needed. Feature priorities align well with PROJECT.md requirements. |
| Architecture | HIGH | Layered modular monolith is the standard pattern for single-user Telegram bots with external API integration. Multiple community templates confirm the router/handler/service/repository structure. |
| Pitfalls | HIGH | Critical pitfalls (paid fields, ToS, demo key limits) confirmed via official 2GIS documentation. Moderate pitfalls (encoding, FSM, callbacks, SQLite) confirmed via official aiogram/aiosqlite docs. |

**Overall confidence:** MEDIUM -- downgraded from HIGH because two critical unknowns remain unresolved and can only be validated empirically.

### Gaps to Address

- **contact_groups field availability:** Cannot confirm whether the project's API key actually returns phone/email data without making a test request. This MUST be validated before any development begins. If the field is inaccessible, the project needs a fundamentally different approach (web scraping, different data source).
- **Paid key pagination limits:** The demo key caps at 50 results. The paid key limits are "undocumented" according to research -- actual page_size and max page values need empirical testing. This determines whether the bot can realistically deliver 100-200 lead requests.
- **Moscow city_id:** The exact city_id or correct bounding box coordinates for Moscow-wide search need to be discovered via the API. Research suggests viewpoint parameters (55.57,37.37 to 55.91,37.86) but these need validation.
- **Rate limiting thresholds:** No documented rate limits for the 2GIS API. The 0.3-second delay between pages is a guess. Actual throttling behavior needs testing under load.
- **ToS enforcement in practice:** Research confirms the ToS prohibit data storage/export, but the practical enforcement risk for a personal-use tool is unknown. This is an accepted risk, not a solvable gap.

## Sources

### Primary (HIGH confidence)
- [2GIS Places API Overview](https://docs.2gis.com/en/api/search/places/overview) -- API structure, field access, premium features
- [2GIS Places API Reference](https://docs.2gis.com/en/api/search/places/reference/3.0/items) -- Endpoint parameters, pagination, field names
- [2GIS Places API Examples](https://docs.2gis.com/en/api/search/places/examples) -- Demo key limitations, page_size caps
- [2GIS Subscription Services](https://docs.2gis.com/en/platform-manager/subscription/services) -- Paid vs free field access
- [2GIS API Rules](https://law.2gis.ae/api-rules/) -- Terms of Service, data storage prohibition
- [aiogram FSM Documentation](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/index.html) -- StatesGroup, FSMContext
- [aiogram Middleware Documentation](https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html) -- Dependency injection patterns
- [aiogram Callback Query Docs](https://docs.aiogram.dev/en/latest/api/types/callback_query.html) -- answer() requirement
- [aiogram FSM Storage Docs](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/storages.html) -- MemoryStorage warning
- [aiosqlite Documentation](https://aiosqlite.omnilib.dev/en/stable/) -- Async SQLite access, WAL mode
- [parser-2gis on GitHub](https://github.com/interlark/parser-2gis) -- Competitor feature baseline

### Secondary (MEDIUM confidence)
- [2GIS API Troubleshooting](https://docs.2gis.com/en/api/search/places/troubleshooting) -- Search relevance, viewpoint parameters
- [aiogram Bot Structure Template](https://github.com/GI-Corp/aiogram-bot-structure-template) -- Community project structure patterns
- [aiogram Bot Template (welel)](https://github.com/welel/aiogram-bot-template) -- Router/middleware/config patterns
- [aiohttp Rate Limiting Patterns](https://quentin.pradet.me/blog/how-do-you-rate-limit-calls-with-aiohttp.html) -- Practical rate limiting
- [parselab.org 2GIS Parser](https://parselab.org/parser-2gis.html) -- Commercial competitor features
- [Google Maps Scraper (omkarcloud)](https://github.com/omkarcloud/google-maps-scraper) -- Cross-domain feature comparison
- [2GIS Parsing Legal Analysis (RU)](https://xmldatafeed.com/parsing-dannyh-s-yandeks-kart-i-2gis-kompleksnoe-tehnicheskoe-issledovanie/) -- Legal risk assessment

### Tertiary (LOW confidence)
- [n8n Lead Sourcing Workflow](https://community.n8n.io/t/free-workflow-csv-lead-sourcing-icp-filter-dedupe-google-sheets/273356) -- Dedup patterns in lead workflows
- [In-memory CSV for Telegram (gist)](https://gist.github.com/ohld/9c9cbcfa09020be62a635ae111cf3142) -- Pattern reference

---
*Research completed: 2026-03-09*
*Ready for roadmap: yes*
