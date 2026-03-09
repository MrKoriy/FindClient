# Feature Research

**Domain:** Telegram bot for lead generation via 2GIS organization scraping
**Researched:** 2026-03-09
**Confidence:** MEDIUM -- based on competitor analysis of 2GIS parsers, Google Maps scrapers, and general lead generation tool landscape. 2GIS-specific Telegram bot niche is small, so features derived from broader lead scraper ecosystem patterns applied to the specific domain.

## Feature Landscape

### Table Stakes (Users Expect These)

Features users assume exist. Missing these = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Category/niche selection | Every 2GIS parser lets you pick a business category. Without this, search is unusable. | LOW | Inline keyboard with preset categories + custom text input. 2GIS Places API supports text query (`q` param) and category IDs. |
| Search execution via 2GIS API | Core value proposition. If scraping doesn't work, there is no product. | MEDIUM | Must handle pagination (page/page_size params), rate limiting, and missing fields gracefully. Demo key: max 10 results/page, 5 pages. Paid key limits TBD -- needs validation at runtime. |
| Contact data extraction | Users expect: name, phone, email, website, address. This is the entire point of the tool. | LOW | 2GIS API returns these via `fields` parameter. Not all orgs have all fields -- must handle nulls. Social media pages also available. |
| CSV export and delivery | Standard output format for lead scrapers. Every competitor (parser-2gis, parselab, Google Maps scrapers) exports CSV. | LOW | Python csv module. Telegram sendDocument for delivery. 50MB Telegram limit is not a concern for CSV. |
| Summary statistics after scrape | Users need to know what they got: total found, how many have phone/email/website. Every competitor shows this. | LOW | Simple counters during data processing. Display as message alongside CSV file. |
| Progress indication during scrape | Scraping 50-200 leads takes time (multiple API pages). Silent waiting feels broken. | LOW | Telegram edit_message to update "Scraping... 30/50 found" in real-time. Essential UX for any async operation in a chat bot. |
| Error handling and user feedback | API failures, rate limits, empty results, invalid input -- user must know what happened. | LOW | Catch HTTP errors, empty result sets, API key issues. Send clear Russian-language error messages. |
| Duplicate filtering across sessions | PROJECT.md explicitly requires this. Competitors (Kanbox, Scrupp) all have dedup. Without it, repeated scrapes produce overlapping garbage data. | MEDIUM | Requires persistent storage (SQLite). Match on org ID from 2GIS (unique identifier). Flag or skip previously scraped orgs. |
| Search history | PROJECT.md requires this. User needs to see what they've already scraped. Common in all lead tools. | MEDIUM | SQLite table: timestamp, category, city, result count. /history command to display past scrapes. |
| /start with main menu | Standard Telegram bot UX. Every bot has this. Without it, user doesn't know what the bot does. | LOW | Welcome message + inline keyboard with available actions. |

### Differentiators (Competitive Advantage)

Features that set the product apart. Not expected from a personal tool, but create real value.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Working hours and rating data in export | Most basic 2GIS parsers grab name/phone/email. Including working hours, rating, and review count helps prioritize outreach (e.g., target businesses with low online presence, or contact during business hours). | LOW | 2GIS API provides these via fields parameter. Just add columns to CSV. Near-zero extra effort for meaningful value. |
| Social media links extraction | Cold DM campaigns need Instagram/VK/Facebook links. 2GIS API returns social media pages. Most simple parsers skip this field. | LOW | Parse social_media links from API response. Add as CSV columns. Directly useful for cold DM workflows beyond just email. |
| Smart dedup with "new leads only" mode | Instead of just skipping dupes, offer a mode that only returns NEW organizations not seen in any previous scrape. Turns the bot from "one-shot scraper" into an ongoing lead pipeline. | MEDIUM | Query SQLite before adding to results. User sees: "Found 47 total, 23 new (24 previously scraped)". Makes repeat usage valuable rather than annoying. |
| Multiple export formats (CSV + XLSX) | Some CRM tools and Russian market tools prefer XLSX. Adding it costs almost nothing with openpyxl. | LOW | openpyxl library. Offer format choice via inline keyboard before export. |
| Inline category search / suggest | Instead of fixed category lists, let users type partial text and get matching 2GIS categories via Suggest API. Better discovery than a hardcoded list. | MEDIUM | 2GIS Suggest API exists for this purpose. Implement as conversational flow: user types partial name, bot suggests matches. |
| Filter by data completeness | "Only show orgs that have email" or "Only show orgs with website". Useful for cold email campaigns where you need verified contact info. | LOW | Post-processing filter on scraped data. Add inline keyboard: "All results" / "With email only" / "With phone only" / "With website only". |
| Re-download previous exports | Ability to re-download a past CSV without re-scraping. Saves API quota and time. | LOW | Store CSV files on disk or in-memory with session ID. /downloads command shows recent exports. |

### Anti-Features (Commonly Requested, Often Problematic)

Features that seem good but create problems for a personal-use tool.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Multi-city support in v1 | "I want to scrape Saint Petersburg too" | Adds complexity to UX (city selection flow), data model (city column in dedup), and testing matrix. PROJECT.md explicitly defers this. Moscow alone is sufficient for validating the concept. | Hardcode Moscow. Design schema to support city field for future expansion. Add city as a quick config change in v2. |
| Built-in email sending / cold outreach | "Why not send emails directly from the bot?" | Completely different domain: email deliverability, SMTP setup, warmup, bounce handling, CAN-SPAM/GDPR. Scope explosion. PROJECT.md explicitly excludes this. | Export CSV -> import into dedicated tools (Instantly, Lemlist, Smartlead). The bot's job ends at data collection. |
| Email verification / validation | "Verify emails are deliverable before export" | Requires external API (ZeroBounce, NeverBounce), costs money per verification, adds latency, and introduces a dependency that can break. For a personal tool, just check format validity. | Basic email format regex validation in CSV. Flag obviously invalid patterns. Manual verification via external tool if needed. |
| Web dashboard / analytics UI | "I want charts showing my scraping history" | Massive scope increase. Requires web server, frontend, auth. For a personal tool used by one person, Telegram chat history IS the dashboard. | /stats command showing totals in text. /history for past scrapes. Telegram chat is the interface. |
| Automated scheduled scraping | "Run scrape every Monday automatically" | Requires scheduler infrastructure (cron/celery), persistent process, monitoring. Overengineered for personal use where you scrape on-demand. | Manual trigger via /scrape command. If scheduling needed later, a simple cron calling the bot is enough. |
| Proxy rotation / anti-detection | "2GIS might block me" | You're using the official API with a paid key, not scraping the website. API access with a valid key doesn't need proxy rotation. This is a non-problem. | Use official API. Monitor for rate limit errors. Add exponential backoff if needed. |
| Google Maps / Yandex Maps dual-source | "Cross-reference with other directories" | Different APIs, different data formats, different rate limits. Doubles complexity for marginal value. 2GIS has the best business directory coverage in Russian cities. | Stick with 2GIS. It has superior organization data for the Russian market compared to Google Maps. |
| Natural language / AI-powered search | "Use AI to understand what I want to scrape" | LLM integration for query parsing is overkill for a category picker. Adds cost, latency, and unpredictability. | Preset categories + custom text input covers 99% of use cases. 2GIS API's own search handles fuzzy matching. |

## Feature Dependencies

```
[2GIS API Integration]
    |
    +--requires--> [Category/Niche Selection]
    |                  |
    |                  +--enhances--> [Inline Category Suggest via Suggest API]
    |
    +--requires--> [Contact Data Extraction]
    |                  |
    |                  +--enables--> [CSV Export]
    |                  |                 |
    |                  |                 +--enhances--> [XLSX Export]
    |                  |                 +--enhances--> [Re-download Past Exports]
    |                  |
    |                  +--enables--> [Summary Statistics]
    |                  +--enhances--> [Social Media Links Extraction]
    |                  +--enhances--> [Working Hours / Rating Data]
    |
    +--enables--> [Progress Indication]
    +--enables--> [Error Handling]

[SQLite Database]
    |
    +--enables--> [Duplicate Filtering]
    |                  |
    |                  +--enhances--> [Smart "New Leads Only" Mode]
    |
    +--enables--> [Search History]

[/start Menu]  (independent, build first)

[Filter by Data Completeness]  --requires--> [Contact Data Extraction]
```

### Dependency Notes

- **CSV Export requires Contact Data Extraction:** Can't export what you haven't scraped.
- **Duplicate Filtering requires SQLite:** Must persist organization IDs across sessions to detect dupes.
- **Smart "New Leads Only" requires Duplicate Filtering:** Enhancement builds on the dedup foundation.
- **Inline Category Suggest requires working API integration:** Needs Suggest API alongside Places API.
- **Filter by Data Completeness requires Contact Data Extraction:** Post-processing step on scraped data.
- **Summary Statistics requires Contact Data Extraction:** Counts come from processing the scraped batch.
- **Re-download requires stored export files:** Either keep files on disk or regenerate from DB.

## MVP Definition

### Launch With (v1)

Minimum viable product -- what's needed to validate the concept.

- [ ] /start with welcome message and main menu -- entry point to the bot
- [ ] /scrape flow: category selection (preset list + custom input) -- core UX
- [ ] Lead count input (default 50) -- control how much to scrape
- [ ] 2GIS Places API search with pagination -- the scraping engine
- [ ] Contact data extraction (name, phone, email, website, address, rating) -- the data
- [ ] CSV generation and Telegram delivery -- the output
- [ ] Summary statistics (total found, with phone/email/website counts) -- feedback
- [ ] Progress indication ("Scraping... 30/50") -- UX during async work
- [ ] Error handling with Russian-language messages -- reliability
- [ ] SQLite storage of scraped org IDs -- foundation for dedup
- [ ] Duplicate detection (skip previously seen orgs) -- prevents garbage data
- [ ] Basic search history (/history command) -- recall past work

### Add After Validation (v1.x)

Features to add once core is working.

- [ ] Social media links in CSV -- when starting cold DM campaigns
- [ ] Working hours and review count in CSV -- when prioritizing outreach targets
- [ ] Smart "new leads only" mode with counts -- when doing repeat scrapes for same category
- [ ] Filter by data completeness ("only with email") -- when CSV has too many incomplete rows
- [ ] XLSX export option -- if CSV import into Russian CRM tools is painful
- [ ] Re-download previous exports -- when accidentally deleting chat messages

### Future Consideration (v2+)

Features to defer until the tool proves its value.

- [ ] Multi-city support (SPb, Novosibirsk, etc.) -- when Moscow leads are exhausted
- [ ] Inline category suggest via 2GIS Suggest API -- when preset categories feel limiting
- [ ] Export to Google Sheets directly -- if CSV->Sheets workflow becomes tedious
- [ ] Multiple output templates (different CSV column orders for different tools) -- if using multiple outreach platforms

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| /start menu | HIGH | LOW | P1 |
| /scrape flow with category selection | HIGH | MEDIUM | P1 |
| 2GIS API integration + pagination | HIGH | MEDIUM | P1 |
| Contact data extraction | HIGH | LOW | P1 |
| CSV export + Telegram delivery | HIGH | LOW | P1 |
| Summary statistics | MEDIUM | LOW | P1 |
| Progress indication | MEDIUM | LOW | P1 |
| Error handling | HIGH | LOW | P1 |
| SQLite + duplicate detection | HIGH | MEDIUM | P1 |
| Search history | MEDIUM | LOW | P1 |
| Social media extraction | MEDIUM | LOW | P2 |
| Working hours / rating in CSV | MEDIUM | LOW | P2 |
| Smart "new leads only" mode | HIGH | MEDIUM | P2 |
| Filter by data completeness | MEDIUM | LOW | P2 |
| XLSX export | LOW | LOW | P2 |
| Re-download past exports | LOW | LOW | P2 |
| Multi-city support | MEDIUM | MEDIUM | P3 |
| Inline category suggest | LOW | MEDIUM | P3 |
| Google Sheets export | LOW | MEDIUM | P3 |

**Priority key:**
- P1: Must have for launch -- core scraping flow and data delivery
- P2: Should have, add when core is validated -- enrichment and convenience
- P3: Nice to have, future consideration -- scope expansion

## Competitor Feature Analysis

| Feature | parser-2gis (GitHub) | parselab.org | Google Maps Scraper (omkarcloud) | tele2gis (GitHub) | Our Approach |
|---------|---------------------|--------------|----------------------------------|-------------------|--------------|
| Data extraction | Name, address, phone, email, website, hours, coords, social, postal code | Same + SQL export, multi-city in one file | 50+ fields including email enrichment from websites | Name, phone, location only | All fields available from 2GIS API. Match parser-2gis coverage. |
| Output formats | CSV, XLSX, JSON | Excel, JSON, SQL | CSV, JSON, Excel | In-chat text only | CSV primary, XLSX as P2 enhancement |
| Deduplication | No | No | No (but lists-based) | No | Built-in cross-session dedup via SQLite. Key differentiator for repeat use. |
| Interface | Desktop GUI | Web service | CLI / Web UI | Telegram bot | Telegram bot. Unique in the 2GIS parser space -- no competitor combines 2GIS + Telegram + dedup. |
| Category browsing | Syncs rubrics from 2GIS | Category tree | Google Maps categories | Free text query | Preset common categories + custom text input. P3: Suggest API. |
| Multi-city | All supported countries | All cities | Global | Single query | Moscow only (v1). Schema supports future expansion. |
| Progress tracking | Real-time file writes | Status bar | Progress percentage | None | Telegram message updates during scrape |
| Anti-bot measures | Chrome-based bypass | Server-side | Multiple search strategies | None needed (API) | None needed -- using official paid API key |
| Cost | Free (LGPL) | Paid service | Free + paid enrichment | Free | Free (personal tool, API key cost only) |

## Sources

- [2GIS Places API Overview](https://docs.2gis.com/en/api/search/places/overview) -- Official API docs, HIGH confidence
- [2GIS Places API Examples](https://docs.2gis.com/en/api/search/places/examples) -- Pagination details (page_size, page params), HIGH confidence
- [2GIS Search APIs Overview](https://docs.2gis.com/en/api/search/overview) -- Available search APIs and data fields, HIGH confidence
- [parser-2gis on GitHub](https://github.com/interlark/parser-2gis) -- Competitor feature analysis, HIGH confidence
- [parselab.org 2GIS parser](https://parselab.org/parser-2gis.html) -- Commercial competitor features, MEDIUM confidence
- [Google Maps Scraper (omkarcloud)](https://github.com/omkarcloud/google-maps-scraper) -- Cross-domain competitor features (50+ data points, enrichment), MEDIUM confidence
- [tele2gis on GitHub](https://github.com/13h3r/tele2gis) -- Only existing 2GIS+Telegram bot, limited features, HIGH confidence
- [Apify lead scraper tools comparison](https://blog.apify.com/best-lead-scraping-tools/) -- General lead scraper landscape 2026, MEDIUM confidence
- [Salesforge lead scraping tools](https://www.salesforge.ai/blog/lead-scraping-tools) -- Feature landscape across 11 tools, MEDIUM confidence
- [n8n community workflow](https://community.n8n.io/t/free-workflow-csv-lead-sourcing-icp-filter-dedupe-google-sheets/273356) -- Dedup and filtering patterns in lead workflows, LOW confidence

---
*Feature research for: 2GIS Lead Scraper Telegram Bot*
*Researched: 2026-03-09*
