"""
Integración con Shopify: empuja el stock real de isha-wear-pos hacia la
tienda en línea, por sucursal (cada sucursal es una "ubicación" distinta
en Shopify), y sirve de base para más adelante recibir pedidos desde allá.

Usa client_credentials grant (Shopify ya no da un token fijo para apps
custom desde 2026): se pide un access token nuevo cada vez que se necesita
uno y no hay uno vigente en caché (dura 24h).
"""

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


def _headers() -> dict:
    return {"X-Shopify-Access-Token": obtener_token(), "Content-Type": "application/json"}


def _url(ruta: str) -> str:
    return f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{ruta}"


def _post_con_reintento(url: str, payload: dict) -> requests.Response:
    """POST a la API de Shopify, reintentando con espera si responde 429
    (límite de peticiones) — Shopify indica cuánto esperar en Retry-After."""
    for _ in range(5):
        r = requests.post(url, json=payload, headers=_headers(), timeout=15)
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
