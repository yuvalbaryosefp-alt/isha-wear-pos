"""
Crea las tablas en la base de datos ejecutando el archivo schema.sql.

Se corre UNA sola vez (o cada vez que cambie el esquema; las tablas usan
"CREATE TABLE IF NOT EXISTS", así que volver a correrlo no borra nada).

Uso:
    .venv/Scripts/python.exe init_db.py
"""

from pathlib import Path

from sqlalchemy import text

from app.db import engine


def main() -> None:
    # Lee el contenido del archivo schema.sql (mismo folder que este script).
    schema_sql = Path("schema.sql").read_text(encoding="utf-8")

    # engine.begin() abre una transacción y hace commit automático al terminar.
    # exec_driver_sql manda el SQL crudo al driver (permite varias instrucciones).
    with engine.begin() as conn:
        conn.exec_driver_sql(schema_sql)

    # Verifica qué tablas quedaron creadas, para confirmar que funcionó.
    with engine.connect() as conn:
        filas = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ))
        tablas = [fila[0] for fila in filas]

    print("Tablas en la base de datos:")
    for tabla in tablas:
        print(f"  - {tabla}")
    print("\nListo. Base de datos inicializada correctamente.")


if __name__ == "__main__":
    main()
