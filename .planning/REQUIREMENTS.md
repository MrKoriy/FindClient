# Requirements: 2GIS Lead Scraper Bot

**Defined:** 2026-03-09
**Core Value:** Быстрый сбор контактов бизнесов по нише в один клик — от выбора категории до готового CSV за минуту.

## v1 Requirements

### Foundation

- [x] **FOUND-01**: Bot starts with `python -m bot` or `python main.py` and connects to Telegram
- [x] **FOUND-02**: Config loads BOT_TOKEN and TWOGIS_API_KEY from .env file
- [x] **FOUND-03**: 2GIS API client fetches organizations by search query in Moscow
- [x] **FOUND-04**: 2GIS API client handles pagination (multiple pages of results)
- [x] **FOUND-05**: 2GIS API client extracts contact data: name, phone, email, website, address, rating, socials

### Bot UX

- [ ] **UX-01**: /start command shows welcome message with main menu (inline keyboard)
- [ ] **UX-02**: /scrape command starts the lead collection flow
- [ ] **UX-03**: Bot shows preset niches: primary (строительство/ремонт, медицина/клиники, ресторан/кафе, юридические услуги)
- [ ] **UX-04**: Bot shows preset niches: secondary (салоны красоты/барбершопы, авторемонт, фитнес/спорт, доставка еды, образование/репетиторы)
- [ ] **UX-05**: Bot allows custom niche text input
- [ ] **UX-06**: Bot asks for lead count with default 50
- [ ] **UX-07**: Bot shows progress during scraping (message edits with current count)
- [ ] **UX-08**: Bot sends CSV file with results when scraping completes
- [ ] **UX-09**: Bot sends summary stats: total found, count with phone, with email, with website, with socials
- [ ] **UX-10**: Bot handles errors gracefully with Russian-language messages

### Data Export

- [ ] **EXPORT-01**: CSV file contains columns: название, телефон, email, сайт, адрес, рейтинг, соцсети
- [ ] **EXPORT-02**: CSV uses utf-8-sig encoding for Cyrillic compatibility in Excel
- [ ] **EXPORT-03**: CSV is generated in-memory (not written to disk)
- [ ] **EXPORT-04**: Missing fields show empty string (not "None" or "null")

### History & Dedup

- [ ] **HIST-01**: SQLite database stores all scraped organization IDs
- [ ] **HIST-02**: Duplicate organizations from past scrapes are filtered out
- [ ] **HIST-03**: /history command shows past searches (niche, date, count)
- [ ] **HIST-04**: Database persists between bot restarts

## v2 Requirements

### Enrichment

- **ENRICH-01**: "New leads only" mode — show only orgs not found in previous scrapes with transparent counts
- **ENRICH-02**: Filter by data completeness ("only orgs with email")
- **ENRICH-03**: Working hours in CSV
- **ENRICH-04**: XLSX export option

### Multi-city

- **CITY-01**: User can select city before scraping (Moscow, SPb, Novosibirsk)
- **CITY-02**: Category suggest via 2GIS Suggest API

## Out of Scope

| Feature | Reason |
|---------|--------|
| Email sending / cold outreach | Scope explosion, different domain |
| Web dashboard / analytics UI | Telegram is the only interface |
| Automated scheduled scraping | Overkill for personal use |
| Proxy rotation | Using official API, not web scraping |
| Multi-user access / auth | Personal tool only |
| AI-powered search | Preset categories + free text covers 99% |
| Google Sheets export | Defer to v2+ |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| FOUND-01 | Phase 1 | Complete |
| FOUND-02 | Phase 1 | Complete |
| FOUND-03 | Phase 1 | Complete |
| FOUND-04 | Phase 1 | Complete |
| FOUND-05 | Phase 1 | Complete |
| HIST-01 | Phase 2 | Pending |
| HIST-02 | Phase 2 | Pending |
| HIST-04 | Phase 2 | Pending |
| EXPORT-01 | Phase 3 | Pending |
| EXPORT-02 | Phase 3 | Pending |
| EXPORT-03 | Phase 3 | Pending |
| EXPORT-04 | Phase 3 | Pending |
| UX-01 | Phase 4 | Pending |
| UX-02 | Phase 4 | Pending |
| UX-03 | Phase 4 | Pending |
| UX-04 | Phase 4 | Pending |
| UX-05 | Phase 4 | Pending |
| UX-06 | Phase 4 | Pending |
| UX-07 | Phase 4 | Pending |
| UX-08 | Phase 4 | Pending |
| UX-09 | Phase 4 | Pending |
| UX-10 | Phase 4 | Pending |
| HIST-03 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 23 total
- Mapped to phases: 23
- Unmapped: 0

---
*Requirements defined: 2026-03-09*
*Last updated: 2026-03-09 after initial definition*
