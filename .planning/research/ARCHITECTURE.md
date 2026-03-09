# Architecture Patterns

**Domain:** Telegram bot with external API scraper (lead generation)
**Researched:** 2026-03-09

## Recommended Architecture

The system follows a **layered modular monolith** pattern -- a single Python process with clearly separated internal layers. This is the standard pattern for personal-use Telegram bots that integrate with external APIs. No microservices, no message queues, no Redis. The bot's workload (single user, sequential scrape requests) does not justify distributed architecture.

```
+------------------+
|   Telegram API   |  (external)
+--------+---------+
         |
+--------v---------+
|  Bot Layer       |  handlers/ + keyboards/ + states/
|  (aiogram 3)     |  Receives commands, manages conversation FSM,
|                  |  sends responses + CSV files
+--------+---------+
         |
+--------v---------+
|  Service Layer   |  services/
|                  |  Orchestrates business logic:
|                  |  - ScrapeService (coordinates the scrape flow)
|                  |  - ExportService (builds CSV in memory)
+--------+---------+
         |
    +----+----+
    |         |
+---v---+ +---v--------+
| 2GIS  | | Database   |  (external API + local SQLite)
| Client| | Repository |
+-------+ +------------+
  api/       db/
```

### Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **Handlers** (`handlers/`) | Receive Telegram updates, validate user input, call services, send responses | Services, Keyboards, FSM States |
| **Keyboards** (`keyboards/`) | Define inline keyboards and reply keyboards for menus | Handlers only (imported, no logic) |
| **States** (`states/`) | Define FSM state groups for multi-step flows (niche selection, count input) | Handlers (via aiogram FSM) |
| **Services** (`services/`) | Business logic: coordinate scraping, build CSV, compute statistics | 2GIS Client, DB Repository |
| **2GIS Client** (`api/`) | HTTP client wrapper for 2GIS Places API. Handles pagination, rate limiting, field mapping | 2GIS external API (outbound HTTP) |
| **DB Repository** (`db/`) | All database operations: store history, check duplicates, CRUD for scrape records | SQLite via aiosqlite |
| **Models** (`models/`) | Data classes/Pydantic models shared across layers: Organization, ScrapeResult, ScrapeHistory | Used by all layers (import only) |
| **Config** (`config.py`) | Loads .env, exposes settings (API keys, bot token, DB path) | Used by all layers at startup |

### Data Flow

**Main scrape flow (the core use case):**

```
1. User sends /scrape
   --> Handler enters FSM: ScrapeStates.choosing_niche

2. Bot shows niche keyboard (inline buttons for categories)
   <-- Handler sends keyboard via bot.send_message

3. User picks niche (e.g., "restaurants") or types custom
   --> Handler receives callback/message, saves to FSM data
   --> Transitions to ScrapeStates.entering_count

4. Bot asks "How many leads? (default 50)"
   <-- Handler sends prompt

5. User enters count (or skips for default)
   --> Handler validates input, calls ScrapeService.scrape()

6. ScrapeService orchestrates:
   a) Calls 2GISClient.search(query, count)
      --> 2GISClient paginates through API (page=1,2,3...)
      --> Each page: GET catalog.api.2gis.com/3.0/items?q=...&page=N
      --> Maps response JSON to Organization models
      --> Respects rate limits (asyncio.sleep between pages)
   b) Calls DBRepository.filter_duplicates(organizations)
      --> SELECT existing org IDs from history table
      --> Returns only new organizations
   c) Calls DBRepository.save_scrape(organizations, metadata)
      --> INSERT new organizations + scrape record
   d) Calls ExportService.to_csv(organizations)
      --> Builds CSV in memory (io.BytesIO + csv.writer)
      --> Returns BufferedInputFile for Telegram

7. Handler receives ScrapeResult (csv_file, stats)
   --> Sends CSV document + statistics message to user
   --> Clears FSM state
```

**Duplicate filtering flow:**

```
Organizations table (persistent):
  - org_2gis_id (unique, from API)
  - name, phone, email, website, address, rating, socials
  - first_seen_at

Scrape history table:
  - scrape_id, query, count_requested, count_found, timestamp

Scrape-org junction table:
  - scrape_id -> org_id (which orgs came from which scrape)

On new scrape:
  1. Fetch N orgs from 2GIS
  2. Check org_2gis_id against organizations table
  3. Only include orgs NOT already in DB
  4. Save all new orgs + create scrape record
```

## Project Structure

```
2giScraper/
  bot.py                  # Entry point: create Bot, Dispatcher, register routers, start polling
  config.py               # Settings from .env (BOT_TOKEN, TWOGIS_API_KEY, DB_PATH)

  handlers/
    __init__.py           # Register all routers
    start.py              # /start command, main menu
    scrape.py             # /scrape flow: niche selection, count, trigger scrape
    history.py            # /history command: show past scrapes

  keyboards/
    __init__.py
    main_menu.py          # Main menu reply keyboard
    niches.py             # Inline keyboard for niche selection

  states/
    __init__.py
    scrape.py             # ScrapeStates: choosing_niche, entering_count, processing

  services/
    __init__.py
    scrape_service.py     # Orchestrates scrape: call API -> filter dupes -> save -> export
    export_service.py     # CSV generation in memory

  api/
    __init__.py
    twogis_client.py      # aiohttp-based 2GIS API client with pagination + rate limiting

  db/
    __init__.py
    models.py             # SQLite table definitions (as SQL or via simple ORM)
    repository.py         # Async DB operations: save, query history, filter duplicates
    migrations.py         # Create tables on first run (or check schema)

  models/
    __init__.py
    organization.py       # Organization dataclass/Pydantic model
    scrape_result.py      # ScrapeResult: organizations list + stats + csv file

  .env                    # BOT_TOKEN, TWOGIS_API_KEY
  requirements.txt
```

## Patterns to Follow

### Pattern 1: Single aiohttp.ClientSession for API Client

**What:** Create one aiohttp.ClientSession at bot startup, pass it to 2GISClient, close on shutdown. Never create a new session per request.

**When:** Always. This is the correct async HTTP pattern.

**Why:** Session reuses TCP connections, SSL contexts, and connection pools. Creating per-request sessions defeats the purpose of async and leaks resources.

**Example:**
```python
# bot.py
async def main():
    async with aiohttp.ClientSession() as session:
        twogis_client = TwoGISClient(session, settings.TWOGIS_API_KEY)
        # ... pass to services via dependency injection or middleware
        dp.run_polling(bot)
```

### Pattern 2: FSM for Multi-Step Conversation

**What:** Use aiogram's built-in StatesGroup + FSMContext to manage the scrape flow (niche -> count -> processing -> result).

**When:** Any multi-step user interaction. The scrape flow has 3+ steps.

**Why:** FSM is the standard aiogram pattern for conversations. It persists state between messages, handles cancellation cleanly, and prevents invalid state transitions.

**Example:**
```python
# states/scrape.py
from aiogram.fsm.state import State, StatesGroup

class ScrapeStates(StatesGroup):
    choosing_niche = State()
    entering_custom_niche = State()
    entering_count = State()
    processing = State()
```

### Pattern 3: Repository Pattern for Database

**What:** All database access goes through a single Repository class with async methods. No raw SQL in handlers or services.

**When:** Always. Even with SQLite, isolate DB access behind a clear interface.

**Why:** Makes testing easier (mock the repository), keeps SQL in one place, and allows swapping SQLite for something else later without touching business logic.

**Example:**
```python
# db/repository.py
class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_existing_org_ids(self, org_ids: list[str]) -> set[str]:
        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ",".join("?" * len(org_ids))
            cursor = await db.execute(
                f"SELECT twogis_id FROM organizations WHERE twogis_id IN ({placeholders})",
                org_ids
            )
            rows = await cursor.fetchall()
            return {row[0] for row in rows}
```

### Pattern 4: In-Memory CSV Generation

**What:** Build CSV in io.BytesIO, never write to disk. Send directly to Telegram as BufferedInputFile.

**When:** Always for CSV export in a Telegram bot context.

**Why:** Disk I/O is unnecessary, slower, and requires cleanup. In-memory CSV for lead counts under 10K is trivially small (< 1MB). Telegram's file limit is 50MB -- never a concern for CSV.

**Example:**
```python
# services/export_service.py
import csv
import io
from aiogram.types import BufferedInputFile

def build_csv(organizations: list[Organization]) -> BufferedInputFile:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Phone", "Email", "Website", "Address", "Rating", "Socials"])
    for org in organizations:
        writer.writerow([org.name, org.phone, org.email, org.website, org.address, org.rating, org.socials])

    content = output.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility
    return BufferedInputFile(content, filename="leads.csv")
```

### Pattern 5: Paginated API Fetching with Rate Limiting

**What:** The 2GIS Client fetches pages sequentially, accumulating results until the requested count is reached or API returns no more results. Add asyncio.sleep between pages.

**When:** Always when calling 2GIS API. page_size has a max (likely 50), so fetching 200 leads requires 4+ pages.

**Why:** Sequential pagination with delays is sufficient for a single-user bot. Parallel page fetching would risk rate limits and is unnecessary given the low volume.

**Example:**
```python
# api/twogis_client.py
async def search(self, query: str, city: str, count: int) -> list[Organization]:
    results = []
    page = 1
    page_size = 50  # 2GIS max per page

    while len(results) < count:
        params = {
            "q": query,
            "city": city,
            "page": page,
            "page_size": page_size,
            "fields": "items.contact_groups,items.address,items.rating,items.reviews",
            "key": self.api_key,
        }
        async with self.session.get(self.BASE_URL, params=params) as resp:
            data = await resp.json()
            items = data.get("result", {}).get("items", [])
            if not items:
                break
            results.extend(self._parse_items(items))
            page += 1
            await asyncio.sleep(0.3)  # Rate limit safety

    return results[:count]
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Business Logic in Handlers

**What:** Putting API calls, CSV generation, or DB queries directly in handler functions.

**Why bad:** Handlers become 100+ line monsters. Impossible to test business logic without mocking Telegram. Duplicated logic when multiple handlers need similar operations.

**Instead:** Handlers receive input, validate, call a service, send the result. Service does the work. Handler code stays under 20 lines.

### Anti-Pattern 2: Blocking Calls in Async Context

**What:** Using `requests` library, `sqlite3` (synchronous), or `open()` for file I/O inside async handlers.

**Why bad:** Blocks the entire event loop. The bot becomes unresponsive to all users (even though this is single-user, it blocks Telegram update processing, causing timeouts).

**Instead:** Use aiohttp (not requests), aiosqlite (not sqlite3), io.BytesIO (not file writes). Everything in the async pipeline must be async.

### Anti-Pattern 3: Global Mutable State

**What:** Storing the aiohttp session, DB connection, or API client as module-level globals.

**Why bad:** Hard to test, lifecycle unclear, risk of using before initialization or after shutdown.

**Instead:** Pass dependencies through aiogram's middleware or bot data dict. Initialize in the startup lifecycle, clean up on shutdown.

### Anti-Pattern 4: One Giant Router

**What:** Putting all handlers (/start, /scrape, /history, callbacks) in a single file.

**Why bad:** File grows rapidly. Related handlers (e.g., all scrape flow states) become hard to find among unrelated ones.

**Instead:** One router per feature domain: start.py, scrape.py, history.py. Each file registers its own Router, main __init__.py includes them all into the Dispatcher.

## Scalability Considerations

| Concern | Current (1 user) | If Shared (10 users) | If Public (100+ users) |
|---------|-------------------|----------------------|------------------------|
| **Concurrency** | Sequential is fine | Still fine with async | Add task queue (e.g., arq) for scrape jobs |
| **Database** | SQLite, single file | SQLite still works | Migrate to PostgreSQL |
| **Rate limits** | No concern | May hit 2GIS limits | Need per-user queuing, API key rotation |
| **State storage** | In-memory FSM (default) | In-memory still works | Redis-backed FSM storage |
| **Bot updates** | Long polling | Long polling | Webhook via FastAPI for lower latency |

For the stated scope (personal tool, single user), none of the "scaled" patterns are needed. The architecture intentionally avoids premature optimization while keeping clear boundaries that make scaling straightforward later.

## Suggested Build Order

Based on component dependencies, build in this order:

1. **Config + Models + Entry Point** -- Foundation. Everything depends on config and data models. Wire up bot.py with a minimal Dispatcher that starts polling.

2. **2GIS Client** (`api/twogis_client.py`) -- Core value. Can be tested independently against the real API before anything else exists. Validates that the API key works, pagination works, and response parsing is correct.

3. **Database Layer** (`db/`) -- Needed before services can filter duplicates or store history. Create tables, implement repository methods.

4. **Services** (`services/`) -- Connects API client and DB. ScrapeService orchestrates the flow. ExportService builds CSV. These can be tested with unit tests before any Telegram integration.

5. **Handlers + Keyboards + States** -- The Telegram-facing layer. Depends on all lower layers being ready. Build /start first (trivial), then /scrape flow (the core), then /history.

**Rationale:** This bottom-up order means each layer can be tested in isolation before wiring it to the layer above. The 2GIS client is validated first because if the API doesn't return the expected data, everything else is moot.

## Sources

- [aiogram bot structure template](https://github.com/GI-Corp/aiogram-bot-structure-template) -- community project structure patterns (MEDIUM confidence)
- [aiogram bot template](https://github.com/welel/aiogram-bot-template) -- widely-used template with routers, middleware, config patterns (MEDIUM confidence)
- [aiogram FSM documentation](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/index.html) -- official FSM/StatesGroup docs (HIGH confidence)
- [aiogram middleware documentation](https://docs.aiogram.dev/en/latest/dispatcher/middlewares.html) -- official middleware architecture (HIGH confidence)
- [2GIS Places API](https://docs.2gis.com/en/api/search/places/overview) -- official API documentation (HIGH confidence)
- [2GIS Items endpoint](https://docs.2gis.com/en/api/search/places/reference/3.0/items) -- endpoint reference with pagination params (HIGH confidence)
- [aiosqlite](https://github.com/omnilib/aiosqlite) -- async SQLite library, actively maintained (HIGH confidence)
- [aiohttp rate limiting patterns](https://quentin.pradet.me/blog/how-do-you-rate-limit-calls-with-aiohttp.html) -- practical rate limiting for aiohttp (MEDIUM confidence)
- [In-memory CSV for Telegram](https://gist.github.com/ohld/9c9cbcfa09020be62a635ae111cf3142) -- pattern for sending CSV without disk I/O (MEDIUM confidence)
