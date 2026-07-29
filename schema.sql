-- ============================================================
-- Esquema de la base de datos — Sistema de inventario Isha Boutique
-- Motor: PostgreSQL
--
-- Modelo:
--   sucursales   -> las tiendas físicas (Tecamachalco, Prado Norte)
--   productos    -> espejo de los productos (con sus IDs de Shopify)
--   stock        -> FOTO actual: cuánto hay de cada producto por sucursal
--   movimientos  -> HISTORIAL: cada entrada, venta, ajuste o traspaso
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
    precio                     NUMERIC(10, 2),         -- precio de venta (NUNCA usar float para dinero)
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


-- ------------------------------------------------------------
-- 3. STOCK  (estado actual: una fila por producto+sucursal)
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
-- 4. MOVIMIENTOS  (historial de todo lo que cambia el stock)
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
-- 5. VENTAS  (registro financiero de cada venta)
--    Guarda a cuánto se vendió y cuánto costó, para calcular ganancias.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ventas (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producto_id     BIGINT NOT NULL REFERENCES productos(id)  ON DELETE CASCADE,
    sucursal_id     BIGINT NOT NULL REFERENCES sucursales(id) ON DELETE CASCADE,

    -- Canal de venta: boutique física o tienda en línea
    canal           TEXT NOT NULL CHECK (canal IN ('boutique', 'ecommerce')),

    cantidad        INTEGER NOT NULL CHECK (cantidad > 0),
    precio_unitario NUMERIC(10, 2) NOT NULL,   -- a cuánto se vendió cada pieza
    costo_unitario  NUMERIC(10, 2),            -- costo de la prenda al momento de vender (foto)
    creada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ------------------------------------------------------------
-- Índices para que las consultas frecuentes sean rápidas
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_stock_sucursal        ON stock (sucursal_id);
CREATE INDEX IF NOT EXISTS idx_movimientos_producto  ON movimientos (producto_id);
CREATE INDEX IF NOT EXISTS idx_movimientos_creado    ON movimientos (creado_en);
CREATE INDEX IF NOT EXISTS idx_ventas_creada         ON ventas (creada_en);
CREATE INDEX IF NOT EXISTS idx_ventas_sucursal       ON ventas (sucursal_id);


-- ------------------------------------------------------------
-- Datos iniciales: las 2 sucursales
-- ------------------------------------------------------------
INSERT INTO sucursales (nombre) VALUES ('Tecamachalco'), ('Prado Norte')
ON CONFLICT (nombre) DO NOTHING;
