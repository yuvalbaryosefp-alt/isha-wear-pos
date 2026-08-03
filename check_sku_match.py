"""
Script de diagnóstico (uso único): compara los SKU del catálogo real de
Shopify contra los SKU que ya existen en isha-wear-pos, para decidir si el
sync se puede hacer por match directo de SKU o si hace falta otra estrategia.

No modifica nada en ninguno de los 2 sistemas, solo lee y reporta.

Uso:
    .venv/Scripts/python.exe check_sku_match.py
"""

import os
import re

import requests
from dotenv import load_dotenv
from sqlalchemy import text

from app.db import engine

load_dotenv()

STORE = os.environ["SHOPIFY_STORE"]
CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
API_VERSION = os.environ["SHOPIFY_API_VERSION"]


def obtener_token() -> str:
    r = requests.post(
        f"https://{STORE}/admin/oauth/access_token",
        data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def normalizar(sku: str) -> str:
    """Quita espacios y mayúsculas para comparar, sin cambiar el SKU real."""
    return sku.strip().upper()


def obtener_skus_shopify(token: str) -> dict[str, dict]:
    """Devuelve {sku_normalizado: {producto_id, variante_id, titulo, sku_original}}."""
    resultado = {}
    url = f"https://{STORE}/admin/api/{API_VERSION}/products.json?limit=250"
    headers = {"X-Shopify-Access-Token": token}

    while url:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        productos = r.json()["products"]
        for p in productos:
            for v in p["variants"]:
                sku = (v.get("sku") or "").strip()
                if not sku:
                    continue
                resultado[normalizar(sku)] = {
                    "producto_id": p["id"],
                    "variante_id": v["id"],
                    "inventory_item_id": v["inventory_item_id"],
                    "titulo": p["title"],
                    "sku_original": sku,
                }

        # Paginación por cursor: Shopify manda la siguiente URL en el header Link.
        link = r.headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None

    return resultado


def main() -> None:
    print("Pidiendo token...")
    token = obtener_token()

    print("Descargando catálogo de Shopify (puede tardar unos segundos)...")
    shopify_por_sku = obtener_skus_shopify(token)
    print(f"Shopify: {len(shopify_por_sku)} variantes con SKU (de un catálogo más grande sin contar duplicados/sin SKU).")

    with engine.begin() as conn:
        productos = conn.execute(text(
            "SELECT id, sku, titulo FROM productos WHERE activo = TRUE"
        )).mappings().all()
    print(f"isha-wear-pos: {len(productos)} productos activos.")

    coinciden = []
    sin_match_en_sistema = []
    for p in productos:
        clave = normalizar(p["sku"])
        if clave in shopify_por_sku:
            coinciden.append((p, shopify_por_sku[clave]))
        else:
            sin_match_en_sistema.append(p)

    skus_sistema = {normalizar(p["sku"]) for p in productos}
    sin_match_en_shopify = [v for k, v in shopify_por_sku.items() if k not in skus_sistema]

    print()
    print("=" * 60)
    print(f"COINCIDEN (mismo SKU en ambos): {len(coinciden)} de {len(productos)}")
    print(f"EN SISTEMA pero SIN match en Shopify: {len(sin_match_en_sistema)}")
    print(f"EN SHOPIFY pero SIN match en el sistema: {len(sin_match_en_shopify)}")
    print("=" * 60)

    print("\nEjemplos que SÍ coinciden (hasta 10):")
    for p, s in coinciden[:10]:
        print(f"  SKU {p['sku']!r}: sistema={p['titulo']!r}  |  shopify={s['titulo']!r}")

    print("\nEjemplos EN SISTEMA sin match en Shopify (hasta 10):")
    for p in sin_match_en_sistema[:10]:
        print(f"  SKU {p['sku']!r}: {p['titulo']!r}")

    print("\nEjemplos EN SHOPIFY sin match en el sistema (hasta 10):")
    for s in sin_match_en_shopify[:10]:
        print(f"  SKU {s['sku_original']!r}: {s['titulo']!r}")


if __name__ == "__main__":
    main()
