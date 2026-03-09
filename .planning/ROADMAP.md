# Roadmap: 2GIS Lead Scraper Bot

## Overview

Build a Telegram bot that scrapes 2GIS for business contacts by niche and delivers CSV files. The build follows a bottom-up order: validate that the 2GIS API key actually returns contact data (go/no-go gate), then add persistence and dedup, then wire up CSV export through a service layer, and finally build the complete Telegram UX on top of working internals. Four phases, each independently verifiable before moving on.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Foundation and API Validation** - Project skeleton, config, 2GIS API client with proven contact data access
- [ ] **Phase 2: Database and Dedup** - SQLite persistence for scrape history and duplicate filtering
- [ ] **Phase 3: Service Layer and CSV Export** - Scrape orchestration, dedup filtering, in-memory CSV generation with stats
- [ ] **Phase 4: Telegram Bot UX** - Complete user-facing bot: menus, niche selection, progress feedback, file delivery, history

## Phase Details

### Phase 1: Foundation and API Validation
**Goal**: Bot process connects to Telegram and the 2GIS API client is proven to fetch and parse organization contact data from Moscow
**Depends on**: Nothing (first phase)
**Requirements**: FOUND-01, FOUND-02, FOUND-03, FOUND-04, FOUND-05
**Success Criteria** (what must be TRUE):
  1. Running `python -m bot` or `python main.py` starts the bot and it connects to Telegram (responds to a basic command)
  2. BOT_TOKEN and TWOGIS_API_KEY are loaded from .env; the bot refuses to start if either is missing
  3. The 2GIS API client searches for organizations by query text in Moscow and returns parsed results including contact fields (phone, email, website, address, rating, socials)
  4. The API client fetches multiple pages of results when available (pagination works for requests exceeding one page)
  5. Missing contact fields (no email, no website, etc.) are handled gracefully -- parsed as empty strings, no crashes
**Plans:** 2 plans

Plans:
- [x] 01-01-PLAN.md — Project skeleton, config, models, bot entry point with /start handler
- [ ] 01-02-PLAN.md — 2GIS API client with search, pagination, contact parsing, and verification script

### Phase 2: Database and Dedup
**Goal**: Scraped organizations are persisted in SQLite so the bot can detect and filter duplicates across scraping sessions
**Depends on**: Phase 1
**Requirements**: HIST-01, HIST-02, HIST-04
**Success Criteria** (what must be TRUE):
  1. A SQLite database is created on first run and persists between bot restarts (data survives process stop/start)
  2. After a scrape, organization IDs are stored in the database
  3. On a subsequent scrape for the same niche, previously scraped organizations are excluded from results
  4. Database operations are async (aiosqlite) and do not block the Telegram bot event loop
**Plans**: TBD

Plans:
- [ ] 02-01: TBD

### Phase 3: Service Layer and CSV Export
**Goal**: A service layer orchestrates the full scrape flow (API call, dedup, save, CSV export) and produces a ready-to-send CSV file with summary statistics
**Depends on**: Phase 2
**Requirements**: EXPORT-01, EXPORT-02, EXPORT-03, EXPORT-04
**Success Criteria** (what must be TRUE):
  1. ScrapeService calls 2GIS API, filters out duplicates via the database, saves new results, and returns structured data -- all in one orchestrated flow
  2. CSV file contains columns: name, phone, email, website, address, rating, socials -- with proper Russian headers
  3. CSV uses utf-8-sig encoding (opens correctly with Cyrillic text in Excel without manual encoding selection)
  4. CSV is generated in memory (io.BytesIO), never written to disk
  5. Missing fields appear as empty strings in the CSV, not "None" or "null"
**Plans**: TBD

Plans:
- [ ] 03-01: TBD

### Phase 4: Telegram Bot UX
**Goal**: Users interact with a complete Telegram bot: start menu, niche selection flow, lead count input, live progress, CSV delivery with stats, and search history
**Depends on**: Phase 3
**Requirements**: UX-01, UX-02, UX-03, UX-04, UX-05, UX-06, UX-07, UX-08, UX-09, UX-10, HIST-03
**Success Criteria** (what must be TRUE):
  1. /start shows a welcome message with an inline keyboard menu describing the bot capabilities
  2. /scrape launches a multi-step flow: user picks a preset niche (primary or secondary categories) or types a custom niche, then enters lead count (default 50)
  3. During scraping, the bot edits its message to show progress (current count of leads found)
  4. When scraping completes, the bot sends the CSV file and a summary message with stats: total found, count with phone, with email, with website, with socials
  5. /history shows a list of past scrapes with niche, date, and result count
  6. Errors (API failures, invalid input, empty results) are handled with clear Russian-language messages -- no tracebacks, no English error text
**Plans**: TBD

Plans:
- [ ] 04-01: TBD
- [ ] 04-02: TBD
- [ ] 04-03: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation and API Validation | 1/2 | In Progress | - |
| 2. Database and Dedup | 0/1 | Not started | - |
| 3. Service Layer and CSV Export | 0/1 | Not started | - |
| 4. Telegram Bot UX | 0/3 | Not started | - |
