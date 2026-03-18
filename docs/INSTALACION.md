# Guía de instalación — EncarreraOK

Instalación limpia desde cero en un servidor Ubuntu 22.04 / 24.04 con systemd y Nginx.

---

## Requisitos del sistema

| Componente | Versión mínima  | Notas                              |
|------------|-----------------|------------------------------------|
| Python     | 3.10+           | Verificar con `python3 --version`  |
| pip        | Incluido        | `python3 -m pip --version`         |
| git        | Cualquier       |                                    |
| Nginx      | 1.18+           | Reverse proxy                      |
| systemd    | Incluido Ubuntu | Gestión del proceso                |
| OS         | Ubuntu 22.04 / 24.04 LTS | Recomendado            |

---

## Variables de entorno

### Obligatorias

| Variable         | Descripción                                         | Default       |
|------------------|-----------------------------------------------------|---------------|
| `ADMIN_PASSWORD` | Contraseña del panel de administración. **No tiene valor por defecto.** La aplicación falla al arrancar si está vacía. | — |

### Opcionales

| Variable               | Descripción                                      | Default                                        |
|------------------------|--------------------------------------------------|------------------------------------------------|
| `ADMIN_USER`           | Usuario del panel de administración              | `admin`                                        |
| `ENCARRERAOK_DB_PATH`  | Ruta absoluta al archivo SQLite                  | `/var/lib/encarreraok/encarreraok.sqlite3`     |
| `ENCARRERAOK_LEGAL_DIR`| Ruta al directorio con textos de deslinde        | `legal` (relativa al directorio del proyecto)  |
| `DATABASE_URL`         | URL de conexión PostgreSQL (`postgresql://...`). Solo relevante al migrar a PG. Por ahora la aplicación usa SQLite. | — |

---

## Instalación paso a paso

### 1. Clonar el repositorio

```bash
sudo mkdir -p /opt/encarreraok
sudo chown $USER:$USER /opt/encarreraok
git clone https://github.com/tu-org/encarreraok.git /opt/encarreraok
cd /opt/encarreraok
```

### 2. Crear el entorno virtual e instalar dependencias

```bash
python3 -m venv /opt/encarreraok/venv
source /opt/encarreraok/venv/bin/activate
pip install --upgrade pip
pip install -r /opt/encarreraok/requirements.txt
```

### 3. Crear directorios de datos y evidencias

La aplicación almacena la base de datos y los archivos subidos (firmas, documentos, audios, documentos de salud) bajo `/var/lib/encarreraok/`.

```bash
sudo mkdir -p /var/lib/encarreraok/evidencias/firmas
sudo mkdir -p /var/lib/encarreraok/evidencias/documentos
sudo mkdir -p /var/lib/encarreraok/evidencias/audios
sudo mkdir -p /var/lib/encarreraok/evidencias/salud
sudo chown -R www-data:www-data /var/lib/encarreraok
sudo chmod -R 750 /var/lib/encarreraok
```

> Si el servicio systemd corre como un usuario distinto de `www-data`, ajustar `chown` al usuario correspondiente.

### 4. Crear el directorio de logs

```bash
sudo mkdir -p /var/log/encarreraok
sudo chown www-data:www-data /var/log/encarreraok
```

### 5. Configurar el servicio systemd

Crear el archivo de servicio:

```bash
sudo nano /etc/systemd/system/encarreraok.service
```

Pegar el contenido de la sección [Archivo de servicio systemd](#archivo-de-servicio-systemd) más abajo, ajustando la contraseña.

```bash
sudo systemctl daemon-reload
sudo systemctl enable encarreraok
```

### 6. Inicializar el esquema de base de datos

La base de datos SQLite se crea automáticamente al arrancar la aplicación. El esquema se aplica mediante las migraciones internas de `init_db()`.

Opcionalmente, si se desea usar Alembic para gestión de esquema:

```bash
cd /opt/encarreraok
source venv/bin/activate
export ADMIN_PASSWORD="tu_contraseña_aqui"
export ENCARRERAOK_DB_PATH="/var/lib/encarreraok/encarreraok.sqlite3"
alembic upgrade head
```

> En instalaciones nuevas, Alembic crea todas las tablas desde cero. En instalaciones existentes aplica solo las migraciones pendientes.

### 7. Configurar Nginx

Crear el archivo de configuración de sitio:

```bash
sudo nano /etc/nginx/sites-available/encarreraok
```

Pegar el contenido de la sección [Configuración Nginx](#configuración-nginx) más abajo.

```bash
sudo ln -s /etc/nginx/sites-available/encarreraok /etc/nginx/sites-enabled/encarreraok
sudo nginx -t
sudo systemctl reload nginx
```

### 8. Arrancar el servicio

```bash
sudo systemctl start encarreraok
sudo systemctl status encarreraok
```

### 9. Verificar la instalación

```bash
# El formulario público debe responder 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/e/1

# El panel de administración debe responder 200 con credenciales correctas
curl -s -o /dev/null -w "%{http_code}" -u admin:TU_PASSWORD http://localhost:8000/admin/eventos
```

Ambos comandos deben imprimir `200`.

---

## Configuración Nginx

Bloque de configuración completo para `/etc/nginx/sites-available/encarreraok`:

```nginx
server {
    listen 80;
    server_name tu-dominio.com;

    # Tamaño máximo de uploads (firmas, documentos, audios)
    client_max_body_size 10M;

    # Archivos estáticos servidos directamente por Nginx
    location /assets/ {
        alias /opt/encarreraok/assets/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # Todo el resto va a uvicorn
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;

        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
    }
}
```

> Para HTTPS, configurar certificado SSL (Let's Encrypt recomendado con `certbot --nginx`).

---

## Archivo de servicio systemd

Contenido completo para `/etc/systemd/system/encarreraok.service`:

```ini
[Unit]
Description=EncarreraOK - MVP deslindes digitales
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/encarreraok

# Variables de entorno
Environment="ADMIN_PASSWORD=CAMBIAR_ESTA_CONTRASEÑA"
Environment="ADMIN_USER=admin"
Environment="ENCARRERAOK_DB_PATH=/var/lib/encarreraok/encarreraok.sqlite3"
Environment="ENCARRERAOK_LEGAL_DIR=/opt/encarreraok/legal"

# Comando de arranque
ExecStart=/opt/encarreraok/venv/bin/uvicorn main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --log-level warning

# Política de reinicio
Restart=on-failure
RestartSec=5s

# Límites
TimeoutStartSec=30
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
```

**Importante:** Reemplazar `CAMBIAR_ESTA_CONTRASEÑA` por una contraseña segura antes de iniciar el servicio.

---

## Verificación post-instalación

### Verificar que el servicio está activo

```bash
sudo systemctl is-active encarreraok
# Debe responder: active
```

### Verificar logs de arranque

```bash
sudo journalctl -u encarreraok -n 50 --no-pager
```

### Verificar endpoints

```bash
# Formulario público del primer evento (devuelve HTML del formulario de aceptación)
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/e/1

# Panel de administración con credenciales correctas
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -u admin:TU_PASSWORD \
  http://localhost:8000/admin/eventos

# Sin credenciales debe devolver 401
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:8000/admin/eventos
```

Resultado esperado:

```
HTTP 200   ← /e/1
HTTP 200   ← /admin/eventos con credenciales
HTTP 401   ← /admin/eventos sin credenciales
```

### Verificar directorio de evidencias

```bash
ls -la /var/lib/encarreraok/evidencias/
# Deben existir: firmas/  documentos/  audios/  salud/
```

### Verificar logs de la aplicación

```bash
tail -f /var/log/encarreraok/app.log
```

---

## Notas adicionales

- **SQLite por defecto:** La aplicación usa SQLite en producción. No requiere servidor de base de datos externo.
- **PostgreSQL:** Los scripts de migración están disponibles en `scripts/` pero la migración a PostgreSQL no está activada. Ver `docs/CUTOVER.md` para el procedimiento cuando se decida migrar.
- **Workers:** Se recomienda `--workers 1` con SQLite para evitar conflictos de escritura concurrente.
- **Backups:** Programar backup diario del archivo SQLite: `cp /var/lib/encarreraok/encarreraok.sqlite3 /backups/encarreraok_$(date +%Y%m%d).sqlite3`
