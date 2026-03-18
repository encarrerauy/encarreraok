#!/usr/bin/env python3
"""
migrate_sqlite_to_pg.py — Migración de datos de SQLite a PostgreSQL para EncarreraOK.

Uso:
    python3 scripts/migrate_sqlite_to_pg.py \\
        --sqlite-path /var/lib/encarreraok/encarreraok.sqlite3 \\
        --pg-url "postgresql://user:pass@host:5432/encarreraok"

    # Simulación sin escribir nada:
    python3 scripts/migrate_sqlite_to_pg.py --dry-run

    # Omitir alembic (ya aplicado manualmente):
    python3 scripts/migrate_sqlite_to_pg.py --skip-alembic
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Tablas en orden FK-safe (eventos primero, luego dependientes)
# ---------------------------------------------------------------------------
TABLAS_ORDEN = ["eventos", "deslindes", "aceptaciones"]

BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Helpers de conexión
# ---------------------------------------------------------------------------

def conectar_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def conectar_postgres(pg_url: str):
    """Retorna una conexión psycopg (v3) o lanza RuntimeError con mensaje claro."""
    try:
        import psycopg  # type: ignore
    except ImportError:
        raise RuntimeError(
            "psycopg no está instalado. "
            "Ejecuta: pip install 'psycopg[binary]'"
        )
    try:
        conn = psycopg.connect(pg_url)
        conn.autocommit = False
        return conn
    except Exception as exc:
        raise RuntimeError(
            f"No se pudo conectar a PostgreSQL.\n"
            f"URL usada: {pg_url}\n"
            f"Error: {exc}"
        )


# ---------------------------------------------------------------------------
# Paso 3: ejecutar alembic upgrade head
# ---------------------------------------------------------------------------

def ejecutar_alembic(dry_run: bool) -> None:
    if dry_run:
        print("[DRY-RUN] Se omitiría: alembic upgrade head")
        return

    print("PASO 3: Ejecutando alembic upgrade head...")
    resultado = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        print(f"  ERROR al ejecutar alembic upgrade head:")
        print(f"  stdout: {resultado.stdout}")
        print(f"  stderr: {resultado.stderr}")
        sys.exit(1)
    print(f"  OK — alembic upgrade head completado.")
    if resultado.stdout.strip():
        for linea in resultado.stdout.strip().splitlines():
            print(f"  {linea}")


# ---------------------------------------------------------------------------
# Paso 4: migrate_table
# ---------------------------------------------------------------------------

def migrate_table(
    src: sqlite3.Connection,
    dst,
    table_name: str,
    dry_run: bool,
) -> bool:
    """
    Migra todas las filas de `table_name` desde SQLite a PostgreSQL.

    - Usa ON CONFLICT DO NOTHING para idempotencia.
    - Procesa en batches de BATCH_SIZE con commit por batch.
    - En dry_run solo imprime el conteo de filas origen.

    Retorna True si la migración fue exitosa, False si hubo errores.
    """
    src_cur = src.cursor()
    src_cur.execute(f"SELECT * FROM {table_name}")
    filas = src_cur.fetchall()
    total = len(filas)

    if dry_run:
        print(f"  [DRY-RUN] {table_name}: {total} filas en SQLite (no se insertará nada)")
        return True

    if total == 0:
        print(f"  {table_name}: 0 filas — tabla vacía, nada que migrar.")
        return True

    # Obtener nombres de columnas de la primera fila
    columnas = list(filas[0].keys())
    cols_str = ", ".join(columnas)
    # psycopg2 usa %s como placeholder
    placeholders = ", ".join(["%s"] * len(columnas))
    sql = (
        f"INSERT INTO {table_name} ({cols_str}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )

    dst_cur = dst.cursor()
    migradas = 0
    errores = 0

    for i in range(0, total, BATCH_SIZE):
        batch = filas[i : i + BATCH_SIZE]
        try:
            for fila in batch:
                valores = tuple(fila[col] for col in columnas)
                dst_cur.execute(sql, valores)
                migradas += 1
            dst.commit()
        except Exception as exc:
            dst.rollback()
            errores += len(batch)
            inicio = i + 1
            fin = min(i + BATCH_SIZE, total)
            print(
                f"  ERROR en {table_name} filas {inicio}-{fin}: {exc}"
            )

    if errores == 0:
        print(f"  {table_name}: {migradas}/{total} ✓")
        return True
    else:
        print(f"  {table_name}: {migradas}/{total} migradas, {errores} con error ✗")
        return False


# ---------------------------------------------------------------------------
# Paso 5: verify_counts
# ---------------------------------------------------------------------------

def verify_counts(src: sqlite3.Connection, dst) -> bool:
    """
    Compara row counts entre SQLite y PostgreSQL para cada tabla.
    Imprime tabla comparativa y retorna True solo si todos los counts coinciden.
    """
    dst_cur = dst.cursor()
    src_cur = src.cursor()

    print()
    print(f"  {'Tabla':<20} {'SQLite':>10} {'PostgreSQL':>12} {'Estado':>8}")
    print(f"  {'-'*20} {'-'*10} {'-'*12} {'-'*8}")

    todo_ok = True
    for tabla in TABLAS_ORDEN:
        src_cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        count_src = src_cur.fetchone()[0]

        dst_cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        count_dst = dst_cur.fetchone()[0]

        ok = count_src == count_dst
        estado = "OK ✓" if ok else "DIFF ✗"
        if not ok:
            todo_ok = False

        print(f"  {tabla:<20} {count_src:>10} {count_dst:>12} {estado:>8}")

    print()
    return todo_ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migra datos de SQLite a PostgreSQL para EncarreraOK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sqlite-path",
        default=os.environ.get("ENCARRERAOK_DB_PATH", "/var/lib/encarreraok/encarreraok.sqlite3"),
        help="Ruta al archivo .sqlite3 (default: $ENCARRERAOK_DB_PATH)",
    )
    parser.add_argument(
        "--pg-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="DSN de PostgreSQL, e.g. postgresql://user:pass@host:5432/db (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra qué haría sin ejecutar ninguna escritura.",
    )
    parser.add_argument(
        "--skip-alembic",
        action="store_true",
        help="Omite el paso 'alembic upgrade head'.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("EncarreraOK — Migración SQLite → PostgreSQL")
    if args.dry_run:
        print("  MODO DRY-RUN: no se realizarán escrituras")
    print("=" * 60)

    # ------------------------------------------------------------------
    # PASO 1: Validar sqlite-path
    # ------------------------------------------------------------------
    print(f"\nPASO 1: Validando archivo SQLite...")
    sqlite_path = args.sqlite_path
    if not sqlite_path:
        print("  ERROR: --sqlite-path no especificado y ENCARRERAOK_DB_PATH no está definida.")
        sys.exit(1)
    if not os.path.isfile(sqlite_path):
        print(f"  ERROR: El archivo SQLite no existe: {sqlite_path}")
        sys.exit(1)
    if not os.access(sqlite_path, os.R_OK):
        print(f"  ERROR: No hay permisos de lectura sobre: {sqlite_path}")
        sys.exit(1)
    print(f"  OK — {sqlite_path}")

    # ------------------------------------------------------------------
    # PASO 2: Validar pg-url y conectar
    # ------------------------------------------------------------------
    print(f"\nPASO 2: Validando conexión a PostgreSQL...")
    pg_url = args.pg_url
    if not pg_url:
        print(
            "  ERROR: --pg-url no especificado y DATABASE_URL no está definida.\n"
            "  Ejemplo: export DATABASE_URL='postgresql://user:pass@host:5432/encarreraok'"
        )
        sys.exit(1)

    if args.dry_run:
        print(f"  [DRY-RUN] Se intentaría conectar a: {pg_url}")
        dst_conn: Optional[object] = None
    else:
        try:
            dst_conn = conectar_postgres(pg_url)
            print(f"  OK — Conectado a PostgreSQL.")
        except RuntimeError as exc:
            print(f"  {exc}")
            sys.exit(1)

    # Conectar SQLite (siempre, para leer conteos y datos)
    src_conn = conectar_sqlite(sqlite_path)

    # ------------------------------------------------------------------
    # PASO 3: alembic upgrade head
    # ------------------------------------------------------------------
    print()
    if args.skip_alembic:
        print("PASO 3: Omitido (--skip-alembic).")
    else:
        ejecutar_alembic(dry_run=args.dry_run)

    # ------------------------------------------------------------------
    # PASO 4: Migrar tablas en orden FK-safe
    # ------------------------------------------------------------------
    print(f"\nPASO 4: Migrando tablas en orden: {' → '.join(TABLAS_ORDEN)}")
    exitos = []
    for tabla in TABLAS_ORDEN:
        ok = migrate_table(src_conn, dst_conn, tabla, dry_run=args.dry_run)
        exitos.append(ok)

    # ------------------------------------------------------------------
    # PASO 5: Verificar counts
    # ------------------------------------------------------------------
    print(f"\nPASO 5: Verificando row counts...")
    if args.dry_run:
        # En dry-run solo mostrar counts de SQLite
        src_cur = src_conn.cursor()
        print(f"\n  {'Tabla':<20} {'SQLite':>10}")
        print(f"  {'-'*20} {'-'*10}")
        for tabla in TABLAS_ORDEN:
            src_cur.execute(f"SELECT COUNT(*) FROM {tabla}")
            count = src_cur.fetchone()[0]
            print(f"  {tabla:<20} {count:>10}")
        print()
        counts_ok = True
    else:
        counts_ok = verify_counts(src_conn, dst_conn)

    # ------------------------------------------------------------------
    # PASO 6: Resumen final
    # ------------------------------------------------------------------
    print("=" * 60)
    migracion_ok = all(exitos) and counts_ok

    if migracion_ok:
        print("RESULTADO: PASS ✓ — Migración completada sin errores.")
    else:
        print("RESULTADO: FAIL ✗ — Hubo errores durante la migración.")
        if not all(exitos):
            print("  - Una o más tablas tuvieron errores de inserción.")
        if not counts_ok:
            print("  - Los conteos de filas no coinciden entre SQLite y PostgreSQL.")
    print("=" * 60)

    src_conn.close()
    if dst_conn is not None:
        dst_conn.close()

    sys.exit(0 if migracion_ok else 1)


if __name__ == "__main__":
    main()
