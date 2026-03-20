# Cambios de refactorización — Branch `claude/eloquent-goodall`

Documento técnico de todos los cambios realizados en este branch respecto a `main` (snapshot de producción del 2026-03-17).

---

## Resumen ejecutivo

| | Antes (main) | Después (este branch) |
|---|---|---|
| `main.py` | 5.243 líneas — lógica, templates, rutas, config y esquema todo junto | 462 líneas — orquestador puro |
| Templates HTML | 10 templates inline en `DictLoader` dentro de `main.py` | 10 archivos `.html` en `app/templates/` |
| Configuración | Variables de entorno leídas con `os.environ` en múltiples lugares | `app/config.py` con dataclass `Settings` centralizada |
| Autenticación | Lógica HTTP Basic inline en cada ruta admin | `app/middleware/auth.py` como dependencia reutilizable |
| Rutas | Todas en `main.py` | `app/routers/public.py` (3 rutas) + `app/routers/admin.py` (19 rutas) |
| Generación de PDF | Inline en rutas | `app/pdf_generator.py` compartido |
| Migraciones de esquema | Solo `init_db()` en `main.py` con `ALTER TABLE` manuales | `init_db()` conservado + Alembic disponible como alternativa |
| Scripts de migración | No existían | `scripts/migrate_sqlite_to_pg.py` + `scripts/verify_migration.py` |

**Qué NO cambió:**
- Lógica de negocio (cálculo de hashes, generación de PDF, procesamiento de evidencias)
- URLs públicas y de administración
- Base de datos SQLite en producción
- Comportamiento observable por el usuario final
- Datos existentes en producción

---

## Cambios por commit

### Commit 1 — Extracción de templates HTML

**Hash:** `4c9f483`
**Mensaje:** `REFACTOR: Extract Jinja2 templates from DictLoader to FileSystemLoader`

**Qué era:**
`main.py` contenía los 10 templates HTML como strings literales dentro de un `DictLoader` de Jinja2, ocupando 1.808 líneas del archivo.

**Qué es ahora:**
Un `FileSystemLoader` apunta a `app/templates/`. Cada template es un archivo `.html` independiente y editable.

Archivos creados:

| Archivo | Líneas |
|---|---|
| `app/templates/evento_form.html` | 687 |
| `app/templates/admin_preview.html` | 221 |
| `app/templates/admin_aceptacion_detalle.html` | 197 |
| `app/templates/admin_monitor_evento.html` | 144 |
| `app/templates/admin_aceptaciones.html` | 135 |
| `app/templates/admin_eventos_form.html` | 101 |
| `app/templates/admin_eventos_lista.html` | 91 |
| `app/templates/admin_busqueda_deslindes.html` | 82 |
| `app/templates/admin_gestion_eliminacion.html` | 76 |
| `app/templates/confirmacion.html` | 42 |

`main.py`: 5.243 → 3.439 líneas (−1.808 líneas).

**Impacto en comportamiento:** Ninguno. Los templates son idénticos al contenido que estaba inline.

---

### Commit 2 — Config centralizada y Auth middleware

**Hash:** `ba87941`
**Mensaje:** `REFACTOR: Extract config and auth middleware to dedicated modules`

**Qué era:**
- Las variables de entorno (`ADMIN_USER`, `ADMIN_PASSWORD`, rutas de BD) se leían con `os.environ.get()` en distintos puntos del código.
- La contraseña de admin tenía un valor por defecto hardcodeado (`"encarrera2025"`).
- La lógica de autenticación HTTP Basic estaba duplicada o mezclada con el código de rutas.

**Qué es ahora:**

`app/config.py`:
```python
@dataclass(frozen=True)
class Settings:
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    db_path: str = os.environ.get("ENCARRERAOK_DB_PATH", "/var/lib/encarreraok/encarreraok.sqlite3")
    legal_dir: str = os.environ.get("ENCARRERAOK_LEGAL_DIR", "legal")

    def __post_init__(self):
        if not self.admin_password:
            raise ValueError("ADMIN_PASSWORD es obligatoria y no puede estar vacía.")
```

`app/middleware/auth.py`:
- `HTTPBasic` + `secrets.compare_digest` para evitar timing attacks.
- Importa de `settings`, no lee `os.environ` directamente.

**Cambio de comportamiento importante:**
`ADMIN_PASSWORD` ya no tiene valor por defecto. Si la variable no está definida al arrancar, la aplicación lanza `ValueError` y no inicia. Esto previene despliegues accidentales sin contraseña.

`main.py`: 3.439 → 3.417 líneas (−22 líneas).

---

### Commit 3 — Alembic

**Hash:** `289cd1b`
**Mensaje:** `INFRA: Add Alembic for versioned schema migrations`

**Qué era:**
El esquema de base de datos se creaba y migraba completamente dentro de `init_db()` y `ensure_schema_migrations()` en `main.py`, con `ALTER TABLE` manuales envueltos en bloques `try/except`.

**Qué es ahora:**
Alembic coexiste con el mecanismo existente. El `init_db()` original se conserva para compatibilidad hacia atrás. Alembic está disponible como alternativa para instalaciones nuevas y para la futura migración a PostgreSQL.

Archivos creados:

| Archivo | Descripción |
|---|---|
| `alembic.ini` | Configuración de Alembic |
| `alembic/env.py` | Resuelve `DATABASE_URL` (PostgreSQL) o `ENCARRERAOK_DB_PATH` (SQLite fallback) |
| `alembic/versions/001_initial_schema.py` | Esquema completo baseline: tablas `eventos`, `deslindes`, `aceptaciones` e índices |
| `alembic/versions/002_incremental_migrations.py` | Todos los `ALTER TABLE ADD COLUMN` históricos extraídos de `ensure_schema_migrations()` |

**Cómo usar Alembic:**

```bash
# Aplicar todas las migraciones pendientes
alembic upgrade head

# Ver estado actual
alembic current

# Ver historial
alembic history

# Revertir una migración
alembic downgrade -1

# Revertir todo
alembic downgrade base
```

**Nota:** `alembic upgrade head` en una base de datos nueva crea el esquema completo. En una base de datos existente de producción, aplica solo las migraciones que aún no se han ejecutado, según la tabla `alembic_version`.

`requirements.txt`: se agregó `alembic>=1.13.0`.

---

### Commit 4 — Scripts de migración SQLite → PostgreSQL

**Hash:** `cc55481`
**Mensaje:** `INFRA: Add SQLite-to-PostgreSQL migration scripts and cutover docs`

**Estado:** Los scripts están listos pero **no se han ejecutado en producción**. La aplicación sigue usando SQLite.

Archivos creados:

| Archivo | Descripción |
|---|---|
| `scripts/migrate_sqlite_to_pg.py` | Migra datos en orden FK-safe: `eventos → deslindes → aceptaciones`. Batch inserts de 100 filas. Idempotente (`ON CONFLICT DO NOTHING`). |
| `scripts/verify_migration.py` | Verifica integridad post-migración: conteo de filas, FK, tokens duplicados, nulos en campos críticos. Exit code 1 si hay fallos. |
| `docs/CUTOVER.md` | Procedimiento completo de cutover a producción (~10 min de downtime estimado) con rollback incluido. |

Flags de `migrate_sqlite_to_pg.py`:
- `--sqlite-path` — ruta al archivo SQLite origen
- `--pg-url` — URL de conexión PostgreSQL destino
- `--dry-run` — simulación sin escrituras
- `--skip-alembic` — omite `alembic upgrade head` si el esquema ya está creado

---

### Commit 5 — Extracción de routers

**Hash:** `ddad28b`
**Mensaje:** `REFACTOR: Extract all route handlers into dedicated router modules`

**Qué era:**
Las 22 funciones de ruta estaban definidas directamente en `main.py` junto con la lógica de inicio, esquema de BD y configuración.

**Qué es ahora:**

`app/routers/public.py` — 3 rutas:
- `GET /e/{evento_id}` — formulario de aceptación
- `POST /e/{evento_id}` — procesamiento de aceptación (firma, documentos, audio)
- `GET /aceptacion/pdf/{token}` — descarga pública de PDF con token temporal

`app/routers/admin.py` — 19 rutas:
- Dashboard y búsqueda global
- CRUD de eventos
- Listado, detalle y exportación ZIP de aceptaciones
- Monitor en vivo de evento
- Preview de evidencias
- Revocación de tokens PDF
- Gestión de eliminación de datos

`app/pdf_generator.py`:
- Clases `TTFFont` y `SimplePDFGenerator`
- Funciones `_generar_bytes_pdf`, `cargar_deslinde`, `calcular_hash_sha256`
- Compartido entre el router público y el router de admin

`app/templates_config.py`:
- `templates_env` con `FileSystemLoader` apuntando a `app/templates/`
- Filtro `fecha_ddmmaaaa` registrado una sola vez

`main.py`: 3.417 → 604 líneas (−82%).

**Impacto en comportamiento:** Ninguno. Extracción estructural pura. Las URLs, la lógica y los datos no cambiaron.

---

### Commit 6 — Limpieza de main.py

**Hash:** `aaf8efe`
**Mensaje:** `REFACTOR: Clean up main.py to lean orchestrator (604 → 462 lines)`

**Qué se eliminó:**

Imports huérfanos (ya no usados tras la extracción de routers):
- `pathlib.Path`
- `pydantic.BaseModel`
- `datetime.datetime`
- `hashlib`
- `typing.Optional`, `List`, `Dict`, `Any`

Código muerto:
- Constante `DEFAULT_DB_PATH` (reemplazada por `settings.db_path`)
- Modelos Pydantic `Evento` y `Aceptacion` (nunca usados por los routers)
- Bloque de comentarios `PLAN DE PRUEBAS MANUALES` (~90 líneas)
- Bloque `BACKLOG DE SEGURIDAD` como comentario

**Lo que se conservó:**
- `setup_logging()` — configura RotatingFileHandler en `/var/log/encarreraok/app.log`
- `ensure_storage()` — crea directorios de evidencias al arrancar
- `ensure_schema_migrations()` — migraciones `ALTER TABLE` históricas
- `init_db()` — inicialización del esquema SQLite
- `app = FastAPI(...)` + montaje de estáticos + `include_router`
- Hook `@app.on_event("startup")` — llama `init_db()` y crea evento de ejemplo si la BD está vacía

`main.py`: 604 → 462 líneas.

---

### Fix adicional — Bug en `init_db()`

**Commit:** incluido en `aaf8efe`

**Problema:**
`ensure_schema_migrations()` se llamaba al principio de `init_db()`, antes de los `CREATE TABLE IF NOT EXISTS`. En una instalación nueva (base de datos vacía), `ensure_schema_migrations()` intentaba hacer `PRAGMA table_info(aceptaciones)` sobre una tabla que aún no existía.

**Consecuencia:**
En instalaciones completamente nuevas, las columnas que se agregan en `ensure_schema_migrations()` (`valido`, `deslinde_version`) no se creaban, porque el `CREATE TABLE` posterior sí las incluía en su definición inicial pero el `ALTER TABLE` ya había fallado silenciosamente y el flujo continuaba.

**Solución:**
Mover la llamada a `ensure_schema_migrations(conn)` al final de `init_db()`, después de todos los `CREATE TABLE IF NOT EXISTS` y `ALTER TABLE` de inicialización.

---

## Estructura de archivos antes vs después

### Antes (main, snapshot `f7bd047`)

```
/opt/encarreraok/
├── main.py                    ← 5.243 líneas (TODO en un solo archivo)
├── requirements.txt
├── assets/
├── legal/
└── (sin app/, sin scripts/, sin alembic/, sin docs/)
```

### Después (este branch, `aaf8efe`)

```
/opt/encarreraok/
├── main.py                          ← 462 líneas (orquestador)
├── requirements.txt
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 001_initial_schema.py
│       └── 002_incremental_migrations.py
├── app/
│   ├── config.py                    ← Settings dataclass
│   ├── pdf_generator.py             ← Generación de PDF
│   ├── templates_config.py          ← Jinja2 FileSystemLoader
│   ├── middleware/
│   │   └── auth.py                  ← HTTP Basic auth
│   ├── routers/
│   │   ├── public.py                ← 3 rutas públicas
│   │   └── admin.py                 ← 19 rutas admin
│   └── templates/
│       ├── evento_form.html
│       ├── confirmacion.html
│       ├── admin_aceptaciones.html
│       ├── admin_aceptacion_detalle.html
│       ├── admin_eventos_lista.html
│       ├── admin_eventos_form.html
│       ├── admin_monitor_evento.html
│       ├── admin_preview.html
│       ├── admin_busqueda_deslindes.html
│       └── admin_gestion_eliminacion.html
├── scripts/
│   ├── migrate_sqlite_to_pg.py
│   └── verify_migration.py
├── docs/
│   └── CUTOVER.md
├── assets/
└── legal/
```

---

## Qué NO se hizo (pendiente)

| Tarea | Estado | Notas |
|---|---|---|
| Migración a PostgreSQL | Scripts listos, no ejecutados | Ver `docs/CUTOVER.md` |
| Tests automatizados | No existen | No hay pytest ni ningún framework de testing |
| Rate limiting | No implementado | Pendiente para producción con tráfico alto |
| CSRF protection | No implementado | El formulario público no tiene token CSRF |
| HTTPS en Nginx | Configuración manual requerida | Usar `certbot --nginx` post-instalación |

---

## Cómo hacer rollback

Si algo falla en producción después de deployar este branch:

```bash
# 1. Detener el servicio
sudo systemctl stop encarreraok

# 2. Volver al código anterior
cd /opt/encarreraok
git checkout main

# 3. Reinstalar dependencias (por si cambiaron)
source venv/bin/activate
pip install -r requirements.txt

# 4. Reiniciar el servicio
sudo systemctl start encarreraok
sudo systemctl is-active encarreraok
```

**Nota sobre la base de datos:** La base de datos SQLite no necesita rollback. El esquema es compatible hacia atrás — las columnas nuevas que pueda haber agregado Alembic tienen valores `DEFAULT` y no rompen el código anterior.

**Nota sobre `ADMIN_PASSWORD`:** Con el código anterior (`main`), la variable `ADMIN_PASSWORD` tenía un valor por defecto hardcodeado. Con este branch es obligatoria. Al hacer rollback, si `ADMIN_PASSWORD` no estaba en el entorno del servicio, agregarla antes de reiniciar para evitar que la aplicación no arranque (aunque con el código antiguo sí arrancaba sin ella, usando el default inseguro).
