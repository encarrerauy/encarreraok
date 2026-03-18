# Checklist pre-producción — EncarreraOK

Lista de verificación antes de deployar el branch `claude/eloquent-goodall` a producción.
Completar todos los ítems antes de hacer el merge y el deploy.

---

## Validación funcional (manual)

Probar con la aplicación corriendo localmente o en staging con datos reales.

### Flujo público

- [ ] `GET /e/{evento_id}` carga el formulario de aceptación con el texto de deslinde correcto
- [ ] `POST /e/{evento_id}` con firma digital → crea registro en `aceptaciones` → muestra página de confirmación con token PDF
- [ ] `POST /e/{evento_id}` con documento adjunto (frente y dorso) → archivos guardados en `evidencias/documentos/`
- [ ] `POST /e/{evento_id}` con audio → archivo guardado en `evidencias/audios/`
- [ ] `GET /aceptacion/pdf/{token}` con token válido → descarga PDF con datos correctos del participante
- [ ] `GET /aceptacion/pdf/{token}` con token revocado → responde `403`
- [ ] `GET /aceptacion/pdf/{token}` con token inválido o inexistente → responde `404`

### Panel de administración

- [ ] `GET /admin/eventos` → lista eventos (requiere credenciales válidas)
- [ ] `GET /admin/eventos` sin credenciales → responde `401`
- [ ] `GET /admin/evento/{id}` → muestra detalle del evento
- [ ] `POST /admin/evento/nuevo` → crea evento nuevo
- [ ] `POST /admin/evento/{id}/editar` → modifica evento existente
- [ ] `GET /admin/aceptaciones` → lista aceptaciones con filtros
- [ ] `GET /admin/aceptacion/{id}` → muestra detalle de aceptación con evidencias
- [ ] `GET /admin/exportar_zip/{evento_id}` → descarga ZIP con PDF y evidencias del evento
- [ ] `GET /admin/evento/{id}/monitor` → monitor en vivo carga y refresca correctamente
- [ ] `POST /admin/aceptaciones/{id}/revocar_token` → revoca token PDF (verificar que `GET /aceptacion/pdf/{token}` devuelve `403` después)
- [ ] `GET /admin/buscar_deslindes` → busca aceptaciones por número de documento

---

## Validación de seguridad

- [ ] `ADMIN_PASSWORD` no es `"encarrera2025"` ni ninguna contraseña obvia
- [ ] `ADMIN_PASSWORD` no está vacía (la aplicación lanza `ValueError` y no inicia si está vacía — verificar en logs)
- [ ] `ADMIN_PASSWORD` no está hardcodeada en ningún archivo del repositorio
- [ ] Variables de entorno están en `/etc/systemd/system/encarreraok.service`, no en el código fuente
- [ ] No hay archivos `.env`, `.backup*`, `*.sqlite3`, o `*.prod_estable_*` en el repositorio
- [ ] HTTPS configurado en Nginx (certificado SSL válido)
- [ ] El panel de administración no es accesible sin autenticación (verificar con `curl` sin credenciales)
- [ ] Los archivos de evidencias (`/var/lib/encarreraok/evidencias/`) no son accesibles directamente vía HTTP (no hay `location /var/lib/` en Nginx)

---

## Validación de infraestructura

- [ ] Directorios de evidencias existen y tienen permisos correctos:
  - `/var/lib/encarreraok/evidencias/firmas/`
  - `/var/lib/encarreraok/evidencias/documentos/`
  - `/var/lib/encarreraok/evidencias/audios/`
  - `/var/lib/encarreraok/evidencias/salud/`
- [ ] El usuario del servicio systemd tiene permisos de escritura en `/var/lib/encarreraok/`
- [ ] El directorio de logs existe: `/var/log/encarreraok/` con permisos para el usuario del servicio
- [ ] Log rotation configurado (la aplicación usa `RotatingFileHandler` con 10 MB / 5 backups; verificar que no hay conflicto con `logrotate` del sistema)
- [ ] Backup de BD programado (cron o script que copie el `.sqlite3` diariamente)
- [ ] Nginx configurado con `client_max_body_size 10M` (requerido para uploads de evidencias hasta 5 MB por audio + 4 MB por imagen de documento)
- [ ] `nginx -t` pasa sin errores antes de recargar
- [ ] El servicio systemd está habilitado para arrancar automáticamente: `systemctl is-enabled encarreraok` → `enabled`
- [ ] `uvicorn` corre con `--workers 1` (SQLite no soporta escrituras concurrentes desde múltiples procesos)

---

## Comandos de verificación

Ejecutar estos comandos desde el servidor de producción una vez deployado.

### Estado del servicio

```bash
# El servicio debe estar activo
sudo systemctl is-active encarreraok

# Verificar que no hay errores en los últimos logs de arranque
sudo journalctl -u encarreraok -n 30 --no-pager
```

### Endpoints públicos

```bash
# Formulario del evento 1 (debe devolver 200 con HTML)
curl -s -o /dev/null -w "GET /e/1 → HTTP %{http_code}\n" http://localhost:8000/e/1

# Si el evento 1 no existe, crear uno desde el admin primero.
# En instalación nueva se crea automáticamente al arrancar.
```

### Endpoints de administración

```bash
# Con credenciales correctas — debe devolver 200
curl -s -o /dev/null -w "GET /admin/eventos (auth OK) → HTTP %{http_code}\n" \
  -u admin:TU_PASSWORD \
  http://localhost:8000/admin/eventos

# Sin credenciales — debe devolver 401
curl -s -o /dev/null -w "GET /admin/eventos (sin auth) → HTTP %{http_code}\n" \
  http://localhost:8000/admin/eventos

# Con credenciales incorrectas — debe devolver 401
curl -s -o /dev/null -w "GET /admin/eventos (auth incorrecta) → HTTP %{http_code}\n" \
  -u admin:contraseña_incorrecta \
  http://localhost:8000/admin/eventos
```

### Verificar que Nginx pasa los headers correctos

```bash
# Verificar headers de proxy
curl -s -I http://localhost:8000/e/1 | head -10
```

### Verificar directorios de datos

```bash
# Todos los directorios deben existir
ls -la /var/lib/encarreraok/evidencias/

# La BD debe existir (o crearse al primer arranque)
ls -lh /var/lib/encarreraok/encarreraok.sqlite3
```

### Verificar integridad de la BD (opcional)

```bash
# Verificar que SQLite no reporta errores
sqlite3 /var/lib/encarreraok/encarreraok.sqlite3 "PRAGMA integrity_check;"
# Debe responder: ok

# Ver tablas existentes
sqlite3 /var/lib/encarreraok/encarreraok.sqlite3 ".tables"
# Debe mostrar: aceptaciones  deslindes  eventos
```

### Verificar tamaño de uploads en Nginx

```bash
# Verificar client_max_body_size en la configuración activa
grep client_max_body_size /etc/nginx/sites-enabled/encarreraok
# Debe mostrar: client_max_body_size 10M;
```

### Test de carga de archivo (simulación)

```bash
# Generar un archivo de prueba de 5 MB
dd if=/dev/urandom of=/tmp/test_5mb.bin bs=1M count=5 2>/dev/null

# Intentar un POST con archivo (debe llegar al endpoint, no ser rechazado por Nginx)
# Nota: el endpoint /e/{id} rechazará el tipo de archivo, pero la respuesta
# NO debe ser 413 (Request Entity Too Large) — eso sería un error de Nginx.
curl -s -o /dev/null -w "HTTP %{http_code}\n" \
  -F "nombre=Test" \
  -F "documento=12345678" \
  -F "firma_data=data:image/png;base64,iVBORw0KGgo=" \
  -F "audio=@/tmp/test_5mb.bin;type=audio/webm" \
  http://localhost:8000/e/1
# Esperado: 200 o 422 (validación). NO debe ser 413.
```

---

## Resultado esperado — resumen

| Verificación | Resultado esperado |
|---|---|
| `systemctl is-active encarreraok` | `active` |
| `GET /e/1` | `200` |
| `GET /admin/eventos` (con auth) | `200` |
| `GET /admin/eventos` (sin auth) | `401` |
| `GET /admin/eventos` (auth incorrecta) | `401` |
| `sqlite3 ... "PRAGMA integrity_check;"` | `ok` |
| `nginx -t` | `syntax is ok` |
| Upload de 5 MB | No devuelve `413` |
