# Isha Wear POS

Internal inventory, point-of-sale and CRM system for **Isha Boutique**, a multi-brand boutique in Mexico City with two locations: Tecamachalco and Prado Norte. It replaces manual stock and sales tracking, and stays in sync with the Shopify online store.

> Private project for internal use. The app asks for a username and password on every page.

## Features

**Inventory**
- Stock per product and per location, with a movement history (receipts, adjustments, transfers between locations).
- Product creation and editing with photo, editable SKU, retail/wholesale price and cost.
- Search by SKU or name, CSV export, printable labels with a QR code per SKU.
- "Deleted" products are deactivated without losing their history.

**Sales**
- Single sale or **multi-item sale** (cart) with live stock validation.
- Channels: boutique, e-commerce, consignment, wholesale, home delivery and Instagram.
- Partial payments and installments (cash, card or bank transfer), layaways, discounts and change.
- Sequential receipt number **per location**.
- Printable receipt (80 mm thermal ticket format): "Sales ticket" when paid in full, "Pending balance note" when there is an outstanding balance; shows the salesperson and payment method.
- Returns/exchanges and sale deletion (one at a time or in bulk).

**Customers (CRM)**
- Profile with contact info, birthday, Instagram (profile and direct DM links) and notes.
- Purchase history grouped **by receipt** (one per shopping occasion), with payments applied per receipt or to the account in general.
- Outstanding balances, customers who haven't returned, and a top-customers ranking.

**Salespeople and commissions**
- Salesperson accounts with their own login (restricted access: they can only register sales and view their own).
- Commission calculated on money actually collected: cash/transfer 3%; card 3% of the amount after a 5% bank fee. Shows the current month and the all-time total.

**Reports and cash register**
- Sales, profit and average ticket reports (layaways don't count until paid off).
- Cash register summary per location and payment method.
- Sales export to CSV for the accountant.

**Shopify integration**
- Pushes real stock to Shopify (one location per branch) whenever it changes.
- Receives paid orders via webhook (`orders/paid`), deducts stock and records the sale as an e-commerce sale. Every request is verified with an HMAC signature.
- Can send new products to Shopify as drafts.

## Stack

- **Backend:** Python, FastAPI, SQLAlchemy (raw SQL via `text()`), Jinja2
- **Database:** PostgreSQL (psycopg 3)
- **Frontend:** Server-rendered HTML + vanilla JavaScript
- **Deployment:** Railway (auto-deploys on push to `main`)
- **Other:** Pillow (photos), qrcode (labels), requests (Shopify API)

## Project structure

```
app/
  main.py              Routes, authentication and business logic
  db.py                PostgreSQL connection
  config.py            Shopify settings
  shopify_sync.py      Stock and products pushed to Shopify
  shopify_webhooks.py  Incoming Shopify orders
templates/             HTML pages (Jinja2)
static/                Logo and static files
schema.sql             Database schema (idempotent)
init_db.py             Applies schema.sql to the database
link_shopify_skus.py   One-off script: links products to Shopify by SKU
check_sku_match.py     One-off diagnostic script
Procfile               Start command for Railway
```

## Local setup

Requires Python 3.10+ and a PostgreSQL database.

```bash
git clone https://github.com/yuvalbaryosefp-alt/isha-wear-pos.git
cd isha-wear-pos

python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # then fill in the values (see below)
python init_db.py               # creates/updates the tables

uvicorn app.main:app --reload
```

Open <http://localhost:8000> and sign in with `APP_USUARIO` / `APP_CLAVE`.

> Careful: if your `DATABASE_URL` points to the production database, anything you do locally affects real data.

## Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string: `postgresql+psycopg://user:password@host:port/database` |
| `APP_USUARIO` / `APP_CLAVE` | Admin account credentials |
| `SHOPIFY_STORE` | The store's `.myshopify.com` domain |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Shopify app credentials (the secret also verifies webhook signatures) |
| `SHOPIFY_API_VERSION` | Shopify API version (defaults to `2026-07`) |

Never commit `.env` to the repository (it's already in `.gitignore`).

## Database

`schema.sql` uses `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so it can be re-run without deleting data. After changing the schema, run:

```bash
python init_db.py
```

Main tables: `sucursales` (locations), `productos`, `stock`, `movimientos`, `ventas` (sales), `pagos` (payments), `devoluciones` (returns), `clientas` (customers), `vendedoras` (salespeople), `notas_folio` (receipt counters).

## Access roles

- **Admin** (environment credentials): full access: inventory, customers, salespeople, cash register, reports and exports.
- **Salesperson** (own login): can only register sales and view/print their own.

## Deployment

Railway runs the `Procfile` (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) and redeploys on every push to `main`. Set the same environment variables on the Railway service. The Shopify webhook should point to `https://<your-domain>/webhooks/shopify/orders`.
