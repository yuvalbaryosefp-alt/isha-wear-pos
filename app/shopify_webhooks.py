"""
Recibe pedidos pagados desde Shopify (webhook orders/paid) y los refleja en
isha-wear-pos: descuenta el stock de la sucursal correcta y registra la
venta (canal='ecommerce') para que aparezca en reportes/ticket promedio.

Seguridad: cada request se verifica con la firma HMAC que manda Shopify
(firmada con el Client Secret de la app) — cualquier request sin firma
válida se rechaza, así nadie puede "inventar" una venta falsa llamando
directo a este endpoint.
"""

import base64
import hashlib
import hmac
from datetime import datetime, timezone

import requests
from sqlalchemy import text

from app.config import SHOPIFY_CLIENT_SECRET, SHOPIFY_STORE, SHOPIFY_API_VERSION
from app.db import engine
from app.shopify_sync import headers_autenticados


def verificar_firma(cuerpo_crudo: bytes, firma_header: str | None) -> bool:
    """Confirma que el request realmente viene de Shopify (firma HMAC del
    cuerpo crudo, usando el Client Secret de la app como llave)."""
    if not firma_header or not SHOPIFY_CLIENT_SECRET:
        return False
    digest = hmac.new(SHOPIFY_CLIENT_SECRET.encode(), cuerpo_crudo, hashlib.sha256).digest()
    calculada = base64.b64encode(digest).decode()
    return hmac.compare_digest(calculada, firma_header)


def _ubicacion_del_pedido(order_id: int) -> int | None:
    """Pregunta a Shopify desde qué ubicación se está surtiendo el pedido
    (para saber de cuál sucursal descontar). Si el pedido tiene varias
    fulfillment orders (surtido dividido entre sucursales), se usa la
    primera — caso raro con solo 2 ubicaciones chicas."""
    r = requests.get(
        f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/fulfillment_orders.json",
        headers=headers_autenticados(),
        timeout=15,
    )
    r.raise_for_status()
    ordenes = r.json().get("fulfillment_orders", [])
    return ordenes[0]["assigned_location_id"] if ordenes else None


def procesar_pedido(payload: dict) -> None:
    """Registra en isha-wear-pos las líneas de un pedido de Shopify ya
    pagado. Ignora (sin error) los SKU que no estén ligados a un producto
    de nuestro sistema — probablemente no se han contado físicamente todavía.
    """
    order_id = payload["id"]

    with engine.begin() as conn:
        ya_procesado = conn.execute(text(
            "SELECT 1 FROM ventas WHERE shopify_order_id = :oid LIMIT 1"
        ), {"oid": order_id}).scalar()
        if ya_procesado:
            return  # Shopify puede reenviar el mismo webhook más de una vez.

    location_id = _ubicacion_del_pedido(order_id)
    with engine.begin() as conn:
        sucursal = conn.execute(text(
            "SELECT id FROM sucursales WHERE shopify_location_id = :loc"
        ), {"loc": location_id}).mappings().one_or_none()

    if sucursal is None:
        # No sabemos de qué sucursal física salió (o Shopify no asignó
        # ubicación) — no adivinamos, se queda sin registrar para revisión manual.
        print(f"[shopify_webhooks] Pedido {order_id}: sin ubicación reconocida ({location_id}), se omite.")
        return

    sucursal_id = sucursal["id"]
    pedido_id_propio = f"shopify-{payload.get('name', order_id)}"
    creada_en = payload.get("processed_at") or payload.get("created_at")
    creada_en = datetime.fromisoformat(creada_en) if creada_en else datetime.now(timezone.utc)

    with engine.begin() as conn:
        for item in payload.get("line_items", []):
            variant_id = item.get("variant_id")
            if variant_id is None:
                continue

            producto = conn.execute(text(
                "SELECT id, costo FROM productos WHERE shopify_variant_id = :vid"
            ), {"vid": variant_id}).mappings().one_or_none()
            if producto is None:
                continue  # SKU todavía no ligado a nuestro sistema, se omite.

            cantidad = int(item["quantity"])
            precio_unitario = float(item["price"])

            # No dejar que el stock quede negativo aunque Shopify muestre más
            # de lo que en realidad tenemos contado (mejor un desfase visible
            # que un dato imposible) — se deja rastro del ajuste igual.
            actual = conn.execute(text(
                "SELECT cantidad FROM stock WHERE producto_id = :p AND sucursal_id = :s"
            ), {"p": producto["id"], "s": sucursal_id}).scalar()
            actual = actual if actual is not None else 0
            nuevo = max(0, actual - cantidad)

            conn.execute(text(
                "INSERT INTO stock (producto_id, sucursal_id, cantidad) VALUES (:p, :s, :c) "
                "ON CONFLICT (producto_id, sucursal_id) DO UPDATE SET cantidad = :c, actualizado_en = NOW()"
            ), {"p": producto["id"], "s": sucursal_id, "c": nuevo})

            conn.execute(text(
                "INSERT INTO movimientos (producto_id, sucursal_id, tipo, delta, motivo) "
                "VALUES (:p, :s, 'venta', :delta, :motivo)"
            ), {
                "p": producto["id"], "s": sucursal_id, "delta": -(actual - nuevo),
                "motivo": f"venta ecommerce (pedido Shopify {payload.get('name', order_id)})",
            })

            venta_id = conn.execute(text(
                "INSERT INTO ventas "
                "(producto_id, sucursal_id, canal, tipo_precio, cantidad, precio_unitario, "
                " costo_unitario, descuento_pct, pedido_id, shopify_order_id, creada_en) "
                "VALUES (:p, :s, 'ecommerce', 'menudeo', :cant, :precio, :costo, NULL, :pedido, :oid, :creada) "
                "RETURNING id"
            ), {
                "p": producto["id"], "s": sucursal_id, "cant": cantidad, "precio": precio_unitario,
                "costo": producto["costo"], "pedido": pedido_id_propio, "oid": order_id, "creada": creada_en,
            }).scalar_one()

            conn.execute(text(
                "INSERT INTO pagos (venta_id, metodo, monto, creado_en) "
                "VALUES (:v, 'tarjeta', :monto, :creada)"
            ), {"v": venta_id, "monto": round(precio_unitario * cantidad, 2), "creada": creada_en})

    print(f"[shopify_webhooks] Pedido {payload.get('name', order_id)} procesado correctamente.")
