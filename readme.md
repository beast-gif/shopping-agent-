# AR7 Shopping Agent

A personal shopping agent for Bengaluru. Ask in plain words, and it compares the apps,
fills the cart, and leaves only the payment to you.

- **Food:** "biryani" finds the best restaurants on **Swiggy and Zomato**, merges the same
  restaurant across both, shows one combined menu, builds the cart on both apps and shows
  the **real bills** (fees, taxes, offers), so you pick the cheaper one.
- **Restaurant by name:** "meghana foods" goes straight to its menu.
- **Coupons and offers:** Zomato applies its best offer by itself; for Swiggy, type a code from your
  Swiggy app (`coupon SWIGGYIT`) and the bill is refreshed with Swiggy's real total.
- **Groceries on five apps:** "guilt free ice cream under 200" searches **Zepto, Amazon Now,
  Swiggy Instamart, Blinkit and Flipkart Minutes** at the same time, reads nutrition labels, matches
  the same product across all of them, ranks by a health score, and compares the real bills. You can
  fill a **basket** (ice cream, chips, curd) and get one bill per app. Zepto, Amazon Now and Instamart
  get real carts and bills; Blinkit and Flipkart Minutes are compare-only (listed prices plus a link
  to buy in their app).
- **Web page with cards:** product, dish and restaurant cards with photos, every app's price, the
  nutrition label and +/− quantity buttons, served from your laptop.
- **Location aware:** delivers to the right saved address (home, college ...) from Wi-Fi/GPS, or
  you tap the place.
- **Payment stays with you:** nothing is ordered unless you type the exact `confirm <total>` and pay
  the UPI QR yourself (Zepto / Instamart / Swiggy / Zomato), or press *Proceed to pay* yourself
  (Amazon Now).

Built with **LangGraph** (agent workflows with human-in-the-loop), the apps' official **MCP servers**
(Zepto, Swiggy Food, Swiggy Instamart, Zomato), **Apify's MCP server** (Blinkit and Flipkart Minutes
scrapers), **Playwright** (Amazon Now through your own logged-in browser), and an OpenAI-compatible
LLM (**Velona**).

---

## Contents

- [How a request looks](#how-a-request-looks)
- [Architecture](#architecture)
- [Grocery apps](#grocery-apps)
- [How it decides](#how-it-decides)
- [Safety](#safety)
- [Setup](#setup)
- [Running it](#running-it)
- [Project structure](#project-structure)
- [Configuration](#configuration)
- [Files it creates](#files-it-creates)
- [Troubleshooting](#troubleshooting)
- [Limitations and roadmap](#limitations-and-roadmap)

---

## How a request looks

An illustrative session (prices and fees vary):

```
You: guilt free ice cream, masala kaju under 300
  → Where should it be delivered?            [home] [clg] [work] ...
  → Searching Zepto, Amazon Now, Instamart, Blinkit, Flipkart Minutes...
      Zepto: 15 products, 14 look like ice cream · nutrition labels: read 14, 10 had numbers
      Blinkit: 10 rows in 46 s · Flipkart Minutes: 11 rows in 11 s · Apify so far: about $0.09
  →  #  product                             size   Zepto Amazon Instamart Blinkit Flipkart  ₹/100 kcal sugar   G
      1  Get-A-Way Chocolate Brownie Fudge  100 ml   ₹78   ₹60*        —       —        —     60   111  2.4 0.59 ✓
      4  Go Zero Belgian Dark Chocolate     100 ml   ₹94   ₹53*      ₹58     ₹95      ₹73     53    97  3.0 0.47 ✓
     > 4x2
  → (masala kaju list) > 3
  → Your basket ... [bill]
  → Real bills from each app:
      1  Zepto         items ₹268  delivery ₹30, other ₹5           TO PAY ₹303
      2  Amazon Now    items ₹186  handling ₹0, delivery ₹0         TO PAY ₹186
      3  Instamart     items ₹196  handling ₹12, delivery partner …  TO PAY ₹244
      4  Blinkit       items ₹270  fees not known (compare-only)    ≥ ₹270
     Cheapest with everything: 2. Amazon Now at ₹186
  → Type 'open' → your Amazon cart opens in Brave → you press Proceed to pay
```

---

## Architecture

```
 Web page (index.html) ──WebSocket──┐
                                    ├──> Engine ──> Main graph (LangGraph)
 Terminal (chat.py) ────────────────┘      │            │
                                           │        router (LLM: grocery / food / unknown)
                                           │        ├── food subgraph     (Swiggy ∥ Zomato)
                                           │        └── grocery subgraph  (Zepto ∥ Amazon ∥ Instamart ∥ Blinkit ∥ Flipkart)
                                           │                 │
                          SQLite checkpointer          Adapters
                         (.checkpoints/shop.sqlite)    ├── MCP (OAuth 2.1 + PKCE): Zepto, Swiggy Food, Instamart, Zomato
                                                       ├── MCP (API token): Apify → Blinkit, Flipkart Minutes scrapers
                                                       └── Browser: Amazon Now (Brave via Playwright)
```

**Layers**

| Layer | Files | Job |
|---|---|---|
| Front ends | `scripts/web.py`, `app/web/`, `scripts/chat.py` | show events, send what you type/tap |
| Engine | `app/engine.py` | one entry point `send(text)`: a new request or an answer; turns graph pauses/results into events; remembers the unfinished request |
| Graphs | `app/workflows/main.py`, `food.py`, `grocery.py` | the steps of each flow; `interrupt()` wherever you choose |
| Logic | `app/workflows/*_logic.py` | pure functions: matching, ranking, scores, tables, bills (no network) |
| Adapters | `app/adapters/` | one per app; every app's JSON becomes the shared schemas in `app/schemas.py` |
| Plumbing | `app/mcp_client.py`, `app/browser.py`, `app/location.py`, `app/places.py`, `app/payment.py`, `app/approval.py` | MCP sessions + tool filter, browser, place detection, UPI QR, typed approvals |

**Human-in-the-loop with LangGraph.** Every question is an `interrupt({"prompt", "options"})`.
The graph pauses, its state is checkpointed to SQLite, and it resumes with `Command(resume=answer)`.
Food and grocery are compiled graphs run inside one node each of the main graph (subgraphs), and
their pauses travel up to the chat. Rules that keep this safe:

- every node returns `{"route": <next node>}`, so the flow is explicit;
- state holds only plain dicts (JSON-safe for the checkpointer);
- nodes that ask you make **no network calls**, because LangGraph re-runs them on resume.

**Front end ⇄ back end.** The page sends one kind of message, `{"type": "send", "text": ...}`.
The server pushes events as they happen: `log` (live progress), `question` (prompt + buttons),
`image` (UPI QR), `done`, `error`, `busy`. A reload replays the history, and a server restart offers
the unfinished request again.

**Cards.** List questions also carry a `cards` block: products, dishes or restaurants, with photo
links, every app's price, labels and G. The page draws cards with +/− buttons and turns your taps back
into the same text the terminal uses (`#4 × 2, #7 × 1` → `"4x2, 7"`), so the graph reads picks in
one way only. Photos load straight from the apps' image servers. The agent passes along only the
links the apps send.

---

## Grocery apps

| App | Connected through | Search | Cart + real bill | Order | Cost |
|---|---|---|---|---|---|
| **Zepto** | official MCP (`mcp.zepto.co.in`) | ✓ + nutrition labels | ✓ (agent's own cart) | ✓ UPI after `confirm` | free |
| **Amazon Now** | your logged-in Brave (`/tez/` JSON endpoints) | ✓ | ✓ (your real cart: other items set aside and put back) | you press *Proceed to pay* | free |
| **Swiggy Instamart** | official MCP (`mcp.swiggy.com/im`) | ✓ (each pack size is a row) | ✓ (agent's own cart) | ✓ UPI after `confirm` | free |
| **Blinkit** | Apify scraper `s-r/blinkit-scraper` | ✓ (main search term, 10 results) | ✗ compare-only | links to open in the app | Apify credit |
| **Flipkart Minutes** | Apify scraper `clearpath/flipkart-minutes-scraper` | ✓ (main search term, 10 results) | ✗ compare-only | links to open in the app | Apify credit |

**How an Apify search runs.** Calling an Actor returns the *run*, not the products, so it takes three calls:
```
① s-r--blinkit-scraper({queries, locations: [{lat, lon}], max_items: 10, waitSecs: 45})
     → {runId, status: "RUNNING", datasets.default: {id, itemCount: 10}}
② while not finished AND itemCount < 10 AND under 90 s:  get-actor-run(runId, waitSecs: 30)
③ get-dataset-items(datasetId) → the product rows
```
Rows are read as soon as there are enough (Blinkit often has its 10 rows while still "RUNNING"). A run with
no rows after 90 s is aborted. Location is your place's coordinates (from its Zepto address).

**Apify cost.** Apify's free plan gives $5 of credit a month. Per search, roughly:
```
Flipkart Minutes:  ~11 rows × $1.99 / 1000 + $0.005 per run ≈ $0.027
Blinkit:           ~11 rows × $5.00 / 1000 + $0.005 per run ≈ $0.060
                                                        total ≈ $0.09  →  ~55 searches a month
```
The log shows a running estimate (`Apify so far this session: about $…`). `apify.enabled: false` or
`--no-apify` leaves both out.

**Instamart bill (real example).** `get_cart` returns label/value lines:
```
Item Total ₹19.00 · Handling Fee ₹12.00 · Small Cart Fee ₹23.60 · Delivery Partner Fee ₹35.70 · To Pay ₹90
```
These become `items ₹19 | handling ₹12, small cart ₹23.6, delivery partner ₹35.7 | TO PAY ₹90` (the ₹0.30
rounding stays under the ₹0.50 "other" threshold). Small baskets pay large fees, which is why the agent
compares real bills, not listed prices.

---

## How it decides

### Restaurants (food)

```
R = 0.5 · rating/5 + 0.3 · (1 − ETA/ETA_max) + 0.2 · dish_match        (dish_match = 1, or 0.5)
```
- **Same restaurant on both apps:** `token_set_ratio(names) ≥ 85` and their distances agree within 1 km.
- **On only one app's list?** Each app returns its own top list, so a restaurant can be on both but
  listed by one. When you pick a one-app restaurant, the agent searches the other app for its **name**
  and attaches it if the same rule matches (e.g. "Sri Sai Palace": name 100, |1.1 − 1.2| km ≤ 1 ✓).
  The menu and bills then cover both apps.
- **Same dish on both menus:** `token_sort_ratio ≥ 90`, and veg/non-veg compatible.
- **Is "nandhana palace" a restaurant or a dish?** It's a restaurant if a restaurant's name contains your
  words, AND dishes containing those words come from fewer than 2 restaurants whose names don't contain them.

### Coupons

- **Zomato:** its cart applies the best restaurant offer automatically (e.g. "Flat ₹75 OFF").
- **Swiggy:** the agent asks `fetch_food_coupons` for codes and tries up to 4. Swiggy's MCP lists only
  **COD-valid** coupons (`"filter_applied": "COD only"`), and that list is often empty. So at the bills
  you can type `coupon CODE` with a code from your Swiggy app: the agent applies it, reads Swiggy's real
  total back, and recompares the apps.
  ```
  before ₹349 → apply SWIGGYIT → after ₹299 → "coupon SWIGGYIT applied (saves ₹50)"
  ```
- Bank and UPI-app offers are applied at payment, so they never show up here.

### Products (groceries)

**Plan (LLM).** "guilt free ice cream" becomes search terms, category words, exclude words, pack claims and
limits per 100 g/ml, realistic for the category. A limit that no labelled product meets is dropped
automatically, for example "≤ 150 kcal" for chocolate.

**Same product on several apps.**
1. Clean the names: lower case; remove sizes, marketing ("guilt free", "zero added sugar") and packaging
   ("cup", "tub"). "Get- A-Way" becomes "getaway".
2. `token_sort_ratio(clean_a, clean_b) ≥ 90`.
3. Sizes agree: `|a − b| / max(a, b) ≤ 5%`. Multipacks count in full: "125 ml x 5" and "5 x 125 ml" are 625 ml.
4. Apps are added one at a time (Zepto → Amazon → Instamart → Blinkit → Flipkart). A product joins a group
   only if it matches **every** listing already in it; the group's score is the lowest of those. Best pairs
   first, one listing per app per group.

Example: Zepto "Go Zero Belgian Dark Chocolate Guilt Free Ice Cream Cup 100 ml" and Amazon
"Go Zero Belgian Dark Chocolate Ice Cream 100 ml" both clean to `go zero belgian dark chocolate ice cream`
and score 100 ✓. Amazon's 500 ml tub isn't matched (sizes differ by 80%) but **borrows the label**,
since labels are per 100 ml.

**Health score** (per 100 g/ml; max = highest among the results):
```
G = 0.4 · (1 − kcal/kcal_max) + 0.4 · (1 − sugar/sugar_max) + 0.2 · (protein/protein_max)
```
A missing value is dropped and the rest re-weighted (divide by the weights used). Order shown:
✓ within limits (by G) → ? no label (pack claims first) → ✗ outside limits. Requests without a health
qualifier ("milk") are ranked by ₹ per 100 g/ml.

### Location

1. **You said it** ("at college"): matched against place names, aliases and abbreviations (clg, hostel, ...).
2. **Wi-Fi name (SSID)** mapped to a place.
3. **Windows GPS:** pick place P1 if `d1 ≤ 300 m` and `d1 + accuracy < d2`. IP-based fixes and fixes
   worse than 150 m are rejected.
4. **Last used place.** The web page asks with buttons instead (`always_ask`).

Places are set up once (`scripts/setup_places.py`). It groups each app's saved addresses by text
similarity: IDF-weighted shared words, symmetric coverage, × 0.6 for a pincode mismatch.

### Bills

The listed price isn't what you pay, so the agent fills the carts and reads each app's own bill
(Zepto: payment preview; Amazon: the bill its cart page fetches; Instamart: `billBreakdown`;
Swiggy/Zomato: their cart pricing). Unnamed amounts show as `other = TO PAY − items − named fees`, so every
bill adds up. The suggestion is the cheapest app with a real bill that has **all** your items. Blinkit and
Flipkart Minutes show `≥ items total` (fees unknown); if one lists everything for less than the best real
bill, an "Also:" line says so. Choosing one gives you the product links to buy there.

---

## Safety

- **Tool filter:** every MCP tool must be on a per-app allowlist and must not match a blocked pattern
  (`checkout`, `place_`, `order$`, `confirm`, `pay` ...). The agent can't see or call anything else.
- **Orders only through `call_order()`:** a hard-coded rule table, UPI only (no wallets or cards).
- **Typed approval:** `confirm <total rounded up>` is a single-use token bound to one app and amount,
  valid for 5 minutes, max ₹1500 (`ordering.max_order_total`). There's no button for it, on purpose.
- **Bill check:** if the app asks for more than you approved, the agent stops and tells you not to pay.
- **Unpaid orders lapse** after 5 minutes (Swiggy Food's `confirm_order` closes them as failed; Instamart's
  `confirm_order` is only ever called after a successful payment, as Swiggy's docs require).
- **Instamart:** `checkout` is blocked for normal calls; through `call_order` it must be
  `paymentMethod: "UPI"` (no cash, no Swiggy Money), and `confirmDuplicate` is never set.
- **Apify:** the server address limits it to the two scrapers (`?tools=…`), and the allowlist lets the
  agent call only those plus `get-actor-run`, `get-dataset-items` and `abort-actor-run`. No other paid
  Actor can be started.
- **Amazon Now:** the agent can call only 8 read/cart endpoints (no checkout). You press *Proceed to pay*
  yourself. Other items in your real cart are only set aside while comparing and put back afterwards.
- **The web page listens on 127.0.0.1 only.**
- Secrets live in `.env` (Velona key, Apify token) and `.tokens/`, and the Amazon login in `.browser/`.
  All are git-ignored.

---

## Setup

**Needs:** Windows 10/11, Python 3.13, Brave (or Chrome), Swiggy / Zomato / Zepto / Amazon accounts,
a Velona API key, and (for Blinkit / Flipkart Minutes) a free Apify account.

```powershell
cd C:\Users\amrit\Desktop\shopping_agents
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**1. LLM:** copy `.env.example` to `.env` and fill it in:
```
VELONA_BASE_URL=https://velona.in/v1
VELONA_API_KEY=...
VELONA_MODEL=openai/gpt-4o-mini
APIFY_TOKEN=apify_api_...        # Apify Console → Settings → API & Integrations
```
Check it with `python scripts\check_velona.py`.

**2. Log in to the MCP apps** (once; tokens are saved in `.tokens/`):
```powershell
python scripts\list_tools.py
```
A browser opens for each app (Zepto, Swiggy Food, Instamart, Zomato). Log in, and the tool list is written to
`tools_dump.json`. Apify uses the token from `.env` (no browser). Instamart uses the same Swiggy account and
saved addresses as Swiggy Food, with its own login token.

**3. Log in to Amazon** in the agent's own browser profile (once):
```powershell
python scripts\amazon_login.py
```

**4. Set up your places** (once; re-run when you add addresses in the apps):
```powershell
python scripts\setup_places.py
python scripts\where_am_i.py          # check: which place does it detect?
```

---

## Running it

**Web page** (recommended):
```powershell
python scripts\web.py                 # opens http://127.0.0.1:8000
python scripts\web.py --auto-place    # detect the place instead of asking
python scripts\web.py --no-amazon     # don't open Brave
```

**Terminal chat:**
```powershell
python scripts\chat.py
```

**Things to type:**

| Type | Does |
|---|---|
| `biryani` | best restaurants for a dish on both apps |
| `meghana foods` | a restaurant's merged menu |
| `chicken momos at college` | food to a named place |
| `guilt free ice cream under 200` | groceries with a budget |
| `ice cream, chips, curd` | a grocery basket, one bill |
| `3x2, 7` | (in a list) 2 of #3 and 1 of #7 |
| `/paneer` | (in a list) search the list; `/` = top again, `/*` = everything |
| `coupon SWIGGYIT` | (at the food bills) apply a Swiggy coupon code from your app |
| `back`, `skip`, `cancel`, `bill`, `remove 2` | as shown under each question |

On the web page you mostly tap: place and budget buttons, **ADD** / **− n +** on cards, then
**Add to basket** or **Get the bills**. Typing still works everywhere. Only `confirm <total>` has
no button, on purpose.

**Test one part at a time:**
```powershell
python scripts\try_food.py biryani
python scripts\try_grocery.py "guilt free ice cream, dark chocolate"     # quote commas in PowerShell
python scripts\try_grocery.py curd --no-apify --no-instamart   # leave apps out
python scripts\try_amazon.py ice cream --cart
python scripts\probe_apify.py         # Blinkit + Flipkart Minutes: tools, one small search each
python scripts\probe_instamart.py     # Instamart: tools, addresses, a search, the cart (--cart-test)
python scripts\try_adapters.py
python scripts\probe.py               # raw MCP responses into probes/
```

---

## Project structure

```
shopping_agents/
├─ app/
│  ├─ adapters/          one adapter per app → shared schemas
│  │  ├─ base.py            interfaces + parsing helpers (prices, pack sizes, nutrition text, payments)
│  │  ├─ zepto.py           MCP: addresses, search, labels, cart, UPI order
│  │  ├─ swiggy.py          MCP: restaurants, menu, cart, coupons, UPI order
│  │  ├─ zomato.py          MCP: restaurants, menu, cart, UPI checkout
│  │  ├─ instamart.py       MCP: search (per pack size), cart, bill, UPI order
│  │  ├─ apify.py           Apify runner + Blinkit, Flipkart Minutes (compare-only)
│  │  └─ amazon_now.py      browser: search, product, cart (atc), bill, set aside / put back
│  ├─ workflows/
│  │  ├─ main.py            router → food / grocery subgraphs
│  │  ├─ food.py            food graph          food_logic.py      merging, R score, menus, bills
│  │  └─ grocery.py         grocery graph       grocery_logic.py   plan, matching, G score, basket, bills
│  ├─ web/
│  │  ├─ server.py          FastAPI + WebSocket + event hub
│  │  └─ static/index.html  the page
│  ├─ engine.py          Services (connections) + Engine (send / restore / events)
│  ├─ router.py          intent router (one structured LLM call)
│  ├─ mcp_client.py      MCP hub: OAuth, tool filter, order rules
│  ├─ browser.py         the agent's Brave/Chrome profile
│  ├─ location.py        Wi-Fi / GPS / last place
│  ├─ places.py          places.yaml + address grouping
│  ├─ payment.py         UPI QR + waiting for payment
│  ├─ approval.py        'confirm <total>' tokens
│  ├─ llm.py, config.py, schemas.py, state.py, debug.py
├─ scripts/             runnable entry points (web, chat, try_*, setup, probes)
├─ config.yaml          all settings (LLM via ${ENV} from .env)
├─ requirements.txt
└─ .env.example
```

---

## Configuration

Everything is in `config.yaml`. The parts you're most likely to change:

| Key | Default | Meaning |
|---|---|---|
| `ordering.enabled` | `true` | `false` = compare and fill carts only, never order |
| `ordering.max_order_total` | `1500` | refuse any order above this (₹) |
| `ordering.payment_timeout_seconds` | `300` | an unpaid order lapses after this |
| `scoring.grocery.w_kcal / w_sugar / w_protein` | `0.4 / 0.4 / 0.2` | health-score weights (keep the sum at 1) |
| `scoring.grocery.product_match_threshold` | `90` | name score for "same product" |
| `scoring.grocery.details_limit` | `20` | Zepto labels read per search |
| `scoring.food.w_rating / w_eta / w_dish` | `0.5 / 0.3 / 0.2` | restaurant-rank weights |
| `scoring.food.top_restaurants` | `10` | restaurants shown |
| `locations.match_radius_m` | `300` | a GPS fix must be this close to a place |
| `amazon_now.channel` | `brave` | `brave`, `chrome` or `chromium` |
| `amazon_now.executable_path` | `""` | path to brave.exe if it isn't found automatically |
| `apify.enabled` | `true` | `false` = never call Apify (no Blinkit / Flipkart Minutes) |
| `apify.max_items` | `10` | results per app per search (Blinkit's free cap is 10); more = more matches, more cost |
| `apify.timeout_s` | `90` | stop waiting for a slow scraper |
| `apify.usd_per_1000` | `blinkit 5.0, flipkart_minutes 1.99` | only for the cost estimate in the log |
| `mcp.servers.<app>.enabled` | `true` | turn one app off (e.g. `instamart`) |

---

## Files it creates

All git-ignored.

| Path | What |
|---|---|
| `.tokens/` | MCP OAuth tokens (Zepto, Swiggy Food, Instamart, Zomato) |
| `.browser/amazon_brave/` | the agent's browser profile (Amazon login) |
| `places.yaml` | your places ↔ each app's address IDs, plus Wi-Fi names |
| `.checkpoints/shop.sqlite` | LangGraph checkpoints (conversations) |
| `.state/` | Zepto device ID, last place, unfinished request, UPI QR images |
| `probes/` | raw app responses saved for debugging (bills, coupons, Amazon recon, Apify rows, Instamart carts) |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `try_grocery.py ice cream, chips` searches "ice cream chips" | PowerShell drops unquoted commas: use quotes or `+` (`ice cream + chips`). Commas typed in the chat are fine. |
| LLM 404 | `VELONA_BASE_URL` must be `https://velona.in/v1` (not `/gateway/v1`) |
| "Brave not found" | set `amazon_now.executable_path` to `brave.exe` |
| "Amazon not signed in" | `python scripts\amazon_login.py` |
| Amazon left out at college | the agent can't switch Amazon addresses yet: change it at the top of Amazon Now |
| An MCP login loops or fails | delete that app's file in `.tokens/` and run `scripts\list_tools.py` again |
| Swiggy says "no coupons for this cart" | Swiggy's MCP lists only COD coupons: type `coupon CODE` from your Swiggy app at the bills |
| A restaurant shows one app but is on both | tap it: the agent looks it up on the other app by name and says "Also on Zomato: …" |
| A bill shows "other ₹X" | an unnamed fee; send `probes/grocery_quotes.json` / `probes/last_quotes.json` to name it |
| Apify: "no token" / not connected | put `APIFY_TOKEN=...` in `.env` |
| Blinkit takes ~45 s | normal: its scraper is slow; the search waits for the slowest app (`--no-apify` to skip) |
| Apify credit ran out | Apify blocks runs until next month; set `apify.enabled: false` meanwhile |
| Instamart: "The store is unavailable at the moment" | from `get_cart` before a cart exists; the agent uses the cart `update_cart` returned instead |
| The page shows Python code instead of the chat | `app\web\static\index.html` was overwritten: copy the real `index.html` back (it starts with `<!doctype html>`) and press Ctrl+F5 |
| The page says "offline" | the server stopped: run `python scripts\web.py` again (the unfinished request is offered) |

---

## Limitations and roadmap

**Not supported yet**
- Switching Amazon Now's delivery address (it's used only when its pincode matches your place).
- Dishes with size or add-on choices.
- Finding Swiggy coupon codes by itself (Swiggy's MCP lists only COD coupons; typed codes work).
- Carts and orders on Blinkit and Flipkart Minutes (Apify scrapers are read-only: compare + links).
- Nutrition labels from Amazon, Instamart, Blinkit or Flipkart (matched products use Zepto's label).

**Not yet run against the real apps**
- Zepto and Instamart UPI orders.
- Amazon's new-order detection.
- Putting set-aside Amazon items back.
- Typed Swiggy coupons that are UPI-only (whether `apply_food_coupon` accepts them).

**Next**
1. Amazon address switching (one more capture run).
2. Order status, history and "order the same as last time".
3. Saved preferences (veg only, default budget, favourite brands).
4. Dishes with sizes and add-ons.

---

*Personal project. It uses the apps' official MCP servers where they exist, your own logged-in browser
for Amazon Now, and third-party Apify scrapers (read-only) for Blinkit and Flipkart Minutes. Use it on your
own accounts, responsibly.*
