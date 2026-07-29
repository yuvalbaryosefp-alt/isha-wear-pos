"""
Conexión a la base de datos PostgreSQL.

Lee la variable DATABASE_URL del archivo .env y crea el "engine" de SQLAlchemy,
que es el objeto encargado de abrir y administrar las conexiones a Postgres.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

# Carga las variables definidas en el archivo .env hacia el entorno del programa.
load_dotenv()

raw_url = os.getenv("DATABASE_URL")

# Si no encuentra la cadena de conexión, detiene el programa con un mensaje claro
# en vez de fallar más adelante con un error confuso.
if not raw_url:
    raise RuntimeError(
        "Falta DATABASE_URL en el archivo .env. "
        "Revisa que el archivo exista y tenga la cadena de conexión de Railway."
    )

# Railway entrega la URL empezando con "postgresql://" o "postgres://".
# SQLAlchemy necesita el prefijo "postgresql+psycopg://" para usar el driver
# correcto. Aquí lo ajustamos automáticamente, así puedes pegar la URL tal cual.
if raw_url.startswith("postgresql://"):
    DATABASE_URL = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
elif raw_url.startswith("postgres://"):
    DATABASE_URL = raw_url.replace("postgres://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = raw_url

# El engine administra un "pool" (grupo) de conexiones reutilizables.
# pool_pre_ping=True hace una mini-prueba antes de usar cada conexión,
# para que una conexión cerrada por el servidor no rompa la app.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
