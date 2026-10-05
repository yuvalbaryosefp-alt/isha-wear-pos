# Isha Wear POS

Sistema interno de inventario, punto de venta y CRM para **Isha Boutique** (boutique multimarca en CDMX con dos sucursales: Tecamachalco y Prado Norte). Reemplaza el control manual de stock y ventas, y se mantiene sincronizado con la tienda en línea de Shopify.

> Proyecto privado de uso interno. La app pide usuario y contraseña en cada página.

## Qué hace

**Inventario**
- Stock por producto y por sucursal, con historial de movimientos (entradas, ajustes, traspasos entre sucursales).
- Alta y edición de productos con foto, SKU editable, precio menudeo/mayoreo y costo.
- Búsqueda por SKU o nombre, exportación a CSV, etiquetas imprimibles con código QR por SKU.
- Productos "eliminados" se desactivan sin perder su historial.

**Ventas**
- Registro de venta individual o **venta múltiple** (carrito) con validación de stock en vivo.
- Canales: boutique, e-commerce, consignación, mayoreo, domicilio e Instagram.
- Pagos parciales y abonos (efectivo, tarjeta o transferencia), apartados, descuentos y cambio.
- Folio consecutivo de nota **por sucursal**.
- Nota imprimible (formato ticket térmico de 80 mm): "Ticket de venta" si está pagada, "Nota pendiente de liquidar" si hay saldo; muestra vendedora y método de pago.
- Devoluciones/cambios y eliminación de ventas (individual o varias a la vez).

**Clientas (CRM)**
- Ficha con contacto, cumpleaños, Instagram (perfil y DM directo) y notas.
- Historial agrupado **por nota** (una por ocasión de compra), con abono por nota o general a la cuenta.
- Saldos pendientes, clientas que no han vuelto y ranking de mejores clientas.

**Vendedoras y comisiones**
- Alta de vendedoras con usuario propio (acceso restringido: solo registrar ventas y ver las suyas).
- Comisión calculada sobre lo realmente cobrado: efectivo/transferencia 3 %; tarjeta 3 % sobre el monto menos 5 % de comisión bancaria. Muestra el mes actual y el total.

**Reportes y caja**
- Reportes de ventas, utilidad y ticket promedio (los apartados no cuentan hasta liquidarse).
- Corte de caja por sucursal y método de pago.
- Exportación de ventas a CSV para el contador.

**Integración con Shopify**
- Empuja el stock real a Shopify (una ubicación por sucursal) cada vez que cambia.
- Recibe pedidos pagados por webhook (`orders/paid`), descuenta stock y registra la venta como canal e-commerce. Cada request se valida con firma HMAC.
- Puede enviar productos nuevos a Shopify como borrador.

## Stack

- **Backend:** Python, FastAPI, SQLAlchemy (consultas SQL con `text()`), Jinja2
- **Base de datos:** PostgreSQL (psycopg 3)
- **Frontend:** HTML renderizado en servidor + JavaScript sin frameworks
- **Despliegue:** Railway (auto-deploy al hacer push a `main`)
- **Otros:** Pillow (fotos), qrcode (etiquetas), requests (API de Shopify)

## Estructura

```
app/
  main.py              Rutas, autenticación y lógica de negocio
  db.py                Conexión a PostgreSQL
  config.py            Variables de Shopify
  shopify_sync.py      Stock y productos hacia Shopify
  shopify_webhooks.py  Pedidos entrantes de Shopify
templates/             Páginas HTML (Jinja2)
static/                Logo y archivos estáticos
schema.sql             Esquema de la base de datos (idempotente)
init_db.py             Aplica schema.sql a la base de datos
link_shopify_skus.py   Script de un solo uso: liga productos con Shopify por SKU
check_sku_match.py     Script de diagnóstico de un solo uso
Procfile               Comando de arranque para Railway
```

## Instalación local

Requiere Python 3.10+ y una base de datos PostgreSQL.

```bash
git clone https://github.com/yuvalbaryosefp-alt/isha-wear-pos.git
cd isha-wear-pos

python -m venv .venv
.venv/Scripts/activate          # en macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # y llena los valores (ver abajo)
python init_db.py               # crea/actualiza las tablas

uvicorn app.main:app --reload
```

Abre <http://localhost:8000> e inicia sesión con `APP_USUARIO` / `APP_CLAVE`.

> Cuidado: si tu `DATABASE_URL` apunta a la base de producción, lo que hagas localmente afecta datos reales.

## Variables de entorno

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL: `postgresql+psycopg://usuario:clave@host:puerto/base` |
| `APP_USUARIO` / `APP_CLAVE` | Credenciales de la cuenta administradora |
| `SHOPIFY_STORE` | Dominio `.myshopify.com` de la tienda |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | Credenciales de la app de Shopify (el secret también valida la firma de los webhooks) |
| `SHOPIFY_API_VERSION` | Versión de la API de Shopify (por defecto `2026-07`) |

Nunca subas `.env` al repositorio (ya está en `.gitignore`).

## Base de datos

`schema.sql` usa `CREATE TABLE IF NOT EXISTS` y `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, así que se puede volver a correr sin borrar datos. Después de cambiar el esquema, ejecuta:

```bash
python init_db.py
```

Tablas principales: `sucursales`, `productos`, `stock`, `movimientos`, `ventas`, `pagos`, `devoluciones`, `clientas`, `vendedoras`, `notas_folio`.

## Roles de acceso

- **Admin** (credenciales de entorno): acceso completo — inventario, clientas, vendedoras, caja, reportes y exportaciones.
- **Vendedora** (usuario propio): solo registra ventas y consulta/imprime las suyas.

## Despliegue

Railway ejecuta el `Procfile` (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) y redespliega con cada push a `main`. Configura las mismas variables de entorno en el servicio de Railway. El webhook de Shopify debe apuntar a `https://<tu-dominio>/webhooks/shopify/orders`.
