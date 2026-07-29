"""
Aplicación web del sistema de inventario — Isha Boutique.

Levanta un servidor con FastAPI que muestra los productos y su stock por
sucursal, y permite dar de alta productos nuevos.

Para correrlo (desde la carpeta del proyecto):
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload
Luego abrir en el navegador:  http://127.0.0.1:8000
"""

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import engine

app = FastAPI(title="Inventario Isha Boutique")

# Carpeta donde viven las plantillas HTML (se calcula relativa a este archivo).
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Postgres guarda las fechas en UTC. La convertimos a hora de Ciudad de México
# solo para MOSTRARLA (en la base siempre se queda en UTC, que es lo correcto).
ZONA_CDMX = ZoneInfo("America/Mexico_City")


def parsear_dinero(texto: str) -> float | None:
    """Convierte un texto a número de dinero. Devuelve None si viene vacío o inválido."""
    if not texto or not texto.strip():
        return None
    try:
        return float(texto.replace(",", "").strip())
    except ValueError:
        return None


@app.get("/")
def pagina_principal(request: Request, error: str | None = None):
    """Muestra la tabla de productos con su stock por sucursal."""
    with engine.connect() as conn:
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()

        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, costo FROM productos "
            "WHERE activo = TRUE ORDER BY titulo"
        )).mappings().all()

        clientas = conn.execute(text(
            "SELECT id, nombre FROM clientas ORDER BY nombre"
        )).mappings().all()

        stock_rows = conn.execute(text(
            "SELECT producto_id, sucursal_id, cantidad FROM stock"
        )).all()

        # Últimos 10 movimientos, con nombre de producto y sucursal.
        movimientos_rows = conn.execute(text(
            "SELECT m.creado_en, p.titulo, s.nombre AS sucursal, "
            "       m.tipo, m.delta, m.motivo "
            "FROM movimientos m "
            "JOIN productos p ON p.id = m.producto_id "
            "JOIN sucursales s ON s.id = m.sucursal_id "
            "ORDER BY m.creado_en DESC LIMIT 10"
        )).mappings().all()

    # Convierte cada fecha (UTC) a hora de Ciudad de México, solo para mostrar.
    movimientos = [
        {**dict(m), "creado_en": m["creado_en"].astimezone(ZONA_CDMX)}
        for m in movimientos_rows
    ]

    # Diccionario para buscar rápido: (producto, sucursal) -> cantidad
    stock_map = {(r.producto_id, r.sucursal_id): r.cantidad for r in stock_rows}

    # Arma una fila por producto con la lista de cantidades (una por sucursal).
    filas = []
    for p in productos:
        filas.append({
            "id": p["id"],
            "sku": p["sku"],
            "titulo": p["titulo"],
            "categoria": p["categoria"],
            "precio": p["precio"],
            "costo": p["costo"],
            "cantidades": [stock_map.get((p["id"], s["id"]), 0) for s in sucursales],
        })

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sucursales": sucursales,
            "productos": productos,
            "clientas": clientas,
            "filas": filas,
            "movimientos": movimientos,
            "error": error,
        },
    )


@app.post("/movimientos")
def registrar_movimiento(
    producto_id: int = Form(...),
    sucursal_id: int = Form(...),
    tipo: str = Form(...),
    cantidad: str = Form(...),
    motivo: str = Form(""),
):
    """Registra un movimiento y actualiza el stock de esa sucursal."""
    tipo = tipo.strip()
    motivo = motivo.strip() or None

    # La cantidad debe ser un número entero.
    try:
        cantidad_num = int(cantidad.strip())
    except ValueError:
        return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)

    if tipo not in ("entrada", "ajuste"):
        return RedirectResponse("/?error=Tipo de movimiento inválido.", status_code=303)

    if tipo == "entrada" and cantidad_num <= 0:
        return RedirectResponse("/?error=La cantidad debe ser mayor a 0.", status_code=303)
    if tipo == "ajuste" and cantidad_num < 0:
        return RedirectResponse("/?error=La cantidad no puede ser negativa.", status_code=303)

    with engine.begin() as conn:
        # Stock actual del producto en esa sucursal (0 si no existía el renglón).
        actual = conn.execute(text(
            "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
        ), {"p": producto_id, "s": sucursal_id}).scalar()
        actual = actual if actual is not None else 0

        # Calcula el nuevo stock y el "delta" (cuánto cambió) según el tipo.
        if tipo == "entrada":
            nuevo = actual + cantidad_num
            delta = cantidad_num
        else:  # ajuste: el stock queda exactamente en la cantidad indicada
            nuevo = cantidad_num
            delta = cantidad_num - actual

        # Actualiza (o crea) el renglón de stock.
        conn.execute(text(
            "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
            "ON CONFLICT (producto_id, sucursal_id) "
            "DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
        ), {"p": producto_id, "s": sucursal_id, "c": nuevo})

        # Guarda el movimiento en el historial.
        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, :tipo, :delta, :motivo)"
        ), {"p": producto_id, "s": sucursal_id, "tipo": tipo, "delta": delta, "motivo": motivo})

    return RedirectResponse("/", status_code=303)


@app.post("/ventas")
def registrar_venta(
    producto_id: int = Form(...),
    sucursal_id: int = Form(...),
    canal: str = Form(...),
    cantidad: str = Form(...),
    precio: str = Form(""),
    cliente_id: str = Form(""),  # opcional: puede venir vacío ("Sin clienta")
):
    """Registra una venta: descuenta stock y guarda la info financiera."""
    canal = canal.strip()

    # La cantidad debe ser un entero mayor a 0.
    try:
        cantidad_num = int(cantidad.strip())
    except ValueError:
        return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)
    if cantidad_num <= 0:
        return RedirectResponse("/?error=La cantidad debe ser mayor a 0.", status_code=303)

    if canal not in ("boutique", "ecommerce"):
        return RedirectResponse("/?error=Canal de venta inválido.", status_code=303)

    precio_indicado = parsear_dinero(precio)

    # Convierte el select de clienta a número o None ("Sin clienta" llega vacío).
    cliente_id_num = int(cliente_id) if cliente_id.strip() else None

    with engine.begin() as conn:
        # Trae precio de lista y costo del producto.
        producto = conn.execute(text(
            "SELECT precio, costo FROM productos WHERE id = :p"
        ), {"p": producto_id}).mappings().one_or_none()

        if producto is None:
            return RedirectResponse("/?error=Producto no encontrado.", status_code=303)

        # Si no escribieron precio, se usa el precio de lista del producto.
        precio_unitario = precio_indicado if precio_indicado is not None else producto["precio"]
        if precio_unitario is None:
            return RedirectResponse(
                "/?error=Falta el precio de venta (el producto no tiene precio de lista).",
                status_code=303,
            )

        costo_unitario = producto["costo"]  # puede ser None si no se capturó

        # Verifica que haya stock suficiente en esa sucursal.
        actual = conn.execute(text(
            "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
        ), {"p": producto_id, "s": sucursal_id}).scalar()
        actual = actual if actual is not None else 0

        if actual < cantidad_num:
            return RedirectResponse(
                f"/?error=No hay suficiente stock (hay {actual}, intentas vender {cantidad_num}).",
                status_code=303,
            )

        # 1) Descuenta el stock.
        conn.execute(text(
            "UPDATE stock SET cantidad = cantidad - :c, actualizado_en = NOW() "
            "WHERE producto_id = :p AND sucursal_id = :s"
        ), {"c": cantidad_num, "p": producto_id, "s": sucursal_id})

        # 2) Deja rastro en el historial de movimientos.
        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, 'venta', :delta, :motivo)"
        ), {"p": producto_id, "s": sucursal_id, "delta": -cantidad_num, "motivo": f"venta {canal}"})

        # 3) Guarda el registro financiero (cliente_id puede ser None).
        conn.execute(text(
            "INSERT INTO ventas "
            "(producto_id, sucursal_id, canal, cantidad, precio_unitario, costo_unitario, cliente_id) "
            "VALUES (:p, :s, :canal, :cant, :precio, :costo, :cliente)"
        ), {
            "p": producto_id, "s": sucursal_id, "canal": canal,
            "cant": cantidad_num, "precio": precio_unitario, "costo": costo_unitario,
            "cliente": cliente_id_num,
        })

    return RedirectResponse("/", status_code=303)


@app.post("/productos")
def crear_producto(
    sku: str = Form(...),
    titulo: str = Form(...),
    categoria: str = Form(""),
    costo: str = Form(""),
):
    """Da de alta un producto (momento de compra) y le crea stock en 0 en cada sucursal.

    El precio de venta NO se pide aquí: se define después con el botón Editar.
    """
    # Limpia los textos (quita espacios sobrantes).
    sku = sku.strip()
    titulo = titulo.strip()
    categoria = categoria.strip() or None

    # Convierte el costo a número (None si viene vacío o mal escrito).
    costo_valor = parsear_dinero(costo)

    try:
        # Todo dentro de una transacción: o se guarda todo, o nada.
        with engine.begin() as conn:
            nuevo_id = conn.execute(text(
                "INSERT INTO productos (sku, titulo, categoria, costo) "
                "VALUES (:sku, :titulo, :categoria, :costo) RETURNING id"
            ), {
                "sku": sku, "titulo": titulo,
                "categoria": categoria, "costo": costo_valor,
            }).scalar_one()

            sucursal_ids = conn.execute(text(
                "SELECT id FROM sucursales WHERE activa = TRUE"
            )).scalars().all()

            for suc_id in sucursal_ids:
                conn.execute(text(
                    "INSERT INTO stock (producto_id, sucursal_id, cantidad) "
                    "VALUES (:producto_id, :sucursal_id, 0)"
                ), {"producto_id": nuevo_id, "sucursal_id": suc_id})

    except IntegrityError:
        # Pasa si el SKU ya existe (regla UNIQUE). Avisamos sin romper.
        mensaje = f"El SKU '{sku}' ya existe. Usa uno diferente."
        return RedirectResponse(f"/?error={mensaje}", status_code=303)

    # 303 hace que el navegador vuelva a "/" con un GET (evita reenviar el form).
    return RedirectResponse("/", status_code=303)


@app.post("/productos/{producto_id}/eliminar")
def eliminar_producto(producto_id: int):
    """'Elimina' un producto SIN borrarlo de la base: lo marca inactivo.

    Así desaparece de las listas y formularios, pero su historial de ventas
    y movimientos se conserva intacto para que los reportes no pierdan datos.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE productos SET activo = FALSE, actualizado_en = NOW() WHERE id = :id"
        ), {"id": producto_id})
    return RedirectResponse("/", status_code=303)


@app.post("/productos/{producto_id}/reactivar")
def reactivar_producto(producto_id: int):
    """Deshace un 'eliminar': vuelve a marcar el producto como activo."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE productos SET activo = TRUE, actualizado_en = NOW() WHERE id = :id"
        ), {"id": producto_id})
    return RedirectResponse("/desactivados", status_code=303)


@app.get("/clientas")
def ver_clientas(request: Request, error: str | None = None):
    """Lista las clientas con cuánto y cuándo le han comprado (recurrencia)."""
    with engine.connect() as conn:
        filas = conn.execute(text(
            "SELECT c.id, c.nombre, c.telefono, "
            "       COUNT(v.id) AS num_compras, "
            "       COALESCE(SUM(v.precio_unitario * v.cantidad), 0) AS total_comprado, "
            "       MAX(v.creada_en) AS ultima_compra "
            "FROM clientas c "
            "LEFT JOIN ventas v ON v.cliente_id = c.id "
            "GROUP BY c.id, c.nombre, c.telefono "
            "ORDER BY c.nombre"
        )).mappings().all()

    # Convierte la fecha de última compra a hora de CDMX (si existe).
    clientas = []
    for f in filas:
        d = dict(f)
        if d["ultima_compra"] is not None:
            d["ultima_compra"] = d["ultima_compra"].astimezone(ZONA_CDMX)
        clientas.append(d)

    return templates.TemplateResponse(request, "clientas.html", {"clientas": clientas, "error": error})


@app.post("/clientas")
def crear_clienta(
    nombre: str = Form(...),
    telefono: str = Form(""),
    email: str = Form(""),
    cumpleanos: str = Form(""),
    notas: str = Form(""),
):
    """Da de alta una clienta nueva."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO clientas (nombre, telefono, email, cumpleanos, notas) "
            "VALUES (:nombre, :telefono, :email, :cumpleanos, :notas)"
        ), {
            "nombre": nombre.strip(),
            "telefono": telefono.strip() or None,
            "email": email.strip() or None,
            "cumpleanos": cumpleanos.strip() or None,
            "notas": notas.strip() or None,
        })
    return RedirectResponse("/clientas", status_code=303)


@app.get("/clientas/{cliente_id}")
def ver_clienta(request: Request, cliente_id: int):
    """Ficha de una clienta: sus datos y su historial completo de compras."""
    with engine.connect() as conn:
        clienta = conn.execute(text(
            "SELECT id, nombre, telefono, email, cumpleanos, notas FROM clientas WHERE id = :id"
        ), {"id": cliente_id}).mappings().one_or_none()

        if clienta is None:
            return RedirectResponse("/clientas?error=Clienta no encontrada.", status_code=303)

        compras_rows = conn.execute(text(
            "SELECT v.creada_en, p.titulo, v.cantidad, v.precio_unitario, s.nombre AS sucursal, v.canal "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "WHERE v.cliente_id = :id ORDER BY v.creada_en DESC"
        ), {"id": cliente_id}).mappings().all()

    compras = []
    for c in compras_rows:
        d = dict(c)
        d["creada_en"] = d["creada_en"].astimezone(ZONA_CDMX)
        compras.append(d)

    return templates.TemplateResponse(request, "clienta_detalle.html", {"clienta": clienta, "compras": compras})


@app.get("/desactivados")
def ver_desactivados(request: Request):
    """Lista los productos eliminados (inactivos), por si hay que reactivar alguno."""
    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria FROM productos "
            "WHERE activo = FALSE ORDER BY titulo"
        )).mappings().all()

    return templates.TemplateResponse(request, "desactivados.html", {"productos": productos})


@app.get("/productos/{producto_id}/editar")
def editar_producto_form(request: Request, producto_id: int):
    """Muestra la pantalla para editar un producto (poner precio de venta, ajustar costo)."""
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, costo FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

    if producto is None:
        return RedirectResponse("/?error=Producto no encontrado.", status_code=303)

    return templates.TemplateResponse(request, "editar.html", {"producto": producto})


@app.post("/productos/{producto_id}/editar")
def editar_producto(
    producto_id: int,
    titulo: str = Form(...),
    categoria: str = Form(""),
    precio: str = Form(""),
    costo: str = Form(""),
):
    """Guarda los cambios del producto (título, categoría, precio de venta y costo)."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE productos SET titulo = :titulo, categoria = :categoria, "
            "precio = :precio, costo = :costo, actualizado_en = NOW() WHERE id = :id"
        ), {
            "titulo": titulo.strip(),
            "categoria": categoria.strip() or None,
            "precio": parsear_dinero(precio),
            "costo": parsear_dinero(costo),
            "id": producto_id,
        })

    return RedirectResponse("/", status_code=303)


# Fragmento de SQL con las 4 métricas financieras (se reutiliza en varias consultas).
_METRICAS = (
    "SUM(v.cantidad) AS piezas, "
    "SUM(v.precio_unitario * v.cantidad) AS ingresos, "
    "SUM(COALESCE(v.costo_unitario, 0) * v.cantidad) AS costo, "
    "SUM((v.precio_unitario - COALESCE(v.costo_unitario, 0)) * v.cantidad) AS bruta"
)


def _resumen(row) -> dict:
    """Convierte una fila de métricas a números limpios y calcula el margen %."""
    piezas = int(row["piezas"] or 0)
    ingresos = float(row["ingresos"] or 0)
    costo = float(row["costo"] or 0)
    bruta = float(row["bruta"] or 0)
    margen = (bruta / ingresos * 100) if ingresos else 0.0
    fila = {"piezas": piezas, "ingresos": ingresos, "costo": costo,
            "bruta": bruta, "margen": margen}
    # Si la fila viene de un desglose, trae también la dimensión (categoría, etc.).
    if "dim" in row.keys():
        fila["dim"] = row["dim"]
    return fila


@app.get("/reportes")
def reportes(request: Request, desde: str | None = None, hasta: str | None = None):
    """Reporte de ganancia bruta por período, con desglose por categoría, sucursal y canal."""
    # Por defecto, del primer día del mes actual hasta hoy.
    hoy = date.today()
    if not desde:
        desde = hoy.replace(day=1).isoformat()
    if not hasta:
        hasta = hoy.isoformat()

    # Filtra por la fecha de la venta (comparando solo la parte de fecha).
    where = "WHERE v.creada_en::date BETWEEN :desde AND :hasta"
    params = {"desde": desde, "hasta": hasta}

    with engine.connect() as conn:
        total = conn.execute(text(
            f"SELECT {_METRICAS} FROM ventas v {where}"
        ), params).mappings().one()

        por_categoria = conn.execute(text(
            f"SELECT COALESCE(p.categoria, 'Sin categoría') AS dim, {_METRICAS} "
            f"FROM ventas v JOIN productos p ON p.id = v.producto_id {where} "
            f"GROUP BY p.categoria ORDER BY bruta DESC NULLS LAST"
        ), params).mappings().all()

        por_sucursal = conn.execute(text(
            f"SELECT s.nombre AS dim, {_METRICAS} "
            f"FROM ventas v JOIN sucursales s ON s.id = v.sucursal_id {where} "
            f"GROUP BY s.nombre ORDER BY bruta DESC"
        ), params).mappings().all()

        por_canal = conn.execute(text(
            f"SELECT v.canal AS dim, {_METRICAS} "
            f"FROM ventas v {where} GROUP BY v.canal ORDER BY bruta DESC"
        ), params).mappings().all()

    # Nombres bonitos para los canales.
    etiquetas_canal = {"boutique": "Boutique", "ecommerce": "E-commerce"}
    canal_filas = []
    for r in por_canal:
        fila = _resumen(r)
        fila["dim"] = etiquetas_canal.get(fila["dim"], fila["dim"])
        canal_filas.append(fila)

    return templates.TemplateResponse(request, "reportes.html", {
        "desde": desde,
        "hasta": hasta,
        "total": _resumen(total),
        "por_categoria": [_resumen(r) for r in por_categoria],
        "por_sucursal": [_resumen(r) for r in por_sucursal],
        "por_canal": canal_filas,
    })
