-- ============================================================
-- Esquema de la base de datos — Sistema de inventario Isha Boutique
-- Motor: PostgreSQL
--
-- Modelo:
--   sucursales   -> las tiendas físicas (Tecamachalco, Prado Norte)
--   productos    -> espejo de los productos (con sus IDs de Shopify)
--   clientas     -> CRM básico: quiénes compran y con qué frecuencia
--   stock        -> FOTO actual: cuánto hay de cada producto por sucursal
--   movimientos  -> HISTORIAL: cada entrada, venta, ajuste o traspaso
--   ventas       -> registro financiero de cada venta (opcionalmente ligada a una clienta)
--   pagos        -> abonos hechos a una venta (permite pagos parciales / apartados)
-- ============================================================


-- ------------------------------------------------------------
-- 1. SUCURSALES
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sucursales (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- id automático
    nombre              TEXT      NOT NULL UNIQUE,   -- "Tecamachalco", "Prado Norte"
    shopify_location_id BIGINT,                      -- ubicación equivalente en Shopify (se llena al conectar)
    activa              BOOLEAN   NOT NULL DEFAULT TRUE,
    creada_en           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- 2. PRODUCTOS
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS productos (
    id                         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku                        TEXT NOT NULL UNIQUE,   -- código único de la prenda
    titulo                     TEXT NOT NULL,
    categoria                  TEXT,                   -- Vestidos, Conjuntos, etc.
    precio                     NUMERIC(10, 2),         -- precio de venta MENUDEO (NUNCA usar float para dinero)
    precio_mayoreo             NUMERIC(10, 2),         -- precio de venta MAYOREO
    costo                      NUMERIC(10, 2),         -- cuánto costó comprar la prenda
    activo                     BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = "eliminado" (se conserva su historial)

    -- Puente hacia Shopify (se llenan cuando conectemos la sincronización)
    shopify_product_id         BIGINT,
    shopify_variant_id         BIGINT,
    shopify_inventory_item_id  BIGINT,

    creado_en                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_en             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Por si la tabla productos ya existía sin estas columnas, las agregamos.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS costo NUMERIC(10, 2);
ALTER TABLE productos ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE;

-- Foto del producto: se guarda como bytes directamente en la base (BYTEA),
-- ya redimensionada y comprimida por la app antes de llegar aquí.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS foto BYTEA;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS foto_tipo TEXT;  -- ej. 'image/jpeg'

-- Segundo precio de lista (mayoreo), además del precio de menudeo ya existente.
ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_mayoreo NUMERIC(10, 2);


-- ------------------------------------------------------------
-- 3. CLIENTAS  (CRM básico: quiénes compran y con qué frecuencia)
--    Va ANTES de "ventas" porque ventas hace referencia a esta tabla.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clientas (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre       TEXT NOT NULL,
    telefono     TEXT,             -- WhatsApp: el canal clave con la clienta
    email        TEXT,
    cumpleanos   DATE,             -- opcional; útil para detectar fechas próximas
    notas        TEXT,             -- tallas, preferencias, gustos
    creada_en    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- 4. STOCK  (estado actual: una fila por producto+sucursal)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producto_id    BIGINT NOT NULL REFERENCES productos(id)  ON DELETE CASCADE,
    sucursal_id    BIGINT NOT NULL REFERENCES sucursales(id) ON DELETE CASCADE,
    cantidad       INTEGER NOT NULL DEFAULT 0 CHECK (cantidad >= 0),  -- nunca negativo
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Imposible tener dos renglones del mismo producto en la misma sucursal
    UNIQUE (producto_id, sucursal_id)
);


-- ------------------------------------------------------------
-- 5. MOVIMIENTOS  (historial de todo lo que cambia el stock)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS movimientos (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producto_id BIGINT NOT NULL REFERENCES productos(id)  ON DELETE CASCADE,
    sucursal_id BIGINT NOT NULL REFERENCES sucursales(id) ON DELETE CASCADE,

    -- Tipo de movimiento (CHECK limita a valores válidos)
    tipo        TEXT NOT NULL CHECK (tipo IN ('entrada', 'venta', 'ajuste', 'traspaso')),

    -- Cambio en unidades: positivo suma (entrada), negativo resta (venta)
    delta       INTEGER NOT NULL,

    motivo      TEXT,                                  -- nota opcional ("llegada de fábrica", "venta POS")
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- 6. VENTAS  (registro financiero de cada venta)
--    Guarda a cuánto se vendió y cuánto costó, para calcular ganancias.
--    cliente_id es OPCIONAL: se puede vender sin ligar a una clienta.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ventas (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producto_id     BIGINT NOT NULL REFERENCES productos(id)  ON DELETE CASCADE,
    sucursal_id     BIGINT NOT NULL REFERENCES sucursales(id) ON DELETE CASCADE,
    cliente_id      BIGINT REFERENCES clientas(id) ON DELETE SET NULL,

    -- Canal de venta.
    canal           TEXT NOT NULL CHECK (canal IN ('boutique', 'ecommerce', 'consignacion', 'mayoreo', 'domicilio')),

    -- Con cuál de los 2 precios de lista se vendió (independiente de si el
    -- precio_unitario final se ajustó a mano, ej. por un descuento).
    tipo_precio     TEXT NOT NULL DEFAULT 'menudeo' CHECK (tipo_precio IN ('menudeo', 'mayoreo')),

    cantidad        INTEGER NOT NULL CHECK (cantidad > 0),
    precio_unitario NUMERIC(10, 2) NOT NULL,   -- a cuánto se vendió cada pieza (YA con descuento aplicado)
    costo_unitario  NUMERIC(10, 2),            -- costo de la prenda al momento de vender (foto)
    descuento_pct   NUMERIC(5, 2) CHECK (descuento_pct >= 0 AND descuento_pct <= 100),  -- porcentaje de descuento aplicado, si hubo

    -- Agrupa las varias líneas de una misma compra (ej. las prendas del
    -- carrito) como UN solo "ticket", para poder calcular el ticket promedio
    -- (gasto por visita, no por prenda). NULL en ventas viejas = se trata
    -- como su propio ticket individual.
    pedido_id       TEXT,

    creada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Por si la tabla ventas ya existía sin esta columna (creada antes del CRM), la agregamos.
-- ON DELETE SET NULL: si se borra una clienta, sus ventas pasadas NO se pierden,
-- solo quedan "sin clienta asociada".
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS cliente_id BIGINT REFERENCES clientas(id) ON DELETE SET NULL;

-- Por si la tabla ventas ya existía sin esta columna, la agregamos con default
-- 'menudeo' (todas las ventas anteriores a este cambio se asumen menudeo).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS tipo_precio TEXT NOT NULL DEFAULT 'menudeo'
    CHECK (tipo_precio IN ('menudeo', 'mayoreo'));

-- Porcentaje de descuento aplicado en la venta (NULL = sin descuento).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS descuento_pct NUMERIC(5, 2)
    CHECK (descuento_pct >= 0 AND descuento_pct <= 100);

-- Agrupa varias líneas de una misma compra como un solo ticket.
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS pedido_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ventas_pedido ON ventas (pedido_id);

-- ID del pedido de Shopify que originó esta venta (NULL = venta boutique,
-- no vino de Shopify). Sirve para no procesar el mismo webhook 2 veces
-- (Shopify puede reenviar el mismo webhook más de una vez).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS shopify_order_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_ventas_shopify_order ON ventas (shopify_order_id);

-- Folio consecutivo de la nota impresa (uno por ticket/pedido_id, no por
-- línea). Empieza en 1 la primera vez que se usa; las ventas de antes de
-- este cambio se quedan en NULL (no se renumeran).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS numero_nota INTEGER;

-- Lleva el último folio usado POR SUCURSAL: cada sede numera sus notas por
-- separado (Tecamachalco y Prado Norte cada una empieza en su propio #1),
-- en vez de compartir un solo contador. Un UPSERT con ON CONFLICT sobre
-- esta tabla incrementa el número de forma atómica (sin choques si dos
-- ventas se registran al mismo tiempo).
CREATE TABLE IF NOT EXISTS notas_folio (
    sucursal_id  BIGINT PRIMARY KEY REFERENCES sucursales(id) ON DELETE CASCADE,
    ultimo_numero INTEGER NOT NULL DEFAULT 0
);

-- Marca una venta como apartado (la prenda ya salió del stock, pero la
-- clienta todavía no la paga completa). Los reportes financieros excluyen
-- los apartados mientras les quede saldo pendiente, para no contar como
-- ingreso/ganancia algo que todavía no se cobró; en cuanto se terminan de
-- pagar (saldo = 0), entran solos al reporte, sin tocar nada a mano.
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS apartado BOOLEAN NOT NULL DEFAULT FALSE;

-- Amplía los canales de venta válidos (antes solo boutique/ecommerce).
-- DROP + ADD porque Postgres no tiene "ALTER CONSTRAINT ... IF NOT EXISTS"
-- para CHECKs; el nombre es el que Postgres autogeneró al crear la tabla.
ALTER TABLE ventas DROP CONSTRAINT IF EXISTS ventas_canal_check;
ALTER TABLE ventas ADD CONSTRAINT ventas_canal_check
    CHECK (canal IN ('boutique', 'ecommerce', 'consignacion', 'mayoreo', 'domicilio'));

-- ------------------------------------------------------------
-- 8. VENDEDORAS  (quién atendió cada venta, para reportes de comisiones)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendedoras (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre    TEXT NOT NULL UNIQUE,
    activa    BOOLEAN NOT NULL DEFAULT TRUE,
    creada_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Opcional: una venta puede no tener vendedora asignada (ON DELETE SET NULL,
-- igual que clientas, para no perder el historial si se borra a alguien).
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS vendedora_id BIGINT REFERENCES vendedoras(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ventas_vendedora ON ventas (vendedora_id);


-- ------------------------------------------------------------
-- 7. PAGOS  (abonos hechos a una venta; una venta puede tener varios)
--    Permite pagos parciales: el saldo pendiente = total de la venta
--    menos la suma de sus pagos.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pagos (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venta_id    BIGINT NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
    metodo      TEXT NOT NULL CHECK (metodo IN ('efectivo', 'tarjeta', 'transferencia')),
    monto       NUMERIC(10, 2) NOT NULL CHECK (monto > 0),
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pagos_venta ON pagos (venta_id);


-- ------------------------------------------------------------
-- 8. DEVOLUCIONES  (devoluciones/cambios reales de una clienta)
--    A diferencia de "Eliminar venta" (pensado para borrar errores de
--    captura), una devolución/cambio NO borra la venta original: queda en
--    el historial de que sí se vendió, y aquí se registra aparte que la
--    prenda regresó. Solo puede haber una devolución por venta.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devoluciones (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venta_id    BIGINT NOT NULL UNIQUE REFERENCES ventas(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL CHECK (tipo IN ('devolucion', 'cambio')),
    motivo      TEXT,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- Índices para que las consultas frecuentes sean rápidas
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_stock_sucursal        ON stock (sucursal_id);
CREATE INDEX IF NOT EXISTS idx_movimientos_producto  ON movimientos (producto_id);
CREATE INDEX IF NOT EXISTS idx_movimientos_creado    ON movimientos (creado_en);
CREATE INDEX IF NOT EXISTS idx_ventas_creada         ON ventas (creada_en);
CREATE INDEX IF NOT EXISTS idx_ventas_sucursal       ON ventas (sucursal_id);
CREATE INDEX IF NOT EXISTS idx_ventas_cliente        ON ventas (cliente_id);


-- ------------------------------------------------------------
-- Datos iniciales: las 2 sucursales
-- ------------------------------------------------------------
INSERT INTO sucursales (nombre) VALUES ('Tecamachalco'), ('Prado Norte')
ON CONFLICT (nombre) DO NOTHING;
