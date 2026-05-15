# EncarreraOK — Arquitectura y contexto técnico

---

## Prompt de contexto para IA (copiar al inicio de cada sesión)

> EncarreraOK es un sistema de aceptación de deslindes legales para eventos deportivos en Uruguay.
> Stack: FastAPI + Jinja2 + Uvicorn. DB dual: SQLite en local (auto-migración en `main.py`), PostgreSQL en producción (migraciones via Alembic). Estructura: `main.py` inicializa la app; rutas en `app/routers/` (admin, operator, public); templates en `app/templates/`. Producción: DigitalOcean droplet, `/opt/encarreraok`, servicio systemd `encarreraok`, variables de entorno en `/etc/encarreraok.env`. Para deployar: `cd /opt/encarreraok && git pull origin main && sudo systemctl restart encarreraok`. Placeholders SQL: se usa `%s` uniformemente; en SQLite hay un wrapper `_SQLiteCompatCursor` que lo traduce. Evidencias (firmas, documentos, audios) se guardan en filesystem, nunca en DB. El email se envía via Mailgun. La rama principal es `main`.

---

## 1. Propósito

Sistema legal de aceptación de deslindes para eventos deportivos. Registra:
- Identidad del participante (nombre + documento)
- Evidencias técnicas (firma manuscrita, foto documento, audio)
- Metadata forense (IP, user-agent, timestamp UTC, hash del deslinde)

**Prioridad: legalidad y trazabilidad. No estética, no complejidad.**

---

## 2. Principios rectores

- Legalidad > UX
- Hechos comprobados > suposiciones
- Simpleza explícita > arquitectura "elegante"
- Nada se refactoriza sin razón operativa o legal clara
- "Funciona en local" no es suficiente — debe funcionar detrás de Nginx en producción

---

## 3. Stack técnico

| Componente       | Tecnología                          |
|------------------|-------------------------------------|
| Framework        | FastAPI + Uvicorn                   |
| Templates        | Jinja2                              |
| DB local (dev)   | SQLite — `data/encarreraok.sqlite3` |
| DB producción    | PostgreSQL                          |
| Migraciones      | Alembic (`alembic/versions/`)       |
| Email            | Mailgun REST API                    |
| Reverse proxy    | Nginx                               |
| Evidencias       | Filesystem local                    |
| Autenticación    | Sesiones con cookie + bcrypt        |

**No se usa:** ORM, frameworks frontend, cloud storage, Redis, WebSockets.

---

## 4. Estructura de archivos

```
encarreraok/
├── main.py                        # Entry point: init app, DB, rutas, migraciones SQLite
├── app/
│   ├── db.py                      # Conexión DB (SQLite local / PostgreSQL prod)
│   ├── routers/
│   │   ├── admin.py               # Panel admin — gestión de eventos, aceptaciones, operadores
│   │   ├── operator.py            # Panel operador — monitor en tiempo real
│   │   └── public.py              # Rutas públicas — formulario deslinde, recarga
│   ├── repositories/
│   │   └── aceptaciones_repository.py
│   ├── services/
│   └── templates/
│       ├── admin_*.html           # Templates panel admin
│       ├── op_*.html              # Templates panel operador
│       ├── deslinde_form.html     # Formulario público de aceptación
│       └── recarga_form.html      # Formulario de re-carga de documentos (tras rechazo)
├── alembic/
│   └── versions/
│       ├── 001_... → 007_...      # Migraciones PostgreSQL incrementales
├── data/
│   └── encarreraok.sqlite3        # DB local (NO commitear cambios de este archivo)
└── ARCHITECTURE.md
```

---

## 5. Base de datos — tabla principal `aceptaciones`

Columnas relevantes (estado actual):

| Columna                    | Tipo    | Descripción                                      |
|----------------------------|---------|--------------------------------------------------|
| `id`                       | INT PK  |                                                  |
| `evento_id`                | INT FK  |                                                  |
| `nombre_participante`      | TEXT    |                                                  |
| `documento`                | TEXT    |                                                  |
| `email`                    | TEXT    | Opcional, capturado en formulario                |
| `fecha_hora`               | TEXT    | UTC                                              |
| `ip`                       | TEXT    |                                                  |
| `user_agent`               | TEXT    |                                                  |
| `firma_path`               | TEXT    | Path relativo en filesystem                      |
| `doc_frente_path`          | TEXT    |                                                  |
| `doc_dorso_path`           | TEXT    |                                                  |
| `audio_path`               | TEXT    |                                                  |
| `salud_doc_path`           | TEXT    |                                                  |
| `salud_doc_tipo`           | TEXT    | `"basica"` / `"completa"`                        |
| `audio_exento`             | INT     | 1 si no requirió audio                           |
| `firma_asistida`           | INT     | 1 si fue asistida por operador                   |
| `valido`                   | INT     | 0 = anulado, NULL/1 = válido                     |
| `motivo_anulacion`         | TEXT    |                                                  |
| `fecha_anulacion`          | TEXT    |                                                  |
| `anulado_por`              | TEXT    |                                                  |
| `estado_revision`          | TEXT    | `"ACEPTADO"` / `"RECHAZADO"` / NULL = sin revisar|
| `motivo_rechazo`           | TEXT    |                                                  |
| `revisado_por`             | TEXT    | Username del admin/op que revisó                 |
| `fecha_revision`           | TEXT    |                                                  |
| `recarga_token`            | TEXT    | UUID único para link de re-carga                 |
| `recarga_token_expires_at` | TEXT    | ISO datetime UTC                                 |
| `recarga_token_usado`      | INT     | 1 si ya fue usado                                |

### Tabla `aceptaciones_historial`

Auditoría de todos los cambios de estado. Columna de fecha: **`fecha`** (no `fecha_hora`).

| Columna       | Tipo | Descripción                         |
|---------------|------|-------------------------------------|
| `id`          | INT  |                                     |
| `aceptacion_id` | INT |                                    |
| `accion`      | TEXT | `"REVISION"`, `"ANULACION"`, etc.  |
| `usuario`     | TEXT | Quien realizó la acción             |
| `detalle`     | TEXT | JSON con datos del cambio           |
| `fecha`       | TEXT | ISO datetime UTC                    |

---

## 6. Flujo completo del sistema

### 6.1 Registro de deslinde (participante)
1. Participante accede a `/e/{evento_id}`
2. Completa nombre, documento, email (opcional)
3. Adjunta evidencias según configuración del evento (firma, doc, audio, salud)
4. Se guarda en `aceptaciones` + archivos en filesystem
5. Si el evento tiene email configurado, el sistema puede notificar

### 6.2 Revisión por admin/operador
1. Admin entra al monitor `/admin/evento/{id}/monitor`
2. Ve tabla de aceptaciones con columna **Revisión** (ACEPTADO / RECHAZADO / Sin revisar)
3. Puede hacer clic en una fila → `/admin/aceptaciones/{id}` (preview con documentos)
4. Desde el preview: botones **ACEPTAR** / **RECHAZAR** (con modal para motivo obligatorio)
5. Acción registrada en `aceptaciones_historial`

### 6.3 Rechazo con re-carga (Fase 4)
1. Admin rechaza y opcionalmente envía email
2. Email incluye link único: `https://ok.encarrera.uy/recarga/{token}`
3. Participante abre link, ve motivo de rechazo y docs actuales, sube nuevos
4. Sistema actualiza paths, resetea `estado_revision = NULL`, marca token como usado
5. Admin vuelve a revisar desde el monitor

---

## 7. Roles y acceso

| Rol       | Ruta base        | Acceso                                                |
|-----------|------------------|-------------------------------------------------------|
| Admin     | `/admin/`        | Todo: eventos, aceptaciones, operadores, CSV, eliminar|
| Operador  | `/op/{evento_id}/` | Solo eventos asignados: monitor + preview + revisar |
| Público   | `/e/{evento_id}` | Formulario de aceptación                              |
| Público   | `/recarga/{token}` | Re-carga de documentos (link desde email)           |

---

## 8. Compatibilidad SQL — regla de los placeholders

Toda consulta usa `%s` como placeholder. En SQLite, el wrapper `_SQLiteCompatCursor` en `app/db.py` lo traduce a `?` automáticamente. En PostgreSQL se usa directo.

**Nunca mezclar `?` y `%s` en el mismo archivo.**

---

## 9. Migraciones

### Local (SQLite)
Auto-aplicadas en `main.py` → función `ensure_schema_migrations()`. Agrega columnas con `ALTER TABLE IF NOT EXISTS`. Se ejecutan en cada arranque; son idempotentes.

### Producción (PostgreSQL)
Alembic. Para correr en producción:
```bash
cd /opt/encarreraok
source venv/bin/activate
export $(cat /etc/encarreraok.env | xargs)
alembic upgrade head
```
Los archivos están en `alembic/versions/001_` → `007_` (numeración secuencial).

---

## 10. Evidencias en filesystem

```
/var/lib/encarreraok/evidencias/
├── firmas/          # PNG base64 decodificado
├── documentos/      # JPG/PNG del documento de identidad (frente y dorso)
├── audios/          # WebM/OGG del audio de aceptación
└── salud/           # Documento de salud (básica o completa)
```

- Nombre de cada archivo: UUID4
- Nunca se guardan binarios en DB
- Los paths en DB son relativos a la raíz de evidencias

---

## 11. Email — Mailgun

Variables de entorno requeridas en `/etc/encarreraok.env`:
```
MAILGUN_API_KEY=...
MAILGUN_DOMAIN=mg.encarrera.uy
MAILGUN_FROM=ok@encarrera.uy
MAILGUN_REGION=us
APP_BASE_URL=https://ok.encarrera.uy
```

Se usa en: notificación de rechazo con link de recarga.

---

## 12. Producción — DigitalOcean

| Ítem               | Valor                              |
|--------------------|------------------------------------|
| Directorio         | `/opt/encarreraok`                 |
| Servicio systemd   | `encarreraok`                      |
| Variables de entorno | `/etc/encarreraok.env`           |
| Virtualenv         | `/opt/encarreraok/venv`            |
| DB                 | PostgreSQL local                   |
| Dominio            | `ok.encarrera.uy`                  |
| Nginx              | Reverse proxy hacia uvicorn        |

### Protocolo de deploy

```bash
cd /opt/encarreraok
git pull origin main
sudo systemctl restart encarreraok
# verificar:
sudo systemctl status encarreraok
sudo journalctl -u encarreraok -f
```

Si hubo cambios de esquema DB:
```bash
source venv/bin/activate
export $(cat /etc/encarreraok.env | xargs)
alembic upgrade head
sudo systemctl restart encarreraok
```

Rollback de emergencia:
```bash
git reset --hard HEAD~1
sudo systemctl restart encarreraok
```

---

## 13. Límites de archivos (validados en app + Nginx)

| Tipo            | Límite  |
|-----------------|---------|
| Firma           | 1 MB    |
| Imagen documento| 4 MB    |
| Audio           | 5 MB    |
| Request total   | ~15 MB  |

---

## 14. Estado de fases

| Fase | Descripción                                              | Estado  |
|------|----------------------------------------------------------|---------|
| 1    | Deslinde legal versionado (texto + hash + asociación)    | ✅       |
| 2    | Evidencias básicas (firma, documento, audio)             | ✅       |
| 3    | Robustez mobile, 413, logging, compresión                | ✅       |
| 4a   | Admin con autenticación + operadores + CRUD eventos      | ✅       |
| 4b   | Export CSV + ZIP de evidencias + eliminación             | ✅       |
| 4c   | Email en formulario + campo email                        | ✅       |
| 4d   | Revisión ACEPTADO/RECHAZADO + historial de auditoría     | ✅       |
| 4e   | Notificación por email al rechazar (Mailgun)             | ✅       |
| 4f   | Link de re-carga de documentos (`/recarga/{token}`)      | ✅       |
| 4g   | Botones revisar desde vista de documento (preview)       | ✅       |
| 4h   | Mobile responsive en monitor, preview y tablas admin     | ✅       |

---

## 15. Reglas para asistentes IA

- No refactorizar estructura sin motivo operativo o legal
- No dividir rutas ni templates sin permiso
- No eliminar validaciones existentes
- No asumir que "funciona en local" es suficiente
- Al modificar DB: actualizar `ensure_schema_migrations()` en `main.py` (SQLite) **y** crear nueva migración en `alembic/versions/` (PostgreSQL)
- La columna de fecha en `aceptaciones_historial` se llama **`fecha`**, no `fecha_hora`
- Todos los placeholders SQL usan `%s`, nunca `?`
- Rama principal: `main`
