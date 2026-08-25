"""
Aplicación web del sistema de inventario — Isha Boutique.

Levanta un servidor con FastAPI que muestra los productos y su stock por
sucursal, y permite dar de alta productos nuevos.

Para correrlo (desde la carpeta del proyecto):
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload
Luego abrir en el navegador:  http://127.0.0.1:8000
"""

import csv
import hashlib
import io
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
import qrcode
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from app.db import engine
from app.shopify_sync import (
    crear_producto_en_shopify,
    empujar_stock_producto_seguro,
    empujar_stock_productos_seguro,
    FaltaPrecioError,
    ProductoYaLigadoError,
)
from app.shopify_webhooks import verificar_firma, procesar_pedido

# Ancho máximo de una foto guardada (en píxeles). Suficiente para identificar
# la prenda en pantalla; evita que fotos de celular (varios MB) inflen la base.
FOTO_ANCHO_MAX = 1000

# Días sin comprar a partir de los cuales una clienta se marca como "no ha vuelto".
DIAS_SIN_COMPRAR_ALERTA = 60

# Canales de venta válidos y su nombre bonito para mostrar en pantalla/reportes.
CANALES = {
    "boutique": "Boutique",
    "ecommerce": "E-commerce",
    "consignacion": "Consignación",
    "mayoreo": "Mayoreo",
    "domicilio": "Domicilio",
}

# Días sin venderse a partir de los cuales una prenda con stock se marca
# como "no se mueve" (candidata a liquidar o dejar de reordenar).
DIAS_SIN_VENDER_ALERTA = 60

# ---------------------------------------------------------------------------
# Protección con usuario y contraseña (HTTP Basic Auth), con 2 roles:
#   - admin: usuario/contraseña únicos en variables de entorno (Railway),
#     NUNCA en el código. Ve y hace todo.
#   - vendedora: una fila en la tabla vendedoras con su propio usuario y
#     contraseña (guardada con sal + hash, nunca en claro). Solo puede
#     registrar ventas — no ve reportes, caja, costos/márgenes, ni las
#     ventas de las demás.
# El navegador muestra un cuadro de login antes de dejar ver cualquier página.
# ---------------------------------------------------------------------------
seguridad = HTTPBasic()


@dataclass
class Identidad:
    rol: str  # "admin" o "vendedora"
    vendedora_id: int | None = None
    vendedora_nombre: str | None = None


def hash_clave(clave: str, salt: str | None = None) -> tuple[str, str]:
    """Sal + hash de una contraseña (para guardar o para comparar con una
    sal ya existente). Devuelve (salt, hash)."""
    salt = salt or secrets.token_hex(16)
    hash_ = hashlib.sha256((salt + clave).encode()).hexdigest()
    return salt, hash_


def requiere_login(
    request: Request, credenciales: HTTPBasicCredentials = Depends(seguridad)
) -> Identidad:
    admin_usuario = os.getenv("APP_USUARIO", "admin")
    admin_clave = os.getenv("APP_CLAVE", "")

    # compare_digest evita que un atacante adivine la clave midiendo tiempos de respuesta.
    if secrets.compare_digest(credenciales.username, admin_usuario) and secrets.compare_digest(
        credenciales.password, admin_clave
    ):
        identidad = Identidad(rol="admin")
        request.state.identidad = identidad
        return identidad

    # No es la admin: prueba contra las vendedoras con acceso al sistema.
    with engine.connect() as conn:
        fila = conn.execute(text(
            "SELECT id, nombre, clave_hash, clave_salt FROM vendedoras "
            "WHERE usuario = :u AND activa = TRUE"
        ), {"u": credenciales.username}).mappings().one_or_none()

    if fila is not None and fila["clave_hash"] is not None:
        _, hash_calculado = hash_clave(credenciales.password, fila["clave_salt"])
        if secrets.compare_digest(hash_calculado, fila["clave_hash"]):
            identidad = Identidad(rol="vendedora", vendedora_id=fila["id"], vendedora_nombre=fila["nombre"])
            request.state.identidad = identidad
            return identidad

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Usuario o contraseña incorrectos.",
        headers={"WWW-Authenticate": "Basic"},
    )


def requiere_admin(identidad: Identidad = Depends(requiere_login)) -> None:
    """Para rutas que solo la administradora puede usar (reportes, caja,
    costos/márgenes, gestión de clientas/vendedoras/inventario, etc.)."""
    if identidad.rol != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta sección es solo para la administradora.",
        )


# dependencies=[...] aplica el login (cualquiera de los 2 roles) a TODAS las
# rutas de la app; las rutas que además necesitan ser admin-only agregan
# dependencies=[Depends(requiere_admin)] en su propio decorador.
app = FastAPI(title="Inventario Isha Boutique", dependencies=[Depends(requiere_login)])

# Carpeta donde viven las plantillas HTML (se calcula relativa a este archivo).
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["etiqueta_canal"] = lambda canal: CANALES.get(canal, canal)

# Archivos estáticos (ej. el logo de la boutique). Nota: los archivos montados
# así NO pasan por requiere_login (son públicos) — aceptable porque son solo
# recursos de marca (logo), no datos del negocio.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Webhooks de Shopify: tienen que ser públicos (Shopify no puede mandar
# usuario/contraseña), así que van en una sub-app aparte que NO hereda
# requiere_login (igual que /static). La seguridad aquí es la firma HMAC
# que se verifica en cada request (ver app/shopify_webhooks.py) — sin
# firma válida, se rechaza antes de tocar la base de datos.
webhooks_app = FastAPI()


@webhooks_app.post("/shopify/orders")
async def recibir_pedido_shopify(request: Request):
    cuerpo_crudo = await request.body()
    firma = request.headers.get("X-Shopify-Hmac-Sha256")

    if not verificar_firma(cuerpo_crudo, firma):
        raise HTTPException(status_code=401, detail="Firma inválida.")

    try:
        procesar_pedido(json.loads(cuerpo_crudo))
    except Exception as error:
        # Devolver error (no 200) hace que Shopify reintente el webhook más
        # tarde — mejor eso que tragarnos el fallo y perder la venta.
        print(f"[webhooks] Error procesando pedido de Shopify: {error}")
        raise HTTPException(status_code=500, detail="No se pudo procesar el pedido.")

    return {"ok": True}


app.mount("/webhooks", webhooks_app)

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


def siguiente_numero_nota(conn, sucursal_id: int) -> int:
    """Folio consecutivo de la nota, llevado POR SUCURSAL (cada sede tiene su
    propio contador, empezando en 1). El UPSERT es atómico: si dos ventas de
    la misma sucursal se registran al mismo tiempo, no se repite el número."""
    return conn.execute(text(
        "INSERT INTO notas_folio (sucursal_id, ultimo_numero) VALUES (:s, 1) "
        "ON CONFLICT (sucursal_id) DO UPDATE SET ultimo_numero = notas_folio.ultimo_numero + 1 "
        "RETURNING ultimo_numero"
    ), {"s": sucursal_id}).scalar_one()


def calcular_precio_desde_utilidad(costo, utilidad_pct) -> float | None:
    """precio = costo × (1 + utilidad_pct/100), redondeado a pesos enteros
    (en la tienda no se manejan centavos). None si falta costo o utilidad."""
    if costo is None or utilidad_pct is None:
        return None
    return round(float(costo) * (1 + utilidad_pct / 100))


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
        # Redondeado a pesos enteros: un descuento no debe reintroducir centavos.
        precio_unitario = round(float(precio_unitario) * (1 - descuento_pct / 100))
    return float(precio_unitario)


def calcular_margen(precio, costo) -> float | None:
    """Margen % = (precio - costo) / precio × 100. None si falta un dato o precio es 0."""
    if not precio or costo is None:
        return None
    return float((precio - costo) / precio * 100)


def obtener_stock_por_producto(conn) -> dict[int, dict[int, int]]:
    """Stock de cada producto activo, por sucursal: {producto_id: {sucursal_id:
    cantidad}}. Se manda al JS de los formularios de venta para avisar en
    vivo si falta stock, sin esperar a que el servidor rechace el envío."""
    filas = conn.execute(text(
        "SELECT st.producto_id, st.sucursal_id, st.cantidad "
        "FROM stock st "
        "JOIN productos p ON p.id = st.producto_id AND p.activo = TRUE"
    )).all()
    mapa: dict[int, dict[int, int]] = {}
    for producto_id, sucursal_id, cantidad in filas:
        mapa.setdefault(producto_id, {})[sucursal_id] = cantidad
    return mapa


def productos_a_json(productos, stock_por_producto: dict[int, dict[int, int]] | None = None) -> str:
    """Convierte una lista de productos (mappings con id/sku/titulo/precio/
    precio_mayoreo) a un JSON que el JavaScript usa para buscar por SKU y
    autocompletar precios, sin tener que ir al servidor por cada búsqueda.
    Si se le da stock_por_producto, cada producto también trae su stock por
    sucursal (ver obtener_stock_por_producto).
    """
    stock_por_producto = stock_por_producto or {}
    return json.dumps([
        {
            "id": p["id"],
            "sku": p["sku"],
            "texto": f"{p['titulo']} ({p['sku']})",
            "precio": float(p["precio"]) if p["precio"] is not None else None,
            "precioMayoreo": float(p["precio_mayoreo"]) if p["precio_mayoreo"] is not None else None,
            "stock": stock_por_producto.get(p["id"], {}),
        }
        for p in productos
    ])


def procesar_foto(contenido: bytes) -> tuple[bytes, str]:
    """Redimensiona y comprime una foto antes de guardarla en la base.

    Recibe los bytes tal como los subió el navegador (puede ser una foto de
    celular de varios MB) y devuelve (bytes_listos, "image/jpeg") ya achicada
    a un ancho máximo y comprimida como JPEG, para no inflar la base de datos.
    """
    imagen = Image.open(io.BytesIO(contenido))
    # Aplica la rotación real que indica el EXIF (celulares guardan la foto
    # "acostada" tal como la capturó el sensor + una bandera de rotación; si
    # no se aplica aquí, se guarda acostada de verdad y ya no hay forma de
    # corregirla después, porque el JPEG final no lleva ese EXIF).
    imagen = ImageOps.exif_transpose(imagen)
    imagen = imagen.convert("RGB")  # normaliza PNG/CMYK/transparencias a RGB plano

    if imagen.width > FOTO_ANCHO_MAX:
        alto_nuevo = int(imagen.height * (FOTO_ANCHO_MAX / imagen.width))
        imagen = imagen.resize((FOTO_ANCHO_MAX, alto_nuevo), Image.LANCZOS)

    buffer = io.BytesIO()
    imagen.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def generar_qr_png(texto: str) -> bytes:
    """Genera un código QR (PNG) con el texto dado — se usa para codificar el
    SKU en la etiqueta imprimible de cada producto."""
    imagen = qrcode.make(texto, box_size=8, border=2)
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    return buffer.getvalue()


@app.get("/")
def pagina_principal(
    request: Request, error: str | None = None, identidad: Identidad = Depends(requiere_login)
):
    """Muestra la tabla de productos con su stock por sucursal (solo admin;
    una vendedora entra directo a Ventas, que es lo único que puede usar)."""
    if identidad.rol != "admin":
        return RedirectResponse("/ventas", status_code=303)

    with engine.connect() as conn:
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()

        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo, "
            "       (foto IS NOT NULL) AS tiene_foto "
            "FROM productos WHERE activo = TRUE ORDER BY sku"
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
            "productos_json": productos_a_json(productos),
            "filas": filas,
            "movimientos": movimientos,
            "error": error,
        },
    )


@app.get("/inventario/exportar.csv", dependencies=[Depends(requiere_admin)])
def exportar_inventario():
    """Descarga el catálogo activo con su stock por sucursal en un CSV, para
    el contador o para análisis fuera del sistema."""
    with engine.connect() as conn:
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo "
            "FROM productos WHERE activo = TRUE ORDER BY sku"
        )).mappings().all()
        stock_rows = conn.execute(text(
            "SELECT producto_id, sucursal_id, cantidad FROM stock"
        )).all()

    stock_map = {(r.producto_id, r.sucursal_id): r.cantidad for r in stock_rows}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["SKU", "Titulo", "Categoria", "Precio menudeo", "Precio mayoreo", "Costo"]
        + [s["nombre"] for s in sucursales]
    )
    for p in productos:
        writer.writerow([
            p["sku"], p["titulo"], p["categoria"] or "",
            p["precio"] if p["precio"] is not None else "",
            p["precio_mayoreo"] if p["precio_mayoreo"] is not None else "",
            p["costo"] if p["costo"] is not None else "",
        ] + [stock_map.get((p["id"], s["id"]), 0) for s in sucursales])

    # BOM al inicio para que Excel en Windows detecte UTF-8 y no arruine acentos.
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=inventario.csv"},
    )


@app.post("/movimientos", dependencies=[Depends(requiere_admin)])
def registrar_movimiento(
    background_tasks: BackgroundTasks,
    producto_id: int = Form(...),
    tipo: str = Form(...),
    sucursal_id: list[int] = Form(...),
    cantidad: list[str] = Form(...),
    motivo: str = Form(""),
):
    """Registra un movimiento en una o varias sucursales a la vez (ej. llegan
    6 piezas y se reparten 3 a Tecamachalco y 3 a Prado Norte en un solo
    envío). Un renglón de sucursal con cantidad vacía significa "no tocar
    esa sucursal"."""
    tipo = tipo.strip()
    motivo = motivo.strip() or None

    if tipo not in ("entrada", "ajuste"):
        return RedirectResponse("/?error=Tipo de movimiento inválido.", status_code=303)

    if len(sucursal_id) != len(cantidad):
        return RedirectResponse("/?error=Datos de sucursales inconsistentes.", status_code=303)

    # Valida TODOS los renglones (solo lecturas) antes de escribir nada.
    renglones = []
    for sid, cant_str in zip(sucursal_id, cantidad):
        cant_str = cant_str.strip()
        if not cant_str:
            continue  # sucursal sin cantidad = no se toca
        try:
            cant_num = int(cant_str)
        except ValueError:
            return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)
        if tipo == "entrada" and cant_num <= 0:
            return RedirectResponse("/?error=La cantidad debe ser mayor a 0.", status_code=303)
        if tipo == "ajuste" and cant_num < 0:
            return RedirectResponse("/?error=La cantidad no puede ser negativa.", status_code=303)
        renglones.append((sid, cant_num))

    if not renglones:
        return RedirectResponse("/?error=Escribe la cantidad de al menos una sucursal.", status_code=303)

    with engine.begin() as conn:
        for sid, cant_num in renglones:
            # Stock actual del producto en esa sucursal (0 si no existía el renglón).
            actual = conn.execute(text(
                "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
            ), {"p": producto_id, "s": sid}).scalar()
            actual = actual if actual is not None else 0

            # Calcula el nuevo stock y el "delta" (cuánto cambió) según el tipo.
            if tipo == "entrada":
                nuevo = actual + cant_num
                delta = cant_num
            else:  # ajuste: el stock queda exactamente en la cantidad indicada
                nuevo = cant_num
                delta = cant_num - actual

            # Actualiza (o crea) el renglón de stock.
            conn.execute(text(
                "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
                "ON CONFLICT (producto_id, sucursal_id) "
                "DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
            ), {"p": producto_id, "s": sid, "c": nuevo})

            # Guarda el movimiento en el historial.
            conn.execute(text(
                "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                "VALUES (:p, :s, :tipo, :delta, :motivo)"
            ), {"p": producto_id, "s": sid, "tipo": tipo, "delta": delta, "motivo": motivo})

    background_tasks.add_task(empujar_stock_producto_seguro, producto_id)
    return RedirectResponse("/", status_code=303)


@app.post("/movimientos/traspaso", dependencies=[Depends(requiere_admin)])
def registrar_traspaso(
    background_tasks: BackgroundTasks,
    producto_id: int = Form(...),
    sucursal_origen_id: int = Form(...),
    sucursal_destino_id: int = Form(...),
    cantidad: str = Form(...),
    motivo: str = Form(""),
):
    """Mueve piezas de una sucursal a otra en una sola operación atómica (resta
    en origen + suma en destino), en vez de hacer un ajuste y una entrada por
    separado y arriesgarse a que queden desincronizados.
    """
    motivo_usuario = motivo.strip()

    if sucursal_origen_id == sucursal_destino_id:
        return RedirectResponse("/?error=La sucursal de origen y destino deben ser distintas.", status_code=303)

    try:
        cantidad_num = int(cantidad.strip())
    except ValueError:
        return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)
    if cantidad_num <= 0:
        return RedirectResponse("/?error=La cantidad debe ser mayor a 0.", status_code=303)

    with engine.begin() as conn:
        nombres = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE id IN (:o, :d)"
        ), {"o": sucursal_origen_id, "d": sucursal_destino_id}).mappings().all()
        nombre_por_id = {n["id"]: n["nombre"] for n in nombres}
        if sucursal_origen_id not in nombre_por_id or sucursal_destino_id not in nombre_por_id:
            return RedirectResponse("/?error=Sucursal inválida.", status_code=303)

        actual_origen = conn.execute(text(
            "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
        ), {"p": producto_id, "s": sucursal_origen_id}).scalar()
        actual_origen = actual_origen if actual_origen is not None else 0

        if cantidad_num > actual_origen:
            return RedirectResponse(
                f"/?error=No hay suficiente stock en {nombre_por_id[sucursal_origen_id]} "
                f"(hay {actual_origen}, se pidieron {cantidad_num}).",
                status_code=303,
            )

        actual_destino = conn.execute(text(
            "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
        ), {"p": producto_id, "s": sucursal_destino_id}).scalar()
        actual_destino = actual_destino if actual_destino is not None else 0

        # Resta en origen.
        conn.execute(text(
            "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
            "ON CONFLICT (producto_id, sucursal_id) DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
        ), {"p": producto_id, "s": sucursal_origen_id, "c": actual_origen - cantidad_num})

        # Suma en destino.
        conn.execute(text(
            "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
            "ON CONFLICT (producto_id, sucursal_id) DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
        ), {"p": producto_id, "s": sucursal_destino_id, "c": actual_destino + cantidad_num})

        motivo_origen = f"Traspaso a {nombre_por_id[sucursal_destino_id]}"
        motivo_destino = f"Traspaso desde {nombre_por_id[sucursal_origen_id]}"
        if motivo_usuario:
            motivo_origen += f" ({motivo_usuario})"
            motivo_destino += f" ({motivo_usuario})"

        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, 'traspaso', :delta, :motivo)"
        ), {"p": producto_id, "s": sucursal_origen_id, "delta": -cantidad_num, "motivo": motivo_origen})

        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, 'traspaso', :delta, :motivo)"
        ), {"p": producto_id, "s": sucursal_destino_id, "delta": cantidad_num, "motivo": motivo_destino})

    background_tasks.add_task(empujar_stock_producto_seguro, producto_id)
    return RedirectResponse("/", status_code=303)


@app.post("/ventas")
def registrar_venta(
    background_tasks: BackgroundTasks,
    producto_id: int = Form(...),
    sucursal_id: int = Form(...),
    canal: str = Form(...),
    tipo_precio: str = Form("menudeo"),
    cantidad: str = Form(...),
    precio: str = Form(""),
    descuento: str = Form(""),   # opcional: % de descuento sobre el precio ya resuelto
    cliente_id: str = Form(""),  # opcional: puede venir vacío ("Sin clienta")
    vendedora_id: str = Form(""),  # opcional: puede venir vacío ("Sin vendedora")
    monto_pagado: str = Form(""),   # opcional: vacío = se asume que pagó todo
    metodo_pago: str = Form("efectivo"),
    apartado: bool = Form(False),  # ej. la prenda ya salió pero no se ha pagado del todo
    sin_pago: bool = Form(False),  # marca explícita: no pagó nada, no depende de dejar el monto vacío
    identidad: Identidad = Depends(requiere_login),
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

    if canal not in CANALES:
        return RedirectResponse("/ventas?error=Canal de venta inválido.", status_code=303)

    if tipo_precio not in ("menudeo", "mayoreo"):
        return RedirectResponse("/ventas?error=Tipo de precio inválido.", status_code=303)

    precio_indicado = parsear_dinero(precio)

    # El descuento es un % opcional entre 0 y 100.
    descuento_pct = parsear_dinero(descuento)
    if descuento_pct is not None and not (0 <= descuento_pct <= 100):
        return RedirectResponse("/ventas?error=El descuento debe ser un % entre 0 y 100.", status_code=303)

    # Convierte los selects opcionales a número o None (vacío = "Sin ...").
    cliente_id_num = int(cliente_id) if cliente_id.strip() else None
    # Una vendedora no puede atribuirle la venta a nadie más que a sí misma:
    # ignora lo que venga del formulario y usa su propia identidad autenticada.
    if identidad.rol == "vendedora":
        vendedora_id_num = identidad.vendedora_id
    else:
        vendedora_id_num = int(vendedora_id) if vendedora_id.strip() else None

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
        # "Sin pago" manda sobre cualquier otra cosa: no hay que recordar
        # escribir "0" ni dejar el campo vacío del modo correcto. Si no se
        # marcó, vacío se asume como que pagó todo (el caso más común) —
        # EXCEPTO si es un apartado, donde vacío significa sin anticipo.
        total_venta = float(precio_unitario) * cantidad_num
        if sin_pago:
            monto_pagado_num = 0.0
        else:
            monto_pagado_num = parsear_dinero(monto_pagado)
            if monto_pagado_num is None:
                monto_pagado_num = 0.0 if apartado else total_venta

        if monto_pagado_num < 0:
            return RedirectResponse("/ventas?error=El monto pagado no puede ser negativo.", status_code=303)

        # Si dio más de lo que cuesta (ej. pagó $1700 en efectivo de una
        # venta de $1650), lo que se registra como pagado es el total —no
        # más— y la diferencia es cambio que se le regresa, no un saldo a
        # favor ni dinero que de verdad haya entrado a la caja.
        cambio = round(max(0.0, monto_pagado_num - total_venta), 2)
        monto_pagado_num = min(monto_pagado_num, total_venta)

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
        # pedido_id propio: una venta individual es un ticket de una sola línea.
        numero_nota = siguiente_numero_nota(conn, sucursal_id)
        venta_id = conn.execute(text(
            "INSERT INTO ventas "
            "(producto_id, sucursal_id, canal, tipo_precio, cantidad, precio_unitario, "
            " costo_unitario, cliente_id, vendedora_id, descuento_pct, pedido_id, numero_nota, apartado) "
            "VALUES (:p, :s, :canal, :tipo_precio, :cant, :precio, :costo, :cliente, :vendedora, :descuento, :pedido, :numero, :apartado) "
            "RETURNING id"
        ), {
            "p": producto_id, "s": sucursal_id, "canal": canal, "tipo_precio": tipo_precio,
            "cant": cantidad_num, "precio": precio_unitario, "costo": costo_unitario,
            "cliente": cliente_id_num, "vendedora": vendedora_id_num, "descuento": descuento_pct,
            "pedido": str(uuid.uuid4()), "numero": numero_nota, "apartado": apartado,
        }).scalar_one()

        # 4) Registra el pago inicial (ya validado arriba). Si el monto es 0,
        #    la venta queda "pendiente" (apartado sin anticipo).
        if monto_pagado_num > 0:
            conn.execute(text(
                "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
            ), {"v": venta_id, "metodo": metodo_pago, "monto": monto_pagado_num})

    background_tasks.add_task(empujar_stock_producto_seguro, producto_id)
    # Lleva directo a la nota de pedido imprimible, igual que ya hacía la
    # venta múltiple — no hay que ir a /ventas a marcarla y darle imprimir.
    query = f"id={venta_id}"
    if cambio > 0:
        query += f"&cambio={cambio:.2f}"
    return RedirectResponse(f"/ventas/nota?{query}", status_code=303)


@app.get("/ventas")
def ver_ventas(
    request: Request,
    error: str | None = None,
    cambio: str | None = None,
    ok: str | None = None,
    identidad: Identidad = Depends(requiere_login),
):
    """Punto de venta: registrar una venta y ver el estado de pago.
    Una vendedora solo ve SUS PROPIAS ventas, no las de las demás ni el
    total del negocio; la admin las ve todas."""
    filtro_vendedora = "AND v.vendedora_id = :mi_vendedora_id" if identidad.rol == "vendedora" else ""
    params: dict = {"mi_vendedora_id": identidad.vendedora_id} if identidad.rol == "vendedora" else {}

    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, precio, precio_mayoreo FROM productos "
            "WHERE activo = TRUE ORDER BY titulo"
        )).mappings().all()

        stock_por_producto = obtener_stock_por_producto(conn)

        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()

        clientas = conn.execute(text(
            "SELECT id, nombre FROM clientas ORDER BY nombre"
        )).mappings().all()

        vendedoras = conn.execute(text(
            "SELECT id, nombre FROM vendedoras WHERE activa = TRUE ORDER BY nombre"
        )).mappings().all()

        ventas_rows = conn.execute(text(
            "SELECT v.id, v.creada_en, p.sku, p.titulo, s.nombre AS sucursal, v.canal, "
            "       v.tipo_precio, c.nombre AS clienta, ve.nombre AS vendedora, "
            "       v.cantidad, v.precio_unitario, "
            "       (v.precio_unitario * v.cantidad) AS total, d.tipo AS devolucion_tipo, v.apartado "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "LEFT JOIN vendedoras ve ON ve.id = v.vendedora_id "
            "LEFT JOIN devoluciones d ON d.venta_id = v.id "
            f"WHERE 1=1 {filtro_vendedora} "
            "ORDER BY v.creada_en DESC LIMIT 200"
        ), params).mappings().all()

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
        elif v["apartado"]:
            # Distinto de "Parcial"/"Pendiente": además de no estar pagada,
            # no cuenta todavía en /reportes ni en el CSV para el contador.
            estado = "Apartado"
        elif pagado > 0:
            estado = "Parcial"
        else:
            estado = "Pendiente"

        etiquetas_metodo = {"efectivo": "Efectivo", "tarjeta": "Tarjeta", "transferencia": "Transferencia"}
        metodos = ", ".join(etiquetas_metodo.get(pg["metodo"], pg["metodo"]) for pg in pagos_venta) or "—"

        ventas.append({
            "id": v["id"],
            "creada_en": v["creada_en"].astimezone(ZONA_CDMX),
            "sku": v["sku"],
            "titulo": v["titulo"],
            "sucursal": v["sucursal"],
            "canal": v["canal"],
            "tipo_precio": v["tipo_precio"],
            "clienta": v["clienta"],
            "vendedora": v["vendedora"],
            "cantidad": v["cantidad"],
            "total": total,
            "pagado": pagado,
            "saldo": saldo,
            "estado": estado,
            "apartado": v["apartado"],
            "metodos": metodos,
            "devolucion_tipo": v["devolucion_tipo"],
        })

    return templates.TemplateResponse(request, "ventas.html", {
        "ventas": ventas, "error": error, "cambio": cambio, "ok": ok,
        "productos": productos, "productos_json": productos_a_json(productos, stock_por_producto),
        "sucursales": sucursales, "clientas": clientas, "vendedoras": vendedoras,
        "identidad": identidad,
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

    # Manda directo al comprobante imprimible del abono, en vez de solo
    # regresar a la lista.
    return RedirectResponse(f"/ventas/nota?id={venta_id}", status_code=303)


@app.post("/ventas/{venta_id}/apartado", dependencies=[Depends(requiere_admin)])
def marcar_apartado(venta_id: int, valor: bool = Form(...)):
    """Marca o desmarca una venta YA registrada como apartado (ej. una que se
    dio de alta antes de que existiera esta opción). Mientras le quede saldo
    pendiente, deja de contar en /reportes y en el CSV para el contador."""
    with engine.begin() as conn:
        actualizada = conn.execute(text(
            "UPDATE ventas SET apartado = :valor WHERE id = :id RETURNING id"
        ), {"valor": valor, "id": venta_id}).scalar()

    if actualizada is None:
        return RedirectResponse("/ventas?error=Venta no encontrada.", status_code=303)
    return RedirectResponse("/ventas", status_code=303)


@app.post("/ventas/{venta_id}/eliminar", dependencies=[Depends(requiere_admin)])
def eliminar_venta(venta_id: int, background_tasks: BackgroundTasks):
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

        # Si ya se registró como devolución/cambio, el stock ya se repuso por
        # ese camino — eliminarla también volvería a sumarlo por duplicado.
        ya_devuelta = conn.execute(text(
            "SELECT 1 FROM devoluciones WHERE venta_id = :id"
        ), {"id": venta_id}).scalar()
        if ya_devuelta:
            return RedirectResponse(
                "/ventas?error=Esta venta ya se registró como devolución/cambio; no se puede eliminar aparte.",
                status_code=303,
            )

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

    background_tasks.add_task(empujar_stock_producto_seguro, venta["producto_id"])
    return RedirectResponse("/ventas", status_code=303)


@app.post("/ventas/eliminar-varias", dependencies=[Depends(requiere_admin)])
def eliminar_ventas_varias(background_tasks: BackgroundTasks, id: list[int] = Form(...)):
    """Elimina varias ventas marcadas de un jalón (ej. limpiar varias de
    prueba a la vez), en vez de una por una. Misma lógica que eliminar_venta
    para cada una: repone el stock, deja un movimiento de ajuste, y se salta
    (sin tronar) las que ya se registraron como devolución/cambio."""
    eliminadas = 0
    saltadas = 0
    productos_tocados: set[int] = set()

    with engine.begin() as conn:
        for venta_id in id:
            venta = conn.execute(text(
                "SELECT producto_id, sucursal_id, cantidad FROM ventas WHERE id = :id"
            ), {"id": venta_id}).mappings().one_or_none()
            if venta is None:
                continue

            ya_devuelta = conn.execute(text(
                "SELECT 1 FROM devoluciones WHERE venta_id = :id"
            ), {"id": venta_id}).scalar()
            if ya_devuelta:
                saltadas += 1
                continue

            conn.execute(text(
                "UPDATE stock SET cantidad = cantidad + :c, actualizado_en = NOW() "
                "WHERE producto_id = :p AND sucursal_id = :s"
            ), {"c": venta["cantidad"], "p": venta["producto_id"], "s": venta["sucursal_id"]})

            conn.execute(text(
                "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                "VALUES (:p, :s, 'ajuste', :delta, :motivo)"
            ), {
                "p": venta["producto_id"], "s": venta["sucursal_id"],
                "delta": venta["cantidad"], "motivo": f"venta #{venta_id} eliminada (stock repuesto)",
            })

            conn.execute(text("DELETE FROM ventas WHERE id = :id"), {"id": venta_id})
            productos_tocados.add(venta["producto_id"])
            eliminadas += 1

    if productos_tocados:
        background_tasks.add_task(empujar_stock_productos_seguro, list(productos_tocados))

    mensaje = f"Se eliminaron {eliminadas} venta(s)."
    if saltadas:
        mensaje += f" {saltadas} no se pudieron eliminar (ya son devolución/cambio)."
    return RedirectResponse(f"/ventas?ok={mensaje}", status_code=303)


@app.post("/ventas/{venta_id}/devolucion", dependencies=[Depends(requiere_admin)])
def registrar_devolucion(
    venta_id: int,
    background_tasks: BackgroundTasks,
    tipo: str = Form(...),
    motivo: str = Form(""),
):
    """Registra una devolución o cambio real de una clienta.

    A diferencia de "Eliminar venta" (pensada para corregir errores de
    captura), esto NO borra la venta: queda en el historial de que sí se
    vendió, y se repone el stock y se deja un registro aparte en
    `devoluciones` de que la prenda regresó. Solo se puede devolver una vez
    la misma venta.
    """
    tipo = tipo.strip()
    motivo_usuario = motivo.strip()

    if tipo not in ("devolucion", "cambio"):
        return RedirectResponse("/ventas?error=Tipo de devolución inválido.", status_code=303)

    with engine.begin() as conn:
        venta = conn.execute(text(
            "SELECT producto_id, sucursal_id, cantidad FROM ventas WHERE id = :id"
        ), {"id": venta_id}).mappings().one_or_none()

        if venta is None:
            return RedirectResponse("/ventas?error=Venta no encontrada.", status_code=303)

        ya_devuelta = conn.execute(text(
            "SELECT 1 FROM devoluciones WHERE venta_id = :id"
        ), {"id": venta_id}).scalar()
        if ya_devuelta:
            return RedirectResponse(
                "/ventas?error=Esta venta ya se había registrado como devolución/cambio.",
                status_code=303,
            )

        # Repone el stock: la prenda física regresó a la sucursal de origen.
        conn.execute(text(
            "UPDATE stock SET cantidad = cantidad + :c, actualizado_en = NOW() "
            "WHERE producto_id = :p AND sucursal_id = :s"
        ), {"c": venta["cantidad"], "p": venta["producto_id"], "s": venta["sucursal_id"]})

        etiqueta = "Devolución" if tipo == "devolucion" else "Cambio"
        motivo_movimiento = f"{etiqueta} de venta #{venta_id}"
        if motivo_usuario:
            motivo_movimiento += f" ({motivo_usuario})"

        conn.execute(text(
            "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
            "VALUES (:p, :s, 'ajuste', :delta, :motivo)"
        ), {
            "p": venta["producto_id"], "s": venta["sucursal_id"],
            "delta": venta["cantidad"], "motivo": motivo_movimiento,
        })

        conn.execute(text(
            "INSERT INTO devoluciones (venta_id, tipo, motivo) VALUES (:v, :tipo, :motivo)"
        ), {"v": venta_id, "tipo": tipo, "motivo": motivo_usuario or None})

    background_tasks.add_task(empujar_stock_producto_seguro, venta["producto_id"])
    return RedirectResponse("/ventas", status_code=303)


@app.get("/ventas/nota")
def nota_pedido(
    request: Request,
    id: list[int] = Query(default=[]),
    copias: int = Query(default=1),
    cambio: float = Query(default=0.0),
    identidad: Identidad = Depends(requiere_login),
):
    """Genera una nota de pedido imprimible con varias ventas juntas (ej. las
    10 prendas que se llevó una clienta), seleccionadas con checkboxes en /ventas.

    copias=2 repite la nota completa dos veces (ej. una para la clienta y otra
    para el archivo de la tienda), con un salto de página entre ambas al imprimir.
    """
    copias = 2 if copias == 2 else 1
    if not id:
        return RedirectResponse(
            "/ventas?error=Selecciona al menos una venta para imprimir la nota.",
            status_code=303,
        )

    ids_stmt = lambda sql: text(sql).bindparams(bindparam("ids", expanding=True))

    # Una vendedora solo puede imprimir notas de SUS PROPIAS ventas, aunque
    # arme la URL a mano con otros ids.
    filtro_vendedora = "AND v.vendedora_id = :mi_vendedora_id" if identidad.rol == "vendedora" else ""
    params = {"ids": id}
    if identidad.rol == "vendedora":
        params["mi_vendedora_id"] = identidad.vendedora_id

    with engine.connect() as conn:
        filas = conn.execute(ids_stmt(
            "SELECT v.id, p.titulo, p.sku, v.cantidad, v.precio_unitario, c.nombre AS clienta, "
            "       v.numero_nota, s.nombre AS sucursal, ve.nombre AS vendedora "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "LEFT JOIN vendedoras ve ON ve.id = v.vendedora_id "
            f"WHERE v.id IN :ids {filtro_vendedora} ORDER BY v.id"
        ), params).mappings().all()

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
    numeros_distintos = set()
    sucursales_distintas = set()
    vendedoras_distintas = set()
    for f in filas:
        subtotal = float(f["precio_unitario"]) * f["cantidad"]
        total += subtotal
        pagado_total += pagado_por_venta.get(f["id"], 0.0)
        clientas_distintas.add(f["clienta"])
        numeros_distintos.add(f["numero_nota"])
        sucursales_distintas.add(f["sucursal"])
        vendedoras_distintas.add(f["vendedora"])
        items.append({
            "titulo": f["titulo"], "sku": f["sku"],
            "cantidad": f["cantidad"], "precio_unitario": float(f["precio_unitario"]),
            "subtotal": subtotal,
        })

    # Si todas las ventas seleccionadas son de la misma clienta, se muestra su
    # nombre; si se mezclan clientas distintas (o ninguna), se deja en blanco.
    clienta_nombre = next(iter(clientas_distintas)) if len(clientas_distintas) == 1 else None

    # Igual con el folio: si se seleccionaron ventas de tickets distintos a
    # mano (ej. desde /ventas), no hay un solo número que aplique a todas.
    numero_nota = next(iter(numeros_distintos)) if len(numeros_distintos) == 1 else None

    # El folio se repite entre sucursales (cada una tiene su propio #1, #2...),
    # así que en la nota se muestra junto con la sede para no confundirlos.
    sucursal_nombre = next(iter(sucursales_distintas)) if len(sucursales_distintas) == 1 else None

    # Igual: si todas las ventas seleccionadas las atendió la misma
    # vendedora, se muestra su nombre; si se mezclan (o ninguna tiene), en blanco.
    vendedora_nombre = next(iter(vendedoras_distintas)) if len(vendedoras_distintas) == 1 else None

    saldo = round(total - pagado_total, 2)

    return templates.TemplateResponse(request, "nota_pedido.html", {
        "items": items,
        "total": total,
        "pagado": pagado_total,
        "saldo": saldo,
        # Documento distinto según si ya se cobró todo o no: un ticket de
        # venta (ya pagada) no debe verse como una nota pendiente de cobrar,
        # y viceversa — para que ni al mostrador ni a la clienta le quede
        # duda de si todavía se debe algo.
        "pagado_completo": saldo <= 0,
        "clienta": clienta_nombre,
        "vendedora": vendedora_nombre,
        "numero_nota": numero_nota,
        "sucursal": sucursal_nombre,
        "fecha": datetime.now(ZONA_CDMX),
        "copias": copias,
        "cambio": cambio,
    })


@app.get("/ventas/carrito")
def carrito_venta(request: Request, error: str | None = None, identidad: Identidad = Depends(requiere_login)):
    """Formulario para registrar varias ventas de una vez (ej. una clienta
    que se lleva 7 prendas), en lugar de repetir el formulario una por una.
    """
    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, precio, precio_mayoreo FROM productos "
            "WHERE activo = TRUE ORDER BY titulo"
        )).mappings().all()
        stock_por_producto = obtener_stock_por_producto(conn)
        sucursales = conn.execute(text(
            "SELECT id, nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
        clientas = conn.execute(text(
            "SELECT id, nombre FROM clientas ORDER BY nombre"
        )).mappings().all()
        vendedoras = conn.execute(text(
            "SELECT id, nombre FROM vendedoras WHERE activa = TRUE ORDER BY nombre"
        )).mappings().all()

    return templates.TemplateResponse(request, "carrito.html", {
        "sucursales": sucursales, "clientas": clientas, "vendedoras": vendedoras, "error": error,
        "productos_json": productos_a_json(productos, stock_por_producto), "identidad": identidad,
    })


@app.post("/ventas/carrito")
def registrar_carrito(
    background_tasks: BackgroundTasks,
    sucursal_id: int = Form(...),
    canal: str = Form(...),
    cliente_id: str = Form(""),
    vendedora_id: str = Form(""),
    monto_pagado: str = Form(""),
    metodo_pago: str = Form("efectivo"),
    apartado: bool = Form(False),  # ej. las prendas ya salieron pero no se han pagado del todo
    sin_pago: bool = Form(False),  # marca explícita: no pagó nada, no depende de dejar el monto vacío
    producto_id: list[int] = Form(...),
    tipo_precio: list[str] = Form(...),
    cantidad: list[str] = Form(...),
    precio: list[str] = Form(...),
    descuento: list[str] = Form(...),
    identidad: Identidad = Depends(requiere_login),
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
    if canal not in CANALES:
        return RedirectResponse("/ventas/carrito?error=Canal de venta inválido.", status_code=303)

    n = len(producto_id)
    if n == 0 or not (len(tipo_precio) == len(cantidad) == len(precio) == len(descuento) == n):
        return RedirectResponse("/ventas/carrito?error=Agrega al menos una prenda al carrito.", status_code=303)

    cliente_id_num = int(cliente_id) if cliente_id.strip() else None
    if identidad.rol == "vendedora":
        vendedora_id_num = identidad.vendedora_id
    else:
        vendedora_id_num = int(vendedora_id) if vendedora_id.strip() else None

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
        # Igual que en la venta individual: "sin pago" manda sobre todo lo
        # demás; si no se marcó, vacío = pagó todo, EXCEPTO si es un
        # apartado, donde vacío significa que no dejó nada de anticipo.
        total_pedido = sum(it["subtotal"] for it in items_resueltos)
        if sin_pago:
            monto_pagado_num = 0.0
        else:
            monto_pagado_num = parsear_dinero(monto_pagado)
            if monto_pagado_num is None:
                monto_pagado_num = 0.0 if apartado else total_pedido

        if monto_pagado_num < 0:
            return RedirectResponse("/ventas/carrito?error=El monto pagado no puede ser negativo.", status_code=303)

        # Igual que en la venta individual: si dio más de lo que cuesta el
        # pedido, lo pagado se topa en el total y la diferencia es cambio
        # a entregar, no un saldo a favor ni dinero que entró a la caja.
        cambio = round(max(0.0, monto_pagado_num - total_pedido), 2)
        monto_pagado_num = min(monto_pagado_num, total_pedido)

        # --- Paso 3: ya validado todo; ahora sí se descuenta stock y se crea cada venta. ---
        # Un solo pedido_id compartido: todas las prendas del carrito son
        # UN ticket (para el ticket promedio), no uno por prenda. Mismo folio
        # de nota para todas las líneas, por la misma razón.
        pedido_id = str(uuid.uuid4())
        numero_nota = siguiente_numero_nota(conn, sucursal_id)
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
                " costo_unitario, cliente_id, vendedora_id, descuento_pct, pedido_id, numero_nota, apartado) "
                "VALUES (:p, :s, :canal, :tipo_precio, :cant, :precio, :costo, :cliente, :vendedora, :descuento, :pedido, :numero, :apartado) "
                "RETURNING id"
            ), {
                "p": it["producto_id"], "s": sucursal_id, "canal": canal, "tipo_precio": it["tipo_precio"],
                "cant": it["cantidad"], "precio": it["precio_unitario"], "costo": it["costo_unitario"],
                "cliente": cliente_id_num, "vendedora": vendedora_id_num, "descuento": it["descuento_pct"],
                "pedido": pedido_id, "numero": numero_nota, "apartado": apartado,
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

    background_tasks.add_task(
        empujar_stock_productos_seguro, [it["producto_id"] for it in items_resueltos]
    )

    # Lleva directo a la nota de pedido imprimible con las ventas recién creadas.
    query = "&".join(f"id={vid}" for vid in venta_ids)
    if cambio > 0:
        query += f"&cambio={cambio:.2f}"
    return RedirectResponse(f"/ventas/nota?{query}", status_code=303)


@app.post("/productos", dependencies=[Depends(requiere_admin)])
def crear_producto(
    sku: str = Form(...),
    titulo: str = Form(...),
    categoria: str = Form(""),
    costo: str = Form(""),
    precio: str = Form(""),           # opcional: precio de venta menudeo
    precio_mayoreo: str = Form(""),   # opcional: precio de venta mayoreo
    utilidad_menudeo: str = Form(""),  # opcional: % utilidad -> calcula precio menudeo
    utilidad_mayoreo: str = Form(""),  # opcional: % utilidad -> calcula precio mayoreo
    sucursal_inicial_id: list[int] = Form(default=[]),  # opcional: dónde llegó la mercancía (una o varias)
    cantidad_inicial: list[str] = Form(default=[]),      # opcional: cuántas piezas llegaron a cada una
    foto: UploadFile | None = File(None),  # opcional: foto de la prenda
):
    """Da de alta un producto (momento de compra) y le crea stock en 0 en cada sucursal.

    Si se indica cantidad inicial en una o varias sucursales, además registra esa
    entrada de una vez por cada una (mismo efecto que ir después a "Registrar
    movimiento" → Entrada), permitiendo repartir una misma llegada de mercancía
    entre las sucursales (ej. 3 a Tecamachalco y 3 a Prado Norte) desde el alta.
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

    # En la tienda no se manejan centavos: redondea a pesos enteros, venga el
    # precio del % de utilidad de arriba o escrito directo a mano.
    if precio_valor is not None:
        precio_valor = round(precio_valor)
    if precio_mayoreo_valor is not None:
        precio_mayoreo_valor = round(precio_mayoreo_valor)

    # Cantidad inicial por sucursal (todas opcionales, se puede repartir entre
    # varias): se valida todo ANTES de escribir nada. Sucursal sin cantidad = 0.
    if len(sucursal_inicial_id) != len(cantidad_inicial):
        return RedirectResponse("/?error=Datos de sucursales inconsistentes.", status_code=303)

    stock_inicial = {}  # sucursal_id -> cantidad, solo las que vinieron con valor > 0
    for suc_id, cant_str in zip(sucursal_inicial_id, cantidad_inicial):
        cant_str = cant_str.strip()
        if not cant_str:
            continue
        try:
            cant_num = int(cant_str)
        except ValueError:
            return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)
        if cant_num < 0:
            return RedirectResponse("/?error=La cantidad no puede ser negativa.", status_code=303)
        if cant_num > 0:
            stock_inicial[suc_id] = cant_num

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
                # Nace con la cantidad repartida a esa sucursal (0 si no le tocó nada).
                conn.execute(text(
                    "INSERT INTO stock (producto_id, sucursal_id, cantidad) "
                    "VALUES (:producto_id, :sucursal_id, :cantidad)"
                ), {"producto_id": nuevo_id, "sucursal_id": suc_id, "cantidad": stock_inicial.get(suc_id, 0)})

            # Deja rastro en el historial de movimientos (igual que una entrada
            # normal), una por cada sucursal que recibió stock inicial.
            for suc_id, cant_num in stock_inicial.items():
                conn.execute(text(
                    "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                    "VALUES (:p, :s, 'entrada', :delta, 'alta inicial del producto')"
                ), {"p": nuevo_id, "s": suc_id, "delta": cant_num})

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


@app.get("/productos/{producto_id}/qr", dependencies=[Depends(requiere_admin)])
def qr_producto(producto_id: int):
    """Código QR con el SKU del producto, para escanearlo con el celular o un
    lector y encontrarlo rápido en el mostrador sin teclearlo."""
    with engine.connect() as conn:
        sku = conn.execute(text(
            "SELECT sku FROM productos WHERE id = :id"
        ), {"id": producto_id}).scalar()

    if sku is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")

    return Response(content=generar_qr_png(sku), media_type="image/png")


@app.get("/productos/etiquetas", dependencies=[Depends(requiere_admin)])
def etiquetas_productos(request: Request, id: list[int] = Query(default=[])):
    """Página imprimible con una etiqueta (SKU + QR + título) por cada
    producto seleccionado, para pegar en la prenda o en el precio.
    """
    if not id:
        return RedirectResponse(
            "/?error=Selecciona al menos un producto para imprimir etiquetas.",
            status_code=303,
        )

    ids_stmt = text(
        "SELECT id, sku, titulo FROM productos WHERE id IN :ids ORDER BY sku"
    ).bindparams(bindparam("ids", expanding=True))

    with engine.connect() as conn:
        productos = conn.execute(ids_stmt, {"ids": id}).mappings().all()

    if not productos:
        return RedirectResponse("/?error=No se encontraron los productos seleccionados.", status_code=303)

    return templates.TemplateResponse(request, "etiquetas.html", {"productos": productos})


@app.get("/productos/{producto_id}/historial", dependencies=[Depends(requiere_admin)])
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


@app.post("/productos/{producto_id}/eliminar", dependencies=[Depends(requiere_admin)])
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


@app.post("/productos/{producto_id}/reactivar", dependencies=[Depends(requiere_admin)])
def reactivar_producto(producto_id: int):
    """Deshace un 'eliminar': vuelve a marcar el producto como activo."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE productos SET activo = TRUE, actualizado_en = NOW() WHERE id = :id"
        ), {"id": producto_id})
    return RedirectResponse("/desactivados", status_code=303)


@app.get("/clientas", dependencies=[Depends(requiere_admin)])
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

        # Clientas con saldo pendiente: suma, por venta, lo que falta de pagar
        # (precio × cantidad menos lo ya abonado), y se queda solo con las que
        # todavía deben algo.
        con_saldo = conn.execute(text(
            "SELECT c.id, c.nombre, c.telefono, "
            "       SUM((v.precio_unitario * v.cantidad) - COALESCE(pv.pagado, 0)) AS saldo_pendiente "
            "FROM clientas c "
            "JOIN ventas v ON v.cliente_id = c.id "
            "LEFT JOIN (SELECT venta_id, SUM(monto) AS pagado FROM pagos GROUP BY venta_id) pv "
            "       ON pv.venta_id = v.id "
            "WHERE (v.precio_unitario * v.cantidad) > COALESCE(pv.pagado, 0) "
            "GROUP BY c.id, c.nombre, c.telefono "
            "ORDER BY saldo_pendiente DESC"
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
        "con_saldo": con_saldo,
        "dias_alerta": DIAS_SIN_COMPRAR_ALERTA,
        "error": error,
    })


@app.post("/clientas")
def crear_clienta(
    request: Request,
    nombre: str = Form(...),
    telefono: str = Form(""),
    email: str = Form(""),
    cumpleanos: str = Form(""),
    notas: str = Form(""),
    siguiente: str = Form("/clientas"),  # a dónde regresar (una vendedora no puede ver /clientas)
):
    """Da de alta una clienta nueva. Accesible tanto para admin (desde
    /clientas) como para vendedora (desde /ventas o /ventas/carrito, para
    poder ligar la venta a una clienta nueva sin ver el resto de clientas).

    El "+ Nueva clienta" de /ventas y /ventas/carrito la manda por fetch()
    (Accept: application/json) para agregarla al selector de Clienta sin
    recargar la página entera — si no, se pierde todo lo demás que ya se
    había llenado del formulario de venta. La pide así en vez de un
    redirect normal.
    """
    nombre = nombre.strip()
    # Lista blanca de a dónde se puede regresar, para no abrir un redirect
    # a cualquier URL que alguien mande en el formulario.
    if siguiente not in ("/clientas", "/ventas", "/ventas/carrito"):
        siguiente = "/clientas"

    with engine.begin() as conn:
        nueva_id = conn.execute(text(
            "INSERT INTO clientas (nombre, telefono, email, cumpleanos, notas) "
            "VALUES (:nombre, :telefono, :email, :cumpleanos, :notas) "
            "RETURNING id"
        ), {
            "nombre": nombre,
            "telefono": telefono.strip() or None,
            "email": email.strip() or None,
            "cumpleanos": cumpleanos.strip() or None,
            "notas": notas.strip() or None,
        }).scalar_one()

    if "application/json" in request.headers.get("accept", ""):
        return {"id": nueva_id, "nombre": nombre}
    return RedirectResponse(siguiente, status_code=303)


@app.post("/clientas/{cliente_id}/abonar", dependencies=[Depends(requiere_admin)])
def abonar_saldo_clienta(
    cliente_id: int,
    monto: str = Form(...),
    metodo: str = Form("efectivo"),
    siguiente: str = Form("/clientas"),  # a dónde regresar: la lista, o la ficha de la clienta
):
    """Aplica un pago de la clienta a su saldo pendiente TOTAL (todas sus
    notas juntas), repartido entre sus ventas con saldo (la más vieja
    primero) hasta agotarlo. Para abonar solo UNA nota/ocasión en vez de
    toda la cuenta, ver abonar_nota."""
    if siguiente not in ("/clientas", f"/clientas/{cliente_id}"):
        siguiente = "/clientas"

    metodo = metodo.strip()
    if metodo not in ("efectivo", "tarjeta", "transferencia"):
        return RedirectResponse(f"{siguiente}?error=Método de pago inválido.", status_code=303)

    monto_num = parsear_dinero(monto)
    if monto_num is None or monto_num <= 0:
        return RedirectResponse(f"{siguiente}?error=El monto del abono debe ser mayor a 0.", status_code=303)

    with engine.begin() as conn:
        ventas_deuda = conn.execute(text(
            "SELECT v.id, (v.precio_unitario * v.cantidad) - COALESCE(pv.pagado, 0) AS saldo "
            "FROM ventas v "
            "LEFT JOIN (SELECT venta_id, SUM(monto) AS pagado FROM pagos GROUP BY venta_id) pv "
            "       ON pv.venta_id = v.id "
            "WHERE v.cliente_id = :id "
            "  AND (v.precio_unitario * v.cantidad) > COALESCE(pv.pagado, 0) "
            "ORDER BY v.creada_en ASC"
        ), {"id": cliente_id}).mappings().all()

        saldo_total = round(sum(float(v["saldo"]) for v in ventas_deuda), 2)
        if saldo_total <= 0:
            return RedirectResponse(f"{siguiente}?error=Esta clienta no tiene saldo pendiente.", status_code=303)
        if monto_num > saldo_total:
            return RedirectResponse(
                f"{siguiente}?error=El abono (${monto_num:.2f}) es mayor a la deuda total (${saldo_total:.2f}).",
                status_code=303,
            )

        restante = monto_num
        ventas_tocadas = []
        for v in ventas_deuda:
            if restante <= 0:
                break
            pago_este = min(restante, float(v["saldo"]))
            conn.execute(text(
                "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
            ), {"v": v["id"], "metodo": metodo, "monto": round(pago_este, 2)})
            ventas_tocadas.append(v["id"])
            restante -= pago_este

    # Manda directo al comprobante imprimible de lo que se acaba de abonar
    # (las ventas que de verdad recibieron parte del pago, sin importar a
    # cuántas notas distintas pertenecían), en vez de solo regresar.
    query = "&".join(f"id={vid}" for vid in ventas_tocadas)
    return RedirectResponse(f"/ventas/nota?{query}", status_code=303)


@app.post("/clientas/{cliente_id}/notas/abonar", dependencies=[Depends(requiere_admin)])
def abonar_nota(
    cliente_id: int,
    id: list[int] = Form(...),  # las ventas de ESA nota/ocasión nada más
    monto: str = Form(...),
    metodo: str = Form("efectivo"),
):
    """Aplica un pago a UNA sola nota (una ocasión/visita de compra), no a
    toda la cuenta de la clienta — repartido entre las ventas de esa nota,
    la más vieja primero, hasta agotarlo. Ver abonar_saldo_clienta para
    abonar a la cuenta completa en vez de a una nota en particular."""
    metodo = metodo.strip()
    if metodo not in ("efectivo", "tarjeta", "transferencia"):
        return RedirectResponse(f"/clientas/{cliente_id}?error=Método de pago inválido.", status_code=303)

    monto_num = parsear_dinero(monto)
    if monto_num is None or monto_num <= 0:
        return RedirectResponse(f"/clientas/{cliente_id}?error=El monto del abono debe ser mayor a 0.", status_code=303)

    ids_stmt = lambda sql: text(sql).bindparams(bindparam("ids", expanding=True))
    with engine.begin() as conn:
        ventas_deuda = conn.execute(ids_stmt(
            "SELECT v.id, (v.precio_unitario * v.cantidad) - COALESCE(pv.pagado, 0) AS saldo "
            "FROM ventas v "
            "LEFT JOIN (SELECT venta_id, SUM(monto) AS pagado FROM pagos GROUP BY venta_id) pv "
            "       ON pv.venta_id = v.id "
            "WHERE v.id IN :ids AND v.cliente_id = :cliente_id "
            "  AND (v.precio_unitario * v.cantidad) > COALESCE(pv.pagado, 0) "
            "ORDER BY v.id ASC"
        ), {"ids": id, "cliente_id": cliente_id}).mappings().all()

        saldo_nota = round(sum(float(v["saldo"]) for v in ventas_deuda), 2)
        if saldo_nota <= 0:
            return RedirectResponse(f"/clientas/{cliente_id}?error=Esta nota no tiene saldo pendiente.", status_code=303)
        if monto_num > saldo_nota:
            return RedirectResponse(
                f"/clientas/{cliente_id}?error=El abono (${monto_num:.2f}) es mayor al saldo de esta nota (${saldo_nota:.2f}).",
                status_code=303,
            )

        restante = monto_num
        for v in ventas_deuda:
            if restante <= 0:
                break
            pago_este = min(restante, float(v["saldo"]))
            conn.execute(text(
                "INSERT INTO pagos (venta_id, metodo, monto) VALUES (:v, :metodo, :monto)"
            ), {"v": v["id"], "metodo": metodo, "monto": round(pago_este, 2)})
            restante -= pago_este

    # Manda directo al comprobante imprimible de esta nota completa (todas
    # sus prendas, no solo las que tocó este abono en particular).
    query = "&".join(f"id={vid}" for vid in id)
    return RedirectResponse(f"/ventas/nota?{query}", status_code=303)


@app.post("/clientas/{cliente_id}/eliminar", dependencies=[Depends(requiere_admin)])
def eliminar_clienta(cliente_id: int):
    """Borra una clienta (ej. un registro duplicado o de prueba).

    Sus ventas pasadas NO se borran ni se pierden: solo quedan sin clienta
    asociada (ventas.cliente_id se pone en NULL, por el ON DELETE SET NULL
    de la base de datos), igual que una venta sin clienta desde el inicio.
    """
    with engine.begin() as conn:
        borrada = conn.execute(text(
            "DELETE FROM clientas WHERE id = :id RETURNING id"
        ), {"id": cliente_id}).scalar()

    if borrada is None:
        return RedirectResponse("/clientas?error=Clienta no encontrada.", status_code=303)
    return RedirectResponse("/clientas", status_code=303)


@app.get("/clientas/{cliente_id}", dependencies=[Depends(requiere_admin)])
def ver_clienta(request: Request, cliente_id: int, error: str | None = None):
    """Ficha de una clienta: sus datos y su historial de compras, agrupado
    POR NOTA (cada visita/ocasión de compra por separado), no todo junto.
    Cada nota trae su propio total/pagado/saldo y se puede abonar o
    imprimir por separado, además del abono general a toda su cuenta."""
    with engine.connect() as conn:
        clienta = conn.execute(text(
            "SELECT id, nombre, telefono, email, cumpleanos, notas FROM clientas WHERE id = :id"
        ), {"id": cliente_id}).mappings().one_or_none()

        if clienta is None:
            return RedirectResponse("/clientas?error=Clienta no encontrada.", status_code=303)

        compras_rows = conn.execute(text(
            "SELECT v.id, v.creada_en, v.pedido_id, v.numero_nota, p.titulo, p.sku, "
            "       v.cantidad, v.precio_unitario, s.nombre AS sucursal, "
            "       v.canal, v.tipo_precio, v.descuento_pct, "
            "       COALESCE((SELECT SUM(monto) FROM pagos WHERE venta_id = v.id), 0) AS pagado_venta "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "WHERE v.cliente_id = :id ORDER BY v.creada_en DESC, v.id DESC"
        ), {"id": cliente_id}).mappings().all()

    # Agrupa por "ticket" (pedido_id si vino de un carrito con varias
    # prendas; si no, su propio id — igual que el ticket promedio en
    # /reportes). El orden de aparición de las llaves sigue el de las filas
    # (ya vienen de más reciente a más vieja).
    notas_por_clave: dict[str, dict] = {}
    orden_claves: list[str] = []
    for c in compras_rows:
        clave = c["pedido_id"] or f"v{c['id']}"
        if clave not in notas_por_clave:
            notas_por_clave[clave] = {
                "clave": clave,
                "venta_ids": [],
                # Nombrado "prendas", no "items": un dict de Python ya trae un
                # método .items() propio, y en Jinja "n.items" con notación de
                # punto resuelve a ESE método antes que a una llave "items" —
                # necesitaría "n['items']" para no chocar. Más simple usar
                # otro nombre y ya.
                "prendas": [],
                "numero_nota": c["numero_nota"],
                "sucursal": c["sucursal"],
                "fecha": c["creada_en"].astimezone(ZONA_CDMX),
                "total": 0.0,
                "pagado": 0.0,
            }
            orden_claves.append(clave)
        nota = notas_por_clave[clave]
        subtotal = float(c["precio_unitario"]) * c["cantidad"]
        nota["venta_ids"].append(c["id"])
        nota["prendas"].append({
            "titulo": c["titulo"], "sku": c["sku"], "cantidad": c["cantidad"],
            "precio_unitario": float(c["precio_unitario"]), "subtotal": subtotal,
        })
        nota["total"] += subtotal
        nota["pagado"] += float(c["pagado_venta"])

    notas = []
    for clave in orden_claves:
        nota = notas_por_clave[clave]
        nota["saldo"] = round(nota["total"] - nota["pagado"], 2)
        notas.append(nota)

    saldo_total_clienta = round(sum(n["saldo"] for n in notas), 2)

    return templates.TemplateResponse(request, "clienta_detalle.html", {
        "clienta": clienta, "notas": notas, "saldo_total_clienta": saldo_total_clienta, "error": error,
    })


@app.get("/vendedoras", dependencies=[Depends(requiere_admin)])
def ver_vendedoras(request: Request, error: str | None = None):
    """Lista las vendedoras/empleadas y cuánto ha vendido cada una en total,
    para reportes de comisiones y desempeño."""
    with engine.connect() as conn:
        filas = conn.execute(text(
            "SELECT ve.id, ve.nombre, ve.activa, ve.usuario, "
            "       COUNT(v.id) AS num_ventas, "
            "       COALESCE(SUM(v.precio_unitario * v.cantidad), 0) AS total_vendido "
            "FROM vendedoras ve "
            "LEFT JOIN ventas v ON v.vendedora_id = ve.id "
            "GROUP BY ve.id, ve.nombre, ve.activa, ve.usuario "
            "ORDER BY ve.activa DESC, ve.nombre"
        )).mappings().all()

    return templates.TemplateResponse(request, "vendedoras.html", {"vendedoras": filas, "error": error})


@app.post("/vendedoras", dependencies=[Depends(requiere_admin)])
def crear_vendedora(
    nombre: str = Form(...),
    usuario: str = Form(""),
    clave: str = Form(""),
):
    """Da de alta una vendedora/empleada nueva. El usuario/contraseña son
    opcionales: sin ellos, la vendedora existe solo para atribuirle ventas
    desde la cuenta admin, pero no puede entrar al sistema ella misma."""
    nombre = nombre.strip()
    usuario = usuario.strip()
    if not nombre:
        return RedirectResponse("/vendedoras?error=El nombre no puede quedar vacío.", status_code=303)
    if usuario and not clave:
        return RedirectResponse(
            "/vendedoras?error=Si le das usuario, también necesita una contraseña.", status_code=303,
        )
    if usuario == os.getenv("APP_USUARIO", "admin"):
        return RedirectResponse("/vendedoras?error=Ese usuario ya lo usa la cuenta admin.", status_code=303)

    with engine.begin() as conn:
        ya_existe = conn.execute(text(
            "SELECT 1 FROM vendedoras WHERE nombre = :nombre"
        ), {"nombre": nombre}).scalar()
        if ya_existe:
            return RedirectResponse("/vendedoras?error=Ya existe una vendedora con ese nombre.", status_code=303)

        if usuario:
            usuario_ocupado = conn.execute(text(
                "SELECT 1 FROM vendedoras WHERE usuario = :usuario"
            ), {"usuario": usuario}).scalar()
            if usuario_ocupado:
                return RedirectResponse("/vendedoras?error=Ese usuario ya está en uso.", status_code=303)

        salt, hash_ = hash_clave(clave) if clave else (None, None)
        conn.execute(text(
            "INSERT INTO vendedoras (nombre, usuario, clave_hash, clave_salt) "
            "VALUES (:nombre, :usuario, :hash, :salt)"
        ), {"nombre": nombre, "usuario": usuario or None, "hash": hash_, "salt": salt})
    return RedirectResponse("/vendedoras", status_code=303)


@app.post("/vendedoras/{vendedora_id}/clave", dependencies=[Depends(requiere_admin)])
def cambiar_clave_vendedora(vendedora_id: int, usuario: str = Form(...), clave: str = Form(...)):
    """Da de alta o cambia el usuario/contraseña de acceso de una vendedora
    que ya existe (ej. olvidó su contraseña, o no tenía acceso todavía)."""
    usuario = usuario.strip()
    if not usuario or not clave:
        return RedirectResponse("/vendedoras?error=Usuario y contraseña son obligatorios.", status_code=303)
    if usuario == os.getenv("APP_USUARIO", "admin"):
        return RedirectResponse("/vendedoras?error=Ese usuario ya lo usa la cuenta admin.", status_code=303)

    with engine.begin() as conn:
        usuario_ocupado = conn.execute(text(
            "SELECT 1 FROM vendedoras WHERE usuario = :usuario AND id != :id"
        ), {"usuario": usuario, "id": vendedora_id}).scalar()
        if usuario_ocupado:
            return RedirectResponse("/vendedoras?error=Ese usuario ya está en uso.", status_code=303)

        salt, hash_ = hash_clave(clave)
        actualizada = conn.execute(text(
            "UPDATE vendedoras SET usuario = :usuario, clave_hash = :hash, clave_salt = :salt "
            "WHERE id = :id RETURNING id"
        ), {"usuario": usuario, "hash": hash_, "salt": salt, "id": vendedora_id}).scalar()

    if actualizada is None:
        return RedirectResponse("/vendedoras?error=Vendedora no encontrada.", status_code=303)
    return RedirectResponse("/vendedoras", status_code=303)


@app.post("/vendedoras/{vendedora_id}/activa", dependencies=[Depends(requiere_admin)])
def cambiar_activa_vendedora(vendedora_id: int, valor: bool = Form(...)):
    """Activa o desactiva una vendedora (ej. ya no trabaja ahí), sin borrar
    su historial de ventas pasadas ni las comisiones ya calculadas.
    Desactivada = tampoco puede entrar al sistema con su usuario/contraseña."""
    with engine.begin() as conn:
        actualizada = conn.execute(text(
            "UPDATE vendedoras SET activa = :valor WHERE id = :id RETURNING id"
        ), {"valor": valor, "id": vendedora_id}).scalar()

    if actualizada is None:
        return RedirectResponse("/vendedoras?error=Vendedora no encontrada.", status_code=303)
    return RedirectResponse("/vendedoras", status_code=303)


@app.post("/vendedoras/{vendedora_id}/eliminar", dependencies=[Depends(requiere_admin)])
def eliminar_vendedora(vendedora_id: int):
    """Borra una vendedora (ej. un registro duplicado o de prueba).

    Sus ventas pasadas NO se borran: solo quedan sin vendedora asociada
    (ON DELETE SET NULL), igual que con clientas. Para alguien que ya no
    trabaja ahí pero cuyo historial quieres conservar intacto, mejor
    desactivarla en vez de borrarla.
    """
    with engine.begin() as conn:
        borrada = conn.execute(text(
            "DELETE FROM vendedoras WHERE id = :id RETURNING id"
        ), {"id": vendedora_id}).scalar()

    if borrada is None:
        return RedirectResponse("/vendedoras?error=Vendedora no encontrada.", status_code=303)
    return RedirectResponse("/vendedoras", status_code=303)


@app.get("/desactivados", dependencies=[Depends(requiere_admin)])
def ver_desactivados(request: Request):
    """Lista los productos eliminados (inactivos), por si hay que reactivar alguno."""
    with engine.connect() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo, categoria FROM productos "
            "WHERE activo = FALSE ORDER BY titulo"
        )).mappings().all()

    return templates.TemplateResponse(request, "desactivados.html", {"productos": productos})


@app.get("/productos/{producto_id}/editar", dependencies=[Depends(requiere_admin)])
def editar_producto_form(request: Request, producto_id: int, error: str | None = None):
    """Muestra la pantalla para editar un producto (poner precio de venta, ajustar costo,
    y ajustar la cantidad de piezas en cada sucursal)."""
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT id, sku, titulo, categoria, precio, precio_mayoreo, costo, "
            "       (foto IS NOT NULL) AS tiene_foto, "
            "       (shopify_product_id IS NOT NULL) AS ligado_shopify "
            "FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

        if producto is None:
            return RedirectResponse("/?error=Producto no encontrado.", status_code=303)

        # Sucursales activas con la cantidad actual de este producto (0 si no
        # tiene renglón de stock todavía), para poder editarla aquí mismo.
        stock_por_sucursal = conn.execute(text(
            "SELECT s.id, s.nombre, COALESCE(st.cantidad, 0) AS cantidad "
            "FROM sucursales s "
            "LEFT JOIN stock st ON st.sucursal_id = s.id AND st.producto_id = :id "
            "WHERE s.activa = TRUE ORDER BY s.id"
        ), {"id": producto_id}).mappings().all()

    return templates.TemplateResponse(request, "editar.html", {
        "producto": producto,
        "stock_por_sucursal": stock_por_sucursal,
        "error": error,
    })


@app.post("/productos/{producto_id}/enviar-a-shopify", dependencies=[Depends(requiere_admin)])
def enviar_producto_a_shopify(producto_id: int):
    """Crea en Shopify (como borrador) un producto que se dio de alta en
    isha-wear-pos y todavía no existe allá — botón manual, a propósito NO
    automático, para no publicar productos a medias (sin foto o precio)."""
    try:
        crear_producto_en_shopify(producto_id)
    except FaltaPrecioError as error:
        return RedirectResponse(f"/productos/{producto_id}/editar?error={error}", status_code=303)
    except ProductoYaLigadoError:
        pass  # ya estaba ligado, no hay nada que hacer — no es un error real
    except Exception as error:
        return RedirectResponse(
            f"/productos/{producto_id}/editar?error=No se pudo enviar a Shopify: {error}",
            status_code=303,
        )

    return RedirectResponse(f"/productos/{producto_id}/editar", status_code=303)


@app.post("/productos/{producto_id}/editar", dependencies=[Depends(requiere_admin)])
def editar_producto(
    producto_id: int,
    background_tasks: BackgroundTasks,
    sku: str = Form(...),
    titulo: str = Form(...),
    categoria: str = Form(""),
    precio: str = Form(""),
    precio_mayoreo: str = Form(""),
    utilidad_menudeo: str = Form(""),  # opcional: % utilidad -> recalcula precio menudeo
    utilidad_mayoreo: str = Form(""),  # opcional: % utilidad -> recalcula precio mayoreo
    costo: str = Form(""),
    sucursal_stock_id: list[int] = Form(default=[]),  # opcional: sucursales cuyo stock se ajusta aquí
    cantidad_stock: list[str] = Form(default=[]),      # cantidad exacta para cada una (en blanco = no tocar)
    foto: UploadFile | None = File(None),
):
    """Guarda los cambios del producto (título, categoría, los 2 precios, costo, foto
    y, opcionalmente, la cantidad exacta de piezas en una o varias sucursales).

    Los precios se pueden escribir a mano, o recalcular a partir de un % de
    utilidad sobre el costo (precio = costo × (1 + %/100)); si se da el %,
    tiene prioridad sobre el precio escrito a mano en el mismo envío.

    La foto solo se reemplaza si se elige un archivo nuevo; si no, se conserva
    la que ya tenía (no hace falta volver a subirla en cada edición).

    El stock por sucursal es un ajuste (fija la cantidad exacta, igual que
    "Registrar movimiento" → Ajuste): dejar una sucursal en blanco significa
    no tocarla.
    """
    sku = sku.strip()
    if not sku:
        return RedirectResponse(
            f"/productos/{producto_id}/editar?error=El SKU no puede quedar vacío.",
            status_code=303,
        )

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

    # En la tienda no se manejan centavos: redondea a pesos enteros, venga el
    # precio del % de utilidad de arriba o escrito directo a mano.
    if precio_valor is not None:
        precio_valor = round(precio_valor)
    if precio_mayoreo_valor is not None:
        precio_mayoreo_valor = round(precio_mayoreo_valor)

    # Ajustes de stock por sucursal: se validan TODOS (solo lecturas) antes de
    # escribir nada. Sucursal con cantidad en blanco = no se toca.
    if len(sucursal_stock_id) != len(cantidad_stock):
        return RedirectResponse("/?error=Datos de sucursales inconsistentes.", status_code=303)

    ajustes_stock = {}  # sucursal_id -> nueva cantidad exacta
    for suc_id, cant_str in zip(sucursal_stock_id, cantidad_stock):
        cant_str = cant_str.strip()
        if not cant_str:
            continue
        try:
            cant_num = int(cant_str)
        except ValueError:
            return RedirectResponse("/?error=La cantidad debe ser un número entero.", status_code=303)
        if cant_num < 0:
            return RedirectResponse("/?error=La cantidad no puede ser negativa.", status_code=303)
        ajustes_stock[suc_id] = cant_num

    campos_sql = (
        "sku = :sku, titulo = :titulo, categoria = :categoria, "
        "precio = :precio, precio_mayoreo = :precio_mayoreo, "
        "costo = :costo, actualizado_en = NOW()"
    )
    parametros = {
        "sku": sku,
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
        # El SKU es único: si otro producto ya lo usa, se avisa en vez de
        # dejar que la restricción de la base de datos truene feo.
        choque = conn.execute(text(
            "SELECT titulo FROM productos WHERE sku = :sku AND id != :id"
        ), {"sku": sku, "id": producto_id}).scalar()
        if choque is not None:
            return RedirectResponse(
                f"/productos/{producto_id}/editar?error=Ese SKU ya lo usa otro producto: {choque}",
                status_code=303,
            )

        conn.execute(text(f"UPDATE productos SET {campos_sql} WHERE id = :id"), parametros)

        for suc_id, cant_nueva in ajustes_stock.items():
            actual = conn.execute(text(
                "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
            ), {"p": producto_id, "s": suc_id}).scalar()
            actual = actual if actual is not None else 0
            delta = cant_nueva - actual

            conn.execute(text(
                "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
                "ON CONFLICT (producto_id, sucursal_id) "
                "DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
            ), {"p": producto_id, "s": suc_id, "c": cant_nueva})

            conn.execute(text(
                "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                "VALUES (:p, :s, 'ajuste', :delta, 'ajuste manual desde Editar producto')"
            ), {"p": producto_id, "s": suc_id, "delta": delta})

    if ajustes_stock:
        background_tasks.add_task(empujar_stock_producto_seguro, producto_id)
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


@app.get("/caja", dependencies=[Depends(requiere_admin)])
def corte_caja(request: Request, fecha: str | None = None):
    """Corte de caja diario: cuánto entró en efectivo/tarjeta/transferencia,
    por sucursal, para cuadrar contra la caja física al cerrar.

    Se basa en `pagos.creado_en` (el momento real en que entró el dinero),
    no en `ventas.creada_en` — un abono de una venta de ayer que se cobra
    hoy debe contar en el corte de HOY, no en el de ayer.
    """
    if not fecha:
        fecha = date.today().isoformat()

    with engine.connect() as conn:
        sucursales = conn.execute(text(
            "SELECT nombre FROM sucursales WHERE activa = TRUE ORDER BY id"
        )).scalars().all()

        # AT TIME ZONE convierte el timestamp (guardado en UTC) a hora de
        # CDMX antes de quedarse solo con la fecha, para no partir el día
        # a medianoche UTC (que en CDMX cae a media tarde/noche).
        filas = conn.execute(text(
            "SELECT s.nombre AS sucursal, p.metodo, SUM(p.monto) AS total, COUNT(*) AS num_pagos "
            "FROM pagos p "
            "JOIN ventas v ON v.id = p.venta_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "WHERE (p.creado_en AT TIME ZONE 'America/Mexico_City')::date = :fecha "
            "GROUP BY s.nombre, p.metodo "
            "ORDER BY s.nombre, p.metodo"
        ), {"fecha": fecha}).mappings().all()

        # Ventas nuevas registradas hoy (para contexto: no siempre coincide
        # con lo cobrado hoy si hubo apartados/abonos de otros días).
        ventas_hoy = conn.execute(text(
            "SELECT COUNT(DISTINCT COALESCE(pedido_id, 'v' || id::text)) AS num_tickets, "
            "       COALESCE(SUM(precio_unitario * cantidad), 0) AS total "
            "FROM ventas "
            "WHERE (creada_en AT TIME ZONE 'America/Mexico_City')::date = :fecha"
        ), {"fecha": fecha}).mappings().one()

    # Arma la matriz sucursal × método con los 3 métodos siempre presentes
    # (aunque no haya habido cobros de ese tipo, para que la tabla no "salte"),
    # y con TODAS las sucursales activas (aunque no hayan cobrado nada hoy).
    metodos = ("efectivo", "tarjeta", "transferencia")
    por_sucursal: dict[str, dict] = {nombre: {m: 0.0 for m in metodos} for nombre in sucursales}
    for f in filas:
        por_sucursal.setdefault(f["sucursal"], {m: 0.0 for m in metodos})[f["metodo"]] = float(f["total"])

    filas_tabla = []
    total_general = 0.0
    for suc_nombre, montos in por_sucursal.items():
        total_suc = sum(montos.values())
        total_general += total_suc
        filas_tabla.append({"sucursal": suc_nombre, "montos": montos, "total": total_suc})

    return templates.TemplateResponse(request, "caja.html", {
        "fecha": fecha,
        "metodos": metodos,
        "filas": filas_tabla,
        "total_general": total_general,
        "num_tickets_hoy": ventas_hoy["num_tickets"],
        "total_ventas_hoy": float(ventas_hoy["total"]),
    })


@app.get("/ventas/exportar.csv", dependencies=[Depends(requiere_admin)])
def exportar_ventas(desde: str | None = None, hasta: str | None = None):
    """Descarga en CSV el detalle de ventas del período (mismo filtro de
    fechas que /reportes), para el contador o análisis fuera del sistema."""
    hoy = date.today()
    if not desde:
        desde = hoy.replace(day=1).isoformat()
    if not hasta:
        hasta = hoy.isoformat()

    with engine.connect() as conn:
        filas = conn.execute(text(
            "SELECT v.creada_en, p.sku, p.titulo, s.nombre AS sucursal, v.canal, "
            "       v.tipo_precio, v.cantidad, v.precio_unitario, v.costo_unitario, "
            "       v.descuento_pct, c.nombre AS clienta, ve.nombre AS vendedora, v.pedido_id "
            "FROM ventas v "
            "JOIN productos p ON p.id = v.producto_id "
            "JOIN sucursales s ON s.id = v.sucursal_id "
            "LEFT JOIN clientas c ON c.id = v.cliente_id "
            "LEFT JOIN vendedoras ve ON ve.id = v.vendedora_id "
            "WHERE v.creada_en::date BETWEEN :desde AND :hasta "
            # Igual que en /reportes: los apartados con saldo pendiente no
            # cuentan todavía (no se han cobrado de verdad).
            "AND (NOT v.apartado OR "
            "     (SELECT COALESCE(SUM(pg.monto), 0) FROM pagos pg WHERE pg.venta_id = v.id) "
            "     >= v.precio_unitario * v.cantidad) "
            "ORDER BY v.creada_en"
        ), {"desde": desde, "hasta": hasta}).mappings().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Fecha", "SKU", "Producto", "Sucursal", "Canal", "Tipo de precio",
        "Cantidad", "Precio unitario", "Costo unitario", "Descuento %",
        "Subtotal", "Ganancia bruta", "Clienta", "Vendedora", "Pedido",
    ])
    for f in filas:
        cantidad = f["cantidad"]
        precio_unitario = float(f["precio_unitario"])
        costo_unitario = float(f["costo_unitario"] or 0)
        subtotal = precio_unitario * cantidad
        ganancia = subtotal - (costo_unitario * cantidad)
        writer.writerow([
            f["creada_en"].astimezone(ZONA_CDMX).strftime("%Y-%m-%d %H:%M"),
            f["sku"], f["titulo"], f["sucursal"],
            CANALES.get(f["canal"], f["canal"]),
            "Menudeo" if f["tipo_precio"] == "menudeo" else "Mayoreo",
            cantidad, precio_unitario, costo_unitario,
            float(f["descuento_pct"] or 0), round(subtotal, 2), round(ganancia, 2),
            f["clienta"] or "", f["vendedora"] or "", f["pedido_id"] or "",
        ])

    nombre_archivo = f"ventas_{desde}_a_{hasta}.csv"
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"},
    )


@app.get("/reportes", dependencies=[Depends(requiere_admin)])
def reportes(request: Request, desde: str | None = None, hasta: str | None = None):
    """Reporte de ganancia bruta por período, con desglose por categoría, sucursal y canal."""
    # Por defecto, del primer día del mes actual hasta hoy.
    hoy = date.today()
    if not desde:
        desde = hoy.replace(day=1).isoformat()
    if not hasta:
        hasta = hoy.isoformat()

    # Filtra por la fecha de la venta (comparando solo la parte de fecha).
    # Los apartados con saldo pendiente NO cuentan como ingreso/ganancia
    # todavía (la prenda salió pero no se ha cobrado); en cuanto se terminan
    # de pagar, el subquery de pagos ya cubre el total y entran solos.
    where = (
        "WHERE v.creada_en::date BETWEEN :desde AND :hasta "
        "AND (NOT v.apartado OR "
        "     (SELECT COALESCE(SUM(pg.monto), 0) FROM pagos pg WHERE pg.venta_id = v.id) "
        "     >= v.precio_unitario * v.cantidad)"
    )
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

        por_vendedora = conn.execute(text(
            f"SELECT COALESCE(ve.nombre, 'Sin vendedora') AS dim, {_METRICAS} "
            f"FROM ventas v LEFT JOIN vendedoras ve ON ve.id = v.vendedora_id {where} "
            f"GROUP BY ve.nombre ORDER BY bruta DESC"
        ), params).mappings().all()

        # Ticket promedio = gasto promedio POR VISITA/COMPRA, no por prenda.
        # Agrupa las líneas de una misma compra por pedido_id (las ventas
        # individuales usan su propio id como grupo de 1 sola línea).
        ticket = conn.execute(text(
            "SELECT COUNT(*) AS num_tickets, AVG(total_ticket) AS ticket_promedio "
            "FROM ("
            "  SELECT COALESCE(v.pedido_id, 'v' || v.id::text) AS ticket_key, "
            "         SUM(v.precio_unitario * v.cantidad) AS total_ticket "
            f" FROM ventas v {where} "
            "  GROUP BY COALESCE(v.pedido_id, 'v' || v.id::text)"
            ") sub"
        ), params).mappings().one()

        # Top 10 productos por ganancia bruta, en el mismo período filtrado.
        top_productos = conn.execute(text(
            f"SELECT p.id, p.sku, p.titulo, {_METRICAS} "
            f"FROM ventas v JOIN productos p ON p.id = v.producto_id {where} "
            f"GROUP BY p.id, p.sku, p.titulo ORDER BY bruta DESC LIMIT 10"
        ), params).mappings().all()

        # Prendas con stock que no se han vendido en DIAS_SIN_VENDER_ALERTA días
        # (o nunca) — candidatas a liquidar o dejar de reordenar. Esto NO se
        # filtra por el período del reporte: siempre mira "hasta hoy".
        sin_movimiento = conn.execute(text(
            "SELECT p.sku, p.titulo, p.categoria, MAX(v.creada_en) AS ultima_venta, "
            "       COALESCE(SUM(st.cantidad), 0) AS stock_total "
            "FROM productos p "
            "LEFT JOIN ventas v ON v.producto_id = p.id "
            "LEFT JOIN stock st ON st.producto_id = p.id "
            "WHERE p.activo = TRUE "
            "GROUP BY p.id, p.sku, p.titulo, p.categoria "
            "HAVING COALESCE(SUM(st.cantidad), 0) > 0 "
            "   AND (MAX(v.creada_en) IS NULL "
            "        OR MAX(v.creada_en) < NOW() - (:dias || ' days')::interval) "
            "ORDER BY ultima_venta ASC NULLS FIRST "
            "LIMIT 50"
        ), {"dias": DIAS_SIN_VENDER_ALERTA}).mappings().all()

    canal_filas = []
    for r in por_canal:
        fila = _resumen(r)
        fila["dim"] = CANALES.get(fila["dim"], fila["dim"])
        canal_filas.append(fila)

    # Top productos: mismas métricas, con sku/título en vez de una dimensión.
    top_productos_filas = []
    for r in top_productos:
        fila = _resumen(r)
        fila["id"] = r["id"]
        fila["sku"] = r["sku"]
        fila["titulo"] = r["titulo"]
        top_productos_filas.append(fila)

    # Productos sin venta reciente: calcula días sin vender para mostrar.
    ahora = datetime.now(ZONA_CDMX)
    sin_movimiento_filas = []
    for r in sin_movimiento:
        ultima = r["ultima_venta"].astimezone(ZONA_CDMX) if r["ultima_venta"] else None
        sin_movimiento_filas.append({
            "sku": r["sku"], "titulo": r["titulo"], "categoria": r["categoria"],
            "ultima_venta": ultima,
            "dias_sin_vender": (ahora - ultima).days if ultima else None,
            "stock_total": r["stock_total"],
        })

    return templates.TemplateResponse(request, "reportes.html", {
        "desde": desde,
        "hasta": hasta,
        "total": _resumen(total),
        "por_categoria": [_resumen(r) for r in por_categoria],
        "por_sucursal": [_resumen(r) for r in por_sucursal],
        "por_canal": canal_filas,
        "por_vendedora": [_resumen(r) for r in por_vendedora],
        "num_tickets": int(ticket["num_tickets"] or 0),
        "ticket_promedio": float(ticket["ticket_promedio"]) if ticket["ticket_promedio"] is not None else 0.0,
        "top_productos": top_productos_filas,
        "sin_movimiento": sin_movimiento_filas,
        "dias_alerta_stock": DIAS_SIN_VENDER_ALERTA,
    })
