"""
Integración con Shopify: empuja el stock real de isha-wear-pos hacia la
tienda en línea, por sucursal (cada sucursal es una "ubicación" distinta
en Shopify), y sirve de base para más adelante recibir pedidos desde allá.

Usa client_credentials grant (Shopify ya no da un token fijo para apps
custom desde 2026): se pide un access token nuevo cada vez que se necesita
uno y no hay uno vigente en caché (dura 24h).
"""

import base64
import time

import requests
from sqlalchemy import text

from app.db import engine
from app.config import SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION

_token_cache = {"valor": None, "expira_en": 0.0}


def obtener_token() -> str:
    """Devuelve un access token vigente, pidiendo uno nuevo si hace falta."""
    if _token_cache["valor"] and time.time() < _token_cache["expira_en"]:
        return _token_cache["valor"]

    r = requests.post(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        data={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    r.raise_for_status()
    datos = r.json()
    _token_cache["valor"] = datos["access_token"]
    # Se pide uno nuevo 5 minutos antes de que expire de verdad, por margen.
    _token_cache["expira_en"] = time.time() + datos["expires_in"] - 300
    return _token_cache["valor"]


def headers_autenticados() -> dict:
    return {"X-Shopify-Access-Token": obtener_token(), "Content-Type": "application/json"}


def _url(ruta: str) -> str:
    return f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{ruta}"


def _post_con_reintento(url: str, payload: dict) -> requests.Response:
    """POST a la API de Shopify, reintentando con espera si responde 429
    (límite de peticiones) — Shopify indica cuánto esperar en Retry-After."""
    for _ in range(5):
        r = requests.post(url, json=payload, headers=headers_autenticados(), timeout=15)
        if r.status_code != 429:
            return r
        time.sleep(float(r.headers.get("Retry-After", 2)))
    r.raise_for_status()
    return r


def conectar_inventario(inventory_item_id: int, location_id: int) -> None:
    """Asegura que Shopify rastree ese producto en esa ubicación (hace falta
    antes de poder fijarle una cantidad ahí). Si ya estaba conectado, Shopify
    responde con éxito de todos modos (es una operación segura de repetir)."""
    _post_con_reintento(
        _url("inventory_levels/connect.json"),
        {"location_id": location_id, "inventory_item_id": inventory_item_id},
    )


def fijar_cantidad(inventory_item_id: int, location_id: int, cantidad: int) -> None:
    """Fija el stock disponible de un producto en una ubicación específica."""
    r = _post_con_reintento(
        _url("inventory_levels/set.json"),
        {"location_id": location_id, "inventory_item_id": inventory_item_id, "available": cantidad},
    )
    r.raise_for_status()


def empujar_stock_producto_seguro(producto_id: int) -> None:
    """Igual que empujar_stock_producto, pero nunca lanza una excepción.

    Pensada para llamarse como BackgroundTask después de responder al
    navegador: una venta real en la tienda física NUNCA debe fallar ni
    hacerse más lenta porque Shopify esté caído o lento en ese momento.
    Si falla, simplemente se queda desincronizado hasta el siguiente cambio
    de stock de ese producto (no es grave, no es dinero).
    """
    try:
        empujar_stock_producto(producto_id)
    except Exception as error:
        print(f"[shopify_sync] No se pudo empujar el stock del producto {producto_id}: {error}")


def empujar_stock_productos_seguro(producto_ids) -> None:
    """Variante para varios productos a la vez (ej. un carrito con varias prendas)."""
    for producto_id in set(producto_ids):
        empujar_stock_producto_seguro(producto_id)


def empujar_stock_producto(producto_id: int) -> bool:
    """Empuja a Shopify el stock actual (por sucursal) de un producto, si está
    ligado a Shopify. Devuelve True si se empujó, False si el producto no
    está ligado (no hace nada en ese caso, no es un error)."""
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT shopify_inventory_item_id FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

        if producto is None or producto["shopify_inventory_item_id"] is None:
            return False

        stock_por_sucursal = conn.execute(text(
            "SELECT s.shopify_location_id, COALESCE(st.cantidad, 0) AS cantidad "
            "FROM sucursales s "
            "LEFT JOIN stock st ON st.sucursal_id = s.id AND st.producto_id = :id "
            "WHERE s.activa = TRUE AND s.shopify_location_id IS NOT NULL"
        ), {"id": producto_id}).mappings().all()

    inventory_item_id = producto["shopify_inventory_item_id"]
    for fila in stock_por_sucursal:
        fijar_cantidad(inventory_item_id, fila["shopify_location_id"], fila["cantidad"])

    return True


class ProductoYaLigadoError(Exception):
    """Se intentó enviar a Shopify un producto que ya tenía un link."""


class FaltaPrecioError(Exception):
    """No se puede crear el producto en Shopify sin precio de menudeo."""


def crear_producto_en_shopify(producto_id: int) -> None:
    """Crea un producto NUEVO en Shopify (que todavía no existe allá) a partir
    de los datos ya capturados en isha-wear-pos: título, SKU, categoría,
    precio y foto (si tiene). Se crea como **borrador** (status='draft') a
    propósito — no se publica solo, para que se pueda revisar/completar
    (descripción, más fotos) antes de que la vea una clienta.

    Al terminar, liga el producto (guarda sus IDs de Shopify) y le empuja
    el stock inicial real, igual que con los productos ligados por SKU.
    """
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT sku, titulo, categoria, precio, costo, foto, foto_tipo, shopify_product_id "
            "FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

    if producto is None:
        raise ValueError("Producto no encontrado.")
    if producto["shopify_product_id"] is not None:
        raise ProductoYaLigadoError("Este producto ya está ligado a Shopify.")
    if producto["precio"] is None:
        raise FaltaPrecioError("Ponle un precio de menudeo antes de enviarlo a Shopify.")

    payload = {
        "product": {
            "title": producto["titulo"],
            "vendor": "Isha Wear",
            "product_type": producto["categoria"] or "",
            "status": "draft",
            "variants": [{
                "sku": producto["sku"],
                "price": f"{producto['precio']:.2f}",
                "inventory_management": "shopify",
            }],
        }
    }
    if producto["foto"] is not None:
        payload["product"]["images"] = [{"attachment": base64.b64encode(bytes(producto["foto"])).decode()}]

    r = _post_con_reintento(_url("products.json"), payload)
    r.raise_for_status()
    creado = r.json()["product"]
    variante = creado["variants"][0]

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE productos SET shopify_product_id = :pid, shopify_variant_id = :vid, "
            "shopify_inventory_item_id = :iid WHERE id = :id"
        ), {
            "pid": creado["id"], "vid": variante["id"],
            "iid": variante["inventory_item_id"], "id": producto_id,
        })

        ubicaciones = conn.execute(text(
            "SELECT shopify_location_id FROM sucursales "
            "WHERE activa = TRUE AND shopify_location_id IS NOT NULL"
        )).scalars().all()

    for location_id in ubicaciones:
        conectar_inventario(variante["inventory_item_id"], location_id)

    empujar_stock_producto(producto_id)


def limpiar_titulo_y_foto(producto_id: int) -> str:
    """Para un producto YA ligado a Shopify (normalmente de los que se
    ligaron por SKU, no por el botón manual): corrige el título en Shopify
    para que coincida con el nuestro (el catálogo viejo de Shopify tiene un
    bug de códigos numéricos pegados al título) y, si Shopify no tiene
    ninguna foto todavía, le sube la que tengamos en isha-wear-pos.

    Devuelve una palabra describiendo qué se hizo, para reportar en lote:
    "titulo+foto", "titulo", "foto", o "sin_cambios".
    """
    with engine.connect() as conn:
        producto = conn.execute(text(
            "SELECT titulo, foto, shopify_product_id FROM productos WHERE id = :id"
        ), {"id": producto_id}).mappings().one_or_none()

    if producto is None or producto["shopify_product_id"] is None:
        return "sin_cambios"

    shopify_id = producto["shopify_product_id"]
    r = requests.get(_url(f"products/{shopify_id}.json"), headers=headers_autenticados(), timeout=15)
    r.raise_for_status()
    actual = r.json()["product"]

    cambios = {}
    hizo_titulo = actual["title"] != producto["titulo"]
    if hizo_titulo:
        cambios["title"] = producto["titulo"]

    hizo_foto = not actual.get("images") and producto["foto"] is not None
    if hizo_foto:
        cambios["images"] = [{"attachment": base64.b64encode(bytes(producto["foto"])).decode()}]

    if not cambios:
        return "sin_cambios"

    cambios["id"] = shopify_id
    r2 = _post_con_reintento_put(_url(f"products/{shopify_id}.json"), cambios)
    r2.raise_for_status()

    if hizo_titulo and hizo_foto:
        return "titulo+foto"
    return "titulo" if hizo_titulo else "foto"


def _post_con_reintento_put(url: str, payload: dict) -> requests.Response:
    """Igual que _post_con_reintento pero con PUT (actualizar un recurso
    existente en vez de crear uno nuevo)."""
    for _ in range(5):
        r = requests.put(url, json={"product": payload}, headers=headers_autenticados(), timeout=15)
        if r.status_code != 429:
            return r
        time.sleep(float(r.headers.get("Retry-After", 2)))
    r.raise_for_status()
    return r
