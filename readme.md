# AR7 Shopping Agent

A personal shopping agent for Bengaluru. Ask in plain words, and it compares the apps,
fills the cart, and leaves only the payment to you.

- **Food:** "biryani" finds the best restaurants on **Swiggy and Zomato**, merges the same
  restaurant across both, shows one combined menu, builds the cart on both apps and shows
  the **real bills** (fees, taxes, offers), so you pick the cheaper one.
- **Restaurant by name:** "meghana foods" goes straight to its menu.
- **Coupons and offers:** Zomato applies its best offer by itself; for Swiggy, type a code from your
  Swiggy app (`coupon SWIGGYIT`) and the bill is refreshed with Swiggy's real total.
- **Groceries:** "guilt free ice cream under 200" searches **Zepto and Amazon Now**, reads
  nutrition labels, matches the same product across both apps, ranks by a health score, and
  compares the real bills. You can fill a **basket** (ice cream, chips, curd) and get one bill
  per app.
- **Web page with cards:** product, dish and restaurant cards with photos, both apps' prices, the
  nutrition label and +/− quantity buttons, served from your laptop.
- **Location aware:** delivers to the right saved address (home, college ...) from Wi-Fi/GPS, or
  you tap the place.
- **Payment stays with you:** nothing is ordered unless you type the exact `confirm <total>` and pay
  the UPI QR yourself (Zepto / Swiggy / Zomato), or press *Proceed to pay* yourself (Amazon Now).

Built with **LangGraph** (agent workflows with human-in-the-loop), the apps' official **MCP servers**
(Zepto, Swiggy, Zomato), **Playwright** (Amazon Now through your own logged-in browser), and an
OpenAI-compatible LLM (**Velona**).

---

## Contents

- [How a request looks](#how-a-request-looks)
- [Architecture](#architecture)
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
  → Searching Zepto and Amazon Now...
      Zepto: 15 products, 14 look like ice cream · nutrition labels: read 14, 10 had numbers
  →  #  product                              size   Zepto  Amazon Now  ₹/100  kcal sugar prot    G
      1  Get-A-Way Chocolate Brownie Fudge   100 ml  ₹90      ₹60*      60    111   2.4  7.0  0.59 ✓
      4  Go Zero Belgian Dark Chocolate       100 ml  ₹53      ₹53       53     97   3.0  2.7  0.47 ✓
     > 4x2
  → (masala kaju list) > 3
  → Your basket ... [bill]
  → Real bills from each app:
      1  Zepto         items ₹113  delivery ₹30, other ₹5   TO PAY ₹148
      2  Amazon Now    items ₹166  handling ₹0, delivery ₹0 TO PAY ₹166
     Cheapest with everything: 1. Zepto at ₹148
  → Type 'confirm 148' to place a UPI order
  → (UPI QR shown) payment: PAID → "Paid. Zepto order is placed."
```

---

## Architecture

```
 Web page (index.html) ──WebSocket──┐
                                    ├──> Engine ──> Main graph (LangGraph)
 Terminal (chat.py) ────────────────┘      │            │
                                           │        router (LLM: grocery / food / unknown)
                                           │        ├── food subgraph     (Swiggy ∥ Zomato)
                                           │        └── grocery subgraph  (Zepto ∥ Amazon Now)
                                           │                 │
                          SQLite checkpointer          Adapters
                         (.checkpoints/shop.sqlite)    ├── MCP: Zepto, Swiggy, Zomato (OAuth 2.1 + PKCE)
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
links, both apps' prices, labels and G. The page draws cards with +/− buttons and turns your taps back
into the same text the terminal uses (`#4 × 2, #7 × 1` → `"4x2, 7"`), so the graph reads picks in
one way only. Photos load straight from the apps' image servers (Swiggy, Zomato, Zepto, Amazon). The
agent passes along only the links the apps send.

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

**Same product on both apps.**
1. Clean the names: lower case; remove sizes, marketing ("guilt free", "zero added sugar") and packaging
   ("cup", "tub"). "Get- A-Way" becomes "getaway".
2. `token_sort_ratio(clean_a, clean_b) ≥ 90`.
3. Sizes agree: `|a − b| / max(a, b) ≤ 5%`.
4. Best-first, one-to-one assignment.

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

The listed price isn't what you pay, so the agent fills **both** carts and reads each app's own bill
(Zepto: payment preview; Amazon: the bill its cart page fetches; Swiggy/Zomato: their cart pricing).
Unnamed amounts show as `other = TO PAY − items − named fees`, so every bill adds up. The suggestion is
the cheapest app that has **all** your items.

---

## Safety

- **Tool filter:** every MCP tool must be on a per-app allowlist and must not match a blocked pattern
  (`checkout`, `place_`, `order$`, `confirm`, `pay` ...). The agent can't see or call anything else.
- **Orders only through `call_order()`:** a hard-coded rule table, UPI only (no wallets or cards).
- **Typed approval:** `confirm <total rounded up>` is a single-use token bound to one app and amount,
  valid for 5 minutes, max ₹1500 (`ordering.max_order_total`). There's no button for it, on purpose.
- **Bill check:** if the app asks for more than you approved, the agent stops and tells you not to pay.
- **Unpaid orders lapse** after 5 minutes (Swiggy's `confirm_order` closes them as failed).
- **Amazon Now:** the agent can call only 8 read/cart endpoints (no checkout). You press *Proceed to pay*
  yourself. Other items in your real cart are only set aside while comparing and put back afterwards.
- **The web page listens on 127.0.0.1 only.**
- Secrets live in `.env` and `.tokens/`, and the Amazon login in `.browser/`. All are git-ignored.

---

## Setup

**Needs:** Windows 10/11, Python 3.13, Brave (or Chrome), Swiggy / Zomato / Zepto / Amazon accounts,
and a Velona API key.

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
```
Check it with `python scripts\check_velona.py`.

**2. Log in to the MCP apps** (once; tokens are saved in `.tokens/`):
```powershell
python scripts\list_tools.py
```
A browser opens for each app (Zepto, Swiggy, Zomato). Log in, and the tool list is written to `tools_dump.json`.

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
python scripts\try_amazon.py ice cream --cart
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

---

## Files it creates

All git-ignored.

| Path | What |
|---|---|
| `.tokens/` | MCP OAuth tokens (Zepto, Swiggy, Zomato) |
| `.browser/amazon_brave/` | the agent's browser profile (Amazon login) |
| `places.yaml` | your places ↔ each app's address IDs, plus Wi-Fi names |
| `.checkpoints/shop.sqlite` | LangGraph checkpoints (conversations) |
| `.state/` | Zepto device ID, last place, unfinished request, UPI QR images |
| `probes/` | raw app responses saved for debugging (bills, coupons, Amazon recon) |

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
| The page says "offline" | the server stopped: run `python scripts\web.py` again (the unfinished request is offered) |

---

## Limitations and roadmap

**Not supported yet**
- Switching Amazon Now's delivery address (it's used only when its pincode matches your place).
- Dishes with size or add-on choices.
- Finding Swiggy coupon codes by itself (Swiggy's MCP lists only COD coupons; typed codes work).

**Not yet run against the real apps**
- Zepto UPI order.
- Amazon's new-order detection.
- Putting set-aside Amazon items back.
- Typed Swiggy coupons that are UPI-only (whether `apply_food_coupon` accepts them).

**Next**
1. Amazon address switching (one more capture run).
2. Order status, history and "order the same as last time".
3. Saved preferences (veg only, default budget, favourite brands).
4. Dishes with sizes and add-ons.

---

*Personal project. It uses the apps' official MCP servers where they exist, and your own logged-in
browser for Amazon Now. Use it on your own accounts, responsibly.*
