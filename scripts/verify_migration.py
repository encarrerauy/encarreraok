#!/usr/bin/env python3
"""
verify_migration.py — Verificaciones post-migración SQLite → PostgreSQL para EncarreraOK.

Compara counts, integridad referencial, unicidad y ausencia de NULLs en campos críticos.
Exit code 0 si todas las verificaciones pasan, 1 si alguna falla (apto para CI).

Uso:
    python3 scripts/verify_migration.py \\
        --sqlite-path /var/lib/encarreraok/encarreraok.sqlite3 \\
        --pg-url "postgresql://user:pass@host:5432/encarreraok"
"""

import argparse
import os
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Tablas del proyecto
# ---------------------------------------------------------------------------
TABLAS = ["eventos", "deslindes", "aceptaciones"]

# ---------------------------------------------------------------------------
# Helpers de conexión
# ---------------------------------------------------------------------------

def conectar_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def conectar_postgres(pg_url: str):
    try:
        import psycopg2  # type: ignore
    except ImportError:
        print("ERROR: psycopg2 no instalado. Ejecuta: pip install psycopg2-binary")
        sys.exit(1)
    try:
        return psycopg2.connect(pg_url)
    except Exception as exc:
        print(f"ERROR: No se pudo conectar a PostgreSQL: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Runner de verificaciones
# ---------------------------------------------------------------------------

class Verificador:
    def __init__(self, src: sqlite3.Connection, dst):
        self.src = src
        self.dst = dst
        self.resultados: list[tuple[str, bool, str]] = []

    def verificar(self, descripcion: str, ok: bool, detalle: str = "") -> bool:
        self.resultados.append((descripcion, ok, detalle))
        marca = "PASS ✓" if ok else "FAIL ✗"
        linea = f"  [{marca}] {descripcion}"
        if detalle:
            linea += f" — {detalle}"
        print(linea)
        return ok

    def resumen(self) -> bool:
        total = len(self.resultados)
        pasaron = sum(1 for _, ok, _ in self.resultados if ok)
        fallaron = total - pasaron
        print()
        print("=" * 60)
        print(f"RESUMEN: {pasaron}/{total} verificaciones pasaron")
        if fallaron:
            print(f"         {fallaron} verificación(es) FALLARON ✗")
        else:
            print("         Todas las verificaciones PASARON ✓")
        print("=" * 60)
        return fallaron == 0


# ---------------------------------------------------------------------------
# Verificación 1: Row counts por tabla
# ---------------------------------------------------------------------------

def verificar_counts(v: Verificador) -> None:
    print("\n--- 1. Row counts por tabla ---")
    src_cur = v.src.cursor()
    dst_cur = v.dst.cursor()

    for tabla in TABLAS:
        src_cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        count_src = src_cur.fetchone()[0]

        dst_cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        count_dst = dst_cur.fetchone()[0]

        ok = count_src == count_dst
        detalle = f"SQLite={count_src}, PostgreSQL={count_dst}"
        v.verificar(f"counts {tabla}", ok, detalle)


# ---------------------------------------------------------------------------
# Verificación 2: Integridad referencial aceptaciones → eventos
# ---------------------------------------------------------------------------

def verificar_fk_aceptaciones(v: Verificador) -> None:
    print("\n--- 2. Integridad referencial: aceptaciones → eventos ---")
    dst_cur = v.dst.cursor()
    dst_cur.execute(
        """
        SELECT COUNT(*)
        FROM aceptaciones a
        WHERE NOT EXISTS (
            SELECT 1 FROM eventos e WHERE e.id = a.evento_id
        )
        """
    )
    huerfanos = dst_cur.fetchone()[0]
    ok = huerfanos == 0
    detalle = f"{huerfanos} aceptacion(es) sin evento padre" if not ok else "todas tienen evento padre"
    v.verificar("aceptaciones.evento_id → eventos.id", ok, detalle)


# ---------------------------------------------------------------------------
# Verificación 3: Integridad referencial deslindes → eventos
# ---------------------------------------------------------------------------

def verificar_fk_deslindes(v: Verificador) -> None:
    print("\n--- 3. Integridad referencial: deslindes → eventos ---")
    dst_cur = v.dst.cursor()
    dst_cur.execute(
        """
        SELECT COUNT(*)
        FROM deslindes d
        WHERE NOT EXISTS (
            SELECT 1 FROM eventos e WHERE e.id = d.evento_id
        )
        """
    )
    huerfanos = dst_cur.fetchone()[0]
    ok = huerfanos == 0
    detalle = f"{huerfanos} deslinde(s) sin evento padre" if not ok else "todos tienen evento padre"
    v.verificar("deslindes.evento_id → eventos.id", ok, detalle)


# ---------------------------------------------------------------------------
# Verificación 4: Sin pdf_token duplicados en aceptaciones
# ---------------------------------------------------------------------------

def verificar_pdf_tokens_unicos(v: Verificador) -> None:
    print("\n--- 4. Unicidad de pdf_token en aceptaciones ---")
    dst_cur = v.dst.cursor()
    dst_cur.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT pdf_token
            FROM aceptaciones
            WHERE pdf_token IS NOT NULL
            GROUP BY pdf_token
            HAVING COUNT(*) > 1
        ) dup
        """
    )
    duplicados = dst_cur.fetchone()[0]
    ok = duplicados == 0
    detalle = f"{duplicados} pdf_token(s) duplicado(s)" if not ok else "sin duplicados"
    v.verificar("pdf_token únicos en aceptaciones", ok, detalle)


# ---------------------------------------------------------------------------
# Verificación 5: Sin NULLs en campos críticos de aceptaciones
# ---------------------------------------------------------------------------

CAMPOS_CRITICOS = [
    "evento_id",
    "nombre_participante",
    "documento",
    "fecha_hora",
]


def verificar_nulls_criticos(v: Verificador) -> None:
    print("\n--- 5. Sin NULLs en campos críticos de aceptaciones ---")
    dst_cur = v.dst.cursor()
    for campo in CAMPOS_CRITICOS:
        dst_cur.execute(
            f"SELECT COUNT(*) FROM aceptaciones WHERE {campo} IS NULL"
        )
        nulls = dst_cur.fetchone()[0]
        ok = nulls == 0
        detalle = f"{nulls} fila(s) con NULL" if not ok else "sin NULLs"
        v.verificar(f"aceptaciones.{campo} sin NULLs", ok, detalle)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verifica la integridad de la migración SQLite → PostgreSQL para EncarreraOK.",
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
        help="DSN de PostgreSQL (default: $DATABASE_URL)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("EncarreraOK — Verificación post-migración SQLite → PostgreSQL")
    print("=" * 60)

    # Validar archivos
    if not args.sqlite_path or not os.path.isfile(args.sqlite_path):
        print(f"ERROR: Archivo SQLite no encontrado: {args.sqlite_path}")
        sys.exit(1)
    if not args.pg_url:
        print("ERROR: --pg-url no especificado y DATABASE_URL no está definida.")
        sys.exit(1)

    src_conn = conectar_sqlite(args.sqlite_path)
    dst_conn = conectar_postgres(args.pg_url)

    v = Verificador(src_conn, dst_conn)

    verificar_counts(v)
    verificar_fk_aceptaciones(v)
    verificar_fk_deslindes(v)
    verificar_pdf_tokens_unicos(v)
    verificar_nulls_criticos(v)

    src_conn.close()
    dst_conn.close()

    todo_ok = v.resumen()
    sys.exit(0 if todo_ok else 1)


if __name__ == "__main__":
    main()
