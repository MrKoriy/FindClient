# Stack Research

## Recommended Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | 3.10+ | Best ecosystem for TG-bots, async support |
| TG Framework | aiogram | 3.x (latest) | Modern async, Router system, FSM, inline keyboards |
| HTTP Client | aiohttp | (comes with aiogram) | Async requests to 2GIS API |
| Database | SQLite + aiosqlite | — | Zero-config, async, enough for personal tool |
| CSV | csv (stdlib) | — | Built-in, utf-8-sig for Excel compatibility |
| Config | python-dotenv | — | .env file for API keys |

## 2GIS API Details

### Endpoint
```
GET https://catalog.api.2gis.com/3.0/items
```

### Authentication
Query parameter: `key=YOUR_API_KEY`

### Key Parameters
| Parameter | Description |
|-----------|-------------|
| `q` | Search query (e.g. "рестораны") |
| `city_id` | City filter (Москва = specific ID) |
| `fields` | Additional fields to return |
| `page` | Page number |
| `page_size` | Items per page (max 10 for demo, higher for paid) |
| `sort` | Sorting: `distance` or `rating` |
| `has_site` | Filter by website presence |
| `type` | Object type filter |

### Fields Parameter (critical)
```
fields=items.contact_groups,items.org,items.address,items.point,items.schedule,items.external_content,items.reviews
```

- `items.contact_groups` — phones, emails, websites, social links (**may require paid key**)
- `items.org` — organization info
- `items.address` — full address with components
- `items.reviews` — rating and review count
- `items.schedule` — working hours
- `items.external_content` — possible social media links

### Pagination Limits
- **Demo key**: 10 items/page, max 5 pages = **50 items total**
- **Paid key**: Higher limits (undocumented, needs testing)

### Moscow city_id
Need to discover via API: search with `q=Москва` in regions endpoint, or use known ID.

## aiogram 3 Key Patterns

### Router + FSM Architecture
```python
from aiogram import Router
from aiogram.fsm.state import State, StatesGroup

class ScrapeStates(StatesGroup):
    choosing_niche = State()
    choosing_count = State()
    scraping = State()

router = Router()
```

### Inline Keyboards for Niche Selection
```python
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Строительство", callback_data="niche:construction")],
    [InlineKeyboardButton(text="Медицина", callback_data="niche:medicine")],
])
```

### Callback Query Handlers
```python
@router.callback_query(F.data.startswith("niche:"))
async def handle_niche(callback: CallbackQuery, state: FSMContext):
    niche = callback.data.split(":")[1]
    await state.update_data(niche=niche)
    await state.set_state(ScrapeStates.choosing_count)
```

### Send Document (CSV file)
```python
from aiogram.types import BufferedInputFile

csv_bytes = generate_csv(results)
file = BufferedInputFile(csv_bytes, filename="leads.csv")
await message.answer_document(file, caption="Готово!")
```

## SQLite Schema (for history + dedup)

```sql
CREATE TABLE searches (
    id INTEGER PRIMARY KEY,
    niche TEXT,
    count INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE leads (
    id INTEGER PRIMARY KEY,
    org_id TEXT UNIQUE,  -- 2GIS organization ID for dedup
    name TEXT,
    phone TEXT,
    email TEXT,
    website TEXT,
    address TEXT,
    rating REAL,
    socials TEXT,  -- JSON array
    search_id INTEGER REFERENCES searches(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## Dependencies (requirements.txt)

```
aiogram>=3.4
aiohttp
aiosqlite
python-dotenv
```

## Key Risks

1. **`items.contact_groups` may be paid-only** — must validate with actual API key before building parser
2. **Demo key hard caps at 50 results** — need paid key for larger scrapes
3. **No documented rate limits** — need empirical testing, add delays between pages
4. **CSV encoding** — must use `utf-8-sig` for Cyrillic in Excel
5. **aiogram MemoryStorage** — default FSM storage loses state on restart (fine for personal use)
