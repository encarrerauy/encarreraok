EncarreraOK — Architecture & Technical Decisions

1. Propósito del sistema

EncarreraOK es un sistema de aceptación legal digital para eventos deportivos, diseñado para:

Registrar aceptaciones legalmente defendibles

Asociar evidencias técnicas (firma, documentos, audio)

Funcionar de forma robusta en mobile

Operar con infraestructura mínima y control total

La prioridad del sistema es legalidad y trazabilidad, no estética ni complejidad técnica.

2. Principios rectores (NO negociables)

Legalidad > UX

Hechos comprobados > Suposiciones

Simpleza explícita > Arquitectura “elegante”

Un archivo antes que un sistema frágil

Nada se “optimiza” sin permiso explícito

Este proyecto ya pasó por producción. Las decisiones reflejan errores reales y correcciones reales.

3. Arquitectura general
3.1 Archivo único (main.py)

El sistema está deliberadamente implementado en un solo archivo main.py.

Motivos:

Evitar refactors innecesarios

Facilitar auditorías legales

Reducir superficie de errores

Mantener control total del flujo

⚠️ No dividir en múltiples archivos sin una razón legal u operativa clara.

3.2 Stack técnico

FastAPI

Uvicorn

SQLite

Jinja2

Nginx (reverse proxy obligatorio)

Filesystem local para evidencias

No se usa:

ORM

Frameworks frontend

Servicios externos

Cloud storage

4. Flujo funcional
4.1 Aceptación de deslinde

Usuario accede a /e/{evento_id}

Se carga:

Evento

Deslinde activo (versionado)

Usuario completa:

Nombre

Documento

Evidencias requeridas según evento

Se valida y guarda:

Hash del deslinde

IP

User-Agent

Fecha UTC

Paths de evidencias

4.2 Versionado legal de deslindes

Cada deslinde tiene:

Texto completo

hash_sha256

Estado activo/inactivo

La aceptación guarda el hash, no el texto

Cambiar el texto no invalida aceptaciones previas

Esto es clave legal.

5. Evidencias y almacenamiento
5.1 Base de datos (SQLite)

Guarda solo:

Metadatos

Paths a evidencias

Hashes

Flags de requerimientos

⚠️ Nunca se guardan binarios en SQLite.

5.2 Filesystem

Ubicación base:

/var/lib/encarreraok/


Subdirectorios:

evidencias/
 ├─ firmas/
 ├─ documentos/
 └─ audios/


Cada archivo:

Nombre UUID

No reutilizable

No editable

6. Mobile first (realidad comprobada)
6.1 Audio en mobile

Hechos comprobados en producción:

En mobile:

El audio puede grabarse correctamente

Pero no siempre se puede reproducir localmente

Especialmente en iOS:

Codecs y MediaRecorder son inconsistentes

Decisión:

El audio es válido aunque no se escuche

El sistema informa, no bloquea

Legalidad > preview local

⚠️ No exigir playback exitoso como condición.

7. Manejo de archivos grandes y error 413 (CRÍTICO)

Este proyecto sufrió errores 413 reales.

7.1 Doble capa obligatoria

El tamaño de requests se controla en:

Nginx

Aplicación (FastAPI)

⚠️ Nginx puede rechazar requests antes de que Python vea algo.

7.2 Límites actuales (lado aplicación)

Firma: 1 MB

Imagen documento: 4 MB por imagen

Audio: 5 MB

El sistema:

Valida tamaño antes de guardar

Devuelve HTTP 413 con mensaje claro

Registra el evento en logs

7.3 Compresión preventiva

Si una imagen supera cierto umbral:

Se intenta compresión automática

Si no se puede reducir:

Se rechaza

Se informa al usuario

⚠️ No asumir que el móvil envía imágenes “razonables”.

8. Logging operativo (NO eliminar)

El sistema registra actividad en archivo para:

Diagnóstico

Auditoría

Evidencia técnica

Incluye:

Request ID

Evento ID

Tamaños de archivos

Paths finales

Errores con stacktrace

⚠️ No eliminar logging ni “simplificarlo”.

9. Administración (estado actual)

Actualmente existe:

Listado básico de aceptaciones

Visualización de paths

Verificación de existencia de archivos

CRUD de Eventos (Crear, Editar, Activar/Desactivar)

Autenticación Basic Auth para rutas de admin

10. Fases del proyecto (estado real)
Fase 1 — Deslinde legal versionado ✅

Texto

Hash

Asociación por aceptación

Fase 2 — Evidencias básicas ✅

Firma manuscrita

Documento identidad

Audio aceptación

Fase 3 — Robustez mobile y producción ⚠️ (parcial)

Manejo 413 ✔

Límites de tamaño ✔

Compresión ✔

UX mobile audio ✔

Logging ✔

Fase 4 — En Progreso ⚠️

Admin con autenticación (Basic Auth implementada ✅)

Export legal (PDF / ZIP) (Exportación ZIP implementada ✅)

Gestión de Datos (Eliminación Parcial/Total implementada ✅)

CRUD Eventos (Implementado ✅)

Auditoría avanzada

11. Reglas para asistentes IA (Cursor / otros)

El asistente NO es el arquitecto.

Reglas obligatorias:

No refactorizar estructura sin permiso

No dividir archivos

No eliminar validaciones existentes

No asumir comportamiento mobile

Todo cambio debe:

Compilar (python -m py_compile)

Arrancar con systemd

Funcionar detrás de Nginx

⚠️ “Funciona en local” no es suficiente.

12. Despliegue y Actualización (Protocolo Obligatorio)

Cada vez que se realiza un commit y push a GitHub, se deben seguir estos pasos estrictos para actualizar el servidor de producción.

1. Acceder al servidor
   ssh root@<IP_SERVIDOR>

2. Ir al directorio del proyecto
   cd /var/www/encarreraok/app

3. Verificar estado actual
   git status

4. Traer cambios (limpiando cambios locales si es necesario, ya que main.py es la fuente de verdad)
   git fetch origin
   git reset --hard origin/main

5. Verificar sintaxis antes de reiniciar
   python3 -m py_compile main.py

6. Reiniciar servicio
   systemctl restart encarreraok

7. Verificar estado y logs
   systemctl status encarreraok
   journalctl -u encarreraok -f

8. Verificación funcional
   - Probar acceso a /admin/eventos
   - Probar carga de formulario /e/{id}

⚠️ Si el servicio falla, hacer rollback inmediato:
   git reset --hard HEAD@{1}
   systemctl restart encarreraok

13. Filosofía final

EncarreraOK no es una app, es un sistema legal.

Cada línea existe porque:

Algo falló

Algo se rompió

Algo pasó en producción
