"""
Aplicación web del sistema de inventario — Isha Boutique.

Levanta un servidor con FastAPI que muestra los productos y su stock por
sucursal, y permite dar de alta productos nuevos.

Para correrlo (desde la carpeta del proyecto):
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload
Luego abrir en el navegador:  http://127.0.0.1:8000
"""

import io
import json
import os
import secrets
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from app.db import engine

# Ancho máximo de una foto guardada (en píxeles). Suficiente para identificar
# la prenda en pantalla; evita que fotos de celular (varios MB) inflen la base.
FOTO_ANCHO_MAX = 1000

# Días sin comprar a partir de los cuales una clienta se marca como "no ha vuelto".
DIAS_SIN_COMPRAR_ALERTA = 60

# ---------------------------------------------------------------------------
# Protección con usuario y contraseña (HTTP Basic Auth).
# El navegador muestra un cuadro de login antes de dejar ver cualquier página.
# Usuario y contraseña viven en variables de entorno (Railway), NUNCA en el código.
# ---------------------------------------------------------------------------
seguridad = HTTPBasic()


def requiere_login(credenciales: HTTPBasicCredentials = Depends(seguridad)) -> None:
    usuario_correcto = os.getenv("APP_USUARIO", "admin")
    clave_correcta = os.getenv("APP_CLAVE", "")

    # compare_digest evita que un atacante adivine la clave midiendo tiempos de respuesta.
    usuario_ok = secrets.compare_digest(credenciales.username, usuario_correcto)
    clave_ok = secrets.compare_digest(credenciales.password, clave_correcta)

    if not (usuario_ok and clave_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos.",
            headers={"WWW-Authenticate": "Basic"},
        )


# dependencies=[...] aplica el login a TODAS las rutas de la app, sin
# tener que repetirlo una por una.
app = FastAPI(title="Inventario Isha Boutique", dependencies=[Depends(requiere_login)])

# Carpeta donde viven las plantillas HTML (se calcula relativa a este archivo).
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Archivos estáticos (ej. el logo de la boutique). Nota: los archivos montados
# así NO pasan por requiere_login (son públicos) — aceptable porque son solo
# recursos de marca (logo), no datos del negocio.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

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


def calcular_precio_desde_utilidad(costo, utilidad_pct) -> float | None:
    """precio = costo × (1 + utilidad_pct/100). None si falta costo o utilidad."""
    if costo is None or utilidad_pct is None:
        return None
    return round(float(costo) * (1 + utilidad_pct / 100), 2)


def resolver_precio_venta(producto_row, tipo_precio: str, precio_indicado, descuento_pct) -> float | None:
    """Calcula el precio unitario final de una venta (reutilizado por venta
    individual y venta múltiple). Devuelve None si falta el precio de lista
    del tipo elegido y tampoco se indicó un precio manual.
    """
    precio_lista = producto_row["precio"] if tipo_precio == "menudeo" else producto_row["precio_mayoreo"]
    precio_unitario = precio_indicado if precio_indicado is not None else precio_lista
    if precio_unitario is None:
        return None
    if descuento_pct:
        precio_unitario = round(float(precio_unitario) * (1 - descuento_pct / 100), 2)
    return float(precio_unitario)


def calcular_margen(precio, costo) -> float | None:
    """Margen % = (precio - costo) / precio × 100. None si falta un dato o precio es 0."""
    if not precio or costo is None:
        return None
    return float((precio - costo) / precio * 100)


def procesar_foto(contenido: bytes) -> tuple[bytes, str]:
    """Redimensiona y comprime una foto antes de guardarla en la base.

    Recibe los bytes tal como los subió el navegador (puede ser una foto de
    celular de varios MB) y devuelve (bytes_listos, "image/jpeg") ya achicada
    a un ancho máximo y comprimida como JPEG, para no inflar la base de datos.
    """
    imagen = Image.open(io.BytesIO(contenido))
    imagen = imagen.convert("RGB")  # normaliza PNG/CMYK/transparencias a RGB plano

    if imagen.width > FOTO_ANCHO_MAX:
        alto_nuevo = int(imagen.height * (FOTO_ANCHO_MAX / imagen.width))
        imagen = imagen.resize((FOTO_ANCHO_MAX, alto_nuevo), Image.LANCZOS)

    buffer = io.BytesIO()
    imagen.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), "image/jpeg"


@app.get("/")
def pagina_principal(request: Request, error: str | None = None):
    """Muestra la tabla de productos con su stock por sucursal."""
    with engine.connect() as conn:
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()

        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo, "
            "       (foto IS NOT NULL) AS tiene_foto "
            "FROM productos WHERE activo = TRUE ORDER BY titulo"
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
            "precio_mayoreo": p["precio_mayoreo"],
            "costo": p["costo"],
            "margen": calcular_margen(p["precio"], p["costo"]),
            "margen_mayoreo": calcular_margen(p["precio_mayoreo"], p["costo"]),
            "tiene_foto": p["tiene_foto"],
            "cantidades": [stock_map.get((p["id"], s["id"]), 0) for s in sucursales],
        })

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sucursales": sucursales,
            "productos": productos,
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
    tipo_precio: str = Form("menudeo"),
    cantidad: str = Form(...),
    precio: str = Form(""),
    descuento: str = Form(""),   # opcional: % de descuento sobre el precio ya resuelto
    cliente_id: str = Form(""),  # opcional: puede venir vacío ("Sin clienta")
    monto_pagado: str = Form(""),   # opcional: vacío = se asume que pagó todo
    metodo_pago: str = Form("efectivo"),
):
    """Registra una venta: descuenta stock y guarda la info financiera y el pago."""
    canal = canal.strip()
    tipo_precio = tipo_precio.strip()
    metodo_pago = metodo_pago.strip()

    if metodo_pago not in ("efectivo", "tarjeta", "transferencia"):
        return RedirectResponse("/ventas?error=Método de pago inválido.", status_code=303)

    # La cantidad debe ser un entero mayor a 0.
    try:
        cantidad_num = int(cantidad.strip())
    except ValueError:
        return RedirectResponse("/ventas?error=La cantidad debe ser un número entero.", status_code=303)
    if cantidad_num <= 0:
        return RedirectResponse("/ventas?error=La cantidad debe ser mayor a 0.", status_code=303)

    if canal not in ("boutique", "ecommerce"):
        return RedirectResponse("/ventas?error=Canal de venta inválido.", status_code=303)

    if tipo_precio not in ("menudeo", "mayoreo"):
        return RedirectResponse("/ventas?error=Tipo de precio inválido.", status_code=303)

    precio_indicado = parsear_dinero(precio)

    # El descuento es un % opcional entre 0 y 100.
    descuento_pct = parsear_dinero(descuento)
    if descuento_pct is not None and not (0 <= descuento_pct <= 100):
        return RedirectResponse("/ventas?error=El descuento debe ser un % entre 0 y 100.", status_code=303)

    # Convierte el select de clienta a número o None ("Sin clienta" llega vacío).
    cliente_id_num = int(cliente_id) if cliente_id.strip() else None

    with engine.begin() as conn:
        # Trae los 2 precios de lista y el costo del producto.
        producto = conn.execute(text(
            "SELECT precio, precio_mayoreo, costo FROM productos WHERE id = :p"
        ), {"p": producto_id}).mappings().one_or_none()

        if producto is None:
            return RedirectResponse("/ventas?error=Producto no encontrado.", status_code=303)

        precio_unitario = resolver_precio_venta(producto, tipo_precio, precio_indicado, descuento_pct)
        if precio_unitario is None:
            return RedirectResponse(
                f"/ventas?error=Falta el precio de {tipo_precio} (el producto no tiene ese precio de lista).",
                status_code=303,
            )

        costo_unitario = producto["costo"]  # puede ser None si no se capturó

        # Valida el monto pagado ANTES de escribir nada (si no, un error aquí
        # dejaría la venta a medias, porque el bloque ya habría hecho commit).
        # Si se deja vacío, se asume que pagó todo (el caso más común).
        total_venta = float(precio_unitario) * cantidad_num
        monto_pagado_num = parsear_dinero(monto_pagado)
        if monto_pagado_num is None:
            monto_pagado_num = total_venta

        if monto_pagado_num < 0 or monto_pagado_num > total_venta:
            return RedirectResponse(
                f"/ventas?error=El monto pagado debe estar entre 0 y el total (${total_venta:.2f}).",
                status_code=303,
            )

        # Verifica que haya stock suficiente en esa sucursal.
        actual = conn.execute(text(
            "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
        ), {"p": producto_id, "s": sucursal_id}).scalar()
        actual = actual if actual is not None else 0

        if actual < cantidad_num:
            return RedirectResponse(
                f"/ventas?error=No hay suficiente stock (hay {actual}, intentas vender {cantidad_num}).",
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
        venta_id = conn.execute(text(
            "INSERT INTO ventas "
            "(producto_id, sucursal_id, canal, tipo_precio, cantidad, precio_unitario, "
            " costo_unitario, cliente_id, descuento_pct) "
            "VALUES (:p, :s, :canal, :tipo_precio, :cant, :precio, :costo, :cliente, :descuento) "
            "RETURNING id"
        ), {
            "p": producto_id, "s": sucursal_id, "canal": canal, "tipo_precio": tipo_precio,
            "cant": cantidad_num, "precio": precio_unitario, "costo": costo_unitario,
            "cliente": cliente_id_num, "descuento": descuento_pct,
        }).scalar_one()

        # 4) Registra el pago inicial (ya validado arriba). Si el monto es 0,
        #    la venta queda "pendiente" (apartado sin anticipo).
        if monto_pagado_num > 0:
            conn.execute(text(
                "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
            ), {"v": venta_id, "metodo": metodo_pago, "monto": monto_pagado_num})

    return RedirectResponse("/ventas", status_code=303)


@app.get("/ventas")
def ver_ventas(request: Request, error: str | None = None):
    """Punto de venta: registrar una venta y ver el estado de pago de todas."""
    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, precio, precio_mayoreo FROM productos "
            "WHERE activo = TRUE ORDER BY titulo"
        )).mappings().all()

        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()

        clientas = conn.execute(text(
            "SELECT id, nombre FROM clientas ORDER BY nombre"
        )).mappings().all()

        ventas_rows = conn.execute(text(
            "SELECT v.id, v.creada_en, p.titulo, s.nombre AS sucursal, v.canal, "
            "       v.tipo_precio, c.nombre AS clienta, v.cantidad, v.precio_unitario, "
            "       (v.precio_unitario * v.cantidad) AS total "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "ORDER BY v.creada_en DESC LIMIT 200"
        )).mappings().all()

        pagos_rows = conn.execute(text(
            "SELECT venta_id, metodo, monto, creado_en FROM pagos ORDER BY creado_en"
        )).mappings().all()

    # Agrupa los pagos por venta: venta_id -> lista de pagos.
    pagos_por_venta: dict[int, list] = {}
    for pg in pagos_rows:
        pagos_por_venta.setdefault(pg["venta_id"], []).append(pg)

    ventas = []
    for v in ventas_rows:
        pagos_venta = pagos_por_venta.get(v["id"], [])
        pagado = sum(float(pg["monto"]) for pg in pagos_venta)
        total = float(v["total"])
        saldo = round(total - pagado, 2)

        if saldo <= 0:
            estado = "Pagado"
        elif pagado > 0:
            estado = "Parcial"
        else:
            estado = "Pendiente"

        etiquetas_metodo = {"efectivo": "Efectivo", "tarjeta": "Tarjeta", "transferencia": "Transferencia"}
        metodos = ", ".join(etiquetas_metodo.get(pg["metodo"], pg["metodo"]) for pg in pagos_venta) or "—"

        ventas.append({
            "id": v["id"],
            "creada_en": v["creada_en"].astimezone(ZONA_CDMX),
            "titulo": v["titulo"],
            "sucursal": v["sucursal"],
            "canal": v["canal"],
            "tipo_precio": v["tipo_precio"],
            "clienta": v["clienta"],
            "cantidad": v["cantidad"],
            "total": total,
            "pagado": pagado,
            "saldo": saldo,
            "estado": estado,
            "metodos": metodos,
        })

    return templates.TemplateResponse(request, "ventas.html", {
        "ventas": ventas, "error": error,
        "productos": productos, "sucursales": sucursales, "clientas": clientas,
    })


@app.post("/ventas/{venta_id}/pagos")
def registrar_pago(venta_id: int, metodo: str = Form(...), monto: str = Form(...)):
    """Registra un abono adicional a una venta que ya existe (para saldar el pendiente)."""
    metodo = metodo.strip()
    if metodo not in ("efectivo", "tarjeta", "transferencia"):
        return RedirectResponse("/ventas?error=Método de pago inválido.", status_code=303)

    monto_num = parsear_dinero(monto)
    if monto_num is None or monto_num <= 0:
        return RedirectResponse("/ventas?error=El monto del abono debe ser mayor a 0.", status_code=303)

    with engine.begin() as conn:
        venta = conn.execute(text(
            "SELECT precio_unitario, cantidad FROM ventas WHERE id = :id"
        ), {"id": venta_id}).mappings().one_or_none()

        if venta is None:
            return RedirectResponse("/ventas?error=Venta no encontrada.", status_code=303)

        total = float(venta["precio_unitario"]) * venta["cantidad"]
        pagado_actual = conn.execute(text(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE venta_id = :id"
        ), {"id": venta_id}).scalar()
        saldo = round(total - float(pagado_actual), 2)

        if monto_num > saldo:
            return RedirectResponse(
                f"/ventas?error=El abono (${monto_num:.2f}) es mayor al saldo pendiente (${saldo:.2f}).",
                status_code=303,
            )

        conn.execute(text(
            "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
        ), {"v": venta_id, "metodo": metodo, "monto": monto_num})

    return RedirectResponse("/ventas", status_code=303)


@app.post("/ventas/{venta_id}/eliminar")
def eliminar_venta(venta_id: int):
    """Elimina una venta (ej. una de prueba) y repone el stock que se había
    descontado, para que el inventario no quede descuadrado.

    No se intenta borrar el movimiento 'venta' original (la tabla movimientos
    no guarda a qué venta pertenece cada renglón, así que no hay forma
    confiable de identificar cuál es exactamente). En su lugar se registra un
    movimiento nuevo tipo 'ajuste' que repone la cantidad, dejando rastro
    claro en el historial de que la venta se canceló.
    """
    with engine.begin() as conn:
        venta = conn.execute(text(
            "SELECT producto_id, sucursal_id, cantidad FROM ventas WHERE id = :id"
        ), {"id": venta_id}).mappings().one_or_none()

        if venta is None:
            return RedirectResponse("/ventas?error=Venta no encontrada.", status_code=303)

        # Repone el stock que se había descontado al vender.
        conn.execute(text(
            "UPDATE stock SET cantidad = cantidad + :c, actualizado_en = NOW() "
            "WHERE producto_id = :p AND sucursal_id = :s"
        ), {"c": venta["cantidad"], "p": venta["producto_id"], "s": venta["sucursal_id"]})

        # Deja rastro de la cancelación en el historial de movimientos.
        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, 'ajuste', :delta, :motivo)"
        ), {
            "p": venta["producto_id"], "s": venta["sucursal_id"],
            "delta": venta["cantidad"], "motivo": f"venta #{venta_id} eliminada (stock repuesto)",
        })

        # Borra la venta; sus pagos se eliminan solos (ON DELETE CASCADE).
        conn.execute(text("DELETE FROM ventas WHERE id = :id"), {"id": venta_id})

    return RedirectResponse("/ventas", status_code=303)


@app.get("/ventas/nota")
def nota_pedido(request: Request, id: list[int] = Query(default=[])):
    """Genera una nota de pedido imprimible con varias ventas juntas (ej. las
    10 prendas que se llevó una clienta), seleccionadas con checkboxes en /ventas.
    """
    if not id:
        return RedirectResponse(
            "/ventas?error=Selecciona al menos una venta para imprimir la nota.",
            status_code=303,
        )

    ids_stmt = lambda sql: text(sql).bindparams(bindparam("ids", expanding=True))

    with engine.connect() as conn:
        filas = conn.execute(ids_stmt(
            "SELECT v.id, p.titulo, p.sku, v.cantidad, v.precio_unitario, c.nombre AS clienta "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "WHERE v.id IN :ids ORDER BY v.id"
        ), {"ids": id}).mappings().all()

        pagos = conn.execute(ids_stmt(
            "SELECT venta_id, monto FROM pagos WHERE venta_id IN :ids"
        ), {"ids": id}).all()

    if not filas:
        return RedirectResponse("/ventas?error=No se encontraron las ventas seleccionadas.", status_code=303)

    # Suma lo pagado de cada venta seleccionada.
    pagado_por_venta: dict[int, float] = {}
    for venta_id, monto in pagos:
        pagado_por_venta[venta_id] = pagado_por_venta.get(venta_id, 0.0) + float(monto)

    items = []
    total = 0.0
    pagado_total = 0.0
    clientas_distintas = set()
    for f in filas:
        subtotal = float(f["precio_unitario"]) * f["cantidad"]
        total += subtotal
        pagado_total += pagado_por_venta.get(f["id"], 0.0)
        clientas_distintas.add(f["clienta"])
        items.append({
            "titulo": f["titulo"], "sku": f["sku"],
            "cantidad": f["cantidad"], "precio_unitario": float(f["precio_unitario"]),
            "subtotal": subtotal,
        })

    # Si todas las ventas seleccionadas son de la misma clienta, se muestra su
    # nombre; si se mezclan clientas distintas (o ninguna), se deja en blanco.
    clienta_nombre = next(iter(clientas_distintas)) if len(clientas_distintas) == 1 else None

    return templates.TemplateResponse(request, "nota_pedido.html", {
        "items": items,
        "total": total,
        "pagado": pagado_total,
        "saldo": round(total - pagado_total, 2),
        "clienta": clienta_nombre,
        "fecha": datetime.now(ZONA_CDMX),
    })


@app.get("/ventas/carrito")
def carrito_venta(request: Request, error: str | None = None):
    """Formulario para registrar varias ventas de una vez (ej. una clienta
    que se lleva 7 prendas), en lugar de repetir el formulario una por una.
    """
    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, precio, precio_mayoreo FROM productos "
            "WHERE activo = TRUE ORDER BY titulo"
        )).mappings().all()
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
        clientas = conn.execute(text(
            "SELECT id, nombre FROM clientas ORDER BY nombre"
        )).mappings().all()

    # Lista de productos como JSON, para que el JavaScript arme filas nuevas
    # del carrito sin recargar la página.
    productos_json = json.dumps([
        {
            "id": p["id"],
            "texto": f"{p['titulo']} ({p['sku']})",
            "precio": float(p["precio"]) if p["precio"] is not None else None,
            "precioMayoreo": float(p["precio_mayoreo"]) if p["precio_mayoreo"] is not None else None,
        }
        for p in productos
    ])

    return templates.TemplateResponse(request, "carrito.html", {
        "sucursales": sucursales, "clientas": clientas, "error": error,
        "productos_json": productos_json,
    })


@app.post("/ventas/carrito")
def registrar_carrito(
    sucursal_id: int = Form(...),
    canal: str = Form(...),
    cliente_id: str = Form(""),
    monto_pagado: str = Form(""),
    metodo_pago: str = Form("efectivo"),
    producto_id: list[int] = Form(...),
    tipo_precio: list[str] = Form(...),
    cantidad: list[str] = Form(...),
    precio: list[str] = Form(...),
    descuento: list[str] = Form(...),
):
    """Registra varias ventas de una sola vez (carrito). Comparten sucursal,
    canal, clienta y un pago combinado que se reparte entre ellas (se va
    saldando una por una, en orden, hasta que se acaba lo pagado). Al
    terminar, redirige directo a la nota de pedido imprimible con todas.
    """
    canal = canal.strip()
    metodo_pago = metodo_pago.strip()

    if metodo_pago not in ("efectivo", "tarjeta", "transferencia"):
        return RedirectResponse("/ventas/carrito?error=Método de pago inválido.", status_code=303)
    if canal not in ("boutique", "ecommerce"):
        return RedirectResponse("/ventas/carrito?error=Canal de venta inválido.", status_code=303)

    n = len(producto_id)
    if n == 0 or not (len(tipo_precio) == len(cantidad) == len(precio) == len(descuento) == n):
        return RedirectResponse("/ventas/carrito?error=Agrega al menos una prenda al carrito.", status_code=303)

    cliente_id_num = int(cliente_id) if cliente_id.strip() else None

    with engine.begin() as conn:
        # --- Paso 1: resolver y validar CADA renglón (solo lecturas, nada se
        # escribe todavía) para no dejar el carrito a medias si algo falla. ---
        items_resueltos = []
        for i in range(n):
            tipo_i = tipo_precio[i].strip()
            if tipo_i not in ("menudeo", "mayoreo"):
                return RedirectResponse(f"/ventas/carrito?error=Tipo de precio inválido en la prenda {i + 1}.", status_code=303)

            try:
                cantidad_i = int(cantidad[i].strip())
            except ValueError:
                return RedirectResponse(f"/ventas/carrito?error=Cantidad inválida en la prenda {i + 1}.", status_code=303)
            if cantidad_i <= 0:
                return RedirectResponse(f"/ventas/carrito?error=La cantidad debe ser mayor a 0 en la prenda {i + 1}.", status_code=303)

            descuento_i = parsear_dinero(descuento[i])
            if descuento_i is not None and not (0 <= descuento_i <= 100):
                return RedirectResponse(f"/ventas/carrito?error=Descuento inválido en la prenda {i + 1}.", status_code=303)

            producto = conn.execute(text(
                "SELECT precio, precio_mayoreo, costo FROM productos WHERE id = :p"
            ), {"p": producto_id[i]}).mappings().one_or_none()
            if producto is None:
                return RedirectResponse(f"/ventas/carrito?error=Producto no encontrado en la prenda {i + 1}.", status_code=303)

            precio_unitario_i = resolver_precio_venta(producto, tipo_i, parsear_dinero(precio[i]), descuento_i)
            if precio_unitario_i is None:
                return RedirectResponse(
                    f"/ventas/carrito?error=Falta el precio de {tipo_i} en la prenda {i + 1}.", status_code=303,
                )

            # Verifica stock suficiente en la sucursal compartida del carrito.
            stock_actual = conn.execute(text(
                "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
            ), {"p": producto_id[i], "s": sucursal_id}).scalar()
            stock_actual = stock_actual if stock_actual is not None else 0
            if stock_actual < cantidad_i:
                return RedirectResponse(
                    f"/ventas/carrito?error=No hay suficiente stock en la prenda {i + 1} "
                    f"(hay {stock_actual}, pediste {cantidad_i}).",
                    status_code=303,
                )

            items_resueltos.append({
                "producto_id": producto_id[i], "tipo_precio": tipo_i, "cantidad": cantidad_i,
                "precio_unitario": precio_unitario_i, "costo_unitario": producto["costo"],
                "descuento_pct": descuento_i, "subtotal": precio_unitario_i * cantidad_i,
            })

        # --- Paso 2: validar el pago combinado ANTES de escribir nada. ---
        total_pedido = sum(it["subtotal"] for it in items_resueltos)
        monto_pagado_num = parsear_dinero(monto_pagado)
        if monto_pagado_num is None:
            monto_pagado_num = total_pedido

        if monto_pagado_num < 0 or monto_pagado_num > total_pedido:
            return RedirectResponse(
                f"/ventas/carrito?error=El monto pagado debe estar entre 0 y el total (${total_pedido:.2f}).",
                status_code=303,
            )

        # --- Paso 3: ya validado todo; ahora sí se descuenta stock y se crea cada venta. ---
        venta_ids = []
        for it in items_resueltos:
            conn.execute(text(
                "UPDATE stock SET cantidad = cantidad - :c, actualizado_en = NOW() "
                "WHERE producto_id = :p AND sucursal_id = :s"
            ), {"c": it["cantidad"], "p": it["producto_id"], "s": sucursal_id})

            conn.execute(text(
                "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                "VALUES (:p, :s, 'venta', :delta, :motivo)"
            ), {
                "p": it["producto_id"], "s": sucursal_id,
                "delta": -it["cantidad"], "motivo": f"venta {canal} (carrito)",
            })

            venta_id = conn.execute(text(
                "INSERT INTO ventas "
                "(producto_id, sucursal_id, canal, tipo_precio, cantidad, precio_unitario, "
                " costo_unitario, cliente_id, descuento_pct) "
                "VALUES (:p, :s, :canal, :tipo_precio, :cant, :precio, :costo, :cliente, :descuento) "
                "RETURNING id"
            ), {
                "p": it["producto_id"], "s": sucursal_id, "canal": canal, "tipo_precio": it["tipo_precio"],
                "cant": it["cantidad"], "precio": it["precio_unitario"], "costo": it["costo_unitario"],
                "cliente": cliente_id_num, "descuento": it["descuento_pct"],
            }).scalar_one()
            venta_ids.append(venta_id)

        # --- Paso 4: reparte el pago combinado entre las ventas creadas, en
        # orden, hasta agotarlo (la primera se salda completa, luego la
        # siguiente, etc. — igual que pagar varias cosas con un solo billete).
        restante = monto_pagado_num
        for it, venta_id in zip(items_resueltos, venta_ids):
            pago_este = min(restante, it["subtotal"])
            if pago_este > 0:
                conn.execute(text(
                    "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
                ), {"v": venta_id, "metodo": metodo_pago, "monto": round(pago_este, 2)})
            restante -= pago_este

    # Lleva directo a la nota de pedido imprimible con las ventas recién creadas.
    query = "&".join(f"id={vid}" for vid in venta_ids)
    return RedirectResponse(f"/ventas/nota?{query}", status_code=303)


@app.post("/productos")
def crear_producto(
    sku: str = Form(...),
    titulo: str = Form(...),
    categoria: str = Form(""),
    costo: str = Form(""),
    precio: str = Form(""),           # opcional: precio de venta menudeo
    precio_mayoreo: str = Form(""),   # opcional: precio de venta mayoreo
    utilidad_menudeo: str = Form(""),  # opcional: % utilidad -> calcula precio menudeo
    utilidad_mayoreo: str = Form(""),  # opcional: % utilidad -> calcula precio mayoreo
    sucursal_inicial: str = Form(""),   # opcional: dónde llegó la mercancía
    cantidad_inicial: str = Form(""),   # opcional: cuántas piezas llegaron
    foto: UploadFile | None = File(None),  # opcional: foto de la prenda
):
    """Da de alta un producto (momento de compra) y le crea stock en 0 en cada sucursal.

    Si se indica sucursal + cantidad inicial, además registra esa entrada de una
    vez (mismo efecto que ir después a "Registrar movimiento" → Entrada).
    Los precios (menudeo/mayoreo) son opcionales aquí: se pueden dejar vacíos y
    completar/ajustar después con el botón Editar. En vez de un precio exacto,
    también se puede indicar un % de utilidad sobre el costo (precio = costo ×
    (1 + %/100)); si se da el %, tiene prioridad sobre el precio escrito a mano.
    """
    # Limpia los textos (quita espacios sobrantes).
    sku = sku.strip()
    titulo = titulo.strip()
    categoria = categoria.strip() or None

    # Convierte costo y precios a número (None si vienen vacíos o mal escritos).
    costo_valor = parsear_dinero(costo)
    precio_valor = parsear_dinero(precio)
    precio_mayoreo_valor = parsear_dinero(precio_mayoreo)

    # Si se dio un % de utilidad, calcula el precio a partir del costo (tiene
    # prioridad sobre el precio manual de arriba, si también se escribió uno).
    utilidad_menudeo_pct = parsear_dinero(utilidad_menudeo)
    utilidad_mayoreo_pct = parsear_dinero(utilidad_mayoreo)

    if utilidad_menudeo_pct is not None or utilidad_mayoreo_pct is not None:
        if costo_valor is None:
            return RedirectResponse(
                "/?error=Para usar % de utilidad necesitas indicar el costo de compra.",
                status_code=303,
            )
        if utilidad_menudeo_pct is not None:
            precio_valor = calcular_precio_desde_utilidad(costo_valor, utilidad_menudeo_pct)
        if utilidad_mayoreo_pct is not None:
            precio_mayoreo_valor = calcular_precio_desde_utilidad(costo_valor, utilidad_mayoreo_pct)

    # La cantidad inicial es opcional: si viene vacía o es 0, no se registra entrada.
    try:
        cantidad_inicial_num = int(cantidad_inicial.strip()) if cantidad_inicial.strip() else 0
    except ValueError:
        cantidad_inicial_num = 0
    sucursal_inicial_id = int(sucursal_inicial) if sucursal_inicial.strip() else None

    # Si se subió una foto (el campo viene vacío si no se eligió archivo), la procesamos.
    foto_bytes, foto_tipo = None, None
    if foto is not None and foto.filename:
        foto_bytes, foto_tipo = procesar_foto(foto.file.read())

    try:
        # Todo dentro de una transacción: o se guarda todo, o nada.
        with engine.begin() as conn:
            nuevo_id = conn.execute(text(
                "INSERT INTO productos "
                "(sku, titulo, categoria, costo, precio, precio_mayoreo, foto, foto_tipo) "
                "VALUES (:sku, :titulo, :categoria, :costo, :precio, :precio_mayoreo, :foto, :foto_tipo) "
                "RETURNING id"
            ), {
                "sku": sku, "titulo": titulo,
                "categoria": categoria, "costo": costo_valor,
                "precio": precio_valor, "precio_mayoreo": precio_mayoreo_valor,
                "foto": foto_bytes, "foto_tipo": foto_tipo,
            }).scalar_one()

            sucursal_ids = conn.execute(text(
                "SELECT id FROM sucursales WHERE activa = TRUE"
            )).scalars().all()

            for suc_id in sucursal_ids:
                # Si esta es la sucursal donde llegó la mercancía, ya nace con
                # esa cantidad; las demás sucursales nacen en 0, como siempre.
                cantidad_inicial_suc = (
                    cantidad_inicial_num if suc_id == sucursal_inicial_id else 0
                )
                conn.execute(text(
                    "INSERT INTO stock (producto_id, sucursal_id, cantidad) "
                    "VALUES (:producto_id, :sucursal_id, :cantidad)"
                ), {"producto_id": nuevo_id, "sucursal_id": suc_id, "cantidad": cantidad_inicial_suc})

            # Si hubo cantidad inicial, deja rastro en el historial de movimientos
            # (igual que una entrada normal), para que quede trazable.
            if sucursal_inicial_id is not None and cantidad_inicial_num > 0:
                conn.execute(text(
                    "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                    "VALUES (:p, :s, 'entrada', :delta, 'alta inicial del producto')"
                ), {"p": nuevo_id, "s": sucursal_inicial_id, "delta": cantidad_inicial_num})

    except IntegrityError:
        # Pasa si el SKU ya existe (regla UNIQUE). Avisamos sin romper.
        mensaje = f"El SKU '{sku}' ya existe. Usa uno diferente."
        return RedirectResponse(f"/?error={mensaje}", status_code=303)

    # 303 hace que el navegador vuelva a "/" con un GET (evita reenviar el form).
    return RedirectResponse("/", status_code=303)


@app.get("/productos/{producto_id}/foto")
def foto_producto(producto_id: int):
    """Devuelve la foto de un producto para que un <img src="..."> la muestre."""
    with engine.connect() as conn:
        fila = conn.execute(text(
            "SELECT foto, foto_tipo FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

    if fila is None or fila["foto"] is None:
        raise HTTPException(status_code=404, detail="Este producto no tiene foto.")

    return Response(content=bytes(fila["foto"]), media_type=fila["foto_tipo"] or "image/jpeg")


@app.get("/productos/{producto_id}/historial")
def historial_producto(request: Request, producto_id: int):
    """Ficha de un producto: sus datos y todo su historial de movimientos y ventas."""
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo, "
            "       (foto IS NOT NULL) AS tiene_foto "
            "FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

        if producto is None:
            return RedirectResponse("/?error=Producto no encontrado.", status_code=303)

        movimientos_rows = conn.execute(text(
            "SELECT m.creado_en, s.nombre AS sucursal, m.tipo, m.delta, m.motivo "
            "FROM movimientos m JOIN sucursales s ON s.id = m.sucursal_id "
            "WHERE m.producto_id = :id ORDER BY m.creado_en DESC"
        ), {"id": producto_id}).mappings().all()

        ventas_rows = conn.execute(text(
            "SELECT v.creada_en, v.cantidad, v.precio_unitario, v.costo_unitario, "
            "       v.canal, v.tipo_precio, v.descuento_pct, s.nombre AS sucursal, c.nombre AS clienta "
            "FROM ventas v "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "WHERE v.producto_id = :id ORDER BY v.creada_en DESC"
        ), {"id": producto_id}).mappings().all()

    # Convierte fechas a hora de CDMX, solo para mostrar.
    movimientos = [
        {**dict(m), "creado_en": m["creado_en"].astimezone(ZONA_CDMX)}
        for m in movimientos_rows
    ]
    ventas = [
        {**dict(v), "creada_en": v["creada_en"].astimezone(ZONA_CDMX)}
        for v in ventas_rows
    ]

    return templates.TemplateResponse(request, "producto_historial.html", {
        "producto": producto, "movimientos": movimientos, "ventas": ventas,
    })


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

    # Convierte la fecha de última compra a hora de CDMX y calcula días sin comprar.
    ahora = datetime.now(ZONA_CDMX)
    clientas = []
    for f in filas:
        d = dict(f)
        if d["ultima_compra"] is not None:
            d["ultima_compra"] = d["ultima_compra"].astimezone(ZONA_CDMX)
            d["dias_sin_comprar"] = (ahora - d["ultima_compra"]).days
        else:
            d["dias_sin_comprar"] = None
        clientas.append(d)

    # Alertas: clientas que SÍ han comprado antes pero llevan muchos días sin volver.
    no_han_vuelto = sorted(
        [c for c in clientas if c["dias_sin_comprar"] is not None
         and c["dias_sin_comprar"] >= DIAS_SIN_COMPRAR_ALERTA],
        key=lambda c: c["dias_sin_comprar"], reverse=True,
    )

    # Top clientas por lo que han gastado en total (solo las que sí han comprado algo).
    top_clientas = sorted(
        [c for c in clientas if c["total_comprado"] > 0],
        key=lambda c: c["total_comprado"], reverse=True,
    )[:5]

    return templates.TemplateResponse(request, "clientas.html", {
        "clientas": clientas,
        "no_han_vuelto": no_han_vuelto,
        "top_clientas": top_clientas,
        "dias_alerta": DIAS_SIN_COMPRAR_ALERTA,
        "error": error,
    })


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
            "SELECT v.creada_en, p.titulo, v.cantidad, v.precio_unitario, s.nombre AS sucursal, "
            "       v.canal, v.tipo_precio, v.descuento_pct "
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
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo, "
            "       (foto IS NOT NULL) AS tiene_foto "
            "FROM productos WHERE id = :id"
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
    precio_mayoreo: str = Form(""),
    utilidad_menudeo: str = Form(""),  # opcional: % utilidad -> recalcula precio menudeo
    utilidad_mayoreo: str = Form(""),  # opcional: % utilidad -> recalcula precio mayoreo
    costo: str = Form(""),
    foto: UploadFile | None = File(None),
):
    """Guarda los cambios del producto (título, categoría, los 2 precios, costo y foto).

    Los precios se pueden escribir a mano, o recalcular a partir de un % de
    utilidad sobre el costo (precio = costo × (1 + %/100)); si se da el %,
    tiene prioridad sobre el precio escrito a mano en el mismo envío.

    La foto solo se reemplaza si se elige un archivo nuevo; si no, se conserva
    la que ya tenía (no hace falta volver a subirla en cada edición).
    """
    costo_valor = parsear_dinero(costo)
    precio_valor = parsear_dinero(precio)
    precio_mayoreo_valor = parsear_dinero(precio_mayoreo)

    utilidad_menudeo_pct = parsear_dinero(utilidad_menudeo)
    utilidad_mayoreo_pct = parsear_dinero(utilidad_mayoreo)

    if utilidad_menudeo_pct is not None or utilidad_mayoreo_pct is not None:
        if costo_valor is None:
            return RedirectResponse(
                "/?error=Para usar % de utilidad necesitas indicar el costo de compra.",
                status_code=303,
            )
        if utilidad_menudeo_pct is not None:
            precio_valor = calcular_precio_desde_utilidad(costo_valor, utilidad_menudeo_pct)
        if utilidad_mayoreo_pct is not None:
            precio_mayoreo_valor = calcular_precio_desde_utilidad(costo_valor, utilidad_mayoreo_pct)

    campos_sql = (
        "titulo = :titulo, categoria = :categoria, "
        "precio = :precio, precio_mayoreo = :precio_mayoreo, "
        "costo = :costo, actualizado_en = NOW()"
    )
    parametros = {
        "titulo": titulo.strip(),
        "categoria": categoria.strip() or None,
        "precio": precio_valor,
        "precio_mayoreo": precio_mayoreo_valor,
        "costo": costo_valor,
        "id": producto_id,
    }

    if foto is not None and foto.filename:
        foto_bytes, foto_tipo = procesar_foto(foto.file.read())
        campos_sql += ", foto = :foto, foto_tipo = :foto_tipo"
        parametros["foto"] = foto_bytes
        parametros["foto_tipo"] = foto_tipo

    with engine.begin() as conn:
        conn.execute(text(f"UPDATE productos SET {campos_sql} WHERE id = :id"), parametros)

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
