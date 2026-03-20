# Procedimiento de Cutover: SQLite → PostgreSQL

Este documento describe el procedimiento completo para migrar EncarreraOK de SQLite
a PostgreSQL en producción, con un tiempo de downtime estimado de ~10 minutos.

---

## Prerrequisitos

- [ ] PostgreSQL 14+ instalado y accesible desde el servidor de la aplicación
- [ ] Base de datos destino creada (vacía) y usuario con permisos `CREATE TABLE`, `INSERT`, `SELECT`
- [ ] `psycopg2` disponible en el entorno Python de producción: `pip install psycopg2-binary`
- [ ] El archivo SQLite de producción identificado (normalmente `/var/lib/encarreraok/encarreraok.sqlite3`)
- [ ] Alembic configurado y funcional (`alembic.ini` presente en el directorio del proyecto)
- [ ] Acceso SSH al servidor de producción con permisos para detener/iniciar el servicio

---

## Variables de entorno necesarias

```bash
# Base de datos origen (SQLite — configuración actual)
export ENCARRERAOK_DB_PATH="/var/lib/encarreraok/encarreraok.sqlite3"

# Base de datos destino (PostgreSQL — nueva)
export DATABASE_URL="postgresql://encarreraok_user:CONTRASEÑA@localhost:5432/encarreraok"

# Credencial de admin (sin cambios)
export ADMIN_PASSWORD="tu_contraseña_admin"
```

---

## Preparación (sin downtime)

Estos pasos se realizan **antes** de la ventana de mantenimiento, con el servicio en producción.

### 1. Clonar el repositorio o actualizar el servidor

```bash
cd /opt/encarreraok
git pull origin main
```

### 2. Instalar psycopg2

```bash
pip install psycopg2-binary
```

### 3. Crear la base de datos PostgreSQL

```bash
# En el servidor PostgreSQL:
sudo -u postgres psql <<EOF
CREATE DATABASE encarreraok;
CREATE USER encarreraok_user WITH ENCRYPTED PASSWORD 'CONTRASEÑA';
GRANT ALL PRIVILEGES ON DATABASE encarreraok TO encarreraok_user;
EOF
```

### 4. Verificar conectividad desde el servidor de la app

```bash
export DATABASE_URL="postgresql://encarreraok_user:CONTRASEÑA@localhost:5432/encarreraok"
python3 -c "import psycopg2; c = psycopg2.connect('$DATABASE_URL'); print('OK'); c.close()"
```

### 5. Aplicar el esquema en PostgreSQL (sin datos, sin downtime)

```bash
cd /opt/encarreraok
alembic upgrade head
```

### 6. Hacer un backup del SQLite actual

```bash
cp /var/lib/encarreraok/encarreraok.sqlite3 \
   /var/lib/encarreraok/encarreraok.sqlite3.backup_$(date +%Y%m%d_%H%M%S)
```

---

## Ventana de mantenimiento (~10 minutos)

Durante esta ventana el servicio estará detenido. Ninguna aceptación nueva puede registrarse.

---

## Comandos exactos paso a paso

### Paso 1 — Detener el servicio

```bash
sudo systemctl stop encarreraok
```

### Paso 2 — Confirmar que el servicio está detenido

```bash
sudo systemctl is-active encarreraok
# Debe responder: inactive
```

### Paso 3 — Backup final del SQLite (punto en el tiempo exacto del cutover)

```bash
cp /var/lib/encarreraok/encarreraok.sqlite3 \
   /var/lib/encarreraok/encarreraok.sqlite3.CUTOVER_$(date +%Y%m%d_%H%M%S)
```

### Paso 4 — Ejecutar la migración de datos

```bash
export ENCARRERAOK_DB_PATH="/var/lib/encarreraok/encarreraok.sqlite3"
export DATABASE_URL="postgresql://encarreraok_user:CONTRASEÑA@localhost:5432/encarreraok"

cd /opt/encarreraok
python3 scripts/migrate_sqlite_to_pg.py \
    --sqlite-path "$ENCARRERAOK_DB_PATH" \
    --pg-url "$DATABASE_URL" \
    --skip-alembic
```

El script imprime el progreso por tabla y un resumen PASS/FAIL al finalizar.
Si el resultado es FAIL, **no continuar** — revisar el error y repetir este paso.

### Paso 5 — Verificar la migración

```bash
python3 scripts/verify_migration.py \
    --sqlite-path "$ENCARRERAOK_DB_PATH" \
    --pg-url "$DATABASE_URL"
```

Todas las verificaciones deben mostrar `PASS ✓`. Si alguna muestra `FAIL ✗`,
revisar el error antes de continuar.

### Paso 6 — Configurar el servicio para usar PostgreSQL

Editar el archivo de entorno del servicio (normalmente `/etc/systemd/system/encarreraok.service`
o `/etc/encarreraok.env`) para agregar `DATABASE_URL`:

```bash
# Ejemplo con archivo de entorno:
echo 'DATABASE_URL=postgresql://encarreraok_user:CONTRASEÑA@localhost:5432/encarreraok' \
    | sudo tee -a /etc/encarreraok.env

# Recargar configuración de systemd:
sudo systemctl daemon-reload
```

### Paso 7 — Iniciar el servicio

```bash
sudo systemctl start encarreraok
sudo systemctl is-active encarreraok
# Debe responder: active
```

### Paso 8 — Verificar logs de arranque

```bash
sudo journalctl -u encarreraok -n 50 --no-pager
```

Buscar errores de conexión a base de datos. Si el arranque es limpio, el cutover finalizó.

---

## Verificación post-cutover

Una vez el servicio está activo, verificar que funciona con PostgreSQL:

```bash
# Verificar que la aplicación responde
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/

# Verificar logs en tiempo real por 30 segundos
sudo journalctl -u encarreraok -f --since "1 minute ago"
```

Adicionalmente, realizar una prueba manual:
1. Abrir un evento activo en el navegador
2. Completar una aceptación de prueba
3. Verificar que aparece en el panel de admin

---

## Rollback (volver a SQLite)

Si algo falla después del cutover y se necesita revertir a SQLite:

### Rollback inmediato — reactivar SQLite

```bash
# 1. Detener el servicio
sudo systemctl stop encarreraok

# 2. Eliminar DATABASE_URL del entorno del servicio
# Editar /etc/encarreraok.env y eliminar la línea DATABASE_URL, o:
sudo sed -i '/^DATABASE_URL=/d' /etc/encarreraok.env

# 3. Asegurar que ENCARRERAOK_DB_PATH apunta al SQLite original
grep ENCARRERAOK_DB_PATH /etc/encarreraok.env
# Si no está, agregarlo:
echo 'ENCARRERAOK_DB_PATH=/var/lib/encarreraok/encarreraok.sqlite3' \
    | sudo tee -a /etc/encarreraok.env

# 4. Recargar y reiniciar
sudo systemctl daemon-reload
sudo systemctl start encarreraok
sudo systemctl is-active encarreraok
```

### Verificar rollback

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/
sudo journalctl -u encarreraok -n 20 --no-pager
```

**Nota importante sobre datos post-cutover:** Si entre el inicio del cutover y el rollback
se registraron nuevas aceptaciones en PostgreSQL, esas filas **no** estarán en el SQLite
de respaldo. Para recuperarlas, exportar manualmente las filas nuevas de PostgreSQL e
insertarlas en el SQLite antes de reiniciar con SQLite.

```bash
# Exportar filas nuevas de PG (si aplica):
export DATABASE_URL="postgresql://encarreraok_user:CONTRASEÑA@localhost:5432/encarreraok"
python3 -c "
import psycopg2, json, sys
conn = psycopg2.connect('$DATABASE_URL')
cur = conn.cursor()
cur.execute(\"SELECT * FROM aceptaciones ORDER BY id DESC LIMIT 100\")
print(json.dumps(cur.fetchall()))
conn.close()
"
```

---

## Estimado de tiempo

| Fase                        | Tiempo estimado        |
|-----------------------------|------------------------|
| Preparación (sin downtime)  | 15-30 minutos          |
| Detener servicio            | < 1 minuto             |
| Backup final SQLite         | < 1 minuto             |
| Migración de datos (10k reg)| 2-3 minutos            |
| Verificación post-migración | 1-2 minutos            |
| Configurar y reiniciar      | 1-2 minutos            |
| **Downtime total**          | **~5-8 minutos**       |

Para bases de datos con más de 50.000 registros, la migración puede tardar 10-15 minutos adicionales.
