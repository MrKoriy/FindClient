# Domain Pitfalls

**Domain:** Telegram bot + 2GIS API lead scraper
**Researched:** 2026-03-09

## Critical Pitfalls

Mistakes that cause rewrites, project failure, or major issues.

### Pitfall 1: contact_groups is a Paid Field -- Free API Keys Do Not Return Phone/Email

**What goes wrong:** The core value of this project is collecting phone numbers, emails, and social links. The `items.contact_groups` field that contains all contact details (phone, email, messengers) is a **paid premium field** in the 2GIS Places API. A demo key or basic subscription will NOT return this data. Without purchasing access, the bot produces CSV files with names and addresses but zero actionable contact info.

**Why it happens:** The 2GIS documentation lists `contact_groups` under "fields available on demand and for an extra cost." Developers assume all fields in a search response are free. They build the entire pipeline, test with a few sample results that may include some basic info, then discover at scale that contact data is gated behind a sales conversation with 2GIS.

**Consequences:** The entire project value proposition collapses. You get organization names and addresses but no phones, no emails, no websites -- making the CSV useless for cold outreach campaigns.

**Prevention:**
1. Before writing any code, contact 2GIS sales team to confirm `items.contact_groups` access and pricing
2. Test your actual API key with `fields=items.contact_groups` parameter and verify phone/email data appears in the response
3. Have a fallback plan: if contact_groups is too expensive, consider scraping organization detail pages on 2gis.ru directly (but see Pitfall 2 about ToS)

**Detection:** API responses contain items with name, address, rating but `contact_groups` is either missing or empty. The `fields` parameter documentation shows the field requires paid access.

**Phase impact:** Must be validated in Phase 1 (API integration) before any other work proceeds. This is a go/no-go decision for the entire project.

**Confidence:** HIGH -- confirmed via [2GIS subscription services docs](https://docs.2gis.com/en/platform-manager/subscription/services) and [Places API reference](https://docs.2gis.com/en/api/search/places/reference/3.0/items)

---

### Pitfall 2: 2GIS Terms of Service Prohibit Data Storage, Caching, and Export

**What goes wrong:** The 2GIS API rules explicitly state: "It is prohibited to save, including for temporary storage (caching), the Products received via the Service." Storing results in SQLite, exporting to CSV, and building a duplicate-filter history -- all core features of this project -- directly violate the ToS.

**Why it happens:** Developers treat API data like free raw material. 2GIS's ToS are designed for map display applications, not data extraction tools. The rules explicitly prohibit using "automatic means or programs" to collect data and restrict usage to "displaying and/or representation to the Consumers as a part of the displayed Issue Page."

**Consequences:**
- API key revocation and permanent ban
- Potential legal action (2GIS has Russian legal entities that can pursue claims)
- In commercial contexts, legal liability for ToS violation
- All stored data and history becomes legally questionable

**Prevention:**
1. Accept the legal risk for personal use (this is a personal tool, not commercial, which reduces but does not eliminate risk)
2. Do NOT distribute this tool or make it available to others
3. Minimize stored data footprint -- store only organization IDs for deduplication, not full contact records
4. Consider rate-limiting requests to avoid triggering automated detection
5. Never use the tool at volumes that look like automated scraping (hundreds of requests per minute)
6. Keep the tool private and do not open-source it

**Detection:** 2GIS blocks your API key. You receive a cease-and-desist or legal notice. API responses start returning errors.

**Phase impact:** Architectural decision in Phase 1. The deduplication database design should store minimal data (org IDs only, not full records) to reduce ToS exposure.

**Confidence:** HIGH -- confirmed via [2GIS API rules](https://law.2gis.ae/api-rules/) and [Russian-language analysis](https://xmldatafeed.com/parsing-dannyh-s-yandeks-kart-i-2gis-kompleksnoe-tehnicheskoe-issledovanie/)

---

### Pitfall 3: Demo Key Pagination Hard-Caps at 50 Results Total

**What goes wrong:** With a demo API key, `page_size` maxes at 10 and `page` maxes at 5. That is a hard ceiling of 50 items per search query. If the user requests 200 leads in a niche, the bot silently returns only 50 or throws errors starting at page 6.

**Why it happens:** 2GIS demo keys are severely restricted for evaluation purposes only. The documentation buries this: "If you use a demo key to access the API, the maximum value [of page_size] is 10." Production keys have higher limits, but these are not publicly documented -- they depend on your subscription tier.

**Consequences:** Users request 100+ leads, bot promises to deliver, then returns only 50 or crashes on page 6. Worse: the bot may silently return fewer results without explaining why.

**Prevention:**
1. Detect key type at startup -- make a test request and check if page 6 returns an error
2. Cap the user's requested lead count based on actual API limits
3. Show transparent messaging: "Your API plan supports up to N results per query"
4. If using a demo key, warn in /start that results are limited to 50
5. For production use, purchase a proper subscription and test actual pagination limits

**Detection:** API returns error on page > 5 or page_size > 10. Total results returned is always <= 50 regardless of the `total` field in the response showing thousands of matches.

**Phase impact:** Phase 1 (API integration). Must probe actual limits of the configured API key and build limit-aware pagination.

**Confidence:** HIGH -- confirmed via [2GIS Places API examples](https://docs.2gis.com/en/api/search/places/examples)

---

### Pitfall 4: Scraping Without Progress Feedback Causes Users to Think the Bot is Dead

**What goes wrong:** Fetching 50-200 leads requires multiple paginated API calls with delays between them (to avoid rate limits). During this 30-120 second window, the Telegram user sees nothing -- no progress, no status, no indication anything is happening. They re-press buttons, re-send /scrape, or assume the bot crashed.

**Why it happens:** Developers build the scraping logic synchronously: receive command -> fetch all pages -> build CSV -> send file. The entire process blocks, and no intermediate updates are sent to the user.

**Consequences:**
- Users spam the bot with duplicate requests, creating parallel scraping tasks
- Multiple identical API calls waste quota
- Duplicate CSV files get sent
- If the user navigates away, they may miss the result entirely

**Prevention:**
1. Send an immediate "Starting search..." message after the user confirms parameters
2. Use `message.edit_text()` to update progress: "Fetched 20/50 leads..." after each page
3. Run scraping as a background `asyncio.create_task` so the bot remains responsive
4. Implement a per-user lock to prevent concurrent scraping sessions
5. Use a debounce mechanism on the /scrape command

**Detection:** Multiple identical requests in logs from the same user within seconds. Users complaining the bot "froze."

**Phase impact:** Phase 2 (Telegram bot UX). Must be designed into the scraping flow from the start, not bolted on later.

**Confidence:** HIGH -- standard Telegram bot UX pattern, confirmed via [python-telegram-bot discussions](https://github.com/python-telegram-bot/python-telegram-bot/discussions/2904)

---

## Moderate Pitfalls

### Pitfall 5: Cyrillic CSV Encoding Breaks in Excel

**What goes wrong:** Organization names, addresses, and categories in Moscow are all in Russian (Cyrillic). Writing CSV with `encoding='utf-8'` produces files that display as garbled text (mojibake) when opened in Microsoft Excel on Windows, because Excel defaults to the system locale encoding for CSV files (typically Windows-1251 for Russian Windows).

**Prevention:**
1. Use `utf-8-sig` encoding (UTF-8 with BOM) -- this is the magic fix for Excel. The BOM tells Excel to interpret the file as UTF-8
2. Alternative: offer a cp1251 encoding option for users who specifically use Russian Excel
3. Test with actual Cyrillic data before releasing: "OOO Ромашка" not "test company"

**Phase impact:** Phase 2 (CSV generation). Simple fix but easy to miss if testing with Latin-only data.

**Confidence:** HIGH -- well-documented Python CSV encoding issue, confirmed via [Python community discussions](https://discourse.mcneel.com/t/writing-csv-files-with-cyrillic-characters-using-python/130392)

---

### Pitfall 6: FSM State Lost on Bot Restart (MemoryStorage Default)

**What goes wrong:** Aiogram defaults to `MemoryStorage` for FSM (Finite State Machine) state management. If the bot process restarts (crash, deployment, VPS reboot) mid-conversation, all users lose their current state -- niche selection, lead count input, everything resets silently. The user was mid-flow selecting a niche, bot restarts, and their next button press does nothing or triggers an error.

**Prevention:**
1. For a personal-use bot, `MemoryStorage` is acceptable IF you handle graceful degradation
2. Add a catch-all handler that detects when a user sends input outside any active state and redirects them to /start
3. If reliability matters, use Redis-backed storage (`RedisStorage`) -- but this adds infrastructure complexity for a personal tool
4. For SQLite-based alternative, store FSM state in the same SQLite database used for history

**Detection:** After bot restart, users report that buttons stopped working or the bot ignores their input.

**Phase impact:** Phase 1 (bot setup). Decide storage strategy upfront. For a personal tool, MemoryStorage + graceful fallback is the pragmatic choice.

**Confidence:** HIGH -- explicitly documented in [aiogram FSM storage docs](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/storages.html): "not recommended for usage in bots because you will lose all states after restarting"

---

### Pitfall 7: Not Answering Callback Queries Causes Frozen Loading Indicator

**What goes wrong:** When a user taps an inline keyboard button (e.g., selecting a niche), Telegram shows a loading spinner on the button. If the bot handler doesn't call `callback_query.answer()`, the spinner stays visible for 30+ seconds, making the bot feel broken even though it's processing correctly.

**Prevention:**
1. Always call `await callback_query.answer()` at the start of every callback handler, even if you have no notification text
2. For slow operations, call `answer()` first, then process: `await callback.answer("Processing...")` followed by the actual logic
3. Create a middleware or decorator that auto-answers callback queries

**Detection:** Telegram buttons show perpetual loading spinners. Users tap buttons repeatedly thinking nothing happened.

**Phase impact:** Phase 2 (Telegram interaction handlers). Every callback handler must include this call.

**Confidence:** HIGH -- confirmed in [aiogram callback query docs](https://docs.aiogram.dev/en/latest/api/types/callback_query.html) and [Telegram Bot API docs](https://core.telegram.org/bots/api#answercallbackquery)

---

### Pitfall 8: allowed_updates Misconfiguration Causes Silent Callback Failures

**What goes wrong:** Aiogram 3.x auto-discovers update types from registered handlers. If you register only message handlers during development and add callback_query handlers later, the polling might only request `message` updates. Callback button presses are silently dropped by Telegram because the bot never asked for `callback_query` updates.

**Prevention:**
1. Explicitly set `allowed_updates` when starting the dispatcher: `dp.start_polling(bot, allowed_updates=["message", "callback_query"])`
2. Better: register all handler types (message + callback) before starting polling
3. Test inline keyboards early in development, not as an afterthought

**Detection:** Inline keyboard buttons do nothing. No errors in logs. Bot receives messages fine but ignores button taps.

**Phase impact:** Phase 1 (bot setup). Must configure polling correctly from the start.

**Confidence:** HIGH -- confirmed in [aiogram discussions](https://github.com/aiogram/aiogram/discussions/1239) and polling documentation

---

### Pitfall 9: SQLite Write Locking with Asyncio

**What goes wrong:** SQLite allows only one writer at a time. In an async context (aiogram uses asyncio), multiple coroutines may try to write simultaneously -- saving search history while deduplicating results while updating stats. Standard `sqlite3` module blocks the entire event loop during writes, freezing the bot.

**Prevention:**
1. Use `aiosqlite` instead of `sqlite3` -- it runs SQLite in a background thread with an async wrapper
2. Use WAL (Write-Ahead Logging) mode: `PRAGMA journal_mode=WAL` -- this allows concurrent reads with a single writer
3. Keep transactions short -- don't hold locks across API calls
4. Consider a simple write queue if contention is observed

**Detection:** Bot freezes briefly during database writes. "database is locked" errors in logs. Periodic unresponsiveness during scraping operations.

**Phase impact:** Phase 1 (database setup). Choose aiosqlite and enable WAL mode from day one.

**Confidence:** HIGH -- well-documented SQLite limitation, confirmed via [aiosqlite documentation](https://aiosqlite.omnilib.dev/en/stable/)

---

## Minor Pitfalls

### Pitfall 10: Missing/Null Fields in 2GIS Responses

**What goes wrong:** Not all organizations have email, website, phone, or social links. Naive code that accesses `item['contact_groups'][0]['contacts']` crashes with KeyError or IndexError on organizations that lack contact data.

**Prevention:**
1. Use `.get()` for every field access with sensible defaults: `item.get('contact_groups', [])`
2. Build a data extraction function that handles all missing-field cases and returns empty strings
3. Track fill rates in stats: "Found 50 orgs: 45 with phone, 30 with email, 20 with website"

**Phase impact:** Phase 1 (API response parsing). Build defensive parsing from the start.

**Confidence:** HIGH -- explicitly noted in PROJECT.md constraints

---

### Pitfall 11: 2GIS Search Relevance is Location-Biased

**What goes wrong:** The 2GIS Places API ranks results by proximity to a `location` coordinate. If you search "restaurants" with a location point in northern Moscow, you get restaurants near that point -- not a city-wide list. Users expecting "all restaurants in Moscow" get a geographically skewed subset.

**Prevention:**
1. Use `viewpoint1` and `viewpoint2` parameters to define a bounding box covering all of Moscow
2. Moscow bounding box approximately: `viewpoint1=55.57,37.37&viewpoint2=55.91,37.86`
3. Consider subdividing Moscow into grid cells for more uniform coverage if results are still clustered
4. Document the geographic scope clearly in bot messages

**Detection:** Results cluster around one area of Moscow. Users in different districts notice missing businesses they know exist.

**Phase impact:** Phase 1 (API query construction). Must choose proper geographic parameters from the start.

**Confidence:** MEDIUM -- confirmed via [2GIS Places troubleshooting docs](https://docs.2gis.com/en/api/search/places/troubleshooting) which discuss viewpoint and location parameters

---

### Pitfall 12: Hardcoded Category Mapping Drifts from 2GIS Taxonomy

**What goes wrong:** The bot offers predefined niches ("construction/repair", "medicine/clinics", etc.) that map to 2GIS search queries. But the text queries users choose may not match 2GIS's internal category taxonomy, leading to irrelevant results. "Delivery food" might return "food court" or "grocery store" results instead of delivery services.

**Prevention:**
1. Use the 2GIS [Categories API](https://docs.2gis.com/en/api/search/categories/overview) to fetch actual category IDs
2. Map each niche to a specific `rubric_id` filter rather than relying on free-text `q` search
3. Test each predefined niche and verify results are relevant before launch
4. For custom niche input, show a confirmation of what 2GIS found: "Found 150 results for 'delivery food'. Does this look right?"

**Phase impact:** Phase 2 (niche selection flow). Requires testing each category mapping against actual API results.

**Confidence:** MEDIUM -- based on 2GIS search behavior documented in [troubleshooting](https://docs.2gis.com/en/api/search/places/troubleshooting)

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| API key setup | contact_groups is paid (Pitfall 1) | Verify field access before writing code; contact 2GIS sales |
| API integration | Demo key 50-item cap (Pitfall 3) | Probe actual key limits at startup; build limit-aware pagination |
| API response parsing | Null/missing fields crash (Pitfall 10) | Defensive `.get()` access on every field |
| Search queries | Location bias (Pitfall 11) | Use Moscow bounding box, not point location |
| Search queries | Category mismatch (Pitfall 12) | Use rubric_id filters via Categories API |
| Database setup | SQLite + asyncio locking (Pitfall 9) | Use aiosqlite + WAL mode from day one |
| Bot setup | FSM state loss on restart (Pitfall 6) | MemoryStorage + graceful fallback handler |
| Bot setup | allowed_updates missing callback_query (Pitfall 8) | Explicitly set allowed_updates in polling config |
| Telegram UX | No progress feedback (Pitfall 4) | Immediate ack + periodic progress edits |
| Telegram UX | Frozen callback spinners (Pitfall 7) | Always call callback_query.answer() first |
| CSV generation | Cyrillic mojibake in Excel (Pitfall 5) | Use utf-8-sig encoding |
| Data storage / ToS | 2GIS prohibits caching/export (Pitfall 2) | Personal use only; minimize stored data; never distribute |

## Sources

- [2GIS Places API Overview](https://docs.2gis.com/en/api/search/places/overview) -- field access, premium features
- [2GIS Places API Examples](https://docs.2gis.com/en/api/search/places/examples) -- pagination limits for demo keys
- [2GIS API Troubleshooting](https://docs.2gis.com/en/api/search/places/troubleshooting) -- search relevance, parameter issues
- [2GIS Subscription Services](https://docs.2gis.com/en/platform-manager/subscription/services) -- paid vs free fields
- [2GIS API Legal Rules (UAE)](https://law.2gis.ae/api-rules/) -- ToS prohibiting caching, storage, automated extraction
- [2GIS API Free Trial](https://dev.2gis.com/api) -- demo key limitations
- [Aiogram FSM Storages](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/storages.html) -- MemoryStorage warning
- [Aiogram Callback Query Docs](https://docs.aiogram.dev/en/latest/api/types/callback_query.html) -- answer() requirement
- [Telegram Bot API Rate Limits](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Avoiding-flood-limits) -- flood control
- [aiosqlite Documentation](https://aiosqlite.omnilib.dev/en/stable/) -- async SQLite access
- [2GIS Parsing Legal Analysis (RU)](https://xmldatafeed.com/parsing-dannyh-s-yandeks-kart-i-2gis-kompleksnoe-tehnicheskoe-issledovanie/) -- legal risks of scraping
