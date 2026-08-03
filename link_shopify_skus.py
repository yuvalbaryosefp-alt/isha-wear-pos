"""
Script de un solo uso: liga cada producto de isha-wear-pos con su
contraparte en Shopify (mismo SKU), guarda los IDs de Shopify en la base,
conecta el inventario a las 2 ubicaciones (Tecamachalco/Prado Norte) y
empuja el stock real actual como punto de partida.

No toca los productos que no tengan match — esos quedan igual que antes.

Uso:
    .venv/Scripts/python.exe link_shopify_skus.py
"""

import re

import requests
from sqlalchemy import text

from app.config import SHOPIFY_STORE, SHOPIFY_API_VERSION
from app.db import engine
from app.shopify_sync import obtener_token, conectar_inventario, empujar_stock_producto


def normalizar(sku: str) -> str:
    return sku.strip().upper()


def obtener_variantes_shopify(token: str) -> dict[str, dict]:
    resultado = {}
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=250"
    headers = {"X-Shopify-Access-Token": token}

    while url:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        for p in r.json()["products"]:
            for v in p["variants"]:
                sku = (v.get("sku") or "").strip()
                if not sku:
                    continue
                resultado[normalizar(sku)] = {
                    "product_id": p["id"],
                    "variant_id": v["id"],
                    "inventory_item_id": v["inventory_item_id"],
                }
        link = r.headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None

    return resultado


def main() -> None:
    print("Pidiendo token y descargando catálogo de Shopify...")
    token = obtener_token()
    shopify_por_sku = obtener_variantes_shopify(token)

    with engine.begin() as conn:
        productos = conn.execute(text(
            "SELECT id, sku FROM productos WHERE activo = TRUE"
        )).mappings().all()

        ubicaciones = conn.execute(text(
            "SELECT shopify_location_id FROM sucursales "
            "WHERE activa = TRUE AND shopify_location_id IS NOT NULL"
        )).scalars().all()

    ligados = 0
    for p in productos:
        match = shopify_por_sku.get(normalizar(p["sku"]))
        if not match:
            continue

        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE productos SET shopify_product_id = :pid, shopify_variant_id = :vid, "
                "shopify_inventory_item_id = :iid WHERE id = :id"
            ), {
                "pid": match["product_id"], "vid": match["variant_id"],
                "iid": match["inventory_item_id"], "id": p["id"],
            })

        # Conecta el inventario a ambas ubicaciones antes de poder fijarle cantidad.
        for location_id in ubicaciones:
            conectar_inventario(match["inventory_item_id"], location_id)

        empujar_stock_producto(p["id"])
        ligados += 1
        print(f"  Ligado y sincronizado: SKU {p['sku']}")

    print(f"\nListo. {ligados} productos ligados y con su stock real ya empujado a Shopify.")


if __name__ == "__main__":
    main()
